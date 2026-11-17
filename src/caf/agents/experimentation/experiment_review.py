"""Experiment Review - a deterministic, CI-enforceable pre-launch gate over the platform-neutral experiment definition.

Rules live in config/skills/experiment_review_rules.yaml and run in code: allocation, metric validity, guardrails,
power-analysis sample-size sufficiency (expected daily visitors x traffic x allocation x duration), whole-week
duration, targeting overlap with other live tests (through the ExperimentConnector), interim SRM (chi-square),
instrumentation, QA links and hypothesis format. The configuration is read through the experiments connector,
so the same gate works for Optimizely, VWO, Adobe Target, LaunchDarkly or an internal registry. The model only
explains findings and suggests fixes; the gate (pass | fail) and the JUnit XML report are pure code.
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from typing import Any, Callable, Literal
from urllib.parse import urlparse

import pandas as pd
from pydantic import BaseModel, Field

from ...connectors.experiments import Audience, ExperimentDefinition, Metric, Variant
from ...core import AgentKind, AgentSpec, BaseAgent, Category, Check, RunContext, Tier, register
from ...core.handoff import Finding
from ...core.textutil import truncate
from .experiment_planning import bonferroni_alpha, sample_size_per_variant

# ============================================================================ helpers (pure)


def _d(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except ValueError:
        return None


def page_key(url: str | None) -> str:
    p = urlparse(url or "")
    return ((p.hostname or "") + (p.path or "/").rstrip("/")).lower()


def _overlap(a: list[str], b: list[str]) -> bool:
    """Audience lists overlap; an empty list means 'everyone'."""
    if not a or not b:
        return True
    return bool({x.lower() for x in a} & {x.lower() for x in b})


def _dates_overlap(a: ExperimentDefinition, b: ExperimentDefinition) -> bool:
    s1, e1, s2, e2 = _d(a.start_date), _d(a.end_date), _d(b.start_date), _d(b.end_date)
    if not (s1 and s2):
        return True
    e1 = e1 or date.max
    e2 = e2 or date.max
    return s1 <= e2 and s2 <= e1


def srm_test(observed: dict[str, float], planned_pct: dict[str, float]) -> dict[str, Any]:
    """Chi-square goodness of fit of observed visitors per variant against the planned allocation."""
    from scipy.stats import chisquare

    keys = [k for k in planned_pct if k in observed]
    total_obs = sum(observed[k] for k in keys)
    total_plan = sum(planned_pct[k] for k in keys)
    if not keys or total_obs <= 0 or total_plan <= 0:
        return {"available": False}
    expected = [total_obs * planned_pct[k] / total_plan for k in keys]
    stat, p = chisquare([observed[k] for k in keys], f_exp=expected)
    return {"available": True, "chi2": round(float(stat), 4), "p_value": float(p), "dof": len(keys) - 1,
            "observed": {k: int(observed[k]) for k in keys}, "expected": {k: round(e, 1) for k, e in zip(keys, expected)},
            "observed_share": {k: round(observed[k] / total_obs, 4) for k in keys}}


def results_by_variant(results: pd.DataFrame | None) -> dict[str, float]:
    if results is None or results.empty or "variant" not in results:
        return {}
    return {str(k): float(v) for k, v in results.groupby("variant")["visitors"].sum().items()}


def definition_from_plan(plan: dict[str, Any]) -> ExperimentDefinition:
    """Platform-neutral definition from an upstream Experiment Planning output."""
    metrics = plan.get("metrics", [])
    m = lambda x: Metric(id=x["id"], name=x["name"], type=x.get("type", "conversion"), event=x.get("event"))
    prim = next((x for x in metrics if x.get("role") == "primary"), None)
    t = plan.get("targeting", {})
    s = plan.get("sizing", {})
    return ExperimentDefinition(
        id=plan["experiment_id"], name=plan.get("title", plan["experiment_id"]), status="draft", page_url=plan.get("page_url", ""),
        hypothesis=plan.get("hypothesis", ""), primary_metric=m(prim) if prim else None,
        secondary_metrics=[m(x) for x in metrics if x.get("role") == "secondary"], guardrail_metrics=[m(x) for x in metrics if x.get("role") == "guardrail"],
        variants=[Variant(id=v["id"], name=v["name"], allocation=v["allocation"], is_control=v["is_control"], description=v.get("description", ""))
                  for v in plan.get("variants", [])],
        traffic_allocation=t.get("traffic_allocation", 100), audience=Audience(devices=t.get("devices", []), geo=t.get("geo", []), conditions=t.get("conditions", [])),
        planned_duration_days=s.get("planned_duration_days"), baseline_rate=s.get("baseline_rate"), mde=s.get("mde"),
        expected_daily_visitors=int(round(s.get("daily_visitors") or 0)) or None, instrumentation={"events": [x.get("event") for x in metrics if x.get("event")]},
    )


# ============================================================================ rules (pure)
# Each rule: fn(defn, env, params) -> (outcome, message, data); outcome in {"pass", "fail", "na"}.

RuleFn = Callable[[ExperimentDefinition, dict[str, Any], dict[str, Any]], tuple[str, str, dict[str, Any]]]


def r_alloc_sum(d: ExperimentDefinition, env: dict[str, Any], p: dict[str, Any]):
    total = sum(v.allocation for v in d.variants)
    ok = abs(total - 100) <= float(p.get("tolerance", 0.01))
    return ("pass" if ok else "fail", f"Variant allocations sum to {total:g}%" + ("" if ok else " instead of 100%") + ".",
            {"total": total, "allocations": {v.id: v.allocation for v in d.variants}})


def r_single_control(d, env, p):
    n = sum(1 for v in d.variants if v.is_control)
    return ("pass" if n == 1 else "fail", f"{n} variant(s) marked as control.", {"controls": n})


def r_min_variants(d, env, p):
    n = len(d.variants)
    return ("pass" if n >= int(p.get("min_variants", 2)) else "fail", f"{n} variant(s) configured.", {"variants": n})


def r_balanced(d, env, p):
    if len(d.variants) < 2:
        return "na", "Fewer than two variants.", {}
    allocs = [v.allocation for v in d.variants]
    spread = max(allocs) - min(allocs)
    return ("pass" if spread <= float(p.get("max_spread_pct", 1.0)) else "fail",
            f"Allocation spread is {spread:g} points ({', '.join(f'{v.id} {v.allocation:g}%' for v in d.variants)}).", {"spread": spread})


def r_traffic_range(d, env, p):
    t = d.traffic_allocation
    ok = float(p.get("min_pct", 1)) <= t <= float(p.get("max_pct", 100))
    return ("pass" if ok else "fail", f"Traffic allocation is {t:g}%.", {"traffic_allocation": t})


def r_primary(d, env, p):
    m = d.primary_metric
    if m is None:
        return "fail", "No primary metric is defined.", {}
    if not (m.event or "").strip():
        return "fail", f"Primary metric '{m.name}' has no event.", {}
    return "pass", f"Primary metric '{m.name}' ({m.type}) measured by event '{m.event}'.", {"event": m.event}


def r_guardrails(d, env, p):
    n = len(d.guardrail_metrics)
    ok = n >= int(p.get("min_guardrails", 1))
    return ("pass" if ok else "fail", f"{n} guardrail metric(s)" + (": " + ", ".join(g.name for g in d.guardrail_metrics) if n else "") + ".", {"guardrails": n})


def r_secondary_limit(d, env, p):
    n = len(d.secondary_metrics)
    return ("pass" if n <= int(p.get("max_secondary", 5)) else "fail", f"{n} secondary metric(s).", {"secondary": n})


def r_baseline(d, env, p):
    b = d.baseline_rate
    ok = b is not None and 0 < b < 1
    return ("pass" if ok else "fail", f"Baseline conversion rate {b:.2%}." if ok else "No valid baseline conversion rate recorded.", {"baseline_rate": b})


def r_mde(d, env, p):
    if d.mde is None:
        return "fail", "No minimum detectable effect recorded.", {}
    ok = float(p.get("min_mde", 0.02)) <= d.mde <= float(p.get("max_mde", 0.5))
    return ("pass" if ok else "fail", f"Relative MDE is {d.mde:.1%}" + ("" if ok else f" (programme range {p.get('min_mde', 0.02):.0%}-{p.get('max_mde', 0.5):.0%})") + ".",
            {"mde": d.mde})


def sample_size_check(d: ExperimentDefinition, alpha: float, power: float, margin: float = 1.0) -> dict[str, Any]:
    """Required visitors per variant (power analysis) vs what the plan delivers in its smallest arm."""
    missing = [k for k, v in (("baseline_rate", d.baseline_rate), ("mde", d.mde), ("expected_daily_visitors", d.expected_daily_visitors),
                                ("planned_duration_days", d.planned_duration_days)) if not v]
    if missing or len(d.variants) < 2:
        return {"available": False, "missing": missing or ["variants"]}
    k = len(d.variants)
    try:
        required = sample_size_per_variant(float(d.baseline_rate), float(d.mde), alpha, power, k)
    except ValueError as exc:
        return {"available": False, "missing": [str(exc)]}
    total_alloc = sum(v.allocation for v in d.variants) or 100.0
    smallest = min(d.variants, key=lambda v: v.allocation)
    share = smallest.allocation / total_alloc  # as the platform would normalise it
    daily_arm = float(d.expected_daily_visitors) * d.traffic_allocation / 100 * share
    available = daily_arm * float(d.planned_duration_days)
    days_needed = math.ceil(required * margin / daily_arm) if daily_arm > 0 else None
    return {"available": True, "required_per_variant": required, "available_smallest_arm": int(available), "smallest_arm": smallest.id,
            "smallest_arm_share": round(share, 4), "daily_per_smallest_arm": round(daily_arm, 1), "coverage": round(available / required, 4),
            "days_needed": days_needed, "weeks_needed": math.ceil(days_needed / 7) if days_needed else None,
            "alpha_per_comparison": round(bonferroni_alpha(alpha, k), 6), "power": power, "variants": k}


def r_sample_size(d, env, p):
    s = env["sample_size"]
    if not s.get("available"):
        return "fail", f"Cannot verify sample size - missing {', '.join(s.get('missing', []))}.", s
    ok = s["coverage"] >= float(p.get("margin", 1.0))
    msg = (f"Needs {s['required_per_variant']:,} visitors per variant (alpha {s['alpha_per_comparison']:g}, power {s['power']:.0%}); "
           f"the plan delivers {s['available_smallest_arm']:,} in the smallest arm ({s['coverage']:.1%} of required)")
    if not ok:
        msg += f" - about {s['days_needed']:,} days ({s['weeks_needed']:,} weeks) at the planned traffic"
    return ("pass" if ok else "fail", msg + ".", s)


def r_min_days(d, env, p):
    n = d.planned_duration_days
    if n is None:
        return "fail", "No planned duration.", {}
    return ("pass" if n >= int(p.get("min_days", 7)) else "fail", f"Planned duration is {n} day(s).", {"days": n})


def r_whole_weeks(d, env, p):
    n = d.planned_duration_days
    if n is None:
        return "na", "No planned duration.", {}
    return ("pass" if n % 7 == 0 and n > 0 else "fail", f"{n} days = {n / 7:.2f} weeks.", {"days": n})


def r_max_days(d, env, p):
    n = d.planned_duration_days
    if n is None:
        return "na", "No planned duration.", {}
    return ("pass" if n <= int(p.get("max_days", 56)) else "fail", f"Planned duration is {n} days (maximum {p.get('max_days', 56)}).", {"days": n})


def r_dates(d, env, p):
    s, e = _d(d.start_date), _d(d.end_date)
    if not (s and e and d.planned_duration_days):
        return "na", "Start/end dates not both set.", {}
    exclusive, inclusive = (e - s).days, (e - s).days + 1
    ok = d.planned_duration_days in (exclusive, inclusive) and e >= s
    return ("pass" if ok else "fail", f"{d.start_date} to {d.end_date} is {inclusive} calendar days; planned {d.planned_duration_days}.",
            {"calendar_days": inclusive})


def find_overlaps(d: ExperimentDefinition, others: list[ExperimentDefinition], statuses: list[str]) -> list[dict[str, Any]]:
    out = []
    for o in others:
        if o.id == d.id or o.status not in statuses:
            continue
        if page_key(o.page_url) != page_key(d.page_url):
            continue
        if not (_overlap(d.audience.devices, o.audience.devices) and _overlap(d.audience.geo, o.audience.geo)):
            continue
        if not _dates_overlap(d, o):
            continue
        shared = sorted({x.lower() for x in d.audience.devices} & {x.lower() for x in o.audience.devices}) or ["all devices"]
        out.append({"id": o.id, "name": o.name, "status": o.status, "devices": shared, "dates": f"{o.start_date or '?'} to {o.end_date or '?'}"})
    return out


def r_overlap(d, env, p):
    hits = env["overlaps"]
    if not hits:
        return "pass", f"No {'/'.join(p.get('statuses', ['running']))} test targets the same page and audience in the same period.", {}
    desc = "; ".join(f"{h['id']} ({h['status']}, {', '.join(h['devices'])}, {h['dates']})" for h in hits)
    return "fail", f"Overlaps with {len(hits)} live test(s) on the same page and audience: {desc}.", {"overlaps": hits}


def r_srm(d, env, p):
    srm = env["srm"]
    if not srm.get("available"):
        return "na", "No interim results to check for sample ratio mismatch.", {}
    total = sum(srm["observed"].values())
    if total < int(p.get("min_visitors", 1000)):
        return "na", f"Only {total:,} visitors so far; SRM check needs {p.get('min_visitors', 1000):,}.", srm
    bad = srm["p_value"] < float(p.get("p_threshold", 0.001))
    msg = (f"Observed {', '.join(f'{k} {v:,}' for k, v in srm['observed'].items())} vs expected "
           f"{', '.join(f'{k} {v:,.0f}' for k, v in srm['expected'].items())}: chi-square {srm['chi2']:.2f}, p = {srm['p_value']:.2e}")
    seg = srm.get("worst_segment")
    if bad and seg:
        msg += f"; the imbalance is concentrated in the '{seg['segment']}' segment (p = {seg['p_value']:.2e})"
    return ("fail" if bad else "pass", msg + ".", srm)


def _events(d: ExperimentDefinition) -> set[str]:
    ev = d.instrumentation.get("events") if isinstance(d.instrumentation, dict) else None
    return {str(e) for e in (ev or [])}


def r_instr_primary(d, env, p):
    if d.primary_metric is None or not d.primary_metric.event:
        return "fail", "No primary metric event to check.", {}
    ok = d.primary_metric.event in _events(d)
    return ("pass" if ok else "fail", f"Primary event '{d.primary_metric.event}' is {'instrumented' if ok else 'NOT in the instrumentation list'}.",
            {"instrumented": sorted(_events(d))})


def r_instr_metrics(d, env, p):
    needed = [m.event for m in d.secondary_metrics + d.guardrail_metrics if m.event]
    missing = [e for e in needed if e not in _events(d)]
    if not needed:
        return "na", "No secondary or guardrail events defined.", {}
    return ("pass" if not missing else "fail", f"{len(needed) - len(missing)} of {len(needed)} secondary/guardrail events instrumented"
            + (f"; missing: {', '.join(missing)}" if missing else "") + ".", {"missing": missing})


def r_qa_present(d, env, p):
    links = d.qa_links or []
    without = [v.id for v in d.variants if not any(re.search(rf"[?&](?:variant|variation|v)={re.escape(v.id)}(?:&|$|#)", l) for l in links)]
    named = len(without) < len(d.variants)  # the links name their variants, so coverage can be checked per variant
    ok = bool(links) and len(links) >= len(d.variants) and (not named or not without)
    msg = f"{len(links)} QA link(s) for {len(d.variants)} variant(s)" + (f"; no link for {', '.join(without)}" if named and without else "")
    return ("pass" if ok else "fail", msg + ".", {"qa_links": links, "variants_without_link": without if named else []})


def r_qa_valid(d, env, p):
    links = d.qa_links or []
    if not links:
        return "na", "No QA links.", {}
    bad = [l for l in links if not re.match(r"^https?://[^\s/]+", l)]
    return ("pass" if not bad else "fail", "All QA links are absolute URLs." if not bad else f"Invalid QA links: {', '.join(bad)}.", {})


HYPOTHESIS_RE = re.compile(r"(?is)^\s*if\b.{5,}?\bthen\b.{5,}?\bbecause\b.{5,}")


def r_hypothesis(d, env, p):
    ok = bool(HYPOTHESIS_RE.match(d.hypothesis or ""))
    return ("pass" if ok else "fail", "Hypothesis follows If / Then / Because." if ok else
            f"Hypothesis does not follow If / Then / Because: '{truncate(d.hypothesis or '(empty)', 90)}'.", {})


def r_owner(d, env, p):
    return ("pass" if (d.owner or "").strip() else "fail", f"Owner: {d.owner}." if d.owner else "No owner assigned.", {})


RULES: dict[str, RuleFn] = {
    "allocation.sum_100": r_alloc_sum, "allocation.single_control": r_single_control, "allocation.min_variants": r_min_variants,
    "allocation.balanced": r_balanced, "traffic.allocation_range": r_traffic_range, "metric.primary_defined": r_primary,
    "metric.guardrails_present": r_guardrails, "metric.secondary_limit": r_secondary_limit, "stats.baseline_present": r_baseline,
    "stats.mde_realistic": r_mde, "stats.sample_size_sufficient": r_sample_size, "duration.min_days": r_min_days,
    "duration.whole_weeks": r_whole_weeks, "duration.max_days": r_max_days, "dates.consistent": r_dates, "targeting.overlap": r_overlap,
    "srm.interim": r_srm, "instrumentation.primary_event": r_instr_primary, "instrumentation.metric_events": r_instr_metrics,
    "qa.links_present": r_qa_present, "qa.links_valid": r_qa_valid, "hypothesis.format": r_hypothesis, "metadata.owner": r_owner,
}


def segment_srm(results: pd.DataFrame | None, planned: dict[str, float]) -> list[dict[str, Any]]:
    if results is None or results.empty or "segment" not in results:
        return []
    out = []
    for seg, g in results.groupby("segment"):
        t = srm_test({str(k): float(v) for k, v in g.groupby("variant")["visitors"].sum().items()}, planned)
        if t.get("available"):
            out.append({"segment": str(seg), "p_value": t["p_value"], "chi2": t["chi2"], "observed": t["observed"]})
    return sorted(out, key=lambda s: s["p_value"])


def build_env(defn: ExperimentDefinition, others: list[ExperimentDefinition], results: pd.DataFrame | None, cfg: dict[str, Any]) -> dict[str, Any]:
    stats = cfg.get("stats", {})
    rule_params = {r["id"]: r.get("params", {}) for r in cfg.get("rules", [])}
    planned = {v.id: v.allocation for v in defn.variants}
    srm = srm_test(results_by_variant(results), planned)
    if srm.get("available"):
        segs = segment_srm(results, planned)
        srm["segments"] = segs
        thr = float(rule_params.get("srm.interim", {}).get("p_threshold", 0.001))
        if segs and segs[0]["p_value"] < thr:
            srm["worst_segment"] = segs[0]
    return {"sample_size": sample_size_check(defn, float(stats.get("alpha", 0.05)), float(stats.get("power", 0.8)),
                                             float(rule_params.get("stats.sample_size_sufficient", {}).get("margin", 1.0))),
            "overlaps": find_overlaps(defn, others, list(rule_params.get("targeting.overlap", {}).get("statuses", ["running", "paused"]))),
            "srm": srm}


def evaluate_rules(defn: ExperimentDefinition, env: dict[str, Any], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Run every enabled rule; returns rule results with check status (pass | fail | warn | info) and gate impact."""
    out = []
    for rule in cfg.get("rules", []):
        if not rule.get("enabled", True):
            continue
        fn = RULES.get(rule["id"])
        if fn is None:
            out.append({"rule_id": rule["id"], "name": rule.get("name", rule["id"]), "status": "info", "outcome": "na", "severity": rule.get("severity", "low"),
                        "blocking": False, "message": "No implementation for this rule id (check the skill file).", "fix": rule.get("fix", ""),
                        "ref": rule.get("ref"), "data": {}})
            continue
        try:
            outcome, message, data = fn(defn, env, rule.get("params", {}) or {})
        except Exception as exc:  # a rule must never take the gate down
            outcome, message, data = "fail", f"Rule error: {type(exc).__name__}: {exc}", {}
        blocking = bool(rule.get("blocking", True))
        status = {"pass": "pass", "na": "info"}.get(outcome, "fail" if blocking else "warn")
        out.append({"rule_id": rule["id"], "name": rule.get("name", rule["id"]), "status": status, "outcome": outcome,
                    "severity": rule.get("severity", "medium"), "blocking": blocking, "message": message, "fix": rule.get("fix", ""),
                    "ref": rule.get("ref"), "data": data})
    return out


def gate(results: list[dict[str, Any]]) -> str:
    return "fail" if any(r["status"] == "fail" for r in results) else "pass"


def junit_xml(experiment_id: str, results: list[dict[str, Any]], gate_status: str, explanations: dict[str, str] | None = None) -> str:
    """JUnit XML for CI: failures for blocking rule failures, system-out for warnings, skipped for not-applicable rules."""
    explanations = explanations or {}
    fails = sum(1 for r in results if r["status"] == "fail")
    skipped = sum(1 for r in results if r["status"] == "info")
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    suites = ET.Element("testsuites", name="caf.experiment_review", tests=str(len(results)), failures=str(fails), errors="0", skipped=str(skipped))
    suite = ET.SubElement(suites, "testsuite", name=f"experiment_review.{experiment_id}", tests=str(len(results)), failures=str(fails),
                          errors="0", skipped=str(skipped), timestamp=stamp)
    props = ET.SubElement(suite, "properties")
    ET.SubElement(props, "property", name="gate", value=gate_status)
    ET.SubElement(props, "property", name="experiment_id", value=experiment_id)
    for r in results:
        case = ET.SubElement(suite, "testcase", classname=f"experiment_review.{experiment_id}.{r['rule_id'].split('.')[0]}", name=r["rule_id"], time="0")
        detail = r["message"] + (f"\nFix: {r['fix']}" if r.get("fix") else "") + (f"\nWhy: {explanations[r['rule_id']]}" if explanations.get(r["rule_id"]) else "")
        if r["status"] == "fail":
            ET.SubElement(case, "failure", message=truncate(r["message"], 300), type=r["severity"]).text = detail
        elif r["status"] == "warn":
            ET.SubElement(case, "system-out").text = f"WARNING ({r['severity']}): {detail}"
        elif r["status"] == "info":
            ET.SubElement(case, "skipped", message=truncate(r["message"], 300))
    ET.indent(suites)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(suites, encoding="unicode") + "\n"


# ============================================================================ contract


class ReviewInput(BaseModel):
    experiment_id: str | None = Field(default=None, description="Experiment to review (read through the experiments connector)")
    definition: dict[str, Any] | None = Field(default=None, description="Platform-neutral experiment definition (alternative to experiment_id)",
                                              json_schema_extra={"ui": "json"})
    explain: bool = Field(default=True, description="Ask the model to explain findings and suggest fixes (gate is unaffected)")


class RuleResult(BaseModel):
    rule_id: str
    name: str
    status: Literal["pass", "fail", "warn", "info"]
    severity: Literal["low", "medium", "high", "critical"]
    blocking: bool
    message: str
    fix: str = ""
    explanation: str = ""
    ref: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class ReviewOutput(BaseModel):
    experiment_id: str
    experiment_name: str
    platform: str
    status: str
    gate: Literal["pass", "fail"]
    blocking_failures: int
    warnings: int
    passed: int
    not_applicable: int
    results: list[RuleResult]
    findings: list[Finding]
    sample_size: dict[str, Any] = Field(default_factory=dict)
    srm: dict[str, Any] = Field(default_factory=dict)
    overlaps: list[dict[str, Any]] = Field(default_factory=list)
    explanation: str = ""
    junit_artifact: str | None = None
    rules_version: str = ""


class FixItem(BaseModel):
    rule_id: str
    explanation: str = Field(description="Why the finding matters for this experiment, in plain language")
    fix: str = Field(description="The concrete change to make")


class ReviewExplanation(BaseModel):
    summary: str = Field(description="Two or three sentences for the experiment owner")
    items: list[FixItem] = Field(default_factory=list)


def _generate_or_none(ctx: RunContext, template: str, schema: type[BaseModel], **variables: Any) -> tuple[Any, str | None]:
    if ctx.llm.offline:
        ctx.render(template, **variables)
        return None, "no language model configured: findings explained with the rule pack's fix texts"
    try:
        return ctx.generate(template, schema, **variables), None
    except Exception as exc:
        ctx.log("model call failed", template=template, error=repr(exc))
        return None, f"model call failed ({type(exc).__name__}); findings explained with the rule pack's fix texts"


_SEV_CATEGORY = {"allocation": "experimentation", "traffic": "experimentation", "metric": "analytics", "stats": "experimentation",
                 "duration": "experimentation", "dates": "experimentation", "targeting": "experimentation", "srm": "analytics",
                 "instrumentation": "analytics", "qa": "experimentation", "hypothesis": "experimentation", "metadata": "experimentation"}


@register
class ExperimentReviewAgent(BaseAgent):
    spec = AgentSpec(
        id="experiment_review",
        name="Experiment Review",
        category=Category.EXPERIMENTATION,
        summary="Pre-launch QA gate for experiments: a deterministic YAML rules engine (allocation, metrics, guardrails, power-based "
                "sample size, duration, overlap with live tests, SRM, instrumentation, QA links) with a CI-ready JUnit report.",
        kind=AgentKind.HYBRID,
        tier=Tier.BALANCED,
        connectors=["experiments"],
        skills=["experiment_review_rules"],
        keywords=["experiment review", "pre-launch", "qa gate", "test setup", "srm", "sample ratio mismatch", "overlapping tests",
                  "guardrails", "experiment checklist", "launch readiness"],
        produces=["review.md", "review.json", "junit.xml"],
        feeds=["experiment_summary", "experiment_planning"],
    )
    Input = ReviewInput
    Output = ReviewOutput
    schemas = {"explain": ReviewExplanation}

    def selftest_input(self) -> dict[str, Any]:
        return {"experiment_id": "exp-mobile-sticky"}

    def stages(self):
        return [("load", self.load), ("evaluate", self.evaluate), ("explain", self.explain), ("report", self.report)]

    # -- stage 1 ---------------------------------------------------------------------------------
    def load(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp = state["inputs"]
        conn = ctx.connector("experiments")
        warnings: list[str] = []
        defn: ExperimentDefinition | None = None
        if inp.get("definition"):
            d = dict(inp["definition"])
            d.setdefault("id", inp.get("experiment_id") or "experiment")
            d.setdefault("name", d["id"])
            defn = ExperimentDefinition.model_validate(d)
        elif inp.get("experiment_id"):
            defn = conn.get(inp["experiment_id"])
            if defn is None:
                plan = ctx.upstream_output("experiment_planning")
                if plan and plan.get("experiment_id") == inp["experiment_id"]:
                    defn = definition_from_plan(plan)
                else:
                    raise ValueError(f"Experiment '{inp['experiment_id']}' was not found through the experiments connector")
        else:
            plan = ctx.upstream_output("experiment_planning")
            if not plan:
                raise ValueError("Provide an experiment_id or a definition to review")
            defn = definition_from_plan(plan)
            warnings.append(f"Reviewing the upstream plan '{defn.id}'.")
        others = [e for e in conn.list() if e.id != defn.id]
        try:
            results = conn.results(defn.id)
        except Exception as exc:
            results = None
            warnings.append(f"Could not read interim results ({type(exc).__name__}); SRM check skipped.")
        return {"data": {"defn": defn.model_dump(mode="json"), "others": [o.model_dump(mode="json") for o in others],
                         "results": results.to_dict("records") if results is not None else []}, "warnings": warnings}

    # -- stage 2 ---------------------------------------------------------------------------------
    def evaluate(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        data = state["data"]
        cfg = ctx.skill("experiment_review_rules")
        defn = ExperimentDefinition.model_validate(data["defn"])
        others = [ExperimentDefinition.model_validate(o) for o in data["others"]]
        results = pd.DataFrame(data["results"]) if data["results"] else None
        env = build_env(defn, others, results, cfg)
        res = evaluate_rules(defn, env, cfg)
        checks = [Check(rule=r["rule_id"], status=r["status"], severity=r["severity"], message=r["message"], ref=r.get("ref"), target=defn.id)
                  for r in res]
        return {"data": {"results_eval": res, "env": env, "rules_version": str(cfg.get("version", ""))}, "checks": checks}

    # -- stage 3 ---------------------------------------------------------------------------------
    def explain(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        res = data["results_eval"]
        flagged = [r for r in res if r["status"] in ("fail", "warn")]
        defn = data["defn"]
        expl: dict[str, str] = {}
        fixes: dict[str, str] = {}
        summary = ""
        reason = None
        if flagged and inp.get("explain", True):
            out, reason = _generate_or_none(ctx, "explain", ReviewExplanation, experiment_id=defn["id"], experiment_name=defn["name"],
                                            hypothesis=defn.get("hypothesis") or "", gate=gate(res),
                                            finding_lines=[f"{r['rule_id']} [{r['status'].upper()}, {r['severity']}] {r['message']} (standard fix: {r['fix']})" for r in flagged])
            if out is not None:
                ids = {r["rule_id"] for r in flagged}
                for item in out.items:
                    if item.rule_id in ids:  # the model may only explain findings that exist
                        expl[item.rule_id] = item.explanation.strip()
                        fixes[item.rule_id] = item.fix.strip()
                summary = out.summary.strip()
        for r in res:
            r["explanation"] = expl.get(r["rule_id"], "")
            if fixes.get(r["rule_id"]):
                r["fix"] = fixes[r["rule_id"]]
        update: dict[str, Any] = {"data": {"results_eval": res, "explanation": summary}}
        if reason:
            update["provenance"] = {"degraded_reasons": [reason]}
        return update

    # -- stage 4 ---------------------------------------------------------------------------------
    def report(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        data = state["data"]
        defn = data["defn"]
        res = data["results_eval"]
        g = gate(res)
        fails = [r for r in res if r["status"] == "fail"]
        warns = [r for r in res if r["status"] == "warn"]
        if not data.get("explanation"):
            top = ", ".join(r["name"].lower() for r in fails[:4])
            data["explanation"] = (f"The launch gate {'fails' if g == 'fail' else 'passes'} for '{defn['name']}'"
                                   + (f": {len(fails)} blocking issue(s), starting with {top}." if fails else ".")
                                   + (f" {len(warns)} warning(s) to review." if warns else ""))
        sev_map = {"critical": "critical", "high": "high", "medium": "medium", "low": "low"}
        findings = [Finding(title=f"{r['name']}: {'blocking failure' if r['status'] == 'fail' else 'warning'}", detail=r["message"],
                            severity=sev_map[r["severity"]] if r["status"] == "fail" else "low",
                            category=_SEV_CATEGORY.get(r["rule_id"].split(".")[0], "experimentation"), evidence=r["rule_id"],
                            recommendation=r["fix"], target=defn["id"], source_agent=self.spec.id) for r in fails + warns]
        junit = junit_xml(defn["id"], res, g, {r["rule_id"]: r["explanation"] for r in res if r.get("explanation")})
        j_ref = ctx.artifacts.write_text("junit.xml", junit, "application/xml", "JUnit report (CI gate)")
        out = ReviewOutput(experiment_id=defn["id"], experiment_name=defn["name"], platform=defn.get("platform", ""), status=defn.get("status", ""),
                           gate=g, blocking_failures=len(fails), warnings=len(warns), passed=sum(1 for r in res if r["status"] == "pass"),
                           not_applicable=sum(1 for r in res if r["status"] == "info"), results=[RuleResult.model_validate(r) for r in res],
                           findings=findings, sample_size=data["env"]["sample_size"], srm=data["env"]["srm"], overlaps=data["env"]["overlaps"],
                           explanation=data["explanation"], junit_artifact=j_ref.path, rules_version=data.get("rules_version", ""))
        md = review_markdown(out)
        artifacts = [j_ref, ctx.artifacts.write_text("review.md", md, "text/markdown", "Experiment review"),
                     ctx.artifacts.write_json("review.json", out.model_dump(mode="json"), "Experiment review (structured)")]
        summary = (f"Gate {g.upper()} for {defn['id']}: {len(fails)} blocking failure(s), {len(warns)} warning(s), "
                   f"{out.passed} passed" + (f" - {', '.join(r['rule_id'] for r in fails[:5])}" if fails else ""))
        return {"output": out.model_dump(mode="json"), "artifacts": artifacts, "summary": summary}


def review_markdown(out: ReviewOutput) -> str:
    icon = {"pass": "PASS", "fail": "FAIL", "warn": "WARN", "info": "N/A"}
    L = [f"# Experiment review: {out.experiment_name} (`{out.experiment_id}`)", "", f"**Gate: {out.gate.upper()}** - {out.blocking_failures} blocking failure(s), "
         f"{out.warnings} warning(s), {out.passed} passed, {out.not_applicable} not applicable (rules v{out.rules_version}).", "", out.explanation, "",
         "| Result | Rule | Severity | Finding | Fix |", "|---|---|---|---|---|"]
    order = {"fail": 0, "warn": 1, "pass": 3, "info": 2}
    for r in sorted(out.results, key=lambda r: order[r.status]):
        fix = (r.fix if r.status in ("fail", "warn") else "")
        if r.explanation:
            fix = f"{fix} ({r.explanation})" if fix else r.explanation
        L.append(f"| {icon[r.status]} | {r.rule_id} | {r.severity} | {r.message} | {fix} |")
    if out.sample_size.get("available"):
        s = out.sample_size
        L += ["", "## Sample size", f"- Required: {s['required_per_variant']:,} per variant; delivered in the smallest arm: {s['available_smallest_arm']:,} "
              f"({s['coverage']:.1%}); needed duration at planned traffic: {s['days_needed']:,} days"]
    if out.srm.get("available"):
        L += ["", "## Sample ratio", f"- Chi-square {out.srm['chi2']:.2f}, p = {out.srm['p_value']:.2e}; observed {out.srm['observed']}, expected {out.srm['expected']}"]
        for seg in out.srm.get("segments", []):
            L.append(f"- Segment {seg['segment']}: p = {seg['p_value']:.2e}, observed {seg['observed']}")
    return "\n".join(L) + "\n"
