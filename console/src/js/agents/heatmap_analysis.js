/* Heatmap Analysis - browser twin of caf/agents/web/heatmap_analysis.py.
   Same stages (load -> dom -> metrics -> insights -> report). Events: the embedded behaviour export
   (behaviour/heatmap_events = the Python behaviour:csv connector, same page matching and ordering).
   DOM mapping (control type semantic / scripted / none, component labels, CTAs) is a port of the
   BeautifulSoup helpers on DOMParser; the real layout of sections and CTAs (exposure, false-bottom
   attribution, button-like styling) is measured in a same-origin iframe instead of Playwright. Metrics
   are ports of the pandas code: sessions, reach curve + false bottom, rage bursts, dead clicks, raw and
   exposure-adjusted CTA CTR, attention by depth band, NaN-safe device split, and the Clarity-style
   aggregated mode. Click map: SVG density (64-column histogram + Gaussian blur, sigma 1.4, like
   scipy.ndimage) on page coordinates with section bands; scroll curve via CAF.lib.chart. */
(() => {
  const AGENT = "heatmap_analysis";
  const SEMANTIC_TAGS = new Set(["a", "button", "input", "select", "textarea", "summary", "label", "option", "details"]);
  const SEMANTIC_ROLES = new Set(["button", "link", "tab", "checkbox", "radio", "switch", "menuitem", "option", "combobox", "slider", "treeitem"]);
  const CTA_CLASS = /(^|[\s_-])(btn|button|cta)([\s_-]|$)/i;
  const GENERIC_CLASSES = new Set(["section", "wrap", "section-sea", "thin", "container", "row", "col"]);
  const LANDMARKS = new Set(["section", "header", "footer", "nav", "aside", "article", "form", "figure"]);
  const AGGREGATE_EVENTS = { rage_click: "Rage clicks", dead_click: "Dead clicks", error_click: "Error clicks", quickback_click: "Quick-back clicks", excessive_scroll: "Excessive scrolling", script_error: "Script errors" };
  const SEQ_BLUE = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"];
  const INK = "#0b0b0b", INK2 = "#52514e", MUTED = "#898781", GRID = "#e1e0d9", SURFACE = "#fcfcfb", CRITICAL = "#d03b3b", SERIOUS = "#ec835a";

  // ------------------------------------------------------------------ Python parity helpers
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
  const pyRound = (x, d) => Number(pyFixed(x, d));
  const pyRepr = (x) => (x === null || x === undefined || Number.isNaN(x) ? "None" : Number.isInteger(x) ? x.toFixed(1) : String(x));
  const isNum = (v) => typeof v === "number" && !Number.isNaN(v);
  const median = (a) => { const s = a.filter(isNum).sort((x, y) => x - y); const n = s.length; if (!n) return NaN; return n % 2 ? s[(n - 1) / 2] : (s[n / 2 - 1] + s[n / 2]) / 2; };
  const mean = (a) => { const s = a.filter(isNum); return s.length ? s.reduce((t, x) => t + x, 0) / s.length : NaN; };
  const num = (v, d = null) => (v === null || v === undefined || !Number.isFinite(Number(v)) ? null : d === null ? Number(v) : pyRound(Number(v), d));
  const capitalize = (s) => (s ? s[0].toUpperCase() + s.slice(1).toLowerCase() : s);
  const humanize = (t) => capitalize(String(t).replace(/[-_]+/g, " ").trim());
  const fmtPct1 = (v) => `${pyFixed(v * 100, 1)}%`;
  const cmp = (a, b) => (a < b ? -1 : a > b ? 1 : 0);
  /** pandas groupby: keys sorted, NaN/None keys dropped. */
  function groupBy(rows, key) { const m = new Map(); for (const r of rows) { const k = typeof key === "function" ? key(r) : r[key]; if (k === null || k === undefined) continue; if (!m.has(k)) m.set(k, []); m.get(k).push(r); } return new Map([...m.entries()].sort((a, b) => cmp(String(a[0]), String(b[0])))); }
  /** Python's built-in max()/min() with NaN: the first argument wins unless the comparison is true. */
  const pmax = (a, b) => (b > a ? b : a), pmin = (a, b) => (b < a ? b : a);
  /** BeautifulSoup get_text(" ") squashed, and get_text(" ", strip=True). */
  const rawText = (el) => { const out = []; const walk = (n) => { for (const c of n.childNodes) { if (c.nodeType === 3) out.push(c.nodeValue); else if (c.nodeType === 1) walk(c); } }; if (el) walk(el); return out.join(" "); };
  const squash = (s) => String(s || "").split(/\s+/).filter(Boolean).join(" ");
  const stripText = (el) => { const out = []; const walk = (n) => { for (const c of n.childNodes) { if (c.nodeType === 3) { const t = c.nodeValue.trim(); if (t) out.push(t); } else if (c.nodeType === 1) walk(c); } }; if (el) walk(el); return out.join(" "); };
  const classes = (el) => String(el.getAttribute("class") || "").split(/\s+/).filter(Boolean);
  const ancestors = (el) => { const out = []; for (let p = el.parentElement; p; p = p.parentElement) out.push(p); return out; };

  // ------------------------------------------------------------------ DOM helpers (pure ports)
  function scriptedSelectors(doc) {
    const out = new Set();
    for (const s of doc.querySelectorAll("script")) {
      const code = s.textContent || "";
      if (!/addEventListener\(\s*['"](click|pointerdown|mousedown|touchstart)|\.onclick\s*=/.test(code)) continue;
      for (const m of code.matchAll(/querySelector(?:All)?\(\s*['"]([^'"]+)['"]\s*\)/g)) out.add(m[1]);
      for (const m of code.matchAll(/getElementById\(\s*['"]([^'"]+)['"]\s*\)/g)) out.add(`#${m[1]}`);
    }
    return out;
  }
  function controlType(el, scripted) {
    for (const node of [el, ...ancestors(el).slice(0, 4)]) {
      if (SEMANTIC_TAGS.has(node.localName) && (node.localName !== "a" || node.getAttribute("href"))) return "semantic";
      if (SEMANTIC_ROLES.has(node.getAttribute("role") || "") || node.getAttribute("onclick")) return node.getAttribute("role") ? "semantic" : "scripted";
    }
    for (const sel of scripted) { try { if (el.matches(sel)) return "scripted"; } catch { /* invalid selector */ } }
    return "none";
  }
  function componentLabel(el, doc) {
    for (const node of [el, ...ancestors(el)]) {
      if (!LANDMARKS.has(node.localName)) continue;
      let heading = "";
      if (node.getAttribute("aria-labelledby")) { const ref = doc.getElementById(node.getAttribute("aria-labelledby").split(/\s+/)[0]); heading = ref ? stripText(ref) : ""; }
      heading = heading || node.getAttribute("aria-label") || "";
      if (!heading) { const h = node.querySelector("h1,h2,h3,h4"); heading = h ? stripText(h) : ""; }
      const cls = classes(node).filter((c) => !["section", "wrap", "section-sea", "thin"].includes(c));
      let kind = humanize(node.getAttribute("id") || (cls.length ? cls[0] : node.localName));
      kind = /(section|band|header|footer|form|nav)$/.test(kind.toLowerCase()) ? kind : `${kind} section`;
      return heading ? `${kind} ('${[...heading].slice(0, 60).join("")}')` : kind;
    }
    return "Page body";
  }
  const isHidden = (el) => [el, ...ancestors(el)].some((p) => p.hasAttribute("hidden"));
  function describeSelectors(doc, selectors) {
    const blank = { found: false, component: "", text: "", tag: "", control: "unknown", hidden: false, styled_cta: false };
    const out = {};
    if (!doc) { for (const s of selectors) out[s] = { ...blank }; return out; }
    const scripted = scriptedSelectors(doc);
    for (const sel of selectors) {
      let el = null;
      try { el = doc.querySelector(sel); } catch { el = null; }
      if (!el) { out[sel] = { ...blank }; continue; }
      out[sel] = { found: true, component: componentLabel(el, doc), text: [...squash(rawText(el))].slice(0, 80).join("") || el.getAttribute("alt") || "", tag: el.localName,
        control: controlType(el, scripted), hidden: isHidden(el), styled_cta: CTA_CLASS.test(classes(el).join(" ")) };
    }
    return out;
  }
  function findCtas(doc) {
    if (!doc) return [];
    const out = [];
    for (const el of doc.querySelectorAll("a,button")) {
      const ident = classes(el).join(" ") + " " + (el.getAttribute("id") || "");
      if (!(CTA_CLASS.test(ident) || ident.toLowerCase().includes("cta"))) continue;
      let sel;
      if (el.getAttribute("id")) sel = `#${el.getAttribute("id")}`;
      else {
        const anc = ancestors(el).find((p) => p.getAttribute("id") || (p.localName === "section" && classes(p).length));
        const ancCls = anc ? classes(anc).filter((c) => c !== "section" && c !== "wrap") : [];
        const prefix = anc && anc.getAttribute("id") ? `#${anc.getAttribute("id")} ` : anc && ancCls.length ? `.${ancCls[0]} ` : "";
        sel = prefix + el.localName + classes(el).slice(0, 2).map((c) => `.${c}`).join("");
      }
      out.push({ selector: sel.trim(), label: [...squash(rawText(el))].slice(0, 60).join(""), component: componentLabel(el, doc), hidden: isHidden(el), styled_cta: CTA_CLASS.test(classes(el).join(" ")) });
    }
    const seen = new Set();
    return out.filter((c) => (seen.has(c.selector) ? false : (seen.add(c.selector), true)));
  }
  function matchSelector(eventSel, ctaSel) {
    if (eventSel === ctaSel) return true;
    const norm = (s) => s.replace(/\b(a|button|div|span)(?=[.#])/g, "").replace(/ /g, "");
    const ta = new Set(norm(eventSel).match(/[#.][\w-]+/g) || []), tb = new Set(norm(ctaSel).match(/[#.][\w-]+/g) || []);
    return ta.size > 0 && [...ta].every((t) => tb.has(t));
  }
  /** LAYOUT_JS, run directly against the rendered frame (no eval). */
  function measureLayout(win, doc, sels) {
    const H = doc.documentElement.scrollHeight, out = { page_h: H, page_w: doc.documentElement.scrollWidth, sections: [], elements: {} };
    doc.querySelectorAll("main > section, main > aside, body > header, body > footer, main section[id]").forEach((s) => {
      const r = s.getBoundingClientRect(), h = s.querySelector("h1,h2,h3");
      const label = String(s.getAttribute("aria-label") || (h && h.textContent) || s.id || s.className || s.tagName).trim().replace(/\s+/g, " ");
      out.sections.push({ top: r.top + win.scrollY, bottom: r.bottom + win.scrollY, label: label.slice(0, 70), cls: s.className || s.tagName.toLowerCase() });
    });
    for (const sel of sels) {
      try {
        const e = doc.querySelector(sel);
        if (!e) continue;
        const r = e.getBoundingClientRect(), cs = win.getComputedStyle(e);
        const padY = parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom);
        const bg = cs.backgroundColor, hasBg = bg && bg !== "rgba(0, 0, 0, 0)" && bg !== "transparent";
        const edge = (parseFloat(cs.borderTopWidth) > 0 && cs.borderTopStyle !== "none") || (cs.boxShadow && cs.boxShadow !== "none");
        out.elements[sel] = { top: r.top + win.scrollY, left: r.left, width: r.width, height: r.height, button_like: !!(padY >= 12 && (hasBg || edge)) };
      } catch { /* invalid selector */ }
    }
    return out;
  }
  const BLANK_GIF = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7";
  const isInline = (u) => /^\s*(data|blob):/i.test(String(u || ""));
  /** Pasted HTML is laid out in the console's origin: no page scripts, no external resources. */
  function sanitizeForFrame(html) {
    const doc = CAF.lib.html.parse(html);
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
    return doctype + doc.documentElement.outerHTML;
  }

  // ------------------------------------------------------------------ metrics (pure ports)
  function sessionsFrame(ev) {
    const out = new Map();
    for (const [sid, g] of groupBy(ev, "session_id")) {
      const ts = g.map((e) => e.t);
      const start = Math.min(...ts), end = Math.max(...ts);
      const dev = (g.find((e) => e.device !== null && e.device !== undefined) || {}).device;
      const scrolls = g.map((e) => e.scroll_pct).filter(isNum);
      out.set(sid, { session_id: sid, device: dev === undefined ? null : String(dev), max_scroll: scrolls.length ? Math.max(...scrolls) : NaN, start, end, clicks: g.filter((e) => e.event_type === "click").length, dwell_s: (end - start) / 1000 });
    }
    return out;
  }
  const reachCurve = (maxScroll, step = 10) => { const out = []; for (let d = 0; d <= 100; d += step) out.push({ depth: d, reach: maxScroll.length ? maxScroll.filter((v) => isNum(v) && v >= d).length / maxScroll.length : 0 }); return out; };
  function detectFalseBottom(maxScroll, minDropPp = 15, step = 5) {
    const curve = reachCurve(maxScroll, step), drops = [];
    for (let i = 1; i < curve.length; i++) {
      const a = curve[i - 1], b = curve[i];
      if (b.depth > 90 || (a.reach >= 0.999 && b.reach >= 0.999)) continue;
      drops.push({ from: a.depth, to: b.depth, pp: (a.reach - b.reach) * 100, before: a.reach, after: b.reach });
    }
    if (!drops.length) return { detected: false };
    const best = drops.reduce((m, d) => (d.pp > m.pp ? d : m));
    const others = drops.filter((d) => d !== best).map((d) => d.pp);
    const typical = others.length ? median(others) : 0;
    return { detected: best.pp >= minDropPp && best.pp >= 2.5 * Math.max(typical, 1), depth_from: Math.trunc(best.from), depth_to: Math.trunc(best.to), drop_pp: pyRound(best.pp, 1),
      typical_drop_pp: pyRound(typical, 1), reach_before: pyRound(best.before, 3), reach_after: pyRound(best.after, 3) };
  }
  function rageEpisodes(clicks, windowS = 2, minClicks = 3) {
    const rows = [];
    const sorted = [...clicks].sort((a, b) => a.t - b.t);
    const groups = new Map();
    for (const c of sorted) { const k = JSON.stringify([String(c.session_id), String(c.selector)]); if (!groups.has(k)) groups.set(k, { sid: c.session_id, sel: c.selector, items: [] }); groups.get(k).items.push(c); }
    const keys = [...groups.keys()].sort((a, b) => { const [s1, x1] = JSON.parse(a), [s2, x2] = JSON.parse(b); return cmp(s1, s2) || cmp(x1, x2); });
    for (const k of keys) {
      const g = groups.get(k), t = g.items.map((c) => c.t / 1000);
      let i = 0;
      while (i < t.length) {
        let j = i;
        while (j + 1 < t.length && t[j + 1] - t[i] <= windowS) j++;
        if (j - i + 1 >= minClicks) { rows.push({ session_id: g.sid, selector: g.sel, clicks: j - i + 1, span_s: pyRound(t[j] - t[i], 2) }); i = j + 1; }
        else i++;
      }
    }
    return rows;
  }
  function clickTable(clicks, nSessions, dom, rage, pageH) {
    const total = clicks.length, rows = [];
    for (const [sel, g] of groupBy(clicks, "selector")) {
      const info = dom[sel] || {};
      let control = info.control || "unknown";
      if (control === "unknown") control = Math.max(...g.map((e) => (isNum(e.is_interactive) ? e.is_interactive : -Infinity))) === 1 ? "semantic" : "none";
      const r = rage.filter((x) => x.selector === sel);
      const gph = median(g.map((e) => e.page_h));
      const depth = g.filter((e) => isNum(e.y)).map((e) => (e.y / (pageH[e.device] !== undefined ? pageH[e.device] : gph)) * 100);
      const sessions = new Set(g.map((e) => e.session_id)).size;
      const firstText = g.find((e) => e.element_text !== null && e.element_text !== undefined);
      rows.push({ selector: sel, component: info.component || "", element_text: info.text || (firstText ? String(firstText.element_text) : ""), tag: info.tag || (g[0].tag === null || g[0].tag === undefined ? "nan" : String(g[0].tag)),
        control, clicks: g.length, sessions, click_share: total ? g.length / total : 0, ctr: nSessions ? sessions / nSessions : 0, dead_clicks: control === "none" ? g.length : 0,
        rage_episodes: r.length, rage_sessions: new Set(r.map((x) => x.session_id)).size, median_x: median(g.map((e) => e.x)), median_y: median(g.map((e) => e.y)),
        depth_pct: depth.length ? pyRound(median(depth), 1) : null });
    }
    return rows.sort((a, b) => b.clicks - a.clicks);
  }
  function ctaTable(ctas, clicks, sess, pageH, layout, ignoredCtr, lowCtr) {
    const n = sess.length, rows = [];
    const selectors = [...new Set(clicks.map((c) => c.selector).filter((s) => s !== null && s !== undefined))].sort(cmp);
    for (const c of ctas) {
      const evs = selectors.filter((s) => matchSelector(s, c.selector));
      const g = clicks.filter((e) => evs.includes(e.selector));
      const clicking = new Set(g.map((e) => e.session_id)).size;
      let depthByDev = {};
      for (const [dev, gd] of groupBy(g, "device")) depthByDev[dev] = (median(gd.map((e) => e.y)) / (pageH[dev] !== undefined ? pageH[dev] : median(gd.map((e) => e.page_h)))) * 100;
      if (layout && layout[c.selector]) depthByDev = { desktop: layout[c.selector].depth_pct, ...Object.fromEntries(Object.entries(depthByDev).filter(([k]) => k !== "desktop")) };
      let exposed = 0;
      for (const [dev, depth] of Object.entries(depthByDev)) exposed += sess.filter((s) => s.device === dev && isNum(s.max_scroll) && s.max_scroll >= depth).length;
      const depths = Object.values(depthByDev);
      if (depths.length) { const med = median(depths); exposed += sess.filter((s) => !(s.device in depthByDev) && isNum(s.max_scroll) && s.max_scroll >= med).length; }
      const ctr = n ? clicking / n : 0, ctrExp = exposed ? clicking / exposed : null;
      const status = c.hidden && clicking === 0 ? "hidden" : ctr < ignoredCtr && (ctrExp === null || ctrExp < 0.08) ? "ignored" : ctr < lowCtr ? "low" : "ok";
      const bl = layout && layout[c.selector] ? layout[c.selector].button_like : undefined;
      rows.push({ ...c, button_like: bl === undefined ? null : bl, event_selectors: evs, clicks: g.length, sessions: clicking, ctr, exposed_sessions: exposed, ctr_exposed: ctrExp,
        depth_pct: depths.length ? pyRound(median(depths), 1) : null, status });
    }
    return rows.sort((a, b) => b.ctr - a.ctr);
  }
  function attentionByBand(ev, band = 10) {
    const secs = new Array(100 / band).fill(0);
    const sessions = new Set(ev.map((e) => e.session_id)).size || 1;
    const sorted = [...ev].sort((a, b) => a.t - b.t);
    for (const [, g0] of groupBy(sorted, "session_id")) {
      const g = g0.filter((e) => ["pageview", "scroll", "click", "leave"].includes(e.event_type));
      for (let i = 0; i < g.length - 1; i++) {
        const dt = pmax(0, (g[i + 1].t - g[i].t) / 1000);
        const f = (v) => (isNum(v) ? v : NaN);
        const reach = f(g[i].scroll_pct), vh = (f(g[i].viewport_h) / f(g[i].page_h)) * 100;
        const top = pmax(0, reach - vh), bottom = pmin(100, reach);
        for (let b = 0; b < secs.length; b++) {
          const lo = b * band, hi = (b + 1) * band;
          const overlap = pmax(0, pmin(hi, bottom) - pmax(lo, top));
          if (overlap > 0) secs[b] += (dt * overlap) / band;
        }
      }
    }
    return secs.map((s, b) => ({ band: `${b * band}-${(b + 1) * band}%`, seconds: pyRound(s / sessions, 2) }));
  }
  /** Per-device sessions, scroll reach, dwell and click behaviour; NaN-safe (all-NaN scroll -> null). */
  function deviceSplit(sess, clicks, rage) {
    const n = sess.length, out = [];
    for (const [dev, g] of groupBy(sess, "device")) {
      const c = clicks.filter((e) => e.device === dev), ids = new Set(g.map((s) => s.session_id));
      const r = rage.filter((x) => ids.has(x.session_id));
      out.push({ device: String(dev), sessions: g.length, share: n ? g.length / n : 0, median_max_scroll: num(median(g.map((s) => s.max_scroll))), median_dwell_s: num(median(g.map((s) => s.dwell_s)), 1),
        clicks_per_session: g.length ? pyRound(c.length / g.length, 2) : 0, dead_click_share: c.length ? pyRound(c.filter((e) => e.is_interactive === 0).length / c.length, 3) : 0,
        rage_sessions: new Set(r.map((x) => x.session_id)).size, dead_click_session_share: null, rage_session_share: null });
    }
    return out.sort((a, b) => b.sessions - a.sessions);
  }
  /** Aggregated exports (Clarity-style rows such as rage_click / dead_click without coordinates or selectors). */
  const isAggregated = (ev) => ev.some((e) => e.event_type in AGGREGATE_EVENTS) && !ev.some((e) => e.event_type === "click" && isNum(e.x) && e.selector);
  function aggregateMetrics(ev) {
    const dev = (e) => (e.device === null || e.device === undefined ? "unknown" : String(e.device));
    const pv = ev.filter((e) => e.event_type === "pageview");
    const sessions = new Set(pv.map((e) => e.session_id)).size;
    const devSessions = Object.fromEntries([...groupBy(pv, dev)].map(([d, g]) => [d, new Set(g.map((e) => e.session_id)).size]));
    const metrics = [];
    for (const [et, label] of Object.entries(AGGREGATE_EVENTS)) {
      const sub = ev.filter((e) => e.event_type === et);
      if (!sub.length) continue;
      const byDev = {};
      for (const [d, g] of groupBy(sub, dev)) { const ds = devSessions[d] || 0, ss = new Set(g.map((e) => e.session_id)).size; byDev[d] = { occurrences: g.length, sessions: ss, session_share: ds ? pyRound(ss / ds, 4) : null }; }
      const affected = new Set(sub.map((e) => e.session_id)).size;
      metrics.push({ metric: et, label, occurrences: sub.length, sessions: affected, session_share: sessions ? pyRound(affected / sessions, 4) : null, by_device: byDev });
    }
    const scroll = ev.filter((e) => e.event_type === "scroll" && isNum(e.scroll_pct));
    const avgScroll = Object.fromEntries([...groupBy(scroll, dev)].map(([d, g]) => [d, num(mean(g.map((e) => e.scroll_pct)), 1)]));
    return { sessions, device_sessions: devSessions, metrics, avg_scroll_depth: avgScroll };
  }
  function aggregateInsights(agg, minSessions) {
    const advice = { rage_click: ["high", "find the elements behind them in session recordings filtered by rage clicks and make them respond", "rage-click session share"],
      dead_click: ["medium", "make the elements visitors click on actionable (or remove their clickable look)", "dead-click session share"],
      error_click: ["high", "fix the JavaScript errors triggered by clicks", "error-click session share"], script_error: ["high", "fix the script errors on the page", "script-error session share"],
      quickback_click: ["medium", "make link destinations match what the link promises", "quick-back session share"],
      excessive_scroll: ["low", "surface the content visitors scroll back and forth to find", "excessive-scroll session share"] };
    const ins = [], hyps = [];
    for (const m of agg.metrics) {
      if (m.sessions < minSessions) continue;
      const [sev, fix, metric] = advice[m.metric];
      const share = m.session_share !== null ? fmtPct1(m.session_share) : "n/a";
      const devs = Object.entries(m.by_device).filter(([, v]) => v.session_share !== null).map(([d, v]) => `${d} ${fmtPct1(v.session_share)}`).join(", ");
      ins.push({ title: `${m.label} in ${share} of sessions (Clarity aggregate)`, severity: sev, selector: null,
        detail: `${m.occurrences} ${m.label.toLowerCase()} across ${m.sessions} of ${agg.sessions} sessions` + (devs ? ` (${devs})` : "") + ". The export has no selectors, so the elements are not identified." });
      hyps.push({ hypothesis: `If we ${fix}, the ${metric} will fall and more visitors will continue to a quote.`, evidence: `${m.occurrences} ${m.label.toLowerCase()} in ${m.sessions} sessions (${share})`, metric, selector: null });
    }
    return [ins, hyps];
  }
  function nearestComponent(from, to, positions) {
    if (!positions.length) return "";
    const measured = positions.filter((p) => "bottom_pct" in p);
    if (measured.length) {
      const hits = measured.filter((p) => p.top_pct < to && p.bottom_pct > from).sort((a, b) => a.top_pct - b.top_pct);
      if (hits.length) return hits.slice(0, 2).map((p) => p.label).join(" -> ");
    }
    const mid = (from + to) / 2;
    return positions.reduce((b, p) => (Math.abs(p.top_pct - mid) < Math.abs(b.top_pct - mid) ? p : b)).label;
  }
  function sectionLabel(cls, heading) {
    const tokens = String(cls || "").split(/\s+/).filter((t) => t && !GENERIC_CLASSES.has(t.toLowerCase()));
    let kind = tokens.length ? humanize(tokens[0]) : "Section";
    kind = /(section|band|header|footer|hero)$/.test(kind.toLowerCase()) ? kind : `${kind} section`;
    heading = String(heading || "").trim();
    return heading && heading.toLowerCase() !== String(cls || "").toLowerCase() ? `${kind} ('${[...heading].slice(0, 50).join("")}')` : kind;
  }
  function ruleHypotheses(clicks, ctas, fb, devices, minRageSessions) {
    const insights = [], hyps = [];
    for (const c of clicks) if (c.rage_sessions >= minRageSessions) {
      insights.push({ title: `Rage clicks on ${c.element_text || c.selector}`, severity: "high", selector: c.selector,
        detail: `${c.rage_sessions} sessions clicked ${c.selector} in ${c.component || "the page"} 3+ times within seconds (${c.clicks} clicks in total) - the control does not respond as expected.` });
      hyps.push({ hypothesis: `If ${c.element_text || c.selector} in ${c.component || "the page"} responds immediately with its content, rage clicks will disappear and more visitors will continue to a quote.`,
        evidence: `${c.rage_episodes} rage-click bursts from ${c.rage_sessions} sessions on ${c.selector}`, metric: "rage-click sessions on the control; quote starts from that section", selector: c.selector });
    }
    for (const c of clicks) if (c.control === "none" && c.sessions >= 10) {
      insights.push({ title: `Dead clicks on ${c.selector}`, severity: "medium", selector: c.selector,
        detail: `${c.clicks} clicks from ${c.sessions} sessions (${pyFixed(c.ctr * 100, 1)}% of visitors) land on a non-interactive ${c.tag} in ${c.component || "the page"}.` });
      hyps.push({ hypothesis: `If ${c.selector} becomes a link to the quote flow (or loses its clickable look), wasted clicks turn into quote starts.`, evidence: `${c.clicks} dead clicks from ${c.sessions} sessions`, metric: "quote-start CTR from the hero", selector: c.selector });
    }
    const scripted = new Map();
    for (const c of clicks) if (c.control === "scripted") { const k = c.component || c.tag; if (!scripted.has(k)) scripted.set(k, []); scripted.get(k).push(c); }
    for (const [comp, items] of scripted) if (items.reduce((s, i) => s + i.sessions, 0) >= 10)
      insights.push({ title: `Non-semantic controls in ${comp}`, severity: "medium", selector: items[0].selector,
        detail: `${items.map((i) => i.selector).join(", ")} are scripted <${items[0].tag}> elements without a button/tab role or tabindex, so keyboard and screen-reader users cannot operate them (${items.reduce((s, i) => s + i.clicks, 0)} clicks).` });
    if (fb.detected) {
      const comp = fb.component || "a section boundary";
      insights.push({ title: `False bottom at ${fb.depth_from}-${fb.depth_to}% scroll depth`, severity: "high", selector: null,
        detail: `Reach falls from ${pyFixed(fb.reach_before * 100, 0)}% to ${pyFixed(fb.reach_after * 100, 0)}% of sessions (${pyFixed(fb.drop_pp, 0)} pp, typical step ${pyFixed(fb.typical_drop_pp, 0)} pp) around ${comp}; content below it is seen by few visitors.` });
      hyps.push({ hypothesis: `If the page signals that more content follows ${comp} (reduce the full-width break, add a 'compare plans' anchor or a visual cue), more visitors will reach pricing and the FAQ.`,
        evidence: `${pyFixed(fb.drop_pp, 0)} pp drop in scroll reach between ${fb.depth_from}% and ${fb.depth_to}%`, metric: `scroll reach at ${fb.depth_to + 10}% depth; plan-section CTR`, selector: null });
    }
    for (const c of ctas) if (c.status === "ignored") {
      const depth = c.depth_pct;
      const where = depth !== null && depth !== undefined ? ` at ${pyFixed(depth, 0)}% depth` : "";
      const styled = c.button_like === false ? " (styled as a plain text link)" : "";
      const belowFb = !!(fb.detected && depth !== null && depth !== undefined && depth > fb.depth_to);
      insights.push({ title: `Ignored CTA: ${c.label || c.selector}`, severity: "medium", selector: c.selector,
        detail: `${c.selector}${styled}${where} is clicked by ${pyFixed(c.ctr * 100, 1)}% of sessions (${c.sessions} sessions)` + (c.exposed_sessions ? `; only ${c.exposed_sessions} sessions scrolled far enough to see it` : "") +
          (belowFb ? ` - it sits below the ${fb.depth_from}-${fb.depth_to}% scroll drop-off` : "") + "." });
      const change = belowFb ? `'${c.label}' is repeated above the false bottom (before ${fb.depth_from}% depth, e.g. beside the plan comparison)` : c.button_like === false ? `'${c.label}' is styled as a button` : `'${c.label}' gets stronger contrast and value-led copy`;
      hyps.push({ hypothesis: `If ${change}, more of the visitors who are unsure will click it and book help.`,
        evidence: `CTR ${pyFixed(c.ctr * 100, 1)}%` + (c.ctr_exposed !== null && c.ctr_exposed !== undefined ? `, ${pyFixed(c.ctr_exposed * 100, 1)}% of the ${c.exposed_sessions} sessions that reached it` : "") + (belowFb ? `; ${pyFixed((1 - fb.reach_after) * 100, 0)}% of sessions never scroll past ${fb.depth_to}%` : ""),
        metric: `${c.selector} CTR`, selector: c.selector });
    }
    if (devices.length >= 2) {
      const [a, b] = devices;
      if (a.median_max_scroll !== null && b.median_max_scroll !== null && Math.abs(a.median_max_scroll - b.median_max_scroll) >= 10) {
        const [lo, hi] = [a, b].sort((x, y) => x.median_max_scroll - y.median_max_scroll);
        insights.push({ title: `${capitalize(lo.device)} visitors scroll less`, severity: "low", selector: null, detail: `Median reach ${pyFixed(lo.median_max_scroll, 0)}% on ${lo.device} vs ${pyFixed(hi.median_max_scroll, 0)}% on ${hi.device}.` });
      }
    }
    return [insights, hyps];
  }
  function summaryLine(m) {
    const parts = [`${m.n_sessions} sessions analysed`];
    if (m.rage.length) parts.push(`rage clicks on ${m.rage[0].selector} (${m.rage[0].sessions} sessions)`);
    if (m.dead.length) parts.push(`dead clicks on ${m.dead[0].selector} (${m.dead[0].clicks} clicks)`);
    const fb = m.false_bottom;
    if (fb.detected) parts.push(`a false bottom at ${fb.depth_from}-${fb.depth_to}% (-${pyFixed(fb.drop_pp, 0)} pp)`);
    const ign = m.ctas.filter((c) => c.status === "ignored");
    if (ign.length) parts.push(`ignored CTA ${ign[0].selector} (${pyFixed(ign[0].ctr * 100, 1)}% CTR)`);
    return parts.join("; ") + ".";
  }

  // ------------------------------------------------------------------ charts (SVG)
  function gaussianBlur2d(grid, sigma = 1.4) {
    const radius = Math.trunc(4 * sigma + 0.5);
    let w = []; for (let i = -radius; i <= radius; i++) w.push(Math.exp((-0.5 * i * i) / (sigma * sigma)));
    const sw = w.reduce((s, x) => s + x, 0); w = w.map((x) => x / sw);
    const reflect = (i, n) => { while (i < 0 || i >= n) i = i < 0 ? -i - 1 : 2 * n - i - 1; return i; };   // scipy mode="reflect"
    const pass = (g, axis) => g.map((row, y) => row.map((_, x) => { let s = 0; for (let k = -radius; k <= radius; k++) s += w[k + radius] * (axis ? g[reflect(y + k, g.length)][x] : row[reflect(x + k, row.length)]); return s; }));
    return pass(pass(grid, 0), 1);
  }
  function seqColor(v) {
    const pos = Math.min(1, Math.max(0, v)) * (SEQ_BLUE.length - 1), i = Math.min(SEQ_BLUE.length - 2, Math.floor(pos)), f = pos - i;
    const a = SEQ_BLUE[i].match(/\w\w/g).map((h) => parseInt(h, 16)), b = SEQ_BLUE[i + 1].match(/\w\w/g).map((h) => parseInt(h, 16));
    return "#" + a.map((x, k) => Math.round(x + (b[k] - x) * f).toString(16).padStart(2, "0")).join("");
  }
  const esc = U.esc;
  function clickMapSvg(clicks, width, height, marks, sections, title) {
    const W = 480, scale = W / width, Hp = Math.max(320, height * scale), pad = { l: 40, t: 28, r: 12, b: 10 };
    const sx = (x) => pad.l + Math.min(Math.max(x, 0), width) * scale, sy = (y) => pad.t + (Math.min(Math.max(y, 0), height) / height) * Hp;
    let svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${pad.l + W + pad.r} ${pad.t + Hp + pad.b}" width="${pad.l + W + pad.r}" role="img" aria-label="${esc(title)}" style="font-family:system-ui,sans-serif;max-width:100%;height:auto">`;
    svg += `<rect width="100%" height="100%" fill="${SURFACE}"/><text x="${pad.l}" y="18" font-size="13" font-weight="600" fill="${INK}">${esc(title)}</text>`;
    svg += `<rect x="${pad.l}" y="${pad.t}" width="${W}" height="${Hp.toFixed(1)}" fill="#ffffff" stroke="${GRID}"/>`;
    for (const s of sections) {
      const y0 = sy((s.top_pct / 100) * height), y1 = sy((("bottom_pct" in s ? s.bottom_pct : s.top_pct + 3) / 100) * height);
      svg += `<rect x="${pad.l}" y="${y0.toFixed(1)}" width="${W}" height="${Math.max(1, y1 - y0).toFixed(1)}" fill="${GRID}" fill-opacity="0.35" stroke="${GRID}"/><text x="${pad.l + 6}" y="${(y0 + 12).toFixed(1)}" font-size="9" fill="${INK2}">${esc(String(s.label).slice(0, 48))}</text>`;
    }
    if (clicks.length) {
      const bx = 64, by = Math.max(32, Math.trunc((64 * height) / width));
      const hist = Array.from({ length: by }, () => new Array(bx).fill(0));
      for (const c of clicks) {
        const x = Math.min(Math.max(c.x, 0), width), y = Math.min(Math.max(c.y, 0), height);
        hist[Math.min(by - 1, Math.floor((y / height) * by))][Math.min(bx - 1, Math.floor((x / width) * bx))] += 1;
      }
      const dens = gaussianBlur2d(hist, 1.4);
      const max = Math.max(...dens.map((r) => Math.max(...r)));
      if (max > 0) {
        const cw = W / bx, ch = Hp / by;
        dens.forEach((row, yi) => row.forEach((v, xi) => { const d = v / max; if (d >= 0.04) svg += `<rect x="${(pad.l + xi * cw).toFixed(1)}" y="${(pad.t + yi * ch).toFixed(1)}" width="${(cw + 0.3).toFixed(1)}" height="${(ch + 0.3).toFixed(1)}" fill="${seqColor(d)}" fill-opacity="0.85"/>`; }));
      }
    }
    for (const m of marks) {
      const x = sx(m.x), y = sy(m.y);
      svg += `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="6" fill="${m.color}" stroke="${SURFACE}" stroke-width="2"/><text x="${(x + 10).toFixed(1)}" y="${(y + 4).toFixed(1)}" font-size="10" font-weight="700" fill="${INK}" paint-order="stroke" stroke="${SURFACE}" stroke-width="3">${esc(m.label)}</text>`;
    }
    for (const p of [0, 25, 50, 75, 100]) svg += `<text x="${pad.l - 6}" y="${(pad.t + (p / 100) * Hp + 4).toFixed(1)}" font-size="9.5" text-anchor="end" fill="${MUTED}">${p}%</text>`;
    return svg + "</svg>";
  }
  function scrollCurveSvg(curves, fb, themed) {
    const names = Object.keys(curves);
    const labels = (curves[names[0]] || []).map((p) => `${p.depth}%`);
    const markers = fb.detected ? [{ index: Math.round(fb.depth_from / 10), label: `False bottom: -${pyFixed(fb.drop_pp, 0)} pp` }] : [];
    return CAF.lib.chart.line({ series: names.map((n) => ({ name: n.replace(" sessions)", ")"), values: curves[n].map((p) => Math.round(p.reach * 1000) / 10) })), labels, title: "Scroll reach by depth (% of sessions)", markers, themed, yLabel: "%" });
  }

  // ------------------------------------------------------------------ the agent
  CAF.agent(AGENT, {
    async run(input, ctx) {
      // 1. load (behaviour:csv twin)
      if (!ctx.hasData("behaviour/heatmap_events")) throw new Error("The behaviour/heatmap_events export is not embedded in this console build.");
      const all = (await ctx.data("behaviour/heatmap_events")).map((e) => ({ ...e, t: Date.parse(e.ts), session_id: String(e.session_id) }));
      let pageUrl = input.page_url || null;
      if (!pageUrl && all.length) {
        const counts = groupBy(all.filter((e) => e.page_url), "page_url");
        let best = null; for (const [u, g] of counts) if (!best || g.length > best[1]) best = [u, g.length];
        if (best) { pageUrl = best[0]; ctx.warn(`No page_url given: analysed ${pageUrl}, the page with the most events.`); }
      }
      let ev = all;
      if (pageUrl) {
        const norm = String(pageUrl).replace(/\/+$/, "");
        let sub = all.filter((e) => String(e.page_url || "").replace(/\/+$/, "") === norm);
        if (!sub.length) { const rest = norm.split("://").pop(), i = rest.indexOf("/"), path = i >= 0 ? rest.slice(i + 1) : rest; sub = path ? all.filter((e) => String(e.page_url || "").includes(path)) : sub; }
        ev = sub;
      }
      ev = [...ev].sort((a, b) => cmp(a.session_id, b.session_id) || a.t - b.t);
      const aggregated = isAggregated(ev);
      if (aggregated) {
        ctx.warn("Aggregated behaviour export (e.g. Microsoft Clarity): no click coordinates or selectors, so click maps, CTA CTRs, scroll-reach curves and false-bottom detection are unavailable; reporting the export's dead/rage-click counts instead.");
        ctx.degraded("aggregated behaviour data: no click coordinates, selectors or per-session scroll");
      }
      if (ev.length) {
        const day = (e) => new Date(e.t).toISOString().slice(0, 10);
        if (aggregated && (input.start_date || input.end_date)) ctx.warn("Date filters ignored: the aggregated export covers the provider's own window (Clarity: last 1-3 days).");
        else {
          if (input.start_date) ev = ev.filter((e) => day(e) >= String(input.start_date).slice(0, 10));
          if (input.end_date) ev = ev.filter((e) => day(e) <= String(input.end_date).slice(0, 10));
        }
        if ((input.device || "all") !== "all") ev = ev.filter((e) => e.device === input.device);
      }
      if (!ev.length) { ctx.warn(`No behaviour events for ${pageUrl || "any page"} with the given filters.`); ctx.degraded("no behaviour events: metrics are empty"); }
      // 2. dom
      let html = input.html && String(input.html).trim() ? input.html : null;
      let htmlSource = html ? "html" : "none";
      if (!html && pageUrl) {
        const page = ctx.page({ url: pageUrl });
        if (page && page.status === 200 && page.html) { html = page.html; htmlSource = "fetched"; }
        else { ctx.warn(`Could not fetch ${pageUrl} (${page ? page.status : "the hosted console only serves the embedded demo site"}): selectors are not mapped to components.`); ctx.degraded("page DOM unavailable: component mapping skipped"); }
      }
      const doc = html ? CAF.lib.html.parse(html) : null;
      const selectors = [...new Set(ev.filter((e) => e.event_type === "click" && e.selector !== null && e.selector !== undefined).map((e) => String(e.selector)))].sort(cmp);
      const dom = describeSelectors(doc, selectors);
      const ctasDom = findCtas(doc);
      let layout = null;
      if (input.use_browser !== false && pageUrl && !aggregated && ev.length && html) {
        const desk = ev.filter((e) => e.device === "desktop" && isNum(e.viewport_w));
        const width = Math.trunc(median((desk.length ? desk : ev).map((e) => e.viewport_w))) || 1280;
        let frame = null;
        try {
          ctx.progress(`Measuring the page layout at ${width}px`);
          frame = await CAF.lib.html.renderInFrame(htmlSource === "html" ? sanitizeForFrame(html) : html, { width, height: 800 });
          layout = measureLayout(frame.win, frame.doc, [...new Set([...selectors, ...ctasDom.map((c) => c.selector)])]);
        } catch (e) {
          ctx.warn(`Layout measurement skipped (${(e && e.name) || "Error"}); positions are estimated from click coordinates.`);
          ctx.degraded("layout unavailable: component positions estimated from click coordinates");
          layout = null;
        } finally { if (frame) frame.remove(); }
        if (layout && htmlSource === "html") ctx.warn("Pasted HTML was laid out without its scripts and external stylesheets; measured positions may differ from the live page.");
      }
      // 3. metrics
      const minRage = Number(input.min_rage_sessions ?? 3);
      let m;
      if (!ev.length) m = { clicks: [], ctas: [], scroll: [], scroll_curves: {}, false_bottom: { detected: false }, rage: [], dead: [], attention: { median_dwell_s: null, mean_dwell_s: null, by_band: [] }, devices: [], n_sessions: 0, n_events: 0, positions: [] };
      else if (aggregated) {
        const agg = aggregateMetrics(ev);
        const by = Object.fromEntries(agg.metrics.map((x) => [x.metric, x]));
        const n = agg.sessions;
        const rage = by.rage_click ? [{ selector: "(page level - aggregated export)", component: "", episodes: by.rage_click.occurrences, sessions: by.rage_click.sessions, clicks: by.rage_click.occurrences, max_burst: 0 }] : [];
        const dead = by.dead_click ? [{ selector: "(page level - aggregated export)", component: "", tag: "", clicks: by.dead_click.occurrences, sessions: by.dead_click.sessions, share_of_clicks: null }] : [];
        const devices = Object.entries(agg.device_sessions).sort((a, b) => b[1] - a[1]).map(([d, ds]) => ({ device: d, sessions: ds, share: n ? ds / n : 0, median_max_scroll: agg.avg_scroll_depth[d] ?? null,
          median_dwell_s: null, clicks_per_session: null, dead_click_share: null, rage_sessions: ((by.rage_click || {}).by_device || {})[d] ? by.rage_click.by_device[d].sessions : 0,
          dead_click_session_share: (((by.dead_click || {}).by_device || {})[d] || {}).session_share ?? null, rage_session_share: (((by.rage_click || {}).by_device || {})[d] || {}).session_share ?? null }));
        if (by.rage_click && by.rage_click.sessions >= minRage) ctx.check({ rule: "behaviour.rage_clicks", status: "fail", severity: "high", message: `Rage clicks in ${by.rage_click.sessions} of ${n} sessions (${by.rage_click.occurrences} occurrences, aggregated export)` });
        if (by.dead_click && by.dead_click.sessions >= minRage) ctx.check({ rule: "behaviour.dead_clicks", status: "warn", severity: "medium", message: `Dead clicks in ${by.dead_click.sessions} of ${n} sessions (${by.dead_click.occurrences} occurrences, aggregated export)` });
        ctx.check({ rule: "behaviour.raw_events", status: "info", severity: "medium", message: "Aggregated export: click map, CTA CTRs, scroll curve and false bottom need raw events (csv provider)." });
        const ts = ev.map((e) => e.t);
        m = { clicks: [], ctas: [], scroll: [], scroll_curves: {}, false_bottom: { detected: false }, rage, dead, attention: { median_dwell_s: null, mean_dwell_s: null, by_band: [] }, devices, n_sessions: n,
          n_events: ev.length, positions: [], aggregate: agg, period: { start: new Date(Math.min(...ts)).toISOString().slice(0, 10), end: new Date(Math.max(...ts)).toISOString().slice(0, 10) } };
      } else {
        ctx.progress(`Computing metrics from ${ev.length} events`);
        const sessMap = sessionsFrame(ev), sess = [...sessMap.values()];
        const clicks = ev.filter((e) => e.event_type === "click" && e.selector !== null && e.selector !== undefined).map((e) => ({ ...e, selector: String(e.selector) }));
        const pageH = Object.fromEntries([...groupBy(ev, "device")].map(([d, g]) => [String(d), median(g.map((e) => e.page_h))]));
        const rage = rageEpisodes(clicks, Number(input.rage_window_s ?? 2), Number(input.rage_min_clicks ?? 3));
        const clickRows = clickTable(clicks, sess.length, dom, rage, pageH);
        let layCta = null, positions = [];
        if (layout && layout.page_h) {
          const H = Number(layout.page_h);
          layCta = Object.fromEntries(Object.entries(layout.elements || {}).filter(([, v]) => (v.height || 0) > 0).map(([s, v]) => [s, { depth_pct: ((v.top + v.height / 2) / H) * 100, button_like: v.button_like }]));
          positions = (layout.sections || []).map((s) => ({ top_pct: (s.top / H) * 100, bottom_pct: (s.bottom / H) * 100, label: sectionLabel(s.cls || "", s.label) }));
        } else positions = clickRows.filter((r) => r.depth_pct !== null).map((r) => ({ top_pct: r.depth_pct, label: `${r.component || r.selector}` }));
        const ctas = ctaTable(ctasDom, clicks, sess, pageH, layCta, Number(input.ignored_cta_ctr ?? 0.015), Number(input.low_cta_ctr ?? 0.03));
        const maxScroll = sess.map((s) => s.max_scroll);
        const fb = detectFalseBottom(maxScroll, Number(input.false_bottom_min_drop_pp ?? 15));
        if (fb.detected) fb.component = nearestComponent(fb.depth_from, fb.depth_to, positions);
        const overall = reachCurve(maxScroll, 10);
        const byDev = Object.fromEntries([...groupBy(sess, "device")].map(([d, g]) => [String(d), reachCurve(g.map((s) => s.max_scroll), 10)]));
        const scroll = overall.map((p, i) => ({ depth: p.depth, reach: p.reach, reach_by_device: Object.fromEntries(Object.entries(byDev).map(([d, c]) => [d, c[i].reach])) }));
        const rageRows = [...groupBy(rage, "selector")].map(([sel, g]) => ({ selector: sel, component: (dom[sel] || {}).component || "", episodes: g.length, sessions: new Set(g.map((x) => x.session_id)).size,
          clicks: g.reduce((s, x) => s + x.clicks, 0), max_burst: Math.max(...g.map((x) => x.clicks)) })).sort((a, b) => b.sessions - a.sessions);
        const deadRows = clickRows.filter((r) => r.control === "none").map((r) => ({ selector: r.selector, component: r.component, tag: r.tag, clicks: r.clicks, sessions: r.sessions, share_of_clicks: r.click_share })).sort((a, b) => b.sessions - a.sessions);
        const att = { median_dwell_s: num(median(sess.map((s) => s.dwell_s)), 1), mean_dwell_s: num(mean(sess.map((s) => s.dwell_s)), 1), by_band: attentionByBand(ev) };
        const curves = Object.fromEntries(Object.entries(byDev).map(([d, c]) => [`${capitalize(d)} (${sess.filter((s) => s.device === d).length} sessions)`, c]));
        m = { clicks: clickRows, ctas, scroll, scroll_curves: curves, false_bottom: fb, rage: rageRows, dead: deadRows, attention: att, devices: deviceSplit(sess, clicks, rage),
          n_sessions: sess.length, n_events: ev.length, positions,
          period: { start: new Date(Math.min(...sess.map((s) => s.start))).toISOString().slice(0, 10), end: new Date(Math.max(...sess.map((s) => s.end))).toISOString().slice(0, 10) } };
        for (const r of rageRows) if (r.sessions >= minRage) ctx.check({ rule: "behaviour.rage_clicks", status: "fail", severity: "high", target: r.selector, message: `Rage clicks on ${r.selector}: ${r.episodes} bursts in ${r.sessions} sessions` });
        for (const r of deadRows.slice(0, 5)) if (r.sessions >= 10) ctx.check({ rule: "behaviour.dead_clicks", status: "warn", severity: "medium", target: r.selector, message: `Dead clicks on ${r.selector}: ${r.clicks} clicks from ${r.sessions} sessions` });
        if (fb.detected) ctx.check({ rule: "behaviour.false_bottom", status: "warn", severity: "high", message: `False bottom at ${fb.depth_from}-${fb.depth_to}% (-${pyRepr(fb.drop_pp)} pp)${fb.component ? " near " + fb.component : ""}` });
        for (const c of ctas) if (c.status === "ignored") ctx.check({ rule: "behaviour.ignored_cta", status: "warn", severity: "medium", target: c.selector, message: `CTA ${c.selector} clicked by ${pyFixed(c.ctr * 100, 1)}% of sessions` });
      }
      // 4. insights (model interprets the computed metrics; selectors are guarded)
      const [ruleIns, ruleHyp] = m.aggregate ? aggregateInsights(m.aggregate, minRage) : ruleHypotheses(m.clicks, m.ctas, m.false_bottom, m.devices, minRage);
      let insights = [], hyps = [], summary = "No behaviour data for this page.", source = "rules";
      if (m.n_sessions && m.aggregate) {
        const agg = m.aggregate;
        const lines = agg.metrics.map((x) => `${x.label} (page level): ${x.occurrences} occurrences in ${x.sessions} of ${agg.sessions} sessions (${pyFixed((x.session_share || 0) * 100, 1)}%)`);
        const scrollTxt = Object.entries(agg.avg_scroll_depth).filter(([, v]) => v !== null).map(([d, v]) => `${d}: average scroll depth ${pyRepr(v)}%`).join(", ") || "not exported";
        const narrative = await ctx.generate("insights", { page_url: pageUrl || "the page", sessions: agg.sessions, period: m.period, clicks: lines, ctas: [], false_bottom: "Scroll-reach curve and false-bottom detection unavailable (aggregated export).",
          scroll: scrollTxt, devices: m.devices.map((d) => `${d.device}: ${d.sessions} sessions (${pyFixed(d.share * 100, 1)}%)`), attention: "not exported", median_dwell: "n/a", rule_insights: ruleIns.map((i) => i.title) });
        summary = `${agg.sessions} sessions (aggregated export): ` + agg.metrics.slice(0, 3).map((x) => `${x.label.toLowerCase()} in ${pyFixed((x.session_share || 0) * 100, 1)}% of sessions`).join("; ") + (Object.keys(agg.avg_scroll_depth).length ? `; average scroll depth ${scrollTxt}` : "") + ".";
        if (ctx.llmLive) {
          insights = (narrative.insights || []).filter((i) => String(i.title || "").trim()).map((i) => ({ title: i.title, detail: i.detail || "", severity: i.severity || "medium", selector: null }));
          if (!insights.length) insights = ruleIns;
          hyps = (narrative.hypotheses || []).filter((h) => String(h.hypothesis || "").trim()).map((h) => ({ hypothesis: h.hypothesis, evidence: h.evidence, metric: h.metric, selector: null, source: "model" }));
          if (!hyps.length) hyps = ruleHyp.map((h) => ({ ...h, source: "rules" }));
          summary = String(narrative.summary || "").trim() || summary; source = "model";
        } else { insights = ruleIns; hyps = ruleHyp.map((h) => ({ ...h, source: "rules" })); }
      } else if (m.n_sessions) {
        const clickLines = m.clicks.slice(0, 15).map((c) => `${c.selector} [${c.component || "unmapped"}; ${c.tag}; control=${c.control}]: ${c.clicks} clicks, ${c.sessions} sessions (CTR ${fmtPct1(c.ctr)}), rage bursts ${c.rage_episodes}, dead clicks ${c.dead_clicks}, depth ${pyRepr(c.depth_pct)}%`);
        const ctaLines = m.ctas.map((c) => `${c.selector} '${c.label}' [${c.component}]: CTR ${fmtPct1(c.ctr)}` + (c.ctr_exposed !== null ? `, ${fmtPct1(c.ctr_exposed)} of ${c.exposed_sessions} sessions that scrolled to it` : "") +
          `, depth ${pyRepr(c.depth_pct)}%, status ${c.status}` + (c.button_like === true ? ", rendered as a button" : c.button_like === false ? ", rendered as a plain text link" : ""));
        const fb = m.false_bottom;
        const fbLine = fb.detected ? `False bottom between ${fb.depth_from}% and ${fb.depth_to}% depth: reach ${fmtPct1(fb.reach_before)} -> ${fmtPct1(fb.reach_after)} (-${pyRepr(fb.drop_pp)} pp vs a typical ${pyRepr(fb.typical_drop_pp)} pp step), nearest component: ${fb.component || "unknown"}` : "No false bottom detected.";
        const deviceLines = m.devices.map((d) => `${d.device}: ${d.sessions} sessions (${fmtPct1(d.share)}), median reach ${d.median_max_scroll === null ? "n/a" : pyFixed(d.median_max_scroll, 0)}%, median dwell ${pyRepr(d.median_dwell_s)}s, ${pyRepr(d.clicks_per_session)} clicks/session, dead-click share ${fmtPct1(d.dead_click_share || 0)}, rage sessions ${d.rage_sessions}`);
        ctx.progress("Writing insights and test hypotheses from the metrics");
        const narrative = await ctx.generate("insights", { page_url: pageUrl || "the page", sessions: m.n_sessions, period: m.period, clicks: clickLines, ctas: ctaLines, false_bottom: fbLine,
          scroll: m.scroll.map((p) => `${p.depth}%: ${pyFixed(p.reach * 100, 0)}%`).join(", "), devices: deviceLines, attention: m.attention.by_band.map((b) => `${b.band}: ${pyRepr(b.seconds)}s`).join(", "),
          median_dwell: pyRepr(m.attention.median_dwell_s), rule_insights: ruleIns.map((i) => i.title) });
        const known = new Set([...m.clicks.map((c) => c.selector), ...m.ctas.map((c) => c.selector)]);
        if (ctx.llmLive) {
          insights = (narrative.insights || []).filter((i) => String(i.title || "").trim()).map((i) => ({ title: i.title, detail: i.detail || "", severity: i.severity || "medium", selector: known.has(i.selector) ? i.selector : null }));
          hyps = (narrative.hypotheses || []).filter((h) => String(h.hypothesis || "").trim()).map((h) => ({ hypothesis: h.hypothesis, evidence: h.evidence, metric: h.metric, selector: known.has(h.selector) ? h.selector : null, source: "model" }));
          const covered = new Set(hyps.map((h) => h.selector).filter(Boolean));
          hyps.push(...ruleHyp.filter((h) => h.selector && !covered.has(h.selector)).map((h) => ({ ...h, source: "rules" })));
          summary = String(narrative.summary || "").trim() || summaryLine(m); source = "model";
          if (!insights.length) insights = ruleIns;
        } else { insights = ruleIns; hyps = ruleHyp.map((h) => ({ ...h, source: "rules" })); summary = summaryLine(m); }
      }
      // 5. report
      const layoutSource = layout ? "browser" : m.positions.length ? "clicks" : "none";
      let clickSvg = "", scrollSvg = "", scrollSvgThemed = "";
      if (m.n_sessions && !m.aggregate) {
        try {
          const devCounts = [...groupBy(ev, "device")].map(([d, g]) => [d, new Set(g.map((e) => e.session_id)).size]);
          const mainDev = devCounts.reduce((b, x) => (x[1] > b[1] ? x : b))[0];
          const cl = ev.filter((e) => e.event_type === "click" && e.device === mainDev && isNum(e.x) && isNum(e.y));
          const width = median(ev.filter((e) => e.device === mainDev).map((e) => e.viewport_w)), height = median(ev.filter((e) => e.device === mainDev).map((e) => e.page_h));
          const rageSel = new Set(m.rage.filter((r) => r.sessions >= minRage).map((r) => r.selector));
          const marks = [];
          for (const c of m.clicks) {
            const g = cl.filter((e) => e.selector === c.selector);
            if (!g.length) continue;
            if (rageSel.has(c.selector)) marks.push({ x: median(g.map((e) => e.x)), y: median(g.map((e) => e.y)), color: CRITICAL, label: `Rage: ${c.element_text || c.selector}` });
            else if (c.control === "none" && c.sessions >= 10) marks.push({ x: median(g.map((e) => e.x)), y: median(g.map((e) => e.y)), color: SERIOUS, label: `Dead: ${c.selector}` });
          }
          clickSvg = clickMapSvg(cl, width, height, marks, layoutSource === "browser" ? m.positions : [], `Click density, ${mainDev} (${cl.length} clicks)`);
          scrollSvg = scrollCurveSvg(m.scroll_curves, m.false_bottom, false);
          scrollSvgThemed = scrollCurveSvg(m.scroll_curves, m.false_bottom, true);
          ctx.artifact({ name: "click_map.svg", mime: "image/svg+xml", content: clickSvg, description: `Click map in page coordinates (${mainDev})` });
          ctx.artifact({ name: "scroll_curve.svg", mime: "image/svg+xml", content: scrollSvg, description: "Share of sessions reaching each scroll depth" });
        } catch (e) { ctx.log(`heatmap charts failed: ${e.message}`); }
      }
      const paired = (idx, ins) => {
        if (ins.selector) return hyps.find((h) => h.selector === ins.selector) || {};
        if (m.aggregate && idx < hyps.length && !hyps[idx].selector) return hyps[idx];
        return {};
      };
      const findings = insights.map((i, k) => ({ title: i.title, detail: i.detail || "", severity: ["low", "medium", "high", "critical"].includes(i.severity) ? i.severity : "medium", category: "behaviour",
        evidence: paired(k, i).evidence || "", recommendation: paired(k, i).hypothesis || "", target: i.selector || pageUrl, source_agent: AGENT }));
      const pick = (o, keys) => Object.fromEntries(keys.map((k) => [k, o[k] === undefined ? null : o[k]]));
      const output = {
        page_url: pageUrl, sessions: m.n_sessions, events: m.n_events, period: m.period || { start: null, end: null }, device_filter: input.device || "all", dom_source: htmlSource, layout_source: layoutSource,
        clicks: m.clicks.map((c) => pick(c, ["selector", "component", "element_text", "tag", "control", "clicks", "sessions", "click_share", "ctr", "dead_clicks", "rage_episodes", "rage_sessions", "median_x", "median_y", "depth_pct"])),
        ctas: m.ctas.map((c) => pick(c, ["selector", "label", "component", "clicks", "sessions", "ctr", "exposed_sessions", "ctr_exposed", "depth_pct", "button_like", "status"])),
        scroll: m.scroll, false_bottom: { component: "", ...pick(m.false_bottom, Object.keys(m.false_bottom).filter((k) => ["detected", "depth_from", "depth_to", "drop_pp", "typical_drop_pp", "reach_before", "reach_after", "component"].includes(k))) },
        rage_clicks: m.rage, dead_clicks: m.dead, attention: m.attention, devices: m.devices, summary, insights, hypotheses: hyps, insight_source: source,
        data_mode: m.aggregate ? "aggregated" : "events",
        aggregate: m.aggregate ? { source: "csv", note: "Aggregated export: click maps, selector-level clicks, CTA CTRs, scroll-reach curves and false-bottom detection need a raw event export.", sessions: m.aggregate.sessions, metrics: m.aggregate.metrics, avg_scroll_depth: m.aggregate.avg_scroll_depth } : null,
        findings,
      };
      ctx.artifact({ name: "heatmap_metrics.json", mime: "application/json", content: JSON.stringify(output, null, 1), description: "All computed behavioural metrics" });
      const view = [
        { type: "kv", title: "Behaviour", data: { page: pageUrl || "n/a", sessions: m.n_sessions, events: m.n_events, period: m.period ? `${m.period.start} to ${m.period.end}` : "n/a", layout: layoutSource, data_mode: output.data_mode, insights_by: source } },
        { type: "markdown", title: "Summary", markdown: summary },
      ];
      if (clickSvg) view.push({ type: "svg", title: "Click map", svg: clickSvg });
      if (scrollSvgThemed) view.push({ type: "svg", title: "Scroll reach", svg: scrollSvgThemed });
      view.push(
        { type: "table", title: "Insights", rows: insights.map((i) => ({ severity: i.severity, insight: i.title, detail: i.detail, selector: i.selector || "" })) },
        { type: "table", title: "Test hypotheses (for experiment ideation)", rows: hyps.map((h) => ({ hypothesis: h.hypothesis, evidence: h.evidence, metric: h.metric, source: h.source })) },
        { type: "table", title: "Clicks by selector", rows: output.clicks.map((c) => ({ selector: c.selector, component: c.component, control: c.control, clicks: c.clicks, sessions: c.sessions, rage_sessions: c.rage_sessions, depth_pct: c.depth_pct })) },
        { type: "table", title: "Calls to action", rows: output.ctas.map((c) => ({ cta: c.selector, label: c.label, ctr: fmtPct1(c.ctr), ctr_exposed: c.ctr_exposed === null ? "" : fmtPct1(c.ctr_exposed), depth_pct: c.depth_pct, status: c.status })) },
        { type: "table", title: "Devices", rows: m.devices.map((d) => ({ device: d.device, sessions: d.sessions, median_max_scroll: d.median_max_scroll, median_dwell_s: d.median_dwell_s, clicks_per_session: d.clicks_per_session, rage_sessions: d.rage_sessions })) });
      return { output, summary: `Heatmap: ${summary} ${hyps.length} test hypothesis(es) for experiment ideation.`, view };
    },
  });
})();
