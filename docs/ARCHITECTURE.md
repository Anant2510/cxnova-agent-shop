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
