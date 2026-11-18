"""Typed contracts shared by every agent: specs, results, checks, artifacts and actions."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class Category(str, Enum):
    STRATEGY = "strategy"
    CONTENT = "content"
    WEB = "web"
    EXPERIMENTATION = "experimentation"
    # categories the agent packs add (config/packs/*.yaml: Campaign Studio, ARM SDLC Factory ...)
    DESIGN = "design"
    ENGINEERING = "engineering"
    QUALITY = "quality"
    OPERATIONS = "operations"
    COMPLIANCE = "compliance"
    ACTIVATION = "activation"
    ANALYTICS = "analytics"
    PLATFORM = "platform"


CATEGORY_LABELS = {
    Category.STRATEGY: "Strategy and planning",
    Category.CONTENT: "Content creation",
    Category.WEB: "Website optimization",
    Category.EXPERIMENTATION: "End-to-end testing",
    Category.DESIGN: "Design and UX",
    Category.ENGINEERING: "Engineering and build",
    Category.QUALITY: "Quality and testing",
    Category.OPERATIONS: "Release and operations",
    Category.COMPLIANCE: "Compliance and approvals",
    Category.ACTIVATION: "Activation and send",
    Category.ANALYTICS: "Analytics and measurement",
    Category.PLATFORM: "Platform agents",
}

BLUEPRINT_PACK = "blueprint"   # the 28 agents of the Coforge Independent Blueprint (python classes under caf.agents)


class AgentKind(str, Enum):
    LLM = "llm"                      # language model does the core work (with deterministic guardrails)
    DETERMINISTIC = "deterministic"  # pure code; no model needed
    HYBRID = "hybrid"                # deterministic detection/maths + model for explanation/generation


class Tier(str, Enum):
    NONE = "none"
    FAST = "fast"
    BALANCED = "balanced"
    PRO = "pro"
    COMPLEX = "complex"


class AgentSpec(BaseModel):
    """Static description of an agent. Name, use cases and independence design come from the blueprint."""

    id: str
    name: str
    category: Category
    summary: str
    kind: AgentKind
    tier: Tier = Tier.BALANCED
    connectors: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list, description="Routing hints for the orchestrator")
    produces: list[str] = Field(default_factory=list, description="Artifact / output types")
    feeds: list[str] = Field(default_factory=list, description="Agents that commonly consume this agent's output")
    side_effects: list[str] = Field(default_factory=list, description="Gated publish actions this agent can propose")
    primary_inputs: list[str] | None = Field(default=None, description="Input fields the orchestrator fills with the run goal "
                                             "when a step leaves them empty ([] = never; None = heuristic)")
    version: str = "1.0.0"
    # where the agent comes from: the blueprint, or an agent pack (config/packs/<pack>.yaml)
    pack: str = BLUEPRINT_PACK
    pack_name: str = "Marketing blueprint"
    accelerator: str = Field(default="", description="The CX Nova accelerator or product family the agent belongs to")
    actions: list[dict[str, Any]] = Field(default_factory=list, description="Pack agents: the actions the agent performs "
                                          "[{id, title, produces}]; the step's `action` input picks one")
    runners: list[str] = Field(default_factory=list, description="Pack agents: how a run can be served (remote, model, twin)")
    auto_route: bool = Field(default=True, description="False: the routers and the planner never pick it by themselves; "
                             "it runs when a flow or a caller names it")
    # filled from config/blueprint.yaml
    description: str = ""
    use_cases: list[str] = Field(default_factory=list)
    independence: str = ""
    opal_reference: str = ""
    vendor_requirement_removed: str | None = None

    @property
    def category_label(self) -> str:
        return CATEGORY_LABELS[self.category]


CheckStatus = Literal["pass", "warn", "fail", "info"]
Severity = Literal["low", "medium", "high", "critical"]


class Check(BaseModel):
    """A deterministic finding or validation result (rule engines, linters, QA gates)."""

    rule: str
    status: CheckStatus
    message: str
    severity: Severity = "medium"
    ref: str | None = Field(default=None, description="Standard reference, e.g. 'WCAG 1.1.1 (A)'")
    target: str | None = Field(default=None, description="URL, selector or field the finding applies to")
    data: dict[str, Any] = Field(default_factory=dict)


class ArtifactRef(BaseModel):
    name: str
    mime: str
    path: str
    size: int = 0
    description: str = ""


class PendingAction(BaseModel):
    """A side effect (publish, send, deploy, distribute) proposed by an agent.

    Actions with requires_approval=True are held for a human decision by the orchestrator's
    approval gate (LangGraph interrupt) unless approvals.auto_approve is on.
    """

    id: str
    kind: str                     # e.g. "cms.publish", "esp.export", "newswire.distribute", "flags.deploy"
    connector: str                # connector kind used to perform it
    method: str                   # connector method name
    payload: dict[str, Any] = Field(default_factory=dict)
    summary: str = ""
    requires_approval: bool = True
    mandatory: bool = Field(default=False, description="Always held for a human, even when auto-approve is on")
    status: Literal["pending", "approved", "rejected", "done", "failed"] = "pending"
    result: dict[str, Any] | None = None


RunStatus = Literal["ok", "degraded", "error", "needs_approval"]


class AgentResult(BaseModel):
    agent_id: str
    run_id: str
    status: RunStatus
    summary: str = ""
    output: dict[str, Any] = Field(default_factory=dict)
    checks: list[Check] = Field(default_factory=list)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    pending_actions: list[PendingAction] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("ok", "degraded", "needs_approval")

    def check_counts(self) -> dict[str, int]:
        counts = {"pass": 0, "warn": 0, "fail": 0, "info": 0}
        for c in self.checks:
            counts[c.status] = counts.get(c.status, 0) + 1
        return counts
