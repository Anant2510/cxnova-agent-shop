"""Connector hub: the provider and the settings of each connector kind, for the run it serves.

The operator's own runs (the CLI, the console, the operator token) use the server's connections, as config/settings.yaml
names them. A customer's run is different, because a connection is somebody's system:

* a kind the customer connected itself (its policy's `connections`) is served by that connection, with the credentials
  the customer gave, and by nothing else;
* the layer's own services (reading documents, fetching public pages, web search, translation) are the server's for
  everyone, as is whatever else the operator shares (`security.shared_connectors`);
* the layer's own stores (records, the vector index, short links) are the server's too, kept apart per customer;
* every other kind is someone's estate. Where the server holds only the bundled demo data for it, a customer's run reads
  that and its result says so. Where the server is connected to a real system (the operator's own), a customer's run is
  kept from it and gets the bundled local provider instead.

What the customer's policy lets agents do with each kind (`reach.systems`: none, read, read_write) is enforced here too.
"""

from __future__ import annotations

import importlib
import pkgutil
import threading
from pathlib import Path
from typing import Any

from .base import (Connector, ConnectorError, ConnectorNotAllowed, ConnectorNotConfigured, connector, provider_class,
                   providers)

DEFAULT_PROVIDERS = {
    "ingestion": "local",
    "crawler": "httpx",
    "search": "none",
    "keyword_data": "csv",
    "analytics": "csv",
    "behaviour": "csv",
    "experiments": "json",
    "cms": "file",
    "esp": "file",
    "tasks": "json_file",
    "webinar": "csv",
    "translation": "llm",
    "newswire": "file",
    "flags": "file",
    "warehouse": "duckdb",
    "vectorstore": "tfidf",
    "store": "sqlite",
    "shortlink": "sqlite",
    "crm": "csv",
}

SERVICES = ("ingestion", "crawler", "search", "translation")   # the layer's own services: the server's, for every customer's run
STORES = ("store", "vectorstore", "shortlink")                 # the layer's own storage: the server's, kept apart per customer
ACCESS = ("none", "read", "read_write")                        # what a policy lets agents do with a kind of system
# What each kind is called and is, as a person reads it (the portal, the admin console and `caf shop systems` show these;
# the policy and the CLI name a kind by its id).
KINDS = {
    "analytics": ("Analytics", "Traffic, conversions and the channel mix"),
    "behaviour": ("Behaviour analytics", "Clicks, scroll depth and heatmaps on a page"),
    "keyword_data": ("Search data", "Search queries and rankings for the site"),
    "experiments": ("Testing tool", "Experiments and their results"),
    "flags": ("Feature flags", "Where a test variation is delivered"),
    "cms": ("CMS", "Where pages and articles are published"),
    "esp": ("E-mail platform", "Where campaigns are exported"),
    "tasks": ("Work tracker", "Where tasks are created"),
    "webinar": ("Webinar platform", "Registrants and attendance"),
    "newswire": ("Newswire", "Press release distribution"),
    "crm": ("CRM", "Contacts and their history"),
    "warehouse": ("Data warehouse", "Read with SQL"),
    "search": ("Web search", "Finding pages on the public web"),
    "crawler": ("Web fetching", "Reading public web pages"),
    "translation": ("Translation", "Machine translation"),
    "ingestion": ("Document reading", "The documents and links a run is given"),
    "store": ("Record store", "Quote bank, learnings, backlog, research caches"),
    "vectorstore": ("Retrieval index", "What agents search for supporting material"),
    "shortlink": ("Short links", "Tracked links for print and QR codes"),
}

_loaded = False
_load_lock = threading.Lock()


def load_connectors() -> None:
    global _loaded
    with _load_lock:
        if _loaded:
            return
        import caf.connectors as pkg

        for mod in pkgutil.walk_packages(pkg.__path__, prefix="caf.connectors."):
            if mod.name.endswith((".base",)):
                continue
            importlib.import_module(mod.name)
        _loaded = True


def all_kinds() -> list[str]:
    """Every connector kind this layer has, the customer's estate first."""
    load_connectors()
    known = set(DEFAULT_PROVIDERS) | set(providers().keys())  # type: ignore[union-attr]
    order = list(KINDS)
    return [k for k in order if k in known] + sorted(known - set(order))


def external_providers(kind: str) -> list[str]:
    """The providers of a kind that reach a system outside this server: what a customer can connect."""
    load_connectors()
    return [p for p in providers(kind) if provider_class(kind, p).external]  # type: ignore[union-attr]


def provider_help(kind: str, provider: str) -> dict[str, Any]:
    """What someone connecting this provider is shown: what it is, the settings it cannot do without, and the sentence
    of its documentation that lists its settings (the operator's file and environment hints taken out)."""
    import re

    cls = provider_class(kind, provider)
    doc = " ".join((cls.__doc__ or "").replace("``", "").split())
    listed = ""
    found = re.search(r"Config[^:]*:\s*", doc)
    if found:
        depth = 0
        rest = doc[found.end():]
        for i, ch in enumerate(rest):   # up to the first full stop outside brackets: "(e.g. https://...)" does not end it
            depth += ch in "([{"
            depth -= ch in ")]}"
            if ch == "." and depth <= 0 and (i + 1 == len(rest) or rest[i + 1] == " "):
                rest = rest[:i]
                break
        listed = re.sub(r"credentials_file(\s*\([^)]*\))?\s*\|\s*", "", rest)        # a file on the server: the operator's, not a connection's
        listed = re.sub(r"\(([A-Z][A-Z0-9_]{3,})(, |\))", lambda m: "(" if m.group(2) == ", " else "", listed).replace(" ()", "")
        listed = re.sub(r",?\s*engine_options \([^)]*\)", "", listed).strip()
    return {"provider": provider, "what": doc.split(". ")[0].rstrip(".")[:160], "needs": list(getattr(cls, "required", ()) or ()),
            "settings": listed[:600]}


class ConnectorHub:
    def __init__(self, settings: Any, llm: Any = None):
        load_connectors()
        self.settings = settings
        self.llm = llm
        self._cache: dict[tuple[str, str], Connector] = {}
        self._resolved: dict[str, tuple[dict[str, Any], str]] = {}
        self.demo: dict[str, str] = {}   # "kind:provider" -> the bundled sample path it read in this run
        self.owners: dict[str, str] = {}  # "kind:provider" -> customer | layer, for each connector this run was given
        self._lock = threading.RLock()
        tenant = dict(getattr(settings, "tenant", None) or {})
        self.tenant: str | None = str(tenant["id"]) if tenant.get("id") else None   # the customer this run is for
        self._own = {str(k): dict(v) for k, v in (tenant.get("connections") or {}).items() if isinstance(v, dict) and v.get("provider")}
        self._access = {str(k): str(v) for k, v in (tenant.get("access") or {}).items()}

    # ------------------------------------------------------------------ whose connection serves a kind
    def shared(self, kind: str) -> bool:
        """True when a customer's run may use the server's own connection for this kind. Unset, that is the layer's
        services; `security.shared_connectors` names other kinds, or `all` (the image in a customer's own cloud, where
        the server's systems are the customer's), or `none`."""
        named = (getattr(self.settings, "security", None) or {}).get("shared_connectors")
        names = [str(n).strip().lower() for n in (named.split(",") if isinstance(named, str) else (named or [])) if str(n).strip()]
        if not names:
            return kind in SERVICES
        return "all" in names or "*" in names or kind in names

    def _in_samples(self, path: Any) -> bool:
        try:
            return Path(self.settings.resolve(path)).resolve().is_relative_to(Path(self.settings.sample_data).resolve())
        except Exception:  # noqa: BLE001 - a path that cannot be resolved is not the bundled data
            return False

    def _is_bundled(self, kind: str, cfg: dict[str, Any]) -> bool:
        """The server's block for this kind is the bundled local provider on the bundled data: nobody's system."""
        provider = str(cfg.get("provider") or DEFAULT_PROVIDERS.get(kind, "none")).strip()
        try:
            if provider_class(kind, provider).external:
                return False
        except ConnectorError:
            return False
        return not cfg.get("path") or self._in_samples(cfg["path"])

    def config_for(self, kind: str) -> tuple[dict[str, Any], str]:
        """The settings that serve this kind in this run, and whose they are: "customer" (the caller's own connection)
        or "layer" (this server's)."""
        if not self.tenant:   # the operator's own runs: the server's block, read as it is now
            return self.settings.connector_config(kind), "layer"
        with self._lock:
            if kind not in self._resolved:   # a customer's: worked out once per run (credentials are decrypted here)
                self._resolved[kind] = self._resolve(kind)
            return self._resolved[kind]

    def _resolve(self, kind: str) -> tuple[dict[str, Any], str]:
        server = self.settings.connector_config(kind)
        own = self._own.get(kind)
        if own:
            from ..core import vault

            try:
                return vault.unlock(own), "customer"
            except RuntimeError as exc:
                raise ConnectorNotConfigured(f"{kind}:{own.get('provider')}: the credentials stored for this connection "
                                             f"cannot be read here: {exc}") from exc
        if kind in STORES or self.shared(kind) or self._is_bundled(kind, server):
            return server, "layer"
        # the server is connected to a real system for this kind (the operator's own): not this customer's to use
        cfg: dict[str, Any] = {"provider": DEFAULT_PROVIDERS.get(kind, "none")}
        if server.get("path") and self._in_samples(server["path"]):
            cfg["path"] = server["path"]
        return cfg, "layer"

    def provider_for(self, kind: str) -> str:
        cfg, _ = self.config_for(kind)
        return str(cfg.get("provider") or DEFAULT_PROVIDERS.get(kind, "none")).strip()

    def owner_of(self, kind: str) -> str:
        return self.config_for(kind)[1]

    # ------------------------------------------------------------------ what the caller's policy allows
    def access(self, kind: str, provider: str | None = None) -> str:
        """What the caller's policy lets agents do with this kind of system: none, read, or read_write (the default, and
        always the operator's). The policy may name the kind or the product (`analytics: read`, `ga4: read`)."""
        if not self.tenant:
            return "read_write"
        level = self._access.get(kind) or (self._access.get(provider) if provider else None) or "read_write"
        return level if level in ACCESS else "none"   # a word this layer does not know opens nothing

    def write_refusal(self, kind: str) -> str | None:
        """Why agents may not write to this kind of system for this caller, or None when they may."""
        if not self.tenant:
            return None
        try:
            provider: str | None = self.provider_for(kind)
        except ConnectorError:
            provider = None
        level = self.access(kind, provider)
        if level == "read_write":
            return None
        if level == "read":
            return f"The policy of this account lets agents read the {kind} system, not write to it (reach.systems: {kind}: read)"
        return f"The policy of this account keeps agents from the {kind} system (reach.systems: {kind}: none)"

    # ------------------------------------------------------------------ connectors
    def get(self, kind: str, provider: str | None = None) -> Connector:
        cfg, owner = self.config_for(kind)
        active = str(cfg.get("provider") or DEFAULT_PROVIDERS.get(kind, "none")).strip()
        provider = provider or active
        if self.access(kind, provider) == "none":
            raise ConnectorNotAllowed(self.write_refusal(kind) or f"{kind} is closed to agents by this account's policy")
        key = (kind, provider)
        with self._lock:
            if key not in self._cache:
                cls = provider_class(kind, provider)
                if not self.tenant:
                    if provider != cfg.get("provider"):   # a provider asked for by name beside the active one
                        cfg = {**cfg.get("providers", {}).get(provider, {})}
                elif provider != active:
                    cfg, owner = self._beside(kind, provider, cls)
                conn = cls(cfg, self)
                conn.owner = owner
                self._cache[key] = conn
            conn = self._cache[key]
        self.owners[f"{kind}:{provider}"] = getattr(conn, "owner", "layer")   # a stand-in placed in the hub by a test has none
        return conn

    def _beside(self, kind: str, provider: str, cls: type[Connector]) -> tuple[dict[str, Any], str]:
        """A provider a customer's run asks for by name, beside the one that serves the kind: a local provider is
        always there; an external one only where the server's is shared with customers."""
        server = self.settings.connector_config(kind)
        if not cls.external:
            return {**server.get("providers", {}).get(provider, {})}, "layer"
        if self.shared(kind):
            return ({**server.get("providers", {}).get(provider, {})} if provider != server.get("provider") else server), "layer"
        raise ConnectorNotConfigured(f"{kind}:{provider} is not connected for this account")

    def describe(self) -> list[dict[str, Any]]:
        out = []
        for kind in sorted(set(DEFAULT_PROVIDERS) | set(providers().keys())):  # type: ignore[arg-type]
            try:
                active = self.provider_for(kind)
                info = self.get(kind).describe()
            except Exception as exc:  # misconfigured provider, or closed by the caller's policy
                info = {"kind": kind, "provider": (self._own.get(kind) or {}).get("provider") or DEFAULT_PROVIDERS.get(kind, "none"),
                        "available": False, "error": str(exc)}
                active = info["provider"]
            info["providers"] = providers(kind)
            if self.tenant:
                info["owner"] = "customer" if kind in self._own else "layer"
                info["access"] = self.access(kind, active)
            out.append(info)
        return out


__all__ = ["ACCESS", "Connector", "ConnectorError", "ConnectorHub", "ConnectorNotAllowed", "ConnectorNotConfigured", "KINDS",
           "SERVICES", "STORES", "all_kinds", "connector", "external_providers", "load_connectors", "provider_help", "providers"]
