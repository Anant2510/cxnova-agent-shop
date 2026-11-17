/* Industry Marketing - browser twin of caf/agents/content/industry_marketing.py.
   Versioned industry profiles (industries/<id> skills) + retrieval over your own corpus: the
   embedded industry_corpus/* documents (front-matter industry / title / doc_type / tags) are chunked
   section-aware and indexed in an in-memory store (twin of the tfidf vectorstore: TF-IDF 1-2 grams,
   sublinear tf, English stop words, cosine), filtered by industry. The model tailors the source
   grounded in the passages; code verifies every proof point against its document's figures, strips
   citations to documents that were not retrieved, enforces 'never' phrases (one repair pass with the
   live model) and appends required disclaimers. */
(() => {
  const COLLECTION = "industry_corpus";
  const SHARED = ["cross-industry", "cross_industry", "all", "general"];
  const INDEX = new Map(), INDEXED = new Map(); // collection -> Map(id -> chunk); corpus key -> fingerprint
  const FIGURE = /(?<![\p{L}\p{N}_.])\p{Nd}[\p{Nd},]*(?:\.\p{Nd}+)?/gu;
  const QUANT = /%|->|\bhours?\b|\bdays?\b|\bpoints?\b|\btimes\b|\bmillion\b|\bfrom\b.+\bto\b/i;
  const OUTCOME = /->|\b(?:cut|cuts|reduced|reduces|raised|raises|increased|improved|fell|rose|lifted|lowered|grew|saved|halved|doubled|tripled|dropped|boosted|shortened)\b/i;
  const ENGLISH_STOP = new Set(("a about above across after afterwards again against all almost alone along already also although always am among amongst amoungst amount an and another any anyhow "
    + "anyone anything anyway anywhere are around as at back be became because become becomes becoming been before beforehand behind being below beside besides between beyond bill both bottom but by "
    + "call can cannot cant co con could couldnt cry de describe detail do done down due during each eg eight either eleven else elsewhere empty enough etc even ever every everyone everything "
    + "everywhere except few fifteen fifty fill find fire first five for former formerly forty found four from front full further get give go had has hasnt have he hence her here hereafter hereby "
    + "herein hereupon hers herself him himself his how however hundred i ie if in inc indeed interest into is it its itself keep last latter latterly least less ltd made many may me meanwhile might "
    + "mill mine more moreover most mostly move much must my myself name namely neither never nevertheless next nine no nobody none noone nor not nothing now nowhere of off often on once one only onto "
    + "or other others otherwise our ours ourselves out over own part per perhaps please put rather re same see seem seemed seeming seems serious several she should show side since sincere six sixty so "
    + "some somehow someone something sometime sometimes somewhere still such system take ten than that the their them themselves then thence there thereafter thereby therefore therein thereupon these "
    + "they thick thin third this those though three through throughout thru thus to together too top toward towards twelve twenty two un under until up upon us very via was we well were what whatever "
    + "when whence whenever where whereafter whereas whereby wherein whereupon wherever whether which while whither who whoever whole whom whose why will with within without would yet you your yours "
    + "yourself yourselves").split(" "));
  const cmp = (a, b) => (a < b ? -1 : a > b ? 1 : 0);
  const cmpTuple = (a, b) => { for (let i = 0; i < a.length; i++) { const c = cmp(a[i], b[i]); if (c) return c; } return 0; };
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
  const len = (s) => [...String(s ?? "")].length;
  const normWs = (s) => String(s ?? "").replace(/\s+/g, " ").trim();
  const reEsc = (s) => String(s).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const pyStr = (s) => { s = String(s); const q = s.includes("'") && !s.includes('"') ? '"' : "'";
    return q + s.replace(/\\/g, "\\\\").replace(new RegExp(q, "g"), "\\" + q).replace(/\n/g, "\\n").replace(/\r/g, "\\r").replace(/\t/g, "\\t") + q; };
  const pyList = (xs) => `[${xs.map(pyStr).join(", ")}]`;
  const splitlines = (t) => { const p = String(t ?? "").split(/\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]/); if (p.length && p[p.length - 1] === "") p.pop(); return p; };
  function truncate(text, n, ell = "...") {
    const cps = [...String(text ?? "")];
    if (cps.length <= n) return String(text ?? "");
    const cut = cps.slice(0, Math.max(0, n - ell.length)).join(""), i = cut.lastIndexOf(" ");
    return (i >= 0 ? cut.slice(0, i) : cut) + ell;
  }

  // ---------------------------------------------------------------- corpus: front matter, chunks, retrieval
  /** Minimal YAML for front matter: key: value, quoted strings, [inline, lists], block lists, # comments. */
  function parseYamlLite(src) {
    const out = {};
    let listKey = null;
    const scalar = (v) => {
      v = v.trim();
      if (/^".*"$/.test(v)) { try { return JSON.parse(v); } catch { return v.slice(1, -1); } }
      if (/^'.*'$/.test(v)) return v.slice(1, -1).replace(/''/g, "'");
      if (/^(null|~|)$/i.test(v)) return null;
      if (/^(true|false)$/i.test(v)) return /^true$/i.test(v);
      return v;
    };
    const stripComment = (v) => (/^["']/.test(v.trim()) ? v : v.replace(/\s+#.*$/, ""));
    for (const raw of splitlines(src)) {
      if (!raw.trim() || /^\s*#/.test(raw)) continue;
      const item = raw.match(/^\s+-\s+(.*)$/) || raw.match(/^-\s+(.*)$/);
      if (item && listKey) { out[listKey].push(scalar(stripComment(item[1]))); continue; }
      const m = raw.match(/^([A-Za-z_][\w-]*)\s*:\s*(.*)$/);
      if (!m) continue;
      const v = stripComment(m[2]).trim();
      listKey = null;
      if (!v) { out[m[1]] = []; listKey = m[1]; continue; }
      if (/^\[.*\]$/.test(v)) out[m[1]] = v.slice(1, -1).split(",").map((x) => x.trim()).filter(Boolean).map(scalar);
      else out[m[1]] = scalar(v);
    }
    for (const [k, v] of Object.entries(out)) if (Array.isArray(v) && !v.length && listKey !== k) out[k] = null;
    return out;
  }
  function parseFrontMatter(text) {
    const m = String(text || "").match(/^\ufeff?---\s*\n([\s\S]*?)\n---\s*\n?([\s\S]*)$/);
    if (!m) return [{}, text || ""];
    let meta = {};
    try { meta = parseYamlLite(m[1]) || {}; } catch { meta = {}; }
    return [meta, m[2]];
  }
  const pyStrOf = (v) => (v === true ? "True" : v === false ? "False" : String(v));
  function loadCorpus(files) { // files: [[stem, text]] sorted like Path.glob("*.md")
    return files.map(([stem, raw]) => {
      const [meta, b] = parseFrontMatter(raw), body = b.replace(/^\s*Illustrative sample content:[^\n]*\n/, "").trim(), tags = meta.tags || [];
      return { doc_id: pyStrOf(meta.id || stem), industry: pyStrOf(meta.industry || "").trim().toLowerCase(), title: pyStrOf(meta.title || stem),
        doc_type: pyStrOf(meta.doc_type || ""), tags: Array.isArray(tags) ? tags.map(pyStrOf) : [pyStrOf(tags)], text: body };
    });
  }
  /** (section heading, block) pairs; a Markdown table stays one block. */
  function blocks(body) {
    const out = [];
    let heading = "";
    for (const raw of String(body || "").split(/\n\s*\n/)) {
      let block = raw.trim();
      if (!block) continue;
      const lines = splitlines(block), m = lines[0].match(/^(#{1,6})\s+(.+?)\s*$/);
      if (m) { heading = m[2]; const rest = lines.slice(1).join("\n").trim(); if (!rest) continue; block = rest; }
      out.push([heading, block]);
    }
    return out;
  }
  const wc = (s) => String(s).split(/\s+/).filter(Boolean).length;
  function corpusChunks(docs, maxWords = 160) {
    const out = [];
    for (const d of docs) {
      const chunks = [];
      for (const [heading, block] of blocks(d.text)) {
        const n = wc(block), last = chunks[chunks.length - 1];
        if (last && last[0] === heading && last[1].reduce((a, b) => a + wc(b), 0) + n <= maxWords) last[1].push(block);
        else chunks.push([heading, [block]]);
      }
      chunks.forEach(([heading, bs], i) => out.push({ id: `${d.doc_id}#${i}`, text: `${d.title}` + (heading ? ` - ${heading}` : "") + "\n" + bs.join("\n\n"),
        metadata: { doc_id: d.doc_id, industry: d.industry, title: d.title, doc_type: d.doc_type, tags: d.tags, section: heading, chunk: i } }));
    }
    return out;
  }
  /** textutil.tfidf_similarity: TfidfVectorizer(ngram_range=(1,2), sublinear_tf, stop_words="english") + cosine. */
  function tfidfSimilarity(query, docs) {
    if (!docs.length) return [];
    const grams = [...docs, query].map((d) => {
      const toks = (String(d).toLowerCase().match(/[\p{L}\p{N}_]{2,}/gu) || []).filter((t) => !ENGLISH_STOP.has(t));
      const out = [...toks];
      for (let i = 0; i + 1 < toks.length; i++) out.push(`${toks[i]} ${toks[i + 1]}`);
      return out;
    });
    if (!grams.some((g) => g.length)) return docs.map(() => 0); // empty vocabulary
    const N = grams.length, df = new Map();
    for (const g of grams) for (const t of new Set(g)) df.set(t, (df.get(t) || 0) + 1);
    const vecs = grams.map((g) => {
      const tf = new Map(); for (const t of g) tf.set(t, (tf.get(t) || 0) + 1);
      const v = new Map(); let norm = 0;
      for (const [t, c] of tf) { const w = (Math.log(c) + 1) * (Math.log((1 + N) / (1 + df.get(t))) + 1); v.set(t, w); norm += w * w; }
      norm = Math.sqrt(norm); if (norm > 0) for (const [t, w] of v) v.set(t, w / norm);
      return v;
    });
    const q = vecs[vecs.length - 1];
    return vecs.slice(0, -1).map((v) => CAF.lib.text.cosine(q, v));
  }
  const matchFilter = (value, wanted) => (Array.isArray(wanted) ? wanted.includes(value) || (Array.isArray(value) && value.some((x) => wanted.includes(x)))
    : value === wanted || (Array.isArray(value) && value.includes(wanted)));
  function vsUpsert(collection, docs) { if (!INDEX.has(collection)) INDEX.set(collection, new Map()); const col = INDEX.get(collection); for (const d of docs) col.set(String(d.id), { id: String(d.id), text: d.text, metadata: d.metadata || {} }); return col.size; }
  const vsCount = (collection) => (INDEX.get(collection) || new Map()).size;
  function vsSearch(collection, query, k = 5, filters = null) {
    let docs = [...(INDEX.get(collection) || new Map()).values()];
    if (filters) docs = docs.filter((d) => Object.entries(filters).every(([fk, fv]) => matchFilter(d.metadata[fk], fv)));
    if (!docs.length) return [];
    const scores = tfidfSimilarity(query, docs.map((d) => d.text));
    return docs.map((d, i) => [scores[i], d]).sort((a, b) => b[0] - a[0]).slice(0, k).map(([s, d]) => ({ ...d, score: round(s, 4) }));
  }

  // ---------------------------------------------------------------- grounding + compliance
  function passageSentences(text) {
    const out = [];
    let header = [];
    for (let line of splitlines(text)) {
      line = line.trim();
      if (line.startsWith("|")) {
        const cells = line.replace(/^\|+|\|+$/g, "").split("|").map((c) => c.trim());
        if (cells.filter(Boolean).every((c) => /^:?-{2,}:?$/.test(c))) continue;
        if (!header.length) { header = cells.map((c) => c.toLowerCase()); continue; }
        if (cells.length >= 3 && header.length >= 3 && header[1].includes("before") && header[2].includes("after")) out.push(`${cells[0]}: ${cells[1]} -> ${cells[2]}`);
        else out.push(cells.filter(Boolean).join("; "));
        continue;
      }
      header = [];
      line = normWs(line.replace(/^\s*(?:[-+]|\d+[.)])\s+/, "").replace(/[*_#>`]/g, ""));
      if (line) out.push(...line.split(/(?<=[.!?])\s+(?=[A-Z0-9"'(])/).map((x) => x.trim()).filter(Boolean));
    }
    return out;
  }
  function selectPassages(hits, limit, perDoc = 3) {
    const counts = new Map(), out = [];
    for (const h of hits) {
      const d = h.metadata.doc_id;
      if ((counts.get(d) || 0) >= perDoc) continue;
      counts.set(d, (counts.get(d) || 0) + 1); out.push(h);
      if (out.length >= limit) break;
    }
    return out;
  }
  const outcomeSections = (chunks, docIds, have) => chunks.filter((c) => docIds.has(c.metadata.doc_id) && !have.has(c.id) && /result|outcome|proof point|impact/i.test(c.metadata.section || ""))
    .map((c) => ({ ...c, score: 0, metadata: { ...c.metadata, augmented: true } }));
  function resolveIndustry(value, profiles) {
    const v = normWs(value).toLowerCase();
    for (const [pid, prof] of Object.entries(profiles)) {
      const names = new Set([pid, String(prof.name ?? "").toLowerCase(), ...(prof.aliases || []).map((a) => String(a).toLowerCase())]);
      if (names.has(v)) return pid;
    }
    for (const [pid, prof] of Object.entries(profiles))
      for (const a of [pid, ...(prof.aliases || []).map((x) => String(x).toLowerCase())]) if (new RegExp(`(?<![a-z0-9])${reEsc(a)}(?![a-z0-9])`).test(v)) return pid;
    return null;
  }
  const findPhrases = (text, phrases) => { const low = String(text || "").toLowerCase(); return phrases.filter((p) => p && new RegExp(`(?<![a-z0-9])${reEsc(String(p).toLowerCase())}(?![a-z0-9])`).test(low)); };
  const figures = (text) => new Set([...String(text || "").matchAll(FIGURE)].map((m) => m[0].replaceAll(",", "")));
  function verifyProofPoint(text, docText) { const need = figures(text), have = figures(docText); return need.size > 0 && [...need].every((f) => have.has(f)); }
  function outcomeScore(sentence, section) {
    let score = 0;
    if (/result|outcome|impact|proof|benefit/i.test(section || "")) score += 3;
    if (/challenge|problem|context|background/i.test(section || "")) score -= 3;
    if (OUTCOME.test(sentence)) score += 2;
    return score;
  }
  /** Deterministic fallback: verbatim quantified outcome statements from retrieved passages, verified against the document's figures. */
  function extractProofPoints(passages, docs, limit = 3, perDoc = 2) {
    const order = { case_study: 0, analyst_note: 1, profile: 2 }, cands = [];
    passages.forEach((p, rank) => {
      const meta = p.metadata, docText = (docs[meta.doc_id] || {}).text ?? p.text;
      passageSentences(p.text).forEach((sent, pos) => {
        if (len(sent) >= 25 && len(sent) <= 260 && /\p{Nd}/u.test(sent) && QUANT.test(sent) && verifyProofPoint(sent, docText))
          cands.push([-outcomeScore(sent, meta.section || ""), order[meta.doc_type || ""] ?? 3, rank, pos, sent, meta.doc_id]);
      });
    });
    const out = [], seen = new Set(), counts = new Map();
    for (const [neg, , , , sent, docId] of cands.sort(cmpTuple)) {
      if (-neg < 2 || seen.has(sent.toLowerCase()) || (counts.get(docId) || 0) >= perDoc) continue;
      seen.add(sent.toLowerCase()); counts.set(docId, (counts.get(docId) || 0) + 1);
      out.push({ text: sent, source_doc: docId, verified: true, basis: "corpus_extract" });
      if (out.length >= limit) break;
    }
    return out;
  }
  const normalizeDocId = (v) => String(v || "").trim().replace(/^[[\]()]+|[[\]()]+$/g, "").trim().replace(/#\d+$/, "");
  function matchPersonas(names, profilePersonas, focus) {
    const byName = new Map(profilePersonas.map((p) => [String(p.name).toLowerCase(), p])), picked = [];
    for (const n of names || []) {
      const nl = String(n).toLowerCase();
      const hit = byName.get(nl) || [...byName].find(([k]) => k.includes(nl) || nl.includes(k))?.[1] || null;
      if (hit && !picked.includes(hit)) picked.push(hit);
    }
    if (!picked.length && focus) { const hit = [...byName].find(([k]) => k.includes(focus.toLowerCase()) || focus.toLowerCase().includes(k)); if (hit) picked.push(hit[1]); }
    return picked.length ? picked : profilePersonas.slice(0, 2);
  }
  // LocalIngestion twins for `source` (paths -> embedded datasets, demo-site URLs -> page text)
  async function loadSource(ctx, s) {
    s = String(s).trim();
    if (/^https?:\/\//i.test(s)) {
      if (!ctx.isDemoUrl(s)) throw new Error(`ConnectorError: the hosted console cannot fetch ${s} (no network) - paste the content into source_text`);
      const pg = ctx.site(s);
      if (!pg || pg.status < 200 || pg.status >= 300) throw new Error(`ConnectorError: Could not fetch ${s}: status ${pg ? pg.status : 0}`);
      const doc = CAF.lib.html.parse(pg.html);
      doc.querySelectorAll("script, style, noscript, template, nav, header, footer, aside, form").forEach((e) => e.remove());
      const root = doc.querySelector("article") || doc.querySelector("main") || doc.body;
      return [...(root ? root.querySelectorAll("h1, h2, h3, h4, p, li, td") : [])].map((e) => normWs(e.textContent)).filter(Boolean).join("\n");
    }
    const name = s.replace(/^\.?\//, "").replace(/^sample_data\//, "").replace(/\.[A-Za-z0-9]+$/, "");
    if (ctx.hasData(name)) return String(await ctx.dataRaw(name));
    if (len(s) > 200 || s.includes("\n")) return s;
    throw new Error(`ConnectorError: Source not found: ${s}`);
  }

  const lib = { parseFrontMatter, loadCorpus, blocks, corpusChunks, tfidfSimilarity, passageSentences, selectPassages, outcomeSections, resolveIndustry, findPhrases,
    figures, verifyProofPoint, outcomeScore, extractProofPoints, normalizeDocId, matchPersonas, vsSearch, vsUpsert };

  CAF.agent("industry_marketing", {
    lib,
    async run(input, ctx) {
      // -- profile + source
      const ids = Object.keys(CAF.skills).filter((n) => n.startsWith("industries/")).map((n) => n.split("/").slice(1).join("/")).sort(cmp);
      const profiles = Object.fromEntries(ids.map((i) => [i, ctx.skill(`industries/${i}`)]));
      const pid = resolveIndustry(input.industry, profiles);
      if (!pid) throw new Error(`Unknown industry '${input.industry}'. Profiles available: ${pyList(Object.keys(profiles).sort(cmp))} (config/skills/industries/)`);
      const prof = profiles[pid];
      let text = String(input.source_text || "").trim(), origin = "source_text";
      if (!text && input.source) {
        try { text = (await loadSource(ctx, input.source)).trim(); origin = input.source; }
        catch (e) { ctx.warn(`Could not load ${input.source}: ${(e && e.message) || e}`); if (/no network/.test(String(e && e.message))) ctx.degraded("the source URL is outside the demo site and cannot be fetched in the hosted console (no network)"); }
      }
      if (!text) { text = ctx.upstreamText(6000).trim(); origin = "upstream"; }
      if (!text) throw new Error("Provide source_text or source (URL / path) - or run after a content agent in an orchestrated plan.");

      // -- retrieval over the corpus (in-memory index, filtered by industry)
      const prefix = String(input.corpus_path || "sample_data/industry_corpus").replace(/^\.?\//, "").replace(/^sample_data\//, "").replace(/\/+$/, "");
      const names = CAF.datasets.names().filter((n) => n.startsWith(prefix + "/") && !n.slice(prefix.length + 1).includes("/"))
        .sort((a, b) => cmp(`${a}.md`, `${b}.md`));
      let docs = {}, chunks = [];
      if (names.length) {
        const files = [];
        for (const n of names) files.push([n.slice(prefix.length + 1), String(await ctx.dataRaw(n))]);
        const loaded = loadCorpus(files);
        docs = Object.fromEntries(loaded.map((d) => [d.doc_id, d]));
        chunks = corpusChunks(loaded);
        const fp = U.hash(files.map(([n, t]) => `${n}:${t.length}`).join("|"));
        if (INDEXED.get(prefix) !== fp || !vsCount(COLLECTION)) {
          vsUpsert(COLLECTION, chunks);
          INDEXED.set(prefix, fp);
          ctx.check({ rule: "corpus.indexed", status: "info", severity: "low", message: `Indexed ${loaded.length} documents into '${COLLECTION}' (${vsCount(COLLECTION)} chunks).` });
        }
      } else if (!vsCount(COLLECTION)) ctx.degraded(`industry corpus not found at ${input.corpus_path} and the vector store is empty - copy is not grounded in your documents`);
      const query = [truncate(text, 1500, ""), input.persona || "", input.asset_type || "", (prof.buyer_priorities || []).slice(0, 3).join(" ")].filter(Boolean).join(" ");
      const maxP = Number(input.max_passages);
      const hits = vsCount(COLLECTION) ? vsSearch(COLLECTION, query, maxP * 3, { industry: [pid, ...SHARED] }) : [];
      const passages = selectPassages(hits, maxP);
      const outcome = outcomeSections(chunks, new Set(passages.map((p) => p.metadata.doc_id)), new Set(passages.map((p) => p.id)));
      if (outcome.length) {
        passages.push(...outcome);
        ctx.check({ rule: "retrieval.outcome_sections", status: "info", severity: "low", message: `Added ${outcome.length} results / proof-point section(s) of the retrieved documents.` });
      }
      const perDoc = new Set(passages.map((p) => p.metadata.doc_id)), wrong = passages.filter((p) => ![pid, ...SHARED].includes(p.metadata.industry)).map((p) => p.id);
      ctx.check({ rule: "retrieval.filter", status: wrong.length ? "fail" : "pass", severity: "high",
        message: `${passages.length} passages from ${perDoc.size} ${pid} document(s)` + (wrong.length ? `; off-industry: ${pyList(wrong)}` : ".") });
      if (!passages.length && !ctx.state.degraded.some((r) => r.startsWith("industry corpus not found"))) ctx.degraded(`no corpus passages for industry '${pid}' - copy relies on the profile only`);
      if (!passages.length) ctx.warn("No grounding passages were retrieved; proof points cannot be cited.");

      // -- tailor (model, grounded)
      const comp = prof.compliance || {}, term = prof.terminology || {};
      const promptVars = { industry_name: prof.name ?? pid, persona: input.persona ?? null, asset_type: input.asset_type ?? null, region: input.region ?? null,
        source_text: truncate(text, 6000), terminology: [...(term.preferred || [])],
        avoid: (term.avoid || []).filter((a) => a && typeof a === "object").map((a) => `say '${a.use}' instead of '${a.term}'`),
        personas: (prof.personas || []).map((p) => `${p.name} - priorities ${(p.priorities || []).join(", ")}`), buyer_priorities: prof.buyer_priorities || [],
        must: comp.must || [], never: comp.never || [], disclaimers: comp.required_disclaimers || [], regulators: comp.regulators || [],
        proof_guidance: (prof.proof_points || {}).guidance ?? "", tone: prof.tone ?? "",
        passages: passages.map((p) => `[${p.metadata.doc_id}] (${p.metadata.doc_type ?? ""}: ${p.metadata.title ?? ""}) ${truncate(normWs(p.text), 900)}`) };
      ctx.progress(`Tailoring for ${prof.name ?? pid}`);
      let draft = await ctx.generate("tailor", promptVars);

      // -- deterministic compliance, grounding and reconciliation
      const never = [...(comp.never || []), ...(ctx.brand.banned_phrases || [])];
      let found = findPhrases(`${draft.tailored_headline}\n${draft.tailored_copy}`, never), repaired = false;
      if (found.length && ctx.llmLive) { // one model repair pass, then report what is left
        ctx.progress("Repairing prohibited phrases");
        draft = await ctx.generate("repair", { draft: { ...draft }, violations: found, ...promptVars });
        repaired = true;
        found = findPhrases(`${draft.tailored_headline}\n${draft.tailored_copy}`, never);
      }
      ctx.check({ rule: "compliance.never_phrases", status: found.length ? "fail" : "pass", severity: found.length ? "critical" : "low", ref: `industries/${pid} v${prof.version ?? ""}`,
        message: found.length ? `Prohibited phrases for ${prof.name ?? "None"}: ${pyList(found)}` : "No prohibited phrases." });
      const retrieved = new Map();
      for (const p of passages) retrieved.set(p.metadata.doc_id, p);
      let copy = String(draft.tailored_copy || "").trim();
      const refs = new Set([...copy.matchAll(/\[([A-Za-z0-9_-]+)(?:#\d+)?\]/g)].map((m) => m[1]));
      const unknownRefs = [...refs].filter((r) => !retrieved.has(r) && (r in docs || r.includes("_"))).sort(cmp);
      if (unknownRefs.length) {
        copy = copy.replace(new RegExp(`\\s?\\[(?:${unknownRefs.map(reEsc).join("|")})(?:#\\d+)?\\]`, "g"), "");
        ctx.check({ rule: "grounding.citations", status: "warn", severity: "medium", message: `Removed citations to documents that were not retrieved: ${pyList(unknownRefs)}` });
      }
      let proofs = [];
      const dropped = [];
      for (const pp of draft.proof_points || []) {
        const docId = normalizeDocId(pp.source_doc), t = normWs(pp.text);
        if (!t) continue;
        if (!retrieved.has(docId)) dropped.push(`'${truncate(t, 60)}' (unknown source '${pp.source_doc}')`);
        else if (!verifyProofPoint(t, (docs[docId] || {}).text ?? retrieved.get(docId).text)) dropped.push(`'${truncate(t, 60)}' (figures not found in ${docId})`);
        else proofs.push({ text: t, source_doc: docId, verified: true, basis: "model" });
      }
      if (dropped.length) ctx.check({ rule: "grounding.proof_points", status: "warn", severity: "high", message: `Dropped ${dropped.length} unverifiable proof point(s): ` + dropped.slice(0, 4).join("; ") });
      if (!proofs.length && passages.length) {
        proofs = extractProofPoints(passages, docs);
        if (proofs.length) ctx.check({ rule: "grounding.proof_points", status: "info", severity: "low", message: `${proofs.length} proof point(s) extracted verbatim from retrieved case material.` });
      }
      ctx.check({ rule: "grounding.proof_points_verified", status: proofs.length ? "pass" : "warn", severity: "medium",
        message: proofs.length ? `${proofs.length} proof point(s), each traced to a retrieved document with matching figures.` : "No verifiable proof points." });
      const added = [];
      for (const disc of comp.required_disclaimers || []) if (!copy.toLowerCase().includes(String(disc).toLowerCase().replace(/\.+$/, ""))) { copy = `${copy}\n\n_${disc}_`; added.push(disc); }
      ctx.check(added.length ? { rule: "compliance.disclaimers", status: "info", severity: "low", message: `Appended required disclaimer(s): ${pyList(added)}` }
        : { rule: "compliance.disclaimers", status: "pass", severity: "low", message: "Required disclaimers present." });
      const avoidFound = (term.avoid || []).filter((a) => a && typeof a === "object" && findPhrases(copy, [a.term]).length).map((a) => `${a.term} -> ${a.use}`);
      if (avoidFound.length) ctx.check({ rule: "terminology.avoid", status: "warn", severity: "low", message: `Generic terms with a sector equivalent: ${pyList(avoidFound)}` });
      const terms = [...new Set(findPhrases(`${draft.tailored_headline}\n${copy}`, [...(term.preferred || [])]))];
      ctx.check({ rule: "terminology.sector_terms", status: terms.length >= 2 ? "pass" : "warn", severity: "low", message: `${terms.length} sector term(s) used: ${pyList(terms.slice(0, 8))}` });
      const personas = matchPersonas(draft.personas || [], prof.personas || [], input.persona || null);
      const cited = [...new Set([...proofs.map((p) => p.source_doc), ...(draft.citations || []).map(normalizeDocId).filter((c) => retrieved.has(c))])];
      for (const d of retrieved.keys()) if (!cited.includes(d) && new RegExp(`\\[${reEsc(d)}(?:#\\d+)?\\]`).test(copy)) cited.push(d);
      const citations = cited.map((d) => { const p = retrieved.get(d); return { doc_id: d, title: p.metadata.title ?? d, doc_type: p.metadata.doc_type ?? "", industry: p.metadata.industry ?? "", score: p.score ?? 0 }; });
      const compliance = { passed: !found.length, never_phrases_found: found, disclaimers_added: added, avoid_terms_found: avoidFound, must_rules: [...(comp.must || [])], repaired };

      // -- export
      const headline = normWs(draft.tailored_headline);
      const output = { industry: pid, industry_name: prof.name ?? pid, profile_version: String(prof.version ?? ""), asset_type: input.asset_type || "", tailored_headline: headline,
        tailored_copy: copy, key_terms_used: terms, personas: personas.map((p) => ({ name: p.name, priorities: [...(p.priorities || [])] })), proof_points: proofs, citations,
        compliance, grounded: passages.length > 0, passages_used: passages.length };
      const md = [`# ${headline}`, "", copy, "", "## Proof points", ...(proofs.length ? proofs.map((p) => `- ${p.text} [${p.source_doc}]`) : ["_None verified._"]),
        "", "## Sources", ...(citations.length ? citations.map((c) => `- \`${c.doc_id}\` - ${c.title} (${c.doc_type})`) : ["_No corpus sources._"]),
        "", `_Profile: industries/${pid} v${output.profile_version} · compliance ${compliance.passed ? "passed" : "FAILED"}_`];
      ctx.artifact({ name: "industry_copy.md", mime: "text/markdown", content: md.join("\n") + "\n", description: `${output.industry_name} copy` });
      ctx.artifact({ name: "industry_copy.json", mime: "application/json", content: JSON.stringify(output, null, 2), description: "Tailored copy with proof points and citations" });
      const summary = `${output.industry_name} ${output.asset_type}: '${truncate(headline, 70)}' - ${terms.length} sector terms, ${proofs.length} verified proof point(s), ${citations.length} cited doc(s), compliance `
        + (compliance.passed ? "passed" : "FAILED") + (output.grounded ? "" : " - not grounded");
      return {
        output, summary,
        view: [
          { type: "kv", title: output.industry_name, data: { profile: `industries/${pid} v${output.profile_version}`, asset_type: output.asset_type, source: origin,
            passages: passages.length, compliance: compliance.passed ? "passed" : "FAILED", disclaimers_added: added.length } },
          { type: "markdown", title: headline, markdown: copy },
          { type: "table", title: "Proof points", columns: ["proof_point", "source_doc", "basis"], rows: proofs.map((p) => ({ proof_point: p.text, source_doc: p.source_doc, basis: p.basis })) },
          { type: "table", title: "Retrieved passages", columns: ["id", "doc_type", "section", "score"],
            rows: passages.map((p) => ({ id: p.id, doc_type: p.metadata.doc_type, section: p.metadata.section || "-", score: p.metadata.augmented ? "added (results section)" : p.score })) },
        ],
      };
    },
  });
})();
