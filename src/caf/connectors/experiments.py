"""Experiment connector + platform-neutral experiment definition.

The same ExperimentDefinition is read/written whether the test lives in Optimizely, VWO,
AB Tasty, LaunchDarkly, Adobe Target or an internal registry.
"""

from __future__ import annotations

import json
import threading
from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel, Field

from ..core.security import is_safe_id
from .base import Connector, connector


class Metric(BaseModel):
    id: str
    name: str
    type: Literal["conversion", "revenue", "count", "duration", "rate"] = "conversion"
    event: str | None = None


class Variant(BaseModel):
    id: str
    name: str
    allocation: float = Field(description="Percent of experiment traffic")
    is_control: bool = False
    description: str = ""


class Audience(BaseModel):
    devices: list[str] = Field(default_factory=list)
    geo: list[str] = Field(default_factory=list)
    conditions: list[str] = Field(default_factory=list)


class ExperimentDefinition(BaseModel):
    id: str
    name: str
    status: Literal["draft", "running", "paused", "completed", "archived"] = "draft"
    platform: str = "internal"
    page_url: str = ""
    hypothesis: str = ""
    primary_metric: Metric | None = None
    secondary_metrics: list[Metric] = Field(default_factory=list)
    guardrail_metrics: list[Metric] = Field(default_factory=list)
    variants: list[Variant] = Field(default_factory=list)
    traffic_allocation: float = 100.0
    audience: Audience = Field(default_factory=Audience)
    start_date: str | None = None
    end_date: str | None = None
    planned_duration_days: int | None = None
    baseline_rate: float | None = None
    mde: float | None = Field(default=None, description="Relative minimum detectable effect, e.g. 0.05 = 5%")
    expected_daily_visitors: int | None = None
    qa_links: list[str] = Field(default_factory=list)
    instrumentation: dict[str, Any] = Field(default_factory=dict)
    owner: str | None = None


class Experiments(Connector):
    def list(self, status: str | None = None) -> list[ExperimentDefinition]: raise NotImplementedError
    def get(self, experiment_id: str) -> ExperimentDefinition | None: raise NotImplementedError
    def results(self, experiment_id: str) -> pd.DataFrame: raise NotImplementedError
    def upsert(self, definition: dict[str, Any] | ExperimentDefinition) -> ExperimentDefinition: raise NotImplementedError


@connector("experiments", "json")
class JsonExperiments(Experiments):
    """Local experiment registry (registry.json) with per-test results CSVs."""

    _lock = threading.Lock()

    def _registry_path(self):
        return self.path("path", "sample_data/experiments") / "registry.json"

    def _load(self) -> list[dict[str, Any]]:
        """Base registry plus experiments written during runs (run-local copies win)."""
        p = self._registry_path()
        items = json.loads(p.read_text(encoding="utf-8")).get("experiments", []) if p.exists() else []
        local = self.state_dir("_experiments") / "registry.json"   # a customer's experiments are that customer's alone
        if local.exists():
            extra = json.loads(local.read_text(encoding="utf-8")).get("experiments", [])
            ids = {e.get("id") for e in extra}
            items = [e for e in items if e.get("id") not in ids] + extra
        return items

    def list(self, status: str | None = None) -> list[ExperimentDefinition]:
        items = [ExperimentDefinition.model_validate(e) for e in self._load()]
        return [e for e in items if status is None or e.status == status]

    def get(self, experiment_id: str) -> ExperimentDefinition | None:
        return next((e for e in self.list() if e.id == experiment_id), None)

    def results(self, experiment_id: str) -> pd.DataFrame:
        if not is_safe_id(experiment_id):  # ids become file names: no separators or '..'
            raise ValueError(f"Invalid experiment id {experiment_id!r}")
        p = self.path("path", "sample_data/experiments") / "results" / f"{experiment_id}.csv"
        if not p.exists():
            return pd.DataFrame(columns=["variant", "segment", "visitors", "conversions", "revenue"])
        return pd.read_csv(p)

    def upsert(self, definition: dict[str, Any] | ExperimentDefinition) -> ExperimentDefinition:
        """Writes go to a run-local copy so sample data is never mutated."""
        exp = definition if isinstance(definition, ExperimentDefinition) else ExperimentDefinition.model_validate(definition)
        out = self.state_dir("_experiments") / "registry.json"
        with self._lock:
            items = json.loads(out.read_text(encoding="utf-8")).get("experiments", []) if out.exists() else []
            items = [e for e in items if e.get("id") != exp.id] + [exp.model_dump(mode="json")]
            out.write_text(json.dumps({"experiments": items}, indent=2), encoding="utf-8")
        return exp
