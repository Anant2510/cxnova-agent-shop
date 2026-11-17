"""Chart Summary - summarise the data, not the pixels.

Accepts the dataset behind a chart (CSV path, inline CSV, records, or an upstream Exploration Generation dataset),
auto-detects the x / y / group columns and computes the facts BEFORE anything is written: trend (scipy linregress
slope, p-value, % change), seasonality (autocorrelation at plausible lags for the detected frequency), change points
(binary segmentation on the mean with a BIC-style penalty plus a minimum shift), outliers (robust MAD z-score > 3.5
on the residuals of the segment fit) and contributions by group. The model writes report commentary and a
WCAG-compliant long description strictly from those facts (numbers it did not receive are rejected); offline, the
same texts are produced deterministically. The chart is rendered with matplotlib. A vision-model path remains only
for image-only input, clearly labelled as an estimate.
"""

from __future__ import annotations

import base64
import io
import math
import re
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

from ...core import AgentKind, AgentSpec, BaseAgent, Category, Check, RunContext, Tier, register
from ...core.handoff import Finding
from ...core.textutil import truncate
from .experiment_summary import unsupported_numbers

OUTLIER_Z = 3.5

# ============================================================================ column detection (pure)


def _is_dateish(s: pd.Series) -> bool:
    if pd.api.types.is_datetime64_any_dtype(s):
        return True
    if pd.api.types.is_numeric_dtype(s):
        return False
    vals = s.dropna().astype(str).head(50)
    if vals.empty or not vals.str.match(r"^\d{4}[-/]\d{1,2}([-/]\d{1,2})?([ T].*)?$|^\d{1,2}/\d{1,2}/\d{2,4}$").mean() >= 0.9:
        return False
    return pd.to_datetime(vals, errors="coerce").notna().mean() >= 0.9


def detect_columns(df: pd.DataFrame, x: str | None = None, y: list[str] | None = None, group: str | None = None) -> dict[str, Any]:
    """x = first date-like column (else first categorical), y = numeric columns, group = low-cardinality category in long format."""
    cols = list(df.columns)
    if x is None:
        x = next((c for c in cols if _is_dateish(df[c])), None)
        if x is None:
            x = next((c for c in cols if not pd.api.types.is_numeric_dtype(df[c])), None)
    if not y:
        y = [c for c in cols if c not in (x, group) and pd.api.types.is_numeric_dtype(df[c]) and not re.search(r"(^|_)(id|year|week|month|day)$", str(c).lower())]
    if group is None and x is not None:
        for c in cols:
            if c in (x, *y) or pd.api.types.is_numeric_dtype(df[c]):
                continue
            nun = df[c].nunique(dropna=True)
            if 2 <= nun <= 12 and df[x].nunique() < len(df):
                group = c
                break
    is_time = bool(x is not None and _is_dateish(df[x]))
    return {"x": x, "y": list(y), "group": group, "is_time": is_time}


def infer_frequency(dates: pd.Series) -> str:
    d = pd.to_datetime(dates).sort_values().drop_duplicates().diff().dt.days.dropna()
    if d.empty:
        return "unknown"
    med = float(d.median())
    for name, lo, hi in (("daily", 0.5, 1.5), ("weekly", 6, 8), ("monthly", 27, 32), ("quarterly", 85, 95), ("yearly", 360, 370)):
        if lo <= med <= hi:
            return name
    return f"every {med:.0f} days"


SEASONAL_LAGS = {"daily": [7, 14, 28], "weekly": [4, 13, 26, 52], "monthly": [3, 6, 12], "quarterly": [2, 4], "yearly": []}

# ============================================================================ statistics (pure)


def noise_sigma(y: np.ndarray) -> float:
    """Robust noise scale from first differences (insensitive to level shifts and trends)."""
    d = np.diff(np.asarray(y, float))
    if len(d) == 0:
        return 0.0
    mad = float(np.median(np.abs(d - np.median(d))))
    if mad > 0:
        return 1.4826 * mad / math.sqrt(2)
    return float(np.std(d)) / math.sqrt(2)


def _seg_sse(cs: np.ndarray, cs2: np.ndarray, i: int, j: int) -> float:
    n = j - i
    s = cs[j] - cs[i]
    return float(cs2[j] - cs2[i] - s * s / n)


def change_points(y: np.ndarray | list[float], sigma: float | None = None, *, penalty_factor: float = 2.0, min_shift_sigma: float = 3.0,
                  min_size: int = 3) -> list[dict[str, Any]]:
    """Binary segmentation on the mean.

    A split is accepted when the drop in squared error beats a BIC-style penalty (penalty_factor * sigma^2 * ln n)
    and the mean shift is practically meaningful (>= min_shift_sigma * sigma). Returns change points sorted by index
    with the means before/after (within the segment that was split).
    """
    y = np.asarray(y, float)
    n = len(y)
    if n < 2 * min_size:
        return []
    sigma = float(sigma if sigma is not None else noise_sigma(y)) or float(np.std(y)) or 1.0
    penalty = penalty_factor * sigma ** 2 * math.log(n)
    cs = np.concatenate([[0.0], np.cumsum(y)])
    cs2 = np.concatenate([[0.0], np.cumsum(y * y)])
    found: list[dict[str, Any]] = []
    stack = [(0, n)]
    while stack:
        i, j = stack.pop()
        if j - i < 2 * min_size:
            continue
        base = _seg_sse(cs, cs2, i, j)
        best, best_k, best_means = 0.0, None, (0.0, 0.0)
        for k in range(i + min_size, j - min_size + 1):
            left, right = (cs[k] - cs[i]) / (k - i), (cs[j] - cs[k]) / (j - k)
            if abs(right - left) < min_shift_sigma * sigma:
                continue
            gain = base - _seg_sse(cs, cs2, i, k) - _seg_sse(cs, cs2, k, j)
            if gain > best:
                best, best_k, best_means = gain, k, (float(left), float(right))
        if best_k is not None and best > penalty:
            found.append({"index": int(best_k), "mean_before": best_means[0], "mean_after": best_means[1],
                          "shift": best_means[1] - best_means[0], "shift_pct": (best_means[1] / best_means[0] - 1) if best_means[0] else None,
                          "gain": float(best), "penalty": float(penalty), "shift_sigma": abs(best_means[1] - best_means[0]) / sigma})
            stack += [(i, best_k), (best_k, j)]
    return sorted(found, key=lambda c: c["index"])


def segment_fit(y: np.ndarray, cps: list[dict[str, Any]]) -> np.ndarray:
    bounds = [0] + [c["index"] for c in cps] + [len(y)]
    fit = np.empty(len(y))
    for a, b in zip(bounds, bounds[1:]):
        fit[a:b] = float(np.mean(y[a:b]))
    return fit


def mad_z(r: np.ndarray) -> np.ndarray:
    """Robust z-score (Iglewicz & Hoaglin): 0.6745 * (r - median) / MAD."""
    r = np.asarray(r, float)
    med = np.median(r)
    mad = np.median(np.abs(r - med))
    if mad == 0:  # more than half the values identical: use the mean absolute deviation (z = (x - med) / (1.2533 * MeanAD))
        mean_ad = float(np.mean(np.abs(r - med)))
        return (r - med) / (1.253314 * mean_ad) if mean_ad > 0 else np.zeros_like(r)
    return 0.6745 * (r - med) / mad


def series_facts(y: np.ndarray | list[float], frequency: str = "unknown") -> dict[str, Any]:
    """Trend, change points, outliers and seasonality for one series (indices refer to positions in y)."""
    from scipy.stats import linregress

    y = np.asarray(y, float)
    n = len(y)
    out: dict[str, Any] = {"n": n, "first": float(y[0]) if n else None, "last": float(y[-1]) if n else None,
                           "min": float(np.min(y)) if n else None, "max": float(np.max(y)) if n else None,
                           "mean": float(np.mean(y)) if n else None, "total": float(np.sum(y)) if n else None,
                           "argmin": int(np.argmin(y)) if n else None, "argmax": int(np.argmax(y)) if n else None}
    if n < 3:
        out.update(trend=None, change_points=[], outliers=[], seasonality=None, sigma=None)
        return out
    sigma = noise_sigma(y)
    med = pd.Series(y).rolling(5, center=True, min_periods=1).median().to_numpy()
    spikes = np.abs(y - med) > 2 * OUTLIER_Z * max(sigma, 1e-9)  # screen gross spikes before segmenting
    clean = np.where(spikes, med, y)
    cps = change_points(clean, sigma)
    fit = segment_fit(clean, cps)
    z = mad_z(y - fit)
    outliers = [{"index": int(i), "value": float(y[i]), "expected": float(fit[i]), "z": float(z[i])} for i in np.where(np.abs(z) > OUTLIER_Z)[0]]
    keep = np.abs(z) <= OUTLIER_Z
    xs = np.arange(n, dtype=float)
    lr = linregress(xs[keep], y[keep]) if keep.sum() >= 3 else linregress(xs, y)
    f0, f1 = lr.intercept, lr.intercept + lr.slope * (n - 1)
    trend = {"slope_per_period": float(lr.slope), "p_value": float(lr.pvalue), "r_squared": float(lr.rvalue ** 2),
             "fitted_first": float(f0), "fitted_last": float(f1), "pct_change": float(f1 / f0 - 1) if f0 else None,
             "direction": "up" if lr.slope > 0 and lr.pvalue < 0.05 else ("down" if lr.slope < 0 and lr.pvalue < 0.05 else "flat"),
             "significant": bool(lr.pvalue < 0.05), "points_used": int(keep.sum())}
    if cps:  # with level shifts, say how much of the 'trend' is the step change
        trend["within_segment_slopes"] = [float(linregress(xs[a:b][keep[a:b]], y[a:b][keep[a:b]]).slope) if keep[a:b].sum() >= 3 else None
                                          for a, b in zip([0] + [c["index"] for c in cps], [c["index"] for c in cps] + [n])]
    resid = np.where(keep, y, fit) - fit
    if len(cps) == 0:
        resid = np.where(keep, y, fit) - (lr.intercept + lr.slope * xs)
    season = {"lags_tested": [], "acf": {}, "bound": float(1.96 / math.sqrt(n)), "detected": False, "lag": None}
    for lag in SEASONAL_LAGS.get(frequency, [2, 3, 4]):
        if n >= 2 * lag + 2:
            r0 = resid - resid.mean()
            denom = float(np.sum(r0 * r0))
            acf = float(np.sum(r0[lag:] * r0[:-lag]) / denom) if denom > 0 else 0.0
            season["lags_tested"].append(lag)
            season["acf"][str(lag)] = round(acf, 4)
    strongest = max(season["acf"].items(), key=lambda kv: kv[1], default=None)
    if strongest and strongest[1] > max(0.3, season["bound"]):
        season.update(detected=True, lag=int(strongest[0]), strength=strongest[1])
    season["too_short_for"] = [lag for lag in SEASONAL_LAGS.get(frequency, []) if n < 2 * lag + 2]
    out.update(sigma=float(sigma), trend=trend, change_points=cps, outliers=outliers, seasonality=season)
    return out


def contributions(df: pd.DataFrame, x: str, y: str, group: str, cps: list[dict[str, Any]] | None = None, periods: list[Any] | None = None) -> dict[str, Any]:
    """Share of total by group, each group's share of the first-to-last change and of each level shift."""
    piv = df.pivot_table(index=x, columns=group, values=y, aggfunc="sum").sort_index().fillna(0.0)
    total = piv.sum(axis=1)
    share = (piv.sum() / max(float(total.sum()), 1e-12)).sort_values(ascending=False)
    first, last = piv.iloc[0], piv.iloc[-1]
    delta_total = float(total.iloc[-1] - total.iloc[0])
    change = []
    for g in piv.columns:
        d = float(last[g] - first[g])
        change.append({"group": str(g), "first": float(first[g]), "last": float(last[g]), "change": d,
                       "share_of_change": (d / delta_total) if delta_total else None})
    change.sort(key=lambda c: -abs(c["change"]))
    shifts = []
    for c in cps or []:
        k = c["index"]
        if 0 < k < len(piv):
            before, after = piv.iloc[:k].mean(), piv.iloc[k:].mean()
            dt = float(after.sum() - before.sum())
            parts = sorted(({"group": str(g), "change": float(after[g] - before[g]), "share_of_shift": float((after[g] - before[g]) / dt) if dt else None}
                            for g in piv.columns), key=lambda p: -abs(p["change"]))
            shifts.append({"at": str(piv.index[k])[:10], "total_change": dt, "parts": parts})
    return {"share_of_total": {str(k): float(v) for k, v in share.items()}, "first_to_last": change, "total_first": float(total.iloc[0]),
            "total_last": float(total.iloc[-1]), "total_change": delta_total, "shifts": shifts}


def category_facts(df: pd.DataFrame, x: str, y: str) -> dict[str, Any]:
    """Non-time charts: ranking, concentration and outlying categories."""
    s = df.groupby(x)[y].sum().sort_values(ascending=False)
    total = float(s.sum())
    z = mad_z(s.to_numpy()) if len(s) >= 4 else np.zeros(len(s))
    top_n = max(1, int(math.ceil(len(s) * 0.2)))
    return {"n": int(len(s)), "top": [{"category": str(k), "value": float(v), "share": float(v / total) if total else None} for k, v in s.head(5).items()],
            "bottom": [{"category": str(k), "value": float(v)} for k, v in s.tail(3).items()], "total": total,
            "top_20pct_share": float(s.head(top_n).sum() / total) if total else None,
            "outliers": [{"category": str(k), "value": float(v), "z": float(zz)} for (k, v), zz in zip(s.items(), z) if abs(zz) > OUTLIER_Z]}


# ============================================================================ text (pure)


def fmt_num(v: float | None) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "n/a"
    a = abs(v)
    if a >= 1e9:
        return f"{v / 1e9:,.2f} billion"
    if a >= 1e6:
        return f"{v / 1e6:,.1f} million"
    if a >= 100 or float(v).is_integer():
        return f"{v:,.0f}"
    if a >= 10:
        return f"{v:,.1f}"
    return f"{v:,.2f}"


def fmt_p(p: float) -> str:
    return "p < 0.0001" if p < 0.0001 else f"p = {p:.4f}"


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


def _date(v: Any, frequency: str | None = None) -> str:
    try:
        ts = pd.Timestamp(v)
    except Exception:
        return str(v)
    if frequency == "monthly":
        return ts.strftime("%b %Y")
    if frequency == "quarterly":
        return f"Q{(ts.month - 1) // 3 + 1} {ts.year}"
    if frequency == "yearly":
        return str(ts.year)
    return ts.strftime("%-d %b %Y")


def fact_lines(meta: dict[str, Any], facts: dict[str, Any]) -> list[str]:
    lines = [f"Chart: {meta['title']} - {meta['n_points']} {meta['frequency'] if meta['is_time'] else 'categories'} points"
             + (f" from {meta['start']} to {meta['end']}" if meta["is_time"] else "")
             + (f" (excluded incomplete period(s): {', '.join(_date(v, meta.get('frequency')) for v in meta['partial_excluded'])})" if meta.get("partial_excluded") else "")]
    labels = meta["labels"]
    for y, f in facts["series"].items():
        if f.get("trend") is None and "top" not in f:
            continue
        if "top" in f:  # categorical
            lines.append(f"{y}: total {fmt_num(f['total'])}; highest {f['top'][0]['category']} {fmt_num(f['top'][0]['value'])} "
                         f"({f['top'][0]['share']:.0%} of total); lowest {f['bottom'][-1]['category']} {fmt_num(f['bottom'][-1]['value'])}")
            continue
        t = f["trend"]
        lines.append(f"{y}: from {fmt_num(f['first'])} ({labels[0]}) to {fmt_num(f['last'])} ({labels[-1]}); range {fmt_num(f['min'])} "
                     f"({labels[f['argmin']]}) to {fmt_num(f['max'])} ({labels[f['argmax']]}); mean {fmt_num(f['mean'])}")
        lines.append(f"{y} trend: {t['direction']} - slope {fmt_num(t['slope_per_period'])} per period, {fmt_p(t['p_value'])}, "
                     f"R-squared {t['r_squared']:.2f}, fitted change {t['pct_change']:+.1%} over the period" if t.get("pct_change") is not None else
                     f"{y} trend: {t['direction']}")
        for c in f["change_points"]:
            lines.append(f"{y} change point at {labels[c['index']]}: mean {fmt_num(c['mean_before'])} before, {fmt_num(c['mean_after'])} after "
                         f"({c['shift_pct']:+.0%})" if c.get("shift_pct") is not None else f"{y} change point at {labels[c['index']]}")
        for o in f["outliers"]:
            lines.append(f"{y} outlier at {labels[o['index']]}: {fmt_num(o['value'])} vs about {fmt_num(o['expected'])} expected (robust z = {o['z']:.1f})")
        s = f["seasonality"]
        if s and s["lags_tested"]:
            lines.append(f"{y} seasonality: " + (f"repeating pattern every {s['lag']} periods (autocorrelation {s['strength']:.2f})" if s["detected"]
                                                  else f"none detected at lags {', '.join(map(str, s['lags_tested']))}")
                         + (f"; series too short to test lag {', '.join(map(str, s['too_short_for']))}" if s.get("too_short_for") else ""))
    c = facts.get("contributions")
    if c:
        top = c["first_to_last"][:3]
        lines.append(f"Total {fmt_num(c['total_first'])} to {fmt_num(c['total_last'])} ({fmt_num(c['total_change'])}); largest contributions: "
                     + "; ".join(f"{p['group']} {fmt_num(p['change'])}" + (f" ({p['share_of_change']:.0%} of the change)" if p.get("share_of_change") is not None else "")
                                 for p in top))
        lines.append("Share of total: " + ", ".join(f"{g} {v:.0%}" for g, v in list(c["share_of_total"].items())[:6]))
        for sh in c["shifts"]:
            lines.append(f"Shift at {sh['at']}: total {fmt_num(sh['total_change'])}, led by " + ", ".join(f"{p['group']} {fmt_num(p['change'])}" for p in sh["parts"][:3]))
    return lines


class ChartNarrative(BaseModel):
    headline: str = Field(description="One sentence: the main takeaway")
    commentary: str = Field(description="Report commentary (2-4 sentences) using only the computed facts")
    long_description: str = Field(description="WCAG long description: chart type, axes and ranges, overall pattern, notable points, takeaway")
    alt_text: str = Field(description="Short alternative text, at most 150 characters")
    key_points: list[str] = Field(default_factory=list, max_length=5)


def deterministic_narrative(meta: dict[str, Any], facts: dict[str, Any]) -> ChartNarrative:
    labels = meta["labels"]
    y0 = meta["y"][0]
    f = facts["series"][y0]
    nice = y0.replace("_", " ")
    if "top" in f:  # categorical chart
        top, bottom = f["top"][0], f["bottom"][-1]
        headline = f"{top['category']} leads {nice} with {fmt_num(top['value'])} ({top['share']:.0%} of the total)."
        commentary = (f"Across {f['n']} {meta['x'].replace('_', ' ')} values the total is {fmt_num(f['total'])}. {top['category']} is highest and "
                      f"{bottom['category']} lowest ({fmt_num(bottom['value'])}).")
        long = (f"Bar chart of {nice} by {meta['x'].replace('_', ' ')} for {f['n']} categories, sorted from highest to lowest. "
                f"Values range from {fmt_num(bottom['value'])} ({bottom['category']}) to {fmt_num(top['value'])} ({top['category']}). {commentary}")
        return ChartNarrative(headline=headline, commentary=commentary, long_description=long, alt_text=truncate(f"Bar chart: {headline}", 150),
                              key_points=[f"{t['category']}: {fmt_num(t['value'])}" for t in f["top"][:3]])
    t, cps, outs = f["trend"], f["change_points"], f["outliers"]
    parts = []
    if cps:
        c = max(cps, key=lambda c: abs(c["shift"]))
        parts.append(f"{nice} {'stepped up' if c['shift'] > 0 else 'dropped'} from about {fmt_num(c['mean_before'])} to {fmt_num(c['mean_after'])} per "
                     f"{meta['period_word']} from {labels[c['index']]} ({c['shift_pct']:+.0%})")
    elif t and t["direction"] != "flat":
        parts.append(f"{nice} trended {t['direction']} ({t['pct_change']:+.0%} over the period)")
    else:
        parts.append(f"{nice} was broadly flat")
    if outs:
        o = max(outs, key=lambda o: abs(o["z"]))
        parts.append(f"with a one-off {'spike' if o['value'] > o['expected'] else 'dip'} to {fmt_num(o['value'])} in the {meta['period_word']} of {labels[o['index']]}")
    headline = (" ".join(parts[:1]) + (", " + parts[1] if len(parts) > 1 else "") + ".").replace("..", ".")
    headline = headline[0].upper() + headline[1:]
    commentary_bits = [f"Over {meta['n_points']} {meta['period_word']}s ({meta['start']} to {meta['end']}), {nice} moved from {fmt_num(f['first'])} to {fmt_num(f['last'])}."]
    for c in cps:
        commentary_bits.append(f"A change point at {labels[c['index']]} moved the average from {fmt_num(c['mean_before'])} to {fmt_num(c['mean_after'])} "
                               f"({c['shift_pct']:+.0%}), a shift far larger than the {meta['period_word']}-to-{meta['period_word']} noise.")
    for o in outs:
        commentary_bits.append(f"The {meta['period_word']} of {labels[o['index']]} is an outlier at {fmt_num(o['value'])} against about {fmt_num(o['expected'])} "
                               f"expected (robust z {o['z']:.1f}); treat it as a one-off, not a new level.")
    if t:
        commentary_bits.append(f"The linear trend is {t['direction']} ({t['pct_change']:+.0%} fitted change, {fmt_p(t['p_value'])})"
                               + (", mostly explained by the step change." if cps else "."))
    s = f["seasonality"]
    if s and s.get("detected"):
        commentary_bits.append(f"There is a repeating pattern every {s['lag']} {meta['period_word']}s (autocorrelation {s['strength']:.2f}).")
    c = facts.get("contributions")
    if c and c["first_to_last"]:
        lead = c["first_to_last"][0]
        commentary_bits.append(f"{_cap(lead['group'])} contributed most to the change ({fmt_num(lead['change'])}"
                               + (f", {lead['share_of_change']:.0%} of it" if lead.get("share_of_change") is not None else "") + ").")
    commentary = " ".join(commentary_bits)
    other = [y for y in meta["y"][1:] if facts["series"].get(y, {}).get("trend")]
    long = (f"{'Line' if meta['is_time'] else 'Bar'} chart titled '{meta['title']}'. The horizontal axis shows {meta['x'].replace('_', ' ')} from {meta['start']} "
            f"to {meta['end']} ({meta['n_points']} {meta['period_word']}s); the vertical axis shows {nice}, ranging from {fmt_num(f['min'])} "
            f"({labels[f['argmin']]}) to {fmt_num(f['max'])} ({labels[f['argmax']]}). " + " ".join(commentary_bits[1:])
            + (f" The chart also shows {', '.join(o.replace('_', ' ') for o in other)}." if other else ""))
    alt = f"{'Line' if meta['is_time'] else 'Bar'} chart of {nice}, {meta['start']} to {meta['end']}"
    if cps:
        c = max(cps, key=lambda c: abs(c["shift"]))
        alt += f": {'up' if c['shift'] > 0 else 'down'} from about {fmt_num(c['mean_before'])} to {fmt_num(c['mean_after'])} from {labels[c['index']]}"
    elif t:
        alt += f": trend {t['direction']} ({t['pct_change']:+.0%})"
    if outs:
        o = max(outs, key=lambda o: abs(o["z"]))
        alt += f"; {'spike' if o['value'] > o['expected'] else 'dip'} to {fmt_num(o['value'])} on {labels[o['index']]}"
    alt = truncate(alt + ".", 150)
    points = commentary_bits[1:5]
    return ChartNarrative(headline=headline, commentary=commentary, long_description=long, alt_text=alt, key_points=points)


def render_chart(df: pd.DataFrame, meta: dict[str, Any], facts: dict[str, Any]) -> bytes:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    palette = ["#1f6f78", "#c77d0a", "#0e3a4a", "#7a4e9c", "#2e8b57", "#b03a2e", "#55666f", "#d35400"]  # >= 3:1 on white
    fig, ax = plt.subplots(figsize=(9.5, 4.6), dpi=120)
    x, group = meta["x"], meta["group"]
    if meta["is_time"]:
        xs = pd.to_datetime(meta["x_values"])
        if group:
            piv = df.pivot_table(index=x, columns=group, values=meta["y"][0], aggfunc="sum").sort_index()
            for i, g in enumerate(piv.columns[:8]):
                ax.plot(pd.to_datetime(piv.index), piv[g], marker="o", markersize=3, linewidth=1.6, color=palette[i % len(palette)], label=str(g))
        else:
            for i, y in enumerate(meta["y"][:3]):
                axis = ax if i == 0 else ax.twinx() if i == 1 else None
                if axis is None:
                    break
                vals = np.asarray(meta["series_values"][y], float)
                axis.plot(xs, vals, marker="o", markersize=3, linewidth=1.8, color=palette[i], label=y.replace("_", " "))
                axis.set_ylabel(y.replace("_", " "), color=palette[i])
                if i == 0:
                    f = facts["series"][y]
                    if f.get("change_points"):
                        bounds = [0] + [c["index"] for c in f["change_points"]] + [len(vals)]
                        for a, b in zip(bounds, bounds[1:]):
                            seg_mean = np.mean([v for k, v in enumerate(vals[a:b], a) if k not in {o["index"] for o in f["outliers"]}] or vals[a:b])
                            ax.hlines(seg_mean, xs[a], xs[b - 1], colors="#0e3a4a", linestyles="dotted", linewidth=1.2)
                        for c in f["change_points"]:
                            ax.axvline(xs[c["index"]], color="#b03a2e", linestyle="--", linewidth=1)
                            ax.annotate("change point", (xs[c["index"]], ax.get_ylim()[1]), fontsize=8, color="#b03a2e", ha="left", va="top")
                    for o in f["outliers"]:
                        ax.scatter([xs[o["index"]]], [o["value"]], s=120, facecolors="none", edgecolors="#b03a2e", linewidths=1.6, zorder=5)
                        ax.annotate(f"outlier {fmt_num(o['value'])}", (xs[o["index"]], o["value"]), textcoords="offset points", xytext=(6, 4),
                                    fontsize=8, color="#b03a2e")
        ax.set_xlabel(x.replace("_", " "))
    else:
        y = meta["y"][0]
        s = df.groupby(x)[y].sum().sort_values(ascending=False).head(20)
        ax.bar(s.index.astype(str), s.values, color=palette[0])
        plt.setp(ax.get_xticklabels(), rotation=30, ha="right")
        ax.set_xlabel(x.replace("_", " "))
    ax.set_ylabel(meta["y"][0].replace("_", " ") if not meta["is_time"] or group else ax.get_ylabel())
    ax.set_title(truncate(meta["title"], 90), fontsize=11)
    ax.spines[["top", "right"]].set_visible(False)
    handles, labels = [], []
    for a in fig.axes:
        h, l = a.get_legend_handles_labels()
        handles += h
        labels += l
    if handles:
        ax.legend(handles, labels, frameon=False, fontsize=8, loc="upper left")
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


# ============================================================================ contract


class ChartImage(BaseModel):
    data_base64: str = Field(description="Base64-encoded image bytes")
    mime: str = Field(default="image/png", description="image/png, image/jpeg, image/webp")


class ChartSummaryInput(BaseModel):
    dataset_path: str | None = Field(default=None, description="CSV file inside the framework folder (e.g. sample_data/charts/weekly_quotes.csv)")
    csv_text: str | None = Field(default=None, description="Inline CSV", json_schema_extra={"ui": "textarea"})
    records: list[dict[str, Any]] | None = Field(default=None, description="Data as records")
    x: str | None = Field(default=None, description="x column (auto-detected)")
    y: list[str] = Field(default_factory=list, description="Value column(s) (auto-detected)")
    group: str | None = Field(default=None, description="Group / series column in long format (auto-detected)")
    partial_periods: list[str] = Field(default_factory=list, description="x values covered only in part (e.g. the current week); excluded from the statistics")
    title: str | None = Field(default=None, description="Chart title")
    audience: str | None = Field(default=None, description="Who the commentary is for (e.g. executives)")
    image: ChartImage | None = Field(default=None, description="Chart image - used only when no data is supplied (vision path)")


class ChartSummaryOutput(BaseModel):
    title: str
    source: Literal["data", "vision"]
    is_estimate: bool = False
    x: str | None = None
    y: list[str] = Field(default_factory=list)
    group: str | None = None
    frequency: str | None = None
    n_points: int = 0
    period: dict[str, str] = Field(default_factory=dict)
    facts: dict[str, Any] = Field(default_factory=dict)
    fact_lines: list[str] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    headline: str
    commentary: str
    long_description: str
    alt_text: str
    key_points: list[str] = Field(default_factory=list)
    narrative_source: Literal["model", "deterministic", "vision"]
    chart: str | None = None


def _generate_or_none(ctx: RunContext, template: str, schema: type[BaseModel], images: Any = None, **variables: Any) -> tuple[Any, str | None]:
    if ctx.llm.offline:
        ctx.render(template, **variables)
        return None, "no language model configured: commentary written by deterministic templates from the computed facts"
    try:
        return ctx.generate(template, schema, images=images, **variables), None
    except Exception as exc:
        ctx.log("model call failed", template=template, error=repr(exc))
        return None, f"model call failed ({type(exc).__name__}); commentary written by deterministic templates"


PERIOD_WORD = {"daily": "day", "weekly": "week", "monthly": "month", "quarterly": "quarter", "yearly": "year"}


@register
class ChartSummaryAgent(BaseAgent):
    spec = AgentSpec(
        id="chart_summary",
        name="Chart Summary",
        category=Category.EXPERIMENTATION,
        summary="Summarises the data behind a chart - trend, seasonality, change points, outliers and group contributions computed "
                "statistically - into report commentary and a WCAG long description, and renders the chart itself.",
        kind=AgentKind.HYBRID,
        tier=Tier.FAST,
        connectors=[],
        skills=["brand_voice"],
        keywords=["chart summary", "summarise chart", "describe chart", "chart commentary", "trend", "alt text", "long description",
                  "dashboard narrative", "explain this chart"],
        produces=["chart.png", "chart_summary.md", "chart_description.html"],
        feeds=["campaign_brief_generation"],
    )
    Input = ChartSummaryInput
    Output = ChartSummaryOutput
    schemas = {"narrative": ChartNarrative, "vision": ChartNarrative}

    def selftest_input(self) -> dict[str, Any]:
        return {"dataset_path": "sample_data/charts/weekly_quotes.csv", "title": "Weekly quotes and purchases, 2026"}

    def stages(self):
        return [("load", self.load), ("analyse", self.analyse), ("narrate", self.narrate), ("render", self.render)]

    # -- stage 1 ---------------------------------------------------------------------------------
    def load(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp = state["inputs"]
        df, source_desc, hints = None, "", {}
        if inp.get("records"):
            df, source_desc = pd.DataFrame(inp["records"]), "records"
        elif inp.get("csv_text"):
            df, source_desc = pd.read_csv(io.StringIO(inp["csv_text"])), "inline CSV"
        elif inp.get("dataset_path"):
            home = Path(ctx.settings.home).resolve()
            try:
                p = ctx.local_path(inp["dataset_path"])  # security policy: service callers read allowed folders only
            except PermissionError as exc:
                raise ValueError(f"dataset_path is not readable here: {exc}") from exc
            if not p.exists():
                raise ValueError(f"dataset_path not found: {inp['dataset_path']}")
            df, source_desc = pd.read_csv(p), str(p.relative_to(home)) if p.is_relative_to(home) else p.name
        else:
            up = ctx.upstream_output("exploration_generation")
            if up and up.get("dataset"):
                df, source_desc, hints = pd.DataFrame(up["dataset"]), "upstream exploration_generation dataset", up.get("dataset_hints") or {}
        if df is None:
            if inp.get("image"):
                return {"data": {"mode": "vision"}}
            raise ValueError("Provide data (dataset_path, csv_text, records or an upstream exploration) or a chart image")
        if df.empty:
            raise ValueError("The dataset has no rows")
        return {"data": {"mode": "data", "records": df.to_dict("records"), "columns": [str(c) for c in df.columns], "source": source_desc, "hints": hints}}

    # -- stage 2 ---------------------------------------------------------------------------------
    def analyse(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        if data["mode"] != "data":
            return {}
        df = pd.DataFrame(data["records"], columns=data["columns"])
        hints = data.get("hints") or {}
        y_hint = hints.get("y") if isinstance(hints.get("y"), list) else ([hints["y"]] if hints.get("y") else [])
        cols = detect_columns(df, inp.get("x") or hints.get("x"), inp.get("y") or y_hint, inp.get("group") or hints.get("group"))
        if not cols["y"]:
            raise ValueError(f"No numeric value column found in {data['columns']}")
        x, group = cols["x"], cols["group"]
        checks: list[Check] = []
        facts: dict[str, Any] = {"series": {}}
        meta: dict[str, Any] = {"x": x or "index", "y": cols["y"], "group": group, "is_time": cols["is_time"],
                                "title": inp.get("title") or ", ".join(c.replace("_", " ") for c in cols["y"]) + (f" by {x.replace('_', ' ')}" if x else "")}
        if cols["is_time"]:
            df["_x"] = pd.to_datetime(df[x])
            partial = {pd.Timestamp(v) for v in (inp.get("partial_periods") or hints.get("partial_periods") or []) if str(v).strip()}
            if partial:
                dropped = df["_x"].isin(partial)
                if dropped.any() and (~dropped).sum() >= 3:
                    meta_partial = sorted({str(v)[:10] for v in df.loc[dropped, "_x"]})
                    df = df[~dropped].copy()
                    checks.append(Check(rule="chart.partial_periods", status="info", severity="low",
                                        message=f"Excluded partial period(s) from the statistics: {', '.join(meta_partial)}."))
                    meta["partial_excluded"] = meta_partial
            agg = df.groupby("_x")[cols["y"]].sum().sort_index()  # totals across groups
            freq = infer_frequency(agg.index.to_series())
            labels = [_date(v, freq) for v in agg.index]
            meta.update(frequency=freq, period_word=PERIOD_WORD.get(freq, "period"), n_points=len(agg), labels=labels,
                        start=labels[0], end=labels[-1], x_values=[v.isoformat() for v in agg.index],
                        series_values={y: [float(v) for v in agg[y]] for y in cols["y"]})
            for y in cols["y"][:3]:
                facts["series"][y] = series_facts(agg[y].to_numpy(float), freq)
            if group:
                facts["contributions"] = contributions(df.assign(**{x: df["_x"]}), x, cols["y"][0], group,
                                                       facts["series"][cols["y"][0]]["change_points"])
        else:
            xcol = x or df.columns[0]
            meta.update(frequency=None, period_word="category", n_points=int(df[xcol].nunique()), labels=[str(v) for v in df[xcol]],
                        start="", end="", x_values=[str(v) for v in df[xcol]], series_values={})
            for y in cols["y"][:3]:
                facts["series"][y] = category_facts(df, xcol, y)
        lines = fact_lines(meta, facts)
        findings: list[Finding] = []
        for y, f in facts["series"].items():
            for c in f.get("change_points", []):
                findings.append(Finding(title=f"{y}: level shift at {meta['labels'][c['index']]}", severity="medium", category="analytics",
                                        detail=f"mean {fmt_num(c['mean_before'])} -> {fmt_num(c['mean_after'])}" + (f" ({c['shift_pct']:+.0%})" if c.get("shift_pct") is not None else ""),
                                        evidence="binary segmentation, BIC-style penalty", target=y, source_agent=self.spec.id))
            for o in f.get("outliers", []) if isinstance(f.get("outliers"), list) else []:
                lab = meta["labels"][o["index"]] if "index" in o else o.get("category")
                findings.append(Finding(title=f"{y}: outlier at {lab}", severity="low", category="analytics",
                                        detail=f"{fmt_num(o['value'])}" + (f" vs about {fmt_num(o['expected'])} expected" if "expected" in o else "") + f" (robust z {o['z']:.1f})",
                                        evidence="MAD z-score > 3.5", target=y, source_agent=self.spec.id))
            t = f.get("trend")
            if t and t["significant"]:
                findings.append(Finding(title=f"{y}: {t['direction']}ward trend", severity="low", category="analytics",
                                        detail=f"{t['pct_change']:+.1%} fitted change, p = {t['p_value']:.4f}", target=y, source_agent=self.spec.id))
            s = f.get("seasonality")
            if s and s.get("detected"):
                findings.append(Finding(title=f"{y}: repeating pattern every {s['lag']} periods", severity="low", category="analytics",
                                        detail=f"autocorrelation {s['strength']:.2f}", target=y, source_agent=self.spec.id))
        checks.append(Check(rule="chart.columns", status="pass", severity="low",
                            message=f"x = {meta['x']}{' (time, ' + str(meta['frequency']) + ')' if meta['is_time'] else ''}, y = {', '.join(cols['y'])}"
                                    + (f", group = {group}" if group else "")))
        if meta["is_time"] and meta["n_points"] < 8:
            checks.append(Check(rule="chart.length", status="warn", severity="medium", message=f"Only {meta['n_points']} points; trend and change-point tests have little power."))
        return {"data": {"meta": meta, "facts": facts, "lines": lines, "findings": [f.model_dump() for f in findings]}, "checks": checks}

    # -- stage 3 ---------------------------------------------------------------------------------
    def narrate(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        warnings: list[str] = []
        if data["mode"] == "vision":
            img = inp["image"]
            try:
                raw = base64.b64decode(img["data_base64"], validate=True)
            except Exception as exc:
                raise ValueError("image.data_base64 is not valid base64") from exc
            out, reason = _generate_or_none(ctx, "vision", ChartNarrative, images=[(raw, img.get("mime") or "image/png")], title=inp.get("title"),
                                            audience=inp.get("audience"))
            if out is None:
                out = ChartNarrative(headline="Image-only chart: no data was supplied, so nothing could be computed.",
                                     commentary="Supply the data behind the chart (CSV or records) for a verified summary; image reading needs a vision model.",
                                     long_description="A chart image was provided without its data. No values were extracted.", alt_text="Chart image (not analysed)",
                                     key_points=[])
            return {"data": {"narrative": out.model_dump(), "narrative_source": "vision"},
                    "warnings": ["Vision path: values are read from pixels by a model and are estimates; supply the data for exact figures."],
                    "provenance": {"degraded_reasons": [reason or "vision path: figures are model estimates read from the image"]}}
        meta, facts, lines = data["meta"], data["facts"], data["lines"]
        det = deterministic_narrative(meta, facts)
        model, reason = _generate_or_none(ctx, "narrative", ChartNarrative, title=meta["title"], fact_lines=lines, audience=inp.get("audience"),
                                          chart_type="line chart" if meta["is_time"] else "bar chart")
        source = "model"
        if model is not None:
            nums = [v for y, f in facts["series"].items() for v in _numbers(f)] + [v for v in _numbers(facts.get("contributions") or {})]
            tokens_ = set(re.findall(r"\d+(?:\.\d+)?", " ".join(lines)))
            bad = unsupported_numbers(" ".join([model.headline, model.commentary, model.long_description, model.alt_text] + model.key_points), nums, tokens_)
            if bad:
                warnings.append(f"Model commentary used numbers that were not computed ({', '.join(bad[:5])}); deterministic commentary used instead.")
                model = None
            elif len(model.alt_text) > 150:
                model.alt_text = truncate(model.alt_text, 150)
        if model is None:
            model, source = det, "deterministic"
        out: dict[str, Any] = {"data": {"narrative": model.model_dump(), "narrative_source": source}, "warnings": warnings}
        if reason:
            out["provenance"] = {"degraded_reasons": [reason]}
        return out

    # -- stage 4 ---------------------------------------------------------------------------------
    def render(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        n = ChartNarrative.model_validate(data["narrative"])
        artifacts = []
        if data["mode"] == "vision":
            out = ChartSummaryOutput(title=inp.get("title") or "Chart", source="vision", is_estimate=True, headline=n.headline, commentary=n.commentary,
                                     long_description=n.long_description, alt_text=n.alt_text, key_points=n.key_points, narrative_source="vision")
            return {"output": out.model_dump(mode="json"), "summary": f"Image-only chart summarised by the vision path (estimates): {truncate(n.headline, 100)}"}
        meta, facts = data["meta"], data["facts"]
        df = pd.DataFrame(data["records"], columns=data["columns"])
        chart_path = None
        try:
            png = render_chart(df, meta, facts)
            ref = ctx.artifacts.write_bytes("chart.png", png, "image/png", f"Chart: {meta['title']}")
            artifacts.append(ref)
            chart_path = ref.path
        except Exception as exc:
            ctx.log("chart render failed", error=repr(exc))
        html = (f"<figure>\n  <img src=\"chart.png\" alt=\"{_esc(n.alt_text)}\" aria-describedby=\"chart-desc\">\n"
                f"  <figcaption>{_esc(meta['title'])}</figcaption>\n</figure>\n<details>\n  <summary>Chart description</summary>\n"
                f"  <div id=\"chart-desc\"><p>{_esc(n.long_description)}</p></div>\n</details>\n")
        md = "\n".join([f"# {meta['title']}", "", f"**{n.headline}**", "", n.commentary, "", "## Key points"] + [f"- {p}" for p in n.key_points]
                       + ["", "## Long description (WCAG 1.1.1)", n.long_description, "", f"Alt text: {n.alt_text}", "", "## Computed facts"]
                       + [f"- {l}" for l in data["lines"]]) + "\n"
        artifacts += [ctx.artifacts.write_text("chart_summary.md", md, "text/markdown", "Chart commentary and long description"),
                      ctx.artifacts.write_text("chart_description.html", html, "text/html", "Accessible figure markup (alt text + long description)")]
        out = ChartSummaryOutput(title=meta["title"], source="data", x=meta["x"], y=meta["y"], group=meta["group"], frequency=meta.get("frequency"),
                                 n_points=meta["n_points"], period={"start": meta.get("start", ""), "end": meta.get("end", "")}, facts=_jsonable(facts),
                                 fact_lines=data["lines"], findings=data["findings"], headline=n.headline, commentary=n.commentary,
                                 long_description=n.long_description, alt_text=n.alt_text, key_points=n.key_points,
                                 narrative_source=data["narrative_source"], chart=chart_path)
        return {"output": out.model_dump(mode="json"), "artifacts": artifacts, "summary": truncate(n.headline, 220)}


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _numbers(obj: Any) -> list[float]:
    out: list[float] = []
    if isinstance(obj, dict):
        for v in obj.values():
            out += _numbers(v)
    elif isinstance(obj, list):
        for v in obj:
            out += _numbers(v)
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool) and math.isfinite(obj):
        out.append(float(obj))
        if abs(obj) >= 1e6:
            out += [obj / 1e6, obj / 1e9]
    return out


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (np.floating, np.integer)):
        return obj.item()
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj
