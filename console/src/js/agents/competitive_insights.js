/* Competitive Insights - browser twin of caf/agents/strategy/competitive_insights.py.
   Same evidence pipeline per competitor: the fixture search plan (news + search angles), feeds,
   press pages and sitemap discovery on the demo site, deterministic evidence typing, impact signals,
   window filtering, same-story collapsing, citation validation and digest fallbacks. Synthesis runs
   per competitor in parallel (Promise.all) from the numbered evidence only. Evidence and synthesis
   are cached in memory for the page session (the Python runtime uses the record store). The hosted
   page has no network: feeds, press pages and sitemaps outside the demo site degrade with a reason. */
(() => {
  const T = CAF.lib.text;
  const FIXTURE_TODAY = "2026-10-18"; // connectors.search.reference_date (fixture provider)
  const EVIDENCE_CACHE = new Map(), SYNTHESIS_CACHE = new Map();
  const cmp = (a, b) => (a < b ? -1 : a > b ? 1 : 0);
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
  const round = (x, d) => Number(pyFixed(x, d));
  const pyFloat = (x) => (x === null || x === undefined ? "None" : Number.isInteger(x) ? `${x}.0` : String(x));
  const normWs = (s) => String(s ?? "").replace(/\s+/g, " ").trim();
  const reEsc = (s) => String(s).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  /** textutil.truncate (code-point lengths, cut at the last space). */
  function truncate(text, n, ell = "...") {
    const cps = [...String(text ?? "")];
    if (cps.length <= n) return String(text ?? "");
    const cut = cps.slice(0, Math.max(0, n - ell.length)).join(""), i = cut.lastIndexOf(" ");
    return (i >= 0 ? cut.slice(0, i) : cut) + ell;
  }
  /** html.unescape (entities only, tags untouched). */
  function unescapeHtml(s) {
    s = String(s ?? "");
    if (!s.includes("&")) return s;
    if (typeof document !== "undefined") { const ta = document.createElement("textarea"); ta.innerHTML = s; return ta.value; }
    return s.replace(/&(#x[0-9a-f]+|#\d+|amp|lt|gt|quot|apos|nbsp);/gi, (m, e) => (e[0] === "#" ? String.fromCodePoint(e[1].toLowerCase() === "x" ? parseInt(e.slice(2), 16) : +e.slice(1))
      : { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: "\u00a0" }[e.toLowerCase()]));
  }
  const capitalize = (w) => (w ? w[0].toUpperCase() + w.slice(1).toLowerCase() : w);
  const RX = new Map();
  const bounded = (t) => { if (!RX.has(t)) RX.set(t, new RegExp(`(?<![a-z0-9])${reEsc(t)}(?![a-z0-9])`)); return RX.get(t); };
  const hasAny = (text, terms) => terms.some((t) => bounded(t).test(text));

  // ---------------------------------------------------------------- urls + dates
  /** urllib.parse.urlsplit -> {scheme (lowercased), netloc, path} */
  function urlsplit(url) {
    let s = String(url ?? "").trim(), scheme = "";
    const sm = s.match(/^([A-Za-z][A-Za-z0-9+.-]*):/);
    if (sm) { scheme = sm[1].toLowerCase(); s = s.slice(sm[0].length); }
    let netloc = "";
    if (s.startsWith("//")) { const m = s.slice(2).match(/^[^/?#]*/); netloc = m[0]; s = s.slice(2 + netloc.length); }
    return { scheme, netloc, path: s.split(/[?#]/)[0] };
  }
  const host = (url) => urlsplit(url).netloc.toLowerCase().replace(/^www\./, "");
  function canonicalUrl(url) {
    const p = urlsplit(url), scheme = p.scheme || "https", netloc = p.netloc.toLowerCase().replace(/^www\./, ""), path = p.path.replace(/\/+$/, "") || "/";
    if (netloc) return `${scheme}://${netloc}${path.startsWith("/") ? path : "/" + path}`;
    if (path.startsWith("//") || path.startsWith("/")) return `${scheme}://${path.startsWith("//") ? path.slice(2) : path}`;
    return `${scheme}:${path}`;
  }
  const urljoin = (base, href) => { try { return new URL(href, base).toString(); } catch { return href; } };
  const dayOf = (iso) => Date.UTC(+iso.slice(0, 4), +iso.slice(5, 7) - 1, +iso.slice(8, 10)) / 864e5;
  function ymd(y, m, d) {
    const t = new Date(Date.UTC(y, m - 1, d));
    if (y < 1 || t.getUTCFullYear() !== y || t.getUTCMonth() !== m - 1 || t.getUTCDate() !== d) return null;
    return `${String(y).padStart(4, "0")}-${String(m).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
  }
  const MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec", "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december"];
  const DAYNAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
  /** email.utils.parsedate_to_datetime(...).date() (RFC 2822; the time part is required, like Python). */
  function rfc2822(s) {
    let data = s.split(/\s+/).filter(Boolean);
    if (!data.length) return null;
    if (data[0].endsWith(",") || DAYNAMES.includes(data[0].toLowerCase())) data.shift();
    else { const i = data[0].lastIndexOf(","); if (i >= 0) data[0] = data[0].slice(i + 1); }
    if (data.length === 3) { const stuff = data[0].split("-"); if (stuff.length === 3) data = [...stuff, ...data.slice(1)]; }
    if (data.length === 4) {
      const t = data[3]; let i = t.indexOf("+"); if (i === -1) i = t.indexOf("-");
      if (i > 0) data.splice(3, 1, t.slice(0, i), t.slice(i)); else data.push("");
    }
    if (data.length < 5) return null;
    let [dd, mm, yy, tm, tz] = data.slice(0, 5);
    if (!(dd && mm && yy)) return null;
    mm = mm.toLowerCase();
    if (!MONTHS.includes(mm)) { [dd, mm] = [mm, dd.toLowerCase()]; if (!MONTHS.includes(mm)) return null; }
    let mon = MONTHS.indexOf(mm) + 1; if (mon > 12) mon -= 12;
    if (dd.endsWith(",")) dd = dd.slice(0, -1);
    if (yy.indexOf(":") > 0) [yy, tm] = [tm, yy];
    if (yy.endsWith(",")) { yy = yy.slice(0, -1); if (!yy) return null; }
    if (!/^\d/.test(yy)) [yy, tz] = [tz, yy];
    if (tm.endsWith(",")) tm = tm.slice(0, -1);
    let parts = tm.split(":");
    if (parts.length === 1 && parts[0].includes(".")) parts = parts[0].split(".");
    if (parts.length !== 2 && parts.length !== 3) return null;
    const int = (v) => (/^[+-]?\d+$/.test(String(v).trim()) ? parseInt(v, 10) : NaN);
    let y = int(yy); const d = int(dd), hh = int(parts[0]), mi = int(parts[1]), ss = parts.length === 3 ? int(parts[2]) : 0;
    if ([y, d, hh, mi, ss].some(Number.isNaN)) return null;
    if (y < 100) y += y > 68 ? 1900 : 2000;
    if (hh > 23 || mi > 59 || ss > 59 || hh < 0 || mi < 0 || ss < 0) return null;
    return ymd(y, mon, d);
  }
  /** parse_date: ISO date/datetime (incl. basic YYYYMMDD) or RFC 2822 -> "YYYY-MM-DD" | null */
  function parseDate(value) {
    if (!value) return null;
    const s = String(value).trim();
    let m = s.match(/^(\d{4})-(\d{2})-(\d{2})/);
    if (m) { const r = ymd(+m[1], +m[2], +m[3]); if (r) return r; }
    m = s.match(/^(\d{4})(\d{2})(\d{2})(?=$|T)/);
    if (m) { const r = ymd(+m[1], +m[2], +m[3]); if (r) return r; }
    return rfc2822(s);
  }

  // ---------------------------------------------------------------- deterministic rules
  const PRESS_WIRE_PATTERNS = [/pr ?newswire/, /business ?wire/, /globe ?newswire/, /newswire/, /accesswire/, /einpresswire/, /presswire/, /press release/];
  const ANALYST_SOURCE_PATTERNS = [/\bgartner\b/, /\bforrester\b/, /\bidc\b/, /j\.? ?d\.? power/, /\bjdpower\b/, /a\.? ?m\.? best/, /\bambest\b/, /\bmoody/, /s&p\b/,
    /\bfitch\b/, /\bkbra\b/, /\bcelent\b/, /everest group/, /datos insights/, /\baite\b/, /\bnovarica\b/, /\banalyst/, /(?<![a-z])research/, /\bratings\b/];
  const ANALYST_ORGS = ["gartner", "forrester", "j.d. power", "jd power", "am best", "a.m. best", "moody's", "moodys", "s&p global", "fitch", "kbra", "celent",
    "everest group", "datos insights"];
  const COMMUNITY_SOURCE_PATTERNS = [/(?<![a-z])forum/, /(?<![a-z])communit/, /\breddit\b/, /\btrustpilot\b/, /\bquora\b/, /\bglassdoor\b/, /consumer ?affairs/,
    /\bsitejabber\b/, /(?<![a-z0-9])r\/[a-z0-9_]+/, /\bmastodon\b/];
  const COMMUNITY_DOMAINS = ["x.com", "twitter.com", "reddit.com", "trustpilot.com", "facebook.com", "quora.com", "youtube.com", "tiktok.com", "instagram.com",
    "threads.net", "glassdoor.com", "linkedin.com"];
  const CAMPAIGN_TERMS = ["campaign", "campaigns", "advert", "adverts", "advertising", "ad campaign", "ad recall", "tv ad", "tv spot", "commercial", "sponsor",
    "sponsors", "sponsorship", "ambassador", "promotion", "promo", "billboard", "influencer", "influencers", "rebrand", "rebrands", "brand refresh", "tagline",
    "jingle", "hashtag", "giveaway"];
  const COMMUNITY_TERMS = ["customers say", "customer reviews", "reviews", "reviewers", "complaint", "complaints", "thread", "users report", "policyholders say",
    "petition", "viral", "backlash", "social media", "stars", "app store", "one-star"];
  const ANALYST_TERMS = ["analyst", "analysts", "rating", "ratings", "downgrade", "downgrades", "downgraded", "upgrade", "upgraded", "outlook", "report finds",
    "study finds", "survey", "ranking", "ranked", "benchmark", "market share", "forecast"];
  const PRESS_PATHS = ["/press", "/newsroom", "/news-release", "/media-centre", "/media-center", "/media/", "/news/", "/press-release"];
  const IMPACT_SIGNALS = [
    ["pricing", 3, ["price", "prices", "pricing", "priced", "premium", "premiums", "rate", "rates", "rate increase", "rate increases", "rate rise", "rate cut",
      "repricing", "discount", "discounts", "cheaper", "price war", "commission", "commissions", "deductible", "deductibles"]],
    ["launch", 3, ["launch", "launches", "launched", "launching", "relaunch", "relaunches", "relaunched", "unveil", "unveils", "unveiled", "introduces",
      "introduced", "rolls out", "rolled out", "debut", "debuts", "debuted", "new product"]],
    ["legal_regulatory", 3, ["lawsuit", "lawsuits", "fine", "fines", "fined", "regulator", "regulators", "investigation", "settlement", "product recall", "recalls",
      "breach", "data breach", "outage", "class action", "penalty", "sanction", "market conduct"]],
    ["market_exit", 3, ["exit", "exits", "exiting", "withdraw", "withdraws", "withdrawal", "pulls out", "retreat", "retreats", "non-renewal", "non-renewals", "stops writing"]],
    ["m_and_a", 3, ["acquires", "acquired", "to acquire", "acquisition of", "merger", "merges with", "takeover", "buyout", "stake in"]],
    ["partnership", 2, ["partnership", "partners with", "partnered", "teams up", "tie-up", "alliance", "collaboration", "joint venture"]],
    ["campaign", 2, ["campaign", "sponsors", "sponsorship", "ambassador", "advertising", "rebrand", "tv spot"]],
    ["leadership", 2, ["appoints", "appointed", "names", "named", "hires", "ceo", "chief executive", "chief digital officer", "chief marketing officer", "steps down", "resigns"]],
    ["financial", 2, ["earnings", "results", "revenue", "profit", "funding", "ipo", "combined ratio", "downgrade", "downgraded", "upgrade", "upgraded"]],
    ["expansion", 2, ["expands", "expanded", "expansion", "enters", "new market", "new markets", "launches in"]],
    ["recognition", 1, ["award", "awards", "wins", "tops", "ranked first", "named best"]],
  ];
  const WEIGHT = Object.fromEntries(IMPACT_SIGNALS.map(([n, w]) => [n, w]));
  const TYPE_WEIGHT = { press: 1, news: 1, campaign: 1, analyst: 0.9, community: 0.6 };
  const QUERY_PLAN = [["news", "{name}"], ["news", "{name} {focus}"], ["search", "{name}"], ["search", "{name} analyst rating outlook"],
    ["search", "{name} customer reviews complaints"], ["search", "{name} marketing campaign"]];
  const TYPES = ["news", "press", "analyst", "community", "campaign"];

  function resolveCompetitor(raw) {
    const s = normWs(raw);
    if (/^https?:\/\//i.test(s) || /^[\p{L}\p{N}_-]+(\.[\p{L}\p{N}_-]+)+(\/\S*)?$/u.test(s)) {
      const url = /^http/i.test(s) ? s : `https://${s}`, h = host(url);
      const name = h.split(".")[0].split(/[-_]+/).filter(Boolean).map(capitalize).join(" ") || h;
      return { input: raw, name, url, domain: h };
    }
    return { input: raw, name: s, url: null, domain: null };
  }
  const GENERIC_NAME = new Set(["insurance", "assurance", "group", "inc", "ltd", "llc", "plc", "co", "company", "corp", "the", "mutual", "holdings", "bank", "financial"]);
  function nameTokens(name) {
    const toks = (String(name).toLowerCase().match(/[a-z0-9]+/g) || []).filter((t) => t.length >= 3);
    const distinctive = toks.filter((t) => !GENERIC_NAME.has(t));
    return distinctive.length ? distinctive : toks;
  }
  const mentions = (text, name) => { const low = String(text).toLowerCase(); return nameTokens(name).some((t) => bounded(t).test(low)); };
  function inferDomain(name, urls) {
    const toks = nameTokens(name), counts = new Map();
    for (const u of urls) { const h = host(u); if (toks.length && h.split(/[.-]/).includes(toks[0])) counts.set(h, (counts.get(h) || 0) + 1); }
    return counts.size ? [...counts].sort((a, b) => b[1] - a[1] || cmp(a[0], b[0]))[0][0] : null;
  }
  function classifyEvidence(item, competitorDomain = null) {
    const h = host(item.url || ""), path = urlsplit(item.url || "").path.toLowerCase(), publisher = String(item.publisher || "").toLowerCase();
    const text = ` ${item.title ?? ""} ${item.snippet ?? ""} `.toLowerCase();
    const where = h.split(/[.-]/).join(" ") + " " + publisher;
    const src = (patterns) => patterns.some((p) => p.test(where));
    if (item.origin === "press_page" || item.origin === "sitemap") return "press";
    if (src(PRESS_WIRE_PATTERNS)) return "press";
    if (competitorDomain && (h === competitorDomain || h.endsWith("." + competitorDomain))) return hasAny(text, CAMPAIGN_TERMS) ? "campaign" : "press";
    if (COMMUNITY_DOMAINS.some((d) => h === d || h.endsWith("." + d)) || src(COMMUNITY_SOURCE_PATTERNS)) return "community";
    if (src(ANALYST_SOURCE_PATTERNS)) return "analyst";
    if (PRESS_PATHS.some((p) => path.includes(p)) && where.includes("newsroom")) return "press";
    if (hasAny(text, CAMPAIGN_TERMS)) return "campaign";
    if (hasAny(text, ANALYST_ORGS)) return "analyst";
    if (hasAny(text, COMMUNITY_TERMS)) return "community";
    if (hasAny(text, ANALYST_TERMS)) return "analyst";
    return "news";
  }
  function impactSignals(item) {
    const title = ` ${item.title ?? ""} `.toLowerCase(), snippet = ` ${item.snippet ?? ""} `.toLowerCase();
    const inTitle = IMPACT_SIGNALS.filter(([, , terms]) => hasAny(title, terms)).map(([n]) => n);
    const inSnippet = IMPACT_SIGNALS.filter(([n, , terms]) => !inTitle.includes(n) && hasAny(snippet, terms)).map(([n]) => n);
    return [inTitle, inSnippet];
  }
  function impactScore(titleSignals, snippetSignals, evType, ageDays, days) {
    let base = titleSignals.filter((s) => s in WEIGHT).reduce((a, s) => a + WEIGHT[s], 0);
    base += 0.5 * snippetSignals.filter((s) => s in WEIGHT).reduce((a, s) => a + WEIGHT[s], 0);
    const recency = ageDays === null || ageDays === undefined || ageDays <= days ? 1 : 0.8;
    return round(base * (TYPE_WEIGHT[evType] ?? 0.8) * recency, 2);
  }
  const nowStamp = () => new Date().toISOString().slice(0, 19) + "+00:00";
  /** Dedupe (url + title), classify, date, window-filter, score; ids C<index>-E<nn>, newest first. */
  function finalizeEvidence(raw, { competitor, index, domain, ref, days, eventsDays, maxItems }) {
    const seenUrl = new Set(), seenTitle = new Set(), items = [], refDay = dayOf(ref);
    for (const it of raw) {
      const title = normWs(unescapeHtml(String(it.title || ""))), url = String(it.url || "").trim();
      if (!title || !url) continue;
      const cu = canonicalUrl(url), ct = title.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
      if (seenUrl.has(cu) || seenTitle.has(ct)) continue;
      const d = parseDate(it.published), age = d ? refDay - dayOf(d) : null;
      if (age !== null && (age > eventsDays || age < -1)) continue;
      seenUrl.add(cu); seenTitle.add(ct);
      const rec = { competitor, title, url, publisher: it.publisher || it.source || null, published: d, retrieved_at: it.retrieved_at || nowStamp(),
        snippet: truncate(normWs(unescapeHtml(String(it.snippet || ""))), 400), origin: it.origin || "search", provider: it.provider || "", age_days: age,
        in_window: age === null || age <= days };
      rec.type = classifyEvidence(rec, domain);
      const [inTitle, inSnippet] = impactSignals(rec);
      rec.signals = [...inTitle, ...inSnippet];
      const leadPool = inTitle.length ? inTitle : inSnippet;
      rec.lead_signal = rec.signals.length ? leadPool.reduce((best, s) => (WEIGHT[s] > WEIGHT[best] ? s : best), leadPool[0]) : null;
      rec.impact_score = impactScore(inTitle, inSnippet, rec.type, age, days);
      items.push(rec);
    }
    const ageKey = (r) => (r.age_days !== null ? r.age_days : 1e6);
    items.sort((a, b) => (a.in_window === b.in_window ? 0 : a.in_window ? -1 : 1) || b.impact_score - a.impact_score || ageKey(a) - ageKey(b) || cmp(a.title, b.title));
    const kept = items.slice(0, maxItems);
    kept.sort((a, b) => (a.age_days === null) - (b.age_days === null) || (a.age_days ?? 0) - (b.age_days ?? 0) || cmp(a.title, b.title));
    kept.forEach((r, n) => { r.id = `C${index}-E${String(n + 1).padStart(2, "0")}`; });
    return kept;
  }
  function normalizeIds(ids) {
    const out = [];
    for (const i of ids || []) { const m = String(i).match(/\[?\s*(C\d+-E\d+)\s*\]?/i); if (m) out.push(m[1].toUpperCase()); }
    return [...new Set(out)];
  }
  function splitInlineIds(text) {
    const found = (String(text).match(/C\d+-E\d+/gi) || []).map((m) => m.toUpperCase());
    return [normWs(String(text).replace(/\s*[[(]\s*(?:C\d+-E\d+[,;\s]*)+[\])]/gi, "")), found];
  }
  function validateClaims(claims, idTypes, { estimateMode, wantTypes = [] }) {
    const kept = [], stats = { kept: 0, dropped: 0, flagged: 0 };
    for (const c of claims || []) {
      const [text, inline] = splitInlineIds(String((c || {}).text || ""));
      if (!text) { stats.dropped++; continue; }
      if (estimateMode) {
        kept.push({ text, evidence_ids: [], basis: "model_knowledge", is_estimate: true, flags: ["model knowledge - not backed by retrieved evidence"] });
        stats.kept++; continue;
      }
      const ids = normalizeIds([...((c || {}).evidence_ids || []), ...inline]);
      const good = ids.filter((i) => i in idTypes), bad = ids.filter((i) => !(i in idTypes));
      if (!good.length) { stats.dropped++; continue; }
      const flags = [];
      if (bad.length) flags.push("removed unknown evidence ids: " + bad.join(", "));
      if (wantTypes.length && !good.some((i) => wantTypes.includes(idTypes[i]))) flags.push(`cites no ${wantTypes.join("/")} evidence`);
      if (flags.length) stats.flagged++;
      stats.kept++;
      kept.push({ text, evidence_ids: good, basis: "evidence", is_estimate: false, flags });
    }
    return [kept, stats];
  }
  function validateActions(actions, idTypes, { estimateMode, perPriority = 3 }) {
    const groups = { P1: [], P2: [], P3: [] };
    for (const a of actions || []) {
      const text = normWs(String((a || {}).action || ""));
      if (!text) continue;
      const ids = normalizeIds(a.evidence_ids || []), good = ids.filter((i) => i in idTypes), flags = [];
      if (ids.length && good.length < ids.length) flags.push("removed unknown evidence ids: " + ids.filter((i) => !(i in idTypes)).join(", "));
      if (!good.length) flags.push(estimateMode ? "model-knowledge estimate - no evidence retrieved" : "not linked to retrieved evidence");
      const prio = a.priority in groups ? a.priority : "P2";
      if (groups[prio].length < perPriority)
        groups[prio].push({ priority: prio, action: text, rationale: normWs(String(a.rationale || "")), owner_role: normWs(String(a.owner_role || "")),
          evidence_ids: good, evidence_backed: good.length > 0, flags });
    }
    return groups;
  }
  const GENERIC_TITLE = new Set(["insurance", "insurer", "insurers", "home", "homes", "says", "new", "with", "from", "after", "over", "into", "their"]);
  function titleTerms(title, competitor = "") {
    const skip = new Set([...nameTokens(competitor), ...GENERIC_TITLE]), out = new Set();
    for (let w of String(title).toLowerCase().match(/[a-z0-9.]+/g) || []) {
      w = w.replace(/^\.+|\.+$/g, "");
      if (w.length > 3 && w.endsWith("s") && !w.endsWith("ss")) w = w.slice(0, -1);
      if ((w.length >= 4 || /\d/.test(w)) && !skip.has(w) && !T.STOP.has(w)) out.add(w);
    }
    return out;
  }
  function sameStory(a, b, days = 10) {
    if (!a.lead_signal || a.lead_signal !== b.lead_signal) return false;
    if (Math.abs((a.age_days || 0) - (b.age_days || 0)) > days) return false;
    const tb = titleTerms(b.title, b.competitor || "");
    return [...titleTerms(a.title, a.competitor || "")].some((t) => tb.has(t));
  }
  function selectEvents(items, { limit = 6, minScore = 1.5, types = ["news", "press", "campaign", "analyst"] } = {}) {
    const key = (a, b) => b.impact_score - a.impact_score || (a.age_days || 0) - (b.age_days || 0) || cmp(a.id, b.id);
    const dated = items.filter((i) => i.published && types.includes(i.type) && (i.impact_score || 0) >= minScore).sort(key);
    const picked = [];
    for (const i of dated) {
      const twin = picked.findIndex((p) => sameStory(i, p));
      if (twin < 0) { if (picked.length < limit) picked.push(i); }
      else if (i.impact_score >= picked[twin].impact_score && (i.age_days || 0) > (picked[twin].age_days || 0)) picked[twin] = i;
    }
    return picked.sort(key);
  }
  const recentHeadlines = (items, limit = 8) => items.filter((i) => i.in_window && ["news", "press", "campaign"].includes(i.type))
    .sort((a, b) => cmp(b.published || "", a.published || "") || cmp(b.id, a.id)).slice(0, limit);
  const evidenceDigest = (items, types, limit = 4) => items.filter((i) => types.includes(i.type) && i.in_window).slice(0, limit)
    .map((i) => ({ text: `${i.title} (${i.publisher || host(i.url)}, ${i.published || "undated"}).`, evidence_ids: [i.id], basis: "evidence_digest", is_estimate: false,
      flags: ["evidence digest - no validated model claim for this section"] }));
  function typeCounts(items) { const c = Object.fromEntries(TYPES.map((t) => [t, 0])); for (const i of items) c[i.type] = (c[i.type] || 0) + 1; return c; }
  function summaryDigest(name, items, events, days) {
    const counts = typeCounts(items.filter((i) => i.in_window)), total = Object.values(counts).reduce((a, b) => a + b, 0);
    if (!total && !events.length) return [];
    const parts = Object.entries(counts).filter(([, v]) => v).map(([k, v]) => `${v} ${k}`).join(", "), out = [];
    if (total) out.push({ text: `${total} evidence items about ${name} in the last ${days} days (${parts}).`, evidence_ids: items.filter((i) => i.in_window).slice(0, 3).map((i) => i.id),
      basis: "evidence_digest", is_estimate: false, flags: ["evidence digest"] });
    for (const e of events.slice(0, 2)) out.push({ text: `High-impact signal (${e.signals.join(", ")}): ${e.title}.`, evidence_ids: [e.id], basis: "evidence_digest", is_estimate: false, flags: ["evidence digest"] });
    return out;
  }
  const renderClaims = (claims) => claims.map((c) => `${c.text} ${(c.evidence_ids || []).map((i) => `[${i}]`).join(" ")}`.trim() + (c.is_estimate ? " (estimate)" : "")).join(" ");

  // ---------------------------------------------------------------- demo-site pages, sitemaps, feeds
  /** bs4 get_text(sep[, strip]) - descendant text joined by sep (script/style/template skipped). */
  function getText(el, sep = "", strip = false) {
    const out = [];
    const walk = (n) => {
      for (const c of n.childNodes) {
        if (c.nodeType === 3 || c.nodeType === 4) { const t = strip ? c.nodeValue.trim() : c.nodeValue; if (!strip || t) out.push(t); }
        else if (c.nodeType === 1 && !["SCRIPT", "STYLE", "TEMPLATE"].includes(c.nodeName.toUpperCase())) walk(c);
      }
    };
    if (el) walk(el);
    return out.join(sep);
  }
  function pressItems(markup, baseUrl) {
    const doc = CAF.lib.html.parse(markup || ""), out = [];
    for (const el of doc.querySelectorAll("article, li, .press-release, .news-item")) {
      const a = el.querySelector("a[href]");
      if (!a) continue;
      const head = el.querySelector("h1, h2, h3, h4") || a, title = normWs(getText(head, " "));
      if ([...title].length < 20) continue;
      const t = el.querySelector("time");
      let published = t ? (t.getAttribute("datetime") || getText(t, " ", true)) : null;
      if (!published) { const m = getText(el, " ").match(/\b(20\d\d-\d\d-\d\d)\b/); published = m ? m[1] : null; }
      out.push({ title, url: urljoin(baseUrl, a.getAttribute("href")), published, snippet: truncate(normWs(getText(el, " ")), 300) });
    }
    if (!out.length) {
      const title = doc.querySelector("h1") || doc.querySelector("title");
      const meta = doc.querySelector('meta[property="article:published_time"]') || doc.querySelector("time");
      const published = meta ? (meta.getAttribute("content") || meta.getAttribute("datetime")) : null, desc = doc.querySelector('meta[name="description"]');
      if (title) out.push({ title: normWs(getText(title, " ")), url: baseUrl, published, snippet: desc ? desc.getAttribute("content") || "" : "" });
    }
    const uniq = new Map();
    for (const it of out) if (!uniq.has(canonicalUrl(it.url))) uniq.set(canonicalUrl(it.url), it);
    return [...uniq.values()];
  }
  function sitemapPressUrls(xml, ref, eventsDays, limit = 5) {
    const rows = [];
    for (const [, block] of String(xml || "").matchAll(/<url>([\s\S]*?)<\/url>/gi)) {
      const loc = block.match(/<loc>\s*([\s\S]*?)\s*<\/loc>/i), mod = block.match(/<lastmod>\s*([\s\S]*?)\s*<\/lastmod>/i);
      if (!loc || !PRESS_PATHS.some((p) => urlsplit(loc[1]).path.toLowerCase().includes(p))) continue;
      const d = mod ? parseDate(mod[1]) : null;
      if (d && dayOf(ref) - dayOf(d) > eventsDays) continue;
      rows.push([loc[1], d]);
    }
    return rows.sort((a, b) => cmp(b[1] || "", a[1] || "")).slice(0, limit);
  }
  function assignPressPages(competitors, pages) {
    const owned = Object.fromEntries(competitors.map((c) => [c.name, []])), shared = [];
    for (const url of pages) {
      const h = host(url), low = url.toLowerCase();
      const match = competitors.filter((c) => (c.domain && (h === c.domain || h.endsWith("." + c.domain))) || nameTokens(c.name).some((t) => low.includes(t))).map((c) => c.name);
      if (match.length === 1) owned[match[0]].push(url); else shared.push(url);
    }
    return [owned, shared];
  }
  /** crawler.fetch for the embedded demo site (fixture host): redirects, status overrides, final_url. */
  function fetchDemo(ctx, url) {
    if (!ctx.isDemoUrl(url)) return null;
    const p = ctx.site(url);
    if (!p) return null;
    let finalUrl = url;
    if (p.redirects && p.redirects.length) {
      let meta = {};
      try { meta = JSON.parse((ctx.site("/_fixture.json") || {}).html || "{}"); } catch { meta = {}; }
      let path = CAF.site.path(url);
      for (let i = 0; i < 8 && (meta.redirects || {})[path]; i++) { finalUrl = urljoin(finalUrl, meta.redirects[path][0]); path = CAF.site.path(finalUrl); }
    }
    const html = String(p.html || "");
    return { status: p.status, html, final_url: finalUrl, ok: p.status >= 200 && p.status < 300,
      is_html: /\.html?$/i.test(p.path || "") || /^(<!doctype html|<html)/i.test(html.trimStart()) };
  }
  function readFeed(xmlText, feedUrl) {
    let doc;
    try { doc = new DOMParser().parseFromString(xmlText, "application/xml"); } catch { return []; }
    if (!doc || doc.getElementsByTagName("parsererror").length) return [];
    const ATOM = "http://www.w3.org/2005/Atom";
    let items = [...doc.getElementsByTagName("item")].filter((e) => !e.namespaceURI);
    const atom = !items.length;
    if (atom) items = [...doc.getElementsByTagNameNS(ATOM, "entry")];
    const child = (el, tag, ns = null) => [...el.children].find((c) => c.localName === tag && (c.namespaceURI || null) === ns) || null;
    const txt = (el) => { if (!el) return ""; let s = ""; for (const c of el.childNodes) { if (c.nodeType === 1) break; if (c.nodeType === 3 || c.nodeType === 4) s += c.nodeValue; } return s.trim(); };
    return items.map((it) => {
      let link = txt(child(it, "link"));
      if (!link) { const l = child(it, "link", ATOM); link = l ? l.getAttribute("href") || "" : ""; }
      const published = (txt(child(it, "pubDate")) || txt(child(it, "updated", ATOM))).slice(0, 25) || null;
      return { title: txt(child(it, "title")) || txt(child(it, "title", ATOM)), url: link, snippet: txt(child(it, "description")) || txt(child(it, "summary", ATOM)),
        published, source: feedUrl, retrieved_at: nowStamp(), provider: "rss:fixture" };
    });
  }
  function fixtureSearch(items, query, { num: n = 10, recencyDays = null, ref = FIXTURE_TODAY } = {}) {
    const q = [...new Set(String(query).toLowerCase().replace(/-/g, " ").split(/\s+/).filter((t) => t.length > 2))];
    const scored = [];
    for (const it of items || []) {
      const terms = new Set((it.query_terms || []).map((t) => String(t).toLowerCase()));
      const hay = `${it.title ?? ""} ${it.snippet ?? ""}`.toLowerCase();
      const score = 3 * q.filter((t) => terms.has(t)).length + q.filter((t) => hay.includes(t)).length;
      if (score) scored.push([score, it.published || "", it]);
    }
    scored.sort((a, b) => b[0] - a[0] || cmp(b[1], a[1]));
    const stamp = nowStamp();
    let results = scored.map(([, , it]) => ({ title: it.title, url: it.url, snippet: it.snippet ?? "", published: it.published ?? null, source: it.source ?? null,
      retrieved_at: stamp, provider: "fixture" }));
    if (recencyDays !== null && recencyDays !== undefined)
      results = results.filter((r) => { if (!r.published) return true; const d = /^\d{4}-\d{2}-\d{2}/.test(r.published) ? ymd(+r.published.slice(0, 4), +r.published.slice(5, 7), +r.published.slice(8, 10)) : null;
        return d === null || dayOf(d) >= dayOf(ref) - recencyDays; });
    return results.slice(0, n);
  }

  // ---------------------------------------------------------------- report rendering
  function reportMarkdown(out, brandName) {
    const lines = [`# Competitive insights - ${out.competitors.map((c) => c.competitor).join(", ")}`, "",
      `Prepared for ${brandName} on ${out.reference_date} · window ${out.window_days} days · high-impact events ${out.events_days} days` + (out.focus ? ` · focus: ${out.focus}` : ""), ""];
    const orElse = (arr, fallback) => (arr.length ? arr : fallback);
    for (const c of out.competitors) {
      lines.push(`## ${c.competitor}`, "");
      if (c.is_estimate) lines.push("> **Model-knowledge estimate.** No evidence was retrieved for this competitor, so nothing below is backed by sources. Verify before use.", "");
      lines.push("### Executive summary", c.executive_summary || "_No validated claims._", "", "### Recent headlines");
      lines.push(...orElse(c.recent_headlines.map((h) => `- ${h.date || "undated"} - [${h.title}](${h.url}) - ${h.publisher || ""} [${h.evidence_id}]`), ["_None in the window._"]));
      for (const [key, title] of [["marketing_activity", "Marketing activity"], ["analyst_commentary", "Analyst commentary"], ["community_signals", "Community signals"]]) {
        lines.push("", `### ${title}`);
        lines.push(...orElse(c[key].map((cl) => `- ${renderClaims([cl])}` + (cl.flags && cl.flags.length ? ` _(${cl.flags.join("; ")})_` : "")), ["_No evidence-backed items._"]));
      }
      lines.push("", "### Next best actions");
      for (const [prio, label] of [["P1", "P1 - act now"], ["P2", "P2 - this month"], ["P3", "P3 - monitor"]]) {
        const acts = (c.next_best_actions || {})[prio] || [];
        if (!acts.length) continue;
        lines.push(`**${label}**`);
        for (const a of acts) {
          const refs = a.evidence_ids.map((i) => `[${i}]`).join(" "), owner = a.owner_role ? ` _(owner: ${a.owner_role})_` : "";
          lines.push(`- ${a.action}${owner} ${refs}`.trimEnd() + (a.evidence_backed ? "" : " _(not evidence-backed)_"));
        }
      }
      lines.push("", `### High-impact events (last ${out.events_days} days)`);
      lines.push(...orElse(c.high_impact_events.map((e) => `- **${e.date}** [${e.title}](${e.url}) - ${e.signals.join(", ")} (impact ${pyFloat(e.impact_score)}) - ${e.significance} [${e.evidence_id}]`), ["_None detected._"]));
      lines.push("", "### Evidence", "", "| Id | Type | Published | Publisher | Title |", "|---|---|---|---|---|");
      lines.push(...orElse(c.evidence.map((e) => `| <a id="${e.id}"></a>${e.id} | ${e.type} | ${e.published || "undated"} | ${e.publisher || host(e.url)} | [${e.title.replaceAll("|", "/")}](${e.url}) |`),
        ["| - | - | - | - | no evidence |"]));
      const v = c.validation || {};
      lines.push("", `_Validation: ${v.kept || 0} claims kept, ${v.dropped || 0} dropped for missing citations, ${v.flagged || 0} flagged._`, "");
    }
    return lines.join("\n") + "\n";
  }
  function reportHtml(md, title) {
    const body = CAF.lib.md.html(md).replace(/\[(C\d+-E\d+)\]/g, '<a class="cite" href="#$1">[$1]</a>');
    return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>${U.esc(title)}</title><style>
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;max-width:980px;margin:32px auto;padding:0 20px;color:#1d2530;line-height:1.55}
h1{font-size:1.7rem;border-bottom:2px solid #0b5cad;padding-bottom:8px} h2{margin-top:2.2rem;color:#0b5cad} h3{margin-top:1.4rem}
table{border-collapse:collapse;width:100%;font-size:.88rem} th,td{border:1px solid #dfe3e8;padding:6px 8px;text-align:left;vertical-align:top}
th{background:#f3f5f8} blockquote{background:#fff6e5;border-left:4px solid #e0a100;margin:0;padding:8px 14px}
a.cite{font-size:.78rem;text-decoration:none;color:#5b6573} em{color:#5b6573}
</style></head><body>${body}</body></html>`;
  }
  const EVIDENCE_FIELDS = ["id", "competitor", "type", "title", "url", "publisher", "published", "retrieved_at", "snippet", "origin", "provider", "age_days", "in_window", "signals", "impact_score"];
  const evidenceOut = (e) => Object.fromEntries(EVIDENCE_FIELDS.map((k) => [k, e[k] === undefined ? null : e[k]]));

  const lib = { pyFixed, truncate, urlsplit, host, canonicalUrl, parseDate, resolveCompetitor, nameTokens, mentions, inferDomain, classifyEvidence, impactSignals,
    impactScore, finalizeEvidence, normalizeIds, splitInlineIds, validateClaims, validateActions, titleTerms, sameStory, selectEvents, recentHeadlines,
    evidenceDigest, summaryDigest, typeCounts, renderClaims, sitemapPressUrls, assignPressPages, fixtureSearch, reportMarkdown };

  CAF.agent("competitive_insights", {
    lib,
    async run(input, ctx) {
      // -- stage 1 plan
      const comps = [], seen = new Set();
      for (const raw of input.competitors) { const c = resolveCompetitor(raw); if (c.name && !seen.has(c.name.toLowerCase())) { seen.add(c.name.toLowerCase()); comps.push(c); } }
      if (comps.length < input.competitors.length) ctx.check({ rule: "input.duplicates", status: "info", severity: "low", message: "Duplicate competitors were merged." });
      const days = Number(input.days);
      let eventsDays = Number(input.events_days);
      if (eventsDays < days) {
        ctx.check({ rule: "input.windows", status: "warn", severity: "low", message: `events_days (${eventsDays}) was shorter than days (${days}); using ${days}.` });
        eventsDays = days;
      }
      const hasSearch = ctx.hasData("search/fixture_results");
      const ref = FIXTURE_TODAY; // connectors.search.reference_date - the fixture's 'today'
      const [pressOwned, pressShared] = assignPressPages(comps, input.press_pages || []);
      const fixture = hasSearch ? ((await ctx.data("search/fixture_results")).results || []) : [];
      const offSite = new Set();
      const fetchPage = (url, errors) => {
        const page = fetchDemo(ctx, url);
        if (!page) { offSite.add(url); errors.push(`press page ${url} failed: the hosted console cannot fetch sites outside the demo site`); return []; }
        if (!page.ok || !page.is_html) { errors.push(`press page ${url} returned status ${page.status}`); return []; }
        const stamp = nowStamp();
        return pressItems(page.html, page.final_url).map((it) => ({ ...it, origin: "press_page", publisher: host(page.final_url), retrieved_at: stamp, provider: "crawler" }));
      };

      // -- stage 2 gather: evidence per competitor (fixture search plan + demo-site feeds/press pages/sitemaps)
      const warnings = [], reasons = [];
      let feedItems = [];
      if ((input.feeds || []).length) {
        for (const url of input.feeds) {
          const page = fetchDemo(ctx, url);
          if (!page) { offSite.add(url); continue; }
          if (!page.ok) continue;
          feedItems.push(...readFeed(page.html, url));
        }
        feedItems = feedItems.filter((r) => { if (!r.published) return true; const d = /^\d{4}-\d{2}-\d{2}/.test(r.published) ? parseDate(r.published.slice(0, 10)) : null;
          return d === null || dayOf(d) >= dayOf(ref) - eventsDays; })
          .map((r) => ({ ...r, origin: "feed", publisher: host(r.url) || host(r.source || "") }));
      }
      const sharedPages = [];
      for (const url of pressShared) sharedPages.push(...fetchPage(url, warnings));
      if (!hasSearch) reasons.push("no search provider configured (search 'none') - evidence limited to feeds and press pages");
      const results = comps.map((comp, k) => {
        const ci = k + 1;
        const key = [T.slugify(comp.name, 40), ref, days, eventsDays, ci, U.hash(JSON.stringify([hasSearch ? "fixture" : "none", input.focus ?? null, [...(input.feeds || [])].sort(),
          [...(pressOwned[comp.name] || []), ...pressShared].sort(), input.max_items]))].join(":");
        if (!input.refresh && EVIDENCE_CACHE.has(key)) {
          const hit = U.clone(EVIDENCE_CACHE.get(key));
          if (hit.items.length) return { comp: { ...comp, domain: hit.domain || comp.domain }, items: hit.items, cache_hit: true, errors: [], key };
        }
        const raw = [], errors = [], name = comp.name, focus = String(input.focus || "").trim();
        if (hasSearch) {
          for (const [method, template] of QUERY_PLAN) {
            if (template.includes("{focus}") && !focus) continue;
            const query = template.replace("{name}", name).replace("{focus}", focus).trim();
            raw.push(...fixtureSearch(fixture, query, { num: input.max_items, recencyDays: eventsDays, ref })
              .filter((r) => mentions(`${r.title} ${r.snippet} ${r.url}`, name)).map((r) => ({ ...r, publisher: r.source, origin: method })));
          }
        }
        for (const url of pressOwned[name] || []) raw.push(...fetchPage(url, errors));
        if (comp.url) { // sitemap discovery of newsroom pages on the competitor's own site
          const parts = urlsplit(comp.url), sitemap = `${parts.scheme}://${parts.netloc}/sitemap.xml`, sm = fetchDemo(ctx, sitemap);
          if (!sm) offSite.add(sitemap);
          for (const [loc, lastmod] of sitemapPressUrls(sm && sm.ok ? sm.html : "", ref, eventsDays, 3))
            for (const it of fetchPage(loc, errors)) raw.push({ ...it, published: it.published || lastmod, origin: "sitemap" });
        }
        raw.push(...[...feedItems, ...sharedPages].filter((i) => mentions(`${i.title || ""} ${i.snippet || ""}`, name)).map((i) => ({ ...i })));
        const domain = comp.domain || inferDomain(name, raw.map((r) => r.url || ""));
        const items = finalizeEvidence(raw, { competitor: name, index: ci, domain, ref, days, eventsDays, maxItems: Number(input.max_items) });
        if (items.length) EVIDENCE_CACHE.set(key, U.clone({ competitor: name, domain, items }));
        return { comp: { ...comp, domain }, items, cache_hit: false, errors, key };
      });
      if ((input.feeds || []).length) ctx.check({ rule: "evidence.feeds", status: feedItems.length ? "info" : "warn", severity: "low",
        message: `${feedItems.length} item(s) read from ${input.feeds.length} feed(s) within ${eventsDays} days.` });
      if ((input.press_pages || []).length) {
        const owned = Object.values(pressOwned).reduce((a, v) => a + v.length, 0);
        ctx.check({ rule: "evidence.press_pages", status: "info", severity: "low",
          message: `${input.press_pages.length} press page(s): ${owned} matched to a competitor, ${pressShared.length} shared (${sharedPages.length} item(s), filtered by mention).` });
      }
      const pyDict = (o) => `{${Object.entries(o).map(([k, v]) => `'${k}': ${v}`).join(", ")}}`;
      for (const res of results) {
        const name = res.comp.name, n = res.items.length;
        warnings.push(...res.errors.map((e) => `${name}: ${e}`));
        ctx.check({ rule: "evidence.collected", status: n ? "pass" : "warn", severity: "medium", target: name, data: { cache_key: res.key },
          message: `${name}: ${n} evidence items` + (res.cache_hit ? " (cache hit)" : "") + (n ? ` - ${pyDict(typeCounts(res.items))}` : " - report will be a model-knowledge estimate") });
        if (!n) reasons.push(`no evidence found for ${name} - report is a model-knowledge estimate`);
      }
      if (offSite.size) {
        reasons.unshift("feeds, press pages and sitemaps outside the demo site cannot be fetched in the hosted console (no network) - evidence limited to the embedded search fixture and demo-site pages");
        warnings.push(`Not fetched (outside the demo site): ${[...offSite].slice(0, 6).join(", ")}`);
      }
      warnings.forEach((w) => ctx.warn(w));
      reasons.forEach((r) => ctx.degraded(r));

      // -- stage 3 synthesize: PRO tier, from the numbered evidence only (parallel, cached per prompt)
      ctx.progress(`Synthesising ${results.length} competitor report(s)`);
      const syntheses = await Promise.all(results.map(async (res) => {
        const items = res.items, events = selectEvents(items);
        const vars = { competitor: res.comp.name, focus: input.focus ?? null, days, events_days: eventsDays, reference_date: ref, estimate_mode: !items.length,
          evidence: items.map((i) => ({ id: i.id, type: i.type, published: i.published, publisher: i.publisher, title: i.title, snippet: truncate(i.snippet || "", 280) })),
          events: events.map((e) => ({ id: e.id, title: e.title, signals: e.signals })) };
        let key = null;
        try { const p = CAF.prompts.render(ctx.agent.id, "synthesis", vars); key = U.hash(`${p.system}\n${p.user}\n${ctx.llmLive ? "claude:" + (ctx.agent.tier || "") : "offline"}`); } catch { key = null; }
        if (key && !input.refresh && SYNTHESIS_CACHE.has(key)) return { synthesis: U.clone(SYNTHESIS_CACHE.get(key)), cache_hit: true, events };
        const syn = await ctx.generate("synthesis", vars);
        if (key) SYNTHESIS_CACHE.set(key, U.clone(syn));
        return { synthesis: syn, cache_hit: false, events };
      }));

      // -- stage 4 validate: deterministic citation validation + digest fallbacks
      const reports = results.map((res, k) => {
        const synRes = syntheses[k], name = res.comp.name, items = res.items, syn = synRes.synthesis || {}, estimate = !items.length;
        const idTypes = Object.fromEntries(items.map((i) => [i.id, i.type])), totals = { kept: 0, dropped: 0, flagged: 0 }, sections = {};
        for (const [key, want, digestTypes] of [["executive_summary", [], []], ["marketing_activity", ["campaign", "press", "news"], ["campaign", "press"]],
          ["analyst_commentary", ["analyst"], ["analyst"]], ["community_signals", ["community"], ["community"]]]) {
          let [kept, stats] = validateClaims(syn[key] || [], idTypes, { estimateMode: estimate, wantTypes: want });
          for (const t in totals) totals[t] += stats[t];
          if (!kept.length && !estimate) kept = key === "executive_summary" ? summaryDigest(name, items, selectEvents(items), days) : evidenceDigest(items, digestTypes);
          sections[key] = kept;
        }
        const notes = {};
        for (const n of syn.event_notes || []) { const ids = normalizeIds([(n || {}).evidence_id || ""]); if (ids.length) notes[ids[0]] = n.significance || ""; }
        const events = synRes.events.map((e) => {
          const note = normWs(notes[e.id] || "");
          return { evidence_id: e.id, title: e.title, date: e.published, publisher: e.publisher, url: e.url, type: e.type, signals: e.signals, impact_score: e.impact_score,
            significance: note || `Rule-detected ${e.signals.join(", ")} signal in a ${e.type} item.`, basis: note ? "model" : "rules" };
        });
        const status = !totals.dropped && !totals.flagged ? "pass" : "warn";
        ctx.check({ rule: "claims.citations", status: estimate ? "info" : status, severity: "high", target: name, data: totals,
          message: estimate ? `${name}: no evidence - claims are flagged model-knowledge estimates.` : `${name}: ${totals.kept} claims kept, ${totals.dropped} dropped (no valid evidence id), ${totals.flagged} flagged.` });
        return { competitor: name, input: res.comp.input, domain: res.comp.domain ?? null, is_estimate: estimate, executive_summary: renderClaims(sections.executive_summary),
          summary_claims: sections.executive_summary,
          recent_headlines: recentHeadlines(items).map((h) => ({ evidence_id: h.id, title: h.title, date: h.published, publisher: h.publisher, url: h.url, type: h.type })),
          marketing_activity: sections.marketing_activity, analyst_commentary: sections.analyst_commentary, community_signals: sections.community_signals,
          next_best_actions: validateActions(syn.next_best_actions || [], idTypes, { estimateMode: estimate }), high_impact_events: events,
          evidence: items.map(evidenceOut), evidence_counts: typeCounts(items), validation: totals, cache: { evidence: res.cache_hit, synthesis: synRes.cache_hit } };
      });

      // -- stage 5 report
      const findings = [];
      for (const r of reports) {
        const top = (r.next_best_actions.P1.length ? r.next_best_actions.P1 : r.next_best_actions.P2.length ? r.next_best_actions.P2 : [null])[0];
        for (const e of r.high_impact_events.slice(0, 3))
          findings.push({ title: `${r.competitor}: ${e.title}`, detail: e.significance, severity: e.impact_score >= 4 ? "high" : "medium", category: "competitive",
            evidence: `${e.publisher || host(e.url)}, ${e.date ?? "None"} [${e.evidence_id}] ${e.url}`, recommendation: top ? top.action : "", target: e.url, source_agent: "competitive_insights" });
      }
      const output = { reference_date: ref, window_days: days, events_days: eventsDays, focus: input.focus ?? null,
        executive_summary: reports.filter((r) => r.executive_summary).map((r) => `${r.competitor}: ${r.executive_summary}`).join(" "),
        competitors: reports, evidence_count: reports.reduce((a, r) => a + r.evidence.length, 0), is_estimate: reports.some((r) => r.is_estimate), findings };
      const md = reportMarkdown(output, ctx.brand.name || "us"), title = "Competitive insights - " + reports.map((r) => r.competitor).join(", ");
      ctx.artifact({ name: "competitive_insights.md", mime: "text/markdown", content: md, description: "Competitive intelligence report" });
      ctx.artifact({ name: "competitive_insights.html", mime: "text/html", content: reportHtml(md, title), description: "Competitive intelligence report (HTML)" });
      ctx.artifact({ name: "evidence.json", mime: "application/json", content: JSON.stringify(reports.flatMap((r) => r.evidence), null, 2), description: "Evidence ledger (url, publisher, published, retrieved_at, type)" });
      for (const r of reports) if (r.is_estimate) ctx.warn(`${r.competitor}: no evidence retrieved - the report is a model-knowledge estimate.`);
      const summary = `Competitive insights for ${reports.length} competitor(s), ${days}-day window: `
        + reports.map((r) => `${r.competitor} (${r.evidence.length} items, ${r.high_impact_events.length} high-impact` + (r.is_estimate ? ", estimate" : "") + ")").join("; ");
      return {
        output, summary,
        view: [
          { type: "kv", title: "Competitive insights", data: { reference_date: ref, window: `${days} days`, events_window: `${eventsDays} days`, competitors: reports.length,
            evidence_items: output.evidence_count, estimate: output.is_estimate ? "yes" : "no" } },
          { type: "table", title: "High-impact events", columns: ["competitor", "date", "event", "signals", "impact", "type", "id"],
            rows: reports.flatMap((r) => r.high_impact_events.map((e) => ({ competitor: r.competitor, date: e.date, event: e.title, signals: e.signals.join(", "),
              impact: e.impact_score, type: e.type, id: e.evidence_id }))) },
          { type: "table", title: "Next best actions", columns: ["competitor", "priority", "action", "owner", "evidence"],
            rows: reports.flatMap((r) => ["P1", "P2", "P3"].flatMap((p) => r.next_best_actions[p].map((a) => ({ competitor: r.competitor, priority: p, action: a.action,
              owner: a.owner_role, evidence: a.evidence_ids.join(" ") || "not evidence-backed" })))) },
          { type: "html", title: "Report", artifact: "competitive_insights.html" },
          { type: "table", title: "Evidence", columns: ["id", "type", "published", "publisher", "title", "impact"],
            rows: reports.flatMap((r) => r.evidence.map((e) => ({ id: e.id, type: e.type, published: e.published || "undated", publisher: e.publisher || host(e.url), title: e.title, impact: e.impact_score }))) },
        ],
      };
    },
  });
})();
