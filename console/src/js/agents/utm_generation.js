/* UTM Generation - browser twin of caf/agents/strategy/utm_generation.py (deterministic). */
CAF.agent("utm_generation", {
  async run(input, ctx) {
    const tax = ctx.skill("utm_taxonomy");
    const clean = (v) => (v === null || v === undefined || String(v).trim() === "" ? null : CAF.lib.text.slugify(String(v), 80).replace(/-/g, "_"));
    const parts = Object.fromEntries(Object.entries(input.campaign_parts || {}).map(([k, v]) => [k, clean(v)]));
    let campaign = input.campaign;
    const brief = ctx.upstreamOutput("campaign_brief_generation");
    if (!campaign && !Object.keys(parts).length && brief) campaign = brief.campaign_name;
    if (!campaign) {
      const need = [...tax.campaign_pattern.matchAll(/{(\w+)}/g)].map((m) => m[1]);
      const missing = need.filter((p) => !parts[p]);
      if (missing.length) {
        ctx.check({ rule: "campaign.parts", status: "fail", severity: "high", message: `Campaign pattern '${tax.campaign_pattern}' needs parts ${missing.join(", ")}.` });
        campaign = Object.values(parts).filter(Boolean).join("_") || "unnamed_campaign";
      } else {
        campaign = tax.campaign_pattern.replace(/{(\w+)}/g, (_, k) => parts[k]);
        for (const [name, rx] of Object.entries(tax.campaign_parts || {})) if (parts[name] && !new RegExp(rx).test(parts[name]))
          ctx.check({ rule: `campaign.part.${name}`, status: "fail", severity: "medium", message: `'${parts[name]}' does not match the ${name} rule ${rx}.` });
      }
    }
    campaign = clean(campaign) || "unnamed_campaign";
    const ok = new RegExp(tax.value_pattern).test(campaign) && campaign.length <= (tax.max_value_length || 60);
    ctx.check({ rule: "campaign.naming", status: ok ? "pass" : "fail", severity: "high", message: `Campaign '${campaign}' ${ok ? "complies with" : "breaks"} the naming convention.` });

    let base;
    try { base = new URL(String(input.base_url).trim()); if (!/^https?:$/.test(base.protocol)) throw new Error(); }
    catch { ctx.check({ rule: "url.valid", status: "fail", severity: "critical", message: `'${input.base_url}' is not an absolute http(s) URL.` }); base = null; }
    if (base && [...base.searchParams.keys()].some((k) => k.toLowerCase().startsWith("utm_")))
      ctx.check({ rule: "url.existing_utm", status: "warn", severity: "low", message: "Base URL already had UTM parameters; they were replaced." });

    const channels = (input.channels || []).some((c) => c === "all" || c === "*") ? Object.keys(tax.channels) : (input.channels && input.channels.length ? input.channels : ["email"]);
    const rows = input.mode === "single" ? [["custom", input.source, input.medium]] : channels.map((ch) => {
      const cfg = tax.channels[ch];
      if (!cfg) { ctx.check({ rule: "channel.known", status: "fail", severity: "medium", message: `Channel '${ch}' is not in the taxonomy (${Object.keys(tax.channels).join(", ")}).` }); return null; }
      return [ch, cfg.source, cfg.medium];
    }).filter(Boolean);
    const links = []; const seen = new Set();
    for (const [channel, s, m] of rows) {
      const source = clean(s), medium = clean(m);
      if (!source || !medium) { ctx.check({ rule: "params.required", status: "fail", severity: "high", message: `${channel}: utm_source and utm_medium are required.` }); continue; }
      if (!(tax.allowed_sources || []).includes(source)) ctx.check({ rule: "source.allowed", status: "warn", severity: "low", target: channel, message: `utm_source '${source}' is not in the approved list.` });
      if (!(tax.allowed_mediums || []).includes(medium)) ctx.check({ rule: "medium.allowed", status: "warn", severity: "medium", target: channel, message: `utm_medium '${medium}' is not an approved medium.` });
      const params = { utm_source: source, utm_medium: medium, utm_campaign: campaign, utm_content: clean(input.content), utm_term: clean(input.term), utm_id: clean(input.utm_id) };
      let url = String(input.base_url);
      if (base) {
        const u = new URL(base.toString());
        for (const k of [...u.searchParams.keys()]) if (k.toLowerCase().startsWith("utm_")) u.searchParams.delete(k);
        for (const [k, v] of Object.entries(params)) if (v) u.searchParams.set(k, v);
        url = u.toString();
      }
      if (seen.has(url)) ctx.check({ rule: "links.duplicate", status: "warn", severity: "low", message: `Duplicate link for ${channel}.` });
      seen.add(url);
      links.push({ channel, source, medium, campaign, content: params.utm_content, term: params.utm_term, url });
    }
    const compliant = !ctx.state.checks.some((c) => c.status === "fail");
    const csv = CAF.lib.csv.stringify(links.map((l) => ({ channel: l.channel, utm_source: l.source, utm_medium: l.medium, utm_campaign: l.campaign, utm_content: l.content || "", utm_term: l.term || "", url: l.url })));
    ctx.artifact({ name: "utm_links.csv", mime: "text/csv", content: csv, description: "UTM link matrix" });
    return {
      output: { campaign, links, taxonomy_version: String(tax.version || ""), compliant },
      summary: `${links.length} UTM link${links.length === 1 ? "" : "s"} for campaign '${campaign}'${compliant ? "" : " - taxonomy issues found"}`,
      view: [{ type: "table", title: "Tagged links", rows: links.map((l) => ({ channel: l.channel, source: l.source, medium: l.medium, url: l.url })) }],
    };
  },
});
