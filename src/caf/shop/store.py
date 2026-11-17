"""The shop's records: customers, API keys, subscriptions, runs, usage and access requests, in one SQLite file.

The file lives at ``$CAF_DATA_DIR/shop.db`` (default ``<home>/data/shop.db``). Every write is a short transaction, so
the API, the CLI and the admin console can share the file. Secrets never land here in clear: API keys are stored as
SHA-256 hashes (the full key is shown once, when issued) and a customer's model credential is stored encrypted
(see caf.shop.secrets) or as the name of an environment variable on the server.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

KEY_PREFIX = "cxn"                 # cxn_<8 id chars>_<40 secret chars>
CUSTOMER_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
RUN_MODES = {"coforge_cloud": 1, "coforge_cloud_customer_model": 2, "customer_cloud": 3}

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, run_mode TEXT NOT NULL DEFAULT 'coforge_cloud',
    status TEXT NOT NULL DEFAULT 'active', contact TEXT DEFAULT '', notes TEXT DEFAULT '',
    policy TEXT NOT NULL DEFAULT '{}', last_check TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS api_keys (
    id TEXT PRIMARY KEY, customer_id TEXT NOT NULL REFERENCES customers(id), prefix TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE, label TEXT DEFAULT '', created_at TEXT NOT NULL, last_used_at TEXT, revoked_at TEXT);
CREATE INDEX IF NOT EXISTS api_keys_prefix ON api_keys(prefix);
CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id TEXT NOT NULL REFERENCES customers(id),
    kind TEXT NOT NULL, item_id TEXT NOT NULL, starts_at TEXT NOT NULL, ends_at TEXT, status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL, UNIQUE(customer_id, kind, item_id));
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, surface TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, customer_id TEXT NOT NULL, run_id TEXT NOT NULL,
    agent_id TEXT NOT NULL, agent_version TEXT DEFAULT '', surface TEXT NOT NULL, run_mode TEXT NOT NULL,
    provider TEXT DEFAULT '', status TEXT NOT NULL, llm_calls INTEGER DEFAULT 0, input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0, duration_ms REAL DEFAULT 0);
CREATE INDEX IF NOT EXISTS usage_customer_ts ON usage(customer_id, ts);
CREATE TABLE IF NOT EXISTS access_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, name TEXT NOT NULL, company TEXT DEFAULT '',
    email TEXT NOT NULL, items TEXT DEFAULT '[]', run_mode TEXT DEFAULT '', message TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new');
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def data_dir(settings: Any = None) -> Path:
    env = os.environ.get("CAF_DATA_DIR")
    if env:
        return Path(env)
    home = getattr(settings, "home", None) or Path(os.environ.get("CAF_HOME") or Path.cwd())
    return Path(home) / "data"


class ShopError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class ShopStore:
    def __init__(self, path: str | Path | None = None, settings: Any = None):
        self.path = Path(path) if path else data_dir(settings) / "shop.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        con = sqlite3.connect(self.path, timeout=30)
        try:
            con.executescript(SCHEMA)
            con.commit()
        finally:
            con.close()

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            con = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            con.row_factory = sqlite3.Row
            try:
                con.execute("BEGIN")
                yield con
                if con.in_transaction:
                    con.execute("COMMIT")
            except BaseException:
                if con.in_transaction:
                    con.execute("ROLLBACK")
                raise
            finally:
                con.close()

    # ------------------------------------------------------------------------------- customers
    @staticmethod
    def _customer(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["policy"] = json.loads(d.get("policy") or "{}")
        d["last_check"] = json.loads(d["last_check"]) if d.get("last_check") else None
        d["run_mode_number"] = RUN_MODES.get(d["run_mode"], 1)
        return d

    def add_customer(self, customer_id: str, name: str, *, run_mode: str = "coforge_cloud", contact: str = "",
                     notes: str = "", policy: dict[str, Any] | None = None) -> dict[str, Any]:
        customer_id = customer_id.strip().lower()
        if not CUSTOMER_ID.match(customer_id):
            raise ShopError("customer id: lowercase letters, digits and '-' (2 to 63 characters)")
        if run_mode not in RUN_MODES:
            raise ShopError(f"run_mode must be one of {sorted(RUN_MODES)}")
        ts = now_iso()
        with self._db() as db:
            if db.execute("SELECT 1 FROM customers WHERE id=?", (customer_id,)).fetchone():
                raise ShopError(f"customer '{customer_id}' already exists", 409)
            db.execute("INSERT INTO customers(id,name,run_mode,contact,notes,policy,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                       (customer_id, name.strip(), run_mode, contact, notes, json.dumps(policy or {}), ts, ts))
        return self.get_customer(customer_id)

    def get_customer(self, customer_id: str) -> dict[str, Any]:
        with self._db() as db:
            row = db.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
        if row is None:
            raise ShopError(f"unknown customer '{customer_id}'", 404)
        return self._customer(row)

    def list_customers(self) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM customers ORDER BY created_at").fetchall()
        return [self._customer(r) for r in rows]

    def update_customer(self, customer_id: str, **fields: Any) -> dict[str, Any]:
        allowed = {"name", "run_mode", "status", "contact", "notes", "policy", "last_check"}
        bad = set(fields) - allowed
        if bad:
            raise ShopError(f"cannot update {sorted(bad)}")
        if "run_mode" in fields and fields["run_mode"] not in RUN_MODES:
            raise ShopError(f"run_mode must be one of {sorted(RUN_MODES)}")
        if "status" in fields and fields["status"] not in ("active", "suspended", "ended"):
            raise ShopError("status must be active, suspended or ended")
        values = {k: (json.dumps(v) if k in ("policy", "last_check") and v is not None else v) for k, v in fields.items()}
        values["updated_at"] = now_iso()
        self.get_customer(customer_id)
        with self._db() as db:
            db.execute(f"UPDATE customers SET {', '.join(f'{k}=?' for k in values)} WHERE id=?", (*values.values(), customer_id))
        return self.get_customer(customer_id)

    # ------------------------------------------------------------------------------- API keys
    def issue_key(self, customer_id: str, label: str = "") -> tuple[str, dict[str, Any]]:
        """Create a key for a customer. Returns (the full key, its record); the full key is never stored."""
        self.get_customer(customer_id)
        key_id = secrets.token_hex(4)
        full = f"{KEY_PREFIX}_{key_id}_{secrets.token_urlsafe(30)}"
        ts = now_iso()
        with self._db() as db:
            db.execute("INSERT INTO api_keys(id,customer_id,prefix,key_hash,label,created_at) VALUES(?,?,?,?,?,?)",
                       (key_id, customer_id, f"{KEY_PREFIX}_{key_id}", hash_key(full), label, ts))
        return full, {"id": key_id, "customer_id": customer_id, "prefix": f"{KEY_PREFIX}_{key_id}", "label": label, "created_at": ts}

    def list_keys(self, customer_id: str) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = db.execute("SELECT id,customer_id,prefix,label,created_at,last_used_at,revoked_at FROM api_keys "
                              "WHERE customer_id=? ORDER BY created_at", (customer_id,)).fetchall()
        return [dict(r) for r in rows]

    def revoke_key(self, customer_id: str, key_id: str) -> None:
        with self._db() as db:
            cur = db.execute("UPDATE api_keys SET revoked_at=? WHERE id=? AND customer_id=? AND revoked_at IS NULL",
                             (now_iso(), key_id, customer_id))
            if cur.rowcount == 0:
                raise ShopError("unknown or already revoked key", 404)

    @staticmethod
    def looks_like_key(token: str) -> bool:
        return bool(token) and token.startswith(f"{KEY_PREFIX}_") and token.count("_") >= 2

    def customer_for_key(self, key: str) -> dict[str, Any] | None:
        """The active customer a key belongs to, or None. Comparison is on the SHA-256 hash."""
        if not self.looks_like_key(key):
            return None
        prefix = "_".join(key.split("_", 2)[:2])
        digest = hash_key(key)
        with self._db() as db:
            row = db.execute("SELECT customer_id, id FROM api_keys WHERE prefix=? AND key_hash=? AND revoked_at IS NULL",
                             (prefix, digest)).fetchone()
            if row is None:
                return None
            db.execute("UPDATE api_keys SET last_used_at=? WHERE id=?", (now_iso(), row["id"]))
            cust = db.execute("SELECT * FROM customers WHERE id=?", (row["customer_id"],)).fetchone()
        if cust is None or cust["status"] != "active":
            return None
        return self._customer(cust)

    # ------------------------------------------------------------------------------- subscriptions
    def set_subscriptions(self, customer_id: str, items: list[dict[str, Any]], *, replace: bool = True) -> list[dict[str, Any]]:
        """items: [{kind: agent|playbook|all, item_id, ends_at?}] ('all' covers every agent and playbook)."""
        self.get_customer(customer_id)
        ts = now_iso()
        with self._db() as db:
            if replace:
                db.execute("DELETE FROM subscriptions WHERE customer_id=?", (customer_id,))
            for item in items:
                kind = str(item.get("kind") or "agent")
                if kind not in ("agent", "playbook", "all"):
                    raise ShopError("subscription kind must be agent, playbook or all")
                item_id = str(item.get("item_id") or ("*" if kind == "all" else ""))
                if not item_id:
                    raise ShopError("subscription needs item_id")
                db.execute("INSERT OR REPLACE INTO subscriptions(customer_id,kind,item_id,starts_at,ends_at,status,created_at) "
                           "VALUES(?,?,?,?,?,?,?)", (customer_id, kind, item_id, item.get("starts_at") or ts, item.get("ends_at"),
                                                   item.get("status") or "active", ts))
        return self.list_subscriptions(customer_id)

    def list_subscriptions(self, customer_id: str, *, active_only: bool = False) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM subscriptions WHERE customer_id=? ORDER BY kind, item_id", (customer_id,)).fetchall()
        out = []
        today = now_iso()
        for r in rows:
            d = dict(r)
            d["current"] = d["status"] == "active" and (not d["ends_at"] or d["ends_at"] >= today[:10])
            if not active_only or d["current"]:
                out.append(d)
        return out

    # ------------------------------------------------------------------------------- runs + usage
    def record_run(self, run_id: str, customer_id: str, surface: str) -> None:
        with self._db() as db:
            db.execute("INSERT OR IGNORE INTO runs(run_id,customer_id,surface,created_at) VALUES(?,?,?,?)",
                       (run_id, customer_id, surface, now_iso()))

    def run_owner(self, run_id: str) -> str | None:
        with self._db() as db:
            row = db.execute("SELECT customer_id FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return row["customer_id"] if row else None

    def record_usage(self, *, customer_id: str, run_id: str, agent_id: str, surface: str, run_mode: str, status: str,
                     metrics: dict[str, Any] | None = None, agent_version: str = "", provider: str = "") -> None:
        m = metrics or {}
        with self._db() as db:
            db.execute("INSERT INTO usage(ts,customer_id,run_id,agent_id,agent_version,surface,run_mode,provider,status,"
                       "llm_calls,input_tokens,output_tokens,duration_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (now_iso(), customer_id, run_id, agent_id, agent_version, surface, run_mode, provider, status,
                        int(m.get("llm_calls", 0) or 0), int(m.get("input_tokens", 0) or 0), int(m.get("output_tokens", 0) or 0),
                        float(m.get("duration_ms", 0) or 0)))

    def runs_today(self, customer_id: str) -> int:
        since = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00")
        with self._db() as db:
            row = db.execute("SELECT COUNT(DISTINCT run_id) AS n FROM usage WHERE customer_id=? AND ts>=?", (customer_id, since)).fetchone()
        return int(row["n"] or 0)

    def usage_summary(self, customer_id: str | None = None, month: str | None = None) -> dict[str, Any]:
        """Usage per agent for one month (YYYY-MM, default: this month), for one customer or all."""
        month = month or datetime.now(timezone.utc).strftime("%Y-%m")
        start = f"{month}-01"
        year, mon = int(month[:4]), int(month[5:7])
        end = (datetime(year + (mon // 12), (mon % 12) + 1, 1) - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S")
        where, params = ["ts>=? AND ts<=?"], [start, end]
        if customer_id:
            where.append("customer_id=?")
            params.append(customer_id)
        with self._db() as db:
            rows = db.execute(f"SELECT customer_id, agent_id, run_mode, COUNT(*) AS runs, "
                              f"SUM(CASE WHEN status IN ('ok','degraded') THEN 1 ELSE 0 END) AS ok_runs, "
                              f"SUM(llm_calls) AS llm_calls, SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens, "
                              f"SUM(duration_ms) AS duration_ms FROM usage WHERE {' AND '.join(where)} "
                              f"GROUP BY customer_id, agent_id, run_mode ORDER BY customer_id, runs DESC", params).fetchall()
        lines = [dict(r) for r in rows]
        totals = {"runs": sum(r["runs"] for r in lines), "llm_calls": sum(r["llm_calls"] or 0 for r in lines),
                  "input_tokens": sum(r["input_tokens"] or 0 for r in lines), "output_tokens": sum(r["output_tokens"] or 0 for r in lines)}
        return {"month": month, "customer_id": customer_id, "lines": lines, "totals": totals}

    def recent_usage(self, customer_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        with self._db() as db:
            if customer_id:
                rows = db.execute("SELECT * FROM usage WHERE customer_id=? ORDER BY id DESC LIMIT ?", (customer_id, limit)).fetchall()
            else:
                rows = db.execute("SELECT * FROM usage ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------------------- access requests
    def add_request(self, *, name: str, email: str, company: str = "", items: list[str] | None = None, run_mode: str = "",
                    message: str = "") -> dict[str, Any]:
        if not name.strip() or "@" not in email:
            raise ShopError("name and a valid email are required")
        with self._db() as db:
            cur = db.execute("INSERT INTO access_requests(created_at,name,company,email,items,run_mode,message) VALUES(?,?,?,?,?,?,?)",
                             (now_iso(), name.strip()[:200], company.strip()[:200], email.strip()[:200],
                              json.dumps([str(i)[:80] for i in (items or [])][:50]), run_mode[:40], message.strip()[:2000]))
            row = db.execute("SELECT * FROM access_requests WHERE id=?", (cur.lastrowid,)).fetchone()
        return self._request(row)

    @staticmethod
    def _request(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["items"] = json.loads(d.get("items") or "[]")
        return d

    def list_requests(self, status: str | None = None) -> list[dict[str, Any]]:
        with self._db() as db:
            if status:
                rows = db.execute("SELECT * FROM access_requests WHERE status=? ORDER BY id DESC", (status,)).fetchall()
            else:
                rows = db.execute("SELECT * FROM access_requests ORDER BY id DESC").fetchall()
        return [self._request(r) for r in rows]

    def set_request_status(self, request_id: int, status: str) -> None:
        if status not in ("new", "contacted", "converted", "closed"):
            raise ShopError("status must be new, contacted, converted or closed")
        with self._db() as db:
            cur = db.execute("UPDATE access_requests SET status=? WHERE id=?", (status, request_id))
            if cur.rowcount == 0:
                raise ShopError("unknown request", 404)
