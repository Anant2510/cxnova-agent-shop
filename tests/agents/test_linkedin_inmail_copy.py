"""LinkedIn InMail Copy Creation: platform limits from the version-controlled skill, token-aware
budgeting, deterministic trim, merge-field previews from a CRM export, channel extensibility and
the model-repair -> deterministic-trim fallback."""

import pytest
import yaml

from caf.agents.content import linkedin_inmail_copy as li
from caf.config import get_settings
from caf.core import RunContext, get_agent
from caf.core.llm import LLM

PC = yaml.safe_load((get_settings().home / "config/skills/platform_constraints.yaml").read_text())
MF = PC["merge_fields"]
INMAIL = PC["channels"]["linkedin_inmail"]


class ScriptedLLM(LLM):
    """Online-looking model: every structured call returns `variant` (over-limit unless changed)."""

    def __init__(self, variant):
        super().__init__(get_settings().llm)
        self._filler, self._offline = self._offline, None
        self.variant, self.calls = variant, []

    def model_for(self, tier):
        return "scripted"

    def structured(self, prompt, schema, *, system=None, tier="balanced", images=None, retries=1, meter=None):
        self.calls.append(schema.__name__)
        if meter is not None:
            meter.add({})
        if schema is li.MessageDraft:
            return li.MessageDraft(variants=[li.MessageVariant(**self.variant)])
        return li.MessageVariant(**self.variant)


def test_skill_declares_channels_limits_and_tokens():
    assert PC["version"]
    assert {"linkedin_inmail", "x_post", "email", "whatsapp"} <= set(PC["channels"])
    assert (INMAIL["fields"]["subject"]["max_chars"], INMAIL["fields"]["body"]["max_chars"], INMAIL["fields"]["cta_text"]["max_chars"]) == (60, 1500, 20)
    assert {m["token"] for m in MF.values()} == {"{first_name}", "{company}", "{job_title}"}


def test_budgeted_length_reserves_token_width():
    assert li.measure("Hi {first_name}", MF) == 3 + MF["first_name"]["reserve_chars"]
    assert li.measure("Hi {unknown}", MF) == len("Hi {unknown}")
    assert li.measure("Read https://www.meridian-mutual.example/a/very/long/path", MF, url_chars=23) == 5 + 23


def test_validator_enforces_platform_rules():
    issues = li.validate_fields({"subject": "S" * 61, "body": "Hello {first_name}, {last_name} " + "x" * 600, "cta_text": "Join {company}"},
                                INMAIL, MF, banned=["world-class"], rules=PC["rules"])
    got = {(i["field"], i["rule"], i["status"]) for i in issues}
    assert ("subject", "max_chars", "fail") in got
    assert ("body", "recommended_chars", "warn") in got
    assert ("body", "unknown_token", "fail") in got
    assert ("cta_text", "token_not_allowed", "fail") in got
    missing = li.validate_fields({"subject": "Hi", "body": "Our WORLD-CLASS ROI PROGRAM", "cta_text": None}, INMAIL, MF,
                                 banned=["world-class"], rules=PC["rules"])
    got = {(i["field"], i["rule"]) for i in missing}
    assert {("cta_text", "required"), ("body", "banned_phrase"), ("body", "all_caps")} <= got   # ROI is allow-listed


def test_trim_keeps_tokens_and_sentences_whole():
    text = "Hi {first_name}, flood season is here. Teams at {company} are preparing now. Join our briefing to see how."
    out = li.trim_to_limit(text, 80, MF)
    assert li.measure(out, MF) <= 80 and out.endswith(".") and "{first_name}" in out
    word_cut = li.trim_to_limit("Register for our {company} flood readiness briefing", 30, MF)
    assert li.measure(word_cut, MF) <= 30 and "{compan" not in word_cut.replace("{company}", "")


def test_deterministic_fix_makes_variant_valid():
    fields = {"subject": "Hi {first_name}, an exclusive briefing on flood risk for {company} and your whole team", "body": "Hello {last_name}.",
              "cta_text": None, "preheader": "dropped on LinkedIn"}
    fixed, notes = li.deterministic_fix(fields, INMAIL, MF, default_cta="Learn more")
    assert not [i for i in li.validate_fields(fixed, INMAIL, MF, rules=PC["rules"]) if i["status"] == "fail"]
    assert fixed["cta_text"] == "Learn more" and fixed["preheader"] is None and "{last_name}" not in fixed["body"]
    assert any("trimmed" in n for n in notes)


def test_previews_render_fallbacks_and_platform_macros():
    fields = {"subject": "{first_name}, flood cover for {company}", "body": "As {job_title} you know renewals.", "cta_text": "Book a seat"}
    [full, partial] = li.previews_for(fields, [{"first_name": "Priya", "company": "Northwind", "job_title": "Risk Manager"},
                                               {"company": "Fabrikam Re"}], INMAIL, MF)
    assert full["subject"] == "Priya, flood cover for Northwind" and full["fallbacks_used"] == []
    assert partial["subject"] == "there, flood cover for Fabrikam Re" and partial["fallbacks_used"] == ["first_name", "job_title"]
    assert li.platform_copy(fields, INMAIL["platform_tokens"])["subject"] == "%FIRSTNAME%, flood cover for %COMPANYNAME%"


def test_crm_export_parsing_and_persona_matching():
    text = (get_settings().home / "sample_data/crm/contacts.csv").read_text()
    rows = li.parse_recipients_csv(text)
    assert len(rows) == 60 and set(rows[0]) == {"first_name", "company", "job_title"}
    brokers = li.match_recipients("Principal Broker", rows)
    assert brokers and all("Broker" in r["job_title"] for r in brokers)
    assert li.match_recipients("Astronaut", rows) == rows[:3]                           # no match -> first rows


def test_agent_selftest_variants_and_previews(ctx):
    agent = get_agent("linkedin_inmail_copy")
    res = agent.run(agent.selftest_input(), ctx)
    assert res.status != "error", res.error
    out = res.output
    assert [v["persona"] for v in out["variants"]] == ["Principal Broker", "Risk Manager"]
    assert out["constraints_version"] == PC["version"] and out["all_valid"]
    for v in out["variants"]:
        assert v["valid"] and all(v["char_counts"][k] <= v["limits"][k] for k in v["char_counts"])
        assert v["previews"] and all(p["within_limits"] for p in v["previews"])
    assert all("Broker" in p["recipient"] for p in out["variants"][0]["previews"])
    assert any(p["recipient"] == "Risk Manager - Fabrikam Re" for p in out["variants"][1]["previews"])
    assert any(c.rule == "input.recipients_csv" for c in res.checks)


def test_same_engine_emits_x_posts():
    agent = get_agent("linkedin_inmail_copy")
    res = agent.run({"overview": "Our 2027 flood cover expansion explained for brokers.", "channel": "x_post",
                     "cta_url": "https://www.meridian-mutual.example/products/home-insurance.html"}, RunContext.create())
    v = res.output["variants"][0]
    assert res.output["channel_label"] == "X (Twitter) post" and v["subject"] is None and v["cta_text"] is None
    assert v["limits"] == {"body": 280}


def test_unknown_channel_is_invalid_input(ctx):
    res = get_agent("linkedin_inmail_copy").run({"overview": "A webinar invite for brokers.", "channel": "carrier_pigeon"}, ctx)
    assert res.status == "error" and "carrier_pigeon" in res.error


def test_model_repair_then_deterministic_trim():
    long_variant = {"persona": "x", "subject": "Hi {first_name}, " + "a very long subject line about flood risk " * 3,
                    "body": "Short body for {company}.", "cta_text": "Register for the flood readiness briefing"}
    llm = ScriptedLLM(long_variant)
    res = get_agent("linkedin_inmail_copy").run({"overview": "Flood readiness briefing for brokers.", "personas": ["Principal Broker"],
                                                 "sender_name": "Jordan Blake"}, RunContext.create(llm=llm))
    assert llm.calls == ["MessageDraft", "MessageVariant"]                              # draft + exactly one repair pass
    v = res.output["variants"][0]
    assert v["repair"] == "model+trimmed" and v["valid"] and v["persona"] == "Principal Broker"
    assert v["char_counts"]["subject"] <= 60 and v["char_counts"]["cta_text"] <= 20
    assert any(c.rule == "copy.deterministic_trim" for c in res.checks)
