"""What a model call costs.

The price table lives in config/settings.yaml under llm.pricing: dollars per million tokens, input and output, per
model family. A model id matches the longest key it contains, so a Foundry deployment called claude-sonnet-5-5-eu or
a Bedrock id like anthropic.claude-sonnet-5-5-v1:0 still matches `claude-sonnet-5-5`, and `claude-sonnet` catches
every Sonnet. Prompt-cache reads and writes are priced as a factor of the input rate. A model with no matching key
has no price: its tokens are still counted and the run is marked as partly unpriced, never silently zero.
"""
from __future__ import annotations

from typing import Any

MILLION = 1_000_000.0


class Pricing:
    def __init__(self, cfg: dict[str, Any] | None = None) -> None:
        cfg = dict(cfg or {})
        self.currency = str(cfg.get("currency") or "USD")
        self.cache_read_factor = float(cfg.get("cache_read_factor", 0.1) or 0)
        self.cache_write_factor = float(cfg.get("cache_write_factor", 1.25) or 0)
        self.models: dict[str, tuple[float, float]] = {}
        for key, rate in (cfg.get("models") or {}).items():
            pair = _pair(rate)
            if pair is not None:
                self.models[str(key).strip().lower()] = pair
        self._keys = sorted(self.models, key=len, reverse=True)

    # ---- lookups ---------------------------------------------------------------------------
    def rate(self, model: str | None) -> tuple[float, float] | None:
        """(input, output) dollars per million tokens for a model id, or None when the table has no match."""
        m = (model or "").strip().lower()
        if not m:
            return None
        if m in self.models:
            return self.models[m]
        for key in self._keys:
            if key in m:
                return self.models[key]
        return None

    def cost(self, model: str | None, usage: dict[str, Any] | None) -> float | None:
        """Dollars for one call from its reported usage, or None when the model is not priced."""
        r = self.rate(model)
        if r is None:
            return None
        usage = usage or {}
        tin = int(usage.get("input_tokens", 0) or 0)
        tout = int(usage.get("output_tokens", 0) or 0)
        details = usage.get("input_token_details") or {}
        cache_read = int(details.get("cache_read", 0) or 0)
        cache_write = int(details.get("cache_creation", 0) or 0)
        plain = max(0, tin - cache_read - cache_write)   # LangChain reports input_tokens inclusive of the cached parts
        rate_in, rate_out = r
        dollars = (plain * rate_in + cache_read * rate_in * self.cache_read_factor
                   + cache_write * rate_in * self.cache_write_factor + tout * rate_out) / MILLION
        return round(dollars, 8)

    def table(self) -> list[dict[str, Any]]:
        return [{"model": k, "input": v[0], "output": v[1]} for k, v in sorted(self.models.items())]

    def describe(self) -> dict[str, Any]:
        return {"currency": self.currency, "cache_read_factor": self.cache_read_factor, "cache_write_factor": self.cache_write_factor,
                "models": self.table()}


def _pair(rate: Any) -> tuple[float, float] | None:
    if isinstance(rate, dict):
        tin = rate.get("input", rate.get("in"))
        tout = rate.get("output", rate.get("out"))
    elif isinstance(rate, (list, tuple)) and len(rate) == 2:
        tin, tout = rate
    else:
        return None
    try:
        return float(tin or 0), float(tout or 0)
    except (TypeError, ValueError):
        return None


def money(dollars: float | None, currency: str = "USD") -> str:
    """A short money string for logs and the CLI (the portal has its own)."""
    if dollars is None:
        return "unpriced"
    sym = "$" if currency == "USD" else currency + " "
    if dollars == 0:
        return sym + "0"
    if dollars < 0.001:
        return "<" + sym + "0.001"
    if dollars < 1:
        return f"{sym}{dollars:.3f}"
    return f"{sym}{dollars:,.2f}"
