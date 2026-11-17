"""Orchestrator: routing, plan validation, bindings, parallel DAG execution, approval interrupts,
playbook coverage of all 28 agents, and every playbook running end-to-end."""

import pytest

from caf.core import expected_agent_ids, get_agent, missing_agents
from caf.orchestrator import Orchestrator, coverage, deterministic_route, load_playbooks, validate_playbooks
from caf.orchestrator.bindings import build_step_inputs, resolve
from caf.orchestrator.router import Plan, validate_plan

DEMO = "https://www.meridian-mutual.example/products/home-insurance.html"


def test_every_agent_is_reachable_from_a_playbook():
    cov = coverage()
    assert [a for a in expected_agent_ids() if a not in cov] == []


def test_playbooks_reference_registered_agents():
    if missing_agents():
        pytest.skip("agents still being registered")
    assert validate_playbooks() == []


@pytest.mark.parametrize("goal,expected", [
    ("Create UTM tracking links for our Q4 email and paid social", ("agent", "utm_generation")),
    ("Write a press release announcing our new flood cover partnership", ("agent", "press_release_creation")),
    ("Audit our website for accessibility, technical SEO and AI search readiness and suggest tests", ("playbook", "website_audit")),
])
def test_deterministic_router(goal, expected):
    d = deterministic_route(goal)
    got = (d.mode, d.agent if d.mode == "agent" else d.playbook)
    if expected[1] not in [a.spec.id for a in __import__("caf.core", fromlist=["all_agents"]).all_agents()] and expected[0] == "agent":
        pytest.skip(f"{expected[1]} not registered yet")
    assert got == expected, d.model_dump()


def test_bindings_resolve_goal_inputs_and_upstream():
    results = {"links": {"output": {"links": [{"channel": "email", "url": "https://x.example/?utm_source=a"}]}}}
    v = resolve({"a": "$goal", "b": "${input.url}", "c": "${steps.links.output.links.0.url}", "d": "${input.missing|'fallback'}",
                 "e": {"$ref": "steps.links.output.links"}}, goal="G", inputs={"url": "U"}, results=results)
    assert v == {"a": "G", "b": "U", "c": "https://x.example/?utm_source=a", "d": "fallback", "e": results["links"]["output"]["links"]}


def test_goal_fills_declared_primary_inputs_only():
    utm = get_agent("utm_generation")
    out = build_step_inputs(utm, {}, goal="some goal", inputs={"url": DEMO}, results={})
    assert out == {"base_url": DEMO}
    email = get_agent("email_creation")
    assert build_step_inputs(email, {}, goal="G", inputs={}, results={})["overview"] == "G"


def test_plan_validation_drops_unknown_agents_and_bad_deps():
    plan = Plan(mode="custom", steps=[{"id": "a", "agent": "utm_generation", "inputs": {"base_url": DEMO, "nope": 1}},
                                      {"id": "b", "agent": "does_not_exist"},
                                      {"id": "c", "agent": "email_creation", "needs": ["a", "zzz"]}])
    cleaned, warnings = validate_plan(plan)
    assert [s.id for s in cleaned.steps] == ["a", "c"]
    assert cleaned.steps[0].inputs == {"base_url": DEMO}
    assert cleaned.steps[1].needs == ["a"]
    assert len(warnings) == 3


def test_dag_runs_with_upstream_flow_and_approval_gate():
    orch = Orchestrator()
    plan = {"mode": "custom", "steps": [
        {"id": "brief", "agent": "campaign_brief_generation", "inputs": {"campaign_type": "webinar"}},
        {"id": "links", "agent": "utm_generation", "needs": ["brief"], "inputs": {"base_url": "${input.url}", "channels": ["email"]}},
        {"id": "email", "agent": "email_creation", "needs": ["brief", "links"]}]}
    view = orch.start("Flood webinar invitation", inputs={"url": DEMO}, plan=plan)
    assert view["status"] == "awaiting_approval"
    statuses = {s["id"]: s["status"] for s in view["steps"]}
    assert statuses["links"] == "ok" and statuses["email"] == "needs_approval"
    email_out = next(s for s in view["steps"] if s["id"] == "email")["result"]["output"]
    assert "utm_medium=email" in email_out["emails"][0]["cta_url"]  # upstream UTM link flowed into the CTA
    final = orch.resume(view["run_id"], {"*": "reject"})
    assert final["status"] == "completed"
    actions = next(s for s in final["steps"] if s["id"] == "email")["result"]["pending_actions"]
    assert all(a["status"] == "rejected" for a in actions)


def test_failed_dependency_skips_downstream():
    orch = Orchestrator()
    plan = {"mode": "custom", "steps": [
        {"id": "bad", "agent": "utm_generation", "inputs": {"base_url": DEMO, "channels": ["email"], "mode": "not-a-mode"}},
        {"id": "after", "agent": "email_creation", "needs": ["bad"]}]}
    view = orch.start("x", plan=plan)
    statuses = {s["id"]: s["status"] for s in view["steps"]}
    assert statuses == {"bad": "error", "after": "skipped"}


@pytest.mark.parametrize("playbook", sorted(load_playbooks()))
def test_every_playbook_runs_end_to_end(playbook):
    if missing_agents():
        pytest.skip("agents still being registered")
    orch = Orchestrator()
    view = orch.start("Run this playbook end to end for Meridian Mutual", playbook=playbook, auto_approve=True,
                      inputs={"url": DEMO, "audience": "Homeowners in flood-exposed areas", "industry": "insurance",
                              "competitors": ["Harborline Insurance", "Crestview Assurance"], "question": "conversion rate by channel"})
    bad = {s["id"]: (s["result"] or {}).get("error") for s in view["steps"] if s["status"] in ("error", "skipped", "pending")}
    assert not bad, bad
    if view["status"] == "awaiting_approval":  # mandatory gates (e.g. blog publish) hold even under auto-approve
        assert all(p.get("mandatory") for p in view["pending_approvals"]), view["pending_approvals"]
        view = orch.resume(view["run_id"], {"*": "approve"})
    assert view["status"] == "completed"


def test_queued_step_starts_when_any_slot_frees_and_run_summary_is_written(monkeypatch):
    """With max_parallel=2, a third ready step starts as soon as the first step finishes, not after the
    slowest one in its batch; the run ends with runs/<run>/summary.md and an orchestrator.end trace event."""
    import json
    import threading
    import time as _time
    from types import SimpleNamespace

    import caf.orchestrator.graph as graph
    from caf.config import load_settings
    from caf.core.spec import AgentResult

    durations = {"utm_generation": 0.2, "qr_code_generation": 0.8, "keyword_research": 0.5}
    times: dict[str, list[float]] = {}
    lock = threading.Lock()

    class Fake:
        def __init__(self, agent_id):
            self.spec = SimpleNamespace(id=agent_id)

        def run(self, inputs, ctx, upstream=None, options=None):
            start = _time.monotonic()
            _time.sleep(durations[self.spec.id])
            with lock:
                times[self.spec.id] = [start, _time.monotonic()]
            return AgentResult(agent_id=self.spec.id, run_id=ctx.run_id, status="ok", summary=f"{self.spec.id} done")

    monkeypatch.setattr(graph, "get_agent", Fake)
    monkeypatch.setattr(graph, "build_step_inputs", lambda agent, raw, **kw: {})
    settings = load_settings()
    settings.orchestrator = {**settings.orchestrator, "max_parallel": 2}
    orch = Orchestrator(settings)
    plan = {"mode": "custom", "steps": [{"id": "a", "agent": "utm_generation"}, {"id": "b", "agent": "qr_code_generation"},
                                        {"id": "c", "agent": "keyword_research"}]}
    view = orch.start("x", plan=plan)
    assert view["status"] == "completed"
    first, second, third = sorted(times.values())  # by start time; which two go first is up to the thread pool
    assert third[0] >= min(first[1], second[1]) - 0.05   # never more than two at once
    assert third[0] < max(first[1], second[1]) - 0.1     # ...and the third did not wait for the slower of the two
    report = settings.runs_dir / view["final"]["report"]
    text = report.read_text()
    assert "3/3 steps completed" in text and "| c | keyword_research | ok |" in text
    events = [json.loads(line) for line in (settings.runs_dir / view["run_id"] / "trace.jsonl").read_text().splitlines()]
    end = [e for e in events if e["kind"] == "orchestrator.end"]
    assert end and end[0]["status"] == "completed" and view["final"]["duration_s"] >= 0.8
