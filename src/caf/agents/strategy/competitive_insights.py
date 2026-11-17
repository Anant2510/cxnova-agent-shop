"""Competitive Insights - evidence-first competitor intelligence for any number of competitors.

Opal's version researches one competitor per run with a bundled date-aware tool. Here an
evidence pipeline you own runs per competitor IN PARALLEL (thread pool + shared rate limiter):
search.news + search.search + RSS/Atom feeds + press-page / sitemap crawling. Every item keeps
its id, url, publisher, published date, retrieved_at and a deterministic type
(news | press | analyst | community | campaign), and evidence is cached in the record store keyed
by competitor + date so repeat runs are cheap. Synthesis runs at the PRO tier from that evidence
only; every claim cites evidence ids and a deterministic validator drops claims whose citations do
not resolve. Headlines and high-impact events are selected by rules, not by the model.
"""

from __future__ import annotations

import hashlib
import html as _html
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Literal
from urllib.parse import urljoin, urlsplit, urlunsplit

from pydantic import BaseModel, Field

from ...core import AgentKind, AgentSpec, BaseAgent, Category, Check, RunContext, Tier, register
from ...core.handoff import Finding
from ...core.textutil import STOPWORDS, normalize_ws, slugify, truncate

EvidenceType = Literal["news", "press", "analyst", "community", "campaign"]
Origin = Literal["news", "search", "feed", "press_page", "sitemap"]
EVIDENCE_COLLECTION = "competitive_insights.evidence"
SYNTHESIS_COLLECTION = "competitive_insights.synthesis"
MAX_WORKERS = 3  # parallel competitors - doubles as a coarse rate limit

# ----------------------------------------------------------------------------- contract


class CompetitiveInput(BaseModel):
    competitors: list[str] = Field(min_length=1, max_length=12, description="Competitor names or website URLs - no one-competitor limit")
    focus: str | None = Field(default=None, description="Optional focus, e.g. 'home insurance pricing' or 'digital claims'")
    days: int = Field(default=30, ge=1, le=365, description="Report window in days (headlines, activity)")
    events_days: int = Field(default=60, ge=1, le=730, description="Look-back window in days for high-impact events")
    feeds: list[str] = Field(default_factory=list, description="RSS/Atom feed URLs scanned for competitor mentions")
    press_pages: list[str] = Field(default_factory=list, description="Press / newsroom page URLs to crawl")
    max_items: int = Field(default=25, ge=5, le=100, description="Maximum evidence items kept per competitor")
    refresh: bool = Field(default=False, description="Bypass the local evidence and synthesis cache")


class CitedClaim(BaseModel):
    text: str = Field(description="One factual sentence")
    evidence_ids: list[str] = Field(default_factory=list, description="Ids of the evidence items that support the claim")


class ActionItem(BaseModel):
    priority: Literal["P1", "P2", "P3"] = "P2"
    action: str
    rationale: str = ""
    owner_role: str = ""
    evidence_ids: list[str] = Field(default_factory=list)


class EventNote(BaseModel):
    evidence_id: str
    significance: str = Field(description="Why the event matters to us, one sentence")


class CompetitorSynthesis(BaseModel):
    executive_summary: list[CitedClaim] = Field(default_factory=list, max_length=5)
    marketing_activity: list[CitedClaim] = Field(default_factory=list, max_length=8)
    analyst_commentary: list[CitedClaim] = Field(default_factory=list, max_length=6)
    community_signals: list[CitedClaim] = Field(default_factory=list, max_length=6)
    next_best_actions: list[ActionItem] = Field(default_factory=list, max_length=9)
    event_notes: list[EventNote] = Field(default_factory=list, max_length=8)


class Evidence(BaseModel):
    id: str
    competitor: str
    type: EvidenceType
    title: str
    url: str
    publisher: str | None = None
    published: str | None = None
    retrieved_at: str
    snippet: str = ""
    origin: Origin
    provider: str = ""
    age_days: int | None = None
    in_window: bool = True
    signals: list[str] = Field(default_factory=list)
    impact_score: float = 0.0


class Claim(BaseModel):
    text: str
    evidence_ids: list[str] = Field(default_factory=list)
    basis: Literal["evidence", "evidence_digest", "model_knowledge"] = "evidence"
    is_estimate: bool = False
    flags: list[str] = Field(default_factory=list)


class Action(BaseModel):
    priority: Literal["P1", "P2", "P3"]
    action: str
    rationale: str = ""
    owner_role: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    evidence_backed: bool = True
    flags: list[str] = Field(default_factory=list)


class ActionGroups(BaseModel):
    P1: list[Action] = Field(default_factory=list)
    P2: list[Action] = Field(default_factory=list)
    P3: list[Action] = Field(default_factory=list)


class Headline(BaseModel):
    evidence_id: str
    title: str
    date: str | None = None
    publisher: str | None = None
    url: str
    type: EvidenceType


class HighImpactEvent(Headline):
    signals: list[str] = Field(default_factory=list)
    impact_score: float = 0.0
    significance: str = ""
    basis: Literal["model", "rules"] = "rules"


class CompetitorReport(BaseModel):
    competitor: str
    input: str
    domain: str | None = None
    is_estimate: bool = Field(description="True when no evidence was found and the report is model knowledge")
    executive_summary: str
    summary_claims: list[Claim] = Field(default_factory=list)
    recent_headlines: list[Headline] = Field(default_factory=list)
    marketing_activity: list[Claim] = Field(default_factory=list)
    analyst_commentary: list[Claim] = Field(default_factory=list)
    community_signals: list[Claim] = Field(default_factory=list)
    next_best_actions: ActionGroups = Field(default_factory=ActionGroups)
    high_impact_events: list[HighImpactEvent] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    evidence_counts: dict[str, int] = Field(default_factory=dict)
    validation: dict[str, int] = Field(default_factory=dict)
    cache: dict[str, bool] = Field(default_factory=dict)


class CompetitiveInsightsOutput(BaseModel):
    reference_date: str
    window_days: int
    events_days: int
    focus: str | None = None
    executive_summary: str
    competitors: list[CompetitorReport]
    evidence_count: int
    is_estimate: bool
    findings: list[Finding] = Field(default_factory=list)


# ----------------------------------------------------------------------------- deterministic rules

# Source patterns are matched against "<host labels> <publisher>" (regex, boundary-aware) - social
# platforms are matched on the exact host instead, so 'x.com' never matches 'fox.com'.
PRESS_WIRE_PATTERNS = (r"pr ?newswire", r"business ?wire", r"globe ?newswire", r"newswire", r"accesswire", r"einpresswire",
                       r"presswire", r"press release")
ANALYST_SOURCE_PATTERNS = (r"\bgartner\b", r"\bforrester\b", r"\bidc\b", r"j\.? ?d\.? power", r"\bjdpower\b", r"a\.? ?m\.? best",
                           r"\bambest\b", r"\bmoody", r"s&p\b", r"\bfitch\b", r"\bkbra\b", r"\bcelent\b", r"everest group",
                           r"datos insights", r"\baite\b", r"\bnovarica\b", r"\banalyst", r"(?<![a-z])research", r"\bratings\b")
ANALYST_ORGS = ("gartner", "forrester", "j.d. power", "jd power", "am best", "a.m. best", "moody's", "moodys", "s&p global",
                "fitch", "kbra", "celent", "everest group", "datos insights")
COMMUNITY_SOURCE_PATTERNS = (r"(?<![a-z])forum", r"(?<![a-z])communit", r"\breddit\b", r"\btrustpilot\b", r"\bquora\b",
                             r"\bglassdoor\b", r"consumer ?affairs", r"\bsitejabber\b", r"(?<![a-z0-9])r/[a-z0-9_]+", r"\bmastodon\b")
COMMUNITY_DOMAINS = ("x.com", "twitter.com", "reddit.com", "trustpilot.com", "facebook.com", "quora.com", "youtube.com",
                     "tiktok.com", "instagram.com", "threads.net", "glassdoor.com", "linkedin.com")
CAMPAIGN_TERMS = ("campaign", "campaigns", "advert", "adverts", "advertising", "ad campaign", "ad recall", "tv ad", "tv spot",
                  "commercial", "sponsor", "sponsors", "sponsorship", "ambassador", "promotion", "promo", "billboard", "influencer",
                  "influencers", "rebrand", "rebrands", "brand refresh", "tagline", "jingle", "hashtag", "giveaway")
COMMUNITY_TERMS = ("customers say", "customer reviews", "reviews", "reviewers", "complaint", "complaints", "thread", "users report",
                   "policyholders say", "petition", "viral", "backlash", "social media", "stars", "app store", "one-star")
ANALYST_TERMS = ("analyst", "analysts", "rating", "ratings", "downgrade", "downgrades", "downgraded", "upgrade",
                 "upgraded", "outlook", "report finds", "study finds", "survey", "ranking", "ranked", "benchmark",
                 "market share", "forecast")
PRESS_PATHS = ("/press", "/newsroom", "/news-release", "/media-centre", "/media-center", "/media/", "/news/", "/press-release")

# signal -> (weight, phrases); whole-word matching, inflections listed explicitly. Title matches
# count in full, snippet-only matches count half (see impact_score).
IMPACT_SIGNALS: dict[str, tuple[float, tuple[str, ...]]] = {
    "pricing": (3, ("price", "prices", "pricing", "priced", "premium", "premiums", "rate", "rates", "rate increase",
                    "rate increases", "rate rise", "rate cut", "repricing", "discount", "discounts", "cheaper", "price war",
                    "commission", "commissions", "deductible", "deductibles")),
    "launch": (3, ("launch", "launches", "launched", "launching", "relaunch", "relaunches", "relaunched", "unveil", "unveils",
                   "unveiled", "introduces", "introduced", "rolls out", "rolled out", "debut", "debuts", "debuted", "new product")),
    "legal_regulatory": (3, ("lawsuit", "lawsuits", "fine", "fines", "fined", "regulator", "regulators", "investigation",
                             "settlement", "product recall", "recalls", "breach", "data breach", "outage", "class action",
                             "penalty", "sanction", "market conduct")),
    "market_exit": (3, ("exit", "exits", "exiting", "withdraw", "withdraws", "withdrawal", "pulls out", "retreat", "retreats",
                        "non-renewal", "non-renewals", "stops writing")),
    "m_and_a": (3, ("acquires", "acquired", "to acquire", "acquisition of", "merger", "merges with", "takeover", "buyout",
                    "stake in")),
    "partnership": (2, ("partnership", "partners with", "partnered", "teams up", "tie-up", "alliance", "collaboration",
                        "joint venture")),
    "campaign": (2, ("campaign", "sponsors", "sponsorship", "ambassador", "advertising", "rebrand", "tv spot")),
    "leadership": (2, ("appoints", "appointed", "names", "named", "hires", "ceo", "chief executive", "chief digital officer",
                       "chief marketing officer", "steps down", "resigns")),
    "financial": (2, ("earnings", "results", "revenue", "profit", "funding", "ipo", "combined ratio", "downgrade",
                      "downgraded", "upgrade", "upgraded")),
    "expansion": (2, ("expands", "expanded", "expansion", "enters", "new market", "new markets", "launches in")),
    "recognition": (1, ("award", "awards", "wins", "tops", "ranked first", "named best")),
}
TYPE_WEIGHT = {"press": 1.0, "news": 1.0, "campaign": 1.0, "analyst": 0.9, "community": 0.6}
QUERY_PLAN = [  # (method, query template) - extra angles help real providers return diverse evidence types
    ("news", "{name}"),
    ("news", "{name} {focus}"),
    ("search", "{name}"),
    ("search", "{name} analyst rating outlook"),
    ("search", "{name} customer reviews complaints"),
    ("search", "{name} marketing campaign"),
]
_ID = re.compile(r"\[?\s*(C\d+-E\d+)\s*\]?", re.I)


def _has_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", text) for t in terms)


def _host(url: str) -> str:
    return urlsplit(url or "").netloc.lower().removeprefix("www.")


def resolve_competitor(raw: str) -> dict[str, Any]:
    """Name or URL -> {input, name, url, domain}. 'https://www.harborline.example' -> 'Harborline'."""
    s = normalize_ws(raw)
    if s.lower().startswith(("http://", "https://")) or re.fullmatch(r"[\w-]+(\.[\w-]+)+(/\S*)?", s):
        url = s if s.lower().startswith("http") else f"https://{s}"
        host = _host(url)
        label = host.split(".")[0]
        name = " ".join(w.capitalize() for w in re.split(r"[-_]+", label) if w) or host
        return {"input": raw, "name": name, "url": url, "domain": host}
    return {"input": raw, "name": s, "url": None, "domain": None}


def name_tokens(name: str) -> list[str]:
    generic = {"insurance", "assurance", "group", "inc", "ltd", "llc", "plc", "co", "company", "corp", "the", "mutual",
               "holdings", "bank", "financial"}
    toks = [t for t in re.findall(r"[a-z0-9]+", name.lower()) if len(t) >= 3]
    distinctive = [t for t in toks if t not in generic]
    return distinctive or toks


def mentions(text: str, name: str) -> bool:
    low = text.lower()
    return any(re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", low) for t in name_tokens(name))


def infer_domain(name: str, urls: list[str]) -> str | None:
    """Most frequent host whose labels contain the competitor's distinctive name token."""
    toks = name_tokens(name)
    counts: dict[str, int] = {}
    for u in urls:
        host = _host(u)
        labels = re.split(r"[.-]", host)
        if toks and toks[0] in labels:
            counts[host] = counts.get(host, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] if counts else None


def canonical_url(url: str) -> str:
    p = urlsplit((url or "").strip())
    return urlunsplit((p.scheme.lower() or "https", p.netloc.lower().removeprefix("www."), p.path.rstrip("/") or "/", "", ""))


def parse_date(value: Any) -> date | None:
    """ISO dates/datetimes and RFC 822 (RSS pubDate) -> date."""
    if not value:
        return None
    s = str(value).strip()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")[:25]).date()
    except ValueError:
        pass
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(s).date()
    except (TypeError, ValueError, IndexError):
        return None


def classify_evidence(item: dict[str, Any], competitor_domain: str | None = None) -> str:
    """Deterministic evidence type: press | analyst | community | campaign | news."""
    host = _host(item.get("url", ""))
    path = urlsplit(item.get("url", "")).path.lower()
    publisher = (item.get("publisher") or "").lower()
    text = f" {item.get('title', '')} {item.get('snippet', '')} ".lower()
    where = " ".join(re.split(r"[.-]", host)) + " " + publisher

    def src(patterns: tuple[str, ...]) -> bool:
        return any(re.search(p, where) for p in patterns)

    if item.get("origin") in ("press_page", "sitemap"):
        return "press"
    if src(PRESS_WIRE_PATTERNS):
        return "press"
    if competitor_domain and (host == competitor_domain or host.endswith("." + competitor_domain)):
        return "campaign" if _has_any(text, CAMPAIGN_TERMS) else "press"
    if any(host == d or host.endswith("." + d) for d in COMMUNITY_DOMAINS) or src(COMMUNITY_SOURCE_PATTERNS):
        return "community"
    if src(ANALYST_SOURCE_PATTERNS):
        return "analyst"
    if any(p in path for p in PRESS_PATHS) and "newsroom" in where:
        return "press"
    if _has_any(text, CAMPAIGN_TERMS):
        return "campaign"
    if _has_any(text, ANALYST_ORGS):
        return "analyst"
    if _has_any(text, COMMUNITY_TERMS):
        return "community"
    if _has_any(text, ANALYST_TERMS):
        return "analyst"
    return "news"


def impact_signals(item: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(signals in the title, signals only in the snippet)."""
    title = f" {item.get('title', '')} ".lower()
    snippet = f" {item.get('snippet', '')} ".lower()
    in_title = [n for n, (_w, terms) in IMPACT_SIGNALS.items() if _has_any(title, terms)]
    in_snippet = [n for n, (_w, terms) in IMPACT_SIGNALS.items() if n not in in_title and _has_any(snippet, terms)]
    return in_title, in_snippet


def impact_score(title_signals: list[str], snippet_signals: list[str], ev_type: str, age_days: int | None, days: int) -> float:
    """Signal weights (title full, snippet half) x evidence-type weight x recency (0.8 outside the report window)."""
    base = sum(IMPACT_SIGNALS[s][0] for s in title_signals if s in IMPACT_SIGNALS)
    base += 0.5 * sum(IMPACT_SIGNALS[s][0] for s in snippet_signals if s in IMPACT_SIGNALS)
    recency = 1.0 if age_days is None or age_days <= days else 0.8
    return round(base * TYPE_WEIGHT.get(ev_type, 0.8) * recency, 2)


def finalize_evidence(raw: list[dict[str, Any]], *, competitor: str, index: int, domain: str | None, ref: date,
                      days: int, events_days: int, max_items: int) -> list[dict[str, Any]]:
    """Dedupe (url + title), classify, date, window-filter, score and number evidence items.

    Items older than events_days are dropped; ids are C<index>-E<nn>, newest first.
    """
    seen_url: set[str] = set()
    seen_title: set[str] = set()
    items: list[dict[str, Any]] = []
    for it in raw:
        title = normalize_ws(_html.unescape(str(it.get("title") or "")))
        url = str(it.get("url") or "").strip()
        if not title or not url:
            continue
        cu, ct = canonical_url(url), re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
        if cu in seen_url or ct in seen_title:
            continue
        d = parse_date(it.get("published"))
        age = (ref - d).days if d else None
        if age is not None and (age > events_days or age < -1):
            continue
        seen_url.add(cu)
        seen_title.add(ct)
        rec = {"competitor": competitor, "title": title, "url": url, "publisher": (it.get("publisher") or it.get("source") or None),
               "published": d.isoformat() if d else None, "retrieved_at": it.get("retrieved_at") or datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "snippet": truncate(normalize_ws(_html.unescape(str(it.get("snippet") or ""))), 400), "origin": it.get("origin", "search"),
               "provider": it.get("provider") or "", "age_days": age, "in_window": age is None or age <= days}
        rec["type"] = classify_evidence(rec, domain)
        in_title, in_snippet = impact_signals(rec)
        rec["signals"] = in_title + in_snippet
        rec["lead_signal"] = max(in_title or in_snippet, key=lambda x: IMPACT_SIGNALS[x][0]) if rec["signals"] else None
        rec["impact_score"] = impact_score(in_title, in_snippet, rec["type"], age, days)
        items.append(rec)
    # keep the most useful items (in window, high impact, recent), then number them newest first
    items.sort(key=lambda r: (not r["in_window"], -r["impact_score"], r["age_days"] if r["age_days"] is not None else 10**6, r["title"]))
    items = items[:max_items]
    items.sort(key=lambda r: (r["age_days"] is None, r["age_days"] if r["age_days"] is not None else 0, r["title"]))
    for n, r in enumerate(items, 1):
        r["id"] = f"C{index}-E{n:02d}"
    return items


def normalize_ids(ids: list[str]) -> list[str]:
    out = []
    for i in ids or []:
        m = _ID.search(str(i))
        if m:
            out.append(m.group(1).upper())
    return list(dict.fromkeys(out))


def split_inline_ids(text: str) -> tuple[str, list[str]]:
    """Pull [C1-E03] markers out of claim text (they are re-rendered from evidence_ids)."""
    found = [m.upper() for m in re.findall(r"C\d+-E\d+", text, flags=re.I)]
    clean = normalize_ws(re.sub(r"\s*[\[(]\s*(?:C\d+-E\d+[,;\s]*)+[\])]", "", text, flags=re.I))
    return clean, found


def validate_claims(claims: list[dict[str, Any]], id_types: dict[str, str], *, estimate_mode: bool,
                    want_types: tuple[str, ...] = ()) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Keep claims whose cited evidence ids exist; strip unknown ids (flagged); drop uncited claims.

    In estimate mode (no evidence at all) claims are kept as clearly-flagged model knowledge.
    """
    kept: list[dict[str, Any]] = []
    stats = {"kept": 0, "dropped": 0, "flagged": 0}
    for c in claims:
        text, inline = split_inline_ids(str(c.get("text") or ""))
        if not text:
            stats["dropped"] += 1
            continue
        if estimate_mode:
            kept.append({"text": text, "evidence_ids": [], "basis": "model_knowledge", "is_estimate": True,
                         "flags": ["model knowledge - not backed by retrieved evidence"]})
            stats["kept"] += 1
            continue
        ids = normalize_ids(list(c.get("evidence_ids") or []) + inline)
        good = [i for i in ids if i in id_types]
        bad = [i for i in ids if i not in id_types]
        if not good:
            stats["dropped"] += 1
            continue
        flags = []
        if bad:
            flags.append("removed unknown evidence ids: " + ", ".join(bad))
        if want_types and not any(id_types[i] in want_types for i in good):
            flags.append(f"cites no {'/'.join(want_types)} evidence")
        if flags:
            stats["flagged"] += 1
        stats["kept"] += 1
        kept.append({"text": text, "evidence_ids": good, "basis": "evidence", "is_estimate": False, "flags": flags})
    return kept, stats


def validate_actions(actions: list[dict[str, Any]], id_types: dict[str, str], *, estimate_mode: bool,
                     per_priority: int = 3) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {"P1": [], "P2": [], "P3": []}
    for a in actions:
        text = normalize_ws(str(a.get("action") or ""))
        if not text:
            continue
        ids = normalize_ids(a.get("evidence_ids") or [])
        good = [i for i in ids if i in id_types]
        flags = []
        if ids and len(good) < len(ids):
            flags.append("removed unknown evidence ids: " + ", ".join(i for i in ids if i not in id_types))
        if not good:
            flags.append("model-knowledge estimate - no evidence retrieved" if estimate_mode else "not linked to retrieved evidence")
        prio = a.get("priority") if a.get("priority") in groups else "P2"
        if len(groups[prio]) < per_priority:
            groups[prio].append({"priority": prio, "action": text, "rationale": normalize_ws(str(a.get("rationale") or "")),
                                 "owner_role": normalize_ws(str(a.get("owner_role") or "")), "evidence_ids": good,
                                 "evidence_backed": bool(good), "flags": flags})
    return groups


_GENERIC = {"insurance", "insurer", "insurers", "home", "homes", "says", "new", "with", "from", "after", "over", "into", "their"}


def title_terms(title: str, competitor: str = "") -> set[str]:
    """Distinctive, lightly stemmed title words (no stopwords, generic insurance words or the competitor's name)."""
    skip = set(name_tokens(competitor)) | _GENERIC
    out = set()
    for w in re.findall(r"[a-z0-9.]+", title.lower()):
        w = w.strip(".")
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        if (len(w) >= 4 or any(ch.isdigit() for ch in w)) and w not in skip and w not in STOPWORDS:
            out.add(w)
    return out


def same_story(a: dict[str, Any], b: dict[str, Any], days: int = 10) -> bool:
    """Follow-up coverage of one event: same lead signal, a shared distinctive title word, close in time."""
    if not a.get("lead_signal") or a.get("lead_signal") != b.get("lead_signal"):
        return False
    if abs((a.get("age_days") or 0) - (b.get("age_days") or 0)) > days:
        return False
    return bool(title_terms(a["title"], a.get("competitor", "")) & title_terms(b["title"], b.get("competitor", "")))


def select_events(items: list[dict[str, Any]], limit: int = 6, min_score: float = 1.5,
                  types: tuple[str, ...] = ("news", "press", "campaign", "analyst")) -> list[dict[str, Any]]:
    """Dated, non-community items above the impact threshold, strongest (then newest) first.
    Follow-up coverage of the same story collapses into one event, represented by the original
    (earliest) report unless a later one scores higher."""
    dated = sorted([i for i in items if i.get("published") and i["type"] in types and i.get("impact_score", 0) >= min_score],
                   key=lambda i: (-i["impact_score"], i["age_days"] or 0, i["id"]))
    picked: list[dict[str, Any]] = []
    for i in dated:
        twin = next((n for n, p in enumerate(picked) if same_story(i, p)), None)
        if twin is None:
            if len(picked) < limit:
                picked.append(i)
        elif i["impact_score"] >= picked[twin]["impact_score"] and (i["age_days"] or 0) > (picked[twin]["age_days"] or 0):
            picked[twin] = i
    return sorted(picked, key=lambda i: (-i["impact_score"], i["age_days"] or 0, i["id"]))


def recent_headlines(items: list[dict[str, Any]], limit: int = 8) -> list[dict[str, Any]]:
    news = [i for i in items if i["in_window"] and i["type"] in ("news", "press", "campaign")]
    return sorted(news, key=lambda i: (i["published"] or "", i["id"]), reverse=True)[:limit]


def evidence_digest(items: list[dict[str, Any]], types: tuple[str, ...], limit: int = 4) -> list[dict[str, Any]]:
    """Deterministic fallback claims: one per evidence item of the wanted types (verbatim title)."""
    picked = [i for i in items if i["type"] in types and i["in_window"]][:limit]
    return [{"text": f"{i['title']} ({i.get('publisher') or _host(i['url'])}, {i.get('published') or 'undated'}).",
             "evidence_ids": [i["id"]], "basis": "evidence_digest", "is_estimate": False,
             "flags": ["evidence digest - no validated model claim for this section"]} for i in picked]


def summary_digest(name: str, items: list[dict[str, Any]], events: list[dict[str, Any]], days: int) -> list[dict[str, Any]]:
    counts = type_counts([i for i in items if i["in_window"]])
    total = sum(counts.values())
    if not total and not events:
        return []
    parts = ", ".join(f"{v} {k}" for k, v in counts.items() if v)
    out = []
    if total:
        newest = [i["id"] for i in items if i["in_window"]][:3]
        out.append({"text": f"{total} evidence items about {name} in the last {days} days ({parts}).", "evidence_ids": newest,
                    "basis": "evidence_digest", "is_estimate": False, "flags": ["evidence digest"]})
    for e in events[:2]:
        out.append({"text": f"High-impact signal ({', '.join(e['signals'])}): {e['title']}.", "evidence_ids": [e["id"]],
                    "basis": "evidence_digest", "is_estimate": False, "flags": ["evidence digest"]})
    return out


def type_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    counts = {t: 0 for t in ("news", "press", "analyst", "community", "campaign")}
    for i in items:
        counts[i["type"]] = counts.get(i["type"], 0) + 1
    return counts


def render_claims(claims: list[dict[str, Any]]) -> str:
    out = []
    for c in claims:
        refs = " ".join(f"[{i}]" for i in c.get("evidence_ids", []))
        out.append(f"{c['text']} {refs}".strip() + (" (estimate)" if c.get("is_estimate") else ""))
    return " ".join(out)


def press_items(markup: str, base_url: str) -> list[dict[str, Any]]:
    """Items from a newsroom page (article/li blocks with a link + optional <time>), or the page itself."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(markup or "", "lxml")
    out: list[dict[str, Any]] = []
    for el in soup.select("article, li, .press-release, .news-item"):
        a = el.find("a", href=True)
        if not a:
            continue
        head = el.find(["h1", "h2", "h3", "h4"]) or a
        title = normalize_ws(head.get_text(" "))
        if len(title) < 20:
            continue
        t = el.find("time")
        published = (t.get("datetime") or t.get_text(" ", strip=True)) if t else None
        if not published:
            m = re.search(r"\b(20\d\d-\d\d-\d\d)\b", el.get_text(" "))
            published = m.group(1) if m else None
        out.append({"title": title, "url": urljoin(base_url, a["href"]), "published": published,
                    "snippet": truncate(normalize_ws(el.get_text(" ")), 300)})
    if not out:
        title = soup.find("h1") or soup.title
        meta = soup.find("meta", attrs={"property": "article:published_time"}) or soup.find("time")
        published = (meta.get("content") or meta.get("datetime")) if meta else None
        desc = soup.find("meta", attrs={"name": "description"})
        if title:
            out.append({"title": normalize_ws(title.get_text(" ")), "url": base_url, "published": published,
                        "snippet": desc.get("content", "") if desc else ""})
    uniq: dict[str, dict[str, Any]] = {}
    for it in out:
        uniq.setdefault(canonical_url(it["url"]), it)
    return list(uniq.values())


def sitemap_press_urls(xml: str, ref: date, events_days: int, limit: int = 5) -> list[tuple[str, str | None]]:
    """(loc, lastmod) of press/news URLs in a sitemap modified within the window, newest first."""
    rows = []
    for block in re.findall(r"<url>(.*?)</url>", xml or "", flags=re.S | re.I):
        loc = re.search(r"<loc>\s*(.*?)\s*</loc>", block, flags=re.S | re.I)
        mod = re.search(r"<lastmod>\s*(.*?)\s*</lastmod>", block, flags=re.S | re.I)
        if not loc or not any(p in urlsplit(loc.group(1)).path.lower() for p in PRESS_PATHS):
            continue
        d = parse_date(mod.group(1)) if mod else None
        if d and (ref - d).days > events_days:
            continue
        rows.append((loc.group(1), d.isoformat() if d else None))
    return sorted(rows, key=lambda r: r[1] or "", reverse=True)[:limit]


def assign_press_pages(competitors: list[dict[str, Any]], pages: list[str]) -> tuple[dict[str, list[str]], list[str]]:
    """Press page -> competitor by domain / name in the URL; the rest are shared (mention-filtered)."""
    owned: dict[str, list[str]] = {c["name"]: [] for c in competitors}
    shared: list[str] = []
    for url in pages:
        host, low = _host(url), url.lower()
        match = [c["name"] for c in competitors
                 if (c.get("domain") and (host == c["domain"] or host.endswith("." + c["domain"])))
                 or any(t in low for t in name_tokens(c["name"]))]
        if len(match) == 1:
            owned[match[0]].append(url)
        else:
            shared.append(url)
    return owned, shared


class RateLimiter:
    """Minimum interval between calls, shared across worker threads (0 = unlimited)."""

    def __init__(self, min_interval_s: float):
        self.min_interval = max(0.0, float(min_interval_s))
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            delay = self._next - now
            self._next = max(now, self._next) + self.min_interval
        if delay > 0:
            time.sleep(delay)


def _fingerprint(*parts: Any) -> str:
    return hashlib.sha1(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:12]


# ----------------------------------------------------------------------------- report rendering

def report_markdown(out: dict[str, Any], brand_name: str) -> str:
    lines = [f"# Competitive insights - {', '.join(c['competitor'] for c in out['competitors'])}", "",
             f"Prepared for {brand_name} on {out['reference_date']} · window {out['window_days']} days · "
             f"high-impact events {out['events_days']} days" + (f" · focus: {out['focus']}" if out.get("focus") else ""), ""]
    for c in out["competitors"]:
        lines += [f"## {c['competitor']}", ""]
        if c["is_estimate"]:
            lines += ["> **Model-knowledge estimate.** No evidence was retrieved for this competitor, so nothing below is "
                      "backed by sources. Verify before use.", ""]
        lines += ["### Executive summary", c["executive_summary"] or "_No validated claims._", ""]
        lines += ["### Recent headlines"]
        lines += [f"- {h['date'] or 'undated'} - [{h['title']}]({h['url']}) - {h.get('publisher') or ''} [{h['evidence_id']}]"
                  for h in c["recent_headlines"]] or ["_None in the window._"]
        for key, title in (("marketing_activity", "Marketing activity"), ("analyst_commentary", "Analyst commentary"),
                           ("community_signals", "Community signals")):
            lines += ["", f"### {title}"]
            lines += [f"- {render_claims([cl])}" + (f" _({'; '.join(cl['flags'])})_" if cl.get("flags") else "") for cl in c[key]] \
                or ["_No evidence-backed items._"]
        lines += ["", "### Next best actions"]
        for prio, label in (("P1", "P1 - act now"), ("P2", "P2 - this month"), ("P3", "P3 - monitor")):
            acts = c["next_best_actions"].get(prio, [])
            if acts:
                lines.append(f"**{label}**")
                for a in acts:
                    refs = " ".join(f"[{i}]" for i in a["evidence_ids"])
                    owner = f" _(owner: {a['owner_role']})_" if a.get("owner_role") else ""
                    lines.append(f"- {a['action']}{owner} {refs}".rstrip() + ("" if a["evidence_backed"] else " _(not evidence-backed)_"))
        lines += ["", f"### High-impact events (last {out['events_days']} days)"]
        lines += [f"- **{e['date']}** [{e['title']}]({e['url']}) - {', '.join(e['signals'])} (impact {e['impact_score']}) - "
                  f"{e['significance']} [{e['evidence_id']}]" for e in c["high_impact_events"]] or ["_None detected._"]
        lines += ["", "### Evidence", "", "| Id | Type | Published | Publisher | Title |", "|---|---|---|---|---|"]
        lines += [f"| <a id=\"{e['id']}\"></a>{e['id']} | {e['type']} | {e['published'] or 'undated'} | {e.get('publisher') or _host(e['url'])} "
                  f"| [{e['title'].replace('|', '/')}]({e['url']}) |" for e in c["evidence"]] or ["| - | - | - | - | no evidence |"]
        v = c["validation"]
        lines += ["", f"_Validation: {v.get('kept', 0)} claims kept, {v.get('dropped', 0)} dropped for missing citations, "
                      f"{v.get('flagged', 0)} flagged._", ""]
    return "\n".join(lines) + "\n"


def report_html(markdown_text: str, title: str) -> str:
    from ...core.emailkit import md_to_html

    body = md_to_html(markdown_text)
    body = re.sub(r"\[(C\d+-E\d+)\]", r'<a class="cite" href="#\1">[\1]</a>', body)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_html.escape(title)}</title><style>
body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;max-width:980px;margin:32px auto;padding:0 20px;color:#1d2530;line-height:1.55}}
h1{{font-size:1.7rem;border-bottom:2px solid #0b5cad;padding-bottom:8px}} h2{{margin-top:2.2rem;color:#0b5cad}} h3{{margin-top:1.4rem}}
table{{border-collapse:collapse;width:100%;font-size:.88rem}} th,td{{border:1px solid #dfe3e8;padding:6px 8px;text-align:left;vertical-align:top}}
th{{background:#f3f5f8}} blockquote{{background:#fff6e5;border-left:4px solid #e0a100;margin:0;padding:8px 14px}}
a.cite{{font-size:.78rem;text-decoration:none;color:#5b6573}} em{{color:#5b6573}}
</style></head><body>{body}</body></html>"""


# ----------------------------------------------------------------------------- agent

@register
class CompetitiveInsightsAgent(BaseAgent):
    spec = AgentSpec(
        id="competitive_insights",
        name="Competitive Insights",
        category=Category.STRATEGY,
        summary="Evidence-backed competitive intelligence for one or many competitors in parallel - headlines, marketing activity, analyst and community signals, high-impact events and prioritised next-best-actions, every claim cited.",
        kind=AgentKind.HYBRID,
        tier=Tier.PRO,
        connectors=["search", "crawler", "store"],
        skills=["brand_voice"],
        keywords=["competitor", "competitive", "competition", "battlecard", "competitive intelligence", "market moves",
                  "rival", "competitor analysis", "positioning"],
        produces=["competitive_insights.md", "competitive_insights.html", "evidence.json"],
        feeds=["campaign_brief_generation", "industry_marketing", "blog_post_creation", "experiment_ideation"],
    )
    Input = CompetitiveInput
    Output = CompetitiveInsightsOutput
    schemas = {"synthesis": CompetitorSynthesis}

    def selftest_input(self) -> dict[str, Any]:
        return {"competitors": ["Harborline Insurance", "Crestview Assurance"], "focus": "home insurance", "days": 30, "events_days": 60}

    def stages(self):
        return [("plan", self.plan), ("gather", self.gather), ("synthesize", self.synthesize), ("validate", self.validate),
                ("report", self.report)]

    # -- stage 1 ---------------------------------------------------------------------------------
    def plan(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp = state["inputs"]
        checks: list[Check] = []
        comps: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in inp["competitors"]:
            c = resolve_competitor(raw)
            if c["name"] and c["name"].lower() not in seen:
                seen.add(c["name"].lower())
                comps.append(c)
        if len(comps) < len(inp["competitors"]):
            checks.append(Check(rule="input.duplicates", status="info", severity="low", message="Duplicate competitors were merged."))
        days, events_days = int(inp["days"]), int(inp["events_days"])
        if events_days < days:
            checks.append(Check(rule="input.windows", status="warn", severity="low",
                                message=f"events_days ({events_days}) was shorter than days ({days}); using {days}."))
            events_days = days
        search = ctx.connector("search")
        ref_raw = (getattr(search, "config", {}) or {}).get("reference_date")
        ref = parse_date(ref_raw) or datetime.now(timezone.utc).date()
        owned, shared = assign_press_pages(comps, inp.get("press_pages") or [])
        return {"data": {"competitors": comps, "days": days, "events_days": events_days, "ref": ref.isoformat(),
                         "press_owned": owned, "press_shared": shared}, "checks": checks}

    # -- stage 2: evidence pipeline, parallel per competitor -------------------------------------
    def gather(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        search, crawler, store = ctx.connector("search"), ctx.connector("crawler"), ctx.connector("store")
        ref = date.fromisoformat(data["ref"])
        days, events_days = data["days"], data["events_days"]
        warnings: list[str] = []
        reasons: list[str] = []
        limiter = RateLimiter(0.5 if getattr(search, "external", False) else 0.0)
        feed_items: list[dict[str, Any]] = []
        if inp.get("feeds"):
            try:
                feed_items = [{**r.to_dict(), "origin": "feed", "publisher": _host(r.url) or _host(r.source or "")}
                              for r in search.feeds(inp["feeds"], days=events_days)]
            except Exception as exc:
                warnings.append(f"Feeds could not be read: {type(exc).__name__}: {exc}")
        shared_pages: list[dict[str, Any]] = []
        for url in data["press_shared"]:
            shared_pages += self._press_page(url, crawler, limiter, warnings)
        if not search.available:
            reasons.append("no search provider configured (search 'none') - evidence limited to feeds and press pages")

        def work(args: tuple[int, dict[str, Any]]) -> dict[str, Any]:
            ci, comp = args
            key = ":".join([slugify(comp["name"], 40), data["ref"], str(days), str(events_days),
                            _fingerprint(search.provider, inp.get("focus"), sorted(inp.get("feeds") or []),
                                         sorted(data["press_owned"].get(comp["name"], []) + data["press_shared"]), inp["max_items"])])
            if not inp.get("refresh"):
                try:
                    cached = store.get(EVIDENCE_COLLECTION, key)
                except Exception:
                    cached = None
                if cached and cached.get("items"):
                    return {"comp": {**comp, "domain": cached.get("domain") or comp.get("domain")}, "items": cached["items"],
                            "cache_hit": True, "errors": [], "key": key}
            raw, errors = self._collect(comp, inp, search, crawler, limiter, ref, events_days,
                                        data["press_owned"].get(comp["name"], []))
            raw += [dict(i) for i in feed_items + shared_pages if mentions(f"{i.get('title', '')} {i.get('snippet', '')}", comp["name"])]
            domain = comp.get("domain") or infer_domain(comp["name"], [r.get("url", "") for r in raw])
            items = finalize_evidence(raw, competitor=comp["name"], index=ci, domain=domain, ref=ref, days=days,
                                      events_days=events_days, max_items=int(inp["max_items"]))
            if items:
                try:
                    store.put(EVIDENCE_COLLECTION, {"competitor": comp["name"], "domain": domain, "items": items,
                                                    "collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}, key=key)
                except Exception as exc:
                    errors.append(f"cache write failed: {type(exc).__name__}")
            return {"comp": {**comp, "domain": domain}, "items": items, "cache_hit": False, "errors": errors, "key": key}

        comps = data["competitors"]
        with ThreadPoolExecutor(max_workers=max(1, min(MAX_WORKERS, len(comps))), thread_name_prefix="ci-evidence") as pool:
            results = list(pool.map(work, list(enumerate(comps, 1))))
        checks: list[Check] = []
        if inp.get("feeds"):
            checks.append(Check(rule="evidence.feeds", status="info" if feed_items else "warn", severity="low",
                                message=f"{len(feed_items)} item(s) read from {len(inp['feeds'])} feed(s) within {events_days} days."))
        if inp.get("press_pages"):
            owned = sum(len(v) for v in data["press_owned"].values())
            checks.append(Check(rule="evidence.press_pages", status="info", severity="low",
                                message=f"{len(inp['press_pages'])} press page(s): {owned} matched to a competitor, "
                                        f"{len(data['press_shared'])} shared ({len(shared_pages)} item(s), filtered by mention)."))
        for res in results:
            name = res["comp"]["name"]
            warnings += [f"{name}: {e}" for e in res["errors"]]
            n = len(res["items"])
            checks.append(Check(rule="evidence.collected", status="pass" if n else "warn", severity="medium", target=name,
                                message=f"{name}: {n} evidence items" + (" (cache hit)" if res["cache_hit"] else "") +
                                        (f" - {type_counts(res['items'])}" if n else " - report will be a model-knowledge estimate"),
                                data={"cache_key": res["key"]}))
            if not n:
                reasons.append(f"no evidence found for {name} - report is a model-knowledge estimate")
        prev = list((state.get("provenance") or {}).get("degraded_reasons", []))
        return {"data": {"results": results}, "checks": checks, "warnings": warnings,
                "provenance": {"degraded_reasons": prev + reasons, "evidence_count": sum(len(r["items"]) for r in results),
                               "evidence_cache_hits": sum(1 for r in results if r["cache_hit"])}}

    def _collect(self, comp: dict[str, Any], inp: dict[str, Any], search: Any, crawler: Any, limiter: RateLimiter,
                 ref: date, events_days: int, press_urls: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
        raw: list[dict[str, Any]] = []
        errors: list[str] = []
        name, focus = comp["name"], (inp.get("focus") or "").strip()
        if search.available:
            for method, template in QUERY_PLAN:
                if "{focus}" in template and not focus:
                    continue
                query = template.format(name=name, focus=focus).strip()
                limiter.wait()
                try:
                    if method == "news":
                        results = search.news(query, num=inp["max_items"], days=events_days)
                    else:
                        results = search.search(query, num=inp["max_items"], recency_days=events_days)
                except Exception as exc:
                    errors.append(f"{method} '{query}' failed: {type(exc).__name__}")
                    continue
                raw += [{**r.to_dict(), "publisher": r.source, "origin": method} for r in results
                        if mentions(f"{r.title} {r.snippet} {r.url}", name)]
        for url in press_urls:
            raw += self._press_page(url, crawler, limiter, errors)
        if comp.get("url"):  # sitemap discovery of newsroom pages on the competitor's own site
            parts = urlsplit(comp["url"])
            sitemap = f"{parts.scheme}://{parts.netloc}/sitemap.xml"
            limiter.wait()
            try:
                xml = crawler.fetch_text(sitemap)
            except Exception:
                xml = None
            for loc, lastmod in sitemap_press_urls(xml or "", ref, events_days, limit=3):
                for it in self._press_page(loc, crawler, limiter, errors):
                    raw.append({**it, "published": it.get("published") or lastmod, "origin": "sitemap"})
        return raw, errors

    def _press_page(self, url: str, crawler: Any, limiter: RateLimiter, errors: list[str]) -> list[dict[str, Any]]:
        if not getattr(crawler, "is_fixture", lambda u: False)(url):
            limiter.wait()
        try:
            page = crawler.fetch(url)
        except Exception as exc:
            errors.append(f"press page {url} failed: {type(exc).__name__}")
            return []
        if not page.ok or not page.is_html:
            errors.append(f"press page {url} returned status {page.status}{' ' + page.error if page.error else ''}")
            return []
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return [{**it, "origin": "press_page", "publisher": _host(page.final_url), "retrieved_at": stamp, "provider": "crawler"}
                for it in press_items(page.html, page.final_url)]

    # -- stage 3: PRO-tier synthesis from evidence only (parallel, cached) ------------------------
    def synthesize(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        store = ctx.connector("store")
        model_id = ctx.llm.model_for(ctx.tier)

        def work(res: dict[str, Any]) -> dict[str, Any]:
            items = res["items"]
            events = select_events(items)
            variables = dict(competitor=res["comp"]["name"], focus=inp.get("focus"), days=data["days"], events_days=data["events_days"],
                             reference_date=data["ref"], estimate_mode=not items,
                             evidence=[{"id": i["id"], "type": i["type"], "published": i["published"], "publisher": i.get("publisher"),
                                        "title": i["title"], "snippet": truncate(i.get("snippet") or "", 280)} for i in items],
                             events=[{"id": e["id"], "title": e["title"], "signals": e["signals"]} for e in events])
            system, user = ctx.render("synthesis", **variables)
            key = _fingerprint(system, user, model_id, ctx.llm.provider)
            if not inp.get("refresh"):
                try:
                    hit = store.get(SYNTHESIS_COLLECTION, key)
                except Exception:
                    hit = None
                if hit and hit.get("synthesis"):
                    return {"synthesis": hit["synthesis"], "cache_hit": True, "events": events}
            syn = ctx.generate("synthesis", CompetitorSynthesis, **variables).model_dump()
            try:
                store.put(SYNTHESIS_COLLECTION, {"competitor": res["comp"]["name"], "model": model_id, "synthesis": syn}, key=key)
            except Exception:
                pass
            return {"synthesis": syn, "cache_hit": False, "events": events}

        results = data["results"]
        with ThreadPoolExecutor(max_workers=max(1, min(MAX_WORKERS, len(results))), thread_name_prefix="ci-synth") as pool:
            syntheses = list(pool.map(work, results))
        hits = sum(1 for s in syntheses if s["cache_hit"])
        provenance: dict[str, Any] = {"synthesis_cache_hits": hits}
        if hits and ctx.llm.offline:  # the cache key includes the provider, so these entries were written by the offline model
            prev = list((state.get("provenance") or {}).get("degraded_reasons", []))
            provenance["degraded_reasons"] = prev + ["offline model: cached synthesis is placeholder text"]
        return {"data": {"syntheses": syntheses}, "provenance": provenance}

    # -- stage 4: deterministic citation validation + digest fallbacks ---------------------------
    def validate(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        data = state["data"]
        checks: list[Check] = []
        reports = []
        for res, syn_res in zip(data["results"], data["syntheses"]):
            name, items, syn = res["comp"]["name"], res["items"], syn_res["synthesis"]
            estimate = not items
            id_types = {i["id"]: i["type"] for i in items}
            totals = {"kept": 0, "dropped": 0, "flagged": 0}
            sections: dict[str, list[dict[str, Any]]] = {}
            for key, want, digest_types in (("executive_summary", (), ()),
                                            ("marketing_activity", ("campaign", "press", "news"), ("campaign", "press")),
                                            ("analyst_commentary", ("analyst",), ("analyst",)),
                                            ("community_signals", ("community",), ("community",))):
                kept, stats = validate_claims(syn.get(key) or [], id_types, estimate_mode=estimate, want_types=want)
                for k in totals:
                    totals[k] += stats[k]
                if not kept and not estimate:
                    kept = summary_digest(name, items, select_events(items), data["days"]) if key == "executive_summary" \
                        else evidence_digest(items, digest_types)
                sections[key] = kept
            events = []
            notes = {normalize_ids([n.get("evidence_id", "")])[0]: n.get("significance", "")
                     for n in syn.get("event_notes") or [] if normalize_ids([n.get("evidence_id", "")])}
            for e in syn_res["events"]:
                note = normalize_ws(notes.get(e["id"], ""))
                events.append({"evidence_id": e["id"], "title": e["title"], "date": e["published"], "publisher": e.get("publisher"),
                               "url": e["url"], "type": e["type"], "signals": e["signals"], "impact_score": e["impact_score"],
                               "significance": note or f"Rule-detected {', '.join(e['signals'])} signal in a {e['type']} item.",
                               "basis": "model" if note else "rules"})
            actions = validate_actions(syn.get("next_best_actions") or [], id_types, estimate_mode=estimate)
            reports.append({
                "competitor": name, "input": res["comp"]["input"], "domain": res["comp"].get("domain"), "is_estimate": estimate,
                "executive_summary": render_claims(sections["executive_summary"]), "summary_claims": sections["executive_summary"],
                "recent_headlines": [{"evidence_id": h["id"], "title": h["title"], "date": h["published"], "publisher": h.get("publisher"),
                                      "url": h["url"], "type": h["type"]} for h in recent_headlines(items)],
                "marketing_activity": sections["marketing_activity"], "analyst_commentary": sections["analyst_commentary"],
                "community_signals": sections["community_signals"], "next_best_actions": actions, "high_impact_events": events,
                "evidence": items, "evidence_counts": type_counts(items), "validation": totals,
                "cache": {"evidence": res["cache_hit"], "synthesis": syn_res["cache_hit"]}})
            status = "pass" if not totals["dropped"] and not totals["flagged"] else "warn"
            checks.append(Check(rule="claims.citations", status="info" if estimate else status, severity="high", target=name,
                                message=(f"{name}: no evidence - claims are flagged model-knowledge estimates." if estimate else
                                         f"{name}: {totals['kept']} claims kept, {totals['dropped']} dropped (no valid evidence id), "
                                         f"{totals['flagged']} flagged."), data=totals))
        return {"data": {"reports": reports}, "checks": checks}

    # -- stage 5: markdown + HTML report ---------------------------------------------------------
    def report(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        reports = [CompetitorReport.model_validate(r) for r in data["reports"]]
        findings: list[Finding] = []
        for r in reports:
            top_action = (r.next_best_actions.P1 or r.next_best_actions.P2 or [None])[0]
            for e in r.high_impact_events[:3]:
                findings.append(Finding(title=f"{r.competitor}: {e.title}", detail=e.significance,
                                        severity="high" if e.impact_score >= 4 else "medium", category="competitive",
                                        evidence=f"{e.publisher or _host(e.url)}, {e.date} [{e.evidence_id}] {e.url}",
                                        recommendation=top_action.action if top_action else "", target=e.url,
                                        source_agent="competitive_insights"))
        combined = " ".join(f"{r.competitor}: {r.executive_summary}" for r in reports if r.executive_summary)
        out = CompetitiveInsightsOutput(reference_date=data["ref"], window_days=data["days"], events_days=data["events_days"],
                                        focus=inp.get("focus"), executive_summary=combined, competitors=reports,
                                        evidence_count=sum(len(r.evidence) for r in reports),
                                        is_estimate=any(r.is_estimate for r in reports), findings=findings)
        dumped = out.model_dump(mode="json")
        md = report_markdown(dumped, ctx.brand.get("name", "us"))
        title = "Competitive insights - " + ", ".join(r.competitor for r in reports)
        artifacts = [ctx.artifacts.write_text("competitive_insights.md", md, "text/markdown", "Competitive intelligence report"),
                     ctx.artifacts.write_text("competitive_insights.html", report_html(md, title), "text/html", "Competitive intelligence report (HTML)"),
                     ctx.artifacts.write_json("evidence.json", [e.model_dump(mode="json") for r in reports for e in r.evidence],
                                              "Evidence ledger (url, publisher, published, retrieved_at, type)")]
        parts = [f"{r.competitor} ({len(r.evidence)} items, {len(r.high_impact_events)} high-impact"
                 + (", estimate" if r.is_estimate else "") + ")" for r in reports]
        summary = f"Competitive insights for {len(reports)} competitor(s), {data['days']}-day window: " + "; ".join(parts)
        warnings = [f"{r.competitor}: no evidence retrieved - the report is a model-knowledge estimate." for r in reports if r.is_estimate]
        return {"output": dumped, "artifacts": artifacts, "summary": summary, "warnings": warnings}
