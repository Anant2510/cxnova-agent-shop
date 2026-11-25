"""The case file: one JSON document per case, and the small helpers every stage reads it with.

    case
      reference, profile, fields{...}                 the case facts given at intake
      documents[]                                     each with immutable versions, a classification and extracted fields
      assessment{...}                                 the latest assessment: entities, graph, findings, recommendation, agent runs
      history[]                                       earlier assessments in brief (what was known at each decision point)
      events[]                                        the audit trail: every upload, assessment, review, correction and decision
      reviews[], decision, feedback, chat[]
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from . import values as V
from .profiles import Profile


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_case(reference: str, profile: Profile, fields: dict[str, Any] | None = None, *, by: str = "intake") -> dict[str, Any]:
    case: dict[str, Any] = {"reference": reference, "profile": profile.id, "profile_version": profile.version, "created_at": now(), "status": "registered",
                            "fields": {}, "documents": [], "assessment": None, "history": [], "events": [], "reviews": [], "decision": None, "chat": [],
                            "progress": {"state": "idle", "stage": "", "done": []}}
    for f in profile.case.get("fields") or []:
        value = (fields or {}).get(f["id"], f.get("default"))
        if value not in (None, ""):
            case["fields"][f["id"]] = value
    if not case["fields"].get("received_date"):
        case["fields"]["received_date"] = now()[:10]
    event(case, "case.registered", f"Case {reference} registered on profile {profile.name} v{profile.version}", by=by)
    return case


def event(case: dict[str, Any], kind: str, detail: str, *, by: str = "system", data: dict[str, Any] | None = None) -> dict[str, Any]:
    ev = {"id": f"E{len(case['events']) + 1}", "at": now(), "kind": kind, "by": by, "detail": detail}
    if data:
        ev["data"] = data
    case["events"].append(ev)
    return ev


def current(doc: dict[str, Any]) -> dict[str, Any]:
    return doc["versions"][doc["current"] - 1]


def doc_type(doc: dict[str, Any]) -> str | None:
    """The type the case works with: a reviewer's correction, else the classifier's answer."""
    return doc.get("effective_type") or (doc.get("classification") or {}).get("type")


def docs_of(case: dict[str, Any], type_id: str) -> list[dict[str, Any]]:
    return [d for d in case["documents"] if not d.get("removed") and doc_type(d) == type_id]


def live_docs(case: dict[str, Any]) -> list[dict[str, Any]]:
    return [d for d in case["documents"] if not d.get("removed")]


def case_value(case: dict[str, Any], profile: Profile, field_id: str) -> tuple[Any, str]:
    ftype = next((f.get("type", "text") for f in profile.case.get("fields") or [] if f["id"] == field_id), "text")
    return V.normalise(case["fields"].get(field_id), ftype), ftype


def assertions(case: dict[str, Any], profile: Profile, ref: str) -> list[dict[str, Any]]:
    """Every value the case holds for `type.field` (one per document of that type) or `case.field`, with its source."""
    head, _, fld = ref.partition(".")
    if head == "case":
        norm, ftype = case_value(case, profile, fld)
        if norm is None:
            return []
        return [{"ref": ref, "doc": "case", "doc_name": "Case record", "type": "case", "version": 0, "field": fld, "field_name": profile.field_name(ref),
                 "value": case["fields"].get(fld), "norm": norm, "ftype": ftype, "confidence": 1.0, "page": None, "line": None,
                 "snippet": f"{profile.field_name(ref)}: {case['fields'].get(fld)}", "verified": True, "method": "case record"}]
    out = []
    for d in docs_of(case, head):
        f = (d.get("fields") or {}).get(fld)
        if not f or f.get("norm") is None:
            continue
        anchor = f.get("anchor") or {}
        out.append({"ref": ref, "doc": d["id"], "doc_name": d["name"], "type": head, "version": d["current"], "field": fld, "field_name": profile.field_name(ref),
                    "value": f.get("value"), "norm": f["norm"], "ftype": f.get("type", "text"), "confidence": f.get("confidence", 0.0),
                    "page": anchor.get("page"), "line": anchor.get("line"), "snippet": anchor.get("snippet") or "", "verified": bool(f.get("verified")),
                    "method": f.get("method", "")})
    return out


def source(a: dict[str, Any]) -> dict[str, Any]:
    """An assertion as a citation on a finding."""
    return {k: a.get(k) for k in ("doc", "doc_name", "type", "version", "field", "field_name", "value", "page", "line", "snippet", "verified", "confidence", "method")}


def finding(fid: str, family: str, title: str, status: str, message: str, *, agent: str, severity: str = "medium", reasoning: list[str] | None = None,
            sources: list[dict[str, Any]] | None = None, path: list[str] | None = None, rule: dict[str, Any] | None = None,
            reference: dict[str, Any] | None = None, requires_human: bool | None = None, data: dict[str, Any] | None = None) -> dict[str, Any]:
    """status: pass | fail | review | missing | info. Every finding carries what it rests on."""
    srcs = [source(s) if "ref" in s else s for s in sources or []]
    conf = min([s.get("confidence") or 0 for s in srcs], default=1.0) if srcs else 1.0
    return {"id": fid, "family": family, "title": title, "status": status, "severity": severity, "message": message, "reasoning": reasoning or [],
            "sources": srcs, "graph_path": path or [], "rule": rule, "reference": reference, "agent": agent, "confidence": round(conf, 2),
            "requires_human": status in ("fail", "review", "missing") if requires_human is None else requires_human, "data": data or {}}


def aid(doc_id: str, field_id: str) -> str:
    """Graph node id of an evidence assertion."""
    return f"{doc_id}.{field_id}"
