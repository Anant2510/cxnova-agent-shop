/* Campaign Brief Generation - browser twin of caf/agents/strategy/campaign_brief_generation.py. */
CAF.agent("campaign_brief_generation", {
  async run(input, ctx) {
    const contract = ctx.skill("campaign_brief");
    const research = ["competitive_insights", "keyword_research", "industry_marketing"].map((id) => {
      const up = ctx.upstreamOutput(id);
      return up ? `[${id}] ${String(up.executive_summary || up.summary || JSON.stringify(up)).slice(0, 1500)}` : null;
    }).filter(Boolean).join("\n");
    ctx.progress("Drafting the seven-section brief");
    const b = await ctx.generate("brief", { ...input, sections: contract.sections, deliverable_types: contract.deliverable_types, research });

    const n = (b.key_messages || []).length;
    ctx.check({ rule: "key_messages.count", status: n >= 3 && n <= 5 ? "pass" : "warn", severity: "medium", message: `${n} key messages (contract: 3-5).` });
    const noNum = (b.objectives_and_goals || []).filter((o) => !/\d/.test(o.target || "")).map((o) => o.objective);
    ctx.check({ rule: "objectives.measurable", status: noNum.length ? "warn" : "pass", severity: "medium", message: noNum.length ? `Objectives without a numeric target: ${noNum.join("; ")}` : "Every objective has a numeric target." });
    const total = (b.channels_and_media_plan || []).reduce((s, c) => s + (Number(c.budget_share_pct) || 0), 0);
    if (total && Math.abs(total - 100) > 1) {
      b.channels_and_media_plan.forEach((c) => { c.budget_share_pct = Math.round((c.budget_share_pct * 1000) / total) / 10; });
      ctx.check({ rule: "media.budget_sum", status: "warn", severity: "low", message: `Budget shares summed to ${Math.round(total)}%; normalised to 100%.` });
    } else ctx.check({ rule: "media.budget_sum", status: "pass", severity: "low", message: "Budget shares sum to 100%." });
    const nd = (b.creative_and_content_deliverables || []).length;
    ctx.check({ rule: "deliverables.itemised", status: nd >= 3 ? "pass" : "warn", severity: "medium", message: `${nd} itemised deliverables.` });
    const banned = (ctx.brand.banned_phrases || []).filter((p) => JSON.stringify(b).toLowerCase().includes(p.toLowerCase()));
    if (banned.length) ctx.check({ rule: "brand.banned_phrases", status: "fail", severity: "high", message: `Banned phrases: ${banned.join(", ")}` });

    const titles = Object.fromEntries(contract.sections.map((s) => [s.key, s.title]));
    const L = [`# ${b.campaign_name}`, "", `## 1. ${titles.campaign_overview}`, b.campaign_overview, "", `## 2. ${titles.objectives_and_goals}`, "| Objective | KPI | Target |", "|---|---|---|",
      ...b.objectives_and_goals.map((o) => `| ${o.objective} | ${o.kpi} | ${o.target} |`), "", `## 3. ${titles.target_audience}`, `**Primary:** ${b.target_audience.primary}`];
    const a = b.target_audience;
    if ((a.secondary || []).length) L.push(`**Secondary:** ${a.secondary.join("; ")}`);
    if (a.insight) L.push(`**Insight:** ${a.insight}`);
    if ((a.personas || []).length) L.push(`**Personas:** ${a.personas.join("; ")}`);
    L.push("", `## 4. ${titles.key_messages}`, ...b.key_messages.map((m, i) => `${i + 1}. ${m}`), "", `## 5. ${titles.creative_and_content_deliverables}`,
      "| # | Deliverable | Type | Channel | Owner | Due (days) |", "|---|---|---|---|---|---|",
      ...b.creative_and_content_deliverables.map((d, i) => `| ${i + 1} | ${d.name} | ${d.type} | ${d.channel} | ${d.owner_role} | +${d.due_offset_days} |`),
      "", `## 6. ${titles.channels_and_media_plan}`, "| Channel | Role | Timing | Budget share |", "|---|---|---|---|",
      ...b.channels_and_media_plan.map((c) => `| ${c.channel} | ${c.role} | ${c.timing || ""} | ${Math.round(c.budget_share_pct)}% |`),
      "", `## 7. ${titles.measurement_and_reporting}`, `**KPIs:** ${b.measurement_and_reporting.kpis.join("; ")}`, `**Cadence:** ${b.measurement_and_reporting.cadence}`);
    if ((b.measurement_and_reporting.tools || []).length) L.push(`**Tools:** ${b.measurement_and_reporting.tools.join(", ")}`);
    if (b.measurement_and_reporting.utm_convention) L.push(`**UTM convention:** ${b.measurement_and_reporting.utm_convention}`);
    const markdown = L.join("\n") + "\n";
    ctx.artifact({ name: "brief.md", mime: "text/markdown", content: markdown, description: "Campaign brief" });
    ctx.artifact({ name: "brief.json", mime: "application/json", content: JSON.stringify(b, null, 2), description: "Campaign brief (structured)" });
    if (input.emit_tasks !== false) {
      const tasks = b.creative_and_content_deliverables.map((d) => ({ title: `${b.campaign_name}: ${d.name}`, type: d.type, channel: d.channel, owner_role: d.owner_role, due_offset_days: d.due_offset_days, description: d.description }));
      ctx.action({ id: "tasks-1", kind: "tasks.create", connector: "tasks", method: "create", payload: { tasks, project: contract.default_task_project }, summary: `Create ${tasks.length} deliverable tasks`, requires_approval: false });
    }
    return {
      output: { ...b, markdown, tasks_emitted: input.emit_tasks !== false ? nd : 0 },
      summary: `Brief '${b.campaign_name}': ${b.objectives_and_goals.length} objectives, ${nd} deliverables across ${b.channels_and_media_plan.length} channels`,
      view: [{ type: "markdown", title: "Brief", markdown }],
    };
  },
});
