"""Experiment Review: the deterministic gate fails the broken experiment, detects SRM and emits CI-ready JUnit XML."""

import xml.etree.ElementTree as ET

import pandas as pd
import pytest
from scipy.stats import chisquare

from caf.agents.experimentation import experiment_review as er
from caf.connectors.experiments import ExperimentDefinition
from caf.core import get_agent


def _review(ctx, **inp):
    res = get_agent("experiment_review").run(inp, ctx)
    assert res.status != "error", res.error
    return res


def test_broken_experiment_fails_the_gate_with_many_findings(ctx):
    res = _review(ctx, experiment_id="exp-mobile-sticky")
    out = res.output
    assert out["gate"] == "fail"
    failed = {r["rule_id"] for r in out["results"] if r["status"] == "fail"}
    assert {"allocation.sum_100", "metric.guardrails_present", "stats.sample_size_sufficient", "duration.min_days", "duration.whole_weeks",
            "targeting.overlap", "instrumentation.primary_event", "qa.links_present"} <= failed
    assert out["blocking_failures"] >= 8 and len(out["findings"]) >= 8
    overlap = next(r for r in out["results"] if r["rule_id"] == "targeting.overlap")
    assert overlap["data"]["overlaps"][0]["id"] == "exp-pricing-table"
    ss = out["sample_size"]
    assert ss["required_per_variant"] > 100 * ss["available_smallest_arm"]


def test_pricing_table_interim_srm_is_detected(ctx):
    out = _review(ctx, experiment_id="exp-pricing-table").output
    srm = next(r for r in out["results"] if r["rule_id"] == "srm.interim")
    assert srm["status"] == "fail" and out["gate"] == "fail"
    assert out["srm"]["p_value"] < 1e-6
    assert out["srm"]["worst_segment"]["segment"] == "mobile"
    expected = chisquare([10403, 9603], f_exp=[10003, 10003])
    assert out["srm"]["chi2"] == pytest.approx(expected.statistic, rel=1e-6)


def test_well_configured_experiment_passes(ctx):
    out = _review(ctx, experiment_id="exp-hero-cta").output
    assert out["gate"] == "pass" and out["blocking_failures"] == 0


def test_junit_xml_is_ci_ready(ctx):
    res = _review(ctx, experiment_id="exp-mobile-sticky")
    path = ctx.settings.runs_dir / res.output["junit_artifact"]
    root = ET.parse(path).getroot()
    suite = root.find("testsuite")
    cases = suite.findall("testcase")
    assert len(cases) == len(res.output["results"])
    assert int(suite.get("failures")) == res.output["blocking_failures"] == len(suite.findall("testcase/failure"))
    assert suite.find("properties/property[@name='gate']").get("value") == "fail"


def test_definition_dict_input_and_rule_coverage(ctx):
    good = get_agent("experiment_review")
    defn = ctx.connector("experiments").get("exp-hero-cta").model_dump()
    defn.update(id="exp-hero-cta-copy", status="draft", hypothesis="Change the CTA copy.")  # break only the hypothesis
    out = _review(ctx, definition=defn).output
    failed = {r["rule_id"] for r in out["results"] if r["status"] == "fail"}
    assert failed == {"hypothesis.format"}
    cfg = ctx.skill("experiment_review_rules")
    assert {r["id"] for r in cfg["rules"]} <= set(er.RULES), "every YAML rule has an implementation"
    assert good.spec.side_effects == []


def test_sample_size_check_uses_smallest_arm_and_bonferroni():
    d = ExperimentDefinition(id="x", name="x", baseline_rate=0.05, mde=0.10, expected_daily_visitors=10_000, planned_duration_days=28,
                             traffic_allocation=50, variants=[{"id": "control", "name": "c", "allocation": 50, "is_control": True},
                                                               {"id": "a", "name": "a", "allocation": 25}, {"id": "b", "name": "b", "allocation": 25}])
    s = er.sample_size_check(d, 0.05, 0.8)
    assert s["smallest_arm_share"] == 0.25 and s["available_smallest_arm"] == int(10_000 * 0.5 * 0.25 * 28)
    assert s["alpha_per_comparison"] == pytest.approx(0.025)


def test_srm_test_and_overlap_helpers():
    t = er.srm_test({"control": 5000, "treatment": 5000}, {"control": 50, "treatment": 50})
    assert t["p_value"] == pytest.approx(1.0)
    a = ExperimentDefinition(id="a", name="a", status="draft", page_url="https://x.example/p", audience={"devices": ["mobile"]})
    b = ExperimentDefinition(id="b", name="b", status="running", page_url="https://x.example/p/", audience={"devices": ["desktop"]})
    c = ExperimentDefinition(id="c", name="c", status="running", page_url="https://x.example/p", audience={"devices": []})
    hits = er.find_overlaps(a, [b, c], ["running"])
    assert [h["id"] for h in hits] == ["c"]
    assert er.results_by_variant(pd.DataFrame({"variant": ["a", "a"], "visitors": [1, 2]})) == {"a": 3.0}
