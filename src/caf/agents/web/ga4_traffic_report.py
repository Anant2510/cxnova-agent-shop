"""GA4 Traffic Report - period-over-period web analytics with statistics done in pandas, not by the model.

Independence: there is no Optimizely Connect Platform in the loop. Data comes from the `analytics`
connector (CSV exports by default; GA4 Data API and Adobe Analytics providers return the same DataFrames),
so GA4, Adobe Analytics, Matomo, Piwik PRO or a warehouse table can be swapped by configuration.

Stages: window -> load -> compute -> anomalies -> narrate -> report
  * KPIs period-over-period (sessions, users, new users, conversions, conversion rate, revenue, engagement rate)
    with significance tests (Mann-Whitney U on daily values for volumes, two-proportion z-test for rates)
  * top pages with change, channel attribution (share, CVR, share shift in percentage points)
  * anomaly detection on the daily series with two robust methods: same-weekday rolling median + MAD z-score
    and statsmodels STL (period 7, robust) residual z-scores, with a practical-significance floor; anomaly
    events are attributed across channels (a drop in every channel at once reads as a tracking outage)
  * the model writes only the executive summary and action plan from the computed facts; numbers in its prose
    are checked against those facts
  * HTML report from our own Jinja2 template with matplotlib charts embedded as base64 PNG

Scheduling (fully outside any vendor platform) - e.g. a monthly cron entry on the 1st at 07:00:
    0 7 1 * *  cd /opt/caf && CAF_ANALYTICS_PROVIDER=ga4 python -c "from caf.core import get_agent; \
        get_agent('ga4_traffic_report').run({'compare_to': 'previous_period'})"
or an Airflow task:
    PythonOperator(task_id="ga4_traffic_report", python_callable=lambda: get_agent("ga4_traffic_report").run(
        {"compare_to": "previous_year", "domain": "www.example.com"}))
Artifacts land under runs/<run_id>/ga4_traffic_report/ for distribution by the scheduler.
"""

from __future__ import annotations

import base64
import io
import math
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field, model_validator

from ...core import AgentKind, AgentSpec, BaseAgent, Category, Check, RunContext, Tier, register
from ...core.handoff import Finding

VOLUME_METRICS = ["sessions", "users", "new_users", "conversions", "revenue"]
KPI_LABELS = {"sessions": "Sessions", "users": "Users", "new_users": "New users", "conversions": "Conversions",
              "conversion_rate": "Conversion rate", "revenue": "Revenue", "engagement_rate": "Engagement rate"}
ANOMALY_METRICS = ["sessions", "users", "conversions", "revenue"]
LOOKBACK_DAYS = 56

# chart tokens (reference data-viz palette: slot 1 blue, de-emphasis gray, status critical)
INK, INK2, MUTED, GRID, AXIS, SURFACE = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7", "#fcfcfb"
SERIES, CRITICAL, GOOD = "#2a78d6", "#d03b3b", "#006300"


def degraded_update(state: dict[str, Any], reasons: list[str], **extra: Any) -> dict[str, Any]:
    """Provenance update for one stage (the core's merge_provenance reducer accumulates degraded_reasons)."""
    out: dict[str, Any] = dict(extra)
    if reasons:
        out["degraded_reasons"] = list(dict.fromkeys(reasons))
    return out

# ============================================================================ window + statistics (pure)


def resolve_window(start: date | None, end: date | None, bounds: tuple[date, date] | None, today: date,
                   days: int = 30) -> tuple[date, date, list[str]]:
    notes = []
    if end is None:
        end = bounds[1] if bounds else today - timedelta(days=1)
        notes.append(f"end date defaulted to {end.isoformat()} ({'last day of available data' if bounds else 'yesterday'})")
    if start is None:
        start = end - timedelta(days=days - 1)
        notes.append(f"start date defaulted to {start.isoformat()} (last {days} complete days)")
    if start > end:
        raise ValueError(f"start_date {start} is after end_date {end}")
    return start, end, notes


def comparison_window(start: date, end: date, mode: str) -> tuple[date, date]:
    n = (end - start).days + 1
    if mode == "previous_year":  # 52 weeks back keeps the weekday mix identical
        return start - timedelta(days=364), end - timedelta(days=364)
    return start - timedelta(days=n), start - timedelta(days=1)


DAILY_COLUMNS = ["sessions", "users", "new_users", "engaged_sessions", "conversions", "revenue"]


def complete_daily(df: pd.DataFrame, start: date, end: date) -> tuple[pd.DataFrame, list[str]]:
    """Reindex to every calendar day; missing days become 0 and are reported (a gap is itself a tracking issue).
    The frame always carries the standard metric columns, even when the provider returned nothing."""
    idx = pd.date_range(start, end, freq="D")
    d = df.copy()
    if "date" not in d:
        d["date"] = pd.Series(dtype="datetime64[ns]")
    d["date"] = pd.to_datetime(d["date"])
    for col in DAILY_COLUMNS:
        d[col] = pd.to_numeric(d[col], errors="coerce") if col in d else 0.0
    d = d.groupby("date").sum(numeric_only=True).reindex(idx)
    missing = [x.date().isoformat() for x in d.index[d.isna().all(axis=1)]]
    d = d.fillna(0)
    d.index.name = "date"
    return d, missing


def kpi_totals(d: pd.DataFrame) -> dict[str, float]:
    s = d.sum(numeric_only=True)
    sessions = float(s.get("sessions", 0))
    out = {m: float(s.get(m, 0)) for m in VOLUME_METRICS}
    out["conversion_rate"] = float(s.get("conversions", 0)) / sessions if sessions else 0.0
    out["engagement_rate"] = float(s.get("engaged_sessions", 0)) / sessions if sessions else 0.0
    return out


def pct_change(cur: float, prev: float | None) -> float | None:
    if prev is None or prev == 0 or (isinstance(prev, float) and math.isnan(prev)):
        return None
    return (cur - prev) / prev


def compare_kpis(cur: pd.DataFrame, prev: pd.DataFrame | None, cur_adj: pd.DataFrame | None = None,
                 prev_adj: pd.DataFrame | None = None) -> list[dict[str, Any]]:
    """Totals, change and a significance test per KPI. *_adj frames exclude anomaly days (per-day means compared)."""
    from scipy.stats import mannwhitneyu
    from statsmodels.stats.proportion import proportions_ztest

    ct = kpi_totals(cur)
    pt = kpi_totals(prev) if prev is not None and len(prev) and prev["sessions"].sum() > 0 else None
    rows = []
    for m in ["sessions", "users", "new_users", "conversions", "conversion_rate", "revenue", "engagement_rate"]:
        row: dict[str, Any] = {"metric": m, "label": KPI_LABELS[m], "current": ct[m], "previous": pt[m] if pt else None,
                               "change_abs": (ct[m] - pt[m]) if pt else None, "change_pct": pct_change(ct[m], pt[m] if pt else None),
                               "p_value": None, "significant": None, "test": "", "adjusted_change_pct": None,
                               "kind": "rate" if m.endswith("_rate") else "volume",
                               "note": "sum of daily users (overstates unique users)" if m in ("users", "new_users") else ""}
        if pt:
            try:
                if m.endswith("_rate"):
                    num = "conversions" if m == "conversion_rate" else "engaged_sessions"
                    counts = np.array([cur[num].sum(), prev[num].sum()], dtype=float)
                    nobs = np.array([cur["sessions"].sum(), prev["sessions"].sum()], dtype=float)
                    if nobs.min() > 0:
                        _, p = proportions_ztest(counts, nobs)
                        row.update(p_value=float(p), test="two-proportion z-test")
                elif len(cur) >= 5 and len(prev) >= 5:
                    _, p = mannwhitneyu(cur[m].to_numpy(dtype=float), prev[m].to_numpy(dtype=float), alternative="two-sided")
                    row.update(p_value=float(p), test="Mann-Whitney U on daily values")
            except Exception:
                pass
            if row["p_value"] is not None and not math.isnan(row["p_value"]):
                row["significant"] = row["p_value"] < 0.05
            else:
                row["p_value"] = None
            if cur_adj is not None and prev_adj is not None and len(cur_adj) and len(prev_adj):
                if m.endswith("_rate"):
                    a, b = kpi_totals(cur_adj)[m], kpi_totals(prev_adj)[m]
                else:
                    a, b = cur_adj[m].mean(), prev_adj[m].mean()
                row["adjusted_change_pct"] = pct_change(float(a), float(b))
        rows.append(row)
    return rows


def compare_dimension(cur: pd.DataFrame, prev: pd.DataFrame | None, key: str, value_cols: list[str]) -> pd.DataFrame:
    cur = cur.copy()
    if prev is None or prev.empty:
        prev = pd.DataFrame(columns=[key] + value_cols)
    m = cur.merge(prev[[key] + [c for c in value_cols if c in prev.columns]], on=key, how="outer", suffixes=("", "_prev"))
    for c in value_cols:
        if c not in m:
            m[c] = 0.0
        if f"{c}_prev" not in m:
            m[f"{c}_prev"] = 0.0
        m[c] = pd.to_numeric(m[c], errors="coerce").fillna(0.0)
        m[f"{c}_prev"] = pd.to_numeric(m[f"{c}_prev"], errors="coerce").fillna(0.0)
    m["sessions_change_pct"] = np.where(m["sessions_prev"] > 0, (m["sessions"] - m["sessions_prev"]) / m["sessions_prev"].where(m["sessions_prev"] > 0, 1), np.nan)
    return m


def channel_table(cur: pd.DataFrame, prev: pd.DataFrame | None) -> list[dict[str, Any]]:
    m = compare_dimension(cur, prev, "channel", ["sessions", "users", "conversions", "revenue"])
    ts, tc = m["sessions"].sum(), m["conversions"].sum()
    tsp, tcp = m["sessions_prev"].sum(), m["conversions_prev"].sum()
    rows = []
    for _, r in m.sort_values("sessions", ascending=False).iterrows():
        share = r["sessions"] / ts if ts else 0.0
        share_prev = r["sessions_prev"] / tsp if tsp else None
        cvr = r["conversions"] / r["sessions"] if r["sessions"] else 0.0
        cvr_prev = r["conversions_prev"] / r["sessions_prev"] if r["sessions_prev"] else None
        rows.append({"channel": str(r["channel"]), "sessions": float(r["sessions"]), "sessions_prev": float(r["sessions_prev"]),
                     "sessions_change_pct": None if pd.isna(r["sessions_change_pct"]) else float(r["sessions_change_pct"]),
                     "share": share, "share_prev": share_prev, "share_change_pp": (share - share_prev) * 100 if share_prev is not None else None,
                     "conversions": float(r["conversions"]), "conversion_share": r["conversions"] / tc if tc else 0.0,
                     "conversion_share_prev": r["conversions_prev"] / tcp if tcp else None,
                     "conversion_rate": cvr, "conversion_rate_prev": cvr_prev, "revenue": float(r["revenue"]),
                     "revenue_prev": float(r["revenue_prev"])})
    return rows


def page_table(cur: pd.DataFrame, prev: pd.DataFrame | None, top_n: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Top pages by current sessions with change, plus the biggest movers by absolute session change."""
    cols = ["sessions", "users", "engaged_sessions", "conversions"]
    m = compare_dimension(cur, prev, "page_path", cols)
    if "page_title" not in m:
        m["page_title"] = ""
    m["page_title"] = m["page_title"].fillna("").astype(str)
    m["conversion_rate"] = np.where(m["sessions"] > 0, m["conversions"] / m["sessions"].where(m["sessions"] > 0, 1), 0.0)
    m["engagement_rate"] = np.where(m["sessions"] > 0, m["engaged_sessions"] / m["sessions"].where(m["sessions"] > 0, 1), 0.0)
    m["delta"] = m["sessions"] - m["sessions_prev"]

    def row(r: pd.Series) -> dict[str, Any]:
        status = "new" if r["sessions_prev"] == 0 and r["sessions"] > 0 else ("lost" if r["sessions"] == 0 else "")
        return {"page_path": str(r["page_path"]), "page_title": r["page_title"], "sessions": float(r["sessions"]),
                "sessions_prev": float(r["sessions_prev"]), "sessions_change_pct": None if pd.isna(r["sessions_change_pct"]) else float(r["sessions_change_pct"]),
                "conversions": float(r["conversions"]), "conversion_rate": float(r["conversion_rate"]),
                "engagement_rate": float(r["engagement_rate"]), "delta": float(r["delta"]), "status": status}

    top = [row(r) for _, r in m.sort_values("sessions", ascending=False).head(top_n).iterrows()]
    floor = max(50.0, float(m["sessions_prev"].quantile(0.25))) if len(m) else 50.0
    movers = m[(m["sessions_prev"] >= floor) | (m["sessions"] >= floor)].copy()
    movers["abs_delta"] = movers["delta"].abs()
    return top, [row(r) for _, r in movers.sort_values("abs_delta", ascending=False).head(6).iterrows()]


# ---------------------------------------------------------------------------- anomaly detection


def seasonal_baseline(series: pd.Series, period: int = 7, lags: int = 4) -> pd.Series:
    """Expected value = median of the same weekday over the previous `lags` periods (needs >= 2 of them)."""
    shifted = pd.concat([series.shift(period * k) for k in range(1, lags + 1)], axis=1)
    med = shifted.median(axis=1, skipna=True)
    return med.where(shifted.notna().sum(axis=1) >= 2)


def robust_z(resid: pd.Series) -> pd.Series:
    """(x - median) / (1.4826 * MAD) - a z-score that a few extreme days cannot inflate."""
    r = resid.dropna()
    if r.empty:
        return resid * np.nan
    med = float(r.median())
    mad = float((r - med).abs().median())
    scale = 1.4826 * mad if mad > 0 else (float(r.std()) or 1.0)
    return (resid - med) / scale


def stl_components(series: pd.Series, period: int = 7) -> tuple[pd.Series, pd.Series] | None:
    """Robust STL; returns (fitted, residual) or None when the series is too short."""
    s = series.astype(float)
    if s.notna().sum() < 2 * period + 1:
        return None
    from statsmodels.tsa.seasonal import STL

    s = s.interpolate(limit_direction="both")
    res = STL(s, period=period, robust=True).fit()
    return res.trend + res.seasonal, res.resid


def detect_anomalies(daily: pd.DataFrame, metrics: list[str], *, method: str = "consensus", threshold: float = 3.5,
                     min_change: float = 0.15) -> pd.DataFrame:
    """Day-level anomaly flags for every metric (pure).

    rolling MAD : residual vs same-weekday rolling median, robust z over the whole series
    STL         : robust STL (weekly period) residual, robust z
    method      : consensus (both methods agree - fewest false alarms, the default) | either (more sensitive) | mad | stl
    A day is flagged when the method rule gives |z| >= threshold AND the deviation from the expected value is at
    least `min_change` (practical significance), so noisy low-volume metrics are not over-flagged.
    """
    rows = []
    for m in metrics:
        if m not in daily:
            continue
        y = daily[m].astype(float)
        base = seasonal_baseline(y)
        z_mad = robust_z(y - base)
        stl = stl_components(y)
        z_stl = robust_z(stl[1]) if stl else pd.Series(np.nan, index=y.index)
        expected = base.fillna(stl[0]) if stl else base
        for ts in y.index:
            zm, zs, exp = z_mad.get(ts), z_stl.get(ts), expected.get(ts)
            if exp is None or pd.isna(exp) or exp <= 0:
                continue
            mad_hit = zm is not None and not pd.isna(zm) and abs(zm) >= threshold
            stl_hit = zs is not None and not pd.isna(zs) and abs(zs) >= threshold
            hits = (["rolling_mad"] if mad_hit else []) + (["stl"] if stl_hit else [])
            flagged = {"consensus": mad_hit and (stl_hit or stl is None), "either": mad_hit or stl_hit,
                       "mad": mad_hit, "stl": stl_hit}.get(method, mad_hit and stl_hit)
            dev = (y[ts] - exp) / exp
            if flagged and abs(dev) >= min_change:
                rows.append({"date": ts.date(), "metric": m, "actual": float(y[ts]), "expected": float(exp), "deviation_pct": float(dev),
                             "z_mad": None if pd.isna(zm) else float(zm), "z_stl": None if pd.isna(zs) else float(zs), "methods": hits})
    return pd.DataFrame(rows, columns=["date", "metric", "actual", "expected", "deviation_pct", "z_mad", "z_stl", "methods"])


def group_events(flags: pd.DataFrame, start: date, end: date) -> list[dict[str, Any]]:
    """Merge consecutive flagged days (same direction) inside [start, end] into anomaly events."""
    if flags.empty:
        return []
    f = flags[(flags["date"] >= start) & (flags["date"] <= end)].copy()
    if f.empty:
        return []
    f["direction"] = np.where(f["deviation_pct"] < 0, "drop", "spike")
    days = f.groupby(["date", "direction"]).agg(metrics=("metric", lambda s: sorted(set(s)))).reset_index().sort_values("date")
    events: list[dict[str, Any]] = []
    for _, r in days.iterrows():
        if events and events[-1]["direction"] == r["direction"] and (r["date"] - events[-1]["end"]).days <= 1:
            events[-1]["end"] = r["date"]
            events[-1]["days"].append(r["date"])
            events[-1]["metrics"] = sorted(set(events[-1]["metrics"]) | set(r["metrics"]))
        else:
            events.append({"start": r["date"], "end": r["date"], "direction": r["direction"], "days": [r["date"]], "metrics": list(r["metrics"])})
    out = []
    for e in events:
        sub = f[f["date"].isin(e["days"])]
        sess = sub[sub["metric"] == "sessions"]
        ref = sess if not sess.empty else sub[sub["metric"] == sub["metric"].iloc[0]]
        actual, expected = float(ref["actual"].sum()), float(ref["expected"].sum())
        out.append({"start": e["start"].isoformat(), "end": e["end"].isoformat(), "days": len(e["days"]), "direction": e["direction"],
                    "metrics": e["metrics"], "reference_metric": str(ref["metric"].iloc[0]), "actual": actual, "expected": expected,
                    "deviation_pct": (actual - expected) / expected if expected else 0.0,
                    "max_abs_z": float(max([abs(v) for v in list(sub["z_mad"].dropna()) + list(sub["z_stl"].dropna())] or [0.0])),
                    "methods": sorted({m for ms in sub["methods"] for m in ms})})
    return out


def attribute_event(event: dict[str, Any], channels_daily: pd.DataFrame | None) -> dict[str, Any]:
    """Split an anomaly across channels: does every channel move together (tracking/availability) or one channel?"""
    if channels_daily is None or channels_daily.empty:
        return {"pattern": "unknown", "explanation": "no channel-level daily data", "channels": []}
    cd = channels_daily.copy()
    cd["date"] = pd.to_datetime(cd["date"])
    piv = cd.pivot_table(index="date", columns="channel", values="sessions", aggfunc="sum").sort_index()
    piv = piv.reindex(pd.date_range(piv.index.min(), piv.index.max(), freq="D")).fillna(0)
    days = pd.to_datetime(pd.date_range(event["start"], event["end"], freq="D"))
    rows = []
    for ch in piv.columns:
        base = seasonal_baseline(piv[ch])
        act, exp = float(piv[ch].reindex(days).sum()), float(base.reindex(days).sum())
        if exp > 0:
            rows.append({"channel": str(ch), "actual": act, "expected": exp, "deviation_pct": (act - exp) / exp, "delta": act - exp})
    if not rows:
        return {"pattern": "unknown", "explanation": "not enough channel history", "channels": []}
    rows.sort(key=lambda r: r["delta"])
    devs = np.array([r["deviation_pct"] for r in rows])
    same_dir = np.mean(np.sign(devs) == np.sign(event["deviation_pct"])) if len(devs) else 0
    spread = float(np.std(devs))
    if same_dir >= 0.85 and spread <= 0.15 and abs(event["deviation_pct"]) >= 0.3:
        pattern = "all_channels"
        expl = (f"every channel moved together ({min(devs) * 100:+.0f}% to {max(devs) * 100:+.0f}% vs expected) - "
                + ("consistent with a tracking or site-availability outage rather than a demand change" if event["direction"] == "drop"
                   else "consistent with a site-wide event (bot traffic, tag duplication or PR coverage)"))
    else:
        total = sum(abs(r["delta"]) for r in rows) or 1
        lead = sorted(rows, key=lambda r: -abs(r["delta"]))[:2]
        pattern = "concentrated"
        expl = "driven by " + " and ".join(f"{r['channel']} ({r['deviation_pct'] * 100:+.0f}%, {abs(r['delta']) / total * 100:.0f}% of the change)" for r in lead)
    return {"pattern": pattern, "explanation": expl, "channels": rows}


# ---------------------------------------------------------------------------- insights + grounding


def fmt_num(v: float | None, kind: str = "count") -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "n/a"
    if kind == "pct":
        return f"{v * 100:.1f}%"
    if kind == "money":
        return f"${v:,.0f}"
    return f"{v:,.0f}"


def fmt_change(v: float | None) -> str:
    return "n/a" if v is None else f"{v * 100:+.1f}%"


def deterministic_insights(kpis: list[dict[str, Any]], channels: list[dict[str, Any]], movers: list[dict[str, Any]],
                           events: list[dict[str, Any]], compare_label: str) -> list[str]:
    out = []
    by = {k["metric"]: k for k in kpis}
    for m in ("sessions", "conversions", "revenue"):
        k = by.get(m)
        if k and k["change_pct"] is not None:
            sig = "" if k["significant"] is None else (", statistically significant" if k["significant"] else ", within normal variation")
            adj = f" ({fmt_change(k['adjusted_change_pct'])} excluding anomaly days)" if k.get("adjusted_change_pct") is not None else ""
            out.append(f"{k['label']} {fmt_num(k['current'], 'money' if m == 'revenue' else 'count')}: {fmt_change(k['change_pct'])} vs {compare_label}{sig}{adj}.")
    for m in ("conversion_rate", "engagement_rate"):
        k = by.get(m)
        if k and k["previous"] is not None:
            pp = (k["current"] - k["previous"]) * 100
            sig = "" if k["significant"] is None else (", significant" if k["significant"] else ", not significant")
            out.append(f"{k['label']} {fmt_num(k['current'], 'pct')} ({pp:+.2f} pp{sig}).")
    for e in events:
        span = e["start"] if e["start"] == e["end"] else f"{e['start']} to {e['end']}"
        out.append(f"Anomaly {span}: {e['reference_metric']} {e['deviation_pct'] * 100:+.0f}% vs expected "
                   f"({fmt_num(e['actual'])} vs {fmt_num(e['expected'])}); {e.get('attribution', {}).get('explanation', '')}.".replace("; .", "."))
    shifts = [c for c in channels if c.get("share_change_pp") is not None]
    if shifts:
        up = max(shifts, key=lambda c: c["share_change_pp"])
        down = min(shifts, key=lambda c: c["share_change_pp"])
        if abs(up["share_change_pp"]) >= 0.5:
            out.append(f"{up['channel']} gained share: {up['share'] * 100:.1f}% of sessions ({up['share_change_pp']:+.1f} pp).")
        if abs(down["share_change_pp"]) >= 0.5 and down is not up:
            out.append(f"{down['channel']} lost share: {down['share'] * 100:.1f}% of sessions ({down['share_change_pp']:+.1f} pp).")
    for p in movers[:2]:
        if p["sessions_change_pct"] is not None and abs(p["sessions_change_pct"]) >= 0.1:
            out.append(f"{p['page_path']} sessions {fmt_change(p['sessions_change_pct'])} ({fmt_num(p['sessions_prev'])} to {fmt_num(p['sessions'])}).")
    return out


def deterministic_summary(kpis: list[dict[str, Any]], events: list[dict[str, Any]], channels: list[dict[str, Any]],
                          movers: list[dict[str, Any]], compare_label: str) -> str:
    """Offline executive summary: a synthesis of the computed facts (no model)."""
    by = {k["metric"]: k for k in kpis}
    s, cr = by.get("sessions"), by.get("conversion_rate")
    if not s or not s["current"]:
        return "No traffic data was available for the selected period."
    parts = []
    if s["change_pct"] is None:
        parts.append(f"The site recorded {fmt_num(s['current'])} sessions; no comparison data was available.")
    else:
        verdict = "a statistically significant change" if s["significant"] else "within normal day-to-day variation"
        parts.append(f"Sessions were {fmt_num(s['current'])}, {fmt_change(s['change_pct'])} vs {compare_label} ({verdict}).")
    drops = [e for e in events if e["direction"] == "drop"]
    if drops and s.get("adjusted_change_pct") is not None:
        e = drops[0]
        cause = "a tracking or availability outage" if e.get("attribution", {}).get("pattern") == "all_channels" else "an unexplained drop"
        parts.append(f"{e['start']} to {e['end']} looks like {cause} ({e['deviation_pct'] * 100:+.0f}% vs expected across "
                     f"{'all channels' if e.get('attribution', {}).get('pattern') == 'all_channels' else 'the site'}); excluding anomaly days the change is "
                     f"{fmt_change(s['adjusted_change_pct'])}.")
    for e in [e for e in events if e["direction"] == "spike"][:1]:
        parts.append(f"A spike on {e['start']} to {e['end']} ({e['deviation_pct'] * 100:+.0f}% vs expected) was {e.get('attribution', {}).get('explanation', 'detected')}.")
    if cr and cr["previous"] is not None:
        parts.append(f"Conversion rate was {fmt_num(cr['current'], 'pct')} ({(cr['current'] - cr['previous']) * 100:+.2f} pp, "
                     f"{'significant' if cr['significant'] else 'not significant'}).")
    if movers and movers[0].get("sessions_change_pct") is not None:
        m = movers[0]
        parts.append(f"The largest page movement was {m['page_path']} ({fmt_change(m['sessions_change_pct'])}).")
    return " ".join(parts)


_NUM = re.compile(r"(?<![\w.])[-+]?\$?\d[\d,]*(?:\.\d+)?%?")


def fact_numbers(facts: list[str]) -> set[float]:
    vals: set[float] = set()
    for f in facts:
        for tok in _NUM.findall(f):
            v = _to_float(tok)
            if v is not None:
                vals.add(round(v, 2))
    return vals


def _to_float(tok: str) -> float | None:
    t = tok.replace("$", "").replace(",", "").replace("+", "").rstrip("%")
    try:
        return float(t)
    except ValueError:
        return None


def ungrounded_numbers(text: str, allowed: set[float], tolerance: float = 0.051) -> list[str]:
    """Numbers in model prose that are not (approximately) one of the computed facts. Years and small ordinals are ignored."""
    bad = []
    for tok in _NUM.findall(text or ""):
        v = _to_float(tok)
        if v is None or (1900 <= v <= 2100 and "." not in tok and "%" not in tok) or (abs(v) <= 10 and float(v).is_integer() and "%" not in tok):
            continue
        if not any(abs(v - a) <= max(tolerance, abs(a) * 0.006) for a in allowed):
            bad.append(tok)
    return bad


def deterministic_actions(kpis: list[dict[str, Any]], channels: list[dict[str, Any]], movers: list[dict[str, Any]],
                          events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    acts = []
    for e in events:
        if e["direction"] == "drop" and e.get("attribution", {}).get("pattern") == "all_channels":
            acts.append({"title": f"Investigate the {e['start']} tracking gap and add tag-health monitoring", "priority": "P1",
                         "rationale": f"All channels fell about {abs(e['deviation_pct']) * 100:.0f}% together, which points at measurement rather than demand.",
                         "owner_role": "Analytics engineer", "metric_to_watch": "daily sessions vs expected"})
        elif e["direction"] == "drop":
            acts.append({"title": f"Diagnose the traffic drop on {e['start']}", "priority": "P1", "rationale": e.get("attribution", {}).get("explanation", ""),
                         "owner_role": "Digital marketing lead", "metric_to_watch": e["reference_metric"]})
        else:
            acts.append({"title": f"Learn from the spike on {e['start']}", "priority": "P2", "rationale": e.get("attribution", {}).get("explanation", ""),
                         "owner_role": "Campaign manager", "metric_to_watch": e["reference_metric"]})
    by = {k["metric"]: k for k in kpis}
    cr = by.get("conversion_rate")
    if cr and cr["previous"] is not None and cr["current"] < cr["previous"] and cr.get("significant"):
        acts.append({"title": "Audit the quote funnel for the conversion-rate decline", "priority": "P1",
                     "rationale": f"Conversion rate fell {(cr['current'] - cr['previous']) * 100:.2f} pp and the change is statistically significant.",
                     "owner_role": "CRO lead", "metric_to_watch": "conversion rate"})
    losers = [c for c in channels if (c.get("sessions_change_pct") or 0) < -0.1 and c["share"] >= 0.05]
    for c in losers[:2]:
        acts.append({"title": f"Review {c['channel']} performance", "priority": "P2",
                     "rationale": f"{c['channel']} sessions changed {fmt_change(c['sessions_change_pct'])} vs the comparison period.",
                     "owner_role": "Channel owner", "metric_to_watch": f"{c['channel']} sessions"})
    low = [p for p in movers if (p.get("sessions_change_pct") or 0) < -0.15]
    for p in low[:1]:
        acts.append({"title": f"Recover traffic to {p['page_path']}", "priority": "P2",
                     "rationale": f"Sessions {fmt_change(p['sessions_change_pct'])} vs the comparison period.",
                     "owner_role": "SEO / content lead", "metric_to_watch": f"{p['page_path']} sessions"})
    if not acts:
        acts.append({"title": "Keep the current mix and test one growth lever", "priority": "P3",
                     "rationale": "No significant declines or anomalies were detected.", "owner_role": "Digital marketing lead",
                     "metric_to_watch": "sessions and conversion rate"})
    return acts


# ---------------------------------------------------------------------------- charts


def _style(ax: Any) -> None:
    ax.set_facecolor(SURFACE)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(AXIS)
    ax.tick_params(colors=MUTED, labelsize=9, length=0)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def _png(fig: Any) -> str:
    import matplotlib.pyplot as plt

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=144, facecolor=SURFACE, bbox_inches="tight")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


def chart_daily(cur: pd.DataFrame, expected: pd.Series | None, events: list[dict[str, Any]], title: str) -> str:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(9.6, 3.4))
    fig.patch.set_facecolor(SURFACE)
    _style(ax)
    x = cur.index
    if expected is not None and expected.reindex(x).notna().any():
        ax.plot(x, expected.reindex(x).to_numpy(), color=MUTED, linewidth=1.6, label="Expected (same-weekday median of the prior 4 weeks)",
                solid_capstyle="round")
    ax.plot(x, cur["sessions"].to_numpy(), color=SERIES, linewidth=2, label="Sessions", solid_capstyle="round", solid_joinstyle="round")
    ax.fill_between(x, cur["sessions"].to_numpy(), color=SERIES, alpha=0.08, linewidth=0)
    for e in events:
        days = pd.date_range(e["start"], e["end"], freq="D")
        pts = cur.reindex(days)["sessions"].dropna()
        ax.scatter(pts.index, pts.to_numpy(), s=60, color=CRITICAL, edgecolor=SURFACE, linewidth=2, zorder=5)
        ax.annotate(f"Anomaly {e['deviation_pct'] * 100:+.0f}%", (pts.index[0], pts.iloc[0]), xytext=(8, -18 if e["direction"] == "drop" else 10),
                    textcoords="offset points", color=INK, fontsize=9, fontweight="bold")
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v / 1000:.0f}K" if v >= 1000 else f"{v:.0f}"))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    ax.set_ylim(bottom=0)
    ax.set_title(title, loc="left", color=INK, fontsize=11, fontweight="bold")
    ax.legend(loc="lower left", frameon=False, fontsize=9, labelcolor=INK2)
    return _png(fig)


def chart_bars(labels: list[str], values: list[float], notes: list[str], title: str) -> str:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    n = max(len(labels), 1)
    fig, ax = plt.subplots(figsize=(9.6, 0.42 * n + 0.9))
    fig.patch.set_facecolor(SURFACE)
    _style(ax)
    ax.grid(axis="y", visible=False)
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    y = np.arange(n)[::-1]
    ax.barh(y, values, height=0.5, color=SERIES)
    top = max(values) if values else 1
    for yi, v, note in zip(y, values, notes):
        ax.text(v + top * 0.01, yi, f"{v:,.0f}  {note}", va="center", fontsize=9, color=INK2)
    ax.set_yticks(y)
    ax.set_yticklabels([l if len(l) <= 42 else l[:40] + "..." for l in labels], color=INK, fontsize=9)
    ax.set_xlim(0, top * 1.28)
    ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v / 1000:.0f}K" if v >= 1000 else f"{v:.0f}"))
    ax.set_title(title, loc="left", color=INK, fontsize=11, fontweight="bold")
    return _png(fig)


# ============================================================================ contract


class TrafficInput(BaseModel):
    start_date: date | None = Field(default=None, description="First day of the report (default: 30 complete days ending at end_date)")
    end_date: date | None = Field(default=None, description="Last day (default: last day of available data, or yesterday for live providers)")
    compare_to: Literal["previous_period", "previous_year"] = Field(default="previous_period")
    domain: str | None = Field(default=None, description="Optional hostname filter passed to the analytics provider")
    top_pages: int = Field(default=10, ge=3, le=50)
    anomaly_method: Literal["consensus", "either", "mad", "stl"] = Field(
        default="consensus", description="consensus = rolling-median MAD and STL must agree (fewest false alarms); either = more sensitive")
    anomaly_threshold: float = Field(default=3.5, ge=2.0, le=10.0, description="Robust z-score threshold")
    anomaly_min_change: float = Field(default=0.15, ge=0.0, le=1.0, description="Minimum deviation from expected (practical significance)")
    include_charts: bool = True
    non_conversion_paths: str = Field(default=r"(login|account|sign-?in|register|search|claims|track|help|faq|privacy|terms|cookie|careers|contact)",
                                      description="Regex of utility pages that are not expected to convert (excluded from low-conversion findings)")

    @model_validator(mode="after")
    def _order(self) -> "TrafficInput":
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("start_date must be on or before end_date")
        return self


class Period(BaseModel):
    start: str
    end: str
    days: int


class Kpi(BaseModel):
    metric: str
    label: str
    current: float
    previous: float | None = None
    change_abs: float | None = None
    change_pct: float | None = None
    adjusted_change_pct: float | None = Field(default=None, description="Change in the per-day mean with anomaly days excluded")
    p_value: float | None = None
    significant: bool | None = None
    test: str = ""
    kind: Literal["volume", "rate"] = "volume"
    note: str = ""


class PageRow(BaseModel):
    page_path: str
    page_title: str = ""
    sessions: float
    sessions_prev: float
    sessions_change_pct: float | None = None
    conversions: float
    conversion_rate: float
    engagement_rate: float
    status: str = ""


class ChannelRow(BaseModel):
    channel: str
    sessions: float
    sessions_prev: float
    sessions_change_pct: float | None = None
    share: float
    share_prev: float | None = None
    share_change_pp: float | None = None
    conversions: float
    conversion_share: float
    conversion_rate: float
    conversion_rate_prev: float | None = None
    revenue: float


class Anomaly(BaseModel):
    start: str
    end: str
    days: int
    direction: Literal["drop", "spike"]
    metrics: list[str]
    reference_metric: str
    actual: float
    expected: float
    deviation_pct: float
    max_abs_z: float
    methods: list[str]
    pattern: str = "unknown"
    explanation: str = ""
    in_comparison_period: bool = False


class ActionRow(BaseModel):
    title: str
    rationale: str = ""
    priority: Literal["P1", "P2", "P3"] = "P2"
    owner_role: str = ""
    metric_to_watch: str = ""


class TrafficOutput(BaseModel):
    provider: str
    domain: str | None = None
    period: Period
    comparison: Period
    compare_to: str
    kpis: list[Kpi]
    top_pages: list[PageRow]
    movers: list[PageRow]
    channels: list[ChannelRow]
    anomalies: list[Anomaly]
    comparison_anomalies: list[Anomaly]
    data_quality: list[str]
    insights: list[str]
    executive_summary: str
    highlights: list[str]
    action_plan: list[ActionRow]
    narrative_source: Literal["model", "computed"]
    ungrounded_numbers: list[str] = Field(default_factory=list, description="Numbers in the model's prose not found in the computed facts")
    findings: list[Finding]


class NarrativeAction(BaseModel):
    title: str
    rationale: str = ""
    priority: Literal["P1", "P2", "P3"] = "P2"
    owner_role: str = ""
    metric_to_watch: str = ""


class TrafficNarrative(BaseModel):
    executive_summary: str
    highlights: list[str] = Field(default_factory=list, max_length=6)
    actions: list[NarrativeAction] = Field(default_factory=list, max_length=8)


REPORT_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Traffic report {{ period.start }} to {{ period.end }}</title>
<style>
:root{--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;--line:#e1e0d9;--bg:#f9f9f7;--card:#fcfcfb;--good:#006300;--bad:#d03b3b;--accent:#184f95}
*{box-sizing:border-box}body{margin:0;font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;color:var(--ink);background:var(--bg)}
header{background:var(--accent);color:#fff;padding:28px 32px 70px}header h1{margin:0 0 4px;font-size:24px}header p{margin:0;opacity:.85}
main{max-width:1120px;margin:-50px auto 0;padding:0 20px 48px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:18px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}.tile .v{font-size:24px;font-weight:600}
.tile .l{color:var(--ink2);font-size:13px}.d{font-size:13px;font-weight:600}.up{color:var(--good)}.down{color:var(--bad)}.flat{color:var(--ink2)}
.sig{display:block;font-size:11px;color:var(--muted);font-weight:400}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:20px 22px;margin-bottom:18px}
h2{font-size:18px;margin:0 0 12px}table{width:100%;border-collapse:collapse;font-size:14px;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:7px 9px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--ink2);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.03em}
td.n,th.n{text-align:right}img{max-width:100%;height:auto;display:block;margin:6px 0}
.pill{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;font-weight:600;color:#fff}.P1{background:var(--bad)}.P2{background:#b54708}.P3{background:#475467}
.alert{border-left:4px solid var(--bad);padding:8px 12px;background:#fdf2f2;margin:8px 0;border-radius:4px}
.muted{color:var(--muted)}.note{font-size:13px;color:var(--ink2)}code{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12.5px}
</style></head><body>
<header><h1>Web traffic report</h1><p>{{ domain }} &middot; {{ period.start }} to {{ period.end }} ({{ period.days }} days) vs {{ compare_label }} {{ comparison.start }} to {{ comparison.end }} &middot; source: {{ provider }}</p></header>
<main>
<div class="tiles">{% for k in kpis %}<div class="tile"><div class="l">{{ k.label }}</div><div class="v">{{ k.value }}</div>
<div class="d {{ k.css }}">{{ k.delta }}<span class="sig">{{ k.sig }}</span></div></div>{% endfor %}</div>
<section><h2>Executive summary</h2><p>{{ executive_summary }}</p>
{% if highlights %}<ul>{% for h in highlights %}<li>{{ h }}</li>{% endfor %}</ul>{% endif %}
<p class="note">Narrative: {{ narrative_label }}. Every number above was computed in pandas/scipy/statsmodels.</p></section>
{% if anomalies %}<section><h2>Anomalies detected</h2>{% for a in anomalies %}<div class="alert"><b>{{ a.span }}</b> - {{ a.reference_metric }} {{ a.dev }} vs expected
({{ a.actual }} vs {{ a.expected }}; robust |z| up to {{ a.z }}; methods: {{ a.methods }}).<br>{{ a.explanation }}</div>{% endfor %}</section>{% endif %}
{% if chart_daily %}<section><h2>Daily sessions</h2><img alt="Daily sessions for the report period{{ ' with anomalies marked' if anomalies else '' }}" src="data:image/png;base64,{{ chart_daily }}"></section>{% endif %}
<section><h2>Prioritised action plan</h2><table><thead><tr><th>Priority</th><th>Action</th><th>Owner</th><th>Metric to watch</th></tr></thead><tbody>
{% for a in actions %}<tr><td><span class="pill {{ a.priority }}">{{ a.priority }}</span></td><td><b>{{ a.title }}</b><br><span class="muted">{{ a.rationale }}</span></td><td>{{ a.owner_role }}</td><td>{{ a.metric_to_watch }}</td></tr>{% endfor %}
</tbody></table></section>
<section><h2>Channel attribution</h2>{% if chart_channels %}<img alt="Sessions by channel" src="data:image/png;base64,{{ chart_channels }}">{% endif %}
<table><thead><tr><th>Channel</th><th class="n">Sessions</th><th class="n">Change</th><th class="n">Share</th><th class="n">Share shift</th><th class="n">Conv.</th><th class="n">Conv. share</th><th class="n">CVR</th><th class="n">Revenue</th></tr></thead><tbody>
{% for c in channels %}<tr><td>{{ c.channel }}</td><td class="n">{{ c.sessions }}</td><td class="n">{{ c.change }}</td><td class="n">{{ c.share }}</td><td class="n">{{ c.shift }}</td><td class="n">{{ c.conversions }}</td><td class="n">{{ c.conv_share }}</td><td class="n">{{ c.cvr }}</td><td class="n">{{ c.revenue }}</td></tr>{% endfor %}
</tbody></table></section>
<section><h2>Top pages</h2>{% if chart_pages %}<img alt="Top pages by sessions" src="data:image/png;base64,{{ chart_pages }}">{% endif %}
<table><thead><tr><th>Page</th><th class="n">Sessions</th><th class="n">Change</th><th class="n">Engagement</th><th class="n">Conv.</th><th class="n">CVR</th></tr></thead><tbody>
{% for p in pages %}<tr><td><code>{{ p.page_path }}</code><br><span class="muted">{{ p.page_title }}</span></td><td class="n">{{ p.sessions }}</td><td class="n">{{ p.change }}</td><td class="n">{{ p.er }}</td><td class="n">{{ p.conversions }}</td><td class="n">{{ p.cvr }}</td></tr>{% endfor %}
</tbody></table></section>
<section><h2>Data quality and method</h2><ul>{% for q in data_quality %}<li>{{ q }}</li>{% endfor %}
<li>Volumes compared with a Mann-Whitney U test on daily values; rates with a two-proportion z-test (alpha 0.05).</li>
<li>Anomalies: same-weekday rolling median with a MAD-based robust z-score and robust STL (weekly period) residuals, combined by the '{{ method }}' rule; threshold |z| &ge; {{ threshold }} and at least {{ min_change }} deviation from expected.</li>
{% if comparison_anomalies %}<li>The comparison period itself contains anomalies ({{ comparison_anomalies }}); period-over-period changes are also shown excluding anomaly days.</li>{% endif %}
</ul></section>
</main></body></html>"""


def render_report(context: dict[str, Any]) -> str:
    import jinja2

    env = jinja2.Environment(autoescape=True, trim_blocks=True, lstrip_blocks=True)
    return env.from_string(REPORT_TEMPLATE).render(**context)


# ============================================================================ the agent


@register
class Ga4TrafficReportAgent(BaseAgent):
    spec = AgentSpec(
        id="ga4_traffic_report",
        name="GA4 Traffic Report",
        category=Category.WEB,
        summary="Executive-ready traffic report from any analytics connector (GA4, Adobe Analytics, CSV): period-over-period KPIs with "
                "significance tests, top pages, channel attribution and robust anomaly detection computed in code, narrated by the model.",
        kind=AgentKind.HYBRID,
        tier=Tier.PRO,
        connectors=["analytics"],
        skills=["brand_voice"],
        keywords=["traffic report", "traffic drop", "traffic", "ga4", "google analytics", "adobe analytics", "web analytics", "sessions", "channel attribution",
                  "period over period", "anomaly", "monthly report"],
        produces=["traffic_report.html", "daily_sessions.png", "kpis.csv"],
        feeds=["experiment_ideation", "chart_summary", "exploration_generation"],
    )
    Input = TrafficInput
    Output = TrafficOutput
    schemas = {"summary": TrafficNarrative}

    def selftest_input(self) -> dict[str, Any]:
        return {"start_date": "2026-08-01", "end_date": "2026-08-31", "compare_to": "previous_period"}

    def stages(self):
        return [("window", self.window), ("load", self.load), ("compute", self.compute), ("anomalies", self.anomalies),
                ("narrate", self.narrate), ("report", self.report)]

    # -- 1. window -------------------------------------------------------------------------------
    def window(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp = state["inputs"]
        conn = ctx.connector("analytics")
        try:
            bounds = conn.date_bounds()
        except Exception as exc:
            bounds = None
            ctx.log("date_bounds unavailable", error=repr(exc))
        sd = date.fromisoformat(inp["start_date"]) if inp.get("start_date") else None
        ed = date.fromisoformat(inp["end_date"]) if inp.get("end_date") else None
        start, end, notes = resolve_window(sd, ed, bounds, datetime.now(timezone.utc).date())
        warnings, degraded = [], []
        if bounds and (start < bounds[0] or end > bounds[1]):
            if start > bounds[1] or end < bounds[0]:
                warnings.append(f"No analytics data in the requested period: the connector covers {bounds[0]} to {bounds[1]}.")
                degraded.append("no analytics data for the requested period")
            else:  # clip to the data instead of reporting days without data as zeros
                start, end = max(start, bounds[0]), min(end, bounds[1])
                warnings.append(f"Requested period extends beyond the available data ({bounds[0]} to {bounds[1]}); report clipped to {start} to {end}.")
        cs, ce = comparison_window(start, end, inp.get("compare_to", "previous_period"))
        if bounds:
            if cs < bounds[0]:
                warnings.append(f"Comparison period starts {cs}, before the first day of data ({bounds[0]}); comparisons may be partial.")
        return {"data": {"start": start.isoformat(), "end": end.isoformat(), "cmp_start": cs.isoformat(), "cmp_end": ce.isoformat(),
                         "bounds": [b.isoformat() for b in bounds] if bounds else None, "window_notes": notes,
                         "provider": conn.provider}, "warnings": warnings, "provenance": degraded_update(state, degraded)}

    # -- 2. load ---------------------------------------------------------------------------------
    def load(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        conn = ctx.connector("analytics")
        start, end = date.fromisoformat(data["start"]), date.fromisoformat(data["end"])
        cs, ce = date.fromisoformat(data["cmp_start"]), date.fromisoformat(data["cmp_end"])
        domain = inp.get("domain")
        hist_start = min(cs, start) - timedelta(days=LOOKBACK_DAYS)
        degraded, warnings = [], []
        try:
            daily_all = conn.daily(hist_start, end, domain=domain)
        except TypeError:  # providers without the domain kwarg
            daily_all = conn.daily(hist_start, end)
        frames = {"daily": daily_all.assign(date=pd.to_datetime(daily_all["date"]).dt.date.astype(str)).to_dict("records") if len(daily_all) else []}

        def call(name: str, s: date, e: date, **kw: Any) -> pd.DataFrame:
            fn = getattr(conn, name)
            try:
                return fn(s, e, domain=domain, **kw)
            except TypeError:
                return fn(s, e, **kw)

        pages_cur, pages_prev = call("pages", start, end, limit=500), call("pages", cs, ce, limit=500)
        ch_cur, ch_prev = call("channels", start, end), call("channels", cs, ce)
        frames.update(pages_cur=pages_cur.to_dict("records"), pages_prev=pages_prev.to_dict("records"),
                      channels_cur=ch_cur.to_dict("records"), channels_prev=ch_prev.to_dict("records"))
        if hasattr(conn, "channels_daily"):
            try:
                chd = conn.channels_daily(hist_start, end)
                frames["channels_daily"] = chd.assign(date=pd.to_datetime(chd["date"]).dt.date.astype(str)).to_dict("records")
            except Exception as exc:
                degraded.append(f"channel-level daily data unavailable ({type(exc).__name__}): anomalies not attributed to channels")
        else:
            degraded.append("provider has no channels_daily(): anomalies not attributed to channels")
        if not frames["daily"]:
            warnings.append("The analytics connector returned no daily rows for this period.")
            degraded.append("no analytics data for the period")
        return {"data": {"frames": frames}, "warnings": warnings, "provenance": degraded_update(state, degraded, evidence_count=len(frames["daily"]))}

    # -- 3. compute ------------------------------------------------------------------------------
    def compute(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        f = data["frames"]
        start, end = date.fromisoformat(data["start"]), date.fromisoformat(data["end"])
        cs, ce = date.fromisoformat(data["cmp_start"]), date.fromisoformat(data["cmp_end"])
        daily = pd.DataFrame(f["daily"]) if f["daily"] else pd.DataFrame(columns=["date", "sessions", "users", "new_users", "engaged_sessions", "conversions", "revenue"])
        cur, miss_cur = complete_daily(daily, start, end)
        prev, miss_prev = complete_daily(daily, cs, ce)
        bounds = data.get("bounds")
        dq = list(data.get("window_notes", []))
        if bounds:  # days outside the data range are "not available", not "missing"
            lo, hi = date.fromisoformat(bounds[0]), date.fromisoformat(bounds[1])
            miss_prev = [d for d in miss_prev if lo <= date.fromisoformat(d) <= hi]
            miss_cur = [d for d in miss_cur if lo <= date.fromisoformat(d) <= hi]
            prev_available = cs >= lo
        else:
            prev_available = prev["sessions"].sum() > 0
        if miss_cur:
            dq.append(f"{len(miss_cur)} day(s) with no data in the report period: {', '.join(miss_cur[:6])}")
        if miss_prev:
            dq.append(f"{len(miss_prev)} day(s) with no data in the comparison period: {', '.join(miss_prev[:6])}")
        degraded = []
        if not prev_available or prev["sessions"].sum() == 0:
            dq.append("No comparison data available: changes are not computed.")
            degraded.append("no analytics data for the comparison period")
            prev_used = None
        else:
            prev_used = prev
        channels = channel_table(pd.DataFrame(f["channels_cur"]) if f["channels_cur"] else pd.DataFrame(columns=["channel", "sessions", "users", "conversions", "revenue"]),
                                 pd.DataFrame(f["channels_prev"]) if (f["channels_prev"] and prev_used is not None) else None)
        top, movers = page_table(pd.DataFrame(f["pages_cur"]) if f["pages_cur"] else pd.DataFrame(columns=["page_path", "page_title", "sessions", "users", "engaged_sessions", "conversions"]),
                                 pd.DataFrame(f["pages_prev"]) if (f["pages_prev"] and prev_used is not None) else None, int(inp.get("top_pages", 10)))
        return {"data": {"cur": cur.reset_index().assign(date=lambda x: x["date"].dt.date.astype(str)).to_dict("records"),
                         "prev": prev_used.reset_index().assign(date=lambda x: x["date"].dt.date.astype(str)).to_dict("records") if prev_used is not None else None,
                         "channels": channels, "top_pages": top, "movers": movers if prev_used is not None else [], "data_quality": dq},
                "provenance": degraded_update(state, degraded)}

    # -- 4. anomalies ----------------------------------------------------------------------------
    def anomalies(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        f = data["frames"]
        start, end = date.fromisoformat(data["start"]), date.fromisoformat(data["end"])
        cs, ce = date.fromisoformat(data["cmp_start"]), date.fromisoformat(data["cmp_end"])
        daily = pd.DataFrame(f["daily"])
        checks: list[Check] = []
        if daily.empty:
            return {"data": {"events": [], "cmp_events": [], "kpis": compare_kpis(self._frame(data["cur"]), None)}}
        lo = date.fromisoformat(data["bounds"][0]) if data.get("bounds") else pd.to_datetime(daily["date"]).min().date()
        full, _ = complete_daily(daily, max(lo, min(cs, start) - timedelta(days=LOOKBACK_DAYS)), end)
        flags = detect_anomalies(full, ANOMALY_METRICS, method=inp.get("anomaly_method", "consensus"),
                                 threshold=float(inp.get("anomaly_threshold", 3.5)), min_change=float(inp.get("anomaly_min_change", 0.15)))
        chd = pd.DataFrame(f["channels_daily"]) if f.get("channels_daily") else None
        events = group_events(flags, start, end)
        cmp_events = group_events(flags, cs, ce) if data.get("prev") else []
        for e in events + cmp_events:
            e["attribution"] = attribute_event(e, chd)
        for e in events:
            checks.append(Check(rule="traffic.anomaly", status="warn", severity="high" if abs(e["deviation_pct"]) >= 0.3 else "medium",
                                message=f"{e['direction'].capitalize()} {e['start']} to {e['end']}: {e['reference_metric']} {e['deviation_pct'] * 100:+.0f}% vs expected; {e['attribution']['explanation']}",
                                target=e["reference_metric"], data={"max_abs_z": round(e["max_abs_z"], 2), "methods": e["methods"]}))
        if not events:
            checks.append(Check(rule="traffic.anomaly", status="pass", severity="low", message="No anomalies in the report period."))
        # KPIs, plus an anomaly-adjusted view (anomaly days excluded from both periods)
        cur, prev = self._frame(data["cur"]), self._frame(data["prev"]) if data.get("prev") else None
        bad_days = {d for e in events + cmp_events for d in pd.date_range(e["start"], e["end"], freq="D")}
        cur_adj = cur[~cur.index.isin(bad_days)] if bad_days else None
        prev_adj = prev[~prev.index.isin(bad_days)] if (bad_days and prev is not None) else None
        kpis = compare_kpis(cur, prev, cur_adj, prev_adj)
        for k in kpis:
            if k["change_pct"] is not None and k["significant"] and k["change_pct"] < -0.05 and k["metric"] in ("sessions", "conversions", "revenue", "conversion_rate"):
                checks.append(Check(rule=f"traffic.kpi.{k['metric']}", status="warn", severity="medium",
                                    message=f"{k['label']} {fmt_change(k['change_pct'])} vs comparison period ({k['test']}, p={k['p_value']:.3g})."))
        return {"data": {"events": events, "cmp_events": cmp_events, "kpis": kpis}, "checks": checks}

    @staticmethod
    def _frame(records: list[dict[str, Any]] | None) -> pd.DataFrame:
        df = pd.DataFrame(records or [])
        if df.empty:
            return pd.DataFrame(columns=["sessions", "users", "new_users", "engaged_sessions", "conversions", "revenue"])
        df["date"] = pd.to_datetime(df["date"])
        return df.set_index("date")

    # -- 5. narrate (model) ----------------------------------------------------------------------
    def narrate(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        compare_label = "the previous year" if inp.get("compare_to") == "previous_year" else "the previous period"
        insights = deterministic_insights(data["kpis"], data["channels"], data["movers"], data["events"], compare_label)
        kpi_lines = []
        for k in data["kpis"]:
            kind = "pct" if k["kind"] == "rate" else ("money" if k["metric"] == "revenue" else "count")
            line = f"{k['label']}: {fmt_num(k['current'], kind)}"
            if k["previous"] is not None:
                line += f" (previous {fmt_num(k['previous'], kind)}, change {fmt_change(k['change_pct'])}"
                if k["p_value"] is not None:
                    line += f", {'significant' if k['significant'] else 'not significant'} p={k['p_value']:.3f}"
                if k["adjusted_change_pct"] is not None:
                    line += f", {fmt_change(k['adjusted_change_pct'])} excluding anomaly days"
                line += ")"
            kpi_lines.append(line)
        channel_lines = [f"{c['channel']}: {fmt_num(c['sessions'])} sessions ({c['share'] * 100:.1f}% share"
                         + (f", {c['share_change_pp']:+.1f} pp" if c["share_change_pp"] is not None else "")
                         + f"), {fmt_num(c['conversions'])} conversions, CVR {c['conversion_rate'] * 100:.2f}%"
                         + (f", sessions {fmt_change(c['sessions_change_pct'])}" if c["sessions_change_pct"] is not None else "") for c in data["channels"]]
        page_lines = [f"{p['page_path']}: {fmt_num(p['sessions'])} sessions" + (f" ({fmt_change(p['sessions_change_pct'])})" if p["sessions_change_pct"] is not None else "")
                      + f", CVR {p['conversion_rate'] * 100:.2f}%" for p in data["top_pages"]]
        anomaly_lines = [f"{e['start']} to {e['end']}: {e['reference_metric']} {e['deviation_pct'] * 100:+.0f}% vs expected "
                         f"({fmt_num(e['actual'])} vs {fmt_num(e['expected'])}); {e['attribution']['explanation']}" for e in data["events"]]
        narrative = ctx.generate("summary", TrafficNarrative, domain=inp.get("domain") or "the website", start=data["start"], end=data["end"],
                                 compare_label=compare_label, cmp_start=data["cmp_start"], cmp_end=data["cmp_end"], kpis=kpi_lines,
                                 channels=channel_lines, pages=page_lines, anomalies=anomaly_lines, insights=insights,
                                 data_quality=data["data_quality"])
        det_actions = deterministic_actions(data["kpis"], data["channels"], data["movers"], data["events"])
        checks: list[Check] = []
        bad: list[str] = []
        if not ctx.llm.offline:
            allowed = fact_numbers(kpi_lines + channel_lines + page_lines + anomaly_lines + insights + data["data_quality"]
                                   + [data["start"], data["end"], data["cmp_start"], data["cmp_end"]])
            prose = " ".join([narrative.executive_summary] + narrative.highlights + [a.rationale for a in narrative.actions])
            bad = sorted(set(ungrounded_numbers(prose, allowed)))
            checks.append(Check(rule="narrative.numbers_grounded", status="warn" if bad else "pass", severity="medium",
                                message=f"Numbers not found in the computed facts: {', '.join(bad)}" if bad else "Every number in the narrative matches a computed fact."))
            summary = narrative.executive_summary.strip() or " ".join(insights[:3])
            highlights = [h for h in narrative.highlights if h.strip()] or insights[:5]
            actions = [a.model_dump() for a in narrative.actions if a.title.strip()] or det_actions
            source = "model"
        else:
            summary = deterministic_summary(data["kpis"], data["events"], data["channels"], data["movers"], compare_label)
            highlights, actions, source = insights[:6], det_actions, "computed"
        return {"data": {"insights": insights, "executive_summary": summary, "highlights": highlights, "actions": actions,
                         "narrative_source": source, "ungrounded": bad, "compare_label": compare_label}, "checks": checks}

    # -- 6. report -------------------------------------------------------------------------------
    def report(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        cur = self._frame(data["cur"])
        start, end = data["start"], data["end"]
        days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
        cdays = (date.fromisoformat(data["cmp_end"]) - date.fromisoformat(data["cmp_start"])).days + 1

        def anomaly_model(e: dict[str, Any], in_cmp: bool) -> Anomaly:
            return Anomaly(start=e["start"], end=e["end"], days=e["days"], direction=e["direction"], metrics=e["metrics"],
                           reference_metric=e["reference_metric"], actual=e["actual"], expected=round(e["expected"], 1),
                           deviation_pct=round(e["deviation_pct"], 4), max_abs_z=round(e["max_abs_z"], 2), methods=e["methods"],
                           pattern=e["attribution"]["pattern"], explanation=e["attribution"]["explanation"], in_comparison_period=in_cmp)

        anomalies = [anomaly_model(e, False) for e in data["events"]]
        cmp_anoms = [anomaly_model(e, True) for e in data["cmp_events"]]
        findings = []
        for a in anomalies:
            findings.append(Finding(title=f"Traffic {a.direction} {a.start} to {a.end} ({a.deviation_pct * 100:+.0f}% {a.reference_metric})",
                                    detail=a.explanation, severity="high" if abs(a.deviation_pct) >= 0.3 else "medium", category="analytics",
                                    evidence=f"{a.actual:,.0f} vs {a.expected:,.0f} expected; robust |z| {a.max_abs_z:.1f} ({', '.join(a.methods)})",
                                    recommendation="Check tag firing, consent banner and site availability for those dates; annotate the report." if a.pattern == "all_channels"
                                    else "Review the channels driving the change and campaign calendars for those dates.",
                                    target=a.reference_metric, source_agent=self.spec.id))
        for k in data["kpis"]:
            if k["change_pct"] is not None and k["significant"] and abs(k["change_pct"]) >= 0.05:
                findings.append(Finding(title=f"{k['label']} {fmt_change(k['change_pct'])} vs {data['compare_label']}",
                                        detail=f"{k['test']}, p={k['p_value']:.3g}", severity="medium" if k["change_pct"] < 0 else "low",
                                        category="analytics", evidence=f"{k['current']:,.4g} vs {k['previous']:,.4g}", target=k["metric"], source_agent=self.spec.id))
        utility = re.compile(inp.get("non_conversion_paths") or r"^$", re.I)
        for p in data["top_pages"]:
            if p["sessions"] >= 500 and p["conversion_rate"] < 0.01 and not utility.search(p["page_path"]):
                findings.append(Finding(title=f"High-traffic page with low conversion: {p['page_path']}", severity="medium", category="analytics",
                                        detail=f"{p['sessions']:,.0f} sessions, CVR {p['conversion_rate'] * 100:.2f}%",
                                        evidence=f"engagement rate {p['engagement_rate'] * 100:.0f}%", target=p["page_path"], source_agent=self.spec.id,
                                        recommendation="Test clearer calls to action or next steps on this page."))
        out = TrafficOutput(provider=data["provider"], domain=inp.get("domain"), period=Period(start=start, end=end, days=days),
                            comparison=Period(start=data["cmp_start"], end=data["cmp_end"], days=cdays), compare_to=inp.get("compare_to", "previous_period"),
                            kpis=[Kpi(**k) for k in data["kpis"]], top_pages=[PageRow(**{k: p[k] for k in PageRow.model_fields}) for p in data["top_pages"]],
                            movers=[PageRow(**{k: p[k] for k in PageRow.model_fields}) for p in data["movers"]],
                            channels=[ChannelRow(**{k: c[k] for k in ChannelRow.model_fields}) for c in data["channels"]],
                            anomalies=anomalies, comparison_anomalies=cmp_anoms, data_quality=data["data_quality"], insights=data["insights"],
                            executive_summary=data["executive_summary"], highlights=data["highlights"],
                            action_plan=[ActionRow(**{k: a.get(k, "") for k in ActionRow.model_fields}) for a in data["actions"]],
                            narrative_source=data["narrative_source"], ungrounded_numbers=data["ungrounded"], findings=findings)

        artifacts = []
        charts = {"chart_daily": "", "chart_channels": "", "chart_pages": ""}
        if inp.get("include_charts", True) and len(cur):
            try:
                hist = self._frame(data["frames"]["daily"])["sessions"].astype(float) if data["frames"].get("daily") else None
                expected = seasonal_baseline(hist.asfreq("D").fillna(0)) if hist is not None and len(hist) else None
                charts["chart_daily"] = chart_daily(cur, expected, data["events"], f"Daily sessions, {start} to {end}")
                ch = data["channels"][:8]
                charts["chart_channels"] = chart_bars([c["channel"] for c in ch], [c["sessions"] for c in ch],
                                                      [f"{c['share'] * 100:.0f}% share" + (f", {fmt_change(c['sessions_change_pct'])}" if c["sessions_change_pct"] is not None else "") for c in ch],
                                                      "Sessions by channel")
                pg = data["top_pages"][:10]
                charts["chart_pages"] = chart_bars([p["page_path"] for p in pg], [p["sessions"] for p in pg],
                                                   [fmt_change(p["sessions_change_pct"]) if p["sessions_change_pct"] is not None else "" for p in pg], "Top pages by sessions")
                artifacts.append(ctx.artifacts.write_bytes("daily_sessions.png", base64.b64decode(charts["chart_daily"]), "image/png", "Daily sessions with anomalies"))
            except Exception as exc:  # charts are presentation; never fail the report for them
                ctx.log("chart rendering failed", error=repr(exc))
        kpi_csv = pd.DataFrame([k.model_dump() for k in out.kpis]).to_csv(index=False)
        artifacts.append(ctx.artifacts.write_text("kpis.csv", kpi_csv, "text/csv", "KPI comparison with significance tests"))

        def tile(k: Kpi) -> dict[str, str]:
            kind = "pct" if k.kind == "rate" else ("money" if k.metric == "revenue" else "count")
            if k.change_pct is None:
                return {"label": k.label, "value": fmt_num(k.current, kind), "delta": "no comparison", "css": "flat", "sig": ""}
            up = k.change_pct >= 0
            delta = (f"{'▲' if up else '▼'} {(k.current - k.previous) * 100:+.2f} pp" if k.kind == "rate" else f"{'▲' if up else '▼'} {fmt_change(k.change_pct)}")
            sig = "" if k.significant is None else ("significant" if k.significant else "not significant")
            if k.adjusted_change_pct is not None:
                sig += f" · {fmt_change(k.adjusted_change_pct)} excl. anomalies"
            return {"label": k.label, "value": fmt_num(k.current, kind), "delta": delta, "css": "flat" if abs(k.change_pct) < 0.005 else ("up" if up else "down"), "sig": sig}

        html = render_report({
            "domain": inp.get("domain") or "All traffic", "period": out.period.model_dump(), "comparison": out.comparison.model_dump(),
            "compare_label": data["compare_label"], "provider": out.provider, "kpis": [tile(k) for k in out.kpis],
            "executive_summary": out.executive_summary, "highlights": out.highlights,
            "narrative_label": "written by a language model from the computed facts" + (f" (ungrounded numbers flagged: {', '.join(out.ungrounded_numbers)})" if out.ungrounded_numbers else "")
            if out.narrative_source == "model" else "generated from the computed facts (no model configured)",
            "anomalies": [{"span": a.start if a.start == a.end else f"{a.start} to {a.end}", "reference_metric": a.reference_metric,
                           "dev": f"{a.deviation_pct * 100:+.0f}%", "actual": f"{a.actual:,.0f}", "expected": f"{a.expected:,.0f}",
                           "z": f"{a.max_abs_z:.1f}", "methods": ", ".join(a.methods), "explanation": a.explanation} for a in anomalies],
            "actions": [a.model_dump() for a in out.action_plan], **charts,
            "channels": [{"channel": c.channel, "sessions": f"{c.sessions:,.0f}", "change": fmt_change(c.sessions_change_pct), "share": f"{c.share * 100:.1f}%",
                          "shift": "n/a" if c.share_change_pp is None else f"{c.share_change_pp:+.1f} pp", "conversions": f"{c.conversions:,.0f}",
                          "conv_share": f"{c.conversion_share * 100:.1f}%", "cvr": f"{c.conversion_rate * 100:.2f}%", "revenue": f"${c.revenue:,.0f}"} for c in out.channels],
            "pages": [{"page_path": p.page_path, "page_title": p.page_title, "sessions": f"{p.sessions:,.0f}", "change": fmt_change(p.sessions_change_pct) if p.status != "new" else "new",
                       "er": f"{p.engagement_rate * 100:.0f}%", "conversions": f"{p.conversions:,.0f}", "cvr": f"{p.conversion_rate * 100:.2f}%"} for p in out.top_pages],
            "data_quality": out.data_quality, "threshold": inp.get("anomaly_threshold", 3.5), "method": inp.get("anomaly_method", "consensus"), "min_change": f"{float(inp.get('anomaly_min_change', 0.15)) * 100:.0f}%",
            "comparison_anomalies": "; ".join(f"{a.start} to {a.end} ({a.deviation_pct * 100:+.0f}%)" for a in cmp_anoms)})
        artifacts.insert(0, ctx.artifacts.write_text("traffic_report.html", html, "text/html", "Executive traffic report"))
        by = {k.metric: k for k in out.kpis}
        s = by["sessions"]
        summary = (f"{start} to {end}: {s.current:,.0f} sessions ({fmt_change(s.change_pct)} vs {data['compare_label']}), "
                   f"CVR {by['conversion_rate'].current * 100:.2f}%, {len(anomalies)} anomaly event(s)"
                   + (f" incl. {anomalies[0].start} to {anomalies[0].end} ({anomalies[0].deviation_pct * 100:+.0f}%)" if anomalies else ""))
        return {"output": out.model_dump(mode="json"), "artifacts": artifacts, "summary": summary}
