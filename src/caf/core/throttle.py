"""Throttling for model calls: every agent in the process shares the same limits per model deployment.

Three limits, each optional (0 = no limit), set under `llm.throttle` in config/settings.yaml or per provider
(`llm.providers.<provider>.throttle`), and overridable with CAF_LLM_MAX_CONCURRENT / CAF_LLM_RPM / CAF_LLM_TPM:

* max_concurrent       - model calls in flight at once (across all agents and parallel orchestrator steps)
* requests_per_minute  - calls started in any rolling 60 s window
* tokens_per_minute    - input + output tokens in any rolling 60 s window (input is estimated before the call,
                         then corrected with the usage the provider reports)

A 429 / overloaded answer that survives the SDK's own retries pauses the whole deployment for its Retry-After
(or `cooldown_s`), so the other agents back off too instead of hammering the same limit.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from contextlib import contextmanager
from typing import Any, Iterator

_REGISTRY: dict[tuple[str, str], "Throttle"] = {}
_REGISTRY_LOCK = threading.Lock()


def _num(value: Any, default: float = 0) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def limits_from(settings_throttle: dict[str, Any] | None, provider_throttle: dict[str, Any] | None) -> dict[str, float]:
    """Merge global and per-provider limits, then environment overrides."""
    merged = {**(settings_throttle or {}), **(provider_throttle or {})}
    env = {"max_concurrent": "CAF_LLM_MAX_CONCURRENT", "requests_per_minute": "CAF_LLM_RPM", "tokens_per_minute": "CAF_LLM_TPM"}
    out = {
        "max_concurrent": _num(merged.get("max_concurrent"), 4),
        "requests_per_minute": _num(merged.get("requests_per_minute"), 0),
        "tokens_per_minute": _num(merged.get("tokens_per_minute"), 0),
        "cooldown_s": _num(merged.get("cooldown_s"), 20),
        "max_wait_s": _num(merged.get("max_wait_s"), 600),
    }
    for key, var in env.items():
        if os.environ.get(var):
            out[key] = _num(os.environ[var], out[key])
    return out


def get_throttle(provider: str, model: str, limits: dict[str, float]) -> "Throttle":
    """One shared throttle per (provider, model deployment) in this process."""
    key = (provider, model)
    with _REGISTRY_LOCK:
        t = _REGISTRY.get(key)
        if t is None or t.limits != limits:
            t = _REGISTRY[key] = Throttle(limits, name=f"{provider}:{model}")
        return t


def reset_throttles() -> None:
    with _REGISTRY_LOCK:
        _REGISTRY.clear()


class ThrottleTimeout(RuntimeError):
    pass


class Throttle:
    def __init__(self, limits: dict[str, float], name: str = ""):
        self.limits = dict(limits)
        self.name = name
        self._cond = threading.Condition()
        self._in_flight = 0
        self._starts: deque[float] = deque()                 # call start times (rolling minute)
        self._tokens: deque[list[float]] = deque()           # [timestamp, tokens] (rolling minute)
        self._paused_until = 0.0
        self.stats = {"calls": 0, "waited_ms": 0.0, "max_wait_ms": 0.0, "rate_limited": 0}

    # -------------------------------------------------------------------------------- internals
    def _trim(self, now: float) -> None:
        while self._starts and now - self._starts[0] >= 60:
            self._starts.popleft()
        while self._tokens and now - self._tokens[0][0] >= 60:
            self._tokens.popleft()

    def _tokens_used(self) -> float:
        return sum(t for _, t in self._tokens)

    def _delay(self, now: float, est_tokens: float) -> float:
        """Seconds to wait before a call may start (0 = go now)."""
        lim = self.limits
        waits = [max(0.0, self._paused_until - now)]
        if lim["max_concurrent"] and self._in_flight >= lim["max_concurrent"]:
            waits.append(0.25)  # woken by notify when a call finishes
        if lim["requests_per_minute"] and len(self._starts) >= lim["requests_per_minute"]:
            waits.append(60 - (now - self._starts[0]) + 0.01)
        tpm = lim["tokens_per_minute"]
        if tpm and self._tokens and self._tokens_used() + est_tokens > tpm:
            waits.append(60 - (now - self._tokens[0][0]) + 0.01)
        return max(waits)

    # -------------------------------------------------------------------------------- API
    @contextmanager
    def slot(self, est_tokens: float = 0) -> Iterator["_Slot"]:
        """Wait for room under every limit, then hold one call slot until the block ends."""
        t0 = time.monotonic()
        with self._cond:
            while True:
                now = time.monotonic()
                self._trim(now)
                delay = self._delay(now, est_tokens)
                if delay <= 0:
                    break
                if now - t0 + delay > self.limits["max_wait_s"]:
                    raise ThrottleTimeout(f"model throttle for {self.name}: no capacity within {self.limits['max_wait_s']:.0f}s")
                self._cond.wait(timeout=min(delay, 5.0))
            now = time.monotonic()
            self._in_flight += 1
            self._starts.append(now)
            entry = [now, float(est_tokens)]
            if self.limits["tokens_per_minute"]:
                self._tokens.append(entry)
            waited = (now - t0) * 1000
            self.stats["calls"] += 1
            self.stats["waited_ms"] += waited
            self.stats["max_wait_ms"] = max(self.stats["max_wait_ms"], waited)
        slot = _Slot(self, entry, waited)
        try:
            yield slot
        finally:
            with self._cond:
                self._in_flight -= 1
                self._cond.notify_all()

    def rate_limited(self, retry_after: float | None = None) -> None:
        """A call was refused for rate or capacity: pause new calls to this deployment for a while."""
        with self._cond:
            self.stats["rate_limited"] += 1
            pause = retry_after if retry_after is not None and retry_after >= 0 else self.limits["cooldown_s"]
            self._paused_until = max(self._paused_until, time.monotonic() + pause)
            self._cond.notify_all()

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, **{k: v for k, v in self.limits.items()}, "stats": dict(self.stats), "in_flight": self._in_flight}


class _Slot:
    def __init__(self, throttle: Throttle, entry: list[float], waited_ms: float):
        self.throttle = throttle
        self.entry = entry
        self.waited_ms = waited_ms

    def used(self, tokens: float | None) -> None:
        """Replace the estimate with the tokens the provider reported."""
        if tokens is not None and self.throttle.limits["tokens_per_minute"]:
            with self.throttle._cond:
                self.entry[1] = float(tokens)
                self.throttle._cond.notify_all()


def retry_after_of(exc: BaseException) -> tuple[bool, float | None]:
    """(is a rate/capacity refusal, Retry-After seconds) for an SDK / HTTP exception."""
    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    text = str(exc).lower()
    limited = status in (429, 529) or "rate_limit" in text or "overloaded" in text or "too many requests" in text
    after = None
    headers = getattr(getattr(exc, "response", None), "headers", None) or {}
    try:
        value = headers.get("retry-after") or headers.get("Retry-After")
        after = float(value) if value else None
    except (TypeError, ValueError):
        after = None
    return limited, after
