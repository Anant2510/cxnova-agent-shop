"""Write docs/LIVENESS.md: proof that every blueprint agent is live under the framework.

    python3 scripts/liveness_report.py            # offline probe (no model cost)
    python3 scripts/liveness_report.py --live     # probe on the configured model provider
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("CAF_HOME", str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "docs" / "LIVENESS.md"))
    args = ap.parse_args()
    from caf.core import get_agent
    from caf.health import liveness
    from caf.orchestrator import coverage

    report = liveness(offline=not args.live)
    cov = coverage()
    lines = [
        "# Agent liveness report", "",
        f"Checked {report['checked_at']} - probe mode: **{report['mode']}** - **{report['live']}/{report['total']} agents live**"
        + (" (all blueprint agents registered)" if not report["missing"] else f" - missing: {report['missing']}"), "",
        "Each probe runs the agent's full LangGraph sub-graph from its `selftest_input()` (deterministic stages, validators, "
        "connectors, exporters) and validates the result against the agent's output contract. In offline mode model steps are "
        "answered by the offline model, so this proves wiring and the deterministic core; run with `--live` to send model steps "
        "to the configured provider.", "",
        "| # | Category | Agent | Kind | Status | Graph stages | Checks (pass/warn/fail) | Playbooks | Time |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(report["agents"], 1):
        try:
            label = get_agent(r["agent_id"]).spec.category_label
        except KeyError:
            label = "-"
        c = r.get("checks") or {}
        status = ("LIVE - " if r["live"] else "DOWN - ") + str(r["status"])
        lines.append(f"| {i} | {label} | {r.get('name', r['agent_id'])} | {r.get('kind', '-')} | {status} | {' > '.join(r.get('graph_nodes') or [])} | "
                     f"{c.get('pass', 0)}/{c.get('warn', 0)}/{c.get('fail', 0)} | {', '.join(cov.get(r['agent_id'], [])) or '-'} | {r['duration_ms'] / 1000:.1f}s |")
    degraded = [r for r in report["agents"] if r.get("degraded_reasons")]
    if degraded:
        lines += ["", "## Degraded-mode notes", ""]
        for r in degraded:
            lines.append(f"- **{r.get('name', r['agent_id'])}**: {'; '.join(r['degraded_reasons'])}")
    down = [r for r in report["agents"] if not r["live"]]
    if down:
        lines += ["", "## Down", ""] + [f"- {r['agent_id']}: {r.get('error')}" for r in down]
    Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{report['live']}/{report['total']} live -> {args.out}")
    return 0 if report["all_live"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
