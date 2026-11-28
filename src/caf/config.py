"""Runtime settings: config/settings.yaml with ${ENV} interpolation and env overrides.

Environment variables can also live in a `.env` file in the framework home (or the file named by CAF_ENV_FILE);
it is read once when settings load, and variables already set in the real environment win.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z0-9_]+)(?::-([^}]*))?\}")
TRUTHY = {"1", "true", "yes", "on"}


def _interpolate(value: Any) -> Any:
    if isinstance(value, str):
        def repl(m: re.Match) -> str:  # shell semantics: ${VAR:-default} also applies when VAR is set but empty
            current = os.environ.get(m.group(1))
            if m.group(2) is not None:
                return current if current else m.group(2)
            return current or ""
        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    return value


def as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in TRUTHY


_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def load_dotenv(path: str | Path) -> list[str]:
    """Load KEY=VALUE lines from a .env file into os.environ without overriding variables that are already set.

    Supports comments, `export KEY=...`, single/double quotes and inline ` # comments` after unquoted values.
    Empty values are skipped (treated as unset). Returns the names that were set.
    """
    f = Path(path)
    if not f.is_file():
        return []
    loaded: list[str] = []
    for raw in f.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not _KEY.match(key):
            continue
        value = value.strip()
        if value[:1] in ("'", '"') and value.find(value[0], 1) > 0:
            value = value[1:value.find(value[0], 1)]
        elif value.startswith("#"):  # `KEY=   # comment` leaves the variable empty
            value = ""
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        if value and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


def find_home() -> Path:
    """Locate the framework home (the folder holding config/settings.yaml)."""
    env = os.environ.get("CAF_HOME")
    if env:
        return Path(env).resolve()
    here = Path(__file__).resolve()
    for parent in [Path.cwd(), *Path.cwd().parents, *here.parents]:
        if (parent / "config" / "settings.yaml").exists():
            return parent
    return Path.cwd()


class LLMSettings(BaseModel):
    provider: str = "auto"
    temperature: float | None = None  # None: each model's default (reasoning models accept nothing else)
    max_tokens: int = 4096
    timeout_s: float = 180.0
    throttle: dict[str, Any] = Field(default_factory=dict)
    providers: dict[str, dict[str, Any]] = Field(default_factory=dict)
    pricing: dict[str, Any] = Field(default_factory=dict)   # see Pricing in caf.core.pricing

    def resolved_provider(self) -> str:
        p = (os.environ.get("CAF_LLM_PROVIDER") or self.provider or "auto").strip().lower()
        if p != "auto":
            return p
        if os.environ.get("ANTHROPIC_API_KEY"):
            return "anthropic"
        if os.environ.get("ANTHROPIC_FOUNDRY_API_KEY") and (os.environ.get("ANTHROPIC_FOUNDRY_BASE_URL") or os.environ.get("ANTHROPIC_FOUNDRY_RESOURCE")):
            return "anthropic_foundry"
        if os.environ.get("AZURE_OPENAI_API_KEY") and os.environ.get("AZURE_OPENAI_ENDPOINT"):
            return "azure_openai"
        if os.environ.get("OPENAI_API_KEY"):
            return "openai"
        if os.environ.get("CAF_BEDROCK_REGION"):
            return "bedrock"
        if os.environ.get("OLLAMA_HOST"):
            return "ollama"
        return "offline"

    def provider_config(self, provider: str | None = None) -> dict[str, Any]:
        return dict(self.providers.get(provider or self.resolved_provider(), {}))

    def model_for(self, tier: str, provider: str | None = None) -> str:
        cfg = self.provider_config(provider)
        tiers = cfg.get("tiers", {})
        env = os.environ.get(f"CAF_MODEL_{tier.upper()}")
        if env:
            return env
        return tiers.get(tier) or tiers.get("balanced") or "offline"


class Settings(BaseModel):
    home: Path
    llm: LLMSettings = Field(default_factory=LLMSettings)
    connectors: dict[str, dict[str, Any]] = Field(default_factory=dict)
    brand: dict[str, Any] = Field(default_factory=dict)
    approvals: dict[str, Any] = Field(default_factory=dict)
    orchestrator: dict[str, Any] = Field(default_factory=dict)
    security: dict[str, Any] = Field(default_factory=dict)
    # Set on the settings of a customer's run (CustomerPolicy.settings_for), empty for the operator's own: who the run is
    # for, the systems that customer connected (as stored: credentials still encrypted) and what its policy lets agents do
    # with each. The connector hub and the security policy read it; see ConnectorHub and Policy.
    tenant: dict[str, Any] = Field(default_factory=dict)
    runs_dir: Path
    skills_dirs: list[Path]
    prompts_dirs: list[Path]
    sample_data: Path

    def resolve(self, p: str | Path) -> Path:
        path = Path(p)
        return path if path.is_absolute() else (self.home / path)

    def connector_config(self, kind: str) -> dict[str, Any]:
        return dict(self.connectors.get(kind, {}))

    @property
    def auto_approve(self) -> bool:
        env = os.environ.get("CAF_AUTO_APPROVE")
        if env is not None:
            return as_bool(env)
        return as_bool(self.approvals.get("auto_approve", False))

    @property
    def is_service(self) -> bool:
        """True when runs come from a remote surface (REST, MCP, A2A) rather than the local CLI."""
        return str(self.security.get("mode", "local")).lower() == "service"


def load_settings(path: str | Path | None = None) -> Settings:
    home = find_home()
    load_dotenv(os.environ.get("CAF_ENV_FILE") or home / ".env")
    cfg_path = Path(path) if path else home / "config" / "settings.yaml"
    raw: dict[str, Any] = {}
    if cfg_path.exists():
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    raw = _interpolate(raw)

    def _p(v: str | Path) -> Path:
        pth = Path(v)
        return pth if pth.is_absolute() else home / pth

    paths = raw.get("paths", {})
    runs_dir = _p(os.environ.get("CAF_RUNS_DIR") or paths.get("runs", "runs"))
    skills = [_p(d) for d in ([os.environ["CAF_SKILLS_DIR"]] if os.environ.get("CAF_SKILLS_DIR") else []) + paths.get("skills", ["config/skills"])]
    prompts = [_p(d) for d in ([os.environ["CAF_PROMPTS_DIR"]] if os.environ.get("CAF_PROMPTS_DIR") else []) + paths.get("prompts", ["config/prompts"])]
    sample = _p(paths.get("sample_data", "sample_data"))
    return Settings(
        home=home,
        llm=LLMSettings(**raw.get("llm", {})),
        connectors=raw.get("connectors", {}),
        brand=raw.get("brand", {}),
        approvals=raw.get("approvals", {}),
        orchestrator=raw.get("orchestrator", {}),
        security={k: v for k, v in (raw.get("security") or {}).items() if k != "mode"},  # the mode is set by the surface
        runs_dir=runs_dir,
        skills_dirs=skills,
        prompts_dirs=prompts,
        sample_data=sample,
    )


def data_dir(settings: Any = None) -> Path:
    """Where the layer keeps its records (the shop's and the orchestrator's SQLite files): $CAF_DATA_DIR, else <home>/data."""
    env = os.environ.get("CAF_DATA_DIR")
    if env:
        return Path(env)
    home = getattr(settings, "home", None) or Path(os.environ.get("CAF_HOME") or Path.cwd())
    return Path(home) / "data"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
