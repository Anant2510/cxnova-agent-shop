"""Shared agentic memory: what the agents keep for each other, at three distances.

    run          working memory of one assessment: the note each agent leaves for the agents after it. Gone when the
                 assessment ends (it stays readable in the assessment's record).
    case         what belongs to one case for as long as the case lives: its documents, the reviewer's corrections and
                 decisions. This is the case file.
    workspace    what outlives a case and is shared by every case of the workspace:
                   semantic    the knowledge base: the organisations, goods and reference numbers met so far
                   episodic    one episode per assessed case: what it was, who was in it, how it came out
                   procedural  the vocabulary approved through governed feedback (signals, aliases, synonyms)

An agent reaches memory only through the handle its contract allows (`MemoryHandle`): a read or a write outside the
scopes its contract names is refused and recorded. Nothing reaches the workspace's procedural memory without a person's
approval; episodes and the knowledge base are written by the orchestrator when an assessment ends, never by a model.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from . import values as V
from .casefile import now

SCOPES = ("run", "case", "workspace")
_lock = threading.Lock()


class MemoryDenied(PermissionError):
    pass


class Episodes:
    """The workspace's episodic memory, one JSON line per assessed case (the latest assessment of a case replaces its line)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def remember(self, case: dict[str, Any], profile: Any) -> dict[str, Any] | None:
        a = case.get("assessment")
        if not a:
            return None
        rec = a.get("recommendation") or {}
        ep = {"case": case["reference"], "profile": profile.id, "use_case": profile.name, "at": a.get("at"), "assessment": a.get("id"), "outcome": rec.get("outcome"), "label": rec.get("label"),
              "reasons": (rec.get("reasons") or [])[:4], "decision": (case.get("decision") or {}).get("decision"), "sample": (case.get("sample") or {}).get("id"),
              "parties": [{"key": e.get("key"), "name": e["name"], "roles": e.get("roles") or []} for e in a.get("entities") or [] if e["type"] == "Party"],
              "identifiers": [{"type": e["type"], "value": e["name"], "key": V.ref_key(e["name"])} for e in a.get("entities") or [] if e["type"] not in ("Party", "GoodsItem")],
              "remembered_at": now()}
        with _lock:
            rows = [r for r in self.all() if r.get("case") != ep["case"]] + [ep]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
            tmp.replace(self.path)
        return ep

    def recall(self, case: dict[str, Any], entities: list[dict[str, Any]]) -> dict[str, Any]:
        """What the workspace already knows that touches this case: the parties met before, and reference numbers another case quotes.
        Copies of the same bundled sample are left out, so loading a sample twice does not look like a duplicate."""
        sample = (case.get("sample") or {}).get("id")
        others = [e for e in self.all() if e.get("case") != case["reference"] and not (sample and e.get("sample") == sample and e.get("profile") == case.get("profile"))]
        parties, identifiers = [], []
        for ent in entities:
            if ent["type"] == "Party":
                seen = [e for e in others if any(p.get("key") == ent.get("key") for p in e.get("parties") or [])]
                if seen:
                    parties.append({"entity": ent["id"], "name": ent["name"], "cases": [{"case": e["case"], "use_case": e.get("use_case"), "label": e.get("label"), "outcome": e.get("outcome"), "at": e.get("at")} for e in seen[-5:]],
                                    "count": len(seen)})
            elif ent["type"] != "GoodsItem":
                key = V.ref_key(ent["name"])
                seen = [e for e in others if any(i.get("key") == key and i.get("type") == ent["type"] for i in e.get("identifiers") or [])]
                if seen and len(key) >= 4:
                    identifiers.append({"entity": ent["id"], "type": ent["type"], "value": ent["name"], "cases": [{"case": e["case"], "use_case": e.get("use_case"), "label": e.get("label"), "at": e.get("at")} for e in seen[-5:]],
                                        "count": len(seen)})
        return {"parties": parties, "identifiers": identifiers, "episodes_searched": len(others)}


    def seen_with(self, case: dict[str, Any], party_key: str, id_type: str) -> list[dict[str, Any]]:
        """The reference numbers of one kind (a bank account, say) that earlier cases quoted when this party was in them, oldest first.
        Copies of the same bundled sample are left out, as in `recall`."""
        sample = (case.get("sample") or {}).get("id")
        out = []
        for e in self.all():
            if e.get("case") == case["reference"] or (sample and e.get("sample") == sample and e.get("profile") == case.get("profile")):
                continue
            if party_key and any(p.get("key") == party_key for p in e.get("parties") or []):
                trusted = e.get("decision") == "approve" or (e.get("outcome") == "proceed" and e.get("decision") in (None, "approve"))   # went through clean, or a person approved it
                out += [{"case": e["case"], "at": e.get("at"), "value": i.get("value"), "key": i.get("key"), "trusted": trusted, "outcome": e.get("outcome"), "decision": e.get("decision")}
                        for i in e.get("identifiers") or [] if i.get("type") == id_type and i.get("key")]
        return out


class MemoryHandle:
    """Memory as one agent may use it in one run. The contract says which scopes it may read and write."""

    def __init__(self, agent: str, allowed: dict[str, list[str]] | None, working: list[dict[str, Any]] | None = None, episodes: Episodes | None = None):
        self.agent = agent
        self.allowed = {"read": list((allowed or {}).get("read") or []), "write": list((allowed or {}).get("write") or [])}
        self.working = working if working is not None else []
        self.episodes = episodes
        self.log: list[dict[str, Any]] = []

    def _check(self, mode: str, scope: str, key: str) -> None:
        ok = scope in self.allowed[mode]
        self.log.append({"mode": mode, "scope": scope, "key": key, "allowed": ok})
        if not ok:
            raise MemoryDenied(f"the contract of '{self.agent}' does not allow it to {mode} {scope} memory ({key})")

    def note(self, key: str, value: Any) -> None:
        """Leave a note in the run's working memory for the agents that come after."""
        self._check("write", "run", key)
        self.working.append({"key": key, "value": value, "by": self.agent, "at": now()})

    def notes(self, key: str | None = None) -> list[dict[str, Any]]:
        self._check("read", "run", key or "*")
        return [n for n in self.working if key is None or n["key"] == key]

    def recall(self, case: dict[str, Any], entities: list[dict[str, Any]]) -> dict[str, Any]:
        self._check("read", "workspace", "episodes")
        if self.episodes is None:
            return {"parties": [], "identifiers": [], "episodes_searched": 0}
        return self.episodes.recall(case, entities)

    def earlier(self, case: dict[str, Any], party_key: str, id_type: str) -> list[dict[str, Any]]:
        self._check("read", "workspace", f"episodes:{id_type}")
        return self.episodes.seen_with(case, party_key, id_type) if self.episodes is not None else []

    def summary(self) -> dict[str, Any]:
        return {"reads": [f"{x['scope']}:{x['key']}" for x in self.log if x["mode"] == "read" and x["allowed"]], "writes": [f"{x['scope']}:{x['key']}" for x in self.log if x["mode"] == "write" and x["allowed"]],
                "refused": [f"{x['mode']} {x['scope']}:{x['key']}" for x in self.log if not x["allowed"]]}
