"""External publishing providers: CMS (AEM, Contentful, WordPress), ESP (Braze, Mailchimp, AJO) and
task emitters (Jira, Azure DevOps).

They implement the same methods and keyword arguments as the local ``file`` / ``json_file``
providers in publishing.py, so a PendingAction proposed by an agent executes unchanged against the
client's platform. All of them are side effects: agents propose them and the approval gate runs them.
Return values keep the local keys (``id``, ``url``/``status`` for CMS, ``id``/``status`` for ESP,
``[{id, title, url}]`` for tasks) plus provider-specific extras.
"""

from __future__ import annotations

import base64
import html as _html
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

from ..core.textutil import slugify, truncate
from ._http import ExternalProvider, adobe_ims_token, basic_auth_header, check, expect_json
from .base import ConnectorError, connector
from .publishing import Cms, Esp, Tasks

PUBLISHED = {"published", "publish", "live", "public", "active"}


def _to_html(body: str, body_format: str) -> str:
    if body_format == "html":
        return body
    if body_format == "markdown":
        import markdown

        return markdown.markdown(body or "", extensions=["extra", "sane_lists"])
    return "".join(f"<p>{_html.escape(p).replace(chr(10), '<br>')}</p>" for p in re.split(r"\n\s*\n", body or "") if p.strip())


# ============================================================================= CMS

AEM_SCOPES = "AdobeID,openid,aem.fragments.management,aem.folders"
CF_MIME = {"markdown": "text/x-markdown", "html": "text/html"}


@connector("cms", "aem")
class AemCms(ExternalProvider, Cms):
    """Adobe Experience Manager as a Cloud Service - create or update a Content Fragment.

    ``api: sites`` (default) uses the Sites OpenAPI for Content Fragments on the author tier
    (base_url = https://author-p<program>-e<environment>.adobeaemcloud.com):
      GET  {base_url}/adobe/sites/cf/fragments?path=<parent>/<name>   (find existing -> id)
      GET  {base_url}/adobe/sites/cf/fragments/{id}                   (ETag for optimistic locking)
      POST {base_url}/adobe/sites/cf/fragments  {title, description, modelId, parentPath, name, fields[]}
      PUT  {base_url}/adobe/sites/cf/fragments/{id}  (If-Match: <etag>) {title, description, fields[]}
      POST {base_url}/adobe/sites/cf/fragments/publish {ids: [...]}    (when status is published)
    ``api: assets`` uses the legacy Assets HTTP API (Adobe has deprecated its Content Fragment
    support but it is still common on older tenants): POST /api/assets/<folder>/<name> with
    {"properties": {"cq:model", "title", "description", "elements": {<field>: {"value"}}}} and PUT on 409.
    Auth: Authorization: Bearer <token> - a static ``access_token`` or an Adobe IMS OAuth
    server-to-server token from client_id/client_secret (scopes default to
    "AdobeID,openid,aem.fragments.management,aem.folders"; x-api-key is sent with client_id).
    Config (connectors.cms): base_url (author URL), parent_path (DAM folder; may contain {locale}),
    model_id (base64url model id) or model_path (/conf/<site>/settings/dam/cfm/models/<model>),
    access_token | client_id + client_secret; optional api, field_map {title, body}, scopes, api_prefix.
    Not confirmed against a live tenant: Sites API field ``type`` values ("text"/"long-text") and
    ``mimeType`` on long-text fields, and the publish body (only ``ids`` is sent).
    """

    external = True
    required = ("base_url", "parent_path")

    @property
    def available(self) -> bool:
        cfg = self.config
        return bool(cfg.get("base_url") and cfg.get("parent_path") and (cfg.get("access_token") or (cfg.get("client_id") and cfg.get("client_secret"))))

    def default_headers(self) -> dict[str, str]:
        return {"accept": "application/json", **({"x-api-key": str(self.config["client_id"])} if self.config.get("client_id") else {})}

    def _auth(self) -> dict[str, str]:
        return {"authorization": f"Bearer {adobe_ims_token(self, AEM_SCOPES)}"}

    @property
    def _fm(self) -> dict[str, str]:
        return {"title": "title", "body": "body", **(self.config.get("field_map") or {})}

    def _model_id(self) -> str:
        if self.config.get("model_id"):
            return str(self.config["model_id"])
        if not self.config.get("model_path"):
            self.require("model_id")
        return base64.urlsafe_b64encode(str(self.config["model_path"]).encode()).rstrip(b"=").decode()

    def publish(self, *, title: str, body: str, slug: str | None = None, content_type: str = "article",
                fields: dict[str, Any] | None = None, status: str = "draft", locale: str = "en",
                body_format: str = "markdown") -> dict[str, Any]:
        self.require(*self.required)
        name = slug or slugify(title)
        parent = str(self.config["parent_path"]).format(locale=locale, content_type=content_type).rstrip("/")
        if str(self.config.get("api") or "sites") == "assets":
            return self._publish_assets(title, body, name, parent, fields or {}, status, body_format)
        return self._publish_sites(title, body, name, parent, fields or {}, status, body_format)

    # --- Sites OpenAPI --------------------------------------------------------------------------
    def _publish_sites(self, title: str, body: str, name: str, parent: str, extra: dict[str, Any], status: str,
                       body_format: str) -> dict[str, Any]:
        prefix = str(self.config.get("api_prefix") or "/adobe/sites").rstrip("/")
        fm = self._fm
        cf_fields: list[dict[str, Any]] = [
            {"name": fm["title"], "type": "text", "values": [title]},
            {"name": fm["body"], "type": "long-text", "mimeType": CF_MIME.get(body_format, "text/plain"), "values": [body]},
        ]
        for key, value in extra.items():
            values = value if isinstance(value, list) else [value]
            first = values[0] if values else ""
            kind = "boolean" if isinstance(first, bool) else "number" if isinstance(first, (int, float)) else "text"
            cf_fields.append({"name": fm.get(key, key), "type": kind, "multiple": isinstance(value, list), "values": values})
        path = f"{parent}/{name}"
        found = expect_json(self.http.get(f"{prefix}/cf/fragments", params={"path": path, "projection": "summary"}, headers=self._auth()),
                            self, "find content fragment")
        existing = next((i for i in found.get("items") or [] if i.get("path") == path), None)
        description = truncate(re.sub(r"\s+", " ", body), 160) if body else ""
        if existing:
            fid = existing["id"]
            current = check(self.http.get(f"{prefix}/cf/fragments/{fid}", headers=self._auth()), self, "read content fragment")
            etag = current.headers.get("etag") or existing.get("etag") or "*"
            resp = self.http.put(f"{prefix}/cf/fragments/{fid}", json={"title": title, "description": description, "fields": cf_fields},
                                 headers={**self._auth(), "if-match": etag, "content-type": "application/json"})
            data, action = expect_json(resp, self, "update content fragment"), "updated"
        else:
            payload = {"title": title, "description": description, "modelId": self._model_id(), "parentPath": parent, "name": name,
                       "fields": cf_fields}
            resp = self.http.post(f"{prefix}/cf/fragments", json=payload, headers={**self._auth(), "content-type": "application/json"})
            data, action = expect_json(resp, self, "create content fragment"), "created"
        fid = str(data.get("id") or (existing or {}).get("id") or "")
        published = status.lower() in PUBLISHED
        if published and fid:
            check(self.http.post(f"{prefix}/cf/fragments/publish", json={"ids": [fid]},
                                 headers={**self._auth(), "content-type": "application/json"}), self, "publish content fragment")
        return {"id": fid, "url": f"{self.api_base()}/assetdetails.html{data.get('path') or path}",
                "status": "published" if published else "draft", "path": data.get("path") or path, "action": action}

    # --- legacy Assets HTTP API -------------------------------------------------------------------
    def _publish_assets(self, title: str, body: str, name: str, parent: str, extra: dict[str, Any], status: str,
                        body_format: str) -> dict[str, Any]:
        fm = self._fm
        folder = parent.removeprefix("/content/dam")
        url = f"/api/assets{quote(folder)}/{quote(name)}"
        elements: dict[str, Any] = {fm["title"]: {"value": title}, fm["body"]: {"value": body, ":type": CF_MIME.get(body_format, "text/plain")}}
        elements.update({fm.get(k, k): {"value": v} for k, v in extra.items()})
        props: dict[str, Any] = {"title": title, "elements": elements}
        headers = {**self._auth(), "content-type": "application/json"}
        if not self.config.get("model_path"):
            self.require("model_path")
        resp = self.http.post(url, json={"properties": {**props, "cq:model": self.config["model_path"]}}, headers=headers)
        action = "created"
        if resp.status_code == 409:
            resp, action = self.http.put(url, json={"properties": props}, headers=headers), "updated"
        check(resp, self, "update content fragment" if action == "updated" else "create content fragment")
        result = {"id": f"/content/dam{folder}/{name}", "url": f"{self.api_base()}/assetdetails.html/content/dam{folder}/{name}",
                  "status": "draft", "path": f"/content/dam{folder}/{name}", "action": action}
        if status.lower() in PUBLISHED:
            result["warning"] = "Assets HTTP API cannot publish; activate the fragment in AEM (or use api: sites)"
        return result


@connector("cms", "contentful")
class ContentfulCms(ExternalProvider, Cms):
    """Contentful Content Management API (CMA) - create/update an entry, optionally publish it.

    Endpoints (https://api.contentful.com):
      GET /spaces/{space_id}/environments/{environment}/entries/{entry_id}       (existing? version + other locales)
      PUT /spaces/{space_id}/environments/{environment}/entries/{entry_id}       create (X-Contentful-Content-Type)
                                                                                 or update (X-Contentful-Version)
      PUT /spaces/{space_id}/environments/{environment}/entries/{entry_id}/published  (X-Contentful-Version)
    Auth: Authorization: Bearer <CMA personal access / management token>;
    Content-Type: application/vnd.contentful.management.v1+json.
    Config (connectors.cms): space_id, management_token (required); optional environment (master),
    content_type_map {article: blogPost}, field_map {title, slug, body}, locale_map {en: en-US},
    default_locale (en-US), base_url. The entry id is derived from content type + slug so publishing
    again updates the same entry; values of other locales are preserved. The body is written as a
    (Markdown/HTML) Long text value - Rich Text fields need a different mapping.
    """

    external = True
    required = ("space_id", "management_token")
    base_url = "https://api.contentful.com"
    CT = "application/vnd.contentful.management.v1+json"

    def default_headers(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self.config.get('management_token') or ''}"}

    def _locale(self, locale: str) -> str:
        mapped = (self.config.get("locale_map") or {}).get(locale)
        if mapped:
            return str(mapped)
        default = str(self.config.get("default_locale") or "en-US")
        return default if locale in (default, default.split("-")[0]) else locale

    def publish(self, *, title: str, body: str, slug: str | None = None, content_type: str = "article",
                fields: dict[str, Any] | None = None, status: str = "draft", locale: str = "en",
                body_format: str = "markdown") -> dict[str, Any]:
        cfg = self.require(*self.required)
        slug = slug or slugify(title)
        ct_id = str((self.config.get("content_type_map") or {}).get(content_type) or content_type)
        fm = {"title": "title", "slug": "slug", "body": "body", **(self.config.get("field_map") or {})}
        loc = self._locale(locale)
        entry_id = re.sub(r"[^A-Za-z0-9\-_.]", "-", f"{ct_id}-{slug}")[:64]
        path = f"/spaces/{cfg['space_id']}/environments/{self.config.get('environment') or 'master'}/entries/{entry_id}"
        current = self.http.get(path)
        values = {fm["title"]: title, fm["slug"]: slug, fm["body"]: body, **{fm.get(k, k): v for k, v in (fields or {}).items()}}
        if current.status_code == 404:
            entry_fields = {k: {loc: v} for k, v in values.items()}
            resp = self.http.put(path, json={"fields": entry_fields}, headers={"content-type": self.CT, "x-contentful-content-type": ct_id})
            action = "created"
        else:
            existing = expect_json(current, self, "read entry")
            entry_fields = dict(existing.get("fields") or {})
            for k, v in values.items():
                entry_fields[k] = {**(entry_fields.get(k) or {}), loc: v}
            resp = self.http.put(path, json={"fields": entry_fields},
                                 headers={"content-type": self.CT, "x-contentful-version": str(existing["sys"]["version"])})
            action = "updated"
        entry = expect_json(resp, self, f"{'create' if action == 'created' else 'update'} entry")
        published = status.lower() in PUBLISHED
        if published:
            check(self.http.put(f"{path}/published", headers={"x-contentful-version": str(entry["sys"]["version"])}), self, "publish entry")
        space, env = cfg["space_id"], self.config.get("environment") or "master"
        return {"id": entry["sys"]["id"], "url": f"https://app.contentful.com/spaces/{space}/environments/{env}/entries/{entry['sys']['id']}",
                "status": "published" if published else "draft", "version": entry["sys"].get("version"), "action": action, "locale": loc}


@connector("cms", "wordpress")
class WordPressCms(ExternalProvider, Cms):
    """WordPress REST API (core wp/v2) - create or update a post/page by slug.

    Endpoints: GET {base_url}/wp-json/wp/v2/{posts|pages}?slug=<slug>&status=<all> (find existing),
    POST {base_url}/wp-json/wp/v2/{posts|pages} (create) or POST .../{posts|pages}/{id} (update).
    Auth: HTTP Basic with a WordPress Application Password (username + application_password).
    Config (connectors.cms): base_url (site URL, e.g. https://blog.example.com), username, application_password (required);
    optional post_type_map {article: posts, page: pages}, default_locale (en).
    Body: Markdown is rendered to HTML (python-markdown). Status maps draft|published|review|private|
    scheduled to draft|publish|pending|private|future. WordPress core has no locale field: a
    non-default locale is suffixed to the slug; plugin fields (e.g. Polylang ``lang``, categories,
    excerpt) can be passed through ``fields``.
    """

    external = True
    required = ("base_url", "username", "application_password")
    STATUS = {"draft": "draft", "published": "publish", "publish": "publish", "live": "publish", "review": "pending",
              "pending": "pending", "private": "private", "scheduled": "future", "future": "future"}

    def api_base(self) -> str:
        return str(self.config.get("base_url") or "").rstrip("/") + "/wp-json/wp/v2"

    def default_headers(self) -> dict[str, str]:
        if self.config.get("username") and self.config.get("application_password"):
            pw = str(self.config["application_password"]).replace(" ", "")
            return {"authorization": basic_auth_header(str(self.config["username"]), pw), "accept": "application/json"}
        return {}

    def publish(self, *, title: str, body: str, slug: str | None = None, content_type: str = "article",
                fields: dict[str, Any] | None = None, status: str = "draft", locale: str = "en",
                body_format: str = "markdown") -> dict[str, Any]:
        self.require(*self.required)
        collection = str((self.config.get("post_type_map") or {"page": "pages"}).get(content_type) or "posts")
        slug = slug or slugify(title)
        if locale != str(self.config.get("default_locale") or "en"):
            slug = f"{slug}-{slugify(locale)}"
        wp_status = self.STATUS.get(status.lower(), "draft")
        payload = {"title": title, "content": _to_html(body, body_format), "status": wp_status, "slug": slug, **(fields or {})}
        found = expect_json(self.http.get(f"/{collection}", params={"slug": slug, "status": "publish,future,draft,pending,private",
                                                                    "context": "edit"}), self, "find post")
        if isinstance(found, list) and found:
            post, action = expect_json(self.http.post(f"/{collection}/{found[0]['id']}", json=payload), self, "update post"), "updated"
        else:
            post, action = expect_json(self.http.post(f"/{collection}", json=payload), self, "create post"), "created"
        return {"id": str(post.get("id")), "url": post.get("link") or "", "status": post.get("status") or wp_status, "action": action}


# ============================================================================= ESP

@connector("esp", "braze")
class BrazeEsp(ExternalProvider, Esp):
    """Braze REST API - email template (default) or Content Block.

    Endpoints (on the instance's REST endpoint, e.g. https://rest.iad-01.braze.com):
      POST /templates/email/create {template_name, subject, body, plaintext_body, preheader, tags}
           -> {email_template_id, message}
      POST /content_blocks/create {name, description, content, state, tags} -> {content_block_id, liquid_tag}
    Auth: Authorization: Bearer <REST API key> with templates.email.create / content_blocks.create.
    Config (connectors.esp): api_key, rest_endpoint (required); optional mode (email_template |
    content_block), tags (must already exist in Braze), content_block_state (active | draft).
    """

    external = True
    required = ("api_key", "rest_endpoint")
    retry_post = False

    def api_base(self) -> str:
        return str(self.config.get("rest_endpoint") or self.config.get("base_url") or "").rstrip("/")

    def default_headers(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self.config.get('api_key') or ''}", "accept": "application/json"}

    def export(self, *, name: str, subject: str, preheader: str, html: str, text: str, mjml: str | None = None,
               audience: str | None = None, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        self.require(*self.required)
        tags = list(self.config.get("tags") or [])
        if str(self.config.get("mode") or "email_template") == "content_block":
            body = {"name": re.sub(r"[^A-Za-z0-9_-]", "_", name)[:100], "description": f"{subject} | {preheader}"[:250],
                    "content": html, "state": self.config.get("content_block_state") or "active", **({"tags": tags} if tags else {})}
            data = expect_json(self.http.post("/content_blocks/create", json=body), self, "create content block")
            return {"id": data.get("content_block_id"), "status": "exported", "provider": "braze", "type": "content_block",
                    "liquid_tag": data.get("liquid_tag")}
        body = {"template_name": name, "subject": subject, "body": html, "plaintext_body": text, "preheader": preheader,
                **({"tags": tags} if tags else {})}
        data = expect_json(self.http.post("/templates/email/create", json=body), self, "create email template")
        if data.get("message") not in (None, "success"):
            raise ConnectorError(f"esp:braze create email template: {data.get('message')} {data.get('errors') or ''}".strip())
        return {"id": data.get("email_template_id"), "status": "exported", "provider": "braze", "type": "email_template"}


@connector("esp", "mailchimp")
class MailchimpEsp(ExternalProvider, Esp):
    """Mailchimp Marketing API v3 - user template, plus an optional draft campaign.

    Endpoints (https://{dc}.api.mailchimp.com/3.0):
      POST /templates {name (<= 50 chars), html[, folder_id]} -> {id}
      when a list is configured: POST /campaigns {type: regular, recipients{list_id}, settings{subject_line,
      preview_text, title, from_name, reply_to}} then PUT /campaigns/{id}/content {html, plain_text}.
      The campaign is only saved as a draft - nothing is scheduled or sent.
    Auth: HTTP Basic (any username, API key as password); the data center comes from the key suffix
    (``...-us21``) or ``server_prefix``.
    Config (connectors.esp): api_key (required); optional server_prefix, folder_id, list_id or
    audience_map {audience name: list_id}, from_name, reply_to.
    """

    external = True
    required = ("api_key",)

    def api_base(self) -> str:
        if self.config.get("base_url"):
            return str(self.config["base_url"]).rstrip("/")
        dc = self.config.get("server_prefix") or str(self.config.get("api_key") or "").rpartition("-")[2]
        if not dc:
            raise ConnectorError("esp:mailchimp: set server_prefix (e.g. us21) or use an API key ending in -<dc>")
        return f"https://{dc}.api.mailchimp.com/3.0"

    def default_headers(self) -> dict[str, str]:
        return {"authorization": basic_auth_header("caf", str(self.config.get("api_key") or "")), "accept": "application/json"}

    def export(self, *, name: str, subject: str, preheader: str, html: str, text: str, mjml: str | None = None,
               audience: str | None = None, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        self.require(*self.required)
        tpl: dict[str, Any] = {"name": name[:50], "html": html}
        if self.config.get("folder_id"):
            tpl["folder_id"] = self.config["folder_id"]
        template = expect_json(self.http.post("/templates", json=tpl), self, "create template")
        out: dict[str, Any] = {"id": str(template.get("id")), "status": "exported", "provider": "mailchimp", "template_id": template.get("id")}
        list_id = (self.config.get("audience_map") or {}).get(audience or "") or self.config.get("list_id")
        if list_id:
            settings = {"subject_line": subject, "preview_text": preheader, "title": name,
                        **({"from_name": self.config["from_name"]} if self.config.get("from_name") else {}),
                        **({"reply_to": self.config["reply_to"]} if self.config.get("reply_to") else {})}
            camp = expect_json(self.http.post("/campaigns", json={"type": "regular", "recipients": {"list_id": list_id}, "settings": settings}),
                               self, "create draft campaign")
            check(self.http.put(f"/campaigns/{camp['id']}/content", json={"html": html, "plain_text": text}), self, "set campaign content")
            out.update(campaign_id=camp["id"], status="draft_campaign", url=camp.get("archive_url"))
        return out


AJO_SCOPES = "openid,AdobeID,read_organizations,additional_info.projectedProductContext,session"


@connector("esp", "ajo")
class AjoEsp(ExternalProvider, Esp):
    """Adobe Journey Optimizer - email content template via the Content Templates API.

    Endpoint: POST https://platform.adobe.io/ajo/content/templates
      headers: Authorization: Bearer <IMS token>, x-api-key, x-gw-ims-org-id, x-sandbox-name,
      Content-Type: application/vnd.adobe.ajo.template.v1+json. No Accept header on create (Adobe KB
      ka-26320: sending one returns 406 Not Acceptable).
      body: {name, description, templateType: html, channels: [email], source: {origin: ajo, metadata},
      template: {html, editorContext: {}}}
    Auth: Adobe IMS OAuth server-to-server (client_credentials) from client_id/client_secret, or
    a static access_token.
    Config (connectors.esp): client_id, org_id (IMS org id ...@AdobeOrg), client_secret (required unless
    access_token is set);
    optional sandbox (default prod), scopes, base_url.
    Unconfirmed: the create response body; the template id is read from JSON ``id`` or the
    Location header. Subject and preheader are message-level settings in AJO, so they are recorded
    in the template description (and the HTML already carries the preheader).
    """

    external = True
    required = ("client_id", "org_id")  # + client_secret, unless a static access_token is set
    base_url = "https://platform.adobe.io"
    drop_headers = ("accept",)  # httpx would send Accept: */* by default

    @property
    def available(self) -> bool:
        return super().available and bool(self.config.get("client_secret") or self.config.get("access_token"))

    def default_headers(self) -> dict[str, str]:
        return {"x-api-key": str(self.config.get("client_id") or ""), "x-gw-ims-org-id": str(self.config.get("org_id") or ""),
                "x-sandbox-name": str(self.config.get("sandbox") or "prod")}

    def export(self, *, name: str, subject: str, preheader: str, html: str, text: str, mjml: str | None = None,
               audience: str | None = None, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        self.require(*self.required)
        body = {"name": name, "description": truncate(f"Subject: {subject} | Preheader: {preheader}", 250), "templateType": "html",
                "channels": ["email"], "source": {"origin": "ajo", "metadata": {}}, "template": {"html": html, "editorContext": {}}}
        resp = self.http.post("/ajo/content/templates", json=body,
                              headers={"authorization": f"Bearer {adobe_ims_token(self, AJO_SCOPES)}",
                                       "content-type": "application/vnd.adobe.ajo.template.v1+json"})
        data = expect_json(resp, self, "create content template")
        template_id = (data.get("id") if isinstance(data, dict) else None) or resp.headers.get("location", "").rstrip("/").rpartition("/")[2]
        return {"id": template_id, "status": "exported", "provider": "ajo", "sandbox": self.config.get("sandbox") or "prod",
                "etag": resp.headers.get("etag")}


# ============================================================================= Tasks

def _due(task: dict[str, Any]) -> str | None:
    if task.get("due_date") or task.get("due"):
        return str(task.get("due_date") or task.get("due"))[:10]
    if task.get("due_offset_days") is not None:
        return (datetime.now(timezone.utc).date() + timedelta(days=int(task["due_offset_days"]))).isoformat()
    return None


_META_KEYS = ("type", "channel", "owner_role", "owner", "priority")


def _details(task: dict[str, Any]) -> list[str]:
    return [f"{k.replace('_', ' ').capitalize()}: {task[k]}" for k in _META_KEYS if task.get(k)]


def adf(text: str, bullets: list[str] | None = None) -> dict[str, Any]:
    """Plain text -> Atlassian Document Format (paragraphs, hard breaks, '- ' bullet lists)."""

    def para(lines: list[str]) -> dict[str, Any]:
        content: list[dict[str, Any]] = []
        for i, line in enumerate(lines):
            if i:
                content.append({"type": "hardBreak"})
            if line:
                content.append({"type": "text", "text": line})
        return {"type": "paragraph", "content": content}

    def bullet_list(items: list[str]) -> dict[str, Any]:
        return {"type": "bulletList", "content": [{"type": "listItem", "content": [para([i])]} for i in items if i]}

    blocks: list[dict[str, Any]] = []
    for block in re.split(r"\n\s*\n", (text or "").strip()):
        lines = [ln.rstrip() for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        if all(re.match(r"^\s*[-*•]\s+", ln) for ln in lines):
            blocks.append(bullet_list([re.sub(r"^\s*[-*•]\s+", "", ln) for ln in lines]))
        else:
            blocks.append(para(lines))
    if bullets:
        blocks.append(bullet_list(bullets))
    return {"type": "doc", "version": 1, "content": blocks or [{"type": "paragraph", "content": []}]}


@connector("tasks", "jira")
class JiraTasks(ExternalProvider, Tasks):
    """Jira Cloud REST API v3 - one issue per task.

    Endpoint: POST {base_url}/rest/api/3/issue with {"fields": {project: {key}, summary, issuetype:
    {name}, description (Atlassian Document Format), labels, duedate}} -> {id, key, self}.
    Auth: HTTP Basic email + API token (Jira Cloud); ``auth: bearer`` sends the api_token as a
    Bearer PAT (Jira Data Center).
    Config (connectors.tasks): base_url (https://<site>.atlassian.net), email, api_token (required);
    optional project_key (else the create(project=) argument), issue_type (Task), labels,
    extra_fields (merged into every issue's fields, e.g. components or a custom field).
    Task mapping: title -> summary, description + type/channel/owner -> ADF description, type and
    channel -> labels (slugified), due_offset_days/due_date -> duedate.
    """

    external = True
    required = ("base_url", "email", "api_token")

    @property
    def available(self) -> bool:
        if str(self.config.get("auth") or "basic") == "bearer":
            return bool(self.config.get("base_url") and self.config.get("api_token"))
        return super().available

    def api_base(self) -> str:
        return str(self.config.get("base_url") or "").rstrip("/")

    def default_headers(self) -> dict[str, str]:
        cfg = self.config
        if str(cfg.get("auth") or "basic") == "bearer":
            return {"authorization": f"Bearer {cfg.get('api_token') or ''}", "accept": "application/json"}
        if cfg.get("email") and cfg.get("api_token"):
            return {"authorization": basic_auth_header(str(cfg["email"]), str(cfg["api_token"])), "accept": "application/json"}
        return {}

    def create(self, tasks: list[dict[str, Any]], *, project: str | None = None) -> list[dict[str, Any]]:
        self.require(*(("base_url", "api_token") if str(self.config.get("auth") or "basic") == "bearer" else self.required))
        key = self.config.get("project_key") or project
        if not key:
            self.require("project_key")
        created = []
        for t in tasks:
            title = str(t.get("title") or "Untitled task")
            labels = list(self.config.get("labels") or ["caf"]) + [slugify(str(t[k])) for k in ("type", "channel") if t.get(k)]
            fields: dict[str, Any] = {"project": {"key": key}, "summary": truncate(title, 250),
                                      "issuetype": {"name": self.config.get("issue_type") or "Task"},
                                      "description": adf(str(t.get("description") or ""), _details(t)),
                                      "labels": sorted(set(labels)), **(self.config.get("extra_fields") or {})}
            if _due(t):
                fields["duedate"] = _due(t)
            data = expect_json(self.http.post("/rest/api/3/issue", json={"fields": fields}, headers={"content-type": "application/json"}),
                               self, f"create issue '{truncate(title, 40)}'")
            created.append({"id": data["key"], "title": title, "url": f"{self.api_base()}/browse/{data['key']}", "issue_id": data.get("id")})
        return created


@connector("tasks", "azure_devops")
class AzureDevOpsTasks(ExternalProvider, Tasks):
    """Azure DevOps Boards REST API 7.1 - one work item per task (JSON Patch).

    Endpoint: POST https://dev.azure.com/{organization}/{project}/_apis/wit/workitems/${type}?api-version=7.1
    with Content-Type: application/json-patch+json and [{op: add, path: /fields/System.Title, value}, ...].
    Auth: HTTP Basic with an empty username and a Personal Access Token (scope vso.work_write).
    Config (connectors.tasks): organization, pat (required); project (else the create(project=)
    argument), work_item_type (Task), area_path, iteration_path, tags, due_date_field (e.g.
    Microsoft.VSTS.Scheduling.DueDate - only set when configured, as process templates differ),
    base_url (Azure DevOps Server collection URL).
    Task mapping: title -> System.Title, description + details -> System.Description (HTML),
    type/channel -> System.Tags.
    """

    external = True
    required = ("organization", "pat")

    def api_base(self) -> str:
        return str(self.config.get("base_url") or f"https://dev.azure.com/{self.config.get('organization') or ''}").rstrip("/")

    def default_headers(self) -> dict[str, str]:
        return {"authorization": basic_auth_header("", str(self.config.get("pat") or "")), "accept": "application/json"}

    def create(self, tasks: list[dict[str, Any]], *, project: str | None = None) -> list[dict[str, Any]]:
        self.require(*self.required)
        proj = self.config.get("project") or project
        if not proj:
            self.require("project")
        wit = str(self.config.get("work_item_type") or "Task")
        created = []
        for t in tasks:
            title = str(t.get("title") or "Untitled task")
            desc = _to_html(str(t.get("description") or ""), "text")
            if _details(t):
                desc += "<ul>" + "".join(f"<li>{_html.escape(d)}</li>" for d in _details(t)) + "</ul>"
            tags = list(self.config.get("tags") or ["caf"]) + [str(t[k]) for k in ("type", "channel") if t.get(k)]
            ops = [{"op": "add", "path": "/fields/System.Title", "value": truncate(title, 250)},
                   {"op": "add", "path": "/fields/System.Description", "value": desc},
                   {"op": "add", "path": "/fields/System.Tags", "value": "; ".join(dict.fromkeys(tags))}]
            for cfg_key, field in (("area_path", "System.AreaPath"), ("iteration_path", "System.IterationPath")):
                if self.config.get(cfg_key):
                    ops.append({"op": "add", "path": f"/fields/{field}", "value": self.config[cfg_key]})
            if self.config.get("due_date_field") and _due(t):
                ops.append({"op": "add", "path": f"/fields/{self.config['due_date_field']}", "value": _due(t)})
            url = f"/{quote(str(proj))}/_apis/wit/workitems/{quote('$' + wit, safe='$')}"
            data = expect_json(self.http.post(url, params={"api-version": "7.1"}, json=ops,
                                              headers={"content-type": "application/json-patch+json"}), self, f"create work item '{truncate(title, 40)}'")
            href = ((data.get("_links") or {}).get("html") or {}).get("href") or f"{self.api_base()}/{quote(str(proj))}/_workitems/edit/{data['id']}"
            created.append({"id": str(data["id"]), "title": title, "url": href})
        return created
