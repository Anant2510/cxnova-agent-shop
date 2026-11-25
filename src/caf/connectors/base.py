"""Connector base class and provider registry.

Each connector *kind* (search, analytics, cms, esp ...) defines an interface; each *provider*
(none, csv, ga4, adobe_analytics, aem, contentful ...) implements it. config/settings.yaml picks
the provider per kind, so swapping GA4 for Adobe Analytics - or Braze for AJO - is configuration.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:  # pragma: no cover
    from . import ConnectorHub

_PROVIDERS: dict[str, dict[str, type["Connector"]]] = {}


class ConnectorError(RuntimeError):
    pass


class ConnectorNotConfigured(ConnectorError):
    pass


def connector(kind: str, provider: str):
    def deco(cls: type["Connector"]) -> type["Connector"]:
        cls.kind = kind
        cls.provider = provider
        _PROVIDERS.setdefault(kind, {})[provider] = cls
        return cls

    return deco


def providers(kind: str | None = None) -> dict[str, list[str]] | list[str]:
    if kind:
        return sorted(_PROVIDERS.get(kind, {}))
    return {k: sorted(v) for k, v in sorted(_PROVIDERS.items())}


def provider_class(kind: str, provider: str) -> type["Connector"]:
    try:
        return _PROVIDERS[kind][provider]
    except KeyError as exc:
        raise ConnectorError(f"No provider '{provider}' for connector kind '{kind}'. Available: {providers(kind)}") from exc


class Connector:
    kind: ClassVar[str] = ""
    provider: ClassVar[str] = ""
    external: ClassVar[bool] = False       # True when the provider calls a third-party API
    description: ClassVar[str] = ""

    def __init__(self, config: dict[str, Any], hub: "ConnectorHub"):
        self.config = dict(config or {})
        self.hub = hub
        self.settings = hub.settings

    # helpers ---------------------------------------------------------------------------------
    def path(self, key: str = "path", default: str | None = None) -> Path:
        value = self.config.get(key, default)
        if value is None:
            raise ConnectorNotConfigured(f"{self.kind}:{self.provider} needs '{key}' in settings.connectors.{self.kind}")
        resolved = self.settings.resolve(value)
        try:   # the bundled demo data is not the caller's: a run that read it says so (see BaseAgent.run)
            if Path(resolved).resolve().is_relative_to(Path(self.settings.sample_data).resolve()):
                self.hub.demo[f"{self.kind}:{self.provider}"] = str(value)
        except Exception:  # pragma: no cover - a hub or settings without these is a test double
            pass
        return resolved

    def require(self, *keys: str) -> dict[str, Any]:
        missing = [k for k in keys if not self.config.get(k)]
        if missing:
            raise ConnectorNotConfigured(
                f"{self.kind}:{self.provider} is missing {missing}. Set them in config/settings.yaml (connectors.{self.kind}) or env.")
        return {k: self.config[k] for k in keys}

    @property
    def available(self) -> bool:
        return True

    def healthcheck(self) -> dict[str, Any]:
        return {"ok": self.available, "provider": self.provider, "external": self.external}

    def describe(self) -> dict[str, Any]:
        return {"kind": self.kind, "provider": self.provider, "external": self.external, "available": self.available,
                "description": self.description or (self.__doc__ or "").strip().split("\n")[0]}
