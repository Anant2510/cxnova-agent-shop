"""Service surfaces: REST, health, A2A and MCP (streamable HTTP)."""

import pytest
from fastapi.testclient import TestClient

DEMO = "https://www.meridian-mutual.example/products/home-insurance.html"
MCP_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json", "mcp-protocol-version": "2025-06-18"}


@pytest.fixture(scope="module")
def client():
    from caf.api.server import create_app

    with TestClient(create_app()) as c:
        yield c


def test_health_and_catalog(client):
    h = client.get("/health").json()
    assert h["agents_expected"] == 28
    agents = client.get("/agents").json()
    assert len(agents) == h["agents_registered"]
    one = client.get("/agents/utm_generation").json()
    assert one["input_schema"]["properties"]["base_url"]


def test_run_agent_and_orchestrate(client):
    r = client.post("/agents/utm_generation/run", json={"inputs": {"base_url": DEMO, "campaign": "2026q4_us_home_launch", "channels": ["email", "qr"]}}).json()
    assert r["status"] == "ok" and len(r["output"]["links"]) == 2
    v = client.post("/orchestrate", json={"goal": "Tag links for the launch", "agent": "utm_generation", "inputs": {"url": DEMO}}).json()
    assert v["status"] == "completed"
    assert client.get(f"/runs/{v['run_id']}").json()["run_id"] == v["run_id"]
    art = v["steps"][0]["result"]["artifacts"][0]["path"]
    assert client.get(f"/runs/{v['run_id']}/artifacts/{art.split('/', 1)[1]}").status_code == 200


def test_approvals_endpoint(client):
    v = client.post("/orchestrate", json={"goal": "Invite homeowners to a flood webinar", "agent": "email_creation"}).json()
    assert v["status"] == "awaiting_approval"
    done = client.post(f"/runs/{v['run_id']}/approvals", json={"decisions": {"*": "approve"}}).json()
    assert done["status"] == "completed"
    assert all(a["status"] == "done" for a in done["steps"][0]["result"]["pending_actions"])


def test_a2a_card_and_message(client):
    card = client.get("/.well-known/agent-card.json").json()
    assert card["skills"] and card["preferredTransport"] == "JSONRPC"
    body = {"jsonrpc": "2.0", "id": 1, "method": "message/send",
            "params": {"message": {"role": "user", "messageId": "m1", "parts": [{"kind": "text", "text": "Create UTM links for our email newsletter"}],
                                   }}}
    res = client.post("/a2a", json=body).json()["result"]
    assert res["kind"] == "task" and res["status"]["state"] in ("completed", "input-required")


def test_mcp_lists_every_agent_as_a_tool(client):
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
    assert client.post("/mcp/", json=init, headers=MCP_HEADERS).status_code == 200
    tools = client.post("/mcp/", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, headers=MCP_HEADERS).json()["result"]["tools"]
    names = {t["name"] for t in tools}
    from caf.core import all_agents

    assert {f"run_{a.spec.id}" for a in all_agents()} <= names
    assert {"orchestrate", "approve_actions", "list_agents"} <= names


def test_shortlink_redirect_records_scans(client):
    from caf.config import get_settings
    from caf.connectors import ConnectorHub

    links = ConnectorHub(get_settings()).get("shortlink")
    made = links.shorten(DEMO + "?utm_source=print&utm_medium=qr")
    r = client.get(f"/r/{made['code']}", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith(DEMO)
    assert client.get(f"/r/{made['code']}/stats").json()["scans"] >= 1
