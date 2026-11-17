"""CRM connector kind: contacts for personalisation, ABM and outbound segmentation.

Interface: ``Crm.contacts(segment=None, limit=500)`` ->
DataFrame[contact_id, email, first_name, last_name, company, job_title, industry, country, lifecycle_stage]

Segments mean the same thing for every provider:
  * ``None``                              -> all contacts (up to ``limit``)
  * ``{"industry": "Insurance", "lifecycle_stage": ["mql", "sql"]}`` -> AND of case-insensitive
    equality / any-of filters on the common columns (pushed down to SOQL / HubSpot search)
  * ``"prospects"``                       -> a named segment: built-ins below or connectors.crm.segments
  * ``"industry=Insurance;country=US,CA"`` -> inline form of the dict
lifecycle_stage is normalised to subscriber | lead | mql | sql | opportunity | customer | evangelist | other.

Providers: ``csv`` (default; sample_data/crm/contacts.csv or any export with these columns),
``salesforce`` (SOQL over REST), ``hubspot`` (CRM v3 contacts search) and ``none``.
Importing this module registers ``crm`` -> ``csv`` as the kind's default (like every other kind's
local default), so ``hub.get("crm")`` works offline without a settings change; add a ``crm:`` block
to config/settings.yaml to choose another provider.
"""

from __future__ import annotations

import re
from typing import Any

import pandas as pd

from . import DEFAULT_PROVIDERS
from ._http import ExternalProvider, client_credentials_token, expect_json
from .base import Connector, ConnectorError, connector

DEFAULT_PROVIDERS.setdefault("crm", "csv")

CRM_COLUMNS = ["contact_id", "email", "first_name", "last_name", "company", "job_title", "industry", "country", "lifecycle_stage"]
STAGES = {"subscriber": "subscriber", "lead": "lead", "mql": "mql", "marketingqualifiedlead": "mql", "sql": "sql",
          "salesqualifiedlead": "sql", "opportunity": "opportunity", "customer": "customer", "evangelist": "evangelist", "other": "other"}
BUILTIN_SEGMENTS: dict[str, dict[str, Any]] = {
    "subscribers": {"lifecycle_stage": "subscriber"}, "leads": {"lifecycle_stage": "lead"}, "mqls": {"lifecycle_stage": "mql"},
    "sqls": {"lifecycle_stage": "sql"}, "opportunities": {"lifecycle_stage": "opportunity"}, "customers": {"lifecycle_stage": "customer"},
    "prospects": {"lifecycle_stage": ["lead", "mql", "sql", "opportunity"]},
}


def normalize_stage(value: Any) -> str:
    raw = "" if value is None or (isinstance(value, float) and pd.isna(value)) else str(value).strip()
    return STAGES.get(re.sub(r"[\s_\-]+", "", raw.lower()), raw.lower())


def parse_segment(segment: Any, named: dict[str, Any] | None = None) -> dict[str, list[str]]:
    """Segment (None | dict | named | 'col=v1,v2;col2=v') -> {column: [values]} on the common columns."""
    if segment in (None, "", {}):
        return {}
    segments = {**BUILTIN_SEGMENTS, **(named or {})}
    if isinstance(segment, str):
        if segment in segments:
            spec: Any = segments[segment]
        elif "=" in segment:
            spec = {}
            for part in re.split(r"[;&]", segment):
                if "=" in part:
                    key, _, vals = part.partition("=")
                    spec[key.strip()] = [v.strip() for v in vals.split(",") if v.strip()]
        else:
            raise ConnectorError(f"Unknown CRM segment '{segment}'. Use one of {sorted(segments)}, define it under "
                                 "connectors.crm.segments, or pass a dict / 'column=value' string.")
    elif isinstance(segment, dict):
        spec = segment
    else:
        raise ConnectorError(f"CRM segment must be None, a dict or a string, not {type(segment).__name__}")
    out: dict[str, list[str]] = {}
    for key, value in dict(spec).items():
        if key not in CRM_COLUMNS:
            raise ConnectorError(f"Unknown CRM segment field '{key}'; use one of {CRM_COLUMNS}")
        values = [str(v) for v in (value if isinstance(value, (list, tuple, set)) else [value])]
        out[key] = [normalize_stage(v) for v in values] if key == "lifecycle_stage" else values
    return out


def _frame(records: list[dict[str, Any]]) -> pd.DataFrame:
    df = pd.DataFrame(records, columns=CRM_COLUMNS).fillna("")
    for col in CRM_COLUMNS:
        df[col] = df[col].astype(str)
    df["lifecycle_stage"] = df["lifecycle_stage"].map(normalize_stage)
    return df.reset_index(drop=True)


class Crm(Connector):
    def contacts(self, segment: Any = None, limit: int = 500) -> pd.DataFrame:
        raise NotImplementedError

    @property
    def segments(self) -> dict[str, Any]:
        return dict(self.config.get("segments") or {})


@connector("crm", "none")
class NoCrm(Crm):
    """No CRM connected - personalisation falls back to generic copy."""

    @property
    def available(self) -> bool:
        return False

    def contacts(self, segment: Any = None, limit: int = 500) -> pd.DataFrame:
        parse_segment(segment, self.segments)  # still validate the request
        return pd.DataFrame(columns=CRM_COLUMNS)


@connector("crm", "csv")
class CsvCrm(Crm):
    """Contacts CSV in the common columns (sample_data/crm/contacts.csv, or any CRM export renamed to them)."""

    def contacts(self, segment: Any = None, limit: int = 500) -> pd.DataFrame:
        df = pd.read_csv(self.path("path", "sample_data/crm/contacts.csv"), dtype=str, keep_default_na=False)
        df.columns = [c.strip().lower() for c in df.columns]
        for col in CRM_COLUMNS:
            if col not in df:
                df[col] = ""
        df = _frame(df[CRM_COLUMNS].to_dict("records"))
        for col, values in parse_segment(segment, self.segments).items():
            df = df[df[col].str.lower().isin({v.lower() for v in values})]
        return df.head(int(limit)).reset_index(drop=True)


# ----------------------------------------------------------------------------- Salesforce

SF_FIELDS = {
    "Contact": {"contact_id": "Id", "email": "Email", "first_name": "FirstName", "last_name": "LastName", "company": "Account.Name",
                "job_title": "Title", "industry": "Account.Industry", "country": "MailingCountry", "lifecycle_stage": None},
    "Lead": {"contact_id": "Id", "email": "Email", "first_name": "FirstName", "last_name": "LastName", "company": "Company",
             "job_title": "Title", "industry": "Industry", "country": "Country", "lifecycle_stage": "Status"},
}


def soql_literal(value: str) -> str:
    escaped = str(value).replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return f"'{escaped}'"


def _dig(record: dict[str, Any], path: str | None) -> Any:
    cur: Any = record
    for part in (path or "").split(".") if path else []:
        cur = cur.get(part) if isinstance(cur, dict) else None
    return cur


@connector("crm", "salesforce")
class SalesforceCrm(ExternalProvider, Crm):
    """Salesforce - SOQL query over the REST API.

    Auth: OAuth 2.0 client credentials - POST {login_url}/services/oauth2/token with grant_type=client_credentials, client_id,
    client_secret (a Connected App / External Client App with the client-credentials flow enabled and
    a run-as user; login_url is the org's My Domain URL, https://<domain>.my.salesforce.com) ->
    {access_token, instance_url}; or configure
    a static access_token + instance_url.
    Query: GET {instance_url}/services/data/v{api_version}/query?q=<SOQL>, following nextRecordsUrl.
    Config (connectors.crm): login_url, client_id, client_secret | access_token + instance_url;
    optional object (Contact | Lead, default Contact), api_version (default 62.0), field_map overrides
    (e.g. {lifecycle_stage: Lifecycle_Stage__c} - a Contact has no standard lifecycle field, so it is
    empty unless mapped), lifecycle_values {mql: ["Marketing Qualified"]} (picklist values per stage),
    where (extra SOQL condition), require_email (default true), segments.
    Segment filters become ``<field> IN ('..')`` clauses with escaped literals (SOQL string comparison
    is case-insensitive).
    """

    external = True
    required = ("login_url", "client_id", "client_secret")

    @property
    def available(self) -> bool:
        cfg = self.config
        return bool((cfg.get("access_token") and cfg.get("instance_url")) or all(cfg.get(k) for k in self.required))

    def _session(self) -> tuple[str, str]:
        cfg = self.config
        if cfg.get("access_token") and cfg.get("instance_url"):
            return str(cfg["access_token"]), str(cfg["instance_url"]).rstrip("/")
        creds = self.require(*self.required)
        token = client_credentials_token(self, f"{str(creds['login_url']).rstrip('/')}/services/oauth2/token",
                                         client_id=creds["client_id"], client_secret=creds["client_secret"], cache="salesforce")
        instance = self.token_cache("salesforce").data.get("instance_url") or creds["login_url"]
        return token, str(instance).rstrip("/")

    def field_map(self) -> dict[str, str | None]:
        obj = str(self.config.get("object") or "Contact")
        return {**SF_FIELDS.get(obj, SF_FIELDS["Contact"]), **(self.config.get("field_map") or {})}

    def soql(self, segment: Any = None, limit: int = 500) -> str:
        fm = self.field_map()
        fields = list(dict.fromkeys(f for f in fm.values() if f))
        where: list[str] = []
        if self.config.get("require_email", True):
            where.append(f"{fm['email']} != null")
        stage_values = self.config.get("lifecycle_values") or {}
        for col, values in parse_segment(segment, self.segments).items():
            if not fm.get(col):
                raise ConnectorError(f"crm:salesforce cannot filter on {col}: map it in connectors.crm.field_map")
            if col == "lifecycle_stage":
                values = [v for s in values for v in stage_values.get(s, [s])]
            where.append(f"{fm[col]} IN ({', '.join(soql_literal(v) for v in values)})")
        if self.config.get("where"):
            where.append(f"({self.config['where']})")
        obj = str(self.config.get("object") or "Contact")
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        return f"SELECT {', '.join(fields)} FROM {obj}{clause} LIMIT {int(limit)}"

    def contacts(self, segment: Any = None, limit: int = 500) -> pd.DataFrame:
        token, instance = self._session()
        version = str(self.config.get("api_version") or "62.0").lstrip("v")
        headers = {"authorization": f"Bearer {token}", "accept": "application/json"}
        data = expect_json(self.http.get(f"{instance}/services/data/v{version}/query", params={"q": self.soql(segment, limit)},
                                         headers=headers), self, "SOQL query")
        records = list(data.get("records") or [])
        while not data.get("done", True) and data.get("nextRecordsUrl") and len(records) < limit:
            data = expect_json(self.http.get(f"{instance}{data['nextRecordsUrl']}", headers=headers), self, "SOQL query (next page)")
            records += data.get("records") or []
        fm = self.field_map()
        return _frame([{col: _dig(r, fm.get(col)) for col in CRM_COLUMNS} for r in records[:limit]])


# ----------------------------------------------------------------------------- HubSpot

HS_PROPERTIES = {"contact_id": "hs_object_id", "email": "email", "first_name": "firstname", "last_name": "lastname", "company": "company",
                 "job_title": "jobtitle", "industry": "industry", "country": "country", "lifecycle_stage": "lifecyclestage"}
HS_STAGE = {"mql": "marketingqualifiedlead", "sql": "salesqualifiedlead"}


@connector("crm", "hubspot")
class HubSpotCrm(ExternalProvider, Crm):
    """HubSpot CRM v3 - contacts search.

    Endpoint: POST https://api.hubapi.com/crm/v3/objects/contacts/search with {filterGroups:
    [{filters: [{propertyName, operator EQ|IN, value|values}]}], properties, limit (<= 200), after,
    sorts}; paged by paging.next.after. Search returns at most 10,000 results per query and is
    rate-limited to 5 requests/s per account.
    Auth: Authorization: Bearer <private app access token> (scope crm.objects.contacts.read).
    Config (connectors.crm): access_token (required); optional property_map overrides, search_path
    (e.g. HubSpot's date-versioned /crm/objects/2026-09/contacts/search), segments, base_url.
    Mapping: hs_object_id (or the record id) -> contact_id, email, firstname, lastname, company,
    jobtitle, industry, country, lifecyclestage (marketingqualifiedlead -> mql, salesqualifiedlead ->
    sql). IN values are lowercased, as HubSpot requires for string properties.
    """

    external = True
    required = ("access_token",)
    base_url = "https://api.hubapi.com"
    retry_post = True  # search is read-only

    def default_headers(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self.config.get('access_token') or ''}", "accept": "application/json"}

    def props(self) -> dict[str, str]:
        return {**HS_PROPERTIES, **(self.config.get("property_map") or {})}

    def filters(self, segment: Any) -> list[dict[str, Any]]:
        props = self.props()
        out = []
        for col, values in parse_segment(segment, self.segments).items():
            if col == "lifecycle_stage":
                values = [HS_STAGE.get(v, v) for v in values]
            if len(values) == 1:
                out.append({"propertyName": props[col], "operator": "EQ", "value": values[0]})
            else:
                out.append({"propertyName": props[col], "operator": "IN", "values": [v.lower() for v in values]})
        return out

    def contacts(self, segment: Any = None, limit: int = 500) -> pd.DataFrame:
        self.require(*self.required)
        props = self.props()
        filters = self.filters(segment)
        path = str(self.config.get("search_path") or "/crm/v3/objects/contacts/search")
        records: list[dict[str, Any]] = []
        after: str | None = None
        while len(records) < limit:
            body: dict[str, Any] = {"properties": list(dict.fromkeys(props.values())), "limit": min(200, int(limit) - len(records)),
                                    "sorts": [{"propertyName": "hs_object_id", "direction": "ASCENDING"}]}
            if filters:
                body["filterGroups"] = [{"filters": filters}]
            if after:
                body["after"] = after
            data = expect_json(self.http.post(path, json=body, headers={"content-type": "application/json"}), self, "contacts search")
            for r in data.get("results") or []:
                p = r.get("properties") or {}
                records.append({col: p.get(prop) if col != "contact_id" else (p.get(prop) or r.get("id")) for col, prop in props.items()})
            after = ((data.get("paging") or {}).get("next") or {}).get("after")
            if not after or not data.get("results"):
                break
        return _frame(records[:limit])
