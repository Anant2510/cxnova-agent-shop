"""Accessibility: axe-core (browser) and static fallback agree on the demo page; WCAG mapping and the CI gate are exact."""

import functools
import xml.etree.ElementTree as ET

import pytest

from caf.agents.web.web_accessibility_evaluation import (
    a11y_score,
    ci_gate,
    contrast_ratio,
    junit_xml,
    normalise_violations,
    parse_color,
    sc_from_tag,
    static_scan,
    wcag_refs,
)
from caf.core import get_agent

HOME = "https://www.meridian-mutual.example/"
EXPECTED = {"image-alt", "label", "color-contrast", "html-has-lang", "link-name", "heading-order"}


@functools.lru_cache(maxsize=1)
def _browser() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            p.chromium.launch(args=["--no-sandbox"]).close()
        return True
    except Exception:
        return False


def test_sc_tag_parsing_and_names(ctx):
    assert sc_from_tag("wcag111") == "1.1.1"
    assert sc_from_tag("wcag1410") == "1.4.10"
    assert sc_from_tag("wcag412") == "4.1.2"
    assert sc_from_tag("wcag2aa") is None and sc_from_tag("best-practice") is None
    criteria = ctx.skill("wcag_criteria")["criteria"]
    assert len(criteria) == 87
    refs = wcag_refs(["cat.color", "wcag2aa", "wcag143"], criteria)
    assert refs == [{"sc": "1.4.3", "name": "Contrast (Minimum)", "level": "AA"}]
    assert wcag_refs(["wcag2a", "wcag412"], criteria)[0]["name"] == "Name, Role, Value"


def test_contrast_maths():
    assert contrast_ratio(parse_color("#000"), parse_color("white")) == pytest.approx(21.0)
    assert contrast_ratio(parse_color("#a3a3a3"), parse_color("#ffffff")) == pytest.approx(2.52, abs=0.01)
    assert parse_color("rgba(0,0,0,0.5)") is None  # translucent: not judged statically


def test_static_fallback_finds_demo_violations(ctx):
    r = get_agent("web_accessibility_evaluation").run({"url": HOME, "engine": "static"}, ctx)
    assert r.status != "error", r.error
    v = {x["id"]: x for x in r.output["violations"]}
    assert set(v) == EXPECTED
    assert v["image-alt"]["nodes_count"] == 2 and v["image-alt"]["wcag"][0]["sc"] == "1.1.1"
    assert "2.52" in v["color-contrast"]["failure_summary"]
    assert v["heading-order"]["best_practice"] and not v["heading-order"]["blocking"]
    assert r.output["engine"].startswith("static")
    assert all(x["fix_snippet"] for x in r.output["violations"])  # offline -> YAML fix text, never placeholders
    assert r.output["remediation_source"] == "rule_library"


def test_ci_gate_and_junit(ctx):
    agent = get_agent("web_accessibility_evaluation")
    strict = agent.run({"url": HOME, "engine": "static", "fail_on": "serious"}, ctx)
    assert strict.output["ci_pass"] is False and strict.output["blocking_count"] == 5
    raw = static_scan("<html lang='en'><head><title>T</title></head><body><main><h1>a</h1><h3>b</h3></main></body></html>", {"wcag2a", "wcag2aa", "best-practice"})
    viol = normalise_violations(raw["violations"], {}, "serious")
    ok, blocking = ci_gate(viol, "serious")
    assert ok and not blocking and [x["id"] for x in viol] == ["heading-order"]
    xml = junit_xml(HOME, "WCAG 2.1 Level AA", "static", [{**x, "explanation": ""} for x in viol], ["document-title"], "serious", "2026-10-18T00:00:00")
    root = ET.fromstring(xml.split("\n", 1)[1])
    assert root.attrib["failures"] == "0" and root.attrib["tests"] == "2"
    xml2 = junit_xml(HOME, "WCAG", "static", [{**x, "blocking": True, "explanation": "fix"} for x in viol], [], "moderate", "t")
    assert ET.fromstring(xml2.split("\n", 1)[1]).find(".//failure") is not None


def test_score_weights():
    assert a11y_score([], 10, {}) == 100.0
    assert a11y_score([{"impact": "critical"}], 10, {"pass_weight": 3, "violation_weights": {"critical": 10}}) == pytest.approx(75.0)


def test_html_input_static(ctx):
    html = "<html><body><img src='a.png'><a href='/x'></a><input type='text'></body></html>"
    r = get_agent("web_accessibility_evaluation").run({"html": html, "engine": "static"}, ctx)
    ids = {v["id"] for v in r.output["violations"]}
    assert {"image-alt", "link-name", "label", "html-has-lang", "document-title"} <= ids


@pytest.mark.browser
def test_axe_core_in_chromium_matches_expectations(ctx):
    if not _browser():
        pytest.skip("Playwright Chromium not available")
    r = get_agent("web_accessibility_evaluation").run({"url": HOME}, ctx)
    assert r.status != "error", r.error
    assert r.output["engine"].startswith("axe-core 4.10.2")
    v = {x["id"]: x for x in r.output["violations"]}
    assert EXPECTED <= set(v)
    assert v["color-contrast"]["wcag"][0] == {"sc": "1.4.3", "name": "Contrast (Minimum)", "level": "AA"}
    assert r.output["passes_count"] > 20 and r.output["ci_pass"] is False
    assert {a.name for a in r.artifacts} >= {"a11y_junit.xml", "a11y_report.html", "axe_results.json"}


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


def test_online_remediation_replaces_library_text_per_rule():
    from caf.core import RunContext

    llm = ScriptedLLM({"RemediationSet": {"items": [
        {"rule_id": "link-name", "explanation": "Screen readers announce just 'link'.", "fix_snippet": '<a class="chat-fab" href="/contact.html" aria-label="Chat with us">'},
        {"rule_id": "unknown-rule", "explanation": "x", "fix_snippet": "y"}]}})
    r = get_agent("web_accessibility_evaluation").run({"url": HOME, "engine": "static"}, RunContext.create(llm=llm))
    v = {x["id"]: x for x in r.output["violations"]}
    assert r.output["remediation_source"] == "model"
    assert 'aria-label="Chat with us"' in v["link-name"]["fix_snippet"]
    assert v["image-alt"]["fix_snippet"].startswith("<img")  # untouched rules keep the YAML fix
    assert {x["id"] for x in r.output["violations"]} == EXPECTED  # the model cannot add or remove violations
