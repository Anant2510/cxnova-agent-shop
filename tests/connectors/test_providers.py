"""External provider adapters: request + response shapes against httpx.MockTransport.

Every adapter is exercised with ``config["_transport"]`` set to a routed MockTransport, so these
tests assert the method / URL / auth / body each vendor expects and that responses map to exactly
the columns and keys the local providers return. Nothing here touches the network and no vendor
credentials exist in this environment: these are contract tests, not live integration tests.
"""

from __future__ import annotations

import base64
import json
import sys
from datetime import date, datetime, timedelta, timezone

import httpx
import numpy as np
import pandas as pd
import pytest

from caf.config import get_settings, load_settings
from caf.connectors import ConnectorHub, ConnectorNotConfigured, load_connectors, providers
from caf.connectors import _http
from caf.connectors.base import ConnectorError, provider_class
from caf.connectors.data import EVENT_COLUMNS, KEYWORD_COLUMNS
from mockapi import Api, basic, body, form, ims_route, no_network

TODAY = datetime.now(timezone.utc).date()
RECENT, YESTERDAY = TODAY - timedelta(days=3), TODAY - timedelta(days=1)  # dates inside every recency window under test


# ----------------------------------------------------------------------------- helpers

@pytest.fixture(scope="module")
def hub():
    return ConnectorHub(get_settings())


def make(hub, kind: str, provider: str, mock: Api | None = None, **config):
    cfg = {"retry_backoff_s": 0, **config}
    if mock is not None:
        cfg["_transport"] = mock.transport
    return provider_class(kind, provider)(cfg, hub)




# ----------------------------------------------------------------------------- registry / contract

EXPECTED = {
    "search": {"serpapi", "brave", "tavily"}, "analytics": {"ga4", "adobe_analytics"}, "keyword_data": {"gsc", "dataforseo"},
    "cms": {"aem", "contentful", "wordpress"}, "esp": {"braze", "mailchimp", "ajo"}, "tasks": {"jira", "azure_devops"},
    "experiments": {"optimizely", "adobe_target"}, "flags": {"launchdarkly"}, "behaviour": {"clarity"}, "webinar": {"zoom"},
    "translation": {"deepl", "azure"}, "warehouse": {"sqlalchemy"}, "vectorstore": {"qdrant"}, "crm": {"salesforce", "hubspot"},
}
EXTERNAL = sorted((k, p) for k, ps in EXPECTED.items() for p in ps)


def test_all_external_providers_are_registered():
    load_connectors()
    registered = providers()
    for kind, wanted in EXPECTED.items():
        assert wanted <= set(registered[kind]), (kind, registered[kind])
    assert {"csv", "none"} <= set(registered["crm"])


@pytest.mark.parametrize("kind,provider", EXTERNAL)
def test_external_contract_without_config(hub, kind, provider):
    cls = provider_class(kind, provider)
    assert cls.__dict__.get("external") is True, "external = True must be set on the class"
    doc = cls.__doc__ or ""
    assert "Auth" in doc and "Config" in doc and ("http" in doc or "SQLAlchemy" in doc), "docstring must name endpoint, auth and config"
    conn = cls({"_transport": httpx.MockTransport(no_network)}, hub)  # construction never needs network
    assert conn.available is False
    info = conn.describe()
    assert info["external"] is True and info["available"] is False and info["description"]


MISSING_CONFIG_CALLS = {
    ("search", "serpapi"): lambda c: c.search("q"), ("search", "brave"): lambda c: c.news("q"), ("search", "tavily"): lambda c: c.search("q"),
    ("analytics", "ga4"): lambda c: c.daily(date(2026, 9, 1), date(2026, 9, 2)),
    ("analytics", "adobe_analytics"): lambda c: c.channels(date(2026, 9, 1), date(2026, 9, 2)),
    ("keyword_data", "gsc"): lambda c: c.queries("home"), ("keyword_data", "dataforseo"): lambda c: c.queries("home"),
    ("cms", "aem"): lambda c: c.publish(title="t", body="b"), ("cms", "contentful"): lambda c: c.publish(title="t", body="b"),
    ("cms", "wordpress"): lambda c: c.publish(title="t", body="b"),
    ("esp", "braze"): lambda c: c.export(name="n", subject="s", preheader="p", html="<p/>", text="t"),
    ("esp", "mailchimp"): lambda c: c.export(name="n", subject="s", preheader="p", html="<p/>", text="t"),
    ("esp", "ajo"): lambda c: c.export(name="n", subject="s", preheader="p", html="<p/>", text="t"),
    ("tasks", "jira"): lambda c: c.create([{"title": "t"}]), ("tasks", "azure_devops"): lambda c: c.create([{"title": "t"}]),
    ("experiments", "optimizely"): lambda c: c.list(), ("experiments", "adobe_target"): lambda c: c.list(),
    ("flags", "launchdarkly"): lambda c: c.deploy_variation(experiment_id="e", variation_id="v"),
    ("behaviour", "clarity"): lambda c: c.events(), ("webinar", "zoom"): lambda c: c.attendees("1"),
    ("translation", "deepl"): lambda c: c.translate(["hi"], source="en", target="de"),
    ("translation", "azure"): lambda c: c.translate(["hi"], source="en", target="de"),
    ("warehouse", "sqlalchemy"): lambda c: c.query("select 1"),
    ("vectorstore", "qdrant"): lambda c: c.search("col", "q"), ("crm", "salesforce"): lambda c: c.contacts(),
    ("crm", "hubspot"): lambda c: c.contacts(),
}


@pytest.mark.parametrize("kind,provider", EXTERNAL)
def test_missing_config_raises_not_configured(hub, kind, provider):
    assert (kind, provider) in MISSING_CONFIG_CALLS
    conn = make(hub, kind, provider, Api())
    with pytest.raises(ConnectorNotConfigured):
        MISSING_CONFIG_CALLS[(kind, provider)](conn)


# ----------------------------------------------------------------------------- _http: retries, errors

def test_retry_on_503_for_get_and_429_for_post(hub, monkeypatch):
    waits: list[float] = []
    monkeypatch.setattr(_http, "_sleep", waits.append)
    state = {"get": 0, "post": 0}

    def flaky_get(req):
        state["get"] += 1
        return (503, {"busy": True}) if state["get"] == 1 else {"ok": True}

    def throttled_post(req):
        state["post"] += 1
        return httpx.Response(429, headers={"retry-after": "2"}) if state["post"] == 1 else httpx.Response(200, json={"ok": 1})

    api = Api(("GET", r"/flaky$", flaky_get), ("POST", r"/throttled$", throttled_post), ("POST", r"/down$", (503, {})))
    conn = make(hub, "search", "serpapi", api, api_key="k")
    client = _http.http_client(conn, base_url="https://vendor.example")
    assert client.get("/flaky").json() == {"ok": True} and state["get"] == 2
    assert client.post("/throttled", json={}).status_code == 200 and state["post"] == 2
    assert 2.0 in waits  # Retry-After honoured
    assert client.post("/down", json={}).status_code == 503  # non-idempotent POST is not retried on 5xx
    assert len(api.find("POST", r"/down$")) == 1
    read_only = _http.http_client(conn, base_url="https://vendor.example", retry_post=True)
    assert read_only.post("/down", json={}).status_code == 503
    assert len(api.find("POST", r"/down$")) == 1 + 3  # declared read-only POSTs get max_retries (2) retries


def test_connect_errors_are_retried(hub, monkeypatch):
    monkeypatch.setattr(_http, "_sleep", lambda s: None)
    attempts = {"n": 0}

    def handler(req):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise httpx.ConnectError("refused", request=req)
        return httpx.Response(200, json={"ok": True})

    conn = make(hub, "search", "brave", None, api_key="k", _transport=httpx.MockTransport(handler))
    assert _http.http_client(conn).get("https://vendor.example/x").json() == {"ok": True} and attempts["n"] == 3


def test_errors_never_echo_query_string_secrets(hub):
    api = Api(("GET", r"serpapi\.com/search\.json$", (401, {"error": "Invalid API key."})))
    conn = make(hub, "search", "serpapi", api, api_key="SECRET-KEY-123")
    with pytest.raises(ConnectorError) as exc:
        conn.search("home insurance")
    msg = str(exc.value)
    assert "SECRET-KEY-123" not in msg and "HTTP 401" in msg and "credentials" in msg
    assert "api_key=SECRET-KEY-123" in str(api.calls[0].url)  # it was sent, just never echoed


# ----------------------------------------------------------------------------- configuration-only swap

def test_provider_swap_is_configuration_only(monkeypatch):
    monkeypatch.setenv("CAF_SEARCH_PROVIDER", "brave")
    monkeypatch.setenv("SEARCH_API_KEY", "brave-key")
    monkeypatch.setenv("CAF_ANALYTICS_PROVIDER", "adobe_analytics")
    for k, v in {"ADOBE_ANALYTICS_COMPANY_ID": "coforge1", "ADOBE_ANALYTICS_RSID": "meridianprod", "ADOBE_CLIENT_ID": "cid",
                 "ADOBE_CLIENT_SECRET": "csecret"}.items():
        monkeypatch.setenv(k, v)
    settings = load_settings()  # config/settings.yaml + env, exactly as in production
    api = Api(("GET", r"api\.search\.brave\.com/res/v1/web/search$", {"web": {"results": [
        {"title": "Flood cover <strong>explained</strong>", "url": "https://news.example/flood", "description": "What it covers",
         "page_age": "2026-10-01T09:00:00", "meta_url": {"hostname": "news.example"}}]}}),
        ims_route(), ("POST", r"analytics\.adobe\.io/api/coforge1/reports$", {"rows": [
            {"itemId": "1", "value": "Organic Search", "data": [100, 80, 4, 400.0]}], "lastPage": True}))
    settings.connectors["search"]["_transport"] = api.transport
    settings.connectors["analytics"]["_transport"] = api.transport
    hub = ConnectorHub(settings)
    search = hub.get("search")
    assert (search.provider, search.external, search.available) == ("brave", True, True)
    assert search.search("flood cover")[0].title == "Flood cover explained"
    analytics = hub.get("analytics")
    assert analytics.provider == "adobe_analytics" and analytics.available
    assert list(analytics.channels(date(2026, 9, 1), date(2026, 9, 30)).columns) == \
        ["channel", "sessions", "users", "conversions", "revenue", "conversion_rate"]
    assert api.one("GET", r"brave").headers["x-subscription-token"] == "brave-key"
    # a non-active provider reads its own block under connectors.<kind>.providers.<provider>
    settings.connectors["search"]["providers"] = {"tavily": {"api_key": "tvly-key", "_transport": Api(
        ("POST", r"api\.tavily\.com/search$", {"results": []})).transport}}
    assert hub.get("search", "tavily").search("x") == []


# ----------------------------------------------------------------------------- search

def test_serpapi_web_and_news(hub):
    def web(req):
        q = dict(req.url.params)
        assert q["engine"] == "google" and q["q"] == "home insurance flood" and q["num"] == "5" and q["tbs"] == "qdr:w"
        assert q["api_key"] == "serp-key" and q["gl"] == "us"
        return {"organic_results": [
            {"position": 1, "title": "Flood guide", "link": "https://harborline.example/flood", "snippet": "S1", "date": RECENT.strftime("%b %d, %Y"),
             "source": "Harborline"},
            {"position": 2, "title": "Old page", "link": "https://old.example/", "snippet": "S2", "date": "Jan 2, 2020"},
            {"position": 3, "title": "Undated", "link": "https://crestview.example/home", "snippet": "S3"}]}

    def news(req):
        assert req.url.params["engine"] == "google_news"
        return {"news_results": [
            {"position": 1, "title": "Insurers brace", "link": "https://wire.example/a", "source": {"name": "Wire"},
             "iso_date": (TODAY - timedelta(days=2)).isoformat() + "T08:00:00Z"},
            {"position": 2, "highlight": {"title": "Top story", "link": "https://paper.example/top", "source": {"name": "Paper"},
                                          "date": YESTERDAY.strftime("%m/%d/%Y") + ", 07:00 AM, +0000 UTC"},
             "stories": [{"title": "Related", "link": "https://blog.example/rel", "source": {"name": "Blog"},
                          "iso_date": (TODAY - timedelta(days=400)).isoformat() + "T00:00:00Z"}]}]}

    api = Api(("GET", r"serpapi\.com/search\.json$", lambda r: web(r) if r.url.params["engine"] == "google" else news(r)))
    conn = make(hub, "search", "serpapi", api, api_key="serp-key", country="us")
    res = conn.search("home insurance flood", num=5, recency_days=7)
    assert [r.url for r in res] == ["https://harborline.example/flood", "https://crestview.example/home"]  # 2020 result filtered
    assert res[0].published == RECENT.isoformat() and res[0].source == "Harborline" and res[0].provider == "serpapi"
    assert res[1].source == "crestview.example" and res[1].published is None and res[0].retrieved_at
    items = conn.news("insurers", num=10, days=30)
    assert [i.url for i in items] == ["https://wire.example/a", "https://paper.example/top"]
    assert items[1].published == YESTERDAY.isoformat() and items[0].source == "Wire" and items[0].provider == "serpapi:news"
    assert set(items[0].to_dict()) == {"title", "url", "snippet", "published", "source", "retrieved_at", "provider"}


def test_brave_web_and_news(hub):
    api = Api(("GET", r"/res/v1/web/search$", {"web": {"results": [
        {"title": "Home <strong>insurance</strong>", "url": "https://a.example/x", "description": "Compare <b>cover</b>",
         "age": "2 days ago", "profile": {"name": "A Example"}}]}}),
        ("GET", r"/res/v1/news/search$", {"results": [
            {"title": "Storm season", "url": "https://n.example/s", "description": "d", "page_age": RECENT.isoformat() + "T10:00:00",
             "meta_url": {"hostname": "n.example"}}]}))
    conn = make(hub, "search", "brave", api, api_key="bk", country="US")
    web = conn.search("home insurance", num=50, recency_days=14)
    req = api.one("GET", r"/web/search$")
    assert req.headers["x-subscription-token"] == "bk" and req.url.params["count"] == "20"  # capped at 20
    start = (TODAY - timedelta(days=14)).isoformat()
    assert req.url.params["freshness"] == f"{start}to{TODAY.isoformat()}" and req.url.params["country"] == "US"
    assert web[0].title == "Home insurance" and web[0].snippet == "Compare cover" and web[0].source == "A Example"
    assert web[0].published == (TODAY - timedelta(days=2)).isoformat()
    news = conn.news("storm", num=60, days=30)
    assert api.one("GET", r"/news/search$").url.params["count"] == "50"
    assert news[0].published == RECENT.isoformat() and news[0].source == "n.example" and news[0].provider == "brave:news"


def test_tavily_search_and_news(hub):
    def handler(req):
        b = body(req)
        assert req.headers["authorization"] == "Bearer tvly-key" and b["max_results"] == 20
        if b["topic"] == "news":
            assert b["start_date"] == (TODAY - timedelta(days=7)).isoformat()
            return {"results": [{"title": "N", "url": "https://n.example/1", "content": "c", "score": 0.9,
                                 "published_date": "Mon, 13 Oct 2026 10:00:00 GMT"}]}
        assert "start_date" not in b and b["search_depth"] == "advanced"
        return {"results": [{"title": "W", "url": "https://w.example/1", "content": "snippet", "score": 0.8, "published_date": None}]}

    api = Api(("POST", r"api\.tavily\.com/search$", handler))
    conn = make(hub, "search", "tavily", api, api_key="tvly-key", search_depth="advanced")
    web = conn.search("q", num=25)
    assert web[0].snippet == "snippet" and web[0].source == "w.example" and web[0].provider == "tavily"
    news = conn.news("q", num=30, days=7)
    assert news[0].provider == "tavily:news" and news[0].url == "https://n.example/1"


# ----------------------------------------------------------------------------- analytics: GA4

@pytest.fixture(scope="module")
def service_account(tmp_path_factory):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    info = {"type": "service_account", "project_id": "caf-demo", "private_key_id": "kid-1", "private_key": pem,
            "client_email": "caf-reader@caf-demo.iam.gserviceaccount.com", "token_uri": "https://oauth2.googleapis.com/token"}
    path = tmp_path_factory.mktemp("google") / "sa.json"
    path.write_text(json.dumps(info))
    return key.public_key(), path


def verify_jwt(assertion: str, public_key) -> dict:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    def dec(part: str) -> bytes:
        return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))

    head, claims, sig = assertion.split(".")
    public_key.verify(dec(sig), f"{head}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())  # raises if forged
    assert json.loads(dec(head)) == {"alg": "RS256", "typ": "JWT", "kid": "kid-1"}
    return json.loads(dec(claims))


def ga4_rows(dims: list[str], mets: list[str], values: list[tuple[list[str], list[str]]], total: int | None = None) -> dict:
    return {"dimensionHeaders": [{"name": d} for d in dims], "metricHeaders": [{"name": m, "type": "TYPE_INTEGER"} for m in mets],
            "rows": [{"dimensionValues": [{"value": v} for v in d], "metricValues": [{"value": v} for v in m]} for d, m in values],
            "rowCount": total if total is not None else len(values)}


def ga4_report(req: httpx.Request):
    b = body(req)
    dims, mets = [d["name"] for d in b["dimensions"]], [m["name"] for m in b["metrics"]]
    if dims == ["date"]:
        return ga4_rows(dims, mets, [(["20260901"], ["100", "80", "50", "60", "5", "250.5"]),
                                     (["20260903"], ["120", "90", "55", "70", "6", "300"])])
    if dims == ["pagePath", "pageTitle"]:
        return ga4_rows(dims, mets, [(["/", "Home"], ["300", "250", "150", "9000", "10"]),
                                     (["/quote", "Get a quote"], ["100", "90", "80", "7000", "20"])])
    if dims == ["sessionDefaultChannelGroup"]:
        return ga4_rows(dims, mets, [(["Organic Search"], ["400", "350", "12", "4800.25"]), (["Email"], ["50", "45", "4", "1600"])])
    if dims == ["date", "sessionDefaultChannelGroup"]:
        return ga4_rows(dims, mets, [(["20260902", "Email"], ["5", "4", "1", "10"]), (["20260901", "Direct"], ["7", "6", "0", "0"])])
    raise AssertionError(dims)


def test_ga4_service_account_jwt_and_daily(hub, service_account):
    public_key, sa_path = service_account

    def token(req):
        f = form(req)
        assert f["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
        claims = verify_jwt(f["assertion"], public_key)
        assert claims["iss"] == "caf-reader@caf-demo.iam.gserviceaccount.com"
        assert claims["scope"] == "https://www.googleapis.com/auth/analytics.readonly"
        assert claims["aud"] == "https://oauth2.googleapis.com/token" and claims["exp"] - claims["iat"] == 3600
        return {"access_token": "ya29.test", "expires_in": 3599, "token_type": "Bearer"}

    api = Api(("POST", r"oauth2\.googleapis\.com/token$", token),
              ("POST", r"analyticsdata\.googleapis\.com/v1beta/properties/123456:runReport$", ga4_report))
    conn = make(hub, "analytics", "ga4", api, property_id="properties/123456", credentials_file=str(sa_path))
    assert conn.available
    df = conn.daily(date(2026, 9, 1), date(2026, 9, 3), domain="www.meridian-mutual.example")
    local = hub.get("analytics", "csv").daily(date(2026, 9, 1), date(2026, 9, 3))
    assert list(df.columns) == list(local.columns) == ["date", "sessions", "users", "new_users", "engaged_sessions", "conversions", "revenue"]
    assert [d.kind for d in df.dtypes[1:]] == [d.kind for d in local.dtypes[1:]]
    assert df["date"].tolist() == [date(2026, 9, 1), date(2026, 9, 3)]  # rows as returned: a GA4 gap stays a gap (agents flag it)
    assert df.iloc[0].tolist()[1:] == [100, 80, 50, 60, 5, 250.5]
    req = api.find("POST", r":runReport$")[0]
    b = body(req)
    assert req.headers["authorization"] == "Bearer ya29.test"
    assert b["dateRanges"] == [{"startDate": "2026-09-01", "endDate": "2026-09-03"}]
    assert [m["name"] for m in b["metrics"]] == ["sessions", "totalUsers", "newUsers", "engagedSessions", "keyEvents", "totalRevenue"]
    assert b["dimensionFilter"]["filter"] == {"fieldName": "hostName", "stringFilter": {"matchType": "EXACT", "value": "www.meridian-mutual.example"}}
    conn.daily(date(2026, 9, 1), date(2026, 9, 3))
    assert len(api.find("POST", r"oauth2")) == 1  # token cached


def test_ga4_pages_channels_and_paging(hub):
    api = Api(("POST", r":runReport$", ga4_report))
    conn = make(hub, "analytics", "ga4", api, property_id="123456", access_token="static-token", conversion_metric="conversions")
    start, end = date(2026, 9, 1), date(2026, 9, 30)
    pages = conn.pages(start, end, limit=1)
    local = hub.get("analytics", "csv").pages(start, end, limit=1)
    assert list(pages.columns) == list(local.columns)
    row = pages.iloc[0]
    assert (row["page_path"], row["sessions"], row["engagement_rate"], row["avg_engagement_time_s"], row["conversion_rate"]) == \
        ("/", 300, 0.5, 30.0, 0.0333)
    b = body(api.find("POST", r":runReport$")[0])
    assert b["orderBys"] == [{"metric": {"metricName": "sessions"}, "desc": True}] and b["limit"] == 1
    assert "conversions" in [m["name"] for m in b["metrics"]]  # conversion_metric override
    channels = conn.channels(start, end)
    assert list(channels.columns) == list(hub.get("analytics", "csv").channels(start, end).columns)
    assert channels["channel"].tolist() == ["Organic Search", "Email"] and channels.iloc[1]["conversion_rate"] == 0.08
    cd = conn.channels_daily(start, end)
    assert list(cd.columns) == list(hub.get("analytics", "csv").channels_daily(start, end).columns)
    assert cd["date"].tolist() == [date(2026, 9, 1), date(2026, 9, 2)]

    def paged(req):
        off = body(req)["offset"]
        rows = [(["/a", "A"], ["5", "5", "5", "5", "1"]), (["/b", "B"], ["4", "4", "4", "4", "0"]), (["/c", "C"], ["3", "3", "3", "3", "0"])]
        return ga4_rows(["pagePath", "pageTitle"], ["sessions", "totalUsers", "engagedSessions", "userEngagementDuration", "keyEvents"],
                        rows[off:off + 2], total=3)

    api2 = Api(("POST", r":runReport$", paged))
    conn2 = make(hub, "analytics", "ga4", api2, property_id="1", access_token="t")
    conn2.page_size = 2
    assert conn2.pages(start, end, limit=10)["page_path"].tolist() == ["/a", "/b", "/c"]
    assert [body(r)["offset"] for r in api2.calls] == [0, 2]


def test_analytics_date_bounds(hub):
    """date_bounds() = first/last day *with data* (the CSV provider's min/max), asked of the platform and cached."""
    yesterday = TODAY - timedelta(days=1)

    def ga4_edges(req):
        b = body(req)
        assert b["dateRanges"][0] == {"startDate": "2015-08-14", "endDate": yesterday.isoformat()} and b["limit"] == 1
        desc = b["orderBys"][0]["desc"]
        assert b["orderBys"][0]["dimension"] == {"dimensionName": "date"}
        return ga4_rows(["date"], ["sessions"], [(["20260930" if desc else "20240305"], ["17"])])

    api = Api(("POST", r":runReport$", ga4_edges))
    ga4 = make(hub, "analytics", "ga4", api, property_id="1", access_token="t")
    assert ga4.date_bounds() == (date(2024, 3, 5), date(2026, 9, 30)) == ga4.date_bounds()
    assert [body(r)["orderBys"][0]["desc"] for r in api.calls] == [False, True]  # two 1-row reports, then cached

    def adobe_days(req):
        b = body(req)
        assert b["dimension"] == "variables/daterangeday" and b["metricContainer"]["metrics"] == [{"columnId": "0", "id": "metrics/visits"}]
        return {"rows": [{"itemId": "1260301", "value": "Mar 1, 2026", "data": [0]}, {"itemId": "1260302", "value": "Mar 2, 2026", "data": [40]},
                         {"itemId": "1260930", "value": "Sep 30, 2026", "data": [55]}, {"itemId": "1261001", "value": "Oct 1, 2026", "data": [0]}],
                "lastPage": True}

    aa_api = Api(ims_route(), ("POST", r"/reports$", adobe_days))
    aa = make(hub, "analytics", "adobe_analytics", aa_api, company_id="c", rsid="r", client_id="i", client_secret="s")
    assert aa.date_bounds() == (date(2026, 3, 2), date(2026, 9, 30))  # zero-visit days are not "data"
    window = body(aa_api.find("POST", r"/reports$")[0])["globalFilters"][0]["dateRange"]
    assert window.startswith((yesterday - timedelta(days=364)).isoformat())  # trailing lookback_days (365) by default
    pinned = make(hub, "analytics", "adobe_analytics", Api(ims_route(), ("POST", r"/reports$", adobe_days)), company_id="c", rsid="r",
                  client_id="i", client_secret="s", data_start="2023-01-01")
    assert pinned.date_bounds() == (date(2023, 1, 1), date(2026, 9, 30))
    empty = make(hub, "analytics", "adobe_analytics", Api(ims_route(), ("POST", r"/reports$", {"rows": [], "lastPage": True})),
                 company_id="c", rsid="r", client_id="i", client_secret="s")
    assert empty.date_bounds() == (yesterday - timedelta(days=364), yesterday)  # nothing returned: the window itself


def test_ga4_without_signing_libraries_is_not_configured(hub, service_account, monkeypatch):
    _, sa_path = service_account
    monkeypatch.setitem(sys.modules, "google.auth", None)
    monkeypatch.setitem(sys.modules, "cryptography.hazmat.primitives", None)
    conn = make(hub, "analytics", "ga4", Api(), property_id="1", credentials_file=str(sa_path))
    with pytest.raises(ConnectorNotConfigured, match="google-auth"):
        conn.daily(date(2026, 9, 1), date(2026, 9, 2))


# ----------------------------------------------------------------------------- analytics: Adobe

def adobe_reports(req: httpx.Request):
    b = body(req)
    dim, page = b["dimension"], b["settings"]["page"]
    if dim == "variables/daterangeday" and "metricFilters" in b["metricContainer"]:
        return {"rows": [{"itemId": "1260901", "value": "Sep 1, 2026", "data": [10, 9, 1, 100.0, 5, 4, 0, 0.0]}], "lastPage": True}
    if dim == "variables/daterangeday":
        rows = [{"itemId": "1260901", "value": "Sep 1, 2026", "data": [200, 150, 6, 900.5, 50]},
                {"itemId": "1260902", "value": "Sep 2, 2026", "data": [220, 160, 7, 1000.0, 60]}]
        return {"rows": [rows[page]], "lastPage": page == 1, "totalPages": 2}
    if dim == "variables/page":
        return {"rows": [{"itemId": "11", "value": "home", "data": [300, 250, 100, 6000, 12]}], "lastPage": True}
    if dim == "variables/marketingchannel":
        return {"rows": [{"itemId": "1", "value": "Natural Search", "data": [400, 300, 10, 2500.0]},
                         {"itemId": "2", "value": "Email", "data": [50, 40, 2, 300.0]}][: b["settings"]["limit"]], "lastPage": True}
    raise AssertionError(dim)


def test_adobe_analytics_ims_and_reports(hub):
    api = Api(ims_route("aa-token"), ("POST", r"analytics\.adobe\.io/api/coforge1/reports$", adobe_reports))
    conn = make(hub, "analytics", "adobe_analytics", api, company_id="coforge1", rsid="meridianprod", client_id="cid",
                client_secret="secret")
    df = conn.daily(date(2026, 9, 1), date(2026, 9, 2))
    tok = form(api.one("POST", r"ims/token/v3$"))
    assert tok == {"grant_type": "client_credentials", "client_id": "cid", "client_secret": "secret",
                   "scope": "openid,AdobeID,read_organizations,additional_info.projectedProductContext"}
    first = api.find("POST", r"/reports$")[0]
    assert first.headers["authorization"] == "Bearer aa-token" and first.headers["x-api-key"] == "cid"
    assert first.headers["x-proxy-global-company-id"] == "coforge1"
    b = body(first)
    assert b["rsid"] == "meridianprod" and b["dimension"] == "variables/daterangeday" and b["settings"]["dimensionSort"] == "asc"
    assert b["globalFilters"] == [{"type": "dateRange", "dateRange": "2026-09-01T00:00:00.000/2026-09-03T00:00:00.000"}]
    assert [m["id"] for m in b["metricContainer"]["metrics"]] == ["metrics/visits", "metrics/visitors", "metrics/orders", "metrics/revenue",
                                                                   "metrics/bounces"]
    assert [body(r)["settings"]["page"] for r in api.find("POST", r"/reports$")] == [0, 1]  # paged until lastPage
    assert list(df.columns) == list(hub.get("analytics", "csv").daily(date(2026, 9, 1), date(2026, 9, 2)).columns)
    assert df["sessions"].tolist() == [200, 220] and df["engaged_sessions"].tolist() == [150, 160]  # visits - bounces
    assert df["new_users"].isna().all()  # Adobe has no standard new-visitor metric: NaN, never invented
    pages = conn.pages(date(2026, 9, 1), date(2026, 9, 30), limit=5)
    assert list(pages.columns) == list(hub.get("analytics", "csv").pages(date(2026, 9, 1), date(2026, 9, 30)).columns)
    assert pages.iloc[0][["page_path", "sessions", "engaged_sessions", "engagement_time_s"]].tolist() == ["home", 300, 200, 6000]
    ch = conn.channels(date(2026, 9, 1), date(2026, 9, 30))
    assert ch["channel"].tolist() == ["Natural Search", "Email"] and ch.iloc[0]["conversion_rate"] == 0.025
    cd = conn.channels_daily(date(2026, 9, 1), date(2026, 9, 1))
    bd = body(api.find("POST", r"/reports$")[-1])
    assert bd["metricContainer"]["metricFilters"][0] == {"id": "f0", "type": "breakdown", "dimension": "variables/marketingchannel", "itemId": "1"}
    assert cd.to_dict("records") == [{"date": date(2026, 9, 1), "channel": "Email", "sessions": 5, "users": 4, "conversions": 0, "revenue": 0.0},
                                     {"date": date(2026, 9, 1), "channel": "Natural Search", "sessions": 10, "users": 9, "conversions": 1,
                                      "revenue": 100.0}]
    assert len(api.find("POST", r"ims/token/v3$")) == 1  # one IMS token for every report


def test_adobe_new_users_metric_mapping(hub):
    def handler(req):
        assert [m["id"] for m in body(req)["metricContainer"]["metrics"]][2] == "cm123_new_visitors"
        return {"rows": [{"itemId": "1261018", "value": "Oct 18, 2026", "data": [10, 8, 3, 1, 5.0, 2]}], "lastPage": True}

    api = Api(ims_route(), ("POST", r"/reports$", handler))
    conn = make(hub, "analytics", "adobe_analytics", api, company_id="c", rsid="r", client_id="i", client_secret="s",
                metric_map={"new_users": "cm123_new_visitors"})
    df = conn.daily(date(2026, 10, 18), date(2026, 10, 18))
    assert df.iloc[0]["new_users"] == 3 and df.iloc[0]["date"] == date(2026, 10, 18)


def test_adobe_static_access_token_skips_ims(hub):
    for kind, provider, cfg, call in [
        ("analytics", "adobe_analytics", {"company_id": "c", "rsid": "r"}, lambda c: c.channels(date(2026, 9, 1), date(2026, 9, 2))),
        ("experiments", "adobe_target", {"tenant": "t"}, lambda c: c.results("1")),
        ("esp", "ajo", {"org_id": "o"}, lambda c: c.export(**EMAIL)),
    ]:
        api = Api(("POST", r"/reports$", {"rows": [], "lastPage": True}),
                  ("GET", r"/report/performance$", {"report": {"statistics": {"experiences": []}}}),
                  ("POST", r"/ajo/content/templates$", (201, {"id": "t1"})))
        conn = make(hub, kind, provider, api, client_id="cid", access_token="pre-issued", **cfg)
        assert conn.available
        call(conn)
        assert api.calls and not api.find("POST", r"ims/token")
        assert all(c.headers["authorization"] == "Bearer pre-issued" for c in api.calls)
    assert make(hub, "esp", "ajo", None, client_id="c", org_id="o").available is False  # neither secret nor token


# ----------------------------------------------------------------------------- keyword data

def test_gsc_queries_regex_and_fallback(hub):
    def handler(req):
        b = body(req)
        assert req.headers["authorization"] == "Bearer gsc-token" and b["dimensions"] == ["query", "page"] and b["type"] == "web"
        assert b["endDate"] == (TODAY - timedelta(days=3)).isoformat()
        if "dimensionFilterGroups" in b:
            assert b["dimensionFilterGroups"][0]["filters"][0] == {"dimension": "query", "operator": "includingRegex",
                                                                   "expression": r"\b(flood|insurance)\b"}
            return {"rows": [{"keys": ["flood insurance cost", "https://www.meridian-mutual.example/products/home-insurance.html"],
                              "clicks": 40, "impressions": 900, "ctr": 0.04444, "position": 6.2},
                             {"keys": ["home insurance flood", "https://www.meridian-mutual.example/"], "clicks": 90, "impressions": 2000,
                              "ctr": 0.045, "position": 4.1}]}
        return {"rows": [{"keys": ["meridian mutual", "https://www.meridian-mutual.example/"], "clicks": 100, "impressions": 150,
                          "ctr": 0.6667, "position": 1.0}]}

    api = Api(("POST", r"searchconsole\.googleapis\.com/webmasters/v3/sites/[^/]+/searchAnalytics/query$", handler))
    conn = make(hub, "keyword_data", "gsc", api, site_url="sc-domain:meridian-mutual.example", access_token="gsc-token")
    df = conn.queries(topic="flood insurance", limit=10)
    assert list(df.columns) == list(hub.get("keyword_data", "csv").queries().columns) == KEYWORD_COLUMNS
    assert df["query"].tolist() == ["home insurance flood", "flood insurance cost"]  # sorted by impressions like the CSV provider
    assert b"/sites/sc-domain%3Ameridian-mutual.example/searchAnalytics/query" in api.calls[0].url.raw_path
    none_match = make(hub, "keyword_data", "gsc", Api(("POST", r"searchAnalytics/query$",
                                                        lambda r: {"rows": []} if "dimensionFilterGroups" in body(r) else handler(r))),
                      site_url="https://www.meridian-mutual.example/", access_token="gsc-token")
    assert none_match.queries(topic="unrelated topic").iloc[0]["query"] == "meridian mutual"  # CSV-style fallback


def test_dataforseo_ranked_keywords_and_suggestions(hub):
    def ranked(req):
        assert basic(req) == ("login@coforge.example", "pw")
        task = body(req)[0]
        assert task["target"] == "meridian-mutual.example" and task["location_code"] == 2840 and task["language_code"] == "en"
        assert task["filters"] == [["keyword_data.keyword", "like", "%flood%"]]
        return {"status_code": 20000, "tasks": [{"status_code": 20000, "result": [{"items": [
            {"keyword_data": {"keyword": "flood cover", "keyword_info": {"search_volume": 1000}},
             "ranked_serp_element": {"serp_item": {"url": "https://www.meridian-mutual.example/flood", "rank_group": 4, "etv": 55.5}}},
            {"keyword_data": {"keyword": "flood insurance", "keyword_info": {"search_volume": 5000}},
             "ranked_serp_element": {"serp_item": {"url": "https://www.meridian-mutual.example/home", "rank_group": 9, "etv": 20.0}}}]}]}]}

    api = Api(("POST", r"api\.dataforseo\.com/v3/dataforseo_labs/google/ranked_keywords/live$", ranked),
              ("POST", r"keyword_suggestions/live$", {"status_code": 20000, "tasks": [{"status_code": 20000, "result": [
                  {"items": [{"keyword": "pet insurance cost", "keyword_info": {"search_volume": 800}}]}]}]}))
    conn = make(hub, "keyword_data", "dataforseo", api, login="login@coforge.example", password="pw")
    df = conn.queries(topic="flood", site="meridian-mutual.example")
    assert list(df.columns) == KEYWORD_COLUMNS and df.attrs["estimated"] is True
    assert df.iloc[0].tolist() == ["flood insurance", "https://www.meridian-mutual.example/home", 20.0, 5000.0, 0.004, 9.0]
    sugg = conn.queries(topic="pet insurance")
    assert sugg.iloc[0]["query"] == "pet insurance cost" and np.isnan(sugg.iloc[0]["position"])
    bad = make(hub, "keyword_data", "dataforseo", Api(("POST", r"ranked_keywords", {"status_code": 20000, "tasks": [
        {"status_code": 40501, "status_message": "Invalid Field: 'target'."}]})), login="l", password="p", target="x")
    with pytest.raises(ConnectorError, match="40501"):
        bad.queries()


# ----------------------------------------------------------------------------- CMS

def test_aem_sites_api_create_and_publish(hub):
    model_path = "/conf/meridian/settings/dam/cfm/models/article"

    def create(req):
        assert req.headers["authorization"] == "Bearer ims-token" and req.headers["x-api-key"] == "aem-client"
        b = body(req)
        assert b["modelId"] == base64.urlsafe_b64encode(model_path.encode()).rstrip(b"=").decode()
        assert b["parentPath"] == "/content/dam/meridian/fr/articles" and b["name"] == "claims-tips" and b["title"] == "Claims tips"
        assert b["fields"][:2] == [{"name": "headline", "type": "text", "values": ["Claims tips"]},
                                   {"name": "main", "type": "long-text", "mimeType": "text/x-markdown", "values": ["# Hi"]}]
        assert b["fields"][2] == {"name": "author", "type": "text", "multiple": False, "values": ["Tom"]}
        return 201, {"id": "cf-1", "path": "/content/dam/meridian/fr/articles/claims-tips"}

    api = Api(ims_route(), ("GET", r"/adobe/sites/cf/fragments$", {"items": []}), ("POST", r"/adobe/sites/cf/fragments$", create),
              ("POST", r"/adobe/sites/cf/fragments/publish$", lambda r: (200, {"ids": body(r)["ids"]})))
    conn = make(hub, "cms", "aem", api, base_url="https://author-p1-e2.adobeaemcloud.com", client_id="aem-client", client_secret="s",
                parent_path="/content/dam/meridian/{locale}/articles", model_path=model_path, field_map={"title": "headline", "body": "main"})
    out = conn.publish(title="Claims tips", body="# Hi", slug="claims-tips", locale="fr", status="published", fields={"author": "Tom"})
    assert out["id"] == "cf-1" and out["status"] == "published" and out["action"] == "created" and out["url"].endswith("/claims-tips")
    assert api.one("GET", r"/cf/fragments$").url.params["path"] == "/content/dam/meridian/fr/articles/claims-tips"
    assert body(api.one("POST", r"/publish$")) == {"ids": ["cf-1"]}
    assert form(api.one("POST", r"ims/token"))["scope"] == "AdobeID,openid,aem.fragments.management,aem.folders"


def test_aem_sites_api_update_uses_etag(hub):
    path = "/content/dam/meridian/en/articles/claims-tips"
    api = Api(("GET", r"/cf/fragments$", {"items": [{"id": "cf-9", "path": path, "etag": "stale"}]}),
              ("GET", r"/cf/fragments/cf-9$", httpx.Response(200, json={"id": "cf-9"}, headers={"etag": '"v7"'})),
              ("PUT", r"/cf/fragments/cf-9$", lambda r: {"id": "cf-9", "path": path}))
    conn = make(hub, "cms", "aem", api, base_url="https://author.example", access_token="dev-token",
                parent_path="/content/dam/meridian/{locale}/articles", model_id="bW9kZWw")
    out = conn.publish(title="Claims tips", body="<p>x</p>", slug="claims-tips", body_format="html")
    put = api.one("PUT", r"/cf/fragments/cf-9$")
    assert put.headers["if-match"] == '"v7"' and put.headers["authorization"] == "Bearer dev-token"
    assert body(put)["fields"][1]["mimeType"] == "text/html" and out["action"] == "updated" and out["status"] == "draft"


def test_aem_assets_api_create_then_update(hub):
    api = Api(("POST", r"/api/assets/meridian/en/claims-tips$", (409, {"error": "exists"})),
              ("PUT", r"/api/assets/meridian/en/claims-tips$", {"ok": True}))
    conn = make(hub, "cms", "aem", api, api="assets", base_url="https://author.example", access_token="t",
                parent_path="/content/dam/meridian/{locale}", model_path="/conf/m/settings/dam/cfm/models/article")
    out = conn.publish(title="Claims tips", body="Body", slug="claims-tips", status="published")
    post = body(api.one("POST", r"/api/assets/"))
    assert post["properties"]["cq:model"] == "/conf/m/settings/dam/cfm/models/article"
    assert post["properties"]["elements"]["body"] == {"value": "Body", ":type": "text/x-markdown"}
    assert "cq:model" not in body(api.one("PUT", r"/api/assets/"))["properties"]
    assert out["action"] == "updated" and out["status"] == "draft" and "warning" in out


def test_contentful_create_publish_and_update(hub):
    entries: dict[str, dict] = {}

    def entry(req):
        if req.method == "GET":
            return (200, entries["e"]) if "e" in entries else (404, {"sys": {"id": "NotFound"}})
        b = body(req)
        if "e" not in entries:
            assert req.headers["x-contentful-content-type"] == "blogPost"
            entries["e"] = {"sys": {"id": "blogPost-claims-tips", "version": 1}, "fields": b["fields"]}
        else:
            assert req.headers["x-contentful-version"] == str(entries["e"]["sys"]["version"])
            entries["e"] = {"sys": {"id": "blogPost-claims-tips", "version": entries["e"]["sys"]["version"] + 1}, "fields": b["fields"]}
        assert req.headers["content-type"] == "application/vnd.contentful.management.v1+json"
        return entries["e"]

    api = Api(("GET", r"/entries/blogPost-claims-tips$", entry), ("PUT", r"/entries/blogPost-claims-tips$", entry),
              ("PUT", r"/entries/blogPost-claims-tips/published$", lambda r: {"sys": {"id": "blogPost-claims-tips", "version": 3}}))
    conn = make(hub, "cms", "contentful", api, space_id="sp1", management_token="cma-token", content_type_map={"article": "blogPost"})
    out = conn.publish(title="Claims tips", body="Body", slug="claims-tips", status="published")
    assert api.calls[0].headers["authorization"] == "Bearer cma-token"
    assert str(api.calls[0].url) == "https://api.contentful.com/spaces/sp1/environments/master/entries/blogPost-claims-tips"
    assert entries["e"]["fields"]["title"] == {"en-US": "Claims tips"} and out["status"] == "published" and out["locale"] == "en-US"
    assert api.one("PUT", r"/published$").headers["x-contentful-version"] == "1"
    out2 = conn.publish(title="Conseils", body="Corps", slug="claims-tips", locale="fr-FR")
    assert entries["e"]["fields"]["title"] == {"en-US": "Claims tips", "fr-FR": "Conseils"}  # other locales preserved
    assert out2["action"] == "updated" and out2["status"] == "draft"


def test_wordpress_create_and_update(hub):
    posts: list[dict] = []

    def find(req):
        assert basic(req) == ("editor", "abcdefghijkl") and req.url.params["slug"] in ("claims-tips", "claims-tips-fr")
        return [p for p in posts if p["slug"] == req.url.params["slug"]]

    def create(req):
        b = body(req)
        if b["slug"] == "claims-tips":
            assert b["content"].startswith("<h1>Claims tips</h1>") and "<li>one</li>" in b["content"] and b["status"] == "publish"
        assert b["categories"] == [3]
        posts.append({"id": 42 + len(posts), "slug": b["slug"], "link": f"https://blog.example/{b['slug']}/", "status": b["status"]})
        return 201, posts[-1]

    api = Api(("GET", r"/wp-json/wp/v2/posts$", find), ("POST", r"/wp-json/wp/v2/posts$", create),
              ("POST", r"/wp-json/wp/v2/posts/42$", lambda r: {**posts[0], "status": body(r)["status"]}))
    conn = make(hub, "cms", "wordpress", api, base_url="https://blog.example", username="editor", application_password="abcd efgh ijkl")
    out = conn.publish(title="Claims tips", body="# Claims tips\n\n- one\n- two", slug="claims-tips", status="published",
                       fields={"categories": [3]})
    assert out == {"id": "42", "url": "https://blog.example/claims-tips/", "status": "publish", "action": "created"}
    again = conn.publish(title="Claims tips", body="Updated", slug="claims-tips", status="review", fields={"categories": [3]})
    assert again["action"] == "updated" and again["status"] == "pending"
    conn.publish(title="Conseils", body="x", slug="claims-tips", locale="fr", fields={"categories": [3]})
    assert api.find("GET", r"/posts$")[-1].url.params["slug"] == "claims-tips-fr"


# ----------------------------------------------------------------------------- ESP

EMAIL = {"name": "Flood-ready webinar invite", "subject": "Is your home flood-ready?", "preheader": "Free 30-minute session",
         "html": "<html><body><p>Hi</p></body></html>", "text": "Hi", "mjml": "<mjml></mjml>", "audience": "homeowners"}


def test_braze_email_template_and_content_block(hub):
    api = Api(("POST", r"rest\.iad-01\.braze\.com/templates/email/create$", {"email_template_id": "tpl-1", "message": "success"}),
              ("POST", r"/content_blocks/create$", {"content_block_id": "cb-1", "liquid_tag": "{{content_blocks.${x}}}", "message": "success"}))
    conn = make(hub, "esp", "braze", api, api_key="braze-key", rest_endpoint="https://rest.iad-01.braze.com", tags=["caf"])
    out = conn.export(**EMAIL)
    req = api.one("POST", r"/templates/email/create$")
    assert req.headers["authorization"] == "Bearer braze-key"
    assert body(req) == {"template_name": EMAIL["name"], "subject": EMAIL["subject"], "body": EMAIL["html"], "plaintext_body": "Hi",
                         "preheader": EMAIL["preheader"], "tags": ["caf"]}
    assert out["id"] == "tpl-1" and out["status"] == "exported"
    cb = make(hub, "esp", "braze", api, api_key="k", rest_endpoint="https://rest.iad-01.braze.com", mode="content_block").export(**EMAIL)
    assert body(api.one("POST", r"/content_blocks/create$"))["name"] == "Flood-ready_webinar_invite" and cb["liquid_tag"]


def test_mailchimp_template_and_draft_campaign(hub):
    api = Api(("POST", r"https://us21\.api\.mailchimp\.com/3\.0/templates$", {"id": 101, "name": "x"}),
              ("POST", r"/3\.0/campaigns$", {"id": "camp1", "archive_url": "https://eepurl.example/abc"}),
              ("PUT", r"/3\.0/campaigns/camp1/content$", {"html": "ok"}))
    conn = make(hub, "esp", "mailchimp", api, api_key="mc-key-us21", audience_map={"homeowners": "list-9"}, from_name="Meridian",
                reply_to="hello@meridian-mutual.example")
    out = conn.export(**EMAIL)
    assert basic(api.calls[0]) == ("caf", "mc-key-us21") and body(api.calls[0]) == {"name": EMAIL["name"][:50], "html": EMAIL["html"]}
    camp = body(api.one("POST", r"/campaigns$"))
    assert camp["recipients"] == {"list_id": "list-9"} and camp["settings"]["subject_line"] == EMAIL["subject"]
    assert camp["settings"]["preview_text"] == EMAIL["preheader"] and camp["settings"]["reply_to"] == "hello@meridian-mutual.example"
    assert body(api.one("PUT", r"/content$")) == {"html": EMAIL["html"], "plain_text": "Hi"}
    assert out["id"] == "101" and out["campaign_id"] == "camp1" and out["status"] == "draft_campaign"
    tpl_only = make(hub, "esp", "mailchimp", Api(("POST", r"/templates$", {"id": 5})), api_key="k", server_prefix="us5").export(**EMAIL)
    assert tpl_only == {"id": "5", "status": "exported", "provider": "mailchimp", "template_id": 5}


def test_ajo_content_template(hub):
    api = Api(ims_route("ajo-token"), ("POST", r"platform\.adobe\.io/ajo/content/templates$",
                                       httpx.Response(201, headers={"location": "/ajo/content/templates/tmpl-77", "etag": '"1"'})))
    conn = make(hub, "esp", "ajo", api, client_id="ajo-client", client_secret="s", org_id="ABC123@AdobeOrg", sandbox="dev")
    out = conn.export(**EMAIL)
    req = api.one("POST", r"/ajo/content/templates$")
    assert req.headers["authorization"] == "Bearer ajo-token" and req.headers["x-api-key"] == "ajo-client"
    assert req.headers["x-gw-ims-org-id"] == "ABC123@AdobeOrg" and req.headers["x-sandbox-name"] == "dev"
    assert req.headers["content-type"] == "application/vnd.adobe.ajo.template.v1+json" and "accept" not in req.headers
    b = body(req)
    assert (b["templateType"], b["channels"], b["source"]["origin"], b["template"]["html"]) == ("html", ["email"], "ajo", EMAIL["html"])
    assert "Is your home flood-ready?" in b["description"] and out["id"] == "tmpl-77" and out["sandbox"] == "dev"


# ----------------------------------------------------------------------------- tasks

TASKS = [{"title": "Webinar: landing page", "type": "landing page", "channel": "Web", "owner_role": "Web producer",
          "due_offset_days": 14, "description": "Build the page.\n\n- hero\n- form"},
         {"title": "Webinar: invite email", "description": ""}]


def test_jira_issues_with_adf(hub):
    keys = iter(["MKT-1", "MKT-2"])
    api = Api(("POST", r"meridian\.atlassian\.net/rest/api/3/issue$", lambda r: (201, {"id": "1000", "key": next(keys)})))
    conn = make(hub, "tasks", "jira", api, base_url="https://meridian.atlassian.net", email="bot@meridian-mutual.example", api_token="jt")
    out = conn.create(TASKS, project="MKT")
    assert basic(api.calls[0]) == ("bot@meridian-mutual.example", "jt")
    f = body(api.calls[0])["fields"]
    assert f["project"] == {"key": "MKT"} and f["summary"] == "Webinar: landing page" and f["issuetype"] == {"name": "Task"}
    assert f["labels"] == ["caf", "landing-page", "web"] and f["duedate"] == (TODAY + timedelta(days=14)).isoformat()
    doc = f["description"]
    assert doc["type"] == "doc" and doc["version"] == 1 and doc["content"][0] == {"type": "paragraph", "content": [{"type": "text", "text": "Build the page."}]}
    assert doc["content"][1]["type"] == "bulletList" and doc["content"][1]["content"][1]["content"][0]["content"][0]["text"] == "form"
    assert doc["content"][2]["content"][0]["content"][0]["content"][0]["text"] == "Type: landing page"
    assert out == [{"id": "MKT-1", "title": "Webinar: landing page", "url": "https://meridian.atlassian.net/browse/MKT-1", "issue_id": "1000"},
                   {"id": "MKT-2", "title": "Webinar: invite email", "url": "https://meridian.atlassian.net/browse/MKT-2", "issue_id": "1000"}]
    empty = body(api.calls[1])["fields"]["description"]
    assert empty == {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": []}]}


def test_azure_devops_work_items(hub):
    ids = iter([501, 502])

    def create(req):
        assert req.headers["content-type"] == "application/json-patch+json" and req.url.params["api-version"] == "7.1"
        wid = next(ids)
        return {"id": wid, "_links": {"html": {"href": f"https://dev.azure.com/coforge-demo/Marketing%20Ops/_workitems/edit/{wid}"}}}

    api = Api(("POST", r"dev\.azure\.com/coforge-demo/Marketing%20Ops/_apis/wit/workitems/\$Task$", create))
    conn = make(hub, "tasks", "azure_devops", api, organization="coforge-demo", pat="pat-123", area_path="Marketing\\Web")
    out = conn.create(TASKS, project="Marketing Ops")
    assert basic(api.calls[0]) == ("", "pat-123")
    ops = {o["path"]: o["value"] for o in body(api.calls[0])}
    assert ops["/fields/System.Title"] == "Webinar: landing page" and ops["/fields/System.AreaPath"] == "Marketing\\Web"
    assert ops["/fields/System.Tags"] == "caf; landing page; Web" and "<li>Owner role: Web producer</li>" in ops["/fields/System.Description"]
    assert all(o["op"] == "add" for o in body(api.calls[0]))
    assert [o["id"] for o in out] == ["501", "502"] and out[0]["url"].endswith("/edit/501")


# ----------------------------------------------------------------------------- experiments + flags

OPTI_EXP = {"id": 9001, "project_id": 77, "name": "Hero CTA copy", "status": "running", "description": "If we change the CTA...",
            "holdback": 1000, "audience_conditions": "everyone", "earliest": "2026-08-01T10:00:00Z",
            "url_targeting": {"edit_url": "https://www.meridian-mutual.example/products/home-insurance.html"},
            "variations": [{"variation_id": 1, "name": "Original", "weight": 5000}, {"variation_id": 2, "name": "See my price", "weight": 5000}],
            "metrics": [{"event_id": 555, "aggregator": "unique", "scope": "visitor", "winning_direction": "increasing"},
                        {"event_id": 556, "aggregator": "sum", "field": "revenue", "scope": "visitor"}]}


def test_optimizely_list_get_and_results(hub):
    def listing(req):
        assert req.url.params["project_id"] == "77" and req.url.params["per_page"] == "100"
        page = int(req.url.params["page"])
        return [OPTI_EXP] + [{**OPTI_EXP, "id": 9000 + i, "status": "concluded", "latest": "2026-09-01T00:00:00Z"} for i in range(2, 101)] \
            if page == 1 else [{**OPTI_EXP, "id": 1, "status": "not_started"}]

    def results(req):
        seg = req.url.params.get("device", "all")
        n = {"desktop": 1, "mobile": 2}[seg]
        return {"metrics": [{"event_id": 555, "results": {"1": {"value": 100 * n, "samples": 1000 * n}, "2": {"value": 120 * n}}},
                            {"event_id": 556, "field": "revenue", "results": {"1": {"value": 500000 * n}, "2": {"value": 650000 * n}}}],
                "reach": {"variations": {"1": {"count": 1000 * n}, "2": {"count": 990 * n}}}}

    api = Api(("GET", r"api\.optimizely\.com/v2/experiments$", listing), ("GET", r"/experiments/9001$", OPTI_EXP),
              ("GET", r"/experiments/404$", (404, {})), ("GET", r"/experiments/9001/results$", results),
              ("GET", r"/experiments/9002/results$", (204, "")))
    conn = make(hub, "experiments", "optimizely", api, api_token="opt-token", project_id=77,
                results_segments={"device": ["desktop", "mobile"]})
    exps = conn.list()
    assert len(exps) == 101 and api.calls[0].headers["authorization"] == "Bearer opt-token"
    assert [e.status for e in conn.list(status="draft")] == ["draft"] and len(conn.list(status="completed")) == 99
    e = conn.get("9001")
    assert (e.platform, e.status, e.traffic_allocation, e.page_url) == ("optimizely", "running", 90.0, OPTI_EXP["url_targeting"]["edit_url"])
    assert [(v.id, v.allocation, v.is_control) for v in e.variants] == [("1", 50.0, True), ("2", 50.0, False)]
    assert e.primary_metric.event == "555" and e.secondary_metrics[0].type == "revenue" and e.start_date == "2026-08-01"
    assert conn.get("404") is None
    res = conn.results("9001")
    local = hub.get("experiments", "json").results("exp-hero-cta")
    assert list(res.columns) == list(local.columns) == ["variant", "segment", "visitors", "conversions", "revenue"]
    assert res.to_dict("records")[0] == {"variant": "1", "segment": "desktop", "visitors": 1000, "conversions": 100, "revenue": 5000.0}
    assert sorted(set(res["segment"])) == ["desktop", "mobile"] and "all" not in set(res["segment"])  # rows partition traffic
    assert set(res["variant"]) == {v.id for v in e.variants}  # joins with the definition like the local data
    assert make(hub, "experiments", "optimizely", api, api_token="t", project_id=77).results("9002").empty
    draft = conn.upsert({"id": "exp-new", "name": "New test"})
    path = get_settings().runs_dir / "_experiments" / "optimizely_drafts.json"
    assert draft.id == "exp-new" and json.loads(path.read_text())["experiments"][0]["id"] == "exp-new"
    assert not [c for c in api.calls if c.method != "GET"]  # upsert never writes to Optimizely


def test_adobe_target_activities_and_report(hub):
    detail = {"id": 168824, "name": "Pricing table", "state": "approved", "priority": 0, "workspace": "1234",
              "startsAt": "2026-08-01T00:00:00Z", "endsAt": "2099-01-01T00:00:00Z", "autoAllocateTraffic": {"enabled": False},
              "locations": {"mboxes": [{"locationLocalId": 0, "name": "target-global-mbox"}]},
              "experiences": [{"experienceLocalId": 0, "name": "Experience A", "visitorPercentage": 50},
                              {"experienceLocalId": 1, "name": "Experience B", "visitorPercentage": 50}],
              "metrics": [{"metricLocalId": 1, "name": "Clicks", "conversion": False},
                          {"metricLocalId": 32767, "name": "Quote start", "conversion": True, "mboxes": [{"name": "quote-start"}]}]}

    def listing(req):
        assert req.headers["accept"] == "application/vnd.adobe.target.v3+json" and req.headers["x-api-key"] == "tgt-client"
        return {"total": 3, "offset": 0, "limit": 100, "activities": [{"id": 168824, "type": "ab", "state": "approved"},
                                                                      {"id": 7, "type": "xt", "state": "saved"},
                                                                      {"id": 8, "type": "recs", "state": "approved"}]}

    def report(req):
        assert req.headers["accept"] == "application/vnd.adobe.target.v1+json"
        return {"reportParameters": {"activityId": 168824}, "report": {"statistics": {
            "totals": {"visitor": {"totals": {"entries": 230, "conversions": 21}}},
            "experiences": [{"experienceLocalId": 0, "visitor": {"totals": {"entries": 115, "conversions": 9}}},
                            {"experienceLocalId": 1, "visitor": {"totals": {"entries": 115, "conversions": 12}}}]}}}

    api = Api(ims_route("tgt-token"), ("GET", r"mc\.adobe\.io/meridian/target/activities$", listing),
              ("GET", r"/target/activities/ab/168824$", detail), ("GET", r"/target/activities/xt/7$", {**detail, "id": 7, "state": "saved"}),
              ("GET", r"/target/activities/ab/168824/report/performance$", report), ("GET", r"/target/activities/ab/7$", (404, {})))
    conn = make(hub, "experiments", "adobe_target", api, tenant="meridian", client_id="tgt-client", client_secret="s")
    exps = conn.list()
    assert [(e.id, e.status) for e in exps] == [("168824", "running"), ("7", "draft")]  # recs activities skipped
    e = exps[0]
    assert e.platform == "adobe_target" and [v.allocation for v in e.variants] == [50.0, 50.0] and e.variants[0].is_control
    assert e.primary_metric.name == "Quote start" and e.primary_metric.event == "quote-start"
    assert e.instrumentation["adobe_target"]["mboxes"] == ["target-global-mbox"] and e.start_date == "2026-08-01"
    assert all(c.headers.get("authorization") == "Bearer tgt-token" for c in api.calls if "mc.adobe.io" in str(c.url))
    res = conn.results("168824")
    assert list(res.columns) == ["variant", "segment", "visitors", "conversions", "revenue"]
    assert res[["variant", "visitors", "conversions"]].values.tolist() == [["0", 115, 9], ["1", 115, 12]]
    assert conn.get("7").id == "7"  # falls through ab (404) to xt


def test_launchdarkly_create_existing_and_deploy(hub):
    def create(req):
        assert req.headers["authorization"] == "api-ld-token"  # LaunchDarkly tokens carry no Bearer prefix
        b = body(req)
        assert b["key"] == "caf-exp-exp-hero-cta" and b["variations"][0]["name"] == "control" and b["defaults"] == {"onVariation": 1, "offVariation": 0}
        v = b["variations"][1]["value"]
        assert v["css"] == ".hero{color:red}" and v["selectors"] == ["#hero-cta"] and "CAF variation runtime" in v["bundle"]
        return 201, {"key": b["key"]}

    api = Api(("POST", r"app\.launchdarkly\.com/api/v2/flags/meridian$", create))
    conn = make(hub, "flags", "launchdarkly", api, api_token="api-ld-token", project_key="meridian")
    out = conn.deploy_variation(experiment_id="exp-hero-cta", variation_id="treatment", css=".hero{color:red}", js="1;",
                                selectors=["#hero-cta"])
    assert out["status"] == "preview" and out["action"] == "created" and out["selectors"] == ["#hero-cta"]
    assert set(out) >= {"bundle", "status", "selectors"}  # the file provider's keys
    api2 = Api(("POST", r"/flags/meridian$", (409, {"code": "conflict"})),
               ("GET", r"/flags/meridian/caf-exp-exp-hero-cta$", {"variations": [{"name": "control"}, {"name": "treatment"}]}),
               ("PATCH", r"/flags/meridian/caf-exp-exp-hero-cta$", {"ok": True}))
    conn2 = make(hub, "flags", "launchdarkly", api2, api_token="t", project_key="meridian", environment="production")
    out2 = conn2.deploy_variation(experiment_id="exp-hero-cta", variation_id="variant-c", preview=False)
    patches = [body(r) for r in api2.find("PATCH", r"caf-exp-exp-hero-cta$")]
    assert patches[0]["patch"][0]["op"] == "add" and patches[0]["patch"][0]["path"] == "/variations/-"
    assert patches[1]["patch"] == [{"op": "replace", "path": "/environments/production/on", "value": True}]
    assert out2["status"] == "deployed" and out2["action"] == "variation_added"
    conn2.deploy_variation(experiment_id="exp-hero-cta", variation_id="treatment")
    assert body(api2.find("PATCH", r"caf-exp")[-1])["patch"][0]["path"] == "/variations/1/value"


# ----------------------------------------------------------------------------- behaviour: Clarity

HOME = "https://www.meridian-mutual.example/products/home-insurance.html"
CLARITY = [
    {"metricName": "Traffic", "information": [
        {"totalSessionCount": "40", "totalBotSessionCount": "2", "distantUserCount": "35", "PagesPerSessionPercentage": 1.4, "URL": HOME, "Device": "PC"},
        {"totalSessionCount": "10", "totalBotSessionCount": "0", "distantUserCount": "9", "PagesPerSessionPercentage": 1.1, "URL": HOME, "Device": "Mobile"}]},
    {"metricName": "DeadClickCount", "information": [
        {"sessionsCount": "6", "sessionsWithMetricPercentage": 15.0, "sessionsWithoutMetricPercentage": 85.0, "pagesViews": "48", "subTotal": "9",
         "URL": HOME, "Device": "PC"}]},
    {"metricName": "RageClickCount", "information": [
        {"sessionsCount": "2", "sessionsWithMetricPercentage": 20.0, "pagesViews": "11", "subTotal": "5", "URL": HOME, "Device": "Mobile"}]},
    {"metricName": "ScrollDepth", "information": [{"averageScrollDepth": 42.5, "URL": HOME, "Device": "PC"}]},
]


def test_clarity_aggregates_in_common_schema(hub):
    api = Api(("GET", r"www\.clarity\.ms/export-data/api/v1/project-live-insights$", CLARITY))
    conn = make(hub, "behaviour", "clarity", api, api_token="clarity-token")
    df = conn.events()
    local = hub.get("behaviour", "csv").events()
    assert list(df.columns) == list(local.columns) == EVENT_COLUMNS
    req = api.calls[0]
    assert req.headers["authorization"] == "Bearer clarity-token"
    assert dict(req.url.params) == {"numOfDays": "3", "dimension1": "URL", "dimension2": "Device"}
    counts = df.groupby(["device", "event_type"]).size().to_dict()
    assert counts == {("desktop", "pageview"): 40, ("desktop", "dead_click"): 9, ("desktop", "scroll"): 1,
                      ("mobile", "pageview"): 10, ("mobile", "rage_click"): 5}
    assert df[df.event_type == "dead_click"]["session_id"].nunique() == 6  # sessions affected, as Clarity reports
    assert df[df.event_type == "scroll"]["scroll_pct"].tolist() == [42.5] and df["x"].isna().all()
    assert str(df["ts"].dtype).startswith("datetime64") and df.attrs["aggregated"] is True
    assert conn.pages() == [HOME] and len(conn.events(page_url="/products/home-insurance.html")) == len(df)
    assert len(api.calls) == 1  # 10 requests/project/day: cached per instance
    ins = conn.insights()
    assert ins[ins.metric == "DeadClickCount"].iloc[0][["sessions", "occurrences", "sessions_pct"]].tolist() == [6.0, 9.0, 15.0]


def test_clarity_downsamples_large_pages(hub):
    conn = make(hub, "behaviour", "clarity", Api(("GET", r"project-live-insights$", CLARITY)), api_token="t", max_sessions_per_page=20)
    df = conn.events()
    desktop = df[df.device == "desktop"]
    assert (desktop.event_type == "pageview").sum() == 20 and (desktop.event_type == "dead_click").sum() == 4
    assert df.attrs["scale"] == {f"{HOME}|desktop": 0.5}


# ----------------------------------------------------------------------------- webinar: Zoom

def zoom_api():
    def token(req):
        assert basic(req) == ("zoom-client", "zoom-secret")
        assert dict(req.url.params) == {"grant_type": "account_credentials", "account_id": "acct-1"}
        return {"access_token": "zoom-token", "token_type": "bearer", "expires_in": 3599}

    def registrants(req):
        assert req.url.params["status"] == "approved" and req.url.params["page_size"] == "300"
        if req.url.params.get("next_page_token") == "p2":
            return {"registrants": [{"email": "Zoe.Thornton@harborandmain.example", "first_name": "Zoe", "last_name": "Thornton",
                                     "org": "Harbor & Main Insurance", "job_title": "Client Service Manager", "create_time": "2026-09-02T00:55:26Z"}]}
        return {"next_page_token": "p2", "registrants": [
            {"email": "kai.brennan@summitvale.example", "first_name": "Kai", "last_name": "Brennan", "org": "Summit & Vale Brokerage",
             "job_title": "Account Manager", "create_time": "2026-08-31T10:39:05Z"},
            {"email": "no.show@homenet.example", "first_name": "Nora", "last_name": "Show", "create_time": "2026-09-01T09:00:00Z"}]}

    participants = {"participants": [
        {"name": "Kai Brennan", "user_email": "kai.brennan@summitvale.example", "join_time": "2026-09-24T15:02:00Z",
         "leave_time": "2026-09-24T15:04:00Z", "duration": 120},
        {"name": "Kai Brennan", "user_email": "kai.brennan@summitvale.example", "join_time": "2026-09-24T15:05:00Z",
         "leave_time": "2026-09-24T15:06:00Z", "duration": 60},
        {"name": "Zoe Thornton", "user_email": "zoe.thornton@harborandmain.example", "join_time": "2026-09-24T15:10:00Z",
         "leave_time": "2026-09-24T16:00:00Z", "duration": 3000},
        {"name": "Walk In", "user_email": "walk.in@inbox.example", "join_time": "2026-09-24T15:30:00Z",
         "leave_time": "2026-09-24T15:45:00Z", "duration": 900},
        {"name": "Guest", "user_email": "", "join_time": "2026-09-24T15:00:00Z", "leave_time": "2026-09-24T15:01:00Z", "duration": 60}]}
    vtt = "WEBVTT\n\n1\n00:00:01.000 --> 00:00:04.000\nWelcome to the session.\n\n2\n00:00:05.000 --> 00:00:08.000\nLet's begin.\n"
    return Api(("POST", r"zoom\.us/oauth/token$", token),
               ("GET", r"api\.zoom\.us/v2/webinars/81234567890$", {"id": 81234567890, "topic": "Climate-ready home protection",
                                                                   "start_time": "2026-09-24T15:00:00Z", "duration": 60,
                                                                   "host_email": "events@meridian-mutual.example", "agenda": "Q&A"}),
               ("GET", r"/webinars/81234567890/panelists$", {"panelists": [{"name": "Dr. Asha Raman", "email": "asha@meridian-mutual.example"}]}),
               ("GET", r"/webinars/81234567890/registrants$", registrants),
               ("GET", r"/report/webinars/81234567890$", {"start_time": "2026-09-24T15:00:00Z", "participants_count": 4}),
               ("GET", r"/report/webinars/81234567890/participants$", participants),
               ("GET", r"/report/webinars/81234567890/qa$", {"questions": [
                   {"email": "zoe.thornton@harborandmain.example", "question_details": [{"question": "Q1"}, {"question": "Q2"}]}]}),
               ("GET", r"/report/webinars/81234567890/polls$", (404, {"code": 3001, "message": "No polls"})),
               ("GET", r"/meetings/81234567890/recordings$", {"share_url": "https://zoom.us/rec/share/abc", "recording_files": [
                   {"file_type": "MP4", "download_url": "https://zoom.us/rec/download/a.mp4"},
                   {"file_type": "TRANSCRIPT", "download_url": "https://zoom.us/rec/download/a.vtt"}]}),
               ("GET", r"zoom\.us/rec/download/a\.vtt$", vtt))


def test_zoom_attendees_match_local_columns(hub):
    api = zoom_api()
    conn = make(hub, "webinar", "zoom", api, account_id="acct-1", client_id="zoom-client", client_secret="zoom-secret",
                webinar_id="81234567890")
    df = conn.attendees()
    local = hub.get("webinar", "csv").attendees()
    assert list(df.columns) == list(local.columns)
    rows = {r["email"]: r for r in df.to_dict("records")}
    assert set(rows) == {"kai.brennan@summitvale.example", "no.show@homenet.example", "zoe.thornton@harborandmain.example",
                         "walk.in@inbox.example"}
    kai = rows["kai.brennan@summitvale.example"]
    assert (kai["attended"], kai["join_minute"], kai["leave_minute"], kai["minutes_attended"]) == (1, 2.0, 6.0, 3.0)
    assert kai["company"] == "Summit & Vale Brokerage" and kai["registered_at"] == "2026-08-31T10:39:05Z"
    assert rows["zoe.thornton@harborandmain.example"]["questions_asked"] == 2
    assert rows["no.show@homenet.example"]["attended"] == 0 and np.isnan(rows["no.show@homenet.example"]["join_minute"])
    walk = rows["walk.in@inbox.example"]
    assert (walk["first_name"], walk["last_name"]) == ("Walk", "In") and pd.isna(walk["registered_at"])  # attended, never registered
    assert df.attrs["not_provided"] == ["resources_clicked", "poll_responses"] and df.attrs["anonymous_participants"] == 1
    assert all(c.headers["authorization"] == "Bearer zoom-token" for c in api.calls if "api.zoom.us" in str(c.url))
    assert len(api.find("POST", r"oauth/token$")) == 1
    info = conn.info()
    assert set(hub.get("webinar", "csv").info()) <= set(info)  # same keys as info.json, plus extras
    assert info["title"] == "Climate-ready home protection" and info["speakers"] == [{"name": "Dr. Asha Raman", "title": ""}]
    assert info["recording_url"] == "https://zoom.us/rec/share/abc" and info["duration_minutes"] == 60 and info["date"] == "2026-09-24"
    assert conn.transcript() == "Welcome to the session.\nLet's begin."


def test_zoom_uuid_double_encoding():
    from caf.connectors.misc_providers import _zoom_id

    assert _zoom_id("81234567890") == "81234567890"
    assert _zoom_id("/ajXp112QmuoKj4854875==") == "%252FajXp112QmuoKj4854875%253D%253D"


# ----------------------------------------------------------------------------- translation

SEGMENTS = ["Hello {name}, visit https://www.meridian-mutual.example/flood and protect your home with Meridian Mutual cover.",
            "R&D <b>matters</b>"]


def test_deepl_protects_placeholders_dnt_and_glossary(hub):
    def handler(req):
        b = body(req)
        assert req.headers["authorization"] == "DeepL-Auth-Key dl-key:fx"
        assert (b["source_lang"], b["target_lang"], b["tag_handling"], b["ignore_tags"], b["formality"]) == ("EN", "DE", "xml", ["keep"], "prefer_more")
        assert "<keep>{name}</keep>" in b["text"][0] and "<keep>https://www.meridian-mutual.example/flood</keep>" in b["text"][0]
        assert "<keep>Meridian Mutual</keep>" in b["text"][0] and "<keep>Versicherungsschutz</keep>" in b["text"][0]
        assert b["text"][1] == "R&amp;D <keep>&lt;b&gt;</keep>matters<keep>&lt;/b&gt;</keep>"
        out = [t.replace("Hello", "Hallo").replace("visit", "besuchen").replace("and protect your home with", "und schützen Sie Ihr Haus mit")
               .replace("matters", "zählt") for t in b["text"]]
        return {"translations": [{"detected_source_language": "EN", "text": t} for t in out]}

    api = Api(("POST", r"https://api-free\.deepl\.com/v2/translate$", handler))
    conn = make(hub, "translation", "deepl", api, api_key="dl-key:fx")
    out = conn.translate(SEGMENTS, source="en-GB", target="de", glossary={"cover": "Versicherungsschutz"}, dnt=["Meridian Mutual"],
                         tone="formal, reassuring")
    assert out == ["Hallo {name}, besuchen https://www.meridian-mutual.example/flood und schützen Sie Ihr Haus mit Meridian Mutual "
                   "Versicherungsschutz.", "R&D <b>zählt</b>"]
    from caf.connectors.misc_providers import _deepl_lang

    assert [_deepl_lang(c, True) for c in ("en", "en-GB", "pt", "pt-BR", "zh-TW", "fr-CA")] == ["EN-US", "EN-GB", "PT-PT", "PT-BR", "ZH-HANT", "FR"]


def test_azure_translator_notranslate_spans(hub):
    def handler(req):
        assert req.headers["ocp-apim-subscription-key"] == "az-key" and req.headers["ocp-apim-subscription-region"] == "westeurope"
        assert dict(req.url.params) == {"api-version": "3.0", "from": "en", "to": "fr-ca", "textType": "html"}
        items = body(req)
        assert '<span class="notranslate">{name}</span>' in items[0]["Text"]
        assert '<span class="notranslate">Meridian Mutual</span>' in items[0]["Text"]
        return [{"translations": [{"text": i["Text"].replace("Hello", "Bonjour").replace("matters", "compte"), "to": "fr-ca"}]} for i in items]

    api = Api(("POST", r"api\.cognitive\.microsofttranslator\.com/translate$", handler))
    conn = make(hub, "translation", "azure", api, api_key="az-key", region="westeurope")
    out = conn.translate(SEGMENTS, source="en", target="fr-CA", dnt=["Meridian Mutual"])
    assert out[0].startswith("Bonjour {name}, visit https://www.meridian-mutual.example/flood") and "Meridian Mutual cover." in out[0]
    assert out[1] == "R&D <b>compte</b>"


# ----------------------------------------------------------------------------- warehouse: SQLAlchemy

def test_sqlalchemy_warehouse_read_only(hub, tmp_path):
    sa = pytest.importorskip("sqlalchemy")
    db = tmp_path / "wh.sqlite"
    engine = sa.create_engine(f"sqlite:///{db}")
    with engine.begin() as c:
        c.execute(sa.text("CREATE TABLE policies (id INTEGER, product TEXT, premium REAL, renewal_time TEXT)"))
        c.execute(sa.text("INSERT INTO policies VALUES (1,'home',420.5,'10:30'),(2,'car',380.0,'11:00'),(3,'home',510.0,'09:15')"))
    conn = make(hub, "warehouse", "sqlalchemy", None, url=f"sqlite:///{db}")
    assert conn.dialect == "sqlite" and conn.tables() == {"policies": [("id", "INTEGER"), ("product", "TEXT"), ("premium", "REAL"),
                                                                       ("renewal_time", "TEXT")]}
    df = conn.query("SELECT product, COUNT(*) AS n, ROUND(AVG(premium), 2) AS avg_premium FROM policies GROUP BY product ORDER BY product;")
    assert df.to_dict("records") == [{"product": "car", "n": 1, "avg_premium": 380.0}, {"product": "home", "n": 2, "avg_premium": 465.25}]
    assert conn.query("SELECT renewal_time FROM policies WHERE renewal_time = '10:30'")["renewal_time"].tolist() == ["10:30"]
    assert len(conn.query("WITH p AS (SELECT * FROM policies) SELECT id FROM p", max_rows=2)) == 2
    assert conn.query("SELECT id FROM policies WHERE product = 'update'").empty  # keywords inside literals are fine
    for bad in ("DELETE FROM policies", "SELECT 1; DROP TABLE policies", "UPDATE policies SET premium = 0",
                "WITH x AS (SELECT 1) INSERT INTO policies VALUES (9,'x',1,'')"):
        with pytest.raises(ConnectorError):
            conn.query(bad)
    assert conn.query("SELECT COUNT(*) AS n FROM policies")["n"].tolist() == [3]
    assert make(hub, "warehouse", "sqlalchemy", None, url="postgresql+psycopg://u:p@db.example/analytics").dialect == "postgres"
    assert make(hub, "warehouse", "sqlalchemy", None, url="snowflake://u@acct/db").dialect == "snowflake"


def test_sqlalchemy_missing_is_clear(hub, monkeypatch):
    monkeypatch.setitem(sys.modules, "sqlalchemy", None)
    conn = make(hub, "warehouse", "sqlalchemy", None, url="sqlite:///x.db")
    with pytest.raises(ConnectorNotConfigured, match="pip install"):
        conn.tables()


# ----------------------------------------------------------------------------- vector store: Qdrant

def test_qdrant_upsert_count_and_search(hub):
    store: dict[str, dict] = {}

    def points(req):
        for p in body(req)["points"]:
            store[p["id"]] = p
        return {"result": {"operation_id": 1, "status": "completed"}, "status": "ok"}

    def query(req):
        b = body(req)
        assert len(b["query"]) == 512 and b["limit"] == 2 and b["with_payload"] is True
        assert b["filter"] == {"must": [{"key": "metadata.type", "match": {"value": "faq"}},
                                        {"key": "metadata.lang", "match": {"any": ["en", "fr"]}}]}
        hits = sorted(store.values(), key=lambda p: -sum(a * q for a, q in zip(p["vector"], b["query"])))[:2]
        return {"result": {"points": [{"id": h["id"], "version": 1, "score": 0.91234, "payload": h["payload"]} for h in hits]}, "status": "ok"}

    api = Api(("GET", r"localhost:6333/collections/caf_quotes$", lambda r: (200, {"result": {}}) if store else (404, {})),
              ("PUT", r"/collections/caf_quotes$", {"result": True}), ("PUT", r"/collections/caf_quotes/points$", points),
              ("POST", r"/collections/caf_quotes/points/count$", lambda r: {"result": {"count": len(store)}}),
              ("POST", r"/collections/caf_quotes/points/query$", query))
    conn = make(hub, "vectorstore", "qdrant", api, url="http://localhost:6333", api_key="qd-key")
    docs = [{"id": "q1", "text": "Flood cover protects your home from storm water", "metadata": {"type": "faq", "lang": "en"}},
            {"id": "q2", "text": "Car insurance renewal reminders", "metadata": {"type": "faq", "lang": "fr"}}]
    assert conn.upsert("quotes", docs) == 2
    assert body(api.one("PUT", r"/collections/caf_quotes$")) == {"vectors": {"size": 512, "distance": "Cosine"}}
    pts = body(api.one("PUT", r"/points$"))["points"]
    assert api.one("PUT", r"/points$").url.params["wait"] == "true" and api.calls[0].headers["api-key"] == "qd-key"
    assert str(__import__("uuid").UUID(pts[0]["id"])) == pts[0]["id"] and pts[0]["payload"]["doc_id"] == "q1"
    hits = conn.search("quotes", "storm flood home", k=2, filters={"type": "faq", "lang": ["en", "fr"]})
    assert hits[0] == {"id": "q1", "text": docs[0]["text"], "metadata": docs[0]["metadata"], "score": 0.9123}
    assert set(hits[0]) == {"id", "text", "metadata", "score"}  # same keys as the tfidf provider
    assert conn.upsert("quotes", [{"id": "q1", "text": "updated", "embedding": [0.1] * 512}]) == 2  # same id -> same point
    assert len(api.find("PUT", r"/collections/caf_quotes$")) == 1  # collection created once
    legacy = make(hub, "vectorstore", "qdrant", Api(("POST", r"/points/search$", lambda r: {"result": [
        {"id": "x", "score": 0.5, "payload": {"doc_id": "q9", "text": "t", "metadata": {}}}]})), url="http://q.example", search_api="search")
    assert legacy.search("quotes", "x", vector=[1.0, 0.0]) == [{"id": "q9", "text": "t", "metadata": {}, "score": 0.5}]


def test_hashed_vector_is_deterministic_and_normalised():
    from caf.connectors.misc_providers import hashed_vector

    a, b = hashed_vector("flood cover for homes"), hashed_vector("flood cover for homes")
    assert a == b and abs(sum(v * v for v in a) - 1) < 1e-4 and len(a) == 512
    assert sum(x * y for x, y in zip(a, hashed_vector("home flood cover"))) > sum(x * y for x, y in zip(a, hashed_vector("pet travel")))


# ----------------------------------------------------------------------------- CRM

CRM_COLS = ["contact_id", "email", "first_name", "last_name", "company", "job_title", "industry", "country", "lifecycle_stage"]


def test_crm_csv_sample_and_segments(hub):
    crm = hub.get("crm")  # default provider without any settings change
    assert crm.provider == "csv" and not crm.external
    df = crm.contacts(limit=1000)
    assert list(df.columns) == CRM_COLS and 55 <= len(df) <= 65 and df["email"].str.endswith(".example").all()
    assert df["email"].is_unique and set(df["lifecycle_stage"]) <= {"subscriber", "lead", "mql", "sql", "opportunity", "customer"}
    ins = crm.contacts({"industry": "insurance brokerage", "lifecycle_stage": ["MQL", "marketingqualifiedlead", "sql"]})
    assert len(ins) and set(ins["industry"]) == {"Insurance Brokerage"} and set(ins["lifecycle_stage"]) <= {"mql", "sql"}
    assert crm.contacts("industry=Insurance Brokerage;lifecycle_stage=mql,sql").equals(ins)
    assert set(crm.contacts("prospects")["lifecycle_stage"]) <= {"lead", "mql", "sql", "opportunity"}
    assert len(crm.contacts(limit=3)) == 3
    with pytest.raises(ConnectorError, match="Unknown CRM segment"):
        crm.contacts("vip-list")
    with pytest.raises(ConnectorError, match="Unknown CRM segment field"):
        crm.contacts({"favourite_colour": "blue"})
    attendees = set(hub.get("webinar", "csv").attendees()["email"])
    assert len(attendees & set(df["email"])) >= 10  # joinable with webinar data
    none = make(hub, "crm", "none")
    assert none.available is False and list(none.contacts().columns) == CRM_COLS


def test_salesforce_soql_client_credentials_and_paging(hub):
    def token(req):
        assert form(req) == {"grant_type": "client_credentials", "client_id": "sf-client", "client_secret": "sf-secret"}
        return {"access_token": "sf-token", "instance_url": "https://meridian.my.salesforce.com", "token_type": "Bearer"}

    page1 = {"done": False, "nextRecordsUrl": "/services/data/v62.0/query/01gX-2000", "records": [
        {"attributes": {"type": "Contact"}, "Id": "003A", "Email": "kai.brennan@summitvale.example", "FirstName": "Kai", "LastName": "Brennan",
         "Title": "Account Manager", "MailingCountry": "US", "Lifecycle_Stage__c": "Marketing Qualified Lead",
         "Account": {"Name": "Summit & Vale Brokerage", "Industry": "Insurance"}}]}
    page2 = {"done": True, "records": [{"Id": "003B", "Email": "x@obrien.example", "FirstName": "Sean", "LastName": "O'Brien",
                                         "Title": None, "MailingCountry": "IE", "Lifecycle_Stage__c": "customer", "Account": None}]}
    api = Api(("POST", r"meridian\.my\.salesforce\.com/services/oauth2/token$", token),
              ("GET", r"/services/data/v62\.0/query$", page1), ("GET", r"/services/data/v62\.0/query/01gX-2000$", page2))
    conn = make(hub, "crm", "salesforce", api, login_url="https://meridian.my.salesforce.com", client_id="sf-client", client_secret="sf-secret",
                field_map={"lifecycle_stage": "Lifecycle_Stage__c"}, lifecycle_values={"mql": ["Marketing Qualified Lead"]})
    df = conn.contacts({"industry": ["Insurance", "O'Brien & Co"], "lifecycle_stage": "mql"}, limit=50)
    q = api.one("GET", r"/query$")
    assert q.headers["authorization"] == "Bearer sf-token"
    assert q.url.params["q"] == ("SELECT Id, Email, FirstName, LastName, Account.Name, Title, Account.Industry, MailingCountry, Lifecycle_Stage__c "
                                 "FROM Contact WHERE Email != null AND Account.Industry IN ('Insurance', 'O\\'Brien & Co') "
                                 "AND Lifecycle_Stage__c IN ('Marketing Qualified Lead') LIMIT 50")
    assert list(df.columns) == CRM_COLS and df["contact_id"].tolist() == ["003A", "003B"]
    assert df.iloc[0][["company", "industry", "lifecycle_stage"]].tolist() == ["Summit & Vale Brokerage", "Insurance", "mql"]  # normalised
    assert df.iloc[1][["company", "job_title", "lifecycle_stage"]].tolist() == ["", "", "customer"]
    lead = make(hub, "crm", "salesforce", None, access_token="t", instance_url="https://x.my.salesforce.com", object="Lead", require_email=False)
    assert lead.soql({"lifecycle_stage": "sql"}, 10) == ("SELECT Id, Email, FirstName, LastName, Company, Title, Industry, Country, Status "
                                                          "FROM Lead WHERE Status IN ('sql') LIMIT 10")
    with pytest.raises(ConnectorError, match="field_map"):
        make(hub, "crm", "salesforce", None, access_token="t", instance_url="https://x").soql({"lifecycle_stage": "mql"})


def test_hubspot_contacts_search(hub):
    def search(req):
        b = body(req)
        assert req.headers["authorization"] == "Bearer pat-na1-123"
        assert b["filterGroups"] == [{"filters": [{"propertyName": "industry", "operator": "IN", "values": ["insurance", "real estate"]},
                                                  {"propertyName": "lifecyclestage", "operator": "EQ", "value": "marketingqualifiedlead"}]}]
        assert "lifecyclestage" in b["properties"] and "jobtitle" in b["properties"]
        if b.get("after") == "2":
            return {"results": [{"id": "202", "properties": {"email": "b@corvanehealth.example", "lifecyclestage": "salesqualifiedlead"}}]}
        return {"results": [{"id": "201", "properties": {"hs_object_id": "201", "email": "a@summitvale.example", "firstname": "Ana",
                                                        "lastname": "Lee", "company": "Summit & Vale", "jobtitle": "CMO",
                                                        "industry": "Insurance", "country": "US", "lifecyclestage": "marketingqualifiedlead"}}],
                "paging": {"next": {"after": "2"}}}

    api = Api(("POST", r"api\.hubapi\.com/crm/v3/objects/contacts/search$", search))
    conn = make(hub, "crm", "hubspot", api, access_token="pat-na1-123")
    df = conn.contacts({"industry": ["Insurance", "Real Estate"], "lifecycle_stage": "mql"}, limit=10)
    assert list(df.columns) == CRM_COLS and df["contact_id"].tolist() == ["201", "202"]
    assert df["lifecycle_stage"].tolist() == ["mql", "sql"] and df.iloc[1]["first_name"] == ""
    assert [body(r).get("after") for r in api.calls] == [None, "2"] and body(api.calls[0])["limit"] == 10
