"""A customer's policy: which models its agents may call, what reaches them, what the agents may touch, and limits.

The policy is a document the customer's admin owns (YAML or JSON, see config/policies/example-customer.yaml). The
shop turns it into the runtime pieces the framework already has: an LLM facade for the customer's own model
deployment (run modes 2 and 3), the security policy for the run (allowed websites), a masker applied to every
prompt, and limits checked before a run starts. Calls outside the policy are refused, never quietly redirected.

It also names the customer's own systems (`connections`: their analytics, CMS, e-mail platform ...) and what the agents
may do with each (`reach.systems`). A customer's runs reach those systems and no other customer's; credentials are
stored encrypted or named as environment variables on the server, and are never shown back.
"""

from __future__ import annotations

import os
import re
from typing import Any

from pydantic import BaseModel, Field, field_validator

from ..config import LLMSettings, Settings
from ..core.llm import SUPPORTED_PROVIDERS
from . import secrets as vault

TIERS = ("fast", "balanced", "pro", "complex")
COFORGE = "coforge"   # the shop's own deployment (run mode 1)
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
OWN_SECRETS = ("CAF_API_TOKEN", "CAF_SECRET_KEY")   # this layer's own: no policy may name them as a credential to send out
# settings a connection may not carry: they read files on this server or are the operator's to set
OPERATOR_SETTINGS = ("path", "credentials_file", "fixtures", "providers", "engine_options")
MAX_CONNECTION_BYTES = 32_000


def env_prefix(customer_id: str) -> str:
    """What the name of an environment variable holding one of this customer's credentials starts with: the customer's
    id in capitals (harbor-bank -> HARBOR_BANK_). One account's settings cannot name another's secret, or the server's."""
    return re.sub(r"[^A-Z0-9]+", "_", str(customer_id).upper()).strip("_") + "_"


class ModelPolicy(BaseModel):
    provider: str = Field(default=COFORGE, description="coforge (the shop's deployment) or anthropic | anthropic_foundry | "
                                                       "azure_openai | openai | bedrock | ollama")
    base_url: str | None = Field(default=None, description="Anthropic-compatible base URL (Foundry Target URI accepted)")
    endpoint: str | None = Field(default=None, description="Azure OpenAI endpoint")
    api_version: str | None = None
    region: str | None = Field(default=None, description="Bedrock region, or the region the customer requires")
    resource: str | None = Field(default=None, description="Foundry resource name (alternative to base_url)")
    api_key_env: str | None = Field(default=None, description="Name of the environment variable on the server holding the key")
    api_key: str | None = Field(default=None, description="The key itself; stored encrypted (needs CAF_SECRET_KEY)")
    tiers: dict[str, str] = Field(default_factory=dict, description="fast/balanced/pro/complex -> the customer's deployment names")
    images_allowed: bool = True
    max_tokens: int | None = None
    timeout_s: float | None = None

    @field_validator("provider")
    @classmethod
    def _provider(cls, v: str) -> str:
        v = (v or COFORGE).strip().lower()
        if v != COFORGE and v not in SUPPORTED_PROVIDERS:
            raise ValueError(f"provider must be '{COFORGE}' or one of {SUPPORTED_PROVIDERS}")
        return v

    @field_validator("tiers")
    @classmethod
    def _tiers(cls, v: dict[str, str]) -> dict[str, str]:
        bad = set(v) - set(TIERS)
        if bad:
            raise ValueError(f"unknown tiers {sorted(bad)}; use {TIERS}")
        return {k: str(val) for k, val in v.items() if str(val).strip()}

    @property
    def uses_customer_model(self) -> bool:
        return self.provider not in (COFORGE, "")


class DataPolicy(BaseModel):
    mask_before_model: list[str] = Field(default_factory=list, description="email, phone, policy_number, card_number, iban")
    keep_run_records_days: int | None = None
    provider_training: str = "forbidden"

    @field_validator("mask_before_model")
    @classmethod
    def _kinds(cls, v: list[str]) -> list[str]:
        bad = [k for k in v if k not in MASKERS]
        if bad:
            raise ValueError(f"unknown masking kinds {bad}; use {sorted(MASKERS)}")
        return v


class ReachPolicy(BaseModel):
    websites: list[str] = Field(default_factory=list, description="Hosts the agents may fetch, e.g. '*.example.com'; empty = any public host")
    systems: dict[str, str] = Field(default_factory=dict, description="What the agents may do with each kind of system (cms, analytics, "
                                                                      "crm ... or a product, e.g. ga4): none | read | read_write. "
                                                                      "A kind that is not named is read_write; writes wait for approval either way.")


class ActionsPolicy(BaseModel):
    need_approval: list[str] = Field(default_factory=lambda: ["*"], description="Publish-type actions held for an approver ('*' = all)")
    approvers: list[str] = Field(default_factory=list)


class LimitsPolicy(BaseModel):
    runs_per_day: int | None = None
    tokens_per_month: int | None = None
    model_spend_per_month_usd: float | None = Field(default=None, description="Informative: the shop meters tokens, the provider bills")


class CustomerPolicy(BaseModel):
    version: int = 1
    signed_by: str | None = None
    models: ModelPolicy = Field(default_factory=ModelPolicy)
    data: DataPolicy = Field(default_factory=DataPolicy)
    reach: ReachPolicy = Field(default_factory=ReachPolicy)
    actions: ActionsPolicy = Field(default_factory=ActionsPolicy)
    limits: LimitsPolicy = Field(default_factory=LimitsPolicy)
    connections: dict[str, dict[str, Any]] = Field(default_factory=dict, description="The customer's own systems, by kind: "
                                                   "{provider: ..., and that provider's settings}. Credentials are stored encrypted, "
                                                   "or named as `<setting>_env`: an environment variable on the server.")

    # ---- storage ------------------------------------------------------------------------------
    def for_storage(self, previous: "CustomerPolicy | None" = None) -> dict[str, Any]:
        """The policy as stored: every credential encrypted, never in clear. A credential that arrives as the mask (the
        policy was read, edited and saved back) keeps what `previous` has stored for it."""
        data = self.model_dump(mode="json")
        key = data["models"].get("api_key")
        if key == vault.MASK:
            key = data["models"]["api_key"] = (previous.models.api_key if previous is not None else None) or None
            if not key:
                raise ValueError(f"models.api_key is the mask ({vault.MASK}) and nothing is stored for it yet: give the key itself")
        if key and not vault.is_encrypted(key):
            data["models"]["api_key"] = vault.encrypt(key)
        before = previous.connections if previous is not None else {}
        data["connections"] = {}
        for kind, cfg in self.connections.items():
            try:
                same = before.get(kind) if (before.get(kind) or {}).get("provider") == cfg.get("provider") else None
                data["connections"][kind] = vault.seal(dict(cfg), same)
            except ValueError as exc:
                raise ValueError(f"connections.{kind}: {exc}") from exc
        return data

    def redacted(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        if data["models"].get("api_key"):
            data["models"]["api_key"] = vault.MASK
        data["connections"] = {kind: vault.mask(cfg) for kind, cfg in self.connections.items()}
        return data

    def problems(self, customer_id: str, settings: Settings | None = None) -> list[str]:
        """What stops this policy from being saved, each as a sentence: a connection to something that is not there to
        connect, a setting that would read this server's files or name a secret that is not this customer's, an address
        that is not public, a reach level that is not none / read / read_write. Whether a connection is complete is not
        judged here: the systems view says what a connection still needs."""
        from ..connectors import ACCESS, all_kinds, external_providers, providers
        from ..core.security import Policy, PolicyError, service_settings

        out: list[str] = []
        kinds = all_kinds()
        products = {p for k in kinds for p in providers(k)}   # type: ignore[union-attr]
        for name, level in self.reach.systems.items():
            if name not in kinds and name not in products:
                out.append(f"reach.systems: '{name}' is neither a kind of system this layer has ({', '.join(kinds)}) nor a product it connects")
            if level not in ACCESS:
                out.append(f"reach.systems.{name}: '{level}' is not one of {', '.join(ACCESS)}")
        if self.models.api_key_env in OWN_SECRETS:
            out.append(f"models.api_key_env: {self.models.api_key_env} is this layer's own secret, not a model key")
        guard = Policy(service_settings(settings)) if settings is not None else None
        prefix = env_prefix(customer_id)
        for kind, cfg in self.connections.items():
            where = f"connections.{kind}"
            if kind not in kinds:
                out.append(f"{where}: this layer has no '{kind}' kind of system. It has: {', '.join(kinds)}")
                continue
            offered = external_providers(kind)
            provider = str(cfg.get("provider") or "").strip()
            if provider not in offered:
                if not provider:
                    why = "name the provider"
                elif provider in providers(kind):   # type: ignore[operator]
                    why = f"'{provider}' works on this server's own files, so it is not a customer's to connect"
                else:
                    why = f"'{provider}' is not a provider of this kind"
                out.append(f"{where}: {why}. " + (f"A customer can connect: {', '.join(offered)}" if offered else
                                                 "This kind is the layer's own and has nothing a customer connects"))
                continue
            if len(repr(cfg)) > MAX_CONNECTION_BYTES:
                out.append(f"{where}: the settings are larger than a connection's can be")
                continue
            for key, value in cfg.items():
                if key == "provider":
                    continue
                if key.startswith("_") or key in OPERATOR_SETTINGS:
                    out.append(f"{where}.{key}: this setting reads files on the server and is the operator's, not a connection's"
                               + (" (give the service-account key itself as credentials_json)" if key == "credentials_file" else ""))
                elif key.endswith("_env"):
                    if not (isinstance(value, str) and ENV_NAME.match(value) and value.upper().startswith(prefix)):
                        out.append(f"{where}.{key}: name an environment variable on the server that starts with {prefix} "
                                   "(the account's id), so no account's settings can name another's secret or the server's own")
                elif guard is not None and isinstance(value, str) and re.match(r"^[a-z][a-z0-9+.\-]*://", value, re.I):
                    try:
                        (guard.check_database if (kind, key) == ("warehouse", "url") else guard.check_endpoint)(value, resolve=False)
                    except PolicyError as exc:
                        out.append(f"{where}.{key}: {exc}")
        return out

    def access(self) -> dict[str, str]:
        """reach.systems as the connector hub reads it."""
        return {str(k): str(v) for k, v in self.reach.systems.items()}

    # ---- runtime ------------------------------------------------------------------------------
    def credential(self) -> str | None:
        m = self.models
        if m.api_key_env in OWN_SECRETS:
            raise PolicyConfigError(f"{m.api_key_env} is this layer's own secret and cannot be used as a model key")
        if m.api_key_env:
            value = os.environ.get(m.api_key_env, "")
            if not value:
                raise PolicyConfigError(f"the server has no environment variable {m.api_key_env} (named by the customer's policy)")
            return value
        if m.api_key:
            return vault.decrypt(m.api_key)
        return None

    def llm_settings(self, base: LLMSettings) -> LLMSettings | None:
        """LLM settings for the customer's own deployment, or None when the shop's deployment is used (mode 1)."""
        m = self.models
        if not m.uses_customer_model:
            return None
        provider = m.provider
        cfg: dict[str, Any] = dict(base.providers.get(provider, {}))
        if m.tiers:  # the customer's own deployment names; tiers it did not name fall back to its nearest named one
            tiers = dict(m.tiers)
            tiers.setdefault("balanced", tiers.get("fast") or tiers.get("pro") or next(iter(tiers.values())))
            tiers.setdefault("fast", tiers["balanced"])
            tiers.setdefault("pro", tiers.get("complex") or tiers["balanced"])
            tiers.setdefault("complex", tiers["pro"])
        else:  # no names given: the provider's standard model names from settings.yaml
            tiers = dict(cfg.get("tiers", {}))
        if not tiers:
            raise PolicyConfigError(f"the policy names provider '{provider}' but no tiers (fast/balanced/pro/complex -> deployment)")
        cfg["tiers"] = tiers
        key = self.credential()
        if provider in ("anthropic", "anthropic_foundry", "openai", "azure_openai") and not key:
            raise PolicyConfigError(f"provider '{provider}' needs a credential: api_key_env (an environment variable on the server) or api_key")
        if key:
            cfg["api_key"] = key
        for field in ("base_url", "endpoint", "api_version", "region", "resource", "max_tokens", "timeout_s"):
            value = getattr(m, field)
            if value not in (None, ""):
                cfg[field] = value
        if provider == "anthropic_foundry" and not (cfg.get("base_url") or cfg.get("resource")):
            raise PolicyConfigError("anthropic_foundry needs base_url (the Target URI) or resource")
        if provider == "azure_openai" and not cfg.get("endpoint"):
            raise PolicyConfigError("azure_openai needs endpoint")
        return LLMSettings(provider=provider, temperature=base.temperature, max_tokens=base.max_tokens, timeout_s=base.timeout_s,
                           throttle=dict(base.throttle), providers={provider: cfg})

    def settings_for(self, settings: Settings, customer_id: str | None = None, owns_run: Any = None) -> Settings:
        """The run settings under this policy: allowed websites become the security allowlist, and, for a customer's
        run, the settings say who the run is for, which systems that customer connected (as stored: credentials still
        encrypted, read only when a connector is first used) and what its agents may do with each. `owns_run(run_id)`
        answers whether a run is this customer's, for the files a run may read under runs/."""
        security = dict(settings.security or {})
        if self.reach.websites:
            security["allowed_hosts"] = list(self.reach.websites)
        update: dict[str, Any] = {"security": security}
        if customer_id:
            update["tenant"] = {"id": str(customer_id), "connections": {k: dict(v) for k, v in self.connections.items()},
                                "access": self.access(), "owns_run": owns_run}
        return settings.model_copy(update=update)

    def masker(self):
        kinds = list(self.data.mask_before_model)
        if not kinds:
            return None

        def mask(text: str) -> str:
            return mask_text(text, kinds)

        return mask


class PolicyConfigError(ValueError):
    """The policy is valid but the server cannot honour it (a missing credential, an unknown endpoint)."""


# ---- masking -------------------------------------------------------------------------------------
MASKERS: dict[str, tuple[re.Pattern[str], str]] = {
    "email": (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[email]"),
    "phone": (re.compile(r"(?<![\w/])(?:\+?\d[\d\s().-]{7,}\d)(?![\w/])"), "[phone]"),
    "policy_number": (re.compile(r"\b(?:POL|PLC|HI|MM|CLM)[- ]?\d{6,}\b", re.I), "[policy number]"),
    "card_number": (re.compile(r"\b(?:\d[ -]?){13,19}\b"), "[card number]"),
    "iban": (re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){3,7}\b"), "[iban]"),
}


def mask_text(text: str, kinds: list[str]) -> str:
    out = text
    for kind in kinds:
        pattern, token = MASKERS[kind]
        out = pattern.sub(token, out)
    return out


def load_policy(data: dict[str, Any] | None) -> CustomerPolicy:
    return CustomerPolicy.model_validate(data or {})
