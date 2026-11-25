"""DocuInsights on this layer: the pack's coded runner and the workbench mounted under /docuinsights.

The accelerator is its own package (src/docuinsights, no dependency on this layer). This module is the only place the
two meet:

    run(action, inputs, ctx)        the coded runner of every di_* agent (config/packs/docuinsights.yaml): one stage of
                                    an assessment, or an on-demand agent, on the accelerator's engine with the run's
                                    model (so calls are routed, metered and priced like any other agent's)
    register(app, settings, ...)    mounts the accelerator's workbench at /docuinsights under this layer's
                                    authentication; an assessment started there runs as the di_case_assessment flow,
                                    so it shows in the portal with its steps and cost

Cases live under the layer's data folder (<data>/docuinsights), one tenant per customer and one for the operator, so a
customer's cases are never visible to another.
"""
from __future__ import annotations

import re
import threading
import time
from functools import lru_cache
from typing import Any

FLOW = "di_case_assessment"
DEFAULT_SAMPLE = "supplier_invoice/mismatch"   # what a bare run of an agent works on: the plainest use case
STAGE_OF = {"register": "intake", "classify": "classify", "extract": "extract", "find_personal_data": "privacy", "resolve": "resolve", "screen_parties": "screening", "build_graph": "graph",
            "check_evidence": "evidence", "retrieve_and_reason": "reference", "evaluate_rules": "policy", "find_contradictions": "consistency", "check_integrity": "integrity",
            "verify_and_recommend": "verify"}
# The last stage of the engine (triage) has no step of its own to run: it needs the recommendation, so `verify_and_recommend` runs it and closes the
# assessment. The `triage` action of di_triage then reports what it set, which is how the flow shows it as a step.
CHECK = {"pass": "pass", "fail": "fail", "missing": "fail", "review": "warn", "info": "info"}
_locks: dict[tuple[str, str], threading.RLock] = {}
_guard = threading.Lock()


# ------------------------------------------------------------------------------------------------ the layer's model, as the engine sees it
class LayerModel:
    """The run's model behind the small interface the accelerator asks for (offline, name, text, json)."""

    provider = "cx nova layer"

    def __init__(self, llm: Any, meter: Any = None, tier: str = "balanced"):
        self.llm, self.meter, self.tier = llm, meter, tier
        self.offline = bool(getattr(llm, "offline", False))
        self.calls: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        fn = getattr(self.llm, "model_for", None)
        return str(fn(self.tier)) if callable(fn) else "model"

    def text(self, system: str, user: str, *, images: list[tuple[bytes, str]] | None = None, step: str = "", tier: str = "balanced") -> str:
        t0 = time.perf_counter()
        before = (getattr(self.meter, "input_tokens", 0), getattr(self.meter, "output_tokens", 0))
        try:
            out = self.llm.complete(user, system=system, tier=tier, images=images, meter=self.meter)
        except Exception as exc:
            self.calls.append({"step": step, "model": self.name, "ms": round((time.perf_counter() - t0) * 1000), "input_tokens": 0, "output_tokens": 0, "error": type(exc).__name__})
            raise
        self.calls.append({"step": step, "model": self.name, "ms": round((time.perf_counter() - t0) * 1000),
                           "input_tokens": getattr(self.meter, "input_tokens", 0) - before[0], "output_tokens": getattr(self.meter, "output_tokens", 0) - before[1]})
        return out

    def json(self, system: str, user: str, *, images: list[tuple[bytes, str]] | None = None, step: str = "", tier: str = "balanced") -> Any:
        from docuinsights.model import extract_json

        reply = self.text(system, user + "\n\nReply with only the JSON object.", images=images, step=step, tier=tier)
        try:
            return extract_json(reply)
        except Exception:
            return extract_json(self.text(system, user + "\n\nYour last reply was not valid JSON. Reply again with only the JSON object.", images=images, step=step, tier=tier))

    def describe(self) -> dict[str, Any]:
        return {"provider": getattr(self.llm, "provider", self.provider), "model": "offline" if self.offline else self.name, "offline": self.offline}


# ------------------------------------------------------------------------------------------------ the case service on this layer
@lru_cache(maxsize=4)
def _service(root: str) -> Any:
    from docuinsights.model import OfflineModel
    from docuinsights.service import Service

    return Service(root, model=OfflineModel())   # the model is given per call (the run's own); this default never reads for a run


def service(settings: Any = None) -> Any:
    from ..config import data_dir, get_settings

    return _service(str(data_dir(settings or get_settings()) / "docuinsights"))


def tenant_of(customer_id: Any) -> str:
    return ("c-" + re.sub(r"[^A-Za-z0-9._-]+", "-", str(customer_id))[:70]) if customer_id else "operator"


def _lock(tenant: str, ref: str) -> threading.RLock:
    with _guard:
        return _locks.setdefault((tenant, ref), threading.RLock())


def _from_upstream(ctx: Any) -> str:
    for u in reversed(list((getattr(ctx, "upstream", None) or {}).values())):
        out = (u or {}).get("output") or {} if isinstance(u, dict) else {}
        res = out.get("result") if isinstance(out, dict) else None
        if isinstance(res, dict) and res.get("case_ref"):
            return str(res["case_ref"])
    return ""


def _open_case(svc: Any, tenant: str, inputs: dict[str, Any], ctx: Any, *, create: bool) -> tuple[str, list[dict[str, Any]]]:
    """The case a step works on: the one named, the one an earlier step of the run registered, a package sent with the
    call, or a bundled sample (the default when nothing at all is given, and the step says so)."""
    from docuinsights.service import Service, ServiceError

    notes: list[dict[str, Any]] = []
    ref = str(inputs.get("case_ref") or "").strip() or _from_upstream(ctx)
    if ref:
        svc.case(tenant, ref)   # raises a 404 ServiceError when it does not exist
        return ref, notes
    try:
        if inputs.get("documents"):
            if not create:
                raise ValueError("a package is registered by the intake agent: run di_intake first, or name the case with case_ref")
            reference = str(inputs.get("reference") or "").strip()
            if not reference:
                raise ValueError("give `reference` for the new case with its `documents`")
            fields = inputs.get("fields") if isinstance(inputs.get("fields"), dict) else {}
            svc.create_case(tenant, reference, inputs.get("profile") or None, fields, by=_actor(ctx))
            for d in inputs["documents"] if isinstance(inputs["documents"], list) else []:
                name, data, category = Service.decode(d if isinstance(d, dict) else {})
                svc.add_document(tenant, reference, name, data, category=category, by=_actor(ctx))
            return reference, notes
        sample = str(inputs.get("sample") or "").strip()
        if not sample:   # nothing given (a self-test, a health probe, a bare call): one standing copy of the default sample, reused
            notes.append({"rule": "a case was given", "status": "warn", "message": f"no case_ref, package or sample was given: the bundled sample {DEFAULT_SAMPLE} was used"})
            profile_id, _, sample_id = DEFAULT_SAMPLE.partition("/")
            standing = f"SELFTEST-{profile_id}-{sample_id}"
            if not svc.store.exists(tenant, standing):
                svc.load_sample(tenant, profile_id, sample_id, reference=standing, by=_actor(ctx))
            return standing, notes
        profile_id, _, sample_id = sample.partition("/")
        case = svc.load_sample(tenant, profile_id, sample_id or "review", by=_actor(ctx))
        return case["reference"], notes
    except ServiceError as exc:
        raise ValueError(str(exc)) from exc


def _actor(ctx: Any) -> str:
    cid = (getattr(ctx, "options", None) or {}).get("customer_id")
    return f"customer:{cid}" if cid else "operator"


def _brief(f: dict[str, Any]) -> dict[str, Any]:
    return {**{k: f.get(k) for k in ("id", "family", "title", "status", "severity", "message", "requires_human")},
            "basis": [{k: b.get(k) for k in ("id", "name", "citation")} for b in f.get("basis") or []]}   # what the knowledge graph says its rule rests on


def _checks(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"rule": f["title"], "status": CHECK.get(f["status"], "info"), "message": f["message"],
             "severity": f["severity"] if f.get("severity") in ("low", "medium", "high", "critical") else "medium"} for f in findings][:60]


# ------------------------------------------------------------------------------------------------ the pack's entry point
def run(action: str, inputs: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """The coded runner for every DocuInsights agent (caf.packs: `code: caf.accelerators.docuinsights:run`).

    Wrong or missing input raises ValueError (the step fails and says why; nothing is modelled in its place)."""
    from docuinsights import pipeline as P
    from docuinsights.profiles import ProfileError, get_profile
    from docuinsights.service import ServiceError
    from docuinsights.store import StoreError

    svc = service(getattr(ctx, "settings", None))
    tenant = tenant_of((getattr(ctx, "options", None) or {}).get("customer_id"))
    model = LayerModel(ctx.llm, getattr(ctx, "meter", None), getattr(ctx, "tier", "balanced"))
    try:
        if action == "list_proposals" or action == "decide_proposal":
            return _feedback(svc, tenant, action, inputs, ctx)
        if action == "knowledge":   # the workspace's knowledge base: needs no case
            ov = svc.knowledge_overview(tenant)
            q = str(inputs.get("query") or inputs.get("request") or "").strip()
            kb = svc.knowledge(tenant)
            matches = kb.search(q, 8) if q else ov["known"]
            detail = kb.entity(matches[0]["id"]) if q and matches else None
            return {"summary": f"Knowledge base: {ov['index']['entities']} entities, {ov['index']['triples']} relationships, quality {ov['quality']['score']}" + (f"; {len(matches)} match(es) for '{q}'" if q else ""),
                    "result": {"entities": ov["index"]["entities"], "relationships": ov["index"]["triples"], "quality": ov["quality"]["score"], "by_type": ov["index"]["byType"], "matches": matches,
                               "sources": {"cases": ov["sources"]["cases"], "imports": [i["label"] for i in ov["sources"]["imports"]], "knowledge_bases": [b["name"] for b in ov["knowledge_bases"] if b["kind"] == "curated" and b["on"]]},
                               "format": ov["format"],
                               "first_match": {"entity": detail["entity"], "origin": detail["origin"], "support": detail["support"][:5], "links": [{k: x[k] for k in ("direction", "predicate", "other_name", "other_type")} for x in detail["links"][:25]],
                                               "way_to_it": [x["name"] for x in detail["trail"]], "rests_on": [{k: b.get(k) for k in ("id", "name", "citation", "says")} for b in detail["basis"]],
                                               "used_in": {"rules": [r["name"] for r in detail["usage"]["rules"][:12]], "findings": detail["usage"]["by_status"], "cases": [c["name"] for c in detail["usage"]["cases"]]}} if detail else None},
                    "checks": [{"rule": "citations checked against the text", "status": "pass" if not (ov["verification"].get("stats") or {}).get("missing") else "warn",
                                "message": f"{(ov['verification'].get('stats') or {}).get('missing', 0)} citation(s) could not be found in the text they cite"}],
                    "artifacts": [{"type": "knowledge", "name": "The workspace's knowledge base", "content": {"index": ov["index"], "quality": ov["quality"], "verification": ov["verification"]}}]}
        ref, notes = _open_case(svc, tenant, inputs, ctx, create=action == "register")
        with _lock(tenant, ref):
            if action in STAGE_OF:
                return _stage(svc, tenant, ref, STAGE_OF[action], model, ctx, notes, P, get_profile)
            case = svc.case(tenant, ref)
            if not case.get("assessment"):   # an on-demand agent on a case that was never assessed: assess it first, on the run's model
                case = svc.assess(tenant, ref, by=_actor(ctx), model=model)
                notes.append({"rule": "the case was assessed", "status": "info", "message": "the case had no assessment yet: it was assessed first"})
            if action == "triage":   # what the triage agent set when the assessment closed, set against today
                from docuinsights.assurance import due_state

                t = (case["assessment"].get("triage") or {})
                if not t:
                    raise ValueError(f"case {ref} was assessed before triage existed: assess it again")
                state = due_state(t) if not case.get("decision") else {}
                return {"summary": f"Priority {t['priority']}, queue '{t['queue_name']}', due {t['due']}" + (f" (overdue by {-state['days_left']} day(s))" if state.get("overdue") else ""),
                        "result": {"case_ref": ref, **{k: t.get(k) for k in ("priority", "queue", "queue_name", "due", "target_days", "clock", "open", "reasons")}, **state,
                                   "assigned": (case.get("assigned") or {}).get("to")},
                        "checks": notes + [{"rule": "within the service target", "status": "fail" if state.get("overdue") else "warn" if state.get("due_soon") else "pass",
                                            "message": f"due {t['due']}; {state.get('days_left', '?')} day(s) left" if state else f"due {t['due']}"}],
                        "human_followups": [f"{t['queue_name']}: pick up {ref} (priority {t['priority']}, due {t['due']})"] if not case.get("decision") else []}
            if action == "disposition":   # a person's answer to a possible watchlist match, recorded with its reason; the case is assessed again
                fid, decision = str(inputs.get("finding_id") or "").strip(), str(inputs.get("decision") or "").strip()
                if not fid or decision not in ("clear", "confirm"):
                    raise ValueError("give finding_id and a decision of clear or confirm (and a reason to clear)")
                case = svc.review(tenant, ref, {"target": "screening", "id": fid, "decision": decision, "reason": str(inputs.get("reason") or "")}, by=_actor(ctx))
                a = case["assessment"]
                left = [h for h in (a.get("screening") or {}).get("hits") or [] if not h.get("decision")]
                return {"summary": f"Match {fid} {'cleared' if decision == 'clear' else 'confirmed'}; {len(left)} still waiting; recommendation: {a['recommendation']['label']}",
                        "result": {"case_ref": ref, "decision": decision, "waiting": len(left), "matches": (a.get("screening") or {}).get("hits"), "recommendation": a["recommendation"], "triage": a.get("triage")},
                        "checks": notes}
            if action == "decision_record":
                masked = inputs.get("masked") is True or str(inputs.get("masked") or "").strip().lower() in ("1", "true", "yes", "y", "on")
                page = svc.record(tenant, ref, masked=masked, by=_actor(ctx))
                return {"summary": f"Decision record of {ref}" + (" with personal data masked" if masked else "") + f" ({len(page):,} characters of HTML)",
                        "result": {"case_ref": ref, "masked": masked, "workbench": f"/docuinsights/api/cases/{ref}/record" + ("?mask=1" if masked else "")}, "checks": notes,
                        "artifacts": [{"type": "decision_record", "name": f"Decision record {ref}", "content": page}]}
            if action == "what_if":
                change = inputs.get("change") if isinstance(inputs.get("change"), dict) else None
                out = svc.counterfactual(tenant, ref, scenario=str(inputs.get("scenario") or "") or None, change=change, value=inputs.get("value"), model=model)
                moved = [m for m in out["moved"] if m["before"] != m["after"]]
                return {"summary": f"What-if ({out['change']}): {out['before']['label']} -> {out['after']['label']}; {len(moved)} finding(s) moved",
                        "result": {"case_ref": ref, **{k: out.get(k) for k in ("change", "before", "after", "explanation", "references", "draft")},
                                   "moved": [{k: m[k] for k in ("id", "title", "before", "after", "after_message")} for m in out["moved"]]},
                        "checks": notes, "artifacts": [{"type": "impact", "name": f"What-if: {out['change']}", "content": out}]}
            if action == "draft":
                out = svc.correspondence(tenant, ref, str(inputs.get("kind") or "evidence_request"), model=model)
                return {"summary": (f"Draft {out['kind']} with {len(out['items'])} item(s) ({out['method']})" if out["body"] else out["note"]),
                        "result": {"case_ref": ref, **{k: out.get(k) for k in ("kind", "subject", "body", "items", "method", "note")}}, "checks": notes,
                        "artifacts": [{"type": "draft", "name": out["subject"] or out["kind"], "content": out}] if out["body"] else [],
                        "human_followups": ["Caseworker: review, edit and send the draft"] if out["body"] else []}
            if action == "ask":
                q = str(inputs.get("question") or inputs.get("request") or "").strip()
                if not q:
                    raise ValueError("ask a question")
                out = svc.ask(tenant, ref, q, model=model)
                return {"summary": out["answer"].split("\n")[0][:300], "checks": notes,
                        "result": {"case_ref": ref, **{k: out.get(k) for k in ("question", "answer", "grounded", "graph_path", "method")},
                                   "sources": [{k: s.get(k) for k in ("n", "kind", "id", "doc_name", "field_name", "page", "line") if s.get(k) is not None} for s in out["sources"]]}}
            if action == "file_case":
                out = svc.file_case(tenant, ref, by=_actor(ctx))
                return {"summary": f"{len(out['documents'])} document(s) and the assessment filed to the {out['repository']} repository",
                        "result": {"case_ref": ref, **out}, "checks": notes, "artifacts": [{"type": "filing_record", "name": "What was filed where", "content": out}]}
    except (ServiceError, StoreError, ProfileError, KeyError) as exc:
        raise ValueError(str(exc.args[0]) if exc.args else str(exc)) from exc
    except ValueError:
        raise
    except Exception as exc:   # the accelerator's own code failed: the step fails and says so. The layer never lets a model or a sample stand in for an assessment
        raise ValueError(f"DocuInsights could not complete {action}, and nothing stands in for its code. What went wrong: {' '.join(str(exc).split())[:200] or 'no detail was given'}") from exc
    raise ValueError(f"unknown action {action}")


def _stage(svc: Any, tenant: str, ref: str, stage: str, model: Any, ctx: Any, notes: list[dict[str, Any]], P: Any, get_profile: Any) -> dict[str, Any]:
    case = svc.case(tenant, ref)
    profile = get_profile(case["profile"])
    if not case["documents"]:
        raise ValueError(f"case {ref} has no documents yet")
    tools = svc.tools(tenant, case, profile, model, progress=False)
    if stage == "intake" or not case.get("working"):   # intake opens a new assessment; so does a stage run on its own
        P.begin(case, trigger="assessment", by=_actor(ctx))
    try:
        out = P.step(case, profile, tools, stage)
        if stage == "verify":   # what follows the recommendation (triage) runs with it, so that this step leaves a finished assessment as it always did
            for later in P.STAGES[P.STAGES.index("verify") + 1:]:
                P.step(case, profile, tools, later)
    finally:
        svc.store.save(tenant, case)
    w = case.get("working") or {}
    a = case.get("assessment") or {}
    result: dict[str, Any] = {"case_ref": ref, "profile": profile.id, "stage": stage}
    served: dict[str, Any] = {"summary": out.get("summary", ""), "result": result, "checks": list(notes)}
    agent_key = {"consistency": "contradiction"}.get(stage, stage)
    found = [f for f in (a.get("findings") if stage == "verify" else w.get("findings")) or [] if stage == "verify" or f["agent"] == agent_key]
    if stage == "intake":
        result.update(documents=out["documents"], unread=out["unread"], failed_checks=out["failed_checks"], workbench=f"/docuinsights/#{ref}")
    elif stage in ("classify", "extract"):
        result.update({k: v for k, v in out.items() if k not in ("summary", "notes", "graph_writes")})
    elif stage == "resolve":
        result.update(entities=out["entities"], unresolved=out["unresolved"],
                      entity_list=[{"id": e["id"], "type": e["type"], "name": e["name"], "roles": e.get("roles") or []} for e in w.get("entities") or []],
                      possible_matches=w.get("unresolved") or [])
    elif stage == "graph":
        result.update(nodes=out.get("nodes"), edges=out.get("edges"), by_type=out.get("by_type"))
        served["artifacts"] = [{"type": "case_graph", "name": "The case graph", "content": w.get("graph")}]
    elif stage == "reference":
        result.update(references=[{"id": r["id"], "mode": r["retrieval"].get("mode"), "as_of": r["retrieval"].get("requested_as_of"), "url": r["retrieval"].get("url"),
                                   "version": r["retrieval"].get("version"), "status": r["result"].get("status"), "candidate": r["result"].get("display"),
                                   "missing": r["result"].get("missing") or []} for r in w.get("references") or []])
    elif stage == "verify":
        rec = a["recommendation"]
        result.update(recommendation=rec, summary=a["summary"], coverage=out.get("coverage"), blocked=out.get("blocked"), assessment=a["id"], triage=a.get("triage"), workbench=f"/docuinsights/#{ref}")
        served["artifacts"] = [{"type": "assessment", "name": f"Assessment {a['id']} of {ref}", "content": {k: a[k] for k in ("id", "at", "summary", "recommendation", "findings", "references", "runs", "metrics")}},
                               {"type": "recommendation", "name": rec["label"], "content": rec}]
        served["human_followups"] = [f"Caseworker: decide the case (recommendation: {rec['label']})"] + [f"Request: {r}" for r in rec["requests"]]
    elif stage == "privacy":
        result.update(personal_data=w.get("privacy"))
    elif stage == "screening":
        scr = w.get("screening") or {}
        result.update(lists=scr.get("lists"), screened=scr.get("screened"), matches=scr.get("hits"))
        if any(not h.get("decision") for h in scr.get("hits") or []):
            served["human_followups"] = ["Compliance: confirm or clear each possible watchlist match, with a reason"]
    if stage not in ("intake", "classify", "extract", "resolve", "graph"):
        result["findings"] = [_brief(f) for f in found]
        served["checks"] += _checks(found)
    for note in out.get("notes") or []:
        served["checks"].append({"rule": "model reading", "status": "warn", "message": note})
    return served


def _feedback(svc: Any, tenant: str, action: str, inputs: dict[str, Any], ctx: Any) -> dict[str, Any]:
    profile = str(inputs.get("profile") or DEFAULT_SAMPLE.partition("/")[0])
    if action == "decide_proposal":
        if not inputs.get("proposal_id") or inputs.get("decision") not in ("approve", "reject"):
            raise ValueError("give proposal_id and a decision of approve or reject")
        prop = svc.decide_proposal(tenant, profile, str(inputs["proposal_id"]), str(inputs["decision"]), by=_actor(ctx), accept_changes=bool(inputs.get("accept_changes")))
        reg = prop.get("regression") or {}
        return {"summary": f"{prop['id']} {prop['status']}" + (f": the regression changed {len(reg.get('changed') or [])} check(s) on the bundled samples" if prop["status"] == "held" else ""),
                "result": {"profile": profile, "proposal": prop},
                "human_followups": ["Governance: accept the listed changes to promote the proposal, or reject it"] if prop["status"] == "held" else []}
    fb = svc.feedback(tenant, profile)
    notes = [] if inputs.get("profile") else [{"rule": "a use case was given", "status": "warn", "message": f"no profile was given: the proposals of the sample use case {profile} are listed"}]
    return {"summary": f"{profile}: {len(fb['proposals'])} proposal(s), vocabulary v{fb['overlay'].get('version', 0)}",
            "result": {"profile": profile, "vocabulary_version": fb["overlay"].get("version", 0), "vocabulary": {k: fb["overlay"].get(k) for k in ("signals", "synonyms")},
                       "proposals": fb["proposals"]}, "checks": notes}


# ------------------------------------------------------------------------------------------------ the workbench under this layer
def register(app: Any, settings: Any, shop: Any = None, orchestrator: Any = None) -> None:
    """Mount the DocuInsights workbench at /docuinsights. The layer's ApiAuth has already resolved who is calling."""
    from docuinsights.app import create_app
    from docuinsights.service import ServiceError

    svc = service(settings)
    customers: dict[str, Any] = {}

    def customer(request: Any) -> Any:
        return getattr(request.state, "customer", None)

    def tenant(request: Any) -> str:
        cust = customer(request)
        if cust is not None and shop is not None:
            try:
                shop.require_entitled(cust, "di_verifier")   # the workbench is for customers who rent the pack
            except Exception as exc:
                raise ServiceError(str(exc), 403) from exc
        t = tenant_of(cust["id"] if cust else None)
        customers[t] = cust
        return t

    def actor(request: Any) -> str:
        cust = customer(request)
        return f"customer:{cust['id']}" if cust else str(getattr(request.state, "role", "") or "operator")

    def runner(t: str, reference: str, by: str) -> None:
        """An assessment started in the workbench runs as the layer's flow: planned, metered and visible in the portal."""
        view = shop.run_flow(customers.get(t), FLOW, {"case_ref": reference}, orchestrator=orchestrator, goal=f"Assess case {reference}", surface="api")
        case = svc.case(t, reference)
        case["layer_run"] = view.get("run_id")
        if (case.get("progress") or {}).get("state") == "running":   # the flow ended without finishing the assessment
            case["progress"] = {"state": "failed", "stage": (case.get("progress") or {}).get("stage", ""), "done": (case.get("progress") or {}).get("done", []),
                                "error": f"the layer's run {view.get('run_id')} ended as {view.get('status')}: open it in the portal"}
            case.pop("working", None)
        svc.store.save(t, case)

    operator_model: list[Any] = []

    def model_of(t: str) -> Any:
        """What the workbench's own on-demand actions read with: the operator's model for the operator; a customer's
        on-demand actions stay on patterns here (their assessments run as the flow, on their own model and policy)."""
        from docuinsights.model import OfflineModel

        if customers.get(t) is not None:
            return OfflineModel()
        if not operator_model:
            try:
                from ..core.llm import LLM

                operator_model.append(LayerModel(LLM(settings.llm)))
            except Exception:
                operator_model.append(OfflineModel())
        return operator_model[0]

    if shop is not None and orchestrator is not None:
        svc.runner = runner
        svc.model_of = model_of
    app.mount("/docuinsights", create_app(svc, tenant_of=tenant, actor_of=actor, auth="host", links={"portal": "../portal"}))
