"""Content Model Creation - page/design analysis -> platform-neutral content model -> CMS artefacts.

Reusable Coforge IP, deliberately not tied to one CMS:
  analyse    fetch the URLs with the crawler (or take pasted HTML) and detect components with DOM heuristics:
             semantic tags, repeated sibling structures, class/id vocabulary and JSON-LD types
             (hero, card grid, CTA banner, FAQ, testimonial, pricing table, article body, author bio, form,
             plus key facts and rich text)
  vision     optional design screenshot (image_path / image_base64) analysed with a vision model
  propose    a deterministic baseline model is always built from the detections; with a real model the
             model refines it into a platform-neutral intermediate model (types, neutral field types,
             validations, references, reusable components)
  normalise  deterministic: ids, reserved names, display fields, required page fields, references resolve,
             component-embedding cycles broken, enum values present
  emit       one small ModelEmitter class per target: AEM Content Fragment Models (Sites API JSON), Sitecore
             templates (SCS YAML), Contentful (CMA JSON + contentful-migration script), Contentstack (content
             type + global field JSON), Strapi (schema.json + component JSON), Drupal (node/paragraph/field
             config YAML) and GraphQL SDL - every target is an artifact
Offline (no model) the baseline model is emitted, so every emitter still produces real output.
"""

from __future__ import annotations

import base64
import io
import json
import mimetypes
import re
import uuid
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, ClassVar, Literal
from urllib.parse import urlsplit

import yaml
from bs4 import BeautifulSoup, Tag
from pydantic import BaseModel, Field, model_validator

from ...core import AgentKind, AgentSpec, BaseAgent, Category, Check, RunContext, Tier, register
from ...core.handoff import Finding

FieldType = Literal["short_text", "long_text", "rich_text", "integer", "decimal", "boolean", "date", "datetime", "media", "url",
                    "email", "enum", "reference", "component", "json"]
FIELD_TYPES = set(FieldType.__args__)  # type: ignore[attr-defined]
COMPONENT_TYPES = ["hero", "card_grid", "cta_banner", "faq", "testimonial", "pricing_table", "article_body", "author_bio", "form",
                   "key_facts", "rich_text"]
TARGETS = ["aem", "sitecore", "contentful", "contentstack", "strapi", "drupal", "graphql"]
RESERVED = {"id", "type", "sys", "uid", "_id", "created_at", "updated_at", "published_at", "created_by", "updated_by", "locale",
            "localizations", "document_id", "publish_details", "tags", "options", "status", "version", "url"}


def degraded_update(state: dict[str, Any], reasons: list[str], **extra: Any) -> dict[str, Any]:
    """Provenance update for one stage (the core's merge_provenance reducer accumulates degraded_reasons)."""
    out: dict[str, Any] = dict(extra)
    if reasons:
        out["degraded_reasons"] = list(dict.fromkeys(reasons))
    return out

# ============================================================================ naming helpers (pure)


def snake(text: str) -> str:
    t = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(text or ""))
    t = re.sub(r"[^A-Za-z0-9]+", "_", t).strip("_").lower()
    if not t:
        return "item"
    return t if t[0].isalpha() else f"f_{t}"


def camel(text: str) -> str:
    parts = snake(text).split("_")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


def pascal(text: str) -> str:
    return "".join(p.capitalize() for p in snake(text).split("_"))


def kebab(text: str) -> str:
    return snake(text).replace("_", "-")


def plural(word: str) -> str:
    if re.search(r"[^aeiou]y$", word):
        return word[:-1] + "ies"
    if re.search(r"(s|x|z|ch|sh)$", word):
        return word + "es"
    return word + "s"


def label_of(text: str) -> str:
    return snake(text).replace("_", " ").capitalize()


# ============================================================================ detection (pure)

CTA_RX = re.compile(r"(^|[\s_-])(btn|button|cta)([\s_-]|$)", re.I)
CURRENCY_RX = re.compile(r"[$€£]\s?\d")


def _cls(el: Tag) -> str:
    return (" ".join(el.get("class", [])) + " " + (el.get("id") or "")).lower()


def _signature(el: Tag) -> str:
    return el.name + "." + ".".join(sorted(el.get("class", [])))


def _text(el: Tag | None) -> str:
    return " ".join(el.get_text(" ").split()) if el is not None else ""


def _selector(el: Tag) -> str:
    if el.get("id"):
        return f"{el.name}#{el['id']}"
    cls = [c for c in el.get("class", []) if c not in ("wrap",)][:2]
    return el.name + "".join(f".{c}" for c in cls)


PROSE_TAGS = {"p", "span", "strong", "em", "b", "i", "br", "a", "h2", "h3", "h4", "h5", "h6", "small", "time", "img", "option"}


def repeated_group(el: Tag, min_items: int = 3) -> tuple[Tag, list[Tag]] | None:
    """Largest set of >= min_items siblings sharing a tag+class signature (cards, plans, quotes, list items).
    Prose siblings (paragraphs, inline elements, headings) are not repeated structures."""
    best: tuple[Tag, list[Tag]] | None = None
    for parent in [el] + el.find_all(True):
        if parent.name in ("nav", "footer", "header", "select", "table", "tr", "thead", "tbody"):
            continue
        kids = [c for c in parent.children if isinstance(c, Tag) and c.name not in ("script", "style") and c.name not in PROSE_TAGS]
        for sig, n in Counter(_signature(c) for c in kids).items():
            if n < min_items:
                continue
            items = [c for c in kids if _signature(c) == sig]
            if best is None or len(items) * (1 + len(items[0].find_all(True))) > len(best[1]) * (1 + len(best[1][0].find_all(True))):
                best = (parent, items)
    return best


def item_fields(item: Tag) -> dict[str, Any]:
    """Field candidates of one repeated item."""
    f: dict[str, Any] = {}
    img = item.find("img")
    if img is not None:
        f["image"] = img.get("src", "")
        f["image_alt"] = img.get("alt") or ""
    h = item.find(re.compile(r"^h[2-5]$"))
    if h is not None:
        f["title"] = _text(h)[:120]
    tag = item.find(class_=re.compile(r"(tag|eyebrow|label|badge|kicker)", re.I))
    if tag is not None and (h is None or tag is not h):
        f["eyebrow"] = _text(tag)[:60]
    strong = item.find(["strong", "b"])
    if strong is not None and h is None:
        f["value"] = _text(strong)[:40]
        f["label"] = _text(item).replace(_text(strong), "", 1).strip()[:120]
    p = item.find("p")
    if p is not None:
        f["text"] = _text(p)[:300]
    a = item.find("a", href=True)
    if a is not None:
        f["link"] = {"label": _text(a)[:60], "url": a["href"]}
    bq = item.find("blockquote")
    if bq is not None:
        f["quote"] = _text(bq)[:400]
        cap = item.find("figcaption")
        if cap is not None:
            f["author"] = _text(cap)[:120]
    return f


def _ctas(el: Tag) -> list[dict[str, str]]:
    out = []
    for a in el.find_all(["a", "button"]):
        ident = _cls(a)
        if CTA_RX.search(" ".join(a.get("class", []))) or "cta" in ident:
            styles = [m for m in re.findall(r"btn-([a-z]+)", " ".join(a.get("class", [])))]
            out.append({"label": _text(a)[:60], "url": a.get("href", ""), "style": styles[0] if styles else "text"})
    return out


def _form_fields(form: Tag, soup: BeautifulSoup) -> list[dict[str, Any]]:
    fields = []
    for el in form.find_all(["input", "select", "textarea"]):
        typ = el.name if el.name in ("select", "textarea") else (el.get("type") or "text").lower()
        if typ in ("hidden", "submit", "button", "reset", "image"):
            continue
        label = ""
        if el.get("id"):
            lab = soup.find("label", attrs={"for": el["id"]})
            label = _text(lab)
        label = label or el.get("aria-label") or el.get("placeholder") or ""
        if not label:
            prev = el.find_previous(["span", "label"])
            label = _text(prev) if prev is not None and form in prev.parents else ""
        fields.append({"name": el.get("name") or el.get("id") or typ, "label": label[:80], "input_type": typ,
                       "required": el.has_attr("required"), "options": [_text(o) for o in el.find_all("option")][:12],
                       "autocomplete": el.get("autocomplete", "")})
    return fields


def page_jsonld_types(soup: BeautifulSoup) -> list[str]:
    out = []
    for s in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        try:
            data = json.loads(s.string or s.get_text())
        except Exception:
            continue
        for item in (data if isinstance(data, list) else [data]):
            if isinstance(item, dict):
                t = item.get("@type")
                out += t if isinstance(t, list) else ([t] if t else [])
    return [str(t) for t in out]


def classify_block(el: Tag, index: int, soup: BeautifulSoup) -> list[dict[str, Any]]:
    """Component detections inside one top-level block of <main>."""
    found: list[dict[str, Any]] = []
    ident = _cls(el)
    sel = _selector(el)
    heading = el.find(re.compile(r"^h[1-3]$"))
    words = len(_text(el).split())

    def add(ctype: str, evidence: list[str], fields: dict[str, Any], confidence: float, items: int = 0) -> None:
        found.append({"type": ctype, "selector": sel, "evidence": evidence, "fields": fields, "confidence": round(confidence, 2), "items": items})

    if el.name == "article" or (el.find("article") is not None and el.find("article").find("h1") is not None):
        art = el if el.name == "article" else el.find("article")
        paras = art.find_all("p")
        if art.find("h1") is not None and len(paras) >= 4:
            times = [t.get("datetime") for t in art.find_all("time") if t.get("datetime")]
            byline = art.find(class_=re.compile(r"byline", re.I))
            add("article_body", ["<article> with H1", f"{len(paras)} paragraphs", f"{len(art.find_all('h2'))} H2 sections"] + (["<time datetime>"] if times else []),
                {"headline": _text(art.find("h1"))[:150], "byline": _text(byline)[:150], "dates": times[:2],
                 "images": len(art.find_all("img")), "words": len(_text(art).split())}, 0.95)
        bio = art.find(class_=re.compile(r"author-bio|about-author|author-box", re.I)) or next(
            (h.find_parent(["aside", "section", "div"]) for h in art.find_all(re.compile(r"^h[2-4]$")) if re.match(r"about the author", _text(h), re.I)), None)
        if bio is not None:
            add("author_bio", [f"{_selector(bio)}", "'About the author' block"],
                {"name": _text(bio.find("strong")) or "", "bio": _text(bio.find("p"))[:300], "has_photo": bio.find("img") is not None,
                 "profile_link": (bio.find("a", href=True) or {}).get("href", "") if bio.find("a", href=True) else ""}, 0.9)
        if found:
            return found

    forms = [el] if el.name == "form" else el.find_all("form")
    for form in forms:
        fields = _form_fields(form, soup)
        if fields:
            submit = form.find(["button", "input"], attrs={"type": re.compile(r"submit", re.I)}) or form.find("button")
            add("form", [f"<form> with {len(fields)} field(s)"], {"title": _text(heading)[:100], "fields": fields,
                                                                  "submit_label": _text(submit) or (submit.get("value", "") if submit else ""),
                                                                  "action": form.get("action", "")}, 0.95, len(fields))
    if found:  # a block built around a form is a form (its field wrappers are not cards, its submit is not a CTA)
        return found

    details = [d for d in el.find_all("details") if d.find("summary") is not None]
    if len(details) >= 2 or (re.search(r"\bfaq\b|frequently", ident + " " + _text(heading).lower()) and details):
        add("faq", [f"{len(details)} <details>/<summary> pairs"] + (["FAQ heading"] if heading is not None and re.search(r"faq|frequently", _text(heading), re.I) else []),
            {"heading": _text(heading)[:120], "questions": [_text(d.find("summary"))[:150] for d in details][:12]}, 0.95, len(details))
        return found

    quotes = el.find_all("blockquote")
    if quotes and (re.search(r"testimonial|review|quote|voices", ident) or len(quotes) >= 2):
        items = []
        for q in quotes:
            fig = q.find_parent("figure")
            items.append({"quote": _text(q)[:300], "author": _text(fig.find("figcaption")) if fig is not None else ""})
        add("testimonial", [f"{len(quotes)} <blockquote>" + (" + <figcaption>" if el.find("figcaption") else "")],
            {"heading": _text(heading)[:120], "items": items[:6]}, 0.9, len(quotes))
        return found

    table = el.find("table")
    panels = [p for p in el.find_all(class_=re.compile(r"plan-panel|pricing-card|plan-card|price-card", re.I))]
    if re.search(r"pricing|plans?\b|price", ident) or (table is not None and len(CURRENCY_RX.findall(_text(table))) >= 3) or len(panels) >= 2:
        plans = []
        for p in panels or []:
            price = p.find(class_=re.compile(r"price", re.I))
            plans.append({"name": (p.get("id") or "").replace("panel-", "") or _text(p.find(re.compile(r"^h[2-5]$"))),
                          "price": _text(price)[:40], "features": [_text(li)[:80] for li in p.find_all("li")][:12],
                          "cta": _ctas(p)[0] if _ctas(p) else {}})
        rows = [[_text(c)[:40] for c in tr.find_all(["td", "th"])] for tr in table.find_all("tr")][:12] if table is not None else []
        tabs = [_text(t) for t in el.find_all(class_=re.compile(r"(^|-)tab$|plan-tab", re.I))]
        add("pricing_table", (["pricing/plans class"] if re.search(r"pricing|plan", ident) else []) + ([f"{len(panels)} plan panels"] if panels else [])
            + ([f"table with {len(rows)} rows and currency values"] if rows else []) + ([f"{len(tabs)} plan tabs"] if tabs else []),
            {"heading": _text(heading)[:120], "plans": plans, "comparison_rows": rows, "tabs": tabs}, 0.9, len(panels) or len(rows))
        return found

    imgs = el.find_all("img")
    ctas = _ctas(el)
    is_hero = re.search(r"hero|banner|masthead|jumbotron", ident) or (index == 0 and el.find("h1") is not None)
    if is_hero and el.find("h1") is not None:
        img = imgs[0] if imgs else None
        lead = el.find("p", class_=re.compile(r"lead|intro|sub", re.I)) or el.find("p")
        add("hero", ["first block with H1" if index == 0 else "hero class"] + ([f"{len(ctas)} CTA(s)"] if ctas else []) + (["image"] if img is not None else []),
            {"heading": _text(el.find("h1"))[:150], "subheading": _text(lead)[:300], "image": (img.get("src", "") if img is not None else ""),
             "image_alt": (img.get("alt") if img is not None else None), "ctas": ctas[:3],
             "breadcrumb": el.find(attrs={"aria-label": re.compile("breadcrumb", re.I)}) is not None}, 0.9 if index == 0 else 0.8)
        return found

    grp = repeated_group(el)
    if grp is not None:
        parent, items = grp
        fields = [item_fields(i) for i in items]
        keys = Counter(k for f in fields for k in f)
        common = {k for k, n in keys.items() if n >= len(items) / 2}
        if {"value", "label"} <= common and "title" not in common:
            add("key_facts", [f"{len(items)} repeated {_signature(items[0])} items with a highlighted value"],
                {"heading": _text(heading)[:120], "items": fields[:8]}, 0.8, len(items))
            return found
        if common & {"title", "image", "link", "quote"}:
            add("card_grid", [f"{len(items)} repeated {_signature(items[0])} items", "fields: " + ", ".join(sorted(common))],
                {"heading": _text(heading)[:120], "items": fields[:8], "item_fields": sorted(common)}, 0.85, len(items))
            return found

    if (re.search(r"promo|cta|banner|band|newsletter|advisor|signup|offer", ident) or (heading is not None and words <= 90)) and (ctas or el.find("a", href=True)) and not imgs:
        link = ctas[0] if ctas else {"label": _text(el.find("a", href=True))[:60], "url": el.find("a", href=True)["href"], "style": "text"}
        add("cta_banner", (["promo/CTA class"] if re.search(r"promo|cta|banner|band|newsletter|advisor|offer", ident) else ["short block with heading + link"]),
            {"heading": _text(heading)[:120], "text": _text(el.find("p"))[:300], "cta": link}, 0.75)
        return found

    paras = el.find_all("p")
    if heading is not None and len(paras) >= 1 and words >= 25:
        add("rich_text", [f"heading + {len(paras)} paragraph(s)"], {"heading": _text(heading)[:120], "words": words}, 0.6)
    return found


def detect_components(url: str, html: str) -> dict[str, Any]:
    """Page type + component detections for one page (pure)."""
    soup = BeautifulSoup(html or "", "lxml")
    for t in soup(["script", "style", "noscript", "template"]):
        if t.name == "script" and "ld+json" in (t.get("type") or ""):
            continue
        t.decompose()
    ld = page_jsonld_types(soup)
    main = soup.find("main") or soup.body or soup
    blocks = []
    for c in main.children:  # unwrap layout divs that only group several blocks
        if not isinstance(c, Tag) or c.name in ("script", "style"):
            continue
        inner = [k for k in c.children if isinstance(k, Tag) and k.name in ("section", "article", "aside", "form", "figure")]
        blocks += inner if c.name == "div" and len(inner) >= 2 else [c]
    comps = []
    for i, b in enumerate(blocks):
        comps += classify_block(b, i, soup)
    types = {c["type"] for c in comps}
    path = urlsplit(url).path or "/"
    if "article_body" in types or {"Article", "BlogPosting", "NewsArticle"} & set(ld):
        ptype = "article_page"
    elif "Product" in ld or "pricing_table" in types:
        ptype = "product_page"
    elif path in ("", "/"):
        ptype = "home_page"
    else:
        ptype = "standard_page"
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    btn_styles = sorted({m for a in soup.find_all(["a", "button"]) for m in re.findall(r"btn-([a-z]+)", " ".join(a.get("class", [])))})
    return {"url": url, "path": path, "title": title, "page_type": ptype, "jsonld_types": sorted(set(ld)), "components": comps,
            "button_styles": btn_styles}


# ============================================================================ neutral model (pure)


def F(fid: str, label: str, ftype: str, *, required: bool = False, multiple: bool = False, localized: bool = True, help: str = "",
      **val: Any) -> dict[str, Any]:
    v = {"min_length": None, "max_length": None, "pattern": None, "min_value": None, "max_value": None, "enum_values": [],
         "allowed_types": [], "media_kinds": [], "unique": False, "min_items": None, "max_items": None}
    v.update({k: x for k, x in val.items() if k in v})
    return {"id": fid, "label": label, "type": ftype, "required": required, "multiple": multiple, "localized": localized, "help": help, "validations": v}


SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*(?:/[a-z0-9]+(?:-[a-z0-9]+)*)*$"
URL_PATTERN = r"^(https?://|/|#|mailto:|tel:)\S*$"


def blueprints(button_styles: list[str], input_types: list[str]) -> dict[str, dict[str, Any]]:
    """Deterministic content-type blueprints for every component the detector knows (and their building blocks)."""
    styles = sorted(set(["primary", "secondary", "outline", "text"]) | set(button_styles))
    inputs = sorted(set(["text", "email", "tel", "textarea", "select", "checkbox"]) | set(input_types))
    return {
        "cta_link": {"label": "Call to action", "kind": "component", "description": "A link styled as a button or text link.", "fields": [
            F("label", "Label", "short_text", required=True, max_length=40), F("link", "Link", "url", required=True, pattern=URL_PATTERN, localized=False),
            F("style", "Style", "enum", enum_values=styles, localized=False), F("open_in_new_tab", "Open in new tab", "boolean", localized=False),
            F("analytics_label", "Analytics label", "short_text", max_length=60, localized=False)]},
        "seo_metadata": {"label": "SEO metadata", "kind": "component", "mixin": True, "description": "Search and social metadata shared by every page.", "fields": [
            F("meta_title", "Meta title", "short_text", max_length=60, help="30-60 characters"), F("meta_description", "Meta description", "long_text", max_length=160, help="70-160 characters"),
            F("canonical_url", "Canonical URL", "url", pattern=URL_PATTERN, localized=False), F("social_image", "Social image", "media", media_kinds=["image"]),
            F("noindex", "Hide from search engines", "boolean", localized=False)]},
        "hero": {"label": "Hero", "kind": "component", "description": "Top-of-page introduction with headline, supporting copy, image and calls to action.", "fields": [
            F("heading", "Heading", "short_text", required=True, max_length=90), F("subheading", "Subheading", "long_text", max_length=300),
            F("image", "Image", "media", media_kinds=["image"]), F("image_alt", "Image alt text", "short_text", max_length=150, help="Required when the image is informative"),
            F("primary_cta", "Primary call to action", "component", allowed_types=["cta_link"]),
            F("secondary_cta", "Secondary call to action", "component", allowed_types=["cta_link"]), F("show_breadcrumb", "Show breadcrumb", "boolean", localized=False)]},
        "card": {"label": "Card", "kind": "component", "description": "One item in a card grid.", "fields": [
            F("title", "Title", "short_text", required=True, max_length=80), F("eyebrow", "Eyebrow / tag", "short_text", max_length=30),
            F("text", "Text", "long_text", max_length=300), F("image", "Image", "media", media_kinds=["image"]), F("image_alt", "Image alt text", "short_text", max_length=150),
            F("link", "Link", "component", allowed_types=["cta_link"])]},
        "card_grid": {"label": "Card grid", "kind": "component", "description": "A heading with a grid of repeated cards.", "fields": [
            F("heading", "Heading", "short_text", max_length=90), F("intro", "Intro", "long_text", max_length=300),
            F("cards", "Cards", "component", required=True, multiple=True, allowed_types=["card"], min_items=1, max_items=12),
            F("columns", "Columns", "integer", min_value=1, max_value=6, localized=False)]},
        "fact": {"label": "Fact", "kind": "component", "description": "A highlighted value with a short label.", "fields": [
            F("value", "Value", "short_text", required=True, max_length=20), F("label", "Label", "short_text", required=True, max_length=120)]},
        "key_facts": {"label": "Key facts", "kind": "component", "description": "A strip of highlighted numbers or proof points.", "fields": [
            F("heading", "Heading", "short_text", max_length=90), F("facts", "Facts", "component", required=True, multiple=True, allowed_types=["fact"], min_items=1, max_items=6)]},
        "cta_banner": {"label": "CTA banner", "kind": "component", "description": "A full-width band with a message and one call to action.", "fields": [
            F("heading", "Heading", "short_text", required=True, max_length=90), F("text", "Text", "long_text", max_length=300),
            F("cta", "Call to action", "component", required=True, allowed_types=["cta_link"]),
            F("theme", "Theme", "enum", enum_values=["light", "dark", "brand"], localized=False)]},
        "faq_item": {"label": "FAQ item", "kind": "entry", "description": "A reusable question and answer.", "fields": [
            F("question", "Question", "short_text", required=True, max_length=200), F("answer", "Answer", "rich_text", required=True),
            F("topic", "Topic", "short_text", max_length=60)]},
        "faq": {"label": "FAQ", "kind": "component", "description": "Question-and-answer section (renders FAQPage structured data).", "fields": [
            F("heading", "Heading", "short_text", max_length=90),
            F("items", "Questions", "reference", required=True, multiple=True, allowed_types=["faq_item"], min_items=1, max_items=20)]},
        "testimonial": {"label": "Testimonial", "kind": "entry", "description": "A customer quote with attribution.", "fields": [
            F("quote", "Quote", "long_text", required=True, max_length=600), F("author_name", "Author name", "short_text", required=True, max_length=80),
            F("author_detail", "Author detail", "short_text", max_length=120, help="e.g. member since 2019"),
            F("rating", "Rating", "decimal", min_value=0, max_value=5, localized=False), F("photo", "Photo", "media", media_kinds=["image"])]},
        "testimonial_list": {"label": "Testimonials", "kind": "component", "description": "A set of customer testimonials.", "fields": [
            F("heading", "Heading", "short_text", max_length=90),
            F("testimonials", "Testimonials", "reference", required=True, multiple=True, allowed_types=["testimonial"], min_items=1, max_items=9)]},
        "plan_feature": {"label": "Plan feature", "kind": "component", "description": "One line of a plan's cover.", "fields": [
            F("label", "Label", "short_text", required=True, max_length=80), F("value", "Value", "short_text", max_length=60),
            F("included", "Included", "boolean", localized=False)]},
        "pricing_plan": {"label": "Pricing plan", "kind": "entry", "description": "A plan or tier with price and features.", "fields": [
            F("name", "Name", "short_text", required=True, max_length=40), F("price", "Price", "decimal", required=True, min_value=0, localized=False),
            F("currency", "Currency", "enum", enum_values=["USD", "EUR", "GBP"], localized=False),
            F("billing_period", "Billing period", "enum", enum_values=["month", "year", "one_off"], localized=False),
            F("summary", "Summary", "long_text", max_length=200), F("features", "Features", "component", multiple=True, allowed_types=["plan_feature"], max_items=20),
            F("cta", "Call to action", "component", allowed_types=["cta_link"]), F("highlighted", "Highlighted", "boolean", localized=False)]},
        "comparison_row": {"label": "Comparison row", "kind": "component", "description": "One row of a comparison table.", "fields": [
            F("label", "Label", "short_text", required=True, max_length=80), F("values", "Values", "short_text", multiple=True, max_length=60)]},
        "pricing_table": {"label": "Pricing table", "kind": "component", "description": "Plans to choose from and an optional comparison table.", "fields": [
            F("heading", "Heading", "short_text", max_length=90), F("intro", "Intro", "long_text", max_length=300),
            F("plans", "Plans", "reference", required=True, multiple=True, allowed_types=["pricing_plan"], min_items=1, max_items=6),
            F("comparison_rows", "Comparison rows", "component", multiple=True, allowed_types=["comparison_row"]),
            F("footnote", "Footnote", "long_text", max_length=300)]},
        "author": {"label": "Author", "kind": "entry", "description": "A named expert who writes or reviews content (E-E-A-T).", "fields": [
            F("name", "Name", "short_text", required=True, max_length=80), F("job_title", "Job title", "short_text", max_length=80),
            F("bio", "Bio", "rich_text"), F("photo", "Photo", "media", media_kinds=["image"]),
            F("profile_url", "Profile URL", "url", pattern=URL_PATTERN, localized=False), F("same_as", "Social profiles", "url", multiple=True, localized=False)]},
        "form_field": {"label": "Form field", "kind": "component", "description": "One input of a form.", "fields": [
            F("name", "Name", "short_text", required=True, pattern=r"^[a-z][a-z0-9_]*$", localized=False), F("label", "Label", "short_text", required=True, max_length=80),
            F("input_type", "Input type", "enum", required=True, enum_values=inputs, localized=False), F("is_required", "Required", "boolean", localized=False),
            F("choices", "Choices", "short_text", multiple=True, max_length=80), F("autocomplete", "Autocomplete token", "short_text", localized=False),
            F("help_text", "Help text", "short_text", max_length=160)]},
        "form": {"label": "Form", "kind": "component", "description": "A configurable form (newsletter, contact, quote).", "fields": [
            F("title", "Title", "short_text", max_length=90), F("intro", "Intro", "long_text", max_length=300),
            F("fields", "Fields", "component", required=True, multiple=True, allowed_types=["form_field"], min_items=1),
            F("submit_label", "Submit label", "short_text", required=True, max_length=30), F("success_message", "Success message", "long_text", max_length=300),
            F("endpoint", "Submission endpoint", "url", pattern=URL_PATTERN, localized=False), F("consent_text", "Consent text", "long_text")]},
        "rich_text": {"label": "Rich text", "kind": "component", "description": "A heading with formatted body copy.", "fields": [
            F("heading", "Heading", "short_text", max_length=90), F("body", "Body", "rich_text", required=True)]},
    }


SECTION_TYPE = {"hero": "hero", "card_grid": "card_grid", "cta_banner": "cta_banner", "faq": "faq", "testimonial": "testimonial_list",
                "pricing_table": "pricing_table", "form": "form", "key_facts": "key_facts", "rich_text": "rich_text"}
DEPENDENCIES = {"hero": ["cta_link"], "card_grid": ["card", "cta_link"], "cta_banner": ["cta_link"], "faq": ["faq_item"],
                "testimonial_list": ["testimonial"], "pricing_table": ["pricing_plan", "plan_feature", "comparison_row", "cta_link"],
                "form": ["form_field"], "key_facts": ["fact"], "pricing_plan": ["plan_feature", "cta_link"], "card": ["cta_link"]}
PAGE_LABELS = {"home_page": "Home page", "product_page": "Product page", "article_page": "Article", "standard_page": "Standard page"}


def build_baseline(pages: list[dict[str, Any]], namespace: str) -> dict[str, Any]:
    """Deterministic intermediate model from the detections (always available, used offline as the final model)."""
    styles = sorted({s for p in pages for s in p.get("button_styles", [])})
    inputs = sorted({f["input_type"] for p in pages for c in p["components"] if c["type"] == "form" for f in c["fields"].get("fields", [])})
    bp = blueprints(styles, inputs)
    evidence: dict[str, list[str]] = defaultdict(list)
    needed: list[str] = []

    def need(tid: str) -> None:
        if tid not in needed:
            needed.append(tid)
            for dep in DEPENDENCIES.get(tid, []):
                need(dep)

    pages_by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for p in pages:
        pages_by_type[p["page_type"]].append(p)
        for c in p["components"]:
            tid = SECTION_TYPE.get(c["type"])
            if tid:
                need(tid)
                evidence[tid].append(f"{c['type']} on {p['path']} ({c['selector']}): " + "; ".join(c["evidence"][:3]))
            elif c["type"] == "author_bio":
                need("author")
                evidence["author"].append(f"author bio on {p['path']} ({c['selector']})")
            elif c["type"] == "article_body":
                evidence["article_page"].append(f"article body on {p['path']} ({c['selector']}): " + "; ".join(c["evidence"][:3]))
    types: list[dict[str, Any]] = []
    need("seo_metadata")
    for pt, group in sorted(pages_by_type.items()):
        comps = [c for p in group for c in p["components"]]
        has_hero = any(c["type"] == "hero" for c in comps)
        section_types = sorted({SECTION_TYPE[c["type"]] for c in comps if c["type"] in SECTION_TYPE and c["type"] != "hero"})
        fields = [F("title", "Title", "short_text", required=True, max_length=120),
                  F("slug", "Slug", "short_text", required=True, unique=True, pattern=SLUG_PATTERN, localized=False, help="URL path, e.g. products/home-insurance"),
                  F("seo", "SEO", "component", allowed_types=["seo_metadata"])]
        if pt == "article_page":
            need("author")
            fields += [F("summary", "Summary", "long_text", max_length=300, help="Shown in listings and as the meta description fallback"),
                       F("hero_image", "Hero image", "media", media_kinds=["image"]), F("hero_image_alt", "Hero image alt text", "short_text", max_length=150),
                       F("body", "Body", "rich_text", required=True), F("author", "Author", "reference", required=True, allowed_types=["author"]),
                       F("reviewed_by", "Reviewed by", "reference", allowed_types=["author"]),
                       F("published_date", "Published date", "date", required=True, localized=False), F("updated_date", "Updated date", "date", localized=False),
                       F("reading_time_minutes", "Reading time (minutes)", "integer", min_value=1, max_value=60, localized=False),
                       F("topics", "Topics", "short_text", multiple=True, max_length=40)]
        if has_hero:
            fields.append(F("hero", "Hero", "component", allowed_types=["hero"]))
        if section_types:
            fields.append(F("sections", "Sections", "component", multiple=True, allowed_types=section_types))
        types.append({"id": pt, "label": PAGE_LABELS.get(pt, label_of(pt)), "kind": "page", "mixin": False,
                      "description": f"{PAGE_LABELS.get(pt, pt)} template derived from {len(group)} analysed page(s): " + ", ".join(p["path"] for p in group[:5]),
                      "display_field": "title", "fields": fields,
                      "evidence": evidence.get(pt, []) + [f"page {p['path']} ({', '.join(sorted({c['type'] for c in p['components']})) or 'no components'})" for p in group]})
    for tid in needed:
        b = bp[tid]
        types.append({"id": tid, "label": b["label"], "kind": b["kind"], "mixin": bool(b.get("mixin")), "description": b["description"],
                      "display_field": None, "fields": [dict(f) for f in b["fields"]], "evidence": evidence.get(tid, [])[:8]})
    return {"namespace": snake(namespace), "version": "1.0.0", "types": types}


# ---------------------------------------------------------------------------- normalisation


def normalise_model(model: dict[str, Any], namespace: str) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Make any proposed model safe to emit: ids, reserved names, display fields, page fields, references, cycles."""
    issues: list[dict[str, str]] = []
    types: list[dict[str, Any]] = []
    id_map: dict[str, str] = {}
    for t in model.get("types", []):
        tid = snake(t.get("id") or t.get("label") or "type")
        base, n = tid, 2
        while tid in id_map.values():
            tid, n = f"{base}_{n}", n + 1
        id_map[t.get("id") or tid] = tid
        kind = t.get("kind") if t.get("kind") in ("page", "entry", "component") else "entry"
        types.append({**t, "id": tid, "kind": kind, "label": t.get("label") or label_of(tid), "mixin": bool(t.get("mixin")),
                      "description": t.get("description", ""), "evidence": list(t.get("evidence", []))})
    known = {t["id"]: t for t in types}
    for t in types:
        seen: set[str] = set()
        fields = []
        for f in t.get("fields", []):
            fid = snake(f.get("id") or f.get("label") or "field")
            if fid in RESERVED:
                new = f"{fid}_list" if f.get("multiple") else f"{fid}_value"
                if fid == "url":
                    new = "link"
                issues.append({"severity": "info", "type": t["id"], "field": fid, "message": f"Renamed reserved field '{fid}' to '{new}'."})
                fid = new
            base, n = fid, 2
            while fid in seen:
                fid, n = f"{base}_{n}", n + 1
            seen.add(fid)
            ftype = f.get("type") if f.get("type") in FIELD_TYPES else "short_text"
            if f.get("type") not in FIELD_TYPES:
                issues.append({"severity": "warn", "type": t["id"], "field": fid, "message": f"Unknown field type '{f.get('type')}' mapped to short_text."})
            v = {"min_length": None, "max_length": None, "pattern": None, "min_value": None, "max_value": None, "enum_values": [], "allowed_types": [],
                 "media_kinds": [], "unique": False, "min_items": None, "max_items": None, **(f.get("validations") or {})}
            v["allowed_types"] = [id_map.get(a, snake(a)) for a in v.get("allowed_types") or []]
            fields.append({"id": fid, "label": f.get("label") or label_of(fid), "type": ftype, "required": bool(f.get("required")),
                           "multiple": bool(f.get("multiple")), "localized": f.get("localized", True) is not False, "help": f.get("help", ""), "validations": v})
        t["fields"] = fields
    # references and components must resolve; kinds decide reference vs component
    for t in types:
        keep = []
        for f in t["fields"]:
            v = f["validations"]
            if f["type"] in ("reference", "component"):
                missing = [a for a in v["allowed_types"] if a not in known]
                if missing:
                    issues.append({"severity": "warn", "type": t["id"], "field": f["id"], "message": f"Dropped unknown target type(s) {missing}."})
                v["allowed_types"] = [a for a in v["allowed_types"] if a in known]
                if f["type"] == "component" and v["allowed_types"] and all(known[a]["kind"] != "component" for a in v["allowed_types"]):
                    f["type"] = "reference"
                    issues.append({"severity": "info", "type": t["id"], "field": f["id"], "message": "Component field pointing at entries converted to a reference."})
                elif f["type"] == "reference" and v["allowed_types"] and all(known[a]["kind"] == "component" for a in v["allowed_types"]):
                    f["type"] = "component"
                    issues.append({"severity": "info", "type": t["id"], "field": f["id"], "message": "Reference to component types converted to an embedded component."})
                if f["type"] == "component" and not v["allowed_types"]:
                    issues.append({"severity": "warn", "type": t["id"], "field": f["id"], "message": "Component field without a valid component type was removed."})
                    continue
            if f["type"] == "enum" and not v["enum_values"]:
                f["type"] = "short_text"
                issues.append({"severity": "warn", "type": t["id"], "field": f["id"], "message": "Enum without values converted to short text."})
            if f["type"] == "media" and not v["media_kinds"]:
                v["media_kinds"] = ["image"]
            if f["type"] == "short_text" and v["max_length"] is None and not v["pattern"]:
                v["max_length"] = 255
            keep.append(f)
        t["fields"] = keep
    # page essentials
    for t in types:
        if t["kind"] != "page":
            continue
        ids = [f["id"] for f in t["fields"]]
        if "title" not in ids:
            t["fields"].insert(0, F("title", "Title", "short_text", required=True, max_length=120))
            issues.append({"severity": "info", "type": t["id"], "field": "title", "message": "Added required title to page type."})
        if "slug" not in ids:
            t["fields"].insert(1, F("slug", "Slug", "short_text", required=True, unique=True, pattern=SLUG_PATTERN, localized=False))
            issues.append({"severity": "info", "type": t["id"], "field": "slug", "message": "Added required unique slug to page type."})
    # component embedding cycles -> break with a reference (and make the target an entry)
    graph = {t["id"]: [(f, a) for f in t["fields"] if f["type"] == "component" for a in f["validations"]["allowed_types"]] for t in types}
    state: dict[str, int] = {}

    def dfs(node: str) -> None:
        state[node] = 1
        for f, a in graph.get(node, []):
            if state.get(a) == 1:
                f["type"] = "reference"
                known[a]["kind"] = "entry"
                issues.append({"severity": "warn", "type": node, "field": f["id"], "message": f"Component cycle via '{a}' broken: field converted to a reference and '{a}' to an entry."})
            elif a not in state:
                dfs(a)
        state[node] = 2

    for t in types:
        if t["id"] not in state:
            dfs(t["id"])
    # display field (every type gets one so CMS lists are readable)
    for t in types:
        texts = [f["id"] for f in t["fields"] if f["type"] == "short_text" and not f["multiple"]]
        if t.get("display_field") not in texts:
            req = [f["id"] for f in t["fields"] if f["type"] == "short_text" and f["required"] and not f["multiple"]]
            t["display_field"] = (req or texts or [None])[0]
        if t["display_field"] is None:
            t["fields"].insert(0, F("internal_name", "Internal name", "short_text", required=True, max_length=120, localized=False,
                                    help="Editor-facing name used in CMS lists"))
            t["display_field"] = "internal_name"
            issues.append({"severity": "info", "type": t["id"], "field": "internal_name", "message": "Added an internal name as display field."})
        if len(t["fields"]) > 50:
            issues.append({"severity": "warn", "type": t["id"], "field": "", "message": f"{len(t['fields'])} fields exceeds the 50-field limit of some CMSs."})
        if not t["fields"]:
            issues.append({"severity": "error", "type": t["id"], "field": "", "message": "Type has no fields."})
    return {"namespace": snake(namespace or model.get("namespace") or "site"), "version": str(model.get("version") or "1.0.0"), "types": types}, issues


def merge_proposal(baseline: dict[str, Any], proposal: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Use the model's refinements, but never lose a type the detector proved is needed."""
    notes = []
    prop_types = {snake(t.get("id", "")): t for t in proposal.get("types", []) if t.get("id") and t.get("fields")}
    merged = []
    for t in baseline["types"]:
        p = prop_types.pop(t["id"], None)
        if p is None:
            merged.append(t)
            notes.append(f"kept baseline type {t['id']} (not in the model's proposal)")
            continue
        base_fields = {f["id"]: f for f in t["fields"]}
        fields = [f for f in p["fields"]]
        prop_ids = {snake(f.get("id", "")) for f in fields}
        for fid, f in base_fields.items():  # required baseline fields survive
            if f["required"] and fid not in prop_ids:
                fields.append(f)
        merged.append({**t, **{k: v for k, v in p.items() if k in ("label", "description", "kind", "mixin")}, "fields": fields,
                       "evidence": t.get("evidence", [])})
    for tid, p in prop_types.items():
        merged.append({**p, "evidence": ["proposed by the model from the page evidence"]})
        notes.append(f"added model-proposed type {tid}")
    return {**baseline, "types": merged}, notes


# ============================================================================ emitters


class ModelEmitter:
    """Turns the neutral model into files for one platform. Subclasses implement emit()."""

    target: ClassVar[str] = ""
    label: ClassVar[str] = ""

    def __init__(self, model: dict[str, Any], options: dict[str, Any] | None = None):
        self.model = model
        self.options = options or {}
        self.types = {t["id"]: t for t in model["types"]}
        self.notes: list[str] = []

    def emit(self) -> list[dict[str, str]]:
        raise NotImplementedError

    # helpers
    def targets(self, f: dict[str, Any]) -> list[str]:
        return [a for a in f["validations"]["allowed_types"] if a in self.types]

    def is_link(self, tid: str) -> bool:
        t = self.types.get(tid, {})
        ids = {f["id"] for f in t.get("fields", [])}
        return tid in ("cta_link", "link") or ({"label", "link"} <= ids and len(ids) <= 6 and t.get("kind") == "component")


class AemEmitter(ModelEmitter):
    """AEM as a Cloud Service Content Fragment Models - payload for POST /adobe/sites/cf/models (Sites API).
    AEM has no embedded components: component and reference fields become content-fragment references."""

    target, label = "aem", "AEM Content Fragment Models"
    TYPE = {"short_text": "text", "long_text": "long-text", "rich_text": "long-text", "integer": "number", "decimal": "float-number",
            "boolean": "boolean", "date": "date-time", "datetime": "date-time", "media": "content-reference", "url": "text", "email": "text",
            "enum": "enumeration", "reference": "content-fragment", "component": "content-fragment", "json": "json"}

    def emit(self) -> list[dict[str, str]]:
        ns = self.model["namespace"]
        conf = self.options.get("configuration_folder") or f"/conf/{kebab(ns)}"
        models = []
        for t in self.model["types"]:
            fields = []
            for f in t["fields"]:
                v = f["validations"]
                d: dict[str, Any] = {"type": self.TYPE[f["type"]], "name": camel(f["id"]), "label": f["label"], "required": f["required"],
                                     "multiple": f["multiple"], "description": f["help"]}
                if f["type"] == "short_text" and v["max_length"]:
                    d["maxLength"] = v["max_length"]
                if f["type"] in ("long_text", "rich_text"):
                    d["mimeType"] = "text/html" if f["type"] == "rich_text" else "text/plain"
                if f["type"] in ("integer", "decimal"):
                    d.update({k: v[k2] for k, k2 in (("min", "min_value"), ("max", "max_value")) if v[k2] is not None})
                if f["type"] == "date":
                    d["dateTimeType"] = "date"
                if f["type"] in ("url", "email") or v["pattern"]:
                    d["validationType"] = "custom"
                    d["customValidationRegex"] = v["pattern"] or (URL_PATTERN if f["type"] == "url" else r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
                    d["customErrorMessage"] = f"Enter a valid {f['label'].lower()}"
                if f["type"] == "enum":
                    d["values"] = [{"key": label_of(x), "value": x} for x in v["enum_values"]]
                    d["hint"] = "select"
                if f["type"] == "media":
                    d["allowedContentTypes"] = v["media_kinds"] or ["image"]
                    d["root"] = f"/content/dam/{kebab(ns)}"
                    d["showThumbnail"] = True
                if f["type"] in ("reference", "component"):
                    d["allowedModels"] = [f"{conf}/settings/dam/cfm/models/{kebab(a)}" for a in self.targets(f)]
                fields.append(d)
            models.append({"name": t["label"], "description": t["description"][:500], "status": "enabled", "fields": fields,
                           "x-caf-model-path": f"{conf}/settings/dam/cfm/models/{kebab(t['id'])}"})
        payload = {"configurationFolder": conf, "locked": False, "models": models}
        readme = (f"# AEM Content Fragment Models ({ns})\n\nPayload for the AEM Sites API: `POST /adobe/sites/cf/models?dryRun=true` validates it without "
                  "creating models; drop `dryRun` to create them under the configuration folder.\n\nNotes:\n"
                  "- AEM has no embedded components; component/reference fields are content-fragment references to the listed models.\n"
                  "- `allowedModels`, `dateTimeType` and the `boolean`/`date-time`/`json` type identifiers follow the Sites API field schema; run the "
                  "dry run against your AEM release and adjust if it reports an unknown property.\n"
                  "- `x-caf-model-path` is informational (the path the model is expected to get) and can be removed.\n")
        return [{"path": "aem/content-fragment-models.json", "content": json.dumps(payload, indent=2, ensure_ascii=False)},
                {"path": "aem/README.md", "content": readme}]


class SitecoreEmitter(ModelEmitter):
    """Sitecore templates as Sitecore Content Serialization (SCS) YAML items + module.json."""

    target, label = "sitecore", "Sitecore templates (SCS)"
    T_TEMPLATE = "ab86861a-6030-46c5-b394-e8f99e8b87db"
    T_SECTION = "e269fbb5-3750-427a-9149-7aa950b49301"
    T_FIELD = "455a3e98-a627-4b40-8035-e683a0331ac7"
    T_FOLDER = "0437fee2-44c9-46a6-abe9-28858d9fee8c"
    F_BASE = "12c33f3f-86c5-43a5-aeb4-5598cec45116"
    F_TYPE = "ab162cc0-dc80-4abf-8871-998ee5d7ba32"
    F_SOURCE = "1eb8ae32-e190-44a6-968d-ed904c794ebf"
    F_SHARED = "be351a73-fcb0-4213-93fa-c302d8ab4f51"
    F_SORT = "ba3f86a2-4a1c-4d78-b63d-91c2779c1b5e"
    F_TITLE = "19a69332-a23e-4e70-8d16-b2640cb24cc8"
    STANDARD_TEMPLATE = "{1930BBEB-7805-471A-A3BE-4858AC7CF696}"
    PROJECT_FOLDER = "825b30b4-b40b-422e-9920-23a1b6bda89c"  # /sitecore/templates/Project on a default install
    TYPE = {"short_text": "Single-Line Text", "long_text": "Multi-Line Text", "rich_text": "Rich Text", "integer": "Integer", "decimal": "Number",
            "boolean": "Checkbox", "date": "Date", "datetime": "Datetime", "url": "General Link", "email": "Single-Line Text", "enum": "Droplist",
            "json": "Multi-Line Text"}

    def gid(self, path: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"caf-sitecore:{path.lower()}"))

    @staticmethod
    def item(iid: str, parent: str, template: str, path: str, shared: list[tuple[str, str, str]] | None = None,
             title: str | None = None) -> str:
        lines = ["---", f'ID: "{iid}"', f'Parent: "{parent}"', f'Template: "{template}"', f"Path: {path}"]
        if shared:
            lines.append("SharedFields:")
            for fid, hint, value in shared:
                lines += [f'- ID: "{fid}"', f"  Hint: {hint}", f"  Value: {json.dumps(value) if any(c in str(value) for c in ':{}[]#@') else value}"]
        if title:
            lines += ["Languages:", "- Language: en", "  Versions:", "  - Version: 1", "    Fields:",
                      f'    - ID: "{SitecoreEmitter.F_TITLE}"', "      Hint: Title", f"      Value: {json.dumps(title)}"]
        return "\n".join(lines) + "\n"

    def emit(self) -> list[dict[str, str]]:
        ns = pascal(self.model["namespace"])
        root_path = self.options.get("path") or f"/sitecore/templates/Project/{ns}"
        parent_id = self.options.get("parent_id") or self.PROJECT_FOLDER
        root_id = self.gid(root_path)
        files = [{"path": f"sitecore/items/templates/{ns}.yml", "content": self.item(root_id, parent_id, self.T_FOLDER, root_path)}]
        folders = {"page": "Pages", "entry": "Content Types", "component": "Components"}
        for folder in folders.values():
            fp = f"{root_path}/{folder}"
            files.append({"path": f"sitecore/items/templates/{ns}/{folder}.yml", "content": self.item(self.gid(fp), root_id, self.T_FOLDER, fp)})
        tpath = {t["id"]: f"{root_path}/{folders[t['kind']]}/{pascal(t['id'])}" for t in self.model["types"]}
        composition: list[str] = []
        for t in self.model["types"]:
            p = tpath[t["id"]]
            bases = [self.STANDARD_TEMPLATE]
            fields_out = []
            for f in t["fields"]:
                if f["type"] == "component":
                    tg = self.targets(f)
                    if len(tg) == 1 and not f["multiple"] and self.is_link(tg[0]):
                        fields_out.append((f, "General Link", ""))
                    elif len(tg) == 1 and self.types[tg[0]].get("mixin"):
                        bases.append("{" + self.gid(tpath[tg[0]]).upper() + "}")
                    elif t["kind"] == "page":
                        composition.append(f"- {t['label']}.{f['label']}: placeholder '{snake(f['id'])}' allowing renderings for "
                                           + ", ".join(self.types[a]["label"] for a in tg) + " (datasource templates under Components)")
                    else:
                        src = "query:./*[" + " or ".join(f"@@templatename='{pascal(a)}'" for a in tg) + "]"
                        fields_out.append((f, "Treelist" if f["multiple"] else "Droptree", src))
                elif f["type"] == "reference":
                    tg = self.targets(f)
                    src = self.options.get("content_root", f"/sitecore/content/{ns}/Data") + "/" + (plural(pascal(tg[0])) if tg else "Items")
                    fields_out.append((f, "Treelist" if f["multiple"] else "Droptree", src))
                elif f["type"] == "media":
                    fields_out.append((f, "Image" if "image" in f["validations"]["media_kinds"] else "File", ""))
                elif f["type"] == "enum":
                    fields_out.append((f, "Droplist", f"/sitecore/content/{ns}/Settings/Enumerations/{pascal(t['id'])}{pascal(f['id'])}"))
                    self.notes.append(f"Create enumeration items {f['validations']['enum_values']} for {t['label']}.{f['label']}")
                elif f["multiple"] and f["type"] in ("short_text", "url"):
                    fields_out.append((f, "Multi-Line Text", ""))
                else:
                    fields_out.append((f, self.TYPE[f["type"]], ""))
            files.append({"path": f"sitecore/items/templates/{ns}/{folders[t['kind']]}/{pascal(t['id'])}.yml",
                          "content": self.item(self.gid(p), self.gid(f"{root_path}/{folders[t['kind']]}"), self.T_TEMPLATE, p,
                                               [(self.F_BASE, "__Base template", "|".join(bases))], title=t["label"])})
            sp = f"{p}/Content"
            files.append({"path": f"sitecore/items/templates/{ns}/{folders[t['kind']]}/{pascal(t['id'])}/Content.yml",
                          "content": self.item(self.gid(sp), self.gid(p), self.T_SECTION, sp)})
            for i, (f, stype, src) in enumerate(fields_out, 1):
                fp = f"{sp}/{pascal(f['id'])}"
                shared = [(self.F_TYPE, "Type", stype), (self.F_SORT, "__Sortorder", str(i * 100))]
                if src:
                    shared.append((self.F_SOURCE, "Source", src))
                if not f["localized"]:
                    shared.append((self.F_SHARED, "Shared", "1"))
                files.append({"path": f"sitecore/items/templates/{ns}/{folders[t['kind']]}/{pascal(t['id'])}/Content/{pascal(f['id'])}.yml",
                              "content": self.item(self.gid(fp), self.gid(sp), self.T_FIELD, fp, shared, title=f["label"])})
        module = {"namespace": f"Project.{ns}", "description": f"{ns} content model generated by the Coforge Agentic Framework",
                  "items": {"path": "items", "includes": [{"name": "templates", "path": root_path, "allowedPushOperations": "CreateUpdateAndDelete"}]}}
        readme = (f"# Sitecore templates ({ns})\n\nSitecore Content Serialization module. Copy into your solution and run `dotnet sitecore ser push`.\n\n"
                  f"- Item IDs are deterministic (UUIDv5 of the item path) so regenerating keeps IDs stable.\n"
                  f"- The root folder's parent is `{parent_id}` (`/sitecore/templates/Project` on a default install); pass "
                  "`emitter_options.sitecore.parent_id` if yours differs.\n- Required fields: add Required validators in the Validation Rules of each field "
                  "(SCS does not carry them here): " + ", ".join(f"{t['label']}.{f['label']}" for t in self.model["types"] for f in t["fields"] if f["required"])[:1500]
                  + "\n\n## Page composition\nComponent lists on page templates map to placeholders and renderings with datasource templates:\n"
                  + "\n".join(composition) + "\n\n## Enumerations\n" + "\n".join(f"- {n}" for n in self.notes) + "\n")
        return files + [{"path": f"sitecore/Project.{ns}.module.json", "content": json.dumps(module, indent=2)},
                        {"path": "sitecore/README.md", "content": readme}]


class ContentfulEmitter(ModelEmitter):
    """Contentful content types (CMA / space-import JSON) + a contentful-migration script.
    Contentful has no embedded components: component fields become entry links with linkContentType validation."""

    target, label = "contentful", "Contentful content types"
    TYPE = {"short_text": "Symbol", "long_text": "Text", "rich_text": "RichText", "integer": "Integer", "decimal": "Number", "boolean": "Boolean",
            "date": "Date", "datetime": "Date", "url": "Symbol", "email": "Symbol", "enum": "Symbol", "json": "Object"}

    def field(self, f: dict[str, Any]) -> dict[str, Any]:
        v = f["validations"]
        vals: list[dict[str, Any]] = []
        if f["type"] in ("short_text", "long_text") and (v["max_length"] or v["min_length"]):
            vals.append({"size": {k: x for k, x in (("min", v["min_length"]), ("max", v["max_length"])) if x is not None}})
        if f["type"] in ("integer", "decimal") and (v["min_value"] is not None or v["max_value"] is not None):
            vals.append({"range": {k: x for k, x in (("min", v["min_value"]), ("max", v["max_value"])) if x is not None}})
        if f["type"] == "url" or (v["pattern"] and f["type"] == "short_text"):
            vals.append({"regexp": {"pattern": v["pattern"] or URL_PATTERN}})
        if f["type"] == "email":
            vals.append({"regexp": {"pattern": r"^\w[\w.-]*@([\w-]+\.)+[\w-]+$"}})
        if f["type"] == "enum":
            vals.append({"in": v["enum_values"]})
        if v["unique"] and f["type"] in ("short_text", "integer", "decimal") and not f["multiple"]:
            vals.append({"unique": True})
        if f["type"] == "media":
            base = {"type": "Link", "linkType": "Asset"}
            item_vals = [{"linkMimetypeGroup": v["media_kinds"] or ["image"]}]
        elif f["type"] in ("reference", "component"):
            base = {"type": "Link", "linkType": "Entry"}
            item_vals = [{"linkContentType": [camel(a) for a in self.targets(f)]}] if self.targets(f) else []
        else:
            base = {"type": self.TYPE[f["type"]]}
            item_vals = vals
            vals = []
        d: dict[str, Any] = {"id": camel(f["id"]), "name": f["label"], "localized": f["localized"], "required": f["required"], "disabled": False, "omitted": False}
        if f["multiple"]:
            items = {**base, "validations": item_vals}
            if items["type"] == "Text":
                items["type"] = "Symbol"
            d.update({"type": "Array", "items": items, "validations": [{"size": {k: x for k, x in (("min", v["min_items"]), ("max", v["max_items"])) if x is not None}}]
                      if (v["min_items"] or v["max_items"]) else []})
        else:
            d.update({**base, "validations": item_vals + vals if base.get("type") == "Link" else item_vals})
        return d

    def emit(self) -> list[dict[str, str]]:
        cts = []
        js = ["// contentful-migration script: npx contentful-cli space migration --space-id <SPACE> 001-create-content-model.js",
              "module.exports = function (migration) {"]
        for t in self.model["types"]:
            fields = [self.field(f) for f in t["fields"]]
            ct = {"sys": {"id": camel(t["id"]), "type": "ContentType"}, "name": t["label"], "description": t["description"][:500],
                  "displayField": camel(t["display_field"]) if t.get("display_field") else None, "fields": fields}
            cts.append(ct)
            var = camel(t["id"])
            js.append(f"  const {var} = migration.createContentType({json.dumps(var)}).name({json.dumps(t['label'])})"
                      f".description({json.dumps(t['description'][:500])});")
            for d in fields:
                chain = f"  {var}.createField({json.dumps(d['id'])}).name({json.dumps(d['name'])}).type({json.dumps(d['type'])})"
                if d.get("linkType"):
                    chain += f".linkType({json.dumps(d['linkType'])})"
                if d["type"] == "Array":
                    chain += f".items({json.dumps(d['items'])})"
                chain += f".localized({str(d['localized']).lower()}).required({str(d['required']).lower()})"
                if d.get("validations"):
                    chain += f".validations({json.dumps(d['validations'])})"
                js.append(chain + ";")
            if ct["displayField"]:
                js.append(f"  {var}.displayField({json.dumps(ct['displayField'])});")
        js.append("};")
        return [{"path": "contentful/content-types.json", "content": json.dumps({"contentTypes": cts, "editorInterfaces": []}, indent=2, ensure_ascii=False)},
                {"path": "contentful/migrations/001-create-content-model.js", "content": "\n".join(js) + "\n"}]


class ContentstackEmitter(ModelEmitter):
    """Contentstack content types (pages, entries) and global fields (components), CMA JSON bodies."""

    target, label = "contentstack", "Contentstack content types"

    def schema(self, t: dict[str, Any], depth: int = 0, top: bool = True) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        is_ct = top and t["kind"] != "component"
        if is_ct:  # Contentstack's built-in title (and url for pages)
            out.append({"display_name": "Title", "uid": "title", "data_type": "text", "mandatory": True, "unique": t["kind"] == "page",
                        "field_metadata": {"_default": True}, "multiple": False})
            if t["kind"] == "page":
                out.append({"display_name": "URL", "uid": "url", "data_type": "text", "mandatory": True, "field_metadata": {"_default": True}, "multiple": False})
        for f in t["fields"]:
            if is_ct and f["id"] in ("title", "slug"):
                continue
            v = f["validations"]
            d: dict[str, Any] = {"display_name": f["label"], "uid": snake(f["id"]), "mandatory": f["required"], "unique": bool(v["unique"]),
                                 "multiple": f["multiple"], "non_localizable": not f["localized"], "field_metadata": {"description": f["help"]}}
            ft = f["type"]
            if ft in ("short_text", "email", "url"):
                d["data_type"] = "text"
                if v["pattern"] or ft != "short_text":
                    d["format"] = v["pattern"] or (URL_PATTERN if ft == "url" else r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
            elif ft in ("long_text", "json"):
                d["data_type"] = "text"
                d["field_metadata"]["multiline"] = True
            elif ft == "rich_text":
                d["data_type"] = "json"
                d["field_metadata"].update({"allow_json_rte": True, "rich_text_type": "advanced"})
            elif ft in ("integer", "decimal"):
                d["data_type"] = "number"
                if v["min_value"] is not None:
                    d["min"] = v["min_value"]
                if v["max_value"] is not None:
                    d["max"] = v["max_value"]
            elif ft == "boolean":
                d["data_type"] = "boolean"
            elif ft in ("date", "datetime"):
                d["data_type"] = "isodate"
                d["field_metadata"]["hide_time"] = ft == "date"
            elif ft == "media":
                d["data_type"] = "file"
                d["field_metadata"]["image"] = "image" in v["media_kinds"]
            elif ft == "enum":
                d.update({"data_type": "text", "display_type": "dropdown", "enum": {"advanced": False, "choices": [{"value": x} for x in v["enum_values"]]}})
            elif ft == "reference":
                tg = [a for a in self.targets(f)]
                d.update({"data_type": "reference", "reference_to": tg, "field_metadata": {**d["field_metadata"], "ref_multiple": f["multiple"],
                                                                                          "ref_multiple_content_types": len(tg) > 1}})
            elif ft == "component":
                tg = self.targets(f)
                if top and len(tg) > 1:
                    d.update({"data_type": "blocks", "multiple": True,
                              "blocks": [{"title": self.types[a]["label"], "uid": snake(a), "reference_to": snake(a)} for a in tg]})
                elif top:
                    d.update({"data_type": "global_field", "reference_to": snake(tg[0])})
                elif depth < 3:  # inside a global field: inline group
                    d.update({"data_type": "group", "schema": self.schema(self.types[tg[0]], depth + 1, top=False)})
                else:
                    continue
            out.append(d)
        return out

    def emit(self) -> list[dict[str, str]]:
        files = []
        for t in self.model["types"]:
            if t["kind"] == "component":
                body = {"global_field": {"title": t["label"], "uid": snake(t["id"]), "description": t["description"][:500],
                                         "schema": self.schema(t, top=False)}}
                files.append({"path": f"contentstack/global_fields/{snake(t['id'])}.json", "content": json.dumps(body, indent=2, ensure_ascii=False)})
            else:
                body = {"content_type": {"title": t["label"], "uid": snake(t["id"]), "description": t["description"][:500], "schema": self.schema(t),
                                         "options": {"title": True, "publishable": True, "is_page": t["kind"] == "page", "singleton": False,
                                                     "sub_title": [], "url_pattern": "/:title" if t["kind"] == "page" else "", "url_prefix": "/"}}}
                files.append({"path": f"contentstack/content_types/{snake(t['id'])}.json", "content": json.dumps(body, indent=2, ensure_ascii=False)})
        order = [snake(t["id"]) for t in self.model["types"] if t["kind"] == "component"] + [snake(t["id"]) for t in self.model["types"] if t["kind"] != "component"]
        files.append({"path": "contentstack/README.md", "content": "# Contentstack\n\nCreate global fields first (POST /v3/global_fields), then content types "
                      "(POST /v3/content_types) in this order:\n\n" + "\n".join(f"{i}. {u}" for i, u in enumerate(order, 1)) + "\n"})
        return files


class StrapiEmitter(ModelEmitter):
    """Strapi (v4/v5) content-type schema.json files, component JSON and core router/controller/service stubs."""

    target, label = "strapi", "Strapi schemas"

    def comp_uid(self, tid: str) -> str:
        t = self.types[tid]
        category = "shared" if (t.get("mixin") or self.is_link(tid) or tid not in self.section_types) else "sections"
        return f"{category}.{kebab(tid)}"

    def attr(self, f: dict[str, Any], in_component: bool) -> dict[str, Any] | None:
        v = f["validations"]
        ft = f["type"]
        if f["id"] == "slug" and ft == "short_text":
            return {"type": "uid", "targetField": "title", "required": f["required"]}
        if ft in ("short_text", "url"):
            if f["multiple"]:
                return {"type": "json", "required": f["required"]}
            d: dict[str, Any] = {"type": "string", "required": f["required"]}
            if v["max_length"]:
                d["maxLength"] = v["max_length"]
            if v["pattern"] or ft == "url":
                d["regex"] = v["pattern"] or URL_PATTERN
            if v["unique"]:
                d["unique"] = True
            return d
        if ft == "email":
            return {"type": "email", "required": f["required"]}
        if ft in ("long_text", "json"):
            return {"type": "text" if ft == "long_text" else "json", "required": f["required"], **({"maxLength": v["max_length"]} if v["max_length"] and ft == "long_text" else {})}
        if ft == "rich_text":
            return {"type": "blocks", "required": f["required"]}
        if ft in ("integer", "decimal"):
            d = {"type": "integer" if ft == "integer" else "decimal", "required": f["required"]}
            if v["min_value"] is not None:
                d["min"] = v["min_value"]
            if v["max_value"] is not None:
                d["max"] = v["max_value"]
            return d
        if ft == "boolean":
            return {"type": "boolean", "default": False}
        if ft in ("date", "datetime"):
            return {"type": ft, "required": f["required"]}
        if ft == "enum":
            return {"type": "enumeration", "enum": v["enum_values"], "required": f["required"]}
        if ft == "media":
            kinds = {"image": "images", "video": "videos", "document": "files"}
            return {"type": "media", "multiple": f["multiple"], "required": f["required"], "allowedTypes": [kinds.get(k, "files") for k in v["media_kinds"]]}
        if ft == "reference":
            tg = self.targets(f)
            if not tg:
                return None
            if len(tg) > 1:
                self.notes.append(f"{f['id']}: Strapi relations have one target; used {tg[0]} (polymorphic targets {tg})")
            return {"type": "relation", "relation": "manyToMany" if f["multiple"] else "manyToOne", "target": f"api::{kebab(tg[0])}.{kebab(tg[0])}"}
        if ft == "component":
            tg = self.targets(f)
            if f["multiple"] and len(tg) > 1 and not in_component:
                return {"type": "dynamiczone", "components": [self.comp_uid(a) for a in tg], "required": f["required"]}
            if len(tg) > 1:
                self.notes.append(f"{f['id']}: dynamic zones are not allowed inside components; used {tg[0]}")
            d = {"type": "component", "repeatable": f["multiple"], "component": self.comp_uid(tg[0]), "required": f["required"]}
            if f["multiple"] and v["min_items"]:
                d["min"] = v["min_items"]
            if f["multiple"] and v["max_items"]:
                d["max"] = v["max_items"]
            return d
        return None

    def emit(self) -> list[dict[str, str]]:
        self.section_types = {a for t in self.model["types"] if t["kind"] == "page" for f in t["fields"] if f["id"] in ("sections", "hero")
                              for a in self.targets(f)}
        files = []
        for t in self.model["types"]:
            attrs = {}
            for f in t["fields"]:
                a = self.attr(f, in_component=t["kind"] == "component")
                if a is not None:
                    if not f["localized"] and t["kind"] != "component":
                        a["pluginOptions"] = {"i18n": {"localized": False}}
                    attrs[camel(f["id"])] = a
            k = kebab(t["id"])
            if t["kind"] == "component":
                cat, name = self.comp_uid(t["id"]).split(".")
                body = {"collectionName": f"components_{cat}_{plural(snake(t['id']))}", "info": {"displayName": t["label"], "description": t["description"][:500]},
                        "options": {}, "attributes": attrs}
                files.append({"path": f"strapi/src/components/{cat}/{name}.json", "content": json.dumps(body, indent=2, ensure_ascii=False)})
            else:
                body = {"kind": "collectionType", "collectionName": plural(snake(t["id"])),
                        "info": {"singularName": k, "pluralName": plural(k), "displayName": t["label"], "description": t["description"][:500]},
                        "options": {"draftAndPublish": True}, "pluginOptions": {"i18n": {"localized": True}}, "attributes": attrs}
                base = f"strapi/src/api/{k}"
                files += [{"path": f"{base}/content-types/{k}/schema.json", "content": json.dumps(body, indent=2, ensure_ascii=False)},
                          {"path": f"{base}/routes/{k}.js", "content": f"'use strict';\n\nconst {{ factories }} = require('@strapi/strapi');\n\nmodule.exports = factories.createCoreRouter('api::{k}.{k}');\n"},
                          {"path": f"{base}/controllers/{k}.js", "content": f"'use strict';\n\nconst {{ factories }} = require('@strapi/strapi');\n\nmodule.exports = factories.createCoreController('api::{k}.{k}');\n"},
                          {"path": f"{base}/services/{k}.js", "content": f"'use strict';\n\nconst {{ factories }} = require('@strapi/strapi');\n\nmodule.exports = factories.createCoreService('api::{k}.{k}');\n"}]
        if self.notes:
            files.append({"path": "strapi/README.md", "content": "# Strapi\n\nCopy `src/` into your Strapi project.\n\nAdjustments made for Strapi:\n" + "\n".join(f"- {n}" for n in self.notes) + "\n"})
        return files


class DrupalEmitter(ModelEmitter):
    """Drupal 10/11 configuration YAML: node types (pages, entries), paragraph types (components), field storage + field config."""

    target, label = "drupal", "Drupal field config"
    TYPE = {"short_text": ("string", "core"), "long_text": ("string_long", "core"), "rich_text": ("text_long", "text"), "integer": ("integer", "core"),
            "decimal": ("decimal", "core"), "boolean": ("boolean", "core"), "date": ("datetime", "datetime"), "datetime": ("datetime", "datetime"),
            "media": ("entity_reference", "core"), "url": ("link", "link"), "email": ("email", "core"), "enum": ("list_string", "options"),
            "reference": ("entity_reference", "core"), "component": ("entity_reference_revisions", "entity_reference_revisions"), "json": ("string_long", "core")}

    @staticmethod
    def dump(obj: dict[str, Any]) -> str:
        return yaml.safe_dump(obj, sort_keys=False, allow_unicode=True, default_flow_style=False)

    def field_name(self, fid: str, entity: str, storage_type: str, registry: dict[tuple[str, str], str]) -> str:
        name = ("field_" + snake(fid))[:32]
        base, n = name, 2
        while (entity, name) in registry and registry[(entity, name)] != storage_type:
            name = f"{base[:29]}_{n}"
            n += 1
        return name

    def emit(self) -> list[dict[str, str]]:
        files: list[dict[str, str]] = []
        storages: dict[tuple[str, str], str] = {}
        modules_needed: set[str] = set()
        for t in self.model["types"]:
            entity = "paragraph" if t["kind"] == "component" else "node"
            bundle = snake(t["id"])[:32]
            if entity == "node":
                files.append({"path": f"drupal/config/sync/node.type.{bundle}.yml", "content": self.dump({
                    "langcode": "en", "status": True, "dependencies": {}, "name": t["label"], "type": bundle, "description": t["description"][:500],
                    "help": "", "new_revision": True, "preview_mode": 1, "display_submitted": t["kind"] == "page"})})
            else:
                modules_needed.add("paragraphs")
                files.append({"path": f"drupal/config/sync/paragraphs.paragraphs_type.{bundle}.yml", "content": self.dump({
                    "langcode": "en", "status": True, "dependencies": {}, "id": bundle, "label": t["label"], "icon_uuid": None, "icon_default": None,
                    "description": t["description"][:500], "behavior_plugins": {}})})
            for f in t["fields"]:
                if entity == "node" and f["id"] in ("title",):
                    continue  # node base field
                if entity == "node" and f["id"] == "slug":
                    self.notes.append(f"{t['label']}.slug: use the path alias (Pathauto pattern) instead of a field")
                    continue
                ftype, module = self.TYPE[f["type"]]
                v = f["validations"]
                storage_settings: dict[str, Any] = {}
                field_settings: dict[str, Any] = {}
                deps_module = {module} - {"core"}
                if f["type"] == "short_text":
                    storage_settings = {"max_length": int(v["max_length"] or 255), "case_sensitive": False, "is_ascii": False}
                elif f["type"] in ("long_text", "json"):
                    storage_settings = {"case_sensitive": False}
                elif f["type"] == "rich_text":
                    field_settings = {"allowed_formats": []}
                elif f["type"] == "decimal":
                    storage_settings = {"precision": 10, "scale": 2}
                    field_settings = {"min": v["min_value"], "max": v["max_value"], "prefix": "", "suffix": ""}
                elif f["type"] == "integer":
                    storage_settings = {"unsigned": False, "size": "normal"}
                    field_settings = {"min": v["min_value"], "max": v["max_value"], "prefix": "", "suffix": ""}
                elif f["type"] == "boolean":
                    field_settings = {"on_label": "On", "off_label": "Off"}
                elif f["type"] in ("date", "datetime"):
                    storage_settings = {"datetime_type": "date" if f["type"] == "date" else "datetime"}
                elif f["type"] == "url":
                    field_settings = {"title": 0, "link_type": 17}
                elif f["type"] == "enum":
                    storage_settings = {"allowed_values": [{"value": x, "label": label_of(x)} for x in v["enum_values"]], "allowed_values_function": ""}
                elif f["type"] == "media":
                    modules_needed.add("media")
                    deps_module.add("media")
                    storage_settings = {"target_type": "media"}
                    bundles = {"image": "image", "video": "remote_video", "document": "document"}
                    field_settings = {"handler": "default:media", "handler_settings": {"target_bundles": {bundles[k]: bundles[k] for k in v["media_kinds"] if k in bundles}}}
                elif f["type"] == "reference":
                    storage_settings = {"target_type": "node"}
                    field_settings = {"handler": "default:node", "handler_settings": {"target_bundles": {snake(a): snake(a) for a in self.targets(f)}}}
                elif f["type"] == "component":
                    modules_needed |= {"paragraphs", "entity_reference_revisions"}
                    deps_module.add("paragraphs")
                    storage_settings = {"target_type": "paragraph"}
                    field_settings = {"handler": "default:paragraph", "handler_settings": {"negate": 0, "target_bundles": {snake(a): snake(a) for a in self.targets(f)},
                                      "target_bundles_drag_drop": {snake(a): {"enabled": True, "weight": i} for i, a in enumerate(self.targets(f))}}}
                name = self.field_name(f["id"], entity, ftype, storages)
                if (entity, name) not in storages:
                    storages[(entity, name)] = ftype
                    files.append({"path": f"drupal/config/sync/field.storage.{entity}.{name}.yml", "content": self.dump({
                        "langcode": "en", "status": True, "dependencies": {"module": sorted(deps_module | {entity if entity == "node" else "paragraphs"})},
                        "id": f"{entity}.{name}", "field_name": name, "entity_type": entity, "type": ftype, "settings": storage_settings,
                        "module": module, "locked": False, "cardinality": -1 if f["multiple"] else 1, "translatable": True, "indexes": {},
                        "persist_with_no_fields": False, "custom_storage": False})})
                bundle_cfg = f"node.type.{bundle}" if entity == "node" else f"paragraphs.paragraphs_type.{bundle}"
                files.append({"path": f"drupal/config/sync/field.field.{entity}.{bundle}.{name}.yml", "content": self.dump({
                    "langcode": "en", "status": True, "dependencies": {"config": [f"field.storage.{entity}.{name}", bundle_cfg], "module": sorted(deps_module)},
                    "id": f"{entity}.{bundle}.{name}", "field_name": name, "entity_type": entity, "bundle": bundle, "label": f["label"],
                    "description": f["help"], "required": f["required"], "translatable": f["localized"], "default_value": [],
                    "default_value_callback": "", "settings": field_settings, "field_type": ftype})})
        readme = ("# Drupal configuration\n\nCopy `config/sync/*.yml` into your sync directory and run `drush config:import --partial`.\n\n"
                  f"Required contributed modules: {', '.join(sorted(modules_needed)) or 'none'} (plus core: text, link, options, datetime, media).\n\n"
                  + "\n".join(f"- {n}" for n in self.notes) + "\n")
        return files + [{"path": "drupal/README.md", "content": readme}]


class GraphqlEmitter(ModelEmitter):
    """GraphQL SDL for a headless delivery API over the neutral model (pages and entries are queryable)."""

    target, label = "graphql", "GraphQL SDL"
    SCALAR = {"short_text": "String", "long_text": "String", "integer": "Int", "decimal": "Float", "boolean": "Boolean", "date": "Date",
              "datetime": "DateTime", "url": "String", "email": "String", "json": "JSON", "rich_text": "RichText", "media": "Asset"}

    def emit(self) -> list[dict[str, str]]:
        out = ['"""Generated by the Coforge Agentic Framework from the platform-neutral content model."""', "scalar Date", "scalar DateTime", "scalar JSON", "",
               "type Asset {\n  url: String!\n  title: String\n  description: String\n  contentType: String\n  width: Int\n  height: Int\n}", "",
               "type RichText {\n  json: JSON!\n  html: String\n}", ""]
        enums, unions = [], []
        for t in self.model["types"]:
            name = pascal(t["id"])
            lines = []
            if t["kind"] != "component":
                lines.append("  id: ID!")
            for f in t["fields"]:
                tg = self.targets(f)
                if f["type"] == "enum":
                    ename = f"{name}{pascal(f['id'])}"
                    vals = [re.sub(r"\W", "_", x).upper() or "VALUE" for x in f["validations"]["enum_values"]]
                    vals = [v if v[0].isalpha() else f"V_{v}" for v in vals]
                    enums.append(f"enum {ename} {{\n" + "\n".join(f"  {v}" for v in dict.fromkeys(vals)) + "\n}")
                    gql = ename
                elif f["type"] in ("reference", "component"):
                    if not tg:
                        continue
                    if len(tg) > 1:
                        uname = f"{name}{pascal(f['id'])}Item"
                        unions.append(f"union {uname} = " + " | ".join(pascal(a) for a in tg))
                        gql = uname
                    else:
                        gql = pascal(tg[0])
                else:
                    gql = self.SCALAR[f["type"]]
                typ = f"[{gql}!]!" if f["multiple"] else (f"{gql}!" if f["required"] else gql)
                desc = f'  """{f["help"]}"""\n' if f["help"] else ""
                lines.append(f"{desc}  {camel(f['id'])}: {typ}")
            out.append(f'"""{t["label"]} ({t["kind"]}). {t["description"][:200]}"""\ntype {name} {{\n' + "\n".join(lines) + "\n}")
            out.append("")
        query = ["type Query {"]
        for t in self.model["types"]:
            if t["kind"] == "component":
                continue
            n = pascal(t["id"])
            arg = "slug: String!" if t["kind"] == "page" else "id: ID!"
            query += [f"  {camel(t['id'])}({arg}): {n}", f"  {camel(t['id'])}Collection(limit: Int = 20, skip: Int = 0): [{n}!]!"]
        query.append("}")
        sdl = "\n".join(out + enums + [""] + unions + [""] + query) + "\n"
        return [{"path": "graphql/schema.graphql", "content": sdl}]


EMITTERS: dict[str, type[ModelEmitter]] = {e.target: e for e in (AemEmitter, SitecoreEmitter, ContentfulEmitter, ContentstackEmitter, StrapiEmitter,
                                                                    DrupalEmitter, GraphqlEmitter)}


def validate_sdl(sdl: str) -> list[str]:
    """Every referenced named type is defined (a cheap structural check without a GraphQL library)."""
    defined = set(re.findall(r"^(?:type|enum|union|scalar|input|interface)\s+(\w+)", sdl, flags=re.M)) | {"String", "Int", "Float", "Boolean", "ID"}
    body = re.sub(r'"""[\s\S]*?"""', "", sdl)
    used = set(re.findall(r":\s*\[?(\w+)", body)) | {u.strip() for m in re.findall(r"union\s+\w+\s*=\s*([^\n]+)", body) for u in m.split("|")}
    return sorted(u for u in used if u and u not in defined)


def zip_files(files: list[dict[str, str]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.writestr(f["path"], f["content"])
    return buf.getvalue()


# ============================================================================ contract


class ContentModelInput(BaseModel):
    urls: list[str] = Field(default_factory=list, max_length=25, description="Pages to analyse (one per page template is ideal)")
    html: str | None = Field(default=None, description="Alternative to urls: one page's HTML", json_schema_extra={"ui": "textarea"})
    page_url: str | None = Field(default=None, description="URL the pasted HTML belongs to")
    crawl: bool = Field(default=False, description="Crawl from the first URL (same host) instead of using only the listed URLs")
    max_pages: int = Field(default=10, ge=1, le=50)
    image_path: str | None = Field(default=None, description="Optional design screenshot (PNG/JPEG/WebP) analysed with a vision model")
    image_base64: str | None = Field(default=None, description="Alternative to image_path: base64 image (console uploads)")
    image_mime: str = Field(default="image/png")
    namespace: str = Field(default="site", description="Project / namespace used in ids, paths and module names")
    targets: list[Literal["aem", "sitecore", "contentful", "contentstack", "strapi", "drupal", "graphql"]] = Field(default_factory=lambda: list(TARGETS))
    emitter_options: dict[str, dict[str, Any]] = Field(default_factory=dict, description="Per-target options, e.g. {'sitecore': {'parent_id': '...'}, 'aem': {'configuration_folder': '/conf/x'}}")

    @model_validator(mode="after")
    def _need_source(self) -> "ContentModelInput":
        if not (self.urls or self.html or self.image_path or self.image_base64):
            raise ValueError("Provide urls, html or a design image.")
        return self


class Validations(BaseModel):
    min_length: int | None = None
    max_length: int | None = None
    pattern: str | None = None
    min_value: float | None = None
    max_value: float | None = None
    enum_values: list[str] = Field(default_factory=list)
    allowed_types: list[str] = Field(default_factory=list, description="Target type ids for reference/component fields")
    media_kinds: list[Literal["image", "video", "document"]] = Field(default_factory=list)
    unique: bool = False
    min_items: int | None = None
    max_items: int | None = None


class FieldDef(BaseModel):
    id: str
    label: str
    type: FieldType
    required: bool = False
    multiple: bool = False
    localized: bool = True
    help: str = ""
    validations: Validations = Field(default_factory=Validations)


class TypeDef(BaseModel):
    id: str
    label: str
    kind: Literal["page", "entry", "component"]
    mixin: bool = Field(default=False, description="Reusable field group inherited by pages (e.g. SEO metadata)")
    description: str = ""
    display_field: str | None = None
    fields: list[FieldDef]
    evidence: list[str] = Field(default_factory=list)


class ContentModel(BaseModel):
    namespace: str
    version: str = "1.0.0"
    types: list[TypeDef]


class DetectedComponent(BaseModel):
    type: str
    selector: str
    confidence: float
    items: int = 0
    evidence: list[str] = Field(default_factory=list)


class PageAnalysis(BaseModel):
    url: str
    title: str = ""
    page_type: str
    jsonld_types: list[str] = Field(default_factory=list)
    components: list[DetectedComponent]


class EmittedTarget(BaseModel):
    target: str
    label: str
    files: list[str]
    artifact: str


class ModelIssue(BaseModel):
    severity: Literal["info", "warn", "error"]
    type: str
    field: str = ""
    message: str


class ContentModelOutput(BaseModel):
    namespace: str
    pages: list[PageAnalysis]
    component_inventory: dict[str, int]
    model: ContentModel
    model_source: Literal["model", "baseline"]
    normalisation_issues: list[ModelIssue]
    emitted: list[EmittedTarget]
    vision_used: bool = False
    findings: list[Finding]


class ProposedModel(BaseModel):
    types: list[TypeDef] = Field(default_factory=list)
    rationale: str = ""


class DesignComponent(BaseModel):
    type: Literal["hero", "card_grid", "cta_banner", "faq", "testimonial", "pricing_table", "article_body", "author_bio", "form", "key_facts",
                  "rich_text", "other"]
    label: str = ""
    fields: list[str] = Field(default_factory=list, description="Visible content slots, e.g. heading, image, price")
    item_count: int = 0


class DesignAnalysis(BaseModel):
    page_type: Literal["home_page", "product_page", "article_page", "standard_page"] = "standard_page"
    components: list[DesignComponent] = Field(default_factory=list)


REPORT_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Content model - {{ namespace }}</title>
<style>
:root{--ink:#18212f;--muted:#5d6676;--line:#e2e5eb;--bg:#f5f6f8;--card:#fff;--accent:#14532d;--page:#1f4e8c;--entry:#7a2e0e;--component:#3b2f80}
*{box-sizing:border-box}body{margin:0;font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:var(--ink);background:var(--bg)}
header{background:var(--accent);color:#fff;padding:28px 32px 60px}header h1{margin:0 0 4px;font-size:24px}header p{margin:0;opacity:.88}
main{max-width:1160px;margin:-40px auto 0;padding:0 20px 48px}section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px 22px;margin-bottom:16px}
h2{font-size:18px;margin:0 0 10px}h3{font-size:16px;margin:0 0 6px}table{width:100%;border-collapse:collapse;font-size:13.5px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.03em}
.k{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;font-weight:600;color:#fff}.page{background:var(--page)}.entry{background:var(--entry)}.component{background:var(--component)}
code{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12.5px}.muted{color:var(--muted)}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}
.ev{font-size:12.5px;color:var(--muted);margin:4px 0 8px 16px;padding:0}
</style></head><body>
<header><h1>Content model: {{ namespace }}</h1><p>{{ type_count }} types &middot; {{ page_count }} page(s) analysed &middot; model source: {{ model_source }} &middot; targets: {{ targets }}</p></header>
<main>
<section><h2>Component inventory</h2><div class="grid">{% for p in pages %}<div><h3>{{ p.path }} <span class="muted">({{ p.page_type }})</span></h3>
<ul class="ev">{% for c in p.components %}<li><b>{{ c.type }}</b> <code>{{ c.selector }}</code> - {{ c.evidence | join("; ") }}</li>{% endfor %}</ul></div>{% endfor %}</div></section>
{% for t in types %}<section><h2><span class="k {{ t.kind }}">{{ t.kind }}</span> {{ t.label }} <code class="muted">{{ t.id }}</code></h2><p>{{ t.description }}</p>
<table><thead><tr><th>Field</th><th>Type</th><th>Required</th><th>Multiple</th><th>Localised</th><th>Validation / target</th></tr></thead><tbody>
{% for f in t.fields %}<tr><td><b>{{ f.label }}</b> <code>{{ f.id }}</code>{% if f.id == t.display_field %} <span class="muted">(display)</span>{% endif %}</td><td>{{ f.type }}</td><td>{{ "yes" if f.required else "" }}</td><td>{{ "yes" if f.multiple else "" }}</td><td>{{ "yes" if f.localized else "" }}</td><td class="muted">{{ f.rule }}</td></tr>{% endfor %}
</tbody></table>{% if t.evidence %}<ul class="ev">{% for e in t.evidence %}<li>{{ e }}</li>{% endfor %}</ul>{% endif %}</section>{% endfor %}
<section><h2>Emitted artefacts</h2><table><thead><tr><th>Target</th><th>Files</th></tr></thead><tbody>{% for e in emitted %}<tr><td>{{ e.label }}</td><td><code>{{ e.files | join(", ") }}</code></td></tr>{% endfor %}</tbody></table></section>
{% if issues %}<section><h2>Normalisation notes</h2><ul>{% for i in issues %}<li>[{{ i.severity }}] {{ i.type }}{{ "." if i.field else "" }}{{ i.field }}: {{ i.message }}</li>{% endfor %}</ul></section>{% endif %}
</main></body></html>"""


def field_rule(f: dict[str, Any]) -> str:
    v = f["validations"]
    parts = []
    if v.get("allowed_types"):
        parts.append("-> " + " | ".join(v["allowed_types"]))
    if v.get("max_length"):
        parts.append(f"max {v['max_length']} chars")
    if v.get("min_value") is not None or v.get("max_value") is not None:
        parts.append(f"range {v.get('min_value')}..{v.get('max_value')}")
    if v.get("enum_values"):
        parts.append("one of " + ", ".join(v["enum_values"]))
    if v.get("pattern"):
        parts.append(f"pattern {v['pattern']}")
    if v.get("unique"):
        parts.append("unique")
    if v.get("min_items") or v.get("max_items"):
        parts.append(f"{v.get('min_items') or 0}-{v.get('max_items') or 'n'} items")
    if v.get("media_kinds"):
        parts.append("/".join(v["media_kinds"]))
    return "; ".join(parts)


# ============================================================================ the agent


@register
class ContentModelCreationAgent(BaseAgent):
    spec = AgentSpec(
        id="content_model_creation",
        name="Content Model Creation",
        category=Category.WEB,
        summary="Analyses pages (and optional design screenshots) to detect components, builds a platform-neutral content model and emits "
                "AEM, Sitecore, Contentful, Contentstack, Strapi, Drupal and GraphQL artefacts from it.",
        kind=AgentKind.HYBRID,
        tier=Tier.PRO,
        connectors=["crawler"],
        skills=["brand_voice"],
        keywords=["content model", "content types", "headless cms", "cms migration", "aem content fragment", "sitecore templates", "contentful",
                  "contentstack", "strapi", "drupal", "graphql schema"],
        produces=["content_model.json", "content_model.html", "cms-<target>.zip", "schema.graphql"],
        feeds=["blog_post_creation", "content_translation"],
    )
    Input = ContentModelInput
    Output = ContentModelOutput
    schemas = {"propose": ProposedModel, "vision": DesignAnalysis}

    def selftest_input(self) -> dict[str, Any]:
        base = "https://www.meridian-mutual.example"
        return {"urls": [f"{base}/", f"{base}/products/home-insurance.html", f"{base}/blog/claims-tips.html", f"{base}/contact.html"],
                "namespace": "meridian"}

    def stages(self):
        return [("analyse", self.analyse), ("vision", self.vision), ("propose", self.propose), ("normalise", self.normalise),
                ("emit", self.emit), ("report", self.report)]

    # -- 1. analyse ------------------------------------------------------------------------------
    def analyse(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp = state["inputs"]
        pages, warnings, degraded = [], [], []
        if inp.get("html"):
            pages.append(detect_components(inp.get("page_url") or "https://page.invalid/", inp["html"]))
        urls = list(inp.get("urls") or [])
        if urls:
            crawler = ctx.connector("crawler")
            if inp.get("crawl"):
                fetched = [p for p in crawler.crawl(urls[0], max_pages=int(inp.get("max_pages", 10))) if p.ok and p.is_html]
            else:
                fetched = [crawler.fetch(u) for u in urls[: int(inp.get("max_pages", 10)) if inp.get("crawl") else 25]]
            for p in fetched:
                if p.ok and p.html:
                    pages.append(detect_components(p.final_url or p.url, p.html))
                else:
                    warnings.append(f"Skipped {p.url}: {p.error or p.status}")
            if not any(p.ok for p in fetched):
                degraded.append("no page could be fetched")
        return {"data": {"pages": pages}, "warnings": warnings, "provenance": degraded_update(state, degraded, evidence_count=len(pages))}

    # -- 2. vision (optional) --------------------------------------------------------------------
    def vision(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp = state["inputs"]
        image: bytes | None = None
        mime = inp.get("image_mime") or "image/png"
        if inp.get("image_base64"):
            raw = inp["image_base64"].split(",", 1)[-1]
            image = base64.b64decode(raw)
        elif inp.get("image_path"):
            try:
                p = ctx.local_path(inp["image_path"])  # security policy: service callers read allowed folders only
            except PermissionError as exc:
                return {"warnings": [f"Design image not read: {exc}"], "provenance": degraded_update(state, ["design image not allowed"])}
            if not p.is_file():
                return {"warnings": [f"Design image not found: {p}"], "provenance": degraded_update(state, ["design image not found"])}
            image = p.read_bytes()
            mime = mimetypes.guess_type(p.name)[0] or mime
        if image is None:
            return {"data": {"vision_used": False}}
        if ctx.llm.offline:
            return {"data": {"vision_used": False}, "warnings": ["Design image supplied but no vision-capable model is configured; image not analysed."],
                    "provenance": degraded_update(state, ["design image not analysed (no model)"])}
        try:
            design = ctx.generate("vision", DesignAnalysis, images=[(image, mime)], known_types=COMPONENT_TYPES)
        except Exception as exc:
            return {"data": {"vision_used": False}, "warnings": [f"Vision analysis failed ({type(exc).__name__})."],
                    "provenance": degraded_update(state, ["design image analysis failed"])}
        comps = [{"type": c.type, "selector": f"design:{c.label or c.type}", "evidence": ["vision model on design screenshot"] + ([f"slots: {', '.join(c.fields)}"] if c.fields else []),
                  "fields": {}, "confidence": 0.6, "items": c.item_count} for c in design.components if c.type != "other"]
        page = {"url": "design://screenshot", "path": "design", "title": "Design screenshot", "page_type": design.page_type, "jsonld_types": [],
                "components": comps, "button_styles": []}
        return {"data": {"pages": state["data"]["pages"] + [page], "vision_used": True}}

    # -- 3. propose (model) ----------------------------------------------------------------------
    def propose(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        pages = data["pages"]
        baseline = build_baseline(pages, inp.get("namespace", "site"))
        source = "baseline"
        notes: list[str] = []
        if pages:
            evidence = [f"{p['path']} ({p['page_type']}; JSON-LD: {', '.join(p['jsonld_types']) or 'none'}): "
                        + "; ".join(f"{c['type']} {c['selector']} [{', '.join(c['evidence'][:2])}]" for c in p["components"]) for p in pages]
            compact = [{"id": t["id"], "kind": t["kind"], "fields": [f"{f['id']}:{f['type']}{'*' if f['required'] else ''}{'[]' if f['multiple'] else ''}"
                                                                     + (f"->{'|'.join(f['validations']['allowed_types'])}" if f['validations']['allowed_types'] else "")
                                                                     for f in t["fields"]]} for t in baseline["types"]]
            proposal = ctx.generate("propose", ProposedModel, namespace=baseline["namespace"], evidence=evidence,
                                    baseline=json.dumps(compact, ensure_ascii=False), field_types=sorted(FIELD_TYPES))
            if not ctx.llm.offline and proposal.types:
                merged, notes = merge_proposal(baseline, proposal.model_dump())
                baseline, source = merged, "model"
        return {"data": {"raw_model": baseline, "model_source": source, "merge_notes": notes}}

    # -- 4. normalise ----------------------------------------------------------------------------
    def normalise(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        model, issues = normalise_model(data["raw_model"], inp.get("namespace", "site"))
        ContentModel.model_validate(model)  # contract: the neutral model must be valid before any emitter runs
        checks = [Check(rule="model.normalise", status={"info": "info", "warn": "warn", "error": "fail"}[i["severity"]], severity="low" if i["severity"] == "info" else "medium",
                        message=f"{i['type']}{'.' + i['field'] if i['field'] else ''}: {i['message']}") for i in issues]
        n_ref = sum(1 for t in model["types"] for f in t["fields"] if f["type"] in ("reference", "component"))
        checks.append(Check(rule="model.references_resolve", status="pass", severity="high",
                            message=f"All {n_ref} reference/component fields resolve to defined types."))
        return {"data": {"model": model, "issues": issues}, "checks": checks}

    # -- 5. emit ---------------------------------------------------------------------------------
    def emit(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        model = data["model"]
        emitted, artifacts, checks = [], [], []
        for target in inp.get("targets") or TARGETS:
            emitter = EMITTERS[target](model, (inp.get("emitter_options") or {}).get(target))
            try:
                files = emitter.emit()
            except Exception as exc:  # one broken emitter must not sink the others
                checks.append(Check(rule=f"emit.{target}", status="fail", severity="high", message=f"{emitter.label} failed: {type(exc).__name__}: {exc}"))
                continue
            for f in files:  # every JSON/YAML file must parse
                try:
                    if f["path"].endswith(".json"):
                        json.loads(f["content"])
                    elif f["path"].endswith(".yml"):
                        list(yaml.safe_load_all(f["content"]))
                except Exception as exc:
                    checks.append(Check(rule=f"emit.{target}.parse", status="fail", severity="high", message=f"{f['path']} does not parse: {exc}"))
            if target == "graphql":
                missing = validate_sdl(files[0]["content"])
                checks.append(Check(rule="emit.graphql.sdl", status="fail" if missing else "pass", severity="high",
                                    message=f"Undefined GraphQL types: {missing}" if missing else "Every GraphQL type reference is defined."))
            art = ctx.artifacts.write_bytes(f"cms-{target}.zip", zip_files(files), "application/zip", f"{emitter.label} ({len(files)} files)")
            artifacts.append(art)
            primary = {"graphql": "graphql/schema.graphql", "aem": "aem/content-fragment-models.json", "contentful": "contentful/content-types.json"}.get(target)
            for f in files:
                if f["path"] == primary:  # the key file of the target, also as a browsable artifact (folders kept)
                    mime = "application/json" if primary.endswith(".json") else "text/plain"
                    artifacts.append(ctx.artifacts.write_text(primary, f["content"], mime, emitter.label))
            emitted.append({"target": target, "label": emitter.label, "files": [f["path"] for f in files], "artifact": art.path})
            checks.append(Check(rule=f"emit.{target}", status="pass", severity="medium", message=f"{emitter.label}: {len(files)} file(s)"))
        return {"data": {"emitted": emitted}, "artifacts": artifacts, "checks": checks}

    # -- 6. report -------------------------------------------------------------------------------
    def report(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        import jinja2

        data = state["data"]
        model, pages = data["model"], data["pages"]
        inventory = Counter(c["type"] for p in pages for c in p["components"])
        findings = []
        for p in pages:
            for c in p["components"]:
                if c["type"] == "hero" and c["fields"].get("image") and c["fields"].get("image_alt") is None:
                    findings.append(Finding(title=f"Hero image without alt text on {p['path']}", category="content", severity="medium",
                                            detail="The model makes image_alt a field on Hero; populate it during migration.",
                                            evidence=c["selector"], target=p["url"], source_agent=self.spec.id,
                                            recommendation="Fill hero.image_alt for every migrated hero."))
        findings.append(Finding(title=f"Content model: {len(model['types'])} types from {len(pages)} page(s)", category="content", severity="low",
                                detail=", ".join(f"{k} x{v}" for k, v in inventory.most_common()), evidence="; ".join(t["id"] for t in model["types"]),
                                recommendation="Review the model in a modelling workshop before importing.", source_agent=self.spec.id))
        out = ContentModelOutput(
            namespace=model["namespace"],
            pages=[PageAnalysis(url=p["url"], title=p["title"], page_type=p["page_type"], jsonld_types=p["jsonld_types"],
                                components=[DetectedComponent(type=c["type"], selector=c["selector"], confidence=c["confidence"], items=c.get("items", 0),
                                                              evidence=c["evidence"]) for c in p["components"]]) for p in pages],
            component_inventory=dict(inventory), model=ContentModel.model_validate(model), model_source=data["model_source"],
            normalisation_issues=[ModelIssue(**i) for i in data["issues"]], emitted=[EmittedTarget(**e) for e in data["emitted"]],
            vision_used=bool(data.get("vision_used")), findings=findings)
        env = jinja2.Environment(autoescape=True, trim_blocks=True, lstrip_blocks=True)
        html = env.from_string(REPORT_TEMPLATE).render(
            namespace=model["namespace"], type_count=len(model["types"]), page_count=len(pages), model_source=data["model_source"],
            targets=", ".join(e["target"] for e in data["emitted"]),
            pages=[{"path": p["path"], "page_type": p["page_type"], "components": p["components"]} for p in pages],
            types=[{**t, "fields": [{**f, "rule": field_rule(f)} for f in t["fields"]]} for t in model["types"]],
            emitted=data["emitted"], issues=data["issues"])
        artifacts = [ctx.artifacts.write_json("content_model.json", model, "Platform-neutral intermediate content model"),
                     ctx.artifacts.write_text("content_model.html", html, "text/html", "Content model overview")]
        kinds = Counter(t["kind"] for t in model["types"])
        summary = (f"{len(model['types'])} content types ({kinds.get('page', 0)} page, {kinds.get('entry', 0)} entry, {kinds.get('component', 0)} component) "
                   f"from {len(pages)} page(s) and {sum(inventory.values())} detected components; emitted {len(data['emitted'])} target(s): "
                   + ", ".join(e["target"] for e in data["emitted"]))
        return {"output": out.model_dump(mode="json"), "artifacts": artifacts, "summary": summary}
