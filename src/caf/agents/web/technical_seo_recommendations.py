"""Technical SEO Recommendations - a crawler + YAML rules engine you own; the model only explains.

Stages (linear LangGraph):
  crawl       BFS crawl through the crawler connector, robots.txt, sitemap(s) and bounded verification
              fetches for link, canonical, hreflang and sitemap targets the crawl did not reach
  evaluate    config/skills/seo_rules.yaml evaluated by small pure check functions (CHECKS)
  prioritise  severity x page importance (first-party clicks/impressions from the keyword_data
              connector, or internal in-link share as a labelled proxy) -> P1 / P2 / P3
  explain     the model explains and orders the findings it is handed - it never detects them
  report      issues CSV + HTML report; `findings` feed experiment_ideation

Everything that decides *what is broken* is deterministic and auditable, so the same rule pack
can gate a CI pipeline or a post-migration check. A browser port only needs the pure functions.
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
from collections import defaultdict
from typing import Any, Callable, Literal
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup
from pydantic import BaseModel, Field, model_validator

from ...core import AgentKind, AgentSpec, BaseAgent, Category, Check, RunContext, Tier, register
from ...core.handoff import SEVERITY_ORDER, Finding

Severity = Literal["low", "medium", "high", "critical"]
Priority = Literal["P1", "P2", "P3"]
HTML_PLACEHOLDER_URL = "https://page.invalid/"


def degraded_update(state: dict[str, Any], reasons: list[str], **extra: Any) -> dict[str, Any]:
    """Provenance update for one stage (the core's merge_provenance reducer accumulates degraded_reasons)."""
    out: dict[str, Any] = dict(extra)
    if reasons:
        out["degraded_reasons"] = list(dict.fromkeys(reasons))
    return out

# ============================================================================ URL + parsing helpers


def norm_url(url: str, base: str | None = None) -> str:
    """Absolute URL without fragment, lower-case scheme/host, default port dropped, empty path -> '/'."""
    raw = urljoin(base, url.strip()) if base else url.strip()
    raw = urldefrag(raw)[0]
    p = urlsplit(raw)
    scheme = p.scheme.lower()
    host = (p.hostname or "").lower()
    try:
        port = p.port
    except ValueError:
        port = None
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    return urlunsplit((scheme, host, p.path or "/", p.query, ""))


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def same_page(a: str, b: str) -> bool:
    """True when two URLs differ at most by query string (a parameter variant of the same page)."""
    pa, pb = urlsplit(norm_url(a)), urlsplit(norm_url(b))
    return (pa.scheme, pa.netloc, pa.path) == (pb.scheme, pb.netloc, pb.path)


def short_url(url: str, start_url: str | None) -> str:
    if start_url and host_of(url) == host_of(start_url):
        p = urlsplit(url)
        return p.path + (f"?{p.query}" if p.query else "")
    return url


def parse_jsonld(raw: str) -> tuple[Any, str | None]:
    """Parse one <script type="application/ld+json"> body. Returns (data, error)."""
    text = (raw or "").strip()
    text = re.sub(r"^\s*(<!--|<!\[CDATA\[)", "", text)
    text = re.sub(r"(-->|\]\]>)\s*$", "", text).strip()
    if not text:
        return None, "empty JSON-LD block"
    try:
        return json.loads(text), None
    except json.JSONDecodeError as exc:
        return None, f"{exc.msg} (line {exc.lineno}, column {exc.colno})"


def jsonld_types(data: Any) -> list[str]:
    """Top-level @type values (lists, @graph and arrays of entities are flattened)."""
    out: list[str] = []
    items = data if isinstance(data, list) else [data]
    for item in items:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("@graph"), list):
            out += jsonld_types(item["@graph"])
        t = item.get("@type")
        out += [str(x) for x in t] if isinstance(t, list) else ([str(t)] if t else [])
    return out


_LANG_RX = re.compile(r"^[a-zA-Z]{2,3}(-[a-zA-Z0-9]{2,8})*$")
_HREFLANG_RX = re.compile(r"^(x-default|[a-z]{2}(-[a-z]{4})?(-([a-z]{2}|\d{3}))?)$", re.I)


def heading_skips(headings: list[tuple[int, str]]) -> list[dict[str, Any]]:
    """Consecutive headings that jump more than one level down (h2 -> h4)."""
    skips = []
    for (prev, _), (level, text) in zip(headings, headings[1:]):
        if level > prev + 1:
            skips.append({"from": prev, "to": level, "text": text[:80]})
    return skips


def extract_page_facts(url: str, html: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    """Everything the page-level rules need, as plain JSON-able data (pure; no network)."""
    soup = BeautifulSoup(html or "", "lxml")
    head = soup.head or soup
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    page_host = host_of(url)

    titles = [t.get_text(" ", strip=True) for t in head.find_all("title")]
    descs = [m.get("content", "").strip() for m in soup.find_all("meta", attrs={"name": re.compile(r"^description$", re.I)})]
    canon_tags = [l for l in soup.find_all("link", href=True) if "canonical" in [r.lower() for r in (l.get("rel") or [])]]
    canonicals = [norm_url(l["href"], url) for l in canon_tags]
    hreflang = []
    for l in soup.find_all("link", href=True, hreflang=True):
        if "alternate" in [r.lower() for r in (l.get("rel") or [])]:
            hreflang.append({"lang": l["hreflang"].strip(), "href": norm_url(l["href"], url)})
    robots_meta = " ".join(m.get("content", "") for m in soup.find_all("meta", attrs={"name": re.compile(r"^(robots|googlebot)$", re.I)})).lower()
    x_robots = headers.get("x-robots-tag", "").lower()
    directives = {d.strip() for d in re.split(r"[,\s]+", f"{robots_meta} {x_robots}") if d.strip()}
    html_tag = soup.find("html")
    lang = (html_tag.get("lang") or "").strip() if html_tag else ""

    headings = []
    for h in soup.find_all(re.compile(r"^h[1-6]$")):
        headings.append((int(h.name[1]), h.get_text(" ", strip=True)))
    jsonld = []
    for i, s in enumerate(soup.find_all("script", attrs={"type": re.compile(r"^\s*application/ld\+json\s*$", re.I)}), 1):
        data, err = parse_jsonld(s.string or s.get_text())
        ctx_ok = isinstance(data, dict) and ("@context" in data or "@graph" in data) or isinstance(data, list)
        jsonld.append({"index": i, "error": err, "types": jsonld_types(data) if err is None else [],
                       "has_context": bool(ctx_ok)})
    images = soup.find_all("img")
    missing_alt = [img.get("src", "")[:200] for img in images if img.get("alt") is None]

    internal, external = [], []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
            continue
        absu = norm_url(href, url)
        if not absu.startswith(("http://", "https://")):
            continue
        (internal if host_of(absu) == page_host else external).append(absu)
    resources = [norm_url(l["href"], url) for l in soup.find_all("link", href=True) if "stylesheet" in [r.lower() for r in (l.get("rel") or [])]]
    resources += [norm_url(s["src"], url) for s in soup.find_all("script", src=True)]
    for t in soup(["script", "style", "noscript", "template"]):
        t.decompose()
    body_text = " ".join((soup.body or soup).get_text(" ").split())
    return {
        "url": url,
        "title": titles[0] if titles else "",
        "title_count": len(titles),
        "meta_description": descs[0] if descs else "",
        "meta_description_count": len(descs),
        "canonical": canonicals[0] if canonicals else None,
        "canonical_count": len(canonicals),
        "hreflang": hreflang,
        "noindex": bool({"noindex", "none"} & directives),
        "robots_directives": sorted(directives),
        "lang": lang,
        "h1": [t for lvl, t in headings if lvl == 1],
        "headings": headings,
        "heading_skips": heading_skips(headings),
        "jsonld": jsonld,
        "images_total": len(images),
        "images_missing_alt": missing_alt,
        "links_internal": sorted(set(internal)),
        "links_external": sorted(set(external)),
        "resources": sorted(set(resources)),
        "word_count": len(body_text.split()),
        "html_bytes": len((html or "").encode("utf-8")),
    }


def parse_sitemap(xml_text: str) -> dict[str, Any]:
    """Return {'kind': 'urlset'|'sitemapindex'|'invalid', 'urls': [...], 'sitemaps': [...], 'error': str|None}."""
    from lxml import etree

    try:
        root = etree.fromstring((xml_text or "").encode("utf-8"), parser=etree.XMLParser(resolve_entities=False, no_network=True))
    except Exception as exc:  # malformed XML
        return {"kind": "invalid", "urls": [], "sitemaps": [], "error": f"XML parse error: {exc}"}
    tag = etree.QName(root).localname.lower()
    locs = [(el.text or "").strip() for el in root.iter() if isinstance(el.tag, str) and etree.QName(el).localname == "loc"]
    if tag == "sitemapindex":
        return {"kind": "sitemapindex", "urls": [], "sitemaps": [l for l in locs if l], "error": None}
    if tag == "urlset":
        return {"kind": "urlset", "urls": [l for l in locs if l], "sitemaps": [], "error": None}
    return {"kind": "invalid", "urls": [], "sitemaps": [], "error": f"unexpected root element <{tag}>"}


def robots_parser(text: str | None) -> RobotFileParser | None:
    if text is None:
        return None
    rp = RobotFileParser()
    rp.parse(text.splitlines())
    return rp


def response_record(page: Any) -> dict[str, Any]:
    redirects = [[norm_url(u), int(c)] for u, c in (page.redirects or [])]
    return {
        "url": norm_url(page.url),
        "final_url": norm_url(page.final_url) if page.final_url else norm_url(page.url),
        "status": int(page.status or 0),
        "first_status": redirects[0][1] if redirects else int(page.status or 0),
        "redirects": redirects,
        "content_type": page.headers.get("content-type", "") if page.headers else "",
        "elapsed_ms": float(page.elapsed_ms or 0),
        "bytes": len((page.html or "").encode("utf-8")),
        "error": page.error,
        "is_html": bool(page.is_html) if page.html else False,
    }


# ============================================================================ site model


def build_site(start_url: str | None, records: dict[str, dict[str, Any]], page_facts: dict[str, dict[str, Any]],
               robots: dict[str, Any], sitemap: dict[str, Any], *, mode: str = "crawl", truncated: bool = False) -> dict[str, Any]:
    """Assemble the site model the checks run against (pure)."""
    inlinks: dict[str, set[str]] = defaultdict(set)
    for src, facts in page_facts.items():
        for tgt in facts["links_internal"]:
            if tgt != src:
                inlinks[tgt].add(src)
    referenced: dict[str, list[str]] = defaultdict(list)
    for tgt, srcs in inlinks.items():
        referenced[tgt].append(f"linked from {len(srcs)} page(s)")
    for u in sitemap.get("urls", []):
        referenced[norm_url(u)].append("listed in sitemap")
    for src, facts in page_facts.items():
        if facts.get("canonical") and facts["canonical"] != src:
            referenced[facts["canonical"]].append("canonical target")
        for h in facts.get("hreflang", []):
            if h["href"] != src:
                referenced[h["href"]].append("hreflang target")
    return {
        "start_url": start_url, "mode": mode, "truncated": truncated,
        "records": records, "pages": page_facts,
        "inlinks": {k: sorted(v) for k, v in inlinks.items()},
        "referenced": {k: sorted(set(v)) for k, v in referenced.items()},
        "robots": robots, "sitemap": sitemap,
    }


def _rp(site: dict[str, Any]) -> RobotFileParser | None:
    rb = site.get("robots") or {}
    return robots_parser(rb.get("text")) if rb.get("found") else None


def _record(site: dict[str, Any], url: str) -> dict[str, Any] | None:
    return site["records"].get(norm_url(url))


def _is_internal(site: dict[str, Any], url: str) -> bool:
    start = site.get("start_url")
    return not start or host_of(url) == host_of(start)


def _where(site: dict[str, Any], url: str) -> str:
    return "; ".join(site["referenced"].get(url, [])) or "discovered during crawl"


def _ref_sources(site: dict[str, Any], url: str) -> list[str]:
    srcs = list(site["inlinks"].get(url, []))
    srcs += [s for s, f in site["pages"].items() if f.get("canonical") == url or any(h["href"] == url for h in f.get("hreflang", []))]
    return sorted(set(srcs))


# ============================================================================ checks (pure)
# Each check: fn(site, params) -> list[{"url", "detail", "evidence"?, "sources"?}]

Issue = dict[str, Any]


def _indexable_representatives(site: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Pages that represent themselves: not noindex and canonical is self (or a parameter variant)."""
    out = {}
    for url, f in site["pages"].items():
        canon = f.get("canonical")
        # noindex pages and pages consolidated into another URL (incl. ?param variants) are not duplicates
        if f.get("noindex") or (canon and canon != url):
            continue
        out[url] = f
    return out


def chk_missing(site, params) -> list[Issue]:
    field = params["field"]
    label = field.replace("_", " ")
    return [{"url": u, "detail": f"No {label}"} for u, f in site["pages"].items() if not (f.get(field) or "").strip()]


def chk_duplicate(site, params) -> list[Issue]:
    field = params["field"]
    groups: dict[str, list[str]] = defaultdict(list)
    for u, f in _indexable_representatives(site).items():
        v = " ".join((f.get(field) or "").split()).lower()
        if v:
            groups[v].append(u)
    issues = []
    for urls in groups.values():
        if len(urls) < 2:
            continue
        for u in urls:
            others = [short_url(o, site.get("start_url")) for o in urls if o != u]
            issues.append({"url": u, "detail": f"Same {field.replace('_', ' ')} as {', '.join(others)}",
                           "evidence": site["pages"][u].get(field, "")[:200], "sources": [o for o in urls if o != u]})
    return issues


def chk_length(site, params) -> list[Issue]:
    field, lo, hi = params["field"], int(params.get("min", 0)), int(params.get("max", 10_000))
    out = []
    for u, f in site["pages"].items():
        v = (f.get(field) or "").strip()
        if v and not (lo <= len(v) <= hi):
            out.append({"url": u, "detail": f"{len(v)} characters (target {lo}-{hi})", "evidence": v[:200]})
    return out


def chk_count_below(site, params) -> list[Issue]:
    field, lo = params["field"], int(params.get("min", 1))
    return [{"url": u, "detail": f"{len(f.get(field) or [])} {field.upper()} element(s)"} for u, f in site["pages"].items()
            if len(f.get(field) or []) < lo]


def chk_count_above(site, params) -> list[Issue]:
    field, hi = params["field"], int(params.get("max", 1))
    out = []
    for u, f in site["pages"].items():
        vals = f.get(field) or []
        if len(vals) > hi:
            out.append({"url": u, "detail": f"{len(vals)} {field.upper()} elements", "evidence": " | ".join(v[:70] for v in vals)})
    return out


def chk_heading_skip(site, params) -> list[Issue]:
    out = []
    for u, f in site["pages"].items():
        skips = f.get("heading_skips") or []
        if skips:
            detail = "; ".join(f"H{s['from']} -> H{s['to']} ('{s['text']}')" for s in skips[:5])
            out.append({"url": u, "detail": detail, "evidence": f"{len(skips)} skip(s)"})
    return out


def chk_lang_missing(site, params) -> list[Issue]:
    return [{"url": u, "detail": "<html> has no lang attribute"} for u, f in site["pages"].items() if not f.get("lang")]


def chk_lang_invalid(site, params) -> list[Issue]:
    return [{"url": u, "detail": f"lang=\"{f['lang']}\" is not a valid BCP 47 code"} for u, f in site["pages"].items()
            if f.get("lang") and not _LANG_RX.match(f["lang"])]


def chk_noindex(site, params) -> list[Issue]:
    return [{"url": u, "detail": "robots directives: " + ", ".join(f.get("robots_directives") or [])} for u, f in site["pages"].items() if f.get("noindex")]


def chk_jsonld_parse_error(site, params) -> list[Issue]:
    out = []
    for u, f in site["pages"].items():
        bad = [b for b in f.get("jsonld") or [] if b.get("error")]
        if bad:
            out.append({"url": u, "detail": "; ".join(f"block {b['index']}: {b['error']}" for b in bad)})
    return out


def chk_jsonld_missing_type(site, params) -> list[Issue]:
    out = []
    for u, f in site["pages"].items():
        bad = [b for b in f.get("jsonld") or [] if not b.get("error") and (not b.get("types") or not b.get("has_context"))]
        if bad:
            out.append({"url": u, "detail": f"{len(bad)} block(s) without @context/@type"})
    return out


def chk_image_alt_missing(site, params) -> list[Issue]:
    out = []
    for u, f in site["pages"].items():
        miss = f.get("images_missing_alt") or []
        if miss:
            out.append({"url": u, "detail": f"{len(miss)} of {f.get('images_total', 0)} images have no alt attribute",
                        "evidence": ", ".join(m.rsplit("/", 1)[-1] for m in miss[:6])})
    return out


def chk_large_html(site, params) -> list[Issue]:
    limit = float(params.get("max_kb", 500)) * 1024
    return [{"url": u, "detail": f"{f['html_bytes'] / 1024:.0f} KB of HTML"} for u, f in site["pages"].items() if f.get("html_bytes", 0) > limit]


def chk_status_client_error(site, params) -> list[Issue]:
    out = []
    for u, r in site["records"].items():
        if not _is_internal(site, u):
            continue
        if 400 <= r["status"] < 500:
            via = f" (after redirect to {short_url(r['final_url'], site.get('start_url'))})" if r["redirects"] else ""
            out.append({"url": u, "detail": f"HTTP {r['status']}{via}", "evidence": _where(site, u), "sources": _ref_sources(site, u)})
    return out


def chk_status_server_error(site, params) -> list[Issue]:
    out = []
    for u, r in site["records"].items():
        if _is_internal(site, u) and r["status"] >= 500:
            out.append({"url": u, "detail": f"HTTP {r['status']}", "evidence": _where(site, u), "sources": _ref_sources(site, u)})
    return out


def chk_status_redirect(site, params) -> list[Issue]:
    out = []
    for u, r in site["records"].items():
        if _is_internal(site, u) and r["redirects"]:
            out.append({"url": u, "detail": f"{r['first_status']} -> {short_url(r['final_url'], site.get('start_url'))}",
                        "evidence": _where(site, u), "sources": _ref_sources(site, u)})
    return out


def chk_redirect_chain(site, params) -> list[Issue]:
    max_hops = int(params.get("max_hops", 1))
    out = []
    for u, r in site["records"].items():
        if len(r["redirects"]) > max_hops:
            hops = " -> ".join([short_url(x[0], site.get("start_url")) for x in r["redirects"]] + [short_url(r["final_url"], site.get("start_url"))])
            out.append({"url": u, "detail": f"{len(r['redirects'])} hops: {hops}"})
    return out


def chk_links_to_redirect(site, params) -> list[Issue]:
    out = []
    for u, f in site["pages"].items():
        targets = [t for t in f["links_internal"] if (_record(site, t) or {}).get("redirects")]
        if targets:
            out.append({"url": u, "detail": "Links to " + ", ".join(
                f"{short_url(t, site.get('start_url'))} ({site['records'][t]['first_status']})" for t in targets[:8]), "sources": targets})
    return out


def chk_broken_internal_links(site, params) -> list[Issue]:
    out = []
    for u, f in site["pages"].items():
        bad = [t for t in f["links_internal"] if (_record(site, t) or {}).get("status", 0) >= 400]
        if bad:
            out.append({"url": u, "detail": "Links to " + ", ".join(
                f"{short_url(t, site.get('start_url'))} ({site['records'][t]['status']})" for t in bad[:8]), "sources": bad})
    return out


def chk_canonical_missing(site, params) -> list[Issue]:
    return [{"url": u, "detail": "No <link rel=\"canonical\">"} for u, f in site["pages"].items() if not f.get("canonical")]


def chk_canonical_multiple(site, params) -> list[Issue]:
    return [{"url": u, "detail": f"{f['canonical_count']} canonical tags"} for u, f in site["pages"].items() if f.get("canonical_count", 0) > 1]


def chk_canonical_non_self(site, params) -> list[Issue]:
    out = []
    for u, f in site["pages"].items():
        c = f.get("canonical")
        if c and c != u and not same_page(c, u):
            out.append({"url": u, "detail": f"Canonical is {short_url(c, site.get('start_url'))}", "sources": [c]})
    return out


def chk_canonical_target_status(site, params) -> list[Issue]:
    out = []
    for u, f in site["pages"].items():
        c = f.get("canonical")
        r = _record(site, c) if c else None
        if not c or c == u or not r or r["status"] == 0:
            continue
        if r["redirects"]:
            out.append({"url": u, "detail": f"Canonical {short_url(c, site.get('start_url'))} redirects ({r['first_status']}) to {short_url(r['final_url'], site.get('start_url'))}", "sources": [c]})
        elif r["status"] >= 400:
            out.append({"url": u, "detail": f"Canonical {short_url(c, site.get('start_url'))} returns {r['status']}", "sources": [c]})
    return out


def chk_hreflang_target_status(site, params) -> list[Issue]:
    out = []
    for u, f in site["pages"].items():
        bad = []
        for h in f.get("hreflang") or []:
            r = _record(site, h["href"])
            if h["href"] == u or not r or r["status"] == 0:
                continue
            if r["redirects"]:
                bad.append(f"{h['lang']} -> {short_url(h['href'], site.get('start_url'))} redirects ({r['first_status']})")
            elif r["status"] != 200:
                bad.append(f"{h['lang']} -> {short_url(h['href'], site.get('start_url'))} returns {r['status']}")
        if bad:
            out.append({"url": u, "detail": "; ".join(bad), "sources": [h["href"] for h in f.get("hreflang") or []]})
    return out


def chk_hreflang_reciprocity(site, params) -> list[Issue]:
    out = []
    for u, f in site["pages"].items():
        missing = []
        for h in f.get("hreflang") or []:
            tgt = h["href"]
            if tgt == u or h["lang"].lower() == "x-default":
                continue
            tf = site["pages"].get(tgt)
            if tf is None:  # target not crawled as a 200 page; target_status covers broken ones
                continue
            back = {x["href"] for x in tf.get("hreflang") or []}
            if u not in back and (f.get("canonical") or u) not in back:
                missing.append(f"{short_url(tgt, site.get('start_url'))} ({h['lang']}) has no return tag")
        if missing:
            out.append({"url": u, "detail": "; ".join(missing)})
    return out


def chk_hreflang_x_default(site, params) -> list[Issue]:
    return [{"url": u, "detail": f"{len(f['hreflang'])} hreflang annotation(s), no x-default"} for u, f in site["pages"].items()
            if f.get("hreflang") and not any(h["lang"].lower() == "x-default" for h in f["hreflang"])]


def chk_hreflang_self_reference(site, params) -> list[Issue]:
    return [{"url": u, "detail": "hreflang cluster does not include this page"} for u, f in site["pages"].items()
            if f.get("hreflang") and not any(h["href"] in (u, f.get("canonical")) for h in f["hreflang"])]


def chk_hreflang_invalid_code(site, params) -> list[Issue]:
    out = []
    for u, f in site["pages"].items():
        bad = [h["lang"] for h in f.get("hreflang") or [] if not _HREFLANG_RX.match(h["lang"])]
        if bad:
            out.append({"url": u, "detail": "Invalid code(s): " + ", ".join(bad)})
    return out


def chk_sitemap_missing(site, params) -> list[Issue]:
    sm = site.get("sitemap") or {}
    if sm.get("found"):
        return []
    return [{"url": site.get("start_url") or "", "detail": "; ".join(sm.get("errors") or ["no sitemap found"])}]


def _sitemap_urls(site) -> list[str]:
    return [norm_url(u) for u in (site.get("sitemap") or {}).get("urls", [])]


def chk_sitemap_non_200(site, params) -> list[Issue]:
    out = []
    for u in _sitemap_urls(site):
        r = _record(site, u)
        if not r or r["status"] == 0:
            continue
        if r["redirects"]:
            out.append({"url": u, "detail": f"{r['first_status']} redirect to {short_url(r['final_url'], site.get('start_url'))}"})
        elif r["status"] != 200:
            out.append({"url": u, "detail": f"HTTP {r['status']}"})
    return out


def chk_sitemap_non_canonical(site, params) -> list[Issue]:
    out = []
    for u in _sitemap_urls(site):
        f = site["pages"].get(u)
        if f and f.get("canonical") and f["canonical"] != u:
            out.append({"url": u, "detail": f"Canonical is {short_url(f['canonical'], site.get('start_url'))}"})
    return out


def chk_sitemap_noindex(site, params) -> list[Issue]:
    return [{"url": u, "detail": "Listed in sitemap but noindex"} for u in _sitemap_urls(site) if (site["pages"].get(u) or {}).get("noindex")]


def chk_sitemap_orphan(site, params) -> list[Issue]:
    if site.get("truncated"):
        return []
    start = norm_url(site["start_url"]) if site.get("start_url") else None
    out = []
    for u in _sitemap_urls(site):
        if u == start or u not in site["pages"]:
            continue
        if not site["inlinks"].get(u):
            out.append({"url": u, "detail": "No internal links point to this page"})
    return out


def chk_sitemap_blocked(site, params) -> list[Issue]:
    rp = _rp(site)
    if rp is None:
        return []
    return [{"url": u, "detail": "Disallowed for Googlebot by robots.txt"} for u in _sitemap_urls(site) if not rp.can_fetch("Googlebot", u)]


def chk_robots_missing(site, params) -> list[Issue]:
    rb = site.get("robots") or {}
    if rb.get("found"):
        return []
    return [{"url": rb.get("url") or "", "detail": f"robots.txt returned {rb.get('status', 'no response')}"}]


def chk_robots_blocks_all(site, params) -> list[Issue]:
    rp, start = _rp(site), site.get("start_url")
    if rp is None or not start:
        return []
    blocked = [ua for ua in params.get("user_agents", ["*"]) if not rp.can_fetch(ua, start)]
    return [{"url": site["robots"]["url"], "detail": "Homepage disallowed for: " + ", ".join(blocked)}] if blocked else []


def chk_robots_no_sitemap(site, params) -> list[Issue]:
    rb = site.get("robots") or {}
    if rb.get("found") and not rb.get("sitemaps"):
        return [{"url": rb.get("url") or "", "detail": "No Sitemap: line"}]
    return []


def chk_robots_blocks_resources(site, params) -> list[Issue]:
    rp = _rp(site)
    if rp is None:
        return []
    res = sorted({r for f in site["pages"].values() for r in f.get("resources") or [] if r.endswith((".css", ".js")) or ".css?" in r or ".js?" in r})
    blocked = [r for r in res if _is_internal(site, r) and not rp.can_fetch("Googlebot", r)]
    return [{"url": site["robots"]["url"], "detail": f"{len(blocked)} CSS/JS file(s) disallowed", "evidence": ", ".join(short_url(b, site.get('start_url')) for b in blocked[:6])}] if blocked else []


def chk_slow_response(site, params) -> list[Issue]:
    limit = float(params.get("max_ms", 800))
    return [{"url": u, "detail": f"{site['records'][u]['elapsed_ms']:.0f} ms"} for u in site["pages"]
            if site["records"].get(u, {}).get("elapsed_ms", 0) > limit]


CHECKS: dict[str, Callable[[dict[str, Any], dict[str, Any]], list[Issue]]] = {
    "missing": chk_missing, "duplicate": chk_duplicate, "length": chk_length, "count_below": chk_count_below,
    "count_above": chk_count_above, "heading_skip": chk_heading_skip, "lang_missing": chk_lang_missing,
    "lang_invalid": chk_lang_invalid, "noindex": chk_noindex, "jsonld_parse_error": chk_jsonld_parse_error,
    "jsonld_missing_type": chk_jsonld_missing_type, "image_alt_missing": chk_image_alt_missing, "large_html": chk_large_html,
    "status_client_error": chk_status_client_error, "status_server_error": chk_status_server_error,
    "status_redirect": chk_status_redirect, "redirect_chain": chk_redirect_chain, "links_to_redirect": chk_links_to_redirect,
    "broken_internal_links": chk_broken_internal_links, "canonical_missing": chk_canonical_missing,
    "canonical_multiple": chk_canonical_multiple, "canonical_non_self": chk_canonical_non_self,
    "canonical_target_status": chk_canonical_target_status, "hreflang_target_status": chk_hreflang_target_status,
    "hreflang_reciprocity": chk_hreflang_reciprocity, "hreflang_x_default": chk_hreflang_x_default,
    "hreflang_self_reference": chk_hreflang_self_reference, "hreflang_invalid_code": chk_hreflang_invalid_code,
    "sitemap_missing": chk_sitemap_missing, "sitemap_non_200": chk_sitemap_non_200, "sitemap_non_canonical": chk_sitemap_non_canonical,
    "sitemap_noindex": chk_sitemap_noindex, "sitemap_orphan": chk_sitemap_orphan, "sitemap_blocked": chk_sitemap_blocked,
    "robots_missing": chk_robots_missing, "robots_blocks_all": chk_robots_blocks_all, "robots_no_sitemap": chk_robots_no_sitemap,
    "robots_blocks_resources": chk_robots_blocks_resources, "slow_response": chk_slow_response,
}

# Checks that only need the page itself (usable for pasted HTML, no crawl).
PAGE_ONLY_CHECKS = {"missing", "duplicate", "length", "count_below", "count_above", "heading_skip", "lang_missing", "lang_invalid",
                    "noindex", "jsonld_parse_error", "jsonld_missing_type", "image_alt_missing", "large_html", "canonical_missing",
                    "canonical_multiple", "canonical_non_self", "hreflang_x_default", "hreflang_self_reference", "hreflang_invalid_code"}


def select_rules(rules: list[dict[str, Any]], only: list[str] | None) -> list[dict[str, Any]]:
    active = [r for r in rules if r.get("enabled", True)]
    if not only:
        return active
    wanted = {o.lower() for o in only}
    return [r for r in active if r["id"].lower() in wanted or str(r.get("category", "")).lower() in wanted]


def consolidated_variants(site: dict[str, Any]) -> set[str]:
    """Pages whose canonical points at another page that was also crawled (e.g. ?product=pet -> /quote)."""
    return {u for u, f in site["pages"].items() if f.get("canonical") and f["canonical"] != u and f["canonical"] in site["pages"]}


def evaluate_rules(site: dict[str, Any], rules: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run every rule. Returns (issues, rule_results); rule_results carry status ran|skipped|error.

    Page-level content rules run on canonical representatives only, so a page reachable through
    many parameter variants is reported once; canonical rules still see every variant.
    """
    issues, results = [], []
    variants = consolidated_variants(site)
    rep_site = {**site, "pages": {u: f for u, f in site["pages"].items() if u not in variants}}
    for rule in rules:
        check = rule.get("check")
        fn = CHECKS.get(check)
        if fn is None:
            results.append({"rule": rule, "status": "error", "issues": 0, "note": f"unknown check '{check}'"})
            continue
        if site["mode"] == "html" and check not in PAGE_ONLY_CHECKS:
            results.append({"rule": rule, "status": "skipped", "issues": 0, "note": "needs a crawl (pasted HTML only)"})
            continue
        if check == "sitemap_orphan" and site.get("truncated"):
            results.append({"rule": rule, "status": "skipped", "issues": 0, "note": "crawl hit max_pages - orphan check inconclusive"})
            continue
        target = site if str(check).startswith("canonical_") or rule.get("scope") != "page" else rep_site
        found = fn(target, rule.get("params") or {})
        for f in found:
            issues.append({"rule_id": rule["id"], "title": rule["title"], "category": rule.get("category", ""),
                           "severity": rule.get("severity", "medium"), "url": f.get("url", ""), "detail": f.get("detail", ""),
                           "evidence": f.get("evidence", ""), "sources": f.get("sources", [])})
        results.append({"rule": rule, "status": "ran", "issues": len(found), "note": ""})
    return issues, results


# ============================================================================ prioritisation (pure)


def traffic_by_page(rows: list[dict[str, Any]], base_url: str | None) -> dict[str, dict[str, float]]:
    """Aggregate Search-Console-style rows (query, page, clicks, impressions) per normalised page URL."""
    agg: dict[str, dict[str, float]] = defaultdict(lambda: {"clicks": 0.0, "impressions": 0.0})
    for r in rows:
        page = str(r.get("page") or "").strip()
        if not page:
            continue
        url = norm_url(page, base_url) if base_url else norm_url(page)
        agg[url]["clicks"] += float(r.get("clicks") or 0)
        agg[url]["impressions"] += float(r.get("impressions") or 0)
    return dict(agg)


def page_importance(site: dict[str, Any], traffic: dict[str, dict[str, float]] | None) -> tuple[dict[str, float], str]:
    """0..1 importance per URL from clicks (+ impressions/50), or from internal in-links as a proxy."""
    if traffic:
        units = {u: t["clicks"] + t["impressions"] / 50.0 for u, t in traffic.items()}
        top = max(units.values(), default=0.0)
        if top > 0:
            return {u: math.log1p(v) / math.log1p(top) for u, v in units.items()}, "search_console"
    counts = {u: len(v) for u, v in site["inlinks"].items()}
    if site.get("start_url"):
        counts[norm_url(site["start_url"])] = max(counts.values(), default=1) or 1
    top = max(counts.values(), default=0)
    if top <= 0:
        return {}, "none"
    return {u: math.log1p(c) / math.log1p(top) for u, c in counts.items()}, "internal_links"


def prioritise(issues: list[dict[str, Any]], importance: dict[str, float], cfg: dict[str, Any], site_scope: set[str]) -> list[dict[str, Any]]:
    weights = cfg.get("severity_weights", {"critical": 10, "high": 6, "medium": 3, "low": 1})
    tw = float(cfg.get("traffic_weight", 1.5))
    th = cfg.get("priority_thresholds", {"P1": 9, "P2": 4})
    out = []
    for i in issues:
        if i["rule_id"] in site_scope:
            imp = 1.0
        else:
            imp = max([importance.get(i["url"], 0.0)] + [importance.get(s, 0.0) for s in i.get("sources", [])])
        score = round(weights.get(i["severity"], 3) * (1 + tw * imp), 2)
        out.append({**i, "importance": round(imp, 3), "priority_score": score, "priority": bucket(score, th)})
    return out


def bucket(score: float, thresholds: dict[str, float]) -> str:
    if score >= float(thresholds.get("P1", 9)):
        return "P1"
    if score >= float(thresholds.get("P2", 4)):
        return "P2"
    return "P3"


def group_issues(issues: list[dict[str, Any]], rules: dict[str, dict[str, Any]], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    bonus = float(cfg.get("breadth_bonus_per_doubling", 0.5))
    by_rule: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for i in issues:
        by_rule[i["rule_id"]].append(i)
    groups = []
    for rid, items in by_rule.items():
        rule = rules.get(rid, {})
        top = max(x["priority_score"] for x in items)
        score = round(top + bonus * math.log2(len(items)), 2)
        groups.append({"rule_id": rid, "title": rule.get("title", rid), "category": rule.get("category", ""),
                       "severity": rule.get("severity", "medium"), "priority_score": score,
                       "priority": bucket(score, cfg.get("priority_thresholds", {})), "count": len(items),
                       "urls": [x["url"] for x in sorted(items, key=lambda x: -x["priority_score"])],
                       "examples": [x["detail"] for x in sorted(items, key=lambda x: -x["priority_score"])[:3]],
                       "why_it_matters": rule.get("why", ""), "recommendation": rule.get("recommendation", ""),
                       "fix_steps": [rule.get("recommendation", "")] if rule.get("recommendation") else [],
                       "effort": rule.get("effort", "medium"), "owner_role": rule.get("owner", "")})
    groups.sort(key=lambda g: (-g["priority_score"], SEVERITY_ORDER.get(g["severity"], 9), g["rule_id"]))
    return groups


def deterministic_summary(start: str | None, pages: int, groups: list[dict[str, Any]], importance_source: str) -> str:
    if not groups:
        return f"No technical SEO issues were found on the {pages} page(s) audited from {start or 'the supplied HTML'}."
    p = {k: sum(1 for g in groups if g["priority"] == k) for k in ("P1", "P2", "P3")}
    total = sum(g["count"] for g in groups)
    top = "; ".join(f"{g['title']} ({g['count']})" for g in groups[:3])
    basis = {"search_console": "first-party search clicks and impressions", "internal_links": "internal link share (no search data)"}.get(importance_source, "severity")
    return (f"The audit of {pages} page(s) from {start or 'the supplied HTML'} found {total} issue(s) across {len(groups)} rule(s): "
            f"{p['P1']} P1, {p['P2']} P2 and {p['P3']} P3 groups, prioritised by severity and {basis}. "
            f"Fix first: {top}.")


# ============================================================================ agent contract


class SeoInput(BaseModel):
    start_url: str | None = Field(default=None, description="Site or page URL to crawl (breadth-first, same host)")
    html: str | None = Field(default=None, description="Alternative to start_url: audit one page's HTML (page-level rules only)",
                             json_schema_extra={"ui": "textarea"})
    page_url: str | None = Field(default=None, description="URL the pasted HTML belongs to (resolves relative links)")
    max_pages: int = Field(default=50, ge=1, le=2000, description="Crawl budget (HTML and non-HTML URLs)")
    max_link_checks: int = Field(default=150, ge=0, le=5000, description="Extra fetches to verify link, canonical, hreflang and sitemap targets outside the crawl")
    use_traffic_data: bool = Field(default=True, description="Prioritise with first-party search data from the keyword_data connector")
    render_js: bool = Field(default=False, description="Re-evaluate crawled HTML pages after JavaScript rendering (Playwright), for client-rendered sites")
    max_render: int = Field(default=20, ge=1, le=200, description="Pages to render when render_js is on")
    rules: list[str] = Field(default_factory=list, description="Optional subset of rule ids or categories, e.g. ['canonical', 'title.duplicate']")

    @model_validator(mode="after")
    def _need_source(self) -> "SeoInput":
        if not (self.start_url or self.html):
            raise ValueError("Provide start_url (crawl) or html (single page).")
        return self


class SeoIssue(BaseModel):
    rule_id: str
    title: str
    category: str
    severity: Severity
    url: str
    detail: str
    evidence: str = ""
    priority: Priority
    priority_score: float
    importance: float = Field(description="0..1 page importance used for prioritisation")


class SeoIssueGroup(BaseModel):
    rule_id: str
    title: str
    category: str
    severity: Severity
    priority: Priority
    priority_score: float
    count: int
    urls: list[str]
    why_it_matters: str = ""
    recommendation: str = ""
    fix_steps: list[str] = Field(default_factory=list)
    effort: Literal["low", "medium", "high"] = "medium"
    owner_role: str = ""


class CrawledUrl(BaseModel):
    url: str
    status: int
    final_url: str | None = None
    redirect_hops: int = 0
    title: str | None = None
    in_sitemap: bool = False
    inlinks: int = 0
    issues: int = 0
    clicks: float | None = None
    impressions: float | None = None


class RobotsSummary(BaseModel):
    url: str | None = None
    found: bool = False
    status: int | None = None
    sitemaps: list[str] = Field(default_factory=list)
    blocked_user_agents: list[str] = Field(default_factory=list, description="User agents disallowed from the homepage")


class SitemapSummary(BaseModel):
    found: bool = False
    sources: list[str] = Field(default_factory=list)
    url_count: int = 0
    errors: list[str] = Field(default_factory=list)


class SeoOutput(BaseModel):
    start_url: str | None
    mode: Literal["crawl", "html"]
    pages_analysed: int
    urls_fetched: int
    crawl_truncated: bool = False
    rule_pack_version: str
    rules_evaluated: int
    rules_failed: int
    rules_skipped: int = 0
    issue_count: int
    priority_counts: dict[str, int]
    issues: list[SeoIssue]
    groups: list[SeoIssueGroup]
    fix_order: list[str]
    executive_summary: str
    explanation_source: Literal["model", "rule_pack"]
    importance_source: Literal["search_console", "internal_links", "none"]
    traffic_data_used: bool
    crawl: list[CrawledUrl]
    robots: RobotsSummary
    sitemap: SitemapSummary
    findings: list[Finding]


class IssueExplanation(BaseModel):
    rule_id: str = Field(description="Copy the rule id exactly as given")
    why_it_matters: str
    fix_steps: list[str] = Field(default_factory=list, max_length=5)
    effort: Literal["low", "medium", "high"] = "medium"
    owner_role: str = ""


class SeoExplanation(BaseModel):
    executive_summary: str
    explanations: list[IssueExplanation] = Field(default_factory=list)
    fix_order: list[str] = Field(default_factory=list, description="Rule ids in recommended fix order")


def reconcile_explanation(groups: list[dict[str, Any]], expl: SeoExplanation | None, use_model: bool) -> tuple[list[dict[str, Any]], list[str], bool]:
    """Merge model explanations onto the computed groups by rule_id; never add or drop findings."""
    known = [g["rule_id"] for g in groups]
    used = False
    if expl is not None and use_model:
        by_id = {e.rule_id: e for e in expl.explanations if e.rule_id in known}
        for g in groups:
            e = by_id.get(g["rule_id"])
            if e is None:
                continue
            used = True
            if e.why_it_matters.strip():
                g["why_it_matters"] = e.why_it_matters.strip()
            steps = [s.strip() for s in e.fix_steps if s and s.strip()]
            if steps:
                g["fix_steps"] = steps
            g["effort"] = e.effort
            if e.owner_role.strip():
                g["owner_role"] = e.owner_role.strip()
        order = [r for r in dict.fromkeys(expl.fix_order) if r in known]
    else:
        order = []
    order += [r for r in known if r not in order]
    return groups, order, used


# ============================================================================ HTML report

REPORT_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Technical SEO audit - {{ site }}</title>
<style>
:root{--ink:#1d2433;--muted:#5b6475;--line:#e3e6ec;--bg:#f6f7f9;--card:#fff;--p1:#b42318;--p2:#b54708;--p3:#475467;--ok:#067647;--accent:#1f4e8c}
*{box-sizing:border-box}body{margin:0;font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:var(--ink);background:var(--bg)}
header{background:var(--accent);color:#fff;padding:28px 32px}header h1{margin:0 0 4px;font-size:24px}header p{margin:0;opacity:.85}
main{max-width:1180px;margin:0 auto;padding:24px 20px 48px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:-44px 0 24px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}.tile b{display:block;font-size:26px}.tile span{color:var(--muted);font-size:13px}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:20px 22px;margin-bottom:18px}
h2{font-size:18px;margin:0 0 12px}table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.03em}
.pill{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;font-weight:600;color:#fff}.P1{background:var(--p1)}.P2{background:var(--p2)}.P3{background:var(--p3)}
.sev{font-size:12px;color:var(--muted)}code,.url{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12.5px;word-break:break-all}
details{border-top:1px solid var(--line);padding:10px 0}summary{cursor:pointer;font-weight:600}ul.steps{margin:6px 0 0 18px;padding:0}
.muted{color:var(--muted)}.ok{color:var(--ok)}.note{font-size:13px;color:var(--muted)}
</style></head><body>
<header><h1>Technical SEO audit</h1><p>{{ site }} &middot; {{ generated }} &middot; rule pack v{{ rule_pack_version }}</p></header>
<main>
<div class="tiles">
<div class="tile"><b>{{ pages_analysed }}</b><span>HTML pages analysed</span></div>
<div class="tile"><b>{{ urls_fetched }}</b><span>URLs fetched</span></div>
<div class="tile"><b>{{ issue_count }}</b><span>issues</span></div>
<div class="tile"><b style="color:var(--p1)">{{ p1 }}</b><span>P1 rule groups</span></div>
<div class="tile"><b style="color:var(--p2)">{{ p2 }}</b><span>P2 rule groups</span></div>
<div class="tile"><b class="ok">{{ rules_passed }}</b><span>rules passed</span></div>
</div>
<section><h2>Executive summary</h2><p>{{ executive_summary }}</p>
<p class="note">Detection: deterministic crawler + YAML rule pack. Prioritisation: severity x page importance from {{ importance_label }}. Explanations: {{ explanation_label }}.</p></section>
<section><h2>Prioritised fixes</h2>
<table><thead><tr><th>#</th><th>Priority</th><th>Issue</th><th>URLs</th><th>Effort</th><th>Owner</th></tr></thead><tbody>
{% for g in groups %}<tr><td>{{ loop.index }}</td><td><span class="pill {{ g.priority }}">{{ g.priority }}</span> <span class="sev">{{ g.severity }}</span></td>
<td><b>{{ g.title }}</b><br><span class="muted">{{ g.why_it_matters }}</span></td><td>{{ g.count }}</td><td>{{ g.effort }}</td><td>{{ g.owner_role }}</td></tr>
{% endfor %}</tbody></table></section>
<section><h2>Issue details</h2>
{% for g in groups %}<details{% if loop.index <= 3 %} open{% endif %}><summary><span class="pill {{ g.priority }}">{{ g.priority }}</span> {{ g.title }} <span class="muted">({{ g.count }}) &middot; <code>{{ g.rule_id }}</code></span></summary>
<p><b>How to fix</b></p><ul class="steps">{% for s in g.fix_steps %}<li>{{ s }}</li>{% endfor %}</ul>
<table><thead><tr><th>URL</th><th>Detail</th><th>Evidence</th></tr></thead><tbody>
{% for i in g.rows %}<tr><td class="url">{{ i.url }}</td><td>{{ i.detail }}</td><td class="muted">{{ i.evidence }}</td></tr>{% endfor %}
</tbody></table></details>
{% endfor %}{% if not groups %}<p class="ok">No issues found.</p>{% endif %}</section>
<section><h2>Crawl inventory</h2><table><thead><tr><th>URL</th><th>Status</th><th>Title</th><th>In sitemap</th><th>In-links</th><th>Clicks</th><th>Issues</th></tr></thead><tbody>
{% for c in crawl %}<tr><td class="url">{{ c.url }}{% if c.redirect_hops %}<br><span class="muted">&rarr; {{ c.final_url }}</span>{% endif %}</td><td>{{ c.status }}</td><td>{{ c.title or "" }}</td><td>{{ "yes" if c.in_sitemap else "" }}</td><td>{{ c.inlinks }}</td><td>{{ c.clicks_label }}</td><td>{{ c.issues }}</td></tr>{% endfor %}
</tbody></table></section>
<section><h2>Robots and sitemap</h2>
<p><b>robots.txt:</b> {{ "found" if robots.found else "not found" }} {% if robots.url %}<span class="url">({{ robots.url }})</span>{% endif %}{% if robots.sitemaps %}<br>Sitemap directives: {{ robots.sitemaps | join(", ") }}{% endif %}</p>
<p><b>Sitemap:</b> {{ sitemap.url_count }} URL(s) from {{ sitemap.sources | join(", ") or "none" }}{% if sitemap.errors %}<br><span class="muted">{{ sitemap.errors | join("; ") }}</span>{% endif %}</p></section>
<section><h2>Rules evaluated</h2><table><thead><tr><th>Rule</th><th>Category</th><th>Severity</th><th>Result</th></tr></thead><tbody>
{% for r in rule_results %}<tr><td><code>{{ r.id }}</code> {{ r.title }}</td><td>{{ r.category }}</td><td>{{ r.severity }}</td><td>{% if r.status == "skipped" %}<span class="muted">skipped - {{ r.note }}</span>{% elif r.issues %}{{ r.issues }} issue(s){% else %}<span class="ok">passed</span>{% endif %}</td></tr>{% endfor %}
</tbody></table></section>
</main></body></html>"""


def render_report(context: dict[str, Any]) -> str:
    import jinja2

    env = jinja2.Environment(autoescape=True, trim_blocks=True, lstrip_blocks=True)
    return env.from_string(REPORT_TEMPLATE).render(**context)


# ============================================================================ the agent


@register
class TechnicalSeoAgent(BaseAgent):
    spec = AgentSpec(
        id="technical_seo_recommendations",
        name="Technical SEO Recommendations",
        category=Category.WEB,
        summary="Crawls a site and runs an auditable YAML rule pack (status codes, redirects, canonicals, hreflang, sitemaps, robots, "
                "metadata, headings, structured data, links), prioritised by first-party search traffic; the model only explains the fixes.",
        kind=AgentKind.HYBRID,
        tier=Tier.BALANCED,
        connectors=["crawler", "keyword_data"],
        skills=["seo_rules"],
        keywords=["technical seo", "seo audit", "crawl", "canonical", "hreflang", "sitemap", "robots.txt", "redirects", "broken links", "meta description"],
        produces=["seo_issues.csv", "seo_audit.html"],
        feeds=["experiment_ideation", "geo_recommendations", "content_model_creation"],
    )
    Input = SeoInput
    Output = SeoOutput
    schemas = {"explain": SeoExplanation}

    def selftest_input(self) -> dict[str, Any]:
        return {"start_url": "https://www.meridian-mutual.example/", "max_pages": 40}

    def stages(self):
        return [("crawl", self.crawl), ("evaluate", self.evaluate), ("prioritise", self.prioritise),
                ("explain", self.explain), ("report", self.report)]

    # -- 1. crawl -------------------------------------------------------------------------------
    def crawl(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp = state["inputs"]
        warnings: list[str] = []
        degraded: list[str] = []
        if inp.get("html") and not inp.get("start_url"):
            url = norm_url(inp.get("page_url") or HTML_PLACEHOLDER_URL)
            facts = {url: extract_page_facts(url, inp["html"])}
            records = {url: {"url": url, "final_url": url, "status": 200, "first_status": 200, "redirects": [], "content_type": "text/html",
                             "elapsed_ms": 0.0, "bytes": facts[url]["html_bytes"], "error": None, "is_html": True}}
            site = build_site(inp.get("page_url"), records, facts, {"found": False, "url": None}, {"found": False, "urls": [], "errors": ["not fetched (pasted HTML)"]}, mode="html")
            warnings.append("Pasted HTML: only page-level rules ran; status, redirect, link, robots and sitemap rules need a crawl.")
            degraded.append("html input: site-level checks skipped")
            return {"data": {"site": site}, "warnings": warnings, "provenance": degraded_update(state, degraded)}

        crawler = ctx.connector("crawler")
        start = norm_url(inp["start_url"])
        root = f"{urlsplit(start).scheme}://{urlsplit(start).netloc}"
        pages = crawler.crawl(start, max_pages=int(inp.get("max_pages", 50)))
        records: dict[str, dict[str, Any]] = {}
        facts: dict[str, dict[str, Any]] = {}
        for p in pages:
            rec = response_record(p)
            records.setdefault(rec["url"], rec)
            if not rec["redirects"] and rec["status"] == 200 and p.html and p.is_html:
                facts.setdefault(rec["url"], extract_page_facts(rec["url"], p.html, p.headers))
        truncated = len(pages) >= int(inp.get("max_pages", 50))
        if not pages or all(r["status"] == 0 for r in records.values()):
            err = next((r["error"] for r in records.values() if r.get("error")), "no response")
            warnings.append(f"Could not fetch {start}: {err}")
            degraded.append("start URL could not be fetched")

        # robots.txt
        rb_page = crawler.fetch(f"{root}/robots.txt")
        rb_found = rb_page.status == 200 and bool(rb_page.html)
        rp = robots_parser(rb_page.html) if rb_found else None
        robots = {"url": f"{root}/robots.txt", "found": rb_found, "status": rb_page.status, "text": rb_page.html if rb_found else None,
                  "sitemaps": list(rp.site_maps() or []) if rp else []}

        # sitemaps (follow one level of sitemap index)
        sources = robots["sitemaps"] or [f"{root}/sitemap.xml"]
        sm_urls: list[str] = []
        sm_errors: list[str] = []
        fetched_sources: list[str] = []
        queue = list(dict.fromkeys(sources))[:10]
        seen_sm: set[str] = set()
        while queue and len(seen_sm) < 20:
            sm = queue.pop(0)
            if sm in seen_sm:
                continue
            seen_sm.add(sm)
            pg = crawler.fetch(sm)
            if pg.status != 200 or not pg.html:
                sm_errors.append(f"{sm} returned {pg.status or pg.error}")
                continue
            parsed = parse_sitemap(pg.html)
            if parsed["error"]:
                sm_errors.append(f"{sm}: {parsed['error']}")
                continue
            fetched_sources.append(sm)
            sm_urls += parsed["urls"]
            queue += parsed["sitemaps"]
        sitemap = {"found": bool(fetched_sources), "sources": fetched_sources, "urls": list(dict.fromkeys(sm_urls)), "errors": sm_errors}

        # verification fetches: link / canonical / hreflang / sitemap targets the crawl did not reach
        targets: list[str] = []
        for f in facts.values():
            targets += f["links_internal"]
            if f.get("canonical"):
                targets.append(f["canonical"])
            targets += [h["href"] for h in f.get("hreflang") or []]
        sitemap_norm = {norm_url(u) for u in sitemap["urls"]}
        targets += sorted(sitemap_norm)
        pending = [t for t in dict.fromkeys(targets) if t not in records and t.startswith(("http://", "https://"))]
        budget = int(inp.get("max_link_checks", 150))
        for t in pending[:budget]:
            p = crawler.fetch(t)
            rec = response_record(p)
            rec["url"] = t
            records[t] = rec
            # sitemap-only pages (never linked) are still part of the site: analyse them too
            if not rec["redirects"] and rec["status"] == 200 and p.html and p.is_html and host_of(t) == host_of(start) and t in sitemap_norm:
                facts.setdefault(t, extract_page_facts(t, p.html, p.headers))
        if len(pending) > budget:
            warnings.append(f"{len(pending) - budget} link/canonical/hreflang/sitemap target(s) were not verified (max_link_checks={budget}).")
        unverified = [u for u, r in records.items() if r["status"] == 0]
        if unverified:
            warnings.append(f"{len(unverified)} URL(s) could not be fetched (network/timeout) and were not judged: {', '.join(unverified[:5])}")
        if truncated:
            warnings.append(f"Crawl stopped at max_pages={inp.get('max_pages')}; raise it for a complete audit.")

        if inp.get("render_js"):
            if crawler.browser_available():
                rendered, failures = 0, []
                for u in list(facts)[: int(inp.get("max_render", 20))]:
                    try:
                        rp_page = crawler.render(u)
                        facts[u] = extract_page_facts(u, rp_page.html, {})
                        rendered += 1
                    except Exception as exc:  # keep the static facts for this page
                        failures.append(f"{u} ({type(exc).__name__})")
                        if not rendered:  # the browser itself cannot start: stop trying
                            break
                if failures and not rendered:
                    warnings.append(f"JS rendering unavailable ({failures[0]}); rules ran on the static HTML.")
                    degraded.append("browser unavailable: JavaScript rendering skipped")
                elif failures:
                    warnings.append(f"JS rendering failed for {len(failures)} page(s) (static HTML used): {', '.join(failures[:3])}")
                ctx.log("rendered pages", count=rendered)
            else:
                warnings.append("render_js requested but Playwright/Chromium is not available: rules ran on the static HTML.")
                degraded.append("browser unavailable: JavaScript rendering skipped")
        site = build_site(start, records, facts, robots, sitemap, mode="crawl", truncated=truncated)
        ctx.log("crawl complete", urls=len(records), pages=len(facts), sitemap_urls=len(sitemap["urls"]))
        return {"data": {"site": site}, "warnings": warnings,
                "provenance": degraded_update(state, degraded, evidence_count=len(records))}

    # -- 2. evaluate -----------------------------------------------------------------------------
    def evaluate(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        site = state["data"]["site"]
        pack = ctx.skill("seo_rules")
        rules = select_rules(pack.get("rules", []), state["inputs"].get("rules"))
        issues, results = evaluate_rules(site, rules)
        checks: list[Check] = []
        for r in results:
            rule = r["rule"]
            if r["status"] == "ran" and r["issues"] == 0:
                checks.append(Check(rule=rule["id"], status="pass", severity=rule.get("severity", "medium"),
                                    message=f"{rule['title']}: no issues ({len(site['pages'])} page(s) checked)."))
            elif r["status"] != "ran":
                checks.append(Check(rule=rule["id"], status="info", severity="low", message=f"{rule['title']}: {r['status']} - {r['note']}"))
        slim = [{"id": r["rule"]["id"], "title": r["rule"]["title"], "category": r["rule"].get("category", ""),
                 "severity": r["rule"].get("severity", "medium"), "status": r["status"], "issues": r["issues"], "note": r["note"]} for r in results]
        return {"data": {"issues": issues, "rule_results": slim, "rule_pack_version": str(pack.get("version", "")), "pack_cfg": {
            k: pack.get(k) for k in ("severity_weights", "traffic_weight", "breadth_bonus_per_doubling", "priority_thresholds") if pack.get(k) is not None},
            "rules_by_id": {r["id"]: r for r in rules}}, "checks": checks}

    # -- 3. prioritise ---------------------------------------------------------------------------
    def prioritise(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        data, inp = state["data"], state["inputs"]
        site = data["site"]
        warnings, degraded = [], []
        traffic: dict[str, dict[str, float]] = {}
        if inp.get("use_traffic_data", True) and site["mode"] == "crawl":
            try:
                kw = ctx.connector("keyword_data")
                if getattr(kw, "available", True):
                    df = kw.queries(site=site["start_url"], limit=100_000)
                    traffic = traffic_by_page(df.to_dict("records"), site["start_url"]) if len(df) else {}
                    traffic = {u: t for u, t in traffic.items() if host_of(u) == host_of(site["start_url"])}
                if not traffic:
                    degraded.append("no first-party search data for this site: importance from internal links")
            except Exception as exc:  # missing export, auth error ...
                degraded.append(f"keyword_data unavailable ({type(exc).__name__}): importance from internal links")
        importance, source = page_importance(site, traffic)
        if degraded:
            warnings.append("Priorities use internal in-link share as an estimate of page importance (no search traffic data).")
        site_scope = {rid for rid, r in data["rules_by_id"].items() if r.get("scope") == "site"}
        issues = prioritise(data["issues"], importance, data["pack_cfg"], site_scope)
        groups = group_issues(issues, data["rules_by_id"], data["pack_cfg"])
        return {"data": {"issues": issues, "groups": groups, "traffic": traffic, "importance_source": source},
                "warnings": warnings, "provenance": degraded_update(state, degraded)}

    # -- 4. explain (model) ----------------------------------------------------------------------
    def explain(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        data = state["data"]
        site, groups = data["site"], data["groups"]
        start = site.get("start_url")
        summary = deterministic_summary(start, len(site["pages"]), groups, data["importance_source"])
        expl = None
        if groups:
            prompt_groups = [{"rule_id": g["rule_id"], "title": g["title"], "severity": g["severity"], "priority": g["priority"],
                              "count": g["count"], "examples": [f"{short_url(u, start)}: {d}" for u, d in zip(g["urls"], g["examples"])],
                              "recommendation": g["recommendation"]} for g in groups[:25]]
            expl = ctx.generate("explain", SeoExplanation, site=start or "the supplied page", pages_crawled=len(site["records"]),
                                issue_count=sum(g["count"] for g in groups), group_count=len(groups), groups=prompt_groups,
                                importance_source={"search_console": "first-party search clicks and impressions",
                                                   "internal_links": "internal in-link share"}.get(data["importance_source"], "severity only"))
        use_model = not ctx.llm.offline
        groups, order, used = reconcile_explanation(groups, expl, use_model)
        if expl is not None and use_model and expl.executive_summary.strip():
            summary = expl.executive_summary.strip()
        return {"data": {"groups": groups, "fix_order": order, "executive_summary": summary,
                         "explanation_source": "model" if used else "rule_pack"}}

    # -- 5. report -------------------------------------------------------------------------------
    def report(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        from datetime import datetime, timezone

        data = state["data"]
        site, issues, groups = data["site"], data["issues"], data["groups"]
        start = site.get("start_url")
        traffic = data.get("traffic") or {}
        checks: list[Check] = []
        for i in sorted(issues, key=lambda x: -x["priority_score"]):
            checks.append(Check(rule=i["rule_id"], status="fail" if i["severity"] in ("critical", "high") else "warn", severity=i["severity"],
                                message=f"{i['title']}: {i['detail']}", target=i["url"],
                                data={"priority": i["priority"], "score": i["priority_score"]}))
        sitemap_set = {norm_url(u) for u in site["sitemap"].get("urls", [])}
        per_url = defaultdict(int)
        for i in issues:
            per_url[i["url"]] += 1
        crawl = []
        for u, r in site["records"].items():
            t = traffic.get(u)
            crawl.append(CrawledUrl(url=u, status=r["first_status"], final_url=r["final_url"] if r["redirects"] else None,
                                    redirect_hops=len(r["redirects"]), title=(site["pages"].get(u) or {}).get("title") or None,
                                    in_sitemap=u in sitemap_set, inlinks=len(site["inlinks"].get(u, [])), issues=per_url.get(u, 0),
                                    clicks=t["clicks"] if t else None, impressions=t["impressions"] if t else None))
        rb = site["robots"]
        rp = robots_parser(rb.get("text")) if rb.get("found") else None
        blocked = [ua for ua in ("*", "Googlebot", "Bingbot", "GPTBot", "CCBot", "ClaudeBot", "PerplexityBot")
                   if rp is not None and start and not rp.can_fetch(ua, start)]
        robots = RobotsSummary(url=rb.get("url"), found=bool(rb.get("found")), status=rb.get("status"), sitemaps=rb.get("sitemaps") or [],
                               blocked_user_agents=blocked)
        sitemap = SitemapSummary(found=bool(site["sitemap"].get("found")), sources=site["sitemap"].get("sources", []),
                                 url_count=len(site["sitemap"].get("urls", [])), errors=site["sitemap"].get("errors", []))
        findings = [Finding(title=f"{g['title']} ({g['count']} URL{'s' if g['count'] != 1 else ''})", detail=g["why_it_matters"],
                            severity=g["severity"], category="seo",
                            evidence="; ".join(f"{short_url(u, start)}: {d}" for u, d in zip(g["urls"][:3], g["examples"])),
                            recommendation=" ".join(g["fix_steps"][:3]) or g["recommendation"], target=g["urls"][0] if g["urls"] else start,
                            source_agent=self.spec.id) for g in groups]
        pcounts = {k: sum(1 for i in issues if i["priority"] == k) for k in ("P1", "P2", "P3")}
        out = SeoOutput(
            start_url=start, mode=site["mode"], pages_analysed=len(site["pages"]), urls_fetched=len(site["records"]),
            crawl_truncated=bool(site.get("truncated")), rule_pack_version=data["rule_pack_version"],
            rules_evaluated=sum(1 for r in data["rule_results"] if r["status"] == "ran"),
            rules_failed=sum(1 for r in data["rule_results"] if r["status"] == "ran" and r["issues"]),
            rules_skipped=sum(1 for r in data["rule_results"] if r["status"] != "ran"),
            issue_count=len(issues), priority_counts=pcounts,
            issues=[SeoIssue(**{k: i[k] for k in SeoIssue.model_fields}) for i in sorted(issues, key=lambda x: (-x["priority_score"], x["rule_id"], x["url"]))],
            groups=[SeoIssueGroup(**{k: g[k] for k in SeoIssueGroup.model_fields}) for g in groups],
            fix_order=data["fix_order"], executive_summary=data["executive_summary"], explanation_source=data["explanation_source"],
            importance_source=data["importance_source"], traffic_data_used=data["importance_source"] == "search_console",
            crawl=crawl, robots=robots, sitemap=sitemap, findings=findings)

        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["priority", "priority_score", "severity", "rule_id", "rule_title", "category", "url", "detail", "evidence",
                    "recommendation", "clicks", "impressions"])
        rules_by_id = data["rules_by_id"]
        for i in out.issues:
            t = traffic.get(i.url) or {}
            w.writerow([i.priority, i.priority_score, i.severity, i.rule_id, i.title, i.category, i.url, i.detail, i.evidence,
                        rules_by_id.get(i.rule_id, {}).get("recommendation", ""), t.get("clicks", ""), t.get("impressions", "")])
        by_rule = defaultdict(list)
        for i in out.issues:
            by_rule[i.rule_id].append({"url": short_url(i.url, start), "detail": i.detail, "evidence": i.evidence})
        html = render_report({
            "site": start or "pasted HTML", "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "rule_pack_version": data["rule_pack_version"], "pages_analysed": out.pages_analysed, "urls_fetched": out.urls_fetched,
            "issue_count": out.issue_count, "p1": sum(1 for g in groups if g["priority"] == "P1"),
            "p2": sum(1 for g in groups if g["priority"] == "P2"), "rules_passed": out.rules_evaluated - out.rules_failed,
            "executive_summary": out.executive_summary,
            "importance_label": {"search_console": "first-party search clicks and impressions", "internal_links": "internal in-link share (estimate)"}.get(out.importance_source, "severity only"),
            "explanation_label": "language model, grounded in the findings" if out.explanation_source == "model" else "rule pack text (no model)",
            "groups": [{**g, "rows": by_rule.get(g["rule_id"], [])} for g in groups],
            "crawl": [{**c.model_dump(), "url": short_url(c.url, start), "final_url": short_url(c.final_url, start) if c.final_url else None,
                       "clicks_label": "" if c.clicks is None else f"{c.clicks:,.0f}"} for c in crawl],
            "robots": robots.model_dump(), "sitemap": sitemap.model_dump(), "rule_results": data["rule_results"]})
        artifacts = [ctx.artifacts.write_text("seo_issues.csv", buf.getvalue(), "text/csv", "Every issue with priority, URL and fix"),
                     ctx.artifacts.write_text("seo_audit.html", html, "text/html", "Technical SEO audit report")]
        top = groups[0]["title"] if groups else "none"
        summary = (f"Audited {out.pages_analysed} page(s) / {out.urls_fetched} URL(s): {out.issue_count} issue(s) "
                   f"({pcounts['P1']} P1, {pcounts['P2']} P2, {pcounts['P3']} P3) across {out.rules_failed} rule(s); top: {top}")
        return {"output": out.model_dump(mode="json"), "artifacts": artifacts, "checks": checks, "summary": summary}
