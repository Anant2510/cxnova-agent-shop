/* Experiment Summary - browser twin of caf/agents/experimentation/experiment_summary.py.
   Rates, lifts, Newcombe / Katz-log intervals, the pooled z-test, a seeded Beta Monte Carlo (P(beat), expected loss,
   credible interval, P(best)), SRM, segment breakdown and the Simpson's-paradox check are computed here; ship / iterate /
   abandon comes from experiment_decision_rules.yaml. The model only narrates the computed facts - a narrative quoting a
   number that was not computed is rewritten once, then replaced by the deterministic narrative. */
(() => {
  const XP = (CAF.xp = CAF.xp || {});
  const T = () => CAF.lib.text;

  /* ============================================================ statistics (pure) */
  const signPct = (x, d = 1) => (x === null || x === undefined || Number.isNaN(x) ? "n/a" : `${x < 0 || Object.is(x, -0) ? "-" : "+"}${Math.abs(x * 100).toFixed(d)}%`);
  const pct = (x, d = 1, sign = false) => (x === null || x === undefined || Number.isNaN(x) ? "n/a" : sign ? signPct(x, d) : `${(x * 100).toFixed(d)}%`);
  const signNum = (x, d) => `${x < 0 || Object.is(x, -0) ? "-" : "+"}${Math.abs(x).toFixed(d)}`;

  function normaliseResults(rows) {
    const df = rows.map((r) => Object.fromEntries(Object.entries(r).map(([k, v]) => [String(k).trim().toLowerCase(), v])));
    const missing = ["variant", "visitors", "conversions"].filter((c) => !df.length || !(c in df[0]));
    if (missing.length) throw new Error(`results need columns ["variant", "visitors", "conversions"] (+ optional segment, revenue); missing ${JSON.stringify(missing)}`);
    const num = (v) => (v === null || v === undefined || v === "" ? NaN : Number(v));
    const out = df.map((r) => ({ variant: String(r.variant), segment: r.segment === null || r.segment === undefined || r.segment === "" ? "all" : String(r.segment),
      visitors: num(r.visitors), conversions: num(r.conversions), revenue: "revenue" in r ? num(r.revenue) : NaN }));
    if (out.some((r) => r.conversions > r.visitors || r.visitors < 0 || r.conversions < 0)) throw new Error("conversions must be between 0 and visitors in every row");
    return out;
  }
  function variantTable(df) {
    const g = new Map();
    for (const r of df) {
      const a = g.get(r.variant) || { variant: r.variant, visitors: 0, conversions: 0, revenue: NaN };
      a.visitors += Number.isNaN(r.visitors) ? 0 : r.visitors; a.conversions += Number.isNaN(r.conversions) ? 0 : r.conversions;
      if (!Number.isNaN(r.revenue)) a.revenue = (Number.isNaN(a.revenue) ? 0 : a.revenue) + r.revenue;
      g.set(r.variant, a);
    }
    for (const a of g.values()) { a.conversion_rate = a.conversions / Math.max(a.visitors, 1); a.revenue_per_visitor = a.revenue / Math.max(a.visitors, 1); a.average_order_value = a.revenue / Math.max(a.conversions, 1); }
    return g;
  }
  /** Wilson score interval (statsmodels proportion_confint method="wilson"). */
  function wilson(count, n, alpha = 0.05) {
    const q = count / n, crit = XP.nppf(1 - alpha / 2), c2 = crit * crit, denom = 1 + c2 / n;
    const center = (q + c2 / (2 * n)) / denom, dist = (crit * Math.sqrt((q * (1 - q)) / n + c2 / (4 * n * n))) / denom;
    return [center - dist, center + dist];
  }
  /** Pooled two-proportion z-test (statsmodels proportions_ztest, two-sided). */
  function zTest(c1, n1, c2, n2) {
    const p1 = c1 / n1, p2 = c2 / n2, p = (c1 + c2) / (n1 + n2), se = Math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2));
    const z = se > 0 ? (p1 - p2) / se : 0;
    return [z, 2 * XP.nsf(Math.abs(z))];
  }
  function frequentistCompare(ct, nt, cc, nc, alpha = 0.05) {
    const pt = ct / nt, pc = cc / nc;
    const [z, p] = zTest(ct, nt, cc, nc);
    const [l1, u1] = wilson(ct, nt, alpha), [l2, u2] = wilson(cc, nc, alpha);
    const diff = pt - pc;
    const ciAbs = [diff - Math.sqrt((pt - l1) ** 2 + (u2 - pc) ** 2), diff + Math.sqrt((pc - l2) ** 2 + (u1 - pt) ** 2)];
    let ciRel = [NaN, NaN];
    if (ct > 0 && cc > 0) { const v = 1 / ct - 1 / nt + 1 / cc - 1 / nc, d = XP.nppf(1 - alpha / 2) * Math.sqrt(v), lr = Math.log(pt / pc); ciRel = [Math.exp(lr - d) - 1, Math.exp(lr + d) - 1]; }
    return { rate_variant: pt, rate_control: pc, abs_lift: diff, rel_lift: pc > 0 ? pt / pc - 1 : NaN, ci_abs: ciAbs, ci_rel: ciRel, z, p_value: p, alpha, significant: p < alpha, ci_level: 1 - alpha };
  }
  const quantileSorted = (s, q) => { const pos = (s.length - 1) * q, lo = Math.floor(pos), hi = Math.ceil(pos); return s[lo] + (s[hi] - s[lo]) * (pos - lo); };
  /** Seeded Beta(1,1) Monte Carlo with the browser PRNG (same draws and seed as the YAML; values agree with numpy within MC error). */
  function bayesianCompare(ct, nt, cc, nc, draws = 200000, seed = 20261018) {
    const r = CAF.lib.stats.rng(seed), beta = CAF.lib.stats.betaSample;
    const rel = new Float64Array(draws);
    let wins = 0, loss = 0, sumC = 0, keep = 0;
    for (let i = 0; i < draws; i++) {
      const c = beta(1 + cc, 1 + nc - cc, r), t = beta(1 + ct, 1 + nt - ct, r);
      rel[i] = t / c - 1; if (t > c) wins++; loss += Math.max(c - t, 0); keep += Math.max(t - c, 0); sumC += c;
    }
    rel.sort();
    return { prob_beat_control: wins / draws, expected_loss_abs: loss / draws, expected_loss_rel: loss / draws / (sumC / draws), expected_loss_keep_control_abs: keep / draws,
      credible_rel: [quantileSorted(rel, 0.025), quantileSorted(rel, 0.975)], draws, seed, prior: "Beta(1, 1)" };
  }
  function probBest(rows, draws = 200000, seed = 20261018) {
    const r = CAF.lib.stats.rng(seed + 1), beta = CAF.lib.stats.betaSample;
    const wins = new Array(rows.length).fill(0);
    for (let i = 0; i < draws; i++) { let best = -1, bi = 0; rows.forEach(([, conv, n], k) => { const x = beta(1 + conv, 1 + n - conv, r); if (x > best) { best = x; bi = k; } }); wins[bi]++; }
    return Object.fromEntries(rows.map(([name], k) => [name, wins[k] / draws]));
  }
  function segmentBreakdown(df, control, alpha = 0.05) {
    const out = [];
    for (const seg of [...new Set(df.map((r) => r.segment))].sort()) {
      const vt = variantTable(df.filter((r) => r.segment === seg));
      if (!vt.has(control)) continue;
      const nc = Math.trunc(vt.get(control).visitors), cc = Math.trunc(vt.get(control).conversions);
      for (const v of [...vt.keys()].sort()) {
        if (v === control) continue;
        const nt = Math.trunc(vt.get(v).visitors), ct = Math.trunc(vt.get(v).conversions);
        if (Math.min(nc, nt) === 0) continue;
        const p = ct + cc > 0 ? zTest(ct, nt, cc, nc)[1] : 1;
        const pc = cc / nc, pt = ct / nt;
        out.push({ segment: seg, variant: v, visitors_control: nc, visitors_variant: nt, rate_control: pc, rate_variant: pt, abs_lift: pt - pc, rel_lift: pc > 0 ? pt / pc - 1 : NaN, p_value: p, significant: p < alpha });
      }
    }
    return out;
  }
  /** scipy.stats.chi2_contingency (Yates' correction when dof == 1). */
  function chi2Contingency(table) {
    const R = table.length, C = table[0].length, rs = table.map((r) => r.reduce((s, v) => s + v, 0)), cs = Array.from({ length: C }, (_, j) => table.reduce((s, r) => s + r[j], 0));
    const N = rs.reduce((s, v) => s + v, 0), dof = (R - 1) * (C - 1);
    let chi2 = 0;
    for (let i = 0; i < R; i++) for (let j = 0; j < C; j++) {
      const e = (rs[i] * cs[j]) / N;
      let o = table[i][j];
      if (dof === 1) { const d = e - o; o += Math.sign(d) * Math.min(0.5, Math.abs(d)); }
      chi2 += e > 0 ? (o - e) ** 2 / e : 0;
    }
    return [chi2, dof ? XP.chi2Sf(chi2, dof) : 1];
  }
  function simpsonsCheck(df, control, variant, overallAbs) {
    const sub = df.filter((r) => r.variant === control || r.variant === variant);
    const segs = [...new Set(sub.map((r) => r.segment))].sort();
    if (segs.length < 2) return { available: false, reason: "fewer than two segments" };
    const agg = (seg, v, f) => sub.filter((r) => r.segment === seg && r.variant === v).reduce((s, r) => s + (Number.isNaN(r[f]) ? 0 : r[f]), 0);
    if (!sub.some((r) => r.variant === control) || !sub.some((r) => r.variant === variant)) return { available: false, reason: "variant missing from segments" };
    const nC = segs.map((s) => agg(s, control, "visitors")), nT = segs.map((s) => agg(s, variant, "visitors"));
    const rC = segs.map((s, i) => agg(s, control, "conversions") / Math.max(nC[i], 1)), rT = segs.map((s, i) => agg(s, variant, "conversions") / Math.max(nT[i], 1));
    const lifts = segs.map((_, i) => rT[i] - rC[i]);
    const signs = new Set(lifts.filter((l, i) => nC[i] > 0 && nT[i] > 0 && l !== 0).map(Math.sign));
    const paradox = signs.size === 1 && overallAbs !== 0 && Math.sign(overallAbs) !== [...signs][0];
    const [chi2, pMix] = chi2Contingency(segs.map((_, i) => [nC[i], nT[i]]));
    const tot = nC.reduce((s, v, i) => s + v + nT[i], 0);
    const w = segs.map((_, i) => (nC[i] + nT[i]) / tot);
    const stdC = w.reduce((s, x, i) => s + x * rC[i], 0), stdT = w.reduce((s, x, i) => s + x * rT[i], 0);
    const sumC = nC.reduce((s, v) => s + v, 0), sumT = nT.reduce((s, v) => s + v, 0);
    return { available: true, paradox, segment_signs: Object.fromEntries(segs.map((s, i) => [s, Math.sign(lifts[i])])), segment_mix_chi2: XP.round(chi2, 4), segment_mix_p: pMix,
      segment_mix: Object.fromEntries(segs.map((s, i) => [s, { control: XP.round(nC[i] / sumC, 4), variant: XP.round(nT[i] / sumT, 4) }])),
      mix_standardised_rel_lift: stdC > 0 ? stdT / stdC - 1 : null };
  }
  const cmp = (a, op, b) => { if (a === null || a === undefined || b === null || b === undefined) return false;
    return { "<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b, "==": a == b, "!=": a != b }[op] ?? false; }; // eslint-disable-line eqeqeq
  function decide(facts, rules) {
    for (const rule of rules) {
      const conds = rule.when || [];
      if (conds.every((c) => cmp(facts[c.fact], c.op || "==", typeof c.value === "string" ? facts[c.value] : c.value)))
        return { decision: rule.decision, rule_id: rule.id, label: rule.label || rule.id, action: rule.action || "", conditions: conds };
    }
    return { decision: "iterate", rule_id: "default", label: "No rule matched", action: "Review the result manually.", conditions: [] };
  }
  const NUM = /(?<![\w.])[-+−]?\d{1,3}(?:,\d{3})+(?:\.\d+)?%?|(?<![\w.])[-+−]?\d+(?:\.\d+)?%?/g;
  /** Numbers in `text` that are not a rounding of a computed fact (as a value or a percentage) nor present in the facts text. */
  XP.unsupportedNumbers = (text, allowed, tokens) => {
    const bad = [];
    for (const m of String(text || "").matchAll(NUM)) {
      const tok = m[0].replace(/−/g, "-").replace(/^\+/, "");
      const clean = tok.replace(/,/g, "");
      if (tokens.has(clean.replace(/^-/, "").replace(/%$/, "")) || tokens.has(tok)) continue;
      const x = Math.abs(parseFloat(clean.replace(/%$/, "")));
      if (Number.isNaN(x)) continue;
      if (x <= 10 && !clean.includes(".") && !clean.endsWith("%")) continue;
      const dec = clean.replace(/%$/, "").includes(".") ? clean.replace(/%$/, "").split(".")[1].length : 0;
      const tol = 0.5 * 10 ** -dec + 1e-9;
      const cands = clean.endsWith("%") ? allowed.map((v) => Math.abs(v) * 100) : [...allowed.map((v) => Math.abs(v)), ...allowed.map((v) => Math.abs(v) * 100)];
      if (!cands.some((c) => Math.abs(x - c) <= tol)) bad.push(tok);
    }
    return bad;
  };
  function tagHeuristics(text, heuristics, limit = 2, minScore = 0.2) {
    if (!heuristics.length || !String(text || "").trim()) return [];
    const low = " " + text.toLowerCase().replace(/[^a-z0-9]+/g, " ") + " ";
    const sims = T().similarity(text, heuristics.map((h) => `${h.name} ${h.principle} ${(h.idea || {}).title || ""}`));
    const scored = heuristics.map((h, i) => [Math.min((h.keywords || []).filter((k) => low.includes(` ${k.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim()} `)).length, 3) * 0.25 + (sims[i] || 0), h.id]);
    return scored.sort((a, b) => b[0] - a[0] || (a[1] < b[1] ? 1 : -1)).filter(([s]) => s >= minScore).slice(0, limit).map(([, id]) => id);
  }
  function factLines(data, defn) {
    const L = [], values = [];
    for (const v of data.variants) {
      L.push(`${v.variant}${v.is_control ? " (control)" : ""}: ${XP.int(v.visitors)} visitors, ${XP.int(v.conversions)} conversions, conversion rate ${pct(v.conversion_rate, 2)}` + (v.revenue_per_visitor !== null ? `, revenue per visitor ${v.revenue_per_visitor.toFixed(2)}` : ""));
      values.push(v.visitors, v.conversions, v.conversion_rate, v.revenue_per_visitor || 0, v.revenue || 0, v.observed_share);
    }
    for (const c of data.comparisons) {
      L.push(`${c.variant} vs ${c.control}: absolute lift ${signNum(c.abs_lift * 100, 2)} percentage points, relative lift ${signPct(c.rel_lift)} (${pct(c.ci_level, 0)} CI ${signPct(c.ci_rel[0])} to ${signPct(c.ci_rel[1])}), z = ${c.z.toFixed(2)}, p = ${c.p_value.toFixed(4)}, ${c.significant ? "significant" : "not significant"} at alpha ${XP.g(c.alpha)}`);
      L.push(`Bayesian: P(${c.variant} beats ${c.control}) = ${pct(c.prob_beat_control)}, expected loss if shipped = ${pct(c.expected_loss_rel, 3)} of the control rate, 95% credible interval of the lift ${signPct(c.credible_rel[0])} to ${signPct(c.credible_rel[1])}`);
      if (c.rpv_lift_rel !== null) L.push(`Revenue per visitor ${signPct(c.rpv_lift_rel)} for ${c.variant} (point estimate; no per-visitor variance available)`);
      values.push(c.abs_lift, c.rel_lift, ...c.ci_rel, ...c.ci_abs, c.z, c.p_value, c.prob_beat_control, c.expected_loss_rel, c.expected_loss_abs, ...c.credible_rel, c.rpv_lift_rel || 0, c.alpha, c.ci_level);
    }
    const s = data.srm;
    if (s.available) { L.push(`Sample ratio check: chi-square ${s.chi2.toFixed(2)}, p = ${s.p_value.toFixed(4)} -> ${s.detected ? "MISMATCH" : "no mismatch"}`); values.push(s.chi2, s.p_value, s.threshold); }
    for (const seg of data.segments) {
      L.push(`Segment ${seg.segment}: ${seg.variant} ${pct(seg.rate_variant, 2)} vs control ${pct(seg.rate_control, 2)} (${signPct(seg.rel_lift)}, p = ${seg.p_value.toFixed(4)})`);
      values.push(seg.rate_variant, seg.rate_control, seg.rel_lift, seg.p_value, seg.visitors_control, seg.visitors_variant, seg.abs_lift);
    }
    const sp = data.simpsons;
    if (sp.available) { L.push(`Simpson's paradox: ${sp.paradox ? "YES" : "no"}; segment mix difference p = ${sp.segment_mix_p.toFixed(4)}; mix-standardised lift ${pct(sp.mix_standardised_rel_lift, 1, true)}`); values.push(sp.segment_mix_p, sp.mix_standardised_rel_lift || 0); }
    const f = data.facts || {};
    if (f.required_per_variant) { L.push(`Planned sample per variant: ${XP.int(f.required_per_variant)} (reached: ${f.sample_reached ? "yes" : "no"})`); values.push(f.required_per_variant); }
    if (defn.mde) { L.push(`Planned minimum detectable effect: ${pct(defn.mde, 0)} relative`); values.push(defn.mde); }
    const b = data.rules.bayesian || {};
    values.push(b.prob_beat_min ?? 0.95, b.expected_loss_max_rel ?? 0.001, 0.95, data.rules.srm_p_threshold ?? 0.001);
    const tokens = new Set((`${L.join(" ")} ${defn.name || ""} ${defn.hypothesis || ""} ${defn.start_date || ""} ${defn.end_date || ""}`).match(/\d+(?:\.\d+)?/g) || []);
    return [L, values.filter((v) => v !== null && v !== undefined && !Number.isNaN(v)).map(Number), tokens];
  }
  function deterministicNarrative(data, defn) {
    const best = data.best, dec = data.decision, ctrl = data.control;
    const vt = Object.fromEntries(data.variants.map((v) => [v.variant, v]));
    const metric = (defn.primary_metric || {}).name || "conversion rate";
    const srmBad = data.srm.detected;
    const headline = srmBad ? `Result not trustworthy: the traffic split does not match the plan (sample ratio mismatch, p = ${XP.e(data.srm.p_value)}) - decision: ${dec.decision}.`
      : `${best.variant} ${best.rel_lift > 0 ? "beat" : "trailed"} ${ctrl} by ${pct(best.rel_lift, 1, true)} on ${metric.toLowerCase()} - decision: ${dec.decision} (${dec.label.toLowerCase()}).`;
    const happened = `${XP.int(vt[best.variant].visitors)} visitors saw ${best.variant} and ${XP.int(vt[ctrl].visitors)} saw ${ctrl}. ${metric} was ${pct(best.rate_variant, 2)} against ${pct(best.rate_control, 2)}: ${signNum(best.abs_lift * 100, 2)} percentage points, a relative lift of ${pct(best.rel_lift, 1, true)} (${pct(best.ci_level, 0)} confidence interval ${pct(best.ci_rel[0], 1, true)} to ${pct(best.ci_rel[1], 1, true)}, p = ${best.p_value.toFixed(4)}). The Bayesian analysis puts the probability that ${best.variant} is better at ${pct(best.prob_beat_control)}, with an expected loss of ${pct(best.expected_loss_rel, 3)} of the control rate if it is shipped.`;
    let means;
    if (srmBad) means = `The traffic split did not match the plan (sample ratio mismatch, p = ${XP.e(data.srm.p_value)}), so the measured difference cannot be trusted until the assignment problem is found and fixed.`;
    else if (best.significant && best.rel_lift > 0) means = `The improvement is statistically significant and unlikely to be noise; even the low end of the interval (${pct(best.ci_rel[0], 1, true)}) is an improvement.`;
    else if (best.significant) means = "The variant performs significantly worse than the control; the change hurts the primary metric.";
    else means = `The difference is not statistically significant: the interval (${pct(best.ci_rel[0], 1, true)} to ${pct(best.ci_rel[1], 1, true)}) includes no change.`;
    const steps = [dec.action];
    const segs = data.segments.filter((s) => s.variant === best.variant);
    if (segs.length && !srmBad) {
      const weakest = segs.reduce((a, b) => (b.rel_lift < a.rel_lift ? b : a)), strongest = segs.reduce((a, b) => (b.rel_lift > a.rel_lift ? b : a));
      if (weakest !== strongest) steps.push(`Review the ${weakest.segment} experience, where the lift is smallest (${pct(weakest.rel_lift, 1, true)}, p = ${weakest.p_value.toFixed(4)}) against ${pct(strongest.rel_lift, 1, true)} on ${strongest.segment}.`);
    }
    if (srmBad && data.srm.observed) steps.push("Compare assignment and exposure logs per device to locate where visitors are lost.");
    const caveats = [];
    if (best.rpv_lift_rel !== null && best.rpv_lift_rel !== undefined) caveats.push(`Revenue per visitor moved ${pct(best.rpv_lift_rel, 1, true)}; this is a point estimate because per-visitor revenue variance is not available.`);
    if (data.simpsons.paradox) caveats.push("The pooled result reverses within every segment (Simpson's paradox); use the mix-standardised lift.");
    else if (data.simpsons.available && data.simpsons.segment_mix_p < 0.001) caveats.push(`The device mix differs between arms (p = ${XP.e(data.simpsons.segment_mix_p)}); the mix-standardised lift is ${pct(data.simpsons.mix_standardised_rel_lift, 1, true)}.`);
    if (data.segments.length) caveats.push("Segment results are exploratory and not adjusted for multiple comparisons.");
    if (data.comparisons.length > 1) caveats.push(`${data.comparisons.length} treatments were compared; significance uses a Bonferroni-adjusted alpha of ${XP.g(best.alpha)}.`);
    return { headline, what_happened: happened, what_it_means: means, next_steps: steps, caveats };
  }
  function summaryMarkdown(out, lines) {
    const n = out.narrative;
    const L = [`# ${out.experiment_name}`, "", `**${n.headline}**`, "", "## What happened", n.what_happened, "", "## What it means", n.what_it_means, "", "## Next steps", ...n.next_steps.map((s) => `- ${s}`)];
    if (n.caveats.length) L.push("", "## Caveats", ...n.caveats.map((c) => `- ${c}`));
    L.push("", "## Computed statistics", "| Variant | Visitors | Conversions | Rate | RPV |", "|---|---|---|---|---|",
      ...out.variants.map((v) => `| ${v.variant}${v.is_control ? " (control)" : ""} | ${XP.int(v.visitors)} | ${XP.int(v.conversions)} | ${pct(v.conversion_rate, 2)} | ${v.revenue_per_visitor === null ? "" : v.revenue_per_visitor.toFixed(2)} |`),
      "", ...lines.map((l) => `- ${l}`), "", `Decision rule \`${out.decision.rule_id}\`: **${out.decision.decision.toUpperCase()}** - ${out.decision.label}. Narrative source: ${out.narrative_source}.`,
      "", "Method: " + Object.entries(out.method).map(([k, v]) => `${k}: ${v}`).join("; "));
    return L.join("\n") + "\n";
  }
  /** Conversion rate per variant (overall and per segment) with 95% Wilson intervals - standalone SVG (the Python service renders a PNG). */
  function resultsChartSvg(out) {
    const groups = ["overall", ...[...new Set(out.segments.map((s) => s.segment))].sort()];
    const vars = out.variants.map((v) => v.variant);
    const colours = ["#5b6770", "#1f6f78", "#f2b33d", "#7a4e9c", "#c0392b"];
    const bars = [];
    vars.forEach((v, i) => groups.forEach((g, gi) => {
      let conv, n;
      if (g === "overall") { const x = out.variants.find((y) => y.variant === v); conv = x.conversions; n = x.visitors; }
      else { const row = out.segments.find((s) => s.segment === g && (s.variant === v || v === out.control)); if (!row) return; n = v === out.control ? row.visitors_control : row.visitors_variant; conv = Math.round((v === out.control ? row.rate_control : row.rate_variant) * n); }
      if (!n) return;
      const [lo, hi] = wilson(conv, n);
      bars.push({ i, gi, rate: conv / n, lo, hi });
    }));
    const W = 760, H = 330, pad = { l: 58, r: 16, t: 34, b: 52 };
    const ticks = CAF.lib.chart.niceTicks(0, Math.max(...bars.map((b) => b.hi), 0.01));
    const yMax = ticks[ticks.length - 1];
    const y = (v) => pad.t + (1 - v / yMax) * (H - pad.t - pad.b);
    const gw = (W - pad.l - pad.r) / groups.length, bw = (gw * 0.8) / Math.max(vars.length, 1);
    let svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="${U.esc(out.primary_metric)} by variant" style="font-family:system-ui,sans-serif"><rect width="${W}" height="${H}" fill="#ffffff"/>`;
    svg += `<text x="${pad.l}" y="20" font-size="13" font-weight="600" fill="#0f1724">${U.esc(T().truncate(out.experiment_name, 60))} - decision: ${U.esc(out.decision.decision)}</text>`;
    for (const t of ticks) svg += `<line x1="${pad.l}" x2="${W - pad.r}" y1="${y(t)}" y2="${y(t)}" stroke="#d9dfe7"/><text x="${pad.l - 6}" y="${y(t) + 4}" font-size="10.5" text-anchor="end" fill="#5d6a7d">${(t * 100).toFixed(1)}%</text>`;
    groups.forEach((g, gi) => { svg += `<text x="${pad.l + gw * (gi + 0.5)}" y="${H - pad.b + 16}" font-size="11" text-anchor="middle" fill="#5d6a7d">${U.esc(g)}</text>`; });
    for (const b of bars) {
      const x = pad.l + gw * b.gi + gw * 0.1 + bw * b.i, cx = x + bw * 0.46;
      svg += `<rect x="${x.toFixed(1)}" y="${y(b.rate).toFixed(1)}" width="${(bw * 0.92).toFixed(1)}" height="${(y(0) - y(b.rate)).toFixed(1)}" fill="${colours[b.i % colours.length]}"><title>${(b.rate * 100).toFixed(2)}% (95% CI ${(b.lo * 100).toFixed(2)}-${(b.hi * 100).toFixed(2)}%)</title></rect>`;
      svg += `<line x1="${cx.toFixed(1)}" x2="${cx.toFixed(1)}" y1="${y(b.lo).toFixed(1)}" y2="${y(b.hi).toFixed(1)}" stroke="#222222"/><line x1="${(cx - 4).toFixed(1)}" x2="${(cx + 4).toFixed(1)}" y1="${y(b.hi).toFixed(1)}" y2="${y(b.hi).toFixed(1)}" stroke="#222222"/><line x1="${(cx - 4).toFixed(1)}" x2="${(cx + 4).toFixed(1)}" y1="${y(b.lo).toFixed(1)}" y2="${y(b.lo).toFixed(1)}" stroke="#222222"/>`;
    }
    vars.forEach((v, i) => { svg += `<rect x="${pad.l + i * 170}" y="${H - 18}" width="10" height="10" fill="${colours[i % colours.length]}"/><text x="${pad.l + 14 + i * 170}" y="${H - 9}" font-size="10.5" fill="#5d6a7d">${U.esc(v + (v === out.control ? " (control)" : ""))}</text>`; });
    svg += `<text x="12" y="${pad.t - 6}" font-size="10.5" fill="#5d6a7d">${U.esc(out.primary_metric)}</text>`;
    return svg + "</svg>";
  }

  CAF.agent("experiment_summary", {
    async run(input, ctx) {
      if (!XP.generateOrNone || !XP.pageFacts) throw new Error("The shared experimentation kit (CAF.xp, defined in experiment_ideation.js) did not load");
      if (!XP.planning || !XP.review) throw new Error("experiment_planning.js / experiment_review.js (sizing and SRM helpers) did not load");
      if (!(input.experiment_id || (input.results && input.results.length) || input.results_csv || input.results_sql)) throw new Error("Provide an experiment_id, result rows, results_csv or results_sql");
      const rules = ctx.skill("experiment_decision_rules");
      /* ---- load */
      let defn = null;
      if (input.definition && U.isObj(input.definition)) { const d = { ...input.definition }; d.id = d.id ?? (input.experiment_id || "experiment"); d.name = d.name ?? d.id; defn = XP.definition(d); }
      if (input.experiment_id && !defn) {
        defn = (await XP.registry(ctx)).find((e) => e.id === input.experiment_id) || null;
        if (!defn) ctx.warn(`No definition for '${input.experiment_id}' in the registry; equal planned allocation assumed.`);
      }
      let rows, source;
      if (input.results && input.results.length) { rows = input.results; source = "inline records"; }
      else if (input.results_csv) { rows = CAF.lib.csv.parse(input.results_csv); source = "inline CSV"; }
      else if (input.results_sql) {
        const reason = "results_sql needs the Python service's warehouse: the hosted console does not execute free SQL";
        if (!input.experiment_id || !ctx.hasData(`experiments/results/${input.experiment_id}`)) throw new Error(`${reason} - pass results, results_csv or experiment_id instead`);
        ctx.warn(`${reason}; the embedded results for '${input.experiment_id}' were used instead.`);
        ctx.degraded(reason);
        rows = await ctx.data(`experiments/results/${input.experiment_id}`); source = "experiments connector";
      } else {
        const name = `experiments/results/${input.experiment_id}`;
        rows = ctx.hasData(name) ? await ctx.data(name) : [];
        source = "experiments connector";
      }
      if (!rows || !rows.length) throw new Error(`No results found for ${input.experiment_id || "the input"}`);
      let df = normaliseResults(rows);
      const expId = input.experiment_id || (defn ? defn.id : "experiment");
      /* ---- compute */
      let vt = variantTable(df);
      const empty = [...vt.values()].filter((v) => v.visitors <= 0).map((v) => v.variant);
      if (empty.length) { ctx.warn(`Variant(s) with no visitors were left out: ${empty.join(", ")}.`); df = df.filter((r) => !empty.includes(r.variant)); vt = variantTable(df); }
      if (vt.size < 2) throw new Error("Results need at least two variants with visitors");
      const names = [...vt.keys()];
      const control = input.control || ((defn && defn.variants) || []).filter((v) => v.is_control).map((v) => v.id)[0] || (vt.has("control") ? "control" : names[0]);
      if (!vt.has(control)) throw new Error(`Control '${control}' not found in results variants ${JSON.stringify(names)}`);
      const alpha = +(input.alpha || rules.alpha || 0.05);
      const treatments = names.filter((v) => v !== control);
      const alphaCmp = alpha / Math.max(1, treatments.length);
      const draws = +(rules.monte_carlo_draws ?? 200000), seed = +(rules.seed ?? 20261018);
      const planned = defn && defn.variants && defn.variants.length ? Object.fromEntries(defn.variants.map((v) => [v.id, +v.allocation])) : Object.fromEntries(names.map((v) => [v, 100 / vt.size]));
      const totalVis = names.reduce((s, v) => s + vt.get(v).visitors, 0);
      const nn = (x) => (Number.isNaN(x) ? null : x);
      const variants = names.map((v) => { const r = vt.get(v); return { variant: v, is_control: v === control, visitors: Math.trunc(r.visitors), conversions: Math.trunc(r.conversions), conversion_rate: r.conversion_rate,
        revenue: nn(r.revenue), revenue_per_visitor: nn(r.revenue_per_visitor), average_order_value: nn(r.average_order_value), planned_allocation: planned[v] ?? null, observed_share: r.visitors / Math.max(totalVis, 1) }; });
      ctx.progress("Computing statistics");
      const c = vt.get(control);
      const comparisons = treatments.map((t) => {
        const r = vt.get(t);
        const f = frequentistCompare(Math.trunc(r.conversions), Math.trunc(r.visitors), Math.trunc(c.conversions), Math.trunc(c.visitors), alphaCmp);
        const b = bayesianCompare(Math.trunc(r.conversions), Math.trunc(r.visitors), Math.trunc(c.conversions), Math.trunc(c.visitors), draws, seed);
        const rpv = !Number.isNaN(r.revenue_per_visitor) && !Number.isNaN(c.revenue_per_visitor) && c.revenue_per_visitor > 0 ? r.revenue_per_visitor / c.revenue_per_visitor - 1 : null;
        return { variant: t, control, rate_variant: f.rate_variant, rate_control: f.rate_control, abs_lift: f.abs_lift, rel_lift: f.rel_lift, ci_abs: f.ci_abs, ci_rel: f.ci_rel, ci_level: f.ci_level,
          z: f.z, p_value: f.p_value, alpha: f.alpha, significant: f.significant, prob_beat_control: b.prob_beat_control, expected_loss_abs: b.expected_loss_abs, expected_loss_rel: b.expected_loss_rel,
          credible_rel: b.credible_rel, rpv_lift_rel: rpv };
      });
      ctx.degraded("Bayesian Monte Carlo uses the browser PRNG: P(beat), expected loss and credible intervals agree with the Python service within Monte Carlo error, not digit for digit");
      const best = comparisons.reduce((a, b) => (b.prob_beat_control > a.prob_beat_control || (b.prob_beat_control === a.prob_beat_control && b.rel_lift > a.rel_lift) ? b : a));
      const pbest = vt.size > 2 ? probBest(names.map((v) => [v, Math.trunc(vt.get(v).conversions), Math.trunc(vt.get(v).visitors)]), draws, seed) : {};
      const srm = XP.review.srmTest(Object.fromEntries(names.map((v) => [v, vt.get(v).visitors])), planned);
      const srmThr = +(rules.srm_p_threshold ?? 0.001);
      srm.threshold = srmThr;
      srm.detected = !!(srm.available && srm.p_value < srmThr);
      const segments = new Set(df.map((r) => r.segment)).size > 1 ? segmentBreakdown(df, control, alpha) : [];
      const simpson = simpsonsCheck(df, control, best.variant, best.abs_lift);
      ctx.check({ rule: "summary.srm", status: srm.detected ? "fail" : "pass", severity: "critical",
        message: srm.detected ? `Sample ratio mismatch: chi-square ${srm.chi2.toFixed(2)}, p = ${XP.e(srm.p_value)} (threshold ${XP.g(srmThr)}).` : `Allocation matches the plan (p = ${srm.available ? srm.p_value.toFixed(3) : "nan"}).` });
      ctx.check({ rule: "summary.simpsons", status: simpson.paradox ? "warn" : simpson.available ? "pass" : "info", severity: "high",
        message: simpson.paradox ? "Simpson's paradox: the pooled lift has the opposite sign of every segment's lift." : simpson.available ? "Pooled and segment-level lifts agree in direction." : "Single segment - no check." });
      if (simpson.available && (simpson.segment_mix_p ?? 1) < srmThr)
        ctx.check({ rule: "summary.segment_mix", status: "warn", severity: "high", message: `Segment mix differs between arms (chi-square p = ${XP.e(simpson.segment_mix_p)}); compare the mix-standardised lift ${pct(simpson.mix_standardised_rel_lift, 1, true)}.` });
      const method = { frequentist: "pooled two-proportion z-test (statsmodels); Newcombe CI for the difference; Katz log CI for the ratio", alpha, alpha_per_comparison: alphaCmp,
        bayesian: `Beta(1,1) prior, ${XP.int(draws)} Monte Carlo draws, seed ${seed} (browser PRNG)`, srm: `chi-square goodness of fit vs planned allocation, threshold p < ${XP.g(srmThr)}`, source };
      /* ---- decide (YAML rules over computed facts) */
      let sampleReached = null, required = null;
      if (defn && defn.baseline_rate && defn.mde) { try { required = XP.planning.sampleSize(+defn.baseline_rate, +defn.mde, alpha, 0.8, variants.length); sampleReached = Math.min(...variants.map((v) => v.visitors)) >= required; } catch { /* unsizeable */ } }
      const rpvDrop = +((rules.guardrails || {}).rpv_max_drop_rel ?? 0.05);
      const facts = { srm_detected: srm.detected, srm_p: srm.p_value ?? null, p_value: best.p_value, significant: best.significant, lift_rel: best.rel_lift, lift_abs: best.abs_lift,
        ci_rel_low: best.ci_rel[0], ci_rel_high: best.ci_rel[1], prob_beat_control: best.prob_beat_control, expected_loss_rel: best.expected_loss_rel, sample_reached: sampleReached,
        guardrail_breach: best.rpv_lift_rel !== null && best.rpv_lift_rel < -rpvDrop, rpv_lift_rel: best.rpv_lift_rel, simpsons_paradox: !!simpson.paradox, alpha: best.alpha,
        mde: defn && defn.mde ? defn.mde : null, required_per_variant: required };
      const decision = decide(facts, rules.rules || []);
      decision.facts = facts;
      ctx.check({ rule: "summary.decision", status: "info", severity: "medium", message: `Decision: ${decision.decision.toUpperCase()} - ${decision.label} (rule ${decision.rule_id}).` });
      /* ---- narrate (numbers guarded) */
      const data = { variants, comparisons, srm, segments, simpsons: simpson, facts, rules, best, decision, control };
      const d = defn || {};
      const [lines, values, tokens] = factLines(data, d);
      const vars = { experiment_name: d.name || expId, hypothesis: d.hypothesis || "", primary_metric: (d.primary_metric || {}).name || "Conversion rate", fact_lines: lines,
        decision: decision.decision, decision_label: decision.label, decision_action: decision.action };
      let narrative = await XP.generateOrNone(ctx, "narrative", vars, "no language model configured: narrative written by deterministic templates from the computed statistics");
      let narrativeSource = "model";
      const textOf = (n) => [n.headline, n.what_happened, n.what_it_means, ...(n.next_steps || []), ...(n.caveats || [])].join(" ");
      if (narrative) {
        let bad = XP.unsupportedNumbers(textOf(narrative), values, tokens);
        if (bad.length) {
          try { narrative = await ctx.generate("narrative", { ...vars, rejected_numbers: bad }); bad = XP.unsupportedNumbers(textOf(narrative), values, tokens); } catch { /* keep the rejection */ }
        }
        if (bad.length) { ctx.warn(`Model narrative used numbers that were not computed (${bad.slice(0, 5).join(", ")}); the deterministic narrative is used instead.`); narrative = null; }
      }
      if (!narrative) { narrative = deterministicNarrative(data, d); narrativeSource = "deterministic"; }
      narrative = { headline: narrative.headline, what_happened: narrative.what_happened, what_it_means: narrative.what_it_means, next_steps: narrative.next_steps || [], caveats: narrative.caveats || [] };
      /* ---- persist (learnings, page session) */
      const hids = tagHeuristics(`${d.name || ""} ${d.hypothesis || ""}`, ctx.skill("cro_heuristics", {}).heuristics || []);
      let learningId = null;
      if (input.persist !== false) {
        XP.store().learnings.set(expId, { id: expId, experiment_id: expId, name: d.name || expId, page_url: d.page_url ?? null, hypothesis: d.hypothesis ?? null, primary_metric: (d.primary_metric || {}).name ?? null,
          decision: decision.decision, decision_rule: decision.rule_id, variant: best.variant, rel_lift: best.rel_lift, ci_rel: best.ci_rel, p_value: best.p_value, prob_beat_control: best.prob_beat_control,
          srm_detected: srm.detected, headline: narrative.headline, heuristic_ids: hids, summarized_at: U.now(), run_id: ctx.runId, source_agent: "experiment_summary" });
        learningId = expId;
        ctx.degraded(`learning saved to the ${XP.STORE_NOTE}`);
      }
      /* ---- report */
      const findings = [{ title: `Decision: ${decision.decision} - ${decision.label}`, severity: "medium", category: "experimentation", evidence: "",
        detail: `${best.variant} vs ${control}: ${pct(best.rel_lift, 1, true)} relative (CI ${pct(best.ci_rel[0], 1, true)} to ${pct(best.ci_rel[1], 1, true)}), p = ${best.p_value.toFixed(4)}, P(beat) = ${pct(best.prob_beat_control)}.`,
        recommendation: decision.action, target: expId, source_agent: "experiment_summary" }];
      const pyObs = (o) => "{" + Object.entries(o).map(([k, v]) => `'${k}': ${v}`).join(", ") + "}";
      if (srm.detected) findings.push({ title: "Sample ratio mismatch", severity: "critical", category: "analytics", evidence: "", detail: `chi-square ${srm.chi2.toFixed(2)}, p = ${XP.e(srm.p_value)}; observed ${pyObs(srm.observed)}`,
        recommendation: "Investigate assignment before trusting the result.", target: expId, source_agent: "experiment_summary" });
      if (simpson.paradox) findings.push({ title: "Simpson's paradox", severity: "high", category: "analytics", evidence: "", detail: "Pooled lift and segment lifts point in opposite directions.", target: expId, source_agent: "experiment_summary",
        recommendation: `Use the mix-standardised lift (${pct(simpson.mix_standardised_rel_lift, 1, true)}) and fix the segment imbalance.` });
      for (const s of segments) if (s.significant) findings.push({ title: `Segment ${s.segment}: ${pct(s.rel_lift, 1, true)} for ${s.variant}`, severity: "low", category: "analytics", evidence: "", recommendation: "",
        detail: `${pct(s.rate_variant, 2)} vs ${pct(s.rate_control, 2)} (p = ${s.p_value.toFixed(4)}, exploratory - not adjusted for multiple segments).`, target: expId, source_agent: "experiment_summary" });
      const output = { experiment_id: expId, experiment_name: d.name || expId, primary_metric: (d.primary_metric || {}).name || "Conversion rate", control, variants, comparisons, best_variant: best.variant,
        prob_best: pbest, srm, segments, simpsons: simpson, decision, narrative, narrative_source: narrativeSource, findings, heuristic_ids: hids, learning_id: learningId, method };
      const md = summaryMarkdown(output, lines);
      const svg = resultsChartSvg(output);
      ctx.artifact({ name: "experiment_summary.md", mime: "text/markdown", content: md, description: "Experiment summary" });
      ctx.artifact({ name: "experiment_stats.json", mime: "application/json", content: JSON.stringify(output, null, 2), description: "Computed statistics" });
      ctx.artifact({ name: "results_chart.svg", mime: "image/svg+xml", content: svg, description: "Conversion rate by variant and segment (95% Wilson CI)" });
      return {
        output,
        summary: `${output.experiment_name}: ${decision.decision.toUpperCase()} - ${best.variant} ${pct(best.rel_lift, 1, true)} vs ${control} (p = ${best.p_value.toFixed(3)}, P(beat) ${pct(best.prob_beat_control)})` + (srm.detected ? "; SRM detected" : ""),
        view: [{ type: "kv", title: `Decision: ${decision.decision.toUpperCase()}`, data: { rule: `${decision.rule_id} - ${decision.label}`, action: decision.action, narrative: narrativeSource } },
          { type: "table", title: "Variants (computed in code)", rows: variants.map((v) => ({ variant: v.variant + (v.is_control ? " (control)" : ""), visitors: v.visitors, conversions: v.conversions, rate: pct(v.conversion_rate, 2), RPV: v.revenue_per_visitor === null ? "" : v.revenue_per_visitor.toFixed(2), "observed share": pct(v.observed_share, 2) })) },
          { type: "table", title: "Comparisons", rows: comparisons.map((x) => ({ variant: x.variant, "rel lift": pct(x.rel_lift, 1, true), "CI": `${pct(x.ci_rel[0], 1, true)} to ${pct(x.ci_rel[1], 1, true)}`, p: x.p_value.toFixed(4), "P(beat)": pct(x.prob_beat_control), "exp. loss": pct(x.expected_loss_rel, 3) })) },
          { type: "svg", title: "Conversion rate by variant and segment", svg },
          { type: "markdown", title: "Summary", markdown: md }],
      };
    },
    selftest() { return { experiment_id: "exp-hero-cta" }; },
  });
  XP.summary = { frequentistCompare, bayesianCompare, simpsonsCheck, decide, segmentBreakdown, wilson };
})();
