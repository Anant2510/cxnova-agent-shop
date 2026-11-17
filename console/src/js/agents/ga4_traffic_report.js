/* GA4 Traffic Report - browser twin of caf/agents/web/ga4_traffic_report.py.
   Same stages (window -> load -> compute -> anomalies -> narrate -> report). Data: the embedded analytics
   exports (analytics/daily, analytics/pages, analytics/channels = the Python analytics:csv connector),
   aggregated exactly like CsvAnalytics. Statistics are ports, verified against scipy/statsmodels:
   Mann-Whitney U (scipy method="auto": exact null distribution for small untied samples, otherwise the
   normal approximation with tie and continuity correction), statsmodels' pooled two-proportion z-test,
   Cephes ndtr for normal tail areas, the same-weekday rolling median + MAD robust z, and a line-by-line
   port of statsmodels' robust STL (Cleveland et al. 1990; period 7, seasonal 7, trend 15, low-pass 9,
   2 inner / 15 outer iterations) for the second anomaly arm, combined by the same consensus rule.
   The model only narrates; numbers in its prose are checked against the computed facts. */
(() => {
  const AGENT = "ga4_traffic_report";
  const VOLUME_METRICS = ["sessions", "users", "new_users", "conversions", "revenue"];
  const KPI_LABELS = { sessions: "Sessions", users: "Users", new_users: "New users", conversions: "Conversions", conversion_rate: "Conversion rate", revenue: "Revenue", engagement_rate: "Engagement rate" };
  const ANOMALY_METRICS = ["sessions", "users", "conversions", "revenue"];
  const DAILY_COLUMNS = ["sessions", "users", "new_users", "engaged_sessions", "conversions", "revenue"];
  const LOOKBACK_DAYS = 56;
  const isNum = (v) => typeof v === "number" && !Number.isNaN(v);
  const nan = (v) => v === null || v === undefined || (typeof v === "number" && Number.isNaN(v));
  const sum = (a) => a.reduce((s, x) => s + x, 0);

  // ------------------------------------------------------------------ Python formatting parity
  function pyFixed(x, d = 0) {
    x = Number(x);
    const out = x.toFixed(d);
    if (!Number.isFinite(x) || Math.abs(x) >= 1e21) return out;
    const ex = Math.abs(x).toFixed(100), dot = ex.indexOf(".");
    if (!/^50*$/.test(ex.slice(dot + 1 + d))) return out;
    const ip = ex.slice(0, dot), fp = ex.slice(dot + 1, dot + 1 + d);
    if (Number((d ? fp : ip).slice(-1)) % 2) return out;
    return (x < 0 ? "-" : "") + (d ? `${ip}.${fp}` : ip);
  }
  const pyRound = (x, d) => Number(pyFixed(x, d));
  const group = (s) => { const neg = s.startsWith("-"); const body = neg ? s.slice(1) : s; const [i, f] = body.split("."); return (neg ? "-" : "") + i.replace(/\B(?=(\d{3})+(?!\d))/g, ",") + (f !== undefined ? "." + f : ""); };
  /** format(x, "[+][,].{d}f") */
  function fmtF(x, d, { plus = false, comma = false } = {}) {
    const neg = x < 0 || Object.is(x, -0);
    let s = pyFixed(Math.abs(x), d);
    if (comma) s = group(s);
    return (neg ? "-" : plus ? "+" : "") + s;
  }
  /** Round |x| to p significant digits like Python (correct rounding, half-even on exact ties). */
  function roundSig(x, p) {
    const s = Math.abs(x).toFixed(100), dot = s.indexOf("."), digits = s.replace(".", "");
    const first = digits.search(/[1-9]/);
    if (first < 0) return { digits: "0".repeat(p), exp: 0 };
    let exp = dot - first - 1;
    const kept = digits.slice(first, first + p).padEnd(p, "0").split("").map(Number), rest = digits.slice(first + p);
    const up = /^50*$/.test(rest) ? kept[p - 1] % 2 === 1 : rest[0] > "5" || (rest[0] === "5" && /[1-9]/.test(rest.slice(1)));
    if (up) {
      let i = p - 1;
      while (i >= 0 && kept[i] === 9) kept[i--] = 0;
      if (i >= 0) kept[i] += 1; else { kept.unshift(1); kept.pop(); exp += 1; }
    }
    return { digits: kept.join(""), exp };
  }
  /** format(x, "[,].{p}g") */
  function fmtG(x, p = 6, comma = false) {
    if (!Number.isFinite(x)) return Number.isNaN(x) ? "nan" : x > 0 ? "inf" : "-inf";
    if (x === 0) return Object.is(x, -0) ? "-0" : "0";
    p = Math.max(1, p);
    const { digits, exp } = Math.abs(x) < 1e21 ? roundSig(x, p) : { digits: Math.abs(x).toExponential(p - 1).split("e")[0].replace(".", ""), exp: parseInt(Math.abs(x).toExponential(p - 1).split("e")[1], 10) };
    const sign = x < 0 ? "-" : "";
    if (exp >= -4 && exp < p) {
      let t = pyFixed(Math.abs(x), p - 1 - exp);
      if (t.includes(".")) t = t.replace(/0+$/, "").replace(/\.$/, "");
      return sign + (comma ? group(t) : t);
    }
    let mant = digits.length > 1 ? `${digits[0]}.${digits.slice(1)}` : digits;
    if (mant.includes(".")) mant = mant.replace(/0+$/, "").replace(/\.$/, "");
    return `${sign}${mant}e${exp < 0 ? "-" : "+"}${String(Math.abs(exp)).padStart(2, "0")}`;
  }
  function fmtNum(v, kind = "count") {
    if (nan(v)) return "n/a";
    if (kind === "pct") return `${fmtF(v * 100, 1)}%`;
    if (kind === "money") return `$${fmtF(v, 0, { comma: true })}`;
    return fmtF(v, 0, { comma: true });
  }
  const fmtChange = (v) => (v === null || v === undefined ? "n/a" : `${fmtF(v * 100, 1, { plus: true })}%`);
  const cap1 = (s) => (s ? s[0].toUpperCase() + s.slice(1).toLowerCase() : s);

  // ------------------------------------------------------------------ dates (UTC day numbers)
  const dnum = (iso) => { const [y, m, d] = String(iso).slice(0, 10).split("-").map(Number); return Math.round(Date.UTC(y, m - 1, d) / 86400000); };
  const diso = (n) => new Date(n * 86400000).toISOString().slice(0, 10);
  const addDays = (iso, k) => diso(dnum(iso) + k);
  const dayRange = (s, e) => { const out = []; for (let n = dnum(s); n <= dnum(e); n++) out.push(diso(n)); return out; };
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const dayLabel = (iso) => `${iso.slice(8, 10)} ${MONTHS[Number(iso.slice(5, 7)) - 1]}`;

  // ------------------------------------------------------------------ statistics core (verified against scipy / statsmodels)
  // ---- Cephes ndtr / erf / erfc (scipy.special.ndtr) ------------------------------------------
  const P = [2.46196981473530512524e-10, 5.64189564831068821977e-1, 7.46321056442269912687e0, 4.86371970985681366614e1, 1.96520832956077098242e2,
    5.26445194995477358631e2, 9.34528527171957607540e2, 1.02755188689515710272e3, 5.57535335369399327526e2];
  const Q = [1.32281951154744992508e1, 8.67072140885989742329e1, 3.54937778887819891062e2, 9.75708501743205489753e2, 1.82390916687909736289e3,
    2.24633760818710981792e3, 1.65666309194161350182e3, 5.57535340817727675546e2];
  const R = [5.64189583547755073984e-1, 1.27536670759978104416e0, 5.01905042251180477414e0, 6.16021097993053585195e0, 7.40974269950448939160e0, 2.97886665372100240670e0];
  const S = [2.26052863220117276590e0, 9.39603524938001434673e0, 1.20489539808096656605e1, 1.70814450747565897222e1, 9.60896809063285878198e0, 3.36907645100081516050e0];
  const T = [9.60497373987051638749e0, 9.00260197203842689217e1, 2.23200534594684319226e3, 7.00332514112805075473e3, 5.55923013010394962768e4];
  const Uc = [3.35617141647503099647e1, 5.21357949780152679795e2, 4.59432382970980127987e3, 2.26290000613890934246e4, 4.92673942608635921086e4];
  const polevl = (x, c) => c.reduce((acc, k) => acc * x + k, 0);
  const p1evl = (x, c) => c.reduce((acc, k) => acc * x + k, 1);
  const MAXLOG = 7.09782712893383996843e2;
  function erf(x) { if (Math.abs(x) > 1) return 1 - erfc(x); const z = x * x; return (x * polevl(z, T)) / p1evl(z, Uc); }
  function erfc(a) {
    const x = Math.abs(a);
    if (x < 1) return 1 - erf(a);
    let z = -a * a;
    if (z < -MAXLOG) return a < 0 ? 2 : 0;
    z = Math.exp(z);
    const [p, q] = x < 8 ? [polevl(x, P), p1evl(x, Q)] : [polevl(x, R), p1evl(x, S)];
    let y = (z * p) / q;
    if (a < 0) y = 2 - y;
    if (y === 0) return a < 0 ? 2 : 0;
    return y;
  }
  function ndtr(a) {
    const x = a * Math.SQRT1_2, z = Math.abs(x);
    if (z < Math.SQRT1_2) return 0.5 + 0.5 * erf(x);
    const y = 0.5 * erfc(z);
    return x > 0 ? 1 - y : y;
  }
  const normSf = (z) => ndtr(-z);

  // ---- scipy.stats.mannwhitneyu(x, y, alternative="two-sided"), method="auto" ----------------
  function rankAverage(values) {
    const idx = values.map((v, i) => i).sort((a, b) => values[a] - values[b] || a - b);
    const ranks = new Array(values.length), ties = [];
    for (let i = 0; i < idx.length;) {
      let j = i;
      while (j + 1 < idx.length && values[idx[j + 1]] === values[idx[i]]) j++;
      const r = (i + j + 2) / 2;
      for (let k = i; k <= j; k++) ranks[idx[k]] = r;
      ties.push(j - i + 1);
      i = j + 1;
    }
    return { ranks, ties };
  }
  function mwuExactSf(u, m, n) {
    // coefficients of the Gaussian binomial [m+n choose m]_q = distribution of U (counts)
    const max = m * n;
    let poly = new Float64Array(max + 1); poly[0] = 1;
    for (let i = 1; i <= m; i++) {
      const a = n + i;
      for (let k = max; k >= a; k--) poly[k] -= poly[k - a];      // * (1 - q^(n+i))
      for (let k = i; k <= max; k++) poly[k] += poly[k - i];      // / (1 - q^i)
    }
    let total = 0, tail = 0;
    for (let k = 0; k <= max; k++) { total += poly[k]; if (k >= u) tail += poly[k]; }
    return tail / total;
  }
  function mannWhitneyU(x, y) {
    const n1 = x.length, n2 = y.length;
    const { ranks, ties } = rankAverage([...x, ...y]);
    let R1 = 0;
    for (let i = 0; i < n1; i++) R1 += ranks[i];
    const U1 = R1 - (n1 * (n1 + 1)) / 2, U2 = n1 * n2 - U1;
    const U = Math.max(U1, U2);
    const hasTies = ties.some((t) => t > 1);
    let p;
    if ((n1 > 8 && n2 > 8) || hasTies) {
      const n = n1 + n2, mu = (n1 * n2) / 2;
      const tieTerm = ties.reduce((s, t) => s + (t ** 3 - t), 0);
      const s = Math.sqrt(((n1 * n2) / 12) * (n + 1 - tieTerm / (n * (n - 1))));
      const z = (U - mu - 0.5) / s;
      p = normSf(z);
    } else p = mwuExactSf(Math.round(U), n1, n2);
    p *= 2;
    return { statistic: U1, p_value: Math.min(1, Math.max(0, p)) };
  }
  /** statsmodels proportions_ztest(count, nobs) for two samples (pooled variance, two-sided). */
  function proportionsZTest(c1, n1, c2, n2) {
    const diff = c1 / n1 - c2 / n2;
    const pp = (c1 + c2) / (n1 + n2);
    const sd = Math.sqrt(pp * (1 - pp) * (1 / n1 + 1 / n2));
    const z = diff / sd;
    return { z, p_value: normSf(Math.abs(z)) * 2 };
  }

  // ---- statsmodels STL (robust), a line-by-line port of the Cleveland et al. (1990) Fortran ---
  function est(y, n, len, ideg, xs, nleft, nright, w, userw, rw) {
    const range = n - 1;
    let h = Math.max(xs - nleft, nright - xs);
    if (len > n) h += Math.floor((len - n) / 2);
    const h9 = 0.999 * h, h1 = 0.001 * h;
    let a = 0;
    for (let j = nleft; j <= nright; j++) {
      w[j - 1] = 0;
      const r = Math.abs(j - xs);
      if (r <= h9) {
        w[j - 1] = r <= h1 ? 1 : (1 - (r / h) ** 3) ** 3;
        if (userw) w[j - 1] *= rw[j - 1];
        a += w[j - 1];
      }
    }
    if (a <= 0) return null;
    for (let j = nleft; j <= nright; j++) w[j - 1] /= a;
    if (h > 0 && ideg > 0) {
      a = 0;
      for (let j = nleft; j <= nright; j++) a += w[j - 1] * j;
      let b = xs - a, c = 0;
      for (let j = nleft; j <= nright; j++) c += w[j - 1] * (j - a) * (j - a);
      if (Math.sqrt(c) > 0.001 * range) {
        b /= c;
        for (let j = nleft; j <= nright; j++) w[j - 1] = w[j - 1] * (b * (j - a) + 1);
      }
    }
    let ys = 0;
    for (let j = nleft; j <= nright; j++) ys += w[j - 1] * y[j - 1];
    return ys;
  }
  function ess(y, n, len, ideg, njump, userw, rw, ys, off, res) {
    if (n < 2) { ys[off] = y[0]; return; }
    const newnj = Math.min(njump, n - 1);
    let nleft = 1, nright = n;
    if (len >= n) {
      for (let i = 1; i <= n; i += newnj) { const v = est(y, n, len, ideg, i, nleft, nright, res, userw, rw); ys[off + i - 1] = v === null ? y[i - 1] : v; }
    } else if (newnj === 1) {
      const nsh = Math.floor((len + 1) / 2);
      nright = len;
      for (let i = 1; i <= n; i++) {
        if (i > nsh && nright !== n) { nleft++; nright++; }
        const v = est(y, n, len, ideg, i, nleft, nright, res, userw, rw);
        ys[off + i - 1] = v === null ? y[i - 1] : v;
      }
    } else {
      const nsh = Math.floor((len + 1) / 2);
      for (let i = 1; i <= n; i += newnj) {
        if (i < nsh) { nleft = 1; nright = len; } else if (i >= n - nsh + 1) { nleft = n - len + 1; nright = n; } else { nleft = i - nsh + 1; nright = len + i - nsh; }
        const v = est(y, n, len, ideg, i, nleft, nright, res, userw, rw);
        ys[off + i - 1] = v === null ? y[i - 1] : v;
      }
    }
    if (newnj !== 1) {
      for (let i = 1; i <= n - newnj; i += newnj) {
        const delta = (ys[off + i + newnj - 1] - ys[off + i - 1]) / newnj;
        for (let j = i + 1; j <= i + newnj - 1; j++) ys[off + j - 1] = ys[off + i - 1] + delta * (j - i);
      }
      const k = Math.floor((n - 1) / newnj) * newnj + 1;
      if (k !== n) {
        const v = est(y, n, len, ideg, n, nleft, nright, res, userw, rw);
        ys[off + n - 1] = v === null ? y[n - 1] : v;
        if (k !== n - 1) {
          const delta = (ys[off + n - 1] - ys[off + k - 1]) / (n - k);
          for (let j = k + 1; j <= n - 1; j++) ys[off + j - 1] = ys[off + k - 1] + delta * (j - k);
        }
      }
    }
  }
  function ma(x, n, len, ave) {
    const newn = n - len + 1;
    let v = 0;
    for (let i = 0; i < len; i++) v += x[i];
    ave[0] = v / len;
    for (let j = 1, k = len, m = 0; j < newn; j++, k++, m++) { v = v - x[m] + x[k]; ave[j] = v / len; }
  }
  function stl(values, { period = 7, seasonal = 7, robust = true } = {}) {
    const y = Float64Array.from(values), n = y.length, np = period;
    const ns = seasonal;
    let nt = Math.ceil((1.5 * np) / (1 - 1.5 / ns)); if (nt % 2 === 0) nt++;
    let nl = np + 1; if (nl % 2 === 0) nl++;
    const ni = robust ? 2 : 5, no = robust ? 15 : 0;
    const trend = new Float64Array(n), season = new Float64Array(n), rw = new Float64Array(n);
    const W = Array.from({ length: 5 }, () => new Float64Array(n + 2 * np));
    const w1 = new Float64Array(n), w2 = new Float64Array(n + 2), w3 = new Float64Array(n), w4 = new Float64Array(n);
    let userw = false;
    const onestp = () => {
      for (let it = 0; it < ni; it++) {
        for (let i = 0; i < n; i++) W[0][i] = y[i] - trend[i];
        // cycle-subseries smoothing -> W[1] (length n + 2*np)
        for (let j = 1; j <= np; j++) {
          const k = Math.floor((n - j) / np) + 1;
          for (let i = 1; i <= k; i++) w1[i - 1] = W[0][(i - 1) * np + j - 1];
          if (userw) for (let i = 1; i <= k; i++) w3[i - 1] = rw[(i - 1) * np + j - 1];
          ess(w1, k, ns, 1, 1, userw, w3, w2, 1, w4);
          let v = est(w1, k, ns, 1, 0, 1, Math.min(ns, k), w4, userw, w3);
          w2[0] = v === null ? w2[1] : v;
          v = est(w1, k, ns, 1, k + 1, Math.max(1, k - ns + 1), k, w4, userw, w3);
          w2[k + 1] = v === null ? w2[k] : v;
          for (let m = 1; m <= k + 2; m++) W[1][(m - 1) * np + j - 1] = w2[m - 1];
        }
        // low-pass filter of the cycle-subseries
        ma(W[1], n + 2 * np, np, W[2]);
        ma(W[2], n + np + 1, np, W[0]);
        ma(W[0], n + 2, 3, W[2]);
        ess(W[2], n, nl, 1, 1, false, W[3], W[0], 0, W[4]);
        for (let i = 0; i < n; i++) season[i] = W[1][np + i] - W[0][i];
        for (let i = 0; i < n; i++) W[0][i] = y[i] - season[i];
        ess(W[0], n, nt, 1, 1, userw, rw, trend, 0, W[2]);
      }
    };
    for (let k = 0; ; ) {
      onestp();
      k++;
      if (k > no) break;
      for (let i = 0; i < n; i++) W[0][i] = trend[i] + season[i];
      const absr = Array.from(y, (v, i) => Math.abs(v - W[0][i]));
      const sorted = [...absr].sort((a, b) => a - b);
      const mid0 = Math.floor(n / 2), mid1 = n - mid0 - 1;
      const cmad = 3 * (sorted[mid0] + sorted[mid1]);
      const c9 = 0.999 * cmad, c1 = 0.001 * cmad;
      for (let i = 0; i < n; i++) { const r = absr[i]; rw[i] = r <= c1 ? 1 : r <= c9 ? (1 - (r / cmad) ** 2) ** 2 : 0; }
      userw = true;
    }
    const resid = Array.from(y, (v, i) => v - trend[i] - season[i]);
    return { trend: Array.from(trend), seasonal: Array.from(season), resid };
  }
  const median = (a) => { const s = [...a].sort((x, y) => x - y); const n = s.length; if (!n) return NaN; return n % 2 ? s[(n - 1) / 2] : (s[n / 2 - 1] + s[n / 2]) / 2; };
  const stdSample = (a) => { if (a.length < 2) return NaN; const m = sum(a) / a.length; return Math.sqrt(sum(a.map((x) => (x - m) ** 2)) / (a.length - 1)); };
  const stdPop = (a) => { if (!a.length) return NaN; const m = sum(a) / a.length; return Math.sqrt(sum(a.map((x) => (x - m) ** 2)) / a.length); };
  const quantile = (a, q) => { const s = [...a].sort((x, y) => x - y); if (!s.length) return NaN; const pos = (s.length - 1) * q, lo = Math.floor(pos), hi = Math.ceil(pos); return s[lo] + (s[hi] - s[lo]) * (pos - lo); };

  // ------------------------------------------------------------------ analytics:csv connector twin (CsvAnalytics)
  function groupSum(rows, keys, cols) {
    const map = new Map();
    for (const r of rows) {
      if (keys.some((k) => r[k] === null || r[k] === undefined)) continue;  // pandas groupby drops NaN keys
      const id = JSON.stringify(keys.map((k) => String(r[k])));
      let g = map.get(id);
      if (!g) { g = Object.fromEntries(keys.map((k) => [k, String(r[k])])); for (const c of cols) g[c] = 0; map.set(id, g); }
      for (const c of cols) g[c] += isNum(r[c]) ? r[c] : 0;
    }
    return [...map.values()].sort((a, b) => { for (const k of keys) { if (a[k] < b[k]) return -1; if (a[k] > b[k]) return 1; } return 0; });
  }
  const bySessionsDesc = (a, b) => b.sessions - a.sessions;
  function makeProvider(daily, pages, channels) {
    const inRange = (r, s, e) => r.date >= s && r.date <= e;
    const clip = (n) => Math.max(n, 1);
    return {
      provider: "csv",
      dateBounds() { if (!daily.length) return null; const ds = daily.map((r) => r.date).sort(); return [ds[0], ds[ds.length - 1]]; },
      daily(s, e) { return daily.filter((r) => inRange(r, s, e)).sort((a, b) => (a.date < b.date ? -1 : a.date > b.date ? 1 : 0)); },
      pages(s, e, limit = 50) {
        const agg = groupSum(pages.filter((r) => inRange(r, s, e)), ["page_path", "page_title"], ["sessions", "users", "engaged_sessions", "engagement_time_s", "conversions"]);
        for (const r of agg) { r.engagement_rate = pyRound(r.engaged_sessions / clip(r.sessions), 4); r.avg_engagement_time_s = pyRound(r.engagement_time_s / clip(r.sessions), 1); r.conversion_rate = pyRound(r.conversions / clip(r.sessions), 4); }
        return agg.sort(bySessionsDesc).slice(0, limit);
      },
      channels(s, e) {
        const agg = groupSum(channels.filter((r) => inRange(r, s, e)), ["channel"], ["sessions", "users", "conversions", "revenue"]);
        for (const r of agg) r.conversion_rate = pyRound(r.conversions / clip(r.sessions), 4);
        return agg.sort(bySessionsDesc);
      },
      channelsDaily(s, e) { return channels.filter((r) => inRange(r, s, e)); },
    };
  }

  // ------------------------------------------------------------------ window + statistics (pure)
  function resolveWindow(start, end, bounds, today, days = 30) {
    const notes = [];
    if (!end) { end = bounds ? bounds[1] : addDays(today, -1); notes.push(`end date defaulted to ${end} (${bounds ? "last day of available data" : "yesterday"})`); }
    if (!start) { start = addDays(end, -(days - 1)); notes.push(`start date defaulted to ${start} (last ${days} complete days)`); }
    if (start > end) throw new Error(`start_date ${start} is after end_date ${end}`);
    return [start, end, notes];
  }
  function comparisonWindow(start, end, mode) {
    const n = dnum(end) - dnum(start) + 1;
    if (mode === "previous_year") return [addDays(start, -364), addDays(end, -364)];
    return [addDays(start, -n), addDays(start, -1)];
  }
  /** Reindex to every calendar day; missing days become 0 and are reported. */
  function completeDaily(rows, start, end) {
    const byDay = new Map();
    for (const r of rows) {
      const d = String(r.date).slice(0, 10);
      if (!byDay.has(d)) byDay.set(d, Object.fromEntries(DAILY_COLUMNS.map((c) => [c, 0])));
      const acc = byDay.get(d);
      for (const c of DAILY_COLUMNS) acc[c] += isNum(r[c]) ? r[c] : 0;
    }
    const missing = [];
    const out = dayRange(start, end).map((d) => { const v = byDay.get(d); if (!v) missing.push(d); return { date: d, ...(v || Object.fromEntries(DAILY_COLUMNS.map((c) => [c, 0]))) }; });
    return [out, missing];
  }
  function kpiTotals(days) {
    const s = Object.fromEntries(DAILY_COLUMNS.map((c) => [c, sum(days.map((d) => d[c] || 0))]));
    const out = Object.fromEntries(VOLUME_METRICS.map((m) => [m, s[m] || 0]));
    out.conversion_rate = s.sessions ? s.conversions / s.sessions : 0;
    out.engagement_rate = s.sessions ? s.engaged_sessions / s.sessions : 0;
    return out;
  }
  const pctChange = (cur, prev) => (prev === null || prev === undefined || prev === 0 || Number.isNaN(prev) ? null : (cur - prev) / prev);
  function compareKpis(cur, prev, curAdj = null, prevAdj = null) {
    const ct = kpiTotals(cur);
    const pt = prev && prev.length && sum(prev.map((d) => d.sessions)) > 0 ? kpiTotals(prev) : null;
    const meanOf = (days, m) => sum(days.map((d) => d[m])) / days.length;
    return ["sessions", "users", "new_users", "conversions", "conversion_rate", "revenue", "engagement_rate"].map((m) => {
      const row = { metric: m, label: KPI_LABELS[m], current: ct[m], previous: pt ? pt[m] : null, change_abs: pt ? ct[m] - pt[m] : null, change_pct: pctChange(ct[m], pt ? pt[m] : null),
        adjusted_change_pct: null, p_value: null, significant: null, test: "", kind: m.endsWith("_rate") ? "rate" : "volume", note: ["users", "new_users"].includes(m) ? "sum of daily users (overstates unique users)" : "" };
      if (pt) {
        try {
          if (m.endsWith("_rate")) {
            const num = m === "conversion_rate" ? "conversions" : "engaged_sessions";
            const c1 = sum(cur.map((d) => d[num])), c2 = sum(prev.map((d) => d[num])), n1 = sum(cur.map((d) => d.sessions)), n2 = sum(prev.map((d) => d.sessions));
            if (Math.min(n1, n2) > 0) { row.p_value = proportionsZTest(c1, n1, c2, n2).p_value; row.test = "two-proportion z-test"; }
          } else if (cur.length >= 5 && prev.length >= 5) {
            row.p_value = mannWhitneyU(cur.map((d) => Number(d[m])), prev.map((d) => Number(d[m]))).p_value;
            row.test = "Mann-Whitney U on daily values";
          }
        } catch { /* keep p_value null, like the Python except branch */ }
        if (row.p_value !== null && !Number.isNaN(row.p_value)) row.significant = row.p_value < 0.05;
        else row.p_value = null;
        if (curAdj && prevAdj && curAdj.length && prevAdj.length) {
          const [a, b] = m.endsWith("_rate") ? [kpiTotals(curAdj)[m], kpiTotals(prevAdj)[m]] : [meanOf(curAdj, m), meanOf(prevAdj, m)];
          row.adjusted_change_pct = pctChange(a, b);
        }
      }
      return row;
    });
  }
  /** pandas outer merge on `key` (keys sorted lexicographically) with *_prev columns, NaN -> 0. */
  function compareDimension(cur, prev, key, cols) {
    const prevBy = new Map();
    for (const r of prev || []) { if (!prevBy.has(r[key])) prevBy.set(r[key], []); prevBy.get(r[key]).push(r); }
    const curKeys = new Set(cur.map((r) => r[key]));
    const out = [];
    const mk = (c, p) => { const o = { ...(c || { [key]: p[key] }) }; for (const col of cols) { o[col] = c && isNum(c[col]) ? c[col] : 0; o[`${col}_prev`] = p && isNum(p[col]) ? p[col] : 0; } return o; };
    for (const c of cur) { const ps = prevBy.get(c[key]); if (ps) for (const p of ps) out.push(mk(c, p)); else out.push(mk(c, null)); }
    for (const [k, ps] of prevBy) if (!curKeys.has(k)) for (const p of ps) out.push(mk(null, p));
    out.sort((a, b) => (String(a[key]) < String(b[key]) ? -1 : String(a[key]) > String(b[key]) ? 1 : 0));
    for (const r of out) r.sessions_change_pct = r.sessions_prev > 0 ? (r.sessions - r.sessions_prev) / r.sessions_prev : null;
    return out;
  }
  function channelTable(cur, prev) {
    const m = compareDimension(cur, prev, "channel", ["sessions", "users", "conversions", "revenue"]);
    const ts = sum(m.map((r) => r.sessions)), tc = sum(m.map((r) => r.conversions)), tsp = sum(m.map((r) => r.sessions_prev)), tcp = sum(m.map((r) => r.conversions_prev));
    return [...m].sort(bySessionsDesc).map((r) => {
      const share = ts ? r.sessions / ts : 0, sharePrev = tsp ? r.sessions_prev / tsp : null;
      return { channel: String(r.channel), sessions: r.sessions, sessions_prev: r.sessions_prev, sessions_change_pct: r.sessions_change_pct, share, share_prev: sharePrev,
        share_change_pp: sharePrev !== null ? (share - sharePrev) * 100 : null, conversions: r.conversions, conversion_share: tc ? r.conversions / tc : 0,
        conversion_share_prev: tcp ? r.conversions_prev / tcp : null, conversion_rate: r.sessions ? r.conversions / r.sessions : 0,
        conversion_rate_prev: r.sessions_prev ? r.conversions_prev / r.sessions_prev : null, revenue: r.revenue, revenue_prev: r.revenue_prev };
    });
  }
  function pageTable(cur, prev, topN) {
    const m = compareDimension(cur, prev, "page_path", ["sessions", "users", "engaged_sessions", "conversions"]);
    for (const r of m) {
      r.page_title = r.page_title === null || r.page_title === undefined ? "" : String(r.page_title);
      r.conversion_rate = r.sessions > 0 ? r.conversions / r.sessions : 0;
      r.engagement_rate = r.sessions > 0 ? r.engaged_sessions / r.sessions : 0;
      r.delta = r.sessions - r.sessions_prev;
    }
    const row = (r) => ({ page_path: String(r.page_path), page_title: r.page_title, sessions: r.sessions, sessions_prev: r.sessions_prev, sessions_change_pct: r.sessions_change_pct,
      conversions: r.conversions, conversion_rate: r.conversion_rate, engagement_rate: r.engagement_rate, delta: r.delta,
      status: r.sessions_prev === 0 && r.sessions > 0 ? "new" : r.sessions === 0 ? "lost" : "" });
    const top = [...m].sort(bySessionsDesc).slice(0, topN).map(row);
    const floor = m.length ? Math.max(50, quantile(m.map((r) => r.sessions_prev), 0.25)) : 50;
    const movers = m.filter((r) => r.sessions_prev >= floor || r.sessions >= floor).sort((a, b) => Math.abs(b.delta) - Math.abs(a.delta)).slice(0, 6).map(row);
    return [top, movers];
  }

  // ------------------------------------------------------------------ anomaly detection (pure)
  /** Expected value = median of the same weekday over the previous 4 weeks (needs >= 2 of them). */
  function seasonalBaseline(y, period = 7, lags = 4) {
    return y.map((_, i) => {
      const vals = [];
      for (let k = 1; k <= lags; k++) { const j = i - period * k; if (j >= 0 && !nan(y[j])) vals.push(y[j]); }
      return vals.length >= 2 ? median(vals) : NaN;
    });
  }
  /** (x - median) / (1.4826 * MAD) - a z-score that a few extreme days cannot inflate. */
  function robustZ(resid) {
    const r = resid.filter((v) => !nan(v));
    if (!r.length) return resid.map(() => NaN);
    const med = median(r), mad = median(r.map((v) => Math.abs(v - med)));
    const sd = stdSample(r);
    const scale = mad > 0 ? 1.4826 * mad : Number.isNaN(sd) ? NaN : sd || 1;
    return resid.map((v) => (nan(v) ? NaN : (v - med) / scale));
  }
  function detectAnomalies(days, metrics, { method = "consensus", threshold = 3.5, minChange = 0.15 } = {}) {
    const rows = [];
    for (const m of metrics) {
      const y = days.map((d) => Number(d[m]));
      const base = seasonalBaseline(y);
      const zMad = robustZ(y.map((v, i) => v - base[i]));
      const fit = y.filter((v) => !nan(v)).length >= 15 ? stl(y) : null;
      const zStl = fit ? robustZ(fit.resid) : y.map(() => NaN);
      const expected = base.map((b, i) => (Number.isNaN(b) && fit ? fit.trend[i] + fit.seasonal[i] : b));
      days.forEach((d, i) => {
        const zm = zMad[i], zs = zStl[i], exp = expected[i];
        if (nan(exp) || exp <= 0) return;
        const madHit = !nan(zm) && Math.abs(zm) >= threshold, stlHit = !nan(zs) && Math.abs(zs) >= threshold;
        const hits = [...(madHit ? ["rolling_mad"] : []), ...(stlHit ? ["stl"] : [])];
        const flagged = { consensus: madHit && (stlHit || !fit), either: madHit || stlHit, mad: madHit, stl: stlHit }[method] ?? (madHit && stlHit);
        const dev = (y[i] - exp) / exp;
        if (flagged && Math.abs(dev) >= minChange) rows.push({ date: d.date, metric: m, actual: y[i], expected: exp, deviation_pct: dev, z_mad: nan(zm) ? null : zm, z_stl: nan(zs) ? null : zs, methods: hits });
      });
    }
    return rows;
  }
  /** Merge consecutive flagged days (same direction) inside [start, end] into anomaly events. */
  function groupEvents(flags, start, end) {
    const f = flags.filter((r) => r.date >= start && r.date <= end).map((r) => ({ ...r, direction: r.deviation_pct < 0 ? "drop" : "spike" }));
    if (!f.length) return [];
    const byKey = new Map();
    for (const r of f) { const k = `${r.date}|${r.direction}`; if (!byKey.has(k)) byKey.set(k, { date: r.date, direction: r.direction, metrics: new Set() }); byKey.get(k).metrics.add(r.metric); }
    const daysList = [...byKey.values()].sort((a, b) => (a.date < b.date ? -1 : a.date > b.date ? 1 : a.direction < b.direction ? -1 : a.direction > b.direction ? 1 : 0));
    const events = [];
    for (const r of daysList) {
      const last = events[events.length - 1];
      if (last && last.direction === r.direction && dnum(r.date) - dnum(last.end) <= 1) { last.end = r.date; last.days.push(r.date); last.metrics = [...new Set([...last.metrics, ...r.metrics])].sort(); }
      else events.push({ start: r.date, end: r.date, direction: r.direction, days: [r.date], metrics: [...r.metrics].sort() });
    }
    return events.map((e) => {
      const sub = f.filter((r) => e.days.includes(r.date));
      const sess = sub.filter((r) => r.metric === "sessions");
      const ref = sess.length ? sess : sub.filter((r) => r.metric === sub[0].metric);
      const actual = sum(ref.map((r) => r.actual)), expected = sum(ref.map((r) => r.expected));
      const zs = sub.flatMap((r) => [r.z_mad, r.z_stl]).filter((v) => v !== null).map(Math.abs);
      return { start: e.start, end: e.end, days: e.days.length, direction: e.direction, metrics: e.metrics, reference_metric: ref[0].metric, actual, expected,
        deviation_pct: expected ? (actual - expected) / expected : 0, max_abs_z: zs.length ? Math.max(...zs) : 0, methods: [...new Set(sub.flatMap((r) => r.methods))].sort() };
    });
  }
  /** Does every channel move together (tracking/availability) or is the change concentrated in one channel? */
  function attributeEvent(event, channelsDaily) {
    if (!channelsDaily || !channelsDaily.length) return { pattern: "unknown", explanation: "no channel-level daily data", channels: [] };
    const dates = channelsDaily.map((r) => String(r.date).slice(0, 10)).sort();
    const all = dayRange(dates[0], dates[dates.length - 1]);
    const idx = new Map(all.map((d, i) => [d, i]));
    const chans = [...new Set(channelsDaily.filter((r) => r.channel !== null && r.channel !== undefined).map((r) => String(r.channel)))].sort();
    const piv = Object.fromEntries(chans.map((c) => [c, all.map(() => 0)]));
    for (const r of channelsDaily) if (piv[r.channel] && isNum(r.sessions)) piv[r.channel][idx.get(String(r.date).slice(0, 10))] += r.sessions;
    const evDays = dayRange(event.start, event.end);
    const rows = [];
    for (const ch of chans) {
      const base = seasonalBaseline(piv[ch]);
      let act = 0, exp = 0;
      for (const d of evDays) { if (!idx.has(d)) continue; const i = idx.get(d); act += piv[ch][i]; if (!Number.isNaN(base[i])) exp += base[i]; }
      if (exp > 0) rows.push({ channel: ch, actual: act, expected: exp, deviation_pct: (act - exp) / exp, delta: act - exp });
    }
    if (!rows.length) return { pattern: "unknown", explanation: "not enough channel history", channels: [] };
    rows.sort((a, b) => a.delta - b.delta);
    const devs = rows.map((r) => r.deviation_pct);
    const sameDir = devs.filter((d) => Math.sign(d) === Math.sign(event.deviation_pct)).length / devs.length;
    const spread = stdPop(devs);
    let pattern, expl;
    if (sameDir >= 0.85 && spread <= 0.15 && Math.abs(event.deviation_pct) >= 0.3) {
      pattern = "all_channels";
      expl = `every channel moved together (${fmtF(Math.min(...devs) * 100, 0, { plus: true })}% to ${fmtF(Math.max(...devs) * 100, 0, { plus: true })}% vs expected) - ` +
        (event.direction === "drop" ? "consistent with a tracking or site-availability outage rather than a demand change" : "consistent with a site-wide event (bot traffic, tag duplication or PR coverage)");
    } else {
      const total = sum(rows.map((r) => Math.abs(r.delta))) || 1;
      const lead = [...rows].sort((a, b) => Math.abs(b.delta) - Math.abs(a.delta)).slice(0, 2);
      pattern = "concentrated";
      expl = "driven by " + lead.map((r) => `${r.channel} (${fmtF(r.deviation_pct * 100, 0, { plus: true })}%, ${fmtF((Math.abs(r.delta) / total) * 100, 0)}% of the change)`).join(" and ");
    }
    return { pattern, explanation: expl, channels: rows };
  }

  // ------------------------------------------------------------------ insights + grounding (pure)
  function deterministicInsights(kpis, channels, movers, events, compareLabel) {
    const out = [];
    const by = Object.fromEntries(kpis.map((k) => [k.metric, k]));
    for (const m of ["sessions", "conversions", "revenue"]) {
      const k = by[m];
      if (k && k.change_pct !== null) {
        const sig = k.significant === null ? "" : k.significant ? ", statistically significant" : ", within normal variation";
        const adj = k.adjusted_change_pct !== null && k.adjusted_change_pct !== undefined ? ` (${fmtChange(k.adjusted_change_pct)} excluding anomaly days)` : "";
        out.push(`${k.label} ${fmtNum(k.current, m === "revenue" ? "money" : "count")}: ${fmtChange(k.change_pct)} vs ${compareLabel}${sig}${adj}.`);
      }
    }
    for (const m of ["conversion_rate", "engagement_rate"]) {
      const k = by[m];
      if (k && k.previous !== null) {
        const pp = (k.current - k.previous) * 100;
        const sig = k.significant === null ? "" : k.significant ? ", significant" : ", not significant";
        out.push(`${k.label} ${fmtNum(k.current, "pct")} (${fmtF(pp, 2, { plus: true })} pp${sig}).`);
      }
    }
    for (const e of events) {
      const span = e.start === e.end ? e.start : `${e.start} to ${e.end}`;
      out.push(`Anomaly ${span}: ${e.reference_metric} ${fmtF(e.deviation_pct * 100, 0, { plus: true })}% vs expected (${fmtNum(e.actual)} vs ${fmtNum(e.expected)}); ${(e.attribution || {}).explanation || ""}.`.replace("; .", "."));
    }
    const shifts = channels.filter((c) => c.share_change_pp !== null && c.share_change_pp !== undefined);
    if (shifts.length) {
      const up = shifts.reduce((b, c) => (c.share_change_pp > b.share_change_pp ? c : b));
      const down = shifts.reduce((b, c) => (c.share_change_pp < b.share_change_pp ? c : b));
      if (Math.abs(up.share_change_pp) >= 0.5) out.push(`${up.channel} gained share: ${fmtF(up.share * 100, 1)}% of sessions (${fmtF(up.share_change_pp, 1, { plus: true })} pp).`);
      if (Math.abs(down.share_change_pp) >= 0.5 && down !== up) out.push(`${down.channel} lost share: ${fmtF(down.share * 100, 1)}% of sessions (${fmtF(down.share_change_pp, 1, { plus: true })} pp).`);
    }
    for (const p of movers.slice(0, 2)) if (p.sessions_change_pct !== null && Math.abs(p.sessions_change_pct) >= 0.1) out.push(`${p.page_path} sessions ${fmtChange(p.sessions_change_pct)} (${fmtNum(p.sessions_prev)} to ${fmtNum(p.sessions)}).`);
    return out;
  }
  function deterministicSummary(kpis, events, channels, movers, compareLabel) {
    const by = Object.fromEntries(kpis.map((k) => [k.metric, k]));
    const s = by.sessions, cr = by.conversion_rate;
    if (!s || !s.current) return "No traffic data was available for the selected period.";
    const parts = [];
    if (s.change_pct === null) parts.push(`The site recorded ${fmtNum(s.current)} sessions; no comparison data was available.`);
    else parts.push(`Sessions were ${fmtNum(s.current)}, ${fmtChange(s.change_pct)} vs ${compareLabel} (${s.significant ? "a statistically significant change" : "within normal day-to-day variation"}).`);
    const drops = events.filter((e) => e.direction === "drop");
    if (drops.length && s.adjusted_change_pct !== null && s.adjusted_change_pct !== undefined) {
      const e = drops[0], all = (e.attribution || {}).pattern === "all_channels";
      parts.push(`${e.start} to ${e.end} looks like ${all ? "a tracking or availability outage" : "an unexplained drop"} (${fmtF(e.deviation_pct * 100, 0, { plus: true })}% vs expected across ` +
        `${all ? "all channels" : "the site"}); excluding anomaly days the change is ${fmtChange(s.adjusted_change_pct)}.`);
    }
    for (const e of events.filter((x) => x.direction === "spike").slice(0, 1)) parts.push(`A spike on ${e.start} to ${e.end} (${fmtF(e.deviation_pct * 100, 0, { plus: true })}% vs expected) was ${(e.attribution || {}).explanation || "detected"}.`);
    if (cr && cr.previous !== null) parts.push(`Conversion rate was ${fmtNum(cr.current, "pct")} (${fmtF((cr.current - cr.previous) * 100, 2, { plus: true })} pp, ${cr.significant ? "significant" : "not significant"}).`);
    if (movers.length && movers[0].sessions_change_pct !== null) parts.push(`The largest page movement was ${movers[0].page_path} (${fmtChange(movers[0].sessions_change_pct)}).`);
    return parts.join(" ");
  }
  const NUM_RX = /(?<![\w.])[-+]?\$?\d[\d,]*(?:\.\d+)?%?/g;
  const toFloat = (tok) => { const t = tok.replace(/\$/g, "").replace(/,/g, "").replace(/\+/g, "").replace(/%+$/, ""); const v = Number(t); return t !== "" && Number.isFinite(v) ? v : null; };
  function factNumbers(facts) {
    const vals = new Set();
    for (const f of facts) for (const tok of String(f).match(NUM_RX) || []) { const v = toFloat(tok); if (v !== null) vals.add(pyRound(v, 2)); }
    return vals;
  }
  /** Numbers in model prose that are not (approximately) one of the computed facts; years and small ordinals ignored. */
  function ungroundedNumbers(text, allowed, tolerance = 0.051) {
    const bad = [], allow = [...allowed];
    for (const tok of String(text || "").match(NUM_RX) || []) {
      const v = toFloat(tok);
      if (v === null || (v >= 1900 && v <= 2100 && !tok.includes(".") && !tok.includes("%")) || (Math.abs(v) <= 10 && Number.isInteger(v) && !tok.includes("%"))) continue;
      if (!allow.some((a) => Math.abs(v - a) <= Math.max(tolerance, Math.abs(a) * 0.006))) bad.push(tok);
    }
    return bad;
  }
  function deterministicActions(kpis, channels, movers, events) {
    const acts = [];
    for (const e of events) {
      const att = e.attribution || {};
      if (e.direction === "drop" && att.pattern === "all_channels")
        acts.push({ title: `Investigate the ${e.start} tracking gap and add tag-health monitoring`, priority: "P1", rationale: `All channels fell about ${fmtF(Math.abs(e.deviation_pct) * 100, 0)}% together, which points at measurement rather than demand.`, owner_role: "Analytics engineer", metric_to_watch: "daily sessions vs expected" });
      else if (e.direction === "drop") acts.push({ title: `Diagnose the traffic drop on ${e.start}`, priority: "P1", rationale: att.explanation || "", owner_role: "Digital marketing lead", metric_to_watch: e.reference_metric });
      else acts.push({ title: `Learn from the spike on ${e.start}`, priority: "P2", rationale: att.explanation || "", owner_role: "Campaign manager", metric_to_watch: e.reference_metric });
    }
    const cr = Object.fromEntries(kpis.map((k) => [k.metric, k])).conversion_rate;
    if (cr && cr.previous !== null && cr.current < cr.previous && cr.significant)
      acts.push({ title: "Audit the quote funnel for the conversion-rate decline", priority: "P1", rationale: `Conversion rate fell ${fmtF((cr.current - cr.previous) * 100, 2)} pp and the change is statistically significant.`, owner_role: "CRO lead", metric_to_watch: "conversion rate" });
    for (const c of channels.filter((c) => (c.sessions_change_pct || 0) < -0.1 && c.share >= 0.05).slice(0, 2))
      acts.push({ title: `Review ${c.channel} performance`, priority: "P2", rationale: `${c.channel} sessions changed ${fmtChange(c.sessions_change_pct)} vs the comparison period.`, owner_role: "Channel owner", metric_to_watch: `${c.channel} sessions` });
    for (const p of movers.filter((p) => (p.sessions_change_pct || 0) < -0.15).slice(0, 1))
      acts.push({ title: `Recover traffic to ${p.page_path}`, priority: "P2", rationale: `Sessions ${fmtChange(p.sessions_change_pct)} vs the comparison period.`, owner_role: "SEO / content lead", metric_to_watch: `${p.page_path} sessions` });
    if (!acts.length) acts.push({ title: "Keep the current mix and test one growth lever", priority: "P3", rationale: "No significant declines or anomalies were detected.", owner_role: "Digital marketing lead", metric_to_watch: "sessions and conversion rate" });
    return acts;
  }

  // ------------------------------------------------------------------ charts (CAF.lib.chart) + HTML report
  function chartDaily(cur, expected, events, title, themed) {
    const idx = new Map(cur.map((d, i) => [d.date, i]));
    const points = [], markers = [];
    for (const e of events) {
      const ds = dayRange(e.start, e.end).filter((d) => idx.has(d)).map((d) => idx.get(d));
      points.push(...ds);
      if (ds.length) markers.push({ index: ds[0], label: `Anomaly ${fmtF(e.deviation_pct * 100, 0, { plus: true })}%` });
    }
    const series = [{ name: "Sessions", values: cur.map((d) => d.sessions), points }];
    if (expected && expected.some((v) => !nan(v))) series.push({ name: "Expected (same weekday)", values: expected.map((v) => (nan(v) ? null : v)) });
    return CAF.lib.chart.line({ series, labels: cur.map((d) => dayLabel(d.date)), title, markers, themed });
  }
  const E = U.esc;
  function renderReport(c) {
    const tiles = c.kpis.map((k) => `<div class="tile"><div class="l">${E(k.label)}</div><div class="v">${E(k.value)}</div>\n<div class="d ${k.css}">${E(k.delta)}<span class="sig">${E(k.sig)}</span></div></div>`).join("");
    const anomalies = c.anomalies.length ? `<section><h2>Anomalies detected</h2>${c.anomalies.map((a) => `<div class="alert"><b>${E(a.span)}</b> - ${E(a.reference_metric)} ${E(a.dev)} vs expected
(${E(a.actual)} vs ${E(a.expected)}; robust |z| up to ${E(a.z)}; methods: ${E(a.methods)}).<br>${E(a.explanation)}</div>`).join("")}</section>` : "";
    const chart = (svg, alt) => (svg ? `<figure role="img" aria-label="${E(alt)}" style="margin:6px 0">${svg}</figure>` : "");
    return `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Traffic report ${E(c.period.start)} to ${E(c.period.end)}</title>
<style>
:root{--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;--line:#e1e0d9;--bg:#f9f9f7;--card:#fcfcfb;--good:#006300;--bad:#d03b3b;--accent:#184f95}
*{box-sizing:border-box}body{margin:0;font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;color:var(--ink);background:var(--bg)}
header{background:var(--accent);color:#fff;padding:28px 32px 70px}header h1{margin:0 0 4px;font-size:24px}header p{margin:0;opacity:.85}
main{max-width:1120px;margin:-50px auto 0;padding:0 20px 48px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:18px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}.tile .v{font-size:24px;font-weight:600}
.tile .l{color:var(--ink2);font-size:13px}.d{font-size:13px;font-weight:600}.up{color:var(--good)}.down{color:var(--bad)}.flat{color:var(--ink2)}
.sig{display:block;font-size:11px;color:var(--muted);font-weight:400}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:20px 22px;margin-bottom:18px}
h2{font-size:18px;margin:0 0 12px}table{width:100%;border-collapse:collapse;font-size:14px;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:7px 9px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--ink2);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.03em}
td.n,th.n{text-align:right}svg{max-width:100%;height:auto;display:block}
.pill{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;font-weight:600;color:#fff}.P1{background:var(--bad)}.P2{background:#b54708}.P3{background:#475467}
.alert{border-left:4px solid var(--bad);padding:8px 12px;background:#fdf2f2;margin:8px 0;border-radius:4px}
.muted{color:var(--muted)}.note{font-size:13px;color:var(--ink2)}code{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12.5px}
</style></head><body>
<header><h1>Web traffic report</h1><p>${E(c.domain)} &middot; ${E(c.period.start)} to ${E(c.period.end)} (${c.period.days} days) vs ${E(c.compare_label)} ${E(c.comparison.start)} to ${E(c.comparison.end)} &middot; source: ${E(c.provider)}</p></header>
<main>
<div class="tiles">${tiles}</div>
<section><h2>Executive summary</h2><p>${E(c.executive_summary)}</p>
${c.highlights.length ? `<ul>${c.highlights.map((h) => `<li>${E(h)}</li>`).join("")}</ul>` : ""}
<p class="note">Narrative: ${E(c.narrative_label)}. Every number above was computed in code (ports of the pandas/scipy/statsmodels statistics).</p></section>
${anomalies}
${c.chart_daily ? `<section><h2>Daily sessions</h2>${chart(c.chart_daily, `Daily sessions for the report period${c.anomalies.length ? " with anomalies marked" : ""}`)}</section>` : ""}
<section><h2>Prioritised action plan</h2><table><thead><tr><th>Priority</th><th>Action</th><th>Owner</th><th>Metric to watch</th></tr></thead><tbody>
${c.actions.map((a) => `<tr><td><span class="pill ${E(a.priority)}">${E(a.priority)}</span></td><td><b>${E(a.title)}</b><br><span class="muted">${E(a.rationale)}</span></td><td>${E(a.owner_role)}</td><td>${E(a.metric_to_watch)}</td></tr>`).join("")}
</tbody></table></section>
<section><h2>Channel attribution</h2>${chart(c.chart_channels, "Sessions by channel")}
<table><thead><tr><th>Channel</th><th class="n">Sessions</th><th class="n">Change</th><th class="n">Share</th><th class="n">Share shift</th><th class="n">Conv.</th><th class="n">Conv. share</th><th class="n">CVR</th><th class="n">Revenue</th></tr></thead><tbody>
${c.channels.map((r) => `<tr><td>${E(r.channel)}</td><td class="n">${E(r.sessions)}</td><td class="n">${E(r.change)}</td><td class="n">${E(r.share)}</td><td class="n">${E(r.shift)}</td><td class="n">${E(r.conversions)}</td><td class="n">${E(r.conv_share)}</td><td class="n">${E(r.cvr)}</td><td class="n">${E(r.revenue)}</td></tr>`).join("")}
</tbody></table></section>
<section><h2>Top pages</h2>${chart(c.chart_pages, "Top pages by sessions")}
<table><thead><tr><th>Page</th><th class="n">Sessions</th><th class="n">Change</th><th class="n">Engagement</th><th class="n">Conv.</th><th class="n">CVR</th></tr></thead><tbody>
${c.pages.map((p) => `<tr><td><code>${E(p.page_path)}</code><br><span class="muted">${E(p.page_title)}</span></td><td class="n">${E(p.sessions)}</td><td class="n">${E(p.change)}</td><td class="n">${E(p.er)}</td><td class="n">${E(p.conversions)}</td><td class="n">${E(p.cvr)}</td></tr>`).join("")}
</tbody></table></section>
<section><h2>Data quality and method</h2><ul>${c.data_quality.map((q) => `<li>${E(q)}</li>`).join("")}
<li>Volumes compared with a Mann-Whitney U test on daily values; rates with a two-proportion z-test (alpha 0.05).</li>
<li>Anomalies: same-weekday rolling median with a MAD-based robust z-score and robust STL (weekly period) residuals, combined by the '${E(c.method)}' rule; threshold |z| &ge; ${E(c.threshold)} and at least ${E(c.min_change)} deviation from expected.</li>
${c.comparison_anomalies ? `<li>The comparison period itself contains anomalies (${E(c.comparison_anomalies)}); period-over-period changes are also shown excluding anomaly days.</li>` : ""}
</ul></section>
</main></body></html>`;
  }

  // ------------------------------------------------------------------ the agent
  CAF.agent(AGENT, {
    async run(input, ctx) {
      // 1. window
      const has = (n) => ctx.hasData(n);
      const [daily0, pages0, channels0] = await Promise.all(["analytics/daily", "analytics/pages", "analytics/channels"].map((n) => (has(n) ? ctx.data(n) : Promise.resolve([]))));
      const norm = (rows) => rows.filter((r) => r.date).map((r) => ({ ...r, date: String(r.date).slice(0, 10) }));
      const conn = makeProvider(norm(daily0), norm(pages0), norm(channels0));
      if (!has("analytics/daily")) ctx.warn("The analytics/daily export is not embedded in this console build.");
      const bounds = conn.dateBounds();
      let [start, end, notes] = resolveWindow(input.start_date || null, input.end_date || null, bounds, new Date().toISOString().slice(0, 10));
      if (bounds && (start < bounds[0] || end > bounds[1])) {
        if (start > bounds[1] || end < bounds[0]) { ctx.warn(`No analytics data in the requested period: the connector covers ${bounds[0]} to ${bounds[1]}.`); ctx.degraded("no analytics data for the requested period"); }
        else {
          start = start < bounds[0] ? bounds[0] : start; end = end > bounds[1] ? bounds[1] : end;
          ctx.warn(`Requested period extends beyond the available data (${bounds[0]} to ${bounds[1]}); report clipped to ${start} to ${end}.`);
        }
      }
      const compareTo = input.compare_to || "previous_period";
      const [cs, ce] = comparisonWindow(start, end, compareTo);
      if (bounds && cs < bounds[0]) ctx.warn(`Comparison period starts ${cs}, before the first day of data (${bounds[0]}); comparisons may be partial.`);
      // 2. load
      ctx.progress(`Loading analytics ${start} to ${end} (+ ${cs} to ${ce})`);
      const histStart = addDays(cs < start ? cs : start, -LOOKBACK_DAYS);
      const dailyAll = conn.daily(histStart, end);
      const pagesCur = conn.pages(start, end, 500), pagesPrev = conn.pages(cs, ce, 500);
      const chCur = conn.channels(start, end), chPrev = conn.channels(cs, ce);
      const chDaily = conn.channelsDaily(histStart, end);
      if (!dailyAll.length) { ctx.warn("The analytics connector returned no daily rows for this period."); ctx.degraded("no analytics data for the period"); }
      // 3. compute
      const [cur, missCur0] = completeDaily(dailyAll, start, end);
      const [prev, missPrev0] = completeDaily(dailyAll, cs, ce);
      const dq = [...notes];
      let missCur = missCur0, missPrev = missPrev0, prevAvailable;
      if (bounds) {
        missPrev = missPrev0.filter((d) => d >= bounds[0] && d <= bounds[1]);
        missCur = missCur0.filter((d) => d >= bounds[0] && d <= bounds[1]);
        prevAvailable = cs >= bounds[0];
      } else prevAvailable = sum(prev.map((d) => d.sessions)) > 0;
      if (missCur.length) dq.push(`${missCur.length} day(s) with no data in the report period: ${missCur.slice(0, 6).join(", ")}`);
      if (missPrev.length) dq.push(`${missPrev.length} day(s) with no data in the comparison period: ${missPrev.slice(0, 6).join(", ")}`);
      let prevUsed = prev;
      if (!prevAvailable || sum(prev.map((d) => d.sessions)) === 0) { dq.push("No comparison data available: changes are not computed."); ctx.degraded("no analytics data for the comparison period"); prevUsed = null; }
      const channels = channelTable(chCur, chPrev.length && prevUsed ? chPrev : null);
      const [topPages, movers0] = pageTable(pagesCur, pagesPrev.length && prevUsed ? pagesPrev : null, Number(input.top_pages || 10));
      const movers = prevUsed ? movers0 : [];
      // 4. anomalies
      let events = [], cmpEvents = [], kpis;
      if (!dailyAll.length) kpis = compareKpis(cur, null);
      else {
        ctx.progress("Detecting anomalies (rolling median + MAD and robust STL)");
        const lo = bounds ? bounds[0] : dailyAll[0].date;
        const fullStart = addDays(cs < start ? cs : start, -LOOKBACK_DAYS);
        const [full] = completeDaily(dailyAll, lo > fullStart ? lo : fullStart, end);
        const flags = detectAnomalies(full, ANOMALY_METRICS, { method: input.anomaly_method || "consensus", threshold: Number(input.anomaly_threshold ?? 3.5), minChange: Number(input.anomaly_min_change ?? 0.15) });
        events = groupEvents(flags, start, end);
        cmpEvents = prevUsed ? groupEvents(flags, cs, ce) : [];
        for (const e of [...events, ...cmpEvents]) e.attribution = attributeEvent(e, chDaily);
        for (const e of events) ctx.check({ rule: "traffic.anomaly", status: "warn", severity: Math.abs(e.deviation_pct) >= 0.3 ? "high" : "medium",
          message: `${cap1(e.direction)} ${e.start} to ${e.end}: ${e.reference_metric} ${fmtF(e.deviation_pct * 100, 0, { plus: true })}% vs expected; ${e.attribution.explanation}`,
          target: e.reference_metric, data: { max_abs_z: pyRound(e.max_abs_z, 2), methods: e.methods } });
        if (!events.length) ctx.check({ rule: "traffic.anomaly", status: "pass", severity: "low", message: "No anomalies in the report period." });
        const bad = new Set([...events, ...cmpEvents].flatMap((e) => dayRange(e.start, e.end)));
        const curAdj = bad.size ? cur.filter((d) => !bad.has(d.date)) : null;
        const prevAdj = bad.size && prevUsed ? prevUsed.filter((d) => !bad.has(d.date)) : null;
        kpis = compareKpis(cur, prevUsed, curAdj, prevAdj);
        for (const k of kpis) if (k.change_pct !== null && k.significant && k.change_pct < -0.05 && ["sessions", "conversions", "revenue", "conversion_rate"].includes(k.metric))
          ctx.check({ rule: `traffic.kpi.${k.metric}`, status: "warn", severity: "medium", message: `${k.label} ${fmtChange(k.change_pct)} vs comparison period (${k.test}, p=${fmtG(k.p_value, 3)}).` });
      }
      // 5. narrate (model interprets; never calculates)
      const compareLabel = compareTo === "previous_year" ? "the previous year" : "the previous period";
      const insights = deterministicInsights(kpis, channels, movers, events, compareLabel);
      const kpiLines = kpis.map((k) => {
        const kind = k.kind === "rate" ? "pct" : k.metric === "revenue" ? "money" : "count";
        let line = `${k.label}: ${fmtNum(k.current, kind)}`;
        if (k.previous !== null) {
          line += ` (previous ${fmtNum(k.previous, kind)}, change ${fmtChange(k.change_pct)}`;
          if (k.p_value !== null) line += `, ${k.significant ? "significant" : "not significant"} p=${fmtF(k.p_value, 3)}`;
          if (k.adjusted_change_pct !== null) line += `, ${fmtChange(k.adjusted_change_pct)} excluding anomaly days`;
          line += ")";
        }
        return line;
      });
      const channelLines = channels.map((c) => `${c.channel}: ${fmtNum(c.sessions)} sessions (${fmtF(c.share * 100, 1)}% share` + (c.share_change_pp !== null ? `, ${fmtF(c.share_change_pp, 1, { plus: true })} pp` : "") +
        `), ${fmtNum(c.conversions)} conversions, CVR ${fmtF(c.conversion_rate * 100, 2)}%` + (c.sessions_change_pct !== null ? `, sessions ${fmtChange(c.sessions_change_pct)}` : ""));
      const pageLines = topPages.map((p) => `${p.page_path}: ${fmtNum(p.sessions)} sessions` + (p.sessions_change_pct !== null ? ` (${fmtChange(p.sessions_change_pct)})` : "") + `, CVR ${fmtF(p.conversion_rate * 100, 2)}%`);
      const anomalyLines = events.map((e) => `${e.start} to ${e.end}: ${e.reference_metric} ${fmtF(e.deviation_pct * 100, 0, { plus: true })}% vs expected (${fmtNum(e.actual)} vs ${fmtNum(e.expected)}); ${e.attribution.explanation}`);
      ctx.progress("Writing the executive narrative from the computed facts");
      const narrative = await ctx.generate("summary", { domain: input.domain || "the website", start, end, compare_label: compareLabel, cmp_start: cs, cmp_end: ce, kpis: kpiLines,
        channels: channelLines, pages: pageLines, anomalies: anomalyLines, insights, data_quality: dq });
      const detActions = deterministicActions(kpis, channels, movers, events);
      let summary, highlights, actions, source, ungrounded = [];
      if (ctx.llmLive) {
        const allowed = factNumbers([...kpiLines, ...channelLines, ...pageLines, ...anomalyLines, ...insights, ...dq, start, end, cs, ce]);
        const prose = [narrative.executive_summary || "", ...(narrative.highlights || []), ...(narrative.actions || []).map((a) => a.rationale || "")].join(" ");
        ungrounded = [...new Set(ungroundedNumbers(prose, allowed))].sort();
        ctx.check({ rule: "narrative.numbers_grounded", status: ungrounded.length ? "warn" : "pass", severity: "medium",
          message: ungrounded.length ? `Numbers not found in the computed facts: ${ungrounded.join(", ")}` : "Every number in the narrative matches a computed fact." });
        summary = String(narrative.executive_summary || "").trim() || insights.slice(0, 3).join(" ");
        highlights = (narrative.highlights || []).filter((h) => String(h).trim());
        if (!highlights.length) highlights = insights.slice(0, 5);
        actions = (narrative.actions || []).filter((a) => String(a.title || "").trim()).map((a) => ({ title: a.title, rationale: a.rationale || "", priority: a.priority || "P2", owner_role: a.owner_role || "", metric_to_watch: a.metric_to_watch || "" }));
        if (!actions.length) actions = detActions;
        source = "model";
      } else {
        summary = deterministicSummary(kpis, events, channels, movers, compareLabel);
        highlights = insights.slice(0, 6); actions = detActions; source = "computed";
      }
      // 6. report
      const days = dnum(end) - dnum(start) + 1, cdays = dnum(ce) - dnum(cs) + 1;
      const anomalyModel = (e, inCmp) => ({ start: e.start, end: e.end, days: e.days, direction: e.direction, metrics: e.metrics, reference_metric: e.reference_metric, actual: e.actual,
        expected: pyRound(e.expected, 1), deviation_pct: pyRound(e.deviation_pct, 4), max_abs_z: pyRound(e.max_abs_z, 2), methods: e.methods, pattern: e.attribution.pattern,
        explanation: e.attribution.explanation, in_comparison_period: inCmp });
      const anomalies = events.map((e) => anomalyModel(e, false)), cmpAnoms = cmpEvents.map((e) => anomalyModel(e, true));
      const findings = anomalies.map((a) => ({ title: `Traffic ${a.direction} ${a.start} to ${a.end} (${fmtF(a.deviation_pct * 100, 0, { plus: true })}% ${a.reference_metric})`, detail: a.explanation,
        severity: Math.abs(a.deviation_pct) >= 0.3 ? "high" : "medium", category: "analytics",
        evidence: `${fmtF(a.actual, 0, { comma: true })} vs ${fmtF(a.expected, 0, { comma: true })} expected; robust |z| ${fmtF(a.max_abs_z, 1)} (${a.methods.join(", ")})`,
        recommendation: a.pattern === "all_channels" ? "Check tag firing, consent banner and site availability for those dates; annotate the report." : "Review the channels driving the change and campaign calendars for those dates.",
        target: a.reference_metric, source_agent: AGENT }));
      for (const k of kpis) if (k.change_pct !== null && k.significant && Math.abs(k.change_pct) >= 0.05)
        findings.push({ title: `${k.label} ${fmtChange(k.change_pct)} vs ${compareLabel}`, detail: `${k.test}, p=${fmtG(k.p_value, 3)}`, severity: k.change_pct < 0 ? "medium" : "low", category: "analytics",
          evidence: `${fmtG(k.current, 4, true)} vs ${fmtG(k.previous, 4, true)}`, recommendation: "", target: k.metric, source_agent: AGENT });
      let utility;
      try { utility = new RegExp(input.non_conversion_paths || "^$", "i"); } catch { utility = /^$/; ctx.warn("non_conversion_paths is not a valid regular expression; no pages were excluded."); }
      for (const p of topPages) if (p.sessions >= 500 && p.conversion_rate < 0.01 && !utility.test(p.page_path))
        findings.push({ title: `High-traffic page with low conversion: ${p.page_path}`, severity: "medium", category: "analytics", detail: `${fmtF(p.sessions, 0, { comma: true })} sessions, CVR ${fmtF(p.conversion_rate * 100, 2)}%`,
          evidence: `engagement rate ${fmtF(p.engagement_rate * 100, 0)}%`, target: p.page_path, source_agent: AGENT, recommendation: "Test clearer calls to action or next steps on this page." });
      const PAGE_KEYS = ["page_path", "page_title", "sessions", "sessions_prev", "sessions_change_pct", "conversions", "conversion_rate", "engagement_rate", "status"];
      const CH_KEYS = ["channel", "sessions", "sessions_prev", "sessions_change_pct", "share", "share_prev", "share_change_pp", "conversions", "conversion_share", "conversion_rate", "conversion_rate_prev", "revenue"];
      const pick = (o, keys) => Object.fromEntries(keys.map((k) => [k, o[k] === undefined ? null : o[k]]));
      const output = {
        provider: conn.provider, domain: input.domain || null, period: { start, end, days }, comparison: { start: cs, end: ce, days: cdays }, compare_to: compareTo, kpis,
        top_pages: topPages.map((p) => pick(p, PAGE_KEYS)), movers: movers.map((p) => pick(p, PAGE_KEYS)), channels: channels.map((c) => pick(c, CH_KEYS)),
        anomalies, comparison_anomalies: cmpAnoms, data_quality: dq, insights, executive_summary: summary, highlights,
        action_plan: actions.map((a) => ({ title: a.title || "", rationale: a.rationale || "", priority: a.priority || "P2", owner_role: a.owner_role || "", metric_to_watch: a.metric_to_watch || "" })),
        narrative_source: source, ungrounded_numbers: ungrounded, findings,
      };
      // charts: history baseline for the "expected" line, like the Python chart
      const charts = { chart_daily: "", chart_channels: "", chart_pages: "" };
      let viewChart = "";
      if (input.include_charts !== false && cur.length) {
        try {
          const histDays = dailyAll.length ? completeDaily(dailyAll, dailyAll[0].date, dailyAll[dailyAll.length - 1].date)[0] : [];
          const histBase = seasonalBaseline(histDays.map((d) => d.sessions));
          const baseBy = new Map(histDays.map((d, i) => [d.date, histBase[i]]));
          const expected = histDays.length ? cur.map((d) => (baseBy.has(d.date) ? baseBy.get(d.date) : NaN)) : null;
          const title = `Daily sessions, ${start} to ${end}`;
          charts.chart_daily = chartDaily(cur, expected, events, title, false);
          viewChart = chartDaily(cur, expected, events, title, true);
          charts.chart_channels = CAF.lib.chart.bars({ items: channels.slice(0, 8).map((c) => ({ label: c.channel, value: c.sessions })), title: "Sessions by channel", themed: false, format: (v) => fmtF(v, 0, { comma: true }) });
          charts.chart_pages = CAF.lib.chart.bars({ items: topPages.slice(0, 10).map((p) => ({ label: p.page_path, value: p.sessions })), title: "Top pages by sessions", themed: false, format: (v) => fmtF(v, 0, { comma: true }) });
        } catch (e) { ctx.log(`chart rendering failed: ${e.message}`); }
      }
      const tile = (k) => {
        const kind = k.kind === "rate" ? "pct" : k.metric === "revenue" ? "money" : "count";
        if (k.change_pct === null) return { label: k.label, value: fmtNum(k.current, kind), delta: "no comparison", css: "flat", sig: "" };
        const up = k.change_pct >= 0;
        const delta = k.kind === "rate" ? `${up ? "▲" : "▼"} ${fmtF((k.current - k.previous) * 100, 2, { plus: true })} pp` : `${up ? "▲" : "▼"} ${fmtChange(k.change_pct)}`;
        let sig = k.significant === null ? "" : k.significant ? "significant" : "not significant";
        if (k.adjusted_change_pct !== null) sig += ` · ${fmtChange(k.adjusted_change_pct)} excl. anomalies`;
        return { label: k.label, value: fmtNum(k.current, kind), delta, css: Math.abs(k.change_pct) < 0.005 ? "flat" : up ? "up" : "down", sig };
      };
      const html = renderReport({
        domain: input.domain || "All traffic", period: output.period, comparison: output.comparison, compare_label: compareLabel, provider: output.provider, kpis: kpis.map(tile),
        executive_summary: summary, highlights,
        narrative_label: source === "model" ? "written by a language model from the computed facts" + (ungrounded.length ? ` (ungrounded numbers flagged: ${ungrounded.join(", ")})` : "") : "generated from the computed facts (no model configured)",
        anomalies: anomalies.map((a) => ({ span: a.start === a.end ? a.start : `${a.start} to ${a.end}`, reference_metric: a.reference_metric, dev: `${fmtF(a.deviation_pct * 100, 0, { plus: true })}%`,
          actual: fmtF(a.actual, 0, { comma: true }), expected: fmtF(a.expected, 0, { comma: true }), z: fmtF(a.max_abs_z, 1), methods: a.methods.join(", "), explanation: a.explanation })),
        actions: output.action_plan, ...charts,
        channels: output.channels.map((c) => ({ channel: c.channel, sessions: fmtF(c.sessions, 0, { comma: true }), change: fmtChange(c.sessions_change_pct), share: `${fmtF(c.share * 100, 1)}%`,
          shift: c.share_change_pp === null ? "n/a" : `${fmtF(c.share_change_pp, 1, { plus: true })} pp`, conversions: fmtF(c.conversions, 0, { comma: true }), conv_share: `${fmtF(c.conversion_share * 100, 1)}%`,
          cvr: `${fmtF(c.conversion_rate * 100, 2)}%`, revenue: `$${fmtF(c.revenue, 0, { comma: true })}` })),
        pages: output.top_pages.map((p) => ({ page_path: p.page_path, page_title: p.page_title, sessions: fmtF(p.sessions, 0, { comma: true }), change: p.status !== "new" ? fmtChange(p.sessions_change_pct) : "new",
          er: `${fmtF(p.engagement_rate * 100, 0)}%`, conversions: fmtF(p.conversions, 0, { comma: true }), cvr: `${fmtF(p.conversion_rate * 100, 2)}%` })),
        data_quality: dq, threshold: Number(input.anomaly_threshold ?? 3.5), method: input.anomaly_method || "consensus", min_change: `${fmtF(Number(input.anomaly_min_change ?? 0.15) * 100, 0)}%`,
        comparison_anomalies: cmpAnoms.map((a) => `${a.start} to ${a.end} (${fmtF(a.deviation_pct * 100, 0, { plus: true })}%)`).join("; ") });
      ctx.artifact({ name: "traffic_report.html", mime: "text/html", content: html, description: "Executive traffic report" });
      if (charts.chart_daily) ctx.artifact({ name: "daily_sessions.svg", mime: "image/svg+xml", content: charts.chart_daily, description: "Daily sessions with anomalies" });
      const KPI_COLS = ["metric", "label", "current", "previous", "change_abs", "change_pct", "adjusted_change_pct", "p_value", "significant", "test", "kind", "note"];
      const pyCell = (v) => (v === null || v === undefined ? "" : typeof v === "boolean" ? (v ? "True" : "False") : typeof v === "number" && Number.isInteger(v) ? v.toFixed(1) : v);
      ctx.artifact({ name: "kpis.csv", mime: "text/csv", content: CAF.lib.csv.stringify(kpis.map((k) => Object.fromEntries(KPI_COLS.map((c) => [c, ["metric", "label", "test", "kind", "note"].includes(c) ? k[c] : pyCell(k[c])]))), KPI_COLS), description: "KPI comparison with significance tests" });
      const by = Object.fromEntries(kpis.map((k) => [k.metric, k]));
      const s = by.sessions;
      const summaryLine = `${start} to ${end}: ${fmtF(s.current, 0, { comma: true })} sessions (${fmtChange(s.change_pct)} vs ${compareLabel}), CVR ${fmtF(by.conversion_rate.current * 100, 2)}%, ${anomalies.length} anomaly event(s)` +
        (anomalies.length ? ` incl. ${anomalies[0].start} to ${anomalies[0].end} (${fmtF(anomalies[0].deviation_pct * 100, 0, { plus: true })}%)` : "");
      const view = [
        { type: "kv", title: "Period", data: { report: `${start} to ${end} (${days} days)`, comparison: `${cs} to ${ce} (${compareLabel})`, source: `${output.provider} (embedded analytics exports)`, narrative: source } },
        { type: "table", title: "KPIs", rows: kpis.map((k) => ({ kpi: k.label, current: fmtNum(k.current, k.kind === "rate" ? "pct" : k.metric === "revenue" ? "money" : "count"), change: fmtChange(k.change_pct),
          "excl. anomalies": fmtChange(k.adjusted_change_pct), p_value: k.p_value === null ? "" : fmtG(k.p_value, 3), significant: k.significant === null ? "" : k.significant ? "yes" : "no", test: k.test })) },
        { type: "markdown", title: "Executive summary", markdown: [summary, "", ...highlights.map((h) => `- ${h}`)].join("\n") },
      ];
      if (viewChart) view.push({ type: "svg", title: "Daily sessions", svg: viewChart });
      if (anomalies.length) view.push({ type: "table", title: "Anomalies", rows: anomalies.map((a) => ({ span: `${a.start} to ${a.end}`, metric: a.reference_metric, deviation: `${fmtF(a.deviation_pct * 100, 0, { plus: true })}%`, actual: a.actual, expected: a.expected, pattern: a.pattern, methods: a.methods.join(", ") })) });
      view.push(
        { type: "table", title: "Prioritised action plan", rows: output.action_plan.map((a) => ({ priority: a.priority, action: a.title, owner: a.owner_role, watch: a.metric_to_watch })) },
        { type: "table", title: "Channels", rows: output.channels.map((c) => ({ channel: c.channel, sessions: c.sessions, change: fmtChange(c.sessions_change_pct), share: `${fmtF(c.share * 100, 1)}%`, cvr: `${fmtF(c.conversion_rate * 100, 2)}%`, revenue: c.revenue })) },
        { type: "table", title: "Top pages", rows: output.top_pages.map((p) => ({ page: p.page_path, sessions: p.sessions, change: p.status === "new" ? "new" : fmtChange(p.sessions_change_pct), cvr: `${fmtF(p.conversion_rate * 100, 2)}%` })) },
        { type: "html", title: "Report (traffic_report.html)", artifact: "traffic_report.html" });
      return { output, summary: summaryLine, view };
    },
  });
})();
