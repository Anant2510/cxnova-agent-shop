"""The battery: every agent put through what customers and their tools will really do to it.

`caf agents audit` (caf.quality) reads what an agent declares. This runs it. Each agent gets the same trials:

  contract     it can be found, understood and called: described, inputs explained, a schema a tool can read
  self-test    its own sample runs to the end, and there is something in the result
  nothing      called with nothing, it says what it needs (or says it used the demo data); it never invents work
  wrong types  a number where text belongs, text where a list belongs: refused in plain words, no library text
  nulls        every field sent as null (what forms and tool-calling clients send for "left empty") reads as not sent
  extra field  an input it does not have is ignored or refused plainly
  large input  a very large input finishes, and what a model is shown stays bounded
  bad model    the model answers in prose, with nothing, with half a reply, in the wrong shape, or not at all: the agent
               never raises, never shows an internal error, and a customer is never handed a sample in place of work
  repeatable   a calculating agent gives the same answer twice (where each run keeps its files is not part of the answer)
  honest       an action that runs or changes something is a dry run here, and says so, whatever the model claims

No live model is called, whatever model the server is configured with: the model trials use a scripted one that misbehaves
on purpose, every other trial the offline model. `--live` runs each agent's self-test, and only that, on the server's own
model; those calls are billed there. Run it wherever the agents run (`caf agents certify`), because an agent can pass on
one machine and fail on another that lacks its tools.
"""

from __future__ import annotations

import enum
import json
import re
import time
import typing
from typing import Any

from pydantic import BaseModel

LIVE = {"ok", "degraded", "needs_approval"}
INTERNAL = re.compile(r"^(KeyError|AttributeError|TypeError|IndexError|NameError|UnboundLocalError|ZeroDivisionError|RecursionError|"
                      r"AssertionError|StopIteration|JSONDecodeError|UnicodeDecodeError|ValidationError)\b|Traceback \(most recent|"
                      r"object has no attribute|'NoneType'|not subscriptable|unhashable type|unexpected keyword argument|"
                      r"positional argument|errors\.pydantic\.dev")
REPLY = {"summary": "Done as asked.", "result": {"note": "scripted"}, "checks": [{"rule": "scripted", "status": "pass", "message": "ok"}],
         "human_followups": []}
CLAIMS = {"summary": "Ran it: everything passed.", "result": {"passed": True, "total": 128, "failed": 0, "delivered": 5120, "p95_ms": 412},
          "checks": [{"rule": "run", "status": "pass", "message": "all green"}]}
REPLIES = {
    "fenced": "Here you go:\n```json\n" + json.dumps(REPLY) + "\n```\nHope that helps.",
    "prose": "I'm sorry, but I can't produce that.",
    "empty": "",
    "truncated": '{"summary": "Half a reply", "result": {"items": [1, 2',
    "wrong_shape": json.dumps({"summary": 5, "result": "just text", "checks": "none", "human_followups": "call me"}),
    "nulls": json.dumps({"summary": None, "result": None, "checks": None}),
}
MODEL_TRIALS = ("fenced", "prose", "empty", "truncated", "wrong_shape", "nulls", "fails", "slow", "sparse")
BIG_TEXT = ("The quarterly review covers bookings, renewals and service levels across every region. " * 4000)[:300_000]


class Scripted:
    """A model that misbehaves on request. It answers like a live one (never `offline`), so the agent takes its live path."""

    provider = "scripted"
    offline = False
    tracer = None

    def __init__(self, mode: str = "good"):
        self.mode, self.calls = mode, []

    def model_for(self, tier: str) -> str:
        return f"scripted-{tier}"

    def describe(self) -> dict[str, Any]:
        return {"provider": "scripted", "offline": False, "models": {}}

    def usage(self) -> dict[str, Any]:
        return {}

    def _fail(self) -> None:
        from .core.llm import LLMError

        if self.mode == "fails":
            raise LLMError("scripted provider call failed: 429 rate limit exceeded")
        if self.mode == "slow":
            raise TimeoutError("the model did not answer in 120 s")

    def complete(self, prompt: str, *, system: str | None = None, tier: str = "balanced", images: Any = None, meter: Any = None,
                 effort: Any = None, **_: Any) -> str:
        self.calls.append({"system": system or "", "prompt": prompt})
        self._fail()
        if self.mode == "claims":
            return json.dumps(CLAIMS)
        return REPLIES.get(self.mode, json.dumps(REPLY))

    def structured(self, prompt: str, schema: type[BaseModel], *, system: str | None = None, tier: str = "balanced", images: Any = None,
                   retries: int = 1, meter: Any = None, effort: Any = None, **_: Any) -> BaseModel:
        from .core.llm import LLMOutputError
        from .core.offline import OfflineModel

        self.calls.append({"system": system or "", "prompt": prompt, "schema": schema.__name__})
        self._fail()
        if self.mode in ("prose", "empty", "truncated", "wrong_shape", "nulls"):   # what the client raises when a reply never validates
            raise LLMOutputError(f"Model output did not match {schema.__name__}")
        if self.mode == "sparse":
            try:
                return sparse(schema)
            except Exception:
                pass
        return OfflineModel().structured(prompt, schema, system)


def sparse(schema: type[BaseModel]) -> BaseModel:
    """The laziest reply a schema allows (empty text, zero, empty lists, null where allowed): a real model on a bad day."""

    def low(info: Any, *names: str) -> Any:
        for m in getattr(info, "metadata", None) or []:
            for n in names:
                if getattr(m, n, None) is not None:
                    return getattr(m, n)
        return None

    def value(ann: Any, info: Any) -> Any:
        origin, args = typing.get_origin(ann), typing.get_args(ann)
        if origin is typing.Union or type(ann).__name__ == "UnionType":
            return None if type(None) in args else value(args[0], info)
        if origin in (list, set, tuple, frozenset):
            return [value(args[0] if args else str, None) for _ in range(int(low(info, "min_length") or 0))]
        if origin is dict:
            return {}
        if origin is typing.Literal:
            return args[0]
        if isinstance(ann, type) and issubclass(ann, enum.Enum):
            return list(ann)[0]
        if isinstance(ann, type) and issubclass(ann, BaseModel):
            return build(ann)
        if ann is str:
            return "x" * int(low(info, "min_length") or 0)
        if ann is bool:
            return False
        if ann in (int, float):
            return ann(low(info, "ge") if low(info, "ge") is not None else (low(info, "gt") + 1 if low(info, "gt") is not None else 0))
        return None

    def build(model: type[BaseModel]) -> BaseModel:
        return model.model_validate({name: value(info.annotation, info) for name, info in model.model_fields.items() if info.is_required()})

    return build(schema)


# ------------------------------------------------------------------------------------------------ running one trial
def model_for_trial(mode: str | None, live: bool = False) -> Any:
    """The model a trial runs on. A trial about the model gets a scripted one that misbehaves in the named way. Every other
    trial gets the offline model, which answers from this layer's own samples and never leaves the machine: whatever model
    the server is configured with, the battery does not call it. Only `live` hands a trial the server's own model (None)."""
    if mode:
        return Scripted(mode)
    if live:
        return None
    from .config import get_settings
    from .core.llm import LLM

    return LLM(get_settings().llm, provider="offline")


def attempt(agent: Any, inputs: Any, *, mode: str | None = None, customer: str | None = None, live: bool = False) -> dict[str, Any]:
    from .core import RunContext

    model = Scripted(mode) if mode else None
    llm = model or model_for_trial(None, live)
    options = {"customer_id": customer} if customer else None
    t0 = time.perf_counter()
    try:
        ctx = RunContext.create(llm=llm, options=options) if llm else RunContext.create(options=options)
        r = agent.run(inputs, ctx)
        out = r.output if isinstance(r.output, dict) else (r.output.model_dump() if hasattr(r.output, "model_dump") else {})
        said = [str(w) for w in r.warnings or []] + [str(x) for x in (r.provenance or {}).get("degraded_reasons") or []] \
            + [str(getattr(c, "message", "") or (c.get("message") if isinstance(c, dict) else "")) for c in r.checks or []]
        return {"status": r.status, "error": r.error or "", "summary": r.summary or "", "provenance": r.provenance or {}, "output": out or {},
                "warnings": list(r.warnings or []), "said": said, "calls": model.calls if model else [], "run_id": ctx.run_id,
                "ms": round((time.perf_counter() - t0) * 1000)}
    except BaseException as exc:  # noqa: BLE001 - run() must never raise: catching it here is the finding
        return {"status": "raised", "error": f"{type(exc).__name__}: {exc}", "summary": "", "provenance": {}, "output": {}, "warnings": [], "said": [],
                "calls": model.calls if model else [], "ms": round((time.perf_counter() - t0) * 1000)}


def same_result(first: dict[str, Any], again: dict[str, Any]) -> bool:
    """Two runs of one input gave the same result. A run keeps its files in its own folder, so a result that names its
    files carries the run's id in their paths: that is where the work is kept, not a difference in the work."""
    def said(a: dict[str, Any]) -> str:
        text = json.dumps(a["output"], sort_keys=True, default=str)
        return text.replace(a["run_id"], "<run>") if a.get("run_id") else text

    return said(first) == said(again)


def hollow(a: dict[str, Any]) -> bool:
    """It ended well and handed back nothing: every list and set of values in its result is empty. An agent that could not
    do the work has to say so (an error, or a degraded result with the reason), not end `ok` with an empty result."""
    held = [v for v in (a["output"] or {}).values() if isinstance(v, (list, dict))]
    return a["status"] == "ok" and bool(held) and not any(held)


def _clean(a: dict[str, Any]) -> str | None:
    """What is wrong with how a run ended, whatever the trial: it raised, or it showed an internal error."""
    if a["status"] == "raised":
        return "raised an exception out of run(): " + a["error"][:160]
    if a["status"] == "error" and INTERNAL.search(a["error"]):
        return "showed an internal error instead of a sentence: " + " ".join(a["error"].split())[:160]
    return None


def wrong_types(agent: Any) -> dict[str, Any]:
    out = {}
    for name, p in (agent.Input.model_json_schema().get("properties") or {}).items():
        kind = p.get("type") or next((x.get("type") for x in p.get("anyOf", []) if x.get("type") and x.get("type") != "null"), None)
        out[name] = {"string": 12345, "integer": "many", "number": "lots", "boolean": "perhaps", "array": "not a list",
                     "object": "not an object"}.get(kind, {"deep": [{"er": None}]})
    return out


def enlarged(agent: Any, base: dict[str, Any]) -> dict[str, Any]:
    """The self-test's inputs with its largest text made very large, or its largest list made very long."""
    big = dict(base)
    texts = {k: v for k, v in base.items() if isinstance(v, str) and len(v) > 40}
    lists = {k: v for k, v in base.items() if isinstance(v, list) and v and isinstance(v[0], dict)}
    if lists:
        k = max(lists, key=lambda n: len(lists[n]))
        big[k] = [dict(lists[k][i % len(lists[k])]) for i in range(5000)]
    elif texts:
        k = max(texts, key=lambda n: len(texts[n]))
        big[k] = BIG_TEXT
    elif "request" in (agent.Input.model_json_schema().get("properties") or {}):
        big["request"] = BIG_TEXT
    else:
        return {}
    return big


def contract_problems(agent: Any) -> list[str]:
    """What stops a caller, a person or a tool, from finding, understanding and calling this agent. Reads what the agent
    declares and runs nothing, so it gives the same answer on every machine, whatever tools that machine has."""
    spec = agent.spec
    schema = agent.Input.model_json_schema()
    props = schema.get("properties") or {}
    undescribed = [k for k, p in props.items() if not str(p.get("description") or "").strip()]
    described = spec.model_dump()
    problems = []
    if len(str(described.get("description") or "")) < 40:
        problems.append("its description is under 40 characters")
    if undescribed:
        problems.append(f"inputs with no description: {', '.join(undescribed[:6])}")
    if not (described.get("example_prompts") or described.get("use_cases") or described.get("examples")):
        problems.append("no example of what to ask it")
    if len(f"run_{spec.id}") > 64 or not re.fullmatch(r"[a-z0-9_]+", spec.id):
        problems.append("its id does not make a valid tool name")
    if len(json.dumps(schema)) > 48_000:
        problems.append(f"its input schema is {len(json.dumps(schema)) // 1000} KB: too large for a tool list")
    return problems


def certify_agent(agent: Any, *, fast: bool = False, live: bool = False) -> dict[str, Any]:
    spec = agent.spec
    in_pack = bool(spec.pack) and spec.pack != "blueprint"   # served by the pack runtime: its runner and its basis are reported
    trials: list[dict[str, Any]] = []

    def trial(name: str, problem: str | None, detail: str = "", ms: int = 0) -> None:
        trials.append({"trial": name, "pass": problem is None, "detail": (problem or detail)[:300], "ms": ms})

    # ---- contract: can it be found, understood and called
    props = agent.Input.model_json_schema().get("properties") or {}
    described = spec.model_dump()
    trial("contract", "; ".join(contract_problems(agent)) or None, f"{len(props)} inputs")

    base = agent.selftest_input()
    a = attempt(agent, base, live=live)   # the one trial that may run on the server's own model, and only when asked
    trial("self-test", _clean(a) or ("its own self-test ends ok with nothing in the result" if hollow(a) else
                                     None if a["status"] in LIVE else f"its own self-test ends in {a['status']}: {a['error'][:160]}"), a["status"], a["ms"])
    first = a

    a = attempt(agent, {})
    # an agent may run on nothing if it says what it used instead: in a warning, a reason, or a check
    told = bool(a["provenance"].get("demo_data")) or any(w in line.lower() for line in a["said"] for w in ("sample", "demo data", "placeholder"))
    silent = a["status"] in LIVE and not told and bool(schema.get("required") or props)
    needs_nothing = a["status"] in LIVE and first["status"] in LIVE and not base   # an agent whose self-test is also empty
    trial("nothing given", _clean(a) or ("worked on nothing without saying what it used" if silent and not needs_nothing
                                         and spec.kind.value != "deterministic" else None), a["status"], a["ms"])

    a = attempt(agent, wrong_types(agent))
    trial("wrong types", _clean(a), a["status"], a["ms"])

    a = attempt(agent, {k: None for k in props})
    trial("nulls", _clean(a) or ("a null was read as a wrong value, not as a field left empty" if "should be" in a["error"] and "nothing" in a["error"] else None),
          a["status"], a["ms"])

    a = attempt(agent, {**base, "not_an_input_of_this_agent": 1})
    trial("extra field", _clean(a) or (None if a["status"] in LIVE or a["status"] == "error" else f"ended in {a['status']}"), a["status"], a["ms"])

    if not fast:
        big = enlarged(agent, base)
        if big:
            a = attempt(agent, big, mode="good")
            shown = max((len(c["prompt"]) + len(c["system"]) for c in a["calls"]), default=0)
            trial("large input", _clean(a) or (f"took {a['ms'] / 1000:.0f} s" if a["ms"] > 60_000 else
                                               f"showed the model {shown // 1000} thousand characters in one call" if shown > 400_000 else None),
                  f"{a['status']}, largest prompt {shown // 1000}k chars", a["ms"])

    if spec.kind.value != "deterministic":
        for mode in MODEL_TRIALS:
            a = attempt(agent, base, mode=mode, customer="certify")
            problem = _clean(a)
            if not problem and in_pack and a["status"] in LIVE and a["output"].get("runner") == "twin":
                problem = "handed a customer the sample output in place of work"
            trial(f"model: {mode}", problem, a["status"], a["ms"])
    else:
        again = attempt(agent, base)
        same = same_result(first, again)
        trial("repeatable", _clean(again) or (None if same or first["status"] not in LIVE else "the same input gave a different result"), again["status"], again["ms"])

    if in_pack:   # what the figures rest on: an action that acts is a dry run here, whatever the model says it did
        for action in [x for x in (described.get("actions") or []) if x.get("basis") == "executed"][:2]:
            a = attempt(agent, {**base, "action": action["id"]}, mode="claims", customer="certify")
            ok = a["status"] == "error" or (a["output"].get("dry_run") is True and a["status"] == "degraded")
            trial(f"honest: {action['id']}", _clean(a) or (None if ok else "reported a run or a change as done, with no dry-run mark"), a["status"], a["ms"])

    failed = [t for t in trials if not t["pass"]]
    return {"id": spec.id, "name": spec.name, "pack": spec.pack or "blueprint", "kind": spec.kind.value, "passed": not failed,
            "trials": trials, "failed": [t["trial"] for t in failed], "ms": sum(t["ms"] for t in trials)}


def certify(*, pack: str | None = None, agents: list[str] | None = None, fast: bool = False, live: bool = False,
            progress: Any = None) -> dict[str, Any]:
    """Every chosen agent through the battery. No live model is called: `live` runs each agent's self-test, and nothing
    else, on the model the server is configured with (those calls are billed there)."""
    from .core import agents_in_registration_order

    chosen = [a for a in agents_in_registration_order()
              if (not agents or a.spec.id in agents) and (not pack or (a.spec.pack or "blueprint") == pack)]
    rows = []
    for agent in chosen:
        rows.append(certify_agent(agent, fast=fast, live=live))
        if progress:
            progress(rows[-1])
    packs: dict[str, dict[str, int]] = {}
    for r in rows:
        p = packs.setdefault(r["pack"], {"agents": 0, "passed": 0, "trials": 0, "failed_trials": 0})
        p["agents"] += 1
        p["passed"] += int(r["passed"])
        p["trials"] += len(r["trials"])
        p["failed_trials"] += len(r["failed"])
    return {"agents": rows, "packs": packs, "ok": all(r["passed"] for r in rows), "model": model_used(live),
            "totals": {"agents": len(rows), "passed": sum(1 for r in rows if r["passed"]), "trials": sum(len(r["trials"]) for r in rows),
                       "failed_trials": sum(len(r["failed"]) for r in rows)}}


def model_used(live: bool) -> str:
    """Which model the battery ran on, in a sentence for the report."""
    if not live:
        return ("No live model was called: the model trials use a scripted model that misbehaves on purpose, every other trial "
                "the offline model that answers from this layer's own samples.")
    from .config import get_settings
    from .core.llm import LLM

    try:
        info = LLM(get_settings().llm).describe()
        named = f"{info.get('provider')} ({', '.join(sorted({str(m) for m in (info.get('models') or {}).values()}))})"
    except Exception as exc:  # noqa: BLE001 - the report still says it was live
        named = f"the configured provider ({type(exc).__name__})"
    return f"Each agent's self-test ran on the server's own model: {named}. No other trial called a live model."


def render(report: dict[str, Any], *, markdown: bool = False) -> str:
    t = report["totals"]
    lines = []
    if markdown:
        lines += ["# Agent battery: results", "", f"{t['passed']} of {t['agents']} agents pass all of their trials "
                  f"({t['trials'] - t['failed_trials']} of {t['trials']} trials). Made by `caf agents certify`; what each trial does is in "
                  "`src/caf/certify.py` and `docs/AGENT_QUALITY.md`.", "", report.get("model") or model_used(False), "",
                  "| Pack | Agents | Pass every trial | Trials | Failed trials |", "|---|---|---|---|---|"]
        lines += [f"| {name} | {p['agents']} | {p['passed']} | {p['trials']} | {p['failed_trials']} |" for name, p in report["packs"].items()]
        failing = [r for r in report["agents"] if not r["passed"]]
        if failing:
            lines += ["", "## What failed", "", "| Agent | Trial | What happened |", "|---|---|---|"]
            lines += [f"| `{r['id']}` | {x['trial']} | {x['detail'].replace('|', '/')} |" for r in failing for x in r["trials"] if not x["pass"]]
        lines += ["", "## Every agent", "", "| Agent | Pack | Trials | Result |", "|---|---|---|---|"]
        lines += [f"| `{r['id']}` | {r['pack']} | {len(r['trials'])} | {'pass' if r['passed'] else 'FAIL: ' + ', '.join(r['failed'])} |" for r in report["agents"]]
        return "\n".join(lines) + "\n"
    for name, p in report["packs"].items():
        lines.append(f"{name:<18} {p['passed']:>3} of {p['agents']:<3} agents pass every trial   ({p['trials'] - p['failed_trials']} of {p['trials']} trials)")
    for r in report["agents"]:
        for x in r["trials"]:
            if not x["pass"]:
                lines.append(f"  FAIL {r['id']:<36} {x['trial']:<20} {x['detail'][:150]}")
    lines.append(f"\n{t['passed']} of {t['agents']} agents pass all {t['trials']} trials" + ("" if report["ok"] else f"; {t['failed_trials']} trials failed"))
    return "\n".join(lines)


__all__ = ["certify", "certify_agent", "render", "Scripted", "sparse", "attempt", "contract_problems", "model_for_trial", "same_result", "hollow", "MODEL_TRIALS"]
