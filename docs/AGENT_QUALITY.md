# Agent quality: what every agent's figures rest on

An agent is sold on what it returns. A customer who reads "churn propensity 0.87, confidence 0.996" will act on it, so the
layer has to know, and say, whether that number was calculated from the customer's data, written by a model, or copied from
a sample. This page is the standard every agent is held to, how it is enforced, and where each pack stands against it.

## The standard

**1. Every action says what its figures rest on.** One of five, declared per action as `basis`:

| Basis | Meaning | What the runtime does |
|---|---|---|
| `calculated` | Code computes it from the inputs | Needs `code:` on the agent. A model never stands in for the arithmetic when `code_only: true` |
| `authored` | A model writes it from what it is given: copy, a plan, a specification | Normal model run. Figures inside it are part of the writing, not measurements |
| `judgement` | A model's assessment: a score, a rating, a forecast | The model is told to say what each rests on; the result carries a check saying the figures are an assessment |
| `measured` | An observation of a system: results, history, a scan | A model may only repeat figures found in its inputs. With none, the result says "not measured" and is degraded |
| `executed` | Something is run or changed outside this layer: a test run, a send, a deploy | A dry run unless the platform did it: the plan, with no result figures, flagged `dry_run` |

Every result reports the basis in force in `provenance.basis`, and the portal shows it next to the status.

**2. A calculation has code behind it, and nothing stands in for it.** With `code_only: true`, when the code cannot do the
work the step fails and says what is missing. No model answer and no sample output takes its place.

**3. An agent that reads data declares the least it needs** (`minimum`), and refuses to run on less when it runs on its
own: `cs_measurement` needs a `campaign_id` or `results`; propensity needs an identifier and three parameters. Every
model-run agent refuses when it is given nothing at all. Inside a flow of several steps a missing minimum is tolerated
and noted, because an earlier step may carry the data.

**4. Inputs are data, checked before use.** An input can declare `file: [json, csv, ndjson, txt]` (parsed on arrival; a
file that does not parse fails the step with the reason), `choices`, the fields its records must carry, and `max_chars`.
What a model is shown is cut by whole records, never mid-record, and the result says how much was left out.

**5. Where a model writes and arithmetic can check, code checks** (`verify`): an offer's cost against the guardrail, an
SMS against 160 characters, an activation plan that claims deliveries.

**6. Demo data is never passed off as the caller's.** A run that read the sample data shipped with this layer carries
`provenance.demo_data` and a warning.

**7. An agent's own self-test meets its own minimum**, so a check of the agent exercises it on data.

**8. An agent behaves when it is misused, and when its model misbehaves.** Wrong types and missing fields are refused in
plain words that name the field (no library wording). A field sent as null reads as a field not sent. A model that answers
in prose, with half a reply, in the wrong shape, or not at all never produces an internal error. And when a customer's
model call fails, the step fails and says why: a customer is never handed the sample output in place of work. (The
operator's own runs still get the sample, marked, so a demo or a check keeps going.)

## How it is enforced

```bash
caf agents audit                  # every pack agent against the standard; exit code 1 on a fail
caf agents audit --pack voyagerai --verbose
caf agents audit --json
```

The audit reads what an agent declares. The battery runs it:

```bash
caf agents certify                              # every agent through every trial; exit code 1 on a failed trial
caf agents certify --pack arm_sdlc              # one pack (blueprint for the 28 marketing agents)
caf agents certify --agent email_creation       # one agent
caf agents certify --report docs/AGENT_BATTERY.md   # also write the results as a page
caf agents certify --live --agent email_creation    # its self-test on the server's own model (billed there)
```

Each agent gets the same trials: its contract (described, every input explained, a schema a tool can read), its own
self-test, nothing given, wrong types, nulls, an input it does not have, a very large input, nine ways a model can
misbehave, repeatability for calculating agents, and dry-run honesty for actions that run or change something. No live
model is called, whatever model the server is configured with: the model trials use a scripted one that misbehaves on
purpose, every other trial the offline model that answers from this layer's own samples (`src/caf/certify.py`). `--live`
runs each agent's self-test, and only that, on the server's own model; those calls are billed there, and the report says
which model it was. Run it where the agents run, because an agent can pass on one machine and fail on another that lacks
its tools (a browser, a statistics library).
`tests/test_agent_battery.py` runs it on every pack agent in the build.

Until this was fixed the command line used whatever model the server's `.env` named for every trial that was not one of
the nine scripted ones, so a run on a configured server made several live calls per agent. The test suite never did,
because it forces the offline model.

Two judgements the battery makes, each learnt from a wrong verdict on the QR code agent:

- **The same answer twice means the same work, not the same folder.** Each run keeps its files in its own folder, so a
  result that names its files differs between two runs by the run's id in the paths and by nothing else. That is not a
  different result (`same_result`).
- **Ending `ok` with nothing in the result is not a pass** (`hollow`). On a machine without its QR encoder the agent
  returned `ok` with an empty list of codes, and reported the missing library as a payload that did not fit. It now
  fails and says which: the encoder is not installed, or no code could be generated and why.

`tests/test_agent_quality.py` fails the build when a shipped pack has an action whose sample output carries figures and no
declared basis, a `calculated` action without code, a minimum or evidence that names an input the agent does not have, or
a self-test below its own minimum. A stale line in `config/quality/<pack>.yaml` stops start-up.

Where it is declared:

- a hand-written pack declares `basis`, `minimum`, `code_only`, `verify`, `selftest` and `capabilities` in its own YAML
  (`config/packs/voyagerai.yaml` is the reference);
- a pack generated from another product's configuration is classified in an overlay, `config/quality/<pack>.yaml`
  (`basis`, `evidence`, `minimum`, `selftest`), so regenerating the pack does not lose the classification;
- the runtime is `src/caf/packs/inputs.py` (the input contract) and `serve()` in `src/caf/packs/agent.py`.

## Where each pack stands

Counts are actions by basis, from `caf agents audit`.

| Pack | Agents | Calculated | Authored | Judgement | Measured | Executed | Standing |
|---|---|---|---|---|---|---|---|
| VoyagerAI | 8 | 7 | 3 | 0 | 0 | 1 | Fixed in this change |
| IntelliSight | 5 | 4 | 1 | 0 | 0 | 0 | Already code; minimums declared |
| Campaign Studio | 18 | 0 | 8 | 5 | 3 | 2 | Classified; five data agents have minimums |
| ARM SDLC Factory | 51 | 0 | 61 | 3 | 20 | 33 | Classified; honest dry runs; every agent takes `material` and `evidence` |
| Marketing blueprint | 28 | own code and tests per agent | | | | | Demo-data runs now flagged |

### VoyagerAI: the mistake, and the fix

All eight agents were model-run. The propensity score, its confidence, the drivers, the ingestion latency, the identity
match and the "after" value of the feedback loop were written by a model from a shape example (offline they were the
example itself), while the pack's own context said every figure was "computed or sourced". The inputs were free text.

Now the four data stages (ingestion, modelling, identity stitching, propensity) are code with `code_only: true`: they
type and count events, map them to a schema, join identity fragments and calculate the score, and they refuse when the
data does not carry the work. The four writing stages (offer, creatives, activation, feedback) stay with the model, with
code checking the offer against cost guardrails and each creative against its channel's length; activation is a dry run
with delivery figures removed; the feedback loop leaves the "after" value empty until measured events are given.
The reference for the propensity agent is [`VOYAGERAI_PROPENSITY.md`](VOYAGERAI_PROPENSITY.md).

### IntelliSight

Already computed by code on every row. Declared now: the least each agent runs on (a dataset or a named sample; a
question for the chat), and a self-test on the bundled sample. These agents are not `code_only`: if the code fails for a
reason other than the input, a model stands in and the result is marked as its judgement, not as a calculation.

The bundled sample is synthetic, and it was named after a real airline, with that airline's domains and campaign codes
in every row. It is a fictional airline's now (`dataset_name: xperion`, on a reserved `.example` domain); the old name is
refused with the list of samples.

### Campaign Studio

Inside the running studio these agents are computed by the studio (runner `home`). Modelled on this layer:

- measurement, campaign history and asset reuse are `measured`: they report only figures given in `results`, `history`
  or `library`, and refuse to run alone without those or a campaign id;
- forecasts, synthetic-persona scores and the compliance and brand checks are `judgement`, labelled as such;
- publish and send are dry runs, as before.

### ARM SDLC Factory: classified, and what remains

Every one of its 117 actions is classified. The 33 that run or change something (test runs, scans, deployments, Sitecore
edits) are dry runs on this layer: the model is told nothing was executed and to leave result values empty, and the result
is flagged. The 20 that report measurements (site audits, analytics validation, accessibility review) are told to report
only what is in their inputs.

The factory's agents declared no inputs of their own, so on this layer they had a request and nothing else. Each now
takes two (declared in `config/quality/arm_sdlc.yaml`, so regenerating the pack keeps them):

- `material`: what to work on, as a file or pasted text (requirements, a design export, code, a page's HTML, a test plan);
- `evidence`: what a tool or a system observed (a crawl export, a scan result, a test report, an analytics export).

The 20 measuring actions read `evidence` and report only what is in it. Run alone without it they refuse ("it reports what
a tool or a system observed, and nothing observed was given"); inside a flow they run and say what was not measured.

**Generic, not one customer's.** The factory's configuration was written inside one customer's delivery: the prompts
named that customer, its sites, its design system, its quality gate and every platform it runs, and the sample outputs were
that customer's release. None of that is in the catalog now (`src/caf/packs/generalise.py`):

- instructions (prompts, checklists, standards, summaries, flow names) name no customer, and speak of each platform by the
  role it plays: "the CMS", "the design system", "the consent platform", "the marketing-automation platform". The 20
  SitecoreAI agents keep Sitecore's name, because they are that platform's own capabilities; the same holds for the
  OneTrust classifier and the Asana sync;
- samples (the typical ask, every sample output) are a fictional company on a reserved `.example` domain;
- `scripts/build_pack_arm.py` runs the same pass and stops if any customer-specific text is left, so rebuilding the pack
  from the factory's configuration cannot bring it back;
- `tests/test_agent_generic.py` holds the shipped pack to it: no customer's name, site, design system, event or product in
  any instruction, sample or prompt as built for a run, and no assumed platform in a generic agent's instructions.

Which product plays each role for a caller comes from that caller's **workspace**: their own estate, saved once
(`caf shop workspace <customer> --template`, `--set`), and given to every pack agent they call as WORKSPACE, to them
only. A fact about their estate that is not there is unknown to the agent, and it says so.

What remains for this pack:

- the instructions were made platform-neutral by rule, not rewritten by hand. They read plainly, but an editorial pass by
  the accelerator owners would improve them;
- the pack's name and its ids still say ARM (`ARM SDLC Factory`, `arm_*`). Ids are what callers and subscriptions hold,
  so renaming them is a breaking change to make on purpose;
- the 33 dry-run actions become real only when the factory's tools are connected as the pack's home, as Campaign
  Studio's are. On this layer they return the plan.

### Marketing blueprint (28 agents)

These have their own code, typed inputs and tests, and 25 of them refuse empty input. Three run from the bundled demo
company's data through local connectors when called with nothing (`webinar_followup_email`, `heatmap_analysis`,
`ga4_traffic_report`), and six read bundled sample files in their self-tests. Those runs are real work on demo data; they
now say so (standard 6).

**Whose company they write for.** These agents write from skills (`config/skills`): a brand voice given to every
prompt, a press record whose boilerplate and media contact are inserted word for word, a translation glossary, a UTM
convention. The server's describe one fictional demo company, and every customer's work was written from them. Now:

- a customer's own versions come from the `skills` section of their workspace, each stated as what differs
  (`RunContext.skill`). For the four that describe a company (brand voice, press record, glossary, semantic layer) the
  demo company's facts are taken out first, so nothing of it shows through what the customer left unstated; the rules in
  those skills (style limits, AP abbreviations) stay;
- a customer's run that still falls back to the demo company's profile says so: a warning on the result and
  `demo_profile` in its provenance (standard 6, the same rule as demo data);
- a skill that is not this layer's, or not its shape, is refused when the workspace is saved;
- the press release agent no longer proposes a newswire send when no region is known.

`tests/test_agent_generic.py` holds this: a customer with their own profile gets their name, voice and paragraph and
nothing of the demo company's in the prompt or the result; one without is told; another customer sees none of it.

One thing still to decide before selling these to several customers: connectors are configured once for the server
(`config/settings.yaml`), not per customer. A customer's run of a connector-backed agent reads the server's connector,
which today is the demo data. Inputs passed in the call are always the customer's own. Per-customer connector
configuration is not built.

## Adding an agent to this standard

1. Decide the basis of each action. If a number in the output would be acted on, either compute it (`code:`) or call it
   what it is (`judgement`), or require it as input (`measured` with `evidence`).
2. Declare `inputs` with `file`, `choices` and examples; declare `minimum`.
3. For a calculation set `code_only: true` and raise `ValueError` with what is missing and what to add.
4. Where the model writes something arithmetic can check, add a `verify` function.
5. Give it a `selftest` on a bundled synthetic sample, and run `caf agents audit` and `caf agents certify --agent <id>`.
