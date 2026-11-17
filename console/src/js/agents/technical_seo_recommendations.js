/* Technical SEO Recommendations - browser twin of caf/agents/web/technical_seo_recommendations.py.
   Same stages (crawl -> evaluate -> prioritise -> explain -> report), the same seo_rules skill and
   ports of the same pure check functions. The page cannot fetch, so the crawl is a BFS over the
   embedded demo site that mirrors the Python fixture transport (301s and 404s from /_fixture.json,
   robots.txt, sitemap.xml). Any other site: pasted `html` runs the page-level rules only and the run
   is degraded exactly like the Python html-only path. robots.txt decisions use a port of CPython's
   RFC 9309 urllib.robotparser (longest match, wildcards, merged groups). The model only explains. */
(() => {
  const AGENT = "technical_seo_recommendations";
  const PLACEHOLDER = "https://page.invalid/";
  const SEV_ORDER = { critical: 0, high: 1, medium: 2, low: 3 };
  const LANG_RX = /^[a-zA-Z]{2,3}(-[a-zA-Z0-9]{2,8})*$/;
  const HREFLANG_RX = /^(x-default|[a-z]{2}(-[a-z]{4})?(-([a-z]{2}|\d{3}))?)$/i;
  const IMPORTANCE_LABEL = { search_console: "first-party search clicks and impressions", internal_links: "internal link share (no search data)" };

  // ------------------------------------------------------------------ small Python-parity helpers
  /** Python format(x, ".{d}f") / round(x, d): correctly rounded, half-even on exact binary ties. */
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
  const pyFloat = (x) => (x === null || x === undefined || x === "" ? "" : Number.isInteger(x) ? x.toFixed(1) : String(x));
  const plen = (s) => [...String(s || "")].length;
  const pslice = (s, n) => [...String(s || "")].slice(0, n).join("");
  const uniqSorted = (arr) => [...new Set(arr)].sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
  const cmp = (a, b) => (a < b ? -1 : a > b ? 1 : 0);
  const bytesOf = (s) => new TextEncoder().encode(String(s || "")).length;

  /** BeautifulSoup get_text(" ", strip=True): stripped text nodes joined with one space. */
  function bsText(el) {
    const out = [];
    const walk = (n) => { for (const c of n.childNodes) { if (c.nodeType === 3) { const t = c.nodeValue.trim(); if (t) out.push(t); } else if (c.nodeType === 1) walk(c); } };
    if (el) walk(el);
    return out.join(" ");
  }
  const relTokens = (el) => String(el.getAttribute("rel") || "").toLowerCase().split(/\s+/).filter(Boolean);

  // ------------------------------------------------------------------ URL helpers (norm_url & co.)
  function normUrl(url, base) {
    const raw = String(url ?? "").trim();
    let u;
    try { u = base ? new URL(raw, base) : new URL(raw); } catch { return raw.split("#")[0]; }
    u.hash = "";
    if (!/^https?:$/.test(u.protocol)) return u.href;
    return `${u.protocol}//${u.host}${u.pathname || "/"}${u.search}`;
  }
  function hostOf(url) { try { return new URL(url).hostname.toLowerCase(); } catch { return ""; } }
  function splitUrl(url) { try { const u = new URL(url); return { scheme: u.protocol, netloc: u.host, path: u.pathname, query: u.search.replace(/^\?/, "") }; } catch { return { scheme: "", netloc: "", path: url, query: "" }; } }
  function samePage(a, b) { const pa = splitUrl(normUrl(a)), pb = splitUrl(normUrl(b)); return pa.scheme === pb.scheme && pa.netloc === pb.netloc && pa.path === pb.path; }
  function shortUrl(url, start) {
    if (start && hostOf(url) === hostOf(start)) { const p = splitUrl(url); return p.path + (p.query ? `?${p.query}` : ""); }
    return url;
  }

  // ------------------------------------------------------------------ robots.txt (port of CPython 3.13 urllib.robotparser, RFC 9309)
  const Robots = (() => {
    const SAFE = /[A-Za-z0-9_.\-~/]/;
    function quote(s) {
      let out = "";
      for (const ch of String(s)) {
        if (SAFE.test(ch)) { out += ch; continue; }
        try { out += encodeURIComponent(ch).replace(/[!'()*]/g, (c) => "%" + c.charCodeAt(0).toString(16).toUpperCase()); } catch { out += ch; }
      }
      return out;
    }
    const unquote = (s) => String(s).replace(/(%[0-9A-Fa-f]{2})+/g, (m) => { try { return decodeURIComponent(m); } catch { return m; } });
    const normalize = (p) => quote(unquote(p));
    function normalizeUri(path) {
      const i = path.indexOf("?");
      if (i < 0) return normalize(path);
      return normalize(path.slice(0, i)) + "?" + path.slice(i + 1).replace(/[^=&]+/g, (m) => normalize(m));
    }
    function normalizePattern(path) {
      const i = path.indexOf("?");
      let out = (i < 0 ? path : path.slice(0, i)).replace(/[^*$]+/g, (m) => normalize(m));
      if (i >= 0) out += "?" + path.slice(i + 1).replace(/[^=&*$]+/g, (m) => normalize(m));
      return out;
    }
    const reEsc = (s) => s.replace(/[.*+?^${}()|[\]\\/-]/g, "\\$&");
    /** (?>.*?part) atomic groups emulated with lookahead + back-reference (JS has no atomic groups). */
    function translatePattern(path) {
      const parts = path.split("*").map(reEsc);
      let src = parts[0];
      for (let i = 1; i < parts.length - 1; i++) src += `(?=([\\s\\S]*?${parts[i]}))\\${i}`;
      return src + `[\\s\\S]*${parts[parts.length - 1]}`;
    }
    function ruleLine(path, allowance) {
      if (path === "" && !allowance) allowance = true;
      path = normalizePattern(path.replace(/[*]{2,}/g, "*").replace(/[$][$*]+/g, "$"));
      const fullmatch = path.endsWith("$");
      path = path.replace(/\$+$/, "");
      if (path.includes("$")) return null;  // ValueError in Python: line ignored
      let matcher = null;
      if (path.includes("*")) { const rx = new RegExp(`^(?:${translatePattern(path)})${fullmatch ? "$" : ""}`); matcher = (f) => { const m = rx.exec(f); return m ? m[0].length : -1; }; }
      return {
        path, allowance, fullmatch,
        appliesTo(f) {
          if (matcher) { const end = matcher(f); return end >= 0 ? end + 1 : 0; }
          if (fullmatch) return f === path ? path.length + 1 : 0;
          return f.startsWith(path) ? path.length + 1 : 0;
        },
      };
    }
    const entryApplies = (entry, ua) => { const u = String(ua).split("/")[0].toLowerCase(); return entry.useragents.some((a) => a !== "*" && u.includes(a.toLowerCase())); };
    function allowance(entry, filename) {
      let best = -1, allow = true;
      for (const line of entry.rulelines) {
        const m = line.appliesTo(filename);
        if (m) { if (m > best) { best = m; allow = line.allowance; } else if (m === best && !allow) allow = line.allowance; }
      }
      return allow;
    }
    function parse(text) {
      const rp = { entries: [], groups: new Map(), sitemaps: [] };
      const addEntry = (e) => {
        rp.entries.push(e);
        for (const a of e.useragents.map((x) => x.toLowerCase())) {
          const prev = rp.groups.get(a);
          rp.groups.set(a, prev ? { useragents: prev.useragents.filter((x) => e.useragents.includes(x)), rulelines: [...prev.rulelines, ...e.rulelines] } : e);
        }
      };
      let state = 0, entry = { useragents: [], rulelines: [] };
      for (let line of String(text || "").split(/\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]/)) {
        const hash = line.indexOf("#");
        if (hash >= 0) line = line.slice(0, hash);
        line = line.trim();
        if (!line) continue;
        const colon = line.indexOf(":");
        if (colon < 0) continue;
        const key = line.slice(0, colon).trim().toLowerCase(), val = line.slice(colon + 1).trim();
        if (key === "user-agent") { if (state === 2) { addEntry(entry); entry = { useragents: [], rulelines: [] }; } entry.useragents.push(val); state = 1; }
        else if (key === "disallow" || key === "allow") { if (state !== 0) { state = 2; const rl = ruleLine(val, key === "allow"); if (rl) entry.rulelines.push(rl); } }
        else if (key === "crawl-delay" || key === "request-rate") { if (state !== 0) state = 2; }
        else if (key === "sitemap") rp.sitemaps.push(val);
      }
      if (state !== 0) addEntry(entry);
      return rp;
    }
    function findEntry(rp, ua) {
      const exact = rp.groups.get(String(ua).toLowerCase());
      if (exact) return exact;
      for (const e of rp.groups.values()) if (entryApplies(e, ua)) return e;
      return rp.groups.get("*") || null;
    }
    function canFetch(rp, ua, url) {
      const m = /^[a-zA-Z][a-zA-Z0-9+.-]*:\/\/[^/?#]*/.exec(String(url));
      let rest = m ? String(url).slice(m[0].length) : String(url);
      rest = normalizeUri(rest) || "/";
      if (rest === "/robots.txt") return true;
      const entry = findEntry(rp, ua);
      return entry ? allowance(entry, rest) : true;
    }
    return { parse, canFetch, siteMaps: (rp) => (rp.sitemaps.length ? rp.sitemaps : null) };
  })();

  // ------------------------------------------------------------------ page facts (extract_page_facts)
  function parseJsonld(raw) {
    let text = String(raw || "").trim();
    text = text.replace(/^\s*(<!--|<!\[CDATA\[)/, "").replace(/(-->|\]\]>)\s*$/, "").trim();
    if (!text) return [null, "empty JSON-LD block"];
    try { return [JSON.parse(text), null]; } catch (e) { return [null, String((e && e.message) || e)]; }
  }
  function jsonldTypes(data) {
    const out = [];
    for (const item of Array.isArray(data) ? data : [data]) {
      if (!U.isObj(item)) continue;
      if (Array.isArray(item["@graph"])) out.push(...jsonldTypes(item["@graph"]));
      const t = item["@type"];
      if (Array.isArray(t)) out.push(...t.map(String)); else if (t) out.push(String(t));
    }
    return out;
  }
  function headingSkips(headings) {
    const skips = [];
    for (let i = 1; i < headings.length; i++) if (headings[i][0] > headings[i - 1][0] + 1) skips.push({ from: headings[i - 1][0], to: headings[i][0], text: pslice(headings[i][1], 80) });
    return skips;
  }
  function extractPageFacts(url, html, headers = {}) {
    const doc = CAF.lib.html.parse(html || "");
    const head = doc.head || doc;
    const hdr = Object.fromEntries(Object.entries(headers || {}).map(([k, v]) => [k.toLowerCase(), v]));
    const pageHost = hostOf(url);
    const titles = [...head.querySelectorAll("title")].map(bsText);
    const descs = [...doc.querySelectorAll("meta[name]")].filter((m) => /^description$/i.test(m.getAttribute("name"))).map((m) => (m.getAttribute("content") || "").trim());
    const canonicals = [...doc.querySelectorAll("link[href]")].filter((l) => relTokens(l).includes("canonical")).map((l) => normUrl(l.getAttribute("href"), url));
    const hreflang = [...doc.querySelectorAll("link[href][hreflang]")].filter((l) => relTokens(l).includes("alternate"))
      .map((l) => ({ lang: (l.getAttribute("hreflang") || "").trim(), href: normUrl(l.getAttribute("href"), url) }));
    const robotsMeta = [...doc.querySelectorAll("meta[name]")].filter((m) => /^(robots|googlebot)$/i.test(m.getAttribute("name"))).map((m) => m.getAttribute("content") || "").join(" ").toLowerCase();
    const xRobots = String(hdr["x-robots-tag"] || "").toLowerCase();
    const directives = uniqSorted(`${robotsMeta} ${xRobots}`.split(/[,\s]+/).map((d) => d.trim()).filter(Boolean));
    const lang = (doc.documentElement.getAttribute("lang") || "").trim();
    const headings = [...doc.querySelectorAll("h1,h2,h3,h4,h5,h6")].map((h) => [Number(h.tagName[1]), bsText(h)]);
    const jsonld = [...doc.querySelectorAll("script[type]")].filter((s) => /^\s*application\/ld\+json\s*$/i.test(s.getAttribute("type"))).map((s, i) => {
      const [data, err] = parseJsonld(s.textContent);
      const ctxOk = (U.isObj(data) && ("@context" in data || "@graph" in data)) || Array.isArray(data);
      return { index: i + 1, error: err, types: err === null ? jsonldTypes(data) : [], has_context: !!ctxOk };
    });
    const images = [...doc.querySelectorAll("img")];
    const missingAlt = images.filter((img) => !img.hasAttribute("alt")).map((img) => pslice(img.getAttribute("src") || "", 200));
    const internal = [], external = [];
    for (const a of doc.querySelectorAll("a[href]")) {
      const href = (a.getAttribute("href") || "").trim();
      if (!href || /^(#|mailto:|tel:|javascript:|data:)/.test(href)) continue;
      const abs = normUrl(href, url);
      if (!/^https?:\/\//.test(abs)) continue;
      (hostOf(abs) === pageHost ? internal : external).push(abs);
    }
    const resources = [...doc.querySelectorAll("link[href]")].filter((l) => relTokens(l).includes("stylesheet")).map((l) => normUrl(l.getAttribute("href"), url))
      .concat([...doc.querySelectorAll("script[src]")].map((s) => normUrl(s.getAttribute("src"), url)));
    const body = (doc.body || doc.documentElement).cloneNode(true);
    for (const t of body.querySelectorAll("script,style,noscript,template")) t.remove();
    const words = [];
    const walk = (n) => { for (const c of n.childNodes) { if (c.nodeType === 3) words.push(c.nodeValue); else if (c.nodeType === 1) walk(c); } };
    walk(body);
    return {
      url, title: titles[0] || "", title_count: titles.length, meta_description: descs[0] || "", meta_description_count: descs.length,
      canonical: canonicals[0] || null, canonical_count: canonicals.length, hreflang,
      noindex: directives.includes("noindex") || directives.includes("none"), robots_directives: directives, lang,
      h1: headings.filter(([lvl]) => lvl === 1).map(([, t]) => t), headings, heading_skips: headingSkips(headings), jsonld,
      images_total: images.length, images_missing_alt: missingAlt, links_internal: uniqSorted(internal), links_external: uniqSorted(external),
      resources: uniqSorted(resources), word_count: words.join(" ").split(/\s+/).filter(Boolean).length, html_bytes: bytesOf(html),
    };
  }
  function parseSitemap(xml) {
    const bad = (error) => ({ kind: "invalid", urls: [], sitemaps: [], error });
    let doc;
    try { doc = new DOMParser().parseFromString(String(xml || ""), "application/xml"); } catch (e) { return bad(`XML parse error: ${e.message}`); }
    const perr = doc.getElementsByTagName("parsererror")[0];
    if (perr || !doc.documentElement) return bad(`XML parse error: ${((perr && perr.textContent) || "").trim().split("\n")[0]}`);
    const tag = String(doc.documentElement.localName || "").toLowerCase();
    const locs = [...doc.getElementsByTagName("*")].filter((el) => el.localName === "loc").map((el) => (el.textContent || "").trim()).filter(Boolean);
    if (tag === "sitemapindex") return { kind: "sitemapindex", urls: [], sitemaps: locs, error: null };
    if (tag === "urlset") return { kind: "urlset", urls: locs, sitemaps: [], error: null };
    return bad(`unexpected root element <${tag}>`);
  }

  // ------------------------------------------------------------------ demo-site fetch + BFS crawl (HttpxCrawler over FixtureTransport)
  const fixtureMeta = () => { try { return JSON.parse((CAF.site.get("/_fixture.json") || {}).html || "{}"); } catch { return {}; } };
  function contentType(path, status) {
    if (status !== 200) return "text/html; charset=utf-8";
    const ext = (String(path).match(/\.([a-z0-9]+)$/i) || [])[1] || "";
    return { html: "text/html", xml: "application/xml", txt: "text/plain", json: "application/json" }[ext.toLowerCase()] + "; charset=utf-8";
  }
  function demoFetch(url) {
    const redirects = [];
    let current = url;
    const meta = fixtureMeta();
    for (let i = 0; i < 9; i++) {
      let u;
      try { u = new URL(current); } catch { return { url, final_url: current, status: 0, headers: {}, html: "", elapsed_ms: 0, redirects, error: "invalid URL" }; }
      if (u.hostname.toLowerCase() !== CAF.DEMO_HOST) return { url, final_url: current, status: 0, headers: {}, html: "", elapsed_ms: 0, redirects, error: "external URL (the hosted console cannot fetch it)" };
      const path = u.pathname || "/";
      const hop = (meta.redirects || {})[path];
      if (hop) { redirects.push([current, Number(hop[1])]); current = new URL(hop[0], current).toString(); continue; }
      const r = CAF.site.get(path);
      const ct = contentType(r.path, r.status);
      return { url, final_url: current, status: r.status, headers: { "content-type": ct }, html: /text|xml|json/.test(ct) ? r.html : "", elapsed_ms: 0, redirects, error: null };
    }
    return { url, final_url: current, status: 0, headers: {}, html: "", elapsed_ms: 0, redirects, error: "too many redirects" };
  }
  const isHtml = (p) => /html/.test((p.headers || {})["content-type"] || "") || /^(<!doctype html|<html)/i.test(String(p.html || "").trimStart());
  const pageOk = (p) => !p.error && p.status >= 200 && p.status < 300;
  function pageLinks(p) {
    const doc = CAF.lib.html.parse(p.html);
    const out = [];
    for (const a of doc.querySelectorAll("a[href]")) {
      let href;
      try { const u = new URL((a.getAttribute("href") || "").trim(), p.final_url); u.hash = ""; href = u.toString(); } catch { continue; }
      if (/^https?:\/\//.test(href)) out.push(href);
    }
    return out;
  }
  function crawlSite(start, maxPages) {
    const host = hostOf(start), seen = new Set(), queue = [start], pages = [];
    while (queue.length && pages.length < maxPages) {
      const url = queue.shift();
      if (seen.has(url)) continue;
      seen.add(url);
      const page = demoFetch(url);
      pages.push(page);
      if (pageOk(page) && isHtml(page)) for (const link of pageLinks(page)) {
        if (hostOf(link) !== host) continue;
        if (!seen.has(link) && !/\.(pdf|png|jpg|svg|css|js|zip)$/.test(link)) queue.push(link);
      }
    }
    return pages;
  }
  function responseRecord(p) {
    const redirects = (p.redirects || []).map(([u, c]) => [normUrl(u), Number(c)]);
    return { url: normUrl(p.url), final_url: p.final_url ? normUrl(p.final_url) : normUrl(p.url), status: Number(p.status || 0), first_status: redirects.length ? redirects[0][1] : Number(p.status || 0),
      redirects, content_type: (p.headers || {})["content-type"] || "", elapsed_ms: Number(p.elapsed_ms || 0), bytes: bytesOf(p.html), error: p.error || null, is_html: p.html ? isHtml(p) : false };
  }

  // ------------------------------------------------------------------ site model + checks (pure)
  function buildSite(startUrl, records, pages, robots, sitemap, mode = "crawl", truncated = false) {
    const inlinks = {}, referenced = {};
    const add = (obj, k, v) => { (obj[k] = obj[k] || []).push(v); };
    for (const [src, f] of Object.entries(pages)) for (const tgt of f.links_internal) if (tgt !== src) { inlinks[tgt] = inlinks[tgt] || new Set(); inlinks[tgt].add(src); }
    for (const [tgt, srcs] of Object.entries(inlinks)) add(referenced, tgt, `linked from ${srcs.size} page(s)`);
    for (const u of sitemap.urls || []) add(referenced, normUrl(u), "listed in sitemap");
    for (const [src, f] of Object.entries(pages)) {
      if (f.canonical && f.canonical !== src) add(referenced, f.canonical, "canonical target");
      for (const h of f.hreflang || []) if (h.href !== src) add(referenced, h.href, "hreflang target");
    }
    return { start_url: startUrl, mode, truncated, records, pages, robots, sitemap,
      inlinks: Object.fromEntries(Object.entries(inlinks).map(([k, v]) => [k, uniqSorted([...v])])),
      referenced: Object.fromEntries(Object.entries(referenced).map(([k, v]) => [k, uniqSorted(v)])) };
  }
  const rpOf = (site) => { const rb = site.robots || {}; return rb.found ? Robots.parse(rb.text) : null; };
  const recordOf = (site, url) => site.records[normUrl(url)] || null;
  const isInternal = (site, url) => !site.start_url || hostOf(url) === hostOf(site.start_url);
  const where = (site, url) => (site.referenced[url] || []).join("; ") || "discovered during crawl";
  function refSources(site, url) {
    const srcs = [...(site.inlinks[url] || [])];
    for (const [s, f] of Object.entries(site.pages)) if (f.canonical === url || (f.hreflang || []).some((h) => h.href === url)) srcs.push(s);
    return uniqSorted(srcs);
  }
  const P = (site) => Object.entries(site.pages);
  const R = (site) => Object.entries(site.records);
  const su = (site, u) => shortUrl(u, site.start_url);
  function indexableRepresentatives(site) {
    return Object.fromEntries(P(site).filter(([u, f]) => !(f.noindex || (f.canonical && f.canonical !== u))));
  }
  const sitemapUrls = (site) => ((site.sitemap || {}).urls || []).map((u) => normUrl(u));
  const CHECKS = {
    missing(site, { field }) { const label = field.replace(/_/g, " "); return P(site).filter(([, f]) => !String(f[field] || "").trim()).map(([u]) => ({ url: u, detail: `No ${label}` })); },
    duplicate(site, { field }) {
      const groups = new Map();
      for (const [u, f] of Object.entries(indexableRepresentatives(site))) {
        const v = String(f[field] || "").split(/\s+/).filter(Boolean).join(" ").toLowerCase();
        if (v) { if (!groups.has(v)) groups.set(v, []); groups.get(v).push(u); }
      }
      const out = [];
      for (const urls of groups.values()) {
        if (urls.length < 2) continue;
        for (const u of urls) {
          const others = urls.filter((o) => o !== u);
          out.push({ url: u, detail: `Same ${field.replace(/_/g, " ")} as ${others.map((o) => su(site, o)).join(", ")}`, evidence: pslice(site.pages[u][field] || "", 200), sources: others });
        }
      }
      return out;
    },
    length(site, params) {
      const field = params.field, lo = parseInt(params.min ?? 0, 10), hi = parseInt(params.max ?? 10000, 10);
      return P(site).map(([u, f]) => [u, String(f[field] || "").trim()]).filter(([, v]) => v && !(lo <= plen(v) && plen(v) <= hi))
        .map(([u, v]) => ({ url: u, detail: `${plen(v)} characters (target ${lo}-${hi})`, evidence: pslice(v, 200) }));
    },
    count_below(site, params) { const field = params.field, lo = parseInt(params.min ?? 1, 10); return P(site).filter(([, f]) => (f[field] || []).length < lo).map(([u, f]) => ({ url: u, detail: `${(f[field] || []).length} ${field.toUpperCase()} element(s)` })); },
    count_above(site, params) {
      const field = params.field, hi = parseInt(params.max ?? 1, 10);
      return P(site).filter(([, f]) => (f[field] || []).length > hi).map(([u, f]) => ({ url: u, detail: `${f[field].length} ${field.toUpperCase()} elements`, evidence: f[field].map((v) => pslice(v, 70)).join(" | ") }));
    },
    heading_skip(site) {
      return P(site).filter(([, f]) => (f.heading_skips || []).length).map(([u, f]) => ({ url: u, detail: f.heading_skips.slice(0, 5).map((s) => `H${s.from} -> H${s.to} ('${s.text}')`).join("; "), evidence: `${f.heading_skips.length} skip(s)` }));
    },
    lang_missing(site) { return P(site).filter(([, f]) => !f.lang).map(([u]) => ({ url: u, detail: "<html> has no lang attribute" })); },
    lang_invalid(site) { return P(site).filter(([, f]) => f.lang && !LANG_RX.test(f.lang)).map(([u, f]) => ({ url: u, detail: `lang="${f.lang}" is not a valid BCP 47 code` })); },
    noindex(site) { return P(site).filter(([, f]) => f.noindex).map(([u, f]) => ({ url: u, detail: "robots directives: " + (f.robots_directives || []).join(", ") })); },
    jsonld_parse_error(site) {
      return P(site).map(([u, f]) => [u, (f.jsonld || []).filter((b) => b.error)]).filter(([, bad]) => bad.length).map(([u, bad]) => ({ url: u, detail: bad.map((b) => `block ${b.index}: ${b.error}`).join("; ") }));
    },
    jsonld_missing_type(site) {
      return P(site).map(([u, f]) => [u, (f.jsonld || []).filter((b) => !b.error && (!(b.types || []).length || !b.has_context))]).filter(([, bad]) => bad.length)
        .map(([u, bad]) => ({ url: u, detail: `${bad.length} block(s) without @context/@type` }));
    },
    image_alt_missing(site) {
      const name = (m) => (/^data:/i.test(m) ? `inline ${(m.match(/^data:([^;,]+)/i) || [])[1] || "image"} (data URI)` : m.split("/").pop());
      return P(site).filter(([, f]) => (f.images_missing_alt || []).length)
        .map(([u, f]) => ({ url: u, detail: `${f.images_missing_alt.length} of ${f.images_total || 0} images have no alt attribute`, evidence: f.images_missing_alt.slice(0, 6).map(name).join(", ") }));
    },
    large_html(site, params) { const limit = Number(params.max_kb ?? 500) * 1024; return P(site).filter(([, f]) => (f.html_bytes || 0) > limit).map(([u, f]) => ({ url: u, detail: `${pyFixed(f.html_bytes / 1024, 0)} KB of HTML` })); },
    status_client_error(site) {
      return R(site).filter(([u, r]) => isInternal(site, u) && r.status >= 400 && r.status < 500).map(([u, r]) => ({ url: u,
        detail: `HTTP ${r.status}${r.redirects.length ? ` (after redirect to ${su(site, r.final_url)})` : ""}`, evidence: where(site, u), sources: refSources(site, u) }));
    },
    status_server_error(site) { return R(site).filter(([u, r]) => isInternal(site, u) && r.status >= 500).map(([u, r]) => ({ url: u, detail: `HTTP ${r.status}`, evidence: where(site, u), sources: refSources(site, u) })); },
    status_redirect(site) {
      return R(site).filter(([u, r]) => isInternal(site, u) && r.redirects.length).map(([u, r]) => ({ url: u, detail: `${r.first_status} -> ${su(site, r.final_url)}`, evidence: where(site, u), sources: refSources(site, u) }));
    },
    redirect_chain(site, params) {
      const maxHops = parseInt(params.max_hops ?? 1, 10);
      return R(site).filter(([, r]) => r.redirects.length > maxHops).map(([u, r]) => ({ url: u, detail: `${r.redirects.length} hops: ${[...r.redirects.map((x) => su(site, x[0])), su(site, r.final_url)].join(" -> ")}` }));
    },
    links_to_redirect(site) {
      return P(site).map(([u, f]) => [u, f.links_internal.filter((t) => ((recordOf(site, t) || {}).redirects || []).length)]).filter(([, t]) => t.length)
        .map(([u, t]) => ({ url: u, detail: "Links to " + t.slice(0, 8).map((x) => `${su(site, x)} (${site.records[x].first_status})`).join(", "), sources: t }));
    },
    broken_internal_links(site) {
      return P(site).map(([u, f]) => [u, f.links_internal.filter((t) => ((recordOf(site, t) || {}).status || 0) >= 400)]).filter(([, b]) => b.length)
        .map(([u, b]) => ({ url: u, detail: "Links to " + b.slice(0, 8).map((x) => `${su(site, x)} (${site.records[x].status})`).join(", "), sources: b }));
    },
    canonical_missing(site) { return P(site).filter(([, f]) => !f.canonical).map(([u]) => ({ url: u, detail: 'No <link rel="canonical">' })); },
    canonical_multiple(site) { return P(site).filter(([, f]) => (f.canonical_count || 0) > 1).map(([u, f]) => ({ url: u, detail: `${f.canonical_count} canonical tags` })); },
    canonical_non_self(site) { return P(site).filter(([u, f]) => f.canonical && f.canonical !== u && !samePage(f.canonical, u)).map(([u, f]) => ({ url: u, detail: `Canonical is ${su(site, f.canonical)}`, sources: [f.canonical] })); },
    canonical_target_status(site) {
      const out = [];
      for (const [u, f] of P(site)) {
        const c = f.canonical, r = c ? recordOf(site, c) : null;
        if (!c || c === u || !r || r.status === 0) continue;
        if (r.redirects.length) out.push({ url: u, detail: `Canonical ${su(site, c)} redirects (${r.first_status}) to ${su(site, r.final_url)}`, sources: [c] });
        else if (r.status >= 400) out.push({ url: u, detail: `Canonical ${su(site, c)} returns ${r.status}`, sources: [c] });
      }
      return out;
    },
    hreflang_target_status(site) {
      const out = [];
      for (const [u, f] of P(site)) {
        const bad = [];
        for (const h of f.hreflang || []) {
          const r = recordOf(site, h.href);
          if (h.href === u || !r || r.status === 0) continue;
          if (r.redirects.length) bad.push(`${h.lang} -> ${su(site, h.href)} redirects (${r.first_status})`);
          else if (r.status !== 200) bad.push(`${h.lang} -> ${su(site, h.href)} returns ${r.status}`);
        }
        if (bad.length) out.push({ url: u, detail: bad.join("; "), sources: (f.hreflang || []).map((h) => h.href) });
      }
      return out;
    },
    hreflang_reciprocity(site) {
      const out = [];
      for (const [u, f] of P(site)) {
        const missing = [];
        for (const h of f.hreflang || []) {
          if (h.href === u || h.lang.toLowerCase() === "x-default") continue;
          const tf = site.pages[h.href];
          if (!tf) continue;
          const back = new Set((tf.hreflang || []).map((x) => x.href));
          if (!back.has(u) && !back.has(f.canonical || u)) missing.push(`${su(site, h.href)} (${h.lang}) has no return tag`);
        }
        if (missing.length) out.push({ url: u, detail: missing.join("; ") });
      }
      return out;
    },
    hreflang_x_default(site) { return P(site).filter(([, f]) => (f.hreflang || []).length && !f.hreflang.some((h) => h.lang.toLowerCase() === "x-default")).map(([u, f]) => ({ url: u, detail: `${f.hreflang.length} hreflang annotation(s), no x-default` })); },
    hreflang_self_reference(site) { return P(site).filter(([u, f]) => (f.hreflang || []).length && !f.hreflang.some((h) => h.href === u || h.href === f.canonical)).map(([u]) => ({ url: u, detail: "hreflang cluster does not include this page" })); },
    hreflang_invalid_code(site) {
      return P(site).map(([u, f]) => [u, (f.hreflang || []).filter((h) => !HREFLANG_RX.test(h.lang)).map((h) => h.lang)]).filter(([, b]) => b.length).map(([u, b]) => ({ url: u, detail: "Invalid code(s): " + b.join(", ") }));
    },
    sitemap_missing(site) { const sm = site.sitemap || {}; return sm.found ? [] : [{ url: site.start_url || "", detail: (sm.errors && sm.errors.length ? sm.errors : ["no sitemap found"]).join("; ") }]; },
    sitemap_non_200(site) {
      const out = [];
      for (const u of sitemapUrls(site)) {
        const r = recordOf(site, u);
        if (!r || r.status === 0) continue;
        if (r.redirects.length) out.push({ url: u, detail: `${r.first_status} redirect to ${su(site, r.final_url)}` });
        else if (r.status !== 200) out.push({ url: u, detail: `HTTP ${r.status}` });
      }
      return out;
    },
    sitemap_non_canonical(site) { return sitemapUrls(site).filter((u) => site.pages[u] && site.pages[u].canonical && site.pages[u].canonical !== u).map((u) => ({ url: u, detail: `Canonical is ${su(site, site.pages[u].canonical)}` })); },
    sitemap_noindex(site) { return sitemapUrls(site).filter((u) => (site.pages[u] || {}).noindex).map((u) => ({ url: u, detail: "Listed in sitemap but noindex" })); },
    sitemap_orphan(site) {
      if (site.truncated) return [];
      const start = site.start_url ? normUrl(site.start_url) : null;
      return sitemapUrls(site).filter((u) => u !== start && site.pages[u] && !(site.inlinks[u] || []).length).map((u) => ({ url: u, detail: "No internal links point to this page" }));
    },
    sitemap_blocked(site) { const rp = rpOf(site); return rp ? sitemapUrls(site).filter((u) => !Robots.canFetch(rp, "Googlebot", u)).map((u) => ({ url: u, detail: "Disallowed for Googlebot by robots.txt" })) : []; },
    robots_missing(site) { const rb = site.robots || {}; return rb.found ? [] : [{ url: rb.url || "", detail: `robots.txt returned ${rb.status ?? "no response"}` }]; },
    robots_blocks_all(site, params) {
      const rp = rpOf(site), start = site.start_url;
      if (!rp || !start) return [];
      const blocked = (params.user_agents || ["*"]).filter((ua) => !Robots.canFetch(rp, ua, start));
      return blocked.length ? [{ url: site.robots.url, detail: "Homepage disallowed for: " + blocked.join(", ") }] : [];
    },
    robots_no_sitemap(site) { const rb = site.robots || {}; return rb.found && !(rb.sitemaps || []).length ? [{ url: rb.url || "", detail: "No Sitemap: line" }] : []; },
    robots_blocks_resources(site) {
      const rp = rpOf(site);
      if (!rp) return [];
      const res = uniqSorted(Object.values(site.pages).flatMap((f) => f.resources || []).filter((r) => /\.(css|js)$/.test(r) || r.includes(".css?") || r.includes(".js?")));
      const blocked = res.filter((r) => isInternal(site, r) && !Robots.canFetch(rp, "Googlebot", r));
      return blocked.length ? [{ url: site.robots.url, detail: `${blocked.length} CSS/JS file(s) disallowed`, evidence: blocked.slice(0, 6).map((b) => su(site, b)).join(", ") }] : [];
    },
    slow_response(site, params) {
      const limit = Number(params.max_ms ?? 800);
      return Object.keys(site.pages).filter((u) => ((site.records[u] || {}).elapsed_ms || 0) > limit).map((u) => ({ url: u, detail: `${pyFixed(site.records[u].elapsed_ms, 0)} ms` }));
    },
  };
  const PAGE_ONLY = new Set(["missing", "duplicate", "length", "count_below", "count_above", "heading_skip", "lang_missing", "lang_invalid", "noindex", "jsonld_parse_error",
    "jsonld_missing_type", "image_alt_missing", "large_html", "canonical_missing", "canonical_multiple", "canonical_non_self", "hreflang_x_default", "hreflang_self_reference", "hreflang_invalid_code"]);

  function selectRules(rules, only) {
    const active = (rules || []).filter((r) => r.enabled !== false);
    if (!only || !only.length) return active;
    const wanted = new Set(only.map((o) => String(o).toLowerCase()));
    return active.filter((r) => wanted.has(String(r.id).toLowerCase()) || wanted.has(String(r.category || "").toLowerCase()));
  }
  function evaluateRules(site, rules) {
    const issues = [], results = [];
    const variants = new Set(P(site).filter(([u, f]) => f.canonical && f.canonical !== u && site.pages[f.canonical]).map(([u]) => u));
    const repSite = { ...site, pages: Object.fromEntries(P(site).filter(([u]) => !variants.has(u))) };
    for (const rule of rules) {
      const check = rule.check, fn = CHECKS[check];
      if (!fn) { results.push({ rule, status: "error", issues: 0, note: `unknown check '${check}'` }); continue; }
      if (site.mode === "html" && !PAGE_ONLY.has(check)) { results.push({ rule, status: "skipped", issues: 0, note: "needs a crawl (pasted HTML only)" }); continue; }
      if (check === "sitemap_orphan" && site.truncated) { results.push({ rule, status: "skipped", issues: 0, note: "crawl hit max_pages - orphan check inconclusive" }); continue; }
      const target = String(check).startsWith("canonical_") || rule.scope !== "page" ? site : repSite;
      const found = fn(target, rule.params || {});
      for (const f of found) issues.push({ rule_id: rule.id, title: rule.title, category: rule.category || "", severity: rule.severity || "medium", url: f.url || "", detail: f.detail || "", evidence: f.evidence || "", sources: f.sources || [] });
      results.push({ rule, status: "ran", issues: found.length, note: "" });
    }
    return [issues, results];
  }

  // ------------------------------------------------------------------ prioritisation (pure)
  function trafficByPage(rows, base) {
    const agg = {};
    for (const r of rows) {
      const page = String(r.page ?? "").trim();
      if (!page) continue;
      const url = base ? normUrl(page, base) : normUrl(page);
      agg[url] = agg[url] || { clicks: 0, impressions: 0 };
      agg[url].clicks += Number(r.clicks || 0);
      agg[url].impressions += Number(r.impressions || 0);
    }
    return agg;
  }
  function pageImportance(site, traffic) {
    if (traffic && Object.keys(traffic).length) {
      const units = Object.fromEntries(Object.entries(traffic).map(([u, t]) => [u, t.clicks + t.impressions / 50]));
      const top = Math.max(0, ...Object.values(units));
      if (top > 0) return [Object.fromEntries(Object.entries(units).map(([u, v]) => [u, Math.log1p(v) / Math.log1p(top)])), "search_console"];
    }
    const counts = Object.fromEntries(Object.entries(site.inlinks).map(([u, v]) => [u, v.length]));
    if (site.start_url) counts[normUrl(site.start_url)] = Math.max(1, ...Object.values(counts)) || 1;
    const top = Math.max(0, ...Object.values(counts));
    if (top <= 0) return [{}, "none"];
    return [Object.fromEntries(Object.entries(counts).map(([u, c]) => [u, Math.log1p(c) / Math.log1p(top)])), "internal_links"];
  }
  function bucket(score, th) { if (score >= Number((th || {}).P1 ?? 9)) return "P1"; if (score >= Number((th || {}).P2 ?? 4)) return "P2"; return "P3"; }
  function prioritise(issues, importance, cfg, siteScope) {
    const weights = cfg.severity_weights || { critical: 10, high: 6, medium: 3, low: 1 };
    const tw = Number(cfg.traffic_weight ?? 1.5), th = cfg.priority_thresholds || { P1: 9, P2: 4 };
    return issues.map((i) => {
      const imp = siteScope.has(i.rule_id) ? 1 : Math.max(importance[i.url] || 0, ...(i.sources || []).map((s) => importance[s] || 0));
      const score = pyRound((weights[i.severity] ?? 3) * (1 + tw * imp), 2);
      return { ...i, importance: pyRound(imp, 3), priority_score: score, priority: bucket(score, th) };
    });
  }
  function groupIssues(issues, rules, cfg) {
    const bonus = Number(cfg.breadth_bonus_per_doubling ?? 0.5);
    const byRule = new Map();
    for (const i of issues) { if (!byRule.has(i.rule_id)) byRule.set(i.rule_id, []); byRule.get(i.rule_id).push(i); }
    const groups = [];
    for (const [rid, items] of byRule) {
      const rule = rules[rid] || {};
      const top = Math.max(...items.map((x) => x.priority_score));
      const score = pyRound(top + bonus * Math.log2(items.length), 2);
      const ordered = [...items].sort((a, b) => b.priority_score - a.priority_score);
      groups.push({ rule_id: rid, title: rule.title || rid, category: rule.category || "", severity: rule.severity || "medium", priority_score: score,
        priority: bucket(score, cfg.priority_thresholds || {}), count: items.length, urls: ordered.map((x) => x.url), examples: ordered.slice(0, 3).map((x) => x.detail),
        why_it_matters: rule.why || "", recommendation: rule.recommendation || "", fix_steps: rule.recommendation ? [rule.recommendation] : [],
        effort: rule.effort || "medium", owner_role: rule.owner || "" });
    }
    groups.sort((a, b) => b.priority_score - a.priority_score || (SEV_ORDER[a.severity] ?? 9) - (SEV_ORDER[b.severity] ?? 9) || cmp(a.rule_id, b.rule_id));
    return groups;
  }
  function deterministicSummary(start, pages, groups, source) {
    if (!groups.length) return `No technical SEO issues were found on the ${pages} page(s) audited from ${start || "the supplied HTML"}.`;
    const p = Object.fromEntries(["P1", "P2", "P3"].map((k) => [k, groups.filter((g) => g.priority === k).length]));
    const total = groups.reduce((s, g) => s + g.count, 0);
    const top = groups.slice(0, 3).map((g) => `${g.title} (${g.count})`).join("; ");
    const basis = IMPORTANCE_LABEL[source] || "severity";
    return `The audit of ${pages} page(s) from ${start || "the supplied HTML"} found ${total} issue(s) across ${groups.length} rule(s): ` +
      `${p.P1} P1, ${p.P2} P2 and ${p.P3} P3 groups, prioritised by severity and ${basis}. Fix first: ${top}.`;
  }
  /** Merge model explanations onto the computed groups by rule_id; never add or drop findings. */
  function reconcileExplanation(groups, expl, useModel) {
    const known = groups.map((g) => g.rule_id);
    let used = false, order = [];
    if (expl && useModel) {
      const byId = {};
      for (const e of expl.explanations || []) if (known.includes(e.rule_id)) byId[e.rule_id] = e;
      for (const g of groups) {
        const e = byId[g.rule_id];
        if (!e) continue;
        used = true;
        if (String(e.why_it_matters || "").trim()) g.why_it_matters = e.why_it_matters.trim();
        const steps = (e.fix_steps || []).map((s) => String(s || "").trim()).filter(Boolean);
        if (steps.length) g.fix_steps = steps;
        g.effort = e.effort || g.effort;
        if (String(e.owner_role || "").trim()) g.owner_role = e.owner_role.trim();
      }
      order = [...new Set(expl.fix_order || [])].filter((r) => known.includes(r));
    }
    order.push(...known.filter((r) => !order.includes(r)));
    return [groups, order, used];
  }

  // ------------------------------------------------------------------ HTML report (same layout as REPORT_TEMPLATE)
  const E = U.esc;
  function renderReport(c) {
    const pill = (p) => `<span class="pill ${E(p)}">${E(p)}</span>`;
    const fixes = c.groups.map((g, i) => `<tr><td>${i + 1}</td><td>${pill(g.priority)} <span class="sev">${E(g.severity)}</span></td>
<td><b>${E(g.title)}</b><br><span class="muted">${E(g.why_it_matters)}</span></td><td>${g.count}</td><td>${E(g.effort)}</td><td>${E(g.owner_role)}</td></tr>`).join("\n");
    const details = c.groups.map((g, i) => `<details${i < 3 ? " open" : ""}><summary>${pill(g.priority)} ${E(g.title)} <span class="muted">(${g.count}) &middot; <code>${E(g.rule_id)}</code></span></summary>
<p><b>How to fix</b></p><ul class="steps">${g.fix_steps.map((s) => `<li>${E(s)}</li>`).join("")}</ul>
<table><thead><tr><th>URL</th><th>Detail</th><th>Evidence</th></tr></thead><tbody>
${g.rows.map((r) => `<tr><td class="url">${E(r.url)}</td><td>${E(r.detail)}</td><td class="muted">${E(r.evidence)}</td></tr>`).join("")}
</tbody></table></details>`).join("\n");
    const crawl = c.crawl.map((r) => `<tr><td class="url">${E(r.url)}${r.redirect_hops ? `<br><span class="muted">&rarr; ${E(r.final_url)}</span>` : ""}</td><td>${r.status}</td><td>${E(r.title || "")}</td><td>${r.in_sitemap ? "yes" : ""}</td><td>${r.inlinks}</td><td>${E(r.clicks_label)}</td><td>${r.issues}</td></tr>`).join("");
    const rules = c.rule_results.map((r) => `<tr><td><code>${E(r.id)}</code> ${E(r.title)}</td><td>${E(r.category)}</td><td>${E(r.severity)}</td><td>${r.status === "skipped" ? `<span class="muted">skipped - ${E(r.note)}</span>` : r.issues ? `${r.issues} issue(s)` : '<span class="ok">passed</span>'}</td></tr>`).join("");
    return `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Technical SEO audit - ${E(c.site)}</title>
<style>
:root{--ink:#1d2433;--muted:#5b6475;--line:#e3e6ec;--bg:#f6f7f9;--card:#fff;--p1:#b42318;--p2:#b54708;--p3:#475467;--ok:#067647;--accent:#1f4e8c}
*{box-sizing:border-box}body{margin:0;font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:var(--ink);background:var(--bg)}
header{background:var(--accent);color:#fff;padding:28px 32px}header h1{margin:0 0 4px;font-size:24px}header p{margin:0;opacity:.85}
main{max-width:1180px;margin:0 auto;padding:24px 20px 48px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:-44px 0 24px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}.tile b{display:block;font-size:26px}.tile span{color:var(--muted);font-size:13px}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:20px 22px;margin-bottom:18px}
h2{font-size:18px;margin:0 0 12px}table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.03em}
.pill{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;font-weight:600;color:#fff}.P1{background:var(--p1)}.P2{background:var(--p2)}.P3{background:var(--p3)}
.sev{font-size:12px;color:var(--muted)}code,.url{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12.5px;word-break:break-all}
details{border-top:1px solid var(--line);padding:10px 0}summary{cursor:pointer;font-weight:600}ul.steps{margin:6px 0 0 18px;padding:0}
.muted{color:var(--muted)}.ok{color:var(--ok)}.note{font-size:13px;color:var(--muted)}
</style></head><body>
<header><h1>Technical SEO audit</h1><p>${E(c.site)} &middot; ${E(c.generated)} &middot; rule pack v${E(c.rule_pack_version)}</p></header>
<main>
<div class="tiles">
<div class="tile"><b>${c.pages_analysed}</b><span>HTML pages analysed</span></div>
<div class="tile"><b>${c.urls_fetched}</b><span>URLs fetched</span></div>
<div class="tile"><b>${c.issue_count}</b><span>issues</span></div>
<div class="tile"><b style="color:var(--p1)">${c.p1}</b><span>P1 rule groups</span></div>
<div class="tile"><b style="color:var(--p2)">${c.p2}</b><span>P2 rule groups</span></div>
<div class="tile"><b class="ok">${c.rules_passed}</b><span>rules passed</span></div>
</div>
<section><h2>Executive summary</h2><p>${E(c.executive_summary)}</p>
<p class="note">Detection: deterministic crawler + YAML rule pack. Prioritisation: severity x page importance from ${E(c.importance_label)}. Explanations: ${E(c.explanation_label)}.</p></section>
<section><h2>Prioritised fixes</h2>
<table><thead><tr><th>#</th><th>Priority</th><th>Issue</th><th>URLs</th><th>Effort</th><th>Owner</th></tr></thead><tbody>
${fixes}</tbody></table></section>
<section><h2>Issue details</h2>
${details}${c.groups.length ? "" : '<p class="ok">No issues found.</p>'}</section>
<section><h2>Crawl inventory</h2><table><thead><tr><th>URL</th><th>Status</th><th>Title</th><th>In sitemap</th><th>In-links</th><th>Clicks</th><th>Issues</th></tr></thead><tbody>
${crawl}
</tbody></table></section>
<section><h2>Robots and sitemap</h2>
<p><b>robots.txt:</b> ${c.robots.found ? "found" : "not found"} ${c.robots.url ? `<span class="url">(${E(c.robots.url)})</span>` : ""}${c.robots.sitemaps.length ? `<br>Sitemap directives: ${E(c.robots.sitemaps.join(", "))}` : ""}</p>
<p><b>Sitemap:</b> ${c.sitemap.url_count} URL(s) from ${E(c.sitemap.sources.join(", ") || "none")}${c.sitemap.errors.length ? `<br><span class="muted">${E(c.sitemap.errors.join("; "))}</span>` : ""}</p></section>
<section><h2>Rules evaluated</h2><table><thead><tr><th>Rule</th><th>Category</th><th>Severity</th><th>Result</th></tr></thead><tbody>
${rules}
</tbody></table></section>
</main></body></html>`;
  }

  // ------------------------------------------------------------------ stages
  async function stageCrawl(input, ctx) {
    const htmlInput = input.html && String(input.html).trim();
    let start = input.start_url ? normUrl(input.start_url) : null;
    if (start && !CAF.site.isDemo(start)) {
      if (!htmlInput) {
        ctx.page({ url: start });  // marks the run degraded with the console's standard reason
        ctx.warn(`Could not fetch ${start}: the hosted console only crawls the embedded demo site. Paste the page HTML into the html field (page-level rules) or run the Python service for a full crawl.`);
        ctx.degraded("start URL could not be fetched");
        return { site: buildSite(start, {}, {}, { found: false, url: null }, { found: false, urls: [], errors: ["not fetched (hosted console)"] }, "crawl"), unreachable: true };
      }
      ctx.warn(`${start} is not on the embedded demo site, so the pasted HTML was audited instead of a crawl.`);
      start = null;
    }
    if (htmlInput && !start) {
      const url = normUrl(input.page_url || input.start_url || PLACEHOLDER);
      const facts = { [url]: extractPageFacts(url, input.html) };
      const records = { [url]: { url, final_url: url, status: 200, first_status: 200, redirects: [], content_type: "text/html", elapsed_ms: 0, bytes: facts[url].html_bytes, error: null, is_html: true } };
      ctx.warn("Pasted HTML: only page-level rules ran; status, redirect, link, robots and sitemap rules need a crawl.");
      ctx.degraded("html input: site-level checks skipped");
      return { site: buildSite(input.page_url || input.start_url || null, records, facts, { found: false, url: null }, { found: false, urls: [], errors: ["not fetched (pasted HTML)"] }, "html") };
    }
    const maxPages = Number(input.max_pages || 50);
    const root = `${new URL(start).protocol}//${new URL(start).host}`;
    ctx.progress(`Crawling ${start} (demo site, max ${maxPages} pages)`);
    const pages = crawlSite(start, maxPages);
    const records = {}, facts = {}, htmlByUrl = {};
    for (const p of pages) {
      const rec = responseRecord(p);
      if (!records[rec.url]) records[rec.url] = rec;
      if (!rec.redirects.length && rec.status === 200 && p.html && isHtml(p) && !facts[rec.url]) { facts[rec.url] = extractPageFacts(rec.url, p.html, p.headers); htmlByUrl[rec.url] = p.html; }
    }
    const truncated = pages.length >= maxPages;
    if (!pages.length || Object.values(records).every((r) => r.status === 0)) {
      ctx.warn(`Could not fetch ${start}: ${(Object.values(records).find((r) => r.error) || {}).error || "no response"}`);
      ctx.degraded("start URL could not be fetched");
    }
    const rb = demoFetch(`${root}/robots.txt`);
    const rbFound = rb.status === 200 && !!rb.html;
    const rp = rbFound ? Robots.parse(rb.html) : null;
    const robots = { url: `${root}/robots.txt`, found: rbFound, status: rb.status, text: rbFound ? rb.html : null, sitemaps: rp ? Robots.siteMaps(rp) || [] : [] };
    const queue = [...new Set(robots.sitemaps.length ? robots.sitemaps : [`${root}/sitemap.xml`])].slice(0, 10);
    const smUrls = [], smErrors = [], fetchedSources = [], seenSm = new Set();
    while (queue.length && seenSm.size < 20) {
      const sm = queue.shift();
      if (seenSm.has(sm)) continue;
      seenSm.add(sm);
      const pg = demoFetch(sm);
      if (pg.status !== 200 || !pg.html) { smErrors.push(`${sm} returned ${pg.status || pg.error}`); continue; }
      const parsed = parseSitemap(pg.html);
      if (parsed.error) { smErrors.push(`${sm}: ${parsed.error}`); continue; }
      fetchedSources.push(sm);
      smUrls.push(...parsed.urls);
      queue.push(...parsed.sitemaps);
    }
    const sitemap = { found: fetchedSources.length > 0, sources: fetchedSources, urls: [...new Set(smUrls)], errors: smErrors };
    const targets = [];
    for (const f of Object.values(facts)) { targets.push(...f.links_internal); if (f.canonical) targets.push(f.canonical); targets.push(...(f.hreflang || []).map((h) => h.href)); }
    const sitemapNorm = uniqSorted(sitemap.urls.map((u) => normUrl(u)));
    targets.push(...sitemapNorm);
    const pending = [...new Set(targets)].filter((t) => !records[t] && /^https?:\/\//.test(t));
    const budget = Number(input.max_link_checks ?? 150);
    for (const t of pending.slice(0, budget)) {
      const p = demoFetch(t);
      const rec = { ...responseRecord(p), url: t };
      records[t] = rec;
      if (!rec.redirects.length && rec.status === 200 && p.html && isHtml(p) && hostOf(t) === hostOf(start) && sitemapNorm.includes(t) && !facts[t]) { facts[t] = extractPageFacts(t, p.html, p.headers); htmlByUrl[t] = p.html; }
    }
    if (pending.length > budget) ctx.warn(`${pending.length - budget} link/canonical/hreflang/sitemap target(s) were not verified (max_link_checks=${budget}).`);
    const unverified = Object.entries(records).filter(([, r]) => r.status === 0).map(([u]) => u);
    if (unverified.length) ctx.warn(`${unverified.length} URL(s) could not be fetched (network/timeout) and were not judged: ${unverified.slice(0, 5).join(", ")}`);
    if (truncated) ctx.warn(`Crawl stopped at max_pages=${input.max_pages}; raise it for a complete audit.`);
    if (input.render_js) await renderPages(facts, htmlByUrl, Number(input.max_render || 20), ctx);
    return { site: buildSite(start, records, facts, robots, sitemap, "crawl", truncated) };
  }
  /** render_js: re-extract facts from the DOM after the page's own scripts ran (same-origin iframe). */
  async function renderPages(facts, htmlByUrl, maxRender, ctx) {
    let rendered = 0;
    const failures = [];
    for (const u of Object.keys(facts).slice(0, maxRender)) {
      let frame = null;
      try {
        frame = await CAF.lib.html.renderInFrame(htmlByUrl[u]);
        await new Promise((r) => setTimeout(r, 50));
        const doctype = /^\s*<!doctype/i.test(htmlByUrl[u]) ? "<!DOCTYPE html>" : "";
        facts[u] = extractPageFacts(u, doctype + frame.doc.documentElement.outerHTML, {});
        rendered++;
      } catch (e) {
        failures.push(`${u} (${(e && e.name) || "Error"})`);
        if (!rendered) break;
      } finally { if (frame) frame.remove(); }
    }
    if (failures.length && !rendered) { ctx.warn(`JS rendering unavailable (${failures[0]}); rules ran on the static HTML.`); ctx.degraded("browser unavailable: JavaScript rendering skipped"); }
    else if (failures.length) ctx.warn(`JS rendering failed for ${failures.length} page(s) (static HTML used): ${failures.slice(0, 3).join(", ")}`);
    ctx.progress(`Rendered ${rendered} page(s)`);
  }

  CAF.agent(AGENT, {
    async run(input, ctx) {
      if (!input.start_url && !(input.html && String(input.html).trim())) throw new Error("Provide start_url (crawl) or html (single page).");
      // 1. crawl
      const { site, unreachable } = await stageCrawl(input, ctx);
      const start = site.start_url;
      // 2. evaluate
      ctx.progress("Evaluating the seo_rules pack");
      const pack = ctx.skill("seo_rules");
      const rules = selectRules(pack.rules || [], input.rules || []);
      let [issues, results] = unreachable ? [[], rules.map((rule) => ({ rule, status: "skipped", issues: 0, note: "page not available in the hosted console" }))] : evaluateRules(site, rules);
      for (const r of results) {
        if (r.status === "ran" && r.issues === 0) ctx.check({ rule: r.rule.id, status: "pass", severity: r.rule.severity || "medium", message: `${r.rule.title}: no issues (${Object.keys(site.pages).length} page(s) checked).` });
        else if (r.status !== "ran") ctx.check({ rule: r.rule.id, status: "info", severity: "low", message: `${r.rule.title}: ${r.status} - ${r.note}` });
      }
      const ruleResults = results.map((r) => ({ id: r.rule.id, title: r.rule.title, category: r.rule.category || "", severity: r.rule.severity || "medium", status: r.status, issues: r.issues, note: r.note }));
      const cfg = Object.fromEntries(["severity_weights", "traffic_weight", "breadth_bonus_per_doubling", "priority_thresholds"].filter((k) => pack[k] !== undefined && pack[k] !== null).map((k) => [k, pack[k]]));
      const rulesById = Object.fromEntries(rules.map((r) => [r.id, r]));
      // 3. prioritise
      let traffic = {};
      if (input.use_traffic_data !== false && site.mode === "crawl" && !unreachable) {
        let reason = null;
        try {
          // keyword_data:csv twin - the same Search Console export the Python connector reads
          if (!ctx.hasData("keywords/gsc_queries")) throw Object.assign(new Error("no export"), { name: "FileNotFoundError" });
          const rows = await ctx.data("keywords/gsc_queries");
          traffic = rows.length ? trafficByPage(rows, start) : {};
          traffic = Object.fromEntries(Object.entries(traffic).filter(([u]) => hostOf(u) === hostOf(start)));
          if (!Object.keys(traffic).length) reason = "no first-party search data for this site: importance from internal links";
        } catch (e) {
          reason = `keyword_data unavailable (${(e && e.name) || "Error"}): importance from internal links`;
        }
        if (reason) { ctx.degraded(reason); ctx.warn("Priorities use internal in-link share as an estimate of page importance (no search traffic data)."); }
      }
      const [importance, source] = pageImportance(site, traffic);
      const siteScope = new Set(Object.entries(rulesById).filter(([, r]) => r.scope === "site").map(([id]) => id));
      issues = prioritise(issues, importance, cfg, siteScope);
      let groups = groupIssues(issues, rulesById, cfg);
      // 4. explain (model explains and orders; never detects)
      let summary = deterministicSummary(start, Object.keys(site.pages).length, groups, source);
      let expl = null;
      if (groups.length) {
        ctx.progress(`Explaining ${groups.length} finding group(s)`);
        const promptGroups = groups.slice(0, 25).map((g) => ({ rule_id: g.rule_id, title: g.title, severity: g.severity, priority: g.priority, count: g.count,
          examples: g.urls.slice(0, g.examples.length).map((u, i) => `${shortUrl(u, start)}: ${g.examples[i]}`), recommendation: g.recommendation }));
        expl = await ctx.generate("explain", { site: start || "the supplied page", pages_crawled: Object.keys(site.records).length, issue_count: groups.reduce((s, g) => s + g.count, 0),
          group_count: groups.length, groups: promptGroups, importance_source: { search_console: "first-party search clicks and impressions", internal_links: "internal in-link share" }[source] || "severity only" });
      }
      const useModel = ctx.llmLive;
      let order, used;
      [groups, order, used] = reconcileExplanation(groups, expl, useModel);
      if (expl && useModel && String(expl.executive_summary || "").trim()) summary = expl.executive_summary.trim();
      const explanationSource = used ? "model" : "rule_pack";
      // 5. report
      for (const i of [...issues].sort((a, b) => b.priority_score - a.priority_score))
        ctx.check({ rule: i.rule_id, status: ["critical", "high"].includes(i.severity) ? "fail" : "warn", severity: i.severity, message: `${i.title}: ${i.detail}`, target: i.url, data: { priority: i.priority, score: i.priority_score } });
      const sitemapSet = new Set((site.sitemap.urls || []).map((u) => normUrl(u)));
      const perUrl = {};
      for (const i of issues) perUrl[i.url] = (perUrl[i.url] || 0) + 1;
      const crawl = Object.entries(site.records).map(([u, r]) => {
        const t = traffic[u];
        return { url: u, status: r.first_status, final_url: r.redirects.length ? r.final_url : null, redirect_hops: r.redirects.length, title: (site.pages[u] || {}).title || null,
          in_sitemap: sitemapSet.has(u), inlinks: (site.inlinks[u] || []).length, issues: perUrl[u] || 0, clicks: t ? t.clicks : null, impressions: t ? t.impressions : null };
      });
      const rp = site.robots.found ? Robots.parse(site.robots.text) : null;
      const blocked = ["*", "Googlebot", "Bingbot", "GPTBot", "CCBot", "ClaudeBot", "PerplexityBot"].filter((ua) => rp && start && !Robots.canFetch(rp, ua, start));
      const robots = { url: site.robots.url || null, found: !!site.robots.found, status: site.robots.status ?? null, sitemaps: site.robots.sitemaps || [], blocked_user_agents: blocked };
      const sitemap = { found: !!site.sitemap.found, sources: site.sitemap.sources || [], url_count: (site.sitemap.urls || []).length, errors: site.sitemap.errors || [] };
      const findings = groups.map((g) => ({ title: `${g.title} (${g.count} URL${g.count !== 1 ? "s" : ""})`, detail: g.why_it_matters, severity: g.severity, category: "seo",
        evidence: g.urls.slice(0, 3).slice(0, g.examples.length).map((u, i) => `${shortUrl(u, start)}: ${g.examples[i]}`).join("; "),
        recommendation: g.fix_steps.slice(0, 3).join(" ") || g.recommendation, target: g.urls[0] || start, source_agent: AGENT }));
      const pcounts = Object.fromEntries(["P1", "P2", "P3"].map((k) => [k, issues.filter((i) => i.priority === k).length]));
      const sortedIssues = [...issues].sort((a, b) => b.priority_score - a.priority_score || cmp(a.rule_id, b.rule_id) || cmp(a.url, b.url));
      const ISSUE_KEYS = ["rule_id", "title", "category", "severity", "url", "detail", "evidence", "priority", "priority_score", "importance"];
      const GROUP_KEYS = ["rule_id", "title", "category", "severity", "priority", "priority_score", "count", "urls", "why_it_matters", "recommendation", "fix_steps", "effort", "owner_role"];
      const pick = (o, keys) => Object.fromEntries(keys.map((k) => [k, o[k]]));
      const rulesEvaluated = ruleResults.filter((r) => r.status === "ran").length, rulesFailed = ruleResults.filter((r) => r.status === "ran" && r.issues).length;
      const output = {
        start_url: start, mode: site.mode, pages_analysed: Object.keys(site.pages).length, urls_fetched: Object.keys(site.records).length, crawl_truncated: !!site.truncated,
        rule_pack_version: String(pack.version ?? ""), rules_evaluated: rulesEvaluated, rules_failed: rulesFailed, rules_skipped: ruleResults.filter((r) => r.status !== "ran").length,
        issue_count: issues.length, priority_counts: pcounts, issues: sortedIssues.map((i) => pick(i, ISSUE_KEYS)), groups: groups.map((g) => pick(g, GROUP_KEYS)),
        fix_order: order, executive_summary: summary, explanation_source: explanationSource, importance_source: source, traffic_data_used: source === "search_console",
        crawl, robots, sitemap, findings,
      };
      const csvRows = sortedIssues.map((i) => { const t = traffic[i.url] || {}; return { priority: i.priority, priority_score: pyFloat(i.priority_score), severity: i.severity, rule_id: i.rule_id, rule_title: i.title,
        category: i.category, url: i.url, detail: i.detail, evidence: i.evidence, recommendation: (rulesById[i.rule_id] || {}).recommendation || "", clicks: pyFloat(t.clicks), impressions: pyFloat(t.impressions) }; });
      ctx.artifact({ name: "seo_issues.csv", mime: "text/csv", content: CAF.lib.csv.stringify(csvRows, ["priority", "priority_score", "severity", "rule_id", "rule_title", "category", "url", "detail", "evidence", "recommendation", "clicks", "impressions"]), description: "Every issue with priority, URL and fix" });
      const byRule = {};
      for (const i of sortedIssues) (byRule[i.rule_id] = byRule[i.rule_id] || []).push({ url: shortUrl(i.url, start), detail: i.detail, evidence: i.evidence });
      const html = renderReport({
        site: start || "pasted HTML", generated: new Date().toISOString().slice(0, 16).replace("T", " ") + " UTC", rule_pack_version: output.rule_pack_version,
        pages_analysed: output.pages_analysed, urls_fetched: output.urls_fetched, issue_count: output.issue_count, p1: groups.filter((g) => g.priority === "P1").length,
        p2: groups.filter((g) => g.priority === "P2").length, rules_passed: rulesEvaluated - rulesFailed, executive_summary: summary,
        importance_label: { search_console: "first-party search clicks and impressions", internal_links: "internal in-link share (estimate)" }[source] || "severity only",
        explanation_label: explanationSource === "model" ? "language model, grounded in the findings" : "rule pack text (no model)",
        groups: groups.map((g) => ({ ...g, rows: byRule[g.rule_id] || [] })),
        crawl: crawl.map((c) => ({ ...c, url: shortUrl(c.url, start), final_url: c.final_url ? shortUrl(c.final_url, start) : null, clicks_label: c.clicks === null ? "" : U.fmtNum(c.clicks, 0) })),
        robots, sitemap, rule_results: ruleResults });
      ctx.artifact({ name: "seo_audit.html", mime: "text/html", content: html, description: "Technical SEO audit report" });
      const top = groups.length ? groups[0].title : "none";
      return {
        output,
        summary: `Audited ${output.pages_analysed} page(s) / ${output.urls_fetched} URL(s): ${output.issue_count} issue(s) (${pcounts.P1} P1, ${pcounts.P2} P2, ${pcounts.P3} P3) across ${rulesFailed} rule(s); top: ${top}`,
        view: [
          { type: "kv", title: "Audit", data: { site: start || "pasted HTML", mode: site.mode, pages_analysed: output.pages_analysed, urls_fetched: output.urls_fetched, issues: output.issue_count,
            "P1 / P2 / P3 issues": `${pcounts.P1} / ${pcounts.P2} / ${pcounts.P3}`, rules_passed: `${rulesEvaluated - rulesFailed} of ${rulesEvaluated}`, prioritised_by: IMPORTANCE_LABEL[source] || "severity", explanations: explanationSource } },
          { type: "markdown", title: "Executive summary", markdown: summary },
          { type: "table", title: "Prioritised fixes", rows: groups.map((g, i) => ({ "#": i + 1, priority: g.priority, issue: g.title, rule: g.rule_id, severity: g.severity, urls: g.count, effort: g.effort, owner: g.owner_role })) },
          { type: "table", title: "Issues", note: `${issues.length} issue(s)`, rows: sortedIssues.slice(0, 150).map((i) => ({ priority: i.priority, score: i.priority_score, rule: i.rule_id, url: shortUrl(i.url, start), detail: i.detail })) },
          { type: "table", title: "Crawl inventory", rows: crawl.map((c) => ({ url: shortUrl(c.url, start), status: c.status, title: c.title || "", in_sitemap: c.in_sitemap ? "yes" : "", inlinks: c.inlinks, clicks: c.clicks, issues: c.issues })) },
          { type: "html", title: "Audit report (seo_audit.html)", artifact: "seo_audit.html" },
        ],
      };
    },
  });
})();
