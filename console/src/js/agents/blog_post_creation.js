/* Blog Post Creation - browser twin of caf/agents/content/blog_post_creation.py.
   research -> outline -> draft -> critique -> (revise -> critique)* -> finalize, with the same
   deterministic guardrails computed in code (word count, heading structure, keyword placement,
   Flesch reading ease, brand banned phrases, uncited claims), [S#] citation rendering and a CMS
   publish action that is mandatory (always waits for a human, even under auto-approve). The
   critique -> revise loop never runs with the offline model. Research reads the embedded search
   fixture and demo-site reference pages; other reference URLs degrade with a reason. */
(() => {
  const T = CAF.lib.text;
  const cmp = (a, b) => (a < b ? -1 : a > b ? 1 : 0);
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
  const pyStr = (s) => { s = String(s); const q = s.includes("'") && !s.includes('"') ? '"' : "'";
    return q + s.replace(/\\/g, "\\\\").replace(new RegExp(q, "g"), "\\" + q).replace(/\n/g, "\\n").replace(/\r/g, "\\r").replace(/\t/g, "\\t") + q; };
  const pyList = (xs) => `[${xs.map(pyStr).join(", ")}]`;
  const normWs = (s) => String(s ?? "").replace(/\s+/g, " ").trim();
  const len = (s) => [...String(s ?? "")].length;
  const cpSlice = (s, n) => [...String(s ?? "")].slice(0, n).join("");
  function truncate(text, n, ell = "...") {
    const cps = [...String(text ?? "")];
    if (cps.length <= n) return String(text ?? "");
    const cut = cps.slice(0, Math.max(0, n - ell.length)).join(""), i = cut.lastIndexOf(" ");
    return (i >= 0 ? cut.slice(0, i) : cut) + ell;
  }
  /** textutil.slugify: NFKD, drop non-ASCII, non-alphanumerics -> "-" */
  const slugify = (t, max = 60) => (String(t || "").normalize("NFKD").replace(/[^\x00-\x7f]/g, "").replace(/[^A-Za-z0-9]+/g, "-").replace(/^-+|-+$/g, "").toLowerCase()
    .slice(0, max).replace(/^-+|-+$/g, "")) || "item";
  /** str.splitlines() */
  const splitlines = (t) => { const parts = String(t ?? "").split(/\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]/); if (parts.length && parts[parts.length - 1] === "") parts.pop(); return parts; };
  const host = (url) => { const m = String(url || "").match(/^(?:[A-Za-z][A-Za-z0-9+.-]*:)?\/\/([^/?#]*)/); return m ? m[1].toLowerCase().replace(/^www\./, "") : ""; };
  const STOP = T.STOP;

  // ---------------------------------------------------------------- deterministic guardrails
  const CITE = /\[(S\d+)\]/g;
  const LINK = /\[[^\]]+\]\([^)]+\)/;
  const CLAIM_PATTERNS = [/\d+(?:\.\d+)?\s?%/i, /\b\d+(?:\.\d+)?\s?(?:percent|per cent)\b/i, /[$£€]\s?\d/i, /\b\d[\d,.]*\s?(?:million|billion|thousand)\b/i,
    /\b(?:studies|study|research|data|surveys?|statistics|experts?|analysis)\s+(?:show|shows|showed|suggest|suggests|find|finds|found|say|says|reveal|reveals)\b/i,
    /\baccording to\b/i, /\b\d+(?:\.\d+)?\s?(?:times|x)\s+(?:more|less|higher|lower|likely|faster)\b/i,
    /\b(?:one|two|three|four|five|\d+) in (?:every )?(?:\d+|two|three|four|five|ten)\b/i, /\b(?:on average|the average)\b.*\d/i];
  function countSyllables(word) {
    let w = String(word).toLowerCase().replace(/[^a-z]/g, "");
    if (!w) return 0;
    if (w.length <= 3) return 1;
    w = w.replace(/(?:[^laeiouy]es|(?<![td])ed|[^laeiouy]e)$/, "").replace(/^y/, "");
    return Math.max(1, (w.match(/[aeiouy]{1,2}/g) || []).length);
  }
  /** Markdown -> prose for readability (citations, links, emphasis and list markers removed). */
  function plainText(md, includeHeadings = false) {
    const out = [];
    for (const line of splitlines(md)) {
      let s = line.trim();
      if (!s) { out.push(""); continue; }
      if (s.startsWith("#")) { if (includeHeadings) out.push(s.replace(/^#+/, "").trim() + "."); continue; }
      s = s.replace(/^\s*(?:[-*+]|\d+[.)])\s+/, "").replace(CITE, "").replace(/\[([^\]]+)\]\([^)]+\)/g, "$1").replace(/[*_`>]/g, "");
      if (["- ", "* ", "+ "].includes(line.trimStart().slice(0, 2)) || /^\s*\d+[.)]\s/.test(line)) s = /[.!?:]$/.test(s) ? s : s + ".";
      out.push(s);
    }
    return out.join("\n");
  }
  const splitSentences = (text) => String(text || "").split(/(?<=[.!?])\s+|\n+/).map((p) => p.trim()).filter((p) => p && /[A-Za-z0-9]/.test(p));
  function readability(md) {
    const sents = splitSentences(plainText(md)), ws = sents.flatMap((s) => T.words(s));
    const nW = ws.length, nS = Math.max(1, sents.length);
    if (!nW) return { flesch_reading_ease: 0, flesch_kincaid_grade: 0, words: 0, sentences: 0, avg_sentence_words: 0, long_sentences: 0 };
    const syl = ws.reduce((a, w) => a + countSyllables(w), 0), wps = nW / nS, spw = syl / nW;
    return { flesch_reading_ease: round(206.835 - 1.015 * wps - 84.6 * spw, 1), flesch_kincaid_grade: round(0.39 * wps + 11.8 * spw - 15.59, 1), words: nW,
      sentences: sents.length, avg_sentence_words: round(wps, 1), long_sentences: sents.filter((s) => T.words(s).length > 28).length };
  }
  const headings = (md) => [...String(md || "").matchAll(/^(#{1,6})\s+(.+?)\s*#*\s*$/gm)].map((m) => [m[1].length, m[2].trim()]);
  function toks(text) {
    return (String(text || "").toLowerCase().match(/[a-z0-9]+/g) || []).map((w) => (w.length > 4 && w.endsWith("ies") ? w.slice(0, -3) + "y"
      : w.length > 3 && w.endsWith("s") && !/(?:ss|us|is)$/.test(w) ? w.slice(0, -1) : w));
  }
  function containsPhrase(text, phrase) {
    const t = toks(text), p = toks(phrase);
    if (!p.length) return false;
    for (let i = 0; i + p.length <= t.length; i++) if (p.every((x, j) => t[i + j] === x)) return true;
    return false;
  }
  const firstWords = (md, n = 100) => T.words(plainText(md)).slice(0, n).join(" ");
  function unsourcedClaims(md) {
    const out = [];
    for (const line of splitlines(md)) {
      if (line.trim().startsWith("#")) continue;
      for (const s of line.split(/(?<=[.!?])\s+/))
        if (CLAIM_PATTERNS.some((p) => p.test(s)) && !/\[(S\d+)\]/.test(s) && !LINK.test(s)) out.push(truncate(normWs(s.replace(/[*_`]/g, "")), 160));
    }
    return out;
  }
  const stripH1 = (body) => (/^\s*#\s/.test(body || "") ? String(body).replace(/^\s*#\s+[^\n]*\n?/, "").trim() : String(body || "").trim());
  function guardrails({ title, body, meta, primary, secondary, targetWords, minReadingEase, banned, sourceIds }) {
    const checks = [], stats = readability(body), wc = stats.words, dev = Math.abs(wc - targetWords) / Math.max(1, targetWords);
    checks.push({ rule: "length.word_count", status: dev <= 0.2 ? "pass" : dev <= 0.4 ? "warn" : "fail", severity: "medium", message: `${wc} words (target ${targetWords} +/-20%).`, data: { words: wc } });
    const hs = headings(body), h1 = hs.filter(([l]) => l === 1).map(([, h]) => h), h2 = hs.filter(([l]) => l === 2).map(([, h]) => h);
    checks.push({ rule: "structure.h1", status: !h1.length && title.trim() ? "pass" : "fail", severity: "high",
      message: !h1.length ? "Exactly one H1 (the title)." : `Body contains extra H1 headings: ${pyList(h1.slice(0, 2))}` });
    checks.push({ rule: "structure.h2", status: h2.length >= 3 ? "pass" : h2.length ? "warn" : "fail", severity: "medium", message: `${h2.length} H2 sections (minimum 3).` });
    const prev = [[1, title], ...hs], skips = hs.filter(([l1], i) => l1 > prev[i][0] + 1).map(([, h]) => h);
    if (skips.length) checks.push({ rule: "structure.heading_levels", status: "warn", severity: "low", message: `Skipped heading levels at: ${pyList(skips.slice(0, 3))}` });
    const longH = hs.filter(([, h]) => len(h) > 70).map(([, h]) => h);
    if (longH.length) checks.push({ rule: "structure.heading_length", status: "warn", severity: "low", message: `Headings over 70 characters: ${pyList(longH.slice(0, 2))}` });
    const inTitle = containsPhrase(title, primary);
    checks.push({ rule: "seo.keyword_title_h1", status: inTitle ? "pass" : "fail", severity: "high", message: `Primary keyword '${primary}' ${inTitle ? "is" : "is not"} in the title / H1.` });
    const introOk = containsPhrase(firstWords(body, 100), primary);
    checks.push({ rule: "seo.keyword_intro", status: introOk ? "pass" : "fail", severity: "medium", message: `Primary keyword ${introOk ? "appears" : "does not appear"} in the first 100 words.` });
    const ml = len(meta), metaKw = containsPhrase(meta, primary);
    checks.push({ rule: "seo.meta_description", status: ml >= 120 && ml <= 160 && metaKw ? "pass" : "warn", severity: "low",
      message: `Meta description ${ml} chars (120-160)` + (metaKw ? "" : "; missing the primary keyword") + "." });
    if (secondary.length) {
      const used = secondary.filter((k) => containsPhrase(body, k));
      checks.push({ rule: "seo.secondary_keywords", status: "info", severity: "low", message: `${used.length}/${secondary.length} secondary keywords used.`, data: { used } });
    }
    const fre = stats.flesch_reading_ease;
    checks.push({ rule: "readability.flesch", status: fre >= minReadingEase ? "pass" : "warn", severity: "medium",
      message: `Flesch reading ease ${pyFloat(fre)} (minimum ${pyFloat(minReadingEase)}); grade ${pyFloat(stats.flesch_kincaid_grade)}.` });
    const low = `${title} ${meta} ${body}`.toLowerCase(), hits = (banned || []).filter((p) => low.includes(String(p).toLowerCase()));
    checks.push({ rule: "brand.banned_phrases", status: hits.length ? "fail" : "pass", severity: "high", message: hits.length ? `Brand-banned phrases: ${pyList(hits)}` : "No brand-banned phrases." });
    const claims = unsourcedClaims(body);
    checks.push({ rule: "facts.claims_need_source", status: claims.length ? "warn" : "pass", severity: "high", data: { sentences: claims.slice(0, 6) },
      message: claims.length ? `${claims.length} claim(s) with statistics or research language but no citation.` : "Every statistic / research claim is cited." });
    const unknown = [...new Set([...String(body).matchAll(CITE)].map((m) => m[1]))].filter((s) => !sourceIds.includes(s)).sort(cmp);
    if (unknown.length) checks.push({ rule: "facts.unknown_sources", status: "fail", severity: "high", message: `Citations to research notes that do not exist: ${pyList(unknown)}` });
    const penalty = checks.reduce((a, c) => a + (c.status === "fail" ? 12 : c.status === "warn" ? 4 : 0), 0);
    return [checks, Math.max(0, 100 - penalty), stats];
  }
  /** [S3] markers -> numbered links in order of first citation, plus a Sources section. */
  function renderCitations(body, notes) {
    const byId = Object.fromEntries(notes.map((n) => [n.id, n])), order = [];
    for (const m of String(body).matchAll(CITE)) if (byId[m[1]] && !order.includes(m[1])) order.push(m[1]);
    const num = Object.fromEntries(order.map((sid, i) => [sid, i + 1]));
    let rendered = String(body).replace(/\s?\[(S\d+)\]/g, (m, sid) => (sid in num ? ` [[${num[sid]}]](${byId[sid].url})` : ""));
    const sources = notes.map((n) => ({ ...n, number: num[n.id] ?? null, cited: n.id in num }));
    if (order.length) rendered = rendered.trimEnd() + "\n\n## Sources\n\n" + order.map((sid) => `${num[sid]}. [${byId[sid].title}](${byId[sid].url})` + (byId[sid].publisher ? ` - ${byId[sid].publisher}` : "")).join("\n");
    return [rendered, sources];
  }
  function fixMeta(meta, body) {
    meta = normWs(String(meta || "").replace(/[*_`#]/g, ""));
    if (len(meta) < 50) meta = normWs(splitSentences(plainText(body)).slice(0, 2).join(" "));
    return len(meta) > 160 ? truncate(meta, 158, "...") : meta;
  }
  function defaultKeyword(topic) {
    const content = (String(topic).toLowerCase().match(/[a-z0-9']+/g) || []).filter((w) => !STOP.has(w) && w !== "your" && w !== "my");
    return content.slice(0, 4).join(" ") || cpSlice(String(topic).toLowerCase(), 60);
  }
  function articleHtml(title, md, meta) {
    const body = CAF.lib.md.html(String(md).replace(/^#\s+[^\n]*\n/, ""));
    return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${U.esc(title)}</title>`
      + `<meta name="description" content="${U.esc(meta)}"><style>body{font-family:Georgia,serif;max-width:720px;margin:40px auto;padding:0 20px;line-height:1.7;color:#1d2530}`
      + `h1,h2,h3{font-family:system-ui,Arial,sans-serif}a{color:#0b5cad}</style></head><body><article><h1>${U.esc(title)}</h1>${body}</article></body></html>`;
  }
  const noteLines = (notes) => notes.map((n) => `[${n.id}] ${n.title} (${n.publisher || host(n.url)}${n.published ? ", " + n.published : ""}) ${n.url} - ${n.text ?? ""}`);
  /** Reference-page text: main content, block-separated (the Python crawler uses trafilatura). */
  function pageText(doc) {
    doc.querySelectorAll("script, style, noscript, template, nav, header, footer, aside, form, figure, iframe, svg, button").forEach((e) => e.remove());
    doc.querySelectorAll("[class], [id]").forEach((e) => { if (/byline|author|share|social|breadcrumb|related|comment|newsletter|cookie|callout|promo|skip/i.test(`${e.getAttribute("class") || ""} ${e.id || ""}`)) e.remove(); });
    const root = doc.querySelector("article") || doc.querySelector("main") || doc.body, out = [];
    const BLOCK = /^(P|DIV|H[1-6]|LI|UL|OL|TABLE|TR|TD|TH|BLOCKQUOTE|PRE|SECTION|ARTICLE|MAIN|DD|DT|DL|BR|FIGCAPTION)$/;
    const walk = (n) => { for (const c of n.childNodes) { if (c.nodeType === 3) out.push(c.nodeValue); else if (c.nodeType === 1) { const b = BLOCK.test(c.nodeName.toUpperCase()); if (b) out.push(" "); walk(c); if (b) out.push(" "); } } };
    if (root) walk(root);
    return normWs(out.join(""));
  }
  /** bs4 get_text(sep) - descendant text nodes joined by sep. */
  function getText(el, sep = "") {
    const out = [];
    const walk = (n) => { for (const c of n.childNodes) { if (c.nodeType === 3 || c.nodeType === 4) out.push(c.nodeValue); else if (c.nodeType === 1 && !/^(SCRIPT|STYLE|TEMPLATE)$/i.test(c.nodeName)) walk(c); } };
    if (el) walk(el);
    return out.join(sep);
  }
  /** crawler.fetch on the embedded demo site: status overrides, redirects -> final_url (not the file path). */
  function fetchDemo(ctx, url) {
    const p = ctx.site(url);
    if (!p) return null;
    let finalUrl = url;
    if (p.redirects && p.redirects.length) {
      let meta = {};
      try { meta = JSON.parse((ctx.site("/_fixture.json") || {}).html || "{}"); } catch { meta = {}; }
      let path = CAF.site.path(url);
      for (let i = 0; i < 8 && (meta.redirects || {})[path]; i++) { try { finalUrl = new URL(meta.redirects[path][0], finalUrl).toString(); } catch { break; } path = CAF.site.path(finalUrl); }
    }
    const html = String(p.html || "");
    return { status: p.status, html, final_url: finalUrl, ok: p.status >= 200 && p.status < 300, is_html: /\.html?$/i.test(p.path || "") || /^(<!doctype html|<html)/i.test(html.trimStart()) };
  }
  function fixtureSearch(items, query, n = 10) {
    const q = [...new Set(String(query).toLowerCase().replace(/-/g, " ").split(/\s+/).filter((t) => t.length > 2))];
    const scored = [];
    for (const it of items || []) {
      const terms = new Set((it.query_terms || []).map((t) => String(t).toLowerCase())), hay = `${it.title ?? ""} ${it.snippet ?? ""}`.toLowerCase();
      const score = 3 * q.filter((t) => terms.has(t)).length + q.filter((t) => hay.includes(t)).length;
      if (score) scored.push([score, it.published || "", it]);
    }
    scored.sort((a, b) => b[0] - a[0] || cmp(b[1], a[1]));
    return scored.slice(0, n).map(([, , it]) => it);
  }

  const lib = { countSyllables, plainText, splitSentences, readability, headings, containsPhrase, firstWords, unsourcedClaims, stripH1, guardrails, renderCitations,
    fixMeta, defaultKeyword, slugify, truncate };

  CAF.agent("blog_post_creation", {
    lib,
    async run(input, ctx) {
      // -- research
      const notes = [], context = [];
      let primary = String(input.primary_keyword || "").trim(), secondary = (input.secondary_keywords || []).filter((s) => String(s).trim());
      const kr = ctx.upstreamOutput("keyword_research");
      if (kr && (kr.clusters || []).length) {
        const ref = new Set(toks(primary || input.topic));
        const key = (c) => [[...new Set(toks(c.pillar_keyword || ""))].filter((t) => ref.has(t)).length, c.opportunity_score || 0];
        const cluster = kr.clusters.reduce((best, c) => { const a = key(c), b = key(best); return a[0] > b[0] || (a[0] === b[0] && a[1] > b[1]) ? c : best; }, kr.clusters[0]);
        if (!primary) primary = cluster.pillar_keyword || "";
        if (!secondary.length) secondary = (cluster.keywords || []).filter((k) => k.keyword !== primary).map((k) => k.keyword).slice(0, 5);
        context.push(`Keyword cluster '${cluster.name ?? "None"}': ${(cluster.keywords || []).slice(0, 8).map((k) => k.keyword).join(", ")}`);
      }
      primary = primary || defaultKeyword(input.topic);
      const brief = ctx.upstreamOutput("campaign_brief_generation") || {};
      const keyMessages = (brief.key_messages || []).slice(0, 5);
      const audience = input.audience || (brief.target_audience || {}).primary || null;
      const summary = ctx.upstreamOutput("content_summary") || {};
      const summaryText = ["summary_markdown", "summary", "executive_summary"].map((k) => summary[k]).find((v) => typeof v === "string") || "";
      if (summaryText) notes.push({ title: summary.title || "Upstream content summary", url: summary.source || "upstream:content_summary", publisher: "content_summary",
        published: null, origin: "upstream", text: truncate(summaryText, 900) });
      let offSite = false;
      for (const url of input.source_urls || []) {
        if (!ctx.isDemoUrl(url)) { offSite = true; ctx.warn(`Reference ${url} could not be fetched: the hosted console only reads the demo site.`); continue; }
        const pg = fetchDemo(ctx, url);
        if (pg && pg.ok && pg.is_html) {
          const doc = CAF.lib.html.parse(pg.html), t = doc.querySelector("h1") || doc.querySelector("title"), pub = doc.querySelector('meta[property="article:published_time"]');
          notes.push({ title: t ? normWs(getText(t, " ")) : url, url: pg.final_url, publisher: host(pg.final_url), published: pub ? pub.getAttribute("content") : null,
            origin: "reference", text: truncate(pageText(doc), 1500) });
        } else ctx.warn(`Reference ${url} returned status ${pg ? pg.status : 0}; skipped.`);
      }
      if (offSite) ctx.degraded("reference pages outside the demo site cannot be fetched in the hosted console (no network) - research used the embedded search fixture and demo-site pages");
      if (ctx.hasData("search/fixture_results")) {
        for (const r of fixtureSearch((await ctx.data("search/fixture_results")).results, `${input.topic} ${primary}`.trim(), 6))
          notes.push({ title: r.title, url: r.url, publisher: r.source ?? null, published: r.published ?? null, origin: "search", text: truncate(normWs(r.snippet || ""), 400) });
      } else ctx.warn("No search provider configured: research used references and upstream context only.");
      const uniq = new Map();
      for (const n of notes) { const k = n.url.split("#")[0].replace(/\/+$/, ""); if (!uniq.has(k)) uniq.set(k, n); }
      const research = [...uniq.values()].slice(0, 10);
      research.forEach((n, i) => { n.id = `S${i + 1}`; });
      if (!research.length) ctx.degraded("no research sources (search 'none' and no references) - facts rely on model knowledge and need verification");
      const nl = noteLines(research), known = new Set(research.map((n) => n.id));

      // -- outline
      ctx.progress("Outlining");
      const ol = await ctx.generate("outline", { topic: input.topic, brief: input.brief ?? null, primary_keyword: primary, secondary_keywords: secondary, audience,
        target_word_count: input.target_word_count, notes: nl, key_messages: keyMessages, context });
      const outline = (ol.sections || []).map((s) => ({ heading: cpSlice(normWs(String(s.heading || "").replace(/^(?:#+|H\d:?)\s*/, "")), 90),
        key_points: (s.key_points || []).filter((p) => String(p).trim()).map(normWs).slice(0, 6),
        source_ids: (s.source_ids || []).map((x) => String(x).trim().toUpperCase()).filter((i) => known.has(i)) }));
      const title = cpSlice(normWs(String(ol.title || "").replace(/^#+\s*/, "")), 120);
      let meta = ol.meta_description || "";

      // -- draft
      ctx.progress("Drafting");
      let body = stripH1(await ctx.text("draft", { topic: input.topic, title, brief: input.brief ?? null, primary_keyword: primary, secondary_keywords: secondary, audience,
        target_word_count: input.target_word_count, outline, notes: nl, key_messages: keyMessages }));

      // -- critique (reviewer template + deterministic guardrails) <-> revise
      const banned = ctx.brand.banned_phrases || [], sourceIds = research.map((n) => n.id);
      const gr = () => guardrails({ title, body, meta, primary, secondary, targetWords: Number(input.target_word_count), minReadingEase: Number(input.min_reading_ease),
        banned, sourceIds });
      const threshold = Number(input.score_threshold), scores = [];
      let revisions = 0, review = { score: 0, issues: [], strengths: [] }, modelScore = null;
      for (;;) {
        const [checks, gScore] = gr();
        const failing = checks.filter((c) => c.status === "fail" || c.status === "warn").map((c) => `${c.rule}: ${c.message}`);
        ctx.progress(revisions ? `Re-reviewing revision ${revisions}` : "Reviewing the draft");
        review = await ctx.generate("critique", { title, body, primary_keyword: primary, audience, notes: nl, guardrail_findings: failing, target_word_count: input.target_word_count });
        modelScore = ctx.llmLive ? Math.trunc(Number(review.score)) : null;
        const score = modelScore === null ? gScore : Math.min(modelScore, gScore);
        scores.push(score);
        if (!(ctx.llmLive && score < threshold && revisions < Number(input.max_revisions))) break;
        const issues = [...failing, ...(review.issues || []).map((i) => `${i.category} (${i.severity}): ${i.issue}` + (i.suggestion ? ` - fix: ${i.suggestion}` : ""))];
        ctx.progress(`Revising (round ${revisions + 1})`);
        body = stripH1(await ctx.text("revise", { title, body, issues, primary_keyword: primary, target_word_count: input.target_word_count, notes: nl })) || body;
        revisions++;
      }

      // -- finalize: citations, artifacts, mandatory CMS publish
      meta = fixMeta(meta, body);
      const [checks, gScore, stats] = gr();
      const score = modelScore === null ? gScore : Math.min(modelScore, gScore);
      const [rendered, sources] = renderCitations(body, research);
      const markdown = `# ${title}\n\n${rendered.trim()}\n`;
      const slug = slugify(len(primary) >= 8 ? primary : title, 60);
      const critique = { score, model_score: modelScore, guardrail_score: gScore, threshold, passed: score >= threshold, issues: review.issues || [], strengths: review.strengths || [] };
      checks.push({ rule: "review.score", status: critique.passed ? "pass" : "warn", severity: "medium",
        message: `Review score ${score}/100 (threshold ${threshold}) after ${revisions} revision(s)` + (ctx.llmLive ? "." : " - revision loop skipped with the offline model.") });
      checks.forEach((c) => ctx.check(c));
      const html = articleHtml(title, markdown, meta);
      ctx.artifact({ name: "blog.md", mime: "text/markdown", content: markdown, description: "Blog post (Markdown)" });
      ctx.artifact({ name: "blog.html", mime: "text/html", content: html, description: "Blog post preview (HTML)" });
      ctx.artifact({ name: "research_notes.json", mime: "application/json", content: JSON.stringify(research, null, 2), description: "Research notes with source URLs" });
      const status = input.publish_status || "draft";
      if (input.publish !== false)
        ctx.action({ id: "cms-1", kind: "cms.publish", connector: "cms", method: "publish", requires_approval: true, mandatory: true,
          payload: { title, body: markdown, slug, content_type: "article", status, body_format: "markdown",
            fields: { meta_description: meta, author: input.author ?? null, primary_keyword: primary, secondary_keywords: secondary, word_count: stats.words,
              reading_ease: stats.flesch_reading_ease, review_score: score, sources: sources.filter((s) => s.cited).map((s) => s.url) } },
          summary: `Publish '${title}' to the CMS as ${status} (editorial approval required)` });
      const fails = checks.filter((c) => c.status === "fail").length, cited = sources.filter((s) => s.cited).length;
      const output = { title, slug, meta_description: meta, primary_keyword: primary, secondary_keywords: secondary, outline, markdown, word_count: stats.words,
        readability: stats, sources: sources.map(({ text, ...s }) => s), critique, revisions_applied: revisions, score_history: scores, publish_status: status,
        author: input.author ?? null };
      const summaryLine = `Blog post '${title}': ${stats.words} words, reading ease ${pyFloat(stats.flesch_reading_ease)}, review ${score}/100, ${revisions} revision(s), ${cited} cited source(s)`
        + (fails ? `, ${fails} guardrail failure(s)` : "") + (input.publish !== false ? " - CMS publish awaiting approval" : "");
      return {
        output, summary: summaryLine,
        view: [
          { type: "kv", title: "Post", data: { title, slug, primary_keyword: primary, words: stats.words, reading_ease: stats.flesch_reading_ease, grade: stats.flesch_kincaid_grade,
            review_score: `${score}/100 (threshold ${threshold})`, revisions, cited_sources: cited } },
          { type: "html", title, artifact: "blog.html" },
          { type: "table", title: "Guardrails", columns: ["rule", "status", "message"], rows: checks.map((c) => ({ rule: c.rule, status: c.status, message: c.message })) },
          { type: "table", title: "Research notes", columns: ["id", "title", "publisher", "origin", "cited"],
            rows: sources.map((s) => ({ id: s.id, title: s.title, publisher: s.publisher || host(s.url), origin: s.origin, cited: s.cited ? `[${s.number}]` : "" })) },
        ],
      };
    },
  });
})();
