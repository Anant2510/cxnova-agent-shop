"""Entity resolution and the case knowledge graph.

The entity resolution agent links what the documents repeat: the same party under slightly different names, the same
goods described in different words, the same invoice or shipment quoted by reference. The graph agent then writes the
case as a graph: case, documents and their versions, evidence assertions (one per extracted field), the resolved
entities, and later the reference lines, rule evaluations and the recommendation, each joined to what supports it.
The graph is an organised view of the evidence. It is never a source of its own: every node traces to a document
line, a case fact or a retrieved reference.
"""
from __future__ import annotations

from collections import Counter, deque
from typing import Any

from . import values as V
from .casefile import aid, assertions, current, doc_type, live_docs
from .profiles import Profile

SAME_PARTY = 0.86      # name similarity at or above which two mentions are one party
MAYBE_PARTY = 0.60     # between the two: a possible match a person should confirm
SAME_GOODS = 0.5       # share of the shorter description's words found in the other


def vocabulary(profile: Profile, overlay: dict[str, Any] | None, table: str) -> dict[str, str]:
    """A synonym table of the profile plus the approved additions of the feedback loop."""
    merged = {str(k).lower(): str(v) for k, v in (profile.synonyms.get(table) or {}).items()}
    merged.update({str(k).lower(): str(v) for k, v in (((overlay or {}).get("synonyms") or {}).get(table) or {}).items()})
    return merged


def normalise_term(raw: Any, table: dict[str, str]) -> str | None:
    low = " ".join(str(raw or "").lower().split())
    if low in table:
        return table[low]
    for phrase in sorted(table, key=len, reverse=True):   # the longest phrase contained in the text decides
        if phrase and phrase in low:
            return table[phrase]
    return None


# ------------------------------------------------------------------------------------------------ entity resolution
def resolve(case: dict[str, Any], profile: Profile, overlay: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = profile.entities
    alias = vocabulary(profile, overlay, "party")
    parties: list[dict[str, Any]] = []
    for ref, role in (cfg.get("parties") or {}).items():
        for a in assertions(case, profile, ref):
            key = V.party_key(a["value"])
            key = alias.get(key, key)
            if not key:
                continue
            mention = {"doc": a["doc"], "doc_name": a["doc_name"], "field": a["field"], "ref": ref, "value": a["value"], "name": V.party_name(a["value"]), "role": role}
            home = next((p for p in parties if p["key"] == key or V.similarity(p["key"], key) >= SAME_PARTY), None)
            if home is None:
                home = {"key": key, "mentions": []}
                parties.append(home)
            home["mentions"].append(mention)
    entities: list[dict[str, Any]] = []
    for i, p in enumerate(parties, start=1):
        name = Counter(m["name"] for m in p["mentions"]).most_common(1)[0][0]
        entities.append({"id": f"P{i}", "type": "Party", "name": name, "key": p["key"], "roles": sorted({m["role"] for m in p["mentions"]}), "mentions": p["mentions"]})
    unresolved = []
    ps = [e for e in entities if e["type"] == "Party"]
    for i, a in enumerate(ps):
        for b in ps[i + 1:]:
            sim = V.similarity(a["key"], b["key"])
            ta, tb = set(a["key"].split()), set(b["key"].split())
            if MAYBE_PARTY <= sim < SAME_PARTY or (len(ta & tb) >= 2 and (ta <= tb or tb <= ta)):
                unresolved.append({"id": f"M{len(unresolved) + 1}", "a": a["id"], "b": b["id"], "a_name": a["name"], "b_name": b["name"], "similarity": round(sim, 2),
                                   "reason": f"'{a['name']}' and '{b['name']}' are similar but not the same name: one party or two?"})

    goods: list[dict[str, Any]] = []
    for ref in cfg.get("goods") or []:
        for a in assertions(case, profile, ref):
            mention = {"doc": a["doc"], "doc_name": a["doc_name"], "field": a["field"], "ref": ref, "value": a["value"]}
            home = next((g for g in goods if max(V.overlap(m["value"], a["value"]) for m in g["mentions"]) >= SAME_GOODS), None)
            if home is None:
                home = {"mentions": []}
                goods.append(home)
            home["mentions"].append(mention)
    for i, g in enumerate(goods, start=1):
        entities.append({"id": f"G{i}", "type": "GoodsItem", "name": max((m["value"] for m in g["mentions"]), key=len)[:140], "mentions": g["mentions"], "characteristics": {}})

    ch = cfg.get("characteristics") or {}
    for d in [x for x in live_docs(case) if doc_type(x) == ch.get("document_type")]:
        target = next((e for e in entities if e["type"] == "GoodsItem" and any(m["doc"] == d["id"] for m in e["mentions"])), None)
        if target is None:
            target = next((e for e in entities if e["type"] == "GoodsItem"), None)
        if target is None:
            continue
        for fid in ch.get("fields") or []:
            f = (d.get("fields") or {}).get(fid)
            if not f or f.get("norm") is None:
                continue
            table = vocabulary(profile, overlay, fid)
            entry = {"value": f["norm"], "raw": f["value"], "doc": d["id"], "field": fid, "name": (profile.field(doc_type(d) or "", fid) or {}).get("name") or fid}
            if table:
                entry["value"] = normalise_term(f["value"], table)
                entry["unmapped"] = entry["value"] is None
            target["characteristics"][fid] = entry

    n = 0
    for label, refs in (cfg.get("references") or {}).items():
        groups: list[dict[str, Any]] = []
        for ref in refs:
            for a in assertions(case, profile, ref):
                key = V.ref_key(a["value"])
                if len(key) < 3:
                    continue
                home = next((g for g in groups if any(k == key or (min(len(k), len(key)) >= 5 and (k in key or key in k)) for k in g["keys"])), None)
                if home is None:
                    home = {"keys": [], "mentions": []}
                    groups.append(home)
                home["keys"].append(key)
                home["mentions"].append({"doc": a["doc"], "doc_name": a["doc_name"], "field": a["field"], "ref": ref, "value": a["value"]})
        for g in groups:
            n += 1
            entities.append({"id": f"R{n}", "type": label, "name": min((m["value"] for m in g["mentions"]), key=len), "mentions": g["mentions"]})
    counts = Counter(e["type"] for e in entities)
    return {"entities": entities, "unresolved": unresolved,
            "summary": f"{len(entities)} entities resolved from {sum(len(e['mentions']) for e in entities)} mentions ("
                       + ", ".join(f"{v} {k}" for k, v in counts.items()) + f"); {len(unresolved)} possible match(es) left for a person"}


# ------------------------------------------------------------------------------------------------ the graph
def add_node(graph: dict[str, Any], node_id: str, ntype: str, label: str, layer: str, **props: Any) -> str:
    if node_id not in graph["index"]:
        graph["index"][node_id] = len(graph["nodes"])
        graph["nodes"].append({"id": node_id, "type": ntype, "label": str(label)[:120], "layer": layer, **({"props": props} if props else {})})
    return node_id


def add_edge(graph: dict[str, Any], a: str, b: str, rel: str) -> None:
    if a in graph["index"] and b in graph["index"] and (a, b, rel) not in graph["seen"]:
        graph["seen"].add((a, b, rel))
        graph["edges"].append({"from": a, "to": b, "type": rel})


def build(case: dict[str, Any], profile: Profile, entities: list[dict[str, Any]]) -> dict[str, Any]:
    g: dict[str, Any] = {"nodes": [], "edges": [], "index": {}, "seen": set()}
    add_node(g, "case", "Case", case["reference"], "case", profile=profile.id, **{k: str(v) for k, v in case["fields"].items()})
    for d in live_docs(case):
        ver = current(d)
        add_node(g, d["id"], "Document", d["name"], "document", document_type=profile.type_name(doc_type(d)), assumed=bool(d.get("assumed")))
        add_edge(g, "case", d["id"], "CONTAINS")
        vid = f"{d['id']}v{d['current']}"
        add_node(g, vid, "DocumentVersion", f"{d['name']} v{d['current']}", "document", sha256=ver.get("sha256", "")[:16], uploaded_at=ver.get("uploaded_at", ""), read=ver.get("method", ""))
        add_edge(g, d["id"], vid, "HAS_VERSION")
        for fid, f in (d.get("fields") or {}).items():
            if f.get("norm") is None:
                continue
            name = (profile.field(doc_type(d) or "", fid) or {}).get("name") or fid
            add_node(g, aid(d["id"], fid), "EvidenceAssertion", f"{name}: {V.display(f['norm'], f.get('type', 'text'))}", "assertion",
                     confidence=f.get("confidence"), verified=bool(f.get("verified")), method=f.get("method"), page=(f.get("anchor") or {}).get("page"),
                     line=(f.get("anchor") or {}).get("line"))
            add_edge(g, vid, aid(d["id"], fid), "SUPPORTS")
    for e in entities:
        add_node(g, e["id"], e["type"], e["name"], "entity", roles=", ".join(e.get("roles") or []), mentions=len(e["mentions"]))
        for m in e["mentions"]:
            add_edge(g, aid(m["doc"], m["field"]), e["id"], "MENTIONS")
        for fid, c in (e.get("characteristics") or {}).items():
            cid = f"{e['id']}.{fid}"
            add_node(g, cid, "ProductCharacteristic", f"{c['name']}: {c['value'] if c['value'] is not None else c['raw']}", "entity", raw=str(c["raw"]), unmapped=bool(c.get("unmapped")))
            add_edge(g, e["id"], cid, "HAS_CHARACTERISTIC")
            add_edge(g, aid(c["doc"], fid), cid, "SUPPORTS")
    goods = [e for e in entities if e["type"] == "GoodsItem"]
    for e in entities:   # an invoice references the goods; a shipment transports them
        if e["type"] in ("Invoice", "Shipment") and goods:
            add_edge(g, e["id"], goods[0]["id"], "REFERENCES" if e["type"] == "Invoice" else "TRANSPORTS")
    return g


def finish(graph: dict[str, Any]) -> dict[str, Any]:
    """The graph as stored with the case (the working index is dropped)."""
    return {"nodes": graph["nodes"], "edges": graph["edges"]}


def reopen(stored: dict[str, Any]) -> dict[str, Any]:
    g = {"nodes": list(stored.get("nodes") or []), "edges": list(stored.get("edges") or []), "index": {}, "seen": set()}
    g["index"] = {n["id"]: i for i, n in enumerate(g["nodes"])}
    g["seen"] = {(e["from"], e["to"], e["type"]) for e in g["edges"]}
    return g


def neighbours(graph: dict[str, Any], node_id: str) -> list[tuple[str, str, str]]:
    out = []
    for e in graph.get("edges") or []:
        if e["from"] == node_id:
            out.append((e["to"], e["type"], "out"))
        elif e["to"] == node_id:
            out.append((e["from"], e["type"], "in"))
    return out


def shortest_path(graph: dict[str, Any], start: str, goal: str, max_hops: int = 8) -> list[str]:
    """Multi-hop traversal: the shortest chain of nodes joining two things in the case, in either edge direction."""
    ids = {n["id"] for n in graph.get("nodes") or []}
    if start not in ids or goal not in ids:
        return []
    seen, queue = {start}, deque([[start]])
    while queue:
        path = queue.popleft()
        if path[-1] == goal:
            return path
        if len(path) > max_hops:
            continue
        for nxt, _, _ in neighbours(graph, path[-1]):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(path + [nxt])
    return []


def stats(graph: dict[str, Any]) -> dict[str, Any]:
    by_type = Counter(n["type"] for n in graph.get("nodes") or [])
    return {"nodes": len(graph.get("nodes") or []), "edges": len(graph.get("edges") or []), "by_type": dict(by_type)}
