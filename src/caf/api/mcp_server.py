"""MCP server: every agent becomes an MCP tool (run_<agent_id>), plus orchestrate / approve / list tools.

Works with MCP SDK 2.x (MCPServer) and 1.x (FastMCP). Run over stdio with `caf mcp`, or mount the
streamable-HTTP app at /mcp inside the API service.

Tools are called by a model, so runs use the 'service' security policy (no private-network fetches, local
files only under security.file_roots), and the model cannot switch approval gates off: held actions need an
explicit approve_actions call, which MCP clients show to the user for confirmation.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from ..core import RunContext, all_agents, catalog, get_agent
from ..core.security import service_settings
from ..shop import ShopError
from ..shop import context as shop_context

try:  # MCP SDK 2.x
    from mcp.server.mcpserver import MCPServer as _Server
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # pragma: no cover - MCP SDK 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore
    from mcp.server.fastmcp.exceptions import ToolError  # type: ignore


def _tool_error(exc: Exception) -> ToolError:
    """A ShopError's message reaches the MCP client (the SDK hides other exceptions behind a generic text)."""
    return ToolError(str(exc))


def _shop(settings: Any):
    from ..shop import get_shop

    return get_shop(settings or service_settings())


def _make_runner(agent_id: str, input_model: type[BaseModel], settings: Any = None):
    def runner(inputs: input_model) -> dict[str, Any]:  # type: ignore[valid-type]
        agent = get_agent(agent_id)
        cust = shop_context.customer()  # set by the API's auth middleware when the caller used a customer key
        if cust is None:
            result = agent.run(inputs.model_dump(), RunContext.create(settings=settings or service_settings()))
            return result.model_dump(mode="json")
        shop = _shop(settings)
        try:
            shop.require_entitled(cust, agent_id)
            ctx = shop.context_for(cust, surface="mcp")
        except ShopError as exc:
            raise _tool_error(exc) from exc
        result = agent.run(inputs.model_dump(), ctx)
        shop.record_result(cust, ctx.run_id, result, surface="mcp", provider=ctx.llm.provider)
        data = result.model_dump(mode="json")
        data["run_id"] = ctx.run_id
        return data

    runner.__name__ = f"run_{agent_id}"
    runner.__annotations__ = {"inputs": input_model, "return": dict}  # resolvable without module globals
    return runner


class OrchestrateArgs(BaseModel):
    goal: str = Field(description="What you want done, in plain language")
    playbook: str | None = Field(default=None, description="Optional playbook id")
    agent: str | None = Field(default=None, description="Optional single agent id")
    inputs: dict[str, Any] = Field(default_factory=dict, description="Shared run inputs (url, audience, competitors ...)")


class ApproveArgs(BaseModel):
    run_id: str
    decisions: dict[str, str] = Field(description="{'<step>/<action_id>' | '*': 'approve' | 'reject'}")


def build_mcp(orchestrator: Any = None) -> Any:
    from ..orchestrator import Orchestrator

    orch = orchestrator or Orchestrator(service_settings())
    server = _Server("cxnova-agent-shop", instructions="Run the marketing agents in your subscription directly (run_<agent>), or orchestrate multi-agent workflows.")

    def list_agents() -> str:
        """List the agents available to you with id, category, kind and summary."""
        cust = shop_context.customer()
        allowed = _shop(orch.settings).entitled_agents(cust) if cust is not None else None
        return json.dumps([a for a in catalog() if allowed is None or a["id"] in allowed], indent=2)

    server.add_tool(list_agents, name="list_agents", description="List the agents available to you (id, category, kind, summary)")

    def orchestrate(args: OrchestrateArgs) -> dict[str, Any]:
        cust = shop_context.customer()
        if cust is None:
            return orch.start(args.goal, inputs=args.inputs, agent=args.agent, playbook=args.playbook, auto_approve=None)
        shop = _shop(orch.settings)
        try:
            if args.agent:
                shop.require_entitled(cust, args.agent)
            ctx = shop.context_for(cust, surface="mcp")
        except ShopError as exc:
            raise _tool_error(exc) from exc
        view = orch.start(args.goal, inputs=args.inputs, agent=args.agent, playbook=args.playbook, auto_approve=None, ctx=ctx)
        shop.record_view(cust, view, surface="mcp", provider=ctx.llm.provider)
        return view

    server.add_tool(orchestrate, name="orchestrate", description="Plan and run a multi-agent workflow for a goal; returns the run view")

    def approve(args: ApproveArgs) -> dict[str, Any]:
        cust = shop_context.customer()
        if cust is not None and not _shop(orch.settings).owns_run(cust, args.run_id):
            raise ToolError(f"Unknown run '{args.run_id}'")
        view = orch.resume(args.run_id, args.decisions)
        _shop(orch.settings).record_view(cust, view, surface="mcp")
        return view

    server.add_tool(approve, name="approve_actions", description="Approve or reject publish-type actions held at the approval gate")

    for agent in all_agents():
        server.add_tool(_make_runner(agent.spec.id, agent.Input, orch.settings), name=f"run_{agent.spec.id}",
                        description=f"{agent.spec.name}: {agent.spec.summary}"[:1000])
    return server


def main() -> None:  # pragma: no cover - entry point
    build_mcp().run("stdio")
