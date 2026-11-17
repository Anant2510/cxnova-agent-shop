"""Email Creation - brand-voice skill + structured output + deterministic deliverability linter,
rendered to HTML / MJML / plain text and exported through an ESP-neutral connector (approval-gated).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ...core import AgentKind, AgentSpec, BaseAgent, Category, PendingAction, RunContext, Tier, register
from ...core.emailkit import lint_email, render_html, render_mjml, render_text
from ...core.textutil import slugify


class EmailInput(BaseModel):
    overview: str = Field(description="What the email is about (offer, news, event ...)", json_schema_extra={"ui": "textarea"})
    target_audience: str | None = Field(default=None, description="Primary audience")
    call_to_action: str | None = Field(default=None, description="The action you want readers to take")
    cta_url: str | None = Field(default=None, description="Destination URL (a tagged UTM link is used automatically when available upstream)")
    audience_variants: list[str] = Field(default_factory=list, description="Optional extra audiences - one variant each (max 3)")
    extra_context: str | None = Field(default=None, description="Anything else the copy must reflect")
    export: bool = Field(default=True, description="Propose an ESP export (held for approval)")


class EmailVariant(BaseModel):
    audience: str
    subject_lines: list[str] = Field(min_length=1, max_length=5)
    preheader: str
    body_markdown: str
    cta_text: str


class EmailDraft(BaseModel):
    variants: list[EmailVariant] = Field(min_length=1)


class RenderedEmail(EmailVariant):
    subject: str
    cta_url: str | None = None
    html_artifact: str = ""
    text_artifact: str = ""
    mjml_artifact: str = ""
    lint: dict[str, int] = Field(default_factory=dict)


class EmailOutput(BaseModel):
    emails: list[RenderedEmail]
    lint_passed: bool


@register
class EmailCreationAgent(BaseAgent):
    spec = AgentSpec(
        id="email_creation",
        name="Email Creation",
        category=Category.CONTENT,
        summary="Creates on-brand marketing emails (subject, preheader, body, CTA) with audience variants, a deliverability linter and ESP-neutral HTML/MJML/text export.",
        kind=AgentKind.HYBRID,
        tier=Tier.BALANCED,
        connectors=["esp"],
        skills=["brand_voice", "email_deliverability"],
        keywords=["email", "newsletter", "nurture email", "promotional email", "subject line", "edm", "e-mail"],
        produces=["email.html", "email.mjml", "email.txt"],
        feeds=["content_translation", "experiment_ideation"],
        side_effects=["esp.export"],
        primary_inputs=["overview"],
    )
    Input = EmailInput
    Output = EmailOutput
    schemas = {"draft": EmailDraft, "repair": EmailVariant}

    def selftest_input(self) -> dict[str, Any]:
        return {"overview": "Invite homeowners to our free webinar on getting their home flood-ready before winter, with a checklist for attendees.",
                "target_audience": "Homeowners in flood-exposed areas", "call_to_action": "Register for the webinar",
                "cta_url": "https://www.meridian-mutual.example/webinars/climate-ready-home"}

    def stages(self):
        return [("context", self.context), ("draft", self.draft), ("lint_and_repair", self.lint_and_repair), ("render", self.render)]

    def context(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp = state["inputs"]
        brief = ctx.upstream_output("campaign_brief_generation") or {}
        utm = ctx.upstream_output("utm_generation") or {}
        cta_url = inp.get("cta_url")
        email_link = next((l["url"] for l in utm.get("links", []) if l.get("channel") == "email"), None)
        if email_link and (not cta_url or cta_url.split("?")[0] == email_link.split("?")[0]):
            cta_url = email_link
        audiences = [inp.get("target_audience") or brief.get("target_audience", {}).get("primary") or "General prospects"]
        audiences += [a for a in inp.get("audience_variants", []) if a][:2]
        return {"data": {"key_messages": brief.get("key_messages", []), "cta_url": cta_url, "audiences": audiences}}

    def draft(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        draft = ctx.generate("draft", EmailDraft, overview=inp["overview"], target_audience=data["audiences"][0],
                             call_to_action=inp.get("call_to_action"), key_messages=data["key_messages"],
                             extra_context=inp.get("extra_context"), variants=data["audiences"])
        # Reconcile with what was asked: one variant per requested audience, labels exactly as given.
        variants = draft.variants[: len(data["audiences"])]
        while len(variants) < len(data["audiences"]):
            variants.append(variants[-1].model_copy())
        for v, label in zip(variants, data["audiences"]):
            v.audience = label
        return {"data": {"draft": EmailDraft(variants=variants).model_dump()}}

    def _render(self, v: EmailVariant, cta_url: str | None, ctx: RunContext) -> dict[str, str]:
        brand = ctx.brand
        subject = v.subject_lines[0]
        args = dict(subject=subject, preheader=v.preheader, body_markdown=v.body_markdown, cta_text=v.cta_text, cta_url=cta_url or "#", brand=brand)
        return {"html": render_html(**args), "mjml": render_mjml(**args),
                "text": render_text(subject=subject, body_markdown=v.body_markdown, cta_text=v.cta_text, cta_url=cta_url, brand=brand)}

    def lint_and_repair(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        data = state["data"]
        rules = ctx.skill("email_deliverability")
        banned = ctx.brand.get("banned_phrases", [])
        fixed, checks = [], []
        for raw in data["draft"]["variants"]:
            v = EmailVariant.model_validate(raw)
            for attempt in range(2):
                r = self._render(v, data["cta_url"], ctx)
                found = lint_email(subject=v.subject_lines[0], preheader=v.preheader, html=r["html"], text=r["text"],
                                   cta_text=v.cta_text, rules=rules, banned_phrases=banned)
                failures = [f"{c.rule}: {c.message}" for c in found if c.status == "fail"]
                if not failures or attempt == 1 or ctx.llm.offline:
                    break
                v = ctx.generate("repair", EmailVariant, findings=failures, draft=v.model_dump())  # one model repair pass
            for c in found:
                c.target = v.audience
            checks += found
            fixed.append(v.model_dump())
        return {"data": {"final": fixed}, "checks": checks}

    def render(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        emails, artifacts, actions = [], [], []
        all_checks = state.get("checks", [])
        for i, raw in enumerate(data["final"], 1):
            v = EmailVariant.model_validate(raw)
            r = self._render(v, data["cta_url"], ctx)
            slug = slugify(v.audience, 30) or f"variant-{i}"
            a_html = ctx.artifacts.write_text(f"email-{i}-{slug}.html", r["html"], "text/html", f"Email for {v.audience}")
            a_txt = ctx.artifacts.write_text(f"email-{i}-{slug}.txt", r["text"], "text/plain", "Plain-text version")
            a_mjml = ctx.artifacts.write_text(f"email-{i}-{slug}.mjml", r["mjml"], "text/plain", "MJML source")
            artifacts += [a_html, a_txt, a_mjml]
            mine = [c for c in all_checks if c.get("target") == v.audience]
            counts = {s: sum(1 for c in mine if c.get("status") == s) for s in ("pass", "warn", "fail")}
            emails.append(RenderedEmail(**v.model_dump(), subject=v.subject_lines[0], cta_url=data["cta_url"],
                                        html_artifact=a_html.path, text_artifact=a_txt.path, mjml_artifact=a_mjml.path, lint=counts))
            if inp.get("export", True):
                actions.append(PendingAction(id=f"esp-{i}", kind="esp.export", connector="esp", method="export",
                                             payload={"name": f"{slug}-{ctx.run_id[-6:]}", "subject": v.subject_lines[0], "preheader": v.preheader,
                                                      "html": r["html"], "text": r["text"], "mjml": r["mjml"], "audience": v.audience},
                                             summary=f"Export '{v.subject_lines[0]}' to the ESP for {v.audience}"))
        passed = not any(c.get("status") == "fail" for c in all_checks)
        out = EmailOutput(emails=emails, lint_passed=passed)
        return {"output": out.model_dump(), "artifacts": artifacts, "actions": actions,
                "summary": f"{len(emails)} email variant(s); subject: “{emails[0].subject}”; linter {'passed' if passed else 'flagged issues'}"}
