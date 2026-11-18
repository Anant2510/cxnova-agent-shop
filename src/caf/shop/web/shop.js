/* Shared helpers for the shop pages: API calls with the stored token, small DOM utilities, formatting. */
(function () {
  const BASE = (function () {
    // works at the root and behind a reverse proxy prefix: everything before "/shop"
    const i = location.pathname.indexOf("/shop");
    return i > 0 ? location.pathname.slice(0, i) : "";
  })();
  const TOKEN_KEY = "cxnova.shop.token";

  function token() { try { return localStorage.getItem(TOKEN_KEY) || ""; } catch (e) { return ""; } }
  function setToken(t) { try { t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY); } catch (e) {} }

  async function api(path, opts) {
    opts = opts || {};
    const headers = Object.assign({ "Accept": "application/json" }, opts.headers || {});
    if (opts.body !== undefined && !(opts.body instanceof FormData)) headers["Content-Type"] = "application/json";
    const t = opts.token !== undefined ? opts.token : token();
    if (t) headers["Authorization"] = "Bearer " + t;
    const res = await fetch(BASE + path, { method: opts.method || (opts.body !== undefined ? "POST" : "GET"), headers,
      body: opts.body !== undefined ? (typeof opts.body === "string" ? opts.body : JSON.stringify(opts.body)) : undefined });
    let data = null;
    const text = await res.text();
    try { data = text ? JSON.parse(text) : null; } catch (e) { data = { detail: text }; }
    if (!res.ok) {
      const err = new Error((data && (data.detail || data.message)) || ("HTTP " + res.status));
      err.status = res.status; err.data = data; throw err;
    }
    return data;
  }

  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "html") el.innerHTML = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (k === "dataset") Object.assign(el.dataset, v);
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children.flat(Infinity)) {
      if (c === null || c === undefined || c === false) continue;
      el.append(c.nodeType ? c : document.createTextNode(String(c)));
    }
    return el;
  }
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }

  function toast(msg, ms) {
    $$(".toast").forEach(x => x.remove());   // one message at a time: the newest replaces the last
    const t = h("div", { class: "toast", role: "status" }, msg);
    document.body.append(t);
    setTimeout(() => t.remove(), ms || 2600);
  }
  function money(v, cur) {
    if (v === null || v === undefined) return "";
    const n = Number(v);
    return (cur === "USD" ? "$" : (cur || "") + " ") + (Number.isInteger(n) ? n.toLocaleString() : n.toFixed(2));
  }
  function num(v) { return Number(v || 0).toLocaleString(); }
  function when(iso) { if (!iso) return ""; const d = new Date(iso); return isNaN(d) ? iso : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }); }
  const CATS = ["strategy", "content", "web", "experimentation", "design", "engineering", "quality", "operations", "compliance", "activation", "analytics", "platform"];
  function catClass(c) { return CATS.includes(c) ? c : ""; }
  function catColor(c) { return CATS.includes(c) ? "var(--cat-" + c + ")" : "var(--ink-3)"; }
  function tierLabel(tiers) {
    const t = new Set(tiers || []);
    if (t.has("complex") || t.has("pro")) return (t.has("fast") || t.has("balanced")) ? "fast + strong model" : "strong model";
    if (t.size) return "fast model";
    return "no model calls";
  }
  function param(name) { return new URLSearchParams(location.search).get(name); }
  function pathTail() { const parts = location.pathname.replace(/\/+$/, "").split("/"); return decodeURIComponent(parts[parts.length - 1] || ""); }
  function copy(text) { navigator.clipboard && navigator.clipboard.writeText(text).then(() => toast("Copied")); }
  function mark() {
    return h("span", { class: "mark", html: "<svg viewBox='0 0 32 32' width='30' height='30' aria-hidden='true'><rect width='32' height='32' rx='7' fill='#1F3D7A'/><circle cx='9' cy='11' r='3' fill='#fff'/><path d='M9 11 L21 11 Q24 11 24 14 L24 22' stroke='#fff' stroke-width='2.4' fill='none' stroke-linecap='round'/><path d='M6 24 H26' stroke='#fff' stroke-width='2.4' stroke-linecap='round'/></svg>" });
  }
  function topbar(current) {
    const product = document.body.dataset.product || "CX Nova Agent Orchestration Layer";
    const company = document.body.dataset.company || "Coforge";
    const links = [["shop", "Agents", BASE + "/shop"], ["designer", "Designer", BASE + "/shop/designer"], ["console", "Console", BASE + "/console"],
      ["account", "My account", BASE + "/shop/account"], ["admin", "Admin", BASE + "/shop/admin"], ["docs", "API", BASE + "/docs"]];
    return h("header", { class: "topbar" }, h("div", { class: "inner" },
      h("a", { class: "brand", href: BASE + "/shop" }, mark(), h("div", null, h("div", { class: "name" }, product), h("div", { class: "by" }, "by " + company))),
      h("nav", { class: "nav", "aria-label": "Sections" }, links.map(([id, label, href]) => h("a", { href, "aria-current": id === current ? "page" : null }, label)))));
  }
  function footer() {
    const product = document.body.dataset.product || "CX Nova Agent Orchestration Layer";
    return h("footer", { class: "site" }, h("div", { class: "inner" }, h("span", null, product + " · built on the Coforge Agentic Framework"),
      h("a", { href: BASE + "/docs" }, "API reference"), h("a", { href: BASE + "/.well-known/agent-card.json" }, "A2A agent card"),
      h("a", { href: BASE + "/mcp" }, "MCP endpoint")));
  }
  window.Shop = { BASE, api, token, setToken, h, $, $$, clear, toast, money, num, when, catClass, catColor, tierLabel, param, pathTail, copy, topbar, footer };
})();
