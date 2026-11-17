"""Playbooks: reusable multi-agent workflows defined in config/playbooks.yaml."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from ..config import get_settings


class StepDef(BaseModel):
    id: str
    agent: str
    purpose: str = ""
    inputs: dict[str, Any] = Field(default_factory=dict)
    needs: list[str] = Field(default_factory=list)


class Playbook(BaseModel):
    id: str
    name: str
    description: str = ""
    triggers: list[str] = Field(default_factory=list)
    inputs: list[str] = Field(default_factory=list, description="Shared run inputs this playbook uses")
    steps: list[StepDef]


@lru_cache(maxsize=1)
def load_playbooks() -> dict[str, Playbook]:
    path = Path(get_settings().home) / "config" / "playbooks.yaml"
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {pid: Playbook(id=pid, **body) for pid, body in (raw.get("playbooks") or {}).items()}


def validate_playbooks() -> list[str]:
    """Return problems (unknown agents, bad dependencies) across all playbooks."""
    from ..core import get_agent

    problems: list[str] = []
    for pb in load_playbooks().values():
        ids: set[str] = set()
        for step in pb.steps:
            try:
                get_agent(step.agent)
            except KeyError:
                problems.append(f"{pb.id}.{step.id}: unknown agent '{step.agent}'")
            for dep in step.needs:
                if dep not in ids:
                    problems.append(f"{pb.id}.{step.id}: needs '{dep}' which is not an earlier step")
            ids.add(step.id)
    return problems


def coverage() -> dict[str, list[str]]:
    """agent_id -> playbooks that include it."""
    out: dict[str, list[str]] = {}
    for pb in load_playbooks().values():
        for step in pb.steps:
            out.setdefault(step.agent, []).append(pb.id)
    return out
