"""Playbooks: reusable multi-agent workflows defined in config/playbooks.yaml.

The shop adds its own (published custom flows, composed in the admin console) through ``set_extra_source``; every
consumer of ``load_playbooks`` (the router, the planner, the console dropdown, ``caf playbooks``) sees them as
ordinary playbooks.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

import yaml
from pydantic import BaseModel, Field

from ..config import get_settings

log = logging.getLogger("caf.playbooks")


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
    auto_route: bool = Field(default=True, description="False: never picked by the routers or the planner; run by name only")
    pack: str = Field(default="", description="the agent pack that ships this flow (a product's own process)")
    pack_name: str = ""
    input_defs: list[dict[str, Any]] = Field(default_factory=list, description="[{name, description, required, example}] when declared")


@lru_cache(maxsize=1)
def builtin_playbooks() -> dict[str, Playbook]:
    """The playbooks shipped in config/playbooks.yaml."""
    path = Path(get_settings().home) / "config" / "playbooks.yaml"
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    books = {pid: Playbook(id=pid, **body) for pid, body in (raw.get("playbooks") or {}).items()}
    try:  # the flows the agent packs ship (config/packs/*.yaml): a product's own process, runnable by name
        from ..packs import pack_playbooks

        for pid, pb in pack_playbooks().items():
            books.setdefault(pid, pb)
    except Exception as exc:
        log.warning("pack flows unavailable: %s", exc)
    return books


_EXTRA_SOURCE: Callable[[], dict[str, Playbook]] | None = None


def set_extra_source(source: Callable[[], dict[str, Playbook]] | None) -> None:
    """Register the provider of additional playbooks (the shop's published custom flows); None removes it."""
    global _EXTRA_SOURCE
    _EXTRA_SOURCE = source


def load_playbooks() -> dict[str, Playbook]:
    """Built-in playbooks plus the extra source's. A built-in id always wins over a custom one."""
    books = dict(builtin_playbooks())
    if _EXTRA_SOURCE is not None:
        try:
            for pid, pb in _EXTRA_SOURCE().items():
                books.setdefault(pid, pb)
        except Exception as exc:  # a broken flow store must not take the orchestrator down
            log.warning("extra playbooks unavailable: %s", exc)
    return books


load_playbooks.cache_clear = builtin_playbooks.cache_clear  # type: ignore[attr-defined]  # tests reload the YAML this way


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
