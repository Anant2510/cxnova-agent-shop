"""Publishing connectors (side effects, always behind the approval gate when agents propose them):
CMS, ESP, task emitters, newswire and feature-flag delivery. Local `file`/`json_file` providers
write into runs/_<kind>/ so a full campaign can be produced, reviewed and handed off without
any vendor platform. External adapters (AEM, Contentful, Braze, Jira ...) live in *_providers.py.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core.textutil import slugify
from .base import Connector, connector


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class _FileOut(Connector):
    folder_name = "_out"

    @property
    def out_dir(self) -> Path:
        base = self.config.get("path")
        p = self.settings.resolve(base) if base else self.settings.runs_dir / self.folder_name
        p.mkdir(parents=True, exist_ok=True)
        return p


# ----------------------------------------------------------------------------- CMS

class Cms(Connector):
    def publish(self, *, title: str, body: str, slug: str | None = None, content_type: str = "article",
                fields: dict[str, Any] | None = None, status: str = "draft", locale: str = "en",
                body_format: str = "markdown") -> dict[str, Any]:
        raise NotImplementedError


@connector("cms", "file")
class FileCms(_FileOut, Cms):
    """Headless 'file CMS': writes <slug>.<locale>.json + body file under runs/_cms/."""

    folder_name = "_cms"

    def publish(self, *, title: str, body: str, slug: str | None = None, content_type: str = "article",
                fields: dict[str, Any] | None = None, status: str = "draft", locale: str = "en",
                body_format: str = "markdown") -> dict[str, Any]:
        slug = slugify(slug or title) or "entry"  # file names never come from caller text as-is
        locale = re.sub(r"[^A-Za-z0-9_-]+", "-", str(locale or "en")).strip("-")[:35] or "en"
        ext = {"markdown": "md", "html": "html"}.get(body_format, "txt")
        body_path = self.out_dir / f"{slug}.{locale}.{ext}"
        body_path.write_text(body, encoding="utf-8")
        entry = {"id": f"{content_type}:{slug}:{locale}", "title": title, "slug": slug, "content_type": content_type,
                 "status": status, "locale": locale, "fields": fields or {}, "body_file": body_path.name, "updated_at": _stamp()}
        (self.out_dir / f"{slug}.{locale}.json").write_text(json.dumps(entry, indent=2, ensure_ascii=False), encoding="utf-8")
        return {"id": entry["id"], "url": body_path.as_uri(), "status": status}


# ----------------------------------------------------------------------------- ESP

class Esp(Connector):
    def export(self, *, name: str, subject: str, preheader: str, html: str, text: str, mjml: str | None = None,
               audience: str | None = None, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        raise NotImplementedError


@connector("esp", "file")
class FileEsp(_FileOut, Esp):
    """ESP-neutral export: HTML + MJML + plain text + manifest.json under runs/_esp/<name>/."""

    folder_name = "_esp"

    def export(self, *, name: str, subject: str, preheader: str, html: str, text: str, mjml: str | None = None,
               audience: str | None = None, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        folder = self.out_dir / slugify(name)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "email.html").write_text(html, encoding="utf-8")
        (folder / "email.txt").write_text(text, encoding="utf-8")
        files = ["email.html", "email.txt"]
        if mjml:
            (folder / "email.mjml").write_text(mjml, encoding="utf-8")
            files.append("email.mjml")
        manifest = {"name": name, "subject": subject, "preheader": preheader, "audience": audience,
                    "metadata": metadata or {}, "files": files, "exported_at": _stamp()}
        (folder / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        return {"id": slugify(name), "path": str(folder), "files": files, "status": "exported"}


# ----------------------------------------------------------------------------- Tasks

class Tasks(Connector):
    def create(self, tasks: list[dict[str, Any]], *, project: str | None = None) -> list[dict[str, Any]]:
        raise NotImplementedError


@connector("tasks", "json_file")
class JsonFileTasks(_FileOut, Tasks):
    """Task emitter that appends to runs/_tasks/tasks.json (swap for Jira / Azure DevOps / Planner)."""

    folder_name = "_tasks"
    _lock = threading.Lock()

    def create(self, tasks: list[dict[str, Any]], *, project: str | None = None) -> list[dict[str, Any]]:
        path = self.out_dir / "tasks.json"
        with self._lock:
            existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
            created = []
            for t in tasks:
                item = {"id": f"TASK-{uuid.uuid4().hex[:6].upper()}", "project": project or "CAF", "created_at": _stamp(), **t}
                existing.append(item)
                created.append({"id": item["id"], "title": item.get("title"), "url": path.as_uri()})
            path.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
        return created


# ----------------------------------------------------------------------------- Newswire

class Newswire(Connector):
    def distribute(self, *, headline: str, body: str, embargo: str | None = None, regions: list[str] | None = None,
                   metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        raise NotImplementedError


@connector("newswire", "file")
class FileNewswire(_FileOut, Newswire):
    """Writes the release package (markdown + JSON) for newsroom CMS or wire upload."""

    folder_name = "_newswire"

    def distribute(self, *, headline: str, body: str, embargo: str | None = None, regions: list[str] | None = None,
                   metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        slug = slugify(headline)
        (self.out_dir / f"{slug}.md").write_text(f"# {headline}\n\n{body}", encoding="utf-8")
        pkg = {"headline": headline, "embargo": embargo, "regions": regions or [], "metadata": metadata or {}, "created_at": _stamp()}
        (self.out_dir / f"{slug}.json").write_text(json.dumps(pkg, indent=2, ensure_ascii=False), encoding="utf-8")
        return {"id": slug, "status": "queued" if embargo else "ready", "path": str(self.out_dir / f"{slug}.md")}


# ----------------------------------------------------------------------------- Feature flags / variation delivery

RUNTIME_TEMPLATE = """/* Coforge CAF variation runtime - vendor-neutral client-side mutation bundle.
 * experiment: {experiment_id}  variation: {variation_id}
 * Include on the page (or deliver from an edge worker / any testing tool's custom-code slot). */
(function () {{
  var css = {css};
  if (css) {{ var s = document.createElement('style'); s.setAttribute('data-caf-variation', {variation_id_js}); s.textContent = css; document.head.appendChild(s); }}
  function apply() {{
    try {{ {js} }} catch (e) {{ if (window.console) console.warn('[caf] variation error', e); }}
  }}
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', apply); else apply();
}})();
"""


class FeatureFlags(Connector):
    def deploy_variation(self, *, experiment_id: str, variation_id: str, css: str = "", js: str = "",
                         selectors: list[str] | None = None, preview: bool = True) -> dict[str, Any]:
        raise NotImplementedError


@connector("flags", "file")
class FileFlags(_FileOut, FeatureFlags):
    """Emits a self-contained JS mutation bundle runnable on any testing tool (or none)."""

    folder_name = "_flags"

    def deploy_variation(self, *, experiment_id: str, variation_id: str, css: str = "", js: str = "",
                         selectors: list[str] | None = None, preview: bool = True) -> dict[str, Any]:
        folder = self.out_dir / slugify(experiment_id)
        folder.mkdir(parents=True, exist_ok=True)
        bundle = RUNTIME_TEMPLATE.format(experiment_id=experiment_id, variation_id=variation_id,
                                         variation_id_js=json.dumps(variation_id), css=json.dumps(css or ""), js=js or "")
        path = folder / f"{slugify(variation_id)}.js"
        path.write_text(bundle, encoding="utf-8")
        return {"bundle": str(path), "status": "preview" if preview else "deployed", "selectors": selectors or []}
