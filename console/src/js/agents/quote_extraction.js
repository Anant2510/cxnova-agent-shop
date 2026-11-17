/* Quote Extraction - browser twin of caf/agents/content/quote_extraction.py.
   The model proposes candidates; every candidate must ANCHOR by exact match after canonical normalisation
   (char-mapped, so offsets point into the source) or it is rejected with a reason. Speakers come from
   transcript prefixes, interview roles and attribution patterns before the model. A deterministic
   quotability ranker supplies verbatim quotes offline / when too few anchor. Quote bank: browser-local. */
(() => {
  const MAX_MODEL_CHARS = 60000, MIN_QUOTE_WORDS = 6;
  const SPEECH = "(?:said|says|explained|explains|added|adds|noted|notes|commented|comments|told|tells|wrote|writes|stated|states|argued|argues|observed)";
  const NAME = "(?:(?:Dr|Mr|Mrs|Ms|Prof)\\.[ \\t]+)?[A-Z][\\w'’-]+(?:[ \\t]+[A-Z][\\w'’-]+){0,3}";
  const NAME2 = "(?:(?:Dr|Mr|Mrs|Ms|Prof)\\.[ \\t]+)?[A-Z][\\w'’-]+(?:[ \\t]+[A-Z][\\w'’-]+){1,3}";
  const TS_SRC = "^[ \\t]*\\[(?<ts>\\d{1,2}:\\d{2}(?::\\d{2})?)\\][ \\t]*(?:(?<speaker>[^:\\n\\]]{1,80}?):[ \\t]+)?";
  const tsAll = () => new RegExp(TS_SRC, "gm");
  const tsAt = (text, pos) => { const r = new RegExp(TS_SRC, "my"); r.lastIndex = pos; return r.exec(text); };
  const NAME_LINE = /^[ \t]*(?<speaker>[A-Z][\w.'’-]+(?:[ \t]+[A-Z][\w.'’-]+){0,3}):[ \t]+/;
  const NOT_SPEAKERS = new Set(["q", "a", "note", "notes", "source", "title", "date", "summary", "question", "answer", "update", "re", "subject",
    "location", "duration", "agenda", "attendees", "transcript", "host", "moderator", "speaker", "speakers", "webinar"]);
  const AFTER = new RegExp(`^[\\s,.;:]*["”’']?[\\s,]*${SPEECH}\\s+(?<name>${NAME})`);
  const AFTER_INV = new RegExp(`^[\\s,.;:]*["”’']?[\\s,]*(?<name>${NAME2})\\s+${SPEECH}\\b`);
  const DASH_AFTER = new RegExp(`^["”’'.,]*[ \\t]*[-–—]{1,2}[ \\t]*(?<name>${NAME})`);
  const NEXT_LINE_NAME = new RegExp(`^["”’'.,]*[ \\t]*\\n(?:[ \\t]*\\n)?[ \\t]*[-–—]?[ \\t]*(?<name>${NAME2}),[ \\t]*[A-Z]`);
  const INTERVIEW_AFTER = new RegExp(`^["”’']?\\s*[:,]\\s*(?:an?\\s+)?interview with\\s+(?<name>${NAME2})`);
  const BEFORE = new RegExp(`(?<name>${NAME2})(?:,\\s*[^,\\n]{2,80},)?\\s+${SPEECH}\\s*(?:that)?[\\s:,]*["“‘']?\\s*$`);
  const ACCORDING = new RegExp(`according to (?<name>${NAME2})`);
  const QUOTED = /[“"](?<inner>[^”"\n]{20,700})[”"]/g;
  const QA_LINE_SRC = "^[ \\t]*(?<role>[QA])[ \\t]*[:.][ \\t]+";
  const MARKER = /^[ \t]*(?:(?:[-*+•]|\d+[.)])[ \t]+|>[ \t]*)/;
  const BOLD_LEAD = /\*\*[^*\n]{1,80}\*\*[ \t]+/y;
  const ABBR = new Set(["dr", "mr", "mrs", "ms", "prof", "st", "inc", "ltd", "co", "corp", "jr", "sr", "vs", "etc", "e.g", "i.e", "no",
    "fig", "u.s", "a.m", "p.m", "approx", "dept", "est", "jan", "feb", "aug", "sept", "oct", "nov", "dec"]);
  const STRONG = /\b(most|every|never|always|biggest|best|worst|first|only|critical|crucial|essential|key|matters?|transform\w*|changed?|proud|believe|convinced|clear|simple|real|truth|honest|important|difference|biggest|fastest|single|everything|nothing|must|whole|game|future|lesson|surprise\w*|wrong|right)\b/gi;
  const FIRST_PERSON = /\b(I|I'm|I’m|I've|I’ve|I'd|I’d|we|we're|we’re|we've|we’ve|our|my|us)\b/;
  const FILLER = /\b(um+|uh+|erm|you know|kind of|sort of|i mean)\b|\blike,/i;
  const PLEASANTRY = /\b(welcome|thanks? (?:you|for|so much|again|everyone)|thank you|good (?:morning|afternoon|evening)|hello|hi everyone|great question|any questions|let'?s get started|before we (?:start|begin)|can you (?:hear|see)|next slide|you'?re on mute)\b/i;
  const CONTEXT_START = /^(and|but|so|that|this|it|they|he|she|which|also|then|yeah|yes|no|okay|ok|well|right)\b/i;
  const SENT_END = /(?<=[.!?])(?<close>["”’)]*)\s+(?=["“‘(]?[A-Z0-9])/g;
  const normWs = (t) => String(t ?? "").replace(/\s+/g, " ").trim();
  const wc = (t) => CAF.lib.text.wordCount(t);
  const QUOTE_MAP = { "‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-", "\u00a0": " ", "…": "..." };

  /* ------------------------------------------------------------- local ingestion */
  function guessTitle(text) {
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
  async function loadOne(ctx, ref, kind) { // kind: "file" | "content"
    const s = String(ref).trim();
    if (/^https?:\/\//i.test(s)) {
      if (!ctx.isDemoUrl(s)) return { unavailable: `the hosted console cannot fetch ${s} - paste the text into the content field, or run the Python service` };
      const page = ctx.page({ url: s });
      if (page.status !== 200) return { error: `Could not fetch ${s}: status ${page.status}` };
      const h = htmlToText(page.html);
      return { doc: { source: s, title: h.title, text: h.text, mime: "text/html" } };
    }
    if (kind === "content") return { doc: { source: "inline-text", title: guessTitle(s), text: s, mime: "text/plain" } };
    const name = s.replace(/^\.?\//, "").replace(/^sample_data\//, "").replace(/\.[A-Za-z0-9]+$/, "");
    if (!ctx.hasData(name)) return { unavailable: `'${s}' is not embedded in the hosted console - paste its text into the content field, or run the Python service` };
    const ext = (s.match(/\.([A-Za-z0-9]+)$/) || [, "txt"])[1].toLowerCase();
    const raw = await ctx.dataRaw(name);
    if (ext === "html" || ext === "htm") { const h = htmlToText(raw); return { doc: { source: s, title: h.title, text: h.text, mime: "text/html" } }; }
    return { doc: { source: s, title: guessTitle(ext === "pdf" ? raw.replace("[page 1]", "") : raw), text: raw, mime: ext === "pdf" ? "application/pdf" : ext === "md" ? "text/markdown" : ext === "docx" ? "application/vnd.openxmlformats-officedocument.wordprocessingml.document" : "text/plain" } };
  }
  function locate(text, offset) { // caf.connectors.ingestion.Document.locate
    const before = text.slice(0, offset);
    const line = (before.match(/\n/g) || []).length + 1;
    let section = null; for (const m of before.matchAll(/^#{1,6}\s+(.+)$/gm)) section = m[1].trim();
    const ls = before.lastIndexOf("\n") + 1, nl = text.indexOf("\n", offset);
    const m = text.slice(ls, nl >= 0 ? nl : text.length).match(/^\s*\[?(\d{1,2}:\d{2}(?::\d{2})?)\]?/);
    let page = null; for (const pm of before.matchAll(/\[page (\d+)\]/g)) page = +pm[1];
    return { line, section, timestamp: m ? m[1] : null, page };
  }

  /* ------------------------------------------------------------- anchoring + attribution (ported 1:1) */
  /** Canonical form (textutil.canonical rules) + for every canonical char the [start, end) of its source char. */
  function canonicalMap(text) {
    const out = [], starts = [], ends = []; let space = true;
    const s = String(text || "");
    for (let i = 0; i < s.length;) {
      const cp = s.codePointAt(i); const ch = String.fromCodePoint(cp); const w = ch.length;
      for (const c of (QUOTE_MAP[ch] ?? ch).normalize("NFKC").toLowerCase()) {
        if (/\s/.test(c)) { if (space) continue; out.push(" "); starts.push(i); ends.push(i + w); space = true; }
        else { out.push(c); starts.push(i); ends.push(i + w); space = false; }
      }
      i += w;
    }
    if (out.length && out[out.length - 1] === " ") { out.pop(); starts.pop(); ends.pop(); }
    return { canon: out.join(""), starts, ends };
  }
  function cleanCandidate(text) {
    let t = String(text || "").trim().replace(/^["“”‘’']+|["“”‘’']+$/g, "").trim(), speaker = null;
    const m = tsAt(t, 0);
    if (m) { speaker = (m.groups.speaker || "").trim() || null; t = t.slice(m[0].length); }
    return [t.trim().replace(/^["“”‘’']+|["“”‘’']+$/g, "").trim(), speaker];
  }
  function anchorSpan(candidate, src) {
    const c = canonicalMap(candidate).canon;
    if (!c) return null;
    const pos = src.canon.indexOf(c);
    if (pos < 0) return null;
    let occ = 0; for (let p = src.canon.indexOf(c); p >= 0; p = src.canon.indexOf(c, p + 1)) occ++;
    return [src.starts[pos], src.ends[pos + c.length - 1], occ];
  }
  const rejectReason = (c) => (c.includes("...") || c.includes("…") ? "contains an ellipsis - elided quotes cannot be anchored verbatim"
    : wc(c) < MIN_QUOTE_WORDS ? `too short (< ${MIN_QUOTE_WORDS} words)` : null);
  function transcriptSpeaker(text, offset) {
    const lineStart = text.lastIndexOf("\n", Math.max(0, offset - 1)) + 1;
    let last = null;
    for (const m of text.slice(0, lineStart + 200).matchAll(tsAll())) if (m.index <= offset) last = m;
    return last ? [((last.groups.speaker || "").trim() || null), last.groups.ts] : [null, null];
  }
  function interviewRoles(text) {
    const head = String(text || "").slice(0, 4000), roles = {};
    let m = head.match(new RegExp(`interview with\\s+(?<name>${NAME2})`));
    if (m) roles.A = m.groups.name.trim();
    m = head.match(new RegExp(`^[ \\t]*(?:interviewer|interviewed by)[ \\t]*:?[ \\t]*(?<name>${NAME2})`, "mi"));
    if (m) roles.Q = m.groups.name.trim();
    return roles;
  }
  function roleSpeaker(text, offset, roles) {
    if (!roles || !Object.keys(roles).length) return null;
    let last = null;
    for (const m of text.slice(0, offset + 1).matchAll(new RegExp(QA_LINE_SRC, "gm"))) last = m;
    return last ? roles[last.groups.role] || null : null;
  }
  function isSpeakerLabel(label, text) {
    const lab = String(label || "").trim();
    if (!lab || NOT_SPEAKERS.has(lab.toLowerCase())) return false;
    if (lab.split(/\s+/).length >= 2) return true;
    return (String(text || "").match(new RegExp(`^[ \\t]*${lab.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}:[ \\t]`, "gm")) || []).length >= 3;
  }
  function nameLineSpeaker(text, offset) {
    const ls = text.lastIndexOf("\n", Math.max(0, offset - 1)) + 1;
    const m = text.slice(ls).match(NAME_LINE);
    return m && isSpeakerLabel(m.groups.speaker, text) && ls + m[0].length <= offset + 1 ? m.groups.speaker.trim() : null;
  }
  function documentAuthor(text) {
    const head = String(text || "").slice(0, 4000);
    let m = head.match(/\b(?:published|written|prepared|produced|issued)\s+by\s+(?<name>[A-Z][\w&'’.-]*(?:[ \t]+[A-Z][\w&'’.-]*){0,4})/i);
    if (m && m.groups.name.toLowerCase() !== "the") return m.groups.name.trim().replace(/[.,]+$/, "");
    m = head.match(new RegExp(`^[ \\t]*(?:By|Author|Authors|Lead authors?)[ \\t]*:?[ \\t]+(?<name>${NAME2})`, "m"));
    return m ? m.groups.name.trim() : null;
  }
  function attributionNear(text, start, end) {
    const after = text.slice(end, end + 250);
    for (const rx of [DASH_AFTER, AFTER, INTERVIEW_AFTER, NEXT_LINE_NAME, AFTER_INV]) { const m = after.match(rx); if (m) return m.groups.name.trim(); }
    let m = text.slice(Math.max(0, start - 200), start).match(BEFORE);
    if (m) return m.groups.name.trim();
    m = text.slice(Math.max(0, start - 160), end + 160).match(ACCORDING);
    return m ? m.groups.name.trim() : null;
  }
  function resolveSpeaker(text, start, end, modelSpeaker, isTranscript, roles, author) {
    if (isTranscript) { const [sp] = transcriptSpeaker(text, start); if (sp) return [sp, "transcript"]; }
    let sp = roleSpeaker(text, start, roles) || nameLineSpeaker(text, start);
    if (sp) return [sp, "transcript"];
    sp = attributionNear(text, start, end);
    if (sp) return [sp, "attribution"];
    if (modelSpeaker && String(modelSpeaker).trim() && !["unknown", "n/a", "none", "unattributed"].includes(String(modelSpeaker).trim().toLowerCase())) return [String(modelSpeaker).trim(), "model"];
    if (author && !isTranscript) return [author, "document"];
    return [null, "none"];
  }
  const locationLabel = (loc) => [loc.timestamp, loc.page ? `p. ${loc.page}` : null, loc.section ? `§ ${loc.section}` : null, `line ${loc.line}`].filter(Boolean).join(", ");
  async function bankKey(text) {
    const data = new TextEncoder().encode(CAF.lib.text.canonical(text));
    try { const h = await crypto.subtle.digest("SHA-1", data); return [...new Uint8Array(h)].map((b) => b.toString(16).padStart(2, "0")).join("").slice(0, 16); }
    catch { return U.hash(CAF.lib.text.canonical(text)).padStart(16, "0").slice(0, 16); }
  }

  /* ------------------------------------------------------------- heuristic quotability ranker (ported 1:1) */
  function endsWithAbbr(chunk, pos) {
    const m = chunk.slice(0, pos).match(/(\S+)$/);
    if (!m) return false;
    const tok = m[1].replace(/^["“”‘’'()[\]]+|["“”‘’'()[\]]+$/g, "").replace(/\.+$/, "").toLowerCase();
    return ABBR.has(tok) || /^[a-z]$/.test(tok);
  }
  function splitSentences(text, us, ue) {
    const chunk = text.slice(us, ue), spans = []; let cur = 0;
    for (const m of chunk.matchAll(SENT_END)) {
      if (endsWithAbbr(chunk, m.index)) continue;
      const segEnd = m.index + m.groups.close.length;
      if (chunk.slice(cur, segEnd).trim()) spans.push([us + cur, us + segEnd]);
      cur = m.index + m[0].length;
    }
    if (chunk.slice(cur).trim()) spans.push([us + cur, ue]);
    return spans;
  }
  function sentenceSpans(text) {
    const units = []; let pos = 0, para = null, inCode = false;
    const flush = () => { if (para) { units.push([para[0], para[1]]); para = null; } };
    const lines = String(text || "").split(/(?<=\n)/);
    lines.forEach((line, li) => {
      const body = line.replace(/\r?\n$/, ""); const ls = pos, le = pos + body.length; pos += line.length;
      const raw = text.slice(ls, le), s = raw.trim();
      if (s.startsWith("```")) { inCode = !inCode; flush(); return; }
      if (inCode || !s || s.startsWith("#") || s.startsWith("|") || s.startsWith("[page") || /^[-=*_]{3,}$/.test(s)) { flush(); return; }
      const m = tsAt(text, ls);
      const qa = m ? null : raw.match(new RegExp(QA_LINE_SRC));
      let nm = m || qa ? null : raw.match(NAME_LINE);
      if (nm && !isSpeakerLabel(nm.groups.speaker, text)) nm = null;
      const mk = m || qa || nm ? null : raw.match(MARKER);
      if (m || qa || nm || mk) {
        flush();
        const start = m ? ls + m[0].length : ls + (qa || nm || mk)[0].length;
        const content = text.slice(start, le).trim();
        const nxt = (lines.slice(li + 1).find((x) => x.trim()) || "").trim();
        if (mk && /^[ \t]*\d+[.)]/.test(raw) && wc(content) <= 6 && !/[.!?:;,"”]$/.test(content) && !/^\p{Ll}/u.test(nxt)) return; // numbered heading in PDF text
        para = [start, le];
        return;
      }
      if (!para) para = [ls + (raw.length - raw.trimStart().length), le]; else para[1] = le;
    });
    flush();
    const out = [];
    for (let [us, ue] of units) {
      BOLD_LEAD.lastIndex = us;
      const lead = BOLD_LEAD.exec(text);
      if (lead && lead.index === us && us + lead[0].length < ue) us += lead[0].length;
      for (let [a, b] of splitSentences(text, us, ue)) {
        while (a < b && /\s/.test(text[a])) a++;
        while (b > a && /\s/.test(text[b - 1])) b--;
        if (b > a) out.push([a, b]);
      }
    }
    return out;
  }
  function quotedPassages(text) {
    const out = [];
    for (const m of String(text || "").matchAll(QUOTED)) {
      const inner = m.groups.inner.trim(); const n = wc(inner);
      if (n < 6 || n > 70) continue;
      let a = m.index + 1, b = a + m.groups.inner.length;
      while (a < b && /\s/.test(text[a])) a++;
      while (b > a && /\s/.test(text[b - 1])) b--;
      out.push([a, b]);
    }
    return out;
  }
  function quotability(sentence, topic = null, explicit = false) {
    const s = normWs(sentence), n = wc(s);
    if (n < 8 || n > (explicit ? 70 : 50) || s.includes("http") || s.includes("@") || s.includes("[page")) return 0;
    let score = 1;
    if (n >= 12 && n <= 40) score += 1; else if (!explicit) score -= 0.5;
    if (n >= 15 && n <= 30) score += 0.4;
    if (/\d/.test(s)) score += 1;
    score += Math.min(1.2, 0.5 * new Set((s.match(STRONG) || []).map((x) => x.toLowerCase())).size);
    if (FIRST_PERSON.test(s)) score += 1;
    if (explicit) score += 1.5; else if (/[“"][^”"]{3,}[”"]/.test(s)) score += 0.7;
    if (/\b(but|yet|instead|rather than|not because)\b/i.test(s)) score += 0.3;
    if (FILLER.test(s)) score -= 1;
    if (s.endsWith("?")) score -= 1.2;
    if (PLEASANTRY.test(s)) score -= 1.5;
    if (CONTEXT_START.test(s)) score -= 0.6;
    if (/\*\*|__|`|\]\(/.test(s)) score -= 1;
    if (!explicit && /^[A-Z][\w &/-]{1,24}:\s/.test(s)) score -= 1.5;
    if (!explicit && !/[.!?]["”’)]*$/.test(s)) score -= 0.7;
    if (topic) { const tw = new Set(topic.toLowerCase().match(/[a-z]{4,}/g) || []); if (tw.size && (s.toLowerCase().match(/[a-z]{4,}/g) || []).some((w) => tw.has(w))) score += 0.8; }
    return Math.round(Math.max(0, score) * 1000) / 1000;
  }
  function heuristicQuotes(text, k, { topic = null, exclude = [], isTranscript = false } = {}) {
    const overlaps = (a, b, spans) => spans.some(([x, y]) => !(b <= x || a >= y));
    const cands = [];
    const explicit = quotedPassages(text).filter(([a, b]) => !overlaps(a, b, exclude));
    for (const [a, b] of explicit) { const sc = quotability(text.slice(a, b), topic, true); if (sc >= 2) cands.push({ start: a, end: b, text: normWs(text.slice(a, b)), score: sc }); }
    for (const [a, b] of sentenceSpans(text)) {
      if (overlaps(a, b, exclude) || overlaps(a, b, explicit)) continue;
      const sc = quotability(text.slice(a, b), topic);
      if (sc >= 2) cands.push({ start: a, end: b, text: normWs(text.slice(a, b)), score: sc });
    }
    cands.sort((x, y) => y.score - x.score || x.start - y.start);
    const chosen = [], perSpeaker = {};
    for (const c of cands.slice(0, Math.max(60, k * 6))) {
      if (chosen.length >= k) break;
      if (chosen.length && Math.max(0, ...CAF.lib.text.similarity(c.text, chosen.map((x) => x.text))) > 0.6) continue;
      const sp = isTranscript ? transcriptSpeaker(text, c.start)[0] : null;
      if (sp && (perSpeaker[sp] || 0) >= Math.max(2, Math.floor(k / 2))) continue;
      if (sp) perSpeaker[sp] = (perSpeaker[sp] || 0) + 1;
      chosen.push(c);
    }
    return chosen;
  }
  function cleanTitle(title, source = "", text = "") {
    const good = (t) => { const c = t.replace(/^["“‘']+/, "").slice(0, 1); const n = wc(t);
      return n >= 2 && n <= 24 && t.length <= 140 && /\p{Lu}|\d/u.test(c) && !t.startsWith("(") && !t.toLowerCase().startsWith("illustrative"); };
    const strip = (x) => normWs(String(x || "").replace(tsAll(), "")).replace(/^[#\s]+|[#\s]+$/g, "");
    const t = strip(title), transcript = !!tsAt(String(title || ""), 0);
    if (t && good(t) && !transcript) return t;
    if (!transcript) for (const line of String(text || "").split(/\r?\n/).slice(0, 25)) {
      const cand = strip(line);
      if (cand && good(cand) && !/[.,;:]$/.test(cand) && !cand.includes("[page")) return cand;
    }
    const stem = String(source || "").replace(/\\/g, "/").split("/").pop().replace(/\.[^.]*$/, "").replace(/[_-]+/g, " ").trim();
    return stem ? stem.charAt(0).toUpperCase() + stem.slice(1).toLowerCase() : t || "Untitled";
  }

  /* ------------------------------------------------------------- the agent */
  CAF.agent("quote_extraction", {
    async run(input, ctx) {
      const file = (input.file && String(input.file).trim()) || (input.path && String(input.path).trim()) || "";
      const content = String(input.content || "").trim();
      if (!file && !content) throw new Error("Provide content (text or URL) and/or file - at least one is required.");
      const minQ = Math.min(input.min_quotes || 5, input.max_quotes || 10), maxQ = input.max_quotes || 10;

      // -- ingest
      const docs = [];
      for (const [ref, kind] of [[file, "file"], [content, "content"]]) {
        if (!ref) continue;
        const r = await loadOne(ctx, ref, kind);
        if (r.error) throw new Error(`Could not ingest the source: ${r.error}`);
        if (r.unavailable) { ctx.degraded(r.unavailable); continue; }
        docs.push(r.doc);
      }
      if (!docs.length) {
        ctx.check({ rule: "ingest.source", status: "fail", severity: "high", message: "No readable source in the hosted console - nothing to extract." });
        return { output: { source_title: "Source not available", source: file || content, quotes: [], rejected: [], bank_added: 0, bank_existing: 0 },
          summary: "No quotes: the source cannot be read in the hosted console", view: [{ type: "markdown", title: "Source not available", markdown: "Paste the text into the content field, or run the Python service for files and live URLs." }] };
      }
      const text = docs.map((d) => d.text).join("\n\n");
      if (!text.trim()) throw new Error("The source contains no extractable text.");
      const title = cleanTitle(docs[0].title, docs[0].source, text);
      const source = docs.map((d) => d.source).join(" + ");
      const isTranscript = (text.match(tsAll()) || []).length >= 3;
      const roles = interviewRoles(text), author = documentAuthor(text);
      ctx.check({ rule: "ingest.parsed", status: "info", severity: "low", target: source,
        message: `Parsed '${title}' locally (${docs.map((d) => d.mime).join(", ")}): ${wc(text).toLocaleString("en-US")} words${isTranscript ? ", timestamped transcript" : ""}.` });

      // -- propose (model)
      let candidates = [];
      const excerpt = text.length > MAX_MODEL_CHARS ? text.slice(0, MAX_MODEL_CHARS) : text;
      if (text.length > MAX_MODEL_CHARS) ctx.warn(`Source is ${text.length.toLocaleString("en-US")} chars; the model saw the first ${MAX_MODEL_CHARS.toLocaleString("en-US")} (the heuristic ranker covers all of it).`);
      try {
        ctx.progress("Proposing candidate quotes");
        const res = await ctx.generate("extract", { source: excerpt, title, max_quotes: maxQ, min_quotes: minQ, topic: input.topic, is_transcript: isTranscript });
        candidates = res.quotes || [];
      } catch (e) { ctx.warn(`Model proposal failed (${e.message}); heuristic ranker used.`); }

      // -- anchor (deterministic verification)
      const src = canonicalMap(text);
      const accepted = [], rejected = [];
      const quoteAt = (start, end, speaker, spSrc, method, score) => {
        const loc = locate(text, start);
        if (!loc.timestamp && tsAll().test(text)) loc.timestamp = transcriptSpeaker(text, start)[1];
        loc.label = locationLabel(loc);
        return { text: normWs(text.slice(start, end)), speaker, speaker_source: spSrc, location: loc, char_start: start, char_end: end, verified: true, method, score, bank_id: null };
      };
      for (const c of candidates) {
        const raw = String(c.text || "");
        const [cleaned, prefixSpeaker] = cleanCandidate(raw);
        const elided = cleaned.includes("...") || cleaned.includes("…");
        const span = elided ? null : anchorSpan(cleaned, src);
        let reason = null;
        if (!span) reason = elided ? rejectReason(cleaned) : "not found in the source after canonical normalisation (paraphrased or invented)";
        else if (wc(cleaned) < MIN_QUOTE_WORDS) reason = rejectReason(cleaned);
        if (reason) { rejected.push({ text: normWs(raw).slice(0, 400), speaker: c.speaker || null, reason }); continue; }
        const [start, end] = span;
        if (accepted.some((a) => !(end <= a.char_start || start >= a.char_end))) { rejected.push({ text: normWs(raw).slice(0, 400), speaker: c.speaker || null, reason: "duplicate / overlaps an accepted quote" }); continue; }
        if (accepted.length >= maxQ) { rejected.push({ text: normWs(raw).slice(0, 400), speaker: c.speaker || null, reason: `over the ${maxQ}-quote limit` }); continue; }
        const [speaker, spSrc] = resolveSpeaker(text, start, end, c.speaker || prefixSpeaker, isTranscript, roles, author);
        accepted.push(quoteAt(start, end, speaker, spSrc, "llm", null));
      }
      ctx.check({ rule: "quotes.anchored", status: accepted.length || !candidates.length ? "pass" : "warn", severity: "medium",
        message: `${accepted.length} of ${candidates.length} model candidate(s) anchored verbatim in the source; ${rejected.length} rejected.`, data: { anchored: accepted.length, rejected: rejected.length } });

      // -- heuristic top-up (offline or too few anchored)
      let target = 0;
      if (!ctx.llmLive && !accepted.length) { target = Math.max(minQ, Math.min(maxQ, 8)); ctx.degraded("offline model: quotes chosen by the deterministic quotability ranker"); }
      else if (accepted.length < minQ) { target = minQ; ctx.warn(`Only ${accepted.length} model quote(s) could be anchored; topped up with heuristic picks to ${target}.`); }
      if (target) {
        const picks = heuristicQuotes(text, target - accepted.length, { topic: input.topic, exclude: accepted.map((a) => [a.char_start, a.char_end]), isTranscript });
        for (const p of picks) { const [speaker, spSrc] = resolveSpeaker(text, p.start, p.end, null, isTranscript, roles, author); accepted.push(quoteAt(p.start, p.end, speaker, spSrc, "heuristic", p.score)); }
        ctx.check({ rule: "quotes.heuristic", status: "info", severity: "low",
          message: `${picks.length} quote(s) added by the quotability ranker (numbers, strong claims, first-person opinion, quotation marks, 12-40 words) - verbatim by construction.` });
      }
      const quotes = accepted.sort((a, b) => a.char_start - b.char_start);

      // -- quote bank (browser-local; the shared bank is the Python store connector)
      let added = 0, existing = 0;
      if (input.save_to_bank !== false && quotes.length) {
        const bank = U.storage.get("caf.quote_bank", {}) || {};
        const now = U.now();
        for (const q of quotes) {
          const key = await bankKey(q.text); q.bank_id = key;
          if (bank[key]) { existing++; const srcs = [...new Set([...(bank[key].sources || [bank[key].source]), source])].filter(Boolean).sort(); bank[key] = { ...bank[key], sources: srcs, last_seen: now }; continue; }
          bank[key] = { id: key, text: q.text, speaker: q.speaker, source, sources: [source], source_title: title, location: q.location, char_start: q.char_start, char_end: q.char_end,
            method: q.method, topic: input.topic || null, run_id: ctx.runId, created_at: now, last_seen: now };
          added++;
        }
        U.storage.set("caf.quote_bank", bank);
        ctx.check({ rule: "quote_bank.saved", status: "info", severity: "low", message: `Quote bank (this browser): ${added} new, ${existing} already banked (deduplicated by canonical-text hash). The shared, searchable bank is the Python store connector.` });
      }
      const unattributed = quotes.filter((q) => !q.speaker).length;
      if (quotes.length) ctx.check({ rule: "quotes.attribution", status: unattributed ? "warn" : "pass", severity: "low",
        message: `${quotes.length - unattributed} of ${quotes.length} quote(s) attributed` + (unattributed ? `; ${unattributed} without a verifiable speaker.` : " (speakers from transcript prefixes / attribution patterns).") });
      ctx.check({ rule: "quotes.count", status: quotes.length >= minQ ? "pass" : "warn", severity: "medium", message: `${quotes.length} verified quote(s) (target ${minQ}-${maxQ}).` });
      const output = { source_title: title, source, quotes, rejected, bank_added: added, bank_existing: existing };
      ctx.artifact({ name: "quotes.csv", mime: "text/csv", description: "Verified quotes",
        content: CAF.lib.csv.stringify(quotes.map((q) => ({ text: q.text, speaker: q.speaker || "", location: q.location.label, char_start: q.char_start, char_end: q.char_end, method: q.method, bank_id: q.bank_id || "" })),
          ["text", "speaker", "location", "char_start", "char_end", "method", "bank_id"]) });
      ctx.artifact({ name: "quotes.json", mime: "application/json", content: JSON.stringify(output, null, 2), description: "Verified + rejected quotes" });
      const nLlm = quotes.filter((q) => q.method === "llm").length;
      return {
        output,
        summary: `${quotes.length} verified quote(s) from '${title.slice(0, 60)}' (${nLlm} model, ${quotes.length - nLlm} heuristic); ${rejected.length} rejected`,
        view: [
          { type: "table", title: "Verified quotes", rows: quotes.map((q) => ({ quote: q.text, speaker: q.speaker || "-", where: q.location.label, method: q.method })) },
          ...(rejected.length ? [{ type: "table", title: "Rejected candidates", rows: rejected.map((r) => ({ candidate: r.text, reason: r.reason })) }] : []),
        ],
      };
    },
  });
})();
