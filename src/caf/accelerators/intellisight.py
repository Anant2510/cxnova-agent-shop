"""IntelliSight's engines on this layer: the data interpreter, the analytics engine, custom reports, the decision layer and
conversational analytics, ported from the accelerator's own logic (github.com/Anant2510/IntelliSight, index.html).

IntelliSight reads an Adobe Analytics Data Warehouse export (or any flat CSV), classifies its columns the Adobe way
(eVars and props are dimensions, events are metrics, Date/Week/Month/Year are time), labels them, computes KPI totals,
year-over-year and period series, breakdowns by dimension, the conversion funnel and its derived rates (cart abandonment,
checkout completion, average order value), draws chart-ready series, and turns the computed facts into prioritised
recommendations and conversational answers. All arithmetic is done here, in code, on the full dataset; the model is
asked only for judgement (recommendations, the answer to a question) and always from exact pre-computed numbers.

Two differences from the browser product, stated plainly: the product runs model-written JavaScript on the dataset to
answer a question; here the model fills a query plan (metrics, dimension, filter, period) that the engine executes, so
no generated code runs on the server. And the dataset arrives as text (pasted CSV, a run artifact, or a bundled sample)
rather than a file picked in the browser.
"""
from __future__ import annotations

import csv
import io
import json
import re
from collections import OrderedDict, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

MAX_ROWS = 60_000
MAX_CSV_CHARS = 40_000_000
SAMPLES = {"xperion": "intellisight/xperion_adobe_analytics_sample.csv"}
INDUSTRY = {"xperion": "Airlines & Travel"}

EVAR_GUESSES_AIRLINE = {"eVar1": "Page Name", "eVar2": "Site Section", "eVar3": "Server", "eVar4": "Marketing Channel", "eVar5": "Device Type",
                        "eVar6": "Operating System", "eVar7": "Browser", "eVar8": "Campaign", "eVar9": "Search Term", "eVar10": "Language",
                        "eVar11": "Country", "eVar12": "Province", "eVar13": "Loyalty Tier", "eVar14": "Cabin Class", "eVar15": "Origin Airport",
                        "eVar16": "Dest Airport", "eVar17": "Route", "eVar18": "Route Type", "eVar19": "Platform", "eVar20": "Departure Time"}
EVAR_GUESSES_BANKING = {"eVar1": "Page Name", "eVar2": "Site Section", "eVar3": "Server", "eVar4": "Marketing Channel", "eVar5": "Device Type",
                        "eVar6": "Operating System", "eVar7": "Browser", "eVar8": "Campaign", "eVar9": "Search Term", "eVar10": "Language",
                        "eVar11": "Customer Type", "eVar12": "Account Type", "eVar13": "Customer Segment", "eVar14": "Product Tier", "eVar15": "State",
                        "eVar16": "City", "eVar17": "Branch ID", "eVar18": "Application Type", "eVar19": "Funnel Stage", "eVar20": "Content Type",
                        "eVar21": "Referral Source", "eVar22": "Login Method", "eVar23": "Transaction Type", "eVar24": "Error Type",
                        "eVar25": "Chat Initiated", "eVar26": "A/B Test Variant", "eVar27": "Day Part", "eVar28": "Session Depth", "eVar29": "NPS Score",
                        "eVar30": "Cross-Sell Shown"}
EVENT_LABELS_AIRLINE = {
    "event1": "Page Views", "event2": "Visits", "event3": "Unique Visitors", "event4": "Instances", "event5": "Click-throughs", "event6": "Searches",
    "event7": "Bookings", "event8": "Booking Starts", "event9": "Booking Revenue", "event10": "Cart Additions", "event11": "Cart Removals",
    "event12": "Checkouts", "event13": "Purchases", "event14": "Units", "event15": "Promo Codes Applied", "event16": "Promo Code Errors",
    "event17": "Product Quantity", "event18": "Seat Selections", "event19": "Seat Map Views", "event20": "Seat Upgrades", "event21": "Check-ins Completed",
    "event22": "Boarding Passes", "event23": "PNR Lookups", "event24": "Manage Booking Views", "event25": "Booking Modifications", "event26": "Total Revenue",
    "event27": "Ancillary Revenue", "event28": "Ancillary Purchases", "event29": "Lounge Visits", "event30": "WiFi Purchases", "event31": "Baggage Purchases",
    "event32": "Insurance Purchases", "event33": "Priority Boarding", "event34": "Meal Pre-orders", "event35": "Pet Bookings", "event36": "Flight Status Checks",
    "event37": "Disruption Alerts", "event38": "Rebookings", "event39": "Points Earned", "event40": "Points Redeemed", "event41": "Tier Upgrades",
    "event42": "Rewards Views", "event43": "Points Transfers", "event44": "Companion Vouchers", "event45": "Email Opens", "event46": "Email Clicks",
    "event47": "Email Unsubscribes", "event48": "Push Opens", "event49": "Push Dismissals", "event50": "App Opens", "event51": "App Crashes",
    "event52": "Video Views", "event53": "Video Completions", "event54": "Social Shares", "event55": "Social Clicks", "event56": "Chat Initiated",
    "event57": "Chat Resolved", "event58": "Call Escalations", "event59": "Survey Starts", "event60": "Survey Completes", "event61": "NPS Score",
    "event62": "CSAT Score", "event63": "Complaints", "event64": "Refund Requests", "event65": "Refund Amount", "event66": "Vouchers Issued",
    "event67": "Voucher Amount", "event68": "Form Starts", "event69": "Form Completes", "event70": "Form Errors", "event71": "Form Abandons",
    "event72": "Search Result Clicks", "event73": "Filter Uses", "event74": "Sort Changes", "event75": "Calendar Interactions", "event76": "Map Interactions",
    "event77": "Carousel Swipes", "event78": "CTA Clicks", "event79": "Banner Impressions", "event80": "Banner Clicks", "event81": "Time to Purchase",
    "event82": "Pages per Visit", "event83": "Session Duration", "event84": "Scroll Milestones", "event85": "Bounces", "event86": "404 Errors",
    "event87": "JS Errors", "event88": "Page Load Time", "event89": "Timeout Errors", "event90": "API Errors", "event91": "Payment Failures",
    "event92": "Fraud Flags", "event93": "Cross-sell Impressions", "event94": "Cross-sell Clicks", "event95": "Upsell Impressions",
    "event96": "Upsell Conversions", "event97": "Hotel Searches", "event98": "Car Rental Searches", "event99": "Package Bookings", "event100": "Package Revenue"}
EVENT_LABELS_BANKING = {
    "event1": "Page Views", "event2": "Visits", "event3": "Unique Visitors", "event4": "Account Views", "event5": "Rate Comparisons", "event6": "Calculator Uses",
    "event7": "Applications Started", "event8": "Applications Completed", "event9": "Revenue", "event10": "Product Adds", "event11": "Logins",
    "event12": "Fund Transfers", "event13": "Conversions", "event14": "Chat Initiated", "event15": "Branch Appointments", "event16": "Doc Downloads",
    "event17": "Complaints Filed", "event18": "Mobile Deposits", "event19": "Zelle Transactions", "event20": "Bill Payments", "event21": "Account Openings",
    "event22": "Card Activations", "event23": "Loan Inquiries", "event24": "Rate Lock Requests", "event25": "Pre-Approvals", "event26": "Total Revenue",
    "event27": "Fee Revenue", "event28": "Interest Income", "event29": "Overdraft Events", "event30": "ATM Withdrawals", "event31": "Wire Transfers",
    "event32": "ACH Transfers", "event33": "Check Deposits", "event34": "Statement Downloads", "event35": "Alert Setups", "event36": "Password Resets",
    "event37": "Fraud Alerts", "event38": "Dispute Filings", "event39": "Rewards Earned", "event40": "Rewards Redeemed", "event41": "Tier Upgrades",
    "event42": "Rewards Views", "event43": "Points Transfers", "event44": "Cashback Earned", "event45": "Email Opens", "event46": "Email Clicks",
    "event47": "Email Unsubscribes", "event48": "Push Opens", "event49": "Push Dismissals", "event50": "App Opens", "event51": "App Crashes",
    "event52": "Video Views", "event53": "Video Completions", "event54": "Social Shares", "event55": "Social Clicks", "event56": "Live Chat Sessions",
    "event57": "Chat Resolved", "event58": "Call Escalations", "event59": "Survey Starts", "event60": "Survey Completes", "event61": "NPS Score",
    "event62": "CSAT Score", "event63": "Formal Complaints", "event64": "Refund Requests", "event65": "Refund Amount", "event66": "Vouchers Issued",
    "event67": "Voucher Amount", "event68": "Form Starts", "event69": "Form Completes", "event70": "Form Errors", "event71": "Form Abandons"}
KPI_PRIORITY = ["event1", "event2", "event3", "event9", "event7", "event10", "event13", "event26", "event17", "event12"]
DIMENSION_CANDIDATES = ["eVar4", "eVar5", "eVar14", "eVar12", "eVar1", "Hit Type"]
KEY_DIMS = {"airline": ["eVar4", "eVar5", "eVar13", "eVar14", "eVar18", "eVar25"], "banking": ["eVar4", "eVar5", "eVar11", "eVar12", "eVar13", "eVar15", "eVar18", "eVar19"]}
FUNNEL_EVENTS = ["event1", "event6", "event8", "event10", "event12", "event13", "event7"]
METRIC_WORDS = re.compile(r"visit|view|click|bounce|time.*spent|revenue|order|purchase|cart|unit|instance|occurrence|page.?view|session|unique|conversion|book|checkin|check.?in|pnr|ancillary|loyalty", re.I)
DATE_WORDS = re.compile(r"^(date|day of week|week|month|quarter|year|timestamp)$", re.I)


# ------------------------------------------------------------------------------------------------ the dataset
class Dataset:
    """A parsed export with its columns classified the Adobe way, the same way IntelliSight's data interpreter does it."""

    def __init__(self, columns: list[str], rows: list[dict[str, str]], *, name: str = "", industry: str = "") -> None:
        self.columns = columns
        self.rows = rows
        self.name = name or "dataset"
        self.industry = industry or guess_industry(columns, rows)
        self.evars, self.props, self.events, self.metrics, self.dimensions, self.date_cols, self.key_events = [], [], [], [], [], [], []
        self.ctype: dict[str, str] = {}
        self._num: dict[str, list[float]] = {}
        self._classify()
        self.evar_labels = self._evar_labels()
        self.event_labels = EVENT_LABELS_BANKING if self.industry == "Banking & Financial Services" else EVENT_LABELS_AIRLINE

    # ---- classification (classifyCols, detectEvarLabels)
    def _classify(self) -> None:
        head = self.rows[:200]
        for c in self.columns:
            cl = c.lower()
            if re.fullmatch(r"evar\d+", cl):
                self.evars.append(c); self.ctype[c] = "evar"
            elif re.fullmatch(r"prop\d+", cl):
                self.props.append(c); self.ctype[c] = "prop"
            elif re.fullmatch(r"event\d+", cl):
                self.events.append(c); self.ctype[c] = "event"
                if any(r.get(c) not in (None, "", "0", "0.0") for r in head):
                    self.key_events.append(c)
            elif DATE_WORDS.match(cl):
                self.date_cols.append(c); self.ctype[c] = "date"
            elif METRIC_WORDS.search(cl):
                self.metrics.append(c); self.ctype[c] = "metric"
            else:
                vals = [r.get(c) for r in head if r.get(c) not in (None, "")]
                numeric = sum(1 for v in vals if _is_number(v))
                if vals and numeric / len(vals) > 0.7:
                    self.metrics.append(c); self.ctype[c] = "metric"
                else:
                    self.dimensions.append(c); self.ctype[c] = "dimension"

    def _evar_labels(self) -> dict[str, str]:
        guesses = EVAR_GUESSES_BANKING if self.industry == "Banking & Financial Services" else EVAR_GUESSES_AIRLINE
        labels: dict[str, str] = {}
        for e in self.evars:
            if e in guesses:
                labels[e] = guesses[e]
                continue
            uniq = {r.get(e) for r in self.rows[:200] if r.get(e) not in (None, "", "N/A")}
            if 2 <= len(uniq) <= 30:
                labels[e] = e
        return labels

    # ---- access
    def label(self, col: str) -> str:
        return self.event_labels.get(col.lower()) or self.evar_labels.get(col) or col

    def column_for(self, name: str) -> str | None:
        """A column by its id or its label, case-insensitively ('Booking Revenue' -> event9)."""
        if not name:
            return None
        if name in self.columns:
            return name
        low = name.strip().lower()
        for c in self.columns:
            if c.lower() == low or self.label(c).lower() == low:
                return c
        return None

    def date_col(self) -> str:
        for c in ("Date", "Month"):
            if c in self.columns:
                return c
        return self.date_cols[0] if self.date_cols else self.columns[0]

    def numbers(self, col: str) -> list[float]:
        if col not in self._num:
            self._num[col] = [_num(r.get(col)) for r in self.rows]
        return self._num[col]

    def plottable(self) -> list[str]:
        return self.metrics + self.key_events

    def best_kpis(self) -> list[str]:
        pm = self.plottable()
        found = [e for e in KPI_PRIORITY if e in pm][:6]
        for m in pm:
            if len(found) >= 6:
                break
            if m not in found:
                found.append(m)
        return found

    def best_dimension(self) -> str | None:
        for c in DIMENSION_CANDIDATES:
            if c in self.columns:
                uniq = {r.get(c) for r in self.rows[:100] if r.get(c) not in (None, "")}
                if 2 <= len(uniq) <= 50:
                    return c
        for e in self.evars:
            uniq = {r.get(e) for r in self.rows[:500] if r.get(e) not in (None, "")}
            if 2 <= len(uniq) <= 20:
                return e
        return self.dimensions[0] if self.dimensions else None

    def years(self) -> list[str]:
        dc = self.date_col()
        return sorted({str(r.get(dc) or "")[:4] for r in self.rows if re.match(r"^\d{4}", str(r.get(dc) or ""))})

    def date_range(self) -> tuple[str, str]:
        dc = self.date_col()
        ds = sorted(str(r.get(dc)) for r in self.rows if r.get(dc))
        return (ds[0], ds[-1]) if ds else ("?", "?")


def _is_number(v: Any) -> bool:
    try:
        float(str(v).replace(",", ""))
        return True
    except ValueError:
        return False


def _num(v: Any) -> float:
    if v is None or v == "":
        return 0.0
    try:
        return float(str(v).replace(",", "").replace("$", ""))
    except ValueError:
        return 0.0


def guess_industry(columns: list[str], rows: list[dict[str, str]]) -> str:
    """Airline or banking vocabulary in the column names and the first rows' values: the one that occurs more wins."""
    text = " ".join(columns[:60]).lower() + " " + " ".join(str(v) for r in rows[:80] for v in list(r.values())[:40]).lower()
    bank = len(re.findall(r"\b(?:branch|zelle|loan|deposit|bank|mortgage|checking|savings)\b", text))
    air = len(re.findall(r"\b(?:flight|route|cabin|airport|pnr|airline|boarding|seat|fare|check-?in|lounge)\b", text))
    if bank > air:
        return "Banking & Financial Services"
    if air:
        return "Airlines & Travel"
    return "Digital business"


# ------------------------------------------------------------------------------------------------ loading
def load_dataset(inputs: dict[str, Any], ctx: Any = None) -> Dataset:
    """The dataset named by the inputs: pasted CSV text (`dataset`), a bundled sample (`dataset_name`: xperion), or the
    dataset an earlier step of the run interpreted (`ctx.upstream`)."""
    text = inputs.get("dataset")
    name = str(inputs.get("dataset_name") or "").strip().lower()
    if isinstance(text, str) and text.strip() and ("\n" in text or "," in text):
        return parse_csv(text, name=name or "pasted export")
    if name:
        rel = SAMPLES.get(name)
        if rel is None:
            raise ValueError(f"no bundled dataset called '{name}'; the samples are: {', '.join(SAMPLES)}")
        path = _sample_path(rel, ctx)
        return parse_csv(path.read_text(encoding="utf-8"), name=f"{name} sample ({path.name})", industry=INDUSTRY.get(name, ""))
    for u in reversed(list((getattr(ctx, "upstream", None) or {}).values())):   # an earlier step of this run kept the dataset as a file
        out = (u or {}).get("output") or {}
        res = out.get("result") if isinstance(out, dict) else None
        if isinstance(res, dict) and res.get("dataset_file"):
            path = Path(ctx.artifacts.runs_dir) / str(res["dataset_file"])
            if path.exists():
                return parse_csv(path.read_text(encoding="utf-8"), name=str(res.get("dataset") or "dataset"), industry=str(res.get("industry") or ""))
    raise ValueError("give the export as `dataset` (CSV text) or name a bundled sample with `dataset_name` (xperion)")


def _sample_path(rel: str, ctx: Any) -> Path:
    from ..config import get_settings

    home = Path(getattr(getattr(ctx, "settings", None), "home", None) or get_settings().home)
    path = home / "sample_data" / rel
    if not path.exists():
        raise ValueError(f"the bundled sample is missing: {path}")
    return path


def parse_csv(text: str, *, name: str = "", industry: str = "") -> Dataset:
    if len(text) > MAX_CSV_CHARS:
        raise ValueError("the export is too large to analyse here (40 MB of text at most)")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    columns = [c.strip() for c in next(reader, [])]
    if not columns:
        raise ValueError("the export has no header row")
    rows: list[dict[str, str]] = []
    for raw in reader:
        if not any(v.strip() for v in raw):
            continue
        rows.append({c: (raw[i].strip() if i < len(raw) else "") for i, c in enumerate(columns)})
        if len(rows) >= MAX_ROWS:
            break
    if not rows:
        raise ValueError("the export has a header but no rows")
    return Dataset(columns, rows, name=name, industry=industry)


# ------------------------------------------------------------------------------------------------ engine 01: the data interpreter
def profile(ds: Dataset) -> dict[str, Any]:
    """What the dataset is: its shape, its time range, every column classified and labelled, and the KPIs and dimension
    the dashboard would open with (buildDatasetMeta, buildSchema, findBestKPIs, findBestDimension)."""
    lo, hi = ds.date_range()
    cols = []
    for c in ds.columns:
        t = ds.ctype.get(c, "dimension")
        entry: dict[str, Any] = {"column": c, "type": t, "label": ds.label(c)}
        if t in ("event", "metric"):
            entry["total"] = round(sum(ds.numbers(c)), 2)
        else:
            uniq = OrderedDict()
            for r in ds.rows[:500]:
                v = r.get(c)
                if v not in (None, ""):
                    uniq[v] = None
            entry["unique_in_sample"] = len(uniq)
            entry["examples"] = list(uniq)[:5]
        cols.append(entry)
    kpis = ds.best_kpis()
    dim = ds.best_dimension()
    return {"dataset": ds.name, "industry": ds.industry, "rows": len(ds.rows), "columns": len(ds.columns), "date_column": ds.date_col(),
            "date_range": {"from": lo, "to": hi}, "years": ds.years(),
            "counts": {"evars": len(ds.evars), "props": len(ds.props), "events": len(ds.events), "events_with_data": len(ds.key_events),
                       "metrics": len(ds.metrics), "dimensions": len(ds.dimensions), "time": len(ds.date_cols)},
            "suggested_kpis": [{"column": k, "label": ds.label(k)} for k in kpis],
            "suggested_dimension": {"column": dim, "label": ds.label(dim)} if dim else None,
            "key_dimensions": [{"column": e, "label": ds.label(e)} for e in ds.evars if e in ds.evar_labels][:20],
            "columns_detail": cols}


# ------------------------------------------------------------------------------------------------ engine 02: the analytics engine
def _period_key(value: str, grain: str) -> str:
    s = str(value or "")
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        y, mo, d = m.groups()
        if grain == "weekly":
            try:
                dt = date(int(y), int(mo), int(d))
                wk = (dt.timetuple().tm_yday + 6) // 7
                return f"{y}-W{wk:02d}"
            except ValueError:
                return s
        return f"{y}-{mo}" if grain == "monthly" else y
    if grain == "yearly" and len(s) >= 4:
        return s[:4]
    return s


def series(ds: Dataset, metric: str, grain: str = "monthly", rows: list[dict[str, str]] | None = None) -> dict[str, list]:
    """The metric summed per period (aggTime): labels and values, in time order."""
    dc = ds.date_col()
    bucket: dict[str, float] = defaultdict(float)
    for r in (rows if rows is not None else ds.rows):
        d = r.get(dc)
        if d in (None, ""):
            continue
        bucket[_period_key(d, grain)] += _num(r.get(metric))
    keys = sorted(bucket)
    return {"labels": keys, "values": [round(bucket[k], 2) for k in keys]}


def breakdown(ds: Dataset, dim: str, metric: str | None = None, *, top: int = 10, rows: list[dict[str, str]] | None = None) -> list[dict[str, Any]]:
    """Rows per value of a dimension, with the metric's total when one is given (dimBreakdowns, the dashboard's bars)."""
    count: dict[str, int] = defaultdict(int)
    total: dict[str, float] = defaultdict(float)
    for r in (rows if rows is not None else ds.rows):
        v = r.get(dim)
        if v in (None, "", "N/A"):
            continue
        count[v] += 1
        if metric:
            total[v] += _num(r.get(metric))
    key = (lambda v: total[v]) if metric else (lambda v: count[v])
    out = []
    for v in sorted(count, key=key, reverse=True)[:top]:
        row = {"value": v, "rows": count[v]}
        if metric:
            row[metric] = round(total[v], 2)
        out.append(row)
    return out


def yoy(ds: Dataset, metric: str) -> dict[str, Any]:
    years = ds.years()
    y1, y2 = (years[-2], years[-1]) if len(years) >= 2 else (None, None)
    dc = ds.date_col()
    tot = s1 = s2 = 0.0
    for r in ds.rows:
        v = _num(r.get(metric))
        tot += v
        y = str(r.get(dc) or "")[:4]
        if y1 and y == y1:
            s1 += v
        elif y2 and y == y2:
            s2 += v
    return {"column": metric, "label": ds.label(metric), "total": round(tot, 2), "year1": y1, "year1_total": round(s1, 2), "year2": y2,
            "year2_total": round(s2, 2), "yoy_pct": round((s2 - s1) / s1 * 100, 1) if s1 > 0 else None}


def analyse(ds: Dataset, *, grain: str = "monthly", metrics: list[str] | None = None, dimensions: list[str] | None = None) -> dict[str, Any]:
    """Everything the dashboard and the recommendation prompt compute (renderDash, genRecs): KPI totals with year-over-year,
    the period series of each KPI, breakdowns of the key dimensions, the conversion funnel and its derived rates."""
    kpis = [c for c in (metrics or []) if c in ds.columns] or ds.best_kpis()
    years = ds.years()
    y1, y2 = (years[-2], years[-1]) if len(years) >= 2 else (None, None)
    kpi_rows = [yoy(ds, k) for k in kpis]
    trend = {k: series(ds, k, grain) for k in kpis[:6]}
    bank = ds.industry == "Banking & Financial Services"
    rev = "event9" if "event9" in ds.events else ("event26" if "event26" in ds.events else None)
    dims = [d for d in (dimensions or []) if d in ds.columns] or [d for d in KEY_DIMS["banking" if bank else "airline"] if d in ds.columns]
    breakdowns = {ds.label(d) + f" ({d})": breakdown(ds, d, rev, top=8) for d in dims}
    funnel = {ds.label(e): yoy(ds, e) for e in FUNNEL_EVENTS if e in ds.events}
    rates = derived_rates(ds, y1, y2)
    lo, hi = ds.date_range()
    return {"dataset": ds.name, "industry": ds.industry, "rows": len(ds.rows), "period": {"from": lo, "to": hi, "year1": y1, "year2": y2},
            "grain": grain, "kpis": kpi_rows, "series": trend, "breakdowns": breakdowns, "funnel": funnel, "rates": rates,
            "revenue_column": rev}


def derived_rates(ds: Dataset, y1: str | None, y2: str | None) -> dict[str, Any]:
    """Cart abandonment, checkout completion, AOV and removals-vs-additions, overall and per year, with IntelliSight's formulas."""
    dc = ds.date_col()
    acc = {k: [0.0, 0.0, 0.0] for k in ("cart", "purch", "check", "rev", "rem")}
    cols = {"cart": "event10", "purch": "event13", "check": "event12", "rev": "event9", "rem": "event11"}
    for r in ds.rows:
        y = str(r.get(dc) or "")[:4]
        slot = 1 if (y1 and y == y1) else 2 if (y2 and y == y2) else None
        for k, c in cols.items():
            v = _num(r.get(c))
            acc[k][0] += v
            if slot:
                acc[k][slot] += v
    def rate(i: int) -> dict[str, Any]:
        cart, purch, check, rev, rem = (acc[k][i] for k in ("cart", "purch", "check", "rev", "rem"))
        return {"cart_abandonment_pct": round((cart - purch) / cart * 100, 2) if cart > 0 else None,
                "checkout_completion_pct": round(purch / check * 100, 2) if check > 0 else None,
                "average_order_value": round(rev / purch, 2) if purch > 0 else None,
                "cart_removals_vs_additions_pct": round(rem / cart * 100, 2) if cart > 0 else None}
    out = {"overall": rate(0), "formulas": {"cart_abandonment_pct": "(cart additions - purchases) / cart additions * 100",
                                           "checkout_completion_pct": "purchases / checkouts * 100", "average_order_value": "revenue / purchases"}}
    if y1:
        out[y1] = rate(1)
    if y2:
        out[y2] = rate(2)
    return out


def chart(kind: str, title: str, labels: list, datasets: list[dict[str, Any]]) -> dict[str, Any]:
    """The chart spec IntelliSight's chat emits (```chart blocks): type, title, labels, datasets."""
    return {"type": kind, "title": title, "labels": labels, "datasets": datasets}


# ------------------------------------------------------------------------------------------------ custom reports
def custom_report(ds: Dataset, fields: list[str], *, grain: str = "monthly", filters: dict[str, Any] | None = None, top: int = 10) -> dict[str, Any]:
    """One panel per chosen field (updCust): a metric becomes its series over time, a dimension its top values; filters narrow the rows."""
    rows = apply_filters(ds, filters or {})
    panels = []
    for f in fields:
        col = ds.column_for(f)
        if not col:
            panels.append({"field": f, "error": "no such column"})
            continue
        if ds.ctype.get(col) in ("metric", "event"):
            s = series(ds, col, grain, rows)
            panels.append({"field": col, "label": ds.label(col), "kind": "series", "grain": grain, "total": round(sum(s["values"]), 2),
                           "chart": chart("line", ds.label(col), s["labels"], [{"label": ds.label(col), "data": s["values"]}])})
        else:
            b = breakdown(ds, col, None, top=top, rows=rows)
            panels.append({"field": col, "label": ds.label(col), "kind": "breakdown",
                           "chart": chart("bar", ds.label(col), [x["value"] for x in b], [{"label": "rows", "data": [x["rows"] for x in b]}])})
    return {"dataset": ds.name, "rows_in_scope": len(rows), "rows_total": len(ds.rows), "filters": filters or {}, "panels": panels}


def apply_filters(ds: Dataset, filters: dict[str, Any]) -> list[dict[str, str]]:
    """{column: value | [values] | {from, to} on the date column | {"contains": text}}."""
    if not filters:
        return ds.rows
    rows = ds.rows
    for key, want in filters.items():
        col = ds.column_for(key)
        if not col:
            continue
        if isinstance(want, dict) and ("from" in want or "to" in want):
            lo, hi = str(want.get("from") or ""), str(want.get("to") or "~")
            rows = [r for r in rows if lo <= str(r.get(col) or "") <= hi]
        elif isinstance(want, dict) and want.get("contains"):
            needle = str(want["contains"]).lower()
            rows = [r for r in rows if needle in str(r.get(col) or "").lower()]
        elif isinstance(want, list):
            wanted = {str(v) for v in want}
            rows = [r for r in rows if str(r.get(col) or "") in wanted]
        else:
            rows = [r for r in rows if str(r.get(col) or "") == str(want)]
    return rows


# ------------------------------------------------------------------------------------------------ engine 03: the decision layer
class Recommendation(BaseModel):
    title: str
    priority: str = Field(default="medium", description="high | medium | low")
    insight: str = Field(description="2-3 sentences citing exact numbers and the year-over-year change")
    recommendation: str = Field(description="2-3 actionable sentences")
    metric: str = Field(default="", description="what to track")


class Recommendations(BaseModel):
    recommendations: list[Recommendation] = Field(default_factory=list)


def recommend(analysis: dict[str, Any], ctx: Any, *, focus: str = "") -> dict[str, Any]:
    """Six prioritised recommendations from the computed facts (genRecs): the model reasons, it never recomputes."""
    bank = analysis.get("industry") == "Banking & Financial Services"
    areas = ("application funnel conversion, revenue/product mix, digital adoption (mobile/Zelle), customer segment optimization, channel "
             "attribution, account type performance" if bank else
             "cart abandonment, revenue/AOV trends, conversion funnel, channel optimization, device/platform, loyalty/engagement")
    if ctx.llm.offline:
        return {"method": "offline", "recommendations": _plain_recommendations(analysis)}
    recs = ctx.generate("recommend", Recommendations, analysis=json.dumps(_for_prompt(analysis), indent=1), industry=analysis.get("industry"),
                        period=analysis.get("period") or {}, rows=analysis.get("rows"), areas=areas, focus=focus)
    out = [r.model_dump() for r in recs.recommendations][:8]
    for r in out:
        if r["priority"] not in ("high", "medium", "low"):
            r["priority"] = "medium"
    return {"method": "model", "recommendations": out}


def _plain_recommendations(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    """Without a model: the facts the model would have reasoned from, as review items (never invented advice)."""
    out = []
    p = analysis.get("period") or {}
    for k in (analysis.get("kpis") or [])[:6]:
        if k.get("yoy_pct") is None:
            continue
        out.append({"title": f"{k['label']}: {k['yoy_pct']:+}% {p.get('year1')} to {p.get('year2')}", "priority": "high" if abs(k["yoy_pct"]) >= 10 else "medium",
                    "insight": f"{k['label']} went from {k['year1_total']:,.0f} in {p.get('year1')} to {k['year2_total']:,.0f} in {p.get('year2')} ({k['yoy_pct']:+}%).",
                    "recommendation": "Connect a model for the recommendation; this item carries the computed fact only.", "metric": k["label"]})
    rates = (analysis.get("rates") or {}).get("overall") or {}
    if rates.get("cart_abandonment_pct") is not None:
        out.append({"title": f"Cart abandonment at {rates['cart_abandonment_pct']}%", "priority": "high" if rates["cart_abandonment_pct"] > 60 else "medium",
                    "insight": f"(cart additions - purchases) / cart additions = {rates['cart_abandonment_pct']}% over the whole period.",
                    "recommendation": "Connect a model for the recommendation; this item carries the computed fact only.", "metric": "cart abandonment rate"})
    return out


def _for_prompt(analysis: dict[str, Any]) -> dict[str, Any]:
    """The analysis without the long series: what the recommendation prompt needs, small enough to send."""
    keep = {k: v for k, v in analysis.items() if k != "series"}
    keep["series_note"] = "monthly series omitted; totals, year-over-year, breakdowns, funnel and rates are exact"
    return keep


# ------------------------------------------------------------------------------------------------ conversational analytics
class QueryPlan(BaseModel):
    """What the engine should compute to answer a question: the model fills this, the engine runs it (no generated code)."""
    metrics: list[str] = Field(default_factory=list, description="metric columns or labels to total (event9, 'Booking Revenue' ...)")
    dimension: str | None = Field(default=None, description="a dimension column or label to break the metrics down by, if the question asks for one")
    grain: str = Field(default="monthly", description="weekly | monthly | yearly for a trend over time; 'none' for totals only")
    filters: dict[str, Any] = Field(default_factory=dict, description="{column: value | [values] | {from, to} | {contains}} to narrow the rows")
    top: int = Field(default=10, ge=1, le=50)
    compare_years: bool = Field(default=True, description="include the year-over-year comparison")
    chart: str | None = Field(default=None, description="bar | line | doughnut | pie when a chart helps, else null")
    note: str = Field(default="", description="what the plan cannot capture about the question, if anything")


class Answer(BaseModel):
    answer_markdown: str = Field(description="the answer with the exact numbers, in markdown")
    chart: dict[str, Any] | None = Field(default=None, description='{"type","title","labels","datasets":[{"label","data"}]} or null')
    follow_ups: list[str] = Field(default_factory=list, description="up to three questions worth asking next")


def ask(ds: Dataset, question: str, ctx: Any, history: list[dict[str, str]] | None = None) -> dict[str, Any]:
    """IntelliSight's two-step chat: plan what to compute, compute it on the full dataset, answer from the exact results."""
    schema = schema_summary(ds)
    if ctx.llm.offline:
        plan = _heuristic_plan(ds, question)
    else:
        plan = ctx.generate("plan", QueryPlan, question=question, schema=schema, history=history or [])
    computed = execute_plan(ds, plan)
    if ctx.llm.offline:
        return {"method": "offline", "plan": plan.model_dump(), "computed": computed, "answer_markdown": _plain_answer(ds, plan, computed),
                "chart": computed.get("chart"), "follow_ups": []}
    ans = ctx.generate("answer", Answer, question=question, computed=json.dumps(computed, indent=1)[:14000], rows=len(ds.rows),
                       industry=ds.industry, context=revenue_context(ds), history=history or [])
    return {"method": "model", "plan": plan.model_dump(), "computed": computed, "answer_markdown": ans.answer_markdown,
            "chart": ans.chart or computed.get("chart"), "follow_ups": ans.follow_ups[:3]}


def _heuristic_plan(ds: Dataset, question: str) -> QueryPlan:
    q = question.lower()
    metrics = [c for c in ds.plottable() if ds.label(c).lower() in q or c.lower() in q][:3] or (["event9"] if "event9" in ds.events else ds.best_kpis()[:1])
    # a dimension named by its full label, else by one word of it ("channel" → Marketing Channel), else "by <column>"
    cands = [d for d in ds.evars + ds.dimensions if ds.label(d) != d]
    dim = next((d for d in cands if ds.label(d).lower() in q), None) \
        or next((d for d in cands if any(len(w) > 3 and re.search(r"\b" + re.escape(w) + r"s?\b", q) for w in ds.label(d).lower().split())), None) \
        or next((d for d in ds.evars + ds.dimensions if re.search(r"\bby\s+" + re.escape(d.lower()) + r"\b", q)), None)
    grain = "yearly" if "year" in q else "weekly" if "week" in q else "monthly" if re.search(r"month|trend|over time", q) else "none"
    return QueryPlan(metrics=metrics, dimension=dim, grain=grain, chart="bar" if dim else "line", note="planned by keyword match: no model is connected")


def execute_plan(ds: Dataset, plan: QueryPlan) -> dict[str, Any]:
    rows = apply_filters(ds, plan.filters)
    metrics = [c for c in (ds.column_for(m) for m in plan.metrics) if c]
    if not metrics:
        metrics = ds.best_kpis()[:1]
    dim = ds.column_for(plan.dimension) if plan.dimension else None
    out: dict[str, Any] = {"rows_in_scope": len(rows), "rows_total": len(ds.rows), "filters": plan.filters,
                           "metrics": [{"column": m, "label": ds.label(m), "total": round(sum(_num(r.get(m)) for r in rows), 2)} for m in metrics]}
    if plan.compare_years and len(ds.years()) >= 2 and not plan.filters:
        out["year_over_year"] = [yoy(ds, m) for m in metrics]
    if dim:
        out["by_" + dim] = {"dimension": dim, "label": ds.label(dim), "rows": breakdown(ds, dim, metrics[0], top=plan.top, rows=rows)}
        b = out["by_" + dim]["rows"]
        out["chart"] = chart(plan.chart or "bar", f"{ds.label(metrics[0])} by {ds.label(dim)}", [x["value"] for x in b],
                             [{"label": ds.label(metrics[0]), "data": [x[metrics[0]] for x in b]}])
    elif plan.grain in ("weekly", "monthly", "yearly"):
        out["series"] = {m: series(ds, m, plan.grain, rows) for m in metrics[:3]}
        first = out["series"][metrics[0]]
        out["chart"] = chart(plan.chart or "line", f"{ds.label(metrics[0])} by {plan.grain[:-2]}", first["labels"],
                             [{"label": ds.label(m), "data": out["series"][m]["values"]} for m in metrics[:3]])
    return out


def _plain_answer(ds: Dataset, plan: QueryPlan, computed: dict[str, Any]) -> str:
    lines = [f"Computed on {computed['rows_in_scope']:,} of {computed['rows_total']:,} rows (no model is connected, so this is the data, not a narrative)."]
    for m in computed["metrics"]:
        lines.append(f"- **{m['label']}**: {m['total']:,.2f}")
    for y in computed.get("year_over_year") or []:
        if y.get("yoy_pct") is not None:
            lines.append(f"- {y['label']}: {y['year1']} {y['year1_total']:,.0f} to {y['year2']} {y['year2_total']:,.0f} ({y['yoy_pct']:+}%)")
    for k, v in computed.items():
        if k.startswith("by_"):
            lines.append(f"- By {v['label']}: " + "; ".join(f"{r['value']} {r.get(computed['metrics'][0]['column'], r['rows']):,}" for r in v["rows"][:5]))
    return "\n".join(lines)


def schema_summary(ds: Dataset) -> str:
    """The schema the planner reads (buildSchema): metrics with totals, dimensions with examples, the key column mapping."""
    lo, hi = ds.date_range()
    lines = [f"DATASET: {len(ds.rows)} rows x {len(ds.columns)} cols, date column {ds.date_col()}, range {lo} to {hi}", f"INDUSTRY: {ds.industry}", "",
             "METRIC COLUMNS (sum over the dataset):"]
    for c in ds.key_events[:30] + ds.metrics[:10]:
        lines.append(f'  {c} ("{ds.label(c)}"): {round(sum(ds.numbers(c))):,}')
    lines += ["", "DIMENSION COLUMNS:"]
    for c in ds.evars[:20] + ds.dimensions[:10]:
        uniq = OrderedDict()
        for r in ds.rows[:500]:
            v = r.get(c)
            if v not in (None, ""):
                uniq[v] = None
        lines.append(f'  {c} ("{ds.label(c)}"): {len(uniq)} unique, e.g. {" | ".join(list(uniq)[:5])}')
    return "\n".join(lines)


def revenue_context(ds: Dataset) -> str:
    """The always-available totals the answer prompt gets (buildRevBookingCtx)."""
    bank = ds.industry == "Banking & Financial Services"
    conv = "Applications" if bank else "Bookings"
    tot = {c: round(sum(ds.numbers(c))) for c in ("event9", "event7", "event13", "event10", "event2", "event1") if c in ds.columns}
    lines = [f"Total Revenue (event9): ${tot.get('event9', 0):,}", f"Total {conv} (event7): {tot.get('event7', 0):,}",
             f"Total Conversions (event13): {tot.get('event13', 0):,}", f"Total Visits (event2): {tot.get('event2', 0):,}",
             f"Total Page Views (event1): {tot.get('event1', 0):,}"]
    if tot.get("event13"):
        lines.append(f"AOV: ${tot.get('event9', 0) / tot['event13']:.2f}")
    for y in ds.years():
        rows = [r for r in ds.rows if str(r.get(ds.date_col()) or "").startswith(y)]
        lines.append(f"{y}: Revenue=${round(sum(_num(r.get('event9')) for r in rows)):,}, {conv}={round(sum(_num(r.get('event7')) for r in rows)):,}")
    return "\n".join(lines)


# ------------------------------------------------------------------------------------------------ the pack's entry point
def run(action: str, inputs: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """The coded runner for every IntelliSight agent (caf.packs: `code: caf.accelerators.intellisight:run`)."""
    if action == "interpret":
        ds = load_dataset(inputs, ctx)
        prof = profile(ds)
        checks = [{"rule": "date column found", "status": "pass" if ds.date_cols else "warn", "message": f"time column: {ds.date_col()}"},
                  {"rule": "events carry data", "status": "pass" if ds.key_events else "warn", "message": f"{len(ds.key_events)} of {len(ds.events)} events have values"}]
        result = {**prof, "schema": schema_summary(ds), "dataset_file": _keep(ds, ctx)}
        return {"summary": f"{ds.name}: {len(ds.rows):,} rows x {len(ds.columns)} columns, {prof['date_range']['from']} to {prof['date_range']['to']}; "
                           f"{len(ds.key_events)} metrics with data, {len(ds.evar_labels)} labelled dimensions; {ds.industry}.",
                "result": result, "checks": checks, "artifacts": [{"type": "dataset_profile", "name": "The dataset profile", "content": prof}]}
    if action == "analyse":
        ds = load_dataset(inputs, ctx)
        grain = str(inputs.get("grain") or "monthly").lower()
        a = analyse(ds, grain=grain if grain in ("weekly", "monthly", "yearly") else "monthly", metrics=_list(inputs.get("metrics")), dimensions=_list(inputs.get("dimensions")))
        kp = a["kpis"][0] if a["kpis"] else None
        summ = (f"{len(a['kpis'])} KPIs over {a['period']['from']} to {a['period']['to']}" + (f"; {kp['label']} {kp['total']:,.0f}" + (f" ({kp['yoy_pct']:+}% {a['period']['year1']} to {a['period']['year2']})" if kp.get('yoy_pct') is not None else "") if kp else ""))
        charts = [chart("line", ds.label(k), s["labels"], [{"label": ds.label(k), "data": s["values"]}]) for k, s in a["series"].items()]
        return {"summary": summ, "result": {**a, "charts": charts, "dataset_file": _keep(ds, ctx)},
                "artifacts": [{"type": "kpi_table", "name": "KPI totals and year-over-year", "content": a["kpis"]},
                              {"type": "charts", "name": "Chart-ready series", "content": charts}],
                "checks": [{"rule": "two years to compare", "status": "pass" if a["period"]["year1"] else "info",
                            "message": f"{a['period']['year1']} vs {a['period']['year2']}" if a["period"]["year1"] else "one year of data: no year-over-year"}]}
    if action == "report":
        ds = load_dataset(inputs, ctx)
        fields = _list(inputs.get("fields")) or [ds.best_dimension() or ds.columns[0]] + ds.best_kpis()[:2]
        rep = custom_report(ds, fields, grain=str(inputs.get("grain") or "monthly"), filters=_dict(inputs.get("filters")), top=int(inputs.get("top") or 10))
        return {"summary": f"{len(rep['panels'])} panel(s) on {rep['rows_in_scope']:,} of {rep['rows_total']:,} rows", "result": rep,
                "artifacts": [{"type": "report", "name": "Custom report", "content": rep}]}
    if action == "recommend":
        analysis = _dict(inputs.get("analysis")) or _upstream_analysis(ctx)
        if not analysis:
            ds = load_dataset(inputs, ctx)
            analysis = analyse(ds)
        rec = recommend(analysis, ctx, focus=str(inputs.get("focus") or ""))
        n = len(rec["recommendations"])
        return {"summary": f"{n} recommendation(s), {rec['method']}, from the computed facts of {analysis.get('period', {}).get('year1')} vs {analysis.get('period', {}).get('year2')}",
                "result": {"recommendations": rec["recommendations"], "method": rec["method"], "based_on": {"period": analysis.get("period"), "rows": analysis.get("rows"), "kpis": [k["label"] for k in analysis.get("kpis", [])]}},
                "artifacts": [{"type": "recommendations", "name": "Prioritised recommendations", "content": rec["recommendations"]}],
                "human_followups": [] if rec["method"] == "model" else ["Connect a model: without one the items carry the computed facts, not advice"]}
    if action == "ask":
        ds = load_dataset(inputs, ctx)
        q = str(inputs.get("question") or inputs.get("request") or "").strip()
        if not q:
            raise ValueError("ask a question")
        out = ask(ds, q, ctx, history=_history(inputs.get("history")))
        return {"summary": out["answer_markdown"].split("\n")[0][:300], "result": out,
                "artifacts": ([{"type": "chart", "name": out["chart"].get("title") or "chart", "content": out["chart"]}] if out.get("chart") else [])}
    raise ValueError(f"unknown action {action}")


def _list(v: Any) -> list[str]:
    if v in (None, ""):
        return []
    if isinstance(v, list):
        return [str(x) for x in v if str(x).strip()]
    return [x.strip() for x in str(v).split(",") if x.strip()]


def _dict(v: Any) -> dict[str, Any]:
    if isinstance(v, dict):
        return v
    if isinstance(v, str) and v.strip().startswith("{"):
        try:
            return json.loads(v)
        except ValueError:
            return {}
    return {}


def _history(v: Any) -> list[dict[str, str]]:
    if isinstance(v, list):
        return [{"role": str(x.get("role", "user")), "content": str(x.get("content", ""))[:2000]} for x in v if isinstance(x, dict)][-6:]
    return []


def _upstream_analysis(ctx: Any) -> dict[str, Any]:
    for u in reversed(list((getattr(ctx, "upstream", None) or {}).values())):
        out = (u or {}).get("output") or {}
        res = out.get("result") if isinstance(out, dict) else None
        if isinstance(res, dict) and "kpis" in res and "rates" in res:
            return res
    return {}


def _keep(ds: Dataset, ctx: Any) -> str:
    """The dataset kept with the run as a file, so the next steps of a flow read it instead of asking for it again."""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=ds.columns)
    w.writeheader()
    for r in ds.rows:
        w.writerow(r)
    return ctx.artifacts.write_text("dataset.csv", buf.getvalue(), mime="text/csv", description="The dataset as analysed").path
