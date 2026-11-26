"""LLM mind: an open model behind an OpenAI-compatible server (vLLM).

The world treats this exactly like any other mind: it sends the assembled prompt and
records the raw output verbatim, so replay never needs the model again. Output shape is
enforced server-side with a JSON schema per request kind (vLLM structured outputs); the
world still validates every field and issues receipts, because a schema can't check facts.
"""
import http.client
import json
import threading
import time
import urllib.error
import urllib.request

from .memory import GIVES_ANSWER_LINE, prompt_hash
from .minds import MindReply, MindRequest
from .sim import ACTIVITIES as _ACTIVITIES, SAYS_NEXT, STEP_DOS
from .verbs import NO_TARGET, PERSON_ACTIONS

ACTIVITIES = list(_ACTIVITIES)

# Person actions and the ids each may name: family here for ask_kin (the map's place check), people here
# for ask_food, buy_from and steal, and anyone the prompt lists (family, here, People I know) for the rest.
# A deliberation's view "offer" (engine 10) cuts both lists to what can work now.
# verbs-1: both lists are read from the verb table (NO_TARGET: the menu's rows that name nobody; PERSON_ACTIONS:
# those that name a person, each with its row's `among`).

# No maxLength on strings: vLLM 0.11 structured outputs (xgrammar) decode ~15x slower with it
# (measured on Qwen3-4B, A6000: reflect 18.5 s vs 1.7 s). max_tokens bounds the reply and the
# world truncates every stored string, so the cap adds nothing.
SCHEMAS = {
    # say-3 (Steering 2026-11-16 10:57Z/11:04Z): the answer to a line said to you, the talk pilot's grammar minus
    # heard and answer (talk-1 adds them ahead of says). No maxLength: xgrammar decodes ~15x slower with one.
    "answer": {"type": "object",
               "properties": {"says": {"type": "string"}, "next": {"type": "string", "enum": ["stay", "carry_on", "leave"]}},
               "required": ["says", "next"], "additionalProperties": False},
    # text-1: the private decision, a yes or a no and nothing else. Its prompt never holds the speaker's words.
    "decide": {"type": "object", "properties": {"answer": {"type": "string", "enum": ["yes", "no"]}},
               "required": ["answer"], "additionalProperties": False},
    "plan": {
        "type": "object",
        # minItems: a whole waking day, not one short block (one-block plans were 700/700 at eccd95d).
        "properties": {"plan": {"type": "array", "minItems": 2, "maxItems": 6, "items": {
            "type": "object",
            "properties": {"activity": {"type": "string", "enum": ACTIVITIES},
                           "start_h": {"type": "integer", "minimum": 0, "maximum": 23},
                           "end_h": {"type": "integer", "minimum": 1, "maximum": 24}},
            "required": ["activity", "start_h", "end_h"], "additionalProperties": False}}},
        "required": ["plan"], "additionalProperties": False,
    },
    "reflect": {
        "type": "object",
        "properties": {"core": {"type": "array", "maxItems": 2, "items": {"type": "string"}},
                       "summary": {"type": "string"}},
        "required": ["core", "summary"], "additionalProperties": False,
    },
}


def _deliberate_branch(actions: list[str], target: dict, then: dict) -> dict:
    return {"type": "object",
            "properties": {"action": {"type": "string", "enum": actions}, "target": target,
                           "reason": {"type": "string"}, "goal": {"type": "string"}, "then": then},
            "required": ["action", "target", "reason", "goal", "then"], "additionalProperties": False}


def _ids(view: dict, key: str) -> list[int]:
    if key == "kin_here":
        return sorted(x["id"] for x in view["present"] if x["kin"])
    return sorted(view.get(key, [])) if key == "people" else sorted(x["id"] for x in view[key])


def schema_for(req: MindRequest) -> dict:
    """The reply schema for one request. A deliberation's target must be an id its prompt lists:
    family here for ask_kin, people here for steal, anyone listed for the other person actions. An action
    with nobody to aim at is left out. Intent steps may name anyone listed, or nobody; the world drops
    a person step without a target."""
    if req.kind == "answer" and req.view.get("answers"):  # talk-1: yes or no first, from what the world offers
        base = SCHEMAS["answer"]
        return {"type": "object", "properties": {"answer": {"type": "string", "enum": list(req.view["answers"])},
                                                 **base["properties"]},
                "required": ["answer"] + base["required"], "additionalProperties": False}
    if req.kind == "answer" and req.view.get("gives"):  # text-1: a typed line under a held yes; what was heard, first
        base = SCHEMAS["answer"]
        return {"type": "object", "properties": {"gives": {"type": "string", "enum": list(req.view["gives"])},
                                                 **base["properties"]},
                "required": ["gives"] + base["required"], "additionalProperties": False}
    if req.kind == "hear":  # text-1: the script arm's hearing call (Hearer)
        return {"type": "object", "properties": {"gives": {"type": "string", "enum": list(req.view["gives"])}},
                "required": ["gives"], "additionalProperties": False}
    if req.kind == "decide" and req.view.get("decide", {}).get("think"):  # think-1: the thought first, then the answer
        base = SCHEMAS["decide"]
        return {"type": "object", "properties": {"thought": {"type": "string"}, **base["properties"]},
                "required": ["thought"] + base["required"], "additionalProperties": False}
    if req.kind != "deliberate":
        return SCHEMAS[req.kind]
    people = _ids(req.view, "people")
    offer = req.view.get("offer", NO_TARGET + [x for x, _ in PERSON_ACTIONS])
    # Steps follow the offer too: a shop step buys like go_market, and buy_from needs the same coin.
    dos = [d for d in STEP_DOS if not (d == "shop" and "go_market" not in offer
                                       or d == "buy_from" and "buy_from" not in offer)]
    step_target = {"anyOf": [{"type": "null"}, {"type": "integer", "enum": people}]} if people else {"type": "null"}
    then = {"type": "array", "maxItems": 3, "items": {
        "type": "object", "properties": {"do": {"type": "string", "enum": dos}, "target": step_target},
        "required": ["do", "target"], "additionalProperties": False}}
    branches = [_deliberate_branch([x for x in NO_TARGET if x in offer], {"type": "null"}, then)]
    for action, key in PERSON_ACTIONS:
        ids = _ids(req.view, key)
        if ids and action in offer:
            branches.append(_deliberate_branch([action], {"type": "integer", "enum": ids}, then))
    return {"anyOf": branches}


LANE_KINDS = ("plan", "reflect")  # the calls the plan lane holds back; the player's reply never waits on them


class LLMMind:
    """Calls /v1/chat/completions. Thread-safe; keeps simple per-call stats for cost reports."""

    def __init__(self, base_url: str, model: str, temperature: float = 0.7, top_p: float = 0.9,
                 max_tokens: int = 200, timeout: float = 120.0, retries: int = 3, structured: bool = True,
                 api_key: str | None = None, answer_first: bool = False, plan_lane: int = 0):
        self.base_url = base_url.rstrip("/")
        # pt11 (Steering 2026-11-18 23:52Z): a spoken answer waited behind the morning's plan calls. With a vLLM
        # server started with --scheduling-policy priority, answers go first (priority 0, the rest 1). The request
        # text is unchanged and this is not in `sampling`: the world log and replay don't see it.
        self.answer_first = answer_first
        # The plan lane (Steering 2026-11-25 03:56Z, after aipt4's dawn): at most `plan_lane` plan and reflect calls
        # are at the server at once; answer, decide and hear calls never wait for it. aipt4's dawn tail was 26
        # plans decoding beside the player's reply. Timing only: replies apply in the engine's order, at the tick
        # they are due, so the world log is the same with or without it. 0: no lane.
        self._lane = threading.BoundedSemaphore(plan_lane) if plan_lane > 0 else None
        self.model = model
        self.temperature, self.top_p, self.max_tokens = temperature, top_p, max_tokens
        self.timeout, self.retries, self.structured = timeout, retries, structured
        self.api_key = api_key
        self.calls: list[dict] = []  # {kind, latency_s, prompt_tokens, completion_tokens}
        # text-1, after the first pilot (Steering 2026-11-19 12:36Z): hear(req) -> ("food" | "nothing" | None, how), a
        # Hearer on this same model. Set, an answer that offers a typed give is two calls: the hearing, with nothing of
        # the mind, and then the words, on the engine's prompt for what was heard (req.told). None: one call, `gives`
        # first in the answer's own reply.
        self.hear = None

    def body(self, req: MindRequest) -> dict:
        b = {
            "model": self.model,
            "messages": [{"role": "user", "content": req.prompt}],
            "temperature": self.temperature, "top_p": self.top_p, "max_tokens": self.max_tokens,
            # Per-request seed from the prompt. vLLM does not promise identical samples across different
            # batch compositions, so this is not a reproducibility guarantee; replay uses the recording.
            # asiimov.probe sets req.seed to re-ask one prompt with independent samples.
            "seed": req.seed if req.seed is not None else int(req.prompt_hash[:8], 16),
            # Qwen3 hybrid models: no <think> block; deliberation is the answer itself.
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if self.answer_first:
            b["priority"] = 0 if req.kind in ("answer", "decide", "hear") else 1  # text-1: the decision the next answer waits on
        if req.frequency_penalty_milli:  # only the T21 reflect retry; every first ask is unchanged
            b["frequency_penalty"] = req.frequency_penalty_milli / 1000
        if self.structured:
            b["response_format"] = {"type": "json_schema", "json_schema": {
                "name": req.kind, "schema": schema_for(req), "strict": True}}
        return b

    def post(self, path: str, body: dict) -> dict:
        data = json.dumps(body).encode()
        headers = {"Content-Type": "application/json", "User-Agent": "asiimov-mind/0.1"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        last = None
        for attempt in range(self.retries + 1):
            try:
                r = urllib.request.Request(self.base_url + path, data=data, headers=headers)
                with urllib.request.urlopen(r, timeout=self.timeout) as resp:
                    out = json.loads(resp.read())
                if not isinstance(out, dict) or not out.get("choices"):
                    raise ValueError(f"no choices in server reply: {str(out)[:200]}")
                return out
            # HTTPException: truncated body (IncompleteRead). ValueError: a 200 whose body isn't a completion.
            except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException, ValueError) as e:
                if isinstance(e, urllib.error.HTTPError) and 400 <= e.code < 500 and e.code != 429:
                    raise RuntimeError(f"LLM server rejected request: {e.code} {e.read()[:500]!r}") from e
                last = e
                time.sleep(2 ** attempt)
        raise RuntimeError(f"LLM server unreachable after {self.retries + 1} tries: {last}")

    def decide(self, req: MindRequest) -> MindReply:
        if req.kind == "answer" and self.hear is not None and req.view.get("gives") and req.told:
            return self._heard(req)
        return self._ask(req)

    def _heard(self, req: MindRequest) -> MindReply:
        """Hear, then answer: the reply is the words' own JSON with what was heard put first, as one output. Anything
        but a heard "food" is nothing, a `gives` in the words' own JSON is dropped, and words that aren't a JSON
        object go back as they came, so the world reads no `gives` from them."""
        gives, how = self.hear(req)
        gives = gives if gives in req.view["gives"] and gives in req.told else "nothing"
        prompt = req.told[gives]
        reply = self._ask(MindRequest("answer", req.agent_id, req.tick, prompt, prompt_hash(prompt),
                                      {k: v for k, v in req.view.items() if k != "gives"}, seed=req.seed))
        try:
            out = json.loads(reply.raw_output)
        except ValueError:
            out = None
        if not isinstance(out, dict):
            return MindReply(reply.raw_output, reply.model, {**reply.sampling, "hear": how, "told": gives},
                             reply.finish_reason)
        raw = json.dumps({"gives": gives, **{k: v for k, v in out.items() if k != "gives"}})  # only the hearing's
        return MindReply(raw, reply.model, {**reply.sampling, "hear": how, "told": gives}, reply.finish_reason)

    def _ask(self, req: MindRequest) -> MindReply:
        body = self.body(req)
        lane = self._lane if req.kind in LANE_KINDS else None
        tq = time.monotonic()
        if lane is not None:
            lane.acquire()
        try:
            t0 = time.monotonic()
            resp = self.post("/v1/chat/completions", body)
            dt = time.monotonic() - t0
        finally:
            if lane is not None:
                lane.release()
        choice = resp["choices"][0]
        text = choice["message"].get("content") or ""
        usage = resp.get("usage") or {}
        self.calls.append({"kind": req.kind, "latency_s": round(dt, 4),
                           "prompt_tokens": usage.get("prompt_tokens"),
                           "completion_tokens": usage.get("completion_tokens"),
                           "finish_reason": choice.get("finish_reason"),
                           **({"lane_wait_s": round(t0 - tq, 4)} if lane is not None else {})})
        sampling = {"temperature": self.temperature, "top_p": self.top_p, "max_tokens": self.max_tokens,
                    "seed": body["seed"], "structured": self.structured}
        if "frequency_penalty" in body:
            sampling["frequency_penalty"] = body["frequency_penalty"]
        return MindReply(text.strip(), resp.get("model", self.model), sampling, choice.get("finish_reason"))

    def stats(self) -> dict:
        if not self.calls:
            return {"calls": 0}
        lat = sorted(c["latency_s"] for c in self.calls)
        pt = sum(c["prompt_tokens"] or 0 for c in self.calls)
        ct = sum(c["completion_tokens"] or 0 for c in self.calls)
        return {"calls": len(lat), "latency_p50_s": lat[len(lat) // 2], "latency_p95_s": lat[int(len(lat) * .95)],
                "wall_s_in_model": round(sum(lat), 1), "prompt_tokens": pt, "completion_tokens": ct,
                "truncated": sum(1 for c in self.calls if c["finish_reason"] == "length")}


# text-1 (Steering 2026-11-19 07:40Z, ruling (e)): the pair's script arm hears a typed ask with the same model and the
# same stated decision as the minds arm, and nothing of a mind: no soul, no diary, no memory, no words to write.
HEAR_LINE = 'For kind=answer: {"gives":"food|nothing"}'
NOW_ANSWER = "\n\n# Now (answer)\n"


def hear_prompt(prompt: str) -> str:
    """An answer request's prompt cut to what hearing needs: the reply rules and the "# Now" section, where the
    engine states the held yes and quotes the line. Everything between (soul, core, plan, people, beliefs, Earlier,
    Today) is left out, and so is the sentence on `says` and `next`, which a hearing doesn't write. The line is
    printable text on one line (Sim._player), so it can't forge the section break this cuts at."""
    if GIVES_ANSWER_LINE not in prompt or NOW_ANSWER not in prompt or not prompt.endswith(SAYS_NEXT):
        raise ValueError("not an answer prompt that offers a give")
    head = prompt.split("\n\n", 1)[0].replace(GIVES_ANSWER_LINE, HEAR_LINE, 1)
    return head + prompt[prompt.rindex(NOW_ANSWER):-len(SAYS_NEXT)]


class Hearer:
    """ScriptMind's `hear`: asks a model whether the player's words ask for food. It never chooses: the world offers
    a give only where the stub's roll was already yes, and anything but a parsed "food" gives nothing."""

    def __init__(self, client: LLMMind):
        self.client = client
        self.model = client.model

    def __call__(self, req: MindRequest) -> tuple[str | None, dict]:
        try:
            prompt = hear_prompt(req.prompt)
            reply = self.client.decide(MindRequest("hear", req.agent_id, req.tick, prompt, prompt_hash(prompt),
                                                   {"gives": list(req.view["gives"])}))
            out = json.loads(reply.raw_output)
            gives = out.get("gives") if isinstance(out, dict) else None
            return (gives if isinstance(gives, str) else None), {"model": reply.model, **reply.sampling}
        except (RuntimeError, ValueError, RecursionError) as e:  # a dead server or a bad reply: nothing is given
            return None, {"model": self.model, "error": f"{type(e).__name__}: {str(e)[:120]}"}
