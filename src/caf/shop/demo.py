"""Two demo customers, one per way a customer's own software consumes this layer.

Harbor Bank is *assistant-led*: its analysts work in Claude Desktop and Microsoft Copilot and want the IntelliSight agents as
tools inside those assistants, with a person in the loop. That is MCP. Xperion Airways is *orchestrator-led*: its own
customer-experience orchestrator owns the passenger journey and delegates the specialist work to this layer as tasks it
polls. That is A2A. `caf shop demo` (or POST /shop/admin/api/demo, or the button on the portal's Customers page) creates
both, rents them what their story needs, applies a small policy and issues each a key, shown once. Running it again puts
the two accounts back into their demo state without touching their keys; `--rotate-keys` issues new ones."""

from __future__ import annotations

import json
from typing import Any

from .store import ShopError

DEMO_CUSTOMERS: list[dict[str, Any]] = [
    {
        "id": "harbor-bank", "name": "Harbor Bank", "run_mode": "coforge_cloud", "contact": "digital-analytics@harbor-bank.example",
        "door": "mcp",
        "who": "The digital analytics and marketing team of a retail bank. The analysts already work in Claude Desktop and Microsoft "
               "Copilot; they want the agents as tools inside those assistants, with a person reading every answer.",
        "rents": {"flows": ["is_executive_review", "is_question"], "agents": ["is_analytics_chat", "is_custom_report", "is_recommendations"]},
        "why": "An assistant with a human in the loop is what MCP is for: the analyst asks in plain language, the assistant sees the rented "
               "agents as typed tools (run_flow_is_executive_review takes a dataset; run_is_analytics_chat takes a dataset and a question), "
               "fills them from the export the analyst attached, and shows the computed answer. A held action comes back as a question "
               "to the analyst, who approves it in the same conversation.",
        "walkthrough": [
            "Add the layer to Claude Desktop (or Copilot Studio) as a remote MCP server with the key: the tool list the assistant sees is exactly "
            "this subscription, five IntelliSight tools plus list_agents, list_flows, run_flow, orchestrate, get_run and approve_actions.",
            "Attach an Adobe Analytics Data Warehouse export and ask 'what should we fix first?'. The assistant calls "
            "run_flow_is_executive_review with the file as `dataset`; the engine computes on every row and the Decision Layer ranks the findings.",
            "Ask follow-up questions: run_is_analytics_chat answers each from the same dataset (query plan -> engine -> narrated answer).",
            "Sign in to the portal with the same key: the runs, their cost and this month's usage are on My account.",
        ],
        "policy": {"data": {"mask_before_model": ["email", "phone", "card_number", "iban"]}, "limits": {"runs_per_day": 200},
                   "actions": {"approvers": ["digital-analytics@harbor-bank.example"]}},
    },
    {
        "id": "xperion-airways", "name": "Xperion Airways", "run_mode": "coforge_cloud", "contact": "cx-platform@xperion-airways.example",
        "door": "a2a",
        "who": "An airline whose customer-experience orchestrator (Copilot Studio, Agentforce or its own LangGraph) owns the passenger journey "
               "end to end: it watches the PSS and the loyalty platform, decides when to act, and delegates the specialist work.",
        "rents": {"flows": ["vai_passenger_journey", "vai_churn_prevention", "vai_irops_recovery", "vai_airport_disruption", "is_executive_review"],
                  "agents": ["vai_predictive_propensity"]},
        "why": "Machine to machine with no person waiting is what A2A is for: the airline's orchestrator reads this layer's agent card (its "
               "skills are the rented journeys), sends a task when an event happens, polls it to completion and takes the artifacts "
               "(the offer, the creatives, the activation plan) into its own channels. Task states carry the hold: input-required when an "
               "activation waits for approval.",
        "walkthrough": [
            "Their orchestrator fetches /.well-known/agent-card.json with the key once: six skills, the five rented journeys and the propensity agent.",
            "A passenger is disrupted three times in a month. Their orchestrator sends message/send with a data part "
            "{\"flow\": \"vai_churn_prevention\", \"inputs\": {passenger, signals}} and gets a task back in state working.",
            "It polls tasks/get until completed: eight artifacts, one per stage, the activation a dry run that lists what would be sent and "
            "through which channel, because no airline channel is connected to this layer.",
            "Their orchestrator sends the offer through its own channels, or the airline connects them to this layer as the VoyagerAI pack's home.",
        ],
        "policy": {"data": {"mask_before_model": ["email", "phone"]}, "limits": {"runs_per_day": 1000},
                   "actions": {"approvers": ["cx-platform@xperion-airways.example"]}},
    },
]

DOORS: list[dict[str, str]] = [
    {"id": "portal", "name": "Portal", "for": "A person runs and watches",
     "when": "Someone on the customer's side launches runs by hand, follows them live, approves held actions and reads the results. "
             "Nothing to integrate: the key signs them in at /portal."},
    {"id": "rest", "name": "REST API", "for": "Your systems trigger runs",
     "when": "A scheduler, a data pipeline or an application starts runs and collects results: POST /flows/<id>/run or /orchestrate, then "
             "GET /runs/<id>. The simplest integration when there is no assistant and no orchestrator in between."},
    {"id": "mcp", "name": "MCP", "for": "Your assistant gets the agents as tools",
     "when": "Claude Desktop, Microsoft Copilot or your own agent should be able to pick an agent and fill its inputs from the conversation, "
             "with a person reading the answers. The tool list is your subscription; a held action comes back as a question."},
    {"id": "a2a", "name": "A2A", "for": "Your orchestrator delegates tasks",
     "when": "Your own orchestrator (Copilot Studio, Agentforce, Google ADK, LangGraph) owns the end-to-end process and hands whole tasks "
             "to this layer: it reads the agent card, sends message/send, polls tasks/get, and reads the task state for holds."},
]


def demo_customer(customer_id: str) -> dict[str, Any] | None:
    return next((d for d in DEMO_CUSTOMERS if d["id"] == customer_id), None)


def seed_demo(shop: Any, *, base_url: str = "", rotate_keys: bool = False) -> dict[str, Any]:
    """Create or reset the demo customers. Returns one entry per customer with its story, what it rents, its connection details and,
    when a key was issued in this call (a new account, no live key, or `rotate_keys`), the key itself: it is shown once."""
    out: list[dict[str, Any]] = []
    for d in DEMO_CUSTOMERS:
        try:
            shop.store.get_customer(d["id"])
            created = False
        except ShopError:
            shop.store.add_customer(d["id"], d["name"], run_mode=d["run_mode"], contact=d["contact"], notes=d["who"])
            created = True
        items = [{"kind": "flow", "item_id": f} for f in d["rents"]["flows"]] + [{"kind": "agent", "item_id": a} for a in d["rents"]["agents"]]
        shop.store.set_subscriptions(d["id"], items, replace=True)
        if d.get("policy"):
            shop.set_policy(d["id"], d["policy"])
        live = [k for k in shop.store.list_keys(d["id"]) if not k.get("revoked_at")]
        key = None
        if rotate_keys:
            for k in live:
                shop.store.revoke_key(d["id"], k["id"])
            live = []
        if created or not live:
            key, rec = shop.store.issue_key(d["id"], "demo")
            live = [rec]
        c = shop.store.get_customer(d["id"])
        missing = [x for x in d["rents"]["flows"] if x not in shop.entitled_flows(c)] + \
                  [x for x in d["rents"]["agents"] if x not in shop.entitled_agents(c)]
        out.append({"id": c["id"], "name": c["name"], "created": created, "door": d["door"], "who": d["who"], "why": d["why"],
                    "walkthrough": list(d["walkthrough"]), "contact": d["contact"], "run_mode": c["run_mode"],
                    "rents": {"flows": shop.entitled_flows(c), "agents": sorted(shop.entitled_agents(c))},
                    "not_available": missing,   # a rented pack that is switched off on this server
                    "key": key, "key_prefix": live[0]["prefix"] if live else None,
                    "connection": shop.connection_info(c, base_url) if base_url else None})
    return {"customers": out, "doors": DOORS}


def render_text(result: dict[str, Any]) -> str:
    """The seed result as the presenter reads it: who each customer is, what they rent, how they connect, the key (once)."""
    lines: list[str] = []
    for c in result["customers"]:
        lines += [f"{c['name']}  ({c['id']}, {'created' if c['created'] else 'already there, reset to the demo state'})", "", f"  {c['who']}", "",
                  f"  Rents: {', '.join(c['rents']['flows'])} (flows); {', '.join(c['rents']['agents'])} (agents)"]
        if c["not_available"]:
            lines.append(f"  Not available on this server (pack switched off?): {', '.join(c['not_available'])}")
        lines += ["", f"  Their door: {c['door'].upper()}. {c['why']}", ""]
        lines += [f"  {i + 1}. {step}" for i, step in enumerate(c["walkthrough"])]
        if c["key"]:
            lines += ["", f"  Key (shown once, keep it safe):  {c['key']}"]
        else:
            lines += ["", f"  Key: unchanged (prefix {c['key_prefix']}); `caf shop demo --rotate-keys` issues new ones"]
        conn = c.get("connection") or {}
        if conn:
            key = c["key"] or "<your key>"
            if c["door"] == "mcp":
                cfg = json.loads(json.dumps(conn["mcp"]["client_config"]).replace("<your key>", key))
                lines += ["", "  Claude Desktop / Copilot MCP server:", *("    " + line for line in json.dumps(cfg, indent=2).splitlines())]
            else:
                sample = conn.get("samples", {}).get("a2a_message")
                lines += ["", f"  Agent card:  curl -H 'Authorization: Bearer {key}' {conn['a2a']['agent_card']}"]
                if sample:
                    lines += [f"  Delegate a task:  curl -H 'Authorization: Bearer {key}' -H 'Content-Type: application/json' {conn['a2a']['endpoint']} -d '<the message below>'",
                              *("    " + line for line in json.dumps(sample, indent=2).splitlines())]
        lines += ["", ""]
    lines += ["Which door a customer opts for:"] + [f"  {d['name']:<9} {d['for']}: {d['when']}" for d in result["doors"]]
    return "\n".join(lines)
