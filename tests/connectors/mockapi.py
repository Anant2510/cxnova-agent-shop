"""Test helpers: a routed httpx.MockTransport that records requests, plus body/auth decoders."""

from __future__ import annotations

import base64
import json
import re
from urllib.parse import parse_qsl

import httpx


class Api:
    """Routed MockTransport: [(METHOD, regex on the URL without query, handler)] -> records every request.

    A handler is a response, a payload, a (status, payload) tuple, or a callable(request) returning one.
    Unrouted requests get HTTP 599 so a wrong URL fails loudly.
    """

    def __init__(self, *routes):
        self.routes = [(m, re.compile(p), h) for m, p, h in routes]
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.calls.append(request)
        url = str(request.url).split("?")[0]
        for method, pattern, handler in self.routes:
            if method == request.method and pattern.search(url):
                out = handler(request) if callable(handler) else handler
                if isinstance(out, httpx.Response):
                    return out
                status, payload = out if isinstance(out, tuple) else (200, out)
                if isinstance(payload, (str, bytes)):
                    return httpx.Response(status, content=payload if isinstance(payload, bytes) else payload.encode())
                return httpx.Response(status, json=payload)
        return httpx.Response(599, json={"error": f"unrouted {request.method} {url}"})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def find(self, method: str, pattern: str) -> list[httpx.Request]:
        rx = re.compile(pattern)
        return [r for r in self.calls if r.method == method and rx.search(str(r.url).split("?")[0])]

    def one(self, method: str, pattern: str) -> httpx.Request:
        found = self.find(method, pattern)
        assert len(found) == 1, f"expected one {method} {pattern}, got {len(found)}: {[str(c.url) for c in self.calls]}"
        return found[0]


def body(req: httpx.Request):
    return json.loads(req.content)


def form(req: httpx.Request) -> dict[str, str]:
    return dict(parse_qsl(req.content.decode()))


def basic(req: httpx.Request) -> tuple[str, str]:
    scheme, _, token = req.headers["authorization"].partition(" ")
    assert scheme == "Basic"
    user, _, pw = base64.b64decode(token).decode().partition(":")
    return user, pw


def ims_route(token: str = "ims-token"):
    """Adobe IMS OAuth server-to-server token endpoint."""
    return ("POST", r"ims-na1\.adobelogin\.com/ims/token/v3$", {"access_token": token, "token_type": "bearer", "expires_in": 86399})


def no_network(_request):  # pragma: no cover - reaching it is a failure
    raise AssertionError("adapter attempted a network call")
