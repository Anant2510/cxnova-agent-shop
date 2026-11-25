"""The DocuInsights pack on the layer: its agents are full pack agents whose work is the accelerator's own code; the
fourteen-step flow carries one case from intake to a verified, triaged recommendation, each step reading what the one before left;
wrong input fails plainly; a customer's cases are its own; and the workbench is mounted under the layer's own sign-in,
its assessments running as the layer's flow.

(The accelerator itself, without the layer, is proved in tests/test_docuinsights_engine.py.)"""
from __future__ import annotations

import time

from test_runs import ADMIN, _customer, shop_client  # noqa: E402,F401  (the API fixture)

STAGES = ["intake", "classify", "extract", "privacy", "resolve", "screening", "graph", "evidence", "reference", "policy", "contradictions", "integrity", "verify", "triage"]


def test_the_pack_joins_the_catalog_and_stays_out_of_the_planner(packs):
    from caf.core import get_agent
    from caf.orchestrator import load_playbooks
    from caf.orchestrator.router import catalog_for_planner
    from caf.packs import list_packs

    pack = next(p for p in list_packs() if p["id"] == "docuinsights")
    assert pack["agents"] == 19 and pack["flows"] == 4 and pack["tag"] == "DI" and pack["home"] is None
    v = get_agent("di_verifier")
    assert v.spec.pack == "docuinsights" and v.spec.runners == ["code", "model", "twin"] and v.spec.kind.value == "hybrid" and not v.spec.auto_route
    assert [a["id"] for a in get_agent("di_feedback").spec.actions] == ["list_proposals", "decide_proposal"]
    books = load_playbooks()
    assert [s.id for s in books["di_case_assessment"].steps] == STAGES and not books["di_case_assessment"].auto_route
    assert not [a for a in catalog_for_planner() if str(a.get("id", "")).startswith("di_")]   # a case assessment is never planned from a marketing goal


def test_the_flow_takes_a_case_from_intake_to_a_verified_recommendation(packs, shop_client):
    v = shop_client.post("/flows/di_case_assessment/run", json={"inputs": {"sample": "customs_claim/review"}, "wait_s": 180}, headers=ADMIN).json()
    assert v["status"] == "completed" and [s["id"] for s in v["steps"]] == STAGES and [s["status"] for s in v["steps"]] == ["ok"] * len(STAGES)
    out = {s["id"]: s["result"]["output"] for s in v["steps"]}
    assert all(o["runner"] == "code" for o in out.values())
    ref = out["intake"]["result"]["case_ref"]
    assert ref.startswith("CLM-2026-000184") and all(o["result"]["case_ref"] == ref for o in out.values())   # every stage worked on the case intake registered
    assert out["classify"]["result"]["mismatches"] == 2 and out["extract"]["result"]["required_missing"] == 1
    assert out["reference"]["result"]["references"][0]["candidate"] == "8482 10 90 00" and out["reference"]["result"]["references"][0]["mode"] == "snapshot"
    rec = out["verify"]["result"]["recommendation"]
    assert rec["outcome"] == "request_evidence" and rec["requests"] == ["Packing list for this claim"] and out["verify"]["result"]["blocked"] == []
    assert any("decide the case" in x for x in out["verify"]["human_followups"])
    assert out["privacy"]["result"]["personal_data"]["documents"] == [] and out["screening"]["result"]["matches"] == [] and out["integrity"]["result"]["findings"]
    triage = out["triage"]["result"]   # the verify step closes the assessment with the triage; the triage step reports it
    assert triage["priority"] == "normal" and triage["queue"] == "evidence" and triage["due"] == out["verify"]["result"]["triage"]["due"] and triage["reasons"]
    verify = v["steps"][STAGES.index("verify")]["result"]
    assert any(a["name"] == "assessment.json" for a in verify["artifacts"]) and "degraded_reasons" not in verify["provenance"]
    evidence = v["steps"][STAGES.index("evidence")]["result"]
    assert any(c["rule"] == "Packing list" and c["status"] == "fail" for c in evidence["checks"])
    # the on-demand agents work on the same case, by reference
    ans = shop_client.post("/agents/di_assistant/run", json={"inputs": {"case_ref": ref, "question": "What evidence is missing?"}}, headers=ADMIN).json()
    assert ans["status"] == "ok" and "Packing list" in ans["output"]["result"]["answer"] and ans["output"]["result"]["grounded"]
    cf = shop_client.post("/agents/di_counterfactual/run", json={"inputs": {"case_ref": ref, "scenario": "late_claim"}}, headers=ADMIN).json()
    assert cf["output"]["result"]["after"]["outcome"] == "review" and [m["id"] for m in cf["output"]["result"]["moved"]] == ["RULE-claim_window"]


def test_the_other_flows_and_a_second_profile(packs, shop_client):
    w = shop_client.post("/flows/di_what_if/run", json={"inputs": {"sample": "customs_claim/review", "scenario": "add_packing_list"}, "wait_s": 180}, headers=ADMIN).json()
    assert w["status"] == "completed" and w["steps"][-1]["result"]["output"]["result"]["after"]["outcome"] == "review"
    f = shop_client.post("/flows/di_assess_and_file/run", json={"inputs": {"sample": "trade_finance/discrepant"}, "wait_s": 180}, headers=ADMIN).json()
    assert f["status"] == "completed" and [s["status"] for s in f["steps"]] == ["ok"] * 4
    assess, draft, filed = (s["result"]["output"]["result"] for s in f["steps"][1:])
    assert assess["recommendation"]["label"] == "Discrepant presentation" and len(draft["items"]) == 3 and filed["repository"] == "local" and len(filed["documents"]) == 5


def test_the_pack_meets_the_layers_quality_standard_and_passes_its_battery(packs):
    """The standard every agent on the shelf is held to (caf.quality) and the battery every agent is put through (caf.certify)."""
    from caf.certify import certify
    from caf.quality import audit

    report = audit("docuinsights")
    assert report["ok"] and not [f for a in report["agents"] for f in a["findings"]], [f for a in report["agents"] for f in a["findings"]]
    assert {a["standing"] for a in report["agents"]} == {"calculates_or_stands_in"} and all(x["declared"] for a in report["agents"] for x in a["actions"])
    done = certify(pack="docuinsights", fast=True)
    failed = [(r["id"], x["trial"], x["detail"]) for r in done["agents"] for x in r["trials"] if not x["pass"]]
    assert done["totals"]["agents"] == 19 and not failed, failed


def test_the_assurance_agents_screen_check_and_take_a_persons_answer(packs, shop_client):
    listed = shop_client.post("/agents/di_screening/run", json={"inputs": {"sample": "trade_finance/listed"}}, headers=ADMIN).json()
    assert listed["status"] == "ok" and {m["entry"] for m in listed["output"]["result"]["matches"]} == {"Quellmar Shipping Lines", "Tyberon Star"}
    assert any("confirm or clear" in x for x in listed["output"]["human_followups"])
    ref = listed["output"]["result"]["case_ref"]
    done = shop_client.post("/agents/di_verifier/run", json={"inputs": {"case_ref": ref}}, headers=ADMIN).json()["output"]["result"]
    assert done["triage"]["queue"] == "compliance" and done["triage"]["priority"] == "urgent"
    fid = next(f["id"] for f in listed["output"]["result"]["findings"] if "Tiberon" in f["title"])
    no = shop_client.post("/agents/di_screening/run", json={"inputs": {"action": "disposition", "case_ref": ref, "finding_id": fid, "decision": "clear"}}, headers=ADMIN).json()
    assert no["status"] == "error" and "why" in no["error"]   # a match is never cleared without a reason
    yes = shop_client.post("/agents/di_screening/run", json={"inputs": {"action": "disposition", "case_ref": ref, "finding_id": fid, "decision": "clear", "reason": "A different vessel: registry number checked"}},
                           headers=ADMIN).json()
    assert yes["status"] == "ok" and yes["output"]["result"]["waiting"] == 1
    bad = shop_client.post("/agents/di_integrity/run", json={"inputs": {"sample": "supplier_invoice/payment_change"}}, headers=ADMIN).json()
    assert {f["id"]: f["status"] for f in bad["output"]["result"]["findings"]}["INT-invoice_arithmetic"] == "fail"
    rec = shop_client.post("/agents/di_repository/run", json={"inputs": {"action": "decision_record", "case_ref": ref, "masked": True}}, headers=ADMIN).json()
    assert rec["status"] == "ok" and rec["output"]["result"]["masked"] is True, rec.get("error")


def test_wrong_input_fails_plainly_and_a_bare_run_says_which_sample_it_used(packs, shop_client):
    bad = shop_client.post("/agents/di_classifier/run", json={"inputs": {"case_ref": "NO-SUCH-CASE"}}, headers=ADMIN).json()
    assert bad["status"] == "error" and "no case" in bad["error"]   # an error, never a modelled sample
    bare = shop_client.post("/agents/di_evidence/run", json={"inputs": {"request": "check the evidence"}}, headers=ADMIN).json()
    assert bare["status"] == "ok" and bare["output"]["runner"] == "code"
    assert any(c["rule"] == "a case was given" and c["status"] == "warn" and "supplier_invoice/mismatch" in c["message"] for c in bare["checks"])


def test_a_customers_cases_are_its_own(packs, shop_client):
    hdr = _customer(shop_client, [{"kind": "agent", "item_id": a} for a in ("di_intake", "di_verifier", "di_assistant")])
    mine = shop_client.post("/agents/di_verifier/run", json={"inputs": {"sample": "customs_claim/positive"}}, headers=hdr).json()
    assert mine["status"] == "ok" and mine["output"]["result"]["recommendation"]["outcome"] == "proceed"
    ref = mine["output"]["result"]["case_ref"]
    theirs = shop_client.post("/agents/di_assistant/run", json={"inputs": {"case_ref": ref, "question": "What evidence is missing?"}}, headers=ADMIN).json()
    assert theirs["status"] == "error" and "no case" in theirs["error"]   # the operator's tenant does not hold the customer's case


def test_the_workbench_is_mounted_under_the_layers_sign_in(packs, shop_client):
    assert shop_client.app.state.docuinsights_error is None and shop_client.get("/health").json()["docuinsights"] == "mounted"
    page = shop_client.get("/docuinsights/")
    assert page.status_code == 200 and "DocuInsights" in page.text and "/docuinsights/static/hub.js" in page.text and 'data-auth="host"' in page.text
    assert shop_client.get("/docuinsights/static/hub.js").status_code == 200
    assert shop_client.get("/docuinsights/api/cases").status_code == 401   # the page is public; its API is not
    cfg = shop_client.get("/docuinsights/api/config", headers=ADMIN).json()
    assert cfg["auth"] == "host" and cfg["tenant"] == "operator" and {p["id"] for p in cfg["profiles"]} >= {"customs_claim", "trade_finance"}
    made = shop_client.post("/docuinsights/api/samples", json={"profile": "customs_claim", "sample": "contradiction"}, headers=ADMIN)
    assert made.status_code == 201
    ref = made.json()["reference"]
    case = {}
    for _ in range(600):   # the assessment runs as the layer's flow, on a worker thread
        case = shop_client.get(f"/docuinsights/api/cases/{ref}", headers=ADMIN).json()
        if (case.get("progress") or {}).get("state") in ("done", "failed"):
            break
        time.sleep(0.1)
    assert case["progress"]["state"] == "done" and case["assessment"]["recommendation"]["outcome"] == "review"
    for _ in range(100):   # the run id is written when the flow returns
        case = shop_client.get(f"/docuinsights/api/cases/{ref}", headers=ADMIN).json()
        if case.get("layer_run"):
            break
        time.sleep(0.1)
    run = shop_client.get(f"/runs/{case['layer_run']}", headers=ADMIN).json()
    assert run["status"] == "completed" and len(run["steps"]) == len(STAGES)   # the same assessment, visible in the portal with its steps
    hdr = _customer(shop_client, [{"kind": "agent", "item_id": "utm_generation"}], cid="no-docuinsights")
    assert shop_client.get("/docuinsights/api/cases", headers=hdr).status_code == 403   # a customer without the pack has no workbench
