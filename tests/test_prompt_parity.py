"""Prompt parity: the browser console renders the same templates with Nunjucks.

Each agent's self-test is run with a recording PromptStore; every (template, variables) pair is
then rendered by Node + Nunjucks with the console's filter set and compared with Jinja2's output
(whitespace-normalised). Skipped when node/nunjucks are unavailable.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NUNJUCKS = ROOT / "console" / "node_modules" / "nunjucks"

NODE_RENDER = r"""
const nunjucks = require(process.env.NUNJUCKS_PATH);
const fs = require("fs");
const cases = JSON.parse(fs.readFileSync(0, "utf8"));
const env = new nunjucks.Environment(new nunjucks.FileSystemLoader(process.env.PROMPTS_DIR), { autoescape: false, trimBlocks: true, lstripBlocks: true });
const arr = (v) => (v === undefined || v === null ? [] : Array.isArray(v) ? v : [v]);
env.addFilter("tojson", (v, indent) => JSON.stringify(v === undefined ? null : v, null, indent || undefined));
env.addFilter("bullets", (items, prefix = "- ") => arr(items).map((i) => prefix + i).join("\n"));
env.addFilter("numbered", (items) => arr(items).map((i, n) => `${n + 1}. ${i}`).join("\n"));
env.addFilter("join", (v, d = "", attr) => arr(v).map((x) => (attr ? (x || {})[attr] : x)).filter((x) => x !== undefined && x !== null).join(d));
env.addFilter("length", (v) => (v === undefined || v === null ? 0 : typeof v === "object" && !Array.isArray(v) ? Object.keys(v).length : v.length || 0));
env.addFilter("first", (v) => arr(v)[0] ?? "");
env.addFilter("last", (v) => { const a = arr(v); return a.length ? a[a.length - 1] : ""; });
env.addFilter("upper", (v) => String(v ?? "").toUpperCase());
env.addFilter("lower", (v) => String(v ?? "").toLowerCase());
env.addFilter("trim", (v) => String(v ?? "").trim());
const isObj = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
const truthy = (v) => Array.isArray(v) ? (v.length ? v.map(truthy) : null) : isObj(v) ? (Object.keys(v).length ? Object.fromEntries(Object.entries(v).map(([k, x]) => [k, truthy(x)])) : null) : v;
const out = cases.map((c) => { try { return { ok: true, text: env.render(c.template, truthy(c.vars) || {}) }; } catch (e) { return { ok: false, text: String(e.message || e) }; } });
process.stdout.write(JSON.stringify(out));
"""


def _norm(text: str) -> str:
    text = re.sub(r"(\d+)\.0\b", r"\1", text)  # 1.0 (Python) vs 1 (JS) number formatting
    text = text.replace("True", "true").replace("False", "false").replace("None", "null")
    text = text.replace("[]", "null").replace("{}", "null")  # tojson of empty containers (normalised to null in the browser)
    return re.sub(r"\s+", " ", text).strip()


@pytest.mark.skipif(not shutil.which("node") or not NUNJUCKS.exists(), reason="node + console/node_modules/nunjucks required")
def test_templates_render_identically_in_jinja2_and_nunjucks(ctx):
    from caf.core import expected_agent_ids, get_agent
    from caf.core.prompts import PromptStore

    recorded: list[dict] = []
    original = PromptStore.render

    def recording(self, agent_id, name, **variables):
        system, user = original(self, agent_id, name, **variables)
        try:
            payload = json.loads(json.dumps(variables, default=str))
        except Exception:
            payload = None
        if payload is not None:
            recorded.append({"template": f"{agent_id}/{name}.j2", "vars": payload,
                             "jinja": (system + "\n---- USER ----\n" if system else "") + user})
        return system, user

    PromptStore.render = recording
    try:
        for agent_id in expected_agent_ids():
            try:
                agent = get_agent(agent_id)
            except KeyError:
                continue
            agent.run(agent.selftest_input(), ctx)
    finally:
        PromptStore.render = original

    assert recorded, "no prompts were rendered"
    seen: dict[str, dict] = {}
    for r in recorded:
        seen.setdefault(r["template"], r)
    cases = list(seen.values())
    env = {**os.environ, "NUNJUCKS_PATH": str(NUNJUCKS), "PROMPTS_DIR": str(ROOT / "config" / "prompts")}
    proc = subprocess.run(["node", "-e", NODE_RENDER], input=json.dumps(cases), capture_output=True, text=True, env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr
    results = json.loads(proc.stdout)
    mismatches = []
    for case, res in zip(cases, results):
        if not res["ok"]:
            mismatches.append(f"{case['template']}: nunjucks error {res['text'][:200]}")
            continue
        jinja = _norm(case["jinja"].replace("\n---- USER ----\n", "\n---- USER ----\n"))
        nunj = _norm(res["text"])
        if jinja != nunj:
            # locate first difference for a helpful message
            i = next((k for k, (a, b) in enumerate(zip(jinja, nunj)) if a != b), min(len(jinja), len(nunj)))
            mismatches.append(f"{case['template']}: differs at {i}: jinja='{jinja[max(0, i - 60):i + 60]}' nunjucks='{nunj[max(0, i - 60):i + 60]}'")
    assert not mismatches, "\n".join(mismatches)
