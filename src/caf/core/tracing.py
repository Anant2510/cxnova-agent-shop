"""Lightweight run tracing: every orchestrator step, agent stage and model call becomes an event."""

from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Tracer:
    def __init__(self, run_id: str, sink: Path | None = None):
        self.run_id = run_id
        self.sink = sink
        self.events: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._listeners: list[Callable[[dict[str, Any]], None]] = []

    def subscribe(self, fn: Callable[[dict[str, Any]], None]) -> None:
        self._listeners.append(fn)

    def event(self, kind: str, **data: Any) -> dict[str, Any]:
        evt = {"ts": _now(), "run_id": self.run_id, "kind": kind, **data}
        with self._lock:
            self.events.append(evt)
            if self.sink is not None:
                self.sink.parent.mkdir(parents=True, exist_ok=True)
                with self.sink.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(evt, default=str) + "\n")
        for fn in list(self._listeners):
            try:
                fn(evt)
            except Exception:  # listeners must never break a run
                pass
        return evt

    @contextmanager
    def span(self, name: str, **attrs: Any) -> Iterator[dict[str, Any]]:
        t0 = time.perf_counter()
        self.event("span.start", name=name, **attrs)
        info: dict[str, Any] = {}
        try:
            yield info
        except Exception as exc:
            self.event("span.error", name=name, error=repr(exc), duration_ms=round((time.perf_counter() - t0) * 1000, 1), **attrs)
            raise
        else:
            self.event("span.end", name=name, duration_ms=round((time.perf_counter() - t0) * 1000, 1), **attrs, **info)

    def child(self, **_: Any) -> "Tracer":
        return self
