/* Press Release Creation - browser twin of caf/agents/content/press_release_creation.py.
   The model drafts the news; code owns the AP dateline, the structure + AP-style validator, the
   forward-looking-statement check and the verbatim boilerplate / media contact / disclaimers from the
   versioned press_boilerplate skill. Distribution is an approval-gated newswire action. */
(() => {
  const MONTHS = ["Jan.", "Feb.", "March", "April", "May", "June", "July", "Aug.", "Sept.", "Oct.", "Nov.", "Dec."];
  const DATELINE_RX = /^[A-Z][A-Z .'-]+,(?: [A-Z][A-Za-z.]*(?: [A-Z][A-Za-z.]*)*,)? (?:Jan\.|Feb\.|March|April|May|June|July|Aug\.|Sept\.|Oct\.|Nov\.|Dec\.) \d{1,2}, \d{4} —$/;
  const normWs = (t) => String(t ?? "").replace(/\s+/g, " ").trim();
  const esc = (s) => String(s).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const wc = (t) => CAF.lib.text.wordCount(t);

  /* ------------------------------------------------------------- pure helpers (ported 1:1) */
  function parseLocation(location, rec) {
    const parts = String(location || "").split(",").map((p) => p.trim()).filter(Boolean);
    if (!parts.length) return ["", null];
    const city = parts[0].toUpperCase();
    if (new Set((rec.ap_standalone_cities || []).map((c) => String(c).toUpperCase())).has(city)) return [city, null];
    const region = parts.length > 1 ? parts[1] : null;
    if (!region) return [city, null];
    const states = rec.ap_states || {}, postal = rec.ap_postal_codes || {};
    const key = region.replace(/\.+$/, "");
    if (key.toUpperCase() in postal) { const full = postal[key.toUpperCase()]; return [city, states[full] || full]; }
    for (const [name, abbr] of Object.entries(states)) if (key.toLowerCase() === name.toLowerCase() || key.toLowerCase() === String(abbr).replace(/\.+$/, "").toLowerCase()) return [city, abbr];
    return [city, region];
  }
  const apDate = (d, months) => `${months[d.m - 1]} ${d.d}, ${d.y}`;
  function buildDateline(location, d, rec) {
    const [city, region] = parseLocation(location, rec);
    return `${region ? `${city}, ${region}` : city}, ${apDate(d, rec.ap_months || MONTHS)} —`;
  }
  const validateDateline = (dl) => DATELINE_RX.test(dl || "");
  function parseSpokesperson(v) {
    if (!v || !String(v).trim()) return ["", ""];
    const s = String(v); const i = s.indexOf(",");
    return i < 0 ? [s.trim(), ""] : [s.slice(0, i).trim(), s.slice(i + 1).trim()];
  }
  function formatQuote(q, org, verb = "said") {
    let text = normWs(q.text).replace(/^["“”' ]+|["“”' ]+$/g, "");
    if (text.endsWith(".")) text = text.slice(0, -1) + ",";
    else if (!/[?!,]$/.test(text)) text += ",";
    const who = String(q.speaker || "").trim(), title = String(q.title || "").trim();
    let tail = title ? `, ${title}` : "";
    if (title && org && !title.toLowerCase().includes(org.toLowerCase())) tail += ` at ${org}`;
    return `“${text}” ${verb} ${who}${tail}.`;
  }
  const findTerms = (text, terms) => { const out = []; for (const t of terms || []) if (new RegExp(`\\b${esc(t)}\\b`, "i").test(text || "") && !out.includes(t)) out.push(t); return out; };
  const sentencesWith = (text, terms, limit = 6) => CAF.lib.text.sentences(text).filter((s) => terms.some((t) => new RegExp(`\\b${esc(t)}\\b`, "i").test(s))).slice(0, limit);
  function checkLead(lead, orgNames, verbs, day, months) {
    const low = String(lead || "").toLowerCase();
    const who = orgNames.some((n) => n && low.includes(String(n).toLowerCase()));
    const what = verbs.some((v) => new RegExp(`\\b${esc(v)}\\b`).test(low));
    const names = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"];
    const when = /\b(today|tomorrow|yesterday|this week|this month|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b/.test(low)
      || names.some((m) => low.includes(m)) || months.some((m) => low.includes(m.toLowerCase().replace(/\.+$/, "")))
      || /\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}\/\d{1,2}\/\d{2,4}\b/.test(low) || low.includes(String(day.y));
    return { who, what, when, words: wc(lead) };
  }
  function mediaContactLines(mc) {
    const lines = []; const name = [mc.name, mc.title].filter(Boolean).join(", ");
    if (name) lines.push(name);
    for (const k of ["email", "phone"]) if (mc[k]) lines.push(String(mc[k]));
    return lines;
  }
  function assembleBody(dateline, d, org, { heading, boilerplate, fls, extra, mediaContact, endMark }) {
    const paras = [`${dateline} ${normWs(d.lead)}`];
    const body = (d.body_paragraphs || []).map(normWs).filter(Boolean);
    const quotes = (d.quotes || []).filter((q) => String(q.text || "").trim()).map((q, i) => formatQuote(q, org, i === 0 ? "said" : "added"));
    if (body.length) paras.push(body[0]);
    if (quotes.length) paras.push(quotes[0]);
    paras.push(...body.slice(1), ...quotes.slice(1));
    if (normWs(d.cta)) paras.push(normWs(d.cta));
    let out = paras.join("\n\n");
    out += `\n\n## ${heading}\n\n${normWs(boilerplate)}`;
    for (const x of extra) out += `\n\n_${normWs(x)}_`;
    if (fls) out += `\n\n## ${fls.heading || "Forward-looking statements"}\n\n${normWs(fls.text || "")}`;
    const mc = mediaContactLines(mediaContact);
    if (mc.length) out += "\n\n## Media contact\n\n" + mc.join("  \n");
    return out + `\n\n${endMark}\n`;
  }
  function parseDate(s) { const m = String(s || "").trim().slice(0, 10).match(/^(\d{4})-(\d{2})-(\d{2})$/); if (!m) return null; const [y, mo, d] = [+m[1], +m[2], +m[3]];
    const t = new Date(Date.UTC(y, mo - 1, d)); return t.getUTCMonth() === mo - 1 && t.getUTCDate() === d ? { y, m: mo, d } : null; }
  function embargoLabel(embargo, months) { // -> [display, parsed {y,m,d,epoch}, problem]
    if (!embargo) return [null, null, null];
    const m = String(embargo).trim().match(/^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?)?\s*(Z|[+-]\d{2}:?\d{2})?$/);
    if (!m || !parseDate(m.slice(1, 4).join("-"))) return [String(embargo).trim(), null, `Embargo '${embargo}' is not ISO 8601 (e.g. 2026-10-21T09:00:00-05:00).`];
    const [y, mo, d, hh, mm] = [+m[1], +m[2], +m[3], +(m[4] || 0), +(m[5] || 0)];
    let tzs = "local time", epoch = null;
    if (m[7]) {
      const z = m[7] === "Z" ? "+00:00" : m[7].length === 5 ? `${m[7].slice(0, 3)}:${m[7].slice(3)}` : m[7];
      tzs = `UTC${z}`;
      const sign = z[0] === "-" ? -1 : 1; const offMin = sign * (+z.slice(1, 3) * 60 + +z.slice(4, 6));
      epoch = Date.UTC(y, mo - 1, d, hh, mm, +(m[6] || 0)) - offMin * 60000;
    }
    const hour = String(hh % 12 === 0 ? 12 : hh % 12);
    const clock = `${hour}${mm === 0 ? "" : ":" + String(mm).padStart(2, "0")} ${hh < 12 ? "a.m." : "p.m."}`;
    return [`${clock} ${tzs}, ${apDate({ y, m: mo, d }, months)}`, { y, m: mo, d, epoch }, m[7] ? null : "Embargo has no timezone offset - wires need an explicit zone."];
  }

  /* ------------------------------------------------------------- validation (shared by first draft and repair) */
  function validate(d, data, brand) {
    const rec = data.record, style = rec.style || {}, months = rec.ap_months || MONTHS;
    const checks = [];
    const orgNames = [data.organisation, ...(rec.name_variants || [])];
    const okDl = validateDateline(data.dateline);
    checks.push({ rule: "ap.dateline", status: okDl ? "pass" : "fail", severity: "high", ref: "AP Stylebook: datelines",
      message: `Dateline '${data.dateline}' ` + (okDl ? "follows AP format (CITY, State abbr., Mon. D, YYYY —)." : "does not follow AP format.") });
    const h = d.headline, hmax = style.headline_max_chars ?? 100, hwarn = style.headline_warn_chars ?? 80;
    const hst = h.length > hmax ? "fail" : h.length > hwarn ? "warn" : "pass";
    checks.push({ rule: "headline.length", status: hst, severity: "medium", target: "headline", message: `Headline is ${h.length} chars (warn > ${hwarn}, max ${hmax}).` });
    const caps = h.match(/\b[A-Z]{5,}\b/g) || [];
    if (caps.length) checks.push({ rule: "headline.caps", status: "warn", severity: "low", target: "headline", message: `ALL-CAPS words: ${caps.join(", ")}` });
    if (d.subheadline.length > (style.subheadline_max_chars ?? 180)) checks.push({ rule: "subheadline.length", status: "warn", severity: "low", target: "subheadline", message: `Subheadline is ${d.subheadline.length} chars (max ${style.subheadline_max_chars ?? 180}).` });
    const lead = checkLead(d.lead, orgNames, style.announcement_verbs || [], data.day, months);
    const missing = ["who", "what", "when"].filter((k) => !lead[k]);
    const longLead = lead.words > (style.lead_max_words ?? 45) || lead.words < (style.lead_min_words ?? 12);
    const lst = missing.length >= 2 ? "fail" : missing.length || longLead ? "warn" : "pass";
    checks.push({ rule: "structure.inverted_pyramid", status: lst, severity: lst === "fail" ? "high" : "medium", target: "lead", ref: "AP Stylebook: leads", data: lead,
      message: `Lead (${lead.words} words) ` + (missing.length ? `is missing: ${missing.join(", ")}.` : "covers who, what and when.") + (longLead ? ` Aim for ${style.lead_min_words ?? 12}-${style.lead_max_words ?? 45} words.` : "") });
    const withText = d.quotes.filter((q) => String(q.text || "").trim());
    const attributed = withText.filter((q) => String(q.speaker || "").trim().split(/\s+/).filter(Boolean).length >= 2);
    const unattributed = withText.filter((q) => String(q.speaker || "").trim().split(/\s+/).filter(Boolean).length < 2).map((q) => q.text.slice(0, 40));
    const qst = attributed.length && !unattributed.length ? "pass" : attributed.length ? "warn" : "fail";
    checks.push({ rule: "structure.quote_attribution", status: qst, severity: qst === "fail" ? "high" : "medium",
      message: qst === "pass" ? `${attributed.length} attributed quote(s).` : qst === "fail" ? "No attributed quote (named speaker + title required)." : `Quotes without a full speaker name: ${unattributed.join("; ")}` });
    if (attributed.some((q) => !String(q.title || "").trim())) checks.push({ rule: "structure.quote_title", status: "warn", severity: "low", message: "A quoted speaker has no title." });
    const text = [d.headline, d.subheadline, d.lead, ...d.body_paragraphs, ...d.quotes.map((q) => q.text), d.cta].join(" ");
    const banned = (brand.banned_phrases || []).filter((p) => text.toLowerCase().includes(String(p).toLowerCase()));
    if (banned.length) checks.push({ rule: "brand.banned_phrases", status: "fail", severity: "high", message: `Banned phrases: ${banned.join(", ")}` });
    const terms = findTerms(text, rec.forward_looking_terms || []);
    return { checks, facts: { lead, fls_terms: terms, fls_sentences: sentencesWith(text, terms) } };
  }
  function reconcile(draft, data) {
    const [name, title] = data.spokesperson;
    const quotes = (draft.quotes || []).filter((q) => q && String(q.text || "").trim()).slice(0, 3).map((q) => ({ text: String(q.text), speaker: String(q.speaker || ""), title: String(q.title || "") }));
    if (name && quotes.length) quotes[0] = { text: quotes[0].text, speaker: name, title: title || quotes[0].title };
    let lead = normWs(draft.lead);
    const dl = data.dateline.replace(/[ —]+$/, "");
    if (dl && lead.toUpperCase().startsWith(dl.split(",")[0])) lead = lead.replace(/^[A-Z][A-Z .'-]+,[^—–-]{0,60}[—–-]+\s*/, "");
    return { headline: normWs(draft.headline).replace(/\.+$/, ""), subheadline: normWs(draft.subheadline), lead,
      body_paragraphs: (draft.body_paragraphs || []).map(normWs).filter((p) => p && p !== "###" && p !== "-30-"), quotes, cta: normWs(draft.cta) };
  }

  /* ------------------------------------------------------------- the agent */
  CAF.agent("press_release_creation", {
    async run(input, ctx) {
      // -- prepare (deterministic): record, dateline, release metadata
      const rec = ctx.skill("press_boilerplate", {});
      const style = rec.style || {}, months = rec.ap_months || MONTHS;
      const [embDisplay, emb, embProblem] = embargoLabel(input.embargo, months);
      if (embProblem) ctx.check({ rule: "embargo.format", status: "warn", severity: "medium", message: embProblem });
      let day = null;
      if (input.date) { day = parseDate(input.date); if (!day) ctx.check({ rule: "date.format", status: "warn", severity: "medium", message: `Release date '${input.date}' is not YYYY-MM-DD; using today.` }); }
      if (emb) {
        const ed = { y: emb.y, m: emb.m, d: emb.d };
        if (day && (day.y !== ed.y || day.m !== ed.m || day.d !== ed.d)) ctx.check({ rule: "embargo.date", status: "warn", severity: "medium",
          message: `Release date ${input.date} differs from the embargo date ${ed.y}-${String(ed.m).padStart(2, "0")}-${String(ed.d).padStart(2, "0")}; dateline uses the embargo date.` });
        day = ed;
        if (emb.epoch !== null && emb.epoch < Date.now()) ctx.check({ rule: "embargo.past", status: "warn", severity: "high", message: `Embargo ${embDisplay} is already in the past.` });
      }
      day = day || parseDate(U.today());
      const releaseDay = `${day.y}-${String(day.m).padStart(2, "0")}-${String(day.d).padStart(2, "0")}`;
      const location = input.location || rec.headquarters_dateline || "";
      if (!input.location) ctx.warn(`No location given - dateline uses headquarters (${location}).`);
      const dateline = location ? buildDateline(location, day, rec) : "";
      const organisation = rec.organisation || ctx.brand.name || "";
      const pick = (o) => Object.fromEntries(Object.entries(o || {}).filter(([, v]) => v).map(([k, v]) => [k, String(v)]));
      const mediaContact = Object.keys(pick(rec.media_contact)).length ? pick(rec.media_contact) : pick(ctx.brand.media_contact);
      const spokesperson = parseSpokesperson(input.spokesperson);
      const approved = [...(input.approved_quotes || [])];
      for (const q of ((ctx.upstreamOutput("quote_extraction") || {}).quotes || []).slice(0, 5)) if (q.text && q.verified !== false) approved.push(`${q.text} - ${q.speaker || "unattributed"}`);
      const brief = ctx.upstreamOutput("campaign_brief_generation") || {};
      const releaseLabel = embDisplay ? `${style.embargo_label || "EMBARGOED UNTIL"} ${embDisplay}` : style.release_label || "FOR IMMEDIATE RELEASE";
      const data = { record: rec, dateline, day, organisation, spokesperson };

      // -- draft (model)
      ctx.progress("Drafting the release");
      const raw = await ctx.generate("draft", { overview: input.overview, target_audience: input.target_audience, call_to_action: input.call_to_action, organisation,
        release_date: releaseDay, dateline, spokesperson_name: spokesperson[0], spokesperson_title: spokesperson[1], approved_quotes: approved, key_messages: brief.key_messages || [],
        headline_max: style.headline_warn_chars ?? 80, lead_max: style.lead_max_words ?? 45, min_words: (style.word_count || {}).min ?? 300, max_words: (style.word_count || {}).max ?? 800 });
      let draft = reconcile(raw, data);

      // -- validate (+ one repair pass when Claude is connected)
      let v = validate(draft, data, ctx.brand);
      const failures = v.checks.filter((c) => c.status === "fail" && c.rule !== "ap.dateline").map((c) => `${c.rule}: ${c.message}`);
      if (failures.length && ctx.llmLive) {
        ctx.progress("Repairing the draft after validator findings");
        const fixed = await ctx.generate("repair", { findings: failures, draft, organisation, release_date: releaseDay, headline_max: style.headline_warn_chars ?? 80 });
        draft = reconcile(fixed, data);
        v = validate(draft, data, ctx.brand);
      }
      v.checks.forEach((c) => ctx.check(c));

      // -- finalise: legal, assembly, structure, distribution
      const disclaimers = rec.disclaimers || {};
      const fls = v.facts.fls_terms.length ? disclaimers.forward_looking : null;
      if (v.facts.fls_terms.length) {
        if (fls && fls.text) ctx.check({ rule: "legal.forward_looking", status: "warn", severity: "medium", ref: "PSLRA safe harbor",
          message: `Forward-looking terms ${JSON.stringify(v.facts.fls_terms)} found; the safe-harbour disclaimer was appended - legal review advised.`, data: { terms: v.facts.fls_terms, sentences: v.facts.fls_sentences } });
        else ctx.check({ rule: "legal.forward_looking", status: "fail", severity: "critical", ref: "PSLRA safe harbor", message: `Forward-looking terms ${JSON.stringify(v.facts.fls_terms)} found but the boilerplate record has no disclaimer text.` });
      } else ctx.check({ rule: "legal.forward_looking", status: "pass", severity: "low", message: "No forward-looking statements detected." });
      const boiler = rec.boilerplate || {};
      const bpText = normWs(typeof boiler === "object" ? boiler.text || "" : String(boiler));
      const bpHeading = typeof boiler === "object" && boiler.heading ? boiler.heading : `About ${organisation}`;
      const endMark = style.end_mark || "###";
      const body = assembleBody(dateline, draft, organisation, { heading: bpHeading, boilerplate: bpText, fls: fls && fls.text ? fls : null,
        extra: disclaimers.always || [], mediaContact, endMark });
      const words = wc(body), lo = (style.word_count || {}).min ?? 300, hi = (style.word_count || {}).max ?? 800;
      ctx.check({ rule: "structure.word_count", status: words >= lo && words <= hi ? "pass" : "warn", severity: "low", message: `${words} words (recommended ${lo}-${hi}).` });
      const endOk = body.trimEnd().endsWith(endMark);
      ctx.check({ rule: "structure.end_mark", status: endOk ? "pass" : "fail", severity: "medium", message: `End mark '${endMark}' ${endOk ? "present." : "missing."}` });
      ctx.check({ rule: "structure.boilerplate", status: bpText ? "pass" : "fail", severity: bpText ? "low" : "critical",
        message: bpText ? "Boilerplate inserted from the versioned record." : "No boilerplate in config/skills/press_boilerplate.yaml." });
      const mcOk = mediaContactLines(mediaContact).length > 0 && !!(mediaContact.email || mediaContact.phone);
      ctx.check({ rule: "structure.media_contact", status: mcOk ? "pass" : "fail", severity: mcOk ? "low" : "critical",
        message: mcOk ? "Media contact inserted." : "No media contact (press_boilerplate or brand_voice.media_contact)." });
      const failed = ctx.state.checks.filter((c) => c.status === "fail");
      const critical = failed.filter((c) => c.severity === "critical");
      const output = { headline: draft.headline, subheadline: draft.subheadline, dateline, body_markdown: body, quotes: draft.quotes, boilerplate: bpText,
        media_contact: mediaContact, word_count: words, forward_looking_detected: v.facts.fls_terms.length > 0, forward_looking_terms: v.facts.fls_terms,
        release_label: releaseLabel, release_date: releaseDay, embargo: input.embargo || null, boilerplate_version: String(rec.version ?? ""), validation_passed: !failed.length };
      const full = `${releaseLabel}\n\n# ${draft.headline}\n\n` + (draft.subheadline ? `_${draft.subheadline}_\n\n` : "") + body;
      ctx.artifact({ name: "press_release.md", mime: "text/markdown", content: full, description: "Press release" });
      ctx.artifact({ name: "press_release.json", mime: "application/json", content: JSON.stringify(output, null, 2), description: "Press release (structured)" });
      if (input.distribute !== false) {
        if (critical.length) ctx.warn(`Newswire distribution not proposed: fix the critical checks first (${critical.map((c) => c.rule).join("; ")}).`);
        else {
          const regions = (input.regions && input.regions.length ? input.regions : (rec.distribution || {}).default_regions) || ["US"];
          const wireBody = `${releaseLabel}\n\n` + (draft.subheadline ? `_${draft.subheadline}_\n\n` : "") + body;
          ctx.action({ id: `newswire-${CAF.lib.text.slugify(draft.headline, 24)}`, kind: "newswire.distribute", connector: "newswire", method: "distribute",
            payload: { headline: draft.headline, body: wireBody, embargo: input.embargo || null, regions,
              metadata: { subheadline: draft.subheadline, dateline, release_date: releaseDay, word_count: words, forward_looking: v.facts.fls_terms.length > 0,
                boilerplate_version: String(rec.version ?? ""), categories: (rec.distribution || {}).categories || [], audience: input.target_audience || null, run_id: ctx.runId } },
            summary: `Distribute '${draft.headline}' to the newswire (${regions.join(", ")})` + (embDisplay ? `, embargoed until ${embDisplay}` : "")
              + (failed.length ? ` (${failed.length} validation issue(s) - review before approving)` : "") });
        }
      }
      return {
        output,
        summary: `Press release '${draft.headline.slice(0, 70)}': ${words} words, ${draft.quotes.length} quote(s)` + (v.facts.fls_terms.length ? ", forward-looking disclaimer added" : "")
          + (failed.length ? `; ${failed.length} validation issue(s)` : "; validator passed"),
        view: [
          { type: "markdown", title: releaseLabel, markdown: full },
          { type: "kv", title: "Release record", data: { dateline, release_date: releaseDay, word_count: words, forward_looking: v.facts.fls_terms.join(", ") || "none",
            boilerplate_version: String(rec.version ?? ""), media_contact: mediaContactLines(mediaContact).join(" · ") } },
        ],
      };
    },
  });
})();
