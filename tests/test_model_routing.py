"""Model routing: every model step is routed by the kind of work it does, adjusted per call, and recorded."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel, create_model

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def router():
    from caf.config import get_settings
    from caf.core.model_router import get_router

    return get_router(get_settings())


def test_every_prompt_template_has_a_routing_decision(router):
    templates = {f"{p.parent.name}.{p.stem}" for p in (ROOT / "config" / "prompts").glob("*/*.j2")}
    missing = sorted(t for t in templates if t not in router.steps)
    assert not missing, f"add these steps to config/model_routing.yaml: {missing}"
    unknown = sorted(s for s, e in router.steps.items() if e.get("work") not in router.work_types)
    assert not unknown, unknown


def test_work_type_drives_the_tier(router):
    assert router.decide("quote_extraction", "extract", agent_tier="balanced").tier == "fast"
    assert router.decide("blog_post_creation", "critique", agent_tier="balanced").tier == "pro"
    assert router.decide("variation_development", "patch", agent_tier="balanced").tier == "complex"
    assert router.decide("email_creation", "repair", agent_tier="balanced").tier == "fast"
    d = router.decide("unknown_agent", "step", agent_tier="pro")
    assert d.tier == "pro" and "not in model_routing.yaml" in d.reasons[0]


def test_adjustments_pins_overrides_and_profiles(router):
    from caf.core.model_router import ModelRouter

    big = 25000 * 3.5 + 10
    assert router.decide("competitive_insights", "synthesis", prompt_chars=int(big)).tier == "complex"  # analyze escalates on size
    assert router.decide("quote_extraction", "extract", prompt_chars=int(big)).tier == "fast"  # extraction does not
    wide = create_model("Wide", **{f"f{i}": (str, ...) for i in range(60)})
    d = router.decide("quote_extraction", "extract", schema=wide)
    assert d.tier == "balanced" and "schema" in d.reasons[-1]
    assert router.decide("quote_extraction", "extract", images=True).tier == "balanced"
    assert router.decide("geo_recommendations", "answer", requested="fast").tier == "fast"  # follow_request
    assert router.decide("blog_post_creation", "critique", override="fast").tier == "fast"
    policy = {**router.policy, "steps": {**router.steps, "blog_post_creation.critique": {"work": "review", "tier": "balanced"}}}
    pinned = ModelRouter(policy).decide("blog_post_creation", "critique", prompt_chars=int(big))
    assert pinned.tier == "balanced" and pinned.pinned and pinned.escalated() is None
    for profile, expected in (("economy", "balanced"), ("quality", "complex")):
        assert ModelRouter({**router.policy, "profile": profile}).decide("blog_post_creation", "critique").tier == expected


def test_invalid_answer_is_retried_one_model_up(monkeypatch):
    from caf.config import get_settings
    from caf.core import RunContext
    from caf.core.llm import LLM, LLMOutputError

    class Out(BaseModel):
        ok: bool

    llm = LLM(get_settings().llm, provider="offline")
    monkeypatch.setattr(type(llm), "offline", property(lambda self: False))
    monkeypatch.setattr(llm, "model_for", lambda tier: {"fast": "small", "balanced": "mid", "pro": "large", "complex": "large"}[tier])
    calls = []

    def structured(prompt, schema, *, system=None, tier="balanced", images=None, retries=1, meter=None):
        calls.append(tier)
        if tier in ("fast", "balanced"):
            raise LLMOutputError("Model output did not match Out")
        return Out(ok=True)

    monkeypatch.setattr(llm, "structured", structured)
    ctx = RunContext.create(llm=llm).for_agent("heatmap_analysis", tier="balanced")
    monkeypatch.setattr(ctx, "render", lambda template, **kw: ("system", "user"))
    assert ctx.generate("insights", Out).ok
    assert calls == ["pro"]  # analyze work starts on pro; nothing to retry
    calls.clear()
    ctx2 = RunContext.create(llm=llm).for_agent("email_creation", tier="balanced")
    monkeypatch.setattr(ctx2, "render", lambda template, **kw: ("system", "user"))
    assert ctx2.generate("draft", Out).ok
    assert calls == ["balanced", "pro"]
    assert [r["model"] for r in ctx2.meter.routes] == ["mid", "large"] and "retry" in ctx2.meter.routes[-1]["why"]


def test_agent_results_list_the_model_used_per_step():
    from caf.core import RunContext, get_agent

    agent = get_agent("blog_post_creation")
    res = agent.run(agent.selftest_input(), RunContext.create())
    routes = res.provenance["model_routes"]
    by_step = {r["step"].split(".", 1)[1]: r["tier"] for r in routes}
    assert by_step["critique"] == "pro" and by_step["outline"] == "balanced"
    assert all(r["why"] for r in routes)


def test_caf_models_table_for_foundry(capsys):
    import json

    from caf.cli import main

    assert main(["models", "--provider", "anthropic_foundry", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    models = {(r["agent"], r["step"]): r["model"] for r in data["steps"]}
    assert models[("variation_development", "patch")] == "claude-opus-5"
    assert models[("email_creation", "draft")] == "claude-sonnet-5-5"
