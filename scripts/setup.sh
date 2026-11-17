#!/usr/bin/env bash
# One-step local setup for macOS and Linux: virtual environment, dependencies, Chromium, .env and a liveness check.
#   ./scripts/setup.sh
set -euo pipefail
cd "$(dirname "$0")/.."

PY=""
for c in python3.13 python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3.11 or newer was not found. Install it (macOS: brew install python@3.12) and run this script again." >&2
  exit 1
fi
echo "==> Using $("$PY" --version) ($(command -v "$PY"))"

[ -d .venv ] || "$PY" -m venv .venv
# shellcheck disable=SC1091
. .venv/bin/activate
python -m pip install --quiet --upgrade pip
echo "==> Installing the framework and its dependencies (a few minutes the first time)"
pip install --quiet -e ".[anthropic,mcp,browser,dev]"

echo "==> Installing Chromium for the accessibility, heatmap and variation agents"
python -m playwright install chromium || echo "    Chromium download failed: those agents will run in degraded mode until it is installed."

[ -f .env ] || cp .env.example .env

echo "==> Liveness check (offline model, no API cost)"
caf health

cat <<'EOF'

Setup complete. Next:
  1. Put your Anthropic key in .env:   ANTHROPIC_API_KEY=sk-ant-...
  2. In each new terminal:             source .venv/bin/activate
  3. Try it:                           caf health --live
                                       caf serve        (then open http://127.0.0.1:8080/docs)
EOF
