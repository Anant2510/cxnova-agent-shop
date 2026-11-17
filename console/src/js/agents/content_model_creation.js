/* Content Model Creation - browser twin of caf/agents/web/content_model_creation.py.
   Same stages (analyse -> vision -> propose -> normalise -> emit -> report): DOM heuristics on DOMParser
   (ports of the BeautifulSoup detectors: hero, card grid, CTA banner, FAQ, testimonial, pricing table,
   article body, author bio, form, key facts, rich text), the deterministic baseline model, the same
   normaliser (reserved names, references resolve, component cycles broken, display fields) and the
   seven emitters (AEM CF models, Sitecore SCS YAML with deterministic UUIDv5 ids, Contentful CMA JSON +
   migration script, Contentstack, Strapi, Drupal config YAML via a PyYAML-compatible dumper, GraphQL SDL).
   Every emitted file is its own artifact (folder names kept, no zip). Pages come from the embedded demo
   site or pasted HTML; design screenshots are skipped with a warning (no vision step in the console).
   Local helpers (no lib changes): SHA-1/UUIDv5, PyYAML safe_dump emitter, Python json.dumps/repr,
   BeautifulSoup get_text emulation, the HttpxCrawler BFS over the demo site. */
(() => {
  const AGENT = "content_model_creation";
  const FIELD_TYPES = ["short_text", "long_text", "rich_text", "integer", "decimal", "boolean", "date", "datetime", "media", "url", "email", "enum", "reference", "component", "json"];
  const TARGETS = ["aem", "sitecore", "contentful", "contentstack", "strapi", "drupal", "graphql"];
  const RESERVED = new Set(["id", "type", "sys", "uid", "_id", "created_at", "updated_at", "published_at", "created_by", "updated_by", "locale", "localizations", "document_id", "publish_details", "tags", "options", "status", "version", "url"]);
  const SLUG_PATTERN = "^[a-z0-9]+(?:-[a-z0-9]+)*(?:/[a-z0-9]+(?:-[a-z0-9]+)*)*$";
  const URL_PATTERN = "^(https?://|/|#|mailto:|tel:)\\S*$";
  const EMAIL_PATTERN = "^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$";
  const PLACEHOLDER = "https://page.invalid/";
  const VKEYS = ["min_length", "max_length", "pattern", "min_value", "max_value", "enum_values", "allowed_types", "media_kinds", "unique", "min_items", "max_items"];
  const VLISTS = new Set(["enum_values", "allowed_types", "media_kinds"]);

  // ------------------------------------------------------------------ Python-compatible helpers (pure)
  const has = (o, k) => o !== null && o !== undefined && Object.prototype.hasOwnProperty.call(o, k);
  const dict = (entries) => { const o = Object.create(null); for (const [k, v] of entries) o[k] = v; return o; };
  const cmp = (a, b) => (a < b ? -1 : a > b ? 1 : 0);
  const uniqSorted = (a) => [...new Set(a)].sort(cmp);
  const plen = (s) => [...String(s ?? "")].length;
  const pslice = (s, n) => { const t = String(s ?? ""); return t.length <= n ? t : [...t].slice(0, n).join(""); };
  const WS = "\\t\\n\\x0b\\x0c\\r\\x1c-\\x1f \\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000"; // str.isspace()
  const WS_RUN = new RegExp(`[${WS}]+`), WS_EDGES = new RegExp(`^[${WS}]+|[${WS}]+$`, "g");
  const pysplit = (s) => String(s ?? "").split(WS_RUN).filter(Boolean);
  const pystrip = (s) => String(s ?? "").replace(WS_EDGES, "");
  const pystr = (v) => (v === null || v === undefined ? "None" : v === true ? "True" : v === false ? "False" : String(v));
  const NONPRINT = /[\p{Cc}\p{Cf}\p{Co}\p{Cn}\p{Cs}\p{Zl}\p{Zp}\p{Zs}]/u;
  /** repr(str) */
  function pyReprStr(s) {
    s = String(s);
    const q = s.includes("'") && !s.includes('"') ? '"' : "'";
    let out = q;
    for (const ch of s) {
      const cp = ch.codePointAt(0);
      if (ch === q || ch === "\\") out += "\\" + ch;
      else if (ch === "\n") out += "\\n";
      else if (ch === "\r") out += "\\r";
      else if (ch === "\t") out += "\\t";
      else if (ch !== " " && NONPRINT.test(ch)) out += cp <= 0xff ? "\\x" + cp.toString(16).padStart(2, "0") : cp <= 0xffff ? "\\u" + cp.toString(16).padStart(4, "0") : "\\U" + cp.toString(16).padStart(8, "0");
      else out += ch;
    }
    return out + q;
  }
  const pyRepr = (v) => (Array.isArray(v) ? "[" + v.map(pyRepr).join(", ") + "]" : typeof v === "string" ? pyReprStr(v) : pystr(v));
  const asciiEscape = (s) => s.replace(/[\u007f-￿]/g, (c) => "\\u" + c.charCodeAt(0).toString(16).padStart(4, "0"));
  /** json.dumps(x) with default separators. */
  function pyDumps(v, ensureAscii = true) {
    if (v === null || v === undefined) return "null";
    if (typeof v === "boolean") return v ? "true" : "false";
    if (typeof v === "number") return Number.isFinite(v) ? String(v) : v > 0 ? "Infinity" : v < 0 ? "-Infinity" : "NaN";
    if (typeof v === "string") { const s = JSON.stringify(v); return ensureAscii ? asciiEscape(s) : s; }
    if (Array.isArray(v)) return "[" + v.map((x) => pyDumps(x, ensureAscii)).join(", ") + "]";
    return "{" + Object.entries(v).map(([k, x]) => `${pyDumps(k, ensureAscii)}: ${pyDumps(x, ensureAscii)}`).join(", ") + "}";
  }
  /** json.dumps(x, indent=2[, ensure_ascii]) */
  const jsonPretty = (v, ensureAscii = false) => { const s = JSON.stringify(v, null, 2); return ensureAscii ? asciiEscape(s) : s; };
  /** urllib.parse.urlsplit(url).path */
  function urlPath(url) {
    let s = String(url ?? "").replace(/^[\x00-\x20]+/, "").replace(/[\t\r\n]/g, "");
    const m = s.match(/^[A-Za-z][A-Za-z0-9+.-]*:/);
    if (m) s = s.slice(m[0].length);
    if (s.startsWith("//")) { const rest = s.slice(2), i = rest.search(/[/?#]/); s = i < 0 ? "" : rest.slice(i); }
    const j = s.search(/[?#]/);
    return j < 0 ? s : s.slice(0, j);
  }

  // ------------------------------------------------------------------ naming helpers (pure)
  const capitalize = (s) => (s ? s[0].toUpperCase() + s.slice(1).toLowerCase() : s);
  function snake(text) {
    let t = String(text || "").replace(/([a-z0-9])([A-Z])/g, "$1_$2");
    t = t.replace(/[^A-Za-z0-9]+/g, "_").replace(/^_+|_+$/g, "").toLowerCase();
    if (!t) return "item";
    return /^[a-z]/.test(t) ? t : `f_${t}`;
  }
  const camel = (t) => { const p = snake(t).split("_"); return p[0] + p.slice(1).map(capitalize).join(""); };
  const pascal = (t) => snake(t).split("_").map(capitalize).join("");
  const kebab = (t) => snake(t).replace(/_/g, "-");
  const plural = (w) => (/[^aeiou]y$/.test(w) ? w.slice(0, -1) + "ies" : /(s|x|z|ch|sh)$/.test(w) ? w + "es" : w + "s");
  const labelOf = (t) => capitalize(snake(t).replace(/_/g, " "));

  // ------------------------------------------------------------------ DOM helpers (BeautifulSoup semantics)
  const CTA_RX = /(^|[\s_-])(btn|button|cta)([\s_-]|$)/i;
  const CURRENCY_RX = /[$€£] ?\p{Nd}/gu; // applied to squashed text (single spaces only)
  const PROSE_TAGS = new Set(["p", "span", "strong", "em", "b", "i", "br", "a", "h2", "h3", "h4", "h5", "h6", "small", "time", "img", "option"]);
  const STRING_CONTAINERS = new Set(["script", "style", "template", "rt", "rp"]); // bs4 string types left out of get_text()
  const classes = (el) => String(el.getAttribute("class") || "").split(/\s+/).filter(Boolean);
  const identOf = (el) => (classes(el).join(" ") + " " + (el.getAttribute("id") || "")).toLowerCase();
  const signature = (el) => el.localName + "." + [...classes(el)].sort(cmp).join(".");
  function rawText(el) {
    const out = [];
    const walk = (n) => { for (const c of n.childNodes) { if (c.nodeType === 3) out.push(c.nodeValue); else if (c.nodeType === 1 && !STRING_CONTAINERS.has(c.localName)) walk(c); } };
    if (el) walk(el);
    return out.join(" ");
  }
  const textOf = (el) => (el ? pysplit(rawText(el)).join(" ") : "");
  const wordCount = (s) => pysplit(s).length;
  function stripText(el) {
    const out = [];
    const walk = (n) => { for (const c of n.childNodes) { if (c.nodeType === 3) { const t = pystrip(c.nodeValue); if (t) out.push(t); } else if (c.nodeType === 1 && !STRING_CONTAINERS.has(c.localName)) walk(c); } };
    if (el) walk(el);
    return out.join(" ");
  }
  function selectorOf(el) {
    if (el.getAttribute("id")) return `${el.localName}#${el.getAttribute("id")}`;
    return el.localName + classes(el).filter((c) => c !== "wrap").slice(0, 2).map((c) => `.${c}`).join("");
  }
  /** bs4 find(class_=regex): regex searched in each class token, then in the joined class string. */
  const classMatch = (el, rx) => { const c = classes(el); return c.some((x) => rx.test(x)) || (c.length > 0 && rx.test(c.join(" "))); };
  const findByClass = (root, rx) => [...root.querySelectorAll("[class]")].find((e) => classMatch(e, rx)) || null;
  const findAllByClass = (root, rx) => [...root.querySelectorAll("[class]")].filter((e) => classMatch(e, rx));
  const kids = (el) => [...el.children];
  const parentMatching = (el, names) => { for (let p = el.parentElement; p; p = p.parentElement) if (names.includes(p.localName)) return p; return null; };
  const precedes = (other, ref) => !!(ref.compareDocumentPosition(other) & Node.DOCUMENT_POSITION_PRECEDING); // other before ref (ancestors included)

  function repeatedGroup(el, minItems = 3) {
    let best = null;
    for (const parent of [el, ...el.querySelectorAll("*")]) {
      if (["nav", "footer", "header", "select", "table", "tr", "thead", "tbody"].includes(parent.localName)) continue;
      const ks = kids(parent).filter((c) => !["script", "style"].includes(c.localName) && !PROSE_TAGS.has(c.localName));
      const counts = new Map();
      for (const c of ks) { const s = signature(c); counts.set(s, (counts.get(s) || 0) + 1); }
      for (const [sig, n] of counts) {
        if (n < minItems) continue;
        const items = ks.filter((c) => signature(c) === sig);
        if (!best || items.length * (1 + items[0].querySelectorAll("*").length) > best[1].length * (1 + best[1][0].querySelectorAll("*").length)) best = [parent, items];
      }
    }
    return best;
  }
  function itemFields(item) {
    const f = {};
    const img = item.querySelector("img");
    if (img) { f.image = img.getAttribute("src") ?? ""; f.image_alt = img.getAttribute("alt") || ""; }
    const h = item.querySelector("h2,h3,h4,h5");
    if (h) f.title = pslice(textOf(h), 120);
    const tag = findByClass(item, /(tag|eyebrow|label|badge|kicker)/i);
    if (tag && (!h || tag !== h)) f.eyebrow = pslice(textOf(tag), 60);
    const strong = item.querySelector("strong,b");
    if (strong && !h) { f.value = pslice(textOf(strong), 40); f.label = pslice(pystrip(textOf(item).replace(textOf(strong), () => "")), 120); }
    const p = item.querySelector("p");
    if (p) f.text = pslice(textOf(p), 300);
    const a = item.querySelector("a[href]");
    if (a) f.link = { label: pslice(textOf(a), 60), url: a.getAttribute("href") };
    const bq = item.querySelector("blockquote");
    if (bq) { f.quote = pslice(textOf(bq), 400); const cap = item.querySelector("figcaption"); if (cap) f.author = pslice(textOf(cap), 120); }
    return f;
  }
  function ctasOf(el) {
    const out = [];
    for (const a of el.querySelectorAll("a,button")) {
      const cls = classes(a).join(" ");
      if (CTA_RX.test(cls) || identOf(a).includes("cta")) {
        const styles = [...cls.matchAll(/btn-([a-z]+)/g)].map((m) => m[1]);
        out.push({ label: pslice(textOf(a), 60), url: a.getAttribute("href") ?? "", style: styles.length ? styles[0] : "text" });
      }
    }
    return out;
  }
  function formFields(form, doc) {
    const fields = [];
    const spansLabels = [...doc.querySelectorAll("span,label")];
    for (const el of form.querySelectorAll("input,select,textarea")) {
      const typ = ["select", "textarea"].includes(el.localName) ? el.localName : String(el.getAttribute("type") || "text").toLowerCase();
      if (["hidden", "submit", "button", "reset", "image"].includes(typ)) continue;
      let label = "";
      if (el.getAttribute("id")) { const lab = [...doc.querySelectorAll("label[for]")].find((l) => l.getAttribute("for") === el.getAttribute("id")); label = textOf(lab); }
      label = label || el.getAttribute("aria-label") || el.getAttribute("placeholder") || "";
      if (!label) {
        let prev = null; // find_previous(["span", "label"]): nearest preceding element (ancestors included)
        for (const x of spansLabels) if (precedes(x, el)) prev = x;
        label = prev && prev !== form && form.contains(prev) ? textOf(prev) : "";
      }
      fields.push({ name: el.getAttribute("name") || el.getAttribute("id") || typ, label: pslice(label, 80), input_type: typ, required: el.hasAttribute("required"),
        options: [...el.querySelectorAll("option")].map(textOf).slice(0, 12), autocomplete: el.getAttribute("autocomplete") ?? "" });
    }
    return fields;
  }
  function pageJsonldTypes(doc) {
    const out = [];
    for (const s of doc.querySelectorAll("script[type]")) {
      if (!/ld\+json/i.test(s.getAttribute("type"))) continue;
      let data;
      try { data = JSON.parse(s.textContent); } catch { continue; }
      for (const item of Array.isArray(data) ? data : [data]) {
        if (!U.isObj(item)) continue;
        const t = item["@type"];
        out.push(...(Array.isArray(t) ? t : t && !(U.isObj(t) && !Object.keys(t).length) ? [t] : []));
      }
    }
    return out.map(pystr);
  }
  function classifyBlock(el, index, doc) {
    const found = [];
    const ident = identOf(el), sel = selectorOf(el);
    const heading = el.querySelector("h1,h2,h3");
    const words = wordCount(textOf(el));
    const add = (type, evidence, fields, confidence, items = 0) => found.push({ type, selector: sel, evidence, fields, confidence: Math.round(confidence * 100) / 100, items });
    const firstArticle = el.querySelector("article");
    if (el.localName === "article" || (firstArticle && firstArticle.querySelector("h1"))) {
      const art = el.localName === "article" ? el : firstArticle;
      const paras = art.querySelectorAll("p");
      if (art.querySelector("h1") && paras.length >= 4) {
        const times = [...art.querySelectorAll("time")].map((t) => t.getAttribute("datetime")).filter(Boolean);
        const byline = findByClass(art, /byline/i);
        add("article_body", ["<article> with H1", `${paras.length} paragraphs`, `${art.querySelectorAll("h2").length} H2 sections`, ...(times.length ? ["<time datetime>"] : [])],
          { headline: pslice(textOf(art.querySelector("h1")), 150), byline: pslice(textOf(byline), 150), dates: times.slice(0, 2), images: art.querySelectorAll("img").length, words: wordCount(textOf(art)) }, 0.95);
      }
      let bio = findByClass(art, /author-bio|about-author|author-box/i);
      if (!bio) { const h = [...art.querySelectorAll("h2,h3,h4")].find((x) => /^about the author/i.test(textOf(x))); bio = h ? parentMatching(h, ["aside", "section", "div"]) : null; }
      if (bio) {
        const link = bio.querySelector("a[href]");
        add("author_bio", [selectorOf(bio), "'About the author' block"], { name: textOf(bio.querySelector("strong")) || "", bio: pslice(textOf(bio.querySelector("p")), 300), has_photo: !!bio.querySelector("img"), profile_link: link ? link.getAttribute("href") : "" }, 0.9);
      }
      if (found.length) return found;
    }
    const forms = el.localName === "form" ? [el] : [...el.querySelectorAll("form")];
    for (const form of forms) {
      const fields = formFields(form, doc);
      if (fields.length) {
        const submit = [...form.querySelectorAll("button[type],input[type]")].find((x) => /submit/i.test(x.getAttribute("type"))) || form.querySelector("button");
        add("form", [`<form> with ${fields.length} field(s)`], { title: pslice(textOf(heading), 100), fields, submit_label: textOf(submit) || (submit ? submit.getAttribute("value") ?? "" : ""), action: form.getAttribute("action") ?? "" }, 0.95, fields.length);
      }
    }
    if (found.length) return found; // a block built around a form is a form
    const details = [...el.querySelectorAll("details")].filter((d) => d.querySelector("summary"));
    if (details.length >= 2 || (/\bfaq\b|frequently/.test(ident + " " + textOf(heading).toLowerCase()) && details.length)) {
      add("faq", [`${details.length} <details>/<summary> pairs`, ...(heading && /faq|frequently/i.test(textOf(heading)) ? ["FAQ heading"] : [])],
        { heading: pslice(textOf(heading), 120), questions: details.map((d) => pslice(textOf(d.querySelector("summary")), 150)).slice(0, 12) }, 0.95, details.length);
      return found;
    }
    const quotes = [...el.querySelectorAll("blockquote")];
    if (quotes.length && (/testimonial|review|quote|voices/.test(ident) || quotes.length >= 2)) {
      const items = quotes.map((q) => { const fig = parentMatching(q, ["figure"]); return { quote: pslice(textOf(q), 300), author: fig ? textOf(fig.querySelector("figcaption")) : "" }; });
      add("testimonial", [`${quotes.length} <blockquote>` + (el.querySelector("figcaption") ? " + <figcaption>" : "")], { heading: pslice(textOf(heading), 120), items: items.slice(0, 6) }, 0.9, quotes.length);
      return found;
    }
    const table = el.querySelector("table");
    const panels = findAllByClass(el, /plan-panel|pricing-card|plan-card|price-card/i);
    if (/pricing|plans?\b|price/.test(ident) || (table && (textOf(table).match(CURRENCY_RX) || []).length >= 3) || panels.length >= 2) {
      const plans = panels.map((p) => {
        const price = findByClass(p, /price/i), c = ctasOf(p);
        return { name: (p.getAttribute("id") || "").split("panel-").join("") || textOf(p.querySelector("h2,h3,h4,h5")), price: pslice(textOf(price), 40), features: [...p.querySelectorAll("li")].map((li) => pslice(textOf(li), 80)).slice(0, 12), cta: c.length ? c[0] : {} };
      });
      const rows = table ? [...table.querySelectorAll("tr")].map((tr) => [...tr.querySelectorAll("td,th")].map((c) => pslice(textOf(c), 40))).slice(0, 12) : [];
      const tabs = findAllByClass(el, /(^|-)tab$|plan-tab/i).map(textOf);
      add("pricing_table", [...(/pricing|plan/.test(ident) ? ["pricing/plans class"] : []), ...(panels.length ? [`${panels.length} plan panels`] : []), ...(rows.length ? [`table with ${rows.length} rows and currency values`] : []), ...(tabs.length ? [`${tabs.length} plan tabs`] : [])],
        { heading: pslice(textOf(heading), 120), plans, comparison_rows: rows, tabs }, 0.9, panels.length || rows.length);
      return found;
    }
    const imgs = el.querySelectorAll("img");
    const ctas = ctasOf(el);
    const h1 = el.querySelector("h1");
    const isHero = /hero|banner|masthead|jumbotron/.test(ident) || (index === 0 && h1);
    if (isHero && h1) {
      const img = imgs.length ? imgs[0] : null;
      const lead = [...el.querySelectorAll("p")].find((p) => classMatch(p, /lead|intro|sub/i)) || el.querySelector("p");
      add("hero", [index === 0 ? "first block with H1" : "hero class", ...(ctas.length ? [`${ctas.length} CTA(s)`] : []), ...(img ? ["image"] : [])],
        { heading: pslice(textOf(h1), 150), subheading: pslice(textOf(lead), 300), image: img ? img.getAttribute("src") ?? "" : "", image_alt: img ? img.getAttribute("alt") : null, ctas: ctas.slice(0, 3),
          breadcrumb: [...el.querySelectorAll("[aria-label]")].some((x) => /breadcrumb/i.test(x.getAttribute("aria-label"))) }, index === 0 ? 0.9 : 0.8);
      return found;
    }
    const grp = repeatedGroup(el);
    if (grp) {
      const items = grp[1], fields = items.map(itemFields);
      const keys = new Map();
      for (const f of fields) for (const k of Object.keys(f)) keys.set(k, (keys.get(k) || 0) + 1);
      const common = new Set([...keys].filter(([, n]) => n >= items.length / 2).map(([k]) => k));
      if (common.has("value") && common.has("label") && !common.has("title")) {
        add("key_facts", [`${items.length} repeated ${signature(items[0])} items with a highlighted value`], { heading: pslice(textOf(heading), 120), items: fields.slice(0, 8) }, 0.8, items.length);
        return found;
      }
      if (["title", "image", "link", "quote"].some((k) => common.has(k))) {
        const sorted = [...common].sort(cmp);
        add("card_grid", [`${items.length} repeated ${signature(items[0])} items`, "fields: " + sorted.join(", ")], { heading: pslice(textOf(heading), 120), items: fields.slice(0, 8), item_fields: sorted }, 0.85, items.length);
        return found;
      }
    }
    const firstLink = el.querySelector("a[href]");
    if ((/promo|cta|banner|band|newsletter|advisor|signup|offer/.test(ident) || (heading && words <= 90)) && (ctas.length || firstLink) && !imgs.length) {
      const link = ctas.length ? ctas[0] : { label: pslice(textOf(firstLink), 60), url: firstLink.getAttribute("href"), style: "text" };
      add("cta_banner", /promo|cta|banner|band|newsletter|advisor|offer/.test(ident) ? ["promo/CTA class"] : ["short block with heading + link"], { heading: pslice(textOf(heading), 120), text: pslice(textOf(el.querySelector("p")), 300), cta: link }, 0.75);
      return found;
    }
    const paras = el.querySelectorAll("p");
    if (heading && paras.length >= 1 && words >= 25) add("rich_text", [`heading + ${paras.length} paragraph(s)`], { heading: pslice(textOf(heading), 120), words }, 0.6);
    return found;
  }
  function detectComponents(url, html) {
    const doc = CAF.lib.html.parse(html || "");
    for (const t of doc.querySelectorAll("script,style,noscript,template")) if (!(t.localName === "script" && String(t.getAttribute("type") || "").includes("ld+json"))) t.remove();
    const ld = pageJsonldTypes(doc);
    const main = doc.querySelector("main") || doc.body || doc.documentElement;
    const blocks = [];
    for (const c of kids(main)) { // unwrap layout divs that only group several blocks
      if (["script", "style"].includes(c.localName)) continue;
      const inner = kids(c).filter((k) => ["section", "article", "aside", "form", "figure"].includes(k.localName));
      blocks.push(...(c.localName === "div" && inner.length >= 2 ? inner : [c]));
    }
    const comps = [];
    blocks.forEach((b, i) => comps.push(...classifyBlock(b, i, doc)));
    const types = new Set(comps.map((c) => c.type));
    const path = urlPath(url) || "/";
    const ptype = types.has("article_body") || ld.some((t) => ["Article", "BlogPosting", "NewsArticle"].includes(t)) ? "article_page" : ld.includes("Product") || types.has("pricing_table") ? "product_page" : ["", "/"].includes(path) ? "home_page" : "standard_page";
    const titleEl = doc.querySelector("title");
    const btn = uniqSorted([...doc.querySelectorAll("a,button")].flatMap((a) => [...classes(a).join(" ").matchAll(/btn-([a-z]+)/g)].map((m) => m[1])));
    return { url, path, title: titleEl ? stripText(titleEl) : "", page_type: ptype, jsonld_types: uniqSorted(ld), components: comps, button_styles: btn };
  }

  // ------------------------------------------------------------------ neutral model (pure)
  function F(fid, label, ftype, o = {}) {
    const v = { min_length: null, max_length: null, pattern: null, min_value: null, max_value: null, enum_values: [], allowed_types: [], media_kinds: [], unique: false, min_items: null, max_items: null };
    for (const k of Object.keys(v)) if (o[k] !== undefined) v[k] = o[k];
    return { id: fid, label, type: ftype, required: !!o.required, multiple: !!o.multiple, localized: o.localized === undefined ? true : o.localized, help: o.help || "", validations: v };
  }
  function blueprints(buttonStyles, inputTypes) {
    const styles = uniqSorted(["primary", "secondary", "outline", "text", ...buttonStyles]);
    const inputs = uniqSorted(["text", "email", "tel", "textarea", "select", "checkbox", ...inputTypes]);
    const img = { media_kinds: ["image"] };
    return {
      cta_link: { label: "Call to action", kind: "component", description: "A link styled as a button or text link.", fields: [
        F("label", "Label", "short_text", { required: true, max_length: 40 }), F("link", "Link", "url", { required: true, pattern: URL_PATTERN, localized: false }),
        F("style", "Style", "enum", { enum_values: styles, localized: false }), F("open_in_new_tab", "Open in new tab", "boolean", { localized: false }),
        F("analytics_label", "Analytics label", "short_text", { max_length: 60, localized: false })] },
      seo_metadata: { label: "SEO metadata", kind: "component", mixin: true, description: "Search and social metadata shared by every page.", fields: [
        F("meta_title", "Meta title", "short_text", { max_length: 60, help: "30-60 characters" }), F("meta_description", "Meta description", "long_text", { max_length: 160, help: "70-160 characters" }),
        F("canonical_url", "Canonical URL", "url", { pattern: URL_PATTERN, localized: false }), F("social_image", "Social image", "media", img),
        F("noindex", "Hide from search engines", "boolean", { localized: false })] },
      hero: { label: "Hero", kind: "component", description: "Top-of-page introduction with headline, supporting copy, image and calls to action.", fields: [
        F("heading", "Heading", "short_text", { required: true, max_length: 90 }), F("subheading", "Subheading", "long_text", { max_length: 300 }),
        F("image", "Image", "media", img), F("image_alt", "Image alt text", "short_text", { max_length: 150, help: "Required when the image is informative" }),
        F("primary_cta", "Primary call to action", "component", { allowed_types: ["cta_link"] }),
        F("secondary_cta", "Secondary call to action", "component", { allowed_types: ["cta_link"] }), F("show_breadcrumb", "Show breadcrumb", "boolean", { localized: false })] },
      card: { label: "Card", kind: "component", description: "One item in a card grid.", fields: [
        F("title", "Title", "short_text", { required: true, max_length: 80 }), F("eyebrow", "Eyebrow / tag", "short_text", { max_length: 30 }),
        F("text", "Text", "long_text", { max_length: 300 }), F("image", "Image", "media", img), F("image_alt", "Image alt text", "short_text", { max_length: 150 }),
        F("link", "Link", "component", { allowed_types: ["cta_link"] })] },
      card_grid: { label: "Card grid", kind: "component", description: "A heading with a grid of repeated cards.", fields: [
        F("heading", "Heading", "short_text", { max_length: 90 }), F("intro", "Intro", "long_text", { max_length: 300 }),
        F("cards", "Cards", "component", { required: true, multiple: true, allowed_types: ["card"], min_items: 1, max_items: 12 }),
        F("columns", "Columns", "integer", { min_value: 1, max_value: 6, localized: false })] },
      fact: { label: "Fact", kind: "component", description: "A highlighted value with a short label.", fields: [
        F("value", "Value", "short_text", { required: true, max_length: 20 }), F("label", "Label", "short_text", { required: true, max_length: 120 })] },
      key_facts: { label: "Key facts", kind: "component", description: "A strip of highlighted numbers or proof points.", fields: [
        F("heading", "Heading", "short_text", { max_length: 90 }), F("facts", "Facts", "component", { required: true, multiple: true, allowed_types: ["fact"], min_items: 1, max_items: 6 })] },
      cta_banner: { label: "CTA banner", kind: "component", description: "A full-width band with a message and one call to action.", fields: [
        F("heading", "Heading", "short_text", { required: true, max_length: 90 }), F("text", "Text", "long_text", { max_length: 300 }),
        F("cta", "Call to action", "component", { required: true, allowed_types: ["cta_link"] }),
        F("theme", "Theme", "enum", { enum_values: ["light", "dark", "brand"], localized: false })] },
      faq_item: { label: "FAQ item", kind: "entry", description: "A reusable question and answer.", fields: [
        F("question", "Question", "short_text", { required: true, max_length: 200 }), F("answer", "Answer", "rich_text", { required: true }),
        F("topic", "Topic", "short_text", { max_length: 60 })] },
      faq: { label: "FAQ", kind: "component", description: "Question-and-answer section (renders FAQPage structured data).", fields: [
        F("heading", "Heading", "short_text", { max_length: 90 }),
        F("items", "Questions", "reference", { required: true, multiple: true, allowed_types: ["faq_item"], min_items: 1, max_items: 20 })] },
      testimonial: { label: "Testimonial", kind: "entry", description: "A customer quote with attribution.", fields: [
        F("quote", "Quote", "long_text", { required: true, max_length: 600 }), F("author_name", "Author name", "short_text", { required: true, max_length: 80 }),
        F("author_detail", "Author detail", "short_text", { max_length: 120, help: "e.g. member since 2019" }),
        F("rating", "Rating", "decimal", { min_value: 0, max_value: 5, localized: false }), F("photo", "Photo", "media", img)] },
      testimonial_list: { label: "Testimonials", kind: "component", description: "A set of customer testimonials.", fields: [
        F("heading", "Heading", "short_text", { max_length: 90 }),
        F("testimonials", "Testimonials", "reference", { required: true, multiple: true, allowed_types: ["testimonial"], min_items: 1, max_items: 9 })] },
      plan_feature: { label: "Plan feature", kind: "component", description: "One line of a plan's cover.", fields: [
        F("label", "Label", "short_text", { required: true, max_length: 80 }), F("value", "Value", "short_text", { max_length: 60 }),
        F("included", "Included", "boolean", { localized: false })] },
      pricing_plan: { label: "Pricing plan", kind: "entry", description: "A plan or tier with price and features.", fields: [
        F("name", "Name", "short_text", { required: true, max_length: 40 }), F("price", "Price", "decimal", { required: true, min_value: 0, localized: false }),
        F("currency", "Currency", "enum", { enum_values: ["USD", "EUR", "GBP"], localized: false }),
        F("billing_period", "Billing period", "enum", { enum_values: ["month", "year", "one_off"], localized: false }),
        F("summary", "Summary", "long_text", { max_length: 200 }), F("features", "Features", "component", { multiple: true, allowed_types: ["plan_feature"], max_items: 20 }),
        F("cta", "Call to action", "component", { allowed_types: ["cta_link"] }), F("highlighted", "Highlighted", "boolean", { localized: false })] },
      comparison_row: { label: "Comparison row", kind: "component", description: "One row of a comparison table.", fields: [
        F("label", "Label", "short_text", { required: true, max_length: 80 }), F("values", "Values", "short_text", { multiple: true, max_length: 60 })] },
      pricing_table: { label: "Pricing table", kind: "component", description: "Plans to choose from and an optional comparison table.", fields: [
        F("heading", "Heading", "short_text", { max_length: 90 }), F("intro", "Intro", "long_text", { max_length: 300 }),
        F("plans", "Plans", "reference", { required: true, multiple: true, allowed_types: ["pricing_plan"], min_items: 1, max_items: 6 }),
        F("comparison_rows", "Comparison rows", "component", { multiple: true, allowed_types: ["comparison_row"] }),
        F("footnote", "Footnote", "long_text", { max_length: 300 })] },
      author: { label: "Author", kind: "entry", description: "A named expert who writes or reviews content (E-E-A-T).", fields: [
        F("name", "Name", "short_text", { required: true, max_length: 80 }), F("job_title", "Job title", "short_text", { max_length: 80 }),
        F("bio", "Bio", "rich_text"), F("photo", "Photo", "media", img),
        F("profile_url", "Profile URL", "url", { pattern: URL_PATTERN, localized: false }), F("same_as", "Social profiles", "url", { multiple: true, localized: false })] },
      form_field: { label: "Form field", kind: "component", description: "One input of a form.", fields: [
        F("name", "Name", "short_text", { required: true, pattern: "^[a-z][a-z0-9_]*$", localized: false }), F("label", "Label", "short_text", { required: true, max_length: 80 }),
        F("input_type", "Input type", "enum", { required: true, enum_values: inputs, localized: false }), F("is_required", "Required", "boolean", { localized: false }),
        F("choices", "Choices", "short_text", { multiple: true, max_length: 80 }), F("autocomplete", "Autocomplete token", "short_text", { localized: false }),
        F("help_text", "Help text", "short_text", { max_length: 160 })] },
      form: { label: "Form", kind: "component", description: "A configurable form (newsletter, contact, quote).", fields: [
        F("title", "Title", "short_text", { max_length: 90 }), F("intro", "Intro", "long_text", { max_length: 300 }),
        F("fields", "Fields", "component", { required: true, multiple: true, allowed_types: ["form_field"], min_items: 1 }),
        F("submit_label", "Submit label", "short_text", { required: true, max_length: 30 }), F("success_message", "Success message", "long_text", { max_length: 300 }),
        F("endpoint", "Submission endpoint", "url", { pattern: URL_PATTERN, localized: false }), F("consent_text", "Consent text", "long_text")] },
      rich_text: { label: "Rich text", kind: "component", description: "A heading with formatted body copy.", fields: [
        F("heading", "Heading", "short_text", { max_length: 90 }), F("body", "Body", "rich_text", { required: true })] },
    };
  }
  const SECTION_TYPE = dict(Object.entries({ hero: "hero", card_grid: "card_grid", cta_banner: "cta_banner", faq: "faq", testimonial: "testimonial_list", pricing_table: "pricing_table", form: "form", key_facts: "key_facts", rich_text: "rich_text" }));
  const DEPENDENCIES = dict(Object.entries({ hero: ["cta_link"], card_grid: ["card", "cta_link"], cta_banner: ["cta_link"], faq: ["faq_item"], testimonial_list: ["testimonial"], pricing_table: ["pricing_plan", "plan_feature", "comparison_row", "cta_link"],
    form: ["form_field"], key_facts: ["fact"], pricing_plan: ["plan_feature", "cta_link"], card: ["cta_link"] }));
  const PAGE_LABELS = dict(Object.entries({ home_page: "Home page", product_page: "Product page", article_page: "Article", standard_page: "Standard page" }));

  function buildBaseline(pages, namespace) {
    const styles = uniqSorted(pages.flatMap((p) => p.button_styles || []));
    const inputs = uniqSorted(pages.flatMap((p) => p.components.filter((c) => c.type === "form").flatMap((c) => (c.fields.fields || []).map((f) => f.input_type))));
    const bp = blueprints(styles, inputs);
    const evidence = Object.create(null), needed = [];
    const ev = (k, s) => { (evidence[k] = evidence[k] || []).push(s); };
    const need = (tid) => { if (!needed.includes(tid)) { needed.push(tid); for (const d of DEPENDENCIES[tid] || []) need(d); } };
    const pagesByType = new Map();
    for (const p of pages) {
      if (!pagesByType.has(p.page_type)) pagesByType.set(p.page_type, []);
      pagesByType.get(p.page_type).push(p);
      for (const c of p.components) {
        const tid = SECTION_TYPE[c.type];
        if (tid) { need(tid); ev(tid, `${c.type} on ${p.path} (${c.selector}): ` + c.evidence.slice(0, 3).join("; ")); }
        else if (c.type === "author_bio") { need("author"); ev("author", `author bio on ${p.path} (${c.selector})`); }
        else if (c.type === "article_body") ev("article_page", `article body on ${p.path} (${c.selector}): ` + c.evidence.slice(0, 3).join("; "));
      }
    }
    const types = [];
    need("seo_metadata");
    for (const [pt, group] of [...pagesByType].sort((a, b) => cmp(a[0], b[0]))) {
      const comps = group.flatMap((p) => p.components);
      const hasHero = comps.some((c) => c.type === "hero");
      const sectionTypes = uniqSorted(comps.filter((c) => SECTION_TYPE[c.type] && c.type !== "hero").map((c) => SECTION_TYPE[c.type]));
      const fields = [F("title", "Title", "short_text", { required: true, max_length: 120 }),
        F("slug", "Slug", "short_text", { required: true, unique: true, pattern: SLUG_PATTERN, localized: false, help: "URL path, e.g. products/home-insurance" }),
        F("seo", "SEO", "component", { allowed_types: ["seo_metadata"] })];
      if (pt === "article_page") {
        need("author");
        fields.push(F("summary", "Summary", "long_text", { max_length: 300, help: "Shown in listings and as the meta description fallback" }),
          F("hero_image", "Hero image", "media", { media_kinds: ["image"] }), F("hero_image_alt", "Hero image alt text", "short_text", { max_length: 150 }),
          F("body", "Body", "rich_text", { required: true }), F("author", "Author", "reference", { required: true, allowed_types: ["author"] }),
          F("reviewed_by", "Reviewed by", "reference", { allowed_types: ["author"] }),
          F("published_date", "Published date", "date", { required: true, localized: false }), F("updated_date", "Updated date", "date", { localized: false }),
          F("reading_time_minutes", "Reading time (minutes)", "integer", { min_value: 1, max_value: 60, localized: false }),
          F("topics", "Topics", "short_text", { multiple: true, max_length: 40 }));
      }
      if (hasHero) fields.push(F("hero", "Hero", "component", { allowed_types: ["hero"] }));
      if (sectionTypes.length) fields.push(F("sections", "Sections", "component", { multiple: true, allowed_types: sectionTypes }));
      types.push({ id: pt, label: PAGE_LABELS[pt] || labelOf(pt), kind: "page", mixin: false, description: `${PAGE_LABELS[pt] || pt} template derived from ${group.length} analysed page(s): ` + group.slice(0, 5).map((p) => p.path).join(", "),
        display_field: "title", fields, evidence: [...(evidence[pt] || []), ...group.map((p) => `page ${p.path} (${uniqSorted(p.components.map((c) => c.type)).join(", ") || "no components"})`)] });
    }
    for (const tid of needed) {
      const b = bp[tid];
      types.push({ id: tid, label: b.label, kind: b.kind, mixin: !!b.mixin, description: b.description, display_field: null, fields: b.fields.map((f) => ({ ...f })), evidence: (evidence[tid] || []).slice(0, 8) });
    }
    return { namespace: snake(namespace), version: "1.0.0", types };
  }
  function normaliseModel(model, namespace) {
    const issues = [], types = [], idMap = new Map();
    const mapped = new Set();
    for (const t of model.types || []) {
      let tid = snake(t.id || t.label || "type");
      const base = tid;
      for (let n = 2; mapped.has(tid); n++) tid = `${base}_${n}`;
      idMap.set(t.id || tid, tid);
      mapped.add(tid);
      const kind = ["page", "entry", "component"].includes(t.kind) ? t.kind : "entry";
      types.push({ ...t, id: tid, kind, label: t.label || labelOf(tid), mixin: !!t.mixin, description: has(t, "description") ? t.description : "", evidence: [...(t.evidence || [])] });
    }
    const known = new Map(types.map((t) => [t.id, t]));
    for (const t of types) {
      const seen = new Set(), fields = [];
      for (const f of t.fields || []) {
        let fid = snake(f.id || f.label || "field");
        if (RESERVED.has(fid)) {
          const nw = fid === "url" ? "link" : f.multiple ? `${fid}_list` : `${fid}_value`;
          issues.push({ severity: "info", type: t.id, field: fid, message: `Renamed reserved field '${fid}' to '${nw}'.` });
          fid = nw;
        }
        const base = fid;
        for (let n = 2; seen.has(fid); n++) fid = `${base}_${n}`;
        seen.add(fid);
        const ok = FIELD_TYPES.includes(f.type);
        if (!ok) issues.push({ severity: "warn", type: t.id, field: fid, message: `Unknown field type '${pystr(f.type)}' mapped to short_text.` });
        const v = { min_length: null, max_length: null, pattern: null, min_value: null, max_value: null, enum_values: [], allowed_types: [], media_kinds: [], unique: false, min_items: null, max_items: null, ...(f.validations || {}) };
        v.allowed_types = (v.allowed_types || []).map((a) => (idMap.has(a) ? idMap.get(a) : snake(a)));
        fields.push({ id: fid, label: f.label || labelOf(fid), type: ok ? f.type : "short_text", required: !!f.required, multiple: !!f.multiple, localized: f.localized !== false, help: has(f, "help") ? f.help : "", validations: v });
      }
      t.fields = fields;
    }
    // references and components must resolve; kinds decide reference vs component
    for (const t of types) {
      const keep = [];
      for (const f of t.fields) {
        const v = f.validations;
        if (f.type === "reference" || f.type === "component") {
          const missing = v.allowed_types.filter((a) => !known.has(a));
          if (missing.length) issues.push({ severity: "warn", type: t.id, field: f.id, message: `Dropped unknown target type(s) ${pyRepr(missing)}.` });
          v.allowed_types = v.allowed_types.filter((a) => known.has(a));
          if (f.type === "component" && v.allowed_types.length && v.allowed_types.every((a) => known.get(a).kind !== "component")) {
            f.type = "reference";
            issues.push({ severity: "info", type: t.id, field: f.id, message: "Component field pointing at entries converted to a reference." });
          } else if (f.type === "reference" && v.allowed_types.length && v.allowed_types.every((a) => known.get(a).kind === "component")) {
            f.type = "component";
            issues.push({ severity: "info", type: t.id, field: f.id, message: "Reference to component types converted to an embedded component." });
          }
          if (f.type === "component" && !v.allowed_types.length) { issues.push({ severity: "warn", type: t.id, field: f.id, message: "Component field without a valid component type was removed." }); continue; }
        }
        if (f.type === "enum" && !(v.enum_values || []).length) { f.type = "short_text"; issues.push({ severity: "warn", type: t.id, field: f.id, message: "Enum without values converted to short text." }); }
        if (f.type === "media" && !(v.media_kinds || []).length) v.media_kinds = ["image"];
        if (f.type === "short_text" && (v.max_length === null || v.max_length === undefined) && !v.pattern) v.max_length = 255;
        keep.push(f);
      }
      t.fields = keep;
    }
    // page essentials
    for (const t of types) {
      if (t.kind !== "page") continue;
      const ids = t.fields.map((f) => f.id);
      if (!ids.includes("title")) { t.fields.splice(0, 0, F("title", "Title", "short_text", { required: true, max_length: 120 })); issues.push({ severity: "info", type: t.id, field: "title", message: "Added required title to page type." }); }
      if (!ids.includes("slug")) { t.fields.splice(1, 0, F("slug", "Slug", "short_text", { required: true, unique: true, pattern: SLUG_PATTERN, localized: false })); issues.push({ severity: "info", type: t.id, field: "slug", message: "Added required unique slug to page type." }); }
    }
    // component embedding cycles -> break with a reference (and make the target an entry)
    const graph = new Map(types.map((t) => [t.id, t.fields.filter((f) => f.type === "component").flatMap((f) => f.validations.allowed_types.map((a) => [f, a]))]));
    const state = new Map();
    const dfs = (node) => {
      state.set(node, 1);
      for (const [f, a] of graph.get(node) || []) {
        if (state.get(a) === 1) { f.type = "reference"; known.get(a).kind = "entry"; issues.push({ severity: "warn", type: node, field: f.id, message: `Component cycle via '${a}' broken: field converted to a reference and '${a}' to an entry.` }); }
        else if (!state.has(a)) dfs(a);
      }
      state.set(node, 2);
    };
    for (const t of types) if (!state.has(t.id)) dfs(t.id);
    // display field (every type gets one so CMS lists are readable)
    for (const t of types) {
      const texts = t.fields.filter((f) => f.type === "short_text" && !f.multiple).map((f) => f.id);
      if (!texts.includes(t.display_field)) {
        const req = t.fields.filter((f) => f.type === "short_text" && f.required && !f.multiple).map((f) => f.id);
        t.display_field = req.length ? req[0] : texts.length ? texts[0] : null;
      }
      if (t.display_field === null || t.display_field === undefined) {
        t.fields.splice(0, 0, F("internal_name", "Internal name", "short_text", { required: true, max_length: 120, localized: false, help: "Editor-facing name used in CMS lists" }));
        t.display_field = "internal_name";
        issues.push({ severity: "info", type: t.id, field: "internal_name", message: "Added an internal name as display field." });
      }
      if (t.fields.length > 50) issues.push({ severity: "warn", type: t.id, field: "", message: `${t.fields.length} fields exceeds the 50-field limit of some CMSs.` });
      if (!t.fields.length) issues.push({ severity: "error", type: t.id, field: "", message: "Type has no fields." });
    }
    return [{ namespace: snake(namespace || model.namespace || "site"), version: String(model.version || "1.0.0"), types }, issues];
  }
  /** ProposedModel.model_dump(): every TypeDef/FieldDef/Validations key present with its default. */
  function dumpProposal(p) {
    const V = (v) => Object.fromEntries(VKEYS.map((k) => { const x = U.isObj(v) && has(v, k) ? v[k] : undefined; return [k, VLISTS.has(k) ? (Array.isArray(x) ? x : []) : k === "unique" ? !!x : x ?? null]; }));
    return { types: (Array.isArray(p.types) ? p.types : []).filter(U.isObj).map((t) => ({ id: t.id, label: t.label, kind: t.kind, mixin: !!t.mixin, description: t.description ?? "", display_field: t.display_field ?? null,
      fields: (Array.isArray(t.fields) ? t.fields : []).filter(U.isObj).map((f) => ({ id: f.id, label: f.label, type: f.type, required: !!f.required, multiple: !!f.multiple, localized: f.localized ?? true, help: f.help ?? "", validations: V(f.validations) })),
      evidence: Array.isArray(t.evidence) ? t.evidence : [] })), rationale: p.rationale ?? "" };
  }
  function mergeProposal(baseline, proposal) {
    const notes = [];
    const propTypes = new Map();
    for (const t of proposal.types || []) if (t.id && (t.fields || []).length) propTypes.set(snake(t.id || ""), t);
    const merged = [];
    for (const t of baseline.types) {
      const p = propTypes.get(t.id);
      if (!p) { merged.push(t); notes.push(`kept baseline type ${t.id} (not in the model's proposal)`); continue; }
      propTypes.delete(t.id);
      const fields = [...p.fields];
      const propIds = new Set(fields.map((f) => snake(f.id || "")));
      for (const f of t.fields) if (f.required && !propIds.has(f.id)) fields.push(f); // required baseline fields survive
      const over = Object.fromEntries(["label", "description", "kind", "mixin"].filter((k) => has(p, k)).map((k) => [k, p[k]]));
      merged.push({ ...t, ...over, fields, evidence: t.evidence || [] });
    }
    for (const [tid, p] of propTypes) { merged.push({ ...p, evidence: ["proposed by the model from the page evidence"] }); notes.push(`added model-proposed type ${tid}`); }
    return [{ ...baseline, types: merged }, notes];
  }

  // ------------------------------------------------------------------ PyYAML safe_dump(sort_keys=False, allow_unicode=True, default_flow_style=False)
  const Y_BREAKS = "\n\x85  ", Y_WS = "\0 \t\r\n\x85  ";
  const Y_IMPLICIT = [ // SafeDumper's implicit resolvers (YAML 1.1) with their first characters
    [/^(?:yes|Yes|YES|no|No|NO|true|True|TRUE|false|False|FALSE|on|On|ON|off|Off|OFF)$/, "yYnNtTfFoO"],
    [/^(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?|\.[0-9_]+(?:[eE][-+][0-9]+)?|[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$/, "-+0123456789."],
    [/^(?:[-+]?0b[0-1_]+|[-+]?0[0-7_]+|[-+]?(?:0|[1-9][0-9_]*)|[-+]?0x[0-9a-fA-F_]+|[-+]?[1-9][0-9_]*(?::[0-5]?[0-9])+)$/, "-+0123456789"],
    [/^(?:<<)$/, "<"],
    [/^(?:~|null|Null|NULL)$/, "~nN"],
    [/^(?:[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]|[0-9][0-9][0-9][0-9]-[0-9][0-9]?-[0-9][0-9]?(?:[Tt]|[ \t]+)[0-9][0-9]?:[0-9][0-9]:[0-9][0-9](?:\.[0-9]*)?(?:[ \t]*(?:Z|[-+][0-9][0-9]?(?::[0-9][0-9])?))?)$/, "0123456789"],
    [/^(?:=)$/, "="],
  ];
  function yamlPlainIsStr(s) {
    if (s === "") return false;
    for (const [rx, first] of Y_IMPLICIT) if (first.includes(s[0]) && (rx.test(s) || (s.endsWith("\n") && rx.test(s.slice(0, -1))))) return false;
    return true;
  }
  function yamlAnalyze(s) {
    if (!s) return { empty: true, multiline: false, blockPlain: true, single: true };
    const t = [...s];
    let blockInd = false, flowInd = false, lineBreaks = false, special = false;
    let leadSpace = false, leadBreak = false, trailSpace = false, trailBreak = false, breakSpace = false, spaceBreak = false;
    if (s.startsWith("---") || s.startsWith("...")) { blockInd = true; flowInd = true; }
    let precededWs = true, followedWs = t.length === 1 || Y_WS.includes(t[1]), prevSpace = false, prevBreak = false;
    for (let i = 0; i < t.length; i++) {
      const ch = t[i], cp = ch.codePointAt(0);
      if (i === 0) {
        if ("#,[]{}&*!|>'\"%@`".includes(ch)) { flowInd = true; blockInd = true; }
        if (ch === "?" || ch === ":") { flowInd = true; if (followedWs) blockInd = true; }
        if (ch === "-" && followedWs) { flowInd = true; blockInd = true; }
      } else {
        if (",?[]{}".includes(ch)) flowInd = true;
        if (ch === ":") { flowInd = true; if (followedWs) blockInd = true; }
        if (ch === "#" && precededWs) { flowInd = true; blockInd = true; }
      }
      if (Y_BREAKS.includes(ch)) lineBreaks = true;
      if (!(ch === "\n" || (cp >= 0x20 && cp <= 0x7e))) {
        const uni = (cp === 0x85 || (cp >= 0xa0 && cp <= 0xd7ff) || (cp >= 0xe000 && cp <= 0xfffd) || (cp >= 0x10000 && cp < 0x10ffff)) && cp !== 0xfeff;
        if (!uni) special = true; // allow_unicode=True keeps printable unicode plain
      }
      if (ch === " ") { if (i === 0) leadSpace = true; if (i === t.length - 1) trailSpace = true; if (prevBreak) breakSpace = true; prevSpace = true; prevBreak = false; }
      else if (Y_BREAKS.includes(ch)) { if (i === 0) leadBreak = true; if (i === t.length - 1) trailBreak = true; if (prevSpace) spaceBreak = true; prevSpace = false; prevBreak = true; }
      else { prevSpace = false; prevBreak = false; }
      precededWs = Y_WS.includes(ch);
      followedWs = i + 2 >= t.length || Y_WS.includes(t[i + 2]);
    }
    let bp = true, sq = true;
    if (leadSpace || leadBreak || trailSpace || trailBreak) bp = false;
    if (breakSpace) bp = sq = false;
    if (spaceBreak || special) bp = sq = false;
    if (lineBreaks || blockInd) bp = false;
    return { empty: false, multiline: lineBreaks, blockPlain: bp, single: sq };
  }
  const Y_ESC = dict(Object.entries({ "\0": "0", "\x07": "a", "\x08": "b", "\t": "t", "\n": "n", "\x0b": "v", "\x0c": "f", "\r": "r", "\x1b": "e", '"': '"', "\\": "\\", "\x85": "N", "\xa0": "_", " ": "L", " ": "P" }));
  function yamlDump(data) {
    const BW = 80;
    let out = "", column = 0, whitespace = true, indention = true, indent = null;
    const indents = [];
    const put = (s, n) => { out += s; column += n === undefined ? plen(s) : n; };
    const lineBreak = (ch = "\n") => { out += ch; whitespace = true; indention = true; column = 0; };
    const writeIndent = () => {
      const ind = indent || 0;
      if (!indention || column > ind || (column === ind && !whitespace)) lineBreak();
      if (column < ind) { whitespace = true; out += " ".repeat(ind - column); column = ind; }
    };
    const indicator = (ind, needWs, ws = false, indt = false) => { put(whitespace || !needWs ? ind : " " + ind); whitespace = ws; indention = indention && indt; };
    const increase = (flow = false, indentless = false) => { indents.push(indent); if (indent === null) indent = flow ? 2 : 0; else if (!indentless) indent += 2; };
    const plain = (text, split) => {
      if (!text) return;
      if (!whitespace) put(" ");
      whitespace = false; indention = false;
      const t = [...text];
      let spaces = false, start = 0;
      for (let end = 0; end <= t.length; end++) {
        const ch = end < t.length ? t[end] : null;
        if (spaces) {
          if (ch !== " ") {
            if (start + 1 === end && column > BW && split) { writeIndent(); whitespace = false; indention = false; }
            else put(t.slice(start, end).join(""), end - start);
            start = end;
          }
        } else if (ch === null || " \n\x85  ".includes(ch)) { put(t.slice(start, end).join(""), end - start); start = end; }
        if (ch !== null) spaces = ch === " ";
      }
    };
    const single = (text, split) => {
      indicator("'", true);
      const t = [...text];
      let spaces = false, breaks = false, start = 0;
      for (let end = 0; end <= t.length; end++) {
        const ch = end < t.length ? t[end] : null;
        if (spaces) {
          if (ch === null || ch !== " ") {
            if (start + 1 === end && column > BW && split && start !== 0 && end !== t.length) writeIndent();
            else put(t.slice(start, end).join(""), end - start);
            start = end;
          }
        } else if (breaks) {
          if (ch === null || !Y_BREAKS.includes(ch)) {
            if (t[start] === "\n") lineBreak();
            for (const br of t.slice(start, end)) lineBreak(br);
            writeIndent();
            start = end;
          }
        } else if ((ch === null || " \n\x85  ".includes(ch) || ch === "'") && start < end) { put(t.slice(start, end).join(""), end - start); start = end; }
        if (ch === "'") { put("''", 2); start = end + 1; }
        if (ch !== null) { spaces = ch === " "; breaks = Y_BREAKS.includes(ch); }
      }
      indicator("'", false);
    };
    const double = (text, split) => {
      indicator('"', true);
      const t = [...text];
      let start = 0;
      const hex = (n, w) => n.toString(16).toUpperCase().padStart(w, "0");
      for (let end = 0; end <= t.length; end++) {
        const ch = end < t.length ? t[end] : null;
        const cp = ch === null ? 0 : ch.codePointAt(0);
        if (ch === null || '"\\\x85  ﻿'.includes(ch) || !((cp >= 0x20 && cp <= 0x7e) || (cp >= 0xa0 && cp <= 0xd7ff) || (cp >= 0xe000 && cp <= 0xfffd))) {
          if (start < end) { put(t.slice(start, end).join(""), end - start); start = end; }
          if (ch !== null) { put(has(Y_ESC, ch) ? "\\" + Y_ESC[ch] : cp <= 0xff ? "\\x" + hex(cp, 2) : cp <= 0xffff ? "\\u" + hex(cp, 4) : "\\U" + hex(cp, 8)); start = end + 1; }
        }
        if (end > 0 && end < t.length - 1 && (ch === " " || start >= end) && column + (end - start) > BW && split) {
          const d = t.slice(start, end).join("") + "\\";
          if (start < end) start = end;
          put(d);
          writeIndent(); whitespace = false; indention = false;
          if (t[start] === " ") put("\\");
        }
      }
      indicator('"', false);
    };
    const scalar = (v, simpleKey) => {
      const isStr = typeof v === "string";
      const text = v === null || v === undefined ? "null" : typeof v === "boolean" ? (v ? "true" : "false") : String(v);
      increase(true);
      const a = yamlAnalyze(text);
      const implicitPlain = !isStr || yamlPlainIsStr(text);
      const split = !simpleKey;
      if (implicitPlain && !(simpleKey && (a.empty || a.multiline)) && a.blockPlain) plain(text, split);
      else if (a.single && !(simpleKey && a.multiline)) single(text, split);
      else double(text, split);
      indent = indents.pop();
    };
    const node = (v, mapping = false) => {
      const isList = Array.isArray(v), isMap = U.isObj(v);
      if (!isList && !isMap) return scalar(v, false);
      if (isList ? !v.length : !Object.keys(v).length) { // empty collections are flow style
        indicator(isList ? "[" : "{", true, true); increase(true); indent = indents.pop(); indicator(isList ? "]" : "}", false);
        return;
      }
      if (isList) {
        increase(false, mapping && !indention);
        for (const item of v) { writeIndent(); indicator("-", true, false, true); node(item); }
      } else {
        increase(false);
        for (const [k, x] of Object.entries(v)) { writeIndent(); scalar(k, true); indicator(":", false); node(x, true); }
      }
      indent = indents.pop();
    };
    node(data);
    writeIndent(); // document end
    return out;
  }

  // ------------------------------------------------------------------ emitters
  class ModelEmitter {
    constructor(model, options) { this.model = model; this.options = U.isObj(options) ? options : {}; this.types = dict(model.types.map((t) => [t.id, t])); this.notes = []; }
    targets(f) { return f.validations.allowed_types.filter((a) => has(this.types, a)); }
    isLink(tid) {
      const t = this.types[tid] || {};
      const ids = new Set((t.fields || []).map((f) => f.id));
      return tid === "cta_link" || tid === "link" || (ids.has("label") && ids.has("link") && ids.size <= 6 && t.kind === "component");
    }
    opt(k) { return has(this.options, k) ? this.options[k] : undefined; }
  }
  class AemEmitter extends ModelEmitter {
    static target = "aem"; static label = "AEM Content Fragment Models";
    static TYPE = dict(Object.entries({ short_text: "text", long_text: "long-text", rich_text: "long-text", integer: "number", decimal: "float-number", boolean: "boolean", date: "date-time", datetime: "date-time",
      media: "content-reference", url: "text", email: "text", enum: "enumeration", reference: "content-fragment", component: "content-fragment", json: "json" }));
    emit() {
      const ns = this.model.namespace, conf = this.opt("configuration_folder") || `/conf/${kebab(ns)}`;
      const nn = (x) => x !== null && x !== undefined;
      const models = this.model.types.map((t) => ({
        name: t.label, description: pslice(t.description, 500), status: "enabled",
        fields: t.fields.map((f) => {
          const v = f.validations;
          const d = { type: AemEmitter.TYPE[f.type], name: camel(f.id), label: f.label, required: f.required, multiple: f.multiple, description: f.help };
          if (f.type === "short_text" && v.max_length) d.maxLength = v.max_length;
          if (f.type === "long_text" || f.type === "rich_text") d.mimeType = f.type === "rich_text" ? "text/html" : "text/plain";
          if (f.type === "integer" || f.type === "decimal") { if (nn(v.min_value)) d.min = v.min_value; if (nn(v.max_value)) d.max = v.max_value; }
          if (f.type === "date") d.dateTimeType = "date";
          if (f.type === "url" || f.type === "email" || v.pattern) { d.validationType = "custom"; d.customValidationRegex = v.pattern || (f.type === "url" ? URL_PATTERN : EMAIL_PATTERN); d.customErrorMessage = `Enter a valid ${f.label.toLowerCase()}`; }
          if (f.type === "enum") { d.values = v.enum_values.map((x) => ({ key: labelOf(x), value: x })); d.hint = "select"; }
          if (f.type === "media") { d.allowedContentTypes = v.media_kinds.length ? v.media_kinds : ["image"]; d.root = `/content/dam/${kebab(ns)}`; d.showThumbnail = true; }
          if (f.type === "reference" || f.type === "component") d.allowedModels = this.targets(f).map((a) => `${conf}/settings/dam/cfm/models/${kebab(a)}`);
          return d;
        }),
        "x-caf-model-path": `${conf}/settings/dam/cfm/models/${kebab(t.id)}`,
      }));
      const readme = `# AEM Content Fragment Models (${ns})\n\nPayload for the AEM Sites API: \`POST /adobe/sites/cf/models?dryRun=true\` validates it without creating models; drop \`dryRun\` to create them under the configuration folder.\n\nNotes:\n` +
        "- AEM has no embedded components; component/reference fields are content-fragment references to the listed models.\n" +
        "- `allowedModels`, `dateTimeType` and the `boolean`/`date-time`/`json` type identifiers follow the Sites API field schema; run the dry run against your AEM release and adjust if it reports an unknown property.\n" +
        "- `x-caf-model-path` is informational (the path the model is expected to get) and can be removed.\n";
      return [{ path: "aem/content-fragment-models.json", content: jsonPretty({ configurationFolder: conf, locked: false, models }) }, { path: "aem/README.md", content: readme }];
    }
  }
  // SHA-1 + RFC 4122 UUIDv5 (Python uuid.uuid5) for deterministic Sitecore item ids
  function sha1(bytes) {
    const padded = new Uint8Array(((bytes.length + 9 + 63) >> 6) << 6);
    padded.set(bytes); padded[bytes.length] = 0x80;
    const dv = new DataView(padded.buffer), bits = bytes.length * 8;
    dv.setUint32(padded.length - 4, bits >>> 0); dv.setUint32(padded.length - 8, Math.floor(bits / 4294967296));
    const h = [0x67452301, 0xefcdab89, 0x98badcfe, 0x10325476, 0xc3d2e1f0], w = new Uint32Array(80);
    for (let off = 0; off < padded.length; off += 64) {
      for (let i = 0; i < 16; i++) w[i] = dv.getUint32(off + i * 4);
      for (let i = 16; i < 80; i++) { const x = w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16]; w[i] = (x << 1) | (x >>> 31); }
      let [a, b, c, d, e] = h;
      for (let i = 0; i < 80; i++) {
        const f = i < 20 ? (b & c) | (~b & d) : i < 40 ? b ^ c ^ d : i < 60 ? (b & c) | (b & d) | (c & d) : b ^ c ^ d;
        const k = i < 20 ? 0x5a827999 : i < 40 ? 0x6ed9eba1 : i < 60 ? 0x8f1bbcdc : 0xca62c1d6;
        const t = ((((a << 5) | (a >>> 27)) >>> 0) + (f >>> 0) + e + k + w[i]) >>> 0;
        e = d; d = c; c = ((b << 30) | (b >>> 2)) >>> 0; b = a; a = t;
      }
      h[0] = (h[0] + a) >>> 0; h[1] = (h[1] + b) >>> 0; h[2] = (h[2] + c) >>> 0; h[3] = (h[3] + d) >>> 0; h[4] = (h[4] + e) >>> 0;
    }
    const out = new Uint8Array(20), odv = new DataView(out.buffer);
    h.forEach((x, i) => odv.setUint32(i * 4, x));
    return out;
  }
  const NAMESPACE_URL = "6ba7b8119dad11d180b400c04fd430c8";
  function uuid5(name, nsHex = NAMESPACE_URL) {
    const ns = new Uint8Array(nsHex.match(/../g).map((x) => parseInt(x, 16)));
    const nm = new TextEncoder().encode(name);
    const buf = new Uint8Array(ns.length + nm.length); buf.set(ns); buf.set(nm, ns.length);
    const b = sha1(buf).slice(0, 16);
    b[6] = (b[6] & 0x0f) | 0x50; b[8] = (b[8] & 0x3f) | 0x80;
    const hx = [...b].map((x) => x.toString(16).padStart(2, "0")).join("");
    return `${hx.slice(0, 8)}-${hx.slice(8, 12)}-${hx.slice(12, 16)}-${hx.slice(16, 20)}-${hx.slice(20)}`;
  }
  class SitecoreEmitter extends ModelEmitter {
    static target = "sitecore"; static label = "Sitecore templates (SCS)";
    static T_TEMPLATE = "ab86861a-6030-46c5-b394-e8f99e8b87db"; static T_SECTION = "e269fbb5-3750-427a-9149-7aa950b49301"; static T_FIELD = "455a3e98-a627-4b40-8035-e683a0331ac7";
    static T_FOLDER = "0437fee2-44c9-46a6-abe9-28858d9fee8c"; static F_BASE = "12c33f3f-86c5-43a5-aeb4-5598cec45116"; static F_TYPE = "ab162cc0-dc80-4abf-8871-998ee5d7ba32";
    static F_SOURCE = "1eb8ae32-e190-44a6-968d-ed904c794ebf"; static F_SHARED = "be351a73-fcb0-4213-93fa-c302d8ab4f51"; static F_SORT = "ba3f86a2-4a1c-4d78-b63d-91c2779c1b5e";
    static F_TITLE = "19a69332-a23e-4e70-8d16-b2640cb24cc8"; static STANDARD_TEMPLATE = "{1930BBEB-7805-471A-A3BE-4858AC7CF696}"; static PROJECT_FOLDER = "825b30b4-b40b-422e-9920-23a1b6bda89c";
    static TYPE = dict(Object.entries({ short_text: "Single-Line Text", long_text: "Multi-Line Text", rich_text: "Rich Text", integer: "Integer", decimal: "Number", boolean: "Checkbox", date: "Date", datetime: "Datetime",
      url: "General Link", email: "Single-Line Text", enum: "Droplist", json: "Multi-Line Text" }));
    gid(path) { return uuid5(`caf-sitecore:${String(path).toLowerCase()}`); }
    static item(iid, parent, template, path, shared = null, title = null) {
      const lines = ["---", `ID: "${iid}"`, `Parent: "${parent}"`, `Template: "${template}"`, `Path: ${path}`];
      if (shared && shared.length) {
        lines.push("SharedFields:");
        for (const [fid, hint, value] of shared) lines.push(`- ID: "${fid}"`, `  Hint: ${hint}`, `  Value: ${[...":{}[]#@"].some((c) => String(value).includes(c)) ? pyDumps(value) : value}`);
      }
      if (title) lines.push("Languages:", "- Language: en", "  Versions:", "  - Version: 1", "    Fields:", `    - ID: "${SitecoreEmitter.F_TITLE}"`, "      Hint: Title", `      Value: ${pyDumps(title)}`);
      return lines.join("\n") + "\n";
    }
    emit() {
      const S = SitecoreEmitter, ns = pascal(this.model.namespace);
      const rootPath = this.opt("path") || `/sitecore/templates/Project/${ns}`, parentId = this.opt("parent_id") || S.PROJECT_FOLDER, rootId = this.gid(rootPath);
      const files = [{ path: `sitecore/items/templates/${ns}.yml`, content: S.item(rootId, parentId, S.T_FOLDER, rootPath) }];
      const folders = { page: "Pages", entry: "Content Types", component: "Components" };
      for (const folder of Object.values(folders)) { const fp = `${rootPath}/${folder}`; files.push({ path: `sitecore/items/templates/${ns}/${folder}.yml`, content: S.item(this.gid(fp), rootId, S.T_FOLDER, fp) }); }
      const tpath = dict(this.model.types.map((t) => [t.id, `${rootPath}/${folders[t.kind]}/${pascal(t.id)}`]));
      const composition = [];
      for (const t of this.model.types) {
        const p = tpath[t.id], bases = [S.STANDARD_TEMPLATE], out = [];
        for (const f of t.fields) {
          if (f.type === "component") {
            const tg = this.targets(f);
            if (tg.length === 1 && !f.multiple && this.isLink(tg[0])) out.push([f, "General Link", ""]);
            else if (tg.length === 1 && this.types[tg[0]].mixin) bases.push("{" + this.gid(tpath[tg[0]]).toUpperCase() + "}");
            else if (t.kind === "page") composition.push(`- ${t.label}.${f.label}: placeholder '${snake(f.id)}' allowing renderings for ` + tg.map((a) => this.types[a].label).join(", ") + " (datasource templates under Components)");
            else out.push([f, f.multiple ? "Treelist" : "Droptree", "query:./*[" + tg.map((a) => `@@templatename='${pascal(a)}'`).join(" or ") + "]"]);
          } else if (f.type === "reference") {
            const tg = this.targets(f), root = this.opt("content_root");
            out.push([f, f.multiple ? "Treelist" : "Droptree", (root !== undefined ? pystr(root) : `/sitecore/content/${ns}/Data`) + "/" + (tg.length ? plural(pascal(tg[0])) : "Items")]);
          } else if (f.type === "media") out.push([f, f.validations.media_kinds.includes("image") ? "Image" : "File", ""]);
          else if (f.type === "enum") { out.push([f, "Droplist", `/sitecore/content/${ns}/Settings/Enumerations/${pascal(t.id)}${pascal(f.id)}`]); this.notes.push(`Create enumeration items ${pyRepr(f.validations.enum_values)} for ${t.label}.${f.label}`); }
          else if (f.multiple && (f.type === "short_text" || f.type === "url")) out.push([f, "Multi-Line Text", ""]);
          else out.push([f, S.TYPE[f.type], ""]);
        }
        files.push({ path: `sitecore/items/templates/${ns}/${folders[t.kind]}/${pascal(t.id)}.yml`, content: S.item(this.gid(p), this.gid(`${rootPath}/${folders[t.kind]}`), S.T_TEMPLATE, p, [[S.F_BASE, "__Base template", bases.join("|")]], t.label) });
        const sp = `${p}/Content`;
        files.push({ path: `sitecore/items/templates/${ns}/${folders[t.kind]}/${pascal(t.id)}/Content.yml`, content: S.item(this.gid(sp), this.gid(p), S.T_SECTION, sp) });
        out.forEach(([f, stype, src], i) => {
          const fp = `${sp}/${pascal(f.id)}`;
          const shared = [[S.F_TYPE, "Type", stype], [S.F_SORT, "__Sortorder", String((i + 1) * 100)]];
          if (src) shared.push([S.F_SOURCE, "Source", src]);
          if (!f.localized) shared.push([S.F_SHARED, "Shared", "1"]);
          files.push({ path: `sitecore/items/templates/${ns}/${folders[t.kind]}/${pascal(t.id)}/Content/${pascal(f.id)}.yml`, content: S.item(this.gid(fp), this.gid(sp), S.T_FIELD, fp, shared, f.label) });
        });
      }
      const module = { namespace: `Project.${ns}`, description: `${ns} content model generated by the Coforge Agentic Framework`, items: { path: "items", includes: [{ name: "templates", path: rootPath, allowedPushOperations: "CreateUpdateAndDelete" }] } };
      const required = this.model.types.flatMap((t) => t.fields.filter((f) => f.required).map((f) => `${t.label}.${f.label}`)).join(", ");
      const readme = `# Sitecore templates (${ns})\n\nSitecore Content Serialization module. Copy into your solution and run \`dotnet sitecore ser push\`.\n\n` +
        "- Item IDs are deterministic (UUIDv5 of the item path) so regenerating keeps IDs stable.\n" +
        `- The root folder's parent is \`${parentId}\` (\`/sitecore/templates/Project\` on a default install); pass \`emitter_options.sitecore.parent_id\` if yours differs.\n- Required fields: add Required validators in the Validation Rules of each field ` +
        "(SCS does not carry them here): " + pslice(required, 1500) + "\n\n## Page composition\nComponent lists on page templates map to placeholders and renderings with datasource templates:\n" +
        composition.join("\n") + "\n\n## Enumerations\n" + this.notes.map((n) => `- ${n}`).join("\n") + "\n";
      return [...files, { path: `sitecore/Project.${ns}.module.json`, content: jsonPretty(module, true) }, { path: "sitecore/README.md", content: readme }];
    }
  }
  class ContentfulEmitter extends ModelEmitter {
    static target = "contentful"; static label = "Contentful content types";
    static TYPE = dict(Object.entries({ short_text: "Symbol", long_text: "Text", rich_text: "RichText", integer: "Integer", decimal: "Number", boolean: "Boolean", date: "Date", datetime: "Date", url: "Symbol", email: "Symbol", enum: "Symbol", json: "Object" }));
    field(f) {
      const v = f.validations, nn = (x) => x !== null && x !== undefined;
      const range = (lo, hi) => Object.fromEntries([["min", lo], ["max", hi]].filter(([, x]) => nn(x)));
      let vals = [];
      if ((f.type === "short_text" || f.type === "long_text") && (v.max_length || v.min_length)) vals.push({ size: range(v.min_length, v.max_length) });
      if ((f.type === "integer" || f.type === "decimal") && (nn(v.min_value) || nn(v.max_value))) vals.push({ range: range(v.min_value, v.max_value) });
      if (f.type === "url" || (v.pattern && f.type === "short_text")) vals.push({ regexp: { pattern: v.pattern || URL_PATTERN } });
      if (f.type === "email") vals.push({ regexp: { pattern: "^\\w[\\w.-]*@([\\w-]+\\.)+[\\w-]+$" } });
      if (f.type === "enum") vals.push({ in: v.enum_values });
      if (v.unique && ["short_text", "integer", "decimal"].includes(f.type) && !f.multiple) vals.push({ unique: true });
      let base, itemVals;
      if (f.type === "media") { base = { type: "Link", linkType: "Asset" }; itemVals = [{ linkMimetypeGroup: v.media_kinds.length ? v.media_kinds : ["image"] }]; }
      else if (f.type === "reference" || f.type === "component") { base = { type: "Link", linkType: "Entry" }; itemVals = this.targets(f).length ? [{ linkContentType: this.targets(f).map(camel) }] : []; }
      else { base = { type: ContentfulEmitter.TYPE[f.type] }; itemVals = vals; vals = []; }
      const d = { id: camel(f.id), name: f.label, localized: f.localized, required: f.required, disabled: false, omitted: false };
      if (f.multiple) {
        const items = { ...base, validations: itemVals };
        if (items.type === "Text") items.type = "Symbol";
        Object.assign(d, { type: "Array", items, validations: v.min_items || v.max_items ? [{ size: range(v.min_items, v.max_items) }] : [] });
      } else Object.assign(d, { ...base, validations: base.type === "Link" ? [...itemVals, ...vals] : itemVals });
      return d;
    }
    emit() {
      const cts = [], js = ["// contentful-migration script: npx contentful-cli space migration --space-id <SPACE> 001-create-content-model.js", "module.exports = function (migration) {"];
      for (const t of this.model.types) {
        const fields = t.fields.map((f) => this.field(f));
        const ct = { sys: { id: camel(t.id), type: "ContentType" }, name: t.label, description: pslice(t.description, 500), displayField: t.display_field ? camel(t.display_field) : null, fields };
        cts.push(ct);
        const v = camel(t.id);
        js.push(`  const ${v} = migration.createContentType(${pyDumps(v)}).name(${pyDumps(t.label)}).description(${pyDumps(pslice(t.description, 500))});`);
        for (const d of fields) {
          let chain = `  ${v}.createField(${pyDumps(d.id)}).name(${pyDumps(d.name)}).type(${pyDumps(d.type)})`;
          if (d.linkType) chain += `.linkType(${pyDumps(d.linkType)})`;
          if (d.type === "Array") chain += `.items(${pyDumps(d.items)})`;
          chain += `.localized(${String(!!d.localized)}).required(${String(!!d.required)})`;
          if ((d.validations || []).length) chain += `.validations(${pyDumps(d.validations)})`;
          js.push(chain + ";");
        }
        if (ct.displayField) js.push(`  ${v}.displayField(${pyDumps(ct.displayField)});`);
      }
      js.push("};");
      return [{ path: "contentful/content-types.json", content: jsonPretty({ contentTypes: cts, editorInterfaces: [] }) }, { path: "contentful/migrations/001-create-content-model.js", content: js.join("\n") + "\n" }];
    }
  }
  class ContentstackEmitter extends ModelEmitter {
    static target = "contentstack"; static label = "Contentstack content types";
    schema(t, depth = 0, top = true) {
      const out = [], isCt = top && t.kind !== "component";
      if (isCt) { // Contentstack's built-in title (and url for pages)
        out.push({ display_name: "Title", uid: "title", data_type: "text", mandatory: true, unique: t.kind === "page", field_metadata: { _default: true }, multiple: false });
        if (t.kind === "page") out.push({ display_name: "URL", uid: "url", data_type: "text", mandatory: true, field_metadata: { _default: true }, multiple: false });
      }
      for (const f of t.fields) {
        if (isCt && (f.id === "title" || f.id === "slug")) continue;
        const v = f.validations, ft = f.type;
        const d = { display_name: f.label, uid: snake(f.id), mandatory: f.required, unique: !!v.unique, multiple: f.multiple, non_localizable: !f.localized, field_metadata: { description: f.help } };
        if (["short_text", "email", "url"].includes(ft)) { d.data_type = "text"; if (v.pattern || ft !== "short_text") d.format = v.pattern || (ft === "url" ? URL_PATTERN : EMAIL_PATTERN); }
        else if (ft === "long_text" || ft === "json") { d.data_type = "text"; d.field_metadata.multiline = true; }
        else if (ft === "rich_text") { d.data_type = "json"; Object.assign(d.field_metadata, { allow_json_rte: true, rich_text_type: "advanced" }); }
        else if (ft === "integer" || ft === "decimal") { d.data_type = "number"; if (v.min_value !== null && v.min_value !== undefined) d.min = v.min_value; if (v.max_value !== null && v.max_value !== undefined) d.max = v.max_value; }
        else if (ft === "boolean") d.data_type = "boolean";
        else if (ft === "date" || ft === "datetime") { d.data_type = "isodate"; d.field_metadata.hide_time = ft === "date"; }
        else if (ft === "media") { d.data_type = "file"; d.field_metadata.image = v.media_kinds.includes("image"); }
        else if (ft === "enum") Object.assign(d, { data_type: "text", display_type: "dropdown", enum: { advanced: false, choices: v.enum_values.map((x) => ({ value: x })) } });
        else if (ft === "reference") { const tg = this.targets(f); Object.assign(d, { data_type: "reference", reference_to: tg, field_metadata: { ...d.field_metadata, ref_multiple: f.multiple, ref_multiple_content_types: tg.length > 1 } }); }
        else if (ft === "component") {
          const tg = this.targets(f);
          if (top && tg.length > 1) Object.assign(d, { data_type: "blocks", multiple: true, blocks: tg.map((a) => ({ title: this.types[a].label, uid: snake(a), reference_to: snake(a) })) });
          else if (top) Object.assign(d, { data_type: "global_field", reference_to: snake(tg[0]) });
          else if (depth < 3) Object.assign(d, { data_type: "group", schema: this.schema(this.types[tg[0]], depth + 1, false) }); // inside a global field: inline group
          else continue;
        }
        out.push(d);
      }
      return out;
    }
    emit() {
      const files = [];
      for (const t of this.model.types) {
        if (t.kind === "component") files.push({ path: `contentstack/global_fields/${snake(t.id)}.json`, content: jsonPretty({ global_field: { title: t.label, uid: snake(t.id), description: pslice(t.description, 500), schema: this.schema(t, 0, false) } }) });
        else files.push({ path: `contentstack/content_types/${snake(t.id)}.json`, content: jsonPretty({ content_type: { title: t.label, uid: snake(t.id), description: pslice(t.description, 500), schema: this.schema(t),
          options: { title: true, publishable: true, is_page: t.kind === "page", singleton: false, sub_title: [], url_pattern: t.kind === "page" ? "/:title" : "", url_prefix: "/" } } }) });
      }
      const order = [...this.model.types.filter((t) => t.kind === "component"), ...this.model.types.filter((t) => t.kind !== "component")].map((t) => snake(t.id));
      files.push({ path: "contentstack/README.md", content: "# Contentstack\n\nCreate global fields first (POST /v3/global_fields), then content types (POST /v3/content_types) in this order:\n\n" + order.map((u, i) => `${i + 1}. ${u}`).join("\n") + "\n" });
      return files;
    }
  }
  class StrapiEmitter extends ModelEmitter {
    static target = "strapi"; static label = "Strapi schemas";
    compUid(tid) { const t = this.types[tid]; const cat = t.mixin || this.isLink(tid) || !this.sectionTypes.has(tid) ? "shared" : "sections"; return `${cat}.${kebab(tid)}`; }
    attr(f, inComponent) {
      const v = f.validations, ft = f.type, nn = (x) => x !== null && x !== undefined;
      if (f.id === "slug" && ft === "short_text") return { type: "uid", targetField: "title", required: f.required };
      if (ft === "short_text" || ft === "url") {
        if (f.multiple) return { type: "json", required: f.required };
        const d = { type: "string", required: f.required };
        if (v.max_length) d.maxLength = v.max_length;
        if (v.pattern || ft === "url") d.regex = v.pattern || URL_PATTERN;
        if (v.unique) d.unique = true;
        return d;
      }
      if (ft === "email") return { type: "email", required: f.required };
      if (ft === "long_text" || ft === "json") return { type: ft === "long_text" ? "text" : "json", required: f.required, ...(v.max_length && ft === "long_text" ? { maxLength: v.max_length } : {}) };
      if (ft === "rich_text") return { type: "blocks", required: f.required };
      if (ft === "integer" || ft === "decimal") { const d = { type: ft === "integer" ? "integer" : "decimal", required: f.required }; if (nn(v.min_value)) d.min = v.min_value; if (nn(v.max_value)) d.max = v.max_value; return d; }
      if (ft === "boolean") return { type: "boolean", default: false };
      if (ft === "date" || ft === "datetime") return { type: ft, required: f.required };
      if (ft === "enum") return { type: "enumeration", enum: v.enum_values, required: f.required };
      if (ft === "media") { const kinds = dict(Object.entries({ image: "images", video: "videos", document: "files" })); return { type: "media", multiple: f.multiple, required: f.required, allowedTypes: v.media_kinds.map((k) => (has(kinds, k) ? kinds[k] : "files")) }; }
      if (ft === "reference") {
        const tg = this.targets(f);
        if (!tg.length) return null;
        if (tg.length > 1) this.notes.push(`${f.id}: Strapi relations have one target; used ${tg[0]} (polymorphic targets ${pyRepr(tg)})`);
        return { type: "relation", relation: f.multiple ? "manyToMany" : "manyToOne", target: `api::${kebab(tg[0])}.${kebab(tg[0])}` };
      }
      if (ft === "component") {
        const tg = this.targets(f);
        if (f.multiple && tg.length > 1 && !inComponent) return { type: "dynamiczone", components: tg.map((a) => this.compUid(a)), required: f.required };
        if (tg.length > 1) this.notes.push(`${f.id}: dynamic zones are not allowed inside components; used ${tg[0]}`);
        const d = { type: "component", repeatable: f.multiple, component: this.compUid(tg[0]), required: f.required };
        if (f.multiple && v.min_items) d.min = v.min_items;
        if (f.multiple && v.max_items) d.max = v.max_items;
        return d;
      }
      return null;
    }
    emit() {
      this.sectionTypes = new Set(this.model.types.filter((t) => t.kind === "page").flatMap((t) => t.fields.filter((f) => f.id === "sections" || f.id === "hero").flatMap((f) => this.targets(f))));
      const files = [];
      for (const t of this.model.types) {
        const attrs = {};
        for (const f of t.fields) {
          const a = this.attr(f, t.kind === "component");
          if (a) { if (!f.localized && t.kind !== "component") a.pluginOptions = { i18n: { localized: false } }; attrs[camel(f.id)] = a; }
        }
        const k = kebab(t.id);
        if (t.kind === "component") {
          const [cat, name] = this.compUid(t.id).split(".");
          files.push({ path: `strapi/src/components/${cat}/${name}.json`, content: jsonPretty({ collectionName: `components_${cat}_${plural(snake(t.id))}`, info: { displayName: t.label, description: pslice(t.description, 500) }, options: {}, attributes: attrs }) });
        } else {
          const body = { kind: "collectionType", collectionName: plural(snake(t.id)), info: { singularName: k, pluralName: plural(k), displayName: t.label, description: pslice(t.description, 500) },
            options: { draftAndPublish: true }, pluginOptions: { i18n: { localized: true } }, attributes: attrs };
          const base = `strapi/src/api/${k}`;
          const stub = (fn) => `'use strict';\n\nconst { factories } = require('@strapi/strapi');\n\nmodule.exports = factories.${fn}('api::${k}.${k}');\n`;
          files.push({ path: `${base}/content-types/${k}/schema.json`, content: jsonPretty(body) }, { path: `${base}/routes/${k}.js`, content: stub("createCoreRouter") },
            { path: `${base}/controllers/${k}.js`, content: stub("createCoreController") }, { path: `${base}/services/${k}.js`, content: stub("createCoreService") });
        }
      }
      if (this.notes.length) files.push({ path: "strapi/README.md", content: "# Strapi\n\nCopy `src/` into your Strapi project.\n\nAdjustments made for Strapi:\n" + this.notes.map((n) => `- ${n}`).join("\n") + "\n" });
      return files;
    }
  }
  class DrupalEmitter extends ModelEmitter {
    static target = "drupal"; static label = "Drupal field config";
    static TYPE = dict(Object.entries({ short_text: ["string", "core"], long_text: ["string_long", "core"], rich_text: ["text_long", "text"], integer: ["integer", "core"], decimal: ["decimal", "core"], boolean: ["boolean", "core"],
      date: ["datetime", "datetime"], datetime: ["datetime", "datetime"], media: ["entity_reference", "core"], url: ["link", "link"], email: ["email", "core"], enum: ["list_string", "options"],
      reference: ["entity_reference", "core"], component: ["entity_reference_revisions", "entity_reference_revisions"], json: ["string_long", "core"] }));
    fieldName(fid, entity, storageType, registry) {
      let name = ("field_" + snake(fid)).slice(0, 32);
      const base = name;
      for (let n = 2; registry.has(`${entity}|${name}`) && registry.get(`${entity}|${name}`) !== storageType; n++) name = `${base.slice(0, 29)}_${n}`;
      return name;
    }
    emit() {
      const files = [], storages = new Map(), modulesNeeded = new Set();
      const bundleMap = (ids) => dict(ids.map((a) => [snake(a), snake(a)]));
      for (const t of this.model.types) {
        const entity = t.kind === "component" ? "paragraph" : "node", bundle = snake(t.id).slice(0, 32);
        if (entity === "node") files.push({ path: `drupal/config/sync/node.type.${bundle}.yml`, content: yamlDump({ langcode: "en", status: true, dependencies: {}, name: t.label, type: bundle, description: pslice(t.description, 500), help: "", new_revision: true, preview_mode: 1, display_submitted: t.kind === "page" }) });
        else { modulesNeeded.add("paragraphs"); files.push({ path: `drupal/config/sync/paragraphs.paragraphs_type.${bundle}.yml`, content: yamlDump({ langcode: "en", status: true, dependencies: {}, id: bundle, label: t.label, icon_uuid: null, icon_default: null, description: pslice(t.description, 500), behavior_plugins: {} }) }); }
        for (const f of t.fields) {
          if (entity === "node" && f.id === "title") continue; // node base field
          if (entity === "node" && f.id === "slug") { this.notes.push(`${t.label}.slug: use the path alias (Pathauto pattern) instead of a field`); continue; }
          const [ftype, module] = DrupalEmitter.TYPE[f.type];
          const v = f.validations;
          let storageSettings = {}, fieldSettings = {};
          const depsModule = new Set(module === "core" ? [] : [module]);
          if (f.type === "short_text") storageSettings = { max_length: Math.trunc(v.max_length || 255), case_sensitive: false, is_ascii: false };
          else if (f.type === "long_text" || f.type === "json") storageSettings = { case_sensitive: false };
          else if (f.type === "rich_text") fieldSettings = { allowed_formats: [] };
          else if (f.type === "decimal") { storageSettings = { precision: 10, scale: 2 }; fieldSettings = { min: v.min_value ?? null, max: v.max_value ?? null, prefix: "", suffix: "" }; }
          else if (f.type === "integer") { storageSettings = { unsigned: false, size: "normal" }; fieldSettings = { min: v.min_value ?? null, max: v.max_value ?? null, prefix: "", suffix: "" }; }
          else if (f.type === "boolean") fieldSettings = { on_label: "On", off_label: "Off" };
          else if (f.type === "date" || f.type === "datetime") storageSettings = { datetime_type: f.type === "date" ? "date" : "datetime" };
          else if (f.type === "url") fieldSettings = { title: 0, link_type: 17 };
          else if (f.type === "enum") storageSettings = { allowed_values: v.enum_values.map((x) => ({ value: x, label: labelOf(x) })), allowed_values_function: "" };
          else if (f.type === "media") {
            modulesNeeded.add("media"); depsModule.add("media"); storageSettings = { target_type: "media" };
            const bundles = dict(Object.entries({ image: "image", video: "remote_video", document: "document" }));
            fieldSettings = { handler: "default:media", handler_settings: { target_bundles: dict(v.media_kinds.filter((k) => has(bundles, k)).map((k) => [bundles[k], bundles[k]])) } };
          } else if (f.type === "reference") { storageSettings = { target_type: "node" }; fieldSettings = { handler: "default:node", handler_settings: { target_bundles: bundleMap(this.targets(f)) } }; }
          else if (f.type === "component") {
            modulesNeeded.add("paragraphs"); modulesNeeded.add("entity_reference_revisions"); depsModule.add("paragraphs"); storageSettings = { target_type: "paragraph" };
            fieldSettings = { handler: "default:paragraph", handler_settings: { negate: 0, target_bundles: bundleMap(this.targets(f)),
              target_bundles_drag_drop: dict(this.targets(f).map((a, i) => [snake(a), { enabled: true, weight: i }])) } };
          }
          const name = this.fieldName(f.id, entity, ftype, storages);
          if (!storages.has(`${entity}|${name}`)) {
            storages.set(`${entity}|${name}`, ftype);
            files.push({ path: `drupal/config/sync/field.storage.${entity}.${name}.yml`, content: yamlDump({ langcode: "en", status: true, dependencies: { module: uniqSorted([...depsModule, entity === "node" ? entity : "paragraphs"]) },
              id: `${entity}.${name}`, field_name: name, entity_type: entity, type: ftype, settings: storageSettings, module, locked: false, cardinality: f.multiple ? -1 : 1, translatable: true, indexes: {}, persist_with_no_fields: false, custom_storage: false }) });
          }
          const bundleCfg = entity === "node" ? `node.type.${bundle}` : `paragraphs.paragraphs_type.${bundle}`;
          files.push({ path: `drupal/config/sync/field.field.${entity}.${bundle}.${name}.yml`, content: yamlDump({ langcode: "en", status: true, dependencies: { config: [`field.storage.${entity}.${name}`, bundleCfg], module: uniqSorted([...depsModule]) },
            id: `${entity}.${bundle}.${name}`, field_name: name, entity_type: entity, bundle, label: f.label, description: f.help, required: f.required, translatable: f.localized, default_value: [],
            default_value_callback: "", settings: fieldSettings, field_type: ftype }) });
        }
      }
      const readme = "# Drupal configuration\n\nCopy `config/sync/*.yml` into your sync directory and run `drush config:import --partial`.\n\n" +
        `Required contributed modules: ${uniqSorted([...modulesNeeded]).join(", ") || "none"} (plus core: text, link, options, datetime, media).\n\n` + this.notes.map((n) => `- ${n}`).join("\n") + "\n";
      return [...files, { path: "drupal/README.md", content: readme }];
    }
  }
  class GraphqlEmitter extends ModelEmitter {
    static target = "graphql"; static label = "GraphQL SDL";
    static SCALAR = dict(Object.entries({ short_text: "String", long_text: "String", integer: "Int", decimal: "Float", boolean: "Boolean", date: "Date", datetime: "DateTime", url: "String", email: "String", json: "JSON", rich_text: "RichText", media: "Asset" }));
    emit() {
      const out = ['"""Generated by the Coforge Agentic Framework from the platform-neutral content model."""', "scalar Date", "scalar DateTime", "scalar JSON", "",
        "type Asset {\n  url: String!\n  title: String\n  description: String\n  contentType: String\n  width: Int\n  height: Int\n}", "", "type RichText {\n  json: JSON!\n  html: String\n}", ""];
      const enums = [], unions = [];
      for (const t of this.model.types) {
        const name = pascal(t.id), lines = [];
        if (t.kind !== "component") lines.push("  id: ID!");
        for (const f of t.fields) {
          const tg = this.targets(f);
          let gql;
          if (f.type === "enum") {
            const ename = `${name}${pascal(f.id)}`;
            const vals = f.validations.enum_values.map((x) => String(x).replace(/[^\p{L}\p{N}_]/gu, "_").toUpperCase() || "VALUE").map((x) => (/^\p{L}/u.test(x) ? x : `V_${x}`));
            enums.push(`enum ${ename} {\n` + [...new Set(vals)].map((x) => `  ${x}`).join("\n") + "\n}");
            gql = ename;
          } else if (f.type === "reference" || f.type === "component") {
            if (!tg.length) continue;
            if (tg.length > 1) { const uname = `${name}${pascal(f.id)}Item`; unions.push(`union ${uname} = ` + tg.map(pascal).join(" | ")); gql = uname; }
            else gql = pascal(tg[0]);
          } else gql = GraphqlEmitter.SCALAR[f.type];
          const typ = f.multiple ? `[${gql}!]!` : f.required ? `${gql}!` : gql;
          lines.push(`${f.help ? `  """${f.help}"""\n` : ""}  ${camel(f.id)}: ${typ}`);
        }
        out.push(`"""${t.label} (${t.kind}). ${pslice(t.description, 200)}"""\ntype ${name} {\n` + lines.join("\n") + "\n}", "");
      }
      const query = ["type Query {"];
      for (const t of this.model.types) {
        if (t.kind === "component") continue;
        const n = pascal(t.id), arg = t.kind === "page" ? "slug: String!" : "id: ID!";
        query.push(`  ${camel(t.id)}(${arg}): ${n}`, `  ${camel(t.id)}Collection(limit: Int = 20, skip: Int = 0): [${n}!]!`);
      }
      query.push("}");
      return [{ path: "graphql/schema.graphql", content: [...out, ...enums, "", ...unions, "", ...query].join("\n") + "\n" }];
    }
  }
  const EMITTERS = dict([AemEmitter, SitecoreEmitter, ContentfulEmitter, ContentstackEmitter, StrapiEmitter, DrupalEmitter, GraphqlEmitter].map((E) => [E.target, E]));
  /** Every referenced named type is defined (a cheap structural check without a GraphQL library). */
  function validateSdl(sdl) {
    const defined = new Set([...[...sdl.matchAll(/^(?:type|enum|union|scalar|input|interface)\s+(\w+)/gm)].map((m) => m[1]), "String", "Int", "Float", "Boolean", "ID"]);
    const body = sdl.replace(/"""[\s\S]*?"""/g, "");
    const used = new Set([...body.matchAll(/:\s*\[?(\w+)/g)].map((m) => m[1]));
    for (const m of body.matchAll(/union\s+\w+\s*=\s*([^\n]+)/g)) for (const u of m[1].split("|")) used.add(pystrip(u));
    return [...used].filter((u) => u && !defined.has(u)).sort(cmp);
  }
  function fieldRule(f) {
    const v = f.validations, parts = [];
    if ((v.allowed_types || []).length) parts.push("-> " + v.allowed_types.join(" | "));
    if (v.max_length) parts.push(`max ${pystr(v.max_length)} chars`);
    if ((v.min_value !== null && v.min_value !== undefined) || (v.max_value !== null && v.max_value !== undefined)) parts.push(`range ${pystr(v.min_value)}..${pystr(v.max_value)}`);
    if ((v.enum_values || []).length) parts.push("one of " + v.enum_values.join(", "));
    if (v.pattern) parts.push(`pattern ${v.pattern}`);
    if (v.unique) parts.push("unique");
    if (v.min_items || v.max_items) parts.push(`${pystr(v.min_items || 0)}-${pystr(v.max_items || "n")} items`);
    if ((v.media_kinds || []).length) parts.push(v.media_kinds.join("/"));
    return parts.join("; ");
  }

  // ------------------------------------------------------------------ demo-site fetch + BFS crawl (HttpxCrawler over FixtureTransport)
  const fixtureMeta = () => { try { return JSON.parse((CAF.site.get("/_fixture.json") || {}).html || "{}"); } catch { return {}; } };
  function hostOf(url) { try { return new URL(url).hostname.toLowerCase(); } catch { return ""; } }
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
      try { u = new URL(current); } catch { return { url, final_url: current, status: 0, headers: {}, html: "", redirects, error: "invalid URL" }; }
      if (u.hostname.toLowerCase() !== CAF.DEMO_HOST) return { url, final_url: current, status: 0, headers: {}, html: "", redirects, error: "external URL (the hosted console cannot fetch it)" };
      const path = u.pathname || "/";
      const hop = (meta.redirects || {})[path];
      if (hop) { redirects.push([current, Number(hop[1])]); current = new URL(hop[0], current).toString(); continue; }
      const r = CAF.site.get(path);
      const ct = contentType(r.path, r.status);
      return { url, final_url: current, status: r.status, headers: { "content-type": ct }, html: /text|xml|json/.test(ct) ? r.html : "", redirects, error: null };
    }
    return { url, final_url: current, status: 0, headers: {}, html: "", redirects, error: "too many redirects" };
  }
  const isHtml = (p) => /html/.test((p.headers || {})["content-type"] || "") || /^(<!doctype html|<html)/i.test(String(p.html || "").trimStart());
  const pageOk = (p) => !p.error && p.status >= 200 && p.status < 300;
  function pageLinks(p) {
    const out = [];
    for (const a of CAF.lib.html.parse(p.html).querySelectorAll("a[href]")) {
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

  // ------------------------------------------------------------------ HTML report (REPORT_TEMPLATE, jinja2 autoescape + trim_blocks + lstrip_blocks)
  const J = (v) => String(typeof v === "string" ? v : pystr(v)).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&#34;", "'": "&#39;" }[c]));
  function renderReport(c) {
    return `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Content model - ${J(c.namespace)}</title>
<style>
:root{--ink:#18212f;--muted:#5d6676;--line:#e2e5eb;--bg:#f5f6f8;--card:#fff;--accent:#14532d;--page:#1f4e8c;--entry:#7a2e0e;--component:#3b2f80}
*{box-sizing:border-box}body{margin:0;font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:var(--ink);background:var(--bg)}
header{background:var(--accent);color:#fff;padding:28px 32px 60px}header h1{margin:0 0 4px;font-size:24px}header p{margin:0;opacity:.88}
main{max-width:1160px;margin:-40px auto 0;padding:0 20px 48px}section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px 22px;margin-bottom:16px}
h2{font-size:18px;margin:0 0 10px}h3{font-size:16px;margin:0 0 6px}table{width:100%;border-collapse:collapse;font-size:13.5px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.03em}
.k{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;font-weight:600;color:#fff}.page{background:var(--page)}.entry{background:var(--entry)}.component{background:var(--component)}
code{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12.5px}.muted{color:var(--muted)}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}
.ev{font-size:12.5px;color:var(--muted);margin:4px 0 8px 16px;padding:0}
</style></head><body>
<header><h1>Content model: ${J(c.namespace)}</h1><p>${J(c.type_count)} types &middot; ${J(c.page_count)} page(s) analysed &middot; model source: ${J(c.model_source)} &middot; targets: ${J(c.targets)}</p></header>
<main>
<section><h2>Component inventory</h2><div class="grid">${c.pages.map((p) => `<div><h3>${J(p.path)} <span class="muted">(${J(p.page_type)})</span></h3>
<ul class="ev">${p.components.map((x) => `<li><b>${J(x.type)}</b> <code>${J(x.selector)}</code> - ${J(x.evidence.join("; "))}</li>`).join("")}</ul></div>`).join("")}</div></section>
${c.types.map((t) => `<section><h2><span class="k ${J(t.kind)}">${J(t.kind)}</span> ${J(t.label)} <code class="muted">${J(t.id)}</code></h2><p>${J(t.description)}</p>
<table><thead><tr><th>Field</th><th>Type</th><th>Required</th><th>Multiple</th><th>Localised</th><th>Validation / target</th></tr></thead><tbody>
${t.fields.map((f) => `<tr><td><b>${J(f.label)}</b> <code>${J(f.id)}</code>${f.id === t.display_field ? ' <span class="muted">(display)</span>' : ""}</td><td>${J(f.type)}</td><td>${f.required ? "yes" : ""}</td><td>${f.multiple ? "yes" : ""}</td><td>${f.localized ? "yes" : ""}</td><td class="muted">${J(f.rule)}</td></tr>`).join("")}</tbody></table>${t.evidence.length ? `<ul class="ev">${t.evidence.map((e) => `<li>${J(e)}</li>`).join("")}</ul>` : ""}</section>`).join("")}<section><h2>Emitted artefacts</h2><table><thead><tr><th>Target</th><th>Files</th></tr></thead><tbody>${c.emitted.map((e) => `<tr><td>${J(e.label)}</td><td><code>${J(e.files.join(", "))}</code></td></tr>`).join("")}</tbody></table></section>
${c.issues.length ? `<section><h2>Normalisation notes</h2><ul>${c.issues.map((i) => `<li>[${J(i.severity)}] ${J(i.type)}${i.field ? "." : ""}${J(i.field)}: ${J(i.message)}</li>`).join("")}</ul></section>` : ""}</main></body></html>`;
  }
  const mimeOf = (p) => (p.endsWith(".json") ? "application/json" : p.endsWith(".md") ? "text/markdown" : p.endsWith(".js") ? "text/javascript" : "text/plain");

  // ------------------------------------------------------------------ the agent
  CAF.agent(AGENT, {
    async run(input, ctx) {
      const urls = Array.isArray(input.urls) ? input.urls : [];
      if (!urls.length && !input.html && !input.image_path && !input.image_base64) throw new Error("Provide urls, html or a design image.");
      const namespace = input.namespace ?? "site";
      // 1. analyse
      const pages = [];
      if (input.html) pages.push(detectComponents(input.page_url || PLACEHOLDER, String(input.html)));
      if (urls.length) {
        let fetched;
        if (input.crawl) {
          ctx.progress(`Crawling the demo site from ${urls[0]}`);
          if (!CAF.site.isDemo(urls[0])) ctx.page({ url: urls[0] });
          fetched = crawlSite(urls[0], Number(input.max_pages ?? 10)).filter((p) => pageOk(p) && isHtml(p));
        } else {
          ctx.progress(`Analysing ${Math.min(urls.length, 25)} page(s)`);
          fetched = urls.slice(0, 25).map((u) => { if (!CAF.site.isDemo(u)) ctx.page({ url: u }); return demoFetch(u); });
        }
        for (const p of fetched) {
          if (pageOk(p) && p.html) pages.push(detectComponents(p.final_url || p.url, p.html));
          else ctx.warn(`Skipped ${p.url}: ${p.error || p.status}`);
        }
        if (!fetched.some(pageOk)) ctx.degraded("no page could be fetched");
      }
      // 2. vision: design screenshots are not analysed in the hosted console
      if (input.image_base64 || input.image_path) {
        ctx.warn("Design image supplied but the hosted console has no vision step; image not analysed (paste the page HTML, or run the Python service with a vision-capable model).");
        ctx.degraded("design image not analysed (no model)");
      }
      // 3. propose: the model refines the deterministic baseline and never loses a detected type
      let raw = buildBaseline(pages, namespace);
      let source = "baseline";
      if (pages.length) {
        const evidence = pages.map((p) => `${p.path} (${p.page_type}; JSON-LD: ${p.jsonld_types.join(", ") || "none"}): ` + p.components.map((c) => `${c.type} ${c.selector} [${c.evidence.slice(0, 2).join(", ")}]`).join("; "));
        const compact = raw.types.map((t) => ({ id: t.id, kind: t.kind, fields: t.fields.map((f) => `${f.id}:${f.type}${f.required ? "*" : ""}${f.multiple ? "[]" : ""}` + (f.validations.allowed_types.length ? `->${f.validations.allowed_types.join("|")}` : "")) }));
        ctx.progress("Asking the model to refine the baseline content model");
        const proposal = await ctx.generate("propose", { namespace: raw.namespace, evidence, baseline: pyDumps(compact, false), field_types: [...FIELD_TYPES].sort(cmp) });
        const dumped = dumpProposal(proposal || {});
        if (ctx.llmLive && dumped.types.length) { [raw] = mergeProposal(raw, dumped); source = "model"; }
      }
      // 4. normalise
      const [model, issues] = normaliseModel(raw, namespace);
      for (const i of issues) ctx.check({ rule: "model.normalise", status: { info: "info", warn: "warn", error: "fail" }[i.severity], severity: i.severity === "info" ? "low" : "medium", message: `${i.type}${i.field ? "." + i.field : ""}: ${i.message}` });
      const nRef = model.types.reduce((s, t) => s + t.fields.filter((f) => f.type === "reference" || f.type === "component").length, 0);
      ctx.check({ rule: "model.references_resolve", status: "pass", severity: "high", message: `All ${nRef} reference/component fields resolve to defined types.` });
      // 5. emit: one artifact per emitted file (folders kept, no zip)
      const emitted = [], fileArtifacts = [];
      for (const target of input.targets && input.targets.length ? input.targets : TARGETS) {
        const Em = EMITTERS[target];
        if (!Em) continue;
        const emitter = new Em(model, (input.emitter_options || {})[target]);
        let files;
        try { files = emitter.emit(); } catch (e) { ctx.check({ rule: `emit.${target}`, status: "fail", severity: "high", message: `${Em.label} failed: ${(e && e.name) || "Error"}: ${(e && e.message) || e}` }); continue; }
        for (const f of files) if (f.path.endsWith(".json")) { try { JSON.parse(f.content); } catch (e) { ctx.check({ rule: `emit.${target}.parse`, status: "fail", severity: "high", message: `${f.path} does not parse: ${e.message}` }); } }
        if (target === "graphql") {
          const missing = validateSdl(files[0].content);
          ctx.check({ rule: "emit.graphql.sdl", status: missing.length ? "fail" : "pass", severity: "high", message: missing.length ? `Undefined GraphQL types: ${pyRepr(missing)}` : "Every GraphQL type reference is defined." });
        }
        for (const f of files) fileArtifacts.push({ name: f.path, content: f.content, mime: mimeOf(f.path), description: `${Em.label}: ${f.path.slice(f.path.indexOf("/") + 1)}` });
        emitted.push({ target, label: Em.label, files: files.map((f) => f.path), artifact: `${target}/` });
        ctx.check({ rule: `emit.${target}`, status: "pass", severity: "medium", message: `${Em.label}: ${files.length} file(s)` });
      }
      // 6. report
      const inventory = new Map();
      for (const p of pages) for (const c of p.components) inventory.set(c.type, (inventory.get(c.type) || 0) + 1);
      const findings = [];
      for (const p of pages) for (const c of p.components) if (c.type === "hero" && c.fields.image && (c.fields.image_alt === null || c.fields.image_alt === undefined))
        findings.push({ title: `Hero image without alt text on ${p.path}`, category: "content", severity: "medium", detail: "The model makes image_alt a field on Hero; populate it during migration.", evidence: c.selector, target: p.url, source_agent: AGENT, recommendation: "Fill hero.image_alt for every migrated hero." });
      const common = [...inventory].sort((a, b) => b[1] - a[1]);
      findings.push({ title: `Content model: ${model.types.length} types from ${pages.length} page(s)`, category: "content", severity: "low", detail: common.map(([k, v]) => `${k} x${v}`).join(", "), evidence: model.types.map((t) => t.id).join("; "),
        recommendation: "Review the model in a modelling workshop before importing.", source_agent: AGENT });
      const outType = (t) => ({ id: t.id, label: t.label, kind: t.kind, mixin: !!t.mixin, description: t.description ?? "", display_field: t.display_field ?? null,
        fields: t.fields.map((f) => ({ id: f.id, label: f.label, type: f.type, required: f.required, multiple: f.multiple, localized: f.localized, help: f.help ?? "", validations: Object.fromEntries(VKEYS.map((k) => [k, f.validations[k] === undefined ? (VLISTS.has(k) ? [] : k === "unique" ? false : null) : f.validations[k]])) })),
        evidence: t.evidence || [] });
      const modelOut = { namespace: model.namespace, version: model.version, types: model.types.map(outType) };
      const output = {
        namespace: model.namespace,
        pages: pages.map((p) => ({ url: p.url, title: p.title, page_type: p.page_type, jsonld_types: p.jsonld_types, components: p.components.map((c) => ({ type: c.type, selector: c.selector, confidence: c.confidence, items: c.items || 0, evidence: c.evidence })) })),
        component_inventory: Object.fromEntries(inventory), model: modelOut, model_source: source, normalisation_issues: issues, emitted, vision_used: false, findings,
      };
      ctx.artifact({ name: "content_model.json", mime: "application/json", content: jsonPretty(model), description: "Platform-neutral intermediate content model" });
      ctx.artifact({ name: "content_model.html", mime: "text/html", description: "Content model overview", content: renderReport({ namespace: model.namespace, type_count: model.types.length, page_count: pages.length, model_source: source,
        targets: emitted.map((e) => e.target).join(", "), pages, types: model.types.map((t) => ({ ...t, fields: t.fields.map((f) => ({ ...f, rule: fieldRule(f) })) })), emitted, issues }) });
      for (const a of fileArtifacts) ctx.artifact(a);
      const kinds = { page: 0, entry: 0, component: 0 };
      for (const t of model.types) kinds[t.kind] = (kinds[t.kind] || 0) + 1;
      const nComps = [...inventory.values()].reduce((s, x) => s + x, 0);
      const sdl = (fileArtifacts.find((a) => a.name === "graphql/schema.graphql") || {}).content;
      const view = [
        { type: "kv", title: "Content model", data: { namespace: model.namespace, types: `${model.types.length} (${kinds.page} page, ${kinds.entry} entry, ${kinds.component} component)`, pages_analysed: pages.length, components_detected: nComps, model_source: source, files_emitted: fileArtifacts.length } },
        { type: "table", title: "Detected components", rows: pages.flatMap((p) => p.components.map((c) => ({ page: p.path, page_type: p.page_type, component: c.type, selector: c.selector, confidence: c.confidence, evidence: c.evidence.join("; ") }))) },
        { type: "table", title: "Content types", rows: modelOut.types.map((t) => ({ kind: t.kind, id: t.id, label: t.label, fields: t.fields.length, display_field: t.display_field || "", references: t.fields.filter((f) => ["reference", "component"].includes(f.type)).map((f) => `${f.id} -> ${f.validations.allowed_types.join("|")}`).join("; ") })) },
        { type: "table", title: "Emitted targets", rows: emitted.map((e) => ({ target: e.label, files: e.files.length, folder: e.artifact, example: e.files[0] || "" })) },
        { type: "html", title: "Model overview (content_model.html)", artifact: "content_model.html" },
      ];
      if (issues.length) view.push({ type: "table", title: "Normalisation notes", rows: issues.map((i) => ({ severity: i.severity, type: i.type, field: i.field, message: i.message })) });
      if (sdl) view.push({ type: "code", title: "GraphQL SDL (graphql/schema.graphql)", code: sdl.slice(0, 6000) });
      return {
        output,
        summary: `${model.types.length} content types (${kinds.page} page, ${kinds.entry} entry, ${kinds.component} component) from ${pages.length} page(s) and ${nComps} detected components; emitted ${emitted.length} target(s): ` + emitted.map((e) => e.target).join(", "),
        view,
      };
    },
  });
})();
