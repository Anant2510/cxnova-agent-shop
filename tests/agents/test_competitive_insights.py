"""Competitive Insights: deterministic evidence typing, windows, impact events, citation validation,
parallel multi-competitor runs, the record-store cache and the no-search estimate path - on the
search fixture (Harborline Insurance, Crestview Assurance; fixture 'today' = 2026-10-18)."""

from datetime import date

import pytest

from caf.agents.strategy import competitive_insights as ci
from caf.config import get_settings
from caf.core import RunContext, get_agent

REF = date(2026, 10, 18)


def _ctx_with(**providers):
    settings = get_settings().model_copy(deep=True)
    for kind, provider in providers.items():
        settings.connectors[kind] = {**settings.connectors.get(kind, {}), "provider": provider}
    return RunContext.create(settings=settings)


@pytest.mark.parametrize("item,expected", [
    ({"url": "https://www.harborline.example/newsroom/x", "publisher": "Harborline Newsroom", "title": "Harborline partners with Ambit"}, "press"),
    ({"url": "https://www.prnewswire.com/news/x", "publisher": "PR Newswire", "title": "Crestview names CFO"}, "press"),
    ({"url": "https://forumly.example/r/homeinsurance/1", "publisher": "Forumly r/HomeInsurance", "title": "Harborline renewal up 31%??"}, "community"),
    ({"url": "https://kestrel-research.example/notes/1", "publisher": "Kestrel Research", "title": "Harborline Q3 preview"}, "analyst"),
    ({"url": "https://adscene.example/c/1", "publisher": "AdScene", "title": "Harborline debuts 'Home, Handled' TV campaign"}, "campaign"),
    ({"url": "https://policydesk.example/1", "publisher": "The Policy Desk", "title": "Harborline's new app slides to 2.1 stars",
      "snippet": "Reviews cite log-in loops."}, "community"),
    ({"url": "https://news.example/1", "publisher": "Wire", "title": "Crestview downgraded by Fitch"}, "analyst"),
    ({"url": "https://insuranceledger.example/1", "publisher": "Insurance Ledger", "title": "Crestview launches home resilience grants",
      "snippet": "Homeowners who replace a roof with impact-rated materials can apply."}, "news"),
])
def test_evidence_types_are_deterministic(item, expected):
    assert ci.classify_evidence(item, competitor_domain="harborline.example") == expected


def test_competitor_resolution():
    assert ci.resolve_competitor("https://www.harborline.example/") == {
        "input": "https://www.harborline.example/", "name": "Harborline", "url": "https://www.harborline.example/", "domain": "harborline.example"}
    assert ci.resolve_competitor("Crestview Assurance")["domain"] is None
    assert ci.name_tokens("Crestview Assurance") == ["crestview"]
    assert ci.infer_domain("Harborline Insurance", ["https://www.harborline.example/newsroom/a", "https://insuranceledger.example/harborline"]) \
        == "harborline.example"


def test_windows_ids_and_signals():
    raw = [{"title": "Harborline launches FloodFlex parametric flood cover", "url": "https://h.example/1", "published": "2026-08-17"},
           {"title": "State regulator fines Harborline $4.2 million", "url": "https://d.example/2", "published": "2026-09-25"},
           {"title": "Harborline says backlog resolved after $4.2M fine", "url": "https://l.example/3", "published": "Sat, 26 Sep 2026 08:00:00 GMT"},
           {"title": "Harborline cuts broker commission to 10%", "url": "https://b.example/4", "published": "2026-10-13",
            "snippet": "follows the relaunched app"},
           {"title": "Harborline cuts broker commission to 10%", "url": "https://dupe.example/5", "published": "2026-10-13"}]
    items = ci.finalize_evidence(raw, competitor="Harborline", index=1, domain=None, ref=REF, days=30, events_days=60, max_items=25)
    titles = [i["title"] for i in items]
    assert "Harborline launches FloodFlex parametric flood cover" not in titles      # 62 days old > events_days
    assert len(items) == 3                                                             # duplicate title collapsed
    assert [i["id"] for i in items] == ["C1-E01", "C1-E02", "C1-E03"] and items[0]["published"] == "2026-10-13"
    assert items[1]["published"] == "2026-09-26"                                       # RFC 822 pubDate parsed
    commission = items[0]
    assert commission["signals"] == ["pricing", "launch"] and commission["lead_signal"] == "pricing"
    assert commission["impact_score"] == pytest.approx(3 + 0.5 * 3)                    # title full, snippet half
    events = ci.select_events(items)
    assert [e["title"] for e in events].count("State regulator fines Harborline $4.2 million") == 1
    assert "Harborline says backlog resolved after $4.2M fine" not in [e["title"] for e in events]   # same story, original kept


def test_claim_validation_drops_uncited_and_flags_unknown_ids():
    id_types = {"C1-E01": "news", "C1-E02": "analyst"}
    claims = [{"text": "Harborline raised rates.", "evidence_ids": ["C1-E01"]},
              {"text": "Analysts expect a 104% combined ratio [C1-E02].", "evidence_ids": []},
              {"text": "Harborline is exiting Texas.", "evidence_ids": ["C1-E99"]},
              {"text": "Margins look thin.", "evidence_ids": ["C1-E02", "C9-E01"]},
              {"text": "  ", "evidence_ids": ["C1-E01"]}]
    kept, stats = ci.validate_claims(claims, id_types, estimate_mode=False)
    assert stats == {"kept": 3, "dropped": 2, "flagged": 1}
    assert [k["evidence_ids"] for k in kept] == [["C1-E01"], ["C1-E02"], ["C1-E02"]]
    assert kept[1]["text"] == "Analysts expect a 104% combined ratio."               # inline marker parsed out of the text
    assert "removed unknown evidence ids: C9-E01" in kept[2]["flags"]
    analyst_only, _ = ci.validate_claims([{"text": "x", "evidence_ids": ["C1-E01"]}], id_types, estimate_mode=False, want_types=("analyst",))
    assert analyst_only[0]["flags"] == ["cites no analyst evidence"]
    est, est_stats = ci.validate_claims(claims[:1], {}, estimate_mode=True)
    assert est[0]["basis"] == "model_knowledge" and est[0]["is_estimate"] and est[0]["evidence_ids"] == []


def test_actions_grouped_by_priority():
    acts = [{"priority": p, "action": f"do {i}", "evidence_ids": ["C1-E01"]} for i, p in enumerate(["P1"] * 4 + ["P3", "P9"])]
    groups = ci.validate_actions(acts, {"C1-E01": "news"}, estimate_mode=False)
    assert len(groups["P1"]) == 3 and len(groups["P3"]) == 1 and len(groups["P2"]) == 1   # capped; unknown priority -> P2
    assert all(a["evidence_backed"] for g in groups.values() for a in g)


def test_press_page_and_sitemap_parsing():
    html = """<html><body><ul>
      <li><h3><a href="/press/a">Harborline launches FloodFlex in twelve states</a></h3><time datetime="2026-10-01">1 Oct</time></li>
      <li><a href="/press/b">Short</a></li></ul></body></html>"""
    items = ci.press_items(html, "https://www.harborline.example/press/")
    assert items == [{"title": "Harborline launches FloodFlex in twelve states", "url": "https://www.harborline.example/press/a",
                      "published": "2026-10-01", "snippet": "Harborline launches FloodFlex in twelve states 1 Oct"}]
    xml = ("<urlset><url><loc>https://h.example/newsroom/new</loc><lastmod>2026-10-10</lastmod></url>"
           "<url><loc>https://h.example/newsroom/old</loc><lastmod>2026-01-01</lastmod></url>"
           "<url><loc>https://h.example/products/home</loc><lastmod>2026-10-10</lastmod></url></urlset>")
    assert ci.sitemap_press_urls(xml, REF, 60) == [("https://h.example/newsroom/new", "2026-10-10")]


def test_two_competitors_in_parallel_with_cited_evidence(ctx):
    agent = get_agent("competitive_insights")
    res = agent.run({**agent.selftest_input(), "refresh": True}, ctx)
    assert res.status != "error", res.error
    out = res.output
    assert [c["competitor"] for c in out["competitors"]] == ["Harborline Insurance", "Crestview Assurance"]
    assert out["reference_date"] == "2026-10-18" and not out["is_estimate"]
    for rep in out["competitors"]:
        ids = {e["id"] for e in rep["evidence"]}
        assert len(ids) >= 10
        assert all(e["url"] and e["retrieved_at"] and e["type"] in ("news", "press", "analyst", "community", "campaign") for e in rep["evidence"])
        assert all(e["age_days"] is None or e["age_days"] <= out["events_days"] for e in rep["evidence"])
        for key in ("summary_claims", "marketing_activity", "analyst_commentary", "community_signals"):
            assert all(c["evidence_ids"] and set(c["evidence_ids"]) <= ids for c in rep[key])
        assert all(ev["evidence_id"] in ids and ev["date"] for ev in rep["high_impact_events"])
        assert all(h["type"] != "community" for h in rep["recent_headlines"])
        assert set(rep["next_best_actions"]) == {"P1", "P2", "P3"}
    harbor, crest = out["competitors"]
    assert harbor["domain"] == "harborline.example" and crest["domain"] == "crestview.example"
    assert any("regulator fines" in e["title"] for e in harbor["high_impact_events"])
    assert any("exit wildfire" in e["title"] for e in crest["high_impact_events"])
    assert {a.name for a in res.artifacts} >= {"competitive_insights.md", "competitive_insights.html", "evidence.json"}
    html = (ctx.settings.runs_dir / next(a.path for a in res.artifacts if a.name.endswith(".html"))).read_text()
    assert 'href="#C1-E01"' in html or 'id="C1-E01"' in html


def test_repeat_run_is_served_from_cache():
    agent = get_agent("competitive_insights")
    inp = {**agent.selftest_input(), "focus": "home insurance cache test"}
    first = agent.run({**inp, "refresh": True}, _ctx_with())
    second = agent.run(inp, _ctx_with())
    assert first.metrics["llm_calls"] == 2
    assert second.metrics["llm_calls"] == 0                                           # synthesis cached
    assert second.provenance["evidence_cache_hits"] == 2 and second.provenance["synthesis_cache_hits"] == 2
    assert all(c["cache"] == {"evidence": True, "synthesis": True} for c in second.output["competitors"])
    assert second.output["evidence_count"] == first.output["evidence_count"]


def test_no_search_provider_is_a_flagged_estimate():
    agent = get_agent("competitive_insights")
    res = agent.run({"competitors": ["Harborline Insurance"]}, _ctx_with(search="none"))
    assert res.status == "degraded"
    rep = res.output["competitors"][0]
    assert rep["is_estimate"] and not rep["evidence"] and not rep["recent_headlines"] and not rep["high_impact_events"]
    assert all(c["basis"] == "model_knowledge" and c["is_estimate"] for c in rep["summary_claims"])
    assert any("search 'none'" in r for r in res.provenance["degraded_reasons"])
    assert any("model-knowledge estimate" in w for w in res.warnings)


@pytest.mark.parametrize("url,publisher,expected", [
    ("https://www.fox.com/news/1", "Fox", "news"),                     # 'x.com' is a domain, not a substring
    ("https://midcoastnews.example/1", "Midcoast News", "news"),       # 'idc' must be a whole word
    ("https://x.com/harborline/status/1", "X", "community"),
    ("https://www.reddit.com/r/insurance/1", "Reddit", "community"),
    ("https://www.idc.com/1", "IDC", "analyst"),
    ("https://www.businesswire.com/1", "Business Wire", "press"),
])
def test_source_matching_is_boundary_aware(url, publisher, expected):
    assert ci.classify_evidence({"url": url, "publisher": publisher, "title": "Harborline update"}) == expected
