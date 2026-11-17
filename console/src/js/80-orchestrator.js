/* Browser orchestrator: mirrors caf/orchestrator (route -> plan -> parallel dispatch -> approval gate -> synthesis). */
CAF.orch = (() => {
  const SUCCESS = new Set(["ok", "degraded", "needs_approval"]);
  const PRIMARY = ["overview", "topic", "question", "test_idea", "change_request", "source_text", "campaign_type"];
  const URLS = ["page_url", "url", "base_url", "start_url"];

  /* ---------------------------------------------------------------- bindings (port of bindings.py) */
  function lookup(expr, goal, inputs, results) {
    for (let opt of expr.split("|")) {
      opt = opt.trim(); let v;
      if (opt === "goal") v = goal;
      else if (opt.startsWith("input.")) v = U.get(inputs, opt.slice(6));
      else if (opt.startsWith("steps.")) { const parts = opt.split("."); const r = results[parts[1]]; v = r ? U.get(r, parts.slice(2).join(".")) : undefined; }
      else if (/^'.*'$/.test(opt)) v = opt.slice(1, -1);
      if (!U.empty(v)) return v;
    }
    return undefined;
  }
  function resolve(value, goal, inputs, results) {
    if (typeof value === "string") {
      if (value === "$goal") return goal;
      const whole = value.trim().match(/^\$\{([^}]+)\}$/);
      if (whole) return lookup(whole[1], goal, inputs, results);
      return value.replace(/\$\{([^}]+)\}/g, (_, e) => { const v = lookup(e, goal, inputs, results); return v === undefined || v === null ? "" : String(v); });
    }
    if (Array.isArray(value)) return value.map((v) => resolve(v, goal, inputs, results));
    if (U.isObj(value)) { if (Object.keys(value).length === 1 && value.$ref) return lookup(String(value.$ref), goal, inputs, results); const o = {}; for (const [k, v] of Object.entries(value)) o[k] = resolve(v, goal, inputs, results); return o; }
    return value;
  }
  const prune = (v) => (Array.isArray(v) ? v.filter((x) => x !== null && x !== undefined && x !== "").map(prune) : U.isObj(v) ? Object.fromEntries(Object.entries(v).filter(([, x]) => !U.empty(x)).map(([k, x]) => [k, prune(x)])) : v);
  function stepInputs(desc, raw, goal, inputs, results) {
    const props = (desc.input_schema || {}).properties || {};
    const resolved = prune(resolve(U.clone(raw || {}), goal, inputs, results)) || {};
    const out = Object.fromEntries(Object.entries(resolved).filter(([k]) => k in props));
    for (const k of Object.keys(props)) if (!(k in out) && !U.empty(inputs[k])) out[k] = inputs[k];
    const sharedUrl = inputs.url || inputs.page_url;
    if (sharedUrl) { for (const k of URLS) if (k in props && !(k in out)) out[k] = sharedUrl; if ("urls" in props && !("urls" in out)) out.urls = [sharedUrl]; }
    if (goal) {
      const declared = desc.primary_inputs;
      if (Array.isArray(declared)) { for (const k of declared) if (k in props && !(k in out)) out[k] = goal; }
      else for (const k of PRIMARY) { const s = props[k]; if (s && !(k in out)) { const t = JSON.stringify(s); if (t.includes('"string"') && !t.includes('"array"')) { out[k] = goal; break; } } }
    }
    return out;
  }

  /* ---------------------------------------------------------------- deterministic router (port of router.py) */
  function agentDoc(a) { return [a.name, a.summary, (a.keywords || []).join(" ").repeat(2), (a.use_cases || []).join(" ")].join(" "); }
  function bookDoc(p) { return [p.name, p.description, (p.triggers || []).join(" ").repeat(3)].join(" "); }
  function score(goal) {
    const g = goal.toLowerCase(), toks = new Set(CAF.lib.text.tokens(goal));
    const agents = CAF.order.map((id) => CAF.catalog[id]);
    const books = Object.values(CAF.playbooks);
    const as = CAF.lib.text.similarity(goal, agents.map(agentDoc));
    const ps = CAF.lib.text.similarity(goal, books.map(bookDoc));
    const ra = agents.map((a, i) => [as[i] + 0.25 * (a.keywords || []).filter((k) => g.includes(k.toLowerCase())).length + (g.includes(a.name.toLowerCase()) || g.includes(a.id.replace(/_/g, " ")) ? 0.6 : 0), a.id]).sort((x, y) => y[0] - x[0]);
    const multi = toks.size > 6 && [" and ", " then ", ",", "end-to-end", "full", "kit", "programme", "program"].some((w) => g.includes(w)) ? 0.15 : 0;
    const rp = books.map((p, i) => [ps[i] + 0.3 * (p.triggers || []).filter((t) => g.includes(t.toLowerCase())).length + multi, p.id]).sort((x, y) => y[0] - x[0]);
    return { agents: ra, books: rp };
  }
  function route(goal) {
    const { agents, books } = score(goal);
    const [ba, bp] = [agents[0] || [0, null], books[0] || [0, null]];
    const alternatives = [...agents.slice(0, 3).map(([s, a]) => ({ agent: a, score: +s.toFixed(3) })), ...books.slice(0, 2).map(([s, p]) => ({ playbook: p, score: +s.toFixed(3) }))];
    if (bp[1] && bp[0] >= Math.max(0.35, ba[0] * 1.05)) return { mode: "playbook", playbook: bp[1], confidence: +Math.min(bp[0], 1).toFixed(3), reason: `Goal matches the '${CAF.playbooks[bp[1]].name}' playbook`, method: "router", alternatives };
    return { mode: "agent", agent: ba[1], confidence: +Math.min(ba[0], 1).toFixed(3), reason: `Goal matches the ${CAF.catalog[ba[1]].name} agent`, method: "router", alternatives };
  }
  function planFromPlaybook(id) { const p = CAF.playbooks[id]; return { mode: "playbook", playbook: id, reason: p.description, steps: U.clone(p.steps).map((s) => ({ purpose: "", inputs: {}, needs: [], ...s })) }; }
  function planForAgent(id) { const a = CAF.catalog[id]; return { mode: "agent", reason: `Single agent: ${a.name}`, steps: [{ id, agent: id, purpose: a.summary, inputs: {}, needs: [] }] }; }
  function validatePlan(plan) {
    const warnings = [], seen = new Set(), steps = [];
    for (const raw of (plan.steps || []).slice(0, 12)) {
      const desc = CAF.catalog[raw.agent];
      if (!desc) { warnings.push(`Dropped step '${raw.id}': unknown agent '${raw.agent}'`); continue; }
      let sid = String(raw.id || raw.agent).toLowerCase().replace(/[^a-z0-9_]+/g, "_").replace(/^_+|_+$/g, "") || raw.agent; const base = sid; let n = 2;
      while (seen.has(sid)) sid = `${base}_${n++}`;
      const props = (desc.input_schema || {}).properties || {};
      const unknown = Object.keys(raw.inputs || {}).filter((k) => !(k in props));
      if (unknown.length) warnings.push(`Step '${sid}': ignored unknown inputs ${unknown.join(", ")}`);
      const needs = (raw.needs || []).filter((d) => seen.has(d));
      steps.push({ id: sid, agent: raw.agent, purpose: raw.purpose || "", inputs: Object.fromEntries(Object.entries(raw.inputs || {}).filter(([k]) => k in props)), needs });
      seen.add(sid);
    }
    if (!steps.length) throw new Error("The plan has no valid steps");
    return { plan: { ...plan, steps }, warnings };
  }
  async function makePlan(goal, { agent, playbook, inputs = {}, usePlanner = true } = {}) {
    if (playbook) { const { plan, warnings } = validatePlan(planFromPlaybook(playbook)); return { route: { mode: "playbook", playbook, method: "explicit", confidence: 1, reason: "Playbook chosen" }, plan, warnings }; }
    if (agent) return { route: { mode: "agent", agent, method: "explicit", confidence: 1, reason: "Agent chosen" }, plan: planForAgent(agent), warnings: [] };
    const warnings = [];
    if (usePlanner && CAF.llm.live()) {
      try {
        const { system, user } = CAF.prompts.render("orchestrator", "plan", { goal, agents: CAF.planner.agents, playbooks: CAF.planner.playbooks, shared_inputs: Object.keys(inputs).filter((k) => !U.empty(inputs[k])) });
        let proposed = await CAF.llm.json(system, user, { schema: CAF.schemas.plan, tier: CAF.routeTier({ id: "orchestrator", tier: "balanced" }, "plan") });
        if (proposed.mode === "playbook" && CAF.playbooks[proposed.playbook] && !(proposed.steps || []).length) proposed = planFromPlaybook(proposed.playbook);
        const { plan, warnings: w } = validatePlan(proposed);
        return { route: { mode: plan.mode, playbook: plan.playbook, agent: plan.mode === "agent" ? plan.steps[0].agent : null, method: "planner", confidence: 0.9, reason: plan.reason }, plan, warnings: w };
      } catch (e) { warnings.push(`Planner unavailable, used the deterministic router: ${e.message}`); }
    }
    const r = route(goal);
    const floor = (CAF.orchestratorSettings && CAF.orchestratorSettings.min_route_confidence) || 0.05;
    if (r.confidence < floor) warnings.push(`Low-confidence match (${r.confidence.toFixed(2)}): describe what you want produced, or pick a playbook. Without Claude the Python orchestrator would ask for a clearer goal instead of running this.`);
    if (r.mode === "playbook") { const { plan, warnings: w } = validatePlan(planFromPlaybook(r.playbook)); return { route: r, plan, warnings: warnings.concat(w) }; }
    return { route: r, plan: planForAgent(r.agent), warnings };
  }

  /* ---------------------------------------------------------------- execution */
  function depthOf(plan) { const d = {}; for (const s of plan.steps) d[s.id] = s.needs.length ? Math.max(...s.needs.map((n) => (d[n] ?? 0) + 1)) : 0; return d; }
  async function run(run, { onUpdate = () => {} } = {}) {
    const { plan, goal, inputs } = run;
    run.status = "running"; run.results = run.results || {}; run.events.push({ t: Date.now(), kind: "run.start", text: `Running ${plan.steps.length} step(s)` }); onUpdate(run);
    const ctl = (run.controller = new AbortController());
    const pending = new Set(plan.steps.map((s) => s.id).filter((id) => !run.results[id]));
    const running = new Map();
    const maxParallel = 4;
    while (pending.size || running.size) {
      for (const s of plan.steps) { // skip steps whose dependencies failed
        if (!pending.has(s.id)) continue;
        const bad = s.needs.filter((d) => run.results[d] && ["error", "skipped"].includes(run.results[d].status));
        if (bad.length) { pending.delete(s.id); run.results[s.id] = { agent_id: s.agent, status: "skipped", summary: `Skipped: ${bad.join(", ")} did not complete`, output: {}, checks: [], artifacts: [], pending_actions: [], metrics: {} }; run.events.push({ t: Date.now(), kind: "step.skipped", text: `${s.id} skipped` }); onUpdate(run); }
      }
      const ready = plan.steps.filter((s) => pending.has(s.id) && s.needs.every((d) => run.results[d] && SUCCESS.has(run.results[d].status)));
      for (const s of ready.slice(0, Math.max(0, maxParallel - running.size))) {
        pending.delete(s.id);
        const desc = CAF.catalog[s.agent];
        const resultsView = Object.fromEntries(Object.entries(run.results).map(([k, v]) => [k, { agent_id: v.agent_id, status: v.status, output: v.output }]));
        let inp;
        try { inp = stepInputs(desc, s.inputs, goal, inputs, resultsView); }
        catch (e) { run.results[s.id] = { agent_id: s.agent, status: "error", error: `binding failed: ${e.message}`, summary: "Input binding failed", output: {}, checks: [], artifacts: [], pending_actions: [] };
          run.events.push({ t: Date.now(), kind: "step.done", text: `${s.id}: error - input binding failed` }); onUpdate(run); continue; }
        const upstream = {};
        for (const [k, v] of Object.entries(run.results)) if (!s.needs.includes(k) && SUCCESS.has(v.status)) upstream[k] = { agent_id: v.agent_id, output: v.output };
        for (const k of s.needs) { const v = run.results[k]; if (v && SUCCESS.has(v.status)) upstream[k] = { agent_id: v.agent_id, output: v.output }; }
        run.stepState[s.id] = { status: "running", started: Date.now(), inputs: inp };
        run.events.push({ t: Date.now(), kind: "step.start", text: `${s.id} → ${desc.name}` }); onUpdate(run);
        const p = CAF.runAgent(s.agent, inp, { upstream, signal: ctl.signal, runId: run.id, stepId: s.id, autoApprove: run.autoApprove })
          .then((res) => { res.step_inputs = inp; run.results[s.id] = res; run.stepState[s.id] = { ...run.stepState[s.id], status: res.status, ended: Date.now() };
            run.events.push({ t: Date.now(), kind: "step.done", text: `${s.id}: ${res.status} - ${res.summary || res.error || ""}` }); onUpdate(run); })
          .finally(() => running.delete(s.id));
        running.set(s.id, p);
      }
      if (running.size) await Promise.race(running.values());
      else if (pending.size) { // nothing can run: remaining steps are blocked
        for (const id of pending) run.results[id] = { agent_id: plan.steps.find((s) => s.id === id).agent, status: "skipped", summary: "Skipped: dependencies did not complete", output: {}, checks: [], artifacts: [], pending_actions: [] };
        pending.clear();
      }
      if (ctl.signal.aborted) break;
    }
    if (ctl.signal.aborted) { // Stop: no further model calls, no synthesis
      await Promise.allSettled(running.values());
      for (const s of plan.steps) if (!run.results[s.id]) run.results[s.id] = { agent_id: s.agent, status: "skipped", summary: "Skipped: the run was stopped", output: {}, checks: [], artifacts: [], pending_actions: [] };
      run.status = "stopped";
      const done = plan.steps.filter((s) => SUCCESS.has(run.results[s.id].status));
      run.final = { headline: `Run stopped · ${done.length}/${plan.steps.length} steps completed`, method: "digest", next_steps: [], steps: [],
        summary_markdown: plan.steps.map((s) => `- **${CAF.catalog[s.agent].name}** (${run.results[s.id].status}): ${run.results[s.id].summary || run.results[s.id].error || ""}`).join("\n") };
      run.events.push({ t: Date.now(), kind: "run.done", text: "Run stopped" }); onUpdate(run);
      return run;
    }
    const approvals = collectApprovals(run);
    run.status = approvals.length ? "awaiting_approval" : "synthesizing"; onUpdate(run);
    if (!approvals.length) await synthesize(run, onUpdate);
    return run;
  }
  function collectApprovals(run) {
    const out = [];
    for (const [sid, r] of Object.entries(run.results)) for (const a of r.pending_actions || []) if (a.status === "pending") out.push({ key: `${sid}/${a.id}`, step: sid, agent_id: r.agent_id, action: a });
    return out;
  }
  async function decide(run, key, decision, onUpdate = () => {}) {
    const item = collectApprovals(run).find((x) => x.key === key);
    if (!item) return;
    const r = run.results[item.step];
    if (decision === "approve") CAF.performAction(item.agent_id, item.action, r._ctx || CAF.makeCtx(CAF.catalog[item.agent_id], {}, { runId: run.id }));
    else item.action.status = "rejected";
    run.events.push({ t: Date.now(), kind: "action", text: `${item.step}: ${item.action.kind} ${item.action.status}` });
    if (!(r.pending_actions || []).some((a) => a.status === "pending") && r.status === "needs_approval") r.status = (r.provenance || {}).degraded_reasons ? "degraded" : "ok";
    if (!collectApprovals(run).length) { run.status = "synthesizing"; onUpdate(run); await synthesize(run, onUpdate); }
    else onUpdate(run);
  }
  async function synthesize(run, onUpdate) {
    const rows = run.plan.steps.map((s) => { const r = run.results[s.id] || {}; const c = { pass: 0, warn: 0, fail: 0 }; (r.checks || []).forEach((x) => { if (x.status in c) c[x.status]++; });
      return { step: s.id, agent: s.agent, status: r.status || "not_run", summary: r.summary || r.error || "", checks: c, artifacts: (r.artifacts || []).length, error: r.error || null }; });
    const ok = rows.filter((x) => SUCCESS.has(x.status)).length, failed = rows.filter((x) => x.status === "error");
    run.final = { headline: `${ok}/${rows.length} steps completed${failed.length ? `, ${failed.length} failed` : ""}`, summary_markdown: rows.map((x) => `- **${CAF.catalog[x.agent].name}** (${x.status}): ${x.summary}`).join("\n"), next_steps: [], steps: rows, method: "digest" };
    if (CAF.llm.live() && rows.length > 1) {
      try {
        const { system, user } = CAF.prompts.render("orchestrator", "synthesize", { goal: run.goal, steps: rows });
        const syn = await CAF.llm.json(system, user, { schema: CAF.schemas.synthesize, tier: CAF.routeTier({ id: "orchestrator", tier: "fast" }, "synthesize") });
        run.final = { ...run.final, ...syn, method: "model" };
      } catch (e) { run.final.synthesis_error = e.message; }
    }
    run.status = !rows.length || ok ? (failed.length ? "completed_with_errors" : "completed") : "failed";
    run.events.push({ t: Date.now(), kind: "run.done", text: run.final.headline }); onUpdate(run);
  }
  function newRun(goal, inputs, planned) {
    return { id: U.uid("orch"), goal, inputs, route: planned.route, plan: planned.plan, warnings: planned.warnings || [], status: "planned", results: {}, stepState: {}, events: [], final: null, created: Date.now(), autoApprove: false };
  }
  return { route, makePlan, validatePlan, planFromPlaybook, planForAgent, stepInputs, run, decide, collectApprovals, newRun, depthOf, SUCCESS };
})();
