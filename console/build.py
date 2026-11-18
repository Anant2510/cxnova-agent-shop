"""Build the hosted console (console/dist/index.html) from the framework itself.

The page embeds the live agent catalog (specs + Pydantic input/output/prompt JSON schemas),
YAML skills, playbooks, Nunjucks-precompiled prompt templates (no eval needed), the demo site and
compressed sample data, so the browser runs the same contracts as the Python runtime.

    python3 console/build.py [--out console/dist/index.html]

Writes index.html (the artifact page; the publisher adds the document skeleton) and console-local.html (the same page
as a standalone document you can open from disk, offline model only).
"""

from __future__ import annotations

import argparse
import base64
import gzip
import json
import mimetypes
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("CAF_HOME", str(ROOT))
os.environ.setdefault("CAF_LLM_PROVIDER", "offline")

LIBS = [  # pinned, verified against the npm tarballs; jsDelivr mirrors npm paths exactly
    "https://cdn.jsdelivr.net/npm/nunjucks@3.2.4/browser/nunjucks-slim.min.js",
    "https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js",
    "https://cdn.jsdelivr.net/npm/dompurify@3.1.6/dist/purify.min.js",
    "https://cdn.jsdelivr.net/npm/qrcode-generator@1.4.4/qrcode.js",
]
AXE_URL = "https://cdn.jsdelivr.net/npm/axe-core@4.10.2/axe.min.js"
TEXT_EXT = {".md", ".txt", ".tmx", ".xml", ".vtt", ".srt"}
# mirrors the skeleton the artifact publisher wraps around index.html (charset, viewport and the small reset)
LOCAL_SKELETON = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
                  '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
                  '<style>:root{color-scheme:light;padding-top:env(safe-area-inset-top);padding-bottom:env(safe-area-inset-bottom)}'
                  'body{margin:0;font:14px system-ui}img{max-width:100%}[hidden]{display:none!important}</style></head>'
                  '<body>{body}</body></html>')
GZIP_OVER = 16_000


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str, separators=(",", ":"))


def collect_catalog() -> dict:
    from caf.config import get_settings
    from caf.core import catalog
    from caf.core.skills import SkillStore
    from caf.orchestrator import load_playbooks
    from caf.orchestrator.graph import RunSynthesis
    from caf.orchestrator.router import Plan, catalog_for_planner, playbooks_for_planner

    settings = get_settings()
    return {
        # the blueprint's agents have a browser runtime; pack agents (config/packs) run on the server and reach a hosted
        # console at serve time (caf-host-agents), so the stand-alone page stays the 28-agent console
        "catalog": [a for a in catalog(full=True) if a.get("pack", "blueprint") == "blueprint"],
        "skills": SkillStore(settings.skills_dirs).all(),
        "playbooks": {pid: p.model_dump() for pid, p in load_playbooks().items() if not p.pack},
        "planner": {"agents": catalog_for_planner(), "playbooks": playbooks_for_planner()},
        "schemas": {"plan": Plan.model_json_schema(), "synthesize": RunSynthesis.model_json_schema()},
        "routing": routing_table(settings),
        "axe_url": AXE_URL,
    }


def routing_table(settings) -> dict:
    """The model routing policy's tier per agent step, so browser twins route model steps like the Python agents."""
    from caf.core import all_agents
    from caf.core.model_router import get_router

    router = get_router(settings)
    out: dict = {}
    for row in router.table([a for a in all_agents() if a.spec.pack == "blueprint"]):
        entry = router.steps.get(f"{row['agent']}.{row['step']}") or {}
        out.setdefault(row["agent"], {})[row["step"]] = {"tier": row["tier"], "work": row["work"], "follow": bool(entry.get("follow_request"))}
    return out


def precompile_prompts() -> str:
    node_path = os.environ.get("NODE_PATH") or str(Path(os.environ.get("CAF_NODE_MODULES", "")))
    env = {**os.environ}
    nunjucks = None
    for cand in [os.environ.get("NUNJUCKS_PATH"), str(ROOT / "console" / "node_modules" / "nunjucks"),
                 *(str(Path(p) / "nunjucks") for p in (node_path or "").split(os.pathsep) if p)]:
        if cand and Path(cand).exists():
            nunjucks = cand
            break
    if not nunjucks:
        raise SystemExit("nunjucks not found: run `npm install --prefix console nunjucks@3.2.4` or set NUNJUCKS_PATH")
    env["NUNJUCKS_PATH"] = nunjucks
    import tempfile

    fd, tmp = tempfile.mkstemp(prefix="caf-prompts-", suffix=".js")
    os.close(fd)
    out = Path(tmp)
    try:
        subprocess.run(["node", str(ROOT / "console" / "tools" / "precompile.js"), str(ROOT / "config" / "prompts"), str(out)],
                       check=True, env=env, capture_output=True)
        return out.read_text(encoding="utf-8")
    finally:
        out.unlink(missing_ok=True)


def collect_site() -> dict[str, str]:
    site = ROOT / "sample_data" / "site"
    if not site.exists():
        return {}
    assets: dict[str, str] = {}
    for f in site.rglob("*"):
        if f.is_file() and f.suffix.lower() in (".svg", ".png", ".jpg", ".jpeg", ".webp", ".gif"):
            mime = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
            assets["/" + str(f.relative_to(site)).replace(os.sep, "/")] = f"data:{mime};base64,{base64.b64encode(f.read_bytes()).decode()}"
    css: dict[str, str] = {"/" + str(f.relative_to(site)).replace(os.sep, "/"): f.read_text(encoding="utf-8")
                           for f in site.rglob("*.css")}
    pages: dict[str, str] = {}
    for f in sorted(site.rglob("*")):
        if not f.is_file():
            continue
        rel = "/" + str(f.relative_to(site)).replace(os.sep, "/")
        if f.suffix.lower() == ".html":
            html = f.read_text(encoding="utf-8")

            def inline_css(m: re.Match) -> str:
                href = m.group(1)
                key = href if href.startswith("/") else "/" + href.lstrip("./")
                return f"<style>{css[key]}</style>" if key in css else m.group(0)

            html = re.sub(r'<link[^>]+rel=["\']stylesheet["\'][^>]+href=["\']([^"\']+)["\'][^>]*>', inline_css, html)
            html = re.sub(r'<link[^>]+href=["\']([^"\']+\.css)["\'][^>]+rel=["\']stylesheet["\'][^>]*>', inline_css, html)
            for path, uri in assets.items():
                html = html.replace(f'"{path}"', f'"{uri}"').replace(f"'{path}'", f"'{uri}'").replace(f"url({path})", f"url({uri})")
            pages[rel] = html
        elif f.suffix.lower() in (".txt", ".xml", ".json"):
            pages[rel] = f.read_text(encoding="utf-8")
    return pages


def collect_datasets() -> dict[str, dict]:
    base = ROOT / "sample_data"
    out: dict[str, dict] = {}
    if not base.exists():
        return out
    from caf.connectors.ingestion import LocalIngestion
    from caf.connectors import ConnectorHub
    from caf.config import get_settings

    ingestion = LocalIngestion({}, ConnectorHub(get_settings()))
    for f in sorted(base.rglob("*")):
        if not f.is_file() or "site" in f.relative_to(base).parts[:1]:
            continue
        name = str(f.relative_to(base).with_suffix("")).replace(os.sep, "/")
        ext = f.suffix.lower()
        if ext == ".csv":
            kind, text = "csv", f.read_text(encoding="utf-8")
        elif ext == ".json":
            kind, text = "json", f.read_text(encoding="utf-8")
        elif ext in TEXT_EXT:
            kind, text = "text", f.read_text(encoding="utf-8")
        elif ext in (".docx", ".pdf"):
            kind, text = "text", ingestion.load(path=str(f)).text
        else:
            continue
        if len(text.encode()) > GZIP_OVER:
            out[name] = {"type": kind, "enc": "gz64", "data": base64.b64encode(gzip.compress(text.encode(), 9, mtime=0)).decode()}
        else:
            out[name] = {"type": kind, "enc": "plain", "data": text}
    return out


def check_syntax(files: list[Path]) -> None:
    """Fail the build on any JavaScript syntax error (node --check)."""
    bad = []
    for f in files:
        r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
        if r.returncode != 0:
            bad.append(f"{f.name}: {(r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip() else 'syntax error'}")
    if bad:
        raise SystemExit("JavaScript syntax errors:\n  " + "\n  ".join(bad))


def build(out_path: Path) -> dict:
    src = ROOT / "console" / "src"
    boot = collect_catalog()
    boot["site"] = collect_site()
    boot["datasets"] = collect_datasets()
    prompts_js = precompile_prompts()
    core_files = [src / "js" / "00-util.js", src / "js" / "10-runtime.js", src / "js" / "20-lib.js"]
    agent_files = sorted((src / "js" / "agents").glob("*.js"))
    ui_files = [src / "js" / "80-orchestrator.js", src / "js" / "90-ui.js"]
    check_syntax(core_files + agent_files + ui_files)

    def block(files: list[Path]) -> str:
        code = "\n".join(f"/* ---- {p.relative_to(src)} ---- */\n{p.read_text(encoding='utf-8')}" for p in files)
        code = code.replace("</script", "<\\/script")  # never close the tag early
        return "<script>\n" + code + "\n</script>"

    # one <script> per agent: a fault in one agent's file can never take down the page or the other agents
    app_js = "\n".join([block(core_files), *(block([f]) for f in agent_files), block(ui_files)])
    boot_json = _json(boot).replace("</", "<\\/")
    html = (src / "index.html").read_text(encoding="utf-8")
    html = html.replace("<!--STYLE-->", f"<style>\n{(src / 'styles.css').read_text(encoding='utf-8')}\n</style>")
    html = html.replace("<!--DATA-->", f'<script type="application/json" id="caf-boot">{boot_json}</script>')
    libs = "\n".join(f'<script src="{u}"></script>' for u in LIBS)
    html = html.replace("<!--SCRIPTS-->", f"{libs}\n<script>\n{prompts_js}\n</script>\n{app_js}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")  # the artifact page: the publisher adds the document skeleton
    # the same page as a standalone document for opening from disk (offline model; libraries load from jsDelivr)
    local = out_path.with_name("console-local.html")
    local.write_text(LOCAL_SKELETON.replace("{body}", html), encoding="utf-8")
    browser_agents = sorted(p.stem for p in (src / "js" / "agents").glob("*.js"))
    info = {"out": str(out_path), "local": str(local), "bytes": out_path.stat().st_size, "agents_in_catalog": len(boot["catalog"]),
            "browser_agents": len(browser_agents), "missing_browser_agents": sorted({a["id"] for a in boot["catalog"]} - set(browser_agents)),
            "datasets": len(boot["datasets"]), "site_pages": len(boot["site"]), "skills": len(boot["skills"])}
    return info


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "console" / "dist" / "index.html"))
    args = ap.parse_args()
    print(json.dumps(build(Path(args.out)), indent=2))
