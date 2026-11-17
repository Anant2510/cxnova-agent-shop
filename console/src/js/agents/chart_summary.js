/* Chart Summary - browser twin of caf/agents/experimentation/chart_summary.py.
   Summarise the data, not the pixels: x / y / group detection, then trend (least squares, p-value), seasonality (ACF at
   plausible lags), change points (binary segmentation, BIC-style penalty + minimum shift), outliers (robust MAD z on the
   segment-fit residuals) and group contributions are computed here. The model writes commentary and a WCAG long
   description from those facts only (uncomputed numbers -> deterministic text). Rendered with CAF.lib.chart.line.
   Image-only input degrades: reading values from pixels is left to the Python service's vision path. */
(() => {
  const XP = (CAF.xp = CAF.xp || {});
  const T = () => CAF.lib.text;
  const OUTLIER_Z = 3.5;
  const SEASONAL_LAGS = { daily: [7, 14, 28], weekly: [4, 13, 26, 52], monthly: [3, 6, 12], quarterly: [2, 4], yearly: [] };
  const PERIOD_WORD = { daily: "day", weekly: "week", monthly: "month", quarterly: "quarter", yearly: "year" };
  const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  /* ============================================================ column detection (pure) */
  const DATE_RE = /^\d{4}[-/]\d{1,2}([-/]\d{1,2})?([ T].*)?$|^\d{1,2}\/\d{1,2}\/\d{2,4}$/;
  /** Timestamp (ms, UTC) of a date-like value, pandas.to_datetime-style for the common forms; null when not a date. */
  function toTime(v) {
    if (v === null || v === undefined || typeof v === "number" || typeof v === "boolean") return null;
    const s = String(v).trim();
    let m = s.match(/^(\d{4})[-/](\d{1,2})(?:[-/](\d{1,2}))?(?:[ T](\d{1,2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?)?(Z|[+-]\d{2}:?\d{2})?$/);
    if (m) { const t = Date.UTC(+m[1], +m[2] - 1, +(m[3] || 1), +(m[4] || 0), +(m[5] || 0), +(m[6] || 0)); const d = new Date(t); return d.getUTCMonth() === +m[2] - 1 ? t : null; }
    if ((m = s.match(/^(\d{1,2})\/(\d{1,2})\/(\d{2,4})$/))) { let y = +m[3]; if (y < 100) y += y < 69 ? 2000 : 1900; return Date.UTC(y, +m[1] - 1, +m[2]); }
    return null;
  }
  const isNumCol = (rows, c) => { const v = rows.map((r) => r[c]).filter((x) => x !== null && x !== undefined); return v.length > 0 && v.every((x) => typeof x === "number"); };
  function isDateish(rows, c) {
    if (isNumCol(rows, c)) return false;
    const vals = rows.map((r) => r[c]).filter((x) => x !== null && x !== undefined).slice(0, 50).map(String);
    if (!vals.length || vals.filter((s) => DATE_RE.test(s)).length / vals.length < 0.9) return false;
    return vals.filter((s) => toTime(s) !== null).length / vals.length >= 0.9;
  }
  function detectColumns(rows, columns, x = null, y = [], group = null) {
    if (x === null || x === undefined) { x = columns.find((c) => isDateish(rows, c)) ?? null; if (x === null) x = columns.find((c) => !isNumCol(rows, c)) ?? null; }
    if (!y || !y.length) y = columns.filter((c) => c !== x && c !== group && isNumCol(rows, c) && !/(^|_)(id|year|week|month|day)$/.test(String(c).toLowerCase()));
    if ((group === null || group === undefined) && x !== null) {
      for (const c of columns) {
        if (c === x || y.includes(c) || isNumCol(rows, c)) continue;
        const nun = new Set(rows.map((r) => r[c]).filter((v) => v !== null && v !== undefined)).size;
        if (nun >= 2 && nun <= 12 && new Set(rows.map((r) => r[x])).size < rows.length) { group = c; break; }
      }
    }
    return { x, y: [...y], group: group ?? null, is_time: !!(x !== null && isDateish(rows, x)) };
  }
  function inferFrequency(times) {
    const u = [...new Set(times)].sort((a, b) => a - b);
    const d = u.slice(1).map((t, i) => (t - u[i]) / 864e5);
    if (!d.length) return "unknown";
    const med = CAF.lib.stats.median(d);
    for (const [name, lo, hi] of [["daily", 0.5, 1.5], ["weekly", 6, 8], ["monthly", 27, 32], ["quarterly", 85, 95], ["yearly", 360, 370]]) if (lo <= med && med <= hi) return name;
    return `every ${XP.fixed(med, 0)} days`;
  }

  /* ============================================================ statistics (pure; CAF.lib.stats.changePoints/madZ differ - see notes) */
  const median = (a) => CAF.lib.stats.median(a);
  /** Robust noise scale from first differences (insensitive to level shifts and trends). */
  function noiseSigma(y) {
    const d = y.slice(1).map((v, i) => v - y[i]);
    if (!d.length) return 0;
    const md = median(d), mad = median(d.map((v) => Math.abs(v - md)));
    if (mad > 0) return (1.4826 * mad) / Math.SQRT2;
    const mean = d.reduce((s, v) => s + v, 0) / d.length;
    return Math.sqrt(d.reduce((s, v) => s + (v - mean) ** 2, 0) / d.length) / Math.SQRT2;
  }
  /** Binary segmentation on the mean: a split needs gain > 2 sigma^2 ln n AND a shift >= 3 sigma (port of change_points). */
  function changePoints(y, sigma = null, { penaltyFactor = 2, minShiftSigma = 3, minSize = 3 } = {}) {
    const n = y.length;
    if (n < 2 * minSize) return [];
    const sd = (() => { const m = y.reduce((s, v) => s + v, 0) / n; return Math.sqrt(y.reduce((s, v) => s + (v - m) ** 2, 0) / n); })();
    sigma = (sigma !== null ? sigma : noiseSigma(y)) || sd || 1;
    const penalty = penaltyFactor * sigma ** 2 * Math.log(n);
    const cs = [0], cs2 = [0];
    for (const v of y) { cs.push(cs[cs.length - 1] + v); cs2.push(cs2[cs2.length - 1] + v * v); }
    const sse = (i, j) => { const s = cs[j] - cs[i]; return cs2[j] - cs2[i] - (s * s) / (j - i); };
    const found = [], stack = [[0, n]];
    while (stack.length) {
      const [i, j] = stack.pop();
      if (j - i < 2 * minSize) continue;
      const base = sse(i, j);
      let best = 0, bestK = null, means = [0, 0];
      for (let k = i + minSize; k <= j - minSize; k++) {
        const left = (cs[k] - cs[i]) / (k - i), right = (cs[j] - cs[k]) / (j - k);
        if (Math.abs(right - left) < minShiftSigma * sigma) continue;
        const gain = base - sse(i, k) - sse(k, j);
        if (gain > best) { best = gain; bestK = k; means = [left, right]; }
      }
      if (bestK !== null && best > penalty) {
        found.push({ index: bestK, mean_before: means[0], mean_after: means[1], shift: means[1] - means[0], shift_pct: means[0] ? means[1] / means[0] - 1 : null, gain: best, penalty,
          shift_sigma: Math.abs(means[1] - means[0]) / sigma });
        stack.push([i, bestK], [bestK, j]);
      }
    }
    return found.sort((a, b) => a.index - b.index);
  }
  const segmentFit = (y, cps) => { const b = [0, ...cps.map((c) => c.index), y.length], fit = new Array(y.length); for (let s = 0; s + 1 < b.length; s++) { const seg = y.slice(b[s], b[s + 1]); const m = seg.reduce((a, v) => a + v, 0) / seg.length; for (let i = b[s]; i < b[s + 1]; i++) fit[i] = m; } return fit; };
  /** Robust z (Iglewicz & Hoaglin), with the MeanAD fallback when MAD is 0 (lib.madZ returns zeros there). */
  function madZ(r) {
    const med = median(r), mad = median(r.map((v) => Math.abs(v - med)));
    if (mad === 0) { const meanAd = r.reduce((s, v) => s + Math.abs(v - med), 0) / r.length; return meanAd > 0 ? r.map((v) => (v - med) / (1.253314 * meanAd)) : r.map(() => 0); }
    return r.map((v) => (0.6745 * (v - med)) / mad);
  }
  /** pandas rolling(5, center=True, min_periods=1).median(). */
  const rollingMedian = (y, w = 5) => y.map((_, i) => median(y.slice(Math.max(0, i - Math.floor(w / 2)), Math.min(y.length, i + Math.floor(w / 2) + 1))));
  function seriesFacts(y, frequency = "unknown") {
    const n = y.length;
    const out = { n, first: n ? y[0] : null, last: n ? y[n - 1] : null, min: n ? Math.min(...y) : null, max: n ? Math.max(...y) : null, mean: n ? y.reduce((s, v) => s + v, 0) / n : null,
      total: n ? y.reduce((s, v) => s + v, 0) : null, argmin: n ? y.indexOf(Math.min(...y)) : null, argmax: n ? y.indexOf(Math.max(...y)) : null };
    if (n < 3) return { ...out, trend: null, change_points: [], outliers: [], seasonality: null, sigma: null };
    const sigma = noiseSigma(y);
    const med = rollingMedian(y);
    const clean = y.map((v, i) => (Math.abs(v - med[i]) > 2 * OUTLIER_Z * Math.max(sigma, 1e-9) ? med[i] : v));
    const cps = changePoints(clean, sigma);
    const fit = segmentFit(clean, cps);
    const z = madZ(y.map((v, i) => v - fit[i]));
    const outliers = z.map((zz, i) => [zz, i]).filter(([zz]) => Math.abs(zz) > OUTLIER_Z).map(([zz, i]) => ({ index: i, value: y[i], expected: fit[i], z: zz }));
    const keep = z.map((zz) => Math.abs(zz) <= OUTLIER_Z);
    const xs = y.map((_, i) => i);
    const kx = xs.filter((_, i) => keep[i]), ky = y.filter((_, i) => keep[i]);
    const lr = kx.length >= 3 ? CAF.lib.stats.linreg(kx, ky) : CAF.lib.stats.linreg(xs, y);
    const f0 = lr.intercept, f1 = lr.intercept + lr.slope * (n - 1);
    const trend = { slope_per_period: lr.slope, p_value: lr.p_value, r_squared: lr.r2, fitted_first: f0, fitted_last: f1, pct_change: f0 ? f1 / f0 - 1 : null,
      direction: lr.slope > 0 && lr.p_value < 0.05 ? "up" : lr.slope < 0 && lr.p_value < 0.05 ? "down" : "flat", significant: lr.p_value < 0.05, points_used: keep.filter(Boolean).length };
    if (cps.length) {
      const b = [0, ...cps.map((c) => c.index)], e = [...cps.map((c) => c.index), n];
      trend.within_segment_slopes = b.map((a, s) => { const sx = [], sy = []; for (let i = a; i < e[s]; i++) if (keep[i]) { sx.push(i); sy.push(y[i]); } return sx.length >= 3 ? CAF.lib.stats.linreg(sx, sy).slope : null; });
    }
    let resid = y.map((v, i) => (keep[i] ? v : fit[i]) - fit[i]);
    if (!cps.length) resid = y.map((v, i) => (keep[i] ? v : fit[i]) - (lr.intercept + lr.slope * i));
    const season = { lags_tested: [], acf: {}, bound: 1.96 / Math.sqrt(n), detected: false, lag: null };
    for (const lag of SEASONAL_LAGS[frequency] || [2, 3, 4]) {
      if (n < 2 * lag + 2) continue;
      const m = resid.reduce((s, v) => s + v, 0) / n, r0 = resid.map((v) => v - m);
      const denom = r0.reduce((s, v) => s + v * v, 0);
      let num = 0;
      for (let i = lag; i < n; i++) num += r0[i] * r0[i - lag];
      season.lags_tested.push(lag);
      season.acf[String(lag)] = XP.round(denom > 0 ? num / denom : 0, 4);
    }
    const strongest = Object.entries(season.acf).reduce((a, b) => (a === null || b[1] > a[1] ? b : a), null);
    if (strongest && strongest[1] > Math.max(0.3, season.bound)) Object.assign(season, { detected: true, lag: +strongest[0], strength: strongest[1] });
    season.too_short_for = (SEASONAL_LAGS[frequency] || []).filter((lag) => n < 2 * lag + 2);
    return { ...out, sigma, trend, change_points: cps, outliers, seasonality: season };
  }
  /** Share of total by group, each group's share of the first-to-last change and of each level shift. */
  function contributions(rows, xKey, y, group, cps, xs) {
    const groups = [...new Set(rows.map((r) => String(r[group])))].sort();
    const piv = xs.map((t) => Object.fromEntries(groups.map((g) => [g, 0])));
    const pos = new Map(xs.map((t, i) => [t, i]));
    for (const r of rows) { const i = pos.get(r[xKey]); if (i !== undefined && typeof r[y] === "number") piv[i][String(r[group])] += r[y]; }
    const total = piv.map((row) => groups.reduce((s, g) => s + row[g], 0));
    const grand = Math.max(total.reduce((s, v) => s + v, 0), 1e-12);
    const share = groups.map((g) => [g, piv.reduce((s, row) => s + row[g], 0) / grand]).sort((a, b) => b[1] - a[1]);
    const first = piv[0], last = piv[piv.length - 1];
    const dTotal = total[total.length - 1] - total[0];
    const change = groups.map((g) => ({ group: g, first: first[g], last: last[g], change: last[g] - first[g], share_of_change: dTotal ? (last[g] - first[g]) / dTotal : null })).sort((a, b) => Math.abs(b.change) - Math.abs(a.change));
    const shifts = [];
    for (const c of cps || []) {
      const k = c.index;
      if (!(k > 0 && k < piv.length)) continue;
      const mean = (rs, g) => rs.reduce((s, row) => s + row[g], 0) / rs.length;
      const before = Object.fromEntries(groups.map((g) => [g, mean(piv.slice(0, k), g)])), after = Object.fromEntries(groups.map((g) => [g, mean(piv.slice(k), g)]));
      const dt = groups.reduce((s, g) => s + after[g], 0) - groups.reduce((s, g) => s + before[g], 0);
      const parts = groups.map((g) => ({ group: g, change: after[g] - before[g], share_of_shift: dt ? (after[g] - before[g]) / dt : null })).sort((a, b) => Math.abs(b.change) - Math.abs(a.change));
      shifts.push({ at: new Date(xs[k]).toISOString().slice(0, 10), total_change: dt, parts });
    }
    return { share_of_total: Object.fromEntries(share), first_to_last: change, total_first: total[0], total_last: total[total.length - 1], total_change: dTotal, shifts };
  }
  function categoryFacts(rows, x, y) {
    const sums = new Map();
    for (const r of rows) { const k = String(r[x]); if (typeof r[y] === "number") sums.set(k, (sums.get(k) || 0) + r[y]); else if (!sums.has(k)) sums.set(k, 0); }
    const s = [...sums.entries()].sort((a, b) => b[1] - a[1]);
    const total = s.reduce((a, [, v]) => a + v, 0);
    const z = s.length >= 4 ? madZ(s.map(([, v]) => v)) : s.map(() => 0);
    const topN = Math.max(1, Math.ceil(s.length * 0.2));
    return { n: s.length, top: s.slice(0, 5).map(([k, v]) => ({ category: k, value: v, share: total ? v / total : null })), bottom: s.slice(-3).map(([k, v]) => ({ category: k, value: v })), total,
      top_20pct_share: total ? s.slice(0, topN).reduce((a, [, v]) => a + v, 0) / total : null, outliers: s.map(([k, v], i) => ({ category: k, value: v, z: z[i] })).filter((o) => Math.abs(o.z) > OUTLIER_Z) };
  }

  /* ============================================================ text (pure) */
  function fmtNum(v) {
    if (v === null || v === undefined || Number.isNaN(v)) return "n/a";
    const a = Math.abs(v);
    if (a >= 1e9) return `${XP.fmt(v / 1e9, 2)} billion`;
    if (a >= 1e6) return `${XP.fmt(v / 1e6, 1)} million`;
    if (a >= 100 || Number.isInteger(v)) return XP.fmt(v, 0);
    if (a >= 10) return XP.fmt(v, 1);
    return XP.fmt(v, 2);
  }
  const fmtP = (p) => (p < 0.0001 ? "p < 0.0001" : `p = ${XP.fixed(p, 4)}`);
  const sp = (x, d) => XP.fmt(x, d, { pct: true, sign: true, comma: false });
  const pc = (x, d) => XP.fmt(x, d, { pct: true, comma: false });
  const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);
  function dateLabel(t, frequency) {
    const d = new Date(t);
    if (frequency === "monthly") return `${MON[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
    if (frequency === "quarterly") return `Q${Math.floor(d.getUTCMonth() / 3) + 1} ${d.getUTCFullYear()}`;
    if (frequency === "yearly") return String(d.getUTCFullYear());
    return `${d.getUTCDate()} ${MON[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
  }
  function factLines(meta, facts) {
    const L = [`Chart: ${meta.title} - ${meta.n_points} ${meta.is_time ? meta.frequency : "categories"} points` + (meta.is_time ? ` from ${meta.start} to ${meta.end}` : "")
      + (meta.partial_excluded ? ` (excluded incomplete period(s): ${meta.partial_excluded.map((v) => dateLabel(toTime(v), meta.frequency)).join(", ")})` : "")];
    const labels = meta.labels;
    for (const [y, f] of Object.entries(facts.series)) {
      if (f.trend === null && !("top" in f)) continue;
      if ("top" in f) { L.push(`${y}: total ${fmtNum(f.total)}; highest ${f.top[0].category} ${fmtNum(f.top[0].value)} (${pc(f.top[0].share, 0)} of total); lowest ${f.bottom[f.bottom.length - 1].category} ${fmtNum(f.bottom[f.bottom.length - 1].value)}`); continue; }
      const t = f.trend;
      L.push(`${y}: from ${fmtNum(f.first)} (${labels[0]}) to ${fmtNum(f.last)} (${labels[labels.length - 1]}); range ${fmtNum(f.min)} (${labels[f.argmin]}) to ${fmtNum(f.max)} (${labels[f.argmax]}); mean ${fmtNum(f.mean)}`);
      L.push(t.pct_change !== null ? `${y} trend: ${t.direction} - slope ${fmtNum(t.slope_per_period)} per period, ${fmtP(t.p_value)}, R-squared ${XP.fixed(t.r_squared, 2)}, fitted change ${sp(t.pct_change, 1)} over the period` : `${y} trend: ${t.direction}`);
      for (const c of f.change_points) L.push(c.shift_pct !== null ? `${y} change point at ${labels[c.index]}: mean ${fmtNum(c.mean_before)} before, ${fmtNum(c.mean_after)} after (${sp(c.shift_pct, 0)})` : `${y} change point at ${labels[c.index]}`);
      for (const o of f.outliers) L.push(`${y} outlier at ${labels[o.index]}: ${fmtNum(o.value)} vs about ${fmtNum(o.expected)} expected (robust z = ${XP.fixed(o.z, 1)})`);
      const s = f.seasonality;
      if (s && s.lags_tested.length) L.push(`${y} seasonality: ` + (s.detected ? `repeating pattern every ${s.lag} periods (autocorrelation ${XP.fixed(s.strength, 2)})` : `none detected at lags ${s.lags_tested.join(", ")}`)
        + (s.too_short_for && s.too_short_for.length ? `; series too short to test lag ${s.too_short_for.join(", ")}` : ""));
    }
    const c = facts.contributions;
    if (c) {
      L.push(`Total ${fmtNum(c.total_first)} to ${fmtNum(c.total_last)} (${fmtNum(c.total_change)}); largest contributions: ` + c.first_to_last.slice(0, 3).map((p) => `${p.group} ${fmtNum(p.change)}` + (p.share_of_change !== null ? ` (${pc(p.share_of_change, 0)} of the change)` : "")).join("; "));
      L.push("Share of total: " + Object.entries(c.share_of_total).slice(0, 6).map(([g, v]) => `${g} ${pc(v, 0)}`).join(", "));
      for (const sh of c.shifts) L.push(`Shift at ${sh.at}: total ${fmtNum(sh.total_change)}, led by ` + sh.parts.slice(0, 3).map((p) => `${p.group} ${fmtNum(p.change)}`).join(", "));
    }
    return L;
  }
  function deterministicNarrative(meta, facts) {
    const labels = meta.labels, y0 = meta.y[0], f = facts.series[y0], nice = y0.replace(/_/g, " ");
    if ("top" in f) {
      const top = f.top[0], bottom = f.bottom[f.bottom.length - 1];
      const headline = `${top.category} leads ${nice} with ${fmtNum(top.value)} (${pc(top.share, 0)} of the total).`;
      const commentary = `Across ${f.n} ${meta.x.replace(/_/g, " ")} values the total is ${fmtNum(f.total)}. ${top.category} is highest and ${bottom.category} lowest (${fmtNum(bottom.value)}).`;
      const long = `Bar chart of ${nice} by ${meta.x.replace(/_/g, " ")} for ${f.n} categories, sorted from highest to lowest. Values range from ${fmtNum(bottom.value)} (${bottom.category}) to ${fmtNum(top.value)} (${top.category}). ${commentary}`;
      return { headline, commentary, long_description: long, alt_text: T().truncate(`Bar chart: ${headline}`, 150), key_points: f.top.slice(0, 3).map((t) => `${t.category}: ${fmtNum(t.value)}`) };
    }
    const t = f.trend, cps = f.change_points, outs = f.outliers;
    const parts = [];
    const biggest = cps.length ? cps.reduce((a, b) => (Math.abs(b.shift) > Math.abs(a.shift) ? b : a)) : null;
    if (biggest) parts.push(`${nice} ${biggest.shift > 0 ? "stepped up" : "dropped"} from about ${fmtNum(biggest.mean_before)} to ${fmtNum(biggest.mean_after)} per ${meta.period_word} from ${labels[biggest.index]} (${sp(biggest.shift_pct, 0)})`);
    else if (t && t.direction !== "flat") parts.push(`${nice} trended ${t.direction} (${sp(t.pct_change, 0)} over the period)`);
    else parts.push(`${nice} was broadly flat`);
    const bigOut = outs.length ? outs.reduce((a, b) => (Math.abs(b.z) > Math.abs(a.z) ? b : a)) : null;
    if (bigOut) parts.push(`with a one-off ${bigOut.value > bigOut.expected ? "spike" : "dip"} to ${fmtNum(bigOut.value)} in the ${meta.period_word} of ${labels[bigOut.index]}`);
    let headline = (parts[0] + (parts.length > 1 ? ", " + parts[1] : "") + ".").replace("..", ".");
    headline = headline[0].toUpperCase() + headline.slice(1);
    const bits = [`Over ${meta.n_points} ${meta.period_word}s (${meta.start} to ${meta.end}), ${nice} moved from ${fmtNum(f.first)} to ${fmtNum(f.last)}.`];
    for (const c of cps) bits.push(`A change point at ${labels[c.index]} moved the average from ${fmtNum(c.mean_before)} to ${fmtNum(c.mean_after)} (${sp(c.shift_pct, 0)}), a shift far larger than the ${meta.period_word}-to-${meta.period_word} noise.`);
    for (const o of outs) bits.push(`The ${meta.period_word} of ${labels[o.index]} is an outlier at ${fmtNum(o.value)} against about ${fmtNum(o.expected)} expected (robust z ${XP.fixed(o.z, 1)}); treat it as a one-off, not a new level.`);
    if (t) bits.push(`The linear trend is ${t.direction} (${sp(t.pct_change, 0)} fitted change, ${fmtP(t.p_value)})` + (cps.length ? ", mostly explained by the step change." : "."));
    const s = f.seasonality;
    if (s && s.detected) bits.push(`There is a repeating pattern every ${s.lag} ${meta.period_word}s (autocorrelation ${XP.fixed(s.strength, 2)}).`);
    const c = facts.contributions;
    if (c && c.first_to_last.length) { const lead = c.first_to_last[0]; bits.push(`${cap(lead.group)} contributed most to the change (${fmtNum(lead.change)}` + (lead.share_of_change !== null ? `, ${pc(lead.share_of_change, 0)} of it` : "") + ")."); }
    const commentary = bits.join(" ");
    const other = meta.y.slice(1).filter((yy) => (facts.series[yy] || {}).trend);
    const long = `${meta.is_time ? "Line" : "Bar"} chart titled '${meta.title}'. The horizontal axis shows ${meta.x.replace(/_/g, " ")} from ${meta.start} to ${meta.end} (${meta.n_points} ${meta.period_word}s); the vertical axis shows ${nice}, ranging from ${fmtNum(f.min)} (${labels[f.argmin]}) to ${fmtNum(f.max)} (${labels[f.argmax]}). ` + bits.slice(1).join(" ")
      + (other.length ? ` The chart also shows ${other.map((o) => o.replace(/_/g, " ")).join(", ")}.` : "");
    let alt = `${meta.is_time ? "Line" : "Bar"} chart of ${nice}, ${meta.start} to ${meta.end}`;
    if (biggest) alt += `: ${biggest.shift > 0 ? "up" : "down"} from about ${fmtNum(biggest.mean_before)} to ${fmtNum(biggest.mean_after)} from ${labels[biggest.index]}`;
    else if (t) alt += `: trend ${t.direction} (${sp(t.pct_change, 0)})`;
    if (bigOut) alt += `; ${bigOut.value > bigOut.expected ? "spike" : "dip"} to ${fmtNum(bigOut.value)} on ${labels[bigOut.index]}`;
    return { headline, commentary, long_description: long, alt_text: T().truncate(alt + ".", 150), key_points: bits.slice(1, 5) };
  }
  const numbersOf = (obj) => {
    const out = [];
    const rec = (o) => { if (Array.isArray(o)) o.forEach(rec); else if (U.isObj(o)) Object.values(o).forEach(rec); else if (typeof o === "number" && Number.isFinite(o)) { out.push(o); if (Math.abs(o) >= 1e6) out.push(o / 1e6, o / 1e9); } };
    rec(obj);
    return out;
  };
  const jsonable = (o) => (Array.isArray(o) ? o.map(jsonable) : U.isObj(o) ? Object.fromEntries(Object.entries(o).map(([k, v]) => [k, jsonable(v)])) : typeof o === "number" && !Number.isFinite(o) ? null : o);
  const esc = (s) => String(s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

  function renderChart(rows, meta, facts, themed) {
    if (meta.is_time) {
      if (meta.group) {
        const groups = [...new Set(rows.map((r) => String(r[meta.group])))].sort().slice(0, 8);
        const pos = new Map(meta.times.map((t, i) => [t, i]));
        const series = groups.map((g) => { const v = meta.times.map(() => null); for (const r of rows) if (String(r[meta.group]) === g && pos.has(r._t) && typeof r[meta.y[0]] === "number") v[pos.get(r._t)] = (v[pos.get(r._t)] || 0) + r[meta.y[0]]; return { name: g, values: v }; });
        return CAF.lib.chart.line({ series, labels: meta.labels, title: T().truncate(meta.title, 90), themed, yLabel: meta.y[0].replace(/_/g, " ") });
      }
      const y = meta.y[0], f = facts.series[y], vals = meta.series_values[y];
      const series = [{ name: y.replace(/_/g, " "), values: vals, points: (f.outliers || []).map((o) => o.index) }];
      if ((f.change_points || []).length) {
        const b = [0, ...f.change_points.map((c) => c.index), vals.length], outIdx = new Set(f.outliers.map((o) => o.index)), seg = vals.map(() => null);
        for (let s = 0; s + 1 < b.length; s++) { const pts = vals.slice(b[s], b[s + 1]).filter((_, i) => !outIdx.has(b[s] + i)); const m = (pts.length ? pts : vals.slice(b[s], b[s + 1])).reduce((a, v) => a + v, 0) / (pts.length || b[s + 1] - b[s]); for (let i = b[s]; i < b[s + 1]; i++) seg[i] = m; }
        series.push({ name: "segment mean", values: seg });
      }
      return CAF.lib.chart.line({ series, labels: meta.labels, title: T().truncate(meta.title, 90), themed, yLabel: y.replace(/_/g, " "), markers: (f.change_points || []).map((c) => ({ index: c.index, label: "change point" })) });
    }
    const y = meta.y[0], sums = new Map();
    for (const r of rows) { const k = String(r[meta.x]); sums.set(k, (sums.get(k) || 0) + (typeof r[y] === "number" ? r[y] : 0)); }
    return CAF.lib.chart.bars({ items: [...sums.entries()].sort((a, b) => b[1] - a[1]).slice(0, 20).map(([label, value]) => ({ label, value })), title: T().truncate(meta.title, 90), themed });
  }

  CAF.agent("chart_summary", {
    async run(input, ctx) {
      if (!XP.generateOrNone || !XP.pageFacts) throw new Error("The shared experimentation kit (CAF.xp, defined in experiment_ideation.js) did not load");
      if (!XP.unsupportedNumbers) throw new Error("experiment_summary.js (number guard) did not load");
      /* ---- load */
      let rows = null, columns = null, source = "", hints = {};
      const fromRecords = (recs) => { rows = recs.map((r) => ({ ...r })); columns = [...new Set(recs.flatMap((r) => Object.keys(r)))]; };
      if (input.records && input.records.length) { fromRecords(input.records); source = "records"; }
      else if (input.csv_text) { const recs = CAF.lib.csv.parse(input.csv_text); fromRecords(recs); if (!recs.length) rows = []; source = "inline CSV"; }
      else if (input.dataset_path) {
        const p = String(input.dataset_path).replace(/\\/g, "/").replace(/^\.?\//, "");
        if (p.startsWith("/") || /^[A-Za-z]:/.test(p) || p.split("/").includes("..")) throw new Error("dataset_path must point inside the framework folder or the runs folder");
        const name = p.replace(/^sample_data\//, "").replace(/\.csv$/i, "");
        if (!ctx.hasData(name)) { ctx.degraded("the hosted console reads only the embedded sample datasets - other files need the Python service"); throw new Error(`dataset_path not found: ${input.dataset_path}`); }
        fromRecords(await ctx.data(name)); source = p;
      } else {
        const up = ctx.upstreamOutput("exploration_generation");
        if (up && up.dataset && up.dataset.length) { fromRecords(up.dataset); columns = up.columns && up.columns.length ? [...up.columns] : columns; source = "upstream exploration_generation dataset"; hints = up.dataset_hints || {}; }
      }
      if (rows === null) {
        if (input.image && input.image.data_base64) {
          const n = { headline: "Image-only chart: no data was supplied, so nothing could be computed.", commentary: "Supply the data behind the chart (CSV or records) for a verified summary; image reading needs a vision model.",
            long_description: "A chart image was provided without its data. No values were extracted.", alt_text: "Chart image (not analysed)", key_points: [] };
          ctx.warn("Vision path: values are read from pixels by a model and are estimates; supply the data for exact figures.");
          ctx.degraded("image-only input: reading values from a chart image is left to the Python service's vision path - supply the data behind the chart");
          return { output: { title: input.title || "Chart", source: "vision", is_estimate: true, headline: n.headline, commentary: n.commentary, long_description: n.long_description, alt_text: n.alt_text, key_points: [], narrative_source: "vision" },
            summary: `Image-only chart summarised by the vision path (estimates): ${T().truncate(n.headline, 100)}` };
        }
        throw new Error("Provide data (dataset_path, csv_text, records or an upstream exploration) or a chart image");
      }
      if (!rows.length) throw new Error("The dataset has no rows");
      /* ---- analyse (all statistics in code) */
      const yHint = Array.isArray(hints.y) ? hints.y : hints.y ? [hints.y] : [];
      const cols = detectColumns(rows, columns, input.x || hints.x || null, input.y && input.y.length ? input.y : yHint, input.group || hints.group || null);
      if (!cols.y.length) throw new Error(`No numeric value column found in ${JSON.stringify(columns)}`);
      const x = cols.x, group = cols.group;
      const facts = { series: {} };
      const meta = { x: x || "index", y: cols.y, group, is_time: cols.is_time, title: input.title || cols.y.map((c) => c.replace(/_/g, " ")).join(", ") + (x ? ` by ${x.replace(/_/g, " ")}` : "") };
      if (cols.is_time) {
        for (const r of rows) r._t = toTime(r[x]);
        rows = rows.filter((r) => r._t !== null);
        const partial = new Set((input.partial_periods && input.partial_periods.length ? input.partial_periods : hints.partial_periods || []).filter((v) => String(v).trim()).map(toTime).filter((t) => t !== null));
        if (partial.size) {
          const dropped = rows.filter((r) => partial.has(r._t));
          if (dropped.length && rows.length - dropped.length >= 3) {
            const mp = [...new Set(dropped.map((r) => new Date(r._t).toISOString().slice(0, 10)))].sort();
            rows = rows.filter((r) => !partial.has(r._t));
            ctx.check({ rule: "chart.partial_periods", status: "info", severity: "low", message: `Excluded partial period(s) from the statistics: ${mp.join(", ")}.` });
            meta.partial_excluded = mp;
          }
        }
        const times = [...new Set(rows.map((r) => r._t))].sort((a, b) => a - b);
        const pos = new Map(times.map((t, i) => [t, i]));
        const agg = Object.fromEntries(cols.y.map((y) => [y, times.map(() => 0)]));
        for (const r of rows) for (const y of cols.y) if (typeof r[y] === "number") agg[y][pos.get(r._t)] += r[y];
        const freq = inferFrequency(times);
        const labels = times.map((t) => dateLabel(t, freq));
        Object.assign(meta, { frequency: freq, period_word: PERIOD_WORD[freq] || "period", n_points: times.length, labels, start: labels[0], end: labels[labels.length - 1],
          x_values: times.map((t) => new Date(t).toISOString().slice(0, 19)), series_values: agg, times });
        for (const y of cols.y.slice(0, 3)) facts.series[y] = seriesFacts(agg[y], freq);
        if (group) facts.contributions = contributions(rows, "_t", cols.y[0], group, facts.series[cols.y[0]].change_points, times);
      } else {
        const xcol = x || columns[0];
        Object.assign(meta, { frequency: null, period_word: "category", n_points: new Set(rows.map((r) => r[xcol])).size, labels: rows.map((r) => String(r[xcol])), start: "", end: "", x_values: rows.map((r) => String(r[xcol])), series_values: {} });
        for (const y of cols.y.slice(0, 3)) facts.series[y] = categoryFacts(rows, xcol, y);
      }
      const lines = factLines(meta, facts);
      const findings = [];
      for (const [y, f] of Object.entries(facts.series)) {
        for (const c of f.change_points || []) findings.push({ title: `${y}: level shift at ${meta.labels[c.index]}`, severity: "medium", category: "analytics", evidence: "binary segmentation, BIC-style penalty", recommendation: "",
          detail: `mean ${fmtNum(c.mean_before)} -> ${fmtNum(c.mean_after)}` + (c.shift_pct !== null ? ` (${sp(c.shift_pct, 0)})` : ""), target: y, source_agent: "chart_summary" });
        for (const o of Array.isArray(f.outliers) ? f.outliers : []) findings.push({ title: `${y}: outlier at ${"index" in o ? meta.labels[o.index] : o.category}`, severity: "low", category: "analytics", evidence: "MAD z-score > 3.5", recommendation: "",
          detail: `${fmtNum(o.value)}` + ("expected" in o ? ` vs about ${fmtNum(o.expected)} expected` : "") + ` (robust z ${XP.fixed(o.z, 1)})`, target: y, source_agent: "chart_summary" });
        const t = f.trend;
        if (t && t.significant) findings.push({ title: `${y}: ${t.direction}ward trend`, severity: "low", category: "analytics", evidence: "", recommendation: "", detail: `${sp(t.pct_change, 1)} fitted change, p = ${XP.fixed(t.p_value, 4)}`, target: y, source_agent: "chart_summary" });
        const s = f.seasonality;
        if (s && s.detected) findings.push({ title: `${y}: repeating pattern every ${s.lag} periods`, severity: "low", category: "analytics", evidence: "", recommendation: "", detail: `autocorrelation ${XP.fixed(s.strength, 2)}`, target: y, source_agent: "chart_summary" });
      }
      ctx.check({ rule: "chart.columns", status: "pass", severity: "low", message: `x = ${meta.x}${meta.is_time ? ` (time, ${meta.frequency})` : ""}, y = ${cols.y.join(", ")}` + (group ? `, group = ${group}` : "") });
      if (meta.is_time && meta.n_points < 8) ctx.check({ rule: "chart.length", status: "warn", severity: "medium", message: `Only ${meta.n_points} points; trend and change-point tests have little power.` });
      /* ---- narrate (model phrases computed facts; uncomputed numbers -> deterministic text) */
      const det = deterministicNarrative(meta, facts);
      let model = await XP.generateOrNone(ctx, "narrative", { title: meta.title, fact_lines: lines, audience: input.audience, chart_type: meta.is_time ? "line chart" : "bar chart" },
        "no language model configured: commentary written by deterministic templates from the computed facts");
      let narrativeSource = "model";
      if (model) {
        const nums = [...Object.values(facts.series).flatMap(numbersOf), ...numbersOf(facts.contributions || {})];
        const tokens = new Set(lines.join(" ").match(/\d+(?:\.\d+)?/g) || []);
        const bad = XP.unsupportedNumbers([model.headline, model.commentary, model.long_description, model.alt_text, ...(model.key_points || [])].join(" "), nums, tokens);
        if (bad.length) { ctx.warn(`Model commentary used numbers that were not computed (${bad.slice(0, 5).join(", ")}); deterministic commentary used instead.`); model = null; }
        else if (String(model.alt_text || "").length > 150) model.alt_text = T().truncate(model.alt_text, 150);
      }
      if (!model) { model = det; narrativeSource = "deterministic"; }
      const n = { headline: model.headline, commentary: model.commentary, long_description: model.long_description, alt_text: model.alt_text, key_points: (model.key_points || []).slice(0, 5) };
      /* ---- render */
      let chart = null, viewSvg = null;
      try {
        const svg = renderChart(rows, meta, facts, false);
        ctx.artifact({ name: "chart.svg", mime: "image/svg+xml", content: svg, description: `Chart: ${meta.title}` });
        chart = "chart.svg";
        viewSvg = renderChart(rows, meta, facts, true);
      } catch (e) { ctx.log(`chart render failed: ${e.message}`); }
      const html = `<figure>\n  <img src="chart.svg" alt="${esc(n.alt_text)}" aria-describedby="chart-desc">\n  <figcaption>${esc(meta.title)}</figcaption>\n</figure>\n<details>\n  <summary>Chart description</summary>\n  <div id="chart-desc"><p>${esc(n.long_description)}</p></div>\n</details>\n`;
      const md = [`# ${meta.title}`, "", `**${n.headline}**`, "", n.commentary, "", "## Key points", ...n.key_points.map((p) => `- ${p}`), "", "## Long description (WCAG 1.1.1)", n.long_description, "", `Alt text: ${n.alt_text}`, "", "## Computed facts", ...lines.map((l) => `- ${l}`)].join("\n") + "\n";
      ctx.artifact({ name: "chart_summary.md", mime: "text/markdown", content: md, description: "Chart commentary and long description" });
      ctx.artifact({ name: "chart_description.html", mime: "text/html", content: html, description: "Accessible figure markup (alt text + long description)" });
      const output = { title: meta.title, source: "data", is_estimate: false, x: meta.x, y: meta.y, group: meta.group, frequency: meta.frequency ?? null, n_points: meta.n_points, period: { start: meta.start || "", end: meta.end || "" },
        facts: jsonable(facts), fact_lines: lines, findings, headline: n.headline, commentary: n.commentary, long_description: n.long_description, alt_text: n.alt_text, key_points: n.key_points, narrative_source: narrativeSource, chart };
      const view = [];
      if (viewSvg) view.push({ type: "svg", title: meta.title, svg: viewSvg });
      if (meta.is_time && !group) for (const y of meta.y.slice(1, 3)) view.push({ type: "svg", title: y.replace(/_/g, " "), svg: CAF.lib.chart.line({ series: [{ name: y, values: meta.series_values[y], points: ((facts.series[y] || {}).outliers || []).map((o) => o.index) }], labels: meta.labels, title: y.replace(/_/g, " "), themed: true }) });
      view.push({ type: "markdown", title: "Commentary", markdown: `**${n.headline}**\n\n${n.commentary}\n\n*Alt text:* ${n.alt_text}` }, { type: "list", title: "Computed facts", items: lines }, { type: "markdown", title: "Long description (WCAG 1.1.1)", markdown: n.long_description });
      return { output, summary: T().truncate(n.headline, 220), view };
    },
    selftest() { return { dataset_path: "sample_data/charts/weekly_quotes.csv", title: "Weekly quotes and purchases, 2026" }; },
  });
  XP.chart = { detectColumns, inferFrequency, noiseSigma, changePoints, madZ, seriesFacts, contributions, categoryFacts, fmtNum };
})();
