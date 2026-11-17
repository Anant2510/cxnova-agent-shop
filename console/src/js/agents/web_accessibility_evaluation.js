/* Web Accessibility Evaluation - browser twin of caf/agents/web/web_accessibility_evaluation.py.
   Same stages (scan -> map -> remediate -> ci_gate -> report). Detection: axe-core (the same 4.10.2
   build the Python agent vendors) runs inside a same-origin iframe rendered from the page HTML with
   real layout and computed styles (CAF.lib.html.renderInFrame + an injected <script src=axe_url>), for
   the requested WCAG tags. If axe cannot load, the port of the 12-rule static fallback runs instead and
   the run is degraded. Mapping to WCAG success criteria, scoring, fixes and the CI gate / JUnit XML come
   from the shared wcag_criteria skill and the same pure functions; the model only writes fix text. */
(() => {
  const AGENT = "web_accessibility_evaluation";
  const IMPACT_ORDER = ["minor", "moderate", "serious", "critical"];
  const LEVEL_TAG = { wcag2a: "A", wcag2aa: "AA", wcag2aaa: "AAA", wcag21a: "A", wcag21aa: "AA", wcag21aaa: "AAA", wcag22a: "A", wcag22aa: "AA", wcag22aaa: "AAA" };
  const SEV = { critical: "critical", serious: "high", moderate: "medium", minor: "low" };
  const LANG_RX = /^[a-zA-Z]{2,3}(-[a-zA-Z0-9]{2,8})*$/;

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
  const pyFloat = (x) => (Number.isInteger(x) ? x.toFixed(1) : String(x));
  const capitalize = (s) => (s ? s[0].toUpperCase() + s.slice(1).toLowerCase() : s);

  // ------------------------------------------------------------------ WCAG mapping (pure)
  function scFromTag(tag) { const m = /^wcag(\d)(\d)(\d{1,2})$/.exec(tag || ""); return m ? `${m[1]}.${m[2]}.${parseInt(m[3], 10)}` : null; }
  function wcagRefs(tags, criteria) {
    const level = (tags.find((t) => LEVEL_TAG[t]) && LEVEL_TAG[tags.find((t) => LEVEL_TAG[t])]) || "";
    return tags.map(scFromTag).filter(Boolean).map((sc) => { const c = criteria[sc]; return { sc, name: c ? c[0] : "Unknown success criterion", level: c ? c[1] : level }; });
  }
  const impactRank = (i) => Math.max(0, IMPACT_ORDER.indexOf(i));

  // ------------------------------------------------------------------ colour contrast (pure)
  const NAMED = { black: "#000000", white: "#ffffff", red: "#ff0000", green: "#008000", blue: "#0000ff", gray: "#808080", grey: "#808080", silver: "#c0c0c0", yellow: "#ffff00", orange: "#ffa500", navy: "#000080", transparent: null };
  function parseColor(value) {
    if (!value) return null;
    let v = String(value).trim().toLowerCase();
    if (v in NAMED) { v = NAMED[v] || ""; if (!v) return null; }
    let m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/.exec(v);
    if (m) { let h = m[1]; if (h.length === 3) h = h.split("").map((c) => c + c).join(""); return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16) / 255); }
    m = /^rgba?\(\s*(\d+)[ ,]+(\d+)[ ,]+(\d+)(?:[ ,/]+([\d.]+))?\s*\)$/.exec(v);
    if (m) { if (m[4] !== undefined && parseFloat(m[4]) < 1) return null; return [1, 2, 3].map((i) => parseInt(m[i], 10) / 255); }
    return null;
  }
  const relLum = (rgb) => { const ch = (c) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4); const [r, g, b] = rgb.map(ch); return 0.2126 * r + 0.7152 * g + 0.0722 * b; };
  const contrastRatio = (fg, bg) => { const [l1, l2] = [relLum(fg), relLum(bg)].sort((a, b) => b - a); return (l1 + 0.05) / (l2 + 0.05); };
  function passingColor(fgHex, bgHex, target) {
    const fg = parseColor(fgHex), bg = parseColor(bgHex);
    if (!fg || !bg) return null;
    const toward = relLum(bg) > 0.18 ? [0, 0, 0] : [1, 1, 1];
    for (let i = 1; i <= 100; i++) {
      const c = fg.map((f, k) => f + ((toward[k] - f) * i) / 100);
      if (contrastRatio(c, bg) >= target + 0.05) return "#" + c.map((x) => Number(pyFixed(x * 255, 0)).toString(16).padStart(2, "0")).join("");
    }
    return null;
  }
  function humanizeHref(href) {
    const seg = String(href || "").split(/[/?#]/).filter((x) => x && !x.startsWith("http"));
    const word = (seg.length ? seg[seg.length - 1] : "home").replace(/\.(html?|php|aspx?)$/, "");
    return capitalize(word.replace(/[-_]+/g, " ").trim()) || "Home";
  }
  const addAttr = (snippet, tag, attr, value) => snippet.replace(new RegExp(`<${tag}\\b`), `<${tag} ${attr}="${value}"`);
  /** Deterministic, element-specific fix for common rules (offline remediation that refers to the real markup). */
  function specificFix(v, brand = "") {
    const html = (v.html_snippets || [""])[0] || "";
    const rid = v.id, fs = v.failure_summary || "";
    if (rid === "color-contrast") {
      const mFg = /foreground colou?r: (#[0-9a-fA-F]{3,6})/.exec(fs), mBg = /background colou?r: (#[0-9a-fA-F]{3,6})/.exec(fs), mT = /Expected contrast ratio of ([\d.]+):1/.exec(fs);
      if (mFg && mBg) {
        const target = mT ? parseFloat(mT[1]) : 4.5;
        const nw = passingColor(mFg[1], mBg[1], target);
        if (nw) {
          const ratio = contrastRatio(parseColor(nw), parseColor(mBg[1]));
          const sel = (v.targets || ["element"])[0];
          return `/* ${mFg[1]} on ${mBg[1]} fails ${pyFloat(target)}:1 - closest passing colour is ${nw} (${pyFixed(ratio, 2)}:1) */\n${sel} { color: ${nw}; }`;
        }
      }
    }
    if ((rid === "image-alt" || rid === "input-image-alt") && html) return addAttr(html, rid === "image-alt" ? "img" : "input", "alt", "[describe the image, or leave empty if decorative]");
    if (rid === "link-name" && html) { const href = /href="([^"]*)"/.exec(html); return addAttr(html, "a", "aria-label", humanizeHref(href ? href[1] : "")); }
    if (rid === "button-name" && html) return html.startsWith("<button") ? addAttr(html, "button", "aria-label", "[describe the action]") : null;
    if ((rid === "label" || rid === "select-name") && html) {
      const name = /name="([^"]+)"/.exec(html);
      const fid = (name ? name[1] : "field").replace(/ /g, "-");
      const tag = html.startsWith("<select") ? "select" : html.startsWith("<textarea") ? "textarea" : "input";
      const field = html.includes('id="') ? html : addAttr(html, tag, "id", fid);
      return `<label for="${fid}">${capitalize(fid.replace(/-/g, " ").replace(/_/g, " "))}</label>\n${field}`;
    }
    if (rid === "heading-order" && html) {
      const m = /^<h([1-6])\b/.exec(html);
      if (m) {
        const lvl = Number(m[1]);
        const prev = /h(\d) followed by h(\d)/.exec(fs);
        const nw = prev ? Number(prev[1]) + 1 : Math.max(1, lvl - 1);
        const fixed = html.replace(new RegExp(`^<h${lvl}\\b`), `<h${nw} class="h${lvl}-style"`).split(`</h${lvl}>`).join(`</h${nw}>`);
        return `${fixed}\n/* keep the visual size with CSS: .h${lvl}-style { font-size: <h${lvl} size>; } */`;
      }
    }
    if (rid === "html-has-lang") return '<html lang="en">';
    if (rid === "document-title") return `<title>[Page topic] | ${brand || "[Site name]"}</title>`;
    return null;
  }

  // ------------------------------------------------------------------ static fallback (port of static_scan)
  const STATIC_RULES = {
    "image-alt": { impact: "critical", tags: ["cat.text-alternatives", "wcag2a", "wcag111", "section508"], help: "Images must have alternative text" },
    "input-image-alt": { impact: "critical", tags: ["cat.text-alternatives", "wcag2a", "wcag111", "wcag412"], help: "Image buttons must have alternative text" },
    label: { impact: "critical", tags: ["cat.forms", "wcag2a", "wcag412"], help: "Form elements must have labels" },
    "select-name": { impact: "critical", tags: ["cat.forms", "wcag2a", "wcag412"], help: "Select element must have an accessible name" },
    "button-name": { impact: "critical", tags: ["cat.name-role-value", "wcag2a", "wcag412"], help: "Buttons must have discernible text" },
    "link-name": { impact: "serious", tags: ["cat.name-role-value", "wcag2a", "wcag244", "wcag412"], help: "Links must have discernible text" },
    "html-has-lang": { impact: "serious", tags: ["cat.language", "wcag2a", "wcag311"], help: "<html> element must have a lang attribute" },
    "html-lang-valid": { impact: "serious", tags: ["cat.language", "wcag2a", "wcag311"], help: "<html> element must have a valid value for the lang attribute" },
    "document-title": { impact: "serious", tags: ["cat.text-alternatives", "wcag2a", "wcag242"], help: "Documents must have <title> element to aid in navigation" },
    "frame-title": { impact: "serious", tags: ["cat.text-alternatives", "wcag2a", "wcag412"], help: "Frames must have an accessible name" },
    "color-contrast": { impact: "serious", tags: ["cat.color", "wcag2aa", "wcag143"], help: "Elements must meet minimum color contrast ratio thresholds" },
    "heading-order": { impact: "moderate", tags: ["cat.semantics", "best-practice"], help: "Heading levels should only increase by one" },
  };
  const TEXT_TAGS = "p,span,a,li,h1,h2,h3,h4,h5,h6,td,th,label,button,div,small,strong,em,figcaption,blockquote";
  const classes = (el) => String(el.getAttribute("class") || "").split(/\s+/).filter(Boolean);
  function cssPath(el) {
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && parts.length < 5) {
      const tag = node.localName;
      if (node.getAttribute("id")) { parts.push(`${tag}#${node.getAttribute("id")}`); break; }
      const cls = classes(node).filter((c) => /^[A-Za-z_-][\w-]*$/.test(c)).slice(0, 2);
      let sel = tag + cls.map((c) => `.${c}`).join("");
      const parent = node.parentNode;
      if (parent && parent.nodeType === 1) {
        const same = [...parent.children].filter((s) => s.localName === tag);
        const alike = same.filter((s) => JSON.stringify(classes(s).filter((c) => cls.includes(c))) === JSON.stringify(cls));
        if (same.length > 1 && (!cls.length || alike.length > 1)) sel += `:nth-of-type(${same.indexOf(node) + 1})`;
      }
      parts.push(sel);
      if (tag === "html" || tag === "body") break;
      node = parent;
    }
    return parts.reverse().join(" > ");
  }
  function snippet(el, limit = 220) {
    const s = el.outerHTML;
    if (s.length <= limit) return s;
    const m = /^<[^>]+>/.exec(s);
    return (m ? m[0] : s.slice(0, limit)) + "...";
  }
  const hiddenUp = (n) => { for (let p = n.nodeType === 1 ? n : n.parentElement; p; p = p.parentElement) if (p.getAttribute("aria-hidden") === "true") return true; return false; };
  const bsText = (el) => { const out = []; const walk = (n) => { for (const c of n.childNodes) { if (c.nodeType === 3) { const t = c.nodeValue.trim(); if (t) out.push(t); } else if (c.nodeType === 1) walk(c); } }; if (el) walk(el); return out.join(" "); };
  /** Accessible-name approximation: text, img alt and svg <title>, skipping aria-hidden subtrees. */
  function textName(el) {
    const parts = [];
    const walk = (n) => {
      for (const c of n.childNodes) {
        if (c.nodeType === 1) {
          if (c.getAttribute("aria-hidden") !== "true") {
            if (c.localName === "img" && c.getAttribute("alt") && !hiddenUp(c.parentElement || c)) parts.push(c.getAttribute("alt"));
            if (c.localName === "title" && c.parentElement && c.parentElement.localName === "svg" && c.parentElement.getAttribute("aria-hidden") !== "true") parts.push(c.textContent);
          }
          walk(c);
        } else if (c.nodeType === 3 && c.nodeValue.trim() && !hiddenUp(c)) parts.push(c.nodeValue.trim());
      }
    };
    walk(el);
    return parts.join(" ").split(/\s+/).filter(Boolean).join(" ");
  }
  function accessibleName(el, doc) {
    if (el.getAttribute("aria-labelledby")) {
      const txt = el.getAttribute("aria-labelledby").split(/\s+/).filter(Boolean).map((i) => { const t = doc.getElementById(i); return t ? bsText(t) : ""; }).join(" ");
      if (txt.trim()) return txt.trim();
    }
    if ((el.getAttribute("aria-label") || "").trim()) return el.getAttribute("aria-label").trim();
    if (["input", "select", "textarea"].includes(el.localName)) {
      if (el.getAttribute("id")) {
        const lab = [...doc.querySelectorAll("label[for]")].find((l) => l.getAttribute("for") === el.getAttribute("id"));
        if (lab && bsText(lab)) return bsText(lab);
      }
      const wrap = el.parentElement ? el.parentElement.closest("label") : null;
      if (wrap && bsText(wrap)) return bsText(wrap);
      const typ = String(el.getAttribute("type") || "").toLowerCase();
      if (typ === "submit" || typ === "reset") return el.getAttribute("value") || capitalize(typ);
      if (typ === "button" && el.getAttribute("value")) return el.getAttribute("value");
      if (typ === "image" && el.getAttribute("alt")) return el.getAttribute("alt");
      return (el.getAttribute("title") || el.getAttribute("placeholder") || "").trim();
    }
    return textName(el) || (el.getAttribute("title") || "").trim();
  }
  const inlineStyle = (el) => { const out = {}; for (const decl of String(el.getAttribute("style") || "").split(";")) { const i = decl.indexOf(":"); if (i >= 0) out[decl.slice(0, i).trim().toLowerCase()] = decl.slice(i + 1).trim().toLowerCase(); } return out; };
  function fontPx(style, tag) {
    const m = /^([\d.]+)(px|pt|rem|em)$/.exec(style["font-size"] || "");
    if (m) return parseFloat(m[1]) * { px: 1, pt: 4 / 3, rem: 16, em: 16 }[m[2]];
    return { h1: 32, h2: 24, h3: 18.72 }[tag] || 16;
  }
  function staticScan(html, includeTags) {
    const doc = CAF.lib.html.parse(html || "");
    const found = Object.fromEntries(Object.keys(STATIC_RULES).map((k) => [k, []]));
    const applicable = Object.fromEntries(Object.keys(STATIC_RULES).map((k) => [k, 0]));
    const incomplete = [];
    const hit = (rule, el, summary) => found[rule].push({ target: [cssPath(el)], html: snippet(el), failureSummary: summary, impact: STATIC_RULES[rule].impact });
    for (const img of doc.querySelectorAll("img")) {
      applicable["image-alt"]++;
      if (!img.hasAttribute("alt") && !img.getAttribute("aria-label") && !img.getAttribute("aria-labelledby") && !["presentation", "none"].includes(img.getAttribute("role"))) hit("image-alt", img, "Element does not have an alt attribute");
    }
    for (const inp of [...doc.querySelectorAll("input[type]")].filter((i) => /^image$/i.test(i.getAttribute("type")))) {
      applicable["input-image-alt"]++;
      if (!accessibleName(inp, doc)) hit("input-image-alt", inp, "Element has no alt attribute");
    }
    for (const el of doc.querySelectorAll("input,textarea")) {
      const typ = String(el.getAttribute("type") || "text").toLowerCase();
      if (el.localName === "input" && ["hidden", "submit", "reset", "button", "image"].includes(typ)) continue;
      applicable.label++;
      if (!accessibleName(el, doc)) hit("label", el, "Form element does not have an implicit (wrapped) <label>, an explicit <label>, aria-label or aria-labelledby");
    }
    for (const el of doc.querySelectorAll("select")) { applicable["select-name"]++; if (!accessibleName(el, doc)) hit("select-name", el, "Element does not have an accessible name"); }
    const buttons = [...doc.querySelectorAll("button"), ...doc.querySelectorAll('[role="button"]'), ...[...doc.querySelectorAll("input[type]")].filter((i) => /^button$/i.test(i.getAttribute("type")))];
    for (const el of buttons) { applicable["button-name"]++; if (!accessibleName(el, doc)) hit("button-name", el, "Element does not have inner text that is visible to screen readers"); }
    for (const a of doc.querySelectorAll("a[href]")) { applicable["link-name"]++; if (!accessibleName(a, doc)) hit("link-name", a, "Element does not have text that is visible to screen readers"); }
    const htmlEl = doc.documentElement;
    if (htmlEl) {
      applicable["html-has-lang"]++;
      const lang = (htmlEl.getAttribute("lang") || "").trim();
      if (!lang) hit("html-has-lang", htmlEl, "The <html> element does not have a lang attribute");
      else { applicable["html-lang-valid"]++; if (!LANG_RX.test(lang)) hit("html-lang-valid", htmlEl, `Value of lang attribute '${lang}' is not valid`); }
      applicable["document-title"]++;
      const t = doc.querySelector("title");
      if (!t || !bsText(t)) hit("document-title", htmlEl, "Document does not have a non-empty <title> element");
    }
    for (const fr of doc.querySelectorAll("iframe,frame")) { applicable["frame-title"]++; if (!(fr.getAttribute("title") || "").trim() && !fr.getAttribute("aria-label")) hit("frame-title", fr, "Element has no title attribute"); }
    let prev = null;
    for (const h of doc.querySelectorAll("h1,h2,h3,h4,h5,h6")) {
      applicable["heading-order"]++;
      const lvl = Number(h.localName[1]);
      if (prev !== null && lvl > prev + 1) hit("heading-order", h, `Heading order invalid (h${prev} followed by h${lvl})`);
      prev = lvl;
    }
    for (const el of doc.querySelectorAll(TEXT_TAGS)) {
      const st = inlineStyle(el);
      if (!("color" in st) || !bsText(el)) continue;
      const fg = parseColor(st.color);
      let bgVal = null;
      for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
        const s2 = inlineStyle(node);
        bgVal = s2["background-color"] || (s2.background ? s2.background.split(/\s+/)[0] : null);
        if (bgVal) break;
      }
      const bg = bgVal ? parseColor(bgVal) : [1, 1, 1];
      if (!fg || !bg) { incomplete.push({ id: "color-contrast", target: [cssPath(el)], html: snippet(el) }); continue; }
      applicable["color-contrast"]++;
      const ratio = contrastRatio(fg, bg), px = fontPx(st, el.localName);
      const bold = ["bold", "bolder", "700", "800", "900"].includes(st["font-weight"] || "") || ["h1", "h2", "h3", "strong", "b"].includes(el.localName);
      const need = px >= 24 || (px >= 18.66 && bold) ? 3 : 4.5;
      if (ratio < need) hit("color-contrast", el, `Element has insufficient color contrast of ${pyFixed(ratio, 2)} (foreground color: ${st.color}, background color: ${bgVal || "#ffffff (assumed)"}). Expected contrast ratio of ${pyFloat(need)}:1`);
    }
    const violations = [], passes = [];
    let inapplicable = 0;
    for (const [rid, meta] of Object.entries(STATIC_RULES)) {
      if (!meta.tags.some((t) => includeTags.has(t))) continue;
      if (found[rid].length) violations.push({ id: rid, impact: meta.impact, tags: meta.tags, help: meta.help, description: meta.help, helpUrl: `https://dequeuniversity.com/rules/axe/4.10/${rid}`, nodes: found[rid] });
      else if (applicable[rid]) passes.push({ id: rid, nodes: applicable[rid] });
      else inapplicable++;
    }
    return { violations, passes, incomplete: incomplete.length ? [{ id: "color-contrast", nodes: incomplete.length }] : [], inapplicable, testEngine: { name: "caf-static", version: "1.0" } };
  }

  // ------------------------------------------------------------------ axe-core in a same-origin iframe
  const BLANK_GIF = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7";
  const isInline = (u) => /^\s*(data|blob):/i.test(String(u || ""));
  /** Pasted HTML runs in the console's own origin: drop its scripts and event handlers, and neutralise
      external resources (the page cannot fetch them anyway) so nothing untrusted executes or loads. */
  function sanitizeForFrame(html) {
    const doc = CAF.lib.html.parse(html);
    const external = doc.querySelectorAll('script, link[rel~="stylesheet"]:not([href^="data:"])').length;
    const doctype = /^\s*<!doctype/i.test(html) ? "<!DOCTYPE html>" : "";
    doc.querySelectorAll("script, base, meta[http-equiv]").forEach((e) => e.remove());
    for (const el of doc.querySelectorAll("*")) for (const a of [...el.attributes]) if (/^on/i.test(a.name)) el.removeAttribute(a.name);
    doc.querySelectorAll("link[href]").forEach((l) => { if (!isInline(l.getAttribute("href")) && /(^|\s)(stylesheet|icon|preload|prefetch|modulepreload|manifest|apple-touch-icon|mask-icon)(\s|$)/i.test(l.getAttribute("rel") || "")) l.remove(); });
    doc.querySelectorAll("img[src], input[src]").forEach((i) => { if (!isInline(i.getAttribute("src"))) i.setAttribute("src", BLANK_GIF); });
    doc.querySelectorAll("[srcset]").forEach((i) => i.removeAttribute("srcset"));
    doc.querySelectorAll("iframe[src], frame[src]").forEach((f) => f.setAttribute("src", "about:blank"));
    doc.querySelectorAll("video, audio, source, track, embed, object").forEach((m) => { for (const k of ["src", "poster", "data"]) m.removeAttribute(k); });
    const cssUrls = (css) => String(css).replace(/@import[^;]+;/gi, "").replace(/url\(\s*(['"]?)(?!data:|blob:)[^)'"]*\1\s*\)/gi, "none");
    doc.querySelectorAll("style").forEach((st) => { st.textContent = cssUrls(st.textContent); });
    doc.querySelectorAll("[style]").forEach((el) => el.setAttribute("style", cssUrls(el.getAttribute("style"))));
    return { html: doctype + doc.documentElement.outerHTML, external };
  }
  function withAxe(html, axeUrl) {
    const tag = `<script src="${axeUrl}"></script>`;
    if (/<head[^>]*>/i.test(html)) return html.replace(/<head[^>]*>/i, (m) => m + tag);
    if (/<html[^>]*>/i.test(html)) return html.replace(/<html[^>]*>/i, (m) => `${m}<head>${tag}</head>`);
    return `<!doctype html><html><head>${tag}</head><body>${html}</body></html>`;
  }
  async function runAxe(html, tags, width, height) {
    const axeUrl = (CAF.boot || {}).axe_url;
    if (!axeUrl) throw new Error("axe-core URL is not configured in this console build");
    let frame = null;
    try {
      frame = await CAF.lib.html.renderInFrame(withAxe(html, axeUrl), { width, height });
      for (let i = 0; i < 40 && !(frame.win && frame.win.axe); i++) await new Promise((r) => setTimeout(r, 75));
      if (!frame.win || !frame.win.axe) throw new Error("axe-core did not load in the page frame");
      const r = await frame.win.axe.run(frame.doc, { runOnly: { type: "tag", values: tags }, resultTypes: ["violations", "incomplete"] });
      return JSON.parse(JSON.stringify({ violations: r.violations, testEngine: r.testEngine, url: r.url, passes: r.passes.map((p) => ({ id: p.id, nodes: p.nodes.length })),
        incomplete: r.incomplete.map((p) => ({ id: p.id, impact: p.impact, nodes: p.nodes.length, help: p.help })), inapplicable: r.inapplicable.length }));
    } finally { if (frame) frame.remove(); }
  }

  // ------------------------------------------------------------------ normalise, score, CI (pure)
  /** The console embeds demo-site images as data: URIs; keep failing-markup snippets readable. */
  const tidyDataUris = (h) => h.replace(/(src|href|srcset)="data:([^;,"]+)[^"]*"/gi, (m, attr, mime) => `${attr}="data:${mime},..."`);
  const flattenTarget = (t) => (Array.isArray(t) ? t.map(flattenTarget).join(" >> ") : String(t));
  function normaliseViolations(raw, criteria, failOn) {
    const out = raw.map((v) => {
      const nodes = v.nodes || [];
      const impact = v.impact || nodes.map((n) => n.impact || "minor").reduce((b, i) => (impactRank(i) > impactRank(b) ? i : b), "minor");
      const tags = v.tags || [];
      return { id: v.id, impact, tags, wcag: wcagRefs(tags, criteria), help: v.help || "", description: v.description || "", help_url: v.helpUrl || "", nodes_count: nodes.length,
        targets: nodes.map((n) => flattenTarget(n.target)).slice(0, 10), html_snippets: nodes.map((n) => tidyDataUris(String(n.html || "")).slice(0, 300)).slice(0, 5),
        failure_summary: nodes.length ? String(nodes[0].failureSummary || "").slice(0, 600) : "", best_practice: tags.includes("best-practice") && !tags.some(scFromTag),
        blocking: impactRank(impact) >= impactRank(failOn) };
    });
    return out.sort((a, b) => impactRank(b.impact) - impactRank(a.impact) || b.nodes_count - a.nodes_count || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
  }
  function a11yScore(violations, passesCount, scoring) {
    const pw = Number(scoring.pass_weight ?? 3), vw = scoring.violation_weights || { critical: 10, serious: 7, moderate: 3, minor: 1 };
    const bad = violations.reduce((s, v) => s + Number(vw[v.impact] ?? 3), 0), good = pw * passesCount;
    return good + bad ? Number(pyFixed((100 * good) / (good + bad), 1)) : 100;
  }
  // JUnit XML serialised like xml.etree.ElementTree + ET.indent (two-space indent, " />" for empty elements)
  const xmlAttr = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/\r/g, "&#13;").replace(/\n/g, "&#10;").replace(/\t/g, "&#09;");
  const xmlText = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const X = (tag, attrs = {}, children = [], text = null) => ({ tag, attrs, children, text });
  function xmlSerialize(node, level = 0) {
    const pad = (n) => "\n" + "  ".repeat(n);
    const open = `<${node.tag}${Object.entries(node.attrs).map(([k, v]) => ` ${k}="${xmlAttr(v)}"`).join("")}`;
    if (!node.children.length) return node.text === null || node.text === "" ? `${open} />` : `${open}>${xmlText(node.text)}</${node.tag}>`;
    return `${open}>` + node.children.map((c) => pad(level + 1) + xmlSerialize(c, level + 1)).join("") + pad(level) + `</${node.tag}>`;
  }
  function junitXml(url, standardLabel, engine, violations, passes, failOn, timestamp) {
    const total = violations.length + passes.length, failures = violations.filter((v) => v.blocking).length;
    const cases = violations.map((v) => {
      const sc = v.wcag.length ? v.wcag[0] : null;
      const cls = sc ? `a11y.wcag.${sc.sc.replace(/\./g, "_")}` : "a11y.best-practice";
      const ref = sc ? ` [${sc.sc} ${sc.level}]` : " [best practice]";
      const snips = [...v.html_snippets, ...v.targets.map(() => "")];
      const body = v.targets.map((t, i) => `- ${t}\n  ${snips[i]}`).join("\n");
      const child = v.blocking ? X("failure", { type: v.impact, message: `${v.nodes_count} element(s): ${v.help}` }, [], `${v.failure_summary}\n\n${body}\n\nFix: ${v.explanation || ""}`)
        : X("system-out", {}, [], `${v.impact} violation below fail_on=${failOn}: ${v.nodes_count} element(s)\n${body}`);
      return X("testcase", { classname: cls, name: `${v.id}: ${v.help}${ref}`, time: "0" }, [child]);
    });
    const props = X("properties", {}, [["engine", engine], ["standard", standardLabel], ["fail_on", failOn], ["url", url]].map(([k, v]) => X("property", { name: k, value: v })));
    const suite = X("testsuite", { name: `${standardLabel}: ${url}`, tests: String(total), failures: String(failures), errors: "0", skipped: "0", timestamp }, [props, ...cases, ...passes.map((p) => X("testcase", { classname: "a11y.pass", name: p, time: "0" }))]);
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + xmlSerialize(X("testsuites", { name: "accessibility", tests: String(total), failures: String(failures), errors: "0" }, [suite]));
  }

  // ------------------------------------------------------------------ HTML report (REPORT_TEMPLATE)
  const E = U.esc;
  function renderReport(c) {
    const sections = c.violations.map((v) => `<section><h2><span class="imp ${E(v.impact)}">${E(v.impact)}</span> ${E(v.help)} <code class="muted">${E(v.id)}</code></h2>
<p>${v.wcag.map((w) => `<span class="sc">WCAG ${E(w.sc)} ${E(w.name)} (${E(w.level)})</span>`).join("")}${v.best_practice ? '<span class="sc">best practice</span>' : ""}
<span class="muted">${v.nodes_count} element(s)${v.blocking ? " &middot; blocks CI" : ""}</span></p>
<p>${E(v.explanation)}</p><ul class="t">${v.targets.map((t) => `<li><code>${E(t)}</code></li>`).join("")}</ul>
${v.html_snippets.length ? `<p class="muted">Failing markup</p><pre>${E(v.html_snippets.join("\n"))}</pre>` : ""}
${v.fix_snippet ? `<p class="muted">Suggested fix</p><pre>${E(v.fix_snippet)}</pre>` : ""}</section>`).join("");
    return `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Accessibility report - ${E(c.url)}</title>
<style>
:root{--ink:#18212f;--muted:#5d6676;--line:#e2e5eb;--bg:#f5f6f8;--card:#fff;--critical:#b42318;--serious:#c4320a;--moderate:#b54708;--minor:#475467;--ok:#067647;--accent:#0b4a6f}
*{box-sizing:border-box}body{margin:0;font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:var(--ink);background:var(--bg)}
header{background:var(--accent);color:#fff;padding:28px 32px 64px}header h1{margin:0 0 4px;font-size:24px}header p{margin:0;opacity:.88;word-break:break-all}
main{max-width:1120px;margin:-44px auto 0;padding:0 20px 48px}section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:20px 22px;margin-bottom:18px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:18px}.tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.tile b{display:block;font-size:26px}.tile span{color:var(--muted);font-size:13px}h2{font-size:18px;margin:0 0 10px}
.gate{font-weight:700;font-size:15px;padding:4px 12px;border-radius:8px;display:inline-block}.gate.pass{background:#ecfdf3;color:var(--ok)}.gate.fail{background:#fef3f2;color:var(--critical)}
.imp{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;font-weight:700;color:#fff}.critical{background:var(--critical)}.serious{background:var(--serious)}.moderate{background:var(--moderate)}.minor{background:var(--minor)}
.sc{display:inline-block;border:1px solid var(--line);border-radius:6px;padding:0 6px;font-size:12px;margin-right:4px}
pre{background:#0f172a;color:#e2e8f0;padding:10px 12px;border-radius:8px;overflow:auto;font-size:12.5px;white-space:pre-wrap}code{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12.5px}
.muted{color:var(--muted)}.note{font-size:13px;color:var(--muted)}ul.t{margin:4px 0 8px 18px;padding:0}
</style></head><body>
<header><h1>Accessibility evaluation</h1><p>${E(c.url)} &middot; ${E(c.standard_label)} &middot; ${E(c.engine)} &middot; ${E(c.generated)}</p></header>
<main>
<div class="tiles"><div class="tile"><b>${E(pyFloat(c.score))}</b><span>accessibility score</span></div><div class="tile"><b>${c.violations.length}</b><span>violated rules</span></div>
<div class="tile"><b>${c.nodes}</b><span>failing elements</span></div><div class="tile"><b>${c.passes_count}</b><span>rules passed</span></div>
<div class="tile"><b>${c.incomplete_count}</b><span>need manual review</span></div></div>
<section><h2>CI gate</h2><span class="gate ${c.ci_pass ? "pass" : "fail"}">${c.ci_pass ? "PASS" : "FAIL"}</span>
<span class="muted"> fail_on = ${E(c.fail_on)}: ${c.blocking_count} blocking violation type(s).</span>
<p class="note">Automated testing finds roughly a third to a half of WCAG issues; keyboard, screen-reader and cognitive reviews are still needed. Remediation: ${E(c.remediation_label)}.</p></section>
${sections}
${c.violations.length ? "" : `<section><p>No violations found for ${E(c.standard_label)}.</p></section>`}
</main></body></html>`;
  }

  // ------------------------------------------------------------------ the agent
  CAF.agent(AGENT, {
    async run(input, ctx) {
      if (!input.url && !(input.html && String(input.html).trim())) throw new Error("Provide url or html.");
      // 1. scan
      const skill = ctx.skill("wcag_criteria");
      const std = skill.standards[input.standard || "wcag21aa"];
      const tags = [...std.tags, ...(input.include_best_practices !== false ? ["best-practice"] : [])];
      const page = ctx.page({ url: input.url, html: input.html });
      if (!page) throw new Error(`The hosted console cannot fetch ${input.url}: paste the page HTML into the html field (axe-core then runs on it here), or run the Python service.`);
      if (page.status && page.status >= 400) ctx.warn(`Could not fetch ${input.url}: ${page.status}`);
      let html = page.html || "";
      if (page.source === "html input") {
        const clean = sanitizeForFrame(html);
        html = clean.html;
        if (clean.external) ctx.degraded("pasted HTML: page scripts and external stylesheets not loaded - CSS/JS-dependent results (e.g. contrast) may differ");
      }
      const width = Number(input.viewport_width || 1280), height = Number(input.viewport_height || 900);
      let raw = null, engine = "";
      const wantAxe = ["auto", "axe"].includes(input.engine || "auto");
      if (wantAxe) {
        try {
          ctx.progress("Running axe-core in a rendered copy of the page");
          raw = await runAxe(html, tags, width, height);
          engine = `axe-core ${(raw.testEngine || {}).version || "4.10.2"} (in-page iframe)`;
        } catch (e) {
          ctx.warn(`axe-core could not run in the page (${(e && e.message) || e}); used static checks instead.`);
          ctx.degraded("axe-core unavailable: static fallback checks (no CSS/JS evaluation)");
        }
      }
      if (!raw) {
        raw = staticScan(page.html || "", new Set(tags));
        engine = "static fallback checks (caf-static 1.0)";
        ctx.warn(`Static checks cover ${Object.keys(STATIC_RULES).length} rules (axe-core runs ~90): the score is not comparable with an axe-based score.`);
        if (input.engine === "static") ctx.degraded("static checks requested: CSS-dependent issues (contrast, focus, reflow) not fully evaluated");
      }
      if (page.source === "html input") ctx.warn("Pasted HTML: its scripts are not executed and external stylesheets, fonts and images are not loaded in the console, so results reflect the static markup with inline and embedded CSS only.");
      // 2. map
      const failOn = input.fail_on || "serious";
      const violations = normaliseViolations(raw.violations || [], skill.criteria || {}, failOn);
      const fixes = skill.rule_fixes || {};
      const brand = ctx.brand.name || "";
      for (const v of violations) {
        const f = fixes[v.id] || fixes._default || {};
        v.explanation = f.fix || v.help;
        v.fix_snippet = specificFix(v, brand) || String(f.snippet || "").trim();
      }
      const passes = (raw.passes || []).map((p) => p.id);
      const incomplete = raw.incomplete || [];
      for (const v of violations) ctx.check({ rule: v.id, status: v.blocking ? "fail" : "warn", severity: SEV[v.impact], message: `${v.help} (${v.nodes_count} element(s))`,
        ref: v.wcag.map((w) => `WCAG ${w.sc} (${w.level})`).join("; ") || "axe best practice", target: v.targets[0] || input.url, data: { impact: v.impact, nodes: v.nodes_count } });
      ctx.check({ rule: "axe.passes", status: "pass", severity: "low", message: `${passes.length} rule(s) passed` });
      if (incomplete.length) ctx.check({ rule: "axe.incomplete", status: "info", severity: "medium", message: `${incomplete.length} rule(s) need manual review: ` + incomplete.slice(0, 8).map((i) => i.id).join(", ") });
      // 3. remediate (model writes fix text; detection and scoring stay in code)
      let source = "rule_library";
      if (violations.length) {
        const items = violations.slice(0, 15).map((v) => ({ rule_id: v.id, help: v.help, impact: v.impact, wcag: v.wcag.map((w) => `${w.sc} ${w.name} (${w.level})`).join(", ") || "best practice",
          snippets: v.html_snippets.slice(0, 3), summary: v.failure_summary.slice(0, 400), baseline_fix: v.explanation }));
        ctx.progress(`Writing fixes for ${items.length} violation type(s)`);
        const result = await ctx.generate("remediate", { url: input.url || "the supplied page", violations: items });
        if (ctx.llmLive) {
          const byId = {};
          for (const r of result.items || []) byId[r.rule_id] = r;
          for (const v of violations) {
            const r = byId[v.id];
            if (!r) continue;
            if (String(r.explanation || "").trim()) v.explanation = r.explanation.trim();
            if (String(r.fix_snippet || "").trim()) v.fix_snippet = r.fix_snippet.trim();
            source = "model";
          }
        }
      }
      // 4. CI gate
      const blocking = violations.filter((v) => impactRank(v.impact) >= impactRank(failOn));
      const ciPass = !blocking.length;
      const ts = new Date().toISOString().slice(0, 19);
      const xml = junitXml(input.url || "pasted-html", std.label, engine, violations, passes, failOn, ts);
      ctx.artifact({ name: "a11y_junit.xml", mime: "application/xml", content: xml, description: `JUnit results (fail_on=${failOn}) for CI pipelines` });
      ctx.check({ rule: "ci.gate", status: ciPass ? "pass" : "fail", severity: "high", message: `CI gate ${ciPass ? "passed" : "failed"}: ${blocking.length} violation type(s) at or above '${failOn}'.` });
      // 5. report
      const score = a11yScore(violations, passes.length, skill.scoring || {});
      const counts = Object.fromEntries([...IMPACT_ORDER].reverse().map((k) => [k, violations.filter((v) => v.impact === k).length]));
      const findings = violations.map((v) => ({ title: `${v.help} (${v.nodes_count} element${v.nodes_count !== 1 ? "s" : ""})`, detail: v.explanation, severity: SEV[v.impact], category: "accessibility",
        evidence: v.targets.slice(0, 3).join("; ") + (v.wcag.length ? ` - WCAG ${v.wcag.map((w) => `${w.sc} ${w.level}`).join(", ")}` : " - best practice"),
        recommendation: v.fix_snippet.slice(0, 500) || v.explanation, target: v.targets[0] || input.url || null, source_agent: AGENT }));
      const V_KEYS = ["id", "impact", "wcag", "help", "description", "help_url", "nodes_count", "targets", "html_snippets", "failure_summary", "fix_snippet", "explanation", "best_practice", "blocking"];
      const output = {
        url: input.url || null, standard: input.standard || "wcag21aa", standard_label: std.label, engine, viewport: `${width}x${height}`,
        violations: violations.map((v) => Object.fromEntries(V_KEYS.map((k) => [k, v[k]]))), passes_count: passes.length, incomplete_count: incomplete.length,
        inapplicable_count: Number(raw.inapplicable || 0), impact_counts: counts, score, fail_on: failOn, ci_pass: ciPass, blocking_count: blocking.length,
        junit_artifact: "a11y_junit.xml", remediation_source: source, findings,
      };
      const nodes = violations.reduce((s, v) => s + v.nodes_count, 0);
      ctx.artifact({ name: "a11y_report.html", mime: "text/html", description: "Accessibility report with WCAG mapping and fixes", content: renderReport({ ...output, url: input.url || "pasted HTML",
        generated: new Date().toISOString().slice(0, 16).replace("T", " ") + " UTC", nodes,
        remediation_label: source === "model" ? "fix snippets written by a language model from the failing markup" : "deterministic fixes from the WCAG rule library (no model configured)" }) });
      ctx.artifact({ name: "axe_results.json", mime: "application/json", description: "Raw engine results",
        content: JSON.stringify({ engine, violations: raw.violations || [], passes: raw.passes || [], incomplete: raw.incomplete || [] }, null, 1).slice(0, 4000000) });
      return {
        output,
        summary: `${std.label}: ${violations.length} violated rule(s) on ${nodes} element(s) (${counts.critical} critical, ${counts.serious} serious), score ${pyFixed(score, 0)}/100, CI gate ${ciPass ? "PASS" : "FAIL"} (fail_on=${failOn}) via ${engine.includes("axe") ? "axe-core" : "static checks"}`,
        view: [
          { type: "kv", title: "Result", data: { page: input.url || "pasted HTML", standard: std.label, engine, score: `${pyFixed(score, 0)}/100`, ci_gate: `${ciPass ? "PASS" : "FAIL"} (fail_on=${failOn}, ${blocking.length} blocking)`,
            rules_passed: passes.length, need_manual_review: incomplete.length, fixes_written_by: source } },
          { type: "table", title: "Violations", rows: violations.map((v) => ({ impact: v.impact, rule: v.id, issue: v.help, wcag: v.wcag.map((w) => `${w.sc} ${w.level}`).join(", ") || "best practice", elements: v.nodes_count, blocks_ci: v.blocking ? "yes" : "", first_target: v.targets[0] || "" })) },
          { type: "table", title: "Suggested fixes", rows: violations.map((v) => ({ rule: v.id, fix: v.fix_snippet, why: v.explanation })) },
          { type: "html", title: "Report (a11y_report.html)", artifact: "a11y_report.html" },
          { type: "code", title: "JUnit XML for CI (a11y_junit.xml)", code: xml.slice(0, 4000) },
        ],
      };
    },
  });
})();
