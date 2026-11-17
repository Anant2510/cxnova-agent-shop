"""Experiment Summary - statistics computed in code, narrative written only from those numbers.

From raw results (variant, segment, visitors, conversions, revenue - through the ExperimentConnector, a warehouse
query or inline records) the agent computes conversion rates, absolute and relative lift with confidence intervals
(Newcombe / Katz-log, statsmodels), the two-proportion z-test, Bayesian P(beat control), expected loss and credible
intervals from a seeded Beta Monte Carlo, SRM chi-square against the planned allocation, device-segment breakdowns
with a Simpson's-paradox check and revenue per visitor. Ship / iterate / abandon comes from the YAML decision rules
(config/skills/experiment_decision_rules.yaml). The model writes the stakeholder narrative strictly from the
computed facts - numbers it did not receive are rejected - and every summary is persisted to the learnings
repository (store collection "learnings"), tagged with the CRO heuristics it tested so ideation compounds.
"""

from __future__ import annotations

import io
import math
import re
from datetime import datetime, timezone
from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field, model_validator

from ...connectors.experiments import ExperimentDefinition
from ...core import AgentKind, AgentSpec, BaseAgent, Category, Check, RunContext, Tier, register
from ...core.handoff import Finding
from ...core.textutil import tfidf_similarity, truncate
from .experiment_planning import sample_size_per_variant
from .experiment_review import srm_test

# ============================================================================ statistics (pure)

REQUIRED_COLUMNS = ("variant", "visitors", "conversions")


def normalise_results(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [str(c).strip().lower() for c in df.columns]
    missing = [c for c in REQUIRED_COLUMNS if c not in df]
    if missing:
        raise ValueError(f"results need columns {list(REQUIRED_COLUMNS)} (+ optional segment, revenue); missing {missing}")
    if "segment" not in df:
        df["segment"] = "all"
    if "revenue" not in df:
        df["revenue"] = np.nan
    df["variant"] = df["variant"].astype(str)
    df["segment"] = df["segment"].fillna("all").astype(str)
    for c in ("visitors", "conversions", "revenue"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    if (df["conversions"] > df["visitors"]).any() or (df[["visitors", "conversions"]] < 0).any().any():
        raise ValueError("conversions must be between 0 and visitors in every row")
    return df


def variant_table(df: pd.DataFrame) -> pd.DataFrame:
    g = df.groupby("variant", sort=False).agg(visitors=("visitors", "sum"), conversions=("conversions", "sum"),
                                              revenue=("revenue", lambda s: s.sum(min_count=1)))
    g["conversion_rate"] = g["conversions"] / g["visitors"].clip(lower=1)
    g["revenue_per_visitor"] = g["revenue"] / g["visitors"].clip(lower=1)
    g["average_order_value"] = g["revenue"] / g["conversions"].clip(lower=1)
    return g


def frequentist_compare(conv_t: int, n_t: int, conv_c: int, n_c: int, alpha: float = 0.05) -> dict[str, Any]:
    """Treatment vs control: lifts, Newcombe CI (difference), Katz-log CI (ratio) and the pooled two-proportion z-test."""
    from statsmodels.stats.proportion import confint_proportions_2indep, proportions_ztest

    p_t, p_c = conv_t / n_t, conv_c / n_c
    z, p = proportions_ztest([conv_t, conv_c], [n_t, n_c])
    lo_d, hi_d = confint_proportions_2indep(conv_t, n_t, conv_c, n_c, compare="diff", method="newcomb", alpha=alpha)
    try:
        lo_r, hi_r = confint_proportions_2indep(conv_t, n_t, conv_c, n_c, compare="ratio", method="log", alpha=alpha)
        ci_rel = [float(lo_r) - 1, float(hi_r) - 1]
    except Exception:  # zero conversions in an arm
        ci_rel = [float("nan"), float("nan")]
    return {"rate_variant": p_t, "rate_control": p_c, "abs_lift": p_t - p_c, "rel_lift": (p_t / p_c - 1) if p_c > 0 else float("nan"),
            "ci_abs": [float(lo_d), float(hi_d)], "ci_rel": ci_rel, "z": float(z), "p_value": float(p), "alpha": alpha,
            "significant": bool(p < alpha), "ci_level": 1 - alpha}


def bayesian_compare(conv_t: int, n_t: int, conv_c: int, n_c: int, draws: int = 200_000, seed: int = 20261018,
                     prior: tuple[float, float] = (1.0, 1.0)) -> dict[str, Any]:
    """Seeded Beta Monte Carlo: P(variant beats control), expected loss of shipping, 95% credible interval of the relative lift."""
    rng = np.random.default_rng(seed)
    a0, b0 = prior
    c = rng.beta(a0 + conv_c, b0 + n_c - conv_c, size=draws)
    t = rng.beta(a0 + conv_t, b0 + n_t - conv_t, size=draws)
    rel = t / c - 1
    loss = np.maximum(c - t, 0.0)
    return {"prob_beat_control": float((t > c).mean()), "expected_loss_abs": float(loss.mean()),
            "expected_loss_rel": float(loss.mean() / c.mean()), "expected_loss_keep_control_abs": float(np.maximum(t - c, 0).mean()),
            "credible_rel": [float(np.quantile(rel, 0.025)), float(np.quantile(rel, 0.975))], "draws": draws, "seed": seed,
            "prior": f"Beta({a0:g}, {b0:g})"}


def prob_best(rows: list[tuple[str, int, int]], draws: int = 200_000, seed: int = 20261018) -> dict[str, float]:
    """P(each arm has the highest conversion rate) - for tests with several treatments."""
    rng = np.random.default_rng(seed + 1)
    sims = np.column_stack([rng.beta(1 + conv, 1 + n - conv, size=draws) for _, conv, n in rows])
    winners = sims.argmax(axis=1)
    return {name: float((winners == i).mean()) for i, (name, _, _) in enumerate(rows)}


def segment_breakdown(df: pd.DataFrame, control: str, alpha: float = 0.05) -> list[dict[str, Any]]:
    from statsmodels.stats.proportion import proportions_ztest

    out = []
    for seg, g in df.groupby("segment", sort=True):
        vt = g.groupby("variant")[["visitors", "conversions"]].sum()
        if control not in vt.index:
            continue
        nc, cc = int(vt.loc[control, "visitors"]), int(vt.loc[control, "conversions"])
        for var in vt.index:
            if var == control:
                continue
            nt, ct = int(vt.loc[var, "visitors"]), int(vt.loc[var, "conversions"])
            if min(nc, nt) == 0:
                continue
            _, p = proportions_ztest([ct, cc], [nt, nc]) if (ct + cc) > 0 else (0.0, 1.0)
            pc, pt = cc / nc, ct / nt
            out.append({"segment": str(seg), "variant": str(var), "visitors_control": nc, "visitors_variant": nt, "rate_control": pc, "rate_variant": pt,
                        "abs_lift": pt - pc, "rel_lift": (pt / pc - 1) if pc > 0 else float("nan"), "p_value": float(p), "significant": bool(p < alpha)})
    return out


def simpsons_check(df: pd.DataFrame, control: str, variant: str, overall_abs_lift: float) -> dict[str, Any]:
    """Simpson's paradox: the pooled effect has the opposite sign of the effect in every segment.

    Also reports whether the segment mix differs between arms (chi-square), the usual cause, and the
    mix-standardised lift (segment rates weighted by the pooled segment mix).
    """
    from scipy.stats import chi2_contingency

    sub = df[df["variant"].isin([control, variant])]
    segs = sub.groupby(["segment", "variant"])[["visitors", "conversions"]].sum().reset_index()
    if segs["segment"].nunique() < 2:
        return {"available": False, "reason": "fewer than two segments"}
    pivot_n = segs.pivot(index="segment", columns="variant", values="visitors").fillna(0)
    pivot_c = segs.pivot(index="segment", columns="variant", values="conversions").fillna(0)
    if control not in pivot_n or variant not in pivot_n:
        return {"available": False, "reason": "variant missing from segments"}
    rates_c = pivot_c[control] / pivot_n[control].clip(lower=1)
    rates_t = pivot_c[variant] / pivot_n[variant].clip(lower=1)
    seg_lifts = (rates_t - rates_c)
    valid = seg_lifts[(pivot_n[control] > 0) & (pivot_n[variant] > 0)]
    signs = set(np.sign(valid[valid != 0]).astype(int).tolist())
    paradox = len(signs) == 1 and overall_abs_lift != 0 and np.sign(overall_abs_lift) != next(iter(signs))
    chi2, p_mix, _, _ = chi2_contingency(pivot_n[[control, variant]].to_numpy())
    weights = (pivot_n[control] + pivot_n[variant]) / (pivot_n[control] + pivot_n[variant]).sum()
    std_c, std_t = float((weights * rates_c).sum()), float((weights * rates_t).sum())
    return {"available": True, "paradox": bool(paradox), "segment_signs": {str(k): int(np.sign(v)) for k, v in seg_lifts.items()},
            "segment_mix_chi2": round(float(chi2), 4), "segment_mix_p": float(p_mix),
            "segment_mix": {str(s): {"control": round(float(pivot_n.loc[s, control] / pivot_n[control].sum()), 4),
                                     "variant": round(float(pivot_n.loc[s, variant] / pivot_n[variant].sum()), 4)} for s in pivot_n.index},
            "mix_standardised_rel_lift": (std_t / std_c - 1) if std_c > 0 else None}


def _cmp(a: Any, op: str, b: Any) -> bool:
    if a is None or b is None:
        return False
    try:
        return {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b, "==": a == b, "!=": a != b}[op]
    except TypeError:
        return False


def decide(facts: dict[str, Any], rules: list[dict[str, Any]]) -> dict[str, Any]:
    """First decision rule whose conditions all hold (values may name another fact, e.g. value: mde)."""
    for rule in rules:
        conds = rule.get("when") or []
        ok = all(_cmp(facts.get(c["fact"]), c.get("op", "=="), facts.get(c["value"]) if isinstance(c.get("value"), str) else c.get("value"))
                 for c in conds)
        if ok:
            return {"decision": rule["decision"], "rule_id": rule["id"], "label": rule.get("label", rule["id"]), "action": rule.get("action", ""),
                    "conditions": conds}
    return {"decision": "iterate", "rule_id": "default", "label": "No rule matched", "action": "Review the result manually.", "conditions": []}


_NUM = re.compile(r"(?<![\w.])[-+−]?\d{1,3}(?:,\d{3})+(?:\.\d+)?%?|(?<![\w.])[-+−]?\d+(?:\.\d+)?%?")


def _decimals(token: str) -> int:
    t = token.rstrip("%")
    return len(t.split(".")[1]) if "." in t else 0


def unsupported_numbers(text: str, allowed_values: list[float], allowed_tokens: set[str]) -> list[str]:
    """Numbers in `text` that are not a rounding of a computed fact (as a value or a percentage) nor present in the facts text."""
    bad = []
    for m in _NUM.finditer(text or ""):
        tok = m.group(0).replace("−", "-").lstrip("+")
        clean = tok.replace(",", "")
        if clean.lstrip("-").rstrip("%") in allowed_tokens or tok in allowed_tokens:
            continue
        try:
            x = abs(float(clean.rstrip("%")))
        except ValueError:
            continue
        if x <= 10 and "." not in clean and not clean.endswith("%"):
            continue  # small counts ("two arms", "3 segments") are not statistics
        tol = 0.5 * 10 ** (-_decimals(clean)) + 1e-9
        cands = [abs(v) * 100 for v in allowed_values] if clean.endswith("%") else [abs(v) for v in allowed_values] + [abs(v) * 100 for v in allowed_values]
        if not any(abs(x - c) <= tol for c in cands):
            bad.append(tok)
    return bad


# ============================================================================ contract


class SummaryInput(BaseModel):
    experiment_id: str | None = Field(default=None, description="Experiment whose results to summarise (experiments connector)")
    results: list[dict[str, Any]] | None = Field(default=None, description="Result rows: variant, segment, visitors, conversions, revenue")
    results_csv: str | None = Field(default=None, description="Results as CSV text (alternative to results)", json_schema_extra={"ui": "textarea"})
    results_sql: str | None = Field(default=None, description="Read-only SQL returning the result columns from the warehouse")
    definition: dict[str, Any] | None = Field(default=None, description="Experiment definition (planned allocation, MDE, baseline)", json_schema_extra={"ui": "json"})
    control: str | None = Field(default=None, description="Control variant id (default: from the definition, else 'control')")
    alpha: float | None = Field(default=None, gt=0, lt=0.5, description="Significance level (default: decision rules)")
    persist: bool = Field(default=True, description="Save the summary to the learnings repository")

    @model_validator(mode="after")
    def _need_source(self) -> "SummaryInput":
        if not (self.experiment_id or self.results or self.results_csv or self.results_sql):
            raise ValueError("Provide an experiment_id, result rows, results_csv or results_sql")
        return self


class VariantStats(BaseModel):
    variant: str
    is_control: bool
    visitors: int
    conversions: int
    conversion_rate: float
    revenue: float | None = None
    revenue_per_visitor: float | None = None
    average_order_value: float | None = None
    planned_allocation: float | None = None
    observed_share: float


class Comparison(BaseModel):
    variant: str
    control: str
    rate_variant: float
    rate_control: float
    abs_lift: float
    rel_lift: float
    ci_abs: list[float]
    ci_rel: list[float]
    ci_level: float
    z: float
    p_value: float
    alpha: float
    significant: bool
    prob_beat_control: float
    expected_loss_abs: float
    expected_loss_rel: float
    credible_rel: list[float]
    rpv_lift_rel: float | None = None


class SegmentResult(BaseModel):
    segment: str
    variant: str
    visitors_control: int
    visitors_variant: int
    rate_control: float
    rate_variant: float
    abs_lift: float
    rel_lift: float
    p_value: float
    significant: bool


class Narrative(BaseModel):
    headline: str = Field(description="One sentence with the result and the decision")
    what_happened: str = Field(description="The measured outcome, using only the numbers provided")
    what_it_means: str = Field(description="Business interpretation of the result")
    next_steps: list[str] = Field(default_factory=list, description="Two to four concrete actions")
    caveats: list[str] = Field(default_factory=list, description="Data-quality and interpretation caveats from the facts")


class SummaryOutput(BaseModel):
    experiment_id: str
    experiment_name: str
    primary_metric: str
    control: str
    variants: list[VariantStats]
    comparisons: list[Comparison]
    best_variant: str
    prob_best: dict[str, float] = Field(default_factory=dict)
    srm: dict[str, Any]
    segments: list[SegmentResult]
    simpsons: dict[str, Any]
    decision: dict[str, Any]
    narrative: Narrative
    narrative_source: Literal["model", "deterministic"]
    findings: list[Finding]
    heuristic_ids: list[str] = Field(default_factory=list)
    learning_id: str | None = None
    method: dict[str, Any] = Field(default_factory=dict)


def _generate_or_none(ctx: RunContext, template: str, schema: type[BaseModel], **variables: Any) -> tuple[Any, str | None]:
    if ctx.llm.offline:
        ctx.render(template, **variables)
        return None, "no language model configured: narrative written by deterministic templates from the computed statistics"
    try:
        return ctx.generate(template, schema, **variables), None
    except Exception as exc:
        ctx.log("model call failed", template=template, error=repr(exc))
        return None, f"model call failed ({type(exc).__name__}); narrative written by deterministic templates"


def pct(x: float | None, d: int = 1, sign: bool = False) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "n/a"
    return f"{x:+.{d}%}" if sign else f"{x:.{d}%}"


@register
class ExperimentSummaryAgent(BaseAgent):
    spec = AgentSpec(
        id="experiment_summary",
        name="Experiment Summary",
        category=Category.EXPERIMENTATION,
        summary="Summarises experiment results with statistics computed in code - lift with confidence intervals, p-values, Bayesian "
                "probability to beat control, SRM and segment checks - and a ship/iterate/abandon decision, saved to a learnings repository.",
        kind=AgentKind.HYBRID,
        tier=Tier.BALANCED,
        connectors=["experiments", "store", "warehouse"],
        skills=["experiment_decision_rules", "cro_heuristics"],
        keywords=["experiment results", "test results", "summarise test", "lift", "statistical significance", "p-value", "winner",
                  "ship or iterate", "bayesian", "learnings"],
        produces=["experiment_summary.md", "experiment_stats.json", "results_chart.png"],
        feeds=["experiment_ideation", "chart_summary"],
    )
    Input = SummaryInput
    Output = SummaryOutput
    schemas = {"narrative": Narrative}

    def selftest_input(self) -> dict[str, Any]:
        return {"experiment_id": "exp-hero-cta"}

    def stages(self):
        return [("load", self.load), ("compute", self.compute), ("decide", self.decide_stage), ("narrate", self.narrate), ("persist", self.persist),
                ("report", self.report)]

    # -- stage 1 ---------------------------------------------------------------------------------
    def load(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp = state["inputs"]
        warnings: list[str] = []
        defn: ExperimentDefinition | None = None
        if inp.get("definition"):
            d = dict(inp["definition"])
            d.setdefault("id", inp.get("experiment_id") or "experiment")
            d.setdefault("name", d["id"])
            defn = ExperimentDefinition.model_validate(d)
        conn = None
        if inp.get("experiment_id"):
            conn = ctx.connector("experiments")
            defn = defn or conn.get(inp["experiment_id"])
            if defn is None:
                warnings.append(f"No definition for '{inp['experiment_id']}' in the registry; equal planned allocation assumed.")
        source = ""
        if inp.get("results"):
            df, source = pd.DataFrame(inp["results"]), "inline records"
        elif inp.get("results_csv"):
            df, source = pd.read_csv(io.StringIO(inp["results_csv"])), "inline CSV"
        elif inp.get("results_sql"):
            from .exploration_generation import validate_sql, warehouse_query

            wh = ctx.connector("warehouse")
            check = validate_sql(inp["results_sql"], wh.tables(), max_rows=10_000)
            if not check["valid"]:
                raise ValueError("results_sql rejected: " + "; ".join(check["errors"]))
            df, source = warehouse_query(wh, check["sql"], 10_000), "warehouse query"
        else:
            df, source = conn.results(inp["experiment_id"]), "experiments connector"
        if df is None or df.empty:
            raise ValueError(f"No results found for {inp.get('experiment_id') or 'the input'}")
        df = normalise_results(df)
        exp_id = inp.get("experiment_id") or (defn.id if defn else "experiment")
        return {"data": {"df": df.to_dict("records"), "defn": defn.model_dump(mode="json") if defn else None, "experiment_id": exp_id, "source": source},
                "warnings": warnings}

    # -- stage 2 ---------------------------------------------------------------------------------
    def compute(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        rules = ctx.skill("experiment_decision_rules")
        df = pd.DataFrame(data["df"])
        defn = ExperimentDefinition.model_validate(data["defn"]) if data["defn"] else None
        vt = variant_table(df)
        empty = [str(v) for v in vt.index[vt["visitors"] <= 0]]
        warnings = [f"Variant(s) with no visitors were left out: {', '.join(empty)}."] if empty else []
        if empty:
            df = df[~df["variant"].isin(empty)]
            vt = variant_table(df)
        if len(vt) < 2:
            raise ValueError("Results need at least two variants with visitors")
        control = inp.get("control") or next((v.id for v in (defn.variants if defn else []) if v.is_control), None) or \
            ("control" if "control" in vt.index else str(vt.index[0]))
        if control not in vt.index:
            raise ValueError(f"Control '{control}' not found in results variants {list(vt.index)}")
        alpha = float(inp.get("alpha") or rules.get("alpha", 0.05))
        treatments = [v for v in vt.index if v != control]
        alpha_cmp = alpha / max(1, len(treatments))  # Bonferroni across treatments
        draws, seed = int(rules.get("monte_carlo_draws", 200_000)), int(rules.get("seed", 20261018))
        planned = {v.id: v.allocation for v in defn.variants} if defn and defn.variants else {v: 100 / len(vt) for v in vt.index}
        total_vis = float(vt["visitors"].sum())
        variants = [VariantStats(variant=v, is_control=(v == control), visitors=int(r["visitors"]), conversions=int(r["conversions"]),
                                 conversion_rate=float(r["conversion_rate"]), revenue=None if pd.isna(r["revenue"]) else float(r["revenue"]),
                                 revenue_per_visitor=None if pd.isna(r["revenue_per_visitor"]) else float(r["revenue_per_visitor"]),
                                 average_order_value=None if pd.isna(r["average_order_value"]) else float(r["average_order_value"]),
                                 planned_allocation=planned.get(v), observed_share=float(r["visitors"]) / max(total_vis, 1)).model_dump()
                    for v, r in vt.iterrows()]
        c = vt.loc[control]
        comparisons = []
        for t in treatments:
            r = vt.loc[t]
            f = frequentist_compare(int(r["conversions"]), int(r["visitors"]), int(c["conversions"]), int(c["visitors"]), alpha_cmp)
            b = bayesian_compare(int(r["conversions"]), int(r["visitors"]), int(c["conversions"]), int(c["visitors"]), draws, seed)
            rpv = None
            if not pd.isna(r["revenue_per_visitor"]) and not pd.isna(c["revenue_per_visitor"]) and c["revenue_per_visitor"] > 0:
                rpv = float(r["revenue_per_visitor"] / c["revenue_per_visitor"] - 1)
            comparisons.append(Comparison(variant=t, control=control, rpv_lift_rel=rpv, **{k: f[k] for k in ("rate_variant", "rate_control", "abs_lift", "rel_lift",
                                          "ci_abs", "ci_rel", "ci_level", "z", "p_value", "alpha", "significant")},
                                          prob_beat_control=b["prob_beat_control"], expected_loss_abs=b["expected_loss_abs"],
                                          expected_loss_rel=b["expected_loss_rel"], credible_rel=b["credible_rel"]).model_dump())
        best = max(comparisons, key=lambda x: (x["prob_beat_control"], x["rel_lift"]))
        pbest = prob_best([(v, int(vt.loc[v, "conversions"]), int(vt.loc[v, "visitors"])) for v in vt.index], draws, seed) if len(vt) > 2 else {}
        srm = srm_test({v: float(vt.loc[v, "visitors"]) for v in vt.index}, planned)
        srm_thr = float(rules.get("srm_p_threshold", 0.001))
        srm["threshold"] = srm_thr
        srm["detected"] = bool(srm.get("available") and srm["p_value"] < srm_thr)
        segments = segment_breakdown(df, control, alpha) if df["segment"].nunique() > 1 else []
        simpson = simpsons_check(df, control, best["variant"], best["abs_lift"])
        checks = [Check(rule="summary.srm", status="fail" if srm["detected"] else "pass", severity="critical",
                        message=(f"Sample ratio mismatch: chi-square {srm['chi2']:.2f}, p = {srm['p_value']:.2e} (threshold {srm_thr:g})." if srm["detected"]
                                 else f"Allocation matches the plan (p = {srm.get('p_value', float('nan')):.3f}).")),
                  Check(rule="summary.simpsons", status="warn" if simpson.get("paradox") else ("pass" if simpson.get("available") else "info"),
                        severity="high", message=("Simpson's paradox: the pooled lift has the opposite sign of every segment's lift." if simpson.get("paradox")
                                                  else "Pooled and segment-level lifts agree in direction." if simpson.get("available") else "Single segment - no check."))]
        if simpson.get("available") and simpson.get("segment_mix_p", 1) < srm_thr:
            checks.append(Check(rule="summary.segment_mix", status="warn", severity="high",
                                message=f"Segment mix differs between arms (chi-square p = {simpson['segment_mix_p']:.2e}); compare the mix-standardised lift "
                                        f"{pct(simpson.get('mix_standardised_rel_lift'), 1, True)}."))
        return {"data": {"control": control, "variants": variants, "comparisons": comparisons, "best": best, "prob_best": pbest, "srm": srm,
                         "segments": segments, "simpsons": simpson, "alpha": alpha, "rules": rules,
                         "method": {"frequentist": "pooled two-proportion z-test (statsmodels); Newcombe CI for the difference; Katz log CI for the ratio",
                                    "alpha": alpha, "alpha_per_comparison": alpha_cmp, "bayesian": f"Beta(1,1) prior, {draws:,} Monte Carlo draws, seed {seed}",
                                    "srm": f"chi-square goodness of fit vs planned allocation, threshold p < {srm_thr:g}",
                                    "source": data["source"]}},
                "checks": checks, "warnings": warnings}

    # -- stage 3 ---------------------------------------------------------------------------------
    def decide_stage(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        data = state["data"]
        rules = data["rules"]
        defn = ExperimentDefinition.model_validate(data["defn"]) if data["defn"] else None
        best = data["best"]
        k = len(data["variants"])
        sample_reached, required = None, None
        if defn and defn.baseline_rate and defn.mde:
            try:
                required = sample_size_per_variant(defn.baseline_rate, defn.mde, data["alpha"], 0.8, k)
                sample_reached = min(v["visitors"] for v in data["variants"]) >= required
            except ValueError:
                pass
        rpv_drop = rules.get("guardrails", {}).get("rpv_max_drop_rel", 0.05)
        facts = {"srm_detected": data["srm"]["detected"], "srm_p": data["srm"].get("p_value"), "p_value": best["p_value"], "significant": best["significant"],
                 "lift_rel": best["rel_lift"], "lift_abs": best["abs_lift"], "ci_rel_low": best["ci_rel"][0], "ci_rel_high": best["ci_rel"][1],
                 "prob_beat_control": best["prob_beat_control"], "expected_loss_rel": best["expected_loss_rel"], "sample_reached": sample_reached,
                 "guardrail_breach": bool(best["rpv_lift_rel"] is not None and best["rpv_lift_rel"] < -float(rpv_drop)),
                 "rpv_lift_rel": best["rpv_lift_rel"], "simpsons_paradox": bool(data["simpsons"].get("paradox")), "alpha": best["alpha"],
                 "mde": defn.mde if defn and defn.mde else None, "required_per_variant": required}
        decision = decide(facts, rules.get("rules", []))
        decision["facts"] = facts
        check = Check(rule="summary.decision", status="info", severity="medium",
                      message=f"Decision: {decision['decision'].upper()} - {decision['label']} (rule {decision['rule_id']}).")
        return {"data": {"decision": decision, "facts": facts}, "checks": [check]}

    # -- stage 4 ---------------------------------------------------------------------------------
    def narrate(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        data = state["data"]
        defn = data["defn"] or {}
        lines, values, tokens_ = fact_lines(data, defn)
        det = deterministic_narrative(data, defn)
        variables = dict(experiment_name=defn.get("name") or data["experiment_id"], hypothesis=defn.get("hypothesis") or "",
                         primary_metric=(defn.get("primary_metric") or {}).get("name") or "Conversion rate", fact_lines=lines,
                         decision=data["decision"]["decision"], decision_label=data["decision"]["label"], decision_action=data["decision"]["action"])
        warnings: list[str] = []
        narrative, reason = _generate_or_none(ctx, "narrative", Narrative, **variables)
        source = "model"
        if narrative is not None:
            text = " ".join([narrative.headline, narrative.what_happened, narrative.what_it_means] + narrative.next_steps + narrative.caveats)
            bad = unsupported_numbers(text, values, tokens_)
            if bad:
                try:  # one constrained rewrite, then fall back to the deterministic narrative
                    narrative = ctx.generate("narrative", Narrative, **variables, rejected_numbers=bad)
                    text = " ".join([narrative.headline, narrative.what_happened, narrative.what_it_means] + narrative.next_steps + narrative.caveats)
                    bad = unsupported_numbers(text, values, tokens_)
                except Exception:
                    pass
            if bad:
                warnings.append(f"Model narrative used numbers that were not computed ({', '.join(bad[:5])}); the deterministic narrative is used instead.")
                narrative = None
        if narrative is None:
            narrative, source = det, "deterministic"
        out: dict[str, Any] = {"data": {"narrative": narrative.model_dump(), "narrative_source": source, "fact_lines": lines}, "warnings": warnings}
        if reason:
            out["provenance"] = {"degraded_reasons": [reason]}
        return out

    # -- stage 5 ---------------------------------------------------------------------------------
    def persist(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        defn = data["defn"] or {}
        heur = ctx.skill("cro_heuristics", default={}).get("heuristics", [])
        text = f"{defn.get('name', '')} {defn.get('hypothesis', '')}"
        hids = tag_heuristics(text, heur)
        best = data["best"]
        learning_id = None
        if inp.get("persist", True):
            record = {"id": data["experiment_id"], "experiment_id": data["experiment_id"], "name": defn.get("name") or data["experiment_id"],
                      "page_url": defn.get("page_url"), "hypothesis": defn.get("hypothesis"), "primary_metric": (defn.get("primary_metric") or {}).get("name"),
                      "decision": data["decision"]["decision"], "decision_rule": data["decision"]["rule_id"], "variant": best["variant"],
                      "rel_lift": best["rel_lift"], "ci_rel": best["ci_rel"], "p_value": best["p_value"], "prob_beat_control": best["prob_beat_control"],
                      "srm_detected": data["srm"]["detected"], "headline": data["narrative"]["headline"], "heuristic_ids": hids,
                      "summarized_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "run_id": ctx.run_id, "source_agent": self.spec.id}
            try:
                learning_id = ctx.connector("store").put("learnings", record, key=data["experiment_id"])
            except Exception as exc:
                return {"data": {"heuristic_ids": hids, "learning_id": None},
                        "warnings": [f"Learnings repository unavailable ({type(exc).__name__}); summary not saved."]}
        return {"data": {"heuristic_ids": hids, "learning_id": learning_id}}

    # -- stage 6 ---------------------------------------------------------------------------------
    def report(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        data = state["data"]
        defn = data["defn"] or {}
        best, dec = data["best"], data["decision"]
        findings = [Finding(title=f"Decision: {dec['decision']} - {dec['label']}", severity="medium", category="experimentation",
                            detail=f"{best['variant']} vs {data['control']}: {pct(best['rel_lift'], 1, True)} relative (CI {pct(best['ci_rel'][0], 1, True)} to "
                                   f"{pct(best['ci_rel'][1], 1, True)}), p = {best['p_value']:.4f}, P(beat) = {best['prob_beat_control']:.1%}.",
                            recommendation=dec["action"], target=data["experiment_id"], source_agent=self.spec.id)]
        if data["srm"]["detected"]:
            findings.append(Finding(title="Sample ratio mismatch", severity="critical", category="analytics",
                                    detail=f"chi-square {data['srm']['chi2']:.2f}, p = {data['srm']['p_value']:.2e}; observed {data['srm']['observed']}",
                                    recommendation="Investigate assignment before trusting the result.", target=data["experiment_id"], source_agent=self.spec.id))
        if data["simpsons"].get("paradox"):
            findings.append(Finding(title="Simpson's paradox", severity="high", category="analytics",
                                    detail="Pooled lift and segment lifts point in opposite directions.", target=data["experiment_id"], source_agent=self.spec.id,
                                    recommendation=f"Use the mix-standardised lift ({pct(data['simpsons'].get('mix_standardised_rel_lift'), 1, True)}) and fix the segment imbalance."))
        for s in data["segments"]:
            if s["significant"]:
                findings.append(Finding(title=f"Segment {s['segment']}: {pct(s['rel_lift'], 1, True)} for {s['variant']}", severity="low", category="analytics",
                                        detail=f"{pct(s['rate_variant'], 2)} vs {pct(s['rate_control'], 2)} (p = {s['p_value']:.4f}, exploratory - not adjusted for multiple segments).",
                                        target=data["experiment_id"], source_agent=self.spec.id))
        out = SummaryOutput(experiment_id=data["experiment_id"], experiment_name=defn.get("name") or data["experiment_id"],
                            primary_metric=(defn.get("primary_metric") or {}).get("name") or "Conversion rate", control=data["control"],
                            variants=data["variants"], comparisons=data["comparisons"], best_variant=best["variant"], prob_best=data["prob_best"],
                            srm=data["srm"], segments=data["segments"], simpsons=data["simpsons"],
                            decision={k: v for k, v in dec.items()}, narrative=Narrative.model_validate(data["narrative"]),
                            narrative_source=data["narrative_source"], findings=findings, heuristic_ids=data.get("heuristic_ids", []),
                            learning_id=data.get("learning_id"), method=data["method"])
        artifacts = [ctx.artifacts.write_text("experiment_summary.md", summary_markdown(out, data["fact_lines"]), "text/markdown", "Experiment summary"),
                     ctx.artifacts.write_json("experiment_stats.json", out.model_dump(mode="json"), "Computed statistics")]
        try:
            artifacts.append(ctx.artifacts.write_bytes("results_chart.png", results_chart(out), "image/png", "Conversion rate by variant and segment (95% CI)"))
        except Exception as exc:
            ctx.log("chart failed", error=repr(exc))
        summary = (f"{out.experiment_name}: {dec['decision'].upper()} - {best['variant']} {pct(best['rel_lift'], 1, True)} vs {data['control']} "
                   f"(p = {best['p_value']:.3f}, P(beat) {best['prob_beat_control']:.1%})" + ("; SRM detected" if data["srm"]["detected"] else ""))
        return {"output": out.model_dump(mode="json"), "artifacts": artifacts, "summary": summary}


def tag_heuristics(text: str, heuristics: list[dict[str, Any]], limit: int = 2, min_score: float = 0.2) -> list[str]:
    """CRO heuristics an experiment tested: keyword phrases (from the library) plus TF-IDF similarity to name/principle."""
    if not heuristics or not (text or "").strip():
        return []
    low = " " + re.sub(r"[^a-z0-9]+", " ", text.lower()) + " "
    sims = tfidf_similarity(text, [f"{h['name']} {h['principle']} {h.get('idea', {}).get('title', '')}" for h in heuristics])
    scored = []
    for h, sim in zip(heuristics, sims):
        hits = sum(1 for k in h.get("keywords", []) if f" {re.sub(r'[^a-z0-9]+', ' ', k.lower()).strip()} " in low)
        scored.append((min(hits, 3) * 0.25 + sim, h["id"]))
    return [hid for score, hid in sorted(scored, reverse=True) if score >= min_score][:limit]


def fact_lines(data: dict[str, Any], defn: dict[str, Any]) -> tuple[list[str], list[float], set[str]]:
    """Formatted facts for the prompt, plus the numeric values / literal tokens a narrative may use."""
    lines: list[str] = []
    values: list[float] = []
    for v in data["variants"]:
        lines.append(f"{v['variant']}{' (control)' if v['is_control'] else ''}: {v['visitors']:,} visitors, {v['conversions']:,} conversions, "
                     f"conversion rate {v['conversion_rate']:.2%}" + (f", revenue per visitor {v['revenue_per_visitor']:.2f}" if v.get("revenue_per_visitor") is not None else ""))
        values += [v["visitors"], v["conversions"], v["conversion_rate"], v.get("revenue_per_visitor") or 0, v.get("revenue") or 0, v["observed_share"]]
    for c in data["comparisons"]:
        lines.append(f"{c['variant']} vs {c['control']}: absolute lift {c['abs_lift'] * 100:+.2f} percentage points, relative lift {c['rel_lift']:+.1%} "
                     f"({c['ci_level']:.0%} CI {c['ci_rel'][0]:+.1%} to {c['ci_rel'][1]:+.1%}), z = {c['z']:.2f}, p = {c['p_value']:.4f}, "
                     f"{'significant' if c['significant'] else 'not significant'} at alpha {c['alpha']:g}")
        lines.append(f"Bayesian: P({c['variant']} beats {c['control']}) = {c['prob_beat_control']:.1%}, expected loss if shipped = {c['expected_loss_rel']:.3%} "
                     f"of the control rate, 95% credible interval of the lift {c['credible_rel'][0]:+.1%} to {c['credible_rel'][1]:+.1%}")
        if c.get("rpv_lift_rel") is not None:
            lines.append(f"Revenue per visitor {c['rpv_lift_rel']:+.1%} for {c['variant']} (point estimate; no per-visitor variance available)")
        values += [c["abs_lift"], c["rel_lift"], *c["ci_rel"], *c["ci_abs"], c["z"], c["p_value"], c["prob_beat_control"], c["expected_loss_rel"],
                   c["expected_loss_abs"], *c["credible_rel"], c["rpv_lift_rel"] or 0, c["alpha"], c["ci_level"]]
    s = data["srm"]
    if s.get("available"):
        lines.append(f"Sample ratio check: chi-square {s['chi2']:.2f}, p = {s['p_value']:.4f} -> {'MISMATCH' if s['detected'] else 'no mismatch'}")
        values += [s["chi2"], s["p_value"], s["threshold"]]
    for seg in data["segments"]:
        lines.append(f"Segment {seg['segment']}: {seg['variant']} {seg['rate_variant']:.2%} vs control {seg['rate_control']:.2%} "
                     f"({seg['rel_lift']:+.1%}, p = {seg['p_value']:.4f})")
        values += [seg["rate_variant"], seg["rate_control"], seg["rel_lift"], seg["p_value"], seg["visitors_control"], seg["visitors_variant"], seg["abs_lift"]]
    sp = data["simpsons"]
    if sp.get("available"):
        lines.append(f"Simpson's paradox: {'YES' if sp['paradox'] else 'no'}; segment mix difference p = {sp['segment_mix_p']:.4f}; "
                     f"mix-standardised lift {pct(sp.get('mix_standardised_rel_lift'), 1, True)}")
        values += [sp["segment_mix_p"], sp.get("mix_standardised_rel_lift") or 0]
    f = data.get("facts", {})
    if f.get("required_per_variant"):
        lines.append(f"Planned sample per variant: {f['required_per_variant']:,} (reached: {'yes' if f['sample_reached'] else 'no'})")
        values.append(f["required_per_variant"])
    if defn.get("mde"):
        lines.append(f"Planned minimum detectable effect: {defn['mde']:.0%} relative")
        values.append(defn["mde"])
    rules = data["rules"]
    b = rules.get("bayesian", {})
    values += [b.get("prob_beat_min", 0.95), b.get("expected_loss_max_rel", 0.001), 0.95, rules.get("srm_p_threshold", 0.001)]
    tokens_ = set(re.findall(r"\d+(?:\.\d+)?", " ".join(lines) + " " + str(defn.get("name", "")) + " " + str(defn.get("hypothesis", "")) + " "
                             + " ".join(f"{v.get('start_date', '')} {v.get('end_date', '')}" for v in [defn])))
    return lines, [float(v) for v in values if v is not None and not (isinstance(v, float) and math.isnan(v))], tokens_


def deterministic_narrative(data: dict[str, Any], defn: dict[str, Any]) -> Narrative:
    best, dec, ctrl = data["best"], data["decision"], data["control"]
    vt = {v["variant"]: v for v in data["variants"]}
    metric = (defn.get("primary_metric") or {}).get("name") or "conversion rate"
    direction = "beat" if best["rel_lift"] > 0 else "trailed"
    if data["srm"]["detected"]:
        headline = (f"Result not trustworthy: the traffic split does not match the plan (sample ratio mismatch, p = {data['srm']['p_value']:.2e}) - "
                    f"decision: {dec['decision']}.")
    else:
        headline = (f"{best['variant']} {direction} {ctrl} by {pct(best['rel_lift'], 1, True)} on {metric.lower()} - decision: {dec['decision']} "
                    f"({dec['label'].lower()}).")
    happened = (f"{vt[best['variant']]['visitors']:,} visitors saw {best['variant']} and {vt[ctrl]['visitors']:,} saw {ctrl}. {metric} was "
                f"{best['rate_variant']:.2%} against {best['rate_control']:.2%}: {best['abs_lift'] * 100:+.2f} percentage points, a relative lift of "
                f"{pct(best['rel_lift'], 1, True)} ({best['ci_level']:.0%} confidence interval {pct(best['ci_rel'][0], 1, True)} to {pct(best['ci_rel'][1], 1, True)}, "
                f"p = {best['p_value']:.4f}). The Bayesian analysis puts the probability that {best['variant']} is better at {best['prob_beat_control']:.1%}, "
                f"with an expected loss of {best['expected_loss_rel']:.3%} of the control rate if it is shipped.")
    if data["srm"]["detected"]:
        means = (f"The traffic split did not match the plan (sample ratio mismatch, p = {data['srm']['p_value']:.2e}), so the measured difference cannot be trusted "
                 f"until the assignment problem is found and fixed.")
    elif best["significant"] and best["rel_lift"] > 0:
        means = (f"The improvement is statistically significant and unlikely to be noise; even the low end of the interval "
                 f"({pct(best['ci_rel'][0], 1, True)}) is an improvement.")
    elif best["significant"]:
        means = "The variant performs significantly worse than the control; the change hurts the primary metric."
    else:
        means = (f"The difference is not statistically significant: the interval ({pct(best['ci_rel'][0], 1, True)} to {pct(best['ci_rel'][1], 1, True)}) "
                 f"includes no change.")
    steps = [dec["action"]]
    segs = [s for s in data["segments"] if s["variant"] == best["variant"]]
    if segs and not data["srm"]["detected"]:
        weakest = min(segs, key=lambda s: s["rel_lift"])
        strongest = max(segs, key=lambda s: s["rel_lift"])
        if weakest is not strongest:
            steps.append(f"Review the {weakest['segment']} experience, where the lift is smallest ({pct(weakest['rel_lift'], 1, True)}, p = {weakest['p_value']:.4f}) "
                         f"against {pct(strongest['rel_lift'], 1, True)} on {strongest['segment']}.")
    if data["srm"]["detected"] and data["srm"].get("observed"):
        steps.append("Compare assignment and exposure logs per device to locate where visitors are lost.")
    caveats = []
    if best.get("rpv_lift_rel") is not None:
        caveats.append(f"Revenue per visitor moved {pct(best['rpv_lift_rel'], 1, True)}; this is a point estimate because per-visitor revenue variance is not available.")
    if data["simpsons"].get("paradox"):
        caveats.append("The pooled result reverses within every segment (Simpson's paradox); use the mix-standardised lift.")
    elif data["simpsons"].get("available") and data["simpsons"]["segment_mix_p"] < 0.001:
        caveats.append(f"The device mix differs between arms (p = {data['simpsons']['segment_mix_p']:.2e}); the mix-standardised lift is "
                       f"{pct(data['simpsons'].get('mix_standardised_rel_lift'), 1, True)}.")
    if data["segments"]:
        caveats.append("Segment results are exploratory and not adjusted for multiple comparisons.")
    if len(data["comparisons"]) > 1:
        caveats.append(f"{len(data['comparisons'])} treatments were compared; significance uses a Bonferroni-adjusted alpha of {best['alpha']:g}.")
    return Narrative(headline=headline, what_happened=happened, what_it_means=means, next_steps=steps, caveats=caveats)


def summary_markdown(out: SummaryOutput, lines: list[str]) -> str:
    n = out.narrative
    L = [f"# {out.experiment_name}", "", f"**{n.headline}**", "", "## What happened", n.what_happened, "", "## What it means", n.what_it_means, "",
         "## Next steps"] + [f"- {s}" for s in n.next_steps]
    if n.caveats:
        L += ["", "## Caveats"] + [f"- {c}" for c in n.caveats]
    L += ["", "## Computed statistics", "| Variant | Visitors | Conversions | Rate | RPV |", "|---|---|---|---|---|"]
    L += [f"| {v.variant}{' (control)' if v.is_control else ''} | {v.visitors:,} | {v.conversions:,} | {v.conversion_rate:.2%} | "
          f"{'' if v.revenue_per_visitor is None else f'{v.revenue_per_visitor:.2f}'} |" for v in out.variants]
    L += [""] + [f"- {l}" for l in lines]
    L += ["", f"Decision rule `{out.decision['rule_id']}`: **{out.decision['decision'].upper()}** - {out.decision['label']}. Narrative source: {out.narrative_source}.",
          "", "Method: " + "; ".join(f"{k}: {v}" for k, v in out.method.items())]
    return "\n".join(L) + "\n"


def results_chart(out: SummaryOutput) -> bytes:
    """Conversion rate per variant (overall and per segment) with 95% Wilson intervals."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from statsmodels.stats.proportion import proportion_confint

    segs = sorted({s.segment for s in out.segments})
    groups = ["overall"] + segs
    variants = [v.variant for v in out.variants]
    colours = ["#5b6770", "#1f6f78", "#f2b33d", "#7a4e9c", "#c0392b"]
    fig, ax = plt.subplots(figsize=(8, 4.2), dpi=120)
    width = 0.8 / max(len(variants), 1)

    def rate_ci(conv: int, n: int) -> tuple[float, float, float]:
        lo, hi = proportion_confint(conv, n, alpha=0.05, method="wilson")
        return conv / n, conv / n - lo, hi - conv / n

    for i, var in enumerate(variants):
        xs, ys, err_lo, err_hi = [], [], [], []
        for gi, g in enumerate(groups):
            if g == "overall":
                v = next(x for x in out.variants if x.variant == var)
                conv, n = v.conversions, v.visitors
            else:
                row = next((s for s in out.segments if s.segment == g and (s.variant == var or var == out.control)), None)
                if row is None:
                    continue
                n = row.visitors_control if var == out.control else row.visitors_variant
                rate = row.rate_control if var == out.control else row.rate_variant
                conv = int(round(rate * n))
            r, lo, hi = rate_ci(conv, n)
            xs.append(gi + (i - (len(variants) - 1) / 2) * width)
            ys.append(r)
            err_lo.append(lo)
            err_hi.append(hi)
        ax.bar(xs, ys, width=width * 0.92, color=colours[i % len(colours)], label=var + (" (control)" if var == out.control else ""))
        ax.errorbar(xs, ys, yerr=[err_lo, err_hi], fmt="none", ecolor="#222222", capsize=3, linewidth=1)
    ax.set_xticks(range(len(groups)), groups)
    ax.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0, decimals=1))
    ax.set_ylabel(out.primary_metric)
    ax.set_title(f"{truncate(out.experiment_name, 60)} - decision: {out.decision['decision']}", fontsize=10)
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()
