"""The console as a web app on `caf serve`: the page, the host bridge, and model calls answered by the framework's
provider (a local stand-in for a Claude deployment on Microsoft Foundry)."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from test_llm_foundry import KEY, foundry  # noqa: E402,F401  (stand-in Foundry deployment + fixture)


@pytest.fixture(autouse=True)
def _fresh_settings():
    from caf.config import reset_settings_cache

    reset_settings_cache()  # each test builds its app from its own environment
    yield
    reset_settings_cache()


def _client(monkeypatch, token: str | None = None) -> TestClient:
    from caf.api.server import create_app
    from caf.config import reset_settings_cache

    reset_settings_cache()

    if token:
        monkeypatch.setenv("CAF_API_TOKEN", token)
    else:
        monkeypatch.delenv("CAF_API_TOKEN", raising=False)
    return TestClient(create_app())


def test_console_page_and_browser_root(monkeypatch):
    from caf.api.console_host import parse_json_reply

    monkeypatch.setenv("CAF_LLM_PROVIDER", "offline")
    client = _client(monkeypatch, token="t0ken")
    page = client.get("/console")  # public, like / and /health
    assert page.status_code == 200 and "CX Nova Agent Console" in page.text
    assert page.text.index("window.claude = {") < page.text.index('id="caf-boot"')  # the bridge loads first
    root = client.get("/", headers={"accept": "text/html,application/xhtml+xml"}, follow_redirects=False)
    assert root.status_code in (302, 307) and root.headers["location"] == "/shop"  # browsers land on the storefront
    assert client.get("/").json()["service"] == "coforge-agentic-framework"  # API clients keep the JSON index
    assert client.get("/console/host").status_code == 401  # model access needs the token
    host = client.get("/console/host", headers={"Authorization": "Bearer t0ken"}).json()
    assert host["live"] is False and host["provider"] == "offline"
    assert parse_json_reply('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_reply('Here it is: {"a": [1, 2]} - done') == {"a": [1, 2]}


def test_reverse_proxy_prefix(monkeypatch):
    """Behind Caddy at /agentshop: the prefix with and without a trailing slash is the site root, links carry the prefix,
    and the duplicated prefix a prefix-keeping proxy produces is tolerated."""
    from caf.api.server import create_app

    monkeypatch.setenv("CAF_LLM_PROVIDER", "offline")
    monkeypatch.setenv("CAF_API_TOKEN", "t0ken")
    client = TestClient(create_app(), root_path="/agentshop")
    html = {"accept": "text/html,application/xhtml+xml"}
    for path in ("/agentshop", "/agentshop/", "/agentshop/agentshop", "/agentshop/agentshop/"):
        r = client.get(path, headers=html, follow_redirects=False)
        assert r.status_code in (302, 307) and r.headers["location"] == "/agentshop/shop", path
    assert client.get("/agentshop/health").status_code == 200
    page = client.get("/agentshop/shop")
    assert page.status_code == 200 and '"/agentshop/shop/static/shop.css"' in page.text
    assert client.get("/agentshop/agents").status_code == 401  # the API still needs the token under the prefix
    assert client.get("/agentshop/agents", headers={"Authorization": "Bearer t0ken"}).status_code == 200


def test_console_calls_run_on_foundry_with_routing_tier_and_effort(foundry, monkeypatch):  # noqa: F811
    client = _client(monkeypatch)
    host = client.get("/console/host").json()
    assert host == {**host, "provider": "anthropic_foundry", "live": True, "label": "Claude on Microsoft Foundry"}
    assert host["models"]["fast"] == "claude-sonnet-5-5" and host["models"]["pro"] == "claude-opus-5"
    r = client.post("/console/llm", json={"prompt": "What is the capital of France?", "tier": "pro", "model_tier": "default"})
    assert r.json()["text"] == "Paris." and r.json()["model"] == "claude-opus-5"
    body = foundry.requests[-1]
    assert body["model"] == "claude-opus-5" and body["output_config"]["effort"] == "high" and "temperature" not in body
    r = client.post("/console/llm", json={"prompt": "Reply with JSON Schema-valid output", "json": True, "model_tier": "quick"})
    assert r.json()["value"] == {"ok": True} and foundry.requests[-1]["model"] == "claude-sonnet-5-5"
    assert foundry.requests[-1]["output_config"]["effort"] == "low"
    foundry.fail_first = 99  # the deployment keeps refusing: the page gets the console's own rate-limit code
    monkeypatch.setenv("CAF_LLM_RPM", "0")
    r = client.post("/console/llm", json={"prompt": "hi", "tier": "fast"})
    assert r.status_code == 429 and r.json()["code"] == "rate_limited"
