"""LinkedIn InMail Copy Creation - persona variants of B2B message copy, validated against a
version-controlled platform-constraints skill (config/skills/platform_constraints.yaml).

The same engine emits LinkedIn Message / Conversation ads, X posts, email or WhatsApp copy: the
channel's fields, limits and token syntax are configuration. A deterministic validator enforces
the limits (budgeting a reserved width for every merge token); over-limit copy gets one model
repair pass, then a deterministic trim that never cuts a token. Personalisation uses neutral merge
tokens ({first_name}, {company}, {job_title}) rendered against any recipient list - no fixed CRM.
"""

from __future__ import annotations

import csv
import io
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from ...core import AgentKind, AgentSpec, BaseAgent, Category, Check, RunContext, Tier, register
from ...core.textutil import normalize_ws

FIELDS = ("subject", "preheader", "body", "cta_text")
TOKEN = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
URL = re.compile(r"https?://\S+")

# ----------------------------------------------------------------------------- contract


class Recipient(BaseModel):
    first_name: str | None = None
    company: str | None = None
    job_title: str | None = None


class InMailInput(BaseModel):
    overview: str = Field(min_length=10, description="What the message is about - offer, event, content or meeting ask", json_schema_extra={"ui": "textarea"})
    audience: str | None = Field(default=None, description="Who the message is for")
    personas: list[str] = Field(default_factory=list, max_length=6, description="One variant per persona, e.g. 'Head of Claims'")
    channel: str = Field(default="linkedin_inmail", description="Channel key from the platform_constraints skill (linkedin_inmail, linkedin_conversation, x_post, email, whatsapp)")
    call_to_action: str | None = Field(default=None, description="The action you want, e.g. 'Register for the webinar'")
    cta_url: str | None = Field(default=None, description="Destination URL (a UTM-tagged link is used when available upstream)")
    sender_name: str | None = Field(default=None, description="Member profile the message is sent from")
    sender_title: str | None = Field(default=None, description="Sender's job title")
    recipients: list[Recipient] = Field(default_factory=list, max_length=50, description="Optional recipients (first_name, company, job_title) for personalised previews")
    recipients_csv: str | None = Field(default=None, description="Optional CRM export (CSV with first_name, company, job_title columns) - any CRM, no fixed integration")
    extra_context: str | None = Field(default=None, description="Proof points, event details or anything the copy must reflect")


class MessageVariant(BaseModel):
    persona: str = Field(description="Persona label exactly as given")
    subject: str | None = None
    preheader: str | None = None
    body: str
    cta_text: str | None = None


class MessageDraft(BaseModel):
    variants: list[MessageVariant] = Field(min_length=1)


class Preview(BaseModel):
    recipient: str
    subject: str | None = None
    preheader: str | None = None
    body: str
    cta_text: str | None = None
    char_counts: dict[str, int] = Field(default_factory=dict)
    within_limits: bool
    fallbacks_used: list[str] = Field(default_factory=list)


class MessageOut(BaseModel):
    persona: str
    channel: str
    subject: str | None = None
    preheader: str | None = None
    body: str
    cta_text: str | None = None
    cta_url: str | None = None
    char_counts: dict[str, int] = Field(default_factory=dict, description="Budgeted characters per field (tokens at reserved width)")
    limits: dict[str, int] = Field(default_factory=dict)
    valid: bool
    issues: list[str] = Field(default_factory=list)
    repair: Literal["none", "model", "trimmed", "model+trimmed"] = "none"
    merge_fields_used: list[str] = Field(default_factory=list)
    platform_copy: dict[str, str] = Field(default_factory=dict, description="Fields with merge tokens in the platform's macro syntax")
    previews: list[Preview] = Field(default_factory=list)


class InMailOutput(BaseModel):
    channel: str
    channel_label: str
    constraints_version: str
    sender_name: str | None = None
    variants: list[MessageOut]
    all_valid: bool
    merge_fields: list[str]


# ----------------------------------------------------------------------------- deterministic validator

def measure(text: str | None, merge_fields: dict[str, Any], url_chars: int | None = None) -> int:
    """Budgeted length: every known merge token counts as its reserve width; URLs as url_chars (X)."""
    t = text or ""
    if url_chars:
        t = URL.sub("x" * int(url_chars), t)
    return len(TOKEN.sub(lambda m: "x" * int(merge_fields[m.group(1)]["reserve_chars"]) if m.group(1) in merge_fields else m.group(0), t))


def validate_fields(fields: dict[str, str | None], spec: dict[str, Any], merge_fields: dict[str, Any], *,
                    banned: list[str] | None = None, rules: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Issues for one variant: missing required fields, over-limit fields, unknown or disallowed tokens,
    ALL-CAPS shouting and brand-banned phrases. Each issue: {field, rule, status, message, over_by}."""
    issues: list[dict[str, Any]] = []
    rules = rules or {}
    allow = set(rules.get("caps_allowlist", []))
    url_chars = spec.get("url_chars")
    for name, fs in (spec.get("fields") or {}).items():
        value = (fields.get(name) or "").strip()
        label = fs.get("label", name)
        if not value:
            if fs.get("required"):
                issues.append({"field": name, "rule": "required", "status": "fail", "message": f"{label} is required.", "over_by": 0})
            continue
        n = measure(value, merge_fields, url_chars)
        if n > int(fs["max_chars"]):
            issues.append({"field": name, "rule": "max_chars", "status": "fail", "over_by": n - int(fs["max_chars"]),
                           "message": f"{label} is {n} chars (limit {fs['max_chars']}, merge tokens at reserved width)."})
        elif fs.get("recommended_max_chars") and n > int(fs["recommended_max_chars"]):
            issues.append({"field": name, "rule": "recommended_chars", "status": "warn", "over_by": n - int(fs["recommended_max_chars"]),
                           "message": f"{label} is {n} chars; {fs['recommended_max_chars']} or fewer performs better."})
        tokens = TOKEN.findall(value)
        unknown = sorted({t for t in tokens if t not in merge_fields})
        if unknown:
            issues.append({"field": name, "rule": "unknown_token", "status": "fail", "over_by": 0,
                           "message": f"{label} uses undefined merge fields {unknown}; allowed: {sorted(merge_fields)}."})
        if tokens and not fs.get("merge_fields", False):
            issues.append({"field": name, "rule": "token_not_allowed", "status": "fail", "over_by": 0,
                           "message": f"{label} cannot contain merge fields on this channel."})
        caps = [w for w in re.findall(r"\b[A-Z]{3,}\b", TOKEN.sub("", value)) if w not in allow]
        if len(caps) > int(rules.get("max_caps_words", 1)):
            issues.append({"field": name, "rule": "all_caps", "status": "warn", "over_by": 0, "message": f"{label} shouts in ALL CAPS: {caps[:4]}"})
        hits = [p for p in banned or [] if p.lower() in value.lower()]
        if hits:
            issues.append({"field": name, "rule": "banned_phrase", "status": "fail", "over_by": 0, "message": f"{label} uses brand-banned phrases {hits}."})
    return issues


def _cut_words(text: str, limit: int, size: Any) -> str:
    parts = re.split(r"(\s+)", text.strip())
    while len(parts) > 1 and size("".join(parts)) > limit:
        parts = parts[:-2] if len(parts) >= 2 else []
    out = "".join(parts).rstrip(" ,;:-")
    if size(out) > limit and not TOKEN.search(out):  # one very long word
        out = out[:limit]
    return out


def trim_to_limit(text: str, limit: int, merge_fields: dict[str, Any], url_chars: int | None = None) -> str:
    """Drop trailing sentences, then trailing words, until the budgeted length fits; tokens stay intact."""
    def size(t: str) -> int:
        return measure(t, merge_fields, url_chars)

    text = (text or "").strip()
    if size(text) <= limit:
        return text
    pieces = re.split(r"(?<=[.!?])(\s+)", text)
    sents, seps = pieces[0::2], pieces[1::2]
    while len(sents) > 1 and size("".join(s + (seps[i] if i < len(sents) - 1 else "") for i, s in enumerate(sents))) > limit:
        sents.pop()
    out = "".join(s + (seps[i] if i < len(sents) - 1 else "") for i, s in enumerate(sents)).strip()
    return out if size(out) <= limit else _cut_words(out, limit, size)


def deterministic_fix(fields: dict[str, str | None], spec: dict[str, Any], merge_fields: dict[str, Any], *,
                      default_cta: str | None) -> tuple[dict[str, str | None], list[str]]:
    """Make a variant valid without a model: drop undefined/disallowed tokens, fill required short
    fields, trim over-limit fields. Returns (fields, notes)."""
    fixed = dict(fields)
    notes: list[str] = []
    url_chars = spec.get("url_chars")
    for name, fs in (spec.get("fields") or {}).items():
        value = (fixed.get(name) or "").strip()
        allowed = fs.get("merge_fields", False)
        bad = [t for t in TOKEN.findall(value) if t not in merge_fields or not allowed]
        if bad:
            def tok(m: re.Match) -> str:
                key = m.group(1)
                if key in merge_fields and allowed:
                    return m.group(0)
                return str(merge_fields[key]["fallback"]) if key in merge_fields else ""

            value = re.sub(r"[ \t]{2,}", " ", TOKEN.sub(tok, value)).strip()
            value = re.sub(r"\s+([,.!?;:])", r"\1", value)
            notes.append(f"{name}: replaced undefined or disallowed merge fields {sorted(set(bad))}")
        if not value and fs.get("required"):
            if name == "cta_text" and default_cta:
                value = default_cta
            elif name in ("subject", "preheader") and fixed.get("body"):
                value = re.split(r"(?<=[.!?])\s+", str(fixed["body"]).strip())[0]
            if value:
                notes.append(f"{name}: filled a missing required field")
        limit = int(fs["max_chars"])
        if value and measure(value, merge_fields, url_chars) > limit:
            value = trim_to_limit(value, limit, merge_fields, url_chars)
            notes.append(f"{name}: trimmed to {limit} chars")
        fixed[name] = value or None
    for name in FIELDS:  # fields this channel does not have
        if name not in (spec.get("fields") or {}) and fixed.get(name):
            fixed[name] = None
    return fixed, notes


def render_tokens(text: str | None, values: dict[str, Any], merge_fields: dict[str, Any]) -> tuple[str | None, list[str]]:
    if text is None:
        return None, []
    used: list[str] = []

    def rep(m: re.Match) -> str:
        key = m.group(1)
        if key not in merge_fields:
            return m.group(0)
        v = (values.get(key) or "").strip()
        if not v:
            used.append(key)
            return str(merge_fields[key]["fallback"])
        return v

    return TOKEN.sub(rep, text), used


def platform_copy(fields: dict[str, str | None], platform_tokens: dict[str, str]) -> dict[str, str]:
    out = {}
    for name in FIELDS:
        v = fields.get(name)
        if v:
            out[name] = TOKEN.sub(lambda m: platform_tokens.get(m.group(1), m.group(0)), v)
    return out


def parse_recipients_csv(text: str, limit: int = 500) -> list[dict[str, Any]]:
    """Rows of a CRM export -> recipients; header names are matched case-insensitively (first_name /
    firstname / first name, company / account / organisation, job_title / title / role)."""
    aliases = {"first_name": ("first_name", "firstname", "first name", "given_name"),
               "company": ("company", "account", "organisation", "organization", "account_name"),
               "job_title": ("job_title", "title", "jobtitle", "job title", "role")}
    reader = csv.DictReader(io.StringIO(text or ""))
    cols = {c.strip().lower(): c for c in reader.fieldnames or []}
    pick = {k: next((cols[a] for a in names if a in cols), None) for k, names in aliases.items()}
    out = []
    for row in reader:
        rec = {k: (row.get(c) or "").strip() or None for k, c in pick.items() if c}
        if any(rec.values()):
            out.append(rec)
        if len(out) >= limit:
            break
    return out


def match_recipients(persona: str, recipients: list[dict[str, Any]], limit: int = 3) -> list[dict[str, Any]]:
    """Recipients whose job title shares words with the persona (best first); else the first `limit`."""
    def words_of(t: str) -> set[str]:
        return {w for w in re.findall(r"[a-z]{3,}", (t or "").lower()) if w not in {"the", "and", "head", "lead"}}

    want = words_of(persona)
    scored = [(len(want & words_of(r.get("job_title") or "")), i, r) for i, r in enumerate(recipients)]
    hits = [r for score, _i, r in sorted(scored, key=lambda x: (-x[0], x[1])) if score > 0]
    return (hits or recipients)[:limit]


def previews_for(fields: dict[str, str | None], recipients: list[dict[str, Any]], spec: dict[str, Any],
                 merge_fields: dict[str, Any], limit: int = 5) -> list[dict[str, Any]]:
    out = []
    url_chars = spec.get("url_chars")
    for r in recipients[:limit]:
        rendered: dict[str, Any] = {}
        fallbacks: list[str] = []
        counts: dict[str, int] = {}
        ok = True
        for name in FIELDS:
            text, used = render_tokens(fields.get(name), r, merge_fields)
            rendered[name] = text
            fallbacks += used
            if text is not None and name in (spec.get("fields") or {}):
                n = measure(text, {}, url_chars)
                counts[name] = n
                ok = ok and n <= int(spec["fields"][name]["max_chars"])
        label = " - ".join(x for x in (r.get("first_name"), r.get("job_title"), r.get("company")) if x) or "recipient"
        out.append({"recipient": label, **rendered, "char_counts": counts, "within_limits": ok, "fallbacks_used": sorted(set(fallbacks))})
    return out


# ----------------------------------------------------------------------------- agent

@register
class LinkedInInMailCopyAgent(BaseAgent):
    spec = AgentSpec(
        id="linkedin_inmail_copy",
        name="LinkedIn InMail Copy Creation",
        category=Category.CONTENT,
        summary="Writes persona-specific LinkedIn InMail / message-ad copy (subject, body, CTA) validated against version-controlled platform limits, with merge-field personalisation previews; the same engine emits X, email or WhatsApp variants.",
        kind=AgentKind.LLM,
        tier=Tier.BALANCED,
        connectors=["ingestion"],
        skills=["platform_constraints", "brand_voice"],
        keywords=["linkedin", "inmail", "message ad", "sponsored message", "b2b outreach", "abm", "sdr", "outbound", "conversation ad"],
        produces=["inmail_variants.csv", "inmail_variants.json"],
        feeds=["content_translation", "experiment_ideation"],
    )
    Input = InMailInput
    Output = InMailOutput
    schemas = {"draft": MessageDraft, "repair": MessageVariant}

    def selftest_input(self) -> dict[str, Any]:
        return {"overview": "Invite independent brokers and commercial risk managers to a 30-minute briefing on Meridian Mutual's "
                            "2027 flood cover expansion and resilience credits, with a toolkit for client conversations.",
                "audience": "US insurance brokers and risk managers", "personas": ["Principal Broker", "Risk Manager"],
                "call_to_action": "Book a briefing seat", "cta_url": "https://www.meridian-mutual.example/webinars/climate-ready-home",
                "sender_name": "Jordan Blake", "sender_title": "Head of Communications",
                "recipients": [{"company": "Fabrikam Re", "job_title": "Risk Manager"}],
                "recipients_csv": "sample_data/crm/contacts.csv"}

    def stages(self):
        return [("context", self.context), ("draft", self.draft), ("validate_and_repair", self.validate_and_repair), ("render", self.render)]

    def context(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp = state["inputs"]
        pc = ctx.skill("platform_constraints")
        channels = pc.get("channels") or {}
        if inp["channel"] not in channels:
            raise ValueError(f"Unknown channel '{inp['channel']}'. Configured in platform_constraints: {sorted(channels)}")
        spec = channels[inp["channel"]]
        checks: list[Check] = []
        sender = inp.get("sender_name")
        if "sender_name" in (spec.get("required_inputs") or []) and not sender:
            sender = (ctx.brand.get("media_contact") or {}).get("name") or ctx.brand.get("name")
            checks.append(Check(rule="input.sender", status="warn", severity="medium",
                                message=f"{spec['label']} needs a sender profile; defaulted to '{sender}'."))
        brief = ctx.upstream_output("campaign_brief_generation") or {}
        utm = ctx.upstream_output("utm_generation") or {}
        cta_url = inp.get("cta_url")
        link = next((l["url"] for l in utm.get("links", []) if l.get("channel") == spec.get("utm_channel")), None)
        if link and (not cta_url or cta_url.split("?")[0] == link.split("?")[0]):
            cta_url = link
        personas = [p.strip() for p in inp.get("personas") or [] if p.strip()][:6] or \
            [inp.get("audience") or (brief.get("target_audience") or {}).get("primary") or "B2B decision-maker"]
        merge = pc.get("merge_fields") or {}
        limits = [f"{name} ({fs.get('label', name)}) - max {fs['max_chars']} characters"
                  + (f", aim for {fs['recommended_max_chars']} or fewer" if fs.get("recommended_max_chars") else "")
                  + ("" if fs.get("required") else ", optional") + (", merge fields allowed" if fs.get("merge_fields") else ", no merge fields")
                  for name, fs in (spec.get("fields") or {}).items()]
        tokens = [f"{m['token']} - falls back to '{m['fallback']}' when unknown (reserve {m['reserve_chars']} chars)" for m in merge.values()]
        return {"data": {"spec": spec, "merge": merge, "rules": pc.get("rules") or {}, "version": str(pc.get("version", "")),
                         "sender": sender, "cta_url": cta_url, "personas": personas, "limits": limits, "tokens": tokens,
                         "key_messages": list(brief.get("key_messages") or [])[:4]}, "checks": checks}

    def draft(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        spec = data["spec"]
        draft = ctx.generate("draft", MessageDraft, overview=inp["overview"], audience=inp.get("audience"), personas=data["personas"],
                             channel_label=spec["label"], limits=data["limits"], tokens=data["tokens"], fields=list(spec["fields"]),
                             guidance=spec.get("guidance") or [], call_to_action=inp.get("call_to_action"), cta_url=data["cta_url"],
                             sender_name=data["sender"], sender_title=inp.get("sender_title"), extra_context=inp.get("extra_context"),
                             key_messages=data["key_messages"])
        variants = draft.variants[: len(data["personas"])]  # one per persona, labels exactly as given
        while len(variants) < len(data["personas"]):
            variants.append(variants[-1].model_copy())
        for v, label in zip(variants, data["personas"]):
            v.persona = label
        return {"data": {"variants": [v.model_dump() for v in variants]}}

    def validate_and_repair(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        data, channel = state["data"], state["inputs"]["channel"]
        spec, merge, rules = data["spec"], data["merge"], data["rules"]
        banned = ctx.brand.get("banned_phrases", [])
        final, checks = [], []
        for raw in data["variants"]:
            v = MessageVariant.model_validate(raw)
            fields = {k: getattr(v, k) for k in FIELDS}
            repair: list[str] = []
            issues = validate_fields(fields, spec, merge, banned=banned, rules=rules)
            if any(i["status"] == "fail" for i in issues) and not ctx.llm.offline:  # one model repair pass
                fixed = ctx.generate("repair", MessageVariant, draft=v.model_dump(), channel_label=spec["label"], limits=data["limits"],
                                     tokens=data["tokens"], findings=[i["message"] for i in issues if i["status"] == "fail"])
                fields = {k: getattr(fixed, k) for k in FIELDS}
                repair.append("model")
                issues = validate_fields(fields, spec, merge, banned=banned, rules=rules)
            notes: list[str] = []
            if any(i["status"] == "fail" and i["rule"] != "banned_phrase" for i in issues):
                fields, notes = deterministic_fix(fields, spec, merge, default_cta=spec.get("default_cta"))
                repair.append("trimmed")
                issues = validate_fields(fields, spec, merge, banned=banned, rules=rules)
            for name in FIELDS:
                if name not in (spec.get("fields") or {}):
                    fields[name] = None
            valid = not any(i["status"] == "fail" for i in issues)
            for i in issues:
                checks.append(Check(rule=f"{channel}.{i['field']}.{i['rule']}", status=i["status"],
                                    severity="high" if i["status"] == "fail" else "low", target=v.persona, message=i["message"],
                                    ref=f"platform_constraints {data['version']}"))
            if notes:
                checks.append(Check(rule="copy.deterministic_trim", status="warn", severity="medium", target=v.persona,
                                    message="Deterministic fix applied: " + "; ".join(notes)))
            if not issues:
                checks.append(Check(rule="copy.limits", status="pass", severity="low", target=v.persona,
                                    message=f"All fields within {spec['label']} limits."))
            final.append({"persona": v.persona, "fields": fields, "issues": [i["message"] for i in issues], "valid": valid,
                          "repair": "+".join(repair) if repair else "none"})
        return {"data": {"final": final}, "checks": checks}

    def render(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        spec, merge = data["spec"], data["merge"]
        checks: list[Check] = []
        recipients = [r if isinstance(r, dict) else dict(r) for r in inp.get("recipients") or []]
        if inp.get("recipients_csv"):
            try:
                rows = parse_recipients_csv(ctx.connector("ingestion").load(path=inp["recipients_csv"]).text)
                recipients += rows
                checks.append(Check(rule="input.recipients_csv", status="info", severity="low",
                                    message=f"{len(rows)} recipients read from {inp['recipients_csv']} for personalised previews."))
            except Exception as exc:
                checks.append(Check(rule="input.recipients_csv", status="warn", severity="low",
                                    message=f"Could not read {inp['recipients_csv']}: {type(exc).__name__}: {exc}"))
        outs: list[MessageOut] = []
        for f in data["final"]:
            fields = f["fields"]
            counts = {n: measure(fields.get(n), merge, spec.get("url_chars")) for n in spec["fields"] if fields.get(n)}
            pv = previews_for(fields, match_recipients(f["persona"], recipients), spec, merge)
            over = [p["recipient"] for p in pv if not p["within_limits"]]
            if over:
                checks.append(Check(rule="preview.limits", status="warn", severity="medium", target=f["persona"],
                                    message=f"Personalised copy exceeds a limit for: {over} - shorten the copy or the merge value."))
            used = sorted({t for n in FIELDS for t in TOKEN.findall(fields.get(n) or "") if t in merge})
            outs.append(MessageOut(persona=f["persona"], channel=inp["channel"], subject=fields.get("subject"), preheader=fields.get("preheader"),
                                   body=fields.get("body") or "", cta_text=fields.get("cta_text"), cta_url=data["cta_url"], char_counts=counts,
                                   limits={n: int(fs["max_chars"]) for n, fs in spec["fields"].items()}, valid=f["valid"], issues=f["issues"],
                                   repair=f["repair"], merge_fields_used=used,
                                   platform_copy=platform_copy(fields, spec.get("platform_tokens") or {}),
                                   previews=[Preview.model_validate(p) for p in pv]))
        out = InMailOutput(channel=inp["channel"], channel_label=spec["label"], constraints_version=data["version"], sender_name=data["sender"],
                           variants=outs, all_valid=all(o.valid for o in outs), merge_fields=[m["token"] for m in merge.values()])
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["persona", "channel", "subject", "preheader", "body", "cta_text", "cta_url", "valid", "repair", "char_counts"])
        for o in outs:
            w.writerow([o.persona, o.channel, o.subject or "", o.preheader or "", o.body, o.cta_text or "", o.cta_url or "", o.valid, o.repair,
                        "; ".join(f"{k}={v}/{o.limits.get(k)}" for k, v in o.char_counts.items())])
        dumped = out.model_dump(mode="json")
        artifacts = [ctx.artifacts.write_text("inmail_variants.csv", buf.getvalue(), "text/csv", f"{spec['label']} variants"),
                     ctx.artifacts.write_json("inmail_variants.json", dumped, "Variants with personalised previews")]
        n_ok = sum(1 for o in outs if o.valid)
        first = outs[0]
        summary = (f"{len(outs)} {spec['label']} variant(s) for {', '.join(o.persona for o in outs)}; {n_ok}/{len(outs)} within platform limits"
                   + (f"; subject: “{first.subject}”" if first.subject else "") + (f"; {sum(len(o.previews) for o in outs)} personalised previews" if recipients else ""))
        return {"output": dumped, "artifacts": artifacts, "checks": checks, "summary": summary}
