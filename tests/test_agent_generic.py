"""The agents are generic: any customer can call them, and none of them knows another customer.

The SDLC Factory's configuration was written inside one customer's delivery. What this layer's catalog carries has that
customer written out of it (caf.packs.generalise): no name, no site, no design system, no event in any instruction or sample,
and no assumed platform in a generic agent's instructions. The caller's own estate comes from their workspace
(caf.packs.workspace), and so do their own versions of this layer's skills: their brand voice, press boilerplate, glossary and
UTM convention. A customer's run that falls back to the bundled demo company's profile says so. No bundled sample is named
after a real company.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
PACKS = ROOT / "config" / "packs"
ACME = {"organisation": "Acme Air", "skills": {
    "brand_voice": {"name": "Acme Air", "voice": {"tone": "Brisk and friendly"}, "banned_phrases": ["world-class"]},
    "press_boilerplate": {"organisation": "Acme Air", "boilerplate": {"heading": "About Acme Air", "text": "Acme Air flies 40 routes from Denver."}},
    "utm_taxonomy": {"channels": {"email": {"source": "braze", "medium": "email"}}}}}
RELEASE = {"overview": "Acme Air opens a daily Denver to Lisbon route on 2 March 2027, its first to Portugal, with fares on sale today.",
           "target_audience": "Travel trade media", "date": "2026-10-20", "location": "Denver, Colorado", "spokesperson": "Rina Shah, Chief Commercial Officer"}
LINK = {"base_url": "https://www.acme-air.example/fares", "channels": ["email"],
        "campaign_parts": {"year": "2026", "quarter": "q4", "market": "us", "product": "fares", "objective": "acquisition"}}


def shipped() -> tuple[dict, dict]:
    return (yaml.safe_load((PACKS / "arm_sdlc.yaml").read_text(encoding="utf-8")),
            json.loads((PACKS / "arm_sdlc.contracts.json").read_text(encoding="utf-8")))


def run(agent_id: str, inputs: dict, *, customer: str | None = None):
    from caf.certify import Scripted
    from caf.core import RunContext, get_agent

    model = Scripted("good")
    result = get_agent(agent_id).run(inputs, RunContext.create(llm=model, options={"customer_id": customer} if customer else None))
    return result, model


def said(result) -> str:
    return json.dumps(result.output if isinstance(result.output, dict) else result.output.model_dump(mode="json"), default=str)


def test_the_shipped_pack_names_no_customer_and_assumes_no_platform(packs):
    from caf.core import RunContext, get_agent
    from caf.packs import build_prompt
    from caf.packs.generalise import FORBIDDEN, PLATFORM_NAMES, assumed_platforms, generalise, leaks
    from caf.packs.model import load_packs

    pack, contracts = shipped()
    assert leaks(pack, contracts) == []            # no customer's name, sites, design system, events or products, samples included
    assert assumed_platforms(pack) == []           # no generic agent's instructions name a CMS, a consent tool, an analytics suite ...
    assert generalise(pack, contracts) == (pack, contracts)   # the pass has nothing left to do: what is shipped is its fixed point
    # what a model is really shown, per agent and action: the whole prompt, built the way a run builds it
    loaded = load_packs()["arm_sdlc"]
    for agent in loaded.agents:
        for action in agent.actions:
            system, user = build_prompt(loaded, agent, action, {"request": "Plan the release"}, RunContext.create().for_agent(agent.id))
            found = FORBIDDEN.search(system + user)
            assert not found, f"{agent.id}.{action.id}: {found.group(0)!r}"
    # a generic agent speaks of roles; an agent that IS a platform's capability keeps that platform's name, and only that
    front = get_agent("arm_front_velocity").agent_def.skill
    assert "the customer's web estate" in front.system_prompt and "design-system component" in front.checklist[0]
    assert not PLATFORM_NAMES.search(front.system_prompt + " ".join(front.checklist + front.standards))
    sitecore = get_agent("arm_sitecore_marketer_mcp").agent_def.skill.system_prompt
    assert "SitecoreAI" in sitecore and "the customer's sites" in sitecore.lower() and "Marketo" not in sitecore
    # every agent's typical ask and every flow's sample request are the same neutral release
    assert {a["use_cases"][0] for a in pack["agents"]} == {pack["flows"][0]["inputs"][0]["example"]}
    assert all(f["name"].startswith("Web release") for f in pack["flows"])


def test_the_pass_rewrites_instructions_and_samples_and_keeps_bound_platforms():
    from caf.packs.generalise import SAMPLE_ORG, generalise, instruction, leaks, sample

    said = ("You are FrontVelocity, Coforge's front-end acceleration agent working inside the ARM SDLC Factory for\nArm's Sitecore "
            "(XM Cloud) + Next.js estate built on the ADS v5 design system. Map every UI element to an existing ADS v5 component; name "
            "the Sitecore rendering. Data-layer code pushes events to adobeDataLayer and checks OnetrustActiveGroups first.")
    out = instruction(said, "arm_front_velocity")
    assert out.startswith("You are FrontVelocity, Coforge's front-end acceleration agent working inside the SDLC Factory on the customer's web estate.")
    assert "existing design-system component; name the CMS rendering." in out and "pushes events to the data layer and checks the consent platform's active groups first" in out
    kept = instruction("Operate SitecoreAI (XM Cloud) capabilities for Arm's web estate; Marketo embeds carry the form id.", "arm_sitecore_forms")
    assert kept == "Operate SitecoreAI (XM Cloud) capabilities for the customer's web estate; marketing-automation embeds carry the form id."
    assert instruction("Arm tracking taxonomy (adobeDataLayer) and the OneTrust consent rule") == "The customer's tracking taxonomy (the data layer) and the consent rule"
    data = {"title": "Arm Tech Symposia 2027 Tokyo", "url": "https://www.arm.com/events/arm-tech-symposia-2027", "kit": "ADS v5",
            "build": "https://dev.azure.com/arm/ArmWeb/_build/results?buildId=7", "agent": "arm_site_auditor", "glossary": ["Arm", "Neoverse"]}
    assert sample(data) == {"title": f"{SAMPLE_ORG} Tech Summit 2027 Tokyo", "url": "https://www.solvane.example/events/solvane-tech-summit-2027", "kit": "SDS v3",
                            "build": "https://dev.azure.com/solvane/SolvaneWeb/_build/results?buildId=7", "agent": "arm_site_auditor",
                            "glossary": [SAMPLE_ORG, "Aurora"]}   # the agent's own id is not the customer's name
    pack = {"pack": {"id": "arm_sdlc", "name": "ARM SDLC Factory", "description": "runs Arm's AI to Z web release process", "context": "x"},
            "agents": [{"id": "arm_x", "summary": "Checks ADS v5 conformance", "description": "For Arm's web estate", "use_cases": ["Arm Tech Symposia 2027"],
                        "actions": [{"id": "do"}], "skill": {"system_prompt": said, "checklist": ["Use Marketo forms"], "standards": ["SonarQube gate 'Arm Web Way'"]}}],
            "flows": [{"id": "arm_stage_1", "name": "Arm release · 1. Design", "description": "Steps 1 to 5 of Arm's AI to Z release process",
                       "steps": [{"id": "s01", "agent": "arm_x", "purpose": "Step 1"}], "inputs": [{"name": "request", "example": "Arm Tech Symposia 2027"}]}]}
    once = generalise(pack, {"arm_x.do": {"example": {"result": data}}})
    assert leaks(*once) == [] and generalise(*once) == once and once[0]["pack"]["name"] == "ARM SDLC Factory" and once[0]["agents"][0]["id"] == "arm_x"
    assert once[0]["flows"][0]["name"] == "Web release · 1. Design" and once[0]["agents"][0]["skill"]["standards"] == ["The customer's code-quality gate"]


def test_a_customers_workspace_reaches_their_agents_and_nobody_elses(packs):
    from caf.packs import workspace

    saved = workspace.save("acme", "organisation: Acme Air\nplatforms:\n  cms: Adobe Experience Manager\n  consent: Cookiebot\nthresholds: {lcp_p75_ms: 2200}\n")
    assert sorted(saved) == ["organisation", "platforms", "thresholds"] and workspace.load("acme") == saved
    theirs, model = run("arm_front_velocity", {"request": "Stories for the fare page"}, customer="acme")
    prompt = model.calls[0]["prompt"]
    assert theirs.status == "ok" and "WORKSPACE (the caller's own estate" in prompt and '"cms": "Adobe Experience Manager"' in prompt
    assert theirs.provenance["workspace"] == ["organisation", "platforms", "thresholds"] and "the WORKSPACE" in model.calls[0]["system"]
    # their own versions of this layer's skills are read by the agents that use them; a pack agent is shown only how they sound
    workspace.save("acme", {**saved, "skills": ACME["skills"]})
    _, model = run("arm_front_velocity", {"request": "Stories for the fare page"}, customer="acme")
    assert '"tone": "Brisk and friendly"' in model.calls[0]["prompt"] and "utm_taxonomy" not in model.calls[0]["prompt"]
    workspace.save("acme", saved)
    for other in ("globex", None):   # another customer, and the operator: neither is shown Acme's estate
        r, m = run("arm_front_velocity", {"request": "Stories for the fare page"}, customer=other)
        assert "Acme" not in m.calls[0]["prompt"] and "WORKSPACE (" not in m.calls[0]["prompt"] and "workspace" not in r.provenance
    for bad in ("- just\n- a list\n", "a sentence", "x: [1, 2"):
        with pytest.raises(ValueError):
            workspace.parse(bad)
    with pytest.raises(ValueError):
        workspace.save("../outside", {"organisation": "x"})
    assert workspace.clear("acme") is True and workspace.load("acme") == {} and workspace.clear("acme") is False
    # the operator's own runs read the operator's workspace (`caf shop workspace` with no customer named)
    assert workspace.save(None, {"organisation": "Operator Co"}) == workspace.load(workspace.OPERATOR) == {"organisation": "Operator Co"}
    _, model = run("arm_front_velocity", {"request": "Stories for the fare page"})
    assert '"organisation": "Operator Co"' in model.calls[0]["prompt"] and workspace.clear(None) is True
    assert set(workspace.TEMPLATE) >= {"organisation", "sites", "platforms", "standards", "thresholds"}


def test_a_customers_content_is_written_from_their_own_company_profile_or_says_whose():
    from caf.core import RunContext
    from caf.core.context import COMPANY_SKILLS, DEMO_COMPANY
    from caf.packs import workspace

    # nothing saved: the work is done on the bundled demo company's profile, and the result says so. The operator's own run does not.
    theirs, model = run("press_release_creation", RELEASE, customer="globex")
    assert theirs.provenance["demo_profile"] == ["brand_voice", "press_boilerplate"] and DEMO_COMPANY in said(theirs)
    assert any("the demo company's profile" in w and "brand voice, press boilerplate" in w and "`skills` in your workspace" in w for w in theirs.warnings)
    ours, _ = run("press_release_creation", RELEASE)
    assert "demo_profile" not in ours.provenance and not any("demo company's profile" in w for w in ours.warnings)

    # their own profile: their name, voice and approved paragraph; nothing of the demo company's, in the prompt or in the release
    assert workspace.save("acme", ACME)["skills"].keys() == ACME["skills"].keys()
    try:
        theirs, model = run("press_release_creation", RELEASE, customer="acme")
        shown = " ".join(c["system"] + c["prompt"] for c in model.calls)
        assert theirs.status in ("ok", "needs_approval") and "demo_profile" not in theirs.provenance
        assert theirs.output["boilerplate"] == "Acme Air flies 40 routes from Denver." and "## About Acme Air" in theirs.output["body_markdown"]
        assert "Brisk and friendly" in shown and DEMO_COMPANY not in shown and "Meridian" not in said(theirs)
        mail, model = run("email_creation", {"overview": "Tell members about the Lisbon route", "call_to_action": "See fares", "cta_url": "https://www.acme-air.example/lisbon"}, customer="acme")
        assert "demo_profile" not in mail.provenance and DEMO_COMPANY not in " ".join(c["system"] + c["prompt"] for c in model.calls)
        # the rules of a company skill stay (style limits, AP abbreviations); only the company's facts are theirs, or empty
        record = RunContext.create(options={"customer_id": "acme"}).skill("press_boilerplate")
        assert record["style"]["release_label"] == "FOR IMMEDIATE RELEASE" and len(record["ap_states"]) == 50 and record["forward_looking_terms"]
        assert record["organisation"] == "Acme Air" and record["legal_name"] == "" and record["disclaimers"]["forward_looking"] == {"heading": "", "text": ""}
        # any skill can be theirs, stated as what differs: their UTM convention, not the server's
        assert run("utm_generation", LINK, customer="acme")[0].output["links"][0]["source"] == "braze"
        assert run("utm_generation", LINK, customer="globex")[0].output["links"][0]["source"] == "newsletter"
        # another customer, and the operator, see none of it
        other, model = run("email_creation", {"overview": "Tell members about the new route", "call_to_action": "See fares"}, customer="globex")
        assert other.provenance["demo_profile"] == ["brand_voice"] and "Acme" not in " ".join(c["system"] + c["prompt"] for c in model.calls)
    finally:
        workspace.clear("acme")
    # a skill that is not this layer's, or not its shape, is refused when it is saved, in plain words
    for bad, why in (({"skills": {"utm_taxonmy": {"case": "lower"}}}, "this layer has no skill of that name"),
                     ({"skills": {"utm_taxonomy": {"channels": "email"}}}, "`utm_taxonomy.channels` in your workspace's skills must be a set of named values"),
                     ({"skills": {"brand_voice": {"banned_phrases": "best-in-class"}}}, "`brand_voice.banned_phrases` in your workspace's skills must be a list"),
                     ({"skills": ["brand_voice"]}, "`skills` in a workspace is a set of skills by name"), ({"skills": {"brand_voice": {}}}, "is empty")):
        with pytest.raises(ValueError, match=why.replace(".", r"\.")):
            workspace.save("acme", bad)
    assert workspace.load("acme") == {} and workspace.save("acme", workspace.TEMPLATE) and workspace.clear("acme")   # the template itself is valid
    assert set(COMPANY_SKILLS) == {"brand_voice", "press_boilerplate", "translation_glossary", "semantic_layer"}


def test_no_bundled_sample_is_named_after_a_real_company(packs):
    import csv
    import re

    from caf.core import RunContext, get_agent

    # IntelliSight's sample was once named after a real airline, with its domains and campaign codes. It is a fictional one's now.
    real = re.compile(r"westjet|\bWJ-", re.I)
    sample = ROOT / "sample_data" / "intellisight" / "xperion_adobe_analytics_sample.csv"
    for path in (sample, sample.with_name("README.md"), PACKS / "intellisight.yaml", ROOT / "src" / "caf" / "accelerators" / "intellisight.py"):
        found = real.search(path.read_text(encoding="utf-8"))
        assert not found, f"{path.name}: {found.group(0)!r}"
    assert [p.name for p in sample.parent.glob("*.csv")] == [sample.name]
    with sample.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 1800 and {r["Report Suite"] for r in rows} == {"xperion-prod"}
    assert all(r["eVar3"].endswith(".xperion-airways.example") for r in rows)   # a reserved domain: nobody's real site
    opened = get_agent("is_data_interpreter").run({"dataset_name": "xperion"}, RunContext.create())
    assert opened.status == "ok" and "xperion sample" in said(opened)
    gone = get_agent("is_data_interpreter").run({"dataset_name": "westjet"}, RunContext.create())
    assert gone.status == "error" and "the samples are: xperion" in gone.error
