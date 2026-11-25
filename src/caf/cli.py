"""caf - command line for the Coforge Agentic Framework.

  caf agents                         list the 28 agents
  caf describe <agent_id>            spec, input/output schema, graph
  caf run <agent_id> [--input JSON|@file] [--set key=value ...] [--selftest]
  caf orchestrate "<goal>" [--playbook ID] [--agent ID] [--input JSON|@file] [--auto-approve]
  caf playbooks                      list playbooks
  caf health [--live] [--json]       liveness probe of every agent
  caf serve [--host 127.0.0.1] [--port 8080]
  caf mcp                            MCP server over stdio
  caf export-catalog <out.json>      catalog for the browser console
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


def _load_json(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    if value.startswith("@"):
        return json.loads(Path(value[1:]).read_text(encoding="utf-8"))
    return json.loads(value)


def _apply_sets(data: dict[str, Any], sets: list[str]) -> dict[str, Any]:
    for item in sets or []:
        key, _, raw = item.partition("=")
        if raw.startswith("@") and Path(raw[1:]).is_file():   # --set profile=@passengers.csv: the file's text, as an upload would send it
            data[key] = Path(raw[1:]).read_text(encoding="utf-8")
            continue
        try:
            data[key] = json.loads(raw)
        except json.JSONDecodeError:
            data[key] = raw
    return data


def _print(obj: Any, as_json: bool = True) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str) if as_json else obj)


def cmd_agents(args: argparse.Namespace) -> int:
    from .core import catalog

    if getattr(args, "what", None) == "certify":   # caf agents certify: every agent through the battery (caf.certify)
        from .certify import certify, render

        def tick(row: dict) -> None:
            if not args.json:
                print(f"  {'pass' if row['passed'] else 'FAIL'}  {row['id']:<38} {len(row['trials']):>2} trials  {row['ms'] / 1000:>6.1f}s"
                      + ("" if row["passed"] else "   " + ", ".join(row["failed"])), file=sys.stderr, flush=True)

        if not args.json:
            print("--live: each agent's self-test runs on the server's own model, and those calls are billed there." if args.live else
                  "No live model is called (--live runs each agent's self-test on the server's own model).", file=sys.stderr, flush=True)
        report = certify(pack=args.pack, agents=args.agent or None, fast=args.fast, live=args.live, progress=tick)
        if args.report:
            Path(args.report).write_text(render(report, markdown=True), encoding="utf-8")
        _print(report) if args.json else print(render(report))
        return 0 if report["ok"] else 1
    if getattr(args, "what", None) == "audit":   # caf agents audit: every pack agent against the quality standard
        from .quality import audit, render

        report = audit(args.pack)
        _print(report) if args.json else print(render(report, verbose=args.verbose))
        return 0 if report["ok"] else 1
    rows = catalog()
    if args.json:
        _print(rows)
        return 0
    current = None
    for r in rows:
        if r["category_label"] != current:
            current = r["category_label"]
            print(f"\n{current}")
        print(f"  {r['id']:<34} {r['kind']:<13} {r['summary'][:90]}")
    print(f"\n{len(rows)} agents")
    return 0


def cmd_describe(args: argparse.Namespace) -> int:
    from .core import get_agent

    _print(get_agent(args.agent_id).describe())
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from .core import RunContext, get_agent

    agent = get_agent(args.agent_id)
    inputs = agent.selftest_input() if args.selftest else _apply_sets(_load_json(args.input), args.set)
    result = agent.run(inputs, RunContext.create(), options={"auto_approve": args.auto_approve})
    if args.json:
        _print(result.model_dump(mode="json"))
    else:
        print(f"[{result.status}] {result.summary}")
        if result.error:
            print(f"error: {result.error}")
        for c in result.checks:
            if c.status in ("fail", "warn"):
                print(f"  {c.status.upper():<4} {c.rule}: {c.message}")
        for a in result.artifacts:
            print(f"  artifact: runs/{a.path}")
        for p in result.pending_actions:
            print(f"  action [{p.status}] {p.kind}: {p.summary}")
    return 0 if result.status != "error" else 1


def cmd_orchestrate(args: argparse.Namespace) -> int:
    from .orchestrator import Orchestrator

    orch = Orchestrator()
    view = orch.start(args.goal, inputs=_load_json(args.input), playbook=args.playbook, agent=args.agent,
                      auto_approve=args.auto_approve)
    if view["status"] == "awaiting_approval" and not args.json and sys.stdin.isatty():
        for p in view["pending_approvals"]:
            print(f"Approval needed - {p['agent_id']}: {p['summary']}")
        ans = input("Approve all? [y/N] ").strip().lower()
        view = orch.resume(view["run_id"], {"*": "approve" if ans in ("y", "yes") else "reject"})
    if args.json:
        _print(view)
        return 0
    if view["status"] == "needs_clarification":
        print(view["warnings"][0])
        for step in (view.get("final") or {}).get("next_steps", []):
            print(f"  - {step}")
        return 2
    route = view.get("route") or {}
    print(f"run {view['run_id']} | {route.get('mode')} via {route.get('method')}: {route.get('reason')}")
    for s in view["steps"]:
        r = s.get("result") or {}
        print(f"  {s['id']:<12} {s['agent']:<32} {s['status']:<15} {r.get('summary', '')[:80]}")
    final = view.get("final") or {}
    took = f" ({final['duration_s']:.0f} s)" if final.get("duration_s") is not None else ""
    print(f"\n{final.get('headline', view['status'])}{took}")
    if final.get("method") == "model" and final.get("summary_markdown"):
        print(f"\n{final['summary_markdown'].strip()}")
    if final.get("next_steps"):
        print("\nNext steps:")
        for step in final["next_steps"]:
            print(f"  - {step}")
    if final.get("report"):
        print(f"\nSummary: runs/{final['report']}")
    return 0


def cmd_models(args: argparse.Namespace) -> int:
    """Which model does each model step use, and why (config/model_routing.yaml + the provider's tiers)."""
    from .config import get_settings
    from .core import all_agents
    from .core.model_router import get_router

    settings = get_settings()
    provider = args.provider or settings.llm.resolved_provider()
    router = get_router(settings)
    rows = router.table(all_agents())
    for r in rows:
        r["model"] = "offline" if provider == "offline" else settings.llm.model_for(r["tier"], provider)
    if args.json:
        _print({"provider": provider, "profile": router.profile, "steps": rows})
        return 0
    if args.markdown:
        lines = [f"# Model routing", "",
                 f"Provider **{provider}**, profile **{router.profile}**. Generated by `caf models --markdown`; edit "
                 "`config/model_routing.yaml` to change a decision (set `tier:` on a step to pin it).", "",
                 "Tiers per provider come from `config/settings.yaml`. At run time the router can still step a call up a tier "
                 "for a very large input, a large strict output schema, image input, or a retry after an invalid answer; "
                 "each agent result lists the model used per step under `provenance.model_routes`.", "",
                 "| Agent | Step | Kind of work | Tier | Effort | Model | Why |", "|---|---|---|---|---|---|---|"]
        lines += [f"| {r['agent']} | {r['step']} | {r['work']} | {r['tier']} | {r.get('effort', '-')} | {r['model']} | {r['why']} |" for r in rows]
        lines += ["", "## Kinds of work", "", "| Work | Default tier | Effort | What it covers |", "|---|---|---|---|"]
        lines += [f"| {k} | {v.get('tier')} | {v.get('effort', '-')} | {v.get('description', '')} |" for k, v in router.work_types.items()]
        Path(args.markdown).write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"wrote {args.markdown}")
        return 0
    print(f"provider {provider} | profile {router.profile}\n")
    print(f"  {'agent':<30} {'step':<17} {'work':<14} {'tier':<9} {'effort':<7} model")
    for r in rows:
        print(f"  {r['agent']:<30} {r['step']:<17} {r['work']:<14} {r['tier']:<9} {r.get('effort', '-'):<7} {r['model']}")
    return 0


def cmd_playbooks(args: argparse.Namespace) -> int:
    from .orchestrator import load_playbooks

    for p in load_playbooks().values():
        print(f"{p.id:<22} {p.name}: {' -> '.join(s.agent for s in p.steps)}")
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    from .config import get_settings
    from .health import liveness

    only = [a.strip() for a in args.only.split(",") if a.strip()] if getattr(args, "only", None) else None
    if args.live:
        provider = get_settings().llm.resolved_provider()
        if provider == "offline":
            print("No model provider is configured, so a live probe would only repeat the offline one.\n"
                  "Put your key in .env (ANTHROPIC_API_KEY=sk-ant-..., or another provider from .env.example) "
                  "and run `caf health --live` again.", file=sys.stderr)
            return 2
        tier = "fast" if args.fast else None
        from .core.llm import LLM

        try:
            ping = LLM(get_settings().llm).ping()
        except Exception as exc:  # one clear message instead of a page of DOWN lines
            msg = str(exc)
            hint = ""
            if "401" in msg or "authentication" in msg.lower() or "invalid x-api-key" in msg.lower():
                hint = "\nThe key was not accepted."
                key = os.environ.get("ANTHROPIC_API_KEY", "")
                if provider == "anthropic" and key and not key.startswith("sk-ant-"):
                    hint += (" ANTHROPIC_API_KEY does not start with 'sk-ant-', so it is not an Anthropic API key "
                             "(create one at console.anthropic.com > API keys). For a Claude deployment on Microsoft "
                             "Foundry, set ANTHROPIC_FOUNDRY_API_KEY and ANTHROPIC_FOUNDRY_BASE_URL instead.")
                if provider == "anthropic_foundry":
                    hint += " Check ANTHROPIC_FOUNDRY_API_KEY (Foundry portal > your deployment > Keys and Endpoint)."
            elif "404" in msg or "not_found" in msg.lower() or "deploymentnotfound" in msg.lower():
                hint = ("\nThe endpoint answered but has no such model. Check the deployment names: FOUNDRY_DEPLOYMENT_FAST, "
                        "_BALANCED, _PRO, _COMPLEX in .env (or the tiers in config/settings.yaml).")
            print(f"The {provider} model did not answer a one-line test call: {msg[:300]}{hint}", file=sys.stderr)
            return 2
        print(f"Key accepted: {ping['model']} answered in {ping['ms']} ms.", file=sys.stderr)
        print(f"Live probe on {provider}" + (" (every model step on the fast tier)" if tier else " (each agent on its own tier)")
              + ": real model calls, so this uses API credit and takes a few minutes.", file=sys.stderr)
        report = liveness(offline=False, tier=tier, packs=args.packs, only=only)
    else:
        report = liveness(offline=True, packs=args.packs, only=only)
    if args.json:
        _print(report)
    else:
        for r in report["agents"]:
            mark = "LIVE" if r["live"] else "DOWN"
            status = r["status"] + (f" [{r['runner']}]" if r.get("runner") else "")   # pack agents: who served the probe
            # on a live probe a degraded agent did not get its model answer: say why, not what the fallback produced
            why = (r.get("degraded_reasons") or [""])[0] if report["mode"] != "offline" and r["status"] == "degraded" else ""
            print(f"  {mark}  {r['agent_id']:<34} {status:<16} {r['duration_ms']:>8.0f} ms  {(r.get('error') or why or r.get('summary') or '')[:90]}")
        print(f"\n{report['live']}/{report['total']} agents live ({report['mode']} probe, {report['duration_ms'] / 1000:.1f}s)")
    return 0 if report["all_live"] else 1


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .config import get_settings

    token = os.environ.get("CAF_API_TOKEN") or str(get_settings().security.get("api_token") or "")
    if args.host not in ("127.0.0.1", "localhost", "::1") and not token:
        print(f"warning: serving on {args.host} without CAF_API_TOKEN - only loopback clients will be answered. "
              "Set CAF_API_TOKEN to serve other machines.", file=sys.stderr)
    settings = get_settings()
    from .branding import PRODUCT

    root_path = (os.environ.get("CAF_ROOT_PATH") or "").rstrip("/")  # e.g. /agentshop behind a reverse proxy prefix
    host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    base = f"http://{host}:{args.port}{root_path}"
    print(f"\n  {PRODUCT.name}\n"
          f"  Storefront:      {base}/shop\n"
          f"  Agent console:   {base}/console\n"
          f"  Admin:           {base}/shop/admin\n"
          f"  API docs:        {base}/docs\n"
          f"  Model provider:  {settings.llm.resolved_provider()}\n", file=sys.stderr, flush=True)
    uvicorn.run("caf.api.server:app", host=args.host, port=args.port, reload=False, root_path=root_path, proxy_headers=True,
                forwarded_allow_ips=os.environ.get("CAF_FORWARDED_ALLOW_IPS", "127.0.0.1"))
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    from .api.mcp_server import main

    main()
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    from .core import catalog

    Path(args.out).write_text(json.dumps(catalog(full=True), indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


def _cmd_shop_flows(shop: Any, args: argparse.Namespace) -> int:
    """caf shop flows [list|show|save|publish|unpublish|clone|delete|test]: the orchestration flows the shop rents out."""
    import yaml

    from .orchestrator import Orchestrator

    act = args.flow_cmd or "list"
    if act == "list":
        for f in shop.flows():
            stages = " -> ".join("+".join(st) for st in f.stages())
            print(f"  {f.id:<28} {f.status:<10} {f.source:<8} v{f.version:<3} {f.name:<34} {stages}")
        return 0
    if act == "show":
        flow = shop.flow(args.flow_id)
        if flow is None:
            print(f"error: unknown flow '{args.flow_id}'", file=sys.stderr)
            return 2
        print(yaml.safe_dump(flow.model_dump(exclude_none=True), sort_keys=False, allow_unicode=True))
        return 0
    if act == "save":
        data = yaml.safe_load(Path(args.file).read_text(encoding="utf-8")) or {}
        if args.flow_id:
            data["id"] = args.flow_id
        flow = shop.save_flow(data, publish=True if args.publish else None)
        print(f"saved {flow.id} v{flow.version} ({flow.status}); stages: {' -> '.join('+'.join(st) for st in flow.stages())}")
        return 0
    if act in ("publish", "unpublish"):
        flow = shop.publish_flow(args.flow_id, act == "publish")
        print(f"{flow.id} is now {flow.status}" + (f" (MCP tool run_flow_{flow.id})" if flow.published else ""))
        return 0
    if act == "clone":
        flow = shop.clone_flow(args.flow_id, args.new_id, args.name)
        print(f"cloned {args.flow_id} -> {flow.id} (draft); edit with: caf shop flows show {flow.id} > f.yaml, then caf shop flows save f.yaml")
        return 0
    if act == "delete":
        shop.delete_flow(args.flow_id)
        print(f"deleted {args.flow_id}")
        return 0
    if act == "test":
        cust = shop.store.get_customer(args.customer) if args.customer else None
        view = shop.test_flow(args.flow_id, orchestrator=Orchestrator(shop.settings), customer=cust)
        for st in view["steps"]:
            r = st.get("result") or {}
            mark = "ok  " if st["status"] in ("ok", "degraded") else "FAIL"
            print(f"  {mark} {st['id']:<20} {st['agent']:<32} {st['status']:<14} {(r.get('error') or r.get('summary') or '')[:70]}")
        print(f"\n{view['passed']}/{view['total']} steps passed for flow {args.flow_id} (run {view['run_id']})")
        return 0 if view["passed"] == view["total"] else 1
    return 2


def cmd_shop(args: argparse.Namespace) -> int:
    """caf shop ...: customers, keys, subscriptions, policies, checks and usage of the Agent Shop."""
    import yaml

    from .config import get_settings
    from .shop import ShopError, get_shop

    shop = get_shop(get_settings())
    store = shop.store
    what = args.shop_cmd
    try:
        if what == "customers":
            rows = store.list_customers()
            if args.json:
                print(json.dumps(rows, indent=2, default=str))
                return 0
            if not rows:
                print("no customers yet: caf shop add <id> \"<name>\"")
            for c in rows:
                n, nf = len(shop.entitled_agents(c)), len(shop.entitled_flows(c))
                print(f"  {c['id']:<24} {c['name']:<28} mode {c['run_mode_number']}  {c['status']:<9} {n:>2} agents {nf:>2} flows  "
                      f"keys {len([k for k in store.list_keys(c['id']) if not k['revoked_at']])}")
                if shop.model_warning(c):
                    print(f"  {'':<24} ! {shop.model_warning(c)}")
            return 0
        if what == "add":
            shop.check_run_mode(args.mode, None)   # mode 2 follows from a policy naming the customer's model (caf shop policy --file)
            c = store.add_customer(args.customer_id, args.name, run_mode=args.mode, contact=args.contact or "")
            print(f"created {c['id']} ({c['name']}), run mode {c['run_mode']}")
            return 0
        if what == "workspace":   # the customer's own estate, given to every pack agent they call (caf.packs.workspace)
            from .packs import workspace

            if args.template:
                print(yaml.safe_dump(workspace.TEMPLATE, sort_keys=False, allow_unicode=True, width=110), end="")
                return 0
            if args.customer_id != workspace.OPERATOR and not store.get_customer(args.customer_id):
                raise ShopError(f"no customer {args.customer_id!r} (the operator's own workspace is `{workspace.OPERATOR}`)")
            if args.clear:
                print("cleared" if workspace.clear(args.customer_id) else "nothing was saved")
                return 0
            if args.set:
                facts = workspace.save(args.customer_id, Path(args.set).read_text(encoding="utf-8"))
                print(f"saved the workspace of {args.customer_id}: {', '.join(sorted(facts))}")
                return 0
            facts = workspace.load(args.customer_id)
            print(yaml.safe_dump(facts, sort_keys=False, allow_unicode=True, width=110), end="") if facts else print(
                f"{args.customer_id} has no workspace yet: caf shop workspace {args.customer_id} --template > estate.yaml, fill it in, then --set estate.yaml")
            return 0
        if what == "key":
            full, rec = store.issue_key(args.customer_id, args.label or "")
            print(f"key for {args.customer_id} (shown once, store it safely):\n\n  {full}\n\nprefix {rec['prefix']}, id {rec['id']}")
            return 0
        if what == "revoke":
            store.revoke_key(args.customer_id, args.key_id)
            print("revoked")
            return 0
        if what == "subscribe":
            items = [{"kind": "agent", "item_id": a, "ends_at": args.until} for a in (args.agents.split(",") if args.agents else []) if a.strip()]
            flows = (args.flows or "") + ("," + args.playbooks if args.playbooks else "")
            items += [{"kind": "flow", "item_id": f, "ends_at": args.until} for f in flows.split(",") if f.strip()]
            if args.all:
                items.append({"kind": "all", "item_id": "*", "ends_at": args.until})
            if not items:
                print("nothing to subscribe: use --agents a,b --flows f --all", file=sys.stderr)
                return 2
            store.set_subscriptions(args.customer_id, items, replace=not args.add)
            c = store.get_customer(args.customer_id)
            ef = shop.entitled_flows(c)
            print(f"{args.customer_id}: {len(shop.entitled_agents(c))} agents, {len(ef)} flow{'s' if len(ef) != 1 else ''} ({', '.join(ef) or '-'})")
            return 0
        if what == "demo":
            from .shop.demo import render_text, seed_demo

            base = args.base_url or os.environ.get("CAF_PUBLIC_URL") or "http://localhost:8080"
            result = seed_demo(shop, base_url=base, rotate_keys=args.rotate_keys)
            print(json.dumps(result, indent=2, default=str) if args.json else render_text(result))
            return 0
        if what == "flows":
            return _cmd_shop_flows(shop, args)
        if what == "policy":
            if args.file:
                data = yaml.safe_load(Path(args.file).read_text(encoding="utf-8")) or {}
                c = shop.set_policy(args.customer_id, data)
                print(f"policy saved for {c['id']} (run mode {c['run_mode']})")
            print(yaml.safe_dump(shop.policy_of(store.get_customer(args.customer_id)).redacted(), sort_keys=False))
            return 0
        if what == "check":
            c = store.get_customer(args.customer_id)
            only = [a.strip() for a in args.only.split(",")] if args.only else None
            report = shop.check_agents(c, only, tier="fast" if args.fast else None)
            for row in report["agents"]:
                mark = "ok  " if row["live"] else "FAIL"
                print(f"  {mark} {row['agent_id']:<32} {row['status']:<14} {row['duration_ms'] / 1000:6.1f}s  {row.get('error') or ''}")
            print(f"\n{report['passed']}/{report['total']} agents passed on {report['provider']} for {c['id']}")
            return 0 if report["passed"] == report["total"] else 1
        if what == "usage":
            summary = store.usage_summary(args.customer_id, args.month)
            if args.json:
                print(json.dumps(summary, indent=2, default=str))
                return 0
            from .core.pricing import money

            print(f"usage {summary['month']}" + (f" for {args.customer_id}" if args.customer_id else "") + f": {summary['totals']['runs']} runs, "
                  f"{summary['totals']['llm_calls']} model calls, {summary['totals']['input_tokens']} in / {summary['totals']['output_tokens']} out tokens, "
                  f"{money(summary['totals'].get('cost_usd'))}")
            for line in summary["lines"]:
                print(f"  {line['customer_id']:<20} {line['agent_id']:<32} {line['runs']:>4} runs  {line['ok_runs']:>4} ok  "
                      f"{line['input_tokens'] or 0:>8} in {line['output_tokens'] or 0:>8} out  {money(line.get('cost_usd')):>10}")
            return 0
    except ShopError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 2


def main(argv: list[str] | None = None) -> int:
    if sys.stderr.isatty():  # live progress lines (which model is working on what) for interactive runs
        os.environ.setdefault("CAF_PROGRESS", "1")
    from .branding import PRODUCT

    parser = argparse.ArgumentParser(prog="caf", description=f"{PRODUCT.name} ({PRODUCT.engine})")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("agents", help="list the agents; `audit` checks what each declares, `certify` runs each through the battery")
    p.add_argument("what", nargs="?", choices=["audit", "certify"]); p.add_argument("--pack", help="audit, certify: one pack only (blueprint for the 28)")
    p.add_argument("--agent", action="append", help="certify: only this agent (repeatable)")
    p.add_argument("--fast", action="store_true", help="certify: skip the large-input trial")
    p.add_argument("--live", action="store_true", help="certify: run each agent's self-test on the server's own model (billed); by default no live model is called")
    p.add_argument("--report", help="certify: also write the results as Markdown to this file")
    p.add_argument("--verbose", action="store_true", help="audit: list the info findings too")
    p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_agents)
    p = sub.add_parser("describe"); p.add_argument("agent_id"); p.set_defaults(fn=cmd_describe)
    p = sub.add_parser("run"); p.add_argument("agent_id"); p.add_argument("--input"); p.add_argument("--set", action="append", default=[])
    p.add_argument("--selftest", action="store_true"); p.add_argument("--auto-approve", action="store_true"); p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("orchestrate"); p.add_argument("goal"); p.add_argument("--playbook"); p.add_argument("--agent"); p.add_argument("--input")
    p.add_argument("--auto-approve", action="store_true"); p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_orchestrate)
    p = sub.add_parser("playbooks"); p.set_defaults(fn=cmd_playbooks)
    p = sub.add_parser("models", help="which model each model step uses, and why")
    p.add_argument("--provider", help="show the mapping for another provider (e.g. anthropic_foundry)")
    p.add_argument("--markdown", help="write the routing table as Markdown to this path"); p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_models)
    p = sub.add_parser("health"); p.add_argument("--live", action="store_true", help="probe with the configured model provider")
    p.add_argument("--fast", action="store_true", help="with --live: run every model step on the fast tier (cheaper, quicker)")
    p.add_argument("--packs", action="store_true", help="probe the pack agents too (Campaign Studio, ARM SDLC Factory ...): one model call each when live")
    p.add_argument("--only", help="comma-separated agent ids to probe")
    p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_health)
    p = sub.add_parser("serve", help="REST + MCP + A2A service")
    p.add_argument("--host", default=os.environ.get("CAF_HOST", "127.0.0.1"), help="bind address (0.0.0.0 to serve other machines; set CAF_API_TOKEN)")
    p.add_argument("--port", type=int, default=int(os.environ.get("CAF_PORT", "8080"))); p.set_defaults(fn=cmd_serve)
    p = sub.add_parser("mcp"); p.set_defaults(fn=cmd_mcp)
    p = sub.add_parser("export-catalog"); p.add_argument("out"); p.set_defaults(fn=cmd_export)
    p = sub.add_parser("shop", help="the Agent Shop: customers, keys, subscriptions, policies, checks, usage")
    ss = p.add_subparsers(dest="shop_cmd", required=True)
    q = ss.add_parser("customers", help="list customers"); q.add_argument("--json", action="store_true"); q.set_defaults(fn=cmd_shop)
    q = ss.add_parser("add", help="add a customer"); q.add_argument("customer_id"); q.add_argument("name")
    q.add_argument("--mode", default="coforge_cloud", choices=["coforge_cloud", "coforge_cloud_customer_model", "customer_cloud"]); q.add_argument("--contact"); q.set_defaults(fn=cmd_shop)
    q = ss.add_parser("key", help="issue an API key (shown once)"); q.add_argument("customer_id"); q.add_argument("--label"); q.set_defaults(fn=cmd_shop)
    q = ss.add_parser("workspace", help="a customer's own estate (sites, platforms, standards) and their own skills (brand voice, boilerplate, rules), given to the agents they call")
    q.add_argument("customer_id", nargs="?", default="_operator"); q.add_argument("--set", help="YAML or JSON file to save")
    q.add_argument("--clear", action="store_true"); q.add_argument("--template", action="store_true", help="print the roles to fill in"); q.set_defaults(fn=cmd_shop)
    q = ss.add_parser("revoke", help="revoke a key"); q.add_argument("customer_id"); q.add_argument("key_id"); q.set_defaults(fn=cmd_shop)
    q = ss.add_parser("subscribe", help="set what a customer rents"); q.add_argument("customer_id"); q.add_argument("--agents", help="comma-separated agent ids")
    q.add_argument("--flows", help="comma-separated flow ids (a flow is rented as a whole)"); q.add_argument("--playbooks", help=argparse.SUPPRESS)
    q.add_argument("--all", action="store_true", help="every agent and flow")
    q.add_argument("--until", help="YYYY-MM-DD"); q.add_argument("--add", action="store_true", help="add to the existing subscriptions instead of replacing them"); q.set_defaults(fn=cmd_shop)
    q = ss.add_parser("policy", help="show or set a customer's policy"); q.add_argument("customer_id"); q.add_argument("--file", help="YAML or JSON policy to apply"); q.set_defaults(fn=cmd_shop)
    q = ss.add_parser("demo", help="create (or reset) the two demo customers, Harbor Bank over MCP and Xperion Airways over A2A, and print how each connects")
    q.add_argument("--base-url", help="the address customers use (default: CAF_PUBLIC_URL)"); q.add_argument("--rotate-keys", action="store_true", help="revoke their keys and issue new ones")
    q.add_argument("--json", action="store_true"); q.set_defaults(fn=cmd_shop)
    q = ss.add_parser("check", help="run the subscribed agents' self-tests on the customer's model"); q.add_argument("customer_id")
    q.add_argument("--only", help="comma-separated agent ids"); q.add_argument("--fast", action="store_true"); q.set_defaults(fn=cmd_shop)
    q = ss.add_parser("usage", help="metered usage per customer and agent"); q.add_argument("customer_id", nargs="?"); q.add_argument("--month", help="YYYY-MM")
    q.add_argument("--json", action="store_true"); q.set_defaults(fn=cmd_shop)
    q = ss.add_parser("flows", help="orchestration flows: list, show, save, publish, unpublish, clone, delete, test"); q.set_defaults(fn=cmd_shop)
    fs = q.add_subparsers(dest="flow_cmd")
    fs.add_parser("list", help="every flow (built-in and custom)")
    r = fs.add_parser("show", help="a flow as YAML (edit it and save it under a new id)"); r.add_argument("flow_id")
    r = fs.add_parser("save", help="create or update a custom flow from a YAML/JSON file"); r.add_argument("file"); r.add_argument("--id", dest="flow_id")
    r.add_argument("--publish", action="store_true")
    r = fs.add_parser("publish", help="publish: storefront listing, run_flow_<id> MCP tool, REST and A2A"); r.add_argument("flow_id")
    r = fs.add_parser("unpublish"); r.add_argument("flow_id")
    r = fs.add_parser("clone", help="copy a flow (built-in ones too) into an editable draft"); r.add_argument("flow_id"); r.add_argument("new_id"); r.add_argument("--name")
    r = fs.add_parser("delete"); r.add_argument("flow_id")
    r = fs.add_parser("test", help="run the flow once with sample inputs"); r.add_argument("flow_id"); r.add_argument("--customer", help="on this customer's model")
    args = parser.parse_args(argv)
    try:
        return int(args.fn(args) or 0)
    except BrokenPipeError:  # output piped into head/less
        return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
