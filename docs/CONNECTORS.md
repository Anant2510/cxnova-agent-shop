# Connectors: local providers and external adapters

Every agent talks to the outside world through a connector **kind** (`analytics`, `cms`, `esp` ...).
Each kind has a **local provider** that works offline on `sample_data/` and one or more **external
adapters** for the client's real platforms. Adapters return exactly the same DataFrame columns,
dataclasses and dict keys as the local provider, so switching GA4 for Adobe Analytics, Braze for AJO
or Jira for Azure DevOps is a settings change. No agent code changes.

```yaml
# config/settings.yaml
connectors:
  analytics:
    provider: ${CAF_ANALYTICS_PROVIDER:-csv}   # csv | ga4 | adobe_analytics
    property_id: ${GA4_PROPERTY_ID:-}
    credentials_file: ${GOOGLE_APPLICATION_CREDENTIALS:-}
```

* `connectors.<kind>.provider` selects the provider. `ConnectorHub` passes that whole block to it as
  `config`. Every value supports `${ENV}` / `${ENV:-default}`, so secrets stay in the environment.
* To configure a provider that is not the active one (e.g. to call `hub.get("search", "tavily")`
  next to an active `brave`), nest its block under `connectors.<kind>.providers.<provider>`.
* Options every external adapter accepts: `timeout_s` (default 30), `max_retries` (default 2),
  `retry_backoff_s` (default 0.5) and `base_url` (sovereign clouds, proxies, on-prem). Retries cover
  429 (honouring `Retry-After`), connection failures, and 5xx on idempotent or read-only calls. A
  POST that creates something is never retried on 5xx.
* `available` is `True` only when the adapter's required keys are set. It never calls the network,
  and neither does constructing an adapter or importing its module.
* Credentials are read only from the connector's config block. Error messages never echo query
  strings (SerpAPI sends the key as a query parameter).
* `config["_transport"]` (an `httpx` transport) is a test hook. The test suite injects
  `httpx.MockTransport` through it. Never set it in YAML.

Implementation: `src/caf/connectors/_http.py` (client, retries, OAuth helpers),
`search_providers.py`, `analytics_providers.py`, `marketing_providers.py`,
`experiment_providers.py`, `misc_providers.py` and `crm.py` (the new `crm` kind).

## Verification legend

| Label | Meaning |
|---|---|
| **E2E-local** | Local provider. Tested end to end: the agent liveness suite (`tests/test_liveness.py`) runs every agent's self-test against `sample_data/` through it. |
| **Mock-contract** | External adapter. The request (method, URL, auth header, body) and the response mapping to the local shape are unit-tested with `httpx.MockTransport` (`tests/connectors/test_providers.py`). **Not exercised against a live vendor account: there are no credentials in this environment.** |
| **+ Agent replay** | In addition, real agents run unchanged on the adapter against a mock API that replays the sample data, and reach the same result as on the local provider (`tests/connectors/test_agent_swaps.py`). Still mocked, not live. |
| **+ Agent run** | A real agent runs unchanged on the adapter (mock API) and completes. Results are not compared with the local run because the platform's data differs in kind (Clarity aggregates, a translation echo). |
| **Real engine** | Executed against a real engine in tests (SQLite via SQLAlchemy). Vendor dialects themselves are not exercised. |

## Providers by kind

Required keys are **bold**. The env var shown is the one `settings.yaml` already maps; the others
need the snippet in [Settings to add](#settings-to-add).

| Kind | Provider | Config keys (env) | Returns | Verification |
|---|---|---|---|---|
| ingestion | `local` | `whisper_model` | `load(...) -> Document(source,title,text,mime,html,metadata)` | E2E-local |
| crawler | `httpx` | `user_agent`, `timeout_s`, `fixtures` | `fetch() -> Page`, `crawl()`, `render()`, `screenshot()` | E2E-local |
| search | `none` | - | `[]`, `available=False` (agents flag estimates) | No-op |
| search | `fixture` | `path`, `reference_date` | `search()/news()/feeds() -> [SearchResult(title,url,snippet,published,source,retrieved_at,provider)]` | E2E-local |
| search | `serpapi` | **`api_key`** (SEARCH_API_KEY), `country`, `language` | same `SearchResult` list; `provider="serpapi"` / `"serpapi:news"` | Mock-contract |
| search | `brave` | **`api_key`** (SEARCH_API_KEY), `country`, `language`, `safesearch` | same; HTML stripped from titles/snippets | Mock-contract |
| search | `tavily` | **`api_key`** (SEARCH_API_KEY), `search_depth`, `country` | same | Mock-contract |
| keyword_data | `none` | - | empty `[query,page,clicks,impressions,ctr,position]` | No-op |
| keyword_data | `csv` | `path` | `queries(topic, site, limit) -> DataFrame[query,page,clicks,impressions,ctr,position]` | E2E-local |
| keyword_data | `gsc` | **`site_url`** (GSC_SITE_URL), **`credentials_file`** (GOOGLE_APPLICATION_CREDENTIALS) or `credentials_json` or `access_token`, `days`, `data_lag_days`, `search_type`, `max_rows` | same columns, first-party Search Console data | Mock-contract |
| keyword_data | `dataforseo` | **`login`**, **`password`**, `target`, `location_code`, `language_code` | same columns; third-party **estimates** (`df.attrs["estimated"]=True`) | Mock-contract |
| analytics | `csv` | `path` | `daily -> [date,sessions,users,new_users,engaged_sessions,conversions,revenue]`; `pages -> [page_path,page_title,sessions,users,engaged_sessions,engagement_time_s,conversions,engagement_rate,avg_engagement_time_s,conversion_rate]`; `channels -> [channel,sessions,users,conversions,revenue,conversion_rate]`; `channels_daily`; `date_bounds()` | E2E-local |
| analytics | `ga4` | **`property_id`** (GA4_PROPERTY_ID), **`credentials_file`** (GOOGLE_APPLICATION_CREDENTIALS) or `credentials_json` or `access_token`, `conversion_metric` (keyEvents), `revenue_metric` (totalRevenue), `data_start` | same columns and derived-rate formulas | Mock-contract + Agent replay |
| analytics | `adobe_analytics` | **`company_id`** (ADOBE_ANALYTICS_COMPANY_ID), **`rsid`** (ADOBE_ANALYTICS_RSID), **`client_id`** (ADOBE_CLIENT_ID), **`client_secret`** (ADOBE_CLIENT_SECRET) or `access_token`, `scopes`, `segment_id`, `page_dimension`, `channel_dimension`, `metric_map`, `data_start`, `lookback_days` | same columns; `new_users` is NaN unless `metric_map.new_users` is set | Mock-contract + Agent replay |
| behaviour | `csv` | `path` | `events(page_url) -> DataFrame[session_id,ts,page_url,device,event_type,x,y,selector,element_text,tag,is_interactive,scroll_pct,viewport_w,viewport_h,page_h]`, `pages()` | E2E-local |
| behaviour | `clarity` | **`api_token`**, `num_days` (≤3), `max_sessions_per_page`, `viewports` | same columns, **reconstructed from aggregates** (see limitations) + `insights()` | Mock-contract + Agent run (`heatmap_analysis` completes; click sections degrade) |
| experiments | `json` | `path` | `list/get -> ExperimentDefinition`, `results -> DataFrame[variant,segment,visitors,conversions,revenue]`, `upsert` | E2E-local |
| experiments | `optimizely` | **`api_token`**, **`project_id`**, `results_segments`, `revenue_divisor` | same models/columns; `upsert` writes a local draft only | Mock-contract + Agent replay |
| experiments | `adobe_target` | **`tenant`**, **`client_id`**, **`client_secret`** or `access_token`, `scopes`, `activity_types`, `max_activities` | same models/columns (revenue 0); `upsert` writes a local draft only | Mock-contract |
| cms | `file` | `path` | `publish(title=, body=, slug=, content_type=, fields=, status=, locale=, body_format=) -> {id,url,status}` | E2E-local |
| cms | `aem` | **`base_url`**, **`parent_path`**, `model_path` or `model_id`, `access_token` or `client_id`+`client_secret`, `api` (sites/assets), `field_map`, `scopes` | `{id,url,status}` + `path`, `action` | Mock-contract |
| cms | `contentful` | **`space_id`**, **`management_token`**, `environment`, `content_type_map`, `field_map`, `locale_map`, `default_locale` | `{id,url,status}` + `version`, `action`, `locale` | Mock-contract |
| cms | `wordpress` | **`base_url`**, **`username`**, **`application_password`**, `post_type_map`, `default_locale` | `{id,url,status}` + `action` | Mock-contract |
| esp | `file` | `path` | `export(name=, subject=, preheader=, html=, text=, mjml=, audience=, metadata=) -> {id,path,files,status}` | E2E-local |
| esp | `braze` | **`api_key`**, **`rest_endpoint`**, `mode` (email_template/content_block), `tags` | `{id,status}` + `type`, `liquid_tag` | Mock-contract |
| esp | `mailchimp` | **`api_key`**, `server_prefix`, `list_id` or `audience_map`, `from_name`, `reply_to`, `folder_id` | `{id,status}` + `template_id`, `campaign_id` (draft only) | Mock-contract |
| esp | `ajo` | **`client_id`**, **`org_id`**, **`client_secret`** or `access_token`, `sandbox`, `scopes` | `{id,status}` + `sandbox`, `etag` | Mock-contract |
| tasks | `json_file` | `path` | `create(tasks, project=) -> [{id,title,url}]` | E2E-local |
| tasks | `jira` | **`base_url`**, **`email`**, **`api_token`**, `project_key`, `issue_type`, `labels`, `extra_fields`, `auth` (basic/bearer) | `[{id: issue key, title, url: /browse/KEY}]` + `issue_id` | Mock-contract |
| tasks | `azure_devops` | **`organization`**, **`pat`**, `project`, `work_item_type`, `area_path`, `iteration_path`, `tags`, `due_date_field` | `[{id,title,url}]` | Mock-contract |
| webinar | `csv` | `path` | `info() -> dict`, `attendees() -> DataFrame[email,first_name,last_name,company,job_title,registered_at,attended,join_minute,leave_minute,minutes_attended,questions_asked,poll_responses,resources_clicked]`, `transcript() -> str` | E2E-local |
| webinar | `zoom` | **`account_id`**, **`client_id`**, **`client_secret`**, `webinar_id`, `registrant_status` | same columns and `info()` keys; transcript from the cloud-recording VTT | Mock-contract + Agent replay |
| translation | `llm` | - (uses the model adapter) | `translate(segments, source=, target=, glossary=, dnt=, tone=) -> [str]` | E2E-local |
| translation | `deepl` | **`api_key`**, `glossary_id` | same; placeholders/URLs/tags/DNT kept verbatim | Mock-contract + Agent run (`content_translation`: all QA checks pass) |
| translation | `azure` | **`api_key`**, `region`, `category` | same | Mock-contract |
| newswire | `file` | `path` | `distribute(...) -> {id,status,path}` | E2E-local |
| flags | `file` | `path` | `deploy_variation(...) -> {bundle,status,selectors}` | E2E-local |
| flags | `launchdarkly` | **`api_token`**, **`project_key`**, `environment`, `flag_prefix`, `tags` | `{bundle,status,selectors}` + `flag_key`, `action`, `api_url` | Mock-contract |
| warehouse | `duckdb` | `path` | read-only `tables()`, `query(sql, max_rows)`, `.dialect` | E2E-local |
| warehouse | `sqlalchemy` | **`url`** (CAF_WAREHOUSE_URL), `schema`, `engine_options`, `include_tables` | same; identical read-only guard; `.dialect` in sqlglot naming | Real engine (SQLite) |
| vectorstore | `tfidf` | - | `upsert`, `search -> [{id,text,metadata,score}]`, `count` | E2E-local |
| vectorstore | `qdrant` | **`url`**, `api_key`, `collection_prefix`, `vector_size`, `search_api` | same keys | Mock-contract + Agent replay |
| store | `sqlite` | `path` | `put/get/query` | E2E-local |
| shortlink | `sqlite` | `base_url` | `shorten/resolve/record_scan/stats` | E2E-local |
| crm (new) | `csv` (default) | `path` (sample_data/crm/contacts.csv), `segments` | `contacts(segment, limit) -> DataFrame[contact_id,email,first_name,last_name,company,job_title,industry,country,lifecycle_stage]` | Tested end to end on the sample in `tests/connectors` (no agent consumes `crm` yet) |
| crm (new) | `none` | - | empty frame, `available=False` | No-op |
| crm (new) | `salesforce` | **`login_url`**, **`client_id`**, **`client_secret`** or `access_token`+`instance_url`, `object` (Contact/Lead), `api_version`, `field_map`, `lifecycle_values`, `where`, `segments` | same columns | Mock-contract |
| crm (new) | `hubspot` | **`access_token`**, `property_map`, `search_path`, `segments` | same columns | Mock-contract |

### Endpoints and auth at a glance

| Adapter | Endpoint(s) | Auth |
|---|---|---|
| serpapi | `GET serpapi.com/search.json` (engine `google`, `google_news`) | `api_key` query parameter |
| brave | `GET api.search.brave.com/res/v1/web/search`, `/news/search` | `X-Subscription-Token` |
| tavily | `POST api.tavily.com/search` | Bearer |
| ga4 | `POST analyticsdata.googleapis.com/v1beta/properties/{id}:runReport` | Service account, JWT bearer grant (google-auth if installed, else `cryptography`) |
| adobe_analytics | `POST analytics.adobe.io/api/{company_id}/reports` | Adobe IMS OAuth server-to-server, plus `x-api-key` and `x-proxy-global-company-id` |
| gsc | `POST searchconsole.googleapis.com/webmasters/v3/sites/{site}/searchAnalytics/query` | Service account, JWT bearer grant |
| dataforseo | `POST api.dataforseo.com/v3/dataforseo_labs/google/{ranked_keywords,keyword_suggestions}/live` | HTTP Basic |
| aem | `{author}/adobe/sites/cf/fragments` (+ `/publish`), or legacy `/api/assets/...` | Bearer (static token or IMS) |
| contentful | `api.contentful.com/spaces/{space}/environments/{env}/entries/{id}` (+ `/published`) | Bearer (CMA token) |
| wordpress | `{site}/wp-json/wp/v2/posts` and `/pages` | HTTP Basic with an application password |
| braze | `POST {rest}/templates/email/create` or `/content_blocks/create` | Bearer (REST API key) |
| mailchimp | `POST {dc}.api.mailchimp.com/3.0/templates` (+ draft `/campaigns`) | HTTP Basic (API key) |
| ajo | `POST platform.adobe.io/ajo/content/templates` | Adobe IMS, plus `x-api-key`, `x-gw-ims-org-id`, `x-sandbox-name` |
| jira | `POST {site}/rest/api/3/issue` (ADF description) | HTTP Basic (email + API token) or Bearer PAT |
| azure_devops | `POST dev.azure.com/{org}/{project}/_apis/wit/workitems/${type}?api-version=7.1` | HTTP Basic with a PAT |
| optimizely | `GET api.optimizely.com/v2/experiments`, `/{id}`, `/{id}/results` | Bearer |
| adobe_target | `GET mc.adobe.io/{tenant}/target/activities`, `/activities/{ab,xt}/{id}`, `/report/performance` | Adobe IMS, plus `X-Api-Key` |
| launchdarkly | `POST/GET/PATCH app.launchdarkly.com/api/v2/flags/{project}` | `Authorization: <token>` (no Bearer prefix) |
| clarity | `GET www.clarity.ms/export-data/api/v1/project-live-insights` | Bearer |
| zoom | `api.zoom.us/v2/webinars/...`, `/report/webinars/...`, `/meetings/{id}/recordings` | Server-to-Server OAuth (`account_credentials`) |
| deepl | `POST api.deepl.com/v2/translate` (or `api-free.deepl.com` for `:fx` keys) | `DeepL-Auth-Key` |
| azure (translation) | `POST api.cognitive.microsofttranslator.com/translate?api-version=3.0` | `Ocp-Apim-Subscription-Key`, `-Region` |
| sqlalchemy | any SQLAlchemy URL | whatever the URL or driver carries |
| qdrant | `{url}/collections/{c}`, `/points`, `/points/query`, `/points/count` | `api-key` header (Qdrant Cloud) |
| salesforce | `{login_url}/services/oauth2/token`, `{instance}/services/data/v62.0/query` | OAuth client credentials (or a static token) |
| hubspot | `POST api.hubapi.com/crm/v3/objects/contacts/search` | Bearer (private app token) |

## Settings to add

This block is already merged into `config/settings.yaml`; it is reproduced here for reference. It drives
every adapter from the environment. Every key is optional until its
provider is selected. One block serves every provider of a kind, so a shared key such as `base_url`
means "the active provider's" value, and `base_url` also overrides a SaaS adapter's API host. Leave
it empty when switching to Contentful, Braze or another SaaS default, or put the second provider's
keys under `connectors.<kind>.providers.<provider>`.

```yaml
connectors:
  keyword_data:   # + dataforseo
    login: ${DATAFORSEO_LOGIN:-}
    password: ${DATAFORSEO_PASSWORD:-}
  behaviour:      # + clarity
    api_token: ${CLARITY_API_TOKEN:-}
  experiments:    # + optimizely | adobe_target
    api_token: ${OPTIMIZELY_API_TOKEN:-}
    project_id: ${OPTIMIZELY_PROJECT_ID:-}
    tenant: ${ADOBE_TARGET_TENANT:-}
    client_id: ${ADOBE_CLIENT_ID:-}
    client_secret: ${ADOBE_CLIENT_SECRET:-}
  cms:            # + aem | contentful | wordpress
    base_url: ${CAF_CMS_BASE_URL:-}            # the active CMS: AEM author URL or WordPress site URL (empty for Contentful)
    access_token: ${AEM_ACCESS_TOKEN:-}
    parent_path: ${AEM_PARENT_PATH:-}          # e.g. /content/dam/<site>/{locale}/articles
    model_path: ${AEM_MODEL_PATH:-}
    space_id: ${CONTENTFUL_SPACE_ID:-}
    management_token: ${CONTENTFUL_MANAGEMENT_TOKEN:-}
    username: ${WP_USERNAME:-}
    application_password: ${WP_APP_PASSWORD:-}
  esp:            # + braze | mailchimp | ajo
    api_key: ${CAF_ESP_API_KEY:-}              # Braze REST key or Mailchimp API key
    rest_endpoint: ${BRAZE_REST_ENDPOINT:-}
    list_id: ${MAILCHIMP_LIST_ID:-}
    client_id: ${ADOBE_CLIENT_ID:-}
    client_secret: ${ADOBE_CLIENT_SECRET:-}
    org_id: ${ADOBE_ORG_ID:-}
    sandbox: ${AJO_SANDBOX:-prod}
  tasks:          # + jira | azure_devops
    base_url: ${CAF_TASKS_BASE_URL:-}          # the active tracker: Jira site URL (empty for Azure DevOps Services)
    email: ${JIRA_EMAIL:-}
    api_token: ${JIRA_API_TOKEN:-}
    project_key: ${JIRA_PROJECT_KEY:-}
    organization: ${ADO_ORGANIZATION:-}
    project: ${ADO_PROJECT:-}
    pat: ${ADO_PAT:-}
  webinar:        # + zoom
    account_id: ${ZOOM_ACCOUNT_ID:-}
    client_id: ${ZOOM_CLIENT_ID:-}
    client_secret: ${ZOOM_CLIENT_SECRET:-}
    webinar_id: ${ZOOM_WEBINAR_ID:-}
  translation:    # + deepl | azure
    api_key: ${CAF_TRANSLATION_API_KEY:-}
    region: ${AZURE_TRANSLATOR_REGION:-}
  flags:          # + launchdarkly
    api_token: ${LAUNCHDARKLY_API_TOKEN:-}
    project_key: ${LAUNCHDARKLY_PROJECT_KEY:-}
    environment: ${LAUNCHDARKLY_ENVIRONMENT:-}
  vectorstore:    # + qdrant
    url: ${QDRANT_URL:-}
    api_key: ${QDRANT_API_KEY:-}
  crm:            # new kind
    provider: ${CAF_CRM_PROVIDER:-csv}         # none | csv | salesforce | hubspot
    path: sample_data/crm/contacts.csv
    login_url: ${SALESFORCE_LOGIN_URL:-}
    client_id: ${SALESFORCE_CLIENT_ID:-}
    client_secret: ${SALESFORCE_CLIENT_SECRET:-}
    access_token: ${HUBSPOT_ACCESS_TOKEN:-}
```

Until a `crm:` block exists, `crm.py` registers `csv` as the kind's default (the way every other kind
defaults to its local provider), so `hub.get("crm")` works offline today.

Optional Python packages: `google-auth` (the `google` extra; otherwise `cryptography` signs the
service-account JWT), `sqlalchemy>=2` plus your dialect (`snowflake-sqlalchemy`,
`sqlalchemy-bigquery`, `psycopg`, `databricks-sqlalchemy` ...). Adapters raise a clear
`ConnectorNotConfigured` when one is missing.

## CRM segments

`contacts(segment=...)` means the same thing for every provider: `None` (everyone),
`{"industry": "Insurance", "lifecycle_stage": ["mql", "sql"]}` (AND of equality / any-of, pushed
down to SOQL or HubSpot filters), a named segment (built-ins `subscribers`, `leads`, `mqls`, `sqls`,
`opportunities`, `customers`, `prospects`, or `connectors.crm.segments`) or the inline form
`"industry=Insurance;lifecycle_stage=mql,sql"`. Lifecycle stages are normalised to
subscriber | lead | mql | sql | opportunity | customer | evangelist | other. The sample's 60 fictional
contacts (all at `.example` domains) include 12 people who also appear in the webinar attendee
sample, so CRM and webinar data can be joined on email.

## Known limitations (by design, documented in each adapter)

* **Clarity** exports aggregates, not raw events. `events()` reconstructs Clarity's counts in the
  common schema: one `pageview` per session, one `dead_click` / `rage_click` / `quickback_click` /
  `error_click` / `excessive_scroll` / `script_error` row per occurrence on synthetic session ids,
  and one `scroll` row per page and device with the average depth. Coordinates, selectors and page
  height are NaN. Viewports are nominal per device class. Pages above `max_sessions_per_page` are
  down-sampled proportionally. Selector-level click maps, scroll-reach curves and false-bottom
  detection need the raw `csv` export. `heatmap_analysis` runs on Clarity without error, but it
  reads "no `click` events" as 0% CTA CTR. **Suggested agent changes:** check
  `conn.provider == "clarity"` (or `events().attrs["aggregated"]`) and report Clarity's
  dead/rage-click counts (or `conn.insights()`) instead of CTRs. Also guard `device_split` against
  a device whose sessions all have NaN scroll: if Clarity omits `ScrollDepth` for a device, the
  agent currently fails its own output contract (`median_max_scroll=None`).
* **Adobe Analytics** has no standard "new visitors" or "engaged sessions" metric.
  `engaged_sessions = visits - bounces`, and `new_users` stays NaN unless `metric_map.new_users`
  points at a calculated metric. The page dimension is the page *name* unless `page_dimension`
  points at a path eVar or prop.
* **date_bounds()** on GA4 and Adobe returns the first and last day *with data*, like the CSV's
  min/max, asked of the platform once per day. GA4 searches its whole history. Adobe searches
  `data_start` (or the trailing `lookback_days`, default 365) up to yesterday. Daily rows are passed
  through as the platform returns them, so a GA4 day without data stays a gap, which
  `ga4_traffic_report` reports as a tracking issue.
* **DataForSEO** values are third-party estimates (`df.attrs["estimated"] = True`). Topic-only
  suggestions have no page, clicks or position.
* **Optimizely / Adobe Target** definitions carry only what the platform stores. Planning metadata
  (baseline, MDE, guardrails, planned duration) stays empty, so `experiment_review` reports
  `stats.baseline_present` and similar as failures on platform-sourced tests. `upsert()` writes
  `runs/_experiments/<provider>_drafts.json`. It never creates or edits tests in the vendor platform.
  Optimizely result rows partition traffic by `results_segments` (list every value), mirroring the
  local CSVs, which have no `all` row.
* **Zoom** has no resource-click data, so `resources_clicked = 0`, listed in
  `df.attrs["not_provided"]`. Q&A and poll counts are 0 and listed there when those reports are
  unavailable. A `webinar_id` must be configured or passed in.
* **AJO** content templates hold the HTML. The subject and preheader are message-level in AJO, so
  they go into the template description.
* **Mailchimp** only creates a *draft* campaign (when a list is configured). Nothing is scheduled or
  sent. **LaunchDarkly** flags are created with targeting off. Only `preview=False` with an
  `environment` turns targeting on.
* **DeepL / Azure glossaries:** ad-hoc `glossary` terms are inserted in their target form
  (no inflection). For inflection-aware terminology use a DeepL `glossary_id`. Azure's
  dynamic-dictionary markup is not used because it requires English on one side.

## Not confirmed against vendor documentation

Checked against public docs in October 2026 where possible. These details could not be confirmed and
are flagged in the adapter docstrings:

* **AJO**: the create response body. The template id is read from JSON `id` or the `Location`
  header. The payload follows Adobe community examples and KB ka-26320 (send no `Accept` header on
  create).
* **AEM Sites API**: the field `type` values (`text`, `long-text`) and `mimeType` on long-text
  fields, and the full publish body (only `ids` is sent). The Assets HTTP API's Content Fragment
  support is deprecated by Adobe and kept as `api: assets` for older tenants.
* **Optimizely**: the results response schema (`reach.variations[*].count`,
  `metrics[*].results[*].value`). The current reference page lists the parameters but no response
  schema. Revenue is assumed to be in cents (`revenue_divisor`).
* **Adobe Target**: whether `report.statistics.experiences[]` nests counts under `visitor.totals` or
  `totals.visitor.totals` (both are parsed). The IMS scopes are defaults and must match the Developer
  Console credential, as for Adobe Analytics and AJO.
* **Clarity**: metric and field names other than `Traffic` (`DeadClickCount`, `sessionsCount`,
  `subTotal`, `averageScrollDepth` ...). They come from observed exports and are matched
  case-insensitively.
* **DataForSEO**: item field paths, taken from the docs' filter and `order_by` examples.
* **Brave**: per-result `page_age` / `meta_url` (with `age` as fallback). **Tavily**: `start_date`
  for recency (the old `days` parameter is gone from the current spec). **SerpAPI**: `num` for the
  `google_news` engine (not documented, so results are truncated client-side).
* **Adobe Analytics**: the maximum `settings.limit` (the default page size is 10,000). Metric ids
  `metrics/bounces` and `metrics/timespent` are standard but were not re-verified.

## Tests

```bash
python3 -m pytest tests/connectors -q
python3 -c "from caf.connectors import providers, load_connectors; load_connectors(); print(providers())"
```

`tests/connectors/test_providers.py` covers, for every adapter: the registry, `external = True`,
the docstring contract, construction without network, `ConnectorNotConfigured` on missing config,
request method/URL/auth/body, and the response mapping compared with the local provider's actual
output on `sample_data/`. It also covers retry/backoff and secret-safe errors, and swapping
providers through `config/settings.yaml` plus environment variables.
`tests/connectors/test_agent_swaps.py` runs `ga4_traffic_report`, `experiment_planning`,
`experiment_summary`, `webinar_followup_email` and `industry_marketing` unchanged on GA4, Adobe
Analytics, Optimizely, Zoom and Qdrant replays of the sample data, and checks that they reach the
local provider's result. It also runs `heatmap_analysis` on Clarity and `content_translation` on
DeepL. A live check against each vendor
(with real credentials and a sandbox account) is still required before production use.

## Adding an adapter

1. Subclass the kind's interface together with `ExternalProvider` from `_http.py`, decorate it with
   `@connector(kind, provider)`, and set `external = True`, `required = (...)` and `base_url`.
2. Use `self.http` (timeouts, retries, test transport) and `check()` / `expect_json()` for errors.
   For OAuth, use `adobe_ims_token`, `google_access_token` or `client_credentials_token`.
3. Return the local provider's exact columns or keys. Add a mock-contract test that compares the
   adapter's output with the local provider's.
