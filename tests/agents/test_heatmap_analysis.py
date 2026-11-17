"""Heatmap: rage/dead clicks, false bottom, CTA CTRs and attention are pandas maths tied to real components."""

import functools

import pandas as pd
import pytest
from bs4 import BeautifulSoup

from caf.agents.web.heatmap_analysis import (
    control_type,
    detect_false_bottom,
    find_ctas,
    match_selector,
    rage_episodes,
    reach_curve,
    scripted_selectors,
)
from caf.core import get_agent

PAGE = "https://www.meridian-mutual.example/products/home-insurance.html"
PREMIUM = '.plan-tab[data-plan="premium"]'


@functools.lru_cache(maxsize=1)
def _browser() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            p.chromium.launch(args=["--no-sandbox"]).close()
        return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def static_run():
    from caf.core import RunContext

    r = get_agent("heatmap_analysis").run({"page_url": PAGE, "use_browser": False}, RunContext.create())
    assert r.status != "error", r.error
    return r


def test_demo_behaviour_patterns(static_run):
    out = static_run.output
    assert out["sessions"] == 500
    assert out["dead_clicks"][0]["selector"] == "img.hero-image" and out["dead_clicks"][0]["sessions"] >= 40
    assert out["rage_clicks"][0]["selector"] == PREMIUM and out["rage_clicks"][0]["sessions"] >= 20
    fb = out["false_bottom"]
    assert fb["detected"] and fb["depth_from"] == 40 and fb["depth_to"] == 45 and fb["drop_pp"] > 25
    ctas = {c["selector"]: c for c in out["ctas"]}
    assert ctas["#advisor-cta"]["status"] == "ignored" and ctas["#hero-cta"]["status"] == "ok"
    assert ctas["#panel-premium a.btn.btn-outline"]["status"] == "hidden"


def test_components_and_control_types(static_run):
    clicks = {c["selector"]: c for c in static_run.output["clicks"]}
    assert clicks["img.hero-image"]["control"] == "none" and "Hero" in clicks["img.hero-image"]["component"]
    assert clicks[PREMIUM]["control"] == "scripted" and "Compare plans" in clicks[PREMIUM]["component"]
    assert clicks["#hero-cta"]["control"] == "semantic"


def test_hypotheses_feed_experiment_ideation(static_run):
    hyps = static_run.output["hypotheses"]
    sels = {h["selector"] for h in hyps}
    assert {PREMIUM, "img.hero-image", "#advisor-cta"} <= sels
    assert any(h["selector"] is None and "false bottom" in h["evidence"].lower() or "scroll reach" in h["evidence"] for h in hyps)
    assert all(h["hypothesis"] and h["evidence"] and h["metric"] for h in hyps)
    assert {a.name for a in static_run.artifacts} >= {"click_map.png", "scroll_curve.png"}
    assert static_run.output["findings"] and all(f["category"] == "behaviour" for f in static_run.output["findings"])


def test_scroll_curve_and_attention(static_run):
    out = static_run.output
    reach = [p["reach"] for p in out["scroll"]]
    assert reach[0] == 1.0 and all(a >= b for a, b in zip(reach, reach[1:]))
    assert set(out["scroll"][5]["reach_by_device"]) == {"desktop", "mobile"}
    assert len(out["attention"]["by_band"]) == 10 and out["attention"]["median_dwell_s"] > 0
    assert {d["device"] for d in out["devices"]} == {"desktop", "mobile"}


def test_rage_episodes_window():
    ts = pd.to_datetime(["2026-01-01T00:00:00Z", "2026-01-01T00:00:00.5Z", "2026-01-01T00:00:01.2Z",  # burst of 3 in 1.2 s
                         "2026-01-01T00:00:10Z", "2026-01-01T00:00:13Z", "2026-01-01T00:00:16Z"], format="ISO8601")  # slow clicks
    clicks = pd.DataFrame({"session_id": ["s"] * 6, "selector": ["#a"] * 3 + ["#b"] * 3, "ts": ts})
    eps = rage_episodes(clicks, window_s=2.0, min_clicks=3)
    assert eps["selector"].tolist() == ["#a"] and eps["clicks"].tolist() == [3]


def test_false_bottom_detector():
    smooth = pd.Series([100 - i for i in range(0, 100, 2)] * 2, dtype=float)
    assert detect_false_bottom(smooth)["detected"] is False
    cliff = pd.Series([38.0] * 70 + [90.0] * 30)
    fb = detect_false_bottom(cliff)
    assert fb["detected"] and fb["depth_from"] == 35 and fb["depth_to"] == 40
    assert reach_curve(pd.Series([10.0, 50.0]), 50) == [{"depth": 0, "reach": 1.0}, {"depth": 50, "reach": 0.5}, {"depth": 100, "reach": 0.0}]


def test_dom_helpers():
    html = ('<section class="plans"><h2>Plans</h2><div class="plan-tab" data-plan="a">A</div><a class="btn btn-light" href="/q">Go</a></section>'
            '<script>document.querySelectorAll(".plan-tab").forEach(function(t){t.addEventListener("click", function(){});});</script>')
    soup = BeautifulSoup(html, "lxml")
    assert ".plan-tab" in scripted_selectors(soup)
    assert control_type(soup.select_one(".plan-tab"), soup, scripted_selectors(soup)) == "scripted"
    ctas = find_ctas(html)
    assert ctas and ctas[0]["label"] == "Go"
    assert match_selector(".promo-band .btn-light", ".promo-band a.btn.btn-light")
    assert not match_selector("#hero-cta", "#advisor-cta")


@pytest.mark.browser
def test_browser_layout_attribution(ctx):
    if not _browser():
        pytest.skip("Playwright Chromium not available")
    r = get_agent("heatmap_analysis").run({"page_url": PAGE}, ctx)
    out = r.output
    assert out["layout_source"] == "browser"
    assert "Promo band" in out["false_bottom"]["component"] and "Compare plans" in out["false_bottom"]["component"]
    assert {c["selector"]: c for c in out["ctas"]}["#advisor-cta"]["button_like"] is True


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


def test_online_hypotheses_are_reconciled():
    from caf.core import RunContext

    llm = ScriptedLLM({"HeatmapNarrative": {"summary": "Premium tab is broken.", "insights": [{"title": "Broken tab", "selector": PREMIUM, "severity": "high"},
                                                                                           {"title": "Ghost", "selector": "#does-not-exist"}],
                                            "hypotheses": [{"hypothesis": "If the Premium tab works, quote starts rise.", "evidence": "25 rage sessions",
                                                            "metric": "premium quote CTR", "selector": PREMIUM}]}})
    r = get_agent("heatmap_analysis").run({"page_url": PAGE, "use_browser": False}, RunContext.create(llm=llm))
    out = r.output
    assert out["insight_source"] == "model" and out["summary"] == "Premium tab is broken."
    assert {i["selector"] for i in out["insights"]} == {PREMIUM, None}  # unknown selector nulled, not trusted
    hyp = {h["selector"]: h for h in out["hypotheses"]}
    assert hyp[PREMIUM]["source"] == "model"
    assert hyp["img.hero-image"]["source"] == "rules" and hyp["#advisor-cta"]["source"] == "rules"  # rule coverage floor


def _clarity_frame():
    """Event frame in the shape of the Clarity provider's events(): aggregated, no coordinates or selectors."""
    import numpy as np

    from caf.connectors.data import EVENT_COLUMNS

    rows = []
    base = {"ts": "2026-10-17T06:00:00Z", "page_url": PAGE, "x": np.nan, "y": np.nan, "selector": None, "element_text": None, "tag": None,
            "is_interactive": 0, "scroll_pct": np.nan, "page_h": np.nan}
    for dev, n, (vw, vh) in (("desktop", 200, (1280, 800)), ("mobile", 100, (390, 844))):
        b = {**base, "device": dev, "viewport_w": vw, "viewport_h": vh}
        rows += [{**b, "session_id": f"clarity:{dev}:{i}", "event_type": "pageview"} for i in range(n)]
        rows.append({**b, "session_id": f"clarity:{dev}:aggregate", "event_type": "scroll", "scroll_pct": 48.5 if dev == "desktop" else 61.0})
    d = {**base, "device": "desktop", "viewport_w": 1280, "viewport_h": 800}
    rows += [{**d, "session_id": f"clarity:desktop:{i % 12}", "event_type": "rage_click"} for i in range(30)]
    rows += [{**d, "session_id": f"clarity:desktop:{i % 20}", "event_type": "dead_click"} for i in range(50)]
    df = pd.DataFrame(rows, columns=EVENT_COLUMNS)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df.attrs.update({"aggregated": True, "source": "clarity:project-live-insights"})
    return df


class StubClarity:
    provider = "clarity"
    available = True

    def events(self, page_url=None):
        return _clarity_frame()

    def pages(self):
        return [PAGE]


def test_aggregated_clarity_export_reports_counts_not_ctrs():
    import json

    from caf.core import RunContext

    ctx = RunContext.create()
    ctx.connectors._cache[("behaviour", ctx.connectors.provider_for("behaviour"))] = StubClarity()
    r = get_agent("heatmap_analysis").run({"page_url": PAGE, "use_browser": False}, ctx)
    assert r.status != "error", r.error
    out = r.output
    json.dumps(out, allow_nan=False)  # NaN-free
    assert out["data_mode"] == "aggregated" and out["sessions"] == 300
    assert out["ctas"] == [] and out["clicks"] == [] and out["false_bottom"]["detected"] is False
    assert out["rage_clicks"][0]["clicks"] == 30 and out["rage_clicks"][0]["sessions"] == 12
    assert out["dead_clicks"][0]["sessions"] == 20 and out["dead_clicks"][0]["share_of_clicks"] is None
    devices = {d["device"]: d for d in out["devices"]}
    assert devices["desktop"]["median_max_scroll"] == 48.5 and devices["mobile"]["median_max_scroll"] == 61.0
    assert devices["desktop"]["rage_session_share"] == pytest.approx(12 / 200) and devices["mobile"]["rage_sessions"] == 0
    metrics = {m["metric"]: m for m in out["aggregate"]["metrics"]}
    assert metrics["rage_click"]["session_share"] == pytest.approx(12 / 300)
    assert any(f["title"].startswith("Rage clicks in") for f in out["findings"])
    assert all(h["selector"] is None for h in out["hypotheses"]) and out["hypotheses"]
    assert "aggregated behaviour data: no click coordinates, selectors or per-session scroll" in r.provenance["degraded_reasons"]
    assert not any(a.name == "click_map.png" for a in r.artifacts)


def test_device_split_nan_guard():
    from caf.agents.web.heatmap_analysis import device_split

    sess = pd.DataFrame({"device": ["tablet", "tablet"], "max_scroll": [float("nan")] * 2, "dwell_s": [float("nan")] * 2}, index=["a", "b"])
    out = device_split(sess, pd.DataFrame(columns=["device", "is_interactive"]), pd.DataFrame(columns=["session_id"]))
    assert out[0]["median_max_scroll"] is None and out[0]["median_dwell_s"] is None
