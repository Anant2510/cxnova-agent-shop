"""The ontology of the knowledge base, and the naming rules shared with KB Forge.

Identifiers, class and relationship names follow the KB Forge knowledge-base specification (schema 1.0): an entity id
is `<class-slug>/<name-slug>`, classes are PascalCase, relationships camelCase with a domain, a range and an inverse,
and the property-graph relationship type is the SCREAMING_SNAKE form. Keeping to them is what lets a knowledge base
written here be read there, and the other way round.
"""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
# KB Forge's four levels of knowledge, top to bottom, under the names this graph shows them by. KB Forge calls the third
# "Coforge experience" (what a delivery practice has built and delivered); here it holds the accelerator and its agents.
LEVELS = {"industry": "Industry", "domain": "Domain", "solution": "Solution", "customer": "Customer"}
LEVEL_SAYS = {"industry": "The kinds of organisation the use cases serve.", "domain": "What each use case asks for: its kinds of document, its evidence, its rules, its references.",
              "solution": "The accelerator and its agents, each under its contract.", "customer": "The workspace: the organisations and goods met across cases, and each case with what it holds."}
KB_FORGE_LEVEL = {"industry": "industry", "domain": "domain", "experience": "solution", "coforge": "solution", "company": "customer", "customer": "customer", "solution": "solution"}
KB_FORGE_ID = {"industry": "industry", "domain": "domain", "solution": "coforge", "customer": "customer"}   # the level ids KB Forge writes on every entity (knowledgeLevel)


def kb_forge_levels(value: Any) -> list[str]:
    """What an entity's `knowledgeLevel` says, as KB Forge's level ids. KB Forge writes a list (an entity can be known at several levels,
    the first being the one it is shown at); one of its imports writes a single id. Anything else is not a level."""
    out: list[str] = []
    for x in value if isinstance(value, list) else [value]:
        lv = KB_FORGE_ID.get(KB_FORGE_LEVEL.get(str(x or "").strip().lower(), ""))
        if lv and lv not in out:
            out.append(lv)
    return out
COMMON_ATTRIBUTES = [{"name": "name", "datatype": "string", "description": "Primary human-readable label.", "required": True},
                     {"name": "description", "datatype": "text", "description": "What the entity is.", "required": False},
                     {"name": "synonyms", "datatype": "string[]", "description": "Other names for the same thing.", "required": False},
                     {"name": "confidence", "datatype": "decimal", "description": "0 to 1.", "required": False},
                     {"name": "sourceModule", "datatype": "string", "description": "What contributed it.", "required": False}]


def slugify(value: Any, max_len: int = 80) -> str:
    text = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (text[:max_len].rstrip("-") if len(text) > max_len else text) or "item"


def normalize_name(value: Any) -> str:
    """The key two names are matched on: case, punctuation, bracketed qualifiers and small words do not count."""
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"\(.*?\)", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\b(the|a|an|of|and|for)\b", " ", text)
    return " ".join(text.split())


def entity_id(type_name: str, name: Any) -> str:
    return f"{slugify(type_name, 40)}/{slugify(name)}"


def screaming_snake(value: str) -> str:
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(value))
    return re.sub(r"[^A-Za-z0-9]+", "_", text).upper().strip("_") or "RELATED_TO"


def camel(value: str) -> str:
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", str(value)) if p]
    if not parts:
        return "relatedTo"
    if len(parts) == 1 and not parts[0].isupper():
        return parts[0][:1].lower() + parts[0][1:]
    return parts[0].lower() + "".join(p[:1].upper() + p[1:].lower() for p in parts[1:])


def pascal(value: str) -> str:
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", str(value)) if p]
    out = "".join(p[:1].upper() + p[1:] for p in parts) or "Thing"
    return "C" + out if out[0].isdigit() else out


class Ontology:
    def __init__(self, raw: dict[str, Any]):
        self.version = str(raw.get("version") or "1.0")
        self.name = raw.get("name") or "ontology"
        self.description = raw.get("description") or ""
        self.classes: dict[str, dict[str, Any]] = {}
        self.relationships: dict[str, dict[str, Any]] = {}
        self.warnings: list[str] = []
        for c in raw.get("classes") or []:
            self.add_class(c["name"], parent=c.get("parent"), definition=c.get("definition", ""), attributes=c.get("attributes"), source=c.get("source") or "extension",
                           level=c.get("level"), label=c.get("label"), synonyms=c.get("synonyms"), depth=c.get("depth"))
        for r in raw.get("relationships") or []:
            self.add_relationship(r["name"], r.get("domain") or "Thing", r.get("range") or "Thing", inverse=r.get("inverse") or r.get("inverseName"), definition=r.get("definition", ""),
                                  source=r.get("source") or "extension", symmetric=bool(r.get("symmetric")), label=r.get("label"))

    def add_class(self, name: str, *, parent: str | None = None, definition: str = "", attributes: list[Any] | None = None, source: str = "extension", level: str | None = None,
                  label: str | None = None, synonyms: list[str] | None = None, depth: int | None = None) -> dict[str, Any]:
        if name in self.classes and not self.classes[name].get("placeholder"):
            return self.classes[name]
        if parent and parent != "Thing" and parent not in self.classes:   # named as a parent before it is described: a placeholder its own description replaces
            self.classes[parent] = {"name": parent, "label": _words(parent), "definition": "", "parent": None, "source": source, "attributes": [], "synonyms": [], "level": None, "depth": None,
                                    "placeholder": True}
        self.classes[name] = {"name": name, "label": label or _words(name), "definition": definition, "parent": parent if parent and parent != "Thing" else None, "source": source,
                              "attributes": [a if isinstance(a, dict) else {"name": str(a), "datatype": "string"} for a in attributes or []], "synonyms": list(synonyms or []), "level": level, "depth": depth}
        return self.classes[name]

    def add_relationship(self, name: str, domain: str = "Thing", range_: str = "Thing", *, inverse: str | None = None, definition: str = "", source: str = "extension",
                         symmetric: bool = False, label: str | None = None) -> dict[str, Any]:
        if name in self.relationships:
            return self.relationships[name]
        for end in (domain, range_):
            if end != "Thing" and end not in self.classes:
                self.warnings.append(f"relationship '{name}': unknown class '{end}' -> Thing")
        self.relationships[name] = {"name": name, "label": label or _words(name).lower(), "definition": definition, "domain": domain if domain in self.classes else "Thing",
                                    "range": range_ if range_ in self.classes else "Thing", "inverse": inverse or "", "symmetric": symmetric, "transitive": False,
                                    "cardinality": "many-to-many", "examples": [], "source": source, "alignments": [], "neo4jType": screaming_snake(name)}
        return self.relationships[name]

    def ancestors(self, cls: str) -> list[str]:
        out, seen = [], set()
        cur = (self.classes.get(cls) or {}).get("parent")
        while cur and cur not in seen:
            out.append(cur)
            seen.add(cur)
            cur = (self.classes.get(cur) or {}).get("parent")
        return out

    def is_a(self, cls: str, parent: str) -> bool:
        return parent == "Thing" or cls == parent or parent in self.ancestors(cls)

    def fits(self, predicate: str, subject_type: str, object_type: str) -> bool:
        r = self.relationships.get(predicate)
        return bool(r) and self.is_a(subject_type, r["domain"]) and self.is_a(object_type, r["range"])

    def level(self, cls: str) -> str | None:
        """Which of the four levels a class of this graph belongs to (its own, or the nearest ancestor's)."""
        for c in [cls, *self.ancestors(cls)]:
            lv = (self.classes.get(c) or {}).get("level")
            if lv:
                return str(lv)
        return None

    def depth(self, cls: str) -> int | None:
        for c in [cls, *self.ancestors(cls)]:
            d = (self.classes.get(c) or {}).get("depth")
            if d:
                return int(d)
        return None

    def labels(self, cls: str) -> list[str]:
        return [cls, *self.ancestors(cls), "Entity"]

    def merged(self) -> dict[str, Any]:
        """The ontology as KB Forge keeps it per knowledge base (ontology/ontology_merged.json)."""
        return {"classes": [{k: v for k, v in c.items() if k not in ("level", "depth", "placeholder")} | {"knowledgeLevel": self.level(c["name"]) if c["source"] != "import" else None, "graphLevel": self.depth(c["name"]) if c["source"] != "import" else None,
                             "examples": []} for c in sorted(self.classes.values(), key=lambda c: c["name"])],
                "relationships": sorted(self.relationships.values(), key=lambda r: r["name"]), "commonAttributes": COMMON_ATTRIBUTES, "attributeAlignments": {}, "warnings": list(self.warnings)}

    def extension(self) -> dict[str, Any]:
        """Only what this ontology adds to KB Forge's own, in the format of a KB Forge custom ontology."""
        return {"name": self.name, "version": self.version, "description": self.description,
                "classes": [{"name": c["name"], "parent": c["parent"], "definition": c["definition"], "attributes": c["attributes"], "synonyms": c["synonyms"]}
                            for c in self.classes.values() if c["source"] != "core"],
                "relationships": [{"name": r["name"], "domain": r["domain"], "range": r["range"], "definition": r["definition"], "inverseName": r["inverse"], "symmetric": r["symmetric"]}
                                  for r in self.relationships.values() if r["source"] != "core"]}


def _words(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)


@lru_cache(maxsize=1)
def _raw() -> dict[str, Any]:
    return yaml.safe_load((HERE / "foundation" / "ontology.yaml").read_text(encoding="utf-8"))


def load() -> Ontology:
    """A fresh copy each time: an import may add classes to it without touching the shipped one."""
    return Ontology(_raw())
