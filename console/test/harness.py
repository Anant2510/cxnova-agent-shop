"""Headless test harness for the hosted console.

Builds console/dist/index.html, wraps it in the artifact skeleton under a strict CSP (no
'unsafe-eval', no network except the pinned CDN scripts, which are served from console/vendor),
injects a mock `window.claude` and runs every browser agent.

    python3 console/test/harness.py                     # offline probe of all agents
    python3 console/test/harness.py --llm mock          # live code path against a schema-aware mock Claude
    python3 console/test/harness.py --agents a,b --verbose
    python3 console/test/harness.py --orchestrate campaign_launch
    python3 console/test/harness.py --screenshot out.png

Exit code 0 when every selected agent is live and the page logged no errors.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "console"))
VENDOR = ROOT / "console" / "vendor"
CDN_MAP = {
    "nunjucks-slim.min.js": "nunjucks-slim.min.js", "marked.min.js": "marked.min.js", "purify.min.js": "purify.min.js",
    "qrcode.js": "qrcode.js", "axe.min.js": "axe.min.js",
}
ORIGIN = "https://console.test"
CSP = ("default-src 'none'; script-src 'unsafe-inline' https://cdn.jsdelivr.net; style-src 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src https://fonts.gstatic.com; img-src data: blob:; frame-src 'self' about: data: blob:; child-src 'self' about: blob:; connect-src 'none'")

MOCK = r"""
(() => {
  const mode = %MODE%;
  function schemaFrom(prompt) { const i = prompt.lastIndexOf("JSON Schema:\n"); return i >= 0 ? JSON.parse(prompt.slice(i + 13)) : { type: "object" }; }
  async function sample(input, opts) { window.__mockCalls = (window.__mockCalls || 0) + 1; return { text: "[mock-claude] " + String(input).slice(0, 80), truncated: false, modelTierApplied: (opts || {}).modelTier || "default" }; }
  sample.json = async (input, opts) => {
    window.__mockCalls = (window.__mockCalls || 0) + 1;
    window.__mockTiers = (window.__mockTiers || []).concat([(opts || {}).modelTier]);
    const schema = schemaFrom(String(input));
    const value = window.CAF.offline.generate(schema, String(input).replace("[offline]", ""));
    return JSON.parse(JSON.stringify(value).split("[offline]").join("[mock-claude]"));
  };
  sample.limits = async () => ({ maxPromptBytes: 262144, tools: { maxCount: 16 } });
  const downloads = { save: async ({ filename, data }) => { (window.__saved = window.__saved || []).push(filename); return { status: "saved" }; } };
  window.claude = { use: async (name) => (mode === "mock" && name === "sample") ? sample : name === "downloads" ? downloads : null };
})();
"""

SKELETON = """<!doctype html><html><head><meta charset=utf8><meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta http-equiv="Content-Security-Policy" content="{csp}"><style>:root{{color-scheme:light;padding-top:env(safe-area-inset-top);padding-bottom:env(safe-area-inset-bottom)}}body{{margin:0;font:14px system-ui}}img{{max-width:100%}}[hidden]{{display:none!important}}</style></head><body>{body}</body></html>"""


def wait_until(page, expr: str, timeout: float = 180.0) -> None:
    """Poll from Python: Playwright's in-page polling uses eval, which the strict CSP (correctly) blocks."""
    import time

    end = time.time() + timeout
    while time.time() < end:
        if page.evaluate(expr):
            return
        page.wait_for_timeout(250)
    raise TimeoutError(f"timed out waiting for: {expr[:120]}")


def run(args: argparse.Namespace) -> int:
    from build import build
    from playwright.sync_api import sync_playwright

    import os
    import tempfile

    out_file = Path(tempfile.gettempdir()) / f"caf-console-test-{os.getpid()}.html"
    info = build(out_file)
    if args.verbose:
        print(json.dumps(info, indent=2))
    body = out_file.read_text(encoding="utf-8")
    out_file.unlink(missing_ok=True)
    page_html = SKELETON.format(csp=CSP, body=body)
    errors: list[str] = []
    logs: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": args.width, "height": 1000}, color_scheme=args.scheme)
        page = ctx.new_page()

        def route(r):
            url = r.request.url
            if url.startswith(ORIGIN):
                return r.fulfill(status=200, body=page_html, headers={"content-type": "text/html; charset=utf-8"})
            if url.startswith("https://fonts.googleapis.com/"):
                return r.fulfill(status=200, body="/* fonts unavailable offline */", headers={"content-type": "text/css"})
            if url.startswith("https://cdn.jsdelivr.net/"):
                name = url.rsplit("/", 1)[-1]
                f = VENDOR / CDN_MAP.get(name, name)
                if f.exists():
                    return r.fulfill(status=200, body=f.read_bytes(), headers={"content-type": "application/javascript", "access-control-allow-origin": "*"})
            return r.abort()

        page.route("**/*", route)
        page.add_init_script(MOCK.replace("%MODE%", json.dumps(args.llm)))
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on("console", lambda m: (errors if m.type == "error" else logs).append(f"console.{m.type}: {m.text}"))
        page.goto(ORIGIN + "/", wait_until="load")
        wait_until(page, "!!(window.CAF && window.CAFUI && Object.keys(window.CAF.health).length === window.CAF.order.length && window.CAF.order.every(id => window.CAF.health[id] && !window.CAF.health[id].running))")
        ids = page.evaluate("window.CAF.order")
        selected = [a for a in (args.agents.split(",") if args.agents else ids) if a]
        if args.llm == "mock":
            wait_until(page, "window.CAF.llm.state !== 'checking'", 20)
            results = page.evaluate("""async (ids) => { const out = {}; for (const id of ids) { const r = await CAF.runAgent(id, CAF.selftest(id), {}); out[id] = {status: r.status, summary: r.summary, error: r.error, calls: r.metrics.llm_calls, checks: r.checks.length, artifacts: r.artifacts.length, actions: r.pending_actions.length}; } return out; }""", selected)
        else:
            results = page.evaluate("(ids) => Object.fromEntries(ids.map(id => { const h = CAF.health[id]; const r = h.result || {}; return [id, {status: h.status, summary: h.summary, error: h.error, calls: (r.metrics||{}).llm_calls||0, checks: (r.checks||[]).length, artifacts: (r.artifacts||[]).length, actions: (r.pending_actions||[]).length}]; }))", selected)
        orch = None
        if args.orchestrate:
            orch = page.evaluate("""async (pb) => { const inputs = {url: 'https://www.meridian-mutual.example/products/home-insurance.html',
                  ...(pb === 'competitive_pitch' ? {competitors: ['Harborline Insurance', 'Crestview Assurance'], industry: 'insurance'} : {})};
                const planned = await CAF.orch.makePlan('Run the playbook end to end', {playbook: pb, inputs});
                const run = CAF.orch.newRun('Run the playbook end to end', inputs, planned); await CAF.orch.run(run);
                for (const a of CAF.orch.collectApprovals(run)) await CAF.orch.decide(run, a.key, 'approve');
                return {status: run.status, headline: run.final && run.final.headline, steps: Object.fromEntries(Object.entries(run.results).map(([k, v]) => [k, v.status + (v.error ? ': ' + v.error : '')]))}; }""", args.orchestrate)
        if args.screenshot:
            page.evaluate("window.scrollTo(0,0)")
            page.screenshot(path=args.screenshot, full_page=args.full)
        browser.close()

    live = {k: v for k, v in results.items() if v["status"] in ("ok", "degraded", "needs_approval")}
    width = max(len(k) for k in results) if results else 10
    for k, v in results.items():
        mark = "LIVE" if k in live else "DOWN"
        detail = v["error"] or v["summary"] or ""
        print(f"  {mark}  {k:<{width}}  {str(v['status']):<15} calls={v.get('calls') or 0:<3} checks={v.get('checks') or 0:<3} artifacts={v.get('artifacts') or 0:<3} {detail[:110]}")
    print(f"\n{len(live)}/{len(results)} browser agents live ({args.llm} model); missing browser runtimes: {info['missing_browser_agents']}")
    if orch:
        print("\norchestrate:", json.dumps(orch, indent=2))
    real_errors = [e for e in errors if "favicon" not in e]
    if real_errors:
        print("\nPAGE ERRORS:")
        for e in real_errors[:40]:
            print("  ", e[:400])
    if args.verbose:
        for l in logs[:60]:
            print("  ", l[:300])
    return 0 if len(live) == len(results) and not real_errors else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", choices=["offline", "mock"], default="offline")
    ap.add_argument("--agents", default="")
    ap.add_argument("--orchestrate", default="")
    ap.add_argument("--screenshot", default="")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--width", type=int, default=1360)
    ap.add_argument("--scheme", default="light", choices=["light", "dark"])
    ap.add_argument("--verbose", action="store_true")
    raise SystemExit(run(ap.parse_args()))
