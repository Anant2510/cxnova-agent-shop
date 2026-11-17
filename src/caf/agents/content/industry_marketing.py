"""Industry Marketing - tailor horizontal content to a vertical, grounded in your own corpus.

Industry knowledge is yours, not the vendor's: versioned YAML profiles
(config/skills/industries/<id>.yaml - terminology, personas, buyer priorities, compliance must /
never rules, proof-point guidance) plus a retrieval layer over your own documents
(sample_data/industry_corpus/*.md with front-matter industry / title / doc_type / tags) indexed
into the vectorstore connector with metadata filters by industry. The model tailors the source
grounded in the retrieved passages and cites document ids; code then verifies every proof point
(cited document exists, its figures appear in that document), recomputes the sector terms used,
enforces the profile's 'never' phrases and appends missing required disclaimers.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from ...core import AgentKind, AgentSpec, BaseAgent, Category, Check, RunContext, Tier, register
from ...core.textutil import normalize_ws, truncate

COLLECTION = "industry_corpus"
SHARED_INDUSTRIES = ("cross-industry", "cross_industry", "all", "general")
_INDEXED: dict[str, str] = {}  # (provider, folder) -> corpus fingerprint, so unchanged corpora are not re-indexed
_FIGURE = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")

# ----------------------------------------------------------------------------- contract


class IndustryInput(BaseModel):
    industry: str = Field(description="Target industry: bfs, insurance, travel, healthcare, retail (aliases such as 'banking' work)")
    source_text: str | None = Field(default=None, description="The horizontal content / proposition to tailor", json_schema_extra={"ui": "textarea"})
    source: str | None = Field(default=None, description="Or a URL / file path to load the source content from")
    persona: str | None = Field(default=None, description="Persona to focus on, e.g. 'Head of Claims'")
    asset_type: str = Field(default="landing page section", description="What you are writing: landing page section, email, one-pager, social post, sales email ...")
    region: str | None = Field(default=None, description="Market / regulatory region, e.g. US, UK, EU")
    max_passages: int = Field(default=6, ge=1, le=20, description="Corpus passages retrieved for grounding")
    corpus_path: str = Field(default="sample_data/industry_corpus", description="Folder of Markdown documents (front-matter: industry, title, doc_type, tags) to index")


class DraftProofPoint(BaseModel):
    text: str = Field(description="Quantified outcome taken from a retrieved passage")
    source_doc: str = Field(description="Document id of the passage it comes from")


class TailoredDraft(BaseModel):
    tailored_headline: str
    tailored_copy: str = Field(description="Markdown copy tailored to the industry")
    key_terms_used: list[str] = Field(default_factory=list)
    personas: list[str] = Field(default_factory=list, description="Profile personas the copy speaks to")
    proof_points: list[DraftProofPoint] = Field(default_factory=list, max_length=5)
    citations: list[str] = Field(default_factory=list, description="Document ids the copy relies on")


class ProofPoint(BaseModel):
    text: str
    source_doc: str
    verified: bool = True
    basis: Literal["model", "corpus_extract"] = "model"


class Citation(BaseModel):
    doc_id: str
    title: str
    doc_type: str = ""
    industry: str = ""
    score: float = 0.0


class PersonaRef(BaseModel):
    name: str
    priorities: list[str] = Field(default_factory=list)


class Compliance(BaseModel):
    passed: bool
    never_phrases_found: list[str] = Field(default_factory=list)
    disclaimers_added: list[str] = Field(default_factory=list)
    avoid_terms_found: list[str] = Field(default_factory=list)
    must_rules: list[str] = Field(default_factory=list)
    repaired: bool = False


class IndustryOutput(BaseModel):
    industry: str
    industry_name: str
    profile_version: str
    asset_type: str
    tailored_headline: str
    tailored_copy: str
    key_terms_used: list[str] = Field(default_factory=list)
    personas: list[PersonaRef] = Field(default_factory=list)
    proof_points: list[ProofPoint] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    compliance: Compliance
    grounded: bool = Field(description="True when the copy was grounded in retrieved corpus passages")
    passages_used: int = 0


# ----------------------------------------------------------------------------- deterministic core

def parse_front_matter(text: str) -> tuple[dict[str, Any], str]:
    """Split '---' YAML front-matter from a Markdown body (comments in the YAML are fine)."""
    m = re.match(r"^\ufeff?---\s*\n(.*?)\n---\s*\n?(.*)$", text or "", flags=re.S)
    if not m:
        return {}, text or ""
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        meta = {}
    return (meta if isinstance(meta, dict) else {}), m.group(2)


def load_corpus(folder: Path) -> list[dict[str, Any]]:
    docs = []
    for p in sorted(folder.glob("*.md")):
        meta, body = parse_front_matter(p.read_text(encoding="utf-8"))
        body = re.sub(r"^\s*Illustrative sample content:[^\n]*\n", "", body).strip()
        tags = meta.get("tags") or []
        docs.append({"doc_id": str(meta.get("id") or p.stem), "industry": str(meta.get("industry") or "").strip().lower(),
                     "title": str(meta.get("title") or p.stem), "doc_type": str(meta.get("doc_type") or ""),
                     "tags": [str(t) for t in tags] if isinstance(tags, list) else [str(tags)], "text": body, "path": str(p)})
    return docs


def _blocks(body: str) -> list[tuple[str, str]]:
    """(section heading, block) pairs; a Markdown table stays one block."""
    out: list[tuple[str, str]] = []
    heading = ""
    for raw in re.split(r"\n\s*\n", body or ""):
        block = raw.strip()
        if not block:
            continue
        m = re.match(r"^(#{1,6})\s+(.+?)\s*$", block.splitlines()[0])
        if m:
            heading = m.group(2)
            rest = "\n".join(block.splitlines()[1:]).strip()
            if not rest:
                continue
            block = rest
        out.append((heading, block))
    return out


def corpus_chunks(docs: list[dict[str, Any]], max_words: int = 160) -> list[dict[str, Any]]:
    """Section-aware chunks (whole paragraphs / tables, never split mid-sentence), prefixed with the
    document title and section heading so each passage reads on its own."""
    out = []
    for d in docs:
        chunks: list[tuple[str, list[str]]] = []
        for heading, block in _blocks(d["text"]):
            n = len(block.split())
            if chunks and chunks[-1][0] == heading and sum(len(b.split()) for b in chunks[-1][1]) + n <= max_words:
                chunks[-1][1].append(block)
            else:
                chunks.append((heading, [block]))
        for i, (heading, blocks) in enumerate(chunks):
            text = f"{d['title']}" + (f" - {heading}" if heading else "") + "\n" + "\n\n".join(blocks)
            out.append({"id": f"{d['doc_id']}#{i}", "text": text,
                        "metadata": {"doc_id": d["doc_id"], "industry": d["industry"], "title": d["title"], "doc_type": d["doc_type"],
                                     "tags": d["tags"], "section": heading, "chunk": i}})
    return out


def passage_sentences(text: str) -> list[str]:
    """Sentences from a passage; list items are sentences; Before/After table rows become
    'Measure: before -> after' (figures kept verbatim)."""
    out: list[str] = []
    header: list[str] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                continue
            if not header:
                header = [c.lower() for c in cells]
                continue
            if len(cells) >= 3 and len(header) >= 3 and "before" in header[1] and "after" in header[2]:
                out.append(f"{cells[0]}: {cells[1]} -> {cells[2]}")
            else:
                out.append("; ".join(c for c in cells if c))
            continue
        header = []
        line = normalize_ws(re.sub(r"[*_#>`]", "", re.sub(r"^\s*(?:[-+]|\d+[.)])\s+", "", line)))
        if line:
            out += [x.strip() for x in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])", line) if x.strip()]
    return out


def select_passages(hits: list[dict[str, Any]], limit: int, per_doc: int = 3) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    out = []
    for h in hits:
        d = h["metadata"].get("doc_id")
        if counts.get(d, 0) >= per_doc:
            continue
        counts[d] = counts.get(d, 0) + 1
        out.append(h)
        if len(out) >= limit:
            break
    return out


def outcome_sections(chunks: list[dict[str, Any]], doc_ids: set[str], have: set[str]) -> list[dict[str, Any]]:
    """Results / outcomes / proof-point sections of documents already retrieved (deterministic augmentation)."""
    return [{**c, "score": 0.0, "metadata": {**c["metadata"], "augmented": True}} for c in chunks
            if c["metadata"]["doc_id"] in doc_ids and c["id"] not in have
            and re.search(r"result|outcome|proof point|impact", c["metadata"].get("section") or "", re.I)]


def resolve_industry(value: str, profiles: dict[str, dict[str, Any]]) -> str | None:
    """Profile id for an industry name, id or alias ('banking' -> 'bfs'); whole-word containment as a fallback."""
    v = normalize_ws(value).lower()
    for pid, prof in profiles.items():
        names = {pid, str(prof.get("name", "")).lower(), *[str(a).lower() for a in prof.get("aliases") or []]}
        if v in names:
            return pid
    for pid, prof in profiles.items():
        for a in [pid, *[str(x).lower() for x in prof.get("aliases") or []]]:
            if re.search(r"(?<![a-z0-9])" + re.escape(a) + r"(?![a-z0-9])", v):
                return pid
    return None


def find_phrases(text: str, phrases: list[str]) -> list[str]:
    low = (text or "").lower()
    return [p for p in phrases if p and re.search(r"(?<![a-z0-9])" + re.escape(p.lower()) + r"(?![a-z0-9])", low)]


def figures(text: str) -> set[str]:
    return {f.replace(",", "") for f in _FIGURE.findall(text or "")}


def verify_proof_point(text: str, doc_text: str) -> bool:
    """Every figure in the proof point must appear in the cited document (numeric grounding)."""
    need = figures(text)
    return bool(need) and need <= figures(doc_text)


_QUANT = re.compile(r"%|->|\bhours?\b|\bdays?\b|\bpoints?\b|\btimes\b|\bmillion\b|\bfrom\b.+\bto\b", re.I)


_OUTCOME = re.compile(r"->|\b(?:cut|cuts|reduced|reduces|raised|raises|increased|improved|fell|rose|lifted|lowered|grew|saved|"
                      r"halved|doubled|tripled|dropped|boosted|shortened)\b", re.I)


def outcome_score(sentence: str, section: str) -> int:
    """How much a sentence reads like a result: results sections and before -> after statements win,
    challenge / context sections lose."""
    score = 0
    if re.search(r"result|outcome|impact|proof|benefit", section or "", re.I):
        score += 3
    if re.search(r"challenge|problem|context|background", section or "", re.I):
        score -= 3
    if _OUTCOME.search(sentence):
        score += 2
    return score


def extract_proof_points(passages: list[dict[str, Any]], docs: dict[str, dict[str, Any]], limit: int = 3,
                         per_doc: int = 2) -> list[dict[str, Any]]:
    """Deterministic fallback: verbatim quantified outcome statements from retrieved passages, ranked
    by outcome evidence then document type (case study > analyst note > profile), each verified
    against its document's figures."""
    order = {"case_study": 0, "analyst_note": 1, "profile": 2}
    cands = []
    for rank, p in enumerate(passages):
        meta = p["metadata"]
        doc_text = docs.get(meta["doc_id"], {}).get("text", p["text"])
        for pos, sent in enumerate(passage_sentences(p["text"])):
            if 25 <= len(sent) <= 260 and re.search(r"\d", sent) and _QUANT.search(sent) and verify_proof_point(sent, doc_text):
                cands.append((-outcome_score(sent, meta.get("section", "")), order.get(meta.get("doc_type", ""), 3), rank, pos,
                              sent, meta["doc_id"]))
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    counts: dict[str, int] = {}
    for neg, _o, _r, _p, sent, doc_id in sorted(cands):
        if -neg < 2 or sent.lower() in seen or counts.get(doc_id, 0) >= per_doc:
            continue
        seen.add(sent.lower())
        counts[doc_id] = counts.get(doc_id, 0) + 1
        out.append({"text": sent, "source_doc": doc_id, "verified": True, "basis": "corpus_extract"})
        if len(out) >= limit:
            break
    return out


def normalize_doc_id(value: str) -> str:
    return re.sub(r"#\d+$", "", str(value or "").strip().strip("[]()").strip())


def match_personas(names: list[str], profile_personas: list[dict[str, Any]], focus: str | None) -> list[dict[str, Any]]:
    """Profile personas the copy speaks to: model-named ones that exist, else the focus persona, else the first two."""
    by_name = {p["name"].lower(): p for p in profile_personas}
    picked = []
    for n in names:
        n_low = n.lower()
        hit = by_name.get(n_low) or next((p for k, p in by_name.items() if k in n_low or n_low in k), None)
        if hit and hit not in picked:
            picked.append(hit)
    if not picked and focus:
        hit = next((p for k, p in by_name.items() if k in focus.lower() or focus.lower() in k), None)
        if hit:
            picked.append(hit)
    return picked or profile_personas[:2]


def corpus_fingerprint(folder: Path) -> str:
    h = hashlib.sha1()
    for p in sorted(folder.glob("*.md")):
        st = p.stat()
        h.update(f"{p.name}:{st.st_size}:{int(st.st_mtime)}".encode())
    return h.hexdigest()[:16]


# ----------------------------------------------------------------------------- agent

@register
class IndustryMarketingAgent(BaseAgent):
    spec = AgentSpec(
        id="industry_marketing",
        name="Industry Marketing",
        category=Category.CONTENT,
        summary="Tailors horizontal content to an industry (BFS, insurance, travel, healthcare, retail) using versioned industry profiles and retrieval over your own corpus - cited proof points, sector terminology and deterministic compliance checks.",
        kind=AgentKind.HYBRID,
        tier=Tier.BALANCED,
        connectors=["vectorstore", "ingestion"],
        skills=["industries/bfs", "industries/insurance", "industries/travel", "industries/healthcare", "industries/retail", "brand_voice"],
        keywords=["industry", "vertical", "sector", "industry marketing", "bfs", "banking", "insurance", "healthcare", "travel", "retail"],
        produces=["industry_copy.md", "industry_copy.json"],
        feeds=["content_adaptation", "email_creation", "linkedin_inmail_copy", "campaign_brief_generation"],
    )
    Input = IndustryInput
    Output = IndustryOutput
    schemas = {"tailor": TailoredDraft, "repair": TailoredDraft}

    def selftest_input(self) -> dict[str, Any]:
        return {"industry": "insurance", "persona": "Head of Claims", "asset_type": "landing page section", "region": "US",
                "source_text": "Our intelligent automation service helps operations teams digitise intake, triage work automatically "
                               "and keep customers informed at every step, so cases are resolved faster, with fewer errors and lower "
                               "cost to serve. Teams start with simple, high-volume work and keep people in charge of every exception."}

    def stages(self):
        return [("profile", self.profile), ("retrieve", self.retrieve), ("tailor", self.tailor), ("comply", self.comply), ("export", self.export)]

    # -- profile + source ---------------------------------------------------------------------------
    def profile(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp = state["inputs"]
        ids = [n.split("/", 1)[1] for n in ctx.skills.list() if n.startswith("industries/")]
        profiles = {i: ctx.skill(f"industries/{i}") for i in ids}
        pid = resolve_industry(inp["industry"], profiles)
        if not pid:
            raise ValueError(f"Unknown industry '{inp['industry']}'. Profiles available: {sorted(profiles)} (config/skills/industries/)")
        warnings: list[str] = []
        text = (inp.get("source_text") or "").strip()
        origin = "source_text"
        if not text and inp.get("source"):
            try:
                text, origin = ctx.connector("ingestion").load(inp["source"]).text.strip(), inp["source"]
            except Exception as exc:
                warnings.append(f"Could not load {inp['source']}: {type(exc).__name__}: {exc}")
        if not text:
            text, origin = ctx.upstream_text(6000).strip(), "upstream"
        if not text:
            raise ValueError("Provide source_text or source (URL / path) - or run after a content agent in an orchestrated plan.")
        return {"data": {"industry": pid, "profile": profiles[pid], "source_text": text, "origin": origin}, "warnings": warnings}

    # -- retrieval over the corpus (vectorstore, filtered by industry) ------------------------------
    def retrieve(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        vs = ctx.connector("vectorstore")
        try:  # security policy: service callers read allowed folders only
            folder = ctx.local_path(inp.get("corpus_path") or "sample_data/industry_corpus")
        except PermissionError as exc:
            raise ValueError(f"corpus_path is not readable here: {exc}") from exc
        checks: list[Check] = []
        warnings: list[str] = []
        reasons: list[str] = []
        docs: dict[str, dict[str, Any]] = {}
        chunks: list[dict[str, Any]] = []
        if folder.is_dir() and any(folder.glob("*.md")):
            loaded = load_corpus(folder)
            docs = {d["doc_id"]: d for d in loaded}
            chunks = corpus_chunks(loaded)
            key = f"{vs.provider}:{folder}"
            fp = corpus_fingerprint(folder)
            if _INDEXED.get(key) != fp or not vs.count(COLLECTION):
                vs.upsert(COLLECTION, chunks)
                _INDEXED[key] = fp
                checks.append(Check(rule="corpus.indexed", status="info", severity="low",
                                    message=f"Indexed {len(loaded)} documents into '{COLLECTION}' ({vs.count(COLLECTION)} chunks)."))
        elif not vs.count(COLLECTION):
            reasons.append(f"industry corpus not found at {folder} and the vector store is empty - copy is not grounded in your documents")
        industry = data["industry"]
        query = " ".join(x for x in [truncate(data["source_text"], 1500, ""), inp.get("persona") or "", inp.get("asset_type") or "",
                                     " ".join((data["profile"].get("buyer_priorities") or [])[:3])] if x)
        hits = vs.search(COLLECTION, query, k=int(inp["max_passages"]) * 3, filters={"industry": [industry, *SHARED_INDUSTRIES]}) \
            if vs.count(COLLECTION) else []
        passages = select_passages(hits, int(inp["max_passages"]))
        outcome = outcome_sections(chunks, {p["metadata"]["doc_id"] for p in passages}, {p["id"] for p in passages})
        if outcome:
            passages += outcome
            checks.append(Check(rule="retrieval.outcome_sections", status="info", severity="low",
                                message=f"Added {len(outcome)} results / proof-point section(s) of the retrieved documents."))
        per_doc = {p["metadata"]["doc_id"] for p in passages}
        wrong = [p["id"] for p in passages if p["metadata"].get("industry") not in (industry, *SHARED_INDUSTRIES)]
        checks.append(Check(rule="retrieval.filter", status="pass" if not wrong else "fail", severity="high",
                            message=f"{len(passages)} passages from {len(per_doc)} {industry} document(s)" + (f"; off-industry: {wrong}" if wrong else ".")))
        if not passages and not reasons:
            reasons.append(f"no corpus passages for industry '{industry}' - copy relies on the profile only")
        if not passages:
            warnings.append("No grounding passages were retrieved; proof points cannot be cited.")
        prev = list((state.get("provenance") or {}).get("degraded_reasons", []))
        return {"data": {"passages": passages, "docs": docs}, "checks": checks, "warnings": warnings,
                "provenance": {"degraded_reasons": prev + reasons, "passages": len(passages)}}

    # -- tailor (model, grounded) ------------------------------------------------------------------
    def tailor(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        draft = ctx.generate("tailor", TailoredDraft, **self._prompt_vars(inp, data))
        return {"data": {"draft": draft.model_dump()}}

    def _prompt_vars(self, inp: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
        prof = data["profile"]
        comp = prof.get("compliance") or {}
        return dict(industry_name=prof.get("name", data["industry"]), persona=inp.get("persona"), asset_type=inp.get("asset_type"),
                    region=inp.get("region"), source_text=truncate(data["source_text"], 6000),
                    terminology=list((prof.get("terminology") or {}).get("preferred") or []),
                    avoid=[f"say '{a['use']}' instead of '{a['term']}'" for a in (prof.get("terminology") or {}).get("avoid") or [] if isinstance(a, dict)],
                    personas=[f"{p['name']} - priorities {', '.join(p.get('priorities') or [])}" for p in prof.get("personas") or []],
                    buyer_priorities=prof.get("buyer_priorities") or [], must=comp.get("must") or [], never=comp.get("never") or [],
                    disclaimers=comp.get("required_disclaimers") or [], regulators=comp.get("regulators") or [],
                    proof_guidance=(prof.get("proof_points") or {}).get("guidance", ""), tone=prof.get("tone", ""),
                    passages=[f"[{p['metadata']['doc_id']}] ({p['metadata'].get('doc_type', '')}: {p['metadata'].get('title', '')}) "
                              f"{truncate(normalize_ws(p['text']), 900)}" for p in data["passages"]])

    # -- deterministic compliance, grounding and reconciliation -----------------------------------------
    def comply(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        prof = data["profile"]
        comp = prof.get("compliance") or {}
        never = list(comp.get("never") or []) + list(ctx.brand.get("banned_phrases") or [])
        draft = TailoredDraft.model_validate(data["draft"])
        checks: list[Check] = []
        found = find_phrases(f"{draft.tailored_headline}\n{draft.tailored_copy}", never)
        repaired = False
        if found and not ctx.llm.offline:  # one model repair pass, then report what is left
            draft = ctx.generate("repair", TailoredDraft, draft=draft.model_dump(), violations=found, **self._prompt_vars(inp, data))
            repaired = True
            found = find_phrases(f"{draft.tailored_headline}\n{draft.tailored_copy}", never)
        checks.append(Check(rule="compliance.never_phrases", status="fail" if found else "pass", severity="critical" if found else "low",
                            ref=f"industries/{data['industry']} v{prof.get('version', '')}",
                            message=f"Prohibited phrases for {prof.get('name')}: {found}" if found else "No prohibited phrases."))
        docs, passages = data["docs"], data["passages"]
        retrieved = {p["metadata"]["doc_id"]: p for p in passages}
        copy = draft.tailored_copy.strip()
        refs = set(re.findall(r"\[([A-Za-z0-9_\-]+)(?:#\d+)?\]", copy))
        unknown_refs = sorted(r for r in refs if r not in retrieved and (r in docs or "_" in r))
        if unknown_refs:
            copy = re.sub(r"\s?\[(?:" + "|".join(re.escape(u) for u in set(unknown_refs)) + r")(?:#\d+)?\]", "", copy)
            checks.append(Check(rule="grounding.citations", status="warn", severity="medium",
                                message=f"Removed citations to documents that were not retrieved: {sorted(set(unknown_refs))}"))
        proofs, dropped = [], []
        for pp in draft.proof_points:
            doc_id = normalize_doc_id(pp.source_doc)
            text = normalize_ws(pp.text)
            if not text:
                continue
            if doc_id not in retrieved:
                dropped.append(f"'{truncate(text, 60)}' (unknown source '{pp.source_doc}')")
            elif not verify_proof_point(text, docs.get(doc_id, {}).get("text", retrieved[doc_id]["text"])):
                dropped.append(f"'{truncate(text, 60)}' (figures not found in {doc_id})")
            else:
                proofs.append({"text": text, "source_doc": doc_id, "verified": True, "basis": "model"})
        if dropped:
            checks.append(Check(rule="grounding.proof_points", status="warn", severity="high",
                                message=f"Dropped {len(dropped)} unverifiable proof point(s): " + "; ".join(dropped[:4])))
        if not proofs and passages:
            proofs = extract_proof_points(passages, docs)
            if proofs:
                checks.append(Check(rule="grounding.proof_points", status="info", severity="low",
                                    message=f"{len(proofs)} proof point(s) extracted verbatim from retrieved case material."))
        checks.append(Check(rule="grounding.proof_points_verified", status="pass" if proofs else "warn", severity="medium",
                            message=f"{len(proofs)} proof point(s), each traced to a retrieved document with matching figures."
                            if proofs else "No verifiable proof points."))
        added = []
        for disc in comp.get("required_disclaimers") or []:
            if disc.lower().rstrip(".") not in copy.lower():
                copy = f"{copy}\n\n_{disc}_"
                added.append(disc)
        if added:
            checks.append(Check(rule="compliance.disclaimers", status="info", severity="low", message=f"Appended required disclaimer(s): {added}"))
        else:
            checks.append(Check(rule="compliance.disclaimers", status="pass", severity="low", message="Required disclaimers present."))
        avoid_found = [f"{a['term']} -> {a['use']}" for a in (prof.get("terminology") or {}).get("avoid") or []
                       if isinstance(a, dict) and find_phrases(copy, [a["term"]])]
        if avoid_found:
            checks.append(Check(rule="terminology.avoid", status="warn", severity="low", message=f"Generic terms with a sector equivalent: {avoid_found}"))
        terms = list(dict.fromkeys(find_phrases(f"{draft.tailored_headline}\n{copy}", list((prof.get("terminology") or {}).get("preferred") or []))))
        checks.append(Check(rule="terminology.sector_terms", status="pass" if len(terms) >= 2 else "warn", severity="low",
                            message=f"{len(terms)} sector term(s) used: {terms[:8]}"))
        personas = match_personas(draft.personas, prof.get("personas") or [], inp.get("persona"))
        cited = list(dict.fromkeys([p["source_doc"] for p in proofs] + [normalize_doc_id(c) for c in draft.citations if normalize_doc_id(c) in retrieved]))
        cited += [d for d in retrieved if d not in cited and re.search(r"\[" + re.escape(d) + r"(?:#\d+)?\]", copy)]
        citations = [{"doc_id": d, "title": retrieved[d]["metadata"].get("title", d), "doc_type": retrieved[d]["metadata"].get("doc_type", ""),
                      "industry": retrieved[d]["metadata"].get("industry", ""), "score": retrieved[d].get("score", 0.0)} for d in cited]
        compliance = Compliance(passed=not found, never_phrases_found=found, disclaimers_added=added, avoid_terms_found=avoid_found,
                                must_rules=list(comp.get("must") or []), repaired=repaired)
        return {"data": {"final": {"headline": normalize_ws(draft.tailored_headline), "copy": copy, "terms": terms, "proofs": proofs,
                                   "citations": citations, "personas": personas, "compliance": compliance.model_dump()}}, "checks": checks}

    # -- export ------------------------------------------------------------------------------------
    def export(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        f, prof = data["final"], data["profile"]
        out = IndustryOutput(industry=data["industry"], industry_name=prof.get("name", data["industry"]), profile_version=str(prof.get("version", "")),
                             asset_type=inp.get("asset_type") or "", tailored_headline=f["headline"], tailored_copy=f["copy"], key_terms_used=f["terms"],
                             personas=[PersonaRef(name=p["name"], priorities=list(p.get("priorities") or [])) for p in f["personas"]],
                             proof_points=[ProofPoint.model_validate(p) for p in f["proofs"]],
                             citations=[Citation.model_validate(c) for c in f["citations"]], compliance=Compliance.model_validate(f["compliance"]),
                             grounded=bool(data["passages"]), passages_used=len(data["passages"]))
        md = [f"# {out.tailored_headline}", "", out.tailored_copy, "", "## Proof points"]
        md += [f"- {p.text} [{p.source_doc}]" for p in out.proof_points] or ["_None verified._"]
        md += ["", "## Sources"] + ([f"- `{c.doc_id}` - {c.title} ({c.doc_type})" for c in out.citations] or ["_No corpus sources._"])
        md += ["", f"_Profile: industries/{out.industry} v{out.profile_version} · compliance {'passed' if out.compliance.passed else 'FAILED'}_"]
        dumped = out.model_dump(mode="json")
        artifacts = [ctx.artifacts.write_text("industry_copy.md", "\n".join(md) + "\n", "text/markdown", f"{out.industry_name} copy"),
                     ctx.artifacts.write_json("industry_copy.json", dumped, "Tailored copy with proof points and citations")]
        summary = (f"{out.industry_name} {out.asset_type}: '{truncate(out.tailored_headline, 70)}' - {len(out.key_terms_used)} sector terms, "
                   f"{len(out.proof_points)} verified proof point(s), {len(out.citations)} cited doc(s), compliance "
                   + ("passed" if out.compliance.passed else "FAILED") + ("" if out.grounded else " - not grounded"))
        return {"output": dumped, "artifacts": artifacts, "summary": summary}
