/* QR Code Generation - browser twin of caf/agents/content/qr_code_generation.py (deterministic, no model).
   Same stages: resolve targets (UTM-tag with the taxonomy's `qr` channel, or reuse upstream utm_generation links)
   -> short links -> encode + render (CAF.lib.qr SVG, CAF.lib.qr.png PNG) with the contrast / quiet-zone / capacity /
   logo / print-size / scan-distance checks -> manifest. Short links live in an in-memory map but use the Python
   service's exact codes (port of random.Random(url)); scan tracking needs the Python shortlink service (/r/{code}). */
(() => {
  const MIN_MODULE_MM = 0.4, ABS_MIN_MODULE_MM = 0.25, FINDER_ZONE = 9;
  // Byte-mode capacity per version (index = version) and EC level, generated from the qrcode library's
  // BIT_LIMIT_TABLE exactly as byte_capacity() computes it (verified equal for all 160 entries).
  const BYTE_CAPACITY = {
    L: [0, 17, 32, 53, 78, 106, 134, 154, 192, 230, 271, 321, 367, 425, 458, 520, 586, 644, 718, 792, 858, 929, 1003, 1091, 1171, 1273, 1367, 1465, 1528, 1628, 1732, 1840, 1952, 2068, 2188, 2303, 2431, 2563, 2699, 2809, 2953],
    M: [0, 14, 26, 42, 62, 84, 106, 122, 152, 180, 213, 251, 287, 331, 362, 412, 450, 504, 560, 624, 666, 711, 779, 857, 911, 997, 1059, 1125, 1190, 1264, 1370, 1452, 1538, 1628, 1722, 1809, 1911, 1989, 2099, 2213, 2331],
    Q: [0, 11, 20, 32, 46, 60, 74, 86, 108, 130, 151, 177, 203, 241, 258, 292, 322, 364, 394, 442, 482, 509, 565, 611, 661, 715, 751, 805, 868, 908, 982, 1030, 1112, 1168, 1228, 1283, 1351, 1423, 1499, 1579, 1663],
    H: [0, 7, 14, 24, 34, 44, 58, 64, 84, 98, 119, 137, 155, 177, 194, 220, 250, 280, 310, 338, 382, 403, 439, 461, 511, 535, 593, 625, 658, 698, 742, 790, 842, 898, 958, 983, 1051, 1093, 1139, 1219, 1273],
  };
  const SHORT_BASE = "http://localhost:8080/r/"; // settings.connectors.shortlink.base_url default (${CAF_PUBLIC_URL}/r/)
  const SHORTLINKS = new Map(); // in-memory short-link table for this page session: code -> {target, meta, created_at}
  const ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"; // string.ascii_letters + digits

  /* ------------------------------------------------------------- Python formatting twins (messages match byte for byte) */
  function pyFixed(x, nd = 0) { // format(x, f".{nd}f"): correctly rounded, exact ties to even
    if (!Number.isFinite(x)) return String(x);
    const exact = Math.abs(x).toFixed(Math.min(100, nd + 40));
    const [ip, fp = ""] = exact.split(".");
    const tail = fp.slice(nd);
    if (tail[0] === "5" && /^0*$/.test(tail.slice(1))) {
      let digits = BigInt(ip + fp.slice(0, nd));
      if (digits % 2n === 1n) digits += 1n;
      let s = digits.toString().padStart(nd + 1, "0");
      if (nd) s = `${s.slice(0, -nd)}.${s.slice(-nd)}`;
      return (x < 0 ? "-" : "") + s;
    }
    return x.toFixed(nd);
  }
  const pyRound = (x, nd) => Number(pyFixed(x, nd));
  const pyPct0 = (x) => `${pyFixed(x * 100, 0)}%`;
  const pyFloat = (v) => { // str(float)
    if (Number.isInteger(v) && Math.abs(v) < 1e16) return v.toFixed(1);
    const a = Math.abs(v);
    if (a !== 0 && (a < 1e-4 || a >= 1e16)) return v.toExponential().replace(/e([+-])(\d)$/, "e$10$2");
    return String(v);
  };
  function pyStr(s) { // repr(str)
    s = String(s);
    const q = s.includes("'") && !s.includes('"') ? '"' : "'";
    let out = "";
    for (const ch of s) {
      const cp = ch.codePointAt(0);
      if (ch === "\\" || ch === q) out += "\\" + ch;
      else if (ch === "\n") out += "\\n"; else if (ch === "\r") out += "\\r"; else if (ch === "\t") out += "\\t";
      else if (ch !== " " && /[\p{C}\p{Z}]/u.test(ch)) out += cp < 0x100 ? `\\x${cp.toString(16).padStart(2, "0")}` : cp < 0x10000 ? `\\u${cp.toString(16).padStart(4, "0")}` : `\\U${cp.toString(16).padStart(8, "0")}`;
      else out += ch;
    }
    return q + out + q;
  }

  /* ------------------------------------------------------------- urllib.parse twins (build_url parity with the UTM agent) */
  function urlsplit(url) {
    const s = String(url ?? "").replace(/^[\x00-\x20]+/, "").replace(/[\t\r\n]/g, "");
    const m = s.match(/^(?:([A-Za-z][A-Za-z0-9+.-]*):)?(?:\/\/([^/?#]*))?([^?#]*)(?:\?([^#]*))?(?:#([\s\S]*))?$/);
    if (!m) return { scheme: "", netloc: "", path: s, query: "", fragment: "" };
    return { scheme: (m[1] || "").toLowerCase(), netloc: m[2] || "", path: m[3] || "", query: m[4] || "", fragment: m[5] || "" };
  }
  const hostname = (netloc) => { // SplitResult.hostname
    const h = String(netloc || "").replace(/^.*@/, "");
    if (h.includes("[")) { const b = h.slice(h.indexOf("[") + 1); return b.split("]")[0].toLowerCase(); }
    return h.split(":")[0].toLowerCase();
  };
  function unquote(s) { // percent-decoding as UTF-8 with errors="replace"
    return String(s).replace(/(?:%[0-9A-Fa-f]{2})+/g, (run) => new TextDecoder("utf-8", { fatal: false }).decode(new Uint8Array(run.slice(1).split("%").map((h) => parseInt(h, 16)))));
  }
  function parseQsl(query) { // parse_qsl(query, keep_blank_values=True)
    const out = [];
    for (const field of String(query || "").split("&")) {
      if (!field) continue;
      const i = field.indexOf("=");
      const [k, v] = i < 0 ? [field, ""] : [field.slice(0, i), field.slice(i + 1)];
      out.push([unquote(k.replace(/\+/g, " ")), unquote(v.replace(/\+/g, " "))]);
    }
    return out;
  }
  const quotePlus = (s) => encodeURIComponent(String(s)).replace(/[!'()*]/g, (c) => `%${c.charCodeAt(0).toString(16).toUpperCase()}`).replace(/%20/g, "+");
  function urlunsplit({ scheme, netloc, path, query, fragment }) {
    let url = path;
    if (netloc || (scheme && ["http", "https", "ftp", "ws", "wss", "file"].includes(scheme) && url.slice(0, 2) !== "//")) {
      if (url && url[0] !== "/") url = "/" + url;
      url = "//" + (netloc || "") + url;
    }
    if (scheme) url = `${scheme}:${url}`;
    if (query) url += `?${query}`;
    if (fragment) url += `#${fragment}`;
    return url;
  }
  function buildUrl(base, params) { // caf.agents.strategy.utm_generation.build_url
    const p = urlsplit(String(base).trim());
    const query = parseQsl(p.query).filter(([k]) => !k.toLowerCase().startsWith("utm_"));
    for (const [k, v] of Object.entries(params)) if (v) query.push([k, v]);
    return urlunsplit({ ...p, path: p.path || "/", query: query.map(([k, v]) => `${quotePlus(k)}=${quotePlus(v)}`).join("&") });
  }
  const hasUtm = (url) => parseQsl(urlsplit(url).query).some(([k]) => k.toLowerCase().startsWith("utm_"));
  const baseOf = (url) => { const p = urlsplit(String(url).trim()); return `${p.scheme}://${p.netloc.toLowerCase()}${p.path || "/"}`; };
  const isHttpUrl = (url) => { const p = urlsplit(String(url || "").trim()); return ["http", "https"].includes(p.scheme) && !!p.netloc; };
  const slugify = (t, max = 60) => { // caf.core.textutil.slugify
    const s = String(t ?? "").normalize("NFKD").replace(/[^\x00-\x7f]/g, "").replace(/[^A-Za-z0-9]+/g, "-").replace(/^-+|-+$/g, "").toLowerCase();
    return s.slice(0, max).replace(/^-+|-+$/g, "") || "item";
  };
  const utmValue = (v) => (v === null || v === undefined || !String(v).trim() ? null : slugify(String(v), 80).replace(/-/g, "_").toLowerCase());
  function defaultCampaign(url, today = new Date()) {
    const p = urlsplit(url);
    let stem = p.path.replace(/\/+$/, "").split("/").pop();
    const dot = stem.lastIndexOf(".");
    if (dot >= 0) stem = stem.slice(0, dot);
    if (!stem) { const labels = hostname(p.netloc).split(".").filter((x) => x && x !== "www"); stem = labels.length ? labels[0] : "link"; }
    return utmValue(`${today.getFullYear()}q${Math.floor(today.getMonth() / 3) + 1}_qr_${stem}`) || "qr_campaign";
  }

  /* ------------------------------------------------------------- pure helpers (ported 1:1) */
  function parseColor(value) { // PIL ImageColor.getrgb twin: any CSS colour via the canvas parser, else CAF.lib.color
    const v = String(value ?? "").trim();
    if (!v) return null;
    try {
      const g = document.createElement("canvas").getContext("2d");
      if (g) {
        g.fillStyle = "#010203"; g.fillStyle = v; const a = String(g.fillStyle);
        g.fillStyle = "#040506"; g.fillStyle = v; const b = String(g.fillStyle);
        if (a !== b) return null; // the parser rejected the value
        let m = a.match(/^#([0-9a-f]{6})$/i);
        if (m) return [0, 2, 4].map((i) => parseInt(m[1].slice(i, i + 2), 16));
        m = a.match(/^rgba?\(([^)]+)\)$/);
        if (m) return m[1].split(/[\s,/]+/).filter(Boolean).slice(0, 3).map((x) => Math.round(parseFloat(x)));
      }
    } catch { /* fall back to the library parser */ }
    const c = CAF.lib.color.parse(v);
    return c ? c.slice(0, 3).map((x) => Math.round(x)) : null;
  }
  const toHex = ([r, g, b]) => "#" + [r, g, b].map((x) => x.toString(16).padStart(2, "0")).join("");
  const lum = (rgb) => CAF.lib.color.luminance(rgb); // same WCAG formula as relative_luminance()
  const contrastRatio = (fg, bg) => { const a = lum(fg), b = lum(bg); return pyRound((Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05), 2); };
  const byteCapacity = (version, level) => (BYTE_CAPACITY[level] || BYTE_CAPACITY.M)[version] || 0;
  const logoBox = (modules, scale) => { const size = Math.min(modules, Math.ceil(scale * modules) + 2); return [Math.floor((modules - size) / 2), size]; };
  function minVersionForLogo(scale) { for (let v = 1; v <= 40; v++) if (logoBox(17 + 4 * v, scale)[0] >= FINDER_ZONE) return v; return 40; }
  const utf8Binary = (s) => String.fromCharCode(...new TextEncoder().encode(s)); // qrcode-generator's default byte mapping is Latin-1

  /* Python's random.Random(url) (seed v2: utf-8 + sha512, MT19937 init_by_array) + 7 x choice(ALPHABET). */
  function mersenne(key) {
    const N = 624, M = 397, mt = new Uint32Array(N);
    mt[0] = 19650218;
    for (let i = 1; i < N; i++) mt[i] = (Math.imul(1812433253, mt[i - 1] ^ (mt[i - 1] >>> 30)) + i) >>> 0;
    let i = 1, j = 0;
    for (let k = Math.max(N, key.length); k; k--) {
      mt[i] = ((mt[i] ^ Math.imul(mt[i - 1] ^ (mt[i - 1] >>> 30), 1664525)) + key[j] + j) >>> 0;
      i++; j++;
      if (i >= N) { mt[0] = mt[N - 1]; i = 1; }
      if (j >= key.length) j = 0;
    }
    for (let k = N - 1; k; k--) {
      mt[i] = ((mt[i] ^ Math.imul(mt[i - 1] ^ (mt[i - 1] >>> 30), 1566083941)) - i) >>> 0;
      i++;
      if (i >= N) { mt[0] = mt[N - 1]; i = 1; }
    }
    mt[0] = 0x80000000;
    let idx = N;
    return () => {
      if (idx >= N) {
        for (let kk = 0; kk < N; kk++) {
          const y = (mt[kk] & 0x80000000) | (mt[(kk + 1) % N] & 0x7fffffff);
          mt[kk] = mt[(kk + M) % N] ^ (y >>> 1) ^ (y & 1 ? 0x9908b0df : 0);
        }
        idx = 0;
      }
      let y = mt[idx++];
      y ^= y >>> 11; y ^= (y << 7) & 0x9d2c5680; y ^= (y << 15) & 0xefc60000; y ^= y >>> 18;
      return y >>> 0;
    };
  }
  async function pythonShortCode(url) {
    const enc = new TextEncoder().encode(url);
    const dig = new Uint8Array(await crypto.subtle.digest("SHA-512", enc));
    const bytes = new Uint8Array(enc.length + dig.length); bytes.set(enc); bytes.set(dig, enc.length);
    const key = []; // int.from_bytes(big) split into 32-bit words, least significant first
    for (let end = bytes.length; end > 0; end -= 4) { let w = 0; for (let k = Math.max(0, end - 4); k < end; k++) w = w * 256 + bytes[k]; key.push(w >>> 0); }
    while (key.length > 1 && key[key.length - 1] === 0) key.pop();
    const next = mersenne(key);
    let code = "";
    for (let n = 0; n < 7; n++) { let r; do r = next() >>> 26; while (r >= ALPHABET.length); code += ALPHABET[r]; } // _randbelow(62): getrandbits(6)
    return code;
  }
  function fallbackCode(url) { // only when crypto.subtle is unavailable (non-secure context)
    let h = 2166136261, n2 = 0x9e3779b9;
    for (let i = 0; i < url.length; i++) { h = Math.imul(h ^ url.charCodeAt(i), 16777619); n2 = Math.imul(n2 ^ url.charCodeAt(i), 2246822519); }
    let n = (h >>> 0) * 4294967296 + (n2 >>> 0), code = "";
    for (let i = 0; i < 7; i++) { code += ALPHABET[n % 62]; n = Math.floor(n / 62); }
    return code;
  }
  async function shorten(url, meta) { // SqliteShortlinks.shorten twin (reuses the code of a known target)
    for (const [code, v] of SHORTLINKS) if (v.target === url) return { code, short_url: SHORT_BASE + code, target: url };
    let code;
    try { code = await pythonShortCode(url); } catch { code = fallbackCode(url); }
    SHORTLINKS.set(code, { target: url, meta, created_at: U.now() });
    return { code, short_url: SHORT_BASE + code, target: url };
  }

  /** Encode with CAF.lib.qr (smallest version); when a logo needs a minimum version, encode at that version with
      qrcode-generator directly (CAF.lib.qr.make always picks the smallest version). Same SVG geometry either way. */
  function encode(payload, level, minVersion, cell, quiet, fg, bg) {
    const data = utf8Binary(payload);
    const q = CAF.lib.qr.make(data, { ecl: level, cell, margin: quiet, fg, bg });
    if (q.version >= minVersion) return q;
    const g = window.qrcode(minVersion, level); g.addData(data, "Byte"); g.make();
    const n = g.getModuleCount(), size = (n + 2 * quiet) * cell;
    let path = "";
    for (let r = 0; r < n; r++) for (let c = 0; c < n; c++) if (g.isDark(r, c)) path += `M${(c + quiet) * cell},${(r + quiet) * cell}h${cell}v${cell}h-${cell}z`;
    return { svg: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${size} ${size}" width="${size}" height="${size}" shape-rendering="crispEdges"><rect width="${size}" height="${size}" fill="${bg}"/><path d="${path}" fill="${fg}"/></svg>`,
      modules: n, version: (n - 17) / 4, size_px: size, ecl: level, isDark: (r, c) => g.isDark(r, c) };
  }
  function finishSvg(svg, title, logo) { // accessible name + optional centre logo on a background pad (render_svg twin)
    const t = U.esc(title);
    let out = svg.replace(/^<svg /, () => `<svg role="img" aria-label="${t}" `).replace(/^(<svg[^>]*>)/, (m) => `${m}<title>${t}</title>`);
    if (logo) {
      const [start, size] = logoBox(logo.modules, logo.scale);
      const x = (start + logo.quiet) * logo.cell, s = size * logo.cell, inner = Math.max(1, size - 2) * logo.cell;
      out = out.replace(/<\/svg>$/, () => `<rect x="${x}" y="${x}" width="${s}" height="${s}" fill="${logo.back}"/><image x="${x + logo.cell}" y="${x + logo.cell}" width="${inner}" height="${inner}" preserveAspectRatio="xMidYMid meet" href="${U.esc(logo.uri)}"/></svg>`);
    }
    return out;
  }
  async function decodePng(blob) { // optional real decode (Shape Detection API) - null when the browser has no decoder
    try {
      if (typeof window.BarcodeDetector !== "function") return null;
      const formats = typeof window.BarcodeDetector.getSupportedFormats === "function" ? await window.BarcodeDetector.getSupportedFormats() : ["qr_code"];
      if (!formats.includes("qr_code")) return null;
      const found = await new window.BarcodeDetector({ formats: ["qr_code"] }).detect(await createImageBitmap(blob));
      return found.length ? found[0].rawValue : "";
    } catch { return null; }
  }

  /* ------------------------------------------------------------- the agent */
  CAF.agent("qr_code_generation", {
    async run(input, ctx) {
      // -- 1. targets + UTM chaining ------------------------------------------------------------------
      const tax = ctx.skill("utm_taxonomy", {});
      const qrCh = (tax.channels || {}).qr || { source: "print", medium: "qr" };
      const upstream = ctx.upstreamOutput("utm_generation") || {};
      const upLinks = (upstream.links || []).filter((l) => l && l.channel === "qr" && l.url).map((l) => l.url);
      let urls = [...(input.url ? [input.url] : []), ...(input.urls || [])].filter((u) => u && String(u).trim()).map((u) => String(u).trim());
      if (!urls.length && upLinks.length) {
        urls = [...upLinks];
        ctx.check({ rule: "utm.upstream", status: "info", severity: "low", message: `Using ${upLinks.length} 'qr' link(s) from the upstream UTM Generation step.` });
      }
      if (!urls.length && !input.payload) throw new Error("Provide url, urls or payload (or run after UTM Generation with a 'qr' channel).");
      const campaign = utmValue(input.utm_campaign) || utmValue(upstream.campaign) || utmValue((ctx.upstreamOutput("campaign_brief_generation") || {}).campaign_name);
      const targets = [], seen = new Set();
      let defaulted = false;
      for (const url of urls) {
        if (seen.has(url)) { ctx.check({ rule: "batch.duplicate", status: "warn", severity: "low", target: url, message: "Duplicate URL skipped." }); continue; }
        seen.add(url);
        if (!isHttpUrl(url)) { ctx.check({ rule: "url.valid", status: "fail", severity: "high", target: url, message: `'${url}' is not an absolute http(s) URL - no code generated.` }); continue; }
        const up = upLinks.find((l) => baseOf(l) === baseOf(url));
        if (upLinks.includes(url)) targets.push({ kind: "url", target: baseOf(url), tagged: url, utm: "upstream" });
        else if (up && !hasUtm(url)) targets.push({ kind: "url", target: url, tagged: up, utm: "upstream" });
        else if (hasUtm(url)) targets.push({ kind: "url", target: url, tagged: url, utm: "kept" });
        else if (input.tag_utm !== false) {
          const camp = campaign || defaultCampaign(url);
          defaulted = defaulted || !campaign;
          const params = { utm_source: utmValue(qrCh.source), utm_medium: utmValue(qrCh.medium), utm_campaign: camp, utm_content: utmValue(input.utm_content) };
          targets.push({ kind: "url", target: url, tagged: buildUrl(url, params), utm: "tagged" });
        } else targets.push({ kind: "url", target: url, tagged: url, utm: "off" });
      }
      if (defaulted) ctx.check({ rule: "utm.campaign", status: "warn", severity: "medium", message: "No campaign name given (input, upstream UTM or brief) - a dated default was used; pass utm_campaign to follow the naming taxonomy." });
      const tagged = targets.filter((t) => t.utm === "tagged" || t.utm === "upstream").length;
      if (tagged) ctx.check({ rule: "utm.tagged", status: "pass", severity: "low", message: `${tagged} URL(s) tagged for channel 'qr' (utm_source=${qrCh.source}, utm_medium=${qrCh.medium}) with the UTM agent's build_url.` });
      if (input.payload) targets.push({ kind: "payload", target: input.payload, tagged: null, utm: "n/a" });

      // -- 2. short links (in-memory; same codes as the Python service) ------------------------------------
      const urlTargets = targets.filter((t) => t.kind === "url");
      if (input.shorten !== false && urlTargets.length) {
        for (const t of urlTargets) {
          const r = await shorten(t.tagged, { agent: "qr_code_generation", run_id: ctx.runId, target: t.target });
          t.short_url = r.short_url; t.short_code = r.code;
        }
        ctx.degraded("short links are simulated in the browser (in-memory map) - scan tracking needs the Python shortlink service (/r/{code})");
        const host = hostname(urlsplit(urlTargets[0].short_url).netloc);
        if (["localhost", "127.0.0.1", "0.0.0.0"].includes(host) || host.endsWith(".local"))
          ctx.check({ rule: "shortlink.public", status: "warn", severity: "high", message: `Short links point at '${host}' - set CAF_PUBLIC_URL to your public redirect domain before printing, or scans will not resolve.` });
        else ctx.check({ rule: "shortlink.public", status: "pass", severity: "low", message: `Short links on ${host}; scans are tracked by your own redirect service.` });
      }
      for (const t of targets) t.encoded = t.short_url || t.tagged || t.target;

      // -- 3. encode, render, check ---------------------------------------------------------------------
      if (!CAF.lib.qr.available()) {
        ctx.degraded("QR library did not load in this page - no codes rendered");
        ctx.check({ rule: "qr.library", status: "fail", severity: "high", message: "The QR encoder (qrcode-generator) is not available in this page." });
        return { output: { codes: [], taxonomy_version: String(tax.version ?? ""), manifest_artifact: null }, summary: "0 QR codes: QR encoder unavailable" };
      }
      const fill = parseColor(input.fill_color), back = parseColor(input.back_color);
      if (!fill || !back) throw new Error(`Invalid colour: unknown color specifier: ${pyStr(!fill ? input.fill_color : input.back_color)}`);
      const ratio = contrastRatio(fill, back);
      const symbolContrast = pyRound(Math.abs(lum(back) - lum(fill)), 3);
      const cst = ratio < 3 ? "fail" : ratio < 4.5 ? "warn" : "pass";
      ctx.check({ rule: "colour.contrast", status: cst, severity: cst === "fail" ? "high" : "medium", ref: "ISO/IEC 18004 / ISO/IEC 15415 symbol contrast",
        message: `Module/background contrast ${pyFloat(ratio)}:1 (symbol contrast ${pyPct0(symbolContrast)}); ` + (cst === "fail" ? "too low to scan reliably - use darker modules." : cst === "warn" ? "marginal - prefer >= 4.5:1." : "scans reliably."),
        data: { ratio, symbol_contrast: symbolContrast } });
      if (lum(fill) > lum(back)) ctx.check({ rule: "colour.inverted", status: "warn", severity: "medium", message: "Light modules on a dark background (inverted) are not supported by every scanner." });
      const quiet = input.quiet_zone ?? 4;
      if (quiet < 4) ctx.check({ rule: "quiet_zone", status: quiet < 2 ? "fail" : "warn", severity: quiet < 2 ? "high" : "medium", ref: "ISO/IEC 18004 (quiet zone 4X)", message: `Quiet zone is ${quiet} module(s); the standard requires 4.` });

      let level = input.error_correction || "M";
      const scale = input.logo_scale ?? 0.22;
      let logoUri = null;
      if (input.logo_path) {
        const lp = String(input.logo_path).trim();
        if (/^data:image\/(?:png|jpe?g|gif|webp|svg\+xml)[;,]/i.test(lp)) logoUri = lp;
        else ctx.warn(`Logo '${lp}' not found - the hosted console cannot read local files (pass the logo as a data: URI, or run the Python service); codes generated without a logo.`);
        if (logoUri && level !== "H") { ctx.check({ rule: "logo.error_correction", status: "info", severity: "low", message: `Error correction raised from ${level} to H because a logo covers part of the symbol.` }); level = "H"; }
        if (logoUri) {
          const st = scale <= 0.25 ? "pass" : "warn";
          ctx.check({ rule: "logo.coverage", status: st, severity: "medium", message: `Logo spans ${pyPct0(scale)} of the width (~${pyPct0(scale * scale)} of the area; H recovers ~30% damage)` + (st === "pass" ? "." : " - keep it <= 25% to stay safe.") });
        }
      }
      const hasLogo = !!logoUri;
      const minVersion = hasLogo ? minVersionForLogo(scale) : 1;
      const sizePx = input.size_px || 600;
      const formats = input.formats || ["png", "svg"];
      const fgHex = toHex(fill), bgHex = toHex(back);
      const codes = [], views = [];
      for (let i = 0; i < targets.length; i++) {
        const t = targets[i], payload = t.encoded;
        const nbytes = new TextEncoder().encode(payload).length;
        let probe;
        try { probe = encode(payload, level, minVersion, 1, 0, fgHex, bgHex); }
        catch (e) { ctx.check({ rule: "payload.capacity", status: "fail", severity: "critical", target: t.target, message: `Payload of ${nbytes} bytes does not fit any QR version at level ${level}: ${(e && e.message) || e}` }); continue; }
        const n = probe.modules, version = probe.version, total = n + 2 * quiet;
        const modulePx = Math.max(1, Math.floor(sizePx / total)), actual = modulePx * total;
        const cap = byteCapacity(version, level);
        const vst = version <= 10 ? "pass" : "warn";
        ctx.check({ rule: "payload.version", status: vst, severity: vst === "warn" ? "medium" : "low", target: t.target, data: { bytes: nbytes, version, capacity_bytes: cap },
          message: `${nbytes}-byte payload -> version ${version} (${n}x${n} modules) at EC ${level}; capacity ${cap} bytes` + (vst === "pass" ? "." : " - dense code; shorten the URL (short link) for print.") });
        if (hasLogo && minVersion > 1 && version === minVersion) ctx.check({ rule: "logo.min_version", status: "info", severity: "low", target: t.target, message: `Version >= ${minVersion} enforced so the logo stays clear of the finder patterns.` });
        if (modulePx < 3) ctx.check({ rule: "raster.resolution", status: "warn", severity: "low", target: t.target, message: `Only ${modulePx}px per module at ${actual}px; raise size_px or use the SVG for print.` });
        const minPrint = pyRound(total * MIN_MODULE_MM, 1);
        if (input.print_width_mm) {
          const mm = input.print_width_mm / total;
          const pst = mm >= MIN_MODULE_MM ? "pass" : mm >= ABS_MIN_MODULE_MM ? "warn" : "fail";
          ctx.check({ rule: "print.module_size", status: pst, severity: pst === "fail" ? "high" : "medium", target: t.target,
            message: `At ${pyFloat(input.print_width_mm)} mm wide each module is ${pyFixed(mm, 2)} mm (recommended >= ${MIN_MODULE_MM} mm; print at least ${pyFloat(minPrint)} mm).` });
          const dpi = actual / (input.print_width_mm / 25.4);
          if (formats.includes("png") && dpi < 300) ctx.check({ rule: "print.dpi", status: "info", severity: "low", target: t.target, message: `PNG would print at ${pyFixed(dpi, 0)} dpi; use the SVG (vector) for print.` });
          if (input.scan_distance_cm) {
            const need = (input.scan_distance_cm / 10) * 10; // 10:1 rule: width = distance / 10 (cm -> mm)
            const sst = input.print_width_mm >= need ? "pass" : "warn";
            ctx.check({ rule: "print.scan_distance", status: sst, severity: "medium", target: t.target,
              message: `10:1 rule: scanning from ${pyFloat(input.scan_distance_cm)} cm needs >= ${pyFixed(need, 0)} mm; planned ${pyFloat(input.print_width_mm)} mm.` });
          }
        } else ctx.check({ rule: "print.min_size", status: "info", severity: "low", target: t.target, message: `Print at least ${pyFloat(minPrint)} mm (${pyFixed(minPrint / 25.4, 2)} in) wide incl. quiet zone (${MIN_MODULE_MM} mm/module).` });

        let stemSrc = "payload";
        if (t.kind === "url") { stemSrc = urlsplit(t.target).path.split("/").pop(); const dot = stemSrc.lastIndexOf("."); if (dot >= 0) stemSrc = stemSrc.slice(0, dot); }
        const stem = `qr-${String(i + 1).padStart(2, "0")}-${slugify(stemSrc, 30)}`;
        const q = encode(payload, level, minVersion, modulePx, quiet, fgHex, bgHex);
        const svg = finishSvg(q.svg, `QR code: ${t.target}`, hasLogo ? { modules: q.modules, quiet, cell: modulePx, scale, back: bgHex, uri: logoUri } : null);
        const item = { target_url: t.target, encoded_url: payload, tagged_url: t.tagged || null, short_url: t.short_url || null, short_code: t.short_code || null, utm: t.utm,
          png_artifact: null, svg_artifact: null, error_correction: level, version, modules: n, size_px: actual, module_px: modulePx, quiet_zone: quiet,
          contrast_ratio: ratio, min_print_mm: minPrint, logo: hasLogo, scan_verified: null };
        let png = null;
        try { png = await CAF.lib.qr.png(svg, actual); } catch (e) { ctx.warn(`PNG rendering failed for ${t.target} (${(e && e.message) || e}); the SVG is still available.`); }
        if (formats.includes("png") && png) item.png_artifact = ctx.artifact({ name: `${stem}.png`, mime: "image/png", content: png, description: `QR code for ${t.target}` }).name;
        if (formats.includes("svg")) item.svg_artifact = ctx.artifact({ name: `${stem}.svg`, mime: "image/svg+xml", content: svg, description: `QR code (vector) for ${t.target}` }).name;
        const decoded = png ? await decodePng(png) : null;
        if (decoded === null) ctx.check({ rule: "scan.verify", status: "info", severity: "low", target: t.target, message: "Scan verification skipped (no QR decoder in this browser)." });
        else {
          const ok = decoded === payload;
          item.scan_verified = ok;
          ctx.check({ rule: "scan.verify", status: ok ? "pass" : hasLogo ? "fail" : "warn", severity: ok ? "low" : "high", target: t.target,
            message: ok ? "Rendered PNG decodes back to the encoded payload." : `Rendered PNG did not decode to the payload (got ${pyStr(decoded)}) - ` + (hasLogo ? "shrink the logo or raise contrast." : "check colours and quiet zone.") });
        }
        codes.push(item);
        views.push({ type: "svg", title: t.kind === "url" ? t.target : "Payload", note: `encodes ${payload} - v${version}, EC ${level}, ${actual}px, print >= ${pyFloat(minPrint)} mm`, svg });
      }

      // -- 4. manifest + output -------------------------------------------------------------------------
      let manifest = null;
      if (codes.length) {
        const cols = ["target_url", "encoded_url", "tagged_url", "short_url", "error_correction", "version", "modules", "size_px", "min_print_mm", "png_artifact", "svg_artifact", "scan_verified"];
        const rows = codes.map((c) => ({ ...c, min_print_mm: pyFloat(c.min_print_mm), scan_verified: c.scan_verified === null ? null : c.scan_verified ? "True" : "False" }));
        manifest = ctx.artifact({ name: "qr_codes.csv", mime: "text/csv", description: "QR code manifest (batch)", content: CAF.lib.csv.stringify(rows, cols) }).name;
      }
      const fails = ctx.state.checks.filter((c) => c.status === "fail").length, warns = ctx.state.checks.filter((c) => c.status === "warn").length;
      const first = codes[0];
      const detail = first ? ` (v${first.version}, EC ${first.error_correction}, ${first.size_px}px${first.short_url ? ", tracked short links" : ""})` : "";
      const verdict = (fails ? `${fails} check(s) failed` : "no failed checks") + (warns ? `, ${warns} warning(s)` : "");
      return {
        output: { codes, taxonomy_version: String(tax.version ?? ""), manifest_artifact: manifest },
        summary: `${codes.length} QR code${codes.length === 1 ? "" : "s"}${detail}; ${verdict}`,
        view: [
          ...views,
          { type: "table", title: "Codes", columns: ["target", "encoded", "utm", "version", "ec", "size", "min print"],
            rows: codes.map((c) => ({ target: c.target_url, encoded: c.encoded_url, utm: c.utm, version: c.version, ec: c.error_correction, size: `${c.size_px}px`, "min print": `${pyFloat(c.min_print_mm)} mm` })) },
        ],
      };
    },
  });
})();
