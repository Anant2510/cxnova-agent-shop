/* Utilities shared by the console runtime, libraries, agents and UI. */
const U = {
  $(sel, root = document) { return root.querySelector(sel); },
  $$(sel, root = document) { return Array.from(root.querySelectorAll(sel)); },
  esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  },
  clone(v) { return v === undefined ? undefined : JSON.parse(JSON.stringify(v)); },
  uid(prefix = "id") { return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`; },
  sleep(ms) { return new Promise((r) => setTimeout(r, ms)); },
  isObj(v) { return v !== null && typeof v === "object" && !Array.isArray(v); },
  fmtNum(n, d = 0) {
    if (n === null || n === undefined || Number.isNaN(n)) return "–";
    return Number(n).toLocaleString("en-US", { maximumFractionDigits: d, minimumFractionDigits: d });
  },
  fmtPct(x, d = 1) { return x === null || x === undefined || Number.isNaN(x) ? "–" : `${(x * 100).toFixed(d)}%`; },
  fmtMs(ms) { return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`; },
  now() { return new Date().toISOString(); },
  today() { return new Date().toISOString().slice(0, 10); },
  humanize(name) { return String(name || "").replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase()); },
  get(obj, path) {
    let cur = obj;
    for (const part of String(path).split(".").filter(Boolean)) {
      if (cur === null || cur === undefined) return undefined;
      if (Array.isArray(cur) && /^-?\d+$/.test(part)) { const i = +part; cur = cur[i < 0 ? cur.length + i : i]; }
      else cur = cur[part];
    }
    return cur;
  },
  empty(v) { return v === undefined || v === null || v === "" || (Array.isArray(v) && !v.length) || (U.isObj(v) && !Object.keys(v).length); },
  async gunzipB64(b64) {
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    if (typeof DecompressionStream === "undefined") throw new Error("This browser cannot decompress embedded data");
    const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream("gzip"));
    return await new Response(stream).text();
  },
  el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === undefined || v === null || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "html") node.innerHTML = v;
      else if (k === "text") node.textContent = v;
      else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : String(v));
    }
    for (const c of children.flat()) {
      if (c === null || c === undefined || c === false) continue;
      node.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return node;
  },
  debounce(fn, ms = 150) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; },
  hash(str) { let h = 2166136261; for (let i = 0; i < str.length; i++) { h ^= str.charCodeAt(i); h = Math.imul(h, 16777619); } return (h >>> 0).toString(36); },
  storage: {
    get(key, fallback) { try { const v = localStorage.getItem(key); return v === null ? fallback : JSON.parse(v); } catch { return fallback; } },
    set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* storage unavailable */ } },
  },
};
