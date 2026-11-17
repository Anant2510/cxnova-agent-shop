/* GEO Recommendations - browser twin of caf/agents/web/geo_recommendations.py.
   Same stages (fetch -> audit -> score -> citation_test -> plan -> report) and the same geo_rules skill:
   AI-crawler access from robots.txt (port of CPython's RFC 9309 robotparser) for the 13 bots in the pack,
   llms.txt, page directives, static performance signals, our own JSON-LD parser with required /
   recommended properties per type, E-E-A-T and citation-readiness rules. Pillar and overall scores are
   computed in code; the model only writes the plan, reconciled so it can never drop, invent or
   under-prioritise a failing check. Pages come from the embedded demo site (robots.txt, llms.txt and
   the homepage too) or from pasted HTML (crawler/performance fetches skipped, degraded like Python).
   PageSpeed Insights needs network access, so the PSI checks are always "na" in the console. */
(() => {
  const AGENT = "geo_recommendations";
  const T = CAF.lib.text;
  const PILLARS = ["crawler_access", "performance", "structured_data", "eeat", "citation_readiness"];
  const PRIORITY_RANK = { P1: 0, P2: 1, P3: 2 };
  const SEV_INDEX = ["low", "medium", "high", "critical"];
  const PLACEHOLDER = "https://page.invalid/";

  // ------------------------------------------------------------------ Python-parity helpers
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
  const pslice = (s, n) => [...String(s ?? "")].slice(0, n).join("");
  const uniqSorted = (arr) => [...new Set(arr)].sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
  const truthy = (v) => v !== null && v !== undefined && v !== false && v !== 0 && v !== "" && !(Array.isArray(v) && !v.length) && !(U.isObj(v) && !Object.keys(v).length);
  /** BeautifulSoup get_text(" ") then " ".join(text.split()). */
  function rawText(el) {
    const out = [];
    const walk = (n) => { for (const c of n.childNodes) { if (c.nodeType === 3) out.push(c.nodeValue); else if (c.nodeType === 1) walk(c); } };
    if (el) walk(el);
    return out.join(" ");
  }
  const squash = (s) => String(s || "").split(/\s+/).filter(Boolean).join(" ");
  const txt = (el) => squash(rawText(el));
  const ancestor = (el, sel) => (el && el.parentElement ? el.parentElement.closest(sel) : null);
  const following = (a, b) => !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
  /** BS4 class_=regex: matched against each class token, then against the whole class string. */
  function classMatches(el, rx) {
    const cls = String(el.getAttribute("class") || "").split(/\s+/).filter(Boolean);
    return cls.some((c) => rx.test(c)) || (cls.length > 0 && rx.test(cls.join(" ")));
  }
  const findByClass = (root, rx) => [...root.querySelectorAll("[class]")].filter((el) => classMatches(el, rx));
  function hostOf(url) { try { return new URL(url).hostname.toLowerCase(); } catch { return ""; } }
  function absUrl(href, base) { try { const u = new URL(String(href || "").trim(), base); u.hash = ""; return u.toString(); } catch { return String(href || "").trim().split("#")[0]; } }
  const pathOf = (url) => { try { return new URL(url).pathname; } catch { return ""; } };

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
      if (path.includes("$")) return null;
      let matcher = null;
      if (path.includes("*")) { const rx = new RegExp(`^(?:${translatePattern(path)})${fullmatch ? "$" : ""}`); matcher = (f) => { const m = rx.exec(f); return m ? m[0].length : -1; }; }
      return {
        allowance,
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
      const rp = { groups: new Map(), sitemaps: [] };
      const addEntry = (e) => {
        for (const a of e.useragents.map((x) => x.toLowerCase())) {
          const prev = rp.groups.get(a);
          rp.groups.set(a, prev ? { useragents: prev.useragents.filter((x) => e.useragents.includes(x)), rulelines: [...prev.rulelines, ...e.rulelines] } : e);
        }
      };
      let state = 0, entry = { useragents: [], rulelines: [] };
      for (let line of String(text || "").split(/\r\n|[\n\r\v\f\x1c-\x1e\x85]/)) {
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
    function canFetch(rp, ua, url) {
      const m = /^[a-zA-Z][a-zA-Z0-9+.-]*:\/\/[^/?#]*/.exec(String(url));
      let rest = m ? String(url).slice(m[0].length) : String(url);
      rest = normalizeUri(rest) || "/";
      if (rest === "/robots.txt") return true;
      let entry = rp.groups.get(String(ua).toLowerCase()) || null;
      if (!entry) for (const e of rp.groups.values()) if (entryApplies(e, ua)) { entry = e; break; }
      if (!entry) entry = rp.groups.get("*") || null;
      return entry ? allowance(entry, rest) : true;
    }
    return { parse, canFetch };
  })();

  // ------------------------------------------------------------------ demo-site fetch (HttpxCrawler.fetch over the fixture transport)
  const fixtureMeta = () => { try { return JSON.parse((CAF.site.get("/_fixture.json") || {}).html || "{}"); } catch { return {}; } };
  function demoFetch(url) {
    const t0 = performance.now();
    const redirects = [];
    let current = url;
    const meta = fixtureMeta();
    for (let i = 0; i < 9; i++) {
      let u;
      try { u = new URL(current); } catch { return { url, final_url: current, status: 0, html: "", elapsed_ms: 0, redirects, error: "invalid URL" }; }
      if (u.hostname.toLowerCase() !== CAF.DEMO_HOST) return { url, final_url: current, status: 0, html: "", elapsed_ms: 0, redirects, error: "external URL (the hosted console cannot fetch it)" };
      const hop = (meta.redirects || {})[u.pathname || "/"];
      if (hop) { redirects.push([current, Number(hop[1])]); current = new URL(hop[0], current).toString(); continue; }
      const r = CAF.site.get(u.pathname || "/");
      return { url, final_url: current, status: r.status, html: r.html, elapsed_ms: Math.round((performance.now() - t0) * 10) / 10, redirects, error: null };
    }
    return { url, final_url: current, status: 0, html: "", elapsed_ms: 0, redirects, error: "too many redirects" };
  }
  const fetchOk = (p) => !p.error && p.status >= 200 && p.status < 300;

  // ------------------------------------------------------------------ JSON-LD (pure)
  function parseJsonld(raw) {
    let text = String(raw || "").trim();
    text = text.replace(/^\s*(<!--|<!\[CDATA\[)/, "").replace(/(-->|\]\]>)\s*$/, "").trim();
    if (!text) return [null, "empty JSON-LD block"];
    try { return [JSON.parse(text), null]; } catch (e) { return [null, String((e && e.message) || e)]; }
  }
  const typesOf = (node) => { const t = node["@type"]; const vals = Array.isArray(t) ? t : t ? [t] : []; return vals.map((v) => String(v).split("/").pop().split(":").pop()); };
  function jsonldEntities(data, source, path = "$", depth = 0) {
    const out = [];
    if (depth > 8) return out;
    if (Array.isArray(data)) { data.forEach((item, i) => out.push(...jsonldEntities(item, source, `${path}[${i}]`, depth + 1))); return out; }
    if (!U.isObj(data)) return out;
    if (Array.isArray(data["@graph"])) out.push(...jsonldEntities(data["@graph"], source, `${path}.@graph`, depth + 1));
    const types = typesOf(data);
    if (types.length) {
      const top = path === "$" || /^\$(\.@graph)?(\[\d+\])?$/.test(path);
      out.push({ type: types[0], types, props: Object.keys(data).filter((k) => !k.startsWith("@")).sort(), node: data, source, path, top });
    }
    for (const [k, v] of Object.entries(data)) {
      if (k.startsWith("@")) continue;
      if (v && typeof v === "object") out.push(...jsonldEntities(v, source, `${path}.${k}`, depth + 1));
    }
    return out;
  }
  const present = (node, prop) => { const v = node[prop]; if (v === null || v === undefined) return false; if (typeof v === "string" || Array.isArray(v) || U.isObj(v)) return truthy(v); return true; };
  function validateEntity(entity, schemaTypes, strictNested) {
    const t = entity.types.find((x) => schemaTypes[x]);
    if (!t) return null;
    const spec = schemaTypes[t], node = entity.node;
    let missingReq = (spec.required || []).filter((p) => !present(node, p));
    let missingRec = (spec.recommended || []).filter((p) => !present(node, p));
    if (!entity.top && !entity.types.some((x) => strictNested.has(x))) { missingRec = [...missingReq, ...missingRec]; missingReq = []; }
    return { type: entity.type, source: entity.source, path: entity.path, missing_required: missingReq, missing_recommended: missingRec };
  }
  function evaluateAiCrawlers(robotsText, url, bots) {
    if (robotsText === null || robotsText === undefined) return bots.map((b) => ({ ...b, allowed: null, rule: "robots.txt not available" }));
    const rp = Robots.parse(robotsText);
    return bots.map((b) => { const ok = Robots.canFetch(rp, b.agent, url); return { ...b, allowed: !!ok, rule: ok ? "allowed" : "disallowed by robots.txt" }; });
  }
  function parseLlmsTxt(text) {
    if (text === null || text === undefined) return { present: false, valid: null, sections: 0, links: 0 };
    const lines = String(text).split(/\r\n|[\n\r]/).filter((l) => l.trim()).map((l) => l.replace(/\s+$/, ""));
    return { present: true, valid: lines.length > 0 && lines[0].startsWith("# "), sections: lines.filter((l) => l.startsWith("## ")).length, links: (String(text).match(/\[[^\]]+\]\([^)]+\)/g) || []).length };
  }

  // ------------------------------------------------------------------ page facts (extract_geo_facts)
  const QUESTION_START = /^(what|how|why|when|where|which|who|whose|can|could|do|does|did|is|are|was|will|should|would|may|must|have|has)\b/i;
  const STAT_SRC = String.raw`(\b\d+(?:\.\d+)?\s?%|\b\d+(?:\.\d+)?\s(?:percent|per cent)\b|(?<![$€£\d,.])\b\d{1,3}(?:,\d{3})+\b|(?<![$€£])\b\d+(?:\.\d+)?\s?(?:million|billion|thousand)\b|\b\d+(?:\.\d+)?\s(?:out of|in)\s\d+\b|\b\d+(?:\.\d+)?\s(?:days|hours|weeks|months|years)\b)`;
  const STAT_RX = new RegExp(STAT_SRC, "i");
  const FIRST_PARTY_RX = /\b(we|our|us)\b/i;
  const SOURCE_RX = /\b(according to|source[sd]?:|survey|study|studies|report(?:ed)? by|data from|research by|per the)\b/i;
  const PHONE_RX = /(\+?\d{1,2}[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}/;
  const SPA_ROOTS = ["#root", "#app", "#__next", "#__nuxt", "[data-reactroot]", "[ng-app]", "[ng-version]"];
  const SUMMARY_CLASS = /(summary|tldr|tl-dr|takeaway|key-points|at-a-glance|lead|intro|excerpt|standfirst)/i;
  const SUMMARY_HEADING = /^(summary|key takeaways|takeaways|tl;?dr|at a glance|in short|in brief|overview|key facts)\b/i;
  const BLOCKS = "p,li,td,figcaption,dd,blockquote";
  const HEADINGS = "h1,h2,h3,h4,h5,h6";

  function isQuestion(text) {
    const t = squash(text);
    return !!t && (t.endsWith("?") || (QUESTION_START.test(t) && t.split(" ").length <= 12));
  }
  function findFocus(doc, focus) {
    if (!focus) return null;
    const f = String(focus).trim();
    try { if ("#.[".includes(f.slice(0, 1)) || /^[a-z]+[#.[]/.test(f)) { const el = doc.querySelector(f); if (el) return el; } } catch { /* not a selector */ }
    const byId = doc.getElementById(f) || doc.getElementById(f.replace(/^#+/, ""));
    if (byId) return byId;
    const want = T.canonical(f);
    for (const h of doc.querySelectorAll(HEADINGS)) if (want && T.canonical(rawText(h)).includes(want)) return ancestor(h, "section,article") || h.parentElement;
    return null;
  }
  function leafBlocks(root) {
    return [...root.querySelectorAll(BLOCKS)].filter((el) => !el.querySelector(BLOCKS) && !ancestor(el, "nav,footer,header"));
  }
  function questionSections(root, doc) {
    const out = [];
    const flow = [...doc.querySelectorAll(`${HEADINGS},${BLOCKS}`)];
    for (const h of root.querySelectorAll("h2,h3,h4,summary")) {
      const q = txt(h);
      if (!isQuestion(q)) continue;
      let answer = "";
      if (h.tagName === "SUMMARY" && h.parentElement && h.parentElement.tagName === "DETAILS") answer = [...txt(h.parentElement)].slice([...q].length).join("").trim();
      else for (const el of flow) {
        if (!following(h, el)) continue;
        if (/^H[1-6]$/.test(el.tagName)) break;
        const t = txt(el);
        if (t) { answer = t; break; }
      }
      out.push({ question: q, answer, answer_words: answer.split(/\s+/).filter(Boolean).length });
    }
    return out;
  }
  function statistics(root, pageHost, base, brand) {
    const stats = [];
    for (const el of leafBlocks(root)) {
      if (el.tagName === "TD") continue;
      const text = txt(el);
      if (!STAT_RX.test(text)) continue;
      const linked = !!el.querySelector("cite") || [...el.querySelectorAll("a[href]")].some((a) => !["", pageHost].includes(hostOf(absUrl(a.getAttribute("href"), base))));
      const sents = T.sentences(text);
      for (const sent of sents.length ? sents : [text]) {
        const found = [...sent.matchAll(new RegExp(STAT_SRC, "gi"))].map((m) => m[1]);
        if (!found.length) continue;
        const firstParty = FIRST_PARTY_RX.test(sent) || (!!brand && T.canonical(sent).includes(T.canonical(brand)));
        stats.push({ text: pslice(sent, 180), values: found.slice(0, 4), sourced: linked || SOURCE_RX.test(sent), first_party: firstParty });
      }
    }
    return stats;
  }
  function pageDates(doc, entities) {
    const published = [], modified = [];
    for (const t of doc.querySelectorAll("time")) if (t.getAttribute("datetime")) published.push(t.getAttribute("datetime"));
    for (const m of doc.querySelectorAll("meta[property]")) {
      const prop = m.getAttribute("property");
      if (/^article:(published|modified)_time$/.test(prop)) (prop.includes("published") ? published : modified).push(m.getAttribute("content") || "");
    }
    for (const e of entities) {
      if (truthy(e.node.datePublished)) published.push(String(e.node.datePublished));
      if (truthy(e.node.dateModified)) modified.push(String(e.node.dateModified));
    }
    return { published: published.filter(Boolean), modified: modified.filter(Boolean) };
  }
  function toDate(value) {
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(value || "").trim());
    if (!m) return null;
    const d = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3]));
    return Number.isNaN(d.getTime()) ? null : d;
  }
  const isoDay = (d) => d.toISOString().slice(0, 10);

  function extractGeoFacts(url, html, { focus = null, brand = null } = {}) {
    const doc = CAF.lib.html.parse(html || "");
    const raw = CAF.lib.html.parse(html || "");
    const pageHost = hostOf(url);
    const head = doc.head || doc;
    // structured data (before scripts are removed)
    let blocks = 0;
    const errors = [], entities = [];
    [...doc.querySelectorAll("script[type]")].filter((s) => /^\s*application\/ld\+json\s*$/i.test(s.getAttribute("type"))).forEach((s, i) => {
      blocks++;
      const [data, err] = parseJsonld(s.textContent);
      if (err) { errors.push(`block ${i + 1}: ${err}`); return; }
      entities.push(...jsonldEntities(data, `JSON-LD block ${i + 1}`));
    });
    const types = entities.map((e) => e.type);
    const microdata = doc.querySelectorAll("[itemscope]").length;
    const ogEl = doc.querySelector('meta[property="og:type"]');
    const ogType = ogEl ? ogEl.getAttribute("content") || "" : "";
    const robotsMeta = [...doc.querySelectorAll("meta[name]")].filter((m) => /^(robots|googlebot|bingbot)$/i.test(m.getAttribute("name"))).map((m) => m.getAttribute("content") || "").join(" ").toLowerCase();
    const directives = uniqSorted(`${robotsMeta} `.split(/[,\s]+/).map((d) => d.trim()).filter(Boolean));
    // content roots (scripts removed, like soup.decompose())
    for (const t of doc.querySelectorAll("script,style,noscript,template")) t.remove();
    const main = doc.querySelector("main") || doc.querySelector("article") || doc.body || doc.documentElement;
    const focusEl = findFocus(doc, focus);
    const root = focusEl || main;
    const mainText = txt(main);
    const spa = SPA_ROOTS.some((sel) => doc.querySelector(sel));
    // E-E-A-T
    const authorNames = [...head.querySelectorAll("meta[name]")].filter((m) => /^author$/i.test(m.getAttribute("name")) && m.getAttribute("content")).map((m) => m.getAttribute("content").trim());
    authorNames.push(...[...doc.querySelectorAll("a[rel]")].filter((a) => String(a.getAttribute("rel")).toLowerCase().split(/\s+/).some((r) => r.includes("author"))).map(txt));
    for (const el of findByClass(doc, /(byline|author)(?!-bio)/i)) if (!String(el.getAttribute("class") || "").toLowerCase().includes("bio")) authorNames.push(pslice(txt(el), 80));
    for (const e of entities) {
      const a = e.node.author;
      for (const item of Array.isArray(a) ? a : [a]) {
        if (U.isObj(item) && truthy(item.name)) authorNames.push(String(item.name));
        else if (typeof item === "string" && item.trim()) authorNames.push(item.trim());
      }
    }
    let bio = findByClass(doc, /(author-bio|about-author|author-box|\bbio\b)/i).length > 0;
    bio = bio || [...doc.querySelectorAll("h2,h3,h4,h5,h6")].some((h) => /^about the author/i.test(rawText(h).trim()));
    bio = bio || entities.some((e) => e.type === "Person" && ["description", "jobTitle", "sameAs"].some((k) => truthy(e.node[k])));
    const dates = pageDates(doc, entities);
    const extLinks = uniqSorted([...main.querySelectorAll("a[href]")].filter((a) => !ancestor(a, "nav,footer,header")).map((a) => absUrl(a.getAttribute("href"), url))
      .filter((u) => u.startsWith("http") && !["", pageHost].includes(hostOf(u))));
    const allLinks = [...doc.querySelectorAll("a[href]")].map((a) => [a.getAttribute("href") || "", txt(a).toLowerCase()]);
    const about = allLinks.some(([h, t]) => /about/i.test(h) || t.startsWith("about"));
    const contact = allLinks.some(([h, t]) => /contact/i.test(h) || t.startsWith("contact"));
    const contactDetails = allLinks.some(([h]) => h.startsWith("tel:") || h.startsWith("mailto:")) || !!doc.querySelector("address") || PHONE_RX.test(rawText(doc.documentElement));
    const ORG = new Set(["Organization", "Corporation", "InsuranceAgency", "LocalBusiness", "FinancialService", "NGO", "EducationalOrganization"]);
    const orgSchema = entities.filter((e) => e.types.some((t) => ORG.has(t)));
    const reviews = entities.some((e) => ["AggregateRating", "Review"].includes(e.type)) || findByClass(doc, /(testimonial|review|quotes)/i).length > 0 || main.querySelectorAll("figure blockquote").length > 0;
    // citation readiness
    const qSections = questionSections(root, doc);
    const lists = [...root.querySelectorAll("ul,ol")].filter((l) => !ancestor(l, "nav,footer,header") && l.querySelectorAll("li").length >= 2).length;
    const tables = root.querySelectorAll("table").length;
    const stats = statistics(root, pageHost, url, brand);
    const detailsQ = [...doc.querySelectorAll("summary")].filter((s) => isQuestion(rawText(s)));
    const faqHeading = [...doc.querySelectorAll(HEADINGS)].some((h) => /(faq|frequently asked|questions)/i.test(rawText(h)));
    const h1 = doc.querySelector("h1");
    let leadWords = 0;
    if (h1) { const nxt = [...doc.querySelectorAll("p,ul,ol")].find((el) => following(h1, el)); leadWords = nxt ? rawText(nxt).split(/\s+/).filter(Boolean).length : 0; }
    const summaryEl = findByClass(main, SUMMARY_CLASS).length > 0 || [...main.querySelectorAll("h2,h3,h4")].some((h) => SUMMARY_HEADING.test(rawText(h).trim()));
    // performance (static)
    const relHas = (l, r) => String(l.getAttribute("rel") || "").toLowerCase().split(/\s+/).includes(r);
    const blocking = [...head.querySelectorAll("link[href]")].filter((l) => relHas(l, "stylesheet") && (l.getAttribute("media") || "all").toLowerCase() !== "print" && !l.hasAttribute("disabled")).map((l) => absUrl(l.getAttribute("href"), url));
    const rawHead = raw.head || raw;
    for (const s of rawHead.querySelectorAll("script[src]")) if (!(s.hasAttribute("async") || s.hasAttribute("defer") || String(s.getAttribute("type") || "").toLowerCase() === "module")) blocking.push(absUrl(s.getAttribute("src"), url));
    const images = [...doc.querySelectorAll("img")];
    const noDims = images.filter((i) => !(i.getAttribute("width") && i.getAttribute("height")) && !String(i.getAttribute("style") || "").includes("aspect-ratio")).map((i) => i.getAttribute("src") || "");
    const scripts = [...raw.querySelectorAll("script[src]")];
    const thirdParty = uniqSorted(scripts.map((s) => hostOf(absUrl(s.getAttribute("src"), url))).filter((h) => !["", pageHost].includes(h)));
    const visible = T.canonical(rawText(doc.documentElement));
    const faqNotVisible = entities.filter((e) => e.type === "Question" && truthy(e.node.name) && !visible.includes(T.canonical(String(e.node.name)))).map((e) => pslice(String(e.node.name || ""), 80));
    return {
      url, https: String(url).toLowerCase().startsWith("https://"), og_type: ogType.toLowerCase(),
      schema: { blocks, errors, types: uniqSorted(types), entities, microdata_items: microdata }, directives,
      main_words: mainText.split(" ").filter(Boolean).length, spa_root: spa, has_article_tag: !!doc.querySelector("article"),
      authors: [...new Set(authorNames)].filter(Boolean), bio, dates, external_sources: extLinks, about_link: about, contact_link: contact, contact_details: contactDetails,
      org_schema_on_page: orgSchema.length > 0, reviews, questions: qSections, lists, tables, stats,
      faq_like: detailsQ.length >= 2 || (faqHeading && qSections.length >= 2) || qSections.length >= 3, faq_not_visible: faqNotVisible,
      h1_count: doc.querySelectorAll("h1").length, h2_count: main.querySelectorAll("h2").length, lead_words: leadWords, summary_element: summaryEl,
      brand_in_main: !!brand && T.canonical(mainText).includes(T.canonical(brand)),
      render_blocking: blocking, images: images.length, images_without_dimensions: noDims, viewport: [...doc.querySelectorAll("meta[name]")].some((m) => /^viewport$/i.test(m.getAttribute("name"))),
      third_party_scripts: thirdParty, scripts: scripts.length, stylesheets: [...head.querySelectorAll("link")].filter((l) => relHas(l, "stylesheet")).length,
      html_bytes: new TextEncoder().encode(String(html || "")).length, focus_found: !focus ? null : !!focusEl,
    };
  }
  function detectPageType(facts, urlKnown = true) {
    const types = new Set(facts.schema.types);
    const any = (arr) => arr.some((t) => types.has(t));
    if (any(["Article", "BlogPosting", "NewsArticle", "TechArticle", "Report"]) || facts.og_type === "article") return "article";
    if (any(["Product", "Service", "FinancialProduct"]) || facts.og_type === "product") return "product";
    if (urlKnown && ["", "/"].includes(pathOf(facts.url))) return "home";
    if (facts.has_article_tag && facts.authors.length) return "article";
    return "page";
  }
  function resolveFocus(focus, pillars) {
    if (!focus) return null;
    const f = T.canonical(focus).replace(/-/g, " ").replace(/_/g, " ");
    for (const [key, p] of Object.entries(pillars)) {
      const k = key.replace(/_/g, " "), lab = T.canonical(p.label);
      if (f === k || f === lab || lab.includes(f) || f.includes(k)) return key;
    }
    const aliases = { schema: "structured_data", markup: "structured_data", robots: "crawler_access", crawler: "crawler_access", speed: "performance", "core web vitals": "performance",
      cwv: "performance", eeat: "eeat", "e e a t": "eeat", trust: "eeat", citation: "citation_readiness", content: "citation_readiness" };
    const hit = Object.entries(aliases).find(([k]) => f.includes(k));
    return hit ? hit[1] : null;
  }

  // ------------------------------------------------------------------ rules -> checks (pure; evaluate_geo)
  const mkCheck = (rule, status, message, evidence = "", suffix = "", severity = null) => ({
    id: rule.id + (suffix ? `:${suffix}` : ""), rule: rule.id, pillar: rule.pillar, title: rule.title, action: rule.action || "", status,
    severity: severity || rule.severity || "medium", weight: Number(rule.weight ?? 1), message, evidence, recommendation: rule.recommendation || "", why: rule.why || "" });
  const band = (v, good, poor) => (v <= good ? "pass" : v <= poor ? "warn" : "fail");
  const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1).toLowerCase() : s);
  const lastSeg = (u) => String(u).split("/").pop();
  function evaluateGeo(facts, env, pack, today) {
    const rules = Object.fromEntries((pack.rules || []).filter((r) => r.enabled !== false).map((r) => [r.id, r]));
    const ptype = facts.page_type;
    const out = [];
    const params = (rid) => (rules[rid] || {}).params || {};
    const add = (rid, status, message, evidence = "", suffix = "", severity = null) => {
      const rule = rules[rid];
      if (!rule) return;
      const scope = rule.applies_to || ["all"];
      if (!(scope.includes("all") || scope.includes(ptype))) { status = "na"; message = `Not applicable to a ${ptype} page.`; }
      out.push(mkCheck(rule, status, message, evidence, suffix, severity));
    };
    // crawler access
    const rb = env.robots;
    if (!rb) add("crawler.robots_txt", "na", "robots.txt not fetched (pasted HTML).");
    else add("crawler.robots_txt", rb.found ? "pass" : "warn", `robots.txt ${rb.found ? "found" : "returned " + String(rb.status)} at ${rb.url}`);
    const sev = pack.bot_severity || {}, st = pack.bot_status_when_blocked || {};
    for (const b of env.bots || []) {
      const rule = rules["crawler.ai_bot"];
      if (!rule) break;
      let status, msg;
      if (b.allowed === null) { status = "na"; msg = `${b.agent}: robots.txt not available`; }
      else if (b.allowed) { status = "pass"; msg = `${b.agent} (${b.vendor}, ${b.purpose}) may crawl this page`; }
      else {
        status = st[b.purpose] || "fail";
        msg = `${b.agent} (${b.vendor}, ${b.purpose}) is blocked by robots.txt`;
        if (b.purpose === "training") msg += " - a content-licensing choice; it does not stop AI search citations";
      }
      out.push(mkCheck({ ...rule, weight: b.weight ?? 1 }, status, msg, b.note || "", b.agent, status !== "pass" ? sev[b.purpose] || rule.severity : "low"));
    }
    const llms = env.llms;
    if (!llms) add("crawler.llms_txt", "na", "llms.txt not checked (pasted HTML).");
    else if (!llms.present) add("crawler.llms_txt", "warn", `No llms.txt at ${env.llms_url}`);
    else add("crawler.llms_txt", llms.valid ? "pass" : "warn", `llms.txt found: ${llms.sections} section(s), ${llms.links} link(s)` + (llms.valid ? "" : "; first line is not an H1 (# Name)"));
    const bad = facts.directives.filter((d) => ["noindex", "nosnippet", "none"].includes(d) || d.startsWith("max-snippet:0"));
    const soft = facts.directives.filter((d) => ["noai", "noimageai"].includes(d));
    add("crawler.ai_meta_directives", bad.length ? "fail" : soft.length ? "warn" : "pass", bad.length || soft.length ? `Directives blocking AI use: ${[...bad, ...soft].join(", ")}` : "No blocking robots directives");
    const minWords = parseInt(params("crawler.server_rendered").min_words ?? 150, 10);
    if (facts.main_words >= minWords) add("crawler.server_rendered", "pass", `${facts.main_words} words of main content in the server HTML`);
    else if (facts.spa_root) add("crawler.server_rendered", "fail", `Only ${facts.main_words} words in the server HTML and a client-side app root was found`);
    else if (facts.scripts >= 3) add("crawler.server_rendered", "warn", `Only ${facts.main_words} words in the server HTML with ${facts.scripts} scripts - content may be rendered client-side`);
    else add("crawler.server_rendered", "pass", `No client-side app shell detected (thin page: ${facts.main_words} words - see content depth)`);
    // performance
    const ttfb = env.ttfb_ms;
    let p = params("perf.ttfb");
    if (ttfb === null || ttfb === undefined) add("perf.ttfb", "na", "Not measured (pasted HTML).");
    else add("perf.ttfb", band(ttfb, p.good_ms ?? 800, p.poor_ms ?? 1800), `${pyFixed(ttfb, 0)} ms fetch time from the audit host (TTFB proxy${env.fixture ? ", local fixture" : ""})`);
    p = params("perf.html_weight");
    const kb = facts.html_bytes / 1024;
    add("perf.html_weight", kb <= (p.warn_kb ?? 150) ? "pass" : kb <= (p.fail_kb ?? 500) ? "warn" : "fail", `${pyFixed(kb, 0)} KB of HTML`);
    p = params("perf.render_blocking");
    const n = facts.render_blocking.length;
    add("perf.render_blocking", n < (p.warn ?? 3) ? "pass" : n < (p.fail ?? 6) ? "warn" : "fail", `${n} render-blocking resource(s) in <head>`, facts.render_blocking.slice(0, 6).map(lastSeg).join(", "));
    const nd = facts.images_without_dimensions;
    const imgName = (s) => (/^data:/i.test(s) ? "inline image (data URI)" : lastSeg(s));
    add("perf.image_dimensions", !nd.length ? "pass" : nd.length <= 2 ? "warn" : "fail", `${nd.length} of ${facts.images} image(s) without width/height`, nd.slice(0, 5).map(imgName).join(", "));
    add("perf.viewport", facts.viewport ? "pass" : "fail", "Viewport meta tag " + (facts.viewport ? "present" : "missing"));
    const psi = env.psi;
    for (const [rid, key, unit] of [["perf.psi_lcp", "lcp_ms", "ms"], ["perf.psi_cls", "cls", ""], ["perf.psi_tbt", "tbt_ms", "ms"]]) {
      const rule = rules[rid];
      if (!rule) continue;
      const val = (psi || {})[key];
      if (val === null || val === undefined) { add(rid, "na", "PageSpeed Insights not run" + (env.psi_note ? ` (${env.psi_note})` : "")); continue; }
      const pp = rule.params || {};
      add(rid, band(Number(val), Number(pp.good_ms ?? pp.good), Number(pp.poor_ms ?? pp.poor)), unit ? `${pyFixed(val, 0)} ${unit}`.trim() : pyFixed(val, 3),
        `source: PageSpeed Insights ${psi.strategy || ""} ${psi.field ? "field" : "lab"} data`);
    }
    // structured data
    const sd = facts.schema;
    add("schema.present", sd.blocks && sd.entities.length ? "pass" : "fail", `${sd.blocks} JSON-LD block(s); types: ${sd.types.join(", ") || "none"}` + (sd.microdata_items ? `; ${sd.microdata_items} microdata item(s)` : ""));
    if (sd.blocks) add("schema.parse_errors", sd.errors.length ? "fail" : "pass", sd.errors.join("; ") || "All JSON-LD blocks parse");
    else add("schema.parse_errors", "na", "No JSON-LD blocks");
    const strict = new Set(pack.strict_nested_types || []);
    const validated = sd.entities.map((e) => validateEntity(e, pack.schema_types || {}, strict)).filter(Boolean);
    const req = validated.filter((v) => v.missing_required.length).map((v) => `${v.type} (${v.path}): ${v.missing_required.join(", ")}`);
    const rec = validated.filter((v) => v.missing_recommended.length).map((v) => `${v.type}: ${v.missing_recommended.join(", ")}`);
    if (validated.length) {
      add("schema.required_properties", req.length ? "fail" : "pass", req.slice(0, 6).join("; ") || `${validated.length} entities have all required properties`);
      add("schema.recommended_properties", rec.length ? "warn" : "pass", rec.slice(0, 6).join("; ") || "Recommended properties present");
    } else {
      add("schema.required_properties", "na", "No recognised schema.org entities");
      add("schema.recommended_properties", "na", "No recognised schema.org entities");
    }
    const types = new Set(sd.types);
    const expect = { article: ["Article", "BlogPosting", "NewsArticle", "TechArticle", "Report"], product: ["Product", "Service", "FinancialProduct"],
      home: ["Organization", "WebSite", "InsuranceAgency", "LocalBusiness", "Corporation"] }[ptype];
    if (!expect) add("schema.main_entity_match", "na", "Generic page: no single expected main entity type");
    else {
      const hit = expect.filter((t) => types.has(t)).sort();
      add("schema.main_entity_match", hit.length ? "pass" : "fail", hit.length ? `${cap(ptype)} page marked up as ${hit.join(", ")}` : `${cap(ptype)} page without ${[...expect].sort().join(" / ")} markup`);
    }
    if (sd.entities.some((e) => e.type === "FAQPage"))
      add("schema.faq_visible", facts.faq_not_visible.length ? "fail" : "pass", facts.faq_not_visible.length ? "Marked-up questions not visible: " + facts.faq_not_visible.slice(0, 4).join("; ") : "Every FAQ question in the markup is visible on the page");
    else add("schema.faq_visible", "na", "No FAQPage markup");
    // E-E-A-T
    add("eeat.author_byline", facts.authors.length ? "pass" : "fail", facts.authors.length ? `Author: ${facts.authors.slice(0, 3).join(", ")}` : "No author byline or author markup");
    add("eeat.author_bio", facts.bio ? "pass" : "warn", facts.bio ? "Author bio/profile found" : "No author bio or profile");
    const d = facts.dates;
    const hasDates = d.published.length || d.modified.length;
    add("eeat.dates", hasDates ? "pass" : "fail", hasDates ? `Published ${uniqSorted(d.published).slice(0, 2).join(", ") || "-"}; modified ${uniqSorted(d.modified).slice(0, 2).join(", ") || "-"}` : "No machine-readable dates");
    const allDates = [...d.modified, ...d.published].map(toDate).filter(Boolean);
    const maxAge = parseInt(params("eeat.freshness").max_age_days ?? 365, 10);
    if (allDates.length) {
      const last = new Date(Math.max(...allDates.map((x) => x.getTime())));
      const age = Math.round((today.getTime() - last.getTime()) / 86400000);
      add("eeat.freshness", age <= maxAge ? "pass" : "warn", `Last dated ${isoDay(last)} (${age} days ago)`);
    } else add("eeat.freshness", facts.page_type === "article" ? "warn" : "na", "No dates to judge freshness");
    const minSrc = parseInt(params("eeat.citations").min_sources ?? 1, 10);
    const hosts = uniqSorted(facts.external_sources.map(hostOf));
    add("eeat.citations", hosts.length >= minSrc ? "pass" : "warn", hosts.length ? `${hosts.length} external source domain(s): ${hosts.slice(0, 4).join(", ")}` : "No external sources linked from the main content");
    add("eeat.about_contact", facts.about_link && facts.contact_link ? "pass" : facts.about_link || facts.contact_link ? "warn" : "fail",
      `About link: ${facts.about_link ? "yes" : "no"}; contact link: ${facts.contact_link ? "yes" : "no"}`);
    if (facts.org_schema_on_page) add("eeat.organization_schema", "pass", "Organization markup on this page");
    else if (env.homepage_org === true) add("eeat.organization_schema", "pass", "Organization markup found on the homepage");
    else if (env.homepage_org === false) add("eeat.organization_schema", "fail", "No Organization markup on this page or the homepage");
    else add("eeat.organization_schema", "warn", "No Organization markup on this page (homepage not checked)");
    add("eeat.contact_details", facts.contact_details ? "pass" : "warn", "Phone, email or address " + (facts.contact_details ? "visible" : "not found"));
    add("eeat.https", facts.https ? "pass" : "fail", facts.https ? "HTTPS" : "Page served over plain HTTP");
    add("eeat.reviews", facts.reviews ? "pass" : "warn", facts.reviews ? "Reviews/testimonials or rating markup found" : "No reviews or testimonials");
    // citation readiness
    const qs = facts.questions;
    p = params("citation.question_headings");
    add("citation.question_headings", qs.length >= (p.min_questions ?? 2) ? "pass" : qs.length ? "warn" : "fail", `${qs.length} question heading(s)`, qs.slice(0, 4).map((q) => q.question).join("; "));
    p = params("citation.concise_answers");
    if (qs.length) {
      const lo = p.min_words ?? 8, hi = p.max_words ?? 90;
      const isGood = (q) => lo <= q.answer_words && q.answer_words <= hi;
      const good = qs.filter(isGood);
      const share = good.length / qs.length;
      add("citation.concise_answers", share >= (p.min_share ?? 0.6) ? "pass" : share >= 0.3 ? "warn" : "fail", `${good.length} of ${qs.length} questions answered in ${lo}-${hi} words`,
        pslice(qs.filter((q) => !isGood(q)).map((q) => `${pslice(q.question, 50)} (${q.answer_words} words)`).join("; "), 300));
    } else add("citation.concise_answers", "na", "No question headings to evaluate");
    add("citation.lists_tables", facts.lists || facts.tables ? "pass" : "warn", `${facts.lists} list(s), ${facts.tables} table(s) in the content`);
    const stats = facts.stats;
    p = params("citation.statistics_sourced");
    if (stats.length) {
      const sourced = stats.filter((s) => s.sourced).length, first = stats.filter((s) => s.first_party && !s.sourced).length;
      const share = (sourced + first) / stats.length;
      add("citation.statistics_sourced", share >= (p.min_sourced_share ?? 0.5) ? "pass" : "warn", `${sourced} of ${stats.length} statistic(s) cite a source, ${first} are attributed first-party data`,
        "Unattributed: " + pslice(stats.filter((s) => !(s.sourced || s.first_party)).map((s) => pslice(s.text, 90)).join("; "), 400));
    } else add("citation.statistics_sourced", "warn", "No specific statistics found - concrete numbers make answers quotable");
    const hasFaqSchema = sd.entities.some((e) => e.type === "FAQPage");
    if (facts.faq_like) add("citation.faq_schema", hasFaqSchema ? "pass" : "warn", hasFaqSchema ? "FAQ content marked up as FAQPage" : "Q&A content without FAQPage markup");
    else add("citation.faq_schema", hasFaqSchema ? "pass" : "na", hasFaqSchema ? "FAQPage markup present" : "No FAQ-style content");
    p = params("citation.summary");
    const leadOk = facts.lead_words >= 15 && facts.lead_words <= (p.max_lead_words ?? 70);
    add("citation.summary", facts.summary_element || leadOk ? "pass" : "warn",
      facts.summary_element ? "Summary/key-takeaways element found" : leadOk ? `Lead paragraph of ${facts.lead_words} words after the H1` : "No summary or concise lead after the H1");
    p = params("citation.heading_structure");
    add("citation.heading_structure", facts.h1_count === 1 && facts.h2_count >= (p.min_h2 ?? 3) ? "pass" : "warn", `${facts.h1_count} H1, ${facts.h2_count} H2 section(s)`);
    p = params("citation.content_depth");
    add("citation.content_depth", facts.main_words >= (p.min_words ?? 300) ? "pass" : "warn", `${facts.main_words} words of main content`);
    if (env.brand) add("citation.entity_named", facts.brand_in_main ? "pass" : "warn", `'${env.brand}' ` + (facts.brand_in_main ? "named" : "not named") + " in the main content");
    else add("citation.entity_named", "na", "No brand configured");
    return out;
  }

  // ------------------------------------------------------------------ scoring + plan (pure)
  function scorePillars(checks, pillars) {
    const credit = { pass: 1, warn: 0.5, fail: 0 };
    return Object.entries(pillars).map(([key, p]) => {
      const mine = checks.filter((c) => c.pillar === key && c.status !== "na");
      const total = mine.reduce((s, c) => s + c.weight, 0);
      const score = total ? pyRound((100 * mine.reduce((s, c) => s + c.weight * credit[c.status], 0)) / total, 1) : null;
      return { key, label: p.label || key, weight: Number(p.weight ?? 0.2), score, assessed: score !== null, passed: mine.filter((c) => c.status === "pass").length,
        warned: mine.filter((c) => c.status === "warn").length, failed: mine.filter((c) => c.status === "fail").length, note: score !== null ? "" : "not assessed (no applicable checks)" };
    });
  }
  function overallScore(pillars) {
    const assessed = pillars.filter((p) => p.assessed);
    const w = assessed.reduce((s, p) => s + p.weight, 0);
    return w ? pyRound(assessed.reduce((s, p) => s + p.score * p.weight, 0) / w, 1) : null;
  }
  function gradeFor(score, grades) {
    if (score === null || score === undefined) return "n/a";
    for (const g of [...grades].sort((a, b) => b.min - a.min)) if (score >= g.min) return g.grade;
    return "F";
  }
  function checkPriority(check, pack) {
    const sw = pack.severity_weights || { critical: 10, high: 6, medium: 3, low: 1 };
    const pw = Number(((pack.pillars || {})[check.pillar] || {}).weight ?? 0.2);
    const score = pyRound((sw[check.severity] ?? 3) * pw * 10 * (check.status === "fail" ? 1 : 0.5), 2);
    const th = pack.priority_thresholds || { P1: 9, P2: 4 };
    return [score >= (th.P1 ?? 9) ? "P1" : score >= (th.P2 ?? 4) ? "P2" : "P3", score];
  }
  const byPriority = (a, b) => (a.priority < b.priority ? -1 : a.priority > b.priority ? 1 : b.score - a.score);
  function deterministicPlan(checks, pack, focusPillar = null) {
    const actions = new Map();
    for (const c of checks) {
      if (!["fail", "warn"].includes(c.status)) continue;
      let [pr, score] = checkPriority(c, pack);
      if (focusPillar && c.pillar === focusPillar) score += 1;
      const a = actions.get(c.rule);
      if (!a) actions.set(c.rule, { title: c.action || `Improve: ${c.title}`, pillar: c.pillar, priority: pr, score, effort: ["low", "medium"].includes(c.severity) ? "low" : "medium",
        impact: { critical: "high", high: "high", medium: "medium", low: "low" }[c.severity], rationale: c.why, steps: c.recommendation ? [c.recommendation] : [], check_ids: [c.id], source: "rule_pack" });
      else { a.check_ids.push(c.id); if (score > a.score) { a.score = score; a.priority = pr; } }
    }
    const bots = actions.get("crawler.ai_bot");
    if (bots) bots.title = `Review robots.txt rules for ${bots.check_ids.filter((i) => i.includes(":")).map((i) => i.split(":").slice(1).join(":")).join(", ")}`;
    return [...actions.values()].sort(byPriority);
  }
  /** Keep model actions grounded in failing checks; never under-prioritise; cover every failing rule. */
  function reconcilePlan(modelActions, checks, pack, focusPillar = null) {
    const failing = Object.fromEntries(checks.filter((c) => ["fail", "warn"].includes(c.status)).map((c) => [c.id, c]));
    const byRule = Object.fromEntries(Object.values(failing).map((c) => [c.rule, c]));
    const plan = [], covered = new Set();
    for (const a of modelActions) {
      let ids = [...new Set(a.check_ids || [])].filter((i) => failing[i] || byRule[i]);
      ids = ids.map((i) => (failing[i] ? i : byRule[i].id));
      if (!ids.length) continue;
      const floor = ids.map((i) => checkPriority(failing[i], pack)).reduce((best, t) => (PRIORITY_RANK[t[0]] < PRIORITY_RANK[best[0]] ? t : best));
      let prio = PRIORITY_RANK[a.priority] !== undefined ? a.priority : floor[0];
      if (PRIORITY_RANK[floor[0]] < PRIORITY_RANK[prio]) prio = floor[0];
      const pillar = PILLARS.includes(a.pillar) ? a.pillar : failing[ids[0]].pillar;
      plan.push({ ...a, pillar, priority: prio, check_ids: ids, score: floor[1], source: "model" });
      for (const i of ids) covered.add(failing[i].rule);
    }
    for (const d of deterministicPlan(checks, pack, focusPillar)) {
      const rules = new Set(d.check_ids.filter((i) => failing[i]).map((i) => failing[i].rule));
      if ([...rules].some((r) => !covered.has(r)) && d.check_ids.some((i) => ["medium", "high", "critical"].includes(failing[i].severity) || failing[i].status === "fail")) {
        plan.push(d);
        for (const r of rules) covered.add(r);
      }
    }
    return plan.sort((a, b) => (PRIORITY_RANK[a.priority] ?? 2) - (PRIORITY_RANK[b.priority] ?? 2) || (b.score || 0) - (a.score || 0));
  }
  function citationHit(answer, pageUrl, brand) {
    const host = hostOf(pageUrl).replace(/^www\./, "");
    const srcs = (answer.sources || []).join(" ").toLowerCase();
    const cited = !!host && (srcs.includes(host) || srcs.includes(String(pageUrl).toLowerCase()));
    const text = `${answer.answer || ""} ${(answer.brands || []).join(" ")}`.toLowerCase();
    return [cited, !!brand && text.includes(String(brand).toLowerCase())];
  }
  function deterministicSummary(overall, grade, pillars, plan, ptype) {
    const assessed = pillars.filter((p) => p.assessed);
    if (!assessed.length) return "No GEO checks could be assessed.";
    const weakest = [...assessed].sort((a, b) => a.score - b.score).slice(0, 2);
    const strongest = assessed.reduce((best, p) => (p.score > best.score ? p : best));
    const p1 = plan.filter((a) => a.priority === "P1").map((a) => a.title).slice(0, 3);
    return `This ${ptype} page scores ${pyFixed(overall, 0)}/100 (grade ${grade}) for generative engine optimisation. ` +
      `Strongest pillar: ${strongest.label} (${pyFixed(strongest.score, 0)}); weakest: ` + weakest.map((p) => `${p.label} (${pyFixed(p.score, 0)})`).join(", ") + ". " +
      (p1.length ? `Start with: ${p1.join("; ")}.` : `${plan.length} improvement(s) recommended.`);
  }

  // ------------------------------------------------------------------ HTML report (REPORT_TEMPLATE)
  const E = U.esc;
  function renderReport(c) {
    const bars = c.pillars.map((p) => `<div class="bar"><span>${E(p.label)}</span><div class="track"><div class="fill" style="width:${p.width}%"></div></div><b>${E(p.score_label)}</b></div>`).join("");
    const actions = c.actions.map((a) => `<tr><td><span class="pill ${E(a.priority)}">${E(a.priority)}</span></td><td><b>${E(a.title)}</b>${a.rationale ? `<br><span class="muted">${E(a.rationale)}</span>` : ""}
<ol class="steps">${a.steps.map((s) => `<li>${E(s)}</li>`).join("")}</ol></td><td>${E(a.pillar_label)}</td><td>${E(a.effort)}</td><td>${E(a.impact)}</td></tr>`).join("");
    const bots = c.bots.map((b) => `<tr><td><code>${E(b.agent)}</code></td><td>${E(b.vendor)}</td><td>${E(b.purpose)}</td><td class="s ${b.css}">${E(b.label)}</td></tr>`).join("");
    const sections = c.pillar_checks.map((p) => `<section><h2>${E(p.label)} <span class="muted">${E(p.score_label)}</span></h2><table><thead><tr><th>Status</th><th>Check</th><th>Result</th></tr></thead><tbody>
${p.rows.map((r) => `<tr><td class="s ${E(r.status)}">${E(r.status)}</td><td>${E(r.title)}</td><td>${E(r.message)}${r.evidence ? `<br><span class="muted">${E(r.evidence)}</span>` : ""}</td></tr>`).join("")}
</tbody></table></section>`).join("");
    const cit = c.citation;
    const citation = cit.ran ? `<p>Cited in ${E(cit.cited_label)} of answers; brand mentioned in ${E(cit.brand_label)}.</p>
<table><thead><tr><th>Question</th><th>Model tier</th><th>Page cited</th><th>Brand named</th></tr></thead><tbody>
${cit.results.map((r) => `<tr><td>${E(r.question)}</td><td>${E(r.tier)}</td><td>${r.cited_url ? "yes" : "no"}</td><td>${r.brand_mentioned ? "yes" : "no"}</td></tr>`).join("")}
</tbody></table>` : `<p class="muted">${E(cit.note)}</p>`;
    return `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>GEO audit - ${E(c.page)}</title>
<style>
:root{--ink:#18212f;--muted:#5d6676;--line:#e2e5eb;--bg:#f5f6f8;--card:#fff;--pass:#067647;--warn:#b54708;--fail:#b42318;--na:#98a2b3;--accent:#3b2f80}
*{box-sizing:border-box}body{margin:0;font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:var(--ink);background:var(--bg)}
header{background:var(--accent);color:#fff;padding:28px 32px 64px}header h1{margin:0 0 4px;font-size:24px}header p{margin:0;opacity:.85;word-break:break-all}
main{max-width:1120px;margin:-44px auto 0;padding:0 20px 48px}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:20px 22px;margin-bottom:18px}
h2{font-size:18px;margin:0 0 12px}.score{display:flex;gap:28px;align-items:center;flex-wrap:wrap}
.big{font-size:56px;font-weight:700;line-height:1}.grade{font-size:22px;font-weight:700;padding:6px 14px;border-radius:8px;background:#eef0fb;color:var(--accent)}
.bars{flex:1;min-width:280px}.bar{display:grid;grid-template-columns:170px 1fr 52px;gap:10px;align-items:center;margin:6px 0;font-size:14px}
.track{background:#edf0f4;border-radius:6px;height:12px;overflow:hidden}.fill{height:100%;background:var(--accent)}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;padding:7px 9px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.03em}
.s{font-weight:700;font-size:12px;text-transform:uppercase}.pass{color:var(--pass)}.warn{color:var(--warn)}.fail{color:var(--fail)}.na{color:var(--na)}
.pill{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;font-weight:600;color:#fff}.P1{background:var(--fail)}.P2{background:var(--warn)}.P3{background:#475467}
.muted{color:var(--muted)}.note{font-size:13px;color:var(--muted)}.exp{display:inline-block;background:#fef0c7;color:#7a2e0e;font-weight:700;font-size:12px;padding:2px 8px;border-radius:6px}
ol.steps{margin:4px 0 0 18px;padding:0}code{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12.5px}
</style></head><body>
<header><h1>Generative Engine Optimization audit</h1><p>${E(c.page)} &middot; ${E(c.page_type)} page &middot; ${E(c.generated)}</p></header>
<main>
<section><div class="score"><div><div class="big">${E(c.overall)}</div><div class="muted">overall GEO score</div></div><div class="grade">${E(c.grade)}</div>
<div class="bars">${bars}</div></div>
${c.focus_label ? `<p class="note">Focus: ${E(c.focus_label)}</p>` : ""}</section>
<section><h2>Executive summary</h2><p>${E(c.executive_summary)}</p><p class="note">Scores are computed in code from rule pack v${E(c.rule_pack_version)}; the action plan was written by ${E(c.plan_label)}.</p></section>
<section><h2>Prioritised action plan</h2><table><thead><tr><th>Priority</th><th>Action</th><th>Pillar</th><th>Effort</th><th>Impact</th></tr></thead><tbody>
${actions}
</tbody></table>${c.actions.length ? "" : '<p class="pass">Nothing to fix - every check passed.</p>'}</section>
<section><h2>AI crawler access</h2><table><thead><tr><th>User agent</th><th>Vendor</th><th>Purpose</th><th>Access</th></tr></thead><tbody>
${bots}
</tbody></table><p class="note">llms.txt: ${E(c.llms_label)}</p></section>
${sections}
<section><h2>Citation test <span class="exp">EXPERIMENTAL</span></h2><p class="note">${E(cit.label)}</p>
${citation}</section>
</main></body></html>`;
  }

  const CITATION_LABEL = "EXPERIMENTAL - measures whether the configured model(s) cite this page or brand when answering generated questions from their own knowledge (no live browsing). Results vary by model and run; treat as a directional signal.";

  // ------------------------------------------------------------------ the agent
  CAF.agent(AGENT, {
    async run(input, ctx) {
      if (!input.page_url && !(input.html && String(input.html).trim())) throw new Error("Provide page_url or html.");
      const pack = ctx.skill("geo_rules");
      const env = { robots: null, bots: null, llms: null, ttfb_ms: null, psi: null, homepage_org: null };
      // 1. fetch
      let url = input.page_url || null, html = input.html && String(input.html).trim() ? input.html : null, status = null, finalUrl = url, unreachable = false;
      const demo = url && CAF.site.isDemo(url);
      if (html && (!url || !demo)) {
        ctx.warn(url ? `Pasted HTML for ${url}: the hosted console cannot fetch that site, so crawler access, llms.txt, TTFB and homepage checks were skipped.`
          : "Pasted HTML without page_url: crawler access, llms.txt, TTFB and homepage checks were skipped.");
        ctx.degraded("html input: crawler-access and performance fetches skipped");
        if (!url) finalUrl = null;
      } else if (!demo) {
        ctx.page({ url });  // standard "cannot fetch live websites" degraded reason
        ctx.warn(`Could not fetch ${url}: the hosted console only serves the embedded demo site. Paste the page HTML into the html field, or run the Python service.`);
        ctx.degraded("page could not be fetched");
        unreachable = true;
      } else {
        ctx.progress(`Fetching ${url} and its robots.txt / llms.txt (demo site)`);
        if (!html) {
          const page = demoFetch(url);
          if (!fetchOk(page)) { ctx.warn(`Could not fetch ${url}: ${page.error || `HTTP ${page.status}`}`); ctx.degraded("page could not be fetched"); }
          html = page.html || "";
          status = page.status; finalUrl = page.final_url;
          env.ttfb_ms = Number(page.elapsed_ms);
        }
        const root = `${new URL(url).protocol}//${new URL(url).host}`;
        const rb = demoFetch(`${root}/robots.txt`);
        const looksHtml = (t) => /^(<!doctype|<html)/i.test(String(t || "").trimStart());
        const found = rb.status === 200 && !!rb.html && !looksHtml(rb.html);
        env.robots = { url: `${root}/robots.txt`, found, status: rb.status };
        env.bots = evaluateAiCrawlers(found ? rb.html : [404, 410].includes(rb.status) ? "" : null, finalUrl || url, pack.ai_crawlers || []);
        if (!found && ![404, 410].includes(rb.status)) ctx.warn(`robots.txt could not be read (${rb.status || rb.error}); AI crawler access not judged.`);
        const lt = demoFetch(`${root}/llms.txt`);
        const ltOk = lt.status === 200 && !!lt.html && !looksHtml(lt.html);
        env.llms = parseLlmsTxt(ltOk ? lt.html : null);
        env.llms_url = `${root}/llms.txt`;
        env.fixture = true;
        if (!["", "/"].includes(pathOf(url))) {
          const home = demoFetch(`${root}/`);
          if (fetchOk(home)) env.homepage_org = extractGeoFacts(`${root}/`, home.html).org_schema_on_page;
        }
        env.psi_note = input.pagespeed_api_key ? "demo fixture host is not publicly reachable" : "no API key supplied";
        ctx.warn("Demo fixture host: fetch time is a local measurement, not a real TTFB.");
      }
      if (input.pagespeed_api_key && !demo) env.psi_note = "the hosted console cannot call PageSpeed Insights (no network access)";
      const dataUrl = finalUrl || url || PLACEHOLDER;
      // 2. audit
      const brand = input.brand_name || ctx.brand.name || null;
      const focus = input.focus_section || null;
      const focusPillar = resolveFocus(focus, pack.pillars || {});
      const sectionFocus = focus && !focusPillar ? focus : null;
      const facts = extractGeoFacts(dataUrl, html || "", { focus: sectionFocus, brand });
      facts.page_type = detectPageType(facts, !!input.page_url);
      const today = new Date(new Date().toISOString().slice(0, 10) + "T00:00:00Z");
      const geoChecks = unreachable ? [] : evaluateGeo(facts, { ...env, brand }, pack, today);
      if (sectionFocus && !facts.focus_found) ctx.warn(`Focus section '${sectionFocus}' was not found on the page; citation checks used the main content.`);
      const strict = new Set(pack.strict_nested_types || []);
      const validated = facts.schema.entities.map((e) => validateEntity(e, pack.schema_types || {}, strict)).filter(Boolean);
      // 3. score
      const pillars = scorePillars(geoChecks, pack.pillars || {});
      const overall = overallScore(pillars);
      const grade = gradeFor(overall, pack.grades || []);
      for (const c of geoChecks) ctx.check({ rule: c.id, status: c.status === "na" ? "info" : c.status, severity: c.severity, message: `${c.title}: ${c.message}`, ref: `GEO pillar: ${pack.pillars[c.pillar].label}`, target: dataUrl });
      for (const p of pillars) if (!p.assessed) ctx.degraded(`${p.label} not assessed`);
      // 4. citation test (experimental; needs a live model)
      const citation = { enabled: !!input.citation_test, ran: false, experimental: true, label: CITATION_LABEL, questions: [], results: [], cited_rate: null, brand_mention_rate: null, note: "" };
      if (!citation.enabled) citation.note = "Not requested (set citation_test=true with a configured model).";
      else if (!ctx.llmLive) { citation.note = "Skipped: no language model configured (offline mode)."; ctx.warn("Citation test skipped: no language model configured."); }
      else {
        const d0 = CAF.lib.html.parse(html || "");
        const title = d0.querySelector("title") ? squash(d0.querySelector("title").textContent) : "";
        const headings = [...d0.querySelectorAll("h1,h2,h3")].map(txt).slice(0, 20);
        const n = Number(input.citation_questions || 5);
        let questions = null;
        try {
          ctx.progress(`Citation test: generating ${n} question(s)`);
          const qs = await ctx.generate("questions", { page_url: dataUrl, title, headings, count: n, page_questions: facts.questions.map((q) => q.question).slice(0, 10) });
          questions = (qs.questions || []).map((q) => String(q).trim()).filter(Boolean).slice(0, n);
        } catch (e) { citation.note = `Question generation failed: ${(e && e.name) || "Error"}`; ctx.warn(citation.note); }
        if (questions) {
          for (const tier of input.citation_tiers && input.citation_tiers.length ? input.citation_tiers : ["balanced"]) for (const q of questions) {
            let ans;
            try { ans = await ctx.generate("answer", { question: q }, { tier }); } catch (e) { ctx.log(`citation answer failed (${tier}): ${e.message}`); continue; }
            const [cited, brandHit] = citationHit(ans, dataUrl, brand);
            citation.results.push({ question: q, tier, cited_url: cited, brand_mentioned: brandHit, sources: (ans.sources || []).slice(0, 8), answer_excerpt: pslice(ans.answer || "", 300) });
          }
          citation.ran = citation.results.length > 0;
          citation.questions = questions;
          if (citation.results.length) {
            citation.cited_rate = pyRound(citation.results.filter((r) => r.cited_url).length / citation.results.length, 3);
            citation.brand_mention_rate = pyRound(citation.results.filter((r) => r.brand_mentioned).length / citation.results.length, 3);
          }
        }
      }
      // 5. plan (model writes it; code reconciles)
      const labels = Object.fromEntries(Object.entries(pack.pillars || {}).map(([k, v]) => [k, v.label]));
      let plan, summary, planSource = "rule_pack";
      if (unreachable) { plan = []; summary = "No GEO checks could be assessed."; }
      else {
        const failing = geoChecks.filter((c) => ["fail", "warn"].includes(c.status));
        const promptChecks = [...failing].sort((a, b) => checkPriority(b, pack)[1] - checkPriority(a, pack)[1]).slice(0, 30).map((c) => ({ id: c.id, pillar: c.pillar, status: c.status,
          severity: c.severity, priority: checkPriority(c, pack)[0], title: c.title, message: c.message, recommendation: c.recommendation }));
        const pillarLines = pillars.map((p) => (p.assessed ? `${p.label} (${p.key}): ${pyFixed(p.score, 0)}/100` : `${p.label} (${p.key}): not assessed`));
        ctx.progress("Writing the action plan from the failing checks");
        const modelPlan = await ctx.generate("plan", { page_url: dataUrl, page_type: facts.page_type, overall: overall !== null ? pyFixed(overall, 0) : "n/a", grade, pillars: pillarLines,
          checks: promptChecks, focus: (focusPillar && labels[focusPillar]) || sectionFocus || "", citation });
        const useModel = ctx.llmLive;
        plan = useModel ? reconcilePlan(modelPlan.actions || [], geoChecks, pack, focusPillar) : deterministicPlan(geoChecks, pack, focusPillar);
        summary = useModel && String(modelPlan.executive_summary || "").trim() ? modelPlan.executive_summary.trim() : deterministicSummary(overall, grade, pillars, plan, facts.page_type);
        planSource = useModel && plan.some((a) => a.source === "model") ? "model" : "rule_pack";
      }
      // 6. report
      const bots = (env.bots || []).map((b) => ({ agent: b.agent, vendor: b.vendor, purpose: b.purpose, allowed: b.allowed, note: b.note || "" }));
      const llms = env.llms || {};
      const performanceOut = { ttfb_ms: env.ttfb_ms, ttfb_is_proxy: true, html_kb: pyRound(facts.html_bytes / 1024, 1), render_blocking: facts.render_blocking, images: facts.images,
        images_without_dimensions: facts.images_without_dimensions.length, scripts: facts.scripts, third_party_script_hosts: facts.third_party_scripts, psi: env.psi,
        note: (env.psi_note || "") + (env.fixture ? "; fetch time measured against a local fixture" : "") };
      const ACTION_KEYS = ["title", "pillar", "priority", "effort", "impact", "rationale", "steps", "check_ids", "source"];
      const actionPlan = plan.map((a) => Object.fromEntries(ACTION_KEYS.filter((k) => a[k] !== undefined).map((k) => [k, a[k]])));
      const sevById = Object.fromEntries(geoChecks.map((c) => [c.id, c.severity]));
      const findings = actionPlan.map((a) => {
        const sev = (a.check_ids || []).map((i) => sevById[i] || "low").reduce((best, s) => (SEV_INDEX.indexOf(s) > SEV_INDEX.indexOf(best) ? s : best), "low");
        const ev = geoChecks.filter((c) => (a.check_ids || []).includes(c.id)).map((c) => c.message).slice(0, 3);
        return { title: a.title, detail: a.rationale || "", severity: sev, category: "geo", evidence: ev.join("; "), recommendation: (a.steps || []).slice(0, 3).join(" "), target: dataUrl, source_agent: AGENT };
      });
      const CHECK_KEYS = ["id", "rule", "pillar", "title", "status", "severity", "weight", "message", "evidence", "recommendation"];
      const output = {
        page_url: input.page_url || null, final_url: finalUrl, http_status: status, page_type: facts.page_type, overall_score: overall, grade, pillars,
        checks: geoChecks.map((c) => Object.fromEntries(CHECK_KEYS.map((k) => [k, c[k]]))), ai_crawlers: bots,
        llms_txt: { url: env.llms_url || null, present: llms.present ?? null, valid: llms.valid ?? null, sections: llms.sections || 0, links: llms.links || 0 },
        performance: performanceOut,
        structured_data: { blocks: facts.schema.blocks, parse_errors: facts.schema.errors, types: facts.schema.types, entities: validated, microdata_items: facts.schema.microdata_items },
        focus_section: input.focus_section || null, focus_pillar: focusPillar, focus_found: facts.focus_found, executive_summary: summary, action_plan: actionPlan, plan_source: planSource,
        citation_test: citation, rule_pack_version: String(pack.version ?? ""), findings,
      };
      const scoreLabel = (p) => (p.assessed ? pyFixed(p.score, 0) : "n/a");
      const html2 = renderReport({
        page: dataUrl, page_type: facts.page_type, generated: new Date().toISOString().slice(0, 16).replace("T", " ") + " UTC", overall: overall !== null ? pyFixed(overall, 0) : "n/a", grade,
        pillars: pillars.map((p) => ({ ...p, width: p.score || 0, score_label: scoreLabel(p) })),
        focus_label: output.focus_section ? labels[output.focus_pillar || ""] || output.focus_section || "" : "", executive_summary: summary, rule_pack_version: output.rule_pack_version,
        plan_label: planSource === "model" ? "a language model from the failing checks" : "the rule pack (no model configured)",
        actions: actionPlan.map((a) => ({ rationale: "", steps: [], effort: "medium", impact: "medium", ...a, pillar_label: labels[a.pillar] || a.pillar })),
        bots: bots.map((b) => ({ ...b, css: b.allowed === null ? "na" : b.allowed ? "pass" : b.purpose === "training" ? "warn" : "fail", label: b.allowed === null ? "unknown" : b.allowed ? "allowed" : "blocked" })),
        llms_label: llms.present === undefined || llms.present === null ? "not checked" : llms.present ? "present" : `not found at ${env.llms_url}`,
        pillar_checks: pillars.map((p) => ({ label: p.label, score_label: scoreLabel(p), rows: geoChecks.filter((c) => c.pillar === p.key) })),
        citation: { ...citation, cited_label: `${pyFixed((citation.cited_rate || 0) * 100, 0)}%`, brand_label: `${pyFixed((citation.brand_mention_rate || 0) * 100, 0)}%` } });
      ctx.artifact({ name: "geo_report.html", mime: "text/html", content: html2, description: "GEO audit report" });
      const slimFacts = { ...facts, schema: { blocks: facts.schema.blocks, errors: facts.schema.errors, types: facts.schema.types, microdata_items: facts.schema.microdata_items, validated } };
      ctx.artifact({ name: "geo_audit.json", mime: "application/json", content: JSON.stringify({ pillars, checks: geoChecks, facts: slimFacts }, null, 2), description: "GEO checks and extracted page facts" });
      const assessed = pillars.filter((p) => p.assessed);
      const weakest = assessed.length ? assessed.reduce((w, p) => (p.score < w.score ? p : w)) : null;
      const kind = facts.page_type === "page" ? "this page" : `${facts.page_type} page`;
      const llmsState = llms.present === undefined || llms.present === null ? "not checked" : llms.present ? "present" : "missing";
      const summaryLine = overall !== null ? `GEO score ${pyFixed(overall, 0)}/100 (${grade}) for ${kind}; ${bots.filter((b) => b.allowed === false).length} AI crawler(s) blocked, llms.txt ${llmsState}; ${actionPlan.length} action(s)` +
        (weakest ? `; weakest pillar: ${weakest.label} (${pyFixed(weakest.score, 0)})` : "") : `GEO audit: ${actionPlan.length} action(s)`;
      return {
        output, summary: summaryLine,
        view: [
          { type: "kv", title: "GEO score", data: { page: dataUrl, page_type: facts.page_type, overall_score: overall === null ? "n/a" : `${pyFixed(overall, 0)}/100`, grade,
            ai_crawlers_blocked: bots.filter((b) => b.allowed === false).map((b) => b.agent).join(", ") || "none", llms_txt: llmsState, plan_written_by: planSource } },
          { type: "markdown", title: "Executive summary", markdown: summary },
          { type: "table", title: "Pillars", rows: pillars.map((p) => ({ pillar: p.label, score: p.assessed ? p.score : "n/a", weight: p.weight, passed: p.passed, warned: p.warned, failed: p.failed })) },
          { type: "table", title: "Prioritised action plan", rows: actionPlan.map((a) => ({ priority: a.priority, action: a.title, pillar: labels[a.pillar] || a.pillar, effort: a.effort, impact: a.impact, checks: (a.check_ids || []).join(", ") })) },
          { type: "table", title: "AI crawler access", rows: bots.map((b) => ({ agent: b.agent, vendor: b.vendor, purpose: b.purpose, access: b.allowed === null ? "unknown" : b.allowed ? "allowed" : "blocked" })) },
          { type: "table", title: "Checks needing attention", rows: geoChecks.filter((c) => ["fail", "warn"].includes(c.status)).map((c) => ({ status: c.status, check: c.id, result: c.message, evidence: c.evidence })) },
          { type: "html", title: "Report (geo_report.html)", artifact: "geo_report.html" },
        ],
      };
    },
  });
})();
