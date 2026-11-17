"""Quote Extraction: verifiable anchoring, rejection with reasons, deterministic attribution, heuristic ranker, quote bank."""

from __future__ import annotations

import pytest

from caf.agents.content.quote_extraction import (
    CandidateList,
    anchor_span,
    attribution_near,
    canonical_map,
    clean_candidate,
    interview_roles,
    quotability,
    sentence_spans,
    transcript_speaker,
)
from caf.core import RunContext, get_agent
from caf.core.llm import LLM
from caf.core.textutil import canonical

TRANSCRIPT = "sample_data/webinar/transcript.txt"


class FakeLLM(LLM):
    def __init__(self, quotes):
        from caf.config import get_settings

        super().__init__(get_settings().llm, provider="anthropic")
        self.quotes = quotes

    def structured(self, prompt, schema, *, system=None, tier="balanced", images=None, retries=1, meter=None):
        assert schema is CandidateList
        if meter is not None:
            meter.add({})
        return CandidateList.model_validate({"quotes": self.quotes})


def _text(path: str) -> str:
    return open(path, encoding="utf-8").read()


@pytest.mark.parametrize("path", [TRANSCRIPT, "sample_data/docs/whitepaper.md", "sample_data/docs/interview.txt"])
def test_canonical_map_matches_textutil_canonical(path):
    text = _text(path)
    canon, idx = canonical_map(text)
    assert canon == canonical(text)
    assert len(idx) == len(canon)


def test_anchoring_tolerates_typography_and_maps_offsets():
    text = _text(TRANSCRIPT)
    canon, idx = canonical_map(text)
    # straight quotes, collapsed whitespace and different case still anchor; offsets point into the raw text
    cand = "the best claim is the one that never has to be made, so let me start   before the storm."
    start, end, occ = anchor_span(cand, canon, idx)
    assert canonical(text[start:end]) == canonical(cand) and occ == 1
    assert anchor_span("The best claim is the one that is never made.", canon, idx) is None  # paraphrase


def test_unanchored_and_elided_quotes_are_rejected_with_reasons():
    quotes = [
        {"text": "The best claim is the one that never has to be made, so let me start before the storm.", "speaker": "Priya Nair"},
        {"text": "Meridian guarantees every claim is paid within two days.", "speaker": "Tom Ellery"},  # invented
        {"text": "Resilience is the new renovation... the smartest spend is on roofs.", "speaker": "Dr. Asha Raman"},  # elided
        {"text": "[00:56:18] Dr. Asha Raman: Check your rebuild value this month, and plan one resilience upgrade for 2027.", "speaker": None},
    ] + [{"text": t, "speaker": None} for t in (
        "Water damage now accounts for nearly one in three home claims we pay, and the average water claim has risen 27 percent since 2022.",
        "Climate risk is no longer a coastal problem; in 2026 our three most expensive storm days hit inland ZIP codes.",
        "Members who report a claim within 24 hours are paid nine days sooner on average.")]
    res = get_agent("quote_extraction").run({"file": TRANSCRIPT, "save_to_bank": False}, RunContext.create(llm=FakeLLM(quotes)))
    out = res.output
    reasons = {r["text"][:25]: r["reason"] for r in out["rejected"]}
    assert any("not found in the source" in v for k, v in reasons.items() if k.startswith("Meridian guarantees"))
    assert any("ellipsis" in v for k, v in reasons.items() if k.startswith("Resilience is the new"))
    assert len(out["quotes"]) == 5 and all(q["method"] == "llm" and q["verified"] for q in out["quotes"])
    text = _text(TRANSCRIPT)
    for q in out["quotes"]:
        assert canonical(text[q["char_start"]:q["char_end"]]) == canonical(q["text"])
    best = next(q for q in out["quotes"] if q["text"].startswith("The best claim"))
    assert best["speaker"] == "Tom Ellery" and best["speaker_source"] == "transcript"  # prefix overrides the model's 'Priya Nair'
    assert best["location"]["timestamp"] == "00:21:52"
    prefixed = next(q for q in out["quotes"] if q["text"].startswith("Check your rebuild value this month"))
    assert prefixed["speaker"] == "Dr. Asha Raman"  # copied prefix stripped, still anchored


def test_offline_heuristic_fallback_and_quote_bank_dedupe(ctx):
    agent = get_agent("quote_extraction")
    first = agent.run({"file": TRANSCRIPT}, ctx)
    out = first.output
    assert first.status == "degraded"
    assert 5 <= len(out["quotes"]) <= 10 and all(q["method"] == "heuristic" for q in out["quotes"])
    assert all(r["reason"].startswith("not found") for r in out["rejected"])  # offline placeholders never anchor
    assert all(q["speaker"] in {"Dr. Asha Raman", "Tom Ellery", "Priya Nair"} for q in out["quotes"])
    again = agent.run({"file": TRANSCRIPT}, RunContext.create())
    assert again.output["bank_added"] == 0 and again.output["bank_existing"] == len(again.output["quotes"])
    banked = RunContext.create().connector("store").query("quote_bank", limit=500)
    assert {q["bank_id"] for q in out["quotes"]} <= {b["id"] for b in banked}


def test_document_attribution_patterns(ctx):
    res = get_agent("quote_extraction").run({"file": "sample_data/docs/whitepaper.md", "save_to_bank": False}, ctx)
    by_speaker = {q["speaker"] for q in res.output["quotes"] if q["speaker_source"] == "attribution"}
    assert "Dr. Asha Raman" in by_speaker  # '> "..." - Dr. Asha Raman, Head of Risk Analytics'
    docx = get_agent("quote_extraction").run({"file": "sample_data/docs/case_study.docx", "save_to_bank": False}, ctx)
    assert "Sofia Lindqvist" in {q["speaker"] for q in docx.output["quotes"]}  # name line under a pull-quote
    interview = get_agent("quote_extraction").run({"file": "sample_data/docs/interview.txt", "save_to_bank": False}, ctx)
    assert {q["speaker"] for q in interview.output["quotes"]} == {"Tom Ellery"}  # 'A:' answers -> interviewee


def test_input_requires_content_or_file(ctx):
    res = get_agent("quote_extraction").run({}, ctx)
    assert res.status == "error" and "at least one" in res.error


def test_helpers():
    assert clean_candidate("“[00:01:02] Tom Ellery: Hello there friends.”") == ("Hello there friends.", "Tom Ellery")
    text = "[00:00:05] A: Hello.\nmore words here\n[00:00:09] Bea Lim: Next."
    assert transcript_speaker(text, text.index("more")) == ("A", "00:00:05")
    assert attribution_near('"We did it," said Jane Doe, CEO.', 1, 10) == "Jane Doe"
    assert interview_roles("an interview with Tom Ellery, Claims Director\nInterviewer: Jess Morgan, Editor") == {"A": "Tom Ellery", "Q": "Jess Morgan"}
    texts = ["Dr. Asha Raman leads the team.", "It models risk.", "A bullet that wraps\n  onto a second line."]
    src = "Dr. Asha Raman leads the team. It models risk.\n\n- A bullet that wraps\n  onto a second line."
    assert [src[a:b] for a, b in sentence_spans(src)] == texts
    assert quotability("I have handled flood claims for fifteen years, and the single biggest lesson is that prevention costs less.") > \
        quotability("Welcome everyone, and thanks for joining us today, it is great to see you all here.")
