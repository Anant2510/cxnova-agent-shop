import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("CAF_HOME", str(ROOT))
os.environ["CAF_LLM_PROVIDER"] = os.environ.get("CAF_TEST_LLM_PROVIDER", "offline")
# The suite proves the framework on the blueprint's 28 agents and 8 playbooks; the agent packs (config/packs) are
# switched on by tests/test_packs.py, which proves them on top (its `packs` fixture).
os.environ.setdefault("CAF_PACKS_DISABLED", "*")
sys.path.insert(0, str(ROOT / "src"))


@pytest.fixture(scope="session", autouse=True)
def _runs_dir(tmp_path_factory):
    runs = tmp_path_factory.mktemp("runs")
    os.environ["CAF_RUNS_DIR"] = str(runs)
    os.environ["CAF_DATA_DIR"] = str(tmp_path_factory.mktemp("data"))  # the shop's SQLite file, never the repo's data/
    from caf.config import reset_settings_cache
    from caf.shop import reset_shops

    reset_settings_cache()
    reset_shops()
    yield runs


@pytest.fixture()
def packs(monkeypatch):
    """The agent packs switched on for one test (and off again after it)."""
    from caf.core import reload_agents
    from caf.orchestrator.playbooks import builtin_playbooks
    from caf.packs import reset_packs
    from caf.shop import reset_shops

    def reset():
        reset_packs()
        builtin_playbooks.cache_clear()
        reload_agents()
        reset_shops()
        try:
            from caf.api import console_host

            console_host._PACK_TAG.clear()
        except Exception:
            pass

    monkeypatch.setenv("CAF_PACKS_DISABLED", "")
    reset()
    yield
    monkeypatch.setenv("CAF_PACKS_DISABLED", "*")
    reset()


@pytest.fixture()
def ctx():
    from caf.core import RunContext

    return RunContext.create()


def browser_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            p.chromium.launch(args=["--no-sandbox"]).close()
        return True
    except Exception:
        return False
