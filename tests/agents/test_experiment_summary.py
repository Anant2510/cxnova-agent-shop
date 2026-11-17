"""Experiment Summary: statistics computed in code, decisions from YAML rules, narrative guarded against invented numbers."""

import pandas as pd
import pytest
from statsmodels.stats.proportion import proportions_ztest

from caf.agents.experimentation import experiment_summary as es
from caf.core import RunContext, get_agent


class FakeLLM:
    """Minimal stand-in for a real provider: returns fixed structured payloads per schema name."""

    offline = False
    tracer = None

    def __init__(self, payloads):
        self.payloads = payloads

    def describe(self):
        return {"provider": "fake", "offline": False, "models": {}}

    def structured(self, prompt, schema, *, system=None, tier="balanced", images=None, retries=1, meter=None):
        if meter is not None:
            meter.add({})
        p = self.payloads[schema.__name__]
        return schema.model_validate(p(prompt) if callable(p) else p)

    def complete(self, prompt, *, system=None, tier="balanced", images=None, meter=None):
        return ""


def _summary(ctx, **inp):
    res = get_agent("experiment_summary").run(inp, ctx)
    assert res.status != "error", res.error
    return res


def test_hero_cta_statistics_match_statsmodels_and_ship(ctx):
    out = _summary(ctx, experiment_id="exp-hero-cta").output
    c = out["comparisons"][0]
    z, p = proportions_ztest([1043, 930], [23900, 23870])
    assert c["p_value"] == pytest.approx(p) and c["z"] == pytest.approx(z)
    assert c["rel_lift"] == pytest.approx((1043 / 23900) / (930 / 23870) - 1)
    assert c["ci_rel"][0] > 0 and c["ci_abs"][0] > 0
    assert c["prob_beat_control"] == pytest.approx(0.995, abs=0.003)
    assert out["decision"]["decision"] == "ship" and out["decision"]["rule_id"] == "clear_win"
    assert not out["srm"]["detected"] and out["simpsons"]["available"] and not out["simpsons"]["paradox"]
    assert {s["segment"] for s in out["segments"]} == {"desktop", "mobile"}
    assert out["narrative_source"] == "deterministic"


def test_pricing_table_srm_blocks_the_decision(ctx):
    out = _summary(ctx, experiment_id="exp-pricing-table").output
    assert out["srm"]["detected"] and out["srm"]["p_value"] < 1e-6
    assert out["decision"]["decision"] == "iterate" and out["decision"]["rule_id"] == "invalid_srm"
    assert out["simpsons"]["segment_mix_p"] < 1e-6  # mobile visitors are missing from the treatment


def test_simpsons_paradox_is_detected():
    # Kidney-stone data: the variant wins in both segments but loses overall because of the segment mix.
    df = es.normalise_results(pd.DataFrame([
        {"variant": "control", "segment": "small", "visitors": 270, "conversions": 234},
        {"variant": "control", "segment": "large", "visitors": 80, "conversions": 55},
        {"variant": "variant", "segment": "small", "visitors": 87, "conversions": 81},
        {"variant": "variant", "segment": "large", "visitors": 263, "conversions": 192},
    ]))
    overall = (273 / 350) - (289 / 350)
    sp = es.simpsons_check(df, "control", "variant", overall)
    assert sp["paradox"] and set(sp["segment_signs"].values()) == {1}
    assert sp["mix_standardised_rel_lift"] > 0 > overall


def test_bayesian_monte_carlo_is_seeded_and_consistent():
    a = es.bayesian_compare(120, 1000, 100, 1000, draws=100_000, seed=1)
    b = es.bayesian_compare(120, 1000, 100, 1000, draws=100_000, seed=1)
    assert a == b
    assert 0.9 < a["prob_beat_control"] < 0.95  # z ~ 1.43 -> about 0.92
    assert a["expected_loss_abs"] < a["expected_loss_keep_control_abs"]


def test_decision_rules_and_number_guard():
    rules = [{"id": "win", "decision": "ship", "when": [{"fact": "lift_rel", "op": ">", "value": "mde"}]},
             {"id": "else", "decision": "iterate", "when": []}]
    assert es.decide({"lift_rel": 0.2, "mde": 0.1}, rules)["rule_id"] == "win"
    assert es.decide({"lift_rel": 0.05, "mde": 0.1}, rules)["rule_id"] == "else"
    assert es.decide({"lift_rel": None, "mde": 0.1}, rules)["rule_id"] == "else"
    facts = [0.12009, 0.0102, 23900.0, 0.995]
    assert es.unsupported_numbers("Lift of +12.0% (p = 0.0102) on 23,900 visitors, P(beat) 99.5%.", facts, set()) == []
    assert es.unsupported_numbers("Lift of 18% expected to add $2.4M.", facts, set()) == ["18%", "2.4"]


def test_model_narrative_with_invented_numbers_is_rejected(tmp_path):
    bad = {"headline": "The variant lifted quotes by 31% and will add 4,200 policies a year.", "what_happened": "x", "what_it_means": "y",
           "next_steps": ["Ship it."], "caveats": []}
    ctx = RunContext.create(llm=FakeLLM({"Narrative": bad}))
    res = _summary(ctx, experiment_id="exp-hero-cta")
    assert res.output["narrative_source"] == "deterministic"
    assert any("not computed" in w for w in res.warnings)


def test_model_narrative_using_computed_numbers_is_kept():
    good = {"headline": "The new hero copy lifted quote starts by 12.0% (p = 0.0102); ship it.",
            "what_happened": "23,900 visitors saw the treatment and 23,870 the control; 4.36% versus 3.90%.",
            "what_it_means": "A real improvement.", "next_steps": ["Ship to all traffic."], "caveats": []}
    ctx = RunContext.create(llm=FakeLLM({"Narrative": good}))
    res = _summary(ctx, experiment_id="exp-hero-cta")
    assert res.output["narrative_source"] == "model"
    assert res.output["narrative"]["headline"] == good["headline"]


def test_summary_is_persisted_to_learnings_with_heuristic_tags(ctx):
    out = _summary(ctx, experiment_id="exp-hero-cta").output
    rec = ctx.connector("store").get("learnings", "exp-hero-cta")
    assert rec and rec["decision"] == "ship" and "CRO-CTA-CLARITY" in rec["heuristic_ids"]
    assert out["learning_id"] == "exp-hero-cta"


def test_inline_csv_results_without_registry(ctx):
    csv = "variant,visitors,conversions\ncontrol,5000,200\nb,5000,260\nc,5000,205\n"
    out = _summary(ctx, results_csv=csv, persist=False).output
    assert out["control"] == "control" and out["best_variant"] == "b"
    assert out["comparisons"][0]["alpha"] == pytest.approx(0.025)  # Bonferroni over two treatments
    assert set(out["prob_best"]) == {"control", "b", "c"}
