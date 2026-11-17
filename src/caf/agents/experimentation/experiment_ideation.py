"""Experiment Ideation - evidence-grounded test ideas against an owned CRO heuristic library.

Evidence is computed in code from four independent sources: the page itself (crawler: CTAs,
forms, headings, controls), web analytics for the page, behaviour events (dead clicks, rage
clicks, scroll survival) and upstream findings from other agents (heatmap, accessibility, SEO ...).
Heuristics from config/skills/cro_heuristics.yaml fire deterministically on those signals; the
model only phrases ideas that cite heuristic ids and evidence ids; ICE and PIE scores are computed
in code; ideas are persisted to the store collection "experiment_backlog" (deduplicated by hash),
so the backlog and the learnings behind it compound across runs - independent of any vendor.
"""

from __future__ import annotations

import hashlib
import math
import re
import string
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import urlparse

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field, model_validator

from ...core import AgentKind, AgentSpec, BaseAgent, Category, Check, RunContext, Tier, register
from ...core.textutil import canonical, normalize_ws, tfidf_similarity, tokens, truncate

DEMO_URL = "https://www.meridian-mutual.example/products/home-insurance.html"

# ============================================================================ page facts (pure)

_IDENT = re.compile(r"^-?[A-Za-z_][A-Za-z0-9_-]*$")
CTA_VERBS = re.compile(r"^(get|start|see|buy|book|talk|call|compare|try|apply|join|sign|request|claim|find|contact|"
                       r"download|register|shop|switch|quote|check|speak|chat|subscribe)\b", re.I)
_INTERACTIVE_TAGS = {"a", "button", "input", "select", "textarea", "summary", "label", "option", "details"}
_CONTROL_HINT = re.compile(r"(tab|btn|button|toggle|clickable|accordion|chip|pill|select|option)", re.I)
_PRICE = re.compile(r"[$£€]\s?\d[\d,]*(?:\.\d{2})?")


def css_ident(value: str) -> str:
    """Escape a value for use as a CSS identifier (ids and classes)."""
    if _IDENT.match(value or ""):
        return value
    return "".join(ch if (ch.isalnum() or ch in "-_") else f"\\{ch}" for ch in value or "")


def _attr_value(value: str) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _select_count(soup: Any, selector: str) -> int:
    try:
        return len(soup.select(selector))
    except Exception:
        return 0


def unique_selector(el: Any, soup: Any) -> str:
    """A short CSS selector that matches exactly this element in `soup` (ids, classes, data attributes, then a path)."""
    tag = el.name
    if el.get("id") and _select_count(soup, f"#{css_ident(el['id'])}") == 1:
        return f"#{css_ident(el['id'])}"
    classes = [c for c in el.get("class", []) if _IDENT.match(c)]
    base = tag + "".join(f".{c}" for c in classes[:3])
    if classes and _select_count(soup, base) == 1:
        return base
    def by_attrs(names: list[str]) -> str | None:
        for attr in names:
            val = el.get(attr)
            if isinstance(val, list) or val is None or len(str(val)) > 80:
                continue
            sel = f"{base}[{attr}={_attr_value(val)}]"
            if _select_count(soup, sel) == 1:
                return sel
        return None

    found = by_attrs(sorted(a for a in el.attrs if a.startswith("data-")))
    if found:
        return found
    for anc in el.parents:
        if getattr(anc, "name", None) in (None, "[document]"):
            break
        if anc.get("id"):
            sel = f"#{css_ident(anc['id'])} {base}"
            if _select_count(soup, sel) == 1:
                return sel
            break
    found = by_attrs([a for a in ("name", "aria-label", "type", "href") if a in el.attrs])
    if found:
        return found
    parts: list[str] = []
    node = el
    while node is not None and getattr(node, "name", None) not in (None, "[document]", "html"):
        if node.get("id") and _select_count(soup, f"#{css_ident(node['id'])}") == 1:
            parts.append(f"#{css_ident(node['id'])}")
            break
        siblings = [s for s in node.parent.find_all(node.name, recursive=False)] if node.parent is not None else [node]
        idx = next((i for i, s in enumerate(siblings, 1) if s is node), 1)
        parts.append(f"{node.name}:nth-of-type({idx})")
        node = node.parent
    return " > ".join(reversed(parts)) or tag


def _text(el: Any, limit: int = 160) -> str:
    return truncate(normalize_ws(el.get_text(" ")), limit)


def _section_label(sec: Any) -> str:
    if sec.get("aria-label"):
        return sec["aria-label"]
    head = sec.find(["h1", "h2", "h3"])
    if head:
        return _text(head, 80)
    return " ".join(sec.get("class", [])) or sec.get("id") or sec.name


def extract_page_facts(html: str, url: str = "") -> dict[str, Any]:
    """Deterministic page inventory used by ideation, planning and variation development."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "lxml")
    for t in soup(["script", "style", "noscript", "template"]):
        if t.name != "script" or t.get("type") != "application/ld+json":
            t.decompose()
    title = _text(soup.title) if soup.title else ""
    meta = soup.find("meta", attrs={"name": "description"})
    main = soup.find("main") or soup.body or soup
    sections = [s for s in main.find_all(["section", "article"], recursive=True) if s.find_parent(["section", "article"]) is None]
    if not sections:
        sections = [c for c in main.find_all(recursive=False) if getattr(c, "name", None) not in (None, "script", "style")]
    sec_info = []
    hero_index = None
    for i, sec in enumerate(sections, 1):
        cls = " ".join(sec.get("class", []))
        label = _section_label(sec)
        sec_info.append({"index": i, "selector": unique_selector(sec, soup), "label": label, "classes": sec.get("class", [])})
        if hero_index is None and ("hero" in cls.lower() or "hero" in (sec.get("id") or "").lower()):
            hero_index = i
    hero_index = hero_index or (1 if sections else None)

    def section_of(el: Any) -> int:
        for i, sec in enumerate(sections, 1):
            if el is sec or sec in el.parents:
                return i
        return 0  # header / footer / outside main

    def in_chrome(el: Any) -> str | None:
        for anc in el.parents:
            if getattr(anc, "name", None) in ("nav", "footer"):
                return anc.name
            if getattr(anc, "name", None) == "header" and anc.find_parent("main") is None:
                return "header"
        return None

    # --- calls to action
    ctas: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    candidates = soup.select("a.btn, a[class*=cta], a[id*=cta], a[role=button], button, input[type=submit], input[type=button]")
    candidates += [a for a in soup.find_all("a", href=True) if CTA_VERBS.match(_text(a, 60)) and in_chrome(a) not in ("nav", "footer")]
    for el in candidates:
        if id(el) in seen_ids:
            continue
        seen_ids.add(id(el))
        chrome = in_chrome(el)
        if chrome in ("nav", "footer") and not (el.get("class") and any("cta" in c for c in el.get("class", []))):
            continue
        text = _text(el, 80) or el.get("value", "") or el.get("aria-label", "")
        if not text:
            continue
        idx = section_of(el)
        cls = el.get("class", [])
        ctas.append({"text": text, "selector": unique_selector(el, soup), "href": el.get("href"), "tag": el.name,
                     "classes": cls, "section_index": idx, "location": chrome or ("hero" if idx and idx == hero_index else f"section {idx}"),
                     "in_hero": bool(idx and idx == hero_index),
                     "primary": any("primary" in c for c in cls) or "hero-cta" in (el.get("id") or "")})
    if ctas and not any(c["primary"] for c in ctas):
        first_hero = next((c for c in ctas if c["in_hero"]), ctas[0])
        first_hero["primary"] = True
    counts: dict[str, int] = {}
    for c in ctas:
        counts[canonical(c["text"])] = counts.get(canonical(c["text"]), 0) + 1
    for c in ctas:
        c["duplicates"] = counts[canonical(c["text"])]

    # --- forms
    forms = []
    for f in soup.find_all("form"):
        fields = [i for i in f.find_all(["input", "select", "textarea"]) if (i.get("type") or "text").lower() not in ("hidden", "submit", "button", "reset", "image")]
        labelled = 0
        for i in fields:
            fid = i.get("id")
            if i.get("aria-label") or i.get("aria-labelledby") or i.find_parent("label") or (fid and soup.find("label", attrs={"for": fid})):
                labelled += 1
        forms.append({"selector": unique_selector(f, soup), "fields": len(fields), "required": sum(1 for i in fields if i.has_attr("required")),
                      "labelled": labelled, "section_index": section_of(f)})

    # --- images
    imgs = soup.find_all("img")
    missing_alt = [unique_selector(i, soup) for i in imgs if i.get("alt") is None]

    # --- non-semantic controls (look clickable, are not interactive, no role / tabindex)
    controls: dict[str, dict[str, Any]] = {}
    for el in soup.find_all(["div", "span", "li"]):
        cls = " ".join(el.get("class", []))
        data_attrs = [a for a in el.attrs if a.startswith("data-") and a not in ("data-selected",)]
        if not (_CONTROL_HINT.search(cls) or el.get("onclick")) or el.get("role") or el.get("tabindex") is not None:
            continue
        if el.find(list(_INTERACTIVE_TAGS)):
            continue
        if not (data_attrs or el.get("onclick") or re.search(r"(^|[-_ ])(tab|btn|button|toggle)([-_ ]|$)", cls)):
            continue
        classes = [c for c in el.get("class", []) if _IDENT.match(c)]
        key = el.name + "".join(f".{c}" for c in classes[:2])
        info = controls.setdefault(key, {"selector": key, "count": 0, "texts": [], "data_attrs": data_attrs})
        info["count"] += 1
        info["texts"].append(_text(el, 30))

    # --- trust signals
    quotes = soup.select("blockquote, .testimonial, .review, [class*=testimonial]")
    quotes = [q for q in quotes if not any(p in quotes for p in q.parents)]  # count containers once
    testimonial_section = section_of(quotes[0]) if quotes else None
    rating = None
    for el in soup.find_all(string=re.compile(r"(rated|out of 5|stars|reviews)", re.I)):
        rating = truncate(normalize_ws(str(el)), 120)
        break

    # --- pricing / plans / faq / offers
    tables = soup.find_all("table")
    pricing_table = next((t for t in tables if _PRICE.search(t.get_text(" "))), None)
    plans_section = next((s["index"] for s in sec_info if any(k in " ".join(s["classes"]).lower() for k in ("plan", "pricing"))), None)
    faq = soup.select_one("#faq, .faq, [class*=faq]")
    faq_items = soup.select("details, .faq-item")
    offers = []
    for s in sec_info:
        cls = " ".join(s["classes"]).lower()
        if any(k in cls for k in ("promo", "banner", "offer", "band")):
            sec_ctas = [c for c in ctas if c["section_index"] == s["index"]]
            offers.append({"selector": s["selector"], "label": s["label"], "section_index": s["index"],
                           "cta": sec_ctas[0]["text"] if sec_ctas else None})
    text_all = normalize_ws(main.get_text(" "))
    return {
        "url": url,
        "title": title,
        "meta_description": (meta.get("content") or "").strip() if meta else "",
        "h1": [_text(h, 140) for h in soup.find_all("h1")],
        "headings": [{"level": int(h.name[1]), "text": _text(h, 100)} for h in soup.find_all(["h2", "h3"])][:20],
        "sections": [{"index": s["index"], "selector": s["selector"], "label": s["label"]} for s in sec_info],
        "hero_index": hero_index,
        "ctas": ctas,
        "forms": forms,
        "images": {"count": len(imgs), "missing_alt": missing_alt},
        "non_semantic_controls": list(controls.values()),
        "testimonials": {"count": len(quotes), "section_index": testimonial_section},
        "rating_text": rating,
        "pricing": {"table_selector": unique_selector(pricing_table, soup) if pricing_table else None,
                    "has_headers": bool(pricing_table.find("th")) if pricing_table else None,
                    "section_index": section_of(pricing_table) if pricing_table else None,
                    "plans_section_index": plans_section,
                    "prices": sorted(set(_PRICE.findall(text_all)))[:8]},
        "faq": {"selector": unique_selector(faq, soup) if faq else None, "count": len(faq_items),
                "section_index": section_of(faq) if faq else None},
        "offers": offers,
        "n_sections": len(sec_info),
        "word_count": len(text_all.split()),
    }


def page_fact_lines(facts: dict[str, Any], max_ctas: int = 8) -> list[str]:
    """Human-readable page summary passed to prompts (templates cannot iterate dicts)."""
    lines = [f"Title: {facts.get('title') or '(none)'}", f"H1: {' | '.join(facts.get('h1') or []) or '(none)'}"]
    if facts.get("meta_description"):
        lines.append(f"Meta description: {facts['meta_description']}")
    lines.append(f"Sections ({facts.get('n_sections', 0)}): " + "; ".join(f"{s['index']}. {s['label']}" for s in facts.get("sections", [])[:12]))
    for c in facts.get("ctas", [])[:max_ctas]:
        lines.append(f"CTA '{c['text']}' at {c['selector']} ({c['location']}{', primary' if c['primary'] else ''}) -> {c.get('href') or '-'}")
    for f in facts.get("forms", []):
        lines.append(f"Form {f['selector']}: {f['fields']} fields, {f['required']} required, {f['labelled']} labelled")
    for c in facts.get("non_semantic_controls", []):
        lines.append(f"Non-semantic control {c['selector']} x{c['count']} ({', '.join(c['texts'][:4])})")
    if facts.get("images", {}).get("missing_alt"):
        lines.append(f"Images without alt: {', '.join(facts['images']['missing_alt'][:5])}")
    if facts.get("rating_text"):
        lines.append(f"Rating text: {facts['rating_text']}")
    t = facts.get("testimonials", {})
    if t.get("count"):
        lines.append(f"Testimonials: {t['count']} in section {t.get('section_index')}")
    p = facts.get("pricing", {})
    if p.get("table_selector"):
        lines.append(f"Pricing table {p['table_selector']} in section {p.get('section_index')} (header cells: {'yes' if p.get('has_headers') else 'no'})")
    if facts.get("faq", {}).get("selector"):
        lines.append(f"FAQ {facts['faq']['selector']} with {facts['faq']['count']} items in section {facts['faq'].get('section_index')}")
    for o in facts.get("offers", []):
        lines.append(f"Offer band {o['selector']} '{o['label']}' in section {o['section_index']} (CTA: {o.get('cta') or '-'})")
    return lines


# ============================================================================ evidence (pure)


def _bool_series(s: pd.Series) -> pd.Series:
    if s.dtype == bool:
        return s
    return s.astype(str).str.strip().str.lower().isin(["1", "true", "yes", "y", "t"])


def behaviour_summary(df: pd.DataFrame | None, *, rage_window_s: float = 1.5, rage_min_clicks: int = 3) -> dict[str, Any]:
    """Light dead-click / rage-click / scroll summary over normalised behaviour events."""
    if df is None or df.empty:
        return {"available": False}
    df = df.copy()
    et = df["event_type"].astype(str).str.lower()
    sessions = int(df["session_id"].nunique())
    click_mask = et.str.contains("click") | et.isin(["tap"])
    clicks = df[click_mask].copy()
    clicks["is_interactive"] = _bool_series(clicks["is_interactive"]) if "is_interactive" in clicks else False
    # rage clicks: explicit events, else every click in a burst of >= rage_min_clicks on one element within the window
    explicit_rage = et[click_mask].str.contains("rage").to_numpy()
    in_burst: set[Any] = set()
    if not explicit_rage.any():
        c2 = clicks.sort_values(["session_id", "selector", "ts"])
        for _, g in c2.groupby(["session_id", "selector"], sort=False):
            ts = pd.to_datetime(g["ts"], utc=True, format="mixed")
            secs = (ts - ts.min()).dt.total_seconds().to_numpy()
            idx = g.index.to_numpy()
            j = 0
            for i in range(len(secs)):
                while secs[i] - secs[j] > rage_window_s:
                    j += 1
                if i - j + 1 >= rage_min_clicks:
                    in_burst.update(idx[j:i + 1].tolist())
    rage_mask = explicit_rage | clicks.index.isin(list(in_burst))
    rage = clicks[rage_mask]
    # dead clicks: explicit events, else clicks on non-interactive elements that are not part of a rage burst
    explicit_dead = et[click_mask].str.contains("dead").to_numpy()
    dead = clicks[explicit_dead | (~clicks["is_interactive"].to_numpy() & ~rage_mask)]
    dead_all = clicks[explicit_dead | ~clicks["is_interactive"].to_numpy()]

    def targets(frame: pd.DataFrame, n: int = 5) -> list[dict[str, Any]]:
        if frame.empty:
            return []
        g = frame.groupby("selector", dropna=True).agg(count=("selector", "size"), sessions=("session_id", "nunique"),
                                                        text=("element_text", "first"), tag=("tag", "first"))
        g = g.sort_values("count", ascending=False).head(n)
        return [{"selector": str(sel), "count": int(r["count"]), "sessions": int(r["sessions"]),
                 "text": "" if pd.isna(r["text"]) else str(r["text"])[:60], "tag": "" if pd.isna(r["tag"]) else str(r["tag"]),
                 "share_of_clicks": round(int(r["count"]) / max(len(clicks), 1), 4)} for sel, r in g.iterrows()]

    scroll_rows = df[pd.to_numeric(df.get("scroll_pct"), errors="coerce").notna()] if "scroll_pct" in df else df.iloc[0:0]
    reach: dict[int, float] = {}
    false_bottom = None
    median_depth = None
    if not scroll_rows.empty:
        depth = pd.to_numeric(scroll_rows["scroll_pct"], errors="coerce").groupby(scroll_rows["session_id"]).max()
        depth = depth.reindex(df["session_id"].unique()).fillna(0.0)
        if depth.max() <= 1.0:  # fractions -> percent
            depth = depth * 100
        median_depth = float(depth.median())
        surv = lambda d: float((depth >= d - 1e-9).mean())
        for d in range(10, 101, 10):
            reach[d] = round(surv(d), 4)
        # steepest 10-point fall in scroll survival, located at 5-point resolution, plus the modal stop depth
        windows = [(d, surv(d) - surv(d + 10)) for d in range(0, 91, 5)]
        start, drop = max(windows, key=lambda w: (round(w[1], 6), -w[0]))
        if drop > 0:
            counts, edges = np.histogram(depth.clip(0, 99.999), bins=np.arange(0, 105, 5))
            k = int(np.argmax(counts[:-1])) if len(counts) > 1 else 0  # ignore visitors who reach the very end
            in_bin = depth[(depth >= edges[k]) & (depth < edges[k + 1])]
            false_bottom = {"from_pct": start, "to_pct": start + 10, "depth_pct": start + 5, "drop": round(drop, 4),
                            "reach_before": round(surv(start), 4), "reach_after": round(surv(start + 10), 4),
                            "stop_band": [int(edges[k]), int(edges[k + 1])], "stop_share": round(float(counts[k]) / max(len(depth), 1), 4),
                            "stop_depth": round(float(in_bin.median()), 1) if len(in_bin) else float(start + 5)}
    devices = {}
    if "device" in df:
        per_session = df.groupby("session_id")["device"].first().astype(str).str.lower()
        devices = {k: round(float(v), 4) for k, v in per_session.value_counts(normalize=True).items()}
    return {
        "available": True,
        "sessions": sessions,
        "events": int(len(df)),
        "clicks": int(len(clicks)),
        "dead_clicks": int(len(dead)),
        "dead_clicks_incl_rage": int(len(dead_all)),
        "dead_click_share": round(len(dead) / max(len(clicks), 1), 4),
        "rage_clicks": int(len(rage)),
        "rage_sessions": int(rage["session_id"].nunique()) if not rage.empty else 0,
        "top_dead_targets": targets(dead),
        "top_rage_targets": targets(rage),
        "median_scroll_depth": None if median_depth is None else round(median_depth, 1),
        "scroll_reach": {str(k): v for k, v in reach.items()},
        "false_bottom": false_bottom,
        "devices": devices,
    }


def page_analytics(pages: pd.DataFrame | None, page_path: str, cfg: dict[str, Any], days: int | None = None) -> dict[str, Any]:
    """Page metrics vs the site, plus the PIE importance derived from the page's share of sessions."""
    imp = cfg.get("scoring", {}).get("importance", {})
    if pages is None or pages.empty:
        return {"available": False, "importance": float(imp.get("unknown", 5)), "is_estimate": True}
    df = pages.copy()
    norm = lambda p: "/" + str(p).split("://")[-1].split("/", 1)[-1].split("?")[0].strip("/") if "://" in str(p) else "/" + str(p).split("?")[0].strip("/")
    df["_p"] = df["page_path"].map(norm)
    want = norm(page_path or "/")
    row = df[df["_p"] == want]
    if row.empty and want not in ("/", ""):
        row = df[df["_p"].str.endswith(want.rstrip("/").split("/")[-1])]
    sessions_total = float(df["sessions"].sum())
    site_cr = float(df["conversions"].sum()) / max(sessions_total, 1)
    site_er = float(df["engaged_sessions"].sum()) / max(sessions_total, 1) if "engaged_sessions" in df else None
    if row.empty:
        return {"available": False, "site_conversion_rate": round(site_cr, 5), "importance": float(imp.get("unknown", 5)), "is_estimate": True}
    r = row.iloc[0]
    sessions = float(r["sessions"])
    cr = float(r["conversions"]) / max(sessions, 1)
    er = float(r["engaged_sessions"]) / max(sessions, 1) if "engaged_sessions" in r else None
    max_sessions = float(df["sessions"].max())
    lo, hi = float(imp.get("min", 2)), float(imp.get("max", 10))
    importance = lo + (hi - lo) * math.sqrt(sessions / max(max_sessions, 1))
    rank = int((df["sessions"] > sessions).sum()) + 1
    return {
        "available": True, "is_estimate": False, "page_path": str(r["page_path"]), "page_title": str(r.get("page_title", "")),
        "sessions": int(sessions), "conversions": int(r["conversions"]), "conversion_rate": round(cr, 5),
        "engagement_rate": None if er is None else round(er, 4),
        "site_conversion_rate": round(site_cr, 5), "site_engagement_rate": None if site_er is None else round(site_er, 4),
        "share_of_sessions": round(sessions / max(sessions_total, 1), 4), "rank_by_sessions": rank, "pages_compared": int(len(df)),
        "daily_sessions": round(sessions / days, 1) if days else None, "days": days,
        "importance": round(min(hi, max(lo, importance)), 2),
    }


def _ev(source: str, signal: str, detail: str, severity: str = "medium", target: str | None = None, **params: Any) -> dict[str, Any]:
    return {"id": "", "source": source, "signal": signal, "detail": detail, "severity": severity, "target": target, "params": params}


_WORD_MAP = {"btn": "button", "cta": "CTA", "img": "image", "nav": "navigation", "faq": "FAQ", "q": "question"}
_TAG_NOUN = {"img": "image", "a": "link", "button": "button", "div": "element", "span": "element", "li": "item", "table": "table",
             "form": "form", "section": "section", "input": "field"}


def element_label(selector: str, text: str = "", tag: str = "", plural: bool = False) -> str:
    """Readable name for an element from its selector, e.g. img.hero-image -> 'hero image', .plan-tab + Premium -> 'Premium' plan tab."""
    last = re.split(r"\s|>", (selector or "").strip())[-1]
    m = re.match(r"^([a-z][a-z0-9]*)?", last)
    tagname = (m.group(1) if m and m.group(1) else tag or "").lower()
    words: list[str] = []
    for name in re.findall(r"[.#]([\w-]+)", last):
        for w in re.split(r"[-_]", name):
            w = _WORD_MAP.get(w.lower(), w.lower())
            if w and w not in words:
                words.append(w)
    noun = " ".join(words) or _TAG_NOUN.get(tagname, tagname or "element")
    if tagname == "img" and "image" not in noun:
        noun += " image"
    if plural and not noun.endswith("s"):
        noun += "s"
    t = truncate((text or "").strip(), 40)
    return f"'{t}' {noun}" if t else noun


def page_signals(facts: dict[str, Any], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    th = cfg.get("signals", {})
    generic = {canonical(t) for t in cfg.get("generic_cta_texts", [])}
    out: list[dict[str, Any]] = []
    ctas = facts.get("ctas", [])
    primary = next((c for c in ctas if c["primary"]), ctas[0] if ctas else None)
    if primary and canonical(primary["text"]) in generic:
        out.append(_ev("page", "generic_cta_text", f"Primary CTA {primary['selector']} uses the generic label '{primary['text']}'.",
                       "medium", primary["selector"], cta_text=primary["text"], cta_selector=primary["selector"]))
    if primary and primary.get("duplicates", 1) > 1:
        out.append(_ev("page", "duplicate_cta_text", f"The label '{primary['text']}' is used by {primary['duplicates']} different CTAs on the page.",
                       "low", primary["selector"], cta_text=primary["text"], cta_selector=primary["selector"]))
    hero_ctas = [c for c in ctas if c["in_hero"] or c["location"] == "header"]
    if len(hero_ctas) >= int(th.get("ctas_above_fold", 3)):
        out.append(_ev("page", "many_ctas_above_fold", f"{len(hero_ctas)} calls to action compete above the fold.", "medium",
                       None, cta_count=len(hero_ctas)))
    plans_idx = (facts.get("pricing") or {}).get("plans_section_index")
    for o in facts.get("offers", []):
        if o.get("cta") and plans_idx and o["section_index"] < plans_idx:
            out.append(_ev("page", "competing_offer_above_plans",
                           f"Offer band {o['selector']} ('{o['label']}', CTA '{o['cta']}') sits in section {o['section_index']}, before the plan comparison (section {plans_idx}).",
                           "medium", o["selector"], offer_label=f"'{truncate(o['label'], 60)}'", offer_selector=o["selector"]))
    for c in facts.get("non_semantic_controls", []):
        out.append(_ev("page", "non_semantic_controls",
                       f"{c['count']} non-semantic controls {c['selector']} ({', '.join(c['texts'][:3])}) have no role, tabindex or keyboard support.",
                       "high", c["selector"], control_selector=c["selector"], control_label=element_label(c["selector"], plural=c["count"] > 1)))
    missing = facts.get("images", {}).get("missing_alt", [])
    if missing:
        out.append(_ev("page", "images_missing_alt", f"{len(missing)} image(s) without an alt attribute: {', '.join(missing[:3])}.", "medium",
                       missing[0], image_selector=missing[0]))
    for f in facts.get("forms", []):
        if f["fields"] > int(th.get("long_form_fields", 6)):
            out.append(_ev("page", "long_form", f"Form {f['selector']} asks for {f['fields']} fields.", "medium", f["selector"],
                           form_selector=f["selector"], form_fields=f["fields"]))
    t = facts.get("testimonials", {})
    n_sec = max(int(facts.get("n_sections") or 1), 1)
    if not t.get("count") and not facts.get("rating_text"):
        out.append(_ev("page", "no_social_proof", "No testimonials or ratings on the page.", "medium", None))
    elif t.get("count") and t.get("section_index") and t["section_index"] >= max(3, math.ceil(n_sec * 0.5)):
        out.append(_ev("page", "testimonials_below_fold",
                       f"{t['count']} testimonials sit in section {t['section_index']} of {n_sec}, far from the primary CTA.", "low", None,
                       testimonial_section=t["section_index"]))
    p = facts.get("pricing", {})
    if p.get("table_selector") and p.get("has_headers") is False:
        out.append(_ev("page", "table_without_headers", f"Pricing table {p['table_selector']} has no header cells.", "medium",
                       p["table_selector"], table_selector=p["table_selector"]))
    fq = facts.get("faq", {})
    if fq.get("selector") and fq.get("section_index") and fq["section_index"] >= max(3, math.ceil(n_sec * 0.6)):
        out.append(_ev("page", "faq_below_fold", f"The FAQ ({fq['count']} questions) is in section {fq['section_index']} of {n_sec}.", "low",
                       fq["selector"]))
    return out


def analytics_signals(m: dict[str, Any], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    if not m.get("available"):
        return []
    th = cfg.get("signals", {})
    out = []
    params = {"page_cr": f"{m['conversion_rate']:.2%}", "site_cr": f"{m['site_conversion_rate']:.2%}",
              "page_share": f"{m['share_of_sessions']:.0%}",
              "page_engagement": f"{m['engagement_rate']:.0%}" if m.get("engagement_rate") is not None else "n/a"}
    below = m["conversion_rate"] < m["site_conversion_rate"] * float(th.get("below_avg_conversion", 0.9))
    if below:
        out.append(_ev("analytics", "below_avg_conversion",
                       f"Page conversion rate {m['conversion_rate']:.2%} vs site average {m['site_conversion_rate']:.2%} ({m['sessions']:,} sessions).",
                       "high", m.get("page_path"), **params))
    if m["share_of_sessions"] >= float(th.get("high_traffic_share", 0.1)) or m["rank_by_sessions"] <= 3:
        out.append(_ev("analytics", "high_traffic_page",
                       f"The page receives {m['share_of_sessions']:.1%} of site sessions (rank {m['rank_by_sessions']} of {m['pages_compared']}).",
                       "medium", m.get("page_path"), **params))
    er, ser = m.get("engagement_rate"), m.get("site_engagement_rate")
    if er is not None and ser:
        if er >= ser and below:
            out.append(_ev("analytics", "engaged_not_converting",
                           f"Engagement rate {er:.0%} is at or above the site's {ser:.0%}, but conversion is below average.", "medium",
                           m.get("page_path"), **params))
        if er < ser * float(th.get("low_engagement", 0.85)):
            out.append(_ev("analytics", "low_engagement", f"Engagement rate {er:.0%} vs site {ser:.0%}.", "medium", m.get("page_path"), **params))
    return out


SECTION_LAYOUT_JS = """() => {
  const H = Math.max(document.documentElement.scrollHeight, document.body ? document.body.scrollHeight : 0) || 1;
  const main = document.querySelector('main') || document.body;
  let secs = Array.from(main.querySelectorAll('section, article')).filter(s => !(s.parentElement && s.parentElement.closest('section, article')));
  if (!secs.length) { secs = Array.from(main.children); }
  return {height: H, viewport: window.innerHeight, sections: secs.map((s, i) => {
    const r = s.getBoundingClientRect(); const h = s.querySelector('h1, h2, h3');
    return {index: i + 1, top_pct: (r.top + window.scrollY) / H * 100, bottom_pct: (r.bottom + window.scrollY) / H * 100,
            label: s.getAttribute('aria-label') || (h ? h.textContent.trim().slice(0, 80) : (s.className || s.tagName))};
  })};
}"""


def section_ending_before(layout: dict[str, Any] | None, depth_pct: float, tolerance: float = 1.0) -> dict[str, Any] | None:
    """Last section fully visible when visitors stop at `depth_pct` (scroll_pct = bottom of the viewport): the false-bottom candidate."""
    ended = [s for s in (layout or {}).get("sections", []) if s["bottom_pct"] <= depth_pct + tolerance]
    return max(ended, key=lambda s: s["bottom_pct"]) if ended else None


def behaviour_signals(b: dict[str, Any], cfg: dict[str, Any], layout: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    if not b.get("available"):
        return []
    th = cfg.get("signals", {})
    out = []
    for t in b.get("top_dead_targets", [])[:2]:
        if t["count"] >= int(th.get("dead_click_min", 15)) or t["share_of_clicks"] >= float(th.get("dead_click_share", 0.05)):
            out.append(_ev("behaviour", "dead_clicks",
                           f"{t['count']} dead clicks on {t['selector']} from {t['sessions']} sessions ({t['share_of_clicks']:.0%} of all clicks) - the element is not interactive.",
                           "high", t["selector"], dead_selector=t["selector"], dead_clicks=t["count"], dead_share=f"{t['share_of_clicks']:.0%}",
                           dead_target_label=element_label(t["selector"], t.get("text", ""), t.get("tag", ""))))
    for t in b.get("top_rage_targets", [])[:2]:
        if t["count"] >= int(th.get("rage_click_min", 10)):
            out.append(_ev("behaviour", "rage_clicks", f"{t['count']} rage clicks on {t['selector']} ('{t.get('text') or t['tag']}') from {t['sessions']} sessions.",
                           "critical", t["selector"], rage_selector=t["selector"], rage_clicks=t["count"],
                           rage_target_label=element_label(t["selector"], t.get("text", ""), t.get("tag", ""))))
    fb = b.get("false_bottom")
    if fb and fb["drop"] >= float(th.get("false_bottom_drop", 0.2)) and fb["from_pct"] <= int(th.get("false_bottom_max_depth", 70)):
        where = ""
        stop = fb.get("stop_depth", fb["depth_pct"])
        sec = section_ending_before(layout, stop) if layout else None
        if sec:
            where = (f"; {fb.get('stop_share', 0):.0%} of visitors stop at {fb['stop_band'][0]}-{fb['stop_band'][1]}%, just after section {sec['index']} "
                     f"'{truncate(sec['label'], 60)}' ({sec['top_pct']:.0f}-{sec['bottom_pct']:.0f}% of the measured page) comes fully into view")
        out.append(_ev("behaviour", "false_bottom",
                       f"Scroll survival falls from {fb['reach_before']:.0%} to {fb['reach_after']:.0%} between {fb['from_pct']}% and {fb['to_pct']}% of the page "
                       f"(a {fb['drop']:.0%} drop){where} - a false bottom.",
                       "high", sec["selector"] if sec and sec.get("selector") else None,
                       false_bottom_depth=f"{fb['from_pct']}-{fb['to_pct']}", false_bottom_drop=f"{fb['drop']:.0%}",
                       false_bottom_hint=f"section {sec['index']} '{truncate(sec['label'], 50)}'" if sec else None))
    if b.get("median_scroll_depth") is not None and b["median_scroll_depth"] < float(th.get("low_scroll_median", 50)):
        out.append(_ev("behaviour", "low_scroll_depth", f"Median scroll depth is {b['median_scroll_depth']:.0f}% of the page.", "medium", None,
                       median_scroll=f"{b['median_scroll_depth']:.0f}%"))
    mob = b.get("devices", {}).get("mobile", 0.0)
    if mob >= float(th.get("mobile_heavy_share", 0.5)):
        out.append(_ev("behaviour", "mobile_heavy", f"{mob:.0%} of recorded sessions are on mobile.", "low", None, mobile_share=f"{mob:.0%}"))
    return out


_UPSTREAM_SIGNAL = {"accessibility": "upstream_accessibility", "seo": "upstream_seo", "geo": "upstream_seo", "performance": "upstream_performance",
                    "behaviour": "upstream_behaviour", "content": "upstream_content", "analytics": "upstream_analytics"}


def upstream_signal(f: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Map an upstream Finding to a specific signal (+ template params) so it supports the right heuristic."""
    text = f"{f.get('title', '')} {f.get('detail', '')}".lower()
    target = f.get("target") or ""
    css_target = target and not str(target).startswith(("http", "/"))
    if "rage" in text:
        return "upstream_rage_clicks", ({"rage_selector": target, "rage_target_label": element_label(target)} if css_target else {})
    if "dead click" in text or "dead-click" in text:
        return "upstream_dead_clicks", ({"dead_selector": target, "dead_target_label": element_label(target)} if css_target else {})
    if "ignored" in text and "false bottom" not in text:
        return "upstream_behaviour", {}
    if "false bottom" in text or "scroll depth" in text or "scroll reach" in text:
        m = re.search(r"(\d{1,3})\s*[-\u2013]\s*(\d{1,3})\s*%", text)
        return "upstream_scroll", ({"false_bottom_depth": f"{m.group(1)}-{m.group(2)}"} if m else {})
    return _UPSTREAM_SIGNAL.get(str(f.get("category") or "").lower(), "upstream_other"), {}


def upstream_signals(findings: list[dict[str, Any]], limit: int = 12) -> list[dict[str, Any]]:
    out = []
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    for f in sorted(findings, key=lambda f: order.get(f.get("severity", "medium"), 2))[:limit]:
        signal, params = upstream_signal(f)
        detail = f"[{f.get('source_agent') or 'upstream'}] {f.get('title', '')}" + (f": {truncate(f.get('detail', ''), 160)}" if f.get("detail") else "")
        out.append(_ev(f"upstream:{f.get('source_agent') or 'agent'}", signal, detail, f.get("severity", "medium"), f.get("target"),
                       upstream_title=f.get("title", ""), **params))
    return out


def page_findings(upstream: dict[str, Any], page: str | None) -> tuple[list[dict[str, Any]], int]:
    """Upstream findings for this page (outputs that name a different page are skipped). Returns (findings, skipped)."""
    found, skipped = [], 0
    for key, value in (upstream or {}).items():
        if not isinstance(value, dict):
            continue
        out = value.get("output", value)
        if not isinstance(out, dict):
            continue
        src_page = next((out.get(k) for k in ("page_url", "url", "final_url") if isinstance(out.get(k), str) and out.get(k)), None)
        same = src_page is None or page is None or page_key(src_page) == page_key(page)
        for f in out.get("findings", []) or []:
            if not isinstance(f, dict):
                continue
            if not same:
                skipped += 1
                continue
            found.append({**f, "source_agent": f.get("source_agent") or value.get("agent_id") or key})
    return found, skipped


def assign_evidence_ids(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for i, e in enumerate(items, 1):
        e["id"] = f"E{i}"
    return items


_SEV = {"critical": 3, "high": 2, "medium": 1, "low": 0}


def selector_tokens(selector: str | None) -> set[str]:
    """Ids, classes and attribute values of a CSS selector (used to relate evidence about the same element)."""
    if not selector:
        return set()
    toks = set(re.findall(r"[#.]([A-Za-z_][\w-]*)", selector))
    toks |= set(re.findall(r"=\s*\"?([\w-]+)\"?\s*\]", selector))
    return toks


def related_targets(a: str | None, b: str | None) -> bool:
    """True when two evidence targets refer to the same element/component (page-level targets relate to everything)."""
    page_level = lambda t: not t or str(t).startswith(("http", "/"))
    if page_level(a) or page_level(b):
        return True
    return a == b or bool(selector_tokens(a) & selector_tokens(b))


def match_heuristics(heuristics: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Heuristics fired by their `triggers` (any-match), with evidence about the same element plus related `supporting` signals.

    The strongest trigger item becomes the idea's primary evidence; items of the same signal about a different
    element are left out (they would be a different idea), so every cited evidence id is about the changed element.
    """
    by_signal: dict[str, list[dict[str, Any]]] = {}
    for e in evidence:
        by_signal.setdefault(e["signal"], []).append(e)
    out = []
    for h in heuristics:
        trig = [e for s in h.get("triggers", []) for e in by_signal.get(s, [])]
        if not trig:
            continue
        rank = {sig: i for i, sig in enumerate(h.get("triggers", []))}  # trigger order in the library = evidence priority
        trig.sort(key=lambda e: (rank.get(e["signal"], 99), -_SEV.get(e["severity"], 1), 0 if e["source"] in ("behaviour", "analytics") else 1,
                                 int(e["id"][1:] or 0)))
        primary = trig[0]
        ev = [primary]
        for e in trig[1:]:
            same_signal = e["signal"] == primary["signal"]
            if (same_signal and e.get("target") == primary.get("target")) or (not same_signal and related_targets(e.get("target"), primary.get("target"))):
                ev.append(e)
        for s in h.get("supporting", []):
            ev += [e for e in by_signal.get(s, []) if e not in ev and related_targets(e.get("target"), primary.get("target"))]
        strength = (len({e["source"].split(":")[0] for e in ev}) * 10 + max(_SEV.get(e["severity"], 1) for e in ev) * 3
                    + h.get("typical_impact", 5))
        out.append({"heuristic": h, "evidence": ev, "primary": primary, "strength": strength})
    return sorted(out, key=lambda m: (-m["strength"], m["heuristic"]["id"]))


# ============================================================================ scoring (pure)


def _clamp(v: float, lo: float = 1.0, hi: float = 10.0) -> float:
    return round(min(hi, max(lo, v)), 2)


def ice_pie_scores(heuristic: dict[str, Any], evidence: list[dict[str, Any]], metrics: dict[str, Any], scoring: dict[str, Any],
                   learning_effect: int = 0) -> tuple[dict[str, float], dict[str, float]]:
    """ICE (impact, confidence, ease) and PIE (potential, importance, ease) from evidence and page metrics - no model involved."""
    sev_rank = {"critical": 3, "high": 2, "medium": 1, "low": 0}
    top = max(evidence, key=lambda e: sev_rank.get(e.get("severity", "medium"), 1))["severity"] if evidence else "medium"
    imp_cfg = scoring.get("impact", {})
    impact = _clamp(float(heuristic.get("typical_impact", 5)) + float(imp_cfg.get("severity_bonus", {}).get(top, 0)))
    c = scoring.get("confidence", {})
    sources = {e["source"].split(":")[0] for e in evidence}
    conf = float(c.get("base", 3)) + float(c.get("per_source", 1.5)) * max(len(sources) - 1, 0)
    conf += float(c.get("per_item", 0.5)) * min(max(len(evidence) - 1, 0), int(c.get("max_items", 4)))
    if sources & {"behaviour", "analytics"}:
        conf += float(c.get("quantitative_bonus", 1))
    if learning_effect > 0:
        conf += float(c.get("learning_win_bonus", 1))
    elif learning_effect < 0:
        conf -= float(c.get("learning_loss_penalty", 1))
    confidence = _clamp(conf)
    ease = _clamp(float(heuristic.get("ease", 5)))
    p = scoring.get("potential", {})
    gap = 0.0
    if metrics.get("available") and metrics.get("site_conversion_rate"):
        gap = (metrics["site_conversion_rate"] - metrics["conversion_rate"]) / metrics["site_conversion_rate"]
        gap = max(-1.0, min(1.0, gap))
    potential = _clamp(float(p.get("base", 5)) + float(p.get("gap_weight", 5)) * gap + float(p.get("severity_bonus", {}).get(top, 0)))
    importance = _clamp(float(metrics.get("importance", scoring.get("importance", {}).get("unknown", 5))))

    def combine(a: float, b: float, cc: float, how: str) -> float:
        return round(a * b * cc / 100, 2) if how == "product" else round((a + b + cc) / 3, 2)

    ice = {"impact": impact, "confidence": confidence, "ease": ease, "score": combine(impact, confidence, ease, scoring.get("ice", "mean"))}
    pie = {"potential": potential, "importance": importance, "ease": ease, "score": combine(potential, importance, ease, scoring.get("pie", "mean"))}
    return ice, pie


# ============================================================================ templates, hashing, dedupe (pure)

DEFAULT_PARAMS = {
    "cta_text": "the current label", "cta_selector": "the primary CTA", "dead_selector": "the dead-clicked element",
    "dead_target_label": "element", "dead_clicks": "repeated", "dead_share": "a large share", "rage_selector": "the unresponsive control",
    "rage_target_label": "control", "rage_clicks": "repeated", "false_bottom_depth": "40", "false_bottom_drop": "a sharp fall",
    "false_bottom_hint": "the full-width band at that depth", "testimonial_section": "a late", "offer_label": "competing offer",
    "control_label": "controls", "control_selector": "the custom controls", "page_cr": "below average", "site_cr": "the site average",
    "page_share": "a large share", "page_engagement": "a healthy", "table_selector": "the pricing table", "form_selector": "the form",
    "form_fields": "many", "mobile_share": "most", "meta_hint": "title and description", "median_scroll": "low",
}


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return DEFAULT_PARAMS.get(key, key.replace("_", " "))


def fill(template: str, params: dict[str, Any]) -> str:
    try:
        return string.Formatter().vformat(template or "", (), _SafeDict({k: v for k, v in params.items() if v not in (None, "")}))
    except (ValueError, IndexError):
        return template or ""


def compose_hypothesis(if_part: str, then_part: str, because_part: str) -> str:
    strip = lambda s, w: re.sub(rf"^\s*{w}\s+", "", (s or "").strip().rstrip("."), flags=re.I)
    return f"If {strip(if_part, 'if')}, then {strip(then_part, 'then')}, because {strip(because_part, 'because')}."


def idea_hash(page_key: str, heuristic_ids: list[str], title: str) -> str:
    sig = " ".join(sorted(set(tokens(title)))[:14])
    raw = f"{page_key}|{','.join(sorted(set(heuristic_ids)))}|{sig}"
    return "idea-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def jaccard(a: str, b: str) -> float:
    ta, tb = set(tokens(a)), set(tokens(b))
    return len(ta & tb) / max(len(ta | tb), 1)


def find_duplicate(idea: dict[str, Any], existing: list[dict[str, Any]], threshold: float = 0.6) -> dict[str, Any] | None:
    """Backlog record that is the same idea (same hash, or same page + overlapping heuristics + similar title)."""
    for rec in existing:
        if rec.get("id") == idea["id"]:
            return rec
    for rec in existing:
        if rec.get("page") != idea["page"]:
            continue
        if set(rec.get("heuristic_ids", [])) & set(idea["heuristic_ids"]) and jaccard(rec.get("title", ""), idea["title"]) >= threshold:
            return rec
    return None


def page_key(url: str | None) -> str:
    if not url:
        return "/"
    p = urlparse(url)
    return ((p.hostname or "") + (p.path or "/")).lower()


def heuristic_idea(match: dict[str, Any], facts: dict[str, Any]) -> dict[str, Any]:
    """Deterministic idea from a triggered heuristic's template (offline path and top-up)."""
    h = match["heuristic"]
    params: dict[str, Any] = {}
    primary = next((c for c in facts.get("ctas", []) if c.get("primary")), None)
    if primary:
        params.update(cta_text=primary["text"], cta_selector=primary["selector"])
    if facts.get("meta_description"):
        params["meta_hint"] = f"'{truncate(facts['meta_description'], 70)}'"
    for e in match["evidence"]:
        for k, v in (e.get("params") or {}).items():
            params.setdefault(k, v)
    tpl = h.get("idea", {})
    return {
        "title": fill(tpl.get("title", h["name"]), params),
        "hypothesis": compose_hypothesis(fill(tpl.get("hypothesis_if", ""), params), fill(tpl.get("hypothesis_then", ""), params),
                                         fill(tpl.get("hypothesis_because", ""), params)),
        "change": fill(tpl.get("change", ""), params),
        "rationale": f"{h['name']}: {h['principle']}",
        "heuristic_ids": [h["id"]],
        "evidence_ids": [e["id"] for e in match["evidence"]],
        "primary_metric": h.get("primary_metric", "Conversion rate"),
        "target": next((e["target"] for e in match["evidence"] if e.get("target")), None),
        "origin": "heuristic",
    }


# ============================================================================ contract


class IdeationInput(BaseModel):
    url: str | None = Field(default=None, description="Page to generate test ideas for")
    html: str | None = Field(default=None, description="Page HTML (alternative to url when the page cannot be fetched)", json_schema_extra={"ui": "textarea"})
    goal: str | None = Field(default=None, description="Programme goal, e.g. 'more home insurance quote starts'")
    max_ideas: int = Field(default=6, ge=1, le=12, description="How many ranked ideas to return")
    rank_by: Literal["ice", "pie"] = Field(default="ice", description="Ranking model")
    persist: bool = Field(default=True, description="Save ideas to the experiment backlog (deduplicated)")
    measure_layout: bool = Field(default=True, description="Measure section positions with Playwright to map scroll evidence to sections")
    context: str | None = Field(default=None, description="Anything else the ideas should reflect", json_schema_extra={"ui": "textarea"})

    @model_validator(mode="after")
    def _need_page(self) -> "IdeationInput":
        if not (self.url or self.html):
            raise ValueError("Provide a page url or its html")
        return self


class EvidenceItem(BaseModel):
    id: str
    source: str = Field(description="page | analytics | behaviour | upstream:<agent>")
    signal: str
    detail: str
    severity: Literal["low", "medium", "high", "critical"] = "medium"
    target: str | None = None


class IceScore(BaseModel):
    impact: float
    confidence: float
    ease: float
    score: float


class PieScore(BaseModel):
    potential: float
    importance: float
    ease: float
    score: float


class ExperimentIdea(BaseModel):
    id: str
    rank: int
    title: str
    hypothesis: str
    change: str
    rationale: str
    evidence: list[EvidenceItem]
    heuristic_ids: list[str]
    primary_metric: str
    ice: IceScore
    pie: PieScore
    target: str | None = None
    origin: Literal["model", "heuristic"] = "heuristic"
    backlog_status: Literal["new", "duplicate", "not_saved"] = "not_saved"


class TriggeredHeuristic(BaseModel):
    id: str
    name: str
    signals: list[str]
    evidence_ids: list[str]


class IdeationOutput(BaseModel):
    page_url: str | None
    goal: str | None = None
    ideas: list[ExperimentIdea]
    evidence: list[EvidenceItem]
    heuristics_triggered: list[TriggeredHeuristic]
    backlog_size: int
    backlog_new: int = 0
    backlog_duplicates: int = 0
    rank_by: str = "ice"
    page_metrics: dict[str, Any] = Field(default_factory=dict)
    behaviour: dict[str, Any] = Field(default_factory=dict)
    sources_used: list[str] = Field(default_factory=list)


class IdeaDraft(BaseModel):
    title: str = Field(description="Short imperative test name (max ~12 words)")
    hypothesis: str = Field(description="If <change>, then <measurable outcome>, because <evidence-based reason>.")
    change: str = Field(description="Exactly what changes on the page (element, copy, layout)")
    rationale: str = Field(description="Why this should work, citing the evidence")
    heuristic_ids: list[str] = Field(min_length=1, description="Ids from the heuristic list")
    evidence_ids: list[str] = Field(min_length=1, description="Evidence ids (E1, E2 ...) that support the idea")
    primary_metric: str
    target_selector: str = Field(default="", description="CSS selector of the main element changed, if any")


class IdeasDraft(BaseModel):
    ideas: list[IdeaDraft] = Field(min_length=1, max_length=12)


def _generate_or_none(ctx: RunContext, template: str, schema: type[BaseModel], **variables: Any) -> tuple[Any, str | None]:
    """Model call with honest fallbacks: offline -> render only (deterministic path), failure -> None + reason."""
    if ctx.llm.offline:
        ctx.render(template, **variables)
        return None, "no language model configured: ideas phrased from the heuristic library templates"
    try:
        return ctx.generate(template, schema, **variables), None
    except Exception as exc:  # provider/validation failure - fall back to the deterministic path
        ctx.log("model call failed", template=template, error=repr(exc))
        return None, f"model call failed ({type(exc).__name__}); ideas phrased from the heuristic library templates"


@register
class ExperimentIdeationAgent(BaseAgent):
    spec = AgentSpec(
        id="experiment_ideation",
        name="Experiment Ideation",
        category=Category.EXPERIMENTATION,
        summary="Generates evidence-grounded A/B test ideas for a page from crawler, analytics, behaviour and upstream findings, "
                "scored with ICE and PIE in code and saved to a deduplicated experiment backlog.",
        kind=AgentKind.HYBRID,
        tier=Tier.BALANCED,
        connectors=["crawler", "analytics", "behaviour", "store"],
        skills=["cro_heuristics"],
        keywords=["experiment ideas", "test ideas", "cro", "conversion optimisation", "improve conversion", "conversion rate", "a/b test ideas", "hypotheses",
                  "experimentation backlog", "ice score", "pie score", "ideation"],
        produces=["ideas.md", "ideas.csv", "ideas.json"],
        feeds=["experiment_planning", "variation_development"],
    )
    Input = IdeationInput
    Output = IdeationOutput
    schemas = {"ideas": IdeasDraft}

    def selftest_input(self) -> dict[str, Any]:
        return {"url": DEMO_URL, "goal": "More home insurance quote starts", "max_ideas": 6}

    def stages(self):
        return [("inspect_page", self.inspect_page), ("gather_evidence", self.gather_evidence), ("match_heuristics", self.match),
                ("generate_ideas", self.generate_ideas), ("score_and_rank", self.score_and_rank), ("persist_backlog", self.persist_backlog),
                ("report", self.report)]

    # -- stage 1 ---------------------------------------------------------------------------------
    def inspect_page(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp = state["inputs"]
        warnings, reasons = [], []
        html, url = inp.get("html"), inp.get("url")
        if not html:
            page = ctx.connector("crawler").fetch(url)
            if page.ok and page.is_html:
                html, url = page.html, page.final_url
            else:
                warnings.append(f"Could not fetch {url} ({page.error or page.status}); ideas use analytics, behaviour and upstream evidence only.")
                reasons.append("page could not be fetched")
                html = ""
        facts = extract_page_facts(html, url or "")
        layout = None
        crawler = ctx.connector("crawler")
        if html and inp.get("measure_layout", True):
            if crawler.browser_available():
                try:
                    if inp.get("html"):
                        layout = crawler.with_page(lambda pg: pg.evaluate(SECTION_LAYOUT_JS), html=html, base_url=url if url and crawler.is_fixture(url) else None)
                    else:
                        layout = crawler.with_page(lambda pg: pg.evaluate(SECTION_LAYOUT_JS), url=url)
                    by_index = {s["index"]: s["selector"] for s in facts.get("sections", [])}
                    for s in layout.get("sections", []):
                        s["selector"] = by_index.get(s["index"])
                except Exception as exc:
                    warnings.append(f"Layout measurement failed ({type(exc).__name__}); scroll evidence is not mapped to sections.")
            else:
                warnings.append("Playwright is not available; scroll depth is not mapped to page sections.")
                reasons.append("no browser for layout measurement")
        return {"data": {"facts": facts, "url": url, "layout": layout}, "warnings": warnings,
                "provenance": {"degraded_reasons": reasons} if reasons else {}}

    # -- stage 2 ---------------------------------------------------------------------------------
    def gather_evidence(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        cfg = ctx.skill("cro_heuristics")
        data = state["data"]
        url = data.get("url")
        warnings: list[str] = []
        reasons = list(state.get("provenance", {}).get("degraded_reasons", []))
        sources: list[str] = ["page"] if data["facts"].get("ctas") or data["facts"].get("h1") else []
        # analytics
        metrics: dict[str, Any] = {"available": False}
        try:
            an = ctx.connector("analytics")
            bounds = an.date_bounds()
            if bounds:
                start, end = bounds
                pages = an.pages(start, end, limit=1000)
                metrics = page_analytics(pages, urlparse(url or "").path or "/", cfg, days=(end - start).days + 1)
                metrics["period"] = f"{start} to {end}"
            if not metrics.get("available"):
                warnings.append("No analytics rows for this page; PIE importance is an estimate.")
            else:
                sources.append("analytics")
        except Exception as exc:
            warnings.append(f"Analytics unavailable ({type(exc).__name__}); PIE importance is an estimate.")
            reasons.append("no analytics data")
        # behaviour
        behaviour: dict[str, Any] = {"available": False}
        try:
            events = ctx.connector("behaviour").events(page_url=url) if url else None
            behaviour = behaviour_summary(events)
            if behaviour.get("available"):
                sources.append("behaviour")
            else:
                warnings.append("No behaviour events recorded for this page.")
        except Exception as exc:
            warnings.append(f"Behaviour data unavailable ({type(exc).__name__}).")
            reasons.append("no behaviour data")
        upstream, skipped = page_findings(ctx.upstream, url)
        if upstream:
            sources.append("upstream")
        if skipped:
            warnings.append(f"Ignored {skipped} upstream finding(s) about a different page.")
        evidence = (page_signals(data["facts"], cfg) + analytics_signals(metrics, cfg) + behaviour_signals(behaviour, cfg, data.get("layout"))
                    + upstream_signals(upstream))
        evidence = assign_evidence_ids(evidence)
        prov: dict[str, Any] = {"evidence_count": len(evidence), "evidence_sources": sources}
        if reasons:
            prov["degraded_reasons"] = reasons
        return {"data": {"metrics": metrics, "behaviour": behaviour, "evidence": evidence, "sources": sources, "cfg": cfg},
                "warnings": warnings, "provenance": prov}

    # -- stage 3 ---------------------------------------------------------------------------------
    def match(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        data = state["data"]
        matches = match_heuristics(data["cfg"].get("heuristics", []), data["evidence"])
        checks = [Check(rule="ideation.evidence", status="pass" if data["evidence"] else "warn", severity="medium",
                        message=f"{len(data['evidence'])} evidence items from {', '.join(data['sources']) or 'no source'}.")]
        checks.append(Check(rule="ideation.heuristics", status="pass" if matches else "warn", severity="medium",
                            message=f"{len(matches)} heuristics triggered by the evidence."))
        # learnings repository: past results per heuristic (shipped -> +, abandoned -> -)
        effects: dict[str, int] = {}
        learnings: list[dict[str, Any]] = []
        backlog: list[dict[str, Any]] = []
        try:
            store = ctx.connector("store")
            learnings = store.query("learnings", limit=500)
            backlog = store.query("experiment_backlog", limit=100000)
        except Exception as exc:
            ctx.log("store unavailable", error=repr(exc))
        for rec in learnings:
            delta = {"ship": 1, "abandon": -1}.get(str(rec.get("decision", "")).lower(), 0)
            for hid in rec.get("heuristic_ids", []) or []:
                effects[hid] = effects.get(hid, 0) + delta
        return {"data": {"matches": matches, "learning_effects": effects, "learnings": learnings[:50], "backlog": backlog}, "checks": checks}

    # -- stage 4 ---------------------------------------------------------------------------------
    def generate_ideas(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        facts, evidence, matches = data["facts"], data["evidence"], data["matches"]
        want = int(inp.get("max_ideas", 6))
        pk = page_key(data.get("url"))
        known_h = {h["id"]: h for h in data["cfg"].get("heuristics", [])}
        ev_ids = {e["id"] for e in evidence}
        page_backlog = [r for r in data["backlog"] if r.get("page") == pk]
        page_learnings = [r for r in data["learnings"] if page_key(r.get("page_url")) == pk]
        variables = dict(
            page_url=data.get("url") or "(html input)", goal=inp.get("goal"), context=inp.get("context"), max_ideas=want,
            page_lines=page_fact_lines(facts),
            evidence_lines=[f"{e['id']} [{e['source']}/{e['signal']}, {e['severity']}] {e['detail']}" for e in evidence],
            heuristic_lines=[f"{m['heuristic']['id']} - {m['heuristic']['name']}: {m['heuristic']['principle']} "
                             f"(evidence: {', '.join(e['id'] for e in m['evidence'])}; usual metric: {m['heuristic'].get('primary_metric', '')})"
                             for m in matches],
            backlog_titles=[r.get("title", "") for r in page_backlog[:30]],
            learning_lines=[f"{r.get('experiment_id')}: {r.get('decision')} - {r.get('headline', '')}" for r in page_learnings[:10]],
        )
        warnings: list[str] = []
        reasons: list[str] = []
        ideas: list[dict[str, Any]] = []
        draft, reason = _generate_or_none(ctx, "ideas", IdeasDraft, **variables) if matches else (None, None)
        if reason:
            reasons.append(reason)
        if draft is not None:
            for d in draft.ideas:
                hids = [h for h in d.heuristic_ids if h in known_h]
                eids = [e for e in d.evidence_ids if e in ev_ids]
                if not hids and eids:  # map an uncited idea to the closest triggered heuristic
                    sims = tfidf_similarity(f"{d.title} {d.change}", [f"{m['heuristic']['name']} {m['heuristic']['principle']}" for m in matches])
                    if sims and max(sims) > 0.05:
                        hids = [matches[sims.index(max(sims))]["heuristic"]["id"]]
                if not hids or not eids:
                    warnings.append(f"Dropped model idea '{truncate(d.title, 60)}': it cited no known heuristic or evidence id.")
                    continue
                hyp = d.hypothesis.strip()
                if not re.match(r"(?is)^\s*if\b.+\bthen\b.+\bbecause\b", hyp):
                    hyp = compose_hypothesis(d.change, f"{d.primary_metric} will improve", d.rationale)
                ideas.append({"title": d.title.strip(), "hypothesis": hyp, "change": d.change.strip(), "rationale": d.rationale.strip(),
                              "heuristic_ids": hids, "evidence_ids": eids, "primary_metric": d.primary_metric.strip() or known_h[hids[0]].get("primary_metric", ""),
                              "target": d.target_selector or None, "origin": "model"})
        covered = {h for i in ideas for h in i["heuristic_ids"]}
        for m in matches:  # deterministic ideas: the whole set offline, a top-up otherwise
            if len(ideas) >= max(want, 1) * 2:
                break
            if m["heuristic"]["id"] not in covered:
                ideas.append(heuristic_idea(m, facts))
                covered.add(m["heuristic"]["id"])
        if not matches:
            warnings.append("No heuristic was triggered by the available evidence; add analytics/behaviour data or upstream audits.")
        out: dict[str, Any] = {"data": {"raw_ideas": ideas}, "warnings": warnings}
        if reasons:
            out["provenance"] = {"degraded_reasons": list(state.get("provenance", {}).get("degraded_reasons", [])) + reasons}
        return out

    # -- stage 5 ---------------------------------------------------------------------------------
    def score_and_rank(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        scoring = data["cfg"].get("scoring", {})
        known_h = {h["id"]: h for h in data["cfg"].get("heuristics", [])}
        ev_by_id = {e["id"]: e for e in data["evidence"]}
        pk = page_key(data.get("url"))
        scored: list[dict[str, Any]] = []
        seen: list[dict[str, Any]] = []
        for raw in data["raw_ideas"]:
            ev = [ev_by_id[e] for e in raw["evidence_ids"] if e in ev_by_id]
            h = known_h[raw["heuristic_ids"][0]]
            effect = sum(data["learning_effects"].get(hid, 0) for hid in raw["heuristic_ids"])
            ice, pie = ice_pie_scores(h, ev, data["metrics"], scoring, learning_effect=effect)
            idea = {**raw, "id": idea_hash(pk, raw["heuristic_ids"], raw["title"]), "page": pk, "ice": ice, "pie": pie,
                    "evidence": [{k: e[k] for k in ("id", "source", "signal", "detail", "severity", "target")} for e in ev]}
            if any(s["id"] == idea["id"] or (set(s["heuristic_ids"]) & set(idea["heuristic_ids"]) and jaccard(s["title"], idea["title"]) >= 0.6) for s in seen):
                continue  # near-duplicate inside this run
            seen.append(idea)
            scored.append(idea)
        key = "pie" if inp.get("rank_by") == "pie" else "ice"
        other = "ice" if key == "pie" else "pie"
        scored.sort(key=lambda i: (-i[key]["score"], -i[other]["score"], i["title"]))
        scored = scored[: int(inp.get("max_ideas", 6))]
        for rank, idea in enumerate(scored, 1):
            idea["rank"] = rank
        return {"data": {"ideas": scored}}

    # -- stage 6 ---------------------------------------------------------------------------------
    def persist_backlog(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        ideas = data["ideas"]
        new = dup = 0
        size = len(data["backlog"])
        if not inp.get("persist", True):
            return {"data": {"backlog_new": 0, "backlog_dup": 0, "backlog_size": size}}
        try:
            store = ctx.connector("store")
            existing = list(data["backlog"])
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for idea in ideas:
                match = find_duplicate(idea, existing)
                record = {k: idea[k] for k in ("id", "page", "title", "hypothesis", "change", "rationale", "heuristic_ids", "primary_metric", "ice", "pie", "origin", "target")}
                record.update(page_url=data.get("url"), evidence=[e["detail"] for e in idea["evidence"]], source_agent=self.spec.id, run_id=ctx.run_id)
                if match:
                    dup += 1
                    idea["backlog_status"] = "duplicate"
                    idea["id"] = match["id"]
                    merged = {**match, "last_seen": now, "seen_count": int(match.get("seen_count", 1)) + 1,
                              "ice": max(match.get("ice", idea["ice"]), idea["ice"], key=lambda s: s.get("score", 0)),
                              "pie": max(match.get("pie", idea["pie"]), idea["pie"], key=lambda s: s.get("score", 0))}
                    store.put("experiment_backlog", merged, key=match["id"])
                else:
                    new += 1
                    idea["backlog_status"] = "new"
                    record.update(status="new", first_seen=now, last_seen=now, seen_count=1)
                    store.put("experiment_backlog", record, key=idea["id"])
                    existing.append(record)
            size = len(store.query("experiment_backlog", limit=1_000_000))
        except Exception as exc:
            return {"data": {"backlog_new": 0, "backlog_dup": 0, "backlog_size": size},
                    "warnings": [f"Backlog store unavailable ({type(exc).__name__}); ideas were not saved."]}
        return {"data": {"ideas": ideas, "backlog_new": new, "backlog_dup": dup, "backlog_size": size}}

    # -- stage 7 ---------------------------------------------------------------------------------
    def report(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        ideas = data["ideas"]
        evidence = [{k: e[k] for k in ("id", "source", "signal", "detail", "severity", "target")} for e in data["evidence"]]
        triggered = [{"id": m["heuristic"]["id"], "name": m["heuristic"]["name"], "signals": sorted({e["signal"] for e in m["evidence"]}),
                      "evidence_ids": [e["id"] for e in m["evidence"]]} for m in data["matches"]]
        out = IdeationOutput(page_url=data.get("url"), goal=inp.get("goal"), ideas=[ExperimentIdea.model_validate(i) for i in ideas],
                             evidence=evidence, heuristics_triggered=triggered, backlog_size=data.get("backlog_size", 0),
                             backlog_new=data.get("backlog_new", 0), backlog_duplicates=data.get("backlog_dup", 0), rank_by=inp.get("rank_by", "ice"),
                             page_metrics={k: v for k, v in data["metrics"].items()}, behaviour=data["behaviour"], sources_used=data["sources"])
        md = ideas_markdown(out)
        rows = ["rank,id,title,primary_metric,ice,pie,heuristics,evidence"]
        for i in out.ideas:
            esc = lambda s: '"' + str(s).replace('"', '""') + '"'
            rows.append(",".join([str(i.rank), i.id, esc(i.title), esc(i.primary_metric), f"{i.ice.score}", f"{i.pie.score}",
                                  esc(" ".join(i.heuristic_ids)), esc(" ".join(e.id for e in i.evidence))]))
        artifacts = [ctx.artifacts.write_text("ideas.md", md, "text/markdown", "Ranked experiment ideas"),
                     ctx.artifacts.write_text("ideas.csv", "\n".join(rows) + "\n", "text/csv", "Ideas (CSV)"),
                     ctx.artifacts.write_json("ideas.json", out.model_dump(mode="json"), "Ideas with evidence and scores")]
        top = out.ideas[0].title if out.ideas else "none"
        summary = (f"{len(out.ideas)} ranked test ideas from {len(evidence)} evidence items ({', '.join(out.sources_used) or 'no data'}); "
                   f"top by {out.rank_by.upper()}: '{truncate(top, 70)}'; backlog {out.backlog_size} ({out.backlog_new} new)")
        return {"output": out.model_dump(mode="json"), "artifacts": artifacts, "summary": summary}


def ideas_markdown(out: IdeationOutput) -> str:
    lines = [f"# Experiment ideas for {out.page_url or 'the page'}", ""]
    if out.goal:
        lines += [f"**Goal:** {out.goal}", ""]
    lines += [f"Evidence sources: {', '.join(out.sources_used) or 'none'} - ranked by {out.rank_by.upper()} (scores computed in code).", "",
              "| # | Idea | ICE | PIE | Heuristics | Evidence |", "|---|---|---|---|---|---|"]
    for i in out.ideas:
        lines.append(f"| {i.rank} | {i.title} | {i.ice.score:.1f} | {i.pie.score:.1f} | {', '.join(i.heuristic_ids)} | {', '.join(e.id for e in i.evidence)} |")
    for i in out.ideas:
        lines += ["", f"## {i.rank}. {i.title}", f"**Hypothesis:** {i.hypothesis}", f"**Change:** {i.change}", f"**Rationale:** {i.rationale}",
                  f"**Primary metric:** {i.primary_metric}",
                  f"**ICE:** impact {i.ice.impact:g}, confidence {i.ice.confidence:g}, ease {i.ice.ease:g} = {i.ice.score:g} - "
                  f"**PIE:** potential {i.pie.potential:g}, importance {i.pie.importance:g}, ease {i.pie.ease:g} = {i.pie.score:g}",
                  "**Evidence:**"] + [f"- {e.id} ({e.source}): {e.detail}" for e in i.evidence]
    lines += ["", "## All evidence"] + [f"- {e.id} [{e.source}/{e.signal}, {e.severity}] {e.detail}" for e in out.evidence]
    return "\n".join(lines) + "\n"


__all__ = ["extract_page_facts", "page_fact_lines", "behaviour_summary", "page_analytics", "ice_pie_scores", "match_heuristics",
           "unique_selector", "css_ident", "idea_hash", "compose_hypothesis", "page_key"]
