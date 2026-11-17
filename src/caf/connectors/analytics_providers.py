"""External analytics + keyword-data providers: GA4, Adobe Analytics, Google Search Console, DataForSEO.

They return exactly the DataFrame shapes of the local CSV providers in data.py, so an agent cannot
tell (and does not need to know) which platform answered:

* Analytics.daily    -> [date, sessions, users, new_users, engaged_sessions, conversions, revenue]
* Analytics.pages    -> [page_path, page_title, sessions, users, engaged_sessions, engagement_time_s,
                         conversions, engagement_rate, avg_engagement_time_s, conversion_rate]
* Analytics.channels -> [channel, sessions, users, conversions, revenue, conversion_rate]
* channels_daily     -> [date, channel, sessions, users, conversions, revenue]
* KeywordData.queries -> [query, page, clicks, impressions, ctr, position]

Derived rates use the same formulas as CsvAnalytics (rounded identically).
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import numpy as np
import pandas as pd

from ._http import ExternalProvider, adobe_ims_token, basic_auth_header, expect_json, google_access_token
from .base import ConnectorError, connector
from .data import KEYWORD_COLUMNS, Analytics, KeywordData, _d

DAILY_COLUMNS = ["date", "sessions", "users", "new_users", "engaged_sessions", "conversions", "revenue"]
PAGE_COLUMNS = ["page_path", "page_title", "sessions", "users", "engaged_sessions", "engagement_time_s", "conversions",
                "engagement_rate", "avg_engagement_time_s", "conversion_rate"]
CHANNEL_COLUMNS = ["channel", "sessions", "users", "conversions", "revenue", "conversion_rate"]
CHANNEL_DAILY_COLUMNS = ["date", "channel", "sessions", "users", "conversions", "revenue"]


def _today() -> date:
    return datetime.now(timezone.utc).date()


# --- shared shaping (identical formulas to CsvAnalytics) -----------------------------------------

def _shape_daily(df: pd.DataFrame, start: date, end: date, *, new_users_known: bool = True) -> pd.DataFrame:
    """Daily rows exactly as the platform returned them (like rows of the CSV export) - days are never
    invented: GA4 omits days without data, which ga4_traffic_report reports as tracking gaps. An
    unmapped metric (Adobe new_users) stays NaN rather than an invented zero."""
    out = df[DAILY_COLUMNS].copy()
    out = out[(out["date"] >= start) & (out["date"] <= end)]
    if not out.empty:
        out = out.groupby("date", as_index=False).sum(min_count=1)
    ints = ["sessions", "users", "engaged_sessions", "conversions"] + (["new_users"] if new_users_known else [])
    for col in DAILY_COLUMNS[1:]:
        out[col] = np.nan if col == "new_users" and not new_users_known else pd.to_numeric(out[col], errors="coerce").fillna(0)
    for col in ints:
        out[col] = out[col].round().astype("int64")
    out["revenue"] = out["revenue"].astype(float).round(2)
    return out[DAILY_COLUMNS].sort_values("date").reset_index(drop=True)


def _shape_pages(df: pd.DataFrame, limit: int) -> pd.DataFrame:
    base = ["sessions", "users", "engaged_sessions", "engagement_time_s", "conversions"]
    if df.empty:
        return pd.DataFrame(columns=PAGE_COLUMNS)
    agg = df.groupby(["page_path", "page_title"], as_index=False)[base].sum()
    agg["engagement_rate"] = (agg["engaged_sessions"] / agg["sessions"].clip(lower=1)).round(4)
    agg["avg_engagement_time_s"] = (agg["engagement_time_s"] / agg["sessions"].clip(lower=1)).round(1)
    agg["conversion_rate"] = (agg["conversions"] / agg["sessions"].clip(lower=1)).round(4)
    return agg.sort_values("sessions", ascending=False).head(limit).reset_index(drop=True)[PAGE_COLUMNS]


def _shape_channels(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=CHANNEL_COLUMNS)
    agg = df.groupby("channel", as_index=False)[["sessions", "users", "conversions", "revenue"]].sum()
    agg["conversion_rate"] = (agg["conversions"] / agg["sessions"].clip(lower=1)).round(4)
    return agg.sort_values("sessions", ascending=False).reset_index(drop=True)[CHANNEL_COLUMNS]


def _num(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    for c in cols:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    return df


class _ExternalAnalytics(ExternalProvider, Analytics):
    external = True

    def data_days(self, since: date, until: date) -> tuple[date | None, date | None]:
        """(first, last) day with sessions in [since, until], asked of the platform."""
        return None, None

    def date_bounds(self) -> tuple[date, date]:
        """(first, last) day with data - the CSV provider's min/max semantics - asked of the platform once
        per day (cached). ``data_start`` pins the first day; the search window is GA4's whole history
        (from 2015-08-14) or, for platforms without a cheap query, the trailing ``lookback_days``
        (default 365). Falls back to that window ending yesterday when the platform returns nothing."""
        yesterday = _today() - timedelta(days=1)
        cached = self.__dict__.get("_bounds")
        if cached and cached[0] == yesterday:
            return cached[1]
        since = _d(self.config["data_start"]) if self.config.get("data_start") else self.history_start(yesterday)
        first, last = self.data_days(since, yesterday)
        last = last or yesterday
        first = _d(self.config["data_start"]) if self.config.get("data_start") else (first or since)
        bounds = (min(first, last), last)
        self.__dict__["_bounds"] = (yesterday, bounds)
        return bounds

    def history_start(self, yesterday: date) -> date:
        return yesterday - timedelta(days=int(self.config.get("lookback_days") or 365) - 1)


# ----------------------------------------------------------------------------- GA4

GA4_SCOPES = ["https://www.googleapis.com/auth/analytics.readonly"]


@connector("analytics", "ga4")
class Ga4Analytics(_ExternalAnalytics):
    """Google Analytics 4 via the Data API v1beta runReport.

    Endpoint: POST https://analyticsdata.googleapis.com/v1beta/properties/{property_id}:runReport
    (limit <= 250,000 rows per request; paged with offset).
    Auth: Google service account -> OAuth2 JWT bearer grant at https://oauth2.googleapis.com/token
    (scope analytics.readonly; signed with google-auth when importable, else ``cryptography``), or a
    pre-issued ``access_token``. Add the service account as a Viewer on the GA4 property.
    Config (connectors.analytics): property_id (GA4_PROPERTY_ID, required), credentials_file
    (GOOGLE_APPLICATION_CREDENTIALS) | credentials_json | access_token; optional conversion_metric
    (default ``keyEvents`` - GA4 renamed "conversions" to "key events" in 2024), revenue_metric
    (default ``totalRevenue``), data_start. date_bounds() asks GA4 for its first and last day with data
    (two 1-row reports, cached per day).
    Mapping: daily = date x sessions, totalUsers, newUsers, engagedSessions, keyEvents, totalRevenue;
    pages = pagePath, pageTitle x sessions, totalUsers, engagedSessions, userEngagementDuration (s),
    keyEvents; channels = sessionDefaultChannelGroup x sessions, totalUsers, keyEvents, totalRevenue.
    ``domain`` filters on hostName (EXACT).
    """

    external = True
    required = ("property_id",)
    base_url = "https://analyticsdata.googleapis.com/v1beta"
    retry_post = True  # runReport is read-only
    page_size = 100_000

    @property
    def available(self) -> bool:
        cfg = self.config
        return bool(cfg.get("property_id") and (cfg.get("credentials_file") or cfg.get("credentials_json") or cfg.get("access_token")))

    @property
    def _conv(self) -> str:
        return str(self.config.get("conversion_metric") or "keyEvents")

    @property
    def _rev(self) -> str:
        return str(self.config.get("revenue_metric") or "totalRevenue")

    def run_report(self, dimensions: list[str], metrics: list[str], start: date, end: date, *, domain: str | None = None,
                   order_by: str | None = None, order_by_dimension: str | None = None, descending: bool = False,
                   limit: int | None = None) -> pd.DataFrame:
        """Raw runReport rows as a DataFrame named by GA4 API names (date parsed, metrics numeric)."""
        pid = str(self.require("property_id")["property_id"]).removeprefix("properties/")
        body: dict[str, Any] = {"dateRanges": [{"startDate": _d(start).isoformat(), "endDate": _d(end).isoformat()}],
                                "dimensions": [{"name": d} for d in dimensions], "metrics": [{"name": m} for m in metrics]}
        if domain:
            body["dimensionFilter"] = {"filter": {"fieldName": "hostName", "stringFilter": {"matchType": "EXACT", "value": domain}}}
        if order_by:
            body["orderBys"] = [{"metric": {"metricName": order_by}, "desc": True}]
        elif order_by_dimension:
            body["orderBys"] = [{"dimension": {"dimensionName": order_by_dimension}, "desc": descending}]
        page = min(int(limit or self.page_size), self.page_size)
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            body.update(limit=page, offset=offset)
            headers = {"authorization": f"Bearer {google_access_token(self, GA4_SCOPES)}"}
            data = expect_json(self.http.post(f"/properties/{pid}:runReport", json=body, headers=headers), self, "runReport")
            dims = [h["name"] for h in data.get("dimensionHeaders") or []] or dimensions
            mets = [h["name"] for h in data.get("metricHeaders") or []] or metrics
            batch = data.get("rows") or []
            for r in batch:
                rec = {n: v.get("value") for n, v in zip(dims, r.get("dimensionValues") or [])}
                rec.update({n: v.get("value") for n, v in zip(mets, r.get("metricValues") or [])})
                rows.append(rec)
            offset += page
            if not batch or offset >= int(data.get("rowCount") or 0) or (limit is not None and len(rows) >= limit):
                break
        df = _num(pd.DataFrame(rows, columns=dimensions + metrics), metrics)
        if "date" in dimensions:
            df["date"] = pd.to_datetime(df["date"], format="%Y%m%d").dt.date
        return df

    def history_start(self, yesterday: date) -> date:
        return date(2015, 8, 14)  # earliest date the GA4 Data API accepts

    def data_days(self, since: date, until: date) -> tuple[date | None, date | None]:
        """Two 1-row reports: the first and the last day with sessions (GA4 omits days without data)."""
        days = [self.run_report(["date"], ["sessions"], since, until, order_by_dimension="date", descending=desc, limit=1)
                for desc in (False, True)]
        first, last = (d["date"].iloc[0] if len(d) else None for d in days)
        return first, last

    def daily(self, start: date, end: date, domain: str | None = None) -> pd.DataFrame:
        mets = ["sessions", "totalUsers", "newUsers", "engagedSessions", self._conv, self._rev]
        raw = self.run_report(["date"], mets, start, end, domain=domain)
        df = raw.rename(columns=dict(zip(mets, DAILY_COLUMNS[1:])))
        return _shape_daily(df, _d(start), _d(end))

    def pages(self, start: date, end: date, domain: str | None = None, limit: int = 50) -> pd.DataFrame:
        mets = ["sessions", "totalUsers", "engagedSessions", "userEngagementDuration", self._conv]
        raw = self.run_report(["pagePath", "pageTitle"], mets, start, end, domain=domain, order_by="sessions", limit=limit)
        df = raw.rename(columns={"pagePath": "page_path", "pageTitle": "page_title", "sessions": "sessions", "totalUsers": "users",
                                 "engagedSessions": "engaged_sessions", "userEngagementDuration": "engagement_time_s",
                                 self._conv: "conversions"})
        return _shape_pages(df, limit)

    def channels(self, start: date, end: date, domain: str | None = None) -> pd.DataFrame:
        mets = ["sessions", "totalUsers", self._conv, self._rev]
        raw = self.run_report(["sessionDefaultChannelGroup"], mets, start, end, domain=domain)
        df = raw.rename(columns={"sessionDefaultChannelGroup": "channel", "totalUsers": "users", self._conv: "conversions", self._rev: "revenue"})
        return _shape_channels(df)

    def channels_daily(self, start: date, end: date) -> pd.DataFrame:
        mets = ["sessions", "totalUsers", self._conv, self._rev]
        raw = self.run_report(["date", "sessionDefaultChannelGroup"], mets, start, end)
        df = raw.rename(columns={"sessionDefaultChannelGroup": "channel", "totalUsers": "users", self._conv: "conversions", self._rev: "revenue"})
        return df[CHANNEL_DAILY_COLUMNS].sort_values(["date", "channel"]).reset_index(drop=True)


# ----------------------------------------------------------------------------- Adobe Analytics

AA_SCOPES = "openid,AdobeID,read_organizations,additional_info.projectedProductContext"
AA_METRICS = {"sessions": "metrics/visits", "users": "metrics/visitors", "conversions": "metrics/orders",
              "revenue": "metrics/revenue", "bounces": "metrics/bounces", "engagement_time_s": "metrics/timespent",
              "new_users": None}


def adobe_day(item_id: Any, value: Any) -> date:
    """Adobe day items: itemId '1YYMMDD' (years since 1900, e.g. '1261018' = 2026-10-18) or value 'Oct 18, 2026'."""
    s = str(item_id or "")
    if re.fullmatch(r"\d{7}", s):
        return date(1900 + int(s[:3]), int(s[3:5]), int(s[5:7]))
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(value), fmt).date()
        except ValueError:
            continue
    raise ConnectorError(f"analytics:adobe_analytics: cannot parse day item {item_id!r} / {value!r}")


@connector("analytics", "adobe_analytics")
class AdobeAnalytics(_ExternalAnalytics):
    """Adobe Analytics via the Analytics 2.0 Reporting API.

    Endpoint: POST https://analytics.adobe.io/api/{company_id}/reports with {rsid, globalFilters
    (dateRange 'YYYY-MM-DDT00:00:00.000/YYYY-MM-DDT00:00:00.000', end exclusive), metricContainer,
    dimension, settings {limit, page, dimensionSort}}; paged until ``lastPage``.
    Auth: Adobe IMS OAuth server-to-server (client_credentials) at
    https://ims-na1.adobelogin.com/ims/token/v3 (or a static access_token), then headers
    Authorization: Bearer, x-api-key (client_id) and x-proxy-global-company-id.
    Config (connectors.analytics): company_id (global company id), rsid, client_id, client_secret
    (required unless access_token is set); optional scopes (default "openid,AdobeID,read_organizations,
    additional_info.projectedProductContext" - must match the Developer Console credential),
    segment_id (global segment), page_dimension (default variables/page), channel_dimension (default
    variables/marketingchannel), metric_map overrides, page_size, data_start / lookback_days
    (date_bounds() reads the first/last day with visits from one daterangeday report over
    data_start - or the trailing lookback_days, default 365 - to yesterday).
    Mapping: sessions=metrics/visits, users=metrics/visitors, conversions=metrics/orders,
    revenue=metrics/revenue; engaged_sessions = visits - metrics/bounces (Adobe has no engaged-session
    metric); engagement_time_s = metrics/timespent; new_users needs ``metric_map.new_users`` (e.g. a
    calculated metric id) - Adobe has no standard "new visitors" metric, so it is NaN otherwise.
    page_path/page_title both come from the page dimension (Adobe "Page" is the page name - point
    page_dimension at an eVar/prop holding the URL path if you need paths). ``domain`` is ignored
    (use a report suite or segment_id per site).
    """

    external = True
    required = ("company_id", "rsid", "client_id")  # + client_secret, unless a static access_token is set
    base_url = "https://analytics.adobe.io"
    retry_post = True  # /reports is a read-only query

    @property
    def available(self) -> bool:
        return super().available and bool(self.config.get("client_secret") or self.config.get("access_token"))

    def default_headers(self) -> dict[str, str]:
        return {"accept": "application/json", "x-api-key": str(self.config.get("client_id") or ""),
                "x-proxy-global-company-id": str(self.config.get("company_id") or "")}

    def metric(self, name: str) -> str | None:
        return {**AA_METRICS, **(self.config.get("metric_map") or {})}.get(name)

    def _body(self, dimension: str, metrics: list[dict[str, Any]], start: date, end: date, *, limit: int, page: int,
              sort_by_day: bool, metric_filters: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        rng = f"{_d(start).isoformat()}T00:00:00.000/{(_d(end) + timedelta(days=1)).isoformat()}T00:00:00.000"
        filters: list[dict[str, Any]] = [{"type": "dateRange", "dateRange": rng}]
        if self.config.get("segment_id"):
            filters.append({"type": "segment", "segmentId": self.config["segment_id"]})
        container: dict[str, Any] = {"metrics": metrics}
        if metric_filters:
            container["metricFilters"] = metric_filters
        settings: dict[str, Any] = {"limit": limit, "page": page, "countRepeatInstances": True}
        if sort_by_day:
            settings["dimensionSort"] = "asc"
        return {"rsid": self.config["rsid"], "globalFilters": filters, "metricContainer": container, "dimension": dimension,
                "settings": settings}

    def report(self, dimension: str, metrics: list[dict[str, Any]], start: date, end: date, *, limit: int | None = None,
               sort_by_day: bool = False, metric_filters: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        """Raw report rows [{itemId, value, data:[...]}], all pages (or the first ``limit`` rows)."""
        creds = self.require(*self.required)
        page_size = int(limit or self.config.get("page_size") or 10_000)
        rows: list[dict[str, Any]] = []
        page = 0
        while True:
            token = adobe_ims_token(self, AA_SCOPES)
            body = self._body(dimension, metrics, start, end, limit=page_size, page=page, sort_by_day=sort_by_day,
                              metric_filters=metric_filters)
            data = expect_json(self.http.post(f"/api/{quote(str(creds['company_id']), safe='')}/reports", json=body,
                                              headers={"authorization": f"Bearer {token}", "content-type": "application/json"}),
                               self, "report")
            rows += data.get("rows") or []
            if data.get("lastPage", True) or not data.get("rows") or (limit is not None and len(rows) >= limit):
                break
            page += 1
        return rows[:limit] if limit is not None else rows

    def data_days(self, since: date, until: date) -> tuple[date | None, date | None]:
        """First/last day with visits from one daterangeday report over [since, until]."""
        rows = self.report("variables/daterangeday", [{"columnId": "0", "id": self.metric("sessions")}], since, until, sort_by_day=True)
        days = [adobe_day(r.get("itemId"), r.get("value")) for r in rows if (r.get("data") or [0])[0]]
        return (min(days), max(days)) if days else (None, None)

    def _metrics(self, names: list[str]) -> tuple[list[str], list[dict[str, Any]]]:
        keys = [n for n in names if self.metric(n)]
        return keys, [{"columnId": str(i), "id": self.metric(n)} for i, n in enumerate(keys)]

    @staticmethod
    def _frame(rows: list[dict[str, Any]], keys: list[str]) -> pd.DataFrame:
        recs = [{"itemId": r.get("itemId"), "value": r.get("value"), **dict(zip(keys, r.get("data") or []))} for r in rows]
        return _num(pd.DataFrame(recs, columns=["itemId", "value", *keys]), keys)

    def daily(self, start: date, end: date, domain: str | None = None) -> pd.DataFrame:
        keys, mets = self._metrics(["sessions", "users", "new_users", "conversions", "revenue", "bounces"])
        df = self._frame(self.report("variables/daterangeday", mets, start, end, sort_by_day=True), keys)
        df["date"] = [adobe_day(i, v) for i, v in zip(df["itemId"], df["value"])]
        if "new_users" not in keys:
            df["new_users"] = np.nan
        df["engaged_sessions"] = (df["sessions"] - df.get("bounces", 0)).clip(lower=0)
        return _shape_daily(df, _d(start), _d(end), new_users_known="new_users" in keys)

    def pages(self, start: date, end: date, domain: str | None = None, limit: int = 50) -> pd.DataFrame:
        keys, mets = self._metrics(["sessions", "users", "bounces", "engagement_time_s", "conversions"])
        mets[0]["sort"] = "desc"
        dim = str(self.config.get("page_dimension") or "variables/page")
        df = self._frame(self.report(dim, mets, start, end, limit=limit), keys)
        df["page_path"] = df["value"].astype(str)
        df["page_title"] = df["value"].astype(str)
        df["engaged_sessions"] = (df["sessions"] - df.get("bounces", 0)).clip(lower=0)
        if "engagement_time_s" not in df:
            df["engagement_time_s"] = 0
        return _shape_pages(df, limit)

    def channels(self, start: date, end: date, domain: str | None = None) -> pd.DataFrame:
        keys, mets = self._metrics(["sessions", "users", "conversions", "revenue"])
        mets[0]["sort"] = "desc"
        dim = str(self.config.get("channel_dimension") or "variables/marketingchannel")
        df = self._frame(self.report(dim, mets, start, end), keys)
        df["channel"] = df["value"].astype(str)
        return _shape_channels(df)

    def channels_daily(self, start: date, end: date) -> pd.DataFrame:
        """Day x channel via breakdown metric filters (one column per channel x metric), as the 2.0 API
        reports one dimension per request."""
        dim = str(self.config.get("channel_dimension") or "variables/marketingchannel")
        top = self.report(dim, [{"columnId": "0", "id": self.metric("sessions"), "sort": "desc"}], start, end,
                          limit=int(self.config.get("max_channels") or 15))
        names = ["sessions", "users", "conversions", "revenue"]
        filters, mets = [], []
        for ci, ch in enumerate(top):
            filters.append({"id": f"f{ci}", "type": "breakdown", "dimension": dim, "itemId": str(ch.get("itemId"))})
            mets += [{"columnId": f"{ci}:{n}", "id": self.metric(n), "filters": [f"f{ci}"]} for n in names]
        if not top:
            return pd.DataFrame(columns=CHANNEL_DAILY_COLUMNS)
        out = []
        for r in self.report("variables/daterangeday", mets, start, end, sort_by_day=True, metric_filters=filters):
            day = adobe_day(r.get("itemId"), r.get("value"))
            values = list(r.get("data") or [])
            for ci, ch in enumerate(top):
                chunk = values[ci * len(names):(ci + 1) * len(names)]
                out.append({"date": day, "channel": str(ch.get("value")), **dict(zip(names, chunk))})
        df = _num(pd.DataFrame(out, columns=CHANNEL_DAILY_COLUMNS), names)
        return df.sort_values(["date", "channel"]).reset_index(drop=True)


# ----------------------------------------------------------------------------- keyword data

def _topic_tokens(topic: str | None) -> set[str]:
    return {t for t in (topic or "").lower().split() if len(t) > 2}


def _shape_keywords(df: pd.DataFrame, topic: str | None, limit: int) -> pd.DataFrame:
    """Same post-processing as CsvKeywordData: topic token filter (falls back to all rows), sort by impressions."""
    if df.empty:
        return pd.DataFrame(columns=KEYWORD_COLUMNS)
    df = df[KEYWORD_COLUMNS].copy()
    toks = _topic_tokens(topic)
    if toks:
        mask = df["query"].str.lower().apply(lambda q: bool(toks & set(q.split())))
        if mask.any():
            df = df[mask]
    return df.sort_values("impressions", ascending=False).head(limit).reset_index(drop=True)


GSC_SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]


@connector("keyword_data", "gsc")
class GscKeywordData(ExternalProvider, KeywordData):
    """Google Search Console Search Analytics (first-party query x page performance).

    Endpoint: POST https://searchconsole.googleapis.com/webmasters/v3/sites/{siteUrl}/searchAnalytics/query
    with {startDate, endDate, dimensions [query, page], type, rowLimit (<= 25,000), startRow,
    dimensionFilterGroups}. A topic becomes an ``includingRegex`` filter on query (re-queried
    unfiltered when nothing matches, mirroring the CSV provider's fallback).
    Auth: Google service account (scope webmasters.readonly; add the account as a user on the
    property) via JWT bearer, or ``access_token``.
    Config (connectors.keyword_data): site_url (GSC_SITE_URL, e.g. ``sc-domain:example.com`` or
    ``https://www.example.com/``; the ``site`` argument is used when unset), credentials_file |
    credentials_json | access_token; optional days (default 28), data_lag_days (default 3),
    search_type (default web), max_rows (default 5000).
    """

    external = True
    base_url = "https://searchconsole.googleapis.com/webmasters/v3"
    retry_post = True

    @property
    def available(self) -> bool:
        cfg = self.config
        return bool(cfg.get("site_url") and (cfg.get("credentials_file") or cfg.get("credentials_json") or cfg.get("access_token")))

    def _site(self, site: str | None) -> str:
        value = self.config.get("site_url") or site
        if not value:
            self.require("site_url")
        value = str(value)
        return value if value.startswith(("http://", "https://", "sc-domain:")) else f"sc-domain:{value}"

    def _query(self, site_url: str, regex: str | None, max_rows: int) -> list[dict[str, Any]]:
        end = _today() - timedelta(days=int(self.config.get("data_lag_days", 3)))
        start = end - timedelta(days=int(self.config.get("days") or 28) - 1)
        rows: list[dict[str, Any]] = []
        start_row = 0
        while len(rows) < max_rows:
            body: dict[str, Any] = {"startDate": start.isoformat(), "endDate": end.isoformat(), "dimensions": ["query", "page"],
                                    "type": self.config.get("search_type") or "web", "rowLimit": min(25_000, max_rows - len(rows)),
                                    "startRow": start_row}
            if regex:
                body["dimensionFilterGroups"] = [{"filters": [{"dimension": "query", "operator": "includingRegex", "expression": regex}]}]
            headers = {"authorization": f"Bearer {google_access_token(self, GSC_SCOPES)}"}
            data = expect_json(self.http.post(f"/sites/{quote(site_url, safe='')}/searchAnalytics/query", json=body, headers=headers),
                               self, "searchAnalytics.query")
            batch = data.get("rows") or []
            rows += batch
            if len(batch) < body["rowLimit"]:
                break
            start_row += len(batch)
        return rows

    def queries(self, topic: str | None = None, site: str | None = None, limit: int = 1000) -> pd.DataFrame:
        site_url = self._site(site)
        max_rows = max(int(limit), int(self.config.get("max_rows") or 5000))
        toks = sorted(_topic_tokens(topic))
        regex = r"\b(" + "|".join(re.escape(t) for t in toks) + r")\b" if toks else None
        rows = self._query(site_url, regex, max_rows)
        if regex and not rows:
            rows = self._query(site_url, None, max_rows)
        recs = [{"query": (r.get("keys") or ["", ""])[0], "page": (r.get("keys") or ["", ""])[1],
                 "clicks": r.get("clicks", 0), "impressions": r.get("impressions", 0), "ctr": r.get("ctr", 0.0),
                 "position": r.get("position", 0.0)} for r in rows]
        df = pd.DataFrame(recs, columns=KEYWORD_COLUMNS)
        df["ctr"] = pd.to_numeric(df["ctr"], errors="coerce").round(4)
        return _shape_keywords(df, topic, limit)


@connector("keyword_data", "dataforseo")
class DataForSeoKeywordData(ExternalProvider, KeywordData):
    """DataForSEO Labs - third-party keyword estimates (not first-party Search Console data).

    Endpoints: POST https://api.dataforseo.com/v3/dataforseo_labs/google/ranked_keywords/live
    (a site/target is known: keywords the domain ranks for) and
    POST https://api.dataforseo.com/v3/dataforseo_labs/google/keyword_suggestions/live (topic only).
    Body: a JSON array with one task {target|keyword, location_code, language_code, limit, filters}.
    Auth: HTTP Basic (login:password). HTTP 200 responses can still carry task errors, so every
    task's ``status_code`` (20000 = ok) is checked.
    Config (connectors.keyword_data): login, password (required); optional target (default domain),
    location_code (default 2840 = US), language_code (default en).
    Mapping (estimates, flagged with ``df.attrs['estimated'] = True``): query = keyword,
    page = ranked_serp_element.serp_item.url, impressions = keyword_info.search_volume (monthly),
    clicks = serp_item.etv (estimated traffic), ctr = clicks / impressions,
    position = serp_item.rank_group. Topic-only suggestions have no page/clicks/position (NaN).
    Item field paths follow the DataForSEO Labs docs' filter/order_by examples; the full item schema
    was not re-verified against a live response.
    """

    external = True
    required = ("login", "password")
    base_url = "https://api.dataforseo.com/v3"
    retry_post = True

    def default_headers(self) -> dict[str, str]:
        if self.config.get("login") and self.config.get("password"):
            return {"authorization": basic_auth_header(str(self.config["login"]), str(self.config["password"]))}
        return {}

    def _task(self, path: str, task: dict[str, Any]) -> list[dict[str, Any]]:
        self.require(*self.required)
        task = {"location_code": int(self.config.get("location_code") or 2840),
                "language_code": self.config.get("language_code") or "en", **task}
        data = expect_json(self.http.post(path, json=[task]), self, path.rsplit("/", 2)[-2])
        if int(data.get("status_code", 20000)) != 20000:
            raise ConnectorError(f"keyword_data:dataforseo: {data.get('status_code')} {data.get('status_message')}")
        items: list[dict[str, Any]] = []
        for t in data.get("tasks") or []:
            if int(t.get("status_code", 20000)) != 20000:
                raise ConnectorError(f"keyword_data:dataforseo task failed: {t.get('status_code')} {t.get('status_message')}")
            for res in t.get("result") or []:
                items += res.get("items") or []
        return items

    def queries(self, topic: str | None = None, site: str | None = None, limit: int = 1000) -> pd.DataFrame:
        target = self.config.get("target") or site
        toks = sorted(_topic_tokens(topic))
        if target:
            task: dict[str, Any] = {"target": target, "limit": min(int(limit), 1000)}
            if toks:
                conds: list[Any] = []
                for t in toks[:8]:
                    conds += (["or"] if conds else []) + [["keyword_data.keyword", "like", f"%{t}%"]]
                task["filters"] = conds
            recs = []
            for it in self._task("/dataforseo_labs/google/ranked_keywords/live", task):
                kd, serp = it.get("keyword_data") or {}, (it.get("ranked_serp_element") or {}).get("serp_item") or {}
                recs.append({"query": kd.get("keyword"), "page": serp.get("url"), "clicks": serp.get("etv"),
                             "impressions": (kd.get("keyword_info") or {}).get("search_volume"),
                             "position": serp.get("rank_group") or serp.get("rank_absolute")})
        elif topic:
            recs = [{"query": it.get("keyword"), "page": None, "clicks": np.nan,
                     "impressions": (it.get("keyword_info") or {}).get("search_volume"), "position": np.nan}
                    for it in self._task("/dataforseo_labs/google/keyword_suggestions/live", {"keyword": topic, "limit": min(int(limit), 1000)})]
        else:
            raise ConnectorError("keyword_data:dataforseo needs a topic or a site/target")
        df = pd.DataFrame(recs, columns=["query", "page", "clicks", "impressions", "position"])
        df = df[df["query"].notna()]
        for c in ("clicks", "impressions", "position"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df["impressions"] = df["impressions"].fillna(0)
        df["ctr"] = (df["clicks"] / df["impressions"].clip(lower=1)).round(4)
        out = _shape_keywords(df, topic, limit)
        out.attrs["estimated"] = True
        return out
