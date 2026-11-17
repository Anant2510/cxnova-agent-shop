/* Experiment Review - browser twin of caf/agents/experimentation/experiment_review.py.
   A deterministic pre-launch gate: the rules in experiment_review_rules.yaml run in code over the platform-neutral
   definition (embedded registry + plans registered in this page session), with power-based sample size, overlap with
   live tests, interim SRM (chi-square) and a JUnit report. The model only explains flagged findings (unknown rule ids dropped). */
(() => {
  const XP = (CAF.xp = CAF.xp || {});
  const T = () => CAF.lib.text;

  /* ============================================================ helpers (pure) */
  const pageKey = (url) => { try { const u = new URL(url); return (u.hostname + (u.pathname || "/").replace(/\/+$/, "")).toLowerCase(); } catch { return ""; } };
  const overlap = (a, b) => (!a || !a.length || !b || !b.length ? true : a.some((x) => b.map((y) => String(y).toLowerCase()).includes(String(x).toLowerCase())));
  const datesOverlap = (a, b) => {
    const s1 = XP.day(a.start_date), s2 = XP.day(b.start_date);
    if (s1 === null || s2 === null) return true;
    const e1 = XP.day(a.end_date) ?? Infinity, e2 = XP.day(b.end_date) ?? Infinity;
    return s1 <= e2 && s2 <= e1;
  };
  /** Chi-square goodness of fit of observed visitors per variant against the planned allocation (scipy.stats.chisquare). */
  function srmTest(observed, planned) {
    const keys = Object.keys(planned).filter((k) => k in observed);
    const totObs = keys.reduce((s, k) => s + observed[k], 0), totPlan = keys.reduce((s, k) => s + planned[k], 0);
    if (!keys.length || totObs <= 0 || totPlan <= 0) return { available: false };
    const expected = keys.map((k) => (totObs * planned[k]) / totPlan);
    const chi2 = keys.reduce((s, k, i) => s + (observed[k] - expected[i]) ** 2 / expected[i], 0);
    return { available: true, chi2: XP.round(chi2, 4), p_value: XP.chi2Sf(chi2, keys.length - 1), dof: keys.length - 1,
      observed: Object.fromEntries(keys.map((k) => [k, Math.trunc(observed[k])])), expected: Object.fromEntries(keys.map((k, i) => [k, XP.round(expected[i], 1)])),
      observed_share: Object.fromEntries(keys.map((k) => [k, XP.round(observed[k] / totObs, 4)])) };
  }
  const byVariant = (rows) => { const out = {}; for (const r of [...rows].sort((a, b) => (String(a.variant) < String(b.variant) ? -1 : 1))) out[String(r.variant)] = (out[String(r.variant)] || 0) + (+r.visitors || 0); return out; };
  function segmentSrm(rows, planned) {
    if (!rows.length || !rows.some((r) => "segment" in r)) return [];
    const segs = [...new Set(rows.map((r) => String(r.segment)))].sort();
    const out = [];
    for (const seg of segs) { const t = srmTest(byVariant(rows.filter((r) => String(r.segment) === seg)), planned); if (t.available) out.push({ segment: seg, p_value: t.p_value, chi2: t.chi2, observed: t.observed }); }
    return out.sort((a, b) => a.p_value - b.p_value);
  }
  function definitionFromPlan(plan) {
    const metrics = plan.metrics || [];
    const m = (x) => ({ id: x.id, name: x.name, type: x.type || "conversion", event: x.event ?? null });
    const prim = metrics.find((x) => x.role === "primary");
    const t = plan.targeting || {}, s = plan.sizing || {};
    return XP.definition({ id: plan.experiment_id, name: plan.title || plan.experiment_id, status: "draft", page_url: plan.page_url || "", hypothesis: plan.hypothesis || "",
      primary_metric: prim ? m(prim) : null, secondary_metrics: metrics.filter((x) => x.role === "secondary").map(m), guardrail_metrics: metrics.filter((x) => x.role === "guardrail").map(m),
      variants: (plan.variants || []).map((v) => ({ id: v.id, name: v.name, allocation: +v.allocation, is_control: !!v.is_control, description: v.description || "" })),
      traffic_allocation: t.traffic_allocation ?? 100, audience: { devices: t.devices || [], geo: t.geo || [], conditions: t.conditions || [] },
      planned_duration_days: s.planned_duration_days ?? null, baseline_rate: s.baseline_rate ?? null, mde: s.mde ?? null,
      expected_daily_visitors: Math.round(s.daily_visitors || 0) || null, instrumentation: { events: metrics.map((x) => x.event).filter(Boolean) } });
  }
  const normalise = (d) => { const x = XP.definition(d); x.variants = (x.variants || []).map((v) => ({ id: String(v.id), name: String(v.name ?? v.id), allocation: +v.allocation, is_control: !!v.is_control, description: v.description || "" })); x.traffic_allocation = +x.traffic_allocation; return x; };

  /* ============================================================ rules (pure): fn(defn, env, params) -> [outcome, message, data] */
  const g = XP.g;
  function sampleSizeCheck(d, alpha, power, margin = 1) {
    const missing = [["baseline_rate", d.baseline_rate], ["mde", d.mde], ["expected_daily_visitors", d.expected_daily_visitors], ["planned_duration_days", d.planned_duration_days]].filter(([, v]) => !v).map(([k]) => k);
    if (missing.length || d.variants.length < 2) return { available: false, missing: missing.length ? missing : ["variants"] };
    const k = d.variants.length;
    let required;
    try { required = XP.planning.sampleSize(+d.baseline_rate, +d.mde, alpha, power, k); } catch (e) { return { available: false, missing: [e.message] }; }
    const totalAlloc = d.variants.reduce((s, v) => s + v.allocation, 0) || 100;
    const smallest = d.variants.reduce((a, b) => (b.allocation < a.allocation ? b : a));
    const share = smallest.allocation / totalAlloc;
    const dailyArm = (+d.expected_daily_visitors * d.traffic_allocation) / 100 * share;
    const available = dailyArm * +d.planned_duration_days;
    const daysNeeded = dailyArm > 0 ? Math.ceil((required * margin) / dailyArm) : null;
    return { available: true, required_per_variant: required, available_smallest_arm: Math.trunc(available), smallest_arm: smallest.id, smallest_arm_share: XP.round(share, 4),
      daily_per_smallest_arm: XP.round(dailyArm, 1), coverage: XP.round(available / required, 4), days_needed: daysNeeded, weeks_needed: daysNeeded ? Math.ceil(daysNeeded / 7) : null,
      alpha_per_comparison: XP.round(alpha / Math.max(1, k - 1), 6), power, variants: k };
  }
  const events = (d) => new Set(((U.isObj(d.instrumentation) && d.instrumentation.events) || []).map(String));
  const reEsc = (s) => String(s).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const HYPOTHESIS_RE = /^\s*if\b[\s\S]{5,}?\bthen\b[\s\S]{5,}?\bbecause\b[\s\S]{5,}/i;
  const RULES = {
    "allocation.sum_100": (d, env, p) => { const total = d.variants.reduce((s, v) => s + v.allocation, 0); const ok = Math.abs(total - 100) <= +(p.tolerance ?? 0.01);
      return [ok ? "pass" : "fail", `Variant allocations sum to ${g(total)}%` + (ok ? "" : " instead of 100%") + ".", { total, allocations: Object.fromEntries(d.variants.map((v) => [v.id, v.allocation])) }]; },
    "allocation.single_control": (d) => { const n = d.variants.filter((v) => v.is_control).length; return [n === 1 ? "pass" : "fail", `${n} variant(s) marked as control.`, { controls: n }]; },
    "allocation.min_variants": (d, env, p) => { const n = d.variants.length; return [n >= +(p.min_variants ?? 2) ? "pass" : "fail", `${n} variant(s) configured.`, { variants: n }]; },
    "allocation.balanced": (d, env, p) => { if (d.variants.length < 2) return ["na", "Fewer than two variants.", {}]; const a = d.variants.map((v) => v.allocation); const spread = Math.max(...a) - Math.min(...a);
      return [spread <= +(p.max_spread_pct ?? 1) ? "pass" : "fail", `Allocation spread is ${g(spread)} points (${d.variants.map((v) => `${v.id} ${g(v.allocation)}%`).join(", ")}).`, { spread }]; },
    "traffic.allocation_range": (d, env, p) => { const t = d.traffic_allocation; return [+(p.min_pct ?? 1) <= t && t <= +(p.max_pct ?? 100) ? "pass" : "fail", `Traffic allocation is ${g(t)}%.`, { traffic_allocation: t }]; },
    "metric.primary_defined": (d) => { const m = d.primary_metric; if (!m) return ["fail", "No primary metric is defined.", {}]; if (!String(m.event || "").trim()) return ["fail", `Primary metric '${m.name}' has no event.`, {}];
      return ["pass", `Primary metric '${m.name}' (${m.type || "conversion"}) measured by event '${m.event}'.`, { event: m.event }]; },
    "metric.guardrails_present": (d, env, p) => { const n = d.guardrail_metrics.length; return [n >= +(p.min_guardrails ?? 1) ? "pass" : "fail", `${n} guardrail metric(s)` + (n ? ": " + d.guardrail_metrics.map((x) => x.name).join(", ") : "") + ".", { guardrails: n }]; },
    "metric.secondary_limit": (d, env, p) => { const n = d.secondary_metrics.length; return [n <= +(p.max_secondary ?? 5) ? "pass" : "fail", `${n} secondary metric(s).`, { secondary: n }]; },
    "stats.baseline_present": (d) => { const b = d.baseline_rate; const ok = b !== null && b !== undefined && b > 0 && b < 1; return [ok ? "pass" : "fail", ok ? `Baseline conversion rate ${XP.pct(b, 2)}.` : "No valid baseline conversion rate recorded.", { baseline_rate: b ?? null }]; },
    "stats.mde_realistic": (d, env, p) => { if (d.mde === null || d.mde === undefined) return ["fail", "No minimum detectable effect recorded.", {}]; const lo = +(p.min_mde ?? 0.02), hi = +(p.max_mde ?? 0.5); const ok = lo <= d.mde && d.mde <= hi;
      return [ok ? "pass" : "fail", `Relative MDE is ${XP.pct(d.mde, 1)}` + (ok ? "" : ` (programme range ${XP.pct(lo)}-${XP.pct(hi)})`) + ".", { mde: d.mde }]; },
    "stats.sample_size_sufficient": (d, env, p) => { const s = env.sample_size; if (!s.available) return ["fail", `Cannot verify sample size - missing ${(s.missing || []).join(", ")}.`, s];
      const ok = s.coverage >= +(p.margin ?? 1);
      let msg = `Needs ${XP.int(s.required_per_variant)} visitors per variant (alpha ${g(s.alpha_per_comparison)}, power ${XP.pct(s.power)}); the plan delivers ${XP.int(s.available_smallest_arm)} in the smallest arm (${XP.pct(s.coverage, 1)} of required)`;
      if (!ok) msg += ` - about ${XP.int(s.days_needed)} days (${XP.int(s.weeks_needed)} weeks) at the planned traffic`;
      return [ok ? "pass" : "fail", msg + ".", s]; },
    "duration.min_days": (d, env, p) => { const n = d.planned_duration_days; if (n === null || n === undefined) return ["fail", "No planned duration.", {}]; return [n >= +(p.min_days ?? 7) ? "pass" : "fail", `Planned duration is ${n} day(s).`, { days: n }]; },
    "duration.whole_weeks": (d) => { const n = d.planned_duration_days; if (n === null || n === undefined) return ["na", "No planned duration.", {}]; return [n % 7 === 0 && n > 0 ? "pass" : "fail", `${n} days = ${(n / 7).toFixed(2)} weeks.`, { days: n }]; },
    "duration.max_days": (d, env, p) => { const n = d.planned_duration_days; if (n === null || n === undefined) return ["na", "No planned duration.", {}]; return [n <= +(p.max_days ?? 56) ? "pass" : "fail", `Planned duration is ${n} days (maximum ${p.max_days ?? 56}).`, { days: n }]; },
    "dates.consistent": (d) => { const s = XP.day(d.start_date), e = XP.day(d.end_date); if (s === null || e === null || !d.planned_duration_days) return ["na", "Start/end dates not both set.", {}];
      const inclusive = e - s + 1; const ok = [e - s, inclusive].includes(d.planned_duration_days) && e >= s;
      return [ok ? "pass" : "fail", `${d.start_date} to ${d.end_date} is ${inclusive} calendar days; planned ${d.planned_duration_days}.`, { calendar_days: inclusive }]; },
    "targeting.overlap": (d, env, p) => { const hits = env.overlaps; if (!hits.length) return ["pass", `No ${(p.statuses || ["running"]).join("/")} test targets the same page and audience in the same period.`, {}];
      return ["fail", `Overlaps with ${hits.length} live test(s) on the same page and audience: ${hits.map((h) => `${h.id} (${h.status}, ${h.devices.join(", ")}, ${h.dates})`).join("; ")}.`, { overlaps: hits }]; },
    "srm.interim": (d, env, p) => { const srm = env.srm; if (!srm.available) return ["na", "No interim results to check for sample ratio mismatch.", {}];
      const total = Object.values(srm.observed).reduce((s, v) => s + v, 0);
      if (total < +(p.min_visitors ?? 1000)) return ["na", `Only ${XP.int(total)} visitors so far; SRM check needs ${XP.int(p.min_visitors ?? 1000)}.`, srm];
      const bad = srm.p_value < +(p.p_threshold ?? 0.001);
      let msg = `Observed ${Object.entries(srm.observed).map(([k, v]) => `${k} ${XP.int(v)}`).join(", ")} vs expected ${Object.entries(srm.expected).map(([k, v]) => `${k} ${XP.int(v)}`).join(", ")}: chi-square ${srm.chi2.toFixed(2)}, p = ${XP.e(srm.p_value)}`;
      if (bad && srm.worst_segment) msg += `; the imbalance is concentrated in the '${srm.worst_segment.segment}' segment (p = ${XP.e(srm.worst_segment.p_value)})`;
      return [bad ? "fail" : "pass", msg + ".", srm]; },
    "instrumentation.primary_event": (d) => { if (!d.primary_metric || !d.primary_metric.event) return ["fail", "No primary metric event to check.", {}]; const ev = events(d); const ok = ev.has(d.primary_metric.event);
      return [ok ? "pass" : "fail", `Primary event '${d.primary_metric.event}' is ${ok ? "instrumented" : "NOT in the instrumentation list"}.`, { instrumented: [...ev].sort() }]; },
    "instrumentation.metric_events": (d) => { const needed = [...d.secondary_metrics, ...d.guardrail_metrics].map((m) => m.event).filter(Boolean); const ev = events(d); const missing = needed.filter((e) => !ev.has(e));
      if (!needed.length) return ["na", "No secondary or guardrail events defined.", {}];
      return [missing.length ? "fail" : "pass", `${needed.length - missing.length} of ${needed.length} secondary/guardrail events instrumented` + (missing.length ? `; missing: ${missing.join(", ")}` : "") + ".", { missing }]; },
    "qa.links_present": (d) => { const links = d.qa_links || [];
      const without = d.variants.filter((v) => !links.some((l) => new RegExp(`[?&](?:variant|variation|v)=${reEsc(v.id)}(?:&|$|#)`).test(l))).map((v) => v.id);
      const named = without.length < d.variants.length;
      const ok = !!links.length && links.length >= d.variants.length && (!named || !without.length);
      return [ok ? "pass" : "fail", `${links.length} QA link(s) for ${d.variants.length} variant(s)` + (named && without.length ? `; no link for ${without.join(", ")}` : "") + ".", { qa_links: links, variants_without_link: named ? without : [] }]; },
    "qa.links_valid": (d) => { const links = d.qa_links || []; if (!links.length) return ["na", "No QA links.", {}]; const bad = links.filter((l) => !/^https?:\/\/[^\s/]+/.test(l));
      return [bad.length ? "fail" : "pass", bad.length ? `Invalid QA links: ${bad.join(", ")}.` : "All QA links are absolute URLs.", {}]; },
    "hypothesis.format": (d) => { const ok = HYPOTHESIS_RE.test(d.hypothesis || ""); return [ok ? "pass" : "fail", ok ? "Hypothesis follows If / Then / Because." : `Hypothesis does not follow If / Then / Because: '${T().truncate(d.hypothesis || "(empty)", 90)}'.`, {}]; },
    "metadata.owner": (d) => [String(d.owner || "").trim() ? "pass" : "fail", d.owner ? `Owner: ${d.owner}.` : "No owner assigned.", {}],
  };
  function findOverlaps(d, others, statuses) {
    const out = [];
    for (const o of others) {
      if (o.id === d.id || !statuses.includes(o.status)) continue;
      if (pageKey(o.page_url) !== pageKey(d.page_url)) continue;
      if (!(overlap(d.audience.devices, o.audience.devices) && overlap(d.audience.geo, o.audience.geo))) continue;
      if (!datesOverlap(d, o)) continue;
      const od = new Set((o.audience.devices || []).map((x) => String(x).toLowerCase()));
      const shared = [...new Set((d.audience.devices || []).map((x) => String(x).toLowerCase()))].filter((x) => od.has(x)).sort();
      out.push({ id: o.id, name: o.name, status: o.status, devices: shared.length ? shared : ["all devices"], dates: `${o.start_date || "?"} to ${o.end_date || "?"}` });
    }
    return out;
  }
  function buildEnv(defn, others, rows, cfg) {
    const stats = cfg.stats || {};
    const params = Object.fromEntries((cfg.rules || []).map((r) => [r.id, r.params || {}]));
    const planned = Object.fromEntries(defn.variants.map((v) => [v.id, v.allocation]));
    const srm = srmTest(byVariant(rows), planned);
    if (srm.available) {
      const segs = segmentSrm(rows, planned);
      srm.segments = segs;
      if (segs.length && segs[0].p_value < +((params["srm.interim"] || {}).p_threshold ?? 0.001)) srm.worst_segment = segs[0];
    }
    return { sample_size: sampleSizeCheck(defn, +(stats.alpha ?? 0.05), +(stats.power ?? 0.8), +((params["stats.sample_size_sufficient"] || {}).margin ?? 1)),
      overlaps: findOverlaps(defn, others, (params["targeting.overlap"] || {}).statuses || ["running", "paused"]), srm };
  }
  function evaluateRules(defn, env, cfg) {
    const out = [];
    for (const rule of cfg.rules || []) {
      if (rule.enabled === false) continue;
      const fn = RULES[rule.id];
      if (!fn) { out.push({ rule_id: rule.id, name: rule.name || rule.id, status: "info", outcome: "na", severity: rule.severity || "low", blocking: false, message: "No implementation for this rule id (check the skill file).", fix: rule.fix || "", ref: rule.ref ?? null, data: {} }); continue; }
      let outcome, message, data;
      try { [outcome, message, data] = fn(defn, env, rule.params || {}); } catch (e) { [outcome, message, data] = ["fail", `Rule error: ${e.name}: ${e.message}`, {}]; }
      const blocking = rule.blocking !== undefined ? !!rule.blocking : true;
      const status = { pass: "pass", na: "info" }[outcome] || (blocking ? "fail" : "warn");
      out.push({ rule_id: rule.id, name: rule.name || rule.id, status, outcome, severity: rule.severity || "medium", blocking, message, fix: rule.fix || "", ref: rule.ref ?? null, data: data || {} });
    }
    return out;
  }
  const gate = (results) => (results.some((r) => r.status === "fail") ? "fail" : "pass");
  const xmlAttr = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/\n/g, "&#10;").replace(/\r/g, "&#13;").replace(/\t/g, "&#09;");
  const xmlText = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  /** JUnit XML (same structure as the Python ElementTree output): failures, warnings as system-out, n/a as skipped. */
  function junitXml(id, results, gateStatus, expl = {}) {
    const fails = results.filter((r) => r.status === "fail").length, skipped = results.filter((r) => r.status === "info").length;
    const stamp = new Date().toISOString().slice(0, 19) + "+00:00";
    const L = ['<?xml version="1.0" encoding="UTF-8"?>', `<testsuites name="caf.experiment_review" tests="${results.length}" failures="${fails}" errors="0" skipped="${skipped}">`,
      `  <testsuite name="experiment_review.${xmlAttr(id)}" tests="${results.length}" failures="${fails}" errors="0" skipped="${skipped}" timestamp="${stamp}">`,
      "    <properties>", `      <property name="gate" value="${gateStatus}" />`, `      <property name="experiment_id" value="${xmlAttr(id)}" />`, "    </properties>"];
    for (const r of results) {
      const open = `    <testcase classname="experiment_review.${xmlAttr(id)}.${xmlAttr(r.rule_id.split(".")[0])}" name="${xmlAttr(r.rule_id)}" time="0"`;
      const detail = r.message + (r.fix ? `\nFix: ${r.fix}` : "") + (expl[r.rule_id] ? `\nWhy: ${expl[r.rule_id]}` : "");
      if (r.status === "fail") L.push(`${open}>`, `      <failure message="${xmlAttr(T().truncate(r.message, 300))}" type="${xmlAttr(r.severity)}">${xmlText(detail)}</failure>`, "    </testcase>");
      else if (r.status === "warn") L.push(`${open}>`, `      <system-out>${xmlText(`WARNING (${r.severity}): ${detail}`)}</system-out>`, "    </testcase>");
      else if (r.status === "info") L.push(`${open}>`, `      <skipped message="${xmlAttr(T().truncate(r.message, 300))}" />`, "    </testcase>");
      else L.push(`${open} />`);
    }
    L.push("  </testsuite>", "</testsuites>");
    return L.join("\n") + "\n";
  }
  const pyDict = (o, floats = false) => "{" + Object.entries(o).map(([k, v]) => `'${k}': ${floats && Number.isInteger(v) ? v.toFixed(1) : v}`).join(", ") + "}";
  function reviewMarkdown(out) {
    const icon = { pass: "PASS", fail: "FAIL", warn: "WARN", info: "N/A" }, order = { fail: 0, warn: 1, pass: 3, info: 2 };
    const L = [`# Experiment review: ${out.experiment_name} (\`${out.experiment_id}\`)`, "", `**Gate: ${out.gate.toUpperCase()}** - ${out.blocking_failures} blocking failure(s), ${out.warnings} warning(s), ${out.passed} passed, ${out.not_applicable} not applicable (rules v${out.rules_version}).`,
      "", out.explanation, "", "| Result | Rule | Severity | Finding | Fix |", "|---|---|---|---|---|"];
    for (const r of [...out.results].sort((a, b) => order[a.status] - order[b.status])) {
      let fix = ["fail", "warn"].includes(r.status) ? r.fix : "";
      if (r.explanation) fix = fix ? `${fix} (${r.explanation})` : r.explanation;
      L.push(`| ${icon[r.status]} | ${r.rule_id} | ${r.severity} | ${r.message} | ${fix} |`);
    }
    const s = out.sample_size;
    if (s.available) L.push("", "## Sample size", `- Required: ${XP.int(s.required_per_variant)} per variant; delivered in the smallest arm: ${XP.int(s.available_smallest_arm)} (${XP.pct(s.coverage, 1)}); needed duration at planned traffic: ${XP.int(s.days_needed)} days`);
    if (out.srm.available) {
      L.push("", "## Sample ratio", `- Chi-square ${out.srm.chi2.toFixed(2)}, p = ${XP.e(out.srm.p_value)}; observed ${pyDict(out.srm.observed)}, expected ${pyDict(out.srm.expected, true)}`);
      for (const seg of out.srm.segments || []) L.push(`- Segment ${seg.segment}: p = ${XP.e(seg.p_value)}, observed ${pyDict(seg.observed)}`);
    }
    return L.join("\n") + "\n";
  }
  const CATEGORY = { allocation: "experimentation", traffic: "experimentation", metric: "analytics", stats: "experimentation", duration: "experimentation", dates: "experimentation",
    targeting: "experimentation", srm: "analytics", instrumentation: "analytics", qa: "experimentation", hypothesis: "experimentation", metadata: "experimentation" };

  CAF.agent("experiment_review", {
    async run(input, ctx) {
      if (!XP.generateOrNone || !XP.pageFacts) throw new Error("The shared experimentation kit (CAF.xp, defined in experiment_ideation.js) did not load");
      if (!XP.planning) throw new Error("experiment_planning.js (sample-size helpers) did not load");
      /* ---- load */
      const registry = await XP.registry(ctx);
      let defn;
      if (input.definition && U.isObj(input.definition)) {
        const d = { ...input.definition };
        d.id = d.id ?? (input.experiment_id || "experiment");
        d.name = d.name ?? d.id;
        defn = normalise(d);
      } else if (input.experiment_id) {
        defn = registry.find((e) => e.id === input.experiment_id);
        if (defn) defn = normalise(defn);
        else {
          const plan = ctx.upstreamOutput("experiment_planning");
          if (plan && plan.experiment_id === input.experiment_id) defn = definitionFromPlan(plan);
          else throw new Error(`Experiment '${input.experiment_id}' was not found through the experiments connector`);
        }
      } else {
        const plan = ctx.upstreamOutput("experiment_planning");
        if (!plan) throw new Error("Provide an experiment_id or a definition to review");
        defn = definitionFromPlan(plan);
        ctx.warn(`Reviewing the upstream plan '${defn.id}'.`);
      }
      if (XP.store().experiments.has(defn.id)) ctx.degraded(`definition read from the ${XP.STORE_NOTE}`);
      const others = registry.filter((e) => e.id !== defn.id).map(normalise);
      const resName = `experiments/results/${defn.id}`;
      const rows = ctx.hasData(resName) ? await ctx.data(resName) : [];
      /* ---- evaluate (pure code) */
      const cfg = ctx.skill("experiment_review_rules");
      const env = buildEnv(defn, others, rows, cfg);
      const res = evaluateRules(defn, env, cfg);
      for (const r of res) ctx.check({ rule: r.rule_id, status: r.status, severity: r.severity, message: r.message, ref: r.ref, target: defn.id });
      /* ---- explain (model may only explain findings that exist) */
      const flagged = res.filter((r) => ["fail", "warn"].includes(r.status));
      const expl = {}, fixes = {};
      let summaryText = "";
      if (flagged.length && input.explain !== false) {
        const out = await XP.generateOrNone(ctx, "explain", { experiment_id: defn.id, experiment_name: defn.name, hypothesis: defn.hypothesis || "", gate: gate(res),
          finding_lines: flagged.map((r) => `${r.rule_id} [${r.status.toUpperCase()}, ${r.severity}] ${r.message} (standard fix: ${r.fix})`) }, "no language model configured: findings explained with the rule pack's fix texts");
        if (out) {
          const ids = new Set(flagged.map((r) => r.rule_id));
          let dropped = 0;
          for (const item of out.items || []) { if (ids.has(item.rule_id)) { expl[item.rule_id] = String(item.explanation || "").trim(); fixes[item.rule_id] = String(item.fix || "").trim(); } else dropped++; }
          if (dropped) ctx.log(`dropped ${dropped} model explanation(s) for unknown rule ids`);
          summaryText = String(out.summary || "").trim();
        }
      }
      for (const r of res) { r.explanation = expl[r.rule_id] || ""; if (fixes[r.rule_id]) r.fix = fixes[r.rule_id]; }
      /* ---- report */
      const g0 = gate(res);
      const fails = res.filter((r) => r.status === "fail"), warns = res.filter((r) => r.status === "warn");
      if (!summaryText) summaryText = `The launch gate ${g0 === "fail" ? "fails" : "passes"} for '${defn.name}'` + (fails.length ? `: ${fails.length} blocking issue(s), starting with ${fails.slice(0, 4).map((r) => r.name.toLowerCase()).join(", ")}.` : ".") + (warns.length ? ` ${warns.length} warning(s) to review.` : "");
      const findings = [...fails, ...warns].map((r) => ({ title: `${r.name}: ${r.status === "fail" ? "blocking failure" : "warning"}`, detail: r.message, severity: r.status === "fail" ? r.severity : "low",
        category: CATEGORY[r.rule_id.split(".")[0]] || "experimentation", evidence: r.rule_id, recommendation: r.fix, target: defn.id, source_agent: "experiment_review" }));
      const junit = junitXml(defn.id, res, g0, Object.fromEntries(res.filter((r) => r.explanation).map((r) => [r.rule_id, r.explanation])));
      ctx.artifact({ name: "junit.xml", mime: "application/xml", content: junit, description: "JUnit report (CI gate)" });
      const output = { experiment_id: defn.id, experiment_name: defn.name, platform: defn.platform || "", status: defn.status || "", gate: g0, blocking_failures: fails.length, warnings: warns.length,
        passed: res.filter((r) => r.status === "pass").length, not_applicable: res.filter((r) => r.status === "info").length,
        results: res.map(({ outcome, ...r }) => r), findings, sample_size: env.sample_size, srm: env.srm, overlaps: env.overlaps, explanation: summaryText, junit_artifact: "junit.xml", rules_version: String(cfg.version ?? "") };
      const md = reviewMarkdown(output);
      ctx.artifact({ name: "review.md", mime: "text/markdown", content: md, description: "Experiment review" });
      ctx.artifact({ name: "review.json", mime: "application/json", content: JSON.stringify(output, null, 2), description: "Experiment review (structured)" });
      return {
        output,
        summary: `Gate ${g0.toUpperCase()} for ${defn.id}: ${fails.length} blocking failure(s), ${warns.length} warning(s), ${output.passed} passed` + (fails.length ? ` - ${fails.slice(0, 5).map((r) => r.rule_id).join(", ")}` : ""),
        view: [{ type: "table", title: `Launch gate: ${g0.toUpperCase()} (rules v${output.rules_version})`, rows: [...res].sort((a, b) => ({ fail: 0, warn: 1, info: 2, pass: 3 })[a.status] - ({ fail: 0, warn: 1, info: 2, pass: 3 })[b.status])
          .map((r) => ({ result: r.status.toUpperCase(), rule: r.rule_id, severity: r.severity, finding: r.message, fix: ["fail", "warn"].includes(r.status) ? r.fix : "" })) },
          { type: "markdown", title: "Review", markdown: md }],
      };
    },
    selftest() { return { experiment_id: "exp-mobile-sticky" }; },
  });
  XP.review = { srmTest, definitionFromPlan, evaluateRules, gate, junitXml, sampleSizeCheck };
})();
