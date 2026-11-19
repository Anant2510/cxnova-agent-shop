#!/usr/bin/env python3
"""Builds config/packs/arm_sdlc.yaml (+ arm_sdlc.contracts.json) from the ARM SDLC Factory's own configuration.

    python3 scripts/build_pack_arm.py --from /path/to/arm-sdlc-factory/config

Reads the factory's agents.yaml (the 51 agents), skills/*.yaml (each agent's system prompt, checklist and standards),
agent-contracts.json (what every agent action hands on, with a sample) and process/arm-ai-to-z.yaml (the 8 teams and
50 steps), and writes them as an agent pack: every agent with its actions and skill, the sample output per action as
its twin, and the release process as flows (one per team, and the whole release led by each step's lead agent).
Run it again whenever the factory's configuration changes; do not edit the generated files by hand.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import yaml

CATEGORY = {
    "front_velocity": "engineering", "design_velocity": "design", "app_velocity": "engineering", "intellisight": "analytics",
    "site_auditor": "quality", "brand_auditor": "compliance", "quality_digital_workers": "quality", "blueswan": "quality",
    "accessibility_validator": "quality", "localization_validator": "quality", "xtender": "content", "xops": "operations",
    "migrate_dxp": "engineering", "devops_runbook": "operations", "factory_nfr_spec": "engineering", "factory_unit_test": "quality",
    "factory_test_case_drafter": "quality", "factory_pipeline_quality": "operations", "factory_asana_sync": "operations",
    "factory_test_reviewer": "quality", "factory_test_strategy": "quality", "factory_automation_planner": "quality",
    "factory_automation_scripter": "quality", "factory_perf_test": "quality", "factory_vuln_scan": "quality",
    "factory_defect_logger": "quality", "factory_code_reviewer": "engineering", "factory_tracking_implementer": "analytics",
    "factory_onetrust_classifier": "compliance", "factory_evaluator": "quality", "factory_tool_smith": "engineering",
    # the SitecoreAI agents and platform APIs, by what they do (not "platform": the catalog groups agents by their work)
    "sitecore_brief_generator": "strategy", "sitecore_researcher": "strategy", "sitecore_strategy": "strategy",
    "sitecore_competitor_analyzer": "strategy", "sitecore_audience_summary": "analytics", "sitecore_market_signals": "analytics",
    "sitecore_performance": "analytics", "sitecore_persona_auditor": "compliance", "sitecore_design_studio": "design",
    "sitecore_structured_extractor": "content", "sitecore_marketer_mcp": "content", "sitecore_translator": "content",
    "sitecore_content_generator": "content", "sitecore_validate_accessibility": "quality", "sitecore_validate_page": "quality",
    "sitecore_forms": "engineering", "sitecore_app_studio": "engineering", "sitecore_cloud_sdk": "engineering",
    "sitecore_deploy_api": "operations", "sitecore_publishing_api": "activation",
}
FAMILY = {"coforge": "CX Nova accelerators", "sitecore": "SitecoreAI agents and platform APIs", "factory": "Factory agents",
          "controlled": "Controlled runbooks"}
TIER = {"smart": "balanced", "fast": "fast", "judge": "pro"}
SAMPLE_REQUEST = ("Arm Tech Symposia 2027: the event hub pages on the Arm events site (SitecoreAI XM Cloud with Next.js on ADS v5), in English "
                  "and Japanese, with Cvent registration, Marketo forms, OneTrust consent and Adobe Analytics tracking; go-live in six weeks.")
PREFIX = "arm_"


def trim(value, depth=0, max_str=220, max_items=2):
    """A sample small enough to ship and to show a model: long strings cut, long lists shortened, deep trees closed."""
    if isinstance(value, str):
        return value if len(value) <= max_str else value[:max_str].rstrip() + "…"
    if isinstance(value, list):
        return [trim(v, depth + 1, max_str, max_items) for v in value[:max_items]] if depth < 6 else []
    if isinstance(value, dict):
        if depth >= 6:
            return {}
        items = list(value.items())[:14]
        return {k: trim(v, depth + 1, max_str, max_items) for k, v in items}
    return value


def human(action: str) -> str:
    return action.replace("_", " ").capitalize()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="src", required=True, help="the factory's config folder")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[1] / "config" / "packs"))
    args = ap.parse_args()
    src, out = Path(args.src), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    catalogue = yaml.safe_load((src / "agents.yaml").read_text(encoding="utf-8"))
    contracts = json.loads((src / "agent-contracts.json").read_text(encoding="utf-8"))["contracts"]
    process = yaml.safe_load((src / "process" / "arm-ai-to-z.yaml").read_text(encoding="utf-8"))["process"]
    skills = {p.stem: yaml.safe_load(p.read_text(encoding="utf-8")) for p in sorted((src / "skills").glob("*.yaml"))}

    agents, pack_contracts = [], {}
    for a in catalogue["agents"]:
        skill = skills.get(a.get("skill") or a["id"]) or {}
        platform_only = set(a.get("needs_platform") or [])
        actions = []
        for act in a.get("actions") or []:
            c = contracts.get(f"{a['id']}.{act}") or {}
            arts = c.get("artifacts") or []
            example = {"result": trim(c.get("output_example") or {}),
                       "artifacts": [{"type": x.get("type"), "name": (x.get("name_examples") or [x.get("type")])[0], "content": trim(x.get("example"))}
                                     for x in arts if x.get("example") is not None][:3]}
            pack_contracts[f"{PREFIX}{a['id']}.{act}"] = {
                "output_keys": c.get("output_keys") or [],
                "artifacts": [{"type": x.get("type"), "required": bool(x.get("required")), "keys": x.get("keys") or []} for x in arts],
                "example": example}
            entry = {"id": act, "title": human(act), "produces": [x.get("type") for x in arts if x.get("type")]}
            if act in platform_only or a["family"] == "sitecore" and a.get("runner") == "api":
                entry["needs_home"] = True
            actions.append(entry)
        agents.append({
            "id": PREFIX + a["id"], "home_id": a["id"], "name": a["name"], "accelerator": FAMILY.get(a["family"], a["family"]), "family": a["family"],
            "category": CATEGORY.get(a["id"], "engineering"), "tier": TIER.get(a.get("model", "smart"), "balanced"),
            "summary": a["summary"], "description": " ".join(str(skill.get("purpose") or a["summary"]).split()),
            "keywords": [k for k in re.split(r"[^a-z0-9]+", a["name"].lower()) if len(k) > 3][:6], "guardrails": a.get("guardrails") or [],
            "use_cases": [SAMPLE_REQUEST], "actions": actions,
            "skill": {"system_prompt": str(skill.get("system_prompt") or "").strip(), "checklist": skill.get("checklist") or [],
                      "standards": skill.get("standards") or []}})

    known = {a["home_id"] for a in agents}
    steps = process["steps"]
    by_id = {s["id"]: s for s in steps}

    def step_agents(s):
        return [p for p in s.get("playbook") or [] if p.get("kind") == "agent" and p.get("id") in known]

    def sid(s, suffix=""):
        return f"s{int(s['number']):02d}" + (f"_{suffix}" if suffix else "")

    def ordered(flow_steps):
        """Dependencies first (the process lists steps by team column, not by order of work); ties keep the process order."""
        done, out, left = set(), [], list(flow_steps)
        while left:
            ready = [st for st in left if all(n in done for n in st["needs"])]
            if not ready:  # a cycle in the source would end here: keep what is left as it is
                out.extend(left)
                break
            st = ready[0]
            out.append(st)
            done.add(st["id"])
            left.remove(st)
        return out

    request_input = [{"name": "request", "description": "The release request: what is being built or changed, for which site and locales, by when",
                      "required": True, "example": SAMPLE_REQUEST}]
    flows = []
    for team in sorted(process["teams"], key=lambda t: t["order"]):
        team_steps = [s for s in steps if s["team"] == team["id"]]
        in_team = {s["id"] for s in team_steps}
        flow_steps = []
        for s in team_steps:
            ags = step_agents(s)
            if not ags:
                continue
            lead, rest = ags[0], ags[1:]
            needs = [sid(by_id[d]) for d in s.get("depends_on") or [] if d in in_team and step_agents(by_id[d])]
            flow_steps.append({"id": sid(s), "agent": PREFIX + lead["id"], "purpose": f"Step {s['number']}: {s['title']}",
                               "inputs": {"action": lead["action"]}, "needs": needs})
            used = set()
            for sp in rest:
                short = re.sub(r"^(factory|sitecore)_", "", sp["id"])[:18]
                while short in used:
                    short += "_2"
                used.add(short)
                flow_steps.append({"id": sid(s, short), "agent": PREFIX + sp["id"], "purpose": f"Step {s['number']} (specialist): {human(sp['action'])}",
                                   "inputs": {"action": sp["action"]}, "needs": [sid(s)]})
        if flow_steps:
            flows.append({"id": f"arm_stage_{team['order']}_{team['id']}", "name": f"Arm release · {team['order']}. {team['name']}", "stage": team["name"],
                          "description": f"{team.get('objective') or team['name']} Steps {team_steps[0]['number']} to {team_steps[-1]['number']} of Arm's "
                                         f"AI to Z release process: each step's lead agent, then its specialists in parallel.",
                          "inputs": request_input, "steps": ordered(flow_steps)})
    whole = []
    for s in steps:
        ags = step_agents(s)
        if not ags:
            continue
        whole.append({"id": sid(s), "agent": PREFIX + ags[0]["id"], "purpose": f"{by_id[s['id']]['team']} · step {s['number']}: {s['title']}",
                      "inputs": {"action": ags[0]["action"]}, "needs": [sid(by_id[d]) for d in s.get("depends_on") or [] if step_agents(by_id[d])]})
    flows.append({"id": "arm_ai_to_z_release", "name": "Arm release · AI to Z, end to end", "stage": "All 8 teams",
                  "description": f"Arm's whole AI to Z release process, {len(whole)} of its 50 steps led by an agent, in the order the process "
                                 f"gives them: from design through requirements, development, QA, build, reporting and production to release. "
                                 f"Each step is its lead agent; the per-team flows add the specialists.",
                  "inputs": request_input, "steps": ordered(whole)})

    pack = {
        "pack": {
            "id": "arm_sdlc", "name": "ARM SDLC Factory", "product": "ARM SDLC Factory", "tag": "ARM",
            "description": f"The {len(agents)} agents of the ARM SDLC Factory, which runs Arm's AI to Z web release process (8 teams, 50 steps): the "
                           "CX Nova accelerators, the factory's own agents, the SitecoreAI agents and platform APIs it drives, and the controlled "
                           "DevOps runbook; with the release process as flows.",
            "color": "#0f7a4a", "source": "generated by scripts/build_pack_arm.py from the factory's agents.yaml, skills, agent contracts and process",
            "context": "In this layer, the context pack your instructions mention is what you are given below: the REQUEST, the INPUTS, the CONTEXT "
                       "and the UPSTREAM WORK of earlier steps. Workspace facts that are not there are unknown: say so in a check with status warn "
                       "instead of assuming Arm's.",
        },
        "agents": agents, "flows": flows}

    class Dumper(yaml.SafeDumper):
        pass

    def str_rep(d, data):
        return d.represent_scalar("tag:yaml.org,2002:str", data, style="|" if "\n" in data else None)

    Dumper.add_representer(str, str_rep)
    head = ("# GENERATED by scripts/build_pack_arm.py from the ARM SDLC Factory's configuration. Do not edit by hand:\n"
            "# change the factory's agents.yaml, skills or process and run the script again.\n")
    (out / "arm_sdlc.yaml").write_text(head + yaml.dump(pack, Dumper=Dumper, sort_keys=False, allow_unicode=True, width=130), encoding="utf-8")
    (out / "arm_sdlc.contracts.json").write_text(json.dumps(pack_contracts, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"{len(agents)} agents, {sum(len(a['actions']) for a in agents)} actions, {len(flows)} flows "
          f"({', '.join(str(len(f['steps'])) for f in flows)} steps) -> {out / 'arm_sdlc.yaml'}")


if __name__ == "__main__":
    main()
