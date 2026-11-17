"""Press Release Creation - model drafts the news, code owns everything that must be exact.

* Boilerplate, media contact and legal disclaimers live in a versioned YAML record
  (config/skills/press_boilerplate.yaml, falling back to brand_voice.media_contact) and are inserted
  verbatim - never written by the model.
* The AP dateline is built deterministically from the location and release date.
* A deterministic AP-style / structure validator checks the dateline format, headline length, the
  inverted-pyramid lead (who / what / when in the first paragraph), quote attribution, word count,
  the end mark (###) and the presence of boilerplate + media contact.
* A legal check scans the model-written text for forward-looking terms (expects, anticipates, plans,
  will, forecast ...) and appends the safe-harbour disclaimer whenever one is found.
* Distribution only through an approval-gated PendingAction to the pluggable NewswireConnector
  (Business Wire, PR Newswire, newsroom CMS or the local file provider).
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from ...core import AgentKind, AgentSpec, BaseAgent, Category, Check, PendingAction, RunContext, Tier, register
from ...core.textutil import normalize_ws, slugify, word_count


class PressReleaseInput(BaseModel):
    overview: str = Field(description="Required. What is being announced, with the facts that may be used", json_schema_extra={"ui": "textarea"})
    target_audience: str | None = Field(default=None, description="Who the release is for (media, customers, investors ...)")
    date: str | None = Field(default=None, description="Release date, YYYY-MM-DD (defaults to today, or the embargo date)")
    call_to_action: str | None = Field(default=None, description="What readers should do next (URL welcome)")
    location: str | None = Field(default=None, description="Dateline city, e.g. 'Springfield, Illinois', 'Chicago' or 'London'")
    spokesperson: str | None = Field(default=None, description="Quoted spokesperson as 'Name, Title', e.g. 'Tom Ellery, Claims Director'")
    embargo: str | None = Field(default=None, description="Embargo lift time, ISO 8601 with offset, e.g. 2026-10-21T09:00:00-05:00")
    approved_quotes: list[str] = Field(default_factory=list, description="Optional pre-approved quotes the release may use verbatim")
    regions: list[str] = Field(default_factory=list, description="Newswire distribution regions (default from the boilerplate record)")
    distribute: bool = Field(default=True, description="Propose newswire distribution (held for approval)")


class Quote(BaseModel):
    text: str
    speaker: str
    title: str = ""


class PressReleaseDraft(BaseModel):
    headline: str
    subheadline: str = ""
    lead: str = Field(description="First paragraph without the dateline: who, what, when, where, why")
    body_paragraphs: list[str] = Field(default_factory=list)
    quotes: list[Quote] = Field(default_factory=list)
    cta: str = ""


class PressReleaseOutput(BaseModel):
    headline: str
    subheadline: str
    dateline: str
    body_markdown: str = Field(description="Release body from the dateline to the end mark (headline and subheadline are separate fields)")
    quotes: list[Quote]
    boilerplate: str
    media_contact: dict[str, str]
    word_count: int
    forward_looking_detected: bool
    forward_looking_terms: list[str] = Field(default_factory=list)
    release_label: str = ""
    release_date: str = ""
    embargo: str | None = None
    boilerplate_version: str = ""
    validation_passed: bool = True


# ----------------------------------------------------------------------------- pure helpers

def parse_location(location: str, record: dict[str, Any]) -> tuple[str, str | None]:
    """'Springfield, Illinois' -> ('SPRINGFIELD', 'Ill.'); 'Chicago' -> ('CHICAGO', None); 'London, UK' -> ('LONDON', None)."""
    parts = [p.strip() for p in (location or "").split(",") if p.strip()]
    if not parts:
        return "", None
    city = parts[0].upper()
    standalone = {c.upper() for c in record.get("ap_standalone_cities", [])}
    if city in standalone:
        return city, None
    region = parts[1] if len(parts) > 1 else None
    if not region:
        return city, None
    states = record.get("ap_states", {})
    postal = record.get("ap_postal_codes", {})
    key = region.rstrip(".")
    if key.upper() in postal:
        return city, states.get(postal[key.upper()], postal[key.upper()])
    for name, abbr in states.items():
        if key.lower() == name.lower() or key.lower() == str(abbr).rstrip(".").lower():
            return city, abbr
    return city, region  # international: CITY, Country


def ap_date(d: date, months: list[str]) -> str:
    return f"{months[d.month - 1]} {d.day}, {d.year}"


def build_dateline(location: str, d: date, record: dict[str, Any]) -> str:
    city, region = parse_location(location, record)
    months = record.get("ap_months") or ["Jan.", "Feb.", "March", "April", "May", "June", "July", "Aug.", "Sept.", "Oct.", "Nov.", "Dec."]
    place = f"{city}, {region}" if region else city
    return f"{place}, {ap_date(d, months)} —"


DATELINE_RX = re.compile(r"^[A-Z][A-Z .'-]+,(?: [A-Z][A-Za-z.]*(?: [A-Z][A-Za-z.]*)*,)? "
                         r"(?:Jan\.|Feb\.|March|April|May|June|July|Aug\.|Sept\.|Oct\.|Nov\.|Dec\.) \d{1,2}, \d{4} —$")


def validate_dateline(dateline: str) -> bool:
    return bool(DATELINE_RX.match(dateline or ""))


def parse_spokesperson(value: str | None) -> tuple[str, str]:
    if not value or not value.strip():
        return "", ""
    name, _, title = value.partition(",")
    return name.strip(), title.strip()


def format_quote(q: Quote, organisation: str, verb: str = "said") -> str:
    """AP-style attributed quote paragraph: “Text,” said Name, Title at Org."""
    text = normalize_ws(q.text).strip('"“”\' ')
    if text.endswith("."):
        text = text[:-1] + ","
    elif not text.endswith(("?", "!", ",")):
        text += ","
    who = q.speaker.strip()
    title = q.title.strip()
    tail = f", {title}" if title else ""
    if title and organisation and organisation.lower() not in title.lower():
        tail += f" at {organisation}"
    return f"“{text}” {verb} {who}{tail}."


def find_terms(text: str, terms: list[str]) -> list[str]:
    found = []
    for t in terms:
        if re.search(rf"\b{re.escape(t)}\b", text or "", flags=re.I) and t not in found:
            found.append(t)
    return found


def sentences_with(text: str, terms: list[str], limit: int = 6) -> list[str]:
    from ...core.textutil import sentences

    out = []
    for s in sentences(text):
        if any(re.search(rf"\b{re.escape(t)}\b", s, flags=re.I) for t in terms):
            out.append(s)
        if len(out) >= limit:
            break
    return out


def check_lead(lead: str, organisation_names: list[str], verbs: list[str], release_day: date, months: list[str]) -> dict[str, Any]:
    """Inverted pyramid: who / what / when in the first paragraph."""
    low = (lead or "").lower()
    who = any(n.lower() in low for n in organisation_names if n)
    what = any(re.search(rf"\b{re.escape(v)}\b", low) for v in verbs)
    month_names = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]
    when = bool(re.search(r"\b(today|tomorrow|yesterday|this week|this month|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", low)
                or any(m in low for m in month_names) or any(m.lower().rstrip(".") in low for m in months)
                or re.search(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b", low)
                or str(release_day.year) in low)
    return {"who": who, "what": what, "when": when, "words": word_count(lead)}


def media_contact_lines(mc: dict[str, Any]) -> list[str]:
    lines = []
    name = ", ".join(x for x in (mc.get("name"), mc.get("title")) if x)
    if name:
        lines.append(name)
    for k in ("email", "phone"):
        if mc.get(k):
            lines.append(str(mc[k]))
    return lines


def assemble_body(dateline: str, draft: PressReleaseDraft, organisation: str, *, boilerplate_heading: str, boilerplate: str,
                  fls: dict[str, str] | None, extra_disclaimers: list[str], media_contact: dict[str, Any], end_mark: str) -> str:
    """Deterministic layout from the dateline to the end mark."""
    paras = [f"{dateline} {normalize_ws(draft.lead)}"]
    body = [normalize_ws(p) for p in draft.body_paragraphs if normalize_ws(p)]
    quotes = [format_quote(q, organisation, "said" if i == 0 else "added") for i, q in enumerate(draft.quotes) if q.text.strip()]
    # AP / wire convention: first quote by the second or third paragraph, a second quote later in the body.
    if body:
        paras.append(body[0])
    if quotes:
        paras.append(quotes[0])
    paras += body[1:]
    paras += quotes[1:]
    if normalize_ws(draft.cta):
        paras.append(normalize_ws(draft.cta))
    out = "\n\n".join(paras)
    out += f"\n\n## {boilerplate_heading}\n\n{normalize_ws(boilerplate)}"
    for d in extra_disclaimers:
        out += f"\n\n_{normalize_ws(d)}_"
    if fls:
        out += f"\n\n## {fls.get('heading', 'Forward-looking statements')}\n\n{normalize_ws(fls.get('text', ''))}"
    mc = media_contact_lines(media_contact)
    if mc:
        out += "\n\n## Media contact\n\n" + "  \n".join(mc)
    return out + f"\n\n{end_mark}\n"


def embargo_label(embargo: str | None, months: list[str]) -> tuple[str | None, datetime | None, str | None]:
    """Returns (display, parsed, problem)."""
    if not embargo:
        return None, None, None
    try:
        dt = datetime.fromisoformat(embargo.strip().replace("Z", "+00:00"))
    except ValueError:
        return embargo.strip(), None, f"Embargo '{embargo}' is not ISO 8601 (e.g. 2026-10-21T09:00:00-05:00)."
    tz = dt.strftime("%z")
    tzs = f"UTC{tz[:3]}:{tz[3:]}" if tz else "local time"
    hour = dt.strftime("%I").lstrip("0") or "12"
    minute = dt.strftime("%M")
    ampm = "a.m." if dt.hour < 12 else "p.m."
    clock = f"{hour}{'' if minute == '00' else ':' + minute} {ampm}"
    problem = None if dt.tzinfo else "Embargo has no timezone offset - wires need an explicit zone."
    return f"{clock} {tzs}, {ap_date(dt.date(), months)}", dt, problem


# ----------------------------------------------------------------------------- agent

@register
class PressReleaseAgent(BaseAgent):
    spec = AgentSpec(
        id="press_release_creation",
        name="Press Release Creation",
        category=Category.CONTENT,
        summary="Writes AP-style press releases from a brief overview with your versioned boilerplate, media contact and legal disclaimers, "
                "validates structure and forward-looking statements, and proposes gated newswire distribution.",
        kind=AgentKind.LLM,
        tier=Tier.BALANCED,
        connectors=["newswire"],
        skills=["press_boilerplate", "brand_voice"],
        keywords=["press release", "news release", "announcement", "media release", "newswire", "pr", "launch announcement",
                  "executive appointment"],
        produces=["press_release.md", "press_release.json"],
        feeds=["content_translation", "content_adaptation", "quote_extraction"],
        side_effects=["newswire.distribute"],
    )
    Input = PressReleaseInput
    Output = PressReleaseOutput
    schemas = {"draft": PressReleaseDraft, "repair": PressReleaseDraft}

    def selftest_input(self) -> dict[str, Any]:
        return {"overview": "From January 2027, Meridian Mutual members who make verified resilience upgrades - impact-rated roofs, "
                            "roof-to-wall straps, ember-resistant vents and leak detection with automatic water shut-off - earn credits "
                            "worth up to 12% of their premium. Optional flood cover is extended to every state in which Meridian writes "
                            "home insurance, with a lower-cost option for homes outside mapped flood zones. Every 2027 renewal notice "
                            "explains the three biggest factors behind the price.",
                "target_audience": "Homeowners, insurance trade media and independent brokers",
                "date": "2026-10-20", "call_to_action": "Check which upgrades qualify at www.meridian-mutual.example/resilience",
                "location": "Springfield, Illinois", "spokesperson": "Dr. Asha Raman, Head of Risk Analytics"}

    def stages(self):
        return [("prepare", self.prepare), ("draft", self.draft), ("validate", self.validate), ("finalise", self.finalise)]

    # -- prepare: the record, dateline and release metadata (deterministic) ------------------------------
    def prepare(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp = state["inputs"]
        record = ctx.skill("press_boilerplate", default={})
        checks: list[Check] = []
        warnings: list[str] = []
        months = record.get("ap_months") or ["Jan.", "Feb.", "March", "April", "May", "June", "July", "Aug.", "Sept.", "Oct.", "Nov.", "Dec."]
        emb_display, emb_dt, emb_problem = embargo_label(inp.get("embargo"), months)
        if emb_problem:
            checks.append(Check(rule="embargo.format", status="warn", severity="medium", message=emb_problem))
        release_day: date | None = None
        if inp.get("date"):
            try:
                release_day = date.fromisoformat(str(inp["date"]).strip()[:10])
            except ValueError:
                checks.append(Check(rule="date.format", status="warn", severity="medium",
                                    message=f"Release date '{inp['date']}' is not YYYY-MM-DD; using today."))
        if emb_dt is not None:
            if release_day and release_day != emb_dt.date():
                checks.append(Check(rule="embargo.date", status="warn", severity="medium",
                                    message=f"Release date {release_day} differs from the embargo date {emb_dt.date()}; dateline uses the embargo date."))
            release_day = emb_dt.date()
            now = datetime.now(timezone.utc)
            if emb_dt.tzinfo and emb_dt < now:
                checks.append(Check(rule="embargo.past", status="warn", severity="high",
                                    message=f"Embargo {emb_display} is already in the past."))
        release_day = release_day or date.today()
        location = inp.get("location") or record.get("headquarters_dateline") or ""
        if not inp.get("location"):
            warnings.append(f"No location given - dateline uses headquarters ({location}).")
        dateline = build_dateline(location, release_day, record) if location else ""
        organisation = record.get("organisation") or ctx.brand.get("name", "")
        mc = {k: str(v) for k, v in (record.get("media_contact") or {}).items() if v} or \
             {k: str(v) for k, v in (ctx.brand.get("media_contact") or {}).items() if v}
        spokesperson = parse_spokesperson(inp.get("spokesperson"))
        approved = list(inp.get("approved_quotes") or [])
        upstream_quotes = (ctx.upstream_output("quote_extraction") or {}).get("quotes") or []
        for q in upstream_quotes[:5]:
            if q.get("text") and q.get("verified", True):
                approved.append(f"{q['text']} - {q.get('speaker') or 'unattributed'}")
        brief = ctx.upstream_output("campaign_brief_generation") or {}
        return {"data": {"record": record, "dateline": dateline, "release_day": release_day.isoformat(), "organisation": organisation,
                         "media_contact": mc, "spokesperson": list(spokesperson), "approved_quotes": approved,
                         "key_messages": brief.get("key_messages", []), "embargo_display": emb_display,
                         "release_label": f"{record.get('style', {}).get('embargo_label', 'EMBARGOED UNTIL')} {emb_display}" if emb_display
                         else record.get("style", {}).get("release_label", "FOR IMMEDIATE RELEASE")},
                "checks": checks, "warnings": warnings}

    # -- draft (model) -------------------------------------------------------------------------------
    def draft(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        rec = data["record"]
        style = rec.get("style", {})
        name, title = data["spokesperson"]
        draft = ctx.generate("draft", PressReleaseDraft, overview=inp["overview"], target_audience=inp.get("target_audience"),
                             call_to_action=inp.get("call_to_action"), organisation=data["organisation"],
                             release_date=data["release_day"], dateline=data["dateline"], spokesperson_name=name,
                             spokesperson_title=title, approved_quotes=data["approved_quotes"], key_messages=data["key_messages"],
                             headline_max=style.get("headline_warn_chars", 80), lead_max=style.get("lead_max_words", 45),
                             min_words=style.get("word_count", {}).get("min", 300), max_words=style.get("word_count", {}).get("max", 800))
        return {"data": {"draft": self._reconcile(draft, data).model_dump()}}

    def _reconcile(self, draft: PressReleaseDraft, data: dict[str, Any]) -> PressReleaseDraft:
        """Deterministic fixes: spokesperson attribution, dateline the model may have repeated, stray end marks."""
        name, title = data["spokesperson"]
        quotes = [q for q in draft.quotes if q.text.strip()][:3]
        if name:
            if quotes:
                quotes[0] = Quote(text=quotes[0].text, speaker=name, title=title or quotes[0].title)
        lead = normalize_ws(draft.lead)
        dl = data["dateline"].rstrip(" —")
        if dl and lead.upper().startswith(dl.split(",")[0]):
            lead = re.sub(r"^[A-Z][A-Z .'-]+,[^—–-]{0,60}[—–-]+\s*", "", lead)
        headline = normalize_ws(draft.headline).rstrip(".")
        paras = [normalize_ws(p) for p in draft.body_paragraphs if normalize_ws(p) and normalize_ws(p) not in ("###", "-30-")]
        return PressReleaseDraft(headline=headline, subheadline=normalize_ws(draft.subheadline), lead=lead,
                                 body_paragraphs=paras, quotes=quotes, cta=normalize_ws(draft.cta))

    # -- validate (deterministic) + one repair pass when a model is configured ---------------------------
    def _validate(self, draft: PressReleaseDraft, data: dict[str, Any], brand: dict[str, Any]) -> tuple[list[Check], dict[str, Any]]:
        rec = data["record"]
        style = rec.get("style", {})
        months = rec.get("ap_months") or []
        checks: list[Check] = []
        org_names = [data["organisation"]] + list(rec.get("name_variants", []))
        # dateline
        ok = validate_dateline(data["dateline"])
        checks.append(Check(rule="ap.dateline", status="pass" if ok else "fail", severity="high", ref="AP Stylebook: datelines",
                            message=f"Dateline '{data['dateline']}' " + ("follows AP format (CITY, State abbr., Mon. D, YYYY —)." if ok
                                                                       else "does not follow AP format.")))
        # headline
        h = draft.headline
        hmax, hwarn = style.get("headline_max_chars", 100), style.get("headline_warn_chars", 80)
        st = "fail" if len(h) > hmax else ("warn" if len(h) > hwarn else "pass")
        checks.append(Check(rule="headline.length", status=st, severity="medium", target="headline",
                            message=f"Headline is {len(h)} chars (warn > {hwarn}, max {hmax})."))
        caps = [w for w in re.findall(r"\b[A-Z]{5,}\b", h)]
        if caps:
            checks.append(Check(rule="headline.caps", status="warn", severity="low", target="headline", message=f"ALL-CAPS words: {caps}"))
        if len(draft.subheadline) > style.get("subheadline_max_chars", 180):
            checks.append(Check(rule="subheadline.length", status="warn", severity="low", target="subheadline",
                                message=f"Subheadline is {len(draft.subheadline)} chars (max {style.get('subheadline_max_chars', 180)})."))
        # inverted pyramid lead
        lead = check_lead(draft.lead, org_names, style.get("announcement_verbs", []), date.fromisoformat(data["release_day"]), months)
        missing = [k for k in ("who", "what", "when") if not lead[k]]
        long_lead = lead["words"] > style.get("lead_max_words", 45) or lead["words"] < style.get("lead_min_words", 12)
        st = "fail" if len(missing) >= 2 else ("warn" if missing or long_lead else "pass")
        checks.append(Check(rule="structure.inverted_pyramid", status=st, severity="high" if st == "fail" else "medium", target="lead",
                            ref="AP Stylebook: leads",
                            message=(f"Lead ({lead['words']} words) " + ("covers who, what and when." if not missing else f"is missing: {', '.join(missing)}.")
                                     + (f" Aim for {style.get('lead_min_words', 12)}-{style.get('lead_max_words', 45)} words." if long_lead else "")),
                            data=lead))
        # quotes
        attributed = [q for q in draft.quotes if q.text.strip() and len(q.speaker.split()) >= 2]
        unattributed = [q.text[:40] for q in draft.quotes if q.text.strip() and len(q.speaker.split()) < 2]
        st = "pass" if attributed and not unattributed else ("warn" if attributed else "fail")
        checks.append(Check(rule="structure.quote_attribution", status=st, severity="high" if st == "fail" else "medium",
                            message=(f"{len(attributed)} attributed quote(s)." if st == "pass" else
                                     "No attributed quote (named speaker + title required)." if st == "fail" else
                                     f"Quotes without a full speaker name: {unattributed}")))
        if any(not q.title.strip() for q in attributed):
            checks.append(Check(rule="structure.quote_title", status="warn", severity="low", message="A quoted speaker has no title."))
        # brand
        text = " ".join([draft.headline, draft.subheadline, draft.lead, *draft.body_paragraphs, *(q.text for q in draft.quotes), draft.cta])
        banned = [p for p in brand.get("banned_phrases", []) if p.lower() in text.lower()]
        if banned:
            checks.append(Check(rule="brand.banned_phrases", status="fail", severity="high", message=f"Banned phrases: {banned}"))
        terms = find_terms(text, rec.get("forward_looking_terms", []))
        return checks, {"lead": lead, "fls_terms": terms, "fls_sentences": sentences_with(text, terms), "text": text}

    def validate(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        data = state["data"]
        draft = PressReleaseDraft.model_validate(data["draft"])
        checks, facts = self._validate(draft, data, ctx.brand)
        failures = [f"{c.rule}: {c.message}" for c in checks if c.status == "fail" and c.rule != "ap.dateline"]
        if failures and not ctx.llm.offline:
            repaired = ctx.generate("repair", PressReleaseDraft, findings=failures, draft=draft.model_dump(),
                                    organisation=data["organisation"], release_date=data["release_day"],
                                    headline_max=data["record"].get("style", {}).get("headline_warn_chars", 80))
            draft = self._reconcile(repaired, data)
            checks, facts = self._validate(draft, data, ctx.brand)
        return {"data": {"draft": draft.model_dump(), "facts": {k: v for k, v in facts.items() if k != "text"}}, "checks": checks}

    # -- finalise: assemble, legal, structure, distribution ----------------------------------------------
    def finalise(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        rec = data["record"]
        style = rec.get("style", {})
        draft = PressReleaseDraft.model_validate(data["draft"])
        facts = data["facts"]
        checks: list[Check] = []
        disclaimers = rec.get("disclaimers", {}) or {}
        fls_terms = facts["fls_terms"]
        fls = disclaimers.get("forward_looking") if fls_terms else None
        if fls_terms:
            if fls and fls.get("text"):
                checks.append(Check(rule="legal.forward_looking", status="warn", severity="medium", ref="PSLRA safe harbor",
                                    message=f"Forward-looking terms {fls_terms} found; the safe-harbour disclaimer was appended - legal review advised.",
                                    data={"terms": fls_terms, "sentences": facts.get("fls_sentences", [])}))
            else:
                checks.append(Check(rule="legal.forward_looking", status="fail", severity="critical", ref="PSLRA safe harbor",
                                    message=f"Forward-looking terms {fls_terms} found but the boilerplate record has no disclaimer text."))
        else:
            checks.append(Check(rule="legal.forward_looking", status="pass", severity="low", message="No forward-looking statements detected."))
        boiler = (rec.get("boilerplate") or {})
        bp_text = normalize_ws(boiler.get("text", "") if isinstance(boiler, dict) else str(boiler))
        bp_heading = boiler.get("heading", f"About {data['organisation']}") if isinstance(boiler, dict) else f"About {data['organisation']}"
        extra = list(disclaimers.get("always") or [])
        body = assemble_body(data["dateline"], draft, data["organisation"], boilerplate_heading=bp_heading, boilerplate=bp_text,
                             fls=fls if fls and fls.get("text") else None, extra_disclaimers=extra,
                             media_contact=data["media_contact"], end_mark=style.get("end_mark", "###"))
        wc = word_count(body)
        lo, hi = style.get("word_count", {}).get("min", 300), style.get("word_count", {}).get("max", 800)
        checks.append(Check(rule="structure.word_count", status="pass" if lo <= wc <= hi else "warn", severity="low",
                            message=f"{wc} words (recommended {lo}-{hi})."))
        end = style.get("end_mark", "###")
        checks.append(Check(rule="structure.end_mark", status="pass" if body.rstrip().endswith(end) else "fail", severity="medium",
                            message=f"End mark '{end}' " + ("present." if body.rstrip().endswith(end) else "missing.")))
        checks.append(Check(rule="structure.boilerplate", status="pass" if bp_text else "fail", severity="critical" if not bp_text else "low",
                            message="Boilerplate inserted from the versioned record." if bp_text else "No boilerplate in config/skills/press_boilerplate.yaml."))
        mc_ok = bool(media_contact_lines(data["media_contact"])) and bool(data["media_contact"].get("email") or data["media_contact"].get("phone"))
        checks.append(Check(rule="structure.media_contact", status="pass" if mc_ok else "fail", severity="critical" if not mc_ok else "low",
                            message="Media contact inserted." if mc_ok else "No media contact (press_boilerplate or brand_voice.media_contact)."))

        all_checks = [*state.get("checks", []), *[c.model_dump() for c in checks]]
        critical = [c for c in all_checks if c.get("status") == "fail" and c.get("severity") == "critical"]
        failed = [c for c in all_checks if c.get("status") == "fail"]
        out = PressReleaseOutput(headline=draft.headline, subheadline=draft.subheadline, dateline=data["dateline"], body_markdown=body,
                                 quotes=draft.quotes, boilerplate=bp_text, media_contact=data["media_contact"], word_count=wc,
                                 forward_looking_detected=bool(fls_terms), forward_looking_terms=fls_terms,
                                 release_label=data["release_label"], release_date=data["release_day"], embargo=inp.get("embargo"),
                                 boilerplate_version=str(rec.get("version", "")), validation_passed=not failed)
        full = f"{data['release_label']}\n\n# {draft.headline}\n\n" + (f"_{draft.subheadline}_\n\n" if draft.subheadline else "") + body
        artifacts = [ctx.artifacts.write_text("press_release.md", full, "text/markdown", "Press release"),
                     ctx.artifacts.write_json("press_release.json", out.model_dump(mode="json"), "Press release (structured)")]
        actions: list[PendingAction] = []
        warnings: list[str] = []
        if inp.get("distribute", True):
            if critical:
                warnings.append("Newswire distribution not proposed: fix the critical checks first ("
                                + "; ".join(c["rule"] for c in critical) + ").")
            else:
                regions = list(inp.get("regions") or rec.get("distribution", {}).get("default_regions", ["US"]))
                wire_body = (f"{data['release_label']}\n\n" + (f"_{draft.subheadline}_\n\n" if draft.subheadline else "") + body)
                note = f" ({len(failed)} validation issue(s) - review before approving)" if failed else ""
                actions.append(PendingAction(
                    id=f"newswire-{slugify(draft.headline, 24)}", kind="newswire.distribute", connector="newswire", method="distribute",
                    payload={"headline": draft.headline, "body": wire_body, "embargo": inp.get("embargo"), "regions": regions,
                             "metadata": {"subheadline": draft.subheadline, "dateline": data["dateline"], "release_date": data["release_day"],
                                          "word_count": wc, "forward_looking": bool(fls_terms), "boilerplate_version": str(rec.get("version", "")),
                                          "categories": rec.get("distribution", {}).get("categories", []),
                                          "audience": inp.get("target_audience"), "run_id": ctx.run_id}},
                    summary=f"Distribute '{draft.headline}' to the newswire ({', '.join(regions)})"
                            + (f", embargoed until {data['embargo_display']}" if data.get("embargo_display") else "") + note))
        summary = (f"Press release '{draft.headline[:70]}': {wc} words, {len(draft.quotes)} quote(s)"
                   + (", forward-looking disclaimer added" if fls_terms else "")
                   + ("; validator passed" if not failed else f"; {len(failed)} validation issue(s)"))
        return {"output": out.model_dump(mode="json"), "artifacts": artifacts, "actions": actions, "checks": checks,
                "warnings": warnings, "summary": summary}
