/* DocuInsights workbench: one page over the case API. No framework and no external script: views are functions of the
   case JSON, redrawn when it changes. Everything a screen shows comes from the API; nothing is computed here. */
(function () {
  "use strict";
  const root = document.getElementById("app"), BASE = root.dataset.base || "";
  const S = { cfg: null, cases: [], ins: null, q: { show: "open", sort: "urgency" }, c: null, view: "home", sel: {}, chat: false, chatLog: [], cf: null, draft: null, fb: null, timer: null, filter: { status: "attention", family: "all" }, layers: { document: true, entity: true, reference: true, reasoning: false, assertion: false }, k: { ov: null, g: null, m: null, e: null, view: "explore", layout: "layered", open: ["industry", "domain", "solution", "customer"], drill: [], grp: null, tf: null, auto: true, frame: "", focus: null, anchor: null, grew: null, reveal: null, cw: null, left: null, labels: true, every: false, hideT: [], hideR: [], hits: null, q: "", place: "", more: null, back: null }, ck: null, pf: null,
    orch: { t: null, playing: false, speed: 1, auto: false, sel: null, timer: null } };

  // ---------------------------------------------------------------------------------------------- helpers
  const SVG = new Set(["svg", "g", "line", "circle", "text", "tspan", "title", "rect", "path", "defs", "pattern", "marker"]);
  function h(tag, attrs) {
    const el = document.createElementNS(SVG.has(tag) ? "http://www.w3.org/2000/svg" : "http://www.w3.org/1999/xhtml", tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k.startsWith("on")) el.addEventListener(k.slice(2), v); else if (k === "class") el.setAttribute("class", v); else if (k === "value" && "value" in el) el.value = v; else el.setAttribute(k, v === true ? "" : v);
    }
    for (let i = 2; i < arguments.length; i++) add(el, arguments[i]);
    return el;
  }
  function add(el, kid) { if (kid === null || kid === undefined || kid === false) return; if (Array.isArray(kid)) kid.forEach((k) => add(el, k)); else el.appendChild(kid.nodeType ? kid : document.createTextNode(String(kid))); }
  // Who is asked for a credential is decided by the page, before the first call: on its own this installation's API key and nothing else
  // is sent (a host's token left in this browser by another application on the same address is not); mounted in a host, the host's token.
  const KEYED = root.dataset.auth !== "host";
  let ownKey = "";
  const token = () => { try { return KEYED ? ownKey || sessionStorage.getItem("docuinsights.key") || "" : localStorage.getItem("cxnova.shop.admin") || localStorage.getItem("cxnova.shop.token") || ""; } catch (e) { return KEYED ? ownKey : ""; } };
  // Who is at the keyboard, as the audit trail will name them. On its own the workbench asks once and remembers; mounted in a host, the host says who it is.
  const who = () => { try { return KEYED ? localStorage.getItem("docuinsights.user") || "" : ""; } catch (e) { return ""; } };
  const signed = (headers) => { const t = token(); if (t) headers.Authorization = "Bearer " + t; if (who()) headers["X-DocuInsights-User"] = encodeURIComponent(who()); return headers; };
  async function api(path, body, method) {
    const headers = signed({});
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const res = await fetch(BASE + "/api/" + path, { method: method || (body !== undefined ? "POST" : "GET"), headers, body: body !== undefined ? JSON.stringify(body) : undefined });
    let data = null; try { data = await res.json(); } catch (e) { data = null; }
    if (!res.ok) { const err = new Error((data && data.detail) || res.status + " " + res.statusText); err.status = res.status; throw err; }
    return data;
  }
  async function raw(path) {
    const res = await fetch(BASE + "/api/" + path, { headers: signed({}) });
    if (!res.ok) { let d = null; try { d = await res.json(); } catch (e) { d = null; } const err = new Error((d && d.detail) || res.status + " " + res.statusText); err.status = res.status; throw err; }
    return res.blob();
  }
  const openRecord = (masked) => run(async () => {   // the decision record is a page of its own: fetched with the credential, opened beside the workbench
    const w = window.open("", "_blank"); const blob = await raw("cases/" + encodeURIComponent(S.c.reference) + "/record" + (masked ? "?mask=1" : ""));
    const url = URL.createObjectURL(new Blob([await blob.text()], { type: "text/html" }));
    if (w) w.location = url; else { const a = h("a", { href: url, download: "decision-record-" + S.c.reference + (masked ? "-masked" : "") + ".html" }); document.body.appendChild(a); a.click(); a.remove(); toast("The decision record was downloaded (the browser blocked a new tab)."); }
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  });
  const download = (path, name) => run(async () => { const url = URL.createObjectURL(await raw(path)); const a = h("a", { href: url, download: name }); document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 30000); });
  function toast(msg) { const t = h("div", { class: "toast", role: "status" }, msg); document.body.appendChild(t); setTimeout(() => t.remove(), 4200); }
  const run = (fn) => async (e) => { if (e && e.preventDefault) e.preventDefault(); try { await fn(e); } catch (err) { if (err.status === 401) return signIn(err.message); toast(err.message); } };
  const pill = (status, label) => h("span", { class: "pill " + (status || "") }, label || status);
  const when = (ts) => ts ? new Date(ts).toLocaleString(undefined, { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "";
  const prof = () => S.cfg.profiles.find((p) => p.id === (S.c ? S.c.profile : (S.sel.profile || S.cfg.profiles[0].id)));
  const typeName = (id) => { const t = prof().document_types.find((x) => x.id === id); return t ? t.name : (id || "Unclassified"); };
  const A = () => (S.c && S.c.assessment) || null;
  const F = (pred) => (A() ? A().findings.filter(pred) : []);
  const section = (title, lead) => [h("div", { class: "page-h" }, h("h1", null, title)), lead ? h("p", { class: "lead" }, lead) : null];
  function cardOf(title) { const c = h("div", { class: "card" }, title ? h("h3", null, title) : null); for (let i = 1; i < arguments.length; i++) add(c, arguments[i]); return c; }
  const kv = (pairs) => h("dl", { class: "kv" }, pairs.filter((p) => p && p[1] !== undefined && p[1] !== null && p[1] !== "").map(([k, v]) => [h("dt", null, k), h("dd", null, v)]));
  const steps = (list) => (list && list.length ? h("ul", { class: "steps" }, list.filter(Boolean).map((s) => h("li", null, s))) : null);
  function form(title, fields, submit) {
    return new Promise((resolve) => {
      const vals = {}; let box;
      const close = (v) => { box.remove(); resolve(v); };
      const inputs = fields.map((f) => {
        let el;
        if (f.options) el = h("select", { onchange: (e) => (vals[f.k] = e.target.value) }, f.options.map((o) => h("option", { value: o[0], selected: o[0] === f.value }, o[1])));
        else if (f.type === "textarea") el = h("textarea", { oninput: (e) => (vals[f.k] = e.target.value) });
        else el = h("input", { type: f.type === "date" ? "date" : f.type === "password" ? "password" : "text", autocomplete: f.type === "password" ? "off" : null, value: f.value || "", placeholder: f.placeholder || "", oninput: (e) => (vals[f.k] = e.target.value) });
        vals[f.k] = f.value || (f.options ? f.options[0][0] : "");
        return h("label", { class: "f" }, h("span", null, f.label), el);
      });
      box = h("div", { class: "modal", onclick: (e) => { if (e.target === box) close(null); } }, h("div", { role: "dialog", "aria-label": title },
        h("form", { class: "stack", onsubmit: (e) => { e.preventDefault();
          const short = fields.findIndex((f) => f.min && String(vals[f.k] || "").trim().length < f.min);   // what was typed stays in the dialog
          if (short >= 0) { toast(fields[short].need || "Fill in: " + fields[short].label); const el = inputs[short].querySelector("input,select,textarea"); if (el) el.focus(); return; }
          close(vals); } }, h("h3", { style: "font-size:16px" }, title), inputs,
          h("div", { class: "row" }, h("button", { class: "btn primary", type: "submit" }, submit || "Save"), h("button", { class: "btn", type: "button", onclick: () => close(null) }, "Cancel")))));
      document.body.appendChild(box); const first = box.querySelector("input,select,textarea"); if (first) first.focus();
    });
  }
  let asking = false;
  async function signIn(message) {
    if (!KEYED) { toast("Sign in to the host portal first, then reload this page. " + (message || "")); return; }
    if (asking) return;   // several calls can fail at once: one question
    asking = true;
    const tried = !!token();
    ownKey = ""; try { sessionStorage.removeItem("docuinsights.key"); } catch (e) { /* private window */ }
    const v = await form("This installation needs its API key", [{ k: "key", type: "password", label: tried ? "That key was not accepted. Enter the API key of this installation." : "API key", placeholder: "API key" }], "Continue");
    asking = false;
    if (v && v.key) { ownKey = v.key.trim(); try { sessionStorage.setItem("docuinsights.key", ownKey); } catch (e) { /* private window: kept for this page only */ } start(); }
  }

  // ---------------------------------------------------------------------------------------------- data
  async function start() {
    try { S.cfg = await api("config"); S.cases = await api("cases"); } catch (err) { if (err.status === 401) { root.replaceChildren(h("p", { class: "boot" }, "Waiting for a key…")); return signIn(err.message); } root.replaceChildren(h("p", { class: "boot" }, err.message)); return; }
    render(); fromAddress();
  }
  const linked = () => { const m = /^#\/knowledge\/(.+)$/.exec(location.hash || ""); try { return m ? decodeURIComponent(m[1]) : null; } catch (err) { return null; } };
  function fromAddress() {   // a link to one node of the knowledge graph: open the graph on exactly that node
    const id = linked();
    if (!id || !S.cfg || (S.view === "knowledge" && S.k.e && S.k.e.entity.id === id)) return;
    kOpen(id).catch((err) => { if (err.status === 401) return signIn(err.message); toast(err.status === 404 ? "That node is not in the knowledge graph any more." : err.message); kHash(null); top("knowledge")(); });
  }
  window.addEventListener("hashchange", fromAddress);
  let resized = null;
  window.addEventListener("resize", () => { clearTimeout(resized); resized = setTimeout(() => { if (S.cfg && S.view === "knowledge" && !S.c && S.k.view !== "manage") { S.k.cw = null; S.k.tf = null; render(); } }, 160); });
  async function open(ref, view) { clearInterval(S.orch.timer); S.orch = { t: null, playing: false, speed: S.orch.speed || 1, auto: !!S.orch.auto, sel: null, timer: null, ticks: 0 }; S.c = await api("cases/" + encodeURIComponent(ref)); S.k.back = null; kHash(null); S.view = view || (A() ? "summary" : "monitor"); S.sel = {}; S.cf = null; S.draft = null; S.chatLog = (S.c.chat || []).flatMap((m) => [{ r: "u", t: m.question }, { r: "a", t: m.answer, no: !m.grounded }]); watch(); render(); }
  function setCase(c, view) { S.c = c; if (view) S.view = view; watch(); render(); }
  function watch() {
    clearTimeout(S.timer);
    if (!S.c || (S.c.progress || {}).state !== "running") return;
    S.timer = setTimeout(async () => { try { const c = await api("cases/" + encodeURIComponent(S.c.reference)); S.orch.ticks = (S.orch.ticks || 0) + 1; S.c = c; render(); watch(); } catch (e) { /* retry on the next tick */ watch(); } }, 700);
  }
  const go = (view) => () => { if (view !== "monitor") { clearInterval(S.orch.timer); S.orch.playing = false; } S.view = view; render(); window.scrollTo(0, 0); };
  function openSource(s) { if (!s.doc || s.doc === "case") return; S.view = "documents"; S.sel.doc = s.doc; S.sel.line = s.page ? { page: s.page, line: s.line } : null; render(); }

  // ---------------------------------------------------------------------------------------------- shell
  const TOP = [["home", "Home"], ["cases", "Cases"], ["insights", "Insights"], ["knowledge", "Knowledge"], ["agents", "Agents"], ["platform", "How it is governed"]];
  const mapQuery = () => "open=" + S.k.open.join(",") + S.k.drill.map((d) => "&drill=" + encodeURIComponent(d)).join("");
  const top = (view) => run(async () => {   // a screen of the workspace (no case open): load what it shows, then draw it
    clearTimeout(S.timer); clearInterval(S.orch.timer); S.orch.playing = false; S.c = null; S.sel = {}; kHash(null); if (view !== "knowledge") S.k.back = null;
    if (view === "home" || view === "cases") S.cases = await api("cases");
    if (view === "knowledge") { S.k.ov = await api("knowledge"); S.k.m = await api("knowledge/map?" + mapQuery()); Object.assign(S.k, { e: null, grp: null, hits: null, tf: null, auto: true, focus: null, anchor: null, grew: null, reveal: null }); }
    if (view === "platform") S.pf = await api("platform");
    if (view === "insights") S.ins = await api("insights");
    S.view = view; render(); window.scrollTo(0, 0);
  });
  function render() {
    const p = prof(), a = A(), running = S.c && (S.c.progress || {}).state === "running";
    const bar = h("header", { class: "top" },
      h("button", { class: "brand", onclick: top("home") }, h("img", { src: BASE + "/static/mark.svg", alt: "" }), h("b", null, S.cfg.product)),
      h("nav", { class: "tabs", "aria-label": "Workspace" }, TOP.map(([id, label]) => h("button", { "aria-current": String(!S.c && (S.view === id || (id === "cases" && S.view === "new"))), onclick: top(id) }, label))),
      h("span", { class: "right" }), h("span", { class: "tag", title: "The model used for reading and narrative. Rules and the recommendation are code." }, S.cfg.model.offline ? "No model: patterns and rules" : "Model: " + S.cfg.model.model + (S.cfg.model.status ? " · " + S.cfg.model.status : "")),
      h("span", { class: "tag" }, "Repository: " + S.cfg.repository.kind), S.cfg.links.portal ? h("a", { class: "tag", href: S.cfg.links.portal, style: "text-decoration:none" }, "CX Nova portal") : null,
      KEYED ? h("button", { class: "tag", style: "border:0;cursor:pointer", title: "The name the audit trail records for what you do here. It is a label, not a sign-in: the API key is the sign-in.", onclick: run(async () => {
        const v = await form("Your name, as the audit trail will record it", [{ k: "name", label: "Name", value: who() }], "Save"); if (!v) return;
        try { if (v.name.trim()) localStorage.setItem("docuinsights.user", v.name.trim().slice(0, 60)); else localStorage.removeItem("docuinsights.user"); } catch (e) { /* private window */ }
        S.cfg = await api("config"); render(); }) }, S.cfg.actor && S.cfg.actor !== "caseworker" ? S.cfg.actor : "Set your name") : null,
      h("span", { class: "tag demo" }, "Demonstration, synthetic data"));
    const views = { home, cases, new: newCase, insights, agents, knowledge, platform, summary, documents, graph, evidence, consistency, assurance, reference, policy, provenance, whatif, monitor, decision, audit };
    const main = h("main", { class: "main" + (S.view === "knowledge" && !S.c && S.k.view !== "manage" ? " full" : "") });
    if (S.c && !a && !["monitor", "documents"].includes(S.view)) S.view = "monitor";
    add(main, (views[S.view] || home)());
    const shell = h("div", { class: "shell" + (S.c ? "" : " wide") }, S.c ? rail(running) : null, main);
    root.replaceChildren(...[bar, shell, S.c && a ? chat() : null].filter(Boolean));
    const cv = document.querySelector(".gx-canvas");   // the explorer fits its scene to the canvas as it really is
    if (cv) { const w = Math.round(cv.getBoundingClientRect().width); if (w && Math.abs(w - kArea().w) > 2) { S.k.cw = w; S.k.tf = null; render(); } }
  }
  function rail(running) {
    const a = A(), count = (pred) => F(pred).length, n = (v, cls) => (v ? h("span", { class: "n " + (cls || "") }, v) : null);
    const item = (id, label, badge) => h("button", { "aria-current": String(S.view === id), onclick: go(id) }, label, badge);
    const st = (f) => ["fail", "missing", "review"].includes(f.status);
    return h("nav", { class: "rail", "aria-label": "Case" },
      h("div", { class: "case" }, h("div", { class: "st", style: "margin:0 0 4px" }, (prof().brand.workspace || prof().name) + (prof().brand.client_label ? ", " + prof().brand.client_label : "")), h("div", { class: "ref" }, S.c.reference), h("div", { class: "st" }, running ? "Assessing…" : S.c.status), S.c.sample ? h("div", { class: "st" }, "Sample: " + S.c.sample.title) : null,
        a && a.triage ? h("div", { class: "tri" }, pill(a.triage.priority, cap(a.triage.priority) + " priority"), dueText(a.triage.due, S.c.due, !!S.c.decision)) : null,
        a ? h("div", { class: "who" }, S.c.assigned ? ["With " + S.c.assigned.to + " ", h("button", { onclick: assign }, "change")] : ["Nobody has taken it ", h("button", { onclick: assign }, "take it")]) : null),
      h("h4", null, "Case"), item("summary", "Intelligence summary"), item("documents", "Documents and classification", n(S.c.documents.filter((d) => !d.removed && d.classification && !d.classification.agrees && !d.review).length, "warn")),
      item("evidence", "Evidence completeness", n(count((f) => f.family === "evidence" && f.status === "missing"), "bad")),
      item("consistency", "Cross-document consistency", n(count((f) => f.agent === "contradiction" && st(f)), "bad")),
      item("assurance", "Screening, integrity, privacy", n(count((f) => ASSURE.includes(f.agent) && st(f)), "bad")),
      prof().reference_sources.length ? item("reference", "Classification assessment", n(count((f) => f.agent === "reference" && f.status === "review"), "warn")) : null,
      item("policy", "Policy and temporal checks", n(count((f) => f.agent === "policy" && st(f)), "bad")),
      h("h4", null, "Explain"), item("graph", "Entity relationship graph"), item("provenance", "Reasoning and provenance"), item("whatif", "Counterfactual simulator"),
      h("h4", null, "Operate"), item("monitor", "Agent orchestration", running ? h("span", { class: "spin right" }) : null), item("decision", "Decision workspace", n(a && !S.c.decision ? 1 : 0, "warn")), item("audit", "Feedback and audit"));
  }

  const ASSURE = ["screening", "integrity", "privacy"];
  const cap = (x) => String(x || "").replace(/^./, (c) => c.toUpperCase());
  function dueText(due, st, decided) {   // a due date as a person reads it: how long is left, and whether it has passed
    if (!due) return h("span", { class: "due muted" }, "–");
    if (decided || !st || st.days_left === undefined) return h("span", { class: "due" }, "due " + due);
    const d = st.days_left;
    return h("span", { class: "due" + (st.overdue ? " over" : st.due_soon ? " soon" : ""), title: "Due " + due }, st.overdue ? "overdue by " + -d + (d === -1 ? " day" : " days") : d === 0 ? "due today" : d === 1 ? "due tomorrow" : d + " days left");
  }
  const assign = run(async () => { const v = await form("Who is working " + S.c.reference + "?", [{ k: "to", label: "Name (leave empty to hand it back to its queue)", value: (S.c.assigned || {}).to || S.cfg.actor || "" }], "Save"); if (v) { setCase(await api("cases/" + S.c.reference + "/assign", { to: v.to })); S.cases = await api("cases"); } });

  // ---------------------------------------------------------------------------------------------- home, new case, agents
  const tryIt = (p, s) => run(async () => { const c = await api("samples", { profile: p.id, sample: s.id }); S.cases = await api("cases"); S.orch = { t: null, playing: false, speed: 1, auto: true, sel: null, timer: null }; open(c.reference, "monitor"); });
  const outcomePill = (c) => (c.label ? pill({ proceed: "pass", review: "fail", request_evidence: "review" }[c.outcome], c.label) : "–");
  function home() {
    const how = [["Put the documents in", "A case is a handful of documents and a few facts about it: PDFs, Word and Excel files, emails with their attachments, scans or text."],
      ["Agents read and link them", "Each document is named, its facts are lifted with the page and line they came from, and the same company or reference number is joined across documents."],
      ["Rules check them", "Is anything missing? Do the dates and amounts hold? Do the documents agree with each other? These checks are code, never a model's opinion."],
      ["A person decides", "You get one of three recommendations with its reasons. Every reason opens the line it rests on. Nothing is decided for you."]];
    const base = [["Agent contracts", "Every agent has a written contract: what it needs, what it promises, what it must never do.", "agents"],
      ["Agent harness", "One harness runs every agent, checks its contract before and after, and records the run.", "platform"],
      ["Shared memory", "Agents leave notes for each other, and the workspace recognises what an earlier case already held.", "platform"],
      ["Knowledge graph", "What each industry works under, what each use case asks for and what every case taught, as one graph in the KB Forge format. Every finding opens the exact node it rests on.", "knowledge"],
      ["Evidence provenance", "Every conclusion points to a line of a document, and every action is in a tamper-evident ledger.", "platform"],
      ["Throttling", "Model calls are rate limited and budgeted per agent. At the limit an agent falls back; it does not fail.", "platform"]];
    const checks = [["Is anything missing?", "Every document the use case requires, always or in stated circumstances."], ["Do the dates and amounts hold?", "Limits, periods and tolerances, each under a rule with a version."],
      ["Do the documents agree?", "The same party, quantity, reference and goods, as each document states them."], ["Does each document hold together?", "Its own sums, its dates, check digits, the same file twice, bank details that changed."],
      ["Is anyone on a watchlist?", "Every party, vessel and port, allowing for spelling. A match always waits for a person."], ["What personal data is in it?", "Found by document and by kind, and masked in whatever leaves the system."]];
    const inv = S.cfg.profiles.find((x) => x.id === "supplier_invoice"), demo = inv && inv.samples.find((x) => x.id === "payment_change");
    return [h("div", { class: "hero" }, h("div", null, h("h1", null, "Give it the documents. Get back a recommendation you can check."),
        h("p", { class: "say" }, S.cfg.product + " reads a set of documents, links what they say, checks them against your rules, and tells a person what to do next, with the line behind every conclusion."),
        h("div", { class: "row" }, h("button", { class: "btn primary", onclick: () => document.getElementById("usecases").scrollIntoView({ behavior: "smooth" }) }, "Try a use case"), h("button", { class: "btn", onclick: go("new") }, "Use my own documents"))),
        demo ? h("button", { class: "file", onclick: tryIt(inv, demo), title: "Open this bundled case and watch the agents work on it" },
          h("div", { class: "hd" }, h("span", { class: "seal review" }, "!"), h("div", null, h("b", null, "Hold for review"), h("span", null, "Supplier invoice AP-2026-004702, three documents, read in under a second"))),
          h("div", { class: "ln" }, h("i", null, "✕"), h("div", null, "The invoice does not add up: 600 at GBP 9.50 is 5,700.00, and it asks for 5,790.00.", h("q", null, "invoice_new_account.pdf, page 1, line 10"))),
          h("div", { class: "ln" }, h("i", null, "!"), h("div", null, "It asks to be paid into an account this supplier has not used before.", h("q", null, "invoice_new_account.pdf, page 1, line 13, against an earlier case"))),
          h("div", { class: "ln" }, h("i", { class: "ok" }, "✓"), h("div", null, "Order, invoice and receipt agree on supplier, goods and quantity, so a three-way match alone would have paid it.", h("q", null, "three documents, eleven lines"))),
          h("div", { class: "go" }, "Open this case")) : null),
      h("h2", { class: "sub" }, "How it works"),
      h("div", { class: "grid g4" }, how.map(([t, d], i) => h("div", { class: "how" }, h("span", { class: "no" }, i + 1), h("h3", null, t), h("p", null, d)))),
      h("h2", { class: "sub", id: "usecases" }, "Pick a use case"),
      h("p", { class: "lead" }, "The same agents run every one of them. A use case is a configuration: the kinds of document, the evidence it asks for and its rules. Each comes with invented sample cases to try."),
      h("div", { class: "grid g3", style: "align-items:start" }, S.cfg.profiles.map((p) => h("div", { class: "uc" }, h("div", { class: "row" }, p.sector ? h("span", { class: "sector" }, p.sector) : null),
        h("h3", null, p.name), h("p", null, p.plain || p.description), h("div", { class: "small muted" }, p.document_types.length + " kinds of document, " + p.rules.length + " rules, " + (p.consistency || []).length + " cross-checks, " + (p.assurance || []).filter((x) => x.family === "integrity").length + " integrity checks"),
        h("div", { class: "samples" }, p.samples.map((s) => h("button", { class: "sample", onclick: tryIt(p, s), title: s.story }, h("b", null, s.title), h("span", null, s.story), h("i", null, "Try it")))),
        h("button", { class: "btn sm ghost", onclick: () => { S.sel.draft = { profile: p.id, reference: "", fields: {}, files: [] }; go("new")(); } }, "Use my own documents"))),
        h("div", { class: "uc own" }, h("div", { class: "row" }, h("span", { class: "sector" }, "Any industry")), h("h3", null, "Your use case"),
          h("p", null, "Claims, onboarding, lending, procurement, compliance: anything that arrives as a set of documents to check."), h("p", { class: "small muted" }, "A new use case is one configuration file. No code changes, no new agents."))),
      h("h2", { class: "sub" }, "What every case is checked for"),
      h("div", { class: "grid g3" }, checks.map(([t, d]) => h("div", { class: "found flat" }, h("h3", null, t), h("p", null, d)))),
      h("h2", { class: "sub" }, "What every agent runs on"),
      h("div", { class: "grid g3" }, base.map(([t, d, v]) => h("button", { class: "found", onclick: top(v) }, h("h3", null, t), h("p", null, d)))),
      S.cases.length ? [h("h2", { class: "sub" }, "Recent cases"), cardOf(null, h("table", null, h("tbody", null, S.cases.slice(0, 5).map((c) => h("tr", { class: "click", onclick: run(() => open(c.reference)) }, h("td", { class: "mono" }, c.reference),
        h("td", null, (S.cfg.profiles.find((p) => p.id === c.profile) || {}).name || c.profile), h("td", null, outcomePill(c)), h("td", null, c.priority ? pill(c.priority, cap(c.priority)) : null), h("td", { class: "small muted" }, when(c.assessed_at)))))), h("button", { class: "btn sm ghost", style: "margin-top:6px", onclick: top("cases") }, "The work queue"))] : null];
  }
  function cases() {
    const waiting = S.cases.filter((c) => c.outcome && !c.decision), q = S.q;
    const shows = { open: ["Open", waiting], overdue: ["Overdue", waiting.filter((c) => c.overdue)], soon: ["Due today or tomorrow", waiting.filter((c) => c.due_soon)], alerts: ["Watchlist or integrity", waiting.filter((c) => c.watchlist_matches || c.integrity_alerts)],
      mine: ["With me", waiting.filter((c) => c.assigned_to && c.assigned_to === S.cfg.actor)], decided: ["Decided", S.cases.filter((c) => c.decision)], all: ["All cases", S.cases] };
    const rank = { urgent: 0, high: 1, normal: 2, low: 3 }, left = (c) => (c.days_left === undefined || c.days_left === null ? 9999 : c.days_left);
    const sorts = { urgency: (x, y) => (rank[x.priority] === undefined ? 9 : rank[x.priority]) - (rank[y.priority] === undefined ? 9 : rank[y.priority]) || left(x) - left(y), due: (x, y) => left(x) - left(y), newest: (x, y) => String(y.created_at).localeCompare(String(x.created_at)) };
    const rows = [...shows[q.show][1]].sort(q.show === "decided" || q.show === "all" ? sorts.newest : sorts[q.sort] || sorts.urgency);
    const tile = (id, hot) => h("button", { class: "tile click" + (q.show === id ? " on" : "") + (hot && shows[id][1].length ? " hot" : ""), onclick: () => { q.show = id; render(); } }, h("b", null, shows[id][1].length), h("span", null, shows[id][0]));
    return [section("Cases", "The work queue of this workspace. Each assessed case has a priority, a queue and a due date, set by fixed rules with their reasons. Nobody is assigned by the system: a person takes a case."),
      h("div", { class: "grid g6", style: "margin-bottom:16px" }, tile("open"), tile("overdue", true), tile("soon"), tile("alerts", true), tile("mine"), tile("decided")),
      h("div", { class: "qbar" }, h("button", { class: "btn primary", onclick: go("new") }, "Create a case"), h("button", { class: "btn", onclick: top("home") }, "Try a sample"),
        h("div", { class: "seg" }, [["urgency", "Most urgent first"], ["due", "Soonest due"], ["newest", "Newest"]].map(([k, l]) => h("button", { "aria-pressed": String(q.sort === k), onclick: () => { q.sort = k; render(); } }, l))),
        h("button", { class: "btn sm ghost", onclick: () => { q.show = "all"; render(); } }, "Show all " + S.cases.length),
        h("button", { class: "btn right", disabled: !S.cases.length, onclick: download("export/cases.csv", "docuinsights-cases.csv"), title: "The queue as a spreadsheet opens it" }, "Export as CSV")),
      cardOf(null, rows.length ? h("table", null, h("thead", null, h("tr", null, ["Reference", "Use case", "Recommendation", "Priority", "Queue", "Due", "With", "Status"].map((x) => h("th", null, x)))),
        h("tbody", null, rows.map((c) => h("tr", { class: "click", onclick: run(() => open(c.reference)) }, h("td", { class: "mono" }, c.reference), h("td", null, (S.cfg.profiles.find((p) => p.id === c.profile) || {}).name || c.profile),
          h("td", null, outcomePill(c), c.watchlist_matches ? h("div", { class: "small", style: "color:var(--bad);margin-top:3px" }, c.watchlist_matches + " watchlist match" + (c.watchlist_matches > 1 ? "es" : "")) : null,
            c.integrity_alerts ? h("div", { class: "small", style: "color:var(--bad);margin-top:3px" }, c.integrity_alerts + " integrity alert" + (c.integrity_alerts > 1 ? "s" : "")) : null),
          h("td", null, c.priority ? pill(c.priority, cap(c.priority)) : h("span", { class: "muted" }, "–")), h("td", { class: "small" }, c.queue || "–"), h("td", null, dueText(c.due, c, !!c.decision)),
          h("td", { class: "small" }, c.assigned_to || h("span", { class: "muted" }, "–")), h("td", { class: "small" }, c.state === "running" ? [h("span", { class: "spin" }), " assessing"] : c.status)))))
        : h("p", { class: "muted" }, S.cases.length ? "No case in this view." : "No cases yet. Try a sample from the home page, or create a case with your own documents."))];
  }
  function newCase() {
    const d = S.sel.draft || (S.sel.draft = { profile: S.cfg.profiles[0].id, reference: "", fields: {}, files: [] });
    S.sel.profile = d.profile; const p = prof();
    const pick = async (files) => { for (const f of files) { const b64 = await new Promise((res, rej) => { const r = new FileReader(); r.onload = () => res(String(r.result).split(",")[1]); r.onerror = rej; r.readAsDataURL(f); }); d.files.push({ name: f.name, size: f.size, category: "", content_base64: b64 }); } render(); };
    const drop = h("div", { class: "drop", tabindex: 0, onclick: () => fileInput.click(), ondragover: (e) => { e.preventDefault(); drop.classList.add("over"); }, ondragleave: () => drop.classList.remove("over"), ondrop: (e) => { e.preventDefault(); pick(e.dataTransfer.files); } },
      "Drop the evidence package here, or click to choose files", h("div", { class: "small" }, "PDF, Word, Excel, email (with its attachments), web page, text, CSV or a scan (PNG, JPEG), up to " + (S.cfg.max_file_mb || 15) + " MB each"));
    const fileInput = h("input", { type: "file", multiple: true, accept: (S.cfg.accepts || [".pdf", ".png", ".jpg", ".jpeg", ".txt"]).join(","), style: "display:none", onchange: (e) => pick(e.target.files) });
    return [section("Create a case", "Register the case, give the case facts the rules need, and upload the evidence package. Each file is kept as an immutable version with its hash."),
      h("form", { class: "stack", onsubmit: run(async () => { const c = await api("cases", { reference: d.reference, profile: d.profile, fields: d.fields, documents: d.files, assess: true }); S.sel.draft = null; S.cases = await api("cases"); open(c.reference, "monitor"); }) },
        cardOf("Case", h("div", { class: "grid g3" }, h("label", { class: "f" }, h("span", null, "Use case"), h("select", { onchange: (e) => { d.profile = e.target.value; d.fields = {}; render(); } }, S.cfg.profiles.map((x) => h("option", { value: x.id, selected: x.id === d.profile }, x.name)))),
          h("label", { class: "f" }, h("span", null, p.case.reference_label || "Case reference"), h("input", { type: "text", required: true, value: d.reference, placeholder: p.case.reference_example || "", oninput: (e) => (d.reference = e.target.value.trim()) })),
          (p.case.fields || []).map((f) => h("label", { class: "f" }, h("span", null, f.name), h("input", { type: f.type === "date" ? "date" : "text", value: d.fields[f.id] || f.default || "", oninput: (e) => (d.fields[f.id] = e.target.value) }))))),
        cardOf("Evidence package", drop, fileInput, d.files.length ? h("table", { style: "margin-top:10px" }, h("thead", null, h("tr", null, ["File", "Size", "Uploaded as", ""].map((x) => h("th", null, x)))), h("tbody", null, d.files.map((f, i) => h("tr", null, h("td", null, f.name), h("td", { class: "small muted" }, Math.round(f.size / 1024) + " KB"),
          h("td", null, h("select", { onchange: (e) => (f.category = e.target.value) }, h("option", { value: "" }, "Other documents"), p.document_types.map((t) => h("option", { value: t.id, selected: f.category === t.id }, t.name)))), h("td", null, h("button", { class: "btn sm ghost", type: "button", onclick: () => { d.files.splice(i, 1); render(); } }, "Remove")))))) : null),
        h("div", { class: "row" }, h("button", { class: "btn primary", type: "submit", disabled: !d.files.length }, "Create and assess"), h("button", { class: "btn", type: "button", onclick: top("cases") }, "Cancel")))];
  }
  // ---------------------------------------------------------------------------------------------- insights: the workspace measured from its own case files
  function insights() {
    const d = S.ins; if (!d) return [section("Insights", "Loading the workspace's numbers…")];
    const r = d.rates, pct = (x) => (x.value === null || x.value === undefined ? "–" : x.value + "%"), o = d.outcomes, n = d.assessed;
    const NAMES = { proceed: "Ready", request_evidence: "Something to request", review: "Referred to a person" }, VAR = { proceed: "--v-go", request_evidence: "--v-ask", review: "--v-hold" }, KEYS = ["proceed", "request_evidence", "review"];
    const stat = (value, label, how, hot) => h("div", { class: "card stat", title: how }, h("b", { style: hot ? "color:var(--bad)" : null }, value), h("span", null, label), h("small", null, how));
    const legend = (counts) => h("div", { class: "legend" }, KEYS.map((k) => h("span", null, h("i", { style: "background:var(" + VAR[k] + ")" }), NAMES[k] + " ", h("b", null, counts[k]))));
    const refresh = h("button", { class: "btn sm", onclick: run(async () => { S.ins = await api("insights"); render(); toast("Counted again"); }) }, "Count again");
    if (!n) return [section("Insights", "The workspace measured from its own case files: how many cases go straight through, how many need a person and why, how long a decision takes, what is due."),
      cardOf(null, h("p", { class: "muted" }, "No case has been assessed yet, so there is nothing to count. Try a sample from the home page, then come back."), h("div", { class: "row", style: "margin-top:10px" }, h("button", { class: "btn primary", onclick: top("home") }, "Try a sample"), refresh))];
    const t = d.time.to_decision_hours, fmtH = (x) => (x === null || x === undefined ? "–" : x < 1 ? Math.round(x * 60) + " min" : x < 48 ? x + " h" : Math.round(x / 24) + " days");
    const most = Math.max(1, ...d.daily.map((x) => x.proceed + x.request_evidence + x.review)), top3 = Math.max(1, ...d.reasons.map((x) => x.cases)), topQ = Math.max(1, ...d.service.queues.map((x) => x.cases));
    const peak = d.daily.reduce((b, x, i) => (x.proceed + x.request_evidence + x.review > b.v ? { v: x.proceed + x.request_evidence + x.review, i } : b), { v: 0, i: -1 }).i;
    const dayName = (iso) => new Date(iso + "T12:00:00").toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
    const agentName = (id) => { const a = S.cfg.agents.find((x) => x.id === id); return a ? short(a.name) : id; };
    return [h("div", { class: "page-h" }, h("h1", null, "Insights"), h("span", { class: "right" }, refresh)),
      h("p", { class: "lead" }, "The workspace measured from its own case files. Nothing here is estimated: every figure is counted, and each says how. Counted from " + d.cases + (d.of_total > d.cases ? " of " + d.of_total : "") + " case(s), " + when(d.as_of) + "."),
      h("div", { class: "viz" },
        h("div", { class: "grid g4" }, stat(pct(r.straight_through), "go straight through", r.straight_through.how), stat(pct(r.exception), "are referred to a person", r.exception.how), stat(fmtH(t.median), "median time to a decision", t.how + (t.of ? " Counted on " + t.of + "." : " None decided yet.")),
          stat(d.service.overdue, "open and overdue", d.service.how, d.service.overdue > 0)),
        h("div", { class: "grid g2", style: "margin-top:14px" },
          cardOf("How " + n + " assessed case(s) came out", h("div", { class: "stackbar", role: "img", "aria-label": KEYS.map((k) => NAMES[k] + " " + o[k]).join(", ") }, KEYS.filter((k) => o[k]).map((k) => h("i", { style: "flex:" + o[k] + ";background:var(" + VAR[k] + ")", title: NAMES[k] + ": " + o[k] + " of " + n }))), legend(o),
            h("table", { style: "margin-top:10px" }, h("tbody", null, [["Something had to be requested", pct(r.evidence_request), r.evidence_request.how], ["Decisions that differ from the recommendation", pct(r.override), r.override.how + " Counted on " + r.override.of + "."],
              ["Extracted values a reviewer changed", pct(r.corrected_values), r.corrected_values.how], ["Agent runs that kept their contract", pct(r.contracts_kept), r.contracts_kept.how + " Counted on " + r.contracts_kept.of + "."]].map(([k, v, how]) => h("tr", { title: how }, h("td", null, k, h("div", { class: "small muted" }, how)), h("td", { style: "text-align:right;font-weight:650;white-space:nowrap" }, v)))))),
          cardOf(d.open + " open case(s) against their due dates", h("div", { class: "grid g3" }, [["Overdue", d.service.overdue, true], ["Due today or tomorrow", d.service.due_soon], ["On track", d.service.on_track]].map(([l, v, hot]) => h("div", null, h("b", { style: "display:block;font:680 24px/1.1 var(--display);" + (hot && v ? "color:var(--bad)" : "") }, v), h("span", { class: "small muted" }, l)))),
            h("h3", { style: "margin:16px 0 4px" }, "By queue"), d.service.queues.length ? d.service.queues.map((q) => h("div", { class: "hbar" }, h("span", { class: "nm" }, q.queue), h("span", { class: "tr" }, h("i", { style: "width:" + Math.round((q.cases / topQ) * 100) + "%" })), h("b", null, q.cases))) : h("p", { class: "muted small" }, "Nothing is open."),
            d.service.open_cases.length ? h("details", { style: "margin-top:10px" }, h("summary", { class: "small muted", style: "cursor:pointer" }, "The open cases, most urgent first"), h("table", { style: "margin-top:6px" }, h("tbody", null, d.service.open_cases.slice(0, 12).map((c) => h("tr", { class: "click", onclick: run(() => open(c.reference)) }, h("td", { class: "mono small" }, c.reference),
              h("td", null, c.priority ? pill(c.priority, cap(c.priority)) : null), h("td", { class: "small" }, c.queue || "–"), h("td", null, dueText(c.due, c, false))))))) : null)),
        h("div", { class: "grid g2", style: "margin-top:14px" },
          cardOf("Why cases need a person", h("p", { class: "small muted", style: "margin-bottom:6px" }, "The checks that most often send a case to a person, counted once for each case they stopped."),
            d.reasons.length ? d.reasons.map((x) => h("div", { class: "hbar", title: x.title + " (" + agentName(x.agent) + "): " + x.cases + " case(s)" }, h("span", { class: "nm" }, x.title, h("small", null, agentName(x.agent))), h("span", { class: "tr" }, h("i", { style: "width:" + Math.round((x.cases / top3) * 100) + "%" })), h("b", null, x.cases)))
              : h("p", { class: "muted small" }, "No check has sent a case to a person.")),
          cardOf("Cases assessed each day, last 14 days", h("div", { class: "cols", role: "img", "aria-label": "Cases assessed per day by outcome" }, d.daily.map((x, i) => { const tot = x.proceed + x.request_evidence + x.review;
              return h("div", { class: "col", tabindex: tot ? 0 : null, "data-tip": dayName(x.day) + ": " + (tot ? KEYS.filter((k) => x[k]).map((k) => x[k] + " " + NAMES[k].toLowerCase()).join(", ") : "none") },
                KEYS.filter((k) => x[k]).map((k) => h("i", { style: "height:" + (x[k] / most) * 100 + "%;background:var(" + VAR[k] + ")" })), tot && (i === peak || i === d.daily.length - 1) ? h("span", { class: "n", style: "top:auto;bottom:calc(" + (tot / most) * 100 + "% + " + (KEYS.filter((k) => x[k]).length * 2 + 1) + "px)" }, tot) : null); })),
            h("div", { class: "colx" }, d.daily.map((x, i) => h("span", { title: x.day }, i === 0 || x.day.slice(8) === "01" ? new Date(x.day + "T12:00:00").toLocaleDateString(undefined, { day: "numeric", month: "short" }) : +x.day.slice(8)))), legend(d.daily.reduce((m, x) => { KEYS.forEach((k) => (m[k] += x[k])); return m; }, { proceed: 0, request_evidence: 0, review: 0 })),
            h("details", { style: "margin-top:6px" }, h("summary", { class: "small muted", style: "cursor:pointer" }, "The same numbers as a table"), h("table", { style: "margin-top:6px" }, h("thead", null, h("tr", null, ["Day", ...KEYS.map((k) => NAMES[k])].map((x) => h("th", null, x)))),
              h("tbody", null, d.daily.filter((x) => x.proceed + x.request_evidence + x.review).map((x) => h("tr", null, h("td", { class: "small" }, dayName(x.day)), KEYS.map((k) => h("td", null, x[k]))))))))),
        h("div", { class: "grid g2", style: "margin-top:14px" },
          cardOf("By use case", h("table", null, h("thead", null, h("tr", null, ["Use case", "Cases", "Outcomes", "Straight through"].map((x) => h("th", null, x)))), h("tbody", null, d.by_use_case.map((u) => h("tr", null, h("td", null, u.name), h("td", null, u.cases),
            h("td", { style: "width:36%" }, h("div", { class: "stackbar", style: "height:10px", title: KEYS.map((k) => NAMES[k] + " " + u[k]).join(", ") }, KEYS.filter((k) => u[k]).map((k) => h("i", { style: "flex:" + u[k] + ";background:var(" + VAR[k] + ")" })))), h("td", { style: "font-weight:650" }, u.straight_through === null ? "–" : u.straight_through + "%")))))),
          cardOf("Screening, integrity and privacy", h("table", null, h("tbody", null, [["Names screened against the watchlists", d.assurance.names_screened], ["Possible watchlist matches", d.assurance.watchlist_matches + (d.assurance.watchlist_matches ? " (" + d.assurance.waiting + " waiting, " + d.assurance.cleared + " cleared, " + d.assurance.confirmed + " confirmed)" : "")],
              ["Integrity checks that need a person", d.assurance.integrity_alerts], ["Documents read", d.volume.documents + " (" + d.volume.pages + " pages, " + d.volume.values + " values lifted)"], ["Model calls", d.volume.model_calls]].map(([k, v]) => h("tr", null, h("td", null, k), h("td", { style: "text-align:right;font-weight:650" }, v))))),
            d.assurance.personal_data.length ? [h("h3", { style: "margin:14px 0 4px" }, "Personal data found, by kind"), h("div", { class: "chips" }, d.assurance.personal_data.map((x) => h("span", { class: "chip", style: "cursor:default" }, x.kind + ": " + x.items)))] : null)))];
  }

  function agents() {
    const list = (title, items, cls) => (items && items.length ? h("div", { class: "clause " + (cls || "") }, h("h4", null, title), h("ul", null, items.map((x) => h("li", null, x)))) : null);
    return [section("The agents and their contracts", "One agent per job. Each has a contract the harness enforces on every run: what it needs before it starts, what it promises when it ends, what it must never do, and what it may spend. Nothing that decides may use a model."),
      h("div", { class: "grid g2" }, S.cfg.agents.map((a) => { const c = a.contract; return h("div", { class: "card contract" }, h("div", { class: "row" }, h("h3", { style: "margin:0" }, a.name), h("span", { class: "pill plain right" }, c.model.use === "none" ? "code only" : "may use a model")),
        h("p", { style: "margin:6px 0 10px" }, c.purpose),
        list("Needs before it starts", c.needs.map((n) => n.text)), list("Promises when it ends", c.promises.map((p) => p.text), "ok"), list("Never", c.never.map((n) => n.charAt(0).toUpperCase() + n.slice(1) + "."), "no"),
        h("div", { class: "small muted", style: "margin-top:8px" }, c.spend + " If it fails: " + c.on_failure_text.charAt(0).toLowerCase() + c.on_failure_text.slice(1) + " Contract v" + c.version + ".")); }))];
  }

  // ---------------------------------------------------------------------------------------------- knowledge
  const KCOL = { IndustrySegment: "#2c62c9", ExperienceDomain: "#0f5c3a", UseCase: "#1b7f52", Document: "#4a9c74", PolicyRule: "#8fbfa6", Role: "#c3d9cd", ReferenceSource: "#c79a1a", ReferenceEntry: "#e0c063", Accelerator: "#c4511a", SoftwareAgent: "#ee6c2b",
    Company: "#5a3794", Organization: "#7a4fb5", Product: "#a887d6", Case: "#3d2a6b", SourceDocument: "#8fa9d8", Identifier: "#b9a3dc", CaseFinding: "#c9c2d8",
    Standard: "#1d4ea0", Regulation: "#16408a", Provision: "#6f93d6", StandardsBody: "#9bb4e4", BusinessProcess: "#0e8f8f", Risk: "#b8342b", KPI: "#c79a1a", Term: "#8a93a3", BestPractice: "#2f9e6e", Capability: "#57b894", Challenge: "#d9822b", PainPoint: "#d9822b" };
  const ORIGIN_PILL = { document: "pass", system: "info", settings: "info", web: "info", curated: "info", manual: "review", imported: "review", inferred: "", decision: "pass", unverified: "fail", unknown: "" };
  // Getting to a node. Every node has an address of its own (#/knowledge/<id>): opening it opens the tree along the way down to it.
  const kHash = (id) => { try { history.replaceState(null, "", id ? "#/knowledge/" + encodeURIComponent(id) : location.pathname + location.search); } catch (err) { /* a frame that may not touch the address */ } };
  const kLink = (id) => location.origin + location.pathname + location.search + "#/knowledge/" + encodeURIComponent(id);
  async function kOpen(id) {
    clearTimeout(S.timer); clearInterval(S.orch.timer); S.orch.playing = false;
    const e = await api("knowledge/entity?id=" + encodeURIComponent(id));
    if (!S.k.ov) S.k.ov = await api("knowledge");
    const fresh = S.view !== "knowledge" || !!S.c || S.k.view !== "explore";   // arriving from a case or another screen: the scene shows the way to this node and nothing else opened
    const m = await api("knowledge/map?" + (fresh ? "open=" + S.k.open.join(",") : mapQuery()) + "&show=" + encodeURIComponent(id));
    if (S.c) S.k.back = S.c.reference;
    if (fresh) S.k.q = "";
    Object.assign(S.k, { e, m, grp: null, open: m.open, drill: m.drilled, view: "explore", hits: null, hideT: [], more: null, tf: null, auto: true, focus: id, anchor: null, grew: null, reveal: null });
    S.c = null; S.sel = {}; S.view = "knowledge"; kHash(id); render();
  }
  const kShow = (id) => run(() => kOpen(id));
  const kSelect = (id) => run(async () => { S.k.e = await api("knowledge/entity?id=" + encodeURIComponent(id)); S.k.grp = null; S.k.more = null; S.k.reveal = id; kHash(id); render(); });
  const kMapNow = async () => { S.k.m = await api("knowledge/map?" + mapQuery()); render(); };
  const kReload = async (keep) => { S.ck = null; S.k.ov = await api("knowledge"); S.k.m = await api("knowledge/map?" + mapQuery()); if (!keep) { S.k.e = null; S.k.grp = null; kHash(null); } render(); };
  const words = (name) => name.replace(/([a-z0-9])([A-Z])/g, "$1 $2").toLowerCase();
  const LCOL = { industry: "#2563eb", domain: "#0d9488", solution: "#7c3aed", customer: "#db2777", added: "#7a8496" };   // the four levels, in KB Forge's colours
  const LVL = { industry: "Industry", domain: "Domain", solution: "Solution", customer: "Customer", added: "Added" };
  const kShort = (name) => String(name).split(": ")[0];   // an article is named with its subject ("UCP 600 article 14(c): presentation within 21 days"); a chip shows the reference
  const dot = (type, level) => h("i", { class: "kdot", style: "background:" + (LCOL[level] || KCOL[type] || "#7a8496") });
  const kchip = (node, label, title) => h("button", { class: "kchip", type: "button", style: "border-left-color:" + (LCOL[node.level] || "#7a8496"), title: title || words(node.type || "") + ", " + (LVL[node.level] || "added").toLowerCase() + " level: open it in the knowledge graph", onclick: kShow(node.id) }, label);
  const statusPills = (by) => ["fail", "missing", "review", "pass", "info"].filter((s) => by[s]).map((s) => pill(s, by[s] + " " + s));
  const plural = (type, n) => { const w = words(type).replace(/^./, (c) => c.toUpperCase()); return n === 1 ? w : /kpi$/i.test(w) ? "KPIs" : /(s|x|ch)$/.test(w) ? w + "es" : /[^aeiou]y$/.test(w) ? w.slice(0, -1) + "ies" : w + "s"; };
  const ICON = { layers: ["m12 2 10 5-10 5L2 7l10-5ZM2 17l10 5 10-5M2 12l10 5 10-5"], up: ["M12 19V5M5 12l7-7 7 7"], down: ["M12 5v14M19 12l-7 7-7-7"], home: ["M3 10.5 12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1v-9.5Z"], close: ["M18 6 6 18M6 6l12 12"],
    plus: ["M12 5v14M5 12h14"], minus: ["M5 12h14"], fit: ["M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3m0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3"], search: ["M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14ZM21 21l-4.3-4.3"], eye: ["M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z", "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z"],
    eyeoff: ["M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z", "M3 3l18 18"], reset: ["M3 12a9 9 0 1 0 3-6.7L3 8M3 3v5h5"], filter: ["M3 5h18l-7 8v6l-4-2v-4L3 5Z"], compass: ["M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20Z", "m16 8-2.5 5.5L8 16l2.5-5.5L16 8Z"], chev: ["m9 6 6 6-6 6"], link: ["M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"] };
  const ico = (name, size) => h("svg", { width: size || 13, height: size || 13, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": 1.8, "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" }, ICON[name].map((d) => h("path", { d })));

  /* The tidy tree, as KB Forge's explorer lays a view out: each entity over its children in a grid whose columns are as wide as the longest
     name, the children of one kind starting a line of their own, and the links drawn as connectors (down from under the parent's name to
     its bus, along it, and down into each child). The positions are computed, not settled by forces: the same scene always looks the same. */
  const GW = 176, GH = 48, BUS_GAP = 24, DROP = 18, COL_GAP = 28, BLOCK_GAP = 56, MAX_COLS = 8, MAX_KINDS = 10, CHAIN_LEFT = 132, BAND_GAP = 56, LABEL_WIDTH = 26, LABEL_LINES = 3;
  const nodeRadius = (degree) => 9 + Math.min(11, Math.sqrt(degree || 0) * 2.4);
  function labelLines(text) {
    const ws = String(text).split(/\s+/).filter(Boolean), lines = []; let i = 0;
    while (i < ws.length && lines.length < LABEL_LINES) { let line = ws[i]; i++; while (i < ws.length && (line + " " + ws[i]).length <= LABEL_WIDTH) { line += " " + ws[i]; i++; } lines.push(line.length > LABEL_WIDTH ? line.slice(0, LABEL_WIDTH - 1) + "…" : line); }
    if (i < ws.length && lines.length) { const last = lines[lines.length - 1].replace(/…$/, ""); lines[lines.length - 1] = (last.length >= LABEL_WIDTH ? last.slice(0, LABEL_WIDTH - 1) : last) + "…"; }
    return lines;
  }
  function textWidth(text, size) {
    let em = 0;
    for (const ch of text) em += ch === " " ? 0.32 : ch >= "A" && ch <= "Z" ? 0.72 : ch >= "a" && ch <= "z" ? 0.6 : ch >= "0" && ch <= "9" ? 0.62 : ".,:;'!|()[]".includes(ch) ? 0.32 : ch === "—" ? 1 : 0.65;
    return em * (size || 11);
  }
  const tidyNode = (label, r) => { const lines = labelLines(label), widest = lines.reduce((m, l) => Math.max(m, textWidth(l)), 0); return { r, half: Math.max(r + 6, widest / 2 + 4), below: r + 14 + Math.max(0, lines.length - 1) * 12.5 + 5 }; };
  const maxOf = (xs) => xs.reduce((m, x) => Math.max(m, x), -Infinity);
  function gridColumns(sizes, cell, line, area, above, most) {
    const total = sizes.reduce((a, b) => a + b, 0);
    if (total <= 1) return 1;
    let best = 1, bestScore = -Infinity;
    for (let cols = 1; cols <= Math.min(maxOf(sizes), most); cols++) { const lines = sizes.reduce((sum, x) => sum + Math.ceil(x / cols), 0), k = Math.min(area.w / (cols * cell), area.h / (above + lines * line), 1.1), score = k - lines * 0.004; if (score > bestScore + 1e-9) { bestScore = score; best = cols; } }
    return sizes.length === 1 ? Math.ceil(total / Math.ceil(total / best)) : best;
  }
  const gridLines = (items, cols) => { const out = [], step = Math.max(1, cols); for (let i = 0; i < items.length; i += step) out.push(items.slice(i, i + step)); return out; };
  function tidyLayout(input) {
    const pos = {}, routes = {}, captions = [], node = input.node, area = input.area, boxes = input.boxes || [];
    const claimed = new Set([...boxes, ...input.tops]), kidsOf = {}, queue = [...boxes, ...input.tops];   // each entity once, under the first parent that reaches it
    for (let i = 0; i < queue.length; i++) { const id = queue[i], list = input.childrenOf(id).filter((c) => !claimed.has(c)); list.forEach((c) => claimed.add(c)); kidsOf[id] = list; queue.push(...list); }
    const open = (id) => (kidsOf[id] || []).length > 0;
    function family(parent, list, below) {   // the children under a parent (null: the tops), `below` being how far the parent's drawing reaches under its centre
      const bus0 = below + BUS_GAP, ns = list.map(node), cell = 2 * maxOf(ns.map((n) => n.half)) + COL_GAP, rMax = maxOf(ns.map((n) => n.r)), belowMax = maxOf(ns.map((n) => n.below));
      let groups = [];
      for (const c of list) { const last = groups[groups.length - 1]; if (last && input.kindOf(last[0]) === input.kindOf(c)) last.push(c); else groups.push([c]); }
      if (groups.length > MAX_KINDS) groups = [groups.reduce((a, g) => a.concat(g), [])];
      let captionW = 0; const heads = {};
      if (groups.length > 1) { groups.forEach((g) => (heads[g[0]] = g.length)); captionW = maxOf(groups.map((g) => textWidth(input.caption(g[0], g.length), 10))) + 20; }
      const cols = gridColumns(groups.map((g) => g.length), cell, rMax + belowMax + BUS_GAP + DROP, { w: Math.max(200, area.w - captionW), h: area.h }, parent === null ? 0 : 2 * (bus0 + DROP), parent === null ? Math.max(MAX_COLS, Math.ceil(Math.sqrt(list.length) * 1.4)) : MAX_COLS);
      const fams = {}; list.forEach((c) => { if (open(c)) fams[c] = family(c, kidsOf[c], node(c).below); });   // what is open: each one's own family, as wide as its slot in the line must be
      const nFams = Object.keys(fams).length, slot = (c) => Math.max(cell, fams[c] ? fams[c].w + BLOCK_GAP : 0), room = Math.max(cols * cell, area.w / 0.62) * 2.5, lines = [];
      for (const g of groups) for (const line of gridLines(g, cols)) { let cur = [], w = 0; for (const c of line) { const sw = slot(c); if (cur.length && w + sw > room) { lines.push(cur); cur = []; w = 0; } cur.push(c); w += sw; } if (cur.length) lines.push(cur); }
      let top = parent === null ? 0 : bus0 + DROP, hgt = below; const lineY = [], lineBus = [], lineBelow = [], lineW = [];
      for (const line of lines) {   // down the lines: each line, then the families of what is open in it, below its names
        const r = maxOf(line.map((c) => node(c).r)), lb = maxOf(line.map((c) => node(c).below)), y = top + r;
        lineBus.push(top - DROP); lineY.push(y); lineBelow.push(lb); lineW.push(line.reduce((sum, c) => sum + slot(c), 0));
        let bottom = y + lb; for (const c of line) if (fams[c]) bottom = Math.max(bottom, y + (lb - node(c).below) + fams[c].h);
        hgt = Math.max(hgt, bottom); top = bottom + BUS_GAP + DROP;
      }
      const slotsW = maxOf(lineW), inner = captionW + slotsW, nHeads = Object.keys(heads).length, edge = nFams > 0 && lines.length > 1;
      const trunkDx = edge ? -inner / 2 - 16 : nHeads ? captionW / 2 - slotsW / 2 : captionW / 2 + (cols % 2 === 0 ? 0 : cell / 2);   // the trunk to the lines below the first: between two columns, or down a left edge
      return { w: inner + (edge ? 32 : 0), h: hgt, place: (cx, py, drop) => {
        const gx = cx + captionW / 2, slotsLeft = gx - slotsW / 2; drop = drop || 0;
        lines.forEach((line, i) => {
          const y = py + drop + lineY[i], free = edge || nHeads > 0 || nFams > 0 || i === lines.length - 1;
          let x = free ? gx - lineW[i] / 2 : slotsLeft + Math.floor((cols - line.length) / 2) * cell; const first = x + slot(line[0]) / 2;
          for (const c of line) { const sw = slot(c), at = x + sw / 2; pos[c] = { x: at, y };
            if (parent !== null) routes[c] = i === 0 ? { parent, busUp: lineY[0] - lineBus[0] } : { parent, busUp: lineY[i] - lineBus[i], trunk: { dx: trunkDx, down: bus0 + drop } };
            if (fams[c]) fams[c].place(at, y, lineBelow[i] - node(c).below); x += sw; }
          if (heads[line[0]] !== undefined) captions.push({ anchor: line[0], dx: slotsLeft - 12 - first, text: input.caption(line[0], heads[line[0]]) });
        });
      } };
    }
    if (boxes.length) {   // the levels, top-down: each box on the spine at the left, what it opened in a band to its right, then the next box under the band
      let y = 0;
      for (const b of boxes) { const bn = node(b); pos[b] = { x: 0, y }; const list = kidsOf[b] || [];
        if (!list.length) { y += 2 * bn.below + BAND_GAP; continue; }
        const fam = family(b, list, bn.below), cx = CHAIN_LEFT + fam.w / 2; fam.place(cx, y);
        list.forEach((c) => { if (routes[c]) routes[c] = Object.assign({}, routes[c], { side: { dx: cx } }); });
        y += fam.h + BAND_GAP + bn.below; }
      if (input.tops.length) { const fam = family(null, input.tops, 0); fam.place(CHAIN_LEFT + fam.w / 2, y); }
    } else if (input.tops.length) family(null, input.tops, 0).place(0, 0);
    return { pos, routes, captions };
  }
  function roundedPath(points, radius) {   // a polyline as an SVG path with its corners rounded
    const q = []; for (const p of points) { const last = q[q.length - 1]; if (!last || Math.abs(last.x - p.x) > 0.05 || Math.abs(last.y - p.y) > 0.05) q.push(p); }
    const f = (n) => n.toFixed(1); if (!q.length) return "";
    let d = "M" + f(q[0].x) + "," + f(q[0].y);
    for (let i = 1; i < q.length - 1; i++) { const a = q[i - 1], b = q[i], c = q[i + 1], l1 = Math.hypot(b.x - a.x, b.y - a.y), l2 = Math.hypot(c.x - b.x, c.y - b.y), r = Math.min(radius, l1 / 2, l2 / 2);
      d += " L" + f(b.x + ((a.x - b.x) / l1) * r) + "," + f(b.y + ((a.y - b.y) / l1) * r) + " Q" + f(b.x) + "," + f(b.y) + " " + f(b.x + ((c.x - b.x) / l2) * r) + "," + f(b.y + ((c.y - b.y) / l2) * r); }
    if (q.length > 1) d += " L" + f(q[q.length - 1].x) + "," + f(q[q.length - 1].y);
    return d;
  }
  function routePath(route, p, c, parent, child) {   // a child's connector from where its parent and it are: out of a box's side or down from under a name, along the bus, down the trunk when it has one, and into it
    const end = { x: c.x, y: c.y - child.r - 2 }, bus = c.y - route.busUp, ox = route.side ? p.x + route.side.dx : p.x;
    const head = route.side ? [{ x: p.x + parent.half, y: p.y }, { x: ox, y: p.y }] : [{ x: p.x, y: p.y + parent.below + 2 }];
    if (!route.trunk) return roundedPath([...head, { x: ox, y: bus }, { x: c.x, y: bus }, end], 7);
    const bus0 = p.y + route.trunk.down, tx = ox + route.trunk.dx;
    return roundedPath([...head, { x: ox, y: bus0 }, { x: tx, y: bus0 }, { x: tx, y: bus }, { x: c.x, y: bus }, end], 7);
  }
  function forceLayout(ids, links, half) {   // Fruchterman-Reingold with a deterministic start, then names are kept off each other: the same scene settles the same way
    const n = ids.length || 1, k = 150, W = Math.sqrt(n * k * k * 1.9), H = W * 0.66, pos = {};
    ids.forEach((id, i) => { const t = i * 2.399963, r = Math.sqrt((i + 0.5) / n) * Math.min(W, H) * 0.5; pos[id] = { x: r * Math.cos(t) * 1.35, y: r * Math.sin(t) }; });
    const its = n > 160 ? 140 : 280;
    for (let it = 0; it < its; it++) {
      const f = {}, temp = 46 * (1 - it / its) + 0.6; ids.forEach((id) => (f[id] = { x: 0, y: 0 }));
      for (let i = 0; i < ids.length; i++) for (let j = i + 1; j < ids.length; j++) { const a = pos[ids[i]], b = pos[ids[j]], dx = a.x - b.x || 0.01, dy = a.y - b.y || 0.01, d = Math.max(Math.hypot(dx, dy), 6), rp = (k * k) / d; f[ids[i]].x += (dx / d) * rp; f[ids[i]].y += (dy / d) * rp; f[ids[j]].x -= (dx / d) * rp; f[ids[j]].y -= (dy / d) * rp; }
      links.forEach(([s, o]) => { const a = pos[s], b = pos[o]; if (!a || !b) return; const dx = a.x - b.x, dy = a.y - b.y, d = Math.max(Math.hypot(dx, dy), 6), at = (d * d) / (k * 1.6); f[s].x -= (dx / d) * at; f[s].y -= (dy / d) * at; f[o].x += (dx / d) * at; f[o].y += (dy / d) * at; });
      ids.forEach((id) => { const p = pos[id], v = f[id], m = Math.hypot(v.x, v.y) || 1; p.x += (v.x / m) * Math.min(m, temp) - p.x * 0.01; p.y += (v.y / m) * Math.min(m, temp) - p.y * 0.016; });
    }
    for (let it = 0; it < 60; it++) { let moved = false;   // a name needs its width and three lines of height
      for (let i = 0; i < ids.length; i++) for (let j = i + 1; j < ids.length; j++) { const a = pos[ids[i]], b = pos[ids[j]], wx = half(ids[i]) + half(ids[j]) + 8, wy = 46, dx = b.x - a.x, dy = b.y - a.y, ox = wx - Math.abs(dx), oy = wy - Math.abs(dy);
        if (ox > 0 && oy > 0) { moved = true; if (ox * 0.6 < oy) { const m = (ox / 2) * (dx < 0 ? -1 : 1); a.x -= m; b.x += m; } else { const m = (oy / 2) * (dy < 0 ? -1 : 1); a.y -= m; b.y += m; } } }
      if (!moved) break; }
    return pos;
  }
  function radialLayout(centre, ids, links) {   // ring by ring around what is selected (the levels, when nothing is): what is one link away, then two, each ring wide enough for its names
    const adj = {}; ids.forEach((id) => (adj[id] = [])); links.forEach(([a, b]) => { if (adj[a] && adj[b]) { adj[a].push(b); adj[b].push(a); } });
    const ring = {}, rings = [centre.filter((c) => adj[c])]; rings[0].forEach((c) => (ring[c] = 0));
    for (let d = 0; rings[d] && rings[d].length; d++) { const next = []; rings[d].forEach((id) => adj[id].forEach((x) => { if (ring[x] === undefined) { ring[x] = d + 1; next.push(x); } })); if (next.length) rings.push(next); else break; }
    const rest = ids.filter((id) => ring[id] === undefined); if (rest.length) rings.push(rest);
    const pos = {}; let r = 0;
    rings.forEach((list, d) => { r = d === 0 ? (list.length > 1 ? Math.max(130, (list.length * 215) / (2 * Math.PI)) : 0) : Math.max(r + 150, (list.length * 96) / (2 * Math.PI));
      list.forEach((id, i) => { const a = -Math.PI / 2 + (2 * Math.PI * (i + (d % 2 ? 0.5 : 0))) / list.length; pos[id] = { x: r * Math.cos(a) * 1.25, y: r * Math.sin(a) }; }); });
    return pos;
  }

  /* The explorer: the knowledge graph drawn as KB Forge's Explore screen draws its level map. The levels are boxes on a spine, each
     heads the entities it opened; an entity is a circle in its level's colour with its name under it and +N for what is still to open below
     it; a double-click drills down. The same scene in three layouts (Layered: the tidy tree; Force; Radial), on a canvas that pans and zooms. */
  let kLay = null, kTap = { id: null, t: 0 };
  const kWide = () => (S.k.cw || Math.max(640, document.documentElement.clientWidth - 54));
  const kLeftOpen = () => (S.k.left === null ? kWide() >= 1180 : S.k.left);   // on a narrow canvas the filters start closed, so the graph has the room
  const kArea = () => ({ w: kWide(), h: Math.max(500, window.innerHeight - 188), left: kLeftOpen() ? 306 : 14, right: S.k.e || S.k.grp ? 396 : 14, top: 80, bottom: 46 });
  function kScene() {   // what is on the scene, as a forest: the levels' boxes, the heads each opened, and under each entity what was drilled open
    const m = S.k.m, sel = S.k.e ? S.k.e.entity.id : null, keep = new Set(S.k.e ? S.k.e.trail.map((x) => x.id) : []), hide = new Set(S.k.hideT || []), by = {}, shown = {};
    m.nodes.forEach((n) => (by[n.id] = n));
    const isShown = (n) => { if (shown[n.id] === undefined) { shown[n.id] = true; shown[n.id] = (!hide.has(n.type) || keep.has(n.id) || n.id === sel) && (!n.from || !by[n.from] || isShown(by[n.from])); } return shown[n.id]; };
    const nodes = m.nodes.filter(isShown), ids = new Set(nodes.map((n) => n.id)), kids = {}, tops = [], boxes = m.levels.map((l) => "level:" + l.id);
    nodes.forEach((n) => { const p = n.from && ids.has(n.from) ? n.from : !n.from && m.open.includes(n.level) ? "level:" + n.level : null; if (p) (kids[p] = kids[p] || []).push(n.id); else tops.push(n.id); });
    Object.keys(kids).forEach((p) => { const order = []; kids[p].forEach((c) => { if (!order.includes(by[c].type)) order.push(by[c].type); }); kids[p] = kids[p].map((c, i) => [c, i]).sort((a, b) => order.indexOf(by[a[0]].type) - order.indexOf(by[b[0]].type) || a[1] - b[1]).map((x) => x[0]); });
    const parentOf = {}; Object.keys(kids).forEach((p) => kids[p].forEach((c) => (parentOf[c] = p)));
    return { m, by, nodes, ids, kids, tops, boxes, parentOf, trail: keep, sel };
  }
  function kLayout(sc) {
    const a = kArea(), free = { w: Math.max(320, a.w - a.left - 396), h: Math.max(260, a.h - a.top - a.bottom) }, mode = S.k.layout || "layered";   // laid out for the canvas with the inspector open, so selecting an entity moves nothing
    const key = [mode, Math.round(free.w / 40), Math.round(free.h / 40), sc.m.open.join(","), sc.nodes.map((n) => n.id + ">" + (sc.parentOf[n.id] || "")).join("|"), S.k.every ? 1 : 0, mode === "radial" ? (sc.sel || S.k.grp || "") : ""].join("#");
    if (kLay && kLay.key === key) return kLay;
    const tn = {}, node = (id) => tn[id] || (tn[id] = id.startsWith("level:") ? { r: GH / 2, half: GW / 2, below: GH / 2 } : tidyNode(kShort(sc.by[id].name), nodeRadius(sc.by[id].degree)));
    const childrenOf = (id) => sc.kids[id] || [];
    let pos = {}, routes = {}, captions = [];
    if (mode === "layered") { const out = tidyLayout({ tops: sc.tops, boxes: sc.boxes, childrenOf, node, area: free, kindOf: (id) => (sc.by[id] ? sc.by[id].type : "level"), caption: (first, n) => plural(sc.by[first].type, n) + " · " + n }); pos = out.pos; routes = out.routes; captions = out.captions; }
    else { const ids = [...sc.boxes, ...sc.nodes.map((n) => n.id)], links = Object.keys(sc.parentOf).map((c) => [sc.parentOf[c], c]);
      if (mode === "radial") { sc.boxes.slice(1).forEach((b, i) => links.push([sc.boxes[i], b])); pos = radialLayout(sc.sel && sc.ids.has(sc.sel) ? [sc.sel] : S.k.grp ? ["level:" + S.k.grp] : sc.boxes, ids, links); }
      else { if (S.k.every) sc.m.links.forEach((l) => { if (sc.ids.has(l.s) && sc.ids.has(l.o)) links.push([l.s, l.o]); }); pos = forceLayout(ids, links, (id) => node(id).half); } }
    return (kLay = { key, pos, routes, captions, node, mode });
  }
  function kBox(lay, ids) { let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity; ids.forEach((id) => { const p = lay.pos[id], n = lay.node(id); if (!p) return; x0 = Math.min(x0, p.x - n.half); x1 = Math.max(x1, p.x + n.half); y0 = Math.min(y0, p.y - n.r - 8); y1 = Math.max(y1, p.y + n.below); });
    (lay.captions || []).forEach((c) => { if (ids.includes(c.anchor) && lay.pos[c.anchor]) x0 = Math.min(x0, lay.pos[c.anchor].x + c.dx - textWidth(c.text, 10) - 4); });   // a kind's caption, left of its line, is part of what is shown
    return { x0, y0, x1, y1 }; }
  function kFit(lay, sc, focus, only, most) {   // the transform that shows the scene (or only these) in what the panels leave free of the canvas
    const a = kArea(), mg = 20, aw = Math.max(120, a.w - a.left - a.right - 2 * mg), ah = Math.max(120, a.h - a.top - a.bottom - 2 * mg), ids = (only || Object.keys(lay.pos)).filter((id) => lay.pos[id]);
    if (!ids.length) return { x: a.w / 2, y: a.h / 2, k: 1 };
    const at = (b, k) => ({ k, x: a.left + mg + aw / 2 - ((b.x0 + b.x1) / 2) * k, y: a.top + mg + ah / 2 - ((b.y0 + b.y1) / 2) * k }), fitK = (b) => Math.min(aw / Math.max(1, b.x1 - b.x0), ah / Math.max(1, b.y1 - b.y0));
    const all = kBox(lay, ids), k = Math.min(most || 1.2, fitK(all));
    if (only || k >= 0.62 || !focus || !lay.pos[focus]) return at(all, Math.max(0.15, k));
    if (sc && lay.mode === "layered") {   // a scene too large to read whole: what was asked for with what it opened, else with what it sits among, at a size that reads
      const own = (sc.kids[focus] || []).length ? kBox(lay, [focus, ...sc.kids[focus]]) : null, up = sc.parentOf[focus], among = up ? kBox(lay, [up, ...(sc.kids[up] || [])]) : null;
      if (own && fitK(own) >= 0.55) return at(own, Math.min(1, fitK(own)));
      if (among && fitK(among) >= 0.55) return at(among, Math.min(1, fitK(among))); }
    const p = lay.pos[focus]; return at({ x0: p.x, x1: p.x, y0: p.y + 40, y1: p.y + 40 }, 0.85);
  }
  const kInView = (lay, ids, t) => { const a = kArea(); return ids.every((id) => { const p = lay.pos[id], n = lay.node(id); if (!p) return true; return (p.x - n.half) * t.k + t.x >= a.left && (p.x + n.half) * t.k + t.x <= a.w - a.right && (p.y - n.r) * t.k + t.y >= a.top && (p.y + n.below) * t.k + t.y <= a.h - a.bottom; }); };
  const kTfText = (t) => "translate(" + t.x.toFixed(1) + " " + t.y.toFixed(1) + ") scale(" + t.k.toFixed(4) + ")";
  function kApplyTf() { const svg = document.querySelector("svg.gx-svg"); if (!svg || !S.k.tf) return; svg.querySelector("#gx-view").setAttribute("transform", kTfText(S.k.tf)); svg.querySelector("#gx-grid").setAttribute("patternTransform", kTfText(S.k.tf)); }
  function kZoom(f, at) { const a = kArea(), t = S.k.tf; if (!t) return; S.k.focus = null; S.k.auto = false; const mx = at ? at.x : (a.left + a.w - a.right) / 2, my = at ? at.y : (a.top + a.h - a.bottom) / 2, k2 = Math.min(3, Math.max(0.15, t.k * f)); S.k.tf = { k: k2, x: mx - (mx - t.x) * (k2 / t.k), y: my - (my - t.y) * (k2 / t.k) }; kApplyTf(); }
  const kAnchor = (id) => { const p = kLay && kLay.pos[id], t = S.k.tf; S.k.focus = null; S.k.anchor = p && t ? { id, sx: p.x * t.k + t.x, sy: p.y * t.k + t.y } : null; };
  const kUnder = (roots) => { const under = new Set(roots); let grew = true; while (grew) { grew = false; S.k.m.nodes.forEach((x) => { if (x.from && under.has(x.from) && !under.has(x.id)) { under.add(x.id); grew = true; } }); } return under; };
  const kDrill = (n, want) => run(async () => {   // open what is below an entity, or take it off the scene again (with everything opened under it); either way it is the one in the inspector
    const isOpen = S.k.drill.includes(n.id), other = !S.k.e || S.k.e.entity.id !== n.id;
    if (want === undefined ? isOpen : !want) { if (isOpen) { const under = kUnder([n.id]); under.delete(n.id); S.k.drill = S.k.drill.filter((x) => x !== n.id && !under.has(x)); if (S.k.e && under.has(S.k.e.entity.id)) S.k.e = null; } }
    else if (n.below && !isOpen) S.k.drill = [...S.k.drill, n.id];
    else if (!other) return;
    kAnchor(n.id); S.k.grew = n.id;
    const [m, e] = await Promise.all([api("knowledge/map?" + mapQuery()), other || !S.k.e ? api("knowledge/entity?id=" + encodeURIComponent(n.id)) : S.k.e]);
    S.k.m = m; S.k.e = e; S.k.grp = null; S.k.more = null; kHash(n.id); render();
  });
  const kLevel = (id, want) => run(async () => {   // open a level to the entities that head it, or close it (with everything drilled open under them)
    const isOpen = S.k.open.includes(id);
    if (want === undefined ? isOpen : !want) { if (!isOpen) return; const under = kUnder(S.k.m.nodes.filter((x) => !x.from && x.level === id).map((x) => x.id)); S.k.drill = S.k.drill.filter((x) => !under.has(x)); S.k.open = S.k.open.filter((x) => x !== id); if (S.k.e && under.has(S.k.e.entity.id)) { S.k.e = null; kHash(null); } }
    else if (!isOpen) S.k.open = [...S.k.open, id]; else return;
    kAnchor("level:" + id); S.k.grew = "level:" + id; await kMapNow();
  });
  const kTop = () => run(async () => { S.k.open = S.k.ov.levels.map((l) => l.id); S.k.drill = []; S.k.e = null; S.k.grp = null; S.k.hideT = []; S.k.tf = null; S.k.auto = true; S.k.focus = null; S.k.anchor = null; kHash(null); await kMapNow(); });
  function kCanvas(sc, lay) {
    const m = sc.m, a = kArea(), sel = sc.sel, grp = S.k.grp, onTrail = sc.trail, pos = lay.pos, tidy = lay.mode === "layered", lv = Object.fromEntries(m.levels.map((l) => [l.id, l]));
    const frame = a.left + ":" + a.right + ":" + a.w + ":" + a.h;
    if (S.k.anchor && pos[S.k.anchor.id] && S.k.tf) {   // after opening or closing something: a scene that still reads whole is shown whole; a larger one keeps what was opened where it was on the screen
      const whole = S.k.auto ? kFit(lay, sc, null) : null;
      if (whole && whole.k >= 0.62) { S.k.tf = whole; S.k.frame = frame; S.k.grew = null; }
      else { const p = pos[S.k.anchor.id], t = S.k.tf; S.k.tf = { k: t.k, x: S.k.anchor.sx - p.x * t.k, y: S.k.anchor.sy - p.y * t.k }; S.k.auto = false; } }
    S.k.anchor = null;
    if (!S.k.tf || (S.k.auto && S.k.frame !== frame)) { S.k.tf = kFit(lay, sc, S.k.focus); S.k.auto = true; S.k.frame = frame; }   // a view nobody moved follows the panels: the scene stays in what they leave free
    if (S.k.grew && pos[S.k.grew]) { const fam = [S.k.grew, ...(sc.kids[S.k.grew] || [])];   // and what it opened is brought into view: whole when that still reads, else from its top
      if (!kInView(lay, fam, S.k.tf)) { const t = kFit(lay, sc, null, fam, S.k.tf.k); if (t.k >= 0.55) S.k.tf = t; else { const b = kBox(lay, fam), k = Math.max(0.55, Math.min(S.k.tf.k, 1)), p = pos[S.k.grew], wide = (b.x1 - b.x0) * k > a.w - a.left - a.right - 40; S.k.tf = { k, x: (a.left + a.w - a.right) / 2 - (wide ? p.x : (b.x0 + b.x1) / 2) * k, y: a.top + 34 - b.y0 * k }; } } }
    S.k.grew = null;
    if (S.k.reveal && pos[S.k.reveal]) { const p = pos[S.k.reveal], n = lay.node(S.k.reveal), t = S.k.tf, x0 = (p.x - n.half) * t.k + t.x, x1 = (p.x + n.half) * t.k + t.x, y0 = (p.y - n.r) * t.k + t.y, y1 = (p.y + n.below) * t.k + t.y;   // what was selected is not left under a panel
      const dx = x1 > a.w - a.right - 8 ? a.w - a.right - 8 - x1 : x0 < a.left + 8 ? a.left + 8 - x0 : 0, dy = y1 > a.h - a.bottom ? a.h - a.bottom - y1 : y0 < a.top ? a.top - y0 : 0; if (dx || dy) S.k.tf = { k: t.k, x: t.x + dx, y: t.y + dy }; }
    S.k.reveal = null;
    const tf = S.k.tf, isBox = (id) => id.startsWith("level:"), colorOf = (id) => (isBox(id) ? LCOL[id.slice(6)] : LCOL[sc.by[id].level]) || "#7a8496", rOf = (id) => lay.node(id).r;
    const relBetween = (p, c) => m.links.filter((l) => (l.s === p && l.o === c) || (l.s === c && l.o === p));
    const hideR = new Set(S.k.hideR || []);
    const tap = (id, one, two) => (ev) => {   // one press shows it in the inspector; a second within a moment drills. The inspector waits that moment, so it never opens under the second press
      ev.stopPropagation(); const now = Date.now(); clearTimeout(kTap.timer);
      if (kTap.id === id && now - kTap.t < 450) { kTap = { id: null, t: 0 }; two(); } else kTap = { id, t: now, timer: setTimeout(() => { kTap = { id: null, t: 0 }; one(); }, 260) }; };
    // the tree's connectors (Layered), or straight links (Force, Radial)
    const treeLinks = Object.keys(sc.parentOf).filter((c) => pos[c] && pos[sc.parentOf[c]]).map((c) => { const p = sc.parentOf[c], path = onTrail.has(c) && (onTrail.has(p) || isBox(p)), strong = c === sel || p === sel; return { c, p, path, strong }; }).sort((x, y) => Number(x.path) - Number(y.path) || Number(x.strong) - Number(y.strong));
    const linkWords = {}; if (S.k.labels !== false) treeLinks.forEach(({ c, p }) => { if (isBox(p)) return; relBetween(p, c).forEach((l) => { const w = (l.s === c ? "↑ " : "") + words(l.p); (linkWords[p] = linkWords[p] || []).includes(w) || linkWords[p].push(w); }); });
    const straight = (aId, bId) => { const A = pos[aId], B = pos[bId], dx = B.x - A.x, dy = B.y - A.y, len = Math.hypot(dx, dy) || 1, ra = isBox(aId) ? 0 : rOf(aId), rb = (isBox(bId) ? 0 : rOf(bId)) + 3; return { d: "M" + (A.x + (dx / len) * ra).toFixed(1) + "," + (A.y + (dy / len) * ra).toFixed(1) + " L" + (B.x - (dx / len) * rb).toFixed(1) + "," + (B.y - (dy / len) * rb).toFixed(1), mx: (A.x + B.x) / 2, my: (A.y + B.y) / 2 }; };
    const pairs = new Set(treeLinks.map(({ c, p }) => p + "|" + c)), loose = S.k.every ? m.links.filter((l) => pos[l.s] && pos[l.o] && sc.ids.has(l.s) && sc.ids.has(l.o) && !hideR.has(l.p) && !pairs.has(l.s + "|" + l.o) && !pairs.has(l.o + "|" + l.s)) : [];
    const drawn = treeLinks.filter(({ p }) => !isBox(p)).length + loose.length;
    const view = h("g", { id: "gx-view", transform: kTfText(tf) },
      tidy ? sc.boxes.slice(0, -1).map((b, i) => h("line", { x1: pos[b].x, y1: pos[b].y + GH / 2, x2: pos[sc.boxes[i + 1]].x, y2: pos[sc.boxes[i + 1]].y - GH / 2, class: "gx-chain-link", "stroke-width": 3, opacity: 0.3, style: "stroke:" + colorOf(b) })) : null,
      treeLinks.map(({ c, p, path, strong }) => { const box = isBox(p), cls = "gx-tree-link" + (path ? " path" : strong ? " strong" : "") + (box ? " box" : ""), rel = relBetween(p, c), title = box ? lv[p.slice(6)].name + " level → " + sc.by[c].name : rel.map((l) => sc.by[l.s].name + " → " + words(l.p) + " → " + sc.by[l.o].name).join("\n");
        if (tidy && lay.routes[c]) return h("path", { d: routePath(lay.routes[c], pos[p], pos[c], lay.node(p), lay.node(c)), fill: "none", class: cls, "stroke-width": path ? 2.6 : strong ? 1.9 : 1.4, "stroke-dasharray": box ? "5 4" : null, style: box && !path ? "stroke:" + colorOf(p) : null }, title ? h("title", null, title) : null);
        const up = rel.length && rel[0].s === c, g = up ? straight(c, p) : straight(p, c);
        return h("g", null, h("path", { d: g.d, fill: "none", class: (box ? "gx-group-link" : "gx-edge-line") + (path ? " path" : strong ? " strong" : ""), "stroke-width": path ? 2.6 : strong ? 1.8 : 1.2, "stroke-dasharray": box ? "4 3" : null, style: box && !path ? "stroke:" + colorOf(p) : null, "marker-end": box ? null : "url(#gx-arrow" + (path ? "-path" : strong ? "-strong" : "") + ")" }, title ? h("title", null, title) : null),
          !box && S.k.labels !== false && rel.length && (strong || path || sc.nodes.length <= 40) ? h("text", { x: g.mx, y: g.my - 4, "font-size": 10, "text-anchor": "middle", class: "gx-edge-text" + (path ? " path" : strong ? " strong" : "") }, words(rel[0].p)) : null); }),
      tidy ? lay.captions.filter((c) => pos[c.anchor]).map((c) => h("text", { x: pos[c.anchor].x + c.dx, y: pos[c.anchor].y + 3.5, "font-size": 10, "text-anchor": "end", class: "gx-kind-caption" }, c.text)) : null,
      tidy ? Object.keys(linkWords).filter((p) => pos[p]).map((p) => h("text", { x: pos[p].x + 7, y: pos[p].y + lay.node(p).below + BUS_GAP - 8, "font-size": 10, class: "gx-edge-text gx-tree-words" }, linkWords[p].slice(0, 2).join(" · ") + (linkWords[p].length > 2 ? " …" : ""))) : null,
      loose.map((l) => { const g = straight(l.s, l.o), strong = l.s === sel || l.o === sel; return h("g", { opacity: strong || !sel ? 1 : 0.45 }, h("path", { d: g.d, fill: "none", class: "gx-edge-line" + (strong ? " sel" : ""), "stroke-width": strong ? 1.8 : 1.1, "marker-end": "url(#gx-arrow" + (strong ? "-sel" : "") + ")" }, h("title", null, sc.by[l.s].name + " → " + words(l.p) + " → " + sc.by[l.o].name)),
        S.k.labels !== false && (strong || loose.length <= 30) ? h("text", { x: g.mx, y: g.my - 4, "font-size": 10, "text-anchor": "middle", class: "gx-edge-text" + (strong ? " sel" : "") }, words(l.p)) : null); }),
      sc.nodes.filter((n) => pos[n.id]).map((n) => { const p = pos[n.id], r = rOf(n.id), isSel = n.id === sel, path = onTrail.has(n.id) && !isSel, color = colorOf(n.id), more = n.drilled ? n.more : n.below, ring = (n.also || []).length ? LCOL[n.also[0]] : null;
        return h("g", { "data-node-id": n.id, transform: "translate(" + p.x.toFixed(1) + " " + p.y.toFixed(1) + ")", style: "cursor:pointer", onpointerdown: tap(n.id, kSelect(n.id), kDrill(n)) },
          h("title", null, words(n.type) + ": " + n.name + (n.below ? " · " + n.below + " below it. Double-click to " + (n.drilled ? "close it." : "drill down.") : "")),
          isSel || path ? h("circle", { r: r + 5, fill: "none", class: isSel ? "gx-ring-sel" : "gx-ring-path", "stroke-width": 2.5 }) : null,
          ring ? h("circle", { r: r + 3.5, fill: "none", "stroke-width": 3, style: "stroke:" + ring }) : null,
          h("circle", { r, class: "gx-node-circle" + (n.stub ? " stub" : ""), "stroke-width": 2, style: "fill:" + color }),
          more > 0 ? h("g", { transform: "translate(" + (r * 0.75).toFixed(1) + " " + (-r * 0.75).toFixed(1) + ")" }, h("circle", { r: 8.5, class: "gx-more-badge", "stroke-width": 1.5, style: "stroke:" + color }), h("text", { "text-anchor": "middle", dy: "0.35em", "font-size": more > 99 ? 6.5 : 8, "font-weight": 700, class: "gx-more-text" }, more > 99 ? "99+" : "+" + more)) : null,
          h("text", { y: r + 14, "text-anchor": "middle", "font-size": 11, "font-weight": isSel ? 650 : 500, class: "gx-node-text" }, labelLines(kShort(n.name)).map((line, i) => h("tspan", { x: 0, dy: i ? 12.5 : 0 }, line)))); }),
      sc.boxes.filter((b) => pos[b]).map((b) => { const l = lv[b.slice(6)], p = pos[b], open = (sc.kids[b] || []).length, more = l.heads - open, isSel = grp === l.id, color = colorOf(b);
        const counts = l.entities ? l.entities.toLocaleString() + (l.entities === 1 ? " entity" : " entities") + (l.heads && l.heads !== l.entities ? " · " + l.heads + " head" : "") + (open ? " · " + open + " open" : "") : "nothing yet";
        return h("g", { "data-group-id": b, transform: "translate(" + p.x.toFixed(1) + " " + p.y.toFixed(1) + ")", class: "gx-group" + (open ? " open" : "") + (l.entities ? "" : " empty"), style: "cursor:pointer", role: "button", "aria-label": l.name + " level, " + counts,
            onpointerdown: tap(b, () => { S.k.grp = l.id; S.k.e = null; kHash(null); render(); }, kLevel(l.id)) },
          h("title", null, l.name + " — " + l.say + (l.entities ? " Double-click to " + (open ? "close." : "open.") : "")),
          isSel ? h("rect", { x: -GW / 2 - 4, y: -GH / 2 - 4, width: GW + 8, height: GH + 8, rx: 14, fill: "none", class: "gx-ring-sel", "stroke-width": 2.5 }) : null,
          h("rect", { x: -GW / 2, y: -GH / 2, width: GW, height: GH, rx: 12, class: "gx-group-box", "stroke-width": open ? 2.5 : 1.8, style: "stroke:" + color }), h("rect", { x: -GW / 2, y: -GH / 2, width: 8, height: GH, rx: 4, class: "gx-group-bar", style: "fill:" + color }),
          h("text", { x: -GW / 2 + 18, y: -3, "font-size": 12.5, "font-weight": 700, class: "gx-group-title" }, l.name), h("text", { x: -GW / 2 + 18, y: 13, "font-size": 9.5, class: "gx-group-sub" }, counts),
          more > 0 ? h("g", { transform: "translate(" + (GW / 2 - 4) + " " + (-GH / 2 + 4) + ")" }, h("circle", { r: 10, class: "gx-more-badge", "stroke-width": 1.5, style: "stroke:" + color }), h("text", { "text-anchor": "middle", dy: "0.35em", "font-size": more > 99 ? 7 : 9, "font-weight": 700, class: "gx-more-text" }, more > 99 ? "99+" : "+" + more)) : null); }));
    const svg = h("svg", { class: "gx-svg", role: "img", "aria-label": "The knowledge graph" },
      h("defs", null, h("pattern", { id: "gx-grid", width: 28, height: 28, patternUnits: "userSpaceOnUse", patternTransform: kTfText(tf) }, h("circle", { cx: 1, cy: 1, r: 1, class: "gx-grid-dot" })),
        ["", "strong", "sel", "path"].map((v) => h("marker", { id: "gx-arrow" + (v ? "-" + v : ""), viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 9, markerHeight: 9, markerUnits: "userSpaceOnUse", orient: "auto-start-reverse" }, h("path", { d: "M 0 0 L 10 5 L 0 10 z", class: "gx-arrow-fill " + v })))),
      h("rect", { width: "100%", height: "100%", fill: "url(#gx-grid)" }), view);
    let drag = null;   // the canvas pans by dragging and zooms on the wheel, without drawing the page again
    svg.addEventListener("pointerdown", (ev) => { if (ev.button !== 0) return; drag = { x: ev.clientX, y: ev.clientY, tx: S.k.tf.x, ty: S.k.tf.y }; try { svg.setPointerCapture(ev.pointerId); } catch (err) { /* not capturable */ } if (S.k.hits) { S.k.hits = null; const box = document.querySelector(".gx-search-list"); if (box) box.remove(); } });
    svg.addEventListener("pointermove", (ev) => { if (!drag) return; S.k.focus = null; S.k.auto = false; S.k.tf = { k: S.k.tf.k, x: drag.tx + ev.clientX - drag.x, y: drag.ty + ev.clientY - drag.y }; kApplyTf(); });
    const drop = () => { drag = null; }; svg.addEventListener("pointerup", drop); svg.addEventListener("pointercancel", drop);
    svg.addEventListener("wheel", (ev) => { ev.preventDefault(); const r = svg.getBoundingClientRect(); kZoom(Math.exp(-ev.deltaY * (ev.ctrlKey ? 0.01 : 0.0016)), { x: ev.clientX - r.left, y: ev.clientY - r.top }); }, { passive: false });
    return { svg, drawn };
  }
  const KSKIP = ["description", "graphLevel", "knowledgeLevel", "knowledgeSpace", "knowledgeSpaceId", "graphLevelIn", "citation", "jurisdiction", "title", "reference", "rationale", "status"];
  async function addUnder(e) {   // a fact by hand hangs under the node that is open, by a link the ontology has for the two kinds
    const v = await form("Add under " + e.entity.name, [{ k: "what", label: "What it is", options: e.attach.map((o, i) => [String(i), o.label]) }, { k: "name", label: "Name, as it should read in the graph" },
      { k: "description", label: "What it says", type: "textarea" }, { k: "source", label: "Where it is stated (a document and section, or a link)" }], "Add to the knowledge graph");
    if (!v) return;
    if (!v.name.trim()) { toast("Give it a name."); return; }
    const o = e.attach[+v.what], out = await api("knowledge/facts", { type: o.type, name: v.name, description: v.description, source: v.source, links: [{ to: e.entity.id, relation: o.relation, direction: o.direction }] });
    toast("Added by hand: " + out.fact.name); S.k.ov = await api("knowledge"); await kOpen(out.fact.entity);
  }
  function copyLink(id) {
    const url = kLink(id), done = () => toast("Link to this node copied");
    const old = () => { const t = h("textarea", { style: "position:fixed;opacity:0" }, url); document.body.appendChild(t); t.select(); try { document.execCommand("copy"); done(); } catch (err) { toast(url); } t.remove(); };
    if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(url).then(done, old); else old();
  }
  const levelBadge = (id, name) => h("span", { class: "level-badge", style: "--lv:" + (LCOL[id] || "#7a8496") }, h("i", { class: "kdot", style: "width:7px;height:7px;background:" + (LCOL[id] || "#7a8496") }), name);
  function kInspector(sc) {   // the inspector, as KB Forge's: where the entity sits and what drilling does, then what it is, what it rests on, where it is used, its links and its source
    const ov = S.k.ov, e = S.k.e, p = e.entity.props, n = sc.by[e.entity.id], levelName = Object.fromEntries(ov.levels.map((l) => [l.id, l.name]));
    const REL = Object.fromEntries(ov.relationships.map((r) => [r.name, r])), u = e.usage, up = n && sc.parentOf[n.id], upBox = up && up.startsWith("level:"), upName = up ? (upBox ? "the " + levelName[up.slice(6)] + " level" : kShort(sc.by[up].name)) : null;
    const groups = {}; e.links.forEach((l) => { const k = l.direction + ":" + l.predicate; (groups[k] = groups[k] || []).push(l); });
    const linkLabel = (dir, pred) => (dir === "out" ? "→ " + words(pred) : "← " + ((REL[pred] || {}).inverse && REL[pred].inverse !== pred ? words(REL[pred].inverse) : words(pred)));
    const lists = Object.entries(p).filter(([k, v]) => Array.isArray(v) && v.length > 1 && !KSKIP.includes(k));
    const cite = [p.title, p.citation, p.reference && !String(p.citation || "").includes(p.reference) ? p.reference : null].filter(Boolean).join(" · ");
    const row = (x, right) => h("button", { class: "lrowb", type: "button", onclick: kShow(x.id) }, dot(x.type, x.level), h("span", null, x.name), h("em", null, right || words(x.type)));
    const open = n && n.drilled ? (sc.kids[n.id] || []).length : 0;
    return h("section", { class: "gx-panel gx-right-panel" }, h("div", { class: "gx-panel-head" }, h("h2", null, "Entity"), h("button", { class: "gx-icon-btn plain", type: "button", "aria-label": "Close the inspector", title: "Close the inspector", onclick: () => { S.k.e = null; kHash(null); render(); } }, ico("close", 14))),
      h("div", { class: "gx-panel-body" },
        n ? h("div", { class: "gx-drill" }, h("p", null, h("strong", null, words(e.entity.type).replace(/^./, (c) => c.toUpperCase())), " in the level map" + (upName ? ", under " + upName : "") + ". " + (n.below ? "Below it: " + n.below + "." : "Nothing below it.")),
          h("div", { class: "btn-row" }, h("button", { class: "btn sm primary", type: "button", disabled: !n.below || n.drilled, title: "Open what is under it (or double-click the node)", onclick: kDrill(n, true) }, ico("down"), " Drill down"),
            up ? h("button", { class: "btn sm", type: "button", title: "Up to " + upName, onclick: upBox ? () => { S.k.grp = up.slice(6); S.k.e = null; kHash(null); render(); } : kSelect(up) }, ico("up"), " Up to " + (upBox ? upName : upName.length > 24 ? upName.slice(0, 23) + "…" : upName)) : null,
            n.drilled ? h("button", { class: "btn sm ghost", type: "button", title: "Take what is open under it off the scene", onclick: kDrill(n, false) }, "Collapse −" + open) : null, h("button", { class: "btn sm ghost", type: "button", title: "Back to the levels and the entities that head them", onclick: kTop() }, ico("home"), " Top"))) : null,
        h("div", { class: "gx-details" },
          h("div", null, h("div", { class: "gx-title-row" }, h("i", { class: "kdot", style: "width:11px;height:11px;background:" + (LCOL[e.level] || "#7a8496") }), h("span", { class: "gx-title" }, e.entity.name)),
            h("div", { class: "gx-badges" }, h("span", { class: "badge", title: e.class.definition || "" }, words(e.entity.type).replace(/^./, (c) => c.toUpperCase())), h("span", { class: "badge info" }, (ov.origin_words[e.origin] || e.origin).replace(/^./, (c) => c.toUpperCase())),
              levelBadge(e.level, levelName[e.level] || "Added knowledge"), (e.also_at || []).map((x) => levelBadge(x, levelName[x] || x)), p.status ? pill(p.status) : null)),
          h("div", null, p.description ? h("p", { class: "small" }, p.description) : null,
            cite ? h("p", { class: "small", style: "margin-top:6px" }, h("b", null, "Cited as: "), cite) : null, p.jurisdiction ? h("p", { class: "small muted" }, p.jurisdiction) : null,
            p.rationale ? h("p", { class: "small", style: "margin-top:6px" }, h("b", null, "Why: "), p.rationale) : null,
            e.origin === "curated" ? h("p", { class: "knote" }, "A short summary written for this knowledge base, not the text of the source. Read the current text before relying on it.") : null,
            e.entity.synonyms.length ? h("p", { class: "tiny faint", style: "margin-top:6px" }, "Also known as: " + e.entity.synonyms.join(", ")) : null,
            h("p", { class: "tiny faint", style: "margin-top:4px" }, "The way to it: " + [levelName[e.trail[0].level] || "Added", ...e.trail.map((x) => kShort(x.name))].join(" › "))),
          lists.length || Object.keys(p).some((k) => !KSKIP.includes(k) && p[k] !== null && typeof p[k] !== "object") ? h("div", null, h("h3", { class: "gx-sub" }, "Attributes"), lists.map(([k, v]) => [h("div", { class: "tiny faint", style: "margin-top:4px" }, words(k)), steps(v)]),
            kv(Object.entries(p).filter(([k, v]) => !KSKIP.includes(k) && v !== null && (typeof v !== "object" || (Array.isArray(v) && v.length < 2))).map(([k, v]) => [words(k), Array.isArray(v) ? v.join("; ") : String(v)]))) : null,
          e.basis.length ? h("div", null, h("h3", { class: "gx-sub" }, e.rule ? "What its rule rests on" : "What this rule rests on"), e.rule ? row(e.rule, "policy rule") : null,
            e.basis.map((b) => h("button", { class: "kbasis", type: "button", style: "border-left-color:" + (LCOL[b.level] || "#7a8496"), onclick: kShow(b.id) }, h("b", null, b.name), h("span", { class: "muted" }, " · " + (b.of ? b.of.name : words(b.type)) + (b.industries.length ? " · " + b.industries.join(", ") : "")), h("div", null, b.says)))) : null,
          u.rules.length || u.findings ? h("div", null, h("h3", { class: "gx-sub" }, "Where it is used"),
            u.rules.length ? [h("div", { class: "tiny faint", style: "margin-bottom:4px" }, u.rules.length + (u.rules.length === 1 ? " rule rests" : " rules rest") + " on it"), (S.k.more === "rules" ? u.rules : u.rules.slice(0, 5)).map((r) => row(r)),
              u.rules.length > 5 ? h("button", { class: "btn sm ghost", onclick: () => { S.k.more = S.k.more === "rules" ? null : "rules"; render(); } }, S.k.more === "rules" ? "Show fewer" : "Show all " + u.rules.length) : null] : null,
            u.findings ? [h("div", { class: "row", style: "margin:8px 0 4px;gap:6px" }, h("span", { class: "tiny faint" }, u.findings + (u.findings === 1 ? " finding" : " findings") + " on " + u.cases.length + (u.cases.length === 1 ? " case" : " cases")), statusPills(u.by_status)),
              u.cases.slice(0, 6).map((c) => h("div", { class: "kcase" }, h("button", { class: "btn sm ghost mono", type: "button", title: "Open the case", onclick: run(() => open(c.name, "provenance")) }, c.name),
                c.findings.slice(0, 8).map((f) => h("button", { class: "pill " + f.status, type: "button", title: f.name + ": go to this finding in the graph", onclick: kShow(f.id) }, f.status)), c.findings.length > 8 ? h("span", { class: "tiny faint" }, "+" + (c.findings.length - 8)) : null)),
              u.cases.length > 6 ? h("p", { class: "tiny faint" }, "and " + (u.cases.length - 6) + " more case(s)") : null] : null) : null,
          h("div", null, h("h3", { class: "gx-sub" }, "Relations ", h("span", { class: "faint" }, "(" + e.links.length + ")")),
            h("ul", { class: "gx-rel-list" }, Object.entries(groups).sort((a, b) => a[0].split(":")[1].localeCompare(b[0].split(":")[1])).map(([k, ls]) => { const [dir, pred] = k.split(":"), all = S.k.more === k;
              return [(all ? ls : ls.slice(0, 4)).map((l) => h("li", { class: "gx-rel" }, h("div", { class: "gx-rel-head" }, h("span", { class: "gx-rel-name", title: (REL[pred] || {}).definition || "" }, linkLabel(dir, pred)), h("button", { class: "linkish", type: "button", onclick: kShow(l.other) }, l.other_name)),
                  h("div", { class: "gx-rel-src" }, words(l.other_type) + " · " + (LVL[l.other_level] || "added").toLowerCase() + " level · " + (ov.origin_words[l.origin] || l.origin)))),
                ls.length > 4 ? h("li", { class: "gx-rel" }, h("button", { class: "btn sm ghost", onclick: () => { S.k.more = all ? null : k; render(); } }, all ? "Show fewer" : "Show all " + ls.length + " " + linkLabel(dir, pred).slice(2))) : null]; }))),
          h("div", null, h("h3", { class: "gx-sub" }, "Where it comes from"), e.support.length ? e.support.slice(0, 4).map((c) => h("div", { class: "src", style: "margin-bottom:4px;cursor:default" }, h("div", { class: "row" }, h("b", null, c.fileName || c.source || c.system || c.app || c.kind), h("span", { class: "muted" }, [c.locator, c.case].filter(Boolean).join(" · ")),
              c.check ? h("span", { class: "right" }, pill({ named: "pass", quoted: "pass", read: "pass", paraphrased: "review", missing: "fail" }[c.check] || "", c.check)) : null), c.quote ? h("q", null, c.quote) : h("span", { class: "muted" }, c.text || ""))) : h("p", { class: "small muted" }, "No source recorded."),
            e.support.length > 4 ? h("p", { class: "tiny faint" }, "and " + (e.support.length - 4) + " more") : null),
          h("div", { class: "btn-row" }, h("button", { class: "btn sm", onclick: () => copyLink(e.entity.id) }, ico("link"), " Copy the link to this node"),
            e.attach.length ? h("button", { class: "btn sm", onclick: run(() => addUnder(e)) }, ico("plus"), " Add under this") : null,
            e.by_hand ? h("button", { class: "btn sm", onclick: run(async () => { await api("knowledge/facts/" + encodeURIComponent(e.by_hand) + "/remove", {}); toast("Taken out: " + e.entity.name); const back = e.trail.length > 1 ? e.trail[e.trail.length - 2].id : null; S.k.ov = await api("knowledge"); if (back) await kOpen(back).catch(() => kReload()); else await kReload(); }) }, "Take out what was added by hand") : null))));
  }
  function kLevelPanel(sc) {
    const l = sc.m.levels.find((x) => x.id === S.k.grp), heads = (sc.kids["level:" + l.id] || []).map((id) => sc.by[id]), isOpen = sc.m.open.includes(l.id);
    return h("section", { class: "gx-panel gx-right-panel" }, h("div", { class: "gx-panel-head" }, h("h2", null, "Level"), h("button", { class: "gx-icon-btn plain", type: "button", "aria-label": "Close the inspector", title: "Close the inspector", onclick: () => { S.k.grp = null; render(); } }, ico("close", 14))),
      h("div", { class: "gx-panel-body" }, h("div", { class: "gx-details" },
        h("div", null, h("div", { class: "gx-title-row" }, h("span", { class: "gx-level-swatch big", style: "background:" + LCOL[l.id] }), h("strong", null, l.name), h("span", { class: "tiny faint" }, "a level of the knowledge")),
          h("p", { class: "small muted", style: "margin:6px 0 2px" }, l.say), h("p", { class: "tiny faint" }, l.entities ? l.entities.toLocaleString() + (l.entities === 1 ? " entity" : " entities") + " at this level; the " + l.heads + " that head it open here, the rest by drilling down from them." : "Nothing at this level yet." + (l.id === "customer" ? " It fills as cases are assessed." : ""))),
        l.entities ? h("div", { class: "gx-drill" }, h("p", null, heads.length + " of " + l.heads + " on the scene."),
          h("div", { class: "btn-row" }, h("button", { class: "btn sm primary", type: "button", disabled: isOpen, onclick: kLevel(l.id, true) }, ico("layers"), " Open"), h("button", { class: "btn sm ghost", type: "button", disabled: !isOpen, onclick: kLevel(l.id, false) }, "Collapse"), h("button", { class: "btn sm ghost", type: "button", onclick: kTop() }, ico("home"), " Top"))) : null,
        heads.length ? h("div", null, h("h3", { class: "gx-sub" }, "Entities ", h("span", { class: "faint" }, "(" + heads.length + ")")), heads.map((x) => h("button", { class: "lrowb", type: "button", onclick: kSelect(x.id) }, dot(x.type, x.level), h("span", null, x.name), h("em", null, words(x.type))))) : null,
        l.classes && l.classes.length ? h("div", null, h("h3", { class: "gx-sub" }, "What is at this level"), h("div", { class: "chips" }, l.classes.map(([c, k]) => h("span", { class: "chip", style: "cursor:default" }, k + " " + words(c))))) : null)));
  }
  function kLeft(sc, drawn) {   // Scene & filters, as KB Forge's: the levels, what is on the scene, and what is shown of it
    const m = sc.m, hide = S.k.hideT || [], hideR = S.k.hideR || [], kinds = {}, rels = {};
    m.nodes.forEach((n) => (kinds[n.type] = (kinds[n.type] || 0) + 1)); m.links.forEach((l) => (rels[l.p] = (rels[l.p] || 0) + 1));
    const total = S.k.ov.index.entities, flip = (list, key, v) => () => { S.k[key] = list.includes(v) ? list.filter((x) => x !== v) : [...list, v]; render(); };
    const check = (key, label, on) => h("label", { class: "gx-check" }, h("input", { type: "checkbox", checked: on, onchange: () => { S.k[key] = !on; render(); } }), label);
    if (!kLeftOpen()) return h("button", { class: "gx-icon-btn gx-filters-btn", type: "button", title: "Scene & filters", "aria-label": "Open scene and filters", onclick: () => { S.k.left = true; render(); } }, ico("filter", 15));
    return h("section", { class: "gx-panel gx-left-panel" }, h("div", { class: "gx-panel-head" }, h("h2", null, "Scene & filters"), h("button", { class: "gx-icon-btn plain", type: "button", "aria-label": "Close the panel", title: "Close the panel", onclick: () => { S.k.left = false; render(); } }, ico("close", 14))),
      h("div", { class: "gx-panel-body" },
        h("div", null, m.levels.map((l) => h("button", { class: "gx-filter-btn", type: "button", title: l.say, onclick: () => { S.k.grp = l.id; S.k.e = null; kHash(null); render(); } }, h("i", { class: "kdot", style: "background:" + LCOL[l.id] }), h("span", { class: "grow" }, l.name), h("span", { class: "num" }, l.entities.toLocaleString()))),
          h("p", { class: "tiny faint", style: "margin:4px 6px 0" }, "a ring: known at a second level too")),
        h("p", { class: "small", style: "margin:0" }, h("b", null, sc.nodes.length), " of " + total.toLocaleString() + " entities on the scene · " + m.links.length + " links (" + drawn + " drawn)"),
        h("div", null, h("h3", { class: "gx-sub" }, "Classes"), h("ul", { class: "gx-list" }, Object.keys(kinds).sort((a, b) => kinds[b] - kinds[a] || a.localeCompare(b)).map((t) => h("li", { class: "gx-filter-row" },
          h("button", { class: "gx-filter-btn" + (hide.includes(t) ? " off" : ""), type: "button", "aria-pressed": String(!hide.includes(t)), title: hide.includes(t) ? "Show this kind" : "Hide this kind", onclick: flip(hide, "hideT", t) }, h("i", { class: "kdot", style: "background:#8a94a6" }), h("span", { class: "grow" }, words(t).replace(/^./, (c) => c.toUpperCase())), h("span", { class: "num" }, kinds[t]), h("span", { class: "gx-eye" }, ico(hide.includes(t) ? "eyeoff" : "eye", 13))),
          h("button", { class: "gx-only", type: "button", title: "Show only this kind", onclick: () => { S.k.hideT = Object.keys(kinds).filter((x) => x !== t); render(); } }, "only")))),
          hide.length ? h("button", { class: "btn sm ghost", type: "button", style: "margin-top:4px", onclick: () => { S.k.hideT = []; render(); } }, "Show every kind") : null),
        Object.keys(rels).length ? h("div", null, h("h3", { class: "gx-sub" }, "Relations"), h("ul", { class: "gx-list" }, Object.keys(rels).sort((a, b) => rels[b] - rels[a] || a.localeCompare(b)).slice(0, S.k.more === "rels" ? 99 : 8).map((r) => h("li", { class: "gx-filter-row" },
          h("button", { class: "gx-filter-btn" + (hideR.includes(r) ? " off" : ""), type: "button", "aria-pressed": String(!hideR.includes(r)), title: "With every link drawn: " + (hideR.includes(r) ? "show" : "hide") + " this kind of link", onclick: flip(hideR, "hideR", r) }, h("span", { class: "grow" }, words(r)), h("span", { class: "num" }, rels[r]), h("span", { class: "gx-eye" }, ico(hideR.includes(r) ? "eyeoff" : "eye", 13)))))),
          Object.keys(rels).length > 8 ? h("button", { class: "btn sm ghost", type: "button", style: "margin-top:4px", onclick: () => { S.k.more = S.k.more === "rels" ? null : "rels"; render(); } }, S.k.more === "rels" ? "Show fewer" : "All " + Object.keys(rels).length + " kinds of link") : null) : null,
        h("div", null, check("labels", "Relation labels", S.k.labels !== false), check("every", "Every link (not only the tree's)", !!S.k.every)),
        h("p", { class: "tiny faint", style: "margin:0;border-top:1px solid var(--line);padding-top:10px" }, h("b", null, "+N"), " on a node = what is still to open below it. ", h("b", null, "Click"), " a node to see what it is, ", h("b", null, "double-click"), " to drill down or close it. Drag the canvas to move it, scroll to zoom.")));
  }
  function explorer() {
    const ov = S.k.ov, sc = kScene(), lay = kLayout(sc), a = kArea(), canvas = kCanvas(sc, lay), e = S.k.e, m = sc.m, free = (a.left + a.w - a.right) / 2, wide = "max-width:" + Math.max(260, a.w - a.left - a.right - 16) + "px;left:" + free + "px";
    const seg = (id, label) => h("button", { type: "button", class: (S.k.layout || "layered") === id ? "on" : "", "aria-pressed": String((S.k.layout || "layered") === id), onclick: () => { S.k.layout = id; S.k.tf = null; S.k.focus = e ? e.entity.id : null; render(); } }, label);
    const search = run(async () => { S.k.hits = S.k.q.trim() ? await api("knowledge/search?q=" + encodeURIComponent(S.k.q)) : null; render(); });
    const up = e && sc.by[e.entity.id] ? sc.parentOf[e.entity.id] : null;
    const crumbs = e ? [h("button", { class: "gx-trail-btn", type: "button", onclick: () => { S.k.grp = e.trail[0].level; S.k.e = null; kHash(null); render(); } }, h("span", { class: "gx-level-swatch", style: "background:" + (LCOL[e.trail[0].level] || "#7a8496") }), LVL[e.trail[0].level] || "Added"),
      e.trail.map((x, i) => [h("span", { class: "gx-crumb-sep" }, ico("chev", 11)), h("button", { class: "gx-trail-btn" + (i === e.trail.length - 1 ? " on" : ""), type: "button", "aria-current": i === e.trail.length - 1 ? "location" : null, title: words(x.type) + ": " + x.name, onclick: i === e.trail.length - 1 ? null : kShow(x.id) }, kShort(x.name).length > 30 ? kShort(x.name).slice(0, 29) + "…" : kShort(x.name))])]
      : S.k.grp ? [h("button", { class: "gx-trail-btn on", type: "button", "aria-current": "location" }, h("span", { class: "gx-level-swatch", style: "background:" + LCOL[S.k.grp] }), LVL[S.k.grp])] : [h("span", { class: "gx-trail-hint" }, "double-click a level to open it, an entity to drill down")];
    return h("div", { class: "gx-root", style: "height:" + (a.h + 54) + "px" },
      h("div", { class: "gx-header" }, h("div", { class: "gx-brand" }, h("span", { class: "gx-brand-icon" }, ico("compass", 18)), h("div", { class: "gx-brand-text" }, h("div", { class: "gx-brand-title" }, "Explore"), h("div", { class: "tiny faint truncate" }, (ov.name || "Workspace knowledge") + " · " + ov.index.entities.toLocaleString() + " entities"))),
        h("form", { class: "gx-header-search gx-search", role: "search", onsubmit: search }, h("span", { class: "gx-search-icon" }, ico("search", 15)), h("input", { class: "input", type: "text", placeholder: "Search a standard, an article, a rule, a company, a reference number…", "aria-label": "Search the knowledge graph", value: S.k.q, oninput: (ev) => (S.k.q = ev.target.value) }),
          S.k.hits ? (S.k.hits.length ? h("ul", { class: "gx-search-list" }, h("li", { class: "gx-search-group" }, "Entities · click one to go to it"), S.k.hits.map((x) => h("li", { class: "gx-search-item", onclick: kShow(x.id) }, h("i", { class: "kdot", style: "background:" + (LCOL[x.level] || "#7a8496") }), h("span", { class: "grow truncate" }, h("b", null, x.name), x.description ? h("span", { class: "faint" }, " · " + x.description) : null), h("span", { class: "tiny faint" }, words(x.type) + " · " + (LVL[x.level] || "added").toLowerCase()))))
            : h("div", { class: "gx-search-empty" }, "Nothing in the graph matches that.")) : null),
        h("div", { class: "seg gx-seg", role: "group", "aria-label": "Layout" }, seg("force", "Force"), seg("layered", "Layered"), seg("radial", "Radial")),
        h("span", { class: "gx-view-tag" }, ico("layers", 14), " Level map"),
        h("div", { class: "gx-header-tools" }, h("button", { class: "btn sm", type: "button", title: "Fit the whole scene on the canvas", onclick: () => { S.k.focus = null; S.k.tf = kFit(lay, sc); S.k.auto = true; kApplyTf(); } }, ico("fit"), " Show all"),
          h("button", { class: "gx-icon-btn", type: "button", title: "Back to the levels and the entities that head them", "aria-label": "Back to the top", onclick: kTop() }, ico("reset", 15)))),
      h("div", { class: "gx-canvas" }, canvas.svg,
        h("nav", { class: "gx-levelbar", "aria-label": "Levels of the map", style: wide }, ico("layers"), h("strong", null, "Level map"),
          m.levels.map((l) => { const open = (sc.kids["level:" + l.id] || []).length; return h("span", { class: "gx-level" }, h("span", { class: "gx-level-sep" }, "·"), h("button", { type: "button", class: m.open.includes(l.id) ? "on" : "", disabled: !l.entities, "aria-pressed": String(m.open.includes(l.id)), title: l.say + (l.entities ? " Click to " + (m.open.includes(l.id) ? "close." : "open.") : ""), onclick: kLevel(l.id) }, h("span", { class: "gx-level-swatch", style: "background:" + LCOL[l.id] }), l.name + " ", h("span", { class: "num" }, open + "/" + l.heads))); })),
        h("nav", { class: "gx-trail", "aria-label": "Where you are in the tree", style: wide }, h("button", { class: "gx-trail-btn", type: "button", disabled: !e, title: "Up to what it hangs from", onclick: !e ? null : up && !up.startsWith("level:") ? kSelect(up) : () => { S.k.grp = e.trail[0].level; S.k.e = null; kHash(null); render(); } }, ico("up", 12), " Up"), h("span", { class: "gx-trail-sep" }),
          h("button", { class: "gx-trail-btn", type: "button", title: "To the top: the levels of the map, and the entities that head them", onclick: kTop() }, ico("home", 12), " Top"), h("span", { class: "gx-crumb-sep" }, ico("chev", 11)), h("span", { class: "gx-trail-crumbs" }, crumbs)),
        kLeft(sc, canvas.drawn), e ? kInspector(sc) : S.k.grp ? kLevelPanel(sc) : null,
        h("p", { class: "gx-hint", style: "left:" + free + "px;max-width:" + Math.max(200, a.w - a.left - a.right - 130) + "px" }, "Level map · the levels top-down, each with the entities that head it · double-click an entity to drill down · Up goes back up, Top to the levels"),
        h("div", { class: "gx-zoom", style: "right:" + (a.right + 2) + "px" }, h("button", { class: "gx-zoom-btn", type: "button", title: "Zoom in", "aria-label": "Zoom in", onclick: () => kZoom(1.25) }, ico("plus", 15)), h("button", { class: "gx-zoom-btn", type: "button", title: "Zoom out", "aria-label": "Zoom out", onclick: () => kZoom(0.8) }, ico("minus", 15)),
          h("button", { class: "gx-zoom-btn", type: "button", title: "Fit the scene", "aria-label": "Fit the scene", onclick: () => { S.k.focus = null; S.k.tf = kFit(lay, sc); S.k.auto = true; kApplyTf(); } }, ico("fit", 15)))));
  }
  const STATES = { standards: ["standard or regulation", "standards and regulations"], provisions: ["provision", "provisions"], bodies: ["standards body", "standards bodies"], practices: ["practice", "practices"], processes: ["process", "processes"], risks: ["risk", "risks"],
    kpis: ["measure", "measures"], terms: ["term", "terms"], challenges: ["challenge", "challenges"], capabilities: ["capability", "capabilities"] };
  function kManage() {   // everything behind the graph, as one list to manage
    const ov = S.k.ov, ver = ov.verification || {}, st = ver.stats || {}, levelName = Object.fromEntries(ov.levels.map((l) => [l.id, l.name]));
    const origins = Object.entries((ver.origins || {}).entities || {}).sort((a, b) => b[1] - a[1]), total = origins.reduce((t, x) => t + x[1], 0) || 1;
    const states = (b) => Object.keys(STATES).filter((k) => (b.states || {})[k]).map((k) => b.states[k] + " " + STATES[k][b.states[k] === 1 ? 0 : 1]).join(" · ");
    const sw = (b) => run(async () => { await api("knowledge/bases/" + encodeURIComponent(b.id), { on: !b.on }); toast(b.name + (b.on ? " is switched off: nothing it states is in the graph now." : " is switched on.")); await kReload(); });
    const lvPill = (id) => h("span", { class: "pill plain", style: "color:" + (LCOL[id] || "#5d6779") }, levelName[id] || "Each at its own");
    const fileIn = h("input", { type: "file", accept: ".zip,.json,.html,.htm", style: "display:none", onchange: run(async (ev) => { const f = ev.target.files[0]; if (!f) return;
      const b64 = await new Promise((res, rej) => { const r = new FileReader(); r.onload = () => res(String(r.result).split(",")[1]); r.onerror = rej; r.readAsDataURL(f); });
      const out = await api("knowledge/imports", { name: f.name, content_base64: b64, level: S.k.place }); toast("Added " + out.import.label + ": " + out.import.entities + " entities, " + out.import.relationships + " relationships, " + (levelName[out.import.placed] ? "at the " + levelName[out.import.placed] + " level" : out.import.own_levels ? "each entity at the level it was built for" : "with no level yet: choose one for it in the list")); await kReload(); }) });
    const download = run(async () => { const res = await fetch(BASE + "/api/knowledge/export", { headers: token() ? { Authorization: "Bearer " + token() } : {} }); if (!res.ok) throw new Error("The export failed (" + res.status + ")");
      const url = URL.createObjectURL(await res.blob()), a = h("a", { href: url, download: "docuinsights-knowledge-base.zip" }); document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 2000); });
    const bases = ov.knowledge_bases, curated = bases.filter((b) => b.kind === "curated"), added = bases.filter((b) => b.kind === "added");
    const cell = (b, sub) => h("td", null, h("b", null, b), sub ? h("div", { class: "small muted" }, sub) : null);
    return [h("div", { class: "card" }, h("h3", null, "Knowledge bases"),
        h("p", { class: "small muted", style: "margin-bottom:8px" }, "Everything the graph is built from. Switch a curated knowledge base off and nothing it states is in the graph; switch it on and it is back. Each one is a file you can read and edit, kept in the " + ov.format.name + " layout (schema " + ov.format.schemaVersion + ") once built."),
        h("table", { class: "kbases" }, h("thead", null, h("tr", null, ["Knowledge base", "Level", "What it states", "In the graph", ""].map((x) => h("th", null, x)))), h("tbody", null,
          curated.map((b) => h("tr", { class: b.on ? "" : "off" }, cell(b.name, "Curated · v" + b.version + (b.shipped ? " · comes with the accelerator" : " · from this installation's knowledge folder")), h("td", null, lvPill(b.level)), h("td", { class: "small" }, states(b)),
            h("td", { class: "small" }, b.on ? b.entities.toLocaleString() + " things, " + b.relationships.toLocaleString() + " links" : h("span", { class: "muted" }, "switched off")),
            h("td", { class: "acts" }, b.on ? h("button", { class: "btn sm", onclick: kShow(b.head_id) }, "Open") : null, h("button", { class: "btn sm" + (b.on ? " ghost" : " primary"), onclick: sw(b) }, b.on ? "Switch off" : "Switch on")))),
          h("tr", null, cell("The accelerator and its use cases", "Built in · " + S.cfg.profiles.length + " use case(s), their documents and rules, " + S.cfg.agents.length + " agents under contract"), h("td", null, lvPill("domain"), " ", lvPill("solution")), h("td", { class: "small" }, "Declared by the use-case profiles and the agent contracts"), h("td", { class: "small muted" }, "always on"), h("td", null)),
          h("tr", null, cell("Cases", "Learned · it grows with every case that is assessed"), h("td", null, lvPill("customer")), h("td", { class: "small" }, ov.sources.cases + " assessed case(s), " + ov.sources.documents + " document(s)"), h("td", { class: "small muted" }, "always on"), h("td", null)),
          added.map((b) => h("tr", { class: b.on ? "" : "off" }, cell(b.name, "Added · " + (b.app || "file") + (b.at ? " · " + when(b.at) : "")),
            h("td", null, b.on ? h("select", { style: "width:auto", "aria-label": "Level of " + b.name, onchange: run(async (ev) => { await api("knowledge/imports/" + encodeURIComponent(b.id) + "/place", { level: ev.target.value }); toast(b.name + " moved."); await kReload(); }) }, [["", b.own_levels ? "Each at its own level" : "No level yet"], ["industry", "Industry"], ["domain", "Domain"], ["solution", "Solution"], ["customer", "Customer"]].map(([v, l]) => h("option", { value: v, selected: (b.level || "") === v }, l))) : lvPill(b.level)),
            h("td", { class: "small" }, "A knowledge base built elsewhere, in KB Forge's format"), h("td", { class: "small" }, b.on ? b.entities.toLocaleString() + " things, " + b.relationships.toLocaleString() + " links" : h("span", { class: "muted" }, "taken out")),
            h("td", { class: "acts" }, h("button", { class: "btn sm" + (b.on ? " ghost" : " primary"), onclick: run(async () => { await api("knowledge/imports/" + encodeURIComponent(b.id) + (b.on ? "/remove" : "/restore"), {}); toast(b.name + (b.on ? " taken out. It can be put back." : " put back.")); await kReload(); }) }, b.on ? "Take out" : "Put back")))))),
        (ov.index.knowledge_problems || []).length ? h("div", { class: "knote", style: "margin-top:10px" }, h("b", null, "Left out when the graph was last built: "), h("ul", { style: "margin:4px 0 0;padding-left:18px" }, ov.index.knowledge_problems.map((x) => h("li", null, x)))) : null,
        fileIn, h("div", { class: "row", style: "margin-top:12px" }, h("select", { style: "width:auto", "aria-label": "Level for a knowledge base you add", onchange: (ev) => (S.k.place = ev.target.value) }, [["", "Level: as built"], ["industry", "Industry"], ["domain", "Domain"], ["solution", "Solution"], ["customer", "Customer"]].map(([v, l]) => h("option", { value: v, selected: S.k.place === v }, l))),
          h("button", { class: "btn primary sm", onclick: () => fileIn.click() }, "Add a knowledge base"), h("button", { class: "btn sm", onclick: download }, "Download for KB Forge (.zip)"),
          h("button", { class: "btn sm", onclick: run(async () => { await api("knowledge/rebuild", {}); toast("The graph was built again from its knowledge bases."); await kReload(); }) }, "Build the graph again"))),
      h("div", { class: "grid g2", style: "margin-top:14px;align-items:start" },
        cardOf("Added by hand", h("p", { class: "small muted", style: "margin-bottom:8px" }, "To add something, open the node it belongs under on the level map and choose “Add under this”: an article under its standard, a control under its industry, what a rule rests on under the rule. It is marked as added by hand, with who added it and what they cite."),
          ov.facts.length ? ov.facts.map((f) => h("div", { class: "kcase" }, h("button", { class: "lrowb", type: "button", style: "flex:1", onclick: kShow(f.entity) }, dot(f.type), h("span", null, f.name), h("em", null, words(f.type) + " · " + f.by + " · " + when(f.at))),
            h("button", { class: "btn sm ghost", onclick: run(async () => { await api("knowledge/facts/" + encodeURIComponent(f.id) + "/remove", {}); toast("Taken out: " + f.name); await kReload(); }) }, "Take out"))) : h("p", { class: "small muted" }, "Nothing has been added by hand.")),
        cardOf("Where the knowledge comes from", h("p", { class: "small muted", style: "margin-bottom:8px" }, "Every build checks each document citation against the text it cites, with no model. " + (st.missing ? st.missing + " could not be found." : "None is missing.")),
          origins.map(([o, n]) => h("div", { class: "barrow" }, h("span", null, ov.origin_words[o] || o), h("div", { class: "bar" }, h("i", { style: "width:" + Math.max(2, (100 * n) / total) + "%" })), h("b", null, n))))),
      h("details", { class: "card", style: "margin-top:14px" }, h("summary", null, "The ontology: " + ov.classes.length + " kinds of thing, " + ov.relationships.length + " kinds of link"),
        h("table", { style: "margin-top:8px" }, h("thead", null, h("tr", null, ["Kind of thing", "Is a", "Level", "In the graph", "What counts as one"].map((x) => h("th", null, x)))), h("tbody", null, ov.classes.filter((c) => c.count || c.source !== "import").map((c) => h("tr", null, h("td", null, h("b", null, c.name)), h("td", { class: "small" }, c.parent || "–"), h("td", { class: "small" }, levelName[c.level] || "–"), h("td", { class: "mono small" }, c.count), h("td", { class: "small muted" }, c.definition))))),
        h("table", { style: "margin-top:14px" }, h("thead", null, h("tr", null, ["Kind of link", "From", "To", "In the graph", "Meaning"].map((x) => h("th", null, x)))), h("tbody", null, ov.relationships.filter((r) => r.count).map((r) => h("tr", null, h("td", null, h("b", null, r.name)), h("td", { class: "small" }, r.domain), h("td", { class: "small" }, r.range), h("td", { class: "mono small" }, r.count), h("td", { class: "small muted" }, r.definition))))))];
  }
  function knowledge() {
    const ov = S.k.ov, idx = ov.index, ver = ov.verification || {}, st = ver.stats || {}, manage = S.k.view === "manage";
    const checked = (st.checked_entities || 0) + (st.checked_relationships || 0), found = checked - (st.missing || 0) - (st.unchecked || 0);
    const seg = (id, label) => h("button", { type: "button", "aria-pressed": String(manage === (id === "manage")), onclick: () => { S.k.view = id; S.k.tf = null; render(); } }, label);
    return [h("div", { class: "kx-head" }, h("h1", null, "Knowledge"), h("div", { class: "seg" }, seg("explore", "Explore the graph"), seg("manage", "Manage")),
        S.k.back ? h("button", { class: "btn sm", onclick: run(() => open(S.k.back)) }, "← Back to case " + S.k.back) : null,
        h("span", { class: "small muted right" }, idx.entities.toLocaleString() + " things known · " + idx.triples.toLocaleString() + " links · quality " + ov.quality.score + " of 100 · kept in KB Forge's format")),
      manage ? [h("p", { class: "lead" }, "Everything the graph is built from, and what it holds. The graph is drawn under Explore the graph, as KB Forge draws one: four levels, from the industry down to each case."),
        h("div", { class: "grid g4", style: "margin:12px 0 14px" }, [[idx.entities.toLocaleString(), "things known"], [idx.triples.toLocaleString(), "links between them"], [ov.quality.score, "quality score, out of 100"], [checked ? Math.round((100 * found) / checked) + "%" : "–", "of " + checked + " citations found in the text"]].map(([v, l]) => h("div", { class: "tile" }, h("b", null, v), h("span", null, l)))),
        kManage()] : explorer()];
  }

  // ---------------------------------------------------------------------------------------------- how it is governed
  function platform() {
    const f = S.pf, c = f.contracts, m = f.memory, led = f.provenance.ledger, t = f.throttling, lim = t.model.limits, row = (k, v) => h("tr", null, h("td", { class: "small muted", style: "width:42%" }, k), h("td", null, v));
    return [section("How the agents are governed", "What every agent runs on, and how each stands in this workspace right now."),
      h("div", { class: "grid g2" },
        cardOf("Agent contracts", h("p", { class: "small muted", style: "margin-bottom:8px" }, "A contract says what an agent needs, what it promises, what it may change, what it may spend and what it must never do."),
          h("table", null, h("tbody", null, row("Agents under contract", c.agents), row("Checks the contracts name", c.checks), row("Agent runs so far", c.runs), row("Runs that kept their contract", h("span", null, c.kept + " ", pill(c.kept === c.runs ? "pass" : "fail", c.kept === c.runs ? "all" : c.runs - c.kept + " broken"))))),
          c.broken.map((b) => h("div", { class: "note", style: "margin-top:6px" }, h("b", null, b.case + " · " + b.agent + ": "), b.why.join("; "))), h("button", { class: "btn sm", style: "margin-top:10px", onclick: top("agents") }, "Read the contracts")),
        cardOf("Agent harness", h("p", { class: "small muted", style: "margin-bottom:8px" }, "The one place every agent runs. It never decides a case and never repairs an agent's output."),
          steps(["Before: checks what the agent needs; hands it only the model and the memory its contract allows.", "During: times the run; meters every model call against the agent's budget and the rate limit.", "After: checks every promise and that the agent changed only what it may; records the run on the case and in the ledger.", "A contract that is not kept is never silent: the case goes to a person."])),
        cardOf("Shared memory", h("p", { class: "small muted", style: "margin-bottom:8px" }, "What the agents keep for each other, at three distances. Only the orchestrator writes what outlives a case, and never from a model."),
          h("table", null, h("tbody", null, row("One run", "The note each agent leaves for the ones after it"), row("One case", "Its documents, the reviewer's corrections and decisions"), row("Workspace: what is known", m.semantic.entities + " things, " + m.semantic.relationships + " links (the knowledge graph)"),
            row("Workspace: what happened", m.episodes + " episode(s), one per assessed case"), row("Workspace: how to read", Object.values(m.procedural).reduce((n, x) => n + x.approved, 0) + " approved improvement(s), " + Object.values(m.procedural).reduce((n, x) => n + x.waiting, 0) + " waiting for a person"))),
          m.recent.length ? h("table", { style: "margin-top:8px" }, h("thead", null, h("tr", null, ["Remembered case", "Use case", "Outcome"].map((x) => h("th", null, x)))), h("tbody", null, m.recent.slice(0, 5).map((e) => h("tr", { class: "click", onclick: run(() => open(e.case)) }, h("td", { class: "mono small" }, e.case), h("td", { class: "small" }, e.use_case), h("td", null, outcomePill(e)))))) : null),
        cardOf("Evidence provenance", h("p", { class: "small muted", style: "margin-bottom:8px" }, "Every conclusion points to a line of a document. Every agent run, review and decision is one line in a ledger, each chained to the line before it."),
          h("table", null, h("tbody", null, row("Ledger entries", led.entries), row("Chain", led.ok ? pill("pass", "intact") : pill("fail", "broken at entry " + led.broken_at)), row("Latest fingerprint", h("span", { class: "mono small" }, led.head || "–")))),
          f.provenance.recent.length ? h("table", { style: "margin-top:8px" }, h("thead", null, h("tr", null, ["#", "Who", "What"].map((x) => h("th", null, x)))), h("tbody", null, f.provenance.recent.slice(0, 6).map((e) => h("tr", null, h("td", { class: "mono small" }, e.n), h("td", { class: "small" }, e.actor), h("td", { class: "small" }, e.summary, e.case ? h("span", { class: "muted" }, " · " + e.case) : null))))) : null),
        cardOf("Knowledge graph", h("p", { class: "small muted", style: "margin-bottom:8px" }, "What the workspace knows, as one graph in the KB Forge format, rebuilt from its sources after every case."),
          h("table", null, h("tbody", null, row("Things known", m.semantic.entities), row("Links between them", m.semantic.relationships))), h("button", { class: "btn sm", style: "margin-top:10px", onclick: top("knowledge") }, "Open the knowledge graph")),
        cardOf("Throttling", h("p", { class: "small muted", style: "margin-bottom:8px" }, "How fast the agents may call a model and how fast a caller may call the API. At a limit a call waits; past the wait the agent falls back to patterns and says so."),
          h("table", null, h("tbody", null, row("Model calls", t.model.describe + ", wait up to " + lim.max_wait_s + "s"), row("In the last minute", t.model.last_minute.calls + " call(s), " + t.model.last_minute.tokens.toLocaleString() + " tokens"),
            row("Since start", t.model.totals.calls + " call(s) · " + t.model.totals.waits + " waited · " + t.model.totals.shed + " not made"), row("Per agent, per run", "a budget of calls and tokens in each contract"),
            row("API", (t.api.requests_per_minute ? t.api.requests_per_minute + " requests a minute per caller" : "no limit") + " · " + t.api.totals.refused + " refused"), row("Assessments at once", t.assessments.max_at_once + " (running now: " + t.assessments.running + ")")))),
        cardOf("Watchlists", h("p", { class: "small muted", style: "margin-bottom:8px" }, "What the screening agent compares every party, vessel and port with. Each result names the list, its version and its fingerprint."),
          (S.cfg.watchlists || []).length ? h("table", null, h("thead", null, h("tr", null, ["List", "Version", "Entries", "Source"].map((x) => h("th", null, x)))), h("tbody", null, S.cfg.watchlists.map((w) => h("tr", null, h("td", null, w.name, w.demonstration ? h("div", { class: "small", style: "color:var(--warn)" }, "invented entries, for demonstration") : null), h("td", { class: "small" }, w.version || "–"), h("td", null, w.entries), h("td", { class: "small muted" }, w.source)))))
            : h("div", { class: "note" }, "No watchlist is loaded: screening reports that it could not be done."),
          (S.cfg.watchlist_problems || []).length ? h("div", { class: "knote", style: "margin-top:8px" }, h("b", null, "Could not be read: "), S.cfg.watchlist_problems.join("; "), ". Every case says so as a finding until the list is fixed; a list that did not load is never read as no match.") : null,
          h("p", { class: "small muted", style: "margin-top:8px" }, "Name your own lists with DOCUINSIGHTS_WATCHLISTS (JSON or CSV files, or folders of them). The demonstration list is then no longer used.")),
        cardOf("Privacy and notifications", h("table", null, h("tbody", null, row("Personal data", "Found by document and by kind on every case, by fixed patterns"), row("Masked in", "The decision record on request, notifications, and the knowledge base (so its export carries no account number, email or telephone number)"),
          row("Stored documents", "Kept as received: nothing is redacted in the original"), row("Notifications", S.cfg.webhook ? "A signed message to the address in DOCUINSIGHTS_WEBHOOK_URL when a case is assessed or decided" : "Off. Set DOCUINSIGHTS_WEBHOOK_URL to tell another system when a case is assessed or decided")))))];
  }

  // ---------------------------------------------------------------------------------------------- findings
  function caseK() {   // the case as the knowledge graph sees it: fetched once for each assessment, drawn when it arrives
    const a = A(); if (!a) return null;
    const key = S.c.reference + ":" + a.id;
    if (!S.ck || S.ck.key !== key) { S.ck = { key, data: null }; api("cases/" + encodeURIComponent(S.c.reference) + "/knowledge").then((d) => { if (S.ck && S.ck.key === key) { S.ck.data = d; if (S.c) render(); } }).catch(() => { /* the graph is not built yet: the cards show without it */ }); }
    return S.ck.data;
  }
  function knowledgeLine(f) {
    const ck = caseK(), fk = ck ? ck.findings[f.id] : null, basis = f.basis && f.basis.length ? f.basis : fk ? fk.basis : [], met = fk ? fk.known : [];
    if (!basis.length && !met.length && !(fk && (fk.rule || fk.node))) return null;
    return h("div", { class: "kline" }, h("span", { class: "klabel", title: "What the knowledge graph holds behind this finding. Each opens the exact node, with the way down to it." }, "Knowledge graph"),
      fk && fk.rule ? kchip(fk.rule, "Rule: " + fk.rule.name) : null, basis.length ? h("span", { class: "small muted" }, "rests on") : null, basis.map((b) => kchip(b, kShort(b.name), b.name + ". " + b.says)),
      met.map((x) => kchip(x, x.name + " · in " + x.cases + " case(s)")), fk && fk.node ? h("button", { class: "btn sm ghost", type: "button", onclick: kShow(fk.node) }, "This finding in the graph") : null);
  }
  function graphCard() {
    const ck = caseK(); if (!ck || (!ck.rests_on.length && !ck.known.length)) return null;
    const count = (fs) => fs.reduce((t, f) => ((t[f.status] = (t[f.status] || 0) + 1), t), {}), lv = Object.entries(ck.handed || {}).filter((x) => x[1]);
    return cardOf("What the knowledge graph says about this case", h("p", { class: "small muted", style: "margin-bottom:8px" }, "Every check applies a rule, and every rule rests on something the graph holds: an article of a standard, a control, a term. Open one to see it in the graph, with the way down to it and every other case it was used on."),
      ck.rests_on.length ? h("table", null, h("thead", null, h("tr", null, ["The checks rest on", "Which part", "Checks on this case"].map((x) => h("th", null, x)))), h("tbody", null, ck.rests_on.slice(0, 10).map((r) => h("tr", null, h("td", null, kchip(r, r.name)),
        h("td", { class: "small" }, r.points.length ? r.points.map((p) => kShort(p).replace(r.name + " ", "")).join(", ") : words(r.type)), h("td", null, h("span", { class: "row", style: "gap:5px" }, statusPills(count(r.findings)))))))) : null,
      ck.known.length ? h("div", { class: "kline", style: "border:0;padding-top:8px" }, h("span", { class: "klabel" }, "Already in the graph"), ck.known.map((x) => kchip(x, x.name + " · in " + x.cases + " case(s)"))) : null,
      h("div", { class: "row", style: "margin-top:10px" }, ck.case ? h("button", { class: "btn sm", onclick: kShow(ck.case) }, "Open this case in the knowledge graph") : null,
        lv.length ? h("span", { class: "small muted" }, "Handed to the agents before they ran: " + lv.map(([k, n]) => n + " " + (LVL[k] || k).toLowerCase() + "-level").join(", ") + " nodes.") : null));
  }
  function findingCard(f, opts) {
    const o = opts || {}, v = f.verifier || {};
    return h("div", { class: "card", id: "f-" + f.id }, h("div", { class: "row" }, pill(f.status), h("b", null, f.title), f.blocked ? pill("fail", "blocked: unsupported") : null, h("span", { class: "right small muted mono" }, f.id)),
      h("p", { style: "margin:6px 0 8px" }, f.message), steps(f.reasoning),
      f.sources.length ? h("div", { class: "grid g2", style: "margin-top:10px" }, f.sources.map((s) => h("div", { class: "src", title: "Open the source", onclick: () => openSource(s) }, h("b", null, s.doc_name), " · ", s.field_name, s.page ? " · page " + s.page + (s.line ? ", line " + s.line : "") : "", h("q", null, s.snippet || String(s.value))))) : null,
      h("div", { class: "chips small muted", style: "margin-top:10px" }, f.rule ? h("span", { class: "chip", title: f.rule.note || "" }, "Rule " + f.rule.id + " v" + f.rule.version) : null,
        f.reference ? h("a", { class: "chip", href: f.reference.url, target: "_blank", rel: "noopener" }, "Reference as of " + (f.reference.as_of || "n/a") + " (" + f.reference.mode + ")") : null,
        h("span", { class: "chip" }, "Agent: " + f.agent), h("span", { class: "chip" }, "Sources verified " + (v.verified || 0) + "/" + (v.sources || 0)), f.requires_human ? h("span", { class: "chip" }, "Needs a person") : null,
        f.human ? pill(f.human.decision === "accept" ? "pass" : "review", f.human.decision + "ed by " + f.human.by) : null,
        f.graph_path.length ? h("button", { class: "btn sm ghost", onclick: () => { S.view = "graph"; S.sel.path = f.graph_path; S.sel.node = "F:" + f.id; S.layers.reasoning = true; S.layers.assertion = true; render(); } }, "Show graph path (" + f.graph_path.length + " nodes)") : null),
      knowledgeLine(f),
      o.review ? h("div", { class: "row", style: "margin-top:10px" }, h("button", { class: "btn sm", onclick: run(() => reviewFinding(f, "accept")) }, "Accept"), h("button", { class: "btn sm", onclick: run(() => reviewFinding(f, "flag")) }, "Flag")) : null);
  }
  async function reviewFinding(f, decision) { const v = await form((decision === "accept" ? "Accept" : "Flag") + ": " + f.title, [{ k: "reason", label: "Note for the audit trail", type: "textarea" }], decision === "accept" ? "Accept" : "Flag"); if (v) setCase(await api("cases/" + S.c.reference + "/review", { target: "finding", id: f.id, decision, reason: v.reason })); }
  function banner(rec) {
    const fresh = S.sealed !== S.c.reference + ":" + A().id; S.sealed = S.c.reference + ":" + A().id;   // the seal lands once, when a recommendation is first shown
    return h("div", { class: "banner " + rec.outcome }, h("span", { class: "seal " + rec.outcome + (fresh ? " stamp" : ""), "aria-hidden": "true" }, { proceed: "✓", request_evidence: "?", review: "!" }[rec.outcome] || "·"),
      h("div", { class: "small muted" }, "Recommendation for the caseworker"), h("h2", null, rec.label), h("p", null, rec.description),
      rec.reasons.length ? h("ul", null, rec.reasons.map((r) => h("li", null, r))) : null, rec.requests.length ? h("p", null, h("b", null, "To request: "), rec.requests.join("; ")) : null,
      rec.caveats.length ? h("p", { class: "small" }, h("b", null, "Caveat: "), rec.caveats.join(" ")) : null, h("p", { class: "small muted" }, "Decided by: " + rec.decided_by + "."));
  }

  function triageCard(a) {
    const t = a.triage; if (!t) return null;
    return h("div", { class: "card tri-card" }, h("div", null, h("dl", { style: "margin:0" }, h("dt", null, "Priority"), h("dd", null, pill(t.priority, cap(t.priority))))), h("div", null, h("dl", { style: "margin:0" }, h("dt", null, "Queue"), h("dd", null, t.queue_name))),
      h("div", null, h("dl", { style: "margin:0" }, h("dt", null, "Due"), h("dd", null, t.due, " ", dueText(t.due, S.c.due, !!S.c.decision)))), h("div", null, h("dl", { style: "margin:0" }, h("dt", null, "With"), h("dd", null, S.c.assigned ? S.c.assigned.to : "Nobody yet", " ", h("button", { class: "btn sm", onclick: assign }, S.c.assigned ? "Change" : "Take it")))),
      h("div", { class: "why" }, h("ul", null, t.reasons.map((r) => h("li", null, r))), t.note ? h("p", { class: "muted", style: "margin-top:6px" }, t.note) : null));
  }
  const recordButtons = () => h("div", { class: "row" }, h("button", { class: "btn sm", onclick: openRecord(false) }, "Open the decision record"), h("button", { class: "btn sm", onclick: openRecord(true), title: "The same record with personal data masked, for a copy that leaves the organisation" }, "With personal data masked"),
    h("span", { class: "small muted" }, "One printable page: what was received, what was found and on which line, who decided what."));

  // ---------------------------------------------------------------------------------------------- screening, integrity, privacy
  function assurance() {
    const a = A(), scr = a.screening, pv = a.privacy, of = (agent) => a.findings.filter((f) => f.agent === agent), rankOf = { fail: 0, missing: 1, review: 2, pass: 3, info: 4 }, sorted = (fs) => [...fs].sort((x, y) => rankOf[x.status] - rankOf[y.status]);
    if (!scr && !pv && !of("integrity").length) return [section("Screening, integrity, privacy", "This case was assessed before these checks existed."), h("button", { class: "btn primary", onclick: run(async () => setCase(await api("cases/" + S.c.reference + "/assess", {}), "monitor")) }, "Assess it again")];
    const dispose = (f, decision) => run(async () => { const hit = f.data.screening;
      const v = await form((decision === "clear" ? "Clear: " : "Confirm: ") + hit.subject + " and " + hit.entry, [{ k: "reason", label: decision === "clear" ? "Why is this not the listed " + hit.kind + "? (required, kept in the audit trail)" : "Note for the audit trail", type: "textarea", min: decision === "clear" ? 5 : 0, need: "Say why this is not the listed " + hit.kind + ". The reason is kept in the audit trail." }], decision === "clear" ? "Clear the match" : "Confirm the match");
      if (v) setCase(await api("cases/" + S.c.reference + "/review", { target: "screening", id: f.id, decision, reason: v.reason })); });
    const hits = of("screening").filter((f) => f.data && f.data.screening && f.data.screening.entry), lists = scr ? scr.lists || [] : [];
    return [section("Screening, integrity, privacy", "Three checks beside the use case's own rules: is any party on a watchlist, does each document hold together on its own, and what personal data does the package carry. All three are code; none is a model's opinion."),
      h("div", { class: "stack" },
        cardOf("Watchlist screening", scr && scr.enabled !== false ? [h("p", { class: "small muted", style: "margin-bottom:8px" }, (scr.screened || []).length + " name(s) screened against " + lists.reduce((n, l) => n + l.entries, 0) + " entries. A match is the same name, a name that holds every word of the listed one, or a spelling at least " + Math.round((scr.threshold || 0) * 100) + "% alike. A match is never cleared by the system."),
          lists.map((l) => h("div", { class: "note", style: "margin-bottom:8px" }, h("b", null, l.name + (l.version ? " " + l.version : "")), " · " + l.entries + " entries" + (l.published ? " · published " + l.published : "") + " · fingerprint " + l.sha256, l.demonstration ? h("div", { style: "color:var(--warn);margin-top:2px" }, "Invented entries, shipped for demonstration. Name your own lists with DOCUINSIGHTS_WATCHLISTS and this one is no longer used.") : null)),
          !lists.length ? h("div", { class: "note" }, "No watchlist is loaded, so nothing could be screened. That is reported as a finding, never as clear.") : null,
          (scr.problems || []).length ? h("div", { class: "knote", style: "margin-bottom:8px" }, h("b", null, "Not every list could be read, so this screening is incomplete: "), scr.problems.join("; ")) : null,
          hits.length ? hits.map((f) => { const x = f.data.screening; return h("div", { class: "hitrow" }, h("div", null, h("div", { class: "vs" }, pill(f.status, { review: "waiting for a person", fail: "confirmed", info: "cleared" }[f.status] || f.status), h("b", null, x.subject), h("span", null, "in this case"), "↔", h("b", null, x.entry), h("span", null, "listed · " + x.how + " (" + Math.round(x.score * 100) + "%)")),
              h("p", { class: "small", style: "margin-top:4px" }, f.message), f.sources.length ? h("div", { class: "chips", style: "margin-top:6px" }, f.sources.slice(0, 4).map((src) => h("span", { class: "chip", onclick: () => openSource(src) }, src.doc_name + (src.page ? " p" + src.page + (src.line ? " l" + src.line : "") : "")))) : null, knowledgeLine(f)),
              f.status === "review" ? h("div", { class: "row", style: "flex-wrap:nowrap" }, h("button", { class: "btn sm", onclick: dispose(f, "clear") }, "Not the same: clear"), h("button", { class: "btn sm primary", onclick: dispose(f, "confirm") }, "Confirm the match")) : h("span", { class: "small muted" }, "decided")); })
            : of("screening").map((f) => h("div", { class: "row", style: "align-items:flex-start;margin-top:6px" }, pill(f.status), h("div", { style: "flex:1" }, h("b", null, f.title), h("div", { class: "small" }, f.message), knowledgeLine(f)))),
          (scr.screened || []).length ? h("details", { style: "margin-top:10px" }, h("summary", { class: "small muted", style: "cursor:pointer" }, "The " + scr.screened.length + " name(s) screened"), h("table", { style: "margin-top:6px" }, h("tbody", null, scr.screened.map((x) => h("tr", null, h("td", null, x.name), h("td", { class: "small muted" }, x.what), h("td", { class: "small muted" }, x.kind)))))) : null]
          : h("p", { class: "muted" }, "Screening is switched off for this use case.")),
        h("div", null, h("h3", { style: "margin:6px 0 10px;font-size:15px" }, "Document integrity (" + of("integrity").length + " checks)"), h("div", { class: "stack" }, sorted(of("integrity")).map((f) => findingCard(f, { review: true })), !of("integrity").length ? h("p", { class: "muted" }, "No integrity check ran on this case.") : null)),
        cardOf("Personal data", pv ? [pv.categories.length ? h("table", null, h("thead", null, h("tr", null, ["Kind", "Items", "In"].map((x) => h("th", null, x)))), h("tbody", null, pv.categories.map((c) => h("tr", null, h("td", null, c.name), h("td", null, c.count),
            h("td", { class: "small" }, c.documents.map((id) => (S.c.documents.find((d) => d.id === id) || {}).name || id).join(", ")))))) : h("p", { class: "muted" }, "None found by the patterns. A pattern finds what is written in a recognisable form; it does not find a person described in free text."),
          h("p", { class: "small muted", style: "margin-top:8px" }, pv.masking), of("privacy").filter((f) => f.status !== "info").map((f) => h("div", { style: "margin-top:10px" }, findingCard(f, { review: true }))), h("div", { style: "margin-top:10px" }, recordButtons())] : h("p", { class: "muted" }, "Not checked.")))];
  }

  // ---------------------------------------------------------------------------------------------- case views
  function known(a) {
    const mem = a.findings.filter((f) => f.family === "memory" || f.family === "governance");
    return mem.length ? cardOf("What the workspace already knew", h("p", { class: "small muted", style: "margin-bottom:8px" }, "Recalled from earlier cases by the workspace's memory. Context for you; only a line marked for review affects the recommendation."),
      mem.map((f) => h("div", { class: "row", style: "align-items:flex-start;margin-bottom:6px" }, pill(f.status === "review" ? "review" : "info", f.status === "review" ? "for review" : "known"), h("div", { style: "flex:1" }, h("b", null, f.title), h("div", { class: "small" }, f.message))))) : null;
  }
  function summary() {
    const a = A(), c = (s) => a.findings.filter((f) => f.status === s).length, g = a.metrics.graph;
    return [section("Case intelligence summary", "What the package contains, what it supports and what stands in the way, composed from the findings."), h("div", { class: "stack" }, banner(a.recommendation), triageCard(a),
      h("div", { class: "grid g4" }, [[S.c.documents.filter((d) => !d.removed).length, "documents, " + a.metrics.pages + " pages"], [c("pass"), "checks passed"], [c("fail") + c("missing") + c("review"), "need attention"], [g.nodes + " / " + g.edges, "graph nodes / edges"]].map(([v, l]) => h("div", { class: "tile" }, h("b", null, v), h("span", null, l)))),
      cardOf("Summary", h("p", null, a.summary), h("p", { class: "small muted", style: "margin:8px 0 10px" }, "Assessment " + a.id + ", " + when(a.at) + ", trigger: " + a.trigger + ", profile v" + a.profile_version + ", vocabulary v" + a.overlay_version + ", " + a.metrics.duration_ms + " ms" + (a.contracts ? ", " + a.contracts.kept + " of " + a.contracts.runs + " agent contracts kept" : "")), recordButtons()),
      known(a), graphCard(),
      cardOf("Case facts", kv(prof().case.fields.map((f) => [f.name, S.c.fields[f.id]]))),
      cardOf("Parties and goods", h("table", null, h("thead", null, h("tr", null, ["Entity", "Kind", "Roles", "Mentioned in"].map((x) => h("th", null, x)))), h("tbody", null, a.entities.map((e) => h("tr", null, h("td", null, e.name), h("td", null, e.type), h("td", { class: "small" }, (e.roles || []).join(", ")),
        h("td", { class: "small muted" }, [...new Set(e.mentions.map((m) => m.doc_name))].join(", "))))))),
      h("div", null, h("h3", { style: "margin:6px 0 10px;font-size:15px" }, "Needs attention"), h("div", { class: "stack" }, a.findings.filter((f) => ["fail", "missing", "review"].includes(f.status)).map((f) => findingCard(f)), !a.findings.some((f) => ["fail", "missing", "review"].includes(f.status)) ? h("p", { class: "muted" }, "Nothing: every check passed.") : null)))];
  }
  function documents() {
    const docs = S.c.documents.filter((d) => !d.removed), p = prof();
    const sel = docs.find((d) => d.id === S.sel.doc) || docs[0];
    const cls = (d) => d.classification || {};
    const correct = async (d) => { const v = await form("Correct the classification of " + d.name, [{ k: "value", label: "Correct class", options: p.document_types.map((t) => [t.id, t.name]), value: cls(d).type || "" }, { k: "reason", label: "Why is this change needed? (kept in the audit trail)", type: "textarea" }], "Confirm change"); if (v) setCase(await api("cases/" + S.c.reference + "/review", { target: "classification", doc: d.id, decision: "correct", value: v.value, reason: v.reason })); };
    const table = h("table", null, h("thead", null, h("tr", null, ["File", "Uploaded as", "AI classification", "Confidence", "Read by", "Review"].map((x) => h("th", null, x)))), h("tbody", null, docs.map((d) => h("tr", { class: "click" + (sel && d.id === sel.id ? " sel" : ""), onclick: () => { S.sel.doc = d.id; S.sel.line = null; render(); } },
      h("td", null, h("b", null, d.name), d.current > 1 ? h("span", { class: "pill plain", style: "margin-left:6px" }, "v" + d.current) : null), h("td", null, p.document_types.some((t) => t.id === d.category) ? typeName(d.category) : "Other documents"),
      h("td", null, cls(d).version ? pill(cls(d).agrees ? "pass" : "review", typeName(cls(d).type)) : "–", d.effective_type && d.effective_type !== cls(d).type ? h("div", { class: "small" }, "corrected to ", h("b", null, typeName(d.effective_type))) : null),
      h("td", null, cls(d).confidence !== undefined ? Math.round(cls(d).confidence * 100) + "%" : "–"), h("td", { class: "small muted" }, (d.versions[d.current - 1].method || "–")),
      h("td", null, d.review ? pill("pass", d.review.decision === "approve" ? "approved" : "corrected") : cls(d).version ? h("span", { class: "row", style: "gap:6px" }, h("button", { class: "btn sm", onclick: run(async (e) => { e.stopPropagation(); setCase(await api("cases/" + S.c.reference + "/review", { target: "classification", doc: d.id, decision: "approve" })); }) }, "Approve"), h("button", { class: "btn sm", onclick: run((e) => { e.stopPropagation(); return correct(d); }) }, "Incorrect")) : "–")))));
    let detail = null;
    if (sel) {
      const ver = sel.versions[sel.current - 1], hit = S.sel.line, tdef = p.document_types.find((t) => t.id === (sel.effective_type || cls(sel).type)) || { fields: [] };
      const editField = async (f) => { const cur = (sel.fields || {})[f.id]; const v = await form("Correct " + f.name, [{ k: "value", label: "Value as it should read", value: cur ? cur.value : "" }, { k: "reason", label: "Reason (audit trail)", type: "textarea" }]); if (v) setCase(await api("cases/" + S.c.reference + "/review", { target: "field", doc: sel.id, field: f.id, value: v.value, reason: v.reason })); };
      const text = h("div", { class: "doc" }, (ver.pages || []).flatMap((page, pi) => page.split("\n").map((ln, li) => h("div", { class: hit && hit.page === pi + 1 && hit.line === li + 1 ? "hit" : "" }, h("i", null, li + 1), ln || " "))), !(ver.pages || []).length ? h("div", null, h("i", null, ""), ver.note || "Not read yet.") : null);
      detail = h("div", { class: "split", style: "margin-top:14px" },
        cardOf("Extracted fields · " + typeName(sel.effective_type || cls(sel).type), cls(sel).rationale ? h("p", { class: "small muted", style: "margin-bottom:8px" }, "Classified because: " + cls(sel).rationale) : null,
          h("table", null, h("tbody", null, tdef.fields.map((f) => { const x = (sel.fields || {})[f.id]; return h("tr", { class: "click", onclick: () => { if (x && x.anchor && x.anchor.page) { S.sel.line = x.anchor; render(); } } }, h("td", { class: "small muted", style: "width:34%" }, f.name, f.required ? " *" : ""), h("td", null, x && x.norm !== null ? x.value : h("span", { class: "muted" }, "not found"), x && x.alternative ? h("div", { class: "small muted" }, "pattern read: " + x.alternative.value) : null),
            h("td", { style: "white-space:nowrap" }, x && x.norm !== null ? pill(x.verified ? "pass" : "review", x.method === "human" ? "corrected" : x.verified ? "sourced" : "unverified") : f.required ? pill("missing", "absent") : null), h("td", null, h("button", { class: "btn sm ghost", onclick: run((e) => { e.stopPropagation(); return editField(f); }) }, "Edit"))); })))),
        h("div", null, h("div", { class: "row", style: "margin-bottom:8px" }, h("b", null, sel.name), h("span", { class: "small muted mono" }, "v" + sel.current + " · sha256 " + (ver.sha256 || "").slice(0, 12)),
          h("a", { class: "btn sm right", href: "#", onclick: run(async (e) => { const t = token(); const r = await fetch(BASE + "/api/cases/" + S.c.reference + "/documents/" + sel.id + "/file", { headers: t ? { Authorization: "Bearer " + t } : {} }); window.open(URL.createObjectURL(await r.blob()), "_blank", "noopener"); }) }, "Open the original")), text,
          ver.note ? h("p", { class: "small muted", style: "margin-top:6px" }, ver.note) : null));
    }
    const more = h("input", { type: "file", multiple: true, style: "display:none", onchange: run(async (e) => { const docsNew = []; for (const f of e.target.files) { const b64 = await new Promise((res) => { const r = new FileReader(); r.onload = () => res(String(r.result).split(",")[1]); r.readAsDataURL(f); }); docsNew.push({ name: f.name, content_base64: b64 }); } setCase(await api("cases/" + S.c.reference + "/documents", { documents: docsNew, assess: true }), "monitor"); }) });
    return [section("Documents and classification", "Each file as it was uploaded beside what it actually is. Green means the classification agrees with the uploader's category; amber is a mismatch to check. A file uploaded again under the same name becomes a new version."),
      cardOf(null, table), h("div", { class: "row", style: "margin-top:10px" }, more, h("button", { class: "btn sm", onclick: () => more.click() }, "Add or amend documents"),
        S.c.sample && S.c.sample.amendments.length && A() ? h("button", { class: "btn sm accent", onclick: run(async () => setCase(await api("cases/" + S.c.reference + "/amendment", {}), "audit")) }, "Receive the amended document (" + S.c.sample.amendments[0].note + ")") : null), detail];
  }
  const listOf = (title, lead, pred, empty) => () => { const fs = F(pred); return [section(title, lead), h("div", { class: "stack" }, fs.length ? fs.map((f) => findingCard(f)) : h("p", { class: "muted" }, empty || "Nothing to show."))]; };
  const evidence = listOf("Evidence completeness", "Mandatory, conditional and alternative evidence for this use case, and how an absence was established.", (f) => f.family === "evidence");
  function consistency() {
    const fs = F((f) => f.agent === "contradiction");
    return [section("Cross-document consistency", "The same fact as each document states it. A discrepancy names the documents that disagree; a corroboration names how many agree."),
      cardOf(null, h("table", null, h("thead", null, h("tr", null, ["Check", "Result", "As stated in each document"].map((x) => h("th", null, x)))), h("tbody", null, fs.map((f) => h("tr", null, h("td", { style: "width:26%" }, h("b", null, f.title), h("div", { class: "small muted" }, f.rule.type)), h("td", { style: "width:14%" }, pill(f.status, { pass: "corroborated", fail: "discrepancy", review: "confirm", info: "single source" }[f.status])),
        h("td", null, f.sources.map((s) => h("div", { class: "src", style: "margin-bottom:4px", onclick: () => openSource(s) }, h("b", null, s.doc_name), ": ", String(s.value), (f.data.disagree || []).includes(s.doc) ? h("span", { class: "pill fail", style: "margin-left:6px" }, "differs") : null)), !f.sources.length ? h("span", { class: "muted" }, f.message) : null)))))),
      (A().unresolved || []).length ? cardOf("Possible entity matches", A().unresolved.map((m) => h("div", { class: "row", style: "margin-bottom:6px" }, h("span", null, m.reason + " (similarity " + m.similarity + ")"), matchButtons(m)))) : null];
  }
  const matchButtons = (m) => (m.decided ? pill("pass", m.decided === "same" ? "one party" : "two parties") : h("span", { class: "row right", style: "gap:6px" }, ["same", "different"].map((d) => h("button", { class: "btn sm", onclick: run(async () => { const v = await form(d === "same" ? "Confirm: one party" : "Confirm: two parties", [{ k: "reason", label: "Reason (audit trail)", type: "textarea" }], "Confirm"); if (v) setCase(await api("cases/" + S.c.reference + "/review", { target: "entity_match", id: m.id, decision: d, reason: v.reason })); }) }, d === "same" ? "Same party" : "Different"))));
  function reference() {
    const a = A();
    return [section("Classification assessment", "The reference content retrieved for the case's date, the line the product evidence supports, and the codes the documents state. The reference stays the authority; this is an aid for the caseworker."),
      h("div", { class: "stack" }, a.references.map((r) => {
        const rt = r.retrieval, res = r.result, path = new Set(res.path || []), alts = new Set((res.alternatives || []).map((x) => x.code)), depth = {};
        r.lines.forEach((n) => { depth[n.code] = n.parent && depth[n.parent] !== undefined ? depth[n.parent] + 1 : 0; });
        return [cardOf(rt.name, kv([["Address", h("a", { href: rt.url, target: "_blank", rel: "noopener" }, rt.url)], ["Requested as of", rt.requested_as_of], ["Retrieved", when(rt.retrieved_at) + " (" + rt.mode + ")"], ["Version", rt.version], ["Content hash", h("span", { class: "mono" }, (rt.result_hash || "").slice(0, 24))], ["Note", rt.note]])),
          F((f) => f.id === "REF-" + r.id + "-classification").map((f) => findingCard(f)),
          cardOf("Product characteristics used", Object.keys(r.characteristics).length ? h("table", null, h("tbody", null, Object.values(r.characteristics).map((c) => h("tr", null, h("td", { class: "muted small", style: "width:30%" }, c.name), h("td", null, String(c.raw)), h("td", null, c.unmapped ? pill("review", "not in the approved vocabulary") : c.value !== c.raw ? h("span", { class: "small muted" }, "read as " + c.value) : null))))) : h("p", { class: "muted" }, "No product evidence in the case.")),
          cardOf("Codes stated in the documents", r.declared.length ? h("table", null, h("tbody", null, r.declared.map((d) => h("tr", null, h("td", null, d.doc_name), h("td", { class: "mono" }, d.code), h("td", null, pill(d.relation === "equal" ? "pass" : "review", d.relation)))))) : h("p", { class: "muted" }, "No code is stated in any document.")),
          cardOf("Hierarchy as retrieved" + (rt.partial ? " (partial)" : ""), h("div", { class: "tree" }, r.lines.map((n) => h("div", { class: "ln" + (n.code === res.code && res.status === "supported" ? " cand" : path.has(n.code) ? " on" : alts.has(n.code) ? " alt" : "") }, h("span", { class: "mono" }, n.display), h("span", { style: "padding-left:" + depth[n.code] * 16 + "px" }, n.description), h("span", { class: "small muted" }, n.third_country_duty ? "duty " + n.third_country_duty : "")))))];
      })), F((f) => f.family === "commodity").map((f) => h("div", { style: "margin-top:14px" }, findingCard(f)))];
  }
  function policy() {
    const a = A(), hist = [...S.c.history, { id: a.id, at: a.at, trigger: a.trigger, label: a.recommendation.label, findings: Object.fromEntries(a.findings.map((f) => [f.id, { status: f.status, title: f.title }])), references: a.references.map((r) => ({ as_of: r.retrieval.requested_as_of, version: r.retrieval.version })), documents: a.documents }];
    return [section("Policy and temporal checks", "The deterministic rules, each with the calculation it performed and the version it ran under, and how the case has changed over time."),
      h("div", { class: "stack" }, F((f) => f.agent === "policy").map((f) => findingCard(f)),
        cardOf("What was known at each assessment", h("table", null, h("thead", null, h("tr", null, ["Assessment", "When", "Trigger", "Documents (version)", "Reference read for", "Outcome", "What changed"].map((x) => h("th", null, x)))), h("tbody", null, hist.map((x, i) => { const prev = hist[i - 1];
          const diff = prev ? Object.keys(x.findings).filter((k) => (prev.findings[k] || {}).status !== x.findings[k].status).map((k) => x.findings[k].title + ": " + ((prev.findings[k] || {}).status || "new") + " → " + x.findings[k].status) : [];
          return h("tr", null, h("td", { class: "mono" }, x.id), h("td", { class: "small" }, when(x.at)), h("td", null, x.trigger), h("td", { class: "small" }, (x.documents || []).map((d) => d.name + " v" + d.version).join(", ")), h("td", { class: "small" }, (x.references || []).map((r) => r.as_of + " (" + r.version + ")").join(", ")), h("td", null, x.label), h("td", { class: "small" }, prev ? (diff.length ? diff.map((t) => h("div", null, t)) : "no change") : "first assessment")); })))))];
  }
  function provenance() {
    const a = A(), fams = ["all", ...new Set(a.findings.map((f) => f.family))];
    const fs = a.findings.filter((f) => (S.filter.family === "all" || f.family === S.filter.family) && (S.filter.status === "all" || (S.filter.status === "attention" ? ["fail", "missing", "review"].includes(f.status) : f.status === S.filter.status)));
    const seg = (key, opts) => h("div", { class: "seg" }, opts.map((o) => h("button", { "aria-pressed": String(S.filter[key] === o), onclick: () => { S.filter[key] = o; render(); } }, o)));
    return [section("Reasoning and provenance explorer", "Every finding with what it rests on: the document, version, page and line; the rule and its version; the reference address and date; the agent; and the verifier's source coverage."),
      h("div", { class: "row", style: "margin-bottom:12px" }, seg("status", ["attention", "pass", "info", "all"]), seg("family", fams), h("span", { class: "small muted right" }, fs.length + " of " + a.findings.length + " findings")),
      h("div", { class: "stack" }, fs.map((f) => findingCard(f, { review: true })), !fs.length ? h("p", { class: "muted" }, "No finding matches this filter.") : null)];
  }
  function whatif() {
    const p = prof(), cf = S.cf;
    const fire = (sc) => run(async () => { let value; if (sc.ask) { const v = await form(sc.title, [{ k: "value", label: "New value", type: sc.ask, value: sc.value || "" }], "Run what-if"); if (!v) return; value = v.value || undefined; } S.cf = { pending: sc.title }; render(); S.cf = await api("cases/" + S.c.reference + "/counterfactual", { scenario: sc.id, value }); render(); });
    return [section("Counterfactual simulator", "Change one fact on a copy of the case and rerun the dependent checks. The case itself is not changed, and an assumed document is labelled as assumed."),
      h("div", { class: "grid g3" }, p.counterfactuals.map((sc) => h("div", { class: "card tight" }, h("b", null, sc.title), h("p", { class: "small muted", style: "margin:5px 0 10px;min-height:54px" }, sc.expect), h("button", { class: "btn sm", onclick: fire(sc) }, "Run")))),
      cf && cf.pending ? h("p", { style: "margin-top:16px" }, h("span", { class: "spin" }), " Rerunning: " + cf.pending) : null,
      cf && !cf.pending ? h("div", { class: "stack", style: "margin-top:16px" }, cardOf("Impact: " + cf.change, h("div", { class: "row", style: "margin-bottom:8px" }, pill("info", cf.before.label), "→", pill(cf.after.outcome === "proceed" ? "pass" : cf.after.outcome === "review" ? "fail" : "review", cf.after.label), h("span", { class: "small muted right" }, "reran " + cf.reran.length + " agents in " + cf.duration_ms + " ms")), steps(cf.explanation)),
        cf.moved.length ? cardOf("Findings that moved", h("table", null, h("thead", null, h("tr", null, ["Finding", "Before", "After", "Now"].map((x) => h("th", null, x)))), h("tbody", null, cf.moved.map((m) => h("tr", null, h("td", null, m.title), h("td", null, pill(m.before)), h("td", null, pill(m.after)), h("td", { class: "small" }, m.after_message)))))) : null,
        cf.draft ? cardOf("Draft the change would call for: " + cf.draft.subject, h("pre", { class: "mono", style: "white-space:pre-wrap;margin:0" }, cf.draft.body)) : null) : null];
  }
  // ---------------------------------------------------------------------------------------------- the orchestration board
  // One picture of an assessment: the process on top, the boss agent's plan under it, the agents the harness ran, and the two things they
  // all share (the knowledge graph and the shared memory). Every lane uses the same nine columns, so a step reads straight down.
  const OSTEPS = [["Case intake", ["intake"]], ["Document understanding", ["classify", "extract", "privacy"]], ["Parties and graph", ["resolve", "screening", "graph"]], ["Evidence validation", ["evidence"]],
    ["Reference and policy", ["reference", "policy"]], ["Checks, verdict, triage", ["consistency", "integrity", "verify", "triage"]], ["Caseworker review", null], ["Decision and learning", null]];
  const OX = (i) => 140 + 107.5 * (i + 0.5);   // the middle of step i, in a drawing 1000 wide whose first 140 are the lane names
  const TRIGGER = { correction: "a person corrected it", amendment: "an amendment arrived", assessment: "asked again", regression: "regression run" };
  const short = (name) => String(name || "").replace(/ Agent$/, "");
  const ms = (v) => (v >= 1000 ? (v / 1000).toFixed(1) + " s" : (v >= 10 ? Math.round(v) : v) + " ms");
  function orchState() {
    const a = A(), pr = S.c.progress || {}, running = pr.state === "running", runs = running ? (S.c.live || { runs: [] }).runs : a ? a.runs : [];
    const decided = !!S.c.decision && !running && !!a && S.c.decision.assessment === a.id, total = runs.length + (decided ? 2 : 0);   // one beat per agent run, then the person's two steps once they happened
    const t = S.orch.t === null || S.orch.t > total ? total : S.orch.t, replay = t < total, seen = runs.slice(0, t), now = replay ? runs[t] : null;
    const doneStages = new Set(seen.map((r) => r.stage)), need = a ? a.findings.filter((f) => ["fail", "missing", "review"].includes(f.status)).length : 0;
    const passes = (S.c.history || []).length + (a ? 1 : 0), again = [...(S.c.history || []), ...(a ? [a] : [])].slice(1);
    const steps = OSTEPS.map(([title, st], i) => {
      if (st) { const mine = seen.filter((r) => st.includes(r.stage)), working = (now && st.includes(now.stage)) || (!replay && running && (st.includes(pr.stage) || (mine.length && !st.every((s) => doneStages.has(s)))));
        const state = pr.state === "failed" && !replay && st.includes(pr.stage) ? "failed" : st.every((s) => doneStages.has(s)) ? "done" : working ? "working" : "waiting";
        return { i, title, st, runs: mine, now: now && st.includes(now.stage) ? now : null, liveStage: !replay && running && st.includes(pr.stage) ? pr.stage : null, state }; }
      const at = t - runs.length, ready = !!a && !running && at >= 0;   // the person's steps: reached once every agent has run
      const state = !ready ? "waiting" : i === 6 ? (decided && at >= 1 ? "done" : "person") : decided && at >= 2 ? "done" : decided && at === 1 ? "person" : "waiting";
      return { i, title, st: null, runs: [], state };
    });
    return { a, pr, running, runs, decided, total, t, replay, seen, now, steps, need, passes, again, stepOf: (stage) => OSTEPS.findIndex(([, st]) => st && st.includes(stage)) };
  }
  function orchStatus(o) {
    if (o.pr.state === "failed" && !o.replay) return "Stopped at " + o.pr.stage + ": " + (o.pr.error || "");
    if (o.replay) return "Replay · beat " + (o.t + 1) + " of " + o.total + " · " + (o.now ? o.now.name + ": " + o.now.summary : o.t === o.runs.length ? "The boss agent hands the case to a person" : "A person decides; the workspace learns the outcome");
    if (o.running) { const ag = S.cfg.agents.find((x) => x.stage === o.pr.stage); return "Live · " + (ag ? ag.name + " is working" : "starting") + " · " + o.runs.length + " of " + S.cfg.stages.length + " agent runs done"; }
    if (!o.a) return "Not assessed yet. Assess the case to watch the agents work.";
    if (o.runs.length && !o.runs[0].plan) return "Assessed by an earlier version, which kept no plan and no hand-offs. Assess it again to see them.";
    return "Assessment " + o.a.id + " · " + o.runs.length + " agent runs in " + ms(o.a.metrics.duration_ms) + " · " + o.a.recommendation.label + (o.decided ? " · decided: " + S.c.status : " · waiting for a person");
  }
  function orchRefresh() {   // redraw the board in place: the slider keeps the pointer that is dragging it
    const o = orchState(), swap = (id, el) => { const old = document.getElementById(id); if (old) old.replaceWith(el); return !!old; };
    if (!swap("orch-body", orchBody(o))) return render();
    swap("orch-btns", orchButtons(o)); swap("orch-status", h("span", { id: "orch-status", class: "ostatus" }, orchStatus(o)));
    const r = document.getElementById("orch-range"); if (r) { r.max = o.total; if (document.activeElement !== r || S.orch.playing) r.value = o.t; }
  }
  function orchPlay(from) {
    clearInterval(S.orch.timer); const o = orchState(); if (!o.total) return;
    S.orch.t = from !== undefined ? from : o.replay ? o.t : 0; S.orch.playing = true;
    S.orch.timer = setInterval(() => { const n = orchState(); if (!S.c || S.view !== "monitor" || !document.getElementById("orch-body")) { clearInterval(S.orch.timer); S.orch.playing = false; return; }
      if (n.t + 1 >= n.total) { clearInterval(S.orch.timer); S.orch.playing = false; S.orch.t = null; } else S.orch.t = n.t + 1; orchRefresh(); }, 1100 / (S.orch.speed || 1));
    orchRefresh();
  }
  const orchStop = () => { clearInterval(S.orch.timer); S.orch.playing = false; };
  function orchButtons(o) {
    const live = S.orch.t === null || !o.replay;
    return h("span", { id: "orch-btns", class: "row", style: "gap:6px;flex-wrap:nowrap" },
      h("button", { class: "obtn" + (live ? " on" : ""), title: "Follow the assessment as it is now", onclick: () => { orchStop(); S.orch.t = null; orchRefresh(); } }, h("i", { class: "dot" + (o.running ? " pulse" : "") }), "Live"),
      h("button", { class: "obtn" + (S.orch.playing ? " on" : ""), disabled: !o.total, title: "Play the assessment back, one agent at a time", onclick: () => { if (S.orch.playing) { orchStop(); orchRefresh(); } else orchPlay(); } }, S.orch.playing ? "❚❚ Pause" : "▶ Replay"),
      h("button", { class: "obtn", title: "Replay speed", onclick: () => { S.orch.speed = { 1: 2, 2: 4, 4: 0.5, 0.5: 1 }[S.orch.speed || 1]; if (S.orch.playing) orchPlay(orchState().t); else orchRefresh(); } }, (S.orch.speed || 1) + "×"));
  }
  function orchestration() {
    const o = orchState();
    if (S.orch.auto && o.a && !o.running) { S.orch.auto = false; if ((S.orch.ticks || 0) < 5 && o.total) setTimeout(() => { if (S.c && S.view === "monitor") orchPlay(0); }, 60); }   // a sample that finished at once is played back once, so there is something to watch
    return h("div", { class: "card orch", id: "orch" },
      h("div", { class: "obar" }, orchButtons(o),
        h("input", { id: "orch-range", type: "range", min: 0, max: o.total, step: 1, value: o.t, "aria-label": "Position in the assessment", oninput: (ev) => { orchStop(); const v = +ev.target.value; S.orch.t = v >= orchState().total ? null : v; orchRefresh(); } }),
        h("span", { id: "orch-status", class: "ostatus" }, orchStatus(o)),
        o.a && !o.running && o.runs.length && !o.runs[0].plan ? h("button", { class: "btn sm", style: "flex:none", onclick: run(async () => setCase(await api("cases/" + S.c.reference + "/assess", {}))) }, "Assess again") : null),
      orchBody(o));
  }
  function orchBody(o) {
    const sel = S.orch.sel, pick = (s) => () => { S.orch.sel = sel && sel.kind === s.kind && sel.i === s.i && sel.id === s.id ? null : s; orchRefresh(); };
    const isSel = (s) => !!sel && sel.kind === s.kind && sel.i === s.i && sel.id === s.id;
    const lane = (name, say, cells, cls) => h("div", { class: "olane " + (cls || "") }, h("div", { class: "oname" }, h("b", null, name), say ? h("span", null, say) : null), cells);
    const col = (i) => "grid-column:" + (i + 2);
    // process layer, with what sent the case round again drawn above it
    const loops = Object.entries(o.again.reduce((m, x) => { m[x.trigger] = (m[x.trigger] || 0) + 1; return m; }, {}));
    const loopRow = loops.length ? h("div", { class: "oloops" }, h("svg", { viewBox: "0 0 1000 30", preserveAspectRatio: "none", "aria-hidden": "true" },
        loops.map((l, k) => { const x1 = OX(6) - k * 10, x0 = OX(0) + k * 10, top = 4 + k * 7; return [h("path", { d: "M" + x1 + ",30 C" + x1 + "," + top + " " + x0 + "," + top + " " + x0 + ",26", class: "loop" }), h("path", { d: "M" + (x0 - 3.5) + ",22 L" + x0 + ",29 L" + (x0 + 3.5) + ",22", class: "loop" })]; })),
      h("span", { class: "olooptxt", style: "left:" + (OX(3) / 10) + "%" }, "Loop back: " + loops.map(([tr, n]) => (TRIGGER[tr] || tr) + (n > 1 ? " ×" + n : "")).join(", ") + " · assessed " + o.passes + " times")) : null;
    const process = lane("Process layer", "what happens to a case", o.steps.map((s) => { const retries = s.runs.reduce((n, r) => n + (r.retries || 0), 0), took = s.runs.reduce((n, r) => n + r.duration_ms, 0);
      return h("button", { class: "ostep " + s.state + (isSel({ kind: "step", i: s.i }) ? " sel" : ""), style: col(s.i), onclick: pick({ kind: "step", i: s.i }), title: s.runs.map((r) => r.summary).join(" · ") },
        h("span", { class: "no" }, "0" + (s.i + 1)), h("b", null, s.title),
        h("span", { class: "st" }, s.state === "working" ? [h("span", { class: "spin" }), " working now"] : s.state === "person" ? "waits for a person" : s.state === "failed" ? "stopped" : s.state === "done" ? "done" + (s.st && o.passes > 1 ? " ×" + o.passes : "") + (s.st ? " · " + ms(Math.round(took * 10) / 10) : "") : "–", retries ? " · " + retries + " retry" : "")); }));
    // the boss agent: the orchestrator's plan for each step, and how far it got
    const boss = lane("Boss agent", "the orchestrator: plans, hands out, holds for people", o.steps.map((s) => { let head, sub, cls = s.state;
      if (s.st) { const all = [...s.runs, ...(s.now ? [s.now] : [])], tasks = all.reduce((n, r) => n + ((r.plan || {}).tasks || []).length, 0), done = s.runs.reduce((n, r) => n + ((r.plan || {}).done || 0), 0), by = (all[0] && all[0].plan && all[0].plan.by) || "";
        if (!all.length) { head = s.state === "working" ? "planning…" : "–"; sub = ""; if (s.state !== "working") cls = "waiting"; }
        else if (!all[0].plan) { head = "done"; sub = "plan not kept by the earlier version"; }
        else if (!tasks) { head = "no plan"; sub = "nothing to do here"; }
        else { head = done + "/" + tasks + " done"; sub = s.now ? "handing out " + (s.now.plan.tasks || []).length + " task(s)" : by ? "asked by " + by : ""; } }
      else if (s.i === 6) { head = s.state === "waiting" ? "–" : s.state === "done" ? "handed over" : "holds for a person"; sub = s.state === "waiting" ? "" : o.need + " finding(s) need a person"; }
      else { head = s.state === "done" ? "decision recorded" : s.state === "person" ? "waits for the decision" : "–"; sub = s.state === "done" ? "by " + S.c.decision.by : ""; }
      return h("button", { class: "oboss " + cls + (isSel({ kind: "step", i: s.i }) ? " sel" : ""), style: col(s.i), onclick: pick({ kind: "step", i: s.i }) }, head === "–" ? h("span", { class: "faint" }, "–") : h("span", null, h("span", { class: "who" }, "Boss · "), h("b", null, head)), sub ? h("span", { class: "sub" }, sub) : null); }));
    // harness -> agents: each run under its contract
    const chip = (r, live) => { const c = r.contract || {}, bad = c.kept === false, odd = !bad && r.status !== "ok", n = (c.promises || []).length, ok = (c.promises || []).filter((x) => x.ok).length;
      return h("button", { class: "oagent " + (live ? "working" : bad ? "bad" : odd ? "odd" : "ok") + (isSel({ kind: "run", id: r.id }) ? " sel" : ""), onclick: live ? null : pick({ kind: "run", id: r.id }), title: r.summary || "" },
        h("span", { class: "nm" }, live ? h("span", { class: "spin" }) : h("i", { class: "mark" }, bad ? "✕" : odd ? "!" : "✓"), short(r.name)),
        h("span", { class: "sub" }, live ? "running under its contract" : (bad ? (n - ok) + " of " + n + " promise(s) broken" : ok + "/" + n + " promises kept") + " · " + (r.model_calls ? r.model_calls + " model call(s)" : "code") + " · " + ms(r.duration_ms))); };
    const harness = lane("Harness → agents", "every agent runs under its contract", o.steps.map((s) => { let kids;
      if (s.st) { kids = s.runs.map((r) => chip(r)); if (s.now) kids.push(chip(s.now, true)); else if (s.liveStage) { const ag = S.cfg.agents.find((x) => x.stage === s.liveStage); if (ag) kids.push(chip({ id: "live", name: ag.name }, true)); } }
      else if (s.i === 6 && s.state !== "waiting") kids = [h("button", { class: "oagent person" + (isSel({ kind: "step", i: 6 }) ? " sel" : ""), onclick: pick({ kind: "step", i: 6 }) }, h("span", { class: "nm" }, h("i", { class: "mark" }, "☺"), "Caseworker"), h("span", { class: "sub" }, S.c.reviews.length + " review action(s)"))];
      else if (s.i === 7 && s.state !== "waiting") kids = [h("button", { class: "oagent " + (s.state === "done" ? "ok" : "person") + (isSel({ kind: "step", i: 7 }) ? " sel" : ""), onclick: pick({ kind: "step", i: 7 }) }, h("span", { class: "nm" }, h("i", { class: "mark" }, s.state === "done" ? "✓" : "☺"), "Caseworker"), h("span", { class: "sub" }, s.state === "done" ? S.c.status : "decides"))];
      return h("div", { class: "ocell", style: col(s.i) }, kids && kids.length ? kids : h("span", { class: "faint" }, "–")); }));
    // knowledge graph: what the harness handed each step from it, and what the case gave back
    const kn = (S.c.learned || {}).knowledge, afterRuns = !!o.a && !o.running && o.t >= o.runs.length && !!S.c.learned;
    const handed = o.steps.map((s) => { const ids = new Set(); let n = 0; const lv = {}; s.runs.forEach((r) => { const g = r.grounding; if (!g) return; (g.ids || []).forEach((x) => ids.add(x)); n = Math.max(n, g.nodes || 0); Object.entries(g.by_level || {}).forEach(([k, v]) => (lv[k] = Math.max(lv[k] || 0, v))); }); return { n: Math.max(n, ids.size), lv }; });
    const kgCells = o.steps.map((s) => { const g = handed[s.i], learned = s.i === 5 && afterRuns ? S.c.learned.entities : 0, out = s.i === 7 && s.state === "done";
      return h("button", { class: "ocount" + (isSel({ kind: "kg", i: s.i }) ? " sel" : ""), style: col(s.i), disabled: !g.n && !learned && !out, onclick: pick({ kind: "kg", i: s.i }) },
        g.n ? h("span", { class: "up", title: g.n + " graph node(s) handed to this step" }, "▲ " + g.n, h("span", { class: "lv" }, Object.keys(g.lv).map((k) => h("i", { style: "background:" + (LCOL[k] || "#7a8496"), title: (LVL[k] || k) + ": " + g.lv[k] })))) : null,
        learned ? h("span", { class: "down", title: learned + " node(s) of this case written back to the knowledge graph" }, "▼ " + learned + " learned") : null, out ? h("span", { class: "down", title: "The decision is written back to the knowledge graph" }, "▼ outcome") : null); });
    const kgBar = h("button", { class: "obarline kg" + (isSel({ kind: "kgbar" }) ? " sel" : ""), onclick: pick({ kind: "kgbar" }) }, h("b", null, "Knowledge graph"),
      kn ? h("span", null, kn.entities.toLocaleString() + " nodes · " + kn.relationships.toLocaleString() + " links · 4 levels") : h("span", null, "the workspace's graph · 4 levels"),
      kn && kn.levels ? h("span", { class: "lvls" }, ["industry", "domain", "solution", "customer", "added"].filter((k) => kn.levels[k]).map((k) => h("span", null, h("i", { style: "background:" + LCOL[k] }), LVL[k] + " " + kn.levels[k].toLocaleString()))) : null);
    const kg = h("div", { class: "olane kgl" }, h("div", { class: "oname" }, h("b", null, "Knowledge graph"), h("span", null, "▲ nodes handed · ▼ learned")), kgCells, h("div", { class: "ospan" }, kgBar));
    // shared memory: what each step read of what earlier steps left, and what it left itself
    const mem = o.steps.map((s) => ({ read: s.runs.reduce((n, r) => n + ((r.sections || {}).read || []).length + ((r.memory || {}).reads || []).length, 0), left: s.runs.reduce((n, r) => n + ((r.sections || {}).wrote || []).length + 1, 0) }));
    const arcs = {}; o.seen.forEach((r) => ((r.sections || {}).read || []).forEach((x) => (x.from || []).forEach((ag) => { const w = o.runs.find((y) => y.agent === ag), f = w ? o.stepOf(w.stage) : -1, to = o.stepOf(r.stage); if (f >= 0 && f < to) arcs[f + ">" + to] = (arcs[f + ">" + to] || 0) + 1; })));
    const recall = o.a && o.a.memory ? o.a.memory.recall || {} : {}, known = (recall.parties || []).length + (recall.identifiers || []).length, prior = o.seen.some((r) => ((r.memory || {}).reads || []).length), items = o.seen.length + new Set(o.seen.flatMap((r) => (r.sections || {}).wrote || [])).size;
    const hot = sel && sel.kind === "step" ? sel.i : sel && sel.kind === "mem" ? sel.i : sel && sel.kind === "run" ? o.stepOf((o.runs.find((r) => r.id === sel.id) || {}).stage) : o.now ? o.stepOf(o.now.stage) : -1;
    const memCells = o.steps.map((s) => h("button", { class: "ocount" + (isSel({ kind: "mem", i: s.i }) ? " sel" : ""), style: col(s.i), disabled: !mem[s.i].read && !mem[s.i].left, onclick: pick({ kind: "mem", i: s.i }) },
      mem[s.i].read ? h("span", { class: "up", title: "read from shared memory" }, "▲ " + mem[s.i].read + " read") : null, mem[s.i].left ? h("span", { class: "down", title: "left in shared memory" }, "▼ " + mem[s.i].left + " left") : null));
    const memBar = h("button", { class: "obarline mem" + (isSel({ kind: "membar" }) ? " sel" : ""), onclick: pick({ kind: "membar" }) }, h("b", null, "Shared memory"), h("span", null, items + " item(s)" + (items ? ": " + o.seen.length + " note(s), " + (items - o.seen.length) + " part(s) of the case file" : "")));
    const arcSvg = h("div", { class: "oarcs" }, h("svg", { viewBox: "0 0 1000 78", preserveAspectRatio: "none", "aria-hidden": "true" },
        Object.entries(arcs).map(([k, n]) => { const [f, to] = k.split(">").map(Number), x1 = OX(f) + 7, x2 = OX(to) - 7, d = Math.min(74, 16 + 11 * (to - f)), on = hot === f || hot === to;
          return [h("path", { d: "M" + x1 + ",1 C" + x1 + "," + d + " " + x2 + "," + d + " " + x2 + ",5", class: "arc" + (on ? " on" : "") }), h("path", { d: "M" + (x2 - 3.5) + ",8 L" + x2 + ",1 L" + (x2 + 3.5) + ",8", class: "arc head" + (on ? " on" : "") })]; }),
        prior && recall.episodes_searched ? [h("path", { d: "M60,40 C60,14 " + (OX(2) - 20) + ",40 " + (OX(2) - 16) + ",5", class: "arc prior" }), h("path", { d: "M" + (OX(2) - 19.5) + ",8 L" + (OX(2) - 16) + ",1 L" + (OX(2) - 12.5) + ",8", class: "arc head prior" })] : null),
      prior ? h("span", { class: "oprior" }, recall.episodes_searched ? "Before this case: " + recall.episodes_searched + " earlier case(s) searched, " + known + " thing(s) recognised" : "Before this case: no earlier case to recall yet") : null);
    const memory = h("div", { class: "olane meml" }, h("div", { class: "oname" }, h("b", null, "Shared memory"), h("span", null, "▲ read · ▼ left")), h("div", { class: "ospan" }, memBar), memCells, h("div", { class: "ospan" }, arcSvg));
    const legend = h("div", { class: "olegend" }, h("span", null, h("i", { class: "k working" }), "working now"), h("span", null, h("i", { class: "k person" }), "waits for a person"), h("span", null, h("i", { class: "k done" }), "done (×2: passes)"),
      h("span", null, h("i", { class: "k loop" }), "loop back: assessed again"), h("span", null, h("i", { class: "k arc" }), "read from shared memory"), h("span", null, h("i", { class: "k prior" }), "from earlier cases"), h("span", null, h("b", null, "Click anything for its detail")));
    return h("div", { id: "orch-body" }, h("div", { class: "oboard" }, loopRow, process, boss, harness, kg, memory), legend, sel ? orchDetail(o, sel) : null);
  }
  function orchDetail(o, sel) {
    const close = h("button", { class: "btn sm ghost right", onclick: () => { S.orch.sel = null; orchRefresh(); } }, "Close"), a = o.a;
    const toKnowledge = (id) => (id ? kShow(id) : top("knowledge"));   // a node the agent was handed opens on the level map, along the way down to it
    const runBlock = (r) => { const c = r.contract || {}, g = r.grounding, sec = r.sections || {}, m = r.memory || {}, note = a && a.memory ? a.memory.working.find((n) => n.by === r.agent || n.key === r.stage + ".summary") : null;
      return h("div", { class: "orun" }, h("div", { class: "row" }, h("b", null, r.name), pill(r.status === "ok" ? "pass" : r.status === "failed" ? "fail" : "review", r.status), h("span", { class: "mono small muted" }, r.id), h("span", { class: "small muted" }, ms(r.duration_ms) + " · " + r.model + (r.prompt_version ? " · prompt " + r.prompt_version : ""))),
        h("p", { class: "small", style: "margin:4px 0 8px" }, r.summary), (r.notes || []).map((n) => h("p", { class: "small muted" }, n)),
        h("div", { class: "ogrid" },
          h("div", null, h("h4", null, "The boss agent's plan"), ((r.plan || {}).tasks || []).length ? h("ol", null, r.plan.tasks.map((t, k) => h("li", { class: k < r.plan.done ? "ok" : "" }, t))) : h("p", { class: "small muted" }, "No plan: nothing for this agent to do in this case."), h("p", { class: "small muted" }, "Asked by " + ((r.plan || {}).by || "–") + " · " + ((r.plan || {}).trigger || ""))),
          h("div", null, h("h4", null, "Contract " + (c.version ? "v" + c.version : "")), (c.needs || []).map((x) => h("div", { class: "ock " + (x.ok ? "ok" : "bad") }, h("i", null, x.ok ? "✓" : "✕"), h("span", null, h("b", null, "Needs: "), x.detail))), (c.promises || []).map((x) => h("div", { class: "ock " + (x.ok ? "ok" : "bad") }, h("i", null, x.ok ? "✓" : "✕"), h("span", null, x.text, h("span", { class: "muted" }, " " + x.detail)))),
            h("p", { class: "small muted" }, "If it fails: " + (c.on_failure || "–") + (r.budget && r.budget.max_calls ? " · model budget " + r.budget.calls + "/" + r.budget.max_calls + " calls" : "") + (r.throttle && r.throttle.waited_ms ? " · waited " + r.throttle.waited_ms + " ms" : ""))),
          h("div", null, h("h4", null, "Handed from the knowledge graph"), g && g.nodes ? [h("div", { class: "chips" }, Object.entries(g.by_class).map(([k, v]) => h("span", { class: "chip", style: "cursor:default" }, v + " " + words(k)))), h("div", { class: "chips", style: "margin-top:5px" }, Object.entries(g.by_level).map(([k, v]) => h("span", { class: "chip", style: "cursor:default;border-color:" + (LCOL[k] || "#ccc") }, (LVL[k] || k) + " level: " + v))),
              h("div", { class: "chips", style: "margin-top:5px" }, g.ids.slice(0, 6).map((id) => h("button", { class: "chip", onclick: toKnowledge(id), title: "Open it in the knowledge graph" }, id.split("/").pop().replace(/-/g, " ").slice(0, 34))), g.nodes > 6 ? h("span", { class: "small muted" }, "and " + (g.nodes - 6) + " more") : null),
              g.rules_with_basis ? h("p", { class: "small muted", style: "margin-top:5px" }, "With the rules it was handed what " + g.rules_with_basis + " of them rest on (an article, a control or a term), and it put that on each finding it wrote.") : null] : h("p", { class: "small muted" }, g ? "Its contract names graph nodes, and this use case has none of them yet." : "Its contract asks for nothing from the graph."),
            r.graph_writes && (r.graph_writes.nodes || r.graph_writes.edges) ? h("p", { class: "small muted", style: "margin-top:5px" }, "Wrote +" + r.graph_writes.nodes + " nodes, +" + r.graph_writes.edges + " links to the case graph.") : null),
          h("div", null, h("h4", null, "Shared memory"), (sec.read || []).map((x) => h("div", { class: "small" }, "▲ read ", h("b", null, x.section), (x.from || []).length ? " left by " + x.from.join(", ") : "")), (m.reads || []).map((x) => h("div", { class: "small" }, "▲ read ", h("b", null, x.replace("workspace:", "the workspace's ")))),
            (sec.wrote || []).map((x) => h("div", { class: "small" }, "▼ left ", h("b", null, x))), note ? h("div", { class: "small" }, "▼ left a note: ", h("span", { class: "muted" }, note.value)) : null, (m.refused || []).map((x) => h("div", { class: "small", style: "color:var(--bad)" }, "refused: " + x)),
            r.io ? h("p", { class: "mono small muted", style: "margin-top:5px" }, r.io.read.slice(0, 8) + " → " + r.io.wrote.slice(0, 8)) : null))); };
    let title, body;
    if (sel.kind === "run") { const r = o.runs.find((x) => x.id === sel.id); if (!r) return null; title = "Agent run"; body = runBlock(r); }
    else if (sel.kind === "kgbar") { const kn = (S.c.learned || {}).knowledge; title = "The knowledge graph and this case";
      body = [h("p", { class: "small" }, "The harness hands each agent only the graph nodes its contract names. When the assessment ends, the case is written back: its companies, products, reference numbers and findings, each with the line it came from."),
        kn ? kv([["In the graph now", kn.entities.toLocaleString() + " nodes, " + kn.relationships.toLocaleString() + " links"], ["By level", Object.entries(kn.levels || {}).map(([k, v]) => (LVL[k] || k) + " " + v).join(" · ")], ["This case gave", S.c.learned.entities + " nodes, " + S.c.learned.relationships + " links"], ["Learned at", when(S.c.learned.at)]]) : h("p", { class: "small muted" }, "This case has not been written to the graph yet."),
        h("button", { class: "btn sm", style: "margin-top:8px", onclick: toKnowledge() }, "Open the knowledge graph")]; }
    else if (sel.kind === "membar") { const rc = a && a.memory ? a.memory.recall || {} : {}; title = "Shared memory of this case";
      body = [h("p", { class: "small" }, "Agents never call each other. Each one reads what earlier agents left in the case file and leaves its own part and a note. The harness refuses a read or a write its contract does not allow."),
        a && a.memory ? h("table", { style: "margin-top:8px" }, h("tbody", null, a.memory.working.map((n) => h("tr", null, h("td", { class: "small mono", style: "width:150px" }, n.by), h("td", { class: "small" }, n.value))))) : null,
        (rc.parties || []).length + (rc.identifiers || []).length ? h("p", { class: "small", style: "margin-top:8px" }, h("b", null, "From earlier cases: "), [...(rc.parties || []).map((x) => x.name + " (" + x.count + " case(s))"), ...(rc.identifiers || []).map((x) => x.type + " " + x.value + " (" + x.count + ")")].join("; ")) : null]; }
    else { const s = o.steps[sel.i]; title = (sel.kind === "kg" ? "Knowledge graph · " : sel.kind === "mem" ? "Shared memory · " : "") + "0" + (s.i + 1) + " " + s.title;
      if (s.st) body = s.runs.length ? s.runs.map(runBlock) : h("p", { class: "small muted" }, s.state === "working" ? "Working now. Its detail appears when it finishes." : "Not reached yet.");
      else if (s.i === 6) body = [h("p", { class: "small" }, s.state === "waiting" ? "Not reached yet." : "The boss agent stops here: " + o.need + " finding(s) need a person. Nothing is decided by an agent."), S.c.reviews.length ? h("table", { style: "margin-top:8px" }, h("tbody", null, S.c.reviews.slice(-8).map((v) => h("tr", null, h("td", { class: "small muted", style: "width:130px" }, when(v.at)), h("td", { class: "small" }, v.by), h("td", { class: "small" }, v.summary || v.action || v.kind || ""))))) : null,
        o.again.length ? h("p", { class: "small muted", style: "margin-top:8px" }, "Assessed " + o.passes + " times: " + o.again.map((x) => x.id + " because " + (TRIGGER[x.trigger] || x.trigger)).join("; ") + ".") : null, s.state !== "waiting" ? h("button", { class: "btn sm", style: "margin-top:8px", onclick: go("decision") }, "Open the decision workspace") : null];
      else body = S.c.decision && s.state === "done" ? [kv([["Decision", S.c.status], ["By", S.c.decision.by], ["When", when(S.c.decision.at)], ["Differs from the recommendation", S.c.decision.override ? "yes" : "no"], ["Note", S.c.decision.note]]), h("p", { class: "small muted", style: "margin-top:6px" }, "The outcome is written to the knowledge graph and to the workspace's memory, so the next case that meets the same company or reference number is told.")] : h("p", { class: "small muted" }, "A person decides. Until then nothing is filed and nothing is learned as an outcome."); }
    return h("div", { class: "odetail" }, h("div", { class: "row", style: "margin-bottom:8px" }, h("h3", null, title), close), body);
  }

  function monitor() {
    const a = A(), pr = S.c.progress || {}, live = S.c.live || { stages: {}, runs: [] };
    const runs = pr.state === "running" ? live.runs : a ? a.runs : [];
    return [section("Agent orchestration", "Watch the case move. The boss agent plans each step, the harness runs each agent under its contract, and every agent works from the same knowledge graph and the same shared memory. Play it back, or click any part for its detail."),
      pr.state === "failed" ? h("div", { class: "banner review", style: "margin-bottom:14px" }, h("b", null, "The assessment stopped at " + pr.stage), h("p", null, pr.error)) : null,
      orchestration(), h("div", { style: "height:16px" }),
      cardOf("Agent runs", runs.length ? h("table", null, h("thead", null, h("tr", null, ["Run", "Agent", "Status", "Contract", "Time", "Model", "Prompt", "Tool calls", "Graph writes", "Tokens in / out"].map((x) => h("th", null, x)))), h("tbody", null, runs.map((r) => h("tr", null, h("td", { class: "mono small" }, r.id), h("td", null, h("b", null, r.name), h("div", { class: "small muted" }, r.summary)), h("td", null, pill(r.status === "ok" ? "pass" : r.status === "failed" ? "fail" : "review", r.status), (r.notes || []).map((n) => h("div", { class: "small muted" }, n))),
        h("td", null, r.contract ? [pill(r.contract.kept ? "pass" : "fail", r.contract.kept ? "kept" : "not kept"), h("div", { class: "small muted" }, r.contract.promises.length + " promise(s) checked" + (r.memory && r.memory.reads.length ? " · read " + r.memory.reads.join(", ") : "") + (r.throttle && r.throttle.waited_ms ? " · waited " + r.throttle.waited_ms + " ms" : "")), (r.contract.broken || []).map((b) => h("div", { class: "small", style: "color:var(--bad)" }, b))] : "–"),
        h("td", { class: "mono small" }, r.duration_ms + " ms"), h("td", { class: "small" }, r.model), h("td", { class: "small mono" }, r.prompt_version || "–"), h("td", { class: "small" }, (r.tool_calls || []).map((t) => h("div", null, t.tool + " · " + t.status)), !(r.tool_calls || []).length ? "–" : null), h("td", { class: "small mono" }, "+" + r.graph_writes.nodes + " / +" + r.graph_writes.edges), h("td", { class: "small mono" }, r.input_tokens + " / " + r.output_tokens))))) : h("p", { class: "muted" }, "No run yet.")),
      a ? h("p", { class: "small muted", style: "margin-top:10px" }, "End to end: " + a.metrics.duration_ms + " ms · " + a.metrics.model_calls + " model call(s) · " + a.metrics.rules + " rule(s) · " + a.metrics.pages + " page(s) · reads: " + a.model.provider + ".") : null,
      a && a.memory ? h("details", { class: "card", style: "margin-top:12px" }, h("summary", null, "Working memory: the note each agent left for the next (" + a.memory.working.length + ")"), h("table", { style: "margin-top:8px" }, h("tbody", null, a.memory.working.map((n) => h("tr", null, h("td", { class: "small mono", style: "width:150px" }, n.by), h("td", { class: "small" }, n.value)))))) : null,
      a ? h("details", { class: "card", style: "margin-top:12px", ontoggle: run(async (ev) => { if (ev.target.open && !S.sel.ledger) { S.sel.ledger = await api("ledger?limit=200&case=" + encodeURIComponent(S.c.reference)); render(); } }), open: !!S.sel.ledger }, h("summary", null, "Provenance ledger: every action on this case, chained"),
        S.sel.ledger ? [h("p", { class: "small muted", style: "margin:8px 0" }, "Chain of the whole workspace: ", S.sel.ledger.verify.ok ? pill("pass", "intact, " + S.sel.ledger.verify.entries + " entries") : pill("fail", "broken at entry " + S.sel.ledger.verify.broken_at)),
          h("table", null, h("thead", null, h("tr", null, ["#", "When", "Who", "What", "Fingerprint"].map((x) => h("th", null, x)))), h("tbody", null, S.sel.ledger.entries.slice().reverse().map((e) => h("tr", null, h("td", { class: "mono small" }, e.n), h("td", { class: "small muted" }, when(e.at)), h("td", { class: "small" }, e.actor), h("td", { class: "small" }, e.summary), h("td", { class: "mono small" }, e.hash.slice(0, 10))))))] : null) : null,
      !a && pr.state !== "running" ? h("button", { class: "btn primary", style: "margin-top:12px", onclick: run(async () => setCase(await api("cases/" + S.c.reference + "/assess", {}))) }, "Assess the case") : null];
  }
  function decision() {
    const a = A(), need = a.findings.filter((f) => ["fail", "missing", "review"].includes(f.status));
    const draft = (kind) => run(async () => { S.draft = await api("cases/" + S.c.reference + "/correspondence", { kind }); render(); });
    const decide = (d, label) => h("button", { class: "btn" + (d === "approve" ? " primary" : ""), onclick: run(async () => { const v = await form(label, [{ k: "note", label: "Note (required when it differs from the recommendation)", type: "textarea" }], label); if (v) setCase(await api("cases/" + S.c.reference + "/decision", { decision: d, note: v.note })); }) }, label);
    return [section("Decision workspace", "The caseworker decides. The recommendation and every finding are here to inspect, correct and overrule; a decision that differs from the recommendation needs a note."),
      h("div", { class: "stack" }, banner(a.recommendation), triageCard(a),
        S.c.decision ? cardOf("Decision recorded", kv([["Decision", S.c.status], ["By", S.c.decision.by], ["When", when(S.c.decision.at)], ["On assessment", S.c.decision.assessment], ["Differs from recommendation", S.c.decision.override ? "yes" : "no"], ["Note", S.c.decision.note]])) : null,
        !S.c.decision && (S.c.earlier_decisions || []).length ? cardOf("Decide again", h("p", { class: "small" }, "A decision was recorded, and then the case was assessed again. A decision belongs to the assessment it was taken on, so this one waits for a new decision."),
          h("table", { style: "margin-top:8px" }, h("thead", null, h("tr", null, ["Earlier decision", "By", "When", "On assessment", "Note"].map((x) => h("th", null, x)))), h("tbody", null, S.c.earlier_decisions.slice().reverse().map((g) => h("tr", null, h("td", null, g.decision), h("td", { class: "small" }, g.by), h("td", { class: "small muted" }, when(g.at)), h("td", { class: "small mono" }, g.assessment), h("td", { class: "small" }, g.note || "–")))))) : null,
        (a.unresolved || []).length ? cardOf("Possible entity matches to settle", a.unresolved.map((m) => h("div", { class: "row", style: "margin-bottom:6px" }, m.reason, matchButtons(m)))) : null,
        h("div", null, h("h3", { style: "margin:4px 0 10px;font-size:15px" }, "Findings that need a person (" + need.length + ")"), h("div", { class: "stack" }, need.map((f) => findingCard(f, { review: true })), !need.length ? h("p", { class: "muted" }, "None.") : null)),
        cardOf("Correspondence", h("div", { class: "row", style: "margin-bottom:10px" }, h("button", { class: "btn sm", onclick: draft("evidence_request") }, "Draft an evidence request"), h("button", { class: "btn sm", onclick: draft("decision_note") }, "Draft a decision note")),
          S.draft ? (S.draft.body ? [h("label", { class: "f" }, h("span", null, "Subject"), h("input", { type: "text", value: S.draft.subject })), h("label", { class: "f", style: "margin-top:8px" }, h("span", null, "Draft (" + S.draft.method + "). " + S.draft.note), h("textarea", { style: "min-height:190px", value: S.draft.body }))] : h("p", { class: "muted" }, S.draft.note)) : h("p", { class: "muted small" }, "A draft is written from the findings for you to edit. Nothing is sent from here.")),
        cardOf("Decide", h("div", { class: "row" }, decide("approve", "Approve"), decide("request_evidence", "Request evidence"), decide("escalate", "Escalate for review"), decide("reject", "Reject"))),
        cardOf("Decision record", recordButtons()),
        cardOf("Content repository", h("p", { class: "small muted", style: "margin-bottom:8px" }, S.cfg.repository.name + ": " + S.cfg.repository.detail + (S.cfg.repository.note ? " " + S.cfg.repository.note : "")),
          S.c.filed ? kv([["Filed", when(S.c.filed.filed_at)], ["Documents", S.c.filed.documents.map((d) => d.name + " → " + d.repository_id).join("; ")]]) : null,
          h("button", { class: "btn sm", style: "margin-top:8px", onclick: run(async () => { await api("cases/" + S.c.reference + "/file", {}); toast("Filed to the repository"); open(S.c.reference, "decision"); }) }, S.c.filed ? "File again" : "File the case and its assessment")))];
  }
  function audit() {
    if (!S.fb || S.fb.profile !== S.c.profile) { api("feedback/" + S.c.profile).then((fb) => { S.fb = Object.assign(fb, { profile: S.c.profile }); render(); }).catch((e) => toast(e.message)); }
    const fb = S.fb && S.fb.profile === S.c.profile ? S.fb : null;
    const act = (p, decision, accept) => run(async () => { await api("feedback/" + S.c.profile + "/" + p.id, { decision, accept_changes: !!accept }); S.fb = null; render(); });
    const proposal = (p) => {
      const open = p.status === "proposed" || p.status === "held", r = p.regression;
      return h("div", { style: "padding:8px 0;border-bottom:1px solid var(--line)" },
        h("div", { class: "row" }, pill({ proposed: "info", approved: "pass", rejected: "", held: "review" }[p.status], p.status), h("b", null, p.summary)),
        h("div", { class: "small muted" }, p.kind + " · from case " + p.origin.case + " (" + p.origin.by + ")" + (p.overlay_version ? " · promoted as vocabulary v" + p.overlay_version : "")),
        r ? h("div", { class: "small", style: "margin-top:4px" }, "Regression on " + r.samples + " sample(s), " + r.checks + " checks: " + (r.passed ? "no change" : r.changed.length + " change(s): " + r.changed.join("; "))) : null,
        open ? h("div", { class: "row", style: "margin-top:6px" },
          p.status === "held" ? h("button", { class: "btn sm", onclick: act(p, "approve", true) }, "Accept the changes and approve") : h("button", { class: "btn sm", onclick: act(p, "approve") }, "Approve (runs regression)"),
          h("button", { class: "btn sm", onclick: act(p, "reject") }, "Reject")) : null);
    };
    const proposals = !fb ? h("p", { class: "muted" }, "Loading…") : fb.proposals.length ? fb.proposals.map(proposal) : h("p", { class: "muted small" }, "No proposals yet. Correct a classification or settle an entity match to produce one.");
    const versions = S.c.documents.flatMap((d) => d.versions.map((v) => h("tr", null, h("td", null, v.name || d.name, d.removed ? " (removed)" : ""), h("td", { class: "mono small" }, "v" + v.v + (v.v === d.current ? " current" : "")),
      h("td", { class: "mono small" }, (v.sha256 || "").slice(0, 12)), h("td", { class: "small muted" }, when(v.uploaded_at) + (v.note ? " · " + v.note : "")))));
    return [section("Feedback and audit history", "Every event on the case, each document version, and the improvement proposals that corrections produced. A proposal changes nothing until it is approved, and approval runs a regression on the bundled samples first."),
      h("div", { class: "split" }, cardOf("Audit trail", h("ul", { class: "timeline" }, [...S.c.events].reverse().map((e) => h("li", null, h("time", null, when(e.at) + " · " + e.by + " · " + e.kind), e.detail)))),
        h("div", { class: "stack" }, cardOf("Document versions", h("table", null, h("tbody", null, versions))), cardOf("Improvement proposals" + (fb ? " · vocabulary v" + fb.overlay.version : ""), proposals)))];
  }

  // ---------------------------------------------------------------------------------------------- graph
  const COLORS = { Case: "#0b1220", Document: "#4a6fb5", DocumentVersion: "#8fa9d8", EvidenceAssertion: "#c3ccd9", Party: "#ee6c2b", GoodsItem: "#1b7f52", ProductCharacteristic: "#79c3a1", Invoice: "#a8589e", Shipment: "#a8589e", Declaration: "#a8589e", ReferenceLine: "#c79a1a", RuleEvaluation: "#7a8496", Rule: "#b3bac6", Recommendation: "#b8342b" };
  let layoutKey = "", layout = null;
  function graph() {
    const g = A().graph, on = (n) => S.layers[n.layer] || n.layer === "case" || (S.sel.path || []).includes(n.id) || n.id === S.sel.node;
    const nodes = g.nodes.filter(on), ids = new Set(nodes.map((n) => n.id)), edges = g.edges.filter((e) => ids.has(e.from) && ids.has(e.to));
    if (!S.layers.assertion) {   // with the assertions folded away, a document is joined straight to what its facts mention
      const seen = new Set(edges.map((e) => e.from + ">" + e.to));
      g.edges.forEach((e) => { const doc = e.from.split(".")[0], key = doc + ">" + e.to; if ((e.type === "MENTIONS" || e.type === "SUPPORTS") && e.from.includes(".") && !ids.has(e.from) && ids.has(doc) && ids.has(e.to) && !seen.has(key)) { seen.add(key); edges.push({ from: doc, to: e.to, type: e.type + " (through its facts)" }); } });
    }
    const key = A().id + S.c.reference + nodes.length + ":" + edges.length, W = 1100, H = 600;
    if (key !== layoutKey) {
      // Fruchterman-Reingold: nodes repel, edges pull, the temperature cools; deterministic start so a case always draws the same
      const pos = {}, n0 = nodes.length || 1, k = Math.sqrt((W * H) / n0) * 0.72;
      nodes.forEach((n, i) => { const t = i * 2.399963, r = n.layer === "case" ? 0 : Math.sqrt((i + 0.5) / n0) * Math.min(W, H) * 0.46; pos[n.id] = { x: W / 2 + r * Math.cos(t) * 1.5, y: H / 2 + r * Math.sin(t) }; });
      for (let it = 0; it < 320; it++) {
        const f = {}, temp = 34 * (1 - it / 320) + 0.6; nodes.forEach((n) => (f[n.id] = { x: 0, y: 0 }));
        for (let i = 0; i < nodes.length; i++) for (let j = i + 1; j < nodes.length; j++) { const a = pos[nodes[i].id], b = pos[nodes[j].id], dx = a.x - b.x || 0.01, dy = a.y - b.y || 0.01, d = Math.max(Math.sqrt(dx * dx + dy * dy), 4), rep = (k * k) / d; f[nodes[i].id].x += (dx / d) * rep; f[nodes[i].id].y += (dy / d) * rep; f[nodes[j].id].x -= (dx / d) * rep; f[nodes[j].id].y -= (dy / d) * rep; }
        edges.forEach((e) => { const a = pos[e.from], b = pos[e.to], dx = a.x - b.x, dy = a.y - b.y, d = Math.max(Math.sqrt(dx * dx + dy * dy), 4), att = (d * d) / k; f[e.from].x -= (dx / d) * att; f[e.from].y -= (dy / d) * att; f[e.to].x += (dx / d) * att; f[e.to].y += (dy / d) * att; });
        nodes.forEach((n) => { const p = pos[n.id], v = f[n.id], m = Math.sqrt(v.x * v.x + v.y * v.y) || 1; p.x += (v.x / m) * Math.min(m, temp) + (W / 2 - p.x) * 0.012; p.y += (v.y / m) * Math.min(m, temp) + (H / 2 - p.y) * 0.02; p.x = Math.min(W - 150, Math.max(24, p.x)); p.y = Math.min(H - 18, Math.max(18, p.y)); });
      }
      layoutKey = key; layout = pos;
    }
    const path = new Set(S.sel.path || []), hot = (e) => (path.has(e.from) || e.from === S.sel.node) && (path.has(e.to) || e.to === S.sel.node);
    const svg = h("svg", { class: "graph", viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": "Case knowledge graph" },
      edges.map((e) => h("line", { x1: layout[e.from].x, y1: layout[e.from].y, x2: layout[e.to].x, y2: layout[e.to].y, class: hot(e) ? "hot" : "" }, h("title", null, e.type))),
      nodes.map((n) => h("g", { class: "nd" + (n.id === S.sel.node || path.has(n.id) ? " sel" : ""), transform: "translate(" + layout[n.id].x + "," + layout[n.id].y + ")", onclick: () => { S.sel.node = n.id; S.sel.path = null; render(); } },
        h("circle", { r: n.layer === "case" ? 11 : n.layer === "assertion" ? 4.5 : 7.5, fill: COLORS[n.type] || "#7a8496", stroke: "#fff", "stroke-width": 1.5 }), n.layer !== "assertion" || n.id === S.sel.node ? h("text", { x: 10, y: 4 }, n.label.length > 34 ? n.label.slice(0, 33) + "…" : n.label) : null, h("title", null, n.type + ": " + n.label))));
    const sel = g.nodes.find((n) => n.id === S.sel.node), nb = sel ? g.edges.filter((e) => e.from === sel.id || e.to === sel.id) : [], name = (id) => (g.nodes.find((n) => n.id === id) || {}).label || id;
    const types = [...new Set(nodes.map((n) => n.type))];
    return [section("Entity relationship graph", "The case as a graph: documents and versions, the facts taken from them, the parties, goods and references they share, the reference lines and the reasoning built on top. Click a node to see what it connects to."),
      h("div", { class: "row", style: "margin-bottom:10px" }, h("div", { class: "seg" }, [["document", "Documents"], ["entity", "Entities"], ["reference", "Reference"], ["reasoning", "Reasoning"], ["assertion", "Evidence assertions"]].map(([k, l]) => h("button", { "aria-pressed": String(!!S.layers[k]), onclick: () => { S.layers[k] = !S.layers[k]; render(); } }, l))),
        h("span", { class: "small muted right" }, nodes.length + " of " + g.nodes.length + " nodes · " + edges.length + " edges"), S.sel.path ? h("button", { class: "btn sm", onclick: () => { S.sel.path = null; render(); } }, "Clear path") : null),
      h("div", { class: "split", style: "grid-template-columns:minmax(0,2.4fr) minmax(0,1fr)" }, h("div", null, svg, h("div", { class: "chips small", style: "margin-top:8px" }, types.map((t) => h("span", { class: "row", style: "gap:5px;margin-right:8px" }, h("span", { style: "width:10px;height:10px;border-radius:50%;display:inline-block;background:" + (COLORS[t] || "#7a8496") }), t)))),
        cardOf(sel ? sel.type : "Select a node", sel ? [h("p", { style: "font-weight:650;margin-bottom:8px;overflow-wrap:anywhere" }, sel.label), kv(Object.entries(sel.props || {}).map(([k, v]) => [k.replace(/_/g, " "), String(v)])), h("h3", { style: "margin-top:12px" }, "Connections (" + nb.length + ")"),
          nb.slice(0, 30).map((e) => h("div", { class: "src", style: "margin-bottom:4px", onclick: () => { S.sel.node = e.from === sel.id ? e.to : e.from; const n = g.nodes.find((x) => x.id === S.sel.node); if (n) S.layers[n.layer] = true; render(); } }, h("span", { class: "muted" }, e.from === sel.id ? e.type + " → " : "← " + e.type + " "), name(e.from === sel.id ? e.to : e.from)))] : h("p", { class: "muted small" }, "Or open a finding and choose “Show graph path” to see the chain of evidence behind it.")))];
  }

  // ---------------------------------------------------------------------------------------------- assistant
  function chat() {
    if (!S.chat) return h("button", { class: "chatbtn", onclick: () => { S.chat = true; render(); } }, "Ask the case");
    const log = h("div", { class: "log" }, h("div", { class: "msg a" }, "Ask about this case. I answer from its documents, its graph and what the knowledge graph says its rules rest on, cite what I used, and say so when they do not hold the answer."),
      S.chatLog.map((m) => h("div", { class: "msg " + m.r + (m.no ? " no" : "") }, m.t, m.src && m.src.length ? h("div", { class: "chips", style: "margin-top:6px" }, [...m.src.filter((s) => s.kind === "knowledge").slice(0, 4), ...m.src.filter((s) => s.kind !== "knowledge").slice(0, 5)].map((s) => (s.kind === "knowledge" ? kchip({ id: s.id, level: "industry", type: "" }, kShort(s.name), "From the knowledge graph. " + s.name + ". " + s.text)
        : h("span", { class: "chip", onclick: () => (s.doc ? openSource(s) : s.id ? (S.view = "provenance", S.filter = { status: "all", family: "all" }, render()) : null) }, s.doc_name ? s.doc_name + (s.page ? " p" + s.page + (s.line ? " l" + s.line : "") : "") : s.id || s.kind)))) : null)));
    const input = h("input", { type: "text", placeholder: "Type your question", "aria-label": "Question" });
    setTimeout(() => { log.scrollTop = log.scrollHeight; input.focus(); }, 0);
    return h("div", { class: "chat", role: "dialog", "aria-label": "Case assistant" }, h("header", null, "Case assistant", h("button", { class: "right", style: "background:none;border:0;color:#fff;cursor:pointer", "aria-label": "Close", onclick: () => { S.chat = false; render(); } }, "✕")), log,
      h("form", { onsubmit: run(async () => { const q = input.value.trim(); if (!q) return; S.chatLog.push({ r: "u", t: q }); render(); const out = await api("cases/" + S.c.reference + "/ask", { question: q }); S.chatLog.push({ r: "a", t: out.answer, no: !out.grounded, src: out.sources }); render(); }) }, input, h("button", { class: "btn primary", type: "submit" }, "Send")));
  }

  start();
})();
