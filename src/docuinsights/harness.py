"""The agent harness: the one place every agent runs.

An agent is a function. The harness is what stands around it, the same for all of them:

    before    reads the agent's contract; checks what it needs; hands it a model it is allowed to use (none at all when
              the contract says so, otherwise metered against its budget and the installation's rate limit), a
              memory handle that opens only the scopes the contract names, and its grounding: the nodes of the
              knowledge graph its contract names, for the case's use case
    during    times the run; retries a failed run as the contract allows
    after     checks every promise; checks the agent changed only the parts of the case it may write; records the run
              (contract kept or broken and why, spend, waits, memory reads and writes, fingerprints of what it read
              and wrote) on the case and in the provenance ledger

The harness never decides a case and never repairs an agent's output. A contract that is not kept is recorded and the
case is sent to a person.
"""
from __future__ import annotations

import copy
import time
from typing import Any, Callable

from . import contracts as C
from .ledger import digest
from .memory import MemoryDenied, MemoryHandle
from .model import ModelError
from .throttle import Budget, Throttled

RETRYABLE = (TimeoutError, ConnectionError)
SECTIONS = ("documents", "entities", "graph", "findings", "references", "recommendation", "privacy", "screening", "triage")
LASTING = ("documents",)   # kept on the case between assessments; the rest is rebuilt by every assessment


class GovernedModel:
    """The model as one agent may use it in one run: allowed or not, metered, rate limited."""

    def __init__(self, inner: Any, contract: dict[str, Any], limiter: Any = None, budget: Budget | None = None):
        self.inner, self.contract, self.limiter = inner, contract, limiter
        self.allowed = (contract["model"].get("use") or "none") != "none"
        self.budget = budget or Budget()
        self.violations: list[str] = []
        self.waited_ms = 0
        self.shed = 0

    @property
    def offline(self) -> bool:
        return bool(getattr(self.inner, "offline", True)) or not self.allowed

    @property
    def name(self) -> str:
        return getattr(self.inner, "name", "model")

    @property
    def calls(self) -> list[dict[str, Any]]:
        return getattr(self.inner, "calls", [])

    def describe(self) -> dict[str, Any]:
        return self.inner.describe() if hasattr(self.inner, "describe") else {"model": self.name}

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        if not self.allowed:
            self.violations.append(f"called a model ({kwargs.get('step') or method}) although its contract says it uses none")
            raise C.ContractBroken(f"the contract of '{self.contract['id']}' does not allow a model call")
        try:
            self.budget.check()
        except Throttled:
            self.shed += 1
            raise
        waited = 0.0
        if self.limiter is not None:
            try:
                waited = self.limiter.acquire()
            except Throttled:
                self.shed += 1
                raise
        self.waited_ms += round(waited * 1000)
        n0 = len(self.calls)
        try:
            return getattr(self.inner, method)(*args, **kwargs)
        finally:
            made = self.calls[n0:]
            tokens = sum(int(c.get("input_tokens") or 0) + int(c.get("output_tokens") or 0) for c in made)
            for c in made or [{}]:
                self.budget.spend(int(c.get("input_tokens") or 0) + int(c.get("output_tokens") or 0))
            if self.limiter is not None:
                self.limiter.release(tokens)

    def text(self, *args: Any, **kwargs: Any) -> str:
        return self._call("text", *args, **kwargs)

    def json(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("json", *args, **kwargs)


def fingerprints(case: dict[str, Any], w: dict[str, Any] | None) -> dict[str, str]:
    """A fingerprint of each part of the case an agent could change (the text of a document is represented by its length and hash)."""
    w = w or {}
    docs = [{"id": d["id"], "current": d.get("current"), "removed": d.get("removed"), "classification": d.get("classification"), "effective_type": d.get("effective_type"), "fields": d.get("fields"),
             "versions": [{"v": v.get("v"), "sha256": v.get("sha256"), "method": v.get("method"), "chars": len(v.get("text") or ""), "checks": v.get("checks")} for v in d.get("versions") or []]}
            for d in case.get("documents") or []]
    return {"documents": digest(docs), "entities": digest([w.get("entities"), w.get("unresolved"), w.get("recall")]), "graph": digest(w.get("graph")), "findings": digest(w.get("findings")),
            "references": digest(w.get("references")), "recommendation": digest(w.get("recommendation")),
            "privacy": digest(w.get("privacy")), "screening": digest(w.get("screening")), "triage": digest(w.get("triage"))}


class Harness:
    def __init__(self, tools: Any):
        self.tools = tools

    def run(self, agent: dict[str, Any], case: dict[str, Any], profile: Any, w: dict[str, Any] | None, fn: Callable[[Any], Any], *, extra: Callable[[Any], dict[str, Any]] | None = None) -> tuple[Any, dict[str, Any]]:
        """Run `fn(tools)` as `agent` under its contract. Returns (what fn returned, the trace). Raises when the agent may not start
        (ContractBroken) or fails and its contract says stop; the trace is attached to the exception as `.trace`."""
        contract = C.get(agent["id"])
        ctx: dict[str, Any] = {"agent": agent["id"], "done": list((w or {}).get("done") or [])}
        needs = [{"check": n, "ok": ok, "detail": detail} for n in contract["needs"] for ok, detail in [C.CHECKS[n](case, profile, ctx)]]
        budget = Budget(contract["model"].get("max_calls") or 0, contract["model"].get("max_tokens") or 0)
        model = GovernedModel(self.tools.model, contract, getattr(self.tools, "limiter", None), budget)
        memory = MemoryHandle(agent["id"], contract["memory"], (w if w is not None else {}).setdefault("memory", []), getattr(self.tools, "episodes", None))
        scoped = copy.copy(self.tools)
        scoped.model, scoped.memory = model, memory
        ground = getattr(self.tools, "ground", None)
        grounding = ground(agent, case, profile, contract["grounding"]) if ground and contract.get("grounding") else None
        scoped.grounding = grounding
        handed = grounding and {**{k: v for k, v in grounding.items() if k != "basis"}, **({"rules_with_basis": len(grounding["basis"])} if grounding.get("basis") else {})}   # the record keeps the count, the agent the content
        writers: dict[str, list[str]] = w.setdefault("writers", {}) if w is not None else {}
        kept: dict[str, list[str]] = case.setdefault("authors", {})   # the parts of a case that outlive an assessment (its documents): who wrote them, across assessments
        read = [{"section": sec, "from": [x for x in dict.fromkeys([*(kept.get(sec) or []), *(writers.get(sec) or [])]) if x != agent["id"]]}
                for sec in contract.get("reads") or []]   # whose work this agent builds on
        before = fingerprints(case, w)
        trace: dict[str, Any] = {"contract": {"id": contract["id"], "version": contract["version"], "kept": True, "needs": needs, "promises": [], "broken": [], "on_failure": contract["on_failure"]},
                                 "attempts": 0, "io": {"read": digest(before)}}
        t0 = time.perf_counter()

        def close(out: Any, error: str = "") -> dict[str, Any]:
            after = fingerprints(case, case.get("working") if w is not None else None)
            touched = [s for s in SECTIONS if before[s] != after[s]]
            broken = list(trace["contract"]["broken"])
            if w is not None:   # a stage of an assessment: it may change only what its contract names
                broken += [f"changed '{s}', which its contract does not let it write" for s in touched if s not in contract["writes"]]
            broken += model.violations + [f"memory refused: {r}" for r in memory.summary()["refused"]]
            ms = round((time.perf_counter() - t0) * 1000, 1)
            limit = float((contract["limits"] or {}).get("timeout_s") or 0)
            if limit and ms > limit * 1000:
                broken.append(f"ran {round(ms / 1000, 1)}s, longer than the {round(limit)}s its contract allows")
            if not error:
                ctx.update(extra(out) if extra else {})
                for p in contract["promises"]:
                    ok, detail = C.CHECKS[p["check"]](case, profile, ctx)
                    trace["contract"]["promises"].append({"check": p["check"], "text": p.get("text") or p["check"], "ok": ok, "detail": detail})
                    if not ok:
                        broken.append(f"promise not kept: {p.get('text') or p['check']} ({detail})")
            trace["contract"].update(kept=not broken, broken=broken)
            trace.update(duration_ms=ms, touched=touched, budget=budget.snapshot(), throttle={"waited_ms": model.waited_ms, "shed": model.shed}, memory=memory.summary(),
                         sections={"read": read, "wrote": touched}, grounding=handed)
            for sec in touched:   # the run's shared memory: who has written each part of the case so far
                if agent["id"] not in writers.setdefault(sec, []):
                    writers[sec].append(agent["id"])
                if sec in LASTING and agent["id"] not in kept.setdefault(sec, []):
                    kept[sec].append(agent["id"])
            trace["io"]["wrote"] = digest(after)
            if error:
                trace["error"] = error
            return trace

        unmet = [n for n in needs if not n["ok"]]
        if unmet:
            trace["contract"]["broken"] = [f"could not start: {n['detail']}" for n in unmet]
            exc = C.ContractBroken(f"{agent.get('name') or agent['id']} cannot start: " + "; ".join(n["detail"] for n in unmet))
            exc.trace = close(None, str(exc))   # type: ignore[attr-defined]
            raise exc
        retries = int((contract["limits"] or {}).get("retries") or 0)
        while True:
            trace["attempts"] += 1
            try:
                out = fn(scoped)
                break
            except RETRYABLE as exc:
                if trace["attempts"] > retries:
                    exc.trace = close(None, f"{type(exc).__name__}: {str(exc)[:240]}")   # type: ignore[attr-defined]
                    raise
            except (MemoryDenied, C.ContractBroken) as exc:
                trace["contract"]["broken"].append(str(exc))
                exc.trace = close(None, f"{type(exc).__name__}: {str(exc)[:240]}")   # type: ignore[attr-defined]
                raise
            except Exception as exc:
                exc.trace = close(None, f"{type(exc).__name__}: {str(exc)[:240]}")   # type: ignore[attr-defined]
                raise
        return out, close(out)


def record(tools: Any, case: dict[str, Any], agent: dict[str, Any], run_id: str, summary: str, trace: dict[str, Any], status: str) -> None:
    """One line in the provenance ledger for one agent run."""
    ledger = getattr(tools, "ledger", None)
    if ledger is None:
        return
    ledger.append("agent_run", agent["id"], f"{agent.get('name') or agent['id']}: {summary}"[:300], case=case.get("reference", ""),
                  data={"run": run_id, "status": status, "contract": {"version": trace["contract"]["version"], "kept": trace["contract"]["kept"], "broken": trace["contract"]["broken"]},
                        "read": trace["io"].get("read"), "wrote": trace["io"].get("wrote"), "model_calls": trace.get("budget", {}).get("calls", 0), "ms": trace.get("duration_ms")})


__all__ = ["Harness", "GovernedModel", "fingerprints", "record", "ModelError"]
