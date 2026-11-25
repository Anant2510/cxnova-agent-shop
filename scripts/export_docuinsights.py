"""Writes DocuInsights as a project of its own: the accelerator without the CX Nova layer.

    python scripts/export_docuinsights.py dist/docuinsights
    python scripts/export_docuinsights.py ~/DocuInsights --update     an existing export, brought up to date in place

The folder holds the package (src/docuinsights, copied as it is here), its own pyproject.toml, a README, the engine's
tests, a settings template (.env.example) and start scripts for macOS/Linux and Windows. Push it to its own repository, or copy it to a server and run it:

    pip install -e ".[server,pdf]"
    python -m docuinsights serve --port 8090

This repository stays the source: change the accelerator here and export again.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PYPROJECT = '''[build-system]
requires = ["setuptools>=77", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "docuinsights"
version = "{version}"
description = "DocuInsights: agentic document and case intelligence. Classify and read a case package, build a case knowledge graph, check evidence, rules and consistency against a use-case profile, and give a caseworker a verified, traceable recommendation."
readme = "README.md"
requires-python = ">=3.10"
license = "LicenseRef-Proprietary"
dependencies = ["pyyaml>=6", "certifi>=2024.2.2"]

[project.optional-dependencies]
server = ["starlette>=0.37", "uvicorn>=0.29"]
pdf = ["pdfplumber>=0.11"]
ocr = ["pytesseract>=0.3", "pillow>=10"]
dev = ["pytest>=8", "httpx>=0.27", "reportlab>=4"]

[project.scripts]
docuinsights = "docuinsights.__main__:main"

[tool.setuptools.packages.find]
where = ["src"]

[tool.setuptools.package-data]
docuinsights = ["profiles/*.yaml", "foundation/*.yaml", "foundation/knowledge/*.yaml", "reference/*.json", "web/*", "samples/*/*"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q"
'''

README = '''# DocuInsights

Give it the documents. Get back a recommendation you can check.

A case is a handful of documents and a few facts about it. Agents read the documents, lift their facts with the page
and line each came from, and link the same company or reference number across them. Rules that are code, never a
model's opinion, check that nothing is missing, that the dates and amounts hold and that the documents agree. A person
gets one of three recommendations, and every reason opens the line it rests on.

Every case is also checked three ways that do not depend on the use case: **integrity** (does each document hold
together on its own: its sums, its dates, the check digits of its account numbers, the same file twice, bank details
that changed since last time), **screening** (is any party, vessel or port on a watchlist) and **privacy** (what
personal data the package carries, masked in whatever leaves the system). It then lands in a **work queue** with a
priority and a due date, and leaves as a **decision record** that can be printed.

It names no customer and no industry. A use case is one configuration file (`src/docuinsights/profiles/*.yaml`): the
kinds of document, the evidence it asks for, its rules. Five ship, each with invented sample cases:

| Use case | Who runs it | What it checks |
| --- | --- | --- |
| `supplier_invoice` | Finance operations | an invoice against its purchase order and proof of receipt, and whether it was seen before |
| `insurance_claim` | Insurance | a claim against its policy: cover dates, notification, the amount, the names |
| `loan_application` | Banking and lending | what an applicant declared against the payslip, the statement and the employer's letter |
| `customs_claim` | Customs and trade | evidence, consistency, and a commodity code read from the tariff for the case's date |
| `trade_finance` | Trade finance | documents presented under a documentary credit against the credit's terms |

## What every agent runs on

| | What it is | Where |
| --- | --- | --- |
| Agent contracts | what each agent needs, promises, may change, may spend and must never do | `foundation/contracts.yaml`, `contracts.py` |
| Agent harness | the one place every agent runs; it enforces the contract and records the run | `harness.py` |
| Shared memory | the run's notes, the case, and what the workspace has learned from earlier cases | `memory.py` |
| Knowledge graph | one graph per workspace in the KB Forge knowledge-base layout: what each industry works under, what each use case asks for, what every case taught. Every finding opens the exact node its rule rests on | `kb.py`, `foundation/ontology.yaml`, `foundation/knowledge/*.yaml` |
| Evidence provenance | every conclusion to a line of a document; every action in a chained ledger | `ledger.py` |
| Throttling | rate limits and per-agent budgets on model calls; a gate on the API | `throttle.py` |

Each is described in [DOCUINSIGHTS.md](DOCUINSIGHTS.md), with what has not been verified against a live system.

## Run it

```bash
./start.sh                       # macOS, Linux: makes .venv on the first run, then serves http://127.0.0.1:8090/
.\\start.ps1                      # Windows
```

or by hand:

```bash
python3 -m venv .venv && . .venv/bin/activate        # Windows: .venv\\Scripts\\activate
pip install -e ".[server,pdf]"
python -m docuinsights samples
python -m docuinsights assess --profile supplier_invoice --sample mismatch
python -m docuinsights serve --port 8090
```

Open the workbench and pick a use case on the home page: **Try it** on a sample runs it end to end. The top of the page
has six screens: Home, Cases (the work queue: priority, queue, due date, who has it), Insights (how many cases go
straight through, how many need a person and why, how long a decision takes; counted from the case files), Knowledge
(everything known, explored as in KB Forge: four levels, industry, domain, solution, customer), Agents (their
contracts) and How it is governed (contracts, memory, provenance, throttling, watchlists as they stand).
Inside a case: summary, documents, evidence, consistency, **screening, integrity and privacy**, rules, the graph,
provenance, the what-if simulator, the decision with its printable record, and **Agent orchestration**: one board
that shows the boss agent's plan, each agent run under its contract, and what every step took from the knowledge
graph and the shared memory. It follows a running assessment and plays a finished one back.

Documents can be PDF, Word, Excel, an email with its attachments, a web page, text, CSV or a scan.

## The knowledge base and KB Forge

One knowledge graph per workspace, kept in the KB Forge knowledge-base layout (`data/default/knowledge/`). It starts
with six curated knowledge bases (`src/docuinsights/foundation/knowledge/*.yaml`): five industries (finance
operations, insurance, banking and lending, customs and trade, trade finance) with their standards and articles,
processes, risks, controls, measures and terms, and the domain itself. Every rule of every use case is linked to the
article, control or term it rests on, and every assessed case adds to the graph.

* **Explore it as in KB Forge.** *Knowledge > Explore the graph* draws the graph the way KB Forge's Explore screen
  draws its level map: the levels as boxes on a spine, entities as circles in their level's colour with +N for what
  is still to open, a double-click to drill down into a tidy tree, Force / Layered / Radial layouts, the level bar,
  the trail, Scene & filters on the left and the inspector on the right.
* **Follow a finding to its reason.** A finding card names what its rule rests on; a click opens that exact node in
  the explorer, with the way down to it drawn, what it says, how it is cited and every case it was used on. Each node
  has a link of its own (`#/knowledge/<id>`) to send to someone.
* **Manage it.** *Knowledge > Manage* switches a curated knowledge base off and on, moves an added one to another
  level, and lists what was added by hand. To add a fact, open the node it belongs under and choose *Add under this*.
* **Add your own.** Put YAML files like the shipped ones in a folder and name it in `DOCUINSIGHTS_KNOWLEDGE_DIR`.

What the curated knowledge bases say is a short summary with its reference, written for this accelerator. It is not
the text of a standard: read the current text before relying on it.

To open the graph in KB Forge: **Knowledge > Download for KB Forge**, then in KB Forge
*Import a graph*. To bring knowledge the other way: **Knowledge > Add a knowledge base** takes a KB Forge
knowledge-base zip, a KB Forge graph page (.html) or a JSON file of nodes and edges. From the command line:

```bash
python -m docuinsights knowledge show
python -m docuinsights knowledge import ~/KBGraph/knowledge_bases/<a knowledge base folder>
python -m docuinsights knowledge export knowledge.zip
```

## Settings

Copy `.env.example` to `.env`. Without a model it runs on the profile's patterns and rules and says so on every
screen. To read with Claude set `DOCUINSIGHTS_MODEL_PROVIDER` to `anthropic` (with `ANTHROPIC_API_KEY`) or
`anthropic_foundry` (with `ANTHROPIC_FOUNDRY_API_KEY` and `ANTHROPIC_FOUNDRY_BASE_URL`). The model tiers use the same
names as the CX Nova layer (`FOUNDRY_DEPLOYMENT_FAST`, `_BALANCED` ...), so one set of lines serves both. The server
calls the model once at start and prints `ONLINE` or the reason it is not; `python -m docuinsights check` does the
same on its own. Set `DOCUINSIGHTS_API_KEY` before exposing the server beyond the machine it runs on; without a key
the API answers 127.0.0.1 only.

Behind a reverse proxy under a prefix: `./start.sh --root-path /docuinsights` and forward `/docuinsights/` to the port.

**Watchlists.** Out of the box screening runs against an invented demonstration list, and says so. Name your own
lists in `DOCUINSIGHTS_WATCHLISTS` (JSON or CSV files, or folders of them) and the invented one is no longer used.

**Telling another system.** Set `DOCUINSIGHTS_WEBHOOK_URL` and a small signed message is sent when a case is assessed
or decided (`DOCUINSIGHTS_WEBHOOK_SECRET` signs it). Personal data in it is masked.

## On a server

[DEPLOY.md](DEPLOY.md) puts it on a Windows server as a service behind the machine's web server, reached from
anywhere at `https://<your server>/docuinsights/` with an API key: push this folder to GitHub, clone it on the server,
`deploy/windows/install.ps1`, `deploy/windows/service.ps1`, one web server rule. `deploy/windows/update.ps1` brings a
running installation up to date.

## Use it from another system

`POST /api/v1/cases` with `{reference, profile, fields, documents: [{name, category, content_base64}], wait: true}`
returns the assessment as JSON: outcome, reasons, findings, triage, watchlist matches, personal data by kind.
`GET /api/v1/cases/{reference}/record` is the decision record (`?mask=1` masks personal data), `GET /api/v1/insights`
the workspace's numbers, `GET /api/export/cases.csv` the work queue. `DOCUINSIGHTS_REPOSITORY=omnidocs` files assessed cases to NewgenONE OmniDocs.
Both, and what has not been verified against a live system, are described in [DOCUINSIGHTS.md](DOCUINSIGHTS.md).

## Tests

```bash
pip install -e ".[dev,server,pdf]"
python -m pytest
```

`tests/test_docuinsights_engine.py` proves the engine on the bundled cases. `tests/test_docuinsights_scenarios.py`
puts it through what an installation meets in practice (documents missing, blank, corrupt, filed twice or mislabelled;
other countries' number and date formats; other alphabets; hostile text; many cases at once; an API called wrongly)
and checks the same invariants after each. [DOCUINSIGHTS.md](DOCUINSIGHTS.md) lists the scenarios and what is not
covered.

## One package, two homes

`src/docuinsights` is the same package the CX Nova Agent Orchestration Layer carries as its DocuInsights agent pack.
This folder is written by the layer repository's `scripts/export_docuinsights.py`. Change the accelerator in one of
the two and carry `src/docuinsights` (and the two `tests/test_docuinsights_*.py` files) across, so the copies do not drift.
'''

START_SH = '''#!/usr/bin/env bash
# Starts DocuInsights on this machine (macOS, Linux). The first run makes .venv and installs into it.
# Extra arguments go to the server:  ./start.sh --port 8091    ./start.sh --root-path /docuinsights
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then python3 -m venv .venv; fi
if [ ! -f .venv/.installed ] || [ pyproject.toml -nt .venv/.installed ]; then   # first run, or the requirements changed
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install --quiet -e ".[server,pdf]"
  touch .venv/.installed
fi
export DOCUINSIGHTS_DATA_DIR="${DOCUINSIGHTS_DATA_DIR:-$PWD/data}"
exec .venv/bin/python -m docuinsights serve "$@"
'''

ENV_EXAMPLE = '''# DocuInsights settings. Copy this file to .env (read when the server starts) or set the same names in the environment.
# Nothing here is needed to run the samples: without a model it reads with the profile's patterns and rules.

# --- the model that reads scans, names documents, lifts fields and writes answers (rules and the outcome are always code)
# DOCUINSIGHTS_MODEL_PROVIDER=offline            # offline | anthropic | anthropic_foundry
# ANTHROPIC_API_KEY=                             # anthropic
# ANTHROPIC_FOUNDRY_API_KEY=                     # anthropic_foundry (Claude on Microsoft Foundry)
# ANTHROPIC_FOUNDRY_BASE_URL=https://<resource>.services.ai.azure.com/anthropic/
# A step asks for a tier, as on the CX Nova layer, and the same names resolve it here:
# FOUNDRY_DEPLOYMENT_FAST=claude-sonnet-5-5      # classify
# FOUNDRY_DEPLOYMENT_BALANCED=claude-sonnet-5-5  # read scans, extract, answer, draft
# FOUNDRY_DEPLOYMENT_PRO=claude-opus-5
# FOUNDRY_DEPLOYMENT_COMPLEX=claude-opus-5
# DOCUINSIGHTS_MODEL=                            # one model or deployment for every tier, overriding the lines above
# Check it:  .venv/bin/python -m docuinsights check

# --- serving
# DOCUINSIGHTS_API_KEY=                          # set it before exposing the server: every /api call must then carry it
# DOCUINSIGHTS_DATA_DIR=./data                   # cases, document versions, the feedback vocabulary
# DOCUINSIGHTS_ROOT_PATH=/docuinsights           # when it sits behind a reverse proxy under a prefix
# DOCUINSIGHTS_HOST=127.0.0.1
# DOCUINSIGHTS_PORT=8090

# --- throttling (0 switches a limit off)
# DOCUINSIGHTS_MODEL_RPM=60                      # model calls a minute, shared by every agent
# DOCUINSIGHTS_MODEL_TPM=0                       # model tokens a minute
# DOCUINSIGHTS_MODEL_CONCURRENCY=4               # model calls in flight at once
# DOCUINSIGHTS_MODEL_MAX_WAIT_S=20               # a call waits this long for a slot; past it the agent falls back to patterns
# DOCUINSIGHTS_API_RPM=600                       # API requests a minute per caller (429 past it)
# DOCUINSIGHTS_KEY_ATTEMPTS=10                   # wrong API keys an address may send inside ten minutes before it has to wait
# DOCUINSIGHTS_MAX_ASSESSMENTS=2                 # assessments running at once; the rest queue

# --- knowledge base
# DOCUINSIGHTS_KNOWLEDGE_NAME=Workspace knowledge  # the name the knowledge base carries when it is exported
# DOCUINSIGHTS_WORKSPACE_NAME=This workspace       # the name of the customer level in the knowledge graph
# DOCUINSIGHTS_KNOWLEDGE_DIR=./knowledge           # a folder of your own knowledge bases (YAML, as in src/docuinsights/foundation/knowledge)

# --- screening and notifications
# DOCUINSIGHTS_WATCHLISTS=./watchlists           # your own watchlists: JSON or CSV files, or folders of them. The invented demonstration list is then not used
# DOCUINSIGHTS_WATCHLIST_DEMO=0                  # 0: no demonstration list even without lists of your own (screening then says it could not be done)
# DOCUINSIGHTS_WEBHOOK_URL=https://<your system>/hook   # told when a case is assessed or decided (personal data masked)
# DOCUINSIGHTS_WEBHOOK_SECRET=                   # signs the message: X-DocuInsights-Signature: sha256=<hmac of the body>

# --- reference sources
# DOCUINSIGHTS_REFERENCE_LIVE=1                  # read references live; otherwise the bundled snapshot is used and labelled

# --- content repository
# DOCUINSIGHTS_REPOSITORY=local                  # local | omnidocs
# OMNIDOCS_URL=https://<server>:<port>
# OMNIDOCS_CABINET=
# OMNIDOCS_USER=
# OMNIDOCS_PASSWORD=
# OMNIDOCS_FOLDER_INDEX=
# OMNIDOCS_VOLUME_ID=1
# OMNIDOCS_DATACLASS_INDEX=
# OMNIDOCS_DATACLASS_NAME=
# OMNIDOCS_FIELD_MAP={"case_reference": {"indexId": "101", "indexType": "S"}}

# --- your own use cases
# DOCUINSIGHTS_PROFILES_DIR=./profiles           # a folder of extra profile files (a profile there replaces a shipped one of the same id)
'''

START_PS1 = '''# Starts DocuInsights on this machine. Behind a reverse proxy at /docuinsights add: --root-path /docuinsights
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (-not (Test-Path .venv)) { python -m venv .venv }
$stamp = ".venv\\.installed"
if (-not (Test-Path $stamp) -or ((Get-Item pyproject.toml).LastWriteTime -gt (Get-Item $stamp).LastWriteTime)) {   # first run, or the requirements changed
  & .\\.venv\\Scripts\\python -m pip install -e ".[server,pdf]" --quiet
  Set-Content $stamp (Get-Date)
}
if (-not $env:DOCUINSIGHTS_DATA_DIR) { $env:DOCUINSIGHTS_DATA_DIR = Join-Path $PSScriptRoot "data" }
& .\\.venv\\Scripts\\python -m docuinsights serve --host 127.0.0.1 --port 8090 @args
'''


def main(target: str, update: bool = False) -> None:
    """`update`: write over an earlier export in place. Files are replaced or added, never removed; .env, .venv and data are not touched."""
    out = Path(target).expanduser().resolve()
    occupied = out.exists() and any(p.name not in (".DS_Store", ".git", ".gitignore") for p in out.iterdir())
    if occupied and not update:
        raise SystemExit(f"{out} is not empty: choose a new or empty folder, or pass --update to bring an earlier export up to date")
    if update and occupied and not (out / "src" / "docuinsights").is_dir():
        raise SystemExit(f"{out} does not hold a DocuInsights export: nothing to update")
    sys.path.insert(0, str(ROOT / "src"))
    from docuinsights import __version__

    (out / "src").mkdir(parents=True, exist_ok=True)
    shutil.copytree(ROOT / "src" / "docuinsights", out / "src" / "docuinsights", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"), copy_function=shutil.copyfile,
                    dirs_exist_ok=True)
    (out / "tests").mkdir(exist_ok=True)
    for name in ("test_docuinsights_engine.py", "test_docuinsights_scenarios.py"):
        shutil.copyfile(ROOT / "tests" / name, out / "tests" / name)
    (out / "scripts").mkdir(exist_ok=True)
    shutil.copyfile(ROOT / "scripts" / "build_docuinsights_samples.py", out / "scripts" / "build_docuinsights_samples.py")
    shutil.copyfile(ROOT / "docs" / "DOCUINSIGHTS.md", out / "DOCUINSIGHTS.md")
    (out / "pyproject.toml").write_text(PYPROJECT.format(version=__version__), encoding="utf-8")
    (out / "README.md").write_text(README, encoding="utf-8")
    (out / "start.ps1").write_text(START_PS1, encoding="utf-8")
    (out / "start.sh").write_text(START_SH, encoding="utf-8")
    try:
        (out / "start.sh").chmod(0o755)
    except OSError:
        pass   # some mounts refuse a mode change: `bash start.sh` still works
    (out / ".env.example").write_text(ENV_EXAMPLE, encoding="utf-8")
    kit = ROOT / "deploy" / "docuinsights"   # what puts the standalone on a server: the scripts and the steps
    (out / "deploy" / "windows").mkdir(parents=True, exist_ok=True)
    for script in sorted((kit / "windows").glob("*.ps1")):
        shutil.copyfile(script, out / "deploy" / "windows" / script.name)
    shutil.copyfile(kit / "DEPLOY.md", out / "DEPLOY.md")
    (out / ".gitignore").write_text(".venv/\n__pycache__/\n*.pyc\n.env\n*.env.local\ndata/\ndocuinsights-data/\nlogs/\n*.egg-info/\n.pytest_cache/\n.DS_Store\n", encoding="utf-8")
    (out / ".gitattributes").write_text("src/docuinsights/samples/**/*.pdf binary\nsrc/docuinsights/samples/**/*.png binary\n", encoding="utf-8")
    files = sum(1 for p in out.rglob("*") if p.is_file() and not ({".venv", "data", ".git", "__pycache__"} & set(p.relative_to(out).parts)))
    print(f"DocuInsights {__version__} {'updated in' if update and occupied else 'exported to'} {out} ({files} files)")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--update"]
    main(args[0] if args else "dist/docuinsights", update="--update" in sys.argv[1:])
