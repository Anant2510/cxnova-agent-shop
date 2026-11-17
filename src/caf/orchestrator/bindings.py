"""Data bindings between orchestrator steps.

Step inputs (in playbooks or model-made plans) may reference the goal, shared run inputs and
upstream step outputs:
  "$goal"                              -> the run goal (string)
  "${input.url}"                       -> shared run input (raw value when it is the whole string)
  "${input.audience|goal}"             -> first non-empty of several sources
  "${steps.brief.output.campaign_name}"-> upstream output value (dotted path, list indices allowed)
  {"$ref": "steps.links.output.links"} -> raw value (lists / dicts)
Unresolvable references become None and are dropped so the agent's own defaults apply.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel

_REF = re.compile(r"\$\{([^}]+)\}")

# Fields that should receive the goal when the step leaves them empty (first match wins).
PRIMARY_TEXT_FIELDS = ["overview", "topic", "question", "test_idea", "change_request", "source_text", "campaign_type"]
URL_FIELDS = ["page_url", "url", "base_url", "start_url"]


def _walk(obj: Any, path: list[str]) -> Any:
    cur = obj
    for part in path:
        if cur is None:
            return None
        if isinstance(cur, BaseModel):
            cur = cur.model_dump()
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, (list, tuple)) and part.lstrip("-").isdigit():
            idx = int(part)
            cur = cur[idx] if -len(cur) <= idx < len(cur) else None
        else:
            return None
    return cur


def lookup(expr: str, *, goal: str, inputs: dict[str, Any], results: dict[str, Any]) -> Any:
    for option in expr.split("|"):
        option = option.strip()
        if option == "goal":
            value: Any = goal
        elif option.startswith("input."):
            value = _walk(inputs, option.split(".")[1:])
        elif option.startswith("steps."):
            parts = option.split(".")
            step = results.get(parts[1]) if len(parts) > 1 else None
            value = _walk(step, parts[2:]) if step is not None else None
        elif option.startswith("'") and option.endswith("'"):
            value = option[1:-1]
        else:
            value = None
        if value not in (None, "", [], {}):
            return value
    return None


def resolve(value: Any, *, goal: str, inputs: dict[str, Any], results: dict[str, Any]) -> Any:
    if isinstance(value, str):
        if value == "$goal":
            return goal
        whole = _REF.fullmatch(value.strip())
        if whole:
            return lookup(whole.group(1), goal=goal, inputs=inputs, results=results)

        def repl(m: re.Match) -> str:
            v = lookup(m.group(1), goal=goal, inputs=inputs, results=results)
            return "" if v is None else str(v)

        return _REF.sub(repl, value)
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            return lookup(str(value["$ref"]), goal=goal, inputs=inputs, results=results)
        return {k: resolve(v, goal=goal, inputs=inputs, results=results) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, goal=goal, inputs=inputs, results=results) for v in value]
    return value


def _prune(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _prune(v) for k, v in value.items() if v not in (None, "", [], {})}
    if isinstance(value, list):
        return [_prune(v) for v in value if v not in (None, "")]
    return value


def build_step_inputs(agent: Any, raw: dict[str, Any], *, goal: str, inputs: dict[str, Any],
                      results: dict[str, Any]) -> dict[str, Any]:
    """Resolve bindings, then fill the agent's fields from shared inputs and the goal."""
    resolved = _prune(resolve(dict(raw or {}), goal=goal, inputs=inputs, results=results))
    fields = agent.Input.model_fields
    out = {k: v for k, v in resolved.items() if k in fields}
    # shared inputs with matching names
    for name in fields:
        if name not in out and inputs.get(name) not in (None, "", [], {}):
            out[name] = inputs[name]
    # URL aliases
    shared_url = inputs.get("url") or inputs.get("page_url")
    if shared_url:
        for name in URL_FIELDS:
            if name in fields and name not in out:
                out[name] = shared_url
        if "urls" in fields and "urls" not in out:
            out["urls"] = [shared_url]
    # the goal feeds the agent's declared primary inputs (or the first matching text field)
    if goal:
        declared = getattr(agent.spec, "primary_inputs", None)
        if declared is not None:
            for name in declared:
                if name in fields and name not in out:
                    out[name] = goal
        else:
            for name in PRIMARY_TEXT_FIELDS:
                if name in fields and name not in out:
                    annotation = str(fields[name].annotation)
                    if "str" in annotation and "list" not in annotation.lower():
                        out[name] = goal
                        break
    return out
