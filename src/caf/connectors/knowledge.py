"""Knowledge connectors you own: vector store, record store (quote bank, learning repository,
experiment backlog, research cache), warehouse (read-only SQL), short links and translation."""

from __future__ import annotations

import json
import re
import sqlite3
import string
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from .base import Connector, ConnectorError, connector


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ----------------------------------------------------------------------------- vector store

class VectorStore(Connector):
    def upsert(self, collection: str, docs: list[dict[str, Any]]) -> int: raise NotImplementedError
    def search(self, collection: str, query: str, k: int = 5, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]: raise NotImplementedError
    def count(self, collection: str) -> int: raise NotImplementedError


@connector("vectorstore", "tfidf")
class TfidfVectorStore(VectorStore):
    """In-process TF-IDF retrieval (no service needed). Swap for Qdrant / pgvector / Chroma in production."""

    _collections: dict[str, dict[str, dict[str, Any]]] = {}
    _lock = threading.Lock()

    def upsert(self, collection: str, docs: list[dict[str, Any]]) -> int:
        with self._lock:
            col = self._collections.setdefault(collection, {})
            for d in docs:
                did = str(d.get("id") or uuid.uuid4().hex)
                col[did] = {"id": did, "text": d["text"], "metadata": d.get("metadata", {})}
            return len(col)

    def count(self, collection: str) -> int:
        return len(self._collections.get(collection, {}))

    def search(self, collection: str, query: str, k: int = 5, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        from ..core.textutil import tfidf_similarity

        docs = list(self._collections.get(collection, {}).values())
        if filters:
            docs = [d for d in docs if all(_match(d["metadata"].get(fk), fv) for fk, fv in filters.items())]
        if not docs:
            return []
        scores = tfidf_similarity(query, [d["text"] for d in docs])
        ranked = sorted(zip(scores, docs), key=lambda x: x[0], reverse=True)[:k]
        return [{**d, "score": round(float(s), 4)} for s, d in ranked]


def _match(value: Any, wanted: Any) -> bool:
    if isinstance(wanted, (list, tuple, set)):
        return value in wanted or (isinstance(value, list) and bool(set(value) & set(wanted)))
    return value == wanted or (isinstance(value, list) and wanted in value)


# ----------------------------------------------------------------------------- record store

class Store(Connector):
    def put(self, collection: str, record: dict[str, Any], key: str | None = None) -> str: raise NotImplementedError
    def get(self, collection: str, key: str) -> dict[str, Any] | None: raise NotImplementedError
    def query(self, collection: str, where: dict[str, Any] | None = None, limit: int = 100) -> list[dict[str, Any]]: raise NotImplementedError


@connector("store", "sqlite")
class SqliteStore(Store):
    """SQLite record store under runs/_store/caf.sqlite (quote bank, learnings, backlog, caches)."""

    _lock = threading.Lock()

    @property
    def db_path(self) -> Path:
        p = self.settings.resolve(self.config["path"]) if self.config.get("path") else self.settings.runs_dir / "_store" / "caf.sqlite"
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("CREATE TABLE IF NOT EXISTS records (collection TEXT, key TEXT, data TEXT, created_at TEXT, "
                     "PRIMARY KEY (collection, key))")
        return conn

    def put(self, collection: str, record: dict[str, Any], key: str | None = None) -> str:
        key = key or record.get("id") or uuid.uuid4().hex
        with self._lock, self._conn() as conn:
            conn.execute("INSERT OR REPLACE INTO records VALUES (?,?,?,?)",
                         (collection, str(key), json.dumps(record, default=str, ensure_ascii=False), _stamp()))
        return str(key)

    def get(self, collection: str, key: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute("SELECT data FROM records WHERE collection=? AND key=?", (collection, key)).fetchone()
        return json.loads(row[0]) if row else None

    def query(self, collection: str, where: dict[str, Any] | None = None, limit: int = 100) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT data FROM records WHERE collection=? ORDER BY created_at DESC", (collection,)).fetchall()
        out = [json.loads(r[0]) for r in rows]
        if where:
            out = [r for r in out if all(r.get(k) == v for k, v in where.items())]
        return out[:limit]


# ----------------------------------------------------------------------------- warehouse

_FORBIDDEN = re.compile(r"\b(insert|update|delete|drop|alter|create|attach|detach|copy|export|import|pragma|install|load|call|set)\b", re.I)


class Warehouse(Connector):
    dialect: str = "duckdb"

    def tables(self) -> dict[str, list[tuple[str, str]]]: raise NotImplementedError
    def query(self, sql: str, *, max_rows: int = 10_000) -> pd.DataFrame: raise NotImplementedError


@connector("warehouse", "duckdb")
class DuckDbWarehouse(Warehouse):
    """Read-only DuckDB over CSV/Parquet files (stand-in for Snowflake / BigQuery / Databricks / Synapse)."""

    dialect = "duckdb"
    _lock = threading.RLock()  # re-entrant: query() holds it while the `con` property may initialise

    def __init__(self, config: dict[str, Any], hub: Any):
        super().__init__(config, hub)
        self._con = None

    @property
    def con(self):
        import duckdb

        with self._lock:
            if self._con is None:
                folder = self.path("path", "sample_data/warehouse")
                con = duckdb.connect(database=":memory:")
                for f in sorted(list(folder.glob("*.csv")) + list(folder.glob("*.parquet"))):
                    reader = "read_csv_auto" if f.suffix == ".csv" else "read_parquet"
                    con.execute(f"CREATE TABLE {f.stem} AS SELECT * FROM {reader}('{f.as_posix()}')")
                con.execute("SET enable_external_access = false")
                self._con = con
            return self._con

    def tables(self) -> dict[str, list[tuple[str, str]]]:
        rows = self.con.execute("SELECT table_name, column_name, data_type FROM information_schema.columns "
                                "WHERE table_schema = 'main' ORDER BY table_name, ordinal_position").fetchall()
        out: dict[str, list[tuple[str, str]]] = {}
        for t, c, d in rows:
            out.setdefault(t, []).append((c, d))
        return out

    def query(self, sql: str, *, max_rows: int = 10_000) -> pd.DataFrame:
        stripped = sql.strip().rstrip(";")
        if ";" in stripped:
            raise ConnectorError("Only a single statement is allowed")
        if not re.match(r"^\s*(with|select)\b", stripped, re.I) or _FORBIDDEN.search(re.sub(r"'[^']*'", "''", stripped)):
            raise ConnectorError("Read-only warehouse: only SELECT/WITH queries are permitted")
        with self._lock:
            cur = self.con.cursor()
            try:
                df = cur.execute(f"SELECT * FROM ({stripped}) AS q LIMIT {int(max_rows)}").df()
            finally:
                cur.close()
        return df


# ----------------------------------------------------------------------------- short links

class Shortlinks(Connector):
    def shorten(self, url: str, *, code: str | None = None, meta: dict[str, Any] | None = None) -> dict[str, Any]: raise NotImplementedError
    def resolve(self, code: str) -> str | None: raise NotImplementedError
    def record_scan(self, code: str, meta: dict[str, Any] | None = None) -> None: raise NotImplementedError
    def stats(self, code: str) -> dict[str, Any]: raise NotImplementedError


@connector("shortlink", "sqlite")
class SqliteShortlinks(Shortlinks):
    """Own short-link + scan tracking service (served by the API at /r/{code})."""

    _lock = threading.Lock()
    _alphabet = string.ascii_letters + string.digits

    @property
    def base_url(self) -> str:
        return (self.config.get("base_url") or "http://localhost:8080/r/").rstrip("/") + "/"

    def _conn(self) -> sqlite3.Connection:
        p = self.settings.runs_dir / "_store" / "shortlinks.sqlite"
        p.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(p, timeout=30)
        conn.execute("CREATE TABLE IF NOT EXISTS links (code TEXT PRIMARY KEY, target TEXT, meta TEXT, created_at TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS scans (code TEXT, ts TEXT, meta TEXT)")
        return conn

    def shorten(self, url: str, *, code: str | None = None, meta: dict[str, Any] | None = None) -> dict[str, Any]:
        import random

        with self._lock, self._conn() as conn:
            existing = conn.execute("SELECT code FROM links WHERE target=?", (url,)).fetchone()
            if existing and not code:
                code = existing[0]
            else:
                rnd = random.Random(url)
                code = code or "".join(rnd.choice(self._alphabet) for _ in range(7))
                conn.execute("INSERT OR REPLACE INTO links VALUES (?,?,?,?)", (code, url, json.dumps(meta or {}), _stamp()))
        return {"code": code, "short_url": self.base_url + code, "target": url}

    def resolve(self, code: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute("SELECT target FROM links WHERE code=?", (code,)).fetchone()
        return row[0] if row else None

    def record_scan(self, code: str, meta: dict[str, Any] | None = None) -> None:
        with self._lock, self._conn() as conn:
            conn.execute("INSERT INTO scans VALUES (?,?,?)", (code, _stamp(), json.dumps(meta or {})))

    def stats(self, code: str) -> dict[str, Any]:
        with self._conn() as conn:
            n = conn.execute("SELECT COUNT(*) FROM scans WHERE code=?", (code,)).fetchone()[0]
            last = conn.execute("SELECT MAX(ts) FROM scans WHERE code=?", (code,)).fetchone()[0]
        return {"code": code, "scans": n, "last_scan": last, "target": self.resolve(code)}


# ----------------------------------------------------------------------------- translation

_PLACEHOLDER = re.compile(r"(\{\{[^}]+\}\}|\{[A-Za-z0-9_]+\}|%[sd]|https?://\S+|<[^>]+>)")


class Translation(Connector):
    def translate(self, segments: list[str], *, source: str, target: str, glossary: dict[str, str] | None = None,
                  dnt: list[str] | None = None, tone: str | None = None) -> list[str]:
        raise NotImplementedError


@connector("translation", "llm")
class LlmTranslation(Translation):
    """Translation through the framework's own model adapter (with glossary + do-not-translate)."""

    def translate(self, segments: list[str], *, source: str, target: str, glossary: dict[str, str] | None = None,
                  dnt: list[str] | None = None, tone: str | None = None) -> list[str]:
        from pydantic import BaseModel

        llm = self.hub.llm
        if llm is None or llm.offline:
            return [_offline_translate(s, target) for s in segments]

        class Out(BaseModel):
            translations: list[str]

        gl = "\n".join(f"- {k} => {v}" for k, v in (glossary or {}).items()) or "- (none)"
        keep = ", ".join(dnt or []) or "(none)"
        numbered = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(segments))
        prompt = (f"Translate each numbered segment from {source} to {target}. Adapt idiom and tone for the target market "
                  f"({tone or 'natural, on-brand'}) rather than translating literally. Never translate or alter: placeholders "
                  f"like {{name}}, {{{{var}}}}, %s, URLs, HTML tags. Do not translate these terms: {keep}.\nGlossary (must use):\n{gl}\n\n"
                  f"Return exactly {len(segments)} translations in order.\n\nSegments:\n{numbered}")
        out = llm.structured(prompt, Out, tier="balanced")
        if len(out.translations) != len(segments):
            raise ConnectorError("Translation count mismatch")
        return out.translations


def _offline_translate(text: str, target: str) -> str:
    """Offline stand-in: keeps placeholders intact and marks the segment, so QA paths still run."""
    parts = _PLACEHOLDER.split(text)
    return f"[offline:{target}] " + "".join(parts)
