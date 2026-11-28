"""The two protocols other systems reach this layer by, at their current versions.

A2A 1.0 (the first stable specification) beside 0.3: one endpoint, read by method name, the way the protocol's own SDKs
serve both. MCP in step with its 2026-07-28 revision: every tool says what calling it does, the tool list is stable and
private to the caller, and nothing depends on a session.
"""

from __future__ import annotations

import re
import time

import pytest
from fastapi.testclient import TestClient

ADMIN = {"Authorization": "Bearer admin-t0ken"}
V1 = {"A2A-Version": "1.0"}
STAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")


@pytest.fixture()
def client(monkeypatch, tmp_path):
    from caf.api.server import create_app
    from caf.config import reset_settings_cache
    from caf.shop import reset_shops

    monkeypatch.setenv("CAF_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CAF_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("CAF_LLM_PROVIDER", "offline")
    monkeypatch.setenv("CAF_API_TOKEN", "admin-t0ken")
    monkeypatch.setenv("CAF_SECRET_KEY", "a passphrase for the shop's vault")
    reset_settings_cache()
    reset_shops()
    with TestClient(create_app()) as c:
        yield c
    reset_settings_cache()
    reset_shops()


def customer(client, cid: str, agents: list[str]) -> dict[str, str]:
    assert client.post("/shop/admin/api/customers", json={"id": cid, "name": cid.title()}, headers=ADMIN).status_code == 201
    subs = [{"kind": "agent", "item_id": a} for a in agents]
    assert client.put(f"/shop/admin/api/customers/{cid}/subscriptions", json=subs, headers=ADMIN).status_code == 200
    key = client.post(f"/shop/admin/api/customers/{cid}/keys", json={"label": "test"}, headers=ADMIN).json()["key"]
    return {"Authorization": f"Bearer {key}"}


def rpc(client, method: str, params: dict | None = None, headers: dict | None = None, path: str = "/a2a") -> dict:
    return client.post(path, json={"jsonrpc": "2.0", "id": 7, "method": method, "params": params or {}}, headers=headers or {}).json()


def send(text: str | None = None, data: dict | None = None, **message) -> dict:
    parts = ([{"text": text}] if text else []) + ([{"data": data}] if data else [])
    return {"message": {"messageId": "m-1", "role": "ROLE_USER", "parts": parts, **message}}


def settled(client, task_id: str, headers: dict) -> dict:
    for _ in range(120):
        task = rpc(client, "GetTask", {"id": task_id}, headers)["result"]
        if task["status"]["state"] not in ("TASK_STATE_SUBMITTED", "TASK_STATE_WORKING"):
            return task
        time.sleep(0.25)
    raise AssertionError(f"task {task_id} did not settle: {task['status']}")


# ------------------------------------------------------------------------------------------------ A2A: the card
def test_the_card_is_written_for_the_version_the_reader_asks_for(client):
    acme = customer(client, "acme", ["utm_generation", "email_creation"])
    new = client.get("/.well-known/agent-card.json", headers={**acme, **V1})
    card = new.json()
    assert card["supportedInterfaces"] == [
        {"url": "http://testserver/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0", "tenant": "acme"},
        {"url": "http://testserver/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "0.3", "tenant": "acme"}]
    assert not {"url", "protocolVersion", "preferredTransport"} & set(card)   # 1.0 moved them into the interfaces
    assert card["capabilities"] == {"streaming": False, "pushNotifications": False, "extendedAgentCard": True}
    assert card["securitySchemes"]["bearer"] == {"httpAuthSecurityScheme": {"scheme": "Bearer", "description": card["securitySchemes"]["bearer"]["httpAuthSecurityScheme"]["description"]}}
    assert card["securitySchemes"]["apiKey"]["apiKeySecurityScheme"]["location"] == "header"
    assert card["securityRequirements"] == [{"schemes": {"bearer": {"list": []}}}, {"schemes": {"apiKey": {"list": []}}}]
    assert {s["id"] for s in card["skills"]} == {"utm_generation", "email_creation"} and card["documentationUrl"].endswith("/docs")
    # it varies with the version and with who asks, and says so; a client that has it is told it has not changed
    assert new.headers["vary"] == "A2A-Version, Authorization, X-API-Key" and new.headers["cache-control"] == "private, max-age=300"
    again = client.get("/.well-known/agent-card.json", headers={**acme, **V1, "If-None-Match": new.headers["etag"]})
    assert again.status_code == 304 and again.content == b""

    # a reader that names no version is a 0.3 client: the same card with the fields it reads at the top
    old = client.get("/.well-known/agent-card.json")
    legacy = old.json()
    assert legacy["protocolVersion"] == "0.3.0" and legacy["url"] == "http://testserver/a2a" and legacy["preferredTransport"] == "JSONRPC"
    assert [i["protocolVersion"] for i in legacy["supportedInterfaces"]] == ["1.0", "0.3"] and "tenant" not in legacy["supportedInterfaces"][0]
    assert legacy["security"] == [{"bearer": []}, {"apiKey": []}] and legacy["securitySchemes"]["bearer"]["type"] == "http"
    assert old.headers["cache-control"] == "public, max-age=300" and old.headers["etag"] != new.headers["etag"]
    assert len(legacy["skills"]) > 28   # the public card: the whole catalogue
    # the version can come as a query parameter, and one agent's card follows the same rule
    assert "url" not in client.get("/.well-known/agent-card.json?A2A-Version=1.0").json()
    one = client.get("/a2a/agents/utm_generation/agent-card.json", headers={**acme, **V1}).json()
    assert one["supportedInterfaces"][0]["url"] == "http://testserver/a2a/agents/utm_generation" and [s["id"] for s in one["skills"]] == ["utm_generation"]


# ------------------------------------------------------------------------------------------------ A2A: 1.0 messages
def test_a_1_0_client_sends_a_message_and_follows_the_task(client):
    acme = {**customer(client, "acme", ["utm_generation", "email_creation"]), **V1}
    link = {"agent": "utm_generation", "inputs": {"base_url": "https://www.acme.example/fares", "campaign": "2026q4_us_fares_launch", "channels": ["email"]}}
    sent = rpc(client, "SendMessage", send("Tag the launch links", link), acme)["result"]
    assert set(sent) == {"task"}   # the answer says which it is by the member it carries, not by `kind`
    task = settled(client, sent["task"]["id"], acme)
    assert task["status"]["state"] == "TASK_STATE_COMPLETED" and STAMP.match(task["status"]["timestamp"])
    said = task["status"]["message"]
    assert said["role"] == "ROLE_AGENT" and set(said["parts"][0]) == {"text"} and "kind" not in task and "kind" not in said
    part = task["artifacts"][0]["parts"][0]
    assert part["mediaType"] == "application/json" and part["data"]["status"] == "ok" and part["data"]["output"]["links"]
    assert task["contextId"] == task["id"] and task["metadata"] == {"runStatus": "completed", "pendingApprovals": []}

    # asked not to wait, it answers at once with the task as it stands
    quick = rpc(client, "SendMessage", {**send("Tag the links", link), "configuration": {"returnImmediately": True}}, acme)["result"]["task"]
    assert quick["status"]["state"] in ("TASK_STATE_SUBMITTED", "TASK_STATE_WORKING", "TASK_STATE_COMPLETED")
    assert settled(client, quick["id"], acme)["status"]["state"] == "TASK_STATE_COMPLETED"

    # its tasks, newest first, a page at a time; another customer sees none of them
    listed = rpc(client, "ListTasks", {"pageSize": 1}, acme)["result"]
    assert listed["totalSize"] == 2 and listed["pageSize"] == 1 and listed["nextPageToken"] == "1" and "artifacts" not in listed["tasks"][0]
    rest = rpc(client, "ListTasks", {"pageSize": 1, "pageToken": "1", "includeArtifacts": True}, acme)["result"]
    assert rest["nextPageToken"] == "" and {listed["tasks"][0]["id"], rest["tasks"][0]["id"]} == {task["id"], quick["id"]} and rest["tasks"][0]["artifacts"]
    assert rpc(client, "ListTasks", {"status": "TASK_STATE_FAILED"}, acme)["result"]["tasks"] == []
    since = {"statusTimestampAfter": "2026-01-01T00:00:00.000Z"}
    assert rpc(client, "ListTasks", since, acme)["result"]["totalSize"] == 2
    assert rpc(client, "ListTasks", {"statusTimestampAfter": "2999-01-01T00:00:00Z"}, acme)["result"]["totalSize"] == 0
    for bad in ({"statusTimestampAfter": "last tuesday"}, {"pageSize": 500}, {"pageToken": "next"}, {"status": "DONE"}):
        assert rpc(client, "ListTasks", bad, acme)["error"]["code"] == -32602, bad
    other = {**customer(client, "globex", ["utm_generation"]), **V1}
    assert rpc(client, "ListTasks", {}, other)["result"]["totalSize"] == 0
    missing = rpc(client, "GetTask", {"id": task["id"]}, other)["error"]
    assert missing["code"] == -32001 and missing["data"] == [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "TASK_NOT_FOUND",
                                                              "domain": "a2a-protocol.org", "metadata": {"taskId": task["id"]}}]

    # its own card over the endpoint, and the tenant in that card is the only one its requests may name
    mine = rpc(client, "GetExtendedAgentCard", {}, acme)["result"]
    assert {s["id"] for s in mine["skills"]} == {"utm_generation", "email_creation"} and mine["supportedInterfaces"][0]["tenant"] == "acme"
    assert rpc(client, "GetTask", {"id": task["id"], "tenant": "acme"}, acme)["result"]["id"] == task["id"]
    assert rpc(client, "GetTask", {"id": task["id"], "tenant": "globex"}, acme)["error"]["code"] == -32602


def test_what_is_not_offered_is_refused_by_name(client):
    acme = {**customer(client, "acme", ["utm_generation"]), **V1}
    link = {"agent": "utm_generation", "inputs": {"base_url": "https://www.acme.example/fares", "campaign": "2026q4_us_fares_launch", "channels": ["email"]}}
    task = settled(client, rpc(client, "SendMessage", send("Tag the links", link), acme)["result"]["task"]["id"], acme)

    def refused(method, params=None, headers=acme):
        error = rpc(client, method, params, headers)["error"]
        return error["code"], (error.get("data") or [{}])[0].get("reason")

    assert refused("SendStreamingMessage", send("x")) == (-32004, "UNSUPPORTED_OPERATION")
    assert refused("SubscribeToTask", {"id": task["id"]}) == (-32004, "UNSUPPORTED_OPERATION")
    assert refused("CreateTaskPushNotificationConfig", {"taskId": task["id"]}) == (-32003, "PUSH_NOTIFICATION_NOT_SUPPORTED")
    assert refused("SendMessage", {**send("x", link), "configuration": {"taskPushNotificationConfig": {"url": "https://example.com/hook"}}}) == (-32003, "PUSH_NOTIFICATION_NOT_SUPPORTED")
    assert refused("CancelTask", {"id": task["id"]}) == (-32002, "TASK_NOT_CANCELABLE")      # it has ended
    assert refused("CancelTask", {"id": "run-nope"}) == (-32001, "TASK_NOT_FOUND")
    assert refused("SendMessage", send("Change the campaign name", taskId=task["id"])) == (-32004, "UNSUPPORTED_OPERATION")   # a run takes no new instructions
    assert refused("SendMessage", {"message": {"messageId": "m", "role": "ROLE_USER", "parts": []}}) == (-32602, None)
    assert refused("SendMessage", send("x", {"flow": "campaign_launch"})) == (-32000, "REQUEST_REFUSED")   # a flow it does not rent
    before = rpc(client, "ListTasks", {}, acme)["result"]["totalSize"]
    assert refused("SendMessage", send("x", {"agent": "email_creation"})) == (-32000, "REQUEST_REFUSED")    # an agent it does not rent
    assert rpc(client, "ListTasks", {}, acme)["result"]["totalSize"] == before                                 # and nothing was started
    assert refused("SendMessage", send("x", {"agent": "no_such_agent"}))[0] in (-32000, -32602)
    # a version this endpoint does not speak, in either dialect; an unknown method stays the JSON-RPC error it is
    assert refused("GetTask", {"id": task["id"]}, {**acme, "A2A-Version": "2.0"}) == (-32009, "VERSION_NOT_SUPPORTED")
    old = rpc(client, "tasks/get", {"id": task["id"]}, {**acme, "A2A-Version": "0.2"})["error"]
    assert old["code"] == -32009 and "data" not in old and "0.3" in old["message"]
    assert rpc(client, "tasks/cancel", {"id": task["id"]}, acme)["error"]["code"] == -32601


def test_held_actions_are_decided_by_a_message_to_the_task(client):
    acme = {**customer(client, "acme", ["email_creation"]), **V1}
    sent = rpc(client, "SendMessage", send("Invite homeowners to a flood webinar", {"agent": "email_creation"}), acme)["result"]["task"]
    held = settled(client, sent["id"], acme)
    assert held["status"]["state"] == "TASK_STATE_INPUT_REQUIRED" and held["metadata"]["pendingApprovals"]
    text = held["status"]["message"]["parts"][0]["text"]
    assert text.startswith("Awaiting approval: ") and '{"decisions": {"<step>/<action>": "approve" | "reject"}}' in text
    # only a decision is taken; another customer cannot decide it; a decision is approve or reject
    assert rpc(client, "SendMessage", send("Make it shorter", taskId=held["id"]), acme)["error"]["code"] == -32004
    other = {**customer(client, "globex", ["email_creation"]), **V1}
    assert rpc(client, "SendMessage", send(data={"decisions": {"*": "approve"}}, taskId=held["id"]), other)["error"]["code"] == -32001
    assert rpc(client, "SendMessage", send(data={"decisions": {"*": "maybe"}}, taskId=held["id"]), acme)["error"]["code"] == -32602
    done = rpc(client, "SendMessage", send(data={"decisions": {"*": "approve"}}, taskId=held["id"]), acme)["result"]["task"]
    done = settled(client, done["id"], acme)
    assert done["id"] == held["id"] and done["status"]["state"] == "TASK_STATE_COMPLETED" and done["metadata"]["pendingApprovals"] == []
    run = client.get(f"/runs/{held['id']}", headers=acme).json()
    assert all(a["status"] == "done" for a in run["steps"][0]["result"]["pending_actions"])
    # decided once: the task has ended, and says so
    late = rpc(client, "SendMessage", send("approve", taskId=held["id"]), acme)["error"]
    assert late["code"] == -32004 and "not awaiting approval" in late["message"]
    # one word decides all of them, here to reject
    again = settled(client, rpc(client, "SendMessage", send("Invite homeowners to a flood webinar", {"agent": "email_creation"}), acme)["result"]["task"]["id"], acme)
    over = settled(client, rpc(client, "SendMessage", send("reject", taskId=again["id"]), acme)["result"]["task"]["id"], acme)
    assert over["status"]["state"] == "TASK_STATE_COMPLETED"
    run = client.get(f"/runs/{again['id']}", headers=acme).json()
    assert all(a["status"] == "rejected" for a in run["steps"][0]["result"]["pending_actions"])


def test_a_0_3_client_is_answered_as_before(client):
    acme = customer(client, "acme", ["utm_generation", "email_creation"])
    body = {"message": {"role": "user", "messageId": "m-1", "parts": [
        {"kind": "text", "text": "Tag the links"},
        {"kind": "data", "data": {"agent": "utm_generation", "inputs": {"base_url": "https://www.acme.example/fares", "campaign": "2026q4_us_fares_launch", "channels": ["email"]}}}]}}
    task = rpc(client, "message/send", body, acme)["result"]
    assert task["kind"] == "task" and task["status"]["state"] in ("submitted", "working", "completed")
    for _ in range(120):
        task = rpc(client, "tasks/get", {"id": task["id"]}, acme)["result"]
        if task["status"]["state"] == "completed":
            break
        time.sleep(0.25)
    assert task["status"]["state"] == "completed" and task["status"]["message"]["parts"][0]["kind"] == "text"
    assert task["artifacts"][0]["parts"][0]["kind"] == "data" and "timestamp" not in task["status"] and "metadata" not in task
    unknown = rpc(client, "tasks/get", {"id": "run-nope"}, acme)["error"]
    assert unknown == {"code": -32001, "message": "Unknown task 'run-nope'"}   # no ErrorInfo: that is 1.0's
    # the same door takes its decisions too
    held = rpc(client, "message/send", {"message": {"role": "user", "messageId": "m-2", "parts": [
        {"kind": "text", "text": "Invite homeowners to a flood webinar"}, {"kind": "data", "data": {"agent": "email_creation"}}]}}, acme)["result"]
    for _ in range(120):
        held = rpc(client, "tasks/get", {"id": held["id"]}, acme)["result"]
        if held["status"]["state"] == "input-required":
            break
        time.sleep(0.25)
    done = rpc(client, "message/send", {"message": {"role": "user", "messageId": "m-3", "taskId": held["id"],
                                                    "parts": [{"kind": "data", "data": {"decisions": {"*": "approve"}}}]}}, acme)["result"]
    assert done["kind"] == "task" and done["id"] == held["id"]


def test_the_endpoint_can_be_used_without_the_web_service():
    """handle_jsonrpc is a plain function: the protocol logic holds with any orchestrator that can start and show a run."""
    from caf.api import a2a

    class Runs:
        def __init__(self):
            self.views = {}

        def start(self, goal, **kw):
            if not goal.strip("? "):
                return {"run_id": None, "warnings": ["Say what you want done."], "final": {"next_steps": ["name an agent"]}}
            view = {"run_id": f"run-{len(self.views) + 1}", "status": "running" if kw.get("wait") == 0 else "completed", "steps": [],
                    "pending_approvals": [], "final": {"summary_markdown": f"Did: {goal}"}}
            self.views[view["run_id"]] = view
            return view

        def view(self, run_id):
            return self.views[run_id]

    runs = Runs()
    call = lambda method, params, **kw: a2a.handle_jsonrpc({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, runs, **kw)  # noqa: E731
    assert call("SendMessage", send("Write the brief"))["result"]["task"]["status"]["message"]["parts"] == [{"text": "Did: Write the brief"}]
    assert call("SendMessage", {**send("Write it"), "configuration": {"returnImmediately": True}}, wait=30)["result"]["task"]["status"]["state"] == "TASK_STATE_WORKING"
    assert set(call("SendMessage", send("?"))["result"]) == {"message"}     # nothing ran: a message, not a task
    assert call("message/send", {"message": {"parts": [{"kind": "text", "text": "?"}]}})["result"]["kind"] == "message"
    assert call("GetTask", {"id": "run-1"}, run_owner=lambda rid: False)["error"]["code"] == -32001   # not the caller's: unknown
    assert call("GetExtendedAgentCard", {})["error"]["data"][0]["reason"] == "EXTENDED_AGENT_CARD_NOT_CONFIGURED"
    assert call("GetTask", {"id": "run-1"}, version="1.0.0")["result"]["id"] == "run-1"                 # a patch number is tolerated
    assert a2a.requested_version(None) == a2a.requested_version("") == "0.3" and a2a.requested_version("1.0") == "1.0"
    assert a2a._stamp("2026-11-28T10:15:00+05:30") == "2026-11-28T04:45:00.000Z"                        # UTC, milliseconds, Z


# ------------------------------------------------------------------------------------------------ MCP
MCP_NEW = {"accept": "application/json, text/event-stream", "content-type": "application/json", "mcp-protocol-version": "2026-07-28"}
META = {"io.modelcontextprotocol/protocolVersion": "2026-07-28", "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": {"name": "t", "version": "1"}}


def mcp(client, method: str, params: dict | None = None, headers: dict | None = None, name: str | None = None) -> dict:
    """One request of the 2026-07-28 revision: no handshake and no session, who and what in every request."""
    extra = {"mcp-method": method, **({"mcp-name": name} if name else {})}
    reply = client.post("/mcp/", json={"jsonrpc": "2.0", "id": 3, "method": method, "params": {**(params or {}), "_meta": META}},
                        headers={**MCP_NEW, **extra, **(headers or {})})
    assert reply.status_code == 200, reply.text
    return reply.json()


def test_the_service_says_which_protocol_versions_it_answers(client):
    from importlib.metadata import version

    from caf.health import protocols_line

    assert client.get("/health").json()["protocols"] == {"a2a": ["1.0", "0.3"], "mcp": {"revision": "2026-07-28", "sdk": version("mcp")}}
    assert protocols_line() == "A2A 1.0 + 0.3, MCP 2026-07-28"


def test_every_mcp_tool_says_what_calling_it_does(client):
    listed = mcp(client, "tools/list", headers=ADMIN)["result"]
    tools = {t["name"]: t for t in listed["tools"]}
    names = list(tools)
    assert names[:6] == ["list_agents", "list_flows", "run_flow", "orchestrate", "approve_actions", "get_run"] and names[6:] == sorted(names[6:])
    assert [t["name"] for t in mcp(client, "tools/list", headers=ADMIN)["result"]["tools"]] == names   # the same order every time
    assert all(t.get("title") and t.get("annotations") for t in tools.values())
    reads = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
    assert {k: tools["get_run"]["annotations"][k] for k in reads} == reads and tools["list_agents"]["annotations"]["readOnlyHint"] is True
    assert tools["approve_actions"]["annotations"]["destructiveHint"] is True and tools["approve_actions"]["title"] == "Decide held actions"
    seo, utm = tools["run_technical_seo_recommendations"], tools["run_utm_generation"]
    assert seo["title"] == "Technical SEO Recommendations" and seo["annotations"]["openWorldHint"] is True   # it crawls
    assert utm["annotations"]["openWorldHint"] is False and utm["annotations"]["readOnlyHint"] is False        # a run is metered and kept
    assert not any(t["annotations"]["destructiveHint"] for n, t in tools.items() if n != "approve_actions")   # held until approved
    # a caller's list is its subscription, so it is private to the caller, with how long it stays fresh
    assert listed["cacheScope"] == "private" and listed["ttlMs"] == 60_000 and listed["resultType"] == "complete"
    acme = customer(client, "acme", ["utm_generation"])
    mine = mcp(client, "tools/list", headers=acme)["result"]
    assert [t["name"] for t in mine["tools"]] == ["list_agents", "list_flows", "run_flow", "orchestrate", "approve_actions", "get_run", "run_utm_generation"]
    assert mine["cacheScope"] == "private"
    # no session and no handshake: a call stands on its own
    ran = mcp(client, "tools/call", {"name": "run_utm_generation", "arguments": {"inputs": {
        "base_url": "https://www.acme.example/fares", "campaign": "2026q4_us_fares_launch", "channels": ["email"]}}}, headers=acme, name="run_utm_generation")["result"]
    assert ran["isError"] is False and ran["structuredContent"]["status"] == "ok" and ran["structuredContent"]["run_id"]
    found = mcp(client, "server/discover", headers=acme)["result"]
    assert "2026-07-28" in found["supportedVersions"] and found["capabilities"]["tools"] is not None
