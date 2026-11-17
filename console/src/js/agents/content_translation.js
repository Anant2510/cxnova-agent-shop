/* Content Translation - browser twin of caf/agents/web/content_translation.py.
   prepare (segmentation, glossary + DNT, TMX memory parsed with DOMParser) -> memory (exact reuse, difflib-style fuzzy
   matching >= threshold, deterministic number/placeholder adaptation) -> machine translation (Python: the pluggable
   TranslationConnector; browser: the translate.j2 template on Claude, `[offline:<locale>]` placeholders offline) ->
   post-edit (post_edit.j2, with the revert guard) -> locale formatting + deterministic QA -> documents, QA report,
   TMX write-back for review and gated CMS publish. Regexes reproduce Python's Unicode \w \d \b semantics. */
(() => {
  /* ------------------------------------------------------------- regex building blocks (Python re, Unicode mode) */
  const W = "\\p{L}\\p{N}_";                                            // \w
  const D = "\\p{Nd}";                                                  // \d
  const B = `(?:(?<=[${W}])(?![${W}])|(?<![${W}])(?=[${W}]))`;          // \b
  const NB = `(?:(?<=[${W}])(?=[${W}])|(?<![${W}])(?![${W}]))`;         // \B
  const LETTER = "[\\p{L}\\p{Nl}\\p{No}]";                              // [^\W\d_]
  const escRx = (s) => String(s).replace(/[\\^$.*+?()[\]{}|/]/g, "\\$&"); // re.escape (valid under the u flag)
  const clsEsc = (s) => String(s).replace(/[\\\]\[^-]/g, "\\$&");
  const PH_SRC = `\\{\\{\\s*[${W}.]+\\s*\\}\\}|\\{[A-Za-z0-9_]+\\}|%(?:${D}+\\$)?[sdif]|%\\([${W}]+\\)[sdif]|https?:\\/\\/[^\\s<>"')\\]]+|<\\/?[A-Za-z][^>]*>|\\[\\[[^\\]]+\\]\\]`;
  const PH = new RegExp(PH_SRC, "gu");                                  // used only with replace / match / matchAll / split
  const PH_FULL = new RegExp(`^(?:${PH_SRC})$`, "u");
  const MD_PREFIX = new RegExp(`^(\\s*(?:#{1,6}\\s+|[-*+•]\\s+|${D}+[.)]\\s+|>\\s*)*)`, "u");
  const SENT_BOUNDARY = new RegExp(`(?<=[.!?])(["”’)]*)(\\s+)(?=["“‘(\\[{]?[A-Z0-9{%<])`, "gu");
  const TOK = new RegExp(`\\{\\{\\s*[${W}.]+\\s*\\}\\}|\\{[${W}]+\\}|%(?:${D}+\\$)?[sdif]|${D}[${D}.,\\u00a0\\u202f]*${D}|${D}|[${W}]+|[^${W}\\s]`, "gu");
  const LETTERS2 = new RegExp(`${LETTER}{2,}`, "u"), LETTERS2_G = new RegExp(`${LETTER}{2,}`, "gu");
  const ABBREVIATIONS = new Set(["dr", "mr", "mrs", "ms", "prof", "st", "inc", "ltd", "co", "corp", "jr", "sr", "vs", "etc", "e.g", "i.e", "no",
    "fig", "u.s", "a.m", "p.m", "approx", "dept"]);
  const POST_EDIT_BATCH = 25, MT_BATCH = 40;
  const SEVERITY = { placeholders: "fail", numbers: "fail", dnt: "fail", untranslated: "fail", max_chars: "fail", glossary: "warn", length: "warn", offline: "warn", mt_failed: "fail" };
  const SPACES = [" ", " ", " "];
  const XML_NS = "http://www.w3.org/XML/1998/namespace";
  const RX = new Map();
  const cached = (key, make) => { if (!RX.has(key)) RX.set(key, make()); return RX.get(key); };

  /** Python regex source (e.g. the locale_formats.yaml number idioms) -> JS RegExp with the same Unicode semantics. */
  function pyRx(src, flags) {
    let s = String(src);
    const f = new Set(flags.split(""));
    const lead = s.match(/^\(\?([aiLmsux]+)\)/);
    if (lead) { for (const c of lead[1]) if ("ims".includes(c)) f.add(c); s = s.slice(lead[0].length); }
    let out = "", cls = false;
    for (let i = 0; i < s.length; i++) {
      const c = s[i];
      if (c === "\\" && i + 1 < s.length) {
        const n = s[++i];
        if (n === "w") out += cls ? W : `[${W}]`;
        else if (n === "W") out += cls ? "\\W" : `[^${W}]`;
        else if (n === "d") out += D;
        else if (n === "D") out += "\\P{Nd}";
        else if (n === "b") out += cls ? "\\b" : B;
        else if (n === "B") out += cls ? "B" : NB;
        else if (n === "A") out += "^";
        else if (n === "Z") out += "$";
        else if (/[A-Za-z0-9]/.test(n)) out += "\\" + n;
        else if ("^$\\.*+?()[]{}|/".includes(n) || (cls && n === "-")) out += "\\" + n;
        else out += n; // Python identity escapes (\#, \ , \&, \~ ...) are plain literals
      } else if (c === "[" && !cls) {
        cls = true; out += c;
        if (s[i + 1] === "^") { out += "^"; i++; }
        if (s[i + 1] === "]") { out += "\\]"; i++; }
      } else if (c === "]" && cls) { cls = false; out += c; }
      else if (!cls && c === "$") out += f.has("m") ? "$" : "(?=\\n?$)";
      else if (!cls && s.startsWith("(?P<", i)) { out += "(?<"; i += 3; }
      else if (!cls && s.startsWith("(?P=", i)) { const j = s.indexOf(")", i); out += `\\k<${s.slice(i + 4, j)}>`; i = j; }
      else out += c;
    }
    return new RegExp(out, [...f].join(""));
  }

  /* ------------------------------------------------------------- Python value / formatting twins */
  const cps = (s) => Array.from(String(s ?? ""));
  const plen = (s) => cps(s).length;                                    // len(str) counts code points
  const normWs = (t) => String(t ?? "").replace(/\s+/g, " ").trim();    // textutil.normalize_ws
  const canonical = (t) => CAF.lib.text.canonical(t);                   // textutil.canonical
  const truthy = (v) => !(v === null || v === undefined || v === false || v === 0 || v === "" || (Array.isArray(v) && !v.length) || (U.isObj(v) && !Object.keys(v).length));
  const slugify = (t, max = 60) => { // textutil.slugify
    const s = String(t ?? "").normalize("NFKD").replace(/[^\x00-\x7f]/g, "").replace(/[^A-Za-z0-9]+/g, "-").replace(/^-+|-+$/g, "").toLowerCase();
    return s.slice(0, max).replace(/^-+|-+$/g, "") || "item";
  };
  function asciiDigits(s) { // int()/float() accept any Unicode decimal digit
    return String(s).replace(/\p{Nd}/gu, (ch) => {
      if (ch >= "0" && ch <= "9") return ch;
      const cp = ch.codePointAt(0); let z = cp;
      while (z > 0 && /\p{Nd}/u.test(String.fromCodePoint(z - 1))) z--;
      return String((cp - z) % 10);
    });
  }
  function pyFloatParse(t) { // float(str) or null
    const s = asciiDigits(t).trim().replace(/(?<=\d)_(?=\d)/g, "");
    if (/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(s)) return Number(s);
    const m = s.match(/^([+-]?)(inf|infinity|nan)$/i);
    if (m) return m[2].toLowerCase() === "nan" ? NaN : m[1] === "-" ? -Infinity : Infinity;
    return null;
  }
  const pyInt = (s) => parseInt(asciiDigits(s), 10);
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
  const pyRound = (x, nd) => (Number.isFinite(x) ? Number(pyFixed(x, nd)) : x);
  const pyPct0 = (x) => `${pyFixed(x * 100, 0)}%`;
  function pyFloatStr(v) { // str(float) for non-integral values
    if (!Number.isFinite(v)) return Number.isNaN(v) ? "nan" : v > 0 ? "inf" : "-inf";
    const a = Math.abs(v);
    if (a !== 0 && (a < 1e-4 || a >= 1e16)) return v.toExponential().replace(/e([+-])(\d)$/, "e$10$2");
    return String(v);
  }
  const fmtNum = (v) => (Number.isInteger(v) ? BigInt(v).toString() : pyFloatStr(v)); // _fmt_num
  const pyFloatRepr = (v) => (Number.isInteger(v) && Math.abs(v) < 1e16 ? v.toFixed(1) : pyFloatStr(v));
  function pyStr(s) { // repr(str)
    s = String(s);
    const q = s.includes("'") && !s.includes('"') ? '"' : "'";
    let out = "";
    for (const ch of s) {
      const cp = ch.codePointAt(0);
      if (ch === "\\" || ch === q) out += "\\" + ch;
      else if (ch === "\n") out += "\\n"; else if (ch === "\r") out += "\\r"; else if (ch === "\t") out += "\\t";
      else if (ch !== " " && /[\p{C}\p{Z}]/u.test(ch)) out += cp < 0x100 ? `\\x${cp.toString(16).padStart(2, "0")}` : cp < 0x10000 ? `\\u${cp.toString(16).padStart(4, "0")}` : `\\U${cp.toString(16).padStart(8, "0")}`;
      else out += ch;
    }
    return q + out + q;
  }
  const pyList = (items) => `[${items.map(pyStr).join(", ")}]`;
  /* collections.Counter twins */
  const counter = (arr, key = (x) => x) => { const m = new Map(); for (const x of arr) { const k = key(x); m.set(k, (m.get(k) || 0) + 1); } return m; };
  const cEq = (a, b) => [...a].every(([k, v]) => (b.get(k) || 0) === v) && [...b].every(([k, v]) => (a.get(k) || 0) === v);
  const cMinus = (a, b) => { const out = []; for (const [k, v] of a) for (let n = v - (b.get(k) || 0); n > 0; n--) out.push(k); return out; };
  const arrEq = (a, b) => a.length === b.length && a.every((x, i) => x === b[i]);
  const errName = (e) => (e && e.name) || "Error";

  /* ------------------------------------------------------------- difflib.SequenceMatcher (isjunk=None, autojunk=False) */
  const calcRatio = (matches, length) => (length ? (2.0 * matches) / length : 1.0);
  class SequenceMatcher {
    constructor(a, b) {
      this.a = a; this.b = b; this.b2j = new Map(); this.blocks = null; this.fullbcount = null;
      b.forEach((elt, i) => { const l = this.b2j.get(elt); if (l) l.push(i); else this.b2j.set(elt, [i]); });
    }
    findLongestMatch(alo, ahi, blo, bhi) {
      const { a, b, b2j } = this;
      let besti = alo, bestj = blo, bestsize = 0, j2len = new Map();
      for (let i = alo; i < ahi; i++) {
        const next = new Map();
        for (const j of b2j.get(a[i]) || []) {
          if (j < blo) continue;
          if (j >= bhi) break;
          const k = (j2len.get(j - 1) || 0) + 1;
          next.set(j, k);
          if (k > bestsize) { besti = i - k + 1; bestj = j - k + 1; bestsize = k; }
        }
        j2len = next;
      }
      while (besti > alo && bestj > blo && a[besti - 1] === b[bestj - 1]) { besti--; bestj--; bestsize++; }
      while (besti + bestsize < ahi && bestj + bestsize < bhi && a[besti + bestsize] === b[bestj + bestsize]) bestsize++;
      return [besti, bestj, bestsize];
    }
    matchingBlocks() {
      if (this.blocks) return this.blocks;
      const la = this.a.length, lb = this.b.length, queue = [[0, la, 0, lb]], found = [];
      while (queue.length) {
        const [alo, ahi, blo, bhi] = queue.pop();
        const x = this.findLongestMatch(alo, ahi, blo, bhi), [i, j, k] = x;
        if (k) {
          found.push(x);
          if (alo < i && blo < j) queue.push([alo, i, blo, j]);
          if (i + k < ahi && j + k < bhi) queue.push([i + k, ahi, j + k, bhi]);
        }
      }
      found.sort((p, q) => p[0] - q[0] || p[1] - q[1] || p[2] - q[2]);
      let i1 = 0, j1 = 0, k1 = 0;
      const out = [];
      for (const [i2, j2, k2] of found) {
        if (i1 + k1 === i2 && j1 + k1 === j2) k1 += k2;
        else { if (k1) out.push([i1, j1, k1]); i1 = i2; j1 = j2; k1 = k2; }
      }
      if (k1) out.push([i1, j1, k1]);
      out.push([la, lb, 0]);
      return (this.blocks = out);
    }
    opcodes() {
      let i = 0, j = 0;
      const out = [];
      for (const [ai, bj, size] of this.matchingBlocks()) {
        const tag = i < ai && j < bj ? "replace" : i < ai ? "delete" : j < bj ? "insert" : "";
        if (tag) out.push([tag, i, ai, j, bj]);
        i = ai + size; j = bj + size;
        if (size) out.push(["equal", ai, i, bj, j]);
      }
      return out;
    }
    ratio() { return calcRatio(this.matchingBlocks().reduce((s, x) => s + x[2], 0), this.a.length + this.b.length); }
    quickRatio() {
      if (!this.fullbcount) { this.fullbcount = new Map(); for (const x of this.b) this.fullbcount.set(x, (this.fullbcount.get(x) || 0) + 1); }
      const avail = new Map();
      let matches = 0;
      for (const x of this.a) {
        const numb = avail.has(x) ? avail.get(x) : this.fullbcount.get(x) || 0;
        avail.set(x, numb - 1);
        if (numb > 0) matches++;
      }
      return calcRatio(matches, this.a.length + this.b.length);
    }
    realQuickRatio() { const la = this.a.length, lb = this.b.length; return calcRatio(Math.min(la, lb), la + lb); }
  }
  const similarity = (a, b) => new SequenceMatcher(cps(canonical(a)), cps(canonical(b))).ratio();

  /* ------------------------------------------------------------- pure helpers: locale formats */
  function glossaryFor(gl, locale) {
    const terms = gl.terms || {};
    if (Object.prototype.hasOwnProperty.call(terms, locale)) return { ...(terms[locale] || {}) };
    const lang = locale.toLowerCase().split("-")[0];
    const hit = Object.entries(terms).find(([k]) => k.toLowerCase() === locale.toLowerCase() || k.toLowerCase().split("-")[0] === lang);
    return { ...((hit && hit[1]) || {}) };
  }
  function localeFormat(formats, locale) {
    const locs = Object.fromEntries(Object.entries(formats.locales || {}).map(([k, v]) => [k.toLowerCase(), v]));
    const base = { ...(truthy(formats.defaults) ? formats.defaults : { decimal: ".", thousands: ",", date: "MM/DD/YYYY", max_expansion: 1.35 }) };
    let hit = locs[locale.toLowerCase()];
    if (!truthy(hit)) { const e = Object.entries(locs).find(([k]) => k.split("-")[0] === locale.toLowerCase().split("-")[0]); hit = e ? e[1] : null; }
    return { ...base, ...(hit || {}), number_idioms: [...(formats.number_idioms || [])] };
  }
  const dateRx = (pattern) => cached(`date:${pattern}`, () => new RegExp(`(?<!${D})${escRx(pattern).split("YYYY").join(`(?<y>${D}{4})`).split("MM").join(`(?<m>${D}{1,2})`).split("DD").join(`(?<d>${D}{1,2})`)}(?!${D})`, "gu"));
  function numberRx(fmt) {
    const dec = fmt.decimal ?? ".", th = fmt.thousands ?? ",";
    return cached(`num:${dec}|${th}`, () => {
      const thc = clsEsc(th) + (SPACES.includes(th) ? " \\u00a0\\u202f" : "");
      return new RegExp(`(?<![${W}.,])${D}{1,3}(?:[${thc}]${D}{3})+(?:${escRx(dec)}${D}+)?(?![${W}])|(?<![${W}.,])${D}+(?:${escRx(dec)}${D}+)?(?![${W}])`, "gu");
    });
  }
  function parseNumber(token, fmt) {
    let t = String(token);
    const th = fmt.thousands ?? ",";
    for (const sep of SPACES.includes(th) ? new Set([th, ...SPACES]) : new Set([th])) t = t.split(sep).join("");
    t = t.split(fmt.decimal ?? ".").join(".");
    return pyFloatParse(t);
  }
  const datePatterns = (fmt) => [dateRx(fmt.date ?? "MM/DD/YYYY"), dateRx("YYYY-MM-DD")];
  function dateValues(text, fmt) {
    const t = String(text || "").replace(PH, " ");
    const out = [];
    for (const rx of datePatterns(fmt)) for (const m of t.matchAll(rx)) out.push([pyInt(m.groups.y), pyInt(m.groups.m), pyInt(m.groups.d)]);
    return out;
  }
  function stripDatesAndIdioms(text, fmt, idioms) {
    let t = String(text || "").replace(PH, " ");
    for (const rx of datePatterns(fmt)) t = t.replace(rx, " ");
    for (const pat of idioms || []) {
      let rx;
      try { rx = cached(`idiom:${pat}`, () => pyRx(pat, "giu")); } catch { continue; }
      t = t.replace(rx, " ");
    }
    return t;
  }
  function numberValues(text, fmt, idioms) {
    const t = stripDatesAndIdioms(text, fmt, idioms);
    const out = [];
    for (const m of t.matchAll(numberRx(fmt))) { const v = parseNumber(m[0], fmt); if (v !== null) out.push(pyRound(v, 6)); }
    return out;
  }
  function formatNumber(value, decimals, grouped, fmt) {
    let s = pyFixed(value, decimals);
    if (grouped) { const neg = s.startsWith("-"), body = neg ? s.slice(1) : s; const [ip, fp] = body.split("."); s = (neg ? "-" : "") + ip.replace(/\B(?=(\d{3})+$)/g, ",") + (fp !== undefined ? `.${fp}` : ""); }
    const dec = fmt.decimal ?? ".", th = fmt.thousands ?? ",";
    return s.replace(/,/g, "\x00").replace(/\./g, () => dec).replace(/\x00/g, () => th);
  }
  function localiseNumbers(target, source, srcFmt, tgtFmt) {
    if (srcFmt.decimal === tgtFmt.decimal && srcFmt.thousands === tgtFmt.thousands) return [target, 0];
    const srcVals = new Set(numberValues(source, srcFmt));
    const rx = numberRx(srcFmt), sd = srcFmt.decimal ?? ".", st = srcFmt.thousands ?? ",";
    let count = 0;
    const repl = (tok) => {
      if (![sd, st].some((sep) => tok.includes(sep))) return tok;
      const v = parseNumber(tok, srcFmt);
      if (v === null || !srcVals.has(pyRound(v, 6))) return tok;
      const decimals = tok.includes(sd) ? plen(tok.split(sd)[1]) : 0;
      count++;
      return formatNumber(v, decimals, tok.includes(st), tgtFmt);
    };
    const parts = String(target).split(PH), keep = String(target).match(PH) || [];
    const out = [];
    parts.forEach((part, i) => { out.push(part.replace(rx, repl)); if (i < keep.length) out.push(keep[i]); });
    return [out.join(""), count];
  }
  function localiseDates(target, source, srcFmt, tgtFmt) {
    const sp = srcFmt.date ?? "MM/DD/YYYY", tp = tgtFmt.date ?? "MM/DD/YYYY";
    if (sp === tp) return [target, 0];
    const rx = dateRx(sp);
    const srcDates = new Set([...String(source || "").matchAll(rx)].map((m) => m[0]));
    let count = 0;
    const out = String(target).replace(rx, (...args) => {
      const m0 = args[0], g = args[args.length - 1];
      if (!srcDates.has(m0)) return m0;
      const mo = pyInt(g.m), d = pyInt(g.d);
      if (!(mo >= 1 && mo <= 12 && d >= 1 && d <= 31)) return m0;
      count++;
      return tp.split("YYYY").join(g.y).split("MM").join(String(mo).padStart(2, "0")).split("DD").join(String(d).padStart(2, "0"));
    });
    return [out, count];
  }

  /* ------------------------------------------------------------- pure helpers: segmentation */
  function splitSentences(content) { // [(sentence, separator_after)], abbreviation-safe
    const out = [];
    let cur = 0;
    for (const m of content.matchAll(SENT_BOUNDARY)) {
      const prev = content.slice(0, m.index).match(/(\S+)$/u);
      const tok = prev ? prev[1].replace(/\.+$/, "").toLowerCase() : "";
      if (ABBREVIATIONS.has(tok) || /^[a-z]$/.test(tok)) continue;
      out.push([content.slice(cur, m.index + m[1].length), m[2]]);
      cur = m.index + m[0].length;
    }
    out.push([content.slice(cur), ""]);
    return out;
  }
  function translatable(text, dnt) {
    let t = String(text || "").replace(PH, " ");
    for (const term of dnt || []) t = t.replace(cached(`dntb:${term}`, () => new RegExp(`${B}${escRx(term)}${B}`, "gu")), " ");
    return LETTERS2.test(t);
  }
  function splitlines(text) { // str.splitlines(keepends=True)
    const s = String(text || ""), out = [];
    let start = 0;
    for (let i = 0; i < s.length; i++) {
      if ("\n\r\v\f\x1c\x1d\x1e\x85  ".includes(s[i])) {
        const end = s[i] === "\r" && s[i + 1] === "\n" ? i + 2 : i + 1;
        out.push(s.slice(start, end)); start = end; i = end - 1;
      }
    }
    if (start < s.length) out.push(s.slice(start));
    return out;
  }
  function segmentText(text, dnt, split = true, keepWhole = new Set()) {
    const tokens = [];
    const raw = (s) => { if (s) { const last = tokens[tokens.length - 1]; if (last && last.t === "raw") last.text += s; else tokens.push({ t: "raw", text: s }); } };
    const content = (s) => {
      const lead = s.slice(0, s.length - s.replace(/^\s+/, "").length), trail = s.slice(s.replace(/\s+$/, "").length), core = s.trim();
      raw(lead);
      if (core) {
        const whole = !split || keepWhole.has(normWs(core));
        for (const [sent, sep] of whole ? [[core, ""]] : splitSentences(core)) {
          if (translatable(sent, dnt)) tokens.push({ t: "seg", text: sent }); else raw(sent);
          raw(sep);
        }
      }
      raw(trail);
    };
    let inCode = false;
    for (const line of splitlines(text)) {
      const body = line.replace(/[\r\n]+$/, ""), nl = line.slice(body.length);
      if (body.trim().startsWith("```")) { inCode = !inCode; raw(line); continue; }
      if (inCode || !body.trim()) { raw(line); continue; }
      if (body.replace(/^\s+/, "").startsWith("|")) {
        for (const part of body.split(/(\|)/)) { if (part === "|" || /^\s*:?-{3,}:?\s*$/.test(part) || !part.trim()) raw(part); else content(part); }
        raw(nl);
        continue;
      }
      const prefix = body.match(MD_PREFIX)[1];
      raw(prefix); content(body.slice(prefix.length)); raw(nl);
    }
    return tokens;
  }
  const assembleDocument = (tokens, translations) => { let k = 0; return tokens.map((t) => (t.t === "seg" ? translations[k++] : t.text)).join(""); };

  /* ------------------------------------------------------------- pure helpers: translation memory (TMX 1.4 via DOMParser) */
  function parseXml(xml) {
    const doc = new DOMParser().parseFromString(String(xml), "application/xml");
    const err = doc.getElementsByTagName("parsererror")[0];
    if (err) throw new Error(normWs(err.textContent).slice(0, 160) || "not well-formed");
    return doc.documentElement;
  }
  const kids = (el, name) => [...(el ? el.children : [])].filter((c) => c.localName === name);
  function loadTmx(xml) { // -> [srclang, [{id, langs: Map(lang_lower -> text)}]]
    const root = parseXml(xml);
    const header = kids(root, "header")[0];
    const srclang = (header && header.getAttribute("srclang")) || "en-US";
    const units = [];
    [...root.getElementsByTagName("tu")].forEach((tu, i) => {
      const langs = new Map();
      for (const tuv of kids(tu, "tuv")) {
        const lang = (tuv.getAttributeNS(XML_NS, "lang") || tuv.getAttribute("xml:lang") || tuv.getAttribute("lang") || "").trim();
        const seg = kids(tuv, "seg")[0];
        if (lang && seg) langs.set(lang.toLowerCase(), normWs(seg.textContent));
      }
      if (langs.size >= 2) units.push({ id: tu.getAttribute("tuid") || `tu-${i + 1}`, langs });
    });
    return [srclang, units];
  }
  function tmxDnt(xml) { // <prop type="x-do-not-translate">a; b</prop> in the header
    const header = kids(parseXml(xml), "header")[0];
    const out = [];
    for (const prop of kids(header, "prop")) if (["x-do-not-translate", "x-dnt"].includes((prop.getAttribute("type") || "").toLowerCase()))
      out.push(...(prop.textContent || "").split(/[;|]/).map((t) => t.trim()).filter(Boolean));
    return out;
  }
  function pick(langs, locale) {
    const loc = locale.toLowerCase();
    if (langs.has(loc)) return langs.get(loc);
    const base = loc.split("-")[0];
    for (const [k, v] of langs) if (k.split("-")[0] === base) return v;
    return null;
  }
  const tmPairs = (units, src, tgt) => units.map((u) => [pick(u.langs, src), pick(u.langs, tgt)]).filter(([s, t]) => s && t);
  function tmLookup(segment, pairs, threshold) { // ['exact'|'fuzzy'|'none', tm_source, tm_target, score]
    const key = normWs(segment);
    for (const [s, t] of pairs) if (normWs(s) === key) return ["exact", s, t, 1.0];
    const a = cps(canonical(segment));
    let best = ["none", null, null, 0.0];
    for (const [s, t] of pairs) {
      const b = cps(canonical(s));
      if (!a.length || !b.length || Math.min(a.length, b.length) / Math.max(a.length, b.length) < threshold) continue;
      const sm = new SequenceMatcher(a, b);
      if (sm.realQuickRatio() < threshold || sm.quickRatio() < threshold) continue;
      const r = sm.ratio();
      if (r >= threshold && r > best[3]) best = ["fuzzy", s, t, pyRound(r, 4)];
    }
    return best;
  }
  function adaptFuzzy(segment, tmSource, tmTarget, srcFmt, tgtFmt) { // numbers / placeholders-only differences
    const a = String(segment).match(TOK) || [], b = String(tmSource).match(TOK) || [];
    let out = tmTarget;
    for (const [op, i1, i2, j1, j2] of new SequenceMatcher(b, a).opcodes()) {
      if (op === "equal") continue;
      if (op !== "replace" || i2 - i1 !== j2 - j1) return null;
      for (let k = 0; k < i2 - i1; k++) {
        const old = b[i1 + k], nw = a[j1 + k];
        if (PH_FULL.test(old) && PH_FULL.test(nw)) {
          if (!out.includes(old)) return null;
          out = out.replace(old, () => nw);
          continue;
        }
        const ov = parseNumber(old, srcFmt), nv = parseNumber(nw, srcFmt);
        if (ov === null || nv === null || !/^\p{Nd}/u.test(old) || !/^\p{Nd}/u.test(nw)) return null;
        let hit = null;
        for (const m of out.matchAll(numberRx(tgtFmt))) if (parseNumber(m[0], tgtFmt) === ov) { hit = m; break; }
        if (!hit) return null;
        const sd = srcFmt.decimal ?? ".";
        const decimals = nw.includes(sd) ? plen(nw.split(sd)[1]) : 0;
        out = out.slice(0, hit.index) + formatNumber(nv, decimals, nw.includes(srcFmt.thousands ?? ","), tgtFmt) + out.slice(hit.index + hit[0].length);
      }
    }
    return out;
  }
  const xmlEsc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); // xml.sax.saxutils.escape
  function buildTmx(units, srclang, tool = "Coforge CAF content_translation") {
    const stamp = new Date().toISOString().replace(/[-:]/g, "").replace(/\.\d+Z$/, "Z");
    const lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<tmx version="1.4">',
      `  <header creationtool="${xmlEsc(tool)}" creationtoolversion="1.0" datatype="plaintext" segtype="sentence" adminlang="en-US" srclang="${xmlEsc(srclang)}" o-tmf="caf" creationdate="${stamp}"/>`, "  <body>"];
    for (const u of units) lines.push(`    <tu tuid="${xmlEsc(u.id)}" creationdate="${stamp}">`, `      <prop type="x-origin">${xmlEsc(u.origin)}</prop>`, '      <prop type="x-status">unreviewed</prop>',
      `      <tuv xml:lang="${xmlEsc(srclang)}"><seg>${xmlEsc(u.source)}</seg></tuv>`, `      <tuv xml:lang="${xmlEsc(u.locale)}"><seg>${xmlEsc(u.target)}</seg></tuv>`, "    </tu>");
    lines.push("  </body>", "</tmx>");
    return lines.join("\n") + "\n";
  }

  /* ------------------------------------------------------------- pure helpers: QA */
  function placeholders(text) {
    return counter([...String(text || "").matchAll(PH)].map((m) => {
      const t = m[0];
      if (t.startsWith("http")) return t.replace(/[.,;:!?]+$/, "");
      if (t.startsWith("<")) return t.replace(/\s(alt|title|aria-label|placeholder)="[^"]*"/g, ' $1=""');
      return t.replace(/\s+/g, "");
    }));
  }
  const termRx = (term, plural) => cached(`term:${plural}:${term}`, () => new RegExp(`(?<![${W}])${escRx(term)}${plural ? `(?:s|es)?${B}` : ""}`, "iu"));
  const fold = (text) => String(text || "").normalize("NFKD").replace(/\p{Mn}/gu, "").toLowerCase(); // accent- and case-insensitive
  const wordList = (text) => (String(text || "").replace(PH, " ").match(LETTERS2_G) || []).map((w) => w.toLowerCase());
  const dateList = (c) => `[${[...c.keys()].map((k) => k.split(",").map(Number)).sort((x, y) => x[0] - y[0] || x[1] - y[1] || x[2] - y[2]).map((t) => `(${t.join(", ")})`).join(", ")}]`;
  function qaSegment(source, target, { srcFmt, tgtFmt, glossary, dnt, maxChars = null }) { // -> [[rule, message]]
    source = source || ""; target = target || "";
    const issues = [];
    if (target.startsWith("[offline:")) issues.push(["offline", "offline placeholder - no translation engine configured"]);
    const sp = placeholders(source), tp = placeholders(target);
    if (!cEq(sp, tp)) {
      const missing = cMinus(sp, tp), extra = cMinus(tp, sp);
      issues.push(["placeholders", "placeholders changed" + (missing.length ? `; missing ${pyList(missing)}` : "") + (extra.length ? `; unexpected ${pyList(extra)}` : "")]);
    }
    const sIdi = srcFmt.number_idioms || [], tIdi = tgtFmt.number_idioms || [];
    const idioms = [...sIdi, ...tIdi.filter((i) => !sIdi.includes(i))];
    const sd = counter(dateValues(source, srcFmt), (t) => t.join(",")), td = counter(dateValues(target, tgtFmt), (t) => t.join(","));
    if (!cEq(sd, td)) issues.push(["numbers", `dates differ (expected ${tgtFmt.date ?? "None"}): source ${dateList(sd)} vs target ${dateList(td)}`]);
    const sv = counter(numberValues(source, srcFmt, idioms)), tv = counter(numberValues(target, tgtFmt, idioms));
    if (!cEq(sv, tv)) {
      const missing = cMinus(sv, tv), extra = cMinus(tv, sv);
      issues.push(["numbers", "numbers differ" + (missing.length ? `; missing ${pyList(missing.map(fmtNum))}` : "") + (extra.length ? `; unexpected ${pyList(extra.map(fmtNum))}` : "")]);
    }
    for (const term of dnt) if (cached(`dnt:${term}`, () => new RegExp(`(?<![${W}])${escRx(term)}(?![${W}])`, "u")).test(source) && !target.includes(term)) issues.push(["dnt", `do-not-translate term '${term}' altered or missing`]);
    const folded = fold(target);
    for (const [s, t] of Object.entries(glossary).sort((x, y) => plen(y[0]) - plen(x[0]))) if (termRx(s, true).test(source) && !termRx(fold(t), false).test(folded)) issues.push(["glossary", `'${s}' should be rendered as '${t}'`]);
    if (translatable(source, dnt) && arrEq(wordList(target.replace(/^\[offline:[^\]]+\]\s*/, "")), wordList(source))) issues.push(["untranslated", "segment left untranslated"]);
    const ratio = Number(tgtFmt.max_expansion ?? 1.35);
    if (plen(target) > plen(source) * ratio + 10) issues.push(["length", `${plen(target)} chars vs ${plen(source)} in source (max expansion ${pyPct0(ratio)})`]);
    if (maxChars && plen(target) > maxChars) issues.push(["max_chars", `${plen(target)} chars exceeds the ${maxChars}-char limit`]);
    return issues;
  }
  const qaPassed = (issues) => !issues.some(([rule]) => (SEVERITY[rule] || "warn") === "fail");
  const hardIssues = (issues) => issues.filter(([rule]) => SEVERITY[rule] === "fail").length;

  /* ------------------------------------------------------------- local ingestion for `source` (twin of the ingestion connector) */
  function htmlToText(html) { // readable Markdown-ish blocks; stand-in for trafilatura (no figures, <br> breaks, FAQ summaries as headings)
    const doc = new DOMParser().parseFromString(String(html || ""), "text/html");
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
    return out.join("\n\n");
  }
  const datasetName = (p) => String(p || "").trim().replace(/^\.?\//, "").replace(/^sample_data\//, "").replace(/\.[A-Za-z0-9]+$/, "");
  async function loadSource(ctx, source) { // {text} | {unavailable} | {error}
    const s = String(source).trim();
    if (/^https?:\/\//i.test(s)) {
      if (!ctx.isDemoUrl(s)) return { unavailable: `the hosted console cannot fetch ${s} - paste the text into the text field, or run the Python service` };
      const page = ctx.page({ url: s });
      if (page.status !== 200) return { error: `status ${page.status} for ${s}` };
      return { text: htmlToText(page.html) };
    }
    const name = datasetName(s);
    if (ctx.hasData(name)) { const raw = await ctx.dataRaw(name); return { text: /\.html?$/i.test(s) ? htmlToText(raw) : raw }; }
    return { unavailable: `'${s}' is not embedded in the hosted console - paste its text into the text field, or run the Python service` };
  }

  /* ------------------------------------------------------------- the agent */
  CAF.agent("content_translation", {
    async run(input, ctx) {
      const aborted = () => !!(ctx.signal && ctx.signal.aborted);
      // -- 1. prepare: assets, source, segmentation ------------------------------------------------------
      const gl = ctx.skill("translation_glossary", {}), formats = ctx.skill("locale_formats", {});
      const dnt = [...(gl.do_not_translate || [])];
      const reasons = [], warnings = [];
      let units = [];
      const tmPath = input.tm_path || gl.translation_memory;
      if (tmPath) {
        const name = datasetName(tmPath);
        if (ctx.hasData(name)) {
          try {
            const rawTm = await ctx.dataRaw(name);
            [, units] = loadTmx(rawTm);
            dnt.push(...tmxDnt(rawTm).filter((t) => !dnt.includes(t)));
          } catch (e) { reasons.push(`translation memory unreadable (${(e && e.message) || e}): no TM leverage`); }
        } else reasons.push(`translation memory not found (${tmPath} is not embedded in the hosted console): no TM leverage`);
      } else reasons.push("no translation memory configured: no TM leverage");
      dnt.sort((a, b) => plen(b) - plen(a));

      let mode = "text", text = input.text;
      if ((input.segments || []).length) mode = "segments";
      else if (!text && input.source) {
        const r = await loadSource(ctx, input.source);
        if (r.error) throw new Error(`Could not ingest the source: ${r.error}`);
        if (r.unavailable) { // no network in the hosted console: degrade with an empty, valid result
          ctx.degraded(r.unavailable);
          ctx.check({ rule: "ingest.source", status: "fail", severity: "high", target: String(input.source), message: `Nothing translated: ${r.unavailable}.` });
          const src0 = input.source_locale || gl.source_locale || "en-US";
          return { output: { source_locale: src0, target_locales: [], segments: [], documents: {}, qa_summary: { overall: { segments: 0, failed: 0, passed: 0 } }, tm_file: null, glossary_version: String(gl.version ?? "") },
            summary: "0 segment(s): source not reachable from the hosted console" };
        }
        text = r.text;
      } else if (!text) {
        const email = ctx.upstreamOutput("email_creation", "webinar_followup_email") || {};
        const first = (truthy(email.emails) ? email.emails[0] : null) || (email.segments || []).map((s) => s && s.email).find(Boolean);
        if (first) {
          text = [first.subject, first.preheader, first.body_markdown, first.cta_text].filter(Boolean).join("\n\n");
          warnings.push("No content given - translating the upstream email.");
        } else {
          text = ctx.upstreamText(20000);
          if (text) warnings.push("No content given - translating the upstream agents' output.");
        }
      }
      let tokens;
      if (mode === "segments") {
        tokens = [];
        input.segments.forEach((s, i) => { if (i) tokens.push({ t: "raw", text: "\n" }); tokens.push({ t: translatable(s, dnt) ? "seg" : "raw", text: s }); });
      } else {
        if (!String(text || "").trim()) throw new Error("Nothing to translate: provide text, segments or source.");
        const srcLoc = input.source_locale || gl.source_locale || "en-US";
        const whole = new Set(units.map((u) => normWs(pick(u.langs, srcLoc) || "")).filter(Boolean));
        tokens = segmentText(text, dnt, true, new Set([...whole].filter((w) => splitSentences(w).length > 1)));
      }
      const segs = tokens.filter((t) => t.t === "seg").map((t) => t.text);
      if (!segs.length) throw new Error("No translatable segments found (only placeholders, numbers or do-not-translate terms).");
      const src = input.source_locale || gl.source_locale || "en-US";
      const requested = Array.isArray(input.target_locales) && input.target_locales.length ? input.target_locales : ["fr-FR"];
      const locales = [...new Set(requested)].filter((l) => l.toLowerCase() !== src.toLowerCase());
      if (locales.length !== requested.length) warnings.push("Target locales equal to the source locale or duplicated were dropped.");
      if (!locales.length) throw new Error("No target locale differs from the source locale.");
      const known = new Set(Object.keys(formats.locales || {}).map((k) => k.toLowerCase()));
      for (const loc of locales) {
        if (!known.has(loc.toLowerCase())) ctx.check({ rule: "locale.formats", status: "warn", severity: "low", target: loc, message: `No formatting rules for ${loc} in locale_formats.yaml; language/default rules used.` });
        if (!truthy(glossaryFor(gl, loc))) ctx.check({ rule: "glossary.coverage", status: "warn", severity: "medium", target: loc, message: `No glossary for ${loc}; terminology is not enforced for this locale.` });
      }
      ctx.check({ rule: "segmentation", status: "info", severity: "low", message: `${segs.length} translatable segment(s) (${mode} mode) into ${locales.join(", ")}.` });
      for (const w of [...warnings, ...reasons.map((r) => r[0].toUpperCase() + r.slice(1) + ".")]) ctx.warn(w);
      for (const r of reasons) ctx.degraded(r);
      ctx.progress(`${segs.length} segment(s) into ${locales.join(", ")}`);

      // -- 2. translation memory: exact reuse, fuzzy adaptation / suggestion -----------------------------
      const threshold = Number(gl.fuzzy_threshold ?? 0.85);
      const srcFmt = localeFormat(formats, src);
      const rowsAll = {};
      for (const loc of locales) {
        const pairs = tmPairs(units, src, loc), tgtFmt = localeFormat(formats, loc);
        const rows = segs.map((seg, i) => {
          const [kind, tmS, tmT, score] = pairs.length ? tmLookup(seg, pairs, threshold) : ["none", null, null, 0.0];
          const row = { id: i, source: seg, target: null, origin: null, tm_score: score || null, tm_suggestion: null, tm_source: null, mt_failed: false };
          if (kind === "exact") Object.assign(row, { target: tmT, origin: "tm_exact" });
          else if (kind === "fuzzy") {
            const adapted = adaptFuzzy(seg, tmS, tmT, srcFmt, tgtFmt);
            Object.assign(row, { tm_suggestion: tmT, tm_source: tmS });
            if (adapted !== null) Object.assign(row, { target: adapted, origin: "tm_fuzzy" });
          }
          return row;
        });
        rowsAll[loc] = rows;
        const n = rows.length, ex = rows.filter((r) => r.origin === "tm_exact").length, fz = rows.filter((r) => r.tm_suggestion).length;
        ctx.check({ rule: "tm.leverage", status: "info", severity: "low", target: loc, data: { exact: ex, fuzzy: fz, segments: n, tm_units: pairs.length },
          message: `${loc}: ${ex} exact and ${fz} fuzzy (>= ${pyPct0(threshold)}) TM match(es) out of ${n} segment(s) from ${pairs.length} TM unit(s); leverage ${pyPct0((ex + fz) / Math.max(1, n))}.` });
      }

      // -- 3. machine translation (translate.j2 on Claude; deterministic placeholders offline) -------------
      const live = ctx.llmLive;
      const glossaryLines = (g) => Object.entries(g).map(([k, v]) => `${k} => ${v}`);
      const translate = async (texts, loc, glossary) => {
        if (!live) return texts.map((s) => `[offline:${loc}] ${s}`); // LlmTranslation offline stand-in
        const res = await ctx.generate("translate", { source_locale: src, target_locale: loc, tone: input.tone, dnt, glossary: glossaryLines(glossary), count: texts.length, segments: texts });
        const outs = (res && res.translations) || [];
        if (outs.length !== texts.length) throw new Error(`engine returned ${outs.length} translations for ${texts.length} segments`);
        return outs;
      };
      const todoBy = locales.map((loc) => [loc, rowsAll[loc].filter((r) => r.target === null)]);
      if (todoBy.some(([, todo]) => todo.length)) {
        for (const [loc, todo] of todoBy) {
          const glossary = glossaryFor(gl, loc);
          for (let b = 0; b < todo.length; b += MT_BATCH) {
            const batch = todo.slice(b, b + MT_BATCH), texts = batch.map((r) => r.source);
            ctx.progress(`${loc}: translating ${texts.length} segment(s)`);
            let outs;
            try { outs = await translate(texts, loc, glossary); }
            catch (e) {
              if (aborted()) throw e;
              try { outs = await translate(texts, loc, glossary); }
              catch (e2) {
                if (aborted()) throw e2;
                ctx.warn(`${loc}: batch translation failed (${errName(e2)}: ${(e2 && e2.message) || e2}); retrying segment by segment.`);
                outs = [];
                for (const t of texts) { try { outs.push((await translate([t], loc, glossary))[0]); } catch (e3) { if (aborted()) throw e3; outs.push(null); } }
              }
            }
            batch.forEach((r, k) => { const o = outs[k]; if (o === null || o === undefined) Object.assign(r, { target: r.source, origin: "mt", mt_failed: true }); else Object.assign(r, { target: o, origin: "mt" }); });
          }
        }
        if (!live) ctx.degraded("offline model: machine translation returned placeholders (segments marked [offline:<locale>])");
      }

      // -- 4. model post-editing (tone / cultural adaptation; fuzzy TM adaptation) -------------------------
      if (input.post_edit === false || !live) { if (!live) ctx.degraded("offline model: no post-editing pass"); }
      else {
        for (const loc of locales) {
          const tgtFmt = localeFormat(formats, loc), glossary = glossaryFor(gl, loc);
          const todo = rowsAll[loc].filter((r) => r.origin === "mt" && !r.mt_failed);
          let reverted = 0;
          for (let b = 0; b < todo.length; b += POST_EDIT_BATCH) {
            const batch = todo.slice(b, b + POST_EDIT_BATCH);
            ctx.progress(`${loc}: post-editing ${batch.length} segment(s)`);
            const items = batch.map((r) => ({ id: r.id, source: r.source, draft: r.target, tm_source: r.tm_source, tm_target: r.tm_suggestion }));
            const edited = new Map();
            try {
              const res = await ctx.generate("post_edit", { items, source_locale: src, target_locale: loc, tone: input.tone, glossary: glossaryLines(glossary), dnt,
                decimal: tgtFmt.decimal, thousands: tgtFmt.thousands === undefined ? "None" : pyStr(tgtFmt.thousands), date: tgtFmt.date });
              for (const e of (res && res.segments) || []) edited.set(e.id, e.target);
            } catch (e) { if (aborted()) throw e; ctx.warn(`${loc}: post-editing failed (${errName(e)}); machine translation kept.`); }
            for (const r of batch) {
              const nw = normWs(edited.get(r.id) || "");
              if (!nw) continue;
              const before = qaSegment(r.source, r.target, { srcFmt, tgtFmt, glossary, dnt });
              const after = qaSegment(r.source, nw, { srcFmt, tgtFmt, glossary, dnt });
              if (hardIssues(after) > hardIssues(before)) { reverted++; continue; } // the editor broke placeholders / numbers / DNT: keep the MT draft
              const origin = r.tm_suggestion && similarity(nw, r.tm_suggestion) > similarity(nw, r.target) ? "tm_fuzzy" : "mt_post_edited";
              Object.assign(r, { target: nw, origin });
            }
          }
          if (reverted) ctx.check({ rule: "post_edit.reverted", status: "warn", severity: "medium", target: loc, message: `${loc}: ${reverted} post-edit(s) reverted because they broke placeholders, numbers or DNT terms.` });
        }
      }

      // -- 5. locale formatting + deterministic QA ---------------------------------------------------------
      const qaSummary = {};
      for (const loc of locales) {
        const tgtFmt = localeFormat(formats, loc), glossary = glossaryFor(gl, loc);
        let fixedN = 0, fixedD = 0;
        for (const r of rowsAll[loc]) {
          if (r.target === null) Object.assign(r, { target: r.source, origin: "mt", mt_failed: true });
          let [target, n1] = localiseNumbers(r.target, r.source, srcFmt, tgtFmt);
          let n2; [target, n2] = localiseDates(target, r.source, srcFmt, tgtFmt);
          fixedN += n1; fixedD += n2;
          const issues = qaSegment(r.source, target, { srcFmt, tgtFmt, glossary, dnt, maxChars: input.max_chars });
          if (r.mt_failed) issues.unshift(["mt_failed", "machine translation failed - source text kept"]);
          Object.assign(r, { target, qa: { passed: qaPassed(issues), issues: issues.map(([rule, msg]) => `${rule}: ${msg}`) }, rules: issues.map(([rule]) => rule) });
        }
        const out = rowsAll[loc];
        const byRule = counter(out.flatMap((r) => r.rules)), byOrigin = counter(out.map((r) => r.origin));
        const passed = out.filter((r) => r.qa.passed).length;
        qaSummary[loc] = { segments: out.length, passed, failed: out.length - passed, issues: Object.fromEntries(byRule), origins: Object.fromEntries(byOrigin), numbers_localised: fixedN, dates_localised: fixedD };
        for (const rule of ["placeholders", "numbers", "dnt", "glossary", "untranslated", "length", "max_chars", "mt_failed"]) {
          const n = byRule.get(rule) || 0, sev = SEVERITY[rule] || "warn";
          const st = !n ? "pass" : byRule.get("offline") && rule === "untranslated" ? "warn" : sev;
          const label = rule.replace(/_/g, " ");
          ctx.check({ rule: `qa.${rule}`, status: st, severity: st === "fail" ? "high" : st === "warn" ? "medium" : "low", target: loc,
            message: n ? `${loc}: ${n} segment(s) with ${label} issues.` : `${loc}: ${label} check passed on all ${out.length} segment(s).` });
        }
        if (fixedN || fixedD) ctx.check({ rule: "locale.formatting", status: "info", severity: "low", target: loc, message: `${loc}: re-formatted ${fixedN} number(s) and ${fixedD} date(s) to the locale's conventions.` });
      }
      const perLoc = Object.values(qaSummary);
      qaSummary.overall = { segments: perLoc.reduce((s, v) => s + v.segments, 0), failed: perLoc.reduce((s, v) => s + v.failed, 0), passed: perLoc.reduce((s, v) => s + v.passed, 0) };

      // -- 6. documents, QA report, TM write-back, gated CMS publish ----------------------------------------
      const documents = {}, segments = [], newUnits = [];
      const ext = mode === "text" ? "md" : "txt";
      for (const loc of locales) {
        const rows = rowsAll[loc];
        documents[loc] = assembleDocument(tokens, rows.map((r) => r.target));
        for (const r of rows) {
          segments.push({ id: r.id, source: r.source, target: r.target, locale: loc, origin: r.origin, qa: r.qa, tm_score: r.tm_score, tm_suggestion: r.tm_suggestion });
          if (r.origin !== "tm_exact" && r.qa.passed && !r.target.startsWith("[offline:") && !r.mt_failed) newUnits.push({ id: `${ctx.runId}-${loc}-${r.id}`, source: r.source, target: r.target, locale: loc, origin: r.origin });
        }
        ctx.artifact({ name: `translated-${loc}.${ext}`, mime: ext === "md" ? "text/markdown" : "text/plain", content: documents[loc], description: `Translation (${loc})` });
      }
      const qaRows = segments.map((s) => ({ locale: s.locale, id: s.id, origin: s.origin, qa_passed: s.qa.passed ? "True" : "False", issues: s.qa.issues.join(" | "),
        tm_score: s.tm_score ? pyFloatRepr(s.tm_score) : "", source: s.source, target: s.target }));
      ctx.artifact({ name: "qa_report.csv", mime: "text/csv", description: "Segment-level translation QA", content: CAF.lib.csv.stringify(qaRows, ["locale", "id", "origin", "qa_passed", "issues", "tm_score", "source", "target"]) });
      let tmFile = null;
      if (newUnits.length) {
        tmFile = ctx.artifact({ name: `tm-${ctx.runId}.tmx`, mime: "application/x-tmx+xml", description: "New translation units for review (TMX 1.4)", content: buildTmx(newUnits, src) }).name;
        ctx.check({ rule: "tm.write", status: "info", severity: "low", message: `${newUnits.length} new unit(s) written to ${tmFile} (status: unreviewed) - download it for review; the hosted console keeps no TM store.` });
      } else ctx.check({ rule: "tm.write", status: "info", severity: "low", message: "No new TM units" + (!live ? " (offline placeholders are never stored)." : ".") });
      if (input.publish) {
        const title = input.title || cps(normWs(segs[0])).slice(0, 80).join("");
        const slug = input.slug || slugify(title, 60);
        for (const [loc, doc] of Object.entries(documents)) {
          const failed = (qaSummary[loc] || {}).failed || 0;
          ctx.action({ id: `cms-${loc}`, kind: "cms.publish", connector: "cms", method: "publish",
            payload: { title, body: doc, slug, content_type: "page", status: "draft", locale: loc, body_format: mode === "text" ? "markdown" : "text",
              fields: { source_locale: src, qa_failed_segments: failed, run_id: ctx.runId } },
            summary: `Publish the ${loc} translation of '${cps(title).slice(0, 50).join("")}' to the CMS as a draft` + (failed ? ` (${failed} segment(s) failed QA - review first)` : "") });
        }
      }
      const o = qaSummary.overall, origins = counter(segments.map((s) => s.origin));
      const summary = `${segs.length} segment(s) into ${Object.keys(documents).join(", ")}: ` + [...origins].sort((x, y) => (x[0] < y[0] ? -1 : x[0] > y[0] ? 1 : 0)).map(([k, v]) => `${k}=${v}`).join(", ") + `; QA ${o.passed}/${o.segments} passed`;
      const view = [
        { type: "kv", title: "Overview", data: { segments: segs.length, locales: locales.join(", "), origins: Object.fromEntries(origins), qa_passed: `${o.passed}/${o.segments}`, glossary_version: String(gl.version ?? ""), tm_units: units.length, engine: live ? "Claude (translate.j2)" : "offline placeholders" } },
        ...locales.map((loc) => (mode === "text" ? { type: "markdown", title: `Translation (${loc})`, note: `${qaSummary[loc].passed}/${qaSummary[loc].segments} segment(s) passed QA`, markdown: documents[loc] }
          : { type: "code", title: `Translation (${loc})`, code: documents[loc] })),
        { type: "table", title: "Segments", columns: ["locale", "id", "origin", "qa", "tm_score", "source", "target", "issues"],
          rows: segments.slice(0, 200).map((s) => ({ locale: s.locale, id: s.id, origin: s.origin, qa: s.qa.passed ? "pass" : "fail", tm_score: s.tm_score, source: s.source, target: s.target, issues: s.qa.issues.join(" | ") })) },
      ];
      return {
        output: { source_locale: src, target_locales: Object.keys(documents), segments, documents, qa_summary: qaSummary, tm_file: tmFile, glossary_version: String(gl.version ?? "") },
        summary,
        view,
      };
    },
  });
})();
