"""Security policy for what agents may reach on behalf of a caller.

Two modes, chosen by the surface that creates the run:

* ``local`` - the CLI and library use. The caller is the operator at the keyboard, so agents may read
  any local file the operator names and fetch private-network hosts (intranet staging sites).
* ``service`` - the REST API, MCP (HTTP and stdio) and A2A. Callers are remote users or language
  models, so agents may only fetch public internet hosts (plus configured fixture hosts and
  ``security.allowed_hosts``) and read files under ``security.file_roots`` and the runs folder.

The ``security`` block of config/settings.yaml tunes service mode; see docs/ARCHITECTURE.md.
Residual risk: host names are checked when a request is made, so a DNS answer that changes between
the check and the connection (DNS rebinding) is not caught; run the service with network egress
controls (no route to cloud metadata endpoints or internal networks) for defence in depth.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
# names that resolve to internal services in common clouds and container platforms
INTERNAL_NAMES = {"localhost", "metadata", "metadata.google.internal", "instance-data", "instance-data.ec2.internal",
                  "kubernetes", "kubernetes.default", "kubernetes.default.svc"}
TRUTHY = {"1", "true", "yes", "on"}


class PolicyError(PermissionError):
    """Raised when a request would reach something the security policy does not allow."""


def _bool(value: Any) -> bool:
    return value if isinstance(value, bool) else str(value or "").strip().lower() in TRUTHY


def _list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return [str(v).strip() for v in value if str(v).strip()]


def validate_run_id(run_id: Any) -> str:
    """Run ids name folders under runs/: letters, digits, '-' and '_' only (no dots, no separators)."""
    if not isinstance(run_id, str) or not RUN_ID.match(run_id):
        raise PolicyError(f"Invalid run id {run_id!r}: use letters, digits, '-' or '_' (max 128)")
    return run_id


def is_safe_id(value: Any) -> bool:
    """True for identifiers that are safe to use as a file name (experiment ids, slugs ...)."""
    return isinstance(value, str) and bool(SAFE_ID.match(value)) and ".." not in value


def is_public_address(addr: str) -> bool:
    """True only for globally routable unicast addresses (no loopback, private, link-local, CGNAT, reserved)."""
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        mapped = ip.ipv4_mapped or ip.sixtofour
        if mapped is not None:
            ip = mapped
        elif ip.teredo:
            ip = ip.teredo[1]
    return bool(ip.is_global) and not ip.is_multicast


_dns_cache: dict[str, tuple[float, list[str]]] = {}
_dns_lock = threading.Lock()


def _resolve(host: str) -> list[str]:
    """Addresses for a host name (cached for a minute). [] when it cannot be resolved here: the request then
    fails on its own, or goes through an egress proxy that does its own resolution."""
    now = time.monotonic()
    with _dns_lock:
        hit = _dns_cache.get(host)
        if hit and now - hit[0] < 60:
            return hit[1]
    try:
        addrs = sorted({info[4][0] for info in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)})
    except (socket.gaierror, UnicodeError, OSError):
        addrs = []
    with _dns_lock:
        _dns_cache[host] = (now, addrs)
    return addrs


def service_settings(settings: Any = None) -> Any:
    """A copy of the settings with the security mode set to 'service' (used by REST, MCP and A2A)."""
    from ..config import get_settings

    base = settings or get_settings()
    return base.model_copy(update={"security": {**dict(base.security or {}), "mode": "service"}})


class Policy:
    """What a run may reach. Build it from the run's settings: ``Policy(ctx.settings)``."""

    def __init__(self, settings: Any):
        cfg = dict(getattr(settings, "security", None) or {})
        self.settings = settings
        self.mode = str(cfg.get("mode") or "local").lower()
        self.service = self.mode == "service"
        self.allow_private_network = (not self.service) or _bool(cfg.get("allow_private_network"))
        self.allow_any_file = (not self.service) or _bool(cfg.get("allow_any_local_file"))
        self.allowed_hosts = [h.lower() for h in _list(cfg.get("allowed_hosts"))]
        fixtures = (settings.connector_config("crawler").get("fixtures") or {}) if hasattr(settings, "connector_config") else {}
        self.fixture_hosts = {str(h).lower() for h in fixtures}
        roots = _list(cfg.get("file_roots")) or ["sample_data"]
        self.file_roots = [Path(settings.resolve(r)).resolve() for r in roots] + [Path(settings.runs_dir).resolve()]

    # ------------------------------------------------------------------------------ network
    def _host_listed(self, host: str) -> bool:
        return any(host == h or (h.startswith("*.") and (host.endswith(h[1:]) or host == h[2:])) for h in self.allowed_hosts)

    def check_url(self, url: str) -> str:
        """Return the URL if this run may fetch it, else raise PolicyError."""
        parsed = urlparse(str(url or "").strip())
        scheme = (parsed.scheme or "").lower()
        if scheme == "file":
            self.check_path(url2pathname(unquote(parsed.path or "")))
            return url
        if scheme in ("about", "data"):
            return url
        if scheme not in ("http", "https"):
            raise PolicyError(f"URL scheme '{scheme or '(none)'}' is not allowed: use http or https")
        host = (parsed.hostname or "").lower().rstrip(".")
        if not host:
            raise PolicyError(f"URL has no host: {url}")
        if host in self.fixture_hosts:
            return url
        if self.allowed_hosts and not self._host_listed(host):
            raise PolicyError(f"Host '{host}' is not in security.allowed_hosts")
        if self.allow_private_network or self._host_listed(host):
            return url
        if host in INTERNAL_NAMES or host.endswith((".localhost", ".internal", ".local", ".svc", ".cluster.local")):
            raise PolicyError(f"Host '{host}' is an internal name; set security.allow_private_network to allow it")
        literal = host.strip("[]")
        try:
            ipaddress.ip_address(literal)
            addrs = [literal]
        except ValueError:
            addrs = _resolve(host)
        for addr in addrs:
            if not is_public_address(addr):
                raise PolicyError(f"Host '{host}' resolves to a non-public address ({addr}); "
                                  "set security.allow_private_network to allow private-network fetches")
        return url

    def url_allowed(self, url: str) -> bool:
        try:
            self.check_url(url)
            return True
        except PolicyError:
            return False

    # ------------------------------------------------------------------------------ files
    def check_path(self, path: str | Path) -> Path:
        """Resolve a local path (relative to the framework home) and return it if this run may read it."""
        raw = Path(str(path)).expanduser()
        p = raw if raw.is_absolute() else Path(self.settings.resolve(raw))
        resolved = p.resolve()
        if self.allow_any_file or any(resolved == root or resolved.is_relative_to(root) for root in self.file_roots):
            return resolved
        raise PolicyError(f"Reading local files outside the allowed folders is disabled for this service: {raw} "
                          "(see security.file_roots)")

    def path_allowed(self, path: str | Path) -> bool:
        try:
            self.check_path(path)
            return True
        except (PolicyError, OSError, ValueError):
            return False
