"""Content Summary - own the ingestion and the chunking, then map-reduce summarise.

Graph (LangGraph, custom fan-out):

    ingest -> chunk -(Send: one task per chunk, in parallel)-> map -> reduce -> format

* Ingestion is the open local layer (`ctx.connector("ingestion")`): trafilatura for URLs/HTML,
  pdfplumber for PDF, python-docx for Word, plain parsing for Markdown, text and transcripts.
* Chunking is semantic (headings / paragraphs, `caf.core.textutil.chunk_text`). MAP runs once per
  chunk in parallel (`langgraph.types.Send`); REDUCE collapses the partial notes hierarchically
  whenever they exceed the reduce budget - so documents of any length work regardless of the
  model's context window.
* Summary modes are prompt templates: `config/prompts/content_summary/<mode>.j2` for executive,
  technical, bullet, abstract, action_items and meta_description (map.j2 / collapse.j2 drive the
  map and collapse steps). The meta description is validated deterministically (<= 155 chars).
* Confidential documents can stay fully on-premise: point the framework at a local model with the
  ollama provider (CAF_LLM_PROVIDER=ollama, OLLAMA_HOST=http://<host>:11434) - parsing, chunking and
  every model call then stay inside your estate. With `confidential=true` the agent refuses to send
  text to a non-local provider and returns a local extractive summary instead.
* No model configured (offline): a deterministic extractive summariser (TF-IDF centrality, position
  and focus bonuses, MMR redundancy control) returns verbatim key points, labelled method='extractive'.
"""

from __future__ import annotations

import re
import threading
from typing import Any, Literal
from urllib.parse import urlsplit

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from pydantic import BaseModel, Field

from ...core import AgentKind, AgentSpec, AgentState, BaseAgent, Category, Check, RunContext, Tier, register
from ...core.textutil import canonical, chunk_text, normalize_ws, sentences, tokens, word_count

Mode = Literal["executive", "technical", "bullet", "abstract", "action_items", "meta_description"]
MODES: tuple[str, ...] = ("executive", "technical", "bullet", "abstract", "action_items", "meta_description")
META_MAX_CHARS = 155
META_MIN_CHARS = 50
MODE_DEFAULT_WORDS = {"executive": 250, "technical": 450, "bullet": 180, "abstract": 180, "action_items": 220,
                      "meta_description": 30}
MODE_KEY_POINTS = {"executive": 5, "technical": 7, "bullet": 7, "abstract": 4, "action_items": 4, "meta_description": 3}
LOCAL_PROVIDERS = ("ollama", "offline")
MAX_COLLAPSE_LEVELS = 6


# ----------------------------------------------------------------------------- contracts

class SummaryInput(BaseModel):
    source: str | None = Field(default=None, description="URL or file path (PDF, DOCX, Markdown, HTML, text, VTT/SRT transcript)")
    path: str | None = Field(default=None, description="File path (relative to the framework home), e.g. sample_data/docs/whitepaper.md")
    text: str | None = Field(default=None, description="Paste the text to summarise", json_schema_extra={"ui": "textarea"})
    html: str | None = Field(default=None, description="Raw HTML (use when the console cannot fetch the URL)", json_schema_extra={"ui": "textarea"})
    title: str | None = Field(default=None, description="Optional title override (useful for pasted text)")
    mode: Mode = Field(default="executive", description="executive | technical | bullet | abstract | action_items | meta_description")
    audience: str | None = Field(default=None, description="Who the summary is for, e.g. 'board members'")
    focus: str | None = Field(default=None, description="Optional angle to emphasise, e.g. 'claims automation'")
    max_words: int | None = Field(default=None, ge=20, le=2000, description="Target summary length (defaults per mode)")
    chunk_words: int = Field(default=900, ge=120, le=4000, description="Words per chunk in the parallel MAP step")
    reduce_budget_words: int = Field(default=3500, ge=400, le=20000,
                                     description="Max words of partial notes fed to one REDUCE call; above it notes are collapsed hierarchically")
    confidential: bool = Field(default=False, description="Only send text to an on-premise/local model (ollama); otherwise summarise extractively, locally")


class ActionItem(BaseModel):
    action: str
    owner: str | None = None
    due: str | None = None


class ChunkNotes(BaseModel):
    """MAP (and COLLAPSE) output: faithful notes for one part of the document."""

    key_points: list[str] = Field(default_factory=list, description="Up to 6 key points, one sentence each")
    facts: list[str] = Field(default_factory=list, description="Concrete facts and figures, numbers exactly as written")
    action_items: list[ActionItem] = Field(default_factory=list, description="Explicit recommendations / next steps")
    entities: list[str] = Field(default_factory=list, description="Organisations, products, people, places")


class SummaryDraft(BaseModel):
    """REDUCE output - one schema for every mode template."""

    title: str
    summary: str = Field(description="Main summary body in Markdown, written for the requested mode")
    key_points: list[str] = Field(default_factory=list)
    action_items: list[ActionItem] = Field(default_factory=list)
    abstract: str = ""
    meta_description: str = Field(default="", description="One sentence, at most 155 characters")


class SummaryOutput(BaseModel):
    title: str
    mode: str
    summary_markdown: str
    key_points: list[str]
    action_items: list[ActionItem]
    abstract: str
    meta_description: str = Field(description="<= 155 characters (validated deterministically)")
    chunks_processed: int
    source_words: int
    summary_words: int
    compression_ratio: float = Field(description="summary words / source words (0.05 = summary is 5% of the source)")
    method: Literal["abstractive", "extractive"]
    source: str = ""
    collapse_levels: int = 0


# ----------------------------------------------------------------------------- pure helpers (browser-portable)

_TS_PREFIX = re.compile(r"^\s*\[(\d{1,2}:\d{2}(?::\d{2})?)\]\s*(?:([^:\n\]]{1,80}?):\s+)?")
_PAGE_MARK = re.compile(r"\[page \d+\]", re.I)
_LIST_ITEM = re.compile(r"^(?:[-*+•]|\d+[.)])\s+")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MD_EMPH = re.compile(r"(\*\*|__|`)")
_NUM = re.compile(r"(?<![\w.])[$€£]?\d[\d,]*(?:\.\d+)?%?")
_ACTION = re.compile(
    r"\b(should|must|need to|needs to|have to|has to|will need to|recommend(?:s|ed)?|next steps?|action items?|"
    r"we will|we'll|plan to|plans to|is required to|are required to|ensure that|make sure|"
    r"will (?:publish|launch|deliver|send|share|release|introduce|roll out|complete|review|update|host|run|provide|"
    r"offer|contact|follow up|create|build|start|hold|issue|email|circulate))\b", re.I)
_IMPERATIVE = re.compile(
    r"^(please\s+)?(review|schedule|update|prepare|send|share|complete|submit|confirm|book|register|download|contact|call|"
    r"check|ensure|implement|adopt|launch|create|set up|follow up|document|install|test|train|start|stop|move|reduce|"
    r"increase|clear|photograph|keep|store|seal|inspect|replace|upgrade|join|visit|sign up)\b", re.I)
_DUE = re.compile(r"\b(by|before|within|no later than|until)\s+((?:the\s+)?(?:end of\s+)?(?:Q[1-4](?:\s+\d{4})?|"
                  r"\d{1,2}\s+\w+|\w+\s+\d{1,2}(?:,\s*\d{4})?|\d+\s+(?:days?|weeks?|months?)|\w+\s+\d{4}|"
                  r"(?:january|february|march|april|may|june|july|august|september|october|november|december|"
                  r"winter|spring|summer|autumn|fall|next\s+\w+|this\s+\w+|year-end|month-end)))", re.I)
_OWNER = re.compile(r"^(?:the\s+)?((?:[a-z][\w&-]*\s+){0,2}team|[A-Z][\w&-]*(?:\s+[A-Z][\w&-]*){0,3}|we|you)\s+"
                    r"(?:should|must|will|needs? to|has to|have to|plans? to|is required to|are required to)\b", re.I)
_NOT_OWNER = {"it", "this", "that", "there", "they", "these", "those", "which", "who", "what", "he", "she", "one"}


def clean_unit(text: str) -> str:
    """Strip markdown emphasis/links, page markers and transcript prefixes from a text unit."""
    t = _PAGE_MARK.sub(" ", text or "")
    t = _TS_PREFIX.sub("", t)
    t = _MD_LINK.sub(r"\1", t)
    t = _MD_EMPH.sub("", t)
    return normalize_ws(t)


def text_units(text: str) -> list[dict[str, Any]]:
    """Split a document into sentence units: headings, tables and code are skipped; list items and
    transcript turns stay separate; `lead` marks the first sentence after a heading."""
    units: list[dict[str, Any]] = []
    lead = True
    in_code = False

    def emit(paragraph: str, is_lead: bool) -> None:
        cleaned = clean_unit(paragraph)
        if is_aside(cleaned):  # a meta note / aside paragraph is skipped as a whole
            return
        for j, s in enumerate(sentences(cleaned)):
            if s:
                units.append({"text": s, "lead": is_lead and j == 0})

    for block in re.split(r"\n\s*\n", text or ""):
        buf: list[str] = []
        for raw in block.splitlines():
            s = raw.strip()
            if s.startswith("```"):
                in_code = not in_code
                continue
            if in_code or not s:
                continue
            if s.startswith("#"):
                if buf:
                    emit(" ".join(buf), lead)
                    buf, lead = [], False
                lead = True
                continue
            if s.startswith("|") or re.fullmatch(r"[-=*_]{3,}", s) or _PAGE_MARK.fullmatch(s):
                continue
            if _TS_PREFIX.match(s) or _LIST_ITEM.match(s):
                if buf:
                    emit(" ".join(buf), lead)
                    buf, lead = [], False
                emit(_LIST_ITEM.sub("", s, count=1), lead)
                lead = False
                continue
            buf.append(s)
        if buf:
            emit(" ".join(buf), lead)
            lead = False
    return units


def is_aside(text: str) -> bool:
    """Editorial asides / meta notes ('(Illustrative sample ...', '... transcripts.)') are not content."""
    t = (text or "").strip()
    return t.startswith("(") or t.lower().startswith(("illustrative", "disclaimer")) or (t.endswith((")", ").")) and "(" not in t)


def _starts_well(t: str) -> bool:
    c = t.lstrip("\"“‘'")[:1]
    return c.isupper() or c.isdigit()


def _title_like(t: str) -> bool:
    return (2 <= word_count(t) <= 24 and len(t) <= 140 and _starts_well(t)
            and not is_aside(t) and not _PAGE_MARK.search(t) and not t.endswith((".", ",", ";", ":")))


def clean_title(title: str, source: str = "", text: str = "") -> str:
    """Readable title: no transcript prefix / markdown. When the parser's guess is a fragment (e.g. a wrapped
    PDF line) or an aside, use the first title-like line of the document, then the file name."""
    t = clean_unit(re.sub(r"^#+\s*", "", title or ""))
    if t and not is_aside(t) and len(t) <= 140 and word_count(t) >= 2 and _starts_well(t):
        return t
    for line in (text or "").splitlines()[:25]:
        cand = clean_unit(re.sub(r"^#+\s*", "", line.strip()))
        if cand and _title_like(cand):
            return cand
    stem = re.sub(r"[_-]+", " ", (source or "").replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0]).strip()
    if stem and stem not in ("inline text", "inline html", "upstream output"):
        return stem.capitalize()
    return (t[:137] + "...") if len(t) > 140 else (t or "Untitled")


def _tfidf(texts: list[str]):
    from sklearn.feature_extraction.text import TfidfVectorizer

    vec = TfidfVectorizer(stop_words="english", sublinear_tf=True, ngram_range=(1, 2), min_df=1)
    return vec.fit_transform(texts)


def centrality_scores(texts: list[str]) -> list[float]:
    """Cosine similarity of each sentence to the document centroid (TF-IDF)."""
    if not texts:
        return []
    if len(texts) == 1:
        return [1.0]
    import numpy as np

    try:
        m = _tfidf(texts)
    except ValueError:  # empty vocabulary (all stop words)
        return [0.0] * len(texts)
    centroid = np.asarray(m.mean(axis=0)).ravel()
    norm = float(np.linalg.norm(centroid)) or 1.0
    sims = (m @ centroid) / norm
    return [float(x) for x in np.asarray(sims).ravel()]


def sentence_score(text: str, centrality: float, *, lead: bool = False, first_part: bool = False,
                   title: str = "", focus: str | None = None) -> float:
    """Deterministic importance score: centrality + position, numbers, title/focus overlap, length shape."""
    n = word_count(text)
    s = centrality
    if lead:
        s += 0.08
    if first_part and lead:
        s += 0.05
    if _NUM.search(text):
        s += 0.05
    toks = set(tokens(text))
    tt = set(tokens(title))
    if tt:
        s += 0.10 * len(toks & tt) / len(tt)
    if focus:
        ft = set(tokens(focus))
        if ft:
            s += 0.25 * len(toks & ft) / len(ft)
    if n < 6:
        s *= 0.3
    elif n < 9:
        s *= 0.7
    elif n > 50:
        s *= 0.6
    if text.rstrip().endswith("?"):
        s *= 0.6
    if is_aside(text):
        s *= 0.3  # editorial asides / meta notes are not content
    return round(s, 6)


def rank_candidates(text: str, part_index: int, *, title: str = "", focus: str | None = None, k: int = 8) -> list[dict[str, Any]]:
    """MAP-side extractive candidates for one chunk (top-k by local score, with document position)."""
    units = text_units(text)
    if not units:
        return []
    cent = centrality_scores([u["text"] for u in units])
    scored = []
    for i, (u, c) in enumerate(zip(units, cent)):
        scored.append({"text": u["text"], "score": sentence_score(u["text"], c, lead=u["lead"], first_part=part_index == 0,
                                                                  title=title, focus=focus),
                       "pos": [part_index, i], "lead": u["lead"]})
    scored.sort(key=lambda x: (-x["score"], x["pos"]))
    return scored[:k]


def mmr_select(texts: list[str], scores: list[float], k: int, lam: float = 0.7) -> list[int]:
    """Maximal marginal relevance: pick k items balancing score and novelty (near-duplicates > 0.8 skipped).
    Incremental max-similarity bookkeeping keeps it O(n * k)."""
    if not texts or k <= 0:
        return []
    import numpy as np
    from sklearn.metrics.pairwise import cosine_similarity

    n = len(texts)
    try:
        sim = cosine_similarity(_tfidf(texts))
    except ValueError:
        sim = np.eye(n)
    top = max(scores) or 1.0
    rel = np.asarray([s_ / top for s_ in scores], dtype=float)
    red = np.zeros(n)
    available = np.ones(n, dtype=bool)
    chosen: list[int] = []
    while len(chosen) < min(k, n):
        val = np.where(available & (red <= 0.8), lam * rel - (1 - lam) * red, -np.inf)
        best = int(np.argmax(val))
        if not np.isfinite(val[best]):
            break
        chosen.append(best)
        available[best] = False
        red = np.maximum(red, sim[best])
    return chosen


def detect_action_items(text: str, limit: int = 8) -> list[dict[str, Any]]:
    """Deterministic action-item detection (modal verbs, imperatives, 'next steps', due dates)."""
    out: list[dict[str, Any]] = []
    for u in text_units(text):
        s = u["text"]
        if word_count(s) < 6 or s.endswith("?") or is_aside(s) or re.search(r"[\"”]\s*[-–—]\s*[A-Z]", s) \
                or re.match(r"^(and|but|so|or|because|which)\b", s, re.I):
            continue
        hit = _ACTION.search(s) or _IMPERATIVE.match(s)
        if not hit:
            continue
        owner = None
        m = _OWNER.match(s)
        if m and m.group(1).split()[0].lower() not in _NOT_OWNER:
            owner = m.group(1)
        due = None
        d = _DUE.search(s)
        if d:
            due = normalize_ws(d.group(2)).rstrip(".,;")
        strength = 2 if _IMPERATIVE.match(s) or re.search(r"\b(must|need to|needs to|recommend)", s, re.I) else 1
        out.append({"action": s, "owner": owner, "due": due, "strength": strength})
    out.sort(key=lambda a: -a["strength"])
    return out[:limit]


def fit_meta_description(text: str, limit: int = META_MAX_CHARS) -> str:
    """Trim to <= limit chars, preferring a sentence end, then a clause boundary, then a word boundary."""
    t = normalize_ws(clean_unit(re.sub(r"^#+\s*", "", text or "")))
    t = t.strip('"“” ')
    if len(t) <= limit:
        return t
    window = t[: limit + 1]
    for pattern, keep_punct in ((r"[.!?](?=\s)", True), (r"[,;:–—-](?=\s)", False)):
        ends = [m.end() for m in re.finditer(pattern, window)]
        ends = [e for e in ends if e >= 70 and e <= limit]
        if ends:
            cut = window[: ends[-1]].rstrip()
            if not keep_punct:
                cut = cut.rstrip(",;:–—- ") + "."
                if len(cut) > limit:
                    continue
            return cut
    cut = t[: limit - 3].rsplit(" ", 1)[0].rstrip(",;:.–—- ")
    return cut + "..."


def numeric_tokens(text: str) -> list[str]:
    """Normalised numbers in a text (thousands separators and currency removed, % kept)."""
    out = []
    for m in _NUM.finditer(text or ""):
        v = m.group(0).lstrip("$€£").replace(",", "")
        if v.endswith("."):
            v = v[:-1]
        if v and v not in (".", "%"):
            out.append(v)
    return out


def ungrounded_numbers(summary_text: str, source_text: str) -> list[str]:
    """Numbers in the summary that never appear in the source (hallucination guard)."""
    src = set(numeric_tokens(source_text))
    src |= {v.rstrip("%") for v in src}
    missing = []
    for v in numeric_tokens(summary_text):
        if v in src or v.rstrip("%") in src:
            continue
        if v not in missing:
            missing.append(v)
    return missing


_TURN_START = re.compile(r"\n(?=[ \t]*\[\d{1,2}:\d{2})")


def looks_like_transcript(text: str) -> bool:
    return len(re.findall(r"(?m)^[ \t]*\[\d{1,2}:\d{2}", text or "")) >= 3


def transcript_paragraphs(text: str) -> str:
    """One paragraph per '[HH:MM:SS] Name:' turn, so semantic chunking never merges or splits turns."""
    return _TURN_START.sub("\n\n", text or "")


def chunk_body(chunk: str, index: int, overlap_words: int) -> str:
    """`chunk_text` starts every chunk after the first with an overlap tail (a bare run of words from the
    previous chunk) as its first paragraph. The model keeps it for continuity; extractive scoring drops it
    so no fragment or duplicate sentence is ever selected."""
    if index == 0 or overlap_words <= 0:
        return chunk
    _head, sep, rest = chunk.partition("\n\n")
    return rest if sep and rest.strip() else chunk


def chunk_sections(chunks: list[str]) -> list[str | None]:
    """Heading in force at the start of each chunk (first heading inside it, else the last one seen)."""
    out: list[str | None] = []
    current: str | None = None
    for c in chunks:
        heads = re.findall(r"^#{1,6}\s+(.+)$", c, flags=re.M)
        first_line = c.lstrip().splitlines()[0] if c.strip() else ""
        section = heads[0].strip() if heads and first_line.startswith("#") else current
        out.append(section)
        if heads:
            current = heads[-1].strip()
    return out


def notes_to_text(notes: dict[str, Any], label: str) -> str:
    lines = [f"[{label}]"]
    if notes.get("key_points"):
        lines.append("Key points:")
        lines += [f"- {p}" for p in notes["key_points"]]
    if notes.get("facts"):
        lines.append("Facts:")
        lines += [f"- {f}" for f in notes["facts"]]
    if notes.get("action_items"):
        lines.append("Actions:")
        for a in notes["action_items"]:
            extra = "; ".join(x for x in (f"owner: {a.get('owner')}" if a.get("owner") else "",
                                          f"due: {a.get('due')}" if a.get("due") else "") if x)
            lines.append(f"- {a.get('action', '')}" + (f" ({extra})" if extra else ""))
    if notes.get("entities"):
        lines.append("Entities: " + ", ".join(notes["entities"]))
    return "\n".join(lines)


def group_by_budget(items: list[str], budget_words: int) -> list[list[int]]:
    """Greedy consecutive packing of note blocks into groups of <= budget words."""
    groups: list[list[int]] = []
    cur: list[int] = []
    count = 0
    for i, t in enumerate(items):
        n = word_count(t)
        if cur and count + n > budget_words:
            groups.append(cur)
            cur, count = [], 0
        cur.append(i)
        count += n
    if cur:
        groups.append(cur)
    return groups


def extractive_notes(part: dict[str, Any]) -> dict[str, Any]:
    """Stand-in notes for a chunk whose model call failed (verbatim top sentences)."""
    return {"key_points": [c["text"] for c in part.get("candidates", [])[:4]],
            "facts": [c["text"] for c in part.get("candidates", []) if _NUM.search(c["text"])][:4],
            "action_items": [{"action": a["action"], "owner": a.get("owner"), "due": a.get("due")} for a in part.get("actions", [])[:3]],
            "entities": []}


def extractive_draft(parts: list[dict[str, Any]], *, mode: str, title: str, max_words: int,
                     focus: str | None = None) -> dict[str, Any]:
    """REDUCE without a model: global re-rank of the MAP candidates + MMR, composed per mode."""
    cands = [c for p in parts for c in p.get("candidates", [])]
    if not cands:
        return {"title": title, "summary": "", "key_points": [], "action_items": [], "abstract": "", "meta_description": ""}
    texts = [c["text"] for c in cands]
    cent = centrality_scores(texts)
    scores = [0.5 * c["score"] + 0.5 * sentence_score(c["text"], g, lead=c.get("lead", False), first_part=c["pos"][0] == 0,
                                                          title=title, focus=focus)
              for c, g in zip(cands, cent)]
    ranking = mmr_select(texts, scores, min(len(texts), 60))  # importance order, near-duplicates removed

    def take(pool: list[int], budget_words: int, max_items: int) -> list[int]:
        chosen, used = [], 0
        for i in pool:
            n = word_count(texts[i])
            if chosen and used + n > budget_words:
                continue
            chosen.append(i)
            used += n
            if len(chosen) >= max_items or used >= budget_words * 0.9:
                break
        return chosen

    # Abstract / body: the most central sentences, read in document order.
    abs_budget = min(150, max(40, max_words))
    abs_idx = sorted(take(ranking, abs_budget, 5), key=lambda i: cands[i]["pos"])
    abstract = " ".join(texts[i] for i in abs_idx)
    body_budget = max(40, int(max_words * 0.5)) if mode in ("executive", "technical") else abs_budget
    body_idx = sorted(take(ranking, body_budget, 4), key=lambda i: cands[i]["pos"])
    body = " ".join(texts[i] for i in body_idx)

    # Key points: the next most important sentences not already in the body (fall back to the full ranking).
    k = MODE_KEY_POINTS.get(mode, 5)
    in_body = set(body_idx) if mode in ("executive", "technical") else set()
    pool = [i for i in ranking if i not in in_body]
    key_idx = pool[:k] if len(pool) >= min(3, k) else ranking[:k]
    key_points = [texts[i] for i in key_idx]

    used_text = {canonical(texts[i]) for i in key_idx} | {canonical(texts[i]) for i in in_body}
    seen: set[str] = set()
    actions: list[dict[str, Any]] = []
    for a in sorted((a for p in parts for a in p.get("actions", [])), key=lambda a: -a.get("strength", 1)):
        key = canonical(a["action"])
        if key in seen or (key in used_text and mode != "action_items"):
            continue
        seen.add(key)
        actions.append({"action": a["action"], "owner": a.get("owner"), "due": a.get("due")})
        if len(actions) >= 8:
            break

    # Meta description: prefer the document's own lead (first part, first sentence after a heading), then the ranking.
    leads = [i for i in range(len(cands)) if cands[i]["pos"][0] == 0 and cands[i].get("lead")]
    meta_src = next((texts[i] for i in sorted(leads, key=lambda i: cands[i]["pos"]) if 70 <= len(texts[i]) <= META_MAX_CHARS), None) \
        or next((texts[i] for i in ranking if 70 <= len(texts[i]) <= META_MAX_CHARS), texts[ranking[0]] if ranking else title)
    return {"title": title, "summary": body, "key_points": key_points, "action_items": actions,
            "abstract": abstract, "meta_description": fit_meta_description(meta_src)}


def _action_line(a: dict[str, Any], checkbox: bool = False) -> str:
    meta = [x for x in ((f"owner: {a['owner']}" if a.get("owner") else ""), (f"due: {a['due']}" if a.get("due") else "")) if x]
    prefix = "- [ ] " if checkbox else "- "
    return prefix + a["action"].strip() + (f" _({'; '.join(meta)})_" if meta else "")


def compose_markdown(mode: str, draft: dict[str, Any]) -> str:
    """Deterministic layout of the final summary per mode."""
    title = draft.get("title") or "Summary"
    kp = [p for p in draft.get("key_points", []) if p]
    actions = draft.get("action_items", []) or []
    body = (draft.get("summary") or "").strip()
    if mode == "meta_description":
        return draft.get("meta_description", "")
    if mode == "abstract":
        return f"# {title}\n\n{(draft.get('abstract') or body).strip()}\n"
    if mode == "bullet":
        bullets = kp or [s for s in sentences(body)][:7]
        return f"# {title}\n\n" + "\n".join(f"- {b}" for b in bullets) + "\n"
    if mode == "action_items":
        lines = [f"# {title} - action items", ""]
        lines += [_action_line(a, checkbox=True) for a in actions] or ["_No explicit action items found in the source._"]
        if kp:
            lines += ["", "## Context", ""] + [f"- {p}" for p in kp]
        return "\n".join(lines) + "\n"
    heading = "Key details" if mode == "technical" else "Key points"
    lines = [f"# {title}", "", body]
    if kp:
        lines += ["", f"## {heading}", ""] + [f"- {p}" for p in kp]
    if actions:
        lines += ["", "## Recommended actions" if mode == "executive" else "## Actions and next steps", ""]
        lines += [_action_line(a) for a in actions]
    return "\n".join(lines).strip() + "\n"


def is_local_provider(llm: Any) -> bool:
    """True when the model runs inside your estate (ollama, offline, or an OpenAI-compatible localhost/private server)."""
    provider = getattr(llm, "provider", "")
    if provider in LOCAL_PROVIDERS:
        return True
    if provider == "openai":
        try:
            base = str(llm.settings.provider_config("openai").get("base_url") or "")
        except Exception:
            base = ""
        host = urlsplit(base).hostname or ""
        return host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local") or host.startswith(("10.", "192.168."))
    return False


_GATES: dict[int, threading.BoundedSemaphore] = {}
_GATES_LOCK = threading.Lock()


def _gate(ctx: RunContext) -> threading.BoundedSemaphore:
    """Bound concurrent MAP model calls (the fan-out itself is unbounded)."""
    n = max(1, int((ctx.settings.orchestrator or {}).get("max_parallel", 4) or 4))
    with _GATES_LOCK:
        return _GATES.setdefault(n, threading.BoundedSemaphore(n))


# ----------------------------------------------------------------------------- agent

@register
class ContentSummaryAgent(BaseAgent):
    spec = AgentSpec(
        id="content_summary",
        name="Content Summary",
        category=Category.CONTENT,
        summary="Summarises long documents, PDFs, pages and transcripts of any length (parallel map-reduce over semantic chunks) "
                "in executive, technical, bullet, abstract, action-item or meta-description mode.",
        kind=AgentKind.LLM,
        tier=Tier.BALANCED,
        connectors=["ingestion"],
        skills=["brand_voice"],
        keywords=["summary", "summarise", "summarize", "executive summary", "abstract", "tl;dr", "key points",
                  "meta description", "condense", "action items"],
        produces=["summary.md", "summary.json"],
        feeds=["webinar_followup_email", "content_adaptation", "blog_post_creation", "email_creation", "press_release_creation"],
    )
    Input = SummaryInput
    Output = SummaryOutput
    schemas = {"map": ChunkNotes, "collapse": ChunkNotes, **{m: SummaryDraft for m in MODES}}

    def selftest_input(self) -> dict[str, Any]:
        return {"path": "sample_data/docs/whitepaper.md", "mode": "executive", "chunk_words": 300}

    def stages(self):
        return [("ingest", self.ingest), ("chunk", self.chunk), ("map", self.map_chunk), ("reduce", self.reduce),
                ("format", self.format)]

    def build_graph(self) -> StateGraph:
        g = StateGraph(AgentState, context_schema=RunContext)
        for name, fn in self.stages():
            g.add_node(name, self.node(name, fn))
        g.add_edge(START, "ingest")
        g.add_edge("ingest", "chunk")
        g.add_conditional_edges("chunk", self.fan_out, ["map", "reduce"])
        g.add_edge("map", "reduce")
        g.add_edge("reduce", "format")
        g.add_edge("format", END)
        return g

    # -- ingest -----------------------------------------------------------------------------
    def ingest(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp = state["inputs"]
        conn = ctx.connector("ingestion")
        warnings: list[str] = []
        try:
            if inp.get("text"):
                doc = conn.load(text=inp["text"])
            elif inp.get("html"):
                doc = conn.load(html=inp["html"])
            elif inp.get("path"):
                doc = conn.load(path=inp["path"])
            elif inp.get("source"):
                doc = conn.load(inp["source"])
            else:
                upstream = ctx.upstream_text(max_chars=400_000)
                if not upstream:
                    raise ValueError("Nothing to summarise: provide source (URL or path), path, text or html.")
                doc = conn.load(text=upstream, filename="upstream-output")
                warnings.append("No source given - summarised the upstream agents' output.")
        except ValueError:
            raise
        except Exception as exc:  # unreadable / unreachable source is an input problem
            raise ValueError(f"Could not ingest the source: {exc}") from exc
        text = doc.text or ""
        n = word_count(text)
        if not n:
            raise ValueError(f"The source '{doc.source}' contains no extractable text.")
        checks = [Check(rule="ingest.parsed", status="info", severity="low", target=str(doc.source),
                        message=f"Parsed {doc.mime} '{normalize_ws(inp.get('title') or '') or clean_title(doc.title, str(doc.source), text)}' locally: {n:,} words"
                                + (f", {doc.metadata['pages']} pages" if doc.metadata.get("pages") else "") + ".")]
        if n < 60:
            checks.append(Check(rule="ingest.length", status="warn", severity="low",
                                message=f"Source is only {n} words; a summary adds little."))
        title = normalize_ws(inp.get("title") or "") or clean_title(doc.title, str(doc.source), text)
        return {"data": {"title": title, "source": str(doc.source), "mime": doc.mime, "text": text,
                         "source_words": n},
                "checks": checks, "warnings": warnings}

    # -- chunk ------------------------------------------------------------------------------
    def chunk(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        size = int(inp.get("chunk_words") or 900)
        overlap = min(60, size // 10)
        text = transcript_paragraphs(data["text"]) if looks_like_transcript(data["text"]) else data["text"]
        chunks = chunk_text(text, max_words=size, overlap_words=overlap)
        sections = chunk_sections([chunk_body(c, i, overlap) for i, c in enumerate(chunks)])
        warnings: list[str] = []
        reasons: list[str] = []
        use_model = not ctx.llm.offline
        if use_model and inp.get("confidential") and not is_local_provider(ctx.llm):
            use_model = False
            msg = (f"Confidential document: model provider '{ctx.llm.provider}' is not on-premise, so no text was sent to it; "
                   "a local extractive summary was produced (use the ollama provider for local abstractive summaries).")
            warnings.append(msg)
            reasons.append("confidential: extractive summary (no on-premise model configured)")
        if ctx.llm.offline:
            reasons.append("offline model: extractive summary (top-ranked verbatim sentences), not an abstractive summary")
        items = [{"index": i, "text": c, "body": chunk_body(c, i, overlap), "section": s}
                 for i, (c, s) in enumerate(zip(chunks, sections))]
        check = Check(rule="chunking.map_reduce", status="info", severity="low",
                      message=f"{len(items)} semantic chunk(s) of <= {size} words, summarised in parallel (map-reduce).",
                      data={"chunk_words": size, "chunks": len(items)})
        return {"data": {"chunks": items, "use_model": use_model}, "checks": [check], "warnings": warnings,
                "provenance": {"degraded_reasons": reasons} if reasons else {}}

    def fan_out(self, state: dict[str, Any]) -> list[Send] | list[str]:
        data, inp = state["data"], state["inputs"]
        chunks = data.get("chunks") or []
        if not chunks:
            return ["reduce"]
        common = {"title": data["title"], "total": len(chunks), "use_model": data["use_model"], "focus": inp.get("focus")}
        return [Send("map", {"data": {"chunk": c, **common}}) for c in chunks]

    # -- map (one task per chunk, in parallel) -------------------------------------------------
    def map_chunk(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        d = state["data"]
        c = d["chunk"]
        body = c.get("body") or c["text"]
        cands = rank_candidates(body, c["index"], title=d.get("title", ""), focus=d.get("focus"))
        actions = detect_action_items(body)
        notes = None
        warnings: list[str] = []
        if d.get("use_model"):
            try:
                with _gate(ctx):
                    notes = ctx.generate("map", ChunkNotes, chunk=c["text"], index=c["index"] + 1, total=d["total"],
                                         title=d.get("title"), section=c.get("section"), focus=d.get("focus")).model_dump()
            except Exception as exc:  # one failed chunk must not sink the document
                warnings.append(f"Part {c['index'] + 1}: model call failed ({type(exc).__name__}); extractive notes used for it.")
        part = {"index": c["index"], "section": c.get("section"), "words": word_count(body), "notes": notes,
                "candidates": cands, "actions": actions}
        return {"data": {f"map:{c['index']:05d}": part}, "warnings": warnings}

    # -- reduce -----------------------------------------------------------------------------
    def reduce(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        mode = inp.get("mode") or "executive"
        max_words = int(inp.get("max_words") or MODE_DEFAULT_WORDS[mode])
        parts = [data[k] for k in sorted(k for k in data if k.startswith("map:"))]
        warnings: list[str] = []
        reasons: list[str] = []
        levels = 0
        method = "abstractive" if data.get("use_model") and any(p.get("notes") for p in parts) else "extractive"
        draft: dict[str, Any] | None = None
        if method == "abstractive":
            blocks: list[dict[str, Any]] = []
            for p in parts:
                n = p["index"] + 1
                label = f"Part {n}" + (f" - {p['section']}" if p.get("section") else "")
                blocks.append({"first": n, "last": n, "text": notes_to_text(p.get("notes") or extractive_notes(p), label)})
            budget = int(inp.get("reduce_budget_words") or 3500)
            try:
                while (len(blocks) > 1 and word_count("\n\n".join(b["text"] for b in blocks)) > budget
                       and levels < MAX_COLLAPSE_LEVELS):
                    groups = group_by_budget([b["text"] for b in blocks], budget)
                    if len(groups) == len(blocks):  # every block alone fills the budget - pair them up
                        groups = [list(range(i, min(i + 2, len(blocks)))) for i in range(0, len(blocks), 2)]
                    merged: list[dict[str, Any]] = []
                    for grp in groups:
                        if len(grp) == 1:
                            merged.append(blocks[grp[0]])
                            continue
                        first, last = blocks[grp[0]]["first"], blocks[grp[-1]]["last"]
                        label = f"Parts {first}-{last}"
                        notes = ctx.generate("collapse", ChunkNotes, notes="\n\n".join(blocks[i]["text"] for i in grp),
                                             title=data["title"], label=label, total=len(parts))
                        merged.append({"first": first, "last": last, "text": notes_to_text(notes.model_dump(), label)})
                    blocks = merged
                    levels += 1
                result = ctx.generate(mode, SummaryDraft, notes="\n\n".join(b["text"] for b in blocks), title=data["title"],
                                      chunks=len(parts), source_words=data["source_words"], max_words=max_words,
                                      audience=inp.get("audience"), focus=inp.get("focus"),
                                      key_points=MODE_KEY_POINTS[mode], meta_max=META_MAX_CHARS)
                draft = result.model_dump()
            except Exception as exc:
                warnings.append(f"Model reduce failed ({type(exc).__name__}: {exc}); fell back to the extractive summary.")
                reasons.append("model reduce failed: extractive summary")
                method = "extractive"
        if draft is None:
            draft = extractive_draft(parts, mode=mode, title=data["title"], max_words=max_words, focus=inp.get("focus"))
        upd: dict[str, Any] = {"data": {"draft": draft, "method": method, "levels": levels, "chunks_processed": len(parts)},
                               "warnings": warnings}
        if reasons:
            upd["provenance"] = {"degraded_reasons": list(state.get("provenance", {}).get("degraded_reasons", [])) + reasons}
        return upd

    # -- format -----------------------------------------------------------------------------
    def format(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        mode = inp.get("mode") or "executive"
        max_words = int(inp.get("max_words") or MODE_DEFAULT_WORDS[mode])
        draft = dict(data["draft"])
        checks: list[Check] = []
        draft["title"] = normalize_ws(draft.get("title") or data["title"]) or data["title"]
        draft["key_points"] = [normalize_ws(p) for p in draft.get("key_points", []) if normalize_ws(p)][:10]
        draft["action_items"] = [ActionItem.model_validate(a).model_dump() for a in draft.get("action_items", [])
                                 if (a.get("action") if isinstance(a, dict) else getattr(a, "action", ""))][:12]

        # meta description: deterministic <= 155 chars
        raw_meta = normalize_ws(draft.get("meta_description") or "") or (draft["key_points"][0] if draft["key_points"] else draft["title"])
        meta = fit_meta_description(raw_meta)
        if len(normalize_ws(raw_meta)) > META_MAX_CHARS:
            checks.append(Check(rule="meta_description.length", status="warn", severity="medium", ref="SERP snippet limit",
                                message=f"Meta description was {len(normalize_ws(raw_meta))} chars; trimmed to {len(meta)} (limit {META_MAX_CHARS})."))
        else:
            checks.append(Check(rule="meta_description.length", status="pass", severity="low", ref="SERP snippet limit",
                                message=f"Meta description is {len(meta)} chars (limit {META_MAX_CHARS})."))
        if len(meta) < META_MIN_CHARS:
            checks.append(Check(rule="meta_description.min_length", status="warn", severity="low",
                                message=f"Meta description is only {len(meta)} chars; aim for {META_MIN_CHARS}-{META_MAX_CHARS}."))
        draft["meta_description"] = meta
        if not normalize_ws(draft.get("abstract") or ""):
            draft["abstract"] = " ".join(sentences(draft.get("summary") or "")[:4])

        md = compose_markdown(mode, draft)
        body_for_count = md if mode != "meta_description" else meta
        summary_words = word_count(body_for_count)
        ratio = round(summary_words / max(1, data["source_words"]), 4)

        if data["method"] == "abstractive":
            missing = ungrounded_numbers(" ".join([md, draft["abstract"], meta]), data["text"])
            checks.append(Check(rule="summary.numbers_grounded", status="warn" if missing else "pass", severity="high" if missing else "low",
                                message=(f"Numbers not found in the source - verify before use: {missing}" if missing
                                         else "Every number in the summary appears in the source."), data={"ungrounded": missing}))
        else:
            checks.append(Check(rule="summary.extractive", status="info", severity="low",
                                message="Extractive summary: every sentence is verbatim from the source."))
        if mode not in ("meta_description", "bullet", "action_items") and summary_words > max_words * 1.35 + 40:
            checks.append(Check(rule="summary.length", status="warn", severity="low",
                                message=f"Summary is {summary_words} words; target was about {max_words}."))
        if mode in ("executive", "technical", "bullet", "abstract"):
            n = len(draft["key_points"])
            checks.append(Check(rule="summary.key_points", status="pass" if n >= 3 else "warn", severity="low",
                                message=f"{n} key point(s)."))
        banned = [p for p in ctx.brand.get("banned_phrases", []) if p.lower() in md.lower()]
        if banned and data["method"] == "abstractive":
            checks.append(Check(rule="brand.banned_phrases", status="fail", severity="high", message=f"Banned phrases used: {banned}"))

        out = SummaryOutput(title=draft["title"], mode=mode, summary_markdown=md, key_points=draft["key_points"],
                            action_items=[ActionItem.model_validate(a) for a in draft["action_items"]],
                            abstract=normalize_ws(draft["abstract"]), meta_description=meta,
                            chunks_processed=int(data.get("chunks_processed", 0)), source_words=int(data["source_words"]),
                            summary_words=summary_words, compression_ratio=ratio, method=data["method"],
                            source=data.get("source", ""), collapse_levels=int(data.get("levels", 0)))
        artifacts = [ctx.artifacts.write_text("summary.md", md, "text/markdown", f"{mode} summary"),
                     ctx.artifacts.write_json("summary.json", out.model_dump(mode="json"), "Summary (structured)")]
        label = mode.replace("_", " ")
        summary = (f"{label.capitalize()} summary of '{truncate_title(draft['title'])}': {data['source_words']:,} words -> "
                   f"{summary_words} ({ratio:.1%}) from {out.chunks_processed} chunk(s), {data['method']}")
        return {"output": out.model_dump(mode="json"), "artifacts": artifacts, "checks": checks, "summary": summary}


def truncate_title(t: str, n: int = 70) -> str:
    return t if len(t) <= n else t[: n - 3].rsplit(" ", 1)[0] + "..."
