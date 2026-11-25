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

What an agent's numbers rest on is declared per action (`basis`), in the pack or in config/quality/<pack>.yaml (for a pack
generated from another product's configuration, so a regeneration does not lose it):

    calculated  the agent's own code computes it from the data given: the same input gives the same output
    authored    the model writes, plans or reviews from what it is given; a count is a count of what it wrote
    judgement   the model's assessment or estimate (a synthetic persona's score, an unsourced forecast): labelled as one
    measured    figures that are observations of a system (analytics, test results, campaign outcomes): only the product that
                measures, the agent's code or the inputs can supply them; a model is told to report none it was not given
    executed    the action runs or changes something (a test run, a scan, a publish, a send): modelled, it is a dry run

See caf.quality for the audit that holds every pack to this, and docs/AGENT_QUALITY.md.
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


BASES = ("calculated", "authored", "judgement", "measured", "executed")


class PackInput(BaseModel):
    name: str
    type: str = Field(default="text", description="text | number | boolean | list | object | any")
    description: str = ""
    required: bool = False
    example: Any = None
    home: str = Field(default="", description="the field's name in the home app's API (campaign_id -> campaignId)")
    file: list[str] = Field(default_factory=list, description="the formats this input can be uploaded in (json, csv, ndjson, txt): forms show "
                                                             "a file picker, and text in one of them is parsed into records before the agent sees it")
    choices: list[str] = Field(default_factory=list, description="the values the input takes (forms show a select; anything else is refused)")
    actions: list[str] = Field(default_factory=list, description="the actions this input is for (forms show it only for those); empty: every action")
    fields: dict[str, Any] = Field(default_factory=dict, description="for records: {required: [names], any_of: [[names]], recommended: [names]}; "
                                                                      "a name may be 'a|b|c' (any of these spellings)")
    max_chars: int = Field(default=6000, description="how much of this input reaches the model: longer lists are cut by whole records, and the run says so")


class PackAction(BaseModel):
    id: str
    title: str = ""
    produces: list[str] = Field(default_factory=list, description="artifact types the action hands on")
    output_keys: list[str] = Field(default_factory=list, description="keys of the action's result")
    artifacts: list[dict[str, Any]] = Field(default_factory=list, description="[{type, required, keys}] from the home contract")
    example: dict[str, Any] | None = Field(default=None, description="a sample result: the twin's answer and the shape shown to the model")
    needs_home: bool = Field(default=False, description="only the home product can really do it (publish, send, deploy); here it is a dry run")
    basis: str = Field(default="", description="what the action's figures rest on: calculated | authored | judgement | measured | executed "
                                               "(see the module's notes); empty: not classified yet, treated as authored and reported by the audit")
    evidence: list[str] = Field(default_factory=list, description="for a measured action: the inputs that carry the measurements when the caller has them")

    @field_validator("basis")
    @classmethod
    def _basis(cls, v: str) -> str:
        if v and v not in BASES:
            raise ValueError(f"basis '{v}': one of {', '.join(BASES)}")
        return v

    @property
    def grounds(self) -> str:
        """The basis in force: an action only its platform can perform is executed whatever else is declared."""
        return "executed" if self.needs_home else (self.basis or "authored")


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
    code: str = Field(default="", description="module:function of a coded runner (an accelerator's own logic): called first, with the "
                                              "action id, the inputs and the context; the model and the twin stand in if it raises")
    code_only: bool = Field(default=False, description="True: nothing stands in for the agent's code. When it cannot do the work the step fails "
                                                       "and says why: a calculation is never replaced by a model's guess or a sample")
    verify: str = Field(default="", description="module:function called after a model or the twin served: (action id, inputs, served, ctx) may "
                                                "add checks and correct what arithmetic decides (a cost against a guardrail, a length against a limit)")
    minimum: list[Any] = Field(default_factory=list, description="the least the agent needs to do real work when it runs on its own: input names; "
                                                                 "a list inside the list means any one of them. In a flow an earlier step's work can stand for it")
    capabilities: list[dict[str, Any]] = Field(default_factory=list, description="what the agent can and cannot do, for its page: [{id, name, predicts|does, needs}]")
    selftest: dict[str, Any] = Field(default_factory=dict, description="inputs the agent's self-test runs on (a bundled sample), so a check of the "
                                                                       "agent exercises its real work and not only its contract")

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


def quality_dir(settings: Any = None) -> Path:
    return Path((settings or get_settings()).home) / "config" / "quality"


def _quality(pack_id: str) -> dict[str, Any]:
    """config/quality/<pack>.yaml: {basis: {<basis>: ["agent.action" | "agent.*", ...]}, evidence: {"agent.action": [inputs]},
    minimum: {agent: [inputs]}, selftest: {agent: {input: sample}}, inputs: {agent or "*": [input]}}; under evidence, "*"
    names the inputs every measured action reads. For packs generated from another product's configuration; a hand-written pack declares these itself."""
    path = quality_dir() / f"{pack_id}.yaml"
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    unknown = set(raw.get("basis") or {}) - set(BASES)
    if unknown:
        raise ValueError(f"{path.name}: basis {sorted(unknown)} is not one of {', '.join(BASES)}")
    return raw


def _load_pack(path: Path) -> Pack:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    head = dict(raw.get("pack") or {})
    contracts_path = path.with_name(path.stem + ".contracts.json")
    contracts = json.loads(contracts_path.read_text(encoding="utf-8")) if contracts_path.exists() else {}
    quality = _quality(str(head.get("id") or path.stem))
    basis_of: dict[str, str] = {ref: basis for basis, refs in (quality.get("basis") or {}).items() for ref in refs or []}
    seen: set[str] = set()
    agents = []
    for a in raw.get("agents") or []:
        a = dict(a)
        actions = []
        for act in a.get("actions") or []:
            act = {"id": act} if isinstance(act, str) else dict(act)
            ref = f"{a.get('id')}.{act['id']}"
            extra = contracts.get(ref) or {}
            for key in ("output_keys", "artifacts", "example", "produces", "title"):
                if key in extra and not act.get(key):
                    act[key] = extra[key]
            for key in (ref, f"{a.get('id')}.*"):   # the action's own line wins over the agent's wildcard
                if key in basis_of:
                    seen.add(key)
                    if not act.get("basis"):
                        act["basis"] = basis_of[key]
            evidence_of = quality.get("evidence") or {}
            if not act.get("evidence"):   # its own line, else the pack's line for every measured action
                if ref in evidence_of:
                    act["evidence"] = evidence_of[ref]
                elif "*" in evidence_of and act.get("basis") == "measured":
                    act["evidence"] = evidence_of["*"]
            actions.append(act)
        a["actions"] = actions
        added = quality.get("inputs") or {}   # inputs a generated pack does not declare: the pack's ("*"), then the agent's own
        have = {i.get("name") for i in a.get("inputs") or []}
        extra_inputs = [dict(i) for key in ("*", a.get("id")) for i in added.get(key) or [] if i.get("name") not in have]
        if extra_inputs:
            a["inputs"] = list(a.get("inputs") or []) + extra_inputs
        for key in ("minimum", "selftest"):   # the least the agent runs on, and the sample its self-test runs on
            if a.get("id") in (quality.get(key) or {}) and not a.get(key):
                a[key] = quality[key][a["id"]]
        agents.append(a)
    ids = {a.get("id") for a in agents}
    lost = sorted({str(n) for key in ("minimum", "selftest", "inputs") for n in (quality.get(key) or {}) if n != "*"} - ids)
    if lost:
        raise ValueError(f"config/quality/{head.get('id') or path.stem}.yaml names agents the pack does not have: {', '.join(lost[:8])}")
    stale = sorted(set(basis_of) - seen)
    if stale:   # a line that names nothing is a classification someone believes is in force
        raise ValueError(f"config/quality/{head.get('id') or path.stem}.yaml names actions the pack does not have: {', '.join(stale[:8])}")
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
