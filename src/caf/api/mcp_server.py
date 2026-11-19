"""MCP server: every agent becomes an MCP tool (run_<agent_id>), every published flow a tool (run_flow_<flow_id>),
plus list_agents / list_flows / run_flow / orchestrate / approve_actions / get_run.

Runs execute on the orchestrator's worker pool: a run tool waits up to orchestrator.run_wait_s and then answers with the
run so far (status running, plus a `next` hint); the client follows it with get_run and can approve held actions with
approve_actions as soon as they appear, while the rest of the run goes on.

Works with MCP SDK 2.x (MCPServer) and 1.x (FastMCP). Run over stdio with `caf mcp`, or mount the
streamable-HTTP app at /mcp inside the API service.

Tools are called by a model, so runs use the 'service' security policy (no private-network fetches, local
files only under security.file_roots), and the model cannot switch approval gates off: held actions need an
explicit approve_actions call, which MCP clients show to the user for confirmation.

A customer key (set on the request by the API's auth middleware) narrows the server to that customer: tools/list
shows only the rented agents and flows, and every call is checked, metered and run under the customer's policy.
Flow tools follow the shop: publishing a flow adds its tool, unpublishing or deleting removes it, without a restart.
"""

from __future__ import annotations

import json
import re
import threading
from typing import Any

from pydantic import BaseModel, Field, create_model

from ..core import RunContext, all_agents, catalog, get_agent
from ..core.security import service_settings
from ..orchestrator.service import RunStateError, follow_hint, run_wait_s
from ..shop import ShopError
from ..shop import context as shop_context

try:  # MCP SDK 2.x
    from mcp.server.mcpserver import MCPServer as _Server
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # pragma: no cover - MCP SDK 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore
    from mcp.server.fastmcp.exceptions import ToolError  # type: ignore

GENERIC_TOOLS = ("list_agents", "list_flows", "run_flow", "orchestrate", "approve_actions", "get_run")


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


def flow_args_model(flow: Any) -> type[BaseModel]:
    """One pydantic model per flow: a field per declared input (required ones required) plus goal."""
    fields: dict[str, Any] = {}
    for inp in flow.inputs:
        desc = inp.description or inp.name.replace("_", " ")
        if inp.example:
            desc += f" (e.g. {inp.example})"
        fields[inp.name] = (Any, Field(description=desc)) if inp.required else (Any | None, Field(default=None, description=desc))
    fields["goal"] = (str | None, Field(default=None, description="Optional goal text that steers the agents; the flow's name when empty"))
    if not flow.inputs:
        fields["inputs"] = (dict[str, Any], Field(default_factory=dict, description="Shared inputs (url, audience, competitors ...)"))
    name = "Flow_" + re.sub(r"[^A-Za-z0-9]", "_", flow.id).title() + "_Args"
    return create_model(name, **fields)  # type: ignore[call-overload]


def _make_flow_runner(flow_id: str, args_model: type[BaseModel], orch: Any):
    def runner(args: args_model) -> dict[str, Any]:  # type: ignore[valid-type]
        data = args.model_dump()
        goal = data.pop("goal", None)
        inputs = {**(data.pop("inputs", None) or {}), **{k: v for k, v in data.items() if v is not None}}
        try:
            view = _shop(orch.settings).run_flow(shop_context.customer(), flow_id, inputs, orchestrator=orch, goal=goal, surface="mcp",
                                                 wait=run_wait_s(orch.settings))
        except ShopError as exc:
            raise _tool_error(exc) from exc
        return follow_hint(view, "mcp")

    runner.__name__ = f"run_flow_{flow_id}"
    runner.__annotations__ = {"args": args_model, "return": dict}
    return runner


class OrchestrateArgs(BaseModel):
    goal: str = Field(description="What you want done, in plain language")
    playbook: str | None = Field(default=None, description="Optional flow (playbook) id; see list_flows")
    agent: str | None = Field(default=None, description="Optional single agent id")
    inputs: dict[str, Any] = Field(default_factory=dict, description="Shared run inputs (url, audience, competitors ...)")


class RunFlowArgs(BaseModel):
    flow_id: str = Field(description="Flow id from list_flows")
    inputs: dict[str, Any] = Field(default_factory=dict, description="The flow's shared inputs (list_flows shows which it expects)")
    goal: str | None = Field(default=None, description="Optional goal text; the flow's name when empty")


class ApproveArgs(BaseModel):
    run_id: str
    decisions: dict[str, str] = Field(description="{'<step>/<action_id>' | '*': 'approve' | 'reject'}. Works while the run is still "
                                                  "going (approved actions execute at once) and when it waits at the approval gate")


class GetRunArgs(BaseModel):
    run_id: str = Field(description="The run_id a run_flow / orchestrate / run_flow_<id> call returned")


class ShopMCPServer(_Server):  # type: ignore[misc,valid-type]
    """MCPServer whose tools/list is the caller's entitlement (a customer sees its rented agents and flows only)."""

    shop_settings: Any = None

    async def list_tools(self) -> list[Any]:  # type: ignore[override]
        tools = await super().list_tools()
        cust = shop_context.customer()
        if cust is None:
            return tools
        shop = _shop(self.shop_settings)
        agents = {f"run_{a}" for a in shop.entitled_agents(cust)}
        flows = {f"run_flow_{f}" for f in shop.entitled_flows(cust)}
        return [t for t in tools if t.name in GENERIC_TOOLS or t.name in agents or t.name in flows]


def build_mcp(orchestrator: Any = None) -> Any:
    from ..orchestrator import Orchestrator

    orch = orchestrator or Orchestrator(service_settings())
    server = ShopMCPServer("cxnova-orchestration", instructions="Run the agents in your subscription directly (run_<agent>), run a rented "
                                                            "orchestration flow (run_flow_<flow>), or orchestrate a goal across them. A long run "
                                                            "answers while it is still going: follow it with get_run.")
    server.shop_settings = orch.settings
    shop = _shop(orch.settings)

    def list_agents() -> str:
        """List the agents available to you with id, category, kind and summary."""
        cust = shop_context.customer()
        allowed = shop.entitled_agents(cust) if cust is not None else None
        return json.dumps([a for a in catalog() if allowed is None or a["id"] in allowed], indent=2)

    server.add_tool(list_agents, name="list_agents", description="List the agents available to you (id, category, kind, summary)")

    def list_flows() -> str:
        """List the orchestration flows available to you: id, name, description, expected inputs, steps and stages."""
        cust = shop_context.customer()
        allowed = set(shop.entitled_flows(cust))
        rows = []
        for f in shop.flows():
            if f.id in allowed:
                d = f.listing()
                rows.append({k: d[k] for k in ("id", "name", "description", "inputs", "agents", "stages", "tool")})
        return json.dumps(rows, indent=2)

    server.add_tool(list_flows, name="list_flows", description="List the orchestration flows available to you (id, inputs, steps, tool name)")

    def run_flow(args: RunFlowArgs) -> dict[str, Any]:
        try:
            view = shop.run_flow(shop_context.customer(), args.flow_id, args.inputs, orchestrator=orch, goal=args.goal, surface="mcp",
                                 wait=run_wait_s(orch.settings))
        except ShopError as exc:
            raise _tool_error(exc) from exc
        return follow_hint(view, "mcp")

    server.add_tool(run_flow, name="run_flow", description="Run an orchestration flow by id with its shared inputs; returns the run view")

    def orchestrate(args: OrchestrateArgs) -> dict[str, Any]:
        cust = shop_context.customer()
        wait = run_wait_s(orch.settings)
        try:
            if cust is None:
                return follow_hint(orch.start(args.goal, inputs=args.inputs, agent=args.agent, playbook=args.playbook, auto_approve=None,
                                              wait=wait), "mcp")
            if args.playbook:  # a rented flow runs as a whole, its agents included
                return follow_hint(shop.run_flow(cust, args.playbook, args.inputs, orchestrator=orch, goal=args.goal, surface="mcp",
                                                 wait=wait), "mcp")
            if args.agent:
                shop.require_entitled(cust, args.agent)
            ctx = shop.context_for(cust, surface="mcp")
            view = orch.start(args.goal, inputs=args.inputs, agent=args.agent, playbook=None, auto_approve=None, ctx=ctx, wait=wait,
                              on_settled=shop.meter(cust, surface="mcp", provider=ctx.llm.provider))
        except ShopError as exc:
            raise _tool_error(exc) from exc
        except (KeyError, ValueError) as exc:
            raise ToolError(str(exc)) from exc
        shop.record_view(cust, view, surface="mcp", provider=ctx.llm.provider)
        return follow_hint(view, "mcp")

    server.add_tool(orchestrate, name="orchestrate", description="Plan and run a multi-agent workflow for a goal; returns the run view")

    def approve(args: ApproveArgs) -> dict[str, Any]:
        cust = shop_context.customer()
        if cust is not None and not shop.owns_run(cust, args.run_id):
            raise ToolError(f"Unknown run '{args.run_id}'")
        actor = f"customer:{cust['id']}" if cust else "operator"
        try:
            view = orch.resume(args.run_id, args.decisions, wait=run_wait_s(orch.settings), actor=f"mcp:{actor}",
                               on_settled=shop.meter(cust, surface="mcp"))
        except KeyError as exc:
            raise ToolError(f"Unknown run '{args.run_id}'") from exc
        except RunStateError as exc:
            raise ToolError(str(exc)) from exc
        shop.record_view(cust, view, surface="mcp")
        return follow_hint(view, "mcp")

    server.add_tool(approve, name="approve_actions", description="Approve or reject publish-type actions held by a run: while it is "
                                                                 "still going (they execute at once) or at its approval gate")

    def get_run(args: GetRunArgs) -> dict[str, Any]:
        cust = shop_context.customer()
        if cust is not None and not shop.owns_run(cust, args.run_id):
            raise ToolError(f"Unknown run '{args.run_id}'")
        try:
            return follow_hint(orch.view(args.run_id), "mcp")
        except KeyError as exc:
            raise ToolError(f"Unknown run '{args.run_id}'") from exc

    server.add_tool(get_run, name="get_run", description="Follow a run: its status (queued, running, awaiting_approval, completed, "
                                                         "failed, stalled), each step's result so far and the actions waiting for approval")

    agent_tools: set[str] = set()

    def sync_agent_tools() -> None:
        """One run_<agent_id> tool per agent of the catalog; kept in step when the workshop makes or removes an agent."""
        wanted = {a.spec.id: a for a in all_agents()}
        for aid in list(agent_tools):
            if aid not in wanted:
                try:
                    server.remove_tool(f"run_{aid}")
                except Exception:
                    pass
                agent_tools.discard(aid)
        for aid, agent in wanted.items():
            if aid in agent_tools:
                continue
            server.add_tool(_make_runner(aid, agent.Input, orch.settings), name=f"run_{aid}",
                            description=f"{agent.spec.name}: {agent.spec.summary}"[:1000])
            agent_tools.add(aid)

    sync_agent_tools()
    server.sync_agent_tools = sync_agent_tools  # type: ignore[attr-defined]

    # ---- flow tools follow the shop: one run_flow_<id> per published flow, kept in step with publish/unpublish/delete
    lock = threading.Lock()
    current: dict[str, int] = {}

    def sync_flow_tools(_shop: Any = None) -> None:
        with lock:
            wanted = {f.id: f for f in shop.flows() if f.published}
            for fid in list(current):
                if fid not in wanted or wanted[fid].version != current[fid]:
                    try:
                        server.remove_tool(f"run_flow_{fid}")
                    except Exception:
                        pass
                    del current[fid]
            for fid, flow in wanted.items():
                if fid in current:
                    continue
                desc = f"Flow '{flow.name}'" + (f": {flow.description}" if flow.description else "") + ". Steps: " + \
                    " → ".join(" + ".join(st) for st in flow.stages())
                server.add_tool(_make_flow_runner(fid, flow_args_model(flow), orch), name=f"run_flow_{fid}", description=desc[:1000])
                current[fid] = flow.version

    sync_flow_tools()
    shop.on_flows_changed(sync_flow_tools)
    server.sync_flow_tools = sync_flow_tools  # type: ignore[attr-defined]
    return server


def main() -> None:  # pragma: no cover - entry point
    build_mcp().run("stdio")
