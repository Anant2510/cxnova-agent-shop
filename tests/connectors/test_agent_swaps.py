"""No vendor lock-in, end to end: real agents run unchanged when settings swap a local provider for an
external adapter.

Each mock API *replays the demo sample data* the way the vendor would serve it (GA4 runReport,
Adobe Analytics 2.0 reports, Optimizely v2 results, Zoom reports, Qdrant), so the same agent run on
the local provider and on the external adapter must reach the same result. Only the transport is
mocked; settings, hub, adapters and agents are the production code paths. Agents that are not
registered yet are skipped.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from caf.config import load_settings
from caf.core import RunContext, get_agent
from mockapi import Api, body, ims_route

SAMPLE = Path(__file__).resolve().parents[2] / "sample_data"  # not get_settings(): no settings cached at collection time


def run_agent(agent_id: str, swaps: dict[str, tuple[dict[str, Any], Api]] | None = None, inputs: dict[str, Any] | None = None):
    try:
        agent = get_agent(agent_id)
    except KeyError:
        pytest.skip(f"agent {agent_id} not registered yet")
    settings = load_settings()
    for kind, (cfg, api) in (swaps or {}).items():
        settings.connectors[kind] = {**cfg, "retry_backoff_s": 0, "_transport": api.transport}
    result = agent.run(inputs or agent.selftest_input(), RunContext.create(settings=settings))
    assert result.status != "error", result.error
    return result


def provider_used(result, kind: str) -> str:
    return next(c.split(":", 1)[1] for c in result.provenance["connectors"] if c.startswith(kind + ":"))


# ----------------------------------------------------------------------------- analytics replays

DAILY, PAGES, CHANNELS = (pd.read_csv(SAMPLE / "analytics" / f"{n}.csv") for n in ("daily", "pages", "channels"))
for _df in (DAILY, PAGES):
    _df["bounces"] = _df["sessions"] - _df["engaged_sessions"]
GA4_METRICS = {"sessions": "sessions", "totalUsers": "users", "newUsers": "new_users", "engagedSessions": "engaged_sessions",
               "keyEvents": "conversions", "totalRevenue": "revenue", "userEngagementDuration": "engagement_time_s"}
GA4_DIMS = {"date": "date", "pagePath": "page_path", "pageTitle": "page_title", "sessionDefaultChannelGroup": "channel"}


def ga4_replay(req):
    """POST properties/{id}:runReport answered from the sample CSVs (all-zero rows omitted, as GA4 does)."""
    b = body(req)
    start, end = b["dateRanges"][0]["startDate"], b["dateRanges"][0]["endDate"]
    dims, mets = [d["name"] for d in b["dimensions"]], [m["name"] for m in b["metrics"]]
    src = PAGES if "pagePath" in dims else CHANNELS if "sessionDefaultChannelGroup" in dims else DAILY
    df = src[(src["date"] >= start) & (src["date"] <= end)].groupby([GA4_DIMS[d] for d in dims], as_index=False)[[GA4_METRICS[m] for m in mets]].sum()
    df = df[(df[[GA4_METRICS[m] for m in mets]] != 0).any(axis=1)]
    if b.get("orderBys"):
        ob = b["orderBys"][0]
        key = GA4_METRICS[ob["metric"]["metricName"]] if "metric" in ob else GA4_DIMS[ob["dimension"]["dimensionName"]]
        df = df.sort_values(key, ascending=not ob.get("desc"))
    total, off = len(df), int(b.get("offset", 0))
    df = df.iloc[off: off + int(b.get("limit", 10_000))]
    rows = [{"dimensionValues": [{"value": str(r[GA4_DIMS[d]]).replace("-", "") if d == "date" else str(r[GA4_DIMS[d]])} for d in dims],
             "metricValues": [{"value": str(r[GA4_METRICS[m]])} for m in mets]} for _, r in df.iterrows()]
    return {"dimensionHeaders": [{"name": d} for d in dims], "metricHeaders": [{"name": m} for m in mets], "rows": rows, "rowCount": total}


AA_METRICS = {"metrics/visits": "sessions", "metrics/visitors": "users", "metrics/orders": "conversions", "metrics/revenue": "revenue",
              "metrics/bounces": "bounces", "metrics/timespent": "engagement_time_s", "cm_new_visitors": "new_users"}
CHANNEL_ITEMS = {str(i): c for i, c in enumerate(sorted(CHANNELS["channel"].unique()))}
CHANNEL_KEYS = {c: i for i, c in CHANNEL_ITEMS.items()}


def adobe_replay(req):
    """POST /api/{company}/reports answered from the sample CSVs (daterangeday, page, marketingchannel, breakdowns)."""
    b = body(req)
    start, end_excl = (part[:10] for part in b["globalFilters"][0]["dateRange"].split("/"))
    end = (date.fromisoformat(end_excl) - timedelta(days=1)).isoformat()
    mets, dim = b["metricContainer"]["metrics"], b["dimension"]
    if dim == "variables/daterangeday":
        filters = {f["id"]: f for f in b["metricContainer"].get("metricFilters", [])}
        rows = []
        for day in pd.date_range(start, end):
            ds, data = day.date().isoformat(), []
            for m in mets:
                if m.get("filters"):
                    sub = CHANNELS[(CHANNELS["date"] == ds) & (CHANNELS["channel"] == CHANNEL_ITEMS[filters[m["filters"][0]]["itemId"]])]
                else:
                    sub = DAILY[DAILY["date"] == ds]
                data.append(float(sub[AA_METRICS[m["id"]]].sum()))
            rows.append({"itemId": f"{day.year - 1900}{day.month:02d}{day.day:02d}", "value": f"{day:%b} {day.day}, {day.year}", "data": data})
        return {"rows": rows, "lastPage": True}
    src, key = (PAGES, "page_path") if dim == "variables/page" else (CHANNELS, "channel")
    df = src[(src["date"] >= start) & (src["date"] <= end)].groupby(key, as_index=False)[[AA_METRICS[m["id"]] for m in mets]].sum()
    df = df.sort_values(AA_METRICS[mets[0]["id"]], ascending=False).head(int(b["settings"]["limit"]))
    return {"rows": [{"itemId": CHANNEL_KEYS.get(r[key], str(i)), "value": r[key], "data": [float(r[AA_METRICS[m["id"]]]) for m in mets]}
                     for i, (_, r) in enumerate(df.iterrows())], "lastPage": True}


ANALYTICS_SWAPS = {
    "ga4": lambda: {"analytics": ({"provider": "ga4", "property_id": "123456", "access_token": "token"}, Api(("POST", r":runReport$", ga4_replay)))},
    "adobe_analytics": lambda: {"analytics": ({"provider": "adobe_analytics", "company_id": "coforge1", "rsid": "meridianprod", "client_id": "id",
                                               "client_secret": "secret", "metric_map": {"new_users": "cm_new_visitors"}},
                                              Api(ims_route(), ("POST", r"/api/coforge1/reports$", adobe_replay)))},
}


@pytest.mark.parametrize("provider", sorted(ANALYTICS_SWAPS))
def test_traffic_report_identical_on_ga4_and_adobe(provider):
    local = run_agent("ga4_traffic_report")
    swapped = run_agent("ga4_traffic_report", ANALYTICS_SWAPS[provider]())
    assert (provider_used(local, "analytics"), provider_used(swapped, "analytics")) == ("csv", provider)
    assert swapped.summary == local.summary  # same sessions, deltas, CVR and the 19-20 Aug tracking anomaly
    for key in ("kpis", "anomalies", "channels"):
        if key in local.output:
            assert json.dumps(swapped.output[key], sort_keys=True, default=str) == json.dumps(local.output[key], sort_keys=True, default=str), key


@pytest.mark.parametrize("provider", sorted(ANALYTICS_SWAPS))
def test_experiment_planning_runs_on_external_analytics(provider):
    local = run_agent("experiment_planning")
    swapped = run_agent("experiment_planning", ANALYTICS_SWAPS[provider]())
    assert provider_used(swapped, "analytics") == provider and swapped.summary == local.summary


# ----------------------------------------------------------------------------- experiments: Optimizely replay

REGISTRY = json.loads((SAMPLE / "experiments" / "registry.json").read_text())["experiments"]
TO_OPTIMIZELY = {"draft": "not_started", "running": "running", "paused": "paused", "completed": "concluded", "archived": "archived"}


def optimizely_experiment(e: dict[str, Any]) -> dict[str, Any]:
    return {"id": e["id"], "project_id": 1, "name": e["name"], "status": TO_OPTIMIZELY[e["status"]], "description": e.get("hypothesis", ""),
            "holdback": int(round((100 - e.get("traffic_allocation", 100)) * 100)), "earliest": e.get("start_date"), "latest": e.get("end_date"),
            "url_targeting": {"edit_url": e.get("page_url", "")},
            "variations": [{"variation_id": v["id"], "name": v["name"], "weight": int(v["allocation"] * 100)} for v in e.get("variants", [])],
            "metrics": [{"event_id": (e.get("primary_metric") or {}).get("event") or "conversion", "aggregator": "unique"}]}


def optimizely_results(req):
    exp_id = str(req.url).split("/experiments/")[1].split("/")[0]
    path = SAMPLE / "experiments" / "results" / f"{exp_id}.csv"
    if not path.exists():
        return 204, ""
    df = pd.read_csv(path)
    df = df[df["segment"] == req.url.params.get("device")]
    return {"metrics": [{"event_id": "conversion", "results": {r.variant: {"value": r.conversions} for r in df.itertuples()}},
                        {"event_id": "purchase", "field": "revenue", "results": {r.variant: {"value": r.revenue * 100} for r in df.itertuples()}}],
            "reach": {"variations": {r.variant: {"count": r.visitors} for r in df.itertuples()}}}


def optimizely_swap():
    def one(req):
        found = next((optimizely_experiment(e) for e in REGISTRY if str(req.url).endswith("/" + e["id"])), None)
        return found if found else (404, {})

    api = Api(("GET", r"api\.optimizely\.com/v2/experiments$", lambda r: [optimizely_experiment(e) for e in REGISTRY] if r.url.params["page"] == "1" else []),
              ("GET", r"/v2/experiments/[^/]+$", one), ("GET", r"/v2/experiments/[^/]+/results$", optimizely_results))
    return {"experiments": ({"provider": "optimizely", "api_token": "token", "project_id": 1, "results_segments": {"device": ["desktop", "mobile"]}}, api)}


def test_experiment_summary_identical_on_optimizely():
    local = run_agent("experiment_summary")
    swapped = run_agent("experiment_summary", optimizely_swap())
    assert provider_used(swapped, "experiments") == "optimizely"
    assert swapped.summary == local.summary  # same SRM-checked stats, lift, p-value and decision


# ----------------------------------------------------------------------------- webinar: Zoom replay

ATTENDEES = pd.read_csv(SAMPLE / "webinar" / "attendees.csv")
INFO = json.loads((SAMPLE / "webinar" / "info.json").read_text())


def zoom_swap():
    start = datetime.fromisoformat(f"{INFO['date']}T15:00:00")
    stamp = lambda minutes: (start + timedelta(minutes=float(minutes))).isoformat() + "Z"  # noqa: E731
    registrants = [{"email": r.email, "first_name": r.first_name, "last_name": r.last_name, "org": r.company, "job_title": r.job_title,
                    "create_time": r.registered_at} for r in ATTENDEES.itertuples()]
    participants = [{"name": f"{r.first_name} {r.last_name}", "user_email": r.email, "join_time": stamp(r.join_minute),
                     "leave_time": stamp(r.leave_minute), "duration": int(r.minutes_attended * 60)} for r in ATTENDEES.itertuples() if r.attended]
    counts = lambda col: {"questions": [{"email": r.email, "question_details": [{}] * int(getattr(r, col))}  # noqa: E731
                                        for r in ATTENDEES.itertuples() if getattr(r, col)]}
    api = Api(("POST", r"zoom\.us/oauth/token$", {"access_token": "zoom", "expires_in": 3599}),
              ("GET", r"/v2/webinars/[^/]+$", {"id": INFO["webinar_id"], "topic": INFO["title"], "start_time": f"{INFO['date']}T15:00:00Z",
                                                "duration": INFO["duration_minutes"], "host_email": "events@meridian-mutual.example"}),
              ("GET", r"/panelists$", {"panelists": [{"name": s["name"]} for s in INFO["speakers"]]}),
              ("GET", r"/registrants$", {"registrants": registrants}), ("GET", r"/report/webinars/[^/]+$", {"start_time": f"{INFO['date']}T15:00:00Z"}),
              ("GET", r"/participants$", {"participants": participants}), ("GET", r"/qa$", counts("questions_asked")),
              ("GET", r"/polls$", counts("poll_responses")), ("GET", r"/recordings$", (404, {})))
    return {"webinar": ({"provider": "zoom", "account_id": "acct", "client_id": "id", "client_secret": "secret", "webinar_id": INFO["webinar_id"]}, api)}


def test_webinar_followup_segments_identical_on_zoom():
    local = run_agent("webinar_followup_email")
    swapped = run_agent("webinar_followup_email", zoom_swap())
    assert provider_used(swapped, "webinar") == "zoom"
    seg = lambda r: [(s.get("segment"), s.get("count")) for s in r.output.get("segments", [])]  # noqa: E731
    assert seg(swapped) == seg(local) and swapped.output.get("mql_count") == local.output.get("mql_count")
    assert swapped.output.get("registered") == local.output.get("registered") == len(ATTENDEES)


# ----------------------------------------------------------------------------- behaviour + translation: degrade, never crash

HOME = "https://www.meridian-mutual.example/products/home-insurance.html"
CLARITY_EXPORT = [
    {"metricName": "Traffic", "information": [{"totalSessionCount": "120", "URL": HOME, "Device": "PC"},
                                              {"totalSessionCount": "80", "URL": HOME, "Device": "Mobile"}]},
    {"metricName": "DeadClickCount", "information": [{"sessionsCount": "14", "subTotal": "31", "URL": HOME, "Device": "PC"}]},
    {"metricName": "RageClickCount", "information": [{"sessionsCount": "6", "subTotal": "19", "URL": HOME, "Device": "Mobile"}]},
    {"metricName": "ScrollDepth", "information": [{"averageScrollDepth": 41.0, "URL": HOME, "Device": "PC"},
                                                  {"averageScrollDepth": 33.5, "URL": HOME, "Device": "Mobile"}]},
]  # Clarity reports ScrollDepth per URL x device; a device without it leaves heatmap_analysis an all-NaN scroll median


def test_heatmap_runs_on_clarity_aggregates():
    """Clarity has no raw clicks: the agent must still complete (its click sections degrade), not crash."""
    swapped = run_agent("heatmap_analysis", {"behaviour": ({"provider": "clarity", "api_token": "token"},
                                                           Api(("GET", r"clarity\.ms/export-data/api/v1/project-live-insights$", CLARITY_EXPORT)))})
    assert provider_used(swapped, "behaviour") == "clarity" and swapped.summary


def test_content_translation_runs_on_deepl():
    def deepl(req):  # echo translator: keeps the protected <keep> spans exactly as sent
        return {"translations": [{"detected_source_language": "EN", "text": t} for t in body(req)["text"]]}

    swapped = run_agent("content_translation", {"translation": ({"provider": "deepl", "api_key": "key:fx"},
                                                                Api(("POST", r"api-free\.deepl\.com/v2/translate$", deepl)))})
    assert provider_used(swapped, "translation") == "deepl"
    qa = re.search(r"QA (\d+)/(\d+) passed", swapped.summary)
    assert qa and qa.group(1) == qa.group(2)  # every QA check passes: placeholders, tags and DNT terms survive the round trip


# ----------------------------------------------------------------------------- vector store: in-memory Qdrant

def qdrant_swap():
    collections: dict[str, dict[str, Any]] = {}

    def name_of(req) -> str:
        return str(req.url).split("/collections/")[1].split("/")[0]

    def collection(req):
        if req.method == "PUT":
            collections[name_of(req)] = {}
            return {"result": True}
        return (200, {"result": {}}) if name_of(req) in collections else (404, {})

    def upsert(req):
        collections[name_of(req)].update({p["id"]: p for p in body(req)["points"]})
        return {"result": {}}

    def keep(payload: dict[str, Any], must: list[dict[str, Any]]) -> bool:
        for cond in must:
            value = payload.get("metadata", {}).get(cond["key"].split(".", 1)[1])
            wanted = cond["match"].get("any", [cond["match"].get("value")])
            if not (value in wanted or (isinstance(value, list) and set(value) & set(wanted))):
                return False
        return True

    def query(req):
        b = body(req)
        dot = lambda p: sum(x * y for x, y in zip(p["vector"], b["query"]))  # noqa: E731
        hits = [p for p in collections.get(name_of(req), {}).values() if keep(p["payload"], (b.get("filter") or {}).get("must", []))]
        hits = sorted(hits, key=lambda p: -dot(p))[: b["limit"]]
        return {"result": {"points": [{"id": h["id"], "score": dot(h), "payload": h["payload"]} for h in hits]}}

    api = Api(("GET", r"/collections/[^/]+$", collection), ("PUT", r"/collections/[^/]+$", collection), ("PUT", r"/points$", upsert),
              ("POST", r"/points/count$", lambda r: {"result": {"count": len(collections[name_of(r)])}} if name_of(r) in collections else (404, {})),
              ("POST", r"/points/query$", query))
    return {"vectorstore": ({"provider": "qdrant", "url": "http://qdrant.internal:6333"}, api)}


def test_industry_marketing_grounds_on_qdrant():
    swapped = run_agent("industry_marketing", qdrant_swap())
    assert provider_used(swapped, "vectorstore") == "qdrant"
    assert "cited doc" in swapped.summary and "0 cited doc" not in swapped.summary  # retrieval grounded the copy
