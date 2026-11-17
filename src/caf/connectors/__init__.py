"""Connector hub: resolves the configured provider for each connector kind."""

from __future__ import annotations

import importlib
import pkgutil
import threading
from typing import Any

from .base import Connector, ConnectorError, ConnectorNotConfigured, connector, provider_class, providers

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


class ConnectorHub:
    def __init__(self, settings: Any, llm: Any = None):
        load_connectors()
        self.settings = settings
        self.llm = llm
        self._cache: dict[tuple[str, str], Connector] = {}
        self._lock = threading.Lock()

    def provider_for(self, kind: str) -> str:
        cfg = self.settings.connector_config(kind)
        return str(cfg.get("provider") or DEFAULT_PROVIDERS.get(kind, "none")).strip()

    def get(self, kind: str, provider: str | None = None) -> Connector:
        provider = provider or self.provider_for(kind)
        key = (kind, provider)
        with self._lock:
            if key not in self._cache:
                cls = provider_class(kind, provider)
                cfg = self.settings.connector_config(kind)
                if provider != cfg.get("provider"):
                    cfg = {**cfg.get("providers", {}).get(provider, {})}
                self._cache[key] = cls(cfg, self)
            return self._cache[key]

    def describe(self) -> list[dict[str, Any]]:
        out = []
        for kind in sorted(set(DEFAULT_PROVIDERS) | set(providers().keys())):  # type: ignore[arg-type]
            active = self.provider_for(kind)
            try:
                info = self.get(kind).describe()
            except Exception as exc:  # misconfigured provider
                info = {"kind": kind, "provider": active, "available": False, "error": str(exc)}
            info["providers"] = providers(kind)
            out.append(info)
        return out


__all__ = ["Connector", "ConnectorError", "ConnectorHub", "ConnectorNotConfigured", "connector", "load_connectors", "providers"]
