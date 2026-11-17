/* Content Summary - browser twin of caf/agents/content/content_summary.py.
   ingest -> semantic chunks -> MAP per chunk (parallel model calls; extractive candidates always) ->
   REDUCE (hierarchical collapse over the budget, then the mode template) -> FORMAT (meta <= 155 chars,
   numbers grounding). Offline / confidential: deterministic extractive summary (verbatim sentences). */
(() => {
  const T = () => CAF.lib.text;
  const MODES = ["executive", "technical", "bullet", "abstract", "action_items", "meta_description"];
  const META_MAX = 155, META_MIN = 50, MAX_COLLAPSE = 6;
  const MODE_WORDS = { executive: 250, technical: 450, bullet: 180, abstract: 180, action_items: 220, meta_description: 30 };
  const MODE_KP = { executive: 5, technical: 7, bullet: 7, abstract: 4, action_items: 4, meta_description: 3 };

  const TS_PREFIX = /^\s*\[(\d{1,2}:\d{2}(?::\d{2})?)\]\s*(?:([^:\n\]]{1,80}?):\s+)?/;
  const LIST_ITEM = /^(?:[-*+•]|\d+[.)])\s+/;
  const NUM_G = /(?<![\p{L}\p{N}_.])[$€£]?\p{Nd}[\p{Nd},]*(?:\.\p{Nd}+)?%?/gu; // _NUM (Unicode \w / \d like Python)
  const NUM_1 = new RegExp(NUM_G.source, "u");
  const ACTION = new RegExp("\\b(should|must|need to|needs to|have to|has to|will need to|recommend(?:s|ed)?|next steps?|action items?|" +
    "we will|we'll|plan to|plans to|is required to|are required to|ensure that|make sure|" +
    "will (?:publish|launch|deliver|send|share|release|introduce|roll out|complete|review|update|host|run|provide|" +
    "offer|contact|follow up|create|build|start|hold|issue|email|circulate))\\b", "i");
  const IMPERATIVE = new RegExp("^(please\\s+)?(review|schedule|update|prepare|send|share|complete|submit|confirm|book|register|download|contact|call|" +
    "check|ensure|implement|adopt|launch|create|set up|follow up|document|install|test|train|start|stop|move|reduce|" +
    "increase|clear|photograph|keep|store|seal|inspect|replace|upgrade|join|visit|sign up)\\b", "i");
  const DUE = new RegExp("\\b(by|before|within|no later than|until)\\s+((?:the\\s+)?(?:end of\\s+)?(?:Q[1-4](?:\\s+\\d{4})?|" +
    "\\d{1,2}\\s+\\w+|\\w+\\s+\\d{1,2}(?:,\\s*\\d{4})?|\\d+\\s+(?:days?|weeks?|months?)|\\w+\\s+\\d{4}|" +
    "(?:january|february|march|april|may|june|july|august|september|october|november|december|" +
    "winter|spring|summer|autumn|fall|next\\s+\\w+|this\\s+\\w+|year-end|month-end)))", "i");
  const OWNER = /^(?:the\s+)?((?:[a-z][\w&-]*\s+){0,2}team|[A-Z][\w&-]*(?:\s+[A-Z][\w&-]*){0,3}|we|you)\s+(?:should|must|will|needs? to|has to|have to|plans? to|is required to|are required to)\b/i;
  const NOT_OWNER = new Set(["it", "this", "that", "there", "they", "these", "those", "which", "who", "what", "he", "she", "one"]);

  const normWs = (t) => String(t || "").replace(/\s+/g, " ").trim();
  const wc = (t) => T().wordCount(t);
  function pyFixed(x, nd = 0) { // format(x, f".{nd}f"): correctly rounded, exact ties to even
    if (!Number.isFinite(x)) return String(x);
    const exact = Math.abs(x).toFixed(Math.min(100, nd + 40));
    const [ip, fp = ""] = exact.split(".");
    const tail = fp.slice(nd);
    if (tail[0] === "5" && /^0*$/.test(tail.slice(1))) {
      let digits = BigInt(ip + fp.slice(0, nd));
      if (digits % 2n === 1n) digits += 1n;
      let s = digits.toString().padStart(nd + 1, "0");
      if (nd) s = `${s.slice(0, -nd)}.${s.slice(-nd)}`;
      return (x < 0 ? "-" : "") + s;
    }
    return x.toFixed(nd);
  }
  const round = (x, d) => (Number.isFinite(x) ? Number(pyFixed(x, d)) : x); // Python round()

  /* ------------------------------------------------------------- local ingestion (twin of the ingestion connector) */
  function guessTitle(text) { // caf.connectors.ingestion._guess_title
    const lines = String(text || "").split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
    for (const l of lines) if (l.startsWith("#")) return l.replace(/^#+/, "").trim().slice(0, 120);
    for (const t of lines.slice(0, 40)) {
      const low = t.toLowerCase();
      if (low.startsWith("illustrative") || low.startsWith("[page") || low.startsWith("---") || /^\[?\d{1,2}:\d{2}/.test(t)) continue;
      const n = t.split(/\s+/).length;
      if (n >= 2 && n <= 16 && !/[.,;:]$/.test(t) && /^\p{Lu}/u.test(t)) return t.slice(0, 120);
    }
    return lines.length ? lines[0].slice(0, 120) : "Untitled";
  }
  function htmlToText(html) { // readable Markdown-ish blocks; stand-in for trafilatura (no figures, <br> breaks, FAQ summaries as headings)
    const doc = new DOMParser().parseFromString(String(html || ""), "text/html");
    const title = normWs((doc.querySelector("title") || {}).textContent || "");
    doc.querySelectorAll("script,style,noscript,nav,footer,header,aside,form,template,svg,button,iframe,figure").forEach((e) => e.remove());
    doc.querySelectorAll("br").forEach((e) => e.replaceWith("\n"));
    const lines = (node) => String(node.textContent || "").split("\n").map(normWs).filter(Boolean);
    const out = [];
    const walk = (el) => {
      for (const node of el ? el.children : []) {
        const tag = node.tagName.toLowerCase(), text = normWs(node.textContent);
        if (/^h[1-6]$/.test(tag)) { if (text) out.push("#".repeat(+tag[1]) + " " + text); }
        else if (tag === "summary") { if (text) out.push("## " + text); }
        else if (["p", "figcaption", "dt", "dd", "pre"].includes(tag)) out.push(...lines(node));
        else if (tag === "blockquote") out.push(...lines(node).map((l) => "> " + l));
        else if (tag === "li") { if (text) out.push("- " + text); }
        else if (tag === "tr") { const cells = [...node.children].map((c) => normWs(c.textContent)); if (cells.some(Boolean)) out.push("| " + cells.join(" | ") + " |"); }
        else if ([...node.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim().length > 20) || !node.children.length) { if (text && CAF.lib.text.wordCount(text) > 3) out.push(text); }
        else walk(node);
      }
    };
    walk(doc.querySelector("main") || doc.querySelector("article") || doc.body);
    const text = out.join("\n\n");
    return { title: title || guessTitle(text), text };
  }
  const MIME = { md: "text/markdown", pdf: "application/pdf", docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    txt: "text/plain", vtt: "text/vtt", srt: "text/vtt", html: "text/html", htm: "text/html", tmx: "text/plain", csv: "text/csv", json: "application/json" };
  const datasetName = (p) => String(p || "").trim().replace(/^\.?\//, "").replace(/^sample_data\//, "").replace(/\.[A-Za-z0-9]+$/, "");
  /** {doc} | {unavailable: reason} | {error} - mirrors LocalIngestion.load(source|text|html|path). */
  async function loadDocument(ctx, { source, path, text, html }) {
    if (text) return { doc: { source: "inline-text", title: guessTitle(text), text, mime: "text/plain" } };
    if (html) { const h = htmlToText(html); return { doc: { source: "inline-html", title: h.title, text: h.text, mime: "text/html" } }; }
    const ref = path || source;
    if (!ref) return {};
    const s = String(ref).trim();
    if (/^https?:\/\//i.test(s)) {
      if (!ctx.isDemoUrl(s)) return { unavailable: `the hosted console cannot fetch ${s} - paste the page text or HTML, or run the Python service` };
      const page = ctx.page({ url: s });
      if (page.status !== 200) return { error: `Could not fetch ${s}: status ${page.status}` };
      const h = htmlToText(page.html);
      return { doc: { source: s, title: h.title, text: h.text, mime: "text/html" } };
    }
    const name = datasetName(s);
    if (ctx.hasData(name)) {
      const ext = (s.match(/\.([A-Za-z0-9]+)$/) || [, "txt"])[1].toLowerCase();
      const raw = await ctx.dataRaw(name);
      if (ext === "html" || ext === "htm") { const h = htmlToText(raw); return { doc: { source: s, title: h.title, text: h.text, mime: "text/html" } }; }
      const pages = (raw.match(/\[page \d+\]/g) || []).length;
      return { doc: { source: s, title: guessTitle(ext === "pdf" ? raw.replace("[page 1]", "") : raw), text: raw, mime: MIME[ext] || "text/plain", pages } };
    }
    if (s.length > 200 || s.includes("\n")) return { doc: { source: "inline-text", title: guessTitle(s), text: s, mime: "text/plain" } };
    return { unavailable: `'${s}' is not embedded in the hosted console - paste its text into the text field, or run the Python service` };
  }

  /* ------------------------------------------------------------- pure helpers (ported 1:1) */
  const isAside = (text) => { const t = String(text || "").trim(); const l = t.toLowerCase();
    return t.startsWith("(") || l.startsWith("illustrative") || l.startsWith("disclaimer") || ((t.endsWith(")") || t.endsWith(").")) && !t.includes("(")); };
  const cleanUnit = (text) => normWs(String(text || "").replace(/\[page \d+\]/gi, " ").replace(TS_PREFIX, "").replace(/\[([^\]]+)\]\([^)]+\)/g, "$1").replace(/(\*\*|__|`)/g, ""));
  const startsWell = (t) => { const c = t.replace(/^["“‘']+/, "").slice(0, 1); return /\p{Lu}|\d/u.test(c); };
  const titleLike = (t) => { const n = wc(t); return n >= 2 && n <= 24 && t.length <= 140 && startsWell(t) && !isAside(t) && !/\[page \d+\]/i.test(t) && !/[.,;:]$/.test(t); };
  function cleanTitle(title, source = "", text = "") {
    const t = cleanUnit(String(title || "").replace(/^#+\s*/, ""));
    if (t && !isAside(t) && t.length <= 140 && wc(t) >= 2 && startsWell(t)) return t;
    for (const line of String(text || "").split(/\r?\n/).slice(0, 25)) {
      const cand = cleanUnit(line.trim().replace(/^#+\s*/, ""));
      if (cand && titleLike(cand)) return cand;
    }
    const stem = String(source || "").replace(/\\/g, "/").split("/").pop().replace(/\.[^.]*$/, "").replace(/[_-]+/g, " ").trim();
    if (stem && !["inline text", "inline html", "upstream output"].includes(stem)) return stem.charAt(0).toUpperCase() + stem.slice(1).toLowerCase();
    return t.length > 140 ? t.slice(0, 137) + "..." : t || "Untitled";
  }
  function textUnits(text) {
    const units = []; let lead = true, inCode = false;
    const emit = (paragraph, isLead) => {
      const cleaned = cleanUnit(paragraph);
      if (isAside(cleaned)) return;
      T().sentences(cleaned).forEach((s, j) => { if (s) units.push({ text: s, lead: isLead && j === 0 }); });
    };
    for (const block of String(text || "").split(/\n\s*\n/)) {
      let buf = [];
      for (const raw of block.split(/\r\n|\r|\n/)) {
        const s = raw.trim();
        if (s.startsWith("```")) { inCode = !inCode; continue; }
        if (inCode || !s) continue;
        if (s.startsWith("#")) { if (buf.length) { emit(buf.join(" "), lead); buf = []; } lead = true; continue; }
        if (s.startsWith("|") || /^[-=*_]{3,}$/.test(s) || /^\[page \d+\]$/i.test(s)) continue;
        if (TS_PREFIX.test(s) || LIST_ITEM.test(s)) {
          if (buf.length) { emit(buf.join(" "), lead); buf = []; lead = false; }
          emit(s.replace(LIST_ITEM, ""), lead); lead = false; continue;
        }
        buf.push(s);
      }
      if (buf.length) { emit(buf.join(" "), lead); lead = false; }
    }
    return units;
  }
  /* sklearn TfidfVectorizer(stop_words="english", sublinear_tf=True, ngram_range=(1, 2), min_df=1), ported exactly:
     lowercase, token_pattern (?u)\b\w\w+\b, stop words removed before bigrams, tf = 1 + ln(count),
     idf = ln((1 + n) / (1 + df)) + 1, L2-normalised rows. Returns sparse rows (Map) or null for an empty vocabulary. */
  const SK_STOP = new Set(("a about above across after afterwards again against all almost alone along already also although always am among amongst amoungst amount an and another any anyhow anyone anything anyway anywhere are around as at back be became because become becomes becoming been before beforehand behind being below beside besides between beyond bill both bottom but by call can cannot cant co con could couldnt cry de describe detail do done down due during each eg eight either eleven else elsewhere empty enough etc even ever every everyone everything everywhere except few fifteen fifty fill find fire first five for former formerly forty found four from front full further get give go had has hasnt have he hence her here hereafter hereby herein hereupon hers herself him himself his how however hundred i ie if in inc indeed interest into is it its itself keep last latter latterly least less ltd made many may me meanwhile might mill mine more moreover most mostly move much must my myself name namely neither never nevertheless next nine no nobody none noone nor not nothing now nowhere of off often on once one only onto or other others otherwise our ours ourselves out over own part per perhaps please put rather re same see seem seemed seeming seems serious several she should show side since sincere six sixty so some somehow someone something sometime sometimes somewhere still such system take ten than that the their them themselves then thence there thereafter thereby therefore therein thereupon these they thick thin third this those though three through throughout thru thus to together too top toward towards twelve twenty two un under until up upon us very via was we well were what whatever when whence whenever where whereafter whereas whereby wherein whereupon wherever whether which while whither who whoever whole whom whose why will with within without would yet you your yours yourself yourselves").split(" "));
  const SK_TOKEN = /(?<![\p{L}\p{N}_])[\p{L}\p{N}_]{2,}(?![\p{L}\p{N}_])/gu;
  function tfidf(texts) {
    const docs = texts.map((t) => {
      const toks = (String(t ?? "").toLowerCase().match(SK_TOKEN) || []).filter((w) => !SK_STOP.has(w));
      const grams = [...toks];
      for (let i = 0; i + 1 < toks.length; i++) grams.push(`${toks[i]} ${toks[i + 1]}`);
      const tf = new Map();
      for (const g of grams) tf.set(g, (tf.get(g) || 0) + 1);
      return tf;
    });
    const df = new Map();
    for (const tf of docs) for (const g of tf.keys()) df.set(g, (df.get(g) || 0) + 1);
    if (!df.size) return null; // sklearn raises "empty vocabulary"
    const n = texts.length;
    return docs.map((tf) => {
      const row = new Map();
      let ss = 0;
      for (const [g, c] of tf) { const w = (Math.log(c) + 1) * (Math.log((1 + n) / (1 + df.get(g))) + 1); row.set(g, w); ss += w * w; }
      const norm = Math.sqrt(ss);
      if (norm > 0) for (const [g, w] of row) row.set(g, w / norm);
      return row;
    });
  }
  const sparseDot = (a, b) => { if (a.size > b.size) [a, b] = [b, a]; let d = 0; for (const [g, w] of a) { const x = b.get(g); if (x !== undefined) d += w * x; } return d; };
  function centrality(texts) { // cosine of each sentence to the TF-IDF centroid (centrality_scores)
    if (!texts.length) return [];
    if (texts.length === 1) return [1.0];
    const m = tfidf(texts);
    if (!m) return texts.map(() => 0.0);
    const centroid = new Map();
    for (const row of m) for (const [g, w] of row) centroid.set(g, (centroid.get(g) || 0) + w);
    let ss = 0;
    for (const [g, v] of centroid) { const x = v / texts.length; centroid.set(g, x); ss += x * x; }
    const norm = Math.sqrt(ss) || 1.0;
    return m.map((row) => sparseDot(row, centroid) / norm);
  }
  const overlap = (a, b) => [...b].filter((x) => a.has(x)).length;
  function sentenceScore(text, cent, { lead = false, firstPart = false, title = "", focus = null } = {}) {
    const n = wc(text);
    let s = cent;
    if (lead) s += 0.08;
    if (firstPart && lead) s += 0.05;
    if (NUM_1.test(text)) s += 0.05;
    const toks = new Set(T().tokens(text)); const tt = new Set(T().tokens(title));
    if (tt.size) s += 0.10 * overlap(toks, tt) / tt.size;
    if (focus) { const ft = new Set(T().tokens(focus)); if (ft.size) s += 0.25 * overlap(toks, ft) / ft.size; }
    if (n < 6) s *= 0.3; else if (n < 9) s *= 0.7; else if (n > 50) s *= 0.6;
    if (text.trimEnd().endsWith("?")) s *= 0.6;
    if (isAside(text)) s *= 0.3;
    return round(s, 6);
  }
  function rankCandidates(text, partIndex, { title = "", focus = null, k = 8 } = {}) {
    const units = textUnits(text);
    if (!units.length) return [];
    const cent = centrality(units.map((u) => u.text));
    const scored = units.map((u, i) => ({ text: u.text, score: sentenceScore(u.text, cent[i], { lead: u.lead, firstPart: partIndex === 0, title, focus }), pos: [partIndex, i], lead: u.lead }));
    scored.sort((a, b) => b.score - a.score || a.pos[0] - b.pos[0] || a.pos[1] - b.pos[1]);
    return scored.slice(0, k);
  }
  function mmrSelect(texts, scores, k, lam = 0.7) { // incremental maximal marginal relevance (mmr_select)
    if (!texts.length || k <= 0) return [];
    const n = texts.length, m = tfidf(texts);
    const sim = (i, j) => (m ? (m[i].size && m[j].size ? sparseDot(m[i], m[j]) : 0) : i === j ? 1 : 0); // cosine_similarity (or eye)
    const top = Math.max(...scores) || 1.0;
    const rel = scores.map((s) => s / top);
    const red = new Array(n).fill(0), available = new Array(n).fill(true);
    const chosen = [];
    while (chosen.length < Math.min(k, n)) {
      let best = -1, bestVal = -Infinity;
      for (let i = 0; i < n; i++) {
        const v = available[i] && red[i] <= 0.8 ? lam * rel[i] - (1 - lam) * red[i] : -Infinity;
        if (v > bestVal) { bestVal = v; best = i; } // np.argmax: first maximum
      }
      if (best < 0 || !Number.isFinite(bestVal)) break;
      chosen.push(best); available[best] = false;
      for (let i = 0; i < n; i++) red[i] = Math.max(red[i], sim(best, i));
    }
    return chosen;
  }
  function detectActionItems(text, limit = 8) {
    const out = [];
    for (const u of textUnits(text)) {
      const s = u.text;
      if (wc(s) < 6 || s.endsWith("?") || isAside(s) || /["”]\s*[-–—]\s*[A-Z]/.test(s) || /^(and|but|so|or|because|which)\b/i.test(s)) continue;
      if (!ACTION.test(s) && !IMPERATIVE.test(s)) continue;
      let owner = null;
      const m = s.match(OWNER);
      if (m && !NOT_OWNER.has(m[1].split(/\s+/)[0].toLowerCase())) owner = m[1];
      const d = s.match(DUE);
      const due = d ? normWs(d[2]).replace(/[.,;]+$/, "") : null;
      const strength = IMPERATIVE.test(s) || /\b(must|need to|needs to|recommend)/i.test(s) ? 2 : 1;
      out.push({ action: s, owner, due, strength });
    }
    return out.sort((a, b) => b.strength - a.strength).slice(0, limit); // stable, like Python's sort
  }
  function fitMeta(text, limit = META_MAX) {
    let t = normWs(cleanUnit(String(text || "").replace(/^#+\s*/, ""))).replace(/^["“” ]+|["“” ]+$/g, "");
    if (t.length <= limit) return t;
    const win = t.slice(0, limit + 1);
    for (const [rx, keep] of [[/[.!?](?=\s)/g, true], [/[,;:–—-](?=\s)/g, false]]) {
      const ends = [...win.matchAll(rx)].map((m) => m.index + 1).filter((e) => e >= 70 && e <= limit);
      if (ends.length) {
        let cut = win.slice(0, ends[ends.length - 1]).trimEnd();
        if (!keep) { cut = cut.replace(/[,;:–—\- ]+$/, "") + "."; if (cut.length > limit) continue; }
        return cut;
      }
    }
    let cut = t.slice(0, limit - 3); const sp = cut.lastIndexOf(" "); if (sp >= 0) cut = cut.slice(0, sp);
    return cut.replace(/[,;:.–—\- ]+$/, "") + "...";
  }
  function numericTokens(text) {
    const out = [];
    for (const m of String(text || "").matchAll(NUM_G)) {
      let v = m[0].replace(/^[$€£]+/, "").replace(/,/g, "");
      if (v.endsWith(".")) v = v.slice(0, -1);
      if (v && v !== "." && v !== "%") out.push(v);
    }
    return out;
  }
  function ungroundedNumbers(summary, source) {
    const src = new Set(numericTokens(source)); [...src].forEach((v) => src.add(v.replace(/%$/, "")));
    const missing = [];
    for (const v of numericTokens(summary)) if (!src.has(v) && !src.has(v.replace(/%$/, "")) && !missing.includes(v)) missing.push(v);
    return missing;
  }
  function chunkText(text, maxWords = 900, overlapWords = 60) { // caf.core.textutil.chunk_text
    const paras = String(text || "").split(/\n\s*\n|(?=^#{1,6}\s)/m).map((p) => (p || "").trim()).filter(Boolean);
    const chunks = []; let cur = [], count = 0;
    const tail = () => (overlapWords > 0 ? cur.join(" ").split(/\s+/).filter(Boolean).slice(-overlapWords).join(" ") : "");
    for (const p of paras) {
      const n = wc(p);
      if (n > maxWords) {
        for (const s of T().sentences(p)) {
          const sn = wc(s);
          if (count + sn > maxWords && cur.length) { chunks.push(cur.join("\n\n")); const t = tail(); cur = t ? [t] : []; count = wc(t); }
          cur.push(s); count += sn;
        }
        continue;
      }
      if (count + n > maxWords && cur.length) { chunks.push(cur.join("\n\n")); const t = tail(); cur = t ? [t] : []; count = wc(t); }
      cur.push(p); count += n;
    }
    if (cur.length) chunks.push(cur.join("\n\n"));
    return chunks.length ? chunks : text ? [text] : [];
  }
  const looksLikeTranscript = (t) => (String(t || "").match(/^[ \t]*\[\d{1,2}:\d{2}/gm) || []).length >= 3;
  const transcriptParagraphs = (t) => String(t || "").replace(/\n(?=[ \t]*\[\d{1,2}:\d{2})/g, "\n\n");
  const chunkBody = (chunk, index, ov) => { if (index === 0 || ov <= 0) return chunk; const i = chunk.indexOf("\n\n"); const rest = i >= 0 ? chunk.slice(i + 2) : ""; return i >= 0 && rest.trim() ? rest : chunk; };
  function chunkSections(chunks) {
    const out = []; let current = null;
    for (const c of chunks) {
      const heads = [...c.matchAll(/^#{1,6}\s+(.+)$/gm)].map((m) => m[1].trim());
      const first = c.trim() ? c.replace(/^\s+/, "").split(/\r?\n/)[0] : "";
      out.push(heads.length && first.startsWith("#") ? heads[0] : current);
      if (heads.length) current = heads[heads.length - 1];
    }
    return out;
  }
  function notesToText(notes, label) {
    const L = [`[${label}]`];
    if ((notes.key_points || []).length) L.push("Key points:", ...notes.key_points.map((p) => `- ${p}`));
    if ((notes.facts || []).length) L.push("Facts:", ...notes.facts.map((f) => `- ${f}`));
    if ((notes.action_items || []).length) {
      L.push("Actions:");
      for (const a of notes.action_items) { const extra = [a.owner ? `owner: ${a.owner}` : "", a.due ? `due: ${a.due}` : ""].filter(Boolean).join("; "); L.push(`- ${a.action || ""}${extra ? ` (${extra})` : ""}`); }
    }
    if ((notes.entities || []).length) L.push("Entities: " + notes.entities.join(", "));
    return L.join("\n");
  }
  function groupByBudget(items, budget) {
    const groups = []; let cur = [], count = 0;
    items.forEach((t, i) => { const n = wc(t); if (cur.length && count + n > budget) { groups.push(cur); cur = []; count = 0; } cur.push(i); count += n; });
    if (cur.length) groups.push(cur);
    return groups;
  }
  const extractiveNotes = (part) => ({ key_points: (part.candidates || []).slice(0, 4).map((c) => c.text),
    facts: (part.candidates || []).filter((c) => NUM_1.test(c.text)).slice(0, 4).map((c) => c.text),
    action_items: (part.actions || []).slice(0, 3).map((a) => ({ action: a.action, owner: a.owner, due: a.due })), entities: [] });
  function extractiveDraft(parts, { mode, title, maxWords, focus }) {
    const cands = parts.flatMap((p) => p.candidates || []);
    if (!cands.length) return { title, summary: "", key_points: [], action_items: [], abstract: "", meta_description: "" };
    const texts = cands.map((c) => c.text);
    const cent = centrality(texts);
    const scores = cands.map((c, i) => 0.5 * c.score + 0.5 * sentenceScore(c.text, cent[i], { lead: !!c.lead, firstPart: c.pos[0] === 0, title, focus }));
    const ranking = mmrSelect(texts, scores, Math.min(texts.length, 60));
    const byPos = (a, b) => cands[a].pos[0] - cands[b].pos[0] || cands[a].pos[1] - cands[b].pos[1];
    const take = (pool, budget, maxItems) => { const chosen = []; let used = 0;
      for (const i of pool) { const n = wc(texts[i]); if (chosen.length && used + n > budget) continue; chosen.push(i); used += n; if (chosen.length >= maxItems || used >= budget * 0.9) break; }
      return chosen; };
    const absBudget = Math.min(150, Math.max(40, maxWords));
    const absIdx = take(ranking, absBudget, 5).sort(byPos);
    const abstract = absIdx.map((i) => texts[i]).join(" ");
    const bodyBudget = ["executive", "technical"].includes(mode) ? Math.max(40, Math.floor(maxWords * 0.5)) : absBudget;
    const bodyIdx = take(ranking, bodyBudget, 4).sort(byPos);
    const body = bodyIdx.map((i) => texts[i]).join(" ");
    const k = MODE_KP[mode] || 5;
    const inBody = new Set(["executive", "technical"].includes(mode) ? bodyIdx : []);
    const pool = ranking.filter((i) => !inBody.has(i));
    const keyIdx = pool.length >= Math.min(3, k) ? pool.slice(0, k) : ranking.slice(0, k);
    const keyPoints = keyIdx.map((i) => texts[i]);
    const used = new Set([...keyIdx, ...inBody].map((i) => T().canonical(texts[i])));
    const seen = new Set(); const actions = [];
    const allActions = parts.flatMap((p) => p.actions || []).map((a, i) => ({ a, i })).sort((x, y) => (y.a.strength || 1) - (x.a.strength || 1) || x.i - y.i).map((x) => x.a);
    for (const a of allActions) {
      const key = T().canonical(a.action);
      if (seen.has(key) || (used.has(key) && mode !== "action_items")) continue;
      seen.add(key); actions.push({ action: a.action, owner: a.owner, due: a.due });
      if (actions.length >= 8) break;
    }
    const leads = cands.map((c, i) => i).filter((i) => cands[i].pos[0] === 0 && cands[i].lead).sort(byPos);
    const metaSrc = leads.map((i) => texts[i]).find((t) => t.length >= 70 && t.length <= META_MAX)
      || ranking.map((i) => texts[i]).find((t) => t.length >= 70 && t.length <= META_MAX) || (ranking.length ? texts[ranking[0]] : title);
    return { title, summary: body, key_points: keyPoints, action_items: actions, abstract, meta_description: fitMeta(metaSrc) };
  }
  const actionLine = (a, checkbox = false) => { const meta = [a.owner ? `owner: ${a.owner}` : "", a.due ? `due: ${a.due}` : ""].filter(Boolean);
    return (checkbox ? "- [ ] " : "- ") + String(a.action).trim() + (meta.length ? ` _(${meta.join("; ")})_` : ""); };
  function composeMarkdown(mode, d) {
    const title = d.title || "Summary"; const kp = (d.key_points || []).filter(Boolean); const actions = d.action_items || []; const body = String(d.summary || "").trim();
    if (mode === "meta_description") return d.meta_description || "";
    if (mode === "abstract") return `# ${title}\n\n${String(d.abstract || body).trim()}\n`;
    if (mode === "bullet") return `# ${title}\n\n` + (kp.length ? kp : T().sentences(body).slice(0, 7)).map((b) => `- ${b}`).join("\n") + "\n";
    if (mode === "action_items") {
      const L = [`# ${title} - action items`, ""];
      L.push(...(actions.length ? actions.map((a) => actionLine(a, true)) : ["_No explicit action items found in the source._"]));
      if (kp.length) L.push("", "## Context", "", ...kp.map((p) => `- ${p}`));
      return L.join("\n") + "\n";
    }
    const L = [`# ${title}`, "", body];
    if (kp.length) L.push("", `## ${mode === "technical" ? "Key details" : "Key points"}`, "", ...kp.map((p) => `- ${p}`));
    if (actions.length) L.push("", mode === "executive" ? "## Recommended actions" : "## Actions and next steps", "", ...actions.map((a) => actionLine(a)));
    return L.join("\n").trim() + "\n";
  }

  /* ------------------------------------------------------------- the agent */
  CAF.agent("content_summary", {
    async run(input, ctx) {
      const mode = MODES.includes(input.mode) ? input.mode : "executive";
      const maxWords = input.max_words || MODE_WORDS[mode];
      const size = input.chunk_words || 900;
      const ov = Math.min(60, Math.floor(size / 10));

      // -- ingest
      let loaded = await loadDocument(ctx, input);
      if (!loaded.doc && !loaded.unavailable && !loaded.error) {
        const up = ctx.upstreamText(400000);
        if (!up) throw new Error("Nothing to summarise: provide source (URL or path), path, text or html.");
        loaded = { doc: { source: "upstream-output", title: guessTitle(up), text: up, mime: "text/plain" } };
        ctx.warn("No source given - summarised the upstream agents' output.");
      }
      if (loaded.error) throw new Error(`Could not ingest the source: ${loaded.error}`);
      if (loaded.unavailable) {
        ctx.degraded(loaded.unavailable);
        ctx.check({ rule: "ingest.source", status: "fail", severity: "high", message: `Nothing summarised: ${loaded.unavailable}.` });
        const out = { title: "Source not available", mode, summary_markdown: "", key_points: [], action_items: [], abstract: "", meta_description: "",
          chunks_processed: 0, source_words: 0, summary_words: 0, compression_ratio: 0, method: "extractive", source: String(input.source || input.path || ""), collapse_levels: 0 };
        return { output: out, summary: "No summary: the source cannot be read in the hosted console", view: [{ type: "markdown", title: "Source not available", markdown: `The hosted console ${loaded.unavailable}.` }] };
      }
      const doc = loaded.doc;
      const text = doc.text || "";
      const sourceWords = wc(text);
      if (!sourceWords) throw new Error(`The source '${doc.source}' contains no extractable text.`);
      const title = normWs(input.title || "") || cleanTitle(doc.title, doc.source, text);
      ctx.check({ rule: "ingest.parsed", status: "info", severity: "low", target: String(doc.source),
        message: `Parsed ${doc.mime} '${title}' locally: ${sourceWords.toLocaleString("en-US")} words${doc.pages ? `, ${doc.pages} pages` : ""}.` });
      if (sourceWords < 60) ctx.check({ rule: "ingest.length", status: "warn", severity: "low", message: `Source is only ${sourceWords} words; a summary adds little.` });

      // -- chunk
      const chunks = chunkText(looksLikeTranscript(text) ? transcriptParagraphs(text) : text, size, ov);
      const sections = chunkSections(chunks.map((c, i) => chunkBody(c, i, ov)));
      let useModel = ctx.llmLive;
      if (useModel && input.confidential) {
        useModel = false;
        ctx.warn("Confidential document: Claude is not an on-premise model, so no text was sent to it; a local extractive summary was produced (run the Python service with the ollama provider for local abstractive summaries).");
        ctx.degraded("confidential: extractive summary (no on-premise model configured)");
      }
      if (!ctx.llmLive) ctx.degraded("offline model: extractive summary (top-ranked verbatim sentences), not an abstractive summary");
      ctx.check({ rule: "chunking.map_reduce", status: "info", severity: "low", message: `${chunks.length} semantic chunk(s) of <= ${size} words, summarised in parallel (map-reduce).`, data: { chunk_words: size, chunks: chunks.length } });

      // -- map (parallel)
      ctx.progress(`Map: ${chunks.length} chunk(s)${useModel ? " on Claude" : " (extractive)"}`);
      const parts = await Promise.all(chunks.map(async (c, i) => {
        const body = chunkBody(c, i, ov);
        const part = { index: i, section: sections[i], words: wc(body), notes: null, candidates: rankCandidates(body, i, { title, focus: input.focus }), actions: detectActionItems(body) };
        if (useModel) {
          try { part.notes = await ctx.generate("map", { chunk: c, index: i + 1, total: chunks.length, title, section: sections[i], focus: input.focus }); }
          catch (e) { ctx.warn(`Part ${i + 1}: model call failed (${e.message}); extractive notes used for it.`); }
        }
        return part;
      }));

      // -- reduce
      let method = useModel && parts.some((p) => p.notes) ? "abstractive" : "extractive";
      let draft = null, levels = 0;
      if (method === "abstractive") {
        let blocks = parts.map((p) => { const n = p.index + 1; const label = `Part ${n}` + (p.section ? ` - ${p.section}` : ""); return { first: n, last: n, text: notesToText(p.notes || extractiveNotes(p), label) }; });
        const budget = input.reduce_budget_words || 3500;
        try {
          while (blocks.length > 1 && wc(blocks.map((b) => b.text).join("\n\n")) > budget && levels < MAX_COLLAPSE) {
            let groups = groupByBudget(blocks.map((b) => b.text), budget);
            if (groups.length === blocks.length) { groups = []; for (let i = 0; i < blocks.length; i += 2) groups.push(i + 1 < blocks.length ? [i, i + 1] : [i]); }
            ctx.progress(`Collapse level ${levels + 1}: ${groups.length} group(s)`);
            const merged = [];
            for (const g of groups) {
              if (g.length === 1) { merged.push(blocks[g[0]]); continue; }
              const first = blocks[g[0]].first, last = blocks[g[g.length - 1]].last, label = `Parts ${first}-${last}`;
              const notes = await ctx.generate("collapse", { notes: g.map((i) => blocks[i].text).join("\n\n"), title, label, total: parts.length });
              merged.push({ first, last, text: notesToText(notes, label) });
            }
            blocks = merged; levels++;
          }
          ctx.progress(`Reduce: ${mode} summary`);
          draft = await ctx.generate(mode, { notes: blocks.map((b) => b.text).join("\n\n"), title, chunks: parts.length, source_words: sourceWords, max_words: maxWords,
            audience: input.audience, focus: input.focus, key_points: MODE_KP[mode], meta_max: META_MAX });
        } catch (e) {
          ctx.warn(`Model reduce failed (${e.message}); fell back to the extractive summary.`);
          ctx.degraded("model reduce failed: extractive summary");
          method = "extractive"; draft = null;
        }
      }
      if (!draft) draft = extractiveDraft(parts, { mode, title, maxWords, focus: input.focus });

      // -- format (deterministic)
      const d = { ...draft };
      d.title = normWs(d.title || title) || title;
      d.key_points = (d.key_points || []).map(normWs).filter(Boolean).slice(0, 10);
      d.action_items = (d.action_items || []).filter((a) => a && a.action).slice(0, 12).map((a) => ({ action: String(a.action), owner: a.owner || null, due: a.due || null }));
      const rawMeta = normWs(d.meta_description || "") || (d.key_points[0] || d.title);
      const meta = fitMeta(rawMeta);
      if (normWs(rawMeta).length > META_MAX) ctx.check({ rule: "meta_description.length", status: "warn", severity: "medium", ref: "SERP snippet limit", message: `Meta description was ${normWs(rawMeta).length} chars; trimmed to ${meta.length} (limit ${META_MAX}).` });
      else ctx.check({ rule: "meta_description.length", status: "pass", severity: "low", ref: "SERP snippet limit", message: `Meta description is ${meta.length} chars (limit ${META_MAX}).` });
      if (meta.length < META_MIN) ctx.check({ rule: "meta_description.min_length", status: "warn", severity: "low", message: `Meta description is only ${meta.length} chars; aim for ${META_MIN}-${META_MAX}.` });
      d.meta_description = meta;
      if (!normWs(d.abstract || "")) d.abstract = T().sentences(d.summary || "").slice(0, 4).join(" ");
      const md = composeMarkdown(mode, d);
      const summaryWords = wc(mode === "meta_description" ? meta : md);
      const ratio = round(summaryWords / Math.max(1, sourceWords), 4);
      if (method === "abstractive") {
        const missing = ungroundedNumbers([md, d.abstract, meta].join(" "), text);
        ctx.check({ rule: "summary.numbers_grounded", status: missing.length ? "warn" : "pass", severity: missing.length ? "high" : "low",
          message: missing.length ? `Numbers not found in the source - verify before use: ${missing.join(", ")}` : "Every number in the summary appears in the source.", data: { ungrounded: missing } });
      } else ctx.check({ rule: "summary.extractive", status: "info", severity: "low", message: "Extractive summary: every sentence is verbatim from the source." });
      if (!["meta_description", "bullet", "action_items"].includes(mode) && summaryWords > maxWords * 1.35 + 40)
        ctx.check({ rule: "summary.length", status: "warn", severity: "low", message: `Summary is ${summaryWords} words; target was about ${maxWords}.` });
      if (["executive", "technical", "bullet", "abstract"].includes(mode))
        ctx.check({ rule: "summary.key_points", status: d.key_points.length >= 3 ? "pass" : "warn", severity: "low", message: `${d.key_points.length} key point(s).` });
      const banned = (ctx.brand.banned_phrases || []).filter((p) => md.toLowerCase().includes(String(p).toLowerCase()));
      if (banned.length && method === "abstractive") ctx.check({ rule: "brand.banned_phrases", status: "fail", severity: "high", message: `Banned phrases used: ${banned.join(", ")}` });

      const output = { title: d.title, mode, summary_markdown: md, key_points: d.key_points, action_items: d.action_items, abstract: normWs(d.abstract),
        meta_description: meta, chunks_processed: parts.length, source_words: sourceWords, summary_words: summaryWords, compression_ratio: ratio,
        method, source: String(doc.source), collapse_levels: levels };
      ctx.artifact({ name: "summary.md", mime: "text/markdown", content: md, description: `${mode} summary` });
      ctx.artifact({ name: "summary.json", mime: "application/json", content: JSON.stringify(output, null, 2), description: "Summary (structured)" });
      const shortTitle = d.title.length <= 70 ? d.title : d.title.slice(0, 67).replace(/\s+\S*$/, "") + "...";
      const label = mode.replace(/_/g, " ");
      return {
        output,
        summary: `${label.charAt(0).toUpperCase() + label.slice(1)} summary of '${shortTitle}': ${sourceWords.toLocaleString("en-US")} words -> ${summaryWords} (${pyFixed(ratio * 100, 1)}%) from ${parts.length} chunk(s), ${method}`,
        view: [
          { type: "markdown", title: `${label.charAt(0).toUpperCase() + label.slice(1)} summary`, markdown: md },
          { type: "kv", title: "Map-reduce", data: { method, source_words: sourceWords, chunks: parts.length, collapse_levels: levels, summary_words: summaryWords,
            compression: `${pyFixed(ratio * 100, 1)}%`, meta_description: meta } },
        ],
      };
    },
  });
})();
