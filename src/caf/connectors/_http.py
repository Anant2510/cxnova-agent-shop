"""Shared plumbing for the external provider adapters (*_providers.py, crm.py).

* ``http_client(conn, ...)`` builds an ``httpx.Client`` with timeouts and a small retry/backoff
  policy for 429/5xx. When ``conn.config["_transport"]`` holds an httpx transport (tests inject
  ``httpx.MockTransport``) it is used instead of the network, so every adapter is testable offline.
* ``check()`` / ``expect_json()`` turn HTTP failures into ``ConnectorError`` messages that never echo
  query strings (some vendors, e.g. SerpAPI, take the API key as a query parameter).
* Token helpers: OAuth2 client-credentials (Adobe IMS, Zoom, Salesforce ...) and the Google
  service-account JWT bearer flow (google-auth when importable, otherwise ``cryptography``).

Credentials are only ever read from the connector's config block, which config/settings.yaml fills
from environment variables. Nothing in this module touches the network at import time.
"""

from __future__ import annotations

import base64
import email.utils
import json
import random
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, ClassVar

import httpx

from .base import ConnectorError, ConnectorNotConfigured

USER_AGENT = "CoforgeAgenticFramework/1.0 (+https://www.coforge.com) connector"
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})

_sleep = time.sleep  # indirection so tests can patch out the backoff wait
_lock = threading.RLock()


# ----------------------------------------------------------------------------- client + retries

class RetryingClient(httpx.Client):
    """httpx.Client that retries 429 (any method), 5xx (idempotent methods, or POST when the caller
    declares its POSTs read-only, e.g. report/search queries) and connection failures.

    Backoff is exponential (``backoff_s * 2**attempt`` plus jitter) and honours ``Retry-After``
    (seconds or HTTP date), capped at ``max_wait_s``.
    """

    def __init__(self, *args: Any, max_retries: int = 2, backoff_s: float = 0.5, max_wait_s: float = 30.0,
                 retry_post: bool = False, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.max_retries = max(0, int(max_retries))
        self.backoff_s = max(0.0, float(backoff_s))
        self.max_wait_s = max(0.0, float(max_wait_s))
        self.retry_post = retry_post

    def _backoff(self, attempt: int) -> float:
        base = self.backoff_s * (2 ** attempt)
        return min(self.max_wait_s, base + (random.uniform(0, self.backoff_s / 2) if self.backoff_s else 0.0))

    def _retry_after(self, response: httpx.Response, attempt: int) -> float:
        header = response.headers.get("retry-after")
        if header:
            try:
                return min(self.max_wait_s, max(0.0, float(header)))
            except ValueError:
                try:
                    when = email.utils.parsedate_to_datetime(header)
                    return min(self.max_wait_s, max(0.0, (when - datetime.now(timezone.utc)).total_seconds()))
                except (TypeError, ValueError):
                    pass
        return self._backoff(attempt)

    def send(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:  # type: ignore[override]
        attempt = 0
        while True:
            try:
                response = super().send(request, **kwargs)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
                # the request never reached the server, so a retry cannot duplicate a side effect
                if attempt >= self.max_retries:
                    raise
                _sleep(self._backoff(attempt))
                attempt += 1
                continue
            retryable = response.status_code in RETRY_STATUSES and (
                response.status_code == 429 or request.method in IDEMPOTENT_METHODS or self.retry_post)
            if not retryable or attempt >= self.max_retries:
                return response
            wait = self._retry_after(response, attempt)
            response.close()
            _sleep(wait)
            attempt += 1


def http_client(conn: Any, base_url: str | None = None, headers: dict[str, str] | None = None, timeout: float = 30,
                *, retry_post: bool = False, auth: Any = None) -> httpx.Client:
    """httpx.Client for an external provider.

    Uses ``conn.config["_transport"]`` when present (tests), ``timeout_s`` / ``max_retries`` /
    ``retry_backoff_s`` from the connector config when set. Never put ``content-type`` in
    ``headers``: it would override the encoding of form posts (token requests) - set it per request.
    """
    cfg = getattr(conn, "config", {}) or {}
    kwargs: dict[str, Any] = {
        "base_url": base_url or "",
        "headers": {"user-agent": USER_AGENT, **{k: v for k, v in (headers or {}).items() if v is not None}},
        "timeout": httpx.Timeout(float(cfg.get("timeout_s") or timeout)),
        "follow_redirects": True,
    }
    if auth is not None:
        kwargs["auth"] = auth
    transport = cfg.get("_transport")
    if transport is not None:
        kwargs["transport"] = transport
    return RetryingClient(max_retries=int(cfg.get("max_retries", 2)), backoff_s=float(cfg.get("retry_backoff_s", 0.5)),
                          retry_post=retry_post, **kwargs)


def _label(conn: Any) -> str:
    return f"{getattr(conn, 'kind', '?')}:{getattr(conn, 'provider', '?')}"


def check(resp: httpx.Response, conn: Any, action: str) -> httpx.Response:
    """Return ``resp`` when 2xx, else raise ConnectorError (query string stripped from the URL)."""
    if resp.is_success:
        return resp
    where = f"{resp.request.method} {str(resp.request.url).split('?')[0]}"
    hint = " - check the credentials, scopes and permissions configured for this connector" if resp.status_code in (401, 403) else ""
    detail = " ".join((resp.text or "").split())[:300]
    raise ConnectorError(f"{_label(conn)} {action} failed: HTTP {resp.status_code} from {where}{hint}. {detail}".strip())


def expect_json(resp: httpx.Response, conn: Any, action: str) -> Any:
    check(resp, conn, action)
    if not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError as exc:
        raise ConnectorError(f"{_label(conn)} {action}: expected JSON, got {resp.headers.get('content-type', '?')}") from exc


def basic_auth_header(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


# ----------------------------------------------------------------------------- provider mixin

class ExternalProvider:
    """Mixin for providers that call a third-party API.

    ``available`` is True when every key in ``required`` is configured (no network call); the first
    use of ``self.http`` builds the client lazily, so constructing a provider never needs network.
    """

    external: ClassVar[bool] = True
    required: ClassVar[tuple[str, ...]] = ()
    base_url: ClassVar[str] = ""
    retry_post: ClassVar[bool] = False       # True when every POST the adapter sends is a read-only query
    drop_headers: ClassVar[tuple[str, ...]] = ()  # httpx defaults a vendor rejects (e.g. Accept for AJO creates)
    config: dict[str, Any]

    @property
    def available(self) -> bool:
        return all(self.config.get(k) for k in self.required)

    def api_base(self) -> str:
        return str(self.config.get("base_url") or self.base_url).rstrip("/")

    def default_headers(self) -> dict[str, str]:
        return {}

    @property
    def http(self) -> httpx.Client:
        client = self.__dict__.get("_http_client")
        if client is None:
            with _lock:
                client = self.__dict__.get("_http_client")
                if client is None:
                    client = http_client(self, base_url=self.api_base(), headers=self.default_headers(),
                                         retry_post=self.retry_post)
                    for name in self.drop_headers:
                        client.headers.pop(name, None)
                    self.__dict__["_http_client"] = client
        return client

    def token_cache(self, name: str = "default") -> "TokenCache":
        with _lock:
            caches = self.__dict__.setdefault("_token_caches", {})
            return caches.setdefault(name, TokenCache())


# ----------------------------------------------------------------------------- tokens

class TokenCache:
    """Thread-safe in-process cache for one bearer token, refreshed 60 s before it expires."""

    def __init__(self) -> None:
        self.token: str | None = None
        self.expires_at = 0.0
        self.data: dict[str, Any] = {}
        self._lock = threading.Lock()

    def get(self, fetch: Callable[[], dict[str, Any]]) -> str:
        with self._lock:
            if self.token and time.time() < self.expires_at - 60:
                return self.token
            data = fetch() or {}
            token = data.get("access_token")
            if not token:
                raise ConnectorError("Token endpoint response did not contain an access_token")
            self.token, self.data = str(token), dict(data)
            self.expires_at = time.time() + float(data.get("expires_in") or 3600)
            return self.token


def client_credentials_token(conn: Any, token_url: str, *, client_id: str, client_secret: str, scope: str | None = None,
                             basic: bool = False, params: dict[str, Any] | None = None, data: dict[str, Any] | None = None,
                             cache: str = "oauth") -> str:
    """OAuth2 client-credentials grant (form-encoded). ``basic=True`` sends the client credentials as
    HTTP Basic auth (Zoom) instead of form fields (Adobe IMS, Salesforce)."""

    def fetch() -> dict[str, Any]:
        form: dict[str, Any] = dict(data if data is not None else {"grant_type": "client_credentials"})
        headers = {}
        if basic:
            headers["authorization"] = basic_auth_header(client_id, client_secret)
        else:
            form.update({"client_id": client_id, "client_secret": client_secret})
        if scope:
            form["scope"] = scope
        resp = conn.http.post(token_url, params=params, data=form or None, headers=headers)
        return expect_json(resp, conn, "OAuth token request")

    return conn.token_cache(cache).get(fetch)


ADOBE_IMS_URL = "https://ims-na1.adobelogin.com/ims/token/v3"


def adobe_ims_token(conn: Any, default_scopes: str) -> str:
    """Adobe IMS OAuth server-to-server token (client_credentials) - or a static ``access_token``.

    Config: client_id, client_secret (ADOBE_CLIENT_ID / ADOBE_CLIENT_SECRET), optional ``scopes``
    (comma string or list; must match the Developer Console credential), optional ``ims_url``.
    """
    if conn.config.get("access_token"):
        return str(conn.config["access_token"])
    creds = conn.require("client_id", "client_secret")
    scopes = conn.config.get("scopes") or default_scopes
    if isinstance(scopes, (list, tuple)):
        scopes = ",".join(str(s) for s in scopes)
    return client_credentials_token(conn, str(conn.config.get("ims_url") or ADOBE_IMS_URL), client_id=creds["client_id"],
                                    client_secret=creds["client_secret"], scope=str(scopes), cache="adobe_ims")


# --- Google service account (JWT bearer, RFC 7523) ------------------------------------------------

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def google_service_account_info(conn: Any) -> dict[str, Any]:
    cfg = conn.config
    raw = cfg.get("credentials_json")
    if raw:
        info = raw if isinstance(raw, dict) else json.loads(raw)
    elif cfg.get("credentials_file"):
        path = conn.settings.resolve(cfg["credentials_file"])
        if not path.exists():
            raise ConnectorNotConfigured(f"{_label(conn)}: credentials_file {path} does not exist")
        info = json.loads(path.read_text(encoding="utf-8"))
    else:
        raise ConnectorNotConfigured(
            f"{_label(conn)} needs Google credentials: set credentials_file (GOOGLE_APPLICATION_CREDENTIALS), "
            f"credentials_json or access_token in settings.connectors.{conn.kind}")
    if info.get("type", "service_account") != "service_account" or not info.get("private_key") or not info.get("client_email"):
        raise ConnectorNotConfigured(f"{_label(conn)}: credentials must be a service-account key (type=service_account, "
                                     "client_email, private_key); user/ADC credentials are not supported - use access_token")
    return info


def sign_service_account_jwt(info: dict[str, Any], claims: dict[str, Any]) -> str:
    """RS256-sign ``claims`` with the service-account key: google-auth when importable, else cryptography."""
    try:
        from google.auth import crypt as google_crypt
        from google.auth import jwt as google_jwt
    except ImportError:
        google_jwt = None
    if google_jwt is not None:
        token = google_jwt.encode(google_crypt.RSASigner.from_service_account_info(info), claims)
        return token.decode("ascii") if isinstance(token, bytes) else str(token)
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError as exc:
        raise ConnectorNotConfigured(
            "Google service-account auth needs google-auth (pip install 'coforge-agentic-framework[google]') or the "
            "'cryptography' package; alternatively configure a pre-issued access_token") from exc
    header = {"alg": "RS256", "typ": "JWT"}
    if info.get("private_key_id"):
        header["kid"] = info["private_key_id"]
    signing_input = ".".join(_b64url(json.dumps(part, separators=(",", ":")).encode()) for part in (header, claims))
    key = serialization.load_pem_private_key(str(info["private_key"]).encode(), password=None)
    signature = key.sign(signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())  # type: ignore[union-attr,call-arg]
    return f"{signing_input}.{_b64url(signature)}"


def google_access_token(conn: Any, scopes: list[str]) -> str:
    """Bearer token for Google APIs: static ``access_token`` or service-account JWT bearer exchange."""
    if conn.config.get("access_token"):
        return str(conn.config["access_token"])
    info = google_service_account_info(conn)

    def fetch() -> dict[str, Any]:
        now = int(time.time())
        aud = info.get("token_uri") or GOOGLE_TOKEN_URL
        claims: dict[str, Any] = {"iss": info["client_email"], "scope": " ".join(scopes), "aud": aud, "iat": now, "exp": now + 3600}
        if conn.config.get("subject"):  # domain-wide delegation
            claims["sub"] = conn.config["subject"]
        assertion = sign_service_account_jwt(info, claims)
        resp = conn.http.post(aud, data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion})
        return expect_json(resp, conn, "Google OAuth token exchange")

    return conn.token_cache("google").get(fetch)
