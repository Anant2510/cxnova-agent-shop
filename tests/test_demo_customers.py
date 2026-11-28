"""The demo customers: Harbor Bank consumes the layer over MCP, Xperion Airways over A2A. Seeding is idempotent (keys kept, state
reset), each key unlocks exactly its subscription on its door, the samples the account page shows are the calls that actually work,
and the operator alone can seed."""
from __future__ import annotations

import json
import time

from test_runs import ADMIN, shop_client  # noqa: E402,F401  (the API fixture)

MCP = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _seed(client, **params):
    r = client.post("/shop/admin/api/demo", params=params, headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.json()


def test_seed_is_idempotent_and_keys_are_shown_once(packs, shop_client):
    assert shop_client.post("/shop/admin/api/demo", headers={"Authorization": "Bearer cxn_not_a_key"}).status_code in (401, 403)
    before = shop_client.get("/shop/admin/api/demo", headers=ADMIN).json()
    assert [c["exists"] for c in before["customers"]] == [False, False] and [d["id"] for d in before["doors"]] == ["portal", "rest", "mcp", "a2a"]
    first = _seed(shop_client)
    ids = [c["id"] for c in first["customers"]]
    assert ids == ["harbor-bank", "xperion-airways"] and all(c["created"] and c["key"] and c["key"].startswith("cxn_") for c in first["customers"])
    bank, air = first["customers"]
    assert bank["door"] == "mcp" and bank["rents"]["flows"] == ["is_executive_review", "is_question"] and "is_analytics_chat" in bank["rents"]["agents"]
    assert air["door"] == "a2a" and set(air["rents"]["flows"]) >= {"vai_churn_prevention", "vai_irops_recovery"} and air["rents"]["agents"] == ["vai_predictive_propensity"]
    assert bank["not_available"] == [] and air["not_available"] == []
    assert bank["connection"]["samples"]["flow"] == "is_executive_review" and air["connection"]["samples"]["flow"] == "vai_passenger_journey"
    # again: nothing created, keys untouched (none shown), subscriptions back to the demo state
    assert shop_client.put("/shop/admin/api/customers/harbor-bank/subscriptions", json=[{"kind": "all", "item_id": "*"}], headers=ADMIN).status_code == 200
    second = _seed(shop_client)
    assert all(not c["created"] and c["key"] is None and c["key_prefix"] == f["key"][:12] for c, f in zip(second["customers"], first["customers"]))
    assert second["customers"][0]["rents"]["flows"] == ["is_executive_review", "is_question"]
    keys = shop_client.get("/shop/admin/api/customers/harbor-bank", headers=ADMIN).json()["keys"]
    assert len([k for k in keys if not k["revoked_at"]]) == 1
    # rotate: the old key stops working, the new one is shown
    third = _seed(shop_client, rotate_keys="true")
    assert all(c["key"] and c["key"] != f["key"] for c, f in zip(third["customers"], first["customers"]))
    assert shop_client.get("/shop/me", headers={"Authorization": "Bearer " + bank["key"]}).status_code in (401, 403)
    assert shop_client.get("/shop/me", headers={"Authorization": "Bearer " + third["customers"][0]["key"]}).json()["customer"]["id"] == "harbor-bank"
    assert shop_client.get("/shop/admin/api/demo", headers=ADMIN).json()["customers"][1]["exists"] is True


def test_harbor_bank_sees_only_its_intellisight_tools_over_mcp_and_the_sample_call_works(packs, shop_client):
    bank = _seed(shop_client)["customers"][0]
    hdr = {"Authorization": "Bearer " + bank["key"]}
    h = {**MCP, **hdr}
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
    assert shop_client.post("/mcp/", json=init, headers=h).status_code == 200
    names = {t["name"] for t in shop_client.post("/mcp/", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, headers=h).json()["result"]["tools"]}
    assert {"run_flow_is_executive_review", "run_flow_is_question", "run_is_analytics_chat", "run_is_custom_report", "run_is_recommendations"} <= names
    assert not any(n.startswith("run_vai_") or n.startswith("run_flow_vai_") or n == "run_campaign_brief_generation" for n in names)
    assert names == set(bank["connection"]["mcp"]["tools"])   # the account page lists exactly what tools/list returns
    call = bank["connection"]["samples"]["mcp_tool_call"]
    ran = shop_client.post("/mcp/", json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": call}, headers=h).json()
    assert not ran["result"].get("isError"), ran["result"]["content"][0]["text"]
    view = ran["result"].get("structuredContent") or json.loads(ran["result"]["content"][0]["text"])
    assert view["status"] in ("completed", "running", "queued") and view["run_id"]
    denied = shop_client.post("/mcp/", json={"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "run_flow_vai_churn_prevention", "arguments": {"args": {"signals": []}}}}, headers=h).json()
    assert denied["result"].get("isError")
    me = shop_client.get("/shop/me", headers=hdr).json()
    assert me["connection"]["doors"][2]["id"] == "mcp" and "<your key>" in me["connection"]["samples"]["rest_curl"]


def test_xperion_airways_delegates_a_journey_over_a2a_with_the_sample_message(packs, shop_client):
    air = _seed(shop_client)["customers"][1]
    hdr = {"Authorization": "Bearer " + air["key"]}
    card = shop_client.get("/.well-known/agent-card.json", headers=hdr).json()
    skills = {s["id"] for s in card["skills"]}
    assert skills == {"vai_predictive_propensity", "flow:vai_passenger_journey", "flow:vai_churn_prevention", "flow:vai_irops_recovery", "flow:vai_airport_disruption", "flow:is_executive_review"}
    assert {s.replace("agent:", "") for s in air["connection"]["a2a"]["skills"]} == skills
    # the sample is A2A 1.0, sent with the version header the account page shows beside it
    samples, v1 = air["connection"]["samples"], {**hdr, **air["connection"]["a2a"]["headers"]}
    msg = samples["a2a_message"]
    assert v1["A2A-Version"] == "1.0" and msg["method"] == "SendMessage" and msg["params"]["message"]["parts"][1]["data"]["flow"] == "vai_passenger_journey"
    task = shop_client.post("/a2a", json=msg, headers=v1).json()["result"]["task"]
    assert task["status"]["state"] in ("TASK_STATE_WORKING", "TASK_STATE_COMPLETED", "TASK_STATE_SUBMITTED")
    for _ in range(60):
        got = shop_client.post("/a2a", json={**samples["a2a_follow"], "params": {"id": task["id"]}}, headers=v1).json()["result"]
        if got["status"]["state"] in ("TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_INPUT_REQUIRED"):
            break
        time.sleep(1)
    assert got["status"]["state"] == "TASK_STATE_COMPLETED" and [a["name"].split(" ")[0] for a in got["artifacts"]] == ["ingest", "model", "identity", "score", "offer", "creatives", "activate", "feedback"]
    assert samples["a2a_decide"]["params"]["message"]["parts"] == [{"data": {"decisions": {"*": "approve"}}}]   # how a held action is decided on this door
    # a flow outside the rental is refused on this door too
    other = json.loads(json.dumps(msg)); other["params"]["message"]["parts"][1]["data"]["flow"] = "campaign_launch"
    assert "error" in shop_client.post("/a2a", json=other, headers=hdr).json()
    me = shop_client.get("/shop/me", headers=hdr).json()
    assert {u["surface"] for u in me["recent"]} >= {"a2a"} and me["connection"]["samples"]["flow"] == "vai_passenger_journey"
    # another customer's key does not see the airline's task (new keys for both; the airline's old one is now revoked)
    bank = _seed(shop_client, rotate_keys="true")["customers"][0]
    peek = shop_client.post("/a2a", json={"jsonrpc": "2.0", "id": 3, "method": "tasks/get", "params": {"id": task["id"]}}, headers={"Authorization": "Bearer " + bank["key"]}).json()
    assert "error" in peek
    # the revoked key no longer identifies the airline: a customer page refuses it, and the public card no longer shows its subscription
    assert shop_client.get("/shop/me", headers=hdr).status_code in (401, 403)
    assert len(shop_client.get("/.well-known/agent-card.json", headers=hdr).json()["skills"]) > 6   # the public card, as for anyone
