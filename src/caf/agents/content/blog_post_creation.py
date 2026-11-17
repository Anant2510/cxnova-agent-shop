"""Blog Post Creation - a multi-stage chain instead of one call:

    research -> outline -> draft -> critique -> (revise -> critique)* -> finalize

Research uses your own SearchConnector, crawled reference pages and upstream context (keyword
clusters, campaign brief, content summary); notes keep their source URLs and the draft cites them
as [S1]...[Sn]. A reviewer template scores the draft; deterministic guardrails (word count,
heading structure, keyword placement, Flesch reading ease, brand banned phrases, claims that need
a source) are computed in code. critique -> revise loops at most `max_revisions` times while the
score is below the threshold (never with the offline model). Publishing is a vendor-neutral
CmsConnector action that always waits for human approval.
"""

from __future__ import annotations

import html as _html
import re
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from ...core import AgentKind, AgentSpec, AgentState, BaseAgent, Category, Check, PendingAction, RunContext, Tier, register
from ...core.textutil import STOPWORDS, normalize_ws, slugify, truncate, words

# ----------------------------------------------------------------------------- contract


class BlogInput(BaseModel):
    topic: str = Field(min_length=3, description="Topic or working title of the post")
    brief: str | None = Field(default=None, description="Angle, brief or notes the post must reflect", json_schema_extra={"ui": "textarea"})
    primary_keyword: str | None = Field(default=None, description="Primary SEO keyword (taken from Keyword Research upstream when omitted)")
    secondary_keywords: list[str] = Field(default_factory=list, description="Secondary keywords to work in naturally")
    audience: str | None = Field(default=None, description="Who the post is for")
    target_word_count: int = Field(default=1200, ge=300, le=4000, description="Target length of the body in words")
    source_urls: list[str] = Field(default_factory=list, description="Reference pages to research (fetched with the crawler)")
    author: str | None = Field(default=None, description="Byline stored in the CMS fields")
    max_revisions: int = Field(default=1, ge=0, le=3, description="Maximum critique -> revise loops")
    score_threshold: int = Field(default=80, ge=0, le=100, description="Revise while the review score is below this")
    min_reading_ease: float = Field(default=55.0, ge=0, le=100, description="Minimum Flesch reading ease (60+ = plain English)")
    publish_status: Literal["draft", "published"] = Field(default="draft", description="CMS status requested on approval")
    publish: bool = Field(default=True, description="Propose a CMS publish action (always held for human approval)")


class OutlineSection(BaseModel):
    heading: str = Field(description="H2 heading, phrased the way readers search")
    key_points: list[str] = Field(default_factory=list, max_length=6)
    source_ids: list[str] = Field(default_factory=list, description="Research note ids (S1, S2 ...) this section draws on")


class Outline(BaseModel):
    title: str = Field(description="Title / H1 containing the primary keyword, max ~65 characters")
    meta_description: str = Field(description="120-155 character meta description with the primary keyword")
    sections: list[OutlineSection] = Field(min_length=3, max_length=10)


class CritiqueIssue(BaseModel):
    category: Literal["brand", "factual", "structure", "seo", "readability", "style", "other"] = "other"
    severity: Literal["low", "medium", "high"] = "medium"
    issue: str
    suggestion: str = ""


class Critique(BaseModel):
    score: int = Field(ge=0, le=100, description="Overall editorial quality 0-100")
    issues: list[CritiqueIssue] = Field(default_factory=list, max_length=12)
    strengths: list[str] = Field(default_factory=list, max_length=5)


class Readability(BaseModel):
    flesch_reading_ease: float
    flesch_kincaid_grade: float
    words: int
    sentences: int
    avg_sentence_words: float
    long_sentences: int


class SourceRef(BaseModel):
    id: str
    number: int | None = None
    title: str
    url: str
    publisher: str | None = None
    published: str | None = None
    origin: str
    cited: bool = False


class CritiqueResult(BaseModel):
    score: int
    model_score: int | None = None
    guardrail_score: int
    threshold: int
    passed: bool
    issues: list[CritiqueIssue] = Field(default_factory=list)
    strengths: list[str] = Field(default_factory=list)


class BlogOutput(BaseModel):
    title: str
    slug: str
    meta_description: str
    primary_keyword: str
    secondary_keywords: list[str] = Field(default_factory=list)
    outline: list[OutlineSection]
    markdown: str
    word_count: int
    readability: Readability
    sources: list[SourceRef] = Field(default_factory=list)
    critique: CritiqueResult
    revisions_applied: int
    score_history: list[int] = Field(default_factory=list)
    publish_status: str
    author: str | None = None


# ----------------------------------------------------------------------------- deterministic guardrails

_CITE = re.compile(r"\[(S\d+)\]")
_LINK = re.compile(r"\[[^\]]+\]\([^)]+\)")
CLAIM_PATTERNS = [
    r"\d+(?:\.\d+)?\s?%", r"\b\d+(?:\.\d+)?\s?(?:percent|per cent)\b", r"[$£€]\s?\d", r"\b\d[\d,.]*\s?(?:million|billion|thousand)\b",
    r"\b(?:studies|study|research|data|surveys?|statistics|experts?|analysis)\s+(?:show|shows|showed|suggest|suggests|find|finds|found|say|says|reveal|reveals)\b",
    r"\baccording to\b", r"\b\d+(?:\.\d+)?\s?(?:times|x)\s+(?:more|less|higher|lower|likely|faster)\b",
    r"\b(?:one|two|three|four|five|\d+) in (?:every )?(?:\d+|two|three|four|five|ten)\b", r"\b(?:on average|the average)\b.*\d",
]


def count_syllables(word: str) -> int:
    """Heuristic English syllable count (silent endings, vowel groups)."""
    w = re.sub(r"[^a-z]", "", word.lower())
    if not w:
        return 0
    if len(w) <= 3:
        return 1
    w = re.sub(r"(?:[^laeiouy]es|(?<![td])ed|[^laeiouy]e)$", "", w)
    w = re.sub(r"^y", "", w)
    return max(1, len(re.findall(r"[aeiouy]{1,2}", w)))


def plain_text(markdown_text: str, include_headings: bool = False) -> str:
    """Markdown -> prose for readability (citations, links, emphasis and list markers removed)."""
    out = []
    for line in (markdown_text or "").splitlines():
        s = line.strip()
        if not s:
            out.append("")
            continue
        if s.startswith("#"):
            if include_headings:
                out.append(s.lstrip("#").strip() + ".")
            continue
        s = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", s)
        s = _CITE.sub("", s)
        s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
        s = re.sub(r"[*_`>]", "", s)
        if line.lstrip()[:2] in ("- ", "* ", "+ ") or re.match(r"^\s*\d+[.)]\s", line):
            s = s if re.search(r"[.!?:]$", s) else s + "."
        out.append(s)
    return "\n".join(out)


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|\n+", text or "")
    return [p.strip() for p in parts if p and re.search(r"[A-Za-z0-9]", p)]


def readability(markdown_text: str) -> dict[str, Any]:
    """Flesch reading ease + Flesch-Kincaid grade computed in code (never by the model)."""
    sents = split_sentences(plain_text(markdown_text))
    ws = [w for s in sents for w in words(s)]
    n_w, n_s = len(ws), max(1, len(sents))
    if not n_w:
        return {"flesch_reading_ease": 0.0, "flesch_kincaid_grade": 0.0, "words": 0, "sentences": 0, "avg_sentence_words": 0.0, "long_sentences": 0}
    syl = sum(count_syllables(w) for w in ws)
    wps, spw = n_w / n_s, syl / n_w
    return {"flesch_reading_ease": round(206.835 - 1.015 * wps - 84.6 * spw, 1),
            "flesch_kincaid_grade": round(0.39 * wps + 11.8 * spw - 15.59, 1), "words": n_w, "sentences": len(sents),
            "avg_sentence_words": round(wps, 1), "long_sentences": sum(1 for s in sents if len(words(s)) > 28)}


def headings(markdown_text: str) -> list[tuple[int, str]]:
    return [(len(m.group(1)), m.group(2).strip()) for m in re.finditer(r"^(#{1,6})\s+(.+?)\s*#*\s*$", markdown_text or "", flags=re.M)]


def _toks(text: str) -> list[str]:
    out = []
    for w in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if len(w) > 4 and w.endswith("ies"):
            w = w[:-3] + "y"
        elif len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
            w = w[:-1]
        out.append(w)
    return out


def contains_phrase(text: str, phrase: str) -> bool:
    """Case/plural-insensitive whole-phrase match ('home insurance claims' contains 'home insurance claim')."""
    t, p = _toks(text), _toks(phrase)
    return bool(p) and any(t[i:i + len(p)] == p for i in range(len(t) - len(p) + 1))


def first_words(markdown_text: str, n: int = 100) -> str:
    return " ".join(words(plain_text(markdown_text))[:n])


def unsourced_claims(markdown_text: str) -> list[str]:
    """Sentences with statistics / research claims and no [S#] citation or link."""
    out = []
    for line in (markdown_text or "").splitlines():
        if line.strip().startswith("#"):
            continue
        for s in re.split(r"(?<=[.!?])\s+", line):
            if any(re.search(p, s, flags=re.I) for p in CLAIM_PATTERNS) and not _CITE.search(s) and not _LINK.search(s):
                out.append(truncate(normalize_ws(re.sub(r"[*_`]", "", s)), 160))
    return out


def strip_h1(body: str) -> str:
    """The H1 is the title, added in code; drop any H1 the model wrote."""
    return re.sub(r"^\s*#\s+[^\n]*\n?", "", body or "", count=1).strip() if re.match(r"^\s*#\s", body or "") else (body or "").strip()


def guardrails(*, title: str, body: str, meta_description: str, primary_keyword: str, secondary_keywords: list[str],
               target_words: int, min_reading_ease: float, banned_phrases: list[str], source_ids: list[str]) -> tuple[list[Check], int, dict[str, Any]]:
    """Deterministic editorial checks; returns (checks, guardrail score 0-100, readability stats)."""
    checks: list[Check] = []
    stats = readability(body)
    wc = stats["words"]
    dev = abs(wc - target_words) / max(1, target_words)
    checks.append(Check(rule="length.word_count", status="pass" if dev <= 0.2 else ("warn" if dev <= 0.4 else "fail"),
                        severity="medium", message=f"{wc} words (target {target_words} +/-20%).", data={"words": wc}))
    hs = headings(body)
    h1 = [h for lvl, h in hs if lvl == 1]
    h2 = [h for lvl, h in hs if lvl == 2]
    checks.append(Check(rule="structure.h1", status="pass" if not h1 and title.strip() else "fail", severity="high",
                        message="Exactly one H1 (the title)." if not h1 else f"Body contains extra H1 headings: {h1[:2]}"))
    checks.append(Check(rule="structure.h2", status="pass" if len(h2) >= 3 else ("warn" if h2 else "fail"), severity="medium",
                        message=f"{len(h2)} H2 sections (minimum 3)."))
    skips = [h for (l0, _), (l1, h) in zip([(1, title)] + hs, hs) if l1 > l0 + 1]
    if skips:
        checks.append(Check(rule="structure.heading_levels", status="warn", severity="low", message=f"Skipped heading levels at: {skips[:3]}"))
    long_h = [h for _, h in hs if len(h) > 70]
    if long_h:
        checks.append(Check(rule="structure.heading_length", status="warn", severity="low", message=f"Headings over 70 characters: {long_h[:2]}"))
    kw = primary_keyword
    checks.append(Check(rule="seo.keyword_title_h1", status="pass" if contains_phrase(title, kw) else "fail", severity="high",
                        message=f"Primary keyword '{kw}' {'is' if contains_phrase(title, kw) else 'is not'} in the title / H1."))
    intro_ok = contains_phrase(first_words(body, 100), kw)
    checks.append(Check(rule="seo.keyword_intro", status="pass" if intro_ok else "fail", severity="medium",
                        message=f"Primary keyword {'appears' if intro_ok else 'does not appear'} in the first 100 words."))
    ml = len(meta_description)
    meta_ok = 120 <= ml <= 160
    checks.append(Check(rule="seo.meta_description", status="pass" if meta_ok and contains_phrase(meta_description, kw) else "warn",
                        severity="low", message=f"Meta description {ml} chars (120-160)"
                        + ("" if contains_phrase(meta_description, kw) else "; missing the primary keyword") + "."))
    if secondary_keywords:
        used = [k for k in secondary_keywords if contains_phrase(body, k)]
        checks.append(Check(rule="seo.secondary_keywords", status="info", severity="low",
                            message=f"{len(used)}/{len(secondary_keywords)} secondary keywords used.", data={"used": used}))
    fre = stats["flesch_reading_ease"]
    checks.append(Check(rule="readability.flesch", status="pass" if fre >= min_reading_ease else "warn", severity="medium",
                        message=f"Flesch reading ease {fre} (minimum {min_reading_ease}); grade {stats['flesch_kincaid_grade']}."))
    low = f"{title} {meta_description} {body}".lower()
    banned = [p for p in banned_phrases if p.lower() in low]
    checks.append(Check(rule="brand.banned_phrases", status="fail" if banned else "pass", severity="high",
                        message=f"Brand-banned phrases: {banned}" if banned else "No brand-banned phrases."))
    claims = unsourced_claims(body)
    checks.append(Check(rule="facts.claims_need_source", status="warn" if claims else "pass", severity="high",
                        message=f"{len(claims)} claim(s) with statistics or research language but no citation." if claims
                        else "Every statistic / research claim is cited.", data={"sentences": claims[:6]}))
    unknown = sorted(set(_CITE.findall(body)) - set(source_ids))
    if unknown:
        checks.append(Check(rule="facts.unknown_sources", status="fail", severity="high",
                            message=f"Citations to research notes that do not exist: {unknown}"))
    penalty = sum(12 if c.status == "fail" else 4 if c.status == "warn" else 0 for c in checks)
    return checks, max(0, 100 - penalty), stats


def render_citations(body: str, notes: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """[S3] markers -> numbered links in order of first citation, plus a Sources section."""
    by_id = {n["id"]: n for n in notes}
    order: list[str] = []
    for sid in _CITE.findall(body):
        if sid in by_id and sid not in order:
            order.append(sid)
    num = {sid: i for i, sid in enumerate(order, 1)}

    def repl(m: re.Match) -> str:
        sid = m.group(1)
        return f"[[{num[sid]}]]({by_id[sid]['url']})" if sid in num else ""

    rendered = re.sub(r"\s?\[(S\d+)\]", lambda m: (" " + repl(m)) if m.group(1) in num else "", body)
    sources = []
    for n in notes:
        sources.append({**n, "number": num.get(n["id"]), "cited": n["id"] in num})
    if order:
        lines = [f"{num[sid]}. [{by_id[sid]['title']}]({by_id[sid]['url']})" + (f" - {by_id[sid]['publisher']}" if by_id[sid].get("publisher") else "")
                 for sid in order]
        rendered = rendered.rstrip() + "\n\n## Sources\n\n" + "\n".join(lines)
    return rendered, sources


def fix_meta(meta: str, body: str) -> str:
    """Deterministic meta-description repair: build from the intro when missing, cap at 160 chars."""
    meta = normalize_ws(re.sub(r"[*_`#]", "", meta or ""))
    if len(meta) < 50:
        meta = normalize_ws(" ".join(split_sentences(plain_text(body))[:2]))
    return truncate(meta, 158, "...") if len(meta) > 160 else meta


def default_keyword(topic: str) -> str:
    content = [w for w in re.findall(r"[a-z0-9']+", topic.lower()) if w not in STOPWORDS and w not in {"your", "my"}]
    return " ".join(content[:4]) or topic.lower()[:60]


def article_html(title: str, markdown_text: str, meta: str) -> str:
    from ...core.emailkit import md_to_html

    body = md_to_html(re.sub(r"^#\s+[^\n]*\n", "", markdown_text, count=1))
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{_html.escape(title)}</title><meta name="description" content="{_html.escape(meta)}">'
            f'<style>body{{font-family:Georgia,serif;max-width:720px;margin:40px auto;padding:0 20px;line-height:1.7;color:#1d2530}}'
            f'h1,h2,h3{{font-family:system-ui,Arial,sans-serif}}a{{color:#0b5cad}}</style></head>'
            f'<body><article><h1>{_html.escape(title)}</h1>{body}</article></body></html>')


# ----------------------------------------------------------------------------- agent

@register
class BlogPostCreationAgent(BaseAgent):
    spec = AgentSpec(
        id="blog_post_creation",
        name="Blog Post Creation",
        category=Category.CONTENT,
        summary="Researches, outlines, drafts, self-critiques and revises an on-brand long-form blog post with SEO, readability and sourcing guardrails, then proposes a CMS publish for human approval.",
        kind=AgentKind.LLM,
        tier=Tier.BALANCED,
        connectors=["search", "crawler", "cms"],
        skills=["brand_voice"],
        keywords=["blog", "blog post", "article", "long-form", "thought leadership", "seo article", "pillar page", "post"],
        produces=["blog.md", "blog.html", "research_notes.json"],
        feeds=["content_adaptation", "content_translation", "quote_extraction", "content_summary"],
        side_effects=["cms.publish"],
    )
    Input = BlogInput
    Output = BlogOutput
    schemas = {"outline": Outline, "critique": Critique}

    def selftest_input(self) -> dict[str, Any]:
        return {"topic": "How to make a home insurance claim after storm damage", "primary_keyword": "home insurance claim",
                "secondary_keywords": ["storm damage", "claims adjuster"], "audience": "Homeowners preparing for winter storms",
                "target_word_count": 900, "author": "Maya Okafor, Senior Claims Adjuster",
                "source_urls": ["https://www.meridian-mutual.example/blog/claims-tips.html"]}

    def stages(self):
        return [("research", self.research), ("outline", self.outline), ("draft", self.draft), ("critique", self.critique),
                ("revise", self.revise), ("finalize", self.finalize)]

    def build_graph(self) -> StateGraph:
        g = StateGraph(AgentState, context_schema=RunContext)
        for name, fn in self.stages():
            g.add_node(name, self.node(name, fn))
        g.add_edge(START, "research")
        g.add_edge("research", "outline")
        g.add_edge("outline", "draft")
        g.add_edge("draft", "critique")
        g.add_conditional_edges("critique", route_after_critique, {"revise": "revise", "finalize": "finalize"})
        g.add_edge("revise", "critique")
        g.add_edge("finalize", END)
        return g

    # -- research ---------------------------------------------------------------------------------
    def research(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp = state["inputs"]
        warnings: list[str] = []
        notes: list[dict[str, Any]] = []
        context: list[str] = []
        primary, secondary = (inp.get("primary_keyword") or "").strip(), [s for s in inp.get("secondary_keywords") or [] if s.strip()]
        kr = ctx.upstream_output("keyword_research")
        if kr and kr.get("clusters"):
            cluster = max(kr["clusters"], key=lambda c: (len(set(_toks(c.get("pillar_keyword", ""))) & set(_toks(primary or inp["topic"]))),
                                                         c.get("opportunity_score", 0)))
            if not primary:
                primary = cluster.get("pillar_keyword", "")
            if not secondary:
                secondary = [k["keyword"] for k in cluster.get("keywords", []) if k.get("keyword") != primary][:5]
            context.append(f"Keyword cluster '{cluster.get('name')}': {', '.join(k['keyword'] for k in cluster.get('keywords', [])[:8])}")
        primary = primary or default_keyword(inp["topic"])
        brief = ctx.upstream_output("campaign_brief_generation") or {}
        key_messages = list(brief.get("key_messages") or [])[:5]
        audience = inp.get("audience") or (brief.get("target_audience") or {}).get("primary")
        summary = ctx.upstream_output("content_summary") or {}
        summary_text = next((summary.get(k) for k in ("summary_markdown", "summary", "executive_summary") if isinstance(summary.get(k), str)), "")
        if summary_text:
            notes.append({"title": summary.get("title") or "Upstream content summary", "url": summary.get("source") or "upstream:content_summary",
                          "publisher": "content_summary", "published": None, "origin": "upstream", "text": truncate(summary_text, 900)})
        crawler = ctx.connector("crawler") if inp.get("source_urls") else None
        for url in inp.get("source_urls") or []:
            try:
                page = crawler.fetch(url)
                if page.ok and page.is_html:
                    soup = page.soup()
                    t = soup.find("h1") or soup.title
                    pub = soup.find("meta", attrs={"property": "article:published_time"})
                    notes.append({"title": normalize_ws(t.get_text(" ")) if t else url, "url": page.final_url, "publisher": _host(page.final_url),
                                  "published": pub.get("content") if pub else None, "origin": "reference", "text": truncate(normalize_ws(page.text()), 1500)})
                else:
                    warnings.append(f"Reference {url} returned status {page.status}; skipped.")
            except Exception as exc:
                warnings.append(f"Reference {url} could not be fetched: {type(exc).__name__}")
        reasons: list[str] = []
        try:
            search = ctx.connector("search")
            if search.available:
                for r in search.search(f"{inp['topic']} {primary}".strip(), num=6):
                    notes.append({"title": r.title, "url": r.url, "publisher": r.source, "published": r.published, "origin": "search",
                                  "text": truncate(normalize_ws(r.snippet or ""), 400)})
            else:
                warnings.append("No search provider configured: research used references and upstream context only.")
        except Exception as exc:
            warnings.append(f"Search failed: {type(exc).__name__}: {exc}")
        uniq: dict[str, dict[str, Any]] = {}
        for n in notes:
            uniq.setdefault(n["url"].split("#")[0].rstrip("/"), n)
        notes = list(uniq.values())[:10]
        for i, n in enumerate(notes, 1):
            n["id"] = f"S{i}"
        if not notes:
            reasons.append("no research sources (search 'none' and no references) - facts rely on model knowledge and need verification")
        prev = list((state.get("provenance") or {}).get("degraded_reasons", []))
        return {"data": {"notes": notes, "primary": primary, "secondary": secondary, "key_messages": key_messages, "audience": audience,
                         "context": context, "revisions": 0, "scores": []},
                "warnings": warnings, "provenance": {"degraded_reasons": prev + reasons, "research_notes": len(notes)}}

    # -- outline ----------------------------------------------------------------------------------
    def outline(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        ol = ctx.generate("outline", Outline, topic=inp["topic"], brief=inp.get("brief"), primary_keyword=data["primary"],
                          secondary_keywords=data["secondary"], audience=data["audience"], target_word_count=inp["target_word_count"],
                          notes=_note_lines(data["notes"]), key_messages=data["key_messages"], context=data["context"])
        known = {n["id"] for n in data["notes"]}
        sections = []
        for s in ol.sections:
            heading = normalize_ws(re.sub(r"^(?:#+|H\d:?)\s*", "", s.heading))[:90]
            sections.append({"heading": heading, "key_points": [normalize_ws(p) for p in s.key_points if p.strip()][:6],
                             "source_ids": [i for i in (x.strip().upper() for x in s.source_ids) if i in known]})
        title = normalize_ws(re.sub(r"^#+\s*", "", ol.title))[:120]
        return {"data": {"title": title, "meta": ol.meta_description, "outline": sections}}

    # -- draft ------------------------------------------------------------------------------------
    def draft(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        body = ctx.generate("draft", None, topic=inp["topic"], title=data["title"], brief=inp.get("brief"), primary_keyword=data["primary"],
                            secondary_keywords=data["secondary"], audience=data["audience"], target_word_count=inp["target_word_count"],
                            outline=data["outline"], notes=_note_lines(data["notes"]), key_messages=data["key_messages"])
        return {"data": {"body": strip_h1(body)}}

    # -- critique (reviewer template + deterministic guardrails) ----------------------------------
    def critique(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        checks, g_score, stats = self._guardrails(inp, data, ctx)
        failing = [f"{c.rule}: {c.message}" for c in checks if c.status in ("fail", "warn")]
        review = ctx.generate("critique", Critique, title=data["title"], body=data["body"], primary_keyword=data["primary"],
                              audience=data["audience"], notes=_note_lines(data["notes"]), guardrail_findings=failing,
                              target_word_count=inp["target_word_count"])
        model_score = None if ctx.llm.offline else int(review.score)
        score = g_score if model_score is None else min(model_score, g_score)
        revisions = int(data.get("revisions", 0))
        revise = (not ctx.llm.offline) and score < int(inp["score_threshold"]) and revisions < int(inp["max_revisions"])
        return {"data": {"review": review.model_dump(), "guardrail_score": g_score, "model_score": model_score, "score": score,
                         "guardrail_findings": failing, "scores": list(data.get("scores", [])) + [score],
                         "next_step": "revise" if revise else "finalize"}}

    # -- revise -----------------------------------------------------------------------------------
    def revise(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        issues = list(data.get("guardrail_findings", [])) + [f"{i['category']} ({i['severity']}): {i['issue']}"
                                                             + (f" - fix: {i['suggestion']}" if i.get("suggestion") else "")
                                                             for i in data["review"].get("issues", [])]
        body = ctx.generate("revise", None, title=data["title"], body=data["body"], issues=issues, primary_keyword=data["primary"],
                            target_word_count=inp["target_word_count"], notes=_note_lines(data["notes"]))
        return {"data": {"body": strip_h1(body) or data["body"], "revisions": int(data.get("revisions", 0)) + 1}}

    # -- finalize: render citations, artifacts, gated CMS publish ---------------------------------
    def finalize(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        data = {**data, "meta": fix_meta(data["meta"], data["body"])}
        checks, g_score, stats = self._guardrails(inp, data, ctx)
        model_score = data.get("model_score")
        score = g_score if model_score is None else min(model_score, g_score)
        body, sources = render_citations(data["body"], data["notes"])
        title = data["title"]
        markdown_text = f"# {title}\n\n{body.strip()}\n"
        slug = slugify(data["primary"] if len(data["primary"]) >= 8 else title, 60)
        threshold = int(inp["score_threshold"])
        review = Critique.model_validate(data["review"])
        result = CritiqueResult(score=score, model_score=model_score, guardrail_score=g_score, threshold=threshold, passed=score >= threshold,
                                issues=review.issues, strengths=review.strengths)
        revisions = int(data.get("revisions", 0))
        checks.append(Check(rule="review.score", status="pass" if result.passed else "warn", severity="medium",
                            message=f"Review score {score}/100 (threshold {threshold}) after {revisions} revision(s)"
                                    + (" - revision loop skipped with the offline model." if ctx.llm.offline else ".")))
        artifacts = [ctx.artifacts.write_text("blog.md", markdown_text, "text/markdown", "Blog post (Markdown)"),
                     ctx.artifacts.write_text("blog.html", article_html(title, markdown_text, data["meta"]), "text/html", "Blog post preview (HTML)"),
                     ctx.artifacts.write_json("research_notes.json", data["notes"], "Research notes with source URLs")]
        actions: list[PendingAction] = []
        if inp.get("publish", True):
            fields = {"meta_description": data["meta"], "author": inp.get("author"), "primary_keyword": data["primary"],
                      "secondary_keywords": data["secondary"], "word_count": stats["words"],
                      "reading_ease": stats["flesch_reading_ease"], "review_score": score,
                      "sources": [s["url"] for s in sources if s["cited"]]}
            actions.append(PendingAction(id="cms-1", kind="cms.publish", connector="cms", method="publish",
                                         payload={"title": title, "body": markdown_text, "slug": slug, "content_type": "article",
                                                  "fields": fields, "status": inp.get("publish_status", "draft"), "body_format": "markdown"},
                                         summary=f"Publish '{title}' to the CMS as {inp.get('publish_status', 'draft')} (editorial approval required)",
                                         requires_approval=True, mandatory=True))
        out = BlogOutput(title=title, slug=slug, meta_description=data["meta"], primary_keyword=data["primary"], secondary_keywords=data["secondary"],
                         outline=[OutlineSection.model_validate(s) for s in data["outline"]], markdown=markdown_text, word_count=stats["words"],
                         readability=Readability(**stats), sources=[SourceRef.model_validate(s) for s in sources], critique=result,
                         revisions_applied=revisions, score_history=list(data.get("scores", [])), publish_status=inp.get("publish_status", "draft"),
                         author=inp.get("author"))
        fails = sum(1 for c in checks if c.status == "fail")
        summary = (f"Blog post '{title}': {stats['words']} words, reading ease {stats['flesch_reading_ease']}, review {score}/100, "
                   f"{revisions} revision(s), {sum(1 for s in sources if s['cited'])} cited source(s)"
                   + (f", {fails} guardrail failure(s)" if fails else "") + (" - CMS publish awaiting approval" if actions else ""))
        return {"output": out.model_dump(mode="json"), "checks": checks, "artifacts": artifacts, "actions": actions, "summary": summary}

    def _guardrails(self, inp: dict[str, Any], data: dict[str, Any], ctx: RunContext) -> tuple[list[Check], int, dict[str, Any]]:
        return guardrails(title=data["title"], body=data["body"], meta_description=data["meta"], primary_keyword=data["primary"],
                          secondary_keywords=data["secondary"], target_words=int(inp["target_word_count"]),
                          min_reading_ease=float(inp["min_reading_ease"]), banned_phrases=ctx.brand.get("banned_phrases", []),
                          source_ids=[n["id"] for n in data["notes"]])


def route_after_critique(state: dict[str, Any]) -> str:
    return (state.get("data") or {}).get("next_step", "finalize")


def _host(url: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(url or "").netloc.lower().removeprefix("www.")


def _note_lines(notes: list[dict[str, Any]]) -> list[str]:
    return [f"[{n['id']}] {n['title']} ({n.get('publisher') or _host(n['url'])}{', ' + n['published'] if n.get('published') else ''}) "
            f"{n['url']} - {n.get('text', '')}" for n in notes]
