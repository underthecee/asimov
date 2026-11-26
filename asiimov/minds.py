"""Minds: anything that turns a prompt (plus, for stand-ins, a perceived view) into raw output.

The world never trusts a mind's output: it parses, validates and returns receipts.
StubMind is a deterministic, parameter-driven stand-in used until the GPU model is up.
It is NOT the product's mind; it exists to exercise the pipeline and as a no-LLM baseline.
"""
import json
import re
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass


@dataclass
class MindRequest:
    kind: str          # plan | deliberate | reflect | answer | decide
    agent_id: int
    tick: int
    prompt: str
    prompt_hash: str
    view: dict         # perceived facts only (own state, people present, kin)
    seed: int | None = None  # sampling seed; None lets the mind derive one from the prompt
    attempt: int = 0   # 1 for the engine's one retry of a cut-off reflection (T21); 2 for a private decision (text-1)
    frequency_penalty_milli: int = 0  # thousandths; the T21 retry asks at 1000 (integers in the core)
    # text-1: an answer that offers a typed give also carries the engine's prompt for each thing a hearing can find
    # ({"food": ..., "nothing": ...}), for a mind that hears first and then writes its words to what it heard.
    told: dict | None = None


@dataclass
class MindReply:
    raw_output: str
    model: str
    sampling: dict
    finish_reason: str | None = None  # the server's ("stop", "length"); "stop" for rule minds


def mind_key(tick: int, actor: int, attempt: int = 0) -> tuple[int, int, int]:
    """How a recorded mind event is found: an agent asks at most once per tick, plus one retry (T21) and one
    private decision about a line said to it that tick (text-1)."""
    return tick, actor, attempt


def recorded_reply(ev) -> MindReply:
    return MindReply(ev.mind["raw_output"], ev.mind["model"], ev.mind["sampling"], ev.mind.get("finish_reason"))


class StubMind:
    model = "stub-rules-v0"

    def decide(self, req: MindRequest) -> MindReply:
        v = req.view
        if req.kind == "plan":
            out = self._plan(v)
        elif req.kind == "deliberate":
            out = self._deliberate(v)
        elif req.kind == "answer" and v.get("answers"):  # talk-1: the answer offered (consent nature offers one)
            yes = v["answers"] == ["yes"] or len(v["answers"]) > 1 and v["traits"]["agreeableness"] > 500
            out = {"answer": "yes" if yes else "no", "says": "Here, take one." if yes else "I can't spare any.",
                   "next": "carry_on"}
        elif req.kind == "answer" and v.get("trade"):  # deal-1: a keyed offer's answer is the world's; it words it
            yes = v["trade"]["answer"] == "yes"
            out = {"says": "All right, it's a deal." if yes else "No, I'll keep what I have.", "next": "carry_on"}
        elif req.kind == "decide":  # text-1: the private decision, by agreeableness as its chosen answers are
            out = {"answer": "yes" if v["traits"]["agreeableness"] > 500 else "no"}
        elif req.kind == "answer" and v.get("gives"):  # text-1: a stub can't hear an ask, so a typed line gets nothing
            warm = v["traits"]["agreeableness"] > 500
            out = {"gives": "nothing", "says": "Good to hear from you." if warm else "I've got things to do.",
                   "next": "carry_on"}
        elif req.kind == "answer":  # say-3: a word back, warm or curt by agreeableness; it carries on as it was
            warm = v["traits"]["agreeableness"] > 500
            out = {"says": "Good to hear from you." if warm else "I've got things to do.", "next": "carry_on"}
        else:
            out = self._reflect(v)
        return MindReply(json.dumps(out, sort_keys=True), self.model, {"temperature": 0}, "stop")

    def _plan(self, v: dict) -> dict:
        t = v["traits"]
        hour = v["hour"]
        if v["role"] in ("child", "elder"):
            blocks = [{"activity": "socialize", "start_h": min(hour + 2, 22), "end_h": min(hour + 5, 23)}]
        else:
            hours = 4 + t["conscientiousness"] * 5 // 1000
            start = hour + 1 + (1 if t["conscientiousness"] < 300 else 0)
            end = min(start + hours, 21)
            blocks = [{"activity": "work", "start_h": start, "end_h": end}]
            if v["food"] < 2 and v["coin"] >= 2 * v["food_price"] and end < 23:
                blocks.append({"activity": "shop", "start_h": end, "end_h": end + 1})  # plannable shopping
                end += 1
            if t["extraversion"] > 550 and end < 23:
                blocks.append({"activity": "socialize", "start_h": end,
                               "end_h": min(end + 1 + t["extraversion"] // 400, 23)})
        return {"plan": blocks}

    def _deliberate(self, v: dict) -> dict:
        if (v.get("occasion") or {}).get("why") == "sick":  # B5 sick-1, the sandbox's rule: the agreeable stay
            pick = "home" if v["traits"]["agreeableness"] > 500 and v["coin"] >= 2 * v["food_price"] else "work"
            return {"action": pick, "target": None, "reason": "", "goal": "", "then": []}  # home unless broke
        out = self._intent(v)
        if out is None and (v.get("occasion") or {}).get("why", "hungry") not in ("hungry", "hungry_coin"):
            out = {"action": "wait", "target": None, "reason": "nothing to do about it"}
        return {"goal": "", "then": [], **(out or self._food(v))}

    def _intent(self, v: dict) -> dict | None:
        """Occasions other than hunger: a liked person is sought out, a disliked one or a thief avoided,
        a giver thanked in kind; a conscientious agent who finished or lost an intent sets a working one."""
        t, occ = v["traits"], v.get("occasion") or {}
        why, who = occ.get("why"), occ.get("who")
        if why == "person" and occ.get("detail") in ("like", "are fond of", "care about") and t["extraversion"] > 400:
            return {"action": "seek", "target": who, "reason": "good to see a friend", "goal": "spend time with a friend",
                    "then": [{"do": "socialize", "target": None}]}
        if (why in ("robbed", "rob_tried") or occ.get("detail") in ("dislike", "can't stand")) and who is not None:
            return {"action": "avoid", "target": who, "reason": "keep clear", "goal": "keep clear of trouble",
                    "then": [{"do": "work", "target": None}]}
        if why == "gift" and v["food"] > 1:
            return {"action": "give_food", "target": who, "reason": "return the favour", "goal": "", "then": []}
        if why in ("intent_done", "blocked", "fire", "food_missing") and t["conscientiousness"] > 500:
            return {"action": "work", "target": None, "reason": "back to it", "goal": "earn for tomorrow",
                    "then": [{"do": "shop", "target": None}, {"do": "rest", "target": None}]}
        return None

    def _food(self, v: dict) -> dict:
        t = v["traits"]
        offer = v.get("offer", ())
        if "buy_food" in offer:
            return {"action": "buy_food", "target": None, "reason": "hungry, have coin"}
        if "go_market" in offer and not v["at_market"]:
            return {"action": "go_market", "target": None, "reason": "need food"}
        holders = [p for p in v["present"] if p["has_food"] and not p["kin"]]
        if t["honesty"] < 350 and holders:
            return {"action": "steal", "target": holders[0]["id"], "reason": "hungry and nobody is watching"}
        kin = [p for p in v["kin"] if p["has_food"]]
        near = [p for p in kin if p["id"] in {q["id"] for q in v["present"]}]
        if near and "ask_kin" in offer:
            return {"action": "ask_kin", "target": near[0]["id"], "reason": "family helps"}
        if kin and "seek" in offer:  # family elsewhere: go to them, then ask (ask_kin needs them here)
            return {"action": "seek", "target": kin[0]["id"], "reason": "family helps", "goal": "get food from family",
                    "then": [{"do": "ask_kin", "target": kin[0]["id"]}]}
        if holders:  # a stranger here with food: offer coin if there is some, else ask
            action = "buy_from" if "buy_from" in offer else "ask_food"
            return {"action": action, "target": holders[0]["id"], "reason": "they have food"}
        return {"action": "wait", "target": None, "reason": "nothing to do"}

    def _reflect(self, v: dict) -> dict:
        marked = [r for r in v["recent"] if any(w in r for w in ("stole", "robbed", "fire", "caught"))]
        return {"core": marked[:2], "summary": f"{len(v['recent'])} things happened today"}


class NoisyMind(StubMind):
    """Stand-in for a sampled LLM: same policy, but with OS randomness. Not reproducible."""
    model = "noisy-stub-v0"

    def decide(self, req: MindRequest) -> MindReply:
        reply = super().decide(req)
        out = json.loads(reply.raw_output)
        roll = os.urandom(1)[0]
        if "plan" in out and out["plan"] and roll < 128:
            out["plan"][0]["end_h"] = max(out["plan"][0]["start_h"] + 1, out["plan"][0]["end_h"] - 1)
        elif out.get("action") == "steal" and roll < 128:
            out = {"action": "wait", "target": None, "reason": "lost nerve"}
        return MindReply(json.dumps(out, sort_keys=True), self.model, {"temperature": 0.7}, "stop")


class ScriptMind(StubMind):
    """The stub, saying one fixed sentence per act kind (test 16's blind pair, plan/test16-live-rule.md): its
    acts aimed at a player show words, as a mind's do, so missing words don't give the script away. Only the
    reply's reason (its own act's sentence) and, for an intent, its goal (the sentence of its first act of
    these kinds, which every such step shows, as an 8B intent's goal is) are rewritten; the engine reads
    neither, so the world is the stub's act for act. Its state hash differs, since an intent's goal and an
    occasion's detail live in state."""
    model = "script-v0"
    SAYS = {"ask_kin": "We're family, and I could use some food.",
            "ask_food": "Could you spare some food? I'm hungry.",
            "buy_from": "I'd like to buy some food from you.",
            "give_food": "Here, take some food.",
            "give_coin": "Here's a coin for you.",
            "seek": "I wanted to see you."}

    # What the answer does next, as the voice is told it (script-voice): the stub's choice, never one to make.
    # say-3b (Steering 08:28Z): the answer occasion's words, mirrored.
    NEXT_WORDS = {"stay": "stay here talking with them", "carry_on": "end the talk and go on with their day",
                  "leave": "get away from them, and avoid them for a while"}
    # When the voice gives no words (Steering 12:14Z): the act's own sentence, warm or curt as the stub's; a
    # "good to hear from you" only answers a greeting.
    FALLBACK = {"stay": ("I'll stay a bit.", "Fine, I'll stay."), "leave": ("I'd better be going.", "I'm off."),
                "carry_on": ("I'll get back to it, then.", "I've got things to do.")}
    GREETS = re.compile(r"^\W*(hi|hello|hey|greetings|good (morning|afternoon|evening|day)|morning|evening)\b", re.I)

    def __init__(self, voice=None, hear=None):
        self.voice = voice  # script-voice: a narrator that words the stub's answer to the player, or None
        self.facts = None  # script-voice (Steering 12:05Z): req -> the world facts the voice may name (voice_facts)
        # text-1 (Steering 2026-11-19 07:40Z, ruling (e)): the pair's arms share the typed give. Where the world offers
        # one (the stub's roll, held, was yes), hear(req) -> ("food" | "nothing" | None, how) says whether the player's
        # words ask for food: llm.Hearer, the minds arm's hearing with no soul, diary or memory. None: a script that
        # can't hear, as the stub.
        self.hear = hear

    def decide(self, req: MindRequest) -> MindReply:
        reply = super().decide(req)
        heard = req.kind == "answer" and self.hear is not None and bool(req.view.get("gives"))
        if heard or (req.kind == "answer" and self.voice is not None and not req.view.get("trade")):
            # (deal-1: a keyed offer has no line for the voice to answer; the stub's own words say its answer)
            out = json.loads(reply.raw_output)
            sampling, model = dict(reply.sampling), self.model
            if heard:  # it only hears: the roll already decided, and the engine gives only what it offered
                gives, how = self.hear(req)
                out["gives"] = gives if gives in req.view["gives"] else "nothing"
                if out["gives"] == "food":
                    out["says"] = "Here, take one."  # the stub's own words for a yes, unless the voice words it
                sampling["hear"] = how
                model += f"+hear:{how.get('model')}"
            if self.voice is None:
                return MindReply(json.dumps(out, sort_keys=True), model, sampling, reply.finish_reason)
            # script-voice (Steering 2026-11-16 10:18Z/11:17Z, joon's choice 11:16Z): the stub's `next` stays; the
            # narrator writes `says` from the player's line, that next and the place facts only (no soul, no diary).
            # The line is this mind's output, so the log records it and a replay serves it back: replay stays exact.
            occ = req.view.get("occasion") or {}
            if self.facts is not None:
                facts = self.facts(req)
            else:
                m = re.search(r"You are at ([a-z_0-9]+)\.", req.prompt)
                facts = {"place": m.group(1) if m else None, "hour": req.view.get("hour", 12),
                         "role": req.view.get("role")}
            if req.view.get("answers"):  # talk-1 (review of #96): the voice words the stub's yes or no, never guesses
                buy = occ.get("move") == "wants_to_buy_food"
                facts = {**facts, "deal": (("yes, and they sell them 1 food for %s coin now" % occ.get("price")) if buy
                                           else "yes, and they hand them 1 food now")
                         if out.get("answer") == "yes" else "no, and nothing changes hands"}
            elif out.get("gives") == "food":  # text-1: a typed ask it heard, on a held yes
                facts = {**facts, "deal": "yes, and they hand them 1 food now"}
            ask = getattr(self.voice, "answer", None)  # a voice that says why it fell back (review of #81)
            args = (str(occ.get("detail", "")), self.NEXT_WORDS.get(out.get("next"), "carry on"), facts)
            said, why = ask(*args) if ask is not None else (self.voice.reply(*args), None)
            if said:
                out["says"] = said
            elif req.view.get("answers") or out.get("gives") == "food":
                pass  # the stub's own words for its answer: "Here, take one." / "I can't spare any."
            else:
                warm = out.get("says") == "Good to hear from you."
                line = str(occ.get("detail", ""))
                if not (warm and self.GREETS.search(line)):
                    out["says"] = self.FALLBACK.get(out.get("next"), self.FALLBACK["carry_on"])[0 if warm else 1]
            sampling = {**sampling, "voice": self.voice.sampling}
            if not said:  # the cause goes in the log with the reply, so a run counts fallbacks by cause
                sampling["voice_why"] = why or "no words"
            return MindReply(json.dumps(out, sort_keys=True), f"{model}+voice:{self.voice.model}",
                             sampling, reply.finish_reason)
        if req.kind != "deliberate":
            return reply
        out = json.loads(reply.raw_output)
        if out.get("action") in self.SAYS:
            out["reason"] = self.SAYS[out["action"]]
        kinds = [out.get("action")] + [st.get("do") for st in out.get("then") or []]
        lead = next((k for k in kinds if k in self.SAYS), None)
        if out.get("then") and lead is not None:  # an intent's steps aimed at the player show its goal
            out["goal"] = self.SAYS[lead]
        return MindReply(json.dumps(out, sort_keys=True), self.model, reply.sampling, reply.finish_reason)


class ReplayMind:
    """Serves recorded outputs by (tick, agent) and refuses any request that doesn't match the record.

    Keyed, not sequential: requests are asked in agent order but committed in the world's
    acting order, and an agent asks at most once per tick.
    """

    def __init__(self, mind_events: list):
        self.record = {}
        for ev in mind_events:
            # pace-2: a plan or reflection lands two ticks after it was filed, and is found by its filing tick
            key = mind_key(ev.data.get("filed", ev.tick), ev.actor, ev.data.get("attempt", 0))
            if key in self.record:
                raise ReplayDivergence(f"two mind events for agent {ev.actor} at tick {ev.tick}")
            self.record[key] = ev

    def decide(self, req: MindRequest) -> MindReply:
        ev = self.record.pop(mind_key(req.tick, req.agent_id, req.attempt), None)
        if ev is None:
            raise ReplayDivergence(f"unrecorded mind request at tick {req.tick} for agent {req.agent_id}")
        expected = (ev.data["kind"], ev.mind["prompt_hash"])
        got = (req.kind, req.prompt_hash)
        if expected != got:
            raise ReplayDivergence(f"mind request mismatch at tick {req.tick} for agent {req.agent_id}: "
                                   f"expected {expected}, got {got}")
        return recorded_reply(ev)


def dispatch(mind, reqs: list[MindRequest], workers: int = 1) -> list[MindReply]:
    """Ask the mind every request of a tick; replies come back in request order, however they arrive.

    workers > 1 calls the mind from that many threads, so the mind must be thread-safe (LLMMind is).
    """
    if workers <= 1 or len(reqs) <= 1:
        return [mind.decide(r) for r in reqs]
    with ThreadPoolExecutor(min(workers, len(reqs))) as ex:
        return list(ex.map(mind.decide, reqs))


class ForbiddenMind:
    def decide(self, req: MindRequest) -> MindReply:
        raise AssertionError("a mind was called during replay")


class ReplayDivergence(Exception):
    pass
