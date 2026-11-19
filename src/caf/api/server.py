"""HTTP service (FastAPI): agents, orchestrator runs, approvals, artifacts, health, A2A, MCP, short links.

    uvicorn caf.api.server:app --port 8080      (or: caf serve)
    OpenAPI docs at /docs

The console (the orchestrator UI) is served at /console, and a browser opening / is sent there; its model calls
go to this server's provider through /console/llm (see caf.api.console_host).

The shop (storefront, customer accounts, admin) is under /shop (see caf.shop.api).

Security: runs created here use the 'service' security policy (no private-network fetches, local files only
under security.file_roots). Two kinds of bearer token are accepted ("Authorization: Bearer <token>" or "X-API-Key"):
the operator's CAF_API_TOKEN (admin: everything) and a customer key issued by the shop (the customer sees and runs
only the agents it subscribed to, under its own policy). Public routes (/, /console, /shop pages and catalog, /health,
/docs, the A2A card and /r/<code> short links) need no token. Without CAF_API_TOKEN, requests with no token are
served from loopback only. Cross-origin browser access is off unless CAF_CORS_ORIGINS lists origins.
"""

from __future__ import annotations

import hmac
import json
import mimetypes
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from starlette.routing import get_route_path

from .. import __version__
from ..config import get_settings
from ..connectors import ConnectorHub
from ..config import as_bool
from ..core import LLM, RunContext, catalog, get_agent
from ..core.security import PolicyError, service_settings, validate_run_id
from ..branding import PRODUCT
from ..health import liveness, summary
from ..orchestrator import Orchestrator, RunStateError, follow_hint, load_playbooks, run_wait_s
from ..shop import ShopError, get_shop
from ..shop import api as shop_api
from ..shop import context as shop_context
from . import a2a, console_host


WAIT_HELP = ("Seconds to wait for the run to settle before answering (default orchestrator.run_wait_s). A run still going "
             "answers 202 with the run so far; follow it with GET /runs/{run_id}")


class RunAgentRequest(BaseModel):
    inputs: dict[str, Any] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict, description="e.g. {'auto_approve': true, 'tier_override': 'fast', 'offline': true}")
    upstream: dict[str, Any] | None = Field(default=None, description="Work of earlier steps the agent may read: {step_id: {agent_id, output}}")


class OrchestrateRequest(BaseModel):
    goal: str
    inputs: dict[str, Any] = Field(default_factory=dict, description="Shared run inputs: url, audience, competitors, industry ...")
    playbook: str | None = None
    agent: str | None = None
    plan: dict[str, Any] | None = Field(default=None, description="Explicit plan {mode, steps:[{id, agent, inputs, needs}]}")
    auto_approve: bool | None = None
    use_planner: bool = True
    wait_s: float | None = Field(default=None, ge=0, le=3600, description=WAIT_HELP)


class RunFlowRequest(BaseModel):
    inputs: dict[str, Any] = Field(default_factory=dict, description="The flow's shared inputs (see GET /flows/{id} → inputs)")
    goal: str | None = Field(default=None, description="Optional goal text; the flow's name when empty")
    auto_approve: bool | None = None
    options: dict[str, Any] = Field(default_factory=dict, description="e.g. {'tier_override': 'fast'}")
    wait_s: float | None = Field(default=None, ge=0, le=3600, description=WAIT_HELP)


class PlanRequest(BaseModel):
    goal: str = ""
    inputs: dict[str, Any] = Field(default_factory=dict, description="Shared run inputs: url, audience, competitors, industry ...")
    playbook: str | None = Field(default=None, description="A flow id: the plan is that flow's steps")
    agent: str | None = Field(default=None, description="A single agent: the plan is that one step")
    use_planner: bool = Field(default=True, description="Let the model plan from the goal (one model call); False uses the deterministic router")


class ApprovalRequest(BaseModel):
    decisions: dict[str, str] = Field(description="{'<step>/<action_id>' or '*': 'approve' | 'reject'}. Works while the run is "
                                                  "still going (approved actions execute at once) and at the approval gate")
    wait_s: float | None = Field(default=None, ge=0, le=3600, description=WAIT_HELP)


class ContinueRequest(BaseModel):
    wait_s: float | None = Field(default=None, ge=0, le=3600, description=WAIT_HELP)


PUBLIC_PATHS = {"/", "/console", "/portal", "/health", "/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json", "/.well-known/agent-card.json",
                "/shop", "/shop/account", "/shop/admin", "/shop/designer", "/shop/runs", "/shop/catalog", "/shop/requests", "/favicon.svg", "/favicon.ico"}
PUBLIC_PREFIXES = ("/shop/static/", "/shop/catalog/", "/shop/agents/", "/shop/flows/")
LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}


def _api_token(settings: Any) -> str:
    return os.environ.get("CAF_API_TOKEN") or str((settings.security or {}).get("api_token") or "")


def _origins(settings: Any) -> list[str]:
    raw = os.environ.get("CAF_CORS_ORIGINS") or (settings.security or {}).get("cors_origins") or ""
    items = raw.split(",") if isinstance(raw, str) else list(raw)
    return [o.strip().rstrip("/") for o in items if str(o).strip()]


class RootPathStrip:
    """ASGI middleware for a reverse-proxy prefix (CAF_ROOT_PATH=/agentshop). Per the ASGI convention the server
    prepends root_path to the request path, so a proxy that strips the prefix yields "/agentshop/shop" and one that
    forwards it unchanged (or a direct call) yields "/agentshop/agentshop/shop". The duplicate is removed here, so
    both reach the routes as "/shop" and every link is built under the prefix."""

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        root = (scope.get("root_path") or "").rstrip("/")
        if scope["type"] in ("http", "websocket") and root:
            path = scope.get("path", "")
            twice = root + root
            if path == twice or path.startswith(twice + "/"):
                path = path[len(root):]
                scope = {**scope, "path": path, "raw_path": path.encode()}
            if path == root:  # "https://host/agentshop" (no slash) is the site root, not an empty route
                scope = {**scope, "path": root + "/", "raw_path": (root + "/").encode()}
        await self.app(scope, receive, send)


class ApiAuth:
    """ASGI middleware: the operator token (admin), a shop customer key (customer), or loopback without a token (local).

    The resolved role and customer are put on the request state (and in caf.shop.context for MCP tools)."""

    def __init__(self, app: Any, settings: Any, shop: Any = None):
        self.app = app
        self.settings = settings
        self.shop = shop

    @staticmethod
    def _public(path: str) -> bool:
        return (path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)
                or (path.startswith("/r/") and not path.rstrip("/").endswith("/stats")))

    async def _run(self, scope: dict[str, Any], receive: Any, send: Any, role: str, customer: dict[str, Any] | None) -> None:
        scope.setdefault("state", {})
        scope["state"]["role"] = role
        scope["state"]["customer"] = customer
        tok_c = shop_context.current_customer.set(customer)
        tok_r = shop_context.current_role.set(role)
        try:
            await self.app(scope, receive, send)
        finally:
            shop_context.current_customer.reset(tok_c)
            shop_context.current_role.reset(tok_r)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = get_route_path(scope)  # the path without the reverse-proxy prefix (root_path)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
        supplied = headers.get("x-api-key", "")
        auth = headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
        token = _api_token(self.settings)
        client = (scope.get("client") or ("", 0))[0] or ""
        local = client in LOOPBACK or client.startswith("127.") or client.startswith("::ffff:127.")
        if supplied:
            if token and hmac.compare_digest(supplied.encode(), token.encode()):
                await self._run(scope, receive, send, "admin", None)
                return
            customer = self.shop.customer_for_token(supplied) if self.shop is not None else None
            if customer is not None:
                await self._run(scope, receive, send, "customer", customer)
                return
            if not self._public(path):
                await self._deny(send, 401, "Invalid token: send the operator token (CAF_API_TOKEN) or your customer key as "
                                            "'Authorization: Bearer <token>'.")
                return
            await self._run(scope, receive, send, "anonymous", None)
            return
        if self._public(path):
            await self._run(scope, receive, send, "local" if (local and not token) else "anonymous", None)
            return
        if token:
            await self._deny(send, 401, "Missing API token: send 'Authorization: Bearer <token>' (the operator token or your customer key).")
            return
        if local:
            await self._run(scope, receive, send, "local", None)
            return
        await self._deny(send, 401, "This service has no API token configured, so it only answers local clients. "
                                    "Set CAF_API_TOKEN on the server and send it as a bearer token.")

    @staticmethod
    async def _deny(send: Any, status: int, detail: str) -> None:
        body = json.dumps({"detail": detail}).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                                (b"www-authenticate", b"Bearer")]})
        await send({"type": "http.response.body", "body": body})


def create_app() -> FastAPI:
    settings = service_settings(get_settings())  # remote callers: service security policy
    orch = Orchestrator(settings)
    allow_request_auto_approve = as_bool(os.environ.get("CAF_ALLOW_REQUEST_AUTO_APPROVE")
                                         or (settings.approvals or {}).get("allow_request_auto_approve", False))
    mcp_app, mcp_error = None, None
    try:
        from mcp.server.transport_security import TransportSecuritySettings

        from .mcp_server import build_mcp

        hosts = [h.strip() for h in os.environ.get("CAF_MCP_ALLOWED_HOSTS", "localhost:*,127.0.0.1:*,localhost,127.0.0.1,testserver").split(",") if h.strip()]
        security = TransportSecuritySettings(enable_dns_rebinding_protection=os.environ.get("CAF_MCP_DNS_PROTECTION", "1") == "1",
                                             allowed_hosts=hosts, allowed_origins=[f"http://{h}" for h in hosts] + [f"https://{h}" for h in hosts])
        mcp_app = build_mcp(orch).streamable_http_app(streamable_http_path="/", stateless_http=True, json_response=True,
                                                      transport_security=security)
    except Exception as exc:  # MCP SDK missing or incompatible: the REST API still works
        mcp_error = repr(exc)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if mcp_app is not None:  # the MCP session manager needs its own lifespan (task group)
            async with mcp_app.router.lifespan_context(mcp_app):
                yield
        else:
            yield

    shop = get_shop(settings)
    app = FastAPI(title=PRODUCT.name, version=__version__, lifespan=lifespan,
                  description=f"{PRODUCT.tagline} Built on the {PRODUCT.engine}: a LangGraph orchestrator driving 28 vendor-neutral "
                              "marketing agents. Send the operator token or your customer key as a bearer token.")
    app.state.mcp_error = mcp_error
    app.state.settings = settings
    app.state.shop = shop
    app.add_middleware(ApiAuth, settings=settings, shop=shop)
    origins = _origins(settings)
    if origins:  # added after auth, so it runs before it and answers CORS preflights
        app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
                           allow_headers=["Authorization", "Content-Type", "X-API-Key", "Mcp-Session-Id", "Mcp-Protocol-Version"])
    app.add_middleware(RootPathStrip)  # outermost: the proxy prefix is handled before anything looks at the path
    app.state.orchestrator = orch
    app.state.health_cache = None
    # a run this process does not hold (after a restart) is approved / continued on its owner's model and policy
    orch.context_factory = shop.context_for_run

    def base_url(request: Request) -> str:
        return str(request.base_url).rstrip("/")

    def answer(view: dict[str, Any], response: Response) -> dict[str, Any]:
        """A run still going answers 202 with the run so far and where to follow it."""
        if view.get("run_id") and not view.get("settled"):
            response.status_code = 202
        return follow_hint(view, "api")

    def actor(request: Request) -> str:
        cust = getattr(request.state, "customer", None)
        return f"customer:{cust['id']}" if cust else str(getattr(request.state, "role", "") or "operator")

    console_host.register(app, settings)
    shop_api.register(app, settings, shop, orchestrator=orch)

    def customer(request: Request) -> dict[str, Any] | None:
        return getattr(request.state, "customer", None)

    def entitled(request: Request, agent_id: str) -> None:
        try:
            shop.require_entitled(customer(request), agent_id)
        except ShopError as exc:
            raise HTTPException(exc.status, str(exc)) from exc

    def own_run(request: Request, run_id: str) -> None:
        if not shop.owns_run(customer(request), run_id):
            raise HTTPException(404, f"Unknown run '{run_id}'")

    @app.get("/")
    def root(request: Request) -> Any:
        if "text/html" in request.headers.get("accept", ""):
            return RedirectResponse(f"{(request.scope.get('root_path') or '').rstrip('/')}/portal")  # browsers land on the portal
        return {"service": "coforge-agentic-framework", "product": PRODUCT.name, "version": __version__, "docs": f"{base_url(request)}/docs",
                "portal": f"{base_url(request)}/portal", "shop": f"{base_url(request)}/shop", "console": f"{base_url(request)}/console",
                "agents": f"{base_url(request)}/agents", "health": f"{base_url(request)}/health",
                "agent_card": f"{base_url(request)}/.well-known/agent-card.json", "mcp": f"{base_url(request)}/mcp"}

    @app.get("/favicon.svg", include_in_schema=False)
    def favicon() -> Any:
        from fastapi.responses import Response

        from ..branding import MARK_SVG

        return Response(MARK_SVG, media_type="image/svg+xml", headers={"Cache-Control": "max-age=86400"})

    # ------------------------------------------------------------------ health
    @app.get("/health")
    def health() -> dict[str, Any]:
        info = summary()
        info["status"] = "ok" if not info["missing"] else "degraded"
        return info

    @app.get("/health/agents")
    def health_agents(offline: bool = Query(True, description="Probe with the offline model (no model cost)"),
                      refresh: bool = False, packs: bool = Query(False, description="Probe the pack agents too")) -> dict[str, Any]:
        cache = app.state.health_cache
        mode = ("offline" if offline else "live") + ("+packs" if packs else "")
        if cache is None or refresh or cache.get("mode") != mode:
            report = liveness(offline=offline, packs=packs)
            report["mode"] = mode
            app.state.health_cache = report
        return app.state.health_cache

    # ------------------------------------------------------------------ agents
    @app.get("/agents")
    def list_agents(request: Request, full: bool = False, pack: str | None = None) -> list[dict[str, Any]]:
        """The catalog: the blueprint's agents and every agent pack's (`pack=blueprint|campaign_studio|arm_sdlc` narrows
        it). A customer key lists only the agents in that customer's subscription."""
        allowed = shop.entitled_agents(customer(request))
        return [a for a in catalog(full=full) if a["id"] in allowed and (not pack or a.get("pack") == pack)]

    @app.get("/packs")
    def packs() -> list[dict[str, Any]]:
        """The agent packs in this layer: the product each comes from, how many agents and flows it brings, and
        whether that product is connected (so its agents can run at home)."""
        from ..packs import list_packs

        return list_packs()

    @app.get("/agents/{agent_id}")
    def describe_agent(request: Request, agent_id: str) -> dict[str, Any]:
        try:
            agent = get_agent(agent_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        entitled(request, agent_id)
        return agent.describe()

    @app.post("/agents/{agent_id}/run")
    def run_agent(request: Request, agent_id: str, req: RunAgentRequest = Body(default_factory=RunAgentRequest)) -> dict[str, Any]:
        try:
            agent = get_agent(agent_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        entitled(request, agent_id)
        if req.options.get("auto_approve") and not allow_request_auto_approve:
            raise HTTPException(403, "Per-request auto_approve is disabled on this server (approvals.allow_request_auto_approve). "
                                     "Approve held actions instead, or have the operator set CAF_AUTO_APPROVE.")
        inputs = req.inputs or agent.selftest_input()
        cust = customer(request)
        try:
            ctx = shop.context_for(cust, surface="api", options=req.options)
        except ShopError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        if req.options.get("offline"):  # no model: deterministic stages and placeholders (a pack agent answers with its twin)
            ctx.llm = LLM(settings.llm, tracer=ctx.tracer, provider="offline")
            ctx.connectors.llm = ctx.llm
        upstream = {k: v for k, v in (req.upstream or {}).items() if isinstance(v, dict)} or None
        result = agent.run(inputs, ctx, upstream=upstream, options=req.options)
        shop.record_result(cust, ctx.run_id, result, surface="api", provider=ctx.llm.provider)
        data = result.model_dump(mode="json")
        data["run_id"] = ctx.run_id
        return data

    # ------------------------------------------------------------------ orchestrator
    @app.get("/playbooks")
    def playbooks(request: Request) -> list[dict[str, Any]]:
        """Playbooks (built-in and published custom flows). A customer key lists only the flows in the subscription."""
        allowed = set(shop.entitled_flows(customer(request)))
        return [p.model_dump() for pid, p in load_playbooks().items() if pid in allowed]

    @app.get("/flows")
    def flows(request: Request) -> list[dict[str, Any]]:
        """Orchestration flows you may run: built-in playbooks and published custom flows (customers: those rented)."""
        allowed = set(shop.entitled_flows(customer(request)))
        return [shop.flow_listing(f) for f in shop.flows() if f.id in allowed]

    @app.get("/flows/{flow_id}")
    def get_flow(request: Request, flow_id: str) -> dict[str, Any]:
        try:
            return shop.flow_listing(shop.require_entitled_flow(customer(request), flow_id))
        except ShopError as exc:
            raise HTTPException(exc.status, str(exc)) from exc

    @app.post("/flows/{flow_id}/run")
    def run_flow(request: Request, response: Response, flow_id: str, req: RunFlowRequest) -> dict[str, Any]:
        """Run a whole flow with its shared inputs; returns the run view (steps, outputs, artifacts, pending approvals).

        The run executes on the server's worker pool. The call waits up to `wait_s` seconds (default
        orchestrator.run_wait_s); a run still going answers 202 with the run so far: follow it with GET /runs/{run_id}."""
        if req.auto_approve and not allow_request_auto_approve:
            raise HTTPException(403, "Per-request auto_approve is disabled on this server (approvals.allow_request_auto_approve). "
                                     "Approve held actions via /runs/{id}/approvals, or have the operator set CAF_AUTO_APPROVE.")
        try:
            view = shop.run_flow(customer(request), flow_id, req.inputs, orchestrator=orch, goal=req.goal, auto_approve=req.auto_approve,
                                 surface="api", options=req.options, wait=run_wait_s(settings, req.wait_s))
        except ShopError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        return answer(view, response)

    @app.post("/orchestrate")
    def orchestrate(request: Request, response: Response, req: OrchestrateRequest) -> dict[str, Any]:
        """Plan and run a goal across the agents. Waits up to `wait_s` seconds; a run still going answers 202 (see
        POST /flows/{id}/run)."""
        if req.auto_approve and not allow_request_auto_approve:
            raise HTTPException(403, "Per-request auto_approve is disabled on this server (approvals.allow_request_auto_approve). "
                                     "Approve held actions via /runs/{id}/approvals, or have the operator set CAF_AUTO_APPROVE.")
        cust = customer(request)
        if req.agent:
            entitled(request, req.agent)
        wait = run_wait_s(settings, req.wait_s)
        try:
            if req.playbook and cust is not None:  # a rented flow runs as a whole, its agents included
                view = shop.run_flow(cust, req.playbook, req.inputs, orchestrator=orch, goal=req.goal, auto_approve=req.auto_approve,
                                     surface="api", wait=wait)
                return answer(view, response)
            ctx = shop.context_for(cust, surface="api") if cust is not None else None
            provider = ctx.llm.provider if ctx else ""
            view = orch.start(req.goal, inputs=req.inputs, agent=req.agent, playbook=req.playbook, plan=req.plan,
                              auto_approve=req.auto_approve, use_planner=req.use_planner, ctx=ctx, wait=wait,
                              on_settled=shop.meter(cust, surface="api", provider=provider))
        except ShopError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        except (KeyError, ValueError) as exc:  # unknown agent or playbook, a plan with no valid step
            raise HTTPException(400, str(exc)) from exc
        shop.record_view(cust, view, surface="api", provider=provider)   # steps done so far (each step is metered once)
        return answer(view, response)

    @app.post("/plan")
    def plan(request: Request, req: PlanRequest) -> dict[str, Any]:
        """What the orchestrator would run for a goal (or a flow, or one agent), without running it: the route taken,
        the steps with their agents, inputs and dependencies, and any warnings. With a model configured and
        use_planner on, this is one model call."""
        from ..core import new_run_id
        from ..orchestrator import route_and_plan

        cust = customer(request)
        if req.agent:
            entitled(request, req.agent)
        if not (req.goal.strip() or req.playbook or req.agent):
            raise HTTPException(400, "Give a goal, a flow (playbook) or an agent to plan for")
        try:
            ctx = shop.customer_context(cust, new_run_id("plan"), surface="plan")
        except ShopError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        ctx.tracer.sink = None  # a preview leaves no run folder behind
        try:
            decision, p, warnings = route_and_plan(req.goal, ctx, agent=req.agent, playbook=req.playbook, inputs=req.inputs,
                                                   use_planner=req.use_planner)
        except (KeyError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        allowed = shop.entitled_agents(cust) if cust is not None else None
        steps = []
        for s in p.steps:
            d = s.model_dump()
            d["entitled"] = allowed is None or s.agent in allowed
            steps.append(d)
        return {"route": decision.model_dump(), "plan": {"mode": p.mode, "playbook": p.playbook, "reason": p.reason, "steps": steps},
                "warnings": warnings, "planner": "model" if (req.use_planner and not ctx.llm.offline) else "router"}

    @app.get("/runs")
    def list_runs(request: Request, limit: int = Query(50, ge=1, le=2000),
                  status: str | None = Query(None, description="queued | running | awaiting_approval | completed | "
                                                              "completed_with_errors | failed | stalled"),
                  agent: str | None = Query(None, description="Only runs that used this agent"),
                  label: str | None = Query(None, description="Only runs of this flow (or playbook, or single agent)"),
                  q: str | None = Query(None, description="Text in the goal, label, run id or customer")) -> list[dict[str, Any]]:
        """Runs, newest first, with each run's agents, step counts, model calls and tokens: the operator sees every run,
        a customer key its own."""
        cust = customer(request)
        return orch.list_runs(limit=limit, customer_id=cust["id"] if cust else None, status=status, agent=agent, label=label, query=q)

    @app.get("/runs/stats")
    def run_stats(request: Request, days: int = Query(14, ge=1, le=90)) -> dict[str, Any]:
        """Runs per day by outcome, today's totals, and the busiest agents and flows (the operator: everyone's; a
        customer: its own)."""
        cust = customer(request)
        return orch.stats(customer_id=cust["id"] if cust else None, days=days)

    @app.get("/approvals")
    def approvals(request: Request) -> list[dict[str, Any]]:
        """Every action waiting for a decision across the runs still open, with its run and goal."""
        cust = customer(request)
        return orch.pending_approvals(customer_id=cust["id"] if cust else None)

    @app.get("/runs/{run_id}")
    def get_run(request: Request, run_id: str) -> dict[str, Any]:
        """The run as it stands. status: queued | running | awaiting_approval | completed | completed_with_errors |
        failed | stalled. Held actions show in pending_approvals as soon as their step finishes."""
        own_run(request, run_id)
        try:
            return follow_hint(orch.view(run_id), "api")
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/runs/{run_id}/approvals")
    def approve(request: Request, response: Response, run_id: str, req: ApprovalRequest) -> dict[str, Any]:
        """Approve or reject held actions. While the run is still going, approved actions execute at once and the run
        goes on; at the approval gate (status awaiting_approval) the run resumes with the decisions."""
        own_run(request, run_id)
        cust = customer(request)
        try:
            view = orch.resume(run_id, req.decisions, wait=run_wait_s(settings, req.wait_s), actor=actor(request),
                               on_settled=shop.meter(cust, surface="api"))
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except RunStateError as exc:
            raise HTTPException(409, str(exc)) from exc
        shop.record_view(cust, view, surface="api")
        return answer(view, response)

    @app.post("/runs/{run_id}/continue")
    def continue_run(request: Request, response: Response, run_id: str,
                     req: ContinueRequest = Body(default_factory=ContinueRequest)) -> dict[str, Any]:
        """Pick up a run that stopped mid-way (status stalled, for example after a restart) from its last checkpoint.
        Steps that finished are not run again."""
        own_run(request, run_id)
        cust = customer(request)
        try:
            view = orch.continue_run(run_id, wait=run_wait_s(settings, req.wait_s), on_settled=shop.meter(cust, surface="api"))
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except RunStateError as exc:
            raise HTTPException(409, str(exc)) from exc
        shop.record_view(cust, view, surface="api")
        return answer(view, response)

    def run_folder(run_id: str) -> Path:
        try:
            validate_run_id(run_id)  # no '.', '..' or separators: a run id names exactly one folder under runs/
        except PolicyError as exc:
            raise HTTPException(404, "unknown run") from exc
        return settings.runs_dir.resolve() / run_id

    @app.get("/runs/{run_id}/artifacts/{path:path}")
    def artifact(request: Request, run_id: str, path: str) -> FileResponse:
        own_run(request, run_id)
        folder = run_folder(run_id)
        target = (folder / path).resolve()
        if not target.is_relative_to(folder) or not target.is_file():
            raise HTTPException(404, "artifact not found")
        return FileResponse(target, media_type=mimetypes.guess_type(target.name)[0] or "application/octet-stream")

    @app.get("/runs/{run_id}/trace")
    def trace(request: Request, run_id: str,
              since: int = Query(0, ge=0, description="Skip the first `since` events (a viewer following a live run)")) -> list[dict[str, Any]]:
        own_run(request, run_id)
        f = run_folder(run_id) / "trace.jsonl"
        if not f.exists():
            raise HTTPException(404, "no trace for this run")
        lines = [line for line in f.read_text(encoding="utf-8").splitlines() if line.strip()]
        out = []
        for line in lines[since:]:
            try:
                out.append(json.loads(line))
            except ValueError:  # a line being written right now
                break
        return out

    # ------------------------------------------------------------------ connectors + short links
    @app.get("/connectors")
    def connectors() -> list[dict[str, Any]]:
        return ConnectorHub(settings).describe()

    @app.get("/r/{code}")
    def shortlink(code: str, request: Request) -> RedirectResponse:
        links = ConnectorHub(settings).get("shortlink")
        target = links.resolve(code)
        if not target:
            raise HTTPException(404, "unknown short link")
        links.record_scan(code, {"ua": request.headers.get("user-agent", ""), "ref": request.headers.get("referer", "")})
        return RedirectResponse(target, status_code=302)

    @app.get("/r/{code}/stats")
    def shortlink_stats(code: str) -> dict[str, Any]:
        return ConnectorHub(settings).get("shortlink").stats(code)

    # ------------------------------------------------------------------ A2A
    @app.get("/.well-known/agent-card.json")
    def agent_card(request: Request) -> dict[str, Any]:
        """The orchestrator's A2A card. With a customer key, the skills are the customer's subscribed agents."""
        cust = customer(request)
        allowed = shop.entitled_agents(cust) if cust else None
        flows = set(shop.entitled_flows(cust))
        return a2a.orchestrator_card(base_url(request), allowed=allowed, flows=[shop.flow_listing(f) for f in shop.flows() if f.id in flows])

    @app.get("/a2a/agents/{agent_id}/agent-card.json")
    def single_card(agent_id: str, request: Request) -> dict[str, Any]:
        entitled(request, agent_id)
        try:
            return a2a.single_agent_card(agent_id, base_url(request))
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    def a2a_call(request: Request, body: dict[str, Any], agent_id: str | None) -> JSONResponse:
        cust = customer(request)
        if agent_id:
            entitled(request, agent_id)
        made: list[Any] = []

        def make_ctx() -> Any:
            if cust is None:
                return None
            ctx = shop.context_for(cust, surface="a2a")
            made.append(ctx)
            return ctx

        wait = run_wait_s(settings)

        def run_flow(flow_id: str, inputs: dict[str, Any], goal: str | None) -> dict[str, Any]:
            return shop.run_flow(cust, flow_id, inputs, orchestrator=orch, goal=goal, surface="a2a", wait=wait)

        def settled(view: dict[str, Any]) -> None:  # every step metered once, when the run settles
            shop.record_view(cust, view, surface="a2a", provider=made[0].llm.provider if made else "")

        try:
            reply = a2a.handle_jsonrpc(body, orch, agent_id=agent_id, ctx=make_ctx, run_owner=(lambda rid: shop.owns_run(cust, rid)),
                                       run_flow=run_flow, wait=wait, on_settled=settled if cust is not None else None)
        except ShopError as exc:
            return JSONResponse({"jsonrpc": "2.0", "id": body.get("id"), "error": {"code": -32000, "message": str(exc)}})
        for ctx in made:
            if "result" in reply:
                try:
                    shop.record_view(cust, orch.view(ctx.run_id), surface="a2a", provider=ctx.llm.provider)
                except KeyError:
                    pass
        return JSONResponse(reply)

    @app.post("/a2a")
    def a2a_rpc(request: Request, body: dict[str, Any] = Body(...)) -> JSONResponse:
        return a2a_call(request, body, None)

    @app.post("/a2a/agents/{agent_id}")
    def a2a_agent_rpc(request: Request, agent_id: str, body: dict[str, Any] = Body(...)) -> JSONResponse:
        return a2a_call(request, body, agent_id)

    # ------------------------------------------------------------------ MCP (streamable HTTP)
    if mcp_app is not None:
        app.mount("/mcp", mcp_app)

    return app


app = create_app()
