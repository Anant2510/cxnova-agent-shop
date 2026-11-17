"""Webinar Follow Up Email Creation - Content Summary + Email engines, segmented by behaviour.

1. Attendance, info and transcript come from the pluggable WebinarConnector (Zoom, Teams, ON24,
   GoToWebinar or CSV) - column aliases in config/skills/webinar_segments.yaml.
2. The transcript is summarised by invoking the Content Summary agent as a sub-agent through the
   registry (`ctx.run_subagent("content_summary", ...)`) - its map-reduce engine is reused, not copied,
   and its model usage / connectors roll up into this agent's metrics.
3. Recipients are segmented by deterministic YAML rules (attended_full / early_drop / no_show) with an
   `engaged` flag from questions and polls that drives the MQL hand-off to sales.
4. One email per segment from a template; rendered (HTML / MJML / text) and linted with
   `caf.core.emailkit`, CTA links tagged with the UTM taxonomy, and exported through the ESP-neutral
   connector behind the approval gate. Per-segment recipient CSVs and an MQL CSV are artifacts.
"""

from __future__ import annotations

import io
import re
from typing import Any

import pandas as pd
from pydantic import BaseModel, Field

from ...core import AgentKind, AgentSpec, BaseAgent, Category, Check, PendingAction, RunContext, Tier, register
from ...core.emailkit import lint_email, render_html, render_mjml, render_text
from ...core.textutil import normalize_ws, slugify, word_count
from ..strategy.utm_generation import build_url

EMAIL_RX = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[A-Za-z]{2,}$")
FIRST_NAME_TAG = "{{first_name}}"
_OPS = {
    "==": lambda a, b: a == b, "!=": lambda a, b: a != b, ">": lambda a, b: a > b, ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b, "<=": lambda a, b: a <= b, "in": lambda a, b: a in (b or []), "not_in": lambda a, b: a not in (b or []),
}


class WebinarFollowupInput(BaseModel):
    webinar_id: str | None = Field(default=None, description="Webinar id in the webinar connector (default: the configured webinar)")
    transcript: str | None = Field(default=None, description="Transcript text (overrides the connector's transcript)", json_schema_extra={"ui": "textarea"})
    recording_url: str | None = Field(default=None, description="Override the recording link")
    slides_url: str | None = Field(default=None, description="Override the slides link")
    extra_context: str | None = Field(default=None, description="Anything every email should mention (offer, next event ...)")
    only_segments: list[str] = Field(default_factory=list, description="Limit to these segment ids (default: all segments in the rules)")
    tag_utm: bool = Field(default=True, description="Tag CTA links with the UTM taxonomy (email channel, utm_content = segment)")
    export: bool = Field(default=True, description="Propose one ESP export per segment (held for approval)")


class FollowupEmail(BaseModel):
    subject: str
    preheader: str
    body_markdown: str
    cta_text: str


class SegmentEmail(FollowupEmail):
    cta_url: str | None = None


class SegmentResult(BaseModel):
    segment: str
    label: str = ""
    count: int
    engaged_count: int = 0
    criteria: str
    email: SegmentEmail | None = None
    recipients_artifact: str | None = None
    html_artifact: str | None = None
    lint: dict[str, int] = Field(default_factory=dict)


class WebinarFollowupOutput(BaseModel):
    webinar: dict[str, Any]
    takeaways: list[str]
    segments: list[SegmentResult]
    mql_count: int
    mql_artifact: str | None = None
    registered: int = 0
    attended: int = 0
    attendance_rate: float = 0.0
    takeaways_method: str = ""
    lint_passed: bool = True


# ----------------------------------------------------------------------------- pure helpers (rule engine)

def resolve_columns(columns: list[str], aliases: dict[str, list[str]]) -> dict[str, str]:
    """Canonical field -> actual column (first alias present; case-insensitive)."""
    lower = {c.lower().strip(): c for c in columns}
    out: dict[str, str] = {}
    for field, names in (aliases or {}).items():
        for n in [field, *names]:
            if str(n).lower().strip() in lower:
                out[field] = lower[str(n).lower().strip()]
                break
    return out


def _truthy(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "y", "attended", "x")
    try:
        return bool(v) and not pd.isna(v)
    except (TypeError, ValueError):
        return bool(v)


def _num(v: Any) -> float:
    try:
        f = float(v)
        return 0.0 if pd.isna(f) else f
    except (TypeError, ValueError):
        return 0.0


def normalise_attendees(df: pd.DataFrame, colmap: dict[str, str], duration_minutes: float) -> pd.DataFrame:
    """Canonical attendee frame: email, names, company, job_title, attended, minutes, watch_pct, questions, polls, resources."""
    out = pd.DataFrame(index=df.index)
    for f in ("email", "first_name", "last_name", "company", "job_title"):
        out[f] = df[colmap[f]].fillna("").astype(str).str.strip() if f in colmap else ""
    out["email"] = out["email"].str.lower()
    out["attended"] = df[colmap["attended"]].map(_truthy) if "attended" in colmap else False
    minutes = df[colmap["minutes"]].map(_num) if "minutes" in colmap else pd.Series(0.0, index=df.index)
    if "minutes" not in colmap and {"join_minute", "leave_minute"} <= set(colmap):
        minutes = (df[colmap["leave_minute"]].map(_num) - df[colmap["join_minute"]].map(_num)).clip(lower=0)
    out["minutes"] = minutes.astype(float)
    dur = duration_minutes if duration_minutes and duration_minutes > 0 else 60.0
    out["watch_pct"] = (out["minutes"] / dur).clip(lower=0, upper=1).round(3)
    for f in ("questions", "polls", "resources"):
        out[f] = df[colmap[f]].map(_num).astype(int) if f in colmap else 0
    return out


def eval_condition(row: dict[str, Any], cond: dict[str, Any]) -> bool:
    op = _OPS.get(str(cond.get("op", "==")))
    if op is None:
        raise ValueError(f"Unknown operator {cond.get('op')!r} in webinar segment rules")
    value = row.get(cond.get("field"))
    want = cond.get("value")
    try:
        if isinstance(want, bool):
            return op(bool(value), want)
        if isinstance(want, (int, float)) and not isinstance(want, bool):
            return op(float(value or 0), float(want))
        return op(value, want)
    except TypeError:
        return False


def eval_rule(row: dict[str, Any], rule: dict[str, Any] | None) -> bool:
    if not rule:
        return False
    if "all" in rule:
        return all(eval_condition(row, c) if "field" in c else eval_rule(row, c) for c in rule["all"])
    if "any" in rule:
        return any(eval_condition(row, c) if "field" in c else eval_rule(row, c) for c in rule["any"])
    return eval_condition(row, rule) if "field" in rule else False


def lead_score(row: dict[str, Any], weights: dict[str, float]) -> int:
    s = sum(float(w) * _num(row.get(k)) for k, w in (weights or {}).items())
    return int(min(100, round(s)))


def segment_attendees(people: pd.DataFrame, rules: dict[str, Any]) -> pd.DataFrame:
    """Adds segment, engaged, mql, lead_score and lead_type columns (first matching segment wins)."""
    segs = rules.get("segments", [])
    eng_rule = {k: v for k, v in (rules.get("engaged") or {}).items() if k in ("all", "any")}
    mql = rules.get("mql", {}) or {}
    consumer = {str(c).strip().lower() for c in mql.get("consumer_companies", [])}
    rows = []
    for rec in people.to_dict(orient="records"):
        seg = next((s["id"] for s in segs if eval_rule(rec, s.get("when"))), "unsegmented")
        engaged = eval_rule(rec, eng_rule)
        is_mql = ((engaged or not mql.get("require_engaged", True)) and seg in mql.get("segments", [])
                  and not any(eval_rule(rec, x if ("all" in x or "any" in x) else {"all": [x]}) for x in mql.get("exclude", [])))
        rows.append({"segment": seg, "engaged": bool(engaged), "mql": bool(is_mql),
                     "lead_score": lead_score(rec, mql.get("score_weights", {})) if is_mql else 0,
                     "lead_type": "consumer" if str(rec.get("company", "")).strip().lower() in consumer else "business"})
    return pd.concat([people.reset_index(drop=True), pd.DataFrame(rows)], axis=1)


def engagement_reason(row: dict[str, Any]) -> str:
    bits = [f"watched {round(_num(row.get('watch_pct')) * 100)}%"]
    if _num(row.get("questions")):
        bits.append(f"asked {int(_num(row['questions']))} question(s)")
    if _num(row.get("polls")):
        bits.append(f"answered {int(_num(row['polls']))} poll(s)")
    if _num(row.get("resources")):
        bits.append(f"clicked {int(_num(row['resources']))} resource(s)")
    return "; ".join(bits)


def speakers_line(info: dict[str, Any]) -> str:
    return "; ".join(", ".join(x for x in (s.get("name"), s.get("title")) if x) for s in info.get("speakers", []) if isinstance(s, dict))


# ----------------------------------------------------------------------------- agent

@register
class WebinarFollowupAgent(BaseAgent):
    spec = AgentSpec(
        id="webinar_followup_email",
        name="Webinar Follow Up Email Creation",
        category=Category.CONTENT,
        summary="Turns webinar attendance and the transcript into segment-specific follow-up emails (attended, left early, no-show), "
                "with an engagement-based MQL hand-off to sales and gated ESP export.",
        kind=AgentKind.HYBRID,
        tier=Tier.BALANCED,
        connectors=["webinar", "esp"],
        skills=["webinar_segments", "email_deliverability", "utm_taxonomy", "brand_voice"],
        keywords=["webinar follow up", "webinar follow-up", "post-webinar", "no-show email", "attendee email", "recording email",
                  "event follow up", "mql hand-off"],
        produces=["email-<segment>.html", "recipients-<segment>.csv", "mql_for_sales.csv"],
        feeds=["content_translation", "experiment_ideation"],
        side_effects=["esp.export"],
    )
    Input = WebinarFollowupInput
    Output = WebinarFollowupOutput
    schemas = {"email": FollowupEmail, "repair": FollowupEmail}

    def selftest_input(self) -> dict[str, Any]:
        return {"extra_context": "The rebuild calculator and the winter home checklist are attached to every email."}

    def stages(self):
        return [("attendance", self.attendance), ("summarise", self.summarise), ("segment", self.segment),
                ("draft", self.draft), ("lint_and_repair", self.lint_and_repair), ("render_export", self.render_export)]

    # -- 1. attendance (connector + normalisation + data-quality checks) ------------------------------------
    def attendance(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp = state["inputs"]
        rules = ctx.skill("webinar_segments")
        conn = ctx.connector("webinar")
        wid = inp.get("webinar_id")
        info = dict(conn.info(wid) or {})
        raw = conn.attendees(wid)
        transcript = inp.get("transcript") or conn.transcript(wid) or ""
        checks: list[Check] = []
        warnings: list[str] = []
        if inp.get("recording_url"):
            info["recording_url"] = inp["recording_url"]
        if inp.get("slides_url"):
            info["slides_url"] = inp["slides_url"]
        colmap = resolve_columns(list(raw.columns), rules.get("columns", {}))
        missing = [f for f in rules.get("required", ["email", "attended"]) if f not in colmap]
        if missing:
            raise ValueError(f"Attendance export lacks required column(s) {missing}; add aliases in webinar_segments.yaml.")
        optional_missing = [f for f in ("minutes", "questions", "polls") if f not in colmap]
        checks.append(Check(rule="data.columns", status="warn" if optional_missing else "pass", severity="medium" if optional_missing else "low",
                            message=("Mapped attendance columns: " + ", ".join(f"{k}<-{v}" for k, v in colmap.items()))
                                    + (f". Missing optional: {optional_missing}" if optional_missing else "")))
        duration = _num(info.get("duration_minutes"))
        if not duration:
            duration = max(_num(raw[colmap["leave_minute"]].max()) if "leave_minute" in colmap else 0,
                           _num(raw[colmap["minutes"]].max()) if "minutes" in colmap else 0) or 60.0
            warnings.append(f"Webinar duration not in info; using {duration:.0f} minutes.")
        people = normalise_attendees(raw, colmap, duration)
        invalid = people[~people["email"].str.match(EMAIL_RX)]
        if len(invalid):
            checks.append(Check(rule="data.invalid_emails", status="warn", severity="medium",
                                message=f"{len(invalid)} row(s) with an invalid email were excluded."))
        people = people[people["email"].str.match(EMAIL_RX)]
        dupes = int(people.duplicated("email").sum())
        if dupes:
            checks.append(Check(rule="data.duplicates", status="warn", severity="low", message=f"{dupes} duplicate registration(s) merged."))
            people = people.sort_values(["attended", "minutes"], ascending=False).drop_duplicates("email").sort_index()
        odd = people[(~people["attended"]) & ((people["minutes"] > 0) | (people["questions"] > 0) | (people["polls"] > 0))]
        if len(odd):
            checks.append(Check(rule="data.consistency", status="warn", severity="low",
                                message=f"{len(odd)} registrant(s) marked as not attended but with session activity - kept as no-shows."))
        n, att = len(people), int(people["attended"].sum())
        checks.append(Check(rule="attendance.rate", status="info", severity="low",
                            message=f"{att} of {n} registrants attended ({att / max(1, n):.0%}); duration {duration:.0f} min."))
        if not transcript.strip():
            warnings.append("No transcript available - emails are drafted without session takeaways.")
        return {"data": {"rules": rules, "info": info, "people": people.to_dict(orient="records"), "transcript": transcript,
                         "duration": duration},
                "checks": checks, "warnings": warnings}

    # -- 2. summarise the transcript with the Content Summary agent (sub-agent via the registry) ------------
    def summarise(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        data = state["data"]
        if not data["transcript"].strip():
            return {"data": {"takeaways": [], "takeaways_method": "none"}}
        # The Content Summary agent runs as a sub-graph; its model usage and connectors roll up into this run.
        result = ctx.run_subagent("content_summary", {"text": data["transcript"], "title": data["info"].get("title"), "mode": "bullet",
                                                      "max_words": 160, "chunk_words": 700,
                                                      "focus": "practical takeaways and advice for homeowners"})
        warnings: list[str] = []
        reasons = [f"content_summary: {r}" for r in result.provenance.get("degraded_reasons", [])]
        if result.status == "error":
            warnings.append(f"Transcript summary failed ({result.error}); emails drafted without takeaways.")
            reasons.append("transcript summary unavailable")
            takeaways, method = [], "none"
        else:
            takeaways = [normalize_ws(t) for t in result.output.get("key_points", []) if normalize_ws(t)][:5]
            method = result.output.get("method", "")
        checks = [Check(rule="transcript.summary", status="info" if takeaways else "warn", severity="low",
                        message=f"Content Summary sub-agent ({method or 'n/a'}): {len(takeaways)} takeaway(s) from "
                                f"{result.output.get('source_words', 0) if result.status != 'error' else 0:,} transcript words.")]
        upd: dict[str, Any] = {"data": {"takeaways": takeaways, "takeaways_method": method,
                                        "summary_markdown": result.output.get("summary_markdown", "") if result.status != "error" else ""},
                               "checks": checks, "warnings": warnings, "artifacts": [a.model_dump() for a in result.artifacts[:1]]}
        if reasons:
            upd["provenance"] = {"degraded_reasons": reasons}
        return upd

    # -- 3. segment (deterministic rule engine) + recipient / MQL CSVs --------------------------------------
    def segment(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        rules = data["rules"]
        people = pd.DataFrame(data["people"])
        seg = segment_attendees(people, rules) if len(people) else people
        checks: list[Check] = []
        artifacts = []
        cols = ["email", "first_name", "last_name", "company", "job_title", "segment", "engaged", "minutes", "watch_pct",
                "questions", "polls", "resources"]
        wanted = set(inp.get("only_segments") or [s["id"] for s in rules.get("segments", [])])
        segments: list[dict[str, Any]] = []
        for s in rules.get("segments", []):
            if s["id"] not in wanted:
                continue
            members = seg[seg["segment"] == s["id"]] if len(seg) else seg
            ref = None
            if len(members):
                buf = io.StringIO()
                members[cols].to_csv(buf, index=False)
                ref = ctx.artifacts.write_text(f"recipients-{s['id']}.csv", buf.getvalue(), "text/csv",
                                               f"Recipients: {s.get('label', s['id'])}")
                artifacts.append(ref)
            segments.append({"segment": s["id"], "label": s.get("label", s["id"]), "criteria": s.get("criteria", ""),
                             "count": int(len(members)), "engaged_count": int(members["engaged"].sum()) if len(members) else 0,
                             "brief": s.get("email", {}), "recipients_artifact": ref.path if ref else None})
        unseg = int((seg["segment"] == "unsegmented").sum()) if len(seg) else 0
        all_counts = ", ".join(f"{s['id']}={int((seg['segment'] == s['id']).sum()) if len(seg) else 0}" for s in rules.get("segments", []))
        selected = f" Emails limited to {sorted(wanted)}." if inp.get("only_segments") else ""
        checks.append(Check(rule="segments.coverage", status="warn" if unseg else "pass", severity="medium" if unseg else "low",
                            message=(f"{unseg} registrant(s) matched no segment rule ({all_counts})." if unseg else
                                     f"Every registrant is in exactly one segment: {all_counts}.") + selected))
        mql = seg[seg["mql"]] if len(seg) else seg
        mql_ref = None
        if len(mql):
            m = mql.copy()
            m["engagement"] = [engagement_reason(r) for r in m.to_dict(orient="records")]
            m["owner"] = rules.get("mql", {}).get("owner", "Sales")
            m = m.sort_values("lead_score", ascending=False)
            buf = io.StringIO()
            m[["email", "first_name", "last_name", "company", "job_title", "lead_type", "lead_score", "segment", "engagement", "owner"]] \
                .to_csv(buf, index=False)
            mql_ref = ctx.artifacts.write_text("mql_for_sales.csv", buf.getvalue(), "text/csv", "MQL hand-off for sales (engaged attendees)")
            artifacts.append(mql_ref)
        checks.append(Check(rule="mql.handoff", status="info", severity="low",
                            message=f"{len(mql)} MQL(s) for sales (engaged = {self._rule_text(rules.get('engaged', {}))}; "
                                    f"segments {rules.get('mql', {}).get('segments', [])})"
                                    + (f": {int((mql['lead_type'] == 'business').sum())} business, {int((mql['lead_type'] == 'consumer').sum())} consumer."
                                       if len(mql) else ".")))
        return {"data": {"segments": segments, "mql_count": int(len(mql)), "mql_artifact": mql_ref.path if mql_ref else None,
                         "registered": int(len(seg)), "attended": int(seg["attended"].sum()) if len(seg) else 0},
                "checks": checks, "artifacts": artifacts}

    @staticmethod
    def _rule_text(rule: dict[str, Any]) -> str:
        for key, joiner in (("any", " or "), ("all", " and ")):
            if key in rule:
                return joiner.join(f"{c.get('field')} {c.get('op')} {c.get('value')}" for c in rule[key] if "field" in c)
        return "n/a"

    # -- 4. draft one email per segment (model, template) ---------------------------------------------------
    def _cta_url(self, brief: dict[str, Any], info: dict[str, Any], tag: bool, ctx: RunContext) -> str | None:
        target = brief.get("cta") or "recording_url"
        url = info.get(target) if not str(target).startswith("http") else target
        url = url or info.get("recording_url") or ctx.brand.get("website")
        if not url or not tag:
            return url
        tax = ctx.skill("utm_taxonomy", default={})
        ch = (tax.get("channels") or {}).get(ctx.skill("webinar_segments", default={}).get("utm", {}).get("channel", "email")) or {}
        campaign = slugify(str(info.get("webinar_id") or info.get("title") or "webinar"), 60).replace("-", "_")
        return build_url(url, {"utm_source": ch.get("source", "newsletter"), "utm_medium": ch.get("medium", "email"),
                               "utm_campaign": campaign, "utm_content": brief.get("utm_content")})

    def draft(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        info = data["info"]
        drafts: dict[str, Any] = {}
        for s in data["segments"]:
            if not s["count"]:
                continue
            brief = s["brief"]
            cta_url = self._cta_url(brief, info, inp.get("tag_utm", True), ctx)
            e = ctx.generate("email", FollowupEmail, webinar_title=info.get("title", "our webinar"), webinar_date=info.get("date", ""),
                             speakers=speakers_line(info), segment_label=s["label"], criteria=s["criteria"], goal=brief.get("goal", ""),
                             cta_text=brief.get("cta_text", "Watch the recording"), takeaways=data.get("takeaways", []),
                             extra_context=inp.get("extra_context"), first_name_tag=FIRST_NAME_TAG,
                             has_recording=bool(info.get("recording_url")), has_slides=bool(info.get("slides_url")))
            drafts[s["segment"]] = {**self._reconcile(e, brief, ctx).model_dump(), "cta_url": cta_url}
        return {"data": {"drafts": drafts}}

    @staticmethod
    def _reconcile(e: FollowupEmail, brief: dict[str, Any], ctx: RunContext) -> FollowupEmail:
        """Deterministic: the CTA follows the rule pack unless the model offers a valid verb-first CTA of <= 5 words."""
        cta = normalize_ws(e.cta_text)
        if ctx.llm.offline or not cta or word_count(cta) > 5 or cta.startswith("["):
            cta = brief.get("cta_text") or "Watch the recording"
        return FollowupEmail(subject=normalize_ws(e.subject), preheader=normalize_ws(e.preheader), body_markdown=e.body_markdown.strip(),
                             cta_text=cta)

    # -- 5. lint (emailkit) + one repair pass when a model is configured ---------------------------------------
    def _render(self, e: dict[str, Any], ctx: RunContext) -> dict[str, str]:
        args = dict(subject=e["subject"], preheader=e["preheader"], body_markdown=e["body_markdown"], cta_text=e["cta_text"],
                    cta_url=e.get("cta_url") or "#", brand=ctx.brand)
        return {"html": render_html(**args), "mjml": render_mjml(**args),
                "text": render_text(subject=e["subject"], body_markdown=e["body_markdown"], cta_text=e["cta_text"],
                                    cta_url=e.get("cta_url"), brand=ctx.brand)}

    def lint_and_repair(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        data = state["data"]
        rules = ctx.skill("email_deliverability", default={})
        banned = ctx.brand.get("banned_phrases", [])
        checks: list[Check] = []
        final: dict[str, Any] = {}
        briefs = {s["segment"]: s["brief"] for s in data["segments"]}
        for seg_id, e in data.get("drafts", {}).items():
            for attempt in range(2):
                r = self._render(e, ctx)
                found = lint_email(subject=e["subject"], preheader=e["preheader"], html=r["html"], text=r["text"], cta_text=e["cta_text"],
                                   rules=rules, banned_phrases=banned)
                failures = [f"{c.rule}: {c.message}" for c in found if c.status == "fail"]
                if not failures or attempt == 1 or ctx.llm.offline:
                    break
                fixed = ctx.generate("repair", FollowupEmail, findings=failures, draft={k: e[k] for k in ("subject", "preheader", "body_markdown", "cta_text")})
                e = {**self._reconcile(fixed, briefs.get(seg_id, {}), ctx).model_dump(), "cta_url": e.get("cta_url")}
            for c in found:
                c.target = seg_id
            checks += found
            final[seg_id] = e
        return {"data": {"final": final}, "checks": checks}

    # -- 6. render, artifacts, gated ESP export, output ---------------------------------------------------------
    def render_export(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        info = data["info"]
        all_checks = state.get("checks", [])
        artifacts, actions, results = [], [], []
        wid = slugify(str(info.get("webinar_id") or info.get("title") or "webinar"), 40)
        for s in data["segments"]:
            e = data.get("final", {}).get(s["segment"])
            res = {k: s[k] for k in ("segment", "label", "count", "engaged_count", "criteria", "recipients_artifact")}
            if e:
                r = self._render(e, ctx)
                a_html = ctx.artifacts.write_text(f"email-{s['segment']}.html", r["html"], "text/html", f"Follow-up email: {s['label']}")
                artifacts += [a_html,
                              ctx.artifacts.write_text(f"email-{s['segment']}.txt", r["text"], "text/plain", "Plain-text version"),
                              ctx.artifacts.write_text(f"email-{s['segment']}.mjml", r["mjml"], "text/plain", "MJML source")]
                mine = [c for c in all_checks if c.get("target") == s["segment"] and str(c.get("rule", "")).split(".")[0]
                        in ("subject", "preheader", "body", "content", "brand", "compliance")]
                res.update(email=SegmentEmail(**e), html_artifact=a_html.path,
                           lint={st: sum(1 for c in mine if c.get("status") == st) for st in ("pass", "warn", "fail")})
                if inp.get("export", True):
                    actions.append(PendingAction(
                        id=f"esp-{s['segment']}", kind="esp.export", connector="esp", method="export",
                        payload={"name": f"{wid}-{s['segment']}", "subject": e["subject"], "preheader": e["preheader"], "html": r["html"],
                                 "text": r["text"], "mjml": r["mjml"], "audience": s["label"],
                                 "metadata": {"segment": s["segment"], "criteria": s["criteria"], "recipients": s["count"],
                                              "recipients_artifact": s["recipients_artifact"], "webinar_id": info.get("webinar_id"),
                                              "cta_url": e.get("cta_url"), "run_id": ctx.run_id}},
                        summary=f"Export '{e['subject']}' to the ESP for {s['count']} {s['label'].lower()} recipient(s)"))
            results.append(SegmentResult(**res))
        lint_passed = not any(c.get("status") == "fail" for c in all_checks if c.get("target") in {s["segment"] for s in data["segments"]})
        registered, attended = data.get("registered", 0), data.get("attended", 0)
        webinar = {k: info.get(k) for k in ("webinar_id", "title", "date", "duration_minutes", "host", "recording_url", "slides_url", "speakers")
                   if info.get(k) is not None}
        out = WebinarFollowupOutput(webinar=webinar, takeaways=data.get("takeaways", []), segments=results, mql_count=data.get("mql_count", 0),
                                    mql_artifact=data.get("mql_artifact"), registered=registered, attended=attended,
                                    attendance_rate=round(attended / max(1, registered), 4), takeaways_method=data.get("takeaways_method", ""),
                                    lint_passed=lint_passed)
        emails = sum(1 for r in results if r.email)
        summary = (f"'{info.get('title', 'Webinar')}': {emails} segment email(s) for {registered} registrants "
                   f"({', '.join(f'{r.segment}={r.count}' for r in results)}); {out.mql_count} MQL(s) for sales; "
                   f"linter {'passed' if lint_passed else 'flagged issues'}")
        return {"output": out.model_dump(mode="json"), "artifacts": artifacts, "actions": actions, "summary": summary}
