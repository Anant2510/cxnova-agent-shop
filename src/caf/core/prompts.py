"""Prompt store: externalised Jinja2 templates under config/prompts/<agent_id>/<name>.j2.

Templates are written in the Jinja2/Nunjucks common subset so the same files drive the Python
runtime (Jinja2) and the browser console (Nunjucks). Rules:
  * no dict iteration or Python methods inside templates (pass lists from code)
  * custom filters available in both runtimes: tojson, bullets, numbered
  * a line consisting of exactly '---- USER ----' splits system text (above) from the user turn
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jinja2

USER_MARKER = "---- USER ----"


def _tojson(value: Any, indent: int | None = None) -> str:
    # compact separators match the browser runtime's JSON.stringify byte-for-byte
    return json.dumps(value, ensure_ascii=False, default=str, indent=indent, separators=None if indent else (",", ":"))


def _bullets(items: Any, prefix: str = "- ") -> str:
    if not items:
        return ""
    if isinstance(items, str):
        items = [items]
    return "\n".join(f"{prefix}{i}" for i in items)


def _numbered(items: Any) -> str:
    if not items:
        return ""
    return "\n".join(f"{n}. {i}" for n, i in enumerate(items, 1))


class PromptStore:
    def __init__(self, dirs: list[Path]):
        self.dirs = [Path(d) for d in dirs]
        self.env = jinja2.Environment(
            loader=jinja2.FileSystemLoader([str(d) for d in self.dirs]),
            undefined=jinja2.ChainableUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
            autoescape=False,
            keep_trailing_newline=False,
        )
        self.env.filters.update(tojson=_tojson, bullets=_bullets, numbered=_numbered)

    def exists(self, agent_id: str, name: str) -> bool:
        return any((d / agent_id / f"{name}.j2").exists() for d in self.dirs)

    def source(self, agent_id: str, name: str) -> str:
        for d in self.dirs:
            p = d / agent_id / f"{name}.j2"
            if p.exists():
                return p.read_text(encoding="utf-8")
        raise FileNotFoundError(f"Prompt {agent_id}/{name}.j2 not found")

    def render(self, agent_id: str, name: str, /, **variables: Any) -> tuple[str | None, str]:
        """Render a template; returns (system, user)."""
        tmpl = self.env.get_template(f"{agent_id}/{name}.j2")
        text = tmpl.render(**variables).strip()
        if USER_MARKER in text:
            system, user = text.split(USER_MARKER, 1)
            return system.strip() or None, user.strip()
        return None, text

    def list(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for d in self.dirs:
            if not d.exists():
                continue
            for p in sorted(d.glob("*/*.j2")):
                out.setdefault(p.parent.name, [])
                if p.stem not in out[p.parent.name]:
                    out[p.parent.name].append(p.stem)
        return out
