"""Orchestrator service: start, resume (approvals), inspect runs. Backed by a LangGraph checkpointer."""

from __future__ import annotations

import threading
import time
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from ..config import Settings, get_settings
from ..core import RunContext, new_run_id
from ..core.security import PolicyError, validate_run_id
from .graph import build_orchestrator_graph
from .router import deterministic_route


class Orchestrator:
    """Drives every registered agent through one LangGraph parent graph.

    The default checkpointer is in-memory; for durable, multi-instance deployments pass a
    LangGraph Postgres or SQLite saver (langgraph-checkpoint-postgres / -sqlite).
    """

    def __init__(self, settings: Settings | None = None, checkpointer: Any = None):
        self.settings = settings or get_settings()
        self.checkpointer = checkpointer or InMemorySaver()
        self.graph = build_orchestrator_graph().compile(checkpointer=self.checkpointer, name="caf_orchestrator")
        self._contexts: dict[str, RunContext] = {}
        self._lock = threading.Lock()

    # ---------------------------------------------------------------------------------- runs
    def start(self, goal: str, *, inputs: dict[str, Any] | None = None, agent: str | None = None,
              playbook: str | None = None, plan: dict[str, Any] | None = None, auto_approve: bool | None = None,
              use_planner: bool = True, run_id: str | None = None, ctx: RunContext | None = None) -> dict[str, Any]:
        ctx = ctx or RunContext.create(run_id or new_run_id("orch"), self.settings)
        if not (agent or playbook or plan) and not (use_planner and not ctx.llm.offline):
            # no model planner: only run what the deterministic router matched with some confidence
            decision = deterministic_route(goal)
            floor = float(self.settings.orchestrator.get("min_route_confidence", 0.05))
            if decision.confidence < floor:
                return self._clarify(goal, inputs, decision, floor)
        with self._lock:
            self._contexts[ctx.run_id] = ctx
        request = {"agent": agent, "playbook": playbook, "plan": plan, "use_planner": use_planner,
                   "auto_approve": bool(auto_approve if auto_approve is not None else self.settings.auto_approve),
                   "max_parallel": int(self.settings.orchestrator.get("max_parallel", 4))}
        state = {"goal": goal, "inputs": dict(inputs or {}), "request": request, "results": {}, "warnings": [], "events": [],
                 "started": time.time()}
        ctx.tracer.event("orchestrator.start", goal=goal, request={k: v for k, v in request.items() if k != "plan"})
        self.graph.invoke(state, self._config(ctx.run_id), context=ctx)
        return self.view(ctx.run_id)

    def resume(self, run_id: str, decisions: dict[str, str]) -> dict[str, Any]:
        self.view(run_id)  # KeyError for unknown or malformed run ids (never creates a run folder)
        ctx = self._ctx(run_id)
        self.graph.invoke(Command(resume={"decisions": decisions}), self._config(run_id), context=ctx)
        return self.view(run_id)

    def view(self, run_id: str) -> dict[str, Any]:
        try:
            validate_run_id(run_id)
        except PolicyError as exc:
            raise KeyError(str(exc)) from exc
        snap = self.graph.get_state(self._config(run_id))
        values = dict(snap.values or {})
        if not values:
            raise KeyError(f"Unknown run '{run_id}'")
        interrupts = [i.value for i in (snap.interrupts or ())]
        status = "awaiting_approval" if interrupts else values.get("status", "running")
        results = values.get("results") or {}
        steps = []
        for step in values.get("plan", []):
            r = results.get(step["id"])
            steps.append({**step, "status": (r or {}).get("status", "pending"), "result": r})
        return {"run_id": run_id, "status": status, "goal": values.get("goal"), "inputs": values.get("inputs"),
                "route": values.get("route"), "steps": steps, "warnings": values.get("warnings", []),
                "pending_approvals": interrupts[0]["pending"] if interrupts else [],
                "final": values.get("final"), "events": values.get("events", [])}

    def runs(self) -> list[str]:
        return list(self._contexts)

    @staticmethod
    def _clarify(goal: str, inputs: dict[str, Any] | None, decision: Any, floor: float) -> dict[str, Any]:
        """A goal too vague for the deterministic router: nothing runs; the caller gets the closest matches."""
        from ..core import get_agent
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
                "events": [], "suggestions": decision.alternatives}

    # ---------------------------------------------------------------------------------- utils
    def _config(self, run_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": run_id}, "recursion_limit": 200}

    def _ctx(self, run_id: str) -> RunContext:
        with self._lock:
            if run_id not in self._contexts:
                self._contexts[run_id] = RunContext.create(run_id, self.settings)
            return self._contexts[run_id]
