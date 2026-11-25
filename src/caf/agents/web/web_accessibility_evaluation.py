"""Web Accessibility Evaluation - deterministic, standards-based detection; the model only writes fixes.

Detection
  * axe-core 4.10.2 (Deque, MPL-2.0, vendored in caf/vendor/axe-core) injected into Chromium through Playwright
    with page.add_script_tag(content=...) and run for the requested WCAG standard (axe runOnly tags)
  * static fallback when no browser is available: img/input-image alt, form labels, select names, html lang,
    document title, link and button names, heading order, iframe titles and inline-style colour contrast -
    emitted with axe-compatible rule ids and tags so mapping, scoring and CI behave identically
Mapping: every result is mapped to its WCAG success criterion, name and conformance level
(axe tag wcag111 -> 1.1.1 'Non-text Content' level A) from config/skills/wcag_criteria.yaml.
Remediation: the model writes code snippets + plain-English explanations per violation type; offline the
deterministic fix text from the YAML is used.
CI: `fail_on` (minor | moderate | serious | critical) sets the gate; the agent returns ci_pass and writes a
JUnit XML artifact, so any pipeline (GitHub Actions, Azure DevOps, Jenkins, GitLab) can fail a build on regression.
Optional CI add-ons that complement this gate (run alongside, not required):
    npx pa11y-ci --sitemap https://www.example.com/sitemap.xml          # HTML_CodeSniffer + axe runners
    npx @lhci/cli autorun --collect.url=https://www.example.com/        # Lighthouse accessibility category

Stages: load -> scan -> map -> remediate -> ci_gate -> report
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from bs4 import BeautifulSoup, Tag
from pydantic import BaseModel, Field, model_validator

from ...core import AgentKind, AgentSpec, BaseAgent, Category, Check, RunContext, Tier, register
from ...core.handoff import Finding

Impact = Literal["minor", "moderate", "serious", "critical"]
AXE_DIR = Path(__file__).resolve().parents[2] / "vendor" / "axe-core"
AXE_VERSION = "4.10.2"
IMPACT_ORDER = ["minor", "moderate", "serious", "critical"]


def degraded_update(state: dict[str, Any], reasons: list[str], **extra: Any) -> dict[str, Any]:
    """Provenance update for one stage (the core's merge_provenance reducer accumulates degraded_reasons)."""
    out: dict[str, Any] = dict(extra)
    if reasons:
        out["degraded_reasons"] = list(dict.fromkeys(reasons))
    return out

# ============================================================================ WCAG mapping (pure)

_SC_TAG = re.compile(r"^wcag(\d)(\d)(\d{1,2})$")
_LEVEL_TAG = {"wcag2a": "A", "wcag2aa": "AA", "wcag2aaa": "AAA", "wcag21a": "A", "wcag21aa": "AA", "wcag21aaa": "AAA",
              "wcag22a": "A", "wcag22aa": "AA", "wcag22aaa": "AAA"}


def sc_from_tag(tag: str) -> str | None:
    """'wcag111' -> '1.1.1', 'wcag1410' -> '1.4.10', 'wcag412' -> '4.1.2'; level tags (wcag2aa) -> None."""
    m = _SC_TAG.match(tag or "")
    return f"{m.group(1)}.{m.group(2)}.{int(m.group(3))}" if m else None


def wcag_refs(tags: list[str], criteria: dict[str, list[Any]]) -> list[dict[str, str]]:
    level = next((_LEVEL_TAG[t] for t in tags if t in _LEVEL_TAG), "")
    out = []
    for t in tags:
        sc = sc_from_tag(t)
        if sc is None:
            continue
        c = criteria.get(sc)
        out.append({"sc": sc, "name": c[0] if c else "Unknown success criterion", "level": c[1] if c else level})
    return out


def impact_rank(impact: str | None) -> int:
    return IMPACT_ORDER.index(impact) if impact in IMPACT_ORDER else 0


# ============================================================================ colour contrast (pure)

_NAMED = {"black": "#000000", "white": "#ffffff", "red": "#ff0000", "green": "#008000", "blue": "#0000ff", "gray": "#808080",
          "grey": "#808080", "silver": "#c0c0c0", "yellow": "#ffff00", "orange": "#ffa500", "navy": "#000080", "transparent": None}


def parse_color(value: str | None) -> tuple[float, float, float] | None:
    if not value:
        return None
    v = value.strip().lower()
    if v in _NAMED:
        v = _NAMED[v] or ""
        if not v:
            return None
    m = re.match(r"^#([0-9a-f]{3}|[0-9a-f]{6})$", v)
    if m:
        h = m.group(1)
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]
    m = re.match(r"^rgba?\(\s*(\d+)[ ,]+(\d+)[ ,]+(\d+)(?:[ ,/]+([\d.]+))?\s*\)$", v)
    if m:
        if m.group(4) is not None and float(m.group(4)) < 1:
            return None  # translucent: cannot judge statically
        return tuple(int(m.group(i)) / 255 for i in (1, 2, 3))  # type: ignore[return-value]
    return None


def relative_luminance(rgb: tuple[float, float, float]) -> float:
    def ch(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(fg: tuple[float, float, float], bg: tuple[float, float, float]) -> float:
    l1, l2 = sorted((relative_luminance(fg), relative_luminance(bg)), reverse=True)
    return (l1 + 0.05) / (l2 + 0.05)


def _style(el: Tag) -> dict[str, str]:
    out = {}
    for decl in (el.get("style") or "").split(";"):
        if ":" in decl:
            k, v = decl.split(":", 1)
            out[k.strip().lower()] = v.strip().lower()
    return out


def _font_px(style: dict[str, str], tag: str) -> float:
    m = re.match(r"^([\d.]+)(px|pt|rem|em)$", style.get("font-size", ""))
    if m:
        n = float(m.group(1))
        return n * {"px": 1, "pt": 4 / 3, "rem": 16, "em": 16}[m.group(2)]
    return {"h1": 32, "h2": 24, "h3": 18.72}.get(tag, 16.0)


def passing_color(fg_hex: str, bg_hex: str, target: float) -> str | None:
    """Closest colour to fg (moving toward black on light backgrounds, white on dark) that meets target contrast."""
    fg, bg = parse_color(fg_hex), parse_color(bg_hex)
    if fg is None or bg is None:
        return None
    toward = (0.0, 0.0, 0.0) if relative_luminance(bg) > 0.18 else (1.0, 1.0, 1.0)
    for i in range(1, 101):
        c = tuple(f + (w - f) * i / 100 for f, w in zip(fg, toward))
        if contrast_ratio(c, bg) >= target + 0.05:  # small margin for rounding
            return "#" + "".join(f"{round(x * 255):02x}" for x in c)
    return None


def _humanize_href(href: str) -> str:
    seg = [x for x in re.split(r"[/?#]", href or "") if x and not x.startswith("http")]
    word = re.sub(r"\.(html?|php|aspx?)$", "", seg[-1] if seg else "home")
    return re.sub(r"[-_]+", " ", word).strip().capitalize() or "Home"


def _add_attr(snippet: str, tag: str, attr: str, value: str) -> str:
    return re.sub(rf"<{tag}\b", f'<{tag} {attr}="{value}"', snippet, count=1)


def specific_fix(v: dict[str, Any], brand: str = "") -> str | None:
    """Deterministic, element-specific fix for common rules (offline remediation that refers to the real markup)."""
    html = (v.get("html_snippets") or [""])[0]
    rid = v["id"]
    if rid == "color-contrast":
        m_fg = re.search(r"foreground colou?r: (#[0-9a-fA-F]{3,6})", v.get("failure_summary", ""))
        m_bg = re.search(r"background colou?r: (#[0-9a-fA-F]{3,6})", v.get("failure_summary", ""))
        m_t = re.search(r"Expected contrast ratio of ([\d.]+):1", v.get("failure_summary", ""))
        if m_fg and m_bg:
            target = float(m_t.group(1)) if m_t else 4.5
            new = passing_color(m_fg.group(1), m_bg.group(1), target)
            if new:
                ratio = contrast_ratio(parse_color(new), parse_color(m_bg.group(1)))  # type: ignore[arg-type]
                sel = (v.get("targets") or ["element"])[0]
                return (f"/* {m_fg.group(1)} on {m_bg.group(1)} fails {target}:1 - closest passing colour is {new} ({ratio:.2f}:1) */\n"
                        f"{sel} {{ color: {new}; }}")
    if rid in ("image-alt", "input-image-alt") and html:
        return _add_attr(html, "img" if rid == "image-alt" else "input", "alt", "[describe the image, or leave empty if decorative]")
    if rid == "link-name" and html:
        href = re.search(r'href="([^"]*)"', html)
        return _add_attr(html, "a", "aria-label", _humanize_href(href.group(1) if href else ""))
    if rid == "button-name" and html:
        return _add_attr(html, "button", "aria-label", "[describe the action]") if html.startswith("<button") else None
    if rid in ("label", "select-name") and html:
        name = re.search(r'name="([^"]+)"', html)
        fid = (name.group(1) if name else "field").replace(" ", "-")
        tag = "select" if html.startswith("<select") else ("textarea" if html.startswith("<textarea") else "input")
        field = html if 'id="' in html else _add_attr(html, tag, "id", fid)
        return f'<label for="{fid}">{fid.replace("-", " ").replace("_", " ").capitalize()}</label>\n{field}'
    if rid == "heading-order" and html:
        m = re.match(r"<h([1-6])\b", html)
        if m:
            lvl = int(m.group(1))
            prev = re.search(r"h(\d) followed by h(\d)", v.get("failure_summary", ""))
            new = int(prev.group(1)) + 1 if prev else max(1, lvl - 1)
            fixed = re.sub(rf"^<h{lvl}\b", f'<h{new} class="h{lvl}-style"', html).replace(f"</h{lvl}>", f"</h{new}>")
            return f"{fixed}\n/* keep the visual size with CSS: .h{lvl}-style {{ font-size: <h{lvl} size>; }} */"
    if rid == "html-has-lang":
        return '<html lang="en">'
    if rid == "document-title":
        return f"<title>[Page topic] | {brand or '[Site name]'}</title>"
    return None


# ============================================================================ static fallback (pure)

STATIC_RULES: dict[str, dict[str, Any]] = {
    "image-alt": {"impact": "critical", "tags": ["cat.text-alternatives", "wcag2a", "wcag111", "section508"], "help": "Images must have alternative text"},
    "input-image-alt": {"impact": "critical", "tags": ["cat.text-alternatives", "wcag2a", "wcag111", "wcag412"], "help": "Image buttons must have alternative text"},
    "label": {"impact": "critical", "tags": ["cat.forms", "wcag2a", "wcag412"], "help": "Form elements must have labels"},
    "select-name": {"impact": "critical", "tags": ["cat.forms", "wcag2a", "wcag412"], "help": "Select element must have an accessible name"},
    "button-name": {"impact": "critical", "tags": ["cat.name-role-value", "wcag2a", "wcag412"], "help": "Buttons must have discernible text"},
    "link-name": {"impact": "serious", "tags": ["cat.name-role-value", "wcag2a", "wcag244", "wcag412"], "help": "Links must have discernible text"},
    "html-has-lang": {"impact": "serious", "tags": ["cat.language", "wcag2a", "wcag311"], "help": "<html> element must have a lang attribute"},
    "html-lang-valid": {"impact": "serious", "tags": ["cat.language", "wcag2a", "wcag311"], "help": "<html> element must have a valid value for the lang attribute"},
    "document-title": {"impact": "serious", "tags": ["cat.text-alternatives", "wcag2a", "wcag242"], "help": "Documents must have <title> element to aid in navigation"},
    "frame-title": {"impact": "serious", "tags": ["cat.text-alternatives", "wcag2a", "wcag412"], "help": "Frames must have an accessible name"},
    "color-contrast": {"impact": "serious", "tags": ["cat.color", "wcag2aa", "wcag143"], "help": "Elements must meet minimum color contrast ratio thresholds"},
    "heading-order": {"impact": "moderate", "tags": ["cat.semantics", "best-practice"], "help": "Heading levels should only increase by one"},
}
_LANG_RX = re.compile(r"^[a-zA-Z]{2,3}(-[a-zA-Z0-9]{2,8})*$")
_TEXT_TAGS = ["p", "span", "a", "li", "h1", "h2", "h3", "h4", "h5", "h6", "td", "th", "label", "button", "div", "small", "strong", "em", "figcaption", "blockquote"]


def css_path(el: Tag) -> str:
    parts: list[str] = []
    node: Any = el
    while isinstance(node, Tag) and node.name not in ("[document]",) and len(parts) < 5:
        if node.get("id"):
            parts.append(f"{node.name}#{node['id']}")
            break
        cls = [c for c in node.get("class", []) if re.match(r"^[A-Za-z_-][\w-]*$", c)][:2]
        sel = node.name + "".join(f".{c}" for c in cls)
        parent = node.parent
        if isinstance(parent, Tag) and parent.name != "[document]":
            same = parent.find_all(node.name, recursive=False)
            alike = [s for s in same if [c for c in s.get("class", []) if c in cls] == cls]
            if len(same) > 1 and (not cls or len(alike) > 1):
                sel += f":nth-of-type({same.index(node) + 1})"
        parts.append(sel)
        if node.name in ("html", "body"):
            break
        node = node.parent
    return " > ".join(reversed(parts))


def _snippet(el: Tag, limit: int = 220) -> str:
    s = str(el)
    if len(s) <= limit:
        return s
    m = re.match(r"^<[^>]+>", s)
    return (m.group(0) if m else s[:limit]) + "..."


def _text_name(el: Tag) -> str:
    """Accessible-name approximation: text, img alt and svg <title>, skipping aria-hidden subtrees."""
    parts: list[str] = []
    for node in el.descendants:
        if isinstance(node, Tag):
            if node.get("aria-hidden") == "true":
                continue
            if node.name == "img" and node.get("alt"):
                if not any(p.get("aria-hidden") == "true" for p in node.parents if isinstance(p, Tag)):
                    parts.append(node["alt"])
            if node.name == "title" and node.parent is not None and node.parent.name == "svg" and node.parent.get("aria-hidden") != "true":
                parts.append(node.get_text(" "))
        elif isinstance(node, str) and node.strip():
            if not any(isinstance(p, Tag) and p.get("aria-hidden") == "true" for p in node.parents):
                parts.append(node.strip())
    return " ".join(" ".join(parts).split())


def accessible_name(el: Tag, soup: BeautifulSoup) -> str:
    if el.get("aria-labelledby"):
        txt = " ".join((soup.find(id=i).get_text(" ", strip=True) if soup.find(id=i) else "") for i in el["aria-labelledby"].split())
        if txt.strip():
            return txt.strip()
    if (el.get("aria-label") or "").strip():
        return el["aria-label"].strip()
    if el.name in ("input", "select", "textarea"):
        if el.get("id"):
            lab = soup.find("label", attrs={"for": el["id"]})
            if lab is not None and lab.get_text(strip=True):
                return lab.get_text(" ", strip=True)
        wrap = el.find_parent("label")
        if wrap is not None and wrap.get_text(strip=True):
            return wrap.get_text(" ", strip=True)
        typ = (el.get("type") or "").lower()
        if typ in ("submit", "reset"):
            return el.get("value") or typ.capitalize()
        if typ in ("button",) and el.get("value"):
            return el["value"]
        if typ == "image" and el.get("alt"):
            return el["alt"]
        return (el.get("title") or el.get("placeholder") or "").strip()
    name = _text_name(el)
    return name or (el.get("title") or "").strip()


def static_scan(html: str, include_tags: set[str]) -> dict[str, Any]:
    """axe-compatible results from static HTML (no CSS, no JS). Returns {violations, passes, incomplete, inapplicable}."""
    soup = BeautifulSoup(html or "", "lxml")
    found: dict[str, list[dict[str, Any]]] = {k: [] for k in STATIC_RULES}
    applicable: dict[str, int] = {k: 0 for k in STATIC_RULES}
    incomplete: list[dict[str, Any]] = []

    def hit(rule: str, el: Tag, summary: str) -> None:
        found[rule].append({"target": [css_path(el)], "html": _snippet(el), "failureSummary": summary, "impact": STATIC_RULES[rule]["impact"]})

    for img in soup.find_all("img"):
        applicable["image-alt"] += 1
        if img.get("alt") is None and not img.get("aria-label") and not img.get("aria-labelledby") and img.get("role") not in ("presentation", "none"):
            hit("image-alt", img, "Element does not have an alt attribute")
    for inp in soup.find_all("input", attrs={"type": re.compile(r"^image$", re.I)}):
        applicable["input-image-alt"] += 1
        if not accessible_name(inp, soup):
            hit("input-image-alt", inp, "Element has no alt attribute")
    for el in soup.find_all(["input", "textarea"]):
        typ = (el.get("type") or "text").lower()
        if el.name == "input" and typ in ("hidden", "submit", "reset", "button", "image"):
            continue
        applicable["label"] += 1
        if not accessible_name(el, soup):
            hit("label", el, "Form element does not have an implicit (wrapped) <label>, an explicit <label>, aria-label or aria-labelledby")
    for el in soup.find_all("select"):
        applicable["select-name"] += 1
        if not accessible_name(el, soup):
            hit("select-name", el, "Element does not have an accessible name")
    for el in soup.find_all(["button"]) + soup.find_all(attrs={"role": "button"}) + soup.find_all("input", attrs={"type": re.compile(r"^button$", re.I)}):
        applicable["button-name"] += 1
        if not accessible_name(el, soup):
            hit("button-name", el, "Element does not have inner text that is visible to screen readers")
    for a in soup.find_all("a", href=True):
        applicable["link-name"] += 1
        if not accessible_name(a, soup):
            hit("link-name", a, "Element does not have text that is visible to screen readers")
    html_el = soup.find("html")
    if html_el is not None:
        applicable["html-has-lang"] += 1
        lang = (html_el.get("lang") or "").strip()
        if not lang:
            hit("html-has-lang", html_el, "The <html> element does not have a lang attribute")
        else:
            applicable["html-lang-valid"] += 1
            if not _LANG_RX.match(lang):
                hit("html-lang-valid", html_el, f"Value of lang attribute '{lang}' is not valid")
        applicable["document-title"] += 1
        t = soup.find("title")
        if t is None or not t.get_text(strip=True):
            hit("document-title", html_el, "Document does not have a non-empty <title> element")
    for fr in soup.find_all(["iframe", "frame"]):
        applicable["frame-title"] += 1
        if not (fr.get("title") or "").strip() and not fr.get("aria-label"):
            hit("frame-title", fr, "Element has no title attribute")
    prev = None
    for h in soup.find_all(re.compile(r"^h[1-6]$")):
        applicable["heading-order"] += 1
        lvl = int(h.name[1])
        if prev is not None and lvl > prev + 1:
            hit("heading-order", h, f"Heading order invalid (h{prev} followed by h{lvl})")
        prev = lvl
    # inline-style contrast: only elements that declare a colour inline (CSS files are not evaluated)
    for el in soup.find_all(_TEXT_TAGS):
        st = _style(el)
        if "color" not in st or not el.get_text(strip=True):
            continue
        fg = parse_color(st["color"])
        bg_val, node = None, el
        while isinstance(node, Tag):
            s2 = _style(node)
            bg_val = s2.get("background-color") or (s2.get("background", "").split()[0] if s2.get("background") else None)
            if bg_val:
                break
            node = node.parent
        bg = parse_color(bg_val) if bg_val else (1.0, 1.0, 1.0)
        if fg is None or bg is None:
            incomplete.append({"id": "color-contrast", "target": [css_path(el)], "html": _snippet(el)})
            continue
        applicable["color-contrast"] += 1
        ratio = contrast_ratio(fg, bg)
        px = _font_px(st, el.name)
        bold = st.get("font-weight", "") in ("bold", "bolder", "700", "800", "900") or el.name in ("h1", "h2", "h3", "strong", "b")
        need = 3.0 if (px >= 24 or (px >= 18.66 and bold)) else 4.5
        if ratio < need:
            hit("color-contrast", el, f"Element has insufficient color contrast of {ratio:.2f} (foreground color: {st['color']}, "
                                      f"background color: {bg_val or '#ffffff (assumed)'}). Expected contrast ratio of {need}:1")

    violations, passes, inapplicable = [], [], 0
    for rid, meta in STATIC_RULES.items():
        if not (set(meta["tags"]) & include_tags):
            continue
        if found[rid]:
            violations.append({"id": rid, "impact": meta["impact"], "tags": meta["tags"], "help": meta["help"], "description": meta["help"],
                               "helpUrl": f"https://dequeuniversity.com/rules/axe/4.10/{rid}", "nodes": found[rid]})
        elif applicable[rid]:
            passes.append({"id": rid, "nodes": applicable[rid]})
        else:
            inapplicable += 1
    return {"violations": violations, "passes": passes, "incomplete": [{"id": "color-contrast", "nodes": len(incomplete)}] if incomplete else [],
            "inapplicable": inapplicable, "testEngine": {"name": "caf-static", "version": "1.0"}}


# ============================================================================ normalise, score, CI (pure)


def _flatten_target(t: Any) -> str:
    if isinstance(t, list):
        return " >> ".join(_flatten_target(x) for x in t)
    return str(t)


def normalise_violations(raw: list[dict[str, Any]], criteria: dict[str, list[Any]], fail_on: str) -> list[dict[str, Any]]:
    out = []
    for v in raw:
        nodes = v.get("nodes") or []
        impact = v.get("impact") or max((n.get("impact") or "minor" for n in nodes), key=impact_rank, default="minor")
        tags = v.get("tags") or []
        out.append({"id": v["id"], "impact": impact, "tags": tags, "wcag": wcag_refs(tags, criteria), "help": v.get("help", ""),
                    "description": v.get("description", ""), "help_url": v.get("helpUrl", ""), "nodes_count": len(nodes),
                    "targets": [_flatten_target(n.get("target")) for n in nodes][:10],
                    "html_snippets": [n.get("html", "")[:300] for n in nodes][:5],
                    "failure_summary": (nodes[0].get("failureSummary") or "")[:600] if nodes else "",
                    "best_practice": "best-practice" in tags and not any(sc_from_tag(t) for t in tags),
                    "blocking": impact_rank(impact) >= impact_rank(fail_on)})
    out.sort(key=lambda v: (-impact_rank(v["impact"]), -v["nodes_count"], v["id"]))
    return out


def a11y_score(violations: list[dict[str, Any]], passes_count: int, scoring: dict[str, Any]) -> float:
    pw = float(scoring.get("pass_weight", 3))
    vw = scoring.get("violation_weights", {"critical": 10, "serious": 7, "moderate": 3, "minor": 1})
    bad = sum(float(vw.get(v["impact"], 3)) for v in violations)
    good = pw * passes_count
    return round(100 * good / (good + bad), 1) if (good + bad) else 100.0


def ci_gate(violations: list[dict[str, Any]], fail_on: str) -> tuple[bool, list[dict[str, Any]]]:
    blocking = [v for v in violations if impact_rank(v["impact"]) >= impact_rank(fail_on)]
    return not blocking, blocking


def junit_xml(url: str, standard_label: str, engine: str, violations: list[dict[str, Any]], passes: list[str], fail_on: str,
              timestamp: str) -> str:
    total = len(violations) + len(passes)
    failures = sum(1 for v in violations if v["blocking"])
    suites = ET.Element("testsuites", name="accessibility", tests=str(total), failures=str(failures), errors="0")
    suite = ET.SubElement(suites, "testsuite", name=f"{standard_label}: {url}", tests=str(total), failures=str(failures),
                          errors="0", skipped="0", timestamp=timestamp)
    props = ET.SubElement(suite, "properties")
    for k, v in (("engine", engine), ("standard", standard_label), ("fail_on", fail_on), ("url", url)):
        ET.SubElement(props, "property", name=k, value=v)
    for v in violations:
        sc = v["wcag"][0] if v["wcag"] else None
        cls = f"a11y.wcag.{sc['sc'].replace('.', '_')}" if sc else "a11y.best-practice"
        ref = f" [{sc['sc']} {sc['level']}]" if sc else " [best practice]"
        tc = ET.SubElement(suite, "testcase", classname=cls, name=f"{v['id']}: {v['help']}{ref}", time="0")
        body = "\n".join(f"- {t}\n  {h}" for t, h in zip(v["targets"], v["html_snippets"] + [""] * len(v["targets"])))
        if v["blocking"]:
            f = ET.SubElement(tc, "failure", type=v["impact"], message=f"{v['nodes_count']} element(s): {v['help']}")
            f.text = f"{v['failure_summary']}\n\n{body}\n\nFix: {v.get('explanation', '')}"
        else:
            so = ET.SubElement(tc, "system-out")
            so.text = f"{v['impact']} violation below fail_on={fail_on}: {v['nodes_count']} element(s)\n{body}"
    for pid in passes:
        ET.SubElement(suite, "testcase", classname="a11y.pass", name=pid, time="0")
    ET.indent(suites)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(suites, encoding="unicode")


# ============================================================================ contract


class A11yInput(BaseModel):
    url: str | None = Field(default=None, description="Page to evaluate")
    html: str | None = Field(default=None, description="Alternative to url: the page HTML (with url, relative assets resolve against it)",
                             json_schema_extra={"ui": "textarea"})
    standard: Literal["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"] = Field(
        default="wcag21aa", description="WCAG version and conformance level to test against (wcag21aa = WCAG 2.1 level AA)")
    include_best_practices: bool = Field(default=True, description="Also run axe best-practice rules (reported, never mapped to a WCAG SC)")
    fail_on: Literal["minor", "moderate", "serious", "critical"] = Field(default="serious", description="CI gate: fail on violations of this impact or worse")
    engine: Literal["auto", "axe", "static"] = Field(default="auto", description="auto = axe-core in Chromium when available, else static checks")
    viewport_width: int = Field(default=1280, ge=320, le=3840, description="Width in pixels of the browser window the page is rendered in (1280 = desktop, 390 = a phone)")
    viewport_height: int = Field(default=900, ge=320, le=4000, description="Height in pixels of the browser window the page is rendered in")

    @model_validator(mode="after")
    def _need_source(self) -> "A11yInput":
        if not (self.url or self.html):
            raise ValueError("Provide url or html.")
        return self


class WcagRef(BaseModel):
    sc: str
    name: str
    level: str


class Violation(BaseModel):
    id: str
    impact: Impact
    wcag: list[WcagRef]
    help: str
    description: str = ""
    help_url: str = ""
    nodes_count: int
    targets: list[str]
    html_snippets: list[str]
    failure_summary: str = ""
    fix_snippet: str = ""
    explanation: str = ""
    best_practice: bool = False
    blocking: bool = False


class A11yOutput(BaseModel):
    url: str | None
    standard: str
    standard_label: str
    engine: str
    viewport: str
    violations: list[Violation]
    passes_count: int
    incomplete_count: int
    inapplicable_count: int = 0
    impact_counts: dict[str, int]
    score: float = Field(description="0-100: passed-rule weight vs impact-weighted violations")
    fail_on: str
    ci_pass: bool
    blocking_count: int
    junit_artifact: str = ""
    remediation_source: Literal["model", "rule_library"]
    findings: list[Finding]


class Remediation(BaseModel):
    rule_id: str = Field(description="axe rule id, copied exactly")
    explanation: str = Field(description="Plain-English: who is affected and why, 1-3 sentences")
    fix_snippet: str = Field(description="Corrected HTML/CSS/ARIA for the failing element(s) shown")


class RemediationSet(BaseModel):
    items: list[Remediation] = Field(default_factory=list)


REPORT_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Accessibility report - {{ url }}</title>
<style>
:root{--ink:#18212f;--muted:#5d6676;--line:#e2e5eb;--bg:#f5f6f8;--card:#fff;--critical:#b42318;--serious:#c4320a;--moderate:#b54708;--minor:#475467;--ok:#067647;--accent:#0b4a6f}
*{box-sizing:border-box}body{margin:0;font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:var(--ink);background:var(--bg)}
header{background:var(--accent);color:#fff;padding:28px 32px 64px}header h1{margin:0 0 4px;font-size:24px}header p{margin:0;opacity:.88;word-break:break-all}
main{max-width:1120px;margin:-44px auto 0;padding:0 20px 48px}section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:20px 22px;margin-bottom:18px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:18px}.tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.tile b{display:block;font-size:26px}.tile span{color:var(--muted);font-size:13px}h2{font-size:18px;margin:0 0 10px}
.gate{font-weight:700;font-size:15px;padding:4px 12px;border-radius:8px;display:inline-block}.gate.pass{background:#ecfdf3;color:var(--ok)}.gate.fail{background:#fef3f2;color:var(--critical)}
.imp{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;font-weight:700;color:#fff}.critical{background:var(--critical)}.serious{background:var(--serious)}.moderate{background:var(--moderate)}.minor{background:var(--minor)}
.sc{display:inline-block;border:1px solid var(--line);border-radius:6px;padding:0 6px;font-size:12px;margin-right:4px}
pre{background:#0f172a;color:#e2e8f0;padding:10px 12px;border-radius:8px;overflow:auto;font-size:12.5px;white-space:pre-wrap}code{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12.5px}
.muted{color:var(--muted)}.note{font-size:13px;color:var(--muted)}ul.t{margin:4px 0 8px 18px;padding:0}
</style></head><body>
<header><h1>Accessibility evaluation</h1><p>{{ url }} &middot; {{ standard_label }} &middot; {{ engine }} &middot; {{ generated }}</p></header>
<main>
<div class="tiles"><div class="tile"><b>{{ score }}</b><span>accessibility score</span></div><div class="tile"><b>{{ violations | length }}</b><span>violated rules</span></div>
<div class="tile"><b>{{ nodes }}</b><span>failing elements</span></div><div class="tile"><b>{{ passes_count }}</b><span>rules passed</span></div>
<div class="tile"><b>{{ incomplete_count }}</b><span>need manual review</span></div></div>
<section><h2>CI gate</h2><span class="gate {{ 'pass' if ci_pass else 'fail' }}">{{ 'PASS' if ci_pass else 'FAIL' }}</span>
<span class="muted"> fail_on = {{ fail_on }}: {{ blocking_count }} blocking violation type(s).</span>
<p class="note">Automated testing finds roughly a third to a half of WCAG issues; keyboard, screen-reader and cognitive reviews are still needed. Remediation: {{ remediation_label }}.</p></section>
{% for v in violations %}<section><h2><span class="imp {{ v.impact }}">{{ v.impact }}</span> {{ v.help }} <code class="muted">{{ v.id }}</code></h2>
<p>{% for w in v.wcag %}<span class="sc">WCAG {{ w.sc }} {{ w.name }} ({{ w.level }})</span>{% endfor %}{% if v.best_practice %}<span class="sc">best practice</span>{% endif %}
<span class="muted">{{ v.nodes_count }} element(s){% if v.blocking %} &middot; blocks CI{% endif %}</span></p>
<p>{{ v.explanation }}</p><ul class="t">{% for t in v.targets %}<li><code>{{ t }}</code></li>{% endfor %}</ul>
{% if v.html_snippets %}<p class="muted">Failing markup</p><pre>{{ v.html_snippets | join("\n") }}</pre>{% endif %}
{% if v.fix_snippet %}<p class="muted">Suggested fix</p><pre>{{ v.fix_snippet }}</pre>{% endif %}</section>{% endfor %}
{% if not violations %}<section><p>No violations found for {{ standard_label }}.</p></section>{% endif %}
</main></body></html>"""


def render_report(context: dict[str, Any]) -> str:
    import jinja2

    env = jinja2.Environment(autoescape=True, trim_blocks=True, lstrip_blocks=True)
    return env.from_string(REPORT_TEMPLATE).render(**context)


# ============================================================================ the agent


@register
class WebAccessibilityAgent(BaseAgent):
    spec = AgentSpec(
        id="web_accessibility_evaluation",
        name="Web Accessibility Evaluation",
        category=Category.WEB,
        summary="Runs axe-core in a real browser (static fallback without one), maps every violation to its WCAG success criterion and "
                "level, scores the page and gates CI with JUnit output; the model only writes fix snippets and explanations.",
        kind=AgentKind.HYBRID,
        tier=Tier.BALANCED,
        connectors=["crawler"],
        skills=["wcag_criteria"],
        keywords=["accessibility", "a11y", "wcag", "ada", "eaa", "axe", "screen reader", "color contrast", "alt text", "vpat"],
        produces=["a11y_report.html", "a11y_junit.xml", "axe_results.json"],
        feeds=["experiment_ideation", "variation_development"],
    )
    Input = A11yInput
    Output = A11yOutput
    schemas = {"remediate": RemediationSet}

    def selftest_input(self) -> dict[str, Any]:
        return {"url": "https://www.meridian-mutual.example/", "standard": "wcag21aa", "fail_on": "serious"}

    def stages(self):
        return [("scan", self.scan), ("map", self.map), ("remediate", self.remediate), ("ci_gate", self.ci_gate), ("report", self.report)]

    # -- 1. scan ---------------------------------------------------------------------------------
    def scan(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp = state["inputs"]
        skill = ctx.skill("wcag_criteria")
        std = skill["standards"][inp.get("standard", "wcag21aa")]
        tags = list(std["tags"]) + (["best-practice"] if inp.get("include_best_practices", True) else [])
        url, html = inp.get("url"), inp.get("html")
        warnings, degraded = [], []
        crawler = ctx.connector("crawler")
        raw, engine = None, ""
        want_axe = inp.get("engine", "auto") in ("auto", "axe")
        if want_axe and crawler.browser_available():
            try:
                raw = self._run_axe(crawler, url, html, tags, int(inp.get("viewport_width", 1280)), int(inp.get("viewport_height", 900)))
                engine = f"axe-core {raw.get('testEngine', {}).get('version', AXE_VERSION)} (Playwright/Chromium)"
            except Exception as exc:
                warnings.append(f"Browser scan failed ({type(exc).__name__}: {str(exc)[:160]}); used static checks instead.")
                degraded.append("browser unavailable: static fallback checks (no CSS/JS evaluation)")
        elif want_axe:
            warnings.append("Playwright/Chromium not available: used static checks (no CSS, JavaScript or computed-contrast evaluation).")
            degraded.append("browser unavailable: static fallback checks (no CSS/JS evaluation)")
        if raw is None:
            if html is None:
                page = crawler.fetch(url)
                if not page.ok:
                    warnings.append(f"Could not fetch {url}: {page.error or page.status}")
                html = page.html or ""
            raw = static_scan(html, set(tags))
            engine = "static fallback checks (caf-static 1.0)"
            warnings.append(f"Static checks cover {len(STATIC_RULES)} rules (axe-core runs ~90): the score is not comparable with an axe-based score.")
            if inp.get("engine") == "static":
                degraded.append("static checks requested: CSS-dependent issues (contrast, focus, reflow) not fully evaluated")
        return {"data": {"raw": raw, "engine": engine, "standard_label": std["label"], "skill": skill},
                "warnings": warnings, "provenance": degraded_update(state, degraded)}

    @staticmethod
    def _run_axe(crawler: Any, url: str | None, html: str | None, tags: list[str], width: int, height: int) -> dict[str, Any]:
        source = (AXE_DIR / "axe.min.js").read_text(encoding="utf-8")
        options = {"runOnly": {"type": "tag", "values": tags}, "resultTypes": ["violations", "incomplete"]}
        script = """async (opts) => {
            const r = await axe.run(document, opts);
            return {violations: r.violations, testEngine: r.testEngine, url: r.url,
                    passes: r.passes.map(p => ({id: p.id, nodes: p.nodes.length})),
                    incomplete: r.incomplete.map(p => ({id: p.id, impact: p.impact, nodes: p.nodes.length, help: p.help})),
                    inapplicable: r.inapplicable.length};
        }"""

        def run(page: Any) -> dict[str, Any]:
            page.add_script_tag(content=source)
            return page.evaluate(script, options)

        if html is not None:
            return crawler.with_page(run, html=html, base_url=url, width=width, height=height)
        return crawler.with_page(run, url=url, width=width, height=height)

    # -- 2. map ----------------------------------------------------------------------------------
    def map(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        raw, skill = data["raw"], data["skill"]
        fail_on = inp.get("fail_on", "serious")
        violations = normalise_violations(raw.get("violations", []), skill.get("criteria", {}), fail_on)
        fixes = skill.get("rule_fixes", {})
        brand = ctx.brand.get("name", "")
        for v in violations:
            f = fixes.get(v["id"]) or fixes.get("_default", {})
            v["explanation"] = f.get("fix", "") or v["help"]
            v["fix_snippet"] = specific_fix(v, brand) or (f.get("snippet") or "").strip()
        passes = [p["id"] for p in raw.get("passes", [])]
        incomplete = raw.get("incomplete", [])
        checks: list[Check] = []
        for v in violations:
            ref = "; ".join(f"WCAG {w['sc']} ({w['level']})" for w in v["wcag"]) or "axe best practice"
            checks.append(Check(rule=v["id"], status="fail" if v["blocking"] else "warn",
                                severity={"critical": "critical", "serious": "high", "moderate": "medium", "minor": "low"}[v["impact"]],
                                message=f"{v['help']} ({v['nodes_count']} element(s))", ref=ref, target=v["targets"][0] if v["targets"] else inp.get("url"),
                                data={"impact": v["impact"], "nodes": v["nodes_count"]}))
        checks.append(Check(rule="axe.passes", status="pass", severity="low", message=f"{len(passes)} rule(s) passed"))
        if incomplete:
            checks.append(Check(rule="axe.incomplete", status="info", severity="medium",
                                message=f"{len(incomplete)} rule(s) need manual review: " + ", ".join(i["id"] for i in incomplete[:8])))
        return {"data": {"violations": violations, "passes": passes, "incomplete": incomplete}, "checks": checks}

    # -- 3. remediate (model) --------------------------------------------------------------------
    def remediate(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        data = state["data"]
        violations = data["violations"]
        source = "rule_library"
        if violations:
            items = [{"rule_id": v["id"], "help": v["help"], "impact": v["impact"],
                      "wcag": ", ".join(f"{w['sc']} {w['name']} ({w['level']})" for w in v["wcag"]) or "best practice",
                      "snippets": v["html_snippets"][:3], "summary": v["failure_summary"][:400], "baseline_fix": v["explanation"]}
                     for v in violations[:15]]
            result = ctx.generate("remediate", RemediationSet, url=state["inputs"].get("url") or "the supplied page", violations=items)
            if not ctx.llm.offline:  # offline placeholders are never shown: the YAML fix text stays
                by_id = {r.rule_id: r for r in result.items}
                for v in violations:
                    r = by_id.get(v["id"])
                    if r is None:
                        continue
                    if r.explanation.strip():
                        v["explanation"] = r.explanation.strip()
                    if r.fix_snippet.strip():
                        v["fix_snippet"] = r.fix_snippet.strip()
                    source = "model"
        return {"data": {"violations": violations, "remediation_source": source}}

    # -- 4. CI gate ------------------------------------------------------------------------------
    def ci_gate(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        fail_on = inp.get("fail_on", "serious")
        ok, blocking = ci_gate(data["violations"], fail_on)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        xml = junit_xml(inp.get("url") or "pasted-html", data["standard_label"], data["engine"], data["violations"], data["passes"], fail_on, ts)
        art = ctx.artifacts.write_text("a11y_junit.xml", xml, "application/xml", f"JUnit results (fail_on={fail_on}) for CI pipelines")
        check = Check(rule="ci.gate", status="pass" if ok else "fail", severity="high",
                      message=f"CI gate {'passed' if ok else 'failed'}: {len(blocking)} violation type(s) at or above '{fail_on}'.")
        return {"data": {"ci_pass": ok, "blocking": len(blocking), "junit": art.path}, "artifacts": [art], "checks": [check]}

    # -- 5. report -------------------------------------------------------------------------------
    def report(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        skill = data["skill"]
        violations = data["violations"]
        score = a11y_score(violations, len(data["passes"]), skill.get("scoring", {}))
        counts = {k: sum(1 for v in violations if v["impact"] == k) for k in reversed(IMPACT_ORDER)}
        sev = {"critical": "critical", "serious": "high", "moderate": "medium", "minor": "low"}
        findings = [Finding(title=f"{v['help']} ({v['nodes_count']} element{'s' if v['nodes_count'] != 1 else ''})",
                            detail=v["explanation"], severity=sev[v["impact"]], category="accessibility",
                            evidence="; ".join(v["targets"][:3]) + (f" - WCAG {', '.join(w['sc'] + ' ' + w['level'] for w in v['wcag'])}" if v["wcag"] else " - best practice"),
                            recommendation=v["fix_snippet"][:500] or v["explanation"], target=v["targets"][0] if v["targets"] else inp.get("url"),
                            source_agent=self.spec.id) for v in violations]
        out = A11yOutput(url=inp.get("url"), standard=inp.get("standard", "wcag21aa"), standard_label=data["standard_label"], engine=data["engine"],
                         viewport=f"{inp.get('viewport_width', 1280)}x{inp.get('viewport_height', 900)}",
                         violations=[Violation(**{k: v[k] for k in Violation.model_fields}) for v in violations],
                         passes_count=len(data["passes"]), incomplete_count=len(data["incomplete"]),
                         inapplicable_count=int(data["raw"].get("inapplicable", 0) or 0), impact_counts=counts, score=score,
                         fail_on=inp.get("fail_on", "serious"), ci_pass=data["ci_pass"], blocking_count=data["blocking"], junit_artifact=data["junit"],
                         remediation_source=data["remediation_source"], findings=findings)
        html = render_report({**out.model_dump(), "url": inp.get("url") or "pasted HTML", "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                              "nodes": sum(v.nodes_count for v in out.violations),
                              "remediation_label": "fix snippets written by a language model from the failing markup" if out.remediation_source == "model"
                              else "deterministic fixes from the WCAG rule library (no model configured)"})
        raw_dump = {"engine": data["engine"], "violations": data["raw"].get("violations", []), "passes": data["raw"].get("passes", []),
                    "incomplete": data["raw"].get("incomplete", [])}
        artifacts = [ctx.artifacts.write_text("a11y_report.html", html, "text/html", "Accessibility report with WCAG mapping and fixes"),
                     ctx.artifacts.write_text("axe_results.json", json.dumps(raw_dump, indent=1, default=str)[:4_000_000], "application/json", "Raw engine results")]
        summary = (f"{out.standard_label}: {len(violations)} violated rule(s) on {sum(v.nodes_count for v in out.violations)} element(s) "
                   f"({counts['critical']} critical, {counts['serious']} serious), score {score:.0f}/100, CI gate {'PASS' if out.ci_pass else 'FAIL'} "
                   f"(fail_on={out.fail_on}) via {'axe-core' if 'axe' in out.engine else 'static checks'}")
        return {"output": out.model_dump(mode="json"), "artifacts": artifacts, "summary": summary}
