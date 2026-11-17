"""Exploration Generation - text-to-query over your own warehouse through an owned semantic layer.

The proprietary analytics engine is replaced by config/skills/semantic_layer.yaml (entities, dimensions,
measures, metrics, joins, synonyms). The model returns a SEMANTIC query (metrics, dimensions, filters, time grain,
date range, order, limit) that code validates against the layer and compiles to SQL deterministically - with
fan-out-safe join paths (many-to-one only) and per-entity CTEs when metrics come from different entities. A
free-SQL mode lets the model (or the user) write SQL, which is validated with sqlglot before it runs: single
SELECT statement, known tables and columns, no table functions, no unconstrained cross joins, LIMIT enforced.
Queries run on the read-only warehouse connector; results come back as pandas, a matplotlib chart and a
`dataset` that Chart Summary can consume. A deterministic NL -> semantic parser (token/synonym matching) covers
offline use and invalid model output.
"""

from __future__ import annotations

import calendar
import io
import math
import re
from collections import deque
from datetime import date, datetime, timedelta
from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel, Field

from ...core import AgentKind, AgentSpec, BaseAgent, Category, Check, RunContext, Tier, register
from ...core.handoff import Finding
from ...core.textutil import truncate

MAX_ROWS = 500
GRAINS = ("day", "week", "month", "quarter", "year")

# ============================================================================ semantic layer (pure)


def load_layer(cfg: dict[str, Any]) -> dict[str, Any]:
    """Normalise the semantic layer YAML into lookup tables."""
    entities = {e["name"]: e for e in cfg.get("entities", [])}
    dims: dict[str, list[dict[str, Any]]] = {}
    for e in entities.values():
        for d in e.get("dimensions", []):
            dims.setdefault(d["name"], []).append({**d, "entity": e["name"], "table": e.get("table", e["name"]),
                                                   "expr": f"{e.get('table', e['name'])}.{d.get('column', d['name'])}"})
    metrics: dict[str, dict[str, Any]] = {}
    for e in entities.values():  # entity measures are queryable too
        for m in e.get("measures", []):
            metrics.setdefault(m["name"], {**m, "entity": e["name"], "kind": "measure"})
    for m in cfg.get("metrics", []):
        metrics[m["name"]] = {**m, "kind": "metric"}
    time_dims = {}
    for e in entities.values():
        td = next((d for d in e.get("dimensions", []) if d.get("type") in ("date", "timestamp")), None)
        if td:
            time_dims[e["name"]] = td["name"]
    synonyms = {str(k).lower(): str(v) for k, v in (cfg.get("synonyms") or {}).items()}
    return {"entities": entities, "dimensions": dims, "metrics": metrics, "joins": cfg.get("joins", []), "synonyms": synonyms,
            "time_dims": time_dims, "dialect": cfg.get("dialect", "duckdb"), "examples": cfg.get("examples", [])}


def _on_columns(on: str) -> list[tuple[str, str]]:
    return re.findall(r"([A-Za-z_][\w]*)\.([A-Za-z_][\w]*)", on or "")


def join_edges(layer: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Directed traversal edges; `safe` = many-to-one (the target side joins on its primary key, so no fan-out)."""
    ents = layer["entities"]
    table_to_entity = {e.get("table", n): n for n, e in ents.items()}
    edges: dict[str, list[dict[str, Any]]] = {}
    for j in layer["joins"]:
        cols = _on_columns(j.get("on", ""))
        for src, dst, declared in ((j["left"], j["right"], True), (j["right"], j["left"], False)):
            dst_table = ents.get(dst, {}).get("table", dst)
            dst_cols = [c for t, c in cols if table_to_entity.get(t, t) == dst or t == dst_table]
            safe = bool(dst_cols) and ents.get(dst, {}).get("primary_key") in dst_cols
            jt = str(j.get("type", "inner")).lower() if declared else "inner"
            edges.setdefault(src, []).append({"from": src, "to": dst, "on": j.get("on", ""), "type": jt, "safe": safe})
    return edges


def join_path(layer: dict[str, Any], src: str, dst: str) -> list[dict[str, Any]] | None:
    """Shortest many-to-one path from entity src to dst (None if only fan-out paths exist)."""
    if src == dst:
        return []
    edges = join_edges(layer)
    q: deque[tuple[str, list[dict[str, Any]]]] = deque([(src, [])])
    seen = {src}
    while q:
        node, path = q.popleft()
        for e in edges.get(node, []):
            if not e["safe"] or e["to"] in seen:
                continue
            if e["to"] == dst:
                return path + [e]
            seen.add(e["to"])
            q.append((e["to"], path + [e]))
    return None


def resolve_name(name: str, layer: dict[str, Any], kind: str) -> str | None:
    """Metric / dimension name from a name or synonym ('gwp' -> gross_written_premium)."""
    n = (name or "").strip()
    pool = layer["metrics"] if kind == "metric" else layer["dimensions"]
    if n in pool:
        return n
    low = n.lower().replace("-", " ")
    snake = re.sub(r"[^a-z0-9]+", "_", low).strip("_")
    if snake in pool:
        return snake
    syn = layer["synonyms"].get(low) or layer["synonyms"].get(n.lower())
    if syn and syn in pool:
        return syn
    return None


class SemanticFilter(BaseModel):
    dimension: str = Field(description="Dimension name from the semantic layer")
    op: Literal["=", "!=", ">", ">=", "<", "<=", "in", "not in", "between", "contains"] = "="
    values: list[str] = Field(default_factory=list, description="Values as strings (dates as YYYY-MM-DD)")


class SemanticOrder(BaseModel):
    field: str = Field(description="Metric, dimension or 'period'")
    direction: Literal["asc", "desc"] = "desc"


class SemanticQuery(BaseModel):
    metrics: list[str] = Field(min_length=1, description="Metric names from the semantic layer")
    dimensions: list[str] = Field(default_factory=list, description="Dimensions to group by")
    filters: list[SemanticFilter] = Field(default_factory=list)
    time_grain: Literal["none", "day", "week", "month", "quarter", "year"] = "none"
    time_dimension: str | None = Field(default=None, description="Date dimension for the grain/range (default: the metric entity's date)")
    date_from: str | None = Field(default=None, description="Inclusive start date YYYY-MM-DD")
    date_to: str | None = Field(default=None, description="Inclusive end date YYYY-MM-DD")
    order_by: list[SemanticOrder] = Field(default_factory=list)
    limit: int = Field(default=100, ge=1, le=MAX_ROWS)


def _iso(v: str | None) -> date | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v)[:10]).date()
    except ValueError:
        return None


def validate_semantic(q: SemanticQuery, layer: dict[str, Any]) -> tuple[SemanticQuery, list[str], list[str]]:
    """Resolve synonyms and check every name, type and join path. Returns (normalised query, errors, warnings)."""
    errors: list[str] = []
    warnings: list[str] = []
    metrics = []
    for m in q.metrics:
        r = resolve_name(m, layer, "metric")
        if r is None:
            errors.append(f"unknown metric '{m}'")
        elif r not in metrics:
            metrics.append(r)
    dims = []
    for d in q.dimensions:
        r = resolve_name(d, layer, "dimension")
        if r is None:
            errors.append(f"unknown dimension '{d}'")
        elif r not in dims:
            dims.append(r)
    filters = []
    for f in q.filters:
        r = resolve_name(f.dimension, layer, "dimension")
        if r is None:
            errors.append(f"unknown filter dimension '{f.dimension}'")
            continue
        dtype = layer["dimensions"][r][0].get("type", "string")
        if f.op == "between" and len(f.values) != 2:
            errors.append(f"filter on {r}: 'between' needs two values")
        elif f.op not in ("in", "not in", "between") and len(f.values) != 1:
            errors.append(f"filter on {r}: '{f.op}' needs exactly one value")
        for v in f.values:
            if dtype == "number":
                try:
                    float(v)
                except ValueError:
                    errors.append(f"filter on {r}: '{v}' is not a number")
            if dtype in ("date", "timestamp") and _iso(v) is None:
                errors.append(f"filter on {r}: '{v}' is not a date (YYYY-MM-DD)")
        if f.op == "contains" and dtype != "string":
            errors.append(f"filter on {r}: 'contains' only applies to text dimensions")
        filters.append(SemanticFilter(dimension=r, op=f.op, values=f.values))
    td = None
    if q.time_dimension:
        td = resolve_name(q.time_dimension, layer, "dimension")
        if td is None or layer["dimensions"][td][0].get("type") not in ("date", "timestamp"):
            errors.append(f"time_dimension '{q.time_dimension}' is not a date dimension")
            td = None
    for v in (q.date_from, q.date_to):
        if v and _iso(v) is None:
            errors.append(f"date '{v}' is not YYYY-MM-DD")
    if q.time_grain in ("none",) and td and not (q.date_from or q.date_to):
        warnings.append("time_dimension set without a grain or date range; it is ignored")
    ents = sorted({layer["metrics"][m]["entity"] for m in metrics})
    for ent in ents:
        if (q.time_grain != "none" or q.date_from or q.date_to) and not (td and layer["dimensions"][td][0]["entity"] == ent) and ent not in layer["time_dims"]:
            errors.append(f"metrics on '{ent}' have no date dimension for the time grain or range")
        for d in dims + [f.dimension for f in filters] + ([td] if td else []):
            if d is None:
                continue
            owners = [x["entity"] for x in layer["dimensions"][d]]
            if ent in owners:
                continue
            if not any(join_path(layer, ent, o) is not None for o in owners):
                errors.append(f"dimension '{d}' cannot be reached from metric entity '{ent}' without duplicating rows (one-to-many join)")
    if len(ents) > 1 and td:
        warnings.append("time_dimension applies to its own entity only; other metrics use their entity's default date")
    order = []
    names = set(metrics) | set(dims) | {"period"}
    for o in q.order_by:
        f = o.field if o.field in names else (resolve_name(o.field, layer, "metric") or resolve_name(o.field, layer, "dimension"))
        if f in names:
            order.append(SemanticOrder(field=f, direction=o.direction))
        else:
            warnings.append(f"order_by '{o.field}' is not in the result and was dropped")
    norm = SemanticQuery(metrics=metrics or q.metrics, dimensions=dims, filters=filters, time_grain=q.time_grain, time_dimension=td,
                         date_from=q.date_from, date_to=q.date_to, order_by=order, limit=min(q.limit, MAX_ROWS))
    return norm, errors, warnings


def _literal(value: str, dtype: str) -> str:
    if dtype == "number":
        return repr(float(value)) if "." in value or "e" in value.lower() else str(int(float(value)))
    if dtype in ("date", "timestamp"):
        return f"DATE '{_iso(value).isoformat()}'"  # type: ignore[union-attr]
    return "'" + str(value).replace("'", "''") + "'"


def _filter_sql(f: SemanticFilter, expr: str, dtype: str) -> str:
    e = f"CAST({expr} AS DATE)" if dtype == "timestamp" else expr
    if f.op in ("in", "not in"):
        return f"{e} {f.op.upper()} ({', '.join(_literal(v, dtype) for v in f.values)})"
    if f.op == "between":
        return f"{e} BETWEEN {_literal(f.values[0], dtype)} AND {_literal(f.values[1], dtype)}"
    if f.op == "contains":
        pat = f.values[0].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_").replace("'", "''")
        return f"{e} ILIKE '%{pat}%' ESCAPE '\\'"
    return f"{e} {f.op} {_literal(f.values[0], dtype)}"


def _dim_for_entity(layer: dict[str, Any], name: str, ent: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The dimension's definition reachable from `ent` and the join path to it."""
    owners = layer["dimensions"][name]
    own = next((d for d in owners if d["entity"] == ent), None)
    if own:
        return own, []
    best = None
    for d in owners:
        p = join_path(layer, ent, d["entity"])
        if p is not None and (best is None or len(p) < len(best[1])):
            best = (d, p)
    if best is None:
        raise ValueError(f"dimension '{name}' is not reachable from '{ent}'")
    return best


def compile_semantic(q: SemanticQuery, layer: dict[str, Any]) -> str:
    """Deterministic SQL for a validated semantic query (one SELECT per metric entity, joined on the dimensions)."""
    by_entity: dict[str, list[str]] = {}
    for m in q.metrics:
        by_entity.setdefault(layer["metrics"][m]["entity"], []).append(m)
    blocks = []
    for ent, mets in by_entity.items():
        table = layer["entities"][ent].get("table", ent)
        joins: list[dict[str, Any]] = []

        def need(path: list[dict[str, Any]]) -> None:
            for e in path:
                if not any(j["to"] == e["to"] for j in joins):
                    joins.append(e)

        select, group = [], []
        td_name = q.time_dimension if (q.time_dimension and any(d["entity"] == ent for d in layer["dimensions"][q.time_dimension])) else layer["time_dims"].get(ent)
        if q.time_grain != "none" and td_name:
            tdef, path = _dim_for_entity(layer, td_name, ent)
            need(path)
            col = tdef["expr"]
            expr = f"CAST({col} AS DATE)" if q.time_grain == "day" else f"CAST(date_trunc('{q.time_grain}', {col}) AS DATE)"
            select.append(f"{expr} AS period")
            group.append(expr)
        for d in q.dimensions:
            ddef, path = _dim_for_entity(layer, d, ent)
            need(path)
            select.append(f"{ddef['expr']} AS {d}")
            group.append(ddef["expr"])
        select += [f"{layer['metrics'][m]['sql']} AS {m}" for m in mets]
        where = []
        for f in q.filters:
            fdef, path = _dim_for_entity(layer, f.dimension, ent)
            need(path)
            where.append(_filter_sql(f, fdef["expr"], fdef.get("type", "string")))
        if (q.date_from or q.date_to) and td_name:
            tdef, path = _dim_for_entity(layer, td_name, ent)
            need(path)
            col = f"CAST({tdef['expr']} AS DATE)" if tdef.get("type") == "timestamp" else tdef["expr"]
            if q.date_from:
                where.append(f"{col} >= DATE '{_iso(q.date_from).isoformat()}'")  # type: ignore[union-attr]
            if q.date_to:
                where.append(f"{col} <= DATE '{_iso(q.date_to).isoformat()}'")  # type: ignore[union-attr]
        sql = "SELECT " + ",\n       ".join(select) + f"\nFROM {table}"
        for j in joins:
            sql += f"\n{'LEFT JOIN' if j['type'] == 'left' else 'INNER JOIN'} {layer['entities'][j['to']].get('table', j['to'])} ON {j['on']}"
        if where:
            sql += "\nWHERE " + "\n  AND ".join(where)
        if group:
            sql += "\nGROUP BY " + ", ".join(group)
        blocks.append((ent, mets, sql))
    keys = (["period"] if q.time_grain != "none" else []) + list(q.dimensions)
    order = q.order_by or ([SemanticOrder(field="period", direction="asc")] if q.time_grain != "none" else [SemanticOrder(field=q.metrics[0], direction="desc")])
    order_sql = "ORDER BY " + ", ".join(f"{o.field} {o.direction.upper()}" for o in order)
    if len(blocks) == 1:
        return f"{blocks[0][2]}\n{order_sql}\nLIMIT {q.limit}"
    if not keys:  # scalar metrics from several entities: single-row CTEs read through scalar sub-selects (no cross join)
        ctes = ",\n".join(f"s_{ent} AS (\n{sql}\n)" for ent, _, sql in blocks)
        cols = [f"(SELECT s_{ent}.{m} FROM s_{ent}) AS {m}" for ent, mets, _ in blocks for m in mets]
        return f"WITH {ctes}\nSELECT {', '.join(cols)}"
    ctes = ",\n".join(f"m_{ent} AS (\n{sql}\n)" for ent, _, sql in blocks)
    names = [f"m_{ent}" for ent, _, _ in blocks]
    sel_keys = [f"COALESCE({', '.join(f'{n}.{k}' for n in names)}) AS {k}" for k in keys]
    sel_mets = [f"m_{ent}.{m}" for ent, mets, _ in blocks for m in mets]
    sql = f"WITH {ctes}\nSELECT {', '.join(sel_keys + sel_mets)}\nFROM {names[0]}"
    for n in names[1:]:
        sql += f"\nFULL OUTER JOIN {n} ON " + " AND ".join(f"{names[0]}.{k} IS NOT DISTINCT FROM {n}.{k}" for k in keys)
    return f"{sql}\n{order_sql}\nLIMIT {q.limit}"


# ============================================================================ warehouse access

def warehouse_query(wh: Any, sql: str, max_rows: int = MAX_ROWS) -> pd.DataFrame:
    """Run an already validated, read-only SELECT through the warehouse connector (which re-checks it)."""
    return wh.query(sql, max_rows=max_rows)


# ============================================================================ SQL validator (pure, sqlglot)

BLOCKED_FUNCTIONS = {"read_csv", "read_csv_auto", "read_parquet", "read_json", "read_json_auto", "read_text", "read_blob", "glob", "parquet_scan",
                     "sniff_csv", "query_table", "query", "getenv", "current_setting", "pg_read_file", "load_extension", "system", "sqlite_scan",
                     "postgres_scan", "mysql_scan", "iceberg_scan", "delta_scan", "http_get"}


def validate_sql(sql: str, tables: dict[str, list[tuple[str, str]]], max_rows: int = MAX_ROWS, dialect: str = "duckdb") -> dict[str, Any]:
    """Parse and sandbox SQL: one read-only SELECT, known tables/columns, no table functions, no unconstrained cross joins, LIMIT enforced."""
    import sqlglot
    from sqlglot import exp

    res: dict[str, Any] = {"valid": False, "errors": [], "warnings": [], "tables": [], "columns": [], "limit_applied": False, "sql": sql}
    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except Exception as exc:
        res["errors"].append(f"parse error: {truncate(str(exc), 200)}")
        return res
    if len(statements) != 1:
        res["errors"].append(f"exactly one statement is allowed (got {len(statements)})")
        return res
    tree = statements[0]
    if not isinstance(tree, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
        res["errors"].append(f"only SELECT queries are allowed (got {type(tree).__name__.upper()})")
        return res
    forbidden = (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Drop, exp.Create, exp.Alter, exp.Command, exp.Copy, exp.Pragma, exp.Set,
                 exp.Use, exp.Attach, exp.Detach, exp.Install, exp.TruncateTable, exp.Export, exp.Into)
    bad = {type(n).__name__ for n in tree.walk() if isinstance(n, forbidden)}
    if bad:
        res["errors"].append(f"write/DDL constructs are not allowed: {', '.join(sorted(bad))}")
    known = {t.lower(): {c.lower() for c, _ in cols} for t, cols in tables.items()}
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    alias_map: dict[str, str] = {}
    for t in tree.find_all(exp.Table):
        if not isinstance(t.this, exp.Identifier):
            res["errors"].append(f"table functions are not allowed ({type(t.this).__name__})")
            continue
        name = t.name.lower()
        if name in ctes:
            alias_map[(t.alias_or_name or name).lower()] = name
            continue
        if t.args.get("db") or t.args.get("catalog"):
            res["errors"].append(f"qualified table '{t.sql()}' is not allowed")
        if name not in known:
            res["errors"].append(f"unknown table '{t.name}'")
        else:
            res["tables"].append(name)
        alias_map[(t.alias_or_name or name).lower()] = name
    for f in tree.find_all(exp.Func):
        fname = (f.sql_name() if not isinstance(f, exp.Anonymous) else f.name).lower()
        if fname in BLOCKED_FUNCTIONS or (isinstance(f, exp.Anonymous) and f.name.lower() in BLOCKED_FUNCTIONS):
            res["errors"].append(f"function '{fname}' is not allowed")
    for j in tree.find_all(exp.Join):
        on, using = j.args.get("on"), j.args.get("using")
        kind = (j.args.get("kind") or "").upper()
        target = j.this
        if kind == "CROSS" or (on is None and not using) or (on is not None and not list(on.find_all(exp.Column))):
            res["errors"].append(f"unconstrained cross join with {truncate(target.sql(), 60)} - use JOIN ... ON a key")
    select_aliases = {a.alias.lower() for a in tree.find_all(exp.Alias)}
    used_tables = [alias_map.get(a, a) for a in alias_map]
    for c in tree.find_all(exp.Column):
        col = c.name.lower()
        if not col or col == "*" or isinstance(c.this, exp.Star):
            continue
        q = (c.table or "").lower()
        if q:
            tbl = alias_map.get(q, q)
            if tbl in ctes:
                continue
            if tbl not in known:
                res["errors"].append(f"unknown table or alias '{c.table}' in {c.sql()}")
            elif col not in known[tbl]:
                res["errors"].append(f"unknown column '{c.name}' in table '{tbl}'")
            else:
                res["columns"].append(f"{tbl}.{col}")
        else:
            owners = [t for t in used_tables if t in known and col in known[t]]
            if owners:
                res["columns"].append(f"{owners[0]}.{col}")
            elif col not in select_aliases and not any(t in ctes for t in used_tables):
                res["errors"].append(f"unknown column '{c.name}'")
    if res["errors"]:
        return res
    limit = tree.args.get("limit")
    current = None
    try:
        current = int(limit.expression.this) if limit is not None else None
    except Exception:
        current = None
    if current is None or current > max_rows:
        tree = tree.limit(max_rows) if hasattr(tree, "limit") else tree
        res["limit_applied"] = True
        res["warnings"].append(f"LIMIT {max_rows} enforced" + (f" (was {current})" if current else ""))
    res["sql"] = tree.sql(dialect=dialect, pretty=True)
    res["tables"] = sorted(set(res["tables"]))
    res["columns"] = sorted(set(res["columns"]))
    res["valid"] = True
    return res


# ============================================================================ deterministic NL -> semantic (pure)

_GRAIN_WORDS = [(r"\b(daily|per day|by day|each day)\b", "day"), (r"\b(weekly|per week|by week|each week|week over week)\b", "week"),
                (r"\b(monthly|per month|by month|each month|month over month|over time|trend)\b", "month"),
                (r"\b(quarterly|per quarter|by quarter)\b", "quarter"), (r"\b(yearly|annually|per year|by year|year over year)\b", "year")]
_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m} | {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}


def _phrases(layer: dict[str, Any], kind: str) -> list[tuple[str, str]]:
    pool = layer["metrics"] if kind == "metric" else layer["dimensions"]
    out = [(n.replace("_", " "), n) for n in pool]
    out += [(k, v) for k, v in layer["synonyms"].items() if v in pool]
    return sorted(set(out), key=lambda x: -len(x[0]))


def _find(text: str, phrase: str) -> int:
    m = re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", text)
    return m.start() if m else -1


def parse_time_range(text: str, today: date, latest_year: int) -> tuple[str | None, str | None]:
    t = text.lower()
    m = re.search(r"\bbetween (\d{4}-\d{2}-\d{2}) and (\d{4}-\d{2}-\d{2})\b", t)
    if m:
        return m.group(1), m.group(2)
    m = re.search(r"\b(?:since|from|after) (\d{4}-\d{2}-\d{2})\b", t)
    if m:
        return m.group(1), None
    m = re.search(r"\b(?:before|until|up to) (\d{4}-\d{2}-\d{2})\b", t)
    if m:
        return None, m.group(1)
    m = re.search(r"\blast (\d+) (day|week|month)s?\b", t)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        days = n * {"day": 1, "week": 7, "month": 30}[unit]
        return (today - timedelta(days=days)).isoformat(), today.isoformat()
    m = re.search(r"\bq([1-4])\s*(\d{4})?\b", t)
    if m:
        y = int(m.group(2) or latest_year)
        q = int(m.group(1))
        start = date(y, 3 * q - 2, 1)
        end = date(y, 3 * q, calendar.monthrange(y, 3 * q)[1])
        return start.isoformat(), end.isoformat()
    m = re.search(r"\b(?:in|during|for) (" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\.?(?: (\d{4}))?\b", t)
    if m:
        mon = _MONTHS[m.group(1)]
        y = int(m.group(2) or latest_year)
        return date(y, mon, 1).isoformat(), date(y, mon, calendar.monthrange(y, mon)[1]).isoformat()
    m = re.search(r"\b(?:in|during|for|since) (20\d{2})\b", t)
    if m:
        y = int(m.group(1))
        return date(y, 1, 1).isoformat(), (date(y, 12, 31).isoformat() if not re.search(r"\bsince\b", t) else None)
    return None, None


def nl_to_semantic(question: str, layer: dict[str, Any], values: dict[str, list[str]] | None = None, today: date | None = None,
                   latest_year: int | None = None) -> tuple[SemanticQuery, list[str]]:
    """Token / synonym matching against metric and dimension names (offline path and fallback for invalid model output).

    Returns the query and notes about parts of the question the semantic layer cannot serve (dropped, never guessed).
    """
    text = " " + re.sub(r"[^a-z0-9%\-/ ]+", " ", question.lower()) + " "
    used = [False] * len(text)

    def take(phrase: str) -> int:
        for m in re.finditer(rf"(?<![a-z0-9]){re.escape(phrase)}(?:e?s)?(?![a-z0-9])", text):
            if not any(used[m.start():m.end()]):
                for i in range(m.start(), m.end()):
                    used[i] = True
                return m.start()
        return -1

    group_markers = [m.end() for m in re.finditer(r"\b(by|per|across|for each|for every|split by|broken down by|grouped by|vs|versus|and|which|each|"
                                                  r"top \d+|bottom \d+|best \d+|worst \d+)\b", text)]

    def after_marker(pos: int) -> bool:  # the phrase follows a grouping word, allowing up to two words in between ("by customer region")
        return any(g <= pos and len(text[g:pos].split()) <= 2 for g in group_markers)

    # longest phrase first across metrics and dimensions, so "sales channel" beats the metric synonym "sales"
    phrases = [(ph, "metric", n) for ph, n in _phrases(layer, "metric")] + [(ph, "dimension", n) for ph, n in _phrases(layer, "dimension")]
    phrases.sort(key=lambda x: (-len(x[0]), x[1]))
    mets: list[tuple[int, str]] = []
    dims: list[tuple[int, str]] = []
    for phrase, kind, name in phrases:
        m = re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?:e?s)?(?![a-z0-9])", text)
        if m is None or any(used[m.start():m.end()]):
            continue
        if kind == "metric" and name not in [n for _, n in mets]:
            mets.append((take(phrase), name))
        elif kind == "dimension" and after_marker(m.start()) and name not in [n for _, n in dims]:
            dims.append((take(phrase), name))
    metric_names = [n for _, n in sorted(mets)]
    if not metric_names:
        ent_counts = {"sessions": "session_count", "visits": "session_count", "quotes": "quote_count", "policies": "policy_count",
                      "customers": "customer_count", "members": "customer_count", "products": "product_count"}
        metric_names = [v for k, v in ent_counts.items() if _find(text, k) >= 0 and v in layer["metrics"]][:1] or \
                       [next(iter(layer["metrics"]))]
    metric_entities = {layer["metrics"][m]["entity"] for m in metric_names if m in layer["metrics"]}

    def reachability(dim: str) -> int:  # 0 = own entity, 1 = reachable without fan-out, 2 = not reachable
        owners = [d["entity"] for d in layer["dimensions"].get(dim, [])]
        if metric_entities & set(owners):
            return 0
        if all(any(join_path(layer, e, o) is not None for o in owners) for e in metric_entities):
            return 1
        return 2

    by_value: dict[str, list[str]] = {}
    for dim, vals in (values or {}).items():
        for v in vals:
            if len(v) >= 3:
                by_value.setdefault(v, []).append(dim)
    filters: list[SemanticFilter] = []
    for v in sorted(by_value, key=len, reverse=True):
        m = re.search(rf"(?<![a-z0-9]){re.escape(v.lower())}(?![a-z0-9])", text)
        if m is None or any(used[m.start():m.end()]):
            continue
        dim = min(by_value[v], key=lambda d: (reachability(d), d))
        take(v.lower())
        f = next((f for f in filters if f.dimension == dim), None)
        if f is None:
            filters.append(SemanticFilter(dimension=dim, op="=", values=[v]))
        else:
            f.op, f.values = "in", f.values + [v]
    grain = "none"
    for pat, g in _GRAIN_WORDS:
        if re.search(pat, text):
            grain = g
            break
    d_from, d_to = parse_time_range(question, today or date.today(), latest_year or (today or date.today()).year)
    limit = 100
    order: list[SemanticOrder] = []
    m = re.search(r"\b(top|bottom|best|worst) (\d+)\b", text)
    if m:
        limit = int(m.group(2))
        order = [SemanticOrder(field=metric_names[0], direction="asc" if m.group(1) in ("bottom", "worst") else "desc")]
    dims_sorted = [n for _, n in sorted(dims) if n not in [f.dimension for f in filters]]
    if grain == "none" and not dims_sorted and re.search(r"\btrend|over time\b", text):
        grain = "month"
    q = SemanticQuery(metrics=metric_names, dimensions=dims_sorted, filters=filters, time_grain=grain, date_from=d_from, date_to=d_to,
                      order_by=order, limit=limit)
    return prune_semantic(q, layer)


def prune_semantic(q: SemanticQuery, layer: dict[str, Any]) -> tuple[SemanticQuery, list[str]]:
    """Drop dimensions / filters / time settings the layer cannot serve for these metrics (instead of failing)."""
    notes: list[str] = []
    for _ in range(6):
        norm, errors, _ = validate_semantic(q, layer)
        if not errors:
            return norm, notes
        changed = False
        for e in errors:
            m = re.search(r"dimension '([^']+)' cannot be reached", e) or re.search(r"unknown (?:filter )?dimension '([^']+)'", e)
            if m:
                bad = m.group(1)
                q = q.model_copy(update={"dimensions": [d for d in q.dimensions if d != bad and resolve_name(d, layer, "dimension") != bad],
                                         "filters": [f for f in q.filters if f.dimension != bad and resolve_name(f.dimension, layer, "dimension") != bad],
                                         "time_dimension": None if q.time_dimension == bad else q.time_dimension})
                notes.append(f"dropped '{bad}': {e}")
                changed = True
            elif "no date dimension" in e:
                q = q.model_copy(update={"time_grain": "none", "date_from": None, "date_to": None})
                notes.append(e)
                changed = True
            elif e.startswith("unknown metric"):
                q = q.model_copy(update={"metrics": [x for x in q.metrics if resolve_name(x, layer, "metric")] or [next(iter(layer["metrics"]))]})
                notes.append(e)
                changed = True
        if not changed:
            break
    norm, _, _ = validate_semantic(q, layer)
    return norm, notes


# ============================================================================ results, chart (pure)


def json_records(df: pd.DataFrame, limit: int = MAX_ROWS) -> list[dict[str, Any]]:
    out = []
    for rec in df.head(limit).to_dict("records"):
        row = {}
        for k, v in rec.items():
            if v is None or (isinstance(v, float) and math.isnan(v)):
                row[k] = None
            elif isinstance(v, (pd.Timestamp, datetime, date)):
                row[k] = v.isoformat()[:10] if (isinstance(v, date) and not isinstance(v, datetime)) or getattr(v, "hour", 0) == 0 else v.isoformat()
            elif hasattr(v, "item"):
                row[k] = v.item()
            else:
                row[k] = v
        out.append(row)
    return out


def partial_periods(periods: list[Any], grain: str, data_min: date | None, data_max: date | None) -> list[str]:
    """Periods (truncated dates) that the data covers only partly - e.g. the current week or month."""
    if grain in ("none", "day") or not periods:
        return []
    def end_of(start: date) -> date:
        if grain == "week":
            return start + timedelta(days=6)
        if grain == "month":
            return date(start.year, start.month, calendar.monthrange(start.year, start.month)[1])
        if grain == "quarter":
            m = start.month + 2
            return date(start.year, m, calendar.monthrange(start.year, m)[1])
        return date(start.year, 12, 31)
    out = []
    for p in sorted({_iso(str(v)) for v in periods if _iso(str(v))}):
        if (data_max and end_of(p) > data_max) or (data_min and p < data_min):
            out.append(p.isoformat())
    return out


def dataset_hints(df: pd.DataFrame, q: SemanticQuery | None) -> dict[str, Any]:
    cols = list(df.columns)
    if q is not None:
        x = "period" if "period" in cols else (q.dimensions[0] if q.dimensions else None)
        group = q.dimensions[0] if ("period" in cols and q.dimensions) else (q.dimensions[1] if len(q.dimensions) > 1 else None)
        return {"x": x, "y": [m for m in q.metrics if m in cols], "group": group}
    num = [c for c in cols if pd.api.types.is_numeric_dtype(df[c])]
    other = [c for c in cols if c not in num]
    return {"x": other[0] if other else None, "y": num, "group": other[1] if len(other) > 1 else None}


def render_chart(df: pd.DataFrame, hints: dict[str, Any], title: str) -> bytes | None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    x, ys, group = hints.get("x"), hints.get("y") or [], hints.get("group")
    if df.empty or not ys:
        return None
    palette = ["#1f6f78", "#f2b33d", "#0e3a4a", "#c0392b", "#7a4e9c", "#55666f", "#2e8b57", "#d35400"]
    fig, ax = plt.subplots(figsize=(9, 4.4), dpi=120)
    y = ys[0]
    if x is None:  # scalar result
        ax.bar(ys, [float(df.iloc[0][c]) for c in ys], color=palette[: len(ys)])
    elif x == "period":
        d = df.copy()
        d[x] = pd.to_datetime(d[x])
        if group:
            top = d.groupby(group)[y].sum().sort_values(ascending=False).head(8).index
            for i, g in enumerate(top):
                s = d[d[group] == g].sort_values(x)
                ax.plot(s[x], s[y], marker="o", markersize=3, linewidth=1.6, color=palette[i % len(palette)], label=str(g))
            ax.legend(frameon=False, fontsize=8, ncols=2)
        else:
            d = d.sort_values(x)
            for i, c in enumerate(ys[:2]):
                axis = ax if i == 0 else ax.twinx()
                axis.plot(d[x], d[c], marker="o", markersize=3, linewidth=1.8, color=palette[i], label=c)
                axis.set_ylabel(c.replace("_", " "))
        ax.set_xlabel("period")
    else:
        d = df.sort_values(y, ascending=False).head(20)
        if group and group in d:
            piv = d.pivot_table(index=x, columns=group, values=y, aggfunc="sum").fillna(0)
            piv.plot(kind="bar", ax=ax, color=palette[: len(piv.columns)], width=0.8)
            ax.legend(frameon=False, fontsize=8)
        else:
            ax.bar(d[x].astype(str), d[y], color=palette[0])
        plt.setp(ax.get_xticklabels(), rotation=30, ha="right")
        ax.set_xlabel(x.replace("_", " "))
    ax.set_ylabel(y.replace("_", " "))
    ax.set_title(truncate(title, 90), fontsize=10)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


def result_facts(df: pd.DataFrame, hints: dict[str, Any]) -> list[str]:
    """Computed facts about the result for the explanation (the model never sees raw tables it could mis-add)."""
    facts = [f"{len(df):,} row(s); columns: {', '.join(df.columns)}"]
    x, ys = hints.get("x"), hints.get("y") or []
    group = hints.get("group")
    for y in ys[:3]:
        if y not in df or not pd.api.types.is_numeric_dtype(df[y]) or df[y].dropna().empty:
            continue
        s = df[y].astype(float)
        integral = bool((s.dropna() % 1 == 0).all())
        fmt = ((lambda v: f"{v:.2%}") if s.abs().max() <= 1.0 and ("rate" in y or "share" in y)
               else (lambda v: f"{v:,.0f}") if integral or s.abs().max() >= 1000 else (lambda v: f"{v:,.2f}"))
        where = lambda r: f"{x} = {r[x]}" + (f", {group} = {r[group]}" if group and group in df else "")
        if x and x in df and len(df) > 1:
            hi, lo = df.loc[s.idxmax()], df.loc[s.idxmin()]
            facts.append(f"{y}: highest {fmt(float(hi[y]))} at {where(hi)}, lowest {fmt(float(lo[y]))} at {where(lo)}")
            if not ("rate" in y or "avg" in y or "per_" in y):
                facts.append(f"{y}: total {fmt(float(s.sum()))} across the rows shown")
            if x == "period" and not hints.get("group") and len(df) >= 2:
                d = df.sort_values(x)
                first, last = float(d[y].iloc[0]), float(d[y].iloc[-1])
                if first:
                    facts.append(f"{y}: {fmt(first)} in the first period ({d[x].iloc[0]}) vs {fmt(last)} in the last ({d[x].iloc[-1]}), {last / first - 1:+.1%}")
        else:
            facts.append(f"{y}: {fmt(float(s.iloc[0]))}")
    return facts


# ============================================================================ contract


class ExplorationInput(BaseModel):
    question: str = Field(min_length=3, description="The analytics question in plain language", json_schema_extra={"ui": "textarea"})
    mode: Literal["semantic", "sql"] = Field(default="semantic", description="semantic = governed metrics; sql = model writes SQL (validated)")
    sql: str | None = Field(default=None, description="Optional SQL to validate and run instead of generating one", json_schema_extra={"ui": "textarea"})
    max_rows: int = Field(default=MAX_ROWS, ge=1, le=MAX_ROWS, description="Row cap")
    chart: bool = Field(default=True, description="Render a chart artifact")


class SqlDraft(BaseModel):
    sql: str = Field(description="One read-only DuckDB SELECT using only the listed tables and columns")
    explanation: str = ""


class ExplorationExplanation(BaseModel):
    explanation: str = Field(description="What the result shows, using only the facts provided")
    follow_up_questions: list[str] = Field(default_factory=list, max_length=4)


class ExplorationOutput(BaseModel):
    question: str
    mode: str
    semantic_query: dict[str, Any] | None
    sql: str
    validation: dict[str, Any]
    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    chart: str | None = None
    explanation: str
    follow_up_questions: list[str] = Field(default_factory=list)
    dataset: list[dict[str, Any]] = Field(default_factory=list, description="Result records for Chart Summary")
    dataset_hints: dict[str, Any] = Field(default_factory=dict)
    query_source: Literal["model", "deterministic", "user_sql"]
    findings: list[Finding] = Field(default_factory=list)


def _generate_or_none(ctx: RunContext, template: str, schema: type[BaseModel], **variables: Any) -> tuple[Any, str | None]:
    if ctx.llm.offline:
        ctx.render(template, **variables)
        return None, "no language model configured: question mapped to the semantic layer by deterministic synonym matching"
    try:
        return ctx.generate(template, schema, **variables), None
    except Exception as exc:
        ctx.log("model call failed", template=template, error=repr(exc))
        return None, f"model call failed ({type(exc).__name__}); deterministic semantic mapping used"


def catalogue_lines(layer: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    mets = [f"{n} [{m['entity']}]: {m.get('description', '')}" for n, m in layer["metrics"].items()]
    dims = []
    for n, defs in layer["dimensions"].items():
        d = defs[0]
        dims.append(f"{n} ({d.get('type', 'string')}, {'/'.join(x['entity'] for x in defs)}): {d.get('description', '')}")
    syn = [f"{k} -> {v}" for k, v in list(layer["synonyms"].items())[:80]]
    return mets, dims, syn


@register
class ExplorationGenerationAgent(BaseAgent):
    spec = AgentSpec(
        id="exploration_generation",
        name="Exploration Generation",
        category=Category.EXPERIMENTATION,
        summary="Answers analytics questions with governed text-to-query over your own warehouse: a semantic layer compiled to validated, "
                "read-only SQL (or sqlglot-checked free SQL), with results, a chart and a dataset for Chart Summary.",
        kind=AgentKind.HYBRID,
        tier=Tier.BALANCED,
        connectors=["warehouse"],
        skills=["semantic_layer"],
        keywords=["explore data", "analytics question", "text to sql", "query", "how many", "by channel", "trend", "breakdown",
                  "segment analysis", "self-service analytics"],
        produces=["query.sql", "results.csv", "chart.png"],
        feeds=["chart_summary", "experiment_ideation"],
    )
    Input = ExplorationInput
    Output = ExplorationOutput
    schemas = {"query": SemanticQuery, "sql": SqlDraft, "explain": ExplorationExplanation}

    def selftest_input(self) -> dict[str, Any]:
        return {"question": "Weekly sessions and conversion rate by device since 2026-06-01"}

    def stages(self):
        return [("context", self.context), ("generate_query", self.generate_query), ("validate", self.validate), ("execute", self.execute),
                ("visualise", self.visualise), ("explain", self.explain)]

    # -- stage 1 ---------------------------------------------------------------------------------
    def context(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        cfg = ctx.skill("semantic_layer", default=None)
        if not cfg:
            raise ValueError("No semantic layer configured (config/skills/semantic_layer.yaml)")
        layer = load_layer(cfg)
        wh = ctx.connector("warehouse")
        tables = wh.tables()
        values: dict[str, list[str]] = {}
        bounds: dict[str, str] = {}
        for name, defs in layer["dimensions"].items():
            d = defs[0]
            if d.get("type") == "string" and not name.endswith("_id") and d["table"] in tables:
                try:
                    v = warehouse_query(wh, f"SELECT DISTINCT {d['expr']} AS v FROM {d['table']} WHERE {d['expr']} IS NOT NULL LIMIT 41", 41)
                    if 0 < len(v) <= 40:
                        values[name] = [str(x) for x in v["v"].tolist()]
                except Exception:
                    continue
        latest = None
        for ent, tdn in layer["time_dims"].items():
            d = layer["dimensions"][tdn][0]
            try:
                b = warehouse_query(wh, f"SELECT MIN({d['expr']}) AS lo, MAX({d['expr']}) AS hi FROM {d['table']}", 1)
                bounds[f"{ent}.{tdn}"] = f"{str(b['lo'].iloc[0])[:10]} to {str(b['hi'].iloc[0])[:10]}"
                hi = str(b["hi"].iloc[0])[:10]
                latest = max(latest or hi, hi)
            except Exception:
                continue
        missing = sorted({e.get("table", n) for n, e in layer["entities"].items()} - set(tables))
        warnings = [f"Semantic layer tables missing from the warehouse: {', '.join(missing)}"] if missing else []
        return {"data": {"layer_cfg": cfg, "tables": {t: list(map(list, cols)) for t, cols in tables.items()}, "values": values, "bounds": bounds,
                         "latest": latest}, "warnings": warnings}

    # -- stage 2 ---------------------------------------------------------------------------------
    def generate_query(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        layer = load_layer(data["layer_cfg"])
        if inp.get("sql"):
            return {"data": {"source": "user_sql", "sql_draft": inp["sql"], "semantic": None}}
        mets, dims, syn = catalogue_lines(layer)
        today = datetime.now().date()
        common = dict(question=inp["question"], metric_lines=mets, dimension_lines=dims, synonym_lines=syn, today=today.isoformat(),
                      bounds=[f"{k}: {v}" for k, v in data["bounds"].items()],
                      value_lines=[f"{k}: {', '.join(v[:15])}" for k, v in data["values"].items()],
                      examples=[f"Q: {e['question']}\n{e['sql'].strip()}" for e in layer["examples"][:5]])
        warnings: list[str] = []
        reason = None
        if inp.get("mode") == "sql":
            table_lines = [f"{t}({', '.join(c for c, _ in cols)})" for t, cols in data["tables"].items()]
            draft, reason = _generate_or_none(ctx, "sql", SqlDraft, table_lines=table_lines, max_rows=inp["max_rows"], **common)
            if draft is not None:
                return {"data": {"source": "model", "sql_draft": draft.sql, "semantic": None}}
            warnings.append("Free-SQL mode needs a model; the question was answered through the semantic layer instead.")
        q, reason2 = _generate_or_none(ctx, "query", SemanticQuery, **common)
        reason = reason or reason2
        source = "model"
        if q is not None:
            norm, errors, _ = validate_semantic(q, layer)
            if errors:
                warnings.append(f"Model query failed validation ({'; '.join(errors[:3])}); deterministic mapping used instead.")
                q = None
        if q is None:
            latest_year = int((data.get("latest") or str(today))[:4])
            q, notes = nl_to_semantic(inp["question"], layer, data["values"], today=today, latest_year=latest_year)
            warnings += [f"Not answerable from the semantic layer and left out: {n}" for n in notes]
            source = "deterministic"
        else:
            q, notes = prune_semantic(q, layer)
            warnings += [f"Adjusted the model query: {n}" for n in notes]
        out: dict[str, Any] = {"data": {"source": source, "semantic": q.model_dump(), "sql_draft": None}, "warnings": warnings}
        if reason:
            out["provenance"] = {"degraded_reasons": [reason]}
        return out

    # -- stage 3 ---------------------------------------------------------------------------------
    def validate(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        layer = load_layer(data["layer_cfg"])
        tables = {t: [tuple(c) for c in cols] for t, cols in data["tables"].items()}
        checks: list[Check] = []
        sem_errors: list[str] = []
        sem_warnings: list[str] = []
        semantic = None
        if data["semantic"] is not None:
            norm, sem_errors, sem_warnings = validate_semantic(SemanticQuery.model_validate(data["semantic"]), layer)
            semantic = norm.model_dump()
            checks.append(Check(rule="exploration.semantic", status="fail" if sem_errors else "pass", severity="high",
                                message="; ".join(sem_errors) if sem_errors else f"Semantic query valid: {', '.join(norm.metrics)}"
                                + (f" by {', '.join(norm.dimensions)}" if norm.dimensions else "") + (f", {norm.time_grain}" if norm.time_grain != "none" else "")))
            if sem_errors:
                raise ValueError("The question could not be mapped to the semantic layer: " + "; ".join(sem_errors))
            sql = compile_semantic(norm, layer)
        else:
            sql = data["sql_draft"]
        v = validate_sql(sql, tables, int(inp["max_rows"]), layer["dialect"])
        if not v["valid"] and data["source"] == "model" and not ctx.llm.offline:
            try:  # one repair pass with the validator's errors
                fixed = ctx.generate("sql", SqlDraft, question=inp["question"], table_lines=[f"{t}({', '.join(c for c, _ in cols)})" for t, cols in tables.items()],
                                     max_rows=inp["max_rows"], errors=v["errors"], previous_sql=sql, metric_lines=[], dimension_lines=[], synonym_lines=[],
                                     today=datetime.now().date().isoformat(), bounds=[], value_lines=[], examples=[])
                v = validate_sql(fixed.sql, tables, int(inp["max_rows"]), layer["dialect"])
            except Exception:
                pass
        v["semantic_errors"] = sem_errors
        v["warnings"] = sem_warnings + v["warnings"]
        checks.append(Check(rule="exploration.sql", status="pass" if v["valid"] else "fail", severity="critical",
                            message=("SQL validated: read-only SELECT on " + ", ".join(v["tables"]) + ("; LIMIT enforced" if v["limit_applied"] else ""))
                            if v["valid"] else "; ".join(v["errors"])))
        if not v["valid"]:
            raise ValueError("Query rejected by the SQL validator: " + "; ".join(v["errors"]))
        return {"data": {"semantic": semantic, "validation": v}, "checks": checks}

    # -- stage 4 ---------------------------------------------------------------------------------
    def execute(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        df = warehouse_query(ctx.connector("warehouse"), data["validation"]["sql"], int(inp["max_rows"]))
        q = SemanticQuery.model_validate(data["semantic"]) if data["semantic"] else None
        hints = dataset_hints(df, q)
        rows = json_records(df, int(inp["max_rows"]))
        warnings = [] if len(df) else ["The query returned no rows."]
        if q is not None and q.time_grain not in ("none", "day") and "period" in df:
            layer = load_layer(data["layer_cfg"])
            lo = hi = None
            for m in q.metrics:  # coverage of the metric entities' date columns, narrowed by the query's date range
                ent = layer["metrics"][m]["entity"]
                span = data["bounds"].get(f"{ent}.{q.time_dimension or layer['time_dims'].get(ent)}") if layer["time_dims"].get(ent) else None
                if span:
                    a, b = (_iso(x.strip()) for x in span.split(" to "))
                    lo = max(lo, a) if lo and a else (a or lo)
                    hi = min(hi, b) if hi and b else (b or hi)
            if q.date_from and _iso(q.date_from):
                lo = max(lo, _iso(q.date_from)) if lo else _iso(q.date_from)
            if q.date_to and _iso(q.date_to):
                hi = min(hi, _iso(q.date_to)) if hi else _iso(q.date_to)
            partial = partial_periods([r["period"] for r in rows], q.time_grain, lo, hi)
            if partial:
                hints["partial_periods"] = partial
                warnings.append(f"Partial {q.time_grain}(s) in the result (data covers them only in part): {', '.join(partial)}.")
        return {"data": {"rows": rows, "columns": [str(c) for c in df.columns], "row_count": int(len(df)), "hints": hints}, "warnings": warnings}

    # -- stage 5 ---------------------------------------------------------------------------------
    def visualise(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        df = pd.DataFrame(data["rows"], columns=data["columns"])
        artifacts = [ctx.artifacts.write_text("query.sql", data["validation"]["sql"] + "\n", "application/sql", "Validated SQL"),
                     ctx.artifacts.write_text("results.csv", df.to_csv(index=False), "text/csv", "Query results")]
        chart = None
        if inp.get("chart", True) and len(df):
            try:
                png = render_chart(df, data["hints"], inp["question"])
                if png:
                    ref = ctx.artifacts.write_bytes("chart.png", png, "image/png", f"Chart: {truncate(inp['question'], 80)}")
                    artifacts.append(ref)
                    chart = ref.path
            except Exception as exc:
                ctx.log("chart failed", error=repr(exc))
        return {"data": {"chart": chart}, "artifacts": artifacts}

    # -- stage 6 ---------------------------------------------------------------------------------
    def explain(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        df = pd.DataFrame(data["rows"], columns=data["columns"])
        facts = result_facts(df, data["hints"])
        out_model, reason = _generate_or_none(ctx, "explain", ExplorationExplanation, question=inp["question"], sql=data["validation"]["sql"], facts=facts)
        if out_model is not None:
            explanation, follow = out_model.explanation, out_model.follow_up_questions[:4]
        else:
            explanation = f"Answer to '{inp['question']}': " + " ".join(f.rstrip(".") + "." for f in facts)
            follow = []
            q = data["semantic"]
            if q:
                if not q["dimensions"]:
                    follow.append(f"How does {q['metrics'][0].replace('_', ' ')} break down by channel or device?")
                if q["time_grain"] == "none":
                    follow.append(f"How has {q['metrics'][0].replace('_', ' ')} changed month by month?")
        out = ExplorationOutput(question=inp["question"], mode=inp.get("mode", "semantic") if data["source"] != "user_sql" else "sql",
                                semantic_query=data["semantic"], sql=data["validation"]["sql"], validation=data["validation"], columns=data["columns"],
                                rows=data["rows"], row_count=data["row_count"], chart=data.get("chart"), explanation=explanation,
                                follow_up_questions=follow, dataset=data["rows"], dataset_hints=data["hints"], query_source=data["source"])
        update: dict[str, Any] = {"output": out.model_dump(mode="json"),
                                  "summary": f"{data['row_count']} row(s) for '{truncate(inp['question'], 70)}' via "
                                             f"{'semantic layer' if data['semantic'] else 'validated SQL'} ({data['source']}); tables: {', '.join(data['validation']['tables'])}"}
        if reason:
            reasons = list(state.get("provenance", {}).get("degraded_reasons", []))
            extra = "no language model: explanation written from computed result facts"
            if extra not in reasons:
                reasons.append(extra)
            update["provenance"] = {"degraded_reasons": reasons}
        return update
