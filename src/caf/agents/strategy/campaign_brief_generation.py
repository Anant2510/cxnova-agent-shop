"""Campaign Brief Generation - prompt template + Pydantic contract behind the model-agnostic adapter.

The seven-section contract lives in config/skills/campaign_brief.yaml; deliverables are emitted as
tasks through a generic TaskEmitter (JSON file, Jira, Azure DevOps ...) instead of a CMP hand-off.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from ...core import AgentKind, AgentSpec, BaseAgent, Category, Check, PendingAction, RunContext, Tier, register

DeliverableType = Literal["email", "landing_page", "social_post", "paid_social_ad", "search_ad", "display_ad", "blog_post",
                          "press_release", "video", "webinar", "event", "sales_enablement", "other"]


class BriefInput(BaseModel):
    campaign_type: str = Field(description="Required. e.g. product launch, webinar, event, content campaign, nurture")
    audience: str | None = Field(default=None, description="Who the campaign is for")
    objective: str | None = Field(default=None, description="What the campaign must achieve")
    assets_needed: str | None = Field(default=None, description="Assets you already know you need")
    markets: str | None = Field(default=None, description="Markets / regions")
    timeline: str | None = Field(default=None, description="Launch date or window")
    budget: str | None = Field(default=None, description="Indicative budget")
    notes: str | None = Field(default=None, description="Rough marketing notes to convert into the brief", json_schema_extra={"ui": "textarea"})
    emit_tasks: bool = Field(default=True, description="Emit each deliverable as a task through the task connector")


class Objective(BaseModel):
    objective: str
    kpi: str
    target: str = Field(description="Numeric target, e.g. '1,200 quote starts' or '+15% CTR'")


class AudienceDef(BaseModel):
    primary: str
    secondary: list[str] = Field(default_factory=list)
    insight: str = ""
    personas: list[str] = Field(default_factory=list)


class Deliverable(BaseModel):
    name: str
    type: DeliverableType = "other"
    channel: str
    owner_role: str
    due_offset_days: int = Field(default=14, ge=0, le=365, description="Days after brief approval")
    description: str = ""


class ChannelPlan(BaseModel):
    channel: str
    role: str
    timing: str = ""
    budget_share_pct: float = Field(default=0, ge=0, le=100)


class Measurement(BaseModel):
    kpis: list[str]
    cadence: str
    tools: list[str] = Field(default_factory=list)
    utm_convention: str = ""


class CampaignBrief(BaseModel):
    campaign_name: str
    campaign_overview: str
    objectives_and_goals: list[Objective] = Field(min_length=1)
    target_audience: AudienceDef
    key_messages: list[str] = Field(min_length=1)
    creative_and_content_deliverables: list[Deliverable] = Field(min_length=1)
    channels_and_media_plan: list[ChannelPlan] = Field(min_length=1)
    measurement_and_reporting: Measurement


class BriefOutput(CampaignBrief):
    markdown: str
    tasks_emitted: int = 0


def brief_markdown(b: CampaignBrief, sections: list[dict[str, Any]]) -> str:
    titles = {s["key"]: s["title"] for s in sections}
    lines = [f"# {b.campaign_name}", "", f"## 1. {titles.get('campaign_overview', 'Campaign overview')}", b.campaign_overview, "",
             f"## 2. {titles.get('objectives_and_goals', 'Objectives and goals')}", "| Objective | KPI | Target |", "|---|---|---|"]
    lines += [f"| {o.objective} | {o.kpi} | {o.target} |" for o in b.objectives_and_goals]
    a = b.target_audience
    lines += ["", f"## 3. {titles.get('target_audience', 'Target audience')}", f"**Primary:** {a.primary}"]
    if a.secondary:
        lines.append("**Secondary:** " + "; ".join(a.secondary))
    if a.insight:
        lines.append(f"**Insight:** {a.insight}")
    if a.personas:
        lines.append("**Personas:** " + "; ".join(a.personas))
    lines += ["", f"## 4. {titles.get('key_messages', 'Key messages')}"] + [f"{i}. {m}" for i, m in enumerate(b.key_messages, 1)]
    lines += ["", f"## 5. {titles.get('creative_and_content_deliverables', 'Creative and content deliverables')}",
              "| # | Deliverable | Type | Channel | Owner | Due (days) |", "|---|---|---|---|---|---|"]
    lines += [f"| {i} | {d.name} | {d.type} | {d.channel} | {d.owner_role} | +{d.due_offset_days} |"
              for i, d in enumerate(b.creative_and_content_deliverables, 1)]
    lines += ["", f"## 6. {titles.get('channels_and_media_plan', 'Channels and media plan')}",
              "| Channel | Role | Timing | Budget share |", "|---|---|---|---|"]
    lines += [f"| {c.channel} | {c.role} | {c.timing} | {c.budget_share_pct:.0f}% |" for c in b.channels_and_media_plan]
    m = b.measurement_and_reporting
    lines += ["", f"## 7. {titles.get('measurement_and_reporting', 'Measurement and reporting')}",
              "**KPIs:** " + "; ".join(m.kpis), f"**Cadence:** {m.cadence}"]
    if m.tools:
        lines.append("**Tools:** " + ", ".join(m.tools))
    if m.utm_convention:
        lines.append(f"**UTM convention:** {m.utm_convention}")
    return "\n".join(lines) + "\n"


@register
class CampaignBriefAgent(BaseAgent):
    spec = AgentSpec(
        id="campaign_brief_generation",
        name="Campaign Brief Generation",
        category=Category.STRATEGY,
        summary="Turns a few inputs or rough notes into a standardised seven-section campaign brief and emits each deliverable as a task.",
        kind=AgentKind.LLM,
        tier=Tier.BALANCED,
        connectors=["tasks"],
        skills=["campaign_brief", "brand_voice"],
        keywords=["campaign brief", "brief", "campaign plan", "kick-off", "kickoff", "launch plan", "deliverables"],
        produces=["brief.md", "brief.json", "tasks"],
        feeds=["email_creation", "linkedin_inmail_copy", "blog_post_creation", "press_release_creation", "content_adaptation", "utm_generation"],
        side_effects=["tasks.create"],
        primary_inputs=["campaign_type", "notes"],
    )
    Input = BriefInput
    Output = BriefOutput
    schemas = {"brief": CampaignBrief}

    def selftest_input(self) -> dict[str, Any]:
        return {"campaign_type": "webinar", "audience": "Homeowners and independent brokers in flood-exposed US regions",
                "objective": "Drive webinar registrations and quote starts for flood-ready home cover",
                "assets_needed": "invite email, LinkedIn ads, landing page, follow-up email"}

    def stages(self):
        return [("draft", self.draft), ("validate", self.validate), ("publish", self.publish)]

    def draft(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp = state["inputs"]
        contract = ctx.skill("campaign_brief")
        research = []
        for agent_id in ("competitive_insights", "keyword_research", "industry_marketing"):
            up = ctx.upstream_output(agent_id)
            if up:
                research.append(f"[{agent_id}] {str(up.get('executive_summary') or up.get('summary') or up)[:1500]}")
        brief = ctx.generate("brief", CampaignBrief, sections=contract["sections"], deliverable_types=contract["deliverable_types"],
                             research="\n".join(research), **inp)
        return {"data": {"brief": brief.model_dump(), "contract": contract}}

    def validate(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        b = CampaignBrief.model_validate(state["data"]["brief"])
        checks: list[Check] = []
        n = len(b.key_messages)
        checks.append(Check(rule="key_messages.count", status="pass" if 3 <= n <= 5 else "warn", severity="medium",
                            message=f"{n} key messages (contract: 3-5)."))
        no_number = [o.objective for o in b.objectives_and_goals if not re.search(r"\d", o.target)]
        checks.append(Check(rule="objectives.measurable", status="warn" if no_number else "pass", severity="medium",
                            message=f"Objectives without a numeric target: {no_number}" if no_number else "Every objective has a numeric target."))
        total = sum(c.budget_share_pct for c in b.channels_and_media_plan)
        if total and abs(total - 100) > 1:
            for c in b.channels_and_media_plan:  # deterministic normalisation, reported
                c.budget_share_pct = round(c.budget_share_pct * 100 / total, 1)
            checks.append(Check(rule="media.budget_sum", status="warn", severity="low", message=f"Budget shares summed to {total:.0f}%; normalised to 100%."))
        else:
            checks.append(Check(rule="media.budget_sum", status="pass", severity="low", message="Budget shares sum to 100%."))
        checks.append(Check(rule="deliverables.itemised", status="pass" if len(b.creative_and_content_deliverables) >= 3 else "warn",
                            severity="medium", message=f"{len(b.creative_and_content_deliverables)} itemised deliverables."))
        banned = [p for p in ctx.brand.get("banned_phrases", []) if p.lower() in b.model_dump_json().lower()]
        if banned:
            checks.append(Check(rule="brand.banned_phrases", status="fail", severity="high", message=f"Banned phrases: {banned}"))
        return {"data": {"brief": b.model_dump()}, "checks": checks}

    def publish(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        b = CampaignBrief.model_validate(state["data"]["brief"])
        contract = state["data"]["contract"]
        md = brief_markdown(b, contract["sections"])
        artifacts = [ctx.artifacts.write_text("brief.md", md, "text/markdown", "Campaign brief"),
                     ctx.artifacts.write_json("brief.json", b.model_dump(), "Campaign brief (structured)")]
        actions: list[PendingAction] = []
        if state["inputs"].get("emit_tasks", True):
            tasks = [{"title": f"{b.campaign_name}: {d.name}", "type": d.type, "channel": d.channel, "owner_role": d.owner_role,
                      "due_offset_days": d.due_offset_days, "description": d.description} for d in b.creative_and_content_deliverables]
            external = getattr(ctx.connector("tasks"), "external", False)
            actions.append(PendingAction(id="tasks-1", kind="tasks.create", connector="tasks", method="create",
                                         payload={"tasks": tasks, "project": contract.get("default_task_project")},
                                         summary=f"Create {len(tasks)} deliverable tasks", requires_approval=bool(external)))
        out = BriefOutput(**b.model_dump(), markdown=md, tasks_emitted=len(b.creative_and_content_deliverables) if actions else 0)
        return {"output": out.model_dump(), "artifacts": artifacts, "actions": actions,
                "summary": f"Brief '{b.campaign_name}': {len(b.objectives_and_goals)} objectives, "
                           f"{len(b.creative_and_content_deliverables)} deliverables across {len(b.channels_and_media_plan)} channels"}
