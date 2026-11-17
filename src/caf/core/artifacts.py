"""Artifact store: every file an agent produces lands under runs/<run_id>/<agent_id>/."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .spec import ArtifactRef

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_name(name: str) -> str:
    cleaned = _SAFE.sub("-", name).strip("-")
    return cleaned or "artifact"


class ArtifactStore:
    def __init__(self, runs_dir: Path, run_id: str, agent_id: str | None = None):
        self.runs_dir = runs_dir
        self.run_id = run_id
        self.agent_id = agent_id

    @property
    def folder(self) -> Path:
        p = self.runs_dir / self.run_id
        if self.agent_id:
            p = p / self.agent_id
        p.mkdir(parents=True, exist_ok=True)
        return p

    def for_agent(self, agent_id: str) -> "ArtifactStore":
        return ArtifactStore(self.runs_dir, self.run_id, agent_id)

    def _ref(self, path: Path, mime: str, description: str) -> ArtifactRef:
        return ArtifactRef(
            name=path.name,
            mime=mime,
            path=path.relative_to(self.runs_dir).as_posix(),  # "run/agent/file" on every OS: it becomes a URL
            size=path.stat().st_size,
            description=description,
        )

    def _path(self, name: str) -> Path:
        """'a/b/c.json' -> <agent folder>/a/b/c.json with every segment sanitised; never escapes the folder."""
        parts = [safe_name(p) for p in str(name).replace("\\", "/").split("/") if p not in ("", ".", "..")]
        path = self.folder.joinpath(*(parts or ["artifact"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def write_text(self, name: str, content: str, mime: str = "text/plain", description: str = "") -> ArtifactRef:
        path = self._path(name)
        path.write_text(content, encoding="utf-8")
        return self._ref(path, mime, description)

    def write_bytes(self, name: str, data: bytes, mime: str, description: str = "") -> ArtifactRef:
        path = self._path(name)
        path.write_bytes(data)
        return self._ref(path, mime, description)

    def write_json(self, name: str, obj: Any, description: str = "") -> ArtifactRef:
        return self.write_text(name, json.dumps(obj, indent=2, default=str, ensure_ascii=False), "application/json", description)

    def resolve(self, ref: ArtifactRef | dict | str) -> Path:
        rel = ref.path if isinstance(ref, ArtifactRef) else (ref["path"] if isinstance(ref, dict) else ref)
        return self.runs_dir / rel
