"""Press Release Creation: AP dateline, structure + legal validator, versioned boilerplate, gated distribution."""

from __future__ import annotations

from datetime import date

from caf.agents.content.press_release_creation import (
    PressReleaseDraft,
    Quote,
    build_dateline,
    check_lead,
    embargo_label,
    find_terms,
    format_quote,
    parse_location,
    validate_dateline,
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
        self.calls.append(prompt)
        if meter is not None:
            meter.add({})
        return schema.model_validate(self.responder(schema, prompt, len(self.calls)))


GOOD = {
    "headline": "Meridian Mutual introduces resilience credits for homeowners",
    "subheadline": "Verified upgrades can earn credits worth up to 12% of the premium from January 2027.",
    "lead": "Meridian Mutual today announced resilience credits that reward members for verified home upgrades such as "
            "impact-rated roofs and leak detection.",
    "body_paragraphs": ["Members who make a verified upgrade will earn credits worth up to 12% of their premium.",
                        "The programme covers impact-rated roofs, roof-to-wall straps, ember-resistant vents and automatic water shut-off.",
                        "Optional flood cover will be extended to every state in which the company writes home insurance."],
    "quotes": [{"text": "Resilience is the new renovation.", "speaker": "Someone Else", "title": "Spokesperson"}],
    "cta": "Members can check which upgrades qualify at www.meridian-mutual.example/resilience.",
}


def _record(ctx):
    return ctx.skill("press_boilerplate")


def test_ap_datelines(ctx):
    rec = _record(ctx)
    d = date(2026, 10, 20)
    assert build_dateline("Springfield, Illinois", d, rec) == "SPRINGFIELD, Ill., Oct. 20, 2026 —"
    assert build_dateline("Springfield, IL", date(2026, 9, 3), rec) == "SPRINGFIELD, Ill., Sept. 3, 2026 —"
    assert build_dateline("Chicago", date(2026, 3, 1), rec) == "CHICAGO, March 1, 2026 —"
    assert build_dateline("Austin, Texas", d, rec) == "AUSTIN, Texas, Oct. 20, 2026 —"
    assert parse_location("London, UK", rec) == ("LONDON", None)
    assert validate_dateline("SPRINGFIELD, Ill., Oct. 20, 2026 —")
    assert validate_dateline("CHICAGO, March 1, 2026 —")
    assert not validate_dateline("Springfield, IL - 10/20/2026")


def test_lead_quote_and_legal_helpers(ctx):
    rec = _record(ctx)
    verbs = rec["style"]["announcement_verbs"]
    ok = check_lead(GOOD["lead"], ["Meridian Mutual"], verbs, date(2026, 10, 20), rec["ap_months"])
    assert ok["who"] and ok["what"] and ok["when"]
    bad = check_lead("Homes are changing fast.", ["Meridian Mutual"], verbs, date(2026, 10, 20), rec["ap_months"])
    assert not (bad["who"] or bad["what"] or bad["when"])
    assert format_quote(Quote(text="We are ready.", speaker="Dana Ruiz", title="Chief Claims Officer"), "Meridian Mutual") == \
        "“We are ready,” said Dana Ruiz, Chief Claims Officer at Meridian Mutual."
    assert set(find_terms("We expect demand and will plan to grow; our forecast holds.", rec["forward_looking_terms"])) >= {"expect", "will", "forecast", "plan to"}
    display, dt, problem = embargo_label("2026-10-21T09:00:00-05:00", rec["ap_months"])
    assert display == "9 a.m. UTC-05:00, Oct. 21, 2026" and problem is None and dt.day == 21


def test_offline_run_inserts_record_and_validates(ctx):
    res = get_agent("press_release_creation").run(get_agent("press_release_creation").selftest_input(), ctx)
    out = res.output
    assert out["dateline"] == "SPRINGFIELD, Ill., Oct. 20, 2026 —"
    assert out["body_markdown"].startswith("SPRINGFIELD, Ill., Oct. 20, 2026 — ")
    assert out["body_markdown"].rstrip().endswith("###")
    assert "## About Meridian Mutual" in out["body_markdown"] and "press@meridian-mutual.example" in out["body_markdown"]
    assert out["media_contact"]["name"] == "Jordan Blake"  # fallback to brand_voice.media_contact
    assert out["quotes"][0]["speaker"] == "Dr. Asha Raman"  # spokesperson reconciled deterministically
    rules = {c.rule: c.status for c in res.checks}
    assert rules["structure.inverted_pyramid"] == "fail"  # offline placeholder lead has no who/what/when
    assert rules["ap.dateline"] == "pass" and rules["structure.end_mark"] == "pass" and rules["structure.boilerplate"] == "pass"
    [action] = res.pending_actions
    assert action.kind == "newswire.distribute" and action.connector == "newswire" and action.method == "distribute"
    assert action.requires_approval and set(action.payload) == {"headline", "body", "embargo", "regions", "metadata"}


def test_forward_looking_release_gets_disclaimer_and_distribution():
    llm = FakeLLM(lambda schema, prompt, n: GOOD)
    inp = {**get_agent("press_release_creation").selftest_input(), "embargo": "2099-01-05T09:00:00-05:00"}
    res = get_agent("press_release_creation").run(inp, RunContext.create(llm=llm))
    out = res.output
    assert out["forward_looking_detected"] and "will" in out["forward_looking_terms"]
    assert "## Forward-looking statements" in out["body_markdown"]
    assert out["release_label"].startswith("EMBARGOED UNTIL") and out["dateline"].endswith("Jan. 5, 2099 —")
    assert "“Resilience is the new renovation,” said Dr. Asha Raman, Head of Risk Analytics at Meridian Mutual." in out["body_markdown"]
    rules = {c.rule: c.status for c in res.checks}
    assert rules["structure.inverted_pyramid"] == "pass" and rules["legal.forward_looking"] == "warn"
    assert rules["structure.quote_attribution"] == "pass"
    [action] = res.pending_actions
    assert action.payload["embargo"] == "2099-01-05T09:00:00-05:00" and action.payload["metadata"]["forward_looking"] is True
    assert len(llm.calls) == 1  # validator passed: no repair call


def test_repair_pass_runs_only_online_on_failures():
    bad = {**GOOD, "lead": "Homes are changing.", "quotes": []}
    llm = FakeLLM(lambda schema, prompt, n: bad if n == 1 else GOOD)
    res = get_agent("press_release_creation").run(get_agent("press_release_creation").selftest_input(), RunContext.create(llm=llm))
    assert len(llm.calls) == 2 and "failed these AP-style / structure checks" in llm.calls[1]
    assert {c.rule: c.status for c in res.checks}["structure.inverted_pyramid"] == "pass"
    assert PressReleaseDraft.model_validate(GOOD).headline in res.output["headline"]
