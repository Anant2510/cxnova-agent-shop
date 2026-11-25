"""Agent contracts: loading them, and the checks behind every line that names one.

A contract is a promise in both directions: what the agent may count on before it starts (`needs`), what everyone else
may count on when it ends (`promises`), what it may touch (`writes`), what it may spend (`model`, `limits`) and what it
may remember (`memory`). The harness reads the contract and enforces it; this module holds the checks.

A check is a function of the case as it stands: it answers (kept, detail). It never repairs anything.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

import yaml

from .casefile import current, doc_type, live_docs

HERE = Path(__file__).resolve().parent
OUTCOMES = ("proceed", "request_evidence", "review")
Check = Callable[[dict[str, Any], Any, dict[str, Any]], tuple[bool, str]]
CHECKS: dict[str, Check] = {}


class ContractBroken(RuntimeError):
    pass


def check(fn: Check) -> Check:
    CHECKS[fn.__name__] = fn
    return fn


def _docs(case: dict[str, Any]) -> list[dict[str, Any]]:
    return [d for d in live_docs(case) if not d.get("assumed")]


def _w(case: dict[str, Any]) -> dict[str, Any]:
    return case.get("working") or case.get("assessment") or {}


# ---- needs
@check
def case_has_documents(case, profile, ctx):
    n = len(live_docs(case))
    return n > 0, f"{n} document(s) in the package"


@check
def documents_are_read(case, profile, ctx):
    unread = [d["name"] for d in _docs(case) if "text" not in current(d)]
    return not unread, "every document has been read" if not unread else f"not read yet: {', '.join(unread[:4])}"


@check
def documents_are_classified(case, profile, ctx):
    todo = [d["name"] for d in _docs(case) if not d.get("classification") and not d.get("effective_type")]
    return not todo, "every document has a kind" if not todo else f"not classified yet: {', '.join(todo[:4])}"


@check
def facts_are_extracted(case, profile, ctx):
    todo = [d["name"] for d in _docs(case) if doc_type(d) and "fields" not in d]
    return not todo, "facts were extracted from every classified document" if not todo else f"no facts yet from: {', '.join(todo[:4])}"


@check
def entities_are_resolved(case, profile, ctx):
    ok = "resolve" in (ctx.get("done") or []) or bool(_w(case).get("entities")) or bool(case.get("assessment"))
    return ok, "the entities of the case are resolved" if ok else "entity resolution has not run"


@check
def findings_exist(case, profile, ctx):
    n = len(_w(case).get("findings") or [])
    return n > 0, f"{n} finding(s) to verify"


@check
def case_is_assessed(case, profile, ctx):
    return bool(case.get("assessment")), "the case has an assessment" if case.get("assessment") else "the case has not been assessed"


# ---- promises
@check
def versions_are_hashed(case, profile, ctx):
    bad = [d["name"] for d in _docs(case) if len(str(current(d).get("sha256") or "")) != 64]
    return not bad, f"{len(_docs(case))} version(s), each with its SHA-256" if not bad else f"no hash on: {', '.join(bad[:4])}"


@check
def documents_read_or_reported(case, profile, ctx):
    bad = [d["name"] for d in _docs(case) if not current(d).get("method") or (current(d).get("method") == "none" and not current(d).get("note"))]
    return not bad, "every file was read or its failure explained" if not bad else f"neither read nor explained: {', '.join(bad[:4])}"


@check
def types_come_from_the_profile(case, profile, ctx):
    bad = [f"{d['name']} -> {(d.get('classification') or {}).get('type')}" for d in _docs(case) if (d.get("classification") or {}).get("type") not in (None, *profile.types)]
    return not bad, "every kind is one the use case declares" if not bad else f"undeclared kind: {'; '.join(bad[:3])}"


@check
def confidence_is_stated(case, profile, ctx):
    bad = [d["name"] for d in _docs(case) if d.get("classification") and not (0 <= float(d["classification"].get("confidence") or 0) <= 1 and d["classification"].get("method"))]
    return not bad, "every classification states its confidence and method" if not bad else f"no confidence or method on: {', '.join(bad[:4])}"


@check
def values_are_anchored_or_flagged(case, profile, ctx):
    bad, n = [], 0
    for d in _docs(case):
        for fid, f in (d.get("fields") or {}).items():
            n += 1
            if f.get("verified") and f.get("method") != "human" and not (f.get("anchor") or {}).get("snippet"):
                bad.append(f"{d['name']}.{fid}")
    return not bad, f"{n} value(s), each anchored to a line or marked unverified" if not bad else f"verified without a line: {', '.join(bad[:4])}"


@check
def fields_are_declared(case, profile, ctx):
    bad = [f"{d['name']}.{fid}" for d in _docs(case) for fid in (d.get("fields") or {}) if doc_type(d) and profile.field(doc_type(d), fid) is None]
    return not bad, "only declared fields were extracted" if not bad else f"undeclared field: {', '.join(bad[:4])}"


@check
def entities_trace_to_mentions(case, profile, ctx):
    ents = _w(case).get("entities") or []
    bad = [e["name"] for e in ents if not e.get("mentions")]
    return not bad, f"{len(ents)} entit(ies), each with the lines that mention it" if not bad else f"no mention behind: {', '.join(bad[:4])}"


@check
def near_matches_are_left_to_a_person(case, profile, ctx):
    w = _w(case)
    ids = {e["id"] for e in w.get("entities") or []}
    bad = [m["id"] for m in w.get("unresolved") or [] if m.get("a") not in ids or m.get("b") not in ids]
    return not bad, f"{len(w.get('unresolved') or [])} possible match(es) left for a person; both sides kept apart" if not bad else "a possible match lost one of its sides"


@check
def graph_has_no_dangling_edges(case, profile, ctx):
    g = _w(case).get("graph") or {}
    ids = {n["id"] for n in g.get("nodes") or []}
    bad = [e for e in g.get("edges") or [] if e.get("source", e.get("from")) not in ids or e.get("target", e.get("to")) not in ids]
    return not bad, f"{len(g.get('edges') or [])} link(s), all between existing nodes" if not bad else f"{len(bad)} link(s) point at a node that does not exist"


@check
def every_requirement_is_answered(case, profile, ctx):
    got = {(f.get("data") or {}).get("requirement") for f in _w(case).get("findings") or [] if f.get("family") == "evidence"}
    bad = [ev["id"] for ev in profile.evidence if ev["id"] not in got]
    return not bad, f"{len(profile.evidence)} requirement(s), each with a finding" if not bad else f"no finding for: {', '.join(bad)}"


def _own(case: dict[str, Any], ctx: dict[str, Any]) -> list[dict[str, Any]]:
    return [f for f in _w(case).get("findings") or [] if f.get("agent") == ctx.get("agent")]


@check
def findings_show_their_reasoning(case, profile, ctx):
    bad = [f["id"] for f in _own(case, ctx) if not f.get("reasoning") and not f.get("message")]
    return not bad, f"{len(_own(case, ctx))} finding(s), each with its reasoning" if not bad else f"no reasoning on: {', '.join(bad[:4])}"


@check
def retrievals_are_recorded(case, profile, ctx):
    recs = _w(case).get("references") or []
    bad = [r.get("id") for r in recs if not all((r.get("retrieval") or {}).get(k) for k in ("requested_as_of", "retrieved_at", "mode")) or not ((r.get("retrieval") or {}).get("result_hash") or (r.get("retrieval") or {}).get("error"))]
    return not bad, f"{len(recs)} retrieval(s), each with its date, time and content hash" if not bad else f"incomplete retrieval record: {', '.join(str(b) for b in bad)}"


@check
def rule_findings_carry_a_version(case, profile, ctx):
    bad = [f["id"] for f in _own(case, ctx) if not (f.get("rule") or {}).get("version")]
    return not bad, f"{len(_own(case, ctx))} rule result(s), each under a rule version" if not bad else f"no rule version on: {', '.join(bad[:4])}"


@check
def discrepancies_cite_both_sides(case, profile, ctx):
    bad = [f["id"] for f in _own(case, ctx) if f.get("status") == "fail" and len(f.get("sources") or []) < 2]
    return not bad, "every discrepancy names the sources that disagree" if not bad else f"one-sided discrepancy: {', '.join(bad[:4])}"


@check
def outcome_is_one_of_three(case, profile, ctx):
    out = (_w(case).get("recommendation") or {}).get("outcome")
    return out in OUTCOMES, f"outcome '{out}'"


@check
def no_unsupported_positive(case, profile, ctx):
    w = _w(case)
    basis = set(((w.get("recommendation") or {}).get("basis") or {}).get("pass") or [])
    bad = [f["id"] for f in w.get("findings") or [] if f.get("blocked") and f["id"] in basis]
    return not bad, "no blocked finding counts towards the outcome" if not bad else f"a blocked finding was counted: {', '.join(bad)}"


@check
def a_person_decides(case, profile, ctx):
    rec = _w(case).get("recommendation") or {}
    return rec.get("requires_human") is True and "no model" in str(rec.get("decided_by")), "the recommendation waits for a person and no model took part in it"


# ---- the assurance agents
@check
def recommendation_exists(case, profile, ctx):
    ok = bool(_w(case).get("recommendation"))
    return ok, "the recommendation has been aggregated" if ok else "there is no recommendation to triage yet"


@check
def personal_data_is_reported(case, profile, ctx):
    got = _w(case).get("privacy")
    ok = isinstance(got, dict) and isinstance(got.get("categories"), list) and isinstance(got.get("documents"), list)
    return ok, f"{len(got['categories'])} kind(s) of personal data reported, by document" if ok else "no report of the personal data in the package"


@check
def personal_values_are_masked(case, profile, ctx):
    from . import assurance

    bad = [f["id"] for f in _own(case, ctx) for text in [f.get("message") or "", *(f.get("reasoning") or []), *[str(s.get("value") or "") + " " + str(s.get("snippet") or "") for s in f.get("sources") or []]]
           if assurance.personal_data(text)]
    return not bad, "no finding repeats a personal value in the clear" if not bad else f"a personal value is shown unmasked in: {', '.join(dict.fromkeys(bad))}"


@check
def every_party_is_screened(case, profile, ctx):
    w = _w(case)
    scr = w.get("screening") or {}
    if not scr.get("enabled", True):
        return True, "screening is switched off for this use case"
    roles = set((profile.screening or {}).get("roles") or [])
    want = [e["name"] for e in w.get("entities") or [] if e["type"] == "Party" and (not roles or roles & set(e.get("roles") or []))]
    got = {s["name"] for s in scr.get("screened") or []}
    bad = [n for n in want if n not in got]
    return not bad, f"{len(want)} part(ies) screened against {sum(x.get('entries', 0) for x in scr.get('lists') or [])} entries" if not bad else f"not screened: {', '.join(bad[:4])}"


@check
def matches_wait_for_a_person(case, profile, ctx):
    bad = [f["id"] for f in _own(case, ctx) if (f.get("data") or {}).get("screening", {}).get("entry") and not (f.get("data") or {})["screening"].get("decision") and not (f["status"] == "review" and f.get("requires_human"))]
    return not bad, "every possible match waits for a person; none was cleared by the system" if not bad else f"a match was settled without a person: {', '.join(bad)}"


@check
def declared_checks_are_answered(case, profile, ctx):
    cfg = profile.integrity
    types = {doc_type(d) for d in _docs(case)}
    want = [f"INT-{s['id']}" for s in cfg.get("sums") or [] if str(s.get("equals", "")).partition(".")[0] in types]
    got = {f["id"] for f in _own(case, ctx)}
    bad = [x for x in want if x not in got]
    return not bad, f"{len(want)} declared calculation(s), each with a finding" if not bad else f"no finding for: {', '.join(bad)}"


@check
def triage_is_complete(case, profile, ctx):
    t = _w(case).get("triage") or {}
    ok = t.get("priority") in ("urgent", "high", "normal", "low") and bool(t.get("queue")) and len(str(t.get("due") or "")) == 10 and bool(t.get("reasons"))
    return ok, f"priority {t.get('priority')}, queue '{t.get('queue_name')}', due {t.get('due')}, with its reasons" if ok else "the triage lacks a priority, a queue, a due date or its reasons"


# ---- promises of the on-demand agents (ctx carries what the call produced)
@check
def original_is_unchanged(case, profile, ctx):
    return ctx.get("before") == ctx.get("after"), "the case's own assessment is byte-for-byte what it was" if ctx.get("before") == ctx.get("after") else "the what-if altered the case"


@check
def draft_keeps_every_item(case, profile, ctx):
    out = ctx.get("output") or {}
    body = str(out.get("body") or "")
    lost = [i for i in out.get("items") or [] if str(i) not in body]
    return not lost, f"{len(out.get('items') or [])} item(s), all in the draft" if not lost else f"{len(lost)} item(s) missing from the draft"


@check
def nothing_changes_without_approval(case, profile, ctx):
    return ctx.get("overlay_before") == ctx.get("overlay_after") or bool(ctx.get("approved")), "the vocabulary is what it was" if not ctx.get("approved") else "changed by an approved proposal"


@check
def answer_is_grounded_or_declined(case, profile, ctx):
    out = ctx.get("output") or {}
    ok = bool(out.get("grounded") and (out.get("sources") or out.get("citations") or out.get("findings"))) or not out.get("grounded")
    return ok, "the answer cites its sources" if out.get("grounded") else "the assistant declined: the case does not hold the answer"


@check
def filing_is_recorded(case, profile, ctx):
    rec = case.get("filed") or ctx.get("output") or {}
    return bool(rec.get("repository") and rec.get("documents") is not None), "the filing record is on the case" if rec else "no filing record"


# ------------------------------------------------------------------------------------------------ loading
@lru_cache(maxsize=1)
def load() -> dict[str, dict[str, Any]]:
    raw = yaml.safe_load((HERE / "foundation" / "contracts.yaml").read_text(encoding="utf-8"))
    out: dict[str, dict[str, Any]] = {}
    for c in raw.get("contracts") or []:
        c = {"needs": [], "promises": [], "never": [], "writes": [], "reads": [], "grounding": [], "model": {"use": "none"}, "memory": {"read": [], "write": []}, "limits": {}, "on_failure": "stop", **c, "version": str(raw.get("version") or "1.0")}
        c["promises"] = [p if isinstance(p, dict) else {"check": str(p), "text": str(p).replace("_", " ")} for p in c["promises"]]
        for name in [*c["needs"], *(p["check"] for p in c["promises"])]:
            if name not in CHECKS:
                raise ValueError(f"contract '{c['id']}' names a check that does not exist: {name}")
        if c["on_failure"] not in ("stop", "degrade", "escalate") or (c["model"].get("use") or "none") not in ("none", "optional", "required"):
            raise ValueError(f"contract '{c['id']}': on_failure is stop, degrade or escalate; model.use is none, optional or required")
        out[c["id"]] = c
    return out


def get(agent_id: str) -> dict[str, Any]:
    contracts = load()
    if agent_id not in contracts:
        raise KeyError(f"no contract for agent '{agent_id}'")
    return contracts[agent_id]


def plain(c: dict[str, Any]) -> dict[str, Any]:
    """A contract as a screen shows it."""
    m = c["model"]
    spend = "No model. Code only." if (m.get("use") or "none") == "none" else (f"May use a model ({m.get('tier', 'balanced')} tier), up to {m.get('max_calls', 'any number of')} call(s)" +
                                                                              (f" and {int(m['max_tokens']):,} tokens" if m.get("max_tokens") else "") + " in one run" + (f"; {m['note']}" if m.get("note") else "") + ".")
    return {"id": c["id"], "version": c["version"], "purpose": c["purpose"], "needs": [{"check": n, "text": n.replace("_", " ").capitalize() + "."} for n in c["needs"]], "promises": c["promises"],
            "never": c["never"], "writes": c["writes"], "reads": c["reads"], "grounding": c["grounding"], "model": m, "spend": spend, "memory": c["memory"], "limits": c["limits"], "on_failure": c["on_failure"],
            "on_failure_text": {"stop": "The assessment stops.", "degrade": "The assessment carries on without this agent's contribution and says so.",
                                "escalate": "The assessment carries on and the case goes to a person."}[c["on_failure"]]}
