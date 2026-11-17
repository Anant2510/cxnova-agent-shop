"""A customer's policy: which models its agents may call, what reaches them, what the agents may touch, and limits.

The policy is a document the customer's admin owns (YAML or JSON, see config/policies/example-customer.yaml). The
shop turns it into the runtime pieces the framework already has: an LLM facade for the customer's own model
deployment (run modes 2 and 3), the security policy for the run (allowed websites), a masker applied to every
prompt, and limits checked before a run starts. Calls outside the policy are refused, never quietly redirected.
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
    systems: dict[str, str] = Field(default_factory=dict, description="connector kind -> none | read | read_write (informative in v1)")


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

    # ---- storage ------------------------------------------------------------------------------
    def for_storage(self) -> dict[str, Any]:
        """The policy as stored: an inline credential encrypted, never in clear."""
        data = self.model_dump(mode="json")
        key = data["models"].get("api_key")
        if key and not vault.is_encrypted(key):
            data["models"]["api_key"] = vault.encrypt(key)
        return data

    def redacted(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        if data["models"].get("api_key"):
            data["models"]["api_key"] = "***"
        return data

    # ---- runtime ------------------------------------------------------------------------------
    def credential(self) -> str | None:
        m = self.models
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

    def settings_for(self, settings: Settings) -> Settings:
        """The run settings under this policy: allowed websites become the security allowlist."""
        security = dict(settings.security or {})
        if self.reach.websites:
            security["allowed_hosts"] = list(self.reach.websites)
        return settings.model_copy(update={"security": security})

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
