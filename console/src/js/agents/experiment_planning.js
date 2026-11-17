/* Experiment Planning - browser twin of caf/agents/experimentation/experiment_planning.py.
   Sizing (Cohen's h, NormalIndPower, Bonferroni), duration in whole weeks, sensitivity, an O'Brien-Fleming design by the exact
   Armitage-McPherson-Rowe recursion and a simulated Bayesian design are all computed here; the model only drafts the
   hypothesis, variants, targeting, metrics and risks, which code reconciles. Exports for six platforms; the plan is registered
   as an ExperimentDefinition in the page-session registry. */
(() => {
  const XP = (CAF.xp = CAF.xp || {});
  const T = () => CAF.lib.text;
  const DEMO_URL = "https://www.meridian-mutual.example/products/home-insurance.html";

  /* ============================================================ frequentist sizing (pure) */
  const bonferroniAlpha = (alpha, k) => alpha / Math.max(1, Math.floor(k) - 1);
  /** Visitors per variant, solved like statsmodels NormalIndPower.solve_power: the two-sided power equation with BOTH tails.
      CAF.lib.stats.sampleSizePerVariant uses the one-tail closed form, which is ~2.4e-6 relative too high - enough to move the
      ceiling by a few visitors at large n - so it only seeds the bracket here. */
  function sampleSize(baseline, mde, alpha, power, k) {
    if (!(baseline > 0 && baseline < 1)) throw new Error("baseline must be a rate between 0 and 1");
    const target = baseline * (1 + mde);
    if (!(target > 0 && target < 1) || mde === 0) throw new Error("baseline x (1 + mde) must stay between 0 and 1 and mde must not be 0");
    const h = Math.abs(CAF.lib.stats.cohensH(baseline, target));
    const crit = XP.nppf(1 - bonferroniAlpha(alpha, k) / 2);
    const closed = CAF.lib.stats.sampleSizePerVariant(baseline, mde, alpha, power, Math.max(1, k - 1));
    const f = (nn) => XP.nsf(crit - h * Math.sqrt(nn / 2)) + XP.ncdf(-crit - h * Math.sqrt(nn / 2)) - power;
    try { return Math.ceil(XP.brent(f, Math.max(closed * 0.25, 1e-9), closed + 1, 1e-9)); } catch { return closed; }
  }
  /** Inverse of the sizing: solve NormalIndPower's two-sided power equation for h (both tails), then map h to a relative lift. */
  function achievableMde(baseline, n, alpha, power, k) {
    if (n === null || n === undefined || n < 2 || !(baseline > 0 && baseline < 1)) return null;
    const crit = XP.nppf(1 - bonferroniAlpha(alpha, k) / 2);
    const pow = (h) => XP.nsf(crit - h * Math.sqrt(n / 2)) + XP.ncdf(-crit - h * Math.sqrt(n / 2)) - power;
    const h = XP.brent(pow, 1e-10, 50);
    const phi = Math.asin(Math.sqrt(baseline)) + h / 2;
    if (phi >= Math.PI / 2) return null;
    return XP.round(Math.sin(phi) ** 2 / baseline - 1, 5);
  }
  function durationForSample(n, daily, allocPct = 100, k = 2, minDays = 7) {
    const perDay = (daily * allocPct) / 100 / k;
    if (perDay <= 0) return { per_variant_daily: 0, days_exact: Infinity, days: null, weeks: null };
    const exact = n / perDay;
    const weeks = Math.max(Math.ceil(Math.max(minDays, 1) / 7), Math.ceil(Math.ceil(exact - 1e-9) / 7));
    return { per_variant_daily: XP.round(perDay, 2), days_exact: XP.round(exact, 2), days: weeks * 7, weeks };
  }
  function sensitivityTable(baseline, daily, { alpha, power, k, alloc, maxDays }) {
    const rows = [];
    for (const m of [0.02, 0.03, 0.05, 0.075, 0.1, 0.15, 0.2, 0.3]) {
      let n;
      try { n = sampleSize(baseline, m, alpha, power, k); } catch { continue; }
      const d = durationForSample(n, daily, alloc, k);
      rows.push({ mde: m, sample_size_per_variant: n, duration_days: d.days, duration_weeks: d.weeks, feasible: d.days !== null && d.days <= maxDays });
    }
    return rows;
  }

  /* ============================================================ group-sequential design (pure) */
  function simpsonGrid(a, m) {
    const x = new Float64Array(m), w = new Float64Array(m), h = (2 * a) / (m - 1);
    for (let i = 0; i < m; i++) { x[i] = -a + i * h; w[i] = i === 0 || i === m - 1 ? 1 : i % 2 === 1 ? 4 : 2; }
    for (let i = 0; i < m; i++) w[i] *= h / 3;
    return [x, w];
  }
  /** P(first crossing |Z_k| >= b_k at look k) - Armitage-McPherson-Rowe recursion with Simpson quadrature (grid 201). */
  function gsCrossing(b, t, drift = 0, grid = 201) {
    const out = [];
    let sd = Math.sqrt(t[0]), mu = drift * t[0], a = b[0] * sd;
    out.push(XP.ncdf((-a - mu) / sd) + XP.nsf((a - mu) / sd));
    if (t.length === 1) return out;
    let [x, w] = simpsonGrid(a, grid);
    let f = new Float64Array(grid);
    for (let i = 0; i < grid; i++) f[i] = XP.npdf((x[i] - mu) / sd) / sd;
    for (let j = 1; j < t.length; j++) {
      const s = Math.sqrt(t[j] - t[j - 1]);
      mu = drift * (t[j] - t[j - 1]);
      a = b[j] * Math.sqrt(t[j]);
      let tot = 0;
      for (let i = 0; i < grid; i++) tot += w[i] * f[i] * (XP.ncdf((-a - x[i] - mu) / s) + XP.nsf((a - x[i] - mu) / s));
      out.push(tot);
      if (j < t.length - 1) {
        const [nx, nw] = simpsonGrid(a, grid);
        const g = new Float64Array(grid);
        for (let i = 0; i < grid; i++) g[i] = w[i] * f[i];
        const nf = new Float64Array(grid);
        for (let r = 0; r < grid; r++) { let acc = 0; for (let c = 0; c < grid; c++) acc += XP.npdf((nx[r] - x[c] - mu) / s) * g[c]; nf[r] = acc / s; }
        x = nx; w = nw; f = nf;
      }
    }
    return out;
  }
  const sum = (a) => a.reduce((s, v) => s + v, 0);
  function obrienFleming(k, alpha, info) {
    let t = info ? [...info] : Array.from({ length: Math.floor(k || 1) }, (_, i) => (i + 1) / k);
    const last = t[t.length - 1];
    t = t.map((v) => v / last);
    const c = XP.brent((cc) => sum(gsCrossing(t.map((v) => cc / Math.sqrt(v)), t)) - alpha, 0.5, 12, 1e-8);
    const b = t.map((v) => c / Math.sqrt(v));
    let cum = 0;
    const spent = gsCrossing(b, t).map((p) => (cum += p));
    return { constant: XP.round(c, 5), info: t.map((v) => XP.round(v, 5)), z: b.map((v) => XP.round(v, 4)), nominal_alpha: b.map((v) => XP.round(2 * XP.nsf(v), 6)), cumulative_alpha: spent.map((v) => XP.round(v, 6)) };
  }
  const driftForPower = (b, info, power) => XP.brent((d) => sum(gsCrossing(b, info, d)) - power, 0.05, 15, 1e-9);
  const maxSampleInflation = (b, info, alpha, power) => (driftForPower(b, info, power) / driftForPower([XP.nppf(1 - alpha / 2)], [1], power)) ** 2;
  function sequentialDesign(nFixed, perDay, { looks, alpha, power, k = 2, maxDays = 365 }) {
    const a = bonferroniAlpha(alpha, k);
    const K = Math.max(1, Math.floor(looks));
    const eq = obrienFleming(K, a);
    let r = maxSampleInflation(eq.z, eq.info, a, power);
    if (!(perDay > 0)) return { available: false, reason: "no traffic" };
    let weeks = Math.max(1, Math.ceil(nFixed * r / Math.max(perDay, 1e-9) / 7));
    let des, lookWeeks, nMax;
    for (let it = 0; it < 3; it++) {
      const kEff = Math.min(K, weeks);
      lookWeeks = [...new Set(Array.from({ length: kEff }, (_, i) => Math.max(1, XP.roundHalfEven(weeks * (i + 1) / kEff))))].sort((x, y) => x - y);
      des = obrienFleming(null, a, lookWeeks.map((w) => w / weeks));
      r = maxSampleInflation(des.z, des.info, a, power);
      nMax = Math.ceil(nFixed * r);
      const need = Math.max(1, Math.ceil(nMax / perDay / 7));
      if (need <= weeks) break;
      weeks = need;
    }
    const drift = driftForPower(des.z, des.info, power);
    const stop = gsCrossing(des.z, des.info, drift);
    const expected = sum(stop.map((p, i) => p * des.info[i])) + (1 - sum(stop)) * 1;
    const schedule = lookWeeks.map((w, i) => ({ look: i + 1, week: w, day: w * 7, information_fraction: XP.round(des.info[i], 4), cumulative_per_variant: Math.ceil(nMax * des.info[i]),
      z_boundary: des.z[i], nominal_alpha: des.nominal_alpha[i], cumulative_alpha_spent: des.cumulative_alpha[i] }));
    return { available: true, boundary: "O'Brien-Fleming (two-sided)", looks: schedule.length, alpha: XP.round(a, 6), constant: des.constant, max_sample_inflation: XP.round(r, 4),
      max_sample_size_per_variant: nMax, max_duration_weeks: weeks, max_duration_days: weeks * 7, feasible: weeks * 7 <= maxDays, expected_sample_fraction_h1: XP.round(expected, 4),
      expected_sample_size_per_variant_h1: Math.ceil(nMax * expected), schedule };
  }

  /* ============================================================ Bayesian design (pure, seeded browser PRNG) */
  const betaPrior = (baseline, strength) => [Math.max(baseline * strength, 0.5), Math.max((1 - baseline) * strength, 0.5)];
  /** Binomial draws: exact (geometric waiting times) for means below 400, normal approximation (rounded) above. */
  function binomialSampler(r) {
    let spare = null;
    const gauss = () => { if (spare !== null) { const s = spare; spare = null; return s; } const u = Math.max(r(), 1e-300), v = r(); const m = Math.sqrt(-2 * Math.log(u)); spare = m * Math.sin(2 * Math.PI * v); return m * Math.cos(2 * Math.PI * v); };
    return (n, p) => {
      if (p <= 0) return 0;
      if (p >= 1) return n;
      const flip = p > 0.5, q0 = flip ? 1 - p : p;
      let k;
      if (n * q0 < 400) { // exact: count Bernoulli successes by geometric waiting times
        const lq = Math.log(1 - q0);
        let pos = 0;
        k = 0;
        for (;;) { pos += Math.floor(Math.log(r() || 1e-300) / lq) + 1; if (pos > n) break; k++; }
      } else {
        k = Math.round(n * q0 + Math.sqrt(n * q0 * (1 - q0)) * gauss());
        k = Math.min(n, Math.max(0, k));
      }
      return flip ? n - k : k;
    };
  }
  function bayesianDesign(baseline, mde, perDay, weeks, { priorStrength = 100, probBeatMin = 0.95, lossMaxRel = 0.001, sims = 4000, seed = 7 } = {}) {
    const [a0, b0] = betaPrior(baseline, priorStrength);
    const binom = binomialSampler(CAF.lib.stats.rng(seed));
    const weeklyN = Math.max(XP.roundHalfEven(perDay * 7), 1);
    weeks = Math.max(1, Math.floor(weeks));
    const run = (pt) => {
      const first = new Int32Array(sims);
      for (let s = 0; s < sims; s++) {
        let cc = 0, ct = 0, f = weeks;
        for (let w = 0; w < weeks; w++) {
          cc += binom(weeklyN, baseline); ct += binom(weeklyN, pt);
          const n = weeklyN * (w + 1);
          const ac = a0 + cc, bc = b0 + n - cc, at = a0 + ct, bt = b0 + n - ct;
          const mc = ac / (ac + bc), vc = (ac * bc) / ((ac + bc) ** 2 * (ac + bc + 1)), mt = at / (at + bt), vt = (at * bt) / ((at + bt) ** 2 * (at + bt + 1));
          const mu = mt - mc, sd = Math.sqrt(vt + vc), z = mu / sd;
          const lossRel = (sd * XP.npdf(z) - mu * XP.ncdf(-z)) / Math.max(mc, 1e-12);
          if (XP.ncdf(z) >= probBeatMin && lossRel <= lossMaxRel) { f = w; break; }
        }
        first[s] = f;
      }
      return first;
    };
    const h1 = run(Math.min(baseline * (1 + mde), 0.999)), h0 = run(baseline);
    const share = (arr, w) => { let c = 0; for (const v of arr) if (v <= w) c++; return c / arr.length; };
    const weekly = Array.from({ length: weeks }, (_, w) => ({ week: w + 1, cumulative_per_variant: weeklyN * (w + 1), p_ship_if_lift_is_mde: XP.round(share(h1, w), 4), p_ship_if_no_lift: XP.round(share(h0, w), 4) }));
    const decided = [...h1].filter((v) => v < weeks);
    return { available: true, prior: { distribution: "Beta", a: XP.round(a0, 3), b: XP.round(b0, 3), strength_visitors: priorStrength, mean: XP.round(a0 / (a0 + b0), 5) },
      decision_rule: { prob_beat_min: probBeatMin, expected_loss_max_rel: lossMaxRel, expected_loss_max_abs: XP.round(lossMaxRel * baseline, 7),
        text: `Ship when P(variant beats control) >= ${XP.pct(probBeatMin)} and the expected loss of shipping is <= ${XP.pct(lossMaxRel, 2)} of the control rate (${(lossMaxRel * baseline).toFixed(5)} absolute).` },
      weekly, max_weeks: weeks, power_at_max: weekly[weeks - 1].p_ship_if_lift_is_mde, false_ship_rate: weekly[weeks - 1].p_ship_if_no_lift,
      median_weeks_to_ship_h1: decided.length ? Math.trunc(CAF.lib.stats.median(decided) + 1) : null,
      method: `${sims} seeded simulations per scenario (seed ${seed}, browser PRNG - Monte Carlo values differ slightly from the Python service); normal approximation to Beta posteriors` };
  }

  /* ============================================================ exporters (pure) */
  const weightsBp = (variants) => { const w = variants.map((v) => XP.roundHalfEven(v.allocation * 100)); if (w.length) w[0] += 10000 - sum(w); return w; };
  const snake = (t) => T().slugify(t, 40).replace(/-/g, "_");
  const EXPORTERS = {
    optimizely: ["optimizely_experiment.json", (p) => { const w = weightsBp(p.variants); return {
      name: p.title, description: p.hypothesis, type: "a/b", status: "not_started", holdback: XP.roundHalfEven((100 - p.targeting.traffic_allocation) * 100),
      url_targeting: { edit_url: p.page_url, conditions: XP.pyJson(["and", ["or", { match_type: "simple", type: "url", value: p.page_url }]]) },
      audience_conditions: !p.targeting.devices.length ? "everyone" : XP.pyJson(["or", ...p.targeting.devices.map((d) => ({ type: "device", value: d }))]),
      variations: p.variants.map((v, i) => ({ name: v.name, weight: w[i], actions: [], description: v.description })),
      metrics: p.metrics.filter((m) => ["primary", "secondary"].includes(m.role)).map((m) => ({ event_key: m.event, aggregator: m.type === "revenue" ? "sum" : "unique", scope: "visitor",
        winning_direction: m.direction === "decrease" ? "decreasing" : "increasing", role: m.role })),
      _caf: { experiment_id: p.experiment_id, guardrails: p.metrics.filter((m) => m.role === "guardrail").map((m) => m.event), planned_duration_days: p.sizing.planned_duration_days } }; }],
    vwo: ["vwo_campaign.json", (p) => ({ type: "ab", name: p.title, primaryUrl: p.page_url, percentTraffic: p.targeting.traffic_allocation,
      urls: { editorUrl: p.page_url, includeUrls: p.targeting.pages.map((u) => ({ type: "url", value: u })), excludeUrls: [] },
      goals: p.metrics.map((m) => ({ name: m.name, type: m.type === "revenue" ? "revenue" : "customConversion", event: m.event, isPrimary: m.role === "primary", isGuardrail: m.role === "guardrail" })),
      variations: p.variants.map((v) => ({ name: v.name, percentSplit: v.allocation, isControl: v.is_control, description: v.description })),
      segments: { devices: p.targeting.devices, audiences: p.targeting.audiences }, hypothesis: p.hypothesis })],
    ab_tasty: ["abtasty_test.json", (p) => ({ name: p.title, type: "ab", url: p.page_url, description: p.hypothesis, traffic: p.targeting.traffic_allocation,
      variations: p.variants.map((v) => ({ name: v.name, traffic: v.allocation, is_original: v.is_control })),
      goals: p.metrics.map((m) => ({ label: m.name, type: "custom_event", event: m.event, primary: m.role === "primary" })),
      targeting: { url_scope: p.targeting.pages.map((u) => ({ condition: "is", value: u })), devices: p.targeting.devices } })],
    launchdarkly: ["launchdarkly_experiment.json", (p) => { const key = T().slugify(p.experiment_id, 60); return {
      flag: { key, name: p.title, kind: "multivariate", description: p.hypothesis, variations: p.variants.map((v) => ({ value: v.id, name: v.name, description: v.description })), tags: ["caf", "experiment"] },
      experiment: { key: `${key}-exp`, name: p.title, iteration: { hypothesis: p.hypothesis, canReshuffleTraffic: false,
        metrics: p.metrics.filter((m) => m.role !== "guardrail").map((m) => ({ key: T().slugify(m.event, 60), isGroup: false })),
        primarySingleMetricKey: T().slugify(p.metrics.find((m) => m.role === "primary").event, 60),
        treatments: p.variants.map((v) => ({ name: v.name, baseline: v.is_control, allocationPercent: String(v.allocation), parameters: [{ flagKey: key, variationId: v.id }] })),
        flags: { [key]: { ruleId: "fallthrough", flagConfigVersion: 1 } } } },
      guardrail_metrics: p.metrics.filter((m) => m.role === "guardrail").map((m) => T().slugify(m.event, 60)) }; }],
    adobe_target: ["adobe_target_activity.json", (p) => ({ name: p.title, state: "saved", priority: 0, locations: { selectors: [{ selectorLocalId: 0, selector: "body" }] },
      experiences: p.variants.map((v, i) => ({ experienceLocalId: i, name: v.name, visitorPercentage: v.allocation, optionLocations: [{ locationLocalId: 0, optionLocalId: i }] })),
      options: p.variants.map((v, i) => ({ optionLocalId: i, name: v.name, content: "<!-- CAF variation bundle -->" })),
      metrics: p.metrics.map((m, i) => [m, i]).filter(([m]) => m.role !== "guardrail").map(([m, i]) => ({ metricLocalId: i, name: m.name, conversion: m.type === "conversion", action: { type: "count_once" }, mboxes: [{ name: m.event }] })),
      reportingAudiences: [], activityUrl: p.page_url, _caf: { traffic_allocation: p.targeting.traffic_allocation, hypothesis: p.hypothesis } })],
    generic_yaml: ["experiment.yaml", (p) => {
      const neutral = {};
      for (const k of ["experiment_id", "title", "page_url", "design", "hypothesis", "variants", "targeting", "metrics", "sizing"]) if (k in p) neutral[k] = p[k];
      const { sensitivity, ...sz } = p.sizing;
      neutral.sizing = sz;
      return XP.yaml({ caf_experiment: neutral });
    }],
  };
  const equalAllocations = (n) => { const base = Math.floor(10000 / n) / 100; const a = Array(n).fill(base); a[0] = XP.round(100 - base * (n - 1), 2); return a; };

  /* ============================================================ hypothesis helpers */
  const splitHypothesis = (text) => { const m = String(text || "").match(/^\s*if\s+([\s\S]+?),?\s+then\s+([\s\S]+?),?\s+because\s+([\s\S]+?)\.?\s*$/i); return m ? { if: m[1].trim(), then: m[2].trim(), because: m[3].trim() } : null; };
  const IMPERATIVE = new Set(("add adjust animate bring change clarify collapse combine convert cut default display embed emphasise emphasize enlarge expand feature fix give group hide highlight " +
    "include increase insert introduce keep label launch lead let link lower make move offer personalise personalize place prefill promote put raise redesign reduce remove rename reorder " +
    "replace restructure rewrite run set shorten show simplify sort split start state surface swap test try turn update use").split(" "));
  const SUBJECT = /^(we|the|a|an|our|visitors|users|customers|members|it|this|these|each|every|all)\b/i;
  function asIfClause(idea) {
    idea = String(idea || "").trim().replace(/\.+$/, "");
    if (!idea) return idea;
    idea = idea.replace(/^\s*if\s+/i, "");
    const first = idea.split(/\s+/)[0];
    const lowered = first.length > 1 && first === first.toUpperCase() && /[A-Z]/.test(first) ? idea : first[0].toLowerCase() + idea.slice(1);
    if (SUBJECT.test(lowered)) return lowered;
    if (IMPERATIVE.has(first.toLowerCase())) return `we ${lowered}`;
    return `we make this change: ${lowered}`;
  }

  function planMarkdown(plan) {
    const s = plan.sizing, seq = plan.sequential, bay = plan.bayesian, t = plan.targeting;
    const L = [`# Experiment plan: ${plan.title}`, "", `- **Experiment id:** \`${plan.experiment_id}\``, `- **Page:** ${plan.page_url}`, `- **Design:** ${plan.design}`, `- **Idea:** ${plan.test_idea}`, "",
      "## Hypothesis", plan.hypothesis, "", "## Variants", "| Id | Name | Allocation | Description |", "|---|---|---|---|",
      ...plan.variants.map((v) => `| ${v.id} | ${v.name}${v.is_control ? " (control)" : ""} | ${XP.g(v.allocation)}% | ${v.description} |`),
      "", "## Targeting", `- Pages: ${t.pages.join(", ")}`, `- Devices: ${t.devices.join(", ") || "all"}`, `- Audiences: ${t.audiences.join(", ") || "all visitors"}`,
      `- Exclusions: ${t.exclusions.join(", ") || "none"}`, `- Traffic allocation: ${XP.g(t.traffic_allocation)}% of eligible visitors`, "", "## Metrics",
      "| Role | Metric | Event | Type | Direction | Definition |", "|---|---|---|---|---|---|",
      ...plan.metrics.map((m) => `| ${m.role} | ${m.name} | \`${m.event}\` | ${m.type} | ${m.direction} | ${m.definition || ""} |`),
      "", "## Statistical sizing (computed, not estimated by a model)", `- Method: ${s.method}`,
      `- Baseline: ${XP.pct(s.baseline_rate, 2)} (${s.baseline_source})${s.is_estimate ? " - ESTIMATE" : ""}`,
      `- Traffic: ${XP.int(s.daily_visitors)} eligible visitors/day (${s.daily_visitors_source}), ${XP.int(s.per_variant_daily)} per variant/day`,
      `- MDE: ${XP.pct(s.mde, 1)} relative (${XP.pct(s.baseline_rate, 2)} -> ${XP.pct(s.target_rate, 2)}), alpha ${XP.g(s.alpha)} two-sided` + (s.n_variants > 2 ? ` (${s.alpha_per_comparison.toFixed(4)} per comparison)` : "") + `, power ${XP.pct(s.power)}`,
      `- **Sample size: ${XP.int(s.sample_size_per_variant)} per variant (${XP.int(s.total_sample_size)} total)**`,
      `- **Duration (fixed horizon): ${s.duration_days} days = ${s.duration_weeks} whole weeks** (${s.duration_days_exact} days of traffic)`,
      `- Achievable MDE within ${s.max_duration_days} days: ${XP.pct(s.achievable_mde_at_max_duration || 0, 1)}`, `- Feasible within the limit: ${s.feasible ? "yes" : "NO"}`, "",
      "| MDE | Per variant | Weeks | Feasible |", "|---|---|---|---|", ...s.sensitivity.map((r) => `| ${XP.pct(r.mde, 1)} | ${XP.int(r.sample_size_per_variant)} | ${r.duration_weeks} | ${r.feasible ? "yes" : "no"} |`)];
    if (seq.available) {
      L.push("", "## Sequential design (O'Brien-Fleming)", `- ${seq.looks} analyses at whole weeks, constant C = ${seq.constant}, alpha ${XP.g(seq.alpha)}`,
        `- Max sample: ${XP.int(seq.max_sample_size_per_variant)} per variant (inflation x${seq.max_sample_inflation.toFixed(3)}), up to ${seq.max_duration_days} days`,
        `- Expected sample if the MDE is real: ${XP.pct(seq.expected_sample_fraction_h1)} of the maximum`, "", "| Look | Week | Per variant | Z boundary | Nominal p | Alpha spent |", "|---|---|---|---|---|---|",
        ...seq.schedule.map((r) => `| ${r.look} | ${r.week} | ${XP.int(r.cumulative_per_variant)} | ${r.z_boundary.toFixed(3)} | ${r.nominal_alpha.toFixed(5)} | ${r.cumulative_alpha_spent.toFixed(5)} |`));
    }
    if (bay.available) {
      const pr = bay.prior;
      L.push("", "## Bayesian design", `- Prior: Beta(${XP.g(pr.a)}, ${XP.g(pr.b)}) - mean ${XP.pct(pr.mean, 2)}, worth ${XP.g(pr.strength_visitors)} visitors`, `- Decision rule: ${bay.decision_rule.text}`,
        `- Probability of shipping a real MDE lift by week ${bay.max_weeks}: ${XP.pct(bay.power_at_max)}; shipping a null variant: ${XP.pct(bay.false_ship_rate, 1)}`, `- ${bay.method}`, "",
        "| Week | Per variant | P(ship) if lift = MDE | P(ship) if no lift |", "|---|---|---|---|",
        ...bay.weekly.map((w) => `| ${w.week} | ${XP.int(w.cumulative_per_variant)} | ${XP.pct(w.p_ship_if_lift_is_mde)} | ${XP.pct(w.p_ship_if_no_lift, 1)} |`));
    }
    L.push("", "## Risks", "| Risk | Likelihood | Mitigation |", "|---|---|---|", ...plan.risks.map((r) => `| ${r.risk} | ${r.likelihood} | ${r.mitigation} |`),
      "", "## Critical assumptions", ...plan.assumptions.map((a) => `- ${a}`));
    if ((plan.exports || []).length) L.push("", "## Platform exports", ...plan.exports.map((e) => `- ${e.platform}: \`${e.filename}\``));
    return L.join("\n") + "\n";
  }

  function upstreamIdea(ctx, testIdea) {
    const up = ctx.upstreamOutput("experiment_ideation") || {};
    const ideas = up.ideas || [];
    if (!ideas.length) return {};
    const sims = T().similarity(testIdea, ideas.map((i) => `${i.title || ""} ${i.change || ""}`));
    const best = sims.length ? sims.indexOf(Math.max(...sims)) : 0;
    if (sims.length && sims[best] < 0.1) return {};
    const idea = ideas[best];
    return { title: idea.title, rationale: idea.rationale, hypothesis: idea.hypothesis, primary_metric: idea.primary_metric,
      evidence: (idea.evidence || []).map((e) => `${e.id} (${e.source}): ${e.detail}`) };
  }

  CAF.agent("experiment_planning", {
    async run(input, ctx) {
      if (!XP.generateOrNone || !XP.pageFacts) throw new Error("The shared experimentation kit (CAF.xp, defined in experiment_ideation.js) did not load");
      const rules = ctx.skill("experiment_review_rules", {});
      const decision = ctx.skill("experiment_decision_rules", {});
      /* ---- inspect page */
      ctx.progress("Inspecting the page");
      const page = ctx.page({ url: input.url, html: input.html });
      let html = "";
      if (page && page.status === 200) html = page.html;
      else { ctx.warn(`Could not fetch ${input.url}${page ? ` (${page.status})` : ""}; the plan is written without page inspection.`); if (page) ctx.degraded("page could not be fetched"); }
      const facts = XP.pageFacts(html, input.url);
      if (input.screenshot !== false) ctx.degraded("page screenshot (page.png) needs the Python service (Playwright)");
      /* ---- baseline */
      let metrics = { available: false };
      if (ctx.hasData("analytics/pages")) {
        let path = "/"; try { path = new URL(input.url).pathname || "/"; } catch { /* keep "/" */ }
        metrics = XP.pageAnalytics(await ctx.data("analytics/pages"), path, {});
      } else ctx.warn("Analytics unavailable (no embedded analytics data).");
      let baseline = input.baseline_rate, bSrc = "input", daily = input.daily_visitors, dSrc = "input", estimate = false;
      if (baseline === null || baseline === undefined) {
        if (metrics.available && metrics.conversion_rate) { baseline = metrics.conversion_rate; bSrc = `analytics: page conversion rate ${metrics.period || ""}`.trim(); }
        else if (metrics.site_conversion_rate) { baseline = metrics.site_conversion_rate; bSrc = "analytics: site-wide conversion rate (page not found)"; estimate = true; }
        else { baseline = 0.03; bSrc = "assumption: 3% (no analytics) - replace with a measured baseline"; estimate = true; }
      }
      if (daily === null || daily === undefined) {
        if (metrics.available && (metrics.daily_sessions || 0) > 0) { daily = metrics.daily_sessions; dSrc = `analytics: average daily sessions ${metrics.period || ""}`.trim(); }
        else { daily = 1000; dSrc = "assumption: 1,000 visitors/day (no analytics for the page) - replace with measured traffic"; estimate = true; }
      }
      if (estimate) { ctx.warn("Sizing uses estimated inputs (see sizing.baseline_source / daily_visitors_source)."); ctx.degraded("sizing inputs estimated (no page analytics)"); }
      if ((input.traffic_allocation ?? 100) <= 1) ctx.warn("traffic_allocation is a percentage: a value of 1 means 1% of eligible traffic.");
      /* ---- size */
      ctx.progress("Sizing the test");
      const minDays = +((rules.defaults || {}).min_duration_days ?? 7);
      const alpha = input.alpha, power = input.power, k = input.n_variants, mde = input.mde, alloc = input.traffic_allocation, maxDays = input.max_duration_days;
      let n;
      try { n = sampleSize(baseline, mde, alpha, power, k); } catch (e) { throw new Error(`Cannot size the test: ${e.message}`); }
      const dur = durationForSample(n, daily, alloc, k, minDays);
      const maxFullWeeks = Math.max(7, Math.floor(maxDays / 7) * 7);
      const ach = achievableMde(baseline, dur.per_variant_daily * maxFullWeeks, alpha, power, k);
      const feasible = dur.days !== null && dur.days <= maxDays;
      const seq = sequentialDesign(n, dur.per_variant_daily, { looks: input.looks, alpha, power, k, maxDays });
      const bcfg = decision.bayesian || {};
      const horizon = Math.max(1, Math.min(dur.weeks || 1, Math.floor(maxDays / 7)));
      const bayes = bayesianDesign(baseline, mde, dur.per_variant_daily, horizon, { priorStrength: +(bcfg.prior_strength ?? 100), probBeatMin: +(bcfg.prob_beat_min ?? 0.95), lossMaxRel: +(bcfg.expected_loss_max_rel ?? 0.001) });
      ctx.degraded("Bayesian operating characteristics simulated with the browser PRNG (Monte Carlo values differ slightly from the Python service)");
      const planned = { frequentist: dur.days, sequential: seq.max_duration_days ?? null, bayesian: horizon * 7 }[input.design];
      const s = { method: "Two-sided two-proportion z-test on Cohen's h (statsmodels NormalIndPower)" + (k > 2 ? ", Bonferroni-adjusted" : ""), baseline_rate: XP.round(baseline, 6), baseline_source: bSrc,
        daily_visitors: XP.round(daily, 1), daily_visitors_source: dSrc, is_estimate: estimate, mde, target_rate: XP.round(baseline * (1 + mde), 6), alpha, alpha_per_comparison: XP.round(bonferroniAlpha(alpha, k), 6),
        power, n_variants: k, sample_size_per_variant: n, total_sample_size: n * k, per_variant_daily: dur.per_variant_daily, duration_days_exact: dur.days_exact, duration_days: dur.days,
        duration_weeks: dur.weeks, planned_duration_days: planned, max_duration_days: maxDays, achievable_mde_at_max_duration: ach, feasible,
        sensitivity: sensitivityTable(baseline, daily, { alpha, power, k, alloc, maxDays }) };
      ctx.check({ rule: "sizing.feasible", status: feasible ? "pass" : "fail", severity: "high", data: { sample_size_per_variant: n, duration_days: dur.days },
        message: `${XP.int(n)} visitors per variant need ${dur.days} days (${dur.weeks} weeks) at ${XP.int(dur.per_variant_daily)} per variant per day` +
          (feasible ? "" : ` - longer than the ${maxDays}-day limit; the achievable MDE in ${maxFullWeeks} days is ${XP.pct(ach || 0, 1)}.`) });
      if (input.design === "sequential" && seq.available)
        ctx.check({ rule: "sizing.sequential", status: seq.feasible ? "pass" : "warn", severity: "medium",
          message: `O'Brien-Fleming with ${seq.looks} looks: max ${XP.int(seq.max_sample_size_per_variant)} per variant (x${seq.max_sample_inflation.toFixed(3)}), expected ${XP.pct(seq.expected_sample_fraction_h1)} of it if the MDE is real.` });
      if (input.design === "bayesian")
        ctx.check({ rule: "sizing.bayesian", status: bayes.false_ship_rate <= 0.1 ? "pass" : "warn", severity: "medium",
          message: `Bayesian rule ships a real ${XP.pct(mde)} lift with probability ${XP.pct(bayes.power_at_max)} by week ${bayes.max_weeks} and a null variant with probability ${XP.pct(bayes.false_ship_rate, 1)}.` });
      if (estimate) ctx.check({ rule: "sizing.inputs_measured", status: "warn", severity: "medium", message: "Baseline or traffic is an estimate, not a measurement." });
      /* ---- draft (model phrases; numbers above are fixed) */
      const ideaCtx = upstreamIdea(ctx, input.test_idea);
      const vars = { test_idea: input.test_idea, url: input.url, page_lines: XP.pageFactLines(facts), design: input.design, n_treatments: k - 1, primary_metric: input.primary_metric,
        primary_event: input.primary_event, devices: input.devices || [],
        sizing_lines: [`Baseline conversion rate: ${XP.pct(s.baseline_rate, 2)} (${s.baseline_source})`, `Minimum detectable effect: ${XP.pct(s.mde)} relative (target rate ${XP.pct(s.target_rate, 2)})`,
          `Sample size: ${XP.int(n)} per variant, ${XP.int(n * k)} in total`, `Duration: ${s.duration_days} days (${s.duration_weeks} weeks); feasible within ${maxDays} days: ${feasible ? "yes" : "no"}`],
        evidence_lines: ideaCtx.evidence || [], idea_rationale: ideaCtx.rationale };
      ctx.progress("Drafting the plan");
      const d = await XP.generateOrNone(ctx, "plan", vars, "no language model configured: hypothesis, variants and risks use deterministic templates");
      /* ---- reconcile */
      const defaults = rules.defaults || {};
      const idea = input.test_idea.trim().replace(/\.+$/, "");
      const primaryName = input.primary_metric || ideaCtx.primary_metric || "Conversion rate";
      const primaryEvent = input.primary_event || snake(primaryName);
      const target = `by at least ${XP.pct(s.mde)} relative to the ${XP.pct(s.baseline_rate, 2)} baseline`;
      const upParts = splitHypothesis(ideaCtx.hypothesis || "");
      let parts;
      if (d) parts = { if: d.hypothesis.if_change, then: d.hypothesis.then_outcome, because: d.hypothesis.because };
      else if (upParts) parts = { ...upParts, then: `${upParts.then} (${target})` };
      else parts = { if: asIfClause(idea), then: `${primaryName.toLowerCase()} will increase ${target}`, because: ideaCtx.because || "it removes uncertainty or effort at the page's main decision point (rationale to be confirmed by evidence)" };
      const hypothesis = XP.composeHypothesis(parts.if, parts.then, parts.because);
      const treatments = ((d && d.variants) || []).slice(0, k - 1);
      while (treatments.length < k - 1) { const nt = treatments.length + 1; treatments.push({ name: `Variant ${String.fromCharCode(65 + nt)}`, description: nt === 1 ? idea : `${idea} (alternative execution ${nt})`, changes: [idea] }); }
      const allocs = equalAllocations(k);
      const variants = [{ id: "control", name: "Control", allocation: allocs[0], is_control: true, description: "Current experience, unchanged", changes: [] },
        ...treatments.map((t, i) => ({ id: `variant_${String.fromCharCode(98 + i)}`, name: d ? t.name : `Variant ${String.fromCharCode(66 + i)}`, allocation: allocs[i + 1], is_control: false,
          description: t.description, changes: (t.changes && t.changes.length ? t.changes : [t.description]) }))];
      const drafted = (d && d.metrics) || [];
      const prim = drafted.find((m) => m.role === "primary") || null;
      let metricsOut = [{ id: snake(primaryName), name: input.primary_metric || !prim ? primaryName : prim.name, role: "primary", type: prim ? prim.type : "conversion",
        event: input.primary_event || !prim ? primaryEvent : snake(prim.event), definition: prim && prim.definition ? prim.definition : `Unique visitors who trigger '${primaryEvent}' / visitors exposed`, direction: "increase", mde: s.mde }];
      for (const m of drafted) {
        if (m === prim) continue;
        const role = m.role === "primary" ? "secondary" : m.role;
        metricsOut.push({ id: snake(m.name), name: m.name, role, type: m.type || "conversion", event: snake(m.event), definition: m.definition || "", direction: role === "guardrail" ? "no_harm" : m.direction || "increase", mde: null });
      }
      if (!d) metricsOut.push({ id: "cta_click_through", name: "Primary CTA click-through rate", role: "secondary", type: "conversion", event: "cta_click", definition: "Visitors who click the primary CTA / visitors exposed", direction: "increase", mde: null });
      if (!metricsOut.some((m) => m.role === "guardrail")) {
        for (const g of defaults.guardrails || [{ id: "bounce_rate", name: "Bounce rate", type: "rate", event: "bounce" }])
          metricsOut.push({ id: g.id, name: g.name, role: "guardrail", type: g.type || "rate", event: g.event || g.id, definition: g.definition || "", direction: "no_harm", mde: null });
        ctx.check({ rule: "plan.guardrails", status: "info", severity: "low", message: "Default guardrail metrics added from experiment_review_rules." });
      }
      const seenIds = new Set();
      metricsOut = metricsOut.filter((m) => (seenIds.has(m.id) ? false : (seenIds.add(m.id), true)));
      const tg = d ? d.targeting : { audiences: ["All visitors to the page"], exclusions: ["Internal and QA traffic", "Known bots"], geo: [], conditions: [] };
      const targeting = { page_url: input.url, pages: [input.url], devices: (input.devices || []).map((x) => String(x).toLowerCase()), audiences: tg.audiences || [],
        exclusions: tg.exclusions && tg.exclusions.length ? tg.exclusions : ["Internal and QA traffic", "Known bots"], geo: tg.geo || [], conditions: tg.conditions || [], traffic_allocation: +alloc };
      const risks = [];
      if (!s.feasible) risks.push({ risk: `Underpowered: ${s.duration_days} days needed but the limit is ${s.max_duration_days} days.`, likelihood: "high",
        mitigation: `Raise the MDE to at least ${XP.pct(s.achievable_mde_at_max_duration || 0, 1)}, reduce variants or increase traffic allocation.`, source: "computed" });
      if (s.is_estimate) risks.push({ risk: "Sizing inputs are estimates, not measurements.", likelihood: "medium", mitigation: "Confirm the baseline and traffic from analytics before launch.", source: "computed" });
      if (k > 2) risks.push({ risk: `${k - 1} comparisons inflate false positives.`, likelihood: "medium", mitigation: `Bonferroni alpha ${s.alpha_per_comparison.toFixed(4)} per comparison is already applied in the sizing.`, source: "computed" });
      if ((s.duration_weeks || 0) <= 1) risks.push({ risk: "A one-week test is exposed to day-of-week and novelty effects.", likelihood: "medium", mitigation: "Keep the full week even if results look decisive early; do not peek without the sequential plan.", source: "computed" });
      if (metrics.period) risks.push({ risk: "Seasonality and campaigns can shift the baseline during the test.", likelihood: "low", mitigation: `Baseline measured over ${metrics.period}; avoid launching during campaign spikes and annotate tracking outages.`, source: "computed" });
      if (d) risks.push(...(d.risks || []).map((r) => ({ risk: r.risk, likelihood: r.likelihood || "medium", mitigation: r.mitigation, source: "model" })));
      else risks.push({ risk: "Sample ratio mismatch from redirects, caching or bot traffic.", likelihood: "low", mitigation: "Monitor the allocation daily with a chi-square SRM check (Experiment Review).", source: "template" },
        { risk: "Flicker when the variation is applied client-side.", likelihood: "medium", mitigation: "Deliver the variation bundle early in <head> or at the edge; QA on slow networks.", source: "template" });
      const assumptions = [...((d && d.assumptions) || []),
        `Visitors are randomised independently at ${XP.g(targeting.traffic_allocation)}% allocation with equal splits (${variants.map((v) => `${XP.g(v.allocation)}%`).join(", ")}).`,
        `The baseline of ${XP.pct(s.baseline_rate, 2)} holds during the test (${s.baseline_source}).`, `'${metricsOut[0].event}' is tracked identically in every arm.`];
      const title = d ? d.title : ideaCtx.title || T().truncate(idea, 80);
      const expId = input.experiment_id || `exp-${T().slugify(title, 40)}`;
      ctx.check({ rule: "plan.hypothesis_format", status: /^if\b[\s\S]+\bthen\b[\s\S]+\bbecause\b/i.test(hypothesis) ? "pass" : "fail", severity: "medium", message: "Hypothesis follows If / Then / Because." });
      const allocSum = XP.round(sum(variants.map((v) => v.allocation)), 6);
      ctx.check({ rule: "plan.allocation", status: Math.abs(allocSum - 100) < 1e-6 ? "pass" : "fail", severity: "high", message: `${variants.length} arms, one control, allocations sum to ${XP.g(allocSum)}%.` });
      /* ---- export + register */
      const plan = { experiment_id: expId, title, page_url: input.url, test_idea: input.test_idea, design: input.design, hypothesis, hypothesis_parts: parts, variants, targeting, metrics: metricsOut,
        sizing: s, sequential: seq, bayesian: bayes, risks, assumptions,
        page: { title: facts.title, h1: facts.h1, ctas: facts.ctas.slice(0, 8).map((c) => ({ text: c.text, selector: c.selector })), screenshot: null } };
      const wanted = input.exporters && input.exporters.length ? input.exporters : ["all"];
      const names = wanted.includes("all") ? Object.keys(EXPORTERS) : wanted.filter((w) => EXPORTERS[w]);
      for (const w of wanted) if (w !== "all" && !EXPORTERS[w]) ctx.warn(`Unknown exporter '${w}' (available: ${Object.keys(EXPORTERS).join(", ")}).`);
      const exportsOut = [];
      for (const name of names) {
        const [filename, fn] = EXPORTERS[name];
        const payload = fn(plan);
        if (typeof payload === "string") ctx.artifact({ name: filename, mime: "application/yaml", content: payload, description: `${name} export` });
        else ctx.artifact({ name: filename, mime: "application/json", content: JSON.stringify(payload, null, 2), description: `${name} export (map event ids in your instance)` });
        exportsOut.push({ platform: name, artifact: filename, filename });
      }
      const primM = metricsOut.find((m) => m.role === "primary");
      const metricRef = (m) => ({ id: m.id, name: m.name, type: m.type, event: m.event });
      XP.store().experiments.set(expId, XP.definition({ id: expId, name: title, status: "draft", platform: "internal", page_url: input.url, hypothesis, primary_metric: metricRef(primM),
        secondary_metrics: metricsOut.filter((m) => m.role === "secondary").map(metricRef), guardrail_metrics: metricsOut.filter((m) => m.role === "guardrail").map(metricRef),
        variants: variants.map((v) => ({ id: v.id, name: v.name, allocation: v.allocation, is_control: v.is_control, description: v.description })), traffic_allocation: targeting.traffic_allocation,
        audience: { devices: targeting.devices, geo: targeting.geo, conditions: targeting.conditions }, planned_duration_days: s.planned_duration_days, baseline_rate: s.baseline_rate, mde: s.mde,
        expected_daily_visitors: Math.round(s.daily_visitors), instrumentation: { events: metricsOut.map((m) => m.event), verified: false, design: input.design } }));
      ctx.degraded(`experiment definition registered in the ${XP.STORE_NOTE}`);
      plan.exports = exportsOut;
      plan.definition_upserted = true;
      plan.markdown = planMarkdown(plan);
      const { markdown, ...json } = plan;
      ctx.artifact({ name: "experiment_plan.md", mime: "text/markdown", content: plan.markdown, description: "Experiment plan" });
      ctx.artifact({ name: "experiment_plan.json", mime: "application/json", content: JSON.stringify(json, null, 2), description: "Experiment plan (platform-neutral)" });
      const view = [
        { type: "table", title: "Sizing (computed in code)", rows: [
          { item: "Baseline", value: `${XP.pct(s.baseline_rate, 2)} (${s.baseline_source})` }, { item: "MDE (relative)", value: `${XP.pct(s.mde, 1)} -> target ${XP.pct(s.target_rate, 2)}` },
          { item: "Sample size per variant", value: XP.int(n) }, { item: "Duration (fixed horizon)", value: `${s.duration_days} days (${s.duration_weeks} weeks)` },
          { item: "Achievable MDE in the limit", value: XP.pct(ach || 0, 1) }, { item: "Planned duration", value: `${planned} days (${input.design})` }, { item: "Feasible", value: feasible ? "yes" : "no" }] },
        { type: "table", title: "Sensitivity", rows: s.sensitivity.map((r) => ({ MDE: XP.pct(r.mde, 1), "per variant": XP.int(r.sample_size_per_variant), weeks: r.duration_weeks, feasible: r.feasible ? "yes" : "no" })) },
      ];
      if (seq.available) view.push({ type: "table", title: `O'Brien-Fleming schedule (C = ${seq.constant})`, rows: seq.schedule.map((r) => ({ look: r.look, week: r.week, "per variant": XP.int(r.cumulative_per_variant), z: r.z_boundary, "nominal p": r.nominal_alpha, "alpha spent": r.cumulative_alpha_spent })) });
      view.push({ type: "markdown", title: "Plan", markdown: plan.markdown });
      return {
        output: plan,
        summary: `Plan '${T().truncate(title, 60)}' (${input.design}): ${XP.int(n)} per variant for a ${XP.pct(s.mde)} MDE on a ${XP.pct(s.baseline_rate, 2)} baseline, ${planned} days${s.feasible ? "" : " - exceeds the max duration"}; exported to ${exportsOut.length} platform format(s)`,
        view,
      };
    },
    selftest() {
      return { url: DEMO_URL, test_idea: "Change the hero CTA from 'Get a quote' to 'See my price in 2 minutes' and make it more prominent", mde: 0.2, design: "sequential", looks: 4,
        max_duration_days: 56, experiment_id: "exp-hero-cta-price-promise", primary_metric: "Quote start rate", primary_event: "quote_start" };
    },
  });
  XP.planning = { sampleSize, achievableMde, durationForSample, obrienFleming, sequentialDesign, bayesianDesign, gsCrossing, equalAllocations, splitHypothesis, asIfClause };
})();
