"""Content Adaptation: the YAML channel registry, deterministic channel validator + fixer (X URL
weighting, SMS segments and opt-out, Instagram links, hashtag / emoji policy, required headings),
the content calendar and the end-to-end run on the sample whitepaper."""

import csv
import io
from datetime import date

import pytest
import yaml

from caf.agents.content import content_adaptation as ca
from caf.config import get_settings
from caf.core import RunContext, get_agent
from caf.core.llm import LLM

REG = yaml.safe_load((get_settings().home / "config/skills/channel_registry.yaml").read_text())
CH = REG["channels"]
URL = "https://www.meridian-mutual.example/products/home-insurance.html"


def test_registry_covers_ten_channels_with_policies():
    assert set(CH) == {"linkedin_post", "x_post", "instagram_caption", "facebook_post", "email_snippet", "web_page_section",
                       "sms", "whatsapp", "youtube_description", "push"}
    for key, spec in CH.items():
        assert spec["label"] and spec["max_chars"] > 0 and spec["structure"] and spec["tone"], key
        assert {"min", "max"} <= set(spec["hashtags"]) and "max" in spec["emoji"] and "allowed" in spec["links"], key


def test_counting_rules():
    assert ca.count_chars(f"Read {URL}", CH["x_post"]) == 5 + 23
    assert ca.sms_segments("a" * 160) == ("GSM-7", 160, 1)
    assert ca.sms_segments("a" * 161) == ("GSM-7", 161, 2)
    assert ca.sms_segments("[x]" + "a" * 156)[1] == 161                                   # GSM-7 extension chars count double
    assert ca.sms_segments("Hi " + "😀")[0] == "UCS-2"
    assert ca.count_emoji("Hi 😀👍🏽 ✅ ok ❤️") == 4                                        # skin tone / VS16 not counted
    assert ca.split_hashtags("Text here. #Glued\n\n#One #Two", ["three", "#one"]) == ("Text here.", ["Glued", "One", "Two", "three"])
    assert ca.compose("Body", ["A", "B"], CH["linkedin_post"]) == "Body\n\n#A #B"
    assert ca.compose("Body", ["A"], CH["sms"]) == "Body"


@pytest.mark.parametrize("channel,text,tags,rule", [
    ("instagram_caption", f"Get ready for winter. {URL}", ["A", "B", "C"], "links_not_allowed"),
    ("sms", f"Flood cover now in every state {URL}", [], "must_include"),
    ("x_post", "One idea.", ["A", "B", "C"], "hashtags_max"),
    ("web_page_section", "Plain paragraph without a heading.", [], "structure_prefix"),
    ("push", "Your renewal is ready 😀😀", [], "emoji_max"),
    ("linkedin_post", "x" * 3100, ["A", "B", "C"], "max_chars"),
])
def test_validator_flags_and_fixer_repairs(channel, text, tags, rule):
    spec = CH[channel]
    issues = ca.validate_adaptation(ca.compose(text, tags, spec), tags, spec, cta_url=URL)
    assert rule in {i["rule"] for i in issues if i["status"] == "fail"}
    fixed, fixed_tags, notes = ca.fix_adaptation(text, tags, spec, cta_url=URL, seed_hashtags=["FloodReady"], title="Home Risk Outlook")
    after = ca.validate_adaptation(ca.compose(fixed, fixed_tags, spec), fixed_tags, spec, cta_url=URL)
    assert not [i for i in after if i["status"] == "fail"], (after, notes)
    if channel == "instagram_caption":
        assert "Link in bio." in fixed and URL not in fixed
    if channel == "sms":
        assert fixed.endswith("Reply STOP to opt out.") and ca.sms_segments(fixed)[2] == 1
    if channel == "web_page_section":
        assert fixed.startswith("## Home Risk Outlook")


def test_figures_not_in_source_are_flagged():
    issues = ca.validate_adaptation("41% of homeowners think flood is covered; 99% agree.", [], CH["email_snippet"],
                                    source_numbers=ca.numbers_in("41% of homeowners surveyed believe a policy covers flood."))
    assert [i["message"] for i in issues if i["rule"] == "figures_not_in_source"] == ["Figures not found in the source: ['99']."]


def test_calendar_uses_preferred_weekdays_on_distinct_days():
    rows = ca.build_calendar([{"channel": c} for c in ("linkedin_post", "x_post", "email_snippet", "sms")], date(2026, 10, 20), CH)
    assert [(r["date"], r["time"]) for r in rows] == [("2026-10-20", "09:00"), ("2026-10-21", "12:00"), ("2026-10-27", "07:30"),
                                                      ("2026-10-31", "10:00")]                # Tue, Wed, next Tue, Sat


def test_reconcile_by_key_label_then_position():
    got = ca.reconcile_channels([{"channel": "LinkedIn post", "text": "a"}, {"channel": "mystery", "text": "b"}, {"channel": "SMS", "text": "c"}],
                                ["sms", "linkedin_post", "x_post"], CH)
    assert {k: v["text"] for k, v in got.items()} == {"linkedin_post": "a", "sms": "c", "x_post": "b"}


def test_whitepaper_to_six_channels_with_calendar(ctx):
    agent = get_agent("content_adaptation")
    inp = agent.selftest_input()
    res = agent.run(inp, ctx)
    assert res.status != "error", res.error
    out = res.output
    assert out["source_title"] == "The 2026 Home Risk Outlook" and out["source"] == "sample_data/docs/whitepaper.md"
    assert [a["channel"] for a in out["adaptations"]] == inp["channels"]
    assert out["all_valid"] and all(a["char_count"] <= a["max_chars"] for a in out["adaptations"])
    by = {a["channel"]: a for a in out["adaptations"]}
    assert by["sms"]["sms_segments"] == 1 and "STOP" in by["sms"]["text"]
    assert not by["instagram_caption"]["links"] and 3 <= len(by["instagram_caption"]["hashtags"]) <= 8
    assert by["web_page_section"]["text"].startswith("## ")
    cal = (ctx.settings.runs_dir / out["calendar_artifact"]).read_text()
    rows = list(csv.DictReader(io.StringIO(cal)))
    assert len(rows) == 6 and rows[0]["date"] == "2026-10-20" and {r["status"] for r in rows} == {"draft"}
    assert {a.name for a in res.artifacts} >= {f"adaptation-{c}.txt" for c in inp["channels"] if c != "web_page_section"} | {
        "adaptation-web_page_section.md", "content_calendar.csv", "adaptations.json"}


def test_text_input_all_channels_and_validation_errors(ctx):
    agent = get_agent("content_adaptation")
    res = agent.run({"text": "Leak sensors cut water claims by 38%. Fit one under every sink.", "channels": ["all"]}, ctx)
    assert len(res.output["adaptations"]) == 10
    bad = agent.run({"text": "x", "channels": ["telegraph"]}, ctx)
    assert bad.status == "error" and "telegraph" in bad.error
    missing = agent.run({"source": "sample_data/docs/does-not-exist.md"}, ctx)
    assert missing.status == "degraded" and missing.output["adaptations"] == []


class ScriptedLLM(LLM):
    def __init__(self):
        super().__init__(get_settings().llm)
        self._filler, self._offline = self._offline, None
        self.calls = []

    def model_for(self, tier):
        return "scripted"

    def structured(self, prompt, schema, *, system=None, tier="balanced", images=None, retries=1, meter=None):
        self.calls.append(schema.__name__)
        if meter is not None:
            meter.add({})
        bad = ca.ChannelText(channel="x_post", text="Water is the everyday catastrophe. " * 12, hashtags=["A", "B", "C", "D"])
        if schema is ca.AdaptationDraft:
            return ca.AdaptationDraft(core_message="Water damage is the most common home claim.", key_points=["31% of claims"], adaptations=[bad])
        return ca.RepairBatch(adaptations=[bad])                                             # the repair is still too long


def test_repair_pass_then_deterministic_fix():
    llm = ScriptedLLM()
    res = get_agent("content_adaptation").run({"text": "Water is the everyday catastrophe: 31% of claims.", "channels": ["x_post"],
                                               "cta_url": URL}, RunContext.create(llm=llm))
    assert llm.calls == ["AdaptationDraft", "RepairBatch"]
    [x] = res.output["adaptations"]
    assert x["valid"] and x["char_count"] <= 280 and len(x["hashtags"]) <= 2 and x["links"] == [URL]
    assert "repaired by the model" in x["notes"] and any(n.startswith("deterministic fix: trimmed") for n in x["notes"])


def test_document_title_skips_wrapped_disclaimers():
    text = "[page 1]\nIllustrative sample content: fictional. Replace with the client's own\ncorpus.\nMeridian Mutual - Board Report, Q3 2026\n"
    assert ca.document_title("corpus.", text) == "Meridian Mutual - Board Report, Q3 2026"
    assert ca.document_title("The 2026 Home Risk Outlook", "anything") == "The 2026 Home Risk Outlook"


def test_pdf_and_docx_sources(ctx):
    agent = get_agent("content_adaptation")
    pdf = agent.run({"source": "sample_data/docs/board_report.pdf", "channels": ["linkedin_post"]}, ctx)
    docx = agent.run({"source": "sample_data/docs/case_study.docx", "channels": ["x_post"]}, ctx)
    assert pdf.output["source_title"] == "Meridian Mutual - Board Report, Q3 2026" and pdf.output["adaptations"]
    assert docx.output["source_title"].startswith("Harbour View Property Management") and docx.output["adaptations"]
