"""Experiment Planning: the statistical core is checked against statsmodels and published group-sequential tables."""

import json
import math

import pytest
import yaml
from scipy.stats import norm
from statsmodels.stats.power import NormalIndPower
from statsmodels.stats.proportion import proportion_effectsize

from caf.agents.experimentation import experiment_planning as ep
from caf.core import get_agent


def test_sample_size_matches_statsmodels_and_textbook_value():
    n = ep.sample_size_per_variant(0.10, 0.10, alpha=0.05, power=0.8)
    h = proportion_effectsize(0.11, 0.10)
    expected = NormalIndPower().solve_power(effect_size=h, alpha=0.05, power=0.8, ratio=1.0, alternative="two-sided")
    assert n == math.ceil(expected) == 14745
    # closed form for two independent arcsine-transformed arms: n = 2 * ((z_a/2 + z_b) / h)^2
    assert abs(n - 2 * ((norm.ppf(0.975) + norm.ppf(0.8)) / h) ** 2) < 2


def test_bonferroni_for_more_than_two_variants():
    n3 = ep.sample_size_per_variant(0.05, 0.10, alpha=0.05, power=0.8, n_variants=3)
    h = abs(proportion_effectsize(0.055, 0.05))
    assert n3 == math.ceil(NormalIndPower().solve_power(effect_size=h, alpha=0.025, power=0.8, ratio=1.0))
    assert n3 > ep.sample_size_per_variant(0.05, 0.10)
    assert ep.bonferroni_alpha(0.05, 4) == pytest.approx(0.05 / 3)


def test_achievable_mde_inverts_the_sample_size():
    n = ep.sample_size_per_variant(0.039, 0.13)
    assert ep.achievable_mde(0.039, n) == pytest.approx(0.13, abs=0.0015)
    assert ep.achievable_mde(0.039, n * 4) < 0.07  # 4x the traffic -> roughly half the MDE


def test_duration_rounds_up_to_whole_weeks_with_a_one_week_minimum():
    d = ep.duration_for_sample(10_000, daily_visitors=1000, traffic_allocation_pct=50, n_variants=2)
    assert d["per_variant_daily"] == 250 and d["days_exact"] == 40 and d["days"] == 42 and d["weeks"] == 6
    assert ep.duration_for_sample(100, 1000)["days"] == 7


@pytest.mark.parametrize("k,constant,inflation", [(1, 1.960, 1.000), (2, 1.977, 1.008), (3, 2.004, 1.017), (4, 2.024, 1.024), (5, 2.040, 1.028)])
def test_obrien_fleming_matches_jennison_turnbull_tables(k, constant, inflation):
    des = ep.obrien_fleming_boundaries(k, alpha=0.05)
    assert des["constant"] == pytest.approx(constant, abs=0.001)
    assert des["cumulative_alpha"][-1] == pytest.approx(0.05, abs=1e-6)
    assert all(a <= b for a, b in zip(des["cumulative_alpha"], des["cumulative_alpha"][1:]))
    assert ep.max_sample_inflation(des["z"], des["info"], 0.05, 0.8) == pytest.approx(inflation, abs=0.001)


def test_sequential_design_schedule_is_in_whole_weeks():
    seq = ep.sequential_design(20_000, per_variant_daily=500, looks=4, alpha=0.05, power=0.8)
    assert seq["available"] and seq["max_sample_size_per_variant"] >= 20_000
    assert all(row["day"] % 7 == 0 for row in seq["schedule"])
    assert seq["schedule"][-1]["information_fraction"] == pytest.approx(1.0)
    assert 0.5 < seq["expected_sample_fraction_h1"] < 1.0


def test_bayesian_design_operating_characteristics_are_monotone():
    b = ep.bayesian_design(0.04, 0.10, per_variant_daily=1500, weeks=6, sims=2000, seed=3)
    h1 = [w["p_ship_if_lift_is_mde"] for w in b["weekly"]]
    h0 = [w["p_ship_if_no_lift"] for w in b["weekly"]]
    assert h1 == sorted(h1) and h0 == sorted(h0)
    assert h1[-1] > 0.5 > h0[-1]
    assert b["prior"]["mean"] == pytest.approx(0.04, abs=1e-3)
    assert ep.bayesian_design(0.04, 0.10, 1500, 6, sims=2000, seed=3) == b  # seeded


def test_allocations_and_hypothesis_helpers():
    assert sum(ep.equal_allocations(3)) == pytest.approx(100.0)
    assert ep.as_if_clause("Change the hero CTA") == "we change the hero CTA"
    assert ep.as_if_clause("the CTA says X") == "the CTA says X"


def _plan(ctx, **over):
    agent = get_agent("experiment_planning")
    res = agent.run({**agent.selftest_input(), "screenshot": False, **over}, ctx)
    assert res.status != "error", res.error
    return res


def test_plan_sizing_exports_and_registry(ctx):
    res = _plan(ctx, design="frequentist", baseline_rate=0.039, daily_visitors=1800, mde=0.13, max_duration_days=42)
    out = res.output
    s = out["sizing"]
    assert s["sample_size_per_variant"] == ep.sample_size_per_variant(0.039, 0.13)
    assert s["duration_days"] % 7 == 0 and s["baseline_source"] == "input"
    assert out["hypothesis"].startswith("If ") and " then " in out["hypothesis"] and " because " in out["hypothesis"]
    assert sum(v["allocation"] for v in out["variants"]) == pytest.approx(100) and sum(v["is_control"] for v in out["variants"]) == 1
    roles = [m["role"] for m in out["metrics"]]
    assert roles.count("primary") == 1 and "guardrail" in roles
    files = {a.name: a for a in res.artifacts}
    opt = json.loads((ctx.settings.runs_dir / files["optimizely_experiment.json"].path).read_text())
    assert sum(v["weight"] for v in opt["variations"]) == 10000
    neutral = yaml.safe_load((ctx.settings.runs_dir / files["experiment.yaml"].path).read_text())
    assert neutral["caf_experiment"]["experiment_id"] == out["experiment_id"]
    registry = json.loads((ctx.settings.runs_dir / "_experiments" / "registry.json").read_text())
    assert any(e["id"] == out["experiment_id"] and e["status"] == "draft" for e in registry["experiments"])
    assert out["definition_upserted"]


def test_plan_defaults_come_from_page_analytics(ctx):
    out = _plan(ctx).output
    s = out["sizing"]
    assert s["baseline_source"].startswith("analytics") and s["daily_visitors_source"].startswith("analytics")
    assert 0.01 < s["baseline_rate"] < 0.1 and not s["is_estimate"]
    assert out["sequential"]["available"] and out["design"] == "sequential"
