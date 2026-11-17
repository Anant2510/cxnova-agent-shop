# Agent authoring guide (Python runtime)

Every agent in `config/blueprint.yaml` (28 of them) must be **live**: registered, compiled to a
LangGraph sub-graph, runnable end-to-end offline from its `selftest_input()`, and honest about
what it could not do. The orchestrator, API, MCP server, A2A cards and the browser console all
discover agents from the registry, so an agent that follows this guide is automatically
available everywhere.

Reference implementations - copy their structure:

| Pattern | File |
|---|---|
| Deterministic (no model) | `src/caf/agents/strategy/utm_generation.py` |
| Model + validator + task hand-off | `src/caf/agents/strategy/campaign_brief_generation.py` |
| Hybrid: model + deterministic linter + repair loop + gated export | `src/caf/agents/content/email_creation.py` |

## 1. Files you create per agent

```
src/caf/agents/<category>/<agent_id>.py      # the agent (auto-discovered, decorated with @register)
config/prompts/<agent_id>/<template>.j2      # one file per structured / text model call
config/skills/<skill_name>.yaml              # rule packs, taxonomies, registries, heuristics (if needed)
tests/agents/test_<agent_id>.py              # targeted tests of the deterministic logic
```
`<category>` is `strategy | content | web | experimentation` (the blueprint category).
Do **not** edit `src/caf/core/**`, `src/caf/connectors/**` or other agents' files. If you need a
core/connector change, describe it precisely in your final report instead.

## 2. Anatomy

```python
@register
class MyAgent(BaseAgent):
    spec = AgentSpec(
        id="technical_seo_recommendations",            # MUST equal the blueprint id
        name="Technical SEO Recommendations",          # MUST equal the blueprint name (no asterisks)
        category=Category.WEB,
        summary="One sentence, what it does for the user.",
        kind=AgentKind.HYBRID,                          # LLM | DETERMINISTIC | HYBRID
        tier=Tier.BALANCED,                             # model tier when it calls the model
        connectors=["crawler"],                         # connector kinds it uses
        skills=["seo_rules"],                           # YAML skills it reads
        keywords=["technical seo", "crawl", ...],       # routing hints for the orchestrator (6-10)
        produces=["seo_audit.html", ...],
        feeds=["experiment_ideation"],                  # agents that typically consume its output
        side_effects=[],                                # e.g. ["cms.publish"] when it proposes actions
    )
    Input = MyInput          # Pydantic: validated before the graph runs
    Output = MyOutput        # Pydantic: the final state['output'] must satisfy it
    schemas = {"explain": ExplainModel}   # template name -> model, for every ctx.generate(..., Model) call

    def selftest_input(self): return {...}      # offline, sample-data based, < 15 s (browser < 45 s)
    def stages(self): return [("crawl", self.crawl), ("evaluate", self.evaluate), ("explain", self.explain), ("report", self.report)]
```
Use the blueprint's Opal inference level for `tier`: Standard -> FAST, Balanced -> BALANCED,
Pro -> PRO, Complex -> COMPLEX; deterministic agents use `Tier.NONE` (or the tier of their
optional explanation step).

### Stages
A stage is `fn(state: dict, ctx: RunContext) -> dict` returning a **partial update** with any of:

| key | merge rule | meaning |
|---|---|---|
| `data` | dict merge | working memory between stages |
| `output` | dict merge | the agent's result (validated against `Output` at the end) |
| `summary` | last write | one line shown in UIs and the orchestrator synthesis (always set it) |
| `checks` | append | `Check` findings / validation results |
| `artifacts` | append | `ArtifactRef`s from `ctx.artifacts.write_*` |
| `warnings` | append | human-readable caveats |
| `actions` | append | `PendingAction`s (publish-type side effects) |
| `provenance` | dict merge | e.g. `{"degraded_reasons": ["no keyword data connector"], "evidence_count": 12}` |

`state["inputs"]` is the validated input as a dict. Items you return as models are dumped to
dicts, so in later stages `state["checks"]`, `state["artifacts"]`, `state["actions"]` are lists of dicts.
Agents with loops or fan-out may override `build_graph()` (use `self.node(name, fn)` to wrap
stages; state type is `AgentState`, context schema is `RunContext`).

### RunContext (`ctx`)
- `ctx.generate(template, Model, tier=None, images=None, **vars)` renders
  `config/prompts/<agent_id>/<template>.j2` and returns a validated `Model` instance (or text when
  `Model` is None). `brand` (brand_voice skill) and `today` are injected into every template.
- `ctx.skill(name, default=...)` -> YAML skill as a dict (deep copy).
- `ctx.connector(kind)` -> configured connector (records provenance). See section 6.
- `ctx.artifacts.write_text(name, content, mime, description)`, `.write_bytes(...)`, `.write_json(...)`.
- `ctx.upstream_output("agent_id", ...)` -> output dict of an earlier step in an orchestrated run, or None.
  `ctx.upstream_findings()` -> all `findings` from upstream steps. `ctx.upstream_text()` -> digest of upstream content.
- `ctx.llm.offline` -> True when no model is configured (tests). `ctx.log(msg)` -> trace event.
- `ctx.brand` -> brand voice dict.

## 3. The rules that make an agent "live"

1. **Deterministic first.** Detection, maths, statistics, validation, encoding, rule engines,
   crawling and parsing are code (pandas, scipy, statsmodels, sqlglot, bs4, Playwright). The model
   only generates, explains or prioritises - and only from facts the code computed, which you pass
   into the prompt. Never let the model do arithmetic you can do.
2. **Typed contract.** `Output` is the contract the orchestrator, API and console rely on.
   Prefer explicit sub-models; put large blobs (HTML reports, CSVs, images) in artifacts and keep
   `output` under ~200 KB. Analysis agents include `findings: list[Finding]` (from
   `caf.core.handoff`) so others can consume them.
3. **Every model call goes through a template + schema.** `ctx.generate("explain", ExplainModel, ...)`
   needs `config/prompts/<agent_id>/explain.j2` and `schemas = {"explain": ExplainModel}`. The
   browser console reuses the same templates and JSON schemas.
4. **Degrade, never crash, never fabricate silently.** If a connector is unavailable (search
   `none`, keyword data `none`, no browser) or data is missing, still return a valid `Output`,
   add a warning and `provenance={"degraded_reasons": [...]}`, and label estimates as estimates
   (e.g. an `is_estimate: bool` field). Raising is reserved for invalid input.
5. **Offline-safe control flow.** With the offline model, structured outputs are schema-valid
   placeholders. Do not branch on model prose; skip repair loops when `ctx.llm.offline`.
   Reconcile model output with inputs deterministically (counts, labels, ids, ordering).
6. **Side effects are actions.** Publishing (CMS, ESP, newswire, flag deploy) is proposed as a
   `PendingAction(kind, connector, method, payload, summary, requires_approval=True)`. The payload
   must be JSON-serialisable kwargs for that connector method. The approval gate executes it.
7. **Upstream-aware, upstream-optional.** Use `ctx.upstream_output(...)` to enrich when an
   orchestrated run provides context (e.g. QR codes reuse UTM links; Ideation reuses findings), but
   the agent must work standalone from its own inputs.
8. **Inputs work without network.** Page/document agents accept `url` **and** `html`/`text`
   alternatives (the hosted console cannot fetch URLs). Default `selftest_input()` uses the offline
   demo site `https://www.meridian-mutual.example/...` and files under `sample_data/`.
9. **Summary + checks.** Always set `summary`. Express rule results as `Check`s with
   `status pass|warn|fail|info`, a `severity`, and a `ref` for standards (e.g. `WCAG 1.4.3 (AA)`).

## 4. Prompt templates (Jinja2 / Nunjucks common subset)
Templates are rendered by Jinja2 in Python and by Nunjucks in the browser, so:
- Allowed: `{{ var }}`, dotted access `a.b.c`, `{% if %}/{% elif %}/{% else %}`, `{% for x in list %}` with
  `loop.index`, filters `default(x, true)`, `join(", ")`, `length`, `upper`, `lower`, `trim`, `first`,
  `last`, `round`, and the custom filters `tojson`, `bullets(prefix)`, `numbered`.
- Not allowed: dict iteration (`.items()`), Python methods (`.get`, `.keys`, `.split`), `~`
  concatenation, `{% set %}` blocks with filters, macros, includes, `is none`. Pre-format in code
  and pass lists/strings.
- System vs user: put a line containing exactly `---- USER ----`; text above becomes the system
  message. Keep the system part for role + brand voice; the user part has inputs + rules.
- Pass computed facts explicitly (e.g. `findings`, `stats`) - the model must not invent numbers.

## 5. Skills (YAML)
Anything a business user might tune is a skill: rule packs (SEO, GEO, accessibility mapping,
experiment review), taxonomies, registries (channels, platform limits), heuristics libraries,
industry profiles, glossaries, boilerplate. One file per concern, with a `version` key and comments.

## 6. Connectors (kinds, local providers, methods)
| kind | default provider | methods |
|---|---|---|
| ingestion | local | `load(source=None, *, text=, html=, path=, data=, filename=) -> Document(source,title,text,mime,html,metadata)`; `Document.sections`, `Document.locate(offset)` |
| crawler | httpx (+fixtures) | `fetch(url) -> Page(url, final_url, status, headers, html, elapsed_ms, redirects, error)`, `Page.ok/.soup()/.text()/.links()/.is_html`, `fetch_text(url)`, `crawl(start_url, max_pages, same_host)`, `browser_available()`, `with_page(fn, url=|html=, width, height)`, `render(url)`, `screenshot(url=|html=, css=, js=, width, height, full_page)`, `is_fixture(url)`, `fixture_root(url)` |
| search | fixture (demo) / none | `search(query, num, recency_days) -> [SearchResult(title,url,snippet,published,source,retrieved_at,provider)]`, `news(query,num,days)`, `feeds(urls, days)`, `.available` |
| keyword_data | csv | `queries(topic=None, site=None, limit) -> DataFrame[query,page,clicks,impressions,ctr,position]`, `.available` |
| analytics | csv | `daily(start,end) -> [date,sessions,users,new_users,engaged_sessions,conversions,revenue]`, `pages(start,end,limit) -> [page_path,page_title,sessions,users,engaged_sessions,engagement_time_s,conversions,engagement_rate,avg_engagement_time_s,conversion_rate]`, `channels(start,end) -> [channel,sessions,users,conversions,revenue,conversion_rate]`, `channels_daily(start,end)`, `date_bounds()` |
| behaviour | csv | `events(page_url=None) -> DataFrame[session_id,ts,page_url,device,event_type,x,y,selector,element_text,tag,is_interactive,scroll_pct,viewport_w,viewport_h,page_h]`, `pages()` |
| experiments | json | `list(status=None) -> [ExperimentDefinition]`, `get(id)`, `results(id) -> DataFrame[variant,segment,visitors,conversions,revenue]`, `upsert(defn)` (models in `caf.connectors.experiments`) |
| webinar | csv | `info()`, `attendees() -> DataFrame`, `transcript() -> str` |
| warehouse | duckdb (read-only) | `tables() -> {table: [(col, type)]}`, `query(sql, max_rows) -> DataFrame` (SELECT/WITH only), `.dialect` |
| vectorstore | tfidf | `upsert(collection, [{id,text,metadata}])`, `search(collection, query, k, filters) -> [{id,text,metadata,score}]`, `count(collection)` |
| store | sqlite | `put(collection, record, key=None)`, `get(collection, key)`, `query(collection, where, limit)` |
| shortlink | sqlite | `shorten(url, code=None, meta=None) -> {code, short_url, target}`, `resolve(code)`, `record_scan(code, meta)`, `stats(code)` |
| translation | llm | `translate(segments, source=, target=, glossary=, dnt=, tone=) -> [str]` |
| cms | file | `publish(title=, body=, slug=, content_type=, fields=, status=, locale=, body_format=)` |
| esp | file | `export(name=, subject=, preheader=, html=, text=, mjml=, audience=, metadata=)` |
| tasks | json_file | `create(tasks, project=)` |
| newswire | file | `distribute(headline=, body=, embargo=, regions=, metadata=)` |
| flags | file | `deploy_variation(experiment_id=, variation_id=, css=, js=, selectors=, preview=)` |

Shared helpers: `caf.core.textutil` (normalize, canonical, sentences, chunk_text, tfidf_similarity,
slugify, word_count, truncate), `caf.core.emailkit` (render_html/mjml/text, lint_email),
`caf.core.handoff` (Finding, Recommendation).

## 7. Sample data (offline demo world: fictional insurer "Meridian Mutual", today = 2026-10-18)
- `sample_data/site/` served at `https://www.meridian-mutual.example/` (pages: `/`, `/products/home-insurance.html`,
  `/products/car-insurance.html`, `/blog/`, `/blog/claims-tips.html`, `/about.html`, `/contact.html`, `/quote.html`,
  `robots.txt`, `sitemap.xml`; `/old-page.html` 301s; `/promo` and `/fr/` 404; no `llms.txt`). Deliberate SEO/GEO/a11y issues.
  Home-insurance selectors: `#hero-cta`, `img.hero-image`, `.plan-tab[data-plan="basic|standard|premium"]`, `#pricing`, `#advisor-cta`, `#faq`.
- `sample_data/analytics/{daily,pages,channels}.csv` (2026-03-01..2026-09-30; campaign spike 2026-06-10..14; tracking outage 2026-08-19..20)
- `sample_data/behaviour/heatmap_events.csv` (home-insurance page; dead clicks on hero image, rage clicks on premium tab, false bottom ~40-45%)
- `sample_data/experiments/registry.json` + `results/exp-hero-cta.csv`, `results/exp-pricing-table.csv` (SRM issue); `exp-mobile-sticky` is deliberately broken
- `sample_data/webinar/{info.json,attendees.csv,transcript.txt}`
- `sample_data/keywords/gsc_queries.csv`, `sample_data/search/fixture_results.json` (competitors Harborline Insurance, Crestview Assurance)
- `sample_data/docs/{whitepaper.md,case_study.docx,board_report.pdf,interview.txt}`
- `sample_data/industry_corpus/*.md` (front-matter: industry, title, doc_type, tags)
- `sample_data/warehouse/*.csv` + `config/skills/semantic_layer.yaml`; `sample_data/charts/*.csv`; `sample_data/translation/tm.tmx`

## 8. Testing
```
CAF_LLM_PROVIDER=offline pytest tests/test_liveness.py -k <agent_id>     # contract + live run
pytest tests/agents/test_<agent_id>.py                                    # your logic tests
```
Write tests that prove the deterministic core is right on the sample data (e.g. the SEO agent
finds the duplicate title, the redirect and the 404; the power calculation matches statsmodels;
the quote extractor rejects an unanchored quote). Mark Playwright tests `@pytest.mark.browser`.
