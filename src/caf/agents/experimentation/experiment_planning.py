"""Experiment Planning - turns a raw idea into a runnable, statistically sound, platform-neutral plan.

The statistical core is real mathematics owned in code (statsmodels / scipy / numpy), never model
prose: sample size per variant (two-sided, Cohen's h, Bonferroni for more than two variants), the
achievable MDE for the available traffic, duration rounded up to whole weeks, a Bayesian design
(prior, expected-loss stopping rule, simulated operating characteristics) and a group-sequential
design with O'Brien-Fleming boundaries computed by exact recursive numerical integration.
Page inspection uses the framework's own crawler (+ optional Playwright screenshot). The model
writes the hypothesis (If/Then/Because), variant descriptions, targeting, metric definitions,
risks and assumptions; code reconciles them (one control, one primary metric, guardrails, equal
allocations, numbers). The plan is exported to Optimizely, VWO, AB Tasty, LaunchDarkly, Adobe Target
or generic YAML through pluggable exporters and upserted as an ExperimentDefinition.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Callable, Literal
from urllib.parse import urlparse

import numpy as np
import yaml
from pydantic import BaseModel, Field
from scipy.optimize import brentq
from scipy.stats import norm

from ...connectors.experiments import Audience, ExperimentDefinition, Metric, Variant
from ...core import AgentKind, AgentSpec, BaseAgent, Category, Check, RunContext, Tier, register
from ...core.textutil import slugify, tfidf_similarity, truncate
from .experiment_ideation import compose_hypothesis, extract_page_facts, page_analytics, page_fact_lines

DEMO_URL = "https://www.meridian-mutual.example/products/home-insurance.html"

# ============================================================================ frequentist sizing (pure)


def bonferroni_alpha(alpha: float, n_variants: int) -> float:
    """Per-comparison alpha for (n_variants - 1) treatment-vs-control comparisons."""
    return alpha / max(1, int(n_variants) - 1)


def cohens_h(p_treatment: float, p_control: float) -> float:
    from statsmodels.stats.proportion import proportion_effectsize

    return abs(float(proportion_effectsize(p_treatment, p_control)))


def sample_size_per_variant(baseline: float, mde: float, alpha: float = 0.05, power: float = 0.8, n_variants: int = 2) -> int:
    """Visitors per variant to detect a relative lift `mde` on `baseline` (two-sided z-test on Cohen's h)."""
    from statsmodels.stats.power import NormalIndPower

    if not (0 < baseline < 1):
        raise ValueError("baseline must be a rate between 0 and 1")
    target = baseline * (1 + mde)
    if not (0 < target < 1) or mde == 0:
        raise ValueError("baseline x (1 + mde) must stay between 0 and 1 and mde must not be 0")
    h = cohens_h(target, baseline)
    n = NormalIndPower().solve_power(effect_size=h, nobs1=None, alpha=bonferroni_alpha(alpha, n_variants), power=power,
                                     ratio=1.0, alternative="two-sided")
    return int(math.ceil(float(n)))


def achievable_mde(baseline: float, n_per_variant: float, alpha: float = 0.05, power: float = 0.8, n_variants: int = 2) -> float | None:
    """Smallest relative lift detectable with n visitors per variant (inverse of sample_size_per_variant)."""
    from statsmodels.stats.power import NormalIndPower

    if n_per_variant is None or n_per_variant < 2 or not (0 < baseline < 1):
        return None
    h = float(NormalIndPower().solve_power(effect_size=None, nobs1=float(n_per_variant), alpha=bonferroni_alpha(alpha, n_variants),
                                           power=power, ratio=1.0, alternative="two-sided"))
    phi = math.asin(math.sqrt(baseline)) + h / 2
    if phi >= math.pi / 2:
        return None
    return round(math.sin(phi) ** 2 / baseline - 1, 5)


def duration_for_sample(n_per_variant: int, daily_visitors: float, traffic_allocation_pct: float = 100.0, n_variants: int = 2,
                        min_days: int = 7) -> dict[str, Any]:
    """Days needed for n per variant with equal splits, rounded up to whole weeks (minimum one week)."""
    per_variant_daily = float(daily_visitors) * float(traffic_allocation_pct) / 100.0 / int(n_variants)
    if per_variant_daily <= 0:
        return {"per_variant_daily": 0.0, "days_exact": math.inf, "days": None, "weeks": None}
    exact = n_per_variant / per_variant_daily
    weeks = max(math.ceil(max(min_days, 1) / 7), math.ceil(math.ceil(exact - 1e-9) / 7))
    return {"per_variant_daily": round(per_variant_daily, 2), "days_exact": round(exact, 2), "days": weeks * 7, "weeks": weeks}


def sensitivity_table(baseline: float, daily_visitors: float, *, alpha: float, power: float, n_variants: int, traffic_allocation_pct: float,
                      max_duration_days: int, mdes: tuple[float, ...] = (0.02, 0.03, 0.05, 0.075, 0.10, 0.15, 0.20, 0.30)) -> list[dict[str, Any]]:
    rows = []
    for m in mdes:
        try:
            n = sample_size_per_variant(baseline, m, alpha, power, n_variants)
        except ValueError:
            continue
        d = duration_for_sample(n, daily_visitors, traffic_allocation_pct, n_variants)
        rows.append({"mde": m, "sample_size_per_variant": n, "duration_days": d["days"], "duration_weeks": d["weeks"],
                     "feasible": d["days"] is not None and d["days"] <= max_duration_days})
    return rows


# ============================================================================ group-sequential design (pure)


def _simpson_grid(a: float, m: int) -> tuple[np.ndarray, np.ndarray]:
    x = np.linspace(-a, a, m)
    h = x[1] - x[0]
    w = np.ones(m)
    w[1:-1:2], w[2:-1:2] = 4, 2
    return x, w * h / 3


def gs_crossing_probs(boundaries: np.ndarray | list[float], info: np.ndarray | list[float], drift: float = 0.0, grid: int = 201) -> np.ndarray:
    """Probability of first crossing |Z_k| >= b_k at each look (Armitage-McPherson-Rowe recursion, exact up to quadrature).

    `info` are information fractions (t_K = 1); `drift` is E[Z] at full information (0 under H0).
    """
    b = np.asarray(boundaries, float)
    t = np.asarray(info, float)
    out = []
    sd, mu = math.sqrt(t[0]), drift * t[0]
    a = b[0] * sd
    out.append(float(norm.cdf(-a, mu, sd) + norm.sf(a, mu, sd)))
    if len(t) == 1:
        return np.array(out)
    x, w = _simpson_grid(a, grid)
    f = norm.pdf(x, mu, sd)
    for j in range(1, len(t)):
        d_i = t[j] - t[j - 1]
        s, mu = math.sqrt(d_i), drift * d_i
        a = b[j] * math.sqrt(t[j])
        out.append(float(np.sum(w * f * (norm.cdf((-a - x - mu) / s) + norm.sf((a - x - mu) / s)))))
        if j < len(t) - 1:
            nx, nw = _simpson_grid(a, grid)
            f = (norm.pdf((nx[:, None] - x[None, :] - mu) / s) / s) @ (w * f)
            x, w = nx, nw
    return np.array(out)


def obrien_fleming_boundaries(k: int | None = None, alpha: float = 0.05, info: list[float] | None = None) -> dict[str, Any]:
    """Two-sided O'Brien-Fleming boundaries b_k = C / sqrt(t_k) with overall type I error `alpha`."""
    t = np.asarray(info if info is not None else [(i + 1) / k for i in range(int(k or 1))], float)
    t = t / t[-1]
    c = brentq(lambda cc: gs_crossing_probs(cc / np.sqrt(t), t).sum() - alpha, 0.5, 12.0, xtol=1e-8)
    b = c / np.sqrt(t)
    spent = np.cumsum(gs_crossing_probs(b, t))
    return {"constant": round(float(c), 5), "info": [round(float(v), 5) for v in t], "z": [round(float(v), 4) for v in b],
            "nominal_alpha": [round(float(2 * norm.sf(v)), 6) for v in b], "cumulative_alpha": [round(float(v), 6) for v in spent]}


def drift_for_power(boundaries: list[float], info: list[float], power: float) -> float:
    return float(brentq(lambda d: gs_crossing_probs(boundaries, info, d).sum() - power, 0.05, 15.0, xtol=1e-9))


def max_sample_inflation(boundaries: list[float], info: list[float], alpha: float = 0.05, power: float = 0.8) -> float:
    """Maximum sample size of the group-sequential design relative to the fixed design (Jennison & Turnbull's R)."""
    fixed = drift_for_power([norm.ppf(1 - alpha / 2)], [1.0], power)
    return float((drift_for_power(boundaries, info, power) / fixed) ** 2)


def sequential_design(n_fixed: int, per_variant_daily: float, *, looks: int, alpha: float, power: float, n_variants: int = 2,
                      max_duration_days: int = 365) -> dict[str, Any]:
    """O'Brien-Fleming design with looks at whole weeks; max sample size inflated so power is preserved."""
    a = bonferroni_alpha(alpha, n_variants)
    k = max(1, int(looks))
    eq = obrien_fleming_boundaries(k, a)
    r = max_sample_inflation(eq["z"], eq["info"], a, power)
    weeks = max(1, math.ceil(n_fixed * r / max(per_variant_daily, 1e-9) / 7)) if per_variant_daily > 0 else None
    if weeks is None:
        return {"available": False, "reason": "no traffic"}
    for _ in range(3):  # align looks to whole weeks, then re-check the inflation for those fractions
        k_eff = min(k, weeks)
        look_weeks = sorted({max(1, int(round(weeks * (i + 1) / k_eff))) for i in range(k_eff)})
        info = [w / weeks for w in look_weeks]
        des = obrien_fleming_boundaries(alpha=a, info=info)
        r = max_sample_inflation(des["z"], des["info"], a, power)
        n_max = int(math.ceil(n_fixed * r))
        need_weeks = max(1, math.ceil(n_max / per_variant_daily / 7))
        if need_weeks <= weeks:
            break
        weeks = need_weeks
    drift = drift_for_power(des["z"], des["info"], power)
    stop = gs_crossing_probs(des["z"], des["info"], drift)
    expected_frac = float(np.sum(stop * np.array(des["info"])) + (1 - stop.sum()) * 1.0)
    schedule = []
    for i, (w, t, z, na, ca) in enumerate(zip(look_weeks, des["info"], des["z"], des["nominal_alpha"], des["cumulative_alpha"]), 1):
        schedule.append({"look": i, "week": w, "day": w * 7, "information_fraction": round(t, 4),
                         "cumulative_per_variant": int(math.ceil(n_max * t)), "z_boundary": z, "nominal_alpha": na,
                         "cumulative_alpha_spent": ca})
    return {"available": True, "boundary": "O'Brien-Fleming (two-sided)", "looks": len(schedule), "alpha": round(a, 6),
            "constant": des["constant"], "max_sample_inflation": round(r, 4), "max_sample_size_per_variant": n_max,
            "max_duration_weeks": weeks, "max_duration_days": weeks * 7, "feasible": weeks * 7 <= max_duration_days,
            "expected_sample_fraction_h1": round(expected_frac, 4), "expected_sample_size_per_variant_h1": int(math.ceil(n_max * expected_frac)),
            "schedule": schedule}


# ============================================================================ Bayesian design (pure)


def beta_prior(baseline: float, strength: float) -> tuple[float, float]:
    """Weakly informative Beta prior centred on the baseline, worth `strength` pseudo-visitors."""
    return max(baseline * strength, 0.5), max((1 - baseline) * strength, 0.5)


def expected_loss_normal(mu_diff: np.ndarray, sd_diff: np.ndarray) -> np.ndarray:
    """E[max(control - treatment, 0)] when (treatment - control) ~ N(mu, sd^2)."""
    z = mu_diff / sd_diff
    return sd_diff * norm.pdf(z) - mu_diff * norm.cdf(-z)


def bayesian_design(baseline: float, mde: float, per_variant_daily: float, weeks: int, *, prior_strength: float = 100.0,
                    prob_beat_min: float = 0.95, expected_loss_max_rel: float = 0.001, sims: int = 4000, seed: int = 7) -> dict[str, Any]:
    """Operating characteristics of a weekly-look Bayesian stopping rule (ship when P(beat) and expected loss pass).

    Weekly conversions are simulated (seeded) under H1 (true lift = mde) and H0 (no lift); Beta posteriors are
    evaluated with their normal approximation, which is accurate at the sample sizes an A/B test needs.
    """
    a0, b0 = beta_prior(baseline, prior_strength)
    rng = np.random.default_rng(seed)
    weekly_n = max(int(round(per_variant_daily * 7)), 1)
    weeks = max(1, int(weeks))

    def run(p_t: float) -> np.ndarray:
        cc = rng.binomial(weekly_n, baseline, size=(sims, weeks)).cumsum(axis=1)
        ct = rng.binomial(weekly_n, p_t, size=(sims, weeks)).cumsum(axis=1)
        n = weekly_n * np.arange(1, weeks + 1)

        def post(c: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
            a, b = a0 + c, b0 + (n - c)
            return a / (a + b), a * b / ((a + b) ** 2 * (a + b + 1))

        mc, vc = post(cc)
        mt, vt = post(ct)
        mu, sd = mt - mc, np.sqrt(vt + vc)
        p_beat = norm.cdf(mu / sd)
        loss_rel = expected_loss_normal(mu, sd) / np.maximum(mc, 1e-12)
        ship = (p_beat >= prob_beat_min) & (loss_rel <= expected_loss_max_rel)
        first = np.where(ship.any(axis=1), ship.argmax(axis=1), weeks)  # index of first shipping week, `weeks` = never
        return first

    h1 = run(min(baseline * (1 + mde), 0.999))
    h0 = run(baseline)
    weekly = []
    for w in range(weeks):
        weekly.append({"week": w + 1, "cumulative_per_variant": weekly_n * (w + 1), "p_ship_if_lift_is_mde": round(float((h1 <= w).mean()), 4),
                       "p_ship_if_no_lift": round(float((h0 <= w).mean()), 4)})
    decided = h1[h1 < weeks]
    return {"available": True, "prior": {"distribution": "Beta", "a": round(a0, 3), "b": round(b0, 3), "strength_visitors": prior_strength,
                                         "mean": round(a0 / (a0 + b0), 5)},
            "decision_rule": {"prob_beat_min": prob_beat_min, "expected_loss_max_rel": expected_loss_max_rel,
                              "expected_loss_max_abs": round(expected_loss_max_rel * baseline, 7),
                              "text": f"Ship when P(variant beats control) >= {prob_beat_min:.0%} and the expected loss of shipping is "
                                      f"<= {expected_loss_max_rel:.2%} of the control rate ({expected_loss_max_rel * baseline:.5f} absolute)."},
            "weekly": weekly, "max_weeks": weeks,
            "power_at_max": weekly[-1]["p_ship_if_lift_is_mde"], "false_ship_rate": weekly[-1]["p_ship_if_no_lift"],
            "median_weeks_to_ship_h1": int(np.median(decided) + 1) if len(decided) else None,
            "method": f"{sims} seeded simulations per scenario (seed {seed}); normal approximation to Beta posteriors"}


# ============================================================================ exporters (pure)


def _bp(pct: float) -> int:
    return int(round(pct * 100))


def _weights_bp(variants: list[dict[str, Any]]) -> list[int]:
    w = [_bp(v["allocation"]) for v in variants]
    if w:
        w[0] += 10000 - sum(w)  # basis points must sum to exactly 10000
    return w


def export_optimizely(plan: dict[str, Any]) -> dict[str, Any]:
    """Optimizely Web Experimentation REST v2 experiment payload (event ids resolved by the adapter)."""
    weights = _weights_bp(plan["variants"])
    return {
        "name": plan["title"], "description": plan["hypothesis"], "type": "a/b", "status": "not_started",
        "holdback": int(round((100 - plan["targeting"]["traffic_allocation"]) * 100)),
        "url_targeting": {"edit_url": plan["page_url"], "conditions": json.dumps(["and", ["or", {"match_type": "simple", "type": "url", "value": plan["page_url"]}]])},
        "audience_conditions": "everyone" if not plan["targeting"]["devices"] else json.dumps(["or"] + [{"type": "device", "value": d} for d in plan["targeting"]["devices"]]),
        "variations": [{"name": v["name"], "weight": w, "actions": [], "description": v["description"]} for v, w in zip(plan["variants"], weights)],
        "metrics": [{"event_key": m["event"], "aggregator": "sum" if m["type"] == "revenue" else "unique", "scope": "visitor",
                     "winning_direction": "decreasing" if m["direction"] == "decrease" else "increasing", "role": m["role"]}
                    for m in plan["metrics"] if m["role"] in ("primary", "secondary")],
        "_caf": {"experiment_id": plan["experiment_id"], "guardrails": [m["event"] for m in plan["metrics"] if m["role"] == "guardrail"],
                 "planned_duration_days": plan["sizing"]["planned_duration_days"]},
    }


def export_vwo(plan: dict[str, Any]) -> dict[str, Any]:
    """VWO Testing campaign payload (type ab)."""
    return {
        "type": "ab", "name": plan["title"], "primaryUrl": plan["page_url"], "percentTraffic": plan["targeting"]["traffic_allocation"],
        "urls": {"editorUrl": plan["page_url"], "includeUrls": [{"type": "url", "value": u} for u in plan["targeting"]["pages"]], "excludeUrls": []},
        "goals": [{"name": m["name"], "type": "revenue" if m["type"] == "revenue" else "customConversion", "event": m["event"],
                   "isPrimary": m["role"] == "primary", "isGuardrail": m["role"] == "guardrail"} for m in plan["metrics"]],
        "variations": [{"name": v["name"], "percentSplit": v["allocation"], "isControl": v["is_control"], "description": v["description"]} for v in plan["variants"]],
        "segments": {"devices": plan["targeting"]["devices"], "audiences": plan["targeting"]["audiences"]},
        "hypothesis": plan["hypothesis"],
    }


def export_abtasty(plan: dict[str, Any]) -> dict[str, Any]:
    """AB Tasty test payload."""
    return {
        "name": plan["title"], "type": "ab", "url": plan["page_url"], "description": plan["hypothesis"],
        "traffic": plan["targeting"]["traffic_allocation"],
        "variations": [{"name": v["name"], "traffic": v["allocation"], "is_original": v["is_control"]} for v in plan["variants"]],
        "goals": [{"label": m["name"], "type": "custom_event", "event": m["event"], "primary": m["role"] == "primary"} for m in plan["metrics"]],
        "targeting": {"url_scope": [{"condition": "is", "value": u} for u in plan["targeting"]["pages"]], "devices": plan["targeting"]["devices"]},
    }


def export_launchdarkly(plan: dict[str, Any]) -> dict[str, Any]:
    """LaunchDarkly multivariate flag + experiment iteration payloads."""
    key = slugify(plan["experiment_id"], 60)
    return {
        "flag": {"key": key, "name": plan["title"], "kind": "multivariate", "description": plan["hypothesis"],
                 "variations": [{"value": v["id"], "name": v["name"], "description": v["description"]} for v in plan["variants"]],
                 "tags": ["caf", "experiment"]},
        "experiment": {"key": f"{key}-exp", "name": plan["title"],
                       "iteration": {"hypothesis": plan["hypothesis"], "canReshuffleTraffic": False,
                                     "metrics": [{"key": slugify(m["event"], 60), "isGroup": False} for m in plan["metrics"] if m["role"] != "guardrail"],
                                     "primarySingleMetricKey": slugify(next(m["event"] for m in plan["metrics"] if m["role"] == "primary"), 60),
                                     "treatments": [{"name": v["name"], "baseline": v["is_control"], "allocationPercent": str(v["allocation"]),
                                                     "parameters": [{"flagKey": key, "variationId": v["id"]}]} for v in plan["variants"]],
                                     "flags": {key: {"ruleId": "fallthrough", "flagConfigVersion": 1}}}},
        "guardrail_metrics": [slugify(m["event"], 60) for m in plan["metrics"] if m["role"] == "guardrail"],
    }


def export_adobe_target(plan: dict[str, Any]) -> dict[str, Any]:
    """Adobe Target Admin API A/B activity payload (form-based/VEC-agnostic)."""
    return {
        "name": plan["title"], "state": "saved", "priority": 0,
        "locations": {"selectors": [{"selectorLocalId": 0, "selector": "body"}]},
        "experiences": [{"experienceLocalId": i, "name": v["name"], "visitorPercentage": v["allocation"],
                         "optionLocations": [{"locationLocalId": 0, "optionLocalId": i}]} for i, v in enumerate(plan["variants"])],
        "options": [{"optionLocalId": i, "name": v["name"], "content": "<!-- CAF variation bundle -->"} for i, v in enumerate(plan["variants"])],
        "metrics": [{"metricLocalId": i, "name": m["name"], "conversion": m["type"] == "conversion", "action": {"type": "count_once"},
                     "mboxes": [{"name": m["event"]}]} for i, m in enumerate(plan["metrics"]) if m["role"] != "guardrail"],
        "reportingAudiences": [], "activityUrl": plan["page_url"],
        "_caf": {"traffic_allocation": plan["targeting"]["traffic_allocation"], "hypothesis": plan["hypothesis"]},
    }


def export_generic_yaml(plan: dict[str, Any]) -> str:
    keep = ("experiment_id", "title", "page_url", "design", "hypothesis", "variants", "targeting", "metrics", "sizing")
    neutral = {k: plan[k] for k in keep if k in plan}
    neutral["sizing"] = {k: v for k, v in plan["sizing"].items() if k != "sensitivity"}
    return yaml.safe_dump({"caf_experiment": neutral}, sort_keys=False, allow_unicode=True)


EXPORTERS: dict[str, tuple[str, Callable[[dict[str, Any]], Any]]] = {
    "optimizely": ("optimizely_experiment.json", export_optimizely),
    "vwo": ("vwo_campaign.json", export_vwo),
    "ab_tasty": ("abtasty_test.json", export_abtasty),
    "launchdarkly": ("launchdarkly_experiment.json", export_launchdarkly),
    "adobe_target": ("adobe_target_activity.json", export_adobe_target),
    "generic_yaml": ("experiment.yaml", export_generic_yaml),
}


def equal_allocations(n: int) -> list[float]:
    """Equal split in percent, rounded to 2 dp, summing to exactly 100."""
    base = math.floor(10000 / n) / 100
    alloc = [base] * n
    alloc[0] = round(100 - base * (n - 1), 2)
    return alloc


# ============================================================================ contract


class PlanInput(BaseModel):
    url: str = Field(min_length=4, description="Page under test (required)")
    test_idea: str = Field(min_length=3, description="The raw test idea to plan (required)", json_schema_extra={"ui": "textarea"})
    html: str | None = Field(default=None, description="Page HTML, used instead of fetching the url", json_schema_extra={"ui": "textarea"})
    baseline_rate: float | None = Field(default=None, gt=0, lt=1, description="Control conversion rate (default: analytics for the page)")
    daily_visitors: int | None = Field(default=None, gt=0, description="Eligible visitors per day (default: analytics for the page)")
    mde: float = Field(default=0.10, gt=0, le=2, description="Minimum detectable effect, relative (0.10 = +10%)")
    alpha: float = Field(default=0.05, gt=0, lt=0.5, description="Two-sided significance level")
    power: float = Field(default=0.8, gt=0.5, lt=1, description="Statistical power")
    n_variants: int = Field(default=2, ge=2, le=8, description="Number of arms including the control")
    design: Literal["frequentist", "bayesian", "sequential"] = "frequentist"
    traffic_allocation: float = Field(default=100.0, gt=0, le=100, description="Percent of eligible traffic in the experiment")
    max_duration_days: int = Field(default=42, ge=7, le=365, description="Longest acceptable runtime")
    looks: int = Field(default=4, ge=1, le=12, description="Analyses (interim + final) for the sequential design")
    primary_metric: str | None = Field(default=None, description="Primary metric name, e.g. 'Quote starts per visitor'")
    primary_event: str | None = Field(default=None, description="Analytics event behind the primary metric, e.g. 'quote_start'")
    devices: list[str] = Field(default_factory=list, description="Device targeting (empty = all)")
    experiment_id: str | None = Field(default=None, description="Id to register the plan under (default: derived from the idea)")
    exporters: list[str] = Field(default_factory=lambda: ["all"], description="optimizely, vwo, ab_tasty, launchdarkly, adobe_target, generic_yaml or all")
    screenshot: bool = Field(default=True, description="Capture a page screenshot with Playwright when available")


class HypothesisDraft(BaseModel):
    if_change: str = Field(description="The change, phrased after 'If ...'")
    then_outcome: str = Field(description="Measurable effect on the primary metric, phrased after 'then ...'")
    because: str = Field(description="Evidence-based reason, phrased after 'because ...'")


class VariantDraft(BaseModel):
    name: str
    description: str
    changes: list[str] = Field(default_factory=list)


class MetricDraft(BaseModel):
    name: str
    role: Literal["primary", "secondary", "guardrail"]
    type: Literal["conversion", "revenue", "count", "duration", "rate"] = "conversion"
    event: str = Field(description="Analytics event that defines the metric (snake_case)")
    definition: str = ""
    direction: Literal["increase", "decrease", "no_harm"] = "increase"


class TargetingDraft(BaseModel):
    audiences: list[str] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=list)
    geo: list[str] = Field(default_factory=list)
    conditions: list[str] = Field(default_factory=list)


class RiskDraft(BaseModel):
    risk: str
    likelihood: Literal["low", "medium", "high"] = "medium"
    mitigation: str


class PlanDraft(BaseModel):
    title: str
    hypothesis: HypothesisDraft
    variants: list[VariantDraft] = Field(min_length=1, description="Treatment arms only; the control is added by the system")
    targeting: TargetingDraft
    metrics: list[MetricDraft] = Field(min_length=1)
    risks: list[RiskDraft] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)


class PlanVariant(BaseModel):
    id: str
    name: str
    allocation: float
    is_control: bool
    description: str
    changes: list[str] = Field(default_factory=list)


class PlanMetric(BaseModel):
    id: str
    name: str
    role: Literal["primary", "secondary", "guardrail"]
    type: Literal["conversion", "revenue", "count", "duration", "rate"] = "conversion"
    event: str
    definition: str = ""
    direction: Literal["increase", "decrease", "no_harm"] = "increase"
    mde: float | None = None


class PlanTargeting(BaseModel):
    page_url: str
    pages: list[str]
    devices: list[str] = Field(default_factory=list)
    audiences: list[str] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=list)
    geo: list[str] = Field(default_factory=list)
    conditions: list[str] = Field(default_factory=list)
    traffic_allocation: float = 100.0


class Sizing(BaseModel):
    method: str
    baseline_rate: float
    baseline_source: str
    daily_visitors: float
    daily_visitors_source: str
    is_estimate: bool
    mde: float
    target_rate: float
    alpha: float
    alpha_per_comparison: float
    power: float
    n_variants: int
    sample_size_per_variant: int
    total_sample_size: int
    per_variant_daily: float
    duration_days_exact: float
    duration_days: int | None
    duration_weeks: int | None
    planned_duration_days: int | None
    max_duration_days: int
    achievable_mde_at_max_duration: float | None
    feasible: bool
    sensitivity: list[dict[str, Any]] = Field(default_factory=list)


class RiskItem(BaseModel):
    risk: str
    likelihood: Literal["low", "medium", "high"] = "medium"
    mitigation: str
    source: Literal["computed", "model", "template"] = "computed"


class PlanOutput(BaseModel):
    experiment_id: str
    title: str
    page_url: str
    test_idea: str
    design: str
    hypothesis: str
    hypothesis_parts: dict[str, str]
    variants: list[PlanVariant]
    targeting: PlanTargeting
    metrics: list[PlanMetric]
    sizing: Sizing
    sequential: dict[str, Any]
    bayesian: dict[str, Any]
    risks: list[RiskItem]
    assumptions: list[str]
    page: dict[str, Any] = Field(default_factory=dict)
    exports: list[dict[str, str]] = Field(default_factory=list)
    definition_upserted: bool = False
    markdown: str = ""


def _generate_or_none(ctx: RunContext, template: str, schema: type[BaseModel], **variables: Any) -> tuple[Any, str | None]:
    if ctx.llm.offline:
        ctx.render(template, **variables)
        return None, "no language model configured: hypothesis, variants and risks use deterministic templates"
    try:
        return ctx.generate(template, schema, **variables), None
    except Exception as exc:
        ctx.log("model call failed", template=template, error=repr(exc))
        return None, f"model call failed ({type(exc).__name__}); plan text uses deterministic templates"


def split_hypothesis(text: str) -> dict[str, str] | None:
    """'If A, then B, because C.' -> {'if': A, 'then': B, 'because': C} (None when the text is not in that form)."""
    m = re.match(r"(?is)^\s*if\s+(.+?),?\s+then\s+(.+?),?\s+because\s+(.+?)\.?\s*$", text or "")
    return {"if": m.group(1).strip(), "then": m.group(2).strip(), "because": m.group(3).strip()} if m else None


_IMPERATIVE = set("""add adjust animate bring change clarify collapse combine convert cut default display embed emphasise emphasize enlarge
expand feature fix give group hide highlight include increase insert introduce keep label launch lead let link lower make move offer
personalise personalize place prefill promote put raise redesign reduce remove rename reorder replace restructure rewrite run set
shorten show simplify sort split start state surface swap test try turn update use""".split())
_SUBJECT = re.compile(r"^(we|the|a|an|our|visitors|users|customers|members|it|this|these|each|every|all)\b", re.I)


def as_if_clause(idea: str) -> str:
    """'Change the CTA ...' -> 'we change the CTA ...' (imperative ideas become a first-person clause)."""
    idea = (idea or "").strip().rstrip(".")
    if not idea:
        return idea
    idea = re.sub(r"^\s*if\s+", "", idea, flags=re.I)
    first = idea.split()[0]
    lowered = idea if (first.isupper() and len(first) > 1) else first[0].lower() + idea[1:]
    if _SUBJECT.match(lowered):
        return lowered
    if first.lower() in _IMPERATIVE:
        return f"we {lowered}"
    return f"we make this change: {lowered}"


def _snake(text: str) -> str:
    return slugify(text, 40).replace("-", "_")


@register
class ExperimentPlanningAgent(BaseAgent):
    spec = AgentSpec(
        id="experiment_planning",
        name="Experiment Planning",
        category=Category.EXPERIMENTATION,
        summary="Turns a test idea into a full experiment plan - If/Then/Because hypothesis, variants, targeting, metrics, "
                "power analysis, duration, Bayesian and O'Brien-Fleming sequential designs - exported to any testing platform.",
        kind=AgentKind.HYBRID,
        tier=Tier.COMPLEX,
        connectors=["crawler", "analytics", "experiments"],
        skills=["experiment_review_rules", "experiment_decision_rules"],
        keywords=["experiment plan", "test plan", "sample size", "power analysis", "mde", "test duration", "hypothesis",
                  "sequential testing", "bayesian test", "a/b test design"],
        produces=["experiment_plan.md", "experiment_plan.json", "optimizely_experiment.json", "vwo_campaign.json", "abtasty_test.json",
                  "launchdarkly_experiment.json", "adobe_target_activity.json", "experiment.yaml", "page.png"],
        feeds=["variation_development", "experiment_review"],
    )
    Input = PlanInput
    Output = PlanOutput
    schemas = {"plan": PlanDraft}

    def selftest_input(self) -> dict[str, Any]:
        return {"url": DEMO_URL, "test_idea": "Change the hero CTA from 'Get a quote' to 'See my price in 2 minutes' and make it more prominent",
                "mde": 0.20, "design": "sequential", "looks": 4, "max_duration_days": 56, "experiment_id": "exp-hero-cta-price-promise",
                "primary_metric": "Quote start rate", "primary_event": "quote_start"}

    def stages(self):
        return [("inspect_page", self.inspect_page), ("baseline", self.baseline), ("size", self.size), ("draft", self.draft),
                ("reconcile", self.reconcile), ("export", self.export)]

    # -- stage 1 ---------------------------------------------------------------------------------
    def inspect_page(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp = state["inputs"]
        crawler = ctx.connector("crawler")
        warnings, reasons, artifacts = [], [], []
        html = inp.get("html")
        if not html:
            page = crawler.fetch(inp["url"])
            if page.ok and page.is_html:
                html = page.html
            else:
                warnings.append(f"Could not fetch {inp['url']} ({page.error or page.status}); the plan is written without page inspection.")
                reasons.append("page could not be fetched")
        facts = extract_page_facts(html or "", inp["url"])
        shot = None
        if inp.get("screenshot", True):
            if crawler.browser_available():
                try:
                    png = crawler.screenshot(url=None if inp.get("html") else inp["url"], html=inp.get("html"), full_page=False)
                    shot = ctx.artifacts.write_bytes("page.png", png, "image/png", "Page under test (above the fold)")
                    artifacts.append(shot)
                except Exception as exc:
                    warnings.append(f"Screenshot failed ({type(exc).__name__}); plan continues without it.")
            else:
                warnings.append("Playwright is not available; no page screenshot.")
                reasons.append("no browser for screenshots")
        out = {"data": {"facts": facts, "screenshot": shot.path if shot else None}, "warnings": warnings, "artifacts": artifacts}
        if reasons:
            out["provenance"] = {"degraded_reasons": reasons}
        return out

    # -- stage 2 ---------------------------------------------------------------------------------
    def baseline(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp = state["inputs"]
        warnings: list[str] = []
        reasons = list(state.get("provenance", {}).get("degraded_reasons", []))
        metrics: dict[str, Any] = {"available": False}
        try:
            an = ctx.connector("analytics")
            bounds = an.date_bounds()
            if bounds:
                start, end = bounds
                metrics = page_analytics(an.pages(start, end, limit=1000), urlparse(inp["url"]).path or "/", {}, days=(end - start).days + 1)
                metrics["period"] = f"{start} to {end}"
        except Exception as exc:
            warnings.append(f"Analytics unavailable ({type(exc).__name__}).")
        baseline, b_src = inp.get("baseline_rate"), "input"
        daily, d_src = inp.get("daily_visitors"), "input"
        estimate = False
        if baseline is None:
            if metrics.get("available") and metrics.get("conversion_rate"):
                baseline, b_src = float(metrics["conversion_rate"]), f"analytics: page conversion rate {metrics.get('period', '')}".strip()
            elif metrics.get("site_conversion_rate"):
                baseline, b_src, estimate = float(metrics["site_conversion_rate"]), "analytics: site-wide conversion rate (page not found)", True
            else:
                baseline, b_src, estimate = 0.03, "assumption: 3% (no analytics) - replace with a measured baseline", True
        if daily is None:
            if metrics.get("available") and (metrics.get("daily_sessions") or 0) > 0:
                daily, d_src = float(metrics["daily_sessions"]), f"analytics: average daily sessions {metrics.get('period', '')}".strip()
            else:
                daily, d_src, estimate = 1000.0, "assumption: 1,000 visitors/day (no analytics for the page) - replace with measured traffic", True
        if estimate:
            warnings.append("Sizing uses estimated inputs (see sizing.baseline_source / daily_visitors_source).")
            reasons.append("sizing inputs estimated (no page analytics)")
        if inp.get("traffic_allocation", 100) <= 1:
            warnings.append("traffic_allocation is a percentage: a value of 1 means 1% of eligible traffic.")
        out = {"data": {"analytics": metrics, "baseline": baseline, "baseline_source": b_src, "daily": daily, "daily_source": d_src,
                        "is_estimate": estimate}, "warnings": warnings}
        if reasons:
            out["provenance"] = {"degraded_reasons": reasons}
        return out

    # -- stage 3 ---------------------------------------------------------------------------------
    def size(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        rules = ctx.skill("experiment_review_rules", default={})
        decision = ctx.skill("experiment_decision_rules", default={})
        min_days = int(rules.get("defaults", {}).get("min_duration_days", 7))
        p, daily = float(data["baseline"]), float(data["daily"])
        alpha, power, k = float(inp["alpha"]), float(inp["power"]), int(inp["n_variants"])
        mde, alloc = float(inp["mde"]), float(inp["traffic_allocation"])
        checks: list[Check] = []
        try:
            n = sample_size_per_variant(p, mde, alpha, power, k)
        except ValueError as exc:
            raise ValueError(f"Cannot size the test: {exc}") from exc
        dur = duration_for_sample(n, daily, alloc, k, min_days)
        max_days = int(inp["max_duration_days"])
        max_full_weeks = max(7, (max_days // 7) * 7)
        ach = achievable_mde(p, dur["per_variant_daily"] * max_full_weeks, alpha, power, k)
        feasible = dur["days"] is not None and dur["days"] <= max_days
        seq = sequential_design(n, dur["per_variant_daily"], looks=int(inp["looks"]), alpha=alpha, power=power, n_variants=k, max_duration_days=max_days)
        bcfg = decision.get("bayesian", {})
        horizon_weeks = max(1, min(dur["weeks"] or 1, max_days // 7))  # evaluate the Bayesian rule over the fixed-horizon window
        bayes = bayesian_design(p, mde, dur["per_variant_daily"], horizon_weeks, prior_strength=float(bcfg.get("prior_strength", 100)),
                                prob_beat_min=float(bcfg.get("prob_beat_min", 0.95)), expected_loss_max_rel=float(bcfg.get("expected_loss_max_rel", 0.001)))
        planned = {"frequentist": dur["days"], "sequential": seq.get("max_duration_days"), "bayesian": horizon_weeks * 7}[inp["design"]]
        sizing = Sizing(
            method="Two-sided two-proportion z-test on Cohen's h (statsmodels NormalIndPower)" + (", Bonferroni-adjusted" if k > 2 else ""),
            baseline_rate=round(p, 6), baseline_source=data["baseline_source"], daily_visitors=round(daily, 1), daily_visitors_source=data["daily_source"],
            is_estimate=data["is_estimate"], mde=mde, target_rate=round(p * (1 + mde), 6), alpha=alpha, alpha_per_comparison=round(bonferroni_alpha(alpha, k), 6),
            power=power, n_variants=k, sample_size_per_variant=n, total_sample_size=n * k, per_variant_daily=dur["per_variant_daily"],
            duration_days_exact=dur["days_exact"], duration_days=dur["days"], duration_weeks=dur["weeks"], planned_duration_days=planned,
            max_duration_days=max_days, achievable_mde_at_max_duration=ach, feasible=feasible,
            sensitivity=sensitivity_table(p, daily, alpha=alpha, power=power, n_variants=k, traffic_allocation_pct=alloc, max_duration_days=max_days))
        checks.append(Check(rule="sizing.feasible", status="pass" if feasible else "fail", severity="high",
                            message=(f"{n:,} visitors per variant need {dur['days']} days ({dur['weeks']} weeks) at {dur['per_variant_daily']:,.0f} per variant per day"
                                     + ("" if feasible else f" - longer than the {max_days}-day limit; the achievable MDE in {max_full_weeks} days is "
                                        f"{(ach or 0):.1%}.")),
                            data={"sample_size_per_variant": n, "duration_days": dur["days"]}))
        if inp["design"] == "sequential" and seq.get("available"):
            checks.append(Check(rule="sizing.sequential", status="pass" if seq["feasible"] else "warn", severity="medium",
                                message=f"O'Brien-Fleming with {seq['looks']} looks: max {seq['max_sample_size_per_variant']:,} per variant "
                                        f"(x{seq['max_sample_inflation']:.3f}), expected {seq['expected_sample_fraction_h1']:.0%} of it if the MDE is real."))
        if inp["design"] == "bayesian":
            checks.append(Check(rule="sizing.bayesian", status="pass" if bayes["false_ship_rate"] <= 0.1 else "warn", severity="medium",
                                message=f"Bayesian rule ships a real {mde:.0%} lift with probability {bayes['power_at_max']:.0%} by week {bayes['max_weeks']} "
                                        f"and a null variant with probability {bayes['false_ship_rate']:.1%}."))
        if data["is_estimate"]:
            checks.append(Check(rule="sizing.inputs_measured", status="warn", severity="medium", message="Baseline or traffic is an estimate, not a measurement."))
        return {"data": {"sizing": sizing.model_dump(), "sequential": seq, "bayesian": bayes}, "checks": checks}

    # -- stage 4 ---------------------------------------------------------------------------------
    def draft(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        s = data["sizing"]
        idea_ctx = self._upstream_idea(ctx, inp["test_idea"])
        variables = dict(test_idea=inp["test_idea"], url=inp["url"], page_lines=page_fact_lines(data["facts"]), design=inp["design"],
                         n_treatments=int(inp["n_variants"]) - 1, primary_metric=inp.get("primary_metric"), primary_event=inp.get("primary_event"),
                         devices=inp.get("devices") or [],
                         sizing_lines=[f"Baseline conversion rate: {s['baseline_rate']:.2%} ({s['baseline_source']})",
                                       f"Minimum detectable effect: {s['mde']:.0%} relative (target rate {s['target_rate']:.2%})",
                                       f"Sample size: {s['sample_size_per_variant']:,} per variant, {s['total_sample_size']:,} in total",
                                       f"Duration: {s['duration_days']} days ({s['duration_weeks']} weeks); feasible within {s['max_duration_days']} days: {'yes' if s['feasible'] else 'no'}"],
                         evidence_lines=idea_ctx.get("evidence", []), idea_rationale=idea_ctx.get("rationale"))
        draft, reason = _generate_or_none(ctx, "plan", PlanDraft, **variables)
        out: dict[str, Any] = {"data": {"draft": draft.model_dump() if draft else None, "idea_ctx": idea_ctx}}
        if reason:
            out["provenance"] = {"degraded_reasons": list(state.get("provenance", {}).get("degraded_reasons", [])) + [reason]}
        return out

    def _upstream_idea(self, ctx: RunContext, test_idea: str) -> dict[str, Any]:
        up = ctx.upstream_output("experiment_ideation") or {}
        ideas = up.get("ideas") or []
        if not ideas:
            return {}
        sims = tfidf_similarity(test_idea, [f"{i.get('title', '')} {i.get('change', '')}" for i in ideas])
        best = int(np.argmax(sims)) if sims else 0
        if sims and sims[best] < 0.1:
            return {}
        idea = ideas[best]
        return {"title": idea.get("title"), "rationale": idea.get("rationale"), "hypothesis": idea.get("hypothesis"),
                "evidence": [f"{e.get('id')} ({e.get('source')}): {e.get('detail')}" for e in idea.get("evidence", [])],
                "primary_metric": idea.get("primary_metric")}

    # -- stage 5 ---------------------------------------------------------------------------------
    def reconcile(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        rules = ctx.skill("experiment_review_rules", default={})
        defaults = rules.get("defaults", {})
        s = data["sizing"]
        idea_ctx = data.get("idea_ctx") or {}
        d = PlanDraft.model_validate(data["draft"]) if data.get("draft") else None
        k = int(inp["n_variants"])
        idea = inp["test_idea"].strip().rstrip(".")
        primary_name = inp.get("primary_metric") or idea_ctx.get("primary_metric") or "Conversion rate"
        primary_event = inp.get("primary_event") or _snake(primary_name)
        checks: list[Check] = []
        # hypothesis
        target = f"by at least {s['mde']:.0%} relative to the {s['baseline_rate']:.2%} baseline"
        upstream_parts = split_hypothesis(idea_ctx.get("hypothesis") or "")
        if d:
            parts = {"if": d.hypothesis.if_change, "then": d.hypothesis.then_outcome, "because": d.hypothesis.because}
        elif upstream_parts:  # the idea came from Experiment Ideation: keep its evidence-based hypothesis, add the sized target
            parts = {**upstream_parts, "then": f"{upstream_parts['then']} ({target})"}
        else:
            because = idea_ctx.get("because") or "it removes uncertainty or effort at the page's main decision point (rationale to be confirmed by evidence)"
            parts = {"if": as_if_clause(idea), "then": f"{primary_name.lower()} will increase {target}", "because": because}
        hypothesis = compose_hypothesis(parts["if"], parts["then"], parts["because"])
        # variants: one control + (k-1) treatments with equal allocations computed here
        treatments = (d.variants if d else [])[: k - 1]
        while len(treatments) < k - 1:
            n_t = len(treatments) + 1
            treatments.append(VariantDraft(name=f"Variant {chr(65 + n_t)}", description=idea if n_t == 1 else f"{idea} (alternative execution {n_t})",
                                           changes=[idea]))
        allocs = equal_allocations(k)
        variants = [PlanVariant(id="control", name="Control", allocation=allocs[0], is_control=True, description="Current experience, unchanged")]
        for i, (t, a) in enumerate(zip(treatments, allocs[1:]), 1):
            variants.append(PlanVariant(id=f"variant_{chr(97 + i)}", name=t.name if d else f"Variant {chr(65 + i)}", allocation=a, is_control=False,
                                        description=t.description, changes=t.changes or [t.description]))
        # metrics: exactly one primary (input wins), at least one guardrail
        metrics: list[PlanMetric] = []
        drafted = d.metrics if d else []
        prim = next((m for m in drafted if m.role == "primary"), None)
        metrics.append(PlanMetric(id=_snake(primary_name), name=primary_name if inp.get("primary_metric") or not prim else prim.name, role="primary",
                                  type=prim.type if prim else "conversion", event=primary_event if inp.get("primary_event") or not prim else _snake(prim.event),
                                  definition=(prim.definition if prim and prim.definition else f"Unique visitors who trigger '{primary_event}' / visitors exposed"),
                                  direction="increase", mde=s["mde"]))
        for m in drafted:
            if m is prim:
                continue
            role = "secondary" if m.role == "primary" else m.role
            metrics.append(PlanMetric(id=_snake(m.name), name=m.name, role=role, type=m.type, event=_snake(m.event), definition=m.definition,
                                      direction=("no_harm" if role == "guardrail" else m.direction)))
        if not d:
            metrics.append(PlanMetric(id="cta_click_through", name="Primary CTA click-through rate", role="secondary", type="conversion",
                                      event="cta_click", definition="Visitors who click the primary CTA / visitors exposed", direction="increase"))
        if not any(m.role == "guardrail" for m in metrics):
            for g in defaults.get("guardrails", [{"id": "bounce_rate", "name": "Bounce rate", "type": "rate", "event": "bounce"}]):
                metrics.append(PlanMetric(id=g["id"], name=g["name"], role="guardrail", type=g.get("type", "rate"), event=g.get("event", g["id"]),
                                          definition=g.get("definition", ""), direction="no_harm"))
            checks.append(Check(rule="plan.guardrails", status="info", severity="low", message="Default guardrail metrics added from experiment_review_rules."))
        seen_ids: set[str] = set()
        metrics = [m for m in metrics if not (m.id in seen_ids or seen_ids.add(m.id))]
        page_path = urlparse(inp["url"]).path or "/"
        t = d.targeting if d else TargetingDraft(audiences=["All visitors to the page"], exclusions=["Internal and QA traffic", "Known bots"])
        targeting = PlanTargeting(page_url=inp["url"], pages=[inp["url"]], devices=[x.lower() for x in inp.get("devices") or []], audiences=t.audiences,
                                  exclusions=t.exclusions or ["Internal and QA traffic", "Known bots"], geo=t.geo, conditions=t.conditions,
                                  traffic_allocation=float(inp["traffic_allocation"]))
        # risks: computed facts first, then model / template risks
        risks: list[RiskItem] = []
        if not s["feasible"]:
            risks.append(RiskItem(risk=f"Underpowered: {s['duration_days']} days needed but the limit is {s['max_duration_days']} days.",
                                  likelihood="high", mitigation=f"Raise the MDE to at least {(s['achievable_mde_at_max_duration'] or 0):.1%}, "
                                                                f"reduce variants or increase traffic allocation."))
        if s["is_estimate"]:
            risks.append(RiskItem(risk="Sizing inputs are estimates, not measurements.", likelihood="medium",
                                  mitigation="Confirm the baseline and traffic from analytics before launch."))
        if k > 2:
            risks.append(RiskItem(risk=f"{k - 1} comparisons inflate false positives.", likelihood="medium",
                                  mitigation=f"Bonferroni alpha {s['alpha_per_comparison']:.4f} per comparison is already applied in the sizing."))
        if (s["duration_weeks"] or 0) <= 1:
            risks.append(RiskItem(risk="A one-week test is exposed to day-of-week and novelty effects.", likelihood="medium",
                                  mitigation="Keep the full week even if results look decisive early; do not peek without the sequential plan."))
        an = data.get("analytics", {})
        if an.get("period"):
            risks.append(RiskItem(risk="Seasonality and campaigns can shift the baseline during the test.", likelihood="low",
                                  mitigation=f"Baseline measured over {an['period']}; avoid launching during campaign spikes and annotate tracking outages."))
        if d:
            risks += [RiskItem(risk=r.risk, likelihood=r.likelihood, mitigation=r.mitigation, source="model") for r in d.risks]
        else:
            risks += [RiskItem(risk="Sample ratio mismatch from redirects, caching or bot traffic.", likelihood="low",
                               mitigation="Monitor the allocation daily with a chi-square SRM check (Experiment Review).", source="template"),
                      RiskItem(risk="Flicker when the variation is applied client-side.", likelihood="medium",
                               mitigation="Deliver the variation bundle early in <head> or at the edge; QA on slow networks.", source="template")]
        assumptions = (d.assumptions if d else []) + [
            f"Visitors are randomised independently at {targeting.traffic_allocation:g}% allocation with equal splits ({', '.join(f'{v.allocation:g}%' for v in variants)}).",
            f"The baseline of {s['baseline_rate']:.2%} holds during the test ({s['baseline_source']}).",
            f"'{metrics[0].event}' is tracked identically in every arm.",
        ]
        title = d.title if d else (idea_ctx.get("title") or truncate(idea, 80))
        exp_id = inp.get("experiment_id") or f"exp-{slugify(title, 40)}"
        hyp_ok = bool(re.match(r"(?is)^if\b.+\bthen\b.+\bbecause\b", hypothesis))
        checks.append(Check(rule="plan.hypothesis_format", status="pass" if hyp_ok else "fail", severity="medium", message="Hypothesis follows If / Then / Because."))
        checks.append(Check(rule="plan.allocation", status="pass" if abs(sum(v.allocation for v in variants) - 100) < 1e-6 else "fail", severity="high",
                            message=f"{len(variants)} arms, one control, allocations sum to {sum(v.allocation for v in variants):g}%."))
        return {"data": {"plan": {"experiment_id": exp_id, "title": title, "hypothesis": hypothesis, "hypothesis_parts": parts,
                                  "variants": [v.model_dump() for v in variants], "metrics": [m.model_dump() for m in metrics],
                                  "targeting": targeting.model_dump(), "risks": [r.model_dump() for r in risks], "assumptions": assumptions,
                                  "page_path": page_path}},
                "checks": checks}

    # -- stage 6 ---------------------------------------------------------------------------------
    def export(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        p = data["plan"]
        s = data["sizing"]
        facts = data["facts"]
        plan = {**p, "page_url": inp["url"], "test_idea": inp["test_idea"], "design": inp["design"], "sizing": s,
                "sequential": data["sequential"], "bayesian": data["bayesian"],
                "page": {"title": facts.get("title"), "h1": facts.get("h1"), "ctas": [{"text": c["text"], "selector": c["selector"]} for c in facts.get("ctas", [])[:8]],
                         "screenshot": data.get("screenshot")}}
        wanted = inp.get("exporters") or ["all"]
        names = list(EXPORTERS) if "all" in wanted else [w for w in wanted if w in EXPORTERS]
        warnings = [f"Unknown exporter '{w}' (available: {', '.join(EXPORTERS)})." for w in wanted if w != "all" and w not in EXPORTERS]
        artifacts, exports = [], []
        for name in names:
            filename, fn = EXPORTERS[name]
            payload = fn(plan)
            if isinstance(payload, str):
                ref = ctx.artifacts.write_text(filename, payload, "application/yaml", f"{name} export")
            else:
                ref = ctx.artifacts.write_json(filename, payload, f"{name} export (map event ids in your instance)")
            artifacts.append(ref)
            exports.append({"platform": name, "artifact": ref.path, "filename": filename})
        # platform-neutral definition in the experiment registry (run-local copy; sample data untouched)
        upserted = False
        prim = next(m for m in p["metrics"] if m["role"] == "primary")
        defn = ExperimentDefinition(
            id=p["experiment_id"], name=p["title"], status="draft", platform="internal", page_url=inp["url"], hypothesis=p["hypothesis"],
            primary_metric=Metric(id=prim["id"], name=prim["name"], type=prim["type"], event=prim["event"]),
            secondary_metrics=[Metric(id=m["id"], name=m["name"], type=m["type"], event=m["event"]) for m in p["metrics"] if m["role"] == "secondary"],
            guardrail_metrics=[Metric(id=m["id"], name=m["name"], type=m["type"], event=m["event"]) for m in p["metrics"] if m["role"] == "guardrail"],
            variants=[Variant(id=v["id"], name=v["name"], allocation=v["allocation"], is_control=v["is_control"], description=v["description"]) for v in p["variants"]],
            traffic_allocation=p["targeting"]["traffic_allocation"],
            audience=Audience(devices=p["targeting"]["devices"], geo=p["targeting"]["geo"], conditions=p["targeting"]["conditions"]),
            planned_duration_days=s["planned_duration_days"], baseline_rate=s["baseline_rate"], mde=s["mde"],
            expected_daily_visitors=int(round(s["daily_visitors"])),
            instrumentation={"events": [m["event"] for m in p["metrics"]], "verified": False, "design": inp["design"]},
        )
        try:
            ctx.connector("experiments").upsert(defn)
            upserted = True
        except Exception as exc:
            warnings.append(f"Could not register the plan in the experiment registry ({type(exc).__name__}).")
        plan["exports"] = exports
        plan["definition_upserted"] = upserted
        md = plan_markdown(plan)
        plan["markdown"] = md
        out = PlanOutput.model_validate(plan)
        artifacts += [ctx.artifacts.write_text("experiment_plan.md", md, "text/markdown", "Experiment plan"),
                      ctx.artifacts.write_json("experiment_plan.json", out.model_dump(mode="json", exclude={"markdown"}), "Experiment plan (platform-neutral)")]
        dur = s["planned_duration_days"]
        summary = (f"Plan '{truncate(out.title, 60)}' ({inp['design']}): {s['sample_size_per_variant']:,} per variant for a {s['mde']:.0%} MDE on a "
                   f"{s['baseline_rate']:.2%} baseline, {dur} days{'' if s['feasible'] else ' - exceeds the max duration'}; "
                   f"exported to {len(exports)} platform format(s)")
        return {"output": out.model_dump(mode="json"), "artifacts": artifacts, "warnings": warnings, "summary": summary}


def plan_markdown(plan: dict[str, Any]) -> str:
    s, seq, bay = plan["sizing"], plan["sequential"], plan["bayesian"]
    L = [f"# Experiment plan: {plan['title']}", "", f"- **Experiment id:** `{plan['experiment_id']}`", f"- **Page:** {plan['page_url']}",
         f"- **Design:** {plan['design']}", f"- **Idea:** {plan['test_idea']}", "", "## Hypothesis", plan["hypothesis"], "", "## Variants",
         "| Id | Name | Allocation | Description |", "|---|---|---|---|"]
    L += [f"| {v['id']} | {v['name']}{' (control)' if v['is_control'] else ''} | {v['allocation']:g}% | {v['description']} |" for v in plan["variants"]]
    t = plan["targeting"]
    L += ["", "## Targeting", f"- Pages: {', '.join(t['pages'])}", f"- Devices: {', '.join(t['devices']) or 'all'}",
          f"- Audiences: {', '.join(t['audiences']) or 'all visitors'}", f"- Exclusions: {', '.join(t['exclusions']) or 'none'}",
          f"- Traffic allocation: {t['traffic_allocation']:g}% of eligible visitors", "", "## Metrics",
          "| Role | Metric | Event | Type | Direction | Definition |", "|---|---|---|---|---|---|"]
    L += [f"| {m['role']} | {m['name']} | `{m['event']}` | {m['type']} | {m['direction']} | {m.get('definition', '')} |" for m in plan["metrics"]]
    L += ["", "## Statistical sizing (computed, not estimated by a model)", f"- Method: {s['method']}",
          f"- Baseline: {s['baseline_rate']:.2%} ({s['baseline_source']}){' - ESTIMATE' if s['is_estimate'] else ''}",
          f"- Traffic: {s['daily_visitors']:,.0f} eligible visitors/day ({s['daily_visitors_source']}), {s['per_variant_daily']:,.0f} per variant/day",
          f"- MDE: {s['mde']:.1%} relative ({s['baseline_rate']:.2%} -> {s['target_rate']:.2%}), alpha {s['alpha']:g} two-sided"
          + (f" ({s['alpha_per_comparison']:.4f} per comparison)" if s["n_variants"] > 2 else "") + f", power {s['power']:.0%}",
          f"- **Sample size: {s['sample_size_per_variant']:,} per variant ({s['total_sample_size']:,} total)**",
          f"- **Duration (fixed horizon): {s['duration_days']} days = {s['duration_weeks']} whole weeks** ({s['duration_days_exact']} days of traffic)",
          f"- Achievable MDE within {s['max_duration_days']} days: {(s['achievable_mde_at_max_duration'] or 0):.1%}",
          f"- Feasible within the limit: {'yes' if s['feasible'] else 'NO'}", "", "| MDE | Per variant | Weeks | Feasible |", "|---|---|---|---|"]
    L += [f"| {r['mde']:.1%} | {r['sample_size_per_variant']:,} | {r['duration_weeks']} | {'yes' if r['feasible'] else 'no'} |" for r in s["sensitivity"]]
    if seq.get("available"):
        L += ["", "## Sequential design (O'Brien-Fleming)", f"- {seq['looks']} analyses at whole weeks, constant C = {seq['constant']}, alpha {seq['alpha']:g}",
              f"- Max sample: {seq['max_sample_size_per_variant']:,} per variant (inflation x{seq['max_sample_inflation']:.3f}), up to {seq['max_duration_days']} days",
              f"- Expected sample if the MDE is real: {seq['expected_sample_fraction_h1']:.0%} of the maximum", "",
              "| Look | Week | Per variant | Z boundary | Nominal p | Alpha spent |", "|---|---|---|---|---|---|"]
        L += [f"| {r['look']} | {r['week']} | {r['cumulative_per_variant']:,} | {r['z_boundary']:.3f} | {r['nominal_alpha']:.5f} | {r['cumulative_alpha_spent']:.5f} |"
              for r in seq["schedule"]]
    if bay.get("available"):
        pr = bay["prior"]
        L += ["", "## Bayesian design", f"- Prior: Beta({pr['a']:g}, {pr['b']:g}) - mean {pr['mean']:.2%}, worth {pr['strength_visitors']:g} visitors",
              f"- Decision rule: {bay['decision_rule']['text']}",
              f"- Probability of shipping a real MDE lift by week {bay['max_weeks']}: {bay['power_at_max']:.0%}; shipping a null variant: {bay['false_ship_rate']:.1%}",
              f"- {bay['method']}", "", "| Week | Per variant | P(ship) if lift = MDE | P(ship) if no lift |", "|---|---|---|---|"]
        L += [f"| {w['week']} | {w['cumulative_per_variant']:,} | {w['p_ship_if_lift_is_mde']:.0%} | {w['p_ship_if_no_lift']:.1%} |" for w in bay["weekly"]]
    L += ["", "## Risks", "| Risk | Likelihood | Mitigation |", "|---|---|---|"]
    L += [f"| {r['risk']} | {r['likelihood']} | {r['mitigation']} |" for r in plan["risks"]]
    L += ["", "## Critical assumptions"] + [f"- {a}" for a in plan["assumptions"]]
    if plan.get("exports"):
        L += ["", "## Platform exports"] + [f"- {e['platform']}: `{e['filename']}`" for e in plan["exports"]]
    return "\n".join(L) + "\n"
