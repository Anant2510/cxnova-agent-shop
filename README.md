# CX Nova Agent Shop

**Marketing and experimentation agents you rent, run from your tools, on the model your policy allows.** The shop
sells the 28 agents and 8 playbooks of the Coforge Agentic Framework below: a storefront with indicative prices,
customer accounts with API keys, subscriptions per agent or bundle, a model policy per customer (its own LLM
deployment, masking, allowed websites, limits), usage metering, and an operator admin console. A customer key
on REST, MCP or A2A sees and runs only what that customer rents. See [Agent Shop](#agent-shop) below and
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
* **Approvals.** Pending publish actions trigger a LangGraph `interrupt`. Resume with
  `POST /runs/{id}/approvals {"decisions": {"<step>/<action>": "approve"}}`. Runs are checkpointed (in-memory by default;
  plug a LangGraph Postgres/SQLite saver for durability).
* **Playbooks** (`config/playbooks.yaml`), which together cover all 28 agents: campaign launch kit, webinar programme,
  SEO content factory, competitive pitch, website audit, experimentation loop, insights reporting, headless CMS migration.

## Surfaces

| Surface | Where | Notes |
|---|---|---|
| REST | `caf serve` then `/docs` | `/agents`, `/agents/{id}/run`, `/orchestrate`, `/runs/{id}`, `/runs/{id}/approvals`, `/runs/{id}/artifacts/...`, `/health`, `/health/agents`, `/playbooks`, `/connectors`, `/r/{code}` short links |
| MCP | `/mcp` (streamable HTTP) or `caf mcp` (stdio) | `run_<agent_id>` for every agent, plus `orchestrate`, `approve_actions`, `list_agents` |
| A2A | `/.well-known/agent-card.json`, `POST /a2a` | Orchestrator card listing every agent as a skill; JSON-RPC `message/send`, `tasks/get` |
| CLI | `caf` | `agents`, `describe`, `run`, `orchestrate`, `playbooks`, `health`, `serve`, `mcp`, `export-catalog` |
| Console | `caf serve` then `/` (or `/console`) | The orchestrator UI: browser twins of all agents, model calls on the server's provider (see below) |

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

## Agent Shop

`caf serve` is the shop: the storefront at `/shop`, the agent console at `/console`, the customer's account at
`/shop/account`, the operator's admin console at `/shop/admin`, and the API, MCP and A2A surfaces underneath.

| Piece | What it does |
| --- | --- |
| Storefront (`/shop`, `/shop/agents/{id}`) | Every agent and bundle with its contract, model steps, connectors and indicative price (`config/shop_catalog.yaml`); an access-request form |
| Customers, keys, subscriptions | `caf shop add / key / subscribe`, or the admin console. Keys are `cxn_...`, stored as hashes, shown once |
| Entitlements | With a customer key, `/agents`, `/playbooks`, the MCP tool list, the A2A card and every run are limited to the subscription; a planned step outside it is skipped and reported |
| Run modes | 1 Coforge Cloud (the shop's model), 2 Coforge Cloud with the customer's model, 3 Customer Cloud (this same image in the customer's environment) |
| Customer policy | `config/policies/example-customer.yaml`: provider, endpoint, credential (`api_key_env` on the server, or inline and encrypted with `CAF_SECRET_KEY`), routing tiers → the customer's deployments, masking before the model (email, phone, policy and card numbers, IBAN), images on/off, allowed websites, approvals, runs per day and tokens per month |
| Metering | Every agent run: customer, agent, version, surface, run mode, provider, tokens, duration, outcome. `caf shop usage`, `/shop/me/usage`, the admin usage view |
| Agent check | `caf shop check <customer>` runs the subscribed agents' self-tests on the customer's model and keeps the pass table on the account |
| Branding | CX Nova (Campaign Studio's palette, IBM Plex); `CAF_PRODUCT_NAME`, `CAF_COMPANY` override it |

Roles on the API: the operator token (`CAF_API_TOKEN`) is admin; a customer key is that customer; with no token
configured, local calls are admin. The shop's records live in `data/shop.db` (`CAF_DATA_DIR`).

## Web console

`caf serve` serves the console at `/console`, and a browser opening the server's address (`/`) is sent there. A small
bridge gives the page the same interface it has as a Claude artifact, backed by two server routes:
`GET /console/host` (which provider and models answer) and `POST /console/llm` (one model call). So the console's model
calls run on the framework's own provider (for example Claude on Microsoft Foundry) with the shared throttle, the
routing tier of each step (fast/balanced on Sonnet, pro/complex on Opus) and the effort for that tier. The API key never
reaches the browser. Files the agents produce are downloaded by the browser; the status chip reads
"Claude on Microsoft Foundry connected" (or the provider in use).

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
* **Add a playbook:** edit `config/playbooks.yaml`.

## What was verified, and what needs your credentials

* Verified in this build: every agent's full graph, the orchestrator, all 8 playbooks, the API/MCP/A2A surfaces and
  every local connector, using the offline model and the demo data (547 tests); axe-core and Playwright paths in
  Chromium; vendor adapters' request/response shapes against mocked APIs; all 28 browser twins and all 8 playbooks in
  Chromium (offline and mock-Claude paths); statistics recomputed against scipy/statsmodels; an independent security
  review (findings fixed and covered by `tests/test_security.py`).
* Needs your environment: real model calls (set a provider key and run `caf health --live`), and vendor adapters
  against live tenants (GA4, Adobe Analytics/Target/AJO/AEM, Optimizely, Braze, Jira ...). Open questions per vendor
  API are listed in `docs/CONNECTORS.md`.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the layer diagram and design rules.
