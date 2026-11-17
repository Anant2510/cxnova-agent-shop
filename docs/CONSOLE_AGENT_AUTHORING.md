# Console agent authoring guide (browser runtime)

The hosted console (`console/dist/index.html`) runs every agent **in the browser** with the same
contracts as the Python runtime: the catalog embedded in the page carries each agent's Pydantic
input/output JSON schemas, `prompt_schemas`, `selftest_input`, the YAML skills and the
Nunjucks-precompiled versions of `config/prompts/**.j2`. A browser agent is the twin of its Python
module: same stages, same deterministic logic, same output contract.

Reference twins: `console/src/js/agents/utm_generation.js` (deterministic),
`campaign_brief_generation.js` (model + validator + task action), `email_creation.js`
(model + linter + repair + gated export).

## File and registration
```js
// console/src/js/agents/<agent_id>.js   (one file per agent; build.py concatenates them)
CAF.agent("<agent_id>", {
  async run(input, ctx) {           // input already validated against the Python Input schema (defaults filled)
    ...
    return { output, summary, view };   // output MUST validate against the Python Output schema
  },
  selftest() { return {...}; },     // optional: browser-specific sample input (default: catalog selftest_input)
});
```
If `output` breaks the Output schema the runtime marks the run `error` - parity is enforced.
Include `findings` exactly as the Python Output does.

## ctx API
| call | meaning |
|---|---|
| `await ctx.generate(template, vars, {tier, images})` | renders `config/prompts/<agent>/<template>.j2` (Nunjucks) and returns an object validated against `prompt_schemas[template]` - Claude when connected, schema-valid offline placeholders otherwise |
| `await ctx.text(template, vars)` | free-text model call |
| `ctx.llmLive` | true when model steps will reach Claude (skip repair loops / heuristics when false) |
| `ctx.skill(name)` | YAML skill as an object (deep copy) |
| `await ctx.data(name)` | embedded sample data: CSV -> array of row objects (numbers coerced), JSON -> object, text -> string. Names are paths under `sample_data/` without extension, e.g. `analytics/daily`, `analytics/pages`, `analytics/channels`, `behaviour/heatmap_events`, `experiments/registry`, `experiments/results/exp-hero-cta`, `webinar/attendees`, `webinar/transcript`, `webinar/info`, `keywords/gsc_queries`, `search/fixture_results`, `docs/whitepaper`, `docs/case_study`, `docs/board_report`, `docs/interview`, `industry_corpus/<name>`, `warehouse/<table>`, `charts/<name>`, `translation/tm`, `crm/contacts`. `CAF.datasets.names()` lists them; `ctx.hasData(name)` checks one. |
| `ctx.page({url, html})` | `{html, url, status, redirects, source}` from pasted HTML, or from the embedded demo site for `https://www.meridian-mutual.example/...` URLs; returns `null` (and marks the run degraded) for other URLs - the hosted page cannot fetch websites |
| `ctx.site(pathOrUrl)` / `CAF.site.pages()` | raw demo-site files incl. `/robots.txt`, `/sitemap.xml`, `/_fixture.json` (404/redirect map) |
| `ctx.check({rule, status, message, severity, ref, target, data})` | finding / validation result (`pass|warn|fail|info`) |
| `ctx.artifact({name, mime, content, description})` | file the viewer can preview and save (content: string or Blob) |
| `ctx.action({id, kind, connector, method, payload, summary, requires_approval})` | gated side effect; browser connectors exist for `esp.export`, `cms.publish`, `newswire.distribute`, `tasks.create`, `flags.deploy_variation` |
| `ctx.warn(msg)`, `ctx.degraded(reason)`, `ctx.progress(msg)` | caveats, degraded-mode reasons, live progress text |
| `ctx.upstreamOutput(...ids)`, `ctx.upstreamFindings()`, `ctx.upstreamText()` | orchestrated-run context, same semantics as Python |
| `await ctx.runSubagent(agentId, input)` | run another agent as a sub-agent (offline/quick/abort settings propagate; model calls roll up) - twin of Python `ctx.run_subagent` |

Return `view` blocks to control display (optional; otherwise output is auto-rendered):
`{type:"markdown", title, markdown}`, `{type:"table", title, rows, columns}`, `{type:"kv", title, data}`,
`{type:"code", title, code}`, `{type:"svg", title, svg}`, `{type:"html", title, artifact}`,
`{type:"image", title, artifact}`, `{type:"list", title, items}`.

## Deterministic libraries (`CAF.lib`)
- `text`: `tokens, words, sentences, canonical, normQuotes, slugify, wordCount, flesch, truncate, dedupe, vectorize, cosine, similarity(query, docs), cluster(docs, {threshold, char})`
- `stats`: `mean, median, std, quantile, normCdf, normPpf, chi2Sf, tSf2, twoPropZTest(c1,n1,c2,n2), diffCI, cohensH, sampleSizePerVariant(baseline, mdeRel, alpha, power, comparisons)` (matches statsmodels NormalIndPower), `mdeForSample, rng(seed), betaSample, bayes(c1,n1,c2,n2), chiSquareGOF(observed, shares), linreg(xs, ys), autocorr, madZ, changePoints(ys)`
- `csv`: `parse(text)`, `stringify(rows, columns)`
- `html`: `parse(html) -> Document`, `text(el)`, `jsonld(doc)`, `meta(doc)`, `headings(doc)`, `links(doc, base)`, `absolute`, `parseRobots(text)`, `robotsAllowed(groups, ua, path)`, `renderInFrame(html, {width, height}) -> {frame, doc, win, remove}` (real layout + computed styles)
- `color`: `parse`, `luminance`, `contrast(fg, bg)`
- `chart`: `line({series:[{name, values, points}], labels, title, markers, themed})`, `bars({items:[{label, value}], title, themed})` -> SVG string (`themed:false` for standalone artifacts)
- `email`: `html, mjml, text, lint` (port of `caf.core.emailkit`)
- `qr`: `make(text, {ecl, cell, margin, fg, bg}) -> {svg, modules, version, size_px, isDark}`, `png(svg, size) -> Blob`
- `md`: `html(markdown)` (sanitised)
- axe-core: inject `<script src="${CAF.boot.axe_url}"></script>` into the HTML you pass to `renderInFrame`, then call `frame.win.axe.run(frame.doc)`.

## Rules
1. Parity: port the Python module's pure functions; keep rule packs/thresholds in skills (shared YAML).
2. No network (fetch is blocked), no `eval`/`new Function`, no external URLs except the demo site.
3. Never let a missing library or Claude break the agent: degrade with a reason.
4. Keep selftests fast (< 5 s offline).
5. Test: `python3 console/test/harness.py --agents <ids> --verbose` (offline) and
   `python3 console/test/harness.py --agents <ids> --llm mock` (live code path with a mock Claude).
   Both must report every agent LIVE with no page errors. The harness enforces a no-eval CSP.
