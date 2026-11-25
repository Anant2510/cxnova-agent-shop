# DocuInsights on a server

How to put this repository on a Windows server so the workbench is reached from anywhere, at
`https://<your server>/docuinsights/`, behind an API key. Twenty minutes the first time, two for an update.

| Step | Where | What |
| --- | --- | --- |
| 1 | Your machine | Push this folder to GitHub |
| 2 | Server | Clone it and run `install.ps1` |
| 3 | Server | `service.ps1`: the Windows service |
| 4 | Server | One rule in the web server, so the public address reaches it |
| 5 | Anywhere | Open the address, enter the key |

## 1. Push to GitHub

```bash
cd <this folder>
git init -b main                         # skip if it is already a repository
git add -A
git status --short | grep -E "[ /]\.env$|^A +data/|\.venv/" && echo "STOP: a secret or data is staged" || echo "clean"
git commit -m "DocuInsights"
git remote add origin https://github.com/<owner>/<repository>.git
git push -u origin main
```

`.gitignore` keeps `.env` (the model key, the API key), `data/` (cases, the knowledge base, the ledger) and `.venv/`
out of git. The second-last line of the block checks that before anything is committed.

## 2. Install on the server

In an elevated PowerShell:

```powershell
cd C:\cxnova
git clone https://github.com/<owner>/<repository>.git
cd <repository>
Set-ExecutionPolicy -Scope Process Bypass -Force
.\deploy\windows\install.ps1
```

The script finds Python 3.10 or later (or installs 3.12 with winget), creates `.venv`, installs DocuInsights, and
writes `.env`:

* `DOCUINSIGHTS_API_KEY`: generated. Everyone who uses the workbench or the API needs it.
* `DOCUINSIGHTS_ROOT_PATH=/docuinsights`, `DOCUINSIGHTS_PORT=8090`, `DOCUINSIGHTS_HOST=127.0.0.1`,
  `DOCUINSIGHTS_DATA_DIR=<this folder>\data`. Another prefix or port: `install.ps1 -Prefix /cases -Port 8095`.
* **The model.** When the CX Nova Agent Orchestration Layer is on the same machine (`C:\cxnova\cxnova-agent-shop`),
  its Claude settings are copied: the same key, endpoint and deployments. No value is printed. Elsewhere, or with
  `-LayerFolder ""`, set `DOCUINSIGHTS_MODEL_PROVIDER` and its key in `.env` yourself (`.env.example` lists them).

It ends by running a bundled sample without a model and asking the configured model once: `ONLINE`, or the reason it
is not. Without a model DocuInsights still runs, on the use case's patterns and rules, and says so on every screen.

## 3. Run it as a service

```powershell
.\deploy\windows\service.ps1
```

With [NSSM](https://nssm.cc) on the machine this creates the Windows service `CXNovaDocuInsights` with rotating logs
in `logs\service.log`; without it, a scheduled task of the same name that starts at boot and restarts if it stops.
It listens on `127.0.0.1:8090` only, for the web server of step 4 to forward to. Other commands:
`service.ps1 -Status`, `-Restart` (after a change to `.env`), `-Uninstall`.

On the server itself: <http://127.0.0.1:8090/docuinsights/>.

## 4. The web server rule

One rule: `https://<your server>/docuinsights/*` goes to `http://127.0.0.1:8090/docuinsights/*`, path unchanged.
Put it next to the rules of the other applications on the machine.

**IIS with Application Request Routing**, in the site's `web.config`, inside `<rewrite><rules>`:

```xml
<rule name="DocuInsights" stopProcessing="true">
  <match url="^docuinsights(/.*)?$" />
  <serverVariables>
    <set name="HTTP_X_FORWARDED_PROTO" value="https" />
    <set name="HTTP_X_FORWARDED_HOST" value="{HTTP_HOST}" />
  </serverVariables>
  <action type="Rewrite" url="http://127.0.0.1:8090/docuinsights{R:1}" />
</rule>
```

(`HTTP_X_FORWARDED_PROTO` and `HTTP_X_FORWARDED_HOST` must be in URL Rewrite's *Allowed Server Variables*; a site
that already proxies another application has them.) Documents are uploaded inside the request, so allow 60 MB:
`<requestLimits maxAllowedContentLength="62914560" />` under `<security><requestFiltering>`.

**nginx**

```nginx
location /docuinsights/ {
    proxy_pass http://127.0.0.1:8090/docuinsights/;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $remote_addr;
    client_max_body_size 60m;
}
```

**Caddy**

```
handle /docuinsights/* {
    reverse_proxy 127.0.0.1:8090
}
```

Check it from any machine: `https://<your server>/docuinsights/health` answers
`{"status": "ok", ..., "api_key": "set"}`. That address is public and says nothing else.

## 5. Open it

`https://<your server>/docuinsights/`. The workbench asks for the API key once per browser session: the value of
`DOCUINSIGHTS_API_KEY` in `.env` on the server. Give the address and the key to whoever should use it.

To choose the key yourself, or to change it (the old key stops working at once):

```powershell
(Get-Content .env) -replace '^DOCUINSIGHTS_API_KEY=.*$', 'DOCUINSIGHTS_API_KEY=<your key>' | Set-Content .env
.\deploy\windows\service.ps1 -Restart
```

Another system calls the same server with the key as a bearer token:
`POST https://<your server>/docuinsights/api/v1/cases` (see `DOCUINSIGHTS.md`).

## What the key does and does not do

* Without a key the API answers callers on the server itself only. A request that arrives through the web server is
  never treated as local, so the workbench is not open by accident when the rule of step 4 is added first.
* One key, one workspace: everyone who has the key sees the same cases and the same knowledge graph. Separate
  workspaces per customer, each with its own key, are what the CX Nova Agent Orchestration Layer adds when
  DocuInsights runs there as an agent pack.
* The key travels with every request. Serve it over HTTPS only.
* An address that sends 10 wrong keys inside ten minutes is refused until they age out, whatever it sends next
  (`DOCUINSIGHTS_KEY_ATTEMPTS`; 0 switches it off). That slows guessing; it does not make a short key a strong one.
  Whoever has the key can run assessments on your model and read every case in the workspace.
* The workbench sends this key and nothing else. A token another application on the same address left in the browser
  (the CX Nova portal's, for one) is never sent here.

## Updating

Push the change to GitHub, then on the server:

```powershell
cd C:\cxnova\<repository>
.\deploy\windows\update.ps1          # git pull, reinstall what it needs, restart, status
```

A knowledge graph built by an earlier version is rebuilt from its sources the first time it is used.

## Where things are

| | |
| --- | --- |
| Settings and secrets | `.env` (never committed) |
| Cases, document versions, the knowledge base, the ledger | `data\` (never committed; back it up with the server) |
| Logs | `logs\service.log` |
| Use cases | `src\docuinsights\profiles\*.yaml`, or your own folder in `DOCUINSIGHTS_PROFILES_DIR` |
| Agent contracts | `src\docuinsights\foundation\contracts.yaml` |
