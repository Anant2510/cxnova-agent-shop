"""Durable run state for the orchestrator, in one SQLite file (``$CAF_DATA_DIR/orchestrator.db``).

Three things live there:

* **Checkpoints** (``SqliteSaver``): the LangGraph checkpoints of every run, so a restart of the service keeps finished
  runs viewable, runs waiting at the approval gate approvable, and runs cut off mid-way resumable from their last
  checkpoint. The saver is LangGraph's own ``InMemorySaver`` with a write-through to SQLite: every checkpoint, blob and
  pending write is stored as it is made, and a run's checkpoints are loaded back into memory only when that run is
  looked at. ``evict()`` drops a settled run from memory, so a long-lived service holds only the runs in flight.
* **The run index** (``RunStore``): one row per run (status, error, owner, timestamps), used to tell a run that is
  still going from one a restart cut off, to report runs that crashed before their first checkpoint, and to prune
  old runs.
* **The approvals ledger** (``RunStore``): every decision on a held action (approve / reject), who made it and what
  executing it returned. Decisions can be taken while the run is still going; the approval gate reads the ledger. A
  decision is *claimed* (one row per action, inserted only if absent) before the action executes, so an action executes
  once even when two processes, or a decision mid-run and the gate, reach it at the same moment.
* **Heartbeats**: a process executing a run touches its index row every few seconds, so another process (the CLI next
  to the service, a second instance) sees the run as running, and a run only counts as stalled once its heartbeat
  stopped (orchestrator.stall_after_s).
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

from langgraph.checkpoint.base import get_checkpoint_id
from langgraph.checkpoint.memory import InMemorySaver

log = logging.getLogger("caf.orchestrator.persistence")

SCHEMA = """
CREATE TABLE IF NOT EXISTS checkpoints (
    thread_id TEXT NOT NULL, checkpoint_ns TEXT NOT NULL, checkpoint_id TEXT NOT NULL, parent_id TEXT,
    ckpt_type TEXT NOT NULL, ckpt BLOB NOT NULL, meta_type TEXT NOT NULL, meta BLOB NOT NULL,
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id));
CREATE TABLE IF NOT EXISTS checkpoint_blobs (
    thread_id TEXT NOT NULL, checkpoint_ns TEXT NOT NULL, channel TEXT NOT NULL, version TEXT NOT NULL, vtype TEXT NOT NULL,
    value_type TEXT NOT NULL, value BLOB NOT NULL,
    PRIMARY KEY (thread_id, checkpoint_ns, channel, version, vtype));
CREATE TABLE IF NOT EXISTS checkpoint_writes (
    thread_id TEXT NOT NULL, checkpoint_ns TEXT NOT NULL, checkpoint_id TEXT NOT NULL, task_id TEXT NOT NULL, idx INTEGER NOT NULL,
    channel TEXT NOT NULL, value_type TEXT NOT NULL, value BLOB NOT NULL, task_path TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx));
CREATE TABLE IF NOT EXISTS run_index (
    run_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, status TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '', customer_id TEXT NOT NULL DEFAULT '', surface TEXT NOT NULL DEFAULT '',
    goal TEXT NOT NULL DEFAULT '', label TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS run_index_created ON run_index(created_at);
CREATE TABLE IF NOT EXISTS run_actions (
    run_id TEXT NOT NULL, action_key TEXT NOT NULL, decision TEXT NOT NULL, status TEXT NOT NULL, result TEXT NOT NULL DEFAULT '{}',
    decided_by TEXT NOT NULL DEFAULT '', decided_at TEXT NOT NULL,
    PRIMARY KEY (run_id, action_key));
"""

SETTLED = ("completed", "completed_with_errors", "failed")
_STAMP = re.compile(r"-(\d{8})-(\d{6})-[0-9a-f]{6}$")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def age_s(stamp: str | None) -> float:
    """Seconds since an ISO timestamp written by now_iso (inf when missing or unreadable)."""
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(str(stamp))).total_seconds()
    except (TypeError, ValueError):
        return float("inf")


def shown_status(ledger_status: str) -> str:
    """An action's status as a run view shows it. 'executing' (claimed, outcome not stored: the process executing it
    stopped) is shown as 'approved', since whether the side effect happened has to be checked on the target system."""
    return "approved" if ledger_status == "executing" else ledger_status


class _DB:
    """One SQLite file shared by the saver and the run store (WAL: readers never wait for the writer)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.con = sqlite3.connect(self.path, timeout=30, check_same_thread=False, isolation_level=None)
        self.con.row_factory = sqlite3.Row
        with self.lock:
            try:
                self.con.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError:  # some network file systems refuse WAL: the default journal still works
                pass
            self.con.execute("PRAGMA synchronous=NORMAL")
            self.con.executescript(SCHEMA)
            cols = {r[1] for r in self.con.execute("PRAGMA table_info(run_index)")}
            for col in ("goal", "label"):  # files made by the first release of the run store
                if col not in cols:
                    self.con.execute(f"ALTER TABLE run_index ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self.lock:
            self.con.execute("BEGIN")
            try:
                yield self.con
                self.con.execute("COMMIT")
            except BaseException:
                self.con.execute("ROLLBACK")
                raise

    def close(self) -> None:
        with self.lock:
            self.con.close()


_SHARED: dict[str, _DB] = {}
_SHARED_LOCK = threading.Lock()


def open_db(path: str | Path) -> _DB:
    """One connection per file per process, shared by every orchestrator that uses the file."""
    key = str(Path(path).resolve())
    with _SHARED_LOCK:
        db = _SHARED.get(key)
        if db is None or not Path(key).exists():
            db = _SHARED[key] = _DB(key)
        return db


def _vtype(v: Any) -> str:
    return "i" if isinstance(v, int) else "f" if isinstance(v, float) else "s"


def _version(text: str, vtype: str) -> Any:
    return int(text) if vtype == "i" else float(text) if vtype == "f" else text


class SqliteSaver(InMemorySaver):
    """LangGraph's in-memory saver with a write-through to SQLite and per-run lazy loading."""

    def __init__(self, path: str | Path, **kwargs: Any):
        super().__init__(**kwargs)
        self.db = open_db(path)
        self._loaded: set[str] = set()
        self._owned: set[str] = set()   # runs this process is writing: their memory copy is the live one
        self._mem = threading.RLock()

    # -------------------------------------------------------------------------------- loading
    def _ensure(self, thread_id: str) -> None:
        with self._mem:
            if thread_id in self._loaded:
                return
            with self.db.lock:
                ckpts = self.db.con.execute("SELECT * FROM checkpoints WHERE thread_id=?", (thread_id,)).fetchall()
                blobs = self.db.con.execute("SELECT * FROM checkpoint_blobs WHERE thread_id=?", (thread_id,)).fetchall()
                writes = self.db.con.execute("SELECT * FROM checkpoint_writes WHERE thread_id=?", (thread_id,)).fetchall()
            for r in ckpts:
                self.storage[thread_id][r["checkpoint_ns"]][r["checkpoint_id"]] = (
                    (r["ckpt_type"], bytes(r["ckpt"])), (r["meta_type"], bytes(r["meta"])), r["parent_id"])
            for r in blobs:
                self.blobs[(thread_id, r["checkpoint_ns"], r["channel"], _version(r["version"], r["vtype"]))] = (
                    r["value_type"], bytes(r["value"]))
            for r in writes:
                self.writes[(thread_id, r["checkpoint_ns"], r["checkpoint_id"])][(r["task_id"], r["idx"])] = (
                    r["task_id"], r["channel"], (r["value_type"], bytes(r["value"])), r["task_path"])
            self._loaded.add(thread_id)

    def _drop(self, thread_id: str) -> None:
        self.storage.pop(thread_id, None)
        for k in [k for k in self.writes if k[0] == thread_id]:
            del self.writes[k]
        for k in [k for k in self.blobs if k[0] == thread_id]:
            del self.blobs[k]
        self._loaded.discard(thread_id)

    def _load_one(self, thread_id: str, checkpoint_ns: str, checkpoint_id: str | None) -> None:
        """Load just one checkpoint of a run (the latest, or the one named) with the blobs and pending writes it needs.
        A poll of a long run reads one checkpoint, not the run's whole history."""
        with self.db.lock:
            if checkpoint_id:
                row = self.db.con.execute("SELECT * FROM checkpoints WHERE thread_id=? AND checkpoint_ns=? AND checkpoint_id=?",
                                          (thread_id, checkpoint_ns, checkpoint_id)).fetchone()
            else:
                row = self.db.con.execute("SELECT * FROM checkpoints WHERE thread_id=? AND checkpoint_ns=? "
                                          "ORDER BY checkpoint_id DESC LIMIT 1", (thread_id, checkpoint_ns)).fetchone()
            if row is None:
                return
            ckpt = (row["ckpt_type"], bytes(row["ckpt"]))
            versions = (self.serde.loads_typed(ckpt) or {}).get("channel_versions", {})
            blobs = [self.db.con.execute("SELECT * FROM checkpoint_blobs WHERE thread_id=? AND checkpoint_ns=? AND channel=? AND version=? "
                                         "AND vtype=?", (thread_id, checkpoint_ns, ch, str(ver), _vtype(ver))).fetchone()
                     for ch, ver in versions.items()]
            writes = self.db.con.execute("SELECT * FROM checkpoint_writes WHERE thread_id=? AND checkpoint_ns=? AND checkpoint_id=?",
                                         (thread_id, checkpoint_ns, row["checkpoint_id"])).fetchall()
        self.storage[thread_id][checkpoint_ns][row["checkpoint_id"]] = (ckpt, (row["meta_type"], bytes(row["meta"])), row["parent_id"])
        for b in blobs:
            if b is not None:
                self.blobs[(thread_id, checkpoint_ns, b["channel"], _version(b["version"], b["vtype"]))] = (b["value_type"], bytes(b["value"]))
        for w in writes:
            self.writes[(thread_id, checkpoint_ns, w["checkpoint_id"])][(w["task_id"], w["idx"])] = (
                w["task_id"], w["channel"], (w["value_type"], bytes(w["value"])), w["task_path"])

    def evict(self, thread_id: str) -> None:
        """Forget a run's checkpoints in memory (they stay in SQLite and load again when the run is looked at)."""
        with self._mem:
            self._drop(thread_id)
            self._owned.discard(thread_id)

    def loaded_threads(self) -> int:
        with self._mem:
            return len(self._loaded)

    def _read(self, thread_ids: Sequence[str], fn: Any) -> Any:
        """Read through memory. A run this process is not writing is loaded fresh from SQLite for the read and dropped
        after it, so views of settled runs never pile up in memory (and always see another process's writes)."""
        with self._mem:
            for tid in thread_ids:
                if tid not in self._owned:
                    self._drop(tid)
                self._ensure(tid)
            try:
                return fn()
            finally:
                for tid in thread_ids:
                    if tid not in self._owned:
                        self._drop(tid)

    # -------------------------------------------------------------------------------- reads
    def get_tuple(self, config: Any) -> Any:
        thread_id = config["configurable"]["thread_id"]
        with self._mem:
            if thread_id in self._owned:  # this process is writing the run: memory is the live copy
                self._ensure(thread_id)
                return super().get_tuple(config)
            self._drop(thread_id)
            self._load_one(thread_id, config["configurable"].get("checkpoint_ns", ""), get_checkpoint_id(config))
            try:
                return super().get_tuple(config)
            finally:
                self._drop(thread_id)

    def list(self, config: Any, *, filter: dict[str, Any] | None = None, before: Any = None, limit: int | None = None) -> Iterator[Any]:
        if config:
            ids = [config["configurable"]["thread_id"]]
        else:
            with self.db.lock:
                ids = [r[0] for r in self.db.con.execute("SELECT DISTINCT thread_id FROM checkpoints")]
        items = self._read(ids, lambda: list(super(SqliteSaver, self).list(config, filter=filter, before=before, limit=limit)))
        yield from items

    def get_delta_channel_history(self, *, config: Any, channels: Sequence[str]) -> Any:  # langgraph-checkpoint 4.x
        return self._read([config["configurable"]["thread_id"]],
                          lambda: super(SqliteSaver, self).get_delta_channel_history(config=config, channels=channels))  # type: ignore[misc]

    # -------------------------------------------------------------------------------- writes
    def _own(self, thread_id: str) -> None:
        """Called under _mem before a write: from now on this process's memory copy of the run is the live one."""
        if thread_id not in self._owned:
            self._drop(thread_id)   # start from exactly what is stored
            self._ensure(thread_id)
            self._owned.add(thread_id)

    def put(self, config: Any, checkpoint: Any, metadata: Any, new_versions: Any) -> Any:
        thread_id = config["configurable"]["thread_id"]
        ns = config["configurable"].get("checkpoint_ns", "")
        with self._mem:
            self._own(thread_id)
            out = super().put(config, checkpoint, metadata, new_versions)
            ckpt, meta, parent = self.storage[thread_id][ns][checkpoint["id"]]
            blob_rows = []
            for ch, ver in new_versions.items():
                vt, vb = self.blobs[(thread_id, ns, ch, ver)]
                blob_rows.append((thread_id, ns, ch, str(ver), _vtype(ver), vt, vb))
        with self.db.tx() as con:
            con.execute("INSERT OR REPLACE INTO checkpoints VALUES (?,?,?,?,?,?,?,?)",
                        (thread_id, ns, checkpoint["id"], parent, ckpt[0], ckpt[1], meta[0], meta[1]))
            if blob_rows:
                con.executemany("INSERT OR REPLACE INTO checkpoint_blobs VALUES (?,?,?,?,?,?,?)", blob_rows)
        return out

    def put_writes(self, config: Any, writes: Sequence[tuple[str, Any]], task_id: str, task_path: str = "") -> None:
        thread_id = config["configurable"]["thread_id"]
        ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = config["configurable"]["checkpoint_id"]
        with self._mem:
            self._own(thread_id)
            super().put_writes(config, writes, task_id, task_path)
            stored = self.writes.get((thread_id, ns, checkpoint_id), {})
            rows = [(thread_id, ns, checkpoint_id, tid, idx, ch, val[0], val[1], path or "")
                    for (tid, idx), (_, ch, val, path) in stored.items() if tid == task_id]
        if rows:
            with self.db.tx() as con:
                con.executemany("INSERT OR REPLACE INTO checkpoint_writes VALUES (?,?,?,?,?,?,?,?,?)", rows)

    def delete_thread(self, thread_id: str) -> None:
        with self._mem:
            super().delete_thread(thread_id)
            self._loaded.discard(thread_id)
            self._owned.discard(thread_id)
        with self.db.tx() as con:
            for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
                con.execute(f"DELETE FROM {table} WHERE thread_id=?", (thread_id,))


class RunStore:
    """The run index and the approvals ledger (same file as the checkpoints)."""

    def __init__(self, path: str | Path):
        self.db = open_db(path)

    # -------------------------------------------------------------------------------- run index
    def record(self, run_id: str, status: str, *, error: str | None = None, customer_id: str | None = None,
               surface: str | None = None, goal: str | None = None, label: str | None = None) -> None:
        now = now_iso()
        with self.db.tx() as con:
            row = con.execute("SELECT run_id FROM run_index WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                con.execute("INSERT INTO run_index(run_id,created_at,updated_at,status,error,customer_id,surface,goal,label) "
                            "VALUES(?,?,?,?,?,?,?,?,?)", (run_id, now, now, status, error or "", customer_id or "", surface or "",
                                                          (goal or "")[:500], (label or "")[:120]))
            else:
                sets, params = ["updated_at=?", "status=?"], [now, status]
                if error is not None:
                    sets.append("error=?")
                    params.append(error)
                con.execute(f"UPDATE run_index SET {', '.join(sets)} WHERE run_id=?", (*params, run_id))

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self.db.lock:
            row = self.db.con.execute("SELECT * FROM run_index WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def list(self, *, limit: int = 50, customer_id: str | None = None) -> list[dict[str, Any]]:
        """The newest runs first (one customer's, or everyone's)."""
        sql, params = "SELECT * FROM run_index", []
        if customer_id is not None:
            sql += " WHERE customer_id=?"
            params.append(customer_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))
        with self.db.lock:
            return [dict(r) for r in self.db.con.execute(sql, params).fetchall()]

    def set_meta(self, run_id: str, *, goal: str, label: str = "") -> None:
        with self.db.tx() as con:
            con.execute("UPDATE run_index SET goal=?, label=CASE WHEN label='' THEN ? ELSE label END WHERE run_id=?",
                        ((goal or "")[:500], (label or "")[:120], run_id))

    def touch(self, run_ids: Sequence[str]) -> None:
        """Heartbeat of the runs this process is executing."""
        if not run_ids:
            return
        with self.db.tx() as con:
            con.executemany("UPDATE run_index SET updated_at=? WHERE run_id=?", [(now_iso(), r) for r in run_ids])

    def unsettled(self) -> list[dict[str, Any]]:
        """Runs whose last known state is not settled (running, queued or waiting for approval)."""
        with self.db.lock:
            rows = self.db.con.execute(f"SELECT * FROM run_index WHERE status NOT IN ({','.join('?' * len(SETTLED))}) "
                                       "ORDER BY created_at DESC", SETTLED).fetchall()
        return [dict(r) for r in rows]

    # -------------------------------------------------------------------------------- approvals ledger
    def decisions(self, run_id: str) -> dict[str, dict[str, Any]]:
        with self.db.lock:
            rows = self.db.con.execute("SELECT * FROM run_actions WHERE run_id=?", (run_id,)).fetchall()
        out = {}
        for r in rows:
            d = dict(r)
            d["result"] = json.loads(d.get("result") or "{}")
            out[d["action_key"]] = d
        return out

    def claim(self, run_id: str, key: str, *, decision: str, decided_by: str = "") -> bool:
        """Take the decision on one action. True for the one caller that gets it; everyone else must use what is recorded.
        An approval is recorded as 'executing' until finish() stores the outcome."""
        status = "executing" if decision == "approve" else "rejected"
        with self.db.tx() as con:
            cur = con.execute("INSERT OR IGNORE INTO run_actions(run_id,action_key,decision,status,result,decided_by,decided_at) "
                              "VALUES(?,?,?,?,?,?,?)", (run_id, key, decision, status, "{}", decided_by, now_iso()))
        return cur.rowcount > 0

    def finish(self, run_id: str, key: str, *, status: str, result: dict[str, Any] | None = None) -> None:
        with self.db.tx() as con:
            con.execute("UPDATE run_actions SET status=?, result=? WHERE run_id=? AND action_key=?",
                        (status, json.dumps(result or {}, default=str), run_id, key))

    # -------------------------------------------------------------------------------- retention
    def expired(self, keep_days: float) -> list[str]:
        """Settled runs older than keep_days (and runs the index never saw, by the date in their id)."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
        stamp = cutoff.isoformat(timespec="seconds")
        with self.db.lock:
            old = [r[0] for r in self.db.con.execute(
                f"SELECT run_id FROM run_index WHERE created_at<? AND status IN ({','.join('?' * len(SETTLED))})", (stamp, *SETTLED))]
            known = {r[0] for r in self.db.con.execute("SELECT run_id FROM run_index")}
            threads = [r[0] for r in self.db.con.execute("SELECT DISTINCT thread_id FROM checkpoints")]
        for tid in threads:
            if tid in known:
                continue
            m = _STAMP.search(tid)
            if m and datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc) < cutoff:
                old.append(tid)
        return old

    def forget(self, run_id: str) -> None:
        with self.db.tx() as con:
            con.execute("DELETE FROM run_index WHERE run_id=?", (run_id,))
            con.execute("DELETE FROM run_actions WHERE run_id=?", (run_id,))


class MemoryRunStore:
    """The same interface kept in memory (CAF_RUN_STORE=memory, or a caller's own checkpointer)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._index: dict[str, dict[str, Any]] = {}
        self._actions: dict[str, dict[str, dict[str, Any]]] = {}

    def record(self, run_id: str, status: str, *, error: str | None = None, customer_id: str | None = None,
               surface: str | None = None, goal: str | None = None, label: str | None = None) -> None:
        with self._lock:
            row = self._index.setdefault(run_id, {"run_id": run_id, "created_at": now_iso(), "error": "",
                                                  "customer_id": customer_id or "", "surface": surface or "",
                                                  "goal": (goal or "")[:500], "label": (label or "")[:120]})
            row.update(status=status, updated_at=now_iso())
            if error is not None:
                row["error"] = error

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._index.get(run_id)
            return dict(row) if row else None

    def unsettled(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._index.values() if r["status"] not in SETTLED]

    def list(self, *, limit: int = 50, customer_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            rows = [dict(r) for r in self._index.values() if customer_id is None or r.get("customer_id") == customer_id]
        return sorted(rows, key=lambda r: r["created_at"], reverse=True)[: int(limit)]

    def set_meta(self, run_id: str, *, goal: str, label: str = "") -> None:
        with self._lock:
            row = self._index.get(run_id)
            if row is not None:
                row["goal"] = (goal or "")[:500]
                row["label"] = row.get("label") or (label or "")[:120]

    def touch(self, run_ids: Sequence[str]) -> None:
        with self._lock:
            for r in run_ids:
                if r in self._index:
                    self._index[r]["updated_at"] = now_iso()

    def decisions(self, run_id: str) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {k: dict(v) for k, v in self._actions.get(run_id, {}).items()}

    def claim(self, run_id: str, key: str, *, decision: str, decided_by: str = "") -> bool:
        with self._lock:
            actions = self._actions.setdefault(run_id, {})
            if key in actions:
                return False
            actions[key] = {"run_id": run_id, "action_key": key, "decision": decision,
                            "status": "executing" if decision == "approve" else "rejected", "result": {},
                            "decided_by": decided_by, "decided_at": now_iso()}
            return True

    def finish(self, run_id: str, key: str, *, status: str, result: dict[str, Any] | None = None) -> None:
        with self._lock:
            row = self._actions.get(run_id, {}).get(key)
            if row is not None:
                row.update(status=status, result=dict(result or {}))

    def expired(self, keep_days: float) -> list[str]:
        return []

    def forget(self, run_id: str) -> None:
        with self._lock:
            self._index.pop(run_id, None)
            self._actions.pop(run_id, None)
