"""Agent registry. The blueprint (config/blueprint.yaml) defines the 28 agents that must be live;
agent classes register themselves with @register and are enriched with blueprint metadata."""

from __future__ import annotations

import importlib
import pkgutil
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from ..config import get_settings
from .agent import BaseAgent
from .spec import AgentSpec

_CLASSES: dict[str, type[BaseAgent]] = {}
_INSTANCES: dict[str, BaseAgent] = {}
_PACK_ORDER: dict[str, int] = {}   # pack agents keep the order their pack lists them in, after the blueprint's
_LOCK = threading.Lock()
_LOAD_LOCK = threading.RLock()   # one loader at a time; _LOCK stays free for register() while agent modules import
_LOADED = False
_LOADING = False


@lru_cache(maxsize=1)
def blueprint() -> dict[str, dict[str, Any]]:
    path = Path(get_settings().home) / "config" / "blueprint.yaml"
    if not path.exists():
        return {}
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {a["id"]: a for a in doc.get("agents", [])}


def expected_agent_ids() -> list[str]:
    return list(blueprint().keys())


def _enrich(spec: AgentSpec) -> AgentSpec:
    bp = blueprint().get(spec.id)
    if not bp:
        return spec
    updates = {
        "use_cases": spec.use_cases or bp.get("use_cases", []),
        "independence": spec.independence or bp.get("independence", ""),
        "opal_reference": spec.opal_reference or bp.get("opal_reference", ""),
        "vendor_requirement_removed": spec.vendor_requirement_removed or bp.get("vendor_requirement_removed"),
        "description": spec.description or spec.summary,
    }
    return spec.model_copy(update=updates)


def register(cls: type[BaseAgent]) -> type[BaseAgent]:
    if not hasattr(cls, "spec"):
        raise TypeError(f"{cls.__name__} has no spec")
    cls.spec = _enrich(cls.spec)
    with _LOCK:
        _CLASSES[cls.spec.id] = cls
        _INSTANCES.pop(cls.spec.id, None)
    return cls


def load_agents() -> None:
    """Import every module under caf.agents so all @register decorators run, then register the agent packs.

    The first callers can be several threads at once (the health probe's workers, the API's thread pool): one of
    them loads, the others wait for it."""
    global _LOADED, _LOADING
    if _LOADED:
        return
    with _LOAD_LOCK:
        if _LOADED or _LOADING:   # loaded while this thread waited, or re-entered by an import during the load
            return
        _LOADING = True
        try:
            import caf.agents as pkg

            for mod in pkgutil.walk_packages(pkg.__path__, prefix="caf.agents."):
                importlib.import_module(mod.name)
            # the agents other CX Nova products bring (config/packs/*.yaml): declared, not coded
            from ..packs import pack_agent_classes

            seen: set[str] = set()
            for i, cls in enumerate(pack_agent_classes()):
                aid = cls.spec.id
                if aid in seen:
                    raise ValueError(f"pack agent id '{aid}' is declared twice in config/packs")
                if aid in _CLASSES and aid not in _PACK_ORDER:   # a pack may not replace a coded agent
                    raise ValueError(f"pack agent id '{aid}' is already taken by another agent")
                seen.add(aid)
                register(cls)
                _PACK_ORDER[aid] = i
            _LOADED = True
        finally:
            _LOADING = False


def get_agent(agent_id: str) -> BaseAgent:
    load_agents()
    with _LOCK:
        if agent_id not in _CLASSES:
            raise KeyError(f"Unknown agent '{agent_id}'. Known: {sorted(_CLASSES)}")
        if agent_id not in _INSTANCES:
            _INSTANCES[agent_id] = _CLASSES[agent_id]()
        return _INSTANCES[agent_id]


def all_agents() -> list[BaseAgent]:
    load_agents()
    order = {aid: i for i, aid in enumerate(expected_agent_ids())}
    ids = sorted(_CLASSES, key=lambda a: (order.get(a, 999 + _PACK_ORDER.get(a, 0)), a))
    return [get_agent(a) for a in ids]


def blueprint_agents() -> list[BaseAgent]:
    """The agents of the Coforge Independent Blueprint (the ones the routers and the planner choose from)."""
    return [a for a in all_agents() if a.spec.auto_route]


def reload_agents() -> None:
    """Forget the loaded pack agents and load again (tests switch packs on and off)."""
    global _LOADED
    with _LOAD_LOCK, _LOCK:
        for aid in list(_PACK_ORDER):
            _CLASSES.pop(aid, None)
            _INSTANCES.pop(aid, None)
        _PACK_ORDER.clear()
        _LOADED = False


def missing_agents() -> list[str]:
    load_agents()
    return [a for a in expected_agent_ids() if a not in _CLASSES]


def catalog(full: bool = False) -> list[dict[str, Any]]:
    out = []
    for agent in all_agents():
        if full:
            out.append(agent.describe())
        else:
            s = agent.spec
            out.append({"id": s.id, "name": s.name, "category": s.category.value, "category_label": s.category_label,
                        "summary": s.summary, "kind": s.kind.value, "tier": s.tier.value, "connectors": s.connectors,
                        "keywords": s.keywords, "feeds": s.feeds, "side_effects": s.side_effects,
                        "pack": s.pack, "pack_name": s.pack_name, "accelerator": s.accelerator,
                        "actions": [a["id"] for a in s.actions], "runners": s.runners})
    return out
