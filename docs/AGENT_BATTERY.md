# Agent battery: results

110 of 110 agents pass all of their trials (1732 of 1732 trials). Made by `caf agents certify`; what each trial does is in `src/caf/certify.py` and `docs/AGENT_QUALITY.md`.

No live model was called: the model trials use a scripted model that misbehaves on purpose, every other trial the offline model that answers from this layer's own samples.

| Pack | Agents | Pass every trial | Trials | Failed trials |
|---|---|---|---|---|
| blueprint | 28 | 28 | 421 | 0 |
| arm_sdlc | 51 | 51 | 843 | 0 |
| campaign_studio | 18 | 18 | 291 | 0 |
| intellisight | 5 | 5 | 80 | 0 |
| voyagerai | 8 | 8 | 97 | 0 |

## Every agent

| Agent | Pack | Trials | Result |
|---|---|---|---|
| `campaign_brief_generation` | blueprint | 16 | pass |
| `keyword_research` | blueprint | 16 | pass |
| `competitive_insights` | blueprint | 15 | pass |
| `utm_generation` | blueprint | 8 | pass |
| `email_creation` | blueprint | 16 | pass |
| `blog_post_creation` | blueprint | 16 | pass |
| `linkedin_inmail_copy` | blueprint | 16 | pass |
| `content_adaptation` | blueprint | 16 | pass |
| `webinar_followup_email` | blueprint | 16 | pass |
| `industry_marketing` | blueprint | 16 | pass |
| `press_release_creation` | blueprint | 16 | pass |
| `quote_extraction` | blueprint | 15 | pass |
| `qr_code_generation` | blueprint | 7 | pass |
| `content_summary` | blueprint | 15 | pass |
| `geo_recommendations` | blueprint | 16 | pass |
| `technical_seo_recommendations` | blueprint | 15 | pass |
| `ga4_traffic_report` | blueprint | 15 | pass |
| `web_accessibility_evaluation` | blueprint | 15 | pass |
| `heatmap_analysis` | blueprint | 16 | pass |
| `content_model_creation` | blueprint | 15 | pass |
| `content_translation` | blueprint | 16 | pass |
| `experiment_ideation` | blueprint | 16 | pass |
| `experiment_planning` | blueprint | 16 | pass |
| `variation_development` | blueprint | 16 | pass |
| `experiment_review` | blueprint | 15 | pass |
| `experiment_summary` | blueprint | 15 | pass |
| `exploration_generation` | blueprint | 16 | pass |
| `chart_summary` | blueprint | 15 | pass |
| `arm_front_velocity` | arm_sdlc | 16 | pass |
| `arm_design_velocity` | arm_sdlc | 18 | pass |
| `arm_app_velocity` | arm_sdlc | 16 | pass |
| `arm_intellisight` | arm_sdlc | 17 | pass |
| `arm_site_auditor` | arm_sdlc | 16 | pass |
| `arm_brand_auditor` | arm_sdlc | 16 | pass |
| `arm_quality_digital_workers` | arm_sdlc | 18 | pass |
| `arm_blueswan` | arm_sdlc | 18 | pass |
| `arm_accessibility_validator` | arm_sdlc | 17 | pass |
| `arm_localization_validator` | arm_sdlc | 16 | pass |
| `arm_xtender` | arm_sdlc | 16 | pass |
| `arm_xops` | arm_sdlc | 16 | pass |
| `arm_migrate_dxp` | arm_sdlc | 16 | pass |
| `arm_devops_runbook` | arm_sdlc | 17 | pass |
| `arm_sitecore_brief_generator` | arm_sdlc | 16 | pass |
| `arm_sitecore_researcher` | arm_sdlc | 16 | pass |
| `arm_sitecore_strategy` | arm_sdlc | 16 | pass |
| `arm_sitecore_competitor_analyzer` | arm_sdlc | 16 | pass |
| `arm_sitecore_audience_summary` | arm_sdlc | 16 | pass |
| `arm_sitecore_market_signals` | arm_sdlc | 16 | pass |
| `arm_sitecore_persona_auditor` | arm_sdlc | 16 | pass |
| `arm_sitecore_design_studio` | arm_sdlc | 17 | pass |
| `arm_sitecore_structured_extractor` | arm_sdlc | 16 | pass |
| `arm_sitecore_marketer_mcp` | arm_sdlc | 18 | pass |
| `arm_sitecore_validate_accessibility` | arm_sdlc | 17 | pass |
| `arm_sitecore_validate_page` | arm_sdlc | 17 | pass |
| `arm_sitecore_performance` | arm_sdlc | 18 | pass |
| `arm_sitecore_translator` | arm_sdlc | 16 | pass |
| `arm_sitecore_content_generator` | arm_sdlc | 16 | pass |
| `arm_sitecore_forms` | arm_sdlc | 17 | pass |
| `arm_sitecore_app_studio` | arm_sdlc | 17 | pass |
| `arm_sitecore_cloud_sdk` | arm_sdlc | 17 | pass |
| `arm_sitecore_deploy_api` | arm_sdlc | 17 | pass |
| `arm_sitecore_publishing_api` | arm_sdlc | 18 | pass |
| `arm_factory_nfr_spec` | arm_sdlc | 16 | pass |
| `arm_factory_unit_test` | arm_sdlc | 17 | pass |
| `arm_factory_test_case_drafter` | arm_sdlc | 16 | pass |
| `arm_factory_pipeline_quality` | arm_sdlc | 16 | pass |
| `arm_factory_asana_sync` | arm_sdlc | 17 | pass |
| `arm_factory_test_reviewer` | arm_sdlc | 16 | pass |
| `arm_factory_test_strategy` | arm_sdlc | 16 | pass |
| `arm_factory_automation_planner` | arm_sdlc | 16 | pass |
| `arm_factory_automation_scripter` | arm_sdlc | 16 | pass |
| `arm_factory_perf_test` | arm_sdlc | 17 | pass |
| `arm_factory_vuln_scan` | arm_sdlc | 17 | pass |
| `arm_factory_defect_logger` | arm_sdlc | 17 | pass |
| `arm_factory_code_reviewer` | arm_sdlc | 16 | pass |
| `arm_factory_tracking_implementer` | arm_sdlc | 16 | pass |
| `arm_factory_onetrust_classifier` | arm_sdlc | 16 | pass |
| `arm_factory_evaluator` | arm_sdlc | 16 | pass |
| `arm_factory_tool_smith` | arm_sdlc | 16 | pass |
| `cs_market_insight` | campaign_studio | 16 | pass |
| `cs_measurement` | campaign_studio | 16 | pass |
| `cs_brief_intake` | campaign_studio | 16 | pass |
| `cs_taxonomy_reuse` | campaign_studio | 16 | pass |
| `cs_routing` | campaign_studio | 16 | pass |
| `cs_foresight_history` | campaign_studio | 16 | pass |
| `cs_foresight_persona` | campaign_studio | 16 | pass |
| `cs_foresight_evaluation` | campaign_studio | 16 | pass |
| `cs_foresight_research` | campaign_studio | 16 | pass |
| `cs_xtender_variants` | campaign_studio | 16 | pass |
| `cs_compliance_check` | campaign_studio | 16 | pass |
| `cs_legal_market_rules` | campaign_studio | 16 | pass |
| `cs_legal_hq_code` | campaign_studio | 16 | pass |
| `cs_legal_claims` | campaign_studio | 16 | pass |
| `cs_design_velocity_check` | campaign_studio | 16 | pass |
| `cs_consent_identity` | campaign_studio | 17 | pass |
| `cs_adobe_publish` | campaign_studio | 17 | pass |
| `cs_adobe_send` | campaign_studio | 17 | pass |
| `is_data_interpreter` | intellisight | 16 | pass |
| `is_analytics_engine` | intellisight | 16 | pass |
| `is_custom_report` | intellisight | 16 | pass |
| `is_recommendations` | intellisight | 16 | pass |
| `is_analytics_chat` | intellisight | 16 | pass |
| `vai_data_ingestion` | voyagerai | 8 | pass |
| `vai_data_modelling` | voyagerai | 8 | pass |
| `vai_identity_stitching` | voyagerai | 8 | pass |
| `vai_predictive_propensity` | voyagerai | 8 | pass |
| `vai_personalization` | voyagerai | 16 | pass |
| `vai_content_generation` | voyagerai | 16 | pass |
| `vai_omnichannel_activation` | voyagerai | 17 | pass |
| `vai_continuous_feedback` | voyagerai | 16 | pass |
