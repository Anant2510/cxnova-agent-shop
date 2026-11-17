"""Offline model: a deterministic, network-free stand-in for an LLM.

Used by the test-suite, CI liveness probes and air-gapped demos. It produces outputs that satisfy
the requested Pydantic schema (types, enums, length and range constraints) using values lifted
from the prompt, so every agent's full graph - deterministic stages, validators, exporters and
connectors - can run end-to-end without a model key. Every generated string is marked
"[offline]" so placeholder copy can never be mistaken for real model output.
"""

from __future__ import annotations

import re
import types
import typing
from enum import Enum
from typing import Any, Literal, get_args, get_origin

import annotated_types as at
from pydantic import BaseModel
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined

MARK = "[offline]"
_KV = re.compile(r"^\s*(?:[-*]\s*)?([A-Za-z][A-Za-z0-9 _/()-]{1,40}?)\s*[:=]\s*(.{2,300})$")


def _humanize(name: str) -> str:
    return name.replace("_", " ").strip().capitalize()


def _singular(name: str) -> str:
    n = name.rstrip()
    if n.endswith("ies"):
        return n[:-3] + "y"
    if n.endswith("s") and not n.endswith("ss"):
        return n[:-1]
    return n


def parse_prompt_facts(prompt: str) -> dict[str, str]:
    facts: dict[str, str] = {}
    for line in prompt.splitlines():
        m = _KV.match(line)
        if m:
            key = m.group(1).strip().lower().replace(" ", "_")
            val = m.group(2).strip().strip('"')
            if val and val.lower() not in ("none", "null", "n/a", "[]", "{}"):
                facts.setdefault(key, val)
    return facts


def _topic(facts: dict[str, str]) -> str:
    for k in ("topic", "campaign_type", "campaign", "title", "subject", "objective", "overview", "page_url", "url",
              "goal", "test_idea", "idea", "competitor", "industry", "question", "audience"):
        if k in facts:
            return facts[k][:80]
    return next(iter(facts.values()), "")[:80] if facts else ""


def _constraints(info: FieldInfo | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if info is None:
        return out
    for m in info.metadata:
        if isinstance(m, at.MinLen):
            out["min_len"] = m.min_length
        elif isinstance(m, at.MaxLen):
            out["max_len"] = m.max_length
        elif isinstance(m, at.Ge):
            out["ge"] = m.ge
        elif isinstance(m, at.Gt):
            out["gt"] = m.gt
        elif isinstance(m, at.Le):
            out["le"] = m.le
        elif isinstance(m, at.Lt):
            out["lt"] = m.lt
        elif hasattr(m, "min_length") and getattr(m, "min_length") is not None:
            out["min_len"] = m.min_length
        elif hasattr(m, "max_length") and getattr(m, "max_length") is not None:
            out["max_len"] = m.max_length
    return out


class OfflineModel:
    name = "offline"

    def complete(self, prompt: str, system: str | None = None) -> str:
        facts = parse_prompt_facts(prompt)
        topic = _topic(facts) or "the requested task"
        lines = [f"{MARK} Draft generated without a language model for: {topic}.", ""]
        headings = re.findall(r"^#{1,3}\s+(.+)$", prompt, flags=re.M)[:8]
        if headings:
            for h in headings:
                lines += [f"## {h.strip()}", f"{MARK} Placeholder for '{h.strip()}' about {topic}.", ""]
        else:
            for k, v in list(facts.items())[:6]:
                lines.append(f"- {_humanize(k)}: {v}")
        lines += ["", f"{MARK} Configure CAF_LLM_PROVIDER (anthropic | openai | azure_openai | bedrock | ollama) for real copy."]
        return "\n".join(lines).strip()

    def structured(self, prompt: str, schema: type[BaseModel], system: str | None = None) -> BaseModel:
        facts = parse_prompt_facts(prompt)
        data = self._model(schema, facts, depth=0)
        return schema.model_validate(data)

    # --- schema walker -------------------------------------------------------------------------
    def _model(self, schema: type[BaseModel], facts: dict[str, str], depth: int) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, info in schema.model_fields.items():
            out[name] = self._value(name, info.annotation, info, facts, depth)
        return out

    def _value(self, name: str, ann: Any, info: FieldInfo | None, facts: dict[str, str], depth: int) -> Any:
        cons = _constraints(info)
        origin = get_origin(ann)
        args = get_args(ann)

        if origin is typing.Annotated:
            inner, *meta = args
            fi = FieldInfo.from_annotation(ann) if info is None else info
            return self._value(name, inner, fi, facts, depth)

        if origin in (typing.Union, types.UnionType):
            non_none = [a for a in args if a is not type(None)]
            if info is not None and info.default not in (PydanticUndefined, None) and depth > 0:
                return info.default
            return self._value(name, non_none[0], info, facts, depth) if non_none else None

        if origin is Literal:
            if info is not None and info.default not in (PydanticUndefined, None) and info.default in args:
                return info.default
            return args[0]

        if isinstance(ann, type) and issubclass(ann, Enum):
            return list(ann)[0].value

        if isinstance(ann, type) and issubclass(ann, BaseModel):
            return self._model(ann, facts, depth + 1) if depth < 6 else {}

        if origin in (list, set, tuple, typing.List) or ann in (list,):
            item_t = args[0] if args else str
            lo = cons.get("min_len", 0)
            hi = cons.get("max_len", 99)
            n = max(lo, min(3, hi)) if depth < 3 else max(lo, min(1, hi))
            items = [self._item(name, item_t, facts, depth, i) for i in range(n)]
            return items

        if origin in (dict, typing.Dict) or ann is dict:
            return {}

        if ann is bool:
            return info.default if info is not None and isinstance(info.default, bool) else False

        if ann in (int, float):
            if info is not None and info.default not in (PydanticUndefined, None):
                return info.default
            lo = cons.get("ge", cons.get("gt", 0))
            hi = cons.get("le", cons.get("lt", 100))
            val = lo + (1 if "gt" in cons else 0)
            val = min(max(val, 1 if lo <= 1 <= hi else lo), hi)
            return int(val) if ann is int else float(val)

        if ann is str or ann is Any or ann is None:
            return self._text(name, facts, cons, info)

        try:  # date, datetime, HttpUrl ... fall back to the default or a string
            if info is not None and info.default not in (PydanticUndefined, None):
                return info.default
        except Exception:
            pass
        return self._text(name, facts, cons, info)

    def _item(self, name: str, item_t: Any, facts: dict[str, str], depth: int, i: int) -> Any:
        origin = get_origin(item_t)
        if isinstance(item_t, type) and issubclass(item_t, BaseModel):
            data = self._model(item_t, facts, depth + 1)
            for k, v in data.items():  # make items distinguishable
                if isinstance(v, str) and v.startswith(MARK):
                    data[k] = f"{v} {i + 1}"
            return data
        if item_t is str or origin is typing.Annotated:
            cons = {}
            if origin is typing.Annotated:
                cons = _constraints(FieldInfo.from_annotation(item_t))
            text = f"{MARK} {_humanize(_singular(name))} {i + 1}"
            return text[: cons.get("max_len", 10_000)]
        return self._value(name, item_t, None, facts, depth + 1)

    def _text(self, name: str, facts: dict[str, str], cons: dict[str, Any], info: FieldInfo | None) -> str:
        if info is not None and isinstance(info.default, str) and info.default:
            return info.default
        key = name.lower()
        direct = facts.get(key)
        if direct is None:
            toks = set(key.split("_"))
            for k, v in facts.items():
                if toks & set(k.split("_")) - {"the", "a", "of"}:
                    direct = v
                    break
        topic = _topic(facts)
        if direct:
            text = f"{MARK} {direct}"
        else:
            text = f"{MARK} {_humanize(name)}" + (f" for {topic}" if topic else "")
        lo, hi = cons.get("min_len", 0), cons.get("max_len", 10_000)
        if len(text) < lo:
            text = (text + " " + "lorem " * lo)[:lo]
        return text[:hi]
