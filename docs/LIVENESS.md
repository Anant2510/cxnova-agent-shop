# Agent liveness report

Checked 2026-10-18T10:13:33+00:00 - probe mode: **offline** - **28/28 agents live** (all blueprint agents registered)

Each probe runs the agent's full LangGraph sub-graph from its `selftest_input()` (deterministic stages, validators, connectors, exporters) and validates the result against the agent's output contract. In offline mode model steps are answered by the offline model, so this proves wiring and the deterministic core; run with `--live` to send model steps to the configured provider.

| # | Category | Agent | Kind | Status | Graph stages | Checks (pass/warn/fail) | Playbooks | Time |
|---|---|---|---|---|---|---|---|---|
| 1 | Strategy and planning | Campaign Brief Generation | llm | LIVE - degraded | draft > validate > publish | 4/0/0 | campaign_launch, webinar_program, competitive_pitch | 1.1s |
| 2 | Strategy and planning | Keyword Research | hybrid | LIVE - degraded | gather > expand > score > cluster > recommend > export | 1/1/0 | content_factory | 3.0s |
| 3 | Strategy and planning | Competitive Insights | hybrid | LIVE - degraded | plan > gather > synthesize > validate > report | 2/2/0 | competitive_pitch | 1.2s |
| 4 | Strategy and planning | UTM Generation | deterministic | LIVE - ok | resolve_campaign > build_links > export | 1/0/0 | campaign_launch | 1.9s |
| 5 | Content creation | Email Creation | hybrid | LIVE - needs_approval | context > draft > lint_and_repair > render | 4/0/0 | campaign_launch, webinar_program | 0.2s |
| 6 | Content creation | Blog Post Creation | llm | LIVE - needs_approval | research > outline > draft > critique > revise > finalize | 6/4/1 | content_factory | 0.6s |
| 7 | Content creation | LinkedIn InMail Copy Creation | llm | LIVE - degraded | context > draft > validate_and_repair > render | 2/2/0 | campaign_launch, webinar_program | 0.1s |
| 8 | Content creation | Content Adaptation | llm | LIVE - degraded | ingest > adapt > validate_and_fix > export | 6/0/0 | campaign_launch, webinar_program, content_factory | 0.1s |
| 9 | Content creation | Webinar Follow Up Email Creation | hybrid | LIVE - needs_approval | attendance > summarise > segment > draft > lint_and_repair > render_export | 11/0/3 | webinar_program | 1.3s |
| 10 | Content creation | Industry Marketing | hybrid | LIVE - degraded | profile > retrieve > tailor > comply > export | 3/2/0 | competitive_pitch | 0.5s |
| 11 | Content creation | Press Release Creation | llm | LIVE - needs_approval | prepare > draft > validate > finalise | 7/1/1 | campaign_launch | 0.1s |
| 12 | Content creation | Quote Extraction | hybrid | LIVE - degraded | ingest > propose > anchor > heuristic > bank | 2/1/0 | webinar_program, content_factory | 0.4s |
| 13 | Content creation | QR Code Generation | deterministic | LIVE - ok | resolve_targets > shorten > encode > report | 10/1/0 | campaign_launch | 0.5s |
| 14 | Content creation | Content Summary | llm | LIVE - degraded | ingest > chunk > map > reduce > format | 2/0/0 | webinar_program, content_factory | 0.3s |
| 15 | Website optimization | GEO Recommendations | hybrid | LIVE - degraded | fetch > audit > score > citation_test > plan > report | 35/7/0 | website_audit | 0.6s |
| 16 | Website optimization | Technical SEO Recommendations | hybrid | LIVE - degraded | crawl > evaluate > prioritise > explain > report | 26/19/6 | website_audit, headless_migration | 1.3s |
| 17 | Website optimization | GA4 Traffic Report | hybrid | LIVE - degraded | window > load > compute > anomalies > narrate > report | 0/1/0 | website_audit, insights_reporting | 5.0s |
| 18 | Website optimization | Web Accessibility Evaluation | hybrid | LIVE - degraded | scan > map > remediate > ci_gate > report | 1/1/6 | website_audit | 2.9s |
| 19 | Website optimization | Heatmap Analysis | hybrid | LIVE - degraded | load > dom > metrics > insights > report | 0/3/1 | website_audit | 7.3s |
| 20 | Website optimization | Content Model Creation | hybrid | LIVE - degraded | analyse > vision > propose > normalise > emit > report | 9/0/0 | headless_migration | 1.8s |
| 21 | Website optimization | Content Translation | hybrid | LIVE - needs_approval | prepare > memory > machine_translate > post_edit > qa > assemble | 11/5/0 | content_factory, headless_migration | 0.1s |
| 22 | End-to-end testing | Experiment Ideation | hybrid | LIVE - degraded | inspect_page > gather_evidence > match_heuristics > generate_ideas > score_and_rank > persist_backlog > report | 2/0/0 | website_audit, experimentation_loop | 2.6s |
| 23 | End-to-end testing | Experiment Planning | hybrid | LIVE - degraded | inspect_page > baseline > size > draft > reconcile > export | 4/0/0 | experimentation_loop | 3.1s |
| 24 | End-to-end testing | Variation Development | hybrid | LIVE - needs_approval | load_page > brand_tokens > plan_patch > compile > validate_render > deliver | 6/0/0 | experimentation_loop | 5.0s |
| 25 | End-to-end testing | Experiment Review | hybrid | LIVE - degraded | load > evaluate > explain > report | 11/2/8 | experimentation_loop | 0.1s |
| 26 | End-to-end testing | Experiment Summary | hybrid | LIVE - degraded | load > compute > decide > narrate > persist > report | 2/0/0 | experimentation_loop | 0.9s |
| 27 | End-to-end testing | Exploration Generation | hybrid | LIVE - degraded | context > generate_query > validate > execute > visualise > explain | 2/0/0 | insights_reporting | 1.5s |
| 28 | End-to-end testing | Chart Summary | hybrid | LIVE - degraded | load > analyse > narrate > render | 1/0/0 | insights_reporting | 1.0s |

## Degraded-mode notes

- **Campaign Brief Generation**: offline model: generated copy is placeholder text
- **Keyword Research**: offline model: generated copy is placeholder text
- **Competitive Insights**: offline model: cached synthesis is placeholder text
- **Email Creation**: offline model: generated copy is placeholder text
- **Blog Post Creation**: offline model: generated copy is placeholder text
- **LinkedIn InMail Copy Creation**: offline model: generated copy is placeholder text
- **Content Adaptation**: offline model: generated copy is placeholder text
- **Webinar Follow Up Email Creation**: content_summary: offline model: extractive summary (top-ranked verbatim sentences), not an abstractive summary; offline model: generated copy is placeholder text
- **Industry Marketing**: offline model: generated copy is placeholder text
- **Press Release Creation**: offline model: generated copy is placeholder text
- **Quote Extraction**: offline model: quotes chosen by the deterministic quotability ranker; offline model: generated copy is placeholder text
- **Content Summary**: offline model: extractive summary (top-ranked verbatim sentences), not an abstractive summary
- **GEO Recommendations**: offline model: generated copy is placeholder text
- **Technical SEO Recommendations**: offline model: generated copy is placeholder text
- **GA4 Traffic Report**: offline model: generated copy is placeholder text
- **Web Accessibility Evaluation**: offline model: generated copy is placeholder text
- **Heatmap Analysis**: offline model: generated copy is placeholder text
- **Content Model Creation**: offline model: generated copy is placeholder text
- **Content Translation**: offline model: machine translation returned placeholders (segments marked [offline:<locale>]); offline model: no post-editing pass
- **Experiment Ideation**: no language model configured: ideas phrased from the heuristic library templates
- **Experiment Planning**: no language model configured: hypothesis, variants and risks use deterministic templates
- **Variation Development**: no language model configured: request parsed deterministically
- **Experiment Review**: no language model configured: findings explained with the rule pack's fix texts
- **Experiment Summary**: no language model configured: narrative written by deterministic templates from the computed statistics
- **Exploration Generation**: no language model configured: question mapped to the semantic layer by deterministic synonym matching; no language model: explanation written from computed result facts
- **Chart Summary**: no language model configured: commentary written by deterministic templates from the computed facts
