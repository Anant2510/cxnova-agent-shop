/* Deterministic libraries for browser agents: text, stats, csv, html, color, charts, email, qr, markdown.
   These mirror the Python implementations (caf.core.textutil, scipy/statsmodels maths, emailkit). */
CAF.lib = {};

/* ------------------------------------------------------------------------- text */
CAF.lib.text = (() => {
  const STOP = new Set(("a an and are as at be but by for from has have i in is it its of on or our that the their them they this to was we were will with you your " +
    "not can may more most into over than then there these those what when where which who why how all any each about after before also just so if do does did up out new").split(" "));
  const words = (t) => (String(t || "").match(/[A-Za-z0-9][A-Za-z0-9'’-]*/g) || []);
  const tokens = (t) => words(t).map((w) => w.toLowerCase()).filter((w) => w.length > 1 && !STOP.has(w));
  const normQuotes = (t) => String(t || "").replace(/[‘’]/g, "'").replace(/[“”]/g, '"').replace(/[–—]/g, "-").replace(/ /g, " ").replace(/…/g, "...");
  const canonical = (t) => normQuotes(String(t || "").normalize("NFKC")).replace(/\s+/g, " ").trim().toLowerCase();
  const ABBR = /\b(Dr|Mr|Mrs|Ms|Prof|Sr|Jr|St|vs|etc|e\.g|i\.e|Inc|Ltd|Co|No|Fig|approx|U\.S|U\.K)\./g;
  const sentences = (t) => String(t || "").replace(/\s+/g, " ").trim().replace(ABBR, (m, a) => a.replace(/\./g, "\u2024") + "\u2024")
    .split(/(?<=[.!?])\s+(?=[A-Z0-9"'“‘(])/).map((s) => s.replace(/\u2024/g, ".").trim()).filter(Boolean);
  const slugify = (t, max = 60) => String(t || "").normalize("NFKD").replace(/[̀-ͯ]/g, "").replace(/[^A-Za-z0-9]+/g, "-").replace(/^-+|-+$/g, "").toLowerCase().slice(0, max).replace(/-+$/, "") || "item";
  const wordCount = (t) => words(t).length;
  const syllables = (w) => { w = w.toLowerCase().replace(/[^a-z]/g, ""); if (w.length <= 3) return 1; w = w.replace(/(?:[^laeiouy]es|ed|[^laeiouy]e)$/, "").replace(/^y/, ""); const m = w.match(/[aeiouy]{1,2}/g); return Math.max(1, m ? m.length : 1); };
  const flesch = (t) => {
    const s = Math.max(1, sentences(t).length), w = words(t); if (!w.length) return 0;
    const syl = w.reduce((a, x) => a + syllables(x), 0);
    return Math.round((206.835 - 1.015 * (w.length / s) - 84.6 * (syl / w.length)) * 10) / 10;
  };
  const truncate = (t, n, ell = "...") => (t.length <= n ? t : t.slice(0, n - ell.length).replace(/\s+\S*$/, "") + ell);
  const dedupe = (items) => { const seen = new Set(); return items.filter((i) => { const k = canonical(i); if (!k || seen.has(k)) return false; seen.add(k); return true; }); };
  function vectorize(docs, { ngram = 2, char = false } = {}) {
    const grams = docs.map((d) => {
      const toks = char ? [] : tokens(d);
      const out = [...toks];
      if (!char) for (let n = 2; n <= ngram; n++) for (let i = 0; i + n <= toks.length; i++) out.push(toks.slice(i, i + n).join(" "));
      if (char) { const s = ` ${canonical(d)} `; for (let i = 0; i + 3 <= s.length; i++) out.push(s.slice(i, i + 3)); }
      return out;
    });
    const df = new Map();
    grams.forEach((g) => new Set(g).forEach((t) => df.set(t, (df.get(t) || 0) + 1)));
    const N = docs.length;
    return grams.map((g) => {
      const tf = new Map(); g.forEach((t) => tf.set(t, (tf.get(t) || 0) + 1));
      const v = new Map(); let norm = 0;
      tf.forEach((c, t) => { const w = (1 + Math.log(c)) * (Math.log((1 + N) / (1 + df.get(t))) + 1); v.set(t, w); norm += w * w; });
      norm = Math.sqrt(norm) || 1; v.forEach((w, t) => v.set(t, w / norm));
      return v;
    });
  }
  const cosine = (a, b) => { let s = 0; const [x, y] = a.size < b.size ? [a, b] : [b, a]; x.forEach((w, t) => { const o = y.get(t); if (o) s += w * o; }); return s; };
  function similarity(query, docs) {
    if (!docs.length) return [];
    const vecs = vectorize([...docs, query]);
    const q = vecs[vecs.length - 1];
    return vecs.slice(0, -1).map((v) => cosine(q, v));
  }
  /** Average-linkage agglomerative clustering on cosine distance; returns cluster label per doc. */
  function cluster(docs, { threshold = 0.75, char = false } = {}) {
    const vecs = vectorize(docs, { char });
    let clusters = docs.map((_, i) => [i]);
    const dist = (A, B) => { let s = 0; for (const a of A) for (const b of B) s += 1 - cosine(vecs[a], vecs[b]); return s / (A.length * B.length); };
    while (clusters.length > 1) {
      let best = [Infinity, -1, -1];
      for (let i = 0; i < clusters.length; i++) for (let j = i + 1; j < clusters.length; j++) { const d = dist(clusters[i], clusters[j]); if (d < best[0]) best = [d, i, j]; }
      if (best[0] > threshold) break;
      const [, i, j] = best; clusters[i] = clusters[i].concat(clusters[j]); clusters.splice(j, 1);
    }
    const labels = new Array(docs.length);
    clusters.forEach((c, k) => c.forEach((i) => (labels[i] = k)));
    return labels;
  }
  return { STOP, words, tokens, canonical, normQuotes, sentences, slugify, wordCount, syllables, flesch, truncate, dedupe, vectorize, cosine, similarity, cluster };
})();

/* ------------------------------------------------------------------------- stats */
CAF.lib.stats = (() => {
  const sum = (a) => a.reduce((s, x) => s + x, 0);
  const mean = (a) => (a.length ? sum(a) / a.length : NaN);
  const variance = (a, ddof = 1) => { const m = mean(a); return a.length > ddof ? sum(a.map((x) => (x - m) ** 2)) / (a.length - ddof) : NaN; };
  const std = (a, ddof = 1) => Math.sqrt(variance(a, ddof));
  const median = (a) => quantile(a, 0.5);
  function quantile(a, q) { const s = [...a].sort((x, y) => x - y); if (!s.length) return NaN; const pos = (s.length - 1) * q, lo = Math.floor(pos), hi = Math.ceil(pos); return s[lo] + (s[hi] - s[lo]) * (pos - lo); }
  /* Double-precision special functions (scipy agreement to ~1e-14), including far tails. */
  const EPS = Number.EPSILON;
  function gammaSeries(a, x) { let s = 1 / a, t = s; for (let n = 1; n < 1000; n++) { t *= x / (a + n); s += t; if (Math.abs(t) <= Math.abs(s) * EPS) break; } return s * Math.exp(-x + a * Math.log(x) - lgamma(a)); }
  function gammaCF(a, x) { // Lentz continued fraction for the upper tail Q(a, x), x >= a + 1
    let b = x + 1 - a, c = 1e300, d = 1 / b, h = d;
    for (let i = 1; i < 1000; i++) { const an = -i * (i - a); b += 2; d = an * d + b; if (Math.abs(d) < 1e-300) d = 1e-300; c = b + an / c; if (Math.abs(c) < 1e-300) c = 1e-300; d = 1 / d; const del = d * c; h *= del; if (Math.abs(del - 1) <= EPS) break; }
    return Math.exp(-x + a * Math.log(x) - lgamma(a)) * h;
  }
  /** Regularized lower / upper incomplete gamma. */
  const gammaP = (a, x) => (x <= 0 ? 0 : x === Infinity ? 1 : x < a + 1 ? gammaSeries(a, x) : 1 - gammaCF(a, x));
  const gammaQ = (a, x) => (x <= 0 ? 1 : x === Infinity ? 0 : x < a + 1 ? 1 - gammaSeries(a, x) : gammaCF(a, x));
  function erfc(x) { if (Number.isNaN(x)) return NaN; if (x === 0) return 1; const q = gammaQ(0.5, x * x); return x > 0 ? q : 2 - q; }
  const erf = (x) => (Math.abs(x) < 1 ? Math.sign(x) * gammaP(0.5, x * x) : 1 - erfc(x));
  const normCdf = (z) => 0.5 * erfc(-z / Math.SQRT2);
  /** Upper tail 1 - Phi(z) without cancellation (p-values far below 1e-7 stay exact). */
  const normSf = (z) => 0.5 * erfc(z / Math.SQRT2);
  function normPpf(p) { // Acklam's rational approximation plus one Halley step on the exact CDF
    const x = normPpf0(p);
    if (!Number.isFinite(x)) return x;
    const e = (p < 0.5 ? normCdf(x) - p : -(normSf(x) - (1 - p)));
    const u = e * Math.sqrt(2 * Math.PI) * Math.exp((x * x) / 2);
    return x - u / (1 + (x * u) / 2);
  }
  function normPpf0(p) {
    if (p <= 0) return -Infinity; if (p >= 1) return Infinity;
    const a = [-3.969683028665376e1, 2.209460984245205e2, -2.759285104469687e2, 1.38357751867269e2, -3.066479806614716e1, 2.506628277459239];
    const b = [-5.447609879822406e1, 1.615858368580409e2, -1.556989798598866e2, 6.680131188771972e1, -1.328068155288572e1];
    const c = [-7.784894002430293e-3, -3.223964580411365e-1, -2.400758277161838, -2.549732539343734, 4.374664141464968, 2.938163982698783];
    const d = [7.784695709041462e-3, 3.224671290700398e-1, 2.445134137142996, 3.754408661907416];
    const pl = 0.02425; let q, r;
    if (p < pl) { q = Math.sqrt(-2 * Math.log(p)); return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1); }
    if (p > 1 - pl) { q = Math.sqrt(-2 * Math.log(1 - p)); return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1); }
    q = p - 0.5; r = q * q;
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1);
  }
  function lgamma(x) { const g = 7, c = [0.99999999999980993, 676.5203681218851, -1259.1392167224028, 771.32342877765313, -176.61502916214059, 12.507343278686905, -0.13857109526572012, 9.9843695780195716e-6, 1.5056327351493116e-7];
    if (x < 0.5) return Math.log(Math.PI / Math.sin(Math.PI * x)) - lgamma(1 - x); x -= 1; let a = c[0]; const t = x + g + 0.5; for (let i = 1; i < g + 2; i++) a += c[i] / (x + i); return 0.5 * Math.log(2 * Math.PI) + (x + 0.5) * Math.log(t) - t + Math.log(a); }
  /** Chi-square upper tail, exact for tiny p-values (scipy.stats.chi2.sf). */
  const chi2Sf = (x, df) => gammaQ(df / 2, x / 2);
  function betacf(a, b, x) { let qab = a + b, qap = a + 1, qam = a - 1, c = 1, d = 1 - qab * x / qap; if (Math.abs(d) < 1e-300) d = 1e-300; d = 1 / d; let h = d;
    for (let m = 1; m <= 300; m++) { const m2 = 2 * m; let aa = m * (b - m) * x / ((qam + m2) * (a + m2)); d = 1 + aa * d; if (Math.abs(d) < 1e-300) d = 1e-300; c = 1 + aa / c; if (Math.abs(c) < 1e-300) c = 1e-300; d = 1 / d; h *= d * c;
      aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2)); d = 1 + aa * d; if (Math.abs(d) < 1e-300) d = 1e-300; c = 1 + aa / c; if (Math.abs(c) < 1e-300) c = 1e-300; d = 1 / d; const del = d * c; h *= del; if (Math.abs(del - 1) < 1e-14) break; }
    return h; }
  function betaInc(a, b, x) { if (x <= 0) return 0; if (x >= 1) return 1; const bt = Math.exp(lgamma(a + b) - lgamma(a) - lgamma(b) + a * Math.log(x) + b * Math.log(1 - x)); return x < (a + 1) / (a + b + 2) ? bt * betacf(a, b, x) / a : 1 - bt * betacf(b, a, 1 - x) / b; }
  const tSf2 = (t, df) => betaInc(df / 2, 0.5, df / (df + t * t)); // two-sided p-value
  function twoPropZTest(c1, n1, c2, n2) { // control (1) vs variant (2), pooled, two-sided
    const p1 = c1 / n1, p2 = c2 / n2, p = (c1 + c2) / (n1 + n2); const se = Math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2));
    const z = se > 0 ? (p2 - p1) / se : 0; return { z, p_value: Math.min(1, 2 * normSf(Math.abs(z))) };
  }
  function diffCI(c1, n1, c2, n2, alpha = 0.05) { const p1 = c1 / n1, p2 = c2 / n2; const se = Math.sqrt(p1 * (1 - p1) / n1 + p2 * (1 - p2) / n2); const z = normPpf(1 - alpha / 2); return [p2 - p1 - z * se, p2 - p1 + z * se]; }
  const cohensH = (p1, p2) => 2 * Math.asin(Math.sqrt(p2)) - 2 * Math.asin(Math.sqrt(p1));
  /** statsmodels NormalIndPower.solve_power + proportion_effectsize, ratio 1, two-sided: solves the power equation
      with both tails (the textbook closed form counts one tail and comes out slightly high at large n). */
  function sampleSizePerVariant(baseline, mdeRel, alpha = 0.05, power = 0.8, comparisons = 1) {
    const a = alpha / Math.max(1, comparisons);
    const h = Math.abs(cohensH(baseline, Math.min(0.9999, baseline * (1 + mdeRel))));
    if (!h) return Infinity;
    const crit = normPpf(1 - a / 2);
    const powerAt = (n) => { const s = h * Math.sqrt(n / 2); return normSf(crit - s) + normCdf(-crit - s); };
    const closed = 2 * ((crit + normPpf(power)) / h) ** 2; // one-tail closed form: an upper bound on the root
    let lo = Math.max(1e-9, closed * 0.25), hi = closed + 1;
    if (powerAt(lo) >= power) return Math.ceil(lo);
    for (let i = 0; i < 200 && hi - lo > 1e-9; i++) { const mid = (lo + hi) / 2; if (powerAt(mid) < power) lo = mid; else hi = mid; }
    return Math.ceil(hi - 1e-9);
  }
  function mdeForSample(baseline, nPerVariant, alpha = 0.05, power = 0.8, comparisons = 1) {
    let lo = 1e-4, hi = 5;
    for (let i = 0; i < 80; i++) { const mid = (lo + hi) / 2; if (sampleSizePerVariant(baseline, mid, alpha, power, comparisons) > nPerVariant) lo = mid; else hi = mid; }
    return hi;
  }
  function rng(seed = 42) { let s = seed >>> 0; return () => { s += 0x6d2b79f5; let t = s; t = Math.imul(t ^ (t >>> 15), t | 1); t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; }; }
  function gammaSample(k, r) { if (k < 1) return gammaSample(k + 1, r) * Math.pow(r(), 1 / k); const d = k - 1 / 3, c = 1 / Math.sqrt(9 * d);
    for (;;) { let x, v; do { const u1 = r(), u2 = r(); x = Math.sqrt(-2 * Math.log(u1 || 1e-12)) * Math.cos(2 * Math.PI * u2); v = 1 + c * x; } while (v <= 0); v = v * v * v; const u = r(); if (u < 1 - 0.0331 * x ** 4 || Math.log(u) < 0.5 * x * x + d * (1 - v + Math.log(v))) return d * v; } }
  const betaSample = (a, b, r) => { const x = gammaSample(a, r), y = gammaSample(b, r); return x / (x + y); };
  function bayes(c1, n1, c2, n2, draws = 20000, seed = 42) { const r = rng(seed); let wins = 0, loss = 0;
    for (let i = 0; i < draws; i++) { const a = betaSample(1 + c1, 1 + n1 - c1, r), b = betaSample(1 + c2, 1 + n2 - c2, r); if (b > a) wins++; loss += Math.max(0, a - b); }
    return { prob_beat_control: wins / draws, expected_loss: loss / draws }; }
  function chiSquareGOF(observed, expectedShares) { const total = sum(observed); const exp = expectedShares.map((s) => (s / sum(expectedShares)) * total);
    const chi2 = sum(observed.map((o, i) => (o - exp[i]) ** 2 / (exp[i] || 1))); const df = observed.length - 1; return { chi2, df, p_value: chi2Sf(chi2, df) }; }
  function linreg(xs, ys) { const n = xs.length, mx = mean(xs), my = mean(ys); let sxy = 0, sxx = 0, syy = 0;
    for (let i = 0; i < n; i++) { sxy += (xs[i] - mx) * (ys[i] - my); sxx += (xs[i] - mx) ** 2; syy += (ys[i] - my) ** 2; }
    const slope = sxx ? sxy / sxx : 0, intercept = my - slope * mx, r = sxx && syy ? sxy / Math.sqrt(sxx * syy) : 0;
    const df = n - 2; const se = df > 0 && sxx ? Math.sqrt(Math.max(0, (1 - r * r) * syy / df) / sxx) : 0; const t = se ? slope / se : 0;
    return { slope, intercept, r2: r * r, p_value: df > 0 ? tSf2(Math.abs(t), df) : 1, n }; }
  function autocorr(ys, lag) { const n = ys.length; if (lag >= n) return 0; const m = mean(ys); let num = 0, den = 0; for (let i = 0; i < n; i++) den += (ys[i] - m) ** 2; for (let i = lag; i < n; i++) num += (ys[i] - m) * (ys[i - lag] - m); return den ? num / den : 0; }
  /** Simple robust z (0.6745 (y - median) / MAD). Not the Python agents' variant: chart_summary carries its own port. */
  function madZ(ys) { const med = median(ys); const mad = median(ys.map((y) => Math.abs(y - med))); return ys.map((y) => (mad ? 0.6745 * (y - med) / mad : 0)); }
  /** Binary segmentation on the mean with a BIC-style penalty. Returns change-point indices.
      A generic helper, not the Python chart_summary algorithm (that twin carries a port for parity). */
  function changePoints(ys, { minSize = 3, maxCps = 4 } = {}) {
    const n = ys.length; if (n < 2 * minSize) return [];
    const sigma2 = Math.max(variance(ys) || 1e-9, 1e-9); const penalty = 2 * Math.log(n) * sigma2;
    const cost = (a, b) => { const seg = ys.slice(a, b); const m = mean(seg); return sum(seg.map((y) => (y - m) ** 2)); };
    const cps = []; const segs = [[0, n]];
    while (segs.length && cps.length < maxCps) {
      let best = null;
      for (const [a, b] of segs) for (let k = a + minSize; k <= b - minSize; k++) { const gain = cost(a, b) - cost(a, k) - cost(k, b); if (!best || gain > best.gain) best = { gain, k, a, b }; }
      if (!best || best.gain < penalty) break;
      cps.push(best.k); segs.splice(segs.findIndex(([a, b]) => a === best.a && b === best.b), 1, [best.a, best.k], [best.k, best.b]);
    }
    return cps.sort((x, y) => x - y);
  }
  return { sum, mean, variance, std, median, quantile, erf, erfc, gammaP, gammaQ, lgamma, normCdf, normSf, normPpf, chi2Sf, betaInc, tSf2, twoPropZTest, diffCI, cohensH, sampleSizePerVariant, mdeForSample, rng, betaSample, bayes, chiSquareGOF, linreg, autocorr, madZ, changePoints };
})();

/* ------------------------------------------------------------------------- csv */
CAF.lib.csv = {
  parse(text, { coerce = true } = {}) {
    const rows = []; let row = [], field = "", inQ = false; const s = String(text || "").replace(/^﻿/, "");
    for (let i = 0; i < s.length; i++) {
      const ch = s[i];
      if (inQ) { if (ch === '"') { if (s[i + 1] === '"') { field += '"'; i++; } else inQ = false; } else field += ch; }
      else if (ch === '"') inQ = true;
      else if (ch === ",") { row.push(field); field = ""; }
      else if (ch === "\n" || ch === "\r") { if (ch === "\r" && s[i + 1] === "\n") i++; row.push(field); field = ""; if (row.length > 1 || row[0] !== "") rows.push(row); row = []; }
      else field += ch;
    }
    if (field !== "" || row.length) { row.push(field); rows.push(row); }
    if (!rows.length) return [];
    const head = rows[0].map((h) => h.trim());
    return rows.slice(1).map((r) => {
      const o = {};
      head.forEach((h, i) => { let v = r[i] ?? ""; if (coerce) { if (v === "") v = null; else if (/^-?\d+(\.\d+)?(e-?\d+)?$/i.test(v)) v = Number(v); } o[h] = v; });
      return o;
    });
  },
  stringify(rows, columns) {
    const cols = columns || (rows[0] ? Object.keys(rows[0]) : []);
    const q = (v) => { if (v === null || v === undefined) return ""; const s = typeof v === "object" ? JSON.stringify(v) : String(v); return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s; };
    return [cols.join(","), ...rows.map((r) => cols.map((c) => q(r[c])).join(","))].join("\n") + "\n";
  },
};

/* ------------------------------------------------------------------------- html */
CAF.lib.html = {
  parse(html) { return new DOMParser().parseFromString(String(html || ""), "text/html"); },
  text(el) { return (el && el.textContent ? el.textContent : "").replace(/\s+/g, " ").trim(); },
  jsonld(doc) {
    const out = [];
    for (const s of doc.querySelectorAll('script[type="application/ld+json"]')) {
      try {
        const data = JSON.parse(s.textContent);
        const items = Array.isArray(data) ? data : data["@graph"] ? data["@graph"] : [data];
        for (const it of items) out.push({ type: [].concat(it["@type"] || "Thing").join(","), data: it, error: null });
      } catch (e) { out.push({ type: "invalid", data: null, error: String(e.message || e) }); }
    }
    return out;
  },
  meta(doc) {
    const q = (sel, attr = "content") => { const e = doc.querySelector(sel); return e ? (e.getAttribute(attr) || "").trim() : null; };
    return { title: (doc.querySelector("title") || {}).textContent?.trim() || null, description: q('meta[name="description"]'), canonical: q('link[rel="canonical"]', "href"),
      robots: q('meta[name="robots"]'), lang: doc.documentElement.getAttribute("lang"), og_title: q('meta[property="og:title"]'),
      hreflang: [...doc.querySelectorAll('link[rel="alternate"][hreflang]')].map((l) => ({ lang: l.getAttribute("hreflang"), href: l.getAttribute("href") })) };
  },
  headings(doc) { return [...doc.querySelectorAll("h1,h2,h3,h4,h5,h6")].map((h) => ({ level: +h.tagName[1], text: CAF.lib.html.text(h) })); },
  links(doc, base) {
    return [...doc.querySelectorAll("a[href]")].map((a) => { let href = a.getAttribute("href"); try { href = new URL(href, base).toString(); } catch { /* keep raw */ }
      let internal = false; try { internal = new URL(href).hostname === new URL(base).hostname; } catch { /* external */ }
      return { href, text: CAF.lib.html.text(a), internal, label: a.getAttribute("aria-label") || "" }; });
  },
  absolute(href, base) { try { return new URL(href, base).toString(); } catch { return href; } },
  /** robots.txt -> groups [{agents:[...], rules:[{allow, path}]}] */
  parseRobots(text) {
    const groups = []; let cur = null, lastWasAgent = false;
    for (const raw of String(text || "").split(/\r?\n/)) {
      const line = raw.replace(/#.*/, "").trim(); if (!line) continue;
      const [k, ...rest] = line.split(":"); const key = k.trim().toLowerCase(); const val = rest.join(":").trim();
      if (key === "user-agent") { if (!cur || !lastWasAgent) { cur = { agents: [], rules: [] }; groups.push(cur); } cur.agents.push(val.toLowerCase()); lastWasAgent = true; continue; }
      lastWasAgent = false;
      if (!cur) continue;
      if (key === "allow" || key === "disallow") cur.rules.push({ allow: key === "allow", path: val });
    }
    return groups;
  },
  robotsAllowed(groups, ua, path = "/") {
    const u = ua.toLowerCase();
    let group = groups.find((g) => g.agents.some((a) => a !== "*" && u.includes(a)));
    if (!group) group = groups.find((g) => g.agents.includes("*"));
    if (!group) return true;
    let best = null;
    for (const r of group.rules) { if (!r.path) { if (!r.allow) continue; } if (path.startsWith(r.path) && (!best || r.path.length > best.path.length)) best = r; }
    return best ? best.allow || best.path === "" : true;
  },
  /** Render html in a hidden same-origin iframe; resolves {frame, doc, win, remove}. */
  renderInFrame(html, { width = 1280, height = 900, visible = false } = {}) {
    return new Promise((resolve, reject) => {
      const frame = document.createElement("iframe");
      frame.setAttribute("aria-hidden", "true");
      frame.tabIndex = -1;
      Object.assign(frame.style, visible ? { width: "100%", height: `${height}px`, border: "0" } : { position: "fixed", left: "-10000px", top: "0", width: `${width}px`, height: `${height}px`, border: "0", opacity: "0", pointerEvents: "none" });
      const timer = setTimeout(() => reject(new Error("page render timed out")), 15000);
      frame.onload = () => { clearTimeout(timer); resolve({ frame, doc: frame.contentDocument, win: frame.contentWindow, remove: () => frame.remove() }); };
      frame.srcdoc = html;
      document.body.appendChild(frame);
    });
  },
};

/* ------------------------------------------------------------------------- colour */
CAF.lib.color = {
  parse(c) {
    if (!c) return null; c = String(c).trim().toLowerCase();
    const named = { white: [255, 255, 255], black: [0, 0, 0], red: [255, 0, 0], green: [0, 128, 0], blue: [0, 0, 255], gray: [128, 128, 128], grey: [128, 128, 128], transparent: null };
    if (c in named) return named[c];
    let m = c.match(/^#([0-9a-f]{3,8})$/);
    if (m) { let h = m[1]; if (h.length <= 4) h = h.split("").map((x) => x + x).join(""); return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)]; }
    m = c.match(/^rgba?\(([^)]+)\)$/);
    if (m) { const p = m[1].split(/[\s,/]+/).filter(Boolean).map(parseFloat); if (p.length >= 4 && p[3] === 0) return null; return p.slice(0, 3); }
    return null;
  },
  luminance([r, g, b]) { const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; }; return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b); },
  contrast(fg, bg) { const a = CAF.lib.color.parse(fg), b = CAF.lib.color.parse(bg); if (!a || !b) return null; const [l1, l2] = [CAF.lib.color.luminance(a), CAF.lib.color.luminance(b)].sort((x, y) => y - x); return (l1 + 0.05) / (l2 + 0.05); },
};

/* ------------------------------------------------------------------------- charts (SVG) */
CAF.lib.chart = (() => {
  const palette = { line: "#1d4ed8", line2: "#b64c15", line3: "#08807a", line4: "#6d46c9", grid: "#d9dfe7", text: "#5d6a7d", accent: "#c9302a" };
  function niceTicks(min, max, count = 5) {
    if (min === max) { min -= 1; max += 1; }
    const span = max - min, step0 = span / count, mag = 10 ** Math.floor(Math.log10(step0)), err = step0 / mag;
    const step = (err >= 7.5 ? 10 : err >= 3.5 ? 5 : err >= 1.5 ? 2 : 1) * mag;
    const lo = Math.floor(min / step) * step, hi = Math.ceil(max / step) * step, ticks = [];
    for (let v = lo; v <= hi + step / 2; v += step) ticks.push(Math.round(v * 1e6) / 1e6);
    return ticks;
  }
  const fmt = (v) => (Math.abs(v) >= 1e6 ? (v / 1e6).toFixed(1) + "M" : Math.abs(v) >= 1e3 ? (v / 1e3).toFixed(v >= 1e4 ? 0 : 1) + "k" : String(Math.round(v * 100) / 100));
  /** line({series:[{name, values:[number]}], labels:[string], title, markers:[{index, label}], width, height, themed}) */
  function line({ series, labels = [], title = "", markers = [], width = 720, height = 280, themed = true, yLabel = "" }) {
    const c = themed ? { line: "var(--accent)", grid: "var(--line)", text: "var(--muted)", accent: "var(--fail)", l2: "var(--cat-web)", l3: "var(--cat-content)", l4: "var(--cat-strategy)" } : { ...palette, l2: palette.line2, l3: palette.line3, l4: palette.line4 };
    const colors = [c.line, c.l2, c.l3, c.l4];
    const pad = { l: 52, r: 16, t: title ? 30 : 14, b: 34 };
    const all = series.flatMap((s) => s.values.filter((v) => v !== null && v !== undefined));
    const ticks = niceTicks(Math.min(0, ...all), Math.max(...all));
    const yMin = ticks[0], yMax = ticks[ticks.length - 1];
    const n = Math.max(...series.map((s) => s.values.length));
    const x = (i) => pad.l + (n <= 1 ? 0 : (i / (n - 1)) * (width - pad.l - pad.r));
    const y = (v) => pad.t + (1 - (v - yMin) / (yMax - yMin || 1)) * (height - pad.t - pad.b);
    let svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${width} ${height}" width="100%" role="img" aria-label="${U.esc(title || "chart")}" style="font-family:system-ui,sans-serif">`;
    if (!themed) svg += `<rect width="${width}" height="${height}" fill="#ffffff"/>`;
    if (title) svg += `<text x="${pad.l}" y="18" font-size="13" font-weight="600" fill="${themed ? "var(--ink)" : "#0f1724"}">${U.esc(title)}</text>`;
    for (const t of ticks) svg += `<line x1="${pad.l}" x2="${width - pad.r}" y1="${y(t)}" y2="${y(t)}" stroke="${c.grid}" stroke-width="1"/><text x="${pad.l - 6}" y="${y(t) + 4}" font-size="10.5" text-anchor="end" fill="${c.text}">${fmt(t)}</text>`;
    const every = Math.max(1, Math.ceil(n / 8));
    labels.forEach((lab, i) => { if (i % every === 0 || i === n - 1) svg += `<text x="${x(i)}" y="${height - 12}" font-size="10.5" text-anchor="middle" fill="${c.text}">${U.esc(lab)}</text>`; });
    markers.forEach((m) => { svg += `<line x1="${x(m.index)}" x2="${x(m.index)}" y1="${pad.t}" y2="${height - pad.b}" stroke="${c.accent}" stroke-dasharray="4 3" stroke-width="1.2"/><text x="${x(m.index) + 4}" y="${pad.t + 10}" font-size="10.5" fill="${c.accent}">${U.esc(m.label || "")}</text>`; });
    series.forEach((s, k) => {
      const pts = s.values.map((v, i) => (v === null || v === undefined ? null : `${x(i).toFixed(1)},${y(v).toFixed(1)}`)).filter(Boolean);
      svg += `<polyline fill="none" stroke="${colors[k % colors.length]}" stroke-width="2" points="${pts.join(" ")}"/>`;
      const last = s.values.length - 1; if (last >= 0 && s.values[last] !== null) svg += `<circle cx="${x(last)}" cy="${y(s.values[last])}" r="3.5" fill="${colors[k % colors.length]}"/>`;
      (s.points || []).forEach((i) => { svg += `<circle cx="${x(i)}" cy="${y(s.values[i])}" r="4.5" fill="none" stroke="${c.accent}" stroke-width="2"/>`; });
    });
    if (series.length > 1) series.forEach((s, k) => { svg += `<rect x="${pad.l + k * 130}" y="${height - 8}" width="10" height="3" fill="${colors[k % colors.length]}"/><text x="${pad.l + 14 + k * 130}" y="${height - 4}" font-size="10" fill="${c.text}">${U.esc(s.name || "")}</text>`; });
    if (yLabel) svg += `<text x="12" y="${pad.t + 4}" font-size="10.5" fill="${c.text}">${U.esc(yLabel)}</text>`;
    return svg + "</svg>";
  }
  /** bars({items:[{label, value}], title, width, themed}) horizontal bars */
  function bars({ items, title = "", width = 720, themed = true, format = fmt }) {
    const c = themed ? { bar: "var(--accent)", text: "var(--muted)", ink: "var(--ink)", grid: "var(--line)" } : { bar: palette.line, text: palette.text, ink: "#0f1724", grid: palette.grid };
    const rowH = 24, pad = { l: 170, r: 60, t: title ? 30 : 8 }; const height = pad.t + items.length * rowH + 8;
    const max = Math.max(...items.map((i) => i.value), 1);
    let svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${width} ${height}" width="100%" role="img" aria-label="${U.esc(title || "bar chart")}" style="font-family:system-ui,sans-serif">`;
    if (!themed) svg += `<rect width="${width}" height="${height}" fill="#ffffff"/>`;
    if (title) svg += `<text x="8" y="18" font-size="13" font-weight="600" fill="${c.ink}">${U.esc(title)}</text>`;
    items.forEach((it, i) => { const yy = pad.t + i * rowH; const w = (it.value / max) * (width - pad.l - pad.r);
      svg += `<text x="${pad.l - 8}" y="${yy + 15}" font-size="11" text-anchor="end" fill="${c.text}">${U.esc(CAF.lib.text.truncate(String(it.label), 28))}</text><rect x="${pad.l}" y="${yy + 4}" width="${Math.max(1, w).toFixed(1)}" height="15" rx="2" fill="${it.color || c.bar}"/><text x="${pad.l + w + 6}" y="${yy + 15}" font-size="11" fill="${c.ink}">${U.esc(format(it.value))}</text>`; });
    return svg + "</svg>";
  }
  return { line, bars, niceTicks, fmt };
})();

/* ------------------------------------------------------------------------- email (port of caf.core.emailkit) */
CAF.lib.email = (() => {
  const UNSUB = "{{unsubscribe_url}}";
  const md = (t) => CAF.lib.md.html(t);
  function html({ subject, preheader, body_markdown, cta_text, cta_url, brand, accent = "#0b5cad" }) {
    const legal = brand.legal || {};
    const button = cta_text && cta_url ? `<table role="presentation" cellpadding="0" cellspacing="0" style="margin:24px 0"><tr><td style="border-radius:6px;background:${accent}"><a href="${U.esc(cta_url)}" style="display:inline-block;padding:12px 22px;color:#ffffff;font-weight:600;text-decoration:none;font-family:Arial,sans-serif">${U.esc(cta_text)}</a></td></tr></table>` : "";
    return `<!doctype html>\n<html lang="${U.esc((brand.locale || "en").slice(0, 2))}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${U.esc(subject)}</title></head>\n<body style="margin:0;background:#f3f5f8">\n<span style="display:none!important;opacity:0;color:transparent;height:0;width:0;overflow:hidden">${U.esc(preheader)}</span>\n<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f3f5f8"><tr><td align="center" style="padding:24px 12px">\n<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#ffffff;border-radius:8px">\n<tr><td style="padding:28px 32px 8px;font-family:Arial,sans-serif;font-size:13px;color:#5b6573">${U.esc(brand.name || "")}</td></tr>\n<tr><td style="padding:8px 32px 28px;font-family:Arial,sans-serif;font-size:16px;line-height:1.55;color:#1d2530">${md(body_markdown)}${button}</td></tr>\n<tr><td style="padding:18px 32px;border-top:1px solid #e6e9ee;font-family:Arial,sans-serif;font-size:12px;color:#6b7480">\n${U.esc(legal.footer || "")}<br>${U.esc(legal.address || "")}<br><a href="${UNSUB}" style="color:#6b7480">Unsubscribe</a></td></tr>\n</table></td></tr></table></body></html>`;
  }
  function mjml({ subject, preheader, body_markdown, cta_text, cta_url, brand, accent = "#0b5cad" }) {
    const legal = brand.legal || {};
    const button = cta_text ? `<mj-button background-color="${accent}" href="${U.esc(cta_url || "#")}">${U.esc(cta_text)}</mj-button>` : "";
    return `<mjml>\n  <mj-head><mj-title>${U.esc(subject)}</mj-title><mj-preview>${U.esc(preheader)}</mj-preview>\n    <mj-attributes><mj-all font-family="Arial, sans-serif" /><mj-text font-size="16px" line-height="1.55" color="#1d2530" /></mj-attributes>\n  </mj-head>\n  <mj-body background-color="#f3f5f8">\n    <mj-section background-color="#ffffff"><mj-column>\n      <mj-text font-size="13px" color="#5b6573">${U.esc(brand.name || "")}</mj-text>\n      <mj-text>${md(body_markdown)}</mj-text>\n      ${button}\n    </mj-column></mj-section>\n    <mj-section><mj-column><mj-text font-size="12px" color="#6b7480">${U.esc(legal.footer || "")}<br/>${U.esc(legal.address || "")}<br/><a href="${UNSUB}">Unsubscribe</a></mj-text></mj-column></mj-section>\n  </mj-body>\n</mjml>`;
  }
  function text({ subject, body_markdown, cta_text, cta_url, brand }) {
    let plain = String(body_markdown || "").replace(/[*_`#>]/g, "").replace(/\[([^\]]+)\]\(([^)]+)\)/g, "$1 ($2)");
    const legal = brand.legal || {};
    const parts = [plain.trim()]; if (cta_text && cta_url) parts.push(`${cta_text}: ${cta_url}`);
    parts.push(`--\n${legal.footer || ""}\n${legal.address || ""}\nUnsubscribe: ${UNSUB}`);
    return parts.filter(Boolean).join("\n\n");
  }
  function lint({ subject, preheader, html: h, text: t, cta_text, rules, banned_phrases = [] }) {
    const checks = []; const s = rules.subject || {}, p = rules.preheader || {}, b = rules.body || {}, req = rules.require || {};
    const n = subject.length;
    if (n > (s.fail_over_chars ?? 78)) checks.push({ rule: "subject.length", status: "fail", severity: "high", message: `Subject is ${n} chars; most clients truncate beyond ${s.fail_over_chars ?? 78}.` });
    else if (n > (s.warn_over_chars ?? 50)) checks.push({ rule: "subject.length", status: "warn", severity: "medium", message: `Subject is ${n} chars; aim for <= ${s.warn_over_chars ?? 50} for mobile.` });
    else checks.push({ rule: "subject.length", status: "pass", severity: "low", message: `Subject length ${n} chars.` });
    const caps = (subject.match(/\b[A-Z]{3,}\b/g) || []).filter((w) => !["FAQ", "USA", "UK", "AI", "CEO"].includes(w));
    if (caps.length > (s.max_caps_words ?? 1)) checks.push({ rule: "subject.caps", status: "warn", severity: "medium", message: `ALL-CAPS words in subject: ${caps.join(", ")}` });
    if (/[!?$]{2,}/.test(subject)) checks.push({ rule: "subject.punctuation", status: "warn", severity: "medium", message: "Repeated punctuation in subject looks spammy." });
    const pn = preheader.length, lo = p.min_chars ?? 40, hi = p.max_chars ?? 130;
    checks.push({ rule: "preheader.length", status: pn >= lo && pn <= hi ? "pass" : "warn", severity: "low", message: `Preheader ${pn} chars (recommended ${lo}-${hi}).` });
    const wc = CAF.lib.text.wordCount(t);
    if (wc < (b.min_words ?? 50) || wc > (b.max_words ?? 350)) checks.push({ rule: "body.length", status: "warn", severity: "low", message: `Body is ${wc} words (recommended ${b.min_words ?? 50}-${b.max_words ?? 350}).` });
    const links = (h.match(/<a\s[^>]*href=/gi) || []).length - 1;
    if (links > (b.max_links ?? 5)) checks.push({ rule: "body.links", status: "warn", severity: "medium", message: `${links} links; more than ${b.max_links ?? 5} hurts deliverability.` });
    for (const img of h.match(/<img\b[^>]*>/gi) || []) if (!/\balt\s*=\s*"[^"]+"/i.test(img)) checks.push({ rule: "body.img_alt", status: "fail", severity: "high", message: "Image without alt text.", ref: "WCAG 1.1.1 (A)" });
    const lowered = `${subject} ${preheader} ${h.replace(/<[^>]+>/g, " ")}`.toLowerCase();
    const hits = (rules.spam_terms || []).filter((x) => lowered.includes(String(x).toLowerCase()));
    if (hits.length) checks.push({ rule: "content.spam_terms", status: "warn", severity: "medium", message: `Spam-trigger terms: ${hits.join(", ")}` });
    const banned = banned_phrases.filter((x) => lowered.includes(String(x).toLowerCase()));
    if (banned.length) checks.push({ rule: "brand.banned_phrases", status: "fail", severity: "high", message: `Brand-banned phrases used: ${banned.join(", ")}` });
    if (req.unsubscribe !== false) { const ok = h.includes(UNSUB) || /unsubscribe/i.test(h); checks.push({ rule: "compliance.unsubscribe", status: ok ? "pass" : "fail", severity: ok ? "low" : "critical", message: ok ? "Unsubscribe link present." : "Missing unsubscribe link (CAN-SPAM / GDPR / CASL)." }); }
    if (req.cta !== false) checks.push({ rule: "content.cta", status: cta_text ? "pass" : "warn", severity: "medium", message: cta_text ? "Clear CTA present." : "No call-to-action." });
    return checks;
  }
  return { html, mjml, text, lint, UNSUB };
})();

/* ------------------------------------------------------------------------- QR (qrcode-generator) */
CAF.lib.qr = {
  available() { return typeof window.qrcode === "function"; },
  make(text, { ecl = "M", cell = 8, margin = 4, fg = "#000000", bg = "#ffffff" } = {}) {
    if (!CAF.lib.qr.available()) throw new Error("QR library did not load");
    const q = window.qrcode(0, ecl); q.addData(text, "Byte"); q.make();
    const n = q.getModuleCount(); const size = (n + margin * 2) * cell;
    let path = "";
    for (let r = 0; r < n; r++) for (let c = 0; c < n; c++) if (q.isDark(r, c)) path += `M${(c + margin) * cell},${(r + margin) * cell}h${cell}v${cell}h-${cell}z`;
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${size} ${size}" width="${size}" height="${size}" shape-rendering="crispEdges"><rect width="${size}" height="${size}" fill="${bg}"/><path d="${path}" fill="${fg}"/></svg>`;
    return { svg, modules: n, version: (n - 17) / 4, size_px: size, ecl, isDark: (r, c) => q.isDark(r, c) };
  },
  async png(svg, size) {
    const img = new Image(); const url = "data:image/svg+xml;base64," + btoa(unescape(encodeURIComponent(svg)));
    await new Promise((res, rej) => { img.onload = res; img.onerror = () => rej(new Error("QR render failed")); img.src = url; });
    const cv = document.createElement("canvas"); cv.width = cv.height = size; const g = cv.getContext("2d"); g.drawImage(img, 0, 0, size, size);
    return await new Promise((res) => cv.toBlob(res, "image/png"));
  },
};

/* ------------------------------------------------------------------------- markdown */
CAF.lib.md = {
  html(text) {
    const src = String(text || "");
    if (window.marked && typeof window.marked.parse === "function") {
      const raw = window.marked.parse(src, { gfm: true, breaks: false });
      return window.DOMPurify ? window.DOMPurify.sanitize(raw) : raw.replace(/<script[\s\S]*?<\/script>/gi, "");
    }
    return src.split(/\n{2,}/).map((p) => `<p>${U.esc(p).replace(/\n/g, "<br>")}</p>`).join("");
  },
};
