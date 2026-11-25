"""The quality standard every agent on the shelf is held to, and the audit that checks it.

An agent is sold on what it returns, so the standard is about what its figures rest on:

  1. Every action says what its figures rest on (its basis: calculated, authored, judgement, measured or executed; see
     caf.packs.model). An action whose sample output carries numbers and names no basis is unclassified: nobody decided
     whether those numbers are computed or made up. That fails the audit.
  2. A calculated action has code behind it. A model does not calculate; when the code cannot do the work the step fails
     and says what is missing, and nothing stands in for it (`code_only`).
  3. An agent that reads data says the least it needs (`minimum`) and refuses to run on less when it runs on its own.
  4. A measured action names the inputs that carry measurements (`evidence`); without them it reports no figure.
  5. An executed action is a dry run unless its platform did the work, and says so.
  6. Where a model writes and arithmetic can check (a cost against a limit, a length against a channel), code checks (`verify`).
  7. An agent's own self-test passes its own minimum.

`caf agents audit` prints the result; tests/test_agent_quality.py fails the build on any `fail` finding in a shipped pack."""

from __future__ import annotations

from typing import Any

from .packs.inputs import missing_minimum
from .packs.model import BASES, Pack, PackAction, PackAgent, load_packs

STANDING = {
    "calculates": "Calculates in code. Refuses when the inputs do not carry the calculation; nothing stands in for it.",
    "calculates_or_stands_in": "Calculates in code; if the code fails a model stands in and the result is marked as its judgement.",
    "writes_checked": "Written by the model from what it is given; the arithmetic in it is checked by code.",
    "writes": "Written by the model from what it is given. Its content is authored, not measured.",
    "judges": "The model's assessment of what it is given, labelled as one.",
    "reports": "Reports figures only when they are in its inputs; it cannot observe anything itself on this layer.",
    "plans": "Plans the work. Only its platform can run or change anything: on this layer it is a dry run.",
}


def has_numbers(value: Any) -> bool:
    """A sample output that carries figures: numbers, or text that reads as one (a percentage, a duration, a score)."""
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        return "%" in value and any(ch.isdigit() for ch in value)
    if isinstance(value, dict):
        return any(has_numbers(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(has_numbers(v) for v in value)
    return False


def standing(agent: PackAgent) -> str:
    bases = {a.grounds for a in agent.actions}
    if agent.code:
        return "calculates" if agent.code_only else "calculates_or_stands_in"
    if bases <= {"executed"} or (agent.needs_home and "executed" in bases):
        return "plans"
    if "measured" in bases and not bases & {"authored", "judgement"}:
        return "reports"
    if agent.verify:
        return "writes_checked"
    if "judgement" in bases and "authored" not in bases:
        return "judges"
    return "writes"


def _finding(level: str, rule: str, where: str, message: str) -> dict[str, str]:
    return {"level": level, "rule": rule, "where": where, "message": message}


def audit_agent(pack: Pack, agent: PackAgent, *, made: bool = False) -> dict[str, Any]:
    findings: list[dict[str, str]] = []
    declared = {i.name for i in agent.inputs}
    data_inputs = [i.name for i in agent.inputs if i.file or i.type in ("any", "list", "object")]
    actions = []
    for action in agent.actions:
        ref = f"{agent.id}.{action.id}"
        explicit = bool(action.basis) or action.needs_home or agent.needs_home
        if not explicit and has_numbers(action.example):
            if agent.code:   # its code produces the figures: calculated when the code ran, and the result says which it was
                findings.append(_finding("info", "basis_not_declared", ref, "coded, with no declared basis: reported as calculated when its code "
                                         "ran; declare `basis` so its page says so" + ("" if agent.code_only else ", and `code_only` if nothing may stand in")))
            else:
                findings.append(_finding("warn" if made else "fail", "unclassified", ref,
                                         "its sample output carries figures and nothing says what they rest on"))
        if action.grounds == "calculated" and not agent.code:
            findings.append(_finding("fail", "calculated_without_code", ref, "declared calculated, but the agent has no code: a model would be doing the arithmetic"))
        unknown = [n for n in action.evidence if n not in declared]
        if unknown:
            findings.append(_finding("fail", "evidence_not_an_input", ref, f"evidence names inputs the agent does not have: {', '.join(unknown)}"))
        if action.grounds == "measured" and not agent.code and not action.evidence:
            findings.append(_finding("warn", "measured_without_evidence", ref,
                                     "reports measurements but has no input that could carry them: on this layer it can only say what is not measured"))
        actions.append({"id": action.id, "title": action.title, "basis": action.grounds, "declared": explicit, "evidence": list(action.evidence),
                        "sample_has_figures": has_numbers(action.example)})
    if agent.code_only and not agent.code:
        findings.append(_finding("fail", "code_only_without_code", agent.id, "code_only is set but there is no code"))
    names = [str(n) for entry in agent.minimum for n in (entry if isinstance(entry, (list, tuple)) else [entry])]
    unknown = [n for n in names if n not in declared]
    if unknown:
        findings.append(_finding("fail", "minimum_not_an_input", agent.id, f"minimum names inputs the agent does not have: {', '.join(unknown)}"))
    if agent.minimum:
        try:   # the inputs its self-test really runs on (the registry builds them from the examples and the declared sample)
            from .core import get_agent

            sample = get_agent(agent.id).selftest_input()
        except Exception:
            sample = dict(agent.selftest)
        short = missing_minimum(agent, sample)
        if short:
            findings.append(_finding("fail", "selftest_below_minimum", agent.id, f"its own self-test does not meet its minimum: {short}"))
    if not agent.code and not agent.minimum:
        if data_inputs:
            findings.append(_finding("info", "no_minimum", agent.id, f"takes data ({', '.join(data_inputs[:4])}) but declares no minimum: it runs on a request alone"))
        elif not agent.inputs:
            findings.append(_finding("info", "request_only", agent.id, "has no inputs of its own: it works from the request and the context it is given"))
    return {"id": agent.id, "name": agent.name, "pack": pack.id, "runner": "code" if agent.code else "model", "code_only": agent.code_only,
            "verify": bool(agent.verify), "minimum": list(agent.minimum), "capabilities": len(agent.capabilities), "standing": standing(agent),
            "standing_text": STANDING[standing(agent)], "actions": actions, "findings": findings}


def audit(pack_id: str | None = None) -> dict[str, Any]:
    """Every pack agent against the standard: {packs: [{id, name, agents, actions, by_basis, by_standing, findings}], agents: [...], totals}."""
    from .workshop import MADE_PACK

    packs, agents = [], []
    for pack in load_packs().values():
        if pack_id and pack.id != pack_id:
            continue
        made = pack.id == MADE_PACK["id"]
        rows = [audit_agent(pack, a, made=made) for a in pack.agents]
        by_basis = {b: sum(1 for r in rows for a in r["actions"] if a["basis"] == b) for b in BASES}
        by_standing: dict[str, int] = {}
        for r in rows:
            by_standing[r["standing"]] = by_standing.get(r["standing"], 0) + 1
        findings = [f for r in rows for f in r["findings"]]
        packs.append({"id": pack.id, "name": pack.name, "made_here": made, "agents": len(rows), "actions": sum(len(r["actions"]) for r in rows),
                      "by_basis": {k: v for k, v in by_basis.items() if v}, "by_standing": by_standing,
                      "with_code": sum(1 for r in rows if r["runner"] == "code"), "with_minimum": sum(1 for r in rows if r["minimum"]),
                      "with_verify": sum(1 for r in rows if r["verify"]),
                      "findings": {lvl: sum(1 for f in findings if f["level"] == lvl) for lvl in ("fail", "warn", "info")}})
        agents.extend(rows)
    fails = [f for r in agents for f in r["findings"] if f["level"] == "fail"]
    return {"packs": packs, "agents": agents, "ok": not fails,
            "totals": {"agents": len(agents), "actions": sum(len(r["actions"]) for r in agents),
                       "fail": len(fails), "warn": sum(1 for r in agents for f in r["findings"] if f["level"] == "warn"),
                       "info": sum(1 for r in agents for f in r["findings"] if f["level"] == "info")}}


def render(report: dict[str, Any], *, verbose: bool = False) -> str:
    lines = []
    for p in report["packs"]:
        basis = ", ".join(f"{n} {b}" for b, n in p["by_basis"].items())
        lines.append(f"\n{p['name']} ({p['id']}): {p['agents']} agents, {p['actions']} actions: {basis}")
        lines.append(f"  code {p['with_code']} | minimum {p['with_minimum']} | checked by code {p['with_verify']} | "
                     f"fail {p['findings']['fail']}, warn {p['findings']['warn']}, info {p['findings']['info']}")
        for key, n in sorted(p["by_standing"].items(), key=lambda kv: -kv[1]):
            lines.append(f"  {n:>3}  {STANDING[key]}")
    shown = ("fail", "warn", "info") if verbose else ("fail", "warn")
    for level in shown:
        rows = [f for a in report["agents"] for f in a["findings"] if f["level"] == level]
        if rows:
            lines.append(f"\n{level.upper()} ({len(rows)})")
            lines.extend(f"  {f['where']:<52} {f['rule']}: {f['message']}" for f in rows)
    t = report["totals"]
    lines.append(f"\n{t['agents']} agents, {t['actions']} actions: {t['fail']} fail, {t['warn']} warn, {t['info']} info"
                 + ("" if verbose or not t["info"] else "  (--verbose lists the info findings)"))
    lines.append("PASS: every action says what its figures rest on" if report["ok"] else "FAIL: see the findings above")
    return "\n".join(lines)


__all__ = ["audit", "audit_agent", "render", "standing", "has_numbers", "STANDING", "PackAction"]
