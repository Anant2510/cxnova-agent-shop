/* Content Adaptation - browser twin of caf/agents/content/content_adaptation.py.
   One source asset -> channel versions governed by the channel_registry skill. Same deterministic
   validator (characters incl. X URL weighting and SMS GSM-7/UCS-2 segments, hashtags, emoji, links,
   required prefix / opt-out, figures not in the source), one model repair pass (live model only),
   then the deterministic fixer; per-channel files, a content calendar CSV and adaptations.json.
   Sources: pasted text / HTML, embedded sample documents (sample_data/<path> -> dataset), demo-site
   URLs, or upstream content; other URLs degrade with a reason (the page has no network). */
(() => {
  const T = CAF.lib.text;
  const URL_RX = /https?:\/\/[^\s)>\]]+/g;
  const HASHTAG = /(?<![\p{L}\p{N}_#&])#([A-Za-z][A-Za-z0-9_]*)/gu;
  const EMOJI = /[\u{1F000}-\u{1FAFF}\u2600-\u27BF\u2B05-\u2B55\u2300-\u23FF]/gu;
  const EMOJI_JOINERS = /[\uFE0F\u200D\u{1F3FB}-\u{1F3FF}]/gu; // variation selector, ZWJ, skin tones
  const NUMBER = /(?<![\p{L}\p{N}_.])\p{Nd}[\p{Nd},]*(?:\.\p{Nd}+)?%?/gu;
  const GSM_BASIC = new Set([..."@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"]);
  const GSM_EXT = new Set([..."^{}\\[~]|€\f"]);
  const WEEKDAYS = { mon: 0, tue: 1, wed: 2, thu: 3, fri: 4, sat: 5, sun: 6 };
  const FIXABLE = new Set(["max_chars", "hashtags_max", "hashtags_min", "emoji_max", "links_not_allowed", "links_max", "structure_prefix", "must_include", "sms_segments", "cta_link_missing"]);
  const len = (s) => [...String(s ?? "")].length;
  const normWs = (s) => String(s ?? "").replace(/\s+/g, " ").trim();
  const reEsc = (s) => String(s).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const pyStr = (s) => { s = String(s); const q = s.includes("'") && !s.includes('"') ? '"' : "'";
    return q + s.replace(/\\/g, "\\\\").replace(new RegExp(q, "g"), "\\" + q).replace(/\n/g, "\\n").replace(/\r/g, "\\r").replace(/\t/g, "\\t") + q; };
  const pyList = (xs) => `[${xs.map(pyStr).join(", ")}]`;
  const sortStr = (xs) => [...xs].sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
  const splitlines = (t) => { const p = String(t ?? "").split(/\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]/); if (p.length && p[p.length - 1] === "") p.pop(); return p; };
  const findall = (rx, s) => [...String(s ?? "").matchAll(rx)].map((m) => (m.length > 1 ? m[1] : m[0]));
  function truncate(text, n, ell = "...") {
    const cps = [...String(text ?? "")];
    if (cps.length <= n) return String(text ?? "");
    const cut = cps.slice(0, Math.max(0, n - ell.length)).join(""), i = cut.lastIndexOf(" ");
    return (i >= 0 ? cut.slice(0, i) : cut) + ell;
  }
  const capitalize = (w) => (w ? w[0].toUpperCase() + w.slice(1).toLowerCase() : w);
  const isUpper0 = (s) => { const c = [...s][0] || ""; return c !== c.toLowerCase() && c === c.toUpperCase(); };
  const isLower0 = (s) => { const c = [...s][0] || ""; return c !== c.toUpperCase() && c === c.toLowerCase(); };

  // ---------------------------------------------------------------- ingestion (LocalIngestion twins)
  /** ingestion._guess_title */
  function guessTitle(text) {
    const lines = splitlines(text).map((l) => l.trim()).filter(Boolean);
    for (const line of lines) if (line.startsWith("#")) return [...line.replace(/^#+/, "").trim()].slice(0, 120).join("");
    for (const line of lines.slice(0, 40)) {
      const t = line.trim(), low = t.toLowerCase();
      if (low.startsWith("illustrative") || low.startsWith("[page") || low.startsWith("---") || /^\[?\d{1,2}:\d{2}/.test(t)) continue;
      const ws = t.split(/\s+/).filter(Boolean);
      if (ws.length >= 2 && ws.length <= 16 && !/[.,;:]$/.test(t) && isUpper0(t)) return [...t].slice(0, 120).join("");
    }
    return lines.length ? [...lines[0]].slice(0, 120).join("") : "Untitled";
  }
  const cleanTranscript = (raw) => splitlines(raw).map((l) => l.trim())
    .filter((l) => l && l !== "WEBVTT" && !/^\d+$/.test(l) && !(l.includes("-->") && /\d\d:\d\d/.test(l))).join("\n");
  /** LocalIngestion._from_html: <title> by regex; body = main content as Markdown-ish text (Python uses trafilatura). */
  function fromHtml(markup, source) {
    const m = String(markup).match(/<title[^>]*>([\s\S]*?)<\/title>/i), title = m ? m[1].replace(/\s+/g, " ").trim() : "";
    const doc = CAF.lib.html.parse(markup);
    doc.querySelectorAll("script, style, noscript, template, nav, header, footer, aside, form, figure, iframe, svg, button").forEach((e) => e.remove());
    doc.querySelectorAll("[class], [id]").forEach((e) => { if (/byline|author|share|social|breadcrumb|related|comment|newsletter|cookie|callout|promo|skip/i.test(`${e.getAttribute("class") || ""} ${e.id || ""}`)) e.remove(); });
    const root = doc.querySelector("article") || doc.querySelector("main") || doc.body, lines = [];
    const inline = (el) => normWs(el.textContent);
    const walk = (el) => {
      for (const c of el.children) {
        const tag = c.nodeName.toUpperCase();
        if (/^H[1-6]$/.test(tag)) { const t = inline(c); if (t) lines.push(`${"#".repeat(+tag[1])} ${t}`); }
        else if (tag === "LI") { const t = inline(c); if (t) lines.push(`- ${t}`); }
        else if (/^(P|BLOCKQUOTE|PRE|TD|TH|DT|DD|FIGCAPTION)$/.test(tag)) { const t = inline(c); if (t) lines.push(t); }
        else if (c.children.length) walk(c);
        else { const t = inline(c); if (t) lines.push(t); }
      }
    };
    if (root) walk(root);
    const body = lines.join("\n");
    return { source, title: title || guessTitle(body), text: body };
  }
  /** sample_data/<x>.<ext> -> embedded dataset (pdf/docx are embedded as their ingested text). */
  async function loadPath(ctx, s) {
    const clean = s.replace(/^\.?\//, "").replace(/^sample_data\//, ""), ext = (clean.match(/\.[A-Za-z0-9]+$/) || [""])[0].toLowerCase();
    const name = clean.replace(/\.[A-Za-z0-9]+$/, "");
    if (!ctx.hasData(name)) return null;
    const raw = await ctx.dataRaw(name);
    if (ext === ".pdf") return { source: s, title: guessTitle(raw.replace("[page 1]", "")), text: raw };
    if (ext === ".vtt" || ext === ".srt") { const t = cleanTranscript(raw); return { source: s, title: guessTitle(t), text: t }; }
    if (ext === ".html" || ext === ".htm") return fromHtml(raw, s);
    return { source: s, title: guessTitle(raw), text: raw };
  }

  // ---------------------------------------------------------------- deterministic core
  function documentTitle(title, text) {
    const t = normWs(title || "");
    if (t.split(" ").filter(Boolean).length >= 3 && !t.toLowerCase().startsWith("illustrative") && !t.toLowerCase().startsWith("[page")) return t;
    for (let line of splitlines(text)) {
      line = normWs(line.replace(/^#+/, "").trim());
      if (!line || line.toLowerCase().startsWith("illustrative") || line.toLowerCase().startsWith("[page") || isLower0(line)) continue;
      const n = line.split(" ").filter(Boolean).length;
      if (n >= 3 && n <= 15 && !line.endsWith(".")) return truncate(line, 120);
    }
    return t || "Untitled";
  }
  const cleanHashtag = (tag) => { const t = String(tag || "").replace(/^#+/, "").replace(/[^A-Za-z0-9_]/g, ""); return t && /^[A-Za-z]/.test(t) ? t : ""; };
  /** Move a trailing block of hashtags out of the text into the tag list (deduped, case-insensitive). */
  function splitHashtags(text, tags) {
    const lines = String(text || "").trimEnd().split("\n");
    let trailing = [];
    while (lines.length && lines[lines.length - 1].trim() && lines[lines.length - 1].split(/\s+/).filter(Boolean).every((w) => w.startsWith("#")))
      trailing = [...findall(HASHTAG, lines.pop()), ...trailing];
    let body = lines.join("\n").trimEnd();
    const m = body.match(/((?:\s+#[A-Za-z][A-Za-z0-9_]*)+)\s*$/);
    if (m) { trailing = [...findall(HASHTAG, m[1]), ...trailing]; body = body.slice(0, m.index).trimEnd(); }
    const out = [], seen = new Set();
    for (let t of [...trailing, ...(tags || []).map(cleanHashtag)]) { t = cleanHashtag(t); if (t && !seen.has(t.toLowerCase())) { seen.add(t.toLowerCase()); out.push(t); } }
    return [body, out];
  }
  function compose(text, hashtags, spec) {
    const placement = (spec.hashtags || {}).placement ?? "none";
    if (hashtags.length && placement !== "none") return `${text.trimEnd()}\n\n` + hashtags.map((h) => `#${h}`).join(" ");
    return text.trimEnd();
  }
  const countChars = (text, spec) => len(spec.url_chars ? String(text).replace(URL_RX, "x".repeat(Number(spec.url_chars))) : text);
  const countEmoji = (text) => findall(EMOJI, String(text || "").replace(EMOJI_JOINERS, "")).length;
  /** (encoding, units, segments) for an SMS: GSM-7 160/153, UCS-2 70/67. */
  function smsSegments(text) {
    const cps = [...String(text)];
    if (cps.every((c) => GSM_BASIC.has(c) || GSM_EXT.has(c))) { const units = cps.reduce((a, c) => a + (GSM_EXT.has(c) ? 2 : 1), 0); return ["GSM-7", units, units <= 160 ? 1 : Math.ceil(units / 153)]; }
    const units = String(text).length; // UTF-16 code units
    return ["UCS-2", units, units <= 70 ? 1 : Math.ceil(units / 67)];
  }
  const numbersIn = (text) => new Set(findall(NUMBER, String(text || "").replace(HASHTAG, " ").replace(URL_RX, " ")).map((n) => n.replaceAll(",", "").replace(/%+$/, "").replace(/\.+$/, "")));
  function validateAdaptation(final, hashtags, spec, { banned = [], sourceNumbers = null, ctaUrl = null } = {}) {
    const issues = [], n = countChars(final, spec);
    if (n > Number(spec.max_chars)) issues.push({ rule: "max_chars", status: "fail", message: `${n} chars (limit ${spec.max_chars}).` });
    else if (spec.recommended_chars && n > Number(spec.recommended_chars)) issues.push({ rule: "recommended_chars", status: "warn", message: `${n} chars; ${spec.recommended_chars} or fewer recommended.` });
    if (spec.visible_chars) {
      const s = final.trim(), m = /\n|(?<=[.!?])\s/.exec(s), hook = m ? s.slice(0, m.index) : s;
      if (len(hook) > Number(spec.visible_chars)) issues.push({ rule: "hook_visible", status: "warn", message: `Opening line is ${len(hook)} chars; only ${spec.visible_chars} show before truncation.` });
    }
    const hp = spec.hashtags || {}, tags = new Set(findall(HASHTAG, final).map((t) => t.toLowerCase()));
    if (tags.size > Number(hp.max ?? 0)) issues.push({ rule: "hashtags_max", status: "fail", message: `${tags.size} hashtags (max ${hp.max ?? 0}).` });
    else if (tags.size < Number(hp.min ?? 0)) issues.push({ rule: "hashtags_min", status: "warn", message: `${tags.size} hashtags (min ${hp.min ?? 0}).` });
    const emoji = countEmoji(final), emax = (spec.emoji || {}).max ?? 0;
    if (emoji > Number(emax)) issues.push({ rule: "emoji_max", status: "fail", message: `${emoji} emoji (max ${emax}).` });
    const lp = spec.links || {}, links = findall(URL_RX, final), allowed = lp.allowed ?? true;
    if (links.length && !allowed) issues.push({ rule: "links_not_allowed", status: "fail", message: "Links are not clickable / allowed on this channel." });
    else if (links.length > Number(lp.max ?? 1)) issues.push({ rule: "links_max", status: "fail", message: `${links.length} links (max ${lp.max ?? 1}).` });
    else if (ctaUrl && !links.length && allowed && lp.include_cta) issues.push({ rule: "cta_link_missing", status: "warn", message: "The CTA link is missing." });
    if (spec.required_prefix && !final.trimStart().startsWith(spec.required_prefix)) issues.push({ rule: "structure_prefix", status: "fail", message: `Must start with '${spec.required_prefix.trim()}'.` });
    for (const phrase of spec.must_include || []) if (!final.toLowerCase().includes(String(phrase).toLowerCase())) issues.push({ rule: "must_include", status: "fail", message: `Missing required text '${phrase}'.` });
    if (spec.max_segments) {
      const [enc, units, segs] = smsSegments(final);
      if (segs > Number(spec.max_segments)) issues.push({ rule: "sms_segments", status: "fail", message: `${segs} SMS segments (${enc}, ${units} units; max ${spec.max_segments}).` });
    }
    const hits = (banned || []).filter((p) => final.toLowerCase().includes(String(p).toLowerCase()));
    if (hits.length) issues.push({ rule: "brand_banned", status: "fail", message: `Brand-banned phrases ${pyList(hits)}.` });
    if (sourceNumbers) {
      const extra = sortStr([...numbersIn(final)].filter((x) => !sourceNumbers.has(x)));
      if (extra.length) issues.push({ rule: "figures_not_in_source", status: "warn", message: `Figures not found in the source: ${pyList(extra.slice(0, 5))}.` });
    }
    return issues;
  }
  /** Trim to a character budget: whole sentences first, then words. */
  function trimText(text, budget) {
    text = String(text).trim();
    if (len(text) <= budget) return text;
    const parts = text.split(/(?<=[.!?])(\s+)/), sents = parts.filter((_, i) => i % 2 === 0), seps = parts.filter((_, i) => i % 2 === 1);
    const join = () => sents.map((s, i) => s + (i < sents.length - 1 ? seps[i] : "")).join("");
    while (sents.length > 1 && len(join()) > budget) sents.pop();
    let out = join().trim();
    if (len(out) > budget) out = truncate(out, Math.max(1, budget), "...");
    return out;
  }
  /** Deterministic fix to the channel rules -> [text, hashtags, notes]. */
  function fixAdaptation(text, hashtags, spec, { ctaUrl = null, seedHashtags = [], title = "" } = {}) {
    const notes = [], lp = spec.links || {}, hp = spec.hashtags || {}, emax = Number((spec.emoji || {}).max ?? 0), allowed = lp.allowed ?? true, lmax = Number(lp.max ?? 1);
    const placement = hp.placement ?? "none", hmax = Number(hp.max ?? 0), hmin = Number(hp.min ?? 0);
    if (spec.max_segments || emax === 0) { if (countEmoji(text)) notes.push("removed emoji"); text = text.replace(EMOJI, "").replace(EMOJI_JOINERS, ""); }
    if (spec.max_segments) text = T.normQuotes(text);
    else if (countEmoji(text) > emax) { let keep = emax; text = text.replace(EMOJI, (m) => (--keep >= 0 ? m : "")); notes.push(`kept the first ${emax} emoji`); }
    let links = findall(URL_RX, text);
    if (links.length && !allowed) {
      text = text.replace(URL_RX, "").trimEnd();
      if (lp.replacement && !text.toLowerCase().includes(String(lp.replacement).toLowerCase())) text = `${text.trimEnd()} ${lp.replacement}`;
      notes.push("replaced links that are not clickable on this channel");
      links = [];
    } else if (links.length > lmax) {
      for (const extra of links.slice(lmax)) text = text.replaceAll(extra, "");
      links = links.slice(0, lmax);
      notes.push("removed extra links");
    }
    text = text.replace(/[ \t]{2,}/g, " ").trim();
    const link = links.length ? links[0] : (ctaUrl && allowed && lp.include_cta && lmax > 0 ? ctaUrl : null);
    if (link && !links.length) notes.push("added the CTA link");
    const optOut = (spec.must_include || []).some((p) => !text.toLowerCase().includes(String(p).toLowerCase())) ? spec.opt_out_text ?? null : null;
    if (optOut) notes.push("added the opt-out");
    const inline = findall(HASHTAG, text), inlineLow = new Set(inline.map((t) => t.toLowerCase()));
    const room = Math.max(0, hmax - inlineLow.size);
    const tags = placement === "none" ? [] : hashtags.filter((t) => !inlineLow.has(t.toLowerCase())).slice(0, room);
    if (placement === "none" && inline.length && hmax === 0) { text = text.replace(HASHTAG, (m, w) => w); notes.push("removed inline hashtags"); }
    if (tags.length + inline.length < hmin && placement !== "none") {
      for (const t of seedHashtags.map(cleanHashtag)) if (t && ![...tags, ...inline].some((x) => x.toLowerCase() === t.toLowerCase()) && tags.length + inline.length < hmin) tags.push(t);
      notes.push("topped up hashtags");
    }
    if (hashtags.length > tags.length && placement !== "none") notes.push(`kept ${tags.length} of ${hashtags.length} hashtags`);
    if (spec.required_prefix && !text.trimStart().startsWith(spec.required_prefix)) { text = `${spec.required_prefix}${title}\n\n${text}`; notes.push("added the required heading"); }
    let core = link ? text.replaceAll(link, "").trim() : text;
    const tail = [link, optOut].filter(Boolean).join(" "), block = compose("", tags, spec);
    let budget = Number(spec.max_chars) - len(block) - (tail ? len(tail) + 1 : 0);
    if (spec.url_chars && link) budget += len(link) - Number(spec.url_chars);
    if (spec.max_segments) budget = Math.min(budget, 160 * Number(spec.max_segments) - (tail ? len(tail) + 1 : 0));
    if (len(core) > budget) { core = trimText(core, budget); notes.push(`trimmed to ${spec.max_chars} characters`); }
    text = tail ? `${core} ${tail}`.trim() : core;
    if (spec.max_segments) // GSM-7 extension characters count double - shrink until it fits
      while (smsSegments(text)[2] > Number(spec.max_segments) && budget > 20) {
        budget -= Math.max(1, smsSegments(text)[1] - 160 * Number(spec.max_segments));
        core = trimText(core, budget);
        text = tail ? `${core} ${tail}`.trim() : core;
      }
    return [text, tags, notes];
  }
  function channelKey(name, registry) {
    const key = String(name || "").trim().toLowerCase().replace(/[\s-]+/g, "_");
    if (Object.prototype.hasOwnProperty.call(registry, key)) return key;
    const labels = Object.fromEntries(Object.entries(registry).map(([k, v]) => [String((v || {}).label ?? "").toLowerCase().replace(/[\s-]+/g, "_"), k]));
    return labels[key] ?? null;
  }
  /** Model versions -> requested channels: exact key or label first, then unmatched items in order. */
  function reconcileChannels(items, wanted, registry) {
    const out = {}, spare = [];
    for (const it of items) { const key = channelKey(it.channel || "", registry); if (key && wanted.includes(key) && !(key in out)) out[key] = { ...it, channel: key }; else spare.push(it); }
    for (const ch of wanted.filter((c) => !(c in out))) { if (!spare.length) break; out[ch] = { ...spare.shift(), channel: ch }; }
    return out;
  }
  /** Preferred tags first, then CamelCase pairs of the title's content words (e.g. HomeRisk). */
  function seedHashtags(preferred, title) {
    const ws = (String(title || "").match(/[A-Za-z]{3,}/g) || []).filter((w) => !T.STOP.has(w.toLowerCase()));
    const pairs = ws.slice(0, -1).map((a, i) => capitalize(a) + capitalize(ws[i + 1])), singles = ws.filter((w) => w.length >= 5).map(capitalize);
    return [...new Set([...preferred, ...pairs, ...singles].map(cleanHashtag))].filter(Boolean);
  }
  const isoDay = (d) => new Date(d * 864e5).toISOString().slice(0, 10);
  const dayNum = (iso) => Date.UTC(+iso.slice(0, 4), +iso.slice(5, 7) - 1, +iso.slice(8, 10)) / 864e5;
  /** One slot per adaptation on distinct days, each on its channel's preferred weekday when set. */
  function buildCalendar(rows, start, registry) {
    let cursor = dayNum(start);
    return rows.map((r) => {
      const slot = String((registry[r.channel] || {}).slot || ""), m = slot.match(/^\s*([A-Za-z]{3})[a-z]*\s+(\d{1,2}:\d{2})\s*$/);
      let day = cursor, time = "09:00";
      if (m) {
        time = m[2];
        const wd = WEEKDAYS[m[1].toLowerCase()];
        if (wd !== undefined) { const cur = (new Date(cursor * 864e5).getUTCDay() + 6) % 7; day = cursor + (((wd - cur) % 7) + 7) % 7; }
      }
      cursor = day + 1;
      return { ...r, date: isoDay(day), time };
    });
  }
  const fallbackText = (core, keyPoints, spec) => trimText([core, ...keyPoints.slice(0, 1)].filter(Boolean).join(" "), Math.max(40, Number(spec.recommended_chars || spec.max_chars) - 40));
  function ruleLine(channel, spec) {
    const hp = spec.hashtags || {}, lp = spec.links || {}, ep = spec.emoji || {};
    const parts = [`${channel} (${spec.label ?? channel}) - max ${spec.max_chars} characters`];
    if (spec.recommended_chars) parts.push(`aim for ${spec.recommended_chars} or fewer`);
    if (spec.visible_chars) parts.push(`hook within the first ${spec.visible_chars} characters`);
    parts.push(Number(hp.max ?? 0) ? `hashtags ${hp.min ?? 0}-${hp.max ?? 0}` : "no hashtags");
    parts.push(Number(ep.max ?? 0) ? `at most ${ep.max ?? 0} emoji` : "no emoji");
    if (!(lp.allowed ?? true)) parts.push("no links" + (lp.replacement ? ` (say '${lp.replacement}')` : ""));
    else parts.push(`at most ${lp.max ?? 1} link(s)`);
    if ((spec.must_include || []).length) parts.push("must include " + spec.must_include.map((p) => `'${p}'`).join(", "));
    if (spec.required_prefix) parts.push(`start with '${spec.required_prefix.trim()}' heading`);
    parts.push(`structure - ${spec.structure ?? ""}`, `tone - ${spec.tone ?? ""}`);
    return parts.filter(Boolean).join("; ");
  }

  const lib = { guessTitle, documentTitle, cleanHashtag, splitHashtags, compose, countChars, countEmoji, smsSegments, numbersIn, validateAdaptation, trimText,
    fixAdaptation, channelKey, reconcileChannels, seedHashtags, buildCalendar, fallbackText, ruleLine };

  CAF.agent("content_adaptation", {
    lib,
    async run(input, ctx) {
      // -- ingest
      const registry = ctx.skill("channel_registry"), cfg = registry.channels || {}, version = String(registry.version ?? "");
      const lower = input.channels.map((c) => String(c).toLowerCase());
      const wanted = lower.length === 1 && (lower[0] === "all" || lower[0] === "*") ? Object.keys(cfg)
        : [...new Set(input.channels.map((c) => String(c).trim().toLowerCase()).filter(Boolean))];
      const unknown = wanted.filter((c) => !Object.prototype.hasOwnProperty.call(cfg, c));
      if (unknown.length) throw new Error(`Unknown channel(s) ${pyList(unknown)}. Configured in channel_registry: ${pyList(sortStr(Object.keys(cfg)))}`);
      let doc = null, origin = "", failed = false;
      const fail = (kind, msg) => { failed = true; ctx.degraded(`source could not be loaded (${kind}: ${msg})`); ctx.warn(`Could not load ${input.source ?? "None"}: ${msg}`); };
      if (input.text) { doc = { source: "inline-text", title: guessTitle(input.text), text: input.text }; origin = "text"; }
      else if (input.html) { doc = fromHtml(input.html, "inline-html"); origin = "html"; }
      else if (input.source) {
        const s = String(input.source).trim();
        if (/^https?:\/\//i.test(s)) {
          if (!ctx.isDemoUrl(s)) fail("ConnectorError", `the hosted console cannot fetch ${s} (no network) - paste the page into the html field or the text field`);
          else {
            const pg = ctx.site(s);
            if (!pg || pg.status < 200 || pg.status >= 300) fail("ConnectorError", `Could not fetch ${s}: status ${pg ? pg.status : 0}`);
            else { doc = fromHtml(pg.html, s); origin = input.source; }
          }
        } else {
          doc = await loadPath(ctx, s);
          if (doc) origin = input.source;
          else if (len(s) > 200 || s.includes("\n")) { doc = { source: "inline-text", title: guessTitle(s), text: s }; origin = input.source; }
          else fail("ConnectorError", `Source not found: ${input.source}`);
        }
      }
      if (!doc && !failed) {
        const upstream = ctx.upstreamText(12000);
        if (!upstream) throw new Error("Provide source (URL or path), text or html - or run after a content agent in an orchestrated plan.");
        doc = { source: "upstream", title: guessTitle(upstream), text: upstream }; origin = "upstream";
      }
      const title = doc ? documentTitle(doc.title, doc.text) : "Untitled", sourceText = doc ? doc.text : "";
      origin = origin || "none";
      const utm = ctx.upstreamOutput("utm_generation") || {};
      const utmLinks = {};
      for (const l of utm.links || []) if (l.url) utmLinks[l.channel ?? "None"] = l.url;
      const tagged = (ch) => { const k = (cfg[ch] || {}).utm_channel; return k === null || k === undefined ? null : utmLinks[k] || null; };
      const ctaFor = (ch) => tagged(ch) || input.cta_url || null;
      const tagLink = (text, ch) => { const t = tagged(ch), base = String(input.cta_url || "").split("?")[0]; return t && base ? text.replace(new RegExp(reEsc(base) + "(\\?[^\\s)]*)?", "g"), () => t) : text; };

      // -- adapt (one structured call for every channel)
      let core = "", keyPoints = [], drafts = {};
      const final = [];
      if (sourceText.trim()) {
        ctx.progress(`Adapting for ${wanted.length} channel(s)`);
        const draft = await ctx.generate("adapt", { source_title: title, source_text: truncate(sourceText, 9000), channels: wanted, channel_rules: wanted.map((ch) => ruleLine(ch, cfg[ch])),
          audience: input.audience ?? null, goal: input.goal ?? null, cta_url: input.cta_url ?? null, hashtags: (input.hashtags || []).map(cleanHashtag).filter(Boolean) });
        drafts = reconcileChannels(draft.adaptations || [], wanted, cfg);
        core = normWs(draft.core_message || "");
        keyPoints = (draft.key_points || []).filter((k) => String(k).trim()).map(normWs).slice(0, 6);

        // -- validate, one model repair pass, deterministic fix
        const banned = ctx.brand.banned_phrases || [], srcNumbers = numbersIn(sourceText), seeds = seedHashtags([...(input.hashtags || [])], title), st = {};
        for (const ch of wanted) {
          const spec = cfg[ch];
          let d = drafts[ch];
          const notes = d && d.notes ? [d.notes] : [];
          if (!d) { d = { channel: ch, text: fallbackText(core, keyPoints, spec), hashtags: [] }; notes.push("model returned no version for this channel - built from the core message"); }
          let [text, tags] = splitHashtags(d.text, d.hashtags || []);
          text = tagLink(text, ch);
          const cta = ctaFor(ch);
          st[ch] = { text, tags, notes, cta, repaired: false, issues: validateAdaptation(compose(text, tags, spec), tags, spec, { banned, sourceNumbers: srcNumbers, ctaUrl: cta }) };
        }
        const failing = Object.keys(st).filter((ch) => st[ch].issues.some((i) => i.status === "fail"));
        if (failing.length && ctx.llmLive) {
          ctx.progress(`Repairing ${failing.length} channel version(s)`);
          const batch = await ctx.generate("repair", { core_message: core, items: failing.map((ch) => ({ channel: ch, text: st[ch].text, hashtags: st[ch].tags,
            issues: st[ch].issues.filter((i) => i.status === "fail").map((i) => i.message), rules: ruleLine(ch, cfg[ch]) })) });
          for (const a of batch.adaptations || []) {
            const key = channelKey(a.channel, cfg);
            if (key && failing.includes(key) && !st[key].repaired) {
              const spec = cfg[key];
              let [text, tags] = splitHashtags(a.text, a.hashtags || []);
              text = tagLink(text, key);
              Object.assign(st[key], { text, tags, repaired: true, issues: validateAdaptation(compose(text, tags, spec), tags, spec, { banned, sourceNumbers: srcNumbers, ctaUrl: st[key].cta }) });
              st[key].notes.push("repaired by the model");
            }
          }
        }
        for (const [ch, s] of Object.entries(st)) {
          const spec = cfg[ch];
          if (s.issues.some((i) => FIXABLE.has(i.rule))) {
            const [text, tags, fixNotes] = fixAdaptation(s.text, s.tags, spec, { ctaUrl: s.cta, seedHashtags: seeds, title });
            Object.assign(s, { text, tags });
            s.notes.push(...fixNotes.map((n) => `deterministic fix: ${n}`));
            s.issues = validateAdaptation(compose(text, tags, spec), tags, spec, { banned, sourceNumbers: srcNumbers, ctaUrl: s.cta });
          }
          const composed = compose(s.text, s.tags, spec);
          for (const i of s.issues) ctx.check({ rule: `${ch}.${i.rule}`, status: i.status, severity: i.status === "fail" ? "high" : "low", target: ch, message: i.message, ref: `channel_registry ${version}` });
          if (!s.issues.length) ctx.check({ rule: `${ch}.rules`, status: "pass", severity: "low", target: ch, message: `${spec.label}: all channel rules met.` });
          final.push({ channel: ch, label: spec.label ?? ch, text: composed, hashtags: s.tags, char_count: countChars(composed, spec), max_chars: Number(spec.max_chars),
            valid: !s.issues.some((i) => i.status === "fail"), issues: s.issues.map((i) => i.message), notes: s.notes, links: findall(URL_RX, composed),
            emoji_count: countEmoji(composed), sms_segments: spec.max_segments ? smsSegments(composed)[2] : null });
        }
      }

      // -- export
      const start = input.start_date ? String(input.start_date).slice(0, 10) : isoDay(Math.floor(Date.now() / 864e5) + 1);
      const scheduled = buildCalendar(final, start, cfg), adaptations = [];
      for (const row of scheduled) {
        const md = (cfg[row.channel] || {}).format === "markdown", name = `adaptation-${row.channel}.${md ? "md" : "txt"}`;
        const ref = ctx.artifact({ name, mime: md ? "text/markdown" : "text/plain", content: row.text + "\n", description: `${row.label} copy` });
        const { date, time, ...a } = row;
        adaptations.push({ ...a, scheduled_for: `${date} ${time}`, artifact: ref.name });
      }
      let calendar = null;
      if (scheduled.length) {
        const rows = scheduled.map((r) => ({ date: r.date, time: r.time, channel: r.channel, label: r.label, campaign: input.campaign || "", text: r.text,
          hashtags: r.hashtags.map((h) => `#${h}`).join(" "), link: (r.links.length ? r.links : [""])[0], char_count: r.char_count, max_chars: r.max_chars,
          valid: r.valid ? "True" : "False", status: "draft" }));
        calendar = ctx.artifact({ name: "content_calendar.csv", mime: "text/csv", content: CAF.lib.csv.stringify(rows, ["date", "time", "channel", "label", "campaign", "text", "hashtags",
          "link", "char_count", "max_chars", "valid", "status"]), description: "Content calendar (one slot per channel)" }).name;
      }
      const output = { source_title: title, source: origin, core_message: core, key_points: keyPoints, adaptations, all_valid: adaptations.length > 0 && adaptations.every((a) => a.valid),
        registry_version: version, calendar_artifact: calendar };
      ctx.artifact({ name: "adaptations.json", mime: "application/json", content: JSON.stringify(output, null, 2), description: "All adaptations" });
      const ok = adaptations.filter((a) => a.valid).length;
      const summary = !adaptations.length ? `No adaptations produced - the source could not be loaded (${origin}).`
        : `Adapted '${title}' into ${adaptations.length} channel version(s) (${adaptations.map((a) => a.channel).join(", ")}); ${ok}/${adaptations.length} meet every channel rule; calendar from ${start}`;
      return {
        output, summary,
        view: [
          { type: "kv", title: "Source", data: { title, source: origin, core_message: core || "-", registry_version: version } },
          { type: "table", title: "Channel versions", columns: ["channel", "scheduled_for", "chars", "valid", "text", "notes"],
            rows: adaptations.map((a) => ({ channel: a.label, scheduled_for: a.scheduled_for, chars: `${a.char_count}/${a.max_chars}` + (a.sms_segments ? ` (${a.sms_segments} SMS)` : ""),
              valid: a.valid ? "yes" : "no", text: a.text, notes: a.notes.join("; ") })) },
        ],
      };
    },
  });
})();
