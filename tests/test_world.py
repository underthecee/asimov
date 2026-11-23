import hashlib
import json

import pytest

from asiimov.events import ORIGINS, read_jsonl, write_jsonl
from asiimov.memory import SECTION_BUDGETS, assemble_prompt, render_files
from asiimov.metrics import icc1, summarize
from asiimov.minds import NoisyMind, ReplayDivergence, StubMind
from asiimov.replay import replay
from asiimov.rng import Rng
import asiimov.sim as sim_mod
from asiimov.sim import ENGINE_VERSION, TICKS_PER_DAY, Sim

FIRE = {TICKS_PER_DAY + 60: [{"kind": "fire", "family": 2}]}


def run(mind, agents=30, days=2, seed=7, player=None):
    sim = Sim(seed, agents, mind)
    sim.run(days, player or {})
    sim.finish()
    return sim


def test_rng_is_a_pure_function_of_seed_stream_index():
    a, b = Rng(3), Rng(3)
    assert [a.draw("x", 0, 99) for _ in range(5)] == [b.draw("x", 0, 99) for _ in range(5)]
    assert Rng(3).raw("x", 4) == Rng(3).raw("x", 4) != Rng(4).raw("x", 4)


def test_same_seed_same_world():
    s1, s2 = run(StubMind(), player=FIRE), run(StubMind(), player=FIRE)
    assert [e.to_json() for e in s1.events] == [e.to_json() for e in s2.events]
    assert s1.state_hash() == s2.state_hash()


def test_noisy_minds_diverge_but_each_log_replays_exactly(tmp_path):
    s1, s2 = run(NoisyMind(), player=FIRE), run(NoisyMind(), player=FIRE)
    assert s1.state_hash() != s2.state_hash()  # sampled minds are not reproducible by recomputation
    for s in (s1, s2):
        path = tmp_path / f"{id(s)}.jsonl"
        write_jsonl(s.events, str(path))
        r = replay(read_jsonl(str(path)))  # no model is called; recorded outputs are served
        assert r.state_hash() == s.state_hash()
        assert len(r.events) == len(s.events)


def test_replay_detects_a_tampered_mind_output():
    s = run(NoisyMind())
    events = [type(e)(**json.loads(e.to_json())) for e in s.events]
    target = next(e for e in events if e.kind == "mind_plan")
    target.mind["raw_output"] = json.dumps({"plan": [{"activity": "rest", "start_h": 8, "end_h": 20}]})
    with pytest.raises(ReplayDivergence):
        replay(events)


def test_every_event_has_provenance():
    s = run(StubMind(), player=FIRE)
    seen = set()
    for e in s.events:
        assert e.origin in ORIGINS
        assert all(p in seen for p in e.parents), f"event {e.id} cites a later or missing parent"
        if e.origin == "mind":
            assert {"model", "prompt_hash", "prompt_tokens", "sampling", "raw_output"} <= set(e.mind)
        if e.kind == "receipt":  # a deliberation or plan, or an intent a deliberation set, or the neighbor rule
            p = s.events[e.parents[0]]
            assert (p.origin == "mind" or (p.kind == "intent_set" and s.events[p.parents[0]].origin == "mind")
                    or p.kind in ("sick_news", "trade_due"))  # the neighbor channel's and trade-1's rules
        seen.add(e.id)
    deliberations = {e.id for e in s.events if e.kind == "mind_deliberate"}
    assert deliberations == {e.parents[0] for e in s.events if e.kind == "receipt"} & deliberations  # each has one


def test_individual_rhythms_are_stable_within_and_different_between_agents():
    s = run(StubMind(), agents=100, days=5, seed=1)
    m = summarize(s.events, s.metrics["activity"], 100)
    assert m["variety"]["wake_icc"] > 0.8
    lo, hi = m["variety"]["wake_range_min"]
    assert hi - lo >= 180


def test_icc_separates_character_from_noise():
    assert icc1({1: [1, 1, 1], 2: [5, 5, 5], 3: [9, 9, 9]}) > 0.99
    assert icc1({1: [1, 9, 5], 2: [9, 1, 5], 3: [5, 9, 1]}) < 0.1


def test_player_intervention_propagates_against_a_twin(monkeypatch):
    # Propagation runs through scarcity (hunger, theft, grudges). With engine 5's fed world the
    # stub's fire reaches nobody outside the family (seeds 1, 2, 5, every family), a true negative
    # (S4); the propagation machinery is tested in the engine-1..4 economy.
    monkeypatch.setattr(sim_mod, "FARM_YIELD", 1)
    monkeypatch.setitem(sim_mod.DEFAULT_STAKES["year"], "yields", [1] * 7)  # the same scarcity under the year (B5)
    base = run(StubMind(), agents=60, days=4, seed=5)
    fire = run(StubMind(), agents=60, days=4, seed=5, player={TICKS_PER_DAY + 60: [{"kind": "fire", "family": 2}]})

    def seq(sim):
        out = {}
        for e in sim.events:
            if e.kind == "activity" and e.tick >= 3 * TICKS_PER_DAY:
                out.setdefault(e.actor, []).append((e.tick, e.data["activity"]))
        return out

    a, b = seq(base), seq(fire)
    changed = [k for k in set(a) | set(b) if a.get(k) != b.get(k)]
    family = set(fire.state["families"][2]["members"])
    assert set(changed) - family, "the fire changed nobody outside the burned family"
    fire_id = next(e.id for e in fire.events if e.kind == "fire")
    assert any(fire_id in e.parents for e in fire.events), "nothing cites the fire as a cause"


def test_memory_files_and_prompt_budgets():
    s = run(StubMind(), agents=40, days=2)
    souls = {a["soul"] for a in s.state["agents"]}
    assert len(souls) == 40
    for a in s.state["agents"]:
        files = render_files(a, s.state)
        assert set(files) == {"soul.md", "core.md", "goals.md", "relationships.md", "beliefs.md",
                              "recent.md", "episodes.md"}
        _, counts = assemble_prompt(a, s.state, "deliberate", "now")
        for name, n in counts.items():
            assert n <= SECTION_BUDGETS[name] + 1


def test_aborted_run_keeps_its_recorded_events(tmp_path, monkeypatch):
    """A paid run that dies mid-way (here: the mind fails on day 2) must not lose the log, and
    --resume must finish it with exactly the log an uninterrupted run writes."""
    import sys

    from asiimov import run as run_mod

    class Dying(StubMind):
        def decide(self, req):
            if req.tick >= 2 * TICKS_PER_DAY:
                raise RuntimeError("LLM server unreachable")
            return super().decide(req)

    args = ["run", "--agents", "10", "--days", "4", "--fire", "1@1"]
    monkeypatch.setattr(run_mod, "StubMind", Dying)
    monkeypatch.setattr(sys, "argv", args + ["--out", str(tmp_path / "a")])
    with pytest.raises(RuntimeError):
        run_mod.main()
    events = read_jsonl(str(tmp_path / "a" / "events.jsonl"))
    assert events[0].kind == "genesis" and events[-1].tick == 2 * TICKS_PER_DAY - 1

    monkeypatch.setattr(run_mod, "StubMind", StubMind)
    monkeypatch.setattr(sys, "argv", ["run", "--resume", "--days", "4", "--out", str(tmp_path / "a")])
    run_mod.main()
    monkeypatch.setattr(sys, "argv", args + ["--out", str(tmp_path / "b")])
    run_mod.main()
    a, b = ((tmp_path / d / "events.jsonl").read_bytes() for d in "ab")
    assert a == b
    assert replay(read_jsonl(str(tmp_path / "a" / "events.jsonl"))).state_hash() == \
        json.loads((tmp_path / "b" / "metrics.json").read_text())["final_hash"]


# sha256 of a fixed world's log, per engine version. If this fails, the rules or the log format changed:
# bump ENGINE_VERSION (old logs then refuse to replay) and record the new hash here.
GOLDEN = {"1": "fa6d1e9898c97fe570590e1ad322d952375b2941c4828001b5ea933c6e4d369b",
          "2": "6dd170b56c91610ef4359f4850f451a87224fb9ed5cb9edf7ec552d0fe884f4d",  # 42adb9e, never merged
          "3": "7b0e876ed43e135033a9b9b6e819c176e70c2e4aeb3b3b14f9b2f8d3ad71088d",
          "4": "87be8cccf05d032f2dc428e615b7f45ad3981da0c735ff9edb2ce74cb48d3c32",
          "5": "49abfd07071dbe24572a47b4ce6716dbbee71d069b82cd3f6353679e04a20350",
          "6": "c7dfb5029e3ea1dbff76b179761d367530fc7e3cbd54d3508c04e76b1344ced8",
          "7": "0b3a32589883725f55439d837727557df5a288eebba3234aaa59f1f2629d1fb9",  # PR #19
          "8": "845a7abb49589da562cbd54dbc952d62a7d61606d00f13a390f4d3cc669b5560",  # PR #20
          "9": "a852794a80a740798ff7d47b30f8a1346cd6fb2f148d500da799ab52d4cc8179",
          "10": "afe795e982af567282a9742b9b5488e0a377cdfce28e8652bb6c564cdd5a39ac",
          "11": "25de1d51e9406c0aeb94bdae72a9c31a000fd4bca37d133846943404207e8ef5",  # T22 keyed draws
          "12": "a3e7b1d70f26733666ce084896eafa6adfc259a54bf33597b12bda520622ae53",  # a repeat ask in a tick rolls fresh
          "map-1": "7bd6f6567899e387e957f998fdcc095304e135f7545c5c42d6ccc9e4dc595ebe",  # the map branch on engine 11
          "map-2": "7d1afa044e8172f2d4224a322fe9e4c15e3da7f6520729997c402279bcabd0cb",  # the map branch on engine 12 (plan/t6-map-spec.md); on main from PR #30
          "14": "45e315cbf4c4ca75beee96fedf760ce4003e1b0c53763d7785dba2d31ae22cb7",  # T21 reflect retry, finish_reason, on the map
          "t25-1": "664b54b907f9143d0d4ed7140194d1690b034c026fae9ed679d5c417fc96bcc7",  # T25 on engine 14, before its number
          "15": "1a1677ccd7eec0952ccfa9e52dca9ad9d8e1511391a8eddf7525a48158636f9a",  # T25 anchored souls
          "pb-1": "00c77fb3225c8ef16e5ed9d847bb0ecd49943624afb9ae2e5f26641c7a8bf862",  # the player's body on 15, before its number
          "16": "eb9ad93f69adb82ff2a357a437ee45a5e57fc50d813f19ae358aee68432638b5",  # the player's body; only genesis' engine field differs from 15
          "idle-1": "4177bc34b2023defa6c75b6d8e2e3ba7233060ae4ddc8902e8c711a384d3c5ce",  # an idle body's mind takes over, before its number
          "17": "981d6255337c66f57bb9ec1f1230e3aad8ee30ef31d03a3056a55f5e483075a0",  # an idle body's mind takes over; only the engine field differs from 16
          "tithe-1": "d0e6fa0c0ec9feaff5f97040248904c7bf0959d0c5dfa0a7031903db214281b5",  # B5 coin sink, on 17, before its number
          "18": "90ff72036ee3f1a7098a5205eb87322ae78079302f2e1252e81a5a65e7bef4a8",  # B5 coin sink; only the engine field differs from tithe-1
          "pay-1": "68989ccaaca8bef56d493eb158644e2e77a459d831091ca60ebfd87dc068e999",  # B5 pay compression, on 18, before its number
          "19": "fa2887a8e6fd6dcddf0f27b94150b99409a3d9c70ea17cc3887861a4ec98c254",  # B5 pay compression; only the engine field differs from pay-1
          "year-1": "330a666ed148282a5b9cfb4a54df0a8ea5a0d7d1f3810be05f9b880393689059",  # B5 the year and the price, on 19, before its number
          "20": "6a3aa743936f75c44d8dc603cf6197823245ad38b8d5304b26aeb3bc8e769dee",  # B5 the year and the price; only the engine field differs from year-1
          "sick-1": "9355d331182baddd94bca146d71a755411b18d86c10295c514736c263d19bb4d",  # B5 sickness and its ask, on 20, before its number
          "21": "a516d4439513be03a9b37b8077401fd2bc2838b84e2a22dd984c5c4a63f20e8e",  # B5 sickness and its ask; only the engine field differs from sick-1
          "alms-1": "e445c8c92f83f60eb7901cb07ba417dacc5a2c16e3224ce3f92549030a1f82b4",  # G1 fix: the chest covers a short buy, on 21, before its number
          "22": "f8e02d5476a6d4a53eebf1695f43dc57aee0935ac40bb99e744f8e80ee553b78",  # G1 fix; only the engine field differs from alms-1
          "player-ask-1": "0ff5dbc8bbbbc7fca23303b121c9766d616d93b8bd3ce0107115ed57d1ebab38",  # the player answers asks, on 22, before its number; no player: only the engine field differs
          "23": "8256877ebe7404477e3f5a7e660f8a754348e6a32425469f1c34a79afc30fbad",  # the player answers asks; only the engine field differs from player-ask-1
          "go-to-1": "8d0e7638edd81b1228af397331db5001599b59fee135bdde60024a009499e4f1",  # the player's go_to, on 23, before its number; no go_to: only the engine field differs
          "24": "6921592f2151f9bb0daeb15ec6f0072b76ce75b6bc87e6342880eef4915e3e31",  # the player's go_to; only the engine field differs from go-to-1
          "neighbor-2": "1d4c8442506b03c5187f5806aa213b8a88e82a7f76c741a9bf81d0ce7e36c932",  # G3w-b the sick-neighbor channel as a trait rule, on 23, before its number
          "25": "0b305730d87847be5430e52f34ff074a3f2744309513e63d3804fccfea9dc0d3",  # G3w-b; only the engine field differs from neighbor-2
          "trade-1": "52c8b3689e1824890fbd69e0a478edc342072ad899f541e80195bd4351ab359d",  # DEP-a trade needs across families, on 25, before its number
          "26": "3cef40e24fb78a22fc95c2509ac032e65b04986e6aefbc82ec100012ec01dc86",  # trade-1; only the engine field differs
          "pace-1": "f4101d303a04c7f4db9db4e0b3c6055da9fcfef04edc90a6a7c487c31a9aea9d",  # E4 player acts at a fixed point, on 26, before its number; no player: only the engine field differs
          "27": "aee18ad622951b9e03deefbf4067eca691ece557a0ac6377ffbde9fb56235bc2",  # E4 pace-1; only the engine field differs
          "supply-1": "2f70eeac6e909949eb6d35e0e0b0970ca594a35a2f5d215359a433605063348c",  # G1-r: inputs last a day longer, runs off work, none when sick; on 27, before its number
          "28": "2b49daeed7ca48e16d454511eb400be45b70b844cc19d1f58649b7140f7da9fa",  # G1-r supply-1; only the engine field differs
          "say-1": "72f25bb2d64cc327b74bb0611e2b72701b17a7a3b0b4d0e25916dbc8f6448689",  # the player's say, on 28, before its number; no say: only the engine field differs
          "29": "206f21041573c3ea0dfff4f1fe8de21ab90be2ab426077bafd0ab086c0b3952b",  # say-1 (#73), the player says a line; no say: only the engine field differs
          "say-2": "b60c73ac39c36f24162c7a8f4efcb08374d8efe493d6e3c43ae20ed71e20cc49",  # say-2: theft barred in a talk window, on 29, before its number; no say: only the engine field differs
          "30": "a7e119f7285d75e164aaebef401ed44882fec20e46483bb9069b3b69584f65ac",  # say-2 (#75), no theft or gift in a talk window; no say: only the engine field differs
          "say-3": "ab40deb5b201960554d8c5d8f1d5f13593e2ce20c4231c00cee20c2b73db7955",  # say-3: a spoken answer's words in `says`, on 30, before its number; no say: only the engine field differs
          "31": "7d56872a5271feaea7d9a44c51e1e25dedd4a8151e052926a11d2ef493732bf3",  # say-3 (#78), a spoken answer's words; no say: only the engine field differs
          "say-3b": "4a3287eac6834cecf42965d6fb09e339f80b557cb62abb0ca85164db5a8a63a9",  # say-3b: answer prompts name people, leave = avoid, on 31, before its number; no say: only the engine field differs
          "32": "1d0bb80d29a3b688cb7f117765dcfc417a028047e0ffe2ccdef593f4fb5018ed",  # say-3b (#85); no say: only the engine field differs
          "say-3c": "4ada6cc0245843982de2b916d8f327c3deb4193b69229818e51bf11dbf7bfb3f",  # say-3c: replies clipped at a sentence or word, on 32, before its number; no say: only the engine field differs
          "33": "ac999cef1ebf6a1c1340c7ab2a5688431a72d3a92146dbb0f6c4a8820aa70bfe",  # say-3c (#88); no say: only the engine field differs
          "talk-1": "e21c7dfef34ffdb72df958131890a3fe4e5041f0335a9fe3d261b53e44c2a370",  # talk-1: an ask with a chip, a yes moves food, on 33, before its number; no say: only the engine field differs
          "34": "7a0862cc1928d90fd89ead9e29f37b49be5f42a485ffed890865625a77c064de",  # talk-1 (#96), an ask with a chip; no say: only the engine field differs
          "talk-1g": "3682d85d16595b3669729027e7243f23a5af50095fe478074fdd4b239e21f1f4",  # talk-1g: the injection guard on chip lines, on 34, before its number; no say: only the engine field differs
          "35": "bb18713cb07e55973a6dd3cd1fc1783c10d87e43cbd3cef3f68632fec9d12de0",  # talk-1g (#97), the injection guard; no say: only the engine field differs
          "pace-1": "061d34b0c2d8ea7c4c33c89a9495a9060285c37188a758923d9ee6797f12442b",  # pace-1: plan/reflect wait a tick for a player's answer, on 35, before its number; no say: only the engine field differs
          "36": "5300954a78dcdc78cf3f08227dec4738e738c5c8621235ff500a142a64d74c56",  # pace-1 (#99), plan/reflect wait a tick for a player's answer; no say: only the engine field differs
          "text-1": "a1a1cef15b280cd20c28f256310021048dc20a9b5cc3a9d871f788baf02054cb",  # text-1: a private decision and a typed line's give, on 35, before its number; no say: only the engine field differs
          "37": "fe59f5e4ba19f7f35cc67a0382e466367abe13cbffc12bfeae5bfeffc770bb2f",  # text-1 with apart-1 and think-1 (#101), a typed ask settled by the private yes; no say: only the engine field differs
          "apart-1": "f44c05e0c7390243a435a973ea22852b19cfe2b15983a70e12621e4255810e5c",  # apart-1: no yes offered in talk once the two have parted, on text-1 and 36, before its number; no say: only the engine field differs
          "pace-2": "bc1893ff9e820624e8aeacda5927a9bfa434bde68303e685bf57762fff731655",  # pace-2: held plan/reflect calls fly to t+2, on 37, before its number; no player held: only the engine field differs
          "38": "f0221edafa0e75fc34bf9ff766c09b507c6d2d1a09ea794227b152ca0bdae583",  # pace-2 (#104), held plan/reflect calls fly to t+2; no player held: only the engine field differs
          "walk-1": "3296b5cca558495ecec9ebbb5c0f8b8a8b37f6d1e3537733d147c67721f6adf4",  # walk-1: the player walks its body tile by tile, on 38, before its number; no walk: only the engine field differs
          "39": "80e0303b704a8d019f0edb3d4d73cbd50c8a3acc8accf66b6184b02dbace189d"}  # walk-1 (#110), the player walks its body tile by tile; no walk: only the engine field differs


def test_log_changes_bump_the_engine_version():
    s = run(StubMind(), agents=20, days=2, seed=3, player=FIRE)
    digest = hashlib.sha256("".join(e.to_json() + "\n" for e in s.events).encode()).hexdigest()
    assert GOLDEN.get(ENGINE_VERSION) == digest, f"engine {ENGINE_VERSION} log hash is now {digest}"
