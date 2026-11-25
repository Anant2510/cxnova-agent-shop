"""Where cases are kept: one folder per case under a data directory, per tenant.

    <data>/<tenant>/cases/<reference>/case.json         the case file
    <data>/<tenant>/cases/<reference>/files/            every document version as uploaded, never overwritten
    <data>/<tenant>/feedback/<profile>.json             the improvement proposals and the approved vocabulary overlay

A tenant keeps one user of a shared installation apart from another (the CX Nova layer passes the customer; a
standalone installation uses 'default'). Swap this class to keep cases in a database or a content platform.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


class StoreError(ValueError):
    pass


def safe(value: str, what: str) -> str:
    if not SAFE.match(value or "") or ".." in value:
        raise StoreError(f"{what} '{value}': letters, digits, '.', '_' and '-' only, at most 80 characters")
    return value


class CaseStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.lock = threading.RLock()

    def _case_dir(self, tenant: str, reference: str) -> Path:
        return self.root / safe(tenant, "tenant") / "cases" / safe(reference, "case reference")

    def exists(self, tenant: str, reference: str) -> bool:
        return (self._case_dir(tenant, reference) / "case.json").exists()

    def load(self, tenant: str, reference: str) -> dict[str, Any]:
        path = self._case_dir(tenant, reference) / "case.json"
        if not path.exists():
            raise KeyError(f"no case '{reference}'")
        with self.lock:
            return json.loads(path.read_text(encoding="utf-8"))

    def age(self, tenant: str, reference: str) -> float:
        """Seconds since the case file was last written."""
        try:
            return max(0.0, time.time() - (self._case_dir(tenant, reference) / "case.json").stat().st_mtime)
        except OSError:
            return 0.0

    def save(self, tenant: str, case: dict[str, Any]) -> None:
        folder = self._case_dir(tenant, case["reference"])
        with self.lock:
            folder.mkdir(parents=True, exist_ok=True)
            tmp = folder / "case.json.tmp"
            tmp.write_text(json.dumps(case, ensure_ascii=False, default=str), encoding="utf-8")
            tmp.replace(folder / "case.json")

    def list(self, tenant: str) -> list[dict[str, Any]]:
        base = self.root / safe(tenant, "tenant") / "cases"
        rows = []
        for path in sorted(base.glob("*/case.json")) if base.is_dir() else []:
            try:
                with self.lock:   # never read a case file while it is being replaced (Windows refuses the read)
                    c = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            a = c.get("assessment") or {}
            t = a.get("triage") or {}
            found = a.get("findings") or []
            waiting = [f for f in found if f.get("requires_human") and f.get("status") in ("fail", "review", "missing")]
            rows.append({"reference": c["reference"], "profile": c["profile"], "status": c.get("status"), "created_at": c.get("created_at"),
                         "documents": len([d for d in c.get("documents") or [] if not d.get("removed")]), "outcome": (a.get("recommendation") or {}).get("outcome"),
                         "label": (a.get("recommendation") or {}).get("label"), "assessed_at": a.get("at"), "decision": (c.get("decision") or {}).get("decision"),
                         "decided_by": (c.get("decision") or {}).get("by"), "decided_at": (c.get("decision") or {}).get("at"),
                         "title": c.get("fields", {}).get("claim_type") or c.get("fields", {}).get("title") or "",
                         "priority": t.get("priority"), "queue": t.get("queue_name"), "due": t.get("due"), "clock": t.get("clock"), "assigned_to": (c.get("assigned") or {}).get("to"),
                         "open_findings": len(waiting), "watchlist_matches": sum(1 for f in waiting if f.get("agent") == "screening"),
                         "integrity_alerts": sum(1 for f in waiting if f.get("agent") == "integrity"), "state": (c.get("progress") or {}).get("state")})
        return sorted(rows, key=lambda r: r.get("created_at") or "", reverse=True)

    def write_file(self, tenant: str, reference: str, doc_id: str, version: int, name: str, data: bytes) -> str:
        folder = self._case_dir(tenant, reference) / "files"
        folder.mkdir(parents=True, exist_ok=True)
        rel = f"{safe(doc_id, 'document id')}_v{int(version)}_{re.sub(r'[^A-Za-z0-9._-]+', '-', name)[-60:]}"
        target = folder / rel
        if target.exists():
            raise StoreError("a document version is immutable and this one already exists")
        target.write_bytes(data)
        return rel

    def read_file(self, tenant: str, reference: str, rel: str) -> bytes:
        folder = (self._case_dir(tenant, reference) / "files").resolve()
        target = (folder / rel).resolve()
        if folder not in target.parents or not target.is_file():
            raise KeyError("no such file")
        return target.read_bytes()

    def feedback(self, tenant: str, profile: str) -> dict[str, Any]:
        path = self.root / safe(tenant, "tenant") / "feedback" / f"{safe(profile, 'profile')}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        return {"overlay": {"version": 0, "signals": {}, "synonyms": {}, "applied": []}, "proposals": []}

    def save_feedback(self, tenant: str, profile: str, data: dict[str, Any]) -> None:
        path = self.root / safe(tenant, "tenant") / "feedback" / f"{safe(profile, 'profile')}.json"
        with self.lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
