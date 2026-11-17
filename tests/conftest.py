import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("CAF_HOME", str(ROOT))
os.environ["CAF_LLM_PROVIDER"] = os.environ.get("CAF_TEST_LLM_PROVIDER", "offline")
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
