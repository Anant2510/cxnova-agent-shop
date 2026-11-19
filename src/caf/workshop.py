"""The workshop: what the catalog can do for a goal, and the agents it lacks, made to order.

Two things happen here.

Assessment. Before a goal runs, the plan is held against the goal: which of its requirements the planned steps
cover, which they cover only in part, and which no agent in the catalog can do. The model does this (one call,
`orchestrator.assess`); without a model the plan is reported as unassessed rather than guessed at.

Made agents. A gap the assessment names (or any purpose someone types) is drafted into an agent: its contract
(inputs, the keys of its result), its skill (system prompt, checklist, standards) and its catalog entry. Once
registered it is a pack agent of the `made` pack (caf.packs), kept in $CAF_DATA_DIR/packs/made.yaml: modelled on
this layer's model (or the customer's, by its policy), contract-checked, listed in every catalog, callable from
REST, MCP, A2A and flows, metered and priced like every other agent, and - unlike the product packs - picked by
the planner for later goals. The data folder survives updates and restarts, so a made agent stays made.
"""
from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from .config import data_dir, get_settings
from .core.spec import CATEGORY_LABELS, CATEGORY_ORDER, Category

MADE_PACK_ID = "made"
MADE_PACK = {"id": MADE_PACK_ID, "name": "Made here", "tag": "MADE", "auto_route": True,
             "description": "Agents made in this layer's workshop to fill gaps the catalog had: the planner picks them like any other.",
             "context": "You are one of the agents of the CX Nova Agent Orchestration Layer. Do the work your skill describes, "
                        "on the request and inputs you are given, and return exactly the shape asked for."}
AGENT_ID = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_WRITE = threading.Lock()


# ------------------------------------------------------------------------------------------------ assessment
class Requirement(BaseModel):
    id: str = Field(description="short id, r1, r2 ...")
    text: str = Field(description="one thing the goal asks for, in the goal's own words")
    status: Literal["covered", "partial", "missing"]
    steps: list[str] = Field(default_factory=list, description="ids of the planned steps that do it")
    note: str = Field(default="", description="why it is partial or missing, or what the steps will produce for it")


class Gap(BaseModel):
    requirement: str = Field(description="the requirement id this gap belongs to")
    agent_id: str = Field(description="a snake_case id for the agent that would fill it")
    agent_name: str
    purpose: str = Field(description="what the agent would do, in one or two sentences")
    category: str = Field(default="content", description="one of the catalog's categories")
    inputs: list[str] = Field(default_factory=list, description="what it needs to be given")
    outputs: list[str] = Field(default_factory=list, description="what it produces")


class Feasibility(BaseModel):
    verdict: Literal["feasible", "partial", "not_feasible", "unassessed"]
    can_do: str = Field(default="", description="what the planned steps will deliver for this goal, in two or three sentences")
    cannot_do: str = Field(default="", description="what the goal asks for that no agent here does, in one or two sentences")
    requirements: list[Requirement] = Field(default_factory=list)
    gaps: list[Gap] = Field(default_factory=list, description="one proposed agent per missing or partial requirement that an agent could fill")
    method: Literal["model", "heuristic"] = "heuristic"
    metrics: dict[str, Any] = Field(default_factory=dict, description="the assessment call's own usage and cost")


def catalog_for_assessor() -> list[dict[str, Any]]:
    from .core.registry import all_agents

    return [{"id": a.spec.id, "name": a.spec.name, "category": a.spec.category_label, "summary": a.spec.summary,
             "planner": bool(a.spec.auto_route)} for a in all_agents()]


def assess(goal: str, steps: list[dict[str, Any]], ctx: Any, *, inputs: dict[str, Any] | None = None) -> Feasibility:
    """Hold the plan against the goal. One model call; without a model, an honest 'unassessed'."""
    from .core.registry import get_agent

    rows = []
    for s in steps:
        try:
            spec = get_agent(s["agent"]).spec
            rows.append({"id": s.get("id"), "agent": s["agent"], "name": spec.name, "summary": spec.summary, "purpose": s.get("purpose") or ""})
        except KeyError:
            rows.append({"id": s.get("id"), "agent": s["agent"], "name": s["agent"], "summary": "", "purpose": s.get("purpose") or ""})
    if ctx.llm.offline or not goal.strip():
        return _heuristic(goal, rows)
    try:
        orch = ctx.for_agent("orchestrator", tier=ctx.settings.orchestrator.get("assess_tier", "pro"))
        feas = orch.generate("assess", Feasibility, goal=goal, steps=rows, agents=catalog_for_assessor(),
                             shared_inputs=sorted((inputs or {}).keys()), categories=[c.value for c in CATEGORY_ORDER])
        feas = _clean(feas, rows)
        feas.method = "model"
        feas.metrics = orch.meter.snapshot()
        return feas
    except Exception as exc:   # an assessment that fails never blocks a plan
        out = _heuristic(goal, rows)
        out.cannot_do = f"Not assessed: the model call failed ({type(exc).__name__}: {str(exc)[:140]})."
        return out


def _heuristic(goal: str, rows: list[dict[str, Any]]) -> Feasibility:
    names = ", ".join(r["name"] for r in rows)
    return Feasibility(verdict="unassessed", method="heuristic",
                       can_do=f"The plan runs {len(rows)} step(s): {names}." if rows else "The plan has no steps.",
                       cannot_do="Not assessed: connect a model to hold the plan against the goal and name what no agent here can do.",
                       requirements=[Requirement(id=f"r{i + 1}", text=r["purpose"] or r["summary"] or r["name"], status="covered", steps=[r["id"]])
                                     for i, r in enumerate(rows)])


def _clean(feas: Feasibility, rows: list[dict[str, Any]]) -> Feasibility:
    from .core.registry import all_agents

    step_ids = {r["id"] for r in rows}
    taken = {a.spec.id for a in all_agents()}
    cats = {c.value for c in Category}
    reqs = []
    for i, r in enumerate(feas.requirements[:30]):
        r.id = r.id or f"r{i + 1}"
        r.steps = [s for s in r.steps if s in step_ids]
        if r.status == "covered" and not r.steps:
            r.status = "partial"
        reqs.append(r)
    gaps = []
    for g in feas.gaps[:12]:
        g.agent_id = snake(g.agent_id or g.agent_name)
        if not g.agent_id or g.agent_id in taken:
            g.agent_id = unique_id(g.agent_id or "made_agent")
        if g.category not in cats:
            g.category = _nearest_category(g.category)
        gaps.append(g)
    statuses = {r.status for r in reqs}
    if feas.verdict not in ("feasible", "partial", "not_feasible"):
        feas.verdict = "feasible" if statuses <= {"covered"} else "not_feasible" if statuses == {"missing"} else "partial"
    if feas.verdict == "feasible" and ("missing" in statuses or gaps):
        feas.verdict = "partial"
    feas.requirements, feas.gaps = reqs, gaps
    return feas


def _nearest_category(text: str) -> str:
    t = (text or "").lower()
    for c, label in CATEGORY_LABELS.items():
        if c.value in t or label.lower() in t:
            return c.value
    return "content"


# ------------------------------------------------------------------------------------------------ drafting an agent
class DraftInput(BaseModel):
    name: str
    type: Literal["text", "number", "boolean", "list", "object"] = "text"
    description: str = ""
    required: bool = False
    example: Any = None


class AgentDraft(BaseModel):
    id: str = Field(description="snake_case, unique in the catalog")
    name: str = Field(description="a short title, e.g. 'Adobe Analytics File Reader'")
    category: str = Field(default="analytics", description="one of the catalog's categories")
    tier: Literal["fast", "balanced", "pro", "complex"] = "balanced"
    summary: str = Field(description="one sentence: what it does and what it produces")
    description: str = Field(default="", description="two or three sentences for the catalog")
    keywords: list[str] = Field(default_factory=list, description="words a goal would use to ask for this")
    inputs: list[DraftInput] = Field(default_factory=list, description="what it must be given, beyond the request text")
    output_keys: list[str] = Field(default_factory=list, description="the keys of its result object")
    produces: list[str] = Field(default_factory=list, description="the kinds of deliverable it hands on (report, table, brief ...)")
    system_prompt: str = Field(description="who the agent is and how it works: its skill, in the second person")
    checklist: list[str] = Field(default_factory=list, description="what it checks its own work against")
    standards: list[str] = Field(default_factory=list)
    use_cases: list[str] = Field(default_factory=list)
    example_request: str = Field(default="", description="a request the agent's self-test can use")


def draft_agent(purpose: str, ctx: Any, *, goal: str = "", name: str = "") -> AgentDraft:
    """Draft an agent for a purpose. One model call; offline, a plain but complete draft from the words given."""
    purpose = purpose.strip()
    if not purpose:
        raise ValueError("Say what the agent should do")
    if ctx.llm.offline:
        draft = _plain_draft(purpose, name)
    else:
        orch = ctx.for_agent("orchestrator", tier=ctx.settings.orchestrator.get("draft_tier", "pro"))
        draft = orch.generate("draft_agent", AgentDraft, purpose=purpose, goal=goal, name=name, categories=[c.value for c in CATEGORY_ORDER],
                              category_labels={c.value: CATEGORY_LABELS[c] for c in CATEGORY_ORDER},
                              existing=[a["name"] for a in catalog_for_assessor()])
    return finish_draft(draft)


def finish_draft(draft: AgentDraft, *, uniquify: bool = True) -> AgentDraft:
    draft.id = snake(draft.id or draft.name) or "made_agent"
    if uniquify:   # a drafted id that is taken gets a suffix; an id someone chose to register is theirs to change
        draft.id = unique_id(draft.id)
    if draft.category not in {c.value for c in Category}:
        draft.category = _nearest_category(draft.category)
    draft.inputs = [i for i in draft.inputs if snake(i.name)][:12]
    for i in draft.inputs:
        i.name = snake(i.name)
    if not draft.output_keys:
        draft.output_keys = ["findings", "recommendations"]
    draft.output_keys = [snake(k) for k in draft.output_keys if snake(k)][:16]
    if not draft.example_request:
        draft.example_request = f"Self-test: {draft.summary}"
    return draft


def _plain_draft(purpose: str, name: str = "") -> AgentDraft:
    words = [w for w in re.findall(r"[a-zA-Z]+", purpose) if len(w) > 2][:6]
    title = name.strip() or " ".join(w.capitalize() for w in words[:4]) or "Made Agent"
    return AgentDraft(id=snake(title), name=title, category=_nearest_category(purpose), summary=purpose[:200],
                      description=purpose, keywords=[w.lower() for w in words],
                      inputs=[DraftInput(name="source", type="text", description="The material to work on: a file's contents, a URL or pasted text", required=False)],
                      output_keys=["findings", "recommendations"], produces=["report"],
                      system_prompt=f"You are {title}. {purpose} Work only from what you are given; say what is missing.",
                      checklist=["Every finding names the evidence it rests on", "Recommendations are specific and actionable",
                                 "Nothing is invented: a missing input is reported, not guessed"],
                      use_cases=[purpose[:120]], example_request=purpose[:160])


# ------------------------------------------------------------------------------------------------ the made pack on disk
def made_path(settings: Any = None) -> Path:
    return data_dir(settings or get_settings()) / "packs" / f"{MADE_PACK_ID}.yaml"


def _read(settings: Any = None) -> dict[str, Any]:
    path = made_path(settings)
    if not path.exists():
        return {"pack": dict(MADE_PACK), "agents": [], "flows": []}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw.setdefault("pack", dict(MADE_PACK))
    raw.setdefault("agents", [])
    return raw


def _write(raw: dict[str, Any], settings: Any = None) -> None:
    path = made_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True, width=120), encoding="utf-8")
    os.replace(tmp, path)


def _refresh() -> None:
    from .core.registry import reload_agents
    from .packs import reset_packs

    reset_packs()
    reload_agents()


def list_made() -> list[dict[str, Any]]:
    return [dict(a) for a in _read().get("agents") or []]


def to_pack_agent(draft: AgentDraft) -> dict[str, Any]:
    example = {"summary": f"{draft.name}: {draft.summary}", "result": {k: "" for k in draft.output_keys}, "artifacts": [], "checks": [], "human_followups": []}
    return {"id": draft.id, "name": draft.name, "category": draft.category, "tier": draft.tier, "summary": draft.summary,
            "description": draft.description or draft.summary, "keywords": draft.keywords[:20],
            "inputs": [i.model_dump() for i in draft.inputs],
            "actions": [{"id": "run", "title": draft.name, "produces": draft.produces[:8], "output_keys": draft.output_keys, "example": example}],
            "skill": {"system_prompt": draft.system_prompt, "checklist": draft.checklist[:12], "standards": draft.standards[:8]},
            "use_cases": draft.use_cases[:8]}


def register_made(draft: AgentDraft | dict[str, Any]) -> dict[str, Any]:
    """Validate the draft, keep it in the made pack, load it: it is an agent of the catalog from here on."""
    from .core.registry import get_agent, load_agents
    from .packs.model import PackAgent

    d = finish_draft(draft if isinstance(draft, AgentDraft) else AgentDraft(**draft), uniquify=False)
    if not AGENT_ID.match(d.id):
        raise ValueError(f"agent id '{d.id}': lowercase letters, digits and '_'")
    load_agents()
    if _exists(d.id):
        raise ValueError(f"an agent called '{d.id}' already exists: pick another id")
    entry = to_pack_agent(d)
    PackAgent(**entry)   # the pack's own validation, before anything is written
    with _WRITE:
        raw = _read()
        raw["agents"] = [a for a in raw["agents"] if a.get("id") != d.id] + [entry]
        _write(raw)
        _refresh()
    return get_agent(d.id).describe()


def remove_made(agent_id: str) -> bool:
    with _WRITE:
        raw = _read()
        before = len(raw["agents"])
        raw["agents"] = [a for a in raw["agents"] if a.get("id") != agent_id]
        if len(raw["agents"]) == before:
            return False
        _write(raw)
        _refresh()
    return True


def _exists(agent_id: str) -> bool:
    from .core.registry import get_agent

    try:
        get_agent(agent_id)
        return True
    except KeyError:
        return False


def snake(text: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", (text or "").strip().lower())).strip("_")[:64]


def unique_id(base: str) -> str:
    base = snake(base) or "made_agent"
    if not base[0].isalpha():
        base = "a_" + base
    if not _exists(base):
        return base
    n = 2
    while _exists(f"{base}_{n}"):
        n += 1
    return f"{base}_{n}"
