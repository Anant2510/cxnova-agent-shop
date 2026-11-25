"""The case orchestrator: the stages of an assessment, the agent that owns each one, and the record of every run.

    intake -> classify -> extract -> privacy -> resolve -> screening -> graph -> evidence -> reference -> policy
           -> consistency -> integrity -> verify -> triage

`assess()` runs them in order. `step()` runs one (and whatever it still depends on), which is how a host that
orchestrates the agents itself (the CX Nova layer: one flow step per agent) drives the same code. Every stage leaves an
agent-run record: what it did, how long it took, the model and prompt version it used if any, its tool calls, and what
it read from and wrote to the graph.
"""
from __future__ import annotations

import time
from datetime import date
from typing import Any, Callable

from . import assurance, checks, contracts as C, graphs, harness as H, reader, understand
from .casefile import current, event, finding, live_docs, now
from .model import PROMPT_VERSION, OfflineModel
from .profiles import Profile

STAGES = ["intake", "classify", "extract", "privacy", "resolve", "screening", "graph", "evidence", "reference", "policy", "consistency", "integrity", "verify", "triage"]

AGENTS: list[dict[str, Any]] = [
    {"id": "orchestrator", "name": "Case Orchestrator", "stage": "", "kind": "code", "responsibility": "Plans the assessment, runs the stages in dependency order, retries and holds the human checkpoints.", "output": "Execution plan and state"},
    {"id": "intake", "name": "Intake Agent", "stage": "intake", "kind": "code + model (scans)", "responsibility": "Registers the case, checks each file, keeps immutable document versions with their hashes and reads them into text.", "output": "Case and evidence package"},
    {"id": "classify", "name": "Classification Agent", "stage": "classify", "kind": "model, verified", "responsibility": "Names each document against the profile's document types, with a confidence, and sets it beside the uploader's category.", "output": "Document type and confidence"},
    {"id": "extract", "name": "Extraction Agent", "stage": "extract", "kind": "model, verified", "responsibility": "Lifts the declared fields with a source anchor (page, line, the line as printed) and checks each value against the text.", "output": "Evidence assertions"},
    {"id": "privacy", "name": "Privacy Agent", "stage": "privacy", "kind": "code", "responsibility": "Finds the personal data in the package, by document and by kind, so that it is masked in whatever leaves the system.", "output": "Personal data report and masks"},
    {"id": "resolve", "name": "Entity Resolution Agent", "stage": "resolve", "kind": "code", "responsibility": "Links repeated parties, references and goods across documents; leaves near-matches for a person.", "output": "Canonical entities and links"},
    {"id": "screening", "name": "Screening Agent", "stage": "screening", "kind": "code + watchlists", "responsibility": "Compares every party, vessel and place with the watchlists and leaves each possible match to a person.", "output": "Possible matches"},
    {"id": "graph", "name": "Knowledge Graph Agent", "stage": "graph", "kind": "code", "responsibility": "Writes the case subgraph: documents, versions, assertions, entities and the provenance edges between them.", "output": "Graph update"},
    {"id": "evidence", "name": "Evidence Completeness Agent", "stage": "evidence", "kind": "code", "responsibility": "Checks mandatory, conditional and alternative evidence and explains how an absence was established.", "output": "Missing and uncertain evidence"},
    {"id": "reference", "name": "Reference Agent", "stage": "reference", "kind": "code + connector", "responsibility": "Retrieves date-relevant reference content with its provenance and reasons over it from stated product characteristics.", "output": "Candidate classifications"},
    {"id": "policy", "name": "Policy Agent", "stage": "policy", "kind": "code", "responsibility": "Executes the approved deterministic rules: timing, value reconciliation, code consistency.", "output": "Rule evaluations"},
    {"id": "contradiction", "name": "Contradiction Agent", "stage": "consistency", "kind": "code", "responsibility": "Compares parties, quantities, weights, references and descriptions across documents.", "output": "Corroborations and discrepancies"},
    {"id": "integrity", "name": "Integrity Agent", "stage": "integrity", "kind": "code + memory", "responsibility": "Checks that each document holds together on its own: sums, dates, check digits, the same file twice, details that changed since last time.", "output": "Integrity checks"},
    {"id": "verification", "name": "Verification Agent", "stage": "verify", "kind": "code", "responsibility": "Checks source coverage per finding, blocks unsupported conclusions and aggregates the recommendation.", "output": "Verified findings and recommendation"},
    {"id": "triage", "name": "Triage Agent", "stage": "triage", "kind": "code", "responsibility": "Says how urgent the case is, which queue picks it up and when it is due against the service target.", "output": "Priority, queue and due date"},
    {"id": "counterfactual", "name": "Counterfactual Agent", "stage": "", "kind": "code", "responsibility": "Changes a fact on a copy of the case, reruns the dependent checks and explains what moved.", "output": "Impact explanation"},
    {"id": "correspondence", "name": "Correspondence Agent", "stage": "", "kind": "template or model, verified", "responsibility": "Drafts an evidence request or a decision note from the findings, for a person to edit.", "output": "Editable draft"},
    {"id": "feedback", "name": "Feedback Agent", "stage": "", "kind": "code", "responsibility": "Turns validated corrections into governed improvement proposals with a regression check.", "output": "Improvement proposal"},
    {"id": "assistant", "name": "Case Assistant", "stage": "", "kind": "model, grounded", "responsibility": "Answers questions from the case graph and the documents, cites its sources and declines what they do not hold.", "output": "Grounded answer"},
    {"id": "repository", "name": "Repository Agent", "stage": "", "kind": "connector", "responsibility": "Files the assessed case and its metadata to the content repository, or pulls a case package from it.", "output": "Filing record"},
]
AGENT_OF = {a["stage"]: a for a in AGENTS if a["stage"]}


class Tools:
    """What a stage may use. A host fills in its own model, its own way of reading a stored file, and its own HTTP for references.
    The foundation is optional and passed the same way: the rate limiter the model calls share, the provenance ledger, the workspace's
    episodic memory, and what to do when an assessment ends (the service writes what the case taught the workspace)."""

    def __init__(self, *, model: Any = None, read_bytes: Callable[[dict[str, Any], dict[str, Any]], bytes] | None = None, overlay: dict[str, Any] | None = None,
                 fetch: Any = None, progress: Callable[[dict[str, Any]], None] | None = None, sidecar: Callable[[dict[str, Any], dict[str, Any]], str | None] | None = None,
                 limiter: Any = None, ledger: Any = None, episodes: Any = None, on_finish: Callable[[dict[str, Any]], None] | None = None,
                 ground: Callable[..., dict[str, Any] | None] | None = None, today: Callable[[], date] | None = None,
                 watchlists: Callable[[], list[dict[str, Any]]] | None = None) -> None:
        self.model = model or OfflineModel()
        self.read_bytes = read_bytes
        self.overlay = overlay or {}
        self.fetch = fetch
        self.progress = progress
        self.sidecar = sidecar
        self.limiter = limiter
        self.ledger = ledger
        self.episodes = episodes
        self.on_finish = on_finish
        self.ground = ground      # the host's knowledge graph: the nodes an agent's contract says it is handed before it starts
        self.today = today or date.today          # the day triage and the date checks count from (a test sets its own)
        self.watchlists = watchlists              # the lists the screening agent reads; none given: the installation's, else the demonstration list
        self.memory: Any = None   # the memory handle of the agent that is running; set by the harness
        self.grounding: Any = None   # what the harness handed the agent that is running


def begin(case: dict[str, Any], *, trigger: str = "assessment", by: str = "system") -> dict[str, Any]:
    n = len(case.get("history") or []) + (1 if case.get("assessment") else 0) + 1
    case["working"] = {"id": f"A{n}", "at": now(), "trigger": trigger, "by": by, "done": [], "stages": {}, "runs": [], "findings": [], "entities": [], "unresolved": [],
                       "graph": {"nodes": [], "edges": []}, "references": [], "memory": [], "recall": None, "privacy": None, "screening": None, "triage": None}
    case["progress"] = {"state": "running", "stage": "", "done": []}
    return case["working"]


def _intake(case: dict[str, Any], profile: Profile, tools: Tools, w: dict[str, Any]) -> dict[str, Any]:
    rows = []
    for d in live_docs(case):
        ver = current(d)
        if d.get("assumed"):
            rows.append({"doc": d["id"], "name": d["name"], "version": d["current"], "method": "assumed", "pages": 0, "chars": 0, "checks": []})
            continue
        if "text" not in ver:
            data = tools.read_bytes(d, ver) if tools.read_bytes else b""
            ver["checks"] = reader.check_file(d["name"], data)
            got = reader.read(d["name"], data, model=tools.model, sidecar=tools.sidecar(d, ver) if tools.sidecar else None)
            ver.update(pages=got["pages"], text=got["text"], method=got["method"], note=got["note"])
        rows.append({"doc": d["id"], "name": d["name"], "version": d["current"], "sha256": ver.get("sha256", "")[:16], "method": ver.get("method"), "pages": len(ver.get("pages") or []),
                     "chars": len(ver.get("text") or ""), "checks": ver.get("checks") or [], "note": ver.get("note") or ""})
    unread = [r for r in rows if r["method"] == "none"]
    failed = [r for r in rows if any(c["status"] == "fail" for c in r["checks"])]
    return {"documents": rows, "unread": len(unread), "failed_checks": len(failed),
            "summary": f"{len(rows)} document(s) in the package, {sum(r['pages'] for r in rows)} page(s) read; {len(unread)} unreadable, {len(failed)} failing a file check"}


def _run_stage(stage: str, case: dict[str, Any], profile: Profile, tools: Tools, w: dict[str, Any]) -> dict[str, Any]:
    if stage == "intake":
        return _intake(case, profile, tools, w)
    if stage == "classify":
        return understand.classify(case, profile, tools.model, tools.overlay)
    if stage == "extract":
        return understand.extract(case, profile, tools.model)
    if stage == "resolve":
        out = graphs.resolve(case, profile, tools.overlay)
        w["entities"], w["unresolved"] = out["entities"], out["unresolved"]
        recall = tools.memory.recall(case, out["entities"]) if tools.memory is not None else None   # what the workspace already knows that touches this case
        w["recall"] = recall
        known = checks.known(case, profile, recall) if recall else []
        w["findings"] = [f for f in w["findings"] if f["agent"] != "resolve"] + known
        met = len((recall or {}).get("parties") or []) + len((recall or {}).get("identifiers") or [])
        return {"entities": len(out["entities"]), "unresolved": len(out["unresolved"]), "known": met,
                "summary": out["summary"] + (f"; {met} already known to the workspace" if met else "")}
    if stage in ("privacy", "screening", "triage"):   # these three leave the case graph alone
        if stage == "privacy":
            out = assurance.privacy(case, profile)
            w["privacy"] = out.pop("privacy")
        elif stage == "screening":
            out = assurance.screening(case, profile, w["entities"], tools.watchlists() if tools.watchlists else None)
            w["screening"] = out.pop("screening")
        else:
            w["triage"] = assurance.triage(case, profile, w["findings"], w["recommendation"], today=tools.today())
            t = w["triage"]
            return {"triage": t, "summary": f"Priority {t['priority']}, queue '{t['queue_name']}', due {t['due']} ({t['target_days']} {'working ' if t['clock'] == 'working' else ''}day target)"}
        _keep(stage, w, out, tools)
        return out
    g = graphs.reopen(w["graph"])
    before = (len(g["nodes"]), len(g["edges"]))
    if stage == "graph":
        g = graphs.build(case, profile, w["entities"])
        out: dict[str, Any] = {"summary": f"Case graph written: {len(g['nodes'])} nodes, {len(g['edges'])} edges", **graphs.stats(g)}
    elif stage == "evidence":
        out = checks.evidence(case, profile)
    elif stage == "reference":
        out = checks.references(case, profile, w["entities"], g, fetch=tools.fetch)
        w["references"] = out.pop("records")
    elif stage == "policy":
        out = checks.policy(case, profile, w["references"])
    elif stage == "consistency":
        out = checks.consistency(case, profile, tools.overlay)
    elif stage == "integrity":
        earlier = (lambda key, kind: tools.memory.earlier(case, key, kind)) if tools.memory is not None and (profile.integrity.get("stable")) else None
        out = assurance.integrity(case, profile, w["entities"], today=tools.today(), earlier=earlier, read_bytes=tools.read_bytes)
    elif stage == "verify":
        out = checks.verify(case, profile, w["findings"], g)
        w["recommendation"] = out["recommendation"]
    else:
        raise ValueError(f"unknown stage '{stage}'")
    if stage != "verify":
        _keep(stage, w, out, tools)
    w["graph"] = graphs.finish(g)
    out["graph_writes"] = {"nodes": len(g["nodes"]) - before[0], "edges": len(g["edges"]) - before[1]}
    return out


def _keep(stage: str, w: dict[str, Any], out: dict[str, Any], tools: Tools) -> None:
    """Put a stage's findings on the case in place of the ones its agent left last time, each with what the knowledge graph says its rule rests on."""
    found = out.pop("findings", [])
    rests = (tools.grounding or {}).get("basis") or {}   # as the harness handed it to this agent
    for f in found:
        got = rests.get((f.get("rule") or {}).get("title") or "")
        if got:
            f["basis"] = got
    if rests:
        out["basis"] = sum(1 for f in found if f.get("basis"))
    w["findings"] = [f for f in w["findings"] if f["agent"] != _agent_key(stage)] + found


def _agent_key(stage: str) -> str:
    return {"consistency": "contradiction"}.get(stage, stage)


def plan(stage: str, case: dict[str, Any], profile: Profile, w: dict[str, Any]) -> list[str]:
    """The orchestrator's plan for one stage: the pieces of work it hands the agent, worked out from the case and its use case before the agent starts."""
    docs = [d for d in live_docs(case) if not d.get("assumed")]
    if stage == "intake":
        return [f"Check and read {d['name']}" for d in docs]
    if stage == "classify":
        return [f"Name the kind of {d['name']}" for d in docs]
    if stage == "extract":
        return [f"Lift the facts of {d['name']}" for d in docs]
    if stage == "privacy":
        return [f"Search {d['name']} for personal data" for d in docs]
    if stage == "resolve":
        return ["Link the parties, goods and reference numbers the documents repeat", "Recall what the workspace already knows about them"]
    if stage == "screening":
        return ["Screen every party against the watchlists"] + [f"Screen: {profile.field_name(x['field'])}" for x in profile.screening.get("fields") or []]
    if stage == "integrity":
        return [f"Check: {r['title']}" for r in assurance.declared_rules(profile) if r["family"] == "integrity"]
    if stage == "triage":
        return ["Set the priority", "Name the queue", "Work out the due date against the service target"]
    if stage == "graph":
        return ["Write the case as a graph"]
    if stage == "evidence":
        return [f"Check: {ev['title']}" for ev in profile.evidence]
    if stage == "reference":
        return [f"Retrieve {r.get('name') or r['id']} for the date of the case" for r in profile.references]
    if stage == "policy":
        return [f"Run the rule: {r.get('title') or r['id']}" for r in profile.rules]
    if stage == "consistency":
        return [f"Compare: {r.get('title') or r['id']}" for r in profile.consistency]
    return ["Check that every finding shows its sources", "Aggregate the recommendation for a person"]


def step(case: dict[str, Any], profile: Profile, tools: Tools, stage: str) -> dict[str, Any]:
    """Run one stage (after any stage it depends on that has not run in this assessment), inside the harness, under the agent's contract,
    and record the agent run."""
    w = case.get("working") or begin(case)
    for earlier in STAGES[:STAGES.index(stage)]:
        if earlier not in w["done"]:
            step(case, profile, tools, earlier)
    agent = AGENT_OF[stage]
    contract = C.get(agent["id"])
    calls0 = len(getattr(tools.model, "calls", []))
    case["progress"] = {"state": "running", "stage": stage, "done": list(w["done"])}
    if tools.progress:
        tools.progress(case)
    t0 = time.perf_counter()
    tasks = plan(stage, case, profile, w)
    run = {"id": f"{w['id']}.{len(w['runs']) + 1}", "agent": agent["id"], "name": agent["name"], "stage": stage, "started_at": now(), "status": "ok", "retries": 0,
           "plan": {"tasks": tasks, "done": 0, "by": w.get("by"), "trigger": w.get("trigger")}}
    case["progress"]["plan"] = {"stage": stage, "tasks": len(tasks)}
    try:
        out, trace = H.Harness(tools).run(agent, case, profile, w, lambda scoped: _run_stage(stage, case, profile, scoped, w))
    except Exception as exc:
        trace = getattr(exc, "trace", None) or {"contract": {"id": agent["id"], "version": contract["version"], "kept": True, "needs": [], "promises": [], "broken": [], "on_failure": contract["on_failure"]}, "io": {}}
        run.update(status="failed", error=f"{type(exc).__name__}: {str(exc)[:240]}", duration_ms=round((time.perf_counter() - t0) * 1000, 1), contract=trace["contract"],
                   budget=trace.get("budget"), throttle=trace.get("throttle"), memory=trace.get("memory"), sections=trace.get("sections"), grounding=trace.get("grounding"))
        if contract["on_failure"] == "stop":
            w["runs"].append(run)
            H.record(tools, case, agent, run["id"], run["error"], trace, "failed")
            case["progress"] = {"state": "failed", "stage": stage, "done": list(w["done"]), "error": run["error"]}
            raise
        # degrade or escalate: the assessment carries on without this agent's contribution, and says so
        out = {"summary": f"{agent['name']} did not complete ({run['error']}); the assessment carried on without it", "notes": [run["error"]]}
        if contract["on_failure"] == "escalate":
            trace["contract"].setdefault("broken", []).append(f"did not complete: {run['error']}")
            trace["contract"]["kept"] = False
    calls = list(getattr(tools.model, "calls", []))[calls0:]
    run.update(duration_ms=round((time.perf_counter() - t0) * 1000, 1), summary=out.get("summary", ""), retries=max(0, trace.get("attempts", 1) - 1),
               model=", ".join(sorted({c["model"] for c in calls})) if calls else "none (code)", prompt_version=PROMPT_VERSION if calls else None,
               model_calls=len(calls), input_tokens=sum(c.get("input_tokens", 0) for c in calls), output_tokens=sum(c.get("output_tokens", 0) for c in calls),
               tool_calls=out.pop("tool_calls", []) + [{"tool": f"model.{c['step']}", "detail": c["model"], "status": c.get("error") or "ok", "ms": c["ms"]} for c in calls],
               graph_writes=out.pop("graph_writes", {"nodes": 0, "edges": 0}), notes=out.get("notes") or [],
               contract=trace["contract"], budget=trace.get("budget"), throttle=trace.get("throttle"), memory=trace.get("memory"), io=trace.get("io"),
               sections=trace.get("sections"), grounding=trace.get("grounding"))
    run["plan"]["done"] = len(tasks) if run["status"] != "failed" else 0
    if (trace.get("throttle") or {}).get("shed"):
        run["notes"] = run["notes"] + [f"{trace['throttle']['shed']} model call(s) were not made: the rate limit or this agent's budget was reached"]
    if run["status"] != "failed" and run["notes"]:
        run["status"] = "degraded"
    w["memory"].append({"key": f"{stage}.summary", "value": run["summary"], "by": agent["id"], "at": now()})   # the note this agent leaves for the next ones
    w["runs"].append(run)
    w["stages"][stage] = out
    w["done"].append(stage)
    if not trace["contract"]["kept"]:
        _escalate(case, profile, w, agent, trace["contract"]["broken"])
    H.record(tools, case, agent, run["id"], run["summary"], trace, run["status"])
    if stage == STAGES[-1]:
        _finish(case, profile, tools, w)
    return out


def _escalate(case: dict[str, Any], profile: Profile, w: dict[str, Any], agent: dict[str, Any], broken: list[str]) -> None:
    """A contract that was not kept never passes silently: it becomes a finding a person must look at, and the outcome is a referral."""
    f = finding(f"GOV-{agent['id']}", "governance", f"{agent['name']}: contract not kept", "review",
                f"{agent['name']} did not keep its contract, so its work on this case needs a person's review.", agent="orchestrator", severity="high", reasoning=list(broken), requires_human=True,
                data={"agent": agent["id"], "broken": list(broken)})
    w["findings"] = [x for x in w["findings"] if x["id"] != f["id"]] + [f]
    rec = w.get("recommendation")
    if rec:   # the outcome was already aggregated: a broken contract now overrides it
        rec.update(outcome="review", label=(profile.outcomes.get("review") or {}).get("label") or "review", description=(profile.outcomes.get("review") or {}).get("description") or "")
        rec["reasons"] = [f"{f['title']}: {f['message']}", *rec.get("reasons", [])]
        rec.setdefault("basis", {}).setdefault("review", []).append(f["id"])


def _finish(case: dict[str, Any], profile: Profile, tools: Tools, w: dict[str, Any]) -> None:
    prev = case.get("assessment")
    if prev:
        case.setdefault("history", []).append(brief(prev))
    assessment = {k: w[k] for k in ("id", "at", "trigger", "by", "stages", "runs", "findings", "entities", "unresolved", "graph", "references", "recommendation")}
    assessment.update({k: w.get(k) for k in ("privacy", "screening", "triage")})
    assessment["memory"] = {"working": w.get("memory") or [], "recall": w.get("recall")}
    assessment["contracts"] = {"version": C.get("orchestrator")["version"], "kept": sum(1 for r in w["runs"] if (r.get("contract") or {}).get("kept", True)), "runs": len(w["runs"]),
                               "broken": [{"agent": r["agent"], "why": r["contract"]["broken"]} for r in w["runs"] if not (r.get("contract") or {}).get("kept", True)]}
    assessment["model"] = tools.model.describe() if hasattr(tools.model, "describe") else {"model": getattr(tools.model, "name", "?")}
    assessment["profile_version"] = profile.version
    assessment["overlay_version"] = (tools.overlay or {}).get("version", 0)
    assessment["documents"] = [{"id": d["id"], "name": d["name"], "version": d["current"], "sha256": current(d).get("sha256", "")[:16]} for d in live_docs(case)]
    assessment["summary"] = checks.narrative(case, profile, assessment)
    assessment["metrics"] = {"duration_ms": round(sum(r["duration_ms"] for r in w["runs"]), 1), "model_calls": sum(r.get("model_calls", 0) for r in w["runs"]),
                             "input_tokens": sum(r.get("input_tokens", 0) for r in w["runs"]), "output_tokens": sum(r.get("output_tokens", 0) for r in w["runs"]),
                             "pages": sum(len(current(d).get("pages") or []) for d in live_docs(case)), "rules": len(profile.rules),
                             "graph": graphs.stats(w["graph"])}
    case["assessment"] = assessment
    case.pop("working", None)
    if case.get("decision"):   # a decision belongs to the assessment it was taken on: what changed since has to be decided again
        gone = case["decision"]
        case.setdefault("earlier_decisions", []).append(gone)
        case["decision"] = None
        event(case, "decision.superseded", f"The decision of {gone.get('at')} ({gone.get('decision')}, by {gone.get('by')}) was taken on assessment {gone.get('assessment')}. The case was assessed again, so it waits for a new decision.",
              by="orchestrator", data={"assessment": assessment["id"], "earlier": gone.get("assessment")})
    case["status"] = "awaiting review"
    case["progress"] = {"state": "done", "stage": "", "done": list(STAGES)}
    rec = assessment["recommendation"]
    event(case, "case.assessed", f"Assessment {assessment['id']} ({assessment['trigger']}): {rec['label']}", by=assessment["by"],
          data={"assessment": assessment["id"], "outcome": rec["outcome"], "findings": {f["id"]: f["status"] for f in assessment["findings"]}})
    if tools.on_finish:
        tools.on_finish(case)   # the host writes what the case taught the workspace: the knowledge base and the episode
    if tools.progress:
        tools.progress(case)


def brief(a: dict[str, Any]) -> dict[str, Any]:
    """An assessment as kept in the case history: what was known and concluded at that point."""
    return {"id": a["id"], "at": a["at"], "trigger": a["trigger"], "by": a.get("by"), "outcome": a["recommendation"]["outcome"], "label": a["recommendation"]["label"],
            "findings": {f["id"]: {"status": f["status"], "title": f["title"], "message": f["message"]} for f in a["findings"]}, "documents": a.get("documents") or [],
            "references": [{"id": r["id"], "as_of": r["retrieval"].get("requested_as_of"), "mode": r["retrieval"].get("mode"), "version": r["retrieval"].get("version"),
                            "candidate": r["result"].get("display")} for r in a.get("references") or []], "summary": a.get("summary", "")}


def assess(case: dict[str, Any], profile: Profile, tools: Tools, *, trigger: str = "assessment", by: str = "system") -> dict[str, Any]:
    begin(case, trigger=trigger, by=by)
    for stage in STAGES:
        step(case, profile, tools, stage)
    return case["assessment"]
