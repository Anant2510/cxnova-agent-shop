"""Chart Summary: statistics before narrative - change points, outliers, trend, seasonality and contributions."""

import base64

import numpy as np
import pandas as pd
import pytest

from caf.agents.experimentation import chart_summary as cs
from caf.agents.experimentation.experiment_summary import unsupported_numbers
from caf.core import get_agent


def _run(ctx, **inp):
    res = get_agent("chart_summary").run(inp, ctx)
    assert res.status != "error", res.error
    return res


def test_weekly_quotes_change_point_and_single_outlier(ctx):
    out = _run(ctx, dataset_path="sample_data/charts/weekly_quotes.csv").output
    assert out["x"] == "week_start" and out["y"][:2] == ["quotes", "purchases"] and out["frequency"] == "weekly"
    q = out["facts"]["series"]["quotes"]
    df = pd.read_csv(ctx.settings.home / "sample_data/charts/weekly_quotes.csv")
    assert [df["week_start"][c["index"]] for c in q["change_points"]] == ["2026-06-15"]
    assert [df["week_start"][o["index"]] for o in q["outliers"]] == ["2026-04-13"]
    assert q["trend"]["direction"] == "up" and q["seasonality"]["detected"] is False
    assert "15 Jun 2026" in out["headline"] and "2,539" in out["headline"]
    assert len(out["alt_text"]) <= 150 and out["long_description"].startswith("Line chart")
    names = {a.name for a in get_agent("chart_summary").run({"dataset_path": "sample_data/charts/weekly_quotes.csv"}, ctx).artifacts}
    assert {"chart.png", "chart_summary.md", "chart_description.html"} <= names


def test_deterministic_narrative_only_uses_computed_numbers(ctx):
    out = _run(ctx, dataset_path="sample_data/charts/weekly_quotes.csv").output
    text = " ".join([out["headline"], out["commentary"], out["long_description"], out["alt_text"]] + out["key_points"])
    import re
    allowed = set(re.findall(r"\d+(?:\.\d+)?", " ".join(out["fact_lines"])))
    assert unsupported_numbers(text, [v for v in cs._numbers(out["facts"])], allowed) == []


def test_change_points_on_synthetic_series():
    rng = np.random.default_rng(0)
    step = np.r_[rng.normal(100, 5, 30), rng.normal(130, 5, 30)]
    cps = cs.change_points(step)
    assert [c["index"] for c in cps] == [30] and cps[0]["shift"] == pytest.approx(30, abs=4)
    assert cs.change_points(rng.normal(100, 5, 60)) == []
    spike = np.r_[rng.normal(100, 5, 40)]
    spike[17] = 400
    f = cs.series_facts(spike, "weekly")
    assert [o["index"] for o in f["outliers"]] == [17] and f["change_points"] == []


def test_seasonality_detected_at_the_right_lag():
    t = np.arange(78)
    y = 1000 + 120 * np.sin(2 * np.pi * t / 13) + np.random.default_rng(1).normal(0, 10, 78)
    s = cs.series_facts(y, "weekly")["seasonality"]
    assert s["detected"] and s["lag"] == 13


def test_grouped_data_contributions(ctx):
    out = _run(ctx, dataset_path="sample_data/charts/monthly_premium.csv").output
    assert out["group"] == "product_line" and out["frequency"] == "monthly"
    c = out["facts"]["contributions"]
    assert sum(c["share_of_total"].values()) == pytest.approx(1.0)
    assert sum(p["change"] for p in c["first_to_last"]) == pytest.approx(c["total_change"])
    assert c["first_to_last"][0]["group"] == "home"


def test_categorical_and_upstream_dataset(ctx):
    records = [{"channel": "Email", "sessions": 900}, {"channel": "Direct", "sessions": 3000}, {"channel": "Display", "sessions": 400}]
    out = _run(ctx, records=records).output
    assert out["x"] == "channel" and out["facts"]["series"]["sessions"]["top"][0]["category"] == "Direct"
    up = {"exploration_generation": {"agent_id": "exploration_generation", "output": {
        "dataset": [{"period": "2026-0%d-01" % m, "session_count": 100 + m} for m in range(1, 10)],
        "dataset_hints": {"x": "period", "y": ["session_count"], "group": None}}}}
    res = get_agent("chart_summary").run({}, ctx, upstream=up)
    assert res.status != "error", res.error
    assert res.output["x"] == "period" and res.output["frequency"] == "monthly"


def test_image_only_input_uses_the_vision_path_offline(ctx):
    img = base64.b64encode(b"\x89PNG\r\n\x1a\nnot-really-a-chart").decode()
    res = _run(ctx, image={"data_base64": img, "mime": "image/png"})
    assert res.output["source"] == "vision" and res.output["is_estimate"]
    assert res.status == "degraded" and res.provenance.get("degraded_reasons")


def test_dataset_path_follows_the_security_policy():
    from caf.core import RunContext
    from caf.core.security import service_settings

    svc = RunContext.create(settings=service_settings())  # REST / MCP / A2A callers
    res = get_agent("chart_summary").run({"dataset_path": "/etc/passwd"}, svc)
    assert res.status == "error" and "not readable here" in res.error
    res = get_agent("chart_summary").run({"dataset_path": ".env.example"}, svc)  # inside the framework, outside file_roots
    assert res.status == "error" and "not readable here" in res.error
    ok = get_agent("chart_summary").run({"dataset_path": "sample_data/analytics/daily.csv", "x": "date", "y": ["sessions"]}, svc)
    assert ok.status in ("ok", "degraded"), ok.error


def test_column_detection_and_frequency():
    df = pd.DataFrame({"month": ["2025-01", "2025-02", "2025-01", "2025-02"], "line": ["a", "a", "b", "b"], "premium": [1, 2, 3, 4]})
    cols = cs.detect_columns(df)
    assert cols == {"x": "month", "y": ["premium"], "group": "line", "is_time": True}
    assert cs.infer_frequency(pd.Series(pd.date_range("2026-01-05", periods=10, freq="7D"))) == "weekly"
    assert abs(cs.mad_z(np.array([1, 1, 1, 1, 50]))[-1]) > cs.OUTLIER_Z


def test_partial_periods_are_excluded_from_the_statistics(ctx):
    rows = [{"week": f"2026-0{m}-0{d}", "quotes": 100 + (d % 3)} for m in (6, 7, 8) for d in (1, 8)] + [{"week": "2026-09-01", "quotes": 12}]
    out = _run(ctx, records=rows, partial_periods=["2026-09-01"]).output
    assert out["facts"]["series"]["quotes"]["outliers"] == [] and out["n_points"] == 6
    assert "excluded incomplete period" in out["fact_lines"][0]
