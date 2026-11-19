"""What a run costs: the price table, the meter, the run index, statistics, the plan estimate and the usage ledger."""
from __future__ import annotations

import pytest

from caf.core.context import Meter
from caf.core.pricing import Pricing, money

from test_runs import ADMIN, shop_client  # noqa: E402,F401  (the API fixture)

TABLE = {"currency": "USD", "cache_read_factor": 0.1, "cache_write_factor": 1.25,
         "models": {"claude-sonnet": {"input": 3, "output": 15}, "claude-sonnet-5-5": {"input": 4, "output": 20},
                    "claude-opus": [15, 75], "offline": {"input": 0, "output": 0}}}


def test_price_table_matches_the_longest_key_a_model_id_contains():
    p = Pricing(TABLE)
    assert p.rate("claude-sonnet-5-5") == (4.0, 20.0)                        # exact
    assert p.rate("anthropic.claude-sonnet-5-5-v1:0") == (4.0, 20.0)         # Bedrock id: the longer key wins
    assert p.rate("claude-sonnet-4-5") == (3.0, 15.0)                         # the family
    assert p.rate("my-opus-deployment") is None and p.rate("claude-opus-5") == (15.0, 75.0)
    assert p.rate("") is None and p.rate(None) is None


def test_cost_counts_cache_reads_and_writes_at_their_factors():
    p = Pricing(TABLE)
    usage = {"input_tokens": 12000, "output_tokens": 800, "input_token_details": {"cache_read": 10000, "cache_creation": 1000}}
    # 1000 plain input at 3 + 10000 cache reads at 0.3 + 1000 cache writes at 3.75 + 800 output at 15, per million
    assert p.cost("claude-sonnet-4-5", usage) == pytest.approx((1000 * 3 + 10000 * 0.3 + 1000 * 3.75 + 800 * 15) / 1e6)
    assert p.cost("claude-sonnet-4-5", {"input_tokens": 1000, "output_tokens": 100}) == pytest.approx(0.0045)
    assert p.cost("offline", {"input_tokens": 1000}) == 0.0
    assert p.cost("unknown-model", {"input_tokens": 1000}) is None            # not priced is not free
    assert money(0) == "$0" and money(0.0004) == "<$0.001" and money(0.0123) == "$0.012" and money(12.5) == "$12.50" and money(None) == "unpriced"


def test_meter_keeps_cost_per_model_and_marks_unpriced_calls():
    m = Meter()
    m.add({"input_tokens": 100, "output_tokens": 10}, model="claude-sonnet-5-5", cost=0.00045)
    m.add({"input_tokens": 50, "output_tokens": 5}, model="claude-sonnet-5-5", cost=0.00022)
    m.add({"input_tokens": 5}, model="mystery", cost=None)
    snap = m.snapshot()
    assert snap["llm_calls"] == 3 and snap["input_tokens"] == 155 and snap["cost_usd"] == pytest.approx(0.00067) and snap["unpriced_calls"] == 1
    assert snap["models"]["claude-sonnet-5-5"]["calls"] == 2 and snap["models"]["mystery"]["unpriced"] is True
    child = Meter()
    child.add({"input_tokens": 10, "output_tokens": 1}, model="claude-opus-5", cost=0.001)
    m.absorb(child.snapshot())
    assert m.snapshot()["cost_usd"] == pytest.approx(0.00167) and set(m.snapshot()["models"]) == {"claude-sonnet-5-5", "mystery", "claude-opus-5"}


def test_llm_prices_each_call_from_the_settings_table(monkeypatch):
    from caf.config import get_settings
    from caf.core.llm import LLM

    settings = get_settings()
    assert Pricing(settings.llm.pricing).rate("claude-sonnet-5-5") is not None   # the shipped table knows the Claude families
    llm = LLM(settings.llm, provider="offline")
    meter = Meter()
    llm.complete("hello", tier="fast", meter=meter)
    assert llm.usage()["cost_usd"] == 0.0 and meter.snapshot()["cost_usd"] == 0.0   # offline is free, and priced as such
    assert llm.describe()["pricing"]["tiers"]["fast"] == (0.0, 0.0)
    assert {x["model"] for x in llm.describe()["pricing"]["models"]} >= {"claude-sonnet", "claude-opus", "offline"}


def test_run_metrics_roll_cost_up_and_record_what_each_agent_cost():
    from caf.orchestrator.service import estimate_plan, run_metrics

    view = {"steps": [{"id": "a", "agent": "x", "result": {"status": "ok", "metrics": {"cost_usd": 0.1, "llm_calls": 2}}},
                      {"id": "b", "agent": "x", "result": {"status": "ok", "metrics": {"cost_usd": 0.2}}},
                      {"id": "c", "agent": "y", "result": {"status": "error", "metrics": {"unpriced_calls": 1}}},
                      {"id": "d", "agent": "z"}],
            "final": {"metrics": {"cost_usd": 0.01, "llm_calls": 1}}}
    m = run_metrics(view)
    assert m["cost_usd"] == pytest.approx(0.31) and m["unpriced_calls"] == 1 and m["agent_costs"] == {"x": 0.3, "y": 0.0}
    est = estimate_plan([{"id": "s1", "agent": "x"}, {"id": "s2", "agent": "z"}], {"x": {"runs": 2, "total_usd": 0.6}})
    assert est["cost_usd"] == pytest.approx(0.3) and est["known_steps"] == 1 and est["steps"] == 2
    assert est["per_step"][1] == {"id": "s2", "agent": "z", "cost_usd": None, "runs": 0}
    assert estimate_plan([{"id": "s", "agent": "q"}], {})["cost_usd"] is None


def test_run_index_keeps_cost_and_agent_history(tmp_path):
    from caf.orchestrator.persistence import MemoryRunStore, RunStore

    for store in (RunStore(tmp_path / "o.db"), MemoryRunStore()):
        store.record("r1", "completed", goal="one")
        store.set_metrics("r1", {"agents": ["x", "y"], "steps": 2, "cost_usd": 0.5, "unpriced_calls": 1, "agent_costs": {"x": 0.4, "y": 0.1}})
        store.record("r2", "completed", goal="two")
        store.set_metrics("r2", {"agents": ["x"], "steps": 1, "cost_usd": 0.2, "agent_costs": {"x": 0.2}})
        row = {r["run_id"]: r for r in store.list(limit=10)}["r1"]
        assert float(row["cost_usd"]) == 0.5 and int(row["unpriced_calls"]) == 1
        hist = store.agent_costs()
        assert hist["x"] == {"runs": 2, "total_usd": pytest.approx(0.6)} and hist["y"]["runs"] == 1


def test_api_reports_cost_on_runs_stats_plan_and_usage(shop_client):
    run = shop_client.post("/orchestrate", json={"goal": "Tag links", "agent": "utm_generation", "inputs": {"url": "https://www.meridian-mutual.example/"}}, headers=ADMIN).json()
    rows = shop_client.get("/runs", headers=ADMIN).json()
    mine = next(r for r in rows if r["run_id"] == run["run_id"])
    assert mine["cost_usd"] == 0.0 and mine["unpriced_calls"] == 0            # offline: priced, at zero
    stats = shop_client.get("/runs/stats?days=2", headers=ADMIN).json()
    assert stats["currency"] == "USD" and "cost_usd" in stats["totals"] and "cost_usd" in stats["today"]
    assert stats["agent_costs"]["utm_generation"]["runs"] >= 1 and stats["top_agents"][0]["avg_cost_usd"] is not None
    plan = shop_client.post("/plan", json={"goal": "", "agent": "utm_generation"}, headers=ADMIN).json()
    assert plan["estimate"]["known_steps"] == 1 and plan["estimate"]["cost_usd"] == 0.0
    unknown = shop_client.post("/plan", json={"goal": "", "agent": "quote_extraction"}, headers=ADMIN).json()
    assert unknown["estimate"]["cost_usd"] is None and unknown["estimate"]["per_step"][0]["runs"] == 0
    health = shop_client.get("/health").json()
    assert health["llm"]["pricing"]["currency"] == "USD" and "tiers" in health["llm"]["pricing"]
