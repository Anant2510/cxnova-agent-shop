"""The IntelliSight and VoyagerAI packs: IntelliSight's engines compute on the full dataset (checked against an independent
sum), hand the dataset between steps, and refuse to model when the input is wrong; VoyagerAI's eight stages run as one
journey with each stage's output bound into the next; both packs are planner-pickable."""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from caf.accelerators import intellisight as isx

from test_runs import ADMIN, shop_client  # noqa: E402,F401  (the API fixture)

SAMPLE = "sample_data/intellisight/xperion_adobe_analytics_sample.csv"


@pytest.fixture()
def sample(packs):
    from caf.config import get_settings

    path = get_settings().home / SAMPLE
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return path, rows


def test_interpreter_classifies_the_adobe_way(sample):
    path, rows = sample
    ds = isx.parse_csv(path.read_text(encoding="utf-8"), name="xperion", industry="Airlines & Travel")
    assert len(ds.rows) == len(rows) and ds.date_col() == "Date" and ds.years() == ["2023", "2024"]
    assert "eVar4" in ds.evars and "event9" in ds.events and "event9" in ds.key_events and "Hit Type" in ds.dimensions
    assert ds.label("eVar4") == "Marketing Channel" and ds.label("event9") == "Booking Revenue" and ds.column_for("Booking Revenue") == "event9"
    assert ds.best_kpis()[:3] == ["event1", "event2", "event3"] and ds.best_dimension() == "eVar4"
    prof = isx.profile(ds)
    assert prof["rows"] == len(rows) and prof["suggested_dimension"]["label"] == "Marketing Channel" and prof["date_range"]["from"] == "2023-01-01"


def test_engine_sums_every_row_and_uses_intellisight_formulas(sample):
    path, rows = sample
    ds = isx.parse_csv(path.read_text(encoding="utf-8"), name="xperion")
    a = isx.analyse(ds)
    rev = next(k for k in a["kpis"] if k["column"] == "event9")
    expect = sum(float(r["event9"] or 0) for r in rows)
    expect_2024 = sum(float(r["event9"] or 0) for r in rows if r["Date"].startswith("2024"))
    assert rev["total"] == pytest.approx(expect, abs=0.01) and rev["year2_total"] == pytest.approx(expect_2024, abs=0.01)
    cart = sum(float(r["event10"] or 0) for r in rows)
    purch = sum(float(r["event13"] or 0) for r in rows)
    assert a["rates"]["overall"]["cart_abandonment_pct"] == pytest.approx((cart - purch) / cart * 100, abs=0.01)
    assert a["rates"]["overall"]["average_order_value"] == pytest.approx(expect / purch, abs=0.01)
    months = isx.series(ds, "event9", "monthly")
    assert len(months["labels"]) == 24 and months["labels"][0] == "2023-01" and sum(months["values"]) == pytest.approx(expect, abs=0.05)
    by_channel = isx.breakdown(ds, "eVar4", "event9", top=3)
    assert len(by_channel) == 3 and by_channel[0]["event9"] >= by_channel[1]["event9"]
    rep = isx.custom_report(ds, ["Booking Revenue", "Marketing Channel"], filters={"eVar5": "Mobile"})
    assert rep["rows_in_scope"] == sum(1 for r in rows if r["eVar5"] == "Mobile") and [p["kind"] for p in rep["panels"]] == ["series", "breakdown"]
    plan = isx.QueryPlan(metrics=["Booking Revenue"], dimension="Marketing Channel", grain="none", filters={"Date": {"from": "2024-01-01", "to": "2024-12-31"}})
    out = isx.execute_plan(ds, plan)
    assert out["metrics"][0]["total"] == pytest.approx(expect_2024, abs=0.01) and out["chart"]["type"] == "bar" and "by_eVar4" in out


def test_intellisight_agents_run_as_code_hand_the_dataset_on_and_fail_plainly(packs, shop_client):
    v = shop_client.post("/flows/is_executive_review/run", json={"inputs": {"dataset_name": "xperion"}, "wait_s": 120}, headers=ADMIN).json()
    assert v["status"] == "completed" and [s["status"] for s in v["steps"]] == ["ok", "ok", "ok"]
    interp, engine, recs = (s["result"] for s in v["steps"])
    assert interp["output"]["runner"] == "code" and interp["output"]["result"]["dataset_file"].endswith("/dataset.csv")
    assert engine["output"]["runner"] == "code" and engine["output"]["result"]["rows"] == 1800 and "degraded_reasons" not in engine["provenance"]
    assert recs["output"]["result"]["method"] == "offline" and recs["output"]["result"]["based_on"]["rows"] == 1800   # the engine's facts reached it
    assert recs["output"]["runner"] == "code" and any(a["name"] == "recommendations.json" for a in recs["artifacts"])
    q = shop_client.post("/flows/is_question/run", json={"inputs": {"dataset_name": "xperion", "question": "How did Booking Revenue trend by month?"}, "wait_s": 120}, headers=ADMIN).json()
    ans = q["steps"][1]["result"]["output"]["result"]
    assert q["status"] == "completed" and ans["plan"]["grain"] == "monthly" and ans["chart"]["type"] == "line" and "no model" in ans["answer_markdown"]
    bad = shop_client.post("/agents/is_analytics_engine/run", json={"inputs": {"dataset": "not a csv"}}, headers=ADMIN).json()
    assert bad["status"] == "error" and "dataset" in bad["error"]   # wrong input: an error, never a modelled sample
    desc = shop_client.get("/agents/is_analytics_engine", headers=ADMIN).json()
    assert desc["pack"] == "intellisight" and desc["runners"] == ["code", "model", "twin"] and desc["kind"] == "hybrid"


def test_voyagerai_journey_binds_each_stage_into_the_next(packs, shop_client):
    elena = json.loads((Path(__file__).resolve().parents[1] / "sample_data" / "voyagerai" / "passenger_elena_chen.json").read_text(encoding="utf-8"))
    body = {"inputs": {"signals": elena["history"], "passenger": elena}, "wait_s": 120}
    v = shop_client.post("/flows/vai_churn_prevention/run", json=body, headers=ADMIN).json()
    assert v["status"] == "completed" and [s["id"] for s in v["steps"]] == ["ingest", "model", "identity", "score", "offer", "creatives", "activate", "feedback"]
    got = {s["id"]: s["result"]["step_inputs"] for s in v["steps"]}
    assert got["score"]["objective"] == "churn" and got["creatives"]["tone"] == "premium-appreciative"
    assert got["creatives"]["offer"] and got["activate"]["creatives"] and got["feedback"]["activation"]   # the stages' outputs, bound
    done = {s["id"]: s["result"] for s in v["steps"]}
    # the four data stages are code: the score is calculated from the events ingestion typed, never written by a model
    assert all(done[s]["output"]["runner"] == "code" and done[s]["provenance"]["basis"] == "calculated" for s in ("ingest", "model", "identity", "score"))
    assert done["score"]["output"]["result"]["score"] == 0.8854 and done["score"]["output"]["result"]["decision"] == "RETAIN CRITICAL"
    act = done["activate"]
    assert act["output"]["dry_run"] is True   # the channels are not connected: the send is a dry run, said so
    assert any("dry run" in r for r in act["provenance"]["degraded_reasons"])
    # a name and a tier are not data to predict from: the score stage stops the journey and says what to add
    thin = {"inputs": {"signals": [{"source": "PSS / Amadeus", "event": "3 disruptions in 28 days", "severity": "high"}],
                       "passenger": {"name": "Elena Chen", "tier": "Platinum Elite", "hub": "SIN"}}, "wait_s": 120}
    t = shop_client.post("/flows/vai_churn_prevention/run", json=thin, headers=ADMIN).json()
    scored = next(s for s in t["steps"] if s["id"] == "score")["result"]
    assert t["status"] != "completed" and scored["status"] == "error" and "cannot be calculated" in scored["error"]
    packs = {p["id"]: p for p in shop_client.get("/packs", headers=ADMIN).json()}
    assert packs["voyagerai"]["agents"] == 8 and packs["voyagerai"]["flows"] == 4 and packs["voyagerai"]["tag"] == "VAI"


def test_the_routers_reach_both_packs_without_disturbing_the_blueprint(packs, shop_client):
    def route(goal):
        p = shop_client.post("/plan", json={"goal": goal, "assess": False}, headers=ADMIN).json()
        return p["route"].get("playbook"), [s["agent"] for s in p["plan"]["steps"]]

    assert route("Read this Adobe Analytics export and give me recommendations and insights")[0] == "is_executive_review"
    assert route("Retain Elena Chen, a Platinum passenger who browsed Emirates fares after three disruptions")[1][0].startswith("vai_")
    assert route("Audit our website")[0] == "website_audit"
    assert route("Explain the August dip in sessions")[1] == ["ga4_traffic_report"]
