"""Liveness probes: prove every agent in the blueprint is live under the framework.

A probe runs the agent's full LangGraph from its selftest_input() and checks that it returns a
contract-valid result. "live" = ran and validated (ok / degraded / needs_approval); "down" =
error or not registered. Probes run on the configured model provider, or offline (no model
cost, verifies wiring, deterministic stages, connectors and contracts).
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from .config import get_settings
from .core import LLM, RunContext, all_agents, expected_agent_ids, get_agent, missing_agents, new_run_id

LIVE = {"ok", "degraded", "needs_approval"}


def probe_agent(agent_id: str, ctx: RunContext) -> dict[str, Any]:
    t0 = time.perf_counter()
    try:
        agent = get_agent(agent_id)
    except KeyError:
        return {"agent_id": agent_id, "name": agent_id, "category": "-", "kind": "-", "live": False, "status": "not_registered",
                "summary": "", "error": "not registered", "checks": {}, "degraded_reasons": [], "llm_calls": 0, "graph_nodes": [],
                "duration_ms": round((time.perf_counter() - t0) * 1000, 1)}
    try:
        result = agent.run(agent.selftest_input(), ctx, options={"auto_approve": False, "probe": True})
        status, error = result.status, result.error
        checks = result.check_counts()
        summary = result.summary
        reasons = (result.provenance or {}).get("degraded_reasons", [])
        metrics = result.metrics
        runner = (result.provenance or {}).get("runner")   # pack agents: home | model | twin
    except Exception as exc:  # pragma: no cover - run() never raises, belt and braces
        status, error, checks, summary, reasons, metrics, runner = "error", repr(exc), {}, "", [], {}, None
    return {"agent_id": agent_id, "name": agent.spec.name, "category": agent.spec.category.value, "kind": agent.spec.kind.value,
            "live": status in LIVE, "status": status, "summary": summary, "error": error, "checks": checks,
            "degraded_reasons": reasons, "runner": runner, "duration_ms": round((time.perf_counter() - t0) * 1000, 1),
            "llm_calls": metrics.get("llm_calls", 0), "graph_nodes": agent.graph_outline().get("nodes", [])}


def liveness(*, offline: bool | None = None, max_workers: int = 4, only: list[str] | None = None,
             tier: str | None = None, packs: bool = False) -> dict[str, Any]:
    """Probe every blueprint agent from its self-test (`packs=True`: the pack agents too, one model call each when
    live). `tier` forces one model tier for every step (e.g. 'fast' for a cheap live check)."""
    settings = get_settings()
    use_offline = offline if offline is not None else os.environ.get("CAF_HEALTH_OFFLINE", "1") == "1"
    llm = LLM(settings.llm, provider="offline" if use_offline else None)
    ctx = RunContext.create(new_run_id("health"), settings, llm=llm, options={"tier_override": tier} if tier else None)
    ids = only or ([a.spec.id for a in all_agents()] if packs else expected_agent_ids())
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        rows = list(pool.map(lambda a: probe_agent(a, ctx), ids))
    live = sum(1 for r in rows if r["live"])
    return {"checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "mode": "offline" if use_offline else ctx.llm.provider,
            "live": live, "total": len(rows), "all_live": live == len(rows) and not missing_agents(),
            "missing": missing_agents(), "duration_ms": round((time.perf_counter() - t0) * 1000, 1), "agents": rows}


def summary() -> dict[str, Any]:
    from .connectors import ConnectorHub
    from .orchestrator import coverage, validate_playbooks

    settings = get_settings()
    from .packs import list_packs

    everyone = all_agents()
    expected = set(expected_agent_ids())
    registered = [a.spec.id for a in everyone if a.spec.id in expected]   # the blueprint's agents; packs are counted beside them
    cov = coverage()
    return {
        "service": "coforge-agentic-framework",
        "version": __import__("caf").__version__,
        "llm": LLM(settings.llm).describe(),
        "agents_expected": len(expected_agent_ids()),
        "agents_registered": len(registered),
        "agents_total": len(everyone),
        "packs": [{"id": p["id"], "name": p["name"], "agents": p["agents"], "flows": p["flows"],
                   "home_connected": bool(p["home"] and p["home"]["connected"])} for p in list_packs()],
        "missing": missing_agents(),
        "playbook_problems": validate_playbooks(),
        "agents_without_playbook": [a for a in expected_agent_ids() if a not in cov],
        "connectors": [{"kind": c["kind"], "provider": c.get("provider"), "available": c.get("available")} for c in ConnectorHub(settings).describe()],
    }
