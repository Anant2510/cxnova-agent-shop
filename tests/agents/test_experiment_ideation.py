"""Experiment Ideation: evidence computed in code, heuristics matched deterministically, ICE/PIE scored in code, backlog deduplicated."""

import pytest

from caf.agents.experimentation import experiment_ideation as ei
from caf.core import RunContext, get_agent

URL = ei.DEMO_URL


class FakeLLM:
    offline = False
    tracer = None

    def __init__(self, payloads):
        self.payloads = payloads

    def describe(self):
        return {"provider": "fake", "offline": False, "models": {}}

    def structured(self, prompt, schema, *, system=None, tier="balanced", images=None, retries=1, meter=None):
        if meter is not None:
            meter.add({})
        return schema.model_validate(self.payloads[schema.__name__])

    def complete(self, prompt, **kw):
        return ""


@pytest.fixture()
def facts(ctx):
    return ei.extract_page_facts(ctx.connector("crawler").fetch(URL).html, URL)


def test_page_facts_find_the_demo_page_issues(facts, ctx):
    cfg = ctx.skill("cro_heuristics")
    signals = {e["signal"]: e for e in ei.page_signals(facts, cfg)}
    assert signals["generic_cta_text"]["target"] == "#hero-cta"
    assert signals["non_semantic_controls"]["target"] == "div.plan-tab"
    assert signals["table_without_headers"]["target"] == "#pricing"
    assert signals["images_missing_alt"]["target"] == "img.hero-image"
    assert next(c for c in facts["ctas"] if c["primary"])["selector"] == "#hero-cta"


def test_behaviour_summary_on_heatmap_events(ctx):
    b = ei.behaviour_summary(ctx.connector("behaviour").events(page_url=URL))
    assert b["top_rage_targets"][0]["selector"] == '.plan-tab[data-plan="premium"]'
    assert b["top_dead_targets"][0]["selector"] == "img.hero-image"  # rage-burst clicks are not double-counted as dead clicks
    assert b["false_bottom"]["stop_band"] == [40, 45]
    assert b["devices"]["mobile"] == pytest.approx(0.4)


def test_ice_and_pie_are_computed_from_evidence():
    h = {"typical_impact": 6, "ease": 8}
    scoring = {"ice": "mean", "pie": "mean", "impact": {"severity_bonus": {"high": 1}},
               "confidence": {"base": 3, "per_source": 1.5, "per_item": 0.5, "max_items": 4, "quantitative_bonus": 1, "learning_win_bonus": 1},
               "potential": {"base": 5, "gap_weight": 5, "severity_bonus": {"high": 1.5}}}
    ev = [{"source": "behaviour", "severity": "high"}, {"source": "page", "severity": "medium"}]
    metrics = {"available": True, "conversion_rate": 0.02, "site_conversion_rate": 0.025, "importance": 8.0}
    ice, pie = ei.ice_pie_scores(h, ev, metrics, scoring)
    assert ice == {"impact": 7.0, "confidence": 6.0, "ease": 8.0, "score": 7.0}  # 3 + 1.5 (2nd source) + 0.5 (2nd item) + 1 (quantitative)
    assert pie["potential"] == 7.5 and pie["importance"] == 8.0 and pie["score"] == pytest.approx((7.5 + 8 + 8) / 3, abs=0.01)
    ice2, _ = ei.ice_pie_scores(h, ev, metrics, scoring, learning_effect=1)
    assert ice2["confidence"] == ice["confidence"] + 1


def test_heuristic_evidence_is_about_the_changed_element(ctx):
    res = get_agent("experiment_ideation").run({"url": URL, "max_ideas": 8, "persist": False, "measure_layout": False}, ctx)
    ideas = {i["heuristic_ids"][0]: i for i in res.output["ideas"]}
    dead = ideas["CRO-DEAD-CLICK-AFFORDANCE"]
    assert {e["target"] for e in dead["evidence"]} == {"img.hero-image"}
    rage = ideas["CRO-RAGE-CLICK-BROKEN"]
    assert rage["evidence"][0]["signal"] == "rage_clicks" and rage["ice"]["impact"] == 10
    for i in res.output["ideas"]:
        assert i["hypothesis"].startswith("If ") and ", then " in i["hypothesis"] and ", because " in i["hypothesis"]
    ranked = [i["ice"]["score"] for i in res.output["ideas"]]
    assert ranked == sorted(ranked, reverse=True)


def test_backlog_is_deduplicated_across_runs(ctx):
    agent = get_agent("experiment_ideation")
    inp = {"url": URL, "max_ideas": 4, "measure_layout": False}
    first = agent.run(inp, ctx).output
    second = agent.run(inp, RunContext.create()).output
    assert second["backlog_new"] == 0 and second["backlog_duplicates"] == len(second["ideas"])
    assert second["backlog_size"] == first["backlog_size"]
    rec = ctx.connector("store").get("experiment_backlog", second["ideas"][0]["id"])
    assert rec["seen_count"] >= 2


def test_model_ideas_are_reconciled_against_heuristics_and_evidence():
    payload = {"ideas": [
        {"title": "Tell visitors how fast the quote is", "hypothesis": "If we say 'See my price in 2 minutes', then quote starts rise, because effort is clear.",
         "change": "Rename #hero-cta", "rationale": "Generic label", "heuristic_ids": ["CRO-CTA-CLARITY"], "evidence_ids": ["E1", "E99"],
         "primary_metric": "Quote starts", "target_selector": "#hero-cta"},
        {"title": "Add a chatbot", "hypothesis": "If we add a bot then x because y.", "change": "Add a bot", "rationale": "Trendy",
         "heuristic_ids": ["MADE-UP"], "evidence_ids": ["E404"], "primary_metric": "Chats"},
    ]}
    ctx = RunContext.create(llm=FakeLLM({"IdeasDraft": payload}))
    res = get_agent("experiment_ideation").run({"url": URL, "max_ideas": 12, "persist": False, "measure_layout": False}, ctx)
    out = res.output
    model_ideas = [i for i in out["ideas"] if i["origin"] == "model"]
    assert [i["title"] for i in model_ideas] == ["Tell visitors how fast the quote is"]
    assert [e["id"] for e in model_ideas[0]["evidence"]] == ["E1"]  # unknown evidence id dropped
    assert any("Dropped model idea 'Add a chatbot'" in w for w in res.warnings)
    assert all(i["ice"]["score"] > 0 for i in out["ideas"])  # scores always computed in code


def test_learnings_raise_confidence_for_heuristics_that_shipped(ctx):
    store = ctx.connector("store")
    agent = get_agent("experiment_ideation")
    inp = {"url": URL, "max_ideas": 12, "persist": False, "measure_layout": False}
    base = {i["heuristic_ids"][0]: i["ice"]["confidence"] for i in agent.run(inp, ctx).output["ideas"]}
    store.put("learnings", {"experiment_id": "exp-test-cta", "decision": "ship", "heuristic_ids": ["CRO-CTA-CLARITY"], "page_url": URL},
              key="exp-test-cta")
    after = {i["heuristic_ids"][0]: i["ice"]["confidence"] for i in agent.run(inp, RunContext.create()).output["ideas"]}
    assert after["CRO-CTA-CLARITY"] >= base["CRO-CTA-CLARITY"]
    store.put("learnings", {"experiment_id": "exp-test-cta", "decision": "iterate", "heuristic_ids": [], "page_url": URL}, key="exp-test-cta")


def test_html_input_without_network(ctx):
    html = "<html><body><main><section class='hero'><h1>Pet cover</h1><a class='btn btn-primary' href='/q'>Learn more</a></section>" \
           "<form id='f'>" + "".join(f"<input name='f{i}'>" for i in range(9)) + "</form></main></body></html>"
    res = get_agent("experiment_ideation").run({"html": html, "persist": False, "measure_layout": False}, ctx)
    assert res.status != "error", res.error
    hids = {h for i in res.output["ideas"] for h in i["heuristic_ids"]}
    assert {"CRO-CTA-CLARITY", "CRO-FORM-FRICTION"} <= hids


@pytest.mark.browser
def test_layout_maps_false_bottom_to_the_promo_band(ctx):
    if not ctx.connector("crawler").browser_available():
        pytest.skip("Playwright not installed")
    res = get_agent("experiment_ideation").run({"url": URL, "persist": False}, ctx)
    fb = next(e for e in res.output["evidence"] if e["signal"] == "false_bottom")
    assert fb["target"] == "section.promo-band" and "Insure your home and car together" in fb["detail"]


def test_upstream_findings_are_page_scoped_and_mapped_to_specific_signals(ctx):
    other = {"agent_id": "web_accessibility_evaluation", "output": {"url": "https://www.meridian-mutual.example/", "findings": [
        {"title": "Images must have alternative text", "category": "accessibility", "severity": "critical", "target": "img.card"}]}}
    same = {"agent_id": "heatmap_analysis", "output": {"page_url": URL, "findings": [
        {"title": "Rage clicks on Premium", "category": "behaviour", "severity": "high", "target": '.plan-tab[data-plan="premium"]'},
        {"title": "False bottom at 40-45% scroll depth", "category": "behaviour", "severity": "high", "target": URL}]}}
    found, skipped = ei.page_findings({"a11y": other, "heatmap": same}, URL)
    assert skipped == 1 and [f["source_agent"] for f in found] == ["heatmap_analysis", "heatmap_analysis"]
    sig = {e["signal"]: e for e in ei.upstream_signals(found)}
    assert sig["upstream_rage_clicks"]["params"]["rage_selector"] == '.plan-tab[data-plan="premium"]'
    assert sig["upstream_scroll"]["params"]["false_bottom_depth"] == "40-45"
    # with no behaviour data of its own, ideation still fires the behaviour heuristics from upstream findings
    cfg = ctx.skill("cro_heuristics")
    ev = ei.assign_evidence_ids(ei.upstream_signals(found))
    fired = {m["heuristic"]["id"] for m in ei.match_heuristics(cfg["heuristics"], ev)}
    assert {"CRO-RAGE-CLICK-BROKEN", "CRO-FALSE-BOTTOM"} <= fired
