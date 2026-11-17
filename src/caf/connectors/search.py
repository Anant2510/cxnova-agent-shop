"""Search connector: web + news search and RSS/Atom feeds, every result timestamped with its source.

Providers here: `none` (no search; agents fall back to clearly-flagged model estimates) and
`fixture` (local JSON results for demos/tests). External providers live in search_providers.py.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .base import Connector, connector


def _today() -> date:
    return datetime.now(timezone.utc).date()


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""
    published: str | None = None
    source: str | None = None
    retrieved_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    provider: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Search(Connector):
    def search(self, query: str, num: int = 10, recency_days: int | None = None) -> list[SearchResult]:
        raise NotImplementedError

    def news(self, query: str, num: int = 10, days: int = 30) -> list[SearchResult]:
        return self.search(query, num=num, recency_days=days)

    def feeds(self, urls: list[str], days: int | None = None) -> list[SearchResult]:
        """Read RSS/Atom feeds through the crawler connector (works with any provider)."""
        from lxml import etree

        out: list[SearchResult] = []
        crawler = self.hub.get("crawler")
        for url in urls:
            raw = crawler.fetch_text(url)
            if not raw:
                continue
            try:
                root = etree.fromstring(raw.encode("utf-8"))
            except Exception:
                continue
            ns = {"a": "http://www.w3.org/2005/Atom"}
            items = root.findall(".//item") or root.findall(".//a:entry", ns)
            for it in items:
                def txt(tag: str) -> str:
                    el = it.find(tag) if not tag.startswith("a:") else it.find(tag, ns)
                    return (el.text or "").strip() if el is not None and el.text else ""
                link = txt("link")
                if not link:
                    el = it.find("a:link", ns)
                    link = el.get("href") if el is not None else ""
                out.append(SearchResult(title=txt("title") or txt("a:title"), url=link,
                                        snippet=txt("description") or txt("a:summary"),
                                        published=(txt("pubDate") or txt("a:updated"))[:25] or None,
                                        source=url, provider=f"rss:{self.provider}"))
        if days is not None:
            ref = self.config.get("reference_date")
            out = [r for r in out if (_within_ref(r.published, days, ref) if ref else _within(r.published, days))]
        return out


def _within(published: str | None, days: int) -> bool:
    if not published:
        return True
    try:
        d = datetime.fromisoformat(published[:10]).date()
    except ValueError:
        return True
    return d >= _today() - timedelta(days=days)


@connector("search", "none")
class NoSearch(Search):
    """No search provider configured - agents must flag outputs as model estimates."""

    @property
    def available(self) -> bool:
        return False

    def search(self, query: str, num: int = 10, recency_days: int | None = None) -> list[SearchResult]:
        return []


@connector("search", "fixture")
class FixtureSearch(Search):
    """Offline search over sample_data/search/*.json ({"results": [{query_terms, title, url, ...}]})."""

    def __init__(self, config: dict[str, Any], hub: Any):
        super().__init__(config, hub)
        self._items: list[dict[str, Any]] | None = None

    @property
    def items(self) -> list[dict[str, Any]]:
        if self._items is None:
            folder = self.path("path", "sample_data/search")
            files = [folder] if folder.is_file() else sorted(folder.glob("*.json"))
            items: list[dict[str, Any]] = []
            for f in files:
                items += json.loads(f.read_text(encoding="utf-8")).get("results", [])
            self._items = items
        return self._items

    def search(self, query: str, num: int = 10, recency_days: int | None = None) -> list[SearchResult]:
        q = {t for t in query.lower().replace("-", " ").split() if len(t) > 2}
        scored = []
        for it in self.items:
            terms = {t.lower() for t in it.get("query_terms", [])}
            hay = f"{it.get('title', '')} {it.get('snippet', '')}".lower()
            score = 3 * len(q & terms) + sum(1 for t in q if t in hay)
            if score:
                scored.append((score, it.get("published") or "", it))
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        results = [SearchResult(title=it["title"], url=it["url"], snippet=it.get("snippet", ""),
                                published=it.get("published"), source=it.get("source"), provider="fixture")
                   for _, _, it in scored]
        if recency_days is not None:
            ref = self.config.get("reference_date")
            results = [r for r in results if _within_ref(r.published, recency_days, ref)]
        return results[:num]


def _within_ref(published: str | None, days: int, ref: str | None) -> bool:
    if not published:
        return True
    base = datetime.fromisoformat(ref).date() if ref else _today()
    try:
        return datetime.fromisoformat(published[:10]).date() >= base - timedelta(days=days)
    except ValueError:
        return True
