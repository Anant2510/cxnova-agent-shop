"""The workshop: a goal is assessed before it runs, and the agents it lacks are made, registered and kept."""
from __future__ import annotations

import pytest

from caf import workshop
from caf.workshop import AgentDraft, DraftInput, Feasibility, Gap, Requirement, finish_draft, snake, to_pack_agent

from test_runs import ADMIN, shop_client  # noqa: E402,F401  (the API fixture)

GOAL = "Create a portal which takes a raw data file out of Adobe Analytics as input and gives recommendations and insights"


def test_offline_assessment_is_honest_about_not_assessing(ctx):
    steps = [{"id": "traffic", "agent": "ga4_traffic_report", "purpose": "the KPIs"}, {"id": "ideas", "agent": "experiment_ideation"}]
    f = workshop.assess(GOAL, steps, ctx)
    assert f.verdict == "unassessed" and f.method == "heuristic" and "connect a model" in f.cannot_do.lower()
    assert [r.steps for r in f.requirements] == [["traffic"], ["ideas"]] and f.gaps == []


def test_assessment_cleaning_keeps_only_real_steps_and_free_ids():
    rows = [{"id": "traffic", "agent": "ga4_traffic_report", "name": "GA4", "summary": "", "purpose": ""}]
    raw = Feasibility(verdict="feasible", requirements=[Requirement(id="", text="kpis", status="covered", steps=["traffic", "ghost"]),
                                                         Requirement(id="r2", text="a portal", status="covered", steps=[])],
                      gaps=[Gap(requirement="r2", agent_id="GA4 Traffic Report", agent_name="Portal Builder", purpose="builds it", category="web stuff")])
    f = workshop._clean(raw, rows)
    assert f.requirements[0].id == "r1" and f.requirements[0].steps == ["traffic"]
    assert f.requirements[1].status == "partial"            # "covered" by no step is not covered
    assert f.gaps[0].agent_id != "ga4_traffic_report" and f.gaps[0].agent_id.startswith("ga4_traffic_report")   # a taken id gets a suffix
    assert f.gaps[0].category == "web" and f.verdict == "partial"   # a verdict that contradicts the requirements is corrected


def test_offline_draft_is_complete_and_valid_as_a_pack_agent(ctx):
    d = workshop.draft_agent("Read a raw Adobe Analytics export and compute KPIs by channel", ctx, name="Adobe Export Reader")
    assert d.id == "adobe_export_reader" and d.category == "analytics" and d.system_prompt and d.output_keys and d.inputs
    from caf.packs.model import PackAgent

    entry = to_pack_agent(d)
    PackAgent(**entry)
    assert entry["actions"][0]["output_keys"] == d.output_keys and entry["skill"]["system_prompt"] == d.system_prompt
    with pytest.raises(ValueError):
        workshop.draft_agent("   ", ctx)
    assert snake("Adobe-Analytics Reader!") == "adobe_analytics_reader"
    draft = finish_draft(AgentDraft(id="", name="Made Thing", summary="s", system_prompt="p", output_keys=[], inputs=[DraftInput(name="Source File")]))
    assert draft.id == "made_thing" and draft.output_keys == ["findings", "recommendations"] and draft.inputs[0].name == "source_file"


def test_made_agent_joins_the_catalog_the_planner_and_mcp_and_outlives_a_reload(shop_client, tmp_path):
    draft = shop_client.post("/agents/draft", json={"purpose": "Read a raw Adobe Analytics data-feed export and compute KPIs", "goal": GOAL}, headers=ADMIN).json()["draft"]
    assert shop_client.post("/agents/draft", json={"purpose": "x"}).status_code == 401                     # the operator makes agents
    before = len(shop_client.get("/agents", headers=ADMIN).json())
    made = shop_client.post("/agents/made", json=draft, headers=ADMIN)
    assert made.status_code == 201 and made.json()["pack"] == "made" and made.json()["id"] == draft["id"]
    try:
        assert len(shop_client.get("/agents", headers=ADMIN).json()) == before + 1
        assert [a["id"] for a in shop_client.get("/agents/made", headers=ADMIN).json()] == [draft["id"]]
        assert shop_client.post("/agents/made", json=draft, headers=ADMIN).status_code == 400            # the id is taken now
        # the planner may pick it, unlike a product pack's agents
        from caf.core.registry import blueprint_agents

        assert draft["id"] in {a.spec.id for a in blueprint_agents()}
        plan = shop_client.post("/plan", json={"goal": "", "agent": draft["id"]}, headers=ADMIN).json()
        assert [s["agent"] for s in plan["plan"]["steps"]] == [draft["id"]] and plan["feasibility"]["verdict"] == "unassessed"
        # it runs (offline: the contract's sample), is metered and priced like any agent
        run = shop_client.post(f"/agents/{draft['id']}/run", json={"inputs": {"request": "last month's export"}}, headers=ADMIN).json()
        assert run["output"]["runner"] == "twin" and set(run["output"]["result"]) == set(draft["output_keys"]) and "cost_usd" in run["metrics"]
        # the MCP tool list follows the catalog without a restart
        from test_api import MCP_HEADERS

        tools = {t["name"] for t in shop_client.post("/mcp/", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, headers={**MCP_HEADERS, **ADMIN}).json()["result"]["tools"]}
        assert f"run_{draft['id']}" in tools
        # a run started from a preview keeps the assessment it was shown
        feas = {"verdict": "partial", "can_do": "kpis", "cannot_do": "no portal", "requirements": [], "gaps": [], "method": "model"}
        view = shop_client.post("/orchestrate", json={"goal": "x", "agent": draft["id"], "feasibility": feas, "wait_s": 30}, headers=ADMIN).json()
        assert view["status"] == "completed" and view["route"]["feasibility"]["verdict"] == "partial"
        # it is on disk, and comes back after the packs are reloaded (a restart)
        from caf.core.registry import get_agent, reload_agents
        from caf.packs import reset_packs

        assert workshop.made_path().exists() and draft["id"] in workshop.made_path().read_text()
        reset_packs()
        reload_agents()
        assert get_agent(draft["id"]).spec.pack == "made"
    finally:
        assert shop_client.delete(f"/agents/made/{draft['id']}", headers=ADMIN).json() == {"removed": draft["id"]}
    assert shop_client.get(f"/agents/{draft['id']}", headers=ADMIN).status_code == 404
    assert shop_client.delete(f"/agents/made/{draft['id']}", headers=ADMIN).status_code == 404
    assert len(shop_client.get("/agents", headers=ADMIN).json()) == before
