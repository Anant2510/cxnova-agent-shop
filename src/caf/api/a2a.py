"""A2A (Agent2Agent protocol) surface: agent cards for the orchestrator and every agent, plus the JSON-RPC endpoint
through which external orchestrators (Copilot Studio, Agentforce, Gemini Enterprise, Bedrock AgentCore, LangGraph,
Adobe's agent orchestrator ...) delegate goals to this layer.

One endpoint speaks two versions of the protocol, the way the protocol's own SDKs do:

* **1.0**, the first stable specification: `SendMessage`, `GetTask`, `ListTasks`, `CancelTask`, `GetExtendedAgentCard`.
  A state is `TASK_STATE_COMPLETED`, a role `ROLE_AGENT`, a part is told by the member it carries (`text`, `data`), the
  answer to SendMessage is `{"task": ...}` or `{"message": ...}`, and an error says what went wrong as a
  `google.rpc.ErrorInfo`.
* **0.3**: `message/send` and `tasks/get`, with `kind` discriminators and lower-case states, exactly as before.

A request is read by its method name (the PascalCase names are 1.0). The `A2A-Version` header, when a client sends it,
has to name a version this endpoint speaks; a client that sends none is a 0.3 client (specification 3.6.2).

A task is a run. A run that holds actions for approval is `input-required`: the caller decides them by sending a
message to that task with a data part `{"decisions": {"<step>/<action>": "approve" | "reject"}}` (`"*"` for all of them).
What is not offered is said in the card and refused by name: no streaming and no push notifications (a caller polls
`GetTask`), and a run in flight cannot be cancelled.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from ..core import all_agents, get_agent

V1, V03 = "1.0", "0.3"
VERSIONS = (V1, V03)
PROTOCOL_VERSION = "0.3.0"       # what a 0.3 client reads at the top of the card
BINDING = "JSONRPC"
ERROR_INFO = "type.googleapis.com/google.rpc.ErrorInfo"
A2A_DOMAIN = "a2a-protocol.org"
OWN_DOMAIN = "cxnova-orchestration"

STREAMING = ("SendStreamingMessage", "SubscribeToTask")
PUSH = ("CreateTaskPushNotificationConfig", "GetTaskPushNotificationConfig", "ListTaskPushNotificationConfigs",
        "DeleteTaskPushNotificationConfig")
V1_METHODS = ("SendMessage", "GetTask", "ListTasks", "CancelTask", "GetExtendedAgentCard", *STREAMING, *PUSH)
V03_METHODS = ("message/send", "tasks/get")

# what can go wrong, by kind: (JSON-RPC code, the reason a 1.0 client reads, whose reason it is). Specification 5.4.
ERRORS: dict[str, tuple[int, str, str]] = {
    "method": (-32601, "", ""),
    "invalid": (-32602, "", ""),
    "refused": (-32000, "REQUEST_REFUSED", OWN_DOMAIN),
    "task_not_found": (-32001, "TASK_NOT_FOUND", A2A_DOMAIN),
    "not_cancelable": (-32002, "TASK_NOT_CANCELABLE", A2A_DOMAIN),
    "push": (-32003, "PUSH_NOTIFICATION_NOT_SUPPORTED", A2A_DOMAIN),
    "unsupported": (-32004, "UNSUPPORTED_OPERATION", A2A_DOMAIN),
    "no_extended_card": (-32007, "EXTENDED_AGENT_CARD_NOT_CONFIGURED", A2A_DOMAIN),
    "version": (-32009, "VERSION_NOT_SUPPORTED", A2A_DOMAIN),
}
STATES = {"completed": "completed", "completed_with_errors": "completed", "failed": "failed",
          "awaiting_approval": "input-required", "queued": "submitted", "running": "working", "stalled": "unknown"}
SETTLED = ("completed", "completed_with_errors", "failed")
APPROVE_WORDS = ("approve", "approve all", "approved", "yes")
REJECT_WORDS = ("reject", "reject all", "rejected", "no")
HOW_TO_DECIDE = ('Decide them by sending a message to this task with a data part {"decisions": {"<step>/<action>": "approve" | "reject"}} '
                 '("*" decides all of them).')


class A2AError(Exception):
    """A request this endpoint refuses: its kind (a key of ERRORS), a sentence for the caller, and facts about it."""

    def __init__(self, kind: str, message: str, **facts: Any):
        super().__init__(message)
        self.kind, self.message, self.facts = kind, message, facts


# ---------------------------------------------------------------------------------------------- versions
def requested_version(value: str | None) -> str:
    """The protocol version a request names in `A2A-Version` (header or query parameter). None or empty: a 0.3 client."""
    text = (value or "").strip()
    if not text:
        return V03
    wanted = ".".join(text.split(".")[:2])
    if wanted not in VERSIONS:
        raise A2AError("version", f"A2A version '{text}' is not supported; this endpoint speaks {' and '.join(VERSIONS)}",
                       requested=text, supported=", ".join(VERSIONS))
    return wanted


def speaks_v1(method: Any) -> bool:
    return method in V1_METHODS


# ---------------------------------------------------------------------------------------------- cards
def agent_skill(agent: Any) -> dict[str, Any]:
    s = agent.spec
    return {"id": s.id, "name": s.name, "description": s.summary, "tags": [s.category.value, s.kind.value, *s.keywords[:6]],
            "examples": s.use_cases[:3], "inputModes": ["application/json", "text/plain"], "outputModes": ["application/json"]}


def flow_skill(flow: dict[str, Any]) -> dict[str, Any]:
    """A rented orchestration flow as an A2A skill: send a data part {"flow": "<id>", "inputs": {...}} to run it."""
    inputs = ", ".join(i["name"] for i in flow.get("inputs", [])) or "url, audience ..."
    return {"id": f"flow:{flow['id']}", "name": flow["name"], "description": flow.get("description") or flow["name"],
            "tags": ["flow", *flow.get("agents", [])[:8]],
            "examples": [f'data part: {{"flow": "{flow["id"]}", "inputs": {{{inputs}}}}}'],
            "inputModes": ["application/json", "text/plain"], "outputModes": ["application/json"]}


def _security(version: str) -> dict[str, Any]:
    """How a caller proves who it is: its key as a bearer token or in X-API-Key. Each version of the protocol writes it its own way."""
    note = "Your customer key (cxn_...), or the operator token"
    if version == V1:
        return {"securitySchemes": {"bearer": {"httpAuthSecurityScheme": {"scheme": "Bearer", "description": note}},
                                    "apiKey": {"apiKeySecurityScheme": {"location": "header", "name": "X-API-Key", "description": note}}},
                "securityRequirements": [{"schemes": {"bearer": {"list": []}}}, {"schemes": {"apiKey": {"list": []}}}]}
    return {"securitySchemes": {"bearer": {"type": "http", "scheme": "bearer", "description": note},
                                "apiKey": {"type": "apiKey", "in": "header", "name": "X-API-Key", "description": note}},
            "security": [{"bearer": []}, {"apiKey": []}]}


def orchestrator_card(base_url: str, allowed: set[str] | None = None, flows: list[dict[str, Any]] | None = None, *,
                      version: str = V03, secured: bool = False, tenant: str | None = None, endpoint: str | None = None) -> dict[str, Any]:
    """The card of this layer's orchestrator. `version` is the one the reader asked for: a 1.0 reader gets the 1.0 card;
    anyone else gets the same card with the fields a 0.3 client reads beside it (`url`, `protocolVersion`,
    `preferredTransport`). `secured`: the service asks for a key, so the card says how to present one and an
    authenticated caller can ask for its own card. `tenant`: the customer the card was made for."""
    from ..branding import PRODUCT

    agents = [a for a in all_agents() if allowed is None or a.spec.id in allowed]
    flows = flows or []
    url = endpoint or f"{base_url}/a2a"
    interfaces = [{"url": url, "protocolBinding": BINDING, "protocolVersion": v, **({"tenant": tenant} if tenant else {})} for v in VERSIONS]
    card: dict[str, Any] = {
        "name": f"{PRODUCT.name} Orchestrator",
        "description": f"Plans and runs multi-agent marketing workflows across {len(agents)} vendor-neutral agents "
                       f"(strategy, content, website optimisation, experimentation)"
                       + (f" and {len(flows)} ready-made orchestration flows." if flows else "."),
        "supportedInterfaces": interfaces,
        "version": __import__("caf").__version__,
        "provider": {"organization": PRODUCT.company, "url": "https://www.coforge.com"},
        "documentationUrl": f"{base_url}/docs",
        "capabilities": {"streaming": False, "pushNotifications": False, "extendedAgentCard": bool(secured)},
        "defaultInputModes": ["text/plain", "application/json"],
        "defaultOutputModes": ["application/json", "text/markdown"],
        "skills": [agent_skill(a) for a in agents] + [flow_skill(f) for f in flows],
    }
    if secured:
        card.update(_security(version))
    if version != V1:
        card.update(protocolVersion=PROTOCOL_VERSION, url=url, preferredTransport=BINDING)
        card["capabilities"]["stateTransitionHistory"] = True
    return card


def single_agent_card(agent_id: str, base_url: str, **kwargs: Any) -> dict[str, Any]:
    agent = get_agent(agent_id)
    card = orchestrator_card(base_url, endpoint=f"{base_url}/a2a/agents/{agent_id}", **kwargs)
    card.update(name=agent.spec.name, description=agent.spec.summary, skills=[agent_skill(agent)])
    return card


# ---------------------------------------------------------------------------------------------- reading a request
def _text_of(message: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """The text parts joined and the data parts merged. A 1.0 part is told by its member, a 0.3 part by `kind`: both
    carry the text in `text` and the data in `data`, so one reading serves both."""
    text, data = [], {}
    for part in message.get("parts") or []:
        if not isinstance(part, dict):
            continue
        kind = part.get("kind") or part.get("type")
        if isinstance(part.get("text"), str) and kind in (None, "text"):
            text.append(part["text"])
        elif isinstance(part.get("data"), dict) and kind in (None, "data"):
            data.update(part["data"])
    return "\n".join(t for t in text if t).strip(), data


def _decisions(text: str, data: dict[str, Any]) -> dict[str, str] | None:
    """What a message to a waiting task decides: a `decisions` data part, or the one word approve / reject for all of them."""
    given = data.get("decisions")
    if isinstance(given, dict) and given:
        if not all(v in ("approve", "reject") for v in given.values()):
            raise A2AError("invalid", 'Each decision is "approve" or "reject"')
        return {str(k): str(v) for k, v in given.items()}
    word = text.strip().lower().rstrip(".!")
    if word in APPROVE_WORDS:
        return {"*": "approve"}
    if word in REJECT_WORDS:
        return {"*": "reject"}
    return None


def _refusal(exc: Exception) -> A2AError:
    """A refusal by the shop (not rented, over a limit, unknown flow) in this endpoint's terms."""
    status = getattr(exc, "status", 400)
    return A2AError("not_found" if status == 404 else "refused", str(exc), status=status)


def _view_of(orchestrator: Any, run_owner: Any, task_id: Any) -> dict[str, Any]:
    """The caller's run. A run that is not the caller's is unknown, in the same words: an id alone opens nothing."""
    try:
        if not isinstance(task_id, str) or (run_owner is not None and not run_owner(task_id)):
            raise KeyError(task_id)
        return orchestrator.view(task_id)
    except KeyError:
        raise A2AError("task_not_found", f"Unknown task '{task_id}'", taskId=str(task_id)) from None


# ---------------------------------------------------------------------------------------------- answering
def _wire(when: datetime) -> str:
    """A time as the protocol writes it: UTC, milliseconds, Z."""
    return when.strftime("%Y-%m-%dT%H:%M:%S.") + f"{when.microsecond // 1000:03d}Z"


def _now() -> str:
    return _wire(datetime.now(timezone.utc))


def _read_time(value: Any) -> datetime | None:
    """An ISO time (a run's, or one a caller sent) in UTC, or None when it cannot be read."""
    try:
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return when.astimezone(timezone.utc) if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _stamp(value: Any) -> str:
    """A run's time as the protocol writes it. One that cannot be read is now."""
    when = _read_time(value)
    return _wire(when) if when else _now()


def _updated(orchestrator: Any, run_id: str) -> Any:
    """When the run's status was last recorded, if this orchestrator keeps an index of its runs."""
    try:
        return (orchestrator.store.get(run_id) or {}).get("updated_at")
    except Exception:  # noqa: BLE001 - a stand-in orchestrator has no store: the answer carries the time it was given
        return None


def _state(status: str, v1: bool) -> str:
    state = STATES.get(status, "working")
    if not v1:
        return state
    return "TASK_STATE_UNSPECIFIED" if state == "unknown" else "TASK_STATE_" + state.upper().replace("-", "_")


def _message(text: str, v1: bool) -> dict[str, Any]:
    if v1:
        return {"messageId": uuid.uuid4().hex, "role": "ROLE_AGENT", "parts": [{"text": text}]}
    return {"kind": "message", "role": "agent", "messageId": uuid.uuid4().hex, "parts": [{"kind": "text", "text": text}]}


def _task(view: dict[str, Any], v1: bool = False, *, artifacts: bool = True, when: Any = None) -> dict[str, Any]:
    held = view.get("pending_approvals") or []
    final = view.get("final") or {}
    waiting = "Awaiting approval: " + ", ".join(str(p.get("summary") or p.get("key")) for p in held) if held else ""
    text = final.get("summary_markdown") or waiting or view.get("next") or view["status"]
    made = []
    for step in view.get("steps") or []:
        r = step.get("result") or {}
        if r and artifacts:
            data = {"status": r.get("status"), "summary": r.get("summary"), "output": r.get("output")}
            part = {"data": data, "mediaType": "application/json"} if v1 else {"kind": "data", "data": data}
            made.append({"artifactId": f"{view['run_id']}-{step['id']}", "name": f"{step['id']} ({step['agent']})", "parts": [part]})
    if not v1:
        return {"kind": "task", "id": view["run_id"], "contextId": view["run_id"],
                "status": {"state": _state(view["status"], False), "message": _message(text, False)}, "artifacts": made}
    if waiting and not final.get("summary_markdown"):
        text = f"{waiting}. {HOW_TO_DECIDE}"
    task: dict[str, Any] = {"id": view["run_id"], "contextId": view["run_id"],
                            "status": {"state": _state(view["status"], True), "message": _message(text, True),
                                       "timestamp": _stamp(when) if when else _now()},
                            "metadata": {"runStatus": view["status"],
                                         "pendingApprovals": [{"key": p.get("key"), "summary": p.get("summary")} for p in held]}}
    if made:
        task["artifacts"] = made
    return task


def error_reply(body: Any, kind: str, message: str, **facts: Any) -> dict[str, Any]:
    """A JSON-RPC error in the dialect the request was written in. A 1.0 client also gets the reason as an ErrorInfo."""
    body = body if isinstance(body, dict) else {}
    v1 = speaks_v1(body.get("method"))
    if kind == "not_found":   # an unknown flow: a 0.3 client was told -32001; in 1.0 that code means an unknown task
        kind = "invalid" if v1 else "task_not_found"
    code, reason, domain = ERRORS.get(kind, ERRORS["refused"])
    error: dict[str, Any] = {"code": code, "message": message}
    if v1 and reason:
        error["data"] = [{"@type": ERROR_INFO, "reason": reason, "domain": domain,
                          "metadata": {k: str(v) for k, v in facts.items() if v is not None}}]
    return {"jsonrpc": "2.0", "id": body.get("id"), "error": error}


# ---------------------------------------------------------------------------------------------- the endpoint
def handle_jsonrpc(body: dict[str, Any], orchestrator: Any, agent_id: str | None = None, *, ctx: Any = None,
                   run_owner: Any = None, run_flow: Any = None, wait: float | None = None, on_settled: Any = None,
                   version: str | None = None, approve: Any = None, list_runs: Any = None, card: Any = None,
                   tenant: str | None = None, check_agent: Any = None) -> dict[str, Any]:
    """One JSON-RPC request, in either version of the protocol.

    `ctx`: a prepared RunContext (a shop customer's) or a callable making one; `run_owner(run_id) -> bool` limits a
    caller to its own runs; `run_flow(flow_id, inputs, goal, wait) -> view` runs a rented flow named in a data part
    ({"flow": "<id>"} or {"playbook": "<id>"}). `wait`: seconds a send waits for the run before answering with the task
    as it stands (the client follows it with GetTask / tasks/get); `on_settled(view)` meters the run when it settles.
    `version`: the request's A2A-Version header. `approve(run_id, decisions, wait) -> view` decides held actions for
    the caller; `list_runs(limit, agent) -> rows` lists the caller's runs; `card() -> dict` is the caller's own card;
    `tenant`: the customer the caller is, which a request's `tenant` has to match when it names one;
    `check_agent(agent_id)` raises when the caller may not run the agent a 1.0 message names (nothing is started)."""
    try:
        requested_version(version)
        return _dispatch(body, orchestrator, agent_id, ctx=ctx, run_owner=run_owner, run_flow=run_flow, wait=wait,
                         on_settled=on_settled, approve=approve, list_runs=list_runs, card=card, tenant=tenant,
                         check_agent=check_agent)
    except A2AError as exc:
        return error_reply(body, exc.kind, exc.message, **exc.facts)


def _dispatch(body: dict[str, Any], orchestrator: Any, agent_id: str | None, *, ctx: Any, run_owner: Any, run_flow: Any,
              wait: float | None, on_settled: Any, approve: Any, list_runs: Any, card: Any, tenant: str | None,
              check_agent: Any = None) -> dict[str, Any]:
    rid, method = body.get("id"), body.get("method")
    v1 = speaks_v1(method)
    params = body.get("params") or {}
    if not isinstance(params, dict):
        raise A2AError("invalid", "params must be an object")
    if not v1 and method not in V03_METHODS:
        raise A2AError("method", f"Method not found: {method}")
    if method in STREAMING:
        raise A2AError("unsupported", "Streaming is not offered (the card says so): send the message with SendMessage and follow the task with GetTask")
    if method in PUSH:
        raise A2AError("push", "Push notifications are not offered (the card says so): follow the task with GetTask")
    if v1 and tenant and params.get("tenant") not in (None, "", tenant):
        raise A2AError("invalid", "tenant does not match the one in your agent card")

    def answer(result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    if method in ("GetTask", "tasks/get"):
        view = _view_of(orchestrator, run_owner, params.get("id"))
        return answer(_task(view, v1, when=_updated(orchestrator, view["run_id"])))
    if method == "CancelTask":
        view = _view_of(orchestrator, run_owner, params.get("id"))
        if view["status"] in SETTLED:
            raise A2AError("not_cancelable", f"Task '{view['run_id']}' has already ended ({view['status']})", taskId=view["run_id"])
        raise A2AError("unsupported", "A run in flight cannot be cancelled. A run that waits for approval ends when its held actions are "
                                      "rejected: " + HOW_TO_DECIDE, taskId=view["run_id"])
    if method == "ListTasks":
        return answer(_list(params, orchestrator, agent_id, list_runs))
    if method == "GetExtendedAgentCard":
        if card is None:
            raise A2AError("no_extended_card", "There is no card beyond the public one for this caller")
        return answer(card())

    # ---- SendMessage / message/send
    message = params.get("message") or {}
    if not isinstance(message, dict):
        raise A2AError("invalid", "message must be an object")
    config = params.get("configuration") or {}
    if v1 and config.get("taskPushNotificationConfig"):
        raise A2AError("push", "Push notifications are not offered (the card says so): follow the task with GetTask")
    if v1 and config.get("returnImmediately"):
        wait = 0
    goal, data = _text_of(message)
    if message.get("taskId"):
        view = _reply(message["taskId"], goal, data, orchestrator, run_owner, approve, wait, on_settled)
        return answer({"task": _task(view, True)} if v1 else _task(view))
    if not goal and not data:
        raise A2AError("invalid", "message needs a text or data part")
    flow_id = data.get("flow") or data.get("playbook")
    if flow_id and run_flow is not None and not agent_id:
        inputs = data.get("inputs", {k: v for k, v in data.items() if k not in ("flow", "playbook", "goal")})
        try:
            view = run_flow(flow_id, inputs, goal or data.get("goal"), wait)
        except A2AError:
            raise
        except Exception as exc:
            raise _refusal(exc) from exc
    else:
        named = agent_id or data.get("agent")
        if v1 and named and check_agent is not None:
            # an agent named outright that the caller may not run is refused before anything starts. (A 0.3 client keeps
            # the answer it always had: a task that failed with the step skipped.)
            try:
                check_agent(named)
            except Exception as exc:
                raise _refusal(exc) from exc
        try:
            if callable(ctx):
                ctx = ctx()
            view = orchestrator.start(goal or data.get("goal", ""), inputs=data.get("inputs", data), agent=named,
                                      playbook=flow_id, ctx=ctx, wait=wait, on_settled=on_settled)
        except (KeyError, ValueError) as exc:   # an agent or flow this layer does not have
            raise A2AError("invalid", str(exc.args[0]) if exc.args else str(exc)) from exc
        except Exception as exc:
            if hasattr(exc, "status"):          # the shop's refusal: not rented, over a limit
                raise _refusal(exc) from exc
            raise
    if view.get("run_id") is None:  # needs_clarification: nothing ran, answer with a message (no task)
        tips = "; ".join((view.get("final") or {}).get("next_steps", []))
        said = _message(" ".join(view.get("warnings") or []) + (f" {tips}." if tips else ""), v1)
        return answer({"message": said} if v1 else said)
    return answer({"task": _task(view, True)} if v1 else _task(view))


def _reply(task_id: Any, text: str, data: dict[str, Any], orchestrator: Any, run_owner: Any, approve: Any,
           wait: float | None, on_settled: Any) -> dict[str, Any]:
    """A message sent to a task that exists. The one thing a run takes once it has started is a decision on what it holds."""
    from ..orchestrator.service import RunStateError

    view = _view_of(orchestrator, run_owner, task_id)
    decisions = _decisions(text, data)
    if decisions is None:
        raise A2AError("unsupported", "A run takes no new instructions once it has started: send a new message without taskId to "
                                      "start another. " + (HOW_TO_DECIDE if view.get("pending_approvals") else ""), taskId=view["run_id"])
    try:
        if approve is not None:
            return approve(view["run_id"], decisions, wait)
        return orchestrator.resume(view["run_id"], decisions, wait=wait, actor="a2a", on_settled=on_settled)
    except KeyError:
        raise A2AError("task_not_found", f"Unknown task '{task_id}'", taskId=str(task_id)) from None
    except RunStateError as exc:
        raise A2AError("unsupported", str(exc), taskId=view["run_id"]) from exc


def _list(params: dict[str, Any], orchestrator: Any, agent_id: str | None, list_runs: Any) -> dict[str, Any]:
    """ListTasks: the caller's runs, newest first, a page at a time."""
    wanted = params.get("status")
    statuses = None
    if wanted:
        statuses = [s for s in STATES if _state(s, True) == wanted]
        if not statuses and wanted not in ("TASK_STATE_CANCELED", "TASK_STATE_REJECTED", "TASK_STATE_AUTH_REQUIRED"):
            raise A2AError("invalid", f"status '{wanted}' is not a task state")
    try:
        size = int(params.get("pageSize") or 50)
        start = int(params.get("pageToken") or 0)
    except (TypeError, ValueError):
        raise A2AError("invalid", "pageSize is a number and pageToken is the nextPageToken of an earlier answer") from None
    if not 1 <= size <= 100 or start < 0:
        raise A2AError("invalid", "pageSize is between 1 and 100")
    after = None
    if params.get("statusTimestampAfter"):
        after = _read_time(params["statusTimestampAfter"])
        if after is None:
            raise A2AError("invalid", "statusTimestampAfter is an ISO 8601 time, for example 2026-11-28T09:30:00.000Z")
    rows = (list_runs or (lambda limit, agent: orchestrator.list_runs(limit=limit, agent=agent)))(1000, agent_id)
    kept = []
    for row in rows:
        if statuses is not None and row.get("status") not in statuses:
            continue
        if params.get("contextId") and row.get("run_id") != params["contextId"]:
            continue
        if after is not None and (_read_time(row.get("updated_at")) or after) < after:
            continue
        kept.append(row)
    tasks = []
    for row in kept[start:start + size]:
        if params.get("includeArtifacts"):
            try:
                tasks.append(_task(orchestrator.view(row["run_id"]), True, when=row.get("updated_at")))
                continue
            except KeyError:
                pass
        tasks.append(_task({"run_id": row["run_id"], "status": row.get("status") or "running", "next": row.get("goal") or row.get("status")},
                           True, artifacts=False, when=row.get("updated_at")))
    more = start + size < len(kept)
    return {"tasks": tasks, "nextPageToken": str(start + size) if more else "", "pageSize": size, "totalSize": len(kept)}
