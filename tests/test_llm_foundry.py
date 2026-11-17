"""Claude on Microsoft Foundry and shared throttling of model calls, against a local stand-in for a Foundry
deployment (`/anthropic/v1/messages`, Foundry key in x-api-key + api-key)."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("langchain_anthropic")

KEY = "FoundryKey0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ab"


class _Foundry(BaseHTTPRequestHandler):
    requests: list[dict] = []
    reject_structured = False   # behave like an endpoint without output_config support
    reject_effort = False       # behave like an endpoint without output_config.effort
    reject_complex = False      # answer "Schema is too complex." to any schema with an array in it
    fail_first = 0              # answer 429 to the first N calls
    delay = 0.0
    in_flight = 0
    max_in_flight = 0
    lock = threading.Lock()

    def log_message(self, *args):
        pass

    def _send(self, status: int, payload: dict, headers: dict | None = None) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):  # noqa: N802
        cls = type(self)
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        if self.path != "/anthropic/v1/messages":
            return self._send(404, {"error": {"message": f"no route {self.path}"}})
        if self.headers.get("x-api-key") != KEY or self.headers.get("api-key") != KEY:
            return self._send(401, {"type": "error", "error": {"type": "authentication_error", "message": "invalid key"}})
        with cls.lock:
            cls.requests.append(body)
            if cls.fail_first > 0:
                cls.fail_first -= 1
                return self._send(429, {"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}}, {"retry-after": "0"})
            cls.in_flight += 1
            cls.max_in_flight = max(cls.max_in_flight, cls.in_flight)
        try:
            time.sleep(cls.delay)
            if cls.reject_effort and (body.get("output_config") or {}).get("effort"):
                return self._send(400, {"type": "error", "error": {"type": "invalid_request_error", "message": "output_config.effort: Extra inputs are not permitted"}})
            fmt = (body.get("output_config") or {}).get("format")
            if fmt and cls.reject_structured:
                return self._send(400, {"type": "error", "error": {"type": "invalid_request_error", "message": "output_config: Extra inputs are not permitted"}})
            if fmt and cls.reject_complex and '"array"' in json.dumps(fmt.get("schema") or {}):
                return self._send(400, {"type": "error", "error": {"type": "invalid_request_error", "message": "Schema is too complex."}})
            if fmt:
                text = json.dumps({"ok": True})
            else:
                text = '{"ok": true}' if "JSON Schema" in json.dumps(body["messages"]) else "Paris."
            self._send(200, {"id": "msg_f", "type": "message", "role": "assistant", "model": body["model"],
                             "content": [{"type": "text", "text": text}], "stop_reason": "end_turn", "stop_sequence": None,
                             "usage": {"input_tokens": 50, "output_tokens": 10}})
        finally:
            with cls.lock:
                cls.in_flight -= 1


@pytest.fixture()
def foundry(monkeypatch):
    from caf.core.throttle import reset_throttles

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Foundry)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _Foundry.requests, _Foundry.reject_structured, _Foundry.fail_first, _Foundry.delay = [], False, 0, 0.0
    _Foundry.reject_effort = False
    _Foundry.reject_complex = False
    _Foundry.in_flight = _Foundry.max_in_flight = 0
    base = f"http://127.0.0.1:{server.server_address[1]}/anthropic/v1/messages"  # the portal's Target URI works as-is
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_API_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_API_KEY", KEY)
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_BASE_URL", base)
    monkeypatch.setenv("CAF_LLM_PROVIDER", "auto")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    reset_throttles()
    yield _Foundry
    server.shutdown()
    reset_throttles()


def _llm():
    from caf.config import load_settings
    from caf.core.llm import LLM

    return LLM(load_settings().llm)


def test_auto_selects_foundry_and_maps_tiers_to_deployments(foundry):
    llm = _llm()
    assert llm.provider == "anthropic_foundry"
    assert llm.model_for("fast") == "claude-sonnet-5-5" and llm.model_for("pro") == "claude-opus-5"
    assert llm.ping()["model"] == "claude-sonnet-5-5"
    body = foundry.requests[-1]
    assert body["output_config"]["format"]["type"] == "json_schema" and "temperature" not in body
    assert llm.complete("What is the capital of France?", tier="pro") == "Paris."
    assert foundry.requests[-1]["model"] == "claude-opus-5"


def test_structured_falls_back_to_prompt_json_when_endpoint_rejects_output_config(foundry):
    foundry.reject_structured = True
    llm = _llm()
    assert llm.ping()["model"]
    assert llm.structured_method() == "prompt_json"
    assert "format" not in foundry.requests[-1].get("output_config", {})  # JSON asked for in the prompt instead


def test_rate_limit_is_retried_and_pauses_the_deployment(foundry, monkeypatch):
    monkeypatch.setenv("CAF_LLM_RPM", "0")
    foundry.fail_first = 2
    llm = _llm()
    assert llm.ping()["model"]  # the SDK retried past two 429s
    assert len(foundry.requests) == 3


def test_concurrency_cap_holds_across_agents(foundry, monkeypatch):
    monkeypatch.setenv("CAF_LLM_MAX_CONCURRENT", "2")
    monkeypatch.setenv("CAF_LLM_RPM", "0")
    foundry.delay = 0.3
    llm = _llm()
    threads = [threading.Thread(target=lambda: llm.complete("hi", tier="fast")) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert foundry.max_in_flight <= 2 and len(foundry.requests) == 6
    stats = llm._throttle("fast").describe()["stats"]
    assert stats["calls"] == 6 and stats["waited_ms"] > 0


def test_requests_per_minute_and_tokens_per_minute_limits():
    from caf.core.throttle import Throttle

    t = Throttle({"max_concurrent": 0, "requests_per_minute": 2, "tokens_per_minute": 1000, "cooldown_s": 5, "max_wait_s": 1})
    now = time.monotonic()
    t._starts.extend([now - 10, now - 5])
    assert 49 < t._delay(now, 0) < 51          # third call waits for the oldest to leave the minute
    t._starts.clear()
    t._tokens.append([now - 30, 900.0])
    assert t._delay(now, 50) == 0               # fits
    assert 29 < t._delay(now, 200) < 31         # would exceed the token budget
    t.rate_limited(3)
    assert t._delay(time.monotonic(), 0) > 2    # paused after a refusal
    t._tokens.clear()
    import caf.core.throttle as th

    t2 = Throttle({"max_concurrent": 1, "requests_per_minute": 0, "tokens_per_minute": 0, "cooldown_s": 5, "max_wait_s": 0.5})
    with t2.slot():
        with pytest.raises(th.ThrottleTimeout):
            with t2.slot():
                pass


def test_effort_follows_the_kind_of_work_and_is_dropped_if_unsupported(foundry):
    from caf.config import load_settings
    from caf.core import RunContext
    from caf.core.llm import LLM
    from pydantic import BaseModel

    class Ok(BaseModel):
        ok: bool

    llm = LLM(load_settings().llm)
    ctx = RunContext.create(settings=load_settings(), llm=llm)
    for agent, step, effort, model in (("quote_extraction", "extract", "low", "claude-sonnet-5-5"),
                                       ("experiment_ideation", "ideas", "high", "claude-opus-5")):
        actx = ctx.for_agent(agent)
        actx.render = lambda template, **kw: ("system", "user")  # type: ignore[method-assign]
        assert actx.generate(step, Ok).ok
        body = foundry.requests[-1]
        assert body["output_config"]["effort"] == effort and body["model"] == model
    foundry.reject_effort = True
    actx = ctx.for_agent("blog_post_creation")
    actx.render = lambda template, **kw: ("system", "user")  # type: ignore[method-assign]
    assert actx.generate("critique", Ok).ok          # retried without effort
    assert "effort" not in foundry.requests[-1].get("output_config", {})
    assert llm._no_effort


def test_schemas_beyond_the_structured_output_grammar_are_asked_for_in_the_prompt(foundry):
    """Claude's native structured output refuses deeply nested schemas with 'Schema is too complex.' (after a long wait).
    Arrays nested more than two deep go by prompt from the start; a refusal switches that schema to the prompt too."""
    from pydantic import BaseModel

    from caf.core import get_agent

    llm = _llm()
    proposed = get_agent("content_model_creation").schemas["propose"]  # types[] -> fields[] -> enum_values[]: three deep
    assert llm._prompt_only(proposed) and not llm._prompt_only(get_agent("experiment_planning").schemas["plan"])

    class Ok(BaseModel):
        ok: bool
        tags: list[list[str]] = []   # two deep: native structured output as usual

    assert llm.structured("Set ok to true.", Ok, tier="fast").ok
    assert foundry.requests[-1]["output_config"]["format"]["type"] == "json_schema"
    foundry.reject_complex = True
    assert llm.structured("Set ok to true.", Ok, tier="fast").ok           # refused natively, answered through the prompt
    assert "format" not in foundry.requests[-1].get("output_config", {}) and "JSON Schema" in json.dumps(foundry.requests[-1]["messages"])
    n = len(foundry.requests)
    assert llm.structured("Set ok to true.", Ok, tier="fast").ok           # remembered: straight to the prompt, one request
    assert len(foundry.requests) == n + 1 and "format" not in foundry.requests[-1].get("output_config", {})
    assert llm.structured_method() == "json_schema"                         # other schemas still use native structured output
