"""The provenance ledger: an append-only record of who did what to which case, each entry chained to the one before.

Every agent run, every human review and decision, every change to the knowledge base adds one line. A line holds the
hash of the line before it, so a line that is edited, removed or inserted later breaks the chain from that point on,
and `verify()` says where. It is evidence of what the system did; it is not a blockchain and makes no such claim.
"""
from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any

from .casefile import now

GENESIS = "0" * 64
_locks: dict[str, threading.Lock] = {}
_guard = threading.Lock()


def digest(value: Any) -> str:
    """A short stable fingerprint of any JSON value: what an agent read or wrote, without keeping the value itself."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def _seal(entry: dict[str, Any]) -> str:
    body = {k: v for k, v in entry.items() if k != "hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


class Ledger:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        with _guard:
            self._lock = _locks.setdefault(str(self.path.resolve()), threading.Lock())

    def _lines(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    out.append({"n": len(out) + 1, "corrupt": True, "hash": ""})
        return out

    def _last(self) -> dict[str, Any] | None:
        """The last entry, read from the end of the file (a ledger of a hundred thousand entries is not read whole to add one)."""
        try:
            size = self.path.stat().st_size
        except OSError:
            return None
        if not size:
            return None
        with self.path.open("rb") as fh:
            step = 8192
            while True:
                fh.seek(max(0, size - step))
                tail = fh.read().rstrip(b"\n")
                if b"\n" in tail or step >= size:
                    break
                step *= 4
        try:
            last = json.loads(tail.split(b"\n")[-1].decode("utf-8"))
            return last if isinstance(last, dict) and isinstance(last.get("n"), int) and last.get("hash") else None
        except (ValueError, UnicodeDecodeError):
            return None

    def append(self, kind: str, actor: str, summary: str, *, case: str = "", data: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            last = self._last()
            if last is None:   # an empty ledger, or a last line that cannot be read: count the lines, so the entry lands where verify() expects it
                lines = self._lines()
                last = {"n": len(lines), "hash": lines[-1]["hash"] if lines else GENESIS}
            entry = {"n": last["n"] + 1, "at": now(), "kind": kind, "actor": actor, "case": case, "summary": summary[:300], "data": data or {}, "prev": last["hash"] or GENESIS}
            entry["hash"] = _seal(entry)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
            return entry

    def entries(self, case: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        rows = [e for e in self._lines() if not case or e.get("case") == case]
        return rows[-limit:][::-1]

    def verify(self) -> dict[str, Any]:
        """Recompute every hash and every link. `broken_at` is the first entry that does not hold."""
        lines = self._lines()
        prev = GENESIS
        for i, e in enumerate(lines, start=1):
            if e.get("corrupt") or e.get("n") != i or e.get("prev") != prev or e.get("hash") != _seal(e):
                return {"ok": False, "entries": len(lines), "broken_at": i, "head": prev[:16], "checked_at": now()}
            prev = e["hash"]
        return {"ok": True, "entries": len(lines), "broken_at": None, "head": prev[:16] if lines else "", "checked_at": now()}
