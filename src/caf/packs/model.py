"""Agent packs: agents of other CX Nova products, declared in config/packs/<pack>.yaml.

A pack brings a product's agents into this layer's catalog without rewriting them as Python classes: every agent is
a declaration (who it is, what it takes, the actions it performs with the shape each returns, and its skill: the
system prompt, checklist and standards it works by). The layer builds a contract-checked agent from each one, so a
pack agent can be put in a flow, run from the console, REST, MCP and A2A, rented and metered like any other.

A pack agent's run is served by the first runner that can take it:

    home    the agent inside the product it comes from (pack.home: its URL and passcode from the environment),
            when the step carries what that product needs (Campaign Studio: a campaign id)
    model   the agent modelled on this layer's model (or the customer's, by its policy) from the pack's skill
    twin    the sample output of the pack's contract: no model, no cost; what an offline run and a self-test use

Large contracts (the sample output per action) can live beside the pack as <pack>.contracts.json:
{"<agent id>.<action id>": {"output_keys": [...], "artifacts": [...], "example": {...}}}.
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

from ..config import get_settings

AGENT_ID = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


class PackInput(BaseModel):
    name: str
    type: str = Field(default="text", description="text | number | boolean | list | object")
    description: str = ""
    required: bool = False
    example: Any = None
    home: str = Field(default="", description="the field's name in the home app's API (campaign_id -> campaignId)")


class PackAction(BaseModel):
    id: str
    title: str = ""
    produces: list[str] = Field(default_factory=list, description="artifact types the action hands on")
    output_keys: list[str] = Field(default_factory=list, description="keys of the action's result")
    artifacts: list[dict[str, Any]] = Field(default_factory=list, description="[{type, required, keys}] from the home contract")
    example: dict[str, Any] | None = Field(default=None, description="a sample result: the twin's answer and the shape shown to the model")
    needs_home: bool = Field(default=False, description="only the home product can really do it (publish, send, deploy); here it is a dry run")


class PackSkill(BaseModel):
    system_prompt: str = ""
    checklist: list[str] = Field(default_factory=list)
    standards: list[str] = Field(default_factory=list)


class PackAgent(BaseModel):
    id: str
    home_id: str = Field(default="", description="the agent's id in the product it comes from")
    name: str
    accelerator: str = ""
    family: str = ""
    category: str = "content"
    tier: str = "balanced"
    summary: str
    description: str = ""
    keywords: list[str] = Field(default_factory=list)
    inputs: list[PackInput] = Field(default_factory=list)
    actions: list[PackAction] = Field(min_length=1)
    skill: PackSkill = Field(default_factory=PackSkill)
    needs_home: bool = False
    guardrails: list[str] = Field(default_factory=list)
    feeds: list[str] = Field(default_factory=list)
    use_cases: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not AGENT_ID.match(v):
            raise ValueError(f"pack agent id '{v}': lowercase letters, digits and '_'")
        return v

    def action(self, action_id: str | None) -> PackAction:
        if action_id:
            for a in self.actions:
                if a.id == action_id:
                    return a
            raise KeyError(f"{self.name} has no action '{action_id}'. Actions: {', '.join(a.id for a in self.actions)}")
        return self.actions[0]


class PackHome(BaseModel):
    """The product the pack's agents live in. Connected when the URL is in the environment."""

    name: str = ""
    kind: str = Field(default="", description="the adapter that speaks the product's API: campaign_studio")
    url_env: str = ""
    passcode_env: str = ""
    needs: str = Field(default="", description="the input a step must carry for the home product to serve it (campaign_id)")
    note: str = ""

    @property
    def url(self) -> str:
        return (os.environ.get(self.url_env) or "").rstrip("/") if self.url_env else ""

    @property
    def passcode(self) -> str:
        return os.environ.get(self.passcode_env) or "" if self.passcode_env else ""

    @property
    def connected(self) -> bool:
        return bool(self.url and self.kind)


class PackFlowStep(BaseModel):
    id: str
    agent: str
    purpose: str = ""
    inputs: dict[str, Any] = Field(default_factory=dict)
    needs: list[str] = Field(default_factory=list)


class PackFlow(BaseModel):
    id: str
    name: str
    description: str = ""
    inputs: list[dict[str, Any]] = Field(default_factory=list)
    steps: list[PackFlowStep] = Field(min_length=1)
    triggers: list[str] = Field(default_factory=list)
    stage: str = ""


class Pack(BaseModel):
    id: str
    name: str
    product: str = ""
    tag: str = Field(default="", description="two to four letters shown next to the pack's agents in the catalog (CS, ARM ...)")
    description: str = ""
    color: str = ""
    source: str = Field(default="", description="where the pack was generated from")
    context: str = Field(default="", description="added to every agent's system prompt: what the product is, its ground rules")
    auto_route: bool = Field(default=False, description="True: the routers and the planner may pick the pack's agents by themselves "
                                                        "(the made pack); product packs run when a caller or a flow names them")
    home: PackHome | None = None
    agents: list[PackAgent] = Field(default_factory=list)
    flows: list[PackFlow] = Field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        home = self.home
        return {"id": self.id, "name": self.name, "product": self.product, "tag": self.tag or "".join(w[0] for w in self.name.split()).upper()[:4],
                "description": self.description, "color": self.color,
                "agents": len(self.agents), "flows": len(self.flows), "accelerators": sorted({a.accelerator for a in self.agents if a.accelerator}),
                "home": ({"name": home.name, "kind": home.kind, "connected": home.connected, "needs": home.needs, "note": home.note,
                          "url_env": home.url_env} if home else None)}


def packs_dir(settings: Any = None) -> Path:
    return Path((settings or get_settings()).home) / "config" / "packs"


def made_packs_dir(settings: Any = None) -> Path:
    """Packs written at run time (the workshop's made agents): in the data folder, so they outlive updates."""
    from ..config import data_dir

    return data_dir(settings or get_settings()) / "packs"


def _load_pack(path: Path) -> Pack:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    head = dict(raw.get("pack") or {})
    contracts_path = path.with_name(path.stem + ".contracts.json")
    contracts = json.loads(contracts_path.read_text(encoding="utf-8")) if contracts_path.exists() else {}
    agents = []
    for a in raw.get("agents") or []:
        a = dict(a)
        actions = []
        for act in a.get("actions") or []:
            act = {"id": act} if isinstance(act, str) else dict(act)
            extra = contracts.get(f"{a.get('id')}.{act['id']}") or {}
            for key in ("output_keys", "artifacts", "example", "produces", "title"):
                if key in extra and not act.get(key):
                    act[key] = extra[key]
            actions.append(act)
        a["actions"] = actions
        agents.append(a)
    return Pack(**head, agents=agents, flows=raw.get("flows") or [])


@lru_cache(maxsize=1)
def load_packs() -> dict[str, Pack]:
    """Every pack under config/packs, by id, in file-name order. A pack that does not parse stops the start-up:
    a catalog that silently lost a product's agents would be worse."""
    packs: dict[str, Pack] = {}
    disabled = {p.strip() for p in (os.environ.get("CAF_PACKS_DISABLED") or "").split(",") if p.strip()}
    for folder, products in ((packs_dir(), True), (made_packs_dir(), False)):   # the products' packs, then the ones made here
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.yaml")):
            pack = _load_pack(path)
            if pack.id in disabled or ("*" in disabled and products):   # '*' switches the product packs off; made agents stay
                continue
            if pack.id in packs:
                raise ValueError(f"pack id '{pack.id}' is defined twice ({path.name})")
            packs[pack.id] = pack
    return packs


def pack_of_agent(agent_id: str) -> tuple[Pack, PackAgent] | None:
    for pack in load_packs().values():
        for agent in pack.agents:
            if agent.id == agent_id:
                return pack, agent
    return None
