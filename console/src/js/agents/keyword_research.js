/* Keyword Research - browser twin of caf/agents/strategy/keyword_research.py.
   Same deterministic core: Search-Console aggregation, modifier-aware relevance gate, intent rules
   and the CTR-curve opportunity model from the keyword_intent skill, TF-IDF (word + char_wb +
   concept tags) clustering with a port of sklearn's HDBSCAN (leaf / eom) and average-linkage
   AgglomerativeClustering. Data: keywords/gsc_queries (= keyword_data:csv) and search/fixture_results
   (= the fixture search provider the Python runtime uses by default). */
(() => {
  const T = CAF.lib.text;
  const INTENTS = ["navigational", "transactional", "commercial", "informational"];
  const cmp = (a, b) => (a < b ? -1 : a > b ? 1 : 0);
  const cmpTuple = (a, b) => { for (let i = 0; i < a.length; i++) { const c = cmp(a[i], b[i]); if (c) return c; } return 0; };
  const maxBy = (rows, key) => rows.reduce((best, r) => (best === null || cmpTuple(key(r), key(best)) > 0 ? r : best), null);
  /** Python format(x, f".{d}f") - correctly rounded, half-even on exact binary ties (JS toFixed rounds them up). */
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
  const round = (x, d) => Number(pyFixed(x, d));
  const pyFloat = (x) => (x === null || x === undefined ? "None" : Number.isInteger(x) ? `${x}.0` : String(x));
  const cap1 = (s) => (s ? s[0].toUpperCase() + s.slice(1).toLowerCase() : s);
  const pct = (x) => `${pyFixed(Number(x || 0) * 100, 1)}%`;

  // ---------------------------------------------------------------- text helpers
  function normalizeKeyword(text) {
    let t = T.normQuotes(String(text ?? "")).normalize("NFKC").toLowerCase();
    t = t.replace(/[^a-z0-9&+' -]+/g, " ").replace(/\s+/g, " ").replace(/^[ \-']+|[ \-']+$/g, "");
    return t.slice(0, 80).trim();
  }
  function stem(token) {
    let t = String(token).toLowerCase().replace(/^'+|'+$/g, "");
    if (t.endsWith("'s")) t = t.slice(0, -2);
    if (t.length > 4 && t.endsWith("ies")) return t.slice(0, -3) + "y";
    if (t.length > 4 && /(?:ches|shes|sses|xes|zes)$/.test(t)) return t.slice(0, -2);
    if (t.length > 3 && t.endsWith("s") && !/(?:ss|us|is)$/.test(t)) return t.slice(0, -1);
    return t;
  }
  const keywordTokens = (k) => normalizeKeyword(k).split(" ").filter(Boolean).map(stem);
  const keywordKey = (k) => keywordTokens(k).join(" ");
  function tokensMatch(a, b) {
    if (a === b) return true;
    const [s, l] = a.length <= b.length ? [a, b] : [b, a];
    if (s.length >= 4 && l.startsWith(s)) return true;
    let n = 0;
    for (let i = 0; i < Math.min(a.length, b.length) && a[i] === b[i]; i++) n++;
    return n >= 5;
  }
  function hasPhrase(keyword, phrase) {
    const kw = keywordTokens(keyword), ph = keywordTokens(phrase);
    if (!ph.length || ph.length > kw.length) return false;
    for (let i = 0; i + ph.length <= kw.length; i++) if (ph.every((t, j) => kw[i + j] === t)) return true;
    return false;
  }
  /** urllib.parse.urlsplit -> (netloc lowercased, path) */
  function splitUrl(u) {
    const s = String(u || "").trim();
    const m = s.match(/^(?:[A-Za-z][A-Za-z0-9+.-]*:)?\/\/([^/?#]*)([^?#]*)/);
    return m ? { host: m[1].toLowerCase(), path: m[2] } : { host: "", path: s.split(/[?#]/)[0] };
  }
  const bareHost = (u) => splitUrl(u).host.replace(/^www\./, "");
  function samePage(a, b) {
    if (!a || !b) return false;
    const pa = splitUrl(a), pb = splitUrl(b);
    const ha = pa.host.replace(/^www\./, ""), hb = pb.host.replace(/^www\./, "");
    if (ha && hb && ha !== hb) return false;
    const norm = (p) => (p || "/").replace(/\/+$/, "") || "/";
    return norm(pa.path) === norm(pb.path);
  }
  function num(v, d = 0) {
    if (v === null || v === undefined || v === "" || typeof v === "boolean") return v === true ? 1 : v === false ? 0 : d;
    const f = Number(v);
    return Number.isNaN(f) ? d : f;
  }

  // ---------------------------------------------------------------- embedded connectors (keyword_data:csv, search:fixture)
  /** CsvKeywordData.queries: topic-token filter (applied only when something matches), impressions desc. */
  function csvQueries(rows, topic, limit = 1000) {
    let df = rows.map((r) => Object.fromEntries(Object.entries(r).map(([k, v]) => [String(k).trim().toLowerCase(), v])));
    if (df.length && !("ctr" in df[0]) && "clicks" in df[0] && "impressions" in df[0])
      df = df.map((r) => ({ ...r, ctr: round(num(r.clicks) / Math.max(1, num(r.impressions)), 4) }));
    if (topic) {
      const toks = new Set(String(topic).toLowerCase().split(/\s+/).filter((t) => t.length > 2));
      const hit = df.filter((r) => String(r.query ?? "").toLowerCase().split(/\s+/).some((w) => toks.has(w)));
      if (hit.length) df = hit;
    }
    return df.sort((a, b) => num(b.impressions) - num(a.impressions)).slice(0, limit);
  }
  /** FixtureSearch.search: 3 x query-term overlap + substring hits; ties newest first; recency vs reference_date. */
  function fixtureSearch(items, query, { num: n = 10, recencyDays = null, ref = "2026-10-18" } = {}) {
    const q = [...new Set(String(query).toLowerCase().replace(/-/g, " ").split(/\s+/).filter((t) => t.length > 2))];
    const scored = [];
    for (const it of items || []) {
      const terms = new Set((it.query_terms || []).map((t) => String(t).toLowerCase()));
      const hay = `${it.title ?? ""} ${it.snippet ?? ""}`.toLowerCase();
      const score = 3 * q.filter((t) => terms.has(t)).length + q.filter((t) => hay.includes(t)).length;
      if (score) scored.push([score, it.published || "", it]);
    }
    scored.sort((a, b) => b[0] - a[0] || cmp(b[1], a[1]));
    let results = scored.map(([, , it]) => ({ title: it.title, url: it.url, snippet: it.snippet ?? "", published: it.published ?? null, source: it.source ?? null, provider: "fixture" }));
    if (recencyDays !== null && recencyDays !== undefined) {
      const day = (iso) => { const m = String(iso || "").match(/^(\d{4})-(\d{2})-(\d{2})/); return m ? Date.UTC(+m[1], +m[2] - 1, +m[3]) / 864e5 : null; };
      results = results.filter((r) => !r.published || day(r.published) === null || day(r.published) >= day(ref) - recencyDays);
    }
    return results.slice(0, n);
  }

  // ---------------------------------------------------------------- deterministic core
  function aggregateQueries(rows, pageUrl = null, minImpressions = 1) {
    const groups = new Map();
    for (const r of rows) {
      const kw = normalizeKeyword(r.query);
      if (!kw) continue;
      const imp = num(r.impressions, 0) || 0, clicks = num(r.clicks, 0) || 0, pos = num(r.position, null);
      const key = keywordKey(kw);
      if (!groups.has(key)) groups.set(key, { variants: new Map(), impressions: 0, clicks: 0, posW: 0, posImp: 0, pages: new Map() });
      const g = groups.get(key);
      g.variants.set(kw, (g.variants.get(kw) || 0) + imp);
      g.impressions += imp; g.clicks += clicks;
      if (pos !== null && imp > 0) { g.posW += pos * imp; g.posImp += imp; }
      const page = String(r.page || "").trim();
      if (page && page.toLowerCase() !== "nan") {
        if (!g.pages.has(page)) g.pages.set(page, { page, impressions: 0, clicks: 0 });
        const p = g.pages.get(page); p.impressions += imp; p.clicks += clicks;
      }
    }
    const out = [];
    for (const g of groups.values()) {
      const imp = g.impressions;
      if (imp < minImpressions) continue;
      const pages = [...g.pages.values()].sort((a, b) => b.clicks - a.clicks || b.impressions - a.impressions || cmp(a.page, b.page));
      const major = pages.filter((p) => imp && p.impressions / imp >= 0.2);
      const keyword = [...g.variants.entries()].sort((a, b) => b[1] - a[1] || cmp(a[0], b[0]))[0][0];
      out.push({ keyword, impressions: Math.round(imp), clicks: Math.round(g.clicks), ctr: imp ? round(g.clicks / imp, 4) : 0,
        position: g.posImp ? round(g.posW / g.posImp, 2) : null, page: pages.length ? pages[0].page : null, pages: pages.map((p) => p.page),
        cannibalised: major.length >= 2, on_target_page: pages.some((p) => samePage(p.page, pageUrl)) });
    }
    return out.sort((a, b) => b.impressions - a.impressions || cmp(a.keyword, b.keyword));
  }
  function modifierTokens(rules) {
    const out = new Set();
    for (const spec of Object.values(rules.intents || {})) for (const ph of [...((spec || {}).phrases || []), ...((spec || {}).starts_with || [])]) keywordTokens(ph).forEach((t) => out.add(t));
    return out;
  }
  function relevanceScores(keywords, references, { ignore = new Set(), synonyms = {} } = {}) {
    const canon = new Map();
    for (const [c, lst] of Object.entries(synonyms || {})) for (const s of [c, ...(lst || [])]) canon.set(stem(s), stem(c));
    const c = (t) => (canon.has(t) ? canon.get(t) : t);
    const tokenSets = keywords.map((k) => new Set(keywordTokens(k).map(c)));
    const refs = references.filter(Boolean).map((r) => [...new Set(keywordTokens(r))].filter((t) => !T.STOP.has(t) && !ignore.has(t)).map(c)).filter((r) => r.length);
    if (!refs.length) return keywords.map(() => 1);
    const pool = [...tokenSets, ...refs.map((r) => new Set(r))];
    const df = new Map();
    for (const s of pool) for (const t of s) df.set(t, (df.get(t) || 0) + 1);
    const idf = (t) => Math.log((1 + pool.length) / (1 + (df.get(t) || 0))) + 1;
    return tokenSets.map((toks) => {
      let best = 0;
      for (const ref of refs) {
        let total = 0, hit = 0;
        for (const t of ref) { total += idf(t); if ([...toks].some((k) => tokensMatch(t, k))) hit += idf(t); }
        best = Math.max(best, total ? hit / total : 0);
      }
      return round(Math.min(best, 1), 3);
    });
  }
  function classifyIntent(keyword, rules, brandTerms = [], competitorTerms = []) {
    const kw = normalizeKeyword(keyword);
    for (const term of brandTerms) if (term && hasPhrase(kw, term)) return ["navigational", `brand:${term}`];
    for (const term of competitorTerms) if (term && hasPhrase(kw, term)) return ["commercial", `competitor:${term}`];
    const first = keywordTokens(kw);
    for (const intent of rules.precedence || INTENTS) {
      const spec = (rules.intents || {})[intent] || {};
      for (const sw of spec.starts_with || []) { const st = keywordTokens(sw); if (st.length && st.length <= first.length && st.every((t, i) => first[i] === t)) return [intent, `starts_with:${sw}`]; }
      for (const ph of spec.phrases || []) if (hasPhrase(kw, ph)) return [intent, `phrase:${ph}`];
    }
    return [rules.default_intent || "informational", "default"];
  }
  function brandTermsFrom(brand, extra = []) {
    const terms = [normalizeKeyword(brand.name || "")];
    const host = bareHost(String(brand.website || "")).split(".")[0];
    if (host) terms.push(normalizeKeyword(host.replace(/-/g, " ")), host.replace(/-/g, ""));
    for (const t of extra || []) terms.push(normalizeKeyword(t));
    return [...new Set(terms)].filter(Boolean);
  }
  function expectedCtr(position, curve) {
    if (position === null || position === undefined || !curve || !curve.length) return 0;
    const pts = curve.map(([p, c]) => [Number(p), Number(c)]).sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    if (position <= pts[0][0]) return pts[0][1];
    for (let i = 0; i + 1 < pts.length; i++) { const [p0, c0] = pts[i], [p1, c1] = pts[i + 1]; if (position <= p1) return c0 + ((c1 - c0) * (position - p0)) / (p1 - p0); }
    return pts[pts.length - 1][1];
  }
  function positionBand(position, bands) {
    if (position !== null && position !== undefined) for (const b of bands) if (position <= Number(b.max_position)) return b;
    return bands.length ? bands[bands.length - 1] : { label: "unknown", ease: 0.5 };
  }
  /** Opportunity = extra clicks at the target position x ease(band) x intent weight [x estimate discount]. */
  function scoreKeyword({ impressions, ctr, position, intent, isEstimate, model }) {
    const curve = model.expected_ctr_curve, target = Number(model.target_position ?? 3);
    const pos = position !== null && position !== undefined ? position : Number((model.estimate_defaults || {}).position ?? 100);
    const band = positionBand(pos, model.position_bands || []);
    const expNow = expectedCtr(pos, curve), expTarget = expectedCtr(pos <= target ? 1 : target, curve);
    const actual = Number(ctr || 0);
    const gain = Math.max(0, expTarget - actual) * Number(impressions || 0);
    let value = gain * Number(band.ease ?? 0.5) * Number((model.intent_weights || {})[intent] ?? 0.7);
    if (isEstimate) value *= Number(model.estimate_discount ?? 0.5);
    let kind;
    if (intent === "navigational") kind = "brand";
    else if (gain <= 0) kind = "hold";
    else if (isEstimate || band.label === "not_ranking") kind = "new_content";
    else if (pos <= target) kind = actual < expNow * Number(model.ctr_gap_ratio ?? 0.7) ? "ctr_gap" : "push_to_first";
    else if (pos <= 10) kind = "page_one_push";
    else if (pos <= 20) kind = "striking_distance";
    else kind = "long_shot";
    return { expected_ctr: round(expNow, 4), potential_clicks: round(gain, 1), value, position_band: band.label ?? "", opportunity_type: kind };
  }
  function normaliseScores(values) { const top = Math.max(0, ...values); return values.map((v) => (top > 0 ? round((100 * v) / top, 1) : 0)); }
  const conceptTags = (keyword, concepts) => Object.entries(concepts || {}).filter(([, terms]) => (terms || []).some((t) => hasPhrase(keyword, t))).map(([name]) => name);

  // ---------------------------------------------------------------- TfidfVectorizer(sublinear_tf, smooth idf, l2) parity
  function tfidf(docTerms) {
    const N = docTerms.length, df = new Map();
    for (const terms of docTerms) for (const t of new Set(terms)) df.set(t, (df.get(t) || 0) + 1);
    return docTerms.map((terms) => {
      const tf = new Map();
      for (const t of terms) tf.set(t, (tf.get(t) || 0) + 1);
      const v = new Map(); let norm = 0;
      for (const [t, c] of tf) { const w = (Math.log(c) + 1) * (Math.log((1 + N) / (1 + df.get(t))) + 1); v.set(t, w); norm += w * w; }
      norm = Math.sqrt(norm);
      if (norm > 0) for (const [t, w] of v) v.set(t, w / norm);
      return v;
    });
  }
  function wordGrams(doc) { // token_pattern (?u)\b\w+\b, ngram_range (1, 2)
    const toks = String(doc).toLowerCase().match(/\b\w+\b/g) || [];
    const out = [...toks];
    for (let i = 0; i + 2 <= toks.length; i++) out.push(`${toks[i]} ${toks[i + 1]}`);
    return out;
  }
  function charWbGrams(doc, lo = 3, hi = 5) { // analyzer="char_wb", ngram_range (3, 5)
    const out = [];
    for (const word of String(doc).toLowerCase().replace(/\s\s+/g, " ").split(/\s+/).filter(Boolean)) {
      const w = ` ${word} `;
      for (let n = lo; n <= hi; n++) {
        let offset = 0;
        out.push(w.slice(offset, offset + n));
        while (offset + n < w.length) { offset += 1; out.push(w.slice(offset, offset + n)); }
        if (offset === 0) break;
      }
    }
    return out;
  }

  // ---------------------------------------------------------------- HDBSCAN: port of sklearn.cluster.HDBSCAN(metric="precomputed")
  function hdbscanLabels(D, { minClusterSize = 2, minSamples = 1, method = "leaf" } = {}) {
    const n = D.length;
    if (n < 2) return new Array(n).fill(-1);
    const core = D.map((row) => [...row].sort((a, b) => a - b)[Math.min(minSamples, n) - 1]);
    const MR = D.map((row, i) => row.map((d, j) => Math.max(core[i], core[j], d)));
    // mst_from_mutual_reachability (Prim; edge = last added node -> next node)
    let labels = Array.from({ length: n }, (_, i) => i), minR = new Array(n).fill(Infinity), cur = 0;
    const mst = [];
    for (let i = 0; i < n - 1; i++) {
      const keep = [];
      for (let k = 0; k < labels.length; k++) if (labels[k] !== cur) keep.push(k);
      const next = keep.map((k) => labels[k]);
      minR = keep.map((k, m) => Math.min(minR[k], MR[cur][next[m]]));
      labels = next;
      let best = 0;
      for (let k = 1; k < minR.length; k++) if (minR[k] < minR[best]) best = k;
      mst.push([cur, labels[best], minR[best]]);
      cur = labels[best];
    }
    mst.sort((a, b) => a[2] - b[2]);
    // make_single_linkage
    const parent = new Array(2 * n - 1).fill(-1), size = new Array(2 * n - 1).fill(0);
    for (let i = 0; i < n; i++) size[i] = 1;
    const find = (x) => { let r = x; while (parent[r] !== -1) r = parent[r]; while (x !== r && parent[x] !== r) { const p = parent[x]; parent[x] = r; x = p; } return r; };
    const hier = [];
    let nextNode = n;
    for (const [a, b, d] of mst) {
      const ra = find(a), rb = find(b);
      hier.push([ra, rb, d, size[ra] + size[rb]]);
      parent[ra] = nextNode; parent[rb] = nextNode; size[nextNode] = size[ra] + size[rb]; nextNode++;
    }
    const bfs = (root) => {
      const res = []; let q = [root];
      while (q.length) { res.push(...q); const inner = q.filter((x) => x >= n).map((x) => x - n); q = []; for (const k of inner) q.push(hier[k][0], hier[k][1]); }
      return res;
    };
    // _condense_tree
    const root = 2 * (n - 1), relabel = new Array(root + 1).fill(0);
    relabel[root] = n;
    let nextLabel = n + 1;
    const nodeList = bfs(root), ignore = new Array(nodeList.length).fill(false), rows = [];
    for (const node of nodeList) {
      if (ignore[node] || node < n) continue;
      const [left, right, dist] = hier[node - n];
      const lam = dist > 0 ? 1 / dist : Infinity;
      const lc = left >= n ? hier[left - n][3] : 1, rc = right >= n ? hier[right - n][3] : 1;
      const fall = (sub) => { for (const s of bfs(sub)) { if (s < n) rows.push([relabel[node], s, lam, 1]); ignore[s] = true; } };
      if (lc >= minClusterSize && rc >= minClusterSize) {
        relabel[left] = nextLabel++; rows.push([relabel[node], relabel[left], lam, lc]);
        relabel[right] = nextLabel++; rows.push([relabel[node], relabel[right], lam, rc]);
      } else if (lc < minClusterSize && rc < minClusterSize) { fall(left); fall(right); }
      else if (lc < minClusterSize) { relabel[right] = relabel[node]; fall(left); }
      else { relabel[left] = relabel[node]; fall(right); }
    }
    // _compute_stability
    const parents = rows.map((r) => r[0]);
    const rootCluster = Math.min(...parents), maxParent = Math.max(...parents);
    const births = new Map(rows.map((r) => [r[1], r[2]]));
    births.set(rootCluster, 0);
    const stability = new Map();
    for (let c = rootCluster; c <= maxParent; c++) stability.set(c, 0);
    for (const [p, , lam, sz] of rows) stability.set(p, stability.get(p) + (lam - (births.has(p) ? births.get(p) : NaN)) * sz);
    // _get_clusters
    const clusterRows = rows.filter((r) => r[3] > 1);
    const nodeOrder = [...stability.keys()].sort((a, b) => b - a).slice(0, -1);
    const isCluster = new Map(nodeOrder.map((c) => [c, true]));
    const childrenOf = (c) => clusterRows.filter((r) => r[0] === c).map((r) => r[1]);
    if (method === "eom") {
      for (const node of nodeOrder) {
        const sub = childrenOf(node).reduce((s, ch) => s + stability.get(ch), 0);
        if (sub > stability.get(node)) { isCluster.set(node, false); stability.set(node, sub); }
        else {
          let q = [node];
          while (q.length) { for (const x of q) if (x !== node) isCluster.set(x, false); const qs = new Set(q); q = clusterRows.filter((r) => qs.has(r[0])).map((r) => r[1]); }
        }
      }
    } else {
      const leafDfs = (c) => { const ch = childrenOf(c); return ch.length ? ch.flatMap(leafDfs) : [c]; };
      const leaves = new Set(clusterRows.length ? leafDfs(Math.min(...clusterRows.map((r) => r[0]))) : []);
      for (const c of [...isCluster.keys()]) isCluster.set(c, leaves.has(c));
    }
    const clusters = new Set([...isCluster].filter(([, v]) => v).map(([c]) => c));
    const cmap = new Map([...clusters].sort((a, b) => a - b).map((c, i) => [c, i]));
    // _do_labelling (TreeUnionFind: union by rank + path compression)
    const uf = Array.from({ length: maxParent + 1 }, (_, i) => [i, 0]);
    const ufind = (x) => { if (uf[x][0] !== x) uf[x][0] = ufind(uf[x][0]); return uf[x][0]; };
    const union = (x, y) => { const xr = ufind(x), yr = ufind(y); if (uf[xr][1] < uf[yr][1]) uf[xr][0] = yr; else if (uf[xr][1] > uf[yr][1]) uf[yr][0] = xr; else { uf[yr][0] = xr; uf[xr][1] += 1; } };
    for (const [p, c] of rows) if (!clusters.has(c)) union(p, c);
    return Array.from({ length: rootCluster }, (_, i) => { const c = ufind(i); return c !== rootCluster && cmap.has(c) ? cmap.get(c) : -1; });
  }
  /** AgglomerativeClustering(n_clusters=None, linkage="average", metric="precomputed", distance_threshold). */
  function agglomerativeLabels(D, threshold) {
    const n = D.length, M = D.map((r) => r.slice()), size = new Array(n).fill(1), alive = new Array(n).fill(true);
    const members = Array.from({ length: n }, (_, i) => [i]);
    for (;;) {
      let best = Infinity, bi = -1, bj = -1;
      for (let i = 0; i < n; i++) if (alive[i]) for (let j = i + 1; j < n; j++) if (alive[j] && M[i][j] < best) { best = M[i][j]; bi = i; bj = j; }
      if (bi < 0 || best >= threshold) break;
      for (let k = 0; k < n; k++) if (alive[k] && k !== bi && k !== bj) { const d = (size[bi] * M[bi][k] + size[bj] * M[bj][k]) / (size[bi] + size[bj]); M[bi][k] = d; M[k][bi] = d; }
      size[bi] += size[bj]; alive[bj] = false; members[bi].push(...members[bj]);
    }
    const labels = new Array(n);
    let k = 0;
    for (let i = 0; i < n; i++) if (alive[i]) { for (const m of members[i]) labels[m] = k; k++; }
    return labels;
  }
  function attachNearest(labels, sim, minSimilarity) {
    const out = [...labels];
    let changed = true;
    while (changed) {
      changed = false;
      const counts = new Map();
      for (const l of out) counts.set(l, (counts.get(l) || 0) + 1);
      for (let i = 0; i < out.length; i++) {
        const lab = out[i];
        if (lab >= 0 && (counts.get(lab) || 0) > 1) continue;
        let best = null, bestSim = minSimilarity;
        for (let j = 0; j < out.length; j++) if (j !== i && out[j] >= 0 && (counts.get(out[j]) || 0) >= 2 && sim[i][j] > bestSim) { best = out[j]; bestSim = sim[i][j]; }
        if (best !== null && best !== lab) { if (lab >= 0) counts.set(lab, counts.get(lab) - 1); out[i] = best; counts.set(best, (counts.get(best) || 0) + 1); changed = true; }
      }
    }
    let next = Math.max(-1, ...out) + 1;
    return out.map((l) => (l < 0 ? next++ : l));
  }
  function relabel(labels) { const m = new Map(); return labels.map((l) => { if (!m.has(l)) m.set(l, m.size); return m.get(l); }); }
  /** Cosine similarity of the weighted, re-normalised [word | char_wb | concept] TF-IDF blocks. */
  function similarityMatrix(keywords, params) {
    const n = keywords.length, toks = keywords.map(keywordTokens), df = new Map();
    for (const t of toks) for (const x of new Set(t)) df.set(x, (df.get(x) || 0) + 1);
    const ubiq = new Set([...df].filter(([, c]) => n >= 8 && c / n >= Number(params.ubiquitous_df ?? 0.6)).map(([x]) => x));
    const docs = toks.map((t, i) => t.filter((x) => !ubiq.has(x)).join(" ") || t.join(" ") || keywords[i]);
    const blocks = [[Number(params.word_weight ?? 0.6), tfidf(docs.map(wordGrams))], [Number(params.char_weight ?? 0.25), tfidf(docs.map((d) => charWbGrams(d)))]];
    const tagDocs = keywords.map((k) => conceptTags(k, params.concepts || {}).map((c) => `zz${c}`));
    if (tagDocs.some((t) => t.length)) blocks.push([Number(params.concept_weight ?? 0.6), tfidf(tagDocs)]);
    const raw = keywords.map((_, i) => keywords.map((__, j) => blocks.reduce((s, [w, vs]) => s + w * w * T.cosine(vs[i], vs[j]), 0)));
    const norms = raw.map((r, i) => Math.sqrt(r[i]) || 1);
    return raw.map((r, i) => r.map((s, j) => s / (norms[i] * norms[j])));
  }
  /** tie_break_distances (Python twin): snap distances to a 2^-30 grid, then add exact (pair rank) x 2^-50 offsets from the
      alphabetical keyword order - no two distances tie, so HDBSCAN / linkage never depend on sort stability and both runtimes agree. */
  function tieBreakDistances(dist, keywords) {
    const n = keywords.length, rank = new Array(n), scale = 2 ** 30, step = 2 ** -50;
    keywords.map((_, i) => i).sort((a, b) => cmp(keywords[a], keywords[b]) || a - b).forEach((i, r) => { rank[i] = r; });
    return dist.map((row, i) => row.map((d, j) => (i === j ? 0
      : Math.floor(d * scale + 0.5) / scale + (Math.min(rank[i], rank[j]) * n + Math.max(rank[i], rank[j])) * step)));
  }
  function clusterKeywords(keywords, params) {
    const n = keywords.length;
    if (!n) return [[], "none"];
    if (n === 1) return [[0], "single"];
    const raw = similarityMatrix(keywords, params);
    const dist = tieBreakDistances(raw.map((row, i) => row.map((s, j) => (i === j ? 0 : Math.min(2, Math.max(0, 1 - s))))), keywords);
    const sim = dist.map((row) => row.map((d) => 1 - d)); // attach on the same exact values, so nearest-neighbour ties resolve identically too
    const attach = Number(params.attach_min_similarity ?? 0.25), minK = Number(params.hdbscan_min_keywords ?? 12);
    let labels = null, method = "agglomerative";
    if (n >= minK) {
      labels = attachNearest(hdbscanLabels(dist, { minClusterSize: Number(params.min_cluster_size ?? 2), minSamples: Number(params.min_samples ?? 1),
        method: String(params.selection_method || "leaf") }), sim, attach);
      const k = new Set(labels).size;
      if ((k === 1 && n > 2 * minK) || k > 0.8 * n) labels = null; // degenerate: one blob, or almost all singletons
      else method = "hdbscan";
    }
    if (labels === null) labels = attachNearest(agglomerativeLabels(dist, Number(params.agglomerative_distance_threshold ?? 0.8)), sim, attach);
    return [relabel(labels), method];
  }

  // ---------------------------------------------------------------- clusters + content plan
  function dominantIntent(rows) {
    const w = new Map();
    for (const r of rows) w.set(r.intent, (w.get(r.intent) || 0) + Number(r.impressions || r.est_monthly_searches || 1));
    const rank = (k) => (INTENTS.includes(k) ? INTENTS.indexOf(k) : 9);
    return [...w].sort((a, b) => b[1] - a[1] || rank(a[0]) - rank(b[0]))[0][0];
  }
  const pickPillar = (rows) => maxBy(rows, (r) => [r.is_estimate ? 0 : 1, r.impressions || r.est_monthly_searches || 0, -r.keyword.split(/\s+/).filter(Boolean).length,
    r.value || 0, r.relevance || 0, -r.keyword.length, r.keyword]);
  function buildClusters(rows, labels) {
    const groups = new Map();
    rows.forEach((r, i) => { if (!groups.has(labels[i])) groups.set(labels[i], []); groups.get(labels[i]).push(r); });
    const clusters = [];
    for (let members of groups.values()) {
      members = [...members].sort((a, b) => b.value - a.value || cmp(a.keyword, b.keyword));
      const pillar = pickPillar(members);
      const pageImp = new Map();
      for (const r of members) if (r.page && !r.is_estimate) pageImp.set(r.page, (pageImp.get(r.page) || 0) + Math.trunc(Number(r.impressions || 0)));
      const existing = pageImp.size ? [...pageImp].sort((a, b) => b[1] - a[1] || cmp(a[0], b[0]))[0][0] : null;
      clusters.push({ pillar_keyword: pillar.keyword, intent: dominantIntent(members), members,
        total_impressions: members.filter((r) => !r.is_estimate).reduce((s, r) => s + Math.trunc(Number(r.impressions || 0)), 0),
        potential_clicks: round(members.reduce((s, r) => s + r.potential_clicks, 0), 1), value: members.reduce((s, r) => s + r.value, 0),
        existing_page: existing, pillar_position: pillar.position ?? null, is_estimate: members.every((r) => r.is_estimate) });
    }
    clusters.sort((a, b) => b.value - a.value || cmp(a.pillar_keyword, b.pillar_keyword));
    normaliseScores(clusters.map((c) => c.value)).forEach((s, i) => { clusters[i].cluster_id = `c${i + 1}`; clusters[i].opportunity_score = s; });
    return clusters;
  }
  const fallbackTitle = (template, keyword) => template.replace("{Keyword}", keyword.slice(0, 1).toUpperCase() + keyword.slice(1)).replace("{keyword}", keyword);
  const expandModifiers = (topic, modifiers) => { const t = normalizeKeyword(topic); return [...new Set((modifiers || []).map((m) => normalizeKeyword(String(m).replace("{topic}", t))))].filter(Boolean); };
  const ADVICE = {
    ctr_gap: "Rewrite the title tag and meta description - the page ranks well but under-earns clicks for its position.",
    push_to_first: "Strengthen the page (depth, internal links, schema) to move from the top 3 to #1.",
    page_one_push: "Improve on-page relevance and internal links to move into the top 3.",
    striking_distance: "Expand the content and add internal links - page two rankings are the quickest page-one wins.",
    long_shot: "Create or consolidate dedicated content; current rankings are too deep to earn clicks.",
    new_content: "Create dedicated content - no first-party ranking exists for this keyword.",
  };
  function findings(top, rows) {
    const out = [];
    for (const r of top.filter((r) => ADVICE[r.opportunity_type]).slice(0, 5)) {
      const evidence = !r.is_estimate ? `${r.impressions} impressions, CTR ${pct(r.ctr)} vs expected ${pct(r.expected_ctr)} at position ${pyFloat(r.position)}`
        : `estimated ${r.est_monthly_searches || "unknown"} monthly searches (model estimate)`;
      out.push({ title: `'${r.keyword}': ${r.opportunity_type.replaceAll("_", " ")} (opportunity ${pyFixed(r.opportunity_score, 0)}/100)`,
        detail: `${cap1(r.intent)} intent; ~${pyFixed(r.potential_clicks, 0)} extra clicks available at the target position.`,
        severity: r.opportunity_score >= 60 ? "high" : "medium", category: "seo", evidence, recommendation: ADVICE[r.opportunity_type], target: r.page, source_agent: "keyword_research" });
    }
    for (const r of rows.filter((r) => r.cannibalised).slice(0, 3))
      out.push({ title: `Keyword cannibalisation: '${r.keyword}'`, severity: "medium", category: "seo",
        detail: "Two or more pages split this keyword's impressions, diluting relevance signals.",
        evidence: `${r.impressions} impressions across several pages; top page ${r.page}`,
        recommendation: "Pick one canonical page for the keyword, consolidate content and point internal links to it.", target: r.page, source_agent: "keyword_research" });
    return out;
  }
  const keywordRow = (m, clusterId) => ({ keyword: m.keyword, cluster_id: clusterId, intent: m.intent, intent_rule: m.intent_rule || "", source: m.source, is_estimate: m.is_estimate,
    relevance: m.relevance ?? 0, impressions: m.impressions ?? null, clicks: m.clicks ?? null, ctr: m.ctr ?? null, position: m.position ?? null,
    est_monthly_searches: m.est_monthly_searches ?? null, expected_ctr: m.expected_ctr ?? 0, potential_clicks: m.potential_clicks ?? 0,
    opportunity_score: m.opportunity_score ?? 0, opportunity_type: m.opportunity_type || "", position_band: m.position_band || "", page: m.page ?? null, cannibalised: !!m.cannibalised });
  function keywordsCsv(rows, names) {
    const f = (v) => (v === null || v === undefined ? "" : pyFloat(v));
    const i = (v) => (v === null || v === undefined ? "" : String(v));
    const sorted = [...rows].sort((a, b) => (Number(a.cluster_id.slice(1)) || 0) - (Number(b.cluster_id.slice(1)) || 0) || b.opportunity_score - a.opportunity_score || cmp(a.keyword, b.keyword));
    const cols = ["cluster_id", "cluster_name", "keyword", "intent", "source", "is_estimate", "impressions", "clicks", "ctr", "position", "est_monthly_searches", "relevance",
      "opportunity_score", "potential_clicks", "opportunity_type", "page"];
    return CAF.lib.csv.stringify(sorted.map((r) => ({ cluster_id: r.cluster_id, cluster_name: names[r.cluster_id] || "", keyword: r.keyword, intent: r.intent, source: r.source,
      is_estimate: r.is_estimate ? "True" : "False", impressions: i(r.impressions), clicks: i(r.clicks), ctr: f(r.ctr), position: f(r.position),
      est_monthly_searches: i(r.est_monthly_searches), relevance: f(r.relevance), opportunity_score: f(r.opportunity_score), potential_clicks: f(r.potential_clicks),
      opportunity_type: r.opportunity_type, page: r.page || "" })), cols);
  }

  const lib = { pyFixed, normalizeKeyword, stem, keywordTokens, keywordKey, tokensMatch, hasPhrase, samePage, csvQueries, fixtureSearch, aggregateQueries, modifierTokens,
    relevanceScores, classifyIntent, brandTermsFrom, expectedCtr, positionBand, scoreKeyword, normaliseScores, conceptTags, similarityMatrix, hdbscanLabels,
    agglomerativeLabels, attachNearest, relabel, tieBreakDistances, clusterKeywords, dominantIntent, pickPillar, buildClusters, fallbackTitle, expandModifiers, findings };

  CAF.agent("keyword_research", {
    lib,
    async run(input, ctx) {
      const rules = ctx.skill("keyword_intent"), model = rules.opportunity, topic = String(input.topic).trim(), pageUrl = input.page_url || null;

      // -- stage 1 gather: first-party data, SERP context, page context
      let rows = [], dataSource = "keyword_data:none";
      if (ctx.hasData("keywords/gsc_queries")) {
        dataSource = "keyword_data:csv";
        const all = await ctx.data("keywords/gsc_queries");
        rows = csvQueries(all, topic, 5000);
        if (pageUrl) { // the page's own queries even when they do not mention the topic
          const seen = new Set(rows.map((r) => `${r.query}\u0001${r.page}`));
          rows = rows.concat(csvQueries(all, null, 20000).filter((r) => samePage(String(r.page || ""), pageUrl) && !seen.has(`${r.query}\u0001${r.page}`)));
        }
      } else ctx.degraded("no first-party keyword data (keywords/gsc_queries is not embedded in this console) - volumes and scores are estimates");
      const serp = [];
      if (ctx.hasData("search/fixture_results")) {
        const own = bareHost(String(ctx.brand.website || pageUrl || ""));
        fixtureSearch((await ctx.data("search/fixture_results")).results, topic, { num: 10 }).forEach((r, i) =>
          serp.push({ rank: i + 1, title: r.title, url: r.url, source: r.source, own_domain: !!own && bareHost(r.url) === own }));
      } else ctx.warn("No search provider configured: SERP context was skipped.");
      let page = {};
      if (pageUrl) {
        if (ctx.isDemoUrl(pageUrl)) {
          const pg = ctx.page({ url: pageUrl });
          if (pg && pg.status >= 200 && pg.status < 400) {
            const doc = CAF.lib.html.parse(pg.html), h1 = doc.querySelector("h1");
            page = { title: CAF.lib.html.text(doc.querySelector("title")), h1: h1 ? CAF.lib.html.text(h1) : "" };
          } else ctx.warn(`Could not read ${pageUrl} (status ${pg ? pg.status : 0}); page context skipped.`);
        } else ctx.warn(`Could not read ${pageUrl}: the hosted console only reads the demo site (no live crawling); page context skipped.`);
      }
      ctx.check({ rule: "data.first_party", status: rows.length ? "pass" : "warn", severity: "medium",
        message: rows.length ? `${rows.length} first-party query rows from ${dataSource}.` : `No first-party query rows from ${dataSource}; results are estimates.` });
      const brief = ctx.upstreamOutput("campaign_brief_generation") || {};

      // -- stage 2 expand: model candidates (always estimates)
      const known = aggregateQueries(rows, pageUrl, Number(model.min_impressions ?? 1));
      const knownKeys = new Set(known.map((k) => keywordKey(k.keyword)));
      const seeds = (input.seed_keywords || []).map(normalizeKeyword).filter(Boolean);
      const relCfg = rules.relevance || {}, ignore = modifierTokens(rules), minRel = Number(relCfg.min_relevance ?? 0.5);
      const relKnown = relevanceScores(known.map((k) => k.keyword), [input.topic, ...seeds], { ignore, synonyms: relCfg.synonyms });
      const relevant = known.filter((k, i) => relKnown[i] >= minRel || k.on_target_page);
      const thin = relevant.length < Number(rules.min_first_party ?? 8);
      const modifiers = thin ? expandModifiers(input.topic, rules.expansion_modifiers || []) : [];
      const estimateFor = [...new Set([...seeds, ...modifiers])].filter((k) => !knownKeys.has(keywordKey(k))).slice(0, 20);
      const maxNew = Math.max(5, Math.min(25, Math.floor(Number(input.max_keywords ?? 60) / 2)));
      ctx.progress("Expanding keyword candidates");
      const expansion = await ctx.generate("expand", { topic: input.topic, market: input.market ?? null, page_url: pageUrl, page_title: page.title || page.h1 || null,
        known: relevant.slice(0, 25).map((k) => `${k.keyword} | ${k.impressions} impressions | position ${pyFloat(k.position)}`),
        serp_titles: serp.map((s) => s.title).slice(0, 10), estimate_for: estimateFor, max_new: maxNew, key_messages: (brief.key_messages || []).slice(0, 5) });
      const candidates = expansion.keywords || [];

      // -- stage 3 score: merge, dedupe, relevance gate, intent, opportunity
      const merged = new Map();
      for (const k of known) merged.set(keywordKey(k.keyword), { ...k, source: "first_party", is_estimate: false, est_monthly_searches: null });
      const estimates = new Map(candidates.filter((c) => normalizeKeyword(c.keyword)).map((c) => [keywordKey(c.keyword), c.est_monthly_searches ?? null]));
      const add = (kw, source) => {
        const key = keywordKey(kw);
        if (!key || merged.has(key)) { if (merged.has(key) && source === "seed") merged.get(key).seed = true; return; }
        const est = estimates.get(key);
        merged.set(key, { keyword: kw, impressions: null, clicks: null, ctr: null, position: null, page: null, pages: [], cannibalised: false, on_target_page: false,
          source, is_estimate: true, est_monthly_searches: est !== null && est !== undefined ? Math.trunc(est) : null, seed: source === "seed" });
      };
      seeds.forEach((s) => add(s, "seed"));
      modifiers.forEach((m) => add(m, "modifier"));
      let newModel = 0;
      for (const c of candidates) { const kw = normalizeKeyword(c.keyword); if (kw && !merged.has(keywordKey(kw)) && newModel < maxNew) { add(kw, "model"); newModel++; } }
      const all = [...merged.values()];
      const rel = relevanceScores(all.map((r) => r.keyword), [input.topic, ...seeds], { ignore, synonyms: relCfg.synonyms });
      const kept = [], dropped = [];
      all.forEach((r, i) => { r.relevance = rel[i]; (r.seed || r.on_target_page || rel[i] >= minRel ? kept : dropped).push(r); });
      if (dropped.length) ctx.check({ rule: "keywords.relevance", status: "info", severity: "low", data: { examples: dropped.slice(0, 8).map((d) => d.keyword) },
        message: `${dropped.length} candidate(s) below relevance ${pyFloat(minRel)} were dropped (${dropped.filter((d) => d.source === "first_party").length} first-party, ${dropped.filter((d) => d.source === "model").length} model).` });
      const brand = brandTermsFrom(ctx.brand, rules.brand_terms), competitors = (rules.competitor_terms || []).map(normalizeKeyword);
      const defaults = model.estimate_defaults || {};
      for (const r of kept) {
        [r.intent, r.intent_rule] = classifyIntent(r.keyword, rules, brand, competitors);
        Object.assign(r, r.is_estimate
          ? scoreKeyword({ impressions: r.est_monthly_searches !== null ? r.est_monthly_searches : (defaults.monthly_searches ?? 50), ctr: 0, position: null, intent: r.intent, isEstimate: true, model })
          : scoreKeyword({ impressions: r.impressions, ctr: r.ctr, position: r.position, intent: r.intent, isEstimate: false, model }));
      }
      const byValue = (a, b) => b.value - a.value || cmp(a.keyword, b.keyword);
      kept.sort(byValue);
      const capN = Number(input.max_keywords ?? 60);
      const seedsKept = kept.filter((r) => r.seed).slice(0, capN);
      const final = [...seedsKept, ...kept.filter((r) => !r.seed).slice(0, Math.max(0, capN - seedsKept.length))].sort(byValue);
      normaliseScores(final.map((r) => r.value)).forEach((s, i) => { final[i].opportunity_score = s; });
      const cannibal = final.filter((r) => r.cannibalised);
      if (cannibal.length) ctx.check({ rule: "keywords.cannibalisation", status: "warn", severity: "medium", data: { keywords: Object.fromEntries(cannibal.slice(0, 10).map((r) => [r.keyword, r.pages.slice(0, 4)])) },
        message: `${cannibal.length} keyword(s) split impressions across several pages: ${cannibal.slice(0, 5).map((r) => `'${r.keyword}'`).join(", ")}` });
      const nEst = final.filter((r) => r.is_estimate).length;
      ctx.check({ rule: "keywords.estimates", status: nEst < final.length ? "info" : "warn", severity: "low", message: `${final.length - nEst} keyword(s) backed by first-party data, ${nEst} flagged as estimates.` });
      if (nEst === final.length && !ctx.state.degraded.length) ctx.degraded("first-party keyword data returned no relevant queries for this topic - volumes and scores are estimates");

      // -- stage 4 cluster (in-process)
      const [labels, method] = clusterKeywords(final.map((r) => r.keyword), rules.clustering || {});
      const clusters = buildClusters(final, labels);
      ctx.check({ rule: "keywords.clustering", status: "info", severity: "low", message: `${final.length} keywords grouped into ${clusters.length} clusters (${method}, TF-IDF word + char n-grams + concept tags).` });

      // -- stage 5 recommend: the model names clusters; reconciled by id with deterministic fallbacks
      const formats = rules.content_formats || {};
      ctx.progress("Naming clusters and planning content");
      const naming = clusters.length ? await ctx.generate("name_clusters", { topic: input.topic, market: input.market ?? null,
        clusters: clusters.slice(0, 20).map((c) => ({ cluster_id: c.cluster_id, intent: c.intent, pillar_keyword: c.pillar_keyword, opportunity_score: c.opportunity_score,
          existing_page: c.existing_page, keywords: c.members.slice(0, 10).map((m) => m.keyword) })) }) : { clusters: [] };
      const byId = new Map((naming.clusters || []).map((s) => [String(s.cluster_id || "").trim().toLowerCase(), s]));
      const recs = clusters.map((c) => {
        const sug = byId.get(c.cluster_id), fmt = formats[c.intent] || { format: "article", title: "{Keyword}" };
        const sv = (k) => (sug ? String(sug[k] || "").trim() : "");
        c.name = (sv("name") || fallbackTitle("{Keyword}", c.pillar_keyword)).slice(0, 80);
        const ranking = !!c.existing_page && c.pillar_position !== null && c.pillar_position <= 20;
        const others = c.members.filter((m) => m.keyword !== c.pillar_keyword);
        const supporting = sug ? (sug.supporting_content || []).map((s) => String(s).trim()).filter(Boolean).slice(0, 6) : [];
        return { cluster_id: c.cluster_id, cluster_name: c.name, action: ranking ? "optimise" : "create", content_type: fmt.format ?? "article",
          pillar_title: (sv("pillar_title") || fallbackTitle(fmt.title ?? "{Keyword}", c.pillar_keyword)).slice(0, 120), target_page: ranking ? c.existing_page : null,
          target_keywords: [c.pillar_keyword, ...others.slice(0, 5).map((m) => m.keyword)],
          supporting_content: supporting.length ? supporting : others.slice(0, 3).map((m) => fallbackTitle("{Keyword}", m.keyword)),
          rationale: sv("rationale") || `${cap1(c.intent)} cluster of ${c.members.length} keywords, ${c.total_impressions} first-party impressions, ~${pyFixed(c.potential_clicks, 0)} extra clicks available.`,
          is_estimate: c.is_estimate };
      });
      const matched = clusters.filter((c) => byId.has(c.cluster_id)).length;
      ctx.check({ rule: "model.cluster_names", status: matched === clusters.length ? "pass" : "info", severity: "low", message: `Model named ${matched}/${clusters.length} clusters; the rest use the pillar keyword.` });

      // -- stage 6 export
      const clustersOut = clusters.map((c) => ({ cluster_id: c.cluster_id, name: c.name, pillar_keyword: c.pillar_keyword, intent: c.intent, keyword_count: c.members.length,
        total_impressions: c.total_impressions, potential_clicks: c.potential_clicks, opportunity_score: c.opportunity_score, existing_page: c.existing_page,
        is_estimate: c.is_estimate, keywords: c.members.map((m) => keywordRow(m, c.cluster_id)) }));
      const allRows = clustersOut.flatMap((c) => c.keywords);
      const top = [...allRows].sort((a, b) => b.opportunity_score - a.opportunity_score || cmp(a.keyword, b.keyword)).slice(0, 10);
      const estimate = !allRows.some((r) => !r.is_estimate);
      const names = Object.fromEntries(clustersOut.map((c) => [c.cluster_id, c.name]));
      ctx.artifact({ name: "keywords.csv", mime: "text/csv", content: keywordsCsv(allRows, names), description: "Keywords with intent, metrics and opportunity" });
      ctx.artifact({ name: "keyword_clusters.json", mime: "application/json", content: JSON.stringify(clustersOut, null, 2), description: "Keyword clusters" });
      if (estimate) ctx.warn("Keyword volumes are estimates (no first-party data) - validate before committing budget.");
      const best = top[0];
      const summary = `${allRows.length} keywords in ${clustersOut.length} clusters for '${input.topic}'`
        + (best ? `; top opportunity '${best.keyword}' (${best.opportunity_type.replaceAll("_", " ")}, score ${pyFixed(best.opportunity_score, 0)})` : "")
        + (estimate ? " - estimates only, no first-party data" : "");
      const output = { topic: input.topic, market: input.market ?? null, summary, data_source: dataSource, is_estimate: estimate, clustering_method: method,
        keyword_count: allRows.length, clusters: clustersOut, top_opportunities: top, content_recommendations: recs, serp_context: serp,
        findings: findings(top, allRows), skill_version: String(rules.version ?? "") };
      const fmtPos = (p) => (p === null ? "-" : pyFixed(p, 1));
      return {
        output, summary,
        view: [
          { type: "kv", title: "Keyword research", data: { topic: input.topic, data_source: dataSource, keywords: allRows.length, clusters: clustersOut.length,
            clustering: method, first_party_rows: rows.length, estimates_only: estimate ? "yes" : "no" } },
          { type: "table", title: "Top opportunities", columns: ["keyword", "intent", "source", "impressions", "position", "ctr", "potential_clicks", "score", "type"],
            rows: top.map((r) => ({ keyword: r.keyword, intent: r.intent, source: r.is_estimate ? `${r.source} (estimate)` : r.source, impressions: r.impressions ?? r.est_monthly_searches ?? "-",
              position: fmtPos(r.position), ctr: r.ctr === null ? "-" : pct(r.ctr), potential_clicks: pyFixed(r.potential_clicks, 0), score: pyFixed(r.opportunity_score, 0),
              type: r.opportunity_type.replaceAll("_", " ") })) },
          { type: "table", title: "Clusters", columns: ["cluster_id", "name", "intent", "pillar_keyword", "keywords", "impressions", "opportunity"],
            rows: clustersOut.map((c) => ({ cluster_id: c.cluster_id, name: c.name, intent: c.intent, pillar_keyword: c.pillar_keyword,
              keywords: c.keywords.map((k) => k.keyword).join("; "), impressions: c.total_impressions, opportunity: pyFixed(c.opportunity_score, 0) })) },
          { type: "table", title: "Content plan", columns: ["cluster_id", "action", "content_type", "pillar_title", "target_page", "supporting_content"],
            rows: recs.map((r) => ({ cluster_id: r.cluster_id, action: r.action, content_type: r.content_type, pillar_title: r.pillar_title, target_page: r.target_page || "",
              supporting_content: r.supporting_content.join("; ") })) },
          ...(output.findings.length ? [{ type: "list", title: "Findings", items: output.findings.map((f) => `${f.title} - ${f.recommendation}`) }] : []),
        ],
      };
    },
  });
})();
