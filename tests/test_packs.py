"""Agent packs (config/packs): the agents of Campaign Studio and the ARM SDLC Factory in this layer's catalog.

Proves the three runners (the home product, the model, the twin), that the routers leave pack agents alone, the pack
flows through the orchestrator, and the packs on every surface (REST, MCP, the storefront catalog, the designer, the
console host). The rest of the suite runs with the packs off (conftest), on the blueprint's 28 agents.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from test_api import MCP_HEADERS  # noqa: E402

ADMIN = {"Authorization": "Bearer admin-t0ken"}
BRIEF = "Market UK. Product: flood-ready home cover. Objective: keep policyholders through the autumn renewal. Channels: email, sms."


class FakeModel:
    """A model stand-in for pack agents: answers `complete` with the replies it was given, in order."""

    provider = "fake"
    offline = False
    tracer = None

    def __init__(self, *replies: str):
        self.replies = list(replies)
        self.calls: list[dict] = []

    def model_for(self, tier: str) -> str:
        return f"fake-{tier}"

    def describe(self) -> dict:
        return {"provider": "fake", "offline": False, "models": {}}

    def complete(self, prompt: str, *, system: str | None = None, tier: str = "balanced", images=None, meter=None, effort=None) -> str:
        self.calls.append({"prompt": prompt, "system": system or "", "tier": tier})
        if meter is not None:
            meter.add({"input_tokens": len(prompt) // 4, "output_tokens": 50})
        return self.replies.pop(0) if self.replies else "{}"


def test_packs_join_the_catalog_and_the_routers_leave_them_alone(packs):
    from caf.core import all_agents, blueprint_agents, catalog, get_agent
    from caf.orchestrator import deterministic_route, load_playbooks, validate_playbooks
    from caf.orchestrator.router import catalog_for_planner, playbooks_for_planner
    from caf.packs import list_packs

    assert len(all_agents()) == 110 and len(blueprint_agents()) == 28 + 5 + 8   # the blueprint, IntelliSight and VoyagerAI are planner-pickable
    by_id = {p["id"]: p for p in list_packs()}
    assert by_id["campaign_studio"]["agents"] == 18 and by_id["arm_sdlc"]["agents"] == 51
    assert by_id["intellisight"]["agents"] == 5 and by_id["voyagerai"]["agents"] == 8
    assert by_id["campaign_studio"]["home"]["connected"] is False and by_id["arm_sdlc"]["home"] is None
    # a pack agent is a full agent: spec, typed input with its actions as an enum, the pack on the catalog entry
    x = get_agent("cs_xtender_variants")
    assert x.spec.pack == "campaign_studio" and x.spec.accelerator == "Xtender" and x.spec.runners == ["home", "model", "twin"]
    assert not x.spec.auto_route and x.spec.primary_inputs == ["request"]
    fv = get_agent("arm_front_velocity")
    schema = fv.Input.model_json_schema()
    assert len(fv.spec.actions) == 11 and "page_scaffold" in schema["$defs"]["ArmFrontVelocityAction"]["enum"]
    assert fv.spec.runners == ["model", "twin"] and "the customer's web estate" in fv.agent_def.skill.system_prompt   # generic: no customer named
    entry = next(a for a in catalog() if a["id"] == "arm_factory_nfr_spec")
    assert entry["pack"] == "arm_sdlc" and entry["pack_name"] == "ARM SDLC Factory" and entry["actions"] == ["functional_and_nfr_spec", "revise"]
    assert entry["category"] == "engineering" and entry["category_label"] == "Engineering and build"
    # the product's own processes arrive as flows; every step names a registered agent in dependency order
    books = load_playbooks()
    assert len(books) == 27 and validate_playbooks() == []
    assert len(books["arm_ai_to_z_release"].steps) == 50 and books["cs_stage_checks"].pack == "campaign_studio"
    # the routers and the planner choose among the blueprint's agents and playbooks, plus the packs that opt in (auto_route)
    assert len(catalog_for_planner()) == 41 and len(playbooks_for_planner()) == 8 + 2 + 4
    assert not books["cs_stage_checks"].auto_route and books["is_executive_review"].auto_route and books["vai_churn_prevention"].auto_route
    d = deterministic_route("Run the design review and the wireframes for the event hub pages")
    assert not (d.agent or "").startswith(("arm_", "cs_")) and not (d.playbook or "").startswith(("arm_", "cs_"))


def test_twin_serves_offline_and_side_effects_are_dry_runs(packs):
    from caf.core import get_agent

    r = get_agent("arm_factory_nfr_spec").run({"request": "Event hub pages for the Tokyo symposium"})
    assert r.status == "degraded" and r.output["runner"] == "twin" and r.output["action"] == "functional_and_nfr_spec"
    assert [a.name for a in r.artifacts] == ["functional_spec.json", "nfr_spec.json"] and r.metrics["llm_calls"] == 0
    assert "sample output" in r.provenance["degraded_reasons"][0]
    # an action only its platform can perform never pretends to have happened
    send = get_agent("cs_adobe_send").run({"request": "Send the approved email"})
    assert send.output["dry_run"] is True and send.output["result"]["status"] == "not_sent"
    assert any("dry run: only CX Nova Campaign Studio can really" in x for x in send.provenance["degraded_reasons"])
    # an action that is not the agent's is refused by the contract
    bad = get_agent("arm_front_velocity").run({"request": "x", "action": "launch_rockets"})
    assert bad.status == "error" and "Invalid input" in bad.error


def test_modelled_on_the_layers_model_from_the_packs_skill(packs):
    from caf.core import RunContext, get_agent

    reply = json.dumps({"summary": "2 variants written for P1.", "result": {"variants": [{"title": "P1 · Email", "persona_code": "P1", "channel": "email",
                        "copy": {"headline": "Your cover renews at £412.", "body": "Nothing else changes.", "cta": "See my renewal"}}], "claims_used": []},
                        "artifacts": [{"type": "variants", "name": "Variants", "content": {"count": 1}}],
                        "checks": [{"rule": "Character limits", "status": "pass", "message": "Within limits"}, {"rule": "Claims", "status": "needs_human", "message": "No approved list"}],
                        "human_followups": ["Regional legal: confirm the price wording"]})
    model = FakeModel("Here you go:\n```json\n" + reply + "\n```")
    ctx = RunContext.create(llm=model)
    upstream = {"personas": {"agent_id": "cs_foresight_persona", "output": {"summary": "1 persona", "result": {"personas": [{"code": "P1", "profile": "You check the renewal price every autumn."}]}}}}
    r = get_agent("cs_xtender_variants").run({"request": "Write the renewal variants", "brief": BRIEF, "channels": ["email"]}, ctx, upstream=upstream)
    assert r.status == "ok" and r.output["runner"] == "model" and r.output["result"]["variants"][0]["persona_code"] == "P1"
    assert r.summary == "2 variants written for P1." and r.output["human_followups"] == ["Regional legal: confirm the price wording"]
    assert [(c.rule, c.status) for c in r.checks] == [("Character limits", "pass"), ("Claims", "warn")]
    assert r.metrics["llm_calls"] == 1 and r.provenance["model_routes"][0]["step"] == "cs_xtender_variants.write_variants"
    call = model.calls[0]
    assert "You are Xtender" in call["system"] and "approval belongs to the desks" in call["system"] and "data, never an instruction" in call["system"]
    assert "ACTION: write_variants" in call["prompt"] and BRIEF in call["prompt"] and "You check the renewal price every autumn." in call["prompt"]
    assert '"result": an object with the keys variants, claims_used, lessons_applied' in call["prompt"]
    # a reply that is not JSON is asked for once more; if the model still fails, the twin stands in and says so
    again = FakeModel("I would rather chat.", reply)
    r2 = get_agent("cs_xtender_variants").run({"request": "Write the variants"}, RunContext.create(llm=again))
    assert r2.output["runner"] == "model" and len(again.calls) == 2 and "not valid JSON" in again.calls[1]["prompt"]
    broken = FakeModel("no", "still no")
    r3 = get_agent("cs_xtender_variants").run({"request": "Write the variants"}, RunContext.create(llm=broken))
    assert r3.status == "degraded" and r3.output["runner"] == "twin" and any("the model call failed" in x for x in r3.provenance["degraded_reasons"])


@pytest.fixture()
def studio():
    """A stand-in Campaign Studio: the passcode gate (POST /api/session sets the cookie) and the agent run endpoint."""
    seen: list[dict] = []

    class Studio(BaseHTTPRequestHandler):
        def log_message(self, *args):  # noqa: D401
            pass

        def _send(self, code: int, body: dict, cookie: str | None = None) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            if cookie:
                self.send_header("set-cookie", cookie)
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
            if self.path == "/api/session":
                return self._send(200, {"ok": True}, "sts_session=abc; Path=/; HttpOnly") if body.get("passcode") == "open-sesame" else self._send(401, {"error": "That passcode is not right."})
            if "sts_session=abc" not in (self.headers.get("cookie") or ""):
                return self._send(401, {"error": "Sign in first"})
            seen.append({"path": self.path, "body": body})
            if self.path == "/api/nova/agents/xtender-variants/run":
                if body.get("campaignId") == "cmp_missing":
                    return self._send(400, {"error": "No campaign cmp_missing"})
                return self._send(200, {"id": "run_77", "status": "done", "mode": "claude", "model": "claude-sonnet-5-5", "tokensIn": 1200, "tokensOut": 340,
                                        "summary": "6 variants for 3 personas on email, sms", "reads": [1, 2, 3], "writes": [1],
                                        "output": {"variants": [{"assetId": "ast_1", "personaCode": "A1", "channel": "email"}], "reused": 0, "claimsUsed": [], "lessonsApplied": []}})
            return self._send(404, {"error": "Agent not found"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Studio)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", seen
    server.shutdown()


def test_home_runner_calls_the_running_campaign_studio(packs, studio, monkeypatch):
    from caf.core import RunContext, get_agent
    from caf.packs import list_packs

    url, seen = studio
    monkeypatch.setenv("CAF_PACK_CAMPAIGN_STUDIO_URL", url + "/")
    monkeypatch.setenv("CAF_PACK_CAMPAIGN_STUDIO_PASSCODE", "open-sesame")
    assert next(p for p in list_packs() if p["id"] == "campaign_studio")["home"]["connected"] is True
    agent = get_agent("cs_xtender_variants")
    r = agent.run({"request": "Write the variants", "campaign_id": "cmp_42", "per_persona": 2})
    assert r.status == "ok" and r.output["runner"] == "home" and r.output["result"]["variants"][0]["assetId"] == "ast_1"
    assert r.summary.startswith("6 variants") and r.provenance["home"] == {"product": "CX Nova Campaign Studio", "run_id": "run_77", "mode": "claude",
                                                                            "model": "claude-sonnet-5-5", "reads": 3, "writes": 1, "tokens_in": 1200, "tokens_out": 340}
    assert r.metrics["llm_calls"] == 0 and "campaign_studio" in r.provenance["connectors"]  # the studio's model did the work, on its own key
    assert seen[-1] == {"path": "/api/nova/agents/xtender-variants/run", "body": {"input": {"campaignId": "cmp_42", "perPersona": 2}, "campaignId": "cmp_42"}}
    # without a campaign the studio is not asked; when the studio refuses, the run is modelled or twinned here and says why
    assert agent.run({"request": "Write the variants"}).output["runner"] == "twin" and len(seen) == 1
    r2 = agent.run({"request": "Write the variants", "campaign_id": "cmp_missing"})
    assert r2.output["runner"] == "twin" and "could not serve it (400: No campaign cmp_missing)" in r2.provenance["degraded_reasons"][0]
    # a customer's run never acts inside the operator's own product
    r3 = agent.run({"request": "Write the variants", "campaign_id": "cmp_42"}, RunContext.create(options={"customer_id": "acme"}))
    assert r3.output["runner"] == "twin" and "serves the operator's runs only" in r3.provenance["degraded_reasons"][0] and len(seen) == 2


@pytest.fixture()
def client(packs, monkeypatch, tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from caf.api.server import create_app
    from caf.config import reset_settings_cache
    from caf.shop import reset_shops

    monkeypatch.setenv("CAF_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CAF_LLM_PROVIDER", "offline")
    monkeypatch.setenv("CAF_API_TOKEN", "admin-t0ken")
    reset_settings_cache()
    reset_shops()
    with TestClient(create_app()) as c:
        yield c
    reset_settings_cache()
    reset_shops()


def test_packs_on_every_surface(client):
    assert [(p["id"], p["agents"], p["flows"]) for p in client.get("/packs", headers=ADMIN).json()] == [("arm_sdlc", 51, 9), ("campaign_studio", 18, 4), ("intellisight", 5, 2), ("voyagerai", 8, 4)]
    assert len(client.get("/agents", headers=ADMIN).json()) == 110
    assert len(client.get("/agents?pack=arm_sdlc", headers=ADMIN).json()) == 51 and len(client.get("/agents?pack=blueprint", headers=ADMIN).json()) == 28
    health = client.get("/health").json()
    assert health["agents_registered"] == 28 and health["agents_expected"] == 28 and health["agents_total"] == 110 and health["status"] == "ok"
    assert health["packs"][1] == {"id": "campaign_studio", "name": "Campaign Studio", "agents": 18, "flows": 4, "home_connected": False}
    # one agent over REST: the twin offline, with the work of earlier steps accepted
    r = client.post("/agents/arm_design_velocity/run", headers=ADMIN, json={"inputs": {"action": "wireframes", "request": "Event hub pages"},
                    "upstream": {"s01": {"agent_id": "arm_front_velocity", "output": {"summary": "3 options"}}}}).json()
    assert r["status"] == "degraded" and r["output"]["action"] == "wireframes" and r["output"]["runner"] == "twin" and r["artifacts"]
    desc = client.get("/agents/cs_foresight_persona", headers=ADMIN).json()
    assert desc["pack_name"] == "Campaign Studio" and "segment_codes" in desc["input_schema"]["properties"]
    # the storefront: pack agents listed with a model tier and an agent page; pack flows with their steps
    cat = client.get("/shop/catalog").json()
    assert len(cat["agents"]) == 110 and len(cat["flows"]) == 27 and [p["id"] for p in cat["packs"]] == ["arm_sdlc", "campaign_studio", "intellisight", "voyagerai"]
    x = next(a for a in cat["agents"] if a["id"] == "cs_xtender_variants")
    assert x["model_tiers"] == ["balanced"] and x["accelerator"] == "Xtender" and x["price"]["monthly"]
    page = client.get("/shop/catalog/arm_blueswan").json()
    assert len(page["model_steps"]) == 8 and page["model_steps"][0]["step"] == "automation_scope"
    flow = client.get("/shop/catalog/flows/arm_stage_2_requirements").json()
    assert flow["pack_name"] == "ARM SDLC Factory" and flow["inputs"][0]["name"] == "request" and flow["inputs"][0]["required"] is True
    assert client.get("/shop/designer").status_code == 200 and "Designer" in client.get("/shop/designer").text
    # a product's process runs as a flow; a customer who rents the flow runs all of it without renting its agents
    v = client.post("/flows/cs_stage_variants/run", headers=ADMIN, json={"inputs": {"brief": BRIEF}}).json()
    assert [s["id"] for s in v["steps"]] == ["reuse", "variants", "evaluation", "research"] and {s["status"] for s in v["steps"]} == {"degraded"}
    assert v["steps"][1]["result"]["step_inputs"]["brief"] == BRIEF and v["steps"][1]["result"]["step_inputs"]["request"] == "Campaign Studio · Variants"
    assert client.post("/shop/admin/api/customers", json={"id": "acme", "name": "ACME"}, headers=ADMIN).status_code == 201
    client.put("/shop/admin/api/customers/acme/subscriptions", json=[{"kind": "flow", "item_id": "cs_stage_checks"}, {"kind": "agent", "item_id": "arm_site_auditor"}], headers=ADMIN)
    key = client.post("/shop/admin/api/customers/acme/keys", json={}, headers=ADMIN).json()["key"]
    hdr = {"Authorization": f"Bearer {key}"}
    assert [a["id"] for a in client.get("/agents", headers=hdr).json()] == ["arm_site_auditor"]
    assert client.post("/flows/cs_stage_checks/run", headers=hdr, json={"inputs": {}}).status_code == 400  # the brief is required
    run = client.post("/flows/cs_stage_checks/run", headers=hdr, json={"inputs": {"brief": BRIEF}}).json()
    assert len(run["steps"]) == 6 and {s["status"] for s in run["steps"]} == {"degraded"}
    assert client.get("/shop/me", headers=hdr).json()["usage"]["totals"]["runs"] == 6
    assert client.post("/agents/cs_routing/run", headers=hdr, json={}).status_code == 403
    # MCP: the operator sees every agent and flow as a tool; the customer its own
    def tools(h):
        return {t["name"] for t in client.post("/mcp/", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, headers={**MCP_HEADERS, **h}).json()["result"]["tools"]}

    admin_tools = tools(ADMIN)
    from caf.api.mcp_server import GENERIC_TOOLS

    assert len(admin_tools) == 110 + 27 + len(GENERIC_TOOLS) and {"run_cs_xtender_variants", "run_arm_front_velocity", "run_flow_arm_ai_to_z_release", "run_is_analytics_engine", "run_flow_vai_churn_prevention"} <= admin_tools
    assert tools(hdr) == {"run_arm_site_auditor", "run_flow_cs_stage_checks", "list_agents", "list_flows", "run_flow", "orchestrate", "approve_actions",
                          "get_run"}
    # the console the layer hosts gets the pack agents (they run on the server) and every flow in its playbook list
    console = client.get("/console").text
    assert 'id="caf-host-agents"' in console and '"cs_xtender_variants"' in console and "arm_stage_4_qa" in console


def test_designed_flows_mix_packs_keep_their_canvas_and_can_be_long(client):
    flow = {"id": "audit_then_campaign", "name": "Audit, then campaign", "description": "The site audit feeding a campaign's variants.",
            "inputs": [{"name": "url", "required": True, "example": "https://www.meridian-mutual.example/products/home-insurance.html"}, {"name": "brief"}],
            "steps": [{"id": "seo", "agent": "technical_seo_recommendations"},
                      {"id": "audit", "agent": "arm_site_auditor", "inputs": {"action": "estate_audit"}},
                      {"id": "variants", "agent": "cs_xtender_variants", "needs": ["seo", "audit"], "inputs": {"request": "${steps.audit.output.summary}"}}],
            "layout": {"seo": {"x": 60, "y": 50}, "audit": {"x": 60, "y": 154}, "variants": {"x": 360, "y": 102}}}
    v = client.post("/shop/admin/api/flows/validate", json=flow, headers=ADMIN).json()
    assert v["ok"] and v["stages"] == [["seo", "audit"], ["variants"]]
    bad = {**flow, "steps": flow["steps"][:2] + [{"id": "variants", "agent": "cs_xtender_variants", "inputs": {"no_such_field": 1}}]}
    assert "has no input(s) ['no_such_field']" in client.post("/shop/admin/api/flows/validate", json=bad, headers=ADMIN).json()["problems"][0]
    saved = client.put("/shop/admin/api/flows/audit_then_campaign?publish=true", json=flow, headers=ADMIN).json()
    assert saved["status"] == "published" and saved["layout"]["variants"] == {"x": 360, "y": 102}
    listed = next(f for f in client.get("/shop/admin/api/flows", headers=ADMIN).json() if f["id"] == "audit_then_campaign")
    assert listed["layout"]["seo"] == {"x": 60, "y": 50} and listed["agent_names"] == ["Technical SEO Recommendations", "Site Auditor (SiteAudit)", "Xtender variant agent"]
    test = client.post("/shop/admin/api/flows/audit_then_campaign/test", json={}, headers=ADMIN).json()
    assert test["passed"] == 3 and test["steps"][2]["result"]["step_inputs"]["request"].startswith("Site Auditor")
    # a product's whole process fits in one flow: the 50-step release clones into an editable draft and validates
    clone = client.post("/shop/admin/api/flows/arm_ai_to_z_release/clone", json={"new_id": "my_release"}, headers=ADMIN).json()
    assert len(clone["steps"]) == 50 and clone["status"] == "draft" and clone["source"] == "custom" and len(clone["stages"]) > 8


def test_first_load_from_several_threads_at_once(packs):
    """`caf health --packs --only a,b` starts with an empty registry and probes from worker threads: they all reach
    load_agents() together. One must load; the rest must wait for it, not trip over what it registered."""
    from caf.core import get_agent, reload_agents
    from caf.health import liveness

    for _ in range(5):
        reload_agents()
        gate, errors, got = threading.Barrier(8), [], []

        def first_call(agent_id):
            try:
                gate.wait(timeout=10)
                got.append(get_agent(agent_id).spec.id)
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))

        ids = ["arm_front_velocity", "cs_xtender_variants", "utm_generation", "arm_factory_nfr_spec"] * 2
        threads = [threading.Thread(target=first_call, args=(i,)) for i in ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert not errors, errors
        assert sorted(got) == sorted(ids)

    reload_agents()   # the command itself, from a cold registry
    report = liveness(offline=True, packs=True, only=["cs_xtender_variants", "arm_front_velocity"])
    assert report["live"] == 2 and report["total"] == 2, report["agents"]


def test_every_agent_sits_in_a_category_that_names_its_work(packs):
    """The catalog groups all 110 agents by what they do, blueprint and packs side by side, in CATEGORY_ORDER; no agent
    hides under a product name or the catch-all 'platform'."""
    from caf.core import all_agents
    from caf.core.spec import CATEGORY_ORDER, Category

    agents = all_agents()
    assert len(agents) == 110
    assert not [a.spec.id for a in agents if a.spec.category == Category.PLATFORM]
    rank = [CATEGORY_ORDER.index(a.spec.category) for a in agents]
    assert rank == sorted(rank)   # grouped, in the order the work happens
    by_cat = {}
    for a in agents:
        by_cat.setdefault(a.spec.category, []).append(a.spec.pack)
    assert by_cat[Category.STRATEGY][:4] == ["blueprint"] * 4   # within a category the blueprint's agents come first
    assert {"arm_sdlc", "campaign_studio"} <= set(by_cat[Category.STRATEGY])
    from caf.packs import list_packs

    assert {p["id"]: p["tag"] for p in list_packs()} == {"arm_sdlc": "ARM", "campaign_studio": "CS", "intellisight": "IS", "voyagerai": "VAI"}
