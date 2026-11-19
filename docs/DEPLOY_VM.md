# Deploying the CX Nova Agent Orchestration Layer on the CX Nova VM

The layer runs as one Windows service on the CX Nova VM (`cxnova2.centralindia.cloudapp.azure.com`), next to
Campaign Studio and Foresight Studio, from a clone of this repository, the same way Campaign Studio got there.
Forty minutes the first time, five for an update.

| Step | Where | What |
| --- | --- | --- |
| 1 | Your Mac | Push the repository to GitHub |
| 2 | VM | Clone it to `C:\cxnova\cxnova-agent-shop` and run `install.ps1` |
| 3 | VM | Put the Foundry key and the public address in `.env` |
| 4 | VM | `check.ps1`: all 28 agents live on Foundry |
| 5 | VM | `service.ps1`: the Windows service, the firewall port |
| 6 | VM | Reverse proxy (HTTPS, path prefix) — `deploy/proxy/README.md` |
| 7 | Anywhere | First customer: account, subscription, key, policy, agent check |
| 8 | Anywhere | Design a flow on the canvas, publish it, rent it |
| 9 | VM | Agent packs: Campaign Studio and ARM SDLC agents |

## 1. Push to GitHub (Mac)

```bash
cd ~/CXNova-Agentic-Factory
git init -b main                         # skip if it is already a repository
git add -A
git status | grep -E "\.env$|data/|runs/" && echo "STOP: secrets or run data staged" || true
git commit -m "CX Nova Agent Orchestration Layer"
git remote add origin https://github.com/<org>/cxnova-agent-shop.git
git push -u origin main
```

`.gitignore` keeps `.env`, `data/`, `runs/`, `.venv/` and the zip snapshots out. `console/dist/` (the built
console) is committed on purpose, so the VM needs no Node.js: install and update use it as committed and never
rebuild it on their own (`-RebuildConsole` does, when Node.js is there). Flows published in the admin console
reach the console's playbook list without a rebuild.

## 2. Install on the VM

In an elevated PowerShell on the VM:

```powershell
cd C:\cxnova
git clone https://github.com/<org>/cxnova-agent-shop.git
cd cxnova-agent-shop
Set-ExecutionPolicy -Scope Process Bypass -Force
.\deploy\windows\install.ps1
```

The script finds Python 3.11+ (or installs 3.12 with winget), creates `.venv`, installs the framework with the
Claude, MCP and browser extras, installs Chromium for Playwright, creates `.env` with a generated operator token
and vault key, and finishes with the offline health check: `28/28 agents live`.

## 3. Configure `.env`

Open `C:\cxnova\cxnova-agent-shop\.env` and set:

```
CAF_LLM_PROVIDER=anthropic_foundry
ANTHROPIC_FOUNDRY_API_KEY=<the Foundry key, the one Campaign Studio uses>
ANTHROPIC_FOUNDRY_BASE_URL=https://dxp-ai-6530-resource.services.ai.azure.com/anthropic/
CAF_PUBLIC_URL=https://cxnova2.centralindia.cloudapp.azure.com/agentshop
CAF_ROOT_PATH=/agentshop                 # only when served under that prefix (step 6); empty for http://host:8080
CAF_HOST=0.0.0.0
CAF_PORT=8080
```

`CAF_API_TOKEN` (already generated) is the operator token: it opens the admin console, the designer and the full
API. Keep it with the other VM secrets; customers never get it.

## 4. Prove every agent on Foundry

```powershell
.\deploy\windows\check.ps1
```

This runs `caf health --live`: the key check first, then all 28 agents from their self-tests on their routed
models (Sonnet 5.5 for fast and balanced work, Opus 5 for analysis, experiments and code). Expect `28/28 agents
live` in a few minutes. `-Fast` runs everything on Sonnet, cheaper and quicker, when you only want to confirm the
key and the wiring.

`check.ps1` proves the 28 blueprint agents. The 69 pack agents (step 9) are probed separately, because a live probe
is one model call per agent:

```powershell
.venv\Scripts\caf health --packs                 # all 97 registered and answering, no model call
.venv\Scripts\caf health --packs --live --only cs_xtender_variants,arm_front_velocity   # a few on the real model
.venv\Scripts\caf health --packs --live          # every pack agent on the real model (69 calls)
```

## 5. Run it as a service

```powershell
.\deploy\windows\service.ps1
```

With [NSSM](https://nssm.cc) on the machine (`nssm.exe` on PATH or in `C:\nssm`) this creates the Windows service
`CXNovaAgentShop` with rotating logs in `logs\service.log`; without it, a scheduled task of the same name that
starts at boot under SYSTEM and restarts if it stops. The script also opens TCP 8080 in Windows Firewall. Open the
same port in the VM's Azure network security group only if people will connect without the proxy of step 6.

Then, on the VM: <http://127.0.0.1:8080/shop>. Other commands: `service.ps1 -Status`, `-Restart`, `-Uninstall`.

## 6. HTTPS through the VM's web server

Follow `deploy/proxy/README.md` for the server the VM uses (IIS with ARR, nginx or Caddy). The pattern is one
rule: `https://cxnova2.centralindia.cloudapp.azure.com/agentshop/*` → `http://127.0.0.1:8080/agentshop/*`, with
`CAF_ROOT_PATH=/agentshop` in `.env`. Orchestrated runs no longer hold the HTTP request: a call answers within
`CAF_RUN_WAIT_S` (60 s by default) and a longer run answers 202 and is followed with `GET /runs/{id}`, so keep the proxy
time-out above `CAF_RUN_WAIT_S` (the 600 s in the proxy examples leaves plenty of room).

Public addresses once that is in place:

| What | Address |
| --- | --- |
| Storefront | `https://cxnova2.centralindia.cloudapp.azure.com/agentshop/shop` |
| Agent console | `.../agentshop/console` |
| Customer account | `.../agentshop/shop/account` |
| Orchestration designer | `.../agentshop/shop/designer` (operator token) |
| Runs (live, step by step) | `.../agentshop/shop/runs` (operator token: every run; customer key: its own) |
| Admin | `.../agentshop/shop/admin` (operator token) |
| API docs | `.../agentshop/docs` |
| MCP | `.../agentshop/mcp` |
| A2A card | `.../agentshop/.well-known/agent-card.json` |

## 7. The first customer

From the VM (the CLI needs no token) or from the admin console:

```powershell
.venv\Scripts\caf shop add meridian-mutual "Meridian Mutual" --contact marketing-ops@meridian-mutual.example
.venv\Scripts\caf shop subscribe meridian-mutual --flows website_audit,campaign_launch --agents utm_generation
.venv\Scripts\caf shop key meridian-mutual --label "marketing ops"      # the key is shown once
.venv\Scripts\caf shop policy meridian-mutual --file config\policies\example-customer.yaml   # optional: its own model
.venv\Scripts\caf shop check meridian-mutual                             # every subscribed agent on its model
.venv\Scripts\caf shop usage meridian-mutual
```

## 8. Designing and renting a flow

Open the **Designer** (`.../agentshop/shop/designer`, or Admin → Flows → **New flow in the designer**); it asks
for the operator token once per browser.

1. **Add agents.** The palette on the left lists all 97 agents by pack. Click one to add it after the selected
   step (clicking down a list builds a chain), or drag it onto the canvas to place it unconnected.
2. **Connect.** Drag from the port on a node's right edge to another node: the second runs after the first and
   sees its output. Steps with no wire between them run in parallel. Click a wire to remove it.
3. **Set each step.** Select a node: the inspector has its name, the action (for pack agents), its purpose,
   "Runs after", and the inputs, with suggestions for `$goal`, `${input.<name>}` and `${steps.<step>.output...}`.
4. **Flow settings** (nothing selected): name, description, the shared inputs a customer sends, price.
5. **Check**, then **Test run** (sample inputs, on this server's model; each node shows its result), then
   **Publish**.

The flow is now a storefront card with its own page (`.../agentshop/shop/flows/<id>`), a playbook in the console,
the MCP tool `run_flow_<id>`, the endpoint `POST /flows/<id>/run` and the A2A skill `flow:<id>`. Rent it to a
customer on its Subscriptions tab; renting the flow covers every agent in it for runs of the flow. Built-in flows
(the 8 playbooks, the Campaign Studio stages, the ARM SDLC stages) open read-only: **Clone** makes a copy you can
change. A plan made in the console opens here through "Save plan as flow". The same from the CLI:

```powershell
.venv\Scripts\caf shop flows list
.venv\Scripts\caf shop flows clone website_audit audit_lite      # a built-in flow as an editable draft (YAML via "flows show")
.venv\Scripts\caf shop flows save my_flow.yaml --publish
.venv\Scripts\caf shop flows test my_flow
.venv\Scripts\caf shop subscribe meridian-mutual --flows my_flow --add
```

Send the customer its key and the address of `/shop/account`: the page shows its agents, the REST, MCP and A2A
connection details, its policy, its usage and the last agent check. With the key as a bearer token the customer
sees and runs only what it rents, on REST, MCP and A2A alike.

## 9. Agent packs: Campaign Studio and ARM SDLC

Two packs ship in `config\packs\` and load with the service: Campaign Studio (18 agents, ids `cs_...`) and the
ARM SDLC Factory (51 agents, ids `arm_...`). They need no installation. What each agent run does is decided per
run and reported as `runner` in the result:

| Runner | When | What happens |
| --- | --- | --- |
| `home` | Campaign Studio agents, operator runs, studio connected, `campaign_id` given | The studio runs its own agent on that campaign and the result comes back |
| `model` | Otherwise, with a model configured | This layer's model does the work from the pack's skill prompt and standards |
| `twin` | No model (offline) | The contract's sample output, marked as a sample |

To let Campaign Studio's agents run in the studio itself, add its local address and passcode to `.env` and
restart (`service.ps1 -Restart`). Campaign Studio listens on port 8787 unless its own `.env` sets another `PORT`:

```
CAF_PACK_CAMPAIGN_STUDIO_URL=http://127.0.0.1:8787
CAF_PACK_CAMPAIGN_STUDIO_PASSCODE=<the studio passcode>
```

Admin → Flows ("Where the agents come from") shows for each pack whether its home product is connected or its
agents are modelled here. A customer's run never uses the home
runner: customers get the modelled agent on the model their policy names. Actions that write to another system
(publish, send, platform changes) come back as a dry run with the step a person has to take, unless the home
product served them. The ARM SDLC Factory has no run-one-agent API, so its agents are always modelled here.

The ARM pack is generated from the factory's own configuration. After the factory's agents, skills or process
change, regenerate it on a machine that has the factory's repository, commit the two files and update the VM:

```
python3 scripts/build_pack_arm.py --from <arm-sdlc-factory>/config
```

`CAF_PACKS_DISABLED=arm_sdlc` (ids separated by commas, or `*`) in `.env` leaves a pack out. The ARM pack carries
client-specific prompts and sample outputs: keep the GitHub repository private.

## Updating

```powershell
cd C:\cxnova\cxnova-agent-shop
.\deploy\windows\update.ps1         # restores the committed console build, git pull, dependencies, service restart
```

After a restart (an update, a reboot, the service recovering), finished runs and runs waiting for approval are as
they were. A run that was cut off mid-way reports `running` for up to a minute (until the stopped process's heartbeat
is stale), then `stalled` in `GET /runs/{id}`; `POST /runs/{id}/continue` picks it up from its last checkpoint and does
not run its finished steps again.

## Where things are

| | |
| --- | --- |
| Settings and secrets | `.env` (never committed) |
| Customers, keys, subscriptions, usage | `data\shop.db` (SQLite; back it up with the VM) |
| Run state: checkpoints, status, approval decisions | `data\orchestrator.db` (SQLite; runs survive a service restart, see below) |
| Run artifacts and traces | `runs\<run-id>\` |
| Service logs | `logs\service.log` |
| Model routing (which model does what) | `config\model_routing.yaml`, `caf models` |
| Prices and bundle names on the storefront | `config\shop_catalog.yaml` |
| Customer policy template | `config\policies\example-customer.yaml` |
| Agent packs | `config\packs\<pack>.yaml` (+ `<pack>.contracts.json`) |

## Docker instead

The same image runs anywhere with Docker: `docker compose up -d` with `.env` beside it (see `docker-compose.yml`,
which mounts `runs/` and `data/`). Azure Container Apps or App Service take the image as is; set the same
variables as in `.env`.
