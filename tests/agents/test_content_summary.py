"""Content Summary: ingestion + semantic chunking + parallel map-reduce, deterministic guards."""

from __future__ import annotations

from typing import Any

import pytest

from caf.agents.content.content_summary import (
    META_MAX_CHARS,
    ChunkNotes,
    SummaryDraft,
    chunk_body,
    clean_title,
    compose_markdown,
    detect_action_items,
    fit_meta_description,
    group_by_budget,
    looks_like_transcript,
    transcript_paragraphs,
    ungrounded_numbers,
)
from caf.core import RunContext, get_agent
from caf.core.llm import LLM
from caf.core.textutil import canonical, chunk_text


class FakeLLM(LLM):
    """Online-looking model (no network): answers structured calls from a responder function."""

    def __init__(self, responder, provider: str = "anthropic"):
        from caf.config import get_settings

        super().__init__(get_settings().llm, provider=provider)
        self.responder = responder
        self.calls_by_schema: dict[str, int] = {}

    def structured(self, prompt, schema, *, system=None, tier="balanced", images=None, retries=1, meter=None):
        self.calls_by_schema[schema.__name__] = self.calls_by_schema.get(schema.__name__, 0) + 1
        if meter is not None:
            meter.add({})
        return schema.model_validate(self.responder(schema, prompt))

    def complete(self, prompt, **kw):  # pragma: no cover - not used by this agent
        return "ok"


def _responder(schema, prompt) -> dict[str, Any]:
    if schema is ChunkNotes:
        return {"key_points": ["Water damage is nearly one in three home claims."], "facts": ["The average water claim is $14,200."],
                "action_items": [{"action": "Check your rebuild value every year", "owner": None, "due": None}], "entities": ["Meridian Mutual"]}
    assert schema is SummaryDraft
    return {"title": "Home risk in 2026", "summary": "Water now drives 31% of claims and 99 storms hit inland in 2026.",
            "key_points": ["Water is the everyday catastrophe.", "Roofs matter.", "Underinsurance is widespread."],
            "action_items": [{"action": "Check your rebuild value", "owner": "Homeowners", "due": None}],
            "abstract": "A short abstract.", "meta_description": "x" * 120 + ", and a long tail that pushes the description beyond the limit."}


def test_extractive_summary_offline_is_verbatim_and_map_reduced(ctx):
    agent = get_agent("content_summary")
    res = agent.run({"path": "sample_data/docs/whitepaper.md", "mode": "executive", "chunk_words": 300}, ctx)
    assert res.status == "degraded", res.error
    out = res.output
    assert out["method"] == "extractive" and out["chunks_processed"] >= 5
    assert out["title"] == "The 2026 Home Risk Outlook"
    src = canonical(open("sample_data/docs/whitepaper.md", encoding="utf-8").read().replace("**", ""))
    for kp in out["key_points"]:
        assert canonical(kp) in src, f"key point is not verbatim: {kp}"
    assert len(out["meta_description"]) <= META_MAX_CHARS
    assert 0 < out["compression_ratio"] < 0.2
    assert "offline model" in " ".join(res.provenance["degraded_reasons"])
    assert res.metrics["llm_calls"] == 0  # offline: no placeholder calls, real extractive output


def test_graph_fans_out_with_send():
    outline = get_agent("content_summary").graph_outline()
    assert ["chunk", "map"] in outline["edges"] and ["map", "reduce"] in outline["edges"]


def test_online_map_reduce_collapse_and_guards():
    llm = FakeLLM(_responder)
    ctx = RunContext.create(llm=llm)
    res = get_agent("content_summary").run({"path": "sample_data/docs/whitepaper.md", "mode": "executive", "chunk_words": 200,
                                            "reduce_budget_words": 400}, ctx)
    assert res.status in ("ok", "degraded"), res.error
    out = res.output
    n_chunks = out["chunks_processed"]
    assert llm.calls_by_schema["ChunkNotes"] >= n_chunks  # one MAP call per chunk (+ collapse calls)
    assert out["collapse_levels"] >= 1  # notes exceeded the budget -> hierarchical collapse
    assert llm.calls_by_schema["SummaryDraft"] == 1
    assert out["method"] == "abstractive"
    assert len(out["meta_description"]) <= META_MAX_CHARS
    rules = {c.rule: c for c in res.checks}
    assert rules["meta_description.length"].status == "warn"  # model's 180-char meta was trimmed
    grounded = rules["summary.numbers_grounded"]
    assert grounded.status == "warn" and "99" in grounded.data["ungrounded"] and "31%" not in grounded.data["ungrounded"]


def test_confidential_never_sends_text_to_a_cloud_model():
    llm = FakeLLM(_responder, provider="anthropic")
    res = get_agent("content_summary").run({"path": "sample_data/docs/interview.txt", "confidential": True}, RunContext.create(llm=llm))
    assert res.output["method"] == "extractive"
    assert llm.calls_by_schema == {}
    assert any("confidential" in r for r in res.provenance["degraded_reasons"])


def test_local_provider_is_allowed_for_confidential():
    from caf.agents.content.content_summary import is_local_provider

    assert is_local_provider(FakeLLM(_responder, provider="ollama"))
    assert not is_local_provider(FakeLLM(_responder, provider="anthropic"))


@pytest.mark.parametrize("mode", ["bullet", "abstract", "action_items", "meta_description", "technical"])
def test_all_modes_render(ctx, mode):
    res = get_agent("content_summary").run({"path": "sample_data/docs/case_study.docx", "mode": mode}, ctx)
    assert res.ok, res.error
    assert res.output["mode"] == mode and res.output["summary_markdown"].strip()
    if mode == "meta_description":
        assert res.output["summary_markdown"] == res.output["meta_description"]


def test_pdf_title_and_url_ingestion(ctx):
    pdf = get_agent("content_summary").run({"path": "sample_data/docs/board_report.pdf", "mode": "bullet"}, ctx)
    assert pdf.output["title"] == "Meridian Mutual - Board Report, Q3 2026"
    page = get_agent("content_summary").run({"source": "https://www.meridian-mutual.example/blog/claims-tips.html", "mode": "bullet"}, ctx)
    assert page.ok and page.output["source_words"] > 100


def test_transcript_turns_stay_whole():
    text = open("sample_data/webinar/transcript.txt", encoding="utf-8").read()
    assert looks_like_transcript(text)
    for chunk in chunk_text(transcript_paragraphs(text), max_words=300, overlap_words=0):
        for para in chunk.split("\n\n"):
            assert para.count("[00:") <= 1  # never two turns merged into one paragraph


def test_meta_description_fitting():
    long = "Water damage now accounts for nearly one in three home claims we pay, and the average water claim has risen 27 percent since 2022, so check your cover."
    fitted = fit_meta_description(long + " " + long)
    assert len(fitted) <= META_MAX_CHARS and fitted.endswith((".", "..."))
    assert fit_meta_description("Short and sweet.") == "Short and sweet."


def test_numbers_grounding_and_actions():
    assert ungrounded_numbers("Claims rose 27% in 2026.", "Claims rose 27% in 2026.") == []
    assert ungrounded_numbers("We paid 3,500 claims.", "We paid 3500 claims.") == []
    assert ungrounded_numbers("We paid 4,100 claims.", "We paid 3500 claims.") == ["4100"]
    acts = detect_action_items("The claims team will publish a prevention checklist by Q1 2027. Water is wet. Homeowners should test sump pumps every autumn.")
    owners = {a["owner"] for a in acts}
    assert "claims team" in owners and "Homeowners" in owners
    assert any(a["due"] == "Q1 2027" for a in acts)


def test_helpers():
    assert chunk_body("tail words here\n\nReal paragraph.", 1, 30) == "Real paragraph."
    assert chunk_body("First chunk.", 0, 30) == "First chunk."
    assert group_by_budget(["a " * 10, "b " * 10, "c " * 30], 25) == [[0, 1], [2]]
    assert clean_title("corpus.", "board_report.pdf", "corpus.\nMeridian Mutual - Board Report, Q3 2026\n") == "Meridian Mutual - Board Report, Q3 2026"
    md = compose_markdown("action_items", {"title": "T", "action_items": [{"action": "Do it", "owner": "Ops", "due": "Q1"}], "key_points": []})
    assert "- [ ] Do it _(owner: Ops; due: Q1)_" in md
