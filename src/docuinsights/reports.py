"""What leaves a case for people outside it: the decision record, the work-queue export, and the workspace's numbers.

    decision_record   one case as a single printable page: what was received, what each agent found and on which line,
                      what was recommended, who decided what and when, and whether the ledger behind it is intact
    insights          the workspace measured from its own case files: how many cases go straight through, how many need
                      a person and why, how long a decision takes, what is due
    cases_csv         the work queue as a spreadsheet opens it

Nothing here is estimated. Every figure is counted from the case files, and each says how it was counted.
"""
from __future__ import annotations

import csv
import io
from collections import Counter
from datetime import date, datetime, timedelta
from html import escape
from statistics import median
from typing import Any, Callable

from . import PRODUCT, __version__
from .assurance import due_state, mask, personal_names
from .casefile import current, live_docs, now
from .profiles import Profile

OPEN = ("fail", "review", "missing")
STATUS_WORD = {"pass": "Pass", "fail": "Fail", "review": "For a person", "missing": "Missing", "info": "Information"}


# ================================================================================================ the workspace's numbers
def _when(ts: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _pct(part: int, whole: int) -> float | None:
    return round(100 * part / whole, 1) if whole else None


def insights(cases: list[dict[str, Any]], profiles: dict[str, Profile], *, today: date | None = None, total: int | None = None) -> dict[str, Any]:
    today = today or date.today()
    assessed = [c for c in cases if c.get("assessment")]
    n = len(assessed)
    outcome = Counter((c["assessment"].get("recommendation") or {}).get("outcome") for c in assessed)
    decided = [c for c in cases if c.get("decision")]
    overrides = [c for c in decided if c["decision"].get("override")]
    hours = sorted(h for h in ((b - a).total_seconds() / 3600 for a, b in ((_when(c.get("created_at")), _when(c["decision"].get("at"))) for c in decided) if a and b) if h >= 0)
    assess_ms = [c["assessment"].get("metrics", {}).get("duration_ms") or 0 for c in assessed]
    reasons: Counter[tuple[str, str]] = Counter()
    by_agent: Counter[str] = Counter()
    scr = Counter()
    privacy: Counter[str] = Counter()
    fields = corrected = docs = pages = model_calls = runs = kept = 0
    open_cases, queues, priorities = [], Counter(), Counter()
    per: dict[str, Counter[str]] = {}
    days: dict[str, Counter[str]] = {}
    for c in cases:
        a = c.get("assessment") or {}
        rec = a.get("recommendation") or {}
        if a:
            per.setdefault(c["profile"], Counter())[rec.get("outcome") or "?"] += 1
            day = str(a.get("at") or "")[:10]
            if day:
                days.setdefault(day, Counter())[rec.get("outcome") or "?"] += 1
        seen_reasons: set[tuple[str, str]] = set()   # a check counts once for each case it stopped
        for f in a.get("findings") or []:
            if f.get("status") in OPEN and f.get("requires_human"):
                seen_reasons.add(((f.get("rule") or {}).get("title") or f.get("title") or f["id"], f.get("agent") or ""))
                by_agent[f.get("agent") or ""] += 1
        reasons.update(seen_reasons)
        s = a.get("screening") or {}
        scr["screened"] += len(s.get("screened") or [])
        for h in s.get("hits") or []:
            scr["hits"] += 1
            scr[{"clear": "cleared", "confirm": "confirmed"}.get(h.get("decision") or "", "waiting")] += 1
        scr["integrity"] += sum(1 for f in a.get("findings") or [] if f.get("agent") == "integrity" and f.get("status") in OPEN)
        for cat in (a.get("privacy") or {}).get("categories") or []:
            privacy[cat["name"]] += cat["count"]
        for d in live_docs(c):
            docs += 1
            pages += len(current(d).get("pages") or [])
            for f in (d.get("fields") or {}).values():
                if f.get("norm") is not None:
                    fields += 1
                    corrected += 1 if f.get("method") == "human" else 0
        model_calls += (a.get("metrics") or {}).get("model_calls") or 0
        runs += (a.get("contracts") or {}).get("runs") or 0
        kept += (a.get("contracts") or {}).get("kept") or 0
        if a and not c.get("decision"):
            t = a.get("triage") or {}
            st = due_state(t, today)
            open_cases.append({"reference": c["reference"], "profile": c["profile"], "label": rec.get("label"), "outcome": rec.get("outcome"), "priority": t.get("priority"), "queue": t.get("queue_name"),
                               "due": t.get("due"), **st, "assigned": (c.get("assigned") or {}).get("to")})
            queues[t.get("queue_name") or "Not triaged"] += 1
            priorities[t.get("priority") or "none"] += 1
    recent = [(today - timedelta(days=i)).isoformat() for i in range(13, -1, -1)]
    order = {"urgent": 0, "high": 1, "normal": 2, "low": 3}
    open_cases.sort(key=lambda r: (order.get(r["priority"] or "", 4), r.get("days_left") if r.get("days_left") is not None else 999, r["reference"]))
    return {
        "as_of": now(), "cases": len(cases), "of_total": total if total is not None else len(cases), "assessed": n, "decided": len(decided), "open": len(open_cases),
        "outcomes": {k: outcome.get(k, 0) for k in ("proceed", "request_evidence", "review")},
        "rates": {
            "straight_through": {"value": _pct(outcome.get("proceed", 0), n), "of": n, "how": "Assessed cases whose recommendation is to proceed, as a share of all assessed cases."},
            "exception": {"value": _pct(outcome.get("review", 0), n), "of": n, "how": "Assessed cases referred to a person because a check failed or needs judgement."},
            "evidence_request": {"value": _pct(outcome.get("request_evidence", 0), n), "of": n, "how": "Assessed cases for which something has to be requested."},
            "override": {"value": _pct(len(overrides), len(decided)), "of": len(decided), "how": "Decisions that differ from the recommendation, as a share of all decisions."},
            "corrected_values": {"value": _pct(corrected, fields), "of": fields, "how": "Extracted values a reviewer changed, as a share of all values on current documents. The complement is extraction accuracy as reviewers saw it."},
            "contracts_kept": {"value": _pct(kept, runs), "of": runs, "how": "Agent runs that kept every line of their contract."},
        },
        "time": {"to_decision_hours": {"median": round(median(hours), 1) if hours else None, "longest": round(hours[-1], 1) if hours else None, "of": len(hours),
                                       "how": "From the registration of a case to its decision, for decided cases."},
                 "assessment_seconds": {"median": round(median(assess_ms) / 1000, 2) if assess_ms else None, "of": len(assess_ms), "how": "The time the agents took on a case, added up over its stages."}},
        "volume": {"documents": docs, "pages": pages, "values": fields, "model_calls": model_calls},
        "service": {"overdue": sum(1 for r in open_cases if r.get("overdue")), "due_soon": sum(1 for r in open_cases if r.get("due_soon")),
                    "on_track": sum(1 for r in open_cases if r.get("days_left") is not None and r["days_left"] > 1), "queues": [{"queue": q, "cases": k} for q, k in queues.most_common()],
                    "priorities": {p: priorities.get(p, 0) for p in ("urgent", "high", "normal", "low")}, "open_cases": open_cases[:50],
                    "how": "Open cases are assessed cases without a decision. Each is set against its own due date as of today."},
        "reasons": [{"title": t, "agent": a, "cases": k} for (t, a), k in reasons.most_common(12)],
        "by_agent": [{"agent": a, "findings": k} for a, k in by_agent.most_common()],
        "assurance": {"names_screened": scr["screened"], "watchlist_matches": scr["hits"], "cleared": scr["cleared"], "confirmed": scr["confirmed"], "waiting": scr["waiting"], "integrity_alerts": scr["integrity"],
                      "personal_data": [{"kind": k, "items": v} for k, v in privacy.most_common()]},
        "by_use_case": [{"id": pid, "name": profiles[pid].name if pid in profiles else pid, "cases": sum(cnt.values()), **{k: cnt.get(k, 0) for k in ("proceed", "request_evidence", "review")},
                         "straight_through": _pct(cnt.get("proceed", 0), sum(cnt.values()))} for pid, cnt in sorted(per.items(), key=lambda kv: -sum(kv[1].values()))],
        "daily": [{"day": d, **{k: days.get(d, Counter()).get(k, 0) for k in ("proceed", "request_evidence", "review")}} for d in recent],
    }


# ================================================================================================ the work queue as a spreadsheet
CSV_COLUMNS = ["reference", "use_case", "status", "recommendation", "priority", "queue", "due", "days_left", "assigned_to", "documents", "open_findings", "watchlist_matches", "integrity_alerts",
               "created_at", "assessed_at", "decision", "decided_by", "decided_at"]


def _cell(value: Any) -> Any:
    """A cell a spreadsheet will show and never run: text that starts like a formula is given a leading apostrophe."""
    s = "" if value is None else str(value)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") and not s.lstrip("-").replace(".", "", 1).isdigit() else s


def cases_csv(rows: list[dict[str, Any]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(CSV_COLUMNS)
    for r in rows:
        w.writerow([_cell(r.get(k)) for k in CSV_COLUMNS])
    return buf.getvalue()


# ================================================================================================ the decision record
CSS = """
:root{--red:#c8102e;--ink:#1a1a1f;--muted:#5f6470;--line:#e4e6eb;--wash:#faf7f7}
*{box-sizing:border-box}body{margin:0;font:13.5px/1.5 "Segoe UI",system-ui,-apple-system,"Helvetica Neue",Arial,sans-serif;color:var(--ink);background:#f3f3f5}
.page{max-width:920px;margin:24px auto;background:#fff;border:1px solid var(--line);border-radius:10px;overflow:hidden}
header{padding:26px 34px 22px;border-top:6px solid var(--red);border-bottom:1px solid var(--line)}
header .k{font-size:13px;color:var(--red);font-weight:700}
h1{font-size:25px;margin:4px 0 2px;font-weight:650;letter-spacing:-.2px}h2{font-size:15px;margin:0 0 10px;font-weight:680;padding-bottom:6px;border-bottom:2px solid var(--ink)}
h3{font-size:13.5px;margin:0 0 2px}section{padding:20px 34px;border-bottom:1px solid var(--line)}section:last-of-type{border-bottom:0}
.meta{color:var(--muted);font-size:12.5px}.mono{font-family:ui-monospace,"Cascadia Mono",Menlo,Consolas,monospace;font-size:12px}
table{width:100%;border-collapse:collapse;font-size:12.5px}th{text-align:left;font-size:11.5px;font-weight:600;color:var(--muted);padding:5px 8px;border-bottom:1px solid var(--line)}
td{padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}tr:last-child td{border-bottom:0}
.out{border:1px solid var(--line);border-left:5px solid var(--ink);border-radius:8px;padding:14px 16px;background:var(--wash)}.out.proceed{border-left-color:#1b7f52}.out.request_evidence{border-left-color:#a86510}.out.review{border-left-color:var(--red)}
.out b.l{font-size:18px}.pill{display:inline-block;font-size:11px;font-weight:700;padding:1px 8px;border-radius:99px;border:1px solid currentColor;white-space:nowrap}
.s-pass{color:#1b7f52}.s-fail,.s-missing{color:var(--red)}.s-review{color:#a86510}.s-info{color:#4b5567}
.f{padding:10px 0;border-bottom:1px solid var(--line);break-inside:avoid}.f:last-child{border-bottom:0}.f .t{display:flex;gap:10px;align-items:baseline}.f .t b{flex:1}
.f ul{margin:4px 0 0;padding-left:18px;color:#3b4050;font-size:12.5px}.src{font-size:12px;color:#3b4050;margin-top:3px}.src q{font-family:ui-monospace,Menlo,Consolas,monospace;quotes:none;color:#1a1a1f}
dl{display:grid;grid-template-columns:190px 1fr;gap:3px 12px;margin:0}dt{color:var(--muted)}dd{margin:0}
.tag{display:inline-block;font-size:11px;padding:1px 8px;border-radius:99px;background:#f1f1f4;color:#3b4050;margin-left:6px}.tag.red{background:#fde8ec;color:var(--red)}
footer{padding:16px 34px 22px;color:var(--muted);font-size:11.5px}
.bar{padding:10px 34px;background:#fff;border-bottom:1px solid var(--line);display:flex;gap:10px;align-items:center}.bar button{font:inherit;border:1px solid var(--red);background:var(--red);color:#fff;border-radius:7px;padding:5px 14px;cursor:pointer}
@media print{body{background:#fff}.page{margin:0;border:0;max-width:none}.bar{display:none}section{padding:14px 0}header{padding:0 0 14px}footer{padding:10px 0}}
"""


class _Html(str):
    """Markup built here, which is written into the page as it is. Everything else is escaped."""


def decision_record(case: dict[str, Any], profile: Profile, *, ledger: dict[str, Any] | None = None, entries: list[dict[str, Any]] | None = None, masked: bool = False, by: str = "") -> str:
    """The case as one printable page. With `masked`, personal data is masked throughout (for a copy that leaves the organisation)."""
    a = case.get("assessment") or {}
    rec = a.get("recommendation") or {}
    names = personal_names(case, profile) if masked else []
    hide: Callable[[Any], str] = (lambda v: mask(v, names)) if masked else (lambda v: "" if v is None else str(v))

    def e(v: Any) -> str:
        return escape(hide(v))

    def kv(pairs: list[tuple[str, Any]]) -> str:
        return "<dl>" + "".join(f"<dt>{escape(str(k))}</dt><dd>{v if isinstance(v, _Html) else e(v)}</dd>" for k, v in pairs if v not in (None, "", [])) + "</dl>"

    def pill(status: str) -> str:
        return f'<span class="pill s-{escape(status)}">{escape(STATUS_WORD.get(status, status))}</span>'

    out = [f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Decision record {escape(case["reference"])}</title>'
           f"<style>{CSS}</style></head><body><div class=\"page\">",
           '<div class="bar"><button onclick="window.print()">Print or save as PDF</button><span class="meta">This page is self-contained: it can be saved, printed or filed as it is.</span></div>',
           f'<header><div class="k">{escape(PRODUCT)} decision record</div><h1>{escape(case["reference"])}'
           + ('<span class="tag red">Personal data masked</span>' if masked else "") + "</h1>"
           f'<div class="meta">{escape(profile.name)}, profile v{escape(str(a.get("profile_version") or profile.version))}. Generated {escape(now())}' + (f" by {escape(by)}" if by else "")
           + f' with {escape(PRODUCT)} {escape(__version__)}.</div></header>']

    # ---- the outcome and the decision
    out.append("<section><h2>Outcome</h2>")
    if rec:
        out.append(f'<div class="out {escape(str(rec.get("outcome")))}"><div class="meta">Recommendation of assessment {escape(str(a.get("id")))}, {escape(str(a.get("at")))}</div><b class="l">{e(rec.get("label"))}</b>'
                   f'<div>{e(rec.get("description"))}</div>')
        for title, items in (("Reasons", rec.get("reasons")), ("To request", rec.get("requests")), ("Caveats", rec.get("caveats"))):
            if items:
                out.append(f"<h3 style='margin-top:8px'>{title}</h3><ul style='margin:2px 0 0;padding-left:18px'>" + "".join(f"<li>{e(x)}</li>" for x in items) + "</ul>")
        out.append(f'<div class="meta" style="margin-top:8px">{e(rec.get("decided_by"))}. The recommendation is advice: a person decides.</div></div>')
    else:
        out.append('<p class="meta">This case has not been assessed.</p>')
    d = case.get("decision")
    if d:
        word = {"approve": "Approved", "request_evidence": "Evidence requested", "escalate": "Escalated", "reject": "Rejected"}.get(d["decision"], d["decision"])
        out.append("<h3 style='margin-top:14px'>Decision</h3>" + kv([("Decision", _Html(f"<b>{escape(word)}</b>" + (' <span class="tag red">differs from the recommendation</span>' if d.get("override") else ""))),
                                                                     ("Decided by", d.get("by")), ("On", d.get("at")), ("On assessment", d.get("assessment")), ("Note", d.get("note"))]))
    else:
        out.append('<p class="meta" style="margin-top:10px">No decision has been recorded yet.</p>')
    for g in case.get("earlier_decisions") or []:
        out.append(f'<p class="meta" style="margin-top:6px">Earlier decision, on assessment {e(g.get("assessment"))}: {e(g.get("decision"))} by {e(g.get("by"))} on {e(g.get("at"))}'
                   + (f'. Note: {e(g.get("note"))}' if g.get("note") else "") + ". Superseded when the case was assessed again.</p>")
    out.append("</section>")

    # ---- triage
    t = a.get("triage")
    if t:
        st = due_state(t)
        unit = ("working " if t.get("clock") == "working" else "") + "day(s)"
        due = f'{t.get("due")} (' + (f'overdue by {-st["days_left"]} {unit}' if st.get("overdue") else f'{st.get("days_left")} {unit} left') + ")" if st else t.get("due")
        out.append("<section><h2>Triage</h2>" + kv([("Priority", str(t.get("priority", "")).capitalize()), ("Queue", t.get("queue_name")), ("Due", due if not d else t.get("due")),
                                                     ("Assigned to", (case.get("assigned") or {}).get("to"))])
                   + "<ul style='margin:8px 0 0;padding-left:18px;font-size:12.5px;color:#3b4050'>" + "".join(f"<li>{e(x)}</li>" for x in t.get("reasons") or []) + "</ul></section>")

    # ---- what was received
    fields = [(profile.field_name(f"case.{k}"), v) for k, v in (case.get("fields") or {}).items()]
    out.append("<section><h2>What was received</h2>" + kv([("Registered", case.get("created_at")), ("Status", case.get("status")), *fields]))
    rows = []
    for doc in live_docs(case):
        ver = current(doc)
        kind = profile.type_name(doc.get("effective_type") or (doc.get("classification") or {}).get("type"))
        rows.append(f'<tr><td>{e(doc["name"])}</td><td>{escape(kind)}</td><td>v{doc["current"]}</td><td>{len(ver.get("pages") or [])}</td><td>{escape(str(ver.get("method") or ""))}</td>'
                    f'<td class="mono">{escape(str(ver.get("sha256") or "")[:16])}</td><td>{escape(str(ver.get("uploaded_at") or ""))}</td></tr>')
    out.append("<table style='margin-top:12px'><thead><tr><th>Document</th><th>Read as</th><th>Version</th><th>Pages</th><th>How read</th><th>SHA-256</th><th>Received</th></tr></thead><tbody>" + "".join(rows)
               + "</tbody></table></section>")

    # ---- the findings
    found = a.get("findings") or []
    if found:
        rank = {"fail": 0, "missing": 1, "review": 2, "pass": 3, "info": 4}
        counts = Counter(f["status"] for f in found)
        out.append("<section><h2>Findings</h2><div class='meta'>" + " · ".join(f"{counts[s]} {STATUS_WORD[s].lower()}" for s in ("fail", "missing", "review", "pass", "info") if counts.get(s)) + "</div>")
        for f in sorted(found, key=lambda x: (rank.get(x["status"], 9), x["id"])):
            out.append(f'<div class="f"><div class="t"><b>{e(f["title"])}</b><span class="mono meta">{escape(f["id"])}</span>{pill(f["status"])}</div><div>{e(f.get("message"))}</div>')
            rule = f.get("rule") or {}
            bits = [f"Agent: {escape(str(f.get('agent')))}"] + ([f"Rule {escape(str(rule.get('id')))} v{escape(str(rule.get('version')))}"] if rule else [])
            bits += [f"Sources verified: {f['verifier'].get('verified')} of {f['verifier'].get('sources')}"] if f.get("verifier") and f["verifier"].get("sources") else []
            out.append(f'<div class="meta">{" · ".join(bits)}</div>')
            for b in f.get("basis") or []:
                out.append(f'<div class="src">Rests on: {e(b.get("name"))}' + (f' <span class="meta">({e(b.get("citation"))})</span>' if b.get("citation") else "") + "</div>")
            for s in (f.get("sources") or [])[:8]:
                where = f"p.{s.get('page')}" + (f" l.{s.get('line')}" if s.get("line") else "") if s.get("page") else ""
                out.append(f'<div class="src">{e(s.get("doc_name"))} {escape(where)} · {e(s.get("field_name"))}' + (f": <q>{e(s.get('snippet'))}</q>" if s.get("snippet") else "") + "</div>")
            if f.get("status") in OPEN and f.get("reasoning"):
                out.append("<ul>" + "".join(f"<li>{e(x)}</li>" for x in f["reasoning"][:10]) + "</ul>")
            if f.get("human"):
                h = f["human"]
                out.append(f'<div class="src"><b>Reviewer:</b> {escape(str(h.get("decision")))} by {e(h.get("by"))} on {escape(str(h.get("at")))}' + (f": {e(h.get('note'))}" if h.get("note") else "") + "</div>")
            out.append("</div>")
        out.append("</section>")

    # ---- screening and personal data
    s = a.get("screening")
    if s and s.get("enabled", True):
        lists = "; ".join(f"{x['name']} {x.get('version') or ''}".strip() + f" ({x.get('entries')} entries, published {x.get('published') or 'not stated'}, sha256 {x.get('sha256')})"
                          + (" — invented entries, for demonstration" if x.get("demonstration") else "") for x in s.get("lists") or [])
        out.append("<section><h2>Screening</h2>" + kv([("Lists", lists or "none loaded"), ("Names screened", "; ".join(f"{x['name']} ({x['what']})" for x in s.get("screened") or []) or "none"),
                                                        ("Match threshold", f"{float(s.get('threshold') or 0):.0%}")]))
        if s.get("hits"):
            out.append("<table style='margin-top:10px'><thead><tr><th>Name in the case</th><th>Listed as</th><th>List</th><th>How</th><th>Disposition</th></tr></thead><tbody>"
                       + "".join(f"<tr><td>{e(h['subject'])}</td><td>{e(h['entry'])}</td><td>{e(h['list'])}</td><td>{escape(h['how'])} ({float(h['score']):.0%})</td>"
                                 f"<td>{escape({'clear': 'Cleared by a person', 'confirm': 'Confirmed by a person'}.get(h.get('decision') or '', 'Waiting for a person'))}</td></tr>" for h in s["hits"]) + "</tbody></table>")
        else:
            out.append('<p class="meta" style="margin-top:8px">No name matched.</p>')
        out.append("</section>")
    p = a.get("privacy")
    if p:
        out.append("<section><h2>Personal data</h2>" + (kv([(c["name"], f"{c['count']} in {len(c['documents'])} document(s)") for c in p.get("categories") or []]) if p.get("categories")
                                                         else '<p class="meta">None found by the patterns.</p>') + f'<p class="meta" style="margin-top:8px">{escape(p.get("masking") or "")}</p></section>')

    # ---- who did what
    runs = a.get("runs") or []
    if runs:
        ct = a.get("contracts") or {}
        out.append(f"<section><h2>Agents</h2><div class='meta'>{ct.get('kept')} of {ct.get('runs')} runs kept their contract (contracts v{escape(str(ct.get('version')))}). Model: {e((a.get('model') or {}).get('model') or 'none')}.</div>"
                   "<table style='margin-top:8px'><thead><tr><th>Agent</th><th>Result</th><th>Contract</th><th>Model calls</th><th>Time</th></tr></thead><tbody>"
                   + "".join(f"<tr><td>{escape(r['name'])}</td><td>{e(r.get('summary'))}</td><td>{'kept' if (r.get('contract') or {}).get('kept', True) else '<b class="s-fail">not kept</b>'}</td>"
                             f"<td>{r.get('model_calls', 0)}</td><td>{r.get('duration_ms')} ms</td></tr>" for r in runs) + "</tbody></table></section>")
    if case.get("reviews"):
        out.append("<section><h2>Reviews by people</h2><table><thead><tr><th>When</th><th>Who</th><th>What</th><th>Decision</th><th>Reason</th></tr></thead><tbody>"
                   + "".join(f"<tr><td>{escape(str(r.get('at')))}</td><td>{e(r.get('by'))}</td><td>{escape(str(r.get('target')))} {e(r.get('doc_name') or r.get('finding') or '')}</td>"
                             f"<td>{e(r.get('decision'))}" + (f": {e(r.get('value'))}" if r.get("value") and r.get("value") != r.get("decision") else "") + f"</td><td>{e(r.get('reason'))}</td></tr>" for r in case["reviews"])
                   + "</tbody></table></section>")
    out.append("<section><h2>Audit trail</h2><table><thead><tr><th>When</th><th>Who</th><th>Event</th><th>Detail</th></tr></thead><tbody>"
               + "".join(f"<tr><td class='mono'>{escape(str(ev.get('at')))}</td><td>{e(ev.get('by'))}</td><td class='mono'>{escape(str(ev.get('kind')))}</td><td>{e(ev.get('detail'))}</td></tr>" for ev in case.get("events") or [])
               + "</tbody></table></section>")
    if ledger is not None:
        ok = ledger.get("ok")
        out.append("<section><h2>Provenance</h2>" + kv([("Ledger", _Html(f"<b class=\"{'s-pass' if ok else 's-fail'}\">{'Intact' if ok else 'BROKEN at entry ' + escape(str(ledger.get('broken_at')))}</b> · {int(ledger.get('entries') or 0)} entries in the workspace's chain")),
                                                         ("Head of the chain", _Html(f"<span class='mono'>{escape(str(ledger.get('head')))}</span>")), ("Entries for this case", len(entries or [])), ("Checked", ledger.get("checked_at"))])
                   + '<p class="meta" style="margin-top:8px">Every agent run, review and decision is one entry, and each entry carries the fingerprint of the one before it. A change to any earlier entry breaks the chain from there on.</p></section>')
    out.append(f"<footer>{escape(PRODUCT)} {escape(__version__)} · A record of what the system did and what a person decided. Rules and the recommendation are code; no model takes part in the outcome."
               + (" Personal data in this copy is masked; the documents themselves are unchanged." if masked else "") + "</footer></div></body></html>")
    return "".join(out)
