"""Quote Extraction - verifiable quotes only.

* Ingestion through the open local layer (trafilatura / pdfplumber / python-docx / transcripts) - no
  browse_web or read_file_content.
* The model (FAST tier) proposes 5-10 candidate quotes with a speaker. Every candidate is then
  ANCHORED by exact match after canonical normalisation (quotes, dashes, whitespace, case - the
  `caf.core.textutil.canonical` rules, applied with a character map so offsets point into the source).
  Anything the model cannot anchor is rejected with a reason - no hallucinated quote can survive.
* Locations come from `Document.locate` (line / section / timestamp / page). Speakers come from the
  transcript prefix "[HH:MM:SS] Name:" (overrides the model), then from attribution patterns in the
  text ("..., said Jane Doe"), and only then from the model.
* A deterministic quotability ranker (numbers, strong claims, first-person opinion, quotation marks,
  12-40 words) supplies quotes when no model is configured or too few candidates anchor; every quote
  is labelled method = llm | heuristic.
* Results compound in an open quote bank: the `store` connector, collection `quote_bank`, deduplicated
  by a hash of the canonical text.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from ...core import AgentKind, AgentSpec, BaseAgent, Category, Check, RunContext, Tier, register
from ...core.textutil import canonical, normalize_quotes, normalize_ws, tfidf_similarity, word_count

MAX_MODEL_CHARS = 60_000
MIN_QUOTE_WORDS = 6
TS_LINE = re.compile(r"^[ \t]*\[(?P<ts>\d{1,2}:\d{2}(?::\d{2})?)\][ \t]*(?:(?P<speaker>[^:\n\]]{1,80}?):[ \t]+)?", re.M)
NAME_LINE = re.compile(r"^[ \t]*(?P<speaker>[A-Z][\w.'’-]+(?:[ \t]+[A-Z][\w.'’-]+){0,3}):[ \t]+", re.M)
NOT_SPEAKERS = {"q", "a", "note", "notes", "source", "title", "date", "summary", "question", "answer", "update", "re", "subject",
                "location", "duration", "agenda", "attendees", "transcript", "host", "moderator", "speaker", "speakers", "webinar"}
SPEECH_VERBS = r"(?:said|says|explained|explains|added|adds|noted|notes|commented|comments|told|tells|wrote|writes|stated|states|argued|argues|observed)"
NAME = r"(?:(?:Dr|Mr|Mrs|Ms|Prof)\.[ \t]+)?[A-Z][\w'’-]+(?:[ \t]+[A-Z][\w'’-]+){0,3}"
NAME2 = r"(?:(?:Dr|Mr|Mrs|Ms|Prof)\.[ \t]+)?[A-Z][\w'’-]+(?:[ \t]+[A-Z][\w'’-]+){1,3}"
_AFTER = re.compile(r"^[\s,.;:]*[\"”’']?[\s,]*" + SPEECH_VERBS + r"\s+(?P<name>" + NAME + ")")
_AFTER_INV = re.compile(r"^[\s,.;:]*[\"”’']?[\s,]*(?P<name>" + NAME2 + r")\s+" + SPEECH_VERBS + r"\b")
_DASH_AFTER = re.compile(r"^[\"”’'.,]*[ \t]*[-–—]{1,2}[ \t]*(?P<name>" + NAME + ")")
_NEXT_LINE_NAME = re.compile(r"^[\"”’'.,]*[ \t]*\n(?:[ \t]*\n)?[ \t]*[-–—]?[ \t]*(?P<name>" + NAME2 + r"),[ \t]*[A-Z]")
_INTERVIEW_AFTER = re.compile(r"^[\"”’']?\s*[:,]\s*(?:an?\s+)?interview with\s+(?P<name>" + NAME2 + ")")
_BEFORE = re.compile(r"(?P<name>" + NAME2 + r")(?:,\s*[^,\n]{2,80},)?\s+" + SPEECH_VERBS + r"\s*(?:that)?[\s:,]*[\"“‘']?\s*$")
_ACCORDING = re.compile(r"according to (?P<name>" + NAME2 + ")")
_QUOTED = re.compile(r"[“\"](?P<inner>[^”\"\n]{20,700})[”\"]")
_QA_LINE = re.compile(r"^[ \t]*(?P<role>[QA])[ \t]*[:.][ \t]+", re.M)
_MARKER = re.compile(r"^[ \t]*(?:(?:[-*+•]|\d+[.)])[ \t]+|>[ \t]*)")
_BOLD_LEAD = re.compile(r"\*\*[^*\n]{1,80}\*\*[ \t]+")
ABBREVIATIONS = {"dr", "mr", "mrs", "ms", "prof", "st", "inc", "ltd", "co", "corp", "jr", "sr", "vs", "etc", "e.g", "i.e", "no",
                 "fig", "u.s", "a.m", "p.m", "approx", "dept", "est", "jan", "feb", "aug", "sept", "oct", "nov", "dec"}
_STRONG = re.compile(r"\b(most|every|never|always|biggest|best|worst|first|only|critical|crucial|essential|key|matters?|transform\w*|"
                     r"changed?|proud|believe|convinced|clear|simple|real|truth|honest|important|difference|biggest|fastest|"
                     r"single|everything|nothing|must|whole|game|future|lesson|surprise\w*|wrong|right)\b", re.I)
_FIRST_PERSON = re.compile(r"\b(I|I'm|I’m|I've|I’ve|I'd|I’d|we|we're|we’re|we've|we’ve|our|my|us)\b")
_FILLER = re.compile(r"\b(um+|uh+|erm|you know|kind of|sort of|i mean)\b|\blike,", re.I)
_PLEASANTRY = re.compile(r"\b(welcome|thanks? (?:you|for|so much|again|everyone)|thank you|good (?:morning|afternoon|evening)|"
                         r"hello|hi everyone|great question|any questions|let'?s get started|before we (?:start|begin)|"
                         r"can you (?:hear|see)|next slide|you'?re on mute)\b", re.I)
_CONTEXT_START = re.compile(r"^(and|but|so|that|this|it|they|he|she|which|also|then|yeah|yes|no|okay|ok|well|right)\b", re.I)
_SENT_END = re.compile(r"(?<=[.!?])(?P<close>[\"”’)]*)\s+(?=[\"“‘(]?[A-Z0-9])")


class QuoteInput(BaseModel):
    content: str | None = Field(default=None, description="Text or a URL to mine for quotes", json_schema_extra={"ui": "textarea"})
    file: str | None = Field(default=None, description="File path: PDF, DOCX, Markdown, text, VTT/SRT transcript (relative to the framework home)")
    path: str | None = Field(default=None, description="Alias of `file` (same convention as the other document agents)")
    min_quotes: int = Field(default=5, ge=1, le=20, description="Top up with heuristic quotes below this many verified model quotes")
    max_quotes: int = Field(default=10, ge=1, le=25, description="Maximum quotes returned")
    topic: str | None = Field(default=None, description="Optional theme to favour, e.g. 'flood prevention'")
    save_to_bank: bool = Field(default=True, description="Store verified quotes in the open quote bank (store connector)")

    @model_validator(mode="after")
    def _need_source(self) -> "QuoteInput":
        if not (self.file and self.file.strip()) and self.path and self.path.strip():
            self.file = self.path
        if not (self.content and self.content.strip()) and not (self.file and self.file.strip()):
            raise ValueError("Provide content (text or URL) and/or file - at least one is required.")
        if self.min_quotes > self.max_quotes:
            self.min_quotes = self.max_quotes
        return self


class Candidate(BaseModel):
    text: str = Field(description="The quote copied character-for-character from the source")
    speaker: str | None = Field(default=None, description="Who said or wrote it, if the source says")
    why: str = Field(default="", description="Why it is quotable (one short phrase)")


class CandidateList(BaseModel):
    quotes: list[Candidate] = Field(default_factory=list)


class QuoteLocation(BaseModel):
    line: int
    section: str | None = None
    timestamp: str | None = None
    page: int | None = None
    label: str = ""


class ExtractedQuote(BaseModel):
    text: str
    speaker: str | None = None
    speaker_source: Literal["transcript", "attribution", "model", "document", "none"] = "none"
    location: QuoteLocation
    char_start: int
    char_end: int
    verified: bool = True
    method: Literal["llm", "heuristic"]
    score: float | None = None
    bank_id: str | None = None


class RejectedQuote(BaseModel):
    text: str
    speaker: str | None = None
    reason: str


class QuoteOutput(BaseModel):
    source_title: str
    source: str = ""
    quotes: list[ExtractedQuote]
    rejected: list[RejectedQuote]
    bank_added: int = 0
    bank_existing: int = 0


# ----------------------------------------------------------------------------- pure helpers: anchoring

def canonical_map(text: str) -> tuple[str, list[int]]:
    """Canonical form (textutil.canonical rules: quote/dash normalisation, NFKC, collapsed whitespace,
    lower case) plus, for every canonical character, the index of the source character it came from."""
    out: list[str] = []
    idx: list[int] = []
    space = True  # suppress leading whitespace
    for i, ch in enumerate(text or ""):
        for c in unicodedata.normalize("NFKC", normalize_quotes(ch)).lower():
            if c.isspace():
                if space:
                    continue
                out.append(" ")
                idx.append(i)
                space = True
            else:
                out.append(c)
                idx.append(i)
                space = False
    if out and out[-1] == " ":
        out.pop()
        idx.pop()
    return "".join(out), idx


def clean_candidate(text: str) -> tuple[str, str | None]:
    """Strip wrapping quotes and a leading transcript/speaker prefix the model may have copied."""
    t = (text or "").strip().strip("\"“”‘’'").strip()
    speaker = None
    m = TS_LINE.match(t)
    if m:
        speaker = (m.group("speaker") or "").strip() or None
        t = t[m.end():]
    t = t.strip().strip("\"“”‘’'").strip()
    return t, speaker


def anchor_span(candidate: str, source_canon: str, source_idx: list[int]) -> tuple[int, int, int] | None:
    """Exact match after canonical normalisation -> (char_start, char_end, occurrences) in the source."""
    c, _ = canonical_map(candidate)
    if not c:
        return None
    pos = source_canon.find(c)
    if pos < 0:
        return None
    occurrences = source_canon.count(c)
    return source_idx[pos], source_idx[pos + len(c) - 1] + 1, occurrences


def reject_reason(candidate: str) -> str | None:
    if "..." in candidate or "…" in candidate:
        return "contains an ellipsis - elided quotes cannot be anchored verbatim"
    if word_count(candidate) < MIN_QUOTE_WORDS:
        return f"too short (< {MIN_QUOTE_WORDS} words)"
    return None


def transcript_speaker(text: str, offset: int) -> tuple[str | None, str | None]:
    """Speaker + timestamp from the nearest '[HH:MM:SS] Name:' line at or above the offset."""
    line_start = text.rfind("\n", 0, offset) + 1
    for m in reversed(list(TS_LINE.finditer(text, 0, line_start + 200))):
        if m.start() <= offset:
            return ((m.group("speaker") or "").strip() or None), m.group("ts")
    return None, None


def interview_roles(text: str) -> dict[str, str]:
    """Q:/A: interviews - 'an interview with <Name>' answers, 'Interviewer: <Name>' asks."""
    head = (text or "")[:4000]
    roles: dict[str, str] = {}
    m = re.search(r"interview with\s+(?P<name>" + NAME2 + ")", head)
    if m:
        roles["A"] = m.group("name").strip()
    m = re.search(r"(?mi)^[ \t]*(?:interviewer|interviewed by)[ \t]*:?[ \t]*(?P<name>" + NAME2 + ")", head)
    if m:
        roles["Q"] = m.group("name").strip()
    return roles


def role_speaker(text: str, offset: int, roles: dict[str, str]) -> str | None:
    """Speaker of the Q:/A: turn that contains the offset (answers can run over several paragraphs)."""
    if not roles:
        return None
    last = None
    for m in _QA_LINE.finditer(text, 0, offset + 1):
        last = m
    return roles.get(last.group("role")) if last else None


def is_speaker_label(label: str, text: str) -> bool:
    """'Tom Ellery:' is a speaker; 'Portfolio:' or 'Operational:' are field labels - unless a one-word label
    recurs as a line prefix (a real interview turn marker such as 'Tom:')."""
    lab = (label or "").strip()
    if not lab or lab.lower() in NOT_SPEAKERS:
        return False
    if len(lab.split()) >= 2:
        return True
    return len(re.findall(rf"(?m)^[ \t]*{re.escape(lab)}:[ \t]", text or "")) >= 3


def name_line_speaker(text: str, offset: int) -> str | None:
    """Speaker from an interview-style 'Name: text' line containing the offset."""
    line_start = text.rfind("\n", 0, offset) + 1
    m = NAME_LINE.match(text, line_start)
    if m and is_speaker_label(m.group("speaker"), text) and m.end() <= offset + 1:
        return m.group("speaker").strip()
    return None


def document_author(text: str) -> str | None:
    """Author / publisher stated in the document header ('Published by Meridian Mutual', 'By Jane Doe')."""
    head = (text or "")[:4000]
    m = re.search(r"(?mi)\b(?:published|written|prepared|produced|issued)\s+by\s+(?P<name>[A-Z][\w&'’.-]*(?:[ \t]+[A-Z][\w&'’.-]*){0,4})", head)
    if m and m.group("name").lower() not in ("the",):
        return m.group("name").strip().rstrip(".,")
    m = re.search(r"(?m)^[ \t]*(?:By|Author|Authors|Lead authors?)[ \t]*:?[ \t]+(?P<name>" + NAME2 + ")", head)
    return m.group("name").strip() if m else None


def attribution_near(text: str, start: int, end: int) -> str | None:
    """Attribution around a quote: '..." - Jane Doe', '..., said Jane Doe', a 'Jane Doe, Title' line under a
    pull-quote, 'Jane Doe said: "..."', 'according to Jane Doe'."""
    after = text[end: end + 250]
    for rx in (_DASH_AFTER, _AFTER, _INTERVIEW_AFTER, _NEXT_LINE_NAME, _AFTER_INV):
        m = rx.match(after)
        if m:
            return m.group("name").strip()
    m = _BEFORE.search(text[max(0, start - 200): start])
    if m:
        return m.group("name").strip()
    m = _ACCORDING.search(text[max(0, start - 160): end + 160])
    return m.group("name").strip() if m else None


def resolve_speaker(text: str, start: int, end: int, model_speaker: str | None, is_transcript: bool,
                    roles: dict[str, str] | None = None, author: str | None = None) -> tuple[str | None, str]:
    """Deterministic attribution first (transcript prefix, interview role, 'Name:' line, attribution pattern);
    the model's speaker only when the source offers nothing."""
    if is_transcript:
        sp, _ = transcript_speaker(text, start)
        if sp:
            return sp, "transcript"
    sp = role_speaker(text, start, roles or {}) or name_line_speaker(text, start)
    if sp:
        return sp, "transcript"
    sp = attribution_near(text, start, end)
    if sp:
        return sp, "attribution"
    if model_speaker and model_speaker.strip() and model_speaker.strip().lower() not in ("unknown", "n/a", "none", "unattributed"):
        return model_speaker.strip(), "model"
    if author and not is_transcript:
        return author, "document"
    return None, "none"


def location_label(loc: dict[str, Any]) -> str:
    parts = []
    if loc.get("timestamp"):
        parts.append(str(loc["timestamp"]))
    if loc.get("page"):
        parts.append(f"p. {loc['page']}")
    if loc.get("section"):
        parts.append(f"§ {loc['section']}")
    parts.append(f"line {loc.get('line')}")
    return ", ".join(parts)


def bank_key(text: str) -> str:
    return hashlib.sha1(canonical(text).encode("utf-8")).hexdigest()[:16]


# ----------------------------------------------------------------------------- pure helpers: heuristic ranker

def _ends_with_abbreviation(chunk: str, pos: int) -> bool:
    m = re.search(r"(\S+)$", chunk[:pos])
    if not m:
        return False
    tok = m.group(1).strip("\"“”‘’'()[]").rstrip(".").lower()
    return tok in ABBREVIATIONS or bool(re.fullmatch(r"[a-z]", tok))


def split_sentences(text: str, us: int, ue: int) -> list[tuple[int, int]]:
    """Sentence spans inside text[us:ue]; abbreviation- and initial-safe ('Dr. Asha Raman' stays whole)."""
    chunk = text[us:ue]
    spans = []
    cur = 0
    for m in _SENT_END.finditer(chunk):
        if _ends_with_abbreviation(chunk, m.start()):
            continue
        seg_end = m.start() + len(m.group("close"))  # keep closing quotes after the full stop
        if chunk[cur:seg_end].strip():
            spans.append((us + cur, us + seg_end))
        cur = m.end()
    if chunk[cur:].strip():
        spans.append((us + cur, ue))
    return spans


def sentence_spans(text: str) -> list[tuple[int, int]]:
    """Verbatim sentence spans (start, end) in the source. Headings, tables, code and page markers are
    skipped; transcript / interview / list / blockquote prefixes and bold lead-ins are excluded from spans."""
    units: list[tuple[int, int]] = []
    pos = 0
    para: list[int] | None = None
    in_code = False

    def flush() -> None:
        nonlocal para
        if para is not None:
            units.append((para[0], para[1]))
            para = None

    lines = (text or "").splitlines(keepends=True)
    for li, line in enumerate(lines):
        ls, le = pos, pos + len(line.rstrip("\r\n"))
        pos += len(line)
        raw = text[ls:le]
        s = raw.strip()
        if s.startswith("```"):
            in_code = not in_code
            flush()
            continue
        if in_code or not s or s.startswith(("#", "|", "[page")) or re.fullmatch(r"[-=*_]{3,}", s):
            flush()
            continue
        m = TS_LINE.match(text, ls)
        qa = _QA_LINE.match(raw)
        nm = None if (m or qa) else NAME_LINE.match(raw)
        nm = nm if nm and is_speaker_label(nm.group("speaker"), text) else None
        mk = None if (m or qa or nm) else _MARKER.match(raw)
        if m or qa or nm or mk:
            flush()
            start = m.end() if m else ls + (qa or nm or mk).end()
            content = text[start:le].strip()
            nxt = next((x.strip() for x in lines[li + 1:] if x.strip()), "")
            if (mk and re.match(r"[ \t]*\d+[.)]", raw) and word_count(content) <= 6 and not re.search(r"[.!?:;,\"”]$", content)
                    and not nxt[:1].islower()):
                continue  # '8. Outlook for 2027' - a numbered heading in PDF text, not a sentence
            para = [start, le]  # wrapped continuation lines extend this turn / list item
            continue
        if para is None:
            para = [ls + (len(raw) - len(raw.lstrip())), le]
        else:
            para[1] = le
    flush()
    out: list[tuple[int, int]] = []
    for us, ue in units:
        lead = _BOLD_LEAD.match(text, us)
        if lead and lead.end() < ue:
            us = lead.end()
        for s_, e_ in split_sentences(text, us, ue):
            while s_ < e_ and text[s_].isspace():
                s_ += 1
            while e_ > s_ and text[e_ - 1].isspace():
                e_ -= 1
            if e_ > s_:
                out.append((s_, e_))
    return out


def quoted_passages(text: str) -> list[tuple[int, int]]:
    """Inner spans of explicit quotations (“...” or "...") of 6-70 words - already-quoted voices."""
    out = []
    for m in _QUOTED.finditer(text or ""):
        inner = m.group("inner").strip()
        if 6 <= word_count(inner) <= 70:
            s_, e_ = m.start("inner"), m.end("inner")
            while s_ < e_ and text[s_].isspace():
                s_ += 1
            while e_ > s_ and text[e_ - 1].isspace():
                e_ -= 1
            out.append((s_, e_))
    return out


def quotability(sentence: str, topic: str | None = None, *, explicit: bool = False) -> float:
    """Deterministic 'would a marketer quote this?' score (0 = not quotable). `explicit` = already a quotation."""
    s = normalize_ws(sentence)
    n = word_count(s)
    hi = 70 if explicit else 50
    if n < 8 or n > hi or "http" in s or "@" in s or "[page" in s:
        return 0.0
    score = 1.0
    if 12 <= n <= 40:
        score += 1.0
    elif not explicit:
        score -= 0.5
    if 15 <= n <= 30:
        score += 0.4
    if re.search(r"\d", s):
        score += 1.0
    strong = len(set(m.lower() for m in _STRONG.findall(s)))
    score += min(1.2, 0.5 * strong)
    if _FIRST_PERSON.search(s):
        score += 1.0
    if explicit:
        score += 1.5
    elif re.search(r"[“\"][^”\"]{3,}[”\"]", s):
        score += 0.7
    if re.search(r"\b(but|yet|instead|rather than|not because)\b", s, re.I):
        score += 0.3
    if _FILLER.search(s):
        score -= 1.0
    if s.endswith("?"):
        score -= 1.2
    if _PLEASANTRY.search(s):
        score -= 1.5
    if _CONTEXT_START.match(s):
        score -= 0.6
    if re.search(r"\*\*|__|`|\]\(", s):
        score -= 1.0
    if re.match(r"^[A-Z][\w &/-]{1,24}:\s", s) and not explicit:
        score -= 1.5  # 'Portfolio: 1,400 homes' - a fact-sheet field, not a quote
    if not explicit and not re.search(r"[.!?][\"”’)]*$", s):
        score -= 0.7  # unterminated fragment
    if topic:
        tw = {w for w in re.findall(r"[a-z]{4,}", topic.lower())}
        if tw and tw & set(re.findall(r"[a-z]{4,}", s.lower())):
            score += 0.8
    return round(max(0.0, score), 3)


def heuristic_quotes(text: str, k: int, *, topic: str | None = None, exclude: list[tuple[int, int]] | None = None,
                     is_transcript: bool = False) -> list[dict[str, Any]]:
    """Top-k quotable verbatim spans: explicit quotations first-class, then sentences; diversified by
    content (TF-IDF) and by speaker."""
    exclude = list(exclude or [])

    def overlaps(s_: int, e_: int, spans: list[tuple[int, int]]) -> bool:
        return any(not (e_ <= xs or s_ >= xe) for xs, xe in spans)

    cands = []
    explicit = [(s_, e_) for s_, e_ in quoted_passages(text) if not overlaps(s_, e_, exclude)]
    for s_, e_ in explicit:
        sc = quotability(text[s_:e_], topic, explicit=True)
        if sc >= 2.0:
            cands.append({"start": s_, "end": e_, "text": normalize_ws(text[s_:e_]), "score": sc})
    for s_, e_ in sentence_spans(text):
        if overlaps(s_, e_, exclude) or overlaps(s_, e_, explicit):
            continue
        sc = quotability(text[s_:e_], topic)
        if sc >= 2.0:
            cands.append({"start": s_, "end": e_, "text": normalize_ws(text[s_:e_]), "score": sc})
    cands.sort(key=lambda c: (-c["score"], c["start"]))
    chosen: list[dict[str, Any]] = []
    per_speaker: dict[str, int] = {}
    for c in cands[: max(60, k * 6)]:
        if len(chosen) >= k:
            break
        if chosen and max(tfidf_similarity(c["text"], [x["text"] for x in chosen]) or [0]) > 0.6:
            continue
        sp = transcript_speaker(text, c["start"])[0] if is_transcript else None
        if sp and per_speaker.get(sp, 0) >= max(2, k // 2):
            continue
        if sp:
            per_speaker[sp] = per_speaker.get(sp, 0) + 1
        chosen.append(c)
    return chosen


def clean_title(title: str, source: str = "", text: str = "") -> str:
    """Readable source title: no transcript prefix; a wrapped-line fragment or an aside falls back to the first
    title-like line of the document, then to the file name."""
    def good(t: str) -> bool:
        c = t.lstrip("\"“‘'")[:1]
        return (2 <= word_count(t) <= 24 and len(t) <= 140 and (c.isupper() or c.isdigit()) and not t.startswith("(")
                and not t.lower().startswith("illustrative"))
    t = normalize_ws(TS_LINE.sub("", title or "")).strip("# ")
    transcript = bool(TS_LINE.match(title or ""))
    if t and good(t) and not transcript:
        return t
    for line in ([] if transcript else (text or "").splitlines()[:25]):
        cand = normalize_ws(TS_LINE.sub("", line)).strip("# ")
        if cand and good(cand) and not cand.endswith((".", ",", ";", ":")) and "[page" not in cand:
            return cand
    stem = re.sub(r"[_-]+", " ", (source or "").replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0]).strip()
    return stem.capitalize() if stem else (t or "Untitled")


# ----------------------------------------------------------------------------- agent

@register
class QuoteExtractionAgent(BaseAgent):
    spec = AgentSpec(
        id="quote_extraction",
        name="Quote Extraction",
        category=Category.CONTENT,
        summary="Extracts five to ten verbatim, verifiable quotes with speaker and location from documents, URLs and transcripts, "
                "rejects anything it cannot anchor in the source and grows an open quote bank.",
        kind=AgentKind.HYBRID,
        tier=Tier.FAST,
        connectors=["ingestion", "store"],
        skills=[],
        keywords=["quotes from", "pull quote", "testimonial", "soundbite", "quote bank", "extract quotes", "find quotes", "verbatim", "interview quotes"],
        produces=["quotes.csv", "quotes.json"],
        feeds=["press_release_creation", "content_adaptation", "blog_post_creation", "linkedin_inmail_copy"],
    )
    Input = QuoteInput
    Output = QuoteOutput
    schemas = {"extract": CandidateList}

    def selftest_input(self) -> dict[str, Any]:
        return {"file": "sample_data/webinar/transcript.txt"}

    def stages(self):
        return [("ingest", self.ingest), ("propose", self.propose), ("anchor", self.anchor), ("heuristic", self.heuristic),
                ("bank", self.bank)]

    # -- ingest ---------------------------------------------------------------------------------------
    def ingest(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp = state["inputs"]
        conn = ctx.connector("ingestion")
        docs = []
        try:
            if inp.get("file"):
                docs.append(conn.load(path=inp["file"]))
            content = (inp.get("content") or "").strip()
            if content:
                docs.append(conn.load(content) if content.startswith(("http://", "https://")) else conn.load(text=content))
        except Exception as exc:
            raise ValueError(f"Could not ingest the source: {exc}") from exc
        text = "\n\n".join(d.text for d in docs)
        if not text.strip():
            raise ValueError("The source contains no extractable text.")
        title = clean_title(docs[0].title, str(docs[0].source), text)
        source = " + ".join(str(d.source) for d in docs)
        is_transcript = len(TS_LINE.findall(text)) >= 3
        checks = [Check(rule="ingest.parsed", status="info", severity="low", target=source,
                        message=f"Parsed '{title}' locally ({', '.join(d.mime for d in docs)}): {word_count(text):,} words"
                                + (", timestamped transcript" if is_transcript else "") + ".")]
        return {"data": {"text": text, "title": title, "source": source, "mime": docs[0].mime, "is_transcript": is_transcript,
                         "roles": interview_roles(text), "author": document_author(text)},
                "checks": checks}

    # -- propose (model) -----------------------------------------------------------------------------
    def propose(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        text = data["text"]
        warnings: list[str] = []
        excerpt = text
        if len(text) > MAX_MODEL_CHARS:
            excerpt = text[:MAX_MODEL_CHARS]
            warnings.append(f"Source is {len(text):,} chars; the model saw the first {MAX_MODEL_CHARS:,} (the heuristic ranker covers all of it).")
        try:
            out = ctx.generate("extract", CandidateList, source=excerpt, title=data["title"], max_quotes=inp["max_quotes"],
                               min_quotes=inp["min_quotes"], topic=inp.get("topic"), is_transcript=data["is_transcript"])
            cands = [c.model_dump() for c in out.quotes]
        except Exception as exc:
            warnings.append(f"Model proposal failed ({type(exc).__name__}: {exc}); heuristic ranker used.")
            cands = []
        return {"data": {"candidates": cands}, "warnings": warnings}

    # -- anchor (deterministic verification) ---------------------------------------------------------
    def anchor(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        from ...connectors.ingestion import Document

        inp, data = state["inputs"], state["data"]
        text = data["text"]
        canon, idx = canonical_map(text)
        doc = Document(source=data["source"], title=data["title"], text=text, mime=data["mime"])
        accepted: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        for c in data.get("candidates", []):
            raw = c.get("text") or ""
            cleaned, prefix_speaker = clean_candidate(raw)
            span = anchor_span(cleaned, canon, idx) if "..." not in cleaned and "…" not in cleaned else None
            reason = None
            if span is None:
                reason = reject_reason(cleaned) if ("..." in cleaned or "…" in cleaned) else \
                    "not found in the source after canonical normalisation (paraphrased or invented)"
            elif word_count(cleaned) < MIN_QUOTE_WORDS:
                reason = reject_reason(cleaned)
            if reason:
                rejected.append({"text": normalize_ws(raw)[:400], "speaker": c.get("speaker"), "reason": reason})
                continue
            start, end, _occ = span
            if any(not (end <= a["char_start"] or start >= a["char_end"]) for a in accepted):
                rejected.append({"text": normalize_ws(raw)[:400], "speaker": c.get("speaker"), "reason": "duplicate / overlaps an accepted quote"})
                continue
            if len(accepted) >= inp["max_quotes"]:
                rejected.append({"text": normalize_ws(raw)[:400], "speaker": c.get("speaker"), "reason": f"over the {inp['max_quotes']}-quote limit"})
                continue
            speaker, sp_src = resolve_speaker(text, start, end, c.get("speaker") or prefix_speaker, data["is_transcript"],
                                              data.get("roles"), data.get("author"))
            accepted.append(self._quote(doc, start, end, speaker, sp_src, "llm", None))
        n_c = len(data.get("candidates", []))
        checks = [Check(rule="quotes.anchored", status="pass" if accepted or not n_c else "warn", severity="medium",
                        message=f"{len(accepted)} of {n_c} model candidate(s) anchored verbatim in the source; {len(rejected)} rejected.",
                        data={"anchored": len(accepted), "rejected": len(rejected)})]
        return {"data": {"accepted": accepted, "rejected": rejected}, "checks": checks}

    def _quote(self, doc: Any, start: int, end: int, speaker: str | None, sp_src: str, method: str, score: float | None) -> dict[str, Any]:
        loc = doc.locate(start)
        if not loc.get("timestamp"):
            loc["timestamp"] = transcript_speaker(doc.text, start)[1] if TS_LINE.search(doc.text) else None
        loc["label"] = location_label(loc)
        return {"text": normalize_ws(doc.text[start:end]), "speaker": speaker, "speaker_source": sp_src, "location": loc,
                "char_start": start, "char_end": end, "verified": True, "method": method, "score": score}

    # -- heuristic top-up (offline or too few anchored) -------------------------------------------------
    def heuristic(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        from ...connectors.ingestion import Document

        inp, data = state["inputs"], state["data"]
        accepted = list(data.get("accepted", []))
        warnings: list[str] = []
        reasons: list[str] = []
        if ctx.llm.offline and not accepted:
            target = max(inp["min_quotes"], min(inp["max_quotes"], 8))
            reasons.append("offline model: quotes chosen by the deterministic quotability ranker")
        elif len(accepted) < inp["min_quotes"]:
            target = inp["min_quotes"]
            warnings.append(f"Only {len(accepted)} model quote(s) could be anchored; topped up with heuristic picks to {target}.")
        else:
            return {"data": {"final": sorted(accepted, key=lambda q: q["char_start"])}}
        text = data["text"]
        doc = Document(source=data["source"], title=data["title"], text=text, mime=data["mime"])
        picks = heuristic_quotes(text, target - len(accepted), topic=inp.get("topic"),
                                 exclude=[(a["char_start"], a["char_end"]) for a in accepted], is_transcript=data["is_transcript"])
        for p in picks:
            speaker, sp_src = resolve_speaker(text, p["start"], p["end"], None, data["is_transcript"], data.get("roles"),
                                              data.get("author"))
            accepted.append(self._quote(doc, p["start"], p["end"], speaker, sp_src, "heuristic", p["score"]))
        checks = [Check(rule="quotes.heuristic", status="info", severity="low",
                        message=f"{len(picks)} quote(s) added by the quotability ranker (numbers, strong claims, first-person "
                                "opinion, quotation marks, 12-40 words) - verbatim by construction.")]
        upd: dict[str, Any] = {"data": {"final": sorted(accepted, key=lambda q: q["char_start"])}, "checks": checks, "warnings": warnings}
        if reasons:
            upd["provenance"] = {"degraded_reasons": reasons}
        return upd

    # -- quote bank + output -------------------------------------------------------------------------
    def bank(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        quotes = data.get("final", [])
        checks: list[Check] = []
        warnings: list[str] = []
        added = existing = 0
        if inp.get("save_to_bank", True) and quotes:
            try:
                store = ctx.connector("store")
                now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                for q in quotes:
                    key = bank_key(q["text"])
                    q["bank_id"] = key
                    prior = store.get("quote_bank", key)
                    if prior:
                        existing += 1
                        sources = sorted(set(prior.get("sources", [prior.get("source")]) + [data["source"]]) - {None})
                        if sources != prior.get("sources"):
                            store.put("quote_bank", {**prior, "sources": sources, "last_seen": now}, key=key)
                        continue
                    store.put("quote_bank", {"id": key, "text": q["text"], "speaker": q["speaker"], "source": data["source"],
                                             "sources": [data["source"]], "source_title": data["title"], "location": q["location"],
                                             "char_start": q["char_start"], "char_end": q["char_end"], "method": q["method"],
                                             "topic": inp.get("topic"), "run_id": ctx.run_id, "created_at": now, "last_seen": now}, key=key)
                    added += 1
                checks.append(Check(rule="quote_bank.saved", status="info", severity="low",
                                    message=f"Quote bank: {added} new, {existing} already banked (deduplicated by canonical-text hash)."))
            except Exception as exc:
                warnings.append(f"Quote bank unavailable ({type(exc).__name__}: {exc}); quotes not stored.")
        unattributed = sum(1 for q in quotes if not q.get("speaker"))
        if quotes:
            checks.append(Check(rule="quotes.attribution", status="pass" if not unattributed else "warn", severity="low",
                                message=f"{len(quotes) - unattributed} of {len(quotes)} quote(s) attributed"
                                        + (" (speakers from transcript prefixes / attribution patterns)." if not unattributed else
                                           f"; {unattributed} without a verifiable speaker.")))
        lo = inp["min_quotes"]
        checks.append(Check(rule="quotes.count", status="pass" if len(quotes) >= lo else "warn", severity="medium",
                            message=f"{len(quotes)} verified quote(s) (target {lo}-{inp['max_quotes']})."))
        out = QuoteOutput(source_title=data["title"], source=data["source"], quotes=[ExtractedQuote.model_validate(q) for q in quotes],
                          rejected=[RejectedQuote.model_validate(r) for r in data.get("rejected", [])], bank_added=added, bank_existing=existing)
        import csv
        import io

        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["text", "speaker", "location", "char_start", "char_end", "method", "bank_id"])
        for q in quotes:
            w.writerow([q["text"], q.get("speaker") or "", q["location"]["label"], q["char_start"], q["char_end"], q["method"], q.get("bank_id") or ""])
        artifacts = [ctx.artifacts.write_text("quotes.csv", buf.getvalue(), "text/csv", "Verified quotes"),
                     ctx.artifacts.write_json("quotes.json", out.model_dump(mode="json"), "Verified + rejected quotes")]
        methods = {m: sum(1 for q in quotes if q["method"] == m) for m in ("llm", "heuristic")}
        summary = (f"{len(quotes)} verified quote(s) from '{data['title'][:60]}' ({methods['llm']} model, {methods['heuristic']} heuristic); "
                   f"{len(out.rejected)} rejected")
        return {"output": out.model_dump(mode="json"), "checks": checks, "artifacts": artifacts, "warnings": warnings, "summary": summary}
