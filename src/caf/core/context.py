"""RunContext: everything an agent stage can use, injected as the LangGraph runtime context."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from ..config import Settings, get_settings
from .artifacts import ArtifactStore
from .llm import LLM, _norm_tier
from .prompts import PromptStore
from .security import Policy, validate_run_id
from .skills import SkillStore
from .tracing import Tracer

T = TypeVar("T", bound=BaseModel)


def new_run_id(prefix: str = "run") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{prefix}-{stamp}-{uuid.uuid4().hex[:6]}"


class Meter:
    """Per-agent usage counter (the shared LLM is used concurrently by parallel steps)."""

    def __init__(self) -> None:
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cost_usd = 0.0
        self.unpriced_calls = 0          # calls on a model the price table does not know
        self.models: dict[str, dict[str, Any]] = {}   # per model: calls, tokens and cost
        self.errors = 0
        self.last_error = ""
        self.routes: list[dict[str, Any]] = []  # which model did which step, and why
        self._lock = threading.Lock()

    def route(self, entry: dict[str, Any]) -> None:
        with self._lock:
            self.routes.append(entry)

    def fail(self, message: str) -> None:
        with self._lock:
            self.errors += 1
            self.last_error = message[:300]

    def add(self, usage: dict[str, Any] | None, *, model: str | None = None, cost: float | None = None) -> None:
        """One model call: its reported usage, the model that answered and what it cost (None: not in the price table)."""
        tin = int((usage or {}).get("input_tokens", 0) or 0)
        tout = int((usage or {}).get("output_tokens", 0) or 0)
        with self._lock:
            self.calls += 1
            self.input_tokens += tin
            self.output_tokens += tout
            if cost is None:
                self.unpriced_calls += 1
            else:
                self.cost_usd += cost
            if model:
                m = self.models.setdefault(model, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})
                m["calls"] += 1
                m["input_tokens"] += tin
                m["output_tokens"] += tout
                if cost is None:
                    m["unpriced"] = True
                else:
                    m["cost_usd"] += cost

    def absorb(self, metrics: dict[str, Any] | None) -> None:
        """Roll a sub-agent's metrics into this meter."""
        m = metrics or {}
        with self._lock:
            self.calls += int(m.get("llm_calls", 0) or 0)
            self.errors += int(m.get("llm_errors", 0) or 0)
            self.last_error = self.last_error or str(m.get("llm_last_error") or "")
            self.input_tokens += int(m.get("input_tokens", 0) or 0)
            self.output_tokens += int(m.get("output_tokens", 0) or 0)
            self.cost_usd += float(m.get("cost_usd", 0) or 0)
            self.unpriced_calls += int(m.get("unpriced_calls", 0) or 0)
            for model, sub in (m.get("models") or {}).items():
                mine = self.models.setdefault(model, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})
                for k in ("calls", "input_tokens", "output_tokens"):
                    mine[k] += int(sub.get(k, 0) or 0)
                mine["cost_usd"] += float(sub.get("cost_usd", 0) or 0)
                if sub.get("unpriced"):
                    mine["unpriced"] = True

    def snapshot(self) -> dict[str, Any]:
        snap: dict[str, Any] = {"llm_calls": self.calls, "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                                "cost_usd": round(self.cost_usd, 6)}
        if self.unpriced_calls:
            snap["unpriced_calls"] = self.unpriced_calls
        if self.models:
            snap["models"] = {k: {**v, "cost_usd": round(v["cost_usd"], 6)} for k, v in self.models.items()}
        if self.errors:
            snap.update(llm_errors=self.errors, llm_last_error=self.last_error)
        return snap


# The skills that describe one company, and the parts of each that are that company's facts (None: all of it). The rest of such a
# skill is rules any company keeps: style limits, AP abbreviations, the SQL dialect.
COMPANY_SKILLS: dict[str, tuple[str, ...] | None] = {
    "brand_voice": None,
    "press_boilerplate": ("organisation", "legal_name", "name_variants", "headquarters_dateline", "boilerplate", "media_contact",
                          "disclaimers", "distribution"),
    "translation_glossary": ("translation_memory", "do_not_translate", "terms"),
    "semantic_layer": ("description", "entities", "joins", "metrics", "synonyms", "examples"),
}
DEMO_COMPANY = "Meridian Mutual"   # the fictional company the bundled skills and sample data describe


def _own_skill(customer: str, name: str, settings: Any) -> Any:
    """The customer's own version of a skill, from their workspace (caf.packs.workspace), or None."""
    try:
        from ..packs.workspace import load

        own = (load(customer, settings).get("skills") or {}).get(name)
    except Exception:  # an unreadable workspace is no workspace
        return None
    return own if isinstance(own, (dict, list)) and own else None


def _blank(value: Any) -> Any:
    """The same shape with nothing in it: every key an agent or a template reads is there, and says nothing."""
    if isinstance(value, dict):
        return {k: _blank(v) for k, v in value.items()}
    if isinstance(value, list):
        return []
    return "" if isinstance(value, str) else value


def _without_company(name: str, value: dict[str, Any]) -> dict[str, Any]:
    """A company skill with the bundled company's facts taken out and its rules kept: what a customer's own facts are laid on."""
    facts = COMPANY_SKILLS[name]
    return {k: (_blank(v) if facts is None or k in facts else v) for k, v in value.items()}


class OwnSkillError(ValueError):
    """A customer's own version of a skill does not have the shape of the skill it stands in for."""

    user_facing = True


def _overlay(base: Any, own: Any, path: str) -> Any:
    if isinstance(base, dict):
        if not isinstance(own, dict):
            raise OwnSkillError(f"`{path}` in your workspace's skills must be a set of named values, as it is in this layer's own "
                                f"`{path.split('.')[0]}` skill; what is saved there is {'a list' if isinstance(own, list) else 'a single value'}")
        return {**base, **{k: _overlay(base.get(k), v, f"{path}.{k}") for k, v in own.items()}}
    if isinstance(base, list) and not isinstance(own, list):
        raise OwnSkillError(f"`{path}` in your workspace's skills must be a list, as it is in this layer's own `{path.split('.')[0]}` skill")
    return own


def own_skill(name: str, value: Any, own: Any) -> Any:
    """A customer's own version of a skill laid over the server's: they state what differs and the rest stands.

    For a skill that describes a company, the bundled company's facts are taken out first, so nothing of another company's
    shows through what the customer did not state."""
    if not isinstance(value, dict):
        return own
    return _overlay(_without_company(name, value) if name in COMPANY_SKILLS else value, own, name)


@dataclass
class RunContext:
    run_id: str
    settings: Settings
    llm: LLM
    skills: SkillStore
    prompts: PromptStore
    connectors: Any  # ConnectorHub (typed loosely to avoid an import cycle)
    artifacts: ArtifactStore
    tracer: Tracer
    agent_id: str | None = None
    tier: str = "balanced"
    upstream: dict[str, Any] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)
    meter: Meter = field(default_factory=Meter)
    used_connectors: set[str] = field(default_factory=set)
    demo_profile: set[str] = field(default_factory=set)   # company skills a customer's run took from the bundled demo company
    approvals: Any = None  # orchestrated runs: the run's approvals ledger (caf.orchestrator.service.RunApprovals)

    # ---- construction -----------------------------------------------------------------------
    @classmethod
    def create(cls, run_id: str | None = None, settings: Settings | None = None, *, llm: LLM | None = None,
               options: dict[str, Any] | None = None) -> "RunContext":
        from ..connectors import ConnectorHub  # local import: connectors depend on core

        settings = settings or get_settings()
        run_id = validate_run_id(run_id or new_run_id())  # run ids name folders under runs/
        tracer = Tracer(run_id, settings.runs_dir / run_id / "trace.jsonl")
        llm = llm or LLM(settings.llm, tracer=tracer)
        if llm.tracer is None:
            llm.tracer = tracer
        hub = ConnectorHub(settings, llm=llm)
        return cls(
            run_id=run_id,
            settings=settings,
            llm=llm,
            skills=SkillStore(settings.skills_dirs),
            prompts=PromptStore(settings.prompts_dirs),
            connectors=hub,
            artifacts=ArtifactStore(settings.runs_dir, run_id),
            tracer=tracer,
            options=dict(options or {}),
        )

    def for_agent(self, agent_id: str, *, tier: Any = None, upstream: dict[str, Any] | None = None,
                  options: dict[str, Any] | None = None) -> "RunContext":
        return replace(
            self,
            agent_id=agent_id,
            tier=_norm_tier(self.options.get("tier_override") or tier or self.tier),
            upstream=dict(upstream if upstream is not None else self.upstream),
            options={**self.options, **(options or {})},
            artifacts=self.artifacts.for_agent(agent_id),
            meter=Meter(),
            used_connectors=set(),
            demo_profile=set(),
        )

    # ---- helpers for agent stages -----------------------------------------------------------
    def log(self, message: str, **data: Any) -> None:
        self.tracer.event("agent.log", agent=self.agent_id, message=message, **data)

    def skill(self, name: str, default: Any = ...) -> Any:
        """A skill by name: the server's, or the caller's own where they saved one in their workspace (`skills: {name: {...}}`).

        A customer's own version is laid over the server's, so they state only what differs (their UTM convention, their
        channels). The skills that describe a company (brand voice, press boilerplate, glossary, semantic layer) are laid
        over that skill with the bundled company's facts taken out: a customer's work is never written in another company's
        voice. When a customer's run does fall back to the bundled demo company, it is noted and the result says so
        (BaseAgent.run)."""
        value = self.skills.get(name, default)
        customer = self.options.get("customer_id")
        if not customer:
            return value
        own = _own_skill(customer, name, self.settings)
        if own is not None:
            return own_skill(name, value, own)
        if name in COMPANY_SKILLS and isinstance(value, (dict, list)) and DEMO_COMPANY in json.dumps(value, default=str):
            self.demo_profile.add(name)
        return value

    def connector(self, kind: str, provider: str | None = None) -> Any:
        conn = self.connectors.get(kind, provider)
        self.used_connectors.add(f"{kind}:{conn.provider}")
        return conn

    @property
    def brand(self) -> dict[str, Any]:
        return self.skill("brand_voice", {})

    @property
    def policy(self) -> Policy:
        """The security policy for this run (what it may fetch and read); see caf.core.security."""
        return Policy(self.settings)

    def local_path(self, path: str | Path) -> Path:
        """Resolve a caller-supplied local path (relative to the framework home) under the run's security
        policy. Service surfaces (REST, MCP, A2A) may only read security.file_roots and runs/."""
        return self.policy.check_path(path)

    def render(self, template: str, /, **variables: Any) -> tuple[str | None, str]:
        assert self.agent_id, "render() needs an agent-scoped context"
        base = {"brand": self.brand, "today": datetime.now(timezone.utc).date().isoformat()}
        return self.prompts.render(self.agent_id, template, **{**base, **variables})

    def generate(self, template: str, schema: type[T] | None = None, /, *, tier: Any = None,
                 images: list[tuple[bytes, str]] | None = None, variables: dict[str, Any] | None = None,
                 **kwargs: Any) -> Any:
        """Render config/prompts/<agent>/<template>.j2 and call the model at the agent's tier.

        Template variables come from **kwargs; pass `variables={...}` for names that clash with the
        options (`tier`, `images`, `variables`). Returns a validated `schema` instance, else text.
        """
        from .llm import LLMOutputError
        from .model_router import get_router

        system, user = self.render(template, **{**kwargs, **(variables or {})})
        router = get_router(self.settings)
        decision = router.decide(self.agent_id, template, agent_tier=self.tier, requested=tier,
                                 override=self.options.get("tier_override"), prompt_chars=len(system or "") + len(user),
                                 schema=schema, images=bool(images))
        def model_name(t: str) -> str:  # tolerant of stand-in LLMs that only implement complete/structured
            fn = getattr(self.llm, "model_for", None)
            return fn(t) if callable(fn) else t

        offline = bool(getattr(self.llm, "offline", False))
        progress = os.environ.get("CAF_PROGRESS") == "1" and not offline
        while True:
            model = model_name(decision.tier)
            self.meter.route(decision.as_dict(model))
            self.tracer.event("model.route", agent=self.agent_id, step=template, work=decision.work, tier=decision.tier,
                              model=model, effort=decision.effort, why="; ".join(decision.reasons))
            extra = {"effort": decision.effort} if decision.effort else {}
            t0 = time.perf_counter()
            if progress:
                print(f"  ... {self.agent_id}.{template}: {model} ({decision.work or decision.tier}"
                      + (f", effort {decision.effort}" if decision.effort else "") + ")", file=sys.stderr, flush=True)
            def call(**kw: Any) -> Any:
                if schema is None:
                    return self.llm.complete(user, system=system, tier=decision.tier, images=images, meter=self.meter, **kw)
                return self.llm.structured(user, schema, system=system, tier=decision.tier, images=images, meter=self.meter, **kw)

            tokens0 = (self.meter.input_tokens, self.meter.output_tokens)
            try:
                try:
                    result = call(**extra)
                except TypeError as exc:  # an LLM stand-in without the effort parameter
                    if not extra or "effort" not in str(exc):
                        raise
                    result = call()
                # the shared model client cannot say which agent called it: the agent's context does (run viewer)
                self.tracer.event("model.done", agent=self.agent_id, step=template, model=model,
                                  duration_ms=round((time.perf_counter() - t0) * 1000, 1),
                                  input_tokens=self.meter.input_tokens - tokens0[0], output_tokens=self.meter.output_tokens - tokens0[1])
                if progress:  # labelled: parallel steps finish out of order
                    print(f"      {self.agent_id}.{template} done in {time.perf_counter() - t0:.1f}s", file=sys.stderr, flush=True)
                return result
            except LLMOutputError as exc:
                nxt = decision.escalated() if router.escalate_on_retry and not offline else None
                while nxt is not None and model_name(nxt.tier) == model:  # skip tiers served by the same model
                    nxt = nxt.escalated()
                if nxt is None:
                    self.meter.fail(f"{type(exc).__name__}: {exc}")
                    raise
                decision = nxt

    def upstream_output(self, *agent_ids: str) -> dict[str, Any] | None:
        """Find the most recent upstream output produced by any of the given agents."""
        for key, value in reversed(list(self.upstream.items())):
            if not isinstance(value, dict):
                continue
            if value.get("agent_id") in agent_ids or key in agent_ids:
                return value.get("output", value)
        return None

    def run_subagent(self, agent_id: str, inputs: dict[str, Any], **options: Any) -> Any:
        """Invoke another agent as a sub-graph; its model usage and connectors roll up into this agent's metrics."""
        from .registry import get_agent

        result = get_agent(agent_id).run(inputs, self, upstream=self.upstream, options={**self.options, **options})
        self.meter.absorb(result.metrics)
        self.used_connectors.update((result.provenance or {}).get("connectors", []))
        self.tracer.event("agent.subagent", parent=self.agent_id, child=agent_id, status=result.status)
        return result

    def upstream_findings(self) -> list[dict[str, Any]]:
        """All `findings` emitted by upstream agents in this orchestrated run (see core.handoff.Finding)."""
        found: list[dict[str, Any]] = []
        for key, value in self.upstream.items():
            if not isinstance(value, dict):
                continue
            out = value.get("output", value)
            for f in out.get("findings", []) if isinstance(out, dict) else []:
                if isinstance(f, dict):
                    found.append({**f, "source_agent": f.get("source_agent") or value.get("agent_id") or key})
        return found

    def upstream_text(self, max_chars: int = 6000) -> str:
        """Best-effort plain-text digest of upstream content (summaries, drafts, briefs)."""
        parts: list[str] = []
        for key, value in self.upstream.items():
            if not isinstance(value, dict):
                continue
            out = value.get("output", value)
            if not isinstance(out, dict):
                continue
            for field_name in ("markdown", "summary_markdown", "body_markdown", "executive_summary", "summary", "campaign_overview"):
                v = out.get(field_name)
                if isinstance(v, str) and v.strip():
                    parts.append(f"[{value.get('agent_id', key)}] {v.strip()}")
                    break
        return "\n\n".join(parts)[:max_chars]
