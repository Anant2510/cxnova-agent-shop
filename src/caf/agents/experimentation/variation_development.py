"""Variation Development - natural-language page changes as standard, vendor-neutral web artefacts.

The most vendor-locked Opal agent (Optimizely Visual Editor + exp_ve_* tools) is replaced by:
  1. a DECLARATIVE DOM patch (set_text, set_html, set_attr, remove_attr, add_class, remove_class,
     set_style, insert_before, insert_after, prepend, append, hide, show, remove, move - each with a
     selector) plus CSS, written by the model or, offline, by a deterministic request parser;
  2. code that validates every operation and selector (bs4/soupsieve statically, Playwright live),
     sanitises HTML/CSS and compiles the operations to safe, escaped JavaScript - the model never
     writes executable code;
  3. execution in Playwright against the live (or fixture) page with before/after screenshots and a
     pixel diff (Pillow ImageChops: changed ratio, bounding box, diff image);
  4. brand consistency from the page's own computed styles and CSS custom properties;
  5. delivery through the FeatureFlag connector as an approval-gated PendingAction, so the same
     bundle runs on any testing tool, an edge worker or none.
"""

from __future__ import annotations

import colorsys
import io
import json
import re
from typing import Any, Literal
from urllib.parse import urljoin, urlparse

from pydantic import AliasChoices, BaseModel, Field, model_validator

from ...core import AgentKind, AgentSpec, BaseAgent, Category, Check, PendingAction, RunContext, Tier, register
from ...core.textutil import canonical, normalize_ws, slugify, tokens, truncate
from .experiment_ideation import css_ident, unique_selector

DEMO_URL = "https://www.meridian-mutual.example/products/home-insurance.html"

OP_NAMES = ("set_text", "set_html", "set_attr", "remove_attr", "add_class", "remove_class", "set_style", "insert_before",
            "insert_after", "prepend", "append", "hide", "show", "remove", "move")
OpName = Literal["set_text", "set_html", "set_attr", "remove_attr", "add_class", "remove_class", "set_style", "insert_before",
                 "insert_after", "prepend", "append", "hide", "show", "remove", "move"]
HTML_OPS = {"set_html", "insert_before", "insert_after", "prepend", "append"}
_INSERT_POS = {"insert_before": "beforebegin", "insert_after": "afterend", "prepend": "afterbegin", "append": "beforeend"}

# ============================================================================ contract


class StyleDecl(BaseModel):
    property: str = Field(description="CSS property, e.g. font-size")
    value: str = Field(description="CSS value, e.g. 19px or var(--sun)")


class PatchOp(BaseModel):
    op: OpName
    selector: str = Field(description="CSS selector of the element to change (must exist on the page)")
    value: str = Field(default="", description="New text (set_text), HTML (set_html/insert_*/prepend/append), attribute value (set_attr) or class names (add_class/remove_class)")
    name: str = Field(default="", description="Attribute name for set_attr / remove_attr")
    styles: list[StyleDecl] = Field(default_factory=list, description="Declarations for set_style")
    target: str = Field(default="", description="Reference element selector for move")
    position: Literal["before", "after", "prepend", "append"] = Field(default="after", description="Where to move the element relative to target")
    all_matches: bool = Field(default=False, description="Apply to every matching element instead of the first")
    reason: str = Field(default="", description="Why this operation implements the request")


class PatchPlan(BaseModel):
    summary: str = Field(description="One sentence describing the variation")
    operations: list[PatchOp] = Field(min_length=1)
    css: str = Field(default="", description="Optional extra CSS rules (no @import, no scripts)")


class VariationInput(BaseModel):
    url: str | None = Field(default=None, description="Page to change (live or the offline demo site)")
    html: str | None = Field(default=None, description="Page HTML (alternative to url)", json_schema_extra={"ui": "textarea"})
    change_request: str | None = Field(default=None, validation_alias=AliasChoices("change_request", "request"),
                                       description="The change in plain language (default: the upstream plan's treatment)",
                                       json_schema_extra={"ui": "textarea"})
    experiment_id: str | None = Field(default=None, description="Experiment the variation belongs to (default: upstream plan or derived)")
    variation_id: str | None = Field(default=None, description="Variation id (default: upstream plan's first treatment or 'variant_b')")
    width: int = Field(default=1280, ge=320, le=2560, description="Viewport width for validation screenshots")
    height: int = Field(default=900, ge=480, le=2000, description="Viewport height")
    deploy: bool = Field(default=True, description="Propose delivery through the feature-flag connector (held for approval)")

    @model_validator(mode="after")
    def _need_page(self) -> "VariationInput":
        if not (self.url or self.html):
            raise ValueError("Provide a page url or its html")
        return self


class AppliedOp(PatchOp):
    status: Literal["verified", "applied", "failed", "invalid"] = "applied"
    matches: int = 0
    message: str = ""


class VariationOutput(BaseModel):
    request: str
    page_url: str | None
    experiment_id: str
    variation_id: str
    summary_text: str
    parser: Literal["model", "deterministic", "none"]
    operations: list[AppliedOp]
    css: str
    js: str
    selectors: list[str]
    brand_tokens: dict[str, Any]
    validation: dict[str, Any]
    screenshots: dict[str, str] = Field(default_factory=dict)
    preview_artifact: str | None = None
    bundle_artifact: str | None = None
    deploy_proposed: bool = False


# ============================================================================ sanitising (pure)

ALLOWED_TAGS = {"a", "abbr", "b", "blockquote", "br", "button", "caption", "cite", "code", "dd", "div", "dl", "dt", "em", "figcaption",
                "figure", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "img", "li", "mark", "ol", "p", "picture", "small", "source",
                "span", "strong", "sub", "sup", "table", "tbody", "td", "th", "thead", "tr", "u", "ul", "s", "time"}
ALLOWED_ATTRS = {"class", "id", "href", "src", "srcset", "alt", "title", "role", "width", "height", "target", "rel", "type", "datetime",
                 "colspan", "rowspan", "scope", "loading", "decoding", "tabindex", "hidden", "lang", "dir"}
URL_ATTRS = {"href", "src", "srcset", "action", "formaction", "xlink:href", "poster"}
_BAD_URL = re.compile(r"^\s*(javascript|vbscript|data(?!:image/(png|gif|jpe?g|webp|svg\+xml);base64,)):", re.I)
_CSS_PROP = re.compile(r"^-{0,2}[a-zA-Z][a-zA-Z0-9-]*$")
_CLASS = re.compile(r"^-?[_a-zA-Z][_a-zA-Z0-9-]*$")
_ATTR = re.compile(r"^[a-zA-Z_][-a-zA-Z0-9_:.]*$")
_BAD_CSS_VALUE = re.compile(r"(expression\s*\(|javascript:|vbscript:|behavior\s*:|-moz-binding|</|[{};<>])", re.I)


def safe_url(value: str) -> bool:
    return not _BAD_URL.match(value or "")


def sanitize_html(markup: str, marker: str | None = None) -> tuple[str, list[str]]:
    """Whitelist sanitiser: allowed tags/attributes only, no event handlers, no script URLs. Returns (html, removed)."""
    from bs4 import BeautifulSoup, Comment

    removed: list[str] = []
    soup = BeautifulSoup(f"<div id='__caf_root'>{markup or ''}</div>", "lxml")
    root = soup.find(id="__caf_root")
    for c in root.find_all(string=lambda s: isinstance(s, Comment)):
        c.extract()
    for el in list(root.find_all(True)):
        if el.name in ("script", "style", "iframe", "object", "embed", "link", "meta", "base", "form", "input", "textarea", "select", "frame",
                       "frameset", "svg", "math", "template", "noscript"):
            removed.append(f"<{el.name}>")
            el.decompose()
            continue
        if el.name not in ALLOWED_TAGS:
            removed.append(f"<{el.name}> (unwrapped)")
            el.unwrap()
            continue
        for attr in list(el.attrs):
            val = el.attrs[attr]
            sval = " ".join(val) if isinstance(val, list) else str(val)
            ok = attr in ALLOWED_ATTRS or attr.startswith(("aria-", "data-"))
            if attr.lower().startswith("on") or attr.lower() in ("style", "srcdoc", "formaction") or not ok:
                removed.append(f"{el.name}[{attr}]")
                del el.attrs[attr]
            elif attr in URL_ATTRS and not safe_url(sval):
                removed.append(f"{el.name}[{attr}] unsafe URL")
                del el.attrs[attr]
        if el.name == "a" and el.get("target") == "_blank":
            el["rel"] = "noopener noreferrer"
    if marker:
        for child in root.find_all(True, recursive=False):
            child["data-caf-inserted"] = marker
    return "".join(str(c) for c in root.contents), removed


def sanitize_css(css: str) -> tuple[str, list[str]]:
    """Strip imports, script URLs, expressions and tag break-outs from model CSS."""
    removed: list[str] = []
    out = css or ""
    for pattern, label in ((r"@import[^;]*;?", "@import"), (r"</\s*style", "</style"), (r"expression\s*\([^)]*\)", "expression()"),
                           (r"(?:-moz-binding|behavior)\s*:[^;}]*;?", "binding/behavior"),
                           (r"url\(\s*['\"]?\s*(?:javascript|vbscript|data:(?!image/))[^)]*\)", "unsafe url()")):
        if re.search(pattern, out, flags=re.I):
            removed.append(label)
            out = re.sub(pattern, "", out, flags=re.I)
    out = out.replace("<", "")
    return out.strip(), removed


def selector_error(selector: str) -> str | None:
    import soupsieve

    if not selector or not selector.strip():
        return "empty selector"
    if len(selector) > 300:
        return "selector too long"
    try:
        soupsieve.compile(selector)
    except Exception as exc:
        return f"invalid selector ({type(exc).__name__})"
    return None


def validate_op(op: PatchOp, soup: Any | None = None) -> list[str]:
    """Static validation of one operation; returns a list of problems (empty = valid)."""
    errors: list[str] = []
    err = selector_error(op.selector)
    if err:
        return [err]
    if soup is not None:
        try:
            if not soup.select(op.selector):
                errors.append(f"selector '{op.selector}' matches nothing on the page")
        except Exception as exc:
            errors.append(f"selector '{op.selector}' failed ({type(exc).__name__})")
    if op.op == "set_text" and not op.value.strip():
        errors.append("set_text needs a value")
    if op.op in HTML_OPS and not op.value.strip():
        errors.append(f"{op.op} needs HTML in value")
    if op.op in ("set_attr", "remove_attr"):
        if not _ATTR.match(op.name or ""):
            errors.append(f"invalid attribute name '{op.name}'")
        elif op.name.lower().startswith("on") or op.name.lower() in ("style", "srcdoc"):
            errors.append(f"attribute '{op.name}' is not allowed (event handlers, inline style and srcdoc are blocked; use set_style)")
        elif op.op == "set_attr" and op.name.lower() in URL_ATTRS and not safe_url(op.value):
            errors.append(f"unsafe URL in {op.name}")
    if op.op in ("add_class", "remove_class"):
        names = op.value.split()
        if not names or any(not _CLASS.match(n) for n in names):
            errors.append(f"invalid class list '{op.value}'")
    if op.op == "set_style":
        if not op.styles:
            errors.append("set_style needs at least one declaration")
        for d in op.styles:
            if not _CSS_PROP.match(d.property or ""):
                errors.append(f"invalid CSS property '{d.property}'")
            if not d.value.strip() or _BAD_CSS_VALUE.search(d.value) or re.search(r"url\(\s*['\"]?\s*javascript", d.value, re.I):
                errors.append(f"unsafe or empty CSS value for {d.property}")
    if op.op == "move":
        e2 = selector_error(op.target)
        if e2:
            errors.append(f"move target: {e2}")
        elif soup is not None and not soup.select(op.target):
            errors.append(f"move target '{op.target}' matches nothing on the page")
    return errors


# ============================================================================ compiler (pure)


def js_str(value: Any) -> str:
    """JSON-encode for safe embedding in JavaScript (and inside an HTML <script>)."""
    s = json.dumps(value, ensure_ascii=False)
    return (s.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def _op_body(op: PatchOp, index: int) -> str:
    v = js_str
    if op.op == "set_text":
        return f"el.textContent = {v(op.value)};"
    if op.op == "set_html":
        html, _ = sanitize_html(op.value)
        return f"el.innerHTML = {v(html)};"
    if op.op in _INSERT_POS:
        html, _ = sanitize_html(op.value, marker=str(index))
        return f"el.insertAdjacentHTML({v(_INSERT_POS[op.op])}, {v(html)});"
    if op.op == "set_attr":
        return f"el.setAttribute({v(op.name)}, {v(op.value)});"
    if op.op == "remove_attr":
        return f"el.removeAttribute({v(op.name)});"
    if op.op == "add_class":
        return f"{v(op.value.split())}.forEach(function (c) {{ el.classList.add(c); }});"
    if op.op == "remove_class":
        return f"{v(op.value.split())}.forEach(function (c) {{ el.classList.remove(c); }});"
    if op.op == "set_style":
        return " ".join(f"el.style.setProperty({v(d.property.strip())}, {v(d.value.strip())}, 'important');" for d in op.styles)
    if op.op == "hide":
        return "el.style.setProperty('display', 'none', 'important'); el.setAttribute('aria-hidden', 'true');"
    if op.op == "show":
        return "el.hidden = false; el.style.removeProperty('display'); el.removeAttribute('aria-hidden');"
    if op.op == "remove":
        return "if (el.parentNode) { el.parentNode.removeChild(el); }"
    if op.op == "move":
        place = {"before": "beforebegin", "after": "afterend", "prepend": "afterbegin", "append": "beforeend"}[op.position]
        return f"var ref = document.querySelector({v(op.target)}); if (!ref) {{ return false; }} ref.insertAdjacentElement({v(place)}, el);"
    raise ValueError(f"unknown op {op.op}")


def compile_patch(ops: list[PatchOp], key: str = "caf-variation") -> str:
    """Compile validated operations to a self-contained, idempotent JavaScript mutation.

    Every model-provided string is JSON-escaped data; the only code is the fixed templates above.
    Operations whose element is not in the DOM yet are retried (SPAs) for up to 2 seconds.
    """
    lines = ["(function () {",
             "  var KEY = " + js_str(key) + ";",
             "  window.__cafApplied = window.__cafApplied || {};",
             "  if (window.__cafApplied[KEY]) { return; }",
             "  window.__cafApplied[KEY] = true;",
             "  function each(sel, all, fn) {",
             "    var els = all ? document.querySelectorAll(sel) : [document.querySelector(sel)], n = 0;",
             "    for (var i = 0; i < els.length; i++) { if (els[i] && fn(els[i]) !== false) { n++; } }",
             "    return n;",
             "  }",
             "  var ops = ["]
    for i, op in enumerate(ops):
        lines.append(f"    function () {{ return each({js_str(op.selector)}, {'true' if op.all_matches else 'false'}, function (el) {{ {_op_body(op, i)} }}); }},")
    lines += ["  ];",
              "  var done = [];",
              "  function run(attempt) {",
              "    var pending = 0;",
              "    for (var i = 0; i < ops.length; i++) {",
              "      if (done[i]) { continue; }",
              "      try { done[i] = ops[i]() > 0; } catch (e) { done[i] = true; if (window.console) { console.warn('[caf] operation ' + i + ' failed', e); } }",
              "      if (!done[i]) { pending++; }",
              "    }",
              "    if (pending && attempt < 20) { setTimeout(function () { run(attempt + 1); }, 100); }",
              "    else if (pending && window.console) { console.warn('[caf] ' + pending + ' operation(s) found no element'); }",
              "  }",
              "  run(0);",
              "})();"]
    return "\n".join(lines) + "\n"


def apply_patch_static(html: str, ops: list[PatchOp], css: str = "") -> tuple[str, list[int]]:
    """Apply operations to the HTML with bs4 (no browser). Returns (html, matches per op)."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "lxml")
    counts: list[int] = []
    for i, op in enumerate(ops):
        try:
            els = soup.select(op.selector)
        except Exception:
            counts.append(0)
            continue
        els = els if op.all_matches else els[:1]
        n = 0
        for el in els:
            n += 1
            if op.op == "set_text":
                el.clear()
                el.append(op.value)
            elif op.op == "set_html":
                el.clear()
                el.append(BeautifulSoup(sanitize_html(op.value)[0], "html.parser"))
            elif op.op in _INSERT_POS:
                frag = BeautifulSoup(sanitize_html(op.value, marker=str(i))[0], "html.parser")
                if op.op == "insert_before":
                    el.insert_before(frag)
                elif op.op == "insert_after":
                    el.insert_after(frag)
                elif op.op == "prepend":
                    el.insert(0, frag)
                else:
                    el.append(frag)
            elif op.op == "set_attr":
                el[op.name] = op.value
            elif op.op == "remove_attr":
                el.attrs.pop(op.name, None)
            elif op.op in ("add_class", "remove_class"):
                cls = list(el.get("class", []))
                for c in op.value.split():
                    if op.op == "add_class" and c not in cls:
                        cls.append(c)
                    elif op.op == "remove_class" and c in cls:
                        cls.remove(c)
                el["class"] = cls
            elif op.op == "set_style":
                existing = el.get("style", "").rstrip("; ")
                decl = "; ".join(f"{d.property}: {d.value} !important" for d in op.styles)
                el["style"] = f"{existing}; {decl}".strip("; ")
            elif op.op == "hide":
                el["style"] = (el.get("style", "") + "; display: none !important").strip("; ")
                el["aria-hidden"] = "true"
            elif op.op == "show":
                el.attrs.pop("hidden", None)
            elif op.op == "remove":
                el.decompose()
            elif op.op == "move":
                ref = soup.select_one(op.target)
                if ref is None:
                    n -= 1
                    continue
                moved = el.extract()
                {"before": ref.insert_before, "after": ref.insert_after, "prepend": lambda x: ref.insert(0, x), "append": ref.append}[op.position](moved)
        counts.append(n)
    if css and soup.head is not None:
        tag = soup.new_tag("style")
        tag["data-caf-variation"] = "1"
        tag.string = css
        soup.head.append(tag)
    return str(soup), counts


# ============================================================================ brand tokens + colours (pure)

BRAND_JS = """(targets) => {
  const pick = (el, label) => { if (!el) { return null; } const cs = getComputedStyle(el);
    return {label: label, color: cs.color, background: cs.backgroundColor, font_family: cs.fontFamily, font_size: cs.fontSize,
            font_weight: cs.fontWeight, line_height: cs.lineHeight, border_radius: cs.borderRadius,
            padding: [cs.paddingTop, cs.paddingRight, cs.paddingBottom, cs.paddingLeft].join(' '), box_shadow: cs.boxShadow,
            letter_spacing: cs.letterSpacing, text_transform: cs.textTransform}; };
  const root = getComputedStyle(document.documentElement); const vars = {};
  for (const sheet of Array.from(document.styleSheets)) { let rules = null; try { rules = sheet.cssRules; } catch (e) { continue; }
    for (const r of Array.from(rules || [])) { if (r.selectorText && /(^|,)\\s*(:root|html)\\s*(,|$)/.test(r.selectorText)) {
      for (const name of Array.from(r.style)) { if (name.startsWith('--')) { vars[name] = root.getPropertyValue(name).trim(); } } } } }
  const out = {custom_properties: vars, body: pick(document.body, 'body'), h1: pick(document.querySelector('h1'), 'h1'),
    h2: pick(document.querySelector('h2'), 'h2'), link: pick(document.querySelector('main a:not(.btn)') || document.querySelector('a'), 'link'),
    primary_button: pick(document.querySelector('.btn-primary, [class*="btn-primary"], button[type="submit"], .btn, button'), 'primary button'),
    buttons: Array.from(document.querySelectorAll('.btn, button, [class*="cta"]')).slice(0, 6).map(el => pick(el, el.className || el.tagName)),
    targets: {}};
  for (const sel of targets) { try { const el = document.querySelector(sel); if (el) { out.targets[sel] = pick(el, sel); } } catch (e) {} }
  return out;
}"""

NAMED_COLOURS = {"black": (0, 0, 0), "white": (255, 255, 255), "red": (220, 38, 38), "green": (22, 163, 74), "blue": (37, 99, 235),
                 "yellow": (234, 179, 8), "orange": (234, 88, 12), "purple": (147, 51, 234), "pink": (219, 39, 119), "teal": (13, 148, 136),
                 "grey": (107, 114, 128), "gray": (107, 114, 128), "navy": (30, 58, 138), "gold": (202, 138, 4), "amber": (245, 158, 11)}


def parse_colour(value: str) -> tuple[int, int, int] | None:
    v = (value or "").strip().lower()
    m = re.match(r"^#([0-9a-f]{3}|[0-9a-f]{6})$", v)
    if m:
        h = m.group(1)
        h = "".join(c * 2 for c in h) if len(h) == 3 else h
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    m = re.match(r"^rgba?\(\s*(\d+)[ ,]+(\d+)[ ,]+(\d+)", v)
    if m:
        return tuple(int(x) for x in m.groups())  # type: ignore[return-value]
    return NAMED_COLOURS.get(v)


def colour_distance(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    """Redmean colour distance (cheap perceptual approximation, 0-~765)."""
    rmean = (a[0] + b[0]) / 2
    dr, dg, db = a[0] - b[0], a[1] - b[1], a[2] - b[2]
    return ((2 + rmean / 256) * dr * dr + 4 * dg * dg + (2 + (255 - rmean) / 256) * db * db) ** 0.5


def brand_palette(tokens_: dict[str, Any]) -> dict[str, tuple[int, int, int]]:
    pal: dict[str, tuple[int, int, int]] = {}
    for name, val in (tokens_.get("custom_properties") or {}).items():
        c = parse_colour(str(val))
        if c:
            pal[f"var({name})"] = c
    for key in ("body", "h1", "h2", "link", "primary_button"):
        s = tokens_.get(key) or {}
        for prop in ("color", "background"):
            c = parse_colour(str(s.get(prop, "")))
            if c and not str(s.get(prop, "")).startswith("rgba(0, 0, 0, 0)"):
                pal.setdefault(f"{key}.{prop}", c)
    return pal


def nearest_brand_colour(word_or_value: str, tokens_: dict[str, Any]) -> str | None:
    """Map a colour word/value to the closest brand custom property (keeps variations on-palette)."""
    want = parse_colour(word_or_value)
    if not want:
        return None
    best, dist = None, 1e9
    for name, c in brand_palette(tokens_).items():
        if not name.startswith("var("):
            continue
        h1, s1, _ = colorsys.rgb_to_hsv(*[x / 255 for x in want])
        h2, s2, _ = colorsys.rgb_to_hsv(*[x / 255 for x in c])
        hue = min(abs(h1 - h2), 1 - abs(h1 - h2)) * 360 if s1 > 0.15 and s2 > 0.15 else 180 * abs(s1 - s2)
        d = colour_distance(want, c) + hue * 2
        if d < dist:
            best, dist = name, d
    return best


def off_brand_colours(ops: list[PatchOp], tokens_: dict[str, Any], css: str = "", tolerance: float = 60.0) -> list[str]:
    """Colours introduced by the patch that are not within `tolerance` of any colour already on the page."""
    pal = list(brand_palette(tokens_).values())
    found: list[str] = []
    values = [d.value for op in ops if op.op == "set_style" for d in op.styles] + re.findall(r":\s*([^;{}]+)", css or "")
    for v in values:
        for raw in re.findall(r"#[0-9a-fA-F]{3,6}\b|rgba?\([^)]*\)|\b(?:" + "|".join(NAMED_COLOURS) + r")\b", v):
            c = parse_colour(raw)
            if c and pal and min(colour_distance(c, p) for p in pal) > tolerance and raw not in found:
                found.append(raw)
    return found


def static_brand_tokens(html: str, css_texts: list[str]) -> dict[str, Any]:
    """Brand tokens without a browser: CSS custom properties from :root rules."""
    props: dict[str, str] = {}
    for css in css_texts:
        clean = re.sub(r"/\*.*?\*/", "", css or "", flags=re.S)
        for m in re.finditer(r"(?:^|[};])\s*((?::root|html)(?:\s*,\s*(?::root|html))*)\s*{([^}]*)}", clean):
            for name, val in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);?", m.group(2)):
                props[name] = val.strip()
    return {"custom_properties": props, "source": "static CSS (no browser)", "targets": {}}


def static_style(el: Any, css_texts: list[str], props: dict[str, str]) -> dict[str, str]:
    """Rough cascade (source order, no specificity) of the rules matching `el` - fallback when no browser."""
    import soupsieve

    out: dict[str, str] = {}
    for css in css_texts:
        for sel, body in re.findall(r"([^{}@]+){([^}]*)}", re.sub(r"/\*.*?\*/", "", css or "", flags=re.S)):
            for single in sel.split(","):
                single = single.strip()
                if not single or ":" in single.replace("::", ":"):
                    continue
                try:
                    if soupsieve.match(single, el):
                        for p, v in re.findall(r"([\w-]+)\s*:\s*([^;]+)", body):
                            out[p.strip()] = v.strip()
                except Exception:
                    continue
    resolve = lambda v: re.sub(r"var\((--[\w-]+)\)", lambda m: props.get(m.group(1), m.group(0)), v)
    return {k: resolve(v) for k, v in out.items()}


def _px(value: str | None) -> float | None:
    m = re.search(r"(-?\d+(?:\.\d+)?)px", value or "")
    return float(m.group(1)) if m else None


def prominence_styles(computed: dict[str, Any] | None) -> list[StyleDecl]:
    """'Make it more prominent' from the element's own computed style: same palette, larger scale, more weight."""
    c = computed or {}
    styles: list[StyleDecl] = []
    fs = _px(c.get("font_size") or c.get("font-size"))
    if fs is None and c.get("font"):
        fs = _px(c["font"])
    styles.append(StyleDecl(property="font-size", value=f"{round(fs * 1.15)}px" if fs else "1.15em"))
    pad = [p for p in (_px(x) for x in str(c.get("padding", "")).split()) if p is not None]
    if len(pad) >= 2:
        top, right = pad[0], pad[1]
        styles.append(StyleDecl(property="padding", value=f"{round(top * 1.25)}px {round(right * 1.3)}px"))
    else:
        styles.append(StyleDecl(property="padding", value="0.95em 1.9em"))
    weight = c.get("font_weight") or c.get("font-weight")
    try:
        heavier = int(float(weight or 400)) < 700
    except ValueError:
        heavier = True
    if heavier:
        styles.append(StyleDecl(property="font-weight", value="700"))
    bg = parse_colour(str(c.get("background") or c.get("background-color") or ""))
    shadow = f"rgba({bg[0]}, {bg[1]}, {bg[2]}, 0.45)" if bg and bg != (255, 255, 255) else "rgba(14, 58, 74, 0.25)"
    styles.append(StyleDecl(property="box-shadow", value=f"0 8px 22px {shadow}"))
    return styles


# ============================================================================ deterministic request parser (pure)

_QUOTED = re.compile(r"[\"“”‘’']([^\"“”‘’']{1,160})[\"“”‘’']")
_PROMINENT = re.compile(r"\b(more prominent|prominent|stand out|bigger|larger|more visible|bolder|emphasi[sz]e|eye-catching|pop)\b", re.I)
_HIDE = re.compile(r"\b(hide|remove|delete|get rid of)\s+(?:the\s+)?(.+?)(?:\.|$| and )", re.I)
_MOVE = re.compile(r"\bmove\s+(?:the\s+)?(.+?)\s+(above|below|before|after|under|over)\s+(?:the\s+)?(.+?)(?:\.|$| and )", re.I)
_COLOUR = re.compile(r"\b(?:make|turn|change)\s+(?:it|the\s+.+?)\s+(?:colou?r\s+)?(?:to\s+)?(" + "|".join(NAMED_COLOURS) + r"|#[0-9a-f]{3,6})\b", re.I)
_ADD = re.compile(r"\badd\s+(?:a\s+|an\s+)?(?:line|note|text|message|badge|label|sentence)?\s*(?:saying\s+)?[\"“'‘](.+?)[\"”'’]\s+(above|below|under|after|before|next to)\s+(?:the\s+)?(.+?)(?:\.|$| and )", re.I)
_ROLE_WORDS = {"button": {"button", "btn", "cta"}, "cta": {"cta", "button", "btn"}, "link": {"a", "link"}, "headline": {"h1"},
               "heading": {"h1", "h2"}, "title": {"h1"}, "image": {"img", "image"}, "photo": {"img", "image"}, "banner": {"promo", "band", "banner"},
               "hero": {"hero"}, "header": {"header", "nav"}, "nav": {"nav"}, "footer": {"footer"}, "tabs": {"tab"}, "tab": {"tab"},
               "table": {"table"}, "faq": {"faq"}, "form": {"form"}}


def _el_tokens(el: Any, selector: str) -> set[str]:
    toks = {el.name}
    for v in [el.get("id") or ""] + list(el.get("class", [])):
        toks |= set(re.split(r"[-_]", v.lower())) | {v.lower()}
    for anc in el.parents:
        if getattr(anc, "name", None) in (None, "[document]"):
            break
        for v in [anc.get("id") or ""] + list(anc.get("class", [])):
            toks |= {p for p in re.split(r"[-_]", v.lower()) if p in ("hero", "header", "footer", "nav", "promo", "pricing", "plans", "faq", "advisor")}
        if anc.name in ("header", "footer", "nav"):
            toks.add(anc.name)
    return {t for t in toks if t}


def _hint_words(text: str) -> set[str]:
    words = set(re.findall(r"[a-z]+", (text or "").lower()))
    out = set(words)
    for w in words:
        out |= _ROLE_WORDS.get(w, set())
    return out - {"the", "a", "an", "to", "and", "it", "make", "change", "text", "copy", "label", "from", "with", "more"}


def find_by_text(soup: Any, text: str, hint: str = "") -> Any | None:
    """Element whose own visible text equals `text` (canonical match), disambiguated by words in the request."""
    want = canonical(text)
    hints = _hint_words(hint)
    best, best_score = None, -1.0
    for el in soup.find_all(True):
        if el.name in ("script", "style", "head", "title", "meta", "html", "body"):
            continue
        if canonical(el.get_text(" ")) != want:
            continue
        if any(canonical(c.get_text(" ")) == want for c in el.find_all(True)):
            continue  # prefer the innermost element carrying the text
        toks = _el_tokens(el, "")
        score = len(hints & toks) * 3.0
        if el.name in ("a", "button") or "btn" in toks:
            score += 1.0 if hints & {"button", "cta", "btn", "link"} else 0.5
        if "primary" in toks:
            score += 0.5
        if score > best_score:
            best, best_score = el, score
    return best


def find_by_description(soup: Any, description: str) -> Any | None:
    """Element best matching a description such as 'hero image', 'promo band' or 'advisor button'."""
    hints = _hint_words(description)
    if not hints:
        return None
    best, best_score = None, 0.0
    for el in soup.find_all(True):
        if el.name in ("script", "style", "head", "title", "meta", "html", "body", "main"):
            continue
        toks = _el_tokens(el, "")
        own = set(tokens(el.get_text(" ")[:80])) if el.name not in ("section", "div") else set()
        score = len(hints & toks) * 2.0 + len(hints & own) * 0.5
        if el.get("id") and hints & set(re.split(r"[-_]", el["id"].lower())):
            score += 1.0
        if el.name in ("section", "div") and score:
            score -= 0.25  # prefer the specific element over its container
        if score > best_score:
            best, best_score = el, score
    return best


def parse_request(request: str, soup: Any, computed: dict[str, dict[str, Any]] | None = None,
                  tokens_: dict[str, Any] | None = None) -> tuple[list[PatchOp], list[str]]:
    """Deterministic NL -> patch for common requests (offline path and model fallback). Returns (ops, notes)."""
    ops: list[PatchOp] = []
    notes: list[str] = []
    text = request.strip()
    bare = _QUOTED.sub('""', text)  # instructions without the quoted copy (so words inside new copy are not commands)
    target_sel: str | None = None
    quoted = _QUOTED.findall(text)
    m = re.search(r"[\"“‘']([^\"“”‘’']{1,160})[\"”’']\s*(?:to|with|into|->|→)\s*[\"“‘']([^\"“”‘’']{1,160})[\"”’']", text)
    if m:
        old, new = m.group(1), m.group(2)
        el = find_by_text(soup, old, hint=text[: m.start()])
        if el is not None:
            target_sel = unique_selector(el, soup)
            ops.append(PatchOp(op="set_text", selector=target_sel, value=new, reason=f"Change '{old}' to '{new}'"))
        else:
            notes.append(f"No element with the text '{old}' was found on the page.")
    elif (len(quoted) == 1 and re.search(r"\b(change|set|rename|rewrite|update|make)\b", text, re.I) and not _ADD.search(text)
          and re.search(r"\b(to|with|into|reads?|says?)\s*[\"“‘']" + re.escape(quoted[0]), text, re.I)):
        el = find_by_description(soup, text.split(quoted[0])[0])
        if el is not None:
            target_sel = unique_selector(el, soup)
            ops.append(PatchOp(op="set_text", selector=target_sel, value=quoted[0], reason="Replace the element's text"))
    for am in _ADD.finditer(text):
        ref = find_by_description(soup, am.group(3))
        if ref is not None:
            pos = "insert_before" if am.group(2).lower() in ("above", "before") else "insert_after"
            esc = am.group(1).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            ops.append(PatchOp(op=pos, selector=unique_selector(ref, soup), value=f"<p class=\"caf-note\">{esc}</p>", reason="Add the requested text"))
    for mm in _MOVE.finditer(bare):
        a, where, b = mm.group(1), mm.group(2).lower(), mm.group(3)
        el_a, el_b = find_by_description(soup, a), find_by_description(soup, b)
        if el_a is not None and el_b is not None and el_a is not el_b:
            ops.append(PatchOp(op="move", selector=unique_selector(el_a, soup), target=unique_selector(el_b, soup),
                               position="before" if where in ("above", "before", "over") else "after", reason=f"Move {a} {where} {b}"))
    for hm in _HIDE.finditer(bare):
        if _MOVE.search(bare) and hm.start() > 0 and "move" in bare[: hm.start()].lower():
            continue
        el = find_by_description(soup, hm.group(2))
        if el is not None:
            ops.append(PatchOp(op="hide", selector=unique_selector(el, soup), reason=f"Hide {hm.group(2)}"))
    if _PROMINENT.search(bare):
        sel = target_sel
        if sel is None:
            el = find_by_description(soup, bare)
            sel = unique_selector(el, soup) if el is not None else None
        if sel:
            ops.append(PatchOp(op="set_style", selector=sel, styles=prominence_styles((computed or {}).get(sel)),
                               reason="More prominent: larger type, more padding and weight, a shadow in the button's own colour"))
        else:
            notes.append("Could not tell which element should be more prominent.")
    cm = _COLOUR.search(bare)
    if cm:
        sel = target_sel
        if sel is None:
            el = find_by_description(soup, bare[: cm.start(1)])
            sel = unique_selector(el, soup) if el is not None else None
        if sel:
            brand = nearest_brand_colour(cm.group(1), tokens_ or {})
            value = brand or cm.group(1)
            prop = "background-color" if re.search(r"button|cta|btn|band|banner|background", bare, re.I) else "color"
            ops.append(PatchOp(op="set_style", selector=sel, styles=[StyleDecl(property=prop, value=value)],
                               reason=f"Colour '{cm.group(1)}' mapped to the brand token {brand}" if brand else "Requested colour"))
    return ops, notes


# ============================================================================ visual diff (pure, Pillow)


def _changed_rows(a: Any, b: Any, threshold: int) -> Any:
    import numpy as np

    return (np.abs(a.astype(np.int16) - b.astype(np.int16)).max(axis=2) > threshold).any(axis=1)


def changed_region(a: Any, b: Any, threshold: int = 24, a_soft: Any = None, b_soft: Any = None,
                   align_threshold: int = 60) -> tuple[int, int, int] | None:
    """Rows that really changed, compensating a vertical layout shift: (y0, y1 in before, y1 in after).

    Rows above the first difference match top-aligned (exact comparison). Rows below the edit match once the pages
    are aligned from the bottom - content merely pushed down or pulled up - so they are not counted. Shifted text can
    land on a sub-pixel offset, so the bottom alignment compares slightly blurred copies with a higher threshold.
    Removed content shows up in the before-coordinates range, inserted content in the after-coordinates range.
    """
    import numpy as np

    ha, hb = a.shape[0], b.shape[0]
    n = min(ha, hb)
    top = _changed_rows(a[:n], b[:n], threshold)
    if not top.any() and ha == hb:
        return None
    y0 = int(np.argmax(top)) if top.any() else n
    d = hb - ha
    sa, sb, thr = (a_soft, b_soft, align_threshold) if (d != 0 and a_soft is not None and b_soft is not None) else (a, b, threshold)
    if d >= 0:
        bottom = _changed_rows(sa, sb[d:d + ha], thr)  # index k: before row k <-> after row k + d
        k = int(len(bottom) - 1 - np.argmax(bottom[::-1])) if bottom.any() else y0 - 1
        y1_a, y1_b = k, k + d
    else:
        bottom = _changed_rows(sa[-d:], sb, thr)  # index k: before row k - d <-> after row k
        k = int(len(bottom) - 1 - np.argmax(bottom[::-1])) if bottom.any() else y0 - 1
        y1_a, y1_b = k - d, k
    return y0, max(y0, min(y1_a, ha - 1)), max(y0, min(y1_b, hb - 1))


def visual_diff(before_png: bytes, after_png: bytes, threshold: int = 24) -> tuple[dict[str, Any], bytes, bytes | None]:
    """Pixel diff with Pillow ImageChops: changed ratio, bounding box, a diff image and a before/after crop of the change.

    `raw_changed_ratio` compares the screenshots pixel by pixel; `changed_ratio` and `bbox` are compensated for the
    vertical layout shift the edit causes (content that merely moved is not counted). The changed area is measured in
    both screenshots so removals (content only in `before`) and insertions (only in `after`) both count.
    """
    import numpy as np
    from PIL import Image, ImageChops, ImageDraw, ImageFilter

    a = Image.open(io.BytesIO(before_png)).convert("RGB")
    b = Image.open(io.BytesIO(after_png)).convert("RGB")
    shift = b.height - a.height
    w, h = max(a.width, b.width), max(a.height, b.height)

    def pad(im: Any) -> Any:
        if im.size == (w, h):
            return im
        canvas = Image.new("RGB", (w, h), (255, 255, 255))
        canvas.paste(im, (0, 0))
        return canvas

    def strong_mask(x: Any, y: Any) -> Any:
        r, g, bl = ImageChops.difference(x, y).split()
        return ImageChops.lighter(ImageChops.lighter(r, g), bl).point(lambda v: 255 if v > threshold else 0).convert("L")

    pa, pb = pad(a), pad(b)
    raw_changed = strong_mask(pa, pb).histogram()[255]
    fa, fb = pa.crop((0, 0, w, a.height)), pb.crop((0, 0, w, b.height))
    region = changed_region(np.asarray(fa), np.asarray(fb), threshold,
                            np.asarray(fa.filter(ImageFilter.BoxBlur(2))), np.asarray(fb.filter(ImageFilter.BoxBlur(2))))
    mask = Image.new("L", (w, h), 0)
    bbox = bbox_before = None
    changed = 0
    if region:
        y0, y1a, y1b = region
        part_b = strong_mask(pa.crop((0, y0, w, y1b + 1)), pb.crop((0, y0, w, y1b + 1)))  # inserted / restyled content (after)
        part_a = strong_mask(pa.crop((0, y0, w, y1a + 1)), pb.crop((0, y0, w, y1a + 1)))  # removed / restyled content (before)
        mask.paste(part_b, (0, y0))
        changed = max(part_b.histogram()[255], part_a.histogram()[255])
        cols_b, cols_a = part_b.getbbox(), part_a.getbbox()
        bbox = [cols_b[0], y0, cols_b[2], y1b + 1] if cols_b else [0, y0, w, y1b + 1]
        bbox_before = [cols_a[0], y0, cols_a[2], y1a + 1] if cols_a else [0, y0, w, y1a + 1]
    base = Image.blend(pb, Image.new("RGB", (w, h), (255, 255, 255)), 0.65)
    overlay = Image.composite(Image.new("RGB", (w, h), (225, 29, 72)), base, mask)
    if bbox:
        ImageDraw.Draw(overlay).rectangle([bbox[0] - 4, bbox[1] - 4, bbox[2] + 4, max(bbox[3], bbox[1] + 2) + 4], outline=(225, 29, 72), width=3)
    out = io.BytesIO()
    overlay.save(out, "PNG", optimize=True)
    crop_png = None
    if bbox and bbox_before:
        m = 40
        x0 = max(0, min(bbox[0], bbox_before[0]) - m)
        x1 = min(w, max(bbox[2], bbox_before[2]) + m)
        ca = a.crop((x0, max(0, bbox_before[1] - m), min(x1, a.width), min(a.height, bbox_before[3] + m)))
        cb = b.crop((x0, max(0, bbox[1] - m), min(x1, b.width), min(b.height, bbox[3] + m)))
        ch = min(max(ca.height, cb.height), 900)
        sheet = Image.new("RGB", (ca.width * 2 + 30, ch + 34), (255, 255, 255))
        sheet.paste(ca.crop((0, 0, ca.width, min(ca.height, ch))), (0, 34))
        sheet.paste(cb.crop((0, 0, cb.width, min(cb.height, ch))), (ca.width + 30, 34))
        dr = ImageDraw.Draw(sheet)
        dr.text((4, 8), "Before (control)", fill=(20, 20, 20))
        dr.text((ca.width + 34, 8), "After (variation)", fill=(20, 20, 20))
        buf = io.BytesIO()
        sheet.save(buf, "PNG", optimize=True)
        crop_png = buf.getvalue()
    stats = {"changed_pixels": int(changed), "total_pixels": int(w * h), "changed_ratio": round(changed / max(w * h, 1), 6), "bbox": bbox,
             "bbox_before": bbox_before, "raw_changed_ratio": round(raw_changed / max(w * h, 1), 6), "layout_shift_px": int(shift),
             "size_before": [a.width, a.height], "size_after": [b.width, b.height], "threshold": threshold}
    return stats, out.getvalue(), crop_png


VERIFY_JS = """(ops) => ops.map((op, i) => {
  let els = []; try { els = Array.from(document.querySelectorAll(op.selector)); } catch (e) { return {i: i, ok: false, matches: 0, msg: 'selector error'}; }
  if (op.op === 'remove') { return {i: i, ok: els.length < op.before_count, matches: els.length, msg: 'removed'}; }
  if (op.op === 'insert_before' || op.op === 'insert_after' || op.op === 'prepend' || op.op === 'append') {
    const n = document.querySelectorAll('[data-caf-inserted="' + i + '"]').length; return {i: i, ok: n > 0, matches: n, msg: n + ' inserted'}; }
  const el = els[0]; if (!el) { return {i: i, ok: false, matches: 0, msg: 'element not found after applying'}; }
  const cs = getComputedStyle(el);
  if (op.op === 'set_text') { const ok = el.textContent.trim() === op.value.trim(); return {i: i, ok: ok, matches: els.length, msg: ok ? 'text set' : 'text differs'}; }
  if (op.op === 'set_style') { const bad = op.styles.filter(d => el.style.getPropertyValue(d.property) === '');
    return {i: i, ok: bad.length === 0, matches: els.length, msg: bad.length ? 'not applied: ' + bad.map(d => d.property).join(', ') :
      op.styles.map(d => d.property + '=' + cs.getPropertyValue(d.property)).join('; ')}; }
  if (op.op === 'hide') { return {i: i, ok: cs.display === 'none', matches: els.length, msg: 'display ' + cs.display}; }
  if (op.op === 'show') { return {i: i, ok: cs.display !== 'none', matches: els.length, msg: 'display ' + cs.display}; }
  if (op.op === 'add_class') { const ok = op.value.split(/\\s+/).every(c => el.classList.contains(c)); return {i: i, ok: ok, matches: els.length, msg: el.className}; }
  if (op.op === 'remove_class') { const ok = op.value.split(/\\s+/).every(c => !el.classList.contains(c)); return {i: i, ok: ok, matches: els.length, msg: el.className}; }
  if (op.op === 'set_attr') { const ok = el.getAttribute(op.name) === op.value; return {i: i, ok: ok, matches: els.length, msg: op.name}; }
  if (op.op === 'remove_attr') { const ok = !el.hasAttribute(op.name); return {i: i, ok: ok, matches: els.length, msg: op.name}; }
  if (op.op === 'set_html') { return {i: i, ok: true, matches: els.length, msg: 'html set'}; }
  if (op.op === 'move') { const ref = document.querySelector(op.target); if (!ref) { return {i: i, ok: false, matches: els.length, msg: 'target missing'}; }
    const pos = ref.compareDocumentPosition(el); const before = !!(pos & Node.DOCUMENT_POSITION_PRECEDING); const inside = !!(pos & Node.DOCUMENT_POSITION_CONTAINED_BY);
    const ok = op.position === 'before' ? before && !inside : (op.position === 'after' ? !before && !inside : inside);
    return {i: i, ok: ok, matches: els.length, msg: 'moved ' + op.position}; }
  return {i: i, ok: true, matches: els.length, msg: ''};
})"""

COUNT_JS = """(sels) => sels.map(s => { try { return document.querySelectorAll(s).length; } catch (e) { return -1; } })"""


def element_inventory(soup: Any, limit: int = 70) -> list[str]:
    """'selector | tag | text' lines for the prompt so the model picks real selectors."""
    lines, seen = [], set()
    picks = soup.select("h1, h2, h3, a.btn, a[class*=cta], a[id], button, [role=button], img, section, [class*=tab], form, table, p.lead, nav a, .hero-note")
    for el in picks:
        if len(lines) >= limit:
            break
        sel = unique_selector(el, soup)
        if sel in seen:
            continue
        seen.add(sel)
        text = truncate(normalize_ws(el.get_text(" ")), 70) if el.name not in ("section", "table", "form") else ""
        alt = el.get("alt") if el.name == "img" else None
        lines.append(f"{sel} | <{el.name}> | {text or (('alt=' + repr(alt)) if alt is not None else '')}".rstrip(" |"))
    return lines


def brand_lines(tok: dict[str, Any]) -> list[str]:
    lines = [f"{k}: {v}" for k, v in list((tok.get("custom_properties") or {}).items())[:30]]
    for key in ("body", "h1", "h2", "link", "primary_button"):
        s = tok.get(key)
        if s:
            lines.append(f"{key}: color {s['color']}, background {s['background']}, font {s['font_weight']} {s['font_size']} {truncate(s['font_family'], 40)}, "
                         f"radius {s['border_radius']}, padding {s['padding']}")
    return lines


def _generate_or_none(ctx: RunContext, template: str, schema: type[BaseModel], **variables: Any) -> tuple[Any, str | None]:
    if ctx.llm.offline:
        ctx.render(template, **variables)
        return None, "no language model configured: request parsed deterministically"
    try:
        return ctx.generate(template, schema, **variables), None
    except Exception as exc:
        ctx.log("model call failed", template=template, error=repr(exc))
        return None, f"model call failed ({type(exc).__name__}); request parsed deterministically"


@register
class VariationDevelopmentAgent(BaseAgent):
    spec = AgentSpec(
        id="variation_development",
        name="Variation Development",
        category=Category.EXPERIMENTATION,
        summary="Builds an experiment variation from a plain-language request as a validated DOM patch + CSS compiled to safe JavaScript, "
                "proven in Playwright with before/after screenshots and a visual diff, and delivered through any feature-flag tool.",
        kind=AgentKind.HYBRID,
        tier=Tier.COMPLEX,
        connectors=["crawler", "flags"],
        skills=["brand_voice"],
        keywords=["variation", "variant", "visual editor", "change the button", "page change", "dom patch", "css change",
                  "a/b variation", "test variation", "preview"],
        produces=["variation.js", "variation.css", "patch.json", "preview.html", "before.png", "after.png", "diff.png", "comparison.png"],
        feeds=["experiment_review"],
        side_effects=["flags.deploy"],
    )
    Input = VariationInput
    Output = VariationOutput
    schemas = {"patch": PatchPlan, "repair": PatchPlan}

    def selftest_input(self) -> dict[str, Any]:
        return {"url": DEMO_URL, "change_request": "Change the hero button text \"Get a quote\" to \"See my price in 2 minutes\" and make it more prominent",
                "experiment_id": "exp-hero-cta-price-promise", "variation_id": "variant_b"}

    def stages(self):
        return [("load_page", self.load_page), ("brand_tokens", self.brand_tokens), ("plan_patch", self.plan_patch),
                ("compile", self.compile), ("validate_render", self.validate_render), ("deliver", self.deliver)]

    # -- stage 1 ---------------------------------------------------------------------------------
    def load_page(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp = state["inputs"]
        crawler = ctx.connector("crawler")
        html, url = inp.get("html"), inp.get("url")
        if not html:
            page = crawler.fetch(url)
            if not (page.ok and page.is_html):
                raise ValueError(f"Could not load {url} ({page.error or page.status}); pass the page html instead")
            html, url = page.html, page.final_url
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        css_texts = [s.get_text() for s in soup.find_all("style")]
        for link in soup.find_all("link", rel=lambda r: r and "stylesheet" in r):
            href = link.get("href")
            if href and url:
                css = crawler.fetch_text(urljoin(url, href))
                if css:
                    css_texts.append(css)
        plan = ctx.upstream_output("experiment_planning") or {}
        treat = next((v for v in plan.get("variants", []) if not v.get("is_control")), None)
        planned = ""
        if treat:
            planned = "; ".join(dict.fromkeys([treat.get("description", "")] + list(treat.get("changes") or []))).strip("; ")
        if not planned:
            idea = next(iter((ctx.upstream_output("experiment_ideation") or {}).get("ideas") or []), None)
            planned = (idea or {}).get("change", "")
        request = (inp.get("change_request") or "").strip() or planned
        if len(request) < 3:
            raise ValueError("Describe the change (change_request) or run Experiment Planning upstream")
        exp_id = inp.get("experiment_id") or plan.get("experiment_id") or f"exp-{slugify(urlparse(url or '').path or 'page', 30)}"
        var_id = inp.get("variation_id") or (treat or {}).get("id") or "variant_b"
        return {"data": {"html": html, "url": url, "css_texts": css_texts, "experiment_id": exp_id, "variation_id": var_id,
                         "inventory": element_inventory(soup), "request": request, "planned": planned if planned != request else ""}}

    # -- stage 2 ---------------------------------------------------------------------------------
    def brand_tokens(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        crawler = ctx.connector("crawler")
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(data["html"], "lxml")
        # candidate targets: elements named in the request (quoted text) + primary CTAs, so computed styles are known
        cands: list[str] = []
        for q in _QUOTED.findall(data["request"]):
            el = find_by_text(soup, q, hint=data["request"])
            if el is not None:
                cands.append(unique_selector(el, soup))
        el = find_by_description(soup, data["request"])
        if el is not None:
            cands.append(unique_selector(el, soup))
        cands = list(dict.fromkeys(cands))[:6]
        warnings, reasons = [], []
        tok: dict[str, Any]
        if crawler.browser_available():
            try:
                tok = self._with_page(crawler, inp, data, lambda pg: pg.evaluate(BRAND_JS, cands))
                tok["source"] = "computed styles (Playwright)"
            except Exception as exc:
                warnings.append(f"Browser style capture failed ({type(exc).__name__}); using static CSS tokens.")
                tok = static_brand_tokens(data["html"], data["css_texts"])
        else:
            tok = static_brand_tokens(data["html"], data["css_texts"])
            reasons.append("no browser: brand tokens from static CSS")
        if not tok.get("targets"):
            props = tok.get("custom_properties", {})
            for sel in cands:
                el = soup.select_one(sel)
                if el is not None:
                    st = static_style(el, data["css_texts"], props)
                    tok.setdefault("targets", {})[sel] = {"font_size": st.get("font-size") or (re.search(r"\d+px", st.get("font", "")) or [None])[0],
                                                          "padding": st.get("padding", ""), "font_weight": st.get("font-weight"),
                                                          "background": st.get("background") or st.get("background-color", "")}
        out = {"data": {"brand": tok, "candidates": cands}, "warnings": warnings}
        if reasons:
            out["provenance"] = {"degraded_reasons": reasons}
        return out

    def _with_page(self, crawler: Any, inp: dict[str, Any], data: dict[str, Any], fn: Any) -> Any:
        kw = {"width": inp.get("width", 1280), "height": inp.get("height", 900)}
        if inp.get("html"):
            base = data["url"] if data.get("url") and crawler.is_fixture(data["url"]) else None
            return crawler.with_page(fn, html=data["html"], base_url=base, **kw)
        return crawler.with_page(fn, url=data["url"], **kw)

    # -- stage 3 ---------------------------------------------------------------------------------
    def plan_patch(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        data = state["data"]
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(data["html"], "lxml")
        brand = data["brand"]
        variables = dict(request=data["request"], planned=data.get("planned"), url=data.get("url") or "(html input)", inventory=data["inventory"],
                         brand_lines=brand_lines(brand),
                         ops=list(OP_NAMES))
        plan, reason = _generate_or_none(ctx, "patch", PatchPlan, **variables)
        warnings: list[str] = []
        parser = "model"
        css = ""
        if plan is not None:
            ops = list(plan.operations)
            css = plan.css
            problems = [(i, validate_op(o, soup)) for i, o in enumerate(ops)]
            bad = [(i, p) for i, p in problems if p]
            if bad and not ctx.llm.offline:  # one repair pass with the validator's findings
                findings = [f"operation {i} ({ops[i].op} {ops[i].selector}): {'; '.join(p)}" for i, p in bad]
                try:
                    fixed = ctx.generate("repair", PatchPlan, request=data["request"], findings=findings, plan=plan.model_dump(),
                                         inventory=data["inventory"], ops=list(OP_NAMES))
                    ops, css = list(fixed.operations), fixed.css
                except Exception as exc:
                    warnings.append(f"Repair pass failed ({type(exc).__name__}); invalid operations are dropped.")
            summary_text = plan.summary
            if not any(not validate_op(o, soup) for o in ops):  # nothing usable from the model: deterministic parser as fallback
                det_ops, notes = parse_request(data["request"], soup, computed=brand.get("targets"), tokens_=brand)
                if not det_ops and data.get("planned"):
                    det_ops, notes = parse_request(data["planned"], soup, computed=brand.get("targets"), tokens_=brand)
                if det_ops:
                    warnings.append("The model's operations failed validation; the deterministic request parser was used instead.")
                    ops, css, parser = det_ops, "", "deterministic"
                    summary_text = f"Deterministic patch for: {truncate(data['request'], 120)}"
        else:
            ops, notes = parse_request(data["request"], soup, computed=brand.get("targets"), tokens_=brand)
            if not ops and data.get("planned"):  # e.g. the run goal landed in change_request; try the planned variant
                ops, notes2 = parse_request(data["planned"], soup, computed=brand.get("targets"), tokens_=brand)
                notes += notes2
            warnings += notes
            parser = "deterministic" if ops else "none"
            summary_text = f"Deterministic patch for: {truncate(data['request'], 120)}"
            if not ops:
                warnings.append("The request could not be turned into operations without a model; configure CAF_LLM_PROVIDER or rephrase "
                                "(e.g. Change \"X\" to \"Y\", make the button more prominent, hide the promo band, move X above Y).")
        css, css_removed = sanitize_css(css)
        if css_removed:
            warnings.append(f"Removed unsafe CSS: {', '.join(css_removed)}.")
        validated, checks = [], []
        for i, op in enumerate(ops):
            problems = validate_op(op, soup)
            if op.op in HTML_OPS:
                _, removed = sanitize_html(op.value)
                if removed:
                    warnings.append(f"Operation {i}: removed unsafe markup {', '.join(sorted(set(removed)))}.")
            entry = {**op.model_dump(), "status": "invalid" if problems else "applied", "matches": len(soup.select(op.selector)) if not selector_error(op.selector) else 0,
                     "message": "; ".join(problems)}
            validated.append(entry)
            checks.append(Check(rule=f"patch.op.{i}", status="fail" if problems else "pass", severity="high", target=op.selector,
                                message=f"{op.op} {op.selector}: " + ("; ".join(problems) if problems else f"valid, matches {entry['matches']} element(s)")))
        out: dict[str, Any] = {"data": {"ops": validated, "css": css, "parser": parser, "summary_text": summary_text}, "warnings": warnings, "checks": checks}
        if reason:
            out["provenance"] = {"degraded_reasons": list(state.get("provenance", {}).get("degraded_reasons", [])) + [reason]}
        return out

    # -- stage 4 ---------------------------------------------------------------------------------
    def compile(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        data = state["data"]
        valid = [PatchOp.model_validate({k: v for k, v in o.items() if k in PatchOp.model_fields}) for o in data["ops"] if o["status"] != "invalid"]
        key = f"{data['experiment_id']}:{data['variation_id']}"
        js = compile_patch(valid, key=key) if valid else ""
        selectors = sorted({o.selector for o in valid} | {o.target for o in valid if o.op == "move" and o.target})
        static_html, counts = apply_patch_static(data["html"], valid, data["css"])
        off = off_brand_colours(valid, data["brand"], data["css"])
        checks = [Check(rule="brand.palette", status="warn" if off else "pass", severity="medium",
                        message=(f"Colours outside the page's palette: {', '.join(off)}" if off else "All colours used by the variation come from the page's own palette."))]
        return {"data": {"js": js, "selectors": selectors, "static_html": static_html, "static_counts": counts, "off_brand": off}, "checks": checks}

    # -- stage 5 ---------------------------------------------------------------------------------
    def validate_render(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        crawler = ctx.connector("crawler")
        valid_idx = [i for i, o in enumerate(data["ops"]) if o["status"] != "invalid"]
        valid_ops = [data["ops"][i] for i in valid_idx]
        artifacts, warnings, checks, reasons = [], [], [], []
        shots: dict[str, str] = {}
        validation: dict[str, Any] = {"browser_used": False, "selectors_valid": all(o["status"] != "invalid" for o in data["ops"]) and bool(data["ops"]),
                                      "off_brand_colours": data["off_brand"]}
        ops = [dict(o) for o in data["ops"]]
        if not data["js"]:
            validation.update(all_ops_verified=False, changed_ratio=0.0, bbox=None)
            return {"data": {"ops": ops, "validation": validation, "shots": shots}, "warnings": ["No valid operations to apply."]}
        if crawler.browser_available():
            def run(page: Any) -> dict[str, Any]:
                errors: list[str] = []
                page.wait_for_timeout(150)
                counts = page.evaluate(COUNT_JS, [o["selector"] for o in valid_ops])
                before = page.screenshot(full_page=True)
                page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
                page.on("console", lambda m: errors.append(f"{m.type}: {m.text}") if m.type in ("error", "warning") and ("[caf]" in m.text or m.type == "error") else None)
                if data["css"]:
                    page.add_style_tag(content=data["css"])
                page.evaluate("() => {\n" + data["js"] + "\n}")
                page.wait_for_timeout(350)
                verify = page.evaluate(VERIFY_JS, [{**o, "before_count": c} for o, c in zip(valid_ops, counts)])
                after = page.screenshot(full_page=True)
                return {"counts": counts, "verify": verify, "before": before, "after": after, "errors": errors}

            try:
                res = self._with_page(crawler, inp, data, run)
                validation["browser_used"] = True
                for o, c, v in zip([ops[i] for i in valid_idx], res["counts"], res["verify"]):
                    o["matches"] = int(c)
                    if c <= 0:
                        o["status"], o["message"] = "failed", "selector matches nothing in the rendered page"
                    else:
                        o["status"] = "verified" if v.get("ok") else "failed"
                        o["message"] = v.get("msg", "")
                stats, diff_png, crop_png = visual_diff(res["before"], res["after"])
                validation.update(stats)
                validation["console_errors"] = res["errors"][:20]
                for name, blob, desc in (("before.png", res["before"], "Control (before)"), ("after.png", res["after"], "Variation (after)"),
                                         ("diff.png", diff_png, "Pixel diff (changed pixels in red)")) + ((("comparison.png", crop_png, "Before/after crop of the changed area"),) if crop_png else ()):
                    ref = ctx.artifacts.write_bytes(name, blob, "image/png", desc)
                    artifacts.append(ref)
                    shots[name.split(".")[0]] = ref.path
                checks.append(Check(rule="render.visual_change", status="pass" if stats["changed_pixels"] > 0 else "fail", severity="high",
                                    message=f"{stats['changed_ratio']:.2%} of pixels changed, bounding box {stats['bbox']}."))
                checks.append(Check(rule="render.console", status="pass" if not res["errors"] else "warn", severity="medium",
                                    message="No script errors while applying the variation." if not res["errors"] else f"{len(res['errors'])} console message(s): {res['errors'][0][:160]}"))
            except Exception as exc:
                warnings.append(f"Playwright validation failed ({type(exc).__name__}: {truncate(str(exc), 160)}); static validation only.")
                reasons.append("browser validation failed")
        else:
            warnings.append("Playwright is not available: selectors were validated statically and the patch was applied to the HTML without screenshots.")
            reasons.append("no browser: no screenshots or visual diff")
        if not validation["browser_used"]:
            for i, c in zip(valid_idx, data["static_counts"]):
                ops[i]["matches"] = c
                ops[i]["status"] = "applied" if c > 0 else "failed"
                ops[i]["message"] = ops[i]["message"] or ("applied to the static HTML" if c > 0 else "no element matched")
            validation.update(changed_ratio=None, bbox=None)
        validation["all_ops_verified"] = all(o["status"] == "verified" for o in ops) if validation["browser_used"] else False
        validation["ops_ok"] = sum(1 for o in ops if o["status"] in ("verified", "applied"))
        validation["ops_total"] = len(ops)
        checks.append(Check(rule="patch.verified", status="pass" if validation.get("all_ops_verified") else ("warn" if validation["ops_ok"] else "fail"),
                            severity="high", message=f"{validation['ops_ok']} of {len(ops)} operation(s) applied" + (" and verified in the browser" if validation["browser_used"] else " statically")))
        out = {"data": {"ops": ops, "validation": validation, "shots": shots}, "artifacts": artifacts, "warnings": warnings, "checks": checks}
        if reasons:
            out["provenance"] = {"degraded_reasons": list(state.get("provenance", {}).get("degraded_reasons", [])) + reasons}
        return out

    # -- stage 6 ---------------------------------------------------------------------------------
    def deliver(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        from ...connectors.publishing import RUNTIME_TEMPLATE

        inp, data = state["inputs"], state["data"]
        ops = data["ops"]
        good = [o for o in ops if o["status"] in ("verified", "applied")]
        artifacts = []
        bundle_ref = preview_ref = None
        if data["js"]:
            bundle = RUNTIME_TEMPLATE.format(experiment_id=data["experiment_id"], variation_id=data["variation_id"],
                                             variation_id_js=json.dumps(data["variation_id"]), css=json.dumps(data["css"] or ""), js=data["js"])
            bundle_ref = ctx.artifacts.write_text("variation.js", bundle, "application/javascript", "Vendor-neutral variation bundle (CSS + compiled DOM patch)")
            artifacts.append(bundle_ref)
            if data["css"]:
                artifacts.append(ctx.artifacts.write_text("variation.css", data["css"], "text/css", "Variation CSS"))
            preview = data["html"]
            base = f'<base href="{data["url"]}">' if data.get("url") else ""
            inject = f"{base}<script data-caf-preview>{bundle.replace('</', '<' + chr(92) + '/')}</script>"
            preview = re.sub(r"(<head[^>]*>)", lambda m: m.group(1) + inject, preview, count=1, flags=re.I) if re.search(r"<head[^>]*>", preview, re.I) else inject + preview
            preview_ref = ctx.artifacts.write_text("preview.html", preview, "text/html", "Page with the variation applied (open in a browser)")
            artifacts.append(preview_ref)
        artifacts.append(ctx.artifacts.write_json("patch.json", {"request": data["request"], "operations": ops, "css": data["css"], "selectors": data["selectors"]},
                                                  "Declarative DOM patch"))
        actions = []
        if inp.get("deploy", True) and good and data["js"]:
            actions.append(PendingAction(id=f"flags-{slugify(data['variation_id'], 20)}", kind="flags.deploy", connector="flags", method="deploy_variation",
                                         payload={"experiment_id": data["experiment_id"], "variation_id": data["variation_id"], "css": data["css"],
                                                  "js": data["js"], "selectors": data["selectors"], "preview": True},
                                         summary=f"Deploy variation '{data['variation_id']}' of {data['experiment_id']} ({len(good)} operation(s)) as a preview bundle"))
        v = data["validation"]
        out = VariationOutput(request=data["request"], page_url=data.get("url"), experiment_id=data["experiment_id"], variation_id=data["variation_id"],
                              summary_text=data["summary_text"], parser=data["parser"], operations=[AppliedOp.model_validate(o) for o in ops],
                              css=data["css"], js=data["js"], selectors=data["selectors"], brand_tokens=data["brand"], validation=v,
                              screenshots=data["shots"], preview_artifact=preview_ref.path if preview_ref else None,
                              bundle_artifact=bundle_ref.path if bundle_ref else None, deploy_proposed=bool(actions))
        diff = f", {v['changed_ratio']:.2%} of pixels changed" if v.get("changed_ratio") is not None else ""
        if not ops:
            summary = (f"No variation built for '{truncate(data['request'], 80)}': the request could not be turned into page operations "
                       f"({'no model configured' if data['parser'] == 'none' else 'invalid operations'})")
        else:
            summary = (f"Variation '{data['variation_id']}': {len(good)}/{len(ops)} operation(s) "
                       f"{'verified in Playwright' if v.get('browser_used') else 'applied statically'}{diff}"
                       + ("; deploy proposed (needs approval)" if actions else ""))
        return {"output": out.model_dump(mode="json"), "artifacts": artifacts, "actions": actions, "summary": summary}


__all__ = ["PatchOp", "StyleDecl", "compile_patch", "validate_op", "sanitize_html", "sanitize_css", "apply_patch_static", "parse_request",
           "visual_diff", "prominence_styles", "nearest_brand_colour", "off_brand_colours", "js_str", "css_ident"]
