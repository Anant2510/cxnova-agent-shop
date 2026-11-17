"""GEO Recommendations - Generative Engine Optimization audit with open, auditable checks.

Every Opal tool has an open equivalent here:
  crawler access   robots.txt evaluated with urllib.robotparser for 13 AI crawlers + llms.txt + page directives
  performance      local measurements (fetch time as a TTFB proxy, HTML weight, render-blocking resources,
                   image dimensions) and, when a key is supplied, the PageSpeed Insights API (lab + field data)
  structured data  our own JSON-LD parser (no extruct) with required/recommended properties per type (YAML)
  E-E-A-T          a rules engine over bylines, bios, dates, citations, about/contact, Organization schema
  citation ready   question headings with concise answers, lists/tables, sourced statistics, FAQ schema, summary

Pillar and overall scores are computed in code (config/skills/geo_rules.yaml). The model only writes the
prioritised action plan from the failing checks. Optional citation testing (citation_test=True with a real
model) asks generated questions and records whether the page or brand is cited - clearly experimental.

Stages: fetch -> audit -> score -> citation_test -> plan -> report
"""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timezone
from typing import Any, Literal
from urllib.parse import urldefrag, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup, Tag
from pydantic import BaseModel, Field, model_validator

from ...core import AgentKind, AgentSpec, BaseAgent, Category, Check, RunContext, Tier, register
from ...core.handoff import Finding
from ...core.textutil import canonical, sentences

Status = Literal["pass", "warn", "fail", "na"]
Severity = Literal["low", "medium", "high", "critical"]
PILLARS = ("crawler_access", "performance", "structured_data", "eeat", "citation_readiness")
PSI_ENDPOINT = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"


def degraded_update(state: dict[str, Any], reasons: list[str], **extra: Any) -> dict[str, Any]:
    """Provenance update for one stage (the core's merge_provenance reducer accumulates degraded_reasons)."""
    out: dict[str, Any] = dict(extra)
    if reasons:
        out["degraded_reasons"] = list(dict.fromkeys(reasons))
    return out

# ============================================================================ parsing helpers (pure)


def parse_jsonld(raw: str) -> tuple[Any, str | None]:
    text = (raw or "").strip()
    text = re.sub(r"^\s*(<!--|<!\[CDATA\[)", "", text)
    text = re.sub(r"(-->|\]\]>)\s*$", "", text).strip()
    if not text:
        return None, "empty JSON-LD block"
    try:
        return json.loads(text), None
    except json.JSONDecodeError as exc:
        return None, f"{exc.msg} (line {exc.lineno}, column {exc.colno})"


def _types(node: dict[str, Any]) -> list[str]:
    t = node.get("@type")
    vals = t if isinstance(t, list) else [t] if t else []
    return [str(v).split("/")[-1].split(":")[-1] for v in vals]


def jsonld_entities(data: Any, source: str, path: str = "$", depth: int = 0) -> list[dict[str, Any]]:
    """Flatten every typed node (top level, @graph, nested values) into {type, types, props, source, path, top}."""
    out: list[dict[str, Any]] = []
    if depth > 8:
        return out
    if isinstance(data, list):
        for i, item in enumerate(data):
            out += jsonld_entities(item, source, f"{path}[{i}]", depth + 1)
        return out
    if not isinstance(data, dict):
        return out
    if isinstance(data.get("@graph"), list):
        out += jsonld_entities(data["@graph"], source, f"{path}.@graph", depth + 1)
    types = _types(data)
    if types:
        top = path == "$" or bool(re.fullmatch(r"\$(\.@graph)?(\[\d+\])?", path))
        out.append({"type": types[0], "types": types, "props": sorted(k for k in data if not k.startswith("@")), "node": data,
                    "source": source, "path": path, "top": top})
    for k, v in data.items():
        if k.startswith("@"):
            continue
        if isinstance(v, (dict, list)):
            out += jsonld_entities(v, source, f"{path}.{k}", depth + 1)
    return out


def _present(node: dict[str, Any], prop: str) -> bool:
    v = node.get(prop)
    if v is None:
        return False
    if isinstance(v, (str, list, dict)):
        return bool(v)
    return True


def validate_entity(entity: dict[str, Any], schema_types: dict[str, dict[str, list[str]]],
                    strict_nested: set[str] | frozenset[str] = frozenset()) -> dict[str, Any] | None:
    """Missing required/recommended properties. Nested entities (author, publisher, brand ...) only fail the
    required check for types listed in strict_nested (Question, Offer ...); otherwise gaps are 'recommended'."""
    spec = next((schema_types[t] for t in entity["types"] if t in schema_types), None)
    if spec is None:
        return None
    node = entity["node"]
    missing_req = [p for p in spec.get("required", []) if not _present(node, p)]
    missing_rec = [p for p in spec.get("recommended", []) if not _present(node, p)]
    if not entity.get("top") and not (set(entity["types"]) & set(strict_nested)):
        missing_rec, missing_req = missing_req + missing_rec, []
    return {"type": entity["type"], "source": entity["source"], "path": entity["path"],
            "missing_required": missing_req, "missing_recommended": missing_rec}


def evaluate_ai_crawlers(robots_text: str | None, url: str, bots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """allowed=True/False per bot from robots.txt; None when robots.txt is unknown (not fetched)."""
    if robots_text is None:
        return [{**b, "allowed": None, "rule": "robots.txt not available"} for b in bots]
    rp = RobotFileParser()
    rp.parse(robots_text.splitlines())
    out = []
    for b in bots:
        ok = rp.can_fetch(b["agent"], url)
        out.append({**b, "allowed": bool(ok), "rule": "allowed" if ok else "disallowed by robots.txt"})
    return out


def parse_llms_txt(text: str | None) -> dict[str, Any]:
    if text is None:
        return {"present": False, "valid": None, "sections": 0, "links": 0}
    lines = [l.rstrip() for l in text.splitlines() if l.strip()]
    h1 = bool(lines) and lines[0].startswith("# ")
    return {"present": True, "valid": h1, "sections": sum(1 for l in lines if l.startswith("## ")),
            "links": len(re.findall(r"\[[^\]]+\]\([^)]+\)", text))}


QUESTION_START = re.compile(r"^(what|how|why|when|where|which|who|whose|can|could|do|does|did|is|are|was|will|should|would|may|must|have|has)\b", re.I)
# Statistics: percentages, ratios, large counts and durations. Prices and plan limits are product facts, not statistics.
STAT_RX = re.compile(r"(\b\d+(?:\.\d+)?\s?%|\b\d+(?:\.\d+)?\s(?:percent|per cent)\b|(?<![$€£\d,.])\b\d{1,3}(?:,\d{3})+\b|"
                     r"(?<![$€£])\b\d+(?:\.\d+)?\s?(?:million|billion|thousand)\b|\b\d+(?:\.\d+)?\s(?:out of|in)\s\d+\b|"
                     r"\b\d+(?:\.\d+)?\s(?:days|hours|weeks|months|years)\b)", re.I)
FIRST_PARTY_RX = re.compile(r"\b(we|our|us)\b", re.I)
SOURCE_RX = re.compile(r"\b(according to|source[sd]?:|survey|study|studies|report(?:ed)? by|data from|research by|per the)\b", re.I)
PHONE_RX = re.compile(r"(\+?\d{1,2}[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}")
SPA_ROOTS = ("#root", "#app", "#__next", "#__nuxt", "[data-reactroot]", "[ng-app]", "[ng-version]")
SUMMARY_CLASS = re.compile(r"(summary|tldr|tl-dr|takeaway|key-points|at-a-glance|lead|intro|excerpt|standfirst)", re.I)
SUMMARY_HEADING = re.compile(r"^(summary|key takeaways|takeaways|tl;?dr|at a glance|in short|in brief|overview|key facts)\b", re.I)
BLOCKS = ["p", "li", "td", "figcaption", "dd", "blockquote"]


def is_question(text: str) -> bool:
    t = " ".join((text or "").split())
    return bool(t) and (t.endswith("?") or (bool(QUESTION_START.match(t)) and len(t.split()) <= 12))


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _abs(href: str, base: str) -> str:
    return urldefrag(urljoin(base, href.strip()))[0]


def _main_root(soup: BeautifulSoup) -> Tag:
    return soup.find("main") or soup.find("article") or soup.body or soup


def find_focus(soup: BeautifulSoup, focus: str | None) -> Tag | None:
    """Locate a page section by CSS selector, id, or heading text (case-insensitive substring)."""
    if not focus:
        return None
    f = focus.strip()
    try:
        if f[:1] in "#.[" or re.match(r"^[a-z]+[#.\[]", f):
            el = soup.select_one(f)
            if el is not None:
                return el
    except Exception:
        pass
    el = soup.find(id=f) or soup.find(id=f.lstrip("#"))
    if el is not None:
        return el
    want = canonical(f)
    for h in soup.find_all(re.compile(r"^h[1-6]$")):
        if want and want in canonical(h.get_text(" ")):
            sec = h.find_parent(["section", "article"])
            return sec or h.parent
    return None


def _leaf_blocks(root: Tag) -> list[Tag]:
    out = []
    for el in root.find_all(BLOCKS):
        if el.find(BLOCKS):  # skip containers of other blocks (li > p) to avoid double counting
            continue
        if el.find_parent(["nav", "footer", "header"]):
            continue
        out.append(el)
    return out


def question_sections(root: Tag) -> list[dict[str, Any]]:
    """Question headings (h2-h4 and FAQ <summary>) with the first answer block that follows."""
    out = []
    for h in root.find_all(["h2", "h3", "h4", "summary"]):
        q = " ".join(h.get_text(" ").split())
        if not is_question(q):
            continue
        answer = ""
        if h.name == "summary" and h.parent is not None and h.parent.name == "details":
            answer = " ".join(h.parent.get_text(" ").split())[len(q):].strip()
        else:
            for el in h.find_all_next(["h1", "h2", "h3", "h4", "h5", "h6"] + BLOCKS):
                if re.match(r"^h[1-6]$", el.name):
                    break
                txt = " ".join(el.get_text(" ").split())
                if txt:
                    answer = txt
                    break
        out.append({"question": q, "answer": answer, "answer_words": len(answer.split())})
    return out


def statistics(root: Tag, page_host: str, base: str, brand: str | None = None) -> list[dict[str, Any]]:
    """Sentences containing a statistic, and whether each is attributed (external link/cite/source phrase)
    or first-party (we/our/brand). Table cells are skipped: prices and limits are specs, not claims."""
    stats = []
    for el in _leaf_blocks(root):
        if el.name == "td":
            continue
        text = " ".join(el.get_text(" ").split())
        if not STAT_RX.search(text):
            continue
        linked = el.find("cite") is not None or any(_host(_abs(a["href"], base)) not in ("", page_host) for a in el.find_all("a", href=True))
        for sent in sentences(text) or [text]:
            found = STAT_RX.findall(sent)
            if not found:
                continue
            first_party = bool(FIRST_PARTY_RX.search(sent)) or (bool(brand) and canonical(brand) in canonical(sent))
            stats.append({"text": sent[:180], "values": [f if isinstance(f, str) else f[0] for f in found][:4],
                          "sourced": linked or bool(SOURCE_RX.search(sent)), "first_party": first_party})
    return stats


def _dates(soup: BeautifulSoup, entities: list[dict[str, Any]]) -> dict[str, list[str]]:
    published, modified = [], []
    for t in soup.find_all("time"):
        if t.get("datetime"):
            published.append(t["datetime"])
    for m in soup.find_all("meta", attrs={"property": re.compile(r"^article:(published|modified)_time$")}):
        (published if "published" in m["property"] else modified).append(m.get("content", ""))
    for e in entities:
        n = e["node"]
        if n.get("datePublished"):
            published.append(str(n["datePublished"]))
        if n.get("dateModified"):
            modified.append(str(n["dateModified"]))
    return {"published": [d for d in published if d], "modified": [d for d in modified if d]}


def _to_date(value: str) -> date | None:
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).date()
    except Exception:
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", value or "")
        return date(int(m[1]), int(m[2]), int(m[3])) if m else None


def extract_geo_facts(url: str, html: str, headers: dict[str, str] | None = None, *, focus: str | None = None,
                      brand: str | None = None) -> dict[str, Any]:
    """All page-level signals the GEO rules need (pure, no network)."""
    soup = BeautifulSoup(html or "", "lxml")
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    page_host = _host(url)
    head = soup.head or soup

    # structured data
    blocks, errors, entities = 0, [], []
    for i, s in enumerate(soup.find_all("script", attrs={"type": re.compile(r"^\s*application/ld\+json\s*$", re.I)}), 1):
        blocks += 1
        data, err = parse_jsonld(s.string or s.get_text())
        if err:
            errors.append(f"block {i}: {err}")
            continue
        entities += jsonld_entities(data, f"JSON-LD block {i}")
    types = [e["type"] for e in entities]
    microdata = len(soup.find_all(attrs={"itemscope": True}))
    og_type = (soup.find("meta", attrs={"property": "og:type"}) or {}).get("content", "") if soup.find("meta", attrs={"property": "og:type"}) else ""

    # directives
    robots_meta = " ".join(m.get("content", "") for m in soup.find_all("meta", attrs={"name": re.compile(r"^(robots|googlebot|bingbot)$", re.I)})).lower()
    directives = sorted({d.strip() for d in re.split(r"[,\s]+", f"{robots_meta} {headers.get('x-robots-tag', '').lower()}") if d.strip()})

    # content roots (computed on a copy without scripts)
    for t in soup(["script", "style", "noscript", "template"]):
        t.decompose()
    main = _main_root(soup)
    focus_el = find_focus(soup, focus)
    root = focus_el or main
    main_text = " ".join(main.get_text(" ").split())
    spa = any(soup.select_one(sel) is not None for sel in SPA_ROOTS)

    # E-E-A-T signals
    author_names = [m.get("content", "").strip() for m in head.find_all("meta", attrs={"name": re.compile(r"^author$", re.I)}) if m.get("content")]
    author_names += [" ".join(a.get_text(" ").split()) for a in soup.find_all("a", rel=lambda r: r and "author" in r)]
    for el in soup.find_all(class_=re.compile(r"(byline|author)(?!-bio)", re.I)):
        if "bio" not in " ".join(el.get("class", [])).lower():
            author_names.append(" ".join(el.get_text(" ").split())[:80])
    for e in entities:
        a = e["node"].get("author")
        for item in (a if isinstance(a, list) else [a]):
            if isinstance(item, dict) and item.get("name"):
                author_names.append(str(item["name"]))
            elif isinstance(item, str) and item.strip():
                author_names.append(item.strip())
    bio = bool(soup.find(class_=re.compile(r"(author-bio|about-author|author-box|\bbio\b)", re.I)))
    bio = bio or any(re.match(r"^about the author", h.get_text(" ").strip(), re.I) for h in soup.find_all(re.compile(r"^h[2-6]$")))
    bio = bio or any(e["type"] == "Person" and any(e["node"].get(k) for k in ("description", "jobTitle", "sameAs")) for e in entities)
    dates = _dates(soup, entities)
    ext_links = sorted({_abs(a["href"], url) for a in main.find_all("a", href=True)
                        if _abs(a["href"], url).startswith("http") and _host(_abs(a["href"], url)) not in ("", page_host)
                        and not a.find_parent(["nav", "footer", "header"])})
    all_links = [(a.get("href", ""), " ".join(a.get_text(" ").split()).lower()) for a in soup.find_all("a", href=True)]
    about = any(re.search(r"about", h, re.I) or t.startswith("about") for h, t in all_links)
    contact = any(re.search(r"contact", h, re.I) or t.startswith("contact") for h, t in all_links)
    contact_details = any(h.startswith(("tel:", "mailto:")) for h, _ in all_links) or bool(soup.find("address")) or bool(PHONE_RX.search(soup.get_text(" ")))
    org_types = {"Organization", "Corporation", "InsuranceAgency", "LocalBusiness", "FinancialService", "NGO", "EducationalOrganization"}
    org_schema = [e for e in entities if set(e["types"]) & org_types]
    reviews = any(e["type"] in ("AggregateRating", "Review") for e in entities) or bool(
        soup.find(class_=re.compile(r"(testimonial|review|quotes)", re.I))) or bool(main.select("figure blockquote"))

    # citation readiness
    q_sections = question_sections(root)
    lists = len([l for l in root.find_all(["ul", "ol"]) if not l.find_parent(["nav", "footer", "header"]) and len(l.find_all("li")) >= 2])
    tables = len(root.find_all("table"))
    stats = statistics(root, page_host, url, brand)
    details_q = [s for s in soup.find_all("summary") if is_question(s.get_text(" "))]
    faq_heading = any(re.search(r"(faq|frequently asked|questions)", h.get_text(" "), re.I) for h in soup.find_all(re.compile(r"^h[1-6]$")))
    h1 = soup.find("h1")
    lead_words = 0
    if h1 is not None:
        nxt = h1.find_next(["p", "ul", "ol"])
        lead_words = len(nxt.get_text(" ").split()) if nxt is not None else 0
    summary_el = bool(main.find(class_=SUMMARY_CLASS)) or any(SUMMARY_HEADING.match(h.get_text(" ").strip()) for h in main.find_all(re.compile(r"^h[2-4]$")))

    # performance (static)
    blocking = []
    for l in head.find_all("link", href=True):
        if "stylesheet" in [r.lower() for r in (l.get("rel") or [])] and (l.get("media") or "all").lower() not in ("print",) and not l.has_attr("disabled"):
            blocking.append(_abs(l["href"], url))
    raw_soup = BeautifulSoup(html or "", "lxml")
    raw_head = raw_soup.head or raw_soup
    for s in raw_head.find_all("script", src=True):
        if not (s.has_attr("async") or s.has_attr("defer") or (s.get("type") or "").lower() == "module"):
            blocking.append(_abs(s["src"], url))
    images = soup.find_all("img")
    no_dims = [i.get("src", "") for i in images if not (i.get("width") and i.get("height")) and "aspect-ratio" not in (i.get("style") or "")]
    scripts = raw_soup.find_all("script", src=True)
    third_party = sorted({_host(_abs(s["src"], url)) for s in scripts if _host(_abs(s["src"], url)) not in ("", page_host)})

    faq_entities = [e for e in entities if e["type"] == "Question"]
    visible = canonical(soup.get_text(" "))
    faq_not_visible = [str(e["node"].get("name", ""))[:80] for e in faq_entities if e["node"].get("name") and canonical(str(e["node"]["name"])) not in visible]

    return {
        "url": url, "https": url.lower().startswith("https://"), "og_type": og_type.lower(),
        "schema": {"blocks": blocks, "errors": errors, "types": sorted(set(types)), "entities": entities, "microdata_items": microdata},
        "directives": directives,
        "main_words": len(main_text.split()), "spa_root": spa,
        "has_article_tag": soup.find("article") is not None,
        "authors": [a for a in dict.fromkeys(author_names) if a],
        "bio": bio, "dates": dates, "external_sources": ext_links,
        "about_link": about, "contact_link": contact, "contact_details": contact_details,
        "org_schema_on_page": bool(org_schema), "reviews": reviews,
        "questions": q_sections, "lists": lists, "tables": tables, "stats": stats,
        "faq_like": len(details_q) >= 2 or (faq_heading and len(q_sections) >= 2) or len(q_sections) >= 3,
        "faq_not_visible": faq_not_visible,
        "h1_count": len(soup.find_all("h1")), "h2_count": len(main.find_all("h2")),
        "lead_words": lead_words, "summary_element": summary_el,
        "brand_in_main": bool(brand) and canonical(brand) in canonical(main_text),
        "render_blocking": blocking, "images": len(images), "images_without_dimensions": no_dims,
        "viewport": soup.find("meta", attrs={"name": re.compile(r"^viewport$", re.I)}) is not None,
        "third_party_scripts": third_party, "scripts": len(scripts),
        "stylesheets": len([l for l in head.find_all("link") if "stylesheet" in [r.lower() for r in (l.get("rel") or [])]]),
        "html_bytes": len((html or "").encode("utf-8")),
        "focus_found": None if not focus else focus_el is not None,
    }


def detect_page_type(facts: dict[str, Any], url_known: bool = True) -> str:
    types = set(facts["schema"]["types"])
    if types & {"Article", "BlogPosting", "NewsArticle", "TechArticle", "Report"} or facts.get("og_type") == "article":
        return "article"
    if types & {"Product", "Service", "FinancialProduct"} or facts.get("og_type") == "product":
        return "product"
    if url_known and urlsplit(facts["url"]).path in ("", "/"):
        return "home"
    if facts.get("has_article_tag") and facts.get("authors"):
        return "article"
    return "page"


def resolve_focus(focus: str | None, pillars: dict[str, dict[str, Any]]) -> str | None:
    if not focus:
        return None
    f = canonical(focus).replace("-", " ").replace("_", " ")
    for key, p in pillars.items():
        if f in (key.replace("_", " "), canonical(p["label"])) or f in canonical(p["label"]) or key.replace("_", " ") in f:
            return key
    aliases = {"schema": "structured_data", "markup": "structured_data", "robots": "crawler_access", "crawler": "crawler_access",
               "speed": "performance", "core web vitals": "performance", "cwv": "performance", "eeat": "eeat", "e e a t": "eeat",
               "trust": "eeat", "citation": "citation_readiness", "content": "citation_readiness"}
    return next((v for k, v in aliases.items() if k in f), None)


# ============================================================================ rules -> checks (pure)


def _check(rule: dict[str, Any], status: Status, message: str, evidence: str = "", suffix: str = "", severity: str | None = None) -> dict[str, Any]:
    return {"id": rule["id"] + (f":{suffix}" if suffix else ""), "rule": rule["id"], "pillar": rule["pillar"], "title": rule["title"],
            "action": rule.get("action", ""),
            "status": status, "severity": severity or rule.get("severity", "medium"), "weight": float(rule.get("weight", 1.0)),
            "message": message, "evidence": evidence, "recommendation": rule.get("recommendation", ""), "why": rule.get("why", "")}


def _band(value: float, good: float, poor: float) -> Status:
    return "pass" if value <= good else ("warn" if value <= poor else "fail")


def evaluate_geo(facts: dict[str, Any], env: dict[str, Any], pack: dict[str, Any], today: date) -> list[dict[str, Any]]:
    """Evaluate every rule in the pack. env carries site-level facts: robots, bots, llms, homepage_org, ttfb_ms, psi."""
    rules = {r["id"]: r for r in pack.get("rules", []) if r.get("enabled", True)}
    ptype = facts["page_type"]
    out: list[dict[str, Any]] = []

    def applies(rule: dict[str, Any]) -> bool:
        scope = rule.get("applies_to") or ["all"]
        return "all" in scope or ptype in scope

    def add(rid: str, status: Status, message: str, evidence: str = "", suffix: str = "", severity: str | None = None) -> None:
        rule = rules.get(rid)
        if rule is None:
            return
        if not applies(rule):
            status, message = "na", f"Not applicable to a {ptype} page."
        out.append(_check(rule, status, message, evidence, suffix, severity))

    # crawler access ----------------------------------------------------------------------------
    rb = env.get("robots")
    if rb is None:
        add("crawler.robots_txt", "na", "robots.txt not fetched (pasted HTML).")
    else:
        add("crawler.robots_txt", "pass" if rb.get("found") else "warn",
            f"robots.txt {'found' if rb.get('found') else 'returned ' + str(rb.get('status'))} at {rb.get('url')}")
    sev, st = pack.get("bot_severity", {}), pack.get("bot_status_when_blocked", {})
    for b in env.get("bots") or []:
        rule = rules.get("crawler.ai_bot")
        if rule is None:
            break
        if b["allowed"] is None:
            status, msg = "na", f"{b['agent']}: robots.txt not available"
        elif b["allowed"]:
            status, msg = "pass", f"{b['agent']} ({b['vendor']}, {b['purpose']}) may crawl this page"
        else:
            status = st.get(b["purpose"], "fail")
            msg = f"{b['agent']} ({b['vendor']}, {b['purpose']}) is blocked by robots.txt"
            if b["purpose"] == "training":
                msg += " - a content-licensing choice; it does not stop AI search citations"
        c = _check({**rule, "weight": b.get("weight", 1.0)}, status, msg, b.get("note", ""), suffix=b["agent"],
                   severity=sev.get(b["purpose"], rule.get("severity")) if status != "pass" else "low")
        out.append(c)
    llms = env.get("llms")
    if llms is None:
        add("crawler.llms_txt", "na", "llms.txt not checked (pasted HTML).")
    elif not llms["present"]:
        add("crawler.llms_txt", "warn", f"No llms.txt at {env.get('llms_url')}")
    else:
        add("crawler.llms_txt", "pass" if llms["valid"] else "warn",
            f"llms.txt found: {llms['sections']} section(s), {llms['links']} link(s)" + ("" if llms["valid"] else "; first line is not an H1 (# Name)"))
    bad = [d for d in facts["directives"] if d in ("noindex", "nosnippet", "none") or d.startswith("max-snippet:0")]
    soft = [d for d in facts["directives"] if d in ("noai", "noimageai")]
    add("crawler.ai_meta_directives", "fail" if bad else ("warn" if soft else "pass"),
        f"Directives blocking AI use: {', '.join(bad + soft)}" if bad or soft else "No blocking robots directives")
    min_words = int(rules.get("crawler.server_rendered", {}).get("params", {}).get("min_words", 150))
    if facts["main_words"] >= min_words:
        add("crawler.server_rendered", "pass", f"{facts['main_words']} words of main content in the server HTML")
    elif facts["spa_root"]:
        add("crawler.server_rendered", "fail", f"Only {facts['main_words']} words in the server HTML and a client-side app root was found")
    elif facts["scripts"] >= 3:
        add("crawler.server_rendered", "warn", f"Only {facts['main_words']} words in the server HTML with {facts['scripts']} scripts - content may be rendered client-side")
    else:
        add("crawler.server_rendered", "pass", f"No client-side app shell detected (thin page: {facts['main_words']} words - see content depth)")

    # performance ----------------------------------------------------------------------------------
    ttfb = env.get("ttfb_ms")
    p = rules.get("perf.ttfb", {}).get("params", {})
    if ttfb is None:
        add("perf.ttfb", "na", "Not measured (pasted HTML).")
    else:
        add("perf.ttfb", _band(ttfb, p.get("good_ms", 800), p.get("poor_ms", 1800)),
            f"{ttfb:.0f} ms fetch time from the audit host (TTFB proxy{', local fixture' if env.get('fixture') else ''})")
    p = rules.get("perf.html_weight", {}).get("params", {})
    kb = facts["html_bytes"] / 1024
    add("perf.html_weight", "pass" if kb <= p.get("warn_kb", 150) else ("warn" if kb <= p.get("fail_kb", 500) else "fail"), f"{kb:.0f} KB of HTML")
    p = rules.get("perf.render_blocking", {}).get("params", {})
    n = len(facts["render_blocking"])
    add("perf.render_blocking", "pass" if n < p.get("warn", 3) else ("warn" if n < p.get("fail", 6) else "fail"),
        f"{n} render-blocking resource(s) in <head>", ", ".join(u.rsplit("/", 1)[-1] for u in facts["render_blocking"][:6]))
    nd = facts["images_without_dimensions"]
    add("perf.image_dimensions", "pass" if not nd else ("warn" if len(nd) <= 2 else "fail"),
        f"{len(nd)} of {facts['images']} image(s) without width/height", ", ".join(s.rsplit("/", 1)[-1] for s in nd[:5]))
    add("perf.viewport", "pass" if facts["viewport"] else "fail", "Viewport meta tag " + ("present" if facts["viewport"] else "missing"))
    psi = env.get("psi")
    for rid, key, unit in (("perf.psi_lcp", "lcp_ms", "ms"), ("perf.psi_cls", "cls", ""), ("perf.psi_tbt", "tbt_ms", "ms")):
        rule = rules.get(rid)
        if rule is None:
            continue
        val = (psi or {}).get(key)
        if val is None:
            add(rid, "na", "PageSpeed Insights not run" + (f" ({env.get('psi_note')})" if env.get("psi_note") else ""))
            continue
        pp = rule.get("params", {})
        good = pp.get("good_ms", pp.get("good"))
        poor = pp.get("poor_ms", pp.get("poor"))
        add(rid, _band(float(val), float(good), float(poor)), f"{val:.0f} {unit}".strip() if unit else f"{val:.3f}",
            f"source: PageSpeed Insights {psi.get('strategy', '')} {'field' if psi.get('field') else 'lab'} data")

    # structured data ------------------------------------------------------------------------------
    sd = facts["schema"]
    add("schema.present", "pass" if sd["blocks"] and sd["entities"] else "fail",
        f"{sd['blocks']} JSON-LD block(s); types: {', '.join(sd['types']) or 'none'}" + (f"; {sd['microdata_items']} microdata item(s)" if sd["microdata_items"] else ""))
    if sd["blocks"]:
        add("schema.parse_errors", "fail" if sd["errors"] else "pass", "; ".join(sd["errors"]) or "All JSON-LD blocks parse")
    else:
        add("schema.parse_errors", "na", "No JSON-LD blocks")
    strict = set(pack.get("strict_nested_types", []))
    validated = [v for v in (validate_entity(e, pack.get("schema_types", {}), strict) for e in sd["entities"]) if v]
    req = [f"{v['type']} ({v['path']}): {', '.join(v['missing_required'])}" for v in validated if v["missing_required"]]
    rec = [f"{v['type']}: {', '.join(v['missing_recommended'])}" for v in validated if v["missing_recommended"]]
    if validated:
        add("schema.required_properties", "fail" if req else "pass", "; ".join(req[:6]) or f"{len(validated)} entities have all required properties")
        add("schema.recommended_properties", "warn" if rec else "pass", "; ".join(rec[:6]) or "Recommended properties present")
    else:
        add("schema.required_properties", "na", "No recognised schema.org entities")
        add("schema.recommended_properties", "na", "No recognised schema.org entities")
    types = set(sd["types"])
    expect = {"article": {"Article", "BlogPosting", "NewsArticle", "TechArticle", "Report"},
              "product": {"Product", "Service", "FinancialProduct"},
              "home": {"Organization", "WebSite", "InsuranceAgency", "LocalBusiness", "Corporation"}}.get(ptype)
    if expect is None:
        add("schema.main_entity_match", "na", "Generic page: no single expected main entity type")
    else:
        hit = sorted(types & expect)
        add("schema.main_entity_match", "pass" if hit else "fail",
            f"{ptype.capitalize()} page marked up as {', '.join(hit)}" if hit else f"{ptype.capitalize()} page without {' / '.join(sorted(expect))} markup")
    if any(e["type"] == "FAQPage" for e in sd["entities"]):
        add("schema.faq_visible", "fail" if facts["faq_not_visible"] else "pass",
            "Marked-up questions not visible: " + "; ".join(facts["faq_not_visible"][:4]) if facts["faq_not_visible"] else "Every FAQ question in the markup is visible on the page")
    else:
        add("schema.faq_visible", "na", "No FAQPage markup")

    # E-E-A-T --------------------------------------------------------------------------------------
    add("eeat.author_byline", "pass" if facts["authors"] else "fail",
        f"Author: {', '.join(facts['authors'][:3])}" if facts["authors"] else "No author byline or author markup")
    add("eeat.author_bio", "pass" if facts["bio"] else "warn", "Author bio/profile found" if facts["bio"] else "No author bio or profile")
    d = facts["dates"]
    add("eeat.dates", "pass" if d["published"] or d["modified"] else "fail",
        f"Published {', '.join(sorted(set(d['published']))[:2]) or '-'}; modified {', '.join(sorted(set(d['modified']))[:2]) or '-'}" if d["published"] or d["modified"] else "No machine-readable dates")
    all_dates = [x for x in (_to_date(v) for v in d["modified"] + d["published"]) if x]
    max_age = int(rules.get("eeat.freshness", {}).get("params", {}).get("max_age_days", 365))
    if all_dates:
        age = (today - max(all_dates)).days
        add("eeat.freshness", "pass" if age <= max_age else "warn", f"Last dated {max(all_dates).isoformat()} ({age} days ago)")
    else:
        add("eeat.freshness", "warn" if facts["page_type"] == "article" else "na", "No dates to judge freshness")
    min_src = int(rules.get("eeat.citations", {}).get("params", {}).get("min_sources", 1))
    hosts = sorted({_host(u) for u in facts["external_sources"]})
    add("eeat.citations", "pass" if len(hosts) >= min_src else "warn",
        f"{len(hosts)} external source domain(s): {', '.join(hosts[:4])}" if hosts else "No external sources linked from the main content")
    add("eeat.about_contact", "pass" if facts["about_link"] and facts["contact_link"] else ("warn" if facts["about_link"] or facts["contact_link"] else "fail"),
        f"About link: {'yes' if facts['about_link'] else 'no'}; contact link: {'yes' if facts['contact_link'] else 'no'}")
    if facts["org_schema_on_page"]:
        add("eeat.organization_schema", "pass", "Organization markup on this page")
    elif env.get("homepage_org") is True:
        add("eeat.organization_schema", "pass", "Organization markup found on the homepage")
    elif env.get("homepage_org") is False:
        add("eeat.organization_schema", "fail", "No Organization markup on this page or the homepage")
    else:
        add("eeat.organization_schema", "warn", "No Organization markup on this page (homepage not checked)")
    add("eeat.contact_details", "pass" if facts["contact_details"] else "warn", "Phone, email or address " + ("visible" if facts["contact_details"] else "not found"))
    add("eeat.https", "pass" if facts["https"] else "fail", "HTTPS" if facts["https"] else "Page served over plain HTTP")
    add("eeat.reviews", "pass" if facts["reviews"] else "warn", "Reviews/testimonials or rating markup found" if facts["reviews"] else "No reviews or testimonials")

    # citation readiness ---------------------------------------------------------------------------
    qs = facts["questions"]
    p = rules.get("citation.question_headings", {}).get("params", {})
    add("citation.question_headings", "pass" if len(qs) >= p.get("min_questions", 2) else ("warn" if qs else "fail"),
        f"{len(qs)} question heading(s)", "; ".join(q["question"] for q in qs[:4]))
    p = rules.get("citation.concise_answers", {}).get("params", {})
    if qs:
        good = [q for q in qs if p.get("min_words", 8) <= q["answer_words"] <= p.get("max_words", 90)]
        share = len(good) / len(qs)
        add("citation.concise_answers", "pass" if share >= p.get("min_share", 0.6) else ("warn" if share >= 0.3 else "fail"),
            f"{len(good)} of {len(qs)} questions answered in {p.get('min_words', 8)}-{p.get('max_words', 90)} words",
            "; ".join(f"{q['question'][:50]} ({q['answer_words']} words)" for q in qs if q not in good)[:300])
    else:
        add("citation.concise_answers", "na", "No question headings to evaluate")
    add("citation.lists_tables", "pass" if facts["lists"] or facts["tables"] else "warn", f"{facts['lists']} list(s), {facts['tables']} table(s) in the content")
    st = facts["stats"]
    p = rules.get("citation.statistics_sourced", {}).get("params", {})
    if st:
        sourced = sum(1 for s in st if s["sourced"])
        first = sum(1 for s in st if s["first_party"] and not s["sourced"])
        share = (sourced + first) / len(st)
        add("citation.statistics_sourced", "pass" if share >= p.get("min_sourced_share", 0.5) else "warn",
            f"{sourced} of {len(st)} statistic(s) cite a source, {first} are attributed first-party data",
            "Unattributed: " + "; ".join(s["text"][:90] for s in st if not (s["sourced"] or s["first_party"]))[:400])
    else:
        add("citation.statistics_sourced", "warn", "No specific statistics found - concrete numbers make answers quotable")
    has_faq_schema = any(e["type"] == "FAQPage" for e in sd["entities"])
    if facts["faq_like"]:
        add("citation.faq_schema", "pass" if has_faq_schema else "warn",
            "FAQ content marked up as FAQPage" if has_faq_schema else "Q&A content without FAQPage markup")
    else:
        add("citation.faq_schema", "pass" if has_faq_schema else "na", "FAQPage markup present" if has_faq_schema else "No FAQ-style content")
    p = rules.get("citation.summary", {}).get("params", {})
    lead_ok = 15 <= facts["lead_words"] <= p.get("max_lead_words", 70)
    add("citation.summary", "pass" if facts["summary_element"] or lead_ok else "warn",
        "Summary/key-takeaways element found" if facts["summary_element"] else (f"Lead paragraph of {facts['lead_words']} words after the H1" if lead_ok else "No summary or concise lead after the H1"))
    p = rules.get("citation.heading_structure", {}).get("params", {})
    ok = facts["h1_count"] == 1 and facts["h2_count"] >= p.get("min_h2", 3)
    add("citation.heading_structure", "pass" if ok else "warn", f"{facts['h1_count']} H1, {facts['h2_count']} H2 section(s)")
    p = rules.get("citation.content_depth", {}).get("params", {})
    add("citation.content_depth", "pass" if facts["main_words"] >= p.get("min_words", 300) else "warn", f"{facts['main_words']} words of main content")
    if env.get("brand"):
        add("citation.entity_named", "pass" if facts["brand_in_main"] else "warn", f"'{env['brand']}' " + ("named" if facts["brand_in_main"] else "not named") + " in the main content")
    else:
        add("citation.entity_named", "na", "No brand configured")
    return out


def score_pillars(checks: list[dict[str, Any]], pillars: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    credit = {"pass": 1.0, "warn": 0.5, "fail": 0.0}
    out = []
    for key, p in pillars.items():
        mine = [c for c in checks if c["pillar"] == key and c["status"] != "na"]
        total = sum(c["weight"] for c in mine)
        score = round(100 * sum(c["weight"] * credit[c["status"]] for c in mine) / total, 1) if total else None
        out.append({"key": key, "label": p.get("label", key), "weight": float(p.get("weight", 0.2)), "score": score, "assessed": score is not None,
                    "passed": sum(1 for c in mine if c["status"] == "pass"), "warned": sum(1 for c in mine if c["status"] == "warn"),
                    "failed": sum(1 for c in mine if c["status"] == "fail"),
                    "note": "" if score is not None else "not assessed (no applicable checks)"})
    return out


def overall_score(pillars: list[dict[str, Any]]) -> float | None:
    assessed = [p for p in pillars if p["assessed"]]
    w = sum(p["weight"] for p in assessed)
    return round(sum(p["score"] * p["weight"] for p in assessed) / w, 1) if w else None


def grade_for(score: float | None, grades: list[dict[str, Any]]) -> str:
    if score is None:
        return "n/a"
    for g in sorted(grades, key=lambda g: -g["min"]):
        if score >= g["min"]:
            return g["grade"]
    return "F"


def check_priority(check: dict[str, Any], pack: dict[str, Any]) -> tuple[str, float]:
    sw = pack.get("severity_weights", {"critical": 10, "high": 6, "medium": 3, "low": 1})
    pw = float(pack.get("pillars", {}).get(check["pillar"], {}).get("weight", 0.2))
    mult = 1.0 if check["status"] == "fail" else 0.5
    score = round(sw.get(check["severity"], 3) * pw * 10 * mult, 2)
    th = pack.get("priority_thresholds", {"P1": 9, "P2": 4})
    return ("P1" if score >= th.get("P1", 9) else "P2" if score >= th.get("P2", 4) else "P3"), score


def deterministic_plan(checks: list[dict[str, Any]], pack: dict[str, Any], focus_pillar: str | None = None) -> list[dict[str, Any]]:
    """One action per failing rule (bot checks merged), ordered by computed priority."""
    actions: dict[str, dict[str, Any]] = {}
    for c in checks:
        if c["status"] not in ("fail", "warn"):
            continue
        pr, score = check_priority(c, pack)
        if focus_pillar and c["pillar"] == focus_pillar:
            score += 1.0
        a = actions.get(c["rule"])
        if a is None:
            actions[c["rule"]] = {"title": c.get("action") or f"Improve: {c['title']}", "pillar": c["pillar"], "priority": pr, "score": score,
                                  "effort": "low" if c["severity"] in ("low", "medium") else "medium",
                                  "impact": {"critical": "high", "high": "high", "medium": "medium", "low": "low"}[c["severity"]],
                                  "rationale": c["why"], "steps": [c["recommendation"]] if c["recommendation"] else [],
                                  "check_ids": [c["id"]], "source": "rule_pack"}
        else:
            a["check_ids"].append(c["id"])
            if score > a["score"]:
                a["score"], a["priority"] = score, pr
    bots = actions.get("crawler.ai_bot")
    if bots:
        agents = [i.split(":", 1)[1] for i in bots["check_ids"] if ":" in i]
        bots["title"] = f"Review robots.txt rules for {', '.join(agents)}"
    return sorted(actions.values(), key=lambda a: (a["priority"], -a["score"]))


PRIORITY_RANK = {"P1": 0, "P2": 1, "P3": 2}


def reconcile_plan(model_actions: list[dict[str, Any]], checks: list[dict[str, Any]], pack: dict[str, Any],
                   focus_pillar: str | None = None) -> list[dict[str, Any]]:
    """Keep model actions grounded in failing checks; never under-prioritise; cover every failing rule."""
    failing = {c["id"]: c for c in checks if c["status"] in ("fail", "warn")}
    by_rule = {c["rule"]: c for c in failing.values()}
    plan, covered = [], set()
    for a in model_actions:
        ids = [i for i in dict.fromkeys(a.get("check_ids") or []) if i in failing or i in by_rule]
        ids = [i if i in failing else by_rule[i]["id"] for i in ids]
        if not ids:
            continue
        floor = min((check_priority(failing[i], pack) for i in ids), key=lambda t: PRIORITY_RANK[t[0]])
        prio = a.get("priority") if a.get("priority") in PRIORITY_RANK else floor[0]
        if PRIORITY_RANK[floor[0]] < PRIORITY_RANK[prio]:
            prio = floor[0]
        pillar = a.get("pillar") if a.get("pillar") in PILLARS else failing[ids[0]]["pillar"]
        plan.append({**a, "pillar": pillar, "priority": prio, "check_ids": ids, "score": floor[1], "source": "model"})
        covered |= {failing[i]["rule"] for i in ids}
    for d in deterministic_plan(checks, pack, focus_pillar):
        rules = {failing[i]["rule"] for i in d["check_ids"] if i in failing}
        if rules - covered and any(failing[i]["severity"] in ("medium", "high", "critical") or failing[i]["status"] == "fail" for i in d["check_ids"]):
            plan.append(d)
            covered |= rules
    return sorted(plan, key=lambda a: (PRIORITY_RANK.get(a["priority"], 2), -a.get("score", 0)))


def psi_metrics(payload: dict[str, Any], strategy: str) -> dict[str, Any]:
    """Extract LCP/CLS/TBT/score from a PageSpeed Insights v5 response (field data preferred when present)."""
    lh = payload.get("lighthouseResult") or {}
    audits = lh.get("audits") or {}
    field = (payload.get("loadingExperience") or {}).get("metrics") or {}

    def audit(name: str) -> float | None:
        v = (audits.get(name) or {}).get("numericValue")
        return float(v) if v is not None else None

    lcp_field = (field.get("LARGEST_CONTENTFUL_PAINT_MS") or {}).get("percentile")
    cls_field = (field.get("CUMULATIVE_LAYOUT_SHIFT_SCORE") or {}).get("percentile")
    perf = ((lh.get("categories") or {}).get("performance") or {}).get("score")
    return {"strategy": strategy, "field": lcp_field is not None,
            "lcp_ms": float(lcp_field) if lcp_field is not None else audit("largest-contentful-paint"),
            "cls": float(cls_field) / 100 if cls_field is not None else audit("cumulative-layout-shift"),
            "tbt_ms": audit("total-blocking-time"), "server_response_ms": audit("server-response-time"),
            "performance_score": round(float(perf) * 100) if perf is not None else None,
            "inp_ms": (field.get("INTERACTION_TO_NEXT_PAINT") or {}).get("percentile")}


def citation_hit(answer: dict[str, Any], page_url: str, brand: str | None) -> tuple[bool, bool]:
    host = _host(page_url).removeprefix("www.")
    srcs = " ".join(answer.get("sources") or []).lower()
    cited = bool(host) and (host in srcs or page_url.lower() in srcs)
    text = f"{answer.get('answer', '')} {' '.join(answer.get('brands') or [])}".lower()
    return cited, bool(brand) and brand.lower() in text


# ============================================================================ agent contract


class GeoInput(BaseModel):
    page_url: str | None = Field(default=None, description="Page to audit (required unless html is given)")
    html: str | None = Field(default=None, description="Alternative to page_url: the page HTML (crawler/performance checks are skipped)",
                             json_schema_extra={"ui": "textarea"})
    focus_section: str | None = Field(default=None, description="Optional focus: a pillar (e.g. 'schema', 'E-E-A-T') or a page section "
                                                                "(CSS selector, id or heading text, e.g. '#faq')")
    pagespeed_api_key: str | None = Field(default=None, description="Optional PageSpeed Insights API key (falls back to $PAGESPEED_API_KEY)",
                                          json_schema_extra={"ui": "password"})
    strategy: Literal["mobile", "desktop"] = Field(default="mobile", description="PageSpeed Insights strategy")
    citation_test: bool = Field(default=False, description="Experimental: ask the model generated questions and record whether it cites the page/brand")
    citation_questions: int = Field(default=5, ge=1, le=10)
    citation_tiers: list[Literal["fast", "balanced", "pro", "complex"]] = Field(default_factory=lambda: ["balanced"],
                                                                                description="Model tiers to query (each tier may map to a different model)")
    brand_name: str | None = Field(default=None, description="Brand to look for in AI answers (defaults to the brand_voice skill)")

    @model_validator(mode="after")
    def _need_source(self) -> "GeoInput":
        if not (self.page_url or self.html):
            raise ValueError("Provide page_url or html.")
        return self


class GeoCheck(BaseModel):
    id: str
    rule: str
    pillar: str
    title: str
    status: Status
    severity: Severity
    weight: float
    message: str
    evidence: str = ""
    recommendation: str = ""


class PillarScore(BaseModel):
    key: str
    label: str
    weight: float
    score: float | None
    assessed: bool
    passed: int
    warned: int
    failed: int
    note: str = ""


class CrawlerAccess(BaseModel):
    agent: str
    vendor: str
    purpose: str
    allowed: bool | None
    note: str = ""


class LlmsTxt(BaseModel):
    url: str | None = None
    present: bool | None = None
    valid: bool | None = None
    sections: int = 0
    links: int = 0


class SchemaEntity(BaseModel):
    type: str
    source: str
    path: str
    missing_required: list[str] = Field(default_factory=list)
    missing_recommended: list[str] = Field(default_factory=list)


class StructuredData(BaseModel):
    blocks: int
    parse_errors: list[str]
    types: list[str]
    entities: list[SchemaEntity]
    microdata_items: int = 0


class Performance(BaseModel):
    ttfb_ms: float | None = None
    ttfb_is_proxy: bool = True
    html_kb: float
    render_blocking: list[str]
    images: int
    images_without_dimensions: int
    scripts: int
    third_party_script_hosts: list[str]
    psi: dict[str, Any] | None = None
    note: str = ""


class ActionItem(BaseModel):
    title: str
    pillar: str
    priority: Literal["P1", "P2", "P3"]
    effort: Literal["low", "medium", "high"] = "medium"
    impact: Literal["low", "medium", "high"] = "medium"
    rationale: str = ""
    steps: list[str] = Field(default_factory=list)
    check_ids: list[str] = Field(default_factory=list)
    source: Literal["model", "rule_pack"] = "rule_pack"


class CitationResult(BaseModel):
    question: str
    tier: str
    cited_url: bool
    brand_mentioned: bool
    sources: list[str] = Field(default_factory=list)
    answer_excerpt: str = ""


class CitationTest(BaseModel):
    enabled: bool
    ran: bool
    experimental: bool = True
    label: str = ("EXPERIMENTAL - measures whether the configured model(s) cite this page or brand when answering generated "
                  "questions from their own knowledge (no live browsing). Results vary by model and run; treat as a directional signal.")
    questions: list[str] = Field(default_factory=list)
    results: list[CitationResult] = Field(default_factory=list)
    cited_rate: float | None = None
    brand_mention_rate: float | None = None
    note: str = ""


class GeoOutput(BaseModel):
    page_url: str | None
    final_url: str | None = None
    http_status: int | None = None
    page_type: str
    overall_score: float | None
    grade: str
    pillars: list[PillarScore]
    checks: list[GeoCheck]
    ai_crawlers: list[CrawlerAccess]
    llms_txt: LlmsTxt
    performance: Performance
    structured_data: StructuredData
    focus_section: str | None = None
    focus_pillar: str | None = None
    focus_found: bool | None = None
    executive_summary: str
    action_plan: list[ActionItem]
    plan_source: Literal["model", "rule_pack"]
    citation_test: CitationTest
    rule_pack_version: str
    findings: list[Finding]


class PlanAction(BaseModel):
    title: str
    pillar: str = Field(description="crawler_access | performance | structured_data | eeat | citation_readiness")
    priority: Literal["P1", "P2", "P3"] = "P2"
    effort: Literal["low", "medium", "high"] = "medium"
    impact: Literal["low", "medium", "high"] = "medium"
    rationale: str = ""
    steps: list[str] = Field(default_factory=list, max_length=6)
    check_ids: list[str] = Field(default_factory=list, description="ids of the failing checks this action fixes, copied exactly")


class GeoPlan(BaseModel):
    executive_summary: str
    actions: list[PlanAction] = Field(default_factory=list)


class CitationQuestions(BaseModel):
    questions: list[str] = Field(min_length=1, max_length=10)


class CitationAnswer(BaseModel):
    answer: str
    sources: list[str] = Field(default_factory=list, description="URLs or publication names you would cite")
    brands: list[str] = Field(default_factory=list, description="Companies or brands named in the answer")


# ============================================================================ HTML report

REPORT_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>GEO audit - {{ page }}</title>
<style>
:root{--ink:#18212f;--muted:#5d6676;--line:#e2e5eb;--bg:#f5f6f8;--card:#fff;--pass:#067647;--warn:#b54708;--fail:#b42318;--na:#98a2b3;--accent:#3b2f80}
*{box-sizing:border-box}body{margin:0;font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:var(--ink);background:var(--bg)}
header{background:var(--accent);color:#fff;padding:28px 32px 64px}header h1{margin:0 0 4px;font-size:24px}header p{margin:0;opacity:.85;word-break:break-all}
main{max-width:1120px;margin:-44px auto 0;padding:0 20px 48px}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:20px 22px;margin-bottom:18px}
h2{font-size:18px;margin:0 0 12px}.score{display:flex;gap:28px;align-items:center;flex-wrap:wrap}
.big{font-size:56px;font-weight:700;line-height:1}.grade{font-size:22px;font-weight:700;padding:6px 14px;border-radius:8px;background:#eef0fb;color:var(--accent)}
.bars{flex:1;min-width:280px}.bar{display:grid;grid-template-columns:170px 1fr 52px;gap:10px;align-items:center;margin:6px 0;font-size:14px}
.track{background:#edf0f4;border-radius:6px;height:12px;overflow:hidden}.fill{height:100%;background:var(--accent)}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;padding:7px 9px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.03em}
.s{font-weight:700;font-size:12px;text-transform:uppercase}.pass{color:var(--pass)}.warn{color:var(--warn)}.fail{color:var(--fail)}.na{color:var(--na)}
.pill{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;font-weight:600;color:#fff}.P1{background:var(--fail)}.P2{background:var(--warn)}.P3{background:#475467}
.muted{color:var(--muted)}.note{font-size:13px;color:var(--muted)}.exp{display:inline-block;background:#fef0c7;color:#7a2e0e;font-weight:700;font-size:12px;padding:2px 8px;border-radius:6px}
ol.steps{margin:4px 0 0 18px;padding:0}code{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12.5px}
</style></head><body>
<header><h1>Generative Engine Optimization audit</h1><p>{{ page }} &middot; {{ page_type }} page &middot; {{ generated }}</p></header>
<main>
<section><div class="score"><div><div class="big">{{ overall }}</div><div class="muted">overall GEO score</div></div><div class="grade">{{ grade }}</div>
<div class="bars">{% for p in pillars %}<div class="bar"><span>{{ p.label }}</span><div class="track"><div class="fill" style="width:{{ p.width }}%"></div></div><b>{{ p.score_label }}</b></div>{% endfor %}</div></div>
{% if focus_label %}<p class="note">Focus: {{ focus_label }}</p>{% endif %}</section>
<section><h2>Executive summary</h2><p>{{ executive_summary }}</p><p class="note">Scores are computed in code from rule pack v{{ rule_pack_version }}; the action plan was written by {{ plan_label }}.</p></section>
<section><h2>Prioritised action plan</h2><table><thead><tr><th>Priority</th><th>Action</th><th>Pillar</th><th>Effort</th><th>Impact</th></tr></thead><tbody>
{% for a in actions %}<tr><td><span class="pill {{ a.priority }}">{{ a.priority }}</span></td><td><b>{{ a.title }}</b>{% if a.rationale %}<br><span class="muted">{{ a.rationale }}</span>{% endif %}
<ol class="steps">{% for s in a.steps %}<li>{{ s }}</li>{% endfor %}</ol></td><td>{{ a.pillar_label }}</td><td>{{ a.effort }}</td><td>{{ a.impact }}</td></tr>{% endfor %}
</tbody></table>{% if not actions %}<p class="pass">Nothing to fix - every check passed.</p>{% endif %}</section>
<section><h2>AI crawler access</h2><table><thead><tr><th>User agent</th><th>Vendor</th><th>Purpose</th><th>Access</th></tr></thead><tbody>
{% for b in bots %}<tr><td><code>{{ b.agent }}</code></td><td>{{ b.vendor }}</td><td>{{ b.purpose }}</td><td class="s {{ b.css }}">{{ b.label }}</td></tr>{% endfor %}
</tbody></table><p class="note">llms.txt: {{ llms_label }}</p></section>
{% for p in pillar_checks %}<section><h2>{{ p.label }} <span class="muted">{{ p.score_label }}</span></h2><table><thead><tr><th>Status</th><th>Check</th><th>Result</th></tr></thead><tbody>
{% for c in p.rows %}<tr><td class="s {{ c.status }}">{{ c.status }}</td><td>{{ c.title }}</td><td>{{ c.message }}{% if c.evidence %}<br><span class="muted">{{ c.evidence }}</span>{% endif %}</td></tr>{% endfor %}
</tbody></table></section>{% endfor %}
<section><h2>Citation test <span class="exp">EXPERIMENTAL</span></h2><p class="note">{{ citation.label }}</p>
{% if citation.ran %}<p>Cited in {{ citation.cited_label }} of answers; brand mentioned in {{ citation.brand_label }}.</p>
<table><thead><tr><th>Question</th><th>Model tier</th><th>Page cited</th><th>Brand named</th></tr></thead><tbody>
{% for r in citation.results %}<tr><td>{{ r.question }}</td><td>{{ r.tier }}</td><td>{{ "yes" if r.cited_url else "no" }}</td><td>{{ "yes" if r.brand_mentioned else "no" }}</td></tr>{% endfor %}
</tbody></table>{% else %}<p class="muted">{{ citation.note }}</p>{% endif %}</section>
</main></body></html>"""


def render_report(context: dict[str, Any]) -> str:
    import jinja2

    env = jinja2.Environment(autoescape=True, trim_blocks=True, lstrip_blocks=True)
    return env.from_string(REPORT_TEMPLATE).render(**context)


def deterministic_summary(overall: float | None, grade: str, pillars: list[dict[str, Any]], plan: list[dict[str, Any]], ptype: str) -> str:
    assessed = [p for p in pillars if p["assessed"]]
    if not assessed:
        return "No GEO checks could be assessed."
    weakest = sorted(assessed, key=lambda p: p["score"])[:2]
    strongest = max(assessed, key=lambda p: p["score"])
    p1 = [a["title"] for a in plan if a["priority"] == "P1"][:3]
    return (f"This {ptype} page scores {overall:.0f}/100 (grade {grade}) for generative engine optimisation. "
            f"Strongest pillar: {strongest['label']} ({strongest['score']:.0f}); weakest: "
            + ", ".join(f"{p['label']} ({p['score']:.0f})" for p in weakest) + ". "
            + (f"Start with: {'; '.join(p1)}." if p1 else f"{len(plan)} improvement(s) recommended."))


# ============================================================================ the agent


@register
class GeoRecommendationsAgent(BaseAgent):
    spec = AgentSpec(
        id="geo_recommendations",
        name="GEO Recommendations",
        category=Category.WEB,
        summary="Audits a page for AI-search visibility (AI crawler access, llms.txt, performance, JSON-LD, E-E-A-T, citation readiness), "
                "scores each pillar in code and writes a prioritised action plan; optional experimental citation testing.",
        kind=AgentKind.HYBRID,
        tier=Tier.PRO,
        connectors=["crawler"],
        skills=["geo_rules", "brand_voice"],
        keywords=["geo", "generative engine optimization", "ai search", "ai overviews", "chatgpt visibility", "perplexity", "llms.txt",
                  "schema markup", "e-e-a-t", "citation"],
        produces=["geo_report.html", "geo_audit.json"],
        feeds=["experiment_ideation", "technical_seo_recommendations", "blog_post_creation"],
    )
    Input = GeoInput
    Output = GeoOutput
    schemas = {"plan": GeoPlan, "questions": CitationQuestions, "answer": CitationAnswer}

    def selftest_input(self) -> dict[str, Any]:
        return {"page_url": "https://www.meridian-mutual.example/products/home-insurance.html"}

    def stages(self):
        return [("fetch", self.fetch), ("audit", self.audit), ("score", self.score), ("citation_test", self.citation_test),
                ("plan", self.plan), ("report", self.report)]

    # -- 1. fetch --------------------------------------------------------------------------------
    def fetch(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp = state["inputs"]
        url = inp.get("page_url")
        warnings, degraded = [], []
        env: dict[str, Any] = {"robots": None, "bots": None, "llms": None, "ttfb_ms": None, "psi": None, "homepage_org": None}
        pack = ctx.skill("geo_rules")
        if inp.get("html"):
            html, headers, status, final = inp["html"], {}, None, url
            if not url:
                warnings.append("Pasted HTML without page_url: crawler access, llms.txt, TTFB and homepage checks were skipped.")
                degraded.append("html input: crawler-access and performance fetches skipped")
                return {"data": {"html": html, "headers": headers, "url": "https://page.invalid/", "status": None, "final_url": None, "env": env, "pack": pack},
                        "warnings": warnings, "provenance": degraded_update(state, degraded)}
        crawler = ctx.connector("crawler")
        html, headers, status, final = inp.get("html"), {}, None, url
        if not html:
            page = crawler.fetch(url)
            if not page.ok:
                raise_msg = page.error or f"HTTP {page.status}"
                warnings.append(f"Could not fetch {url}: {raise_msg}")
                degraded.append("page could not be fetched")
                html = page.html or ""
            else:
                html = page.html
            headers, status, final = page.headers, page.status, page.final_url
            env["ttfb_ms"] = float(page.elapsed_ms)
        root = f"{urlsplit(url).scheme}://{urlsplit(url).netloc}"
        rb = crawler.fetch(f"{root}/robots.txt")
        found = rb.status == 200 and bool(rb.html) and not rb.html.lstrip().lower().startswith(("<!doctype", "<html"))
        env["robots"] = {"url": f"{root}/robots.txt", "found": found, "status": rb.status}
        env["bots"] = evaluate_ai_crawlers(rb.html if found else ("" if rb.status in (404, 410) else None), final or url, pack.get("ai_crawlers", []))
        if not found and rb.status not in (404, 410):
            warnings.append(f"robots.txt could not be read ({rb.status or rb.error}); AI crawler access not judged.")
        lt = crawler.fetch(f"{root}/llms.txt")
        lt_ok = lt.status == 200 and bool(lt.html) and not lt.html.lstrip().lower().startswith(("<!doctype", "<html"))
        env["llms"] = parse_llms_txt(lt.html if lt_ok else None)
        env["llms_url"] = f"{root}/llms.txt"
        is_fixture = bool(getattr(crawler, "is_fixture", lambda u: False)(url))
        env["fixture"] = is_fixture
        if urlsplit(url).path not in ("", "/"):
            home = crawler.fetch(f"{root}/")
            if home.ok:
                hf = extract_geo_facts(f"{root}/", home.html)
                env["homepage_org"] = hf["org_schema_on_page"]
        # PageSpeed Insights (optional, needs a key and a publicly reachable URL)
        key = inp.get("pagespeed_api_key") or os.environ.get("PAGESPEED_API_KEY")
        if key and not is_fixture:
            try:
                import httpx

                r = httpx.get(PSI_ENDPOINT, params={"url": url, "key": key, "strategy": inp.get("strategy", "mobile"), "category": "performance"}, timeout=90)
                r.raise_for_status()
                env["psi"] = psi_metrics(r.json(), inp.get("strategy", "mobile"))
            except Exception as exc:
                env["psi_note"] = f"PageSpeed Insights failed: {type(exc).__name__}"
                warnings.append(f"PageSpeed Insights call failed ({type(exc).__name__}); Core Web Vitals not scored.")
        elif key and is_fixture:
            env["psi_note"] = "demo fixture host is not publicly reachable"
        else:
            env["psi_note"] = "no API key supplied"
        if is_fixture:
            warnings.append("Demo fixture host: fetch time is a local measurement, not a real TTFB.")
        return {"data": {"html": html, "headers": headers, "url": final or url, "status": status, "final_url": final, "env": env, "pack": pack},
                "warnings": warnings, "provenance": degraded_update(state, degraded)}

    # -- 2. audit --------------------------------------------------------------------------------
    def audit(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        pack = data["pack"]
        brand = inp.get("brand_name") or ctx.brand.get("name")
        focus = inp.get("focus_section")
        focus_pillar = resolve_focus(focus, pack.get("pillars", {}))
        section_focus = focus if focus and not focus_pillar else None
        facts = extract_geo_facts(data["url"], data["html"], data["headers"], focus=section_focus, brand=brand)
        facts["page_type"] = detect_page_type(facts, url_known=bool(inp.get("page_url")))
        env = {**data["env"], "brand": brand}
        checks = evaluate_geo(facts, env, pack, datetime.now(timezone.utc).date())
        warnings = []
        if section_focus and not facts["focus_found"]:
            warnings.append(f"Focus section '{section_focus}' was not found on the page; citation checks used the main content.")
        slim = {k: v for k, v in facts.items() if k not in ("schema",)}
        slim["schema"] = {k: v for k, v in facts["schema"].items() if k != "entities"}
        slim["schema"]["validated"] = [v for v in (validate_entity(e, pack.get("schema_types", {}), set(pack.get("strict_nested_types", [])))
                                       for e in facts["schema"]["entities"]) if v]
        return {"data": {"facts": slim, "geo_checks": checks, "focus_pillar": focus_pillar, "section_focus": section_focus, "brand": brand},
                "warnings": warnings}

    # -- 3. score --------------------------------------------------------------------------------
    def score(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        data = state["data"]
        pack = data["pack"]
        pillars = score_pillars(data["geo_checks"], pack.get("pillars", {}))
        overall = overall_score(pillars)
        grade = grade_for(overall, pack.get("grades", []))
        checks = [Check(rule=c["id"], status="info" if c["status"] == "na" else c["status"], severity=c["severity"], message=f"{c['title']}: {c['message']}",
                        ref=f"GEO pillar: {pack['pillars'][c['pillar']]['label']}", target=data["url"]) for c in data["geo_checks"]]
        degraded = [f"{p['label']} not assessed" for p in pillars if not p["assessed"]]
        return {"data": {"pillars": pillars, "overall": overall, "grade": grade}, "checks": checks,
                "provenance": degraded_update(state, degraded)}

    # -- 4. citation test (experimental, optional) ---------------------------------------------------
    def citation_test(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        test = CitationTest(enabled=bool(inp.get("citation_test")), ran=False)
        if not test.enabled:
            test.note = "Not requested (set citation_test=true with a configured model)."
            return {"data": {"citation": test.model_dump()}}
        if ctx.llm.offline:
            test.note = "Skipped: no language model configured (offline mode)."
            return {"data": {"citation": test.model_dump()}, "warnings": ["Citation test skipped: no language model configured."]}
        facts = data["facts"]
        soup = BeautifulSoup(data["html"], "lxml")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        headings = [" ".join(h.get_text(" ").split()) for h in soup.find_all(["h1", "h2", "h3"])][:20]
        n = int(inp.get("citation_questions", 5))
        try:
            qs = ctx.generate("questions", CitationQuestions, page_url=data["url"], title=title, headings=headings, count=n,
                              page_questions=[q["question"] for q in facts["questions"]][:10])
            questions = [q.strip() for q in qs.questions if q.strip()][:n]
        except Exception as exc:
            test.note = f"Question generation failed: {type(exc).__name__}"
            return {"data": {"citation": test.model_dump()}, "warnings": [test.note]}
        results = []
        for tier in inp.get("citation_tiers") or ["balanced"]:
            for q in questions:
                try:
                    ans = ctx.generate("answer", CitationAnswer, tier=tier, question=q)
                except Exception as exc:  # one failing model must not sink the run
                    ctx.log("citation answer failed", tier=tier, error=repr(exc))
                    continue
                cited, brand_hit = citation_hit(ans.model_dump(), data["url"], data.get("brand"))
                results.append(CitationResult(question=q, tier=tier, cited_url=cited, brand_mentioned=brand_hit,
                                              sources=ans.sources[:8], answer_excerpt=ans.answer[:300]))
        test.ran = bool(results)
        test.questions = questions
        test.results = results
        if results:
            test.cited_rate = round(sum(r.cited_url for r in results) / len(results), 3)
            test.brand_mention_rate = round(sum(r.brand_mentioned for r in results) / len(results), 3)
        return {"data": {"citation": test.model_dump()}}

    # -- 5. plan (model) -----------------------------------------------------------------------------
    def plan(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        data = state["data"]
        pack, checks = data["pack"], data["geo_checks"]
        failing = [c for c in checks if c["status"] in ("fail", "warn")]
        labels = {k: v["label"] for k, v in pack.get("pillars", {}).items()}
        prompt_checks = []
        for c in sorted(failing, key=lambda c: check_priority(c, pack)[1], reverse=True)[:30]:
            prio, _ = check_priority(c, pack)
            prompt_checks.append({"id": c["id"], "pillar": c["pillar"], "status": c["status"], "severity": c["severity"], "priority": prio,
                                  "title": c["title"], "message": c["message"], "recommendation": c["recommendation"]})
        pillars = [f"{p['label']} ({p['key']}): {p['score']:.0f}/100" if p["assessed"] else f"{p['label']} ({p['key']}): not assessed" for p in data["pillars"]]
        model_plan = ctx.generate("plan", GeoPlan, page_url=data["url"], page_type=data["facts"]["page_type"],
                                  overall=f"{data['overall']:.0f}" if data["overall"] is not None else "n/a", grade=data["grade"],
                                  pillars=pillars, checks=prompt_checks, focus=labels.get(data.get("focus_pillar") or "", data.get("section_focus") or ""),
                                  citation=data["citation"])
        use_model = not ctx.llm.offline
        if use_model:
            plan = reconcile_plan([a.model_dump() for a in model_plan.actions], checks, pack, data.get("focus_pillar"))
        else:
            plan = deterministic_plan(checks, pack, data.get("focus_pillar"))
        summary = model_plan.executive_summary.strip() if use_model and model_plan.executive_summary.strip() else \
            deterministic_summary(data["overall"], data["grade"], data["pillars"], plan, data["facts"]["page_type"])
        source = "model" if use_model and any(a.get("source") == "model" for a in plan) else "rule_pack"
        return {"data": {"plan": plan, "executive_summary": summary, "plan_source": source}}

    # -- 6. report -------------------------------------------------------------------------------
    def report(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        pack, facts, env = data["pack"], data["facts"], data["env"]
        labels = {k: v["label"] for k, v in pack.get("pillars", {}).items()}
        bots = [CrawlerAccess(agent=b["agent"], vendor=b["vendor"], purpose=b["purpose"], allowed=b["allowed"], note=b.get("note", ""))
                for b in (env.get("bots") or [])]
        llms = env.get("llms") or {}
        perf = Performance(ttfb_ms=env.get("ttfb_ms"), ttfb_is_proxy=True, html_kb=round(facts["html_bytes"] / 1024, 1),
                           render_blocking=facts["render_blocking"], images=facts["images"],
                           images_without_dimensions=len(facts["images_without_dimensions"]), scripts=facts["scripts"],
                           third_party_script_hosts=facts["third_party_scripts"], psi=env.get("psi"),
                           note=env.get("psi_note", "") + ("; fetch time measured against a local fixture" if env.get("fixture") else ""))
        sd = StructuredData(blocks=facts["schema"]["blocks"], parse_errors=facts["schema"]["errors"], types=facts["schema"]["types"],
                            entities=[SchemaEntity(**v) for v in facts["schema"]["validated"]], microdata_items=facts["schema"]["microdata_items"])
        plan = [ActionItem(**{k: a[k] for k in ActionItem.model_fields if k in a}) for a in data["plan"]]
        sev_by_id = {c["id"]: c["severity"] for c in data["geo_checks"]}
        findings = []
        for a in plan:
            sev = max((sev_by_id.get(i, "low") for i in a.check_ids), key=lambda s: ["low", "medium", "high", "critical"].index(s), default="low")
            ev = [c["message"] for c in data["geo_checks"] if c["id"] in a.check_ids][:3]
            findings.append(Finding(title=a.title, detail=a.rationale, severity=sev, category="geo", evidence="; ".join(ev),
                                    recommendation=" ".join(a.steps[:3]), target=data["url"], source_agent=self.spec.id))
        citation = CitationTest.model_validate(data["citation"])
        out = GeoOutput(page_url=inp.get("page_url"), final_url=data.get("final_url"), http_status=data.get("status"), page_type=facts["page_type"],
                        overall_score=data["overall"], grade=data["grade"], pillars=[PillarScore(**p) for p in data["pillars"]],
                        checks=[GeoCheck(**{k: c[k] for k in GeoCheck.model_fields}) for c in data["geo_checks"]], ai_crawlers=bots,
                        llms_txt=LlmsTxt(url=env.get("llms_url"), present=llms.get("present"), valid=llms.get("valid"),
                                         sections=llms.get("sections", 0), links=llms.get("links", 0)),
                        performance=perf, structured_data=sd, focus_section=inp.get("focus_section"), focus_pillar=data.get("focus_pillar"),
                        focus_found=facts.get("focus_found"), executive_summary=data["executive_summary"], action_plan=plan,
                        plan_source=data["plan_source"], citation_test=citation, rule_pack_version=str(pack.get("version", "")), findings=findings)

        def score_label(p: dict[str, Any]) -> str:
            return f"{p['score']:.0f}" if p["assessed"] else "n/a"

        html = render_report({
            "page": data["url"], "page_type": facts["page_type"], "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "overall": f"{out.overall_score:.0f}" if out.overall_score is not None else "n/a", "grade": out.grade,
            "pillars": [{**p, "width": p["score"] or 0, "score_label": score_label(p)} for p in data["pillars"]],
            "focus_label": (labels.get(out.focus_pillar or "") or out.focus_section or "") if out.focus_section else "",
            "executive_summary": out.executive_summary, "rule_pack_version": out.rule_pack_version,
            "plan_label": "a language model from the failing checks" if out.plan_source == "model" else "the rule pack (no model configured)",
            "actions": [{**a.model_dump(), "pillar_label": labels.get(a.pillar, a.pillar)} for a in plan],
            "bots": [{**b.model_dump(), "css": "na" if b.allowed is None else ("pass" if b.allowed else ("warn" if b.purpose == "training" else "fail")),
                      "label": "unknown" if b.allowed is None else ("allowed" if b.allowed else "blocked")} for b in bots],
            "llms_label": "not checked" if llms.get("present") is None else ("present" if llms.get("present") else f"not found at {env.get('llms_url')}"),
            "pillar_checks": [{"label": p["label"], "score_label": score_label(p), "rows": [c for c in data["geo_checks"] if c["pillar"] == p["key"]]}
                              for p in data["pillars"]],
            "citation": {**citation.model_dump(), "cited_label": f"{(citation.cited_rate or 0) * 100:.0f}%",
                         "brand_label": f"{(citation.brand_mention_rate or 0) * 100:.0f}%"}})
        artifacts = [ctx.artifacts.write_text("geo_report.html", html, "text/html", "GEO audit report"),
                     ctx.artifacts.write_json("geo_audit.json", {"pillars": data["pillars"], "checks": data["geo_checks"], "facts": facts},
                                              "GEO checks and extracted page facts")]
        weakest = min((p for p in data["pillars"] if p["assessed"]), key=lambda p: p["score"], default=None)
        kind = "this page" if facts["page_type"] == "page" else f"{facts['page_type']} page"
        llms_state = "not checked" if llms.get("present") is None else ("present" if llms.get("present") else "missing")
        summary = (f"GEO score {out.overall_score:.0f}/100 ({out.grade}) for {kind}; "
                   f"{sum(1 for b in bots if b.allowed is False)} AI crawler(s) blocked, llms.txt {llms_state}; "
                   f"{len(plan)} action(s)" + (f"; weakest pillar: {weakest['label']} ({weakest['score']:.0f})" if weakest else "")) \
            if out.overall_score is not None else f"GEO audit: {len(plan)} action(s)"
        return {"output": out.model_dump(mode="json"), "artifacts": artifacts, "summary": summary}
