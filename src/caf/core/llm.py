"""Model-agnostic LLM adapter.

Agents ask for a *tier* (fast / balanced / pro / complex), never a vendor model. The tier is
mapped to a concrete model per provider in config/settings.yaml, so a client can run the whole
framework on Azure OpenAI, Anthropic, AWS Bedrock, OpenAI, a local Ollama model, or fully
offline - without changing a single agent.
"""

from __future__ import annotations

import base64
import json
import re
import threading
import time
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from ..config import LLMSettings
from .offline import OfflineModel
from .throttle import ThrottleTimeout, get_throttle, limits_from, retry_after_of

T = TypeVar("T", bound=BaseModel)

SUPPORTED_PROVIDERS = ("anthropic", "anthropic_foundry", "openai", "azure_openai", "bedrock", "ollama", "offline")
CLAUDE_PROVIDERS = ("anthropic", "anthropic_foundry")


class LLMError(RuntimeError):
    pass


class LLMOutputError(LLMError):
    """The model answered, but not in the required shape (after the in-call repair attempt)."""


def _extract_json(text: str) -> Any:
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, flags=re.S)
    if fence:
        try:
            return json.loads(fence.group(1))
        except Exception:
            pass
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
    end = max(text.rfind("}"), text.rfind("]"))
    if start >= 0 and end > start:
        return json.loads(text[start : end + 1])
    raise LLMError("No JSON found in model reply")


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts)
    return str(content)


class LLM:
    """Tiered, provider-neutral chat model facade with usage accounting."""

    def __init__(self, settings: LLMSettings, tracer: Any = None, provider: str | None = None):
        self.settings = settings
        self.provider = (provider or settings.resolved_provider()).lower()
        if self.provider not in SUPPORTED_PROVIDERS:
            raise LLMError(f"Unknown LLM provider '{self.provider}'. Use one of {SUPPORTED_PROVIDERS}")
        self.tracer = tracer
        self._models: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._offline = OfflineModel() if self.provider == "offline" else None
        self._structured_fallback = False  # set when the endpoint rejects native structured outputs
        self._no_effort = False            # set when the endpoint rejects a reasoning-effort setting
        self.preprocess: Any = None        # optional str -> str applied to every prompt and system text (e.g. masking)
        self.images_allowed = True         # False: image inputs are dropped before the call (customer policy)
        self._prompt_schemas: set[str] = set()  # schemas the endpoint's native structured output cannot take (asked for in the prompt)
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0

    # ---- introspection ----------------------------------------------------------------------
    @property
    def offline(self) -> bool:
        return self._offline is not None

    def model_for(self, tier: str) -> str:
        if self.offline:
            return "offline"
        return self.settings.model_for(_norm_tier(tier), self.provider)

    def describe(self) -> dict[str, Any]:
        tiers = ["fast", "balanced", "pro", "complex"]
        info: dict[str, Any] = {"provider": self.provider, "offline": self.offline, "models": {t: self.model_for(t) for t in tiers}}
        if not self.offline:
            info["throttle"] = self.throttle_limits()
        return info

    # ---- throttling -------------------------------------------------------------------------
    def throttle_limits(self) -> dict[str, float]:
        cfg = self.settings.provider_config(self.provider)
        return limits_from(self.settings.throttle, cfg.get("throttle"))

    def _throttle(self, tier: str) -> Any:
        return get_throttle(self.provider, self.model_for(tier), self.throttle_limits())

    def _rate_retries(self) -> int:
        cfg = self.settings.provider_config(self.provider)
        local = cfg.get("throttle") if isinstance(cfg.get("throttle"), dict) else {}
        return int(_throttle_num(local.get("max_retries"), self.settings.throttle.get("max_retries", 6)))

    def _invoke(self, runnable: Any, messages: list[Any], tier: str, est_tokens: float) -> Any:
        """Run one model call inside the shared throttle. A rate-limit or overloaded refusal pauses the deployment for
        every agent (Retry-After or cooldown) and the call is retried, up to throttle.max_retries times."""
        throttle = self._throttle(tier)
        attempts = self._rate_retries() + 1
        for attempt in range(attempts):
            try:
                with throttle.slot(est_tokens) as slot:
                    res = runnable.invoke(messages)
                    raw = res.get("raw") if isinstance(res, dict) else res
                    usage = getattr(raw, "usage_metadata", None) or {}
                    if usage:
                        slot.used(int(usage.get("input_tokens", 0) or 0) + int(usage.get("output_tokens", 0) or 0))
                    if slot.waited_ms > 50 and self.tracer is not None:
                        self.tracer.event("llm.throttled", model=self.model_for(tier), waited_ms=round(slot.waited_ms))
                    return res
            except ThrottleTimeout:
                raise
            except Exception as exc:
                limited, after = retry_after_of(exc)
                if not limited or attempt == attempts - 1:
                    raise
                throttle.rate_limited(after)
                if self.tracer is not None:
                    self.tracer.event("llm.rate_limited", model=self.model_for(tier), retry=attempt + 1, retry_after=after)
        raise LLMError("unreachable")  # pragma: no cover

    @staticmethod
    def _estimate_tokens(messages: list[Any]) -> float:
        chars = sum(len(_content_text(getattr(m, "content", ""))) for m in messages)
        return chars / 3.5 + 1000  # prompt estimate plus a typical answer; corrected by reported usage

    def usage(self) -> dict[str, int]:
        return {"llm_calls": self.calls, "input_tokens": self.input_tokens, "output_tokens": self.output_tokens}

    # ---- provider factory -------------------------------------------------------------------
    def chat_model(self, tier: str, effort: str | None = None) -> Any:
        tier = _norm_tier(tier)
        effort = effort if (effort and self.provider in CLAUDE_PROVIDERS and not self._no_effort) else None
        key = f"{tier}|{effort or ''}"
        with self._lock:
            if key in self._models:
                return self._models[key]
            model_name = self.model_for(tier)
            cfg = self.settings.provider_config(self.provider)
            # Temperature is sent only when configured: current reasoning models (Claude Sonnet 5.5 / Fable 5, GPT-5)
            # accept only their default and reject any other value.
            temperature = cfg.get("temperature", self.settings.temperature)
            common = {"temperature": float(temperature)} if temperature not in (None, "") else {}
            max_tokens = int(cfg.get("max_tokens") or self.settings.max_tokens)
            p = self.provider
            # The SDK retries transient failures twice; rate limits are retried by _invoke (shared pause), so a call that
            # times out is not repeated for many minutes.
            retries = int(_throttle_num(cfg.get("sdk_retries"), 2))
            timeout = float(cfg.get("timeout_s") or self.settings.timeout_s)
            if effort:
                common["reasoning_effort"] = effort
            if cfg.get("api_key") and p in ("anthropic", "openai", "azure_openai"):
                common["api_key"] = cfg["api_key"]  # explicit credential (a customer's policy); else the SDK's environment variable
            if p == "anthropic":
                from langchain_anthropic import ChatAnthropic
                kw = {"base_url": cfg["base_url"]} if cfg.get("base_url") else {}
                m = ChatAnthropic(model=model_name, max_tokens=max_tokens, timeout=timeout, max_retries=retries, **kw, **common)
            elif p == "anthropic_foundry":
                # Claude on Microsoft Foundry: the Anthropic Messages API at https://<resource>.services.ai.azure.com/anthropic/,
                # authenticated with the Foundry key in x-api-key (and api-key), exactly like the SDK's AnthropicFoundry client.
                from langchain_anthropic import ChatAnthropic
                key = cfg.get("api_key") or ""
                base = (cfg.get("base_url") or "").strip()
                if not base and cfg.get("resource"):
                    base = f"https://{cfg['resource']}.services.ai.azure.com/anthropic/"
                if not key or not base:
                    raise LLMError("anthropic_foundry needs ANTHROPIC_FOUNDRY_API_KEY and ANTHROPIC_FOUNDRY_BASE_URL "
                                   "(or ANTHROPIC_FOUNDRY_RESOURCE)")
                base = re.sub(r"/v1/messages/?$", "/", base.rstrip("/") + "/")  # accept the Target URI from the portal too
                # Both Foundry header names are set explicitly: anthropic SDK 2.9+ sends API keys as "Authorization: Bearer",
                # which Foundry's key auth does not read, so x-api-key / api-key carry the key whatever the SDK does.
                m = ChatAnthropic(model=model_name, max_tokens=max_tokens, timeout=timeout, max_retries=retries,
                                  base_url=base, api_key=key, default_headers={"api-key": key, "x-api-key": key}, **common)
            elif p == "openai":
                from langchain_openai import ChatOpenAI
                kw = {"base_url": cfg["base_url"]} if cfg.get("base_url") else {}
                m = ChatOpenAI(model=model_name, timeout=timeout, max_retries=retries, **kw, **common)
            elif p == "azure_openai":
                from langchain_openai import AzureChatOpenAI
                m = AzureChatOpenAI(
                    azure_deployment=model_name,
                    azure_endpoint=cfg.get("endpoint") or None,
                    api_version=cfg.get("api_version") or "2025-04-01-preview",
                    timeout=timeout,
                    max_retries=retries,
                    **common,
                )
            elif p == "bedrock":
                from langchain_aws import ChatBedrockConverse
                m = ChatBedrockConverse(model=model_name, region_name=cfg.get("region") or "us-east-1",
                                        max_tokens=max_tokens, **common)
            elif p == "ollama":
                from langchain_ollama import ChatOllama
                m = ChatOllama(model=model_name, base_url=cfg.get("base_url") or "http://localhost:11434", **common)
            else:  # pragma: no cover - offline never builds a chat model
                raise LLMError("offline provider has no chat model")
            self._models[key] = m
            return m

    def _drop_effort(self, exc: BaseException) -> bool:
        """True (and effort disabled from now on) when the endpoint rejected the reasoning-effort setting."""
        text = str(exc).lower()
        if ("400" in text or getattr(exc, "status_code", None) == 400) and "effort" in text:
            self._no_effort = True
            if self.tracer is not None:
                self.tracer.event("llm.effort_unsupported", provider=self.provider, reason=str(exc)[:200])
            return True
        return False

    def _messages(self, prompt: str, system: str | None, images: list[tuple[bytes, str]] | None) -> list[Any]:
        from langchain_core.messages import HumanMessage, SystemMessage
        if self.preprocess is not None:
            prompt = self.preprocess(prompt)
            system = self.preprocess(system) if system else system
        if images and not self.images_allowed:
            images = None
        msgs: list[Any] = []
        if system:
            msgs.append(SystemMessage(content=system))
        if images:
            blocks: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
            for data, mime in images:
                b64 = base64.b64encode(data).decode()
                blocks.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
            msgs.append(HumanMessage(content=blocks))
        else:
            msgs.append(HumanMessage(content=prompt))
        return msgs

    def _account(self, msg: Any, tier: str, t0: float, kind: str, meter: Any = None) -> None:
        usage = getattr(msg, "usage_metadata", None) or {}
        if meter is not None:
            meter.add(usage)
        with self._lock:
            self.calls += 1
            self.input_tokens += int(usage.get("input_tokens", 0) or 0)
            self.output_tokens += int(usage.get("output_tokens", 0) or 0)
        if self.tracer is not None:
            self.tracer.event("llm.call", call=kind, tier=tier, model=self.model_for(tier), provider=self.provider,
                              duration_ms=round((time.perf_counter() - t0) * 1000, 1),
                              input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"))

    # ---- public API -------------------------------------------------------------------------
    def complete(self, prompt: str, *, system: str | None = None, tier: str = "balanced",
                 images: list[tuple[bytes, str]] | None = None, meter: Any = None, effort: str | None = None) -> str:
        t0 = time.perf_counter()
        if self._offline is not None:
            text = self._offline.complete(prompt, system)
            self._account(None, tier, t0, "complete", meter)
            return text
        try:
            messages = self._messages(prompt, system, images)
            try:
                msg = self._invoke(self.chat_model(tier, effort), messages, tier, self._estimate_tokens(messages))
            except Exception as exc:
                if not (effort and self._drop_effort(exc)):
                    raise
                msg = self._invoke(self.chat_model(tier), messages, tier, self._estimate_tokens(messages))
        except Exception as exc:
            if meter is not None:
                meter.fail(f"{type(exc).__name__}: {exc}")
            raise LLMError(f"{self.provider} call failed: {exc}") from exc
        self._account(msg, tier, t0, "complete", meter)
        return _content_text(msg.content).strip()

    def structured_method(self) -> str | None:
        """How structured output is requested. Claude uses native structured outputs (json_schema): Claude 5.x
        models do not accept forced tool choice. Override per provider with `structured_method` in settings."""
        cfg = self.settings.provider_config(self.provider)
        if self._structured_fallback:
            return "prompt_json"
        return cfg.get("structured_method") or {"anthropic": "json_schema", "anthropic_foundry": "json_schema"}.get(self.provider)

    def _prompt_only(self, schema: type[BaseModel]) -> bool:
        """True when this schema is asked for in the prompt instead of the endpoint's native structured output: it
        was refused before ("Schema is too complex"), or its arrays nest deeper than `structured_max_array_depth`
        (default 2), which is where Claude's structured-output grammar gives up after a long wait."""
        key = f"{schema.__module__}.{schema.__qualname__}"
        if key in self._prompt_schemas:
            return True
        cfg = self.settings.provider_config(self.provider)
        limit = int(_throttle_num(cfg.get("structured_max_array_depth"), 2))
        if limit > 0 and _array_depth(schema) > limit:
            self._prompt_schemas.add(key)
            if self.tracer is not None:
                self.tracer.event("llm.structured_by_prompt", provider=self.provider, schema=schema.__name__,
                                  reason=f"arrays nest {_array_depth(schema)} deep (limit {limit})")
            return True
        return False

    def structured(self, prompt: str, schema: type[T], *, system: str | None = None, tier: str = "balanced",
                   images: list[tuple[bytes, str]] | None = None, retries: int = 1, meter: Any = None,
                   effort: str | None = None) -> T:
        t0 = time.perf_counter()
        if self._offline is not None:
            obj = self._offline.structured(prompt, schema, system)
            self._account(None, tier, t0, "structured", meter)
            return obj  # type: ignore[return-value]
        from langchain_core.exceptions import OutputParserException

        model = self.chat_model(tier, effort)
        messages = self._messages(prompt, system, images)
        method = self.structured_method()
        if method == "prompt_json" or (method == "json_schema" and self._prompt_only(schema)):
            return self._structured_by_prompt(prompt, schema, system=system, tier=tier, images=images, retries=retries, meter=meter,
                                              effort=effort)
        last_err: Exception | None = None
        for attempt in range(retries + 1):
            try:
                runnable = model.with_structured_output(schema, include_raw=True, **({"method": method} if method else {}))
                res = self._invoke(runnable, messages, tier, self._estimate_tokens(messages))
                raw = res.get("raw") if isinstance(res, dict) else None
                self._account(raw, tier, t0, "structured", meter)
                parsed = res.get("parsed") if isinstance(res, dict) else res
                if isinstance(parsed, schema):
                    return parsed
                if isinstance(parsed, dict):
                    return schema.model_validate(parsed)
                if raw is not None:
                    return schema.model_validate(_extract_json(_content_text(raw.content)))
                raise LLMError(f"structured output missing: {res.get('parsing_error') if isinstance(res, dict) else res}")
            except (ValidationError, LLMError, json.JSONDecodeError, OutputParserException) as exc:
                last_err = exc
                from langchain_core.messages import HumanMessage
                messages = messages + [HumanMessage(content=f"Your previous reply did not validate: {exc}. Reply again, strictly matching the schema.")]
            except Exception as exc:
                if effort and self._drop_effort(exc):  # checked first: the message also names output_config
                    return self.structured(prompt, schema, system=system, tier=tier, images=images, retries=retries, meter=meter)
                if method == "json_schema" and _schema_too_complex(exc):
                    # this schema is beyond the endpoint's structured-output grammar: ask for it in the prompt from now on
                    self._prompt_schemas.add(f"{schema.__module__}.{schema.__qualname__}")
                    if self.tracer is not None:
                        self.tracer.event("llm.structured_by_prompt", provider=self.provider, schema=schema.__name__, reason=str(exc)[:200])
                    return self._structured_by_prompt(prompt, schema, system=system, tier=tier, images=images, retries=retries,
                                                      meter=meter, effort=effort)
                if method == "json_schema" and _rejects_structured_outputs(exc):
                    # this endpoint does not take output_config.format: ask for JSON in the prompt from now on
                    self._structured_fallback = True
                    if self.tracer is not None:
                        self.tracer.event("llm.structured_fallback", provider=self.provider, reason=str(exc)[:200])
                    return self._structured_by_prompt(prompt, schema, system=system, tier=tier, images=images, retries=retries,
                                                      meter=meter, effort=effort)
                if meter is not None:
                    meter.fail(f"{type(exc).__name__}: {exc}")
                raise LLMError(f"{self.provider} structured call failed: {exc}") from exc
        raise LLMOutputError(f"Model output did not match {schema.__name__}: {last_err}")

    def _structured_by_prompt(self, prompt: str, schema: type[T], *, system: str | None, tier: str,
                              images: list[tuple[bytes, str]] | None, retries: int, meter: Any, effort: str | None = None) -> T:
        """Structured output without the endpoint's help: the schema goes in the prompt, the reply is validated."""
        spec = json.dumps(schema.model_json_schema(), separators=(",", ":"))
        ask = f"{prompt}\n\nReply with only one JSON object that validates against this JSON Schema - no prose, no Markdown fences.\nJSON Schema: {spec}"
        last: Exception | None = None
        for _ in range(retries + 1):
            text = self.complete(ask, system=system, tier=tier, images=images, meter=meter, effort=effort)
            try:
                return schema.model_validate(_extract_json(text))
            except (ValidationError, LLMError, json.JSONDecodeError) as exc:
                last = exc
                ask = f"{ask}\n\nYour previous reply did not validate ({str(exc)[:300]}). Reply again with only the JSON object."
        raise LLMOutputError(f"Model output did not match {schema.__name__}: {last}")

    def ping(self, tier: str = "fast") -> dict[str, Any]:
        """The smallest structured call: proves the key, access to the tier's model and structured output in one go."""
        from pydantic import BaseModel

        class Ping(BaseModel):
            ok: bool

        t0 = time.perf_counter()
        res = self.structured("Set ok to true.", Ping, tier=tier, retries=0, effort="low")
        if not res.ok:
            raise LLMError("the model answered, but not with ok=true")
        return {"provider": self.provider, "model": self.model_for(tier), "ms": round((time.perf_counter() - t0) * 1000)}

    def json(self, prompt: str, *, system: str | None = None, tier: str = "balanced") -> Any:
        if self._offline is not None:
            return {}
        return _extract_json(self.complete(prompt + "\n\nReply with only JSON.", system=system, tier=tier))


def _throttle_num(value: Any, default: Any) -> float:
    try:
        return float(value) if value not in (None, "") else float(default)
    except (TypeError, ValueError):
        return float(default)


def _schema_too_complex(exc: BaseException) -> bool:
    """A 400 saying the structured-output schema itself is the problem (Claude: 'Schema is too complex.')."""
    status = getattr(exc, "status_code", None)
    text = str(exc).lower()
    return (status == 400 or "400" in text) and "schema" in text and any(k in text for k in ("too complex", "too large", "too deep", "exceeds"))


_ARRAY_DEPTHS: dict[type, int] = {}


def _array_depth(schema: type[BaseModel]) -> int:
    """How deep arrays nest in a schema (arrays of objects holding arrays ...), following $refs once."""
    if schema in _ARRAY_DEPTHS:
        return _ARRAY_DEPTHS[schema]
    try:
        js = schema.model_json_schema()
    except Exception:  # pragma: no cover - a schema pydantic cannot export is not ours
        return 0
    defs = js.get("$defs", {})

    def walk(node: Any, d: int, seen: tuple[str, ...]) -> int:
        if not isinstance(node, dict):
            return d
        if "$ref" in node:
            name = str(node["$ref"]).split("/")[-1]
            return d if name in seen else walk(defs.get(name, {}), d, seen + (name,))
        best = d
        if node.get("type") == "array":
            best = max(best, walk(node.get("items", {}), d + 1, seen))
        for v in (node.get("properties") or {}).values():
            best = max(best, walk(v, d, seen))
        for k in ("anyOf", "oneOf", "allOf"):
            for v in node.get(k) or []:
                best = max(best, walk(v, d, seen))
        extra = node.get("additionalProperties")
        if isinstance(extra, dict):
            best = max(best, walk(extra, d, seen))
        return best

    _ARRAY_DEPTHS[schema] = walk(js, 0, ())
    return _ARRAY_DEPTHS[schema]


def _rejects_structured_outputs(exc: BaseException) -> bool:
    """A 400 that names the structured-output parameter: the endpoint does not support output_config.format."""
    status = getattr(exc, "status_code", None)
    text = str(exc).lower()
    return (status == 400 or "400" in text) and any(k in text for k in ("output_config", "output_format", "json_schema", "structured output"))


def _norm_tier(tier: Any) -> str:
    t = getattr(tier, "value", tier) or "balanced"
    t = str(t).lower()
    if t == "none":
        return "fast"
    return t if t in ("fast", "balanced", "pro", "complex") else "balanced"
