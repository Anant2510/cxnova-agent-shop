"""The battery (`caf agents certify`, src/caf/certify.py): every agent through what customers and their tools really do.

Wrong types, nulls, nothing at all, a very large input, and a model that answers in prose, with half a reply, in the wrong
shape or not at all. No live model is needed: a scripted one misbehaves on purpose. The whole web stack stays out of it.
"""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, Field

PACKS = ("voyagerai", "intellisight", "campaign_studio", "arm_sdlc")
CRAWL = "url,status,load_ms,broken_links\nhttps://www.example.com/a,200,1840,0\nhttps://www.example.com/b,404,300,0\nhttps://www.example.com/c,200,5200,4\n"


def run(agent_id: str, inputs, *, mode: str | None = None, customer: str | None = None, flow_steps: int = 1):
    from caf.certify import Scripted
    from caf.core import RunContext, get_agent

    model = Scripted(mode) if mode else None
    options = {"customer_id": customer} if customer else None
    ctx = RunContext.create(llm=model, options=options) if model else RunContext.create(options=options)
    return get_agent(agent_id).run(inputs, ctx, options={"flow_steps": flow_steps}), model


@pytest.mark.parametrize("pack", PACKS)
def test_every_pack_agent_passes_the_battery(packs, pack):
    from caf.certify import certify, render

    report = certify(pack=pack, fast=True)
    failed = [(r["id"], x["trial"], x["detail"]) for r in report["agents"] for x in r["trials"] if not x["pass"]]
    assert report["ok"] and not failed, failed
    assert report["totals"]["agents"] == report["packs"][pack]["agents"] > 0
    # the same result as a page for people: a table per pack, a line per agent
    page = render(report, markdown=True)
    assert f"| {pack} |" in page and all(f"`{r['id']}`" in page for r in report["agents"])


def test_every_agent_in_the_catalog_can_be_found_understood_and_called(packs):
    """The contract trial on all of them, the marketing blueprint included. It runs nothing, so it needs no browser and no
    statistics library: the three agents whose undescribed inputs went unseen where they could not run are seen here."""
    from caf.certify import contract_problems
    from caf.core import agents_in_registration_order

    agents = agents_in_registration_order()
    wrong = {a.spec.id: contract_problems(a) for a in agents if contract_problems(a)}
    assert len(agents) >= 110 and not wrong, wrong
    schema = {a.spec.id: a.Input.model_json_schema()["properties"] for a in agents}
    assert "WCAG 2.1 level AA" in schema["web_accessibility_evaluation"]["standard"]["description"]
    assert "sequential" in schema["experiment_planning"]["design"]["description"] and schema["heatmap_analysis"]["device"]["description"]


def test_blueprint_agents_pass_the_battery(ctx):
    from caf.certify import certify

    report = certify(agents=["utm_generation", "qr_code_generation", "email_creation", "campaign_brief_generation"], fast=True)
    failed = [(r["id"], x["trial"], x["detail"]) for r in report["agents"] for x in r["trials"] if not x["pass"]]
    assert report["totals"]["agents"] == 4 and not failed, failed


def test_the_battery_judges_the_work_not_where_it_is_kept_and_not_an_empty_success():
    from caf.certify import hollow, same_result
    from caf.core import RunContext, get_agent

    # each run keeps its files in its own folder, so a result that names them differs by the run's id and by nothing else
    one = {"run_id": "run-20261125-152410-144836", "output": {"codes": [{"png_artifact": "run-20261125-152410-144836/qr/qr-01.png", "version": 3}]}}
    two = {"run_id": "run-20261125-152410-177d05", "output": {"codes": [{"png_artifact": "run-20261125-152410-177d05/qr/qr-01.png", "version": 3}]}}
    assert same_result(one, two) and same_result(one, one)
    assert not same_result(one, {**two, "output": {"codes": [{"png_artifact": "run-20261125-152410-177d05/qr/qr-01.png", "version": 4}]}})
    assert not same_result({"output": {"n": 1}}, {"output": {"n": 2}})   # no run named: compared as they are
    # ending `ok` with every list empty is not a pass; saying why (degraded, or an error) is
    assert hollow({"status": "ok", "output": {"codes": [], "taxonomy_version": "2026.1", "manifest_artifact": None}})
    assert not hollow({"status": "ok", "output": {"codes": [{"version": 3}], "notes": []}})
    assert not hollow({"status": "degraded", "output": {"pages": []}}) and not hollow({"status": "ok", "output": {"text": "done"}})
    # the agent that showed both: with nothing it can encode, it fails and says why
    nothing = get_agent("qr_code_generation").run({"urls": ["not a url", "also not one"]}, RunContext.create())
    assert nothing.status == "error" and "No QR code could be generated: 'not a url' is not an absolute http(s) URL" in nothing.error


def test_the_battery_calls_no_live_model_unless_asked(monkeypatch):
    from caf import certify as battery
    from caf.config import reset_settings_cache
    from caf.core import get_agent
    from caf.core.llm import LLM

    built = []
    real = LLM.__init__

    def spy(self, settings, tracer=None, provider=None):
        real(self, settings, tracer, provider)
        built.append(self.provider)

    monkeypatch.setattr(LLM, "__init__", spy)
    monkeypatch.setenv("CAF_LLM_PROVIDER", "anthropic")   # a server set up with a live model, as a real one is
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-key")
    reset_settings_cache()
    try:
        agent = get_agent("email_creation")
        ran = battery.attempt(agent, agent.selftest_input())   # a trial that is not about the model
        assert ran["status"] in battery.LIVE and built and set(built) == {"offline"}   # it ran, and the live provider was never built
        assert battery.model_for_trial(None).offline and isinstance(battery.model_for_trial("fails"), battery.Scripted)
        assert battery.model_for_trial(None, live=True) is None   # only when asked: the server's own model
        assert battery.model_used(False).startswith("No live model was called") and "anthropic" in battery.model_used(True)
    finally:
        monkeypatch.undo()
        reset_settings_cache()
    page = battery.render({"totals": {"agents": 0, "passed": 0, "trials": 0, "failed_trials": 0}, "packs": {}, "agents": [], "ok": True,
                           "model": battery.model_used(False)}, markdown=True)
    assert "No live model was called" in page


def test_invalid_input_is_explained_in_plain_words(packs):
    wrong, _ = run("cs_xtender_variants", {"request": 12345, "personas": "not a list", "action": "delete_everything"})
    assert wrong.status == "error" and wrong.error.startswith("Invalid input for cs_xtender_variants: ")
    assert "`request` should be text, got a number" in wrong.error and "`personas` should be a list, got text" in wrong.error
    assert "`action` must be one of 'write_variants'" in wrong.error
    assert "pydantic" not in wrong.error and "validation error" not in wrong.error   # no library wording, no link to its docs
    # a field sent as null is a field not sent: what a form or a tool-calling client sends for "left empty"
    nulls, _ = run("cs_xtender_variants", {"request": None, "brief": None, "action": None, "personas": None})
    assert nulls.status == "error" and "was given nothing to work on" in nulls.error
    filled, model = run("cs_xtender_variants", {"request": "Write the variants", "brief": None, "campaign_id": None}, mode="good")
    assert filled.status == "ok" and model.calls
    listed, _ = run("email_creation", [1, 2])
    assert listed.status == "error" and "must be an object of named fields, got a list" in listed.error


def test_a_customers_failed_model_call_is_a_failure_never_a_sample(packs):
    for mode, why in (("fails", "the model's rate limit was reached"), ("slow", "the model took too long to answer"),
                      ("prose", "its reply was not in the agreed form"), ("nulls", "its reply was not in the agreed form")):
        theirs, _ = run("cs_xtender_variants", {"request": "Write the variants"}, mode=mode, customer="acme")
        assert theirs.status == "error" and why in theirs.error and "no sample was put in its place" in theirs.error, mode
        assert theirs.error.startswith("Xtender variant agent could not do this")   # a sentence, not an exception's name
        ours, _ = run("cs_xtender_variants", {"request": "Write the variants"}, mode=mode)   # the operator's own run: the sample, said
        assert ours.status == "degraded" and ours.output["runner"] == "twin" and any("model call failed" in r for r in ours.provenance["degraded_reasons"])
    # a reply that can be read is read, whatever it is wrapped in or however its envelope is shaped
    for mode in ("fenced", "wrong_shape"):
        theirs, _ = run("cs_xtender_variants", {"request": "Write the variants"}, mode=mode, customer="acme")
        assert theirs.status == "ok" and theirs.output["runner"] == "model", mode
    from caf.core import get_agent

    shaped, _ = run("vai_personalization", get_agent("vai_personalization").selftest_input(), mode="wrong_shape")   # its own check still runs
    assert shaped.status == "ok" and shaped.output["result"]["text"] == "just text" and shaped.output["human_followups"] == ["call me"]


def test_a_measuring_agent_reports_what_was_observed_or_refuses(packs):
    alone, _ = run("arm_site_auditor", {"request": "Audit the events estate"})
    assert alone.status == "error" and "cannot do Estate audit on a request alone" in alone.error and "`evidence`" in alone.error
    fed, model = run("arm_site_auditor", {"request": "Audit the events estate", "evidence": CRAWL}, mode="good")
    assert fed.status == "ok" and fed.provenance["basis"] == "measured" and fed.provenance["evidence"] == ["evidence"]
    assert fed.provenance["inputs"]["records"] == {"evidence": 3}   # the CSV was parsed into rows, and the model was shown them
    assert "https://www.example.com/c" in model.calls[0]["prompt"] and "YOU CANNOT MEASURE OR OBSERVE ANYTHING" in model.calls[0]["system"]
    in_flow, _ = run("arm_site_auditor", {"request": "Audit the events estate"}, mode="good", flow_steps=5)   # an earlier step may carry it
    assert in_flow.status == "degraded" and any(r.startswith("not measured:") for r in in_flow.provenance["degraded_reasons"])
    # every ARM agent can be handed material to work on; before, it had a request and nothing else
    written, model = run("arm_front_velocity", {"request": "Turn these requirements into stories", "material": "REQ-1 The hub lists events by region.\nREQ-2 A visitor can register in two steps."}, mode="good")
    assert written.status == "ok" and "REQ-2 A visitor can register in two steps." in model.calls[0]["prompt"]


def test_the_laziest_valid_reply_is_built_from_any_schema():
    from caf.certify import sparse

    class Line(BaseModel):
        text: str
        weight: float = Field(ge=1)

    class Reply(BaseModel):
        title: str = Field(min_length=3)
        lines: list[Line] = Field(min_length=2)
        note: str | None
        tags: list[str]
        optional: str = "kept out"

    lazy = sparse(Reply)
    assert json.loads(lazy.model_dump_json()) == {"title": "xxx", "lines": [{"text": "", "weight": 1.0}] * 2, "note": None, "tags": [], "optional": "kept out"}
