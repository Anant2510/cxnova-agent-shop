"""python -m docuinsights serve | check | assess | samples | knowledge

    serve       the workbench and its API           --host 127.0.0.1 --port 8090 --data ./docuinsights-data --root-path /docuinsights
    check       say which model is configured and whether it answers (one small call per model in use)
    assess      one bundled sample on the command line   --profile supplier_invoice --sample mismatch
    samples     list the profiles and their samples
    knowledge   show | import <path> | export <file.zip>   the workspace's knowledge base (KB Forge layout)

Settings come from the environment, or from a .env file in the folder it is started from (see .env.example).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path


def load_env(path: Path) -> None:
    """Settings from a .env file beside where it is started (KEY=value lines); the real environment wins."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():   # a .env saved by a Windows editor may start with a byte-order mark
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def model_line(model: object, *, probe: bool) -> str:
    """One line on the model: the provider, the model behind each tier in use, and (when probed) whether it answers."""
    d = model.describe()  # type: ignore[attr-defined]
    if d.get("offline"):
        return "model: offline (no model connected: documents are read with the profile's patterns and rules). Set DOCUINSIGHTS_MODEL_PROVIDER in .env to connect one."
    tiers = d.get("tiers") or {}
    names = ", ".join(f"{t}={tiers[t]}" for t in ("fast", "balanced") if t in tiers) or str(d.get("model"))
    line = f"model: {d['provider']} ({names})"
    if probe and hasattr(model, "check"):
        result = model.check()  # type: ignore[attr-defined]
        model.last_check = result  # type: ignore[attr-defined]
        if all(v == "online" for v in result.values()):
            return line + "  ONLINE"
        return line + "  NOT ANSWERING\n" + "\n".join(f"  {k}: {v}" for k, v in result.items() if v != "online") + \
            "\n  The server still runs; until the model answers, documents are read with the profile's patterns and each run says so."
    return line


def main(argv: list[str] | None = None) -> int:
    load_env(Path.cwd() / ".env")
    p = argparse.ArgumentParser(prog="docuinsights", description="DocuInsights: agentic document and case intelligence")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the workbench and its API")
    s.add_argument("--host", default=os.environ.get("DOCUINSIGHTS_HOST", "127.0.0.1"))
    s.add_argument("--port", type=int, default=int(os.environ.get("DOCUINSIGHTS_PORT", "8090")))
    s.add_argument("--data", default=os.environ.get("DOCUINSIGHTS_DATA_DIR"))
    s.add_argument("--root-path", default=os.environ.get("DOCUINSIGHTS_ROOT_PATH", ""))
    s.add_argument("--no-model-check", action="store_true", help="do not call the model once at start to see that it answers")
    sub.add_parser("check", help="say which model is configured and whether it answers")
    a = sub.add_parser("assess", help="assess a bundled sample and print the outcome")
    a.add_argument("--profile", default="supplier_invoice")
    a.add_argument("--sample", default="mismatch")
    a.add_argument("--json", action="store_true")
    sub.add_parser("samples", help="list profiles and samples")
    k = sub.add_parser("knowledge", help="the workspace's knowledge base: show it, add one built elsewhere, or export it for KB Forge")
    k.add_argument("what", choices=["show", "import", "export"])
    k.add_argument("path", nargs="?", help="import: a KB Forge knowledge base (folder or .zip), a KB Forge graph page (.html) or a JSON graph; export: the .zip to write")
    k.add_argument("--label", default="", help="import: the name to keep it under")
    k.add_argument("--data", default=None, help="data folder (default: DOCUINSIGHTS_DATA_DIR or ./docuinsights-data)")
    k.add_argument("--tenant", default="default")
    args = p.parse_args(argv)

    from .profiles import load_profiles
    from .service import Service

    if args.cmd == "check":
        from .model import ModelError, from_env

        try:
            m = from_env()
        except ModelError as exc:
            print(f"model: not configured: {exc}", file=sys.stderr)
            return 2
        print(model_line(m, probe=True))
        return 0 if m.offline or all(v == "online" for v in getattr(m, "last_check", {}).values()) else 1
    if args.cmd == "samples":
        for prof in load_profiles().values():
            print(f"{prof.id}  {prof.name} (v{prof.version})")
            for smp in prof.samples:
                print(f"    {smp['id']:<14} {smp.get('title', '')}: {smp.get('story', '')}")
        return 0
    if args.cmd == "knowledge":
        from .service import ServiceError

        svc = Service(args.data)
        try:
            if args.what == "import":
                if not args.path:
                    print("knowledge import needs a path", file=sys.stderr)
                    return 2
                src = Path(args.path).expanduser()
                if src.is_dir():   # a knowledge base folder: zipped in memory, exactly as its Download ZIP would be
                    import io
                    import zipfile

                    buf = io.BytesIO()
                    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                        for rel in ("manifest.json", "graph/entities.jsonl", "graph/triples.jsonl", "graph/provenance.json", "ontology/ontology_merged.json"):
                            if (src / rel).is_file():
                                z.write(src / rel, rel)
                    data, name = buf.getvalue(), src.name + ".zip"
                else:
                    data, name = src.read_bytes(), src.name
                got = svc.knowledge_import(args.tenant, name, data, args.label, by="command line")
                print(f"added '{got['import']['label']}': {got['import']['entities']} entities, {got['import']['relationships']} relationships; the knowledge base now holds {got['index']['entities']} entities, {got['index']['triples']} relationships")
                return 0
            ov = svc.knowledge_overview(args.tenant)
            if args.what == "export":
                out = Path(args.path or "docuinsights-knowledge-base.zip").expanduser()
                out.write_bytes(svc.knowledge(args.tenant).export_zip())
                print(f"wrote {out} ({ov['index']['entities']} entities, {ov['index']['triples']} relationships): in KB Forge, Knowledge bases -> Import a graph -> upload this file")
                return 0
            st = (ov["verification"].get("stats") or {})
            print(f"{ov['name']}: {ov['index']['entities']} entities, {ov['index']['triples']} relationships, quality {ov['quality']['score']} ({ov['format']['name']}, schema {ov['format']['schemaVersion']})")
            print(f"  from {ov['sources']['cases']} assessed case(s), {len(ov['sources']['imports'])} added knowledge base(s); {st.get('missing', 0)} citation(s) not found in the text they cite")
            for name, n in list(ov["index"]["byType"].items())[:14]:
                print(f"  {n:>6}  {name}")
            return 0
        except (ServiceError, OSError) as exc:
            print(f"knowledge {args.what}: {exc}", file=sys.stderr)
            return 1
    if args.cmd == "assess":
        svc = Service(tempfile.mkdtemp(prefix="docuinsights-"))
        case = svc.assess("default", svc.load_sample("default", args.profile, args.sample)["reference"])
        if args.json:
            from .app import result

            print(json.dumps(result(case), indent=1))
            return 0
        a_ = case["assessment"]
        print(a_["summary"], "\n")
        for f in a_["findings"]:
            print(f"  {f['status']:<8} {f['title']}: {f['message']}")
        print(f"\n{a_['recommendation']['label']}  ({len(a_['runs'])} agent runs, {a_['metrics']['duration_ms']} ms, model: {a_['model'].get('model')})")
        return 0
    try:
        import uvicorn
    except ImportError:
        print("serve needs uvicorn and starlette: pip install 'docuinsights[server]'", file=sys.stderr)
        return 2
    from .app import StripPrefix, create_app

    svc = Service(args.data)
    app = create_app(svc)
    print(f"DocuInsights on http://{args.host}:{args.port}{args.root_path or ''}/   data: {svc.data_dir}   repository: {svc.repository.status()['kind']}", file=sys.stderr)
    print(model_line(svc.model, probe=not args.no_model_check), file=sys.stderr)
    uvicorn.run(StripPrefix(app, args.root_path) if args.root_path else app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
