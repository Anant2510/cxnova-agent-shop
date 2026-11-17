"""The orchestrator graph (LangGraph).

    START -> route -> schedule --(dispatch: Send per ready step)--> run_step* -> schedule ...
                         \\--(nothing left)--> approve (interrupt for gated actions) -> synthesize -> END

* route     : explicit agent/playbook, model planner, or deterministic router -> validated plan
* schedule  : marks steps whose dependencies failed as skipped; dispatch fans out every ready step with
              langgraph.types.Send, and a per-run slot pool keeps at most orchestrator.max_parallel running
              (a queued step starts the moment any running step finishes, not when the whole batch does)
* run_step  : resolves bindings, runs the agent sub-graph with upstream outputs as context
* approve   : LangGraph interrupt() holding publish-type actions for a human decision
* synthesize: deterministic digest + optional model narrative of the whole run
"""

from __future__ import annotations

import json
import operator
import os
import sys
import threading
import time
from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Send, interrupt
from pydantic import BaseModel, Field

from ..core import PendingAction, RunContext, get_agent
from ..core.agent import merge_dicts
from .bindings import build_step_inputs
from .router import route_and_plan

TERMINAL = {"ok", "degraded", "needs_approval", "error", "skipped"}
SUCCESS = {"ok", "degraded", "needs_approval"}


class OrchestratorState(TypedDict, total=False):
    goal: str
    inputs: dict[str, Any]
    request: dict[str, Any]           # {agent, playbook, plan, auto_approve, use_planner}
    route: dict[str, Any]
    plan: list[dict[str, Any]]
    results: Annotated[dict[str, Any], merge_dicts]
    warnings: Annotated[list[str], operator.add]
    events: Annotated[list[dict[str, Any]], operator.add]
    decisions: dict[str, str]
    final: dict[str, Any]
    status: str
    started: float                    # epoch seconds when the run started


class RunSynthesis(BaseModel):
    headline: str = Field(description="one-line outcome of the run")
    summary_markdown: str = Field(description="5-10 lines: what each agent produced and what needs attention")
    next_steps: list[str] = Field(default_factory=list, max_length=6)


def _evt(kind: str, **data: Any) -> dict[str, Any]:
    return {"ts": time.time(), "kind": kind, **data}


_SLOTS: dict[str, threading.BoundedSemaphore] = {}
_SLOTS_LOCK = threading.Lock()


def _slots(run_id: str, size: int) -> threading.BoundedSemaphore:
    """Per-run pool of step slots (orchestrator.max_parallel)."""
    with _SLOTS_LOCK:
        pool = _SLOTS.get(run_id)
        if pool is None:
            pool = _SLOTS[run_id] = threading.BoundedSemaphore(max(1, size))
        return pool


def _progress(line: str) -> None:
    if os.environ.get("CAF_PROGRESS") == "1":
        print(line, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------------- nodes

def route_node(state: OrchestratorState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    ctx = runtime.context
    req = state.get("request") or {}
    decision, plan, warnings = route_and_plan(
        state.get("goal", ""), ctx, agent=req.get("agent"), playbook=req.get("playbook"), plan=req.get("plan"),
        inputs=state.get("inputs"), use_planner=req.get("use_planner", True))
    ctx.tracer.event("orchestrator.plan", mode=decision.mode, method=decision.method, steps=[s.agent for s in plan.steps])
    return {"route": decision.model_dump(), "plan": [s.model_dump() for s in plan.steps], "warnings": warnings,
            "status": "running", "events": [_evt("plan", route=decision.model_dump(), steps=[s.id for s in plan.steps])]}


def schedule_node(state: OrchestratorState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    results = state.get("results") or {}
    skipped: dict[str, Any] = {}
    changed = True
    status = dict((k, v.get("status")) for k, v in results.items())
    while changed:  # propagate failures down the DAG
        changed = False
        for step in state.get("plan", []):
            sid = step["id"]
            if sid in status:
                continue
            bad = [d for d in step.get("needs", []) if status.get(d) in ("error", "skipped")]
            if bad:
                skipped[sid] = {"agent_id": step["agent"], "status": "skipped", "summary": f"Skipped: dependency {bad} did not complete",
                                "output": {}, "checks": [], "artifacts": [], "pending_actions": [], "metrics": {}}
                status[sid] = "skipped"
                changed = True
    return {"results": skipped} if skipped else {}


def dispatch(state: OrchestratorState) -> list[Send] | Literal["approve"]:
    """Send every ready step; run_step's slot pool bounds how many run at once."""
    results = state.get("results") or {}
    ready = []
    for step in state.get("plan", []):
        if step["id"] in results:
            continue
        if all(results.get(d, {}).get("status") in SUCCESS for d in step.get("needs", [])):
            ready.append(step)
    if not ready:
        return "approve"
    return [Send("run_step", {"step": s, "goal": state.get("goal", ""), "inputs": state.get("inputs") or {},
                              "results": results, "request": state.get("request") or {}}) for s in ready]


def run_step(payload: dict[str, Any], runtime: Runtime[RunContext]) -> dict[str, Any]:
    ctx = runtime.context
    step = payload["step"]
    results = payload["results"]
    try:
        agent = get_agent(step["agent"])
    except KeyError as exc:
        res = {"agent_id": step["agent"], "status": "error", "error": str(exc), "summary": "Agent not registered",
               "output": {}, "checks": [], "artifacts": [], "pending_actions": [], "metrics": {}}
        return {"results": {step["id"]: res}, "events": [_evt("step.error", step=step["id"], error=str(exc))]}
    allowed = ctx.options.get("allowed_agents")  # a shop customer runs only the agents in its subscription
    if allowed is not None and agent.spec.id not in allowed:
        msg = f"'{agent.spec.id}' is not in this customer's subscription"
        res = {"agent_id": agent.spec.id, "status": "skipped", "error": msg, "summary": f"Skipped: {msg}",
               "output": {}, "checks": [], "artifacts": [], "pending_actions": [], "metrics": {}}
        ctx.tracer.event("orchestrator.step.skipped", step=step["id"], agent=agent.spec.id, reason="not subscribed")
        return {"results": {step["id"]: res}, "warnings": [f"Step {step['id']} skipped: {msg}"],
                "events": [_evt("step.skipped", step=step["id"], agent=agent.spec.id, reason=msg)]}
    try:
        step_inputs = build_step_inputs(agent, step.get("inputs", {}), goal=payload["goal"], inputs=payload["inputs"], results=results)
    except Exception as exc:  # binding errors become step errors, never crash the run
        res = {"agent_id": agent.spec.id, "status": "error", "error": f"binding failed: {exc}", "summary": "Input binding failed",
               "output": {}, "checks": [], "artifacts": [], "pending_actions": [], "metrics": {}}
        return {"results": {step["id"]: res}, "events": [_evt("step.error", step=step["id"], error=str(exc))]}
    # every finished step is upstream context; direct dependencies are ordered last (most relevant)
    upstream: dict[str, Any] = {}
    deps = step.get("needs", [])
    for sid, r in results.items():
        if sid not in deps and r.get("status") in SUCCESS:
            upstream[sid] = {"agent_id": r.get("agent_id"), "output": r.get("output", {})}
    for sid in deps:
        r = results.get(sid)
        if r and r.get("status") in SUCCESS:
            upstream[sid] = {"agent_id": r.get("agent_id"), "output": r.get("output", {})}
    auto = bool(payload["request"].get("auto_approve"))
    max_parallel = int(payload["request"].get("max_parallel") or 4)
    with _slots(ctx.run_id, max_parallel):
        ctx.tracer.event("orchestrator.step.start", step=step["id"], agent=agent.spec.id)
        t0 = time.perf_counter()
        result = agent.run(step_inputs, ctx, upstream=upstream, options={"auto_approve": auto, "step_id": step["id"]})
    data = result.model_dump(mode="json")
    data["step_inputs"] = step_inputs
    ctx.tracer.event("orchestrator.step.end", step=step["id"], agent=agent.spec.id, status=result.status)
    _progress(f"  [{result.status}] step {step['id']} ({agent.spec.id}) finished in {time.perf_counter() - t0:.1f}s")
    return {"results": {step["id"]: data},
            "events": [_evt("step.done", step=step["id"], agent=agent.spec.id, status=result.status, summary=result.summary)]}


def approve_node(state: OrchestratorState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    ctx = runtime.context
    results = {k: dict(v) for k, v in (state.get("results") or {}).items()}
    pending = []
    for sid, r in results.items():
        for a in r.get("pending_actions", []):
            if a.get("status") == "pending":
                pending.append({"step": sid, "agent_id": r.get("agent_id"), **a})
    if not pending:
        return {}
    auto = (state.get("request") or {}).get("auto_approve") or ctx.settings.auto_approve
    decisions = {f"{p['step']}/{p['id']}": "approve" for p in pending if auto and not p.get("mandatory")}
    held = [p for p in pending if f"{p['step']}/{p['id']}" not in decisions]
    if held:  # mandatory actions always wait for a human, even with auto-approve on
        answer = interrupt({"type": "approval", "pending": [
            {"key": f"{p['step']}/{p['id']}", "step": p["step"], "agent_id": p["agent_id"], "kind": p["kind"], "summary": p["summary"],
             "mandatory": bool(p.get("mandatory"))} for p in held]})
        given = (answer or {}).get("decisions", answer) if isinstance(answer, dict) else {}
        decisions.update(given or {})
    events = []
    for p in pending:
        key = f"{p['step']}/{p['id']}"
        decision = str(decisions.get(key) or decisions.get(p["id"]) or decisions.get("*") or "reject").lower()
        r = results[p["step"]]
        actions = [dict(a) for a in r.get("pending_actions", [])]
        for a in actions:
            if a["id"] != p["id"]:
                continue
            if decision in ("approve", "approved", "yes", "true"):
                agent = get_agent(p["agent_id"])
                done = agent.execute_action(PendingAction.model_validate(a), ctx.for_agent(p["agent_id"]))
                a.update(done.model_dump(mode="json"))
            else:
                a["status"] = "rejected"
            events.append(_evt("action", step=p["step"], action=a["kind"], status=a["status"]))
        r["pending_actions"] = actions
        if not any(a.get("status") == "pending" for a in actions) and r.get("status") == "needs_approval":
            r["status"] = "degraded" if (r.get("provenance") or {}).get("degraded_reasons") else "ok"
        results[p["step"]] = r
    return {"results": results, "decisions": decisions, "events": events}


def synthesize_node(state: OrchestratorState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    ctx = runtime.context
    results = state.get("results") or {}
    rows = []
    for step in state.get("plan", []):
        r = results.get(step["id"], {})
        counts = {"pass": 0, "warn": 0, "fail": 0}
        for c in r.get("checks", []):
            if c.get("status") in counts:
                counts[c["status"]] += 1
        rows.append({"step": step["id"], "agent": step["agent"], "status": r.get("status", "not_run"),
                     "summary": r.get("summary", ""), "checks": counts, "artifacts": len(r.get("artifacts", [])),
                     "error": r.get("error")})
    ok = sum(1 for x in rows if x["status"] in SUCCESS)
    failed = [x for x in rows if x["status"] == "error"]
    headline = f"{ok}/{len(rows)} steps completed" + (f", {len(failed)} failed" if failed else "")
    digest = "\n".join(f"- **{x['agent']}** ({x['status']}): {x['summary']}" for x in rows)
    final: dict[str, Any] = {"headline": headline, "summary_markdown": digest, "next_steps": [], "steps": rows, "method": "digest"}
    if not ctx.llm.offline and rows:
        try:
            orch = ctx.for_agent("orchestrator", tier=ctx.settings.orchestrator.get("synthesis_tier", "fast"))
            syn = orch.generate("synthesize", RunSynthesis, goal=state.get("goal", ""), steps=rows)
            final.update(headline=syn.headline, summary_markdown=syn.summary_markdown, next_steps=syn.next_steps, method="model")
        except Exception as exc:
            final["synthesis_error"] = f"{type(exc).__name__}: {exc}"
    status = "failed" if rows and not ok else ("completed_with_errors" if failed else "completed")
    started = state.get("started")
    if started:
        final["duration_s"] = round(time.time() - started, 1)
    try:
        final["report"] = _write_summary(ctx, state, final, status)
    except Exception as exc:  # the run result never depends on writing the summary file
        final["report_error"] = f"{type(exc).__name__}: {exc}"
    with _SLOTS_LOCK:
        _SLOTS.pop(ctx.run_id, None)
    ctx.tracer.event("orchestrator.end", status=status, headline=final["headline"], duration_s=final.get("duration_s"),
                     steps={x["step"]: x["status"] for x in rows})
    return {"final": final, "status": status, "events": [_evt("run.done", status=status, headline=final["headline"])]}


def _write_summary(ctx: RunContext, state: OrchestratorState, final: dict[str, Any], status: str) -> str:
    """runs/<run_id>/summary.md (+ summary.json): the outcome, each step and where its files are."""
    results = state.get("results") or {}
    lines = [f"# {final['headline']}", "",
             f"- Goal: {state.get('goal', '')}",
             f"- Run: `{ctx.run_id}` | status **{status}**"
             + (f" | {final['duration_s']:.0f} s" if final.get("duration_s") is not None else ""),
             f"- Route: {(state.get('route') or {}).get('mode', '')} via {(state.get('route') or {}).get('method', '')}"]
    if final.get("method") == "model":  # the digest repeats the step table below
        lines += ["", "## Summary", "", final.get("summary_markdown", "")]
    if final.get("next_steps"):
        lines += ["", "## Next steps", ""] + [f"- {s}" for s in final["next_steps"]]
    lines += ["", "## Steps", "", "| Step | Agent | Status | Summary |", "|---|---|---|---|"]
    for row in final.get("steps", []):
        summary = str(row.get("summary") or row.get("error") or "").replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {row['step']} | {row['agent']} | {row['status']} | {summary} |")
    files = []
    for step in state.get("plan", []):
        for a in (results.get(step["id"]) or {}).get("artifacts", []):
            if isinstance(a, dict) and a.get("path"):
                rel = a["path"].split("/", 1)[1] if a["path"].startswith(f"{ctx.run_id}/") else a["path"]
                files.append(f"- [{rel}]({rel})" + (f" - {a['description']}" if a.get("description") else ""))
    if files:
        lines += ["", "## Files", ""] + files
    ref = ctx.artifacts.write_text("summary.md", "\n".join(lines) + "\n", "text/markdown", "Run summary")
    ctx.artifacts.write_text("summary.json", json.dumps({"run_id": ctx.run_id, "status": status, "goal": state.get("goal"),
                                                          "route": state.get("route"), "final": final}, indent=2, default=str),
                             "application/json", "Run summary (JSON)")
    return ref.path


def build_orchestrator_graph() -> StateGraph:
    g = StateGraph(OrchestratorState, context_schema=RunContext)
    g.add_node("route", route_node)
    g.add_node("schedule", schedule_node)
    g.add_node("run_step", run_step)
    g.add_node("approve", approve_node)
    g.add_node("synthesize", synthesize_node)
    g.add_edge(START, "route")
    g.add_edge("route", "schedule")
    g.add_conditional_edges("schedule", dispatch, ["run_step", "approve"])
    g.add_edge("run_step", "schedule")
    g.add_edge("approve", "synthesize")
    g.add_edge("synthesize", END)
    return g
