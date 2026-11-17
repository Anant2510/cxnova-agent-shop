"""Package the framework as a source zip (no caches, run outputs or node_modules).

    python3 scripts/package.py [--out dist/coforge-agentic-framework.zip]
"""

from __future__ import annotations

import argparse
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules", ".venv", "venv", ".git", "dist", "build"}
SKIP_TOP = {"runs"}
SKIP_SUFFIX = {".pyc", ".pyo", ".log"}
KEEP_DIST = {Path("console/dist/index.html"), Path("console/dist/console-local.html")}  # built console, usable without a build


def wanted(rel: Path) -> bool:
    if rel in KEEP_DIST:
        return True
    parts = rel.parts
    if parts and parts[0] in SKIP_TOP:
        return False
    if any(p in SKIP_DIRS or p.endswith(".egg-info") for p in parts[:-1]):
        return False
    return rel.suffix not in SKIP_SUFFIX and rel.name != ".DS_Store"


def package(out: Path) -> dict:
    out.parent.mkdir(parents=True, exist_ok=True)
    files = sorted(p for p in ROOT.rglob("*") if p.is_file() and wanted(p.relative_to(ROOT)) and p.resolve() != out.resolve())
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for p in files:
            zf.write(p, Path(ROOT.name) / p.relative_to(ROOT))
    return {"out": str(out), "files": len(files), "bytes": out.stat().st_size}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "dist" / f"{ROOT.name}.zip"))
    print(package(Path(ap.parse_args().out)))
