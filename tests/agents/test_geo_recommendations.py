"""GEO: crawler access, JSON-LD validation, E-E-A-T and citation checks are code; scores are arithmetic, not prose."""

import pytest

from caf.agents.web.geo_recommendations import (
    check_priority,
    citation_hit,
    evaluate_ai_crawlers,
    extract_geo_facts,
    jsonld_entities,
    overall_score,
    parse_llms_txt,
    psi_metrics,
    reconcile_plan,
    resolve_focus,
    score_pillars,
    validate_entity,
)
from caf.core import get_agent

SITE = "https://www.meridian-mutual.example"


@pytest.fixture(scope="module")
def product_audit():
    from caf.core import RunContext

    agent = get_agent("geo_recommendations")
    result = agent.run(agent.selftest_input(), RunContext.create())
    assert result.status != "error", result.error
    return result


def test_ai_crawler_access_from_demo_robots(product_audit):
    bots = {b["agent"]: b["allowed"] for b in product_audit.output["ai_crawlers"]}
    assert bots["GPTBot"] is False and bots["CCBot"] is False
    assert bots["OAI-SearchBot"] is True and bots["PerplexityBot"] is True and bots["ClaudeBot"] is True
    assert len(bots) == 13
    assert product_audit.output["llms_txt"]["present"] is False


def test_scores_are_computed_in_code(product_audit):
    out = product_audit.output
    assessed = [p for p in out["pillars"] if p["assessed"]]
    expected = sum(p["score"] * p["weight"] for p in assessed) / sum(p["weight"] for p in assessed)
    assert out["overall_score"] == pytest.approx(expected, abs=0.05)
    assert all(0 <= p["score"] <= 100 for p in assessed)
    assert out["grade"] in "ABCDF"
    assert out["structured_data"]["parse_errors"] == []
    assert {"FAQPage", "Product"} <= set(out["structured_data"]["types"])


def test_action_plan_covers_failures_offline(product_audit):
    out = product_audit.output
    assert out["plan_source"] == "rule_pack"  # offline: deterministic plan, no placeholder prose
    covered = {i for a in out["action_plan"] for i in a["check_ids"]}
    failing = {c["id"] for c in out["checks"] if c["status"] in ("fail", "warn") and c["severity"] in ("medium", "high", "critical")}
    assert failing <= covered
    assert any("GPTBot" in a["title"] for a in out["action_plan"])
    assert out["citation_test"]["enabled"] is False and out["citation_test"]["experimental"] is True


def test_article_eeat_signals(ctx):
    r = get_agent("geo_recommendations").run({"page_url": f"{SITE}/blog/claims-tips.html", "focus_section": "E-E-A-T"}, ctx)
    checks = {c["id"]: c for c in r.output["checks"]}
    assert r.output["page_type"] == "article" and r.output["focus_pillar"] == "eeat"
    for cid in ("eeat.author_byline", "eeat.author_bio", "eeat.dates", "eeat.citations"):
        assert checks[cid]["status"] == "pass", (cid, checks[cid]["message"])
    assert checks["citation.faq_schema"]["status"] == "warn"  # Q&A content without FAQPage markup


def test_section_focus_and_html_only(ctx):
    agent = get_agent("geo_recommendations")
    r = agent.run({"page_url": f"{SITE}/products/home-insurance.html", "focus_section": "#faq"}, ctx)
    assert r.output["focus_found"] is True and r.output["focus_pillar"] is None
    html = open("sample_data/site/products/car-insurance.html", encoding="utf-8").read()
    r2 = agent.run({"html": html}, ctx)
    assert r2.status != "error"
    assert "html input: crawler-access and performance fetches skipped" in r2.provenance["degraded_reasons"]
    assert {c["id"]: c["status"] for c in r2.output["checks"]}["schema.present"] == "fail"


def test_jsonld_entities_and_required_properties():
    data = {"@context": "https://schema.org", "@graph": [{"@type": "Article", "headline": "x", "author": {"@type": "Person", "name": "A"}},
                                                         {"@type": "Organization", "name": "Org"}]}
    ents = jsonld_entities(data, "block 1")
    types = {e["type"]: e for e in ents}
    assert {"Article", "Person", "Organization"} <= set(types)
    spec = {"Article": {"required": ["headline", "author", "datePublished"], "recommended": ["image"]},
            "Organization": {"required": ["name", "url"], "recommended": []}, "Person": {"required": ["name"], "recommended": ["sameAs"]}}
    assert validate_entity(types["Article"], spec)["missing_required"] == ["datePublished"]
    assert validate_entity(types["Organization"], spec)["missing_required"] == ["url"]  # top-level in @graph: strict
    assert validate_entity(types["Person"], spec)["missing_required"] == []


def test_robots_and_llms_parsing():
    bots = [{"agent": "GPTBot"}, {"agent": "PerplexityBot"}]
    res = {b["agent"]: b["allowed"] for b in evaluate_ai_crawlers("User-agent: GPTBot\nDisallow: /\n", "https://x.test/p", bots)}
    assert res == {"GPTBot": False, "PerplexityBot": True}
    assert evaluate_ai_crawlers(None, "https://x.test/", bots)[0]["allowed"] is None
    assert parse_llms_txt("# Site\n\n> summary\n\n## Docs\n- [A](https://x/a)\n") == {"present": True, "valid": True, "sections": 1, "links": 1}


def test_statistics_and_questions_extraction():
    html = ("<main><h1>Guide</h1><p>Short summary of the guide for readers who want the answer fast.</p>"
            "<h2>How long does a claim take?</h2><p>Most claims settle in 10 days according to the 2026 benchmark.</p>"
            "<p>Costs rose 19% in three years.</p></main>")
    f = extract_geo_facts("https://x.test/guide", html)
    assert f["questions"][0]["question"] == "How long does a claim take?" and f["questions"][0]["answer_words"] >= 8
    stats = {s["text"]: s for s in f["stats"]}
    assert stats["Costs rose 19% in three years."]["sourced"] is False
    assert any(s["sourced"] for s in f["stats"])


def test_focus_resolution():
    pillars = {"structured_data": {"label": "Structured data"}, "eeat": {"label": "E-E-A-T signals"}, "performance": {"label": "Performance"}}
    assert resolve_focus("schema", pillars) == "structured_data"
    assert resolve_focus("E-E-A-T", pillars) == "eeat"
    assert resolve_focus("#faq", pillars) is None


def test_plan_reconciliation_grounds_model_actions():
    pack = {"pillars": {"crawler_access": {"weight": 0.2}, "structured_data": {"weight": 0.2}}, "severity_weights": {"high": 6, "medium": 3, "low": 1},
            "priority_thresholds": {"P1": 9, "P2": 4}}
    checks = [{"id": "schema.present", "rule": "schema.present", "pillar": "structured_data", "status": "fail", "severity": "high", "title": "t",
               "why": "w", "recommendation": "r", "action": "Add JSON-LD"},
              {"id": "crawler.llms_txt", "rule": "crawler.llms_txt", "pillar": "crawler_access", "status": "warn", "severity": "low", "title": "t2",
               "why": "w", "recommendation": "r", "action": "Publish llms.txt"}]
    model = [{"title": "Invented", "pillar": "eeat", "priority": "P3", "check_ids": ["made.up"]},
             {"title": "Add schema", "pillar": "structured_data", "priority": "P3", "check_ids": ["schema.present"]}]
    plan = reconcile_plan(model, checks, pack)
    assert [a["title"] for a in plan if a["source"] == "model"] == ["Add schema"]
    assert plan[0]["priority"] == check_priority(checks[0], pack)[0] == "P1"  # never under-prioritised


def test_pillar_scoring_and_psi_parsing():
    checks = [{"pillar": "eeat", "status": "pass", "weight": 2.0}, {"pillar": "eeat", "status": "fail", "weight": 2.0},
              {"pillar": "eeat", "status": "na", "weight": 5.0}]
    pillars = score_pillars(checks, {"eeat": {"label": "E", "weight": 1.0}, "performance": {"label": "P", "weight": 1.0}})
    assert pillars[0]["score"] == 50.0 and pillars[1]["assessed"] is False
    assert overall_score(pillars) == 50.0
    payload = {"lighthouseResult": {"categories": {"performance": {"score": 0.91}}, "audits": {"largest-contentful-paint": {"numericValue": 2100},
               "cumulative-layout-shift": {"numericValue": 0.02}, "total-blocking-time": {"numericValue": 150}}}}
    m = psi_metrics(payload, "mobile")
    assert m["lcp_ms"] == 2100 and m["performance_score"] == 91 and m["field"] is False


def test_citation_hit_detection():
    ans = {"answer": "Meridian Mutual offers flood add-ons.", "sources": ["https://meridian-mutual.example/products"], "brands": []}
    assert citation_hit(ans, f"{SITE}/products/home-insurance.html", "Meridian Mutual") == (True, True)
    assert citation_hit({"answer": "Use any insurer", "sources": []}, f"{SITE}/", "Meridian Mutual") == (False, False)


def test_unused_templates_render(ctx):
    actx = ctx.for_agent("geo_recommendations")
    _, user = actx.render("questions", page_url=f"{SITE}/", title="t", headings=["a", "b"], count=3, page_questions=["q?"])
    assert "3 distinct questions" in user and "- q?" in user
    _, user = actx.render("answer", question="Does home insurance cover floods?")
    assert "Does home insurance cover floods?" in user


class ScriptedLLM:
    """Stand-in for a real provider so the online code paths (plan reconciliation, citation test) run in CI."""

    offline = False

    def __init__(self, responses):
        self.responses, self.tracer, self.calls = responses, None, []

    def describe(self):
        return {"provider": "scripted", "offline": False, "models": {}}

    def structured(self, prompt, schema, *, system=None, tier="balanced", images=None, meter=None, retries=1):
        self.calls.append((schema.__name__, tier, prompt))
        if meter is not None:
            meter.add({})
        r = self.responses[schema.__name__]
        return schema.model_validate(r(prompt) if callable(r) else r)

    def complete(self, prompt, **kw):
        return ""


def test_online_plan_and_experimental_citation_test():
    from caf.core import RunContext

    import itertools

    answers = itertools.cycle([{"answer": "Meridian Mutual covers burst pipes.", "sources": ["https://www.meridian-mutual.example/products/home-insurance.html"]},
                               {"answer": "Most insurers exclude flood.", "sources": ["https://insurer-guide.example"], "brands": ["Harborline"]}])
    llm = ScriptedLLM({
        "CitationQuestions": {"questions": ["Is a burst pipe covered by home insurance?", "Does home insurance cover floods?"]},
        "CitationAnswer": lambda p: next(answers),
        "GeoPlan": {"executive_summary": "Model summary.", "actions": [
            {"title": "Publish llms.txt", "pillar": "crawler_access", "priority": "P3", "check_ids": ["crawler.llms_txt"], "steps": ["Write it"]},
            {"title": "Hallucinated action", "pillar": "eeat", "priority": "P1", "check_ids": ["eeat.made_up"]}]},
    })
    r = get_agent("geo_recommendations").run({"page_url": f"{SITE}/products/home-insurance.html", "citation_test": True, "citation_questions": 2,
                                              "citation_tiers": ["fast", "pro"]}, RunContext.create(llm=llm))
    assert r.status != "error", r.error
    ct = r.output["citation_test"]
    assert ct["ran"] and ct["experimental"] and len(ct["results"]) == 4  # 2 questions x 2 model tiers
    assert {x["tier"] for x in ct["results"]} == {"fast", "pro"}
    assert ct["results"][0]["cited_url"] is True and ct["results"][0]["brand_mentioned"] is True
    assert ct["cited_rate"] == 0.5 and ct["brand_mention_rate"] == 0.5
    assert [c[1] for c in llm.calls if c[0] == "CitationAnswer"] == ["fast", "fast", "pro", "pro"]
    titles = [a["title"] for a in r.output["action_plan"]]
    assert "Publish llms.txt" in titles and "Hallucinated action" not in titles
    assert r.output["executive_summary"] == "Model summary." and r.output["plan_source"] == "model"
    # failing medium+ checks the model forgot are still covered by rule-pack actions
    assert any(a["source"] == "rule_pack" for a in r.output["action_plan"])
