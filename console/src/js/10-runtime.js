/* CAF browser runtime: the same agent contracts, prompts, skills and schemas as the Python
   framework, executed in the page. Model calls go to Claude through the page's `sample`
   capability; when Claude is unavailable (or Offline mode is chosen) the offline model returns
   schema-valid placeholders, exactly like the Python offline provider. */
const CAF = (window.CAF = {
  version: "1.0.0",
  boot: null, catalog: {}, order: [], skills: {}, playbooks: {}, planner: {}, schemas: {},
  impl: {}, health: {}, runs: [], listeners: new Set(),
  remote: {}, packs: [],   // hosted console: agents of the agent packs (Campaign Studio, ARM SDLC ...) run on the server
  categories: [
    { id: "strategy", label: "Strategy and planning" },
    { id: "content", label: "Content creation" },
    { id: "web", label: "Website optimization" },
    { id: "experimentation", label: "End-to-end testing" },
  ],
  DEMO_HOST: "www.meridian-mutual.example",

  agent(id, impl) { CAF.impl[id] = impl; },
  emit(evt) { for (const fn of CAF.listeners) { try { fn(evt); } catch (e) { console.error(e); } } },
  on(fn) { CAF.listeners.add(fn); return () => CAF.listeners.delete(fn); },

  init() {
    const raw = document.getElementById("caf-boot");
    CAF.boot = JSON.parse(raw.textContent);
    for (const a of CAF.boot.catalog) { CAF.catalog[a.id] = a; CAF.order.push(a.id); }
    CAF.skills = CAF.boot.skills || {};
    CAF.playbooks = CAF.boot.playbooks || {};
    // hosted by the shop: the server's current playbooks (the built-in ones plus published custom flows) replace the build's
    const hosted = document.getElementById("caf-host-playbooks");
    CAF.hosted = !!hosted;   // served by the shop (caf serve): shop pages, the flow builder and the operator token exist
    if (hosted) { try { const books = JSON.parse(hosted.textContent || "{}"); if (books && typeof books === "object" && Object.keys(books).length) CAF.playbooks = books; } catch (e) { /* keep the build's */ } }
    const packed = document.getElementById("caf-host-agents");   // the agent packs' agents, from the server that hosts this console
    if (packed) { try { const data = JSON.parse(packed.textContent || "{}"); CAF.packs = data.packs || [];
      for (const a of data.agents || []) if (!CAF.catalog[a.id]) { CAF.catalog[a.id] = a; CAF.order.push(a.id); CAF.remote[a.id] = true; } } catch (e) { /* the blueprint's agents still work */ } }
    CAF.planner = CAF.boot.planner || {};
    CAF.schemas = CAF.boot.schemas || {};
    CAF.routing = CAF.boot.routing || {}; // {agent: {template: {tier, work, follow}}} from config/model_routing.yaml
    CAF.prompts.init();
  },
});

/* ------------------------------------------------------------------------- data + demo site */
CAF.datasets = {
  _cache: new Map(),
  names() { return Object.keys(CAF.boot.datasets || {}); },
  has(name) { return !!(CAF.boot.datasets || {})[name]; },
  async raw(name) {
    const d = (CAF.boot.datasets || {})[name];
    if (!d) throw new Error(`Dataset '${name}' is not embedded in the console`);
    return d.enc === "gz64" ? await U.gunzipB64(d.data) : d.data;
  },
  async get(name) {
    if (CAF.datasets._cache.has(name)) return U.clone(CAF.datasets._cache.get(name));
    const d = (CAF.boot.datasets || {})[name];
    if (!d) throw new Error(`Dataset '${name}' is not embedded in the console`);
    const text = await CAF.datasets.raw(name);
    const value = d.type === "csv" ? CAF.lib.csv.parse(text) : d.type === "json" ? JSON.parse(text) : text;
    CAF.datasets._cache.set(name, value);
    return U.clone(value);
  },
};

CAF.site = {
  host: () => CAF.DEMO_HOST,
  pages() { return Object.keys(CAF.boot.site || {}); },
  path(urlOrPath) {
    if (!urlOrPath) return null;
    let p = String(urlOrPath).trim();
    try {
      if (/^https?:\/\//i.test(p)) {
        const u = new URL(p);
        if (u.hostname !== CAF.DEMO_HOST) return null;
        p = u.pathname || "/";
      }
    } catch { return null; }
    if (!p.startsWith("/")) p = "/" + p;
    return p;
  },
  isDemo(url) { return CAF.site.path(url) !== null; },
  get(urlOrPath) {
    const site = CAF.boot.site || {};
    const meta = site["/_fixture.json"] ? JSON.parse(site["/_fixture.json"]) : {};
    let p = CAF.site.path(urlOrPath);
    if (p === null) return null;
    const redirects = (meta.redirects || {});
    const hops = [];
    for (let i = 0; i < 5 && redirects[p]; i++) { hops.push([p, redirects[p][1]]); p = redirects[p][0]; }
    if ((meta.status || {})[p]) return { status: meta.status[p], html: "<html><body><h1>Not found</h1></body></html>", path: p, redirects: hops };
    const candidates = [p, p.endsWith("/") ? p + "index.html" : p + "/index.html", p + ".html"];
    for (const c of candidates) if (site[c] !== undefined) return { status: 200, html: site[c], path: c, redirects: hops };
    return { status: 404, html: "<html><body><h1>Not found</h1></body></html>", path: p, redirects: hops };
  },
  url(path) { return `https://${CAF.DEMO_HOST}${path.startsWith("/") ? path : "/" + path}`; },
};

/* ------------------------------------------------------------------------- prompts (Nunjucks, precompiled) */
CAF.prompts = {
  env: null,
  init() {
    if (!window.nunjucks || !window.nunjucksPrecompiled) { CAF.prompts.env = null; return; }
    const nj = window.nunjucks;
    const env = new nj.Environment([new nj.PrecompiledLoader(window.nunjucksPrecompiled)], { autoescape: false });
    const arr = (v) => (v === undefined || v === null ? [] : Array.isArray(v) ? v : [v]);
    env.addFilter("tojson", (v, indent) => JSON.stringify(v === undefined ? null : v, null, indent || undefined));
    env.addFilter("bullets", (items, prefix = "- ") => arr(items).map((i) => prefix + i).join("\n"));
    env.addFilter("numbered", (items) => arr(items).map((i, n) => `${n + 1}. ${i}`).join("\n"));
    // Jinja2's ChainableUndefined tolerates undefined in these filters; make Nunjucks match.
    env.addFilter("join", (v, d = "", attr) => arr(v).map((x) => (attr ? (x || {})[attr] : x)).filter((x) => x !== undefined && x !== null).join(d));
    env.addFilter("length", (v) => (v === undefined || v === null ? 0 : typeof v === "object" && !Array.isArray(v) ? Object.keys(v).length : v.length || 0));
    env.addFilter("first", (v) => arr(v)[0] ?? "");
    env.addFilter("last", (v) => { const a = arr(v); return a.length ? a[a.length - 1] : ""; });
    env.addFilter("upper", (v) => String(v ?? "").toUpperCase());
    env.addFilter("lower", (v) => String(v ?? "").toLowerCase());
    env.addFilter("trim", (v) => String(v ?? "").trim());
    CAF.prompts.env = env;
  },
  has(agentId, name) { return !!(window.nunjucksPrecompiled || {})[`${agentId}/${name}`]; },
  /** Jinja2 treats [] and {} as false; JavaScript treats them as true. Normalise empty containers to
      null (recursively) so `{% if items %}` behaves identically in both runtimes. */
  jinjaTruthy(v) {
    if (Array.isArray(v)) return v.length ? v.map(CAF.prompts.jinjaTruthy) : null;
    if (U.isObj(v)) { const keys = Object.keys(v); if (!keys.length) return null; const o = {}; for (const k of keys) o[k] = CAF.prompts.jinjaTruthy(v[k]); return o; }
    return v;
  },
  render(agentId, name, vars) {
    if (!CAF.prompts.env) throw new Error("Prompt runtime (Nunjucks) did not load");
    const base = { brand: CAF.skills.brand_voice || {}, today: U.today() };
    const ctxVars = { ...base, ...CAF.prompts.jinjaTruthy(vars || {}) };
    const text = CAF.prompts.env.render(`${agentId}/${name}`, ctxVars).trim();
    const marker = "---- USER ----";
    if (text.includes(marker)) {
      const [system, user] = text.split(marker);
      return { system: system.trim() || null, user: user.trim() };
    }
    return { system: null, user: text };
  },
};

/* ------------------------------------------------------------------------- JSON Schema (Pydantic) validation */
CAF.schema = {
  resolve(node, root) {
    let n = node, guard = 0;
    while (n && n.$ref && guard++ < 20) {
      const path = n.$ref.replace(/^#\//, "").split("/");
      let target = root;
      for (const p of path) target = target ? target[p] : undefined;
      n = { ...(target || {}), ...Object.fromEntries(Object.entries(n).filter(([k]) => k !== "$ref")) };
    }
    return n || {};
  },
  typeOf(v) { return v === null ? "null" : Array.isArray(v) ? "array" : Number.isInteger(v) ? "integer" : typeof v; },
  /** Validate + coerce + fill defaults. Returns {ok, value, errors}. */
  validate(schema, value, root = schema, path = "$") {
    const errors = [];
    const out = CAF.schema._v(schema, value, root, path, errors);
    return { ok: errors.length === 0, value: out, errors };
  },
  _matches(s, v, root) { const e = []; CAF.schema._v(s, U.clone(v), root, "$", e); return e.length === 0; },
  _v(schemaNode, value, root, path, errors) {
    const s = CAF.schema.resolve(schemaNode || {}, root);
    if (s.anyOf || s.oneOf) {
      const opts = s.anyOf || s.oneOf;
      if (value === null || value === undefined) {
        if (opts.some((o) => CAF.schema.resolve(o, root).type === "null")) return null;
      }
      for (const o of opts) if (CAF.schema._matches(o, value, root)) return CAF.schema._v(o, value, root, path, errors);
      const nonNull = opts.find((o) => CAF.schema.resolve(o, root).type !== "null");
      return CAF.schema._v(nonNull || opts[0], value, root, path, errors);
    }
    if (s.allOf) { let v = value; for (const o of s.allOf) v = CAF.schema._v(o, v, root, path, errors); return v; }
    if (value === undefined) value = s.default !== undefined ? U.clone(s.default) : undefined;
    if (s.const !== undefined && value !== s.const) { errors.push(`${path}: expected ${JSON.stringify(s.const)}`); return value; }
    if (s.enum) {
      if (!s.enum.includes(value)) {
        const ci = typeof value === "string" ? s.enum.find((e) => String(e).toLowerCase() === value.toLowerCase()) : undefined;
        if (ci !== undefined) return ci;
        errors.push(`${path}: '${value}' not in ${JSON.stringify(s.enum)}`);
      }
      return value;
    }
    let type = s.type;
    if (Array.isArray(type)) type = type.find((t) => t !== "null") || type[0];
    if (value === null && (s.type === "null" || (Array.isArray(s.type) && s.type.includes("null")))) return null;
    switch (type) {
      case "object": {
        if (!U.isObj(value)) { if (value === undefined) return value; errors.push(`${path}: expected object`); return value; }
        const props = s.properties || {};
        const out = {};
        for (const [k, sub] of Object.entries(props)) {
          const r = CAF.schema._v(sub, value[k], root, `${path}.${k}`, errors);
          if (r !== undefined) out[k] = r;
        }
        for (const [k, v] of Object.entries(value)) if (!(k in props)) out[k] = v;
        for (const req of s.required || []) if (out[req] === undefined) errors.push(`${path}.${req}: required`);
        return out;
      }
      case "array": {
        if (value === undefined) return value;
        if (!Array.isArray(value)) { if (value === null) { errors.push(`${path}: expected array`); return value; } value = [value]; }
        const items = value.map((v, i) => CAF.schema._v(s.items || {}, v, root, `${path}[${i}]`, errors));
        if (s.minItems !== undefined && items.length < s.minItems) errors.push(`${path}: needs at least ${s.minItems} items`);
        if (s.maxItems !== undefined && items.length > s.maxItems) return items.slice(0, s.maxItems);
        return items;
      }
      case "integer": case "number": {
        if (value === undefined) return value;
        let n = typeof value === "string" && value.trim() !== "" ? Number(value) : value;
        if (typeof n !== "number" || Number.isNaN(n)) { errors.push(`${path}: expected number`); return value; }
        if (type === "integer") n = Math.round(n);
        if (s.minimum !== undefined && n < s.minimum) errors.push(`${path}: below minimum ${s.minimum}`);
        if (s.maximum !== undefined && n > s.maximum) errors.push(`${path}: above maximum ${s.maximum}`);
        if (s.exclusiveMinimum !== undefined && n <= s.exclusiveMinimum) errors.push(`${path}: must be > ${s.exclusiveMinimum}`);
        if (s.exclusiveMaximum !== undefined && n >= s.exclusiveMaximum) errors.push(`${path}: must be < ${s.exclusiveMaximum}`);
        return n;
      }
      case "boolean": {
        if (value === undefined) return value;
        if (typeof value === "string") return ["true", "1", "yes", "on"].includes(value.toLowerCase());
        if (typeof value !== "boolean") { errors.push(`${path}: expected boolean`); }
        return !!value;
      }
      case "string": {
        if (value === undefined) return value;
        if (typeof value !== "string") value = value === null ? value : (typeof value === "object" ? JSON.stringify(value) : String(value));
        if (value === null) { errors.push(`${path}: expected string`); return value; }
        if (s.minLength !== undefined && value.length < s.minLength) errors.push(`${path}: shorter than ${s.minLength}`);
        if (s.maxLength !== undefined && value.length > s.maxLength) errors.push(`${path}: longer than ${s.maxLength}`);
        return value;
      }
      case "null": return null;
      default: return value;
    }
  },
  /** Compact schema text for prompts (titles removed). */
  forPrompt(schema) {
    // drop schema *annotations* named title (strings) but keep properties that are called "title" (objects)
    return JSON.stringify(schema, (k, v) => (k === "title" && typeof v === "string" ? undefined : v));
  },
};

/* ------------------------------------------------------------------------- offline model (schema-driven) */
CAF.offline = {
  MARK: "[offline]",
  facts(prompt) {
    const out = {};
    for (const line of String(prompt || "").split("\n")) {
      const m = line.match(/^\s*(?:[-*]\s*)?([A-Za-z][A-Za-z0-9 _/()-]{1,40}?)\s*[:=]\s*(.{2,300})$/);
      if (m) {
        const k = m[1].trim().toLowerCase().replace(/ /g, "_");
        const v = m[2].trim().replace(/^"|"$/g, "");
        if (v && !["none", "null", "n/a", "[]", "{}"].includes(v.toLowerCase()) && !(k in out)) out[k] = v;
      }
    }
    return out;
  },
  topic(f) {
    for (const k of ["topic", "campaign_type", "campaign", "title", "subject", "objective", "overview", "page_url", "url", "goal", "test_idea", "idea", "competitor", "industry", "question", "audience"])
      if (f[k]) return f[k].slice(0, 80);
    const vals = Object.values(f);
    return vals.length ? vals[0].slice(0, 80) : "";
  },
  humanize(name) { const t = String(name || "").replace(/_/g, " ").trim(); return t ? t[0].toUpperCase() + t.slice(1).toLowerCase() : t; },
  singular(name) { const n = String(name || "").trimEnd(); if (n.endsWith("ies")) return n.slice(0, -3) + "y"; if (n.endsWith("s") && !n.endsWith("ss")) return n.slice(0, -1); return n; },
  /** Exact port of caf.core.offline.OfflineModel.complete. */
  text(prompt) {
    const M = CAF.offline.MARK, f = CAF.offline.facts(prompt);
    const topic = CAF.offline.topic(f) || "the requested task";
    const lines = [`${M} Draft generated without a language model for: ${topic}.`, ""];
    const headings = [...String(prompt || "").matchAll(/^#{1,3}\s+(.+)$/gm)].map((m) => m[1].trim()).slice(0, 8);
    if (headings.length) for (const h of headings) lines.push(`## ${h}`, `${M} Placeholder for '${h}' about ${topic}.`, "");
    else for (const [k, v] of Object.entries(f).slice(0, 6)) lines.push(`- ${CAF.offline.humanize(k)}: ${v}`);
    lines.push("", `${M} Configure CAF_LLM_PROVIDER (anthropic | openai | azure_openai | bedrock | ollama) for real copy.`);
    return lines.join("\n").trim();
  },
  /** Exact port of caf.core.offline.OfflineModel.structured (depth counts nested objects, like Pydantic models). */
  generate(schema, prompt) {
    const f = CAF.offline.facts(prompt);
    return CAF.offline._object(CAF.schema.resolve(schema, schema), schema, f, 0);
  },
  _object(s, root, facts, depth) {
    const out = {};
    for (const [k, sub] of Object.entries(s.properties || {})) out[k] = CAF.offline._value(k, sub, root, facts, depth);
    return out;
  },
  _value(name, node, root, facts, depth) {
    const s = CAF.schema.resolve(node || {}, root);
    if (s.anyOf || s.oneOf) {
      const opts = (s.anyOf || s.oneOf).map((o) => CAF.schema.resolve(o, root));
      if (s.default !== undefined && s.default !== null && depth > 0) return U.clone(s.default);
      const pick = opts.find((o) => o.type !== "null");
      return pick ? CAF.offline._value(name, { ...pick, ...(s.description ? { description: s.description } : {}), ...(s.default !== undefined ? { default: s.default } : {}) }, root, facts, depth) : null;
    }
    if (s.allOf) return CAF.offline._value(name, s.allOf[0], root, facts, depth);
    if (s.const !== undefined) return s.const;
    if (s.enum) return s.default !== undefined && s.default !== null && s.enum.includes(s.default) ? s.default : s.enum[0];
    let type = Array.isArray(s.type) ? s.type.find((t) => t !== "null") : s.type;
    if (!type && s.properties) type = "object";
    if (type === "object") return depth < 6 && s.properties ? CAF.offline._object(s, root, facts, depth + 1) : {};
    if (type === "array") {
      const lo = s.minItems || 0, hi = s.maxItems ?? 99;
      const n = depth < 3 ? Math.max(lo, Math.min(3, hi)) : Math.max(lo, Math.min(1, hi));
      return Array.from({ length: n }, (_, i) => CAF.offline._item(name, s.items || { type: "string" }, root, facts, depth, i));
    }
    if (type === "boolean") return typeof s.default === "boolean" ? s.default : false;
    if (type === "integer" || type === "number") {
      if (s.default !== undefined && s.default !== null) return s.default;
      const gt = s.exclusiveMinimum !== undefined;
      const lo = s.minimum ?? (gt ? s.exclusiveMinimum : 0);
      const hi = s.maximum ?? (s.exclusiveMaximum ?? 100);
      let v = lo + (gt ? 1 : 0);
      v = Math.min(Math.max(v, lo <= 1 && 1 <= hi ? 1 : lo), hi);
      return type === "integer" ? Math.trunc(v) : v;
    }
    if (type === "null") return null;
    return CAF.offline._text(name, s, facts);
  },
  _item(name, itemNode, root, facts, depth, i) {
    const s = CAF.schema.resolve(itemNode, root);
    const M = CAF.offline.MARK;
    let type = Array.isArray(s.type) ? s.type.find((t) => t !== "null") : s.type;
    if (!type && s.properties) type = "object";
    if (type === "object" && s.properties) {
      const data = CAF.offline._object(s, root, facts, depth + 1);
      for (const [k, v] of Object.entries(data)) if (typeof v === "string" && v.startsWith(M)) data[k] = `${v} ${i + 1}`;
      return data;
    }
    if (type === "string" && !s.enum) {
      const text = `${M} ${CAF.offline.humanize(CAF.offline.singular(name))} ${i + 1}`;
      return s.maxLength ? text.slice(0, s.maxLength) : text;
    }
    return CAF.offline._value(name, s, root, facts, depth + 1);
  },
  _text(name, s, facts) {
    if (typeof s.default === "string" && s.default) return s.default;
    const M = CAF.offline.MARK, key = String(name).toLowerCase();
    let direct = facts[key];
    if (direct === undefined) {
      const toks = new Set(key.split("_"));
      for (const [k, v] of Object.entries(facts)) if (k.split("_").some((t) => toks.has(t) && !["the", "a", "of"].includes(t))) { direct = v; break; }
    }
    const topic = CAF.offline.topic(facts);
    let text = direct ? `${M} ${direct}` : `${M} ${CAF.offline.humanize(name)}${topic ? ` for ${topic}` : ""}`;
    const lo = s.minLength || 0, hi = s.maxLength ?? 10000;
    if (text.length < lo) text = (text + " " + "lorem ".repeat(lo)).slice(0, lo);
    return text.slice(0, hi);
  },
};

/* ------------------------------------------------------------------------- Claude bridge */
CAF.llm = {
  sample: null, host: null, state: "checking", mode: U.storage.get("caf.mode", "live"), quick: U.storage.get("caf.quick", false),
  calls: 0, active: 0, queue: [], max: 2,
  async init() {
    try {
      if (!window.claude || typeof window.claude.use !== "function") { CAF.llm.state = "unavailable"; CAF.emit({ type: "llm" }); return; }
      const s = await window.claude.use("sample");
      CAF.llm.sample = s || null;
      // served by `caf serve`: calls go to the framework's own model provider (e.g. Claude on Microsoft Foundry); the server
      // may answer that its provider is offline (CAF_LLM_PROVIDER=offline, no key), which is not a token problem
      CAF.llm.server = window.claude && window.claude.server ? window.claude.server : null;
      CAF.llm.state = s ? "ready" : (CAF.llm.server && !CAF.llm.server.live ? "server_offline" : "unavailable");
      CAF.llm.host = window.claude.host || null;
      if (CAF.llm.host && CAF.llm.host.max_concurrent) CAF.llm.max = Math.max(1, Math.min(8, CAF.llm.host.max_concurrent));
    } catch (e) { CAF.llm.state = e && e.code === "not_granted" ? "not_granted" : "unavailable"; }
    CAF.emit({ type: "llm" });
  },
  live() { return CAF.llm.mode === "live" && !!CAF.llm.sample && CAF.llm.state === "ready"; },
  async connect() {   // hosted console: forget a stale operator token and ask for it again
    if (window.claude && typeof window.claude.reconnect === "function") window.claude.reconnect();
    CAF.llm.state = "checking"; CAF.llm.sample = null; CAF.llm.host = null; CAF.emit({ type: "llm" });
    await CAF.llm.init();
    if (CAF.llm.state === "ready") CAF.llm.setMode("live");
  },
  setMode(m) { CAF.llm.mode = m; U.storage.set("caf.mode", m); CAF.emit({ type: "llm" }); },
  setQuick(q) { CAF.llm.quick = !!q; U.storage.set("caf.quick", !!q); CAF.emit({ type: "llm" }); },
  tier(t) {
    if (CAF.llm.quick) return "quick";
    return { none: "quick", fast: "quick", balanced: "default", pro: "default", complex: "complex" }[t || "balanced"] || "default";
  },
  async _slot(fn) {
    while (CAF.llm.active >= CAF.llm.max) await new Promise((r) => CAF.llm.queue.push(r));
    CAF.llm.active++;
    try { return await fn(); } finally { CAF.llm.active--; const next = CAF.llm.queue.shift(); if (next) next(); }
  },
  _err(e) {
    const code = e && e.code;
    if (code === "not_granted" || code === "sampling_disabled" || code === "not_declared" || code === "capability_disabled" || code === "capability_removed") {
      CAF.llm.state = "declined";
      CAF.emit({ type: "llm" });
      return new Error("Claude access is not allowed for this page, so the console switched to offline mode.");
    }
    if (code === "rate_limited") return new Error("Claude is rate-limited for this page right now. Wait a minute, then run again.");
    if (code === "refused") return new Error("Claude declined this request. Change the input and try again.");
    if (code === "prompt_too_large") return new Error("The prompt was too large for one call. Use a shorter source.");
    if (code === "cancelled") return new Error("Stopped.");
    return new Error(`Claude call failed (${code || "error"}): ${(e && e.message) || e}`);
  },
  prompt(system, user) { return system ? `${system}\n\n${user}` : user; },
  /** Options for one call. A framework server also gets the routing tier itself (fast/balanced/pro/complex),
      because the page's model tiers (quick/default/complex) cannot tell balanced work from pro work. */
  opts(tier, signal, extra) {
    const o = { modelTier: CAF.llm.tier(tier), signal, ...(extra || {}) };
    if (CAF.llm.host) o.tier = CAF.llm.quick ? "fast" : (tier || "balanced");
    return o;
  },
  /** Structured call: returns a value validated against `schema`. */
  async json(system, user, { schema, tier, signal, images, meter, offline = false, quick = false } = {}) {
    if (quick) tier = "fast";
    if (offline || !CAF.llm.live()) {
      if (meter) meter.offline++;
      const value = CAF.offline.generate(schema, user);
      return CAF.schema.validate(schema, value).value;
    }
    const instruction = `\n\nReply with only one JSON object that validates against this JSON Schema - no prose, no Markdown fences.\nJSON Schema:\n${CAF.schema.forPrompt(schema)}`;
    const body = CAF.llm.prompt(system, user);
    let lastErrors = null;
    for (let attempt = 0; attempt < 2; attempt++) {
      // the schema always ends the prompt; a correction note goes before it on the retry
      const promptText = attempt === 0 ? body + instruction
        : `${body}\n\nYour previous reply did not validate (${lastErrors.slice(0, 6).join("; ")}). Correct it.${instruction}`;
      let value;
      try {
        value = await CAF.llm._slot(() => CAF.llm.sample.json(promptText, CAF.llm.opts(tier, signal, images && images.length ? { images } : null)));
      } catch (e) {
        if (e && e.code === "invalid_json" && attempt === 0) { lastErrors = ["reply was not valid JSON"]; continue; }
        throw CAF.llm._err(e);
      } finally { CAF.llm.calls++; if (meter) meter.calls++; }
      const res = CAF.schema.validate(schema, value);
      if (res.ok) return res.value;
      lastErrors = res.errors;
      if (attempt === 1) throw new Error(`Claude's reply did not match the schema: ${res.errors.slice(0, 4).join("; ")}`);
    }
    throw new Error("Claude's reply did not match the schema");
  },
  async text(system, user, { tier, signal, meter, offline = false, quick = false } = {}) {
    if (quick) tier = "fast";
    if (offline || !CAF.llm.live()) { if (meter) meter.offline++; return CAF.offline.text(user); }
    try {
      const r = await CAF.llm._slot(() => CAF.llm.sample(CAF.llm.prompt(system, user), CAF.llm.opts(tier, signal)));
      return r.text;
    } catch (e) { throw CAF.llm._err(e); } finally { CAF.llm.calls++; if (meter) meter.calls++; }
  },
};

/** Tier for one model step: the routing policy's decision (same file as the Python router), else the agent's tier. */
CAF.routeTier = function routeTier(desc, template, requested) {
  const r = ((CAF.routing || {})[desc.id] || {})[template];
  if (!r) return requested || desc.tier;
  return r.follow && requested ? requested : r.tier;
};

/* ------------------------------------------------------------------------- downloads */
CAF.downloads = {
  ns: null, ready: false,
  async init() { try { CAF.downloads.ns = window.claude && window.claude.use ? await window.claude.use("downloads") : null; } catch { CAF.downloads.ns = null; } CAF.downloads.ready = true; CAF.emit({ type: "downloads" }); },
  available() { return !!CAF.downloads.ns; },
  /** Extensions the viewer's save dialog accepts; anything else (.mjml, .xml, .tmx, .sql, .js ...) is saved as plain text. */
  ALLOWED: new Set(["gif", "png", "jpg", "jpeg", "webp", "mp4", "webm", "txt", "json", "md", "docx", "pptx", "epub", "csv", "ttf", "html", "svg", "pdf", "xlsx", "zip"]),
  filename(name) {
    const base = String(name || "artifact").replace(/[\\/:*?"<>|]+/g, "-").slice(0, 200) || "artifact";
    const m = /\.([A-Za-z0-9]+)$/.exec(base);
    return m && CAF.downloads.ALLOWED.has(m[1].toLowerCase()) ? base : `${base}.txt`;
  },
  async save(filename, data) {
    if (!CAF.downloads.ns) throw new Error("Saving files is not available in this view");
    const name = CAF.downloads.filename(filename);
    const body = typeof data === "string" || data instanceof Blob ? data : JSON.stringify(data, null, 2);
    try { return await CAF.downloads.ns.save({ filename: name, data: body || " " }); }
    catch (e) {
      const code = e && e.code;
      if (code === "declined") return { status: "declined" };
      if (code === "rate_limited") throw new Error("Another save is waiting for your answer. Finish that one, then try again.");
      if (code === "too_large") throw new Error("This file is too large to save here.");
      if (["unavailable", "not_granted", "capability_disabled", "capability_removed", "extension_not_enabled"].includes(code)) throw new Error("Saving files is not available in this view.");
      throw new Error(`Could not save ${name}${code ? ` (${code})` : ""}.`);
    }
  },
};

/* ------------------------------------------------------------------------- browser-local connectors (file providers) */
CAF.connectors = {
  esp: { export(p, ctx) { const manifest = { name: p.name, subject: p.subject, preheader: p.preheader, audience: p.audience, files: ["email.html", "email.txt", ...(p.mjml ? ["email.mjml"] : [])], exported_at: U.now() };
    ctx.artifact({ name: `${p.name}-email.html`, mime: "text/html", content: p.html, description: "ESP export (HTML)" });
    ctx.artifact({ name: `${p.name}-manifest.json`, mime: "application/json", content: JSON.stringify(manifest, null, 2), description: "ESP export manifest" });
    return { id: p.name, status: "exported", provider: "file (browser)" }; } },
  cms: { publish(p, ctx) { const slug = p.slug || CAF.lib.text.slugify(p.title || "entry");
    ctx.artifact({ name: `${slug}.${p.locale || "en"}.json`, mime: "application/json", content: JSON.stringify({ ...p, id: `${p.content_type || "article"}:${slug}`, updated_at: U.now() }, null, 2), description: "CMS entry (file CMS)" });
    return { id: `${p.content_type || "article"}:${slug}:${p.locale || "en"}`, status: p.status || "draft", provider: "file (browser)" }; } },
  newswire: { distribute(p, ctx) { const slug = CAF.lib.text.slugify(p.headline || "release");
    ctx.artifact({ name: `${slug}.md`, mime: "text/markdown", content: `# ${p.headline}\n\n${p.body}`, description: "Release package" });
    return { id: slug, status: p.embargo ? "queued" : "ready", provider: "file (browser)" }; } },
  tasks: { create(p, ctx) { const created = (p.tasks || []).map((t) => ({ id: `TASK-${U.hash(JSON.stringify(t)).slice(0, 6).toUpperCase()}`, project: p.project || "CAF", ...t }));
    ctx.artifact({ name: "tasks.json", mime: "application/json", content: JSON.stringify(created, null, 2), description: "Emitted tasks" });
    return { created: created.length, provider: "json_file (browser)" }; } },
  flags: { deploy_variation(p, ctx) {
    const js = `/* Coforge CAF variation runtime - experiment: ${p.experiment_id} variation: ${p.variation_id} */\n(function(){var css=${JSON.stringify(p.css || "")};if(css){var s=document.createElement('style');s.setAttribute('data-caf-variation',${JSON.stringify(p.variation_id)});s.textContent=css;document.head.appendChild(s);}function apply(){try{${p.js || ""}}catch(e){if(window.console)console.warn('[caf] variation error',e);}}if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',apply);else apply();})();\n`;
    ctx.artifact({ name: `${CAF.lib.text.slugify(p.variation_id || "variation")}.js`, mime: "text/javascript", content: js, description: "Variation bundle (any testing tool)" });
    return { status: p.preview === false ? "deployed" : "preview", provider: "file (browser)" }; } },
};

/* ------------------------------------------------------------------------- agent context + execution */
CAF.makeCtx = function makeCtx(desc, input, opts) {
  const state = { checks: [], artifacts: [], warnings: [], actions: [], degraded: [], log: [] };
  const meter = { calls: 0, offline: 0 };
  const upstream = opts.upstream || {};
  const ctx = {
    agent: desc, input, upstream, signal: opts.signal, meter, state, runId: opts.runId,
    lib: CAF.lib, U,
    get brand() { return U.clone(CAF.skills.brand_voice || {}); },
    get llmLive() { return !opts.forceOffline && CAF.llm.live(); },
    skill(name, fallback) { const s = CAF.skills[name]; if (s === undefined) { if (fallback !== undefined) return U.clone(fallback); throw new Error(`Skill '${name}' is not embedded`); } return U.clone(s); },
    data: (name) => CAF.datasets.get(name),
    hasData: (name) => CAF.datasets.has(name),
    dataRaw: (name) => CAF.datasets.raw(name),
    site: (urlOrPath) => CAF.site.get(urlOrPath),
    siteUrl: (path) => CAF.site.url(path),
    isDemoUrl: (url) => CAF.site.isDemo(url),
    /** generate(template, vars, {tier, images}) - the schema is the exported prompt_schemas[template]. */
    async generate(template, vars = {}, o = {}) {
      const schema = (desc.prompt_schemas || {})[template];
      if (!schema) throw new Error(`No exported schema for ${desc.id}/${template}`);
      const { system, user } = CAF.prompts.render(desc.id, template, vars);
      return CAF.llm.json(system, user, { schema, tier: CAF.routeTier(desc, template, o.tier), signal: opts.signal, images: o.images, meter, offline: !!opts.forceOffline, quick: !!opts.quick });
    },
    async text(template, vars = {}, o = {}) {
      const { system, user } = CAF.prompts.render(desc.id, template, vars);
      return CAF.llm.text(system, user, { tier: CAF.routeTier(desc, template, o.tier), signal: opts.signal, meter, offline: !!opts.forceOffline, quick: !!opts.quick });
    },
    /** Run another agent as a sub-agent (same offline/quick/abort settings); its model calls roll up into this agent. */
    async runSubagent(agentId, subInput = {}) {
      const res = await CAF.runAgent(agentId, subInput, { upstream, signal: opts.signal, runId: opts.runId, forceOffline: opts.forceOffline, quick: opts.quick });
      meter.calls += (res.metrics || {}).llm_calls || 0; meter.offline += (res.metrics || {}).offline_calls || 0;
      return res;
    },
    check(c) { state.checks.push({ severity: "medium", ...c }); return c; },
    artifact(a) {
      const content = a.content;
      const size = typeof content === "string" ? new Blob([content]).size : content && content.size ? content.size : 0;
      const ref = { name: a.name, mime: a.mime || "text/plain", content, description: a.description || "", size };
      state.artifacts.push(ref);
      return ref;
    },
    action(a) { const act = { requires_approval: true, status: "pending", payload: {}, ...a }; state.actions.push(act); return act; },
    warn(msg) { state.warnings.push(msg); },
    degraded(reason) { if (!state.degraded.includes(reason)) state.degraded.push(reason); },
    log(msg) { state.log.push({ t: performance.now(), msg }); CAF.emit({ type: "log", agent: desc.id, msg, runId: opts.runId, step: opts.stepId }); },
    progress(msg) { ctx.log(msg); },
    upstreamOutput(...ids) {
      const entries = Object.entries(upstream).reverse();
      for (const [k, v] of entries) if (v && (ids.includes(v.agent_id) || ids.includes(k))) return v.output || v;
      return null;
    },
    upstreamFindings() {
      const out = [];
      for (const [k, v] of Object.entries(upstream)) for (const f of ((v && v.output && v.output.findings) || [])) out.push({ ...f, source_agent: f.source_agent || v.agent_id || k });
      return out;
    },
    upstreamText(max = 6000) {
      const parts = [];
      for (const [k, v] of Object.entries(upstream)) {
        const o = (v && v.output) || {};
        for (const f of ["markdown", "summary_markdown", "body_markdown", "executive_summary", "summary", "campaign_overview"]) if (typeof o[f] === "string" && o[f].trim()) { parts.push(`[${v.agent_id || k}] ${o[f].trim()}`); break; }
      }
      return parts.join("\n\n").slice(0, max);
    },
    /** Resolve a page from url or html input: demo-site URLs are served from the embedded site. */
    page({ url, html } = {}) {
      if (html && String(html).trim()) return { html: String(html), url: url || "about:blank", status: 200, source: "html input" };
      if (url && CAF.site.isDemo(url)) { const p = CAF.site.get(url); return { html: p.html, url: CAF.site.url(p.path), status: p.status, redirects: p.redirects, source: "demo site" }; }
      if (url) { ctx.degraded("the hosted console cannot fetch live websites - paste the page HTML into the html field, or run the Python service"); return null; }
      return null;
    },
  };
  return ctx;
};

CAF.runAgent = async function runAgent(id, input = {}, opts = {}) {
  const desc = CAF.catalog[id];
  const runId = opts.runId || U.uid("run");
  const t0 = performance.now();
  const base = { agent_id: id, run_id: runId, status: "error", summary: "", output: {}, checks: [], artifacts: [], warnings: [], pending_actions: [], provenance: {}, metrics: {}, error: null };
  if (!desc) return { ...base, error: `Unknown agent '${id}'` };
  const impl = CAF.impl[id];
  if (!impl && CAF.remote[id]) return CAF.runRemote(id, input, { ...opts, runId }, base, t0);
  if (!impl) return { ...base, summary: `${desc.name} has no browser runtime`, error: "This agent has no browser runtime in the console" };
  const vin = CAF.schema.validate(desc.input_schema, U.clone(input) || {});
  if (!vin.ok) return { ...base, summary: `${desc.name}: invalid input`, error: `Invalid input: ${vin.errors.join("; ")}` };
  const ctx = CAF.makeCtx(desc, vin.value, { ...opts, runId });
  CAF.emit({ type: "agent.start", agent: id, runId, step: opts.stepId });
  let ret;
  try {
    ret = (await impl.run(vin.value, ctx)) || {};
  } catch (e) {
    const res = { ...base, summary: `${desc.name} failed`, error: (e && e.message) || String(e), checks: ctx.state.checks, warnings: ctx.state.warnings,
      metrics: { duration_ms: Math.round(performance.now() - t0), llm_calls: ctx.meter.calls } };
    CAF.emit({ type: "agent.end", agent: id, runId, step: opts.stepId, result: res });
    if (opts.signal && opts.signal.aborted) res.error = "Stopped";
    console.error(`[${id}]`, e);
    return res;
  }
  const vout = CAF.schema.validate(desc.output_schema, ret.output || {});
  const reasons = [...ctx.state.degraded];
  if (ctx.meter.offline > 0 && desc.kind !== "deterministic") reasons.push("offline model: generated copy is placeholder text");
  const auto = !!opts.autoApprove;
  for (const a of ctx.state.actions) if (!a.requires_approval || (auto && !a.mandatory)) CAF.performAction(id, a, ctx);
  const pending = ctx.state.actions.some((a) => a.status === "pending");
  const res = {
    ...base,
    status: !vout.ok ? "error" : pending ? "needs_approval" : reasons.length ? "degraded" : "ok",
    error: vout.ok ? null : `Output broke the ${desc.name} contract: ${vout.errors.slice(0, 5).join("; ")}`,
    summary: ret.summary || "",
    output: vout.value,
    view: ret.view || null,
    checks: ctx.state.checks, artifacts: ctx.state.artifacts, warnings: ctx.state.warnings, pending_actions: ctx.state.actions,
    provenance: { runtime: "browser", llm: desc.kind === "deterministic" ? "none" : (ctx.meter.calls ? "claude" : ctx.meter.offline ? "offline" : "not used"), ...(reasons.length ? { degraded_reasons: reasons } : {}) },
    metrics: { duration_ms: Math.round(performance.now() - t0), llm_calls: ctx.meter.calls, offline_calls: ctx.meter.offline },
    _ctx: ctx,
  };
  CAF.emit({ type: "agent.end", agent: id, runId, step: opts.stepId, result: res });
  return res;
};

/* A pack agent has no browser twin: the server that hosts the console runs it (on its model, or as the pack's twin when the
   console is in Offline mode) and the result comes back in the same shape as a browser run. */
CAF.localIds = () => CAF.order.filter((id) => !CAF.remote[id]);
CAF.runRemote = async function runRemote(id, input, opts, base, t0) {
  const desc = CAF.catalog[id];
  CAF.emit({ type: "agent.start", agent: id, runId: base.run_id, step: opts.stepId });
  let res;
  try {
    if (!window.claude || typeof window.claude.runAgent !== "function") throw new Error("This agent runs on the server: open the console from the orchestration layer");
    const offline = !!opts.forceOffline || CAF.llm.mode === "offline";
    const r = await window.claude.runAgent(id, input || {}, { upstream: opts.upstream || null, signal: opts.signal, offline, tier: (opts.quick || CAF.llm.quick) ? "fast" : null });
    const out = r.output || {};
    res = { ...base, ...r, run_id: base.run_id, server_run_id: r.run_id, view: null,
      artifacts: (out.artifacts || []).map((a, i) => ({ name: `${a.type || "artifact"}${i ? "-" + (i + 1) : ""}.json`, mime: "application/json", content: JSON.stringify(a.content === undefined ? a : a.content, null, 2), description: a.name || "" })),
      provenance: { ...(r.provenance || {}), runtime: "server" }, metrics: { ...(r.metrics || {}), duration_ms: Math.round(performance.now() - t0) } };
  } catch (e) {
    res = { ...base, summary: `${desc.name} failed`, error: (opts.signal && opts.signal.aborted) ? "Stopped" : ((e && e.message) || String(e)), metrics: { duration_ms: Math.round(performance.now() - t0), llm_calls: 0 } };
  }
  CAF.emit({ type: "agent.end", agent: id, runId: base.run_id, step: opts.stepId, result: res });
  return res;
};

CAF.performAction = function performAction(agentId, action, ctx) {
  try {
    const conn = CAF.connectors[action.connector];
    const fn = conn && conn[action.method];
    if (!fn) throw new Error(`No browser connector for ${action.connector}.${action.method}`);
    action.result = fn(action.payload || {}, ctx);
    action.status = "done";
  } catch (e) { action.status = "failed"; action.result = { error: (e && e.message) || String(e) }; }
  return action;
};

CAF.selftest = function selftest(id) {
  const impl = CAF.impl[id];
  if (impl && typeof impl.selftest === "function") return impl.selftest();
  return U.clone((CAF.catalog[id] || {}).selftest_input || {});
};
