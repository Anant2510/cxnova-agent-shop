"""External search providers: SerpAPI (Google web + Google News), Brave Search API, Tavily.

Every provider returns ``list[SearchResult]`` exactly like the local ``fixture`` / ``none``
providers: ``published`` is normalised to an ISO date (YYYY-MM-DD) when the vendor gives one,
``source`` is the publisher or hostname, ``retrieved_at`` is stamped and ``provider`` names the
vendor (``<provider>:news`` for news). Select one with ``connectors.search.provider`` and set
``api_key`` (settings.yaml maps it to ``SEARCH_API_KEY``).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from ..core.textutil import strip_html
from ._http import ExternalProvider, expect_json
from .base import ConnectorError, connector
from .search import Search, SearchResult, _within

_RELATIVE = re.compile(r"(\d+)\s*(min|minute|hour|hr|day|week|month|year)s?\s+ago", re.I)
_FORMATS = ("%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y", "%m/%d/%Y", "%Y-%m-%d", "%Y/%m/%d")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def iso_date(value: Any, now: datetime | None = None) -> str | None:
    """Best-effort vendor date -> 'YYYY-MM-DD' ('2026-10-17T07:00:00Z', 'Oct 3, 2026',
    '10/17/2026, 07:00 AM, +0000 UTC', '3 days ago'). Unparseable -> None (never guessed)."""
    if value in (None, ""):
        return None
    s = str(value).strip()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        pass
    m = _RELATIVE.search(s)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        days = {"min": 0, "minute": 0, "hour": 0, "hr": 0, "day": n, "week": 7 * n, "month": 30 * n, "year": 365 * n}[unit]
        return ((now or _now()) - timedelta(days=days)).date().isoformat()
    head = s.split(",")[0].strip() if re.match(r"^\d{1,2}/\d{1,2}/\d{4},", s) else s
    for fmt in _FORMATS:
        try:
            return datetime.strptime(head, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _host(url: str | None) -> str | None:
    return urlparse(url or "").hostname or None


def _recent(results: list[SearchResult], days: int | None) -> list[SearchResult]:
    return results if days is None else [r for r in results if _within(r.published, days)]


class _ExternalSearch(ExternalProvider, Search):
    external = True
    required = ("api_key",)

    def _locale(self) -> tuple[str | None, str | None]:
        return self.config.get("country"), self.config.get("language")


# ----------------------------------------------------------------------------- SerpAPI

def _qdr(days: int | None) -> str | None:
    if days is None:
        return None
    for limit, code in ((1, "d"), (7, "w"), (31, "m"), (366, "y")):
        if days <= limit:
            return f"qdr:{code}"
    return None


@connector("search", "serpapi")
class SerpApiSearch(_ExternalSearch):
    """SerpAPI - Google web results (engine=google) and Google News (engine=google_news).

    Endpoint: GET https://serpapi.com/search.json with engine, q, num, gl, hl (+ tbs=qdr:d|w|m|y for
    web recency). Auth: ``api_key`` query parameter (stripped from any error message).
    Config (connectors.search): api_key (required), optional country (gl), language (hl), base_url.
    Notes: the google_news engine has no date filter and no documented ``num`` parameter, so news
    results are filtered on ``iso_date`` and truncated client-side; grouped stories are flattened.
    """

    external = True
    base_url = "https://serpapi.com"

    def _get(self, params: dict[str, Any], action: str) -> dict[str, Any]:
        key = self.require("api_key")["api_key"]
        gl, hl = self._locale()
        params = {**params, "api_key": key, **({"gl": gl} if gl else {}), **({"hl": hl} if hl else {})}
        data = expect_json(self.http.get("/search.json", params=params), self, action)
        err = str(data.get("error") or "")
        if err and "hasn't returned any results" not in err:
            raise ConnectorError(f"search:serpapi {action} failed: {err}")
        return data

    def search(self, query: str, num: int = 10, recency_days: int | None = None) -> list[SearchResult]:
        params: dict[str, Any] = {"engine": "google", "q": query, "num": max(1, min(int(num), 100))}
        if _qdr(recency_days):
            params["tbs"] = _qdr(recency_days)
        data = self._get(params, "web search")
        out = [SearchResult(title=r.get("title") or "", url=r["link"], snippet=r.get("snippet") or "",
                            published=iso_date(r.get("date")), source=r.get("source") or _host(r["link"]), provider="serpapi")
               for r in data.get("organic_results") or [] if r.get("link")]
        return _recent(out, recency_days)[:num]

    def news(self, query: str, num: int = 10, days: int = 30) -> list[SearchResult]:
        data = self._get({"engine": "google_news", "q": query}, "news search")
        flat: list[dict[str, Any]] = []
        for item in data.get("news_results") or []:
            if item.get("stories"):
                flat += ([item["highlight"]] if item.get("highlight") else []) + list(item["stories"])
            else:
                flat.append(item)
        out: list[SearchResult] = []
        seen: set[str] = set()
        for r in flat:
            link = r.get("link")
            if not link or link in seen:
                continue
            seen.add(link)
            src = r.get("source") or {}
            out.append(SearchResult(title=r.get("title") or "", url=link, snippet=r.get("snippet") or "",
                                    published=iso_date(r.get("iso_date") or r.get("date")),
                                    source=(src.get("name") if isinstance(src, dict) else src) or _host(link),
                                    provider="serpapi:news"))
        return _recent(out, days)[:num]


# ----------------------------------------------------------------------------- Brave

def _freshness(days: int | None) -> str | None:
    if days is None:
        return None
    today = _now().date()
    return f"{(today - timedelta(days=int(days))).isoformat()}to{today.isoformat()}"


@connector("search", "brave")
class BraveSearch(_ExternalSearch):
    """Brave Search API - web and news.

    Endpoints: GET https://api.search.brave.com/res/v1/web/search (count <= 20) and
    GET https://api.search.brave.com/res/v1/news/search (count <= 50), with q, count,
    freshness=<YYYY-MM-DD>to<YYYY-MM-DD>, country, search_lang.
    Auth: ``X-Subscription-Token: <api_key>`` header.
    Config (connectors.search): api_key (required), optional country, language, safesearch, base_url.
    Not confirmed in the public docs page: per-result ``page_age`` / ``meta_url`` names (taken from
    the API reference schema); ``age`` is used as a fallback.
    """

    external = True
    base_url = "https://api.search.brave.com/res/v1"

    def default_headers(self) -> dict[str, str]:
        return {"accept": "application/json", "x-subscription-token": str(self.config.get("api_key") or "")}

    def _get(self, path: str, query: str, count: int, days: int | None, action: str) -> dict[str, Any]:
        self.require("api_key")
        country, lang = self._locale()
        params: dict[str, Any] = {"q": query, "count": count}
        if _freshness(days):
            params["freshness"] = _freshness(days)
        if country:
            params["country"] = country
        if lang:
            params["search_lang"] = lang
        if self.config.get("safesearch"):
            params["safesearch"] = self.config["safesearch"]
        return expect_json(self.http.get(path, params=params), self, action)

    def _result(self, r: dict[str, Any], provider: str) -> SearchResult:
        meta = r.get("meta_url") or {}
        profile = r.get("profile") or {}
        return SearchResult(title=strip_html(r.get("title") or ""), url=r["url"], snippet=strip_html(r.get("description") or ""),
                            published=iso_date(r.get("page_age")) or iso_date(r.get("age")),
                            source=profile.get("name") or meta.get("hostname") or _host(r["url"]), provider=provider)

    def search(self, query: str, num: int = 10, recency_days: int | None = None) -> list[SearchResult]:
        data = self._get("/web/search", query, max(1, min(int(num), 20)), recency_days, "web search")
        out = [self._result(r, "brave") for r in (data.get("web") or {}).get("results") or [] if r.get("url")]
        return out[:num]

    def news(self, query: str, num: int = 10, days: int = 30) -> list[SearchResult]:
        data = self._get("/news/search", query, max(1, min(int(num), 50)), days, "news search")
        out = [self._result(r, "brave:news") for r in data.get("results") or [] if r.get("url")]
        return _recent(out, days)[:num]


# ----------------------------------------------------------------------------- Tavily

@connector("search", "tavily")
class TavilySearch(_ExternalSearch):
    """Tavily Search API.

    Endpoint: POST https://api.tavily.com/search with JSON {query, max_results (<= 20),
    topic general|news, start_date (YYYY-MM-DD, for recency), search_depth, include_published_date}.
    Auth: ``Authorization: Bearer <api_key>``.
    Config (connectors.search): api_key (required), optional search_depth (basic|advanced|fast),
    country, base_url. ``published_date`` is Tavily's estimate and is only returned for news or when
    include_published_date is honoured.
    """

    external = True
    base_url = "https://api.tavily.com"
    retry_post = True  # /search is a read-only query

    def _post(self, query: str, num: int, days: int | None, topic: str) -> list[dict[str, Any]]:
        key = self.require("api_key")["api_key"]
        body: dict[str, Any] = {"query": query, "max_results": max(1, min(int(num), 20)), "topic": topic,
                                "search_depth": self.config.get("search_depth") or "basic", "include_published_date": True}
        if days is not None:
            body["start_date"] = (_now().date() - timedelta(days=int(days))).isoformat()
        if self.config.get("country") and topic == "general":
            body["country"] = self.config["country"]
        data = expect_json(self.http.post("/search", json=body, headers={"authorization": f"Bearer {key}"}), self, f"{topic} search")
        return list(data.get("results") or [])

    def _results(self, rows: list[dict[str, Any]], provider: str) -> list[SearchResult]:
        return [SearchResult(title=r.get("title") or "", url=r["url"], snippet=r.get("content") or "",
                             published=iso_date(r.get("published_date")), source=_host(r["url"]), provider=provider)
                for r in rows if r.get("url")]

    def search(self, query: str, num: int = 10, recency_days: int | None = None) -> list[SearchResult]:
        return self._results(self._post(query, num, recency_days, "general"), "tavily")[:num]

    def news(self, query: str, num: int = 10, days: int = 30) -> list[SearchResult]:
        return _recent(self._results(self._post(query, num, days, "news"), "tavily:news"), days)[:num]


__all__ = ["BraveSearch", "SerpApiSearch", "TavilySearch", "iso_date"]
