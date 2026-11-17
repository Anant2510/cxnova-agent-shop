"""UTM Generation - deterministic link tagging driven by an organisation YAML taxonomy.

No model involved: taxonomy rules, validation and URL encoding are plain Python, so links are
reproducible and analytics-platform neutral (GA4, Adobe Analytics, Piwik PRO, Matomo).
"""

from __future__ import annotations

import csv
import io
import re
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, Field

from ...core import AgentKind, AgentSpec, BaseAgent, Category, Check, RunContext, Tier, register
from ...core.textutil import slugify


class UTMInput(BaseModel):
    base_url: str = Field(description="Landing page URL to tag")
    campaign: str | None = Field(default=None, description="Campaign name. Omit to compose it from campaign_parts with the taxonomy pattern")
    campaign_parts: dict[str, str] = Field(default_factory=dict, description="Pattern parts, e.g. year, quarter, market, product, objective")
    channels: list[str] = Field(default_factory=lambda: ["email"], description="Channel keys from the taxonomy, or ['all'] for the full matrix")
    mode: Literal["matrix", "single"] = Field(default="matrix", description="matrix = one link per channel; single = one link with explicit source/medium")
    source: str | None = Field(default=None, description="utm_source override (single mode)")
    medium: str | None = Field(default=None, description="utm_medium override (single mode)")
    content: str | None = Field(default=None, description="utm_content, e.g. creative or placement")
    term: str | None = Field(default=None, description="utm_term (paid search keyword)")
    utm_id: str | None = Field(default=None, description="utm_id (campaign ID for cost imports)")


class UTMLink(BaseModel):
    channel: str
    source: str
    medium: str
    campaign: str
    content: str | None = None
    term: str | None = None
    url: str


class UTMOutput(BaseModel):
    campaign: str
    links: list[UTMLink]
    taxonomy_version: str
    compliant: bool


def _clean(value: str | None, case: str = "lower") -> str | None:
    if value is None or str(value).strip() == "":
        return None
    v = slugify(str(value), max_len=80).replace("-", "_")
    return v.lower() if case == "lower" else v


def build_url(base_url: str, params: dict[str, str | None]) -> str:
    parts = urlsplit(base_url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not k.lower().startswith("utm_")]
    query += [(k, v) for k, v in params.items() if v]
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", urlencode(query, doseq=True), parts.fragment))


@register
class UTMGenerationAgent(BaseAgent):
    spec = AgentSpec(
        id="utm_generation",
        name="UTM Generation",
        category=Category.STRATEGY,
        summary="Builds consistent, correctly-encoded UTM tracking links from a YAML naming taxonomy - single links or a whole channel matrix.",
        kind=AgentKind.DETERMINISTIC,
        tier=Tier.NONE,
        connectors=[],
        skills=["utm_taxonomy"],
        keywords=["utm", "tracking link", "campaign link", "tag links", "attribution", "url parameters"],
        produces=["utm_links.csv", "utm_links.xlsx"],
        feeds=["qr_code_generation", "email_creation"],
        primary_inputs=[],
    )
    Input = UTMInput
    Output = UTMOutput

    def selftest_input(self) -> dict[str, Any]:
        return {"base_url": "https://www.meridian-mutual.example/products/home-insurance.html",
                "campaign_parts": {"year": "2026", "quarter": "q4", "market": "us", "product": "home", "objective": "acquisition"},
                "channels": ["email", "paid_social", "organic_social", "qr"], "content": "hero_cta"}

    def stages(self):
        return [("resolve_campaign", self.resolve_campaign), ("build_links", self.build_links), ("export", self.export)]

    # -- stage 1 --------------------------------------------------------------------------------
    def resolve_campaign(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp = state["inputs"]
        tax = ctx.skill("utm_taxonomy")
        checks: list[Check] = []
        campaign = inp.get("campaign")
        parts = {k: _clean(v) for k, v in (inp.get("campaign_parts") or {}).items()}
        brief = ctx.upstream_output("campaign_brief_generation")
        if not campaign and not parts and brief:
            campaign = brief.get("campaign_name")
        if not campaign:
            pattern = tax["campaign_pattern"]
            missing = [p for p in re.findall(r"{(\w+)}", pattern) if not parts.get(p)]
            if missing:
                checks.append(Check(rule="campaign.parts", status="fail", severity="high",
                                    message=f"Campaign pattern '{pattern}' needs parts {missing}."))
                campaign = "_".join(v for v in parts.values() if v) or "unnamed_campaign"
            else:
                campaign = pattern.format(**parts)
                for name, rx in tax.get("campaign_parts", {}).items():
                    if parts.get(name) and not re.match(rx, parts[name]):
                        checks.append(Check(rule=f"campaign.part.{name}", status="fail", severity="medium",
                                            message=f"'{parts[name]}' does not match the {name} rule {rx}."))
        campaign = _clean(campaign, tax.get("case", "lower")) or "unnamed_campaign"
        ok = bool(re.match(tax["value_pattern"], campaign)) and len(campaign) <= tax.get("max_value_length", 60)
        checks.append(Check(rule="campaign.naming", status="pass" if ok else "fail", severity="high",
                            message=f"Campaign '{campaign}' {'complies with' if ok else 'breaks'} the naming convention."))
        url = inp["base_url"].strip()
        split = urlsplit(url)
        if split.scheme not in ("http", "https") or not split.netloc:
            checks.append(Check(rule="url.valid", status="fail", severity="critical", message=f"'{url}' is not an absolute http(s) URL."))
        if any(k.lower().startswith("utm_") for k, _ in parse_qsl(split.query)):
            checks.append(Check(rule="url.existing_utm", status="warn", severity="low",
                                message="Base URL already had UTM parameters; they were replaced."))
        return {"data": {"campaign": campaign, "taxonomy": tax}, "checks": checks}

    # -- stage 2 --------------------------------------------------------------------------------
    def build_links(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        tax, campaign = data["taxonomy"], data["campaign"]
        checks: list[Check] = []
        channels = list(tax["channels"]) if inp.get("channels") in (["all"], ["*"]) else inp.get("channels") or ["email"]
        rows: list[tuple[str, str | None, str | None]] = []
        if inp.get("mode") == "single":
            rows.append(("custom", inp.get("source"), inp.get("medium")))
        else:
            for ch in channels:
                cfg = tax["channels"].get(ch)
                if not cfg:
                    checks.append(Check(rule="channel.known", status="fail", severity="medium",
                                        message=f"Channel '{ch}' is not in the taxonomy ({sorted(tax['channels'])})."))
                    continue
                rows.append((ch, cfg["source"], cfg["medium"]))
        links: list[UTMLink] = []
        seen: set[str] = set()
        for channel, source, medium in rows:
            source, medium = _clean(source), _clean(medium)
            if not source or not medium:
                checks.append(Check(rule="params.required", status="fail", severity="high",
                                    message=f"{channel}: utm_source and utm_medium are required."))
                continue
            if source not in tax.get("allowed_sources", []):
                checks.append(Check(rule="source.allowed", status="warn", severity="low", target=channel,
                                    message=f"utm_source '{source}' is not in the approved list."))
            if medium not in tax.get("allowed_mediums", []):
                checks.append(Check(rule="medium.allowed", status="warn", severity="medium", target=channel,
                                    message=f"utm_medium '{medium}' is not an approved medium."))
            params = {"utm_source": source, "utm_medium": medium, "utm_campaign": campaign,
                      "utm_content": _clean(inp.get("content")), "utm_term": _clean(inp.get("term")), "utm_id": _clean(inp.get("utm_id"))}
            url = build_url(inp["base_url"], params)
            if url in seen:
                checks.append(Check(rule="links.duplicate", status="warn", severity="low", message=f"Duplicate link for {channel}."))
            seen.add(url)
            links.append(UTMLink(channel=channel, source=source, medium=medium, campaign=campaign,
                                 content=params["utm_content"], term=params["utm_term"], url=url))
        prior_fail = any(c.get("status") == "fail" for c in state.get("checks", []))
        compliant = not prior_fail and not any(c.status == "fail" for c in checks)
        output = UTMOutput(campaign=campaign, links=links, taxonomy_version=str(tax.get("version", "")), compliant=compliant)
        return {"output": output.model_dump(), "checks": checks}

    # -- stage 3 --------------------------------------------------------------------------------
    def export(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        out = state["output"]
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["channel", "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term", "url"])
        for link in out["links"]:
            w.writerow([link["channel"], link["source"], link["medium"], link["campaign"], link.get("content") or "", link.get("term") or "", link["url"]])
        artifacts = [ctx.artifacts.write_text("utm_links.csv", buf.getvalue(), "text/csv", "UTM link matrix")]
        try:
            from openpyxl import Workbook

            wb = Workbook()
            ws = wb.active
            ws.title = "UTM links"
            for row in csv.reader(io.StringIO(buf.getvalue())):
                ws.append(row)
            for col, width in zip("ABCDEFG", (16, 14, 14, 34, 16, 14, 90)):
                ws.column_dimensions[col].width = width
            xbuf = io.BytesIO()
            wb.save(xbuf)
            artifacts.append(ctx.artifacts.write_bytes("utm_links.xlsx", xbuf.getvalue(),
                                                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "UTM links (Excel)"))
        except ImportError:
            pass
        n = len(out["links"])
        summary = f"{n} UTM link{'s' if n != 1 else ''} for campaign '{out['campaign']}'" + ("" if out["compliant"] else " - taxonomy issues found")
        return {"artifacts": artifacts, "summary": summary}
