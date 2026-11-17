"""GA4 Traffic Report: KPIs, significance and anomalies are computed in pandas/scipy/statsmodels, not by the model."""

from datetime import date

import pandas as pd
import pytest

from caf.agents.web.ga4_traffic_report import (
    comparison_window,
    complete_daily,
    detect_anomalies,
    fact_numbers,
    group_events,
    resolve_window,
    robust_z,
    ungrounded_numbers,
)
from caf.core import get_agent

DAILY = "sample_data/analytics/daily.csv"


@pytest.fixture(scope="module")
def august():
    from caf.core import RunContext

    agent = get_agent("ga4_traffic_report")
    result = agent.run(agent.selftest_input(), RunContext.create())
    assert result.status != "error", result.error
    return result


def test_tracking_outage_detected_and_attributed(august):
    anomalies = august.output["anomalies"]
    outage = [a for a in anomalies if a["start"] == "2026-08-19" and a["end"] == "2026-08-20"]
    assert outage, anomalies
    a = outage[0]
    assert a["direction"] == "drop" and a["deviation_pct"] < -0.5
    assert set(a["methods"]) == {"rolling_mad", "stl"}
    assert a["pattern"] == "all_channels"  # every channel fell together -> measurement, not demand
    assert len(anomalies) == 1  # no false alarms elsewhere in August


def test_kpis_match_the_raw_csv(august):
    df = pd.read_csv(DAILY)
    cur = df[(df["date"] >= "2026-08-01") & (df["date"] <= "2026-08-31")]
    prev = df[(df["date"] >= "2026-07-01") & (df["date"] <= "2026-07-31")]
    k = {x["metric"]: x for x in august.output["kpis"]}
    assert k["sessions"]["current"] == cur["sessions"].sum()
    assert k["sessions"]["previous"] == prev["sessions"].sum()
    assert k["sessions"]["change_pct"] == pytest.approx(cur["sessions"].sum() / prev["sessions"].sum() - 1)
    assert k["conversion_rate"]["current"] == pytest.approx(cur["conversions"].sum() / cur["sessions"].sum())
    assert k["engagement_rate"]["current"] == pytest.approx(cur["engaged_sessions"].sum() / cur["sessions"].sum())
    assert k["sessions"]["test"].startswith("Mann-Whitney") and k["conversion_rate"]["test"] == "two-proportion z-test"
    # excluding the outage days shrinks the apparent decline
    assert k["sessions"]["adjusted_change_pct"] > k["sessions"]["change_pct"]


def test_report_contract(august):
    out = august.output
    assert out["period"] == {"start": "2026-08-01", "end": "2026-08-31", "days": 31}
    assert out["comparison"]["start"] == "2026-07-01"
    assert abs(sum(c["share"] for c in out["channels"]) - 1) < 1e-6
    assert out["top_pages"][0]["sessions"] >= out["top_pages"][-1]["sessions"]
    assert out["narrative_source"] == "computed"
    assert any("Traffic drop" in f["title"] for f in out["findings"])
    html = next(a for a in august.artifacts if a.name == "traffic_report.html")
    from caf.config import get_settings

    text = (get_settings().runs_dir / html.path).read_text()
    assert "data:image/png;base64," in text and "2026-08-19" in text


def test_campaign_spike_detected_in_june(ctx):
    r = get_agent("ga4_traffic_report").run({"start_date": "2026-06-01", "end_date": "2026-06-30"}, ctx)
    spikes = [a for a in r.output["anomalies"] if a["direction"] == "spike"]
    assert spikes and spikes[0]["start"] == "2026-06-10" and spikes[0]["end"] == "2026-06-14"


def test_detector_has_no_false_alarms_on_regular_weeks():
    full, missing = complete_daily(pd.read_csv(DAILY), date(2026, 3, 1), date(2026, 9, 30))
    assert missing == []
    flags = detect_anomalies(full, ["sessions", "users", "conversions", "revenue"])
    days = sorted(set(flags["date"]))
    assert all(date(2026, 6, 10) <= d <= date(2026, 6, 14) or d in (date(2026, 8, 19), date(2026, 8, 20)) for d in days), days
    events = group_events(flags, date(2026, 3, 1), date(2026, 9, 30))
    assert [(e["start"], e["direction"]) for e in events] == [("2026-06-10", "spike"), ("2026-08-19", "drop")]


def test_windows():
    s, e, notes = resolve_window(None, None, (date(2026, 3, 1), date(2026, 9, 30)), date(2026, 10, 18))
    assert (s, e) == (date(2026, 9, 1), date(2026, 9, 30)) and notes
    s, e, _ = resolve_window(None, None, None, date(2026, 10, 18))  # live providers: yesterday
    assert e == date(2026, 10, 17) and (e - s).days == 29
    assert comparison_window(date(2026, 8, 1), date(2026, 8, 31), "previous_period") == (date(2026, 7, 1), date(2026, 7, 31))
    assert comparison_window(date(2026, 8, 1), date(2026, 8, 31), "previous_year") == (date(2025, 8, 2), date(2025, 9, 1))
    with pytest.raises(ValueError):
        resolve_window(date(2026, 9, 2), date(2026, 9, 1), None, date(2026, 10, 18))


def test_missing_days_are_reported():
    df = pd.DataFrame({"date": ["2026-01-01", "2026-01-03"], "sessions": [10, 12]})
    full, missing = complete_daily(df, date(2026, 1, 1), date(2026, 1, 3))
    assert missing == ["2026-01-02"] and full["sessions"].tolist() == [10, 0, 12]


def test_robust_z_ignores_outliers():
    s = pd.Series([10, 11, 9, 10, 10, 11, 9, 100.0])
    z = robust_z(s)
    assert z.iloc[-1] > 20 and abs(z.iloc[0]) < 2


def test_number_grounding_guard():
    facts = ["Sessions 116,491: -6.4% vs the previous period", "Conversion rate 2.1%"]
    allowed = fact_numbers(facts)
    assert ungrounded_numbers("Sessions were 116,491 (-6.4%) with a 2.1% conversion rate.", allowed) == []
    assert ungrounded_numbers("Sessions grew 12.5% to 130,000.", allowed) == ["12.5%", "130,000"]


class ScriptedLLM:
    offline = False

    def __init__(self, responses):
        self.responses, self.tracer = responses, None

    def describe(self):
        return {"provider": "scripted", "offline": False, "models": {}}

    def structured(self, prompt, schema, *, system=None, tier="balanced", images=None, meter=None, retries=1):
        if meter is not None:
            meter.add({})
        return schema.model_validate(self.responses[schema.__name__])

    def complete(self, prompt, **kw):
        return ""


def test_online_narrative_numbers_are_checked():
    from caf.core import RunContext

    llm = ScriptedLLM({"TrafficNarrative": {
        "executive_summary": "Sessions were 116,491 (-6.4%), but traffic really grew 42% once you squint.",
        "highlights": ["2026-08-19 to 2026-08-20 was a tracking outage."],
        "actions": [{"title": "Fix tags", "rationale": "All channels fell together.", "priority": "P1"}]}})
    agent = get_agent("ga4_traffic_report")
    r = agent.run(agent.selftest_input(), RunContext.create(llm=llm))
    assert r.output["narrative_source"] == "model"
    assert r.output["ungrounded_numbers"] == ["42%"]
    check = next(c for c in r.checks if c.rule == "narrative.numbers_grounded")
    assert check.status == "warn" and "42%" in check.message
