"""Settings: .env loading (local runs) and ${VAR:-default} interpolation."""

from __future__ import annotations

import os


def test_load_dotenv_parsing(tmp_path, monkeypatch):
    from caf.config import load_dotenv

    env = tmp_path / ".env"
    env.write_text(
        "# comment\n"
        "CAF_T_PLAIN=value\n"
        "export CAF_T_EXPORTED=yes\n"
        "CAF_T_INLINE=fixture          # none | fixture | serpapi\n"
        "CAF_T_QUOTED=\"has # hash and spaces\"\n"
        "CAF_T_SINGLE='single'\n"
        "CAF_T_EMPTY=\n"
        "CAF_T_COMMENT_ONLY=                       # required for docker compose: openssl rand -hex 32\n"
        "CAF_T_EXISTING=from-file\n"
        "not a variable line\n",
        encoding="utf-8",
    )
    names = ["CAF_T_PLAIN", "CAF_T_EXPORTED", "CAF_T_INLINE", "CAF_T_QUOTED", "CAF_T_SINGLE", "CAF_T_EMPTY", "CAF_T_COMMENT_ONLY"]
    for n in names:
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setenv("CAF_T_EXISTING", "from-shell")
    try:
        loaded = load_dotenv(env)
        assert os.environ["CAF_T_PLAIN"] == "value" and os.environ["CAF_T_EXPORTED"] == "yes"
        assert os.environ["CAF_T_INLINE"] == "fixture"
        assert os.environ["CAF_T_QUOTED"] == "has # hash and spaces" and os.environ["CAF_T_SINGLE"] == "single"
        assert "CAF_T_EMPTY" not in os.environ and "CAF_T_COMMENT_ONLY" not in os.environ  # empty means unset
        assert os.environ["CAF_T_EXISTING"] == "from-shell"  # the real environment wins
        assert set(loaded) == set(names) - {"CAF_T_EMPTY", "CAF_T_COMMENT_ONLY"}
    finally:
        for n in names:
            os.environ.pop(n, None)


def test_dotenv_key_selects_the_provider(tmp_path, monkeypatch):
    from caf.config import load_settings

    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.yaml").write_text("llm:\n  provider: ${CAF_LLM_PROVIDER:-auto}\n", encoding="utf-8")
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-test\nCAF_LLM_PROVIDER=auto\n", encoding="utf-8")
    monkeypatch.setenv("CAF_HOME", str(tmp_path))
    for n in ("ANTHROPIC_API_KEY", "CAF_LLM_PROVIDER", "AZURE_OPENAI_API_KEY", "OPENAI_API_KEY", "CAF_BEDROCK_REGION", "OLLAMA_HOST", "CAF_ENV_FILE"):
        monkeypatch.delenv(n, raising=False)
    try:
        settings = load_settings()
        assert settings.llm.resolved_provider() == "anthropic"
    finally:
        os.environ.pop("ANTHROPIC_API_KEY", None)
        os.environ["CAF_LLM_PROVIDER"] = os.environ.get("CAF_TEST_LLM_PROVIDER", "offline")


def test_interpolation_default_applies_to_empty_values(monkeypatch):
    from caf.config import _interpolate

    monkeypatch.setenv("CAF_T_BLANK", "")
    monkeypatch.setenv("CAF_T_SET", "x")
    assert _interpolate("${CAF_T_BLANK:-fallback}") == "fallback"
    assert _interpolate("${CAF_T_SET:-fallback}") == "x"
    assert _interpolate("${CAF_T_BLANK}") == ""
    assert _interpolate({"a": ["${CAF_T_MISSING_XYZ:-d}"]}) == {"a": ["d"]}


def test_env_example_loads_cleanly(tmp_path, monkeypatch):
    """Copying .env.example to .env must not turn its inline comments into values (tokens, host allowlists ...)."""
    from pathlib import Path

    from caf.config import load_dotenv

    example = Path(__file__).resolve().parents[1] / ".env.example"
    keys = [line.split("=", 1)[0].strip() for line in example.read_text(encoding="utf-8").splitlines()
            if "=" in line and not line.lstrip().startswith("#")]
    for k in keys:
        monkeypatch.delenv(k, raising=False)
    try:
        load_dotenv(example)
        for k in keys:
            assert "#" not in os.environ.get(k, ""), k
        assert "CAF_API_TOKEN" not in os.environ and "CAF_ALLOWED_HOSTS" not in os.environ and "CAF_CORS_ORIGINS" not in os.environ
        assert os.environ.get("CAF_SEARCH_PROVIDER") == "fixture"
    finally:
        for k in keys:
            os.environ.pop(k, None)
        os.environ["CAF_LLM_PROVIDER"] = os.environ.get("CAF_TEST_LLM_PROVIDER", "offline")

