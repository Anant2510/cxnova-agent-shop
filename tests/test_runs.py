"""Runs as a service: durable run state (restart, crash, continue, pruning), runs off the HTTP request (202 + poll on
REST, MCP get_run, A2A working tasks), metering once per step, and approvals taken while the run is still going."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading
import time
from types import SimpleNamespace

import pytest

DEMO = "https://www.meridian-mutual.example/products/home-insurance.html"


# ------------------------------------------------------------------------------------------------ helpers
def settle(get_view, timeout: float = 30.0) -> dict:
    """Poll a run until it settles (finished or at the approval gate)."""
    deadline = time.monotonic() + timeout
    view = get_view()
    while not view.get("settled") and view.get("status") != "stalled":
        assert time.monotonic() < deadline, f"run did not settle: {view.get('status')}"
        time.sleep(0.05)
        view = get_view()
    return view


def until(cond, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "condition not reached"
        time.sleep(0.02)


class Gate:
    """Fake agents for scheduling tests: a step runs until its gate opens (deterministic, no sleeps to tune)."""

    def __init__(self) -> None:
        self.gates: dict[str, threading.Event] = {}
        self.started: list[str] = []
        self.lock = threading.Lock()

    def gate(self, agent_id: str) -> threading.Event:
        with self.lock:
            return self.gates.setdefault(agent_id, threading.Event())

    def install(self, monkeypatch, fake_ids: set[str]) -> None:
        import caf.orchestrator.graph as graph
        from caf.core.spec import AgentResult

        real_get, real_inputs = graph.get_agent, graph.build_step_inputs
        gates = self

        class Fake:
            def __init__(self, agent_id):
                self.spec = SimpleNamespace(id=agent_id)

            def run(self, inputs, ctx, upstream=None, options=None):
                with gates.lock:
                    gates.started.append(self.spec.id)
                assert gates.gate(self.spec.id).wait(30), f"{self.spec.id} gate never opened"
                return AgentResult(agent_id=self.spec.id, run_id=ctx.run_id, status="ok", summary=f"{self.spec.id} done")

        monkeypatch.setattr(graph, "get_agent", lambda aid: Fake(aid) if aid in fake_ids else real_get(aid))
        monkeypatch.setattr(graph, "build_step_inputs",
                            lambda agent, raw, **kw: {} if isinstance(agent, Fake) else real_inputs(agent, raw, **kw))


@pytest.fixture()
def orch(tmp_path):
    from caf.orchestrator import Orchestrator

    o = Orchestrator(store_path=tmp_path / "orchestrator.db")
    yield o
    o.shutdown()


# ------------------------------------------------------------------------------------------------ 1. durable state
def test_held_run_survives_a_restart_and_is_approved_once(tmp_path):
    from caf.orchestrator import Orchestrator

    first = Orchestrator(store_path=tmp_path / "o.db")
    view = first.start("Invite homeowners to a flood webinar", agent="email_creation")
    assert view["status"] == "awaiting_approval" and view["pending_approvals"][0]["key"] == "email_creation/esp-1"
    del first  # the service stops

    again = Orchestrator(store_path=tmp_path / "o.db")  # a new process would see exactly this: only the file
    assert again.view(view["run_id"])["status"] == "awaiting_approval"
    done = again.resume(view["run_id"], {"*": "approve"}, actor="operator")
    assert done["status"] == "completed" and done["steps"][0]["result"]["pending_actions"][0]["status"] == "done"
    ledger = again.store.decisions(view["run_id"])
    assert ledger["email_creation/esp-1"]["decision"] == "approve" and ledger["email_creation/esp-1"]["decided_by"] == "operator"
    with pytest.raises(Exception):  # a settled run cannot be approved again
        again.resume(view["run_id"], {"*": "approve"})


def test_settled_runs_leave_memory(orch):
    plan = {"mode": "custom", "steps": [{"id": "links", "agent": "utm_generation", "inputs": {"base_url": DEMO, "channels": ["email"]}}]}
    ids = [orch.start(f"links {i}", plan=plan)["run_id"] for i in range(3)]
    assert orch.runs() == []                              # no context kept for settled runs
    assert orch.checkpointer.loaded_threads() == 0        # nor their checkpoints
    assert all(orch.view(r)["status"] == "completed" for r in ids)
    assert orch.checkpointer.loaded_threads() == 0        # a view loads a run for the read only


def test_crashed_run_is_stalled_then_continued_without_redoing_finished_steps(tmp_path, monkeypatch):
    """A real crash. A child process runs a plan; while it is alive this process sees the run as running (its heartbeat)
    and may not continue it. The child is killed after step a; once its heartbeat is stale the run is stalled, and this
    process continues it without running step a again."""
    log, marker = tmp_path / "log.txt", tmp_path / "a-done"
    fakes = textwrap.dedent(f"""
        import os, time
        from types import SimpleNamespace
        from caf.core.spec import AgentResult
        class Fake:
            def __init__(self, aid): self.spec = SimpleNamespace(id=aid)
            def run(self, inputs, ctx, upstream=None, options=None):
                with open({str(log)!r}, "a") as f: f.write(f"{{os.getpid()}} {{self.spec.id}}\\n")
                if os.environ.get("HANG") == "1" and self.spec.id == "qr_code_generation":
                    time.sleep(120)
                return AgentResult(agent_id=self.spec.id, run_id=ctx.run_id, status="ok", summary=self.spec.id)
        PLAN = {{"mode": "custom", "steps": [{{"id": "a", "agent": "utm_generation"}}, {{"id": "b", "agent": "qr_code_generation"}},
                                            {{"id": "c", "agent": "keyword_research", "needs": ["a", "b"]}}]}}
        def settings():
            from caf.config import load_settings
            s = load_settings()
            s.orchestrator = {{**s.orchestrator, "stall_after_s": 1.0}}
            return s
    """)
    child = fakes + textwrap.dedent(f"""
        import caf.orchestrator.graph as graph
        graph.get_agent = Fake
        graph.build_step_inputs = lambda agent, raw, **kw: {{}}
        from caf.orchestrator import Orchestrator
        o = Orchestrator(settings(), store_path={str(tmp_path / 'o.db')!r})
        o.start("crash me", plan=PLAN, run_id="orch-20261119-000000-c0ffee", wait=0)
        while [s["status"] for s in o.view("orch-20261119-000000-c0ffee")["steps"][:1]] != ["ok"]:
            time.sleep(0.05)
        open({str(marker)!r}, "w").write("1")
        time.sleep(120)
    """)
    env = {**os.environ, "HANG": "1", "PYTHONPATH": os.pathsep.join(sys.path)}
    proc = subprocess.Popen([sys.executable, "-c", child], env=env, cwd=str(tmp_path))
    try:
        until(lambda: marker.exists() or proc.poll() is not None, timeout=120)
        assert proc.poll() is None, "the child process died before step a finished"

        import caf.orchestrator.graph as graph
        from caf.orchestrator import Orchestrator, RunStateError

        space: dict = {}
        exec(fakes, space)  # noqa: S102 - the same fake agents as the child's
        monkeypatch.delenv("HANG", raising=False)
        monkeypatch.setattr(graph, "get_agent", space["Fake"])
        monkeypatch.setattr(graph, "build_step_inputs", lambda agent, raw, **kw: {})
        o = Orchestrator(space["settings"](), store_path=tmp_path / "o.db")
        alive = o.view("orch-20261119-000000-c0ffee")
        assert alive["status"] == "running" and not alive["resumable"]   # the child is executing it
        with pytest.raises(RunStateError):
            o.continue_run("orch-20261119-000000-c0ffee")
    finally:
        proc.kill()
        proc.wait(timeout=30)
    until(lambda: o.view("orch-20261119-000000-c0ffee")["status"] == "stalled", timeout=10)
    view = o.view("orch-20261119-000000-c0ffee")
    assert view["resumable"] and {s["id"]: s["status"] for s in view["steps"]} == {"a": "ok", "b": "pending", "c": "pending"}
    done = o.continue_run("orch-20261119-000000-c0ffee")
    assert done["status"] == "completed" and {s["status"] for s in done["steps"]} == {"ok"}
    with pytest.raises(RunStateError):
        o.continue_run("orch-20261119-000000-c0ffee")
    runs = log.read_text().split()
    by_agent = {a: [p for p, x in zip(runs[::2], runs[1::2]) if x == a] for a in ("utm_generation", "qr_code_generation", "keyword_research")}
    assert len(by_agent["utm_generation"]) == 1  # step a finished before the crash and was not run again
    assert len(by_agent["qr_code_generation"]) == 2 and len(by_agent["keyword_research"]) == 1


def test_an_action_is_executed_once_when_two_deciders_race(orch, monkeypatch):
    """A decision mid-run and another one at the same moment (two API calls, or two processes): one executes."""
    executed = _count_actions(monkeypatch)
    g = Gate()
    g.install(monkeypatch, {"qr_code_generation"})
    plan = {"mode": "custom", "steps": [{"id": "mail", "agent": "email_creation", "inputs": {"overview": "Flood webinar invitation"}},
                                        {"id": "slow", "agent": "qr_code_generation"}]}
    rid = orch.start("Invite", plan=plan, wait=0)["run_id"]
    until(lambda: orch.view(rid)["pending_approvals"])
    from caf.orchestrator import Orchestrator

    other = Orchestrator(store_path=orch.store_path)  # a second process on the same store
    start = threading.Barrier(2)

    def decide(o):
        start.wait()
        o.resume(rid, {"*": "approve"})

    threads = [threading.Thread(target=decide, args=(o,)) for o in (orch, other)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    g.gate("qr_code_generation").set()
    assert settle(lambda: orch.view(rid))["status"] == "completed"
    assert executed == ["esp-1"]


def test_old_settled_runs_are_pruned(tmp_path):
    from caf.orchestrator import Orchestrator

    o = Orchestrator(store_path=tmp_path / "o.db")
    plan = {"mode": "custom", "steps": [{"id": "links", "agent": "utm_generation", "inputs": {"base_url": DEMO, "channels": ["email"]}}]}
    rid = o.start("links", plan=plan)["run_id"]
    with o.store.db.tx() as con:
        con.execute("UPDATE run_index SET created_at='2020-01-01T00:00:00+00:00' WHERE run_id=?", (rid,))
    assert o.prune(keep_days=30) == 1
    with pytest.raises(KeyError):
        o.view(rid)


def test_memory_store_still_available(monkeypatch):
    from caf.config import load_settings
    from caf.orchestrator import Orchestrator

    monkeypatch.setenv("CAF_RUN_STORE", "memory")
    o = Orchestrator(load_settings())
    assert not o.durable
    plan = {"mode": "custom", "steps": [{"id": "links", "agent": "utm_generation", "inputs": {"base_url": DEMO, "channels": ["email"]}}]}
    assert o.start("links", plan=plan)["status"] == "completed"


# ------------------------------------------------------------------------------------------------ 2. runs off the request
def test_run_on_the_pool_answers_early_and_settles_once(orch, monkeypatch):
    g = Gate()
    g.install(monkeypatch, {"qr_code_generation"})
    settled: list[dict] = []
    plan = {"mode": "custom", "steps": [{"id": "slow", "agent": "qr_code_generation"}]}
    view = orch.start("slow one", plan=plan, wait=0.05, on_settled=settled.append)
    assert view["status"] in ("queued", "running") and not view["settled"] and orch.runs() == [view["run_id"]]
    g.gate("qr_code_generation").set()
    final = settle(lambda: orch.view(view["run_id"]))
    assert final["status"] == "completed"
    until(lambda: len(settled) == 1)
    assert settled[0]["status"] == "completed" and orch.runs() == []


def test_concurrent_runs_are_bounded(tmp_path, monkeypatch):
    from caf.config import load_settings
    from caf.orchestrator import Orchestrator

    settings = load_settings()
    settings.orchestrator = {**settings.orchestrator, "max_concurrent_runs": 1}
    o = Orchestrator(settings, store_path=tmp_path / "o.db")
    g = Gate()
    g.install(monkeypatch, {"qr_code_generation"})
    plan = {"mode": "custom", "steps": [{"id": "slow", "agent": "qr_code_generation"}]}
    first = o.start("one", plan=plan, wait=0)
    second = o.start("two", plan=plan, wait=0)
    until(lambda: len(g.started) == 1)
    time.sleep(0.2)
    assert o.view(second["run_id"])["status"] == "queued" and len(g.started) == 1  # waits for a worker
    g.gate("qr_code_generation").set()
    assert settle(lambda: o.view(first["run_id"]))["status"] == "completed"
    assert settle(lambda: o.view(second["run_id"]))["status"] == "completed"
    o.shutdown()


@pytest.fixture()
def shop_client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from caf.config import reset_settings_cache
    from caf.shop import reset_shops

    monkeypatch.setenv("CAF_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CAF_API_TOKEN", "admin-t0ken")
    reset_settings_cache()
    reset_shops()
    from caf.api.server import create_app

    with TestClient(create_app()) as c:
        yield c
    reset_settings_cache()
    reset_shops()


ADMIN = {"Authorization": "Bearer admin-t0ken"}


def _customer(client, subs, cid="meridian-mutual"):
    assert client.post("/shop/admin/api/customers", json={"id": cid, "name": "Meridian"}, headers=ADMIN).status_code == 201
    assert client.put(f"/shop/admin/api/customers/{cid}/subscriptions", json=subs, headers=ADMIN).status_code == 200
    key = client.post(f"/shop/admin/api/customers/{cid}/keys", json={"label": "t"}, headers=ADMIN).json()["key"]
    return {"Authorization": f"Bearer {key}"}


def test_rest_answers_202_and_the_run_is_followed(shop_client, monkeypatch):
    g = Gate()
    g.install(monkeypatch, {"qr_code_generation"})
    plan = {"mode": "custom", "steps": [{"id": "slow", "agent": "qr_code_generation"}]}
    r = shop_client.post("/orchestrate", json={"goal": "slow", "plan": plan, "wait_s": 0}, headers=ADMIN)
    assert r.status_code == 202 and r.json()["status"] in ("queued", "running") and r.json()["poll"] == f"/runs/{r.json()['run_id']}"
    rid = r.json()["run_id"]
    assert shop_client.get(f"/runs/{rid}", headers=ADMIN).json()["status"] in ("queued", "running")
    g.gate("qr_code_generation").set()
    final = settle(lambda: shop_client.get(f"/runs/{rid}", headers=ADMIN).json())
    assert final["status"] == "completed" and "poll" not in final
    # a short run still answers in one call, as before
    quick = shop_client.post("/orchestrate", json={"goal": "Tag links", "agent": "utm_generation", "inputs": {"url": DEMO}}, headers=ADMIN)
    assert quick.status_code == 200 and quick.json()["status"] == "completed"


def test_customer_run_is_metered_when_it_settles_after_the_response(shop_client, monkeypatch):
    hdr = _customer(shop_client, [{"kind": "all"}])
    g = Gate()
    g.install(monkeypatch, {"qr_code_generation"})
    plan = {"mode": "custom", "steps": [{"id": "links", "agent": "utm_generation"}, {"id": "slow", "agent": "qr_code_generation"}]}
    r = shop_client.post("/orchestrate", json={"goal": "Tag the launch links", "plan": plan, "inputs": {"url": DEMO}, "wait_s": 0}, headers=hdr)
    assert r.status_code == 202
    g.gate("qr_code_generation").set()
    settle(lambda: shop_client.get(f"/runs/{r.json()['run_id']}", headers=hdr).json())
    until(lambda: shop_client.get("/shop/me", headers=hdr).json()["usage"]["totals"]["runs"] == 2)


def test_a2a_task_is_working_then_completed(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from caf.config import reset_settings_cache
    from caf.shop import reset_shops

    monkeypatch.setenv("CAF_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CAF_RUN_WAIT_S", "0")
    reset_settings_cache()
    reset_shops()
    g = Gate()
    g.install(monkeypatch, {"qr_code_generation"})
    from caf.api.server import create_app

    try:
        with TestClient(create_app()) as client:
            body = {"jsonrpc": "2.0", "id": 1, "method": "message/send",
                    "params": {"message": {"role": "user", "messageId": "m1", "parts": [{"kind": "data", "data": {"agent": "qr_code_generation", "goal": "codes"}}]}}}
            task = client.post("/a2a", json=body).json()["result"]
            assert task["kind"] == "task" and task["status"]["state"] in ("submitted", "working")
            g.gate("qr_code_generation").set()

            def state():
                t = client.post("/a2a", json={"jsonrpc": "2.0", "id": 2, "method": "tasks/get", "params": {"id": task["id"]}}).json()["result"]
                return {"settled": t["status"]["state"] not in ("submitted", "working"), "status": t["status"]["state"]}

            assert settle(state)["status"] == "completed"
    finally:
        reset_settings_cache()
        reset_shops()


def test_mcp_get_run_follows_a_run(shop_client):
    from test_api import MCP_HEADERS

    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
    assert shop_client.post("/mcp/", json=init, headers={**MCP_HEADERS, **ADMIN}).status_code == 200
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "orchestrate", "arguments": {"args": {"goal": "Tag links", "agent": "utm_generation", "inputs": {"url": DEMO}}}}}
    res = shop_client.post("/mcp/", json=call, headers={**MCP_HEADERS, **ADMIN}).json()["result"]
    import json as _json

    run = res.get("structuredContent") or _json.loads(res["content"][0]["text"])
    run = run.get("result", run)
    got = shop_client.post("/mcp/", json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                          "params": {"name": "get_run", "arguments": {"args": {"run_id": run["run_id"]}}}},
                           headers={**MCP_HEADERS, **ADMIN}).json()["result"]
    got = got.get("structuredContent") or _json.loads(got["content"][0]["text"])
    assert got.get("result", got)["status"] == "completed"


# ------------------------------------------------------------------------------------------------ 3. metering
def test_each_step_is_metered_even_when_the_agent_repeats(shop_client):
    hdr = _customer(shop_client, [{"kind": "all"}])
    links = {"base_url": DEMO, "channels": ["email"]}
    plan = {"mode": "custom", "steps": [{"id": "first", "agent": "utm_generation", "inputs": links},
                                        {"id": "second", "agent": "utm_generation", "inputs": links, "needs": ["first"]},
                                        {"id": "third", "agent": "utm_generation", "inputs": links, "needs": ["second"]}]}
    v = shop_client.post("/orchestrate", json={"goal": "Tag links three times", "plan": plan}, headers=hdr).json()
    assert v["status"] == "completed"
    from caf.shop import get_shop
    from caf.core.security import service_settings

    shop = get_shop(service_settings())
    assert shop.store.metered_steps(v["run_id"]) == {"first", "second", "third"}
    cust = shop.store.get_customer("meridian-mutual")
    shop.record_view(cust, shop_client.get(f"/runs/{v['run_id']}", headers=hdr).json(), surface="api")  # metering again adds nothing
    usage = shop_client.get("/shop/me", headers=hdr).json()["usage"]
    assert usage["totals"]["runs"] == 3 and usage["lines"][0]["agent_id"] == "utm_generation" and usage["lines"][0]["runs"] == 3


def test_usage_table_from_an_earlier_release_is_migrated(tmp_path):
    import sqlite3

    from caf.shop.store import ShopStore

    path = tmp_path / "shop.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE usage (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, customer_id TEXT NOT NULL, run_id TEXT NOT NULL, "
                "agent_id TEXT NOT NULL, agent_version TEXT DEFAULT '', surface TEXT NOT NULL, run_mode TEXT NOT NULL, provider TEXT DEFAULT '', "
                "status TEXT NOT NULL, llm_calls INTEGER DEFAULT 0, input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0, duration_ms REAL DEFAULT 0)")
    con.execute("INSERT INTO usage(ts,customer_id,run_id,agent_id,surface,run_mode,status) VALUES('2026-11-01','c','r1','a','api','coforge_cloud','ok')")
    con.commit()
    con.close()
    store = ShopStore(path)
    kw = dict(customer_id="c", run_id="r2", agent_id="a", surface="api", run_mode="coforge_cloud", status="ok")
    assert store.record_usage(**kw, step_id="s1") and not store.record_usage(**kw, step_id="s1")  # once per step
    assert store.record_usage(**kw) and store.record_usage(**kw)                                     # single-agent runs: one row per call
    assert store.metered_steps("r2") == {"s1"} and len(store.recent_usage("c")) == 4


def test_daily_limit_counts_runs_as_they_start(shop_client, monkeypatch):
    hdr = _customer(shop_client, [{"kind": "all"}])
    assert shop_client.put("/shop/admin/api/customers/meridian-mutual/policy", json={"limits": {"runs_per_day": 1}}, headers=ADMIN).status_code == 200
    g = Gate()
    g.install(monkeypatch, {"qr_code_generation"})
    plan = {"mode": "custom", "steps": [{"id": "slow", "agent": "qr_code_generation"}]}
    first = shop_client.post("/orchestrate", json={"goal": "slow", "plan": plan, "wait_s": 0}, headers=hdr)
    assert first.status_code == 202  # still going, nothing metered yet
    second = shop_client.post("/orchestrate", json={"goal": "slow", "plan": plan, "wait_s": 0}, headers=hdr)
    assert second.status_code == 429 and "daily limit" in second.json()["detail"]
    g.gate("qr_code_generation").set()
    settle(lambda: shop_client.get(f"/runs/{first.json()['run_id']}", headers=hdr).json())


# ------------------------------------------------------------------------------------------------ 4. approvals mid-run
def _count_actions(monkeypatch) -> list[str]:
    from caf.core.agent import BaseAgent

    done: list[str] = []
    real = BaseAgent.execute_action

    def counting(self, action, ctx):
        done.append(action.id)
        return real(self, action, ctx)

    monkeypatch.setattr(BaseAgent, "execute_action", counting)
    return done


def test_actions_are_approved_while_the_run_is_still_going(orch, monkeypatch):
    executed = _count_actions(monkeypatch)
    g = Gate()
    g.install(monkeypatch, {"qr_code_generation"})
    plan = {"mode": "custom", "steps": [{"id": "mail", "agent": "email_creation", "inputs": {"overview": "Flood webinar invitation"}},
                                        {"id": "slow", "agent": "qr_code_generation"}]}
    view = orch.start("Invite and print", plan=plan, wait=0)
    rid = view["run_id"]
    until(lambda: orch.view(rid)["pending_approvals"])
    mid = orch.view(rid)
    assert mid["status"] == "running" and mid["pending_approvals"][0] | {} and mid["pending_approvals"][0]["early"]
    assert mid["pending_approvals"][0]["key"] == "mail/esp-1"
    after = orch.resume(rid, {"mail/esp-1": "approve"}, actor="marketing-ops")
    assert after["status"] == "running" and after["pending_approvals"] == []           # decided, the run goes on
    mail = next(s for s in after["steps"] if s["id"] == "mail")
    assert mail["result"]["pending_actions"][0]["status"] == "done" and executed == ["esp-1"]
    g.gate("qr_code_generation").set()
    final = settle(lambda: orch.view(rid))
    assert final["status"] == "completed"                                              # no stop at the gate
    assert next(s for s in final["steps"] if s["id"] == "mail")["result"]["pending_actions"][0]["status"] == "done"
    assert executed == ["esp-1"]                                                       # executed once, not again at the gate
    assert orch.store.decisions(rid)["mail/esp-1"]["decided_by"] == "marketing-ops"


def test_gate_holds_only_what_is_still_undecided(orch, monkeypatch):
    executed = _count_actions(monkeypatch)
    g = Gate()
    g.install(monkeypatch, {"qr_code_generation"})
    plan = {"mode": "custom", "steps": [{"id": "a", "agent": "email_creation", "inputs": {"overview": "Flood webinar invitation"}},
                                        {"id": "b", "agent": "email_creation", "inputs": {"overview": "Renewal reminder"}},
                                        {"id": "slow", "agent": "qr_code_generation"}]}
    rid = orch.start("Two emails", plan=plan, wait=0)["run_id"]
    until(lambda: len(orch.view(rid)["pending_approvals"]) == 2)
    orch.resume(rid, {"a/esp-1": "reject"})
    g.gate("qr_code_generation").set()
    held = settle(lambda: orch.view(rid))
    assert held["status"] == "awaiting_approval" and [p["key"] for p in held["pending_approvals"]] == ["b/esp-1"]
    done = orch.resume(rid, {"*": "approve"})
    status = {s["id"]: s["result"]["pending_actions"][0]["status"] for s in done["steps"] if s["id"] in ("a", "b")}
    assert done["status"] == "completed" and status == {"a": "rejected", "b": "done"} and executed == ["esp-1"]


def test_rest_approvals_work_mid_run(shop_client, monkeypatch):
    g = Gate()
    g.install(monkeypatch, {"qr_code_generation"})
    plan = {"mode": "custom", "steps": [{"id": "mail", "agent": "email_creation", "inputs": {"overview": "Flood webinar invitation"}},
                                        {"id": "slow", "agent": "qr_code_generation"}]}
    rid = shop_client.post("/orchestrate", json={"goal": "Invite", "plan": plan, "wait_s": 0}, headers=ADMIN).json()["run_id"]
    until(lambda: shop_client.get(f"/runs/{rid}", headers=ADMIN).json()["pending_approvals"])
    r = shop_client.post(f"/runs/{rid}/approvals", json={"decisions": {"*": "approve"}, "wait_s": 0}, headers=ADMIN)
    assert r.status_code == 202 and r.json()["status"] == "running"                     # was 409 before
    assert next(s for s in r.json()["steps"] if s["id"] == "mail")["result"]["pending_actions"][0]["status"] == "done"
    g.gate("qr_code_generation").set()
    assert settle(lambda: shop_client.get(f"/runs/{rid}", headers=ADMIN).json())["status"] == "completed"
    assert shop_client.post(f"/runs/{rid}/approvals", json={"decisions": {"*": "approve"}}, headers=ADMIN).status_code == 409
    assert shop_client.post(f"/runs/{rid}/continue", headers=ADMIN).status_code == 409


# ------------------------------------------------------------------------------------------------ the run viewer (/shop/runs)
def test_running_steps_show_live_and_finished_steps_carry_timing_and_upstream(orch, monkeypatch):
    g = Gate()
    g.install(monkeypatch, {"qr_code_generation"})
    plan = {"mode": "custom", "steps": [{"id": "links", "agent": "utm_generation", "inputs": {"base_url": DEMO, "channels": ["email"]}},
                                        {"id": "slow", "agent": "qr_code_generation", "needs": ["links"]}]}
    rid = orch.start("Tag and print", plan=plan, wait=0)["run_id"]
    until(lambda: {s["id"]: s["status"] for s in orch.view(rid)["steps"]}.get("slow") == "running")
    live = {s["id"]: s for s in orch.view(rid)["steps"]}
    assert live["slow"]["started_at"] and live["slow"]["result"] is None
    links = live["links"]["result"]
    assert links["timing"]["started_at"] <= links["timing"]["finished_at"] and links["upstream"] == []
    g.gate("qr_code_generation").set()
    done = {s["id"]: s for s in settle(lambda: orch.view(rid))["steps"]}
    assert done["slow"]["result"]["upstream"] == [{"step": "links", "agent_id": "utm_generation", "direct": True}]
    rows = orch.list_runs()
    assert rows[0]["run_id"] == rid and rows[0]["goal"] == "Tag and print" and rows[0]["label"] == "custom plan" and rows[0]["status"] == "completed"


def test_runs_list_trace_and_page(shop_client):
    hdr = _customer(shop_client, [{"kind": "all"}])
    mine = shop_client.post("/orchestrate", json={"goal": "Write the webinar invitation", "agent": "email_creation"}, headers=hdr).json()
    ops = shop_client.post("/orchestrate", json={"goal": "Tag links", "agent": "utm_generation", "inputs": {"url": DEMO}}, headers=ADMIN).json()
    everyone = {r["run_id"]: r for r in shop_client.get("/runs", headers=ADMIN).json()}
    assert {mine["run_id"], ops["run_id"]} <= set(everyone)
    assert everyone[mine["run_id"]]["customer_id"] == "meridian-mutual" and everyone[mine["run_id"]]["label"] == "email_creation"
    assert [r["run_id"] for r in shop_client.get("/runs", headers=hdr).json()] == [mine["run_id"]]   # a customer: its own runs only
    assert shop_client.get("/runs?status=awaiting_approval", headers=ADMIN).json()[0]["run_id"] == mine["run_id"]
    trace = shop_client.get(f"/runs/{mine['run_id']}/trace", headers=hdr).json()
    kinds = [e["kind"] for e in trace]
    assert "orchestrator.step.start" in kinds and any(e["kind"] == "model.done" and e["agent"] == "email_creation" for e in trace)
    assert shop_client.get(f"/runs/{mine['run_id']}/trace?since={len(trace) - 2}", headers=hdr).json() == trace[-2:]
    page = shop_client.get("/shop/runs")
    assert page.status_code == 200 and "Runs ·" in page.text and "/shop/static/shop.js" in page.text
    assert shop_client.get("/runs").status_code == 401   # the list needs a token


def test_deciding_some_held_actions_at_the_gate_keeps_the_others_held(orch, monkeypatch):
    executed = _count_actions(monkeypatch)
    plan = {"mode": "custom", "steps": [{"id": "a", "agent": "email_creation", "inputs": {"overview": "Flood webinar invitation"}},
                                        {"id": "b", "agent": "email_creation", "inputs": {"overview": "Renewal reminder"}}]}
    view = orch.start("Two emails", plan=plan)
    assert view["status"] == "awaiting_approval" and len(view["pending_approvals"]) == 2
    one = orch.resume(view["run_id"], {"a/esp-1": "approve"})          # b is not named: it stays held, it is not rejected
    assert one["status"] == "awaiting_approval" and [p["key"] for p in one["pending_approvals"]] == ["b/esp-1"]
    assert executed == ["esp-1"]
    done = orch.resume(view["run_id"], {"b/esp-1": "reject"})
    status = {s["id"]: s["result"]["pending_actions"][0]["status"] for s in done["steps"]}
    assert done["status"] == "completed" and status == {"a": "done", "b": "rejected"} and executed == ["esp-1"]
