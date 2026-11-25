"""Throttling: how fast the agents may call a model, and how fast a caller may call the API.

    Limiter        calls per minute, tokens per minute and calls in flight, shared by every agent of the installation.
                   A call waits for a slot up to `max_wait_s`; past that it is shed, the agent falls back to the
                   profile's patterns and the run records that it did. Nothing fails because the model is busy.
    Budget         what one agent may spend in one run (calls, tokens), from its contract. A spent budget refuses the
                   next call the same way.
    RequestGate    requests per minute per caller on the API (HTTP 429 with Retry-After past it).

Settings (0 switches a limit off): DOCUINSIGHTS_MODEL_RPM (60), DOCUINSIGHTS_MODEL_TPM (0), DOCUINSIGHTS_MODEL_CONCURRENCY (4),
DOCUINSIGHTS_MODEL_MAX_WAIT_S (20), DOCUINSIGHTS_API_RPM (600), DOCUINSIGHTS_MAX_ASSESSMENTS (2).
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque
from typing import Any


class Throttled(RuntimeError):
    """A call was not let through: the limiter shed it, or the agent's budget is spent."""


def _env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


class Limiter:
    def __init__(self, rpm: float = 60, tpm: float = 0, concurrency: int = 4, max_wait_s: float = 20, clock: Any = time.monotonic, sleep: Any = None):
        self.rpm, self.tpm, self.concurrency, self.max_wait_s = rpm, tpm, int(concurrency), max_wait_s
        self._clock = clock
        self._cv = threading.Condition()
        self._sleep = sleep
        self._calls: deque[tuple[float, int]] = deque()   # (time, tokens) of the last minute
        self._in_flight = 0
        self.totals = {"calls": 0, "tokens": 0, "waited_ms": 0, "waits": 0, "shed": 0}

    @classmethod
    def from_env(cls) -> "Limiter":
        return cls(rpm=_env("DOCUINSIGHTS_MODEL_RPM", 60), tpm=_env("DOCUINSIGHTS_MODEL_TPM", 0), concurrency=int(_env("DOCUINSIGHTS_MODEL_CONCURRENCY", 4)),
                   max_wait_s=_env("DOCUINSIGHTS_MODEL_MAX_WAIT_S", 20))

    def _trim(self, t: float) -> None:
        while self._calls and t - self._calls[0][0] >= 60:
            self._calls.popleft()

    def _wait_for(self, t: float) -> float:
        """Seconds until a call could start; 0 when it can start now."""
        self._trim(t)
        waits = [0.0]
        if self.concurrency and self._in_flight >= self.concurrency:
            waits.append(0.05)
        if self.rpm and len(self._calls) >= self.rpm:
            waits.append(60 - (t - self._calls[int(len(self._calls) - self.rpm)][0]))
        if self.tpm and sum(n for _, n in self._calls) >= self.tpm:
            waits.append(60 - (t - self._calls[0][0]))
        return max(waits)

    def acquire(self) -> float:
        """Take a slot, waiting if need be. Returns the seconds waited; raises Throttled when the wait would pass max_wait_s."""
        start = self._clock()
        with self._cv:
            while True:
                t = self._clock()
                need = self._wait_for(t)
                if need <= 0:
                    self._in_flight += 1
                    self._calls.append((t, 0))
                    waited = t - start
                    self.totals["calls"] += 1
                    if waited > 0.001:
                        self.totals["waits"] += 1
                        self.totals["waited_ms"] += round(waited * 1000)
                    return waited
                if (t - start) + need > self.max_wait_s:
                    self.totals["shed"] += 1
                    raise Throttled(f"the model limit is reached ({self.describe()}): the call would wait {round((t - start) + need)}s, more than the {round(self.max_wait_s)}s allowed")
                if self._sleep:
                    self._sleep(min(need, 0.25))
                else:
                    self._cv.wait(timeout=min(need, 0.25))

    def release(self, tokens: int = 0) -> None:
        with self._cv:
            self._in_flight = max(0, self._in_flight - 1)
            if tokens and self._calls:
                t, n = self._calls[-1]
                self._calls[-1] = (t, n + int(tokens))
            self.totals["tokens"] += int(tokens or 0)
            self._cv.notify_all()

    def describe(self) -> str:
        parts = [f"{int(self.rpm)} calls a minute" if self.rpm else "", f"{int(self.tpm):,} tokens a minute" if self.tpm else "", f"{self.concurrency} at a time" if self.concurrency else ""]
        return ", ".join(p for p in parts if p) or "no limit"

    def snapshot(self) -> dict[str, Any]:
        with self._cv:
            self._trim(self._clock())
            return {"limits": {"calls_per_minute": self.rpm, "tokens_per_minute": self.tpm, "concurrent": self.concurrency, "max_wait_s": self.max_wait_s}, "describe": self.describe(),
                    "last_minute": {"calls": len(self._calls), "tokens": sum(n for _, n in self._calls)}, "in_flight": self._in_flight, "totals": dict(self.totals)}


class Budget:
    """What one agent may spend in one run."""

    def __init__(self, max_calls: int = 0, max_tokens: int = 0):
        self.max_calls, self.max_tokens = int(max_calls or 0), int(max_tokens or 0)
        self.calls = self.tokens = self.refused = 0

    def check(self) -> None:
        if self.max_calls and self.calls >= self.max_calls:
            self.refused += 1
            raise Throttled(f"this agent's budget of {self.max_calls} model call(s) for one run is spent")
        if self.max_tokens and self.tokens >= self.max_tokens:
            self.refused += 1
            raise Throttled(f"this agent's budget of {self.max_tokens:,} tokens for one run is spent")

    def spend(self, tokens: int) -> None:
        self.calls += 1
        self.tokens += int(tokens or 0)

    def snapshot(self) -> dict[str, Any]:
        return {"max_calls": self.max_calls, "calls": self.calls, "max_tokens": self.max_tokens, "tokens": self.tokens, "refused": self.refused}


class RequestGate:
    """Requests per minute per caller."""

    def __init__(self, rpm: float = 600, clock: Any = time.monotonic):
        self.rpm, self._clock = rpm, clock
        self._lock = threading.Lock()
        self._seen: dict[str, deque[float]] = {}
        self.totals = {"requests": 0, "refused": 0}

    @classmethod
    def from_env(cls) -> "RequestGate":
        return cls(rpm=_env("DOCUINSIGHTS_API_RPM", 600))

    def allow(self, caller: str) -> float:
        """0 when the request may go ahead, else the seconds to wait before trying again."""
        if not self.rpm:
            return 0.0
        t = self._clock()
        with self._lock:
            q = self._seen.setdefault(caller, deque())
            while q and t - q[0] >= 60:
                q.popleft()
            self.totals["requests"] += 1
            if len(q) >= self.rpm:
                self.totals["refused"] += 1
                return max(1.0, 60 - (t - q[0]))
            q.append(t)
            if len(self._seen) > 5000:   # forget callers that went quiet
                for k in [k for k, v in self._seen.items() if not v or t - v[-1] >= 60]:
                    del self._seen[k]
            return 0.0

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"requests_per_minute": self.rpm, "callers": len(self._seen), "totals": dict(self.totals)}


class KeyAttempts:
    """Wrong API keys per caller address. Past `limit` of them inside `window` seconds the address is refused until the oldest one
    leaves the window, whatever key it sends: a short key cannot be found by trying."""

    def __init__(self, limit: float = 10, window: float = 600, clock: Any = time.monotonic):
        self.limit, self.window, self._clock = limit, window, clock
        self._lock = threading.Lock()
        self._wrong: dict[str, deque[float]] = {}
        self.totals = {"wrong": 0, "refused": 0}

    @classmethod
    def from_env(cls) -> "KeyAttempts":
        return cls(limit=_env("DOCUINSIGHTS_KEY_ATTEMPTS", 10))

    def wait(self, address: str) -> float:
        """0 when this address may try a key, else the seconds until it may."""
        if not self.limit:
            return 0.0
        t = self._clock()
        with self._lock:
            q = self._wrong.get(address)
            while q and t - q[0] >= self.window:
                q.popleft()
            if q is not None and not q:
                del self._wrong[address]
            if q and len(q) >= self.limit:
                self.totals["refused"] += 1
                return max(1.0, self.window - (t - q[0]))
        return 0.0

    def wrong(self, address: str) -> None:
        t = self._clock()
        with self._lock:
            self._wrong.setdefault(address, deque()).append(t)
            self.totals["wrong"] += 1
            if len(self._wrong) > 5000:   # forget addresses whose wrong keys have aged out
                for k in [k for k, v in self._wrong.items() if not v or t - v[-1] >= self.window]:
                    del self._wrong[k]

    def snapshot(self) -> dict[str, Any]:
        return {"wrong_keys_before_a_wait": self.limit, "window_s": self.window, **self.totals}

