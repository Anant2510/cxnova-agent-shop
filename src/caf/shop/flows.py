"""Orchestration flows: multi-agent workflows the operator composes, publishes and rents out.

A flow is a named, versioned plan: agents as steps, each step with the steps it needs (so independent steps run in
parallel), optional input bindings, and the shared inputs the flow expects. The eight built-in playbooks
(config/playbooks.yaml) are flows too (source "builtin"): they can be rented as they are, or cloned into an
editable custom flow. Custom flows live in the shop's database.

A published flow is a product: it appears on the storefront, can be subscribed to, and a customer who rents it gets
it as an MCP tool (run_flow_<id>), a REST endpoint (POST /flows/<id>/run) and an A2A skill. Renting a flow entitles
the customer to run the flow as a whole, not to call its agents one by one; those are rented separately.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field, field_validator

from ..core import get_agent
from ..orchestrator import load_playbooks
from ..orchestrator.router import MAX_STEPS, Plan, PlanStep

FLOW_ID = re.compile(r"^[a-z][a-z0-9_]{1,47}$")
STEP_ID = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


class FlowInput(BaseModel):
    name: str
    description: str = ""
    required: bool = False
    example: str = ""

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not re.match(r"^[a-z][a-z0-9_]{0,47}$", v):
            raise ValueError(f"input name '{v}': lowercase letters, digits and '_' (e.g. url, audience)")
        return v


class FlowStep(BaseModel):
    id: str
    agent: str
    purpose: str = ""
    inputs: dict[str, Any] = Field(default_factory=dict, description="agent fields; may use $goal, ${input.<name>}, ${steps.<id>.output.<field>}")
    needs: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        v = v.strip().lower()
        if not STEP_ID.match(v):
            raise ValueError(f"step id '{v}': lowercase letters, digits and '_' (up to 32 characters)")
        return v


class Flow(BaseModel):
    id: str
    name: str
    description: str = ""
    inputs: list[FlowInput] = Field(default_factory=list, description="shared inputs the flow expects (url, audience, ...)")
    steps: list[FlowStep] = Field(min_length=1)
    triggers: list[str] = Field(default_factory=list, description="phrases the deterministic router may match")
    status: str = Field(default="draft", description="draft | published")
    source: str = Field(default="custom", description="custom (editable, in the shop's database) | builtin (config/playbooks.yaml)")
    version: int = 1
    price: dict[str, Any] | None = Field(default=None, description="monthly, included_runs, per_run; shop defaults when empty")
    pack: str = Field(default="", description="built-in flows: the agent pack that ships it (a product's own process)")
    pack_name: str = ""
    layout: dict[str, dict[str, float]] = Field(default_factory=dict, description="the designer's canvas: {step id: {x, y}}")
    created_at: str | None = None
    updated_at: str | None = None
    published_at: str | None = None

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        v = v.strip().lower()
        if not FLOW_ID.match(v):
            raise ValueError(f"flow id '{v}': lowercase letters, digits and '_' (2 to 48 characters, e.g. launch_kit_uk)")
        return v

    @field_validator("status")
    @classmethod
    def _status(cls, v: str) -> str:
        if v not in ("draft", "published"):
            raise ValueError("status must be draft or published")
        return v

    # ---- derived -----------------------------------------------------------------------------
    @property
    def published(self) -> bool:
        return self.status == "published"

    def agents(self) -> list[str]:
        out: list[str] = []
        for s in self.steps:
            if s.agent not in out:
                out.append(s.agent)
        return out

    def stages(self) -> list[list[str]]:
        """Step ids grouped by the earliest stage they can run in (parallel within a stage)."""
        level: dict[str, int] = {}
        for s in self.steps:  # steps are kept in dependency order by validate_flow
            level[s.id] = 1 + max([level.get(n, 0) for n in s.needs] or [0])
        out: dict[int, list[str]] = {}
        for s in self.steps:
            out.setdefault(level[s.id], []).append(s.id)
        return [out[k] for k in sorted(out)]

    def plan(self) -> Plan:
        return Plan(mode="playbook", playbook=self.id, reason=self.description or self.name,
                    steps=[PlanStep(id=s.id, agent=s.agent, purpose=s.purpose, inputs=dict(s.inputs), needs=list(s.needs)) for s in self.steps])

    def listing(self) -> dict[str, Any]:
        """The flow as the storefront, the API and MCP describe it."""
        return {"id": self.id, "name": self.name, "description": self.description, "status": self.status, "source": self.source,
                "version": self.version, "inputs": [i.model_dump() for i in self.inputs], "agents": self.agents(),
                "steps": [s.model_dump() for s in self.steps], "stages": self.stages(), "triggers": self.triggers,
                "tool": f"run_flow_{self.id}", "updated_at": self.updated_at, "published_at": self.published_at,
                "pack": self.pack, "pack_name": self.pack_name, "layout": self.layout}


def validate_flow(flow: Flow) -> list[str]:
    """Problems that stop a flow from being saved (returned, not raised, so the builder can show them all)."""
    problems: list[str] = []
    if len(flow.steps) > MAX_STEPS:
        problems.append(f"a flow has at most {MAX_STEPS} steps")
    seen: set[str] = set()
    for s in flow.steps:
        if s.id in seen:
            problems.append(f"step id '{s.id}' is used twice")
        seen.add(s.id)
        try:
            agent = get_agent(s.agent)
        except KeyError:
            problems.append(f"step '{s.id}': unknown agent '{s.agent}'")
            continue
        unknown = [k for k in s.inputs if k not in agent.Input.model_fields]
        if unknown:
            problems.append(f"step '{s.id}': {agent.spec.name} has no input(s) {unknown}")
        for n in s.needs:
            if n == s.id:
                problems.append(f"step '{s.id}' cannot need itself")
            elif n not in seen:
                problems.append(f"step '{s.id}' needs '{n}', which is not an earlier step (order the steps so dependencies come first)")
    names = [i.name for i in flow.inputs]
    if len(names) != len(set(names)):
        problems.append("an input name is used twice")
    return problems


def flow_from_playbook(pb: Any) -> Flow:
    defs = [d for d in (getattr(pb, "input_defs", None) or []) if d.get("name")]
    inputs = [FlowInput(name=d["name"], description=str(d.get("description") or ""), required=bool(d.get("required")),
                        example=str(d.get("example") or "")) for d in defs] or [FlowInput(name=n) for n in pb.inputs]
    return Flow(id=pb.id, name=pb.name, description=pb.description, triggers=list(pb.triggers),
                inputs=inputs, status="published", source="builtin", pack=getattr(pb, "pack", "") or "", pack_name=getattr(pb, "pack_name", "") or "",
                steps=[FlowStep(id=s.id, agent=s.agent, purpose=s.purpose, inputs=dict(s.inputs), needs=list(s.needs)) for s in pb.steps])


def builtin_flows() -> dict[str, Flow]:
    return {pid: flow_from_playbook(pb) for pid, pb in load_playbooks().items()}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
