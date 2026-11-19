"""RunContext: everything an agent stage can use, injected as the LangGraph runtime context."""

from __future__ import annotations

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

    def add(self, usage: dict[str, Any] | None) -> None:
        with self._lock:
            self.calls += 1
            if usage:
                self.input_tokens += int(usage.get("input_tokens", 0) or 0)
                self.output_tokens += int(usage.get("output_tokens", 0) or 0)

    def snapshot(self) -> dict[str, Any]:
        snap: dict[str, Any] = {"llm_calls": self.calls, "input_tokens": self.input_tokens, "output_tokens": self.output_tokens}
        if self.errors:
            snap.update(llm_errors=self.errors, llm_last_error=self.last_error)
        return snap


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
        )

    # ---- helpers for agent stages -----------------------------------------------------------
    def log(self, message: str, **data: Any) -> None:
        self.tracer.event("agent.log", agent=self.agent_id, message=message, **data)

    def skill(self, name: str, default: Any = ...) -> Any:
        return self.skills.get(name, default)

    def connector(self, kind: str, provider: str | None = None) -> Any:
        conn = self.connectors.get(kind, provider)
        self.used_connectors.add(f"{kind}:{conn.provider}")
        return conn

    @property
    def brand(self) -> dict[str, Any]:
        return self.skills.get("brand_voice", {})

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

            try:
                try:
                    result = call(**extra)
                except TypeError as exc:  # an LLM stand-in without the effort parameter
                    if not extra or "effort" not in str(exc):
                        raise
                    result = call()
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
        m = result.metrics or {}
        with self.meter._lock:
            self.meter.calls += int(m.get("llm_calls", 0) or 0)
            self.meter.errors += int(m.get("llm_errors", 0) or 0)
            self.meter.last_error = self.meter.last_error or str(m.get("llm_last_error") or "")
            self.meter.input_tokens += int(m.get("input_tokens", 0) or 0)
            self.meter.output_tokens += int(m.get("output_tokens", 0) or 0)
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
