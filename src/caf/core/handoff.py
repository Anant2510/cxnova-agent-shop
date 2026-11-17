"""Shared hand-off models: the common shapes agents use to pass work to each other.

Any analysis agent (Technical SEO, GEO, Accessibility, Heatmap, Traffic Report, Experiment Review,
Chart Summary ...) exposes `findings: list[Finding]` in its Output, so downstream agents such as
Experiment Ideation can consume evidence from any of them without knowing their internals.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Severity = Literal["low", "medium", "high", "critical"]


class Finding(BaseModel):
    title: str
    detail: str = ""
    severity: Severity = "medium"
    category: str = Field(default="", description="seo | geo | accessibility | behaviour | performance | analytics | content | experimentation")
    evidence: str = ""
    recommendation: str = ""
    target: str | None = Field(default=None, description="URL, selector or metric the finding is about")
    source_agent: str = ""


class Recommendation(BaseModel):
    title: str
    rationale: str = ""
    priority: Literal["P1", "P2", "P3"] = "P2"
    effort: Literal["low", "medium", "high"] = "medium"
    impact: Literal["low", "medium", "high"] = "medium"
    owner_role: str = ""


SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
