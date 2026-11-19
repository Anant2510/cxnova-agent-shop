"""The shop: customers rent agents and run them under their own policy.

ShopService sits between the surfaces (REST, MCP, A2A, console, CLI) and the framework:

* a bearer token that is a customer key resolves to the customer (the operator's CAF_API_TOKEN stays the admin key)
* a customer sees and runs only the agents and playbooks it subscribed to (entitlements)
* every run of a customer gets a RunContext built from its policy: the customer's own model deployment (run modes
  2 and 3) or the shop's (mode 1), masking of prompts, the allowed websites, and the customer id on the run
* every agent result is metered: customer, agent, version, surface, run mode, provider, tokens, duration, outcome
* "check agents" runs the framework's liveness probe on the customer's model, so a customer knows before it
  subscribes which agents pass there
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from ..config import Settings
from ..core import LLM, RunContext, all_agents, catalog, expected_agent_ids, get_agent, new_run_id
from ..core.security import service_settings
from ..orchestrator.playbooks import Playbook, StepDef, builtin_playbooks, set_extra_source
from .flows import Flow, FlowInput, FlowStep, flow_from_playbook, now_iso, validate_flow
from .policy import CustomerPolicy, PolicyConfigError, load_policy
from .store import RUN_MODES, ShopError, ShopStore

SURFACES = ("api", "mcp", "a2a", "console", "cli", "check")


class ShopService:
    def __init__(self, settings: Settings, store: ShopStore | None = None):
        self.settings = service_settings(settings)
        self.store = store or ShopStore(settings=settings)
        self._lock = threading.Lock()
        self._catalog_cfg: dict[str, Any] | None = None
        self._flow_listeners: list[Callable[[ShopService], None]] = []
        self._flows_cache: tuple[tuple[int, int], dict[str, Flow]] | None = None
        # published custom flows are playbooks for the router, the planner and the console
        set_extra_source(self._custom_playbooks)

    # ------------------------------------------------------------------------------- customers
    def customer_for_token(self, token: str) -> dict[str, Any] | None:
        return self.store.customer_for_key(token) if token else None

    def policy_of(self, customer: dict[str, Any]) -> CustomerPolicy:
        return load_policy(customer.get("policy"))

    def set_policy(self, customer_id: str, policy: dict[str, Any] | CustomerPolicy) -> dict[str, Any]:
        pol = policy if isinstance(policy, CustomerPolicy) else load_policy(policy)
        stored = pol.for_storage()
        customer = self.store.update_customer(customer_id, policy=stored)
        if pol.models.uses_customer_model and customer["run_mode"] == "coforge_cloud":
            customer = self.store.update_customer(customer_id, run_mode="coforge_cloud_customer_model")
        return customer

    # ------------------------------------------------------------------------------- entitlements
    def entitled_agents(self, customer: dict[str, Any] | None) -> set[str]:
        """Agent ids the customer may run one by one. None (operator, local console) = every agent.

        Renting a flow does not rent its agents: a flow subscription entitles the customer to run the flow as a
        whole (see run_flow); single agents are rented as agents, or with an "all" subscription.
        """
        if customer is None:
            return {a.spec.id for a in all_agents()}   # the blueprint's agents and every pack's
        subs = self.store.list_subscriptions(customer["id"], active_only=True)
        if any(s["kind"] == "all" for s in subs):
            return {a.spec.id for a in all_agents()}   # the blueprint's agents and every pack's
        return {s["item_id"] for s in subs if s["kind"] == "agent"}

    def entitled_flows(self, customer: dict[str, Any] | None) -> list[str]:
        """Ids of the published flows the customer may run: rented as flows, or every agent of the flow rented."""
        published = [f for f in self.flows() if f.published]
        if customer is None:
            return [f.id for f in published]
        subs = self.store.list_subscriptions(customer["id"], active_only=True)
        if any(s["kind"] == "all" for s in subs):
            return [f.id for f in published]
        rented = {s["item_id"] for s in subs if s["kind"] in ("flow", "playbook")}
        agents = self.entitled_agents(customer)
        return [f.id for f in published if f.id in rented or all(a in agents for a in f.agents())]

    def entitled_playbooks(self, customer: dict[str, Any] | None) -> list[str]:
        return self.entitled_flows(customer)

    def require_entitled(self, customer: dict[str, Any] | None, agent_id: str) -> None:
        if customer is not None and agent_id not in self.entitled_agents(customer):
            raise ShopError(f"'{agent_id}' is not in your subscription. Subscribed agents: "
                            f"{', '.join(sorted(self.entitled_agents(customer))) or 'none'}", 403)

    def require_entitled_flow(self, customer: dict[str, Any] | None, flow_id: str) -> Flow:
        flow = self.flow(flow_id)
        if flow is None:
            raise ShopError(f"unknown flow '{flow_id}'", 404)
        if customer is not None:
            if not flow.published:
                raise ShopError(f"flow '{flow_id}' is not published", 404)
            if flow_id not in self.entitled_flows(customer):
                raise ShopError(f"flow '{flow_id}' is not in your subscription. Subscribed flows: "
                                f"{', '.join(self.entitled_flows(customer)) or 'none'}", 403)
        return flow

    # ------------------------------------------------------------------------------- limits
    def check_limits(self, customer: dict[str, Any]) -> None:
        pol = self.policy_of(customer)
        if customer.get("status") != "active":
            raise ShopError("this customer account is not active", 403)
        if pol.limits.runs_per_day and self.store.runs_today(customer["id"]) >= pol.limits.runs_per_day:
            raise ShopError(f"daily limit reached ({pol.limits.runs_per_day} runs per day in your policy)", 429)
        if pol.limits.tokens_per_month:
            used = self.store.usage_summary(customer["id"])["totals"]
            if used["input_tokens"] + used["output_tokens"] >= pol.limits.tokens_per_month:
                raise ShopError(f"monthly token limit reached ({pol.limits.tokens_per_month} tokens in your policy)", 429)

    # ------------------------------------------------------------------------------- run contexts
    def llm_for(self, customer: dict[str, Any] | None, tracer: Any = None) -> LLM | None:
        """An LLM facade for the customer's policy, or None when the shop's default serves as it is."""
        if customer is None:
            return None
        pol = self.policy_of(customer)
        try:
            custom = pol.llm_settings(self.settings.llm)
        except PolicyConfigError as exc:
            raise ShopError(f"model policy cannot be applied: {exc}", 503) from exc
        masker = pol.masker()
        if custom is None and masker is None and pol.models.images_allowed:
            return None
        # the provider is named explicitly: CAF_LLM_PROVIDER on the server must not override a customer's policy
        llm = LLM(custom or self.settings.llm, tracer=tracer, provider=custom.provider if custom else None)
        llm.preprocess = masker
        llm.images_allowed = pol.models.images_allowed
        return llm

    def context_for(self, customer: dict[str, Any] | None, *, run_id: str | None = None, surface: str = "api",
                    options: dict[str, Any] | None = None, allowed_agents: set[str] | None = None) -> RunContext:
        if customer is None:
            return RunContext.create(run_id, self.settings, options=options)
        self.check_limits(customer)
        pol = self.policy_of(customer)
        settings = pol.settings_for(self.settings)
        run_id = run_id or new_run_id(f"c-{customer['id'][:12]}")
        allowed = self.entitled_agents(customer) if allowed_agents is None else set(allowed_agents)
        opts = {**(options or {}), "customer_id": customer["id"], "run_mode": customer["run_mode"],
                "allowed_agents": sorted(allowed), "surface": surface}
        ctx = RunContext.create(run_id, settings, options=opts)
        llm = self.llm_for(customer, tracer=ctx.tracer)
        if llm is not None:  # the customer's own model (or the shop's with masking): the run and its connectors use it
            ctx.llm = llm
            ctx.connectors.llm = llm
        self.store.record_run(ctx.run_id, customer["id"], surface)
        ctx.tracer.event("shop.customer", customer=customer["id"], run_mode=customer["run_mode"], surface=surface,
                         provider=ctx.llm.provider, policy_version=pol.version)
        return ctx

    def owns_run(self, customer: dict[str, Any] | None, run_id: str) -> bool:
        return customer is None or self.store.run_owner(run_id) == customer["id"]

    def context_for_run(self, run_id: str) -> RunContext:
        """The context of an existing run this process does not hold (approving or continuing it after a restart):
        a customer's run goes back on the customer's model and policy. No limit check and no new run record: the run
        was counted when it started."""
        owner = self.store.run_owner(run_id)
        if not owner:
            return RunContext.create(run_id, self.settings)
        try:
            customer = self.store.get_customer(owner)
        except ShopError:
            return RunContext.create(run_id, self.settings, options={"customer_id": owner})
        return self.customer_context(customer, run_id, surface="resume")

    def customer_context(self, customer: dict[str, Any] | None, run_id: str, *, surface: str) -> RunContext:
        """A context on the customer's model and policy that is not a run of theirs: nothing is recorded or counted
        (plan previews, decisions on a run after a restart). The operator's is the shop's own."""
        if customer is None:
            return RunContext.create(run_id, self.settings)
        pol = self.policy_of(customer)
        opts = {"customer_id": customer["id"], "run_mode": customer["run_mode"], "surface": surface}
        ctx = RunContext.create(run_id, pol.settings_for(self.settings), options=opts)
        llm = self.llm_for(customer, tracer=ctx.tracer)
        if llm is not None:
            ctx.llm = llm
            ctx.connectors.llm = llm
        return ctx

    def meter(self, customer: dict[str, Any] | None, *, surface: str, provider: str = "") -> Callable[[dict[str, Any]], None] | None:
        """A callback that meters a run's steps whenever the run settles (``Orchestrator.start(on_settled=...)``)."""
        if customer is None:
            return None
        return lambda view: self.record_view(customer, view, surface=surface, provider=provider)

    # ------------------------------------------------------------------------------- metering
    def record_result(self, customer: dict[str, Any] | None, run_id: str, result: Any, *, surface: str, provider: str = "",
                      step_id: str = "") -> bool:
        if customer is None:
            return False
        data = result if isinstance(result, dict) else result.model_dump(mode="json")
        version = ""
        try:
            version = get_agent(data.get("agent_id", "")).spec.version
        except KeyError:
            pass
        return self.store.record_usage(customer_id=customer["id"], run_id=run_id, agent_id=data.get("agent_id", "?"), surface=surface,
                                       run_mode=customer["run_mode"], status=data.get("status", "?"), metrics=data.get("metrics") or {},
                                       agent_version=version, provider=provider, step_id=step_id)

    def record_view(self, customer: dict[str, Any] | None, view: dict[str, Any], *, surface: str, provider: str = "") -> None:
        """Meter every finished step of an orchestrated run, once per step: a flow that runs the same agent in five
        steps is metered five times, and metering the same run again (each poll, each settle) adds nothing."""
        if customer is None or not view.get("run_id"):
            return
        run_id = view["run_id"]
        done = self.store.metered_steps(run_id)
        for step in view.get("steps", []):
            r = step.get("result")
            sid = step.get("id") or ""
            if not r or not sid or sid in done or r.get("status") in ("pending", "running"):
                continue
            if self.record_result(customer, run_id, r, surface=surface, provider=provider, step_id=sid):
                done.add(sid)

    # ------------------------------------------------------------------------------- the agent check
    def check_agents(self, customer: dict[str, Any], only: list[str] | None = None, *, tier: str | None = None,
                     max_workers: int = 3) -> dict[str, Any]:
        """Run every subscribed agent's self-test on the customer's model and keep the result on the customer record."""
        from ..health import probe_agent

        ids = only or sorted(self.entitled_agents(customer)) or expected_agent_ids()   # nothing rented yet: the blueprint's
        ctx = self.context_for(customer, run_id=new_run_id(f"check-{customer['id'][:10]}"), surface="check",
                               options={"tier_override": tier} if tier else None)
        started = datetime.now(timezone.utc)
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            rows = list(pool.map(lambda a: probe_agent(a, ctx), ids))
        for row in rows:
            self.store.record_usage(customer_id=customer["id"], run_id=ctx.run_id, agent_id=row["agent_id"], surface="check",
                                    run_mode=customer["run_mode"], status=row["status"], provider=ctx.llm.provider,
                                    metrics={"llm_calls": row.get("llm_calls", 0), "duration_ms": row.get("duration_ms", 0)})
        passed = [r["agent_id"] for r in rows if r["live"]]
        report = {"checked_at": started.isoformat(timespec="seconds"), "run_id": ctx.run_id, "provider": ctx.llm.provider,
                  "models": ctx.llm.describe().get("models", {}), "passed": len(passed), "total": len(rows),
                  "agents": [{k: r.get(k) for k in ("agent_id", "name", "live", "status", "summary", "error", "degraded_reasons",
                                                      "duration_ms", "llm_calls")} for r in rows]}
        self.store.update_customer(customer["id"], last_check=report)
        return report

    # ------------------------------------------------------------------------------- flows (orchestrations)
    def _custom_flows(self) -> dict[str, Flow]:
        """Custom flows from the database, cached until the file changes (the CLI writes to the same file)."""
        try:
            st = self.store.path.stat()
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = (0, 0)
        cached = self._flows_cache
        if cached is not None and cached[0] == stamp:
            return cached[1]
        flows: dict[str, Flow] = {}
        for raw in self.store.list_flows():
            try:
                flows[raw["id"]] = Flow.model_validate(raw)
            except Exception:  # a record from a newer release this code cannot read: skip, never crash the shop
                continue
        self._flows_cache = (stamp, flows)
        return flows

    def _custom_playbooks(self) -> dict[str, Playbook]:
        out: dict[str, Playbook] = {}
        for f in self._custom_flows().values():
            if f.published:
                out[f.id] = Playbook(id=f.id, name=f.name, description=f.description, triggers=list(f.triggers),
                                     inputs=[i.name for i in f.inputs],
                                     steps=[StepDef(id=s.id, agent=s.agent, purpose=s.purpose, inputs=dict(s.inputs), needs=list(s.needs))
                                            for s in f.steps])
        return out

    def flows(self) -> list[Flow]:
        """Every flow: the built-in playbooks (published, read-only) then the custom ones (drafts included)."""
        builtin = [flow_from_playbook(pb) for pb in builtin_playbooks().values()]
        custom = [f for fid, f in self._custom_flows().items() if fid not in builtin_playbooks()]
        return builtin + custom

    def flow(self, flow_id: str) -> Flow | None:
        pb = builtin_playbooks().get(flow_id)
        if pb is not None:
            return flow_from_playbook(pb)
        return self._custom_flows().get(flow_id)

    def on_flows_changed(self, listener: Callable[[ShopService], None]) -> None:
        """Register a callback for flow changes (the MCP server keeps its run_flow_<id> tools in step)."""
        self._flow_listeners.append(listener)

    def _flows_changed(self) -> None:
        self._flows_cache = None
        for fn in list(self._flow_listeners):
            try:
                fn(self)
            except Exception:
                pass

    def save_flow(self, data: dict[str, Any] | Flow, *, publish: bool | None = None) -> Flow:
        """Create or update a custom flow. Invalid flows are refused with every problem listed (400)."""
        try:
            flow = data if isinstance(data, Flow) else Flow.model_validate({**data, "source": "custom"})
        except Exception as exc:
            raise ShopError(f"invalid flow: {exc}", 400) from exc
        if flow.id in builtin_playbooks():
            raise ShopError(f"'{flow.id}' is a built-in flow; clone it under another id to change it", 409)
        problems = validate_flow(flow)
        if problems:
            raise ShopError("flow has problems: " + "; ".join(problems), 400)
        existing = self._custom_flows().get(flow.id)
        if publish is not None:
            status = "published" if publish else "draft"
        elif isinstance(data, dict) and "status" in data:
            status = flow.status
        else:
            status = existing.status if existing else flow.status
        published_at = existing.published_at if existing else None
        if status == "published" and not published_at:
            published_at = now_iso()
        dump = flow.model_dump()
        dump.update(status=status, source="custom", published_at=published_at)
        saved = self.store.save_flow(dump)
        self._flows_changed()
        return Flow.model_validate(saved)

    def publish_flow(self, flow_id: str, published: bool = True) -> Flow:
        flow = self.flow(flow_id)
        if flow is None:
            raise ShopError(f"unknown flow '{flow_id}'", 404)
        if flow.source == "builtin":
            raise ShopError("built-in flows are always published; clone one to make a flow of your own", 409)
        problems = validate_flow(flow) if published else []
        if problems:
            raise ShopError("flow cannot be published: " + "; ".join(problems), 400)
        saved = self.store.set_flow_status(flow_id, "published" if published else "draft", published_at=now_iso() if published else None)
        self._flows_changed()
        return Flow.model_validate(saved)

    def delete_flow(self, flow_id: str) -> None:
        flow = self.flow(flow_id)
        if flow is None:
            raise ShopError(f"unknown flow '{flow_id}'", 404)
        if flow.source == "builtin":
            raise ShopError("built-in flows cannot be deleted", 409)
        self.store.delete_flow(flow_id)
        self._flows_changed()

    def clone_flow(self, flow_id: str, new_id: str, name: str | None = None) -> Flow:
        src = self.flow(flow_id)
        if src is None:
            raise ShopError(f"unknown flow '{flow_id}'", 404)
        if self.flow(new_id) is not None:
            raise ShopError(f"flow '{new_id}' already exists", 409)
        data = src.model_dump()
        data.update(id=new_id, name=name or f"{src.name} (copy)", status="draft", source="custom", version=1, triggers=[],
                    created_at=None, updated_at=None, published_at=None)
        return self.save_flow(data)

    def flow_listing(self, flow: Flow) -> dict[str, Any]:
        """A flow as the storefront and the account page show it: steps with agent names, stages, price."""
        names = {a.spec.id: a.spec.name for a in all_agents()}
        d = flow.listing()
        d["steps"] = [{**s, "agent_name": names.get(s["agent"], s["agent"])} for s in d["steps"]]
        d["agent_names"] = [names.get(a, a) for a in d["agents"]]
        d["price"] = self.price_of("flow", flow.id)
        d["subscribers"] = len(self.store.flow_subscribers(flow.id)) if flow.source == "custom" else None
        return d

    def sample_inputs(self, flow: Flow) -> dict[str, Any]:
        """Shared inputs for a test run: the flow's examples, then each agent's self-test input (first wins)."""
        inputs: dict[str, Any] = {i.name: i.example for i in flow.inputs if i.example}
        for aid in flow.agents():
            try:
                for k, v in get_agent(aid).selftest_input().items():
                    inputs.setdefault(k, v)
            except KeyError:
                continue
        return inputs

    def run_flow(self, customer: dict[str, Any] | None, flow_id: str, inputs: dict[str, Any] | None, *, orchestrator: Any,
                 goal: str | None = None, auto_approve: bool | None = None, surface: str = "api",
                 options: dict[str, Any] | None = None, wait: float | None = None) -> dict[str, Any]:
        """Run a whole flow for a customer (or the operator): entitlement, required inputs, metering.

        ``wait``: None runs the flow on this thread; seconds run it on the orchestrator's worker pool and return the
        run as it stands after at most that long. Every step is metered once, when the run settles."""
        flow = self.require_entitled_flow(customer, flow_id)
        inputs = dict(inputs or {})
        missing = [i.name for i in flow.inputs if i.required and inputs.get(i.name) in (None, "", [], {})]
        if missing:
            raise ShopError(f"flow '{flow_id}' needs input(s): {', '.join(missing)}", 400)
        allowed = (self.entitled_agents(customer) | set(flow.agents())) if customer is not None else None
        ctx = self.context_for(customer, surface=surface, options={**(options or {}), "flow_id": flow_id}, allowed_agents=allowed)
        ctx.tracer.event("shop.flow", flow=flow_id, version=flow.version, source=flow.source)
        view = orchestrator.start(goal or flow.name, inputs=inputs, plan=flow.plan().model_dump(), auto_approve=auto_approve, ctx=ctx,
                                  wait=wait, on_settled=self.meter(customer, surface=surface, provider=ctx.llm.provider))
        view["flow"] = {"id": flow.id, "name": flow.name, "version": flow.version}
        self.record_view(customer, view, surface=surface, provider=ctx.llm.provider)   # steps done so far (idempotent)
        return view

    def test_flow(self, flow_id: str, *, orchestrator: Any, inputs: dict[str, Any] | None = None,
                  customer: dict[str, Any] | None = None, wait: float | None = None) -> dict[str, Any]:
        """The operator's test run of a flow (drafts included) with sample inputs, optionally on a customer's model.
        With ``wait`` the test runs on the worker pool; follow it with the orchestrator's view (GET /runs/{id})."""
        flow = self.flow(flow_id)
        if flow is None:
            raise ShopError(f"unknown flow '{flow_id}'", 404)
        shared = {**self.sample_inputs(flow), **(inputs or {})}
        allowed = (self.entitled_agents(customer) | set(flow.agents())) if customer is not None else None
        ctx = self.context_for(customer, run_id=new_run_id(f"flowtest-{flow_id[:12]}"), surface="check", allowed_agents=allowed)
        ctx.tracer.event("shop.flow.test", flow=flow_id, version=flow.version)
        view = orchestrator.start(flow.description or flow.name, inputs=shared, plan=flow.plan().model_dump(), auto_approve=True, ctx=ctx,
                                  wait=wait)
        view["flow"] = {"id": flow.id, "name": flow.name, "version": flow.version}
        view["sample_inputs"] = shared
        view.update(flow_test_counts(view))
        return view

    # ------------------------------------------------------------------------------- storefront
    def catalog_config(self) -> dict[str, Any]:
        if self._catalog_cfg is None:
            path = Path(self.settings.home) / "config" / "shop_catalog.yaml"
            self._catalog_cfg = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
        return self._catalog_cfg or {}

    def price_of(self, kind: str, item_id: str) -> dict[str, Any]:
        """Indicative price: a custom flow's own price, else config/shop_catalog.yaml (agents / bundles), else the defaults."""
        cfg = self.catalog_config()
        base = dict(cfg.get("defaults") or {})
        if kind == "agent":
            item = dict((cfg.get("agents") or {}).get(item_id) or {})
        else:  # flow / bundle: the yaml names built-in flows under "bundles"; custom flows carry a price of their own
            item = dict((cfg.get("bundles") or {}).get(item_id) or {})
            custom = self._custom_flows().get(item_id)
            if custom is not None and custom.price:
                item.update({k: v for k, v in custom.price.items() if v not in (None, "")})
            if not item:
                item = dict(cfg.get("flow_defaults") or {})
        return {"currency": cfg.get("currency", "USD"), "monthly": item.get("monthly", base.get("monthly")),
                "included_runs": item.get("included_runs", base.get("included_runs")), "per_run": item.get("per_run", base.get("per_run")),
                "outcome": item.get("outcome"), "tagline": item.get("tagline"), "note": cfg.get("pricing_note")}

    def listings(self) -> dict[str, Any]:
        """Every agent and published flow as the storefront shows them (public: no customer data)."""
        from ..core.model_router import get_router

        agents_by_id = {a.spec.id: a for a in all_agents()}
        routes = get_router(self.settings).table(list(agents_by_id.values()))
        tiers_by_agent: dict[str, set[str]] = {}
        for row in routes:
            tiers_by_agent.setdefault(row["agent"], set()).add(row["tier"])
        agents = []
        for entry in catalog():
            a = agents_by_id[entry["id"]]
            agents.append({**entry, "description": a.spec.description, "use_cases": a.spec.use_cases, "produces": a.spec.produces,
                           "version": a.spec.version,
                           "model_tiers": sorted(tiers_by_agent.get(entry["id"]) or ({a.spec.tier.value} if a.spec.pack != "blueprint" else set())),
                           "price": self.price_of("agent", entry["id"])})
        flows = [self.flow_listing(f) for f in self.flows() if f.published]
        cfg = self.catalog_config()
        return {"agents": agents, "flows": flows, "bundles": flows, "run_modes": cfg.get("run_modes", {}),
                "currency": cfg.get("currency", "USD"), "pricing_note": cfg.get("pricing_note", "")}

    def listing(self, agent_id: str) -> dict[str, Any]:
        from ..core.model_router import get_router

        agent = get_agent(agent_id)
        desc = agent.describe()
        router = get_router(self.settings)
        steps = [r for r in router.table([agent]) if r["agent"] == agent_id]
        if agent.spec.pack != "blueprint":  # a pack agent's model steps are its actions (one modelled call each)
            for action in agent.spec.actions:
                d = router.decide(agent_id, action["id"], agent_tier=agent.spec.tier)
                steps.append({"agent": agent_id, "step": action["id"], "work": "modelled from the pack's skill", "tier": d.tier,
                              "effort": d.effort or "-", "source": d.source, "why": "; ".join(d.reasons)})
        return {**desc, "model_steps": steps, "price": self.price_of("agent", agent_id)}

    # ------------------------------------------------------------------------------- connection details
    def connection_info(self, customer: dict[str, Any], base_url: str) -> dict[str, Any]:
        base = base_url.rstrip("/")
        allowed = sorted(self.entitled_agents(customer))
        flows = self.entitled_flows(customer)
        first = allowed[0] if allowed else "campaign_brief_generation"
        rest: dict[str, Any] = {"base_url": base, "docs": f"{base}/docs", "run_agent": f"{base}/agents/{first}/run",
                                "orchestrate": f"{base}/orchestrate", "header": "Authorization: Bearer <your key>"}
        if flows:
            rest["run_flow"] = f"{base}/flows/{flows[0]}/run"
        return {
            "rest": rest,
            "mcp": {"url": f"{base}/mcp",
                    "tools": [f"run_{a}" for a in allowed] + [f"run_flow_{f}" for f in flows] + ["list_agents", "list_flows", "run_flow",
                                                                                                 "orchestrate", "approve_actions"],
                    "client_config": {"mcpServers": {"cxnova-orchestration": {"type": "http", "url": f"{base}/mcp",
                                                                         "headers": {"Authorization": "Bearer <your key>"}}}}},
            "a2a": {"agent_card": f"{base}/.well-known/agent-card.json", "endpoint": f"{base}/a2a",
                    "skills": [f"agent:{a}" for a in allowed] + [f"flow:{f}" for f in flows]},
            "console": f"{base}/console",
            "run_mode": customer["run_mode"], "run_mode_number": RUN_MODES.get(customer["run_mode"], 1),
        }

    def account_view(self, customer: dict[str, Any], base_url: str) -> dict[str, Any]:
        pol = self.policy_of(customer)
        subs = self.store.list_subscriptions(customer["id"])
        flows = self.entitled_flows(customer)
        return {"customer": {k: customer[k] for k in ("id", "name", "run_mode", "run_mode_number", "status", "created_at")},
                "subscriptions": subs, "entitled_agents": sorted(self.entitled_agents(customer)),
                "entitled_playbooks": flows, "entitled_flows": flows,
                "flows": [self.flow_listing(f) for f in self.flows() if f.id in flows], "policy": pol.redacted(),
                "usage": self.store.usage_summary(customer["id"]), "recent": self.store.recent_usage(customer["id"], 20),
                "last_check": customer.get("last_check"), "connection": self.connection_info(customer, base_url),
                "keys": self.store.list_keys(customer["id"])}


_SERVICES: dict[str, ShopService] = {}
_SERVICES_LOCK = threading.Lock()


def flow_test_counts(view: dict[str, Any]) -> dict[str, int]:
    """passed / total for a flow test run (also computed by the designer while it follows a run)."""
    steps = view.get("steps", [])
    return {"passed": sum(1 for s in steps if s.get("status") in ("ok", "degraded")), "total": len(steps)}


def get_shop(settings: Settings) -> ShopService:
    """One ShopService per settings home (the API, the console host and the CLI share it)."""
    key = str(settings.home)
    with _SERVICES_LOCK:
        svc = _SERVICES.get(key)
        if svc is None:
            svc = _SERVICES[key] = ShopService(settings)
        return svc


def reset_shops() -> None:
    with _SERVICES_LOCK:
        _SERVICES.clear()
