"""The customer behind the current request, for code that has no request object (MCP tools, A2A handlers)."""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

current_customer: ContextVar[dict[str, Any] | None] = ContextVar("caf_shop_customer", default=None)
current_role: ContextVar[str] = ContextVar("caf_shop_role", default="local")   # local | admin | customer


def customer() -> dict[str, Any] | None:
    return current_customer.get()
