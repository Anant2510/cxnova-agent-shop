/* Webinar Follow Up Email Creation - browser twin of caf/agents/content/webinar_followup_email.py.
   Attendance + info + transcript (embedded webinar data) -> Content Summary as a sub-agent (ctx.runSubagent)
   -> deterministic YAML segmentation (webinar_segments) with engagement flag + MQL scoring -> one email per
   segment (template) rendered/linted with CAF.lib.email -> gated ESP export per segment. */
(() => {
  const EMAIL_RX = /^[^@\s,;]+@[^@\s,;]+\.[A-Za-z]{2,}$/;
  const FIRST_NAME_TAG = "{{first_name}}";
  const OPS = {
    "==": (a, b) => a === b, "!=": (a, b) => a !== b, ">": (a, b) => a > b, ">=": (a, b) => a >= b, "<": (a, b) => a < b, "<=": (a, b) => a <= b,
    in: (a, b) => (b || []).includes(a), not_in: (a, b) => !(b || []).includes(a),
  };
  const normWs = (t) => String(t ?? "").replace(/\s+/g, " ").trim();
  const num = (v) => { const f = Number(v); return v === null || v === undefined || v === "" || Number.isNaN(f) ? 0 : f; };
  const truthy = (v) => (typeof v === "string" ? ["1", "true", "yes", "y", "attended", "x"].includes(v.trim().toLowerCase()) : !!v && !Number.isNaN(v));

  /* ------------------------------------------------------------- pure helpers (rule engine, ported 1:1) */
  function resolveColumns(columns, aliases) {
    const lower = Object.fromEntries(columns.map((c) => [String(c).toLowerCase().trim(), c]));
    const out = {};
    for (const [field, names] of Object.entries(aliases || {})) {
      for (const n of [field, ...(names || [])]) { const k = String(n).toLowerCase().trim(); if (k in lower) { out[field] = lower[k]; break; } }
    }
    return out;
  }
  function normaliseAttendees(rows, colmap, duration) {
    const dur = duration > 0 ? duration : 60;
    return rows.map((r) => {
      const p = {};
      for (const f of ["email", "first_name", "last_name", "company", "job_title"]) p[f] = f in colmap ? String(r[colmap[f]] ?? "").trim() : "";
      p.email = p.email.toLowerCase();
      p.attended = "attended" in colmap ? truthy(r[colmap.attended]) : false;
      let minutes = "minutes" in colmap ? num(r[colmap.minutes]) : 0;
      if (!("minutes" in colmap) && "join_minute" in colmap && "leave_minute" in colmap) minutes = Math.max(0, num(r[colmap.leave_minute]) - num(r[colmap.join_minute]));
      p.minutes = minutes;
      p.watch_pct = Math.round(Math.min(1, Math.max(0, minutes / dur)) * 1000) / 1000;
      for (const f of ["questions", "polls", "resources"]) p[f] = f in colmap ? Math.trunc(num(r[colmap[f]])) : 0;
      return p;
    });
  }
  function evalCondition(row, cond) {
    const op = OPS[String(cond.op || "==")];
    if (!op) throw new Error(`Unknown operator '${cond.op}' in webinar segment rules`);
    const value = row[cond.field], want = cond.value;
    if (typeof want === "boolean") return op(!!value, want);
    if (typeof want === "number") return op(num(value), want);
    return op(value, want);
  }
  function evalRule(row, rule) {
    if (!rule || !Object.keys(rule).length) return false;
    if (rule.all) return rule.all.every((c) => ("field" in c ? evalCondition(row, c) : evalRule(row, c)));
    if (rule.any) return rule.any.some((c) => ("field" in c ? evalCondition(row, c) : evalRule(row, c)));
    return "field" in rule ? evalCondition(row, rule) : false;
  }
  const leadScore = (row, weights) => Math.min(100, Math.round(Object.entries(weights || {}).reduce((s, [k, w]) => s + Number(w) * num(row[k]), 0)));
  function segmentAttendees(people, rules) {
    const segs = rules.segments || [];
    const eng = Object.fromEntries(Object.entries(rules.engaged || {}).filter(([k]) => k === "all" || k === "any"));
    const mql = rules.mql || {};
    const consumer = new Set((mql.consumer_companies || []).map((c) => String(c).trim().toLowerCase()));
    return people.map((rec) => {
      const seg = (segs.find((s) => evalRule(rec, s.when)) || {}).id || "unsegmented";
      const engaged = evalRule(rec, eng);
      const isMql = (engaged || mql.require_engaged === false) && (mql.segments || []).includes(seg)
        && !(mql.exclude || []).some((x) => evalRule(rec, x.all || x.any ? x : { all: [x] }));
      return { ...rec, segment: seg, engaged, mql: isMql, lead_score: isMql ? leadScore(rec, mql.score_weights) : 0,
        lead_type: consumer.has(String(rec.company || "").trim().toLowerCase()) ? "consumer" : "business" };
    });
  }
  function engagementReason(r) {
    const bits = [`watched ${Math.round(num(r.watch_pct) * 100)}%`];
    if (num(r.questions)) bits.push(`asked ${Math.trunc(num(r.questions))} question(s)`);
    if (num(r.polls)) bits.push(`answered ${Math.trunc(num(r.polls))} poll(s)`);
    if (num(r.resources)) bits.push(`clicked ${Math.trunc(num(r.resources))} resource(s)`);
    return bits.join("; ");
  }
  const speakersLine = (info) => (info.speakers || []).filter((s) => s && typeof s === "object").map((s) => [s.name, s.title].filter(Boolean).join(", ")).join("; ");
  const ruleText = (rule) => { for (const [k, j] of [["any", " or "], ["all", " and "]]) if (rule && rule[k]) return rule[k].filter((c) => "field" in c).map((c) => `${c.field} ${c.op} ${c.value}`).join(j); return "n/a"; };
  function buildUrl(base, params) { // twin of utm_generation.build_url
    try {
      const u = new URL(String(base).trim());
      for (const k of [...u.searchParams.keys()]) if (k.toLowerCase().startsWith("utm_")) u.searchParams.delete(k);
      for (const [k, v] of Object.entries(params)) if (v) u.searchParams.append(k, v);
      return u.toString();
    } catch { return base; }
  }
  const utmValue = (v) => (v === null || v === undefined || !String(v).trim() ? null : CAF.lib.text.slugify(String(v), 80).replace(/-/g, "_").toLowerCase());

  /* ------------------------------------------------------------- the agent */
  CAF.agent("webinar_followup_email", {
    async run(input, ctx) {
      const rules = ctx.skill("webinar_segments");
      // -- 1. attendance (webinar connector twin: embedded CSV / info / transcript)
      const info = { ...((ctx.hasData("webinar/info") ? await ctx.data("webinar/info") : null) || {}) };
      const raw = ctx.hasData("webinar/attendees") ? await ctx.data("webinar/attendees") : [];
      const transcript = input.transcript || (ctx.hasData("webinar/transcript") ? await ctx.data("webinar/transcript") : "") || "";
      if (input.webinar_id && info.webinar_id && input.webinar_id !== info.webinar_id)
        ctx.warn(`The hosted console embeds one webinar (${info.webinar_id}); '${input.webinar_id}' needs the Python webinar connector.`);
      if (input.recording_url) info.recording_url = input.recording_url;
      if (input.slides_url) info.slides_url = input.slides_url;
      const colmap = resolveColumns(raw.length ? Object.keys(raw[0]) : [], rules.columns || {});
      const missing = (rules.required || ["email", "attended"]).filter((f) => !(f in colmap));
      if (missing.length) throw new Error(`Attendance export lacks required column(s) ${missing.join(", ")}; add aliases in webinar_segments.yaml.`);
      const optMissing = ["minutes", "questions", "polls"].filter((f) => !(f in colmap));
      ctx.check({ rule: "data.columns", status: optMissing.length ? "warn" : "pass", severity: optMissing.length ? "medium" : "low",
        message: "Mapped attendance columns: " + Object.entries(colmap).map(([k, v]) => `${k}<-${v}`).join(", ") + (optMissing.length ? `. Missing optional: ${optMissing.join(", ")}` : "") });
      let duration = num(info.duration_minutes);
      if (!duration) {
        duration = Math.max(...raw.map((r) => num(r[colmap.leave_minute])), ...raw.map((r) => num(r[colmap.minutes])), 0) || 60;
        ctx.warn(`Webinar duration not in info; using ${duration} minutes.`);
      }
      let people = normaliseAttendees(raw, colmap, duration);
      const invalid = people.filter((p) => !EMAIL_RX.test(p.email)).length;
      if (invalid) ctx.check({ rule: "data.invalid_emails", status: "warn", severity: "medium", message: `${invalid} row(s) with an invalid email were excluded.` });
      people = people.filter((p) => EMAIL_RX.test(p.email));
      const seen = new Map();
      // keep the best registration per email (attended, then most minutes) at its own position - pandas sort/drop_duplicates/sort_index
      people.forEach((p, i) => { const prev = seen.get(p.email); if (!prev || (p.attended && !prev.p.attended) || (p.attended === prev.p.attended && p.minutes > prev.p.minutes)) seen.set(p.email, { p, i }); });
      const dupes = people.length - seen.size;
      if (dupes) { ctx.check({ rule: "data.duplicates", status: "warn", severity: "low", message: `${dupes} duplicate registration(s) merged.` }); people = [...seen.values()].sort((a, b) => a.i - b.i).map((x) => x.p); }
      const odd = people.filter((p) => !p.attended && (p.minutes > 0 || p.questions > 0 || p.polls > 0)).length;
      if (odd) ctx.check({ rule: "data.consistency", status: "warn", severity: "low", message: `${odd} registrant(s) marked as not attended but with session activity - kept as no-shows.` });
      const registered = people.length, attended = people.filter((p) => p.attended).length;
      ctx.check({ rule: "attendance.rate", status: "info", severity: "low", message: `${attended} of ${registered} registrants attended (${Math.round((attended / Math.max(1, registered)) * 100)}%); duration ${Math.round(duration)} min.` });
      if (!transcript.trim()) ctx.warn("No transcript available - emails are drafted without session takeaways.");

      // -- 2. summarise the transcript with the Content Summary agent (sub-agent)
      let takeaways = [], takeawaysMethod = "none";
      if (transcript.trim()) {
        ctx.progress("Summarising the transcript (Content Summary sub-agent)");
        const sub = await ctx.runSubagent("content_summary", { text: transcript, title: info.title, mode: "bullet", max_words: 160, chunk_words: 700,
          focus: "practical takeaways and advice for homeowners" });
        for (const r of (sub.provenance || {}).degraded_reasons || []) ctx.degraded(`content_summary: ${r}`);
        if (sub.status === "error") {
          ctx.warn(`Transcript summary failed (${sub.error}); emails drafted without takeaways.`);
          ctx.degraded("transcript summary unavailable");
        } else {
          takeaways = (sub.output.key_points || []).map(normWs).filter(Boolean).slice(0, 5);
          takeawaysMethod = sub.output.method || "";
        }
        const first = (sub.artifacts || [])[0]; // the sub-agent's summary.md rolls up into this run (result.artifacts[:1])
        if (first) ctx.artifact({ name: first.name, mime: first.mime, content: first.content, description: first.description || "Transcript summary (Content Summary sub-agent)" });
        ctx.check({ rule: "transcript.summary", status: takeaways.length ? "info" : "warn", severity: "low",
          message: `Content Summary sub-agent (${takeawaysMethod || "n/a"}): ${takeaways.length} takeaway(s) from ${(sub.status !== "error" ? sub.output.source_words || 0 : 0).toLocaleString("en-US")} transcript words.` });
      }

      // -- 3. segment (deterministic rule engine) + recipient / MQL CSVs
      const seg = segmentAttendees(people, rules);
      const cols = ["email", "first_name", "last_name", "company", "job_title", "segment", "engaged", "minutes", "watch_pct", "questions", "polls", "resources"];
      const wanted = new Set(input.only_segments && input.only_segments.length ? input.only_segments : (rules.segments || []).map((s) => s.id));
      const segments = [];
      for (const s of rules.segments || []) {
        if (!wanted.has(s.id)) continue;
        const members = seg.filter((p) => p.segment === s.id);
        let ref = null;
        if (members.length) ref = ctx.artifact({ name: `recipients-${s.id}.csv`, mime: "text/csv", content: CAF.lib.csv.stringify(members.map((m) => ({ ...m, engaged: m.engaged ? "True" : "False" })), cols), description: `Recipients: ${s.label || s.id}` });
        segments.push({ segment: s.id, label: s.label || s.id, criteria: s.criteria || "", count: members.length, engaged_count: members.filter((m) => m.engaged).length,
          brief: s.email || {}, recipients_artifact: ref ? ref.name : null });
      }
      const unseg = seg.filter((p) => p.segment === "unsegmented").length;
      const allCounts = (rules.segments || []).map((s) => `${s.id}=${seg.filter((p) => p.segment === s.id).length}`).join(", ");
      const selected = input.only_segments && input.only_segments.length ? ` Emails limited to ${[...wanted].sort().join(", ")}.` : "";
      ctx.check({ rule: "segments.coverage", status: unseg ? "warn" : "pass", severity: unseg ? "medium" : "low",
        message: (unseg ? `${unseg} registrant(s) matched no segment rule (${allCounts}).` : `Every registrant is in exactly one segment: ${allCounts}.`) + selected });
      const mql = seg.filter((p) => p.mql).map((p) => ({ ...p, engagement: engagementReason(p), owner: (rules.mql || {}).owner || "Sales" })).sort((a, b) => b.lead_score - a.lead_score);
      let mqlRef = null;
      if (mql.length) mqlRef = ctx.artifact({ name: "mql_for_sales.csv", mime: "text/csv", description: "MQL hand-off for sales (engaged attendees)",
        content: CAF.lib.csv.stringify(mql, ["email", "first_name", "last_name", "company", "job_title", "lead_type", "lead_score", "segment", "engagement", "owner"]) });
      const biz = mql.filter((m) => m.lead_type === "business").length;
      ctx.check({ rule: "mql.handoff", status: "info", severity: "low", message: `${mql.length} MQL(s) for sales (engaged = ${ruleText(rules.engaged || {})}; segments [${((rules.mql || {}).segments || []).map((x) => `'${x}'`).join(", ")}])`
        + (mql.length ? `: ${biz} business, ${mql.length - biz} consumer.` : ".") });

      // -- 4. draft one email per segment (template) with deterministic CTA + UTM
      const tax = ctx.skill("utm_taxonomy", {});
      const ch = (tax.channels || {})[(rules.utm || {}).channel || "email"] || {};
      const campaign = CAF.lib.text.slugify(String(info.webinar_id || info.title || "webinar"), 60).replace(/-/g, "_");
      const ctaUrl = (brief) => {
        const target = brief.cta || "recording_url";
        let url = /^https?:/i.test(target) ? target : info[target];
        url = url || info.recording_url || ctx.brand.website;
        if (!url || input.tag_utm === false) return url || null;
        return buildUrl(url, { utm_source: utmValue(ch.source || "newsletter"), utm_medium: utmValue(ch.medium || "email"), utm_campaign: campaign, utm_content: brief.utm_content });
      };
      const reconcile = (e, brief) => {
        let cta = normWs(e.cta_text);
        if (!ctx.llmLive || !cta || CAF.lib.text.wordCount(cta) > 5 || cta.startsWith("[")) cta = brief.cta_text || "Watch the recording";
        return { subject: normWs(e.subject), preheader: normWs(e.preheader), body_markdown: String(e.body_markdown || "").trim(), cta_text: cta };
      };
      const drafts = {};
      for (const s of segments) {
        if (!s.count) continue;
        ctx.progress(`Drafting the '${s.label}' email`);
        const e = await ctx.generate("email", { webinar_title: info.title || "our webinar", webinar_date: info.date || "", speakers: speakersLine(info), segment_label: s.label,
          criteria: s.criteria, goal: s.brief.goal || "", cta_text: s.brief.cta_text || "Watch the recording", takeaways, extra_context: input.extra_context,
          first_name_tag: FIRST_NAME_TAG, has_recording: !!info.recording_url, has_slides: !!info.slides_url });
        drafts[s.segment] = { ...reconcile(e, s.brief), cta_url: ctaUrl(s.brief) };
      }

      // -- 5. lint (CAF.lib.email) + one repair pass when Claude is connected
      const lintRules = ctx.skill("email_deliverability", {});
      const banned = ctx.brand.banned_phrases || [];
      const render = (e) => {
        const args = { subject: e.subject, preheader: e.preheader, body_markdown: e.body_markdown, cta_text: e.cta_text, cta_url: e.cta_url || "#", brand: ctx.brand };
        return { html: CAF.lib.email.html(args), mjml: CAF.lib.email.mjml(args), text: CAF.lib.email.text({ ...args, cta_url: e.cta_url }) };
      };
      const finals = {};
      for (const s of segments) {
        let e = drafts[s.segment]; if (!e) continue;
        let found = [];
        for (let attempt = 0; attempt < 2; attempt++) {
          const r = render(e);
          found = CAF.lib.email.lint({ subject: e.subject, preheader: e.preheader, html: r.html, text: r.text, cta_text: e.cta_text, rules: lintRules, banned_phrases: banned });
          const failures = found.filter((c) => c.status === "fail").map((c) => `${c.rule}: ${c.message}`);
          if (!failures.length || attempt === 1 || !ctx.llmLive) break;
          ctx.progress(`Repairing the '${s.label}' email after linter findings`);
          const fixed = await ctx.generate("repair", { findings: failures, draft: { subject: e.subject, preheader: e.preheader, body_markdown: e.body_markdown, cta_text: e.cta_text } });
          e = { ...reconcile(fixed, s.brief), cta_url: e.cta_url };
        }
        found.forEach((c) => ctx.check({ ...c, target: s.segment }));
        finals[s.segment] = e;
      }

      // -- 6. render, artifacts, gated ESP export, output
      const wid = CAF.lib.text.slugify(String(info.webinar_id || info.title || "webinar"), 40);
      const results = [], views = [];
      const lintFamilies = ["subject", "preheader", "body", "content", "brand", "compliance"];
      for (const s of segments) {
        const e = finals[s.segment];
        const res = { segment: s.segment, label: s.label, count: s.count, engaged_count: s.engaged_count, criteria: s.criteria, recipients_artifact: s.recipients_artifact,
          email: null, html_artifact: null, lint: {} };
        if (e) {
          const r = render(e);
          const a = ctx.artifact({ name: `email-${s.segment}.html`, mime: "text/html", content: r.html, description: `Follow-up email: ${s.label}` });
          ctx.artifact({ name: `email-${s.segment}.txt`, mime: "text/plain", content: r.text, description: "Plain-text version" });
          ctx.artifact({ name: `email-${s.segment}.mjml`, mime: "text/plain", content: r.mjml, description: "MJML source" });
          const mine = ctx.state.checks.filter((c) => c.target === s.segment && lintFamilies.includes(String(c.rule || "").split(".")[0]));
          Object.assign(res, { email: { ...e }, html_artifact: a.name, lint: Object.fromEntries(["pass", "warn", "fail"].map((st) => [st, mine.filter((c) => c.status === st).length])) });
          views.push({ type: "html", title: `${s.label} (${s.count}): ${e.subject}`, artifact: a.name });
          if (input.export !== false) ctx.action({ id: `esp-${s.segment}`, kind: "esp.export", connector: "esp", method: "export",
            payload: { name: `${wid}-${s.segment}`, subject: e.subject, preheader: e.preheader, html: r.html, text: r.text, mjml: r.mjml, audience: s.label,
              metadata: { segment: s.segment, criteria: s.criteria, recipients: s.count, recipients_artifact: s.recipients_artifact, webinar_id: info.webinar_id, cta_url: e.cta_url, run_id: ctx.runId } },
            summary: `Export '${e.subject}' to the ESP for ${s.count} ${s.label.toLowerCase()} recipient(s)` });
        }
        results.push(res);
      }
      const segIds = new Set(segments.map((s) => s.segment));
      const lintPassed = !ctx.state.checks.some((c) => c.status === "fail" && segIds.has(c.target));
      const webinar = Object.fromEntries(["webinar_id", "title", "date", "duration_minutes", "host", "recording_url", "slides_url", "speakers"].filter((k) => info[k] !== undefined && info[k] !== null).map((k) => [k, info[k]]));
      const output = { webinar, takeaways, segments: results, mql_count: mql.length, mql_artifact: mqlRef ? mqlRef.name : null, registered, attended,
        attendance_rate: Math.round((attended / Math.max(1, registered)) * 10000) / 10000, takeaways_method: takeawaysMethod, lint_passed: lintPassed };
      const emails = results.filter((r) => r.email).length;
      return {
        output,
        summary: `'${info.title || "Webinar"}': ${emails} segment email(s) for ${registered} registrants (${results.map((r) => `${r.segment}=${r.count}`).join(", ")}); ${mql.length} MQL(s) for sales; linter ${lintPassed ? "passed" : "flagged issues"}`,
        view: [
          { type: "table", title: "Segments", rows: results.map((r) => ({ segment: r.label, recipients: r.count, engaged: r.engaged_count, criteria: r.criteria, subject: r.email ? r.email.subject : "-" })) },
          { type: "list", title: "Session takeaways (Content Summary)", items: takeaways.length ? takeaways : ["No transcript takeaways"] },
          ...views,
          { type: "table", title: `MQL hand-off (${mql.length}, top 10 by lead score)`, rows: mql.slice(0, 10).map((m) => ({ name: `${m.first_name} ${m.last_name}`, company: m.company, type: m.lead_type, score: m.lead_score, engagement: m.engagement })) },
        ],
      };
    },
  });
})();
