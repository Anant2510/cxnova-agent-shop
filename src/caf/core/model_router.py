"""Model router: picks the model tier for each model step from the kind of work it does.

Policy lives in config/model_routing.yaml (reviewed and edited by the team). Resolution for one call:

1. a run-wide override (``tier_override``, e.g. ``caf health --live --fast``) wins outright;
2. a tier pinned on the step in the policy is used as-is (a confirmed decision);
3. otherwise the step's work type gives the tier (or the tier the code asked for, or the agent's default);
4. then the router adjusts: large input steps reasoning work up a tier, a large strict schema lifts fast work to
   balanced, and the profile (economy / balanced / quality) shifts everything one tier down or up.

Every decision carries its reasons, so traces and agent results show which model did what, and why.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

TIERS = ("fast", "balanced", "pro", "complex")
_ENV = re.compile(r"\$\{([A-Za-z0-9_]+)(?::-([^}]*))?\}")


def _tier(value: Any, default: str = "balanced") -> str:
    t = str(getattr(value, "value", value) or "").lower()
    if t == "none":
        return "fast"
    return t if t in TIERS else default


def _shift(tier: str, steps: int) -> str:
    i = max(0, min(len(TIERS) - 1, TIERS.index(tier) + steps))
    return TIERS[i]


@dataclass
class RouteDecision:
    step: str
    tier: str
    work: str | None = None
    source: str = "agent default"
    reasons: list[str] = field(default_factory=list)
    pinned: bool = False
    effort: str | None = None  # how hard the model thinks (Claude): low | medium | high

    def escalated(self) -> "RouteDecision | None":
        """The same step one tier up (for a retry), or None at the top or when pinned."""
        if self.pinned or self.tier == TIERS[-1]:
            return None
        nxt = _shift(self.tier, 1)
        return RouteDecision(self.step, nxt, self.work, self.source, [*self.reasons, f"retry after an invalid answer: {self.tier} -> {nxt}"],
                             effort=self.effort)

    def as_dict(self, model: str | None = None) -> dict[str, Any]:
        out = {"step": self.step, "work": self.work, "tier": self.tier, "effort": self.effort, "why": "; ".join(self.reasons)}
        if model:
            out["model"] = model
        return out


class ModelRouter:
    def __init__(self, policy: dict[str, Any]):
        self.policy = policy or {}
        self.work_types: dict[str, dict[str, Any]] = dict(self.policy.get("work_types") or {})
        self.steps: dict[str, dict[str, Any]] = {k: (v or {}) for k, v in (self.policy.get("steps") or {}).items()}
        self.profile = str(self.policy.get("profile") or "balanced").lower()
        self.large_input = float(self.policy.get("large_input_tokens") or 25000)
        self.strict_fields = int(self.policy.get("strict_schema_fields") or 40)
        self.escalate_on_retry = bool(self.policy.get("escalate_on_retry", True))

    # -------------------------------------------------------------------------------- loading
    @classmethod
    def load(cls, path: Path) -> "ModelRouter":
        import os

        if not path.exists():
            return cls({})
        raw = path.read_text(encoding="utf-8")
        raw = _ENV.sub(lambda m: os.environ.get(m.group(1)) or (m.group(2) or ""), raw)
        return cls(yaml.safe_load(raw) or {})

    # -------------------------------------------------------------------------------- routing
    def decide(self, agent_id: str | None, template: str, *, agent_tier: Any = None, requested: Any = None,
               override: Any = None, prompt_chars: int = 0, schema: Any = None, images: bool = False) -> RouteDecision:
        step = f"{agent_id or 'agent'}.{template}"
        entry = self.steps.get(step) or {}
        work = entry.get("work")
        wt = self.work_types.get(work or "", {})
        effort = entry.get("effort") or wt.get("effort") or None
        if override:
            t = _tier(override)
            return RouteDecision(step, t, work, "run override", [f"run-wide override: {t}"], pinned=True,
                                 effort="low" if t == "fast" else effort)
        if entry.get("tier"):
            t = _tier(entry["tier"])
            return RouteDecision(step, t, work, "pinned", [f"pinned in model_routing.yaml: {t}"], pinned=True, effort=effort)
        if requested and entry.get("follow_request"):  # the agent varies the tier on purpose (e.g. a cross-model test)
            t, source = _tier(requested), "requested by the agent"
            reasons = [f"tier chosen by the agent for this call: {t}"]
        elif wt.get("tier"):
            t, source = _tier(wt["tier"]), "work type"
            reasons = [f"{work} work -> {t}"]
        elif requested:
            t, source = _tier(requested), "requested by the agent"
            reasons = [f"tier requested by the agent: {t}"]
        else:
            t, source = _tier(agent_tier), "agent default"
            reasons = [f"agent default tier: {t}" + ("" if entry else " (step not in model_routing.yaml)")]
        est_tokens = prompt_chars / 3.5
        if wt.get("escalate_on_size") and est_tokens > self.large_input and t != TIERS[-1]:
            new = _shift(t, 1)
            reasons.append(f"large input (~{int(est_tokens):,} tokens): {t} -> {new}")
            t = new
        if t == "fast" and schema is not None and _field_count(schema) > self.strict_fields:
            reasons.append(f"large strict output schema ({_field_count(schema)} fields): fast -> balanced")
            t = "balanced"
        if images and t == "fast":
            reasons.append("image input: fast -> balanced")
            t = "balanced"
        shift = {"economy": -1, "quality": 1}.get(self.profile, 0)
        if shift:
            new = _shift(t, shift)
            if new != t:
                reasons.append(f"{self.profile} profile: {t} -> {new}")
                t = new
        return RouteDecision(step, t, work, source, reasons, effort=effort)

    def table(self, agents: list[Any]) -> list[dict[str, Any]]:
        """Every known model step with its routed tier (inputs of typical size), for review."""
        rows = []
        seen = set()
        for a in agents:
            templates = sorted({k.split(".", 1)[1] for k in self.steps if k.startswith(f"{a.spec.id}.")} | set(getattr(a, "schemas", {}) or {}))
            for tpl in templates:
                d = self.decide(a.spec.id, tpl, agent_tier=a.spec.tier)
                rows.append({"agent": a.spec.id, "step": tpl, "work": d.work or "-", "tier": d.tier, "effort": d.effort or "-",
                             "source": d.source, "why": "; ".join(d.reasons)})
                seen.add(d.step)
        for step in self.steps:
            if step not in seen and step.startswith("orchestrator."):
                agent, tpl = step.split(".", 1)
                d = self.decide(agent, tpl)
                rows.append({"agent": agent, "step": tpl, "work": d.work or "-", "tier": d.tier, "effort": d.effort or "-",
                             "source": d.source, "why": "; ".join(d.reasons)})
        return rows


def _field_count(schema: Any) -> int:
    """Number of property slots in a Pydantic model's JSON Schema (all nested definitions)."""
    try:
        js = schema.model_json_schema() if hasattr(schema, "model_json_schema") else dict(schema)
    except Exception:
        return 0
    text = json.dumps(js)
    return text.count('"type"')


_CACHE: dict[str, tuple[float, ModelRouter]] = {}
_LOCK = threading.Lock()


def get_router(settings: Any) -> ModelRouter:
    """The router for these settings' home (reloaded when model_routing.yaml changes)."""
    path = Path(settings.home) / "config" / "model_routing.yaml"
    key = str(path)
    mtime = path.stat().st_mtime if path.exists() else 0.0
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and hit[0] == mtime:
            return hit[1]
        router = ModelRouter.load(path)
        _CACHE[key] = (mtime, router)
        return router
