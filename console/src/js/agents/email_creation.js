/* Email Creation - browser twin of caf/agents/content/email_creation.py. */
CAF.agent("email_creation", {
  async run(input, ctx) {
    const brief = ctx.upstreamOutput("campaign_brief_generation") || {};
    const utm = ctx.upstreamOutput("utm_generation") || {};
    let ctaUrl = input.cta_url;
    const emailLink = ((utm.links || []).find((l) => l.channel === "email") || {}).url;
    if (emailLink && (!ctaUrl || ctaUrl.split("?")[0] === emailLink.split("?")[0])) ctaUrl = emailLink;
    const audiences = [input.target_audience || (brief.target_audience || {}).primary || "General prospects", ...(input.audience_variants || []).filter(Boolean).slice(0, 2)];

    ctx.progress(`Drafting ${audiences.length} variant(s)`);
    const draft = await ctx.generate("draft", { overview: input.overview, target_audience: audiences[0], call_to_action: input.call_to_action,
      key_messages: brief.key_messages || [], extra_context: input.extra_context, variants: audiences });
    let variants = (draft.variants || []).slice(0, audiences.length);
    while (variants.length < audiences.length) variants.push(U.clone(variants[variants.length - 1]));
    variants.forEach((v, i) => { v.audience = audiences[i]; });

    const rules = ctx.skill("email_deliverability");
    const banned = ctx.brand.banned_phrases || [];
    const render = (v) => {
      const args = { subject: v.subject_lines[0], preheader: v.preheader, body_markdown: v.body_markdown, cta_text: v.cta_text, cta_url: ctaUrl || "#", brand: ctx.brand };
      return { html: CAF.lib.email.html(args), mjml: CAF.lib.email.mjml(args), text: CAF.lib.email.text({ ...args, cta_url: ctaUrl }) };
    };
    const finals = [];
    for (let v of variants) {
      let found;
      for (let attempt = 0; attempt < 2; attempt++) {
        const r = render(v);
        found = CAF.lib.email.lint({ subject: v.subject_lines[0], preheader: v.preheader, html: r.html, text: r.text, cta_text: v.cta_text, rules, banned_phrases: banned });
        const failures = found.filter((c) => c.status === "fail").map((c) => `${c.rule}: ${c.message}`);
        if (!failures.length || attempt === 1 || !ctx.llmLive) break;
        ctx.progress(`Repairing '${v.audience}' after linter findings`);
        v = await ctx.generate("repair", { findings: failures, draft: v });
      }
      found.forEach((c) => ctx.check({ ...c, target: v.audience }));
      finals.push(v);
    }
    const emails = [];
    finals.forEach((v, i) => {
      const r = render(v);
      const slug = CAF.lib.text.slugify(v.audience, 30) || `variant-${i + 1}`;
      const a1 = ctx.artifact({ name: `email-${i + 1}-${slug}.html`, mime: "text/html", content: r.html, description: `Email for ${v.audience}` });
      const a2 = ctx.artifact({ name: `email-${i + 1}-${slug}.txt`, mime: "text/plain", content: r.text, description: "Plain-text version" });
      const a3 = ctx.artifact({ name: `email-${i + 1}-${slug}.mjml`, mime: "text/plain", content: r.mjml, description: "MJML source" });
      const mine = ctx.state.checks.filter((c) => c.target === v.audience);
      const lint = Object.fromEntries(["pass", "warn", "fail"].map((s) => [s, mine.filter((c) => c.status === s).length]));
      emails.push({ ...v, subject: v.subject_lines[0], cta_url: ctaUrl || null, html_artifact: a1.name, text_artifact: a2.name, mjml_artifact: a3.name, lint });
      if (input.export !== false) ctx.action({ id: `esp-${i + 1}`, kind: "esp.export", connector: "esp", method: "export",
        payload: { name: slug, subject: v.subject_lines[0], preheader: v.preheader, html: r.html, text: r.text, mjml: r.mjml, audience: v.audience },
        summary: `Export '${v.subject_lines[0]}' to the ESP for ${v.audience}` });
    });
    const passed = !ctx.state.checks.some((c) => c.status === "fail");
    return {
      output: { emails, lint_passed: passed },
      summary: `${emails.length} email variant(s); subject: “${emails[0].subject}”; linter ${passed ? "passed" : "flagged issues"}`,
      view: emails.map((e) => ({ type: "html", title: `${e.audience}: ${e.subject}`, artifact: e.html_artifact })),
    };
  },
});
