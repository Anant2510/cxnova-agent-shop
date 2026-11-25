"""Reference sources: authoritative content a case is read against, retrieved for a date and cited.

A reference is never pasted into a prompt and forgotten. Each retrieval is a recorded step: the address asked, the
date it was asked for, when it was retrieved, a hash of what came back and which version that is. A profile declares
its sources (`reference_sources`); a connector knows how to fetch one kind.

    uk_trade_tariff   a heading of the UK Integrated Online Tariff, as of a date
                      live:      DOCUINSIGHTS_REFERENCE_LIVE=1 reads the tariff's published API (the machine interface
                                 of the service; the web pages themselves are not scraped)
                      snapshot:  otherwise, or when the live call fails, the snapshot file shipped with the profile,
                                 and the retrieval says so in plain words
    static            a file shipped with the profile (any hierarchy a profile wants to reason over)

The live parser reads the API's JSON:API document (heading in `data`, commodities in `included`). It was written
without access to the service from the build environment: check one live retrieval before relying on it.
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.request
from pathlib import Path
from typing import Any

from . import net
from .casefile import now

HERE = Path(__file__).resolve().parent
TARIFF_SITE = "https://www.trade-tariff.service.gov.uk"


def page_url(heading: str, as_of: str | None) -> str:
    if not as_of:
        return f"{TARIFF_SITE}/headings/{heading}"
    y, m, d = (int(x) for x in as_of.split("-"))
    return f"{TARIFF_SITE}/headings/{heading}?day={d}&month={m}&year={y}"


def snapshot(source: dict[str, Any]) -> tuple[dict[str, Any], str]:
    path = HERE / "reference" / str(source.get("snapshot") or "")
    raw = path.read_bytes()
    return json.loads(raw.decode("utf-8")), hashlib.sha256(raw).hexdigest()


def retrieve(source: dict[str, Any], as_of: str | None, *, fetch: Any = None) -> dict[str, Any]:
    """One retrieval of a reference source for a date, with its provenance. `fetch(url) -> bytes` can be injected (tests, a host's own HTTP policy)."""
    heading = str(source.get("heading") or "")
    connector = source.get("connector") or "static"
    out: dict[str, Any] = {"source": source["id"], "name": source.get("name") or source["id"], "connector": connector, "heading": heading,
                           "requested_as_of": as_of, "retrieved_at": now(), "url": page_url(heading, as_of) if connector == "uk_trade_tariff" else "",
                           "tool_calls": []}
    live = connector == "uk_trade_tariff" and (fetch is not None or os.environ.get("DOCUINSIGHTS_REFERENCE_LIVE") == "1")
    if live:
        api = f"{TARIFF_SITE}/api/v2/headings/{heading}" + (f"?as_of={as_of}" if as_of else "")
        try:
            raw = fetch(api) if fetch is not None else _get(api)
            nodes, title = parse_tariff_api(json.loads(raw.decode("utf-8")), heading)
            if len(nodes) < 2:
                raise ValueError("the response held no commodity lines")
            out.update(mode="live", api_url=api, result_hash=hashlib.sha256(raw).hexdigest(), title=title, nodes=nodes, partial=False,
                       version=f"as of {as_of or 'today'}", note="Retrieved from the tariff's published API for the requested date.")
            out["tool_calls"].append({"tool": "reference.fetch", "detail": api, "status": "ok"})
            return out
        except Exception as exc:
            out["tool_calls"].append({"tool": "reference.fetch", "detail": api, "status": f"failed: {type(exc).__name__}: {str(exc)[:120]}"})
            out["live_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    snap, digest = snapshot(source)
    dated = snap.get("date_of_trade")
    note = snap.get("note") or "Bundled snapshot."
    if as_of and dated and dated != as_of:
        note = f"The snapshot shows the heading for {dated}; the case asks for {as_of}. " + note
    out.update(mode="snapshot", result_hash=digest, title=snap.get("title") or "", nodes=snap.get("nodes") or [], partial=bool(snap.get("partial")),
               version=f"snapshot of {dated}" if dated else "snapshot", snapshot_date=dated, note=note)
    out["tool_calls"].append({"tool": "reference.snapshot", "detail": str(source.get("snapshot")), "status": "ok"})
    return out


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"accept": "application/json", "user-agent": "docuinsights-reference/0.1"})
    with net.urlopen(req, timeout=20) as resp:
        return resp.read()


def parse_tariff_api(doc: dict[str, Any], heading: str) -> tuple[list[dict[str, Any]], str]:
    """The heading's lines as a parent/child list from the API's JSON:API document."""
    data = doc.get("data") or {}
    attrs = data.get("attributes") or {}
    root = (str(attrs.get("goods_nomenclature_item_id") or heading) + "0000000000")[:10]
    title = str(attrs.get("description_plain") or attrs.get("formatted_description") or attrs.get("description") or "")
    nodes = [{"code": root, "display": heading, "description": title, "parent": None, "declarable": False}]
    stack: list[tuple[int, str]] = [(0, root)]   # (indent, node id) of the open ancestors
    seen = {root}
    for item in doc.get("included") or []:
        if item.get("type") != "commodity":
            continue
        a = item.get("attributes") or {}
        code = str(a.get("goods_nomenclature_item_id") or "")
        if len(code) != 10:
            continue
        indent = int(a.get("number_indents") or 1)
        leaf = bool(a.get("leaf") if a.get("leaf") is not None else a.get("declarable"))
        node_id = code if code not in seen else f"{code}-{a.get('producline_suffix') or len(nodes)}"
        seen.add(node_id)
        while stack and stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1] if stack else root
        nodes.append({"code": node_id, "display": f"{code[:4]} {code[4:6]} {code[6:8]} {code[8:]}", "description": str(a.get("description_plain") or a.get("description") or ""),
                      "parent": parent, "declarable": leaf})
        stack.append((indent, node_id))
    return nodes, title


# ------------------------------------------------------------------------------------------------ reasoning over a hierarchy
def _test(cond: dict[str, Any], chars: dict[str, Any]) -> tuple[str, str]:
    """One condition against the product characteristics: ('true' | 'false' | 'unknown', why)."""
    c = chars.get(cond["field"])
    name = (c or {}).get("name") or cond["field"].replace("_", " ")
    if c is None:
        return "unknown", f"{name} is not stated in the product evidence"
    if c.get("value") is None:
        return "unknown", f"{name} '{c.get('raw')}' is not in the approved vocabulary"
    v = c["value"]
    if "equals" in cond:
        ok = str(v).lower() == str(cond["equals"]).lower()
        return ("true" if ok else "false"), f"{name} is {v}" + ("" if ok else f", not {cond['equals']}")
    if "contains" in cond:
        ok = str(cond["contains"]).lower() in str(c.get("raw") or v).lower()
        return ("true" if ok else "false"), f"{name} {'mentions' if ok else 'does not mention'} '{cond['contains']}'"
    try:
        num = float(v)
    except (TypeError, ValueError):
        return "unknown", f"{name} '{v}' is not a number"
    lo, hi = cond.get("min"), cond.get("max")
    ok = (lo is None or num >= float(lo)) and (hi is None or num <= float(hi))
    bound = f"at least {lo} and at most {hi}" if lo is not None and hi is not None else (f"at most {hi}" if hi is not None else f"at least {lo}")
    return ("true" if ok else "false"), f"{name} is {num:g}, which is {'' if ok else 'not '}{bound}"


def _short(text: str, limit: int = 84) -> str:
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "..."


def _judge(aid: dict[str, Any] | None, chars: dict[str, Any]) -> tuple[str, list[str], list[str]]:
    if aid is None:
        return "residual", [], []
    verdicts = [(_test(c, chars), c["field"]) for c in aid.get("all") or []]
    whys = [w for (_, w), _ in verdicts]
    if any(v == "false" for (v, _), _ in verdicts):
        return "false", [w for (v, w), _ in verdicts if v == "false"], []
    unknown = [f for (v, _), f in verdicts if v == "unknown"]
    if unknown:
        return "unknown", [w for (v, w), _ in verdicts if v == "unknown"], unknown
    return "true", whys, []


def classify(nodes: list[dict[str, Any]], aids: dict[str, Any], chars: dict[str, Any]) -> dict[str, Any]:
    """Walk the hierarchy from its root, choosing at each level the line the product characteristics support.

    status: supported     a declarable line was reached and every choice on the way rests on a stated characteristic
            insufficient  a choice could not be made because a characteristic is missing: the lines still possible are returned
            incomplete    the reference held here has no line that fits (a partial snapshot, or goods outside the heading)
            none          there are no product characteristics to reason from
    """
    by_code = {n["code"]: n for n in nodes}
    kids: dict[str | None, list[dict[str, Any]]] = {}
    for n in nodes:
        kids.setdefault(n.get("parent"), []).append(n)
    root = (kids.get(None) or [None])[0]
    if root is None or not chars:
        return {"status": "none", "path": [], "steps": ["No product characteristics were available to reason from."], "missing": [], "alternatives": []}
    path, steps, cur = [root["code"]], [f"Start at {root['display']} {root['description']}."], root
    while True:
        children = kids.get(cur["code"]) or []
        if not children:
            status = "supported" if cur.get("declarable") else "incomplete"
            if status == "incomplete":
                steps.append(f"The reference held here has no lines under {cur['display']}: it cannot be taken further.")
            return {"status": status, "code": cur["code"], "display": cur["display"], "description": cur["description"], "path": path, "steps": steps,
                    "missing": [], "alternatives": [], "measures": {k: cur[k] for k in ("vat", "third_country_duty", "supplementary_unit") if cur.get(k)}}
        judged = [(c, *_judge(aids.get(c["code"]), chars)) for c in children]
        true = [j for j in judged if j[1] == "true"]
        unknown = [j for j in judged if j[1] == "unknown"]
        residual = [j for j in judged if j[1] == "residual"]
        for c, verdict, whys, _ in judged:
            if verdict in ("true", "false"):
                steps.append(f"{c['display']} {_short(c['description'])}: {'applies' if verdict == 'true' else 'ruled out'} ({'; '.join(whys)}).")
        if len(true) == 1:
            nxt = true[0][0]
        elif not true and not unknown and len(residual) == 1:
            nxt = residual[0][0]
            steps.append(f"No specific line under {cur['display']} applies, so the residual line {nxt['display']} '{nxt['description']}' is taken.")
        elif unknown or len(true) > 1:
            open_lines = [j[0] for j in (unknown or true)] + [j[0] for j in residual]
            missing = sorted({f for j in unknown for f in j[3]})
            for c, _, whys, _ in unknown:
                steps.append(f"{c['display']} {_short(c['description'])}: cannot be decided ({'; '.join(whys)}).")
            return {"status": "insufficient", "code": cur["code"], "display": cur["display"], "description": cur["description"], "path": path, "steps": steps,
                    "missing": missing, "alternatives": [{"code": n["code"], "display": n["display"], "description": n["description"]} for n in open_lines], "measures": {}}
        else:
            steps.append(f"No line held under {cur['display']} fits the product evidence.")
            return {"status": "incomplete", "code": cur["code"], "display": cur["display"], "description": cur["description"], "path": path, "steps": steps,
                    "missing": [], "alternatives": [], "measures": {}}
        path.append(nxt["code"])
        cur = by_code[nxt["code"]]
