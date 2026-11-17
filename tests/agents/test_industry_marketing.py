"""Industry Marketing: versioned industry profiles, retrieval over the sample corpus with industry
metadata filters, numeric grounding of proof points and deterministic compliance."""

from pathlib import Path

import pytest
import yaml

from caf.agents.content import industry_marketing as im
from caf.config import get_settings
from caf.core import RunContext, get_agent
from caf.core.llm import LLM

HOME = get_settings().home
CORPUS = HOME / "sample_data/industry_corpus"
PROFILES = {p.stem: yaml.safe_load(p.read_text()) for p in (HOME / "config/skills/industries").glob("*.yaml")}


def test_profiles_are_complete_and_versioned():
    assert set(PROFILES) == {"bfs", "insurance", "travel", "healthcare", "retail"}
    for pid, p in PROFILES.items():
        assert p["version"] and p["id"] == pid and p["name"], pid
        assert len(p["terminology"]["preferred"]) >= 8 and len(p["personas"]) >= 3 and p["buyer_priorities"], pid
        comp = p["compliance"]
        assert comp["must"] and comp["never"] and comp["required_disclaimers"] and comp["regulators"], pid
        assert p["proof_points"]["guidance"], pid


@pytest.mark.parametrize("value,expected", [("insurance", "insurance"), ("Banking", "bfs"), ("BFSI", "bfs"), ("hospitality", "travel"),
                                            ("e-commerce", "retail"), ("Health care", "healthcare"), ("regional banks", "bfs"),
                                            ("aerospace", None)])
def test_industry_aliases(value, expected):
    assert im.resolve_industry(value, PROFILES) == expected


def test_corpus_front_matter_and_chunks():
    docs = {d["doc_id"]: d for d in im.load_corpus(CORPUS)}
    case = docs["insurance_claims_automation_case"]
    assert (case["industry"], case["doc_type"], case["title"]) == ("insurance", "case_study", "Claims automation at a regional property and casualty insurer")
    assert "fnol" in case["tags"] and not case["text"].startswith("Illustrative sample content")
    assert {d["industry"] for d in docs.values()} == set(PROFILES)
    chunks = im.corpus_chunks([case])
    assert all(c["id"].startswith("insurance_claims_automation_case#") and c["metadata"]["industry"] == "insurance" for c in chunks)
    results = next(c for c in chunks if c["metadata"]["section"] == "Results after twelve months")
    assert "| FNOL to first contact (average) | 31 hours | 4 hours |" in results["text"]     # tables kept whole


def test_table_rows_become_verbatim_proof_points():
    sents = im.passage_sentences("| Measure | Before | After |\n|---|---|---|\n| Claims reported digitally | 12% | 58% |\n- Cut costs by 9%.")
    assert sents == ["Claims reported digitally: 12% -> 58%", "Cut costs by 9%."]
    doc = (CORPUS / "insurance_claims_automation_case.md").read_text()
    assert im.verify_proof_point("FNOL to first contact (average): 31 hours -> 4 hours", doc)
    assert not im.verify_proof_point("FNOL to first contact fell by 90%", doc)
    assert not im.verify_proof_point("Faster claims for everyone", doc)                       # no figure -> not a proof point


def test_compliance_helpers():
    never = PROFILES["insurance"]["compliance"]["never"]
    assert im.find_phrases("We offer a Guaranteed Payout and instant approval.", never) == ["guaranteed payout", "instant approval"]
    assert im.find_phrases("Cheapestness is not a word", ["cheapest"]) == []


def test_agent_grounds_copy_in_insurance_corpus(ctx):
    agent = get_agent("industry_marketing")
    res = agent.run(agent.selftest_input(), ctx)
    assert res.status != "error", res.error
    out = res.output
    assert out["industry"] == "insurance" and out["profile_version"] == PROFILES["insurance"]["version"] and out["grounded"]
    assert any(c.rule == "retrieval.filter" and c.status == "pass" for c in res.checks)
    cited = {c["doc_id"] for c in out["citations"]}
    assert cited and all(c["industry"] == "insurance" for c in out["citations"])
    assert out["proof_points"] and all(p["verified"] and p["source_doc"] in cited for p in out["proof_points"])
    docs = {d["doc_id"]: d["text"] for d in im.load_corpus(CORPUS)}
    assert all(im.verify_proof_point(p["text"], docs[p["source_doc"]]) for p in out["proof_points"])
    assert "FNOL to first contact (average): 31 hours -> 4 hours" in [p["text"] for p in out["proof_points"]]
    assert out["compliance"]["passed"]
    assert "Cover is subject to terms, conditions and underwriting criteria." in out["tailored_copy"]
    assert out["personas"][0]["name"] == "Head of Claims"


@pytest.mark.parametrize("industry,doc_prefix", [("BFSI", "bfs"), ("travel", "travel"), ("healthcare", "healthcare"), ("retail", "retail")])
def test_retrieval_is_filtered_by_industry(ctx, industry, doc_prefix):
    agent = get_agent("industry_marketing")
    res = agent.run({**agent.selftest_input(), "industry": industry, "persona": None}, ctx)
    out = res.output
    assert out["grounded"] and out["proof_points"]
    assert all(c["doc_id"].startswith(doc_prefix) for c in out["citations"])
    assert all(p["source_doc"].startswith(doc_prefix) for p in out["proof_points"])


def test_missing_corpus_degrades(ctx, tmp_path):
    agent = get_agent("industry_marketing")
    res = agent.run({**agent.selftest_input(), "corpus_path": str(tmp_path / "nothing-here")}, RunContext.create())
    assert res.status == "degraded"          # vector store may still hold the corpus from earlier runs in this process
    assert res.output["compliance"]["passed"]


def test_unknown_industry_is_invalid_input(ctx):
    res = get_agent("industry_marketing").run({"industry": "aerospace", "source_text": "Anything at all."}, ctx)
    assert res.status == "error" and "aerospace" in res.error


class ScriptedLLM(LLM):
    """Online-looking model that writes non-compliant, badly-grounded copy (and does not fix it)."""

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
        return im.TailoredDraft(
            tailored_headline="Faster claims, guaranteed payout",
            tailored_copy="Cut FNOL to first contact from 31 hours to 4 [insurance_claims_automation_case]. "
                          "Our users love it [bfs_digital_onboarding_case]. Underwriting and loss ratio gains follow.",
            key_terms_used=["synergy"], personas=["head of claims", "Chief Astronaut"],
            proof_points=[im.DraftProofPoint(text="FNOL to first contact fell from 31 hours to 4", source_doc="[insurance_claims_automation_case]"),
                          im.DraftProofPoint(text="Leakage fell by 75%", source_doc="insurance_claims_automation_case"),
                          im.DraftProofPoint(text="Onboarding up 22%", source_doc="made_up_doc")],
            citations=["insurance_claims_automation_case", "made_up_doc"])


def test_compliance_and_grounding_are_enforced_on_model_output():
    llm = ScriptedLLM()
    agent = get_agent("industry_marketing")
    res = agent.run(agent.selftest_input(), RunContext.create(llm=llm))
    out = res.output
    assert llm.calls == ["TailoredDraft", "TailoredDraft"]                                   # draft + one repair pass
    assert not out["compliance"]["passed"] and out["compliance"]["never_phrases_found"] == ["guaranteed payout"]
    assert out["compliance"]["repaired"] and any(c.rule == "compliance.never_phrases" and c.status == "fail" for c in res.checks)
    assert [p["text"] for p in out["proof_points"]] == ["FNOL to first contact fell from 31 hours to 4"]   # 75% and made-up doc dropped
    assert "[bfs_digital_onboarding_case]" not in out["tailored_copy"]                       # citation to a non-retrieved doc removed
    assert {c["doc_id"] for c in out["citations"]} == {"insurance_claims_automation_case"}
    assert out["key_terms_used"] == ["FNOL", "underwriting", "loss ratio"]                    # recomputed, not the model's list
    assert [p["name"] for p in out["personas"]] == ["Head of Claims"]
    assert out["compliance"]["avoid_terms_found"] == ["users -> policyholders"]
