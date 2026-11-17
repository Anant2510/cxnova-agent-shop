"""Keyword Research - first-party query data + SERP context + model expansion, clustered and
scored in-process, so no vendor ranking service is needed.

Deterministic core (small pure functions, portable to the browser console):
  * aggregate Search-Console rows (query x page) per keyword and flag cannibalisation
  * relevance gate - idf-weighted share of the topic's / a seed's words found in the keyword
  * intent rules from config/skills/keyword_intent.yaml (+ brand terms from brand_voice)
  * opportunity score from impressions, CTR and position via the expected-CTR curve in the skill
  * TF-IDF (word + char n-gram) clustering: HDBSCAN, AgglomerativeClustering for small sets
The model only proposes extra candidates (always flagged is_estimate), names the clusters and
suggests pillar + cluster content. With keyword_data 'none' the result is a flagged estimate.
"""

from __future__ import annotations

import csv
import io
import math
import re
import unicodedata
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from ...core import AgentKind, AgentSpec, BaseAgent, Category, Check, RunContext, Tier, register
from ...core.handoff import Finding
from ...core.textutil import STOPWORDS, normalize_quotes

Intent = Literal["informational", "commercial", "transactional", "navigational"]
Source = Literal["first_party", "seed", "model", "modifier"]
INTENTS = ("navigational", "transactional", "commercial", "informational")


# ----------------------------------------------------------------------------- contract

class KeywordResearchInput(BaseModel):
    topic: str = Field(min_length=2, description="Topic, product or theme to research")
    page_url: str | None = Field(default=None, description="Optional page to research - its first-party queries are always kept")
    seed_keywords: list[str] = Field(default_factory=list, description="Keywords you already have in mind")
    market: str | None = Field(default=None, description="Market / country / language, e.g. 'US' or 'en-GB'")
    max_keywords: int = Field(default=60, ge=5, le=500, description="Maximum keywords kept after scoring")


class CandidateKeyword(BaseModel):
    keyword: str = Field(description="Lowercase search query, 2-8 words")
    est_monthly_searches: int | None = Field(default=None, ge=0, description="Rough monthly searches in the market - an estimate")
    rationale: str = Field(default="", description="Gap or intent it covers, one short phrase")


class KeywordExpansion(BaseModel):
    keywords: list[CandidateKeyword] = Field(default_factory=list, max_length=60)


class ClusterSuggestion(BaseModel):
    cluster_id: str = Field(description="Cluster id exactly as given, e.g. c1")
    name: str = Field(description="2-5 word human label for the cluster")
    pillar_title: str = Field(description="Working title of the pillar page for the cluster")
    supporting_content: list[str] = Field(default_factory=list, max_length=6, description="Cluster article titles that link to the pillar")
    rationale: str = ""


class ClusterNaming(BaseModel):
    clusters: list[ClusterSuggestion] = Field(default_factory=list)


class KeywordRow(BaseModel):
    keyword: str
    cluster_id: str = ""
    intent: Intent
    intent_rule: str = ""
    source: Source
    is_estimate: bool = Field(description="True when metrics are model / default estimates, not first-party data")
    relevance: float = Field(ge=0, le=1)
    impressions: int | None = None
    clicks: int | None = None
    ctr: float | None = None
    position: float | None = None
    est_monthly_searches: int | None = None
    expected_ctr: float = 0.0
    potential_clicks: float = Field(default=0.0, description="Extra clicks available at the target position (data period)")
    opportunity_score: float = Field(ge=0, le=100)
    opportunity_type: str = ""
    position_band: str = ""
    page: str | None = None
    cannibalised: bool = False


class KeywordCluster(BaseModel):
    cluster_id: str
    name: str
    pillar_keyword: str
    intent: Intent
    keyword_count: int
    total_impressions: int
    potential_clicks: float
    opportunity_score: float = Field(ge=0, le=100)
    existing_page: str | None = None
    is_estimate: bool
    keywords: list[KeywordRow]


class ContentRecommendation(BaseModel):
    cluster_id: str
    cluster_name: str
    action: Literal["create", "optimise"]
    content_type: str
    pillar_title: str
    target_page: str | None = None
    target_keywords: list[str]
    supporting_content: list[str] = Field(default_factory=list)
    rationale: str = ""
    is_estimate: bool = False


class SerpItem(BaseModel):
    rank: int
    title: str
    url: str
    source: str | None = None
    own_domain: bool = False


class KeywordResearchOutput(BaseModel):
    topic: str
    market: str | None = None
    summary: str
    data_source: str
    is_estimate: bool = Field(description="True when no first-party keyword data backs the result")
    clustering_method: str
    keyword_count: int
    clusters: list[KeywordCluster]
    top_opportunities: list[KeywordRow]
    content_recommendations: list[ContentRecommendation]
    serp_context: list[SerpItem] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    skill_version: str = ""


# ----------------------------------------------------------------------------- text helpers

_KW_STRIP = re.compile(r"[^a-z0-9&+' -]+")


def normalize_keyword(text: Any) -> str:
    """Lowercase, quote/dash-normalised, punctuation-free search query (max 80 chars)."""
    t = unicodedata.normalize("NFKC", normalize_quotes(str(text or ""))).lower()
    t = re.sub(r"\s+", " ", _KW_STRIP.sub(" ", t)).strip(" -'")
    return t[:80].strip()


def stem(token: str) -> str:
    """Very light English singulariser - enough to merge 'quotes'/'quote', 'policies'/'policy'."""
    t = token.lower().strip("'")
    if t.endswith("'s"):
        t = t[:-2]
    if len(t) > 4 and t.endswith("ies"):
        return t[:-3] + "y"
    if len(t) > 4 and t.endswith(("ches", "shes", "sses", "xes", "zes")):
        return t[:-2]
    if len(t) > 3 and t.endswith("s") and not t.endswith(("ss", "us", "is")):
        return t[:-1]
    return t


def keyword_tokens(keyword: str) -> list[str]:
    return [stem(w) for w in normalize_keyword(keyword).split() if w]


def keyword_key(keyword: str) -> str:
    """Dedupe key: normalised, singularised tokens."""
    return " ".join(keyword_tokens(keyword))


def tokens_match(a: str, b: str) -> bool:
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    if len(short) >= 4 and long_.startswith(short):
        return True
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n >= 5


def has_phrase(keyword: str, phrase: str) -> bool:
    """Whole-phrase match on stemmed tokens ('quotes' matches 'quote')."""
    kw, ph = keyword_tokens(keyword), keyword_tokens(phrase)
    if not ph or len(ph) > len(kw):
        return False
    return any(kw[i:i + len(ph)] == ph for i in range(len(kw) - len(ph) + 1))


def same_page(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    pa, pb = urlsplit(a.strip()), urlsplit(b.strip())
    ha, hb = pa.netloc.lower().removeprefix("www."), pb.netloc.lower().removeprefix("www.")
    if ha and hb and ha != hb:
        return False
    return ((pa.path or "/").rstrip("/") or "/") == ((pb.path or "/").rstrip("/") or "/")


def _num(value: Any, default: float | None = 0.0) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(f) else f


# ----------------------------------------------------------------------------- deterministic core

def aggregate_queries(rows: list[dict[str, Any]], page_url: str | None = None, min_impressions: float = 1) -> list[dict[str, Any]]:
    """Collapse Search-Console rows (one per query x page) into one record per keyword.

    Impression-weighted position, CTR recomputed from clicks/impressions, pages ranked by clicks,
    `cannibalised` when two or more pages each take >= 20% of the keyword's impressions.
    """
    groups: dict[str, dict[str, Any]] = {}
    for r in rows:
        kw = normalize_keyword(r.get("query"))
        if not kw:
            continue
        imp, clicks, pos = _num(r.get("impressions")) or 0.0, _num(r.get("clicks")) or 0.0, _num(r.get("position"), None)
        g = groups.setdefault(keyword_key(kw), {"variants": {}, "impressions": 0.0, "clicks": 0.0, "pos_w": 0.0, "pos_imp": 0.0, "pages": {}})
        g["variants"][kw] = g["variants"].get(kw, 0.0) + imp
        g["impressions"] += imp
        g["clicks"] += clicks
        if pos is not None and imp > 0:
            g["pos_w"] += pos * imp
            g["pos_imp"] += imp
        page = str(r.get("page") or "").strip()
        if page and page.lower() != "nan":
            p = g["pages"].setdefault(page, {"page": page, "impressions": 0.0, "clicks": 0.0})
            p["impressions"] += imp
            p["clicks"] += clicks
    out = []
    for g in groups.values():
        imp = g["impressions"]
        if imp < min_impressions:
            continue
        pages = sorted(g["pages"].values(), key=lambda p: (-p["clicks"], -p["impressions"], p["page"]))
        major = [p for p in pages if imp and p["impressions"] / imp >= 0.2]
        keyword = sorted(g["variants"].items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        out.append({
            "keyword": keyword,
            "impressions": int(round(imp)),
            "clicks": int(round(g["clicks"])),
            "ctr": round(g["clicks"] / imp, 4) if imp else 0.0,
            "position": round(g["pos_w"] / g["pos_imp"], 2) if g["pos_imp"] else None,
            "page": pages[0]["page"] if pages else None,
            "pages": [p["page"] for p in pages],
            "cannibalised": len(major) >= 2,
            "on_target_page": any(same_page(p["page"], page_url) for p in pages),
        })
    return sorted(out, key=lambda x: (-x["impressions"], x["keyword"]))


def modifier_tokens(rules: dict[str, Any]) -> set[str]:
    """Stemmed intent-modifier words from the skill (quote, best, cost, how ...) - not subject words."""
    out: set[str] = set()
    for spec in (rules.get("intents") or {}).values():
        for phrase in list(spec.get("phrases", [])) + list(spec.get("starts_with", [])):
            out.update(keyword_tokens(phrase))
    return out


def relevance_scores(keywords: list[str], references: list[str], *, ignore: set[str] | None = None,
                     synonyms: dict[str, list[str]] | None = None) -> list[float]:
    """idf-weighted share of each reference's (topic / seed) subject words present in the keyword;
    max over references. `ignore` drops intent modifiers from the references, `synonyms` maps
    equivalent subject words (house -> home)."""
    canon = {stem(s): stem(c) for c, lst in (synonyms or {}).items() for s in [c, *lst]}

    def c(t: str) -> str:
        return canon.get(t, t)

    token_sets = [{c(t) for t in keyword_tokens(k)} for k in keywords]
    skip = set(STOPWORDS) | set(ignore or ())
    refs = [[c(t) for t in dict.fromkeys(keyword_tokens(r)) if t not in skip] for r in references if r]
    refs = [r for r in refs if r]
    if not refs:
        return [1.0] * len(keywords)
    pool = token_sets + [set(r) for r in refs]
    df: dict[str, int] = {}
    for s in pool:
        for t in s:
            df[t] = df.get(t, 0) + 1
    n = len(pool)

    def idf(t: str) -> float:
        return math.log((1 + n) / (1 + df.get(t, 0))) + 1.0

    out = []
    for toks in token_sets:
        best = 0.0
        for ref in refs:
            total = sum(idf(t) for t in ref)
            hit = sum(idf(t) for t in ref if any(tokens_match(t, k) for k in toks))
            best = max(best, hit / total if total else 0.0)
        out.append(round(min(best, 1.0), 3))
    return out


def classify_intent(keyword: str, rules: dict[str, Any], brand_terms: list[str] | None = None,
                    competitor_terms: list[str] | None = None) -> tuple[str, str]:
    """(intent, rule) - brand terms first, then the skill's rules in precedence order."""
    kw = normalize_keyword(keyword)
    for term in brand_terms or []:
        if term and has_phrase(kw, term):
            return "navigational", f"brand:{term}"
    for term in competitor_terms or []:
        if term and has_phrase(kw, term):
            return "commercial", f"competitor:{term}"
    first = keyword_tokens(kw)
    for intent in rules.get("precedence", list(INTENTS)):
        spec = (rules.get("intents") or {}).get(intent) or {}
        for sw in spec.get("starts_with", []):
            sw_t = keyword_tokens(sw)
            if sw_t and first[: len(sw_t)] == sw_t:
                return intent, f"starts_with:{sw}"
        for ph in spec.get("phrases", []):
            if has_phrase(kw, ph):
                return intent, f"phrase:{ph}"
    return rules.get("default_intent", "informational"), "default"


def brand_terms_from(brand: dict[str, Any], extra: list[str] | None = None) -> list[str]:
    terms = [normalize_keyword(brand.get("name", ""))]
    host = urlsplit(str(brand.get("website") or "")).netloc.lower().removeprefix("www.").split(".")[0]
    if host:
        terms += [normalize_keyword(host.replace("-", " ")), host.replace("-", "")]
    terms += [normalize_keyword(t) for t in extra or []]
    return [t for t in dict.fromkeys(terms) if t]


def expected_ctr(position: float | None, curve: list[list[float]]) -> float:
    """Linear interpolation on the skill's position -> CTR curve."""
    if position is None or not curve:
        return 0.0
    pts = sorted((float(p), float(c)) for p, c in curve)
    if position <= pts[0][0]:
        return pts[0][1]
    for (p0, c0), (p1, c1) in zip(pts, pts[1:]):
        if position <= p1:
            return c0 + (c1 - c0) * (position - p0) / (p1 - p0)
    return pts[-1][1]


def position_band(position: float | None, bands: list[dict[str, Any]]) -> dict[str, Any]:
    if position is not None:
        for b in bands:
            if position <= float(b["max_position"]):
                return b
    return bands[-1] if bands else {"label": "unknown", "ease": 0.5}


def score_keyword(*, impressions: float | None, ctr: float | None, position: float | None, intent: str,
                  is_estimate: bool, model: dict[str, Any]) -> dict[str, Any]:
    """Opportunity = extra clicks at the target position x ease(band) x intent weight [x estimate discount]."""
    curve = model["expected_ctr_curve"]
    target = float(model.get("target_position", 3))
    pos = position if position is not None else float((model.get("estimate_defaults") or {}).get("position", 100))
    band = position_band(pos, model.get("position_bands", []))
    exp_now = expected_ctr(pos, curve)
    exp_target = expected_ctr(1.0 if pos <= target else target, curve)
    actual = float(ctr or 0.0)
    gain = max(0.0, exp_target - actual) * float(impressions or 0)
    value = gain * float(band.get("ease", 0.5)) * float((model.get("intent_weights") or {}).get(intent, 0.7))
    if is_estimate:
        value *= float(model.get("estimate_discount", 0.5))
    if intent == "navigational":
        kind = "brand"
    elif gain <= 0:
        kind = "hold"
    elif is_estimate or band.get("label") == "not_ranking":
        kind = "new_content"
    elif pos <= target:
        kind = "ctr_gap" if actual < exp_now * float(model.get("ctr_gap_ratio", 0.7)) else "push_to_first"
    elif pos <= 10:
        kind = "page_one_push"
    elif pos <= 20:
        kind = "striking_distance"
    else:
        kind = "long_shot"
    return {"expected_ctr": round(exp_now, 4), "potential_clicks": round(gain, 1), "value": value,
            "position_band": band.get("label", ""), "opportunity_type": kind}


def normalise_scores(values: list[float]) -> list[float]:
    top = max(values, default=0.0)
    return [round(100.0 * v / top, 1) if top > 0 else 0.0 for v in values]


def concept_tags(keyword: str, concepts: dict[str, list[str]]) -> list[str]:
    return [name for name, terms in (concepts or {}).items() if any(has_phrase(keyword, t) for t in terms)]


TIE_QUANTUM_BITS = 30  # distances are snapped to a 2^-30 grid (well above float noise, well below real gaps)
TIE_STEP = 2.0 ** -50  # exact pair-rank offsets below the grid: no two distances can tie


def tie_break_distances(dist: Any, keywords: list[str]) -> Any:
    """Runtime-independent distance matrix for clustering.

    HDBSCAN sorts its minimum-spanning-tree edges with numpy's unstable argsort, so exactly tied
    distances (common when keywords only share a concept tag) made the clusters depend on the sort
    implementation - and differ from the browser twin. Snap every distance to a 2^-30 grid (removes
    last-bit float noise between runtimes), then add an exact offset of (pair rank) x 2^-50, the pair
    rank coming from the alphabetical order of the two keywords. All values stay exact binary
    fractions, every off-diagonal distance is unique, and the same matrix is produced in Python and
    in console/src/js/agents/keyword_research.js, so every sort / argmin has a single answer.
    """
    import numpy as np

    n = len(keywords)
    rank = np.empty(n, dtype=np.int64)
    rank[sorted(range(n), key=lambda i: (keywords[i], i))] = np.arange(n)
    lo, hi = np.minimum.outer(rank, rank), np.maximum.outer(rank, rank)
    scale = float(2 ** TIE_QUANTUM_BITS)
    out = np.floor(np.asarray(dist, dtype=np.float64) * scale + 0.5) / scale + (lo * n + hi) * TIE_STEP
    np.fill_diagonal(out, 0.0)
    return out


def cluster_keywords(keywords: list[str], params: dict[str, Any]) -> tuple[list[int], str]:
    """Cluster keywords on TF-IDF word (1-2) + char_wb (3-5) n-grams + concept tags, cosine distance.

    Words present in more than `ubiquitous_df` of the keywords are dropped (no grouping signal).
    HDBSCAN when there are enough keywords, AgglomerativeClustering for small sets or a degenerate
    HDBSCAN result; leftovers (noise / singletons) join their nearest neighbour's cluster above
    `attach_min_similarity`. Distances get a deterministic tie-break (tie_break_distances), so the
    result never depends on sort stability. Returns (labels 0..k-1 in order of first appearance, method).
    """
    n = len(keywords)
    if n == 0:
        return [], "none"
    if n == 1:
        return [0], "single"
    import numpy as np
    from scipy.sparse import hstack
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.preprocessing import normalize

    toks = [keyword_tokens(k) for k in keywords]
    df: dict[str, int] = {}
    for t in toks:
        for x in set(t):
            df[x] = df.get(x, 0) + 1
    ubiq = {x for x, c in df.items() if n >= 8 and c / n >= float(params.get("ubiquitous_df", 0.6))}
    docs = [" ".join(x for x in t if x not in ubiq) or " ".join(t) or k for t, k in zip(toks, keywords)]
    blocks = [float(params.get("word_weight", 0.6)) * normalize(
                  TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True, token_pattern=r"(?u)\b\w+\b").fit_transform(docs)),
              float(params.get("char_weight", 0.25)) * normalize(
                  TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), sublinear_tf=True).fit_transform(docs))]
    tag_docs = [" ".join(f"zz{c}" for c in concept_tags(k, params.get("concepts") or {})) for k in keywords]
    if any(tag_docs):
        blocks.append(float(params.get("concept_weight", 0.6)) * normalize(
            TfidfVectorizer(token_pattern=r"zz\w+", sublinear_tf=True).fit_transform(tag_docs)))
    X = normalize(hstack(blocks).tocsr())
    sim = np.asarray((X @ X.T).todense())
    dist = np.clip(1.0 - sim, 0.0, 2.0)
    np.fill_diagonal(dist, 0.0)
    dist = tie_break_distances(dist, keywords)  # deterministic tie order - identical clusters in Python and the browser
    sim = 1.0 - dist  # attach on the same exact values, so nearest-neighbour ties resolve identically too
    attach = float(params.get("attach_min_similarity", 0.25))
    labels: list[int] | None = None
    method = "agglomerative"
    if n >= int(params.get("hdbscan_min_keywords", 12)):
        try:
            from sklearn.cluster import HDBSCAN

            raw = HDBSCAN(min_cluster_size=int(params.get("min_cluster_size", 2)), min_samples=int(params.get("min_samples", 1)),
                          metric="precomputed", cluster_selection_method=str(params.get("selection_method", "leaf")),
                          copy=True).fit_predict(dist.copy())
            labels = attach_nearest([int(x) for x in raw], sim, attach)
            k = len(set(labels))
            if (k == 1 and n > 2 * int(params.get("hdbscan_min_keywords", 12))) or k > 0.8 * n:
                labels = None  # degenerate: everything one blob, or almost all singletons
            else:
                method = "hdbscan"
        except Exception:  # pragma: no cover - sklearn without HDBSCAN
            labels = None
    if labels is None:
        from sklearn.cluster import AgglomerativeClustering

        raw = AgglomerativeClustering(n_clusters=None, metric="precomputed", linkage="average",
                                      distance_threshold=float(params.get("agglomerative_distance_threshold", 0.8))).fit_predict(dist)
        labels = attach_nearest([int(x) for x in raw], sim, attach)
    return relabel(labels), method


def attach_nearest(labels: list[int], sim: Any, min_similarity: float) -> list[int]:
    """Noise (-1) and singleton keywords join the cluster of their most similar neighbour that sits
    in a cluster of two or more, when that similarity exceeds `min_similarity`; the rest stay alone."""
    out = list(labels)
    changed = True
    while changed:
        changed = False
        counts: dict[int, int] = {}
        for lab in out:
            counts[lab] = counts.get(lab, 0) + 1
        for i, lab in enumerate(out):
            if lab >= 0 and counts.get(lab, 0) > 1:
                continue
            best, best_sim = None, min_similarity
            for j, lj in enumerate(out):
                if j != i and lj >= 0 and counts.get(lj, 0) >= 2 and float(sim[i][j]) > best_sim:
                    best, best_sim = lj, float(sim[i][j])
            if best is not None and best != lab:
                if lab >= 0:
                    counts[lab] -= 1
                out[i] = best
                counts[best] = counts.get(best, 0) + 1
                changed = True
    next_label = max(out, default=-1) + 1
    for i, lab in enumerate(out):
        if lab < 0:
            out[i] = next_label
            next_label += 1
    return out


def relabel(labels: list[int]) -> list[int]:
    mapping: dict[int, int] = {}
    return [mapping.setdefault(lab, len(mapping)) for lab in labels]


def dominant_intent(rows: list[dict[str, Any]]) -> str:
    weights: dict[str, float] = {}
    for r in rows:
        w = float(r.get("impressions") or r.get("est_monthly_searches") or 1)
        weights[r["intent"]] = weights.get(r["intent"], 0.0) + w
    return sorted(weights.items(), key=lambda kv: (-kv[1], INTENTS.index(kv[0]) if kv[0] in INTENTS else 9))[0][0]


def pick_pillar(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Head term: most first-party impressions, else largest estimate; ties -> fewest words,
    highest opportunity value, relevance, shortest."""
    return max(rows, key=lambda r: (0 if r["is_estimate"] else 1, r.get("impressions") or r.get("est_monthly_searches") or 0,
                                    -len(r["keyword"].split()), r.get("value", 0.0), r.get("relevance", 0),
                                    -len(r["keyword"]), r["keyword"]))


def build_clusters(rows: list[dict[str, Any]], labels: list[int]) -> list[dict[str, Any]]:
    """Group scored keyword rows by label; clusters sorted by total opportunity, ids c1..ck."""
    groups: dict[int, list[dict[str, Any]]] = {}
    for r, lab in zip(rows, labels):
        groups.setdefault(lab, []).append(r)
    clusters = []
    for members in groups.values():
        members = sorted(members, key=lambda r: (-r["value"], r["keyword"]))
        pillar = pick_pillar(members)
        page_imp: dict[str, int] = {}
        for r in members:
            if r.get("page") and not r["is_estimate"]:
                page_imp[r["page"]] = page_imp.get(r["page"], 0) + int(r.get("impressions") or 0)
        existing = sorted(page_imp.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] if page_imp else None
        clusters.append({
            "pillar_keyword": pillar["keyword"], "intent": dominant_intent(members), "members": members,
            "total_impressions": sum(int(r.get("impressions") or 0) for r in members if not r["is_estimate"]),
            "potential_clicks": round(sum(r["potential_clicks"] for r in members), 1),
            "value": sum(r["value"] for r in members), "existing_page": existing,
            "pillar_position": pillar.get("position"),
            "is_estimate": all(r["is_estimate"] for r in members),
        })
    clusters.sort(key=lambda c: (-c["value"], c["pillar_keyword"]))
    scores = normalise_scores([c["value"] for c in clusters])
    for i, (c, s) in enumerate(zip(clusters, scores), 1):
        c["cluster_id"], c["opportunity_score"] = f"c{i}", s
    return clusters


def fallback_title(template: str, keyword: str) -> str:
    return template.replace("{Keyword}", keyword[:1].upper() + keyword[1:]).replace("{keyword}", keyword)


def expand_modifiers(topic: str, modifiers: list[str]) -> list[str]:
    t = normalize_keyword(topic)
    return [k for k in dict.fromkeys(normalize_keyword(m.replace("{topic}", t)) for m in modifiers) if k]


# ----------------------------------------------------------------------------- agent

@register
class KeywordResearchAgent(BaseAgent):
    spec = AgentSpec(
        id="keyword_research",
        name="Keyword Research",
        category=Category.STRATEGY,
        summary="Researches, clusters and ranks keyword opportunities for a topic or page from first-party search data, SERP context and model expansion - intent and opportunity computed in code.",
        kind=AgentKind.HYBRID,
        tier=Tier.BALANCED,
        connectors=["keyword_data", "search", "crawler"],
        skills=["keyword_intent", "brand_voice"],
        keywords=["keyword research", "keywords", "seo keywords", "search terms", "topic cluster", "pillar page",
                  "search intent", "keyword gap", "search console"],
        produces=["keywords.csv", "keyword_clusters.json"],
        feeds=["blog_post_creation", "campaign_brief_generation", "content_adaptation", "geo_recommendations"],
    )
    Input = KeywordResearchInput
    Output = KeywordResearchOutput
    schemas = {"expand": KeywordExpansion, "name_clusters": ClusterNaming}

    def selftest_input(self) -> dict[str, Any]:
        return {"topic": "home insurance", "page_url": "https://www.meridian-mutual.example/products/home-insurance.html",
                "seed_keywords": ["flood insurance for homeowners", "home insurance quote"], "market": "US", "max_keywords": 60}

    def stages(self):
        return [("gather", self.gather), ("expand", self.expand), ("score", self.score), ("cluster", self.cluster),
                ("recommend", self.recommend), ("export", self.export)]

    # -- stage 1: first-party data, SERP context, page context ----------------------------------
    def gather(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp = state["inputs"]
        topic, page_url = inp["topic"].strip(), inp.get("page_url")
        warnings: list[str] = []
        reasons: list[str] = []
        checks: list[Check] = []
        rows: list[dict[str, Any]] = []
        data_source = "keyword_data:none"
        try:
            kd = ctx.connector("keyword_data")
            data_source = f"keyword_data:{kd.provider}"
            if kd.available:
                df = kd.queries(topic=topic, limit=5000)
                rows = df.to_dict("records") if df is not None and len(df) else []
                if page_url:  # the page's own queries even when they do not mention the topic
                    full = kd.queries(topic=None, limit=20000)
                    if full is not None and len(full) and "page" in full:
                        seen = {(str(r.get("query")), str(r.get("page"))) for r in rows}
                        rows += [r for r in full.to_dict("records") if same_page(str(r.get("page") or ""), page_url)
                                 and (str(r.get("query")), str(r.get("page"))) not in seen]
            else:
                reasons.append("no first-party keyword data (keyword_data provider 'none') - volumes and scores are estimates")
        except Exception as exc:
            reasons.append(f"keyword data unavailable ({type(exc).__name__}: {exc}) - volumes and scores are estimates")
        serp: list[dict[str, Any]] = []
        try:
            search = ctx.connector("search")
            if search.available:
                own = urlsplit(str(ctx.brand.get("website") or page_url or "")).netloc.lower().removeprefix("www.")
                for i, r in enumerate(search.search(topic, num=10), 1):
                    host = urlsplit(r.url).netloc.lower().removeprefix("www.")
                    serp.append({"rank": i, "title": r.title, "url": r.url, "source": r.source, "own_domain": bool(own) and host == own})
            else:
                warnings.append("No search provider configured: SERP context was skipped.")
        except Exception as exc:
            warnings.append(f"SERP context unavailable: {type(exc).__name__}: {exc}")
        page: dict[str, Any] = {}
        if page_url:
            try:
                pg = ctx.connector("crawler").fetch(page_url)
                if pg.ok and pg.is_html:
                    soup = pg.soup()
                    h1 = soup.find("h1")
                    page = {"title": (soup.title.get_text(" ", strip=True) if soup.title else ""),
                            "h1": h1.get_text(" ", strip=True) if h1 else ""}
                else:
                    warnings.append(f"Could not read {page_url} (status {pg.status}); page context skipped.")
            except Exception as exc:
                warnings.append(f"Could not read {page_url}: {type(exc).__name__}")
        checks.append(Check(rule="data.first_party", status="pass" if rows else "warn", severity="medium",
                            message=f"{len(rows)} first-party query rows from {data_source}." if rows
                            else f"No first-party query rows from {data_source}; results are estimates."))
        brief = ctx.upstream_output("campaign_brief_generation") or {}
        return {"data": {"rows": rows, "serp": serp, "page": page, "data_source": data_source,
                         "key_messages": list(brief.get("key_messages") or [])[:5]},
                "warnings": warnings, "checks": checks,
                "provenance": {"degraded_reasons": list((state.get("provenance") or {}).get("degraded_reasons", [])) + reasons}}

    # -- stage 2: model expansion (candidates are always estimates) -----------------------------
    def expand(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        rules = ctx.skill("keyword_intent")
        model = rules["opportunity"]
        known = aggregate_queries(data["rows"], inp.get("page_url"), float(model.get("min_impressions", 1)))
        known_keys = {keyword_key(k["keyword"]) for k in known}
        seeds = [s for s in (normalize_keyword(x) for x in inp.get("seed_keywords") or []) if s]
        rel_cfg = rules.get("relevance") or {}
        rel = relevance_scores([k["keyword"] for k in known], [inp["topic"]] + seeds, ignore=modifier_tokens(rules),
                               synonyms=rel_cfg.get("synonyms"))
        relevant = [k for k, r in zip(known, rel) if r >= float(rel_cfg.get("min_relevance", 0.5)) or k["on_target_page"]]
        thin = len(relevant) < int(rules.get("min_first_party", 8))
        modifiers = expand_modifiers(inp["topic"], rules.get("expansion_modifiers", [])) if thin else []
        estimate_for = [k for k in dict.fromkeys(seeds + modifiers) if keyword_key(k) not in known_keys][:20]
        max_new = max(5, min(25, int(inp.get("max_keywords", 60)) // 2))
        expansion = ctx.generate(
            "expand", KeywordExpansion, topic=inp["topic"], market=inp.get("market"), page_url=inp.get("page_url"),
            page_title=data["page"].get("title") or data["page"].get("h1"),
            known=[f"{k['keyword']} | {k['impressions']} impressions | position {k['position']}" for k in relevant[:25]],
            serp_titles=[s["title"] for s in data["serp"]][:10], estimate_for=estimate_for, max_new=max_new,
            key_messages=data.get("key_messages", []))
        return {"data": {"known": known, "seeds": seeds, "modifiers": modifiers, "max_new": max_new,
                         "candidates": [c.model_dump() for c in expansion.keywords]}}

    # -- stage 3: merge, dedupe, relevance gate, intent, opportunity ----------------------------
    def score(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        rules = ctx.skill("keyword_intent")
        model = rules["opportunity"]
        defaults = model.get("estimate_defaults") or {}
        checks: list[Check] = []
        merged: dict[str, dict[str, Any]] = {}
        for k in data["known"]:
            merged[keyword_key(k["keyword"])] = {**k, "source": "first_party", "is_estimate": False, "est_monthly_searches": None}
        estimates = {keyword_key(c["keyword"]): c.get("est_monthly_searches") for c in data["candidates"] if normalize_keyword(c["keyword"])}

        def add(kw: str, source: str) -> None:
            key = keyword_key(kw)
            if not key or key in merged:
                if key in merged and source == "seed":
                    merged[key]["seed"] = True
                return
            est = estimates.get(key)
            merged[key] = {"keyword": kw, "impressions": None, "clicks": None, "ctr": None, "position": None, "page": None,
                           "pages": [], "cannibalised": False, "on_target_page": False, "source": source, "is_estimate": True,
                           "est_monthly_searches": int(est) if est is not None else None, "seed": source == "seed"}

        for s in data["seeds"]:
            add(s, "seed")
        for m in data["modifiers"]:
            add(m, "modifier")
        new_model = 0
        for c in data["candidates"]:
            kw = normalize_keyword(c["keyword"])
            if kw and keyword_key(kw) not in merged and new_model < data["max_new"]:
                add(kw, "model")
                new_model += 1

        rows = list(merged.values())
        rel_cfg = rules.get("relevance") or {}
        rel = relevance_scores([r["keyword"] for r in rows], [inp["topic"]] + data["seeds"], ignore=modifier_tokens(rules),
                               synonyms=rel_cfg.get("synonyms"))
        min_rel = float(rel_cfg.get("min_relevance", 0.5))
        kept, dropped = [], []
        for r, score in zip(rows, rel):
            r["relevance"] = score
            if r.get("seed") or r.get("on_target_page") or score >= min_rel:
                kept.append(r)
            else:
                dropped.append(r)
        if dropped:
            checks.append(Check(rule="keywords.relevance", status="info", severity="low",
                                message=f"{len(dropped)} candidate(s) below relevance {min_rel} were dropped "
                                        f"({sum(1 for d in dropped if d['source'] == 'first_party')} first-party, "
                                        f"{sum(1 for d in dropped if d['source'] == 'model')} model).",
                                data={"examples": [d["keyword"] for d in dropped[:8]]}))

        brand = brand_terms_from(ctx.brand, rules.get("brand_terms"))
        competitors = [normalize_keyword(t) for t in rules.get("competitor_terms") or []]
        for r in kept:
            r["intent"], r["intent_rule"] = classify_intent(r["keyword"], rules, brand, competitors)
            if r["is_estimate"]:
                volume = r["est_monthly_searches"] if r["est_monthly_searches"] is not None else defaults.get("monthly_searches", 50)
                s = score_keyword(impressions=volume, ctr=0.0, position=None, intent=r["intent"], is_estimate=True, model=model)
            else:
                s = score_keyword(impressions=r["impressions"], ctr=r["ctr"], position=r["position"], intent=r["intent"],
                                  is_estimate=False, model=model)
            r.update(s)
        kept.sort(key=lambda r: (-r["value"], r["keyword"]))
        cap = int(inp.get("max_keywords", 60))
        seeds_kept = [r for r in kept if r.get("seed")][:cap]
        rest = [r for r in kept if not r.get("seed")][: max(0, cap - len(seeds_kept))]
        final = sorted(seeds_kept + rest, key=lambda r: (-r["value"], r["keyword"]))
        for r, s in zip(final, normalise_scores([r["value"] for r in final])):
            r["opportunity_score"] = s
        cannibal = [r for r in final if r.get("cannibalised")]
        if cannibal:
            checks.append(Check(rule="keywords.cannibalisation", status="warn", severity="medium",
                                message=f"{len(cannibal)} keyword(s) split impressions across several pages: "
                                        + ", ".join(f"'{r['keyword']}'" for r in cannibal[:5]),
                                data={"keywords": {r["keyword"]: r["pages"][:4] for r in cannibal[:10]}}))
        n_est = sum(1 for r in final if r["is_estimate"])
        checks.append(Check(rule="keywords.estimates", status="info" if n_est < len(final) else "warn", severity="low",
                            message=f"{len(final) - n_est} keyword(s) backed by first-party data, {n_est} flagged as estimates."))
        prev = list((state.get("provenance") or {}).get("degraded_reasons", []))
        if n_est == len(final) and not prev:  # provider configured but nothing usable came back
            prev.append("first-party keyword data returned no relevant queries for this topic - volumes and scores are estimates")
        return {"data": {"scored": final}, "checks": checks, "provenance": {"degraded_reasons": prev}}

    # -- stage 4: in-process clustering ---------------------------------------------------------
    def cluster(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        rows = state["data"]["scored"]
        params = ctx.skill("keyword_intent").get("clustering") or {}
        labels, method = cluster_keywords([r["keyword"] for r in rows], params)
        clusters = build_clusters(rows, labels)
        check = Check(rule="keywords.clustering", status="info", severity="low",
                      message=f"{len(rows)} keywords grouped into {len(clusters)} clusters ({method}, TF-IDF word + char n-grams + concept tags).")
        return {"data": {"clusters": clusters, "clustering_method": method}, "checks": [check]}

    # -- stage 5: model names clusters + suggests content; reconciled deterministically ---------
    def recommend(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        rules = ctx.skill("keyword_intent")
        formats = rules.get("content_formats") or {}
        clusters = data["clusters"]
        naming = ctx.generate(
            "name_clusters", ClusterNaming, topic=inp["topic"], market=inp.get("market"),
            clusters=[{"cluster_id": c["cluster_id"], "intent": c["intent"], "pillar_keyword": c["pillar_keyword"],
                       "opportunity_score": c["opportunity_score"], "existing_page": c["existing_page"],
                       "keywords": [m["keyword"] for m in c["members"][:10]]} for c in clusters[:20]]) if clusters else ClusterNaming()
        by_id = {s.cluster_id.strip().lower(): s for s in naming.clusters}
        recs = []
        for c in clusters:
            sug = by_id.get(c["cluster_id"])
            fmt = formats.get(c["intent"]) or {"format": "article", "title": "{Keyword}"}
            c["name"] = (sug.name.strip() if sug and sug.name.strip() else fallback_title("{Keyword}", c["pillar_keyword"]))[:80]
            ranking = c["existing_page"] and c["pillar_position"] is not None and c["pillar_position"] <= 20
            supporting = [s.strip() for s in (sug.supporting_content if sug else []) if s.strip()][:6] or \
                [fallback_title("{Keyword}", m["keyword"]) for m in c["members"] if m["keyword"] != c["pillar_keyword"]][:3]
            recs.append(ContentRecommendation(
                cluster_id=c["cluster_id"], cluster_name=c["name"], action="optimise" if ranking else "create",
                content_type=fmt.get("format", "article"),
                pillar_title=(sug.pillar_title.strip() if sug and sug.pillar_title.strip() else fallback_title(fmt.get("title", "{Keyword}"), c["pillar_keyword"]))[:120],
                target_page=c["existing_page"] if ranking else None,
                target_keywords=[c["pillar_keyword"]] + [m["keyword"] for m in c["members"] if m["keyword"] != c["pillar_keyword"]][:5],
                supporting_content=supporting,
                rationale=(sug.rationale.strip() if sug and sug.rationale.strip() else
                           f"{c['intent'].capitalize()} cluster of {len(c['members'])} keywords, "
                           f"{c['total_impressions']} first-party impressions, ~{c['potential_clicks']:.0f} extra clicks available."),
                is_estimate=c["is_estimate"]).model_dump())
        matched = sum(1 for c in clusters if c["cluster_id"] in by_id)
        check = Check(rule="model.cluster_names", status="pass" if matched == len(clusters) else "info", severity="low",
                      message=f"Model named {matched}/{len(clusters)} clusters; the rest use the pillar keyword.")
        return {"data": {"clusters": clusters, "recommendations": recs}, "checks": [check]}

    # -- stage 6: output + keywords CSV ---------------------------------------------------------
    def export(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        clusters_out: list[KeywordCluster] = []
        all_rows: list[KeywordRow] = []
        for c in data["clusters"]:
            rows = [_row(m, c["cluster_id"]) for m in c["members"]]
            all_rows += rows
            clusters_out.append(KeywordCluster(
                cluster_id=c["cluster_id"], name=c["name"], pillar_keyword=c["pillar_keyword"], intent=c["intent"],
                keyword_count=len(rows), total_impressions=c["total_impressions"], potential_clicks=c["potential_clicks"],
                opportunity_score=c["opportunity_score"], existing_page=c["existing_page"], is_estimate=c["is_estimate"],
                keywords=rows))
        top = sorted(all_rows, key=lambda r: (-r.opportunity_score, r.keyword))[:10]
        estimate = not any(not r.is_estimate for r in all_rows)
        findings = _findings(top, all_rows)
        names = {c.cluster_id: c.name for c in clusters_out}
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["cluster_id", "cluster_name", "keyword", "intent", "source", "is_estimate", "impressions", "clicks", "ctr",
                    "position", "est_monthly_searches", "relevance", "opportunity_score", "potential_clicks", "opportunity_type", "page"])
        for r in sorted(all_rows, key=lambda r: (int(r.cluster_id[1:] or 0), -r.opportunity_score, r.keyword)):
            w.writerow([r.cluster_id, names.get(r.cluster_id, ""), r.keyword, r.intent, r.source, r.is_estimate,
                        "" if r.impressions is None else r.impressions, "" if r.clicks is None else r.clicks,
                        "" if r.ctr is None else r.ctr, "" if r.position is None else r.position,
                        "" if r.est_monthly_searches is None else r.est_monthly_searches, r.relevance,
                        r.opportunity_score, r.potential_clicks, r.opportunity_type, r.page or ""])
        best = top[0] if top else None
        summary = (f"{len(all_rows)} keywords in {len(clusters_out)} clusters for '{inp['topic']}'"
                   + (f"; top opportunity '{best.keyword}' ({best.opportunity_type.replace('_', ' ')}, score {best.opportunity_score:.0f})" if best else "")
                   + (" - estimates only, no first-party data" if estimate else ""))
        out = KeywordResearchOutput(
            topic=inp["topic"], market=inp.get("market"), summary=summary, data_source=data["data_source"], is_estimate=estimate,
            clustering_method=data["clustering_method"], keyword_count=len(all_rows), clusters=clusters_out, top_opportunities=top,
            content_recommendations=[ContentRecommendation.model_validate(r) for r in data["recommendations"]],
            serp_context=[SerpItem.model_validate(s) for s in data["serp"]], findings=findings,
            skill_version=str(ctx.skill("keyword_intent").get("version", "")))
        artifacts = [ctx.artifacts.write_text("keywords.csv", buf.getvalue(), "text/csv", "Keywords with intent, metrics and opportunity"),
                     ctx.artifacts.write_json("keyword_clusters.json", [c.model_dump(mode="json") for c in clusters_out], "Keyword clusters")]
        warnings = ["Keyword volumes are estimates (no first-party data) - validate before committing budget."] if estimate else []
        return {"output": out.model_dump(mode="json"), "artifacts": artifacts, "summary": summary, "warnings": warnings}


def _row(m: dict[str, Any], cluster_id: str) -> KeywordRow:
    return KeywordRow(keyword=m["keyword"], cluster_id=cluster_id, intent=m["intent"], intent_rule=m.get("intent_rule", ""),
                      source=m["source"], is_estimate=m["is_estimate"], relevance=m.get("relevance", 0.0),
                      impressions=m.get("impressions"), clicks=m.get("clicks"), ctr=m.get("ctr"), position=m.get("position"),
                      est_monthly_searches=m.get("est_monthly_searches"), expected_ctr=m.get("expected_ctr", 0.0),
                      potential_clicks=m.get("potential_clicks", 0.0), opportunity_score=m.get("opportunity_score", 0.0),
                      opportunity_type=m.get("opportunity_type", ""), position_band=m.get("position_band", ""),
                      page=m.get("page"), cannibalised=bool(m.get("cannibalised")))


_ADVICE = {
    "ctr_gap": "Rewrite the title tag and meta description - the page ranks well but under-earns clicks for its position.",
    "push_to_first": "Strengthen the page (depth, internal links, schema) to move from the top 3 to #1.",
    "page_one_push": "Improve on-page relevance and internal links to move into the top 3.",
    "striking_distance": "Expand the content and add internal links - page two rankings are the quickest page-one wins.",
    "long_shot": "Create or consolidate dedicated content; current rankings are too deep to earn clicks.",
    "new_content": "Create dedicated content - no first-party ranking exists for this keyword.",
}


def _findings(top: list[KeywordRow], rows: list[KeywordRow]) -> list[Finding]:
    out: list[Finding] = []
    for r in [r for r in top if r.opportunity_type in _ADVICE][:5]:
        evidence = (f"{r.impressions} impressions, CTR {r.ctr or 0:.1%} vs expected {r.expected_ctr:.1%} at position {r.position}"
                    if not r.is_estimate else f"estimated {r.est_monthly_searches or 'unknown'} monthly searches (model estimate)")
        out.append(Finding(title=f"'{r.keyword}': {r.opportunity_type.replace('_', ' ')} (opportunity {r.opportunity_score:.0f}/100)",
                           detail=f"{r.intent.capitalize()} intent; ~{r.potential_clicks:.0f} extra clicks available at the target position.",
                           severity="high" if r.opportunity_score >= 60 else "medium", category="seo", evidence=evidence,
                           recommendation=_ADVICE[r.opportunity_type], target=r.page, source_agent="keyword_research"))
    for r in [r for r in rows if r.cannibalised][:3]:
        out.append(Finding(title=f"Keyword cannibalisation: '{r.keyword}'", severity="medium", category="seo",
                           detail="Two or more pages split this keyword's impressions, diluting relevance signals.",
                           evidence=f"{r.impressions} impressions across several pages; top page {r.page}",
                           recommendation="Pick one canonical page for the keyword, consolidate content and point internal links to it.",
                           target=r.page, source_agent="keyword_research"))
    return out
