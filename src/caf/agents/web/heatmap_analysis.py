"""Heatmap Analysis - behavioural metrics computed in pandas from any behaviour source, tied to real components.

Independence: events come from the `behaviour` connector in a common schema (session_id, ts, event_type, x, y,
selector, element_text, tag, is_interactive, scroll_pct, viewport and page height), so Microsoft Clarity, Hotjar,
Contentsquare, FullStory, Matomo or a home-grown collector are interchangeable.

Stages: load -> dom -> metrics -> insights -> report
  load     events for the page (+ device / date filters)
  dom      fetch the page (or use pasted HTML) and map every selector to its element text, control type
           (semantic / scripted / none) and component label; with a browser, measure the real layout of sections
           and CTAs (Playwright) for exposure and false-bottom attribution
  metrics  click density per selector, CTA CTR (raw and exposure-adjusted), scroll-reach curve in 10% buckets
           with false-bottom detection, rage clicks (>= 3 clicks on one selector within ~2 s), dead clicks (clicks on
           elements that are neither semantic nor scripted controls), attention/dwell by depth band, device split
  insights the model writes insights + test hypotheses from the computed metrics only (offline: rule-based)
  report   click-map PNG (page coordinates, screenshot background when a browser is available) + scroll-curve PNG
Hypotheses are emitted as [{hypothesis, evidence, metric}] for the experiment_ideation agent.
"""

from __future__ import annotations

import base64
import io
import json
import re
from typing import Any, Literal

import numpy as np
import pandas as pd
from bs4 import BeautifulSoup, Tag
from pydantic import BaseModel, Field

from ...core import AgentKind, AgentSpec, BaseAgent, Category, Check, RunContext, Tier, register
from ...core.handoff import Finding

SEMANTIC_TAGS = {"a", "button", "input", "select", "textarea", "summary", "label", "option", "details"}
SEMANTIC_ROLES = {"button", "link", "tab", "checkbox", "radio", "switch", "menuitem", "option", "combobox", "slider", "treeitem"}
CTA_CLASS = re.compile(r"(^|[\s_-])(btn|button|cta)([\s_-]|$)", re.I)
GENERIC_CLASSES = {"section", "wrap", "section-sea", "thin", "container", "row", "col"}
# chart tokens (reference data-viz palette)
INK, INK2, MUTED, GRID, AXIS, SURFACE = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7", "#fcfcfb"
SLOT1, SLOT2, CRITICAL, SERIOUS = "#2a78d6", "#eb6834", "#d03b3b", "#ec835a"
SEQ_BLUE = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]


def degraded_update(state: dict[str, Any], reasons: list[str], **extra: Any) -> dict[str, Any]:
    """Provenance update for one stage (the core's merge_provenance reducer accumulates degraded_reasons)."""
    out: dict[str, Any] = dict(extra)
    if reasons:
        out["degraded_reasons"] = list(dict.fromkeys(reasons))
    return out

# ============================================================================ DOM helpers (pure)


def scripted_selectors(soup: BeautifulSoup) -> set[str]:
    """Selectors queried by inline scripts that attach click handlers (div 'buttons' wired up in JS)."""
    out: set[str] = set()
    for s in soup.find_all("script"):
        code = s.string or s.get_text() or ""
        if not re.search(r"addEventListener\(\s*['\"](click|pointerdown|mousedown|touchstart)|\.onclick\s*=", code):
            continue
        out |= set(re.findall(r"querySelector(?:All)?\(\s*['\"]([^'\"]+)['\"]\s*\)", code))
        out |= {f"#{i}" for i in re.findall(r"getElementById\(\s*['\"]([^'\"]+)['\"]\s*\)", code)}
    return out


def control_type(el: Tag, soup: BeautifulSoup, scripted: set[str]) -> str:
    """semantic (native/ARIA control), scripted (non-semantic element with a JS click handler) or none."""
    for node in [el] + [p for p in el.parents if isinstance(p, Tag)][:4]:
        if node.name in SEMANTIC_TAGS and (node.name != "a" or node.get("href")):
            return "semantic"
        if (node.get("role") or "") in SEMANTIC_ROLES or node.get("onclick"):
            return "semantic" if node.get("role") else "scripted"
    for sel in scripted:
        try:
            if el in soup.select(sel):
                return "scripted"
        except Exception:
            continue
    return "none"


def _humanize(token: str) -> str:
    return re.sub(r"[-_]+", " ", token).strip().capitalize()


def component_label(el: Tag, soup: BeautifulSoup) -> str:
    """Nearest landmark/section with its heading, e.g. "Plans section ('Compare plans')"."""
    for node in [el] + [p for p in el.parents if isinstance(p, Tag)]:
        if node.name not in ("section", "header", "footer", "nav", "aside", "article", "form", "figure"):
            continue
        heading = ""
        if node.get("aria-labelledby"):
            ref = soup.find(id=node["aria-labelledby"].split()[0])
            heading = ref.get_text(" ", strip=True) if ref else ""
        heading = heading or (node.get("aria-label") or "")
        if not heading:
            h = node.find(re.compile(r"^h[1-4]$"))
            heading = h.get_text(" ", strip=True) if h else ""
        cls = [c for c in node.get("class", []) if c not in ("section", "wrap", "section-sea", "thin")]
        kind = _humanize(node.get("id") or (cls[0] if cls else node.name))
        kind = kind if kind.lower().endswith(("section", "band", "header", "footer", "form", "nav")) else f"{kind} section"
        return f"{kind} ('{heading[:60]}')" if heading else kind
    return "Page body"


def describe_selectors(html: str | None, selectors: list[str]) -> dict[str, dict[str, Any]]:
    """Map event selectors to DOM facts. Unknown selectors (or no DOM) get control 'unknown'."""
    out: dict[str, dict[str, Any]] = {}
    if not html:
        return {s: {"found": False, "component": "", "text": "", "tag": "", "control": "unknown", "hidden": False, "styled_cta": False} for s in selectors}
    soup = BeautifulSoup(html, "lxml")
    scripted = scripted_selectors(soup)
    for sel in selectors:
        try:
            el = soup.select_one(sel)
        except Exception:
            el = None
        if el is None:
            out[sel] = {"found": False, "component": "", "text": "", "tag": "", "control": "unknown", "hidden": False, "styled_cta": False}
            continue
        hidden = any(isinstance(p, Tag) and p.has_attr("hidden") for p in [el] + list(el.parents))
        out[sel] = {"found": True, "component": component_label(el, soup), "text": " ".join(el.get_text(" ").split())[:80] or (el.get("alt") or ""),
                    "tag": el.name, "control": control_type(el, soup, scripted), "hidden": hidden,
                    "styled_cta": bool(CTA_CLASS.search(" ".join(el.get("class", []))))}
    return out


def find_ctas(html: str | None) -> list[dict[str, Any]]:
    """Calls to action in the DOM: links/buttons styled as buttons or with 'cta' in their id/class."""
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    out = []
    for el in soup.find_all(["a", "button"]):
        ident = " ".join(el.get("class", [])) + " " + (el.get("id") or "")
        if not (CTA_CLASS.search(ident) or "cta" in ident.lower()):
            continue
        if el.get("id"):
            sel = f"#{el['id']}"
        else:
            anc = next((p for p in el.parents if isinstance(p, Tag) and (p.get("id") or (p.name == "section" and p.get("class")))), None)
            prefix = f"#{anc['id']} " if anc is not None and anc.get("id") else (f".{[c for c in anc.get('class', []) if c not in ('section', 'wrap')][0]} " if anc is not None and [c for c in anc.get('class', []) if c not in ('section', 'wrap')] else "")
            sel = prefix + el.name + "".join(f".{c}" for c in el.get("class", [])[:2])
        hidden = any(isinstance(p, Tag) and p.has_attr("hidden") for p in [el] + list(el.parents))
        out.append({"selector": sel.strip(), "label": " ".join(el.get_text(" ").split())[:60], "component": component_label(el, soup),
                    "hidden": hidden, "styled_cta": bool(CTA_CLASS.search(" ".join(el.get("class", []))))})
    seen, uniq = set(), []
    for c in out:
        if c["selector"] not in seen:
            seen.add(c["selector"])
            uniq.append(c)
    return uniq


def match_selector(event_sel: str, cta_sel: str) -> bool:
    """Event selectors and DOM-derived selectors may differ in detail (.promo-band .btn-light vs .promo-band a.btn.btn-light)."""
    if event_sel == cta_sel:
        return True
    norm = lambda s: re.sub(r"\b(a|button|div|span)(?=[.#])", "", s).replace(" ", "")
    a, b = norm(event_sel), norm(cta_sel)
    ta, tb = set(re.findall(r"[#.][\w-]+", a)), set(re.findall(r"[#.][\w-]+", b))
    return bool(ta) and ta <= tb


# ============================================================================ metrics (pure)


def epoch_seconds(ts: pd.Series) -> np.ndarray:
    """Seconds since the epoch, independent of the datetime resolution (pandas 3 parses to us, not ns)."""
    t = pd.to_datetime(ts, utc=True, format="mixed")
    return (t - pd.Timestamp("1970-01-01", tz="UTC")).dt.total_seconds().to_numpy()


def sessions_frame(ev: pd.DataFrame) -> pd.DataFrame:
    """One row per session: device, max scroll reach, dwell seconds, clicks."""
    g = ev.groupby("session_id")
    s = pd.DataFrame({"device": g["device"].first(), "max_scroll": g["scroll_pct"].max().astype(float),
                      "start": g["ts"].min(), "end": g["ts"].max(), "clicks": g["event_type"].apply(lambda x: int((x == "click").sum()))})
    s["dwell_s"] = (s["end"] - s["start"]).dt.total_seconds()
    return s


def reach_curve(max_scroll: pd.Series, step: int = 10) -> list[dict[str, float]]:
    return [{"depth": d, "reach": float((max_scroll >= d).mean()) if len(max_scroll) else 0.0} for d in range(0, 101, step)]


def detect_false_bottom(max_scroll: pd.Series, min_drop_pp: float = 15.0, step: int = 5) -> dict[str, Any]:
    """Largest single-step loss on the 5%-resolution reach curve, if it stands out from the typical step.

    Steps inside the first screen (reach still 100%) and beyond 90% depth (the natural end-of-page decline) are
    ignored. Detected when the drop is >= min_drop_pp AND at least 2.5x the median of the other steps.
    """
    curve = reach_curve(max_scroll, step)
    drops = []
    for a, b in zip(curve, curve[1:]):
        if b["depth"] > 90 or (a["reach"] >= 0.999 and b["reach"] >= 0.999):
            continue
        drops.append({"from": a["depth"], "to": b["depth"], "pp": (a["reach"] - b["reach"]) * 100, "before": a["reach"], "after": b["reach"]})
    if not drops:
        return {"detected": False}
    best = max(drops, key=lambda d: d["pp"])
    others = [d["pp"] for d in drops if d is not best]
    typical = float(np.median(others)) if others else 0.0
    detected = best["pp"] >= min_drop_pp and best["pp"] >= 2.5 * max(typical, 1.0)
    return {"detected": bool(detected), "depth_from": int(best["from"]), "depth_to": int(best["to"]), "drop_pp": round(best["pp"], 1),
            "typical_drop_pp": round(typical, 1), "reach_before": round(best["before"], 3), "reach_after": round(best["after"], 3)}


def rage_episodes(clicks: pd.DataFrame, window_s: float = 2.0, min_clicks: int = 3) -> pd.DataFrame:
    """Bursts of >= min_clicks clicks on the same selector within window_s seconds, per session."""
    rows = []
    for (sid, sel), g in clicks.sort_values("ts").groupby(["session_id", "selector"]):
        t = epoch_seconds(g["ts"])
        i = 0
        while i < len(t):
            j = i
            while j + 1 < len(t) and t[j + 1] - t[i] <= window_s:
                j += 1
            if j - i + 1 >= min_clicks:
                rows.append({"session_id": sid, "selector": sel, "clicks": j - i + 1, "span_s": round(float(t[j] - t[i]), 2)})
                i = j + 1
            else:
                i += 1
    return pd.DataFrame(rows, columns=["session_id", "selector", "clicks", "span_s"])


def click_table(clicks: pd.DataFrame, n_sessions: int, dom: dict[str, dict[str, Any]], rage: pd.DataFrame,
                page_h: dict[str, float]) -> list[dict[str, Any]]:
    total = len(clicks)
    rows = []
    for sel, g in clicks.groupby("selector"):
        info = dom.get(sel, {})
        control = info.get("control", "unknown")
        if control == "unknown":  # fall back to the collector's own flag
            control = "semantic" if int(g["is_interactive"].max()) == 1 else "none"
        r = rage[rage["selector"] == sel] if len(rage) else rage
        depth = [float(y) / page_h.get(dev, float(g["page_h"].median())) * 100 for y, dev in zip(g["y"], g["device"]) if pd.notna(y)]
        rows.append({"selector": sel, "component": info.get("component") or "", "element_text": info.get("text") or str(g["element_text"].dropna().iloc[0] if g["element_text"].notna().any() else ""),
                     "tag": info.get("tag") or str(g["tag"].iloc[0]), "control": control, "clicks": int(len(g)),
                     "sessions": int(g["session_id"].nunique()), "click_share": len(g) / total if total else 0.0,
                     "ctr": g["session_id"].nunique() / n_sessions if n_sessions else 0.0,
                     "dead_clicks": int(len(g)) if control == "none" else 0,
                     "rage_episodes": int(len(r)), "rage_sessions": int(r["session_id"].nunique()) if len(r) else 0,
                     "median_x": float(g["x"].median()), "median_y": float(g["y"].median()),
                     "depth_pct": round(float(np.median(depth)), 1) if depth else None})
    return sorted(rows, key=lambda r: -r["clicks"])


def cta_table(ctas: list[dict[str, Any]], clicks: pd.DataFrame, sess: pd.DataFrame, page_h: dict[str, float],
              layout: dict[str, dict[str, float]] | None, ignored_ctr: float, low_ctr: float) -> list[dict[str, Any]]:
    """Raw CTR (sessions clicking / sessions) and exposure-adjusted CTR (/ sessions whose scroll reached the CTA)."""
    n = len(sess)
    rows = []
    selectors = sorted(clicks["selector"].dropna().unique()) if len(clicks) else []
    for c in ctas:
        evs = [s for s in selectors if match_selector(s, c["selector"])]
        g = clicks[clicks["selector"].isin(evs)] if evs else clicks.iloc[0:0]
        clicking = int(g["session_id"].nunique()) if len(g) else 0
        depth_by_dev: dict[str, float] = {}
        if len(g):
            for dev, gd in g.groupby("device"):
                depth_by_dev[dev] = float(gd["y"].median()) / page_h.get(dev, float(gd["page_h"].median())) * 100
        if layout and c["selector"] in layout:
            depth_by_dev = {**{d: layout[c["selector"]]["depth_pct"] for d in ("desktop",)}, **{k: v for k, v in depth_by_dev.items() if k != "desktop"}}
        exposed = 0
        for dev, depth in depth_by_dev.items():
            exposed += int((sess.loc[sess["device"] == dev, "max_scroll"] >= depth).sum())
        if depth_by_dev:  # devices without a measured position: assume the median depth
            med = float(np.median(list(depth_by_dev.values())))
            exposed += int((sess.loc[~sess["device"].isin(list(depth_by_dev)), "max_scroll"] >= med).sum())
        ctr = clicking / n if n else 0.0
        ctr_exp = clicking / exposed if exposed else None
        if c["hidden"] and clicking == 0:
            status = "hidden"
        elif ctr < ignored_ctr and (ctr_exp is None or ctr_exp < 0.08):
            status = "ignored"
        elif ctr < low_ctr:
            status = "low"
        else:
            status = "ok"
        button_like = (layout or {}).get(c["selector"], {}).get("button_like")
        rows.append({**c, "button_like": button_like, "event_selectors": evs, "clicks": int(len(g)), "sessions": clicking, "ctr": ctr, "exposed_sessions": exposed,
                     "ctr_exposed": ctr_exp, "depth_pct": round(float(np.median(list(depth_by_dev.values()))), 1) if depth_by_dev else None,
                     "status": status})
    return sorted(rows, key=lambda r: -r["ctr"])


def attention_by_band(ev: pd.DataFrame, band: int = 10) -> list[dict[str, float]]:
    """Seconds each depth band was on screen, averaged per session (viewport window = [reach - viewport share, reach])."""
    secs = np.zeros(100 // band)
    sessions = ev["session_id"].nunique() or 1
    for _, g in ev.sort_values("ts").groupby("session_id"):
        g = g[g["event_type"].isin(["pageview", "scroll", "click", "leave"])]
        ts = epoch_seconds(g["ts"])
        reach = g["scroll_pct"].to_numpy(dtype=float)
        vh = (g["viewport_h"] / g["page_h"] * 100).to_numpy(dtype=float)
        for i in range(len(ts) - 1):
            dt = max(0.0, ts[i + 1] - ts[i])
            top, bottom = max(0.0, reach[i] - vh[i]), min(100.0, reach[i])
            for b in range(len(secs)):
                lo, hi = b * band, (b + 1) * band
                overlap = max(0.0, min(hi, bottom) - max(lo, top))
                if overlap > 0:
                    secs[b] += dt * overlap / band
    return [{"band": f"{b * band}-{(b + 1) * band}%", "seconds": round(float(secs[b] / sessions), 2)} for b in range(len(secs))]


def num(value: Any, digits: int | None = None) -> float | None:
    """float or None for NaN/None/inf (JSON-safe)."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if np.isnan(f) or np.isinf(f):
        return None
    return round(f, digits) if digits is not None else f


def device_split(sess: pd.DataFrame, clicks: pd.DataFrame, rage: pd.DataFrame) -> list[dict[str, Any]]:
    """Per-device sessions, scroll reach, dwell and click behaviour; NaN-safe (a device whose scroll values are all NaN
    reports None instead of NaN)."""
    n = len(sess)
    out = []
    for dev, g in sess.groupby("device"):
        c = clicks[clicks["device"] == dev] if len(clicks) else clicks
        r = rage[rage["session_id"].isin(g.index)] if len(rage) else rage
        out.append({"device": str(dev), "sessions": int(len(g)), "share": len(g) / n if n else 0.0,
                    "median_max_scroll": num(g["max_scroll"].median()), "median_dwell_s": num(g["dwell_s"].median(), 1),
                    "clicks_per_session": round(len(c) / len(g), 2) if len(g) else 0.0,
                    "dead_click_share": round(float((c["is_interactive"] == 0).mean()), 3) if len(c) else 0.0,
                    "rage_sessions": int(r["session_id"].nunique()) if len(r) else 0})
    return sorted(out, key=lambda d: -d["sessions"])


AGGREGATE_EVENTS = {"rage_click": "Rage clicks", "dead_click": "Dead clicks", "error_click": "Error clicks", "quickback_click": "Quick-back clicks",
                    "excessive_scroll": "Excessive scrolling", "script_error": "Script errors"}


def is_aggregated(ev: pd.DataFrame, provider: str | None) -> bool:
    """Aggregated exports (Microsoft Clarity) carry counts, not raw clicks with coordinates and selectors."""
    return bool(getattr(ev, "attrs", {}).get("aggregated")) or (provider or "").lower() == "clarity"


def aggregate_metrics(ev: pd.DataFrame) -> dict[str, Any]:
    """Counts, sessions affected and session shares per aggregated behaviour metric, overall and per device (pure)."""
    pv = ev[ev["event_type"] == "pageview"]
    sessions = int(pv["session_id"].nunique())
    dev_sessions = {str(d): int(g["session_id"].nunique()) for d, g in pv.groupby(pv["device"].fillna("unknown"))}
    metrics = []
    for et, label in AGGREGATE_EVENTS.items():
        sub = ev[ev["event_type"] == et]
        if sub.empty:
            continue
        by_dev = {}
        for d, g in sub.groupby(sub["device"].fillna("unknown")):
            ds = dev_sessions.get(str(d), 0)
            by_dev[str(d)] = {"occurrences": int(len(g)), "sessions": int(g["session_id"].nunique()),
                              "session_share": round(g["session_id"].nunique() / ds, 4) if ds else None}
        affected = int(sub["session_id"].nunique())
        metrics.append({"metric": et, "label": label, "occurrences": int(len(sub)), "sessions": affected,
                        "session_share": round(affected / sessions, 4) if sessions else None, "by_device": by_dev})
    scroll = ev[(ev["event_type"] == "scroll") & ev["scroll_pct"].notna()]
    avg_scroll = {str(d): num(g["scroll_pct"].mean(), 1) for d, g in scroll.groupby(scroll["device"].fillna("unknown"))}
    return {"sessions": sessions, "device_sessions": dev_sessions, "metrics": metrics, "avg_scroll_depth": avg_scroll}


def aggregate_insights(agg: dict[str, Any], min_sessions: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Rule-based insights + page-level hypotheses from aggregated counts (no selectors are known)."""
    advice = {"rage_click": ("high", "find the elements behind them in session recordings filtered by rage clicks and make them respond",
                             "rage-click session share"),
              "dead_click": ("medium", "make the elements visitors click on actionable (or remove their clickable look)", "dead-click session share"),
              "error_click": ("high", "fix the JavaScript errors triggered by clicks", "error-click session share"),
              "script_error": ("high", "fix the script errors on the page", "script-error session share"),
              "quickback_click": ("medium", "make link destinations match what the link promises", "quick-back session share"),
              "excessive_scroll": ("low", "surface the content visitors scroll back and forth to find", "excessive-scroll session share")}
    ins, hyps = [], []
    for m in agg["metrics"]:
        if m["sessions"] < min_sessions:
            continue
        sev, fix, metric = advice[m["metric"]]
        share = f"{m['session_share'] * 100:.1f}%" if m["session_share"] is not None else "n/a"
        devs = ", ".join(f"{d} {v['session_share'] * 100:.1f}%" for d, v in m["by_device"].items() if v["session_share"] is not None)
        ins.append({"title": f"{m['label']} in {share} of sessions (Clarity aggregate)", "severity": sev, "selector": None,
                    "detail": f"{m['occurrences']} {m['label'].lower()} across {m['sessions']} of {agg['sessions']} sessions"
                              + (f" ({devs})" if devs else "") + ". The export has no selectors, so the elements are not identified."})
        hyps.append({"hypothesis": f"If we {fix}, the {metric} will fall and more visitors will continue to a quote.",
                     "evidence": f"{m['occurrences']} {m['label'].lower()} in {m['sessions']} sessions ({share})", "metric": metric, "selector": None})
    return ins, hyps


def nearest_component(depth_from: float, depth_to: float, positions: list[dict[str, Any]]) -> str:
    """Components at the drop: measured sections overlapping the depth range (e.g. 'Promo band -> Plans section'),
    or the click-derived component nearest to it."""
    if not positions:
        return ""
    measured = [p for p in positions if "bottom_pct" in p]
    if measured:
        hits = sorted([p for p in measured if p["top_pct"] < depth_to and p["bottom_pct"] > depth_from], key=lambda p: p["top_pct"])
        if hits:
            return " -> ".join(p["label"] for p in hits[:2])
    mid = (depth_from + depth_to) / 2
    return min(positions, key=lambda p: abs(p["top_pct"] - mid))["label"]


def section_label(cls: str, heading: str) -> str:
    tokens = [t for t in (cls or "").split() if t.lower() not in GENERIC_CLASSES]
    kind = _humanize(tokens[0]) if tokens else "Section"
    kind = kind if kind.lower().endswith(("section", "band", "header", "footer", "hero")) else f"{kind} section"
    heading = (heading or "").strip()
    return f"{kind} ('{heading[:50]}')" if heading and heading.lower() != (cls or "").lower() else kind


def rule_hypotheses(clicks: list[dict[str, Any]], ctas: list[dict[str, Any]], fb: dict[str, Any], devices: list[dict[str, Any]],
                    min_rage_sessions: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Deterministic insights + hypotheses from the computed metrics (used offline and as a coverage floor)."""
    insights, hyps = [], []
    for c in clicks:
        if c["rage_sessions"] >= min_rage_sessions:
            insights.append({"title": f"Rage clicks on {c['element_text'] or c['selector']}", "severity": "high", "selector": c["selector"],
                             "detail": f"{c['rage_sessions']} sessions clicked {c['selector']} in {c['component'] or 'the page'} 3+ times within seconds "
                                       f"({c['clicks']} clicks in total) - the control does not respond as expected."})
            hyps.append({"hypothesis": f"If {c['element_text'] or c['selector']} in {c['component'] or 'the page'} responds immediately with its content, rage clicks will disappear and more visitors will continue to a quote.",
                         "evidence": f"{c['rage_episodes']} rage-click bursts from {c['rage_sessions']} sessions on {c['selector']}",
                         "metric": "rage-click sessions on the control; quote starts from that section", "selector": c["selector"]})
    for c in clicks:
        if c["control"] == "none" and c["sessions"] >= 10:
            insights.append({"title": f"Dead clicks on {c['selector']}", "severity": "medium", "selector": c["selector"],
                             "detail": f"{c['clicks']} clicks from {c['sessions']} sessions ({c['ctr'] * 100:.1f}% of visitors) land on a non-interactive "
                                       f"{c['tag']} in {c['component'] or 'the page'}."})
            hyps.append({"hypothesis": f"If {c['selector']} becomes a link to the quote flow (or loses its clickable look), wasted clicks turn into quote starts.",
                         "evidence": f"{c['clicks']} dead clicks from {c['sessions']} sessions", "metric": "quote-start CTR from the hero", "selector": c["selector"]})
    scripted: dict[str, list[dict[str, Any]]] = {}
    for c in clicks:
        if c["control"] == "scripted":
            scripted.setdefault(c["component"] or c["tag"], []).append(c)
    for comp, items in scripted.items():
        if sum(i["sessions"] for i in items) >= 10:
            sels = ", ".join(i["selector"] for i in items)
            insights.append({"title": f"Non-semantic controls in {comp}", "severity": "medium", "selector": items[0]["selector"],
                             "detail": f"{sels} are scripted <{items[0]['tag']}> elements without a button/tab role or tabindex, so keyboard and "
                                       f"screen-reader users cannot operate them ({sum(i['clicks'] for i in items)} clicks)."})
    if fb.get("detected"):
        comp = fb.get("component") or "a section boundary"
        insights.append({"title": f"False bottom at {fb['depth_from']}-{fb['depth_to']}% scroll depth", "severity": "high", "selector": None,
                         "detail": f"Reach falls from {fb['reach_before'] * 100:.0f}% to {fb['reach_after'] * 100:.0f}% of sessions ({fb['drop_pp']:.0f} pp, typical step "
                                   f"{fb['typical_drop_pp']:.0f} pp) around {comp}; content below it is seen by few visitors."})
        hyps.append({"hypothesis": f"If the page signals that more content follows {comp} (reduce the full-width break, add a 'compare plans' anchor or a visual cue), more visitors will reach pricing and the FAQ.",
                     "evidence": f"{fb['drop_pp']:.0f} pp drop in scroll reach between {fb['depth_from']}% and {fb['depth_to']}%",
                     "metric": f"scroll reach at {fb['depth_to'] + 10}% depth; plan-section CTR", "selector": None})
    for c in ctas:
        if c["status"] == "ignored":
            depth = c.get("depth_pct")
            where = f" at {depth:.0f}% depth" if depth is not None else ""
            styled = " (styled as a plain text link)" if c.get("button_like") is False else ""
            below_fb = bool(fb.get("detected") and depth is not None and depth > fb["depth_to"])
            insights.append({"title": f"Ignored CTA: {c['label'] or c['selector']}", "severity": "medium", "selector": c["selector"],
                             "detail": f"{c['selector']}{styled}{where} is clicked by {c['ctr'] * 100:.1f}% of sessions ({c['sessions']} sessions)"
                                       + (f"; only {c['exposed_sessions']} sessions scrolled far enough to see it" if c.get("exposed_sessions") else "")
                                       + (f" - it sits below the {fb['depth_from']}-{fb['depth_to']}% scroll drop-off" if below_fb else "") + "."})
            if below_fb:
                change = f"'{c['label']}' is repeated above the false bottom (before {fb['depth_from']}% depth, e.g. beside the plan comparison)"
            elif c.get("button_like") is False:
                change = f"'{c['label']}' is styled as a button"
            else:
                change = f"'{c['label']}' gets stronger contrast and value-led copy"
            hyps.append({"hypothesis": f"If {change}, more of the visitors who are unsure will click it and book help.",
                         "evidence": f"CTR {c['ctr'] * 100:.1f}%" + (f", {c['ctr_exposed'] * 100:.1f}% of the {c['exposed_sessions']} sessions that reached it" if c.get("ctr_exposed") is not None else "")
                                     + (f"; {(1 - fb['reach_after']) * 100:.0f}% of sessions never scroll past {fb['depth_to']}%" if below_fb else ""),
                         "metric": f"{c['selector']} CTR", "selector": c["selector"]})
    if len(devices) >= 2:
        a, b = devices[0], devices[1]
        if a["median_max_scroll"] is not None and b["median_max_scroll"] is not None and abs(a["median_max_scroll"] - b["median_max_scroll"]) >= 10:
            lo, hi = sorted([a, b], key=lambda d: d["median_max_scroll"])
            insights.append({"title": f"{lo['device'].capitalize()} visitors scroll less", "severity": "low", "selector": None,
                             "detail": f"Median reach {lo['median_max_scroll']:.0f}% on {lo['device']} vs {hi['median_max_scroll']:.0f}% on {hi['device']}."})
    return insights, hyps


# ============================================================================ charts


def _png(fig: Any) -> bytes:
    import matplotlib.pyplot as plt

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, facecolor=SURFACE, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def click_map_png(clicks: pd.DataFrame, width: float, height: float, marks: list[dict[str, Any]], sections: list[dict[str, Any]],
                  background: bytes | None, title: str) -> bytes:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import LinearSegmentedColormap
    from scipy.ndimage import gaussian_filter

    fig_w = 6.4
    fig, ax = plt.subplots(figsize=(fig_w, max(4.0, fig_w * height / width)))
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    if background is not None:
        from PIL import Image

        img = Image.open(io.BytesIO(background)).convert("L")
        ax.imshow(np.asarray(img), cmap="gray", extent=(0, width, height, 0), alpha=0.35, aspect="auto", zorder=0)
    else:
        for s in sections:
            ax.axhspan(s["top_pct"] / 100 * height, s.get("bottom_pct", s["top_pct"] + 3) / 100 * height, color=GRID, alpha=0.35, zorder=0)
            ax.text(8, s["top_pct"] / 100 * height + 14, s["label"][:48], fontsize=7, color=INK2, va="top", zorder=1)
    if len(clicks):
        bins_x, bins_y = 64, max(32, int(64 * height / width))
        hist, _, _ = np.histogram2d(clicks["y"].clip(0, height), clicks["x"].clip(0, width), bins=[bins_y, bins_x], range=[[0, height], [0, width]])
        dens = gaussian_filter(hist, sigma=1.4)
        if dens.max() > 0:
            dens = dens / dens.max()
            cmap = LinearSegmentedColormap.from_list("seq_blue", SEQ_BLUE)
            masked = np.ma.masked_less(dens, 0.04)
            ax.imshow(masked, cmap=cmap, extent=(0, width, height, 0), alpha=0.85, aspect="auto", zorder=2, interpolation="bilinear")
    for m in marks:
        ax.scatter([m["x"]], [m["y"]], s=70, color=m["color"], edgecolor=SURFACE, linewidth=2, zorder=4)
        ax.annotate(m["label"], (m["x"], m["y"]), xytext=(9, -4), textcoords="offset points", fontsize=8, color=INK, fontweight="bold", zorder=5,
                    bbox={"boxstyle": "round,pad=0.2", "fc": SURFACE, "ec": "none", "alpha": 0.85})
    ax.set_xlim(0, width)
    ax.set_ylim(height, 0)
    ax.set_xticks([])
    ax.tick_params(colors=MUTED, labelsize=8, length=0)
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v / height * 100:.0f}%"))
    for side in ("top", "right", "bottom"):
        ax.spines[side].set_visible(False)
    ax.spines["left"].set_color(AXIS)
    ax.set_title(title, loc="left", color=INK, fontsize=10, fontweight="bold")
    return _png(fig)


def scroll_curve_png(curves: dict[str, list[dict[str, float]]], fb: dict[str, Any], title: str) -> bytes:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(7.2, 3.4))
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    colors = [SLOT1, SLOT2]
    for (name, curve), color in zip(curves.items(), colors):
        ax.plot([p["depth"] for p in curve], [p["reach"] * 100 for p in curve], color=color, linewidth=2, label=name, solid_capstyle="round")
    if fb.get("detected"):
        ax.axvspan(fb["depth_from"], fb["depth_to"], color=GRID, alpha=0.8, zorder=0)
        ax.annotate(f"False bottom: -{fb['drop_pp']:.0f} pp", (fb["depth_to"], fb["reach_after"] * 100), xytext=(8, 14), textcoords="offset points",
                    fontsize=9, color=INK, fontweight="bold")
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 105)
    ax.set_xlabel("Scroll depth (% of page)", color=INK2, fontsize=9)
    ax.set_ylabel("Sessions reaching depth (%)", color=INK2, fontsize=9)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(AXIS)
    ax.tick_params(colors=MUTED, labelsize=8, length=0)
    ax.legend(frameon=False, fontsize=8, labelcolor=INK2, loc="upper right")
    ax.set_title(title, loc="left", color=INK, fontsize=10, fontweight="bold")
    return _png(fig)


# ============================================================================ contract


class HeatmapInput(BaseModel):
    page_url: str | None = Field(default=None, description="Page to analyse (default: the page with the most events)")
    html: str | None = Field(default=None, description="Optional page HTML for component mapping when the page cannot be fetched",
                             json_schema_extra={"ui": "textarea"})
    device: Literal["all", "desktop", "mobile", "tablet"] = Field(default="all", description="Only sessions on this device type (all = every device)")
    start_date: str | None = Field(default=None, description="Optional ISO date filter (inclusive)")
    end_date: str | None = Field(default=None, description="Optional ISO date filter (inclusive)")
    rage_window_s: float = Field(default=2.0, ge=0.5, le=5.0, description="Rage click: clicks on one selector within this many seconds")
    rage_min_clicks: int = Field(default=3, ge=3, le=10, description="Rage click: at least this many clicks on one selector inside the window")
    min_rage_sessions: int = Field(default=3, ge=1, description="Sessions needed before a rage-click hotspot is reported")
    false_bottom_min_drop_pp: float = Field(default=15.0, ge=5.0, le=60.0,
                                            description="False bottom: scroll reach falls by at least this many percentage points in one step of the page")
    ignored_cta_ctr: float = Field(default=0.015, ge=0.0, le=0.2, description="CTA clicked by fewer sessions than this share is 'ignored'")
    low_cta_ctr: float = Field(default=0.03, ge=0.0, le=0.5, description="CTA clicked by fewer sessions than this share (but not 'ignored') is 'low'")
    use_browser: bool = Field(default=True, description="Measure layout and capture a screenshot background with Playwright when available")


class SelectorStat(BaseModel):
    selector: str
    component: str = ""
    element_text: str = ""
    tag: str = ""
    control: Literal["semantic", "scripted", "none", "unknown"]
    clicks: int
    sessions: int
    click_share: float
    ctr: float
    dead_clicks: int
    rage_episodes: int
    rage_sessions: int
    median_x: float
    median_y: float
    depth_pct: float | None = None


class CtaStat(BaseModel):
    selector: str
    label: str
    component: str = ""
    clicks: int
    sessions: int
    ctr: float
    exposed_sessions: int
    ctr_exposed: float | None = None
    depth_pct: float | None = None
    button_like: bool | None = Field(default=None, description="Rendered like a button (computed style); None when no browser measured it")
    status: Literal["ok", "low", "ignored", "hidden"]


class ScrollPoint(BaseModel):
    depth: int
    reach: float
    reach_by_device: dict[str, float] = Field(default_factory=dict)


class FalseBottom(BaseModel):
    detected: bool
    depth_from: int | None = None
    depth_to: int | None = None
    drop_pp: float | None = None
    typical_drop_pp: float | None = None
    reach_before: float | None = None
    reach_after: float | None = None
    component: str = ""


class RageClick(BaseModel):
    selector: str
    component: str = ""
    episodes: int
    sessions: int
    clicks: int
    max_burst: int


class DeadClick(BaseModel):
    selector: str
    component: str = ""
    tag: str = ""
    clicks: int
    sessions: int
    share_of_clicks: float | None = Field(default=None, description="None when the source has no total click count (aggregated exports)")


class Attention(BaseModel):
    median_dwell_s: float | None = None
    mean_dwell_s: float | None = None
    by_band: list[dict[str, Any]] = Field(default_factory=list)


class DeviceStat(BaseModel):
    device: str
    sessions: int
    share: float
    median_max_scroll: float | None = Field(default=None, description="Median max scroll %; Clarity: average scroll depth")
    median_dwell_s: float | None = None
    clicks_per_session: float | None = None
    dead_click_share: float | None = Field(default=None, description="Share of clicks on non-interactive elements (raw events only)")
    rage_sessions: int = 0
    dead_click_session_share: float | None = Field(default=None, description="Share of sessions with dead clicks (aggregated exports)")
    rage_session_share: float | None = None


class AggregateMetric(BaseModel):
    metric: str
    label: str
    occurrences: int
    sessions: int
    session_share: float | None = None
    by_device: dict[str, dict[str, Any]] = Field(default_factory=dict)


class AggregateBehaviour(BaseModel):
    source: str = ""
    sessions: int
    metrics: list[AggregateMetric]
    avg_scroll_depth: dict[str, float | None] = Field(default_factory=dict)
    note: str = ("Aggregated export: click maps, selector-level clicks, CTA CTRs, scroll-reach curves and false-bottom detection need "
                 "a raw event export.")


class InsightItem(BaseModel):
    title: str
    detail: str = ""
    severity: Literal["low", "medium", "high", "critical"] = "medium"
    selector: str | None = None


class Hypothesis(BaseModel):
    hypothesis: str
    evidence: str
    metric: str
    selector: str | None = None
    source: Literal["model", "rules"] = "rules"


class HeatmapOutput(BaseModel):
    page_url: str | None
    sessions: int
    events: int
    period: dict[str, str | None]
    device_filter: str
    dom_source: Literal["fetched", "html", "none"]
    layout_source: Literal["browser", "clicks", "none"]
    clicks: list[SelectorStat]
    ctas: list[CtaStat]
    scroll: list[ScrollPoint]
    false_bottom: FalseBottom
    rage_clicks: list[RageClick]
    dead_clicks: list[DeadClick]
    attention: Attention
    devices: list[DeviceStat]
    summary: str
    insights: list[InsightItem]
    hypotheses: list[Hypothesis]
    insight_source: Literal["model", "rules"]
    data_mode: Literal["events", "aggregated"] = "events"
    aggregate: AggregateBehaviour | None = None
    findings: list[Finding]


class ModelInsight(BaseModel):
    title: str
    detail: str = ""
    severity: Literal["low", "medium", "high", "critical"] = "medium"
    selector: str | None = Field(default=None, description="Selector copied exactly from the metrics, or null")


class ModelHypothesis(BaseModel):
    hypothesis: str = Field(description="If we <change>, then <metric> will <move> because <evidence>")
    evidence: str
    metric: str
    selector: str | None = None


class HeatmapNarrative(BaseModel):
    summary: str
    insights: list[ModelInsight] = Field(default_factory=list, max_length=8)
    hypotheses: list[ModelHypothesis] = Field(default_factory=list, max_length=8)


LAYOUT_JS = """(sels) => {
  const H = document.documentElement.scrollHeight, out = {page_h: H, page_w: document.documentElement.scrollWidth, sections: [], elements: {}};
  document.querySelectorAll('main > section, main > aside, body > header, body > footer, main section[id]').forEach(s => {
    const r = s.getBoundingClientRect(); const h = s.querySelector('h1,h2,h3');
    const label = (s.getAttribute('aria-label') || (h && h.textContent) || s.id || s.className || s.tagName).trim().replace(/\\s+/g, ' ');
    out.sections.push({top: r.top + window.scrollY, bottom: r.bottom + window.scrollY, label: label.slice(0, 70), cls: s.className || s.tagName.toLowerCase()});
  });
  for (const sel of sels) { try { const e = document.querySelector(sel); if (e) { const r = e.getBoundingClientRect(); const cs = getComputedStyle(e);
    const padY = parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom);
    const bg = cs.backgroundColor, hasBg = bg && bg !== 'rgba(0, 0, 0, 0)' && bg !== 'transparent';
    const edge = (parseFloat(cs.borderTopWidth) > 0 && cs.borderTopStyle !== 'none') || (cs.boxShadow && cs.boxShadow !== 'none');
    out.elements[sel] = {top: r.top + window.scrollY, left: r.left, width: r.width, height: r.height,
                         button_like: padY >= 12 && (hasBg || edge)}; } } catch (err) {} }
  return out;
}"""


# ============================================================================ the agent


@register
class HeatmapAnalysisAgent(BaseAgent):
    spec = AgentSpec(
        id="heatmap_analysis",
        name="Heatmap Analysis",
        category=Category.WEB,
        summary="Computes click density, CTA CTRs, scroll-reach with false-bottom detection, rage and dead clicks, attention and device "
                "splits from any behaviour source, maps them to real page components and turns them into test hypotheses.",
        kind=AgentKind.HYBRID,
        tier=Tier.BALANCED,
        connectors=["behaviour", "crawler"],
        skills=["brand_voice"],
        keywords=["heatmap", "click map", "scroll depth", "rage clicks", "dead clicks", "false bottom", "clarity", "hotjar", "cro", "attention"],
        produces=["click_map.png", "scroll_curve.png", "heatmap_metrics.json"],
        feeds=["experiment_ideation", "experiment_planning", "variation_development"],
    )
    Input = HeatmapInput
    Output = HeatmapOutput
    schemas = {"insights": HeatmapNarrative}

    def selftest_input(self) -> dict[str, Any]:
        return {"page_url": "https://www.meridian-mutual.example/products/home-insurance.html"}

    def stages(self):
        return [("load", self.load), ("dom", self.dom), ("metrics", self.metrics), ("insights", self.insights), ("report", self.report)]

    # -- 1. load ---------------------------------------------------------------------------------
    def load(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp = state["inputs"]
        conn = ctx.connector("behaviour")
        warnings, degraded = [], []
        page_url = inp.get("page_url")
        if not page_url:
            pages = conn.pages()
            if pages:
                counts = conn.events().groupby("page_url").size()
                page_url = str(counts.idxmax())
                warnings.append(f"No page_url given: analysed {page_url}, the page with the most events.")
        ev = conn.events(page_url) if page_url else conn.events()
        aggregated = is_aggregated(ev, getattr(conn, "provider", None))
        source = str(getattr(ev, "attrs", {}).get("source") or getattr(conn, "provider", ""))
        if aggregated:
            warnings.append("Aggregated behaviour export (e.g. Microsoft Clarity): no click coordinates or selectors, so click maps, CTA CTRs, "
                            "scroll-reach curves and false-bottom detection are unavailable; reporting the export's dead/rage-click counts instead.")
            degraded.append("aggregated behaviour data: no click coordinates, selectors or per-session scroll")
        if len(ev):
            ev = ev.copy()
            ev["ts"] = pd.to_datetime(ev["ts"], utc=True, format="mixed")
            if aggregated and (inp.get("start_date") or inp.get("end_date")):
                warnings.append("Date filters ignored: the aggregated export covers the provider's own window (Clarity: last 1-3 days).")
            else:
                if inp.get("start_date"):
                    ev = ev[ev["ts"].dt.date >= pd.Timestamp(inp["start_date"]).date()]
                if inp.get("end_date"):
                    ev = ev[ev["ts"].dt.date <= pd.Timestamp(inp["end_date"]).date()]
            if inp.get("device", "all") != "all":
                ev = ev[ev["device"] == inp["device"]]
        if not len(ev):
            warnings.append(f"No behaviour events for {page_url or 'any page'} with the given filters.")
            degraded.append("no behaviour events: metrics are empty")
        records = ev.assign(ts=ev["ts"].astype(str)).to_dict("records") if len(ev) else []
        return {"data": {"page_url": page_url, "events": records, "aggregated": aggregated, "source": source}, "warnings": warnings,
                "provenance": degraded_update(state, degraded, evidence_count=len(records))}

    # -- 2. dom ----------------------------------------------------------------------------------
    def dom(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        url = data["page_url"]
        html, source, warnings, degraded = inp.get("html"), "html" if inp.get("html") else "none", [], []
        crawler = ctx.connector("crawler")
        if not html and url:
            page = crawler.fetch(url)
            if page.ok and page.html:
                html, source = page.html, "fetched"
            else:
                warnings.append(f"Could not fetch {url} ({page.error or page.status}): selectors are not mapped to components.")
                degraded.append("page DOM unavailable: component mapping skipped")
        ev = pd.DataFrame(data["events"])
        selectors = sorted(ev.loc[ev["event_type"] == "click", "selector"].dropna().unique().tolist()) if len(ev) else []
        dom = describe_selectors(html, selectors)
        ctas = find_ctas(html)
        layout, shot = None, None
        if inp.get("use_browser", True) and url and not data.get("aggregated") and len(ev) and crawler.browser_available():
            width = int(ev.loc[ev["device"] == "desktop", "viewport_w"].median()) if (ev["device"] == "desktop").any() else int(ev["viewport_w"].median())
            try:
                sels = list(dict.fromkeys(selectors + [c["selector"] for c in ctas]))

                def measure(page: Any) -> dict[str, Any]:
                    m = page.evaluate(LAYOUT_JS, sels)
                    m["png"] = base64.b64encode(page.screenshot(full_page=True)).decode()
                    return m

                if html is not None and source == "html":
                    layout = crawler.with_page(measure, html=html, base_url=url, width=width, height=800)
                else:
                    layout = crawler.with_page(measure, url=url, width=width, height=800)
                shot = layout.pop("png", None)
            except Exception as exc:
                warnings.append(f"Layout measurement skipped ({type(exc).__name__}); positions are estimated from click coordinates.")
                degraded.append("browser unavailable: component positions estimated from click coordinates")
                layout = None
        return {"data": {"html_source": source, "dom": dom, "ctas": ctas, "layout": layout, "screenshot": shot},
                "warnings": warnings, "provenance": degraded_update(state, degraded)}

    # -- 3. metrics ------------------------------------------------------------------------------
    def metrics(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        ev = pd.DataFrame(data["events"])
        empty = {"clicks": [], "ctas": [], "scroll": [], "false_bottom": {"detected": False}, "rage": [], "dead": [], "attention": {"median_dwell_s": None, "mean_dwell_s": None, "by_band": []},
                 "devices": [], "n_sessions": 0, "n_events": 0, "positions": []}
        if ev.empty:
            return {"data": {"m": empty}}
        ev["ts"] = pd.to_datetime(ev["ts"], utc=True, format="mixed")
        for col in ("x", "y", "scroll_pct", "viewport_w", "viewport_h", "page_h", "is_interactive"):
            ev[col] = pd.to_numeric(ev[col], errors="coerce")
        if data.get("aggregated"):
            return self._aggregated_metrics(ev, inp)
        sess = sessions_frame(ev)
        clicks = ev[ev["event_type"] == "click"].dropna(subset=["selector"])
        page_h = {str(d): float(g["page_h"].median()) for d, g in ev.groupby("device")}
        rage = rage_episodes(clicks, float(inp.get("rage_window_s", 2.0)), int(inp.get("rage_min_clicks", 3)))
        click_rows = click_table(clicks, len(sess), data["dom"], rage, page_h)
        layout = data.get("layout")
        lay_cta = None
        positions: list[dict[str, Any]] = []
        if layout and layout.get("page_h"):
            H = float(layout["page_h"])
            lay_cta = {s: {"depth_pct": (v["top"] + v["height"] / 2) / H * 100, "button_like": v.get("button_like")}
                       for s, v in (layout.get("elements") or {}).items() if v.get("height", 0) > 0}
            positions = [{"top_pct": s["top"] / H * 100, "bottom_pct": s["bottom"] / H * 100, "label": section_label(s.get("cls", ""), s["label"])}
                         for s in layout.get("sections", [])]
        else:  # click-derived positions of mapped components
            positions = [{"top_pct": r["depth_pct"], "label": f"{r['component'] or r['selector']}"} for r in click_rows if r["depth_pct"] is not None]
        ctas = cta_table(data["ctas"], clicks, sess, page_h, lay_cta, float(inp.get("ignored_cta_ctr", 0.015)), float(inp.get("low_cta_ctr", 0.03)))
        fb = detect_false_bottom(sess["max_scroll"], float(inp.get("false_bottom_min_drop_pp", 15.0)))
        if fb.get("detected"):
            fb["component"] = nearest_component(fb["depth_from"], fb["depth_to"], positions)
        overall = reach_curve(sess["max_scroll"], 10)
        by_dev = {str(d): reach_curve(g["max_scroll"], 10) for d, g in sess.groupby("device")}
        scroll = [{"depth": p["depth"], "reach": p["reach"], "reach_by_device": {d: c[i]["reach"] for d, c in by_dev.items()}} for i, p in enumerate(overall)]
        rage_rows = []
        if len(rage):
            for sel, g in rage.groupby("selector"):
                rage_rows.append({"selector": sel, "component": data["dom"].get(sel, {}).get("component", ""), "episodes": int(len(g)),
                                  "sessions": int(g["session_id"].nunique()), "clicks": int(g["clicks"].sum()), "max_burst": int(g["clicks"].max())})
        rage_rows.sort(key=lambda r: -r["sessions"])
        dead_rows = [{"selector": r["selector"], "component": r["component"], "tag": r["tag"], "clicks": r["clicks"], "sessions": r["sessions"],
                      "share_of_clicks": r["click_share"]} for r in click_rows if r["control"] == "none"]
        dead_rows.sort(key=lambda r: -r["sessions"])
        att = {"median_dwell_s": num(sess["dwell_s"].median(), 1), "mean_dwell_s": num(sess["dwell_s"].mean(), 1),
               "by_band": attention_by_band(ev)}
        m = {"clicks": click_rows, "ctas": ctas, "scroll": scroll, "scroll_curves": {f"{d.capitalize()} ({int((sess['device'] == d).sum())} sessions)": c for d, c in by_dev.items()},
             "false_bottom": fb, "rage": rage_rows, "dead": dead_rows, "attention": att, "devices": device_split(sess, clicks, rage),
             "n_sessions": int(len(sess)), "n_events": int(len(ev)), "positions": positions,
             "period": {"start": sess["start"].min().date().isoformat(), "end": sess["end"].max().date().isoformat()}}
        checks = []
        for r in rage_rows:
            if r["sessions"] >= int(inp.get("min_rage_sessions", 3)):
                checks.append(Check(rule="behaviour.rage_clicks", status="fail", severity="high", target=r["selector"],
                                    message=f"Rage clicks on {r['selector']}: {r['episodes']} bursts in {r['sessions']} sessions"))
        for r in dead_rows[:5]:
            if r["sessions"] >= 10:
                checks.append(Check(rule="behaviour.dead_clicks", status="warn", severity="medium", target=r["selector"],
                                    message=f"Dead clicks on {r['selector']}: {r['clicks']} clicks from {r['sessions']} sessions"))
        if fb.get("detected"):
            checks.append(Check(rule="behaviour.false_bottom", status="warn", severity="high",
                                message=f"False bottom at {fb['depth_from']}-{fb['depth_to']}% (-{fb['drop_pp']} pp){' near ' + fb['component'] if fb.get('component') else ''}"))
        for c in ctas:
            if c["status"] == "ignored":
                checks.append(Check(rule="behaviour.ignored_cta", status="warn", severity="medium", target=c["selector"],
                                    message=f"CTA {c['selector']} clicked by {c['ctr'] * 100:.1f}% of sessions"))
        return {"data": {"m": m}, "checks": checks}

    # -- 4. insights (model) ---------------------------------------------------------------------
    def insights(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        m = data["m"]
        if m.get("aggregate"):
            rule_ins, rule_hyp = aggregate_insights(m["aggregate"], int(inp.get("min_rage_sessions", 3)))
        else:
            rule_ins, rule_hyp = rule_hypotheses(m["clicks"], m["ctas"], m["false_bottom"], m["devices"], int(inp.get("min_rage_sessions", 3)))
        if not m["n_sessions"]:
            return {"data": {"insights": [], "hypotheses": [], "insight_source": "rules", "summary": "No behaviour data for this page."}}
        if m.get("aggregate"):
            return self._aggregated_insights(state, ctx, m, rule_ins, rule_hyp)
        fmt = lambda v: f"{v * 100:.1f}%"
        click_lines = [f"{c['selector']} [{c['component'] or 'unmapped'}; {c['tag']}; control={c['control']}]: {c['clicks']} clicks, {c['sessions']} sessions "
                       f"(CTR {fmt(c['ctr'])}), rage bursts {c['rage_episodes']}, dead clicks {c['dead_clicks']}, depth {c['depth_pct']}%" for c in m["clicks"][:15]]
        cta_lines = [f"{c['selector']} '{c['label']}' [{c['component']}]: CTR {fmt(c['ctr'])}" + (f", {fmt(c['ctr_exposed'])} of {c['exposed_sessions']} sessions that scrolled to it" if c["ctr_exposed"] is not None else "")
                     + f", depth {c['depth_pct']}%, status {c['status']}" + ({True: ", rendered as a button", False: ", rendered as a plain text link"}.get(c.get("button_like"), "")) for c in m["ctas"]]
        fb = m["false_bottom"]
        fb_line = (f"False bottom between {fb['depth_from']}% and {fb['depth_to']}% depth: reach {fmt(fb['reach_before'])} -> {fmt(fb['reach_after'])} "
                   f"(-{fb['drop_pp']} pp vs a typical {fb['typical_drop_pp']} pp step), nearest component: {fb.get('component') or 'unknown'}") if fb.get("detected") else "No false bottom detected."
        scroll_line = ", ".join(f"{p['depth']}%: {p['reach'] * 100:.0f}%" for p in m["scroll"])
        device_lines = [f"{d['device']}: {d['sessions']} sessions ({fmt(d['share'])}), median reach "
                        f"{'n/a' if d['median_max_scroll'] is None else format(d['median_max_scroll'], '.0f')}%, median dwell {d['median_dwell_s']}s, "
                        f"{d['clicks_per_session']} clicks/session, dead-click share {fmt(d['dead_click_share'])}, rage sessions {d['rage_sessions']}" for d in m["devices"]]
        narrative = ctx.generate("insights", HeatmapNarrative, page_url=data["page_url"] or "the page", sessions=m["n_sessions"], period=m["period"],
                                 clicks=click_lines, ctas=cta_lines, false_bottom=fb_line, scroll=scroll_line, devices=device_lines,
                                 attention=", ".join(f"{b['band']}: {b['seconds']}s" for b in m["attention"]["by_band"]),
                                 median_dwell=m["attention"]["median_dwell_s"], rule_insights=[i["title"] for i in rule_ins])
        known = {c["selector"] for c in m["clicks"]} | {c["selector"] for c in m["ctas"]}
        if not ctx.llm.offline:
            insights = [{**i.model_dump(), "selector": i.selector if i.selector in known else None} for i in narrative.insights if i.title.strip()]
            hyps = [{**h.model_dump(), "selector": h.selector if h.selector in known else None, "source": "model"} for h in narrative.hypotheses if h.hypothesis.strip()]
            covered = {h["selector"] for h in hyps if h["selector"]}
            hyps += [{**h, "source": "rules"} for h in rule_hyp if h["selector"] and h["selector"] not in covered]
            summary, source = narrative.summary.strip() or self._summary(m), "model"
            insights = insights or rule_ins
        else:
            insights, hyps, summary, source = rule_ins, [{**h, "source": "rules"} for h in rule_hyp], self._summary(m), "rules"
        return {"data": {"insights": insights, "hypotheses": hyps, "insight_source": source, "summary": summary}}

    @staticmethod
    def _aggregated_metrics(ev: pd.DataFrame, inp: dict[str, Any]) -> dict[str, Any]:
        """Aggregated exports (Clarity): report the provider's counts per metric instead of computing CTRs from clicks."""
        agg = aggregate_metrics(ev)
        by = {x["metric"]: x for x in agg["metrics"]}
        n = agg["sessions"]
        rage = [{"selector": "(page level - aggregated export)", "component": "", "episodes": by["rage_click"]["occurrences"], "sessions": by["rage_click"]["sessions"],
                 "clicks": by["rage_click"]["occurrences"], "max_burst": 0}] if "rage_click" in by else []
        dead = [{"selector": "(page level - aggregated export)", "component": "", "tag": "", "clicks": by["dead_click"]["occurrences"],
                 "sessions": by["dead_click"]["sessions"], "share_of_clicks": None}] if "dead_click" in by else []
        devices = []
        for d, ds in sorted(agg["device_sessions"].items(), key=lambda kv: -kv[1]):
            devices.append({"device": d, "sessions": ds, "share": ds / n if n else 0.0, "median_max_scroll": agg["avg_scroll_depth"].get(d),
                            "median_dwell_s": None, "clicks_per_session": None, "dead_click_share": None,
                            "rage_sessions": (by.get("rage_click", {}).get("by_device", {}).get(d) or {}).get("sessions", 0),
                            "dead_click_session_share": (by.get("dead_click", {}).get("by_device", {}).get(d) or {}).get("session_share"),
                            "rage_session_share": (by.get("rage_click", {}).get("by_device", {}).get(d) or {}).get("session_share")})
        checks = []
        min_s = int(inp.get("min_rage_sessions", 3))
        if "rage_click" in by and by["rage_click"]["sessions"] >= min_s:
            checks.append(Check(rule="behaviour.rage_clicks", status="fail", severity="high",
                                message=f"Rage clicks in {by['rage_click']['sessions']} of {n} sessions ({by['rage_click']['occurrences']} occurrences, aggregated export)"))
        if "dead_click" in by and by["dead_click"]["sessions"] >= min_s:
            checks.append(Check(rule="behaviour.dead_clicks", status="warn", severity="medium",
                                message=f"Dead clicks in {by['dead_click']['sessions']} of {n} sessions ({by['dead_click']['occurrences']} occurrences, aggregated export)"))
        checks.append(Check(rule="behaviour.raw_events", status="info", severity="medium",
                            message="Aggregated export: click map, CTA CTRs, scroll curve and false bottom need raw events (csv provider)."))
        m = {"clicks": [], "ctas": [], "scroll": [], "scroll_curves": {}, "false_bottom": {"detected": False}, "rage": rage, "dead": dead,
             "attention": {"median_dwell_s": None, "mean_dwell_s": None, "by_band": []}, "devices": devices, "n_sessions": n,
             "n_events": int(len(ev)), "positions": [], "aggregate": agg, "period": {"start": ev["ts"].min().date().isoformat(), "end": ev["ts"].max().date().isoformat()}}
        return {"data": {"m": m}, "checks": checks}

    def _aggregated_insights(self, state: dict[str, Any], ctx: RunContext, m: dict[str, Any], rule_ins: list[dict[str, Any]],
                             rule_hyp: list[dict[str, Any]]) -> dict[str, Any]:
        agg = m["aggregate"]
        lines = [f"{x['label']} (page level): {x['occurrences']} occurrences in {x['sessions']} of {agg['sessions']} sessions "
                 f"({(x['session_share'] or 0) * 100:.1f}%)" for x in agg["metrics"]]
        scroll = ", ".join(f"{d}: average scroll depth {v}%" for d, v in agg["avg_scroll_depth"].items() if v is not None) or "not exported"
        device_lines = [f"{d['device']}: {d['sessions']} sessions ({d['share'] * 100:.1f}%)" for d in m["devices"]]
        narrative = ctx.generate("insights", HeatmapNarrative, page_url=state["data"]["page_url"] or "the page", sessions=agg["sessions"], period=m["period"],
                                 clicks=lines, ctas=[], false_bottom="Scroll-reach curve and false-bottom detection unavailable (aggregated export).",
                                 scroll=scroll, devices=device_lines, attention="not exported", median_dwell="n/a", rule_insights=[i["title"] for i in rule_ins])
        summary = (f"{agg['sessions']} sessions (aggregated export): " + "; ".join(
            f"{x['label'].lower()} in {(x['session_share'] or 0) * 100:.1f}% of sessions" for x in agg["metrics"][:3])
                   + (f"; average scroll depth {scroll}" if agg["avg_scroll_depth"] else "") + ".")
        if not ctx.llm.offline:  # no selectors exist in an aggregated export: the model may not invent any
            insights = [{**i.model_dump(), "selector": None} for i in narrative.insights if i.title.strip()] or rule_ins
            hyps = [{**h.model_dump(), "selector": None, "source": "model"} for h in narrative.hypotheses if h.hypothesis.strip()] or [{**h, "source": "rules"} for h in rule_hyp]
            return {"data": {"insights": insights, "hypotheses": hyps, "insight_source": "model", "summary": narrative.summary.strip() or summary}}
        return {"data": {"insights": rule_ins, "hypotheses": [{**h, "source": "rules"} for h in rule_hyp], "insight_source": "rules", "summary": summary}}

    @staticmethod
    def _summary(m: dict[str, Any]) -> str:
        parts = [f"{m['n_sessions']} sessions analysed"]
        if m["rage"]:
            r = m["rage"][0]
            parts.append(f"rage clicks on {r['selector']} ({r['sessions']} sessions)")
        if m["dead"]:
            d = m["dead"][0]
            parts.append(f"dead clicks on {d['selector']} ({d['clicks']} clicks)")
        fb = m["false_bottom"]
        if fb.get("detected"):
            parts.append(f"a false bottom at {fb['depth_from']}-{fb['depth_to']}% (-{fb['drop_pp']:.0f} pp)")
        ign = [c for c in m["ctas"] if c["status"] == "ignored"]
        if ign:
            parts.append(f"ignored CTA {ign[0]['selector']} ({ign[0]['ctr'] * 100:.1f}% CTR)")
        return "; ".join(parts) + "."

    # -- 5. report -------------------------------------------------------------------------------
    def report(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        m = data["m"]
        artifacts = []
        ev = pd.DataFrame(data["events"])
        layout_source = "browser" if data.get("layout") else ("clicks" if m.get("positions") else "none")
        if m["n_sessions"]:
            try:
                ev["ts"] = pd.to_datetime(ev["ts"], utc=True, format="mixed")
                for col in ("x", "y", "page_h", "viewport_w"):
                    ev[col] = pd.to_numeric(ev[col], errors="coerce")
                main_dev = ev.groupby("device")["session_id"].nunique().idxmax()
                cl = ev[(ev["event_type"] == "click") & (ev["device"] == main_dev)].dropna(subset=["x", "y"])
                width = float(ev.loc[ev["device"] == main_dev, "viewport_w"].median())
                height = float(ev.loc[ev["device"] == main_dev, "page_h"].median())
                marks = []
                rage_sel = {r["selector"] for r in m["rage"] if r["sessions"] >= int(inp.get("min_rage_sessions", 3))}
                for c in m["clicks"]:
                    g = cl[cl["selector"] == c["selector"]]
                    if g.empty:
                        continue
                    if c["selector"] in rage_sel:
                        marks.append({"x": float(g["x"].median()), "y": float(g["y"].median()), "color": CRITICAL, "label": f"Rage: {c['element_text'] or c['selector']}"})
                    elif c["control"] == "none" and c["sessions"] >= 10:
                        marks.append({"x": float(g["x"].median()), "y": float(g["y"].median()), "color": SERIOUS, "label": f"Dead: {c['selector']}"})
                bg = base64.b64decode(data["screenshot"]) if data.get("screenshot") and main_dev == "desktop" else None
                png = click_map_png(cl, width, height, marks, m["positions"] if layout_source == "browser" else [], bg,
                                    f"Click density, {main_dev} ({len(cl)} clicks)")
                artifacts.append(ctx.artifacts.write_bytes("click_map.png", png, "image/png", f"Click map in page coordinates ({main_dev})"))
                artifacts.append(ctx.artifacts.write_bytes("scroll_curve.png", scroll_curve_png(m["scroll_curves"], m["false_bottom"], "Scroll reach by depth"),
                                                           "image/png", "Share of sessions reaching each scroll depth"))
            except Exception as exc:  # charts never fail the analysis
                ctx.log("heatmap charts failed", error=repr(exc))
        sev_map = {"low": "low", "medium": "medium", "high": "high", "critical": "critical"}
        def paired(idx: int, ins: dict[str, Any]) -> dict[str, Any]:
            hyps = data["hypotheses"]
            if ins.get("selector"):
                return next((h for h in hyps if h.get("selector") == ins["selector"]), {})
            if m.get("aggregate") and idx < len(hyps) and not hyps[idx].get("selector"):  # page-level pairs keep their order
                return hyps[idx]
            return {}

        findings = [Finding(title=i["title"], detail=i["detail"], severity=sev_map.get(i["severity"], "medium"), category="behaviour",
                            evidence=paired(k, i).get("evidence", ""), recommendation=paired(k, i).get("hypothesis", ""),
                            target=i.get("selector") or data["page_url"], source_agent=self.spec.id) for k, i in enumerate(data["insights"])]
        out = HeatmapOutput(page_url=data["page_url"], sessions=m["n_sessions"], events=m["n_events"], period=m.get("period", {"start": None, "end": None}),
                            device_filter=inp.get("device", "all"), dom_source=data["html_source"], layout_source=layout_source,
                            clicks=[SelectorStat(**{k: c[k] for k in SelectorStat.model_fields}) for c in m["clicks"]],
                            ctas=[CtaStat(**{k: c[k] for k in CtaStat.model_fields}) for c in m["ctas"]],
                            scroll=[ScrollPoint(**p) for p in m["scroll"]], false_bottom=FalseBottom(**{k: v for k, v in m["false_bottom"].items() if k in FalseBottom.model_fields}),
                            rage_clicks=[RageClick(**r) for r in m["rage"]], dead_clicks=[DeadClick(**d) for d in m["dead"]],
                            attention=Attention(**m["attention"]), devices=[DeviceStat(**d) for d in m["devices"]], summary=data["summary"],
                            insights=[InsightItem(**i) for i in data["insights"]], hypotheses=[Hypothesis(**h) for h in data["hypotheses"]],
                            insight_source=data["insight_source"], data_mode="aggregated" if m.get("aggregate") else "events",
                            aggregate=AggregateBehaviour(source=data.get("source", ""), **m["aggregate"]) if m.get("aggregate") else None,
                            findings=findings)
        artifacts.append(ctx.artifacts.write_text("heatmap_metrics.json", json.dumps(out.model_dump(mode="json"), indent=1), "application/json",
                                                  "All computed behavioural metrics"))
        return {"output": out.model_dump(mode="json"), "artifacts": artifacts,
                "summary": f"Heatmap: {data['summary']} {len(out.hypotheses)} test hypothesis(es) for experiment ideation."}
