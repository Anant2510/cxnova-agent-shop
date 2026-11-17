"""Data connectors with local providers: keyword data, analytics, behaviour, webinar attendance.

Local providers read the CSV formats in sample_data/ (also the export formats teams already have:
Search Console query exports, GA4/Adobe exports, Clarity/Hotjar event exports, Zoom/ON24 reports).
External API providers live in *_providers.py modules and return the same DataFrame shapes.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

import pandas as pd

from .base import Connector, connector

# ----------------------------------------------------------------------------- keyword data

KEYWORD_COLUMNS = ["query", "page", "clicks", "impressions", "ctr", "position"]


class KeywordData(Connector):
    def queries(self, topic: str | None = None, site: str | None = None, limit: int = 1000) -> pd.DataFrame:
        raise NotImplementedError


@connector("keyword_data", "none")
class NoKeywordData(KeywordData):
    """No first-party keyword data - agents return clearly-flagged model estimates."""

    @property
    def available(self) -> bool:
        return False

    def queries(self, topic: str | None = None, site: str | None = None, limit: int = 1000) -> pd.DataFrame:
        return pd.DataFrame(columns=KEYWORD_COLUMNS)


@connector("keyword_data", "csv")
class CsvKeywordData(KeywordData):
    """Search Console style CSV export (query, page, clicks, impressions, ctr, position)."""

    def queries(self, topic: str | None = None, site: str | None = None, limit: int = 1000) -> pd.DataFrame:
        df = pd.read_csv(self.path("path", "sample_data/keywords/gsc_queries.csv"))
        df.columns = [c.strip().lower() for c in df.columns]
        if "ctr" not in df and {"clicks", "impressions"} <= set(df.columns):
            df["ctr"] = (df["clicks"] / df["impressions"].clip(lower=1)).round(4)
        if topic:
            toks = {t for t in topic.lower().split() if len(t) > 2}
            mask = df["query"].str.lower().apply(lambda q: bool(toks & set(q.split())))
            if mask.any():
                df = df[mask]
        return df.sort_values("impressions", ascending=False).head(limit).reset_index(drop=True)


# ----------------------------------------------------------------------------- analytics


def _d(v: Any) -> date:
    return v if isinstance(v, date) else datetime.fromisoformat(str(v)).date()


class Analytics(Connector):
    """Period-level web analytics. All methods return tidy DataFrames."""

    def daily(self, start: date, end: date, domain: str | None = None) -> pd.DataFrame: raise NotImplementedError
    def pages(self, start: date, end: date, domain: str | None = None, limit: int = 50) -> pd.DataFrame: raise NotImplementedError
    def channels(self, start: date, end: date, domain: str | None = None) -> pd.DataFrame: raise NotImplementedError

    def channels_daily(self, start: date, end: date, domain: str | None = None) -> pd.DataFrame:
        """Per-day channel rows [date, channel, sessions, users, conversions, revenue]. Generic default:
        one channels() call per day (providers with a native daily breakdown override this)."""
        from datetime import timedelta

        frames = []
        day, last = _d(start), _d(end)
        while day <= last:
            df = self.channels(day, day, domain=domain)
            if df is not None and len(df):
                df = df.copy()
                df.insert(0, "date", day)
                frames.append(df)
            day += timedelta(days=1)
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=["date", "channel", "sessions", "users", "conversions", "revenue"])

    def date_bounds(self) -> tuple[date, date] | None:
        return None


@connector("analytics", "csv")
class CsvAnalytics(Analytics):
    """Folder of daily.csv / pages.csv / channels.csv (GA4 or Adobe Analytics exports)."""

    def _load(self, name: str) -> pd.DataFrame:
        df = pd.read_csv(self.path("path", "sample_data/analytics") / f"{name}.csv")
        df["date"] = pd.to_datetime(df["date"]).dt.date
        return df

    def date_bounds(self) -> tuple[date, date]:
        df = self._load("daily")
        return min(df["date"]), max(df["date"])

    def daily(self, start: date, end: date, domain: str | None = None) -> pd.DataFrame:
        df = self._load("daily")
        return df[(df["date"] >= _d(start)) & (df["date"] <= _d(end))].sort_values("date").reset_index(drop=True)

    def pages(self, start: date, end: date, domain: str | None = None, limit: int = 50) -> pd.DataFrame:
        df = self._load("pages")
        df = df[(df["date"] >= _d(start)) & (df["date"] <= _d(end))]
        agg = df.groupby(["page_path", "page_title"], as_index=False)[["sessions", "users", "engaged_sessions", "engagement_time_s", "conversions"]].sum()
        agg["engagement_rate"] = (agg["engaged_sessions"] / agg["sessions"].clip(lower=1)).round(4)
        agg["avg_engagement_time_s"] = (agg["engagement_time_s"] / agg["sessions"].clip(lower=1)).round(1)
        agg["conversion_rate"] = (agg["conversions"] / agg["sessions"].clip(lower=1)).round(4)
        return agg.sort_values("sessions", ascending=False).head(limit).reset_index(drop=True)

    def channels(self, start: date, end: date, domain: str | None = None) -> pd.DataFrame:
        df = self._load("channels")
        df = df[(df["date"] >= _d(start)) & (df["date"] <= _d(end))]
        agg = df.groupby("channel", as_index=False)[["sessions", "users", "conversions", "revenue"]].sum()
        agg["conversion_rate"] = (agg["conversions"] / agg["sessions"].clip(lower=1)).round(4)
        return agg.sort_values("sessions", ascending=False).reset_index(drop=True)

    def channels_daily(self, start: date, end: date) -> pd.DataFrame:
        df = self._load("channels")
        return df[(df["date"] >= _d(start)) & (df["date"] <= _d(end))].reset_index(drop=True)


# ----------------------------------------------------------------------------- behaviour

EVENT_COLUMNS = ["session_id", "ts", "page_url", "device", "event_type", "x", "y", "selector", "element_text", "tag",
                 "is_interactive", "scroll_pct", "viewport_w", "viewport_h", "page_h"]


class Behaviour(Connector):
    """Normalised on-page interaction events (common schema across Clarity, Hotjar, Contentsquare ...)."""

    def events(self, page_url: str | None = None) -> pd.DataFrame: raise NotImplementedError
    def pages(self) -> list[str]: raise NotImplementedError


@connector("behaviour", "csv")
class CsvBehaviour(Behaviour):
    """Event export in the common schema (session_id, ts, event_type, x, y, selector, scroll_pct ...)."""

    def _df(self) -> pd.DataFrame:
        df = pd.read_csv(self.path("path", "sample_data/behaviour/heatmap_events.csv"))
        df["ts"] = pd.to_datetime(df["ts"], utc=True, format="mixed")
        return df

    def pages(self) -> list[str]:
        return sorted(self._df()["page_url"].dropna().unique().tolist())

    def events(self, page_url: str | None = None) -> pd.DataFrame:
        df = self._df()
        if page_url:
            norm = page_url.rstrip("/")
            sub = df[df["page_url"].str.rstrip("/") == norm]
            if sub.empty:  # tolerate path-only or scheme differences
                path = norm.split("://")[-1].split("/", 1)[-1]
                sub = df[df["page_url"].str.contains(path, regex=False)] if path else sub
            df = sub
        return df.sort_values(["session_id", "ts"]).reset_index(drop=True)


# ----------------------------------------------------------------------------- webinar


class Webinar(Connector):
    def info(self, webinar_id: str | None = None) -> dict[str, Any]: raise NotImplementedError
    def attendees(self, webinar_id: str | None = None) -> pd.DataFrame: raise NotImplementedError
    def transcript(self, webinar_id: str | None = None) -> str | None: raise NotImplementedError


@connector("webinar", "csv")
class CsvWebinar(Webinar):
    """Attendance CSV + transcript + info.json (works with Zoom/Teams/ON24/GoToWebinar exports)."""

    def info(self, webinar_id: str | None = None) -> dict[str, Any]:
        p = self.path("path", "sample_data/webinar") / "info.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def attendees(self, webinar_id: str | None = None) -> pd.DataFrame:
        return pd.read_csv(self.path("path", "sample_data/webinar") / "attendees.csv")

    def transcript(self, webinar_id: str | None = None) -> str | None:
        p = self.path("path", "sample_data/webinar") / "transcript.txt"
        return p.read_text(encoding="utf-8") if p.exists() else None
