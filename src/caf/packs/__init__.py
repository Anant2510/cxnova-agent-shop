"""Agent packs: the agents (and flows) of other CX Nova products in this layer's catalog. See caf.packs.model."""

from __future__ import annotations

from typing import Any

from .agent import HomeError, PackOutput, build_agent_class, build_prompt
from .model import Pack, PackAgent, load_packs, made_packs_dir, pack_of_agent, packs_dir

__all__ = ["HomeError", "Pack", "PackAgent", "PackOutput", "build_agent_class", "build_prompt", "list_packs", "load_packs",
           "made_packs_dir", "pack_agent_classes", "pack_of_agent", "pack_playbooks", "packs_dir", "reset_packs"]


def pack_agent_classes() -> list[type]:
    """One agent class per pack agent, in pack order (file name), then the order the pack lists them."""
    return [build_agent_class(pack, agent) for pack in load_packs().values() for agent in pack.agents]


def list_packs() -> list[dict[str, Any]]:
    return [p.summary() for p in load_packs().values()]


def pack_playbooks() -> dict[str, Any]:
    """The flows a pack ships (a product's own process), as playbooks the orchestrator can run by name. They are
    never picked by the routers or the planner by themselves (auto_route False): a caller or a flow names them."""
    from ..orchestrator.playbooks import Playbook, StepDef

    out: dict[str, Any] = {}
    for pack in load_packs().values():
        for f in pack.flows:
            out[f.id] = Playbook(id=f.id, name=f.name, description=f.description, triggers=f.triggers, inputs=[i["name"] for i in f.inputs if i.get("name")],
                                 steps=[StepDef(id=s.id, agent=s.agent, purpose=s.purpose, inputs=dict(s.inputs), needs=list(s.needs)) for s in f.steps],
                                 auto_route=False, pack=pack.id, pack_name=pack.name, input_defs=list(f.inputs))
    return out


def reset_packs() -> None:
    load_packs.cache_clear()
