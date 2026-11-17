"""Exploration Generation: semantic layer compiler, sqlglot sandbox and the deterministic NL parser."""

import pytest

from caf.agents.experimentation import exploration_generation as eg
from caf.core import get_agent


@pytest.fixture()
def layer(ctx):
    return eg.load_layer(ctx.skill("semantic_layer"))


@pytest.fixture()
def wh(ctx):
    return ctx.connector("warehouse")


@pytest.fixture()
def tables(wh):
    return wh.tables()


@pytest.mark.parametrize("sql,needle", [
    ("DELETE FROM sessions", "only SELECT"),
    ("INSERT INTO sessions SELECT * FROM sessions", "only SELECT"),
    ("SELECT 1; DROP TABLE sessions", "exactly one statement"),
    ("SELECT * FROM secrets", "unknown table"),
    ("SELECT sessions.nope FROM sessions", "unknown column"),
    ("SELECT * FROM read_csv('/etc/passwd')", "table functions"),
    ("SELECT sessions.channel FROM sessions, quotes", "cross join"),
    ("SELECT sessions.channel FROM sessions CROSS JOIN quotes", "cross join"),
    ("SELECT sessions.channel FROM sessions JOIN quotes ON TRUE", "cross join"),
    ("SELECT sessions.channel INTO t2 FROM sessions", "not allowed"),
])
def test_validator_rejects_unsafe_sql(tables, sql, needle):
    v = eg.validate_sql(sql, tables)
    assert not v["valid"]
    assert any(needle in e for e in v["errors"]), v["errors"]


def test_validator_accepts_select_and_enforces_limit(tables):
    v = eg.validate_sql("SELECT s.channel, COUNT(*) AS n FROM sessions AS s JOIN quotes q ON q.session_id = s.session_id GROUP BY 1", tables, max_rows=50)
    assert v["valid"] and v["limit_applied"] and "LIMIT 50" in v["sql"]
    assert v["tables"] == ["quotes", "sessions"]
    capped = eg.validate_sql("SELECT sessions.channel FROM sessions LIMIT 100000", tables, max_rows=500)
    assert capped["valid"] and "LIMIT 500" in capped["sql"]
    cte = eg.validate_sql("WITH c AS (SELECT sessions.channel AS ch FROM sessions) SELECT ch FROM c", tables)
    assert cte["valid"]


def test_compiled_semantic_query_matches_hand_written_sql(layer, wh, tables):
    q = eg.SemanticQuery(metrics=["bind rate"], dimensions=["lob"], order_by=[{"field": "product_line", "direction": "asc"}])
    norm, errors, _ = eg.validate_semantic(q, layer)
    assert not errors and norm.metrics == ["quote_to_purchase_rate"] and norm.dimensions == ["product_line"]
    sql = eg.compile_semantic(norm, layer)
    assert "INNER JOIN products ON quotes.product_id = products.product_id" in sql
    v = eg.validate_sql(sql, tables)
    assert v["valid"], v["errors"]
    got = eg.warehouse_query(wh, v["sql"])
    ref = eg.warehouse_query(wh, "SELECT p.product_line, SUM(q.purchased) * 1.0 / COUNT(*) AS r FROM quotes q JOIN products p ON q.product_id = p.product_id "
                                 "GROUP BY 1 ORDER BY 1")
    assert list(got["product_line"]) == list(ref["product_line"])
    assert got["quote_to_purchase_rate"].round(10).tolist() == ref["r"].round(10).tolist()


def test_fan_out_joins_are_refused(layer):
    _, errors, _ = eg.validate_semantic(eg.SemanticQuery(metrics=["conversion_rate"], dimensions=["product_line"]), layer)
    assert any("cannot be reached" in e for e in errors)
    assert eg.join_path(layer, "quotes", "sessions") is not None  # many-to-one is fine
    assert eg.join_path(layer, "sessions", "quotes") is None  # one-to-many would duplicate sessions


def test_metrics_from_two_entities_use_separate_aggregations(layer, wh, tables):
    q, errors, _ = eg.validate_semantic(eg.SemanticQuery(metrics=["quote_count", "session_count"], time_grain="month"), layer)
    sql = eg.compile_semantic(q, layer)
    assert "FULL OUTER JOIN" in sql and not errors
    df = eg.warehouse_query(wh, eg.validate_sql(sql, tables)["sql"])
    assert int(df["quote_count"].sum()) == int(eg.warehouse_query(wh, "SELECT COUNT(*) AS n FROM quotes")["n"].iloc[0])
    assert int(df["session_count"].sum()) == int(eg.warehouse_query(wh, "SELECT COUNT(*) AS n FROM sessions")["n"].iloc[0])


def test_nl_parser_maps_synonyms_dimensions_and_ranges(layer):
    from datetime import date

    q, notes = eg.nl_to_semantic("Top 5 channels by conversion rate", layer)
    assert q.metrics == ["conversion_rate"] and q.dimensions == ["channel"] and q.limit == 5
    q, _ = eg.nl_to_semantic("policy count by sales channel and product line", layer)
    assert q.dimensions == ["sales_channel", "product_line"]
    q, _ = eg.nl_to_semantic("gwp by lob in 2026", layer)
    assert q.metrics == ["gross_written_premium"] and q.dimensions == ["product_line"] and (q.date_from, q.date_to) == ("2026-01-01", "2026-12-31")
    q, _ = eg.nl_to_semantic("traffic by device for Email in Q3 2026", layer, {"channel": ["Email"], "acquisition_channel": ["Email"]},
                             today=date(2026, 10, 18), latest_year=2026)
    assert q.filters[0].dimension == "channel" and (q.date_from, q.date_to) == ("2026-07-01", "2026-09-30")
    q, notes = eg.nl_to_semantic("sessions by product line", layer)
    assert q.dimensions == [] and notes, "unreachable dimension is dropped with a note, never guessed"


def test_agent_returns_dataset_for_chart_summary(ctx):
    res = get_agent("exploration_generation").run({"question": "Quote to purchase rate by product line"}, ctx)
    assert res.status != "error", res.error
    out = res.output
    assert out["row_count"] == 4 and len(out["dataset"]) == 4 and out["dataset_hints"]["x"] == "product_line"
    assert out["validation"]["valid"] and out["chart"]
    assert any(a.name == "chart.png" for a in res.artifacts)


def test_user_sql_is_validated_before_running(ctx):
    agent = get_agent("exploration_generation")
    bad = agent.run({"question": "drop it", "sql": "DROP TABLE sessions"}, ctx)
    assert bad.status == "error" and "only SELECT" in bad.error
    ok = agent.run({"question": "sessions per device", "sql": "SELECT device, COUNT(*) AS n FROM sessions GROUP BY device ORDER BY n DESC"}, ctx)
    assert ok.status != "error" and ok.output["query_source"] == "user_sql" and ok.output["row_count"] == 3


def test_partial_periods_are_flagged():
    from datetime import date

    assert eg.partial_periods(["2026-09-21", "2026-09-28"], "week", date(2026, 3, 1), date(2026, 9, 30)) == ["2026-09-28"]
    assert eg.partial_periods(["2026-02-23", "2026-03-02"], "week", date(2026, 3, 1), date(2026, 9, 30)) == ["2026-02-23"]
    assert eg.partial_periods(["2026-08-01", "2026-09-01"], "month", date(2026, 3, 1), date(2026, 9, 30)) == []
    assert eg.partial_periods(["2026-09-01"], "month", None, date(2026, 9, 15)) == ["2026-09-01"]
