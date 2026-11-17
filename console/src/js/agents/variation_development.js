/* Variation Development - browser twin of caf/agents/experimentation/variation_development.py.
   A plain-language change request becomes a DECLARATIVE DOM patch (model, or the deterministic request parser offline /
   when every model operation fails validation). Code validates each operation and selector, sanitises HTML/CSS and
   compiles the operations to the same escaped, idempotent JavaScript bundle as the Python service - but here the
   operations are APPLIED through DOM APIs inside CAF.lib.html.renderInFrame (no eval). Brand tokens come from
   getComputedStyle in that frame; the proof is a DOM-level diff (pixel screenshots and the visual diff need the Python
   service's Playwright and are reported as degraded). Delivery stays an approval-gated flags.deploy_variation action. */
(() => {
  const XP = (CAF.xp = CAF.xp || {});
  const T = () => CAF.lib.text;
  const DEMO_URL = "https://www.meridian-mutual.example/products/home-insurance.html";
  const OP_NAMES = ["set_text", "set_html", "set_attr", "remove_attr", "add_class", "remove_class", "set_style", "insert_before", "insert_after", "prepend", "append", "hide", "show", "remove", "move"];
  const HTML_OPS = new Set(["set_html", "insert_before", "insert_after", "prepend", "append"]);
  const INSERT_POS = { insert_before: "beforebegin", insert_after: "afterend", prepend: "afterbegin", append: "beforeend" };
  const mkOp = (o) => ({ op: o.op, selector: o.selector || "", value: o.value ?? "", name: o.name ?? "", styles: (o.styles || []).map((d) => ({ property: d.property ?? "", value: d.value ?? "" })),
    target: o.target ?? "", position: o.position || "after", all_matches: !!o.all_matches, reason: o.reason ?? "" });

  /* ============================================================ sanitising (pure) */
  const ALLOWED_TAGS = new Set(("a abbr b blockquote br button caption cite code dd div dl dt em figcaption figure h1 h2 h3 h4 h5 h6 hr i img li mark ol p picture small source span strong sub sup " +
    "table tbody td th thead tr u ul s time").split(" "));
  const ALLOWED_ATTRS = new Set("class id href src srcset alt title role width height target rel type datetime colspan rowspan scope loading decoding tabindex hidden lang dir".split(" "));
  const URL_ATTRS = new Set(["href", "src", "srcset", "action", "formaction", "xlink:href", "poster"]);
  const DROP_TAGS = new Set("script style iframe object embed link meta base form input textarea select frame frameset svg math template noscript".split(" "));
  const BAD_URL = /^\s*(javascript|vbscript|data(?!:image\/(png|gif|jpe?g|webp|svg\+xml);base64,)):/i;
  const CSS_PROP = /^-{0,2}[a-zA-Z][a-zA-Z0-9-]*$/, CLASS_RE = /^-?[_a-zA-Z][_a-zA-Z0-9-]*$/, ATTR_RE = /^[a-zA-Z_][-a-zA-Z0-9_:.]*$/;
  const BAD_CSS_VALUE = /(expression\s*\(|javascript:|vbscript:|behavior\s*:|-moz-binding|<\/|[{};<>])/i;
  const safeUrl = (v) => !BAD_URL.test(v || "");
  /** Whitelist sanitiser (port of sanitize_html): allowed tags/attributes only, no handlers, no script URLs -> [html, removed]. */
  function sanitizeHtml(markup, marker = null) {
    const removed = [];
    const doc = CAF.lib.html.parse(`<!doctype html><html><body><div id="__caf_root">${markup || ""}</div></body></html>`);
    const root = doc.getElementById("__caf_root");
    const walker = doc.createTreeWalker(root, NodeFilter.SHOW_COMMENT);
    const comments = [];
    for (let n = walker.nextNode(); n; n = walker.nextNode()) comments.push(n);
    comments.forEach((c) => c.remove());
    for (const el of [...root.querySelectorAll("*")]) {
      const name = el.tagName.toLowerCase();
      if (DROP_TAGS.has(name)) { removed.push(`<${name}>`); el.remove(); continue; }
      if (!ALLOWED_TAGS.has(name)) { removed.push(`<${name}> (unwrapped)`); el.replaceWith(...el.childNodes); continue; }
      for (const attr of [...el.getAttributeNames()]) {
        const val = el.getAttribute(attr) || "", low = attr.toLowerCase();
        const ok = ALLOWED_ATTRS.has(attr) || attr.startsWith("aria-") || attr.startsWith("data-");
        if (low.startsWith("on") || ["style", "srcdoc", "formaction"].includes(low) || !ok) { removed.push(`${name}[${attr}]`); el.removeAttribute(attr); }
        else if (URL_ATTRS.has(attr) && !safeUrl(val)) { removed.push(`${name}[${attr}] unsafe URL`); el.removeAttribute(attr); }
      }
      if (name === "a" && el.getAttribute("target") === "_blank") el.setAttribute("rel", "noopener noreferrer");
    }
    if (marker !== null) for (const child of root.children) child.setAttribute("data-caf-inserted", marker);
    return [root.innerHTML, removed];
  }
  function sanitizeCss(css) {
    const removed = [];
    let out = css || "";
    for (const [pattern, label] of [[/@import[^;]*;?/gi, "@import"], [/<\/\s*style/gi, "</style"], [/expression\s*\([^)]*\)/gi, "expression()"], [/(?:-moz-binding|behavior)\s*:[^;}]*;?/gi, "binding/behavior"],
      [/url\(\s*['"]?\s*(?:javascript|vbscript|data:(?!image\/))[^)]*\)/gi, "unsafe url()"]]) {
      if (out.search(pattern) >= 0) { removed.push(label); out = out.replace(pattern, ""); }
    }
    return [out.replace(/</g, "").trim(), removed];
  }
  function selectorError(sel, doc) {
    if (!sel || !String(sel).trim()) return "empty selector";
    if (String(sel).length > 300) return "selector too long";
    try { (doc || document).createDocumentFragment().querySelector(sel); } catch (e) { return `invalid selector (${e.name || "SyntaxError"})`; }
    return null;
  }
  const qsa = (doc, sel) => { try { return [...doc.querySelectorAll(sel)]; } catch { return []; } };
  function validateOp(op, doc) {
    const errors = [];
    const err = selectorError(op.selector, doc);
    if (err) return [err];
    if (doc) { try { if (!doc.querySelectorAll(op.selector).length) errors.push(`selector '${op.selector}' matches nothing on the page`); } catch (e) { errors.push(`selector '${op.selector}' failed (${e.name})`); } }
    if (op.op === "set_text" && !String(op.value).trim()) errors.push("set_text needs a value");
    if (HTML_OPS.has(op.op) && !String(op.value).trim()) errors.push(`${op.op} needs HTML in value`);
    if (op.op === "set_attr" || op.op === "remove_attr") {
      const nm = op.name || "";
      if (!ATTR_RE.test(nm)) errors.push(`invalid attribute name '${op.name}'`);
      else if (nm.toLowerCase().startsWith("on") || ["style", "srcdoc"].includes(nm.toLowerCase())) errors.push(`attribute '${op.name}' is not allowed (event handlers, inline style and srcdoc are blocked; use set_style)`);
      else if (op.op === "set_attr" && URL_ATTRS.has(nm.toLowerCase()) && !safeUrl(op.value)) errors.push(`unsafe URL in ${op.name}`);
    }
    if (op.op === "add_class" || op.op === "remove_class") { const names = String(op.value).split(/\s+/).filter(Boolean); if (!names.length || names.some((n) => !CLASS_RE.test(n))) errors.push(`invalid class list '${op.value}'`); }
    if (op.op === "set_style") {
      if (!op.styles.length) errors.push("set_style needs at least one declaration");
      for (const d of op.styles) {
        if (!CSS_PROP.test(d.property || "")) errors.push(`invalid CSS property '${d.property}'`);
        if (!String(d.value).trim() || BAD_CSS_VALUE.test(d.value) || /url\(\s*['"]?\s*javascript/i.test(d.value)) errors.push(`unsafe or empty CSS value for ${d.property}`);
      }
    }
    if (op.op === "move") {
      const e2 = selectorError(op.target, doc);
      if (e2) errors.push(`move target: ${e2}`);
      else if (doc && !qsa(doc, op.target).length) errors.push(`move target '${op.target}' matches nothing on the page`);
    }
    return errors;
  }

  /* ============================================================ compiler (pure: identical bundle text to the Python service) */
  /** json.dumps(ensure_ascii=False) made safe inside JavaScript and an HTML <script>. */
  const jsStr = (v) => {
    const raw = Array.isArray(v) ? "[" + v.map((x) => JSON.stringify(x)).join(", ") + "]" : JSON.stringify(v);
    return raw.replace(/</g, "\\u003c").replace(/>/g, "\\u003e").replace(/&/g, "\\u0026").replace(/\u2028/g, "\\u2028").replace(/\u2029/g, "\\u2029");
  };
  function opBody(op, index) {
    const v = jsStr;
    switch (op.op) {
      case "set_text": return `el.textContent = ${v(op.value)};`;
      case "set_html": return `el.innerHTML = ${v(sanitizeHtml(op.value)[0])};`;
      case "insert_before": case "insert_after": case "prepend": case "append": return `el.insertAdjacentHTML(${v(INSERT_POS[op.op])}, ${v(sanitizeHtml(op.value, String(index))[0])});`;
      case "set_attr": return `el.setAttribute(${v(op.name)}, ${v(op.value)});`;
      case "remove_attr": return `el.removeAttribute(${v(op.name)});`;
      case "add_class": return `${v(String(op.value).split(/\s+/).filter(Boolean))}.forEach(function (c) { el.classList.add(c); });`;
      case "remove_class": return `${v(String(op.value).split(/\s+/).filter(Boolean))}.forEach(function (c) { el.classList.remove(c); });`;
      case "set_style": return op.styles.map((d) => `el.style.setProperty(${v(d.property.trim())}, ${v(d.value.trim())}, 'important');`).join(" ");
      case "hide": return "el.style.setProperty('display', 'none', 'important'); el.setAttribute('aria-hidden', 'true');";
      case "show": return "el.hidden = false; el.style.removeProperty('display'); el.removeAttribute('aria-hidden');";
      case "remove": return "if (el.parentNode) { el.parentNode.removeChild(el); }";
      case "move": { const place = { before: "beforebegin", after: "afterend", prepend: "afterbegin", append: "beforeend" }[op.position]; return `var ref = document.querySelector(${v(op.target)}); if (!ref) { return false; } ref.insertAdjacentElement(${v(place)}, el);`; }
      default: throw new Error(`unknown op ${op.op}`);
    }
  }
  function compilePatch(ops, key = "caf-variation") {
    const L = ["(function () {", "  var KEY = " + jsStr(key) + ";", "  window.__cafApplied = window.__cafApplied || {};", "  if (window.__cafApplied[KEY]) { return; }", "  window.__cafApplied[KEY] = true;",
      "  function each(sel, all, fn) {", "    var els = all ? document.querySelectorAll(sel) : [document.querySelector(sel)], n = 0;", "    for (var i = 0; i < els.length; i++) { if (els[i] && fn(els[i]) !== false) { n++; } }",
      "    return n;", "  }", "  var ops = ["];
    ops.forEach((op, i) => L.push(`    function () { return each(${jsStr(op.selector)}, ${op.all_matches ? "true" : "false"}, function (el) { ${opBody(op, i)} }); },`));
    L.push("  ];", "  var done = [];", "  function run(attempt) {", "    var pending = 0;", "    for (var i = 0; i < ops.length; i++) {", "      if (done[i]) { continue; }",
      "      try { done[i] = ops[i]() > 0; } catch (e) { done[i] = true; if (window.console) { console.warn('[caf] operation ' + i + ' failed', e); } }", "      if (!done[i]) { pending++; }", "    }",
      "    if (pending && attempt < 20) { setTimeout(function () { run(attempt + 1); }, 100); }", "    else if (pending && window.console) { console.warn('[caf] ' + pending + ' operation(s) found no element'); }",
      "  }", "  run(0);", "})();");
    return L.join("\n") + "\n";
  }
  /** The vendor-neutral bundle (port of caf.connectors.publishing.RUNTIME_TEMPLATE). */
  const runtimeBundle = (expId, varId, css, js) => `/* Coforge CAF variation runtime - vendor-neutral client-side mutation bundle.\n * experiment: ${expId}  variation: ${varId}\n * Include on the page (or deliver from an edge worker / any testing tool's custom-code slot). */\n(function () {\n  var css = ${XP.pyJson(css || "")};\n  if (css) { var s = document.createElement('style'); s.setAttribute('data-caf-variation', ${XP.pyJson(varId)}); s.textContent = css; document.head.appendChild(s); }\n  function apply() {\n    try { ${js} } catch (e) { if (window.console) console.warn('[caf] variation error', e); }\n  }\n  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', apply); else apply();\n})();\n`;

  /** Apply the declarative operations with DOM APIs - the same semantics as the compiled bundle, without evaluating code. */
  function applyOps(doc, ops) {
    const results = [], errors = [];
    ops.forEach((op, i) => {
      let n = 0;
      try {
        const els = op.all_matches ? [...doc.querySelectorAll(op.selector)] : [doc.querySelector(op.selector)];
        for (const el of els) {
          if (!el) continue;
          let ok = true;
          switch (op.op) {
            case "set_text": el.textContent = op.value; break;
            case "set_html": el.innerHTML = sanitizeHtml(op.value)[0]; break;
            case "insert_before": case "insert_after": case "prepend": case "append": el.insertAdjacentHTML(INSERT_POS[op.op], sanitizeHtml(op.value, String(i))[0]); break;
            case "set_attr": el.setAttribute(op.name, op.value); break;
            case "remove_attr": el.removeAttribute(op.name); break;
            case "add_class": String(op.value).split(/\s+/).filter(Boolean).forEach((c) => el.classList.add(c)); break;
            case "remove_class": String(op.value).split(/\s+/).filter(Boolean).forEach((c) => el.classList.remove(c)); break;
            case "set_style": for (const d of op.styles) el.style.setProperty(d.property.trim(), d.value.trim(), "important"); break;
            case "hide": el.style.setProperty("display", "none", "important"); el.setAttribute("aria-hidden", "true"); break;
            case "show": el.hidden = false; el.style.removeProperty("display"); el.removeAttribute("aria-hidden"); break;
            case "remove": if (el.parentNode) el.parentNode.removeChild(el); break;
            case "move": { const ref = doc.querySelector(op.target); if (!ref) { ok = false; break; } ref.insertAdjacentElement({ before: "beforebegin", after: "afterend", prepend: "afterbegin", append: "beforeend" }[op.position], el); break; }
            default: throw new Error(`unknown op ${op.op}`);
          }
          if (ok) n++;
        }
      } catch (e) { errors.push(`[caf] operation ${i} failed: ${e.message}`); }
      results.push(n);
    });
    return { counts: results, errors };
  }
  /** Port of VERIFY_JS, evaluated in the rendered frame (computed styles from its window). */
  function verifyOps(doc, win, ops, beforeCounts) {
    return ops.map((op, i) => {
      let els = [];
      try { els = [...doc.querySelectorAll(op.selector)]; } catch { return { i, ok: false, matches: 0, msg: "selector error" }; }
      if (op.op === "remove") return { i, ok: els.length < beforeCounts[i], matches: els.length, msg: "removed" };
      if (INSERT_POS[op.op]) { const n = doc.querySelectorAll(`[data-caf-inserted="${i}"]`).length; return { i, ok: n > 0, matches: n, msg: `${n} inserted` }; }
      const el = els[0];
      if (!el) return { i, ok: false, matches: 0, msg: "element not found after applying" };
      const cs = win.getComputedStyle(el);
      switch (op.op) {
        case "set_text": { const ok = el.textContent.trim() === String(op.value).trim(); return { i, ok, matches: els.length, msg: ok ? "text set" : "text differs" }; }
        case "set_style": { const bad = op.styles.filter((d) => el.style.getPropertyValue(d.property) === ""); return { i, ok: !bad.length, matches: els.length, msg: bad.length ? "not applied: " + bad.map((d) => d.property).join(", ") : op.styles.map((d) => `${d.property}=${cs.getPropertyValue(d.property)}`).join("; ") }; }
        case "hide": return { i, ok: cs.display === "none", matches: els.length, msg: `display ${cs.display}` };
        case "show": return { i, ok: cs.display !== "none", matches: els.length, msg: `display ${cs.display}` };
        case "add_class": return { i, ok: String(op.value).split(/\s+/).filter(Boolean).every((c) => el.classList.contains(c)), matches: els.length, msg: el.className };
        case "remove_class": return { i, ok: String(op.value).split(/\s+/).filter(Boolean).every((c) => !el.classList.contains(c)), matches: els.length, msg: el.className };
        case "set_attr": return { i, ok: el.getAttribute(op.name) === op.value, matches: els.length, msg: op.name };
        case "remove_attr": return { i, ok: !el.hasAttribute(op.name), matches: els.length, msg: op.name };
        case "set_html": return { i, ok: true, matches: els.length, msg: "html set" };
        case "move": {
          const ref = doc.querySelector(op.target);
          if (!ref) return { i, ok: false, matches: els.length, msg: "target missing" };
          const pos = ref.compareDocumentPosition(el), before = !!(pos & 2), inside = !!(pos & 16);
          const ok = op.position === "before" ? before && !inside : op.position === "after" ? !before && !inside : inside;
          return { i, ok, matches: els.length, msg: `moved ${op.position}` };
        }
        default: return { i, ok: true, matches: els.length, msg: "" };
      }
    });
  }

  /* ============================================================ DOM-level diff (the console's stand-in for screenshots + pixel diff) */
  const STYLE_KEYS = ["display", "visibility", "opacity", "color", "background-color", "font-size", "font-weight", "font-family", "padding-top", "padding-right", "padding-bottom", "padding-left",
    "border-top-width", "border-radius", "box-shadow", "text-decoration-line"]; // no width/height: layout reflow is not a change
  function snapshot(doc, win) {
    const map = new Map();
    for (const el of doc.body.querySelectorAll("*")) {
      const r = el.getBoundingClientRect(), cs = win.getComputedStyle(el);
      const own = [...el.childNodes].filter((n) => n.nodeType === 3).map((n) => n.nodeValue).join(" ").replace(/\s+/g, " ").trim();
      map.set(el, { rect: [r.left + win.scrollX, r.top + win.scrollY, r.right + win.scrollX, r.bottom + win.scrollY], text: own, style: STYLE_KEYS.map((k) => cs.getPropertyValue(k)).join("|"),
        attrs: el.getAttributeNames().filter((a) => a !== "style").map((a) => `${a}=${el.getAttribute(a)}`).join("&"), parent: el.parentElement });
    }
    const de = doc.documentElement;
    return { map, size: [Math.max(de.scrollWidth, doc.body.scrollWidth), Math.max(de.scrollHeight, doc.body.scrollHeight)] };
  }
  function domDiff(doc, before, after, movedEls) {
    const changed = [];
    for (const [el, b] of before.map) {
      const a = after.map.get(el);
      if (!a || !el.isConnected) { changed.push({ el, kind: "removed", before: b.rect, after: null }); continue; }
      const kinds = [];
      if (a.text !== b.text) kinds.push("text");
      if (a.style !== b.style) kinds.push("style");
      if (a.attrs !== b.attrs) kinds.push("attributes");
      if (movedEls.has(el) && a.parent !== b.parent) kinds.push("moved");
      if (kinds.length) changed.push({ el, kind: kinds.join("+"), before: b.rect, after: a.rect });
    }
    for (const [el, a] of after.map) if (!before.map.has(el)) changed.push({ el, kind: "inserted", before: null, after: a.rect });
    // only the outermost changed elements count towards areas (a restyled button's text node change is the same region)
    const set = new Set(changed.map((c) => c.el));
    const top = changed.filter((c) => { for (let p = (after.map.get(c.el) || before.map.get(c.el)).parent; p; p = p.parentElement) if (set.has(p)) return false; return true; });
    const union = (rects) => { const rs = rects.filter((r) => r && r[2] - r[0] > 0 && r[3] - r[1] > 0); if (!rs.length) return null; return [Math.min(...rs.map((r) => r[0])), Math.min(...rs.map((r) => r[1])), Math.max(...rs.map((r) => r[2])), Math.max(...rs.map((r) => r[3]))].map(Math.round); };
    const area = (rs) => rs.filter(Boolean).reduce((s, r) => s + Math.max(0, r[2] - r[0]) * Math.max(0, r[3] - r[1]), 0);
    const [w, h] = after.size;
    const describe = (c) => { let sel = ""; try { sel = c.el.isConnected ? XP.uniqueSelector(c.el, doc) : c.el.tagName.toLowerCase() + (c.el.id ? `#${c.el.id}` : ""); } catch { sel = c.el.tagName.toLowerCase(); } return { selector: sel, change: c.kind, tag: c.el.tagName.toLowerCase(), bbox_before: c.before ? c.before.map(Math.round) : null, bbox_after: c.after ? c.after.map(Math.round) : null }; };
    return { changed_elements: top.length, changes: top.slice(0, 30).map(describe), counts: top.reduce((o, c) => { for (const k of c.kind.split("+")) o[k] = (o[k] || 0) + 1; return o; }, {}),
      bbox: union(top.map((c) => c.after)), bbox_before: union(top.map((c) => c.before)), changed_area_ratio: XP.round(Math.max(area(top.map((c) => c.after)), area(top.map((c) => c.before))) / Math.max(w * h, 1), 6),
      layout_shift_px: Math.round(after.size[1] - before.size[1]), size_before: before.size.map(Math.round), size_after: after.size.map(Math.round) };
  }

  /* ============================================================ brand tokens + colours (pure) */
  const NAMED = { black: [0, 0, 0], white: [255, 255, 255], red: [220, 38, 38], green: [22, 163, 74], blue: [37, 99, 235], yellow: [234, 179, 8], orange: [234, 88, 12], purple: [147, 51, 234], pink: [219, 39, 119],
    teal: [13, 148, 136], grey: [107, 114, 128], gray: [107, 114, 128], navy: [30, 58, 138], gold: [202, 138, 4], amber: [245, 158, 11] };
  function parseColour(value) {
    const v = String(value || "").trim().toLowerCase();
    let m = v.match(/^#([0-9a-f]{3}|[0-9a-f]{6})$/);
    if (m) { let h = m[1]; if (h.length === 3) h = h.split("").map((c) => c + c).join(""); return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16)); }
    if ((m = v.match(/^rgba?\(\s*(\d+)[ ,]+(\d+)[ ,]+(\d+)/))) return [+m[1], +m[2], +m[3]];
    return NAMED[v] || null;
  }
  const colourDistance = (a, b) => { const rm = (a[0] + b[0]) / 2, dr = a[0] - b[0], dg = a[1] - b[1], db = a[2] - b[2]; return Math.sqrt((2 + rm / 256) * dr * dr + 4 * dg * dg + (2 + (255 - rm) / 256) * db * db); };
  const sameRgb = (a, b) => a[0] === b[0] && a[1] === b[1] && a[2] === b[2];
  function brandPalette(tok) {
    const pal = new Map();
    for (const [name, val] of Object.entries(tok.custom_properties || {})) { const c = parseColour(String(val)); if (c) pal.set(`var(${name})`, c); }
    for (const key of ["body", "h1", "h2", "link", "primary_button"]) {
      const s = tok[key] || {};
      for (const prop of ["color", "background"]) { const raw = String(s[prop] ?? ""); const c = parseColour(raw); if (c && !raw.startsWith("rgba(0, 0, 0, 0)") && !pal.has(`${key}.${prop}`)) pal.set(`${key}.${prop}`, c); }
    }
    return pal;
  }
  function rgbToHsv(r, g, b) { const mx = Math.max(r, g, b), mn = Math.min(r, g, b), v = mx; if (mn === mx) return [0, 0, v]; const s = (mx - mn) / mx; const rc = (mx - r) / (mx - mn), gc = (mx - g) / (mx - mn), bc = (mx - b) / (mx - mn);
    let h = r === mx ? bc - gc : g === mx ? 2 + rc - bc : 4 + gc - rc; h = ((h / 6) % 1 + 1) % 1; return [h, s, v]; }
  function nearestBrandColour(word, tok) {
    const want = parseColour(word);
    if (!want) return null;
    let best = null, dist = 1e9;
    for (const [name, c] of brandPalette(tok)) {
      if (!name.startsWith("var(")) continue;
      const [h1, s1] = rgbToHsv(...want.map((x) => x / 255)), [h2, s2] = rgbToHsv(...c.map((x) => x / 255));
      const hue = s1 > 0.15 && s2 > 0.15 ? Math.min(Math.abs(h1 - h2), 1 - Math.abs(h1 - h2)) * 360 : 180 * Math.abs(s1 - s2);
      const d = colourDistance(want, c) + hue * 2;
      if (d < dist) { best = name; dist = d; }
    }
    return best;
  }
  function offBrandColours(ops, tok, css = "", tolerance = 60) {
    const pal = [...brandPalette(tok).values()], found = [];
    const values = [...ops.filter((o) => o.op === "set_style").flatMap((o) => o.styles.map((d) => d.value)), ...[...String(css || "").matchAll(/:\s*([^;{}]+)/g)].map((m) => m[1])];
    const re = new RegExp(`#[0-9a-fA-F]{3,6}\\b|rgba?\\([^)]*\\)|\\b(?:${Object.keys(NAMED).join("|")})\\b`, "g");
    for (const v of values) for (const m of String(v).matchAll(re)) { const raw = m[0], c = parseColour(raw); if (c && pal.length && Math.min(...pal.map((p) => colourDistance(c, p))) > tolerance && !found.includes(raw)) found.push(raw); }
    return found;
  }
  function staticBrandTokens(cssTexts) {
    const props = {};
    for (const css of cssTexts) {
      const clean = String(css || "").replace(/\/\*[\s\S]*?\*\//g, "");
      for (const m of clean.matchAll(/(?:^|[};])\s*((?::root|html)(?:\s*,\s*(?::root|html))*)\s*{([^}]*)}/g)) for (const d of m[2].matchAll(/(--[\w-]+)\s*:\s*([^;]+);?/g)) props[d[1]] = d[2].trim();
    }
    return { custom_properties: props, source: "static CSS (no browser frame)", targets: {} };
  }
  function staticStyle(el, cssTexts, props) {
    const out = {};
    for (const css of cssTexts) for (const [, sel, body] of String(css || "").replace(/\/\*[\s\S]*?\*\//g, "").matchAll(/([^{}@]+){([^}]*)}/g)) for (let single of sel.split(",")) {
      single = single.trim();
      if (!single || single.replace(/::/g, ":").includes(":")) continue;
      try { if (el.matches(single)) for (const [, p, v] of body.matchAll(/([\w-]+)\s*:\s*([^;]+)/g)) out[p.trim()] = v.trim(); } catch { /* unsupported selector */ }
    }
    return Object.fromEntries(Object.entries(out).map(([k, v]) => [k, v.replace(/var\((--[\w-]+)\)/g, (m, n) => props[n] ?? m)]));
  }
  /** Port of BRAND_JS against the rendered frame. */
  function frameBrandTokens(doc, win, targets) {
    const pick = (el, label) => { if (!el) return null; const cs = win.getComputedStyle(el);
      return { label, color: cs.color, background: cs.backgroundColor, font_family: cs.fontFamily, font_size: cs.fontSize, font_weight: cs.fontWeight, line_height: cs.lineHeight, border_radius: cs.borderRadius,
        padding: [cs.paddingTop, cs.paddingRight, cs.paddingBottom, cs.paddingLeft].join(" "), box_shadow: cs.boxShadow, letter_spacing: cs.letterSpacing, text_transform: cs.textTransform }; };
    const root = win.getComputedStyle(doc.documentElement), vars = {};
    for (const sheet of [...doc.styleSheets]) {
      let rules = null;
      try { rules = sheet.cssRules; } catch { continue; }
      for (const r of [...(rules || [])]) if (r.selectorText && /(^|,)\s*(:root|html)\s*(,|$)/.test(r.selectorText)) for (const name of [...r.style]) if (name.startsWith("--")) vars[name] = root.getPropertyValue(name).trim();
    }
    const out = { custom_properties: vars, body: pick(doc.body, "body"), h1: pick(doc.querySelector("h1"), "h1"), h2: pick(doc.querySelector("h2"), "h2"),
      link: pick(doc.querySelector("main a:not(.btn)") || doc.querySelector("a"), "link"), primary_button: pick(doc.querySelector('.btn-primary, [class*="btn-primary"], button[type="submit"], .btn, button'), "primary button"),
      buttons: [...doc.querySelectorAll('.btn, button, [class*="cta"]')].slice(0, 6).map((el) => pick(el, el.className || el.tagName)), targets: {} };
    for (const sel of targets) { try { const el = doc.querySelector(sel); if (el) out.targets[sel] = pick(el, sel); } catch { /* invalid selector */ } }
    return out;
  }
  const px = (v) => { const m = String(v || "").match(/(-?\d+(?:\.\d+)?)px/); return m ? parseFloat(m[1]) : null; };
  /** 'Make it more prominent' from the element's own computed style: same palette, larger scale, more weight. */
  function prominenceStyles(c0) {
    const c = c0 || {}, styles = [];
    let fs = px(c.font_size || c["font-size"]);
    if (fs === null && c.font) fs = px(c.font);
    styles.push({ property: "font-size", value: fs ? `${XP.roundHalfEven(fs * 1.15)}px` : "1.15em" });
    const pad = String(c.padding || "").split(/\s+/).map(px).filter((p) => p !== null);
    styles.push({ property: "padding", value: pad.length >= 2 ? `${XP.roundHalfEven(pad[0] * 1.25)}px ${XP.roundHalfEven(pad[1] * 1.3)}px` : "0.95em 1.9em" });
    const w = parseFloat(c.font_weight || c["font-weight"] || 400);
    if (Number.isNaN(w) || Math.trunc(w) < 700) styles.push({ property: "font-weight", value: "700" });
    const bg = parseColour(String(c.background || c["background-color"] || ""));
    styles.push({ property: "box-shadow", value: `0 8px 22px ${bg && !sameRgb(bg, [255, 255, 255]) ? `rgba(${bg[0]}, ${bg[1]}, ${bg[2]}, 0.45)` : "rgba(14, 58, 74, 0.25)"}` });
    return styles.map((s) => ({ property: s.property, value: s.value }));
  }

  /* ============================================================ deterministic request parser (pure) */
  const Q = "\"“”‘’'";
  const QUOTED = () => new RegExp(`[${Q}]([^${Q}]{1,160})[${Q}]`, "g");
  const PROMINENT = /\b(more prominent|prominent|stand out|bigger|larger|more visible|bolder|emphasi[sz]e|eye-catching|pop)\b/i;
  const HIDE = () => /\b(hide|remove|delete|get rid of)\s+(?:the\s+)?(.+?)(?:\.|$| and )/gi;
  const MOVE = () => /\bmove\s+(?:the\s+)?(.+?)\s+(above|below|before|after|under|over)\s+(?:the\s+)?(.+?)(?:\.|$| and )/gi;
  const COLOUR = new RegExp(`\\b(?:make|turn|change)\\s+(?:it|the\\s+.+?)\\s+(?:colou?r\\s+)?(?:to\\s+)?(${Object.keys(NAMED).join("|")}|#[0-9a-f]{3,6})\\b`, "i");
  const ADD = () => /\badd\s+(?:a\s+|an\s+)?(?:line|note|text|message|badge|label|sentence)?\s*(?:saying\s+)?["“'‘](.+?)["”'’]\s+(above|below|under|after|before|next to)\s+(?:the\s+)?(.+?)(?:\.|$| and )/gi;
  const ROLE_WORDS = { button: ["button", "btn", "cta"], cta: ["cta", "button", "btn"], link: ["a", "link"], headline: ["h1"], heading: ["h1", "h2"], title: ["h1"], image: ["img", "image"], photo: ["img", "image"],
    banner: ["promo", "band", "banner"], hero: ["hero"], header: ["header", "nav"], nav: ["nav"], footer: ["footer"], tabs: ["tab"], tab: ["tab"], table: ["table"], faq: ["faq"], form: ["form"] };
  const SKIP_TEXT = /^(SCRIPT|STYLE|TEMPLATE)$/;
  /** bs4 get_text(" "): every text node (whitespace included) joined by a space, script/style/template excluded. */
  function rawText(el) { const parts = []; const walk = (n) => { for (const c of n.childNodes) { if (c.nodeType === 3) parts.push(c.nodeValue); else if (c.nodeType === 1 && !SKIP_TEXT.test(c.tagName)) walk(c); } }; walk(el); return parts.join(" "); }
  const ANC_TOKENS = new Set(["hero", "header", "footer", "nav", "promo", "pricing", "plans", "faq", "advisor"]);
  function elTokens(el) {
    const toks = new Set([el.tagName.toLowerCase()]);
    for (const v of [el.id || "", ...el.classList]) { const low = v.toLowerCase(); for (const p of low.split(/[-_]/)) toks.add(p); toks.add(low); }
    for (let anc = el.parentElement; anc; anc = anc.parentElement) {
      for (const v of [anc.id || "", ...anc.classList]) for (const p of v.toLowerCase().split(/[-_]/)) if (ANC_TOKENS.has(p)) toks.add(p);
      const t = anc.tagName.toLowerCase();
      if (["header", "footer", "nav"].includes(t)) toks.add(t);
    }
    toks.delete("");
    return toks;
  }
  function hintWords(text) {
    const words = new Set(String(text || "").toLowerCase().match(/[a-z]+/g) || []);
    const out = new Set(words);
    for (const w of words) for (const r of ROLE_WORDS[w] || []) out.add(r);
    for (const s of ["the", "a", "an", "to", "and", "it", "make", "change", "text", "copy", "label", "from", "with", "more"]) out.delete(s);
    return out;
  }
  const inter = (a, b) => { let n = 0; for (const x of a) if (b.has(x)) n++; return n; };
  const SKIP_TAGS = new Set(["script", "style", "head", "title", "meta", "html", "body"]);
  function findByText(doc, text, hint = "") {
    const want = T().canonical(text), hints = hintWords(hint);
    let best = null, bestScore = -1;
    for (const el of doc.querySelectorAll("*")) {
      const tag = el.tagName.toLowerCase();
      if (SKIP_TAGS.has(tag) || T().canonical(rawText(el)) !== want) continue;
      if ([...el.querySelectorAll("*")].some((c) => T().canonical(rawText(c)) === want)) continue;
      const toks = elTokens(el);
      let score = inter(hints, toks) * 3;
      if (tag === "a" || tag === "button" || toks.has("btn")) score += ["button", "cta", "btn", "link"].some((w) => hints.has(w)) ? 1 : 0.5;
      if (toks.has("primary")) score += 0.5;
      if (score > bestScore) { best = el; bestScore = score; }
    }
    return best;
  }
  function findByDescription(doc, description) {
    const hints = hintWords(description);
    if (!hints.size) return null;
    let best = null, bestScore = 0;
    for (const el of doc.querySelectorAll("*")) {
      const tag = el.tagName.toLowerCase();
      if (SKIP_TAGS.has(tag) || tag === "main") continue;
      const toks = elTokens(el);
      const own = ["section", "div"].includes(tag) ? new Set() : new Set(T().tokens(rawText(el).slice(0, 80)));
      let score = inter(hints, toks) * 2 + inter(hints, own) * 0.5;
      if (el.id && inter(hints, new Set(el.id.toLowerCase().split(/[-_]/)))) score += 1;
      if (["section", "div"].includes(tag) && score) score -= 0.25;
      if (score > bestScore) { best = el; bestScore = score; }
    }
    return best;
  }
  /** Deterministic NL -> patch for common requests (port of parse_request) -> [ops, notes]. */
  function parseRequest(request, doc, computed = {}, tok = {}) {
    const ops = [], notes = [];
    const text = String(request).trim();
    const bare = text.replace(QUOTED(), '""');
    let targetSel = null;
    const quoted = [...text.matchAll(QUOTED())].map((m) => m[1]);
    const m = text.match(new RegExp(`["“‘']([^${Q}]{1,160})["”’']\\s*(?:to|with|into|->|→)\\s*["“‘']([^${Q}]{1,160})["”’']`));
    if (m) {
      const [, old, nu] = m;
      const el = findByText(doc, old, text.slice(0, m.index));
      if (el) { targetSel = XP.uniqueSelector(el, doc); ops.push(mkOp({ op: "set_text", selector: targetSel, value: nu, reason: `Change '${old}' to '${nu}'` })); }
      else notes.push(`No element with the text '${old}' was found on the page.`);
    } else if (quoted.length === 1 && /\b(change|set|rename|rewrite|update|make)\b/i.test(text) && !ADD().test(text) && new RegExp(`\\b(to|with|into|reads?|says?)\\s*["“‘']` + quoted[0].replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "i").test(text)) {
      const el = findByDescription(doc, text.split(quoted[0])[0]);
      if (el) { targetSel = XP.uniqueSelector(el, doc); ops.push(mkOp({ op: "set_text", selector: targetSel, value: quoted[0], reason: "Replace the element's text" })); }
    }
    for (const am of text.matchAll(ADD())) {
      const ref = findByDescription(doc, am[3]);
      if (ref) {
        const pos = ["above", "before"].includes(am[2].toLowerCase()) ? "insert_before" : "insert_after";
        const esc = am[1].replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
        ops.push(mkOp({ op: pos, selector: XP.uniqueSelector(ref, doc), value: `<p class="caf-note">${esc}</p>`, reason: "Add the requested text" }));
      }
    }
    for (const mm of bare.matchAll(MOVE())) {
      const [, a, where0, b] = mm, where = where0.toLowerCase();
      const elA = findByDescription(doc, a), elB = findByDescription(doc, b);
      if (elA && elB && elA !== elB) ops.push(mkOp({ op: "move", selector: XP.uniqueSelector(elA, doc), target: XP.uniqueSelector(elB, doc), position: ["above", "before", "over"].includes(where) ? "before" : "after", reason: `Move ${a} ${where} ${b}` }));
    }
    const hasMove = MOVE().test(bare);
    for (const hm of bare.matchAll(HIDE())) {
      if (hasMove && hm.index > 0 && bare.slice(0, hm.index).toLowerCase().includes("move")) continue;
      const el = findByDescription(doc, hm[2]);
      if (el) ops.push(mkOp({ op: "hide", selector: XP.uniqueSelector(el, doc), reason: `Hide ${hm[2]}` }));
    }
    if (PROMINENT.test(bare)) {
      let sel = targetSel;
      if (sel === null) { const el = findByDescription(doc, bare); sel = el ? XP.uniqueSelector(el, doc) : null; }
      if (sel) ops.push(mkOp({ op: "set_style", selector: sel, styles: prominenceStyles((computed || {})[sel]), reason: "More prominent: larger type, more padding and weight, a shadow in the button's own colour" }));
      else notes.push("Could not tell which element should be more prominent.");
    }
    const cm = bare.match(COLOUR);
    if (cm) {
      let sel = targetSel;
      if (sel === null) { const el = findByDescription(doc, bare.slice(0, cm.index + cm[0].length - cm[1].length)); sel = el ? XP.uniqueSelector(el, doc) : null; }
      if (sel) {
        const brand = nearestBrandColour(cm[1], tok || {});
        const prop = /button|cta|btn|band|banner|background/i.test(bare) ? "background-color" : "color";
        ops.push(mkOp({ op: "set_style", selector: sel, styles: [{ property: prop, value: brand || cm[1] }], reason: brand ? `Colour '${cm[1]}' mapped to the brand token ${brand}` : "Requested colour" }));
      }
    }
    return [ops, notes];
  }
  const pyRepr = (s) => (String(s).includes("'") && !String(s).includes('"') ? `"${s}"` : `'${String(s).replace(/\\/g, "\\\\").replace(/'/g, "\\'")}'`);
  function elementInventory(doc, limit = 70) {
    const lines = [], seen = new Set();
    for (const el of qsa(doc, "h1, h2, h3, a.btn, a[class*=cta], a[id], button, [role=button], img, section, [class*=tab], form, table, p.lead, nav a, .hero-note")) {
      if (lines.length >= limit) break;
      const sel = XP.uniqueSelector(el, doc);
      if (seen.has(sel)) continue;
      seen.add(sel);
      const tag = el.tagName.toLowerCase();
      const text = ["section", "table", "form"].includes(tag) ? "" : T().truncate(XP.normWs(rawText(el)), 70);
      const alt = tag === "img" ? el.getAttribute("alt") : null;
      lines.push(`${sel} | <${tag}> | ${text || (alt !== null ? "alt=" + pyRepr(alt) : "")}`.replace(/[ |]+$/, ""));
    }
    return lines;
  }
  function brandLines(tok) {
    const L = Object.entries(tok.custom_properties || {}).slice(0, 30).map(([k, v]) => `${k}: ${v}`);
    for (const key of ["body", "h1", "h2", "link", "primary_button"]) { const s = tok[key]; if (s) L.push(`${key}: color ${s.color}, background ${s.background}, font ${s.font_weight} ${s.font_size} ${T().truncate(String(s.font_family), 40)}, radius ${s.border_radius}, padding ${s.padding}`); }
    return L;
  }

  CAF.agent("variation_development", {
    async run(input, ctx) {
      if (!XP.generateOrNone || !XP.pageFacts) throw new Error("The shared experimentation kit (CAF.xp, defined in experiment_ideation.js) did not load");
      if (!(input.url || input.html)) throw new Error("Provide a page url or its html");
      /* ---- load page */
      const page = ctx.page({ url: input.url, html: input.html });
      if (!page || page.status !== 200) throw new Error(`Could not load ${input.url || "the page"} (${page ? page.status : "not fetchable from the hosted console"}); pass the page html instead`);
      const html = page.html, url = input.html ? input.url || null : page.url;
      const doc = CAF.lib.html.parse(html);
      const cssTexts = [...doc.querySelectorAll("style")].map((s) => s.textContent);
      if (doc.querySelector('link[rel~="stylesheet"][href]')) ctx.warn("Linked stylesheets cannot be fetched in the hosted console; static style fallbacks use the inline CSS only.");
      const plan = ctx.upstreamOutput("experiment_planning") || {};
      const treat = (plan.variants || []).find((v) => !v.is_control) || null;
      let planned = treat ? [...new Set([treat.description || "", ...(treat.changes || [])])].join("; ").replace(/^[; ]+|[; ]+$/g, "") : "";
      if (!planned) planned = (((ctx.upstreamOutput("experiment_ideation") || {}).ideas || [])[0] || {}).change || "";
      const request = String(input.change_request || input.request || "").trim() || planned;
      if (request.length < 3) throw new Error("Describe the change (change_request) or run Experiment Planning upstream");
      let path = "page"; try { path = new URL(url).pathname || "page"; } catch { /* html input without url */ }
      const expId = input.experiment_id || plan.experiment_id || `exp-${T().slugify(path, 30)}`;
      const varId = input.variation_id || (treat || {}).id || "variant_b";
      const inventory = elementInventory(doc);
      const plannedNote = planned !== request ? planned : "";
      /* ---- brand tokens: computed styles in a rendered frame */
      let cands = [];
      for (const q of [...request.matchAll(QUOTED())].map((x) => x[1])) { const el = findByText(doc, q, request); if (el) cands.push(XP.uniqueSelector(el, doc)); }
      const descEl = findByDescription(doc, request);
      if (descEl) cands.push(XP.uniqueSelector(descEl, doc));
      cands = [...new Set(cands)].slice(0, 6);
      const width = input.width || 1280, height = input.height || 900;
      let tok;
      try {
        const frame = await CAF.lib.html.renderInFrame(html, { width, height });
        try { tok = frameBrandTokens(frame.doc, frame.win, cands); tok.source = "computed styles (rendered frame in the console)"; } finally { frame.remove(); }
      } catch (e) { ctx.warn(`Browser style capture failed (${e.message}); using static CSS tokens.`); tok = staticBrandTokens(cssTexts); ctx.degraded("no rendered frame: brand tokens from static CSS"); }
      if (!tok.targets || !Object.keys(tok.targets).length) {
        const props = tok.custom_properties || {};
        tok.targets = tok.targets || {};
        for (const sel of cands) { const el = qsa(doc, sel)[0]; if (el) { const st = staticStyle(el, cssTexts, props); tok.targets[sel] = { font_size: st["font-size"] || ((st.font || "").match(/\d+px/) || [null])[0], padding: st.padding || "", font_weight: st["font-weight"] ?? null, background: st.background || st["background-color"] || "" }; } }
      }
      /* ---- plan the patch (model phrases operations; code validates) */
      ctx.progress("Planning the DOM patch");
      const vars = { request, planned: plannedNote, url: url || "(html input)", inventory, brand_lines: brandLines(tok), ops: OP_NAMES };
      const draft = await XP.generateOrNone(ctx, "patch", vars, "no language model configured: request parsed deterministically");
      let parser = "model", css = "", ops, summaryText;
      if (draft) {
        ops = (draft.operations || []).map(mkOp);
        css = draft.css || "";
        const bad = ops.map((o, i) => [i, validateOp(o, doc)]).filter(([, p]) => p.length);
        if (bad.length && ctx.llmLive) {
          try {
            const fixed = await ctx.generate("repair", { request, findings: bad.map(([i, p]) => `operation ${i} (${ops[i].op} ${ops[i].selector}): ${p.join("; ")}`), plan: draft, inventory, ops: OP_NAMES });
            ops = (fixed.operations || []).map(mkOp); css = fixed.css || "";
          } catch (e) { ctx.warn(`Repair pass failed (${e.message}); invalid operations are dropped.`); }
        }
        summaryText = draft.summary;
        if (!ops.some((o) => !validateOp(o, doc).length)) {
          let [det] = parseRequest(request, doc, tok.targets, tok);
          if (!det.length && plannedNote) [det] = parseRequest(plannedNote, doc, tok.targets, tok);
          if (det.length) { ctx.warn("The model's operations failed validation; the deterministic request parser was used instead."); ops = det; css = ""; parser = "deterministic"; summaryText = `Deterministic patch for: ${T().truncate(request, 120)}`; }
        }
      } else {
        let notes;
        [ops, notes] = parseRequest(request, doc, tok.targets, tok);
        if (!ops.length && plannedNote) { const [o2, n2] = parseRequest(plannedNote, doc, tok.targets, tok); ops = o2; notes = [...notes, ...n2]; }
        notes.forEach((n) => ctx.warn(n));
        parser = ops.length ? "deterministic" : "none";
        summaryText = `Deterministic patch for: ${T().truncate(request, 120)}`;
        if (!ops.length) ctx.warn("The request could not be turned into operations without a model; configure CAF_LLM_PROVIDER or rephrase (e.g. Change \"X\" to \"Y\", make the button more prominent, hide the promo band, move X above Y).");
      }
      const [cssClean, cssRemoved] = sanitizeCss(css);
      css = cssClean;
      if (cssRemoved.length) ctx.warn(`Removed unsafe CSS: ${cssRemoved.join(", ")}.`);
      const entries = ops.map((op, i) => {
        const problems = validateOp(op, doc);
        if (HTML_OPS.has(op.op)) { const [, removed] = sanitizeHtml(op.value); if (removed.length) ctx.warn(`Operation ${i}: removed unsafe markup ${[...new Set(removed)].sort().join(", ")}.`); }
        const entry = { ...op, status: problems.length ? "invalid" : "applied", matches: selectorError(op.selector, doc) ? 0 : qsa(doc, op.selector).length, message: problems.join("; ") };
        ctx.check({ rule: `patch.op.${i}`, status: problems.length ? "fail" : "pass", severity: "high", target: op.selector, message: `${op.op} ${op.selector}: ` + (problems.length ? problems.join("; ") : `valid, matches ${entry.matches} element(s)`) });
        return entry;
      });
      /* ---- compile to the vendor-neutral bundle (escaped data + fixed templates) */
      const validIdx = entries.map((o, i) => [o, i]).filter(([o]) => o.status !== "invalid").map(([, i]) => i);
      const valid = validIdx.map((i) => mkOp(entries[i]));
      const js = valid.length ? compilePatch(valid, `${expId}:${varId}`) : "";
      const selectors = [...new Set([...valid.map((o) => o.selector), ...valid.filter((o) => o.op === "move" && o.target).map((o) => o.target)])].sort();
      const off = offBrandColours(valid, tok, css);
      ctx.check({ rule: "brand.palette", status: off.length ? "warn" : "pass", severity: "medium", message: off.length ? `Colours outside the page's palette: ${off.join(", ")}` : "All colours used by the variation come from the page's own palette." });
      /* ---- validate: apply through DOM APIs in a rendered frame, verify, DOM-level diff */
      const validation = { browser_used: false, engine: null, selectors_valid: entries.length > 0 && entries.every((o) => o.status !== "invalid"), off_brand_colours: off };
      let appliedHtml = null;
      if (!js) { Object.assign(validation, { all_ops_verified: false, changed_ratio: 0, bbox: null }); ctx.warn("No valid operations to apply."); }
      else {
        ctx.progress("Applying the variation in a rendered frame");
        try {
          const frame = await CAF.lib.html.renderInFrame(html, { width, height });
          try {
            const fdoc = frame.doc, win = frame.win;
            const counts = valid.map((o) => qsa(fdoc, o.selector).length);
            const before = snapshot(fdoc, win);
            if (css) { const st = fdoc.createElement("style"); st.setAttribute("data-caf-variation", varId); st.textContent = css; (fdoc.head || fdoc.documentElement).appendChild(st); }
            const movedEls = new Set(valid.filter((o) => o.op === "move").flatMap((o) => (o.all_matches ? qsa(fdoc, o.selector) : qsa(fdoc, o.selector).slice(0, 1))));
            const { errors } = applyOps(fdoc, valid);
            const verify = verifyOps(fdoc, win, valid, counts);
            const after = snapshot(fdoc, win);
            const diff = domDiff(fdoc, before, after, movedEls);
            appliedHtml = "<!doctype html>\n" + fdoc.documentElement.outerHTML;
            validation.browser_used = true;
            validation.engine = "console iframe: operations applied with DOM APIs (no eval), verified with computed styles";
            validIdx.forEach((i, k) => {
              const o = entries[i], c = counts[k], v = verify[k];
              o.matches = c;
              if (c <= 0) { o.status = "failed"; o.message = "selector matches nothing in the rendered page"; }
              else { o.status = v.ok ? "verified" : "failed"; o.message = v.msg || ""; }
            });
            Object.assign(validation, { dom_diff: diff, changed_ratio: null, bbox: diff.bbox, bbox_before: diff.bbox_before, layout_shift_px: diff.layout_shift_px, size_before: diff.size_before, size_after: diff.size_after,
              pixel_diff: "not available in the hosted console (needs the Python service's Playwright screenshots)", console_errors: errors.slice(0, 20) });
            ctx.check({ rule: "render.dom_change", status: diff.changed_elements > 0 ? "pass" : "fail", severity: "high",
              message: `${diff.changed_elements} element(s) changed (${Object.entries(diff.counts).map(([k, n]) => `${k} ${n}`).join(", ") || "none"}), bounding box ${JSON.stringify(diff.bbox)}, page height ${diff.layout_shift_px >= 0 ? "+" : ""}${diff.layout_shift_px} px.` });
            ctx.check({ rule: "render.console", status: errors.length ? "warn" : "pass", severity: "medium", message: errors.length ? `${errors.length} console message(s): ${errors[0].slice(0, 160)}` : "No script errors while applying the variation." });
            ctx.degraded("pixel screenshots and the visual diff need the Python service (Playwright); the console verified the variation at DOM level in a rendered frame");
          } finally { frame.remove(); }
        } catch (e) {
          ctx.warn(`Browser frame validation failed (${e.message}); static validation only.`);
          ctx.degraded("browser validation failed");
        }
        if (!validation.browser_used) {
          const sdoc = CAF.lib.html.parse(html);
          const { counts } = applyOps(sdoc, valid);
          if (css && sdoc.head) { const st = sdoc.createElement("style"); st.setAttribute("data-caf-variation", "1"); st.textContent = css; sdoc.head.appendChild(st); }
          appliedHtml = "<!doctype html>\n" + sdoc.documentElement.outerHTML;
          validIdx.forEach((i, k) => { const o = entries[i], c = counts[k]; o.matches = c; o.status = c > 0 ? "applied" : "failed"; o.message = o.message || (c > 0 ? "applied to the static HTML" : "no element matched"); });
          Object.assign(validation, { changed_ratio: null, bbox: null });
        }
      }
      if (js) {
        validation.all_ops_verified = validation.browser_used ? entries.every((o) => o.status === "verified") : false;
        validation.ops_ok = entries.filter((o) => ["verified", "applied"].includes(o.status)).length;
        validation.ops_total = entries.length;
        ctx.check({ rule: "patch.verified", status: validation.all_ops_verified ? "pass" : validation.ops_ok ? "warn" : "fail", severity: "high",
          message: `${validation.ops_ok} of ${entries.length} operation(s) applied` + (validation.browser_used ? " and verified in the rendered frame" : " statically") });
      }
      /* ---- deliver: bundle, preview, patch + approval-gated deploy */
      const good = entries.filter((o) => ["verified", "applied"].includes(o.status));
      let bundleArt = null, previewArt = null;
      if (js) {
        const bundle = runtimeBundle(expId, varId, css, js);
        ctx.artifact({ name: "variation.js", mime: "text/javascript", content: bundle, description: "Vendor-neutral variation bundle (CSS + compiled DOM patch)" });
        bundleArt = "variation.js";
        if (css) ctx.artifact({ name: "variation.css", mime: "text/css", content: css, description: "Variation CSS" });
        const inject = `${url ? `<base href="${url}">` : ""}<script data-caf-preview>${bundle.split("</").join("<\\/")}</script>`;
        const preview = /<head[^>]*>/i.test(html) ? html.replace(/(<head[^>]*>)/i, (m0) => m0 + inject) : inject + html;
        ctx.artifact({ name: "preview.html", mime: "text/html", content: preview, description: "Page with the variation bundle injected (open in a browser)" });
        previewArt = "preview.html";
        if (appliedHtml) ctx.artifact({ name: "applied.html", mime: "text/html", content: appliedHtml, description: "Page with the variation applied (DOM snapshot, scripts inert)" });
      }
      ctx.artifact({ name: "patch.json", mime: "application/json", content: JSON.stringify({ request, operations: entries, css, selectors }, null, 2), description: "Declarative DOM patch" });
      let proposed = false;
      if (input.deploy !== false && good.length && js) {
        ctx.action({ id: `flags-${T().slugify(varId, 20)}`, kind: "flags.deploy", connector: "flags", method: "deploy_variation", requires_approval: true,
          payload: { experiment_id: expId, variation_id: varId, css, js, selectors, preview: true }, summary: `Deploy variation '${varId}' of ${expId} (${good.length} operation(s)) as a preview bundle` });
        proposed = true;
      }
      const output = { request, page_url: url, experiment_id: expId, variation_id: varId, summary_text: summaryText, parser, operations: entries, css, js, selectors, brand_tokens: tok, validation,
        screenshots: {}, preview_artifact: previewArt, bundle_artifact: bundleArt, deploy_proposed: proposed };
      const d = validation.dom_diff;
      const summary = !entries.length
        ? `No variation built for '${T().truncate(request, 80)}': the request could not be turned into page operations (${parser === "none" ? "no model configured" : "invalid operations"})`
        : `Variation '${varId}': ${good.length}/${entries.length} operation(s) ${validation.browser_used ? "verified in the rendered frame" : "applied statically"}` + (d ? `, DOM diff: ${d.changed_elements} element(s) changed, page height ${d.layout_shift_px >= 0 ? "+" : ""}${d.layout_shift_px} px` : "") + (proposed ? "; deploy proposed (needs approval)" : "");
      const view = [{ type: "table", title: `Operations (${parser})`, rows: entries.map((o) => ({ op: o.op, selector: o.selector, status: o.status, matches: o.matches, detail: o.message || o.reason })) }];
      if (d) view.push({ type: "table", title: "DOM-level diff", rows: d.changes.map((c) => ({ element: c.selector, change: c.change, before: c.bbox_before ? c.bbox_before.join(", ") : "", after: c.bbox_after ? c.bbox_after.join(", ") : "" })) });
      if (appliedHtml) view.push({ type: "html", title: "Variation preview (applied in the console)", artifact: "applied.html" });
      if (js) view.push({ type: "code", title: "Compiled bundle (escaped data + fixed templates)", code: js });
      return { output, summary, view };
    },
    selftest() {
      return { url: DEMO_URL, change_request: "Change the hero button text \"Get a quote\" to \"See my price in 2 minutes\" and make it more prominent", experiment_id: "exp-hero-cta-price-promise", variation_id: "variant_b" };
    },
  });
  XP.variation = { sanitizeHtml, sanitizeCss, validateOp, compilePatch, applyOps, parseRequest, prominenceStyles, nearestBrandColour, offBrandColours, elementInventory };
})();
