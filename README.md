# CX Nova Agent Orchestration Layer

**One layer for every CX Nova agent: design a flow on a canvas, publish it, rent it, run it from your tools on the
model your policy allows.** The layer carries 97 agents: the 28 blueprint agents of the Coforge Agentic Framework
below, plus two **agent packs**, Campaign Studio (18 agents) and the ARM SDLC Factory (51 agents). Flows are
composed from any of them in the **orchestration designer** (`/shop/designer`): agents are nodes, a wire means
"runs after", and the inspector sets what each step is asked to do. 21 flows ship built in (8 playbooks, 4 Campaign
Studio stages, 9 ARM SDLC stages including the 50-step AI-to-Z release).

Around that: a storefront with indicative prices, customer accounts with API keys, subscriptions per agent or per
flow, a model policy per customer (its own LLM deployment, masking, allowed websites, limits), usage metering, and
an operator admin console. A customer key on REST, MCP or A2A sees and runs only what that customer rents; a rented
flow is one MCP tool. See [Orchestration layer](#orchestration-layer), [Agent packs](#agent-packs) and
[docs/DEPLOY_VM.md](docs/DEPLOY_VM.md) for the deployment on the CX Nova VM.

## Coforge Agentic Framework (the engine)

A **LangGraph orchestrator that drives all 28 agents** from the *Coforge Independent Blueprint*:
Coforge-owned, vendor-neutral re-implementations of the Optimizely Opal marketing agents. Every agent
is **live**. Each one is a compiled LangGraph sub-graph with a typed contract, runs end-to-end from
its own self-test, and is reachable from the orchestrator, a REST API, MCP, A2A, a CLI and a hosted
browser console.

```
goal ─► route (explicit / Claude planner / deterministic router) ─► plan (DAG)
     ─► parallel fan-out of agent sub-graphs (LangGraph Send) ─► outputs flow downstream
     ─► human approval gate for publish actions (LangGraph interrupt) ─► synthesis
```

* **No lock-in.** Agents ask for a model *tier*, never a vendor model. The same agents run on Anthropic,
  Azure OpenAI, OpenAI, AWS Bedrock, a local Ollama model, or fully offline. Every external system
  (GA4 / Adobe Analytics, AEM / Contentful / WordPress, Braze / AJO / Mailchimp, Optimizely / Adobe Target,
  Jira / Azure DevOps, Zoom, DeepL, Qdrant, Salesforce / HubSpot, Snowflake / BigQuery ...) sits behind a
  connector chosen in configuration.
* **Deterministic where it can be.** Crawling, rule engines, statistics, power analysis, anomaly
  detection, validation and encoding are code. The model writes and explains from computed facts.
* **Configuration over code.** Brand voice, taxonomies, platform limits, rule packs, heuristics,
  glossaries, playbooks and prompts are YAML and Jinja2 files.
* **Humans gate side effects.** CMS publish, ESP export, newswire distribution and variation deploys
  are proposed as actions and executed only after approval. Mandatory gates hold even under auto-approve.
* **Safe as a service.** REST, MCP and A2A runs cannot reach private networks or read local files outside
  `sample_data/` and `runs/`; the API needs `CAF_API_TOKEN` for anything but loopback clients. See
  [Security](docs/ARCHITECTURE.md#security).

## Quick start

Python 3.11 or newer (`python3 --version`; on a Mac, `brew install python@3.12` if it is older).
On macOS or Linux, `./scripts/setup.sh` does the install steps below in one go (virtual environment, dependencies,
Chromium, `.env`) and finishes with `caf health`.

```bash
cd coforge-agentic-framework
python3 -m venv .venv && source .venv/bin/activate          # Windows: py -3.12 -m venv .venv; .venv\Scripts\activate
pip install -e ".[anthropic,mcp,browser,dev]"                # add openai / bedrock / ollama extras if you use them
python -m playwright install chromium                        # optional: accessibility, heatmap and variation agents
cp .env.example .env                                         # then put your ANTHROPIC_API_KEY in .env (read automatically)

caf health                 # liveness probe of all 28 agents (offline model, no cost)
caf health --live          # same probe on your configured model
caf agents                 # the catalog
caf models                 # which model each model step uses, and why
caf run technical_seo_recommendations --selftest
caf orchestrate "Launch our flood-ready home cover: brief, email, LinkedIn, press release, social, tagged links and QR codes" \
    --input '{"url": "https://www.meridian-mutual.example/products/home-insurance.html"}'
caf serve                  # web console at http://127.0.0.1:8080/ plus REST + MCP + A2A (API docs at /docs)
CAF_API_TOKEN=$(openssl rand -hex 32) caf serve --host 0.0.0.0   # serve other machines (send the token as a bearer token)
```

Every run writes its artifacts (HTML reports, emails, CSVs, QR codes, translations ...) to `runs/<run-id>/`.
`caf serve` hosts the console (the orchestrator UI) at http://127.0.0.1:8080/, with its model calls answered by
your configured provider (see [Web console](#web-console)). The same page is published as a Claude artifact, and
`console/dist/console-local.html` opens it from disk with the offline model. All three load four small libraries from
jsDelivr, so they need internet.

Out of the box everything runs offline against a **fictional demo client, Meridian Mutual** (a website
with planted SEO/GEO/accessibility issues, six months of analytics, heatmap events, experiments, a
webinar, keyword data, a warehouse and an industry corpus). Swap any connector to a real platform in
`config/settings.yaml` / `.env`.

Docker: set `CAF_API_TOKEN` in `.env`, then `docker compose up --build` (includes Chromium for accessibility audits and
variation previews).

## The 28 agents

| Category | Agent | How it works | What it does | Connectors |
|---|---|---|---|---|
| Strategy and planning | **Campaign Brief Generation** | Model + guardrails | Turns a few inputs or rough notes into a standardised seven-section campaign brief and emits each deliverable as a task. | tasks |
| Strategy and planning | **Keyword Research** | Code + model | Researches, clusters and ranks keyword opportunities from first-party search data, SERP context and model expansion; intent and opportunity computed in code. | keyword_data, search, crawler |
| Strategy and planning | **Competitive Insights** | Code + model | Evidence-backed competitive intelligence for many competitors in parallel; every claim cited to timestamped evidence. | search, crawler, store |
| Strategy and planning | **UTM Generation** | Pure code | Consistent, correctly-encoded UTM links from a YAML naming taxonomy; single links or a whole channel matrix. | - |
| Content creation | **Email Creation** | Code + model | On-brand emails with audience variants, a deliverability linter and ESP-neutral HTML/MJML/text export. | esp |
| Content creation | **Blog Post Creation** | Model + guardrails | Research, outline, draft, self-critique, revise; SEO/readability/sourcing guardrails; mandatory approval before CMS publish. | search, crawler, cms |
| Content creation | **LinkedIn InMail Copy Creation** | Model + guardrails | Persona-specific InMail/message-ad copy validated against version-controlled platform limits, with merge-field previews. | ingestion |
| Content creation | **Content Adaptation** | Model + guardrails | One source asset to channel-ready versions governed by a YAML channel registry, plus a content calendar. | ingestion |
| Content creation | **Webinar Follow Up Email Creation** | Code + model | Behaviour-segmented follow-ups (attended, left early, no-show) with an MQL hand-off to sales. | webinar, esp |
| Content creation | **Industry Marketing** | Code + model | Industry tailoring grounded in your own corpus (retrieval) and versioned industry profiles, with compliance checks. | vectorstore, ingestion |
| Content creation | **Press Release Creation** | Model + guardrails | AP-style releases with versioned boilerplate, structure and forward-looking-statement checks, gated distribution. | newswire |
| Content creation | **Quote Extraction** | Code + model | Verbatim quotes anchored to the source by exact match (hallucinations rejected), speaker and location, open quote bank. | ingestion, store |
| Content creation | **QR Code Generation** | Pure code | Print-ready QR codes (PNG + SVG) that are UTM-tagged and routed through your own tracked short links. | shortlink |
| Content creation | **Content Summary** | Model + guardrails | Parallel map-reduce over semantic chunks for documents of any length, in six summary modes. | ingestion |
| Website optimization | **GEO Recommendations** | Code + model | AI-search readiness: AI-crawler access, llms.txt, performance, JSON-LD, E-E-A-T, citation readiness, scored in code. | crawler |
| Website optimization | **Technical SEO Recommendations** | Code + model | Crawler + 42-rule YAML pack, prioritised by first-party search traffic; the model only explains. | crawler, keyword_data |
| Website optimization | **GA4 Traffic Report** | Code + model | Period-over-period KPIs with significance tests, attribution and robust anomaly detection from GA4, Adobe Analytics or CSV. | analytics |
| Website optimization | **Web Accessibility Evaluation** | Code + model | axe-core in Chromium mapped to WCAG success criteria, CI gate with JUnit output. | crawler |
| Website optimization | **Heatmap Analysis** | Code + model | Click density, scroll reach and false bottoms, rage/dead clicks mapped to real components, then test hypotheses. | behaviour, crawler |
| Website optimization | **Content Model Creation** | Code + model | Platform-neutral content model emitted as AEM, Sitecore, Contentful, Contentstack, Strapi, Drupal and GraphQL artefacts. | crawler |
| Website optimization | **Content Translation** | Code + model | MT engine + post-editing, TMX memory, glossary and DNT, deterministic QA, gated CMS publish. | translation, ingestion, cms |
| End-to-end testing | **Experiment Ideation** | Code + model | Evidence-grounded test ideas scored with ICE/PIE in code, saved to a deduplicated backlog. | crawler, analytics, behaviour, store |
| End-to-end testing | **Experiment Planning** | Code + model | Hypothesis, variants, metrics, power analysis, duration, Bayesian and O'Brien-Fleming designs, exported to any testing tool. | crawler, analytics, experiments |
| End-to-end testing | **Variation Development** | Code + model | Declarative DOM patch + CSS compiled to safe JS, proven in Playwright with before/after screenshots, delivered via any flag tool. | crawler, flags |
| End-to-end testing | **Experiment Review** | Code + model | 23-rule pre-launch QA gate (power, overlap, SRM, guardrails ...), CI-ready. | experiments |
| End-to-end testing | **Experiment Summary** | Code + model | Lift, CIs, p-values, Bayesian probability to beat control, SRM, segments and a ship/iterate/abandon decision. | experiments, store, warehouse |
| End-to-end testing | **Exploration Generation** | Code + model | Governed text-to-query: semantic layer compiled to validated, read-only SQL over your warehouse. | warehouse |
| End-to-end testing | **Chart Summary** | Code + model | Trend, seasonality, change points, outliers and contributions computed first, then narrated, plus a WCAG long description. | - |

Liveness evidence for every agent: [`docs/LIVENESS.md`](docs/LIVENESS.md) (regenerate with `python3 scripts/liveness_report.py [--live]`).

## Agent packs

A pack brings another CX Nova product's agents into this layer without re-implementing them. It is one YAML file
in `config/packs/` (plus an optional `<pack>.contracts.json` with sample outputs); every agent in it becomes a
registered agent with the same contract as the blueprint agents, so it shows up in the catalog, the designer, the
storefront, REST, MCP, A2A and the console.

| Pack | Agents | Flows | Where it comes from |
|---|---|---|---|
| Campaign Studio (`cs_*`) | 18 | 4 (foresight, variants, checks, brief to measure) | `config/packs/campaign_studio.yaml`, written by hand from the studio's agent catalogue |
| ARM SDLC Factory (`arm_*`) | 51 | 9 (one per team stage, plus the 50-step AI-to-Z release) | generated: `python3 scripts/build_pack_arm.py --from <factory>/config` |

Every pack agent takes `action` (one of the things it can do), `request` (plain words), the inputs its pack
declares and `context`, and answers `{action, runner, summary, result, artifacts, human_followups, dry_run}`.
`runner` says who did the work:

1. **home**: the agent's own product ran it, when the pack has a home adapter and it is connected. Campaign
   Studio has one: set `CAF_PACK_CAMPAIGN_STUDIO_URL` and `CAF_PACK_CAMPAIGN_STUDIO_PASSCODE`, and pass
   `campaign_id`. Operator runs only; a customer's run never acts inside the operator's product.
2. **model**: this layer's model, working from the pack's skill prompt, checklist and standards, on the tier the
   pack names (and the customer's own deployment when the policy says so).
3. **twin**: with no model (offline), the contract's sample output, marked as a sample.

Actions that change another system (publish, send, platform writes) are marked `needs_home`: unless the home
product serves them they come back as a dry run with the follow-up a person has to do. The ARM SDLC Factory has no
run-one-agent API, so its agents are modelled here.

Packs do not change routing by goal: the planner and the router still choose among the 28 blueprint agents, the 8
playbooks and the flows you publish. Pack agents and the packs' own flows run when named, inside a flow you design,
or from the console's registry.
`CAF_PACKS_DISABLED=*` (or a list of pack ids) switches packs off; `caf health --packs` probes them
(`--packs --live` makes one model call per pack agent).

## Orchestrator

* **Routing.** Name an agent or playbook explicitly, or let the **planner** (the model, given the live catalog and
  playbooks) design a plan. Without a model, a **deterministic TF-IDF router** picks a playbook or agent. Plans are
  validated: unknown agents, inputs or dependencies are dropped with warnings.
* **Execution.** A LangGraph parent graph schedules ready steps and fans them out in parallel with `Send`
  (at most `orchestrator.max_parallel` run at once; a queued step starts as soon as any running step finishes).
  A failed dependency skips its dependents. Every finished step is upstream context for later steps. Each run ends
  with `runs/<run-id>/summary.md` (outcome, next steps, every step and its files) and `summary.json`.
* **Bindings.** Step inputs can reference `$goal`, `${input.url}`, `${steps.links.output.links.0.url}`, with
  fallbacks `${input.test_idea|steps.ideas.output.ideas.0.title|goal}`. Each agent declares which inputs the goal fills.
* **Approvals.** A held action (publish, send, deploy ...) shows in the run's `pending_approvals` as soon as its step
  finishes, and can be decided right away while the rest of the run goes on:
  `POST /runs/{id}/approvals {"decisions": {"<step>/<action>": "approve"}}` executes it at once. Whatever is still
  undecided when the last step finishes is held by a LangGraph `interrupt` (status `awaiting_approval`), and the same
  call resumes the run once every held action is decided (or `"*"`): an action left out of a decision stays held, it is
  never rejected by omission. Every decision, who took it and what executing it returned is kept in the run's approvals ledger,
  so no action is executed twice, even across a restart.
* **Runs off the request.** REST, MCP and A2A runs execute on a worker pool (`orchestrator.max_concurrent_runs`, more wait
  in a queue). A call waits up to `orchestrator.run_wait_s` (`CAF_RUN_WAIT_S`, default 60; a request can pass `wait_s`)
  and then answers **202** with the run so far; follow it with `GET /runs/{id}` (MCP: `get_run`; A2A: `tasks/get`). Short
  runs still answer in one call. Keep `run_wait_s` under your reverse proxy's read timeout. The CLI runs in the foreground.
* **Durable runs.** Checkpoints, run status and approval decisions are in `$CAF_DATA_DIR/orchestrator.db` (SQLite,
  `orchestrator.run_store`). After a restart, finished runs stay viewable, runs at the approval gate stay approvable, and a
  run that was cut off mid-way reports `stalled` once its process's heartbeat is a minute old
  (`orchestrator.stall_after_s`): `POST /runs/{id}/continue` picks it up from its last checkpoint without running finished
  steps again. A run another process is executing (the CLI next to the service) shows as `running` there and cannot be
  continued twice. Approval decisions are claimed atomically in the store, so an action executes once. In memory the orchestrator keeps only the runs in flight. Settled runs older than
  `orchestrator.keep_runs_days` (default 30) are pruned at start-up (their files under `runs/` stay).
* **Playbooks** (`config/playbooks.yaml`), which together cover all 28 agents: campaign launch kit, webinar programme,
  SEO content factory, competitive pitch, website audit, experimentation loop, insights reporting, headless CMS migration.

## Surfaces

| Surface | Where | Notes |
|---|---|---|
| REST | `caf serve` then `/docs` | `/agents`, `/agents/{id}/run`, `/flows`, `/flows/{id}/run`, `/plan` (preview the steps a goal would run, nothing started), `/orchestrate`, `/runs` (list, filtered by status, agent, flow or text), `/runs/stats`, `/runs/{id}`, `/runs/{id}/trace`, `/runs/{id}/approvals`, `/runs/{id}/continue`, `/runs/{id}/artifacts/...`, `/approvals` (every held action), `/health`, `/health/agents`, `/playbooks`, `/connectors`, `/r/{code}` short links |
| MCP | `/mcp` (streamable HTTP) or `caf mcp` (stdio) | `run_<agent_id>` for every agent, `run_flow_<flow_id>` for every published flow, plus `list_agents`, `list_flows`, `run_flow`, `orchestrate`, `approve_actions`, `get_run`; with a customer key the tool list is that customer's subscription |
| A2A | `/.well-known/agent-card.json`, `POST /a2a` | Orchestrator card listing every agent as a skill; JSON-RPC `message/send`, `tasks/get` |
| CLI | `caf` | `agents`, `describe`, `run`, `orchestrate`, `playbooks`, `health`, `serve`, `mcp`, `export-catalog` |
| Portal | `caf serve` then `/` (or `/portal`) | The one place to run and watch everything: launch, every run live and step by step, approvals, agents, flows, health, customers (below) |
| Console | `/console` | The engineering console: browser twins of all agents, model calls on the server's provider (see below) |

## Model providers

`config/settings.yaml` maps tiers (`fast`, `balanced`, `pro`, `complex`) to models per provider. `CAF_LLM_PROVIDER=auto`
picks the first configured provider (`ANTHROPIC_API_KEY`, Claude on Microsoft Foundry, Azure OpenAI, OpenAI, Bedrock,
Ollama), otherwise `offline`. Override a tier with `CAF_MODEL_FAST` / `_BALANCED` / `_PRO` / `_COMPLEX`. Structured outputs
are Pydantic models, validated and repaired once on failure.

* **Claude on Microsoft Foundry:** set `ANTHROPIC_FOUNDRY_API_KEY` and `ANTHROPIC_FOUNDRY_BASE_URL` (the deployment's Target
  URI works as-is). Tiers map to your deployment names (`FOUNDRY_DEPLOYMENT_FAST` ... `_COMPLEX`; defaults
  `claude-sonnet-5-5` and `claude-opus-5`).
* **Model routing:** each model step is routed by the kind of work it does (extract, repair, copywrite, analyze, review,
  code ...) in `config/model_routing.yaml`; the router steps a call up for large inputs, large strict outputs, images or
  a retry after an invalid answer, and `CAF_MODEL_PROFILE=economy|balanced|quality` shifts everything. Pin a step's
  `tier:` to confirm a decision. `caf models` prints the table ([`docs/MODEL_ROUTING.md`](docs/MODEL_ROUTING.md)), and every
  agent result lists the model used per step under `provenance.model_routes`. The browser console follows the same file.
* **Throttling:** every model call from every agent goes through one shared limiter per deployment: calls in flight,
  calls per minute and tokens per minute (`llm.throttle`, per-provider overrides, or `CAF_LLM_MAX_CONCURRENT`,
  `CAF_LLM_RPM`, `CAF_LLM_TPM`). 429 / overloaded answers are retried with the provider's Retry-After, and a refusal
  that outlasts the retries pauses the deployment for all agents.

## Connectors

19 connector kinds, each with a local provider (offline demo) and vendor adapters. Full matrix, config keys and
verification status: [`docs/CONNECTORS.md`](docs/CONNECTORS.md).

## Browser console

`console/` builds a single self-contained page (`python3 console/build.py`) that embeds the live catalog (Pydantic
input/output/prompt schemas), skills, playbooks, Nunjucks-precompiled versions of the same prompt templates (no `eval`),
the demo site and compressed sample data. Each agent has a browser twin (`console/src/js/agents/<id>.js`) that must
satisfy the same output contract. Model steps go to Claude through the page's `sample` capability, or to the offline
model. Prompt parity between Jinja2 and Nunjucks is enforced by `tests/test_prompt_parity.py`, and every browser
agent is tested headless under a strict no-eval CSP by `console/test/harness.py`.

## Orchestration layer

`caf serve` is the layer: the portal at `/portal` (where a browser lands), the storefront at `/shop`, the
orchestration designer at `/shop/designer`, the agent console at `/console`, the customer's account at
`/shop/account`, the operator's admin console at `/shop/admin`, and the API, MCP and A2A surfaces underneath.

| Piece | What it does |
| --- | --- |
| Portal (`/portal`, operator token or a customer key) | One application over the whole layer, with a persistent band that says what is running, what waits for a decision and what ran today. **Overview**: live runs with a dot per step, the actions waiting for you (approve or reject from there), recent runs, the last 14 days as bars, the catalogue by kind of work. **Launch**: describe the outcome, preview the plan the orchestrator would run (the graph of steps, how it was chosen, warnings), then run it: the run opens live at once; or pick a flow or one agent and fill its declared inputs. **Runs**: every run kept, grouped by day and filtered by status, flow, agent or text; one run is its graph (long chains wrap onto bands; a running step glows and work flows along the live edges), a timeline of what ran in parallel and a live log; a step shows what it was given and where each value came from (the goal, a run input or an earlier step's output), the earlier work it could read, its output, files (previewed and downloadable), checks, model calls, held actions and its own activity; held actions are decided and a stalled run continued from the page. **Approvals**: every held action across runs. **Agents**: by the work they do, with a run form built from each agent's contract and the runs that used it. **Flows**: each as a graph with its inputs and history. **Health**: model, agent probe, connectors, packs. **Customers** (operator) or **My account** (customer). ⌘K / Ctrl K searches agents, flows and runs, or takes a goal to run. Midnight and paper themes, shared with every other page |
| Orchestration designer (`/shop/designer`, operator token) | A canvas: pick agents from the palette (grouped by the work they do; a pack agent carries its product's tag), click to chain them or drag them in, drag from one node's port to another to say "runs after", and set each step's action, purpose and input bindings in the inspector (suggestions for `$goal`, `${input.x}`, `${steps.s.output...}`). Check validates the flow, Test run executes it and lights each node, Save and Publish put it on sale. Built-in flows open read-only; Clone makes an editable copy. The console's "Save plan as flow" opens the plan here |
| Runs | The portal's Runs view (`/portal#/runs`; the earlier `/shop/runs` address still opens it). The designer's Test run links to it, step by step |
| Storefront (`/shop`, `/shop/agents/{id}`, `/shop/flows/{id}`) | Every agent and published flow with its contract, steps and stages, model steps, connectors and indicative price (`config/shop_catalog.yaml`); an access-request form |
| Orchestration flows | A published flow is a storefront item, a playbook for the router, the planner and the console, the MCP tool `run_flow_<id>`, `POST /flows/{id}/run` and the A2A skill `flow:<id>`; unpublishing removes all of that. Admin → Flows lists your flows, the built-in playbooks and each pack's flows. CLI: `caf shop flows list / show / save / publish / clone / test` |
| Customers, keys, subscriptions | `caf shop add / key / subscribe --agents a,b --flows f`, or the admin console. Keys are `cxn_...`, stored as hashes, shown once. A flow is rented as a whole: the customer runs the flow (every agent in it, for runs of the flow), not its agents one by one |
| Entitlements | With a customer key, `/agents`, `/flows`, `/playbooks`, the MCP tool list, the A2A card and every run are limited to the subscription; a planned step outside it is skipped and reported |
| Run modes | 1 Coforge Cloud (the layer's model), 2 Coforge Cloud with the customer's model, 3 Customer Cloud (this same image in the customer's environment) |
| Customer policy | `config/policies/example-customer.yaml`: provider, endpoint, credential (`api_key_env` on the server, or inline and encrypted with `CAF_SECRET_KEY`), routing tiers → the customer's deployments, masking before the model (email, phone, policy and card numbers, IBAN), images on/off, allowed websites, approvals, runs per day and tokens per month |
| Metering | Every agent run, and every step of an orchestrated run (once per step, so a flow that uses one agent in five steps is metered five times): customer, agent, step, version, surface, run mode, provider, tokens, cost, duration, outcome. A run still going is metered when it settles; the daily run limit counts runs when they start. `caf shop usage`, `/shop/me/usage`, the admin usage view |
| Assessment and the workshop | Before a goal runs, the plan is held against it (`POST /plan` → `feasibility`, one model call): each requirement of the goal is marked covered, partial or missing by the planned steps, with what the run will deliver (`can_do`), what it will not (`cannot_do`), and for each gap the agent that would fill it. The portal's Launch runs this before every goal: when everything is covered the run starts at once; otherwise the assessment is shown and the person runs what is covered or **makes the missing agent**. The workshop drafts it (`POST /agents/draft`: contract, skill, catalog entry, one model call), the operator reviews every field, and `POST /agents/made` registers it: it is a pack agent of the `made` pack (`$CAF_DATA_DIR/packs/made.yaml`), modelled on the layer's model (or the customer's, by policy), contract-checked, in every catalog, callable from REST, MCP (the tool list follows without a restart), A2A and flows, metered and priced, picked by the planner for later goals, and kept across restarts and updates. `GET /agents/made` lists them, `DELETE /agents/made/{id}` removes one. The assessment a run was started from stays with the run (Scope, on the run page) |
| Cost | Every model call is priced as it happens from `llm.pricing` in `config/settings.yaml` (dollars per million tokens, input and output, per model family; a model id matches the longest key it contains, so Foundry deployment names and Bedrock ids match; prompt-cache reads and writes at a factor of the input rate). The cost travels with the step (`metrics.cost_usd`, per model under `metrics.models`), the run (the run index: `cost_usd`, what each agent cost), the statistics (`/runs/stats`: per day, totals, average per agent and per flow) and the usage ledger. A model the table does not know is counted as *unpriced*, never as free. `POST /plan` returns an `estimate` of what the steps are likely to cost, the mean of each agent's earlier runs. The portal shows all of it: on every run, every step, the plan preview ("likely $0.17 from earlier runs"), each agent's typical cost, spend per day, per customer and per month. The shipped figures are the published list prices of each family: put your contract's rates in the table |
| Agent check | `caf shop check <customer>` runs the subscribed agents' self-tests on the customer's model and keeps the pass table on the account |
| Branding | CX Nova (Campaign Studio's palette, IBM Plex); `CAF_PRODUCT_NAME`, `CAF_COMPANY` override it |

Roles on the API: the operator token (`CAF_API_TOKEN`) is admin; a customer key is that customer; with no token
configured, local calls are admin. The layer's records live in `data/shop.db` (`CAF_DATA_DIR`). The product was
called CX Nova Agent Shop before; the URL paths (`/shop/...`), the `/agentshop` prefix on the VM and the Windows
service name `CXNovaAgentShop` keep that name so existing links and installs go on working. The MCP server is now
named `cxnova-orchestration`: a client configured under the old name keeps working, since the name in a client's
config is only a label.

## Web console

`caf serve` serves the console at `/console` (a browser opening the server's address `/` lands on the portal, which links to it). A small
bridge gives the page the same interface it has as a Claude artifact, backed by two server routes:
`GET /console/host` (which provider and models answer) and `POST /console/llm` (one model call). So the console's model
calls run on the framework's own provider (for example Claude on Microsoft Foundry) with the shared throttle, the
routing tier of each step (fast/balanced on Sonnet, pro/complex on Opus) and the effort for that tier. The API key never
reaches the browser. Files the agents produce are downloaded by the browser; the status chip reads
"Claude on Microsoft Foundry connected" (or the provider in use).

The hosted console, the designer and the storefront group all 97 agents by the work they do (strategy, analytics, design,
content, website optimization, engineering, quality, experimentation, compliance, activation, release), the blueprint's
agents first in each group and a pack's agents beside them with the pack's tag (`CS`, `ARM`); the order is
`caf.core.spec.CATEGORY_ORDER`. A pack names its tag and each agent's category in its YAML. Blueprint agents run in
the browser with their model steps on the server; pack agents and pack flows run on the server (`POST
/agents/{id}/run`) and report `runtime: server` with the runner that served them.

Access: with no `CAF_API_TOKEN`, the server answers only this computer (`127.0.0.1`). To serve other people, set a long
random `CAF_API_TOKEN` and listen on all interfaces; the page asks for the token once (or open
`https://<host>/console?token=<token>` once, and it is remembered in that browser):

```bash
CAF_API_TOKEN=$(openssl rand -hex 32) caf serve --host 0.0.0.0
```

To give it a public address, run the container (`Dockerfile`, `docker-compose.yml`) on any host that serves HTTPS, for
example Azure Container Apps or Azure App Service, with the same variables as your `.env`: `CAF_LLM_PROVIDER`,
`ANTHROPIC_FOUNDRY_API_KEY`, `ANTHROPIC_FOUNDRY_BASE_URL` and `CAF_API_TOKEN`. Container port 8080.

## Testing

```bash
CAF_LLM_PROVIDER=offline python3 -m pytest -q           # whole suite (agents, orchestrator, API, MCP, A2A, connectors, parity)
python3 console/test/harness.py [--llm mock]            # every browser agent, offline and through the live code path
python3 console/test/harness.py --llm mock --orchestrate campaign_launch   # a playbook driven end to end in the browser
make package                                            # source zip in dist/ (no caches, run outputs or node_modules)
```

## Extending

* **Add an agent:** follow [`docs/AGENT_AUTHORING.md`](docs/AGENT_AUTHORING.md) and add its browser twin per
  [`docs/CONSOLE_AGENT_AUTHORING.md`](docs/CONSOLE_AGENT_AUTHORING.md). The registry, orchestrator, API, MCP, A2A,
  health probe, console and the shop's storefront pick it up automatically; give it a price and an outcome name in
  `config/shop_catalog.yaml` (otherwise the defaults apply) and a routing entry per model step in
  `config/model_routing.yaml`.
* **Add a connector provider:** subclass the kind's interface, decorate with `@connector(kind, provider)`, select it in
  `settings.yaml`.
* **Add a playbook:** edit `config/playbooks.yaml`, or design it in `/shop/designer` and publish it.
* **Add an agent pack:** add `config/packs/<pack>.yaml` (pack head, `agents:` with their actions, inputs and skill
  prompt, optional `flows:`) and restart. `config/packs/campaign_studio.yaml` is the hand-written reference;
  `scripts/build_pack_arm.py` shows how to generate one from another product's own configuration. A home adapter
  (running an agent inside its own product) is code: see `run_home` in `src/caf/packs/agent.py`.

## What was verified, and what needs your credentials

* Verified in this build: every blueprint agent's full graph, the orchestrator, all 8 playbooks, the API/MCP/A2A
  surfaces and every local connector, using the offline model and the demo data; axe-core and Playwright paths in
  Chromium; vendor adapters' request/response shapes against mocked APIs; all 28 browser twins and all 8 playbooks in
  Chromium (offline and mock-Claude paths); statistics recomputed against scipy/statsmodels; an independent security
  review (findings fixed and covered by `tests/test_security.py`). Agent packs (`tests/test_packs.py`): all 97
  agents registered and answering, the three runners (home against a stand-in studio, model against a stand-in
  model, twin offline), dry runs, every surface, and designed flows including a 50-step clone. The designer was
  driven end to end in Chromium.
* Needs your environment: real model calls (set a provider key and run `caf health --live`; add `--packs` for the
  pack agents), the Campaign Studio home runner against the real studio, and vendor adapters
  against live tenants (GA4, Adobe Analytics/Target/AJO/AEM, Optimizely, Braze, Jira ...). Open questions per vendor
  API are listed in `docs/CONNECTORS.md`.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the layer diagram and design rules.
