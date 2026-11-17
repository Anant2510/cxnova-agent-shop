/* Experiment Ideation - browser twin of caf/agents/experimentation/experiment_ideation.py.
   inspect page (DOM) -> evidence (page, analytics, behaviour events, page-scoped upstream findings) ->
   heuristics from cro_heuristics.yaml (trigger priority, related supporting signals) -> model phrases ideas
   (unknown heuristic / evidence ids dropped) -> ICE / PIE in code -> backlog (deduplicated, page session).
   Also defines CAF.xp, the small kit shared by the seven experimentation twins. */
(() => {
  const XP = (CAF.xp = CAF.xp || {});
  const T = () => CAF.lib.text;
  const DEMO_URL = "https://www.meridian-mutual.example/products/home-insurance.html";

  /* ============================================================ shared experimentation kit */
  XP.STORE_NOTE = "in-memory store for this page session - the durable store (SQLite / registry) needs the Python service";
  XP.store = () => (CAF.__xpStore = CAF.__xpStore || { experiments: new Map(), backlog: new Map(), learnings: new Map() });
  /** Model call with the same honest fallbacks as the Python `_generate_or_none`: offline -> render the template only. */
  XP.generateOrNone = async (ctx, template, vars, offlineReason) => {
    if (!ctx.llmLive) {
      try { CAF.prompts.render(ctx.agent.id, template, vars); } catch (e) { ctx.log(`template ${template} not rendered: ${e.message}`); }
      ctx.degraded(offlineReason);
      return null;
    }
    try { return await ctx.generate(template, vars); } catch (e) {
      ctx.warn(`Model call '${template}' failed (${e.message}); the deterministic path was used.`);
      ctx.degraded(`model call failed - ${offlineReason.replace(/^no language model configured: /, "")}`);
      return null;
    }
  };
  XP.round = (x, d = 4) => (x === null || x === undefined || !Number.isFinite(x) ? x : Math.round(x * 10 ** d) / 10 ** d);
  /** Python round(): ties go to the even neighbour. */
  XP.roundHalfEven = (x) => { const f = Math.floor(x), diff = x - f; return diff > 0.5 ? f + 1 : diff < 0.5 ? f : f % 2 === 0 ? f : f + 1; };
  /** Python '%g' (6 significant digits, no trailing zeros). */
  XP.g = (x) => (x === null || x === undefined ? "None" : String(Number(Number(x).toPrecision(6))));
  /** Python fixed-point formatting: round-half-even on the exact binary value (JS toFixed rounds exact ties up). */
  XP.fixed = (x, d = 0) => {
    if (!Number.isFinite(x)) return String(x);
    const exact = Math.abs(x).toFixed(Math.min(100, d + 60));
    const [ip, fp = ""] = exact.split(".");
    const rest = fp.slice(d);
    let digits = ip + fp.slice(0, d);
    const up = rest[0] > "5" || (rest[0] === "5" && (/[1-9]/.test(rest.slice(1)) || Number(digits[digits.length - 1] || 0) % 2 === 1));
    if (up) { const arr = digits.split("").map(Number); let i = arr.length - 1; while (i >= 0) { if (arr[i] < 9) { arr[i]++; break; } arr[i] = 0; i--; } digits = (i < 0 ? "1" : "") + arr.join(""); }
    const intPart = d ? digits.slice(0, digits.length - d) || "0" : digits;
    const out = d ? `${intPart}.${digits.slice(-d)}` : intPart;
    return (x < 0 || Object.is(x, -0)) && /[1-9]/.test(out) ? `-${out}` : x < 0 || Object.is(x, -0) ? `-${out}` : out;
  };
  /** Python '{x:,.Nf}' / '{x:+.N%}' style formatting built on XP.fixed. */
  XP.fmt = (x, d = 0, { comma = true, pct = false, sign = false } = {}) => {
    if (x === null || x === undefined || Number.isNaN(x)) return pct ? "n/a" : "nan";
    let s = XP.fixed(pct ? x * 100 : x, d);
    const neg = s.startsWith("-");
    if (neg) s = s.slice(1);
    if (comma) { const [i, f] = s.split("."); s = i.replace(/\B(?=(\d{3})+(?!\d))/g, ",") + (f !== undefined ? `.${f}` : ""); }
    return (neg ? "-" : sign ? "+" : "") + s + (pct ? "%" : "");
  };
  XP.pct = (x, d = 0) => (x === null || x === undefined || !Number.isFinite(x) ? "n/a" : `${(x * 100).toFixed(d)}%`);
  XP.int = (n) => U.fmtNum(n, 0);
  /* numeric kit: double-precision normal distribution (Hart 5666 via West 2005), Newton-refined quantile, Brent root finder.
     CAF.lib.stats.normCdf uses a 1.5e-7 erf approximation - too coarse for alpha-spending tails, hence these local helpers. */
  XP.ncdf = (x) => {
    const ax = Math.abs(x);
    let c = 0;
    if (ax <= 37) {
      const e = Math.exp(-ax * ax / 2);
      if (ax < 7.07106781186547) {
        let b = 3.52624965998911e-02 * ax + 0.700383064443688;
        b = b * ax + 6.37396220353165; b = b * ax + 33.912866078383; b = b * ax + 112.079291497871; b = b * ax + 221.213596169931; b = b * ax + 220.206867912376;
        c = e * b;
        b = 8.83883476483184e-02 * ax + 1.75566716318264;
        b = b * ax + 16.064177579207; b = b * ax + 86.7807322029461; b = b * ax + 296.564248779674; b = b * ax + 637.333633378831; b = b * ax + 793.826512519948; b = b * ax + 440.413735824752;
        c /= b;
      } else {
        let b = ax + 0.65; b = ax + 4 / b; b = ax + 3 / b; b = ax + 2 / b; b = ax + 1 / b;
        c = e / b / 2.506628274631;
      }
    }
    return x > 0 ? 1 - c : c;
  };
  XP.nsf = (x) => XP.ncdf(-x);
  XP.npdf = (x) => Math.exp(-0.5 * x * x) / 2.5066282746310002;
  XP.nppf = (p) => {
    let x = CAF.lib.stats.normPpf(p);
    if (!Number.isFinite(x)) return x;
    for (let i = 0; i < 3; i++) { const d = XP.npdf(x); if (!d) break; x -= (XP.ncdf(x) - p) / d; }
    return x;
  };
  /** Brent's method (scipy.optimize.brentq semantics: f(a) and f(b) must bracket a root). */
  XP.brent = (f, a, b, xtol = 2e-12, maxIter = 200) => {
    let fa = f(a), fb = f(b);
    if (fa === 0) return a;
    if (fb === 0) return b;
    if (fa * fb > 0) throw new Error("brent: root not bracketed");
    let c = a, fc = fa, d = b - a, e = d;
    for (let i = 0; i < maxIter; i++) {
      if (fb * fc > 0) { c = a; fc = fa; d = b - a; e = d; }
      if (Math.abs(fc) < Math.abs(fb)) { a = b; b = c; c = a; fa = fb; fb = fc; fc = fa; }
      const tol = 2 * Number.EPSILON * Math.abs(b) + 0.5 * xtol, m = 0.5 * (c - b);
      if (Math.abs(m) <= tol || fb === 0) return b;
      if (Math.abs(e) >= tol && Math.abs(fa) > Math.abs(fb)) {
        const s = fb / fa;
        let p, q;
        if (a === c) { p = 2 * m * s; q = 1 - s; }
        else { const qq = fa / fc, r = fb / fc; p = s * (2 * m * qq * (qq - r) - (b - a) * (r - 1)); q = (qq - 1) * (r - 1) * (s - 1); }
        if (p > 0) q = -q; else p = -p;
        if (2 * p < Math.min(3 * m * q - Math.abs(tol * q), Math.abs(e * q))) { e = d; d = p / q; } else { d = m; e = m; }
      } else { d = m; e = m; }
      a = b; fa = fb;
      b += Math.abs(d) > tol ? d : m > 0 ? tol : -tol;
      fb = f(b);
    }
    return b;
  };
  XP.lgamma = (x) => {
    const c = [0.99999999999980993, 676.5203681218851, -1259.1392167224028, 771.32342877765313, -176.61502916214059, 12.507343278686905, -0.13857109526572012, 9.9843695780195716e-6, 1.5056327351493116e-7];
    if (x < 0.5) return Math.log(Math.PI / Math.sin(Math.PI * x)) - XP.lgamma(1 - x);
    x -= 1;
    let a = c[0];
    const t = x + 7.5;
    for (let i = 1; i < 9; i++) a += c[i] / (x + i);
    return 0.5 * Math.log(2 * Math.PI) + (x + 0.5) * Math.log(t) - t + Math.log(a);
  };
  /** Upper regularised incomplete gamma Q(a, x), computed without the 1 - P cancellation that zeroes tiny p-values. */
  XP.gammaQ = (a, x) => {
    if (x <= 0) return 1;
    const lead = -x + a * Math.log(x) - XP.lgamma(a);
    if (x < a + 1) { let s = 1 / a, t = s; for (let n = 1; n < 1000; n++) { t *= x / (a + n); s += t; if (Math.abs(t) < Math.abs(s) * 1e-16) break; } return Math.max(0, 1 - s * Math.exp(lead)); }
    let b = x + 1 - a, c = 1e300, d = 1 / b, h = d;
    for (let i = 1; i < 1000; i++) { const an = -i * (i - a); b += 2; d = an * d + b; if (Math.abs(d) < 1e-300) d = 1e-300; c = b + an / c; if (Math.abs(c) < 1e-300) c = 1e-300; d = 1 / d; const del = d * c; h *= del; if (Math.abs(del - 1) < 1e-16) break; }
    return Math.exp(lead) * h;
  };
  /** scipy.stats.chi2.sf - exact through the normal tail for 1 dof, closed form for 2. */
  XP.chi2Sf = (x, df) => (!(x > 0) ? 1 : df === 1 ? 2 * XP.nsf(Math.sqrt(x)) : df === 2 ? Math.exp(-x / 2) : XP.gammaQ(df / 2, x / 2));
  /** Python '{x:.Ne}' (two-digit exponent). */
  XP.e = (x, d = 2) => (Number.isFinite(x) ? x.toExponential(d).replace(/e([+-])(\d)$/, "e$10$2") : String(x));
  /** Python date.fromisoformat(value[:10]) -> epoch day number, or null. */
  XP.day = (v) => { if (!v) return null; const m = String(v).slice(0, 10).match(/^(\d{4})-(\d{2})-(\d{2})$/); if (!m) return null; const t = Date.UTC(+m[1], +m[2] - 1, +m[3]); return Number.isFinite(t) ? Math.round(t / 864e5) : null; };
  /** json.dumps() with Python's default separators and ensure_ascii. */
  XP.pyJson = (v) => {
    const str = (s) => JSON.stringify(s).replace(/[\u007f-￿]/g, (c) => "\\u" + c.charCodeAt(0).toString(16).padStart(4, "0"));
    if (v === null || v === undefined) return "null";
    if (Array.isArray(v)) return "[" + v.map(XP.pyJson).join(", ") + "]";
    if (typeof v === "object") return "{" + Object.entries(v).map(([k, x]) => `${str(k)}: ${XP.pyJson(x)}`).join(", ") + "}";
    if (typeof v === "string") return str(v);
    if (typeof v === "boolean") return v ? "true" : "false";
    return Number.isInteger(v) ? String(v) : JSON.stringify(v);
  };
  /** Minimal block-style YAML emitter (safe subset; strings quoted whenever YAML could misread them). */
  XP.yaml = (value) => {
    const PLAIN = /^[A-Za-z_][A-Za-z0-9_ ./()-]*$/;
    const RESERVED = /^(true|false|yes|no|on|off|null|~|y|n)$/i;
    const scalar = (v) => {
      if (v === null || v === undefined) return "null";
      if (typeof v === "boolean") return v ? "true" : "false";
      if (typeof v === "number") return Number.isFinite(v) ? String(v) : v > 0 ? ".inf" : v < 0 ? "-.inf" : ".nan";
      const s = String(v);
      return PLAIN.test(s) && !RESERVED.test(s) && !/\s$/.test(s) && !s.includes(": ") && !s.includes(" #") ? s : JSON.stringify(s);
    };
    const emit = (v, indent) => {
      const pad = " ".repeat(indent);
      if (Array.isArray(v)) {
        if (!v.length) return " []";
        return "\n" + v.map((x) => {
          if (U.isObj(x) && Object.keys(x).length) { const inner = emit(x, indent + 2).replace(/^\n/, ""); return `${pad}- ${inner.slice(indent + 2)}`; }
          if (Array.isArray(x) && x.length) return `${pad}-${emit(x, indent + 2)}`;
          return `${pad}- ${U.isObj(x) ? "{}" : Array.isArray(x) ? "[]" : scalar(x)}`;
        }).join("\n");
      }
      if (U.isObj(v)) {
        if (!Object.keys(v).length) return " {}";
        return "\n" + Object.entries(v).map(([k, x]) => (U.isObj(x) || Array.isArray(x) ? `${pad}${scalar(k)}:${emit(x, indent + 2)}` : `${pad}${scalar(k)}: ${scalar(x)}`)).join("\n");
      }
      return " " + scalar(v);
    };
    return emit(value, 0).replace(/^\n/, "") + "\n";
  };
  /** ExperimentDefinition with its defaults (port of caf.connectors.experiments.ExperimentDefinition). */
  XP.definition = (d) => ({ status: "draft", platform: "internal", page_url: "", hypothesis: "", primary_metric: null, secondary_metrics: [], guardrail_metrics: [], variants: [],
    traffic_allocation: 100, start_date: null, end_date: null, planned_duration_days: null, baseline_rate: null, mde: null, expected_daily_visitors: null, qa_links: [],
    instrumentation: {}, owner: null, ...d, audience: { devices: [], geo: [], conditions: [], ...((d || {}).audience || {}) } });
  /** Registry = embedded registry.json plus plans written in this page session (session copies win, as in JsonExperiments). */
  XP.registry = async (ctx) => {
    let base = [];
    if (ctx.hasData("experiments/registry")) { const reg = await ctx.data("experiments/registry"); base = (reg && reg.experiments) || (Array.isArray(reg) ? reg : []); }
    const local = [...XP.store().experiments.values()];
    const ids = new Set(local.map((e) => e.id));
    return [...base.filter((e) => !ids.has(e.id)), ...local].map(XP.definition);
  };
  XP.clamp = (v, lo = 1, hi = 10) => XP.round(Math.min(hi, Math.max(lo, v)), 2);
  XP.normWs = (t) => String(t || "").replace(/\s+/g, " ").trim();
  /** Visible text with a space between text nodes (bs4 get_text(" ") semantics). */
  XP.text = (el, limit = 160) => {
    if (!el) return "";
    const parts = [];
    const walk = (n) => { for (const c of n.childNodes) { if (c.nodeType === 3) parts.push(c.nodeValue); else if (c.nodeType === 1 && !/^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE)$/.test(c.tagName)) walk(c); } };
    if (el.nodeType === 3) parts.push(el.nodeValue); else walk(el);
    return T().truncate(XP.normWs(parts.join(" ")), limit);
  };
  const IDENT = /^-?[A-Za-z_][A-Za-z0-9_-]*$/;
  XP.cssIdent = (v) => (IDENT.test(v || "") ? v : String(v || "").replace(/[^A-Za-z0-9_-]/g, (c) => "\\" + c));
  const attrValue = (v) => '"' + String(v).replace(/\\/g, "\\\\").replace(/"/g, '\\"') + '"';
  XP.count = (doc, sel) => { try { return doc.querySelectorAll(sel).length; } catch { return 0; } };
  /** Port of unique_selector: id, classes, data-*, nearest ancestor id, other attributes, then an nth-of-type path. */
  XP.uniqueSelector = (el, doc) => {
    const tag = el.tagName.toLowerCase();
    if (el.id && XP.count(doc, `#${XP.cssIdent(el.id)}`) === 1) return `#${XP.cssIdent(el.id)}`;
    const classes = [...el.classList].filter((c) => IDENT.test(c));
    const base = tag + classes.slice(0, 3).map((c) => `.${c}`).join("");
    if (classes.length && XP.count(doc, base) === 1) return base;
    const byAttrs = (names) => {
      for (const a of names) {
        const v = el.getAttribute(a);
        if (v === null || v.length > 80) continue;
        const sel = `${base}[${a}=${attrValue(v)}]`;
        if (XP.count(doc, sel) === 1) return sel;
      }
      return null;
    };
    let found = byAttrs([...el.attributes].map((a) => a.name).filter((n) => n.startsWith("data-")).sort());
    if (found) return found;
    for (let anc = el.parentElement; anc; anc = anc.parentElement) {
      if (anc.id) { const sel = `#${XP.cssIdent(anc.id)} ${base}`; if (XP.count(doc, sel) === 1) return sel; break; }
    }
    found = byAttrs(["name", "aria-label", "type", "href"].filter((a) => el.hasAttribute(a)));
    if (found) return found;
    const parts = [];
    for (let node = el; node && node.tagName && node.tagName.toLowerCase() !== "html"; node = node.parentElement) {
      if (node.id && XP.count(doc, `#${XP.cssIdent(node.id)}`) === 1) { parts.push(`#${XP.cssIdent(node.id)}`); break; }
      let idx = 1;
      for (let sib = node.previousElementSibling; sib; sib = sib.previousElementSibling) if (sib.tagName === node.tagName) idx++;
      parts.push(`${node.tagName.toLowerCase()}:nth-of-type(${idx})`);
    }
    return parts.reverse().join(" > ") || tag;
  };
  const WORD_MAP = { btn: "button", cta: "CTA", img: "image", nav: "navigation", faq: "FAQ", q: "question" };
  const TAG_NOUN = { img: "image", a: "link", button: "button", div: "element", span: "element", li: "item", table: "table", form: "form", section: "section", input: "field" };
  /** img.hero-image -> 'hero image'; .plan-tab + 'Premium' -> "'Premium' plan tab". */
  XP.elementLabel = (selector, text = "", tag = "", plural = false) => {
    const last = String(selector || "").trim().split(/\s|>/).filter(Boolean).pop() || "";
    const m = last.match(/^([a-z][a-z0-9]*)?/);
    const tagname = ((m && m[1]) || tag || "").toLowerCase();
    const words = [];
    for (const [, name] of last.matchAll(/[.#]([\w-]+)/g)) for (let w of name.split(/[-_]/)) { w = WORD_MAP[w.toLowerCase()] || w.toLowerCase(); if (w && !words.includes(w)) words.push(w); }
    let noun = words.join(" ") || TAG_NOUN[tagname] || tagname || "element";
    if (tagname === "img" && !noun.includes("image")) noun += " image";
    if (plural && !noun.endsWith("s")) noun += "s";
    const t = T().truncate(String(text || "").trim(), 40);
    return t ? `'${t}' ${noun}` : noun;
  };
  XP.pageKey = (url) => { try { const u = new URL(url); return (u.hostname + (u.pathname || "/")).toLowerCase(); } catch { return "/"; } };
  XP.composeHypothesis = (a, b, c) => {
    const strip = (s, w) => String(s || "").trim().replace(/\.$/, "").replace(new RegExp(`^\\s*${w}\\s+`, "i"), "");
    return `If ${strip(a, "if")}, then ${strip(b, "then")}, because ${strip(c, "because")}.`;
  };

  /* ============================================================ page facts (port of extract_page_facts) */
  const CTA_VERBS = /^(get|start|see|buy|book|talk|call|compare|try|apply|join|sign|request|claim|find|contact|download|register|shop|switch|quote|check|speak|chat|subscribe)\b/i;
  const CONTROL_HINT = /(tab|btn|button|toggle|clickable|accordion|chip|pill|select|option)/i;
  const PRICE = /[$£€]\s?\d[\d,]*(?:\.\d{2})?/g;
  const INTERACTIVE = "a,button,input,select,textarea,summary,label,option,details";

  XP.pageFacts = (html, url = "") => {
    const doc = CAF.lib.html.parse(html || "");
    doc.querySelectorAll("script,style,noscript,template").forEach((t) => { if (!(t.tagName === "SCRIPT" && t.getAttribute("type") === "application/ld+json")) t.remove(); });
    const title = doc.title ? XP.normWs(doc.title) : "";
    const meta = doc.querySelector('meta[name="description"]');
    const main = doc.querySelector("main") || doc.body || doc.documentElement;
    let sections = [...main.querySelectorAll("section, article")].filter((s) => !(s.parentElement && s.parentElement.closest("section, article")));
    if (!sections.length) sections = [...main.children].filter((c) => !/^(SCRIPT|STYLE)$/.test(c.tagName));
    const label = (sec) => sec.getAttribute("aria-label") || (sec.querySelector("h1,h2,h3") ? XP.text(sec.querySelector("h1,h2,h3"), 80) : ([...sec.classList].join(" ") || sec.id || sec.tagName.toLowerCase()));
    let heroIndex = null;
    const secInfo = sections.map((sec, i) => {
      const cls = [...sec.classList].join(" ");
      if (heroIndex === null && (cls.toLowerCase().includes("hero") || (sec.id || "").toLowerCase().includes("hero"))) heroIndex = i + 1;
      return { index: i + 1, selector: XP.uniqueSelector(sec, doc), label: label(sec), classes: [...sec.classList] };
    });
    heroIndex = heroIndex || (sections.length ? 1 : null);
    const sectionOf = (el) => { for (let i = 0; i < sections.length; i++) if (sections[i] === el || sections[i].contains(el)) return i + 1; return 0; };
    const inChrome = (el) => {
      for (let a = el.parentElement; a; a = a.parentElement) {
        const t = a.tagName.toLowerCase();
        if (t === "nav" || t === "footer") return t;
        if (t === "header" && !a.closest("main")) return "header";
      }
      return null;
    };
    const ctas = [];
    const seen = new Set();
    const cands = [...doc.querySelectorAll("a.btn, a[class*=cta], a[id*=cta], a[role=button], button, input[type=submit], input[type=button]")];
    for (const a of doc.querySelectorAll("a[href]")) if (CTA_VERBS.test(XP.text(a, 60)) && !["nav", "footer"].includes(inChrome(a))) cands.push(a);
    for (const el of cands) {
      if (seen.has(el)) continue;
      seen.add(el);
      const chrome = inChrome(el);
      if (["nav", "footer"].includes(chrome) && ![...el.classList].some((c) => c.includes("cta"))) continue;
      const text = XP.text(el, 80) || el.getAttribute("value") || el.getAttribute("aria-label") || "";
      if (!text) continue;
      const idx = sectionOf(el);
      const cls = [...el.classList];
      ctas.push({ text, selector: XP.uniqueSelector(el, doc), href: el.getAttribute("href"), tag: el.tagName.toLowerCase(), classes: cls, section_index: idx,
        location: chrome || (idx && idx === heroIndex ? "hero" : `section ${idx}`), in_hero: !!(idx && idx === heroIndex),
        primary: cls.some((c) => c.includes("primary")) || (el.id || "").includes("hero-cta") });
    }
    if (ctas.length && !ctas.some((c) => c.primary)) (ctas.find((c) => c.in_hero) || ctas[0]).primary = true;
    const dup = {};
    ctas.forEach((c) => { const k = T().canonical(c.text); dup[k] = (dup[k] || 0) + 1; });
    ctas.forEach((c) => { c.duplicates = dup[T().canonical(c.text)]; });
    const forms = [...doc.querySelectorAll("form")].map((f) => {
      const fields = [...f.querySelectorAll("input,select,textarea")].filter((i) => !["hidden", "submit", "button", "reset", "image"].includes((i.getAttribute("type") || "text").toLowerCase()));
      const labelled = fields.filter((i) => i.getAttribute("aria-label") || i.getAttribute("aria-labelledby") || i.closest("label") || (i.id && doc.querySelector(`label[for="${i.id}"]`))).length;
      return { selector: XP.uniqueSelector(f, doc), fields: fields.length, required: fields.filter((i) => i.hasAttribute("required")).length, labelled, section_index: sectionOf(f) };
    });
    const imgs = [...doc.querySelectorAll("img")];
    const missingAlt = imgs.filter((i) => !i.hasAttribute("alt")).map((i) => XP.uniqueSelector(i, doc));
    const controls = new Map();
    for (const el of doc.querySelectorAll("div, span, li")) {
      const cls = [...el.classList].join(" ");
      const dataAttrs = [...el.attributes].map((a) => a.name).filter((n) => n.startsWith("data-") && n !== "data-selected");
      if (!(CONTROL_HINT.test(cls) || el.getAttribute("onclick")) || el.getAttribute("role") || el.hasAttribute("tabindex")) continue;
      if (el.querySelector(INTERACTIVE)) continue;
      if (!(dataAttrs.length || el.getAttribute("onclick") || /(^|[-_ ])(tab|btn|button|toggle)([-_ ]|$)/.test(cls))) continue;
      const key = el.tagName.toLowerCase() + [...el.classList].filter((c) => IDENT.test(c)).slice(0, 2).map((c) => `.${c}`).join("");
      const info = controls.get(key) || { selector: key, count: 0, texts: [], data_attrs: dataAttrs };
      info.count += 1;
      info.texts.push(XP.text(el, 30));
      controls.set(key, info);
    }
    let quotes = [...doc.querySelectorAll("blockquote, .testimonial, .review, [class*=testimonial]")];
    quotes = quotes.filter((q) => !quotes.some((p) => p !== q && p.contains(q)));
    let rating = null;
    const walker = doc.createTreeWalker(doc.body || doc.documentElement, NodeFilter.SHOW_TEXT);
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      if (n.parentElement && n.parentElement.closest("script,style")) continue;
      if (/(rated|out of 5|stars|reviews)/i.test(n.nodeValue)) { rating = T().truncate(XP.normWs(n.nodeValue), 120); break; }
    }
    const tables = [...doc.querySelectorAll("table")];
    const pricing = tables.find((t) => /[$£€]\s?\d/.test(t.textContent)) || null;
    const plansSection = (secInfo.find((s) => /plan|pricing/.test(s.classes.join(" ").toLowerCase())) || {}).index || null;
    const faq = doc.querySelector("#faq, .faq, [class*=faq]");
    const offers = secInfo.filter((s) => /promo|banner|offer|band/.test(s.classes.join(" ").toLowerCase())).map((s) => {
      const c = ctas.find((x) => x.section_index === s.index);
      return { selector: s.selector, label: s.label, section_index: s.index, cta: c ? c.text : null };
    });
    const mainText = XP.text(main, 1e9);
    return {
      url, title, meta_description: meta ? (meta.getAttribute("content") || "").trim() : "",
      h1: [...doc.querySelectorAll("h1")].map((h) => XP.text(h, 140)),
      headings: [...doc.querySelectorAll("h2, h3")].slice(0, 20).map((h) => ({ level: +h.tagName[1], text: XP.text(h, 100) })),
      sections: secInfo.map((s) => ({ index: s.index, selector: s.selector, label: s.label })),
      hero_index: heroIndex, ctas, forms, images: { count: imgs.length, missing_alt: missingAlt }, non_semantic_controls: [...controls.values()],
      testimonials: { count: quotes.length, section_index: quotes.length ? sectionOf(quotes[0]) : null }, rating_text: rating,
      pricing: { table_selector: pricing ? XP.uniqueSelector(pricing, doc) : null, has_headers: pricing ? !!pricing.querySelector("th") : null,
        section_index: pricing ? sectionOf(pricing) : null, plans_section_index: plansSection, prices: [...new Set(mainText.match(PRICE) || [])].sort().slice(0, 8) },
      faq: { selector: faq ? XP.uniqueSelector(faq, doc) : null, count: doc.querySelectorAll("details, .faq-item").length, section_index: faq ? sectionOf(faq) : null },
      offers, n_sections: secInfo.length, word_count: mainText.split(/\s+/).filter(Boolean).length,
    };
  };

  XP.pageFactLines = (f, maxCtas = 8) => {
    const L = [`Title: ${f.title || "(none)"}`, `H1: ${(f.h1 || []).join(" | ") || "(none)"}`];
    if (f.meta_description) L.push(`Meta description: ${f.meta_description}`);
    L.push(`Sections (${f.n_sections || 0}): ` + (f.sections || []).slice(0, 12).map((s) => `${s.index}. ${s.label}`).join("; "));
    for (const c of (f.ctas || []).slice(0, maxCtas)) L.push(`CTA '${c.text}' at ${c.selector} (${c.location}${c.primary ? ", primary" : ""}) -> ${c.href || "-"}`);
    for (const x of f.forms || []) L.push(`Form ${x.selector}: ${x.fields} fields, ${x.required} required, ${x.labelled} labelled`);
    for (const c of f.non_semantic_controls || []) L.push(`Non-semantic control ${c.selector} x${c.count} (${c.texts.slice(0, 4).join(", ")})`);
    if (((f.images || {}).missing_alt || []).length) L.push(`Images without alt: ${f.images.missing_alt.slice(0, 5).join(", ")}`);
    if (f.rating_text) L.push(`Rating text: ${f.rating_text}`);
    if ((f.testimonials || {}).count) L.push(`Testimonials: ${f.testimonials.count} in section ${f.testimonials.section_index}`);
    const p = f.pricing || {};
    if (p.table_selector) L.push(`Pricing table ${p.table_selector} in section ${p.section_index} (header cells: ${p.has_headers ? "yes" : "no"})`);
    if ((f.faq || {}).selector) L.push(`FAQ ${f.faq.selector} with ${f.faq.count} items in section ${f.faq.section_index}`);
    for (const o of f.offers || []) L.push(`Offer band ${o.selector} '${o.label}' in section ${o.section_index} (CTA: ${o.cta || "-"})`);
    return L;
  };

  /* ============================================================ evidence (pure ports) */
  const median = (a) => CAF.lib.stats.median(a);
  /** Port of behaviour_summary: dead clicks (excluding rage bursts), rage bursts, scroll survival, modal stop depth. */
  XP.behaviourSummary = (events, { rageWindowS = 1.5, rageMinClicks = 3 } = {}) => {
    if (!events || !events.length) return { available: false };
    const rows = events.map((e, i) => ({ ...e, _i: i, _et: String(e.event_type || "").toLowerCase(), _t: Date.parse(e.ts) / 1000 }));
    const sessions = new Set(rows.map((r) => r.session_id)).size;
    const clicks = rows.filter((r) => r._et.includes("click") || r._et === "tap");
    const interactive = (r) => ["1", "true", "yes", "y", "t"].includes(String(r.is_interactive).trim().toLowerCase());
    const explicitRage = clicks.some((r) => r._et.includes("rage"));
    const inBurst = new Set();
    if (!explicitRage) {
      const groups = new Map();
      for (const r of clicks) { const k = `${r.session_id}\u0000${r.selector}`; if (!groups.has(k)) groups.set(k, []); groups.get(k).push(r); }
      for (const g of groups.values()) {
        g.sort((a, b) => a._t - b._t);
        let j = 0;
        for (let i = 0; i < g.length; i++) {
          while (g[i]._t - g[j]._t > rageWindowS) j++;
          if (i - j + 1 >= rageMinClicks) for (let k = j; k <= i; k++) inBurst.add(g[k]._i);
        }
      }
    }
    const isRage = (r) => r._et.includes("rage") || inBurst.has(r._i);
    const rage = clicks.filter(isRage);
    const dead = clicks.filter((r) => r._et.includes("dead") || (!interactive(r) && !isRage(r)));
    const deadAll = clicks.filter((r) => r._et.includes("dead") || !interactive(r));
    const targets = (frame, n = 5) => {
      const g = new Map();
      for (const r of frame) {
        if (r.selector === null || r.selector === undefined) continue;
        const t = g.get(r.selector) || { selector: String(r.selector), count: 0, s: new Set(), text: r.element_text, tag: r.tag };
        t.count++; t.s.add(r.session_id); g.set(r.selector, t);
      }
      return [...g.values()].sort((a, b) => b.count - a.count).slice(0, n).map((t) => ({ selector: t.selector, count: t.count, sessions: t.s.size,
        text: t.text === null || t.text === undefined ? "" : String(t.text).slice(0, 60), tag: t.tag === null || t.tag === undefined ? "" : String(t.tag),
        share_of_clicks: XP.round(t.count / Math.max(clicks.length, 1), 4) }));
    };
    const depthBy = new Map();
    for (const r of rows) if (typeof r.scroll_pct === "number") depthBy.set(r.session_id, Math.max(depthBy.get(r.session_id) ?? -Infinity, r.scroll_pct));
    let depth = [...new Set(rows.map((r) => r.session_id))].map((s) => depthBy.get(s) ?? 0);
    const reach = {};
    let falseBottom = null, medianDepth = null;
    if (depthBy.size) {
      if (Math.max(...depth) <= 1) depth = depth.map((d) => d * 100);
      medianDepth = median(depth);
      const surv = (d) => depth.filter((x) => x >= d - 1e-9).length / depth.length;
      for (let d = 10; d <= 100; d += 10) reach[String(d)] = XP.round(surv(d), 4);
      let start = 0, drop = -Infinity;
      for (let d = 0; d <= 90; d += 5) { const w = surv(d) - surv(d + 10); if (XP.round(w, 6) > XP.round(drop, 6)) { drop = w; start = d; } }
      if (drop > 0) {
        const counts = Array(20).fill(0);
        for (const x of depth) counts[Math.min(19, Math.floor(Math.min(Math.max(x, 0), 99.999) / 5))]++;
        let k = 0;
        for (let i = 1; i < 19; i++) if (counts[i] > counts[k]) k = i;
        const inBin = depth.filter((x) => x >= k * 5 && x < (k + 1) * 5);
        falseBottom = { from_pct: start, to_pct: start + 10, depth_pct: start + 5, drop: XP.round(drop, 4), reach_before: XP.round(surv(start), 4),
          reach_after: XP.round(surv(start + 10), 4), stop_band: [k * 5, (k + 1) * 5], stop_share: XP.round(counts[k] / depth.length, 4),
          stop_depth: inBin.length ? XP.round(median(inBin), 1) : start + 5 };
      }
    }
    const firstDevice = new Map();
    for (const r of [...rows].sort((a, b) => (a.session_id < b.session_id ? -1 : a.session_id > b.session_id ? 1 : a._t - b._t))) if (!firstDevice.has(r.session_id)) firstDevice.set(r.session_id, String(r.device).toLowerCase());
    const devices = {};
    for (const d of firstDevice.values()) devices[d] = (devices[d] || 0) + 1;
    for (const d of Object.keys(devices)) devices[d] = XP.round(devices[d] / firstDevice.size, 4);
    return { available: true, sessions, events: rows.length, clicks: clicks.length, dead_clicks: dead.length, dead_clicks_incl_rage: deadAll.length,
      dead_click_share: XP.round(dead.length / Math.max(clicks.length, 1), 4), rage_clicks: rage.length, rage_sessions: new Set(rage.map((r) => r.session_id)).size,
      top_dead_targets: targets(dead), top_rage_targets: targets(rage), median_scroll_depth: medianDepth === null ? null : XP.round(medianDepth, 1),
      scroll_reach: reach, false_bottom: falseBottom, devices };
  };

  /** Port of page_analytics over analytics/pages rows (daily per page). */
  XP.pageAnalytics = (rows, pagePath, cfg = {}) => {
    const imp = ((cfg.scoring || {}).importance) || {};
    if (!rows || !rows.length) return { available: false, importance: imp.unknown ?? 5, is_estimate: true };
    const dates = rows.map((r) => String(r.date)).sort();
    const days = Math.round((Date.parse(dates[dates.length - 1]) - Date.parse(dates[0])) / 864e5) + 1;
    const agg = new Map();
    for (const r of rows) {
      const k = `${r.page_path}\u0000${r.page_title}`;
      const a = agg.get(k) || { page_path: r.page_path, page_title: r.page_title, sessions: 0, conversions: 0, engaged_sessions: 0 };
      a.sessions += +r.sessions || 0; a.conversions += +r.conversions || 0; a.engaged_sessions += +r.engaged_sessions || 0;
      agg.set(k, a);
    }
    const pages = [...agg.values()].sort((a, b) => b.sessions - a.sessions);
    const norm = (p) => "/" + String(p || "/").replace(/^https?:\/\/[^/]+/, "").split("?")[0].replace(/^\/+|\/+$/g, "");
    const want = norm(pagePath || "/");
    let row = pages.find((p) => norm(p.page_path) === want);
    if (!row && want !== "/") { const last = want.replace(/\/$/, "").split("/").pop(); row = pages.find((p) => norm(p.page_path).endsWith(last)); }
    const total = pages.reduce((s, p) => s + p.sessions, 0);
    const siteCr = pages.reduce((s, p) => s + p.conversions, 0) / Math.max(total, 1);
    const siteEr = pages.reduce((s, p) => s + p.engaged_sessions, 0) / Math.max(total, 1);
    const period = `${dates[0]} to ${dates[dates.length - 1]}`;
    if (!row) return { available: false, site_conversion_rate: XP.round(siteCr, 5), importance: imp.unknown ?? 5, is_estimate: true, period };
    const maxS = Math.max(...pages.map((p) => p.sessions));
    const lo = imp.min ?? 2, hi = imp.max ?? 10;
    return { available: true, is_estimate: false, page_path: row.page_path, page_title: row.page_title, sessions: row.sessions, conversions: row.conversions,
      conversion_rate: XP.round(row.conversions / Math.max(row.sessions, 1), 5), engagement_rate: XP.round(row.engaged_sessions / Math.max(row.sessions, 1), 4),
      site_conversion_rate: XP.round(siteCr, 5), site_engagement_rate: XP.round(siteEr, 4), share_of_sessions: XP.round(row.sessions / Math.max(total, 1), 4),
      rank_by_sessions: pages.filter((p) => p.sessions > row.sessions).length + 1, pages_compared: pages.length, daily_sessions: XP.round(row.sessions / days, 1), days,
      importance: XP.round(Math.min(hi, Math.max(lo, lo + (hi - lo) * Math.sqrt(row.sessions / Math.max(maxS, 1)))), 2), period };
  };

  const ev = (source, signal, detail, severity = "medium", target = null, params = {}) => ({ id: "", source, signal, detail, severity, target, params });
  const pct = (x, d = 0) => `${(x * 100).toFixed(d)}%`;

  function pageSignals(facts, cfg) {
    const th = cfg.signals || {};
    const generic = new Set((cfg.generic_cta_texts || []).map((t) => T().canonical(t)));
    const out = [];
    const ctas = facts.ctas || [];
    const primary = ctas.find((c) => c.primary) || ctas[0];
    if (primary && generic.has(T().canonical(primary.text)))
      out.push(ev("page", "generic_cta_text", `Primary CTA ${primary.selector} uses the generic label '${primary.text}'.`, "medium", primary.selector, { cta_text: primary.text, cta_selector: primary.selector }));
    if (primary && (primary.duplicates || 1) > 1)
      out.push(ev("page", "duplicate_cta_text", `The label '${primary.text}' is used by ${primary.duplicates} different CTAs on the page.`, "low", primary.selector, { cta_text: primary.text, cta_selector: primary.selector }));
    const heroCtas = ctas.filter((c) => c.in_hero || c.location === "header");
    if (heroCtas.length >= (th.ctas_above_fold ?? 3)) out.push(ev("page", "many_ctas_above_fold", `${heroCtas.length} calls to action compete above the fold.`, "medium", null, { cta_count: heroCtas.length }));
    const plansIdx = (facts.pricing || {}).plans_section_index;
    for (const o of facts.offers || []) if (o.cta && plansIdx && o.section_index < plansIdx)
      out.push(ev("page", "competing_offer_above_plans", `Offer band ${o.selector} ('${o.label}', CTA '${o.cta}') sits in section ${o.section_index}, before the plan comparison (section ${plansIdx}).`,
        "medium", o.selector, { offer_label: `'${T().truncate(o.label, 60)}'`, offer_selector: o.selector }));
    for (const c of facts.non_semantic_controls || [])
      out.push(ev("page", "non_semantic_controls", `${c.count} non-semantic controls ${c.selector} (${c.texts.slice(0, 3).join(", ")}) have no role, tabindex or keyboard support.`,
        "high", c.selector, { control_selector: c.selector, control_label: XP.elementLabel(c.selector, "", "", c.count > 1) }));
    const missing = (facts.images || {}).missing_alt || [];
    if (missing.length) out.push(ev("page", "images_missing_alt", `${missing.length} image(s) without an alt attribute: ${missing.slice(0, 3).join(", ")}.`, "medium", missing[0], { image_selector: missing[0] }));
    for (const f of facts.forms || []) if (f.fields > (th.long_form_fields ?? 6))
      out.push(ev("page", "long_form", `Form ${f.selector} asks for ${f.fields} fields.`, "medium", f.selector, { form_selector: f.selector, form_fields: f.fields }));
    const t = facts.testimonials || {};
    const nSec = Math.max(+facts.n_sections || 1, 1);
    if (!t.count && !facts.rating_text) out.push(ev("page", "no_social_proof", "No testimonials or ratings on the page.", "medium"));
    else if (t.count && t.section_index && t.section_index >= Math.max(3, Math.ceil(nSec * 0.5)))
      out.push(ev("page", "testimonials_below_fold", `${t.count} testimonials sit in section ${t.section_index} of ${nSec}, far from the primary CTA.`, "low", null, { testimonial_section: t.section_index }));
    const p = facts.pricing || {};
    if (p.table_selector && p.has_headers === false) out.push(ev("page", "table_without_headers", `Pricing table ${p.table_selector} has no header cells.`, "medium", p.table_selector, { table_selector: p.table_selector }));
    const fq = facts.faq || {};
    if (fq.selector && fq.section_index && fq.section_index >= Math.max(3, Math.ceil(nSec * 0.6)))
      out.push(ev("page", "faq_below_fold", `The FAQ (${fq.count} questions) is in section ${fq.section_index} of ${nSec}.`, "low", fq.selector));
    return out;
  }

  function analyticsSignals(m, cfg) {
    if (!m.available) return [];
    const th = cfg.signals || {};
    const params = { page_cr: pct(m.conversion_rate, 2), site_cr: pct(m.site_conversion_rate, 2), page_share: pct(m.share_of_sessions), page_engagement: m.engagement_rate !== null ? pct(m.engagement_rate) : "n/a" };
    const out = [];
    const below = m.conversion_rate < m.site_conversion_rate * (th.below_avg_conversion ?? 0.9);
    if (below) out.push(ev("analytics", "below_avg_conversion", `Page conversion rate ${pct(m.conversion_rate, 2)} vs site average ${pct(m.site_conversion_rate, 2)} (${U.fmtNum(m.sessions)} sessions).`, "high", m.page_path, params));
    if (m.share_of_sessions >= (th.high_traffic_share ?? 0.1) || m.rank_by_sessions <= 3)
      out.push(ev("analytics", "high_traffic_page", `The page receives ${pct(m.share_of_sessions, 1)} of site sessions (rank ${m.rank_by_sessions} of ${m.pages_compared}).`, "medium", m.page_path, params));
    const er = m.engagement_rate, ser = m.site_engagement_rate;
    if (er !== null && ser) {
      if (er >= ser && below) out.push(ev("analytics", "engaged_not_converting", `Engagement rate ${pct(er)} is at or above the site's ${pct(ser)}, but conversion is below average.`, "medium", m.page_path, params));
      if (er < ser * (th.low_engagement ?? 0.85)) out.push(ev("analytics", "low_engagement", `Engagement rate ${pct(er)} vs site ${pct(ser)}.`, "medium", m.page_path, params));
    }
    return out;
  }

  const sectionEndingBefore = (layout, depth, tol = 1) => { const ended = ((layout || {}).sections || []).filter((s) => s.bottom_pct <= depth + tol); return ended.length ? ended.reduce((a, b) => (b.bottom_pct > a.bottom_pct ? b : a)) : null; };

  function behaviourSignals(b, cfg, layout) {
    if (!b.available) return [];
    const th = cfg.signals || {};
    const out = [];
    for (const t of (b.top_dead_targets || []).slice(0, 2)) if (t.count >= (th.dead_click_min ?? 15) || t.share_of_clicks >= (th.dead_click_share ?? 0.05))
      out.push(ev("behaviour", "dead_clicks", `${t.count} dead clicks on ${t.selector} from ${t.sessions} sessions (${pct(t.share_of_clicks)} of all clicks) - the element is not interactive.`, "high", t.selector,
        { dead_selector: t.selector, dead_clicks: t.count, dead_share: pct(t.share_of_clicks), dead_target_label: XP.elementLabel(t.selector, t.text, t.tag) }));
    for (const t of (b.top_rage_targets || []).slice(0, 2)) if (t.count >= (th.rage_click_min ?? 10))
      out.push(ev("behaviour", "rage_clicks", `${t.count} rage clicks on ${t.selector} ('${t.text || t.tag}') from ${t.sessions} sessions.`, "critical", t.selector,
        { rage_selector: t.selector, rage_clicks: t.count, rage_target_label: XP.elementLabel(t.selector, t.text, t.tag) }));
    const fb = b.false_bottom;
    if (fb && fb.drop >= (th.false_bottom_drop ?? 0.2) && fb.from_pct <= (th.false_bottom_max_depth ?? 70)) {
      const sec = layout ? sectionEndingBefore(layout, fb.stop_depth ?? fb.depth_pct) : null;
      const where = sec ? `; ${pct(fb.stop_share || 0)} of visitors stop at ${fb.stop_band[0]}-${fb.stop_band[1]}%, just after section ${sec.index} '${T().truncate(sec.label, 60)}' (${Math.round(sec.top_pct)}-${Math.round(sec.bottom_pct)}% of the measured page) comes fully into view` : "";
      out.push(ev("behaviour", "false_bottom", `Scroll survival falls from ${pct(fb.reach_before)} to ${pct(fb.reach_after)} between ${fb.from_pct}% and ${fb.to_pct}% of the page (a ${pct(fb.drop)} drop)${where} - a false bottom.`,
        "high", sec && sec.selector ? sec.selector : null, { false_bottom_depth: `${fb.from_pct}-${fb.to_pct}`, false_bottom_drop: pct(fb.drop), false_bottom_hint: sec ? `section ${sec.index} '${T().truncate(sec.label, 50)}'` : null }));
    }
    if (b.median_scroll_depth !== null && b.median_scroll_depth < (th.low_scroll_median ?? 50))
      out.push(ev("behaviour", "low_scroll_depth", `Median scroll depth is ${Math.round(b.median_scroll_depth)}% of the page.`, "medium", null, { median_scroll: `${Math.round(b.median_scroll_depth)}%` }));
    const mob = (b.devices || {}).mobile || 0;
    if (mob >= (th.mobile_heavy_share ?? 0.5)) out.push(ev("behaviour", "mobile_heavy", `${pct(mob)} of recorded sessions are on mobile.`, "low", null, { mobile_share: pct(mob) }));
    return out;
  }

  const UPSTREAM_SIGNAL = { accessibility: "upstream_accessibility", seo: "upstream_seo", geo: "upstream_seo", performance: "upstream_performance", behaviour: "upstream_behaviour", content: "upstream_content", analytics: "upstream_analytics" };
  function upstreamSignal(f) {
    const text = `${f.title || ""} ${f.detail || ""}`.toLowerCase();
    const target = f.target || "";
    const css = target && !/^(http|\/)/.test(String(target));
    if (text.includes("rage")) return ["upstream_rage_clicks", css ? { rage_selector: target, rage_target_label: XP.elementLabel(target) } : {}];
    if (text.includes("dead click") || text.includes("dead-click")) return ["upstream_dead_clicks", css ? { dead_selector: target, dead_target_label: XP.elementLabel(target) } : {}];
    if (text.includes("ignored") && !text.includes("false bottom")) return ["upstream_behaviour", {}];
    if (text.includes("false bottom") || text.includes("scroll depth") || text.includes("scroll reach")) { const m = text.match(/(\d{1,3})\s*[-–]\s*(\d{1,3})\s*%/); return ["upstream_scroll", m ? { false_bottom_depth: `${m[1]}-${m[2]}` } : {}]; }
    return [UPSTREAM_SIGNAL[String(f.category || "").toLowerCase()] || "upstream_other", {}];
  }
  function upstreamSignals(findings, limit = 12) {
    const order = { critical: 0, high: 1, medium: 2, low: 3 };
    return [...findings].sort((a, b) => (order[a.severity] ?? 2) - (order[b.severity] ?? 2)).slice(0, limit).map((f) => {
      const [signal, params] = upstreamSignal(f);
      const detail = `[${f.source_agent || "upstream"}] ${f.title || ""}` + (f.detail ? `: ${T().truncate(f.detail, 160)}` : "");
      return ev(`upstream:${f.source_agent || "agent"}`, signal, detail, f.severity || "medium", f.target || null, { upstream_title: f.title || "", ...params });
    });
  }
  /** Upstream findings for this page; outputs naming another page are skipped (port of page_findings). */
  function pageFindings(upstream, page) {
    const found = []; let skipped = 0;
    for (const [k, v] of Object.entries(upstream || {})) {
      const out = (v && v.output) || v;
      if (!U.isObj(out)) continue;
      const src = ["page_url", "url", "final_url"].map((x) => out[x]).find((x) => typeof x === "string" && x);
      const same = !src || !page || XP.pageKey(src) === XP.pageKey(page);
      for (const f of out.findings || []) { if (!U.isObj(f)) continue; if (!same) { skipped++; continue; } found.push({ ...f, source_agent: f.source_agent || v.agent_id || k }); }
    }
    return { found, skipped };
  }

  const SEV = { critical: 3, high: 2, medium: 1, low: 0 };
  XP.selectorTokens = (sel) => {
    if (!sel) return new Set();
    const s = new Set([...String(sel).matchAll(/[#.]([A-Za-z_][\w-]*)/g)].map((m) => m[1]));
    for (const m of String(sel).matchAll(/=\s*"?([\w-]+)"?\s*\]/g)) s.add(m[1]);
    return s;
  };
  XP.relatedTargets = (a, b) => {
    const pageLevel = (t) => !t || /^(http|\/)/.test(String(t));
    if (pageLevel(a) || pageLevel(b)) return true;
    if (a === b) return true;
    const ta = XP.selectorTokens(a), tb = XP.selectorTokens(b);
    for (const t of ta) if (tb.has(t)) return true;
    return false;
  };
  function matchHeuristics(heuristics, evidence) {
    const bySignal = {};
    for (const e of evidence) (bySignal[e.signal] = bySignal[e.signal] || []).push(e);
    const out = [];
    for (const h of heuristics) {
      const trig = (h.triggers || []).flatMap((s) => bySignal[s] || []);
      if (!trig.length) continue;
      const rank = Object.fromEntries((h.triggers || []).map((s, i) => [s, i]));
      trig.sort((a, b) => (rank[a.signal] ?? 99) - (rank[b.signal] ?? 99) || (SEV[b.severity] ?? 1) - (SEV[a.severity] ?? 1)
        || (["behaviour", "analytics"].includes(a.source) ? 0 : 1) - (["behaviour", "analytics"].includes(b.source) ? 0 : 1) || (+a.id.slice(1) - +b.id.slice(1)));
      const primary = trig[0];
      const evs = [primary];
      for (const e of trig.slice(1)) {
        const same = e.signal === primary.signal;
        if ((same && e.target === primary.target) || (!same && XP.relatedTargets(e.target, primary.target))) evs.push(e);
      }
      for (const s of h.supporting || []) for (const e of bySignal[s] || []) if (!evs.includes(e) && XP.relatedTargets(e.target, primary.target)) evs.push(e);
      const strength = new Set(evs.map((e) => e.source.split(":")[0])).size * 10 + Math.max(...evs.map((e) => SEV[e.severity] ?? 1)) * 3 + (h.typical_impact ?? 5);
      out.push({ heuristic: h, evidence: evs, primary, strength });
    }
    return out.sort((a, b) => b.strength - a.strength || (a.heuristic.id < b.heuristic.id ? -1 : 1));
  }

  /** ICE (impact, confidence, ease) and PIE (potential, importance, ease) - port of ice_pie_scores. */
  XP.icePie = (h, evidence, metrics, scoring, learningEffect = 0) => {
    const top = evidence.length ? evidence.reduce((a, b) => ((SEV[b.severity] ?? 1) > (SEV[a.severity] ?? 1) ? b : a)).severity : "medium";
    const impact = XP.clamp((h.typical_impact ?? 5) + (((scoring.impact || {}).severity_bonus || {})[top] || 0));
    const c = scoring.confidence || {};
    const sources = new Set(evidence.map((e) => e.source.split(":")[0]));
    let conf = (c.base ?? 3) + (c.per_source ?? 1.5) * Math.max(sources.size - 1, 0) + (c.per_item ?? 0.5) * Math.min(Math.max(evidence.length - 1, 0), c.max_items ?? 4);
    if (sources.has("behaviour") || sources.has("analytics")) conf += c.quantitative_bonus ?? 1;
    if (learningEffect > 0) conf += c.learning_win_bonus ?? 1; else if (learningEffect < 0) conf -= c.learning_loss_penalty ?? 1;
    const confidence = XP.clamp(conf);
    const ease = XP.clamp(h.ease ?? 5);
    const p = scoring.potential || {};
    let gap = 0;
    if (metrics.available && metrics.site_conversion_rate) gap = Math.max(-1, Math.min(1, (metrics.site_conversion_rate - metrics.conversion_rate) / metrics.site_conversion_rate));
    const potential = XP.clamp((p.base ?? 5) + (p.gap_weight ?? 5) * gap + ((p.severity_bonus || {})[top] || 0));
    const importance = XP.clamp(metrics.importance ?? ((scoring.importance || {}).unknown ?? 5));
    const combine = (a, b, cc, how) => (how === "product" ? XP.round((a * b * cc) / 100, 2) : XP.round((a + b + cc) / 3, 2));
    return [{ impact, confidence, ease, score: combine(impact, confidence, ease, scoring.ice || "mean") },
      { potential, importance, ease, score: combine(potential, importance, ease, scoring.pie || "mean") }];
  };

  const DEFAULT_PARAMS = { cta_text: "the current label", cta_selector: "the primary CTA", dead_selector: "the dead-clicked element", dead_target_label: "element", dead_clicks: "repeated",
    dead_share: "a large share", rage_selector: "the unresponsive control", rage_target_label: "control", rage_clicks: "repeated", false_bottom_depth: "40", false_bottom_drop: "a sharp fall",
    false_bottom_hint: "the full-width band at that depth", testimonial_section: "a late", offer_label: "competing offer", control_label: "controls", control_selector: "the custom controls",
    page_cr: "below average", site_cr: "the site average", page_share: "a large share", page_engagement: "a healthy", table_selector: "the pricing table", form_selector: "the form",
    form_fields: "many", mobile_share: "most", meta_hint: "title and description", median_scroll: "low" };
  const fill = (tpl, params) => String(tpl || "").replace(/\{(\w+)\}/g, (_, k) => (params[k] !== undefined && params[k] !== null && params[k] !== "" ? String(params[k]) : DEFAULT_PARAMS[k] ?? k.replace(/_/g, " ")));
  const ideaHash = (pk, hids, title) => "idea-" + U.hash(`${pk}|${[...new Set(hids)].sort().join(",")}|${[...new Set(T().tokens(title))].sort().slice(0, 14).join(" ")}`);
  const jaccard = (a, b) => { const ta = new Set(T().tokens(a)), tb = new Set(T().tokens(b)); let n = 0; for (const t of ta) if (tb.has(t)) n++; return n / Math.max(new Set([...ta, ...tb]).size, 1); };
  const findDuplicate = (idea, existing, th = 0.6) => existing.find((r) => r.id === idea.id) ||
    existing.find((r) => r.page === idea.page && (r.heuristic_ids || []).some((h) => idea.heuristic_ids.includes(h)) && jaccard(r.title || "", idea.title) >= th) || null;

  function heuristicIdea(m, facts) {
    const h = m.heuristic;
    const params = {};
    const primary = (facts.ctas || []).find((c) => c.primary);
    if (primary) Object.assign(params, { cta_text: primary.text, cta_selector: primary.selector });
    if (facts.meta_description) params.meta_hint = `'${T().truncate(facts.meta_description, 70)}'`;
    for (const e of m.evidence) for (const [k, v] of Object.entries(e.params || {})) if (!(k in params) && v !== null && v !== undefined) params[k] = v;
    const tpl = h.idea || {};
    return { title: fill(tpl.title || h.name, params), hypothesis: XP.composeHypothesis(fill(tpl.hypothesis_if, params), fill(tpl.hypothesis_then, params), fill(tpl.hypothesis_because, params)),
      change: fill(tpl.change, params), rationale: `${h.name}: ${h.principle}`, heuristic_ids: [h.id], evidence_ids: m.evidence.map((e) => e.id),
      primary_metric: h.primary_metric || "Conversion rate", target: (m.evidence.find((e) => e.target) || {}).target || null, origin: "heuristic" };
  }

  /** Measured section positions (twin of SECTION_LAYOUT_JS) in a real layout frame. */
  async function measureLayout(html, facts) {
    const frame = await CAF.lib.html.renderInFrame(html, { width: 1280, height: 800 });
    try {
      const doc = frame.doc, win = frame.win;
      const H = Math.max(doc.documentElement.scrollHeight, doc.body ? doc.body.scrollHeight : 0) || 1;
      const main = doc.querySelector("main") || doc.body;
      let secs = [...main.querySelectorAll("section, article")].filter((s) => !(s.parentElement && s.parentElement.closest("section, article")));
      if (!secs.length) secs = [...main.children];
      const bySel = Object.fromEntries((facts.sections || []).map((s) => [s.index, s.selector]));
      return { height: H, viewport: win.innerHeight, sections: secs.map((s, i) => { const r = s.getBoundingClientRect(); const h = s.querySelector("h1, h2, h3");
        return { index: i + 1, top_pct: ((r.top + win.scrollY) / H) * 100, bottom_pct: ((r.bottom + win.scrollY) / H) * 100, selector: bySel[i + 1] || null,
          label: s.getAttribute("aria-label") || (h ? h.textContent.trim().slice(0, 80) : (s.className || s.tagName)) }; }) };
    } finally { frame.remove(); }
  }

  CAF.agent("experiment_ideation", {
    async run(input, ctx) {
      const cfg = ctx.skill("cro_heuristics");
      const store = XP.store();
      /* ---- inspect page */
      ctx.progress("Inspecting the page");
      const page = ctx.page({ url: input.url, html: input.html });
      let html = "", url = input.url || null;
      if (page && page.status === 200) { html = page.html; url = input.html ? (input.url || null) : page.url; }
      else if (input.url) ctx.warn(`Could not load ${input.url}; ideas use analytics, behaviour and upstream evidence only.`);
      if (page && page.status !== 200) ctx.degraded("page could not be fetched");
      const facts = XP.pageFacts(html, url || "");
      let layout = null;
      if (html && input.measure_layout !== false) {
        try { layout = await measureLayout(html, facts); } catch (e) { ctx.warn(`Layout measurement failed (${e.message}); scroll evidence is not mapped to sections.`); }
      }
      /* ---- evidence */
      ctx.progress("Gathering evidence");
      const sources = facts.ctas.length || facts.h1.length ? ["page"] : [];
      let metrics = { available: false };
      if (ctx.hasData("analytics/pages")) {
        let path = "/"; try { path = new URL(url).pathname || "/"; } catch { /* html input */ }
        metrics = XP.pageAnalytics(await ctx.data("analytics/pages"), path, cfg);
        if (metrics.available) sources.push("analytics"); else ctx.warn("No analytics rows for this page; PIE importance is an estimate.");
      } else { ctx.warn("Analytics data is not embedded; PIE importance is an estimate."); ctx.degraded("no analytics data"); }
      let behaviour = { available: false };
      if (ctx.hasData("behaviour/heatmap_events") && url) {
        const all = await ctx.data("behaviour/heatmap_events");
        const norm = String(url).replace(/\/+$/, "");
        let evs = all.filter((r) => String(r.page_url || "").replace(/\/+$/, "") === norm);
        if (!evs.length) { const p = norm.split("://").pop().split("/").slice(1).join("/"); evs = p ? all.filter((r) => String(r.page_url || "").includes(p)) : []; }
        behaviour = XP.behaviourSummary(evs);
        if (behaviour.available) sources.push("behaviour"); else ctx.warn("No behaviour events recorded for this page.");
      }
      const { found, skipped } = pageFindings(ctx.upstream, url);
      if (found.length) sources.push("upstream");
      if (skipped) ctx.warn(`Ignored ${skipped} upstream finding(s) about a different page.`);
      const evidence = [...pageSignals(facts, cfg), ...analyticsSignals(metrics, cfg), ...behaviourSignals(behaviour, cfg, layout), ...upstreamSignals(found)];
      evidence.forEach((e, i) => { e.id = `E${i + 1}`; });
      /* ---- heuristics + learnings */
      const matches = matchHeuristics(cfg.heuristics || [], evidence);
      ctx.check({ rule: "ideation.evidence", status: evidence.length ? "pass" : "warn", severity: "medium", message: `${evidence.length} evidence items from ${sources.join(", ") || "no source"}.` });
      ctx.check({ rule: "ideation.heuristics", status: matches.length ? "pass" : "warn", severity: "medium", message: `${matches.length} heuristics triggered by the evidence.` });
      const learnings = [...store.learnings.values()];
      const effects = {};
      for (const r of learnings) { const d = { ship: 1, abandon: -1 }[String(r.decision || "").toLowerCase()] || 0; for (const h of r.heuristic_ids || []) effects[h] = (effects[h] || 0) + d; }
      const pk = XP.pageKey(url || "");
      /* ---- generate ideas */
      const want = input.max_ideas || 6;
      const knownH = Object.fromEntries((cfg.heuristics || []).map((h) => [h.id, h]));
      const evIds = new Set(evidence.map((e) => e.id));
      const backlog = [...store.backlog.values()];
      const vars = { page_url: url || "(html input)", goal: input.goal, context: input.context, max_ideas: want, page_lines: XP.pageFactLines(facts),
        evidence_lines: evidence.map((e) => `${e.id} [${e.source}/${e.signal}, ${e.severity}] ${e.detail}`),
        heuristic_lines: matches.map((m) => `${m.heuristic.id} - ${m.heuristic.name}: ${m.heuristic.principle} (evidence: ${m.evidence.map((e) => e.id).join(", ")}; usual metric: ${m.heuristic.primary_metric || ""})`),
        backlog_titles: backlog.filter((r) => r.page === pk).slice(0, 30).map((r) => r.title), learning_lines: learnings.filter((r) => XP.pageKey(r.page_url || "") === pk).slice(0, 10).map((r) => `${r.experiment_id}: ${r.decision} - ${r.headline || ""}`) };
      const ideas = [];
      if (matches.length) {
        ctx.progress("Phrasing ideas");
        const draft = await XP.generateOrNone(ctx, "ideas", vars, "no language model configured: ideas phrased from the heuristic library templates");
        for (const d of (draft && draft.ideas) || []) {
          let hids = (d.heuristic_ids || []).filter((h) => knownH[h]);
          const eids = (d.evidence_ids || []).filter((e) => evIds.has(e));
          if (!hids.length && eids.length) {
            const sims = T().similarity(`${d.title} ${d.change}`, matches.map((m) => `${m.heuristic.name} ${m.heuristic.principle}`));
            const best = sims.length ? sims.indexOf(Math.max(...sims)) : -1;
            if (best >= 0 && sims[best] > 0.05) hids = [matches[best].heuristic.id];
          }
          if (!hids.length || !eids.length) { ctx.warn(`Dropped model idea '${T().truncate(d.title || "", 60)}': it cited no known heuristic or evidence id.`); continue; }
          let hyp = String(d.hypothesis || "").trim();
          if (!/^\s*if\b[\s\S]+\bthen\b[\s\S]+\bbecause\b/i.test(hyp)) hyp = XP.composeHypothesis(d.change, `${d.primary_metric} will improve`, d.rationale);
          ideas.push({ title: String(d.title).trim(), hypothesis: hyp, change: String(d.change || "").trim(), rationale: String(d.rationale || "").trim(), heuristic_ids: hids, evidence_ids: eids,
            primary_metric: String(d.primary_metric || "").trim() || knownH[hids[0]].primary_metric || "", target: d.target_selector || null, origin: "model" });
        }
        const covered = new Set(ideas.flatMap((i) => i.heuristic_ids));
        for (const m of matches) { if (ideas.length >= Math.max(want, 1) * 2) break; if (!covered.has(m.heuristic.id)) { ideas.push(heuristicIdea(m, facts)); covered.add(m.heuristic.id); } }
      } else ctx.warn("No heuristic was triggered by the available evidence; add analytics/behaviour data or upstream audits.");
      /* ---- score + rank */
      const evById = Object.fromEntries(evidence.map((e) => [e.id, e]));
      const scored = [];
      for (const raw of ideas) {
        const evs = raw.evidence_ids.map((e) => evById[e]).filter(Boolean);
        const effect = raw.heuristic_ids.reduce((s, h) => s + (effects[h] || 0), 0);
        const [ice, pie] = XP.icePie(knownH[raw.heuristic_ids[0]], evs, metrics, cfg.scoring || {}, effect);
        const idea = { ...raw, id: ideaHash(pk, raw.heuristic_ids, raw.title), page: pk, ice, pie, evidence: evs.map((e) => ({ id: e.id, source: e.source, signal: e.signal, detail: e.detail, severity: e.severity, target: e.target })) };
        if (scored.some((s) => s.id === idea.id || (s.heuristic_ids.some((h) => idea.heuristic_ids.includes(h)) && jaccard(s.title, idea.title) >= 0.6))) continue;
        scored.push(idea);
      }
      const key = input.rank_by === "pie" ? "pie" : "ice", other = key === "pie" ? "ice" : "pie";
      scored.sort((a, b) => b[key].score - a[key].score || b[other].score - a[other].score || (a.title < b.title ? -1 : 1));
      const top = scored.slice(0, want);
      top.forEach((i, n) => { i.rank = n + 1; i.backlog_status = "not_saved"; });
      /* ---- backlog (page session) */
      let added = 0, dups = 0;
      if (input.persist !== false) {
        const now = U.now();
        const existing = [...store.backlog.values()];
        for (const idea of top) {
          const match = findDuplicate(idea, existing);
          if (match) {
            dups++; idea.backlog_status = "duplicate"; idea.id = match.id;
            const best = (a, b) => (!a || (b && b.score > a.score) ? b : a);
            store.backlog.set(match.id, { ...match, last_seen: now, seen_count: (match.seen_count || 1) + 1, ice: best(match.ice, idea.ice), pie: best(match.pie, idea.pie) });
          } else {
            added++; idea.backlog_status = "new";
            const rec = { id: idea.id, page: pk, page_url: url, title: idea.title, hypothesis: idea.hypothesis, change: idea.change, rationale: idea.rationale, heuristic_ids: idea.heuristic_ids,
              primary_metric: idea.primary_metric, ice: idea.ice, pie: idea.pie, origin: idea.origin, target: idea.target, evidence: idea.evidence.map((e) => e.detail), source_agent: "experiment_ideation",
              status: "new", first_seen: now, last_seen: now, seen_count: 1, run_id: ctx.runId };
            store.backlog.set(idea.id, rec); existing.push(rec);
          }
        }
        ctx.degraded(`backlog kept in the ${XP.STORE_NOTE}`);
      }
      /* ---- report */
      const output = { page_url: url, goal: input.goal || null, ideas: top.map(({ page, ...i }) => i), rank_by: key,
        evidence: evidence.map((e) => ({ id: e.id, source: e.source, signal: e.signal, detail: e.detail, severity: e.severity, target: e.target })),
        heuristics_triggered: matches.map((m) => ({ id: m.heuristic.id, name: m.heuristic.name, signals: [...new Set(m.evidence.map((e) => e.signal))].sort(), evidence_ids: m.evidence.map((e) => e.id) })),
        backlog_size: store.backlog.size, backlog_new: added, backlog_duplicates: dups, page_metrics: metrics, behaviour, sources_used: sources };
      const md = [`# Experiment ideas for ${url || "the page"}`, "", input.goal ? `**Goal:** ${input.goal}\n` : "", `Evidence sources: ${sources.join(", ") || "none"} - ranked by ${key.toUpperCase()} (scores computed in code).`, "",
        "| # | Idea | ICE | PIE | Heuristics | Evidence |", "|---|---|---|---|---|---|",
        ...top.map((i) => `| ${i.rank} | ${i.title} | ${i.ice.score.toFixed(1)} | ${i.pie.score.toFixed(1)} | ${i.heuristic_ids.join(", ")} | ${i.evidence.map((e) => e.id).join(", ")} |`),
        ...top.flatMap((i) => ["", `## ${i.rank}. ${i.title}`, `**Hypothesis:** ${i.hypothesis}`, `**Change:** ${i.change}`, `**Rationale:** ${i.rationale}`, `**Primary metric:** ${i.primary_metric}`,
          `**ICE:** impact ${i.ice.impact}, confidence ${i.ice.confidence}, ease ${i.ice.ease} = ${i.ice.score} - **PIE:** potential ${i.pie.potential}, importance ${i.pie.importance}, ease ${i.pie.ease} = ${i.pie.score}`,
          "**Evidence:**", ...i.evidence.map((e) => `- ${e.id} (${e.source}): ${e.detail}`)]),
        "", "## All evidence", ...output.evidence.map((e) => `- ${e.id} [${e.source}/${e.signal}, ${e.severity}] ${e.detail}`)].join("\n") + "\n";
      ctx.artifact({ name: "ideas.md", mime: "text/markdown", content: md, description: "Ranked experiment ideas" });
      ctx.artifact({ name: "ideas.csv", mime: "text/csv", content: CAF.lib.csv.stringify(top.map((i) => ({ rank: i.rank, id: i.id, title: i.title, primary_metric: i.primary_metric, ice: i.ice.score, pie: i.pie.score,
        heuristics: i.heuristic_ids.join(" "), evidence: i.evidence.map((e) => e.id).join(" ") }))), description: "Ideas (CSV)" });
      ctx.artifact({ name: "ideas.json", mime: "application/json", content: JSON.stringify(output, null, 2), description: "Ideas with evidence and scores" });
      return {
        output,
        summary: `${top.length} ranked test ideas from ${evidence.length} evidence items (${sources.join(", ") || "no data"}); top by ${key.toUpperCase()}: '${T().truncate(top.length ? top[0].title : "none", 70)}'; backlog ${store.backlog.size} (${added} new)`,
        view: [{ type: "table", title: "Ranked ideas (ICE / PIE computed in code)", rows: top.map((i) => ({ "#": i.rank, idea: i.title, ICE: i.ice.score, PIE: i.pie.score, heuristics: i.heuristic_ids.join(", "), evidence: i.evidence.map((e) => e.id).join(", ") })) },
          { type: "table", title: "Evidence", rows: output.evidence.map((e) => ({ id: e.id, source: e.source, signal: e.signal, detail: e.detail })) },
          { type: "markdown", title: "Ideas", markdown: md }],
      };
    },
    selftest() { return { url: DEMO_URL, goal: "More home insurance quote starts", max_ideas: 6 }; },
  });
})();
