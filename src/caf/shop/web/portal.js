/* The portal: one application over the orchestration layer's API.
   Overview, Launch (goal -> plan preview -> live run), Runs (every run, step by step), Approvals, Agents, Flows, Health,
   Customers (operator) or Account (customer). Views are hash routes; the shell (top bar, spine, the "now" band) stays. */
(function () {
  const { h, $, $$, clear, toast, when, catColor, copy, BASE, theme, setTheme, themeIcon } = Shop;
  const ADMIN_KEY = "cxnova.shop.admin";
  const readKey = (k) => { try { return localStorage.getItem(k) || ""; } catch (e) { return ""; } };
  const tok = () => readKey(ADMIN_KEY) || Shop.token();
  const api = (path, opts) => Shop.api(path, Object.assign({ token: tok() }, opts || {}));
  const REDUCED = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // ------------------------------------------------------------------------------------------------ small helpers
  function put(el, ...kids) { for (const c of kids.flat(Infinity)) { if (c === null || c === undefined || c === false) continue; el.append(c.nodeType ? c : document.createTextNode(String(c))); } return el; }
  const LABEL = { awaiting_approval: "waiting for approval", completed_with_errors: "completed with errors", needs_approval: "action held",
                  queued: "queued", running: "running", stalled: "stalled", completed: "completed", failed: "failed", pending: "waiting",
                  ok: "ok", degraded: "degraded", error: "error", skipped: "skipped", planned: "planned" };
  const label = (s) => LABEL[s] || s || "";
  const SUCCESS = new Set(["ok", "degraded", "needs_approval"]);
  function runClass(s) { return s === "completed" ? "good" : (s === "completed_with_errors" || s === "awaiting_approval") ? "warn" : (s === "failed" || s === "stalled") ? "bad" : (s === "running" || s === "queued") ? "live" : ""; }
  function lampOf(s) { return s === "ok" || s === "completed" ? "good" : ["degraded", "needs_approval", "awaiting_approval", "completed_with_errors"].includes(s) ? "warn" : ["error", "failed", "stalled"].includes(s) ? "bad" : s === "running" || s === "queued" ? "run" : s === "skipped" ? "off" : ""; }
  function dur(ms) { if (ms === null || ms === undefined || isNaN(ms)) return ""; if (ms < 1000) return Math.round(ms) + " ms"; const s = ms / 1000; if (s < 60) return s.toFixed(1) + " s"; const m = Math.floor(s / 60); return m + " min " + String(Math.round(s % 60)).padStart(2, "0") + " s"; }
  function ago(iso) { const t = Date.parse(iso); if (isNaN(t)) return ""; const s = (Date.now() - t) / 1000; if (s < 45) return "just now"; if (s < 3600) return Math.round(s / 60) + " min ago"; if (s < 86400) return Math.round(s / 3600) + " h ago"; return when(iso); }
  const ts = (iso) => { const t = Date.parse(iso); return isNaN(t) ? null : t; };
  const num = (n) => Number(n || 0).toLocaleString();
  const currency = () => (D.stats && D.stats.currency) || "USD";
  function money(x, plus) {  // a cost; `plus` marks a figure that leaves some calls unpriced
    if (x === null || x === undefined || isNaN(x)) return "—";
    const sym = currency() === "USD" ? "$" : currency() + " ";
    const t = x === 0 ? sym + "0" : x < 0.001 ? "<" + sym + "0.001" : x < 1 ? sym + x.toFixed(3) : x < 100 ? sym + x.toFixed(2) : sym + Math.round(x).toLocaleString();
    return plus ? t + "+" : t;
  }
  const stepCost = (s) => { const m = (s.result || {}).metrics || {}; return { cost: Number(m.cost_usd || 0), unpriced: Number(m.unpriced_calls || 0) }; };
  function since(iso) { return h("span", { dataset: { since: iso } }, dur(Date.now() - ts(iso))); }
  function agent(id) { return D.byId[id] || { id, name: (id || "").replace(/_/g, " "), category: "", category_label: "" }; }
  function packTag(a) { const p = D.packs.find((x) => x.id === a.pack); return p ? (p.tag || p.name.split(/\s+/).map((w) => w[0]).join("").toUpperCase().slice(0, 4)) : ""; }
  function dayKey(iso) { const d = new Date(iso); return isNaN(d) ? "" : d.toISOString().slice(0, 10); }
  function dayName(key) { const today = new Date().toISOString().slice(0, 10); const y = new Date(Date.now() - 86400000).toISOString().slice(0, 10); if (key === today) return "Today"; if (key === y) return "Yesterday"; const d = new Date(key + "T12:00:00"); return d.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" }); }
  function md(text) {
    const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    const inline = (s) => esc(s).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>").replace(/`([^`]+)`/g, "<code>$1</code>");
    let html = "", list = false;
    for (const line of String(text || "").split("\n")) {
      const b = /^\s*[-*]\s+(.*)$/.exec(line), hd = /^(#{1,4})\s+(.*)$/.exec(line);
      if (b) { if (!list) { html += "<ul>"; list = true; } html += "<li>" + inline(b[1]) + "</li>"; continue; }
      if (list) { html += "</ul>"; list = false; }
      if (hd) html += "<h4>" + inline(hd[2]) + "</h4>"; else if (line.trim()) html += "<p>" + inline(line) + "</p>";
    }
    if (list) html += "</ul>";
    return h("div", { class: "md small", html });
  }
  const ICON = {
    overview: "<path d='M3 3h7v7H3zM14 3h7v4h-7zM14 10h7v11h-7zM3 13h7v8H3z'/>",
    launch: "<path d='M5 19l3-3M14 4l6 6-8 8-6-6zM4 20l1-5 4 4z'/><circle cx='15' cy='9' r='1.2'/>",
    runs: "<circle cx='12' cy='12' r='9'/><path d='M12 7v5l3 2'/>",
    approvals: "<path d='M20 6L9 17l-5-5'/>",
    agents: "<rect x='4' y='4' width='16' height='16' rx='3'/><path d='M9 9h6v6H9zM2 10h2M2 14h2M20 10h2M20 14h2M10 2v2M14 2v2M10 20v2M14 20v2'/>",
    flows: "<circle cx='6' cy='6' r='2.5'/><circle cx='18' cy='12' r='2.5'/><circle cx='6' cy='18' r='2.5'/><path d='M8.5 6h4a3 3 0 0 1 3 3v3M8.5 18h4a3 3 0 0 0 3-3v-3'/>",
    health: "<path d='M3 12h4l2-5 4 10 2-5h6'/>",
    customers: "<circle cx='9' cy='8' r='3.5'/><path d='M2 20a7 7 0 0 1 14 0M16 4a3.5 3.5 0 0 1 0 7M22 20a7 7 0 0 0-5-6.7'/>",
    account: "<circle cx='12' cy='8' r='4'/><path d='M4 21a8 8 0 0 1 16 0'/>",
    ext: "<path d='M14 4h6v6M20 4l-9 9M19 14v6H5V6h6'/>",
    search: "<circle cx='11' cy='11' r='7'/><path d='M20 20l-4-4'/>",
    arrow: "<path d='M5 12h14M13 6l6 6-6 6'/>",
  };
  const icon = (name) => h("span", { html: `<svg viewBox="0 0 24 24" aria-hidden="true">${ICON[name] || ""}</svg>` }).firstChild;

  // ------------------------------------------------------------------------------------------------ a JSON viewer (children render when opened)
  function jt(value, key, open) {
    const depth = open === true ? 1 : Number(open) || 0;
    const k = key === undefined ? null : h("span", { class: "k" }, key + ": ");
    if (value === null || value === undefined) return h("span", { class: "row" }, k, h("span", { class: "z" }, "null"));
    if (typeof value !== "object") {
      const cls = typeof value === "string" ? "s" : typeof value === "number" ? "n" : "b";
      const text = typeof value === "string" ? JSON.stringify(value) : String(value);
      if (typeof value === "string" && text.length > 600) {
        const span = h("span", { class: cls }, text.slice(0, 600) + "…\"");
        return h("span", { class: "row" }, k, span, " ", h("button", { class: "btn sm ghost", type: "button", onclick: (e) => { span.textContent = text; e.target.remove(); } }, "show all"));
      }
      return h("span", { class: "row" }, k, h("span", { class: cls }, text));
    }
    const arr = Array.isArray(value), n = arr ? value.length : Object.keys(value).length;
    const d = h("details", null, h("summary", null, k, h("span", { class: "c" }, arr ? "[" + n + " item" + (n === 1 ? "" : "s") + "]" : "{" + n + " field" + (n === 1 ? "" : "s") + "}")));
    let done = false;
    const fill = () => { if (done) return; done = true; for (const [ck, cv] of arr ? value.map((v, i) => [i, v]) : Object.entries(value)) d.append(jt(cv, ck, depth - 1)); };
    d.addEventListener("toggle", () => { if (d.open) fill(); });
    if (depth > 0 && n) { d.open = true; fill(); }
    return d;
  }
  function valueView(v) {
    if (v === undefined) return h("span", { class: "muted" }, "—");
    if (v !== null && typeof v === "object") return h("div", { class: "jt" }, jt(v, undefined, false));
    const s = String(v);
    if (s.length <= 240) return h("div", { class: "val" }, s);
    const el = h("div", { class: "val" }, s.slice(0, 240) + "…");
    return h("div", null, el, h("button", { class: "btn sm ghost", type: "button", onclick: (e) => { el.textContent = s; e.target.remove(); } }, "show all"));
  }

  // ------------------------------------------------------------------------------------------------ data
  const D = { agents: [], byId: {}, packs: [], flows: [], runs: [], approvals: [], stats: null, host: null, me: null, operator: false, live: {}, ready: false };
  let liveTimer = null;
  async function loadCatalog() {
    const [agents, packs, flows] = await Promise.all([api("/agents"), api("/packs").catch(() => []), api("/flows").catch(() => [])]);
    D.agents = agents; D.byId = {}; agents.forEach((a) => { D.byId[a.id] = a; }); D.packs = packs; D.flows = flows;
    api("/console/host").then((x) => { D.host = x; renderTop(); }).catch(() => {});
    const key = tok();
    if (key.startsWith("cxn_")) { D.me = await api("/shop/me").catch(() => null); D.operator = false; }
    else { D.operator = await api("/shop/admin/api/overview").then(() => true).catch(() => false); }
  }
  async function refreshLive() {
    try {
      const [runs, approvals, stats] = await Promise.all([api("/runs?limit=300"), api("/approvals"), api("/runs/stats?days=14")]);
      D.runs = runs; D.approvals = approvals; D.stats = stats;
      const live = runs.filter((r) => r.status === "running" || r.status === "queued").slice(0, 4);
      const views = await Promise.all(live.map((r) => api("/runs/" + encodeURIComponent(r.run_id)).catch(() => null)));
      D.live = {}; live.forEach((r, i) => { if (views[i]) D.live[r.run_id] = views[i]; });
      renderNow(); renderSpine();
      if (S.view && S.view.onLive) S.view.onLive();
    } catch (e) { if (e.status === 401 || e.status === 403) gate(e.message); }
  }
  function startLive() { clearInterval(liveTimer); liveTimer = setInterval(() => { if (!document.hidden) refreshLive(); }, 5000); }

  // ------------------------------------------------------------------------------------------------ shell
  const S = { view: null, name: "", params: [] };
  const NAV = [["", "Overview", "overview"], ["launch", "Launch", "launch"], ["runs", "Runs", "runs"], ["approvals", "Approvals", "approvals"],
               ["agents", "Agents", "agents"], ["flows", "Flows", "flows"], ["health", "Health", "health"]];
  function renderTop() {
    const t = clear($("#top"));
    const product = document.body.dataset.product || "CX Nova Agent Orchestration Layer";
    const host = D.host;
    const MARK = "<svg viewBox='0 0 32 32' width='30' height='30' aria-hidden='true'><rect width='32' height='32' rx='7' fill='#1F3D7A'/><circle cx='9' cy='11' r='3' fill='#fff'/><path d='M9 11 L21 11 Q24 11 24 14 L24 22' stroke='#fff' stroke-width='2.4' fill='none' stroke-linecap='round'/><path d='M6 24 H26' stroke='#fff' stroke-width='2.4' stroke-linecap='round'/></svg>";
    put(t, h("a", { class: "brand", href: "#/" }, h("span", { class: "mark", html: MARK }), h("div", null, h("div", { class: "name" }, product.replace(" Agent Orchestration Layer", "")), h("div", { class: "by" }, "Agent orchestration"))),
      h("button", { class: "search", type: "button", onclick: openPalette, "aria-label": "Search, or type a goal" }, icon("search"), h("span", { class: "ph" }, "Search agents, flows and runs, or type a goal"), h("kbd", null, navigator.platform && /Mac/.test(navigator.platform) ? "⌘K" : "Ctrl K")),
      h("div", { class: "who" },
        host ? h("span", { class: "chip prov " + (host.live ? "good" : ""), title: host.live ? "Model calls go to " + host.label : "No model configured: agents answer with placeholders" }, h("span", { class: "lamp " + (host.live ? "good" : "off") }), host.live ? host.label : "Offline model") : null,
        h("button", { class: "btn sm ghost", type: "button", title: "Switch between the midnight and paper themes", "aria-label": "Switch theme", onclick: () => { setTheme(theme() === "dark" ? "light" : "dark"); renderTop(); } }, themeIcon()),
        h("span", { class: "chip" }, h("span", { class: "name" }, D.me ? D.me.customer.name : D.operator ? "Operator" : "Local"), tok() ? h("button", { class: "btn sm ghost", type: "button", style: "padding:0 4px", title: "Sign out", onclick: signOut }, "×") : null)));
  }
  function renderSpine() {
    const s = clear($("#spine"));
    const live = D.runs.filter((r) => r.status === "running" || r.status === "queued").length;
    const held = D.approvals.length;
    const items = NAV.concat(D.operator ? [["customers", "Customers", "customers"]] : D.me ? [["account", "My account", "account"]] : []);
    put(s, items.map(([id, text, ic]) => h("a", { href: "#/" + id, "aria-current": S.name === id ? "page" : null }, icon(ic), h("span", { class: "lbl" }, text),
      id === "runs" && live ? h("span", { class: "badge live", title: live + " running" }, live) : id === "approvals" && held ? h("span", { class: "badge", title: held + " waiting for a decision" }, held) : null)),
      h("span", { class: "gap" }), h("div", { class: "sub" }, "Also"),
      h("a", { class: "ext", href: BASE + "/shop/designer", target: "_blank", rel: "noopener" }, h("span", { class: "lbl" }, "Flow designer"), icon("ext")),
      h("a", { class: "ext", href: BASE + "/console", target: "_blank", rel: "noopener" }, h("span", { class: "lbl" }, "Agent console"), icon("ext")),
      h("a", { class: "ext", href: BASE + "/shop", target: "_blank", rel: "noopener" }, h("span", { class: "lbl" }, "Storefront"), icon("ext")),
      h("a", { class: "ext", href: BASE + "/docs", target: "_blank", rel: "noopener" }, h("span", { class: "lbl" }, "API reference"), icon("ext")));
  }
  function miniDots(view) {
    return h("span", { class: "mini" }, (view.steps || []).map((s) => h("i", { class: lampOf(s.status), title: s.id + ": " + label(s.status) })));
  }
  function renderNow() {
    const n = clear($("#now"));
    const live = D.runs.filter((r) => r.status === "running" || r.status === "queued");
    const held = D.approvals;
    const today = D.stats && D.stats.today;
    if (live.length) {
      put(n, live.slice(0, 3).map((r) => { const v = D.live[r.run_id]; return h("a", { class: "item", href: "#/runs/" + encodeURIComponent(r.run_id), title: r.goal },
        h("span", { class: "lamp run" }), h("span", { class: "g" }, r.goal || r.run_id), v ? miniDots(v) : null, v ? h("span", { class: "muted" }, v.steps.filter((s) => s.result).length + "/" + v.steps.length) : null); }),
        live.length > 3 ? h("a", { class: "item muted", href: "#/runs" }, "+" + (live.length - 3) + " more running") : null);
    } else {
      const last = D.runs[0];
      put(n, h("span", { class: "item quiet" }, h("span", { class: "lamp off" }), last ? "Nothing running now. Last run " + ago(last.created_at) + "." : "Nothing has run yet."));
    }
    put(n, h("span", { class: "sep" }),
      held.length ? h("a", { class: "item", href: "#/approvals", style: "color:var(--kin)" }, h("span", { class: "lamp warn" }), held.length + (held.length === 1 ? " action waits for a decision" : " actions wait for a decision")) : h("span", { class: "item quiet" }, "Nothing waits for approval"),
      today ? [h("span", { class: "sep" }), h("a", { class: "item quiet", href: "#/runs" }, today.runs + (today.runs === 1 ? " run today" : " runs today") + (today.failed ? ", " + today.failed + " failed" : ""))] : null);
  }
  function signOut() { try { localStorage.removeItem(ADMIN_KEY); } catch (e) {} Shop.setToken(""); location.reload(); }
  function gate(message) {
    clearInterval(liveTimer);
    const c = clear($("#content"));
    put(c, h("section", { class: "panel gate stack" },
      h("h1", { style: "font-size:20px" }, "Sign in to the portal"),
      h("p", { class: "muted small" }, "Paste the operator token to see everything, or a customer key to see that customer's agents, flows and runs. It stays in this browser only."),
      message ? h("div", { class: "notice warn" }, message) : null,
      h("form", { class: "stack", onsubmit: (e) => { e.preventDefault(); const v = $("#key").value.trim(); if (!v) return; if (v.startsWith("cxn_")) Shop.setToken(v); else { try { localStorage.setItem(ADMIN_KEY, v); } catch (err) {} } location.reload(); } },
        h("label", { class: "f" }, "Token or key", h("input", { type: "password", id: "key", autocomplete: "off", required: true })),
        h("div", { class: "row" }, h("button", { class: "btn primary", type: "submit" }, "Open the portal")))));
  }

  // ------------------------------------------------------------------------------------------------ the graph
  const GW = 224, GH = 62, GCOL = 256, GROW = 80, GPAD = 16, GWRAP = 34, GBAND = 30;
  let graphSeq = 0;
  function levelsOf(steps) { const lv = {}; for (const s of steps) lv[s.id] = (s.needs || []).reduce((m, d) => Math.max(m, (lv[d] ?? -1) + 1), 0); return lv; }
  function graph(steps, opts) {
    opts = opts || {};
    const g = h("div", { class: "graph" + (opts.planned ? " planned" : "") });
    const nLevels = Math.max(1, ...Object.values(levelsOf(steps)).map((x) => x + 1));
    const fit = (w) => { let n = Math.max(1, Math.floor((w - 2 * GPAD - GW) / GCOL) + 1); if (nLevels > n) n = Math.max(1, Math.floor((w - 2 * GWRAP - GW) / GCOL) + 1); return n; };
    let per = opts.width ? fit(opts.width) : 4, inner = build(per);
    g.append(inner);
    requestAnimationFrame(() => { const w = g.clientWidth; if (!w) return; const n = fit(w); if (n !== per) { per = n; const built = build(per); g.replaceChild(built, inner); inner = built; } });
    return g;

    // levels left to right; when there are more levels than fit, the chain wraps onto the next band like text
    function build(perRow) {
      const lv = levelsOf(steps), rowsIn = {}, at = {}, byId = {}, bandOf = {};
      const levels = Math.max(1, ...Object.values(lv).map((x) => x + 1)), padX = levels > perRow ? GWRAP : GPAD;
      for (const s of steps) { const l = lv[s.id]; rowsIn[l] = (rowsIn[l] || 0) + 1; }
      const bandH = [], bandTop = [];
      for (let l = 0; l < levels; l++) { const b = Math.floor(l / perRow); bandH[b] = Math.max(bandH[b] || 0, (rowsIn[l] || 1) * GROW); }
      let y = GPAD; bandH.forEach((hh, b) => { bandTop[b] = y; y += hh + GBAND; });
      const used = {};
      for (const s of steps) { byId[s.id] = s; const l = lv[s.id], b = Math.floor(l / perRow); bandOf[s.id] = b; used[l] = used[l] || 0; at[s.id] = { x: padX + (l % perRow) * GCOL, y: bandTop[b] + used[l] * GROW }; used[l]++; }
      const cols = Math.min(levels, perRow);
      const inner = h("div", { style: `position:relative;width:${padX * 2 + (cols - 1) * GCOL + GW}px;height:${y - GBAND - 18 + GPAD}px` });
      const NS = "http://www.w3.org/2000/svg", svg = document.createElementNS(NS, "svg");
      svg.setAttribute("width", "100%"); svg.setAttribute("height", "100%");
      const seq = ++graphSeq;
      let k = 0;
      for (const s of steps) for (const d of s.needs || []) {
        if (!at[d]) continue;
        const a = at[d], b = at[s.id], x1 = a.x + GW, y1 = a.y + GH / 2, x2 = b.x, y2 = b.y + GH / 2, mx = (x1 + x2) / 2;
        const path = document.createElementNS(NS, "path"), dep = byId[d];
        const id = `g${seq}e${k++}`;
        path.setAttribute("id", id);
        if (bandOf[d] === bandOf[s.id]) path.setAttribute("d", `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2 - 2},${y2}`);
        else {  // into the next band: out to the right, down through the gap, in from the left
          const yg = bandTop[bandOf[s.id]] - GBAND / 2 - 4, r = 10, xa = x1 + 22, xb = x2 - 22;
          path.setAttribute("d", `M${x1},${y1} L${xa - r},${y1} Q${xa},${y1} ${xa},${y1 + r} L${xa},${yg - r} Q${xa},${yg} ${xa - r},${yg} L${xb + r},${yg} Q${xb},${yg} ${xb},${yg + r} L${xb},${y2 - r} Q${xb},${y2} ${xb + r},${y2} L${x2 - 2},${y2}`);
        }
        path.setAttribute("fill", "none"); path.setAttribute("stroke-width", "1.5");
        const done = dep && dep.status && SUCCESS.has(dep.status);
        path.setAttribute("stroke", done ? "var(--matsu)" : "var(--rule-2)"); if (done) path.setAttribute("opacity", ".75");
        svg.append(path);
        if (opts.live && !REDUCED && done && (s.status === "running" || s.status === "pending")) {  // work flowing along a live edge
          const c = document.createElementNS(NS, "circle"); c.setAttribute("r", "3"); c.setAttribute("class", "particle");
          const m = document.createElementNS(NS, "animateMotion"); m.setAttribute("dur", (1.4 + (k % 3) * .3) + "s"); m.setAttribute("repeatCount", "indefinite");
          const mp = document.createElementNS(NS, "mpath"); mp.setAttribute("href", "#" + id); mp.setAttributeNS("http://www.w3.org/1999/xlink", "xlink:href", "#" + id);
          m.append(mp); c.append(m); svg.append(c);
        }
      }
      inner.append(svg);
      for (const s of steps) {
        const a = agent(s.agent), r = s.result, pos = at[s.id], status = s.status || (opts.planned ? "planned" : "pending");
        const t = (r || {}).timing || {}, ms = r && t.started_at && t.finished_at ? ts(t.finished_at) - ts(t.started_at) : (r && r.metrics ? r.metrics.duration_ms : null);
        let sub;
        if (opts.planned) sub = s.purpose || a.category_label || "";
        else if (status === "running") sub = h("span", null, s.id + ": running for ", since(s.started_at));
        else sub = s.id + ": " + (r ? label(r.status) + (ms ? ", " + dur(ms) : "") + (stepCost(s).cost ? ", " + money(stepCost(s).cost, stepCost(s).unpriced) : "") : label(status));
        inner.append(h("button", { type: "button", class: "gnode " + status + (opts.selected === s.id ? " sel" : "") + (s.entitled === false ? " not-entitled" : ""),
          style: `left:${pos.x}px;top:${pos.y}px;height:${GH}px`, onclick: () => opts.onSelect && opts.onSelect(s.id), title: (a.name || s.agent) + (s.purpose ? ": " + s.purpose : "") + (s.entitled === false ? " (not in this subscription: it would be skipped)" : "") },
          h("span", { class: "bar", style: "background:" + catColor(a.category) }),
          h("span", { class: "body" }, h("span", { class: "nm" }, h("span", { class: "t" }, a.name || s.agent), opts.planned ? (packTag(a) ? h("span", { class: "tag" }, packTag(a)) : null) : h("span", { class: "lamp " + lampOf(status), title: label(status) })),
            h("span", { class: "sub", style: "display:block" }, sub),
            s.activity && status === "running" ? h("span", { class: "act", style: "display:block" }, "Model: " + (s.activity.call || "") + " on " + (s.activity.model || "")) : null)));
      }
      return inner;
    }
  }
  function stagesOf(stages, { done } = {}) {
    const out = h("div", { class: "stages" });
    stages.forEach((st, i) => { if (i) out.append(h("span", { class: "arrow" }, "›")); out.append(h("span", { class: "stage" }, st.map((id) => h("span", { class: "step" }, agent(id).name || id)))); });
    return out;
  }

  // ------------------------------------------------------------------------------------------------ router
  const VIEWS = {};
  function navigate(hash) { location.hash = hash; }
  function route() {
    let hash = location.hash.replace(/^#/, "");
    if (!hash && /\/shop\/runs$/.test(location.pathname)) hash = "/runs";   // the earlier run viewer's address
    const legacy = /^run=([^&]+)(?:&step=([^&]+))?/.exec(hash);   // the first run viewer's links
    if (legacy) { hash = "/runs/" + legacy[1] + (legacy[2] ? "/" + legacy[2] : ""); history.replaceState(null, "", "#" + hash); }
    const [path, qs] = hash.replace(/^\//, "").split("?");
    const parts = path.split("/").filter(Boolean).map(decodeURIComponent);
    const name = parts[0] || "";
    const view = VIEWS[name] || VIEWS[""];
    if (S.view && S.view.destroy) S.view.destroy();
    S.name = name in VIEWS ? name : ""; S.params = parts.slice(1); S.query = new URLSearchParams(qs || "");
    renderSpine();
    const c = clear($("#content")); c.scrollTop = 0;
    S.view = view(c, S.params, S.query) || {};
  }
  window.addEventListener("hashchange", route);

  // ------------------------------------------------------------------------------------------------ overview
  VIEWS[""] = function (c) {
    // the frame is built once; each live tick replaces only the parts that show live data, never the goal box someone is typing in
    const top = h("div", { id: "ov-top" }), held = h("div", { id: "ov-held" }), recent = h("div", { id: "ov-recent" }), act = h("div", { id: "ov-activity" });
    put(c, h("div", { class: "cols" }, h("div", null, top, held, recent), h("div", null, act, h("section", { class: "section" }, h("h2", null, "What is here"), whatIsHere()))));
    let showing = null;
    const draw = () => {
      const live = D.runs.filter((r) => r.status === "running" || r.status === "queued");
      const mode = live.length ? "live" : "launch";
      if (mode === "live") { clear(top).append(liveNow(live)); showing = "live"; }
      else if (showing !== "launch") { clear(top).append(launchBox({ compact: true })); showing = "launch"; }
      const hl = D.approvals;
      clear(held);
      if (hl.length) put(held, h("section", { class: "section" }, h("h2", null, "Waiting for you", h("span", { class: "muted" }, hl.length + (hl.length === 1 ? " action" : " actions"))), approvalsList(hl.slice(0, 5), { compact: true }),
        hl.length > 5 ? h("p", { class: "small", style: "margin-top:8px" }, h("a", { href: "#/approvals" }, "All " + hl.length + " waiting")) : null));
      put(clear(recent), h("section", { class: "section" }, h("h2", null, "Recent runs", h("span", { class: "muted" }, D.runs.length ? D.runs.length + " kept" : "")), recentRuns(D.runs.slice(0, 8)),
        D.runs.length > 8 ? h("p", { class: "small", style: "margin-top:8px" }, h("a", { href: "#/runs" }, "Every run, with each step's inputs and outputs")) : null));
      if (D.stats) clear(act).append(activity(D.stats));
    };
    draw();
    return { onLive: draw };
  };
  function liveNow(live) {
    return h("section", null, h("h2", { style: "font-size:16px;margin-bottom:10px" }, "Live now", h("span", { class: "muted", style: "font-size:13px;font-weight:400;margin-left:8px" }, live.length + " running")),
      h("div", { class: "rows" }, live.map((r) => { const v = D.live[r.run_id]; return h("a", { class: "r", href: "#/runs/" + encodeURIComponent(r.run_id) }, h("span", { class: "lamp run" }),
        h("span", null, h("div", { class: "t" }, r.goal || r.run_id), h("div", { class: "m" }, v ? v.steps.filter((s) => s.result).length + " of " + v.steps.length + " steps done" + (v.steps.some((s) => s.status === "running") ? ", now " + v.steps.filter((s) => s.status === "running").map((s) => agent(s.agent).name).join(", ") : "") : label(r.status))),
        h("span", { class: "end" }, v ? miniDots(v) : null)); })),
      h("div", { style: "margin-top:12px" }, h("a", { class: "btn", href: "#/launch" }, "Launch another run")));
  }
  function recentRuns(rows) {
    if (!rows.length) return h("div", { class: "empty" }, "No runs yet. Describe an outcome on Launch and the orchestrator plans and runs it.", h("div", null, h("a", { class: "btn primary", href: "#/launch" }, "Open Launch")));
    return h("div", { class: "rows" }, rows.map((r) => h("a", { class: "r", href: "#/runs/" + encodeURIComponent(r.run_id) }, h("span", { class: "lamp " + lampOf(r.status), title: label(r.status) }),
      h("span", null, h("div", { class: "t" }, r.goal || r.run_id), h("div", { class: "m" }, [r.label, r.customer_id, r.surface === "check" ? "designer test" : ""].filter(Boolean).join(", "))),
      h("span", { class: "end" }, label(r.status), h("br"), ago(r.created_at), r.cost_usd || r.unpriced_calls ? [h("br"), h("span", { title: r.unpriced_calls ? r.unpriced_calls + " model call(s) on a model the price table does not know" : "What this run cost at the configured rates" }, money(r.cost_usd, r.unpriced_calls))] : null))));
  }
  function activity(st) {
    const days = st.days, max = Math.max(1, ...days.map((d) => d.runs));
    const week = days.slice(-7).reduce((a, d) => ({ runs: a.runs + d.runs, ok: a.ok + d.completed, bad: a.bad + d.failed }), { runs: 0, ok: 0, bad: 0 });
    const rate = week.runs ? Math.round((week.ok / Math.max(1, week.ok + week.bad)) * 100) : null;
    return h("section", null, h("h2", { style: "font-size:16px;margin-bottom:10px" }, "Activity, last 14 days"),
      h("div", { class: "figures" }, h("div", { class: "figure" }, h("div", { class: "v" }, week.runs), h("div", { class: "l" }, "runs this week")),
        h("div", { class: "figure" }, h("div", { class: "v" }, rate === null ? "—" : rate + "%"), h("div", { class: "l" }, "finished without a failure")),
        h("div", { class: "figure" }, h("div", { class: "v" }, money(days.slice(-7).reduce((a, d) => a + (d.cost_usd || 0), 0))), h("div", { class: "l" }, "spend this week")),
        h("div", { class: "figure" }, h("div", { class: "v" }, money(st.totals.cost_usd, st.totals.unpriced_calls)), h("div", { class: "l" }, "spend, all runs kept" + (st.totals.unpriced_calls ? " (" + st.totals.unpriced_calls + " calls unpriced)" : ""))),
        h("div", { class: "figure" }, h("div", { class: "v" }, num(st.totals.llm_calls)), h("div", { class: "l" }, "model calls, " + num(st.totals.input_tokens + st.totals.output_tokens) + " tokens"))),
      h("div", { class: "bars", style: "margin-top:14px", role: "img", "aria-label": "Runs per day" }, days.map((d) => { const H = 60; const seg = (n, cls) => n ? h("i", { class: cls, style: "height:" + Math.max(2, Math.round((n / max) * H)) + "px" }) : null;
        return h("a", { class: "d" + (d.runs ? "" : " none"), href: "#/runs?day=" + d.date, title: dayName(d.date) + ": " + d.runs + " run(s), " + d.completed + " completed, " + d.failed + " failed" + (d.cost_usd ? ", " + money(d.cost_usd) : "") }, seg(d.completed, "ok"), seg(d.open, "open"), seg(d.failed, "bad")); })),
      h("div", { class: "axis" }, h("span", null, dayName(days[0].date)), h("span", null, "today")),
      st.top_flows.length ? h("div", { style: "margin-top:14px" }, h("div", { class: "small muted", style: "margin-bottom:4px" }, "Most run"), h("div", { class: "row", style: "gap:6px" }, st.top_flows.slice(0, 6).map((f) => h("a", { class: "chip", href: "#/runs?label=" + encodeURIComponent(f.label), title: f.avg_cost_usd ? "about " + money(f.avg_cost_usd) + " a run" : "" }, f.label, h("span", { class: "muted" }, f.runs))))) : null);
  }
  function whatIsHere() {
    const cats = new Map();
    D.agents.forEach((a) => { if (!cats.has(a.category)) cats.set(a.category, { label: a.category_label, n: 0, id: a.category }); cats.get(a.category).n++; });
    const host = D.host;
    return h("div", { class: "stack" },
      h("div", null, h("div", { class: "row", style: "justify-content:space-between" }, h("a", { href: "#/agents", class: "strong" }, D.agents.length + " agents"), h("a", { href: "#/flows", class: "strong" }, D.flows.length + " flows")),
        h("div", { class: "catbar", style: "margin-top:8px" }, [...cats.values()].map((c) => h("i", { style: "flex:" + c.n + ";background:" + catColor(c.id), title: c.label + ": " + c.n }))),
        h("div", { class: "legend" }, [...cats.values()].map((c) => h("a", { href: "#/agents?category=" + c.id, style: "color:inherit" }, h("i", { style: "background:" + catColor(c.id) }), c.label, " ", h("span", { class: "muted" }, c.n))))),
      host ? h("div", { class: "small" }, h("div", { class: "muted", style: "margin-bottom:4px" }, "Model"), h("div", null, host.live ? host.label : "Offline placeholders"), host.models ? h("div", { class: "muted xs" }, "fast " + host.models.fast + ", strong " + host.models.pro) : null) : null,
      D.packs.length ? h("div", { class: "small" }, h("div", { class: "muted", style: "margin-bottom:4px" }, "Agent packs"), D.packs.map((p) => h("div", null, h("span", { class: "tag" }, p.tag), " ", p.name, h("span", { class: "muted" }, ", " + p.agents + " agents" + (p.home ? (p.home.connected ? ", connected" : ", not connected") : ""))))) : null,
      h("p", { class: "small" }, h("a", { href: "#/health" }, "Health and connections")));
  }

  // ------------------------------------------------------------------------------------------------ launch
  const EXAMPLES = [
    ["Launch kit for flood cover", "Launch our new flood-ready home insurance cover: brief, launch email, LinkedIn outreach, press release, social posts, tagged links and QR codes for print.", { url: "https://www.meridian-mutual.example/products/home-insurance.html", audience: "Homeowners in flood-exposed areas" }],
    ["Audit the home-insurance page", "Audit our website", { url: "https://www.meridian-mutual.example/products/home-insurance.html" }],
    ["Experimentation loop for the hero CTA", "Run an experimentation loop on the hero call to action of the home insurance page", { url: "https://www.meridian-mutual.example/products/home-insurance.html", test_idea: "Outcome-led hero CTA" }],
    ["Competitive pitch vs Harborline", "Prepare a competitive pitch against Harborline Insurance for the insurance industry", { competitors: "Harborline Insurance, Crestview Assurance", industry: "insurance" }],
    ["Explain the August dip", "Explain the August dip in sessions and conversion", { question: "Why did sessions and conversion rate dip in August?" }],
  ];
  const SHARED = [["url", "Page URL"], ["audience", "Audience"], ["industry", "Industry"], ["competitors", "Competitors (comma-separated)"], ["campaign_type", "Campaign type"], ["test_idea", "Test idea"], ["question", "Question for the data"]];
  const L = { goal: "", inputs: {}, mode: "auto", plan: null, planning: false, launching: false, autoApprove: false, open: false };
  try { Object.assign(L, JSON.parse(localStorage.getItem("cxnova.launch") || "{}"), { plan: null, planning: false, launching: false }); } catch (e) {}
  function remember() { try { localStorage.setItem("cxnova.launch", JSON.stringify({ goal: L.goal, inputs: L.inputs, mode: L.mode, open: L.open })); } catch (e) {} }
  function launchBox(opts) {
    opts = opts || {};
    const box = h("section", { class: "goal" });
    const ta = h("textarea", { placeholder: "Describe the outcome you want. The orchestrator plans which agents run, in what order, and what each one is asked to do.", "aria-label": "Goal" });
    ta.value = L.goal;
    ta.addEventListener("input", () => { L.goal = ta.value; L.plan = null; remember(); });
    ta.addEventListener("keydown", (e) => { if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); run(); } });
    const sel = h("select", { "aria-label": "How to run it", onchange: (e) => { L.mode = e.target.value; L.plan = null; remember(); redraw(); } },
      h("option", { value: "auto" }, "Let the orchestrator plan"),
      h("optgroup", { label: "Run a flow" }, D.flows.map((f) => h("option", { value: "flow:" + f.id }, f.name))),
      h("optgroup", { label: "Run one agent" }, D.agents.map((a) => h("option", { value: "agent:" + a.id }, a.name))));
    sel.value = L.mode;
    // the inputs: what the chosen flow or agent declares, then the shared ones the orchestrator's agents read
    const changed = () => { L.plan = null; remember(); };
    const flow = L.mode.startsWith("flow:") ? D.flows.find((f) => f.id === L.mode.slice(5)) : null;
    const declared = flow ? flow.inputs || [] : [];
    const mustOpen = declared.some((i) => i.required) || L.mode.startsWith("agent:");
    const inputsBox = h("div", { class: "inputs-grid" + (L.open || mustOpen || opts.compact === false ? "" : " hidden") });
    const textField = (k, lbl, desc, required, example) => { const i = h("input", { type: "text", placeholder: example ? "e.g. " + example : "", "aria-label": lbl, oninput: (e) => { const v = e.target.value.trim(); if (v) L.inputs[k] = v; else delete L.inputs[k]; changed(); } }); i.value = L.inputs[k] === undefined ? "" : (typeof L.inputs[k] === "string" ? L.inputs[k] : JSON.stringify(L.inputs[k])); return h("label", { class: "f" }, h("span", null, lbl, required ? h("span", { class: "muted" }, " required") : null), desc ? h("span", { class: "xs muted", style: "font-weight:400" }, desc) : null, i); };
    put(inputsBox, declared.map((i) => textField(i.name, i.name, i.description, i.required, i.example)),
      SHARED.filter(([k]) => !declared.some((i) => i.name === k)).map(([k, lbl]) => textField(k, lbl)));
    if (L.mode.startsWith("agent:")) {  // one agent: its own contract, filled with its self-test values the first time
      const id = L.mode.slice(6);
      clear(inputsBox).append(h("div", { class: "muted small" }, "Loading the agent's inputs…"));
      agentDetails(id).then((a) => { if (L.mode !== "agent:" + id) return; const props = (a.input_schema && a.input_schema.properties) || {};
        if (!Object.keys(L.inputs).some((k) => k in props)) { Object.assign(L.inputs, a.selftest_input || {}); remember(); }
        put(clear(inputsBox), schemaFields(props, (a.input_schema && a.input_schema.required) || [], L.inputs, changed)); }).catch((e) => clear(inputsBox).append(h("div", { class: "notice bad small" }, "Could not load the agent: " + e.message)));
    }
    const nInputs = () => Object.keys(L.inputs).filter((k) => L.inputs[k] !== undefined && L.inputs[k] !== "").length;
    const planBtn = h("button", { class: "btn", type: "button", onclick: preview, title: "The steps the orchestrator would run, what they cover of the goal, what they cannot do, and the likely cost. Nothing runs." }, L.planning ? "Assessing…" : "Assess and plan");
    const runBtn = h("button", { class: "btn primary", type: "button", onclick: run }, L.launching ? "Starting…" : "Run");
    const redraw = () => { const parent = box.parentElement; if (parent) parent.replaceChild(launchBox(opts), box); };
    put(box, ta,
      opts.compact ? h("div", { class: "examples" }, EXAMPLES.map(([t, g, inp]) => h("button", { type: "button", onclick: () => { L.goal = g; L.inputs = Object.assign({}, inp); L.mode = "auto"; L.plan = null; remember(); redraw(); } }, t))) : null,
      inputsBox,
      h("div", { class: "bar" }, sel,
        h("button", { class: "btn sm ghost", type: "button", onclick: () => { L.open = !L.open; remember(); inputsBox.classList.toggle("hidden", !(L.open || mustOpen)); } }, (nInputs() ? nInputs() + (nInputs() === 1 ? " input" : " inputs") : "Inputs")),
        h("span", { class: "right row" }, h("label", { class: "row small", style: "gap:6px", title: "Publish-type actions run without waiting for a person (mandatory gates still hold; the server may refuse)" }, h("input", { type: "checkbox", checked: L.autoApprove, onchange: (e) => { L.autoApprove = e.target.checked; } }), "Auto-approve"),
          planBtn, runBtn)));
    return box;
    async function preview() {
      if (L.planning) return;
      if (!L.goal.trim() && L.mode === "auto") { ta.focus(); toast("Describe the outcome first"); return; }
      L.planning = true; planBtn.textContent = "Assessing…"; planBtn.disabled = true;
      try {
        await assess();
        if (S.name !== "launch") navigate("#/launch"); else showPlan();
      } catch (e) { toast("Could not plan: " + e.message, 6000); }
      finally { L.planning = false; planBtn.textContent = "Assess and plan"; planBtn.disabled = false; }
    }
    async function assess() {   // one call: the plan, its cost estimate and the feasibility of the goal
      const body = { goal: L.goal, inputs: L.inputs };
      if (L.mode.startsWith("flow:")) body.playbook = L.mode.slice(5); else if (L.mode.startsWith("agent:")) body.agent = L.mode.slice(6);
      L.plan = await api("/plan", { body }); L.plan.goal = L.goal; L.plan.mode = L.mode; L.plan.ran = false;
      return L.plan;
    }
    async function run() {
      if (L.launching) return;
      if (!L.goal.trim() && L.mode === "auto") { ta.focus(); toast("Describe the outcome first"); return; }
      L.launching = true; runBtn.textContent = "Starting…"; runBtn.disabled = true;
      try {
        let view;
        // a goal is assessed before it runs: when something in it has no agent, the assessment is shown and the person decides
        const fresh = L.plan && L.plan.plan && L.plan.goal === L.goal && L.plan.mode === L.mode;
        if (L.mode === "auto" && !fresh) { runBtn.textContent = "Assessing…"; await assess(); }
        const feas = L.mode === "auto" && L.plan ? L.plan.feasibility : null;
        if (feas && !fresh && (feas.verdict === "partial" || feas.verdict === "not_feasible")) {
          if (S.name !== "launch") navigate("#/launch"); else showPlan();
          toast(feas.verdict === "not_feasible" ? "No agent here does what the goal asks: see the assessment" : "Part of the goal has no agent: see the assessment, then run what is covered or make the missing agent", 8000);
          const el = $("#feasibility"); if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
          return;
        }
        const inputs = Object.assign({}, L.inputs);
        if (inputs.competitors && typeof inputs.competitors === "string") inputs.competitors = inputs.competitors.split(",").map((x) => x.trim()).filter(Boolean);
        if (L.mode.startsWith("flow:")) view = await api("/flows/" + encodeURIComponent(L.mode.slice(5)) + "/run", { body: { inputs, goal: L.goal || undefined, wait_s: 0, auto_approve: L.autoApprove || undefined } });
        else {
          const body = { goal: L.goal || (L.mode.startsWith("agent:") ? agent(L.mode.slice(6)).name : ""), inputs, wait_s: 0, auto_approve: L.autoApprove || undefined };
          if (L.mode.startsWith("agent:")) body.agent = L.mode.slice(6);
          else if (L.plan && L.plan.plan && L.plan.plan.steps.length && L.plan.goal === L.goal && L.plan.mode === L.mode) { body.plan = { mode: L.plan.plan.mode, playbook: L.plan.plan.playbook, steps: L.plan.plan.steps.map((s) => ({ id: s.id, agent: s.agent, purpose: s.purpose, inputs: s.inputs, needs: s.needs })) }; if (L.plan.feasibility) body.feasibility = L.plan.feasibility; }
          view = await api("/orchestrate", { body });
        }
        if (view.run_id) { toast("Run started"); navigate("#/runs/" + encodeURIComponent(view.run_id)); refreshLive(); }
        else { L.plan = { route: view.route, plan: { steps: [] }, warnings: view.warnings, clarify: view }; if (S.name !== "launch") navigate("#/launch"); else showPlan(); }
      } catch (e) { toast("Could not start the run: " + e.message, 7000); }
      finally { L.launching = false; runBtn.textContent = "Run"; runBtn.disabled = false; }
    }
  }
  const VERDICT = { feasible: ["good", "Feasible: every part of the goal has an agent"], partial: ["warn", "Partly feasible: some of the goal has no agent here"],
                    not_feasible: ["bad", "Not feasible with the agents here"], unassessed: ["", "Not assessed: no model is connected"] };
  function feasibilityView(f, steps, p, readonly) {
    const [cls, title] = VERDICT[f.verdict] || VERDICT.unassessed;
    const stepName = (id) => { const st = steps.find((x) => x.id === id); return st ? agent(st.agent).name : id; };
    const req = (r) => h("div", { class: "req " + r.status }, h("span", { class: "lamp " + (r.status === "covered" ? "good" : r.status === "partial" ? "warn" : "bad") }),
      h("div", null, h("div", null, r.text), r.steps.length ? h("div", { class: "xs muted" }, "by " + r.steps.map(stepName).join(", ")) : null, r.note ? h("div", { class: "xs muted" }, r.note) : null));
    const covered = f.requirements.filter((r) => r.status === "covered"), rest = f.requirements.filter((r) => r.status !== "covered");
    return h("section", { class: "feas " + cls, id: "feasibility" },
      h("div", { class: "row", style: "gap:10px;align-items:flex-start" }, h("span", { class: "chip " + cls, style: "font-size:13px" }, title),
        f.method === "model" ? h("span", { class: "xs muted" }, "assessed by the model" + (f.metrics && f.metrics.cost_usd ? ", " + money(f.metrics.cost_usd) : "")) : null,
        !readonly && f.verdict !== "feasible" && f.verdict !== "unassessed" && steps.length ? h("button", { class: "btn sm right", type: "button", onclick: () => { L.plan.feasibility = f; const b = $(".goal .btn.primary"); if (b) b.click(); }, title: "Start the planned steps now: what they cover gets done, the rest does not" }, "Run what is covered") : null),
      h("div", { class: "cols", style: "margin-top:12px;gap:18px" },
        h("div", null, h("h3", null, "What this run will do"), f.can_do ? h("p", { class: "small" }, f.can_do) : null, covered.length ? h("div", { class: "reqs" }, covered.map(req)) : h("p", { class: "small muted" }, "Nothing in the goal is fully covered.")),
        h("div", null, h("h3", null, rest.length ? "What it cannot do" : "Nothing is missing"), f.cannot_do ? h("p", { class: "small" }, f.cannot_do) : null, rest.length ? h("div", { class: "reqs" }, rest.map(req)) : null,
          f.gaps.length ? h("div", { class: "gaps" }, h("h4", null, f.gaps.length === 1 ? "An agent that would fill the gap" : "Agents that would fill the gaps"),
            f.gaps.map((g) => h("div", { class: "gapcard" }, h("div", null, h("strong", null, g.agent_name), h("span", { class: "muted small" }, " · " + (g.category || "")), h("div", { class: "small" }, g.purpose),
                g.inputs.length || g.outputs.length ? h("div", { class: "xs muted" }, (g.inputs.length ? "takes " + g.inputs.join(", ") : "") + (g.inputs.length && g.outputs.length ? "; " : "") + (g.outputs.length ? "produces " + g.outputs.join(", ") : "")) : null),
              readonly ? null : D.operator || !D.me ? h("button", { class: "btn sm primary", type: "button", onclick: () => openWorkshop({ purpose: g.purpose + (g.inputs.length ? " It is given: " + g.inputs.join(", ") + "." : "") + (g.outputs.length ? " It produces: " + g.outputs.join(", ") + "." : ""), goal: p.goal || L.goal, name: g.agent_name, id: g.agent_id, category: g.category, onMade: () => { toast("Re-assessing the goal with the new agent"); const b = $(".goal .btn:not(.primary):not(.ghost)"); if (b) b.click(); } }) }, "Make this agent") : h("span", { class: "xs muted" }, "ask the operator to make it")))) : null)));
  }
  function estimateChip(e) {
    if (!e) return null;
    if (e.cost_usd === null || e.cost_usd === undefined) return h("span", { class: "chip", title: "None of these agents has run yet, so there is nothing to estimate from" }, "no cost history yet");
    const partial = e.known_steps < e.steps;
    return h("span", { class: "chip", title: "The mean of what each agent cost in its earlier runs" + (partial ? "; " + (e.steps - e.known_steps) + " step(s) have no history and are not counted" : "") },
      "likely " + money(e.cost_usd) + (partial ? "+" : "") + " from earlier runs");
  }
  function showPlan() { const box = $("#plan"); if (!box) return; clear(box); if (L.plan) box.append(planView(L.plan)); }
  function planView(p) {
    if (p.clarify) return h("div", { class: "notice warn", style: "margin-top:18px" }, h("strong", null, "The goal is too vague to route without a model planner. "), (p.clarify.final && p.clarify.final.summary_markdown) || "", (p.clarify.final && p.clarify.final.next_steps || []).length ? h("ul", { class: "small", style: "margin:6px 0 0;padding-left:18px" }, p.clarify.final.next_steps.map((x) => h("li", null, x))) : null);
    const steps = p.plan.steps.map((s) => Object.assign({}, s, { status: "planned" }));
    const r = p.route || {};
    const how = { explicit: "the steps you chose", planner: "planned by the model from the goal", router: "picked by the router from the goal" }[r.method] || r.method;
    return h("div", null,
      p.feasibility ? feasibilityView(p.feasibility, steps, p) : null,
      h("div", { class: "plan-head" }, h("span", { class: "chip ai" }, steps.length + (steps.length === 1 ? " step" : " steps")), h("span", { class: "chip" }, how), r.playbook ? h("a", { class: "chip", href: "#/flows/" + encodeURIComponent(r.playbook) }, "flow: " + r.playbook) : null,
        r.reason ? h("span", { class: "muted small" }, r.reason) : null, estimateChip(p.estimate), h("span", { class: "muted small right" }, "Run starts exactly these steps")),
      (p.warnings || []).length ? h("div", { class: "notice warn small", style: "margin-bottom:10px" }, h("ul", { style: "margin:0;padding-left:18px" }, p.warnings.map((w) => h("li", null, w)))) : null,
      graph(steps, { planned: true }),
      h("div", { class: "stack", style: "margin-top:12px;gap:0" }, steps.map((s) => { const a = agent(s.agent); return h("div", { class: "steprow", style: "padding:6px 0;border-bottom:1px solid var(--rule)" },
        h("span", { class: "stripe", style: "background:" + catColor(a.category) }), h("div", { class: "small" }, h("code", null, s.id), " ", h("a", { class: "strong", href: "#/agents/" + encodeURIComponent(s.agent) }, a.name),
          s.purpose ? h("span", { class: "muted" }, ": " + s.purpose) : null,
          (() => { const e = ((p.estimate || {}).per_step || []).find((x) => x.id === s.id); return e && e.cost_usd !== null && e.cost_usd !== undefined ? h("span", { class: "muted", title: "the mean of this agent's last " + e.runs + " run(s)" }, " about " + money(e.cost_usd)) : null; })(),
          Object.keys(s.inputs || {}).length ? h("div", { class: "xs muted" }, Object.entries(s.inputs).map(([k, v]) => k + " = " + (typeof v === "string" ? v : JSON.stringify(v))).join("; ")) : null,
          (s.needs || []).length ? h("div", { class: "xs muted" }, "after " + s.needs.join(", ")) : null)); })));
  }
  VIEWS.launch = function (c, params, q) {
    if (q.get("goal")) { L.goal = q.get("goal"); L.plan = null; remember(); }
    if (q.get("flow")) { L.mode = "flow:" + q.get("flow"); L.plan = null; remember(); }
    if (q.get("agent")) { L.mode = "agent:" + q.get("agent"); L.plan = null; remember(); }
    put(c, h("h1", { class: "page" }, "Launch"), h("p", { class: "page-lead" }, "Describe the outcome, preview what the orchestrator will run, then run it. The run opens live the moment it starts."),
      launchBox({ compact: true }), h("div", { id: "plan" }));
    showPlan();
    if (!L.plan) put($("#plan"), h("p", { class: "small muted", style: "margin-top:14px" }, "Preview the plan to see the steps before anything runs, or press Run (⌘ Enter) to let the orchestrator plan and start at once."));
  };

  // ------------------------------------------------------------------------------------------------ approvals
  async function decide(runId, decisions, after) {
    try {
      const v = await api("/runs/" + encodeURIComponent(runId) + "/approvals", { body: { decisions, wait_s: 2 } });
      toast(Object.values(decisions).every((d) => d === "reject") ? "Rejected" : "Approved");
      if (after) after(v);
      refreshLive();
    } catch (e) { toast("Could not decide: " + e.message, 6000); }
  }
  function approvalsList(items, opts) {
    opts = opts || {};
    const byRun = new Map();
    items.forEach((a) => { if (!byRun.has(a.run_id)) byRun.set(a.run_id, { goal: a.goal, status: a.run_status, rows: [] }); byRun.get(a.run_id).rows.push(a); });
    return h("div", { class: "stack" }, [...byRun.entries()].map(([rid, g]) => h("div", { class: "held" },
      h("div", { class: "row", style: "gap:8px" }, h("a", { class: "strong", href: "#/runs/" + encodeURIComponent(rid) }, g.goal || rid), h("span", { class: "chip " + runClass(g.status) }, g.status === "running" ? "still running: decide now, the run goes on" : label(g.status)),
        g.rows.length > 1 ? h("span", { class: "right row" }, h("button", { class: "btn sm primary", type: "button", onclick: () => decide(rid, Object.fromEntries(g.rows.map((a) => [a.key, "approve"]))) }, "Approve all " + g.rows.length), h("button", { class: "btn sm", type: "button", onclick: () => decide(rid, Object.fromEntries(g.rows.map((a) => [a.key, "reject"]))) }, "Reject all")) : null),
      g.rows.map((a) => h("div", { class: "a" },
        h("div", { class: "small" }, h("a", { class: "chip", href: "#/runs/" + encodeURIComponent(rid) + "/" + encodeURIComponent(a.step) }, a.step), " ", h("code", null, a.kind), " ", a.summary, a.mandatory ? h("span", { class: "chip bad", style: "margin-left:6px" }, "always needs a person") : null),
        h("div", { class: "row" }, h("button", { class: "btn sm primary", type: "button", onclick: () => decide(rid, { [a.key]: "approve" }) }, "Approve"), h("button", { class: "btn sm", type: "button", onclick: () => decide(rid, { [a.key]: "reject" }) }, "Reject")))))));
  }
  VIEWS.approvals = function (c) {
    const draw = () => { clear(c); put(c, h("h1", { class: "page" }, "Approvals"), h("p", { class: "page-lead" }, "Every action an agent proposed that changes something outside this layer: a publish, a send, a deploy. Nothing here happens until someone decides."),
      D.approvals.length ? approvalsList(D.approvals) : h("div", { class: "empty" }, "Nothing is waiting for a decision.")); };
    draw();
    return { onLive: draw };
  };

  // ------------------------------------------------------------------------------------------------ runs: list + viewer
  VIEWS.runs = function (c, params, q) {
    const RL = { filter: q.get("status") || "all", q: q.get("q") || "", label: q.get("label") || "", agent: q.get("agent") || "", day: q.get("day") || "" };
    put(c, h("div", { class: "runs-layout" }, h("aside", { class: "panel runlist", id: "runlist" }), h("section", { id: "rundetail" })));
    const listBox = $("#runlist");
    let history = null;   // every run the index keeps; the polled recent runs overlay it, so new runs and status changes show within seconds
    api("/runs?limit=2000").then((rows) => { history = rows; renderList(); }).catch(() => {});
    const allRuns = () => { if (!history) return D.runs; const by = new Map(history.map((r) => [r.run_id, r])); D.runs.forEach((r) => by.set(r.run_id, r)); return [...by.values()].sort((a, b) => (b.created_at || "").localeCompare(a.created_at || "")); };
    function matches(r) {
      const f = RL.filter, s = r.status;
      if (f === "live" && !["running", "queued"].includes(s)) return false;
      if (f === "approval" && s !== "awaiting_approval") return false;
      if (f === "done" && !["completed", "completed_with_errors"].includes(s)) return false;
      if (f === "bad" && !["failed", "stalled"].includes(s)) return false;
      if (RL.label && r.label !== RL.label) return false;
      if (RL.agent && !(r.agents || []).includes(RL.agent)) return false;
      if (RL.day && dayKey(r.created_at) !== RL.day) return false;
      const qq = RL.q.toLowerCase();
      return !qq || [r.goal, r.label, r.run_id, r.customer_id].some((x) => (x || "").toLowerCase().includes(qq));
    }
    // the head (search box, filters) is built once and only rebuilt when a filter chip changes; live ticks refresh the items and the count
    const FILTERS = [["all", "All"], ["live", "Live"], ["approval", "Needs approval"], ["done", "Completed"], ["bad", "Failed"]];
    const countEl = h("div", { class: "xs muted" }), itemsEl = h("div", { class: "items" });
    const head = h("div", { class: "head" });
    function renderHead() {
      put(clear(head),
        h("input", { type: "text", placeholder: "Find a run", "aria-label": "Find a run", value: RL.q, oninput: (e) => { RL.q = e.target.value; renderItems(); } }),
        h("div", { class: "seg" }, FILTERS.map(([id, text]) => h("button", { type: "button", "aria-pressed": String(RL.filter === id), onclick: () => { RL.filter = id; $$(".seg button", head).forEach((b) => b.setAttribute("aria-pressed", String(b.textContent === text))); renderItems(); } }, text))),
        RL.label || RL.agent || RL.day ? h("div", { class: "row", style: "gap:6px" }, RL.label ? h("button", { class: "chip ai", type: "button", onclick: () => { RL.label = ""; renderHead(); renderItems(); } }, "flow: " + RL.label + " ×") : null,
          RL.agent ? h("button", { class: "chip ai", type: "button", onclick: () => { RL.agent = ""; renderHead(); renderItems(); } }, agent(RL.agent).name + " ×") : null,
          RL.day ? h("button", { class: "chip ai", type: "button", onclick: () => { RL.day = ""; renderHead(); renderItems(); } }, dayName(RL.day) + " ×") : null) : null,
        countEl);
    }
    function renderItems() {
      const every = allRuns(), rows = every.filter(matches);
      countEl.textContent = rows.length === every.length ? rows.length + (rows.length === 1 ? " run" : " runs") : rows.length + " of " + every.length + " runs";
      const keep = itemsEl.scrollTop;
      clear(itemsEl);
      if (!every.length) itemsEl.append(h("div", { class: "empty small", style: "border:0" }, "No runs yet."));
      else if (!rows.length) itemsEl.append(h("div", { class: "empty small", style: "border:0" }, "No run matches."));
      let day = null;
      for (const r of rows) {
        const dk = dayKey(r.created_at);
        if (dk !== day) { day = dk; itemsEl.append(h("div", { class: "day" }, dayName(dk))); }
        itemsEl.append(h("button", { class: "item", type: "button", "aria-current": String(r.run_id === R.id), onclick: () => navigate("#/runs/" + encodeURIComponent(r.run_id)) },
          h("span", { class: "lamp " + lampOf(r.status), title: label(r.status) }),
          h("span", null, h("span", { class: "g" }, r.goal || r.run_id), h("span", { class: "m" }, label(r.status) + ", " + ago(r.created_at) + (r.duration_ms ? ", " + dur(r.duration_ms) : "") + (r.cost_usd || r.unpriced_calls ? ", " + money(r.cost_usd, r.unpriced_calls) : "")),
            r.label || r.customer_id ? h("span", { class: "m" }, [r.label, r.customer_id].filter(Boolean).join(", ")) : null)));
      }
      itemsEl.scrollTop = keep;
    }
    function renderList() { if (!head.parentElement) listBox.append(head, itemsEl); if (!head.childElementCount) renderHead(); renderItems(); }
    renderList();
    const detail = $("#rundetail");
    if (params[0]) openRun(detail, params[0], params[1] || null);
    else {
      const first = allRuns().find(matches);
      if (first) openRun(detail, first.run_id, null); else put(detail, h("div", { class: "empty" }, "Select a run, or launch one.", h("div", null, h("a", { class: "btn primary", href: "#/launch" }, "Open Launch"))));
    }
    return { onLive: () => { renderList(); }, destroy: () => { clearTimeout(R.timer); R.id = null; } };
  };

  // the run viewer: one run, live, step by step
  const R = { id: null, view: null, trace: [], sel: null, gtab: "flow", stab: null, stepSig: "", timer: null, opened: {} };
  function openRun(box, id, step) {
    clearTimeout(R.timer);
    R.id = id; R.view = null; R.trace = []; R.sel = step; R.stab = null; R.stepSig = ""; R.opened = {}; R.box = box;
    clear(box).append(h("div", { class: "empty" }, "Loading the run…"));
    refreshRun(true);
  }
  async function refreshRun(first) {
    clearTimeout(R.timer);
    const id = R.id;
    if (!id) return;
    try {
      const v = await api("/runs/" + encodeURIComponent(id));
      let tr = [];
      try { tr = await api("/runs/" + encodeURIComponent(id) + "/trace?since=" + R.trace.length); } catch (e) { /* no trace yet */ }
      if (id !== R.id) return;
      R.view = v; R.trace = R.trace.concat(tr || []);
      if (!R.sel || !v.steps.some((s) => s.id === R.sel)) R.sel = pickStep(v);
      renderRun(first);
    } catch (e) {
      if (e.status === 401 || e.status === 403) return gate(e.message);
      if (id === R.id) clear(R.box).append(h("div", { class: "notice bad" }, "Could not load the run: " + e.message));
      return;
    }
    if (!R.view.settled && R.view.status !== "stalled") {
      const again = () => { if (id !== R.id) return; if (document.hidden) { R.timer = setTimeout(again, 2000); return; } refreshRun(); };
      R.timer = setTimeout(again, 2000);
    }
  }
  function pickStep(v) {
    const run = v.steps.find((s) => s.status === "running"); if (run) return run.id;
    const held = v.steps.find((s) => s.status === "needs_approval"); if (held) return held.id;
    const done = v.steps.filter((s) => s.result); return (done.length ? done[done.length - 1] : v.steps[0] || {}).id || null;
  }
  function selectStep(sid) { R.sel = sid; R.stab = null; R.stepSig = ""; history.replaceState(null, "", "#/runs/" + encodeURIComponent(R.id) + "/" + encodeURIComponent(sid)); renderGraphArea(); renderStep(); const p = $("#steppanel"); if (p) p.scrollIntoView({ behavior: "smooth", block: "nearest" }); }
  function renderRun(first) {
    const box = R.box;
    if (first || !$("#runhead", box)) clear(box).append(h("section", { id: "runhead" }), h("section", { class: "section", id: "graphpanel" }), h("section", { class: "section panel", id: "steppanel" }));
    renderHead(); renderGraphArea(); renderStep();
  }
  function runStats(v) {
    let calls = 0, tin = 0, tout = 0, files = 0, fails = 0, done = 0, cost = 0, unpriced = 0;
    for (const s of v.steps) { const r = s.result; if (!r) continue; done++; const m = r.metrics || {}; calls += m.llm_calls || 0; tin += m.input_tokens || 0; tout += m.output_tokens || 0; cost += m.cost_usd || 0; unpriced += m.unpriced_calls || 0; files += (r.artifacts || []).length; fails += (r.checks || []).filter((c) => c.status === "fail").length; }
    const own = (v.final || {}).metrics || {};   // the orchestrator's planning and summary calls
    calls += own.llm_calls || 0; tin += own.input_tokens || 0; tout += own.output_tokens || 0; cost += own.cost_usd || 0; unpriced += own.unpriced_calls || 0;
    return { calls, tin, tout, files, fails, done, cost, unpriced };
  }
  function runTimes(v) {
    let start = null, end = null;
    for (const s of v.steps) { const t = (s.result || {}).timing || {}; const a = ts(t.started_at || s.started_at), b = ts(t.finished_at); if (a && (!start || a < start)) start = a; if (b && (!end || b > end)) end = b; }
    const first = R.trace.length ? ts(R.trace[0].ts) : null; if (first && (!start || first < start)) start = first;
    return { start, end };
  }
  function renderHead() {
    const old = $("#runhead", R.box);
    $$("details[data-k]", old).forEach((d) => { R.opened[d.dataset.k] = d.open; });
    const isOpen = (k, dflt) => (k in R.opened) ? R.opened[k] : dflt;
    const v = R.view, p = clear(old), st = runStats(v), t = runTimes(v), live = !v.settled && v.status !== "stalled";
    const row = D.runs.find((r) => r.run_id === v.run_id) || {};
    const took = t.start ? (live ? since(new Date(t.start).toISOString()) : dur(((t.end || Date.now()) - t.start))) : "";
    put(p, h("div", { class: "runhead" },
      h("div", { class: "grow", style: "min-width:260px" },
        h("div", { class: "row", style: "gap:6px;margin-bottom:6px" },
          h("span", { class: "chip " + runClass(v.status) }, live ? h("span", { class: "lamp run" }) : null, label(v.status)),
          row.label ? h("a", { class: "chip ai", href: "#/runs?label=" + encodeURIComponent(row.label) }, row.label) : null,
          v.route && v.route.method ? h("span", { class: "chip", title: v.route.reason || "" }, { explicit: "steps named by the caller", planner: "planned by the model", router: "picked by the router" }[v.route.method] || v.route.method) : null),
        h("h1", null, v.goal || "(no goal)"),
        h("div", { class: "meta" },
          h("span", null, "Run ", h("code", null, v.run_id), " ", h("button", { class: "btn sm ghost", type: "button", onclick: () => copy(v.run_id), title: "Copy the run id" }, "copy")),
          t.start ? h("span", null, "started " + when(new Date(t.start).toISOString())) : null,
          took ? h("span", null, live ? "running for " : "took ", took) : null,
          row.customer_id ? h("span", null, "customer ", h("code", null, row.customer_id)) : null,
          row.surface ? h("span", null, "via " + (row.surface === "check" ? "designer test" : row.surface)) : null)),
      h("div", { class: "row" },
        v.pending_approvals.length ? h("button", { class: "btn primary", type: "button", onclick: () => { if (window.confirm("Approve all " + v.pending_approvals.length + " held action(s)?")) decide(v.run_id, { "*": "approve" }, afterDecision); } }, "Approve all held") : null,
        v.resumable && (v.status === "stalled" || v.status === "failed") ? h("button", { class: "btn primary", type: "button", onclick: continueRun }, "Continue the run") : null,
        v.final && v.final.report ? h("button", { class: "btn", type: "button", onclick: () => download(v.final.report.replace(/^[^/]+\//, ""), "summary.md") }, "summary.md") : null,
        h("button", { class: "btn ghost", type: "button", onclick: () => refreshRun(), title: "Refresh now" }, "Refresh"))),
      h("div", { class: "figures", style: "margin-top:16px" },
        h("div", { class: "figure" }, h("div", { class: "v" }, st.done, h("small", null, "/ " + v.steps.length)), h("div", { class: "l" }, "steps run")),
        h("div", { class: "figure" }, h("div", { class: "v" }, num(st.calls)), h("div", { class: "l" }, "model calls")),
        h("div", { class: "figure" }, h("div", { class: "v" }, num(st.tin + st.tout)), h("div", { class: "l" }, "tokens (" + num(st.tin) + " in, " + num(st.tout) + " out)")),
        h("div", { class: "figure", title: "Model calls priced at the rates in config/settings.yaml (llm.pricing); the orchestrator's own planning and summary calls included" }, h("div", { class: "v" }, money(st.cost, st.unpriced)), h("div", { class: "l" }, "cost" + (st.unpriced ? ", " + st.unpriced + " call(s) unpriced" : "") + (live ? " so far" : ""))),
        h("div", { class: "figure" }, h("div", { class: "v" }, st.files), h("div", { class: "l" }, "files produced")),
        h("div", { class: "figure" }, h("div", { class: "v", style: st.fails ? "color:var(--shu)" : "" }, st.fails), h("div", { class: "l" }, "failed checks"))));
    if (v.status === "stalled") put(p, h("div", { class: "notice bad", style: "margin-top:12px" }, "This run stopped mid-way (the service restarted). Continue it: the finished steps are not run again."));
    if (v.error) put(p, h("div", { class: "notice bad", style: "margin-top:12px" }, v.error));
    if (v.pending_approvals.length) put(p, h("div", { class: "held" },
      h("strong", null, v.pending_approvals.length + (v.pending_approvals.length === 1 ? " action waits for a decision" : " actions wait for a decision")), v.status === "running" ? h("span", { class: "small" }, ": decide now, the rest of the run goes on") : null,
      v.pending_approvals.map((a) => h("div", { class: "a" },
        h("div", { class: "small" }, h("button", { class: "chip", type: "button", onclick: () => selectStep(a.step), style: "cursor:pointer" }, a.step), " ", h("code", null, a.kind), " ", a.summary, a.mandatory ? h("span", { class: "chip bad", style: "margin-left:6px" }, "always needs a person") : null),
        h("div", { class: "row" }, h("button", { class: "btn sm primary", type: "button", onclick: () => decide(v.run_id, { [a.key]: "approve" }, afterDecision) }, "Approve"), h("button", { class: "btn sm", type: "button", onclick: () => decide(v.run_id, { [a.key]: "reject" }, afterDecision) }, "Reject"))))));
    const f = v.final;
    if (f && (f.headline || f.summary_markdown)) put(p, h("details", { style: "margin-top:14px", open: isOpen("outcome", true), dataset: { k: "outcome" } }, h("summary", { class: "strong", style: "cursor:pointer" }, "Outcome: " + (f.headline || "")),
      h("div", { style: "margin-top:8px" }, f.method === "model" ? md(f.summary_markdown) : null,
        (f.next_steps || []).length ? h("div", null, h("h4", { style: "margin:8px 0 4px" }, "Next steps"), h("ul", { class: "small", style: "margin:0;padding-left:18px" }, f.next_steps.map((x) => h("li", null, x)))) : null)));
    if (v.route && v.route.feasibility) put(p, h("details", { style: "margin-top:10px", open: isOpen("scope", v.route.feasibility.verdict !== "feasible"), dataset: { k: "scope" } }, h("summary", { class: "small muted", style: "cursor:pointer" }, "Scope: what this run was set to do, and what it could not"),
      h("div", { style: "margin-top:8px" }, feasibilityView(v.route.feasibility, v.steps, { goal: v.goal }, true))));
    put(p, h("details", { style: "margin-top:10px", open: isOpen("inputs", false), dataset: { k: "inputs" } }, h("summary", { class: "small muted", style: "cursor:pointer" }, "Run inputs" + (v.warnings && v.warnings.length ? " and " + v.warnings.length + " warning(s)" : "")),
      h("div", { class: "kv", style: "margin-top:8px" }, h("dt", null, "Goal"), h("dd", null, v.goal || "—"), h("dt", null, "Shared inputs"), h("dd", null, v.inputs && Object.keys(v.inputs).length ? h("div", { class: "jt" }, jt(v.inputs, undefined, true)) : "none")),
      v.warnings && v.warnings.length ? h("ul", { class: "small", style: "margin:8px 0 0;padding-left:18px" }, v.warnings.map((w) => h("li", null, w))) : null));
  }
  function afterDecision(v) { if (v && v.run_id === R.id) { R.view = v; R.stepSig = ""; renderRun(); } refreshRun(); }
  async function continueRun() {
    try { const v = await api("/runs/" + encodeURIComponent(R.id) + "/continue", { body: { wait_s: 1 } }); toast("Continuing the run"); R.view = v; R.stepSig = ""; renderRun(); refreshRun(); refreshLive(); }
    catch (e) { toast("Could not continue: " + e.message, 6000); }
  }
  function renderGraphArea() {
    const p = $("#graphpanel", R.box); if (!p) return;
    const prev = $(".graph", p) || $(".log", p), keep = prev ? { top: prev.scrollTop, left: prev.scrollLeft, bottom: prev.scrollHeight - prev.scrollTop - prev.clientHeight < 8 } : null;
    const v = R.view, live = !v.settled && v.status !== "stalled";
    clear(p).append(h("div", { class: "tabs", role: "tablist" }, [["flow", "Flow"], ["timeline", "Timeline"], ["log", "Live log"]].map(([id, text]) =>
      h("button", { type: "button", role: "tab", "aria-selected": String(R.gtab === id), onclick: () => { R.gtab = id; renderGraphArea(); } }, text, id === "log" ? h("span", { class: "n" }, R.trace.length) : null))));
    if (R.gtab === "flow") p.append(graph(v.steps, { live, selected: R.sel, onSelect: selectStep, width: p.clientWidth })); else if (R.gtab === "timeline") p.append(timeline()); else p.append(log(R.trace, null));
    const now = $(".graph", p) || $(".log", p);
    if (now && keep) { if (R.gtab === "log" && keep.bottom) now.scrollTop = now.scrollHeight; else { now.scrollTop = keep.top; now.scrollLeft = keep.left; } }
    else if (now && R.gtab === "log") now.scrollTop = now.scrollHeight;
  }
  function timeline() {
    const v = R.view, now = Date.now(), spans = {};
    let t0 = null, t1 = null;
    for (const s of v.steps) {
      const t = (s.result || {}).timing || {}, a = ts(t.started_at || s.started_at), b = ts(t.finished_at) || (s.status === "running" ? now : null);
      if (a) { spans[s.id] = [a, b || a]; t0 = t0 === null ? a : Math.min(t0, a); t1 = t1 === null ? (b || a) : Math.max(t1, b || a); }
    }
    if (t0 === null) return h("div", { class: "empty small" }, "No step has started yet.");
    const total = Math.max(1, t1 - t0);
    const tl = h("div", { class: "tl" }, h("div", { class: "axis" }, h("span", null, "Step"), h("div", { class: "ticks" }, [0, .25, .5, .75, 1].map((f) => h("span", null, dur(total * f) || "0 s"))), h("span", null, "Took")));
    for (const s of v.steps) {
      const sp = spans[s.id], a = agent(s.agent);
      const bar = sp ? h("span", { class: "bar " + (s.status === "running" ? "run" : lampOf(s.status) === "warn" ? "warn" : lampOf(s.status) === "bad" ? "bad" : ""), style: `left:${((sp[0] - t0) / total) * 100}%;width:${Math.max(0.4, ((sp[1] - sp[0]) / total) * 100)}%` }) : null;
      tl.append(h("div", { class: "r" + (R.sel === s.id ? " sel" : ""), onclick: () => selectStep(s.id), title: a.name },
        h("span", { class: "lab" }, h("span", { class: "lamp " + lampOf(s.status), style: "margin-right:6px" }), s.id, h("span", { class: "muted" }, ", " + (a.name || s.agent))),
        h("span", { class: "track" }, bar), h("span", { class: "muted", style: "text-align:right" }, sp ? (s.status === "running" ? since(s.started_at) : dur(sp[1] - sp[0])) : label(s.status))));
    }
    return h("div", null, tl, h("p", { class: "xs muted", style: "margin-top:8px" }, "Bars that overlap ran in parallel. A step starts when the steps it depends on have finished and a slot is free."));
  }
  const QUIET = new Set(["span.start", "agent.start", "llm.call"]);
  function describe(e) {
    const k = e.kind;
    if (k === "model.route") return ["model", "Model call " + (e.step || "") + " to " + (e.model || "") + (e.work ? " (" + e.work + (e.effort ? ", effort " + e.effort : "") + ")" : "")];
    if (k === "model.done") return ["model", "Model answered " + (e.step || "") + " in " + dur(e.duration_ms) + (e.input_tokens || e.output_tokens ? ", " + num(e.input_tokens) + " tokens in, " + num(e.output_tokens) + " out" : "")];
    if (k === "llm.structured_by_prompt" || k === "llm.structured_fallback") return ["", "Structured answer asked for in the prompt (" + (e.provider || "") + ")"];
    if (k === "pack.home") return ["good", "Served by " + (e.home || "the home product") + (e.run ? " (run " + e.run + ")" : "")];
    if (k === "span.end") return ["", "Stage " + (e.stage || e.name || "") + " done in " + dur(e.duration_ms)];
    if (k === "span.error") return ["bad", "Stage " + (e.stage || e.name || "") + " failed: " + (e.error || "")];
    if (k === "agent.end") return [e.status === "error" ? "bad" : "good", "Agent finished: " + label(e.status) + (e.duration_ms ? " in " + dur(e.duration_ms) : "") + (e.error ? ": " + e.error : "")];
    if (k === "agent.log") return ["", e.message || ""];
    if (k === "orchestrator.start") return ["", "Run started"];
    if (k === "orchestrator.plan") return ["", "Plan: " + (e.steps || []).length + " step(s) via " + (e.method || e.mode || "")];
    if (k === "orchestrator.step.start") return ["", "Step " + e.step + " started"];
    if (k === "orchestrator.step.end") return [e.status === "error" ? "bad" : "good", "Step " + e.step + " " + label(e.status)];
    if (k === "orchestrator.step.skipped") return ["bad", "Step " + e.step + " skipped: " + (e.reason || "")];
    if (k === "orchestrator.decision") return ["", "Decision on " + e.key + ": " + e.decision + (e.by ? " by " + e.by : "")];
    if (k === "orchestrator.end") return [e.status === "failed" ? "bad" : "good", "Run " + label(e.status) + (e.headline ? ": " + e.headline : "")];
    if (k === "orchestrator.error") return ["bad", "Run error: " + (e.error || "")];
    if (k === "orchestrator.continue") return ["", "Run continued from its last checkpoint"];
    if (k === "action.done") return ["good", "Action executed: " + (e.action || "")];
    if (k === "action.failed") return ["bad", "Action failed: " + (e.action || "") + " " + (e.error || "")];
    if (k === "llm.throttled") return ["", "Waited " + dur(e.waited_ms) + " for a model slot"];
    if (k === "llm.rate_limited") return ["bad", "Rate limited by the model provider; retry " + (e.retry || "")];
    if (k === "shop.customer") return ["", "Customer " + e.customer + " on " + (e.provider || "")];
    if (k.startsWith("shop.flow")) return ["", "Flow " + (e.flow || "") + " v" + (e.version || "")];
    return ["", k.replace(/[._]/g, " ")];
  }
  function log(events, filter) {
    const box = h("div", { class: "log" });
    const t0 = R.trace.length ? ts(R.trace[0].ts) : null;
    const rows = events.filter((e) => !QUIET.has(e.kind) && (!filter || filter(e)));
    if (!rows.length) return h("div", { class: "empty small" }, "Nothing recorded yet.");
    for (const e of rows.slice(-1500)) {
      const [cls, text] = describe(e), off = t0 && ts(e.ts) ? (ts(e.ts) - t0) / 1000 : 0;
      const who = e.step && e.kind.startsWith("orchestrator") ? e.step : e.agent ? agent(e.agent).name : "run";
      box.append(h("div", { class: "e " + cls }, h("span", { class: "t" }, "+" + Math.floor(off / 60) + ":" + String(Math.floor(off % 60)).padStart(2, "0")), h("span", { class: "w", title: who }, who), h("span", { class: "d" }, text)));
    }
    return box;
  }
  function lookup(expr) {
    const v = R.view, walk = (obj, parts) => { let cur = obj; for (const p of parts) { if (cur === null || cur === undefined) return undefined; cur = Array.isArray(cur) ? cur[Number(p)] : cur[p]; } return cur; };
    const out = [];
    for (const raw of expr.split("|")) {
      const o = raw.trim(); let src = null, val;
      if (o === "goal") { src = { kind: "goal" }; val = v.goal; }
      else if (o.startsWith("input.")) { src = { kind: "input", name: o.slice(6) }; val = walk(v.inputs || {}, o.split(".").slice(1)); }
      else if (o.startsWith("steps.")) { const p = o.split("."); const st = v.steps.find((s) => s.id === p[1]); src = { kind: "step", step: p[1], path: p.slice(3).join(".") || p.slice(2).join(".") }; val = st && st.result ? walk(st.result, p.slice(2)) : undefined; }
      else if (/^'.*'$/.test(o)) { src = { kind: "literal", text: o.slice(1, -1) }; val = o.slice(1, -1); }
      if (src) out.push(Object.assign(src, { found: !(val === undefined || val === null || val === "" || (Array.isArray(val) && !val.length)) }));
    }
    let used = false;
    for (const s of out) { if (!used && s.found) { s.used = true; used = true; } }
    return out;
  }
  function sourcesOf(raw) {
    if (raw === undefined) return null;
    if (raw === "$goal") return [{ kind: "goal", used: true, found: true }];
    if (raw && typeof raw === "object" && !Array.isArray(raw) && Object.keys(raw).length === 1 && raw.$ref) return lookup(String(raw.$ref));
    if (typeof raw === "string") { const refs = [...raw.matchAll(/\$\{([^}]+)\}/g)]; if (refs.length) return refs.flatMap((m) => lookup(m[1])); }
    return [{ kind: "fixed", used: true, found: true }];
  }
  const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
  function srcChip(s) {
    const cls = "chip" + (s.used ? " used" : "");
    if (s.kind === "goal") return h("span", { class: cls }, "run goal");
    if (s.kind === "input") return h("span", { class: cls }, "run input " + s.name);
    if (s.kind === "step") return h("button", { type: "button", class: cls, onclick: () => selectStep(s.step), title: "Open step " + s.step }, "step " + s.step + (s.path ? " › " + s.path : ""));
    if (s.kind === "literal") return h("span", { class: cls }, "fallback '" + s.text + "'");
    if (s.kind === "fixed") return h("span", { class: cls }, "set in the flow");
    return h("span", { class: cls }, s.text || s.kind);
  }
  function inferred(name, val) {
    const v = R.view, inp = v.inputs || {};
    if (val === undefined) return null;
    if (v.goal && same(val, v.goal)) return [{ kind: "goal", used: true }, { kind: "note", text: "the agent's main input takes the goal" }];
    if (name in inp && same(inp[name], val)) return [{ kind: "input", name, used: true }];
    const url = inp.url || inp.page_url;
    if (url && (same(val, url) || same(val, [url]))) return [{ kind: "input", name: inp.url ? "url" : "page_url", used: true }];
    return [{ kind: "note", text: "filled by the orchestrator" }];
  }
  function inputsTab(s) {
    const r = s.result, raw = s.inputs || {}, got = (r && r.step_inputs) || null;
    const keys = [...new Set([...Object.keys(raw), ...Object.keys(got || {})])];
    const out = h("div", { class: "stack" });
    if (!got) out.append(h("div", { class: "notice" }, s.status === "skipped" ? "This step was skipped, so its inputs were never resolved." : "The values are resolved when the step starts. Below is what the flow sets."));
    if (keys.length) {
      out.append(h("div", { class: "tw" }, h("table", { class: "bind" }, h("thead", null, h("tr", null, h("th", null, "Field"), h("th", null, "Set in the flow"), got ? h("th", null, "Value the agent received") : null, h("th", null, "Came from"))),
        h("tbody", null, keys.map((k) => {
          const has = Object.prototype.hasOwnProperty.call(raw, k), val = got ? got[k] : undefined;
          const src = has ? sourcesOf(raw[k]) : inferred(k, val);
          const missing = got && val === undefined;
          return h("tr", null, h("td", null, h("code", null, k)),
            h("td", null, has ? h("code", null, typeof raw[k] === "string" ? raw[k] : JSON.stringify(raw[k])) : h("span", { class: "muted small" }, "not set")),
            got ? h("td", null, missing ? h("span", { class: "muted small" }, "nothing: the agent's own default applies") : valueView(val)) : null,
            h("td", null, h("span", { class: "src" }, (src || []).map((x) => x.kind === "note" ? h("span", { class: "muted xs" }, x.text) : srcChip(x)))));
        })))));
    } else out.append(h("p", { class: "muted small" }, "This step takes no inputs: the agent works from its defaults and the earlier steps."));
    const up = (r && r.upstream) || [];
    out.append(h("div", null, h("h4", { style: "margin:6px 0" }, "Earlier work this agent could read"),
      up.length ? h("div", { class: "row", style: "gap:6px" }, up.slice().reverse().map((u) => h("button", { type: "button", class: "chip" + (u.direct ? " ai" : ""), style: "cursor:pointer", onclick: () => selectStep(u.step), title: "Open step " + u.step }, u.step + ", " + agent(u.agent_id).name + (u.direct ? " (depends on)" : "")))) :
        h("p", { class: "muted small" }, r ? "None: nothing had finished when this step started." : (s.needs || []).length ? "It will read the output of " + s.needs.join(", ") + " and of every step finished before it." : "None: this step starts the flow."),
      h("p", { class: "xs muted", style: "margin-top:6px" }, "Every finished step's output is context for the agents after it; the steps it depends on come last, as the most relevant. The bindings above are the values the flow passes explicitly.")));
    return out;
  }
  function outputTab(s) {
    const r = s.result;
    if (!r) return h("div", { class: "empty small" }, s.status === "running" ? "The agent is working. Its output appears here when it finishes." : "This step has not run yet.");
    const prov = r.provenance || {};
    return h("div", { class: "stack" },
      h("p", null, h("span", { class: "chip " + (lampOf(r.status) || "") }, label(r.status)), " ", r.summary || ""),
      r.error ? h("div", { class: "notice bad" }, r.error) : null,
      (prov.degraded_reasons || []).length ? h("div", { class: "notice warn small" }, h("strong", null, "Degraded: "), h("ul", { style: "margin:4px 0 0;padding-left:18px" }, prov.degraded_reasons.map((x) => h("li", null, x)))) : null,
      (r.warnings || []).length ? h("div", { class: "notice warn small" }, h("strong", null, "Warnings: "), h("ul", { style: "margin:4px 0 0;padding-left:18px" }, r.warnings.map((x) => h("li", null, x)))) : null,
      h("div", { class: "row" }, h("h4", null, "Output"), h("span", { class: "right row" },
        h("button", { class: "btn sm", type: "button", onclick: () => copy(JSON.stringify(r.output, null, 2)) }, "Copy JSON"),
        h("button", { class: "btn sm", type: "button", onclick: () => saveBlob(new Blob([JSON.stringify(r.output, null, 2)], { type: "application/json" }), s.id + "-output.json") }, "Download JSON"))),
      r.output && Object.keys(r.output).length ? h("div", { class: "jt" }, jt(r.output, undefined, JSON.stringify(r.output).length < 30000 ? 3 : 1)) : h("p", { class: "muted small" }, "No output fields."));
  }
  function filesTab(s) {
    const arts = (s.result && s.result.artifacts) || [];
    if (!arts.length) return h("div", { class: "empty small" }, "This step produced no files.");
    return h("div", { class: "stack" }, h("div", { class: "files" }, arts.map((a) => {
      const rel = String(a.path || "").replace(/^[^/]+\//, ""), name = rel.split("/").pop();
      return h("div", { class: "f" }, h("div", null, h("div", { class: "strong small" }, name), h("div", { class: "xs muted" }, [a.description, a.mime, a.size ? Math.round(a.size / 102.4) / 10 + " KB" : ""].filter(Boolean).join(", "))),
        h("div", { class: "row" }, h("button", { class: "btn sm", type: "button", onclick: () => preview(rel, a.mime, name) }, "Preview"), h("button", { class: "btn sm", type: "button", onclick: () => download(rel, name) }, "Download")));
    })), h("div", { class: "preview", id: "preview" }));
  }
  function checksTab(s) {
    const cs = (s.result && s.result.checks) || [];
    if (!cs.length) return h("div", { class: "empty small" }, "No checks recorded for this step.");
    const order = { fail: 0, warn: 1, info: 2, pass: 3 };
    return h("div", { class: "tw" }, h("table", null, h("thead", null, h("tr", null, h("th", null, "Result"), h("th", null, "Rule"), h("th", null, "Finding"), h("th", null, "Where"))),
      h("tbody", null, cs.slice().sort((a, b) => (order[a.status] ?? 9) - (order[b.status] ?? 9)).map((c) => h("tr", null,
        h("td", null, h("span", { class: "chip " + (c.status === "pass" ? "good" : c.status === "fail" ? "bad" : c.status === "warn" ? "warn" : "") }, c.status), c.severity && c.status !== "pass" ? h("div", { class: "xs muted" }, c.severity) : null),
        h("td", null, h("code", null, c.rule), c.ref ? h("div", { class: "xs muted" }, c.ref) : null), h("td", { class: "small" }, c.message), h("td", { class: "xs muted" }, c.target || ""))))));
  }
  function modelTab(s) {
    const r = s.result;
    if (!r) return h("div", { class: "empty small" }, s.activity ? "Now: " + s.activity.call + " on " + s.activity.model + (s.activity.work ? " (" + s.activity.work + ")" : "") : "This step has not run yet.");
    const m = r.metrics || {}, p = r.provenance || {}, routes = p.model_routes || [];
    return h("div", { class: "stack" },
      h("div", { class: "figures" }, h("div", { class: "figure" }, h("div", { class: "v" }, m.llm_calls || 0), h("div", { class: "l" }, "model calls")),
        h("div", { class: "figure" }, h("div", { class: "v" }, num(m.input_tokens)), h("div", { class: "l" }, "tokens in")),
        h("div", { class: "figure" }, h("div", { class: "v" }, num(m.output_tokens)), h("div", { class: "l" }, "tokens out")),
        h("div", { class: "figure" }, h("div", { class: "v" }, dur(m.duration_ms)), h("div", { class: "l" }, "agent time")),
        h("div", { class: "figure" }, h("div", { class: "v" }, money(m.cost_usd || 0, m.unpriced_calls)), h("div", { class: "l" }, "cost" + (m.unpriced_calls ? ", " + m.unpriced_calls + " call(s) unpriced" : "")))),
      m.models && Object.keys(m.models).length ? h("div", { class: "tw" }, h("table", null, h("thead", null, h("tr", null, h("th", null, "Model"), h("th", { class: "num" }, "Calls"), h("th", { class: "num" }, "Tokens in"), h("th", { class: "num" }, "Tokens out"), h("th", { class: "num" }, "Cost"))),
        h("tbody", null, Object.entries(m.models).map(([name, x]) => h("tr", null, h("td", null, h("code", null, name)), h("td", { class: "num" }, x.calls), h("td", { class: "num" }, num(x.input_tokens)), h("td", { class: "num" }, num(x.output_tokens)), h("td", { class: "num" }, x.unpriced ? h("span", { class: "chip warn", title: "Not in the price table (config/settings.yaml, llm.pricing)" }, "unpriced") : money(x.cost_usd))))))) : null,
      routes.length ? h("div", { class: "tw" }, h("table", null, h("thead", null, h("tr", null, h("th", null, "Call"), h("th", null, "Kind of work"), h("th", null, "Model"), h("th", null, "Why"))),
        h("tbody", null, routes.map((x) => h("tr", null, h("td", null, h("code", null, x.step)), h("td", { class: "small" }, (x.work || "") + (x.effort ? ", effort " + x.effort : "")),
          h("td", { class: "small" }, h("code", null, x.model || x.tier), h("div", { class: "xs muted" }, "tier " + x.tier)), h("td", { class: "xs muted" }, x.why || "")))))) :
        h("p", { class: "muted small" }, p.runner ? "Served by the " + p.runner + " runner" + (p.pack ? " of the " + p.pack + " pack" : "") + "." : "No model calls: this agent's work is code."),
      h("div", { class: "kv" }, h("dt", null, "Provider"), h("dd", null, (p.llm && p.llm.provider) || "—"),
        h("dt", null, "Connectors used"), h("dd", null, (p.connectors || []).length ? h("span", { class: "row", style: "gap:4px" }, p.connectors.map((c) => h("span", { class: "chip" }, c))) : "none"),
        p.runner ? h("dt", null, "Runner") : null, p.runner ? h("dd", null, p.runner + (p.action ? ", action " + p.action : "")) : null));
  }
  function actionsTab(s) {
    const acts = (s.result && s.result.pending_actions) || [], v = R.view, open = !["completed", "completed_with_errors", "failed"].includes(v.status);
    if (!acts.length) return h("div", { class: "empty small" }, "This step proposed no actions.");
    return h("div", { class: "tw" }, h("table", null, h("thead", null, h("tr", null, h("th", null, "Action"), h("th", null, "What it does"), h("th", null, "Status"), h("th", null, ""))),
      h("tbody", null, acts.map((a) => h("tr", null, h("td", null, h("code", null, a.kind), a.mandatory ? h("div", { class: "xs", style: "color:var(--shu)" }, "always needs a person") : null),
        h("td", { class: "small" }, a.summary, a.result && Object.keys(a.result).length ? h("div", { class: "jt", style: "margin-top:4px" }, jt(a.result, "result", false)) : null),
        h("td", null, h("span", { class: "chip " + (a.status === "done" ? "good" : a.status === "pending" ? "warn" : a.status === "failed" ? "bad" : "") }, a.status)),
        h("td", null, a.status === "pending" && open ? h("div", { class: "row" }, h("button", { class: "btn sm primary", type: "button", onclick: () => decide(v.run_id, { [s.id + "/" + a.id]: "approve" }, afterDecision) }, "Approve"),
          h("button", { class: "btn sm", type: "button", onclick: () => decide(v.run_id, { [s.id + "/" + a.id]: "reject" }, afterDecision) }, "Reject")) : null))))));
  }
  function activityTab(s) {
    const t = (s.result || {}).timing || {}, a = ts(t.started_at || s.started_at), b = ts(t.finished_at) || Infinity;
    if (!a) return h("div", { class: "empty small" }, "Nothing recorded yet.");
    return log(R.trace, (e) => e.step === s.id || (e.agent === s.agent && ts(e.ts) >= a - 5 && ts(e.ts) <= b + 5));
  }
  function renderStep() {
    const p = $("#steppanel", R.box); if (!p) return;
    const s = R.view.steps.find((x) => x.id === R.sel);
    if (!s) { clear(p).append(h("div", { class: "empty small", style: "border:0" }, "Select a step in the flow to see what it was given and what it produced.")); return; }
    const sig = JSON.stringify([s, R.stab === "activity" ? R.trace.length : 0]) + "|" + R.stab;
    if (sig === R.stepSig) { $$("[data-since]", p).forEach((el) => el.textContent = dur(Date.now() - ts(el.dataset.since))); return; }
    R.stepSig = sig;
    const r = s.result, a = agent(s.agent), t = (r || {}).timing || {};
    const ms = r && t.started_at && t.finished_at ? ts(t.finished_at) - ts(t.started_at) : null;
    const counts = { files: ((r && r.artifacts) || []).length, checks: ((r && r.checks) || []).length, model: ((r && r.provenance && r.provenance.model_routes) || []).length, actions: ((r && r.pending_actions) || []).length };
    const tabs = [["inputs", "Inputs"], ["output", "Output"], ["files", "Files", counts.files], ["checks", "Checks", counts.checks], ["model", "Model calls", counts.model]]
      .concat(counts.actions ? [["actions", "Actions", counts.actions]] : []).concat([["activity", "Activity"]]);
    if (!R.stab) R.stab = r ? (s.status === "needs_approval" && counts.actions ? "actions" : "output") : "inputs";
    const body = { inputs: inputsTab, output: outputTab, files: filesTab, checks: checksTab, model: modelTab, actions: actionsTab, activity: activityTab }[R.stab] || inputsTab;
    put(clear(p), h("div", { class: "stephead" }, h("span", { class: "bar", style: "background:" + catColor(a.category) }),
      h("div", null, h("div", { class: "row", style: "gap:8px" }, h("h2", null, a.name || s.agent), h("code", { class: "small" }, s.id), h("span", { class: "chip " + (runClass(s.status) || lampOf(s.status)) }, s.status === "running" ? h("span", { class: "lamp run" }) : null, label(s.status)),
          ms !== null ? h("span", { class: "muted small" }, "took " + dur(ms) + (stepCost(s).cost || stepCost(s).unpriced ? ", " + money(stepCost(s).cost, stepCost(s).unpriced) : "")) : s.status === "running" ? h("span", { class: "muted small" }, "running for ", since(s.started_at)) : null,
          D.byId[s.agent] ? h("a", { class: "small right", href: "#/agents/" + encodeURIComponent(s.agent) }, "About this agent") : null),
        s.purpose ? h("p", { class: "small muted", style: "margin-top:4px" }, s.purpose) : null,
        (s.needs || []).length ? h("p", { class: "xs muted", style: "margin-top:4px" }, "Runs after: ", s.needs.map((d, i) => [i ? ", " : "", h("a", { href: "#", onclick: (e) => { e.preventDefault(); selectStep(d); } }, d)])) : null,
        s.activity && s.status === "running" ? h("p", { class: "small", style: "margin-top:4px;color:var(--ai)" }, "Now: " + (s.activity.call || "") + " on " + (s.activity.model || "") + (s.activity.work ? " (" + s.activity.work + ")" : "")) : null)),
      h("div", { class: "tabs", role: "tablist", style: "margin-top:12px" }, tabs.map(([id, text, n]) => h("button", { type: "button", role: "tab", "aria-selected": String(R.stab === id), onclick: () => { R.stab = id; R.stepSig = ""; renderStep(); } }, text, n ? h("span", { class: "n" }, n) : null))),
      body(s));
  }
  function saveBlob(blob, name) { const u = URL.createObjectURL(blob); const a = h("a", { href: u, download: name }); document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(u), 4000); }
  async function fetchFile(rel, runId) {
    const headers = {}; const t = tok(); if (t) headers.Authorization = "Bearer " + t;
    const res = await fetch(BASE + "/runs/" + encodeURIComponent(runId || R.id) + "/artifacts/" + rel.split("/").map(encodeURIComponent).join("/"), { headers });
    if (!res.ok) throw new Error("HTTP " + res.status);
    return res.blob();
  }
  async function download(rel, name, runId) { try { saveBlob(await fetchFile(rel, runId), name || rel.split("/").pop()); } catch (e) { toast("Download failed: " + e.message, 5000); } }
  async function preview(rel, mime, name, runId, boxEl) {
    const box = boxEl || $("#preview"); if (!box) return;
    clear(box).append(h("p", { class: "muted small" }, "Loading " + name + "…"));
    try {
      const blob = await fetchFile(rel, runId), type = mime || blob.type || "";
      clear(box).append(h("div", { class: "row", style: "margin:6px 0" }, h("strong", { class: "small" }, name), h("button", { class: "btn sm ghost right", type: "button", onclick: () => clear(box) }, "Close preview")));
      if (/html/.test(type) || /\.html?$/.test(name)) { const f = h("iframe", { sandbox: "", title: name, referrerpolicy: "no-referrer" }); f.srcdoc = await blob.text(); box.append(f); }
      else if (/^image\//.test(type)) { box.append(h("img", { src: URL.createObjectURL(blob), alt: name })); }
      else if (/json/.test(type) || /\.json$/.test(name)) { const txt = await blob.text(); try { box.append(h("div", { class: "jt" }, jt(JSON.parse(txt), undefined, true))); } catch (e) { box.append(h("pre", null, txt.slice(0, 200000))); } }
      else if (/^text\/|xml|csv|markdown|mjml/.test(type) || /\.(md|txt|csv|tmx|xml|mjml|vtt|srt|sql|yaml|yml|js|css)$/.test(name)) box.append(h("pre", null, (await blob.text()).slice(0, 200000)));
      else box.append(h("p", { class: "muted small" }, "No preview for this kind of file: download it instead."));
    } catch (e) { clear(box).append(h("div", { class: "notice bad" }, "Could not open the file: " + e.message)); }
  }

  // ------------------------------------------------------------------------------------------------ agents
  const typicalCost = (id) => { const c = D.stats && D.stats.agent_costs && D.stats.agent_costs[id]; return c && c.runs ? c : null; };
  function agentTile(a) {
    const p = packTag(a);
    return h("a", { class: "tile", href: "#/agents/" + encodeURIComponent(a.id) },
      h("span", { class: "stripe", style: "background:" + catColor(a.category) }),
      h("span", null, h("span", { class: "nm" }, h("span", { class: "t" }, a.name), p ? h("span", { class: "tag", title: a.pack_name }, p) : null),
        h("span", { class: "sm" }, a.summary), h("span", { class: "ft" }, h("span", null, a.kind === "deterministic" ? "code only" : a.kind === "llm" ? "model with guardrails" : "code and model"), (a.connectors || []).length ? h("span", null, ", " + a.connectors.join(", ")) : null,
          typicalCost(a.id) ? h("span", { class: "right", title: "the mean of its last " + typicalCost(a.id).runs + " run(s)" }, "about " + money(typicalCost(a.id).avg_cost_usd) + " a run") : null)));
  }
  const AGENT_CACHE = {};
  function agentDetails(id) { return AGENT_CACHE[id] || (AGENT_CACHE[id] = api("/agents/" + encodeURIComponent(id)).catch((e) => { delete AGENT_CACHE[id]; throw e; })); }
  // fields for an agent's input contract; typed values go into `values` as the person types
  function schemaFields(props, required, values, onChange) {
    const req = new Set(required || []);
    const kind = (p) => { const t = Array.isArray(p.type) ? p.type.find((x) => x !== "null") : p.type; return t || (p.anyOf ? (p.anyOf.find((x) => x.type && x.type !== "null") || {}).type : "string") || "string"; };
    const long = (k, p) => p.ui === "textarea" || (p.json_schema_extra && p.json_schema_extra.ui === "textarea") || /text|overview|brief|body|content|description|notes|markdown/.test(k);
    return Object.entries(props).filter(([k]) => k !== "context").map(([k, p]) => {
      const type = kind(p), init = values[k] !== undefined ? values[k] : p.default;
      const set = (v) => { if (v === undefined || v === "" || (Array.isArray(v) && !v.length)) delete values[k]; else values[k] = v; if (onChange) onChange(); };
      let input;
      if (type === "boolean") { input = h("input", { type: "checkbox", checked: !!init, onchange: (e) => set(e.target.checked) }); if (init !== undefined) values[k] = !!init; }
      else if (type === "array" || type === "object") { input = h("textarea", { style: "min-height:60px", oninput: (e) => { const v = e.target.value.trim(); if (!v) return set(undefined); try { set(JSON.parse(v)); } catch (err) { set(type === "array" ? v.split(",").map((x) => x.trim()).filter(Boolean) : v); } } }); input.value = init === undefined ? "" : JSON.stringify(init, null, 1); }
      else if (long(k, p) && type === "string") { input = h("textarea", { style: "min-height:70px;font-family:var(--font);font-size:13px", oninput: (e) => set(e.target.value.trim() || undefined) }); input.value = init === undefined || init === null ? "" : String(init); }
      else { input = h("input", { type: type === "number" || type === "integer" ? "number" : "text", oninput: (e) => { const v = e.target.value.trim(); set(v === "" ? undefined : (type === "number" || type === "integer") ? Number(v) : v); } }); input.value = init === undefined || init === null ? "" : String(init); }
      return h("label", { class: "f" }, h("span", null, k, req.has(k) ? h("span", { class: "muted" }, " required") : null), p.description ? h("span", { class: "xs muted", style: "font-weight:400" }, String(p.description).slice(0, 140)) : null, input);
    });
  }
  VIEWS.agents = function (c, params, q) {
    if (params[0]) return agentDetail(c, params[0]);
    const F = { cat: q.get("category") || "all", pack: "all", q: "" };
    const cats = new Map(); D.agents.forEach((a) => { if (!cats.has(a.category)) cats.set(a.category, { label: a.category_label, n: 0 }); cats.get(a.category).n++; });
    put(c, h("h1", { class: "page" }, "Agents"), h("p", { class: "page-lead" }, D.agents.length + " agents, grouped by the work they do. Each has a typed contract, a self-test, and runs from the API, MCP, A2A, a flow, or right here."),
      h("div", { class: "row", style: "gap:10px;margin-bottom:14px" }, h("input", { type: "text", placeholder: "Find an agent", "aria-label": "Find an agent", style: "max-width:320px", oninput: (e) => { F.q = e.target.value; draw(); } }),
        h("div", { class: "seg", id: "packseg" }),
        D.operator || !D.me ? h("button", { class: "btn primary right", type: "button", onclick: () => openWorkshop({}), title: "Draft a new agent from a purpose, review it, register it" }, "Make an agent") : null),
      h("div", { class: "seg", id: "catseg", style: "margin-bottom:18px" }), h("div", { id: "agentlist" }));
    const draw = () => {
      put(clear($("#catseg")), h("button", { type: "button", "aria-pressed": String(F.cat === "all"), onclick: () => { F.cat = "all"; draw(); } }, "All", h("span", { class: "n" }, D.agents.length)),
        [...cats.entries()].map(([id, cc]) => h("button", { type: "button", "aria-pressed": String(F.cat === id), onclick: () => { F.cat = id; draw(); } }, h("i", { class: "lamp", style: "background:" + catColor(id) + ";margin-right:6px" }), cc.label, h("span", { class: "n" }, cc.n))));
      put(clear($("#packseg")), h("button", { type: "button", "aria-pressed": String(F.pack === "all"), onclick: () => { F.pack = "all"; draw(); } }, "Every source"),
        h("button", { type: "button", "aria-pressed": String(F.pack === "blueprint"), onclick: () => { F.pack = "blueprint"; draw(); } }, "Marketing blueprint"),
        D.packs.map((p) => h("button", { type: "button", "aria-pressed": String(F.pack === p.id), onclick: () => { F.pack = p.id; draw(); } }, p.name)));
      const qq = F.q.toLowerCase();
      const rows = D.agents.filter((a) => (F.cat === "all" || a.category === F.cat) && (F.pack === "all" || (a.pack || "blueprint") === F.pack) && (!qq || (a.name + " " + a.summary + " " + a.id + " " + (a.keywords || []).join(" ")).toLowerCase().includes(qq)));
      const list = clear($("#agentlist"));
      if (!rows.length) { list.append(h("div", { class: "empty" }, "No agent matches.")); return; }
      let cur = null, grid = null;
      for (const a of rows) {
        if (a.category !== cur) { cur = a.category; list.append(h("h2", { style: "font-size:15px;margin:18px 0 8px;display:flex;align-items:center;gap:8px" }, h("i", { class: "lamp", style: "background:" + catColor(cur) }), a.category_label)); grid = h("div", { class: "tiles" }); list.append(grid); }
        grid.append(agentTile(a));
      }
    };
    draw();
  };
  function agentDetail(c, id) {
    const a0 = D.byId[id];
    if (!a0) { put(c, h("div", { class: "empty" }, "No agent called " + id + ".")); return; }
    put(c, h("div", { class: "empty small" }, "Loading " + a0.name + "…"));
    Promise.all([agentDetails(id), api("/runs?agent=" + encodeURIComponent(id) + "&limit=12").catch(() => [])]).then(([a, history]) => {
      const props = (a.input_schema && a.input_schema.properties) || {};
      const values = Object.assign({}, a.selftest_input || {});
      const fields = schemaFields(props, (a.input_schema && a.input_schema.required) || [], values);
      const out = h("div", { id: "agent-run-out" });
      async function runNow(btn) {
        const inputs = Object.assign({}, values);
        btn.disabled = true; btn.textContent = "Running…"; clear(out).append(h("p", { class: "muted small" }, "Running " + a.name + "…"));
        try {
          const r = await api("/agents/" + encodeURIComponent(id) + "/run", { body: { inputs } });
          clear(out).append(h("div", { class: "panel stack", style: "margin-top:10px" },
            h("div", { class: "row" }, h("span", { class: "chip " + (lampOf(r.status) || "") }, label(r.status)), h("span", null, r.summary || ""), r.run_id ? h("a", { class: "right small", href: "#/runs/" + encodeURIComponent(r.run_id) }, "Open as a run") : null),
            r.error ? h("div", { class: "notice bad" }, r.error) : null,
            (r.provenance && r.provenance.degraded_reasons || []).length ? h("div", { class: "notice warn small" }, r.provenance.degraded_reasons.join("; ")) : null,
            (r.artifacts || []).length ? h("div", { class: "files" }, r.artifacts.map((f) => { const rel = String(f.path || "").replace(/^[^/]+\//, ""), name = rel.split("/").pop(); return h("div", { class: "f" }, h("div", null, h("div", { class: "strong small" }, name), h("div", { class: "xs muted" }, f.description || f.mime)), h("div", { class: "row" }, h("button", { class: "btn sm", type: "button", onclick: () => preview(rel, f.mime, name, r.run_id, $("#agent-preview")) }, "Preview"), h("button", { class: "btn sm", type: "button", onclick: () => download(rel, name, r.run_id) }, "Download"))); })) : null,
            h("div", { class: "preview", id: "agent-preview" }),
            (r.pending_actions || []).length ? h("div", { class: "notice warn small" }, r.pending_actions.length + " action(s) proposed and held: " + r.pending_actions.map((x) => x.summary).join("; ") + ". Approve from the run page.") : null,
            h("div", { class: "jt" }, jt(r.output || {}, "output", 2))));
          refreshLive();
        } catch (e) { clear(out).append(h("div", { class: "notice bad" }, "The run failed: " + e.message)); }
        finally { btn.disabled = false; btn.textContent = "Run " + a.name; }
      }
      const runBtn = h("button", { class: "btn primary", type: "button", onclick: (e) => runNow(e.currentTarget) }, "Run " + a.name);
      clear(c);
      put(c, h("p", { class: "small" }, h("a", { href: "#/agents" }, "Agents"), h("span", { class: "muted" }, " › " + a.category_label)),
        h("div", { class: "detail-head" }, h("span", { class: "bar", style: "background:" + catColor(a.category) }),
          h("div", null, h("div", { class: "row", style: "gap:8px" }, h("h1", { class: "page", style: "margin:0" }, a.name), packTag(a) ? h("span", { class: "tag" }, packTag(a)) : null, h("span", { class: "chip" }, a.kind === "deterministic" ? "code only" : a.kind === "llm" ? "model with guardrails" : "code and model"), h("span", { class: "chip" }, "v" + (a.version || "1"))),
            h("p", { class: "page-lead", style: "margin:6px 0 0" }, a.description || a.summary),
            a.pack === "made" ? h("p", { class: "small muted" }, "Made in this layer's workshop: it runs on the model with the skill below, and the planner picks it like any other agent.") : null)),
        h("div", { class: "cols", style: "margin-top:20px" },
          h("div", { class: "stack-lg" },
            h("section", { class: "panel" }, h("h2", { style: "font-size:16px;margin-bottom:10px" }, "Run it here"),
              h("p", { class: "small muted", style: "margin-bottom:10px" }, "The fields are the agent's contract, filled with its self-test values. Anything that publishes or sends is held for approval."),
              h("div", { class: "form-grid" }, fields),
              h("div", { class: "row", style: "margin-top:12px" }, runBtn, h("a", { class: "btn", href: "#/launch?agent=" + encodeURIComponent(id) }, "Open in Launch"), h("a", { class: "btn ghost", href: BASE + "/shop/designer", target: "_blank", rel: "noopener" }, "Add to a flow"),
                a.pack === "made" && (D.operator || !D.me) ? h("button", { class: "btn ghost right", type: "button", style: "color:var(--shu)", onclick: async () => { if (!window.confirm("Remove " + a.name + " from the catalog? Its earlier runs keep their records.")) return; try { await api("/agents/made/" + encodeURIComponent(id), { method: "DELETE" }); await loadCatalog(); toast(a.name + " removed"); navigate("#/agents"); } catch (e) { toast("Could not remove: " + e.message, 6000); } } }, "Remove this agent") : null), out),
            h("section", null, h("h2", { style: "font-size:16px;margin-bottom:8px" }, "History", h("span", { class: "muted", style: "font-size:13px;font-weight:400;margin-left:8px" }, history.length ? "runs that used this agent" : "")),
              history.length ? recentRuns(history) : h("p", { class: "muted small" }, "No run has used this agent yet."), history.length >= 12 ? h("p", { class: "small", style: "margin-top:8px" }, h("a", { href: "#/runs?agent=" + encodeURIComponent(id) }, "All runs with this agent")) : null)),
          h("div", { class: "stack-lg" },
            h("section", null, h("h2", { style: "font-size:16px;margin-bottom:8px" }, "Contract"), h("dl", { class: "kv" },
              h("dt", null, "Produces"), h("dd", null, (a.produces || []).length ? a.produces.join(", ") : "structured output"),
              h("dt", null, "Output fields"), h("dd", null, Object.keys((a.output_schema && a.output_schema.properties) || {}).map((k) => h("code", null, k))),
              h("dt", null, "Connectors"), h("dd", null, (a.connectors || []).length ? a.connectors.join(", ") : "none"),
              h("dt", null, "Skills"), h("dd", null, (a.skills || []).length ? a.skills.join(", ") : "none"),
              h("dt", null, "Side effects"), h("dd", null, (a.side_effects || []).length ? a.side_effects.join(", ") + " (held for approval)" : "none"),
              h("dt", null, "Model tier"), h("dd", null, a.tier || "balanced"),
              h("dt", null, "Typical cost"), h("dd", null, typicalCost(id) ? "about " + money(typicalCost(id).avg_cost_usd) + " a run, the mean of its last " + typicalCost(id).runs + " run(s)" : "no priced run yet"),
              a.feeds && a.feeds.length ? h("dt", null, "Feeds") : null, a.feeds && a.feeds.length ? h("dd", null, a.feeds.map((f, i) => [i ? ", " : "", h("a", { href: "#/agents/" + f }, agent(f).name)])) : null,
              a.pack_name ? h("dt", null, "Source") : null, a.pack_name ? h("dd", null, a.pack_name + (a.accelerator ? ", " + a.accelerator : "")) : null,
              (a.actions || []).length ? h("dt", null, "Actions") : null, (a.actions || []).length ? h("dd", null, a.actions.map((x) => h("div", { class: "small" }, h("code", null, x.id), x.title ? " " + x.title : ""))) : null)),
            (a.use_cases || []).length ? h("section", null, h("h2", { style: "font-size:16px;margin-bottom:8px" }, "Typical asks"), h("ul", { class: "small", style: "margin:0;padding-left:18px" }, a.use_cases.map((u) => h("li", { style: "margin-bottom:4px" }, u)))) : null,
            a.independence ? h("section", null, h("h2", { style: "font-size:16px;margin-bottom:8px" }, "How it stays vendor-neutral"), h("p", { class: "small" }, a.independence)) : null,
            h("section", null, h("h2", { style: "font-size:16px;margin-bottom:8px" }, "Call it from your tools"), h("pre", { class: "xs" }, "POST " + BASE + "/agents/" + id + "/run\nMCP tool: run_" + id + "\nA2A skill: " + id)))));
    }).catch((e) => { clear(c).append(h("div", { class: "notice bad" }, "Could not load the agent: " + e.message)); });
  }

  // ------------------------------------------------------------------------------------------------ flows
  VIEWS.flows = function (c, params) {
    if (params[0]) return flowDetail(c, params[0]);
    put(c, h("h1", { class: "page" }, "Flows"), h("p", { class: "page-lead" }, "Several agents wired into one workflow: steps in stages, each stage's steps running in parallel. Run a flow as a whole, rent it as one tool, or open it in the designer to change it."));
    if (!D.flows.length) { put(c, h("div", { class: "empty" }, "No flows you can run. The operator publishes flows from the designer.")); return; }
    const groups = new Map();
    D.flows.forEach((f) => { const g = f.pack_name || (f.source === "custom" ? "Designed here" : "Built in"); if (!groups.has(g)) groups.set(g, []); groups.get(g).push(f); });
    groups.forEach((rows, g) => put(c, h("section", { class: "section" }, h("h2", null, g, h("span", { class: "muted" }, rows.length + (rows.length === 1 ? " flow" : " flows"))),
      rows.map((f) => h("a", { class: "flowrow", href: "#/flows/" + encodeURIComponent(f.id) }, h("div", null, h("div", { class: "nm" }, f.name), h("div", { class: "ds" }, f.description || ""), stagesOf(f.stages || [])),
        h("div", { class: "small muted", style: "text-align:right" }, f.agents.length + " agents", h("br"), f.stages.length + " stages", f.status === "draft" ? h("div", { class: "chip warn", style: "margin-top:6px" }, "draft") : null))))));
  };
  function flowDetail(c, id) {
    put(c, h("div", { class: "empty small" }, "Loading the flow…"));
    Promise.all([api("/flows/" + encodeURIComponent(id)), api("/runs?label=" + encodeURIComponent(id) + "&limit=12").catch(() => [])]).then(([f, history]) => {
      const steps = f.steps.map((s) => Object.assign({}, s, { status: "planned" }));
      const vals = {};
      const inputs = (f.inputs || []).map((i) => { const el = h("input", { type: "text", placeholder: i.example ? "e.g. " + i.example : "", oninput: (e) => { vals[i.name] = e.target.value; } }); return h("label", { class: "f" }, h("span", null, i.name, i.required ? h("span", { class: "muted" }, " required") : null), h("span", { class: "xs muted", style: "font-weight:400" }, i.description || ""), el); });
      const goal = h("input", { type: "text", placeholder: "Optional goal text that steers the agents" });
      const out = h("div");
      async function run(btn) {
        btn.disabled = true; btn.textContent = "Starting…";
        try {
          const inputs = {}; for (const [k, v] of Object.entries(vals)) if (v.trim()) inputs[k] = v.trim();
          const v = await api("/flows/" + encodeURIComponent(id) + "/run", { body: { inputs, goal: goal.value.trim() || undefined, wait_s: 0 } });
          toast("Run started"); navigate("#/runs/" + encodeURIComponent(v.run_id)); refreshLive();
        } catch (e) { clear(out).append(h("div", { class: "notice bad" }, "Could not start: " + e.message)); btn.disabled = false; btn.textContent = "Run this flow"; }
      }
      clear(c);
      put(c, h("p", { class: "small" }, h("a", { href: "#/flows" }, "Flows"), f.pack_name ? h("span", { class: "muted" }, " › " + f.pack_name) : null),
        h("div", { class: "row", style: "gap:10px" }, h("h1", { class: "page", style: "margin:0" }, f.name), h("span", { class: "chip " + (f.status === "published" ? "good" : "warn") }, f.status), f.price && f.price.monthly ? h("span", { class: "chip" }, "from $" + f.price.monthly + " a month") : null,
          (() => { const priced = history.filter((r) => r.cost_usd > 0); return priced.length ? h("span", { class: "chip", title: "the mean cost of its last " + priced.length + " priced run(s)" }, "about " + money(priced.reduce((a, r) => a + r.cost_usd, 0) / priced.length) + " a run") : null; })()),
        h("p", { class: "page-lead" }, f.description || ""),
        h("div", { class: "cols" },
          h("div", { class: "stack-lg" },
            h("section", null, h("h2", { style: "font-size:16px;margin-bottom:8px" }, steps.length + " steps in " + f.stages.length + " stages"), graph(steps, { planned: true })),
            h("section", { class: "panel" }, h("h2", { style: "font-size:16px;margin-bottom:8px" }, "Run this flow"),
              inputs.length ? h("div", { class: "form-grid" }, inputs) : h("p", { class: "small muted" }, "This flow declares no inputs; the agents work from the goal and their defaults."),
              h("label", { class: "f", style: "margin-top:10px" }, "Goal", goal),
              h("div", { class: "row", style: "margin-top:12px" }, h("button", { class: "btn primary", type: "button", onclick: (e) => run(e.currentTarget) }, "Run this flow"), h("a", { class: "btn", href: BASE + "/shop/designer#flow=" + encodeURIComponent(id), target: "_blank", rel: "noopener" }, "Open in the designer")), out),
            h("section", null, h("h2", { style: "font-size:16px;margin-bottom:8px" }, "History"), history.length ? recentRuns(history) : h("p", { class: "muted small" }, "This flow has not run yet."), history.length >= 12 ? h("p", { class: "small", style: "margin-top:8px" }, h("a", { href: "#/runs?label=" + encodeURIComponent(id) }, "All runs of this flow")) : null)),
          h("div", { class: "stack-lg" },
            h("section", null, h("h2", { style: "font-size:16px;margin-bottom:8px" }, "Steps"), h("div", { class: "stack", style: "gap:6px" }, f.steps.map((s) => { const a = agent(s.agent); return h("div", { class: "steprow" }, h("span", { class: "stripe", style: "background:" + catColor(a.category) }), h("div", { class: "small" }, h("code", null, s.id), " ", h("a", { href: "#/agents/" + encodeURIComponent(s.agent) }, s.agent_name || a.name), s.purpose ? h("div", { class: "xs muted" }, s.purpose) : null, (s.needs || []).length ? h("div", { class: "xs muted" }, "after " + s.needs.join(", ")) : null)); }))),
            h("section", null, h("h2", { style: "font-size:16px;margin-bottom:8px" }, "Call it from your tools"), h("pre", { class: "xs" }, "POST " + BASE + "/flows/" + id + "/run\nMCP tool: " + (f.tool || "run_flow_" + id) + "\nA2A skill: flow:" + id)))));
    }).catch((e) => { clear(c).append(h("div", { class: "notice bad" }, "Could not load the flow: " + e.message)); });
  }

  // ------------------------------------------------------------------------------------------------ health
  VIEWS.health = function (c) {
    put(c, h("h1", { class: "page" }, "Health"), h("p", { class: "page-lead" }, "The model behind the agents, whether each agent answers, and which systems the connectors reach."),
      h("div", { id: "health-body" }, h("div", { class: "empty small" }, "Checking…")));
    const body = $("#health-body");
    Promise.all([api("/health"), api("/console/host").catch(() => null), api("/connectors").catch(() => []), api("/packs").catch(() => [])]).then(([hlt, host, conns, packs]) => {
      clear(body);
      const probeBox = h("div", { id: "probe" }, h("p", { class: "muted small" }, "Loading the last probe…"));
      put(body, h("div", { class: "cols" },
        h("div", { class: "stack-lg" },
          h("section", null, h("h2", { style: "font-size:16px;margin-bottom:8px" }, "Agents"),
            h("div", { class: "figures" }, h("div", { class: "figure" }, h("div", { class: "v" }, hlt.agents_registered, h("small", null, "/ " + hlt.agents_expected)), h("div", { class: "l" }, "blueprint agents registered")), h("div", { class: "figure" }, h("div", { class: "v" }, D.agents.length), h("div", { class: "l" }, "agents in the catalog"))),
            h("div", { class: "row", style: "margin:12px 0" }, h("button", { class: "btn sm", type: "button", onclick: () => probe(true, false) }, "Probe again (offline, no cost)"), h("button", { class: "btn sm", type: "button", onclick: () => { if (window.confirm("Probe every blueprint agent on the live model? That is one or more model calls per agent.")) probe(true, true); } }, "Probe on the live model")),
            probeBox),
          h("section", null, h("h2", { style: "font-size:16px;margin-bottom:8px" }, "Connectors"),
            h("div", { class: "tw" }, h("table", null, h("thead", null, h("tr", null, h("th", null, "Kind"), h("th", null, "Provider in use"), h("th", null, "Reaches"), h("th", null, "Available providers"))),
              h("tbody", null, conns.map((x) => h("tr", null, h("td", null, h("code", null, x.kind)), h("td", null, x.provider), h("td", null, h("span", { class: "chip " + (x.available === false ? "bad" : x.external ? "good" : "") }, x.available === false ? "not configured" : x.external ? "external system" : "local demo data")), h("td", { class: "xs muted" }, (x.providers || []).join(", "))))))))),
        h("div", { class: "stack-lg" },
          h("section", null, h("h2", { style: "font-size:16px;margin-bottom:8px" }, "Model"),
            host ? h("dl", { class: "kv" }, h("dt", null, "Provider"), h("dd", null, host.live ? host.label : "Offline (placeholders)"), Object.entries(host.models || {}).map(([t, m]) => { const rate = hlt.llm && hlt.llm.pricing && hlt.llm.pricing.tiers ? hlt.llm.pricing.tiers[t] : null; return [h("dt", null, t), h("dd", null, h("code", null, m), rate ? h("span", { class: "muted" }, " " + money(rate[0]) + " in, " + money(rate[1]) + " out per million tokens") : h("span", { class: "chip warn", style: "margin-left:6px" }, "not in the price table"))]; }), h("dt", null, "In flight"), h("dd", null, "up to " + host.max_concurrent + " calls at once")) : h("p", { class: "muted small" }, "The console host is not reachable."),
            hlt.llm && hlt.llm.pricing ? h("details", { style: "margin-top:10px" }, h("summary", { class: "small muted", style: "cursor:pointer" }, "Price table (" + hlt.llm.pricing.models.length + " model families, " + hlt.llm.pricing.currency + " per million tokens)"),
              h("div", { class: "tw", style: "margin-top:6px" }, h("table", null, h("thead", null, h("tr", null, h("th", null, "Model id contains"), h("th", { class: "num" }, "Input"), h("th", { class: "num" }, "Output"))), h("tbody", null, hlt.llm.pricing.models.map((x) => h("tr", null, h("td", null, h("code", null, x.model)), h("td", { class: "num" }, money(x.input)), h("td", { class: "num" }, money(x.output))))))),
              h("p", { class: "xs muted", style: "margin-top:6px" }, "A model matches the longest key its id contains. Prompt-cache reads cost " + hlt.llm.pricing.cache_read_factor + "× and writes " + hlt.llm.pricing.cache_write_factor + "× the input rate. Edit config/settings.yaml (llm.pricing) to match your contract; past runs keep the cost recorded at the time.")) : null),
          h("section", null, h("h2", { style: "font-size:16px;margin-bottom:8px" }, "Agent packs"), packs.map((p) => h("div", { class: "small", style: "margin-bottom:8px" }, h("span", { class: "tag" }, p.tag), " ", h("strong", null, p.name), h("div", { class: "muted" }, p.agents + " agents, " + p.flows + " flows"), p.home ? h("div", { class: "muted" }, p.home.connected ? "Connected to " + p.home.name + ": its agents can run at home" : p.home.name + " is not connected: its agents are modelled here (" + p.home.url_env + ")") : h("div", { class: "muted" }, "Agents modelled on this layer's model")))),
          h("section", null, h("h2", { style: "font-size:16px;margin-bottom:8px" }, "Service"), h("dl", { class: "kv" }, h("dt", null, "Version"), h("dd", null, hlt.version), h("dt", null, "Model provider"), h("dd", null, hlt.llm && hlt.llm.provider))))));
      probe(false, false);
      async function probe(refresh, live) {
        clear(probeBox).append(h("p", { class: "muted small" }, live ? "Probing every agent on the live model: a few minutes…" : refresh ? "Probing every agent with the offline model…" : "Loading the last probe…"));
        try {
          const rep = await api("/health/agents?offline=" + (!live) + "&refresh=" + refresh);
          const rows = rep.agents || rep.results || [];
          clear(probeBox).append(h("p", { class: "small", style: "margin-bottom:8px" }, h("strong", null, (rep.live_count ?? rows.filter((r) => r.live).length) + " of " + rows.length + " live"), h("span", { class: "muted" }, " on the " + (rep.mode || "offline") + " probe" + (rep.checked_at ? ", " + ago(rep.checked_at) : ""))),
            h("div", { class: "tiles" }, rows.map((r) => { const a = agent(r.agent_id); return h("a", { class: "tile", href: "#/agents/" + encodeURIComponent(r.agent_id), style: "padding:8px 10px" }, h("span", { class: "stripe", style: "background:" + catColor(a.category) }),
              h("span", null, h("span", { class: "nm" }, h("span", { class: "lamp " + (r.live ? (r.status === "ok" ? "good" : "warn") : "bad") }), h("span", { class: "t" }, a.name)), h("span", { class: "xs muted", style: "display:block" }, (r.live ? label(r.status) : "failed") + (r.duration_ms ? ", " + dur(r.duration_ms) : "") + (r.error ? ": " + r.error : "")))); })));
        } catch (e) { clear(probeBox).append(h("div", { class: "notice bad" }, "The probe failed: " + e.message)); }
      }
    }).catch((e) => { clear(body).append(h("div", { class: "notice bad" }, "Could not read the health: " + e.message)); });
  };

  // ------------------------------------------------------------------------------------------------ customers (operator) and account (customer)
  VIEWS.customers = function (c) {
    if (!D.operator) { put(c, h("div", { class: "empty" }, "Customers are managed by the operator.")); return; }
    put(c, h("h1", { class: "page" }, "Customers"), h("p", { class: "page-lead" }, "Who rents what, and what they used. Keys, subscriptions and policies are edited in the admin console."), h("div", { id: "cust" }, h("div", { class: "empty small" }, "Loading…")));
    Promise.all([api("/shop/admin/api/overview"), api("/shop/admin/api/customers"), api("/shop/admin/api/requests").catch(() => [])]).then(([ov, customers, reqs]) => {
      const box = clear($("#cust"));
      const usageBy = {}; (ov.usage.lines || []).forEach((l) => { usageBy[l.customer_id] = usageBy[l.customer_id] || { runs: 0, tokens: 0, cost: 0 }; usageBy[l.customer_id].runs += l.runs; usageBy[l.customer_id].tokens += (l.input_tokens || 0) + (l.output_tokens || 0); usageBy[l.customer_id].cost += l.cost_usd || 0; });
      put(box, h("div", { class: "figures", style: "margin-bottom:18px" }, h("div", { class: "figure" }, h("div", { class: "v" }, ov.customers), h("div", { class: "l" }, "customers, " + ov.active + " active")), h("div", { class: "figure" }, h("div", { class: "v" }, num(ov.usage.totals.runs)), h("div", { class: "l" }, "metered agent runs in " + ov.usage.month)), h("div", { class: "figure" }, h("div", { class: "v" }, num((ov.usage.totals.input_tokens || 0) + (ov.usage.totals.output_tokens || 0))), h("div", { class: "l" }, "tokens metered")), h("div", { class: "figure" }, h("div", { class: "v" }, money(ov.usage.totals.cost_usd || 0)), h("div", { class: "l" }, "model spend metered in " + ov.usage.month)), h("div", { class: "figure" }, h("div", { class: "v" }, ov.requests_new), h("div", { class: "l" }, "new access requests"))),
        h("div", { class: "tw" }, h("table", null, h("thead", null, h("tr", null, h("th", null, "Customer"), h("th", null, "Run mode"), h("th", null, "Status"), h("th", { class: "num" }, "Runs this month"), h("th", { class: "num" }, "Tokens"), h("th", { class: "num" }, "Spend"), h("th", null, ""))),
          h("tbody", null, customers.map((cu) => h("tr", null, h("td", null, h("strong", null, cu.name), h("div", { class: "xs muted mono" }, cu.id)), h("td", { class: "small" }, cu.run_mode_number + ". " + cu.run_mode.replace(/_/g, " ")), h("td", null, h("span", { class: "chip " + (cu.status === "active" ? "good" : "bad") }, cu.status)),
            h("td", { class: "num" }, num((usageBy[cu.id] || {}).runs)), h("td", { class: "num" }, num((usageBy[cu.id] || {}).tokens)), h("td", { class: "num" }, money((usageBy[cu.id] || {}).cost || 0)), h("td", null, h("a", { class: "small", href: "#/runs?q=" + encodeURIComponent(cu.id) }, "Runs"), " ", h("a", { class: "small", href: BASE + "/shop/admin", target: "_blank", rel: "noopener" }, "Manage"))))))),
        reqs.length ? h("section", { class: "section" }, h("h2", null, "Access requests", h("span", { class: "muted" }, reqs.length)), h("div", { class: "tw" }, h("table", null, h("thead", null, h("tr", null, h("th", null, "When"), h("th", null, "Who"), h("th", null, "Wants"), h("th", null, "Status"))), h("tbody", null, reqs.slice(0, 20).map((r) => h("tr", null, h("td", { class: "small" }, when(r.created_at)), h("td", { class: "small" }, r.name, h("div", { class: "xs muted" }, r.email + (r.company ? ", " + r.company : ""))), h("td", { class: "small" }, (r.items || []).join(", ") || r.message || ""), h("td", null, h("span", { class: "chip" }, r.status)))))))) : null);
    }).catch((e) => { clear($("#cust")).append(h("div", { class: "notice bad" }, "Could not load: " + e.message)); });
  };
  VIEWS.account = function (c) {
    if (!D.me) { put(c, h("div", { class: "empty" }, "Sign in with a customer key to see an account.")); return; }
    const me = D.me, u = me.usage || { totals: {} };
    put(c, h("h1", { class: "page" }, me.customer.name), h("p", { class: "page-lead" }, "Run mode " + me.customer.run_mode_number + ", " + me.customer.run_mode.replace(/_/g, " ") + ". Customer since " + when(me.customer.created_at) + "."),
      h("div", { class: "figures", style: "margin-bottom:18px" }, h("div", { class: "figure" }, h("div", { class: "v" }, me.entitled_agents.length), h("div", { class: "l" }, "agents rented")), h("div", { class: "figure" }, h("div", { class: "v" }, me.entitled_flows.length), h("div", { class: "l" }, "flows rented")), h("div", { class: "figure" }, h("div", { class: "v" }, num(u.totals.runs)), h("div", { class: "l" }, "agent runs in " + u.month)), h("div", { class: "figure" }, h("div", { class: "v" }, num((u.totals.input_tokens || 0) + (u.totals.output_tokens || 0))), h("div", { class: "l" }, "tokens")), h("div", { class: "figure" }, h("div", { class: "v" }, money(u.totals.cost_usd || 0)), h("div", { class: "l" }, "model spend in " + u.month))),
      h("div", { class: "cols" },
        h("div", { class: "stack-lg" },
          h("section", null, h("h2", { style: "font-size:16px;margin-bottom:8px" }, "Your agents"), me.entitled_agents.length ? h("div", { class: "row", style: "gap:6px" }, me.entitled_agents.map((a) => h("a", { class: "chip", href: "#/agents/" + a }, agent(a).name))) : h("p", { class: "muted small" }, "No agents yet.")),
          h("section", null, h("h2", { style: "font-size:16px;margin-bottom:8px" }, "Your flows"), me.flows.length ? me.flows.map((f) => h("a", { class: "flowrow", href: "#/flows/" + encodeURIComponent(f.id) }, h("div", null, h("div", { class: "nm" }, f.name), stagesOf(f.stages || [])), h("div", { class: "small muted" }, f.agents.length + " agents"))) : h("p", { class: "muted small" }, "No flows yet."))),
        h("div", { class: "stack-lg" },
          h("section", null, h("h2", { style: "font-size:16px;margin-bottom:8px" }, "Connect your tools"), h("pre", { class: "xs" }, "REST  " + (me.connection.rest && me.connection.rest.base_url) + "\nMCP   " + (me.connection.mcp && me.connection.mcp.url) + "\nA2A   " + (me.connection.a2a && (me.connection.a2a.card || me.connection.a2a.url))), h("p", { class: "small" }, h("a", { href: BASE + "/shop/account", target: "_blank", rel: "noopener" }, "Keys, policy and the full connection details"))),
          h("section", null, h("h2", { style: "font-size:16px;margin-bottom:8px" }, "Your policy"), h("div", { class: "jt" }, jt(me.policy || {}, undefined, 1))))));
  };

  // ------------------------------------------------------------------------------------------------ the workshop: make an agent
  const TIERS = [["fast", "fast: short, mechanical work"], ["balanced", "balanced: writing and reporting"], ["pro", "pro: analysis and design"], ["complex", "complex: code and experiment design"]];
  function openWorkshop(opts) {
    opts = opts || {};
    const old = $("#workshop"); if (old) old.remove();
    const d = h("dialog", { class: "pal workshop", id: "workshop", "aria-label": "Make an agent" });
    document.body.append(d);
    const body = h("div", { class: "wbody" });
    const purpose = h("textarea", { placeholder: "What should the agent do? Say what it is given and what it must produce.", style: "min-height:84px;font-family:var(--font);font-size:14px" });
    purpose.value = opts.purpose || "";
    const nameIn = h("input", { type: "text", placeholder: "A name, if you have one" }); nameIn.value = opts.name || "";
    const draftBtn = h("button", { class: "btn primary", type: "button", onclick: draft }, "Draft the agent");
    const stepOne = h("div", { class: "stack" },
      h("p", { class: "small muted" }, "The model drafts the agent's contract (what it takes, what it returns) and its skill (how it works, what it checks). You review every field before it is registered. Once registered it is an agent of this layer: in the catalog, callable from REST, MCP, A2A and flows, metered, and picked by the planner for later goals."),
      h("label", { class: "f" }, "Purpose", purpose), h("label", { class: "f" }, "Name", nameIn),
      h("div", { class: "row" }, draftBtn, h("button", { class: "btn ghost", type: "button", onclick: () => d.close() }, "Cancel")));
    put(d, h("div", { class: "whead" }, h("h2", null, "Make an agent"), opts.goal ? h("p", { class: "small muted" }, "For the goal: " + opts.goal) : null), put(body, stepOne));
    d.addEventListener("click", (e) => { if (e.target === d) d.close(); });
    d.showModal(); purpose.focus();
    async function draft() {
      if (!purpose.value.trim()) { purpose.focus(); return; }
      draftBtn.disabled = true; draftBtn.textContent = "Drafting…";
      try {
        const r = await api("/agents/draft", { body: { purpose: purpose.value.trim(), goal: opts.goal || "", name: nameIn.value.trim() } });
        const dr = r.draft;
        if (opts.id && !D.byId[opts.id]) dr.id = opts.id;
        if (opts.category) dr.category = opts.category;
        clear(body).append(form(dr, r.metrics));
      } catch (e) { toast("Could not draft: " + e.message, 7000); }
      finally { draftBtn.disabled = false; draftBtn.textContent = "Draft the agent"; }
    }
    function lines(arr) { return (arr || []).join("\n"); }
    function unlines(text) { return String(text || "").split("\n").map((x) => x.trim()).filter(Boolean); }
    function csv(text) { return String(text || "").split(",").map((x) => x.trim()).filter(Boolean); }
    function form(dr, metrics) {
      const cats = [...new Map(D.agents.map((a) => [a.category, a.category_label])).entries()];
      const F = {};
      const field = (key, label, el, hint) => { F[key] = el; return h("label", { class: "f" }, h("span", null, label), hint ? h("span", { class: "xs muted", style: "font-weight:400" }, hint) : null, el); };
      const text = (v) => { const i = h("input", { type: "text" }); i.value = v === undefined || v === null ? "" : v; return i; };
      const area = (v, min) => { const t = h("textarea", { style: "min-height:" + (min || 60) + "px;font-family:var(--font);font-size:13px" }); t.value = v || ""; return t; };
      const inputsText = (dr.inputs || []).map((i) => [i.name, i.type || "text", i.required ? "required" : "optional", i.description || "", i.example === undefined || i.example === null ? "" : (typeof i.example === "string" ? i.example : JSON.stringify(i.example))].join(" | ")).join("\n");
      const catSel = h("select", null, cats.map(([id, label]) => h("option", { value: id }, label))); catSel.value = dr.category;
      const tierSel = h("select", null, TIERS.map(([id, label]) => h("option", { value: id }, label))); tierSel.value = dr.tier || "balanced";
      const regBtn = h("button", { class: "btn primary", type: "button", onclick: register }, "Register the agent");
      const out = h("div");
      const wrap = h("div", { class: "stack" },
        h("div", { class: "row", style: "gap:8px" }, h("span", { class: "chip good" }, "Drafted"), metrics && metrics.cost_usd ? h("span", { class: "xs muted" }, "one model call, " + money(metrics.cost_usd)) : h("span", { class: "xs muted" }, "a plain draft: no model is connected"), h("span", { class: "xs muted right" }, "Edit anything before registering")),
        h("div", { class: "form-grid" },
          field("name", "Name", text(dr.name)), field("id", "Id", text(dr.id), "snake_case, unique"),
          field("category", "Category", catSel), field("tier", "Model tier", tierSel)),
        field("summary", "Summary", text(dr.summary), "one sentence: what it does and what it produces"),
        field("description", "Description", area(dr.description, 50)),
        field("system_prompt", "Skill: how it works", area(dr.system_prompt, 150), "the system prompt it runs with"),
        h("div", { class: "form-grid" },
          field("inputs", "Inputs", area(inputsText, 70), "one per line: name | type | required/optional | description | example"),
          field("checklist", "Checklist", area(lines(dr.checklist), 70), "one check per line")),
        h("div", { class: "form-grid" },
          field("output_keys", "Result keys", text((dr.output_keys || []).join(", ")), "comma-separated"), field("produces", "Produces", text((dr.produces || []).join(", ")), "kinds of deliverable, comma-separated"),
          field("keywords", "Keywords", text((dr.keywords || []).join(", ")), "words a goal would use"), field("use_cases", "Typical asks", area(lines(dr.use_cases), 50), "one per line"),
          field("standards", "Standards", area(lines(dr.standards), 50), "one per line"), field("example_request", "Self-test request", text(dr.example_request))),
        h("div", { class: "row" }, regBtn, h("button", { class: "btn", type: "button", onclick: () => clear(body).append(stepOne) }, "Back"), h("button", { class: "btn ghost", type: "button", onclick: () => d.close() }, "Cancel")), out);
      return wrap;
      function read() {
        const inputs = unlines(F.inputs.value).map((line) => { const [name, type, req, description, example] = line.split("|").map((x) => (x || "").trim()); let ex = example; try { if (example && /^[\[{"0-9tfn-]/.test(example)) ex = JSON.parse(example); } catch (e) { ex = example; } return { name, type: type || "text", required: /^req/i.test(req || ""), description: description || "", example: ex === "" ? null : ex }; }).filter((i) => i.name);
        return { id: F.id.value.trim(), name: F.name.value.trim(), category: F.category.value, tier: F.tier.value, summary: F.summary.value.trim(), description: F.description.value.trim(),
                 keywords: csv(F.keywords.value), inputs, output_keys: csv(F.output_keys.value), produces: csv(F.produces.value), system_prompt: F.system_prompt.value.trim(),
                 checklist: unlines(F.checklist.value), standards: unlines(F.standards.value), use_cases: unlines(F.use_cases.value), example_request: F.example_request.value.trim() };
      }
      async function register() {
        const spec = read();
        if (!spec.name || !spec.summary || !spec.system_prompt) { toast("A name, a summary and the skill are needed"); return; }
        regBtn.disabled = true; regBtn.textContent = "Registering…";
        try {
          const a = await api("/agents/made", { body: spec });
          await loadCatalog();
          renderSpine();
          toast(a.name + " is now in the catalog");
          clear(body).append(h("div", { class: "stack" },
            h("div", { class: "notice good" }, h("strong", null, a.name), " is registered: it is in the catalog, callable from REST (", h("code", null, "POST /agents/" + a.id + "/run"), "), MCP (", h("code", null, "run_" + a.id), ") and A2A, can be put in a flow, and the planner will pick it for goals that need it. It runs on the model with the skill you reviewed; its first self-test is one click away."),
            h("div", { class: "row" }, h("a", { class: "btn primary", href: "#/agents/" + encodeURIComponent(a.id), onclick: () => d.close() }, "Open the agent"),
              opts.onMade ? h("button", { class: "btn", type: "button", onclick: () => { d.close(); opts.onMade(a); } }, "Re-assess the goal with it") : null,
              h("button", { class: "btn ghost", type: "button", onclick: () => d.close() }, "Done"))));
        } catch (e) { clear(out).append(h("div", { class: "notice bad small" }, "Not registered: " + e.message)); }
        finally { regBtn.disabled = false; regBtn.textContent = "Register the agent"; }
      }
    }
  }

  // ------------------------------------------------------------------------------------------------ command palette
  const P = { open: false, q: "", sel: 0, items: [] };
  function openPalette() { const d = $("#pal"); if (d.open) return; P.q = ""; P.sel = 0; renderPalette(); d.showModal(); $("input", d).focus(); }
  function paletteItems(q) {
    const qq = q.trim().toLowerCase(), out = [];
    const sect = (name) => out.push({ group: name });
    if (qq.length > 2 && qq.split(/\s+/).length > 2) { sect("Launch"); out.push({ icon: "launch", t: "Run: “" + q.trim() + "”", m: "the orchestrator plans it", go: () => { L.goal = q.trim(); L.mode = "auto"; L.plan = null; remember(); navigate("#/launch"); } }); }
    const views = NAV.concat(D.operator ? [["customers", "Customers", "customers"]] : D.me ? [["account", "My account", "account"]] : []).filter(([id, t]) => !qq || t.toLowerCase().includes(qq));
    if (views.length) { sect("Go to"); views.forEach(([id, t, ic]) => out.push({ icon: ic, t, go: () => navigate("#/" + id) })); }
    const agents = D.agents.filter((a) => !qq || (a.name + " " + a.id + " " + a.summary).toLowerCase().includes(qq)).slice(0, qq ? 8 : 4);
    if (agents.length) { sect("Agents"); agents.forEach((a) => out.push({ icon: "agents", t: a.name, m: a.category_label + (packTag(a) ? ", " + a.pack_name : ""), go: () => navigate("#/agents/" + encodeURIComponent(a.id)) })); }
    const flows = D.flows.filter((f) => !qq || (f.name + " " + f.id + " " + (f.description || "")).toLowerCase().includes(qq)).slice(0, qq ? 6 : 3);
    if (flows.length) { sect("Flows"); flows.forEach((f) => out.push({ icon: "flows", t: f.name, m: f.agents.length + " agents", go: () => navigate("#/flows/" + encodeURIComponent(f.id)) })); }
    const runs = D.runs.filter((r) => !qq || (r.goal + " " + r.run_id + " " + (r.label || "")).toLowerCase().includes(qq)).slice(0, qq ? 6 : 4);
    if (runs.length) { sect("Runs"); runs.forEach((r) => out.push({ icon: "runs", t: r.goal || r.run_id, m: label(r.status) + ", " + ago(r.created_at), go: () => navigate("#/runs/" + encodeURIComponent(r.run_id)) })); }
    if (qq && D.approvals.length) { const held = D.approvals.filter((a) => (a.summary + " " + a.goal).toLowerCase().includes(qq)).slice(0, 4); if (held.length) { sect("Waiting for approval"); held.forEach((a) => out.push({ icon: "approvals", t: a.summary, m: a.goal, go: () => navigate("#/runs/" + encodeURIComponent(a.run_id) + "/" + encodeURIComponent(a.step)) })); } }
    return out;
  }
  function renderPalette() {
    const d = clear($("#pal"));
    P.items = paletteItems(P.q);
    const selectable = P.items.filter((i) => !i.group);
    if (P.sel >= selectable.length) P.sel = Math.max(0, selectable.length - 1);
    let idx = -1;
    const input = h("input", { type: "text", placeholder: "Search agents, flows and runs, or type a goal to run", "aria-label": "Search", value: P.q,
      oninput: (e) => { P.q = e.target.value; P.sel = 0; renderList(); },
      onkeydown: (e) => { if (e.key === "ArrowDown") { e.preventDefault(); P.sel = Math.min(selectable.length - 1, P.sel + 1); renderList(); } else if (e.key === "ArrowUp") { e.preventDefault(); P.sel = Math.max(0, P.sel - 1); renderList(); } else if (e.key === "Enter") { e.preventDefault(); const it = P.items.filter((i) => !i.group)[P.sel]; if (it) { d.close(); it.go(); } } } });
    const list = h("div", { class: "list" });
    function renderList() {
      P.items = paletteItems(P.q); clear(list); idx = -1;
      const sel = P.items.filter((i) => !i.group);
      if (!sel.length) list.append(h("div", { class: "empty small", style: "border:0" }, "Nothing matches."));
      P.items.forEach((it) => { if (it.group) { list.append(h("div", { class: "grp" }, it.group)); return; } idx++; const i = idx;
        list.append(h("div", { class: "it", role: "option", "aria-selected": String(i === P.sel), onmouseenter: () => { P.sel = i; $$(".it", list).forEach((el, j) => el.setAttribute("aria-selected", String(j === i))); }, onclick: () => { d.close(); it.go(); } },
          icon(it.icon || "arrow"), h("span", { class: "t" }, it.t), h("span", { class: "m" }, it.m || ""))); });
      const cur = $('.it[aria-selected="true"]', list); if (cur) cur.scrollIntoView({ block: "nearest" });
    }
    renderList();
    put(d, input, list, h("div", { class: "foot" }, h("span", null, h("span", { class: "kbd" }, "↑↓"), " move"), h("span", null, h("span", { class: "kbd" }, "Enter"), " open"), h("span", null, h("span", { class: "kbd" }, "Esc"), " close")));
    d.addEventListener("click", (e) => { if (e.target === d) d.close(); }, { once: true });
  }
  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); const d = $("#pal"); if (d.open) d.close(); else openPalette(); }
  });

  // ------------------------------------------------------------------------------------------------ boot
  async function start() {
    try { await loadCatalog(); }
    catch (e) { if (e.status === 401 || e.status === 403 || !tok()) return gate(e.status ? e.message : "Could not reach the API: " + e.message); return gate(e.message); }
    D.ready = true;
    renderTop();
    await refreshLive();
    route();
    startLive();
    setInterval(() => { $$("[data-since]").forEach((el) => { const t = ts(el.dataset.since); if (t) el.textContent = dur(Date.now() - t); }); }, 1000);
  }
  start();
})();
