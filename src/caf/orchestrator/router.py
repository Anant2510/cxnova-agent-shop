"""Routing and planning.

1. Explicit choice wins (agent or playbook named by the caller).
2. With a model available, the planner (config/prompts/orchestrator/plan.j2) designs the
   workflow from the live agent catalog and playbooks; the plan is validated deterministically.
3. Without a model (or if the plan fails validation) a deterministic TF-IDF/keyword router picks
   the best playbook or single agent.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..core import blueprint_agents, get_agent
from ..core.textutil import tfidf_similarity, tokens
from .playbooks import Playbook, StepDef, load_playbooks

MAX_STEPS = 64          # a flow or a plan a caller supplies (a product's whole process fits)
PLANNER_MAX_STEPS = 12  # what the model planner is asked to stay within


class PlanStep(BaseModel):
    id: str = Field(description="short snake_case step id, unique in the plan")
    agent: str = Field(description="agent id from the catalog")
    purpose: str = Field(default="", description="why this step is in the plan")
    inputs: dict[str, Any] = Field(default_factory=dict, description="agent input fields; may use ${steps.<id>.output.<field>} or $goal")
    needs: list[str] = Field(default_factory=list, description="ids of earlier steps this step depends on")


class Plan(BaseModel):
    mode: Literal["agent", "playbook", "custom"]
    playbook: str | None = None
    reason: str = ""
    steps: list[PlanStep] = Field(min_length=1)


class RouteDecision(BaseModel):
    mode: Literal["agent", "playbook", "custom"]
    agent: str | None = None
    playbook: str | None = None
    confidence: float = 0.0
    reason: str = ""
    method: Literal["explicit", "planner", "router"] = "router"
    alternatives: list[dict[str, Any]] = Field(default_factory=list)
    planner_metrics: dict[str, Any] | None = None   # the planning call's own usage and cost, when the model planned
    feasibility: dict[str, Any] | None = None       # the assessment the caller previewed (what the plan covers of the goal, and what not)


def _agent_doc(a: Any) -> str:
    s = a.spec
    return " ".join([s.name, s.summary, " ".join(s.keywords) * 2, " ".join(s.use_cases)])


def _playbook_doc(p: Playbook) -> str:
    return " ".join([p.name, p.description, " ".join(p.triggers) * 3])


def score_candidates(goal: str) -> tuple[list[tuple[float, str]], list[tuple[float, str]]]:
    agents = blueprint_agents()   # the routers choose among the blueprint's agents; pack agents run when named
    books = [p for p in load_playbooks().values() if p.auto_route]
    a_scores = tfidf_similarity(goal, [_agent_doc(a) for a in agents]) if agents else []
    p_scores = tfidf_similarity(goal, [_playbook_doc(p) for p in books]) if books else []
    g = goal.lower()
    goal_tokens = set(tokens(goal))
    ranked_agents = []
    for a, s in zip(agents, a_scores):
        kw_hits = sum(1 for k in a.spec.keywords if k.lower() in g)
        name_hit = 1 if a.spec.name.lower() in g or a.spec.id.replace("_", " ") in g else 0
        ranked_agents.append((s + 0.25 * kw_hits + 0.6 * name_hit, a.spec.id))
    ranked_books = []
    for p, s in zip(books, p_scores):
        trig = sum(1 for t in p.triggers if t.lower() in g)
        multi = 0.15 if len(goal_tokens) > 6 and any(w in g for w in (" and ", " then ", ",", "end-to-end", "full", "kit", "programme", "program")) else 0
        ranked_books.append((s + 0.3 * trig + multi, p.id))
    ranked_agents.sort(reverse=True)
    ranked_books.sort(reverse=True)
    return ranked_agents, ranked_books


def deterministic_route(goal: str) -> RouteDecision:
    agents, books = score_candidates(goal)
    best_a = agents[0] if agents else (0.0, None)
    best_p = books[0] if books else (0.0, None)
    alts = [{"agent": a, "score": round(s, 3)} for s, a in agents[:3]] + [{"playbook": p, "score": round(s, 3)} for s, p in books[:2]]
    if best_p[1] and best_p[0] >= max(0.35, best_a[0] * 1.05):
        return RouteDecision(mode="playbook", playbook=best_p[1], confidence=round(min(best_p[0], 1.0), 3),
                             reason=f"Goal matches the '{best_p[1]}' playbook", alternatives=alts)
    if best_a[1]:
        return RouteDecision(mode="agent", agent=best_a[1], confidence=round(min(best_a[0], 1.0), 3),
                             reason=f"Goal matches the '{best_a[1]}' agent", alternatives=alts)
    raise ValueError("No agents registered")


def plan_from_playbook(pb: Playbook) -> Plan:
    return Plan(mode="playbook", playbook=pb.id, reason=pb.description,
                steps=[PlanStep(**s.model_dump()) for s in pb.steps])


def plan_for_agent(agent_id: str, inputs: dict[str, Any] | None = None) -> Plan:
    agent = get_agent(agent_id)
    return Plan(mode="agent", reason=f"Single agent: {agent.spec.name}",
                steps=[PlanStep(id=agent_id, agent=agent_id, purpose=agent.spec.summary, inputs=dict(inputs or {}))])


def validate_plan(plan: Plan) -> tuple[Plan, list[str]]:
    """Drop unknown agents/fields, fix ids and dependencies; return the cleaned plan and warnings."""
    warnings: list[str] = []
    seen: set[str] = set()
    steps: list[PlanStep] = []
    for raw in plan.steps[:MAX_STEPS]:
        try:
            agent = get_agent(raw.agent)
        except KeyError:
            warnings.append(f"Dropped step '{raw.id}': unknown agent '{raw.agent}'")
            continue
        sid = re.sub(r"[^a-z0-9_]+", "_", (raw.id or raw.agent).lower()).strip("_") or raw.agent
        base, n = sid, 2
        while sid in seen:
            sid = f"{base}_{n}"
            n += 1
        fields = agent.Input.model_fields
        unknown = [k for k in raw.inputs if k not in fields]
        if unknown:
            warnings.append(f"Step '{sid}': ignored unknown inputs {unknown}")
        needs = [d for d in raw.needs if d in seen]
        if len(needs) != len(raw.needs):
            warnings.append(f"Step '{sid}': dropped dependencies on unknown/later steps {sorted(set(raw.needs) - set(needs))}")
        steps.append(PlanStep(id=sid, agent=raw.agent, purpose=raw.purpose,
                              inputs={k: v for k, v in raw.inputs.items() if k in fields}, needs=needs))
        seen.add(sid)
    if len(plan.steps) > MAX_STEPS:
        warnings.append(f"Plan truncated to {MAX_STEPS} steps")
    if not steps:
        raise ValueError("Plan has no valid steps")
    return plan.model_copy(update={"steps": steps}), warnings


def catalog_for_planner() -> list[dict[str, Any]]:
    out = []
    for a in blueprint_agents():
        fields = []
        for name, f in a.Input.model_fields.items():
            req = "required" if f.is_required() else "optional"
            fields.append(f"{name} ({req}): {(f.description or '')[:90]}")
        out.append({"id": a.spec.id, "name": a.spec.name, "category": a.spec.category_label, "summary": a.spec.summary,
                    "inputs": fields, "feeds": a.spec.feeds})
    return out


def playbooks_for_planner() -> list[dict[str, Any]]:
    return [{"id": p.id, "name": p.name, "description": p.description,
             "steps": [f"{s.id}: {s.agent}" + (f" (needs {', '.join(s.needs)})" if s.needs else "") for s in p.steps]}
            for p in load_playbooks().values() if p.auto_route]


def route_and_plan(goal: str, ctx: Any, *, agent: str | None = None, playbook: str | None = None,
                   plan: dict[str, Any] | None = None, inputs: dict[str, Any] | None = None,
                   use_planner: bool = True) -> tuple[RouteDecision, Plan, list[str]]:
    warnings: list[str] = []
    books = load_playbooks()
    if plan:
        p, w = validate_plan(Plan.model_validate(plan))
        return RouteDecision(mode=p.mode, playbook=p.playbook, method="explicit", confidence=1.0, reason="Plan supplied by caller"), p, w
    if playbook:
        if playbook not in books:
            raise KeyError(f"Unknown playbook '{playbook}'. Known: {sorted(books)}")
        p, w = validate_plan(plan_from_playbook(books[playbook]))
        return (RouteDecision(mode="playbook", playbook=playbook, method="explicit", confidence=1.0, reason="Playbook chosen by caller"),
                p, warnings + w)
    if agent:
        get_agent(agent)
        return (RouteDecision(mode="agent", agent=agent, method="explicit", confidence=1.0, reason="Agent chosen by caller"),
                plan_for_agent(agent), warnings)

    if use_planner and not ctx.llm.offline:
        try:
            orch = ctx.for_agent("orchestrator", tier=ctx.settings.orchestrator.get("planner_tier", "balanced"))
            proposed = orch.generate("plan", Plan, goal=goal, agents=catalog_for_planner(), playbooks=playbooks_for_planner(),
                                     shared_inputs=sorted((inputs or {}).keys()))
            if proposed.mode == "playbook" and proposed.playbook in books and not proposed.steps:
                proposed = plan_from_playbook(books[proposed.playbook])
            cleaned, w = validate_plan(proposed)
            decision = RouteDecision(mode=cleaned.mode, playbook=cleaned.playbook,
                                     agent=cleaned.steps[0].agent if cleaned.mode == "agent" else None,
                                     method="planner", confidence=0.9, reason=cleaned.reason,
                                     planner_metrics=orch.meter.snapshot())   # the planning call is part of what the run costs
            return decision, cleaned, w
        except Exception as exc:  # fall back to the deterministic router
            warnings.append(f"Planner unavailable, used deterministic router: {type(exc).__name__}: {exc}")

    decision = deterministic_route(goal)
    if decision.mode == "playbook" and decision.playbook:
        p, w = validate_plan(plan_from_playbook(books[decision.playbook]))
        return decision, p, warnings + w
    return decision, plan_for_agent(decision.agent or ""), warnings
