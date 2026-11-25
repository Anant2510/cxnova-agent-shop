"""The quality standard the agents are held to (docs/AGENT_QUALITY.md), proved without the web stack.

  - the input contract: files that parse or fail plainly, a minimum below which an agent refuses, input cut by whole records
  - VoyagerAI's propensity as a calculation: the same profile gives the same score, every driver has its evidence, thin data
    is refused, a file of passengers is scored row by row, and calibration on labelled outcomes is validated on a holdout
  - the model writes, the code checks: guardrails, channel lengths, nothing sent, no outcome before it is measured
  - what a result's figures rest on is always said: calculated, authored, judgement, measured or executed (a dry run)
  - the audit (`caf agents audit`) finds no action whose figures nobody classified
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ELENA = json.loads((ROOT / "sample_data" / "voyagerai" / "passenger_elena_chen.json").read_text(encoding="utf-8"))


class Model:
    """A model that answers every call with one canned reply and keeps what it was shown."""

    provider = "fake"
    offline = False
    tracer = None

    def __init__(self, reply: dict):
        self.reply, self.calls = reply, []

    def model_for(self, tier: str) -> str:
        return f"fake-{tier}"

    def describe(self) -> dict:
        return {"provider": "fake", "offline": False, "models": {}}

    def complete(self, prompt: str, *, system: str | None = None, tier: str = "balanced", images=None, meter=None, effort=None) -> str:
        self.calls.append({"prompt": prompt, "system": system or ""})
        return json.dumps(self.reply)


def run(agent_id: str, inputs: dict, *, reply: dict | None = None, upstream: dict | None = None, customer: str | None = None, flow_steps: int = 1):
    from caf.core import RunContext, get_agent

    model = Model(reply) if reply is not None else None
    ctx = RunContext.create(llm=model, options={"customer_id": customer} if customer else None)
    result = get_agent(agent_id).run(inputs, ctx, upstream=upstream, options={"flow_steps": flow_steps})
    return result, model


SCORED = {"score": {"agent_id": "vai_predictive_propensity", "output": {"runner": "code", "result": {"objective": "disruption_sensitivity", "score": 0.69, "decision": "PROACTIVE CARE"}}}}


# ------------------------------------------------------------------------------------------------ the input contract
def test_uploads_parse_into_records_or_fail_plainly():
    from caf.packs.inputs import fit, parse_upload

    rows = parse_upload("passenger_id,tier,nps_last,loyalty_id\nP-1,Gold,7,007\nP-2,Silver,,+4471\n", ["csv", "json"], name="profile")
    assert rows == [{"passenger_id": "P-1", "tier": "Gold", "nps_last": 7, "loyalty_id": "007"}, {"passenger_id": "P-2", "tier": "Silver", "loyalty_id": "+4471"}]   # an empty cell is unknown, not zero: left out
    assert parse_upload('[{"a": 1}, {"a": 2}]', ["json"], name="x") == [{"a": 1}, {"a": 2}]
    assert parse_upload('{"a": 1}\n{"a": 2}\n', ["ndjson"], name="x") == [{"a": 1}, {"a": 2}]
    assert parse_upload({"already": "parsed"}, ["json"], name="x") == {"already": "parsed"}
    with pytest.raises(ValueError, match="x"):
        parse_upload('{"a": 1,', ["json"], name="x")
    # what a model is shown is cut by whole records, and the cut is said
    many = [{"id": i, "note": "n" * 40} for i in range(200)]
    kept, note = fit(many, 2000)
    assert 0 < len(kept) < 200 and kept == many[:len(kept)] and "of 200" in note
    assert fit(many[:3], 2000) == (many[:3], None)


def test_an_agent_refuses_below_its_minimum_and_says_what_to_add(packs):
    nothing, _ = run("cs_xtender_variants", {})
    assert nothing.status == "error" and "was given nothing to work on" in nothing.error
    alone, _ = run("cs_measurement", {"request": "Report on the spring campaign"})
    assert alone.status == "error" and "needs more to do real work" in alone.error and "campaign_id" in alone.error and "results" in alone.error
    assert not alone.error.startswith("ValueError")   # a refusal reads as a sentence, not as an exception
    # with its data it runs; the same agent inside a flow of several steps is not stopped (an earlier step may carry the data)
    reply = {"summary": "Read 1 row.", "result": {"by_persona": []}, "checks": []}
    fed, model = run("cs_measurement", {"request": "Report", "results": [{"persona": "Families", "sends": 1000, "opens": 310}]}, reply=reply)
    assert fed.status == "ok" and fed.provenance["basis"] == "measured" and fed.provenance["evidence"] == ["results"]
    assert "YOU CANNOT MEASURE OR OBSERVE ANYTHING" in model.calls[0]["system"]
    in_flow, _ = run("cs_measurement", {"request": "Report"}, reply=reply, flow_steps=4)
    assert in_flow.status == "degraded" and any(r.startswith("not measured:") for r in in_flow.provenance["degraded_reasons"])
    assert "short of its own minimum" in in_flow.provenance["inputs"]["notes"][0]


# ------------------------------------------------------------------------------------------------ propensity: a calculation
def test_propensity_is_calculated_explained_and_repeatable(packs):
    first, _ = run("vai_predictive_propensity", {"profile": ELENA})
    again, _ = run("vai_predictive_propensity", {"profile": json.dumps(ELENA)})   # a file's text scores the same as the object
    r = first.output["result"]
    assert first.status == "ok" and first.output["runner"] == "code" and first.provenance["basis"] == "calculated"
    assert first.metrics["llm_calls"] == 0 and first.provenance["llm"] == {"provider": "none"}
    assert r["score"] == again.output["result"]["score"] == 0.8854 and r["decision"] == "RETAIN CRITICAL" and r["coverage"] == 1.0
    assert r["model"]["source"] == "default" and "priors" in r["model"]["note"]
    # every driver names its parameter, the value read, where it was read from, and its share; the largest comes first
    assert len(r["drivers"]) == 11 and all(d["source"] and d["effect"] in ("raises", "lowers") for d in r["drivers"])
    assert [abs(d["contribution"]) for d in r["drivers"]] == sorted((abs(d["contribution"]) for d in r["drivers"]), reverse=True)
    assert r["drivers"][0]["parameter"] == "disruptions_28d" and r["drivers"][0]["value"] == 3 and "3 events" in r["drivers"][0]["source"]
    # the score is the scorecard and nothing else: the base rate's log-odds plus the drivers' contributions
    from caf.accelerators.voyagerai import default_model, spec

    z = default_model("churn", spec())["intercept"] + sum(d["contribution"] for d in r["drivers"])
    assert abs(1 / (1 + math.exp(-z)) - r["score"]) < 0.002


def test_propensity_refuses_thin_data_and_scores_a_file_row_by_row(packs):
    thin, _ = run("vai_predictive_propensity", {"profile": {"name": "Elena Chen", "tier": "Platinum Elite", "hub": "SIN"}})
    assert thin.status == "error" and thin.error.startswith("Churn propensity cannot be calculated for Elena Chen")
    assert "at least 3 needed" in thin.error and "disruptions_28d" in thin.error and thin.metrics["llm_calls"] == 0
    prose, _ = run("vai_predictive_propensity", {"profile": "Elena is a Platinum member who seems unhappy lately"})
    assert prose.status == "error"   # a description is not data: nothing is inferred from it
    wrong, _ = run("vai_predictive_propensity", {"profile": ELENA, "objective": "lifetime_value"})
    assert wrong.status == "error"
    # a file of passengers: each is scored or refused on its own, and the refusals are listed with their reason
    batch, _ = run("vai_predictive_propensity", {"sample": "passengers"})
    b = batch.output["result"]
    assert batch.status == "ok" and b["scored"] == 36 and b["not_scored"] == 4 and sum(b["by_decision"].values()) == 36
    assert len(b["not_scored_list"]) == 4 and all("at least 3 needed" in x["why"] for x in b["not_scored_list"])
    assert [a.name for a in batch.artifacts][-1] == "scores.csv"
    # another prediction reads other parameters: the same file does not support it, and `check` says so before anything is scored
    check, _ = run("vai_predictive_propensity", {"action": "check", "sample": "passengers"})
    supports = {s["objective"]: s for s in check.output["result"]["supports"]}
    assert supports["churn"]["can_score"] and supports["churn"]["records_that_can_be_scored"] == 36
    assert not supports["disruption_sensitivity"]["can_score"] and supports["disruption_sensitivity"]["missing_most_important_first"]
    flight, _ = run("vai_predictive_propensity", {"sample": "disruption", "objective": "disruption_sensitivity"})
    assert flight.status == "ok" and flight.output["result"]["scored"] == 14


def test_propensity_says_what_it_can_predict_and_matches_its_configuration(packs):
    from caf.accelerators.voyagerai import spec
    from caf.core import get_agent

    cfg = spec()
    described, _ = run("vai_predictive_propensity", {"action": "describe"})
    predictions = {p["id"]: p for p in described.output["result"]["predictions"]}
    assert list(predictions) == list(cfg["objectives"]) == ["churn", "disruption_sensitivity", "conversion", "upsell"]
    for oid, p in predictions.items():
        assert p["predicts"] and p["window"] and p["parameters"] and p["minimum"] and p["decisions"]
        assert {t["parameter"] for t in p["parameters"]} <= set(cfg["parameters"]) and abs(sum(t["share_of_weight"] for t in p["parameters"]) - 1) < 0.01
    assert described.output["result"]["limits"]   # what it does not do is part of the answer
    page = get_agent("vai_predictive_propensity").describe()
    assert [c["id"] for c in page["capabilities"]] == list(cfg["objectives"]) and page["kind"] == "deterministic"
    assert page["input_schema"]["properties"]["profile"]["x-file"] == ["json", "csv", "ndjson"]
    assert {a["id"]: a["basis"] for a in page["actions"]} == {"score": "calculated", "check": "calculated", "describe": "calculated", "calibrate": "calculated"}
    # every weight sums into a scorecard whose bands are ordered, so a higher score never lands in a lower band
    for obj in cfg["objectives"].values():
        cuts = [band["at"] for band in obj["decisions"]]
        assert cuts == sorted(cuts, reverse=True) and cuts[-1] == 0 and 0 < obj["base_rate"] < 1 and obj["minimum"]["parameters"] >= 3


def test_calibration_fits_a_customers_outcomes_and_stays_that_customers(packs):
    fitted, _ = run("vai_predictive_propensity", {"action": "calibrate", "sample": "churn_outcomes"}, customer="airline-a")
    c = fitted.output["result"]
    assert fitted.status == "ok" and c["usable"] and not c["saved"] and c["rows"]["labelled"] == 2400 and c["rows"]["held_out"] > 400
    assert c["holdout"]["auc"] > c["default_on_same_holdout"]["auc"] > 0.6 and c["holdout"]["brier"] < c["default_on_same_holdout"]["brier"]
    # the sample's outcomes were drawn from known weights (scripts/build_voyagerai_samples.py): the fit finds their direction
    terms = {t["parameter"]: t["weight"] for t in c["model"]["terms"]}
    assert terms["competitor_browsing_28d"] > 1 and terms["disruptions_28d"] > 0.5 and terms["booking_trend"] < -1
    # saved, it scores that customer's later calls; another customer and the operator keep the default
    saved, _ = run("vai_predictive_propensity", {"action": "calibrate", "sample": "churn_outcomes", "save": True}, customer="airline-a")
    assert saved.output["result"]["saved"]
    theirs, _ = run("vai_predictive_propensity", {"profile": ELENA}, customer="airline-a")
    other, _ = run("vai_predictive_propensity", {"profile": ELENA}, customer="airline-b")
    assert theirs.output["result"]["model"]["source"] == "calibrated" and other.output["result"]["model"]["source"] == "default"
    assert theirs.output["result"]["score"] != other.output["result"]["score"] == 0.8854
    back, _ = run("vai_predictive_propensity", {"profile": ELENA, "model": "default"}, customer="airline-a")
    assert back.output["result"]["score"] == 0.8854
    run("vai_predictive_propensity", {"action": "calibrate", "reset": True}, customer="airline-a")
    assert run("vai_predictive_propensity", {"profile": ELENA}, customer="airline-a")[0].output["result"]["model"]["source"] == "default"
    # too few labelled rows: no model, and the reason
    few, _ = run("vai_predictive_propensity", {"action": "calibrate", "outcomes": [{"disruptions_28d": 1, "churned": 1}] * 20})
    assert few.status == "error" and "300" in few.error


# ------------------------------------------------------------------------------------------------ the model writes, the code checks
def test_what_the_model_writes_is_checked_by_arithmetic(packs):
    silver = {"name": "A Traveller", "tier": "Silver"}
    offer = {"summary": "Offer composed.", "result": {"segment": "silver-at-risk", "offer": {"miles": "50,000 bonus miles"}, "cost": {"miles": 50000, "credit_usd": 900},
                                                      "within_guardrails": True, "personalization_match": 0.97}, "checks": []}
    r, _ = run("vai_personalization", {"request": "Compose the offer", "passenger": silver}, reply=offer, upstream=SCORED)
    out = r.output["result"]
    assert out["within_guardrails"] is False and "personalization_match" not in out and out["guardrails_checked"]["offer_miles"] == 50000
    assert [(c.rule, c.status) for c in r.checks] == [("Offer cost against the guardrails", "fail")] and "exceeds the cost guardrail" in r.output["human_followups"][0]
    mine, _ = run("vai_personalization", {"request": "Compose the offer", "passenger": silver, "guardrails": {"max_miles": 60000, "max_credit_usd": 1000}}, reply=offer, upstream=SCORED)
    assert mine.output["result"]["within_guardrails"] is True and mine.output["result"]["guardrails_checked"]["basis"] == "the guardrails given"

    creatives = {"summary": "2 creatives.", "result": {"creatives": [{"channel": "sms", "text": "x" * 190}, {"channel": "email", "text": "Dear traveller"}], "variants": 9}, "checks": []}
    r, _ = run("vai_content_generation", {"request": "Write the creatives", "offer": {"miles": 1000}}, reply=creatives, upstream=SCORED)
    assert r.output["result"]["variants"] == 2 and r.output["result"]["creatives"][0]["chars"] == 190
    assert [(c.rule, c.status) for c in r.checks] == [("Channel length limits", "warn")] and "190 characters against 160" in r.checks[0].message

    sent = {"summary": "Sent on 3 channels.", "result": {"sequence": [{"channel": "email", "at": "T+0"}], "sent": True, "delivered": 3, "open_rate": 0.4}, "checks": []}
    r, model = run("vai_omnichannel_activation", {"request": "Activate", "creatives": [{"channel": "email", "text": "a"}, {"channel": "sms", "text": "b"}]}, reply=sent, upstream=SCORED)
    out = r.output["result"]
    assert r.status == "degraded" and r.output["dry_run"] is True and r.provenance["basis"] == "executed" and "THIS IS A DRY RUN" in model.calls[0]["system"]
    assert out["sent"] is False and out["delivered"] is None and out["open_rate"] is None and out["coverage"] == "1 of 2 creatives sequenced"

    armed = {"summary": "Loop armed.", "result": {"metrics": ["open"], "result": {"metric": "churn propensity", "before": 0.9, "after": 0.31}}, "checks": []}
    r, _ = run("vai_continuous_feedback", {"request": "Arm the loop", "activation": {"sequence": []}}, reply=armed, upstream=SCORED)
    assert r.output["result"]["result"] == {"metric": "disruption sensitivity", "before": 0.69, "after": None,
                                           "basis": "not measured yet: the loop is armed; the value arrives with the journey's events"}
    measured, _ = run("vai_continuous_feedback", {"request": "Arm the loop", "activation": {"sequence": []}, "outcomes": [{"event": "rebooked"}]}, reply=armed, upstream=SCORED)
    assert measured.output["result"]["result"]["after"] == 0.31   # with measured events given, the figure is the caller's


def test_every_result_says_what_its_figures_rest_on(packs):
    ran = {"summary": "Load test passed.", "result": {"p95_ms": 412, "passed": True}, "checks": []}
    r, model = run("arm_factory_perf_test", {"request": "Run a load test on checkout"}, reply=ran)
    assert r.status == "degraded" and r.output["dry_run"] is True and r.provenance["basis"] == "executed"
    assert "THIS IS A DRY RUN" in model.calls[0]["system"] and any("Nothing was run or changed" in x for x in r.provenance["degraded_reasons"])
    judged = {"summary": "Scored.", "result": {"score": 71}, "checks": []}
    r, model = run("cs_foresight_evaluation", {"request": "Evaluate this concept: a spring sale for families"}, reply=judged)
    assert r.status == "ok" and r.provenance["basis"] == "judgement" and "your own assessment" in model.calls[0]["system"]
    assert ("Basis of the figures", "info") in [(c.rule, c.status) for c in r.checks]
    # the four data stages of a VoyagerAI journey are code; nothing stands in for them, offline included
    typed, _ = run("vai_data_ingestion", {"signals": ELENA["history"], "passenger": ELENA})
    assert typed.status == "ok" and typed.output["runner"] == "code" and typed.metrics["llm_calls"] == 0
    empty, _ = run("vai_data_ingestion", {"request": "Ingest the signals"})
    assert empty.status == "error" and "signals" in empty.error


def test_the_audit_finds_no_unclassified_figure_and_every_self_test_meets_its_minimum(packs):
    from caf.core import RunContext, get_agent
    from caf.packs.inputs import missing_minimum
    from caf.packs.model import BASES, load_packs
    from caf.quality import audit, has_numbers, render

    report = audit()
    fails = [f for a in report["agents"] for f in a["findings"] if f["level"] == "fail"]
    assert report["ok"] and not fails, fails
    assert report["totals"]["agents"] == sum(len(p.agents) for p in load_packs().values()) and "PASS" in render(report)
    for pack in load_packs().values():
        for agent in pack.agents:
            for action in agent.actions:
                assert action.grounds in BASES
                if has_numbers(action.example):   # a sample that shows figures: somebody decided what they rest on
                    assert action.basis or action.needs_home or agent.needs_home or agent.code, f"{agent.id}.{action.id}"
                assert action.grounds != "calculated" or agent.code, f"{agent.id}.{action.id}: a model does not calculate"
            assert not missing_minimum(agent, get_agent(agent.id).selftest_input()), agent.id
    # each product's calculating agents run their own self-test to the end with no model at all
    for agent_id in ("vai_data_ingestion", "vai_data_modelling", "vai_identity_stitching", "vai_predictive_propensity", "is_analytics_engine"):
        agent = get_agent(agent_id)
        done = agent.run(agent.selftest_input(), RunContext.create())
        assert done.status == "ok" and done.output["runner"] == "code", agent_id


def test_a_run_on_the_bundled_demo_data_says_so(ctx):
    from caf.core import get_agent

    r = get_agent("webinar_followup_email").run({}, ctx)
    assert r.status != "error" and "webinar:csv" in r.provenance["demo_data"]
    assert any("demo data that ships with this layer" in w for w in r.warnings)
