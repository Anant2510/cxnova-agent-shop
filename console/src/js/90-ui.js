/* Console UI: registry rail, orchestrator board, agent runner, health and runs. */
const UI = (window.CAFUI = {
  tab: U.storage.get("caf.tab", "orchestrate"),
  selected: null,
  run: null,
  selectedStep: null,
  agentResults: {},
  healthMode: null,
  examples: [
    { label: "Launch kit for flood cover", goal: "Launch our new flood-ready home insurance cover: brief, launch email, LinkedIn outreach, press release, social posts, tagged links and QR codes for print.", inputs: { url: "https://www.meridian-mutual.example/products/home-insurance.html", audience: "Homeowners and independent brokers in flood-exposed US regions", campaign_type: "product launch" } },
    { label: "Audit the home-insurance page", goal: "Audit our home-insurance page end to end - technical SEO, AI-search readiness, accessibility, heatmap behaviour and traffic - then suggest experiments.", inputs: { url: "https://www.meridian-mutual.example/products/home-insurance.html" } },
    { label: "Experimentation loop for the hero CTA", goal: "Run the experimentation loop on the home-insurance hero CTA: ideas, plan, build the variation and review it before launch.", inputs: { url: "https://www.meridian-mutual.example/products/home-insurance.html", test_idea: "Change the hero button text \"Get a quote\" to \"See my price in 2 minutes\" and make it more prominent" } },
    { label: "Competitive pitch vs Harborline", goal: "Build a competitive pitch against Harborline Insurance and Crestview Assurance for the insurance vertical.", inputs: { competitors: "Harborline Insurance, Crestview Assurance", industry: "insurance" } },
    { label: "Webinar programme", goal: "Run the follow-up programme for our climate-ready home webinar: recap, quotes, social posts and behaviour-based follow-up emails.", inputs: {} },
    { label: "Explain the August dip", goal: "Monthly insights: traffic report, explore conversion rate by channel, and narrate the weekly quotes trend.", inputs: { question: "conversion rate by channel" } },
  ],

  init() {
    UI.renderShell();
    UI.renderRegistry();
    UI.renderOrchestrate();
    UI.renderHealth();
    UI.renderRuns();
    const wanted = /[#&]agent=([a-z0-9_]+)/.exec(location.hash);  // the shop's "Try it in the console" links
    UI.selectAgent((wanted && CAF.catalog[wanted[1]] ? wanted[1] : null) || U.storage.get("caf.agent", "campaign_brief_generation"), false);
    if (wanted && CAF.catalog[wanted[1]]) setTimeout(() => UI.switchTab("agent"), 0);
    const flow = /[#&]playbook=([a-z0-9_]+)/.exec(location.hash);      // the storefront's flow pages
    if (flow && CAF.playbooks[flow[1]]) setTimeout(() => { U.$("#playbook").value = flow[1]; UI.switchTab("orchestrate"); UI.plan(true); }, 0);
    UI.switchTab(UI.tab);
    CAF.on((e) => {
      if (e.type === "llm") UI.renderLlmChip();
      if (e.type === "health") { UI.renderRegistry(); UI.renderHealth(); UI.renderLiveChip(); }
    });
    UI.renderLlmChip();
    UI.renderLiveChip();
  },

  /* ---------------------------------------------------------------- shell + chips */
  renderShell() {
    for (const t of ["orchestrate", "agent", "health", "runs"]) U.$(`#tab-${t}`).onclick = () => UI.switchTab(t);
    U.$("#mode-live").onclick = () => CAF.llm.setMode("live");
    U.$("#mode-offline").onclick = () => CAF.llm.setMode("offline");
    U.$("#agent-search").oninput = U.debounce(() => UI.renderRegistry(), 80);
    U.$("#rail-toggle").onclick = () => { const r = U.$("#rail"); r.classList.toggle("open"); U.$("#rail-toggle").textContent = r.classList.contains("open") ? "Hide agents" : "Show agents"; };
  },
  switchTab(t) {
    UI.tab = t; U.storage.set("caf.tab", t);
    for (const x of ["orchestrate", "agent", "health", "runs"]) { U.$(`#tab-${x}`).setAttribute("aria-selected", String(x === t)); U.$(`#view-${x}`).hidden = x !== t; }
    if (t === "orchestrate") requestAnimationFrame(UI.drawWires);
  },
  renderLlmChip() {
    const lamp = U.$("#lamp-llm"), label = U.$("#llm-label");
    const st = CAF.llm.state, live = CAF.llm.live();
    lamp.className = "lamp " + (live ? "live" : st === "checking" ? "running" : st === "declined" ? "error" : "skipped");
    const nav = document.getElementById("shop-nav");
    if (nav) nav.classList.toggle("hidden", !(CAF.hosted || CAF.llm.host));  // the shop pages exist only when the framework serves the console
    const needsToken = CAF.hosted && !live && CAF.llm.mode !== "offline" && (st === "not_granted" || st === "unavailable");
    label.textContent = live ? `${CAF.llm.host ? CAF.llm.host.label : "Claude"} connected${CAF.llm.quick ? " · quick tier" : ""}` : CAF.llm.mode === "offline" ? "Offline model" : st === "checking" ? "Connecting to Claude"
      : st === "server_offline" ? `Server model offline (${(CAF.llm.server && CAF.llm.server.provider) || "no provider"}) · offline model` : needsToken ? "Not connected · click to enter the operator token" : st === "declined" ? "Claude not allowed · offline" : "Claude unavailable · offline";
    const chip = label.parentElement;
    if (chip) { chip.style.cursor = needsToken ? "pointer" : ""; chip.title = needsToken ? "The hosted model needs this server's operator token (CAF_API_TOKEN)" : ""; chip.onclick = needsToken ? () => CAF.llm.connect() : null; }
    U.$("#mode-live").setAttribute("aria-pressed", String(CAF.llm.mode === "live"));
    U.$("#mode-offline").setAttribute("aria-pressed", String(CAF.llm.mode === "offline"));
  },
  renderLiveChip() {
    const ids = CAF.localIds(); const h = CAF.health;   // the probe covers the agents that run in this page; pack agents run on the server
    const live = ids.filter((id) => h[id] && h[id].live).length;
    const checked = ids.filter((id) => h[id]).length;
    U.$("#live-count").textContent = `${live}/${ids.length}`;
    U.$("#lamp-live").className = "lamp " + (checked < ids.length ? "running" : live === ids.length ? "live" : "degraded");
  },
  toast(msg) { const t = U.el("div", { class: "toast", text: msg }); U.$("#toasts").append(t); setTimeout(() => t.remove(), 3800); },

  /* ---------------------------------------------------------------- registry rail */
  agentStatus(id) {
    const last = UI.agentResults[id];
    if (last && last.status) return last.status === "error" ? "error" : last.status === "ok" ? "live" : last.status;
    const h = CAF.health[id];
    if (!h && CAF.remote[id]) return "live";   // registered on the server (its own health check proves it); not probed from the page
    if (!h) return "idle";
    if (h.running) return "running";
    if (!h.live) return "error";
    return h.status === "degraded" && !h.offlineOnly ? "degraded" : "live";
  },
  renderRegistry() {
    const q = (U.$("#agent-search").value || "").toLowerCase().trim();
    const root = U.$("#cats"); root.innerHTML = "";
    for (const cat of CAF.categories) {
      const ids = CAF.localIds().filter((id) => CAF.catalog[id].category === cat.id);   // pack agents have their own groups below
      const shown = ids.filter((id) => !q || `${CAF.catalog[id].name} ${CAF.catalog[id].summary} ${(CAF.catalog[id].keywords || []).join(" ")}`.toLowerCase().includes(q));
      if (!shown.length) continue;
      const box = U.el("div", { class: "cat" }, U.el("div", { class: "cat-title" }, U.el("i", { class: `cat-${cat.id}` }), cat.label, U.el("span", { class: "count", text: `${ids.filter((id) => CAF.health[id] && CAF.health[id].live).length}/${ids.length}` })));
      for (const id of shown) {
        const a = CAF.catalog[id];
        const row = U.el("button", { class: "agent-row", "aria-current": String(UI.selected === id), title: a.summary, onclick: () => { UI.selectAgent(id); UI.switchTab("agent"); } },
          U.el("span", { class: `lamp ${UI.agentStatus(id)}` }), U.el("span", { class: "name", text: a.name }), U.el("span", { class: `kind ${a.kind}`, text: a.kind === "deterministic" ? "CODE" : a.kind.toUpperCase() }));
        box.append(row);
      }
      root.append(box);
    }
    // the agent packs (other CX Nova products' agents): one folded group per pack, by accelerator
    for (const pack of CAF.packs) {
      const ids = CAF.order.filter((id) => CAF.catalog[id].pack === pack.id);
      const shown = ids.filter((id) => !q || `${CAF.catalog[id].name} ${CAF.catalog[id].summary} ${CAF.catalog[id].accelerator || ""}`.toLowerCase().includes(q));
      if (!shown.length) continue;
      const open = !!q || shown.includes(UI.selected) || U.storage.get(`caf.pack.${pack.id}`, false);
      const box = U.el("details", { class: "cat pack" }, U.el("summary", { class: "cat-title" }, U.el("i", { class: "cat-pack" }), pack.name, U.el("span", { class: "count", text: `${ids.length}` })));
      if (open) box.setAttribute("open", "");
      box.addEventListener("toggle", () => U.storage.set(`caf.pack.${pack.id}`, box.open));
      let group = null;
      for (const id of shown) {
        const a = CAF.catalog[id];
        if ((a.accelerator || "") !== group) { group = a.accelerator || ""; if (group) box.append(U.el("div", { class: "pack-group", text: group })); }
        box.append(U.el("button", { class: "agent-row", "aria-current": String(UI.selected === id), title: a.summary, onclick: () => { UI.selectAgent(id); UI.switchTab("agent"); } },
          U.el("span", { class: `lamp ${UI.agentStatus(id)}` }), U.el("span", { class: "name", text: a.name }), U.el("span", { class: "kind llm", text: "PACK" })));
      }
      root.append(box);
    }
  },

  /* ---------------------------------------------------------------- orchestrate */
  renderOrchestrate() {
    const v = U.$("#view-orchestrate"); v.innerHTML = "";
    const ex = UI.examples[0];
    const goal = U.el("textarea", { id: "goal", "aria-label": "Goal" }); goal.value = U.storage.get("caf.goal", ex.goal);
    const sel = U.el("select", { id: "playbook" }, U.el("option", { value: "", text: "Auto: let the orchestrator plan" }), ...Object.values(CAF.playbooks).map((p) => U.el("option", { value: p.id, text: `Playbook: ${p.name}` })));
    const shared = [["url", "Page URL", "url"], ["audience", "Audience", "text"], ["industry", "Industry", "text"], ["competitors", "Competitors (comma-separated)", "text"], ["campaign_type", "Campaign type", "text"], ["test_idea", "Test idea", "text"]];
    const savedInputs = U.storage.get("caf.inputs", ex.inputs);
    const inputs = U.el("div", { class: "form-grid" }, ...shared.map(([k, label, type]) => { const i = U.el("input", { id: `in-${k}`, type, placeholder: label }); i.value = savedInputs[k] || ""; return U.el("label", { class: "field" }, U.el("span", { text: label }), i); }));
    const examples = U.el("div", { class: "examples" }, ...UI.examples.map((e) => U.el("button", { type: "button", onclick: () => { goal.value = e.goal; for (const [k] of shared) U.$(`#in-${k}`).value = e.inputs[k] || ""; sel.value = ""; UI.plan(); } }, e.label)));
    const quick = U.el("input", { type: "checkbox", id: "quick" }); quick.checked = CAF.llm.quick; quick.onchange = () => CAF.llm.setQuick(quick.checked);
    const auto = U.el("input", { type: "checkbox", id: "auto-approve" });
    v.append(U.el("div", { class: "panel goal-box stack" },
      U.el("div", {}, U.el("div", { class: "eyebrow", text: "Orchestrator" }), U.el("h2", { text: "Describe the outcome; the orchestrator plans and drives the agents" }),
        U.el("p", { class: "lede", text: "Plans come from Claude when connected, or from the deterministic router over the agent catalog and playbooks. Independent steps run in parallel, outputs flow downstream, and publish-type actions wait for your approval." })),
      U.el("label", { class: "field" }, U.el("span", { text: "Goal" }), goal), examples,
      U.el("details", {}, U.el("summary", { class: "small muted", text: "Shared inputs (page URL, audience, industry, competitors ...)" }), U.el("div", { class: "stack", style: "margin-top:10px" }, inputs)),
      U.el("div", { class: "row" }, sel, U.el("button", { class: "btn", id: "btn-plan", onclick: () => UI.plan() }, "Plan"), U.el("button", { class: "btn primary", id: "btn-run", onclick: () => UI.runPlan() }, "Run plan"),
        CAF.hosted ? U.el("button", { class: "btn", id: "btn-save-flow", title: "Open this plan in the designer: change the steps on the canvas, publish it, rent it out", onclick: () => UI.saveAsFlow() }, "Save plan as flow") : null,
        CAF.hosted ? U.el("a", { class: "btn ghost", id: "btn-compose", href: UI.shopBase() + "/shop/designer", target: "_blank", rel: "noopener", title: "Compose a flow on the canvas: agents as steps, lines between them" }, "Open the designer ↗") : null,
        U.el("button", { class: "btn ghost", id: "btn-stop", hidden: true, onclick: () => UI.run && UI.run.controller && UI.run.controller.abort() }, "Stop"),
        U.el("label", { class: "check-inline", title: "Every model step uses the quick tier: about a second per call instead of tens of seconds" }, quick, "Fast mode"),
        U.el("label", { class: "check-inline", title: "Execute publish actions without waiting (mandatory gates still wait)" }, auto, "Auto-approve")),
      U.el("div", { class: "hint", id: "plan-meta" })));
    v.append(U.el("div", { class: "panel stack", id: "plan-panel" }));
    v.append(U.el("div", { id: "approval-panel" }));
    v.append(U.el("div", { class: "grid-2", id: "run-detail" }, U.el("div", { class: "panel stack", id: "step-panel" }), U.el("div", { class: "stack" }, U.el("div", { class: "panel stack", id: "final-panel" }), U.el("div", { class: "panel stack", id: "timeline-panel" }))));
    UI.plan(true);
    window.addEventListener("resize", U.debounce(UI.drawWires, 100));
  },
  readShared() {
    const out = {};
    for (const k of ["url", "audience", "industry", "competitors", "campaign_type", "test_idea"]) {
      const v = (U.$(`#in-${k}`).value || "").trim(); if (!v) continue;
      out[k] = k === "competitors" ? v.split(",").map((s) => s.trim()).filter(Boolean) : v;
    }
    return out;
  },
  shopBase() { return (window.claude && typeof window.claude.base === "string") ? window.claude.base : location.pathname.replace(/\/console\/?$/, ""); },
  saveAsFlow() {
    // hand the current plan to the shop's flow builder (admin → Flows) as a draft, in the URL hash: nothing is saved until the operator does
    if (!UI.run || !UI.run.plan || !UI.run.plan.steps.length) { UI.toast("Plan first, then save the plan as a flow."); return; }
    const goal = (U.$("#goal").value || "").trim();
    const shared = UI.readShared();
    const draft = { name: UI.run.plan.mode === "playbook" && CAF.playbooks[UI.run.plan.playbook] ? CAF.playbooks[UI.run.plan.playbook].name + " (variant)" : "", description: goal,
      inputs: Object.keys(shared).filter((k) => shared[k] && (!Array.isArray(shared[k]) || shared[k].length)).map((k) => ({ name: k, description: "", required: k === "url", example: Array.isArray(shared[k]) ? shared[k].join(", ") : String(shared[k]) })),
      steps: UI.run.plan.steps.map((s) => ({ id: s.id, agent: s.agent, purpose: s.purpose || "", inputs: s.inputs || {}, needs: s.needs || [] })) };
    const packed = btoa(unescape(encodeURIComponent(JSON.stringify(draft)))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
    window.open(UI.shopBase() + "/shop/designer#draft=" + packed, "_blank", "noopener");
  },
  async plan(silent = false) {
    const goal = U.$("#goal").value.trim(); if (!goal) return;
    U.storage.set("caf.goal", goal);
    const shared = UI.readShared(); U.storage.set("caf.inputs", { ...shared, competitors: (shared.competitors || []).join(", ") });
    const playbook = U.$("#playbook").value || null;
    U.$("#plan-meta").textContent = CAF.llm.live() && !playbook && !silent ? "Planning with Claude..." : "Planning...";
    let planned;
    try { planned = await CAF.orch.makePlan(goal, { playbook, inputs: shared, usePlanner: !silent }); }
    catch (e) { U.$("#plan-meta").textContent = ""; UI.toast(e.message); return; }
    UI.run = CAF.orch.newRun(goal, shared, planned);
    UI.selectedStep = null;
    UI.renderRun();
  },
  async runPlan() {
    if (!UI.run || ["running", "awaiting_approval"].includes(UI.run.status)) { if (!UI.run) await UI.plan(); if (!UI.run || UI.run.status === "running") return; }
    if (UI.run.status !== "planned") { await UI.plan(true); }
    UI.run.autoApprove = U.$("#auto-approve").checked;
    CAF.runs.unshift(UI.run);
    U.$("#btn-stop").hidden = false; U.$("#btn-run").disabled = true;
    try { await CAF.orch.run(UI.run, { onUpdate: () => UI.renderRun() }); }
    catch (e) { UI.toast(e.message); }
    finally { U.$("#btn-stop").hidden = true; U.$("#btn-run").disabled = false; UI.renderRun(); UI.renderRuns(); for (const [sid, r] of Object.entries(UI.run.results)) if (r.agent_id) UI.agentResults[r.agent_id] = r; UI.renderRegistry(); }
  },
  renderRun() {
    const run = UI.run; if (!run) return;
    // keep the registry lamps in step with the run (including statuses changed by approvals)
    for (const r of Object.values(run.results || {})) if (r && r.agent_id && r.status !== "skipped") UI.agentResults[r.agent_id] = r;
    UI.renderRegistry();
    const r = run.route || {};
    U.$("#plan-meta").textContent = `${run.plan.mode === "agent" ? "Single agent" : run.plan.mode === "playbook" ? `Playbook: ${CAF.playbooks[run.plan.playbook]?.name || run.plan.playbook}` : "Custom plan"} · via ${r.method}${r.confidence ? ` · confidence ${r.confidence}` : ""}`;
    const pp = U.$("#plan-panel"); pp.innerHTML = "";
    const depth = CAF.orch.depthOf(run.plan); const cols = [];
    for (const s of run.plan.steps) (cols[depth[s.id]] = cols[depth[s.id]] || []).push(s);
    pp.append(U.el("div", { class: "result-head" }, U.el("div", {}, U.el("div", { class: "eyebrow", text: "Plan" }), U.el("h3", { text: run.route.reason || run.plan.reason || "Plan" })),
      U.el("span", { class: `pill ${run.status}`, text: run.status.replace(/_/g, " ") })));
    if (run.warnings && run.warnings.length) pp.append(U.el("div", { class: "warnbox", text: run.warnings.join(" · ") }));
    const board = U.el("div", { class: "board", id: "board" });
    const grid = U.el("div", { class: "board-cols" });
    cols.forEach((col, i) => {
      grid.append(U.el("div", { class: "board-col" }, U.el("div", { class: "eyebrow", text: i === 0 ? "Stage 1 · parallel" : `Stage ${i + 1}` }), ...col.map((s) => {
        const res = run.results[s.id]; const st = res ? res.status : (run.stepState[s.id] || {}).status || "pending"; const a = CAF.catalog[s.agent];
        return U.el("button", { class: `step ${st}`, "data-step": s.id, "aria-current": String(UI.selectedStep === s.id), onclick: () => { UI.selectedStep = s.id; UI.renderRun(); } },
          U.el("div", { class: "top" }, U.el("span", { class: `lamp ${st === "pending" ? "idle" : st === "ok" ? "live" : st}` }), U.el("span", { class: "id", text: s.id }), U.el("span", { class: "grow" }), U.el("span", { class: `cat-dot cat-${a.category}` })),
          U.el("div", { class: "agent", text: a.name }), U.el("div", { class: "sum", text: res ? (res.summary || res.error || "") : (s.purpose || a.summary) }));
      })));
    });
    board.append(grid);
    board.insertAdjacentHTML("beforeend", '<svg class="wires" id="wires" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"></svg>');
    pp.append(board);
    requestAnimationFrame(UI.drawWires);
    // approvals
    const ap = U.$("#approval-panel"); ap.innerHTML = "";
    const pending = CAF.orch.collectApprovals(run);
    if (pending.length) {
      ap.append(U.el("div", { class: "approval" }, U.el("div", { class: "row" }, U.el("strong", { text: `${pending.length} action(s) waiting for approval` }), U.el("span", { class: "grow" }),
        U.el("button", { class: "btn sm", onclick: async () => { for (const p of CAF.orch.collectApprovals(run)) await CAF.orch.decide(run, p.key, "approve", () => UI.renderRun()); UI.renderRun(); } }, "Approve all")),
        ...pending.map((p) => U.el("div", { class: "approval-item" }, U.el("div", { class: "grow" }, U.el("div", { class: "mono small", text: `${p.step} · ${p.action.kind}` }), U.el("div", { text: p.action.summary })),
          U.el("div", { class: "row" }, U.el("button", { class: "btn sm primary", onclick: () => CAF.orch.decide(run, p.key, "approve", () => UI.renderRun()) }, "Approve"), U.el("button", { class: "btn sm danger", onclick: () => CAF.orch.decide(run, p.key, "reject", () => UI.renderRun()) }, "Reject"))))));
    }
    // step detail
    const sp = U.$("#step-panel"); sp.innerHTML = "";
    const sid = UI.selectedStep || (run.plan.steps.find((s) => run.results[s.id]) || run.plan.steps[0]).id;
    const step = run.plan.steps.find((s) => s.id === sid);
    const res = run.results[sid];
    sp.append(U.el("div", {}, U.el("div", { class: "eyebrow", text: `Step ${sid}` }), U.el("h3", { text: CAF.catalog[step.agent].name })));
    if (res) UI.renderResult(res, sp, { compact: true });
    else sp.append(U.el("div", { class: "empty", text: run.status === "planned" ? "Run the plan to see this step's output." : "Waiting for upstream steps." }), U.el("details", { class: "json" }, U.el("summary", { text: "Step inputs (bindings)" }), U.el("pre", { class: "code", text: JSON.stringify(step.inputs, null, 2) })));
    // final
    const fp = U.$("#final-panel"); fp.innerHTML = "";
    fp.append(U.el("div", { class: "eyebrow", text: "Run summary" }));
    if (run.final) { fp.append(U.el("h3", { text: run.final.headline }), U.el("div", { class: "md", html: CAF.lib.md.html(run.final.summary_markdown) }));
      if ((run.final.next_steps || []).length) fp.append(U.el("div", { class: "section-title" }, U.el("h3", { text: "Next steps" })), U.el("ol", { class: "small" }, ...run.final.next_steps.map((n) => U.el("li", { text: n })))); }
    else fp.append(U.el("p", { class: "muted small", text: run.status === "planned" ? `${run.plan.steps.length} step(s) planned. Nothing has run yet.` : run.status === "awaiting_approval" ? "All steps finished. Approve or reject the held actions to complete the run." : "Running..." }));
    const tp = U.$("#timeline-panel"); tp.innerHTML = "";
    tp.append(U.el("div", { class: "eyebrow", text: "Trace" }), run.events.length ? U.el("div", { class: "timeline" }, ...run.events.slice(-60).map((e) => U.el("div", {}, U.el("time", { text: new Date(e.t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) }), U.el("span", { text: e.text })))) : U.el("p", { class: "muted small", text: "Events appear here as the orchestrator dispatches steps." }));
  },
  drawWires() {
    const board = U.$("#board"), svg = U.$("#wires"); if (!board || !svg || !UI.run) return;
    const br = board.getBoundingClientRect();
    svg.setAttribute("viewBox", `0 0 ${board.scrollWidth} ${board.scrollHeight}`);
    svg.style.width = board.scrollWidth + "px"; svg.style.height = board.scrollHeight + "px";
    let paths = "";
    for (const s of UI.run.plan.steps) for (const d of s.needs) {
      const a = board.querySelector(`[data-step="${d}"]`), b = board.querySelector(`[data-step="${s.id}"]`); if (!a || !b) continue;
      const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
      const x1 = ra.right - br.left + board.scrollLeft, y1 = ra.top + ra.height / 2 - br.top, x2 = rb.left - br.left + board.scrollLeft, y2 = rb.top + rb.height / 2 - br.top;
      const mx = (x1 + x2) / 2; const done = UI.run.results[d] && CAF.orch.SUCCESS.has(UI.run.results[d].status);
      const hot = (UI.run.stepState[s.id] || {}).status === "running";
      paths += `<path class="${hot ? "hot" : done ? "done" : ""}" d="M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}"/>`;
    }
    svg.innerHTML = paths;
  },

  /* ---------------------------------------------------------------- agent runner */
  selectAgent(id, rerender = true) {
    if (!CAF.catalog[id]) id = CAF.order[0];
    UI.selected = id; U.storage.set("caf.agent", id);
    if (rerender) UI.renderRegistry();
    const a = CAF.catalog[id]; const v = U.$("#view-agent"); v.innerHTML = "";
    const cat = CAF.categories.find((c) => c.id === a.category) || { id: a.category, label: [a.pack_name, a.accelerator, a.category_label].filter(Boolean).join(" · ") };
    const nodes = (a.graph && a.graph.nodes) || [];
    v.append(U.el("div", { class: "panel stack" },
      U.el("div", { class: "agent-hero" }, U.el("div", { class: "stack" }, U.el("div", { class: "row" }, U.el("span", { class: `cat-dot cat-${a.category}` }), U.el("span", { class: "eyebrow", text: cat.label })),
        U.el("h2", { text: a.name }), U.el("p", { class: "lede", text: a.summary })),
        U.el("div", { class: "tags" }, U.el("span", { class: `kind ${a.kind}`, text: a.kind === "deterministic" ? "CODE" : a.kind.toUpperCase() }), U.el("span", { class: "pill", text: `tier ${a.tier}` }), U.el("span", { class: `pill ${UI.agentStatus(id) === "live" ? "live" : ""}`, text: UI.agentStatus(id) }))),
      U.el("div", { class: "graphline", "aria-label": "LangGraph stages" }, ...nodes.flatMap((n, i) => (i ? [U.el("i"), U.el("span", { text: n })] : [U.el("span", { text: n })]))),
      U.el("div", { class: "grid-2" },
        U.el("div", { class: "stack" }, U.el("div", { class: "eyebrow", text: "Use cases" }), U.el("ul", { class: "small", style: "margin:0;padding-left:18px" }, ...(a.use_cases || []).map((u) => U.el("li", { text: u })))),
        U.el("div", { class: "stack" }, U.el("div", { class: "eyebrow", text: "How it runs independently" }), U.el("p", { class: "indep", style: "margin:0", text: a.independence }),
          U.el("div", { class: "hint", text: `Connectors: ${(a.connectors || []).join(", ") || "none"} · Skills: ${(a.skills || []).join(", ") || "none"}${a.vendor_requirement_removed ? ` · Removes the ${a.vendor_requirement_removed} requirement` : ""}` }))),
      U.el("details", {}, U.el("summary", { class: "small muted", text: "Optimizely Opal reference" }), U.el("p", { class: "small", text: a.opal_reference }))));
    const form = UI.form(a.input_schema, CAF.selftest(id));
    const out = U.el("div", { class: "panel stack", id: "agent-result" });
    const btn = U.el("button", { class: "btn primary", onclick: async () => {
      let values; try { values = form.read(); } catch (e) { UI.toast(e.message); return; }
      btn.disabled = true; btn.textContent = "Running..."; out.innerHTML = ""; out.append(U.el("div", { class: "row" }, U.el("span", { class: "lamp running" }), U.el("span", { class: "muted", id: "agent-progress", text: CAF.llm.live() && a.kind !== "deterministic" ? "Working with Claude..." : "Running..." })));
      const off = CAF.on((e) => { if (e.type === "log" && e.agent === id) { const p = U.$("#agent-progress"); if (p) p.textContent = e.msg; } });
      const res = await CAF.runAgent(id, values, {});
      off(); btn.disabled = false; btn.textContent = "Run agent";
      UI.agentResults[id] = res; CAF.runs.unshift({ id: res.run_id, single: true, agent: id, result: res, created: Date.now() });
      out.innerHTML = ""; UI.renderResult(res, out); UI.renderRegistry(); UI.renderRuns();
    } }, "Run agent");
    v.append(U.el("div", { class: "panel stack" }, U.el("div", { class: "row" }, U.el("h3", { class: "grow", text: "Inputs" }), U.el("button", { class: "btn sm ghost", onclick: () => form.fill(CAF.selftest(id)) }, "Load sample input")), form.el,
      U.el("div", { class: "row" }, btn, U.el("span", { class: "hint", text: a.kind === "deterministic" ? "Runs entirely in your browser - no model call." : CAF.llm.live() ? "Model steps run on Claude; deterministic steps run in your browser." : "Model steps use the offline model until Claude is connected." }))));
    v.append(out);
    if (UI.agentResults[id]) UI.renderResult(UI.agentResults[id], out); else out.append(U.el("div", { class: "empty", text: "Run the agent to see its output, checks and artifacts." }));
  },
  /** What served a result, for its meta line: the browser runtime reports a word, the server an object. */
  modelLabel(res) {
    const p = res.provenance || {}; const runner = (res.output || {}).runner;
    if (runner === "home") return ((p.home || {}).name || "home product");
    if (runner === "twin") return "none (sample twin)";
    const l = p.llm;
    if (!l) return "n/a";
    if (typeof l === "string") return l;
    return l.offline ? "offline" : [l.provider, l.model].filter(Boolean).join(" ") || "n/a";
  },
  form(schema, initial) {
    const root = schema || {}; const props = root.properties || {}; const required = new Set(root.required || []);
    const fields = {};
    const grid = U.el("div", { class: "form-grid" });
    const base = (s) => { s = CAF.schema.resolve(s, root); if (s.anyOf) { const nn = s.anyOf.map((o) => CAF.schema.resolve(o, root)).find((o) => o.type !== "null"); return { ...nn, description: s.description || nn.description, default: s.default, ui: s.ui || nn.ui }; } return s; };
    for (const [name, raw] of Object.entries(props)) {
      const s = base(raw); const id = `f-${name}`; let input, kind;
      const longText = s.ui === "textarea" || /html|text|content|notes|overview|transcript|body|source|markdown|csv|dataset/i.test(name);
      if (s.enum) { kind = "enum"; input = U.el("select", { id }, ...(required.has(name) ? [] : [U.el("option", { value: "", text: "(default)" })]), ...s.enum.map((e) => U.el("option", { value: e, text: e }))); }
      else if (s.type === "boolean") { kind = "bool"; input = U.el("input", { id, type: "checkbox" }); }
      else if (s.type === "integer" || s.type === "number") { kind = "num"; input = U.el("input", { id, type: "number", step: s.type === "integer" ? "1" : "any" }); }
      else if (s.type === "array" && CAF.schema.resolve(s.items || {}, root).type === "string") { kind = "lines"; input = U.el("textarea", { id, rows: 3, placeholder: "one per line" }); }
      else if (s.type === "array" || s.type === "object" || !s.type) { kind = "json"; input = U.el("textarea", { id, class: "code", rows: 4, placeholder: "JSON" }); }
      else { kind = "str"; input = longText ? U.el("textarea", { id, rows: /html|csv|dataset|transcript/i.test(name) ? 6 : 3, class: /html|csv/i.test(name) ? "code" : "" }) : U.el("input", { id, type: "text" }); }
      fields[name] = { input, kind, schema: s };
      const wide = kind === "json" || kind === "lines" || input.tagName === "TEXTAREA";
      const label = kind === "bool" ? U.el("label", { class: `check-inline ${wide ? "wide" : ""}` }, input, U.humanize(name)) :
        U.el("label", { class: `field ${wide ? "wide" : ""}` }, U.el("span", { html: `${U.esc(U.humanize(name))}${required.has(name) ? "<em>*</em>" : ""}` }), input, s.description ? U.el("span", { class: "hint", style: "font-weight:400", text: s.description }) : null);
      grid.append(label);
    }
    const fill = (vals) => {
      for (const [name, f] of Object.entries(fields)) {
        const v = vals && name in vals ? vals[name] : f.schema.default;
        if (f.kind === "bool") f.input.checked = !!v;
        else if (v === undefined || v === null) f.input.value = "";
        else if (f.kind === "lines") f.input.value = (Array.isArray(v) ? v : [v]).join("\n");
        else if (f.kind === "json") f.input.value = JSON.stringify(v, null, 2);
        else f.input.value = String(v);
      }
    };
    fill(initial);
    return { el: grid, fill, read() {
      const out = {};
      for (const [name, f] of Object.entries(fields)) {
        const raw = f.kind === "bool" ? f.input.checked : f.input.value;
        if (f.kind === "bool") { out[name] = raw; continue; }
        if (String(raw).trim() === "") { if (required.has(name)) throw new Error(`${U.humanize(name)} is required`); continue; }
        if (f.kind === "num") out[name] = Number(raw);
        else if (f.kind === "lines") out[name] = String(raw).split("\n").map((x) => x.trim()).filter(Boolean);
        else if (f.kind === "json") { try { out[name] = JSON.parse(raw); } catch { throw new Error(`${U.humanize(name)} must be valid JSON`); } }
        else out[name] = raw;
      }
      return out;
    } };
  },

  /* ---------------------------------------------------------------- results */
  renderResult(res, root, { compact = false } = {}) {
    const counts = { pass: 0, warn: 0, fail: 0, info: 0 }; (res.checks || []).forEach((c) => (counts[c.status] = (counts[c.status] || 0) + 1));
    root.append(U.el("div", { class: "result-head" }, U.el("div", { class: "stack", style: "gap:4px" }, U.el("div", { class: "row" }, U.el("span", { class: `pill ${res.status}`, text: res.status.replace(/_/g, " ") }), U.el("strong", { text: res.summary || "" }))),
      U.el("div", { class: "metrics" }, U.el("span", {}, U.el("b", { text: U.fmtMs((res.metrics || {}).duration_ms || 0) })), U.el("span", {}, "model calls ", U.el("b", { text: String((res.metrics || {}).llm_calls || 0) })),
        U.el("span", {}, "checks ", U.el("b", { text: `${counts.pass}✓ ${counts.warn}! ${counts.fail}✗` })), U.el("span", { text: `runtime: ${(res.provenance || {}).runtime || "python"} · model: ${UI.modelLabel(res)}` }))));
    if (res.error) root.append(U.el("div", { class: "errbox", text: res.error }));
    const reasons = (res.provenance || {}).degraded_reasons || [];
    if (reasons.length) root.append(U.el("div", { class: "warnbox", text: `Degraded: ${reasons.join(" · ")}` }));
    if ((res.warnings || []).length) root.append(U.el("div", { class: "warnbox", text: res.warnings.join(" · ") }));
    if ((res.pending_actions || []).length) {
      root.append(U.el("div", { class: "section-title" }, U.el("h3", { text: "Actions" }), U.el("span", { class: "hint", text: "publish-type side effects are held for approval" })));
      for (const a of res.pending_actions) {
        const row = U.el("div", { class: "artifact" }, U.el("span", { class: `pill ${a.status === "done" ? "ok" : a.status === "pending" ? "needs_approval" : a.status === "rejected" ? "" : "error"}`, text: a.status }), U.el("span", { class: "grow", text: `${a.kind}: ${a.summary}` }));
        if (a.status === "pending") row.append(U.el("button", { class: "btn sm primary", onclick: () => { CAF.performAction(res.agent_id, a, res._ctx); if (!res.pending_actions.some((x) => x.status === "pending") && res.status === "needs_approval") res.status = (res.provenance || {}).degraded_reasons ? "degraded" : "ok"; root.innerHTML = ""; UI.renderResult(res, root, { compact }); } }, "Approve"),
          U.el("button", { class: "btn sm danger", onclick: () => { a.status = "rejected"; root.innerHTML = ""; UI.renderResult(res, root, { compact }); } }, "Reject"));
        root.append(row);
      }
    }
    for (const block of res.view || []) UI.renderBlock(block, res, root);
    if (!(res.view || []).length && res.output && Object.keys(res.output).length) UI.autoRender(res.output, root);
    if ((res.checks || []).length) {
      const order = { fail: 0, warn: 1, info: 2, pass: 3 };
      const rows = [...res.checks].sort((a, b) => order[a.status] - order[b.status]);
      root.append(U.el("details", { open: !compact && rows.some((c) => c.status !== "pass") ? true : undefined }, U.el("summary", { class: "small", text: `Checks (${rows.length})` }),
        U.el("div", { class: "table-wrap", style: "margin-top:8px" }, U.el("table", { class: "checks" }, U.el("thead", {}, U.el("tr", {}, ...["Status", "Rule", "Finding", "Ref"].map((h) => U.el("th", { text: h })))),
          U.el("tbody", {}, ...rows.slice(0, 300).map((c) => U.el("tr", {}, U.el("td", {}, U.el("span", { class: `pill ${c.status}`, text: c.status })), U.el("td", { class: "mono small", text: c.rule }), U.el("td", { text: `${c.message}${c.target ? ` (${c.target})` : ""}` }), U.el("td", { class: "small muted", text: c.ref || "" }))))))));
    }
    if ((res.artifacts || []).length) {
      const list = U.el("div", { class: "artifacts" });
      for (const a of res.artifacts) list.append(UI.artifactRow(a));
      root.append(U.el("details", { open: !compact ? true : undefined }, U.el("summary", { class: "small", text: `Artifacts (${res.artifacts.length})` }), U.el("div", { style: "margin-top:8px" }, list)));
    }
    root.append(U.el("details", { class: "json" }, U.el("summary", { text: "Output contract (JSON)" }), U.el("pre", { class: "code", text: JSON.stringify(res.output, null, 2).slice(0, 60000) })));
  },
  artifactRow(a) {
    const isText = typeof a.content === "string";
    const preview = U.el("div", { hidden: true });
    const row = U.el("div", { class: "stack", style: "gap:6px" });
    const ext = (a.name.split(".").pop() || "").toLowerCase();
    const btns = U.el("div", { class: "row" });
    btns.append(U.el("button", { class: "btn sm", onclick: () => { preview.hidden = !preview.hidden; if (!preview.hidden && !preview.childNodes.length) preview.append(UI.previewOf(a)); } }, "Preview"));
    if (CAF.downloads.available()) btns.append(U.el("button", { class: "btn sm", onclick: async () => { try { const r = await CAF.downloads.save(a.name, a.content); if (r && r.status === "saved") UI.toast(`Saved ${a.name}`); } catch (e) { UI.toast(e.message); } } }, "Save"));
    if (isText) btns.append(U.el("button", { class: "btn sm ghost", onclick: async () => { try { await navigator.clipboard.writeText(a.content); UI.toast("Copied"); } catch { UI.toast("Copy is blocked here - use Preview and select the text"); } } }, "Copy"));
    row.append(U.el("div", { class: "artifact" }, U.el("span", { class: "nm grow", text: a.name }), U.el("span", { class: "hint", text: `${a.mime} · ${U.fmtNum(a.size / 1024, 1)} KB` }), btns), preview);
    return row;
  },
  previewOf(a) {
    const c = a.content; const mime = a.mime || "";
    if (mime === "text/html") { const f = U.el("iframe", { sandbox: "", title: a.name }); f.srcdoc = c; return U.el("div", { class: "preview" }, f); }
    if (mime === "image/svg+xml" && typeof c === "string") return U.el("div", { class: "preview", style: "padding:10px" }, U.el("img", { alt: a.description || a.name, src: "data:image/svg+xml;base64," + btoa(unescape(encodeURIComponent(c))) }));
    if (mime.startsWith("image/") && c instanceof Blob) return U.el("div", { class: "preview", style: "padding:10px" }, U.el("img", { alt: a.description || a.name, src: URL.createObjectURL(c) }));
    if (mime === "text/csv" && typeof c === "string") { const rows = CAF.lib.csv.parse(c, { coerce: false }); return UI.table(rows.slice(0, 200)); }
    if (mime === "text/markdown") return U.el("div", { class: "md", html: CAF.lib.md.html(c) });
    return U.el("pre", { class: "code", text: typeof c === "string" ? c.slice(0, 80000) : `[binary ${a.mime}]` });
  },
  table(rows, cols) {
    if (!rows || !rows.length) return U.el("div", { class: "muted small", text: "No rows" });
    cols = cols || Object.keys(rows[0]).slice(0, 10);
    const fmt = (v) => (v === null || v === undefined ? "" : typeof v === "number" ? (Number.isInteger(v) ? U.fmtNum(v) : U.fmtNum(v, Math.abs(v) < 1 ? 4 : 2)) : typeof v === "object" ? JSON.stringify(v).slice(0, 160) : String(v));
    return U.el("div", { class: "table-wrap" }, U.el("table", { class: "data" }, U.el("thead", {}, U.el("tr", {}, ...cols.map((c) => U.el("th", { text: c })))),
      U.el("tbody", {}, ...rows.slice(0, 300).map((r) => U.el("tr", {}, ...cols.map((c) => U.el("td", { class: typeof r[c] === "number" ? "n" : "", text: fmt(r[c]) })))))));
  },
  renderBlock(b, res, root) {
    const title = b.title ? U.el("div", { class: "section-title" }, U.el("h3", { text: b.title }), b.note ? U.el("span", { class: "hint", text: b.note }) : null) : null;
    if (title) root.append(title);
    if (b.type === "markdown") root.append(U.el("div", { class: "md", html: CAF.lib.md.html(b.markdown || "") }));
    else if (b.type === "table") root.append(UI.table(b.rows || [], b.columns));
    else if (b.type === "kv") root.append(U.el("dl", { class: "kv" }, ...Object.entries(b.data || {}).flatMap(([k, v]) => [U.el("dt", { text: U.humanize(k) }), U.el("dd", { text: typeof v === "object" ? JSON.stringify(v) : String(v) })])));
    else if (b.type === "code") root.append(U.el("pre", { class: "code", text: b.code || "" }));
    else if (b.type === "svg") root.append(U.el("div", { class: "preview", style: "padding:8px;background:var(--surface)", html: b.svg || "" }));
    else if (b.type === "html") { const art = (res.artifacts || []).find((x) => x.name === b.artifact); const html = art ? art.content : b.html; const f = U.el("iframe", { sandbox: "", title: b.title || "preview" }); f.srcdoc = html || ""; root.append(U.el("div", { class: "preview" }, f)); }
    else if (b.type === "image") { const art = (res.artifacts || []).find((x) => x.name === b.artifact); if (art) root.append(UI.previewOf(art)); }
    else if (b.type === "list") root.append(U.el("ul", { class: "small", style: "margin:0;padding-left:18px" }, ...(b.items || []).map((i) => U.el("li", { text: i }))));
  },
  autoRender(output, root) {
    for (const key of ["markdown", "summary_markdown", "narrative_markdown", "body_markdown", "report_markdown"]) if (typeof output[key] === "string" && output[key].trim()) { root.append(U.el("div", { class: "md", html: CAF.lib.md.html(output[key]) })); return; }
    const arrays = Object.entries(output).filter(([, v]) => Array.isArray(v) && v.length && U.isObj(v[0]));
    for (const [k, v] of arrays.slice(0, 2)) { root.append(U.el("div", { class: "section-title" }, U.el("h3", { text: U.humanize(k) }), U.el("span", { class: "hint", text: `${v.length} item(s)` }))); const flat = v.map((o) => Object.fromEntries(Object.entries(o).filter(([, x]) => typeof x !== "object" || x === null))); root.append(UI.table(flat)); }
    const scalars = Object.fromEntries(Object.entries(output).filter(([, v]) => v !== null && typeof v !== "object").slice(0, 12));
    if (Object.keys(scalars).length) root.append(U.el("dl", { class: "kv" }, ...Object.entries(scalars).flatMap(([k, v]) => [U.el("dt", { text: U.humanize(k) }), U.el("dd", { text: String(v) })])));
  },

  /* ---------------------------------------------------------------- health */
  renderHealth() {
    const v = U.$("#view-health"); v.innerHTML = "";
    const ids = CAF.localIds(); const h = CAF.health;
    const live = ids.filter((id) => h[id] && h[id].live).length, degraded = ids.filter((id) => h[id] && h[id].live && h[id].status === "degraded" && !h[id].offlineOnly).length, down = ids.filter((id) => h[id] && !h[id].live && !h[id].running).length;
    const offlineOnly = ids.filter((id) => h[id] && h[id].offlineOnly).length;
    const last = U.storage.get("caf.liveProbe", null);
    v.append(U.el("div", { class: "panel stack" },
      U.el("div", { class: "health-summary" }, U.el("div", { class: "big-num num" }, String(live), U.el("small", { text: ` / ${ids.length} live` })),
        U.el("div", { class: "meter", role: "img", "aria-label": `${live} of ${ids.length} agents live` }, U.el("i", { style: `width:${((live - degraded) / ids.length) * 100}%;background:var(--live)` }), U.el("i", { style: `width:${(degraded / ids.length) * 100}%;background:var(--warn)` }), U.el("i", { style: `width:${(down / ids.length) * 100}%;background:var(--fail)` })),
        U.el("div", { class: "stack", style: "gap:2px" }, U.el("span", { class: "small", text: UI.healthMode === "live" ? "Last probe: live, on Claude (quick tier)" : "Last probe: offline model (wiring, contracts, deterministic stages)" }),
          U.el("span", { class: "hint", text: `${live - degraded} fully live${offlineOnly ? ` (${offlineOnly} with model steps on the offline stand-in)` : ""} · ${degraded} degraded · ${down} down` }),
          last ? U.el("span", { class: "hint", text: `Last live probe ${new Date(last.at).toLocaleString()}: ${last.live}/${last.total} live` }) : null)),
      U.el("p", { class: "lede", text: "A probe runs every agent's full pipeline from its sample input and validates the result against the agent's output contract. The offline probe proves wiring and deterministic stages without model cost; the live probe sends each model step to Claude." }),
      U.el("div", { class: "row" }, U.el("button", { class: "btn", onclick: () => UI.probe("offline") }, "Run offline probe"), U.el("button", { class: "btn primary", onclick: () => UI.probe("live"), disabled: CAF.llm.state !== "ready" ? true : undefined }, "Run live probe on Claude"),
        U.el("span", { class: "hint", text: CAF.llm.state === "ready" ? "The live probe makes about one model call per model step (around 30 quick calls in all)." : "Connect Claude to run the live probe." }))));
    const grid = U.el("div", { class: "health-grid" });
    for (const id of ids) {
      const a = CAF.catalog[id], r = h[id] || {};
      const st = r.running ? "running" : !r.status ? "idle" : !r.live ? "error" : r.status === "degraded" && !r.offlineOnly ? "degraded" : "live";
      const label = !r.status ? "not probed" : r.offlineOnly ? "live · offline model" : r.status === "needs_approval" ? "live · awaiting approval" : r.status;
      grid.append(U.el("button", { class: "hcard", onclick: () => { UI.selectAgent(id); if (r.result) { UI.agentResults[id] = r.result; UI.selectAgent(id); } UI.switchTab("agent"); } },
        U.el("div", { class: "t" }, U.el("span", { class: `lamp ${st}` }), U.el("b", { text: a.name })), U.el("div", { class: "s", text: r.error || r.summary || a.summary }),
        U.el("div", { class: "m" }, U.el("span", { text: label }), r.duration_ms !== undefined ? U.el("span", { text: U.fmtMs(r.duration_ms) }) : null, r.llm_calls ? U.el("span", { text: `${r.llm_calls} model calls` }) : null, U.el("span", { class: `kind ${a.kind}`, text: a.kind === "deterministic" ? "CODE" : a.kind.toUpperCase() }))));
    }
    v.append(grid);
  },
  async probe(mode = "offline") {
    UI.healthMode = mode;
    const ids = CAF.localIds(); const queue = [...ids]; const conc = mode === "live" ? 2 : 4;
    ids.forEach((id) => { CAF.health[id] = { ...(CAF.health[id] || {}), running: true }; });
    CAF.emit({ type: "health" });
    const work = async () => {
      while (queue.length) {
        const id = queue.shift();
        const t0 = performance.now();
        const res = await CAF.runAgent(id, CAF.selftest(id), { forceOffline: mode === "offline", quick: true, runId: `probe-${mode}` });
        const reasons = ((res.provenance || {}).degraded_reasons || []);
        const offlineOnly = res.status === "degraded" && reasons.length > 0 && reasons.every((r) => /^offline model/.test(r));
        CAF.health[id] = { live: ["ok", "degraded", "needs_approval"].includes(res.status), status: res.status, offlineOnly, reasons, summary: res.summary, error: res.error, duration_ms: Math.round(performance.now() - t0), llm_calls: (res.metrics || {}).llm_calls || 0, mode, result: res };
        CAF.emit({ type: "health" });
      }
    };
    await Promise.all(Array.from({ length: conc }, work));
    const live = ids.filter((id) => CAF.health[id].live).length;
    if (mode === "live") U.storage.set("caf.liveProbe", { at: Date.now(), live, total: ids.length });
    CAF.emit({ type: "health" });
    if (mode === "live") UI.toast(`${live}/${ids.length} agents live on Claude`);
    return { live, total: ids.length };
  },

  /* ---------------------------------------------------------------- runs */
  renderRuns() {
    const v = U.$("#view-runs"); v.innerHTML = "";
    if (!CAF.runs.length) { v.append(U.el("div", { class: "empty", text: "Orchestrated runs and single-agent runs from this session appear here." })); return; }
    v.append(U.el("div", { class: "panel" }, UI.table(CAF.runs.map((r) => r.single ? { when: new Date(r.created).toLocaleTimeString(), type: "agent", what: CAF.catalog[r.agent].name, status: r.result.status, summary: r.result.summary } :
      { when: new Date(r.created).toLocaleTimeString(), type: r.plan.mode, what: r.goal.slice(0, 80), status: r.status, summary: r.final ? r.final.headline : `${r.plan.steps.length} steps` }))));
  },
});

/* ---------------------------------------------------------------- boot */
(function boot() {
  try { CAF.init(); } catch (e) { document.body.prepend(U.el("div", { class: "errbox", text: `Console failed to start: ${e.message}` })); throw e; }
  UI.init();
  CAF.llm.init();
  CAF.downloads.init();
  setTimeout(() => UI.probe("offline"), 50);
})();
