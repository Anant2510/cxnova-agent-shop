"""Skill store: versioned YAML configuration (brand voice, taxonomies, rule packs, channel registries).

Skills make the behaviour contract configuration rather than code: a brand team can change the
seven brief sections, LinkedIn limits or SEO rules without touching Python.
"""

from __future__ import annotations

import copy
import threading
from pathlib import Path
from typing import Any

import yaml


class SkillNotFound(KeyError):
    pass


class SkillStore:
    def __init__(self, dirs: list[Path]):
        self.dirs = [Path(d) for d in dirs]
        self._cache: dict[str, Any] = {}
        self._lock = threading.Lock()

    def _path(self, name: str) -> Path | None:
        rel = name if name.endswith((".yaml", ".yml")) else f"{name}.yaml"
        for d in self.dirs:
            p = d / rel
            if p.exists():
                return p
        return None

    def exists(self, name: str) -> bool:
        return self._path(name) is not None

    def get(self, name: str, default: Any = ...) -> Any:
        """Return a deep copy of the skill so callers can mutate safely."""
        with self._lock:
            if name not in self._cache:
                path = self._path(name)
                if path is None:
                    if default is not ...:
                        return copy.deepcopy(default)
                    raise SkillNotFound(f"Skill '{name}' not found in {[str(d) for d in self.dirs]}")
                self._cache[name] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            return copy.deepcopy(self._cache[name])

    def list(self) -> list[str]:
        names: set[str] = set()
        for d in self.dirs:
            if d.exists():
                for p in d.rglob("*.yaml"):
                    names.add(str(p.relative_to(d))[:-5])
        return sorted(names)

    def all(self) -> dict[str, Any]:
        return {n: self.get(n) for n in self.list()}

    def reload(self) -> None:
        with self._lock:
            self._cache.clear()
