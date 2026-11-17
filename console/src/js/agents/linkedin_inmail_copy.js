/* LinkedIn InMail Copy Creation - browser twin of caf/agents/content/linkedin_inmail_copy.py.
   Persona variants validated against the version-controlled platform_constraints skill: budgeted
   lengths (merge tokens at reserved width, URLs at url_chars), required fields, undefined /
   disallowed tokens, ALL-CAPS and banned phrases; one model repair pass (live model only), then a
   deterministic, token-safe trim. Previews render neutral merge tokens against the recipients input
   and any CRM export - here the embedded crm/contacts dataset (or pasted CSV text). */
(() => {
  const FIELDS = ["subject", "preheader", "body", "cta_text"];
  const TOKEN = /\{([A-Za-z_][A-Za-z0-9_]*)\}/g;
  const URL_RX = /https?:\/\/\S+/g;
  const len = (s) => [...String(s ?? "")].length;
  const cpSlice = (s, n) => [...String(s ?? "")].slice(0, n).join("");
  const pyStr = (s) => { s = String(s); const q = s.includes("'") && !s.includes('"') ? '"' : "'";
    return q + s.replace(/\\/g, "\\\\").replace(new RegExp(q, "g"), "\\" + q).replace(/\n/g, "\\n").replace(/\r/g, "\\r").replace(/\t/g, "\\t") + q; };
  const pyList = (xs) => `[${xs.map(pyStr).join(", ")}]`;
  const sortStr = (xs) => [...xs].sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
  const has = (o, k) => Object.prototype.hasOwnProperty.call(o || {}, k);

  // ---------------------------------------------------------------- deterministic validator
  /** Budgeted length: every known merge token counts as its reserve width; URLs as url_chars (X). */
  function measure(text, mergeFields, urlChars = null) {
    let t = text || "";
    if (urlChars) t = t.replace(URL_RX, "x".repeat(Number(urlChars)));
    return len(t.replace(TOKEN, (m, k) => (has(mergeFields, k) ? "x".repeat(Number(mergeFields[k].reserve_chars)) : m)));
  }
  const tokensIn = (value) => [...String(value || "").matchAll(TOKEN)].map((m) => m[1]);
  function validateFields(fields, spec, mergeFields, { banned = [], rules = {} } = {}) {
    const issues = [], allow = new Set(rules.caps_allowlist || []), urlChars = spec.url_chars;
    for (const [name, fs] of Object.entries(spec.fields || {})) {
      const value = String(fields[name] || "").trim(), label = fs.label ?? name;
      if (!value) { if (fs.required) issues.push({ field: name, rule: "required", status: "fail", message: `${label} is required.`, over_by: 0 }); continue; }
      const n = measure(value, mergeFields, urlChars);
      if (n > Number(fs.max_chars)) issues.push({ field: name, rule: "max_chars", status: "fail", over_by: n - Number(fs.max_chars),
        message: `${label} is ${n} chars (limit ${fs.max_chars}, merge tokens at reserved width).` });
      else if (fs.recommended_max_chars && n > Number(fs.recommended_max_chars)) issues.push({ field: name, rule: "recommended_chars", status: "warn",
        over_by: n - Number(fs.recommended_max_chars), message: `${label} is ${n} chars; ${fs.recommended_max_chars} or fewer performs better.` });
      const toks = tokensIn(value), unknown = sortStr(new Set(toks.filter((t) => !has(mergeFields, t))));
      if (unknown.length) issues.push({ field: name, rule: "unknown_token", status: "fail", over_by: 0,
        message: `${label} uses undefined merge fields ${pyList(unknown)}; allowed: ${pyList(sortStr(Object.keys(mergeFields)))}.` });
      if (toks.length && !fs.merge_fields) issues.push({ field: name, rule: "token_not_allowed", status: "fail", over_by: 0, message: `${label} cannot contain merge fields on this channel.` });
      const caps = (value.replace(TOKEN, "").match(/\b[A-Z]{3,}\b/g) || []).filter((w) => !allow.has(w));
      if (caps.length > Number(rules.max_caps_words ?? 1)) issues.push({ field: name, rule: "all_caps", status: "warn", over_by: 0, message: `${label} shouts in ALL CAPS: ${pyList(caps.slice(0, 4))}` });
      const hits = (banned || []).filter((p) => value.toLowerCase().includes(String(p).toLowerCase()));
      if (hits.length) issues.push({ field: name, rule: "banned_phrase", status: "fail", over_by: 0, message: `${label} uses brand-banned phrases ${pyList(hits)}.` });
    }
    return issues;
  }
  function cutWords(text, limit, size) {
    let parts = text.trim().split(/(\s+)/);
    while (parts.length > 1 && size(parts.join("")) > limit) parts = parts.length >= 2 ? parts.slice(0, -2) : [];
    let out = parts.join("").replace(/[ ,;:-]+$/, "");
    if (size(out) > limit && !/\{([A-Za-z_][A-Za-z0-9_]*)\}/.test(out)) out = cpSlice(out, limit); // one very long word
    return out;
  }
  /** Drop trailing sentences, then trailing words, until the budgeted length fits; tokens stay intact. */
  function trimToLimit(text, limit, mergeFields, urlChars = null) {
    const size = (t) => measure(t, mergeFields, urlChars);
    text = String(text || "").trim();
    if (size(text) <= limit) return text;
    const pieces = text.split(/(?<=[.!?])(\s+)/), sents = pieces.filter((_, i) => i % 2 === 0), seps = pieces.filter((_, i) => i % 2 === 1);
    const join = () => sents.map((s, i) => s + (i < sents.length - 1 ? seps[i] : "")).join("");
    while (sents.length > 1 && size(join()) > limit) sents.pop();
    const out = join().trim();
    return size(out) <= limit ? out : cutWords(out, limit, size);
  }
  /** Make a variant valid without a model: drop undefined/disallowed tokens, fill required short fields, trim over-limit fields. */
  function deterministicFix(fields, spec, mergeFields, { defaultCta = null } = {}) {
    const fixed = { ...fields }, notes = [], urlChars = spec.url_chars;
    for (const [name, fs] of Object.entries(spec.fields || {})) {
      let value = String(fixed[name] || "").trim();
      const allowed = !!fs.merge_fields, bad = tokensIn(value).filter((t) => !has(mergeFields, t) || !allowed);
      if (bad.length) {
        value = value.replace(TOKEN, (m, k) => (has(mergeFields, k) && allowed ? m : has(mergeFields, k) ? String(mergeFields[k].fallback) : "")).replace(/[ \t]{2,}/g, " ").trim();
        value = value.replace(/\s+([,.!?;:])/g, "$1");
        notes.push(`${name}: replaced undefined or disallowed merge fields ${pyList(sortStr(new Set(bad)))}`);
      }
      if (!value && fs.required) {
        if (name === "cta_text" && defaultCta) value = defaultCta;
        else if ((name === "subject" || name === "preheader") && fixed.body) value = String(fixed.body).trim().split(/(?<=[.!?])\s+/)[0];
        if (value) notes.push(`${name}: filled a missing required field`);
      }
      const limit = Number(fs.max_chars);
      if (value && measure(value, mergeFields, urlChars) > limit) { value = trimToLimit(value, limit, mergeFields, urlChars); notes.push(`${name}: trimmed to ${limit} chars`); }
      fixed[name] = value || null;
    }
    for (const name of FIELDS) if (!has(spec.fields, name) && fixed[name]) fixed[name] = null; // fields this channel does not have
    return [fixed, notes];
  }
  function renderTokens(text, values, mergeFields) {
    if (text === null || text === undefined) return [null, []];
    const used = [];
    const out = String(text).replace(TOKEN, (m, k) => {
      if (!has(mergeFields, k)) return m;
      const v = String((values || {})[k] || "").trim();
      if (!v) { used.push(k); return String(mergeFields[k].fallback); }
      return v;
    });
    return [out, used];
  }
  function platformCopy(fields, platformTokens) {
    const out = {};
    for (const name of FIELDS) { const v = fields[name]; if (v) out[name] = String(v).replace(TOKEN, (m, k) => (has(platformTokens, k) ? platformTokens[k] : m)); }
    return out;
  }
  /** CRM export rows -> recipients (header aliases matched case-insensitively). */
  function parseRecipientsCsv(text, limit = 500) {
    const aliases = { first_name: ["first_name", "firstname", "first name", "given_name"], company: ["company", "account", "organisation", "organization", "account_name"],
      job_title: ["job_title", "title", "jobtitle", "job title", "role"] };
    const rows = CAF.lib.csv.parse(text || "", { coerce: false });
    const cols = {};
    for (const c of Object.keys(rows[0] || {})) cols[c.trim().toLowerCase()] = c;
    const pick = Object.fromEntries(Object.entries(aliases).map(([k, names]) => [k, names.map((a) => cols[a]).find((c) => c !== undefined) ?? null]));
    const out = [];
    for (const row of rows) {
      const rec = {};
      for (const [k, c] of Object.entries(pick)) if (c) rec[k] = String(row[c] ?? "").trim() || null;
      if (Object.values(rec).some(Boolean)) out.push(rec);
      if (out.length >= limit) break;
    }
    return out;
  }
  /** Recipients whose job title shares words with the persona (best first); else the first `limit`. */
  function matchRecipients(persona, recipients, limit = 3) {
    const wordsOf = (t) => new Set((String(t || "").toLowerCase().match(/[a-z]{3,}/g) || []).filter((w) => !["the", "and", "head", "lead"].includes(w)));
    const want = wordsOf(persona);
    const scored = recipients.map((r, i) => [[...wordsOf(r.job_title || "")].filter((w) => want.has(w)).length, i, r]);
    const hits = scored.sort((a, b) => b[0] - a[0] || a[1] - b[1]).filter(([s]) => s > 0).map(([, , r]) => r);
    return (hits.length ? hits : recipients).slice(0, limit);
  }
  function previewsFor(fields, recipients, spec, mergeFields, limit = 5) {
    const urlChars = spec.url_chars;
    return recipients.slice(0, limit).map((r) => {
      const rendered = {}, counts = {};
      let fallbacks = [], ok = true;
      for (const name of FIELDS) {
        const [text, used] = renderTokens(fields[name], r, mergeFields);
        rendered[name] = text; fallbacks = fallbacks.concat(used);
        if (text !== null && has(spec.fields, name)) { const n = measure(text, {}, urlChars); counts[name] = n; ok = ok && n <= Number(spec.fields[name].max_chars); }
      }
      const label = [r.first_name, r.job_title, r.company].filter(Boolean).join(" - ") || "recipient";
      return { recipient: label, ...rendered, char_counts: counts, within_limits: ok, fallbacks_used: sortStr(new Set(fallbacks)) };
    });
  }
  /** Python passes recipients_csv to ingestion.load(path=...): map sample_data/<x>.csv to the embedded dataset. */
  async function loadRecipientsCsv(ctx, ref) {
    const s = String(ref || "").trim();
    const name = s.replace(/^\.?\/?(?:sample_data\/)?/, "").replace(/\.csv$/i, "");
    if (ctx.hasData(name)) return [await ctx.dataRaw(name), name];
    if (s.includes("\n") && s.includes(",")) return [s, "pasted CSV"];
    throw new Error(`Source not found: ${s} (the hosted console reads embedded datasets such as sample_data/crm/contacts.csv, or pasted CSV text)`);
  }

  const lib = { measure, validateFields, trimToLimit, deterministicFix, renderTokens, platformCopy, parseRecipientsCsv, matchRecipients, previewsFor };

  CAF.agent("linkedin_inmail_copy", {
    lib,
    async run(input, ctx) {
      // -- context
      const pc = ctx.skill("platform_constraints"), channels = pc.channels || {};
      if (!has(channels, input.channel)) throw new Error(`Unknown channel '${input.channel}'. Configured in platform_constraints: ${pyList(sortStr(Object.keys(channels)))}`);
      const spec = channels[input.channel], merge = pc.merge_fields || {}, rules = pc.rules || {}, version = String(pc.version ?? "");
      let sender = input.sender_name || null;
      if ((spec.required_inputs || []).includes("sender_name") && !sender) {
        sender = (ctx.brand.media_contact || {}).name || ctx.brand.name;
        ctx.check({ rule: "input.sender", status: "warn", severity: "medium", message: `${spec.label} needs a sender profile; defaulted to '${sender}'.` });
      }
      const brief = ctx.upstreamOutput("campaign_brief_generation") || {}, utm = ctx.upstreamOutput("utm_generation") || {};
      let ctaUrl = input.cta_url || null;
      const link = ((utm.links || []).find((l) => l.channel === spec.utm_channel) || {}).url;
      if (link && (!ctaUrl || ctaUrl.split("?")[0] === link.split("?")[0])) ctaUrl = link;
      const given = (input.personas || []).map((p) => String(p).trim()).filter(Boolean).slice(0, 6);
      const personas = given.length ? given : [input.audience || (brief.target_audience || {}).primary || "B2B decision-maker"];
      const limits = Object.entries(spec.fields || {}).map(([name, fs]) => `${name} (${fs.label ?? name}) - max ${fs.max_chars} characters`
        + (fs.recommended_max_chars ? `, aim for ${fs.recommended_max_chars} or fewer` : "") + (fs.required ? "" : ", optional") + (fs.merge_fields ? ", merge fields allowed" : ", no merge fields"));
      const tokens = Object.values(merge).map((m) => `${m.token} - falls back to '${m.fallback}' when unknown (reserve ${m.reserve_chars} chars)`);
      const keyMessages = (brief.key_messages || []).slice(0, 4);

      // -- draft
      ctx.progress(`Drafting ${personas.length} persona variant(s)`);
      const draft = await ctx.generate("draft", { overview: input.overview, audience: input.audience ?? null, personas, channel_label: spec.label, limits, tokens,
        fields: Object.keys(spec.fields || {}), guidance: spec.guidance || [], call_to_action: input.call_to_action ?? null, cta_url: ctaUrl, sender_name: sender,
        sender_title: input.sender_title ?? null, extra_context: input.extra_context ?? null, key_messages: keyMessages });
      const variants = (draft.variants || []).slice(0, personas.length).map((v) => ({ ...v }));
      while (variants.length < personas.length) variants.push({ ...variants[variants.length - 1] });
      variants.forEach((v, i) => { v.persona = personas[i]; });

      // -- validate and repair
      const banned = ctx.brand.banned_phrases || [], final = [];
      for (const v of variants) {
        let fields = Object.fromEntries(FIELDS.map((k) => [k, v[k] ?? null]));
        const repair = [];
        let issues = validateFields(fields, spec, merge, { banned, rules });
        if (issues.some((i) => i.status === "fail") && ctx.llmLive) { // one model repair pass
          ctx.progress(`Repairing '${v.persona}'`);
          const fixed = await ctx.generate("repair", { draft: { persona: v.persona, ...fields }, channel_label: spec.label, limits, tokens,
            findings: issues.filter((i) => i.status === "fail").map((i) => i.message) });
          fields = Object.fromEntries(FIELDS.map((k) => [k, fixed[k] ?? null]));
          repair.push("model");
          issues = validateFields(fields, spec, merge, { banned, rules });
        }
        let notes = [];
        if (issues.some((i) => i.status === "fail" && i.rule !== "banned_phrase")) {
          [fields, notes] = deterministicFix(fields, spec, merge, { defaultCta: spec.default_cta ?? null });
          repair.push("trimmed");
          issues = validateFields(fields, spec, merge, { banned, rules });
        }
        for (const name of FIELDS) if (!has(spec.fields, name)) fields[name] = null;
        const valid = !issues.some((i) => i.status === "fail");
        for (const i of issues) ctx.check({ rule: `${input.channel}.${i.field}.${i.rule}`, status: i.status, severity: i.status === "fail" ? "high" : "low", target: v.persona,
          message: i.message, ref: `platform_constraints ${version}` });
        if (notes.length) ctx.check({ rule: "copy.deterministic_trim", status: "warn", severity: "medium", target: v.persona, message: "Deterministic fix applied: " + notes.join("; ") });
        if (!issues.length) ctx.check({ rule: "copy.limits", status: "pass", severity: "low", target: v.persona, message: `All fields within ${spec.label} limits.` });
        final.push({ persona: v.persona, fields, issues: issues.map((i) => i.message), valid, repair: repair.length ? repair.join("+") : "none" });
      }

      // -- render: previews, platform macros, artifacts
      const recipients = (input.recipients || []).map((r) => ({ first_name: r.first_name ?? null, company: r.company ?? null, job_title: r.job_title ?? null }));
      if (input.recipients_csv) {
        try {
          const [text, src] = await loadRecipientsCsv(ctx, input.recipients_csv), rows = parseRecipientsCsv(text);
          recipients.push(...rows);
          ctx.check({ rule: "input.recipients_csv", status: "info", severity: "low", message: `${rows.length} recipients read from ${src === "pasted CSV" ? "pasted CSV" : input.recipients_csv} for personalised previews.` });
        } catch (e) {
          ctx.check({ rule: "input.recipients_csv", status: "warn", severity: "low", message: `Could not read ${String(input.recipients_csv).slice(0, 80)}: ${(e && e.message) || e}` });
        }
      }
      const outs = final.map((f) => {
        const fields = f.fields;
        const counts = Object.fromEntries(Object.keys(spec.fields || {}).filter((n) => fields[n]).map((n) => [n, measure(fields[n], merge, spec.url_chars)]));
        const pv = previewsFor(fields, matchRecipients(f.persona, recipients), spec, merge);
        const over = pv.filter((p) => !p.within_limits).map((p) => p.recipient);
        if (over.length) ctx.check({ rule: "preview.limits", status: "warn", severity: "medium", target: f.persona,
          message: `Personalised copy exceeds a limit for: ${pyList(over)} - shorten the copy or the merge value.` });
        const used = sortStr(new Set(FIELDS.flatMap((n) => tokensIn(fields[n]).filter((t) => has(merge, t)))));
        return { persona: f.persona, channel: input.channel, subject: fields.subject ?? null, preheader: fields.preheader ?? null, body: fields.body || "", cta_text: fields.cta_text ?? null,
          cta_url: ctaUrl, char_counts: counts, limits: Object.fromEntries(Object.entries(spec.fields || {}).map(([n, fs]) => [n, Number(fs.max_chars)])), valid: f.valid,
          issues: f.issues, repair: f.repair, merge_fields_used: used, platform_copy: platformCopy(fields, spec.platform_tokens || {}), previews: pv };
      });
      const output = { channel: input.channel, channel_label: spec.label, constraints_version: version, sender_name: sender, variants: outs,
        all_valid: outs.every((o) => o.valid), merge_fields: Object.values(merge).map((m) => m.token) };
      const csvRows = outs.map((o) => ({ persona: o.persona, channel: o.channel, subject: o.subject || "", preheader: o.preheader || "", body: o.body, cta_text: o.cta_text || "",
        cta_url: o.cta_url || "", valid: o.valid ? "True" : "False", repair: o.repair, char_counts: Object.entries(o.char_counts).map(([k, v]) => `${k}=${v}/${o.limits[k]}`).join("; ") }));
      ctx.artifact({ name: "inmail_variants.csv", mime: "text/csv", content: CAF.lib.csv.stringify(csvRows, ["persona", "channel", "subject", "preheader", "body", "cta_text", "cta_url",
        "valid", "repair", "char_counts"]), description: `${spec.label} variants` });
      ctx.artifact({ name: "inmail_variants.json", mime: "application/json", content: JSON.stringify(output, null, 2), description: "Variants with personalised previews" });
      const nOk = outs.filter((o) => o.valid).length, first = outs[0];
      const summary = `${outs.length} ${spec.label} variant(s) for ${outs.map((o) => o.persona).join(", ")}; ${nOk}/${outs.length} within platform limits`
        + (first.subject ? `; subject: “${first.subject}”` : "") + (recipients.length ? `; ${outs.reduce((a, o) => a + o.previews.length, 0)} personalised previews` : "");
      return {
        output, summary,
        view: [
          { type: "kv", title: spec.label, data: { channel: input.channel, constraints_version: version, sender: sender || "-", cta_url: ctaUrl || "-", all_valid: output.all_valid ? "yes" : "no" } },
          ...outs.map((o) => ({ type: "table", title: `${o.persona}${o.valid ? "" : " (needs attention)"} - repair: ${o.repair}`, columns: ["field", "copy", "chars", "limit"],
            rows: FIELDS.filter((n) => o[n]).map((n) => ({ field: n, copy: o[n], chars: o.char_counts[n] ?? "", limit: o.limits[n] ?? "" })) })),
          { type: "table", title: "Personalised previews", columns: ["persona", "recipient", "subject", "body", "within_limits", "fallbacks"],
            rows: outs.flatMap((o) => o.previews.map((p) => ({ persona: o.persona, recipient: p.recipient, subject: p.subject || "", body: p.body,
              within_limits: p.within_limits ? "yes" : "no", fallbacks: p.fallbacks_used.join(", ") }))) },
        ],
      };
    },
  });
})();
