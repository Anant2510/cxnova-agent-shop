"""External experimentation providers: Optimizely Web Experimentation and Adobe Target (read into the
platform-neutral ExperimentDefinition) and LaunchDarkly (variation delivery as a JSON flag).

Experiments.results() returns the local shape DataFrame[variant, segment, visitors, conversions,
revenue] where ``variant`` is the same id used in ExperimentDefinition.variants[].id, so the
analysis agents join definitions and results identically for every platform.
``upsert()`` never creates or edits tests in a vendor platform: like the json provider it stores a
run-local draft (runs/_experiments/<provider>_drafts.json) for a human to set up.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import pandas as pd

from ..core.textutil import slugify
from ._http import ExternalProvider, adobe_ims_token, check, expect_json
from .base import ConnectorError, connector
from .experiments import Audience, ExperimentDefinition, Experiments, Metric, Variant
from .publishing import RUNTIME_TEMPLATE, FeatureFlags

RESULT_COLUMNS = ["variant", "segment", "visitors", "conversions", "revenue"]
_draft_lock = threading.Lock()


class _ExternalExperiments(ExternalProvider, Experiments):
    external = True

    def upsert(self, definition: dict[str, Any] | ExperimentDefinition) -> ExperimentDefinition:
        """Store a local draft for hand-off; the platform is never modified from here."""
        exp = definition if isinstance(definition, ExperimentDefinition) else ExperimentDefinition.model_validate(definition)
        out = self.settings.runs_dir / "_experiments" / f"{self.provider}_drafts.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        with _draft_lock:
            items = json.loads(out.read_text(encoding="utf-8")).get("experiments", []) if out.exists() else []
            items = [e for e in items if e.get("id") != exp.id] + [exp.model_dump(mode="json")]
            out.write_text(json.dumps({"experiments": items}, indent=2), encoding="utf-8")
        return exp


def _date(value: Any) -> str | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return str(value)[:10]


# ----------------------------------------------------------------------------- Optimizely

OPTIMIZELY_STATUS = {"not_started": "draft", "running": "running", "paused": "paused", "campaign_paused": "paused",
                     "concluded": "completed", "archived": "archived"}


def _optimizely_metric(m: dict[str, Any]) -> Metric:
    agg, field = str(m.get("aggregator") or "unique"), str(m.get("field") or "")
    kind = "revenue" if field == "revenue" else {"unique": "conversion", "count": "count", "sum": "count", "ratio": "rate",
                                                 "bounce": "rate", "exit": "rate"}.get(agg, "conversion")
    ident = str(m.get("event_id") or m.get("id") or f"{agg}-{m.get('scope', 'visitor')}")
    name = m.get("display_title") or m.get("name") or (f"event {m['event_id']}" if m.get("event_id") else ident)
    return Metric(id=ident, name=str(name), type=kind, event=str(m["event_id"]) if m.get("event_id") else None)


@connector("experiments", "optimizely")
class OptimizelyExperiments(_ExternalExperiments):
    """Optimizely Web Experimentation REST API v2 (read).

    Endpoints (https://api.optimizely.com/v2):
      GET /experiments?project_id=<id>&per_page=100&page=<n>   list (JSON array, paged)
      GET /experiments/{experiment_id}                          one experiment
      GET /experiments/{experiment_id}/results[?device=...]     results (200, or 202/204 = not ready)
    Auth: Authorization: Bearer <personal access token>.
    Config (connectors.experiments): api_token, project_id (required); optional results_segments
    (e.g. {device: [desktop, mobile, tablet]}: one results call per value, rows labelled with the
    segment like the local CSVs; default a single "all" segment), revenue_divisor (default 100 -
    Optimizely records revenue in cents).
    Mapping: status not_started->draft, running, paused, concluded->completed, archived; variation
    weight (basis points) -> allocation %; traffic_allocation = 100 - holdback/100; metrics[0] ->
    primary metric (others secondary); url_targeting.edit_url -> page_url; description -> hypothesis.
    results(): visitors = reach.variations[id].count, conversions = primary metric results[id].value,
    revenue = first revenue metric's value / revenue_divisor.
    Unconfirmed: the current reference page documents the results endpoint and its parameters but no
    response schema; field names follow the published v2 results schema (reach, metrics[].results).
    """

    external = True
    required = ("api_token", "project_id")
    base_url = "https://api.optimizely.com/v2"

    def default_headers(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self.config.get('api_token') or ''}", "accept": "application/json"}

    def to_definition(self, e: dict[str, Any]) -> ExperimentDefinition:
        variations = e.get("variations") or []
        variants = []
        for i, v in enumerate(variations):
            name = str(v.get("name") or f"Variation {i + 1}")
            variants.append(Variant(id=str(v.get("variation_id") or v.get("id") or i), name=name, allocation=round(float(v.get("weight") or 0) / 100, 2),
                                    is_control=i == 0 or name.lower() in ("original", "control"), description=str(v.get("description") or "")))
        if sum(v.is_control for v in variants) > 1:  # keep exactly one control: the first
            variants = [v.model_copy(update={"is_control": i == 0}) for i, v in enumerate(variants)]
        metrics = [_optimizely_metric(m) for m in e.get("metrics") or []]
        aud = e.get("audience_conditions")
        targeting = e.get("url_targeting") or {}
        return ExperimentDefinition(
            id=str(e["id"]), name=str(e.get("name") or e["id"]), status=OPTIMIZELY_STATUS.get(str(e.get("status")), "draft"),  # type: ignore[arg-type]
            platform="optimizely", page_url=str(targeting.get("edit_url") or ""), hypothesis=str(e.get("description") or ""),
            primary_metric=metrics[0] if metrics else None, secondary_metrics=metrics[1:], variants=variants,
            traffic_allocation=round(100 - float(e.get("holdback") or 0) / 100, 2),
            audience=Audience(conditions=[] if aud in (None, "", "everyone") else [aud if isinstance(aud, str) else json.dumps(aud)]),
            start_date=_date(e.get("earliest") or e.get("created")), end_date=_date(e.get("latest")) if e.get("status") == "concluded" else None,
            instrumentation={"optimizely": {"project_id": e.get("project_id"), "experiment_id": e.get("id"),
                                            "page_ids": e.get("page_ids") or [], "event_ids": [m.event for m in metrics if m.event]}})

    def list(self, status: str | None = None) -> list[ExperimentDefinition]:
        cfg = self.require(*self.required)
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = expect_json(self.http.get("/experiments", params={"project_id": cfg["project_id"], "per_page": 100, "page": page}),
                                self, "list experiments")
            items += batch or []
            if not batch or len(batch) < 100:
                break
            page += 1
        out = [self.to_definition(e) for e in items]
        return [e for e in out if status is None or e.status == status]

    def get(self, experiment_id: str) -> ExperimentDefinition | None:
        self.require(*self.required)
        resp = self.http.get(f"/experiments/{quote(str(experiment_id), safe='')}")
        if resp.status_code == 404:
            return None
        return self.to_definition(expect_json(resp, self, "get experiment"))

    def _results_rows(self, data: dict[str, Any], segment: str) -> list[dict[str, Any]]:
        reach = (data.get("reach") or {}).get("variations") or {}
        metrics = data.get("metrics") or []
        primary = (metrics[0].get("results") or {}) if metrics else {}
        revenue_metric = next((m for m in metrics if str(m.get("field") or "") == "revenue" or "revenue" in str(m.get("name") or "").lower()), None)
        revenue = (revenue_metric or {}).get("results") or {}
        divisor = float(self.config.get("revenue_divisor") or 100)
        rows = []
        for vid in dict.fromkeys(list(reach) + list(primary)):
            visitors = (reach.get(vid) or {}).get("count")
            if visitors is None:
                visitors = (primary.get(vid) or {}).get("samples", 0)
            rows.append({"variant": str(vid), "segment": segment, "visitors": int(visitors or 0),
                         "conversions": float((primary.get(vid) or {}).get("value") or 0),
                         "revenue": round(float((revenue.get(vid) or {}).get("value") or 0) / divisor, 2)})
        return rows

    def results(self, experiment_id: str) -> pd.DataFrame:
        """Rows partition the traffic like the local results CSVs: one row per variant and segment
        (``results_segments`` values, e.g. device desktop/mobile/tablet - list every value), or a
        single ``all`` segment when no segmentation is configured. Never both, so sums stay correct."""
        self.require(*self.required)
        queries: list[tuple[str, dict[str, Any]]] = []
        for param, values in (self.config.get("results_segments") or {}).items():
            queries += [(str(v), {param: v}) for v in values]
        queries = queries or [("all", {})]
        rows: list[dict[str, Any]] = []
        for segment, params in queries:
            resp = self.http.get(f"/experiments/{quote(str(experiment_id), safe='')}/results", params=params)
            if resp.status_code in (202, 204, 404):
                continue  # not computed yet / no data
            rows += self._results_rows(expect_json(resp, self, "get results"), segment)
        df = pd.DataFrame(rows, columns=RESULT_COLUMNS)
        df["conversions"] = df["conversions"].round().astype("int64") if not df.empty else df["conversions"]
        return df


# ----------------------------------------------------------------------------- Adobe Target

TARGET_STATE = {"approved": "running", "deactivated": "paused", "paused": "paused", "saved": "draft", "deleted": "archived"}
TARGET_SCOPES = "openid,AdobeID,read_organizations,additional_info.projectedProductContext,additional_info.job_function"
V3 = "application/vnd.adobe.target.v3+json"
V1 = "application/vnd.adobe.target.v1+json"


@connector("experiments", "adobe_target")
class AdobeTargetExperiments(_ExternalExperiments):
    """Adobe Target Admin API - A/B and Experience Targeting activities.

    Endpoints (https://mc.adobe.io/{tenant}/target):
      GET /activities?limit=&offset=                    (Accept application/vnd.adobe.target.v3+json)
      GET /activities/ab/{id} | /activities/xt/{id}     (v3)
      GET /activities/{ab|xt}/{id}/report/performance   (v1)
    Auth: Adobe IMS OAuth server-to-server token (client_id/client_secret, or a static access_token) +
    X-Api-Key: client_id.
    Config (connectors.experiments): tenant, client_id, client_secret (required unless access_token is
    set); optional scopes,
    activity_types (default [ab, xt]), max_activities (default 50).
    Mapping: state approved->running (completed once endsAt has passed), deactivated/paused->paused,
    saved->draft, deleted->archived; experiences -> variants (visitorPercentage -> allocation, the
    first experience is the control); metrics[0] (conversion) -> primary metric; startsAt/endsAt ->
    dates; locations.mboxes/selectors -> instrumentation.
    results(): report.statistics.experiences[] -> visitors = visitor.totals.entries, conversions =
    visitor.totals.conversions; revenue is 0 (Target revenue reporting lives in A4T/Analytics).
    """

    external = True
    required = ("tenant", "client_id")  # + client_secret, unless a static access_token is set
    base_url = "https://mc.adobe.io"

    @property
    def available(self) -> bool:
        return super().available and bool(self.config.get("client_secret") or self.config.get("access_token"))

    def api_base(self) -> str:
        return f"{str(self.config.get('base_url') or self.base_url).rstrip('/')}/{self.config.get('tenant') or ''}/target"

    def default_headers(self) -> dict[str, str]:
        return {"x-api-key": str(self.config.get("client_id") or "")}

    def _get(self, path: str, accept: str, params: dict[str, Any] | None = None) -> Any:
        self.require(*self.required)
        token = adobe_ims_token(self, TARGET_SCOPES)
        return self.http.get(path, params=params, headers={"authorization": f"Bearer {token}", "accept": accept})

    def to_definition(self, a: dict[str, Any], kind: str = "ab") -> ExperimentDefinition:
        exps = a.get("experiences") or []
        variants = [Variant(id=str(x.get("experienceLocalId", i)), name=str(x.get("name") or f"Experience {chr(65 + i)}"),
                            allocation=float(x.get("visitorPercentage") or (round(100 / len(exps), 2) if exps else 0)), is_control=i == 0)
                    for i, x in enumerate(exps)]
        metrics = [Metric(id=str(m.get("metricLocalId")), name=str(m.get("name") or m.get("metricLocalId")),
                          type="conversion" if m.get("conversion") else "count",
                          event=",".join(b.get("name", "") for b in m.get("mboxes") or []) or None) for m in a.get("metrics") or []]
        metrics.sort(key=lambda m: m.type != "conversion")
        state = TARGET_STATE.get(str(a.get("state")), "draft")
        ends = a.get("endsAt") or (a.get("lifetime") or {}).get("end")
        if state == "running" and ends and _date(ends) and _date(ends) < datetime.now(timezone.utc).date().isoformat():
            state = "completed"
        locations = a.get("locations") or {}
        return ExperimentDefinition(
            id=str(a["id"]), name=str(a.get("name") or a["id"]), status=state, platform="adobe_target",  # type: ignore[arg-type]
            primary_metric=metrics[0] if metrics else None, secondary_metrics=metrics[1:], variants=variants,
            traffic_allocation=100.0, start_date=_date(a.get("startsAt") or (a.get("lifetime") or {}).get("start")), end_date=_date(ends),
            instrumentation={"adobe_target": {"activity_type": kind, "workspace": a.get("workspace"),
                                              "mboxes": [m.get("name") for m in locations.get("mboxes") or []],
                                              "selectors": [s.get("selector") for s in locations.get("selectors") or []],
                                              "auto_allocate": (a.get("autoAllocateTraffic") or {}).get("enabled", False)}})

    def _summaries(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        offset, limit = 0, 100
        while True:
            data = expect_json(self._get("/activities", V3, {"limit": limit, "offset": offset}), self, "list activities")
            batch = data.get("activities") or []
            items += batch
            offset += len(batch)
            if not batch or offset >= int(data.get("total") or 0):
                break
        return items

    def list(self, status: str | None = None) -> list[ExperimentDefinition]:
        types = [str(t) for t in self.config.get("activity_types") or ["ab", "xt"]]
        out = []
        for s in [a for a in self._summaries() if str(a.get("type")) in types][: int(self.config.get("max_activities") or 50)]:
            defn = self._detail(str(s["id"]), str(s["type"]))
            if defn is not None:
                out.append(defn)
        return [e for e in out if status is None or e.status == status]

    def _detail(self, activity_id: str, kind: str) -> ExperimentDefinition | None:
        resp = self._get(f"/activities/{kind}/{activity_id}", V3)
        if resp.status_code == 404:
            return None
        return self.to_definition(expect_json(resp, self, "get activity"), kind)

    def get(self, experiment_id: str) -> ExperimentDefinition | None:
        for kind in [str(t) for t in self.config.get("activity_types") or ["ab", "xt"]]:
            found = self._detail(experiment_id, kind)
            if found is not None:
                return found
        return None

    def results(self, experiment_id: str) -> pd.DataFrame:
        for kind in [str(t) for t in self.config.get("activity_types") or ["ab", "xt"]]:
            resp = self._get(f"/activities/{quote(kind, safe='')}/{quote(str(experiment_id), safe='')}/report/performance", V1)
            if resp.status_code == 404:
                continue
            data = expect_json(resp, self, "performance report")
            rows = []
            for x in ((data.get("report") or {}).get("statistics") or {}).get("experiences") or []:
                stats = x.get("visitor") or (x.get("totals") or {}).get("visitor") or {}
                tot = stats.get("totals") or stats
                rows.append({"variant": str(x.get("experienceLocalId")), "segment": "all", "visitors": int(tot.get("entries") or 0),
                             "conversions": int(tot.get("conversions") or 0), "revenue": 0.0})
            return pd.DataFrame(rows, columns=RESULT_COLUMNS)
        return pd.DataFrame(columns=RESULT_COLUMNS)


# ----------------------------------------------------------------------------- LaunchDarkly

@connector("flags", "launchdarkly")
class LaunchDarklyFlags(ExternalProvider, FeatureFlags):
    """LaunchDarkly REST API v2 - deliver a variation as a JSON flag variation.

    Endpoints (https://app.launchdarkly.com/api/v2):
      POST  /flags/{project_key}  {name, key, description, variations[{name, value}], temporary, tags,
            clientSideAvailability, defaults{onVariation, offVariation}}  (new flag: control + variation)
      GET   /flags/{project_key}/{flag_key} and PATCH it with JSON Patch to add/replace the variation
            when the flag already exists (409 on create)
      PATCH /flags/{project_key}/{flag_key} [{op: replace, path: /environments/{env}/on, value: true}]
            only when preview=False and ``environment`` is configured.
    Auth: Authorization: <API access token> (LaunchDarkly does not use a Bearer prefix).
    Config (connectors.flags): api_token, project_key (required); optional environment, flag_prefix
    (default caf-exp-), tags. New flags are created with targeting off (serving offVariation) in every
    environment, so preview=True never changes what visitors see.
    Variation value: {experiment_id, variation_id, css, js, selectors, bundle} - ``bundle`` is the same
    vendor-neutral runtime the file provider writes, so a client-side snippet can eval it.
    """

    external = True
    required = ("api_token", "project_key")
    base_url = "https://app.launchdarkly.com/api/v2"

    def default_headers(self) -> dict[str, str]:
        return {"authorization": str(self.config.get("api_token") or ""), "accept": "application/json"}

    def deploy_variation(self, *, experiment_id: str, variation_id: str, css: str = "", js: str = "",
                         selectors: list[str] | None = None, preview: bool = True) -> dict[str, Any]:
        cfg = self.require(*self.required)
        project = cfg["project_key"]
        flag_key = f"{self.config.get('flag_prefix') or 'caf-exp-'}{slugify(experiment_id)}"
        bundle = RUNTIME_TEMPLATE.format(experiment_id=experiment_id, variation_id=variation_id,
                                         variation_id_js=json.dumps(variation_id), css=json.dumps(css or ""), js=js or "")
        value = {"experiment_id": experiment_id, "variation_id": variation_id, "css": css or "", "js": js or "",
                 "selectors": selectors or [], "bundle": bundle}
        control = {"experiment_id": experiment_id, "variation_id": "control", "css": "", "js": "", "selectors": [], "bundle": ""}
        tags = list(self.config.get("tags") or ["caf", "experiment"])
        body = {"name": f"CAF experiment {experiment_id}", "key": flag_key, "description": f"Variation delivery for {experiment_id} (Coforge CAF)",
                "variations": [{"name": "control", "value": control}, {"name": str(variation_id), "value": value}],
                "temporary": True, "tags": tags, "clientSideAvailability": {"usingEnvironmentId": True, "usingMobileKey": False},
                "defaults": {"onVariation": 1, "offVariation": 0}}
        resp = self.http.post(f"/flags/{project}", json=body, headers={"content-type": "application/json"})
        action = "created"
        if resp.status_code == 409:
            flag = expect_json(self.http.get(f"/flags/{project}/{flag_key}"), self, "get flag")
            names = [str(v.get("name")) for v in flag.get("variations") or []]
            if str(variation_id) in names:
                ops = [{"op": "replace", "path": f"/variations/{names.index(str(variation_id))}/value", "value": value}]
                action = "updated"
            else:
                ops = [{"op": "add", "path": "/variations/-", "value": {"name": str(variation_id), "value": value}}]
                action = "variation_added"
            resp = self.http.patch(f"/flags/{project}/{flag_key}", json={"patch": ops, "comment": f"CAF: deploy {variation_id}"},
                                   headers={"content-type": "application/json"})
        check(resp, self, f"{action} flag")
        status = "preview"
        env = self.config.get("environment")
        if not preview:
            if not env:
                raise ConnectorError("flags:launchdarkly: set 'environment' to turn targeting on (preview=False)")
            check(self.http.patch(f"/flags/{project}/{flag_key}", json={"patch": [{"op": "replace", "path": f"/environments/{env}/on", "value": True}],
                                                                         "comment": f"CAF: enable {flag_key}"},
                                  headers={"content-type": "application/json"}), self, "enable targeting")
            status = "deployed"
        return {"bundle": f"launchdarkly:{project}/{flag_key}#{variation_id}", "status": status, "selectors": selectors or [],
                "flag_key": flag_key, "action": action, "api_url": f"{self.api_base()}/flags/{project}/{flag_key}"}
