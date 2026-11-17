"""Security policy: service surfaces cannot reach private networks or arbitrary local files, the API needs a
token for remote clients, run ids and file names cannot escape their folders, and approval gates hold."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

DEMO = "https://www.meridian-mutual.example/products/home-insurance.html"


@pytest.fixture()
def service():
    from caf.config import get_settings
    from caf.core.security import service_settings

    return service_settings(get_settings())


@pytest.fixture()
def secret_file(tmp_path):
    f = tmp_path / "secret.env"
    f.write_text("API_KEY=super-secret-value\n", encoding="utf-8")
    return f


# ----------------------------------------------------------------------------------- policy
@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8080/", "http://10.0.0.5/admin", "http://169.254.169.254/latest/meta-data/", "http://[::1]/",
    "http://localhost/", "http://metadata.google.internal/computeMetadata/v1/", "http://100.64.1.1/", "http://0.0.0.0/",
    "http://[::ffff:127.0.0.1]/", "file:///etc/passwd", "ftp://example.com/x", "gopher://example.com/",
])
def test_service_policy_blocks_private_and_odd_urls(service, url):
    from caf.core.security import Policy, PolicyError

    with pytest.raises(PolicyError):
        Policy(service).check_url(url)


def test_service_policy_allows_public_and_fixture_hosts(service):
    from caf.core.security import Policy

    pol = Policy(service)
    assert pol.url_allowed(DEMO)  # fixture host: served from disk
    assert pol.url_allowed("https://93.184.216.34/")  # a public address literal
    assert pol.path_allowed("sample_data/webinar")
    assert not pol.path_allowed("/etc/passwd")
    assert not pol.path_allowed("config/../.env")


def test_local_mode_is_the_operator(secret_file):
    from caf.config import get_settings
    from caf.core.security import Policy

    pol = Policy(get_settings())
    assert pol.url_allowed("http://127.0.0.1:8080/") and pol.path_allowed(secret_file)


def test_allowlist_and_private_network_overrides(service):
    from caf.core.security import Policy

    s = service.model_copy(update={"security": {**service.security, "allowed_hosts": "*.client.example", "allow_private_network": "true"}})
    pol = Policy(s)
    assert pol.url_allowed("http://staging.client.example/") and pol.url_allowed(DEMO)
    assert not pol.url_allowed("https://www.other.example/")


def test_run_ids_cannot_name_other_folders():
    from caf.core import RunContext
    from caf.core.security import PolicyError
    from caf.orchestrator import Orchestrator

    for bad in ("..", ".", "../x", "a/b", "%2e", "x" * 200, ""):
        if bad:
            with pytest.raises(PolicyError):
                RunContext.create(bad)
    orch = Orchestrator()
    with pytest.raises(KeyError):
        orch.view("..")
    with pytest.raises(KeyError):
        orch.resume("run-does-not-exist", {"*": "approve"})


# ----------------------------------------------------------------------------------- connectors
def test_ingestion_respects_the_policy(service, secret_file):
    from caf.connectors import ConnectorHub
    from caf.connectors.base import ConnectorError
    from caf.core.security import PolicyError

    hub = ConnectorHub(service)
    ing = hub.get("ingestion")
    with pytest.raises(ConnectorError):
        ing.load(str(secret_file))
    with pytest.raises(ConnectorError):
        ing.load(secret_file.as_uri())
    with pytest.raises(PolicyError):
        ing.load(path=str(secret_file))
    with pytest.raises(ConnectorError):
        ing.load("http://127.0.0.1:9/secret")
    assert "Meridian" in ing.load("sample_data/webinar/transcript.txt").text or ing.load("sample_data/webinar/transcript.txt").text

    from caf.config import get_settings

    local = ConnectorHub(get_settings()).get("ingestion")
    assert "super-secret-value" in local.load(str(secret_file)).text  # the CLI operator may read their own files


def test_crawler_checks_every_redirect_hop(service):
    from caf.connectors import ConnectorHub

    crawler = ConnectorHub(service).get("crawler")
    assert crawler.fetch(DEMO).ok
    blocked = crawler.fetch("http://127.0.0.1:9/")
    assert blocked.status == 0 and blocked.error.startswith("blocked by security policy")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "93.184.216.34":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})
        return httpx.Response(200, text="metadata secret")

    crawler._client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    page = crawler.fetch("http://93.184.216.34/start")
    assert not page.ok and "blocked by security policy" in (page.error or "")
    assert page.redirects == [("http://93.184.216.34/start", 302)] and page.html == ""


def test_browser_refuses_forbidden_targets(service):
    from caf.connectors import ConnectorHub
    from caf.core.security import PolicyError

    crawler = ConnectorHub(service).get("crawler")
    for target in ("file:///etc/passwd", "http://127.0.0.1:9/"):
        with pytest.raises(PolicyError):
            crawler.with_page(lambda pg: pg.content(), url=target)
    with pytest.raises(PolicyError):
        crawler.with_page(lambda pg: pg.content(), html="<p>x</p>", base_url="http://10.1.2.3/")


def test_file_cms_names_stay_inside_its_folder(tmp_path):
    from caf.config import get_settings
    from caf.connectors import ConnectorHub

    cms = ConnectorHub(get_settings()).get("cms", "file")
    res = cms.publish(title="Flood cover", body="# Hi", slug="../../../../" + str(tmp_path / "evil"), locale="../../x")
    out = cms.out_dir.resolve()
    written = [p for p in out.iterdir() if p.is_file()]
    assert written and all(p.resolve().parent == out for p in written)
    assert not list(tmp_path.glob("evil*")) and "/" not in res["id"].split(":", 1)[1]


def test_experiment_ids_cannot_traverse():
    from caf.config import get_settings
    from caf.connectors import ConnectorHub

    exps = ConnectorHub(get_settings()).get("experiments", "json")
    with pytest.raises(ValueError):
        exps.results("../../keywords/gsc_queries")
    assert exps.results("exp-hero-cta").shape[0] > 0


# ----------------------------------------------------------------------------------- API
@pytest.fixture()
def api(monkeypatch):
    monkeypatch.delenv("CAF_API_TOKEN", raising=False)
    monkeypatch.delenv("CAF_CORS_ORIGINS", raising=False)
    from caf.api.server import create_app

    return create_app()


REMOTE = ("203.0.113.9", 40000)  # TestClient without `with`: no lifespan, so one app serves several clients


def test_api_without_token_serves_loopback_only(api):
    assert TestClient(api).get("/agents").status_code == 200
    remote = TestClient(api, client=REMOTE)
    r = remote.get("/agents")
    assert r.status_code == 401 and "CAF_API_TOKEN" in r.json()["detail"]
    assert remote.get("/health").status_code == 200  # public
    assert remote.get("/.well-known/agent-card.json").status_code == 200
    assert remote.get("/r/unknown-code").status_code == 404  # short links stay public (QR codes)
    assert remote.get("/r/unknown-code/stats").status_code == 401
    assert remote.post("/mcp/", json={}).status_code == 401
    assert remote.post("/a2a", json={"jsonrpc": "2.0", "id": 1, "method": "tasks/get", "params": {"id": "x"}}).status_code == 401


def test_api_token(api, monkeypatch):
    monkeypatch.setenv("CAF_API_TOKEN", "s3cret-token")
    remote = TestClient(api, client=REMOTE)
    assert remote.get("/agents").status_code == 401
    assert remote.get("/agents", headers={"authorization": "Bearer wrong"}).status_code == 401
    assert remote.get("/agents", headers={"authorization": "Bearer s3cret-token"}).status_code == 200
    assert remote.get("/agents", headers={"x-api-key": "s3cret-token"}).status_code == 200
    assert TestClient(api).get("/agents").status_code == 401  # with a token configured, loopback needs it too


def test_api_has_no_wildcard_cors(api):
    with TestClient(api) as c:
        r = c.get("/agents", headers={"origin": "https://evil.example"})
        assert "access-control-allow-origin" not in {k.lower() for k in r.headers}


def test_api_cors_allowlist(monkeypatch):
    monkeypatch.setenv("CAF_CORS_ORIGINS", "https://console.client.example")
    from caf.api.server import create_app

    with TestClient(create_app()) as c:
        ok = c.options("/agents", headers={"origin": "https://console.client.example", "access-control-request-method": "GET"})
        assert ok.headers.get("access-control-allow-origin") == "https://console.client.example"
        bad = c.get("/agents", headers={"origin": "https://evil.example"})
        assert "access-control-allow-origin" not in {k.lower() for k in bad.headers}


def test_api_runs_cannot_escape_their_folder(api):
    with TestClient(api) as c:
        for rid in (".", "%2e", "..", "%2e%2e"):
            assert c.get(f"/runs/{rid}/artifacts/_store/caf.sqlite").status_code == 404
            assert c.get(f"/runs/{rid}/trace").status_code == 404


def test_api_agent_inputs_cannot_read_local_files(api, secret_file):
    with TestClient(api) as c:
        for agent, inputs in [("content_summary", {"source": str(secret_file)}), ("content_summary", {"path": str(secret_file)}),
                              ("content_summary", {"source": "/etc/passwd"}), ("quote_extraction", {"source": str(secret_file)})]:
            body = c.post(f"/agents/{agent}/run", json={"inputs": inputs}).json()
            dump = json.dumps(body)
            assert "super-secret-value" not in dump and "root:x:0:0" not in dump
            assert body["status"] == "error"


def test_api_auto_approve_needs_operator_consent(api, monkeypatch):
    with TestClient(api) as c:
        r = c.post("/agents/press_release_creation/run", json={"options": {"auto_approve": True}})
        assert r.status_code == 403
        r = c.post("/orchestrate", json={"goal": "Announce the flood cover", "agent": "press_release_creation", "auto_approve": True})
        assert r.status_code == 403
        held = c.post("/agents/press_release_creation/run", json={}).json()
        assert held["status"] == "needs_approval" and all(a["status"] == "pending" for a in held["pending_actions"])
    monkeypatch.setenv("CAF_ALLOW_REQUEST_AUTO_APPROVE", "true")
    from caf.api.server import create_app

    with TestClient(create_app()) as c:
        done = c.post("/agents/press_release_creation/run", json={"options": {"auto_approve": True}}).json()
        assert any(a["status"] == "done" for a in done["pending_actions"])


def test_mcp_tools_cannot_switch_gates_off():
    from caf.api.mcp_server import OrchestrateArgs

    assert "auto_approve" not in OrchestrateArgs.model_fields


# ----------------------------------------------------------------------------------- contracts + routing
def test_input_schemas_carry_factory_defaults():
    from caf.core import all_agents, get_agent

    props = get_agent("content_translation").describe()["input_schema"]["properties"]
    assert props["target_locales"]["default"] == ["fr-FR"]
    for agent in all_agents():
        schema = agent.describe()["input_schema"].get("properties", {})
        for name, field in agent.Input.model_fields.items():
            if field.default_factory is not None:
                assert "default" in schema.get(name, {}), f"{agent.spec.id}.{name}"


def test_vague_goal_asks_for_clarification_instead_of_guessing():
    from caf.orchestrator import Orchestrator

    view = Orchestrator().start("Help us do better marketing")
    assert view["status"] == "needs_clarification" and view["run_id"] is None and not view["steps"]
    assert view["final"]["next_steps"] and view["suggestions"]
    ok = Orchestrator().start("What did the August traffic drop look like?")
    assert ok["status"] != "needs_clarification" and ok["steps"][0]["agent"] == "ga4_traffic_report"
