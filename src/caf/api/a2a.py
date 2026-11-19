"""A2A (Agent2Agent protocol) surface: agent cards for the orchestrator and every agent, plus a
JSON-RPC `message/send` endpoint so external orchestrators (Copilot Studio, Agentforce, Google
ADK, LangGraph, Adobe's agent orchestrator ...) can delegate goals to this framework."""

from __future__ import annotations

import uuid
from typing import Any

from ..core import all_agents, get_agent

PROTOCOL_VERSION = "0.3.0"


def agent_skill(agent: Any) -> dict[str, Any]:
    s = agent.spec
    return {"id": s.id, "name": s.name, "description": s.summary, "tags": [s.category.value, s.kind.value, *s.keywords[:6]],
            "examples": s.use_cases[:3], "inputModes": ["application/json", "text/plain"], "outputModes": ["application/json"]}


def flow_skill(flow: dict[str, Any]) -> dict[str, Any]:
    """A rented orchestration flow as an A2A skill: send a data part {"flow": "<id>", "inputs": {...}} to run it."""
    inputs = ", ".join(i["name"] for i in flow.get("inputs", [])) or "url, audience ..."
    return {"id": f"flow:{flow['id']}", "name": flow["name"], "description": flow.get("description") or flow["name"],
            "tags": ["flow", *flow.get("agents", [])[:8]],
            "examples": [f'data part: {{"flow": "{flow["id"]}", "inputs": {{{inputs}}}}}'],
            "inputModes": ["application/json", "text/plain"], "outputModes": ["application/json"]}


def orchestrator_card(base_url: str, allowed: set[str] | None = None, flows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    from ..branding import PRODUCT

    agents = [a for a in all_agents() if allowed is None or a.spec.id in allowed]
    flows = flows or []
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "name": f"{PRODUCT.name} Orchestrator",
        "description": f"Plans and runs multi-agent marketing workflows across {len(agents)} vendor-neutral agents "
                       f"(strategy, content, website optimisation, experimentation)"
                       + (f" and {len(flows)} ready-made orchestration flows." if flows else "."),
        "url": f"{base_url}/a2a",
        "preferredTransport": "JSONRPC",
        "version": __import__("caf").__version__,
        "provider": {"organization": PRODUCT.company, "url": "https://www.coforge.com"},
        "capabilities": {"streaming": False, "pushNotifications": False, "stateTransitionHistory": True},
        "defaultInputModes": ["text/plain", "application/json"],
        "defaultOutputModes": ["application/json", "text/markdown"],
        "skills": [agent_skill(a) for a in agents] + [flow_skill(f) for f in flows],
    }


def single_agent_card(agent_id: str, base_url: str) -> dict[str, Any]:
    agent = get_agent(agent_id)
    card = orchestrator_card(base_url)
    card.update(name=agent.spec.name, description=agent.spec.summary, url=f"{base_url}/a2a/agents/{agent_id}",
                skills=[agent_skill(agent)])
    return card


def _text_of(message: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    text, data = [], {}
    for part in message.get("parts", []):
        kind = part.get("kind") or part.get("type")
        if kind == "text":
            text.append(part.get("text", ""))
        elif kind == "data" and isinstance(part.get("data"), dict):
            data.update(part["data"])
    return "\n".join(t for t in text if t).strip(), data


def handle_jsonrpc(body: dict[str, Any], orchestrator: Any, agent_id: str | None = None, *, ctx: Any = None,
                   run_owner: Any = None, run_flow: Any = None, wait: float | None = None, on_settled: Any = None) -> dict[str, Any]:
    """`ctx`: a prepared RunContext (a shop customer's) or a callable making one; `run_owner(run_id) -> bool` limits
    tasks/get to that caller's runs; `run_flow(flow_id, inputs, goal) -> view` runs a rented flow named in a data part
    ({"flow": "<id>"} or {"playbook": "<id>"}). `wait`: seconds message/send waits for the run before answering with
    the task in state "working" (the client follows it with tasks/get); `on_settled(view)` meters the run when it settles."""
    rid = body.get("id")
    method = body.get("method")
    if method not in ("message/send", "tasks/get"):
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"Method not found: {method}"}}
    params = body.get("params") or {}
    if method == "tasks/get":
        try:
            if run_owner is not None and not run_owner(params.get("id")):
                raise KeyError(f"Unknown task '{params.get('id')}'")
            view = orchestrator.view(params.get("id"))
        except KeyError as exc:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32001, "message": str(exc)}}
        return {"jsonrpc": "2.0", "id": rid, "result": _task(view)}
    goal, data = _text_of(params.get("message") or {})
    if not goal and not data:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": "message needs a text or data part"}}
    flow_id = data.get("flow") or data.get("playbook")
    if flow_id and run_flow is not None and not agent_id:
        try:
            view = run_flow(flow_id, data.get("inputs", {k: v for k, v in data.items() if k not in ("flow", "playbook", "goal")}), goal or data.get("goal"))
        except Exception as exc:
            code = -32001 if getattr(exc, "status", 400) == 404 else -32000
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": str(exc)}}
    else:
        if callable(ctx):
            ctx = ctx()
        view = orchestrator.start(goal or data.get("goal", ""), inputs=data.get("inputs", data), agent=agent_id or data.get("agent"),
                                  playbook=flow_id, ctx=ctx, wait=wait, on_settled=on_settled)
    if view.get("run_id") is None:  # needs_clarification: nothing ran, answer with a message (no task)
        tips = "; ".join((view.get("final") or {}).get("next_steps", []))
        return {"jsonrpc": "2.0", "id": rid, "result": {"kind": "message", "role": "agent", "messageId": uuid.uuid4().hex,
                                                        "parts": [{"kind": "text", "text": " ".join(view.get("warnings") or []) + (f" {tips}." if tips else "")}]}}
    return {"jsonrpc": "2.0", "id": rid, "result": _task(view)}


def _task(view: dict[str, Any]) -> dict[str, Any]:
    state = {"completed": "completed", "completed_with_errors": "completed", "failed": "failed",
             "awaiting_approval": "input-required", "queued": "submitted", "running": "working",
             "stalled": "unknown"}.get(view["status"], "working")
    artifacts = []
    for step in view["steps"]:
        r = step.get("result") or {}
        if r:
            artifacts.append({"artifactId": f"{view['run_id']}-{step['id']}", "name": f"{step['id']} ({step['agent']})",
                              "parts": [{"kind": "data", "data": {"status": r.get("status"), "summary": r.get("summary"),
                                                                  "output": r.get("output")}}]})
    final = view.get("final") or {}
    msg = final.get("summary_markdown") or ("Awaiting approval: " + ", ".join(p["summary"] for p in view["pending_approvals"])
                                            if view["pending_approvals"] else view.get("next") or view["status"])
    return {"kind": "task", "id": view["run_id"], "contextId": view["run_id"],
            "status": {"state": state, "message": {"kind": "message", "role": "agent", "messageId": uuid.uuid4().hex,
                                                    "parts": [{"kind": "text", "text": msg}]}},
            "artifacts": artifacts}
