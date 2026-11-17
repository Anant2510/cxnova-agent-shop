"""Technical SEO: the deterministic rules engine finds the demo site's known issues and nothing silly."""

from urllib.parse import urlsplit

import pytest

from caf.agents.web.technical_seo_recommendations import (
    build_site,
    chk_hreflang_reciprocity,
    evaluate_rules,
    extract_page_facts,
    heading_skips,
    norm_url,
    page_importance,
    parse_jsonld,
    parse_sitemap,
    prioritise,
)
from caf.core import get_agent

SITE = "https://www.meridian-mutual.example"


@pytest.fixture(scope="module")
def audit():
    from caf.core import RunContext

    agent = get_agent("technical_seo_recommendations")
    result = agent.run(agent.selftest_input(), RunContext.create())
    assert result.status != "error", result.error
    return result


def _issues(result):
    return {(i["rule_id"], urlsplit(i["url"]).path + (f"?{urlsplit(i['url']).query}" if urlsplit(i["url"]).query else "")): i
            for i in result.output["issues"]}


def test_finds_every_known_demo_issue(audit):
    found = _issues(audit)
    expected = [
        ("meta_description.missing", "/"),
        ("h1.multiple", "/"),
        ("lang.missing", "/"),
        ("title.duplicate", "/products/car-insurance.html"),
        ("canonical.missing", "/products/car-insurance.html"),
        ("status.redirect", "/old-page.html"),
        ("status.client_error", "/promo"),
        ("sitemap.non_200", "/promo"),
        ("sitemap.non_200", "/old-page.html"),
        ("links.broken_internal", "/"),
        ("links.to_redirect", "/"),
        ("hreflang.target_error", "/"),
        ("status.client_error", "/fr/"),
        ("headings.skip", "/blog/claims-tips.html"),
    ]
    missing = [e for e in expected if e not in found]
    assert not missing, f"missing issues: {missing}"
    assert found[("status.redirect", "/old-page.html")]["detail"].startswith("301")
    assert "/promo (404)" in found[("links.broken_internal", "/")]["detail"]
    assert "/fr/ returns 404" in found[("hreflang.target_error", "/")]["detail"]
    assert "H2 -> H4" in found[("headings.skip", "/blog/claims-tips.html")]["detail"]
    assert found[("h1.multiple", "/")]["detail"] == "2 H1 elements"


def test_no_false_positives_on_clean_pages(audit):
    found = _issues(audit)
    # parameter variants consolidated by a canonical (/quote?product=pet -> /quote) are not duplicates
    assert not [k for k in found if k[0] == "title.duplicate" and "?" in k[1]]
    # pages with a self canonical are not flagged as missing one
    assert ("canonical.missing", "/products/home-insurance.html") not in found
    assert ("lang.missing", "/products/home-insurance.html") not in found
    assert not [k for k in found if k[0] == "jsonld.parse_error"]


def test_contract_artifacts_and_findings(audit):
    out = audit.output
    names = {a.name for a in audit.artifacts}
    assert {"seo_issues.csv", "seo_audit.html"} <= names
    assert out["findings"] and all(f["category"] == "seo" for f in out["findings"])
    assert out["robots"]["found"] and "GPTBot" in out["robots"]["blocked_user_agents"]
    assert out["sitemap"]["url_count"] == 9
    assert set(out["fix_order"]) == {g["rule_id"] for g in out["groups"]}
    assert out["priority_counts"]["P1"] >= 1


def test_traffic_data_drives_priority(audit):
    out = audit.output
    if out["importance_source"] != "search_console":
        pytest.skip("keyword export not available in this checkout")
    assert out["traffic_data_used"] is True
    # an issue on the homepage (most clicks) outranks the same severity on a low-traffic page
    by = {(i["rule_id"], i["url"]): i for i in out["issues"]}
    assert by[("headings.skip", f"{SITE}/")]["priority_score"] > by[("headings.skip", f"{SITE}/blog/claims-tips.html")]["priority_score"]


def test_pure_helpers():
    assert norm_url("HTTPS://WWW.Example.com:443/a#frag") == "https://www.example.com/a"
    assert norm_url("https://example.com") == "https://example.com/"
    assert heading_skips([(1, "a"), (2, "b"), (4, "c"), (2, "d")]) == [{"from": 2, "to": 4, "text": "c"}]
    data, err = parse_jsonld('{"@type": "Thing",}')
    assert data is None and "line 1" in err
    assert parse_sitemap("<html>")["kind"] == "invalid"
    assert parse_sitemap('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://a/</loc></url></urlset>')["urls"] == ["https://a/"]


def test_hreflang_reciprocity_on_synthetic_pages():
    a = extract_page_facts("https://x.test/en/", '<html lang="en"><head><link rel="alternate" hreflang="de" href="https://x.test/de/"></head></html>')
    b = extract_page_facts("https://x.test/de/", '<html lang="de"><head></head></html>')
    site = build_site("https://x.test/en/", {}, {a["url"]: a, b["url"]: b}, {"found": False}, {"found": False, "urls": []})
    issues = chk_hreflang_reciprocity(site, {})
    assert issues and "no return tag" in issues[0]["detail"]


def test_html_input_runs_page_rules_only(ctx):
    html = open("sample_data/site/products/car-insurance.html", encoding="utf-8").read()
    result = get_agent("technical_seo_recommendations").run({"html": html, "page_url": f"{SITE}/products/car-insurance.html"}, ctx)
    assert result.status != "error", result.error
    rules = {i["rule_id"] for i in result.output["issues"]}
    assert "canonical.missing" in rules and "meta_description.length" in rules
    assert result.output["mode"] == "html" and result.output["rules_skipped"] > 0
    assert "html input: site-level checks skipped" in result.provenance.get("degraded_reasons", [])


def test_prioritise_uses_importance():
    site = {"inlinks": {}, "start_url": None}
    imp, src = page_importance(site, {"https://a/": {"clicks": 1000, "impressions": 0}, "https://a/b": {"clicks": 1, "impressions": 0}})
    assert src == "search_console" and imp["https://a/"] == 1.0
    cfg = {"severity_weights": {"high": 6}, "traffic_weight": 1.5, "priority_thresholds": {"P1": 9, "P2": 4}}
    out = prioritise([{"rule_id": "r", "severity": "high", "url": "https://a/"}, {"rule_id": "r", "severity": "high", "url": "https://a/b"}], imp, cfg, set())
    assert out[0]["priority"] == "P1" and out[1]["priority"] == "P2"


def test_rule_subset_and_unknown_check():
    facts = extract_page_facts("https://x.test/", "<html><head><title>t</title></head><body><h1>a</h1><h1>b</h1></body></html>")
    site = build_site("https://x.test/", {}, {facts["url"]: facts}, {"found": False}, {"found": False, "urls": []}, mode="html")
    issues, results = evaluate_rules(site, [{"id": "h1.multiple", "title": "t", "check": "count_above", "params": {"field": "h1", "max": 1}, "scope": "page"},
                                            {"id": "bogus", "title": "b", "check": "no_such_check"}])
    assert [i["rule_id"] for i in issues] == ["h1.multiple"]
    assert {r["status"] for r in results} == {"ran", "error"}


class ScriptedLLM:
    offline = False

    def __init__(self, responses):
        self.responses, self.tracer = responses, None

    def describe(self):
        return {"provider": "scripted", "offline": False, "models": {}}

    def structured(self, prompt, schema, *, system=None, tier="balanced", images=None, meter=None, retries=1):
        if meter is not None:
            meter.add({})
        return schema.model_validate(self.responses[schema.__name__])

    def complete(self, prompt, **kw):
        return ""


def test_online_explanations_are_merged_by_rule_id():
    from caf.core import RunContext

    llm = ScriptedLLM({"SeoExplanation": {
        "executive_summary": "Two broken URLs hurt crawl efficiency.",
        "explanations": [{"rule_id": "status.client_error", "why_it_matters": "Visitors hit dead ends.", "fix_steps": ["Redirect /promo to /offers"],
                          "effort": "low", "owner_role": "Web developer"},
                         {"rule_id": "not.a.rule", "why_it_matters": "ignored"}],
        "fix_order": ["lang.missing", "status.client_error", "bogus"]}})
    agent = get_agent("technical_seo_recommendations")
    r = agent.run(agent.selftest_input(), RunContext.create(llm=llm))
    out = r.output
    g = {x["rule_id"]: x for x in out["groups"]}
    assert out["explanation_source"] == "model" and out["executive_summary"].startswith("Two broken URLs")
    assert g["status.client_error"]["fix_steps"] == ["Redirect /promo to /offers"]
    assert out["fix_order"][:2] == ["lang.missing", "status.client_error"] and "bogus" not in out["fix_order"]
    assert set(out["fix_order"]) == set(g)  # every finding still present, none invented
    assert {i["rule_id"] for i in out["issues"]} == set(g)
