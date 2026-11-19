"""The Agent Shop: customers, keys, subscriptions, the customer's policy (its own model, masking, websites, limits),
entitlements on REST / MCP / A2A, metering, the agent check, the storefront pages, and orchestration flows (the
flow builder, publishing, renting a flow as a whole, run_flow_<id> MCP tools)."""

from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from test_api import MCP_HEADERS  # noqa: E402
from test_llm_foundry import KEY  # noqa: E402

DEMO = "https://www.meridian-mutual.example/products/home-insurance.html"


@pytest.fixture()
def shop_env(monkeypatch, tmp_path):
    from caf.config import reset_settings_cache
    from caf.shop import reset_shops

    monkeypatch.setenv("CAF_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CAF_LLM_PROVIDER", "offline")
    monkeypatch.setenv("CAF_API_TOKEN", "admin-t0ken")
    monkeypatch.setenv("CAF_SECRET_KEY", "a passphrase for the shop's vault")
    reset_settings_cache()
    reset_shops()
    yield
    reset_settings_cache()
    reset_shops()


@pytest.fixture()
def client(shop_env):
    from caf.api.server import create_app

    with TestClient(create_app()) as c:
        yield c


ADMIN = {"Authorization": "Bearer admin-t0ken"}


def _customer(client, cid="meridian-mutual", name="Meridian Mutual", subs=None):
    assert client.post("/shop/admin/api/customers", json={"id": cid, "name": name}, headers=ADMIN).status_code == 201
    if subs is not None:
        assert client.put(f"/shop/admin/api/customers/{cid}/subscriptions", json=subs, headers=ADMIN).status_code == 200
    key = client.post(f"/shop/admin/api/customers/{cid}/keys", json={"label": "test"}, headers=ADMIN).json()["key"]
    assert key.startswith("cxn_")
    return key, {"Authorization": f"Bearer {key}"}


# ------------------------------------------------------------------------------------------------ store + policy
def test_store_keys_subscriptions_and_entitlements(shop_env):
    from caf.config import get_settings
    from caf.shop import ShopError, get_shop

    shop = get_shop(get_settings())
    c = shop.store.add_customer("acme", "ACME", contact="ops@acme.example")
    assert c["run_mode_number"] == 1 and shop.entitled_agents(c) == set()
    full, rec = shop.store.issue_key("acme", "laptop")
    assert shop.store.customer_for_key(full)["id"] == "acme" and shop.store.customer_for_key("cxn_nope_x") is None
    assert shop.store.customer_for_key(full[:-1] + ("a" if full[-1] != "a" else "b")) is None  # one character off
    shop.store.set_subscriptions("acme", [{"kind": "flow", "item_id": "website_audit"}, {"kind": "agent", "item_id": "email_creation"}])
    c = shop.store.get_customer("acme")
    # renting a flow rents the flow as a whole, not its agents one by one
    assert shop.entitled_agents(c) == {"email_creation"}
    assert shop.entitled_flows(c) == ["website_audit"]
    with pytest.raises(ShopError) as exc:
        shop.require_entitled(c, "blog_post_creation")
    assert exc.value.status == 403
    with pytest.raises(ShopError) as exc:
        shop.require_entitled_flow(c, "campaign_launch")
    assert exc.value.status == 403
    # every agent of a flow rented one by one entitles the flow too; "playbook" is accepted as a synonym of "flow"
    shop.store.set_subscriptions("acme", [{"kind": "agent", "item_id": a} for a in ("technical_seo_recommendations", "geo_recommendations",
                                          "web_accessibility_evaluation", "heatmap_analysis", "ga4_traffic_report", "experiment_ideation")]
                                 + [{"kind": "playbook", "item_id": "competitive_pitch"}])
    c = shop.store.get_customer("acme")
    assert shop.entitled_flows(c) == ["competitive_pitch", "website_audit"]
    assert {s["kind"] for s in shop.store.list_subscriptions("acme")} == {"agent", "flow"}
    shop.store.set_subscriptions("acme", [{"kind": "all", "item_id": "*", "ends_at": "2020-01-01"}])  # expired
    assert shop.entitled_agents(shop.store.get_customer("acme")) == set()
    shop.store.revoke_key("acme", rec["id"])
    assert shop.store.customer_for_key(full) is None
    with pytest.raises(ShopError):
        shop.store.add_customer("Bad Id", "x")


def test_policy_encrypts_inline_credentials_and_builds_customer_llm_settings(shop_env, monkeypatch):
    from caf.config import get_settings
    from caf.shop import PolicyConfigError, get_shop, load_policy, mask_text

    shop = get_shop(get_settings())
    shop.store.add_customer("acme", "ACME")
    policy = {"models": {"provider": "anthropic_foundry", "base_url": "https://acme.services.ai.azure.com/anthropic/v1/messages",
                         "api_key": "secret-foundry-key", "tiers": {"balanced": "acme-sonnet", "pro": "acme-opus"}},
              "data": {"mask_before_model": ["email", "phone"]}, "reach": {"websites": ["*.acme.example"]}, "limits": {"runs_per_day": 3}}
    c = shop.set_policy("acme", policy)
    assert c["run_mode"] == "coforge_cloud_customer_model"
    raw = shop.store.get_customer("acme")["policy"]
    assert raw["models"]["api_key"].startswith("enc:v1:") and "secret-foundry-key" not in json.dumps(raw)
    pol = shop.policy_of(shop.store.get_customer("acme"))
    assert pol.redacted()["models"]["api_key"] == "***" and pol.credential() == "secret-foundry-key"
    llm_settings = pol.llm_settings(get_settings().llm)
    cfg = llm_settings.providers["anthropic_foundry"]
    assert llm_settings.provider == "anthropic_foundry" and cfg["api_key"] == "secret-foundry-key"
    assert cfg["tiers"] == {"balanced": "acme-sonnet", "pro": "acme-opus", "fast": "acme-sonnet", "complex": "acme-opus"}  # unnamed tiers follow the nearest named one
    assert pol.settings_for(get_settings()).security["allowed_hosts"] == ["*.acme.example"]
    assert mask_text("Mail jane@acme.example or call +44 20 7946 0958 about POL-1234567", ["email", "phone", "policy_number"]) \
        == "Mail [email] or call [phone] about [policy number]"
    # a policy that names an environment variable the server does not have cannot be applied
    monkeypatch.delenv("CAF_SECRET_KEY", raising=False)
    with pytest.raises(PolicyConfigError):
        load_policy({"models": {"provider": "openai", "api_key_env": "NO_SUCH_VAR", "tiers": {"fast": "x"}}}).llm_settings(get_settings().llm)
    with pytest.raises(RuntimeError):  # inline keys need the vault key
        load_policy({"models": {"provider": "openai", "api_key": "k", "tiers": {"fast": "x"}}}).for_storage()
    with pytest.raises(ValueError):
        load_policy({"models": {"provider": "nope"}})


# ------------------------------------------------------------------------------------------------ REST surfaces
def test_customer_sees_and_runs_only_its_subscription(client):
    key, hdr = _customer(client, subs=[{"kind": "flow", "item_id": "website_audit"}, {"kind": "agent", "item_id": "utm_generation"}])
    ids = {a["id"] for a in client.get("/agents", headers=hdr).json()}
    assert ids == {"utm_generation"}  # the rented flow does not rent its agents one by one
    assert len(client.get("/agents", headers=ADMIN).json()) == 28  # the operator sees everything
    assert [p["id"] for p in client.get("/playbooks", headers=hdr).json()] == ["website_audit"]
    assert [f["id"] for f in client.get("/flows", headers=hdr).json()] == ["website_audit"]
    assert len(client.get("/flows", headers=ADMIN).json()) == 8
    assert client.get("/flows/campaign_launch", headers=hdr).status_code == 403
    assert client.get("/flows/website_audit", headers=hdr).json()["tool"] == "run_flow_website_audit"
    assert client.get("/agents/blog_post_creation", headers=hdr).status_code == 403
    assert client.post("/agents/blog_post_creation/run", json={}, headers=hdr).status_code == 403
    r = client.post("/agents/utm_generation/run", json={"inputs": {"base_url": DEMO, "campaign": "2026q4_us_home_launch", "channels": ["email"]}}, headers=hdr)
    assert r.status_code == 200 and r.json()["status"] == "ok" and r.json()["run_id"].startswith("c-meridian-mut")
    run_id = r.json()["run_id"]
    assert client.get(f"/runs/{run_id}/trace", headers=hdr).status_code == 200
    # a customer never sees another customer's runs
    _, other = _customer(client, cid="other-co", name="Other", subs=[{"kind": "all"}])
    assert client.get(f"/runs/{run_id}/trace", headers=other).status_code == 404
    # a flow outside the subscription is refused; a plan with an unsubscribed agent skips that step
    assert client.post("/orchestrate", json={"goal": "launch", "playbook": "campaign_launch"}, headers=hdr).status_code == 403
    assert client.post("/flows/campaign_launch/run", json={"inputs": {"url": DEMO}}, headers=hdr).status_code == 403
    plan = {"mode": "custom", "steps": [{"id": "links", "agent": "utm_generation"}, {"id": "mail", "agent": "email_creation", "needs": ["links"]}]}
    v = client.post("/orchestrate", json={"goal": "Tag the launch links", "plan": plan, "inputs": {"url": DEMO}}, headers=hdr).json()
    by_step = {s["id"]: s["status"] for s in v["steps"]}
    assert by_step == {"links": "ok", "mail": "skipped"} and any("not in this customer's subscription" in w for w in v["warnings"])
    assert client.get(f"/runs/{v['run_id']}", headers=hdr).json()["run_id"] == v["run_id"]
    assert client.get(f"/runs/{v['run_id']}", headers=other).status_code == 404
    # the rented flow runs as a whole: all six of its agents run for this customer even though none is rented singly
    fv = client.post("/flows/website_audit/run", json={"inputs": {"url": DEMO}}, headers=hdr).json()
    assert fv["flow"]["id"] == "website_audit" and fv["route"]["playbook"] == "website_audit"
    assert {s["status"] for s in fv["steps"]} <= {"ok", "degraded"} and len(fv["steps"]) == 6
    assert client.get(f"/runs/{fv['run_id']}", headers=hdr).status_code == 200
    # metering: every agent run is on the account (one direct, two steps of the plan, six of the flow), with surface and outcome
    me = client.get("/shop/me", headers=hdr).json()
    assert me["customer"]["id"] == "meridian-mutual" and me["usage"]["totals"]["runs"] == 9
    assert {(u["agent_id"], u["surface"], u["status"]) for u in me["recent"]} >= {("utm_generation", "api", "ok"), ("email_creation", "api", "skipped")}
    assert {u["agent_id"] for u in me["recent"]} >= {"heatmap_analysis", "technical_seo_recommendations", "experiment_ideation"}
    assert me["connection"]["mcp"]["url"].endswith("/mcp") and "run_utm_generation" in me["connection"]["mcp"]["tools"]
    assert "run_flow_website_audit" in me["connection"]["mcp"]["tools"] and me["entitled_flows"] == ["website_audit"]
    assert me["flows"][0]["id"] == "website_audit" and me["flows"][0]["agent_names"]
    assert me["keys"][0]["prefix"] == key[:12] and "key" not in me["keys"][0]
    admin_usage = client.get("/shop/admin/api/usage?customer=meridian-mutual", headers=ADMIN).json()
    assert admin_usage["totals"]["runs"] == 9


def test_tokens_roles_and_public_pages(client):
    key, hdr = _customer(client, subs=[{"kind": "all"}])
    # the admin API needs the operator token, not a customer key; the account needs a customer key
    assert client.get("/shop/admin/api/customers", headers=hdr).status_code == 403
    assert client.get("/shop/admin/api/customers").status_code == 401  # testclient is loopback, but a token is configured
    assert client.get("/shop/me", headers=ADMIN).status_code == 401
    assert client.get("/agents", headers={"Authorization": "Bearer cxn_dead_beefbeefbeef"}).status_code == 401
    # public: storefront, listing pages, catalog, access requests
    for path in ("/shop", "/shop/agents/email_creation", "/shop/account", "/shop/admin", "/shop/static/shop.css", "/favicon.svg"):
        assert client.get(path).status_code == 200, path
    assert "CX Nova Agent Orchestration Layer" in client.get("/shop").text
    root = client.get("/", headers={"accept": "text/html"}, follow_redirects=False)
    assert root.headers["location"] == "/shop"
    cat = client.get("/shop/catalog").json()
    assert len(cat["agents"]) == 28 and len(cat["flows"]) == 8 and cat["agents"][0]["price"]["monthly"]
    assert cat["flows"][0]["stages"] and cat["flows"][0]["price"]["monthly"]
    assert client.get("/shop/catalog/flows/website_audit").json()["agent_cards"][0]["id"] == "technical_seo_recommendations"
    assert client.get("/shop/flows/website_audit").status_code == 200 and client.get("/shop/catalog/flows/nope").status_code == 404
    one = client.get("/shop/catalog/technical_seo_recommendations").json()
    assert one["model_steps"] and one["price"]["outcome"] == "SEO audit"
    r = client.post("/shop/requests", json={"name": "Priya", "email": "priya@acme.example", "items": ["website_audit"], "run_mode": "customer_cloud"})
    assert r.status_code == 201
    reqs = client.get("/shop/admin/api/requests", headers=ADMIN).json()
    assert reqs[0]["email"] == "priya@acme.example" and reqs[0]["status"] == "new"
    # suspend the customer: its key stops working
    client.patch("/shop/admin/api/customers/meridian-mutual", json={"status": "suspended"}, headers=ADMIN)
    assert client.get("/agents", headers=hdr).status_code == 401


def test_daily_limit_from_the_policy(client):
    key, hdr = _customer(client, subs=[{"kind": "agent", "item_id": "utm_generation"}])
    assert client.put("/shop/admin/api/customers/meridian-mutual/policy", json={"limits": {"runs_per_day": 1}}, headers=ADMIN).status_code == 200
    body = {"inputs": {"base_url": DEMO, "campaign": "2026q4_us_home_launch", "channels": ["email"]}}
    assert client.post("/agents/utm_generation/run", json=body, headers=hdr).status_code == 200
    r = client.post("/agents/utm_generation/run", json=body, headers=hdr)
    assert r.status_code == 429 and "daily limit" in r.json()["detail"]


def test_mcp_and_a2a_follow_the_subscription(client):
    key, hdr = _customer(client, subs=[{"kind": "agent", "item_id": "utm_generation"}])
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
    h = {**MCP_HEADERS, **hdr}
    assert client.post("/mcp/", json=init, headers=h).status_code == 200
    listed = client.post("/mcp/", json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "list_agents", "arguments": {}}}, headers=h).json()
    agents = json.loads(listed["result"]["content"][0]["text"])
    assert [a["id"] for a in agents] == ["utm_generation"]
    denied = client.post("/mcp/", json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                        "params": {"name": "run_email_creation", "arguments": {"inputs": {"overview": "x"}}}}, headers=h).json()
    assert denied["result"].get("isError") and "not in your subscription" in denied["result"]["content"][0]["text"]
    ok = client.post("/mcp/", json={"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                    "params": {"name": "run_utm_generation", "arguments": {"inputs": {"base_url": DEMO, "campaign": "2026q4_us_home_launch", "channels": ["email"]}}}}, headers=h).json()
    assert not ok["result"].get("isError")
    # A2A: the card lists only the subscribed skills; a message runs under the customer's context
    card = client.get("/.well-known/agent-card.json", headers=hdr).json()
    assert [s["id"] for s in card["skills"]] == ["utm_generation"] and card["name"].startswith("CX Nova Agent Orchestration Layer")
    skills = [s["id"] for s in client.get("/.well-known/agent-card.json").json()["skills"]]
    assert len(skills) == 36 and "flow:website_audit" in skills  # the operator's card: 28 agents and 8 built-in flows
    assert client.get("/a2a/agents/email_creation/agent-card.json", headers=hdr).status_code == 403
    msg = {"jsonrpc": "2.0", "id": 5, "method": "message/send",
           "params": {"message": {"role": "user", "messageId": "m1", "parts": [{"kind": "text", "text": "Create UTM links for our email newsletter"}]}}}
    task = client.post("/a2a", json=msg, headers=hdr).json()["result"]
    assert task["kind"] == "task"
    assert client.post("/a2a", json={"jsonrpc": "2.0", "id": 6, "method": "tasks/get", "params": {"id": task["id"]}}, headers=hdr).json()["result"]["id"] == task["id"]
    me = client.get("/shop/me", headers=hdr).json()
    assert {u["surface"] for u in me["recent"]} >= {"mcp", "a2a"}
    # tools/list is the customer's entitlement: its one agent and the generic tools, no flow tools yet
    names = {t["name"] for t in client.post("/mcp/", json={"jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": {}}, headers=h).json()["result"]["tools"]}
    assert names == {"run_utm_generation", "list_agents", "list_flows", "run_flow", "orchestrate", "approve_actions", "get_run"}
    assert len({t["name"] for t in client.post("/mcp/", json={"jsonrpc": "2.0", "id": 8, "method": "tools/list", "params": {}}, headers={**MCP_HEADERS, **ADMIN}).json()["result"]["tools"]}) == 28 + 8 + 6
    # rent a flow: its tool appears, runs as a whole over MCP and over A2A, and is refused without the subscription
    client.put("/shop/admin/api/customers/meridian-mutual/subscriptions", json=[{"kind": "agent", "item_id": "utm_generation"}, {"kind": "flow", "item_id": "competitive_pitch"}], headers=ADMIN)
    names = {t["name"] for t in client.post("/mcp/", json={"jsonrpc": "2.0", "id": 9, "method": "tools/list", "params": {}}, headers=h).json()["result"]["tools"]}
    assert "run_flow_competitive_pitch" in names and "run_flow_website_audit" not in names
    flows = json.loads(client.post("/mcp/", json={"jsonrpc": "2.0", "id": 10, "method": "tools/call", "params": {"name": "list_flows", "arguments": {}}}, headers=h).json()["result"]["content"][0]["text"])
    assert [f["id"] for f in flows] == ["competitive_pitch"] and flows[0]["tool"] == "run_flow_competitive_pitch"
    ran = client.post("/mcp/", json={"jsonrpc": "2.0", "id": 11, "method": "tools/call", "params": {"name": "run_flow_competitive_pitch",
                      "arguments": {"args": {"url": DEMO, "competitors": ["https://www.rival.example/"], "goal": "Pitch against the rival"}}}}, headers=h).json()
    assert not ran["result"].get("isError"), ran["result"]["content"][0]["text"]
    view = ran["result"].get("structuredContent") or json.loads(ran["result"]["content"][0]["text"])
    assert view["flow"]["id"] == "competitive_pitch" and len(view["steps"]) == 3 and {s["status"] for s in view["steps"]} <= {"ok", "degraded"}
    denied = client.post("/mcp/", json={"jsonrpc": "2.0", "id": 12, "method": "tools/call", "params": {"name": "run_flow", "arguments": {"args": {"flow_id": "website_audit", "inputs": {"url": DEMO}}}}}, headers=h).json()
    assert denied["result"].get("isError") and "not in your subscription" in denied["result"]["content"][0]["text"]
    card = client.get("/.well-known/agent-card.json", headers=hdr).json()
    assert [s["id"] for s in card["skills"]] == ["utm_generation", "flow:competitive_pitch"]
    msg = {"jsonrpc": "2.0", "id": 13, "method": "message/send", "params": {"message": {"role": "user", "messageId": "m2", "parts": [
        {"kind": "data", "data": {"flow": "competitive_pitch", "inputs": {"url": DEMO, "competitors": ["https://www.rival.example/"]}}}]}}}
    task = client.post("/a2a", json=msg, headers=hdr).json()["result"]
    assert task["kind"] == "task" and len(task["artifacts"]) == 3
    msg["params"]["message"]["parts"][0]["data"]["flow"] = "website_audit"
    assert client.post("/a2a", json=msg, headers=hdr).json()["error"]["message"].startswith("flow 'website_audit' is not in your subscription")


# ------------------------------------------------------------------------------------------------ the flow builder
def test_flow_builder_compose_publish_rent(client):
    """The operator composes a flow from agents, tests it, publishes it; it becomes a storefront item, a playbook for the
    router and the console, an MCP tool and a rentable subscription; unpublishing takes all of that away again."""
    admin_flows = client.get("/shop/admin/api/flows", headers=ADMIN).json()
    assert len(admin_flows) == 8 and all(f["source"] == "builtin" and f["status"] == "published" for f in admin_flows)
    assert client.get("/shop/admin/api/flows").status_code == 401  # operator only
    flow = {"id": "launch_kit", "name": "Launch kit", "description": "Brief, then emails and tagged links in parallel.",
            "inputs": [{"name": "url", "description": "Product page", "required": True, "example": DEMO}, {"name": "audience"}],
            "steps": [{"id": "brief", "agent": "campaign_brief_generation", "purpose": "the brief everything hangs off"},
                      {"id": "mail", "agent": "email_creation", "needs": ["brief"], "inputs": {"overview": "${steps.brief.output.summary|goal}"}},
                      {"id": "links", "agent": "utm_generation", "needs": ["brief"]}],
            "triggers": ["launch kit"], "price": {"monthly": 400, "included_runs": 40}}
    # validation lists every problem without saving
    bad = {**flow, "steps": [{"id": "a", "agent": "nope"}, {"id": "a", "agent": "email_creation", "needs": ["zzz"], "inputs": {"wrong_field": 1}}]}
    v = client.post("/shop/admin/api/flows/validate", json=bad, headers=ADMIN).json()
    assert not v["ok"] and len(v["problems"]) == 4
    assert client.put("/shop/admin/api/flows/launch_kit", json=bad, headers=ADMIN).status_code == 400
    assert client.put("/shop/admin/api/flows/website_audit", json={**flow, "id": "website_audit"}, headers=ADMIN).status_code == 409  # built-in
    v = client.post("/shop/admin/api/flows/validate", json=flow, headers=ADMIN).json()
    assert v["ok"] and v["stages"] == [["brief"], ["mail", "links"]]
    # saved as a draft: invisible to customers and the storefront, testable by the operator
    saved = client.put("/shop/admin/api/flows/launch_kit", json=flow, headers=ADMIN).json()
    assert saved["status"] == "draft" and saved["version"] == 1 and saved["tool"] == "run_flow_launch_kit" and saved["price"]["monthly"] == 400
    assert "launch_kit" not in {f["id"] for f in client.get("/shop/catalog").json()["flows"]}
    assert client.get("/shop/catalog/flows/launch_kit").status_code == 404
    assert "launch_kit" not in {p["id"] for p in client.get("/playbooks", headers=ADMIN).json()}
    test = client.post("/shop/admin/api/flows/launch_kit/test", json={}, headers=ADMIN).json()
    assert test["total"] == 3 and test["passed"] == 3 and test["sample_inputs"]["url"] == DEMO
    # published: storefront, catalog, playbook list (router + console), MCP tool, A2A skill
    pub = client.post("/shop/admin/api/flows/launch_kit/publish", headers=ADMIN).json()
    assert pub["status"] == "published" and pub["published_at"]
    assert client.get("/shop/catalog/flows/launch_kit").json()["stages"] == [["brief"], ["mail", "links"]]
    assert "launch_kit" in {p["id"] for p in client.get("/playbooks", headers=ADMIN).json()}
    assert client.get("/shop/flows/launch_kit").status_code == 200
    assert 'id="caf-host-playbooks"' in client.get("/console").text and "launch_kit" in client.get("/console").text
    tools = {t["name"] for t in client.post("/mcp/", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, headers={**MCP_HEADERS, **ADMIN}).json()["result"]["tools"]}
    assert "run_flow_launch_kit" in tools
    assert "flow:launch_kit" in {s["id"] for s in client.get("/.well-known/agent-card.json").json()["skills"]}
    # the operator runs it as a playbook through /orchestrate; the explicit step binding is honoured
    view = client.post("/orchestrate", json={"goal": "Launch the flood-ready home cover", "playbook": "launch_kit", "inputs": {"url": DEMO}}, headers=ADMIN).json()
    assert view["route"]["playbook"] == "launch_kit" and [s["id"] for s in view["steps"]] == ["brief", "mail", "links"]
    # a customer rents it: required inputs are enforced, the run is metered, the subscription shows it
    key, hdr = _customer(client, subs=[{"kind": "flow", "item_id": "launch_kit"}])
    assert client.get("/agents", headers=hdr).json() == []
    missing = client.post("/flows/launch_kit/run", json={"inputs": {"audience": "brokers"}}, headers=hdr)
    assert missing.status_code == 400 and "url" in missing.json()["detail"]
    run = client.post("/flows/launch_kit/run", json={"inputs": {"url": DEMO, "audience": "brokers"}}, headers=hdr).json()
    assert run["flow"]["version"] == 1 and {s["status"] for s in run["steps"]} <= {"ok", "degraded", "needs_approval"}  # emails wait at the gate
    me = client.get("/shop/me", headers=hdr).json()
    assert me["entitled_flows"] == ["launch_kit"] and me["usage"]["totals"]["runs"] == 3 and me["flows"][0]["price"]["monthly"] == 400
    assert client.get("/shop/admin/api/flows/launch_kit", headers=ADMIN).json()["subscribers"] == 1
    # editing bumps the version; cloning a built-in gives an editable draft; unpublishing removes the tool and the listing
    edited = client.put("/shop/admin/api/flows/launch_kit", json={**flow, "name": "Launch kit v2"}, headers=ADMIN).json()
    assert edited["version"] == 2 and edited["status"] == "published"
    clone = client.post("/shop/admin/api/flows/website_audit/clone", json={"new_id": "audit_lite"}, headers=ADMIN).json()
    assert clone["status"] == "draft" and clone["source"] == "custom" and len(clone["steps"]) == 6
    assert client.post("/shop/admin/api/flows/website_audit/unpublish", headers=ADMIN).status_code == 409
    un = client.post("/shop/admin/api/flows/launch_kit/unpublish", headers=ADMIN).json()
    assert un["status"] == "draft"
    assert client.post("/flows/launch_kit/run", json={"inputs": {"url": DEMO}}, headers=hdr).status_code == 404
    tools = {t["name"] for t in client.post("/mcp/", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, headers={**MCP_HEADERS, **ADMIN}).json()["result"]["tools"]}
    assert "run_flow_launch_kit" not in tools and "launch_kit" not in {p["id"] for p in client.get("/playbooks", headers=ADMIN).json()}
    assert client.delete("/shop/admin/api/flows/launch_kit", headers=ADMIN).json() == {"deleted": "launch_kit"}
    assert client.delete("/shop/admin/api/flows/website_audit", headers=ADMIN).status_code == 409
    assert client.get("/shop/me", headers=hdr).json()["subscriptions"] == []  # the rented flow is gone with it


def test_flows_cli(shop_env, capsys, tmp_path):
    from caf.cli import main

    spec = tmp_path / "flow.yaml"
    spec.write_text("id: quick_links\nname: Quick links\ndescription: Tagged links from a brief\ninputs: [{name: url, required: true, example: " + DEMO + "}]\n"
                    "steps:\n  - {id: brief, agent: campaign_brief_generation}\n  - {id: links, agent: utm_generation, needs: [brief]}\n", encoding="utf-8")
    assert main(["shop", "flows", "save", str(spec)]) == 0
    assert main(["shop", "flows", "list"]) == 0
    out = capsys.readouterr().out
    assert "saved quick_links v1 (draft)" in out and "brief -> links" in out and "website_audit" in out
    assert main(["shop", "flows", "test", "quick_links"]) == 0
    assert main(["shop", "flows", "publish", "quick_links"]) == 0
    assert main(["shop", "flows", "clone", "website_audit", "audit_lite"]) == 0
    assert main(["shop", "flows", "show", "audit_lite"]) == 0
    out = capsys.readouterr().out
    assert "2/2 steps passed" in out and "run_flow_quick_links" in out and "source: custom" in out
    assert main(["shop", "flows", "delete", "website_audit"]) == 2  # built-in
    assert main(["shop", "flows", "unpublish", "quick_links"]) == 0 and main(["shop", "flows", "delete", "quick_links"]) == 0


# ------------------------------------------------------------------------------------------------ run mode 2
@pytest.fixture()
def customer_foundry():
    """The customer's own Claude deployment on Foundry: a stand-in that checks the Foundry key headers like the
    Foundry stand-in and answers every agent schema like the Claude stand-in."""
    from test_llm_anthropic import _Claude

    class Deployment(_Claude):
        requests: list[dict] = []

        def do_POST(self):  # noqa: N802
            if self.headers.get("x-api-key") != KEY or self.headers.get("api-key") != KEY or not self.path.endswith("/v1/messages"):
                data = json.dumps({"type": "error", "error": {"type": "authentication_error", "message": "invalid key"}}).encode()
                self.send_response(401)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            super().do_POST()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Deployment)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield Deployment, f"http://127.0.0.1:{server.server_address[1]}/anthropic/"
    server.shutdown()


def test_customer_model_policy_routes_calls_to_the_customers_deployment(client, customer_foundry, monkeypatch):
    deployment, base_url = customer_foundry
    monkeypatch.setenv("MERIDIAN_FOUNDRY_KEY", KEY)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    key, hdr = _customer(client, subs=[{"kind": "agent", "item_id": "email_creation"}, {"kind": "agent", "item_id": "campaign_brief_generation"}])
    policy = {"version": 2, "signed_by": "ciso@meridian-mutual.example",
              "models": {"provider": "anthropic_foundry", "base_url": base_url, "api_key_env": "MERIDIAN_FOUNDRY_KEY",
                         "tiers": {"fast": "mm-sonnet", "balanced": "mm-sonnet", "pro": "mm-opus", "complex": "mm-opus"}},
              "data": {"mask_before_model": ["email", "phone"]}}
    r = client.put("/shop/admin/api/customers/meridian-mutual/policy", json=policy, headers=ADMIN)
    assert r.status_code == 200 and r.json()["run_mode"] == "coforge_cloud_customer_model"
    me = client.get("/shop/me", headers=hdr).json()
    assert me["policy"]["models"]["provider"] == "anthropic_foundry" and me["customer"]["run_mode_number"] == 2
    # the shop itself runs offline; the customer's run goes to the customer's deployment with the customer's key
    overview = "Invite homeowners to our flood-ready webinar; questions to jane.doe@meridian-mutual.example or +44 20 7946 0958"
    r = client.post("/agents/email_creation/run", json={"inputs": {"overview": overview, "target_audience": "homeowners"}}, headers=hdr)
    assert r.status_code == 200, r.text
    assert r.json()["status"] in ("ok", "degraded", "needs_approval"), (r.json().get("error"), r.json().get("metrics"))
    assert deployment.requests, "the customer's deployment answered no call"
    models = {b["model"] for b in deployment.requests}
    assert models <= {"mm-sonnet", "mm-opus"}
    sent = json.dumps(deployment.requests)
    assert "jane.doe@meridian-mutual.example" not in sent and "7946 0958" not in sent and "[email]" in sent  # masked before the model
    usage = client.get("/shop/me/usage", headers=hdr).json()
    assert usage["totals"]["runs"] == 1 and usage["lines"][0]["run_mode"] == "coforge_cloud_customer_model"
    assert usage["totals"]["llm_calls"] >= 1
    # the agent check runs the subscribed agents on the customer's model and keeps the result on the account
    report = client.post("/shop/admin/api/customers/meridian-mutual/check?only=email_creation&tier=fast", headers=ADMIN).json()
    assert report["total"] == 1 and report["provider"] == "anthropic_foundry" and report["models"]["fast"] == "mm-sonnet"
    assert client.get("/shop/me", headers=hdr).json()["last_check"]["run_id"] == report["run_id"]
    # a policy naming a credential the server does not have is refused with a clear message, not sent elsewhere
    client.put("/shop/admin/api/customers/meridian-mutual/policy", json={**policy, "models": {**policy["models"], "api_key_env": "MISSING_VAR"}}, headers=ADMIN)
    r = client.post("/agents/email_creation/run", json={"inputs": {"overview": overview}}, headers=hdr)
    assert r.status_code == 503 and "MISSING_VAR" in r.json()["detail"]


def test_shop_cli_round_trip(shop_env, capsys, tmp_path):
    from caf.cli import main

    assert main(["shop", "add", "acme", "ACME Insurance", "--mode", "customer_cloud"]) == 0
    assert main(["shop", "subscribe", "acme", "--flows", "website_audit", "--agents", "email_creation,utm_generation"]) == 0
    assert main(["shop", "key", "acme", "--label", "ops"]) == 0
    out = capsys.readouterr().out
    assert "cxn_" in out and "2 agents" in out and "1 flow" in out
    pol = tmp_path / "p.yaml"
    pol.write_text("models: {provider: coforge}\ndata: {mask_before_model: [email]}\nlimits: {runs_per_day: 10}\n")
    assert main(["shop", "policy", "acme", "--file", str(pol)]) == 0
    assert main(["shop", "check", "acme", "--only", "utm_generation,email_creation"]) == 0
    assert main(["shop", "usage", "acme"]) == 0
    assert main(["shop", "customers"]) == 0
    out = capsys.readouterr().out
    assert "ok   utm_generation" in out and "2/2 agents passed" in out and "acme" in out


def test_every_page_script_parses(tmp_path):
    """One stray character in an inline script leaves a whole page blank; node --check catches it without a browser."""
    import re
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    web = Path(__file__).resolve().parents[1] / "src" / "caf" / "shop" / "web"
    checked = 0
    for page in sorted(web.glob("*.html")):
        for i, m in enumerate(re.finditer(r"<script(?![^>]*\bsrc=)([^>]*)>(.*?)</script>", page.read_text(encoding="utf-8"), re.S)):
            if "application/json" in m.group(1) or not m.group(2).strip():
                continue
            f = tmp_path / f"{page.stem}_{i}.js"
            f.write_text(m.group(2), encoding="utf-8")
            r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
            assert r.returncode == 0, f"{page.name} script {i}: {r.stderr[:400]}"
            checked += 1
    r = subprocess.run([node, "--check", str(web / "shop.js")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[:400]
    assert checked >= 5
