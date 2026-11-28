# Using your agents: the four doors

You rent agents and flows from the CX Nova Agent Orchestration Layer. Your key (`cxn_…`, issued once by the operator) is
the only credential: whichever door you come through, it signs you in as your account, shows you what you rent and nothing
else, and meters what you run. Nothing below needs a different key or a different account.

| Door | For | When it is the right one |
|---|---|---|
| **Portal** | A person runs and watches | Someone on your side launches runs by hand, follows them live, approves held actions and reads the results. Nothing to integrate: the key signs them in at `/portal`. |
| **REST API** | Your systems trigger runs | A scheduler, a data pipeline or an application starts runs and collects results. The simplest integration when there is no assistant and no orchestrator in between. |
| **MCP** | Your assistant gets the agents as tools | Claude Desktop, Microsoft Copilot or your own agent should be able to pick an agent and fill its inputs from the conversation, with a person reading the answers. |
| **A2A** | Your orchestrator delegates tasks | Your own orchestrator (Copilot Studio, Agentforce, Google ADK, LangGraph) owns the end-to-end process and hands whole tasks to this layer, polling them to completion. |

The portal's **My account** page shows the same four doors with *your* addresses, the tool and skill names your key unlocks,
and copy-ready samples for your first rented flow, with your key already in them. What follows is the general form; `<host>`
is the layer's address (for example `https://cxnova2.centralindia.cloudapp.azure.com/agentshop`).

## Portal

Open `<host>/portal` and sign in with the key. **Launch** runs a goal in plain language (the planner chooses among what you
rent), or a flow or an agent by name with its declared inputs; where a flow reads a data file (IntelliSight's Adobe Analytics
export), **Data file** attaches it from your computer. **Runs** follows every run live, step by step, with its cost; **Approvals**
decides actions a run holds (publishing, sending); **My account** shows what you rent, what you ran this month, your policy
and how to connect.

## REST API

Every surface in one HTTP API, documented interactively at `<host>/docs`. Send the key as `Authorization: Bearer <key>`.

```bash
# run a rented flow with its inputs; a run that takes longer than wait_s answers 202 with the run so far
curl -H 'Authorization: Bearer <key>' -H 'Content-Type: application/json' \
  <host>/flows/is_executive_review/run -d '{"inputs": {"dataset_name": "xperion"}, "wait_s": 60}'

# follow it
curl -H 'Authorization: Bearer <key>' <host>/runs/<run_id>

# or let the planner choose among what you rent
curl -H 'Authorization: Bearer <key>' -H 'Content-Type: application/json' \
  <host>/orchestrate -d '{"goal": "Read this Adobe Analytics export and tell me what to fix first", "inputs": {"dataset": "<csv text>"}}'
```

`GET /agents`, `GET /flows` and `GET /playbooks` list what the key rents. A planned step outside the rental is skipped and
reported in the run, never silently run. Held actions are decided with `POST /runs/<run_id>/approvals` (`{"decisions": {"<step>/<action>": "approve"}}`).

## MCP: your assistant gets the agents as tools

The layer is a remote MCP server (streamable HTTP) at `<host>/mcp`. Add it to Claude Desktop, Copilot Studio or any MCP
client:

```json
{ "mcpServers": { "cxnova-orchestration": {
    "type": "http", "url": "<host>/mcp",
    "headers": { "Authorization": "Bearer <key>" } } } }
```

What the assistant then sees (`tools/list`) is exactly your subscription:

- `run_<agent_id>` for each rented agent. The tool's argument schema **is** the agent's contract, so the assistant knows that
  `run_is_analytics_chat` takes `dataset` and `question`, or `run_vai_predictive_propensity` takes `profiles` and `objective`.
- `run_flow_<flow_id>` for each rented flow, with a field per declared input plus `goal`.
- `list_agents`, `list_flows`, `run_flow`, `orchestrate` (a goal; the planner chooses), `get_run`, `approve_actions`.

A run that takes longer than a tool call can wait answers with its `run_id` and says to follow it with `get_run`. A held
action appears in `pending_approvals`; the assistant asks its user and decides it with `approve_actions`. So an analyst can
attach an export, ask "what should we fix first?", and the assistant calls `run_flow_is_executive_review` with the file as
`dataset`, then answers follow-up questions with `run_is_analytics_chat`.

The server follows the protocol's 2026-07-28 revision (clients on earlier revisions are still answered):

- **No session.** Every request stands on its own and carries your key, so the server sits behind any load balancer.
- **Every tool says what calling it does.** `list_agents`, `list_flows` and `get_run` are marked read-only. A `run_...`
  tool is not (a run is metered and kept) and is marked as reaching outside systems when its agent crawls or uses a
  connector. Only `approve_actions` is marked destructive, because an approved action publishes or sends. These are
  hints for the client's own confirmation prompts; what your key may do is decided on the server.
- **The tool list** comes in one stable order, marked private to you (it is your subscription) and fresh for a minute.
- **Results are structured.** Each tool returns its result as structured content as well as text.

## A2A: your orchestrator delegates tasks

Discovery is the agent card; execution is JSON-RPC 2.0.

```bash
curl -H 'Authorization: Bearer <key>' <host>/.well-known/agent-card.json
```

The card describes this layer as one agent whose **skills** are your rented agents and flows (a flow as `flow:<id>`). The
endpoint speaks **A2A 1.0**, the protocol's first stable version. Send the header `A2A-Version: 1.0` with every request,
the card included. To delegate, send `SendMessage` to `<host>/a2a`; a data part names the flow (or agent) and carries the
inputs, a text part carries the goal:

```json
{ "jsonrpc": "2.0", "id": 1, "method": "SendMessage",
  "params": { "message": { "role": "ROLE_USER", "messageId": "m-1", "parts": [
    { "text": "Retain this Platinum passenger after three disruptions" },
    { "data": { "flow": "vai_churn_prevention",
        "inputs": { "passenger": { "name": "Elena Chen", "tier": "Platinum Elite", "hub": "SIN" },
                    "signals": [ { "source": "PSS", "event": "3 disruptions in 28 days", "severity": "high" } ] } } } ] } } }
```

The answer is `{"task": ...}`: `id` (the run), `status.state` (`TASK_STATE_SUBMITTED`, `TASK_STATE_WORKING`,
`TASK_STATE_INPUT_REQUIRED` when an action is held, `TASK_STATE_COMPLETED`, `TASK_STATE_FAILED`), a status message with the
run's summary, and one **artifact per step** with that step's output. Poll it with `GetTask` until it settles; add
`"configuration": {"returnImmediately": true}` to get the task back at once. Without a data part the text is a goal and the
planner chooses among what you rent.

- **Held actions.** `TASK_STATE_INPUT_REQUIRED` lists what is held in `metadata.pendingApprovals`. Decide them with a
  message to the same task: `"taskId": "<id>"` and a data part `{"decisions": {"<step>/<action>": "approve"}}` (`"*"` decides
  all of them; the one word `approve` or `reject` as text does too). The portal, REST and the MCP `approve_actions` tool
  decide them as well. A run takes no other message once it has started.
- **Your tasks.** `ListTasks` pages through your runs, newest first (`status`, `pageSize`, `pageToken`,
  `includeArtifacts`). `GetExtendedAgentCard` returns your own card over the endpoint.
- **What is not offered** is said in the card and refused by name: no streaming and no push notifications
  (`UNSUPPORTED_OPERATION`, `PUSH_NOTIFICATION_NOT_SUPPORTED`), and a run in flight cannot be cancelled.
- **Errors** carry the reason as the protocol defines it, for example `TASK_NOT_FOUND` for a task that is not yours.
- **Your tenant.** Your card's interfaces carry your customer id as `tenant`. A request that names a tenant has to name
  that one.
- **A client that still speaks 0.3** (`message/send`, `tasks/get`, no version header) is answered in 0.3, exactly as
  before, and the card it reads keeps the 0.3 fields beside the new ones.

Not built yet: signed agent cards, streaming, push notifications.

## Two customers, two doors

The operator can create two demo accounts (`caf shop demo`, or *Create the demo customers* on the portal's Customers page)
that show the choice made:

**Harbor Bank** rents IntelliSight (the *Export to decisions* and *Ask the data* flows; the Conversational Analytics, Custom
Report Builder and Decision Layer agents). Its analysts already work in Claude Desktop and Copilot, and they read every
answer, so the bank comes through **MCP**: the assistant gets five typed tools, fills them from the export the analyst
attached, and a held action is a question back to the analyst.

**Xperion Airways** rents the four VoyagerAI journeys, the propensity agent and the IntelliSight executive review. Its
customer-experience orchestrator watches the PSS and the loyalty platform and decides when to act, so the airline comes
through **A2A**: on an event it sends a task, polls it, and takes the artifacts (the offer, the creatives, the activation
plan, a dry run until an airline channel is connected to this layer) into its own channels.

Both are metered identically: every step, with the agent, tokens, cost and outcome, visible on My account and on the
operator's usage view. The door changes how your software talks to the layer, not what it pays or what it may run.

## Your estate: the workspace

The agents are generic. Their instructions speak of "the CMS", "the design system", "the consent platform", and name no
product and no company. What those are for you is your workspace: your sites, the product that plays each role, your
standards and thresholds, saved once and given to every agent you call.

```bash
caf shop workspace <your-id> --template > estate.yaml   # the roles, with a line on what each one is
caf shop workspace <your-id> --set estate.yaml          # save it (YAML or JSON, up to 64 KB)
caf shop workspace <your-id>                            # show what is saved
```

It reaches the agents of your runs only; no other customer's agent is shown it. For a single call, pass facts in the
`context` input instead. What the agents are not told about your estate they treat as unknown and say so in a check,
rather than assuming it. Today the operator saves it for you; saving it from the portal is not built yet.

### Your company profile and your own rules

The content agents (e-mail, press release, LinkedIn, campaign brief, translation and the rest of the marketing blueprint)
write from **skills**: a brand voice, a press record with the approved boilerplate, a translation glossary, a UTM
convention. The ones on the server describe a fictional demo company. Yours go in the `skills` section of your
workspace, each stated as what differs from the server's:

```yaml
skills:
  brand_voice:
    name: Acme Air
    voice: {tone: Brisk and friendly, do: [Lead with the route and the price]}
    banned_phrases: [world-class]
    media_contact: {name: Rina Shah, email: press@acme-air.example}
  press_boilerplate:
    organisation: Acme Air
    headquarters_dateline: Denver, Colorado
    boilerplate: {heading: About Acme Air, text: Acme Air flies 40 routes from Denver.}
  utm_taxonomy:
    channels: {email: {source: braze, medium: email}}
```

- For the four skills that describe a company (`brand_voice`, `press_boilerplate`, `translation_glossary`,
  `semantic_layer`) nothing of the demo company shows through what you leave out: an unstated fact is empty, and the
  agent's own checks say what is missing (no media contact, no forward-looking disclaimer). Their rules stay: headline
  limits, AP abbreviations, the label on a release.
- Any other skill (`utm_taxonomy`, `channel_registry`, `seo_rules`, ...) is the server's with your changes laid over it.
- A skill that is not this layer's, or not its shape, is refused when the workspace is saved, with the reason.
- Until you save your own brand voice or press record, a run of yours uses the demo company's and says so: a warning on
  the result and `demo_profile` in its provenance. It is never passed off as yours.

## Where your data goes

A run's inputs and outputs (an attached export becomes `dataset.csv` kept with the run) stay on the layer's server for the
retention your policy sets. Your policy also sets masking before any model call (e-mail, phone, card and account numbers),
the model your runs use (the layer's, or your own deployment in run mode 2), the sites agents may fetch, and your limits.
Run mode 3 puts this same image in your own cloud when raw data must not leave your estate.
