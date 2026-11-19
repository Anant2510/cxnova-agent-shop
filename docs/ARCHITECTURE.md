# Architecture

```mermaid
flowchart LR
  subgraph Entry["Entry points"]
    CLI[caf CLI]
    REST[REST API /docs]
    MCP[MCP server - every agent is a tool]
    A2A[A2A agent cards + JSON-RPC]
    CON[Hosted console]
  end
  subgraph ORCH["Orchestrator (LangGraph parent graph)"]
    R[route: explicit / Claude planner / deterministic router] --> S[schedule]
    S -- Send fan-out --> RS[run_step x N in parallel]
    RS --> S
    S -- nothing left --> AP[approve: interrupt for publish actions]
    AP --> SY[synthesize]
  end
  subgraph AGENTS["28 agents (each a compiled LangGraph sub-graph)"]
    ST[Strategy & planning x4]
    CC[Content creation x10]
    WO[Website optimization x7]
    ET[End-to-end testing x7]
  end
  subgraph SHARED["Shared runtime"]
    LLM[Model adapter: tiers -> Anthropic / Azure OpenAI / OpenAI / Bedrock / Ollama / offline]
    SK[YAML skills: brand voice, taxonomies, rule packs]
    PR[Jinja2 prompts (Nunjucks-compatible)]
    CN[Connector hub: 19 kinds, local + vendor providers]
    AR[Artifacts + traces: runs/<run_id>/]
  end
  Entry --> ORCH --> AGENTS --> SHARED
```

## Layers
| Layer | Package | Responsibility |
|---|---|---|
| Contracts | `caf.core.spec`, `caf.core.handoff` | AgentSpec, AgentResult, Check, ArtifactRef, PendingAction, Finding |
| Agent runtime | `caf.core.agent`, `caf.core.context` | Every agent = LangGraph `StateGraph(AgentState, context_schema=RunContext)`; typed input/output validation; degraded-mode reporting; action execution |
| Registry | `caf.core.registry` | Discovers agents, merges blueprint metadata, proves all 28 are registered |
| Model adapter | `caf.core.llm`, `caf.core.offline` | Tier -> model per provider, structured output with repair, usage metering, offline model |
| Configuration | `config/` | settings (env-interpolated), blueprint, playbooks, skills, prompts |
| Connectors | `caf.connectors` | 19 connector kinds; local providers (offline demo) + vendor adapters (GA4, Adobe Analytics, AEM, Adobe Target, AJO, Contentful, Braze, Jira ...) |
| Orchestrator | `caf.orchestrator` | Router, planner, playbooks, bindings, LangGraph parent graph with Send fan-out and interrupt-based approvals |
| Surfaces | `caf.api`, `caf.cli` | FastAPI (REST, artifacts, short links), MCP (streamable HTTP + stdio), A2A, CLI |
| Liveness | `caf.health` | Runs every agent's self-test through its full graph; `/health/agents`, `caf health` |
| Console | `console/` | Browser twin runtime over the same contracts; Claude via the page's sample capability |

## Design rules (from the Coforge Independent Blueprint)
1. **No vendor lock-in.** Agents request a model *tier*, never a vendor model; every external system sits behind a connector interface chosen in configuration.
2. **Deterministic where it can be.** Detection, statistics, validation, encoding and rule engines are code; the model generates and explains from computed facts.
3. **Configuration over code.** Brand voice, taxonomies, channel limits, rule packs, heuristics, glossaries and playbooks are YAML; prompts are templates.
4. **Typed hand-offs.** Outputs are contracts; analysis agents emit `findings` so any agent can consume them.
5. **Humans gate side effects.** Publishing is proposed as an action and executed only after approval (or explicit auto-approve).
6. **Prove liveness.** Every agent ships a self-test; the registry and the health probe fail loudly if any agent is missing or broken.

## Runs

| Concern | How |
|---|---|
| Where a run executes | CLI: on the calling thread. REST, MCP, A2A: on a bounded worker pool (`orchestrator.max_concurrent_runs`); the call waits up to `orchestrator.run_wait_s` and answers 202 with the run so far, which the caller follows with `GET /runs/{id}` (`get_run`, `tasks/get`) |
| Run state | `caf.orchestrator.persistence`: LangGraph's in-memory saver with a write-through to `$CAF_DATA_DIR/orchestrator.db`; a run's checkpoints load into memory only while it runs or is read, and leave when it settles |
| Statuses | `queued`, `running`, `awaiting_approval`, `completed`, `completed_with_errors`, `failed`, `stalled` (the process running it stopped: its heartbeat in `run_index` is older than `orchestrator.stall_after_s`; `POST /runs/{id}/continue` resumes from the last checkpoint, finished steps are not run again) |
| Portal | `/portal`: one page application (`src/caf/shop/web/portal.html` + `portal.js`) over the API, hash-routed (`#/`, `#/launch`, `#/runs/<id>/<step>`, `#/approvals`, `#/agents/<id>`, `#/flows/<id>`, `#/health`, `#/customers`, `#/account`). It polls `GET /runs`, `GET /approvals` and `GET /runs/stats` every 5 s (and the views of the runs in flight for the "now" band), and an open run every 2 s until it settles. `POST /plan` previews the steps a goal, flow or agent would run without starting anything; the previewed plan is passed to `POST /orchestrate` as `plan`, so what ran is exactly what was shown. The run index carries per-run metrics (agents, steps, model calls, tokens, duration), recorded when the run settles, so lists and statistics never open a run's checkpoints |
| Run viewer | The portal's Runs view (`/shop/runs` still opens it) over `GET /runs` (the run index), `GET /runs/{id}` (polled while the run goes on) and `GET /runs/{id}/trace?since=n` (the trace, incrementally). Each finished step's result carries `step_inputs` (resolved bindings), `upstream` (the earlier steps it could read) and `timing`; a step executing in this process shows `running` with `started_at` and the model call it is on (from the run's trace events); `model.done` events attribute each model answer to its agent |
| Approvals | Held actions appear in `pending_approvals` as their step finishes and can be decided mid-run (executed at once); the approval gate at the end holds only what is undecided, and a decision on some held actions leaves the others held (never rejected by omission). Decisions are in the approvals ledger (`run_actions`), which the gate honours. Each decision is claimed with a single insert before the action executes, so it executes once even when two processes reach it together |
| Entitlement of a run | The customer's allowed agents are part of the run's state, so a run continued after a restart keeps them; approving or continuing a customer's run rebuilds its context from the customer's current policy (`ShopService.context_for_run`) |
| Metering | Once per step (`usage.step_id`, unique per run and step), when the run settles; the daily run limit counts runs when they start |
| Assessment, made agents | `caf.workshop`: `assess()` (prompt `orchestrator/assess.j2`, work type analyze) returns a `Feasibility` (verdict, can_do, cannot_do, requirements with the steps that cover them, gaps with a proposed agent each); offline it is `unassessed`, never a guess. `draft_agent()` (`orchestrator/draft_agent.j2`, work type design) returns an `AgentDraft`; `register_made()` validates it as a `PackAgent`, writes the `made` pack under the data folder (`caf.packs.model.made_packs_dir`), resets the pack cache and reloads the registry; the pack carries `auto_route: true`, so its agents are `blueprint_agents()` for the routers and the planner. `POST /orchestrate` takes the previewed `feasibility` and the route node keeps it in `route.feasibility`. `build_mcp` exposes `sync_agent_tools()`, called after a make or a remove |
| Agent packs, the code runner | `caf.packs`: one YAML per pack in `config/packs/` (plus the `made` pack under the data folder); `build_agent_class` makes a registered agent per pack agent, and `serve()` tries the runners in order: `code` when the agent declares `code: module:function` (`run_code` imports it once and calls `fn(action_id, inputs, ctx)`; a `ValueError` fails the step, any other exception is recorded as a degraded reason and the next runner takes over), `home` when the pack has a connected home adapter, `model`, then `twin`. `caf.accelerators.intellisight` is the code behind IntelliSight's five agents: `Dataset` (Adobe column classification and labels), `analyse`, `series`, `breakdown`, `custom_report`, `recommend` and the two-step `ask` (model query plan → engine → model narration; offline a heuristic plan and a plain answer); the dataset is kept with the run as `dataset.csv` and handed on as `dataset_file`. A pack's `auto_route` says whether `pack_playbooks()` and `blueprint_agents()` offer its flows and agents to the routers |
| Cost | `caf.core.pricing.Pricing` prices each call in `LLM._account` from `llm.pricing` (settings); the `Meter` keeps cost and per-model counts per step (`result.metrics.cost_usd`, `metrics.models`, `unpriced_calls`), the planner's and synthesizer's own calls land in `route.planner_metrics` and `final.metrics`, `run_metrics` rolls everything into the run index (`cost_usd`, `unpriced_calls`, `agent_costs` JSON), `Orchestrator.stats` and `estimate` read it back (per day, per agent, per flow; the mean per agent is the plan estimate), and `ShopStore.record_usage` keeps `cost_usd` per metered step |
| Retention | Settled runs older than `orchestrator.keep_runs_days` are pruned from the store at start-up; files under `runs/` are kept |

## Security

Runs are created under one of two security modes (`caf.core.security`):

| Mode | Used by | Network fetches (crawler, ingestion, Chromium) | Local files named by the caller |
|---|---|---|---|
| `local` | CLI, Python library | any host, including intranet | any path the operator names |
| `service` | REST API, MCP (HTTP and stdio), A2A | public internet hosts, fixture hosts and `security.allowed_hosts`; loopback, private, link-local, CGNAT and internal names are refused, on every redirect hop and every browser subresource | only `security.file_roots` (default `sample_data/`) and `runs/` |

Service controls, all in the `security` block of `config/settings.yaml` (or the matching env vars):

* **API access.** `CAF_API_TOKEN` set: every route except `/`, `/health`, `/docs`, `/openapi.json`, the A2A card and `/r/<code>`
  short links needs `Authorization: Bearer <token>` (or `X-API-Key`). Not set: only loopback clients are answered.
  `caf serve` binds `127.0.0.1` unless told otherwise. Cross-origin browser access is off unless `CAF_CORS_ORIGINS` lists origins.
* **Approval gates.** API callers can skip gates per request only when the operator enables
  `approvals.allow_request_auto_approve`; MCP tools never can (held actions need `approve_actions`, which MCP clients show
  to the user). Mandatory gates (blog publish) hold under every setting.
* **Paths and ids.** Run ids are `[A-Za-z0-9_-]` only, so `/runs/{id}/...` can never leave its own folder; CMS slugs and
  locales, ESP and newswire names are slugified; experiment ids are validated before they become file names or vendor URLs.
* **Read-only analytics SQL.** `exploration_generation` compiles a governed semantic layer to a single `SELECT`, and DuckDB
  runs it with external access disabled.

Residual risk: host names are checked when each request is made, so a DNS answer that changes between the check and the
connection (DNS rebinding) is not caught. Run the service with network egress controls (no route to cloud metadata
endpoints or internal networks) for defence in depth.
