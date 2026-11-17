"""Keyword Research: deterministic intent rules, CTR curve, opportunity maths, aggregation,
relevance gate and clustering - on the Search Console sample (sample_data/keywords/gsc_queries.csv)."""

import csv
import io

import pandas as pd
import pytest
import yaml

from caf.agents.strategy import keyword_research as kr
from caf.config import get_settings
from caf.core import RunContext, get_agent

ROOT = get_settings().home
RULES = yaml.safe_load((ROOT / "config/skills/keyword_intent.yaml").read_text())
MODEL = RULES["opportunity"]
BRAND = kr.brand_terms_from({"name": "Meridian Mutual", "website": "https://www.meridian-mutual.example"})


def _ctx_with(**providers):
    settings = get_settings().model_copy(deep=True)
    for kind, provider in providers.items():
        settings.connectors[kind] = {**settings.connectors.get(kind, {}), "provider": provider}
    return RunContext.create(settings=settings)


@pytest.mark.parametrize("keyword,intent", [
    ("meridian mutual login", "navigational"),
    ("meridian mutual home insurance", "navigational"),
    ("home insurance quote", "transactional"),
    ("homeowners insurance quotes", "transactional"),   # plural matched through stemming
    ("home insurance near me", "transactional"),
    ("best home insurance", "commercial"),
    ("cheap home insurance", "commercial"),
    ("home insurance cost", "commercial"),
    ("how much is home insurance", "informational"),
    ("does home insurance cover flooding", "informational"),
    ("home maintenance checklist", "informational"),
    ("flood insurance", "informational"),               # no modifier -> default intent
])
def test_intent_rules(keyword, intent):
    got, rule = kr.classify_intent(keyword, RULES, BRAND)
    assert got == intent, (keyword, got, rule)


def test_expected_ctr_curve_interpolates_and_never_rises():
    curve = MODEL["expected_ctr_curve"]
    assert kr.expected_ctr(1, curve) == pytest.approx(0.30)
    assert kr.expected_ctr(2.5, curve) == pytest.approx((0.16 + 0.11) / 2)
    assert kr.expected_ctr(500, curve) == 0.0
    values = [kr.expected_ctr(p / 2, curve) for p in range(2, 200)]
    assert all(a >= b for a, b in zip(values, values[1:]))


def test_opportunity_maths_and_types():
    s = kr.score_keyword(impressions=10_000, ctr=0.01, position=12, intent="informational", is_estimate=False, model=MODEL)
    assert s["potential_clicks"] == pytest.approx(10_000 * (0.11 - 0.01), rel=1e-6)   # to the target position (3)
    assert s["value"] == pytest.approx(1000 * 0.85 * 0.7, rel=1e-6)                    # striking-distance ease x intent weight
    assert s["opportunity_type"] == "striking_distance"
    small = kr.score_keyword(impressions=500, ctr=0.01, position=12, intent="informational", is_estimate=False, model=MODEL)
    assert small["value"] < s["value"]
    gap = kr.score_keyword(impressions=5000, ctr=0.05, position=2, intent="commercial", is_estimate=False, model=MODEL)
    assert gap["opportunity_type"] == "ctr_gap"            # ranks #2 but earns a third of the expected CTR
    assert kr.score_keyword(impressions=5000, ctr=0.9, position=1, intent="commercial", is_estimate=False,
                            model=MODEL)["opportunity_type"] == "hold"
    assert kr.score_keyword(impressions=5000, ctr=0.1, position=1, intent="navigational", is_estimate=False,
                            model=MODEL)["opportunity_type"] == "brand"
    est = kr.score_keyword(impressions=10_000, ctr=0.0, position=None, intent="informational", is_estimate=True, model=MODEL)
    assert est["opportunity_type"] == "new_content" and est["value"] < s["value"]


def test_aggregation_merges_pages_and_flags_cannibalisation():
    df = pd.read_csv(ROOT / "sample_data/keywords/gsc_queries.csv")
    rows = {r["keyword"]: r for r in kr.aggregate_queries(df.to_dict("records"))}
    flood = rows["flood insurance"]                      # ranks with the product page and a blog post
    assert flood["impressions"] == 74_100 + 21_800
    assert flood["position"] == pytest.approx((26.3 * 74_100 + 14.2 * 21_800) / 95_900, abs=0.01)
    assert flood["ctr"] == pytest.approx((229 + 335) / 95_900, abs=1e-4)
    assert flood["cannibalised"] and len(flood["pages"]) == 2
    assert not rows["home insurance quote"]["cannibalised"]


def test_relevance_gate_ignores_modifiers_and_uses_synonyms():
    rel = kr.relevance_scores(["car insurance quote", "auto insurance quotes", "home insurance quote", "house insurance",
                               "homeowners insurance", "pet insurance"], ["home insurance", "home insurance quote"],
                              ignore=kr.modifier_tokens(RULES), synonyms=RULES["relevance"]["synonyms"])
    threshold = RULES["relevance"]["min_relevance"]
    car, auto, home, house, owners, pet = rel
    assert car < threshold and auto < threshold and pet < threshold
    assert home == 1.0 and house >= threshold and owners >= threshold


def test_clustering_groups_related_keywords():
    kws = ["flood insurance", "flood zone insurance", "private flood insurance", "parametric flood insurance",
           "home insurance quote", "homeowners insurance quotes", "flood insurance quote",
           "does home insurance cover flooding", "what does home insurance cover", "does home insurance cover water damage",
           "home insurance cost", "average home insurance cost 2026", "home insurance rebuild cost",
           "how to make a home insurance claim", "home insurance claim tips", "home insurance claim denied",
           "home maintenance checklist", "winter home checklist"]
    labels, method = kr.cluster_keywords(kws, RULES["clustering"])
    assert method == "hdbscan"
    lab = dict(zip(kws, labels))
    assert lab["flood zone insurance"] == lab["private flood insurance"] == lab["flood insurance"]
    assert lab["does home insurance cover flooding"] == lab["what does home insurance cover"]
    assert lab["home insurance claim tips"] == lab["home insurance claim denied"]
    assert lab["home maintenance checklist"] == lab["winter home checklist"]
    assert lab["home insurance quote"] != lab["home insurance claim tips"] != lab["home maintenance checklist"]
    small, small_method = kr.cluster_keywords(kws[:5], RULES["clustering"])
    assert small_method == "agglomerative" and len(small) == 5
    assert kr.cluster_keywords([], RULES["clustering"]) == ([], "none")


# Clusters the browser twin (console/src/js/agents/keyword_research.js) produces for the selftest input,
# captured with console/test/harness.py. tie_break_distances makes both runtimes cluster identically.
BROWSER_SELFTEST_CLUSTERS = [
    ["home insurance"],
    ["flood insurance quote", "home insurance quote", "homeowners insurance quotes"],
    ["flood insurance for homeowners", "home insurance for older homes", "homeowners insurance", "house insurance",
     "renters vs homeowners insurance"],
    ["flood insurance", "flood insurance cost", "flood zone insurance", "private flood insurance"],
    ["best home insurance", "bundle home and car insurance", "home and auto bundle", "home insurance near me",
     "switch home insurance"],
    ["average home insurance cost 2026", "cheap home insurance", "home insurance calculator", "home insurance cost",
     "home insurance deductible", "home insurance discounts", "home insurance rebuild cost"],
    ["accidental damage cover home insurance", "does home insurance cover flooding", "does home insurance cover water damage",
     "home emergency cover", "is flood damage covered by homeowners insurance", "what does home insurance cover"],
    ["climate change home insurance", "home maintenance checklist", "resilience credits home insurance", "water leak sensor",
     "winter home checklist"],
    ["buildings and contents insurance", "contents insurance", "mobile home insurance", "vacation home insurance"],
    ["how much is home insurance", "why is my home insurance going up"],
    ["insurance for rental property", "rental property insurance"],
    ["harborline home insurance price increase", "home insurance excess explained", "home insurance price increase 2027"],
    ["home insurance claim tips", "home insurance tips", "how to cancel home insurance", "how to make a home insurance claim"],
    ["does making a claim increase home insurance premium", "how long does a home insurance claim take"],
    ["home insurance wildfire risk", "wildfire home protection"],
    ["alternatives to harborline insurance"],
    ["umbrella insurance"],
    ["home warranty vs home insurance"],
    ["change address home insurance"],
    ["mutual home insurance"],
]


def test_tie_break_makes_distances_unique_and_order_preserving():
    import numpy as np

    kws = ["home insurance cost", "cheap home insurance", "home insurance price", "flood cover", "flood insurance"]
    dist = np.full((5, 5), 0.5)
    np.fill_diagonal(dist, 0.0)
    dist[0, 1] = dist[1, 0] = 0.25
    out = kr.tie_break_distances(dist, kws)
    off = out[np.triu_indices(5, 1)]
    assert len(set(off.tolist())) == len(off)                          # exact ties are gone
    assert (out == out.T).all() and (np.diag(out) == 0).all()
    assert out[0, 1] == off.min() and np.abs(out - dist).max() < 1e-8  # real gaps and values kept


def test_selftest_clusters_match_browser_twin(ctx):
    agent = get_agent("keyword_research")
    out = agent.run(agent.selftest_input(), ctx).output
    got = [sorted(k["keyword"] for k in c["keywords"]) for c in out["clusters"]]
    assert len(got) == len(BROWSER_SELFTEST_CLUSTERS) == 20
    assert got == BROWSER_SELFTEST_CLUSTERS


def test_agent_on_first_party_sample(ctx, tmp_path):
    agent = get_agent("keyword_research")
    res = agent.run(agent.selftest_input(), ctx)
    assert res.status != "error", res.error
    out = res.output
    assert out["data_source"] == "keyword_data:csv" and not out["is_estimate"]
    keywords = [k for c in out["clusters"] for k in c["keywords"]]
    names = {k["keyword"] for k in keywords}
    assert "car insurance" not in names and "pet insurance" not in names          # other products gated out
    assert {"home insurance quote", "flood insurance for homeowners"} <= names        # seeds always kept
    assert len(keywords) == out["keyword_count"] <= agent.selftest_input()["max_keywords"]
    assert out["clustering_method"] == "hdbscan"
    assert len({c["cluster_id"] for c in out["clusters"]}) == len(out["clusters"]) == len(out["content_recommendations"])
    assert out["top_opportunities"][0]["keyword"] == "home insurance"                 # 121k impressions at position 18.4
    assert out["top_opportunities"][0]["opportunity_score"] == 100.0
    seed = next(k for k in keywords if k["keyword"] == "flood insurance for homeowners")
    assert seed["is_estimate"] and seed["source"] == "seed"
    assert any(c.rule == "keywords.cannibalisation" and "flood insurance" in c.message for c in res.checks)
    assert any(f["title"].startswith("Keyword cannibalisation: 'flood insurance'") for f in out["findings"])
    csv_art = next(a for a in res.artifacts if a.name == "keywords.csv")
    rows = list(csv.DictReader(io.StringIO((ctx.settings.runs_dir / csv_art.path).read_text())))
    assert len(rows) == out["keyword_count"] and {"keyword", "intent", "opportunity_score", "is_estimate"} <= set(rows[0])


def test_none_provider_returns_flagged_estimate():
    agent = get_agent("keyword_research")
    res = agent.run({"topic": "home insurance", "seed_keywords": ["flood insurance for homeowners"]}, _ctx_with(keyword_data="none"))
    assert res.status == "degraded"
    out = res.output
    assert out["is_estimate"] and out["data_source"] == "keyword_data:none"
    keywords = [k for c in out["clusters"] for k in c["keywords"]]
    assert keywords and all(k["is_estimate"] for k in keywords)
    assert {"home insurance quote", "best home insurance"} <= {k["keyword"] for k in keywords}   # skill modifiers
    assert any("no first-party keyword data" in r for r in res.provenance["degraded_reasons"])


def test_page_queries_are_kept_even_off_topic(ctx):
    agent = get_agent("keyword_research")
    res = agent.run({"topic": "flood insurance", "page_url": "https://www.meridian-mutual.example/products/home-insurance.html",
                     "max_keywords": 200}, ctx)
    names = {k["keyword"] for c in res.output["clusters"] for k in c["keywords"]}
    assert "bundle home and car insurance" in names       # ranks on the requested page, not about flood
    assert "parametric flood insurance" in names


def test_off_topic_first_party_data_falls_back_to_flagged_estimates(ctx):
    res = get_agent("keyword_research").run({"topic": "quantum widget repair"}, ctx)
    out = res.output
    assert res.status == "degraded" and out["is_estimate"] and out["keyword_count"] >= 5
    assert all(k["is_estimate"] and k["source"] == "modifier" for c in out["clusters"] for k in c["keywords"])
    assert any("no relevant queries" in r for r in res.provenance["degraded_reasons"])
