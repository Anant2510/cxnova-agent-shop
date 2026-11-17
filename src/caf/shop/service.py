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
from typing import Any

import yaml

from ..config import Settings
from ..core import LLM, RunContext, all_agents, catalog, expected_agent_ids, get_agent, new_run_id
from ..core.security import service_settings
from .policy import CustomerPolicy, PolicyConfigError, load_policy
from .store import RUN_MODES, ShopError, ShopStore

SURFACES = ("api", "mcp", "a2a", "console", "cli", "check")


class ShopService:
    def __init__(self, settings: Settings, store: ShopStore | None = None):
        self.settings = service_settings(settings)
        self.store = store or ShopStore(settings=settings)
        self._lock = threading.Lock()
        self._catalog_cfg: dict[str, Any] | None = None

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
        """Agent ids the customer may run. None (operator, local console) = every agent."""
        if customer is None:
            return set(expected_agent_ids())
        from ..orchestrator import load_playbooks

        subs = self.store.list_subscriptions(customer["id"], active_only=True)
        if any(s["kind"] == "all" for s in subs):
            return set(expected_agent_ids())
        ids: set[str] = set()
        books = load_playbooks()
        for s in subs:
            if s["kind"] == "agent":
                ids.add(s["item_id"])
            elif s["kind"] == "playbook" and s["item_id"] in books:
                ids.update(step.agent for step in books[s["item_id"]].steps)
        return ids

    def entitled_playbooks(self, customer: dict[str, Any] | None) -> list[str]:
        from ..orchestrator import load_playbooks

        allowed = self.entitled_agents(customer)
        return [pid for pid, pb in load_playbooks().items() if all(step.agent in allowed for step in pb.steps)]

    def require_entitled(self, customer: dict[str, Any] | None, agent_id: str) -> None:
        if customer is not None and agent_id not in self.entitled_agents(customer):
            raise ShopError(f"'{agent_id}' is not in your subscription. Subscribed agents: "
                            f"{', '.join(sorted(self.entitled_agents(customer))) or 'none'}", 403)

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
                    options: dict[str, Any] | None = None) -> RunContext:
        if customer is None:
            return RunContext.create(run_id, self.settings, options=options)
        self.check_limits(customer)
        pol = self.policy_of(customer)
        settings = pol.settings_for(self.settings)
        run_id = run_id or new_run_id(f"c-{customer['id'][:12]}")
        opts = {**(options or {}), "customer_id": customer["id"], "run_mode": customer["run_mode"],
                "allowed_agents": sorted(self.entitled_agents(customer)), "surface": surface}
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

    # ------------------------------------------------------------------------------- metering
    def record_result(self, customer: dict[str, Any] | None, run_id: str, result: Any, *, surface: str, provider: str = "") -> None:
        if customer is None:
            return
        data = result if isinstance(result, dict) else result.model_dump(mode="json")
        version = ""
        try:
            version = get_agent(data.get("agent_id", "")).spec.version
        except KeyError:
            pass
        self.store.record_usage(customer_id=customer["id"], run_id=run_id, agent_id=data.get("agent_id", "?"), surface=surface,
                                run_mode=customer["run_mode"], status=data.get("status", "?"), metrics=data.get("metrics") or {},
                                agent_version=version, provider=provider)

    def record_view(self, customer: dict[str, Any] | None, view: dict[str, Any], *, surface: str, provider: str = "") -> None:
        """Meter every finished step of an orchestrated run (idempotent per step: steps already metered are skipped)."""
        if customer is None or not view.get("run_id"):
            return
        seen = {(u["run_id"], u["agent_id"]) for u in self.store.recent_usage(customer["id"], limit=500) if u["run_id"] == view["run_id"]}
        for step in view.get("steps", []):
            r = step.get("result")
            if not r or r.get("status") in ("pending", "running") or (view["run_id"], r.get("agent_id")) in seen:
                continue
            seen.add((view["run_id"], r.get("agent_id")))
            self.record_result(customer, view["run_id"], r, surface=surface, provider=provider)

    # ------------------------------------------------------------------------------- the agent check
    def check_agents(self, customer: dict[str, Any], only: list[str] | None = None, *, tier: str | None = None,
                     max_workers: int = 3) -> dict[str, Any]:
        """Run every subscribed agent's self-test on the customer's model and keep the result on the customer record."""
        from ..health import probe_agent

        ids = only or sorted(self.entitled_agents(customer)) or expected_agent_ids()
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

    # ------------------------------------------------------------------------------- storefront
    def catalog_config(self) -> dict[str, Any]:
        if self._catalog_cfg is None:
            path = Path(self.settings.home) / "config" / "shop_catalog.yaml"
            self._catalog_cfg = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
        return self._catalog_cfg or {}

    def price_of(self, kind: str, item_id: str) -> dict[str, Any]:
        cfg = self.catalog_config()
        base = dict(cfg.get("defaults") or {})
        item = dict(((cfg.get("agents") if kind == "agent" else cfg.get("bundles")) or {}).get(item_id) or {})
        return {"currency": cfg.get("currency", "USD"), "monthly": item.get("monthly", base.get("monthly")),
                "included_runs": item.get("included_runs", base.get("included_runs")), "per_run": item.get("per_run", base.get("per_run")),
                "outcome": item.get("outcome"), "tagline": item.get("tagline"), "note": cfg.get("pricing_note")}

    def listings(self) -> dict[str, Any]:
        """Every agent and bundle as the storefront shows them (public: no customer data)."""
        from ..core.model_router import get_router
        from ..orchestrator import load_playbooks

        agents_by_id = {a.spec.id: a for a in all_agents()}
        routes = get_router(self.settings).table(list(agents_by_id.values()))
        tiers_by_agent: dict[str, set[str]] = {}
        for row in routes:
            tiers_by_agent.setdefault(row["agent"], set()).add(row["tier"])
        agents = []
        for entry in catalog():
            a = agents_by_id[entry["id"]]
            agents.append({**entry, "description": a.spec.description, "use_cases": a.spec.use_cases, "produces": a.spec.produces,
                           "version": a.spec.version, "model_tiers": sorted(tiers_by_agent.get(entry["id"], set())),
                           "price": self.price_of("agent", entry["id"])})
        bundles = []
        for pid, pb in load_playbooks().items():
            bundles.append({"id": pid, "name": pb.name, "description": pb.description, "inputs": pb.inputs,
                            "agents": [s.agent for s in pb.steps], "price": self.price_of("bundle", pid)})
        cfg = self.catalog_config()
        return {"agents": agents, "bundles": bundles, "run_modes": cfg.get("run_modes", {}), "currency": cfg.get("currency", "USD"),
                "pricing_note": cfg.get("pricing_note", "")}

    def listing(self, agent_id: str) -> dict[str, Any]:
        from ..core.model_router import get_router

        agent = get_agent(agent_id)
        desc = agent.describe()
        steps = [r for r in get_router(self.settings).table([agent]) if r["agent"] == agent_id]
        return {**desc, "model_steps": steps, "price": self.price_of("agent", agent_id)}

    # ------------------------------------------------------------------------------- connection details
    def connection_info(self, customer: dict[str, Any], base_url: str) -> dict[str, Any]:
        base = base_url.rstrip("/")
        allowed = sorted(self.entitled_agents(customer))
        first = allowed[0] if allowed else "campaign_brief_generation"
        return {
            "rest": {"base_url": base, "docs": f"{base}/docs", "run_agent": f"{base}/agents/{first}/run", "orchestrate": f"{base}/orchestrate",
                     "header": "Authorization: Bearer <your key>"},
            "mcp": {"url": f"{base}/mcp", "tools": [f"run_{a}" for a in allowed] + ["orchestrate", "approve_actions", "list_agents"],
                    "client_config": {"mcpServers": {"cxnova-agent-shop": {"type": "http", "url": f"{base}/mcp",
                                                                         "headers": {"Authorization": "Bearer <your key>"}}}}},
            "a2a": {"agent_card": f"{base}/.well-known/agent-card.json", "endpoint": f"{base}/a2a"},
            "console": f"{base}/console",
            "run_mode": customer["run_mode"], "run_mode_number": RUN_MODES.get(customer["run_mode"], 1),
        }

    def account_view(self, customer: dict[str, Any], base_url: str) -> dict[str, Any]:
        pol = self.policy_of(customer)
        subs = self.store.list_subscriptions(customer["id"])
        return {"customer": {k: customer[k] for k in ("id", "name", "run_mode", "run_mode_number", "status", "created_at")},
                "subscriptions": subs, "entitled_agents": sorted(self.entitled_agents(customer)),
                "entitled_playbooks": self.entitled_playbooks(customer), "policy": pol.redacted(),
                "usage": self.store.usage_summary(customer["id"]), "recent": self.store.recent_usage(customer["id"], 20),
                "last_check": customer.get("last_check"), "connection": self.connection_info(customer, base_url),
                "keys": self.store.list_keys(customer["id"])}


_SERVICES: dict[str, ShopService] = {}
_SERVICES_LOCK = threading.Lock()


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
