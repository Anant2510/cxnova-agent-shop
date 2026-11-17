"""Every agent in the blueprint must be registered, describable and live end-to-end.

Runs each agent's self-test through its full LangGraph (deterministic stages, validators,
exporters, connectors). With CAF_LLM_PROVIDER=offline, model calls are answered by the offline
model; set CAF_TEST_LLM_PROVIDER=anthropic (or another provider) to run the same suite live.
"""

import json

import pytest

from caf.core import expected_agent_ids, get_agent, missing_agents

AGENT_IDS = expected_agent_ids()


def test_blueprint_has_28_agents():
    assert len(AGENT_IDS) == 28


def test_every_blueprint_agent_is_registered():
    assert missing_agents() == []


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_agent_contract_exports(agent_id):
    agent = get_agent(agent_id)
    desc = agent.describe()
    json.dumps(desc)  # must be JSON-serialisable for the API, MCP and the console
    assert desc["name"] and desc["summary"] and desc["category"]
    assert desc["use_cases"], "blueprint use cases should be merged into the spec"
    assert desc["graph"]["nodes"], "agent must compile to a LangGraph graph"
    from caf.config import get_settings
    from caf.core.prompts import PromptStore

    prompts = PromptStore(get_settings().prompts_dirs)
    for template in agent.schemas:
        assert prompts.exists(agent_id, template), f"schemas['{template}'] has no config/prompts/{agent_id}/{template}.j2"


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_agent_is_live(agent_id, ctx):
    agent = get_agent(agent_id)
    result = agent.run(agent.selftest_input(), ctx)
    assert result.status != "error", result.error
    assert result.summary, "agents must return a one-line summary"
    json.dumps(result.model_dump(mode="json"))
