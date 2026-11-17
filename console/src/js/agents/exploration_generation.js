/* Exploration Generation - browser twin of caf/agents/experimentation/exploration_generation.py.
   Question -> SEMANTIC query (model, validated against semantic_layer.yaml; deterministic synonym parser offline / on invalid
   output) -> deterministic SQL compiler (many-to-one join paths only, per-entity CTEs) -> SQL validator -> a small in-browser
   SQL engine executes that same SQL text over the embedded warehouse tables (joins, filters, group-by, aggregates, CASE,
   NULLIF/COALESCE, date_trunc, CTEs, FULL OUTER JOIN, scalar sub-selects, order, limit). Free SQL (model or user) is validated
   the same way and executed when it stays inside the engine's single-SELECT subset; otherwise it degrades with a reason.
   Returns the same `dataset` + hints so Chart Summary can chain. */
(() => {
  const XP = (CAF.xp = CAF.xp || {});
  const T = () => CAF.lib.text;
  const MAX_ROWS = 500;
  const GRAINS = ["day", "week", "month", "quarter", "year"];
  const reEsc = (s) => String(s).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

  /* ============================================================ semantic layer (pure) */
  function loadLayer(cfg) {
    const entities = Object.fromEntries((cfg.entities || []).map((e) => [e.name, e]));
    const dims = {}, metrics = {}, timeDims = {};
    for (const e of Object.values(entities)) for (const d of e.dimensions || []) (dims[d.name] = dims[d.name] || []).push({ ...d, entity: e.name, table: e.table || e.name, expr: `${e.table || e.name}.${d.column || d.name}` });
    for (const e of Object.values(entities)) for (const m of e.measures || []) if (!(m.name in metrics)) metrics[m.name] = { ...m, entity: e.name, kind: "measure" };
    for (const m of cfg.metrics || []) metrics[m.name] = { ...m, kind: "metric" };
    for (const e of Object.values(entities)) { const td = (e.dimensions || []).find((d) => ["date", "timestamp"].includes(d.type)); if (td) timeDims[e.name] = td.name; }
    const synonyms = Object.fromEntries(Object.entries(cfg.synonyms || {}).map(([k, v]) => [String(k).toLowerCase(), String(v)]));
    return { entities, dimensions: dims, metrics, joins: cfg.joins || [], synonyms, time_dims: timeDims, dialect: cfg.dialect || "duckdb", examples: cfg.examples || [] };
  }
  const onColumns = (on) => [...String(on || "").matchAll(/([A-Za-z_]\w*)\.([A-Za-z_]\w*)/g)].map((m) => [m[1], m[2]]);
  function joinEdges(layer) {
    const ents = layer.entities;
    const t2e = Object.fromEntries(Object.entries(ents).map(([n, e]) => [e.table || n, n]));
    const edges = {};
    for (const j of layer.joins) {
      const cols = onColumns(j.on);
      for (const [src, dst, declared] of [[j.left, j.right, true], [j.right, j.left, false]]) {
        const dstTable = (ents[dst] || {}).table || dst;
        const dstCols = cols.filter(([t]) => (t2e[t] || t) === dst || t === dstTable).map(([, c]) => c);
        const safe = dstCols.length > 0 && dstCols.includes((ents[dst] || {}).primary_key);
        (edges[src] = edges[src] || []).push({ from: src, to: dst, on: j.on || "", type: declared ? String(j.type || "inner").toLowerCase() : "inner", safe });
      }
    }
    return edges;
  }
  function joinPath(layer, src, dst) {
    if (src === dst) return [];
    const edges = layer._edges || (layer._edges = joinEdges(layer));
    const q = [[src, []]], seen = new Set([src]);
    while (q.length) {
      const [node, path] = q.shift();
      for (const e of edges[node] || []) {
        if (!e.safe || seen.has(e.to)) continue;
        if (e.to === dst) return [...path, e];
        seen.add(e.to); q.push([e.to, [...path, e]]);
      }
    }
    return null;
  }
  function resolveName(name, layer, kind) {
    const n = String(name || "").trim();
    const pool = kind === "metric" ? layer.metrics : layer.dimensions;
    if (n in pool) return n;
    const low = n.toLowerCase().replace(/-/g, " ");
    const snake = low.replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
    if (snake in pool) return snake;
    const syn = layer.synonyms[low] || layer.synonyms[n.toLowerCase()];
    return syn && syn in pool ? syn : null;
  }
  const isoDay = (v) => { if (!v) return null; const m = String(v).slice(0, 10).match(/^(\d{4})-(\d{2})-(\d{2})$/); if (!m) return null; const d = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3])); return d.getUTCMonth() === +m[2] - 1 ? d.toISOString().slice(0, 10) : null; };
  const normQuery = (q) => ({ metrics: [...(q.metrics || [])], dimensions: [...(q.dimensions || [])], filters: (q.filters || []).map((f) => ({ dimension: f.dimension, op: f.op || "=", values: (f.values || []).map(String) })),
    time_grain: q.time_grain || "none", time_dimension: q.time_dimension ?? null, date_from: q.date_from ?? null, date_to: q.date_to ?? null,
    order_by: (q.order_by || []).map((o) => ({ field: o.field, direction: o.direction || "desc" })), limit: Math.min(Math.max(1, Math.round(q.limit ?? 100)), MAX_ROWS) });
  /** Resolve synonyms and check names, types and join paths -> [normalised query, errors, warnings]. */
  function validateSemantic(q0, layer) {
    const q = normQuery(q0);
    const errors = [], warnings = [], metrics = [], dims = [], filters = [];
    for (const m of q.metrics) { const r = resolveName(m, layer, "metric"); if (r === null) errors.push(`unknown metric '${m}'`); else if (!metrics.includes(r)) metrics.push(r); }
    for (const d of q.dimensions) { const r = resolveName(d, layer, "dimension"); if (r === null) errors.push(`unknown dimension '${d}'`); else if (!dims.includes(r)) dims.push(r); }
    for (const f of q.filters) {
      const r = resolveName(f.dimension, layer, "dimension");
      if (r === null) { errors.push(`unknown filter dimension '${f.dimension}'`); continue; }
      const dtype = layer.dimensions[r][0].type || "string";
      if (f.op === "between" && f.values.length !== 2) errors.push(`filter on ${r}: 'between' needs two values`);
      else if (!["in", "not in", "between"].includes(f.op) && f.values.length !== 1) errors.push(`filter on ${r}: '${f.op}' needs exactly one value`);
      for (const v of f.values) {
        if (dtype === "number" && !(String(v).trim() !== "" && Number.isFinite(Number(v)))) errors.push(`filter on ${r}: '${v}' is not a number`);
        if (["date", "timestamp"].includes(dtype) && isoDay(v) === null) errors.push(`filter on ${r}: '${v}' is not a date (YYYY-MM-DD)`);
      }
      if (f.op === "contains" && dtype !== "string") errors.push(`filter on ${r}: 'contains' only applies to text dimensions`);
      filters.push({ dimension: r, op: f.op, values: f.values });
    }
    let td = null;
    if (q.time_dimension) {
      td = resolveName(q.time_dimension, layer, "dimension");
      if (td === null || !["date", "timestamp"].includes(layer.dimensions[td][0].type)) { errors.push(`time_dimension '${q.time_dimension}' is not a date dimension`); td = null; }
    }
    for (const v of [q.date_from, q.date_to]) if (v && isoDay(v) === null) errors.push(`date '${v}' is not YYYY-MM-DD`);
    if (q.time_grain === "none" && td && !(q.date_from || q.date_to)) warnings.push("time_dimension set without a grain or date range; it is ignored");
    const ents = [...new Set(metrics.map((m) => layer.metrics[m].entity))].sort();
    for (const ent of ents) {
      if ((q.time_grain !== "none" || q.date_from || q.date_to) && !(td && layer.dimensions[td][0].entity === ent) && !(ent in layer.time_dims)) errors.push(`metrics on '${ent}' have no date dimension for the time grain or range`);
      for (const d of [...dims, ...filters.map((f) => f.dimension), ...(td ? [td] : [])]) {
        const owners = layer.dimensions[d].map((x) => x.entity);
        if (owners.includes(ent)) continue;
        if (!owners.some((o) => joinPath(layer, ent, o) !== null)) errors.push(`dimension '${d}' cannot be reached from metric entity '${ent}' without duplicating rows (one-to-many join)`);
      }
    }
    if (ents.length > 1 && td) warnings.push("time_dimension applies to its own entity only; other metrics use their entity's default date");
    const names = new Set([...metrics, ...dims, "period"]);
    const order = [];
    for (const o of q.order_by) {
      const f = names.has(o.field) ? o.field : resolveName(o.field, layer, "metric") || resolveName(o.field, layer, "dimension");
      if (f && names.has(f)) order.push({ field: f, direction: o.direction }); else warnings.push(`order_by '${o.field}' is not in the result and was dropped`);
    }
    return [{ metrics: metrics.length ? metrics : q.metrics, dimensions: dims, filters, time_grain: q.time_grain, time_dimension: td, date_from: q.date_from, date_to: q.date_to, order_by: order, limit: Math.min(q.limit, MAX_ROWS) }, errors, warnings];
  }
  function literal(value, dtype) {
    if (dtype === "number") { const s = String(value); const x = Number(s); return s.includes(".") || s.toLowerCase().includes("e") ? (Number.isInteger(x) ? x.toFixed(1) : String(x)) : String(Math.trunc(x)); }
    if (["date", "timestamp"].includes(dtype)) return `DATE '${isoDay(value)}'`;
    return "'" + String(value).replace(/'/g, "''") + "'";
  }
  function filterSql(f, expr, dtype) {
    const e = dtype === "timestamp" ? `CAST(${expr} AS DATE)` : expr;
    if (["in", "not in"].includes(f.op)) return `${e} ${f.op.toUpperCase()} (${f.values.map((v) => literal(v, dtype)).join(", ")})`;
    if (f.op === "between") return `${e} BETWEEN ${literal(f.values[0], dtype)} AND ${literal(f.values[1], dtype)}`;
    if (f.op === "contains") { const pat = f.values[0].replace(/\\/g, "\\\\").replace(/%/g, "\\%").replace(/_/g, "\\_").replace(/'/g, "''"); return `${e} ILIKE '%${pat}%' ESCAPE '\\'`; }
    return `${e} ${f.op} ${literal(f.values[0], dtype)}`;
  }
  function dimForEntity(layer, name, ent) {
    const owners = layer.dimensions[name];
    const own = owners.find((d) => d.entity === ent);
    if (own) return [own, []];
    let best = null;
    for (const d of owners) { const p = joinPath(layer, ent, d.entity); if (p !== null && (best === null || p.length < best[1].length)) best = [d, p]; }
    if (best === null) throw new Error(`dimension '${name}' is not reachable from '${ent}'`);
    return best;
  }
  /** Deterministic SQL for a validated semantic query (port of compile_semantic: identical text). */
  function compileSemantic(q, layer) {
    const byEnt = {};
    for (const m of q.metrics) (byEnt[layer.metrics[m].entity] = byEnt[layer.metrics[m].entity] || []).push(m);
    const blocks = [];
    for (const [ent, mets] of Object.entries(byEnt)) {
      const table = layer.entities[ent].table || ent;
      const joins = [];
      const need = (path) => { for (const e of path) if (!joins.some((j) => j.to === e.to)) joins.push(e); };
      const select = [], group = [];
      const tdName = q.time_dimension && layer.dimensions[q.time_dimension].some((d) => d.entity === ent) ? q.time_dimension : layer.time_dims[ent];
      if (q.time_grain !== "none" && tdName) {
        const [tdef, path] = dimForEntity(layer, tdName, ent); need(path);
        const expr = q.time_grain === "day" ? `CAST(${tdef.expr} AS DATE)` : `CAST(date_trunc('${q.time_grain}', ${tdef.expr}) AS DATE)`;
        select.push(`${expr} AS period`); group.push(expr);
      }
      for (const d of q.dimensions) { const [ddef, path] = dimForEntity(layer, d, ent); need(path); select.push(`${ddef.expr} AS ${d}`); group.push(ddef.expr); }
      select.push(...mets.map((m) => `${layer.metrics[m].sql} AS ${m}`));
      const where = [];
      for (const f of q.filters) { const [fdef, path] = dimForEntity(layer, f.dimension, ent); need(path); where.push(filterSql(f, fdef.expr, fdef.type || "string")); }
      if ((q.date_from || q.date_to) && tdName) {
        const [tdef, path] = dimForEntity(layer, tdName, ent); need(path);
        const col = tdef.type === "timestamp" ? `CAST(${tdef.expr} AS DATE)` : tdef.expr;
        if (q.date_from) where.push(`${col} >= DATE '${isoDay(q.date_from)}'`);
        if (q.date_to) where.push(`${col} <= DATE '${isoDay(q.date_to)}'`);
      }
      let sql = "SELECT " + select.join(",\n       ") + `\nFROM ${table}`;
      for (const j of joins) sql += `\n${j.type === "left" ? "LEFT JOIN" : "INNER JOIN"} ${layer.entities[j.to].table || j.to} ON ${j.on}`;
      if (where.length) sql += "\nWHERE " + where.join("\n  AND ");
      if (group.length) sql += "\nGROUP BY " + group.join(", ");
      blocks.push([ent, mets, sql]);
    }
    const keys = [...(q.time_grain !== "none" ? ["period"] : []), ...q.dimensions];
    const order = q.order_by.length ? q.order_by : q.time_grain !== "none" ? [{ field: "period", direction: "asc" }] : [{ field: q.metrics[0], direction: "desc" }];
    const orderSql = "ORDER BY " + order.map((o) => `${o.field} ${o.direction.toUpperCase()}`).join(", ");
    if (blocks.length === 1) return `${blocks[0][2]}\n${orderSql}\nLIMIT ${q.limit}`;
    if (!keys.length) {
      const ctes = blocks.map(([ent, , sql]) => `s_${ent} AS (\n${sql}\n)`).join(",\n");
      return `WITH ${ctes}\nSELECT ${blocks.flatMap(([ent, mets]) => mets.map((m) => `(SELECT s_${ent}.${m} FROM s_${ent}) AS ${m}`)).join(", ")}`;
    }
    const ctes = blocks.map(([ent, , sql]) => `m_${ent} AS (\n${sql}\n)`).join(",\n");
    const names = blocks.map(([ent]) => `m_${ent}`);
    const selKeys = keys.map((k) => `COALESCE(${names.map((n) => `${n}.${k}`).join(", ")}) AS ${k}`);
    const selMets = blocks.flatMap(([ent, mets]) => mets.map((m) => `m_${ent}.${m}`));
    let sql = `WITH ${ctes}\nSELECT ${[...selKeys, ...selMets].join(", ")}\nFROM ${names[0]}`;
    for (const n of names.slice(1)) sql += `\nFULL OUTER JOIN ${n} ON ` + keys.map((k) => `${names[0]}.${k} IS NOT DISTINCT FROM ${n}.${k}`).join(" AND ");
    return `${sql}\n${orderSql}\nLIMIT ${q.limit}`;
  }

  /* ============================================================ SQL: tokenizer, parser, validator, engine */
  class SqlError extends Error {}
  class Unsupported extends Error {}
  const RESERVED = new Set(("select from where group by order limit offset having as and or not in is null between like ilike escape case when then else end join inner left right full outer cross on using " +
    "with distinct asc desc nulls first last cast true false union intersect except all date timestamp interval exists natural window over filter qualify into values insert update delete " +
    "drop create alter copy pragma set use attach detach install export truncate merge call load").split(" "));
  function tokenize(src) {
    const s = String(src), toks = [];
    let i = 0;
    while (i < s.length) {
      const c = s[i];
      if (/\s/.test(c)) { i++; continue; }
      if (c === "-" && s[i + 1] === "-") { while (i < s.length && s[i] !== "\n") i++; continue; }
      if (c === "/" && s[i + 1] === "*") { const j = s.indexOf("*/", i + 2); i = j < 0 ? s.length : j + 2; continue; }
      if (c === "'") {
        let j = i + 1, v = "";
        for (;;) { if (j >= s.length) throw new SqlError("unterminated string literal"); if (s[j] === "'") { if (s[j + 1] === "'") { v += "'"; j += 2; continue; } break; } v += s[j++]; }
        toks.push({ t: "str", v, pos: i, end: j + 1 }); i = j + 1; continue;
      }
      if (c === '"') { const j = s.indexOf('"', i + 1); if (j < 0) throw new SqlError("unterminated quoted identifier"); toks.push({ t: "id", v: s.slice(i + 1, j), q: true, pos: i, end: j + 1 }); i = j + 1; continue; }
      if (/[0-9]/.test(c) || (c === "." && /[0-9]/.test(s[i + 1] || ""))) { const m = s.slice(i).match(/^(\d+\.?\d*|\.\d+)(e[+-]?\d+)?/i); toks.push({ t: "num", v: Number(m[0]), raw: m[0], pos: i, end: i + m[0].length }); i += m[0].length; continue; }
      if (/[A-Za-z_]/.test(c)) { const m = s.slice(i).match(/^[A-Za-z_][A-Za-z0-9_$]*/); toks.push({ t: "id", v: m[0], pos: i, end: i + m[0].length }); i += m[0].length; continue; }
      const two = s.slice(i, i + 2);
      if (["<=", ">=", "<>", "!=", "||", "::", "=="].includes(two)) { toks.push({ t: "op", v: two === "==" ? "=" : two, pos: i, end: i + 2 }); i += 2; continue; }
      if ("=<>+-*/%(),.;[]".includes(c)) { toks.push({ t: "op", v: c, pos: i, end: i + 1 }); i++; continue; }
      throw new SqlError(`unexpected character '${c}' at position ${i}`);
    }
    toks.push({ t: "eof", v: "", pos: s.length, end: s.length });
    return toks;
  }
  const AGG = new Set(["count", "sum", "avg", "mean", "min", "max", "median", "any_value", "first", "last", "stddev", "stddev_samp", "variance", "var_samp", "string_agg", "list", "array_agg", "bool_and", "bool_or", "count_if"]);
  /** Recursive-descent parser for one statement: [WITH ...] SELECT ... (no UNION / window functions). */
  function parse(src) {
    const toks = tokenize(src);
    let p = 0;
    const peek = (o = 0) => toks[Math.min(p + o, toks.length - 1)];
    const kw = (w, o = 0) => { const t = peek(o); return t.t === "id" && !t.q && t.v.toLowerCase() === w; };
    const op = (v, o = 0) => { const t = peek(o); return t.t === "op" && t.v === v; };
    const next = () => toks[p++];
    const fail = (msg) => { const t = peek(); throw new SqlError(`${msg} near '${src.slice(t.pos, t.pos + 25) || "end of input"}'`); };
    const expectKw = (w) => { if (!kw(w)) fail(`expected ${w.toUpperCase()}`); return next(); };
    const expectOp = (v) => { if (!op(v)) fail(`expected '${v}'`); return next(); };
    const ident = () => { const t = peek(); if (t.t !== "id" || (!t.q && RESERVED.has(t.v.toLowerCase()) && !["date", "first", "last", "filter", "set", "values"].includes(t.v.toLowerCase()))) fail("expected an identifier"); next(); return t.q ? t.v : t.v.toLowerCase(); };
    const isAliasTok = () => { const t = peek(); return t.t === "id" && (t.q || !RESERVED.has(t.v.toLowerCase())); };
    function parseType() {
      const t0 = peek();
      if (t0.t !== "id") fail("expected a type name");
      next();
      let name = t0.v;
      while (peek().t === "id" && !RESERVED.has(peek().v.toLowerCase()) && ["precision", "varying"].includes(peek().v.toLowerCase())) name += " " + ident();
      if (op("(")) { next(); while (!op(")")) { if (peek().t === "eof") fail("unclosed type"); next(); } next(); }
      if (op("[")) { next(); expectOp("]"); name += "[]"; }
      return name.toLowerCase();
    }
    function primary() {
      const t = peek();
      if (t.t === "num") { next(); return { k: "lit", v: t.v }; }
      if (t.t === "str") { next(); return { k: "lit", v: t.v }; }
      if (op("(")) {
        next();
        if (kw("select") || kw("with")) { const q = statementBody(); expectOp(")"); return { k: "subq", q }; }
        const e = expr(); expectOp(")"); return e;
      }
      if (t.t !== "id") fail("unexpected token");
      const low = t.q ? null : t.v.toLowerCase();
      if (low === "null") { next(); return { k: "lit", v: null }; }
      if (low === "true" || low === "false") { next(); return { k: "lit", v: low === "true" }; }
      if ((low === "date" || low === "timestamp") && peek(1).t === "str") { next(); const s = next().v; return { k: "lit", v: low === "date" ? s.slice(0, 10) : s }; }
      if (low === "interval") throw new Unsupported("INTERVAL arithmetic");
      if (low === "exists") throw new Unsupported("EXISTS sub-queries");
      if (low === "case") {
        next();
        const node = { k: "case", base: null, whens: [], else: null };
        if (!kw("when")) node.base = expr();
        while (kw("when")) { next(); const c = expr(); expectKw("then"); node.whens.push([c, expr()]); }
        if (kw("else")) { next(); node.else = expr(); }
        expectKw("end");
        return node;
      }
      if (low === "cast" || low === "try_cast") { next(); expectOp("("); const e = expr(); expectKw("as"); const type = parseType(); expectOp(")"); return { k: "cast", e, type }; }
      if (low === "extract" && op("(", 1)) { next(); next(); const part = ident(); expectKw("from"); const e = expr(); expectOp(")"); return { k: "fn", name: "date_part", args: [{ k: "lit", v: part }, e], distinct: false }; }
      next();
      const name = t.q ? t.v : t.v.toLowerCase();
      if (op("(")) {
        next();
        const node = { k: "fn", name: t.v.toLowerCase(), args: [], distinct: false, star: false, src: null };
        if (op("*")) { next(); node.star = true; }
        else if (!op(")")) {
          if (kw("distinct")) { next(); node.distinct = true; }
          else if (kw("all")) next();
          node.args.push(expr());
          while (op(",")) { next(); node.args.push(expr()); }
          if (kw("order")) throw new Unsupported("ORDER BY inside an aggregate");
        }
        expectOp(")");
        if (kw("filter")) { next(); expectOp("("); expectKw("where"); node.filter = expr(); expectOp(")"); }
        if (kw("over")) throw new Unsupported("window functions (OVER)");
        return node;
      }
      if (op(".")) {
        next();
        if (op("*")) { next(); return { k: "star", table: name }; }
        const col = ident();
        if (op(".")) { next(); return { k: "col", table: col, name: ident(), schema: name }; }
        return { k: "col", table: name, name: col };
      }
      return { k: "col", table: null, name };
    }
    function postfix() { let e = primary(); while (op("::")) { next(); e = { k: "cast", e, type: parseType() }; } return e; }
    function unary() { if (op("-")) { next(); return { k: "neg", e: unary() }; } if (op("+")) { next(); return unary(); } return postfix(); }
    function mul() { let l = unary(); while (op("*") || op("/") || op("%")) { const o = next().v; l = { k: "bin", op: o, l, r: unary() }; } return l; }
    function add() { let l = mul(); while (op("+") || op("-")) { const o = next().v; l = { k: "bin", op: o, l, r: mul() }; } return l; }
    function concat() { let l = add(); while (op("||")) { next(); l = { k: "bin", op: "||", l, r: add() }; } return l; }
    function cmp() {
      let l = concat();
      for (;;) {
        if (["=", "!=", "<>", "<", "<=", ">", ">="].some((o) => op(o))) { const o = next().v; l = { k: "bin", op: o === "<>" ? "!=" : o, l, r: concat() }; continue; }
        if (kw("is")) {
          next(); const neg = kw("not") ? (next(), true) : false;
          if (kw("null")) { next(); l = { k: "isnull", e: l, not: neg }; continue; }
          if (kw("distinct")) { next(); expectKw("from"); l = { k: "distinct", l, r: concat(), not: neg }; continue; }
          if (kw("true") || kw("false")) { const v = next().v.toLowerCase() === "true"; l = { k: "distinct", l, r: { k: "lit", v }, not: !neg }; continue; }
          fail("expected NULL or DISTINCT FROM after IS");
        }
        let neg = false;
        if (kw("not") && (kw("in", 1) || kw("between", 1) || kw("like", 1) || kw("ilike", 1))) { next(); neg = true; }
        if (kw("in")) {
          next(); expectOp("(");
          if (kw("select") || kw("with")) { const q = statementBody(); expectOp(")"); l = { k: "in", e: l, subq: q, list: null, not: neg }; continue; }
          const list = [expr()]; while (op(",")) { next(); list.push(expr()); } expectOp(")");
          l = { k: "in", e: l, list, not: neg }; continue;
        }
        if (kw("between")) { next(); const lo = concat(); expectKw("and"); l = { k: "between", e: l, lo, hi: concat(), not: neg }; continue; }
        if (kw("like") || kw("ilike")) { const ci = next().v.toLowerCase() === "ilike"; const pat = concat(); let esc = null; if (kw("escape")) { next(); esc = concat(); } l = { k: "like", e: l, pat, esc, ci, not: neg }; continue; }
        if (neg) fail("unexpected NOT");
        return l;
      }
    }
    function notE() { if (kw("not")) { next(); return { k: "not", e: notE() }; } return cmp(); }
    function andE() { let l = notE(); while (kw("and")) { next(); l = { k: "bin", op: "and", l, r: notE() }; } return l; }
    function expr() { let l = andE(); while (kw("or")) { next(); l = { k: "bin", op: "or", l, r: andE() }; } return l; }
    function tableRef() {
      if (op("(")) { next(); const q = statementBody(); expectOp(")"); if (kw("as")) next(); const alias = isAliasTok() ? ident() : null; if (!alias) fail("a derived table needs an alias"); return { sub: q, alias }; }
      const t = peek();
      const name = ident();
      const ref = { name, alias: null, qualified: null, fn: false, pos: t.pos };
      if (op(".")) { next(); ref.qualified = `${name}.${ident()}`; ref.name = ref.qualified.split(".").pop(); }
      if (op("(")) { ref.fn = true; let depth = 0; do { if (op("(")) depth++; if (op(")")) depth--; next(); } while (depth > 0 && peek().t !== "eof"); }
      if (kw("as")) next();
      if (isAliasTok()) ref.alias = ident();
      return ref;
    }
    function select() {
      expectKw("select");
      const s = { distinct: false, items: [], from: null, joins: [], where: null, groupBy: [], having: null, orderBy: [], limit: null, offset: null, limitTok: null, into: false };
      if (kw("distinct")) { next(); s.distinct = true; if (kw("on")) throw new Unsupported("DISTINCT ON"); } else if (kw("all")) next();
      do {
        if (s.items.length) next();
        const start = peek().pos;
        if (op("*")) { next(); s.items.push({ star: true, table: null }); continue; }
        const e = expr();
        if (e.k === "star") { s.items.push({ star: true, table: e.table }); continue; }
        let alias = null;
        if (kw("as")) { next(); alias = ident(); } else if (isAliasTok()) alias = ident();
        s.items.push({ e, alias, text: src.slice(start, toks[p - 1].end).replace(/\s+AS\s+\S+$/i, "").trim() });
      } while (op(","));
      if (kw("into")) { s.into = true; next(); ident(); }
      if (kw("from")) {
        next();
        s.from = tableRef();
        for (;;) {
          if (op(",")) { next(); s.joins.push({ kind: "cross", table: tableRef(), on: null, using: null, comma: true }); continue; }
          let kind = null;
          if (kw("join")) kind = "inner";
          else if (kw("inner") && kw("join", 1)) { next(); kind = "inner"; }
          else if ((kw("left") || kw("right") || kw("full")) && (kw("join", 1) || (kw("outer", 1) && kw("join", 2)))) { kind = next().v.toLowerCase(); if (kw("outer")) next(); }
          else if (kw("cross") && kw("join", 1)) { next(); kind = "cross"; }
          else if (kw("natural")) throw new Unsupported("NATURAL joins");
          if (!kind) break;
          expectKw("join");
          const j = { kind, table: tableRef(), on: null, using: null };
          if (kw("on")) { next(); j.on = expr(); }
          else if (kw("using")) { next(); expectOp("("); j.using = [ident()]; while (op(",")) { next(); j.using.push(ident()); } expectOp(")"); }
          s.joins.push(j);
        }
      }
      if (kw("where")) { next(); s.where = expr(); }
      if (kw("group")) { next(); expectKw("by"); if (kw("all")) throw new Unsupported("GROUP BY ALL"); s.groupBy.push(expr()); while (op(",")) { next(); s.groupBy.push(expr()); } }
      if (kw("having")) { next(); s.having = expr(); }
      if (kw("qualify") || kw("window")) throw new Unsupported(`${peek().v.toUpperCase()} clauses`);
      if (kw("order")) {
        next(); expectKw("by");
        do {
          if (s.orderBy.length) next();
          const e = expr();
          let dir = "asc", nulls = null;
          if (kw("asc") || kw("desc")) dir = next().v.toLowerCase();
          if (kw("nulls")) { next(); nulls = next().v.toLowerCase(); }
          s.orderBy.push({ e, dir, nulls });
        } while (op(","));
      }
      if (kw("limit")) { next(); const t = peek(); if (t.t !== "num") { if (kw("all")) { next(); } else fail("LIMIT needs a number"); } else { next(); s.limit = t.v; s.limitTok = t; } }
      if (kw("offset")) { next(); const t = next(); if (t.t !== "num") fail("OFFSET needs a number"); s.offset = t.v; }
      return s;
    }
    function statementBody() {
      const ctes = [];
      if (kw("with")) {
        next(); if (kw("recursive")) throw new Unsupported("recursive CTEs");
        do { if (ctes.length) next(); const name = ident(); if (op("(")) throw new Unsupported("CTE column lists"); expectKw("as"); expectOp("("); ctes.push({ name, q: statementBody() }); expectOp(")"); } while (op(","));
      }
      const sel = select();
      if (kw("union") || kw("intersect") || kw("except")) throw new Unsupported(`${peek().v.toUpperCase()} queries`);
      return { ctes, select: sel };
    }
    const first = peek();
    if (!(kw("select") || kw("with"))) return { kind: first.t === "id" ? first.v.toUpperCase() : "UNKNOWN" };
    const stmt = statementBody();
    while (op(";")) next();
    if (peek().t !== "eof") fail("unexpected trailing input");
    return { kind: "SELECT", stmt, toks };
  }
  const walk = (node, fn) => {
    if (!node || typeof node !== "object") return;
    if (Array.isArray(node)) { node.forEach((n) => walk(n, fn)); return; }
    if (node.k) fn(node);
    for (const [k, v] of Object.entries(node)) if (k !== "toks" && v && typeof v === "object" && k !== "res") walk(v, fn);
  };
  const statementCount = (sql) => { let n = 0, cur = false; try { for (const t of tokenize(sql)) { if (t.t === "eof") break; if (t.t === "op" && t.v === ";") { if (cur) n++; cur = false; } else cur = true; } } catch { return 1; } return n + (cur ? 1 : 0); };
  const BLOCKED = new Set(["read_csv", "read_csv_auto", "read_parquet", "read_json", "read_json_auto", "read_text", "read_blob", "glob", "parquet_scan", "sniff_csv", "query_table", "query", "getenv",
    "current_setting", "pg_read_file", "load_extension", "system", "sqlite_scan", "postgres_scan", "mysql_scan", "iceberg_scan", "delta_scan", "http_get"]);
  /** Port of validate_sql: one read-only SELECT, known tables/columns, no table functions, no unconstrained cross joins, LIMIT enforced. */
  function validateSql(sql, tables, maxRows = MAX_ROWS) {
    const res = { valid: false, errors: [], warnings: [], tables: [], columns: [], limit_applied: false, sql, engine: "browser" };
    const n = statementCount(sql);
    let parsed;
    try { parsed = parse(sql); } catch (e) {
      if (e instanceof Unsupported) { res.errors.push(`not supported by the browser SQL engine: ${e.message}`); res.unsupported = e.message; return res; }
      if (n !== 1) { res.errors.push(`exactly one statement is allowed (got ${n})`); return res; }
      res.errors.push(`parse error: ${T().truncate(e.message, 200)}`); return res;
    }
    if (n !== 1) { res.errors.push(`exactly one statement is allowed (got ${n})`); return res; }
    if (parsed.kind !== "SELECT") { res.errors.push(`only SELECT queries are allowed (got ${parsed.kind})`); return res; }
    const known = Object.fromEntries(Object.entries(tables).map(([t, cols]) => [t.toLowerCase(), new Set(cols.map(([c]) => c.toLowerCase()))]));
    const ctes = new Set(), aliasMap = {}, selectAliases = new Set(), cols = [];
    const visit = (body) => {
      for (const c of body.ctes) { ctes.add(c.name.toLowerCase()); visit(c.q); }
      const s = body.select;
      if (s.into) res.errors.push("write/DDL constructs are not allowed: Into");
      for (const it of s.items) if (it.alias) selectAliases.add(it.alias.toLowerCase());
      const refs = [s.from, ...s.joins.map((j) => j.table)].filter(Boolean);
      for (const r of refs) {
        if (r.sub) { visit(r.sub); ctes.add(r.alias.toLowerCase()); aliasMap[r.alias.toLowerCase()] = r.alias.toLowerCase(); continue; }
        if (r.fn) { res.errors.push(`table functions are not allowed (${r.name})`); if (BLOCKED.has(r.name.toLowerCase())) res.errors.push(`function '${r.name.toLowerCase()}' is not allowed`); continue; }
        const name = r.name.toLowerCase();
        if (ctes.has(name)) { aliasMap[(r.alias || name).toLowerCase()] = name; continue; }
        if (r.qualified) res.errors.push(`qualified table '${r.qualified}' is not allowed`);
        if (!(name in known)) res.errors.push(`unknown table '${r.name}'`); else res.tables.push(name);
        aliasMap[(r.alias || name).toLowerCase()] = name;
      }
      for (const j of s.joins) {
        const hasCols = (() => { let f = false; walk(j.on, (x) => { if (x.k === "col") f = true; }); return f; })();
        if (j.kind === "cross" || (!j.on && !j.using) || (j.on && !hasCols)) res.errors.push(`unconstrained cross join with ${T().truncate(j.table.sub ? `(subquery) ${j.table.alias}` : j.table.qualified || j.table.name + (j.table.alias ? ` AS ${j.table.alias}` : ""), 60)} - use JOIN ... ON a key`);
      }
      walk([s.items.map((i) => i.e), s.where, s.groupBy, s.having, s.orderBy.map((o) => o.e), s.joins.map((j) => j.on)], (x) => {
        if (x.k === "fn" && BLOCKED.has(x.name)) res.errors.push(`function '${x.name}' is not allowed`);
        if (x.k === "col") cols.push(x);
        if (x.k === "subq") visit(x.q);
        if (x.k === "in" && x.subq) visit(x.subq);
      });
    };
    visit(parsed.stmt);
    const used = Object.values(aliasMap);
    for (const c of cols) {
      const col = c.name.toLowerCase();
      if (c.table) {
        const tbl = aliasMap[c.table.toLowerCase()] || c.table.toLowerCase();
        if (ctes.has(tbl)) continue;
        if (!(tbl in known)) res.errors.push(`unknown table or alias '${c.table}' in ${c.table}.${c.name}`);
        else if (!known[tbl].has(col)) res.errors.push(`unknown column '${c.name}' in table '${tbl}'`);
        else res.columns.push(`${tbl}.${col}`);
      } else {
        const owners = used.filter((t) => known[t] && known[t].has(col));
        if (owners.length) res.columns.push(`${owners[0]}.${col}`);
        else if (!selectAliases.has(col) && !used.some((t) => ctes.has(t))) res.errors.push(`unknown column '${c.name}'`);
      }
    }
    res.errors = [...new Set(res.errors)];
    if (res.errors.length) return res;
    const top = parsed.stmt.select;
    const current = top.limit;
    if (current === null || current > maxRows) {
      res.limit_applied = true;
      res.warnings.push(`LIMIT ${maxRows} enforced` + (current ? ` (was ${current})` : ""));
      res.sql = top.limitTok ? sql.slice(0, top.limitTok.pos) + String(maxRows) + sql.slice(top.limitTok.end) : `${sql.replace(/[\s;]+$/, "")}\nLIMIT ${maxRows}`;
    }
    res.tables = [...new Set(res.tables)].sort();
    res.columns = [...new Set(res.columns)].sort();
    res.valid = true;
    return res;
  }

  /* ---- engine: values are numbers, strings (dates as ISO strings), booleans and null */
  const isNum = (v) => typeof v === "number";
  const toNum = (v) => (v === null || v === undefined ? null : typeof v === "boolean" ? +v : typeof v === "number" ? v : String(v).trim() !== "" && Number.isFinite(Number(v)) ? Number(v) : null);
  function compare(a, b) {
    if (a === null || a === undefined || b === null || b === undefined) return null;
    if (typeof a === "boolean") a = +a;
    if (typeof b === "boolean") b = +b;
    if (isNum(a) && isNum(b)) return a < b ? -1 : a > b ? 1 : 0;
    if (isNum(a) !== isNum(b)) { const x = toNum(a), y = toNum(b); if (x !== null && y !== null) return x < y ? -1 : x > y ? 1 : 0; }
    const x = String(a), y = String(b);
    return x < y ? -1 : x > y ? 1 : 0;
  }
  const truthy = (v) => v === true || (isNum(v) && v !== 0);
  const dateOf = (v) => { if (v === null || v === undefined) return null; const s = String(v); return /^\d{4}-\d{2}-\d{2}/.test(s) ? s.slice(0, 10) : null; };
  function dateTrunc(part, v) {
    const d = dateOf(v);
    if (d === null) return null;
    const [y, m, day] = d.split("-").map(Number);
    const p = String(part).toLowerCase();
    if (p === "day") return d;
    if (p === "month") return `${d.slice(0, 7)}-01`;
    if (p === "year") return `${y}-01-01`;
    if (p === "quarter") return `${y}-${String(Math.floor((m - 1) / 3) * 3 + 1).padStart(2, "0")}-01`;
    if (p === "week") { const t = new Date(Date.UTC(y, m - 1, day)); t.setUTCDate(t.getUTCDate() - ((t.getUTCDay() + 6) % 7)); return t.toISOString().slice(0, 10); }
    throw new Unsupported(`date_trunc('${part}')`);
  }
  function datePart(part, v) {
    const d = dateOf(v); if (d === null) return null;
    const [y, m, day] = d.split("-").map(Number);
    const p = String(part).toLowerCase();
    if (p === "year") return y; if (p === "month") return m; if (p === "day") return day; if (p === "quarter") return Math.floor((m - 1) / 3) + 1;
    if (p === "dow" || p === "dayofweek") return new Date(Date.UTC(y, m - 1, day)).getUTCDay();
    if (p === "isodow") return ((new Date(Date.UTC(y, m - 1, day)).getUTCDay() + 6) % 7) + 1;
    throw new Unsupported(`date_part('${part}')`);
  }
  function castTo(v, type) {
    if (v === null || v === undefined) return null;
    const t = String(type).toLowerCase().split(/[\s(]/)[0];
    if (t === "date") { const d = dateOf(v); if (d === null) throw new SqlError(`could not convert '${v}' to DATE`); return d; }
    if (t === "timestamp" || t === "timestamptz" || t === "datetime") return String(v);
    if (["varchar", "text", "string", "char", "bpchar"].includes(t)) return typeof v === "boolean" ? String(v) : String(v);
    if (["double", "float", "real", "decimal", "numeric", "float8", "float4"].includes(t)) { const x = toNum(v); if (x === null) throw new SqlError(`could not convert '${v}' to ${t.toUpperCase()}`); return x; }
    if (["integer", "int", "bigint", "smallint", "tinyint", "hugeint", "int4", "int8", "ubigint", "uinteger"].includes(t)) { const x = toNum(v); if (x === null) throw new SqlError(`could not convert '${v}' to INTEGER`); return Math.round(x); }
    if (["boolean", "bool"].includes(t)) return truthy(toNum(v) ?? (String(v).toLowerCase() === "true" ? 1 : 0));
    throw new Unsupported(`CAST to ${type}`);
  }
  const likeRe = (pat, esc, ci) => {
    let re = "";
    const p = String(pat), e = esc === null || esc === undefined ? null : String(esc);
    for (let i = 0; i < p.length; i++) {
      const c = p[i];
      if (e && c === e && i + 1 < p.length) { re += reEsc(p[++i]); continue; }
      re += c === "%" ? "[\\s\\S]*" : c === "_" ? "[\\s\\S]" : reEsc(c);
    }
    return new RegExp(`^${re}$`, ci ? "i" : "");
  };
  /** True when an expression aggregates at this query level (sub-query bodies are their own level). */
  const containsAgg = (node) => {
    if (!node || typeof node !== "object") return false;
    if (Array.isArray(node)) return node.some(containsAgg);
    if (node.k === "fn" && AGG.has(node.name)) return true;
    if (node.k === "subq") return false;
    return Object.entries(node).some(([k, v]) => k !== "res" && k !== "subq" && v && typeof v === "object" && containsAgg(v));
  };
  function scalarFn(name, a) {
    switch (name) {
      case "nullif": return compare(a[0], a[1]) === 0 ? null : a[0];
      case "coalesce": case "ifnull": return a.find((v) => v !== null && v !== undefined) ?? null;
      case "abs": return a[0] === null ? null : Math.abs(toNum(a[0]));
      case "round": { if (a[0] === null) return null; const d = a.length > 1 ? toNum(a[1]) : 0; const f = 10 ** d, x = toNum(a[0]) * f; return (Math.sign(x) * Math.round(Math.abs(x))) / f; }
      case "floor": return a[0] === null ? null : Math.floor(toNum(a[0]));
      case "ceil": case "ceiling": return a[0] === null ? null : Math.ceil(toNum(a[0]));
      case "sqrt": return a[0] === null ? null : Math.sqrt(toNum(a[0]));
      case "ln": return a[0] === null ? null : Math.log(toNum(a[0]));
      case "power": case "pow": return a[0] === null || a[1] === null ? null : toNum(a[0]) ** toNum(a[1]);
      case "greatest": { const v = a.filter((x) => x !== null); return v.length ? v.reduce((x, y) => (compare(y, x) > 0 ? y : x)) : null; }
      case "least": { const v = a.filter((x) => x !== null); return v.length ? v.reduce((x, y) => (compare(y, x) < 0 ? y : x)) : null; }
      case "lower": case "lcase": return a[0] === null ? null : String(a[0]).toLowerCase();
      case "upper": case "ucase": return a[0] === null ? null : String(a[0]).toUpperCase();
      case "length": case "len": case "char_length": return a[0] === null ? null : String(a[0]).length;
      case "trim": return a[0] === null ? null : String(a[0]).trim();
      case "concat": return a.filter((x) => x !== null).map(String).join("");
      case "replace": return a.some((x) => x === null) ? null : String(a[0]).split(String(a[1])).join(String(a[2]));
      case "substr": case "substring": { if (a[0] === null) return null; const s = String(a[0]), st = Math.max(toNum(a[1]) - 1, 0); return a.length > 2 ? s.substr(st, toNum(a[2])) : s.slice(st); }
      case "date_trunc": case "datetrunc": return dateTrunc(a[0], a[1]);
      case "date_part": case "datepart": return datePart(a[0], a[1]);
      case "year": case "month": case "day": case "quarter": return datePart(name, a[0]);
      case "strftime": { const d = dateOf(a[0]); if (d === null) return null; return String(a[1]).replace(/%Y/g, d.slice(0, 4)).replace(/%m/g, d.slice(5, 7)).replace(/%d/g, d.slice(8, 10)); }
      default: throw new Unsupported(`function ${name}()`);
    }
  }
  /** Execute a parsed statement. `tables` maps lower-case table names to row arrays. */
  function execute(stmt, tables) {
    const env = new Map();
    const runBody = (body, outer) => {
      const local = new Map(outer);
      for (const c of body.ctes) local.set(c.name.toLowerCase(), runBody(c.q, local));
      return runSelect(body.select, local);
    };
    function source(ref, envMap) {
      if (ref.sub) { const r = runBody(ref.sub, envMap); return { alias: ref.alias.toLowerCase(), cols: r.columns, rows: r.records }; }
      const name = ref.name.toLowerCase();
      const alias = (ref.alias || ref.name).toLowerCase();
      if (envMap.has(name)) { const r = envMap.get(name); return { alias, cols: r.columns, rows: r.records }; }
      const rows = tables[name];
      if (!rows) throw new SqlError(`unknown table '${ref.name}'`);
      return { alias, cols: rows.length ? Object.keys(rows[0]) : [], rows };
    }
    function runSelect(s, envMap) {
      let scope = [], rows;
      if (!s.from) rows = [{}];
      else {
        const base = source(s.from, envMap);
        scope = [base];
        rows = base.rows.map((r) => ({ [base.alias]: r }));
        for (const j of s.joins) { const right = source(j.table, envMap); rows = joinRows(rows, scope, right, j, envMap); scope.push(right); }
      }
      const colIndex = new Map();
      for (const src of scope) for (const c of src.cols) { const k = c.toLowerCase(); colIndex.set(k, colIndex.has(k) ? null : src.alias); }
      const aliasSet = new Set(scope.map((x) => x.alias));
      const outNames = s.items.map((it, i) => (it.star ? null : it.alias || (it.e.k === "col" ? it.e.name : duckName(it.e) || it.text || `col${i}`)));
      const bindCol = (x) => {
        if (x.res !== undefined) return;
        if (x.table) { const a = x.table.toLowerCase(); if (!aliasSet.has(a)) throw new SqlError(`unknown table or alias '${x.table}'`); x.res = [a, x.name.toLowerCase()]; return; }
        const k = x.name.toLowerCase();
        if (!colIndex.has(k)) { x.res = null; return; }
        if (colIndex.get(k) === null) throw new SqlError(`ambiguous column '${x.name}'`);
        x.res = [colIndex.get(k), k];
      };
      const subCache = new Map();
      const scalarSub = (q) => { if (!subCache.has(q)) { const r = runBody(q, envMap); if (r.records.length > 1) throw new SqlError("a scalar sub-query returned more than one row"); subCache.set(q, r.records.length ? r.records[0][r.columns[0]] ?? null : null); } return subCache.get(q); };
      const inSub = (q) => { if (!subCache.has(q)) { const r = runBody(q, envMap); subCache.set(q, r.records.map((x) => x[r.columns[0]])); } return subCache.get(q); };
      const evalR = (n, row) => {
        switch (n.k) {
          case "lit": return n.v;
          case "col": { bindCol(n); if (n.res === null) throw new SqlError(`unknown column '${n.name}'`); const rec = row[n.res[0]]; if (!rec) return null; const v = rec[n.res[1]]; return v === undefined ? (rec[Object.keys(rec).find((k) => k.toLowerCase() === n.res[1])] ?? null) : v; }
          case "neg": { const v = toNum(evalR(n.e, row)); return v === null ? null : -v; }
          case "not": { const v = evalR(n.e, row); return v === null ? null : !truthy(v); }
          case "isnull": { const v = evalR(n.e, row); return n.not ? v !== null && v !== undefined : v === null || v === undefined; }
          case "distinct": { const a = evalR(n.l, row), b = evalR(n.r, row); const same = (a === null && b === null) || compare(a, b) === 0; return n.not ? same : !same; }
          case "between": { const v = evalR(n.e, row), lo = compare(v, evalR(n.lo, row)), hi = compare(v, evalR(n.hi, row)); if (lo === null || hi === null) return null; const r = lo >= 0 && hi <= 0; return n.not ? !r : r; }
          case "in": { const v = evalR(n.e, row); if (v === null) return null; const list = n.subq ? inSub(n.subq) : n.list.map((x) => evalR(x, row)); const hit = list.some((x) => compare(v, x) === 0); if (hit) return !n.not; return list.some((x) => x === null) ? null : !!n.not; }
          case "like": { const v = evalR(n.e, row), pat = evalR(n.pat, row); if (v === null || pat === null) return null; const r = likeRe(pat, n.esc ? evalR(n.esc, row) : null, n.ci).test(String(v)); return n.not ? !r : r; }
          case "case": {
            const base = n.base ? evalR(n.base, row) : undefined;
            for (const [c, v] of n.whens) { const hit = n.base ? compare(base, evalR(c, row)) === 0 : truthy(evalR(c, row)); if (hit) return evalR(v, row); }
            return n.else ? evalR(n.else, row) : null;
          }
          case "cast": return castTo(evalR(n.e, row), n.type);
          case "subq": return scalarSub(n.q);
          case "bin": return binop(n.op, () => evalR(n.l, row), () => evalR(n.r, row));
          case "fn": if (AGG.has(n.name)) throw new SqlError(`aggregate ${n.name}() is not allowed here`); return scalarFn(n.name, n.args.map((x) => evalR(x, row)));
          case "star": throw new SqlError("* is only allowed in the select list");
          default: throw new Unsupported(`expression ${n.k}`);
        }
      };
      const aggregate = (n, grpRows0) => {
        const grpRows = n.filter ? grpRows0.filter((r) => truthy(evalR(n.filter, r))) : grpRows0;
        if (n.star) return grpRows.length;
        let vals = grpRows.map((r) => evalR(n.args[0], r)).filter((v) => v !== null && v !== undefined);
        if (n.distinct) { const seen = new Set(); vals = vals.filter((v) => { const k = typeof v + ":" + v; if (seen.has(k)) return false; seen.add(k); return true; }); }
        switch (n.name) {
          case "count": return vals.length;
          case "count_if": return vals.filter(truthy).length;
          case "sum": return vals.length ? vals.reduce((s, v) => s + toNum(v), 0) : null;
          case "avg": case "mean": return vals.length ? vals.reduce((s, v) => s + toNum(v), 0) / vals.length : null;
          case "min": return vals.length ? vals.reduce((a, b) => (compare(b, a) < 0 ? b : a)) : null;
          case "max": return vals.length ? vals.reduce((a, b) => (compare(b, a) > 0 ? b : a)) : null;
          case "median": { if (!vals.length) return null; const xs = vals.map(toNum).sort((a, b) => a - b); const m = (xs.length - 1) / 2; return (xs[Math.floor(m)] + xs[Math.ceil(m)]) / 2; }
          case "any_value": case "first": return vals.length ? vals[0] : null;
          case "last": return vals.length ? vals[vals.length - 1] : null;
          case "stddev": case "stddev_samp": case "variance": case "var_samp": { if (vals.length < 2) return null; const xs = vals.map(toNum), m = xs.reduce((s, v) => s + v, 0) / xs.length; const v = xs.reduce((s, x) => s + (x - m) ** 2, 0) / (xs.length - 1); return n.name.startsWith("var") ? v : Math.sqrt(v); }
          case "bool_and": return vals.length ? vals.every(truthy) : null;
          case "bool_or": return vals.length ? vals.some(truthy) : null;
          case "string_agg": return vals.length ? vals.map(String).join(n.args[1] ? String(evalR(n.args[1], grpRows[0])) : ",") : null;
          default: throw new Unsupported(`aggregate ${n.name}()`);
        }
      };
      const evalG = (n, grp) => {
        if (n.k === "fn" && AGG.has(n.name)) return aggregate(n, grp.rows);
        switch (n.k) {
          case "lit": return n.v;
          case "col": case "subq": return grp.first ? evalR(n, grp.first) : n.k === "subq" ? scalarSub(n.q) : null;
          case "neg": { const v = toNum(evalG(n.e, grp)); return v === null ? null : -v; }
          case "not": { const v = evalG(n.e, grp); return v === null ? null : !truthy(v); }
          case "isnull": { const v = evalG(n.e, grp); return n.not ? v !== null : v === null; }
          case "distinct": { const a = evalG(n.l, grp), b = evalG(n.r, grp); const same = (a === null && b === null) || compare(a, b) === 0; return n.not ? same : !same; }
          case "between": { const v = evalG(n.e, grp), lo = compare(v, evalG(n.lo, grp)), hi = compare(v, evalG(n.hi, grp)); if (lo === null || hi === null) return null; const r = lo >= 0 && hi <= 0; return n.not ? !r : r; }
          case "in": { const v = evalG(n.e, grp); if (v === null) return null; const list = n.subq ? inSub(n.subq) : n.list.map((x) => evalG(x, grp)); const hit = list.some((x) => compare(v, x) === 0); return hit ? !n.not : !!n.not; }
          case "like": { const v = evalG(n.e, grp), pat = evalG(n.pat, grp); if (v === null || pat === null) return null; const r = likeRe(pat, n.esc ? evalG(n.esc, grp) : null, n.ci).test(String(v)); return n.not ? !r : r; }
          case "case": {
            const base = n.base ? evalG(n.base, grp) : undefined;
            for (const [c, v] of n.whens) { const hit = n.base ? compare(base, evalG(c, grp)) === 0 : truthy(evalG(c, grp)); if (hit) return evalG(v, grp); }
            return n.else ? evalG(n.else, grp) : null;
          }
          case "cast": return castTo(evalG(n.e, grp), n.type);
          case "bin": return binop(n.op, () => evalG(n.l, grp), () => evalG(n.r, grp));
          case "fn": return scalarFn(n.name, n.args.map((x) => evalG(x, grp)));
          default: throw new Unsupported(`expression ${n.k}`);
        }
      };
      if (s.where) rows = rows.filter((r) => truthy(evalR(s.where, r)));
      const byAlias = (e) => (e.k === "col" && !e.table ? outNames.findIndex((nm) => nm && nm.toLowerCase() === e.name.toLowerCase()) : -1);
      const groupExprs = s.groupBy.map((g) => {
        if (g.k === "lit" && isNum(g.v)) { const it = s.items[g.v - 1]; if (!it || it.star) throw new SqlError(`GROUP BY position ${g.v} is not in the select list`); return it.e; }
        if (g.k === "col" && !g.table) { bindCol(g); if (g.res === null) { const i = byAlias(g); if (i >= 0) return s.items[i].e; } }
        return g;
      });
      const grouped = groupExprs.length > 0 || s.items.some((it) => !it.star && containsAgg(it.e)) || (s.having && containsAgg(s.having));
      let out = [];
      const starCols = (it, row) => { const res = {}; for (const src of scope) if (!it.table || src.alias === it.table.toLowerCase()) for (const c of src.cols) res[c] = row[src.alias] ? row[src.alias][c] ?? null : null; return res; };
      const orderKeys = [];
      if (grouped) {
        if (s.items.some((it) => it.star)) throw new SqlError("SELECT * cannot be combined with GROUP BY");
        const groups = new Map();
        for (const r of rows) {
          const key = JSON.stringify(groupExprs.map((g) => { const v = evalR(g, r); return v === undefined ? null : v; }));
          let g = groups.get(key);
          if (!g) { g = { rows: [], first: r }; groups.set(key, g); }
          g.rows.push(r);
        }
        if (!groupExprs.length && !groups.size) groups.set("[]", { rows: [], first: null });
        for (const g of groups.values()) {
          if (s.having && !truthy(evalG(s.having, g))) continue;
          const rec = {};
          s.items.forEach((it, i) => { rec[outNames[i]] = evalG(it.e, g); });
          out.push({ rec, ctx: g });
        }
        for (const o of s.orderBy) orderKeys.push((item) => resolveOrder(o.e, item.rec, () => evalG(o.e, item.ctx)));
      } else {
        for (const r of rows) {
          let rec = {};
          s.items.forEach((it, i) => { if (it.star) Object.assign(rec, starCols(it, r)); else rec[outNames[i]] = evalR(it.e, r); });
          out.push({ rec, ctx: r });
        }
        for (const o of s.orderBy) orderKeys.push((item) => resolveOrder(o.e, item.rec, () => evalR(o.e, item.ctx)));
      }
      function resolveOrder(e, rec, fallback) {
        if (e.k === "lit" && isNum(e.v)) { const nm = outNames[e.v - 1]; if (!nm) throw new SqlError(`ORDER BY position ${e.v} is not in the select list`); return rec[nm]; }
        const i = byAlias(e);
        if (i >= 0) return rec[outNames[i]];
        return fallback();
      }
      if (s.distinct) { const seen = new Set(); out = out.filter((o) => { const k = JSON.stringify(o.rec); if (seen.has(k)) return false; seen.add(k); return true; }); }
      if (s.orderBy.length) {
        const keyed = out.map((item, idx) => ({ item, idx, keys: orderKeys.map((f) => f(item)) }));
        keyed.sort((a, b) => {
          for (let k = 0; k < s.orderBy.length; k++) {
            const o = s.orderBy[k], x = a.keys[k], y = b.keys[k];
            const xn = x === null || x === undefined, yn = y === null || y === undefined;
            if (xn || yn) { if (xn && yn) continue; const nullsFirst = o.nulls === "first"; return (xn ? -1 : 1) * (nullsFirst ? 1 : -1); }
            const c = compare(x, y);
            if (c) return o.dir === "desc" ? -c : c;
          }
          return a.idx - b.idx;
        });
        out = keyed.map((k) => k.item);
      }
      if (s.offset) out = out.slice(s.offset);
      if (s.limit !== null && s.limit !== undefined) out = out.slice(0, s.limit);
      const columns = out.length ? Object.keys(out[0].rec) : s.items.flatMap((it, i) => (it.star ? scope.filter((src) => !it.table || src.alias === it.table.toLowerCase()).flatMap((src) => src.cols) : [outNames[i]]));
      return { columns, records: out.map((o) => o.rec) };
    }
    function binop(o, lf, rf) {
      if (o === "and") { const a = lf(); if (a !== null && !truthy(a)) return false; const b = rf(); if (b !== null && !truthy(b)) return false; return a === null || b === null ? null : true; }
      if (o === "or") { const a = lf(); if (a !== null && truthy(a)) return true; const b = rf(); if (b !== null && truthy(b)) return true; return a === null || b === null ? null : false; }
      const a = lf(), b = rf();
      if (a === null || a === undefined || b === null || b === undefined) return null;
      switch (o) {
        case "=": return compare(a, b) === 0;
        case "!=": return compare(a, b) !== 0;
        case "<": return compare(a, b) < 0;
        case "<=": return compare(a, b) <= 0;
        case ">": return compare(a, b) > 0;
        case ">=": return compare(a, b) >= 0;
        case "||": return String(a) + String(b);
        default: {
          const x = toNum(a), y = toNum(b);
          if (x === null || y === null) throw new SqlError(`cannot apply '${o}' to '${a}' and '${b}'`);
          if (o === "+") return x + y; if (o === "-") return x - y; if (o === "*") return x * y;
          if (o === "/") return y === 0 ? null : x / y;
          if (o === "%") return y === 0 ? null : x % y;
          throw new Unsupported(`operator ${o}`);
        }
      }
    }
    function joinRows(left, scope, right, j, envMap) {
      const leftAliases = new Set(scope.map((x) => x.alias));
      const owners = (node) => { const set = new Set(); walk(node, (x) => { if (x.k === "col") { if (x.table) set.add(x.table.toLowerCase()); else { const k = x.name.toLowerCase(); if (right.cols.some((c) => c.toLowerCase() === k)) set.add(right.alias); else for (const s2 of scope) if (s2.cols.some((c) => c.toLowerCase() === k)) set.add(s2.alias); } } }); return set; };
      const conj = [];
      const split = (n) => { if (n && n.k === "bin" && n.op === "and") { split(n.l); split(n.r); } else if (n) conj.push(n); };
      if (j.using) for (const c of j.using) conj.push({ k: "bin", op: "=", l: { k: "col", table: scope.find((s2) => s2.cols.includes(c)).alias, name: c }, r: { k: "col", table: right.alias, name: c } });
      split(j.on);
      const pairs = [], residual = [];
      for (const c of conj) {
        const eq = c.k === "bin" && c.op === "=" ? [c.l, c.r, false] : c.k === "distinct" && c.not ? [c.l, c.r, true] : null;
        if (eq) {
          const lo = owners(eq[0]), ro = owners(eq[1]);
          const onlyRight = (s2) => s2.size === 1 && s2.has(right.alias), onlyLeft = (s2) => s2.size > 0 && [...s2].every((a) => leftAliases.has(a));
          if (onlyRight(ro) && onlyLeft(lo)) { pairs.push({ l: eq[0], r: eq[1], nullSafe: eq[2] }); continue; }
          if (onlyRight(lo) && onlyLeft(ro)) { pairs.push({ l: eq[1], r: eq[0], nullSafe: eq[2] }); continue; }
        }
        residual.push(c);
      }
      const evalRow = scopedEval([...scope, right]);
      const out = [];
      const nullLeft = Object.fromEntries(scope.map((x) => [x.alias, null]));
      const matchedRight = new Set();
      const passes = (row) => residual.every((c) => truthy(evalRow(c, row)));
      if (pairs.length) {
        const index = new Map();
        right.rows.forEach((rec, ri) => {
          const vals = pairs.map((pr) => evalRow(pr.r, { [right.alias]: rec }));
          if (vals.some((v, k) => (v === null || v === undefined) && !pairs[k].nullSafe)) return;
          const key = JSON.stringify(vals.map(normKey));
          (index.get(key) || index.set(key, []).get(key)).push(ri);
        });
        for (const row of left) {
          const vals = pairs.map((pr) => evalRow(pr.l, row));
          let hits = [];
          if (!vals.some((v, k) => (v === null || v === undefined) && !pairs[k].nullSafe)) hits = index.get(JSON.stringify(vals.map(normKey))) || [];
          let any = false;
          for (const ri of hits) { const comb = { ...row, [right.alias]: right.rows[ri] }; if (passes(comb)) { out.push(comb); any = true; matchedRight.add(ri); } }
          if (!any && (j.kind === "left" || j.kind === "full")) out.push({ ...row, [right.alias]: null });
        }
      } else {
        if (left.length * right.rows.length > 4e6) throw new Unsupported("a join without an equality key on this many rows");
        for (const row of left) {
          let any = false;
          right.rows.forEach((rec, ri) => { const comb = { ...row, [right.alias]: rec }; if (j.kind === "cross" || passes(comb)) { out.push(comb); any = true; matchedRight.add(ri); } });
          if (!any && (j.kind === "left" || j.kind === "full")) out.push({ ...row, [right.alias]: null });
        }
      }
      if (j.kind === "right" || j.kind === "full") right.rows.forEach((rec, ri) => { if (!matchedRight.has(ri)) out.push({ ...nullLeft, [right.alias]: rec }); });
      return out;
    }
    /** Evaluator for JOIN keys and residual ON predicates over an explicit scope (column index built once per join). */
    function scopedEval(scope) {
      const colIndex = new Map();
      for (const src of scope) for (const c of src.cols) { const k = c.toLowerCase(); colIndex.set(k, colIndex.has(k) ? null : src.alias); }
      let row = null;
      const ev = (x) => {
        switch (x.k) {
          case "lit": return x.v;
          case "col": { const a = x.table ? x.table.toLowerCase() : colIndex.get(x.name.toLowerCase()); if (!a) throw new SqlError(`unknown column '${x.name}'`); const rec = row[a]; if (!rec) return null; const v = rec[x.name.toLowerCase()]; return v === undefined ? (rec[x.name] ?? null) : v; }
          case "bin": return binop(x.op, () => ev(x.l), () => ev(x.r));
          case "distinct": { const a = ev(x.l), b = ev(x.r); const same = (a === null && b === null) || compare(a, b) === 0; return x.not ? same : !same; }
          case "isnull": { const v = ev(x.e); return x.not ? v !== null : v === null; }
          case "not": { const v = ev(x.e); return v === null ? null : !truthy(v); }
          case "neg": { const v = toNum(ev(x.e)); return v === null ? null : -v; }
          case "cast": return castTo(ev(x.e), x.type);
          case "fn": if (AGG.has(x.name)) throw new SqlError("aggregates are not allowed in JOIN conditions"); return scalarFn(x.name, x.args.map(ev));
          case "in": { const v = ev(x.e); if (v === null || !x.list) return null; return x.list.map(ev).some((y) => compare(v, y) === 0) ? !x.not : !!x.not; }
          case "between": { const v = ev(x.e), lo = compare(v, ev(x.lo)), hi = compare(v, ev(x.hi)); if (lo === null || hi === null) return null; return (lo >= 0 && hi <= 0) !== !!x.not; }
          case "like": { const v = ev(x.e), p = ev(x.pat); if (v === null || p === null) return null; return likeRe(p, x.esc ? ev(x.esc) : null, x.ci).test(String(v)) !== !!x.not; }
          case "case": { const base = x.base ? ev(x.base) : undefined; for (const [c, v] of x.whens) { if (x.base ? compare(base, ev(c)) === 0 : truthy(ev(c))) return ev(v); } return x.else ? ev(x.else) : null; }
          default: throw new Unsupported(`expression ${x.k} in a JOIN condition`);
        }
      };
      return (node, r) => { row = r; return ev(node); };
    }
    const normKey = (v) => (v === undefined ? null : typeof v === "boolean" ? +v : v);
    return runBody(stmt, env);
  }
  /** DuckDB's derived column name for an unaliased expression (count_star(), sum(x), (a * b) ...). */
  function duckName(n) {
    switch (n.k) {
      case "lit": return n.v === null ? "NULL" : typeof n.v === "string" ? `'${n.v}'` : String(n.v);
      case "col": return n.table ? `${n.table}.${n.name}` : n.name;
      case "fn": if (n.star) return n.name === "count" ? "count_star()" : `${n.name}(*)`; return `${n.name}(${n.distinct ? "DISTINCT " : ""}${n.args.map(duckName).join(", ")})`;
      case "bin": return `(${duckName(n.l)} ${n.op === "and" || n.op === "or" ? n.op.toUpperCase() : n.op} ${duckName(n.r)})`;
      case "cast": return `CAST(${duckName(n.e)} AS ${String(n.type).toUpperCase()})`;
      case "neg": return `-(${duckName(n.e)})`;
      default: return null;
    }
  }
  /** Tables referenced by a statement (excluding CTE / derived-table names). */
  function referencedTables(stmt) {
    const names = new Set(), ctes = new Set();
    const visit = (body) => {
      for (const c of body.ctes) { ctes.add(c.name.toLowerCase()); visit(c.q); }
      const s = body.select;
      for (const r of [s.from, ...s.joins.map((j) => j.table)].filter(Boolean)) { if (r.sub) visit(r.sub); else names.add(r.name.toLowerCase()); }
      walk([s.items.map((i) => i.e), s.where, s.having, s.groupBy, s.orderBy.map((o) => o.e), s.joins.map((j) => j.on)], (x) => { if (x.k === "subq") visit(x.q); if (x.k === "in" && x.subq) visit(x.subq); });
    };
    visit(stmt);
    return [...names].filter((n) => !ctes.has(n));
  }
  XP.sql = { tokenize, parse, validateSql, execute, referencedTables, SqlError, Unsupported };

  /* ============================================================ deterministic NL -> semantic (pure) */
  const GRAIN_WORDS = [[/\b(daily|per day|by day|each day)\b/, "day"], [/\b(weekly|per week|by week|each week|week over week)\b/, "week"],
    [/\b(monthly|per month|by month|each month|month over month|over time|trend)\b/, "month"], [/\b(quarterly|per quarter|by quarter)\b/, "quarter"], [/\b(yearly|annually|per year|by year|year over year)\b/, "year"]];
  const MONTH_NAMES = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"];
  const MONTHS = Object.fromEntries([...MONTH_NAMES.map((m, i) => [m, i + 1]), ...MONTH_NAMES.map((m, i) => [m.slice(0, 3), i + 1])]);
  const lastDay = (y, m) => new Date(Date.UTC(y, m, 0)).getUTCDate();
  const ymd = (y, m, d) => `${y}-${String(m).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
  function parseTimeRange(text, today, latestYear) {
    const t = String(text).toLowerCase();
    let m = t.match(/\bbetween (\d{4}-\d{2}-\d{2}) and (\d{4}-\d{2}-\d{2})\b/);
    if (m) return [m[1], m[2]];
    if ((m = t.match(/\b(?:since|from|after) (\d{4}-\d{2}-\d{2})\b/))) return [m[1], null];
    if ((m = t.match(/\b(?:before|until|up to) (\d{4}-\d{2}-\d{2})\b/))) return [null, m[1]];
    if ((m = t.match(/\blast (\d+) (day|week|month)s?\b/))) {
      const days = +m[1] * { day: 1, week: 7, month: 30 }[m[2]];
      const d = new Date(`${today}T00:00:00Z`); d.setUTCDate(d.getUTCDate() - days);
      return [d.toISOString().slice(0, 10), today];
    }
    if ((m = t.match(/\bq([1-4])\s*(\d{4})?\b/))) { const y = +(m[2] || latestYear), q = +m[1]; return [ymd(y, 3 * q - 2, 1), ymd(y, 3 * q, lastDay(y, 3 * q))]; }
    const monthAlt = Object.keys(MONTHS).sort((a, b) => b.length - a.length).join("|");
    if ((m = t.match(new RegExp(`\\b(?:in|during|for) (${monthAlt})\\.?(?: (\\d{4}))?\\b`)))) { const mon = MONTHS[m[1]], y = +(m[2] || latestYear); return [ymd(y, mon, 1), ymd(y, mon, lastDay(y, mon))]; }
    if ((m = t.match(/\b(?:in|during|for|since) (20\d{2})\b/))) { const y = +m[1]; return [ymd(y, 1, 1), /\bsince\b/.test(t) ? null : ymd(y, 12, 31)]; }
    return [null, null];
  }
  function phrasesOf(layer, kind) {
    const pool = kind === "metric" ? layer.metrics : layer.dimensions;
    const set = new Map();
    for (const n of Object.keys(pool)) set.set(`${n.replace(/_/g, " ")}\u0000${n}`, [n.replace(/_/g, " "), n]);
    for (const [k, v] of Object.entries(layer.synonyms)) if (v in pool) set.set(`${k}\u0000${v}`, [k, v]);
    return [...set.values()];
  }
  function nlToSemantic(question, layer, values = {}, today, latestYear) {
    const text = " " + String(question).toLowerCase().replace(/[^a-z0-9%\-/ ]+/g, " ") + " ";
    const used = new Array(text.length).fill(false);
    const phraseRe = (ph, plural = true) => new RegExp(`(?<![a-z0-9])${reEsc(ph)}${plural ? "(?:e?s)?" : ""}(?![a-z0-9])`, "g");
    const take = (ph) => { for (const m of text.matchAll(phraseRe(ph))) { if (!used.slice(m.index, m.index + m[0].length).some(Boolean)) { for (let i = m.index; i < m.index + m[0].length; i++) used[i] = true; return m.index; } } return -1; };
    const markers = [...text.matchAll(/\b(by|per|across|for each|for every|split by|broken down by|grouped by|vs|versus|and|which|each|top \d+|bottom \d+|best \d+|worst \d+)\b/g)].map((m) => m.index + m[0].length);
    const afterMarker = (pos) => markers.some((g) => g <= pos && text.slice(g, pos).split(/\s+/).filter(Boolean).length <= 2);
    const phrases = [...phrasesOf(layer, "metric").map(([ph, n]) => [ph, "metric", n]), ...phrasesOf(layer, "dimension").map(([ph, n]) => [ph, "dimension", n])]
      .sort((a, b) => b[0].length - a[0].length || (a[1] < b[1] ? -1 : a[1] > b[1] ? 1 : 0) || (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0));
    const mets = [], dims = [];
    for (const [ph, kind, name] of phrases) {
      const m = phraseRe(ph).exec(text);
      if (!m || used.slice(m.index, m.index + m[0].length).some(Boolean)) continue;
      if (kind === "metric" && !mets.some(([, n]) => n === name)) mets.push([take(ph), name]);
      else if (kind === "dimension" && afterMarker(m.index) && !dims.some(([, n]) => n === name)) dims.push([take(ph), name]);
    }
    const byPos = (a, b) => a[0] - b[0] || (a[1] < b[1] ? -1 : 1);
    let metricNames = [...mets].sort(byPos).map(([, n]) => n);
    if (!metricNames.length) {
      const entCounts = [["sessions", "session_count"], ["visits", "session_count"], ["quotes", "quote_count"], ["policies", "policy_count"], ["customers", "customer_count"], ["members", "customer_count"], ["products", "product_count"]];
      metricNames = entCounts.filter(([k, v]) => phraseRe(k, false).test(text) && v in layer.metrics).map(([, v]) => v).slice(0, 1);
      if (!metricNames.length) metricNames = [Object.keys(layer.metrics)[0]];
    }
    const metricEntities = new Set(metricNames.filter((m) => m in layer.metrics).map((m) => layer.metrics[m].entity));
    const reach = (dim) => {
      const owners = (layer.dimensions[dim] || []).map((d) => d.entity);
      if (owners.some((o) => metricEntities.has(o))) return 0;
      if ([...metricEntities].every((e) => owners.some((o) => joinPath(layer, e, o) !== null))) return 1;
      return 2;
    };
    const byValue = new Map();
    for (const [dim, vals] of Object.entries(values || {})) for (const v of vals) if (v.length >= 3) { if (!byValue.has(v)) byValue.set(v, []); byValue.get(v).push(dim); }
    const filters = [];
    for (const v of [...byValue.keys()].sort((a, b) => b.length - a.length)) {
      const m = phraseRe(v.toLowerCase(), false).exec(text);
      if (!m || used.slice(m.index, m.index + m[0].length).some(Boolean)) continue;
      const dim = [...byValue.get(v)].sort((a, b) => reach(a) - reach(b) || (a < b ? -1 : 1))[0];
      take(v.toLowerCase());
      const f = filters.find((x) => x.dimension === dim);
      if (!f) filters.push({ dimension: dim, op: "=", values: [v] }); else { f.op = "in"; f.values = [...f.values, v]; }
    }
    let grain = "none";
    for (const [re, g] of GRAIN_WORDS) if (re.test(text)) { grain = g; break; }
    const [dFrom, dTo] = parseTimeRange(question, today, latestYear);
    let limit = 100, order = [];
    const tm = text.match(/\b(top|bottom|best|worst) (\d+)\b/);
    if (tm) { limit = +tm[2]; order = [{ field: metricNames[0], direction: ["bottom", "worst"].includes(tm[1]) ? "asc" : "desc" }]; }
    const fdims = new Set(filters.map((f) => f.dimension));
    const dimsSorted = [...dims].sort(byPos).map(([, n]) => n).filter((n) => !fdims.has(n));
    if (grain === "none" && !dimsSorted.length && /\btrend|over time\b/.test(text)) grain = "month";
    return pruneSemantic({ metrics: metricNames, dimensions: dimsSorted, filters, time_grain: grain, time_dimension: null, date_from: dFrom, date_to: dTo, order_by: order, limit: Math.min(Math.max(limit, 1), MAX_ROWS) }, layer);
  }
  function pruneSemantic(q, layer) {
    const notes = [];
    for (let it = 0; it < 6; it++) {
      const [norm, errors] = validateSemantic(q, layer);
      if (!errors.length) return [norm, notes];
      let changed = false;
      for (const e of errors) {
        const m = e.match(/dimension '([^']+)' cannot be reached/) || e.match(/unknown (?:filter )?dimension '([^']+)'/);
        if (m) {
          const bad = m[1];
          q = { ...q, dimensions: (q.dimensions || []).filter((d) => d !== bad && resolveName(d, layer, "dimension") !== bad), filters: (q.filters || []).filter((f) => f.dimension !== bad && resolveName(f.dimension, layer, "dimension") !== bad),
            time_dimension: q.time_dimension === bad ? null : q.time_dimension };
          notes.push(`dropped '${bad}': ${e}`); changed = true;
        } else if (e.includes("no date dimension")) { q = { ...q, time_grain: "none", date_from: null, date_to: null }; notes.push(e); changed = true; }
        else if (e.startsWith("unknown metric")) { const keep = (q.metrics || []).filter((x) => resolveName(x, layer, "metric")); q = { ...q, metrics: keep.length ? keep : [Object.keys(layer.metrics)[0]] }; notes.push(e); changed = true; }
      }
      if (!changed) break;
    }
    return [validateSemantic(q, layer)[0], notes];
  }

  /* ============================================================ results, chart, facts (pure) */
  function partialPeriods(periods, grain, dataMin, dataMax) {
    if (["none", "day"].includes(grain) || !periods.length) return [];
    const endOf = (s) => { const [y, m, d] = s.split("-").map(Number);
      if (grain === "week") { const t = new Date(Date.UTC(y, m - 1, d + 6)); return t.toISOString().slice(0, 10); }
      if (grain === "month") return ymd(y, m, lastDay(y, m));
      if (grain === "quarter") return ymd(y, m + 2, lastDay(y, m + 2));
      return ymd(y, 12, 31); };
    return [...new Set(periods.map((v) => isoDay(String(v))).filter(Boolean))].sort().filter((p) => (dataMax && endOf(p) > dataMax) || (dataMin && p < dataMin));
  }
  const numericCol = (rows, c) => { const vals = rows.map((r) => r[c]).filter((v) => v !== null && v !== undefined); return vals.length > 0 && vals.every((v) => typeof v === "number"); };
  function datasetHints(columns, rows, q) {
    if (q) {
      const x = columns.includes("period") ? "period" : q.dimensions.length ? q.dimensions[0] : null;
      const group = columns.includes("period") && q.dimensions.length ? q.dimensions[0] : q.dimensions.length > 1 ? q.dimensions[1] : null;
      return { x, y: q.metrics.filter((m) => columns.includes(m)), group };
    }
    const num = columns.filter((c) => numericCol(rows, c)), other = columns.filter((c) => !num.includes(c));
    return { x: other[0] ?? null, y: num, group: other.length > 1 ? other[1] : null };
  }
  function resultFacts(columns, rows, hints) {
    const facts = [`${XP.int(rows.length)} row(s); columns: ${columns.join(", ")}`];
    const x = hints.x, group = hints.group;
    for (const y of (hints.y || []).slice(0, 3)) {
      if (!columns.includes(y) || !numericCol(rows, y)) continue;
      const vals = rows.map((r) => (r[y] === null || r[y] === undefined ? null : +r[y]));
      const nonNull = vals.filter((v) => v !== null);
      const absMax = Math.max(...nonNull.map(Math.abs));
      const integral = nonNull.every((v) => v % 1 === 0);
      const fmt = absMax <= 1 && (y.includes("rate") || y.includes("share")) ? (v) => XP.pct(v, 2) : integral || absMax >= 1000 ? (v) => XP.int(v) : (v) => U.fmtNum(v, 2);
      const where = (r) => `${x} = ${r[x]}` + (group && columns.includes(group) ? `, ${group} = ${r[group]}` : "");
      if (x && columns.includes(x) && rows.length > 1) {
        let hi = -1, lo = -1;
        vals.forEach((v, i) => { if (v === null) return; if (hi < 0 || v > vals[hi]) hi = i; if (lo < 0 || v < vals[lo]) lo = i; });
        facts.push(`${y}: highest ${fmt(vals[hi])} at ${where(rows[hi])}, lowest ${fmt(vals[lo])} at ${where(rows[lo])}`);
        if (!(y.includes("rate") || y.includes("avg") || y.includes("per_"))) facts.push(`${y}: total ${fmt(nonNull.reduce((s, v) => s + v, 0))} across the rows shown`);
        if (x === "period" && !hints.group && rows.length >= 2) {
          const d = rows.map((r, i) => [r, i]).sort((a, b) => (compare(a[0][x], b[0][x]) || a[1] - b[1])).map(([r]) => r);
          const first = +d[0][y], last = +d[d.length - 1][y];
          if (first) facts.push(`${y}: ${fmt(first)} in the first period (${d[0][x]}) vs ${fmt(last)} in the last (${d[d.length - 1][x]}), ${(last / first - 1 >= 0 ? "+" : "-") + Math.abs((last / first - 1) * 100).toFixed(1)}%`);
        }
      } else facts.push(`${y}: ${fmt(vals[0])}`);
    }
    return facts;
  }
  function renderChart(rows, hints, title, themed) {
    const x = hints.x, ys = hints.y || [], group = hints.group;
    if (!rows.length || !ys.length) return null;
    const y = ys[0];
    const ttl = T().truncate(title, 90);
    if (x === null || x === undefined) return CAF.lib.chart.bars({ items: ys.map((c) => ({ label: c, value: +rows[0][c] || 0 })), title: ttl, themed });
    if (x === "period") {
      const periods = [...new Set(rows.map((r) => r[x]))].sort();
      if (group) {
        const totals = new Map();
        for (const r of rows) totals.set(r[group], (totals.get(r[group]) || 0) + (+r[y] || 0));
        const top = [...totals.entries()].sort((a, b) => b[1] - a[1]).slice(0, 8).map(([g]) => g);
        return CAF.lib.chart.line({ series: top.slice(0, 4).map((g) => ({ name: String(g), values: periods.map((p) => { const r = rows.find((z) => z[x] === p && z[group] === g); return r ? +r[y] : null; }) })), labels: periods, title: `${ttl} - ${y.replace(/_/g, " ")}`, themed, yLabel: y.replace(/_/g, " ") });
      }
      return CAF.lib.chart.line({ series: [{ name: y, values: periods.map((p) => { const r = rows.find((z) => z[x] === p); return r ? +r[y] : null; }) }], labels: periods, title: ttl, themed, yLabel: y.replace(/_/g, " ") });
    }
    const d = [...rows].sort((a, b) => (+b[y] || 0) - (+a[y] || 0)).slice(0, 20);
    return CAF.lib.chart.bars({ items: d.map((r) => ({ label: group && r[group] !== undefined ? `${r[x]} - ${r[group]}` : String(r[x]), value: +r[y] || 0 })), title: `${ttl} - ${y.replace(/_/g, " ")}`, themed,
      format: (v) => (y.includes("rate") || y.includes("share")) && Math.abs(v) <= 1 ? XP.pct(v, 2) : CAF.lib.chart.fmt(v) });
  }
  function catalogueLines(layer) {
    const mets = Object.entries(layer.metrics).map(([n, m]) => `${n} [${m.entity}]: ${m.description || ""}`);
    const dims = Object.entries(layer.dimensions).map(([n, defs]) => `${n} (${defs[0].type || "string"}, ${defs.map((x) => x.entity).join("/")}): ${defs[0].description || ""}`);
    const syn = Object.entries(layer.synonyms).slice(0, 80).map(([k, v]) => `${k} -> ${v}`);
    return [mets, dims, syn];
  }
  const typeOf = (rows, c) => { const v = rows.map((r) => r[c]).find((x) => x !== null && x !== undefined); if (typeof v === "number") return rows.every((r) => r[c] === null || r[c] === undefined || Number.isInteger(r[c])) ? "BIGINT" : "DOUBLE"; if (typeof v === "string" && /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}/.test(v)) return "TIMESTAMP"; if (typeof v === "string" && /^\d{4}-\d{2}-\d{2}$/.test(v)) return "DATE"; return "VARCHAR"; };

  CAF.agent("exploration_generation", {
    async run(input, ctx) {
      if (!XP.generateOrNone || !XP.pageFacts) throw new Error("The shared experimentation kit (CAF.xp, defined in experiment_ideation.js) did not load");
      const maxRows = Math.min(input.max_rows || MAX_ROWS, MAX_ROWS);
      /* ---- context: semantic layer, warehouse tables, known values, date bounds */
      const cfg = ctx.skill("semantic_layer", null);
      if (!cfg) throw new Error("No semantic layer configured (config/skills/semantic_layer.yaml)");
      const layer = loadLayer(cfg);
      const whNames = CAF.datasets.names().filter((n) => n.startsWith("warehouse/")).map((n) => n.slice("warehouse/".length));
      const data = {};
      for (const t of whNames) data[t.toLowerCase()] = await ctx.data(`warehouse/${t}`);
      if (!whNames.length) { ctx.degraded("no warehouse tables are embedded in the console"); }
      const tables = Object.fromEntries(Object.entries(data).map(([t, rows]) => [t, rows.length ? Object.keys(rows[0]).map((c) => [c, typeOf(rows, c)]) : []]));
      const values = {}, bounds = {};
      for (const [name, defs] of Object.entries(layer.dimensions)) {
        const d = defs[0], rows = data[String(d.table).toLowerCase()];
        if (d.type !== "string" || name.endsWith("_id") || !rows) continue;
        const col = d.column || d.name;
        const seen = [];
        for (const r of rows) { const v = r[col]; if (v === null || v === undefined || seen.includes(String(v))) continue; seen.push(String(v)); if (seen.length > 40) break; }
        if (seen.length > 0 && seen.length <= 40) values[name] = seen;
      }
      let latest = null;
      for (const [ent, tdn] of Object.entries(layer.time_dims)) {
        const d = layer.dimensions[tdn][0], rows = data[String(d.table).toLowerCase()];
        if (!rows) continue;
        let lo = null, hi = null;
        for (const r of rows) { const v = r[d.column || d.name]; if (v === null || v === undefined) continue; const s = String(v); if (lo === null || s < lo) lo = s; if (hi === null || s > hi) hi = s; }
        if (lo === null) continue;
        bounds[`${ent}.${tdn}`] = `${lo.slice(0, 10)} to ${hi.slice(0, 10)}`;
        latest = latest === null || hi.slice(0, 10) > latest ? hi.slice(0, 10) : latest;
      }
      const missing = [...new Set(Object.entries(layer.entities).map(([n, e]) => e.table || n))].filter((t) => !(t.toLowerCase() in tables)).sort();
      if (missing.length) ctx.warn(`Semantic layer tables missing from the warehouse: ${missing.join(", ")}`);
      /* ---- generate the query */
      const today = U.today();
      const [mets, dims, syn] = catalogueLines(layer);
      const common = { question: input.question, metric_lines: mets, dimension_lines: dims, synonym_lines: syn, today, bounds: Object.entries(bounds).map(([k, v]) => `${k}: ${v}`),
        value_lines: Object.entries(values).map(([k, v]) => `${k}: ${v.slice(0, 15).join(", ")}`), examples: (layer.examples || []).slice(0, 5).map((e) => `Q: ${e.question}\n${String(e.sql).trim()}`) };
      const tableLines = Object.entries(tables).map(([t, cols]) => `${t}(${cols.map(([c]) => c).join(", ")})`);
      let source, sqlDraft = null, semantic = null;
      if (input.sql) { source = "user_sql"; sqlDraft = input.sql; }
      else {
        if (input.mode === "sql") {
          const draft = await XP.generateOrNone(ctx, "sql", { ...common, table_lines: tableLines, max_rows: maxRows }, "no language model configured: question mapped to the semantic layer by deterministic synonym matching");
          if (draft) { source = "model"; sqlDraft = draft.sql; }
          else ctx.warn("Free-SQL mode needs a model; the question was answered through the semantic layer instead.");
        }
        if (!sqlDraft) {
          ctx.progress("Mapping the question to the semantic layer");
          let q = await XP.generateOrNone(ctx, "query", common, "no language model configured: question mapped to the semantic layer by deterministic synonym matching");
          source = "model";
          if (q) { const [, errors] = validateSemantic(q, layer); if (errors.length) { ctx.warn(`Model query failed validation (${errors.slice(0, 3).join("; ")}); deterministic mapping used instead.`); q = null; } }
          let notes;
          if (!q) { [q, notes] = nlToSemantic(input.question, layer, values, today, +((latest || today).slice(0, 4))); for (const n of notes) ctx.warn(`Not answerable from the semantic layer and left out: ${n}`); source = "deterministic"; }
          else { [q, notes] = pruneSemantic(q, layer); for (const n of notes) ctx.warn(`Adjusted the model query: ${n}`); }
          semantic = q;
        }
      }
      /* ---- validate: semantic -> compile; then the SQL validator */
      let sql, semErrors = [], semWarnings = [];
      if (semantic) {
        const [norm, errs, warns] = validateSemantic(semantic, layer);
        semErrors = errs; semWarnings = warns; semantic = norm;
        ctx.check({ rule: "exploration.semantic", status: errs.length ? "fail" : "pass", severity: "high",
          message: errs.length ? errs.join("; ") : `Semantic query valid: ${norm.metrics.join(", ")}` + (norm.dimensions.length ? ` by ${norm.dimensions.join(", ")}` : "") + (norm.time_grain !== "none" ? `, ${norm.time_grain}` : "") });
        if (errs.length) throw new Error("The question could not be mapped to the semantic layer: " + errs.join("; "));
        sql = compileSemantic(norm, layer);
      } else sql = sqlDraft;
      let v = validateSql(sql, tables, maxRows);
      if (!v.valid && !v.unsupported && source === "model" && ctx.llmLive) {
        try {
          const fixed = await ctx.generate("sql", { question: input.question, table_lines: tableLines, max_rows: maxRows, errors: v.errors, previous_sql: sql, metric_lines: [], dimension_lines: [], synonym_lines: [], today, bounds: [], value_lines: [], examples: [] });
          v = validateSql(fixed.sql, tables, maxRows);
        } catch { /* keep the first validation */ }
      }
      v.semantic_errors = semErrors;
      v.warnings = [...semWarnings, ...v.warnings];
      let notExecuted = null;
      if (!v.valid && v.unsupported) notExecuted = v.unsupported;
      ctx.check({ rule: "exploration.sql", status: v.valid ? "pass" : notExecuted ? "warn" : "fail", severity: "critical",
        message: v.valid ? "SQL validated: read-only SELECT on " + v.tables.join(", ") + (v.limit_applied ? "; LIMIT enforced" : "") : v.errors.join("; ") });
      if (!v.valid && !notExecuted) throw new Error("Query rejected by the SQL validator: " + v.errors.join("; "));
      /* ---- execute in the browser engine */
      let columns = [], rows = [];
      if (!notExecuted) {
        ctx.progress("Running the query in the browser SQL engine");
        try {
          const parsed = parse(v.sql);
          const res = execute(parsed.stmt, data);
          columns = res.columns.map(String);
          const iso = (x) => { const m = typeof x === "string" && x.match(/^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(\.\d+)?$/); return m ? (m[2] === "00:00:00" && !m[3] ? m[1] : `${m[1]}T${m[2]}${m[3] || ""}`) : x; };
          rows = res.records.slice(0, maxRows).map((r) => Object.fromEntries(columns.map((c) => { const x = r[c]; return [c, x === undefined || (typeof x === "number" && !Number.isFinite(x)) ? null : iso(x)]; })));
        } catch (e) {
          if (!(e instanceof Unsupported)) throw new Error(`Query failed in the browser SQL engine: ${e.message}`);
          notExecuted = e.message;
        }
      }
      if (notExecuted) {
        const reason = `the browser SQL engine does not support ${notExecuted}: the query was validated but not executed - run it on the Python service (DuckDB)`;
        ctx.warn(`Not executed in the browser: ${notExecuted}.`); ctx.degraded(reason);
      }
      if (!rows.length && !notExecuted) ctx.warn("The query returned no rows.");
      const hints = datasetHints(columns, rows, semantic);
      if (semantic && !["none", "day"].includes(semantic.time_grain) && columns.includes("period")) {
        let lo = null, hi = null;
        for (const m of semantic.metrics) {
          const ent = layer.metrics[m].entity;
          const span = layer.time_dims[ent] ? bounds[`${ent}.${semantic.time_dimension || layer.time_dims[ent]}`] : null;
          if (span) { const [a, b] = span.split(" to ").map((x) => isoDay(x.trim())); lo = lo && a ? (a > lo ? a : lo) : a || lo; hi = hi && b ? (b < hi ? b : hi) : b || hi; }
        }
        if (semantic.date_from && isoDay(semantic.date_from)) lo = lo ? (isoDay(semantic.date_from) > lo ? isoDay(semantic.date_from) : lo) : isoDay(semantic.date_from);
        if (semantic.date_to && isoDay(semantic.date_to)) hi = hi ? (isoDay(semantic.date_to) < hi ? isoDay(semantic.date_to) : hi) : isoDay(semantic.date_to);
        const partial = partialPeriods(rows.map((r) => r.period), semantic.time_grain, lo, hi);
        if (partial.length) { hints.partial_periods = partial; ctx.warn(`Partial ${semantic.time_grain}(s) in the result (data covers them only in part): ${partial.join(", ")}.`); }
      }
      /* ---- visualise */
      ctx.artifact({ name: "query.sql", mime: "application/sql", content: v.sql + "\n", description: "Validated SQL" });
      ctx.artifact({ name: "results.csv", mime: "text/csv", content: CAF.lib.csv.stringify(rows, columns), description: "Query results" });
      let chart = null, viewSvg = null;
      if (input.chart !== false && rows.length) {
        try {
          const svg = renderChart(rows, hints, input.question, false);
          if (svg) { ctx.artifact({ name: "chart.svg", mime: "image/svg+xml", content: svg, description: `Chart: ${T().truncate(input.question, 80)}` }); chart = "chart.svg"; viewSvg = renderChart(rows, hints, input.question, true); }
        } catch (e) { ctx.log(`chart failed: ${e.message}`); }
      }
      /* ---- explain (model reads computed facts only) */
      const facts = notExecuted ? [`The query was validated but not executed in the browser (${notExecuted}).`] : resultFacts(columns, rows, hints);
      const ex = await XP.generateOrNone(ctx, "explain", { question: input.question, sql: v.sql, facts }, "no language model configured: question mapped to the semantic layer by deterministic synonym matching");
      let explanation, follow;
      if (ex) { explanation = ex.explanation; follow = (ex.follow_up_questions || []).slice(0, 4); }
      else {
        explanation = `Answer to '${input.question}': ` + facts.map((f) => f.replace(/\.+$/, "") + ".").join(" ");
        follow = [];
        if (semantic) {
          if (!semantic.dimensions.length) follow.push(`How does ${semantic.metrics[0].replace(/_/g, " ")} break down by channel or device?`);
          if (semantic.time_grain === "none") follow.push(`How has ${semantic.metrics[0].replace(/_/g, " ")} changed month by month?`);
        }
        ctx.degraded("no language model: explanation written from computed result facts");
      }
      const output = { question: input.question, mode: source !== "user_sql" ? input.mode || "semantic" : "sql", semantic_query: semantic, sql: v.sql, validation: v, columns, rows, row_count: rows.length, chart,
        explanation, follow_up_questions: follow, dataset: rows, dataset_hints: hints, query_source: source, findings: [] };
      const view = [{ type: "code", title: `SQL (${semantic ? "compiled from the semantic layer" : "validated"}; executed in the browser engine)`, code: v.sql }];
      if (viewSvg) view.push({ type: "svg", title: "Chart", svg: viewSvg });
      view.push({ type: "table", title: `${rows.length} row(s)`, rows: rows.slice(0, 100), columns: columns.slice(0, 10) }, { type: "markdown", title: "Explanation", markdown: explanation });
      return {
        output,
        summary: `${rows.length} row(s) for '${T().truncate(input.question, 70)}' via ${semantic ? "semantic layer" : "validated SQL"} (${source}); tables: ${v.tables.join(", ")}`,
        view,
      };
    },
    selftest() { return { question: "Weekly sessions and conversion rate by device since 2026-06-01" }; },
  });
  XP.exploration = { loadLayer, validateSemantic, compileSemantic, nlToSemantic, pruneSemantic, parseTimeRange, joinPath, datasetHints, resultFacts, partialPeriods };
})();
