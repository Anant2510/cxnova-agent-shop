"""Content Translation - pluggable MT + model post-editing, with the quality assets owned in-house.

* Translation goes through the pluggable TranslationConnector (`ctx.connector("translation")`: the LLM
  provider by default, DeepL / Azure Translator / NLLB adapters via settings), then an optional model
  post-editing pass for tone and cultural adaptation (template post_edit.j2; skipped offline).
* The assets that create quality are yours: a versioned glossary + do-not-translate list
  (config/skills/translation_glossary.yaml), a TMX translation memory (exact matches reused, fuzzy
  matches >= 85% via difflib adapted or suggested; new units written to runs/_store/tm-<run>.tmx for
  review - never into the source TM) and locale formatting rules (config/skills/locale_formats.yaml).
* A deterministic QA pass checks every segment: placeholder integrity ({name}, {{var}}, %s, URLs, HTML
  tags), numbers preserved (parsed per locale), glossary and do-not-translate compliance, untranslated
  segments and length expansion.
* Optional CMS publish per locale through the neutral CmsConnector, behind the approval gate.
"""

from __future__ import annotations

import csv
import difflib
import io
import re
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from xml.sax.saxutils import escape as xml_escape

from pydantic import BaseModel, Field

from ...core import AgentKind, AgentSpec, BaseAgent, Category, Check, PendingAction, RunContext, Tier, register
from ...core.textutil import canonical, normalize_ws, slugify

Origin = Literal["tm_exact", "tm_fuzzy", "mt", "mt_post_edited"]
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
PLACEHOLDER_RX = re.compile(r"\{\{\s*[\w.]+\s*\}\}|\{[A-Za-z0-9_]+\}|%(?:\d+\$)?[sdif]|%\(\w+\)[sdif]|"
                            r"https?://[^\s<>\"')\]]+|</?[A-Za-z][^>]*>|\[\[[^\]]+\]\]")
MD_PREFIX = re.compile(r"^(\s*(?:#{1,6}\s+|[-*+•]\s+|\d+[.)]\s+|>\s*)*)")
SENT_BOUNDARY = re.compile(r"(?<=[.!?])([\"”’)]*)(\s+)(?=[\"“‘(\[{]?[A-Z0-9{%<])")
ABBREVIATIONS = {"dr", "mr", "mrs", "ms", "prof", "st", "inc", "ltd", "co", "corp", "jr", "sr", "vs", "etc", "e.g", "i.e", "no",
                 "fig", "u.s", "a.m", "p.m", "approx", "dept"}
POST_EDIT_BATCH = 25
MT_BATCH = 40
SEVERITY = {"placeholders": "fail", "numbers": "fail", "dnt": "fail", "untranslated": "fail", "max_chars": "fail",
            "glossary": "warn", "length": "warn", "offline": "warn", "mt_failed": "fail"}


class TranslationInput(BaseModel):
    text: str | None = Field(default=None, description="Content to translate (Markdown or plain text)", json_schema_extra={"ui": "textarea"})
    segments: list[str] = Field(default_factory=list, description="Pre-segmented strings (UI strings, email fields) - translated 1:1")
    source: str | None = Field(default=None, description="URL or file path to translate (ingested locally)")
    source_locale: str = Field(default="en-US", description="Source locale, e.g. en-US")
    target_locales: list[str] = Field(default_factory=lambda: ["fr-FR"], min_length=1, description="Target locales, e.g. ['fr-FR', 'de-DE']")
    tone: str | None = Field(default=None, description="Tone / register for cultural adaptation, e.g. 'warm, formal vous'")
    post_edit: bool = Field(default=True, description="Model post-editing pass for tone and cultural adaptation (skipped offline)")
    max_chars: int | None = Field(default=None, ge=1, description="Hard per-segment character limit (UI strings)")
    tm_path: str | None = Field(default=None, description="TMX translation memory (default from the glossary skill)")
    publish: bool = Field(default=False, description="Propose a CMS publish per locale (held for approval)")
    title: str | None = Field(default=None, description="Title for the CMS entry")
    slug: str | None = Field(default=None, description="Slug for the CMS entry")


class SegmentQA(BaseModel):
    passed: bool
    issues: list[str] = Field(default_factory=list)


class TranslatedSegment(BaseModel):
    id: int
    source: str
    target: str
    locale: str
    origin: Origin
    qa: SegmentQA
    tm_score: float | None = None
    tm_suggestion: str | None = None


class TranslationOutput(BaseModel):
    source_locale: str
    target_locales: list[str]
    segments: list[TranslatedSegment]
    documents: dict[str, str]
    qa_summary: dict[str, Any]
    tm_file: str | None = None
    glossary_version: str = ""


class TranslateBatch(BaseModel):
    """Machine-translation step as a template (used by the browser twin; the Python runtime goes through the
    pluggable TranslationConnector instead)."""

    translations: list[str] = Field(default_factory=list, description="One translation per numbered segment, in the same order")


class EditedSegment(BaseModel):
    id: int
    target: str


class PostEditBatch(BaseModel):
    segments: list[EditedSegment] = Field(default_factory=list)


# ----------------------------------------------------------------------------- pure helpers: locale formats

def glossary_for(gl: dict[str, Any], locale: str) -> dict[str, str]:
    """Approved terms for a target locale: exact key first, then the same language (fr-CA -> fr-FR)."""
    terms = gl.get("terms") or {}
    if locale in terms:
        return dict(terms[locale] or {})
    lang = locale.lower().split("-")[0]
    return dict(next((v for k, v in terms.items() if k.lower() == locale.lower() or k.lower().split("-")[0] == lang), None) or {})


def _date_rx(pattern: str) -> re.Pattern:
    rx = re.escape(pattern).replace("YYYY", r"(?P<y>\d{4})").replace("MM", r"(?P<m>\d{1,2})").replace("DD", r"(?P<d>\d{1,2})")
    return re.compile(rf"(?<!\d){rx}(?!\d)")


def locale_format(formats: dict[str, Any], locale: str) -> dict[str, Any]:
    locs = {k.lower(): v for k, v in (formats.get("locales") or {}).items()}
    base = dict(formats.get("defaults") or {"decimal": ".", "thousands": ",", "date": "MM/DD/YYYY", "max_expansion": 1.35})
    hit = locs.get(locale.lower()) or next((v for k, v in locs.items() if k.split("-")[0] == locale.lower().split("-")[0]), None)
    return {**base, **(hit or {}), "number_idioms": list(formats.get("number_idioms") or [])}


def _number_rx(fmt: dict[str, Any]) -> re.Pattern:
    dec, th = re.escape(fmt.get("decimal", ".")), fmt.get("thousands", ",")
    th_class = re.escape(th) + ("   " if th in (" ", " ", " ") else "")
    return re.compile(rf"(?<![\w.,])\d{{1,3}}(?:[{th_class}]\d{{3}})+(?:{dec}\d+)?(?![\w])|(?<![\w.,])\d+(?:{dec}\d+)?(?![\w])")


def parse_number(token: str, fmt: dict[str, Any]) -> float | None:
    t = token
    for sep in {fmt.get("thousands", ","), " ", " ", " "} if fmt.get("thousands") in (" ", " ", " ") else {fmt.get("thousands", ",")}:
        t = t.replace(sep, "")
    t = t.replace(fmt.get("decimal", "."), ".")
    try:
        return float(t)
    except ValueError:
        return None


def date_values(text: str, fmt: dict[str, Any]) -> list[tuple[int, int, int]]:
    """Numeric dates (y, m, d) in the locale's pattern, plus ISO dates."""
    t = PLACEHOLDER_RX.sub(" ", text or "")
    out = []
    for rx in (_date_rx(fmt.get("date", "MM/DD/YYYY")), _date_rx("YYYY-MM-DD")):
        for m in rx.finditer(t):
            out.append((int(m.group("y")), int(m.group("m")), int(m.group("d"))))
    return out


def strip_dates_and_idioms(text: str, fmt: dict[str, Any], idioms: list[str] | None = None) -> str:
    t = PLACEHOLDER_RX.sub(" ", text or "")
    for rx in (_date_rx(fmt.get("date", "MM/DD/YYYY")), _date_rx("YYYY-MM-DD")):
        t = rx.sub(" ", t)
    for pat in idioms or []:
        try:
            t = re.sub(pat, " ", t, flags=re.I)
        except re.error:
            continue
    return t


def number_values(text: str, fmt: dict[str, Any], idioms: list[str] | None = None) -> list[float]:
    """Numeric values in text, parsed with the locale's separators. Numbers inside placeholders / URLs, numeric
    dates (compared separately) and configured idioms such as 24/7 are ignored."""
    t = strip_dates_and_idioms(text, fmt, idioms)
    out = []
    for m in _number_rx(fmt).finditer(t):
        v = parse_number(m.group(0), fmt)
        if v is not None:
            out.append(round(v, 6))
    return out


def format_number(value: float, decimals: int, grouped: bool, fmt: dict[str, Any]) -> str:
    s = f"{value:,.{decimals}f}" if grouped else f"{value:.{decimals}f}"
    return s.replace(",", "\x00").replace(".", fmt.get("decimal", ".")).replace("\x00", fmt.get("thousands", ","))


def localise_numbers(target: str, source: str, src_fmt: dict[str, Any], tgt_fmt: dict[str, Any]) -> tuple[str, int]:
    """Re-format numbers the engine copied in SOURCE-locale style (e.g. '14,200' in a French target) when the
    value occurs in the source. Plain integers are left alone. Returns (text, conversions)."""
    if src_fmt.get("decimal") == tgt_fmt.get("decimal") and src_fmt.get("thousands") == tgt_fmt.get("thousands"):
        return target, 0
    src_vals = set(number_values(source, src_fmt))
    rx = _number_rx(src_fmt)
    count = 0

    def repl(m: re.Match) -> str:
        nonlocal count
        tok = m.group(0)
        if not any(sep in tok for sep in (src_fmt.get("decimal", "."), src_fmt.get("thousands", ","))):
            return tok
        v = parse_number(tok, src_fmt)
        if v is None or round(v, 6) not in src_vals:
            return tok
        decimals = len(tok.split(src_fmt.get("decimal", "."))[1]) if src_fmt.get("decimal", ".") in tok else 0
        count += 1
        return format_number(v, decimals, src_fmt.get("thousands", ",") in tok, tgt_fmt)

    protected = PLACEHOLDER_RX.split(target)
    keep = PLACEHOLDER_RX.findall(target)
    out = []
    for i, part in enumerate(protected):
        out.append(rx.sub(repl, part))
        if i < len(keep):
            out.append(keep[i])
    return "".join(out), count


def localise_dates(target: str, source: str, src_fmt: dict[str, Any], tgt_fmt: dict[str, Any]) -> tuple[str, int]:
    """Re-format numeric dates copied verbatim from the source into the target locale's date pattern."""
    sp, tp = src_fmt.get("date", "MM/DD/YYYY"), tgt_fmt.get("date", "MM/DD/YYYY")
    if sp == tp:
        return target, 0
    rx = _date_rx(sp)
    src_dates = {m.group(0) for m in rx.finditer(source or "")}
    count = 0

    def repl(m: re.Match) -> str:
        nonlocal count
        if m.group(0) not in src_dates:
            return m.group(0)
        y, mo, d = m.group("y"), int(m.group("m")), int(m.group("d"))
        if not (1 <= mo <= 12 and 1 <= d <= 31):
            return m.group(0)
        count += 1
        return tp.replace("YYYY", y).replace("MM", f"{mo:02d}").replace("DD", f"{d:02d}")

    return rx.sub(repl, target), count


# ----------------------------------------------------------------------------- pure helpers: segmentation

def _split_sentences(content: str) -> list[tuple[str, str]]:
    """[(sentence, separator_after)] - abbreviation-safe, separators preserved for exact reassembly."""
    out: list[tuple[str, str]] = []
    cur = 0
    for m in SENT_BOUNDARY.finditer(content):
        prev = re.search(r"(\S+)$", content[:m.start()])
        tok = prev.group(1).rstrip(".").lower() if prev else ""
        if tok in ABBREVIATIONS or re.fullmatch(r"[a-z]", tok):
            continue
        end = m.start() + len(m.group(1))
        out.append((content[cur:end], m.group(2)))
        cur = m.end()
    out.append((content[cur:], ""))
    return out


def translatable(text: str, dnt: list[str] | None = None) -> bool:
    t = PLACEHOLDER_RX.sub(" ", text or "")
    for term in dnt or []:
        t = re.sub(rf"\b{re.escape(term)}\b", " ", t)
    return bool(re.search(r"[^\W\d_]{2,}", t))


def segment_text(text: str, dnt: list[str] | None = None, split: bool = True, keep_whole: set[str] | None = None) -> list[dict[str, str]]:
    """Tokens [{'t': 'seg'|'raw', 'text': ...}] whose concatenation is exactly the input. Markdown prefixes,
    code blocks, table pipes, whitespace and untranslatable runs stay raw. A paragraph whose normalised text
    is in `keep_whole` (multi-sentence TM units) is kept as one segment instead of being sentence-split."""
    keep_whole = keep_whole or set()
    tokens: list[dict[str, str]] = []

    def raw(s: str) -> None:
        if s:
            if tokens and tokens[-1]["t"] == "raw":
                tokens[-1]["text"] += s
            else:
                tokens.append({"t": "raw", "text": s})

    def content(s: str) -> None:
        lead = s[: len(s) - len(s.lstrip())]
        trail = s[len(s.rstrip()):]
        core = s.strip()
        raw(lead)
        if core:
            whole = not split or normalize_ws(core) in keep_whole
            for sent, sep in ([(core, "")] if whole else _split_sentences(core)):
                if translatable(sent, dnt):
                    tokens.append({"t": "seg", "text": sent})
                else:
                    raw(sent)
                raw(sep)
        raw(trail)

    in_code = False
    for line in (text or "").splitlines(keepends=True):
        body = line.rstrip("\r\n")
        nl = line[len(body):]
        if body.strip().startswith("```"):
            in_code = not in_code
            raw(line)
            continue
        if in_code or not body.strip():
            raw(line)
            continue
        if body.lstrip().startswith("|"):
            for part in re.split(r"(\|)", body):
                if part == "|" or re.fullmatch(r"\s*:?-{3,}:?\s*", part) or not part.strip():
                    raw(part)
                else:
                    content(part)
            raw(nl)
            continue
        prefix = MD_PREFIX.match(body).group(1)
        raw(prefix)
        content(body[len(prefix):])
        raw(nl)
    return tokens


# ----------------------------------------------------------------------------- pure helpers: translation memory

def load_tmx(xml_text: str) -> tuple[str, list[dict[str, Any]]]:
    """Parse TMX 1.4 -> (srclang, [{'id', 'langs': {lang_lower: text}}])."""
    root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    header = root.find("header")
    srclang = (header.get("srclang") if header is not None else None) or "en-US"
    units = []
    for i, tu in enumerate(root.iter("tu")):
        langs: dict[str, str] = {}
        for tuv in tu.findall("tuv"):
            lang = (tuv.get(XML_LANG) or tuv.get("lang") or "").strip()
            seg = tuv.find("seg")
            if lang and seg is not None:
                langs[lang.lower()] = normalize_ws("".join(seg.itertext()))
        if len(langs) >= 2:
            units.append({"id": tu.get("tuid") or f"tu-{i + 1}", "langs": langs})
    return srclang, units


def tmx_dnt(xml_text: str) -> list[str]:
    """Do-not-translate terms declared in the TMX header (<prop type="x-do-not-translate">a; b</prop>)."""
    root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    header = root.find("header")
    out: list[str] = []
    for prop in (header.findall("prop") if header is not None else []):
        if (prop.get("type") or "").lower() in ("x-do-not-translate", "x-dnt"):
            out += [t.strip() for t in re.split(r"[;|]", prop.text or "") if t.strip()]
    return out


def _pick(langs: dict[str, str], locale: str) -> str | None:
    loc = locale.lower()
    if loc in langs:
        return langs[loc]
    base = loc.split("-")[0]
    return next((v for k, v in langs.items() if k.split("-")[0] == base), None)


def tm_pairs(units: list[dict[str, Any]], source_locale: str, target_locale: str) -> list[tuple[str, str]]:
    out = []
    for u in units:
        s, t = _pick(u["langs"], source_locale), _pick(u["langs"], target_locale)
        if s and t:
            out.append((s, t))
    return out


def tm_lookup(segment: str, pairs: list[tuple[str, str]], threshold: float) -> tuple[str, str | None, str | None, float]:
    """('exact'|'fuzzy'|'none', tm_source, tm_target, score) - exact on normalised text, fuzzy via difflib."""
    key = normalize_ws(segment)
    for s, t in pairs:
        if normalize_ws(s) == key:
            return "exact", s, t, 1.0
    a = canonical(segment)
    best = ("none", None, None, 0.0)
    for s, t in pairs:
        b = canonical(s)
        if not a or not b or min(len(a), len(b)) / max(len(a), len(b)) < threshold:
            continue
        sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
        if sm.real_quick_ratio() < threshold or sm.quick_ratio() < threshold:
            continue
        r = sm.ratio()
        if r >= threshold and r > best[3]:
            best = ("fuzzy", s, t, round(r, 4))
    return best


_TOK = re.compile(r"\{\{\s*[\w.]+\s*\}\}|\{\w+\}|%(?:\d+\$)?[sdif]|\d[\d.,  ]*\d|\d|\w+|[^\w\s]", re.U)


def adapt_fuzzy(segment: str, tm_source: str, tm_target: str, src_fmt: dict[str, Any], tgt_fmt: dict[str, Any]) -> str | None:
    """Deterministic fuzzy-match repair when the ONLY differences are numbers or placeholders
    ('Save 10% today' vs 'Save 15% today'): substitute them in the approved TM target. None otherwise."""
    a, b = _TOK.findall(segment), _TOK.findall(tm_source)
    out = tm_target
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, b, a, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        if op != "replace" or (i2 - i1) != (j2 - j1):
            return None
        for old, new in zip(b[i1:i2], a[j1:j2]):
            if PLACEHOLDER_RX.fullmatch(old) and PLACEHOLDER_RX.fullmatch(new):
                if old not in out:
                    return None
                out = out.replace(old, new, 1)
                continue
            ov, nv = parse_number(old, src_fmt), parse_number(new, src_fmt)
            if ov is None or nv is None or not re.match(r"\d", old) or not re.match(r"\d", new):
                return None
            hit = None
            for m in _number_rx(tgt_fmt).finditer(out):
                if parse_number(m.group(0), tgt_fmt) == ov:
                    hit = m
                    break
            if hit is None:
                return None
            decimals = len(new.split(src_fmt.get("decimal", "."))[1]) if src_fmt.get("decimal", ".") in new else 0
            out = out[: hit.start()] + format_number(nv, decimals, src_fmt.get("thousands", ",") in new, tgt_fmt) + out[hit.end():]
    return out


def build_tmx(units: list[dict[str, str]], srclang: str, tool: str = "Coforge CAF content_translation") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<tmx version="1.4">',
             f'  <header creationtool="{xml_escape(tool)}" creationtoolversion="1.0" datatype="plaintext" segtype="sentence" '
             f'adminlang="en-US" srclang="{xml_escape(srclang)}" o-tmf="caf" creationdate="{stamp}"/>', "  <body>"]
    for u in units:
        lines += [f'    <tu tuid="{xml_escape(u["id"])}" creationdate="{stamp}">',
                  f'      <prop type="x-origin">{xml_escape(u["origin"])}</prop>',
                  '      <prop type="x-status">unreviewed</prop>',
                  f'      <tuv xml:lang="{xml_escape(srclang)}"><seg>{xml_escape(u["source"])}</seg></tuv>',
                  f'      <tuv xml:lang="{xml_escape(u["locale"])}"><seg>{xml_escape(u["target"])}</seg></tuv>',
                  "    </tu>"]
    lines += ["  </body>", "</tmx>"]
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------------- pure helpers: QA

def placeholders(text: str) -> Counter:
    out = []
    for m in PLACEHOLDER_RX.finditer(text or ""):
        t = m.group(0)
        if t.startswith("http"):
            t = t.rstrip(".,;:!?")
        elif t.startswith("<"):
            t = re.sub(r"\s(alt|title|aria-label|placeholder)=\"[^\"]*\"", r' \1=""', t)
        else:
            t = re.sub(r"\s+", "", t)
        out.append(t)
    return Counter(out)


def _term_rx(term: str, plural: bool) -> re.Pattern:
    suffix = r"(?:s|es)?\b" if plural else ""
    return re.compile(rf"(?<!\w){re.escape(term)}{suffix}", re.I)


def fold(text: str) -> str:
    """Accent- and case-insensitive form for target-term checks (Schaden ~ Schäden, réglé ~ regle)."""
    return "".join(c for c in unicodedata.normalize("NFKD", text or "") if not unicodedata.combining(c)).lower()


def qa_segment(source: str, target: str, *, src_fmt: dict[str, Any], tgt_fmt: dict[str, Any], glossary: dict[str, str],
               dnt: list[str], max_chars: int | None = None) -> list[tuple[str, str]]:
    """Deterministic QA -> [(rule, message)] (rule keys map to SEVERITY)."""
    issues: list[tuple[str, str]] = []
    if (target or "").startswith("[offline:"):
        issues.append(("offline", "offline placeholder - no translation engine configured"))
    sp, tp = placeholders(source), placeholders(target)
    if sp != tp:
        missing, extra = list((sp - tp).elements()), list((tp - sp).elements())
        issues.append(("placeholders", "placeholders changed" + (f"; missing {missing}" if missing else "") + (f"; unexpected {extra}" if extra else "")))
    idioms = list(src_fmt.get("number_idioms") or []) + [i for i in (tgt_fmt.get("number_idioms") or []) if i not in (src_fmt.get("number_idioms") or [])]
    sd, td = Counter(date_values(source, src_fmt)), Counter(date_values(target, tgt_fmt))
    if sd != td:
        issues.append(("numbers", f"dates differ (expected {tgt_fmt.get('date')}): source {sorted(sd)} vs target {sorted(td)}"))
    sv, tv = Counter(number_values(source, src_fmt, idioms)), Counter(number_values(target, tgt_fmt, idioms))
    if sv != tv:
        missing, extra = list((sv - tv).elements()), list((tv - sv).elements())
        issues.append(("numbers", "numbers differ" + (f"; missing {[_fmt_num(x) for x in missing]}" if missing else "")
                       + (f"; unexpected {[_fmt_num(x) for x in extra]}" if extra else "")))
    for term in dnt:
        if re.search(rf"(?<!\w){re.escape(term)}(?!\w)", source or "") and term not in (target or ""):
            issues.append(("dnt", f"do-not-translate term '{term}' altered or missing"))
    folded = fold(target)
    for src_term, tgt_term in sorted(glossary.items(), key=lambda kv: -len(kv[0])):
        if _term_rx(src_term, True).search(source or "") and not _term_rx(fold(tgt_term), False).search(folded):
            issues.append(("glossary", f"'{src_term}' should be rendered as '{tgt_term}'"))
    if translatable(source, dnt) and word_list(re.sub(r"^\[offline:[^\]]+\]\s*", "", target or "")) == word_list(source):
        issues.append(("untranslated", "segment left untranslated"))
    ratio = float(tgt_fmt.get("max_expansion", 1.35))
    if len(target or "") > len(source or "") * ratio + 10:
        issues.append(("length", f"{len(target)} chars vs {len(source)} in source (max expansion {ratio:.0%})"))
    if max_chars and len(target or "") > max_chars:
        issues.append(("max_chars", f"{len(target)} chars exceeds the {max_chars}-char limit"))
    return issues


def word_list(text: str) -> list[str]:
    """Letters-only words (numbers, punctuation and placeholders ignored) - for the untranslated check."""
    return [w.lower() for w in re.findall(r"[^\W\d_]{2,}", PLACEHOLDER_RX.sub(" ", text or ""))]


def _fmt_num(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else str(v)


def qa_passed(issues: list[tuple[str, str]]) -> bool:
    return not any(SEVERITY.get(rule, "warn") == "fail" for rule, _ in issues)


def hard_issues(issues: list[tuple[str, str]]) -> int:
    return sum(1 for rule, _ in issues if SEVERITY.get(rule) == "fail")


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, canonical(a), canonical(b), autojunk=False).ratio()


def assemble_document(tokens: list[dict[str, str]], translations: list[str]) -> str:
    out, k = [], 0
    for t in tokens:
        if t["t"] == "seg":
            out.append(translations[k])
            k += 1
        else:
            out.append(t["text"])
    return "".join(out)


# ----------------------------------------------------------------------------- agent

@register
class ContentTranslationAgent(BaseAgent):
    spec = AgentSpec(
        id="content_translation",
        name="Content Translation",
        category=Category.WEB,
        summary="Translates and culturally adapts content into multiple locales through a pluggable MT engine plus model post-editing, "
                "reusing your TMX memory and glossary, with a deterministic QA pass and gated CMS publish.",
        kind=AgentKind.HYBRID,
        tier=Tier.BALANCED,
        connectors=["translation", "ingestion", "cms"],
        skills=["translation_glossary", "locale_formats", "brand_voice"],
        keywords=["translate", "translation", "localise", "localize", "localisation", "multilingual", "french", "german",
                  "spanish", "locale"],
        produces=["translated-<locale>.md", "qa_report.csv", "tm-<run>.tmx"],
        feeds=["email_creation", "web_accessibility_evaluation"],
        side_effects=["cms.publish"],
    )
    Input = TranslationInput
    Output = TranslationOutput
    schemas = {"post_edit": PostEditBatch, "translate": TranslateBatch}  # translate.j2: browser MT step (Python uses the connector)

    def selftest_input(self) -> dict[str, Any]:
        return {"text": SELFTEST_TEXT, "target_locales": ["fr-FR", "de-DE"], "tone": "warm, reassuring, plain-spoken",
                "title": "Home insurance claims - quick guide", "publish": True}

    def stages(self):
        return [("prepare", self.prepare), ("memory", self.memory), ("machine_translate", self.machine_translate),
                ("post_edit", self.post_edit), ("qa", self.qa), ("assemble", self.assemble)]

    # -- 1. prepare: source, segmentation, assets ------------------------------------------------------------
    def prepare(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp = state["inputs"]
        gl = ctx.skill("translation_glossary", default={})
        formats = ctx.skill("locale_formats", default={})
        dnt = list(gl.get("do_not_translate") or [])
        warnings: list[str] = []
        checks: list[Check] = []
        reasons: list[str] = []
        units: list[dict[str, Any]] = []
        tm_path = inp.get("tm_path") or gl.get("translation_memory")
        if tm_path:
            try:  # a caller-supplied TM goes through the security policy; the glossary's configured TM is trusted
                tm_file = ctx.local_path(tm_path) if inp.get("tm_path") else ctx.settings.resolve(tm_path)
                raw_tm = tm_file.read_text(encoding="utf-8")
                _srclang, units = load_tmx(raw_tm)
                dnt += [t for t in tmx_dnt(raw_tm) if t not in dnt]
            except FileNotFoundError:
                reasons.append(f"translation memory not found ({tm_path}): no TM leverage")
            except PermissionError as exc:
                reasons.append(f"translation memory not read ({exc}): no TM leverage")
            except ET.ParseError as exc:
                reasons.append(f"translation memory unreadable ({exc}): no TM leverage")
        else:
            reasons.append("no translation memory configured: no TM leverage")
        dnt.sort(key=len, reverse=True)
        mode = "text"
        text = inp.get("text")
        if inp.get("segments"):
            mode = "segments"
        elif not text and inp.get("source"):
            try:
                text = ctx.connector("ingestion").load(inp["source"]).text
            except Exception as exc:
                raise ValueError(f"Could not ingest the source: {exc}") from exc
        elif not text:
            email = ctx.upstream_output("email_creation", "webinar_followup_email") or {}
            first = (email.get("emails") or [None])[0] or next((s.get("email") for s in email.get("segments", []) if s.get("email")), None)
            if first:
                text = "\n\n".join(x for x in (first.get("subject"), first.get("preheader"), first.get("body_markdown"), first.get("cta_text")) if x)
                warnings.append("No content given - translating the upstream email.")
            else:
                text = ctx.upstream_text(max_chars=20000)
                if text:
                    warnings.append("No content given - translating the upstream agents' output.")
        if mode == "segments":
            tokens: list[dict[str, str]] = []
            for i, s in enumerate(inp["segments"]):
                if i:
                    tokens.append({"t": "raw", "text": "\n"})
                tokens.append({"t": "seg" if translatable(s, dnt) else "raw", "text": s})
        else:
            if not (text or "").strip():
                raise ValueError("Nothing to translate: provide text, segments or source.")
            src_loc = inp.get("source_locale") or gl.get("source_locale") or "en-US"
            whole = {normalize_ws(_pick(u["langs"], src_loc) or "") for u in units} - {""}
            tokens = segment_text(text, dnt, keep_whole={w for w in whole if len(_split_sentences(w)) > 1})
        segs = [t["text"] for t in tokens if t["t"] == "seg"]
        if not segs:
            raise ValueError("No translatable segments found (only placeholders, numbers or do-not-translate terms).")
        src = inp.get("source_locale") or gl.get("source_locale") or "en-US"
        locales = [l for l in dict.fromkeys(inp["target_locales"]) if l.lower() != src.lower()]
        if len(locales) != len(inp["target_locales"]):
            warnings.append("Target locales equal to the source locale or duplicated were dropped.")
        if not locales:
            raise ValueError("No target locale differs from the source locale.")
        known = {k.lower() for k in (formats.get("locales") or {})}
        for loc in locales:
            if loc.lower() not in known:
                checks.append(Check(rule="locale.formats", status="warn", severity="low", target=loc,
                                    message=f"No formatting rules for {loc} in locale_formats.yaml; language/default rules used."))
            if not glossary_for(gl, loc):
                checks.append(Check(rule="glossary.coverage", status="warn", severity="medium", target=loc,
                                    message=f"No glossary for {loc}; terminology is not enforced for this locale."))
        checks.append(Check(rule="segmentation", status="info", severity="low",
                            message=f"{len(segs)} translatable segment(s) ({mode} mode) into {', '.join(locales)}."))
        upd: dict[str, Any] = {"data": {"tokens": tokens, "segments": segs, "mode": mode, "source_locale": src, "locales": locales,
                                        "glossary": gl, "formats": formats, "dnt": dnt, "tm_units": units},
                               "checks": checks, "warnings": warnings + [r[0].upper() + r[1:] + "." for r in reasons]}
        if reasons:
            upd["provenance"] = {"degraded_reasons": reasons}
        return upd

    # -- 2. translation memory: exact reuse, fuzzy adaptation / suggestion ------------------------------------
    def memory(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        gl = data["glossary"]
        threshold = float(gl.get("fuzzy_threshold", 0.85))
        units = data.get("tm_units") or []
        checks: list[Check] = []
        src, results = data["source_locale"], {}
        src_fmt = locale_format(data["formats"], src)
        for loc in data["locales"]:
            pairs = tm_pairs(units, src, loc)
            tgt_fmt = locale_format(data["formats"], loc)
            rows = []
            for i, seg in enumerate(data["segments"]):
                kind, tm_s, tm_t, score = tm_lookup(seg, pairs, threshold) if pairs else ("none", None, None, 0.0)
                row = {"id": i, "source": seg, "target": None, "origin": None, "tm_score": score or None, "tm_suggestion": None,
                       "tm_source": None}
                if kind == "exact":
                    row.update(target=tm_t, origin="tm_exact")
                elif kind == "fuzzy":
                    adapted = adapt_fuzzy(seg, tm_s, tm_t, src_fmt, tgt_fmt)
                    row.update(tm_suggestion=tm_t, tm_source=tm_s)
                    if adapted is not None:
                        row.update(target=adapted, origin="tm_fuzzy")
                rows.append(row)
            results[loc] = rows
            n = len(rows)
            ex = sum(1 for r in rows if r["origin"] == "tm_exact")
            fz = sum(1 for r in rows if r["tm_suggestion"])
            checks.append(Check(rule="tm.leverage", status="info", severity="low", target=loc,
                                message=f"{loc}: {ex} exact and {fz} fuzzy (>= {threshold:.0%}) TM match(es) out of {n} segment(s) "
                                        f"from {len(pairs)} TM unit(s); leverage {(ex + fz) / max(1, n):.0%}.",
                                data={"exact": ex, "fuzzy": fz, "segments": n, "tm_units": len(pairs)}))
        return {"data": {"rows": results}, "checks": checks}

    # -- 3. machine translation through the pluggable connector -----------------------------------------------
    def _translator(self, ctx: RunContext) -> tuple[Any, str | None]:
        """The configured engine when it is usable; otherwise the LLM provider, with the reason recorded."""
        configured = ctx.connectors.provider_for("translation")
        try:
            engine = ctx.connector("translation")
            if getattr(engine, "available", True):
                return engine, None
            missing = [k for k in getattr(engine, "required", ()) if not engine.config.get(k)]
            note = f"translation provider '{configured}' is not configured (missing {missing}); used the llm provider"
        except Exception as exc:
            note = f"translation provider '{configured}' unavailable ({exc}); used the llm provider"
        return ctx.connector("translation", provider="llm"), note

    def machine_translate(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        gl = data["glossary"]
        rows_all = {loc: [dict(r) for r in rows] for loc, rows in data["rows"].items()}
        todo_by_loc = {loc: [r for r in rows if r["target"] is None] for loc, rows in rows_all.items()}  # not served by the TM
        if not any(todo_by_loc.values()):
            return {"data": {"rows": rows_all, "engine": "tm_only"}}
        engine, note = self._translator(ctx)
        warnings: list[str] = [note] if note else []
        notes: list[str] = [note] if note else []

        def run(eng: Any, texts: list[str], loc: str, glossary: dict[str, str]) -> list[str]:
            outs = eng.translate(texts, source=data["source_locale"], target=loc, glossary=glossary, dnt=data["dnt"], tone=inp.get("tone"))
            if len(outs) != len(texts):
                raise ValueError(f"engine returned {len(outs)} translations for {len(texts)} segments")
            return outs

        for loc, todo in todo_by_loc.items():
            glossary = glossary_for(gl, loc)
            for b in range(0, len(todo), MT_BATCH):
                batch = todo[b: b + MT_BATCH]
                texts = [r["source"] for r in batch]
                try:
                    outs: list[str | None] = list(run(engine, texts, loc, glossary))
                except Exception as exc:
                    if getattr(engine, "provider", "llm") != "llm":  # external engine failed: switch to the LLM provider
                        msg = f"translation provider '{engine.provider}' failed ({type(exc).__name__}: {exc}); switched to the llm provider"
                        warnings.append(msg)
                        notes.append(msg)
                        engine = ctx.connector("translation", provider="llm")
                    try:
                        outs = list(run(engine, texts, loc, glossary))
                    except Exception as exc2:
                        warnings.append(f"{loc}: batch translation failed ({type(exc2).__name__}: {exc2}); retrying segment by segment.")
                        outs = []
                        for t in texts:
                            try:
                                outs.append(run(engine, [t], loc, glossary)[0])
                            except Exception:
                                outs.append(None)
                for r, o in zip(batch, outs):
                    if o is None:
                        r.update(target=r["source"], origin="mt", mt_failed=True)
                    else:
                        r.update(target=o, origin="mt")
        upd: dict[str, Any] = {"data": {"rows": rows_all, "engine": getattr(engine, "provider", "llm")}, "warnings": warnings}
        reasons = list(notes)
        if getattr(engine, "provider", "") == "llm" and ctx.llm.offline:
            reasons.append("offline model: machine translation returned placeholders (segments marked [offline:<locale>])")
        if reasons:
            upd["provenance"] = {"degraded_reasons": list(state.get("provenance", {}).get("degraded_reasons", [])) + reasons}
        return upd

    # -- 4. model post-editing (tone / cultural adaptation; fuzzy TM adaptation) ------------------------------
    def post_edit(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        rows_all = {loc: [dict(r) for r in rows] for loc, rows in data["rows"].items()}
        if not inp.get("post_edit", True) or ctx.llm.offline:
            reason = "offline model: no post-editing pass" if ctx.llm.offline else None
            upd: dict[str, Any] = {"data": {"rows": rows_all}}
            if reason:
                upd["provenance"] = {"degraded_reasons": list(state.get("provenance", {}).get("degraded_reasons", [])) + [reason]}
            return upd
        gl = data["glossary"]
        checks: list[Check] = []
        warnings: list[str] = []
        src_fmt = locale_format(data["formats"], data["source_locale"])
        for loc, rows in rows_all.items():
            tgt_fmt = locale_format(data["formats"], loc)
            glossary = glossary_for(gl, loc)
            todo = [r for r in rows if r["origin"] == "mt" and not r.get("mt_failed")]
            reverted = 0
            for b in range(0, len(todo), POST_EDIT_BATCH):
                batch = todo[b: b + POST_EDIT_BATCH]
                items = [{"id": r["id"], "source": r["source"], "draft": r["target"],
                          "tm_source": r.get("tm_source"), "tm_target": r.get("tm_suggestion")} for r in batch]
                try:
                    res = ctx.generate("post_edit", PostEditBatch, items=items, source_locale=data["source_locale"], target_locale=loc,
                                       tone=inp.get("tone"), glossary=[f"{k} => {v}" for k, v in glossary.items()], dnt=data["dnt"],
                                       decimal=tgt_fmt.get("decimal"), thousands=repr(tgt_fmt.get("thousands")), date=tgt_fmt.get("date"))
                    edited = {e.id: e.target for e in res.segments}
                except Exception as exc:
                    warnings.append(f"{loc}: post-editing failed ({type(exc).__name__}); machine translation kept.")
                    edited = {}
                for r in batch:
                    new = normalize_ws(edited.get(r["id"]) or "")
                    if not new:
                        continue
                    before = qa_segment(r["source"], r["target"], src_fmt=src_fmt, tgt_fmt=tgt_fmt, glossary=glossary, dnt=data["dnt"])
                    after = qa_segment(r["source"], new, src_fmt=src_fmt, tgt_fmt=tgt_fmt, glossary=glossary, dnt=data["dnt"])
                    if hard_issues(after) > hard_issues(before):
                        reverted += 1  # the editor broke placeholders / numbers / DNT terms: keep the MT draft
                        continue
                    origin = "mt_post_edited"
                    if r.get("tm_suggestion") and similarity(new, r["tm_suggestion"]) > similarity(new, r["target"]):
                        origin = "tm_fuzzy"  # the editor adapted the approved TM translation rather than the MT draft
                    r.update(target=new, origin=origin)
            if reverted:
                checks.append(Check(rule="post_edit.reverted", status="warn", severity="medium", target=loc,
                                    message=f"{loc}: {reverted} post-edit(s) reverted because they broke placeholders, numbers or DNT terms."))
        return {"data": {"rows": rows_all}, "checks": checks, "warnings": warnings}

    # -- 5. locale formatting + deterministic QA --------------------------------------------------------------
    def qa(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        gl = data["glossary"]
        src_fmt = locale_format(data["formats"], data["source_locale"])
        checks: list[Check] = []
        rows_all = {}
        summary: dict[str, Any] = {}
        for loc, rows in data["rows"].items():
            tgt_fmt = locale_format(data["formats"], loc)
            glossary = glossary_for(gl, loc)
            fixed_n = fixed_d = 0
            out = []
            for r in rows:
                r = dict(r)
                if r["target"] is None:  # defensive: nothing produced
                    r.update(target=r["source"], origin="mt", mt_failed=True)
                target, n1 = localise_numbers(r["target"], r["source"], src_fmt, tgt_fmt)
                target, n2 = localise_dates(target, r["source"], src_fmt, tgt_fmt)
                fixed_n, fixed_d = fixed_n + n1, fixed_d + n2
                issues = qa_segment(r["source"], target, src_fmt=src_fmt, tgt_fmt=tgt_fmt, glossary=glossary, dnt=data["dnt"],
                                    max_chars=inp.get("max_chars"))
                if r.get("mt_failed"):
                    issues.insert(0, ("mt_failed", "machine translation failed - source text kept"))
                r.update(target=target, qa={"passed": qa_passed(issues), "issues": [f"{rule}: {msg}" for rule, msg in issues]},
                         _rules=[rule for rule, _ in issues])
                out.append(r)
            rows_all[loc] = out
            by_rule = Counter(rule for r in out for rule in r["_rules"])
            by_origin = Counter(r["origin"] for r in out)
            passed = sum(1 for r in out if r["qa"]["passed"])
            summary[loc] = {"segments": len(out), "passed": passed, "failed": len(out) - passed, "issues": dict(by_rule),
                            "origins": dict(by_origin), "numbers_localised": fixed_n, "dates_localised": fixed_d}
            for rule in ("placeholders", "numbers", "dnt", "glossary", "untranslated", "length", "max_chars", "mt_failed"):
                n = by_rule.get(rule, 0)
                sev = SEVERITY.get(rule, "warn")
                st = "pass" if not n else ("warn" if by_rule.get("offline") and rule == "untranslated" else sev)
                checks.append(Check(rule=f"qa.{rule}", status=st, severity="high" if st == "fail" else ("medium" if st == "warn" else "low"),
                                    target=loc, message=f"{loc}: {n} segment(s) with {rule.replace('_', ' ')} issues." if n
                                    else f"{loc}: {rule.replace('_', ' ')} check passed on all {len(out)} segment(s)."))
            if fixed_n or fixed_d:
                checks.append(Check(rule="locale.formatting", status="info", severity="low", target=loc,
                                    message=f"{loc}: re-formatted {fixed_n} number(s) and {fixed_d} date(s) to the locale's conventions."))
        overall_failed = sum(v["failed"] for v in summary.values())
        summary["overall"] = {"segments": sum(v["segments"] for v in summary.values()), "failed": overall_failed,
                              "passed": sum(v["passed"] for v in summary.values() if isinstance(v, dict) and "passed" in v)}
        return {"data": {"rows": rows_all, "qa_summary": summary}, "checks": checks}

    # -- 6. assemble documents, TM write-back, artifacts, gated CMS publish ------------------------------------
    def assemble(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        documents: dict[str, str] = {}
        segments: list[TranslatedSegment] = []
        artifacts, actions = [], []
        new_units: list[dict[str, str]] = []
        offline = ctx.llm.offline
        for loc, rows in data["rows"].items():
            documents[loc] = assemble_document(data["tokens"], [r["target"] for r in rows])
            for r in rows:
                segments.append(TranslatedSegment(id=r["id"], source=r["source"], target=r["target"], locale=loc, origin=r["origin"],
                                                  qa=SegmentQA(**r["qa"]), tm_score=r.get("tm_score"), tm_suggestion=r.get("tm_suggestion")))
                if r["origin"] != "tm_exact" and r["qa"]["passed"] and not r["target"].startswith("[offline:") and not r.get("mt_failed"):
                    new_units.append({"id": f"{ctx.run_id}-{loc}-{r['id']}", "source": r["source"], "target": r["target"],
                                      "locale": loc, "origin": r["origin"]})
            ext = "md" if data["mode"] == "text" else "txt"
            artifacts.append(ctx.artifacts.write_text(f"translated-{loc}.{ext}", documents[loc],
                                                      "text/markdown" if ext == "md" else "text/plain", f"Translation ({loc})"))
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["locale", "id", "origin", "qa_passed", "issues", "tm_score", "source", "target"])
        for s in segments:
            w.writerow([s.locale, s.id, s.origin, s.qa.passed, " | ".join(s.qa.issues), s.tm_score or "", s.source, s.target])
        artifacts.append(ctx.artifacts.write_text("qa_report.csv", buf.getvalue(), "text/csv", "Segment-level translation QA"))

        checks: list[Check] = []
        tm_file = None
        if new_units:
            store = ctx.settings.runs_dir / "_store"
            store.mkdir(parents=True, exist_ok=True)
            path = store / f"tm-{ctx.run_id}.tmx"
            sample = Path(ctx.settings.sample_data).resolve()
            if sample in path.resolve().parents:  # never write into the sample / source TM folder
                raise RuntimeError("Refusing to write translation memory into sample_data")
            xml = build_tmx(new_units, data["source_locale"])
            path.write_text(xml, encoding="utf-8")
            tm_file = path.relative_to(ctx.settings.runs_dir).as_posix() if ctx.settings.runs_dir in path.parents else str(path)
            artifacts.append(ctx.artifacts.write_text(f"tm-{ctx.run_id}.tmx", xml, "application/x-tmx+xml",
                                                      "New translation units for review (TMX 1.4)"))
            checks.append(Check(rule="tm.write", status="info", severity="low",
                                message=f"{len(new_units)} new unit(s) written to runs/_store/tm-{ctx.run_id}.tmx (status: unreviewed)."))
        else:
            checks.append(Check(rule="tm.write", status="info", severity="low",
                                message="No new TM units" + (" (offline placeholders are never stored)." if offline else ".")))

        summary_qa = data["qa_summary"]
        if inp.get("publish"):
            title = inp.get("title") or normalize_ws(data["segments"][0])[:80]
            slug = inp.get("slug") or slugify(title, 60)
            for loc, doc in documents.items():
                failed = summary_qa.get(loc, {}).get("failed", 0)
                actions.append(PendingAction(
                    id=f"cms-{loc}", kind="cms.publish", connector="cms", method="publish",
                    payload={"title": title, "body": doc, "slug": slug, "content_type": "page", "status": "draft", "locale": loc,
                             "body_format": "markdown" if data["mode"] == "text" else "text",
                             "fields": {"source_locale": data["source_locale"], "qa_failed_segments": failed, "run_id": ctx.run_id}},
                    summary=f"Publish the {loc} translation of '{title[:50]}' to the CMS as a draft"
                            + (f" ({failed} segment(s) failed QA - review first)" if failed else "")))
        out = TranslationOutput(source_locale=data["source_locale"], target_locales=list(documents), segments=segments, documents=documents,
                                qa_summary=summary_qa, tm_file=tm_file, glossary_version=str(data["glossary"].get("version", "")))
        o = summary_qa.get("overall", {})
        origins = Counter(s.origin for s in segments)
        summary = (f"{len(data['segments'])} segment(s) into {', '.join(documents)}: "
                   + ", ".join(f"{k}={v}" for k, v in sorted(origins.items()))
                   + f"; QA {o.get('passed', 0)}/{o.get('segments', 0)} passed")
        return {"output": out.model_dump(mode="json"), "artifacts": artifacts, "actions": actions, "checks": checks, "summary": summary}


SELFTEST_TEXT = """# Your home insurance quote

Hi {first_name}, your home insurance quote is ready.

Save 20% when you insure your home and car together. Members who report a claim within 24 hours are paid nine days sooner on average.

Rebuilding costs have risen 19% in three years. Check your rebuild value today.

- Photograph every room before you need to make a claim.
- Your deductible of $1,500 applies to each claim.
- Your policy renews on 11/30/2026, so ask an advisor about flood cover before then.

Start a claim online at https://www.meridian-mutual.example/claims or call us 24/7.
"""
