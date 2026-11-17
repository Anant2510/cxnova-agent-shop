"""Content Adaptation - one source asset, many channel-ready versions.

Source content comes through the open ingestion layer (URL, PDF, DOCX, Markdown, HTML, text) or
from upstream content agents. Per-channel rules live in a YAML channel registry
(config/skills/channel_registry.yaml) - limits, structure, hashtag / emoji / link policy, tone,
calendar slot - so a new channel is configuration. The model adapts the core message for every
requested channel in one structured call; a deterministic validator checks characters (incl. X URL
weighting and SMS segments), hashtags, emoji, links, required prefixes / opt-outs and figures not
found in the source. Failures get one model repair pass, then a deterministic fix. Exports:
per-channel files and a content-calendar CSV.
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel, Field

from ...core import AgentKind, AgentSpec, BaseAgent, Category, Check, RunContext, Tier, register
from ...core.textutil import normalize_quotes, normalize_ws, truncate

URL = re.compile(r"https?://[^\s)>\]]+")
HASHTAG = re.compile(r"(?<![\w#&])#([A-Za-z][A-Za-z0-9_]*)")
EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B05-\u2B55\u2300-\u23FF]")
EMOJI_JOINERS = re.compile("[\uFE0F\u200D\U0001F3FB-\U0001F3FF]")  # variation selector, ZWJ, skin tones
NUMBER = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?%?")
GSM_BASIC = set("@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà")
GSM_EXT = set("^{}\\[~]|€\f")
WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

# ----------------------------------------------------------------------------- contract


class AdaptationInput(BaseModel):
    source: str | None = Field(default=None, description="URL or file path of the source asset (PDF, DOCX, Markdown, HTML, text)")
    text: str | None = Field(default=None, description="Source content pasted as text", json_schema_extra={"ui": "textarea"})
    html: str | None = Field(default=None, description="Source content as HTML (when the console cannot fetch URLs)", json_schema_extra={"ui": "textarea"})
    channels: list[str] = Field(default_factory=lambda: ["linkedin_post", "x_post", "email_snippet"], min_length=1,
                                description="Channel keys from the channel registry, or ['all']")
    audience: str | None = Field(default=None, description="Who the adapted content is for")
    goal: str | None = Field(default=None, description="What the content should achieve, e.g. 'drive whitepaper downloads'")
    cta_url: str | None = Field(default=None, description="Link to promote (UTM-tagged per channel when UTM links are available upstream)")
    hashtags: list[str] = Field(default_factory=list, description="Preferred hashtags (used first when a channel needs more)")
    start_date: date | None = Field(default=None, description="First day of the content calendar (default: tomorrow)")
    campaign: str | None = Field(default=None, description="Campaign label for the calendar")


class ChannelText(BaseModel):
    channel: str = Field(description="Channel key exactly as given")
    text: str = Field(description="The adapted copy, without the hashtag block")
    hashtags: list[str] = Field(default_factory=list, max_length=10, description="Hashtags without the # sign")
    notes: str = ""


class AdaptationDraft(BaseModel):
    core_message: str = Field(description="The single message every version must preserve")
    key_points: list[str] = Field(default_factory=list, max_length=6)
    adaptations: list[ChannelText] = Field(min_length=1)


class RepairBatch(BaseModel):
    adaptations: list[ChannelText] = Field(min_length=1)


class Adaptation(BaseModel):
    channel: str
    label: str
    text: str
    hashtags: list[str] = Field(default_factory=list)
    char_count: int
    max_chars: int
    valid: bool
    issues: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list)
    emoji_count: int = 0
    sms_segments: int | None = None
    scheduled_for: str | None = None
    artifact: str = ""


class AdaptationOutput(BaseModel):
    source_title: str
    source: str
    core_message: str
    key_points: list[str] = Field(default_factory=list)
    adaptations: list[Adaptation]
    all_valid: bool
    registry_version: str
    calendar_artifact: str | None = None


# ----------------------------------------------------------------------------- deterministic core

def document_title(title: str | None, text: str) -> str:
    """A usable title: the parser's title unless it is a fragment (e.g. a wrapped disclaimer line in a
    PDF), else the first title-like line - 3-15 words, not a sentence, not a page marker or disclaimer."""
    t = normalize_ws(title or "")
    if len(t.split()) >= 3 and not t.lower().startswith(("illustrative", "[page")):
        return t
    for line in (text or "").splitlines():
        line = normalize_ws(line.lstrip("#").strip())
        if not line or line.lower().startswith(("illustrative", "[page")) or line[0].islower():
            continue
        if 3 <= len(line.split()) <= 15 and not line.endswith("."):
            return truncate(line, 120)
    return t or "Untitled"


def clean_hashtag(tag: str) -> str:
    t = re.sub(r"[^A-Za-z0-9_]", "", str(tag or "").lstrip("#"))
    return t if t and t[0].isalpha() else ""


def split_hashtags(text: str, tags: list[str]) -> tuple[str, list[str]]:
    """Move a trailing block of hashtags out of the text into the tag list (deduped, case-insensitive)."""
    lines = (text or "").rstrip().split("\n")
    trailing: list[str] = []
    while lines and lines[-1].strip() and all(w.startswith("#") for w in lines[-1].split()):
        trailing = HASHTAG.findall(lines.pop()) + trailing
    body = "\n".join(lines).rstrip()
    m = re.search(r"((?:\s+#[A-Za-z][A-Za-z0-9_]*)+)\s*$", body)
    if m:  # hashtags glued to the end of the last sentence
        trailing = HASHTAG.findall(m.group(1)) + trailing
        body = body[: m.start()].rstrip()
    out: list[str] = []
    seen: set[str] = set()
    for t in trailing + [clean_hashtag(x) for x in tags or []]:
        t = clean_hashtag(t)
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return body, out


def compose(text: str, hashtags: list[str], spec: dict[str, Any]) -> str:
    placement = (spec.get("hashtags") or {}).get("placement", "none")
    if hashtags and placement != "none":
        return f"{text.rstrip()}\n\n" + " ".join(f"#{h}" for h in hashtags)
    return text.rstrip()


def count_chars(text: str, spec: dict[str, Any]) -> int:
    if spec.get("url_chars"):
        text = URL.sub("x" * int(spec["url_chars"]), text)
    return len(text)


def count_emoji(text: str) -> int:
    return len(EMOJI.findall(EMOJI_JOINERS.sub("", text or "")))


def sms_segments(text: str) -> tuple[str, int, int]:
    """(encoding, units, segments) for an SMS: GSM-7 160/153, UCS-2 70/67."""
    if all(c in GSM_BASIC or c in GSM_EXT for c in text):
        units = sum(2 if c in GSM_EXT else 1 for c in text)
        return "GSM-7", units, 1 if units <= 160 else math.ceil(units / 153)
    units = len(text.encode("utf-16-le")) // 2
    return "UCS-2", units, 1 if units <= 70 else math.ceil(units / 67)


def numbers_in(text: str) -> set[str]:
    return {n.replace(",", "").rstrip("%").rstrip(".") for n in NUMBER.findall(URL.sub(" ", HASHTAG.sub(" ", text or "")))}


FIXABLE = {"max_chars", "hashtags_max", "hashtags_min", "emoji_max", "links_not_allowed", "links_max", "structure_prefix",
           "must_include", "sms_segments", "cta_link_missing"}


def validate_adaptation(final: str, hashtags: list[str], spec: dict[str, Any], *, banned: list[str] | None = None,
                        source_numbers: set[str] | None = None, cta_url: str | None = None) -> list[dict[str, Any]]:
    """Rule results for the composed text. Each issue: {rule, status, message}."""
    issues: list[dict[str, Any]] = []
    n = count_chars(final, spec)
    if n > int(spec["max_chars"]):
        issues.append({"rule": "max_chars", "status": "fail", "message": f"{n} chars (limit {spec['max_chars']})."})
    elif spec.get("recommended_chars") and n > int(spec["recommended_chars"]):
        issues.append({"rule": "recommended_chars", "status": "warn", "message": f"{n} chars; {spec['recommended_chars']} or fewer recommended."})
    if spec.get("visible_chars"):
        hook = re.split(r"\n|(?<=[.!?])\s", final.strip(), maxsplit=1)[0]
        if len(hook) > int(spec["visible_chars"]):
            issues.append({"rule": "hook_visible", "status": "warn",
                           "message": f"Opening line is {len(hook)} chars; only {spec['visible_chars']} show before truncation."})
    hp = spec.get("hashtags") or {}
    tags = {t.lower() for t in HASHTAG.findall(final)}
    if len(tags) > int(hp.get("max", 0)):
        issues.append({"rule": "hashtags_max", "status": "fail", "message": f"{len(tags)} hashtags (max {hp.get('max', 0)})."})
    elif len(tags) < int(hp.get("min", 0)):
        issues.append({"rule": "hashtags_min", "status": "warn", "message": f"{len(tags)} hashtags (min {hp.get('min', 0)})."})
    emoji = count_emoji(final)
    if emoji > int((spec.get("emoji") or {}).get("max", 0)):
        issues.append({"rule": "emoji_max", "status": "fail", "message": f"{emoji} emoji (max {(spec.get('emoji') or {}).get('max', 0)})."})
    lp = spec.get("links") or {}
    links = URL.findall(final)
    if links and not lp.get("allowed", True):
        issues.append({"rule": "links_not_allowed", "status": "fail", "message": "Links are not clickable / allowed on this channel."})
    elif len(links) > int(lp.get("max", 1)):
        issues.append({"rule": "links_max", "status": "fail", "message": f"{len(links)} links (max {lp.get('max', 1)})."})
    elif cta_url and not links and lp.get("allowed", True) and lp.get("include_cta"):
        issues.append({"rule": "cta_link_missing", "status": "warn", "message": "The CTA link is missing."})
    if spec.get("required_prefix") and not final.lstrip().startswith(spec["required_prefix"]):
        issues.append({"rule": "structure_prefix", "status": "fail", "message": f"Must start with '{spec['required_prefix'].strip()}'."})
    for phrase in spec.get("must_include") or []:
        if phrase.lower() not in final.lower():
            issues.append({"rule": "must_include", "status": "fail", "message": f"Missing required text '{phrase}'."})
    if spec.get("max_segments"):
        enc, units, segs = sms_segments(final)
        if segs > int(spec["max_segments"]):
            issues.append({"rule": "sms_segments", "status": "fail", "message": f"{segs} SMS segments ({enc}, {units} units; max {spec['max_segments']})."})
    hits = [p for p in banned or [] if p.lower() in final.lower()]
    if hits:
        issues.append({"rule": "brand_banned", "status": "fail", "message": f"Brand-banned phrases {hits}."})
    if source_numbers is not None:
        extra = sorted(numbers_in(final) - source_numbers)
        if extra:
            issues.append({"rule": "figures_not_in_source", "status": "warn", "message": f"Figures not found in the source: {extra[:5]}."})
    return issues


def trim_text(text: str, budget: int) -> str:
    """Trim to a character budget: whole sentences first, then words."""
    text = text.strip()
    if len(text) <= budget:
        return text
    parts = re.split(r"(?<=[.!?])(\s+)", text)
    sents, seps = parts[0::2], parts[1::2]
    while len(sents) > 1 and len("".join(s + (seps[i] if i < len(sents) - 1 else "") for i, s in enumerate(sents))) > budget:
        sents.pop()
    out = "".join(s + (seps[i] if i < len(sents) - 1 else "") for i, s in enumerate(sents)).strip()
    if len(out) > budget:
        out = truncate(out, max(1, budget), "...")
    return out


def fix_adaptation(text: str, hashtags: list[str], spec: dict[str, Any], *, cta_url: str | None, seed_hashtags: list[str],
                   title: str) -> tuple[str, list[str], list[str]]:
    """Deterministic fix to the channel rules. Returns (text, hashtags, notes)."""
    notes: list[str] = []
    lp, hp = spec.get("links") or {}, spec.get("hashtags") or {}
    emax = int((spec.get("emoji") or {}).get("max", 0))
    if spec.get("max_segments") or emax == 0:
        if count_emoji(text):
            notes.append("removed emoji")
        text = EMOJI_JOINERS.sub("", EMOJI.sub("", text))
    if spec.get("max_segments"):
        text = normalize_quotes(text)
    elif count_emoji(text) > emax:
        keep = emax

        def drop(m: re.Match) -> str:
            nonlocal keep
            keep -= 1
            return m.group(0) if keep >= 0 else ""

        text = EMOJI.sub(drop, text)
        notes.append(f"kept the first {emax} emoji")
    links = URL.findall(text)
    if links and not lp.get("allowed", True):
        text = URL.sub("", text).rstrip()
        if lp.get("replacement") and lp["replacement"].lower() not in text.lower():
            text = f"{text.rstrip()} {lp['replacement']}"
        notes.append("replaced links that are not clickable on this channel")
        links = []
    elif len(links) > int(lp.get("max", 1)):
        for extra in links[int(lp.get("max", 1)):]:
            text = text.replace(extra, "")
        links = links[: int(lp.get("max", 1))]
        notes.append("removed extra links")
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    link = links[0] if links else (cta_url if (cta_url and lp.get("allowed", True) and lp.get("include_cta") and int(lp.get("max", 1)) > 0) else None)
    if link and not links:
        notes.append("added the CTA link")
    opt_out = spec.get("opt_out_text") if any(p.lower() not in text.lower() for p in spec.get("must_include") or []) else None
    if opt_out:
        notes.append("added the opt-out")
    inline = [t for t in HASHTAG.findall(text)]
    room = max(0, int(hp.get("max", 0)) - len({t.lower() for t in inline}))
    tags = [] if hp.get("placement", "none") == "none" else [t for t in hashtags if t.lower() not in {i.lower() for i in inline}][:room]
    if hp.get("placement", "none") == "none" and inline and int(hp.get("max", 0)) == 0:
        text = HASHTAG.sub(lambda m: m.group(1), text)
        notes.append("removed inline hashtags")
    if len(tags) + len(inline) < int(hp.get("min", 0)) and hp.get("placement", "none") != "none":
        for t in [clean_hashtag(x) for x in seed_hashtags]:
            if t and t.lower() not in {x.lower() for x in tags + inline} and len(tags) + len(inline) < int(hp.get("min", 0)):
                tags.append(t)
        notes.append("topped up hashtags")
    if len(hashtags) > len(tags) and hp.get("placement", "none") != "none":
        notes.append(f"kept {len(tags)} of {len(hashtags)} hashtags")
    if spec.get("required_prefix") and not text.lstrip().startswith(spec["required_prefix"]):
        text = f"{spec['required_prefix']}{title}\n\n{text}"
        notes.append("added the required heading")
    core = text.replace(link, "").strip() if link else text
    tail = " ".join(x for x in (link, opt_out) if x)
    block = compose("", tags, spec)
    budget = int(spec["max_chars"]) - len(block) - (len(tail) + 1 if tail else 0)
    if spec.get("url_chars") and link:
        budget += len(link) - int(spec["url_chars"])
    if spec.get("max_segments"):
        budget = min(budget, 160 * int(spec["max_segments"]) - (len(tail) + 1 if tail else 0))
    if len(core) > budget:
        core = trim_text(core, budget)
        notes.append(f"trimmed to {spec['max_chars']} characters")
    text = f"{core} {tail}".strip() if tail else core
    if spec.get("max_segments"):  # GSM-7 extension characters count double - shrink until it fits
        while sms_segments(text)[2] > int(spec["max_segments"]) and budget > 20:
            budget -= max(1, sms_segments(text)[1] - 160 * int(spec["max_segments"]))
            core = trim_text(core, budget)
            text = f"{core} {tail}".strip() if tail else core
    return text, tags, notes


def channel_key(name: str, registry: dict[str, Any]) -> str | None:
    key = re.sub(r"[\s-]+", "_", (name or "").strip().lower())
    if key in registry:
        return key
    labels = {re.sub(r"[\s-]+", "_", str(v.get("label", "")).lower()): k for k, v in registry.items()}
    return labels.get(key)


def reconcile_channels(items: list[dict[str, Any]], wanted: list[str], registry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Model versions -> requested channels: exact key or label first, then unmatched items in order."""
    out: dict[str, dict[str, Any]] = {}
    spare = []
    for it in items:
        key = channel_key(it.get("channel", ""), registry)
        if key in wanted and key not in out:
            out[key] = {**it, "channel": key}
        else:
            spare.append(it)
    for ch in [c for c in wanted if c not in out]:
        if not spare:
            break
        out[ch] = {**spare.pop(0), "channel": ch}
    return out


def seed_hashtags(preferred: list[str], title: str) -> list[str]:
    """Preferred tags first, then CamelCase pairs of the title's content words (e.g. HomeRisk)."""
    from ...core.textutil import STOPWORDS

    ws = [w for w in re.findall(r"[A-Za-z]{3,}", title or "") if w.lower() not in STOPWORDS]
    pairs = [a.capitalize() + b.capitalize() for a, b in zip(ws, ws[1:])]
    singles = [w.capitalize() for w in ws if len(w) >= 5]
    return [t for t in dict.fromkeys(clean_hashtag(x) for x in [*preferred, *pairs, *singles]) if t]


def build_calendar(rows: list[dict[str, Any]], start: date, registry: dict[str, Any]) -> list[dict[str, Any]]:
    """One slot per adaptation on distinct days, each on its channel's preferred weekday when set."""
    cursor = start
    out = []
    for r in rows:
        slot = str((registry.get(r["channel"]) or {}).get("slot") or "")
        m = re.match(r"^\s*([A-Za-z]{3})[a-z]*\s+(\d{1,2}:\d{2})\s*$", slot)
        day, time = cursor, "09:00"
        if m:
            time = m.group(2)
            wd = WEEKDAYS.get(m.group(1).lower())
            if wd is not None:
                day = cursor + timedelta(days=(wd - cursor.weekday()) % 7)
        out.append({**r, "date": day.isoformat(), "time": time})
        cursor = day + timedelta(days=1)
    return out


def fallback_text(core_message: str, key_points: list[str], spec: dict[str, Any]) -> str:
    parts = [core_message] + key_points[:1]
    return trim_text(" ".join(p for p in parts if p), max(40, int(spec.get("recommended_chars") or spec["max_chars"]) - 40))


# ----------------------------------------------------------------------------- agent

@register
class ContentAdaptationAgent(BaseAgent):
    spec = AgentSpec(
        id="content_adaptation",
        name="Content Adaptation",
        category=Category.CONTENT,
        summary="Repurposes one source asset (URL, PDF, DOCX, Markdown, text or upstream content) into channel-ready versions governed by a YAML channel registry, validated and fixed deterministically, with a content-calendar export.",
        kind=AgentKind.LLM,
        tier=Tier.BALANCED,
        connectors=["ingestion"],
        skills=["channel_registry", "brand_voice"],
        keywords=["repurpose", "adapt", "adapt content", "social posts", "multi-channel", "content calendar", "linkedin post", "tweet",
                  "instagram caption", "sms copy", "social calendar"],
        produces=["adaptation-<channel>.txt", "content_calendar.csv", "adaptations.json"],
        feeds=["content_translation", "utm_generation", "qr_code_generation"],
    )
    Input = AdaptationInput
    Output = AdaptationOutput
    schemas = {"adapt": AdaptationDraft, "repair": RepairBatch}

    def selftest_input(self) -> dict[str, Any]:
        return {"source": "sample_data/docs/whitepaper.md",
                "channels": ["linkedin_post", "x_post", "instagram_caption", "email_snippet", "sms", "web_page_section"],
                "audience": "Homeowners in flood-exposed US regions", "goal": "Drive whitepaper downloads",
                "cta_url": "https://www.meridian-mutual.example/products/home-insurance.html", "hashtags": ["FloodReady", "HomeInsurance"],
                "start_date": "2026-10-20", "campaign": "Q4 flood readiness"}

    def stages(self):
        return [("ingest", self.ingest), ("adapt", self.adapt), ("validate_and_fix", self.validate_and_fix), ("export", self.export)]

    # -- ingest -----------------------------------------------------------------------------------
    def ingest(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp = state["inputs"]
        registry = ctx.skill("channel_registry")
        channels_cfg = registry.get("channels") or {}
        wanted = list(channels_cfg) if [c.lower() for c in inp["channels"]] in (["all"], ["*"]) else \
            list(dict.fromkeys(c.strip().lower() for c in inp["channels"] if c.strip()))
        unknown = [c for c in wanted if c not in channels_cfg]
        if unknown:
            raise ValueError(f"Unknown channel(s) {unknown}. Configured in channel_registry: {sorted(channels_cfg)}")
        warnings: list[str] = []
        reasons: list[str] = []
        doc = None
        origin = ""
        ing = ctx.connector("ingestion")
        try:
            if inp.get("text"):
                doc, origin = ing.load(text=inp["text"]), "text"
            elif inp.get("html"):
                doc, origin = ing.load(html=inp["html"]), "html"
            elif inp.get("source"):
                doc, origin = ing.load(inp["source"]), inp["source"]
        except Exception as exc:
            reasons.append(f"source could not be loaded ({type(exc).__name__}: {exc})")
            warnings.append(f"Could not load {inp.get('source')}: {exc}")
        if doc is None and not reasons:
            upstream = ctx.upstream_text(12000)
            if not upstream:
                raise ValueError("Provide source (URL or path), text or html - or run after a content agent in an orchestrated plan.")
            doc, origin = ing.load(text=upstream, filename="upstream"), "upstream"
        title = document_title(doc.title, doc.text) if doc else "Untitled"
        text = doc.text if doc else ""
        utm = ctx.upstream_output("utm_generation") or {}
        links = {l.get("channel"): l["url"] for l in utm.get("links", []) if l.get("url")}
        return {"data": {"wanted": wanted, "registry": channels_cfg, "version": str(registry.get("version", "")), "title": title,
                         "source_text": text, "origin": origin or "none", "utm_links": links},
                "warnings": warnings, "provenance": {"degraded_reasons": list((state.get("provenance") or {}).get("degraded_reasons", [])) + reasons}}

    # -- adapt (one structured call for every channel) --------------------------------------------
    def adapt(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        if not data["source_text"].strip():
            return {"data": {"core_message": "", "key_points": [], "drafts": {}}}
        rules = [_rule_line(ch, data["registry"][ch]) for ch in data["wanted"]]
        draft = ctx.generate("adapt", AdaptationDraft, source_title=data["title"], source_text=truncate(data["source_text"], 9000),
                             channels=data["wanted"], channel_rules=rules, audience=inp.get("audience"), goal=inp.get("goal"),
                             cta_url=inp.get("cta_url"), hashtags=[clean_hashtag(h) for h in inp.get("hashtags") or [] if clean_hashtag(h)])
        drafts = reconcile_channels([a.model_dump() for a in draft.adaptations], data["wanted"], data["registry"])
        return {"data": {"core_message": normalize_ws(draft.core_message), "key_points": [normalize_ws(k) for k in draft.key_points if k.strip()][:6],
                         "drafts": drafts}}

    # -- validate, one model repair pass, deterministic fix ----------------------------------------
    def validate_and_fix(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        if not data["source_text"].strip():
            return {"data": {"final": []}}
        banned = ctx.brand.get("banned_phrases", [])
        src_numbers = numbers_in(data["source_text"])
        seeds = seed_hashtags(list(inp.get("hashtags") or []), data["title"])
        state_by_ch: dict[str, dict[str, Any]] = {}
        for ch in data["wanted"]:
            spec = data["registry"][ch]
            d = data["drafts"].get(ch)
            notes = [d["notes"]] if d and d.get("notes") else []
            if not d:
                d = {"channel": ch, "text": fallback_text(data["core_message"], data["key_points"], spec), "hashtags": []}
                notes.append("model returned no version for this channel - built from the core message")
            text, tags = split_hashtags(d["text"], d.get("hashtags") or [])
            text = self._tag_link(text, ch, inp, data)
            cta = self._cta(ch, inp, data)
            issues = validate_adaptation(compose(text, tags, spec), tags, spec, banned=banned, source_numbers=src_numbers, cta_url=cta)
            state_by_ch[ch] = {"text": text, "tags": tags, "notes": notes, "issues": issues, "repaired": False, "cta": cta}
        failing = [ch for ch, s in state_by_ch.items() if any(i["status"] == "fail" for i in s["issues"])]
        if failing and not ctx.llm.offline:
            batch = ctx.generate("repair", RepairBatch, items=[{"channel": ch, "text": state_by_ch[ch]["text"], "hashtags": state_by_ch[ch]["tags"],
                                                              "issues": [i["message"] for i in state_by_ch[ch]["issues"] if i["status"] == "fail"],
                                                              "rules": _rule_line(ch, data["registry"][ch])} for ch in failing],
                                 core_message=data["core_message"])
            for a in batch.adaptations:
                key = channel_key(a.channel, data["registry"])
                if key in failing and not state_by_ch[key]["repaired"]:
                    spec = data["registry"][key]
                    text, tags = split_hashtags(a.text, a.hashtags)
                    text = self._tag_link(text, key, inp, data)
                    state_by_ch[key].update(text=text, tags=tags, repaired=True,
                                            issues=validate_adaptation(compose(text, tags, spec), tags, spec, banned=banned,
                                                                       source_numbers=src_numbers, cta_url=state_by_ch[key]["cta"]))
                    state_by_ch[key]["notes"].append("repaired by the model")
        final, checks = [], []
        for ch, s in state_by_ch.items():
            spec = data["registry"][ch]
            if any(i["rule"] in FIXABLE for i in s["issues"]):
                text, tags, fix_notes = fix_adaptation(s["text"], s["tags"], spec, cta_url=s["cta"], seed_hashtags=seeds, title=data["title"])
                s.update(text=text, tags=tags)
                s["notes"] += [f"deterministic fix: {n}" for n in fix_notes]
                s["issues"] = validate_adaptation(compose(text, tags, spec), tags, spec, banned=banned, source_numbers=src_numbers,
                                                  cta_url=s["cta"])
            composed = compose(s["text"], s["tags"], spec)
            for i in s["issues"]:
                checks.append(Check(rule=f"{ch}.{i['rule']}", status=i["status"], severity="high" if i["status"] == "fail" else "low",
                                    target=ch, message=i["message"], ref=f"channel_registry {data['version']}"))
            if not s["issues"]:
                checks.append(Check(rule=f"{ch}.rules", status="pass", severity="low", target=ch, message=f"{spec['label']}: all channel rules met."))
            seg = sms_segments(composed)[2] if spec.get("max_segments") else None
            final.append({"channel": ch, "label": spec.get("label", ch), "text": composed, "hashtags": s["tags"],
                          "char_count": count_chars(composed, spec), "max_chars": int(spec["max_chars"]),
                          "valid": not any(i["status"] == "fail" for i in s["issues"]), "issues": [i["message"] for i in s["issues"]],
                          "notes": s["notes"], "links": URL.findall(composed), "emoji_count": count_emoji(composed), "sms_segments": seg})
        return {"data": {"final": final}, "checks": checks}

    def _cta(self, channel: str, inp: dict[str, Any], data: dict[str, Any]) -> str | None:
        return data["utm_links"].get((data["registry"][channel] or {}).get("utm_channel")) or inp.get("cta_url")

    def _tag_link(self, text: str, channel: str, inp: dict[str, Any], data: dict[str, Any]) -> str:
        """Swap the plain CTA link for the channel's UTM-tagged link when one exists upstream."""
        tagged = data["utm_links"].get((data["registry"][channel] or {}).get("utm_channel"))
        base = (inp.get("cta_url") or "").split("?")[0]
        if tagged and base:
            text = re.sub(re.escape(base) + r"(\?[^\s)]*)?", tagged, text)
        return text

    # -- export -----------------------------------------------------------------------------------
    def export(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        final = data["final"]
        start = date.fromisoformat(str(inp["start_date"])) if inp.get("start_date") else datetime.now(timezone.utc).date() + timedelta(days=1)
        scheduled = build_calendar(final, start, data["registry"])
        artifacts = []
        adaptations = []
        for row in scheduled:
            ext = "md" if (data["registry"][row["channel"]].get("format") == "markdown") else "txt"
            ref = ctx.artifacts.write_text(f"adaptation-{row['channel']}.{ext}", row["text"] + "\n",
                                           "text/markdown" if ext == "md" else "text/plain", f"{row['label']} copy")
            artifacts.append(ref)
            adaptations.append(Adaptation(**{k: row[k] for k in ("channel", "label", "text", "hashtags", "char_count", "max_chars", "valid",
                                                                 "issues", "notes", "links", "emoji_count", "sms_segments")},
                                          scheduled_for=f"{row['date']} {row['time']}", artifact=ref.path))
        cal_path = None
        if scheduled:
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["date", "time", "channel", "label", "campaign", "text", "hashtags", "link", "char_count", "max_chars", "valid", "status"])
            for row in scheduled:
                w.writerow([row["date"], row["time"], row["channel"], row["label"], inp.get("campaign") or "", row["text"],
                            " ".join(f"#{h}" for h in row["hashtags"]), (row["links"] or [""])[0], row["char_count"], row["max_chars"],
                            row["valid"], "draft"])
            cal = ctx.artifacts.write_text("content_calendar.csv", buf.getvalue(), "text/csv", "Content calendar (one slot per channel)")
            artifacts.append(cal)
            cal_path = cal.path
        out = AdaptationOutput(source_title=data["title"], source=data["origin"], core_message=data.get("core_message", ""),
                               key_points=data.get("key_points", []), adaptations=adaptations,
                               all_valid=bool(adaptations) and all(a.valid for a in adaptations), registry_version=data["version"],
                               calendar_artifact=cal_path)
        dumped = out.model_dump(mode="json")
        artifacts.append(ctx.artifacts.write_text("adaptations.json", json.dumps(dumped, indent=2, ensure_ascii=False), "application/json",
                                                  "All adaptations"))
        if not adaptations:
            summary = f"No adaptations produced - the source could not be loaded ({data['origin']})."
        else:
            ok = sum(1 for a in adaptations if a.valid)
            summary = (f"Adapted '{data['title']}' into {len(adaptations)} channel version(s) ({', '.join(a.channel for a in adaptations)}); "
                       f"{ok}/{len(adaptations)} meet every channel rule; calendar from {start.isoformat()}")
        return {"output": dumped, "artifacts": artifacts, "summary": summary}


def _rule_line(channel: str, spec: dict[str, Any]) -> str:
    hp, lp, ep = spec.get("hashtags") or {}, spec.get("links") or {}, spec.get("emoji") or {}
    parts = [f"{channel} ({spec.get('label', channel)}) - max {spec['max_chars']} characters"]
    if spec.get("recommended_chars"):
        parts.append(f"aim for {spec['recommended_chars']} or fewer")
    if spec.get("visible_chars"):
        parts.append(f"hook within the first {spec['visible_chars']} characters")
    parts.append(f"hashtags {hp.get('min', 0)}-{hp.get('max', 0)}" if int(hp.get("max", 0)) else "no hashtags")
    parts.append(f"at most {ep.get('max', 0)} emoji" if int(ep.get("max", 0)) else "no emoji")
    if not lp.get("allowed", True):
        parts.append("no links" + (f" (say '{lp['replacement']}')" if lp.get("replacement") else ""))
    else:
        parts.append(f"at most {lp.get('max', 1)} link(s)")
    if spec.get("must_include"):
        parts.append("must include " + ", ".join(f"'{p}'" for p in spec["must_include"]))
    if spec.get("required_prefix"):
        parts.append(f"start with '{spec['required_prefix'].strip()}' heading")
    parts.append(f"structure - {spec.get('structure', '')}")
    parts.append(f"tone - {spec.get('tone', '')}")
    return "; ".join(p for p in parts if p)
