"""The Claude adapter against a local stand-in for the Messages API: request shape (no forced temperature,
native structured outputs, output budget) and a full agent run through the real langchain-anthropic client."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

pytest.importorskip("langchain_anthropic")


def _schema_classes():
    from caf.core import all_agents
    from caf.orchestrator.graph import RunSynthesis
    from caf.orchestrator.router import Plan

    classes = [Plan, RunSynthesis]
    for agent in all_agents():
        classes += list(agent.schemas.values())
    return classes


class _Claude(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def log_message(self, *args):  # quiet
        pass

    def do_POST(self):  # noqa: N802 - http.server API
        from caf.core.offline import OfflineModel

        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        type(self).requests.append(body)
        fmt = ((body.get("output_config") or {}).get("format") or {})
        prompt = json.dumps(body.get("messages"))
        if fmt.get("type") == "json_schema":
            from langchain_anthropic.chat_models import _convert_to_anthropic_output_config_format as convert

            wanted = fmt.get("schema") or {}  # find the Pydantic class this request was built from
            cls = next((c for c in _schema_classes() if convert(c).get("schema") == wanted), None)
            if cls is not None and cls.__name__ == "Plan":  # the planner: pick a real playbook with its steps, as the prompt asks
                from caf.orchestrator import load_playbooks
                from caf.orchestrator.router import plan_from_playbook

                plan = plan_from_playbook(load_playbooks()["campaign_launch"]).model_dump()
                text = json.dumps({**plan, "mode": "playbook", "playbook": "campaign_launch", "reason": "A launch kit fits the goal."})
            elif cls is not None:
                text = OfflineModel().structured(prompt, cls, None).model_dump_json()
            elif set(wanted.get("properties", {})) == {"translations"}:  # the LLM translation connector's inline model
                import re

                n = int(re.search(r"Return exactly (\d+) translations", prompt).group(1))
                text = json.dumps({"translations": [f"[fr] segment {i + 1}" for i in range(n)]})
            else:
                raise AssertionError(f"stand-in has no schema for {sorted(wanted.get('properties', {}))}")
        elif "JSON Schema: {" in prompt:  # a schema asked for in the prompt (beyond the native structured-output grammar)
            last = body["messages"][-1]["content"]
            last = last if isinstance(last, str) else "".join(b.get("text", "") for b in last if isinstance(b, dict))
            spec = json.loads(last.split("JSON Schema: ", 1)[1].split("\n\nYour previous reply", 1)[0])
            cls = next((c for c in _schema_classes() if c.model_json_schema() == spec), None)
            if cls is None:
                raise AssertionError(f"stand-in has no schema for prompt-requested {sorted(spec.get('properties', {}))}")
            text = OfflineModel().structured(prompt, cls, None).model_dump_json()
        else:
            text = "A short answer from the stand-in model."
        reply = {"id": "msg_test", "type": "message", "role": "assistant", "model": body["model"],
                 "content": [{"type": "text", "text": text}], "stop_reason": "end_turn", "stop_sequence": None,
                 "usage": {"input_tokens": 120, "output_tokens": 80}}
        data = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture()
def claude(monkeypatch):
    server = HTTPServer(("127.0.0.1", 0), _Claude)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _Claude.requests = []
    url = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", url)
    monkeypatch.setenv("ANTHROPIC_API_URL", url)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    yield _Claude.requests
    server.shutdown()


def test_claude_requests_use_model_defaults_and_native_structured_outputs(claude):
    from caf.config import get_settings
    from caf.core import RunContext, get_agent
    from caf.core.llm import LLM

    settings = get_settings()
    llm = LLM(settings.llm, provider="anthropic")
    ctx = RunContext.create(settings=settings, llm=llm)
    res = get_agent("campaign_brief_generation").run(get_agent("campaign_brief_generation").selftest_input(), ctx)
    assert res.status in ("ok", "needs_approval"), res.error
    assert res.metrics["llm_calls"] >= 1
    assert claude, "no request reached the stand-in API"
    for body in claude:
        assert "temperature" not in body  # Claude Sonnet 5.5 rejects non-default temperature
        assert body["max_tokens"] == 16000
        assert body["model"].startswith("claude-")
    structured = [b for b in claude if (b.get("output_config") or {}).get("format", {}).get("type") == "json_schema"]
    assert structured and "tools" not in structured[0]  # native structured outputs, not forced tool calls


def test_orchestrator_plans_and_runs_a_playbook_on_claude(claude, monkeypatch):
    from caf.config import get_settings
    from caf.orchestrator import Orchestrator

    monkeypatch.setenv("CAF_LLM_PROVIDER", "anthropic")
    orch = Orchestrator(get_settings())
    view = orch.start("Launch our flood-ready home cover: brief, email, LinkedIn, press release, social, tagged links and QR codes",
                      inputs={"url": "https://www.meridian-mutual.example/products/home-insurance.html"})
    assert view["route"]["method"] == "planner" and view["route"]["playbook"] == "campaign_launch", view["warnings"]
    if view["status"] == "awaiting_approval":
        view = orch.resume(view["run_id"], {"*": "approve"})
    assert view["status"] == "completed", [(s["id"], s["status"]) for s in view["steps"]]
    assert all(s["status"] == "ok" for s in view["steps"])  # real model path: nothing degraded to placeholders
    assert view["final"]["method"] == "model"
    assert not any("temperature" in b for b in claude)



class _Rejecting(BaseHTTPRequestHandler):
    """The Messages API answering a key it does not accept."""

    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers["content-length"]))
        data = json.dumps({"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}).encode()
        self.send_response(401)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture()
def rejecting(monkeypatch):
    server = HTTPServer(("127.0.0.1", 0), _Rejecting)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "NotAnAnthropicKeyButSomethingElse0123456789")
    for k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_URL"):
        monkeypatch.setenv(k, url)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    yield
    server.shutdown()


def test_rejected_key_is_reported_once_and_never_as_ok(rejecting, monkeypatch, capsys):
    from caf.cli import main
    from caf.config import get_settings
    from caf.core import RunContext, get_agent
    from caf.core.llm import LLM

    monkeypatch.setenv("CAF_LLM_PROVIDER", "anthropic")
    assert main(["health", "--live", "--fast"]) == 2
    err = capsys.readouterr().err
    assert "did not answer a one-line test call" in err and "not accepted" in err and "does not start with 'sk-ant-'" in err

    ctx = RunContext.create(settings=get_settings(), llm=LLM(get_settings().llm, provider="anthropic"))
    for agent_id in ("quote_extraction", "content_summary"):  # agents that fall back to their own methods
        res = get_agent(agent_id).run(get_agent(agent_id).selftest_input(), ctx)
        assert res.status != "ok", agent_id
        if res.status != "error":
            assert any("model call failed" in r for r in res.provenance.get("degraded_reasons", [])), agent_id
