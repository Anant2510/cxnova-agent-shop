"""The model DocuInsights asks for judgement: reading a scan, naming a document, lifting fields, writing a narrative.

A model's answer is advisory. Rules, date and value arithmetic, evidence requirements and the recommendation are code.
Without a model (provider `offline`) the same steps run on patterns and keywords declared in the profile, and say so.

    DOCUINSIGHTS_MODEL_PROVIDER   offline (default) | anthropic | anthropic_foundry
    ANTHROPIC_API_KEY             anthropic
    ANTHROPIC_FOUNDRY_API_KEY, ANTHROPIC_FOUNDRY_BASE_URL (or ANTHROPIC_FOUNDRY_RESOURCE)   Claude on Microsoft Foundry

A step asks for a tier, never a model, exactly as on the CX Nova layer, and the tiers resolve the same way there and
here so one set of settings serves both:

    anthropic_foundry   FOUNDRY_DEPLOYMENT_FAST / _BALANCED / _PRO / _COMPLEX   (your deployment names)
                        defaults: claude-sonnet-5-5, claude-sonnet-5-5, claude-opus-5, claude-opus-5
    anthropic           defaults: claude-haiku-5-5, claude-sonnet-5-5, claude-opus-5-5, claude-opus-5-5
    DOCUINSIGHTS_MODEL_FAST / _BALANCED / _PRO / _COMPLEX   override one tier; DOCUINSIGHTS_MODEL overrides all

DocuInsights asks for `fast` to classify and `balanced` to read scans, extract, answer and draft.

A host supplies its own client instead (the CX Nova layer wraps the run's model, so calls are routed, metered and
priced there): anything with `offline`, `name`, `text()` and `json()` will do.
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any

from . import net

PROMPT_VERSION = "2026.11"
TIERS = ("fast", "balanced", "pro", "complex")
DEFAULT_TIERS = {"anthropic": {"fast": "claude-haiku-5-5", "balanced": "claude-sonnet-5-5", "pro": "claude-opus-5-5", "complex": "claude-opus-5-5"},
                 "anthropic_foundry": {"fast": "claude-sonnet-5-5", "balanced": "claude-sonnet-5-5", "pro": "claude-opus-5", "complex": "claude-opus-5"}}
RETRY_STATUS = (408, 409, 429, 500, 502, 503, 504, 529)


class ModelError(RuntimeError):
    pass


def extract_json(text: str) -> Any:
    text = (text or "").strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    m = re.search(r"```(?:json)?\s*(.+?)```", text, flags=re.S)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except ValueError:
            pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        return json.loads(text[start:end + 1])
    raise ModelError("the model did not return JSON")


class BaseModel_:
    """Shared bookkeeping: every call is recorded (step, model, time, tokens) for the agent monitor."""

    name = "offline"
    provider = "offline"
    offline = True

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def text(self, system: str, user: str, *, images: list[tuple[bytes, str]] | None = None, step: str = "", tier: str = "balanced") -> str:
        raise ModelError("no model connected")

    def json(self, system: str, user: str, *, images: list[tuple[bytes, str]] | None = None, step: str = "", tier: str = "balanced") -> Any:
        reply = self.text(system, user + "\n\nReply with only the JSON object.", images=images, step=step, tier=tier)
        try:
            return extract_json(reply)
        except Exception:
            reply = self.text(system, user + "\n\nYour last reply was not valid JSON. Reply again with only the JSON object.", images=images, step=step, tier=tier)
            return extract_json(reply)

    def _record(self, step: str, t0: float, usage: dict[str, Any] | None = None, error: str = "", model: str = "") -> None:
        self.calls.append({"step": step, "model": model or self.name, "ms": round((time.perf_counter() - t0) * 1000), "prompt_version": PROMPT_VERSION,
                           "input_tokens": int((usage or {}).get("input_tokens") or 0), "output_tokens": int((usage or {}).get("output_tokens") or 0),
                           **({"error": error} if error else {})})

    def describe(self) -> dict[str, Any]:
        return {"provider": self.provider, "model": self.name, "offline": self.offline}


class OfflineModel(BaseModel_):
    pass


class AnthropicModel(BaseModel_):
    """Claude over the Messages API: api.anthropic.com, or a Microsoft Foundry resource (same API, key in api-key)."""

    offline = False

    def __init__(self, api_key: str, *, model: str = "", tiers: dict[str, str] | None = None, base_url: str = "https://api.anthropic.com/",
                 provider: str = "anthropic", max_tokens: int = 16000, timeout: float = 300.0, retries: int = 3) -> None:
        super().__init__()
        self.api_key, self.provider = api_key, provider
        self.tiers = {t: (tiers or {}).get(t) or model or DEFAULT_TIERS.get(provider, DEFAULT_TIERS["anthropic"])[t] for t in TIERS}
        self.name = self.tiers["balanced"]
        self.url = re.sub(r"/v1/messages/?$", "/", base_url.rstrip("/") + "/") + "v1/messages"
        self.max_tokens, self.timeout, self.retries = max_tokens, timeout, retries

    def model_for(self, tier: str) -> str:
        return self.tiers.get(tier) or self.tiers["balanced"]

    def text(self, system: str, user: str, *, images: list[tuple[bytes, str]] | None = None, step: str = "", tier: str = "balanced",
             max_tokens: int | None = None, timeout: float | None = None) -> str:
        model = self.model_for(tier)
        content: list[dict[str, Any]] = [{"type": "image", "source": {"type": "base64", "media_type": mime, "data": base64.b64encode(data).decode()}}
                                         for data, mime in images or []]
        content.append({"type": "text", "text": user})
        body = json.dumps({"model": model, "max_tokens": max_tokens or self.max_tokens, "system": system, "messages": [{"role": "user", "content": content}]}).encode()
        headers = {"content-type": "application/json", "anthropic-version": "2023-06-01", "x-api-key": self.api_key, "api-key": self.api_key}
        t0 = time.perf_counter()
        attempt = 0
        while True:
            attempt += 1
            try:
                with net.urlopen(urllib.request.Request(self.url, data=body, method="POST", headers=headers), timeout=timeout or self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
                if exc.code in RETRY_STATUS and attempt <= self.retries:   # busy or over a per-minute quota: wait as asked, then try again
                    try:
                        wait = float(exc.headers.get("retry-after") or 0)
                    except (TypeError, ValueError):
                        wait = 0.0
                    time.sleep(min(30.0, wait or 2.0 * attempt))
                    continue
                self._record(step, t0, error=f"HTTP {exc.code}", model=model)
                raise ModelError(f"{self.provider} answered {exc.code}: {detail}") from exc
            except Exception as exc:
                self._record(step, t0, error=type(exc).__name__, model=model)
                raise ModelError(f"{self.provider} call failed: {type(exc).__name__}: {exc}") from exc
        self._record(step, t0, data.get("usage"), model=model)
        return "".join(b.get("text", "") for b in data.get("content") or [] if b.get("type") == "text").strip()

    def describe(self) -> dict[str, Any]:
        return {"provider": self.provider, "model": self.name, "offline": False, "tiers": dict(self.tiers)}

    def check(self, timeout: float = 30.0) -> dict[str, Any]:
        """One small call per distinct model in use, to say whether it answers: {model: 'online' | the error}."""
        out: dict[str, Any] = {}
        for tier in ("fast", "balanced"):
            model = self.model_for(tier)
            if model in out:
                continue
            try:
                self.text("You answer in one word.", "Reply with the word: online", step="check", tier=tier, max_tokens=64, timeout=timeout)
                out[model] = "online"
            except ModelError as exc:
                out[model] = str(exc)
        return out


def from_env(env: dict[str, str] | None = None) -> BaseModel_:
    env = env if env is not None else dict(os.environ)
    provider = (env.get("DOCUINSIGHTS_MODEL_PROVIDER") or "offline").strip().lower()
    if provider in ("offline", ""):
        return OfflineModel()
    if provider not in DEFAULT_TIERS:
        raise ModelError(f"unknown DOCUINSIGHTS_MODEL_PROVIDER '{provider}' (offline, anthropic, anthropic_foundry)")
    tiers = {}
    for t in TIERS:
        deployment = env.get(f"FOUNDRY_DEPLOYMENT_{t.upper()}") if provider == "anthropic_foundry" else None
        tiers[t] = env.get(f"DOCUINSIGHTS_MODEL_{t.upper()}") or env.get("DOCUINSIGHTS_MODEL") or deployment or DEFAULT_TIERS[provider][t]
    kw: dict[str, Any] = {"tiers": tiers, "provider": provider, "max_tokens": int(env.get("DOCUINSIGHTS_MAX_TOKENS") or 16000),
                          "timeout": float(env.get("DOCUINSIGHTS_MODEL_TIMEOUT_S") or 300)}
    if provider == "anthropic":
        if not env.get("ANTHROPIC_API_KEY"):
            raise ModelError("DOCUINSIGHTS_MODEL_PROVIDER=anthropic needs ANTHROPIC_API_KEY")
        return AnthropicModel(env["ANTHROPIC_API_KEY"], base_url=env.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com/", **kw)
    key = env.get("ANTHROPIC_FOUNDRY_API_KEY") or ""
    base = env.get("ANTHROPIC_FOUNDRY_BASE_URL") or (f"https://{env['ANTHROPIC_FOUNDRY_RESOURCE']}.services.ai.azure.com/anthropic/" if env.get("ANTHROPIC_FOUNDRY_RESOURCE") else "")
    if not key or not base:
        raise ModelError("anthropic_foundry needs ANTHROPIC_FOUNDRY_API_KEY and ANTHROPIC_FOUNDRY_BASE_URL (or ANTHROPIC_FOUNDRY_RESOURCE)")
    return AnthropicModel(key, base_url=base, **kw)
