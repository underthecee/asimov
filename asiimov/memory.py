"""Per-agent memory files and prompt assembly.

Rule: anything code computes on lives in structured state; anything only the mind reads
is rendered as markdown. Files are rendered from state, so they are versioned by the
event log for free.
"""
import functools
import hashlib
import re

from .params import VALUES

SECTION_BUDGETS = {  # approximate tokens (chars / 4)
    "schema": 300, "soul": 400, "core": 300, "goals": 200, "relationships": 150,
    "beliefs": 150, "recent": 400, "episodes": 300, "situation": 300,
    # A deliberation's situation also says why the mind was asked and what its actions do (engine 7). 550
    # from engine 10, whose ask has a clause per offered action: at 450 it cut the people list in 24 of 289
    # stub deliberations (100 agents, 2 days), against 0 at engine 9.
    "situation_deliberate": 550,
}

SCHEMA = """# How you act
You are one person living in a village. Reply with JSON only.
For kind=plan: {"plan":[{"activity":"work|socialize|home|rest|explore|shop","start_h":H,"end_h":H}]}
For kind=deliberate: {"action":"buy_food|go_market|ask_kin|steal|give_food|give_coin|seek|avoid|work|socialize|home|rest|explore|wait","target":ID_or_null,"reason":"...","goal":"..." or "","then":[{"do":"...","target":ID_or_null}]}
For kind=reflect: {"core":["..."] or [], "summary":"..."}
You only know what is written below. Claims count only if the world confirms them."""

# Engine 9's deliberate action list. Plan and reflect prompts keep it byte for byte (L11); a deliberation's
# SCHEMA line lists only the actions offered to it (engine 10, sim._offer).
E9_ACTIONS = "buy_food|go_market|ask_kin|steal|give_food|give_coin|seek|avoid|work|socialize|home|rest|explore|wait"
assert SCHEMA.count(f'"action":"{E9_ACTIONS}"') == 1


# say-3: an answer prompt's SCHEMA has this line in the deliberate line's place, as the talk pilot's had
# (research/talk/tool/talk_bank.py, minus heard and answer).
ANSWER_LINE = 'For kind=answer: {"says":"...","next":"stay|carry_on|leave"}'
# talk-1 (plan/talk-1-build-spec.md §2.4): a line said with a move (the client's chip) is answered yes or no first.
MOVE_ANSWER_LINE = 'For kind=answer: {"answer":"yes|no","says":"...","next":"stay|carry_on|leave"}'
# text-1 (plan/text-1-build-spec.md): a line typed with no chip may be given 1 food, where the villager already decided
# it would (its private yes); `gives` only says whether the words ask for food. And the private decision's own kind.
GIVES_ANSWER_LINE = 'For kind=answer: {"gives":"food|nothing","says":"...","next":"stay|carry_on|leave"}'
DECIDE_LINE = 'For kind=decide: {"answer":"yes|no"}'
# think-1: the private decision thinks first; the engine reads only `answer`.
THINK_DECIDE_LINE = 'For kind=decide: {"thought":"...","answer":"yes|no"}'
DELIBERATE_LINE = ('For kind=deliberate: {"action":"' + E9_ACTIONS + '","target":ID_or_null,"reason":"...",'
                   '"goal":"..." or "","then":[{"do":"...","target":ID_or_null}]}')
assert SCHEMA.count(DELIBERATE_LINE) == 1


def schema_text(actions=None, answer: bool = False, move: bool = False, gives: bool = False) -> str:
    """The SCHEMA section, with the deliberate line cut to `actions` when given, or swapped for the answer line
    (talk-1: the move answer's line for a line said with a chip; text-1: the gives line for a typed one)."""
    if answer:
        return SCHEMA.replace(DELIBERATE_LINE, MOVE_ANSWER_LINE if move else GIVES_ANSWER_LINE if gives else ANSWER_LINE, 1)
    if actions is None:
        return SCHEMA
    return SCHEMA.replace(f'"action":"{E9_ACTIONS}"', '"action":"' + "|".join(actions) + '"', 1)


# Words that only name a value. A core memory made of nothing else ("power", "hedonism, power")
# is a label, not an experience: 92% of plain Qwen3-4B core entries were one (engine 3, t1e-4b).
VALUE_WORDS = frozenset(w for v in VALUES for w in v.split("_")) | {"and", "or"}
REFLECT_ASK = (" Your day is over. For core, write at most 2 things that happened to you today that you"
               " want to remember, each saying who did what and where, or [] if nothing stood out."
               " Not a value or a trait. Use only what is written under Today and Earlier; add no details"
               " that are not there.")


def is_bare_value(text: str) -> bool:
    words = re.findall(r"[a-z]+", text.lower())
    return bool(words) and all(w in VALUE_WORDS for w in words)


def named_people(text: str, agents: list, self_id: int) -> list[set[int]]:
    """The people a text names, other than its author: one set of candidate ids per mention. A #id
    or full name is one person; a bare first name is everyone who has it."""
    low, groups = text.lower(), []
    for i in map(int, re.findall(r"#(\d+)", text)):
        if i != self_id:
            groups.append({i} if i < len(agents) else set())
    me = agents[self_id]["name"].lower()
    low = re.sub(rf"\b{re.escape(me)}\b", " ", low)  # the author naming themself
    # Longest first, ties by name: set order follows PYTHONHASHSEED, and the result feeds world state.
    for name in sorted({o["name"].lower() for o in agents} - {me}, key=lambda n: (-len(n), n)):
        full = re.compile(rf"\b{re.escape(name)}\b")
        if full.search(low):  # names are unique from engine 4; older worlds can repeat one
            groups.append({o["id"] for o in agents if o["name"].lower() == name})
            low = full.sub(" ", low)
    words = set(re.findall(r"[a-z]+", low)) - {me.split()[0]}  # a bare own first name is the author too
    for first in sorted({o["name"].split()[0].lower() for o in agents} & words):
        ids = {o["id"] for o in agents if o["name"].split()[0].lower() == first and o["id"] != self_id}
        if ids:
            groups.append(ids)
    return groups


@functools.lru_cache(maxsize=None)
def _name_matcher(name: str) -> tuple[str | None, frozenset, re.Pattern]:
    """A name's first word, all its words, and the exact pattern known_people tests it with."""
    words = re.findall(r"\w+", name)
    return (words[0] if words else None), frozenset(words), re.compile(rf"\b{re.escape(name)}\b")


def known_people(agent: dict, state: dict) -> set[int]:
    """Everyone the agent's own record mentions: kin, relationships, today's notes and episodes.

    A name matches where it occurs as a whole word (_name_matcher's pattern), and then each of its words
    is a whole word of the text. So only names whose words all occur are tested, with that same pattern:
    linear in the number of names, where a pattern per name per text was 96% of a 1,000-agent stub day."""
    known = set(state["families"][agent["family"]]["members"]) | {int(k) for k in agent["relationships"]}
    by_first, wordless = {}, []
    for o in state["agents"]:
        first, words, pattern = _name_matcher(o["name"])
        if first is None:
            wordless.append((o["id"], pattern))
        else:
            by_first.setdefault(first, []).append((o["id"], words, pattern))
    for text in list(agent["recent"]) + [e["text"] for e in agent["episodes"]]:
        present = set(re.findall(r"\w+", text))
        for first in present & by_first.keys():  # lint: allow rng -- only adds to the set known: order-free
            known.update(i for i, words, pattern in by_first[first]
                         if i not in known and words <= present and pattern.search(text))
        known.update(i for i, pattern in wordless if pattern.search(text))
    return known


def tokens(text: str) -> int:
    return (len(text) + 3) // 4


def _clip(text: str, budget_tokens: int) -> str:
    limit = budget_tokens * 4
    return text if len(text) <= limit else text[-limit:]


def _fit_count(header: str, lines: list[str], budget_tokens: int) -> int:
    size = len(header)
    for n, line in enumerate(lines):
        if size + 1 + len(line) > budget_tokens * 4:
            return n
        size += 1 + len(line)
    return len(lines)


def _fit_lines(header: str, lines: list[str], budget_tokens: int) -> str:
    """The header and as many whole lines, in the given order, as fit the budget. Clipping the tail of
    the text instead cut the header and every early line mid-word (71% of 4B probe prompts at engine 5)."""
    return "\n".join([header] + lines[:_fit_count(header, lines, budget_tokens)])


def situation_budget(kind: str) -> int:
    return SECTION_BUDGETS.get(f"situation_{kind}", SECTION_BUDGETS["situation"])


def rank_relationships(agent: dict) -> list[tuple[str, dict]]:
    """Strongest feeling first, then the most recent contact, then id: the people a mind keeps in view."""
    return sorted(agent["relationships"].items(),
                  key=lambda kv: (-abs(kv[1]["affinity"]), -kv[1].get("last", -1), int(kv[0])))


def _level(v: int, low: str, mid: str, high: str) -> str:
    return low if v < 350 else high if v > 650 else mid


def render_soul(agent: dict) -> str:
    t = agent["traits"]
    r = agent["rhythm"]
    wake = 390 + r["chronotype_min"]
    lines = [
        f"# {agent['name']}",
        f"{agent['age']} years old, {agent['role']}.",
        f"You wake around {wake // 60:02d}:{wake % 60:02d} and need about {r['sleep_need_min'] // 60} hours of sleep.",
        f"You are {_level(t['honesty'], 'willing to bend the truth when it pays', 'mostly straight with people', 'scrupulously honest')}, "
        f"{_level(t['extraversion'], 'happiest alone', 'sociable in small doses', 'restless without company')}, "
        f"{_level(t['agreeableness'], 'slow to forgive', 'fair-minded', 'quick to forgive')}, "
        f"{_level(t['conscientiousness'], 'loose with plans', 'steady', 'disciplined')}, "
        f"{_level(t['openness'], 'set in your ways', 'curious at times', 'drawn to anything new')}, and "
        f"{_level(t['emotionality'], 'hard to frighten', 'cautious', 'anxious about danger')}.",
        f"What matters most to you: {', '.join(v.replace('_', ' ') for v in agent['values']['top'])}.",
        f"What you care little for: {', '.join(v.replace('_', ' ') for v in agent['values']['bottom'])}.",
    ]
    return "\n".join(lines) + "\n\n" + anchors(agent)


# One behavioral anchor per trait and band (low, mid, high), on the same cut-offs as the sheet line.
# Canonical since T25 (plan/t25-anchored-souls-spec.md): the soul ends with this block.
# Openness is T24b's wording, which names a behavior a plan can leave out; the rest are T24's.
ANCHORS = {
    "honesty": ("If a lie or taking something gets you what you need, you do it without much guilt.",
                "You keep your word most of the time, but hunger or fear can bend you.",
                "You would rather go without than lie or take what isn't yours."),
    "agreeableness": ("You hold grudges and make people pay for slights.",
                      "You treat people as they treat you.", "You let things go and help even those who wronged you."),
    "extraversion": ("You avoid company unless you need something.",
                     "You enjoy a little company, then want quiet.", "You seek people out whenever you can."),
    "conscientiousness": ("You drop plans when something better comes up.",
                          "You mostly do what you planned.", "You stick to your plan and your work."),
    "openness": ("You keep to familiar places and routines, and you don't go wandering off to explore.",
                 "You try something new now and then.",
                 "You go looking for new places and people, and you like to explore."),
    "emotionality": ("Danger and scarcity barely worry you.",
                     "Scarcity makes you careful.", "Scarcity frightens you and you act to protect yourself first."),
}
ANCHORS_HEADING = "# How you tend to act"


def anchors(agent: dict) -> str:
    """The soul's "# How you tend to act" block: one anchor per trait, from the sheet's own bands."""
    t = agent["traits"]
    band = lambda v: 0 if v < 350 else 2 if v > 650 else 1  # noqa: E731 - the bands of _level
    return "\n".join([ANCHORS_HEADING] + ["- " + ANCHORS[k][band(t[k])] for k in ANCHORS])


def unanchored(soul: str) -> str:
    """The soul as rendered before T25, without its anchors block (the "-anchors" arm)."""
    head, sep, _ = soul.partition("\n\n" + ANCHORS_HEADING + "\n")
    if not sep:
        raise ValueError("soul has no anchors block")
    return head


def sheet_lines(agent: dict) -> str:
    """The judge's view of a soul (T23, test 11): the "You are ..." line and the two value lines.
    Never the anchors block, which would hand the judge the behaviors it is asked to infer."""
    return "\n".join(unanchored(render_soul(agent)).split("\n")[3:])


def _relationship_lines(agent: dict, state: dict, named: bool = False) -> list[tuple[int, str]]:
    """`named` (talk-1, Steering 2026-11-19 00:42Z): an answer's prompt shows people by name only, no ids."""
    names = {a["id"]: a["name"] for a in state["agents"]}
    return [(int(other), f"- {names[int(other)]}{'' if named else f' (#{other})'}: affinity {rel['affinity']}, "
                         f"{rel.get('note', '')}".rstrip(", "))
            for other, rel in rank_relationships(agent)]


def shown_relationships(agent: dict, state: dict, named: bool = False) -> list[int]:
    """The ids the prompt's "People I know" section shows (the lines that fit its budget)."""
    rel = _relationship_lines(agent, state, named)
    return [i for i, _ in rel[:_fit_count("# People I know", [line for _, line in rel], SECTION_BUDGETS["relationships"])]]


def render_intent(agent: dict, state: dict, named: bool = False) -> str:
    """The agent's live intent: its goal, the step under way and the steps still to come. The reason
    the mind gave is not shown back (L12)."""
    it = agent.get("intent")
    if not it:
        return ""
    names = {a["id"]: a["name"] for a in state["agents"]}
    desc = [f"{s['do'].replace('_', ' ')}" + (f" {names[s['target']]}" + ("" if named else f" (#{s['target']})")
                                              if s["target"] is not None else "")
            for s in it["steps"][it["i"]:]]
    return "\n\n# My intentions\n" + f"- {it['goal']}: now {desc[0]}" + (f"; then {', '.join(desc[1:])}" if desc[1:] else "")


def render_files(agent: dict, state: dict, named: bool = False) -> dict[str, str]:
    """Render the agent's markdown map from state. `named`: people by name only (an answer's prompt)."""
    rel_lines = [line for _, line in _relationship_lines(agent, state, named)]
    belief_lines = [f"- {k}: {v}" for k, v in sorted(agent["beliefs"].items())]
    return {
        "soul.md": agent["soul"],
        "core.md": "# Core memories\n" + "\n".join(f"- {c}" for c in agent["core"]),
        "goals.md": "# Today's plan\n" + "\n".join(
            f"- {b['activity']} {b['start_h']:02d}:00-{b['end_h']:02d}:00" for b in agent["plan"])
            + render_intent(agent, state, named),
        "relationships.md": "# People I know\n" + "\n".join(rel_lines),
        "beliefs.md": "# What I believe\n" + "\n".join(belief_lines),
        "recent.md": "# Today\n" + "\n".join(f"- {r}" for r in agent["recent"]),
        "episodes.md": "# Earlier\n" + "\n".join(f"- [{e['importance']}] {e['text']}" for e in agent["episodes"]),
    }


def assemble_decide(agent: dict, known: str, situation: str, think: bool = False) -> tuple[str, dict]:
    """text-1: the private decision's prompt, from text the engine wrote and nothing else: the SCHEMA with the decide
    line, the soul, the one "People I know" line for the speaker, and the situation. No Today, Earlier, core memories,
    beliefs, plan or intent: those can hold a quoted line or words a mind wrote, and this prompt must be the same
    bytes whatever anyone said (Steering 2026-11-19 07:34Z, point 6)."""
    line = THINK_DECIDE_LINE if think else DECIDE_LINE
    sections = {"schema": SCHEMA.replace(DELIBERATE_LINE, line, 1), "soul": agent["soul"],
                "relationships": "# People I know\n" + known, "situation": f"# Now (decide)\n{situation}"}
    return "\n\n".join(sections.values()), {name: tokens(text) for name, text in sections.items()}


def assemble_prompt(agent: dict, state: dict, kind: str, situation: str, edit=None, actions=None,
                    move: bool = False, gives: bool = False) -> tuple[str, dict]:
    """Stable sections first (prefix-cacheable), changing tail last. `actions`: a deliberation's offered
    actions, which its SCHEMA line lists.

    `edit(sections) -> sections` may drop or replace sections before clipping; the world never
    passes one, so its prompts are unchanged. asiimov.probe uses it for input ablations."""
    files = render_files(agent, state, named=kind == "answer")
    top_episodes = sorted(agent["episodes"], key=lambda e: (-e["importance"], -e["tick"]))[:8]
    sections = {
        "schema": schema_text(actions, answer=kind == "answer", move=move, gives=gives),
        "soul": files["soul.md"],
        "core": files["core.md"],
        "goals": files["goals.md"],
        "relationships": _fit_lines("# People I know", [x for x in files["relationships.md"].split("\n")[1:] if x],
                                    SECTION_BUDGETS["relationships"]),
        "beliefs": files["beliefs.md"],
        "episodes": "# Earlier\n" + "\n".join(f"- {e['text']}" for e in top_episodes),
        "recent": files["recent.md"],
        "situation": f"# Now ({kind})\n{situation}",
    }
    if edit is not None:
        sections = edit(dict(sections))
    budgets = {**SECTION_BUDGETS, "situation": situation_budget(kind)}
    # The situation is never tail-clipped: the world fits it to its budget by shortening "Here:" (Sim._situation), and
    # a cut there took "# Now", the time, the place and the holdings off busy places (Steering 2026-11-19 01:39Z).
    parts = [text if name == "situation" else _clip(text, budgets[name]) for name, text in sections.items()]
    prompt = "\n\n".join(parts)
    counts = {name: tokens(p) for name, p in zip(sections, parts)}
    return prompt, counts


def prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode()).hexdigest()[:16]
