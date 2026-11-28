"""Each customer's own systems.

A customer connects its own analytics, CMS, e-mail platform and the rest (its policy's `connections`); its runs reach
those and no other customer's, and never a system the server is connected to for its operator. Credentials are stored
encrypted and are not shown again. What the policy lets agents do with each kind of system (`reach.systems`) is enforced.
What the layer itself keeps between runs (records, the index, drafts, short links) is kept apart per customer, and a
customer's run reads only its own files under runs/.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

ADMIN = {"Authorization": "Bearer admin-t0ken"}
AGENTS = ["blog_post_creation", "utm_generation", "quote_extraction"]
CONTENTFUL = {"provider": "contentful", "space_id": "sp1", "management_token": "cma-acme-secret", "content_type_map": {"article": "blogPost"}}


class FakeContentful:
    """The two calls a publish makes: read the entry (not there yet), write it."""

    def __init__(self):
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.calls.append(request)
        if request.method == "GET":
            return httpx.Response(404, json={"sys": {"id": "NotFound"}})
        return httpx.Response(201, json={"sys": {"id": "entry-1", "version": 1}, "fields": json.loads(request.content)["fields"]})


@pytest.fixture()
def env(monkeypatch, tmp_path):
    from caf.config import reset_settings_cache
    from caf.shop import reset_shops

    monkeypatch.setenv("CAF_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CAF_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("CAF_LLM_PROVIDER", "offline")
    monkeypatch.setenv("CAF_API_TOKEN", "admin-t0ken")
    monkeypatch.setenv("CAF_SECRET_KEY", "a passphrase for the shop's vault")
    for name in ("CAF_CMS_PROVIDER", "CAF_SHARED_CONNECTORS", "CAF_SEARCH_PROVIDER"):
        monkeypatch.delenv(name, raising=False)
    reset_settings_cache()
    reset_shops()
    yield tmp_path
    reset_settings_cache()
    reset_shops()


@pytest.fixture()
def client(env):
    from caf.api.server import create_app

    with TestClient(create_app()) as c:
        yield c


@pytest.fixture()
def contentful(monkeypatch):
    """A customer's own Contentful answered by a stand-in: the settings a connector is given are the real ones, as they
    come out of the customer's stored policy, with the transport swapped for the stand-in."""
    from caf.connectors import ConnectorHub

    fake = FakeContentful()
    real = ConnectorHub._resolve

    def resolved(self, kind):
        cfg, owner = real(self, kind)
        if owner == "customer" and cfg.get("provider") == "contentful":
            cfg = {**cfg, "_transport": httpx.MockTransport(fake), "retry_backoff_s": 0}
        return cfg, owner

    monkeypatch.setattr(ConnectorHub, "_resolve", resolved)
    return fake


def shop():
    from caf.config import get_settings
    from caf.shop import get_shop

    return get_shop(get_settings())


def customer(client, cid: str, agents: list[str] = AGENTS) -> dict[str, str]:
    assert client.post("/shop/admin/api/customers", json={"id": cid, "name": cid.title()}, headers=ADMIN).status_code == 201
    subs = [{"kind": "agent", "item_id": a} for a in agents]
    assert client.put(f"/shop/admin/api/customers/{cid}/subscriptions", json=subs, headers=ADMIN).status_code == 200
    return {"Authorization": "Bearer " + client.post(f"/shop/admin/api/customers/{cid}/keys", json={"label": "t"}, headers=ADMIN).json()["key"]}


def system(client, cid: str, kind: str, **body):
    return client.put(f"/shop/admin/api/customers/{cid}/systems/{kind}", json=body, headers=ADMIN)


def row(client, cid: str, kind: str) -> dict:
    return next(r for r in client.get(f"/shop/admin/api/customers/{cid}/systems", headers=ADMIN).json() if r["kind"] == kind)


def settled(client, view: dict, headers: dict) -> dict:
    for _ in range(200):
        if view.get("status") not in ("queued", "running"):
            return view
        time.sleep(0.2)
        view = client.get(f"/runs/{view['run_id']}", headers=headers).json()
    raise AssertionError(f"run did not settle: {view.get('status')}")


def write_a_post(client, headers: dict) -> dict:
    """A run that ends by asking to publish to the CMS (the action is held for approval)."""
    sent = client.post("/orchestrate", json={"goal": "Write a post about flood cover", "agent": "blog_post_creation",
                                             "inputs": {"topic": "Flood cover basics"}, "wait_s": 60}, headers=headers)
    assert sent.status_code in (200, 202), sent.text
    return settled(client, sent.json(), headers)


def decide(client, view: dict, headers: dict, decision: str = "approve") -> dict:
    done = client.post(f"/runs/{view['run_id']}/approvals", json={"decisions": {"*": decision}, "wait_s": 60}, headers=headers)
    assert done.status_code in (200, 202), done.text
    return settled(client, done.json(), headers)


def the_action(view: dict) -> dict:
    return view["steps"][0]["result"]["pending_actions"][0]


# ------------------------------------------------------------------------------------------------ connecting
def test_a_connection_is_stored_encrypted_and_never_shown_again(client):
    acme = customer(client, "acme")
    saved = system(client, "acme", "cms", connection=CONTENTFUL)
    assert saved.status_code == 200, saved.text
    assert saved.json()["serves"] == "customer" and saved.json()["ready"] and saved.json()["connection"]["management_token"] == "***"

    stored = shop().store.get_customer("acme")["policy"]["connections"]["cms"]
    assert stored["management_token"].startswith("enc:v1:") and stored["space_id"] == "sp1"       # what is not a secret stays readable
    everywhere = json.dumps([client.get("/shop/admin/api/customers/acme", headers=ADMIN).json(), client.get("/shop/admin/api/customers", headers=ADMIN).json(),
                             client.get("/shop/admin/api/customers/acme/systems", headers=ADMIN).json(), client.get("/shop/me", headers=acme).json(),
                             client.get("/connectors", headers=acme).json()])
    assert "cma-acme-secret" not in everywhere and stored["management_token"] not in everywhere
    mine = next(s for s in client.get("/shop/me", headers=acme).json()["systems"] if s["kind"] == "cms")
    assert (mine["serves"], mine["provider"], mine["connection"]["management_token"]) == ("customer", "contentful", "***")

    # the policy read, edited and saved back keeps the credential it shows as ***; so does an edit of the connection
    policy = client.get("/shop/admin/api/customers/acme", headers=ADMIN).json()["policy"]
    policy["limits"]["runs_per_day"] = 50
    assert client.put("/shop/admin/api/customers/acme/policy", json=policy, headers=ADMIN).status_code == 200
    assert system(client, "acme", "cms", connection={**CONTENTFUL, "management_token": "***", "environment": "staging"}).status_code == 200
    from caf.core import vault

    again = shop().store.get_customer("acme")["policy"]["connections"]["cms"]
    assert vault.decrypt(again["management_token"]) == "cma-acme-secret" and again["environment"] == "staging"
    # the mask keeps a credential only for the same product: another product's has to be given
    other = system(client, "acme", "cms", connection={"provider": "wordpress", "base_url": "https://blog.acme.example", "username": "bot", "application_password": "***"})
    assert other.status_code == 400 and "nothing is stored" in other.json()["detail"]

    # an address that carries a password is a credential too, whatever the setting is called
    warehouse = {"provider": "sqlalchemy", "url": "postgresql://reader:s3cret-pw@db.acme.example:5432/marketing", "schema": "public"}
    assert system(client, "acme", "warehouse", connection=warehouse).json()["connection"] == {**warehouse, "url": "***"}
    assert system(client, "acme", "warehouse", connection={**warehouse, "url": "***", "schema": "reporting"}).status_code == 200
    kept = shop().store.get_customer("acme")["policy"]["connections"]["warehouse"]
    assert kept["url"].startswith("enc:v1:") and vault.decrypt(kept["url"]) == warehouse["url"] and kept["schema"] == "reporting"
    assert "s3cret-pw" not in json.dumps(client.get("/shop/admin/api/customers/acme", headers=ADMIN).json())

    # a credential can stay out of the database: an environment variable on the server, named for this customer
    assert system(client, "acme", "esp", connection={"provider": "braze", "api_key_env": "ACME_BRAZE_KEY", "rest_endpoint": "https://rest.iad-01.braze.com"}).json()["connection"] == {
        "provider": "braze", "api_key_env": "ACME_BRAZE_KEY", "rest_endpoint": "https://rest.iad-01.braze.com"}
    # an incomplete connection is saved and says what it still needs
    jira = system(client, "acme", "tasks", connection={"provider": "jira", "base_url": "https://acme.atlassian.net"}).json()
    assert (jira["ready"], jira["needs"]) == (False, ["email", "api_token"])

    gone = client.delete("/shop/admin/api/customers/acme/systems/cms", headers=ADMIN).json()
    assert (gone["connected"], gone["serves"], gone["provider"]) == (False, "local", "file")
    assert "cms" not in shop().store.get_customer("acme")["policy"]["connections"]


def test_what_is_not_a_customers_to_connect_is_refused(client, monkeypatch):
    customer(client, "acme")
    monkeypatch.setenv("ACME_KEY", "x")

    def refused(kind, **body) -> str:
        answer = system(client, "acme", kind, **body)
        assert answer.status_code == 400, (kind, body, answer.text)
        return answer.json()["detail"]

    assert "works on this server's own files" in refused("cms", connection={"provider": "file"})
    assert "A customer can connect: aem, contentful, wordpress" in refused("cms", connection={"provider": "drupal"})
    assert "no 'billing' kind" in refused("billing", connection={"provider": "stripe", "api_key": "k"})
    assert "reads files on the server" in refused("cms", connection={**CONTENTFUL, "path": "../.env"})
    assert "credentials_json" in refused("analytics", connection={"provider": "ga4", "property_id": "1", "credentials_file": "/etc/passwd"})
    assert "reads files on the server" in refused("cms", connection={**CONTENTFUL, "_transport": "x"})
    assert "reads files on the server" in refused("warehouse", connection={"provider": "sqlalchemy", "url": "postgresql://db.acme.example/x", "engine_options": {}})
    # a secret that is not this customer's: the server's own, another customer's, any name that does not start with its id
    for name in ("CAF_API_TOKEN", "CAF_SECRET_KEY", "GLOBEX_CONTENTFUL_TOKEN", "ANTHROPIC_API_KEY"):
        assert "starts with ACME_" in refused("cms", connection={"provider": "contentful", "space_id": "a", "management_token_env": name})
    # an address that is this server, its network or a cloud's internal service; a database that is a file on this server
    assert "not a public address" in refused("cms", connection={"provider": "aem", "base_url": "http://169.254.169.254/latest", "parent_path": "/content"})
    assert "internal name" in refused("cms", connection={"provider": "wordpress", "base_url": "http://localhost:8080", "username": "u", "application_password": "p"})
    assert "not a public address" in refused("vectorstore", connection={"provider": "qdrant", "url": "http://10.0.0.7:6333"})
    assert "database file on this server" in refused("warehouse", connection={"provider": "sqlalchemy", "url": "sqlite:///data/shop.db"})
    assert "none, read, read_write" in refused("cms", access="write")
    assert "neither a kind" in refused("teleport", access="read")
    assert shop().store.get_customer("acme")["policy"].get("connections", {}) == {}      # nothing of the refused was kept

    # the same rules when the whole policy is saved, and for the model key
    bad = client.put("/shop/admin/api/customers/acme/policy", json={"models": {"provider": "openai", "api_key_env": "CAF_API_TOKEN", "tiers": {"balanced": "gpt-5"}},
                                                                   "connections": {"cms": {"provider": "file"}}}, headers=ADMIN)
    assert bad.status_code == 400 and "this layer's own secret" in bad.json()["detail"] and "connections.cms" in bad.json()["detail"]
    assert system(client, "acme", "cms", connection={"provider": "contentful", "space_id": "a", "management_token_env": "ACME_KEY"}).status_code == 200


def test_a_model_key_shown_as_the_mask_is_kept_when_the_policy_is_saved_back(client):
    from caf.core import vault

    customer(client, "acme")
    own = {"models": {"provider": "openai", "api_key": "sk-acme-model-key", "tiers": {"balanced": "gpt-5"}}}
    assert client.put("/shop/admin/api/customers/acme/policy", json=own, headers=ADMIN).json()["policy"]["models"]["api_key"] == "***"
    shown = client.get("/shop/admin/api/customers/acme", headers=ADMIN).json()["policy"]
    shown["limits"]["runs_per_day"] = 10
    assert client.put("/shop/admin/api/customers/acme/policy", json=shown, headers=ADMIN).status_code == 200      # what the Policy tab does
    stored = shop().store.get_customer("acme")["policy"]
    assert vault.decrypt(stored["models"]["api_key"]) == "sk-acme-model-key" and stored["limits"]["runs_per_day"] == 10
    fresh = customer(client, "globex") and client.put("/shop/admin/api/customers/globex/policy", json=own | {"models": {**own["models"], "api_key": "***"}}, headers=ADMIN)
    assert fresh.status_code == 400 and "nothing is stored" in fresh.json()["detail"]                              # a mask with nothing behind it is not a key


# ------------------------------------------------------------------------------------------------ whose system a run reaches
def test_a_customers_run_reaches_its_own_system_and_no_other_customers(client, contentful, env):
    acme, globex = customer(client, "acme"), customer(client, "globex")
    assert system(client, "acme", "cms", connection=CONTENTFUL).status_code == 200

    held = write_a_post(client, acme)
    assert held["status"] == "awaiting_approval" and held["pending_approvals"][0]["kind"] == "cms.publish" and not contentful.calls
    done = the_action(decide(client, held, acme))
    assert done["status"] == "done" and done["result"]["via"] == {"system": "cms:contentful", "owner": "customer"}
    assert {str(c.url).split("/entries/")[0] for c in contentful.calls} == {"https://api.contentful.com/spaces/sp1/environments/master"}
    assert {c.headers["authorization"] for c in contentful.calls} == {"Bearer cma-acme-secret"}       # the credential acme gave, decrypted for the call
    assert not (env / "runs" / "_customers" / "acme" / "_cms").exists()                              # and nothing was left on this server

    # the other customer's same run reaches nothing of acme's: it is written to its own folder here
    before = len(contentful.calls)
    theirs = the_action(decide(client, write_a_post(client, globex), globex))
    assert theirs["status"] == "done" and theirs["result"]["via"] == {"system": "cms:file", "owner": "layer"} and len(contentful.calls) == before
    assert sorted(p.name for p in (env / "runs" / "_customers" / "globex" / "_cms").iterdir()) == ["flood-cover-basics.en.json", "flood-cover-basics.en.md"]
    assert not (env / "runs" / "_cms").exists()

    # the agent check and a flow test try agents on the customer's model with sample inputs: they do not call its systems
    s = shop()
    check = s.context_for(s.store.get_customer("acme"), surface="check").connectors
    assert (check.get("cms").provider, check.tenant) == ("file", "acme") and s.context_for(s.store.get_customer("acme"), surface="mcp").connectors.get("cms").provider == "contentful"

    # what each sees of its systems is its own
    kinds = lambda headers: {c["kind"]: (c["provider"], c["owner"]) for c in client.get("/connectors", headers=headers).json()}  # noqa: E731
    assert kinds(acme)["cms"] == ("contentful", "customer") and kinds(globex)["cms"] == ("file", "layer")
    assert "owner" not in client.get("/connectors", headers=ADMIN).json()[0]                           # the operator's is the server's own, as before


def test_a_system_the_server_is_connected_to_is_the_operators_not_a_customers(env, monkeypatch):
    from caf.config import load_settings
    from caf.connectors import ConnectorHub, ConnectorNotConfigured
    from caf.shop.policy import load_policy

    monkeypatch.setenv("CAF_CMS_PROVIDER", "contentful")           # the operator's own Contentful, in the server's settings
    monkeypatch.setenv("CONTENTFUL_SPACE_ID", "operator-space")
    monkeypatch.setenv("CONTENTFUL_MANAGEMENT_TOKEN", "operator-token")
    monkeypatch.setenv("CAF_SEARCH_PROVIDER", "brave")              # and a search account the operator pays for
    monkeypatch.setenv("SEARCH_API_KEY", "operator-search-key")
    settings = load_settings()

    def hub_for(customer_id=None, connections=None):
        policy = load_policy({"connections": connections or {}})
        return ConnectorHub(policy.settings_for(settings, customer_id) if customer_id else settings)

    operator = hub_for()
    assert (operator.get("cms").provider, operator.get("cms").config["management_token"]) == ("contentful", "operator-token")

    theirs = hub_for("acme")
    cms = theirs.get("cms")
    assert (cms.provider, cms.owner, cms.external) == ("file", "layer", False) and "operator" not in json.dumps(cms.config)
    search = theirs.get("search")                                   # a service of the layer's: the server's serves every customer
    assert (search.provider, search.owner, search.config["api_key"]) == ("brave", "layer", "operator-search-key")
    analytics = theirs.get("analytics")                             # only demo data on the server: read, and marked as demo by the run
    assert (analytics.provider, analytics.owner) == ("csv", "layer") and theirs._is_bundled("analytics", analytics.config)
    with pytest.raises(ConnectorNotConfigured):                     # the operator's product asked for by name
        theirs.get("cms", "contentful")

    own = hub_for("acme", {"cms": {"provider": "wordpress", "base_url": "https://blog.acme.example", "username": "bot", "application_password": "pw"}})
    assert (own.get("cms").provider, own.get("cms").owner, own.get("cms").config["username"]) == ("wordpress", "customer", "bot")

    # the operator may share a kind, everything (the image in the customer's own cloud), or nothing
    for shared, cms_provider, search_provider in (("cms", "contentful", "none"), ("all", "contentful", "brave"), ("none", "file", "none")):
        monkeypatch.setenv("CAF_SHARED_CONNECTORS", shared)
        settings = load_settings()
        hub = hub_for("acme")
        assert (hub.get("cms").provider, hub.get("search").provider) == (cms_provider, search_provider), shared


def test_a_credential_that_cannot_be_read_stops_that_connector_only(client, monkeypatch):
    acme = customer(client, "acme")
    assert system(client, "acme", "cms", connection=CONTENTFUL).status_code == 200
    monkeypatch.setenv("CAF_SECRET_KEY", "a different key than the one it was encrypted with")
    seen = row(client, "acme", "cms")
    assert seen["serves"] == "customer" and "different CAF_SECRET_KEY" in seen["error"]
    link = client.post("/agents/utm_generation/run", json={"inputs": {"base_url": "https://www.acme.example/", "campaign": "2026q4_us_fares_launch", "channels": ["email"]}}, headers=acme)
    assert link.status_code == 200 and link.json()["status"] == "ok"        # a run that does not touch the CMS is not held up by it


# ------------------------------------------------------------------------------------------------ what agents may do
def test_what_the_policy_lets_agents_do_with_a_system_is_enforced(client, contentful):
    from caf.connectors import ConnectorNotAllowed

    acme = customer(client, "acme")
    assert system(client, "acme", "cms", connection=CONTENTFUL, access="read").json()["access"] == "read"

    # read only: the publish is turned down when the agent proposes it, with the reason, and nobody is asked to approve it
    view = write_a_post(client, acme)
    result = view["steps"][0]["result"]
    assert view["status"] == "completed" and not view["pending_approvals"] and not contentful.calls
    assert the_action(view)["status"] == "rejected" and "not write to it" in the_action(view)["result"]["refused"]
    assert any("was not done" in w and "reach.systems: cms: read" in w for w in result["warnings"])

    # an approval does not open what the policy closes: allowed when held, closed before the decision
    assert system(client, "acme", "cms", access="read_write").json()["access"] == "read_write"
    held = write_a_post(client, acme)
    assert held["status"] == "awaiting_approval"
    assert system(client, "acme", "cms", access="read").status_code == 200
    late = the_action(decide(client, held, acme))
    assert late["status"] == "failed" and "not write to it" in late["result"]["error"] and not contentful.calls

    # none: the system is closed to agents altogether, and the account's view says so
    assert system(client, "acme", "cms", access="none").json()["serves"] == "closed"
    hub = shop().context_for(shop().store.get_customer("acme"), surface="api").connectors
    with pytest.raises(ConnectorNotAllowed, match="keeps agents from the cms system"):
        hub.get("cms")
    assert hub.get("search").provider                                                     # the kinds it did not close stay open
    # the policy may name the product in place of the kind, and a word this layer does not know opens nothing
    from caf.connectors import ConnectorHub
    from caf.config import get_settings
    from caf.shop.policy import load_policy

    named = ConnectorHub(load_policy({"reach": {"systems": {"csv": "read", "esp": "sometimes"}}}).settings_for(get_settings(), "acme"))
    assert named.access("analytics", "csv") == "read" and named.access("esp") == "none" and named.access("tasks") == "read_write"
    assert ConnectorHub(get_settings()).access("cms") == "read_write"                    # the operator's own runs are not under a customer's policy


# ------------------------------------------------------------------------------------------------ what the layer keeps, kept apart
def test_what_the_layer_keeps_between_runs_is_kept_apart_per_customer(client, env):
    from caf.config import get_settings
    from caf.connectors import ConnectorError, ConnectorHub

    customer(client, "acme"), customer(client, "globex")
    s = shop()
    acme = s.context_for(s.store.get_customer("acme"), surface="api").connectors
    globex = s.context_for(s.store.get_customer("globex"), surface="api").connectors
    operator = ConnectorHub(get_settings())

    # records: what one customer's agents learnt is not read by another's, nor by the operator's
    acme.get("store").put("learnings", {"id": "L1", "finding": "Acme: short forms convert"}, key="L1")
    assert acme.get("store").query("learnings") and globex.get("store").query("learnings") == [] and operator.get("store").query("learnings") == []
    assert (env / "runs" / "_customers" / "acme" / "_store" / "caf.sqlite").is_file() and acme.get("store").partition == "acme"

    # the index
    acme.get("vectorstore").upsert("corpus", [{"id": "d1", "text": "acme pricing playbook"}])
    assert acme.get("vectorstore").count("corpus") == 1 and globex.get("vectorstore").count("corpus") == 0 and operator.get("vectorstore").count("corpus") == 0
    assert globex.get("vectorstore").search("corpus", "pricing") == []

    # the experiment registry: the bundled ones are everyone's to read, a customer's own are that customer's
    bundled = {e.id for e in operator.get("experiments").list()}
    acme.get("experiments").upsert({"id": "acme-exp-1", "name": "Acme hero test"})
    assert {e.id for e in acme.get("experiments").list()} == bundled | {"acme-exp-1"} and {e.id for e in globex.get("experiments").list()} == bundled

    # short links: one table, because a code has to resolve for whoever opens it; a link each for the same address
    url = "https://www.example.com/landing?utm_source=print"
    a, g, o = (hub.get("shortlink").shorten(url) for hub in (acme, globex, operator))
    assert len({a["code"], g["code"], o["code"]}) == 3 and acme.get("shortlink").shorten(url)["code"] == a["code"]
    assert all(operator.get("shortlink").resolve(code) == url for code in (a["code"], g["code"], o["code"]))
    assert client.get(f"/r/{a['code']}", follow_redirects=False).headers["location"] == url
    with pytest.raises(ConnectorError, match="already in use"):                              # nobody takes over a code that is someone else's
        globex.get("shortlink").shorten("https://evil.example/", code=a["code"])


def test_a_customers_run_reads_only_its_own_files_under_runs(client, env):
    from caf.config import get_settings
    from caf.core.security import Policy, PolicyError, service_settings

    acme, globex = customer(client, "acme"), customer(client, "globex")
    link = {"inputs": {"base_url": "https://www.acme.example/", "campaign": "2026q4_us_fares_launch", "channels": ["email"]}}
    mine = client.post("/agents/utm_generation/run", json=link, headers=acme).json()["run_id"]
    theirs = client.post("/agents/utm_generation/run", json=link, headers=globex).json()["run_id"]
    for folder in (f"runs/{mine}", f"runs/{theirs}", "runs/_customers/acme/_cms", "runs/_customers/globex/_cms", "runs/_store", "runs/_cms"):
        (env / folder).mkdir(parents=True, exist_ok=True)
        (env / folder / "note.md").write_text('The CEO said: "Our flood cover is the broadest in the market," at the launch on Tuesday.')

    s = shop()
    policy = Policy(s.settings_for(s.store.get_customer("acme")))
    runs = env / "runs"
    assert policy.check_path(runs / mine / "note.md") and policy.check_path(runs / "_customers" / "acme" / "_cms" / "note.md")
    for other in (runs / theirs / "note.md", runs / "_customers" / "globex" / "_cms" / "note.md", runs / "_store" / "note.md", runs / "_cms" / "note.md",
                  runs, runs / "_customers", runs / mine / ".." / theirs / "note.md"):
        with pytest.raises(PolicyError):
            policy.check_path(other)
    assert policy.check_path("sample_data/site/index.html" if (get_settings().home / "sample_data/site/index.html").exists() else "sample_data")   # the bundled data stays readable
    operator = Policy(service_settings(get_settings()))                                      # the operator's own runs read runs/ as before
    assert operator.check_path(runs / theirs / "note.md") and operator.check_path(runs / "_store" / "note.md")

    # and through an agent: a file of its own run is read, another customer's named as an input is not
    def quotes(path, headers):
        return client.post("/agents/quote_extraction/run", json={"inputs": {"file": str(path)}}, headers=headers).json()

    own = quotes(runs / mine / "note.md", acme)
    assert own["status"] in ("ok", "degraded") and "1 verified quote" in own["summary"]
    other = quotes(runs / theirs / "note.md", acme)
    assert other["status"] == "error" and "not in one of this account's own runs" in other["error"] and not other["output"]
    assert "1 verified quote" in quotes(runs / theirs / "note.md", ADMIN)["summary"]           # the operator reads any run's


# ------------------------------------------------------------------------------------------------ a customer's address is a public one
def test_a_customers_connection_only_ever_calls_a_public_address(env, monkeypatch):
    from caf.config import get_settings
    from caf.connectors import ConnectorError, ConnectorHub
    from caf.core import security
    from caf.core.security import service_settings
    from caf.shop.policy import load_policy

    seen: list[str] = []

    def wordpress(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if "wp-json/wp/v2/posts" in str(request.url) and request.method == "GET":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})   # the vendor's host sends us inside
        return httpx.Response(200, json=[])

    def hub_for(base_url: str) -> ConnectorHub:
        policy = load_policy({"connections": {"cms": {"provider": "wordpress", "base_url": base_url, "username": "bot", "application_password": "pw"}}})
        hub = ConnectorHub(policy.settings_for(service_settings(get_settings()), "acme"))
        hub._resolved["cms"] = ({**hub.config_for("cms")[0], "_transport": httpx.MockTransport(wordpress), "retry_backoff_s": 0}, "customer")
        return hub

    # the name resolves to this server's network when the call is made (it was a public name when the settings were saved)
    monkeypatch.setattr(security, "_resolve", lambda host: ["10.0.0.5"] if host == "blog.acme.example" else ["93.184.216.34"])
    with pytest.raises(ConnectorError, match="not a public address"):
        hub_for("https://blog.acme.example").get("cms").publish(title="t", body="b")
    assert seen == []                                                                         # the request never left
    # a public host that redirects to a cloud's internal service: the redirect is not followed
    with pytest.raises(ConnectorError, match="not a public address"):
        hub_for("https://public.example").get("cms").publish(title="t", body="b")
    assert seen and all("169.254" not in url for url in seen)
    # a database: a server at a public address, never a file here; driver options are the operator's
    for url, why in (("sqlite:////etc/passwd", "database file"), ("postgresql://u:p@localhost/db", "internal name"), ("postgresql://u:p@10.1.2.3/db", "not a public address")):
        with pytest.raises(security.PolicyError, match=why):
            security.Policy(service_settings(get_settings())).check_database(url)
    assert security.Policy(service_settings(get_settings())).check_database("snowflake://u:p@xy12345.eu-central-1/db/schema")
    # the layer's own connections are the operator's to point anywhere, as before
    assert security.Policy(get_settings()).check_endpoint("http://localhost:6333")            # the CLI's own runs


# ------------------------------------------------------------------------------------------------ the command line
def test_the_command_line_shows_and_connects_a_customers_systems(client, env, capsys):
    from caf.cli import main

    customer(client, "acme")
    file = env / "systems.yaml"
    file.write_text("cms:\n  provider: contentful\n  space_id: sp1\n  management_token: cma-acme-secret\n")
    assert main(["shop", "systems", "acme", "--set", str(file), "--access", "cms=read"]) == 0
    out = capsys.readouterr().out
    assert "connected for acme: cms" in out and re.search(r"cms\s+their own: contentful\s+read only", out) and "cma-acme-secret" not in out
    assert re.search(r"analytics\s+demo data: csv\s+read and write\s+adobe_analytics, ga4", out)
    assert main(["shop", "systems", "acme", "--json"]) == 0
    rows = {r["kind"]: r for r in json.loads(capsys.readouterr().out)}
    assert rows["cms"]["connection"]["management_token"] == "***" and rows["cms"]["access"] == "read" and rows["store"]["group"] == "store"
    assert main(["shop", "systems", "acme", "--disconnect", "cms"]) == 0 and "disconnected cms" in capsys.readouterr().out
    file.write_text("cms: {provider: file}\n")
    assert main(["shop", "systems", "acme", "--set", str(file)]) == 2 and "works on this server's own files" in capsys.readouterr().err
    assert Path(env / "data").exists()
