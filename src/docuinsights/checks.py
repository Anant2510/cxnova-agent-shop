"""The checking agents: evidence completeness, reference reasoning, policy rules, contradictions, verification.

Everything here is code. A model may have read the documents; it never performs a date calculation, a value
comparison, an evidence decision or the recommendation. Each check returns findings that carry their reasoning steps,
the document lines they rest on, the rule and its version, and (for reference checks) the address and date retrieved.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from . import graphs as G
from . import reference as R
from . import values as V
from .casefile import aid, assertions, case_value, docs_of, doc_type, finding, live_docs
from .profiles import Profile


# ------------------------------------------------------------------------------------------------ evidence completeness
def evidence(case: dict[str, Any], profile: Profile) -> dict[str, Any]:
    docs = live_docs(case)
    out = []
    for ev in profile.evidence:
        applies = ev.get("required") == "always" or (ev.get("required") == "when" and V.truthy(case["fields"].get(str(ev.get("when", "")).partition(".")[2])))
        names = " or ".join(profile.type_name(t) for t in ev["any_of"])
        have = [d for t in ev["any_of"] for d in docs_of(case, t)]
        rule = {"id": f"evidence.{ev['id']}", "version": profile.version, "title": f"{ev['title']} " + ("is required" if ev.get("required") == "always" else "is required when it applies"),
                "note": ev.get("note") or ""}
        if have:
            best = max(have, key=lambda d: (d.get("classification") or {}).get("confidence") or 0)
            cls = best.get("classification") or {}
            sure = best.get("effective_type") or (cls.get("confidence") or 0) >= 0.6 or best.get("assumed")
            srcs = [{"doc": d["id"], "doc_name": d["name"], "type": doc_type(d), "version": d["current"], "field": "classification", "field_name": "Document type",
                     "value": profile.type_name(doc_type(d)), "page": 1, "line": None, "snippet": (d.get("classification") or {}).get("rationale") or "", "verified": True,
                     "confidence": 1.0 if d.get("effective_type") or d.get("assumed") else (d.get("classification") or {}).get("confidence") or 0,
                     "method": "assumed" if d.get("assumed") else ("human" if d.get("effective_type") else (d.get("classification") or {}).get("method"))} for d in have]
            out.append(finding(f"EV-{ev['id']}", "evidence", ev["title"], "pass" if sure else "review",
                               f"{names} present: {', '.join(d['name'] for d in have)}" + ("" if sure else " (classification is uncertain: confirm the document type)"),
                               agent="evidence", severity="low" if sure else "medium", sources=srcs, path=["case", *[d["id"] for d in have]], rule=rule,
                               reasoning=[f"Looked for a document classified as {names} among {len(docs)} document(s): found {len(have)}."],
                               data={"requirement": ev["id"], "applies": applies}))
            continue
        steps = [f"Looked for a document classified as {names} among {len(docs)} document(s): none."]
        phrases = [s for t in ev["any_of"] for s in (profile.types[t].get("signals") or [])[:2]]
        mentions = [d for d in docs if any(p.lower() in (d["versions"][d["current"] - 1].get("text") or "").lower() for p in phrases)]
        steps.append("Searched the text of every document for " + ", ".join(f"'{p}'" for p in phrases) + ": "
                     + (", ".join(f"mentioned in {d['name']} ({profile.type_name(doc_type(d))}), which refers to it but is not one" for d in mentions) if mentions else "not found") + ".")
        unread = [d for d in docs if not (d["versions"][d["current"] - 1].get("text") or "").strip()]   # a file nobody could read may be the very document
        labelled = [d for d in unread if d.get("category") in ev["any_of"]]
        for d in docs:
            if d in unread:
                steps.append(f"{d['name']} could not be read ({d['versions'][d['current'] - 1].get('note') or 'no text in it'})"
                             + (f"; it was uploaded under '{profile.type_name(d['category'])}'." if d in labelled else "."))
            elif d.get("category") in ev["any_of"]:
                steps.append(f"{d['name']} was uploaded under '{profile.type_name(d['category'])}' but reads as {profile.type_name(doc_type(d))}.")
        if applies:
            steps.append(f"Conclusion: {ev['title']} is missing. {ev.get('note') or ''}".strip())
            noun = profile.brand.get("case_noun", "case")
            if labelled:
                files = ", ".join(d["name"] for d in labelled)
                message, request = f"{names} not provided in a form that can be read: {files} was uploaded as one, and could not be read", f"{names} for this {noun}: a readable copy ({files} could not be read)"
            elif unread:
                files = ", ".join(d["name"] for d in unread[:3])
                message, request = f"{names} not provided; {files} could not be read and may be it", f"{names} for this {noun} (or a readable copy of {files})"
            else:
                message, request = f"{names} not provided", f"{names} for this {noun}"
            out.append(finding(f"EV-{ev['id']}", "evidence", ev["title"], "missing", message, agent="evidence", severity="high", reasoning=steps,
                               path=["case"], rule=rule, data={"requirement": ev["id"], "applies": True, "request": request}))
        else:
            steps.append("Conclusion: not required for this case.")
            out.append(finding(f"EV-{ev['id']}", "evidence", ev["title"], "info", f"{names} not provided and not required for this case", agent="evidence", severity="low",
                               reasoning=steps, path=["case"], rule=rule, requires_human=False, data={"requirement": ev["id"], "applies": False}))
    missing = [f for f in out if f["status"] == "missing"]
    return {"findings": out, "summary": f"{len(profile.evidence)} evidence requirement(s) checked: {sum(1 for f in out if f['status'] == 'pass')} met, {len(missing)} missing"
            + (" (" + ", ".join(f["title"] for f in missing) + ")" if missing else "")}


# ------------------------------------------------------------------------------------------------ reference reasoning
def references(case: dict[str, Any], profile: Profile, entities: list[dict[str, Any]], graph: dict[str, Any], *, fetch: Any = None) -> dict[str, Any]:
    records, out, calls = [], [], []
    for src in profile.references:
        head, _, fld = str(src.get("as_of") or "").partition(".")
        as_of = case_value(case, profile, fld)[0] if head == "case" else None
        rec = R.retrieve(src, as_of, fetch=fetch)
        calls += rec.pop("tool_calls", [])
        cite = {"source": src["id"], "name": rec["name"], "url": rec.get("url"), "as_of": as_of, "retrieved_at": rec["retrieved_at"], "mode": rec["mode"],
                "version": rec.get("version"), "result_hash": rec.get("result_hash", "")[:16]}
        out.append(finding(f"REF-{src['id']}-retrieval", "reference", f"{rec['name']}: retrieved", "info",
                           (f"Read live for {as_of}." if rec["mode"] == "live" else f"Read from a bundled snapshot ({rec.get('version')}), not live: confirm against the source before deciding."),
                           agent="reference", severity="low", reference=cite, requires_human=rec["mode"] != "live",
                           reasoning=[f"Requested {rec['name']} as of {as_of or 'no date given'}.", f"Address: {rec.get('url')}", f"Retrieved {rec['retrieved_at']} ({rec['mode']}); content hash {cite['result_hash']}.",
                                      rec.get("note") or ""] + ([f"The live call failed ({rec['live_error']}), so the snapshot was used."] if rec.get("live_error") else []),
                           data={"lines": len(rec.get("nodes") or []), "partial": bool(rec.get("partial"))}))
        goods = next((e for e in entities if e["type"] == "GoodsItem" and e.get("characteristics")), None)
        chars = (goods or {}).get("characteristics") or {}
        result = R.classify(rec.get("nodes") or [], src.get("aids") or {}, chars)
        by_code = {n["code"]: n for n in rec.get("nodes") or []}
        for code in result.get("path") or []:   # the reference lines on the path join the graph
            n = by_code[code]
            G.add_node(graph, f"ref:{code}", "ReferenceLine", f"{n['display']} {n['description'][:60]}", "reference", source=src["id"], as_of=as_of or "", mode=rec["mode"])
            if n.get("parent") and f"ref:{n['parent']}" in graph["index"]:
                G.add_edge(graph, f"ref:{code}", f"ref:{n['parent']}", "CHILD_OF")
        declared = []
        for ref in src.get("declared_codes") or []:
            for a in assertions(case, profile, ref):
                code = (str(a["norm"]) + "0000000000")[:10]
                rel = ("equal" if result.get("status") == "supported" and code == result.get("code") else
                       "listed" if code in by_code else "same heading, not among the lines held" if code.startswith(str(src.get("heading") or "~")) else "outside this heading")
                declared.append({**a, "code": code, "relation": rel})
        srcs = [a for fid in chars for a in assertions(case, profile, f"{src.get('characteristics_from')}.{fid}")] if src.get("characteristics_from") else []
        path = ([goods["id"]] if goods else []) + [f"{goods['id']}.{f}" for f in chars if goods] + [f"ref:{c}" for c in result.get("path") or []]
        title = f"{rec['name']}: classification the evidence supports"
        if result["status"] == "supported":
            if goods:
                G.add_edge(graph, goods["id"], f"ref:{result['code']}", "CANDIDATE_CLASSIFICATION")
            out.append(finding(f"REF-{src['id']}-classification", "classification", title, "pass",
                               f"The product evidence supports {result['display']} ({result['description']}).", agent="reference", severity="medium", reasoning=result["steps"],
                               sources=srcs, path=path, reference=cite, requires_human=rec["mode"] != "live" or bool(rec.get("partial")),
                               data={"candidate": result["code"], "display": result["display"], "measures": result.get("measures") or {}}))
        elif result["status"] == "insufficient":
            names = [chars.get(f, {}).get("name") or (profile.field(src.get("characteristics_from") or "", f) or {}).get("name") or f.replace("_", " ") for f in result["missing"]]
            out.append(finding(f"REF-{src['id']}-classification", "sufficiency", title, "review",
                               "The product evidence does not decide between " + ", ".join(a["display"] for a in result["alternatives"]) + ": " + ", ".join(names) + " not established.",
                               agent="reference", severity="high", reasoning=result["steps"] + ["Conclusion: no classification is returned. Targeted product evidence is requested instead."],
                               sources=srcs, path=path, reference=cite,
                               data={"alternatives": result["alternatives"], "missing": result["missing"], "request": "Product evidence stating " + ", ".join(names)}))
        else:
            out.append(finding(f"REF-{src['id']}-classification", "sufficiency", title, "review" if chars or rec.get("nodes") else "info",
                               "No product evidence to reason from." if result["status"] == "none" else "The reference held here has no line that fits the product evidence.",
                               agent="reference", severity="medium", reasoning=result["steps"], sources=srcs, path=path, reference=cite,
                               data={"request": "Product evidence (a technical data sheet or specification)"} if result["status"] == "none" else {}))
        records.append({"id": src["id"], "retrieval": {k: v for k, v in rec.items() if k != "nodes"}, "lines": rec.get("nodes") or [], "result": result,
                        "declared": [{"ref": d["ref"], "doc": d["doc"], "doc_name": d["doc_name"], "code": d["code"], "relation": d["relation"]} for d in declared],
                        "characteristics": chars, "citation": cite})
    return {"records": records, "findings": out, "tool_calls": calls,
            "summary": "; ".join(f"{r['retrieval']['name']} read {r['retrieval']['mode']} for {r['retrieval'].get('requested_as_of')}: "
                                 + (f"candidate {r['result'].get('display')}" if r["result"]["status"] == "supported" else r["result"]["status"]) for r in records) or "no reference source in this profile"}


# ------------------------------------------------------------------------------------------------ policy rules
def _days(a: str, b: str) -> int:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def _span(days: int) -> str:
    y, rest = divmod(abs(days), 365)
    m, d = divmod(rest, 30)
    return f"{y} year(s), {m} month(s), {d} day(s)"


def policy(case: dict[str, Any], profile: Profile, refs: list[dict[str, Any]]) -> dict[str, Any]:
    out = []
    for r in profile.rules:
        rule = {"id": r["id"], "version": str(r.get("version") or profile.version), "title": r.get("title") or r["id"], "note": r.get("note") or "", "type": r["type"]}
        kw = {"agent": "policy", "rule": rule}
        fid, fam, title = f"RULE-{r['id']}", r.get("family") or "policy", rule["title"]

        def need(ref: str) -> list[dict[str, Any]]:
            return assertions(case, profile, ref)

        def path_of(srcs: list[dict[str, Any]]) -> list[str]:
            return ["case" if s["doc"] == "case" else aid(s["doc"], s["field"]) for s in srcs]

        if r["type"] in ("date_interval", "date_order"):
            a_ref, b_ref = (r["from"], r["to"]) if r["type"] == "date_interval" else (r["first"], r["then"])
            a, b = need(a_ref), need(b_ref)
            if not a or not b:
                lacking = [profile.field_name(x) for x, got in ((a_ref, a), (b_ref, b)) if not got]
                out.append(finding(fid, fam, title, "review", "Cannot be evaluated: " + ", ".join(lacking) + " not available.", sources=a + b, path=path_of(a + b),
                                   reasoning=[f"{x} is needed and was not found." for x in lacking], **kw))
                continue
            days = _days(a[0]["norm"], b[0]["norm"])
            steps = [f"{a[0]['field_name']}: {a[0]['norm']} (from {a[0]['doc_name']}).", f"{b[0]['field_name']}: {b[0]['norm']} (from {b[0]['doc_name']}).",
                     f"Calculated interval: {days} day(s) ({_span(days)})."]
            if r["type"] == "date_interval":
                limit = int(r["max_days"])
                ok = 0 <= days <= limit
                steps += [f"Rule {r['id']} v{rule['version']}: the interval must be between 0 and {limit} days.", f"Comparison: {days} {'<=' if days <= limit else '>'} {limit}" + (" and not negative." if days >= 0 else "; the interval is negative."),
                          f"Conclusion: {'pass' if ok else 'fail'}."]
                msg = f"{days} day(s) from {a[0]['norm']} to {b[0]['norm']}; the limit is {limit} days."
            else:
                ok = days >= 0
                steps += [f"Rule {r['id']} v{rule['version']}: the first date must not be after the second.", f"Conclusion: {'pass' if ok else 'fail'}."]
                msg = f"{a[0]['field_name']} {a[0]['norm']} is {'not after' if ok else 'after'} {b[0]['field_name']} {b[0]['norm']}."
            out.append(finding(fid, fam, title, "pass" if ok else "fail", msg, severity="high", reasoning=steps, sources=[a[0], b[0]], path=path_of([a[0], b[0]]),
                               data={"days": days}, **kw))
        elif r["type"] in ("amounts_match", "amount_limit"):
            refs_ = r["amounts"] if r["type"] == "amounts_match" else [r["amount"], r["limit"]]
            got = [(ref, need(ref)) for ref in refs_]
            have = [a for _, rows in got for a in rows]
            absent = [profile.field_name(ref) for ref, rows in got if not rows]
            if len(have) < 2:
                out.append(finding(fid, fam, title, "review", "Cannot be evaluated: fewer than two of the values are available (" + ", ".join(absent) + " absent).",
                                   sources=have, path=path_of(have), reasoning=[f"{x} was not found." for x in absent], **kw))
                continue
            tol = float(r.get("tolerance_pct") or 0)
            steps = [f"{a['field_name']}: {V.display(a['norm'])} (from {a['doc_name']})." for a in have]
            currencies = {a["norm"]["currency"] for a in have if a["norm"].get("currency")}
            base = have[-1] if r["type"] == "amount_limit" else have[0]
            worst = 0.0
            ok = len(currencies) <= 1
            if not ok:
                steps.append("The values are in different currencies (" + ", ".join(sorted(currencies)) + "): they cannot be compared without an approved rate.")
            for a in have:
                if a is base:
                    continue
                diff = (a["norm"]["amount"] - base["norm"]["amount"]) / base["norm"]["amount"] * 100 if base["norm"]["amount"] else 0.0
                within = diff <= tol if r["type"] == "amount_limit" else abs(diff) <= tol
                worst = max(worst, abs(diff))
                ok = ok and within
                steps.append(f"{a['field_name']} differs from {base['field_name']} by {diff:+.2f}% ({V.display(a['norm'])} vs {V.display(base['norm'])}): "
                             f"{'within' if within else 'outside'} the {tol}% tolerance.")
            steps.append(f"Rule {r['id']} v{rule['version']}. Conclusion: {'pass' if ok else 'fail'}." + (f" Not available: {', '.join(absent)}." if absent else ""))
            if r["type"] == "amount_limit":
                over = (have[0]["norm"]["amount"] - base["norm"]["amount"]) / base["norm"]["amount"] * 100 if base["norm"]["amount"] else 0.0
                msg = (f"{V.display(have[0]['norm'])} is within the limit of {V.display(base['norm'])}" if ok else
                       f"{V.display(have[0]['norm'])} exceeds the limit of {V.display(base['norm'])} by {over:.2f}% (tolerance {tol}%)") + "."
            else:
                msg = ("Values reconcile" if ok else "Values do not reconcile") + f" (largest difference {worst:.2f}%, tolerance {tol}%)" + (f"; {', '.join(absent)} not available" if absent else "") + "."
            out.append(finding(fid, fam, title, "pass" if ok else "fail", msg,
                               severity="high", reasoning=steps, sources=have, path=path_of(have), data={"largest_difference_pct": round(worst, 2)}, **kw))
        elif r["type"] == "codes_consistent":
            got = [(ref, need(ref)) for ref in r["codes"]]
            have = [a for _, rows in got for a in rows]
            absent = [profile.field_name(ref) for ref, rows in got if not rows and docs_of(case, ref.partition(".")[0])]
            rec = next((x for x in refs if x["id"] == r.get("reference")), None)
            cand = (rec or {}).get("result") or {}
            codes = {(str(a["norm"]) + "0000000000")[:10] for a in have}
            steps = [f"{a['field_name']}: {V.display(a['norm'], 'code')} (from {a['doc_name']})." for a in have] + [f"{x} is absent from the document." for x in absent]
            extra = [f"ref:{c}" for c in cand.get("path") or []]
            if cand.get("status") == "supported":
                steps.append(f"Evidence-supported candidate from the reference: {cand['display']} ({cand['description']}).")
            elif rec:
                steps.append("The reference could not return a supported candidate, so the stated codes are compared with each other only.")
            if cand.get("status") == "supported" and codes and codes != {cand["code"]}:
                status, msg = "fail", "The stated code(s) " + ", ".join(V.display(c, "code") for c in sorted(codes)) + f" differ from the evidence-supported {cand['display']}."
            elif len(codes) > 1:
                status, msg = "fail", "The documents state different commodity codes: " + ", ".join(V.display(c, "code") for c in sorted(codes)) + "."
            elif absent or not have:
                status, msg = "review", ("No commodity code is stated in the documents." if not have else ", ".join(absent) + " is not stated; the other code cannot be corroborated.")
            elif cand.get("status") != "supported" and rec:
                status, msg = "review", f"The documents agree on {V.display(next(iter(codes)), 'code')}, but the product evidence does not yet support a classification."
            else:
                status, msg = "pass", f"All sources agree on {V.display(next(iter(codes)), 'code')}."
            steps.append(f"Rule {r['id']} v{rule['version']}. Conclusion: {status}.")
            out.append(finding(fid, fam, title, status, msg, severity="high", reasoning=steps, sources=have, path=path_of(have) + extra, reference=(rec or {}).get("citation"),
                               data={"codes": sorted(codes), "candidate": cand.get("code")}, **kw))
    return {"findings": out, "summary": f"{len(out)} rule(s) executed: " + ", ".join(f"{sum(1 for f in out if f['status'] == s)} {s}" for s in ("pass", "fail", "review"))}


# ------------------------------------------------------------------------------------------------ contradictions
def consistency(case: dict[str, Any], profile: Profile, overlay: dict[str, Any] | None = None) -> dict[str, Any]:
    alias = G.vocabulary(profile, overlay, "party")
    out = []
    for c in profile.consistency:
        rows = [a for ref in c["fields"] for a in assertions(case, profile, ref)]
        fid, fam, kind = f"CON-{c['id']}", c.get("family") or "consistency", c.get("kind", "text")
        rule = {"id": f"consistency.{c['id']}", "version": profile.version, "title": c["title"], "type": kind}
        kw = {"agent": "contradiction", "rule": rule, "path": [aid(a["doc"], a["field"]) for a in rows]}
        shown = [f"{a['doc_name']} · {a['field_name'].split(' · ')[-1]}: {V.display(a['norm'], a['ftype'])}" for a in rows]
        if len(rows) < 2:
            out.append(finding(fid, fam, c["title"], "info", "Stated in one document only: nothing to compare it with." if rows else "Not stated in any document.", severity="low",
                               reasoning=shown, sources=rows, requires_human=False, data={"kind": "consistency", "compared": len(rows)}, **kw))
            continue
        base, differ, maybe = rows[0], [], []
        for a in rows[1:]:
            if kind == "party":
                ka, kb = alias.get(V.party_key(base["value"]), V.party_key(base["value"])), alias.get(V.party_key(a["value"]), V.party_key(a["value"]))
                sim = 1.0 if ka == kb else V.similarity(ka, kb)
                (differ if sim < G.MAYBE_PARTY else maybe if sim < G.SAME_PARTY else []).append(a)
            elif kind == "number":
                tol = float(c.get("tolerance_pct") or 0)
                d = abs(a["norm"] - base["norm"]) / abs(base["norm"]) * 100 if base["norm"] else (0 if a["norm"] == 0 else 100)
                if d > tol:
                    differ.append(a)
            elif kind == "ref":
                ka, kb = V.ref_key(base["value"]), V.ref_key(a["value"])
                if not (ka == kb or (min(len(ka), len(kb)) >= 5 and (ka in kb or kb in ka))):
                    differ.append(a)
            elif V.overlap(base["value"], a["value"]) < G.SAME_GOODS and str(base["norm"]).lower() != str(a["norm"]).lower():
                differ.append(a)
        if differ:
            out.append(finding(fid, fam, c["title"], "fail", f"{base['doc_name']} states {V.display(base['norm'], base['ftype'])}; "
                               + "; ".join(f"{a['doc_name']} states {V.display(a['norm'], a['ftype'])}" for a in differ) + ".", severity=c.get("severity", "medium"),
                               reasoning=shown + [f"Compared as {kind}" + (f" with a tolerance of {c.get('tolerance_pct')}%" if kind == "number" else "") + f": {len(differ)} value(s) disagree with {base['doc_name']}."],
                               sources=rows, data={"kind": "consistency", "compared": len(rows), "disagree": [a["doc"] for a in differ]}, **kw))
        elif maybe:
            out.append(finding(fid, fam, c["title"], "review", "Possibly the same party under a different name: " + "; ".join(f"'{V.party_name(a['value'])}' in {a['doc_name']}" for a in maybe)
                               + f" against '{V.party_name(base['value'])}' in {base['doc_name']}.", severity=c.get("severity", "medium"),
                               reasoning=shown + ["The names are similar but not the same: a person should confirm whether this is one party."], sources=rows,
                               data={"kind": "consistency", "compared": len(rows), "disagree": [a["doc"] for a in maybe]}, **kw))
        else:
            out.append(finding(fid, fam, c["title"], "pass", f"Corroborated by {len(rows)} document(s): {V.display(base['norm'], base['ftype'])[:90]}.", severity="low",
                               reasoning=shown + [f"Compared as {kind}: all {len(rows)} agree."], sources=rows, data={"kind": "consistency", "compared": len(rows)}, **kw))
    fails = [f for f in out if f["status"] == "fail"]
    return {"findings": out, "summary": f"{len(out)} cross-document check(s): {sum(1 for f in out if f['status'] == 'pass')} corroborated, {len(fails)} discrepancy(ies), "
                                        f"{sum(1 for f in out if f['status'] == 'review')} to confirm" + (" (" + ", ".join(f["title"] for f in fails) + ")" if fails else "")}


# ------------------------------------------------------------------------------------------------ verification and recommendation
def known(case: dict[str, Any], profile: Profile, recall: dict[str, Any]) -> list[dict[str, Any]]:
    """What the workspace's memory holds that touches this case, as findings. A reference number another case already quotes is
    information by default; a use case that lists the reference under `memory.duplicates` sends it to a person (a deterministic
    comparison, never a model's judgement)."""
    from .assurance import mask   # a bank account the workspace remembers is named without spelling it out

    strict = {str(k).lower() for k in ((profile.raw.get("memory") or {}).get("duplicates") or [])}
    out = []
    for n, i in enumerate(recall.get("identifiers") or [], start=1):
        review = i["type"].lower() in strict
        where = ", ".join(f"{c['case']} ({c.get('label') or 'assessed'})" for c in i["cases"])
        shown = mask(i["value"])
        out.append(finding(f"MEM-reference-{n}", "memory", f"{i['type']} {shown} is already known", "review" if review else "info",
                           f"{i['type']} {shown} is also quoted in {i['count']} other case(s): {where}." + (" The same reference in two cases needs a person to confirm it is not a duplicate." if review else ""),
                           agent="resolve", severity="high" if review else "low", requires_human=review,
                           reasoning=[f"The workspace's memory holds one episode per assessed case; {recall.get('episodes_searched', 0)} were searched.",
                                      f"{i['type']} references are compared after removing spaces and punctuation.", f"Found in: {where}."], data={"memory": i}))
    parties = recall.get("parties") or []
    if parties:
        lines = [f"{p['name']}: {p['count']} earlier case(s), most recently {p['cases'][-1]['case']} ({p['cases'][-1].get('label') or 'assessed'})" for p in parties]
        out.append(finding("MEM-parties", "memory", "Parties the workspace has met before", "info", "; ".join(lines) + ".", agent="resolve", severity="low", requires_human=False,
                           reasoning=["Earlier cases are context for the caseworker. They do not change this case's outcome."] + lines, data={"memory": {"parties": parties}}))
    return out


def verify(case: dict[str, Any], profile: Profile, findings: list[dict[str, Any]], graph: dict[str, Any]) -> dict[str, Any]:
    """Source coverage per finding, then the recommendation. A positive finding that cannot show its sources is blocked."""
    blocked = []
    for f in findings:
        srcs = f["sources"]
        ok_n = sum(1 for s in srcs if s.get("verified"))
        needs_source = f["status"] == "pass" and f["family"] != "reference"
        cover = ok_n / len(srcs) if srcs else (0.0 if needs_source else 1.0)
        f["verifier"] = {"sources": len(srcs), "verified": ok_n, "coverage": round(cover, 2), "ok": cover == 1.0,
                         "note": "every cited line was found in its document" if cover == 1.0 and srcs else
                                 "rests on the absence of a document" if not srcs and not needs_source else f"{len(srcs) - ok_n} cited value(s) could not be matched to a document line"}
        if needs_source and cover < 1.0:
            f["blocked"] = True
            blocked.append(f)
        G.add_node(graph, f"F:{f['id']}", "RuleEvaluation", f"{f['title']}: {f['status']}", "reasoning", status=f["status"], family=f["family"], agent=f["agent"])
        if f.get("rule"):
            G.add_node(graph, f"rule:{f['rule']['id']}", "Rule", f"{f['rule']['title']} (v{f['rule']['version']})", "reasoning", version=f["rule"]["version"])
            G.add_edge(graph, f"F:{f['id']}", f"rule:{f['rule']['id']}", "EVALUATES")
        for node in f["graph_path"]:
            G.add_edge(graph, f"F:{f['id']}", node, "SUPPORTED_BY")
    missing = [f for f in findings if f["status"] == "missing"]
    insufficient = [f for f in findings if f["family"] == "sufficiency" and f["status"] == "review"]
    fails = [f for f in findings if f["status"] == "fail"]
    reviews = [f for f in findings if f["status"] == "review" and f not in insufficient] + blocked
    if fails:
        outcome = "review"
    elif missing or insufficient:
        outcome = "request_evidence"
    elif reviews:
        outcome = "review"
    else:
        outcome = "proceed"
    label = (profile.outcomes.get(outcome) or {}).get("label") or outcome
    requests = [f["data"]["request"] for f in missing + insufficient if f["data"].get("request")]
    caveats = [f["message"] for f in findings if f["family"] == "reference" and f["requires_human"]]
    caveats += [f"{f['title']}: verification could not match every source ({f['verifier']['note']})." for f in blocked]
    reasons = [f"{f['title']}: {f['message']}" for f in fails + missing + insufficient + reviews]
    rec = {"outcome": outcome, "label": label, "description": (profile.outcomes.get(outcome) or {}).get("description") or "",
           "reasons": reasons, "requests": requests, "caveats": caveats,
           "basis": {"pass": [f["id"] for f in findings if f["status"] == "pass" and not f.get("blocked")], "fail": [f["id"] for f in fails], "missing": [f["id"] for f in missing],
                     "review": [f["id"] for f in reviews + insufficient], "blocked": [f["id"] for f in blocked]},
           "decided_by": "deterministic aggregation of the findings; no model takes part in the outcome",
           "requires_human": True}
    G.add_node(graph, "recommendation", "Recommendation", label, "reasoning", outcome=outcome)
    for f in fails + missing + insufficient + reviews or findings:
        G.add_edge(graph, "recommendation", f"F:{f['id']}", "BASED_ON")
    G.add_edge(graph, "case", "recommendation", "HAS_RECOMMENDATION")
    total = len(findings)
    covered = sum(1 for f in findings if f["verifier"]["ok"])
    return {"recommendation": rec, "blocked": [f["id"] for f in blocked], "coverage": {"findings": total, "fully_sourced": covered},
            "summary": f"Recommendation: {label}. {covered} of {total} finding(s) fully sourced; {len(blocked)} blocked as unsupported."}


def narrative(case: dict[str, Any], profile: Profile, assessment: dict[str, Any]) -> str:
    """The case summary in plain sentences, composed from the findings (so it can say nothing they do not)."""
    ents = assessment.get("entities") or []
    goods = next((e for e in ents if e["type"] == "GoodsItem"), None)
    roles = {}
    for e in ents:
        for role in e.get("roles") or []:
            roles.setdefault(role, e["name"])
    fs = assessment.get("findings") or []
    parts = [f"{profile.brand.get('case_noun', 'case').capitalize()} {case['reference']}" + (f" ({case['fields']['claim_type']})" if case["fields"].get("claim_type") else "")
             + f" holds {len(live_docs(case))} document(s)."]
    if goods:
        who = [f"{r.lower()} {n}" for r, n in roles.items() if r in ("Seller", "Buyer", "Importer", "Applicant", "Beneficiary")][:2]
        parts.append(f"The goods are described as {goods['name']}" + (", with " + " and ".join(who) if who else "") + ".")
    missing = [f["title"] for f in fs if f["status"] == "missing"]
    if missing:
        parts.append("Missing evidence: " + ", ".join(missing) + ".")
    for f in fs:
        if f["id"].endswith("-classification"):
            parts.append(f["message"])
    fails = [f for f in fs if f["status"] == "fail"]
    if fails:
        parts.append("Discrepancies: " + " ".join(f"{f['title']}: {f['message']}" for f in fails[:4]))
    timing = [f for f in fs if f["family"] == "timing" and f["status"] in ("pass", "fail")]
    if timing:
        parts.append(" ".join(f"{t['title']}: {t['status']} ({t['message']})" for t in timing[:1]))
    rec = assessment.get("recommendation") or {}
    if rec:
        parts.append(f"Recommendation: {rec['label']}.")
    return " ".join(parts)
