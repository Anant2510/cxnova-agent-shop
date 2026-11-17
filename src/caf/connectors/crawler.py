"""Crawler connector: fetch, crawl, robots/sitemaps, JS rendering and screenshots.

Provider `httpx` fetches over HTTP(S). Hosts listed under `fixtures` are served from local folders
(sample_data/site for the demo insurer) so audits, crawls and screenshots work fully offline.
Playwright (Chromium) is used for JS rendering, screenshots, axe-core and variation validation.
"""

from __future__ import annotations

import json
import mimetypes
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urldefrag, urljoin, urlparse

import httpx

from ..core.security import Policy, PolicyError
from .base import Connector, connector

DEFAULT_UA = "CoforgeAgenticFramework/1.0 (+https://www.coforge.com) audit-bot"


@dataclass
class Page:
    url: str
    final_url: str
    status: int
    headers: dict[str, str]
    html: str
    elapsed_ms: float
    redirects: list[tuple[str, int]] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status < 300

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")

    @property
    def is_html(self) -> bool:
        return "html" in self.content_type or self.html.lstrip().lower().startswith(("<!doctype html", "<html"))

    def soup(self):
        from bs4 import BeautifulSoup
        return BeautifulSoup(self.html or "", "lxml")

    def text(self) -> str:
        try:
            import trafilatura
            extracted = trafilatura.extract(self.html, include_comments=False, include_tables=True, favor_recall=True)
            if extracted:
                return extracted
        except Exception:
            pass
        soup = self.soup()
        for t in soup(["script", "style", "noscript", "template"]):
            t.decompose()
        return " ".join(soup.get_text(" ").split())

    def links(self) -> list[str]:
        out = []
        for a in self.soup().find_all("a", href=True):
            href = urldefrag(urljoin(self.final_url, a["href"].strip()))[0]
            if href.startswith(("http://", "https://")):
                out.append(href)
        return out


class FixtureTransport(httpx.BaseTransport):
    """Serves fixture hosts from disk; honours _fixture.json redirects/status overrides."""

    def __init__(self, roots: dict[str, Path]):
        self.roots = roots
        self._meta: dict[str, dict[str, Any]] = {}

    def meta(self, host: str) -> dict[str, Any]:
        if host not in self._meta:
            f = self.roots[host] / "_fixture.json"
            self._meta[host] = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
        return self._meta[host]

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        root = self.roots[host]
        path = request.url.path or "/"
        meta = self.meta(host)
        if path in meta.get("redirects", {}):
            target, code = meta["redirects"][path]
            return httpx.Response(code, headers={"location": target}, request=request)
        if path in meta.get("status", {}):
            return httpx.Response(int(meta["status"][path]), text="<html><body><h1>Not found</h1></body></html>",
                                  headers={"content-type": "text/html; charset=utf-8"}, request=request)
        rel = path.lstrip("/")
        candidates = [root / rel] if rel else []
        candidates += [root / rel / "index.html", root / (rel + ".html")] if not rel.endswith((".html", ".txt", ".xml", ".css", ".svg", ".png", ".jpg", ".js", ".json")) else []
        if not rel:
            candidates = [root / "index.html"]
        root_resolved = root.resolve()
        for c in candidates:
            if c.is_file() and c.resolve().is_relative_to(root_resolved):
                mime = mimetypes.guess_type(c.name)[0] or "application/octet-stream"
                if mime.startswith("text/") or mime in ("application/xml", "image/svg+xml", "application/json", "application/javascript"):
                    mime += "; charset=utf-8"
                return httpx.Response(200, content=c.read_bytes(), headers={"content-type": mime}, request=request)
        return httpx.Response(404, text="<html><body><h1>Not found</h1></body></html>",
                              headers={"content-type": "text/html; charset=utf-8"}, request=request)


class Crawler(Connector):
    def fetch(self, url: str) -> Page: raise NotImplementedError
    def fetch_text(self, url: str) -> str | None: raise NotImplementedError
    def crawl(self, start_url: str, max_pages: int = 25, same_host: bool = True) -> list[Page]: raise NotImplementedError
    def render(self, url: str) -> Page: raise NotImplementedError
    def screenshot(self, url: str | None = None, html: str | None = None, **kw: Any) -> bytes: raise NotImplementedError


@connector("crawler", "httpx")
class HttpxCrawler(Crawler):
    """HTTP crawler (httpx) with offline fixture hosts and optional Playwright rendering."""

    def __init__(self, config: dict[str, Any], hub: Any):
        super().__init__(config, hub)
        fixtures = self.config.get("fixtures") or {}
        self.fixture_roots = {host: self.settings.resolve(p) for host, p in fixtures.items()}
        self.user_agent = self.config.get("user_agent") or DEFAULT_UA
        self.timeout = float(self.config.get("timeout_s", 20))
        self._client: httpx.Client | None = None

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            mounts = {}
            if self.fixture_roots:
                ft = FixtureTransport(self.fixture_roots)
                for host in self.fixture_roots:
                    mounts[f"all://{host}"] = ft
            self._client = httpx.Client(headers={"user-agent": self.user_agent}, timeout=self.timeout,
                                        follow_redirects=False, mounts=mounts)
        return self._client

    def is_fixture(self, url: str) -> bool:
        return urlparse(url).hostname in self.fixture_roots

    def fixture_root(self, url: str) -> Path | None:
        return self.fixture_roots.get(urlparse(url).hostname or "")

    @property
    def policy(self) -> Policy:
        return Policy(self.settings)

    def fetch(self, url: str, max_redirects: int = 8) -> Page:
        t0 = time.perf_counter()
        redirects: list[tuple[str, int]] = []
        current = url
        policy = self.policy
        try:
            for _ in range(max_redirects + 1):
                try:  # every hop, so a redirect cannot lead to a host the policy forbids
                    policy.check_url(current)
                except PolicyError as exc:
                    return Page(url, current, 0, {}, "", round((time.perf_counter() - t0) * 1000, 1), redirects,
                                f"blocked by security policy: {exc}")
                resp = self.client.get(current)
                if resp.status_code in (301, 302, 303, 307, 308) and "location" in resp.headers:
                    redirects.append((current, resp.status_code))
                    current = urljoin(current, resp.headers["location"])
                    continue
                return Page(url=url, final_url=current, status=resp.status_code,
                            headers={k.lower(): v for k, v in resp.headers.items()},
                            html=resp.text if "text" in resp.headers.get("content-type", "") or "xml" in resp.headers.get("content-type", "") or "json" in resp.headers.get("content-type", "") else "",
                            elapsed_ms=round((time.perf_counter() - t0) * 1000, 1), redirects=redirects)
            return Page(url, current, 0, {}, "", round((time.perf_counter() - t0) * 1000, 1), redirects, "too many redirects")
        except httpx.HTTPError as exc:
            return Page(url, current, 0, {}, "", round((time.perf_counter() - t0) * 1000, 1), redirects, f"{type(exc).__name__}: {exc}")

    def fetch_text(self, url: str) -> str | None:
        page = self.fetch(url)
        return page.html if page.ok else None

    def crawl(self, start_url: str, max_pages: int = 25, same_host: bool = True) -> list[Page]:
        host = urlparse(start_url).hostname
        seen: set[str] = set()
        queue: deque[str] = deque([start_url])
        pages: list[Page] = []
        while queue and len(pages) < max_pages:
            url = queue.popleft()
            if url in seen:
                continue
            seen.add(url)
            page = self.fetch(url)
            pages.append(page)
            if page.ok and page.is_html:
                for link in page.links():
                    if same_host and urlparse(link).hostname != host:
                        continue
                    if link not in seen and not link.endswith((".pdf", ".png", ".jpg", ".svg", ".css", ".js", ".zip")):
                        queue.append(link)
        return pages

    # --- browser-backed helpers ---------------------------------------------------------------
    _browser_ok: bool | None = None

    def browser_available(self) -> bool:
        """True only if Playwright imports AND Chromium launches (checked once per process)."""
        if HttpxCrawler._browser_ok is None:
            try:
                from playwright.sync_api import sync_playwright

                with sync_playwright() as p:
                    p.chromium.launch(args=["--no-sandbox"]).close()
                HttpxCrawler._browser_ok = True
            except Exception:
                HttpxCrawler._browser_ok = False
        return HttpxCrawler._browser_ok

    def _route(self, page: Any, policy: Policy) -> None:
        """Serve fixture hosts from disk and hold every other request to the security policy."""
        if not self.fixture_roots and not policy.service:
            return
        ft = FixtureTransport(self.fixture_roots) if self.fixture_roots else None

        def handler(route: Any) -> None:
            req_url = route.request.url
            if ft is not None and urlparse(req_url).hostname in self.fixture_roots:
                resp = ft.handle_request(httpx.Request("GET", req_url))
                if resp.status_code in (301, 302, 307, 308):
                    route.fulfill(status=resp.status_code, headers={"location": urljoin(req_url, resp.headers["location"])})
                else:
                    route.fulfill(status=resp.status_code, body=resp.content,
                                  headers={"content-type": resp.headers.get("content-type", "text/html")})
                return
            if not policy.url_allowed(req_url):
                route.abort("blockedbyclient")
                return
            if not policy.service:
                route.continue_()
                return
            # Service mode: fetch hop by hop so a redirect cannot lead the browser to a forbidden host
            # (Playwright does not route redirect hops itself; fulfilled redirects come back through here).
            try:
                resp = route.fetch(max_redirects=0)
            except Exception:
                route.abort("failed")
                return
            location = resp.headers.get("location")
            if 300 <= resp.status < 400 and location and not policy.url_allowed(urljoin(req_url, location)):
                route.abort("blockedbyclient")
                return
            route.fulfill(response=resp)

        page.route("**/*", handler)

    def with_page(self, fn: Any, *, url: str | None = None, html: str | None = None, width: int = 1280,
                  height: int = 900, base_url: str | None = None) -> Any:
        """Open Chromium, load url or html, call fn(page) and return its result."""
        from playwright.sync_api import sync_playwright

        policy = self.policy
        for target in (url, base_url):
            if target:
                policy.check_url(target)  # raises PolicyError (file://, private hosts ... in service mode)
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox"])
            try:
                ctx = browser.new_context(viewport={"width": width, "height": height}, user_agent=self.user_agent)
                page = ctx.new_page()
                self._route(page, policy)
                if url:
                    page.goto(url, wait_until="load", timeout=int(self.timeout * 1000))
                elif html is not None:
                    if base_url:
                        page.goto(base_url, wait_until="load", timeout=int(self.timeout * 1000))
                        page.set_content(html, wait_until="load")
                    else:
                        page.set_content(html, wait_until="load")
                return fn(page)
            finally:
                browser.close()

    def render(self, url: str) -> Page:
        if not self.browser_available():
            return self.fetch(url)
        t0 = time.perf_counter()
        html = self.with_page(lambda pg: pg.content(), url=url)
        return Page(url, url, 200, {"content-type": "text/html"}, html, round((time.perf_counter() - t0) * 1000, 1))

    def screenshot(self, url: str | None = None, html: str | None = None, *, css: str | None = None,
                   js: str | None = None, width: int = 1280, height: int = 900, full_page: bool = True,
                   base_url: str | None = None) -> bytes:
        def shoot(page: Any) -> bytes:
            if css:
                page.add_style_tag(content=css)
            if js:
                page.evaluate(js)
            page.wait_for_timeout(150)
            return page.screenshot(full_page=full_page)

        return self.with_page(shoot, url=url, html=html, width=width, height=height, base_url=base_url)
