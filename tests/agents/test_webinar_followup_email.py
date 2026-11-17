"""Webinar Follow Up Email: YAML rule engine, MQL hand-off, sub-agent reuse, emailkit, gated ESP export."""

from __future__ import annotations

import csv
from pathlib import Path

import pandas as pd

from caf.agents.content.webinar_followup_email import (
    FollowupEmail,
    eval_rule,
    lead_score,
    normalise_attendees,
    resolve_columns,
    segment_attendees,
)
from caf.core import RunContext, get_agent
from caf.core.llm import LLM


class FakeLLM(LLM):
    def __init__(self, responder):
        from caf.config import get_settings

        super().__init__(get_settings().llm, provider="anthropic")
        self.responder = responder
        self.calls: list[str] = []

    def structured(self, prompt, schema, *, system=None, tier="balanced", images=None, retries=1, meter=None):
        self.calls.append(schema.__name__)
        if meter is not None:
            meter.add({})
        return schema.model_validate(self.responder(schema, prompt))


def _rules(ctx):
    return ctx.skill("webinar_segments")


def test_segments_and_mql_on_sample_attendance(ctx):
    res = get_agent("webinar_followup_email").run({}, ctx)
    assert res.status == "needs_approval", res.error
    out = res.output
    counts = {s["segment"]: s["count"] for s in out["segments"]}
    assert counts == {"no_show": 60, "early_drop": 35, "attended_full": 55}
    assert out["registered"] == 150 and out["attended"] == 90 and out["attendance_rate"] == 0.6
    assert out["mql_count"] == 57  # engaged (>=1 question or >=2 polls) in attended segments
    runs = Path(ctx.settings.runs_dir)
    for s in out["segments"]:
        rows = list(csv.DictReader((runs / s["recipients_artifact"]).open(encoding="utf-8")))
        assert len(rows) == s["count"] and {r["segment"] for r in rows} == {s["segment"]}
    mql = list(csv.DictReader((runs / out["mql_artifact"]).open(encoding="utf-8")))
    assert len(mql) == 57 and all(int(r["lead_score"]) > 0 for r in mql)
    assert {r["lead_type"] for r in mql} == {"business", "consumer"}


def test_takeaways_come_from_the_content_summary_sub_agent(ctx):
    res = get_agent("webinar_followup_email").run({}, ctx)
    out = res.output
    assert out["takeaways_method"] == "extractive" and 3 <= len(out["takeaways"]) <= 5
    transcript = open("sample_data/webinar/transcript.txt", encoding="utf-8").read()
    assert all(t.split(" ")[0] in transcript for t in out["takeaways"])
    assert any(a.path.endswith("content_summary/summary.md") for a in res.artifacts)
    assert any(r.startswith("content_summary:") for r in res.provenance["degraded_reasons"])
    assert "ingestion:local" in res.provenance["connectors"] and "webinar:csv" in res.provenance["connectors"]


def test_emails_are_rendered_linted_tagged_and_gated(ctx):
    res = get_agent("webinar_followup_email").run({}, ctx)
    actions = {a.id: a for a in res.pending_actions}
    assert set(actions) == {"esp-no_show", "esp-early_drop", "esp-attended_full"}
    for a in actions.values():
        assert a.kind == "esp.export" and a.requires_approval and a.status == "pending"
        assert {"name", "subject", "preheader", "html", "text", "mjml", "audience", "metadata"} <= set(a.payload)
        assert "{{unsubscribe_url}}" in a.payload["html"]
    seg = {s["segment"]: s for s in res.output["segments"]}
    assert "utm_medium=email" in seg["no_show"]["email"]["cta_url"] and "utm_content=no_show" in seg["no_show"]["email"]["cta_url"]
    assert "/slides.pdf" in seg["attended_full"]["email"]["cta_url"]
    assert seg["no_show"]["email"]["cta_text"] == "Watch the recording"  # rule-pack CTA when offline
    assert any(c.rule == "compliance.unsubscribe" and c.target == "no_show" for c in res.checks)


def test_online_drafts_one_email_per_segment_and_keeps_rule_cta():
    from caf.agents.content.content_summary import ChunkNotes, SummaryDraft

    takeaways = ["Check your rebuild value every year.", "Impact-rated roofs file 41 percent fewer claims.", "Report damage on day one."]

    def responder(schema, prompt):
        if schema is ChunkNotes:  # the Content Summary sub-agent's MAP step
            return {"key_points": ["Rebuild values lag."], "facts": [], "action_items": [], "entities": []}
        if schema is SummaryDraft:  # ... and its REDUCE step
            return {"title": "Webinar", "summary": "Summary.", "key_points": takeaways, "abstract": "A.", "meta_description": "M."}
        assert schema is FollowupEmail
        assert "Hi {{first_name}}," in prompt and all(t in prompt for t in takeaways)
        return {"subject": "Your recording is ready", "preheader": "Catch the three ideas homeowners asked about most this week.",
                "body_markdown": "Hi {{first_name}},\n\n" + "We covered rebuild values, roofs and leaks. " * 8, "cta_text": "Click here to see the full recording now"}

    llm = FakeLLM(responder)
    res = get_agent("webinar_followup_email").run({}, RunContext.create(llm=llm))
    assert res.output["takeaways"] == takeaways and res.output["takeaways_method"] == "abstractive"
    assert llm.calls.count("SummaryDraft") == 1 and llm.calls.count("ChunkNotes") >= 2
    assert llm.calls.count("FollowupEmail") == 3
    assert res.metrics["llm_calls"] == len(llm.calls)  # sub-agent model calls roll up via ctx.run_subagent
    for s in res.output["segments"]:
        assert s["email"]["subject"] == "Your recording is ready"
        assert s["email"]["cta_text"] != "Click here to see the full recording now"  # > 5 words -> rule-pack CTA


def test_rule_engine_and_column_aliases(ctx):
    rules = _rules(ctx)
    raw = pd.DataFrame({"Email Address": ["a@x.example", "b@x.example", "c@x.example", "bad"],
                        "Attended (Yes/No)": ["Yes", "Yes", "No", "Yes"], "Time in Session (minutes)": [55, 10, 0, 50],
                        "Questions Asked": [0, 1, 0, 0], "Poll Responses": [3, 0, 0, 0], "Company Name": ["Acme", "Individual", "", "Z"]})
    colmap = resolve_columns(list(raw.columns), rules["columns"])
    assert colmap["email"] == "Email Address" and colmap["minutes"] == "Time in Session (minutes)"
    people = normalise_attendees(raw, colmap, 60)
    seg = segment_attendees(people, rules)
    assert list(seg["segment"]) == ["attended_full", "early_drop", "no_show", "attended_full"]
    assert list(seg["engaged"]) == [True, True, False, False]
    assert list(seg["mql"]) == [True, True, False, False]
    assert list(seg["lead_type"])[:2] == ["business", "consumer"]
    assert eval_rule({"x": 3}, {"any": [{"field": "x", "op": ">=", "value": 5}, {"field": "x", "op": "in", "value": [3]}]})
    assert lead_score({"watch_pct": 1.0, "questions": 2, "polls": 3, "resources": 1}, rules["mql"]["score_weights"]) == 100
