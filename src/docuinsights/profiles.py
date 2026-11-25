"""Use-case profiles: everything DocuInsights knows about a domain is declared in docuinsights/profiles/<id>.yaml.

Extra folders can be added with DOCUINSIGHTS_PROFILES_DIR (a profile there replaces a shipped one with the same id).
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
FIELD_TYPES = ("text", "party", "date", "money", "number", "code", "ref")


class ProfileError(ValueError):
    pass


class Profile:
    def __init__(self, raw: dict[str, Any], path: Path):
        self.raw = raw
        self.path = path
        self.id: str = raw["id"]
        self.name: str = raw.get("name") or self.id
        self.version: str = str(raw.get("version") or "1.0")
        self.description: str = raw.get("description") or ""
        self.brand: dict[str, Any] = raw.get("brand") or {}
        self.case: dict[str, Any] = raw.get("case") or {}
        self.types: dict[str, dict[str, Any]] = {t["id"]: t for t in raw.get("document_types") or []}
        self.entities: dict[str, Any] = raw.get("entities") or {}
        self.evidence: list[dict[str, Any]] = raw.get("evidence") or []
        self.references: list[dict[str, Any]] = raw.get("reference_sources") or []
        self.rules: list[dict[str, Any]] = raw.get("rules") or []
        self.consistency: list[dict[str, Any]] = raw.get("consistency") or []
        self.outcomes: dict[str, Any] = raw.get("outcomes") or {}
        self.counterfactuals: list[dict[str, Any]] = raw.get("counterfactuals") or []
        self.correspondence: dict[str, Any] = raw.get("correspondence") or {}
        self.synonyms: dict[str, dict[str, str]] = raw.get("synonyms") or {}
        self.samples: list[dict[str, Any]] = raw.get("samples") or []
        self.ontology: dict[str, Any] = raw.get("ontology") or {}
        self.order: int = int(raw.get("order") or 100)          # where the use case sits in a list: the plainest first
        self.sector: str = raw.get("sector") or ""              # the kind of organisation that runs it
        self.plain: str = raw.get("plain") or self.description  # what it does, in one sentence anyone can read
        self.memory: dict[str, Any] = raw.get("memory") or {}
        # the assurance agents: what each is told about this use case (all four work without a word here)
        self.integrity: dict[str, Any] = raw.get("integrity") or {}   # sums, check digits, dates and details that must not change
        self.screening: dict[str, Any] = raw.get("screening") or {}   # who is screened against the watchlists
        self.privacy: dict[str, Any] = raw.get("privacy") or {}       # personal data the use case does not expect
        self.service: dict[str, Any] = raw.get("service") or {}       # the service target a case is triaged against
        self.formats: dict[str, Any] = raw.get("formats") or {}       # how its documents write a date: {dates: day_first | month_first}
        order = self.formats.get("dates") or "day_first"
        if order not in ("day_first", "month_first"):
            raise ProfileError(f"{self.id}: formats.dates is day_first or month_first")
        for t in self.types.values():   # each date field reads the way the use case says, unless the field says otherwise itself
            for f in t.get("fields") or []:
                if f.get("type") == "date":
                    f.setdefault("order", order)
        self._check()

    def _check(self) -> None:
        for tid, t in self.types.items():
            for f in t.get("fields") or []:
                if f.get("type", "text") not in FIELD_TYPES:
                    raise ProfileError(f"{self.id}: field {tid}.{f.get('id')} has unknown type '{f.get('type')}'")
        known = set(self.types)
        for ev in self.evidence:
            unknown = [t for t in ev.get("any_of") or [] if t not in known]
            if unknown:
                raise ProfileError(f"{self.id}: evidence '{ev.get('id')}' names unknown document type(s) {unknown}")
        for group in (self.rules, self.consistency):
            for r in group:
                for ref in _refs_of(r):
                    head, _, fld = ref.partition(".")
                    if head == "case":
                        continue
                    if head not in known or fld not in {f["id"] for f in self.types[head].get("fields") or []}:
                        raise ProfileError(f"{self.id}: rule '{r.get('id')}' refers to unknown field '{ref}'")
        from . import assurance   # imported here: it reads profiles

        wrong = assurance.check_profile(self)
        if wrong:
            raise ProfileError(f"{self.id}: " + "; ".join(wrong))

    def type_name(self, type_id: str | None) -> str:
        return (self.types.get(type_id or "") or {}).get("name") or (type_id or "Unclassified")

    def field(self, type_id: str, field_id: str) -> dict[str, Any] | None:
        for f in (self.types.get(type_id) or {}).get("fields") or []:
            if f["id"] == field_id:
                return f
        return None

    def field_name(self, ref: str) -> str:
        head, _, fld = ref.partition(".")
        if head == "case":
            for f in self.case.get("fields") or []:
                if f["id"] == fld:
                    return f.get("name") or fld
            return fld.replace("_", " ")
        f = self.field(head, fld)
        return f"{self.type_name(head)} · {(f or {}).get('name') or fld}"

    def summary(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "version": self.version, "description": self.description, "brand": self.brand, "sector": self.sector, "plain": self.plain,
                "memory": self.memory, "service": self.service, "assurance": self._assurance(), "consistency": [{"id": r["id"], "title": r.get("title"), "kind": r.get("kind")} for r in self.consistency],
                "case": self.case, "document_types": [{"id": t["id"], "name": t.get("name") or t["id"], "group": t.get("group") or "",
                                                       "fields": [{"id": f["id"], "name": f.get("name") or f["id"], "type": f.get("type", "text"),
                                                                   "required": bool(f.get("required"))} for f in t.get("fields") or []]}
                                                      for t in self.types.values()],
                "evidence": self.evidence, "outcomes": self.outcomes, "ontology": self.ontology,
                "rules": [{"id": r["id"], "title": r.get("title"), "family": r.get("family"), "version": r.get("version"), "type": r.get("type")} for r in self.rules],
                "counterfactuals": [{"id": c["id"], "title": c.get("title"), "expect": c.get("expect"), "ask": c.get("ask"),
                                     "value": (c.get("change") or {}).get("value") if c.get("ask") else None} for c in self.counterfactuals],
                "samples": [{"id": s["id"], "title": s.get("title"), "path": s.get("path"), "story": s.get("story")} for s in self.samples],
                "reference_sources": [{"id": r["id"], "name": r.get("name"), "connector": r.get("connector")} for r in self.references]}

    def _assurance(self) -> list[dict[str, Any]]:
        from . import assurance

        return [{"key": r["key"], "title": r["title"], "kind": r["kind"], "family": r["family"]} for r in assurance.declared_rules(self)]


def _refs_of(rule: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for key in ("from", "to", "first", "then", "amount", "limit"):
        v = rule.get(key)
        if isinstance(v, str) and "." in v and " " not in v:
            out.append(v)
    for key in ("fields", "amounts", "codes"):
        out += [v for v in rule.get(key) or [] if isinstance(v, str) and "." in v]
    return out


def profile_dirs() -> list[Path]:
    extra = [Path(p) for p in (os.environ.get("DOCUINSIGHTS_PROFILES_DIR") or "").split(os.pathsep) if p.strip()]
    return [HERE / "profiles", *extra]


@lru_cache(maxsize=1)
def load_profiles() -> dict[str, Profile]:
    found: dict[str, Profile] = {}
    for folder in profile_dirs():
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.yaml")):
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if not raw.get("id"):
                raise ProfileError(f"{path.name}: a profile needs an id")
            found[raw["id"]] = Profile(raw, path)
    if not found:
        raise ProfileError("no profiles found")
    return dict(sorted(found.items(), key=lambda kv: (kv[1].order, kv[1].name)))


def get_profile(profile_id: str | None) -> Profile:
    profiles = load_profiles()
    if not profile_id:
        return next(iter(profiles.values()))
    if profile_id not in profiles:
        raise ProfileError(f"unknown profile '{profile_id}'; the profiles are: {', '.join(profiles)}")
    return profiles[profile_id]


def reset_profiles() -> None:
    load_profiles.cache_clear()
