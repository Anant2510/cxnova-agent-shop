"""Orchestrator service: start, follow, approve, resume and continue runs. Backed by a LangGraph checkpointer.

Run state is durable by default (orchestrator.run_store: sqlite, see caf.orchestrator.persistence): every run's
checkpoints, status and approval decisions are in $CAF_DATA_DIR/orchestrator.db, so a restart of the service keeps
finished runs viewable, held actions approvable and runs cut off mid-way resumable (``continue_run``). In memory, the
orchestrator holds only the runs in flight: a run's context and checkpoints are dropped as soon as it settles.

Runs can execute on the calling thread (``wait=None``: the CLI, tests, scripts) or on a bounded worker pool
(``wait=<seconds>``: REST, MCP and A2A). With a pool, ``start`` waits up to ``wait`` seconds and returns the run as it
stands; the caller follows it with ``view`` (GET /runs/{id}).

Held actions (publish, send, deploy ...) can be decided while the run is still going: ``resume`` on a running run
executes approved actions straight away and records every decision in the approvals ledger; the approval gate at the
end of the run holds only what is still undecided.
"""

from __future__ import annotations

import logging
import threading
import time
import weakref
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from typing import Any, Callable

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from ..config import Settings, data_dir, get_settings
from ..core import PendingAction, RunContext, get_agent, new_run_id
from ..core.security import PolicyError, validate_run_id
from .graph import build_orchestrator_graph, release_run
from .persistence import SETTLED, MemoryRunStore, RunStore, SqliteSaver, age_s, shown_status
from .router import Plan, deterministic_route, validate_plan

log = logging.getLogger("caf.orchestrator")

OPEN = ("queued", "running")   # a run this process is executing (or about to)


class RunStateError(RuntimeError):
    """The run exists but is not in a state that allows the request (approve a finished run, continue a live one)."""


class RunApprovals:
    """What the approval gate and decisions taken mid-run share for one run: the ledger and a lock."""

    _locks: dict[str, threading.RLock] = {}
    _guard = threading.Lock()

    def __init__(self, store: Any, run_id: str):
        self.store = store
        self.run_id = run_id
        with self._guard:
            self.lock = self._locks.setdefault(run_id, threading.RLock())

    def decisions(self) -> dict[str, dict[str, Any]]:
        return self.store.decisions(self.run_id)

    def claim(self, key: str, decision: str, by: str = "") -> bool:
        """Take the decision on an action (atomic in the store: one taker, across processes)."""
        return self.store.claim(self.run_id, key, decision=decision, decided_by=by)

    def finish(self, key: str, status: str, result: dict[str, Any] | None = None) -> None:
        self.store.finish(self.run_id, key, status=status, result=result)

    @classmethod
    def forget(cls, run_id: str) -> None:
        with cls._guard:
            cls._locks.pop(run_id, None)


def _is_approve(decision: Any) -> bool:
    return str(decision or "").lower() in ("approve", "approved", "yes", "true")


class Orchestrator:
    """Drives every registered agent through one LangGraph parent graph.

    ``checkpointer``: pass your own LangGraph saver (for example Postgres for several instances); the run index and
    the approvals ledger are then kept in memory. Otherwise ``orchestrator.run_store`` decides: ``sqlite`` (default) or
    ``memory``.
    """

    def __init__(self, settings: Settings | None = None, checkpointer: Any = None, *, store_path: str | Path | None = None):
        self.settings = settings or get_settings()
        cfg = self.settings.orchestrator or {}
        mode = str(cfg.get("run_store") or "sqlite").strip().lower()
        self.durable = checkpointer is None and mode == "sqlite"
        if self.durable:
            self.store_path = Path(store_path) if store_path else data_dir(self.settings) / "orchestrator.db"
            self.checkpointer = SqliteSaver(self.store_path)
            self.store: Any = RunStore(self.store_path)
        else:
            self.store_path = None
            self.checkpointer = checkpointer or InMemorySaver()
            self.store = MemoryRunStore()
        self.graph = build_orchestrator_graph().compile(checkpointer=self.checkpointer, name="caf_orchestrator")
        # builds the context of a run this process does not hold (after a restart): the API sets the shop's, which
        # puts a customer's run back on the customer's model and policy
        self.context_factory: Callable[[str], RunContext] | None = None
        self._active: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._pool: ThreadPoolExecutor | None = None
        self.max_concurrent_runs = max(1, int(float(cfg.get("max_concurrent_runs") or 4)))
        # a run nobody has touched for this long, and that this process is not executing, is stalled
        self.stall_after_s = max(0.5, float(cfg.get("stall_after_s") or 60))
        self._heartbeat: threading.Thread | None = None
        if self.durable:
            self.prune()

    # ---------------------------------------------------------------------------------- runs
    def start(self, goal: str, *, inputs: dict[str, Any] | None = None, agent: str | None = None,
              playbook: str | None = None, plan: dict[str, Any] | None = None, auto_approve: bool | None = None,
              use_planner: bool = True, run_id: str | None = None, ctx: RunContext | None = None,
              wait: float | None = None, on_settled: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
        """Start a run. ``wait=None`` runs it on this thread and returns the settled run; a number runs it on the
        worker pool and returns after at most that many seconds (status queued/running when it is not done yet).
        ``on_settled(view)`` is called whenever the run settles (finished, or waiting for approval)."""
        ctx = ctx or RunContext.create(run_id or new_run_id("orch"), self.settings)
        if not (agent or playbook or plan) and not (use_planner and not ctx.llm.offline):
            # no model planner: only run what the deterministic router matched with some confidence
            decision = deterministic_route(goal)
            floor = float(self.settings.orchestrator.get("min_route_confidence", 0.05))
            if decision.confidence < floor:
                return self._clarify(goal, inputs, decision, floor)
        self._prevalidate(agent, playbook, plan)  # a bad request fails here, not later on a worker thread
        request = {"agent": agent, "playbook": playbook, "plan": plan, "use_planner": use_planner,
                   "auto_approve": bool(auto_approve if auto_approve is not None else self.settings.auto_approve),
                   "max_parallel": int(self.settings.orchestrator.get("max_parallel", 4)),
                   # the run's entitlement is part of its durable state, so a run continued after a restart keeps it
                   "allowed_agents": ctx.options.get("allowed_agents")}
        state = {"goal": goal, "inputs": dict(inputs or {}), "request": request, "results": {}, "warnings": [], "events": [],
                 "started": time.time()}
        self.store.record(ctx.run_id, "queued", customer_id=ctx.options.get("customer_id"), surface=ctx.options.get("surface"))
        ctx.tracer.event("orchestrator.start", goal=goal, request={k: v for k, v in request.items() if k not in ("plan", "allowed_agents")})
        return self._launch(ctx, state, wait, on_settled)

    def view(self, run_id: str) -> dict[str, Any]:
        """The run as it stands: status, plan, each step's result so far, held actions and the final summary.

        status: queued | running | awaiting_approval | completed | completed_with_errors | failed | stalled
        (stalled: the process running it stopped, for example a restart; ``continue_run`` picks it up)."""
        try:
            validate_run_id(run_id)
        except PolicyError as exc:
            raise KeyError(str(exc)) from exc
        with self._lock:
            entry = self._active.get(run_id)
        index = self.store.get(run_id)
        snap = self.graph.get_state(self._config(run_id))
        values = dict(snap.values or {})
        if not values:
            if entry is not None:
                return self._empty_view(run_id, entry["state"], entry.get("goal"))
            if index and index.get("status") == "failed":
                return self._empty_view(run_id, "failed", None, error=index.get("error"))
            raise KeyError(f"Unknown run '{run_id}'")
        interrupts = [i.value for i in (snap.interrupts or ())]
        settled_status = values.get("status") if values.get("status") in SETTLED else None
        elsewhere = False
        if interrupts:
            status = "awaiting_approval"
        elif settled_status:
            status = settled_status
        elif entry is not None:
            status = "running" if entry["state"] == "running" else "queued"
        elif index and index.get("error"):
            status = "failed"
        elif index and index.get("status") in OPEN and age_s(index.get("updated_at")) < self.stall_after_s:
            status = "running"   # another process is executing it (its heartbeat is fresh)
            elsewhere = True
        else:
            status = "stalled"
        decided = self.store.decisions(run_id)
        results = values.get("results") or {}
        steps = []
        for step in values.get("plan", []):
            r = results.get(step["id"])
            if r and decided:
                r = self._overlay(step["id"], r, decided)
            steps.append({**step, "status": (r or {}).get("status", "pending"), "result": r})
        if interrupts:
            pending = [{**p, "early": False} for p in interrupts[0]["pending"] if p["key"] not in decided]
        elif status in ("running", "queued", "stalled"):
            pending = self._early_pending(steps, decided)
        else:
            pending = []
        view = {"run_id": run_id, "status": status, "goal": values.get("goal"), "inputs": values.get("inputs"),
                "route": values.get("route"), "steps": steps, "warnings": values.get("warnings", []),
                "pending_approvals": pending, "final": values.get("final"), "events": values.get("events", []),
                "settled": status in SETTLED or status == "awaiting_approval",
                "resumable": entry is None and not interrupts and not settled_status and not elsewhere}
        if index and index.get("error"):
            view["error"] = index["error"]
        return view

    def resume(self, run_id: str, decisions: dict[str, str], *, ctx: RunContext | None = None, wait: float | None = None,
               on_settled: Callable[[dict[str, Any]], None] | None = None, actor: str = "") -> dict[str, Any]:
        """Decide held actions. A run waiting at the approval gate resumes with the decisions; a run still going gets
        them straight away (approved actions execute now, see ``decide``)."""
        view = self.view(run_id)  # KeyError for unknown or malformed run ids (never creates a run folder)
        if view["status"] in ("queued", "running", "stalled"):
            return self.decide(run_id, decisions, ctx=ctx, actor=actor)
        if view["status"] != "awaiting_approval":
            raise RunStateError(f"Run is '{view['status']}', not awaiting approval")
        ctx = self._ctx_for(run_id, ctx)
        self.store.record(run_id, "queued")
        return self._launch(ctx, Command(resume={"decisions": decisions, "by": actor}), wait, on_settled)

    def decide(self, run_id: str, decisions: dict[str, str], *, ctx: RunContext | None = None, actor: str = "") -> dict[str, Any]:
        """Decide actions held by steps that already finished, while the rest of the run goes on.

        Keys are '<step>/<action_id>', an action id, or '*' (every action held right now). Approved actions are
        executed at once through their connector; every decision lands in the approvals ledger, which the approval
        gate at the end of the run honours (nothing is executed twice). Actions not named stay held."""
        view = self.view(run_id)
        if not view["pending_approvals"]:
            return view
        with self._lock:
            entry = self._active.get(run_id)
        base = (entry or {}).get("ctx") or self._ctx_for(run_id, ctx)
        approvals = RunApprovals(self.store, run_id)
        by_key = {s["id"]: s for s in view["steps"]}
        with approvals.lock:
            recorded = approvals.decisions()
            for p in view["pending_approvals"]:
                key = p["key"]
                if key in recorded:
                    continue
                decision = decisions.get(key) or decisions.get(p.get("id", "")) or decisions.get("*")
                if not decision:
                    continue
                action = next((a for a in ((by_key.get(p["step"]) or {}).get("result") or {}).get("pending_actions", [])
                               if a.get("id") == p.get("id")), None)
                if action is None:
                    continue
                approve = _is_approve(decision)
                if not approvals.claim(key, "approve" if approve else "reject", by=actor or "early"):
                    continue  # decided meanwhile (the gate, or another process)
                if approve:
                    try:
                        done = get_agent(p["agent_id"]).execute_action(PendingAction.model_validate(action), base.for_agent(p["agent_id"]))
                        approvals.finish(key, done.status, done.result)
                    except Exception as exc:  # execute_action reports connector errors itself; this is anything else
                        approvals.finish(key, "failed", {"error": f"{type(exc).__name__}: {exc}"})
                base.tracer.event("orchestrator.decision", key=key, decision="approve" if _is_approve(decision) else "reject",
                                  early=True, by=actor)
        return self.view(run_id)

    def continue_run(self, run_id: str, *, ctx: RunContext | None = None, wait: float | None = None,
                     on_settled: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
        """Pick up a run that stopped mid-way (a restart, a crash) from its last checkpoint. Steps that finished are
        not run again."""
        view = self.view(run_id)
        if not view.get("resumable"):
            raise RunStateError(f"Run is '{view['status']}': only a stalled run can be continued")
        ctx = self._ctx_for(run_id, ctx)
        self.store.record(run_id, "queued", error="")
        ctx.tracer.event("orchestrator.continue", run_id=run_id)
        return self._launch(ctx, None, wait, on_settled)

    def runs(self) -> list[str]:
        """Runs this process is executing right now."""
        with self._lock:
            return list(self._active)

    def prune(self, keep_days: float | None = None) -> int:
        """Delete settled runs older than orchestrator.keep_runs_days from the store (their files under runs/ stay)."""
        days = float(keep_days if keep_days is not None else (self.settings.orchestrator or {}).get("keep_runs_days") or 30)
        if days <= 0:
            return 0
        removed = 0
        try:
            for run_id in self.store.expired(days):
                self.checkpointer.delete_thread(run_id)
                self.store.forget(run_id)
                removed += 1
        except Exception as exc:  # pruning is housekeeping: it never stops the service
            log.warning("run store pruning failed: %s", exc)
        return removed

    def shutdown(self, wait: bool = True) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=wait)
            self._pool = None

    # ---------------------------------------------------------------------------------- execution
    def _launch(self, ctx: RunContext, payload: Any, wait: float | None,
                on_settled: Callable[[dict[str, Any]], None] | None) -> dict[str, Any]:
        run_id = ctx.run_id
        ctx.approvals = RunApprovals(self.store, run_id)
        goal = payload.get("goal") if isinstance(payload, dict) else None
        with self._lock:
            if run_id in self._active:
                raise RunStateError(f"Run '{run_id}' is already in progress")
            self._active[run_id] = {"ctx": ctx, "state": "queued", "goal": goal, "future": None}
        self._start_heartbeat()
        if wait is None:
            self._execute(run_id, payload, on_settled)
            return self.view(run_id)
        try:
            future: Future = self._executor().submit(self._execute, run_id, payload, on_settled)
        except Exception:
            with self._lock:
                self._active.pop(run_id, None)
            raise
        with self._lock:
            if run_id in self._active:
                self._active[run_id]["future"] = future
        try:
            future.result(timeout=max(0.0, float(wait)))
        except FutureTimeout:
            pass
        return self.view(run_id)

    def _execute(self, run_id: str, payload: Any, on_settled: Callable[[dict[str, Any]], None] | None) -> None:
        with self._lock:
            entry = self._active[run_id]
            entry["state"] = "running"
        ctx: RunContext = entry["ctx"]
        self.store.record(run_id, "running")
        error = None
        try:
            self.graph.invoke(payload, self._config(run_id), context=ctx)
        except Exception as exc:  # a bug or an outage in the graph itself (agents never raise into it)
            error = f"{type(exc).__name__}: {exc}"
            log.exception("run %s failed", run_id)
            ctx.tracer.event("orchestrator.error", error=error)
        finally:
            release_run(run_id)
            view = self._settle(run_id, error)
        if on_settled is not None and view is not None:
            try:
                on_settled(view)
            except Exception:  # metering and notifications never break a run
                log.exception("on_settled callback failed for run %s", run_id)

    def _settle(self, run_id: str, error: str | None) -> dict[str, Any] | None:
        """Record where the run ended and drop it from memory (it stays in the store)."""
        with self._lock:
            self._active.pop(run_id, None)
        if error:  # recorded first, so the view reports the failure (and offers continue_run)
            self.store.record(run_id, "failed", error=error)
        try:
            view: dict[str, Any] | None = self.view(run_id)
        except KeyError:
            view = None
        if view is not None and not error:
            self.store.record(run_id, view["status"] if view["status"] != "stalled" else "failed")
        if view is None or view["status"] in SETTLED:
            RunApprovals.forget(run_id)
        evict = getattr(self.checkpointer, "evict", None)
        if callable(evict):
            evict(run_id)
        return view

    def _start_heartbeat(self) -> None:
        """Touch the index rows of the runs this process is executing, so other processes see them as running."""
        with self._lock:
            if self._heartbeat is not None and self._heartbeat.is_alive():
                return
            me = weakref.ref(self)
            every = max(0.2, min(10.0, self.stall_after_s / 4))

            def beat() -> None:
                while True:
                    time.sleep(every)
                    orch = me()
                    if orch is None:
                        return
                    with orch._lock:
                        ids = list(orch._active)
                    try:
                        orch.store.touch(ids)
                    except Exception as exc:  # housekeeping: never stops a run
                        log.debug("heartbeat failed: %s", exc)
                    del orch

            self._heartbeat = threading.Thread(target=beat, name="caf-run-heartbeat", daemon=True)
            self._heartbeat.start()

    def _executor(self) -> ThreadPoolExecutor:
        with self._lock:
            if self._pool is None:
                self._pool = ThreadPoolExecutor(max_workers=self.max_concurrent_runs, thread_name_prefix="caf-run")
            return self._pool

    def _ctx_for(self, run_id: str, ctx: RunContext | None) -> RunContext:
        if ctx is not None:
            return ctx
        if self.context_factory is not None:
            return self.context_factory(run_id)
        return RunContext.create(run_id, self.settings)

    def _prevalidate(self, agent: str | None, playbook: str | None, plan: dict[str, Any] | None) -> None:
        from .playbooks import load_playbooks

        if plan:
            validate_plan(Plan.model_validate(plan))
        elif playbook:
            books = load_playbooks()
            if playbook not in books:
                raise KeyError(f"Unknown playbook '{playbook}'. Known: {sorted(books)}")
        elif agent:
            get_agent(agent)

    # ---------------------------------------------------------------------------------- views
    @staticmethod
    def _empty_view(run_id: str, status: str, goal: str | None, error: str | None = None) -> dict[str, Any]:
        view = {"run_id": run_id, "status": status, "goal": goal, "inputs": None, "route": None, "steps": [], "warnings": [],
                "pending_approvals": [], "final": None, "events": [], "settled": status in SETTLED, "resumable": False}
        if error:
            view["error"] = error
        return view

    @staticmethod
    def _overlay(step_id: str, result: dict[str, Any], decided: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """Show decisions taken mid-run on the step's actions (the graph state catches up at the approval gate)."""
        actions = result.get("pending_actions") or []
        if not any(f"{step_id}/{a.get('id')}" in decided for a in actions):
            return result
        out = dict(result)
        merged = []
        for a in actions:
            rec = decided.get(f"{step_id}/{a.get('id')}")
            if rec and a.get("status") == "pending":
                a = {**a, "status": shown_status(rec["status"]), "result": rec.get("result") or a.get("result")}
            merged.append(a)
        out["pending_actions"] = merged
        if out.get("status") == "needs_approval" and not any(a.get("status") == "pending" for a in merged):
            out["status"] = "degraded" if (out.get("provenance") or {}).get("degraded_reasons") else "ok"
        return out

    @staticmethod
    def _early_pending(steps: list[dict[str, Any]], decided: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for s in steps:
            r = s.get("result") or {}
            for a in r.get("pending_actions", []) or []:
                key = f"{s['id']}/{a.get('id')}"
                if a.get("status") == "pending" and key not in decided:
                    out.append({"key": key, "id": a.get("id"), "step": s["id"], "agent_id": r.get("agent_id"), "kind": a.get("kind"),
                                "summary": a.get("summary", ""), "mandatory": bool(a.get("mandatory")), "early": True})
        return out

    @staticmethod
    def _clarify(goal: str, inputs: dict[str, Any] | None, decision: Any, floor: float) -> dict[str, Any]:
        """A goal too vague for the deterministic router: nothing runs; the caller gets the closest matches."""
        from .playbooks import load_playbooks

        books = load_playbooks()
        names = []
        for alt in decision.alternatives:
            if alt.get("playbook") in books:
                names.append(f"playbook '{alt['playbook']}' ({books[alt['playbook']].name})")
            elif alt.get("agent"):
                try:
                    names.append(f"agent '{alt['agent']}' ({get_agent(alt['agent']).spec.name})")
                except KeyError:
                    continue
        message = (f"The goal is too vague to route without a model planner (best match confidence {decision.confidence:.2f} "
                   f"< {floor:.2f}). Say what you want produced, or choose a playbook or agent explicitly.")
        return {"run_id": None, "status": "needs_clarification", "goal": goal, "inputs": dict(inputs or {}),
                "route": decision.model_dump(), "steps": [], "warnings": [message], "pending_approvals": [],
                "final": {"headline": "Goal needs clarification", "summary_markdown": message,
                          "next_steps": [f"Try {n}" for n in names[:4]], "steps": [], "method": "router"},
                "events": [], "suggestions": decision.alternatives, "settled": True, "resumable": False}

    # ---------------------------------------------------------------------------------- utils
    def _config(self, run_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": run_id}, "recursion_limit": 200}


def run_wait_s(settings: Any, requested: Any = None) -> float:
    """Seconds a service call waits for a run to settle before answering with the run so far."""
    try:
        default = float((settings.orchestrator or {}).get("run_wait_s") or 60)
    except (TypeError, ValueError):
        default = 60.0
    if requested is None or requested == "":
        return max(0.0, default)
    try:
        return min(max(0.0, float(requested)), 3600.0)
    except (TypeError, ValueError):
        return max(0.0, default)


def follow_hint(view: dict[str, Any], surface: str = "api") -> dict[str, Any]:
    """Tell the caller how to follow a run that has not settled yet (added to the view in place)."""
    rid = view.get("run_id")
    if not rid or view.get("settled"):
        return view
    if view.get("status") == "stalled":
        view["next"] = (f"The run stopped mid-way (the service restarted). Continue it with POST /runs/{rid}/continue; "
                        "finished steps are not run again.")
    elif surface == "mcp":
        view["next"] = (f"The run is still going. Call get_run with run_id '{rid}' every few seconds to follow it; held actions "
                        "can be approved with approve_actions as soon as they appear in pending_approvals.")
    else:
        view["poll"] = f"/runs/{rid}"
    return view


__all__ = ["Orchestrator", "RunApprovals", "RunStateError", "follow_hint", "run_wait_s"]
