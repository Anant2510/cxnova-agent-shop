"""The DocuInsights web application: the workbench (a single page) and the API behind it.

    standalone   python -m docuinsights serve            http://127.0.0.1:8090/
    in a host    create_app(service, tenant_of=..., actor_of=..., auth="host") mounted under the host's own app and auth

    /                         the workbench
    /api/...                  what the workbench calls
    /api/knowledge...         the workspace's knowledge base: overview, the level map (opened on any one node), graph, one entity with the way down
                              to it and where it is used, search, import, export (.zip, KB Forge layout), knowledge bases on and off, facts by hand
    /api/cases/{ref}/knowledge   the case seen from the graph: each finding's node, the rule it applied and what that rule rests on
    /api/platform             the foundation as it stands: contracts, memory, provenance ledger, throttling
    /api/v1/cases             the integration surface: another system (a NewgenONE process, an ESB, a script) submits a
                              case package and reads the assessment back as JSON

Standalone security: set DOCUINSIGHTS_API_KEY and every /api call must carry it (Authorization: Bearer <key> or
X-API-Key). Without a key the API answers local callers only (127.0.0.1), and a request that came through a reverse
proxy is not local even when the proxy runs on the same machine: an open install is never exposed by accident.
A host that mounts the app does its own authentication and tells the app who is calling. /health is public and says
only that the server is up.

Standalone throttling: each caller (its key, or its address) may make DOCUINSIGHTS_API_RPM requests a minute (600);
past that the API answers 429 with Retry-After. An address that sends DOCUINSIGHTS_KEY_ATTEMPTS wrong keys (10) inside
ten minutes is refused until they age out. A host that mounts the app applies its own limits.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import mimetypes
import os
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from . import PRODUCT, __version__
from .assurance import due_state
from .profiles import ProfileError
from .service import Service, ServiceError
from .store import StoreError, safe

log = logging.getLogger("docuinsights")
WEB = Path(__file__).resolve().parent / "web"
LOOPBACK = ("127.0.0.1", "::1", "localhost", "testclient")
PROXIED = ("x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "forwarded", "x-real-ip", "x-original-url", "x-arr-log-id", "via")   # headers a reverse proxy adds
MAX_BODY = 60 * 1024 * 1024


def view(case: dict[str, Any]) -> dict[str, Any]:
    """The case as the workbench gets it: without the working state and the bundled transcriptions."""
    out = {k: v for k, v in case.items() if k not in ("working", "aliases")}
    out["due"] = due_state((case.get("assessment") or {}).get("triage")) if not case.get("decision") else {}
    out["documents"] = [{**d, "versions": [{k: v for k, v in ver.items() if k != "sidecar"} for ver in d["versions"]]} for d in case["documents"]]
    w = case.get("working")
    if w:   # an assessment in flight: what each finished stage reported, for the orchestration monitor
        out["live"] = {"stages": {k: v.get("summary", "") for k, v in (w.get("stages") or {}).items()}, "runs": w.get("runs") or []}
    return out


def result(case: dict[str, Any]) -> dict[str, Any]:
    """The assessment as another system needs it: the outcome, why, and what each document turned out to be."""
    a = case.get("assessment") or {}
    rec = a.get("recommendation") or {}
    return {"reference": case["reference"], "profile": case["profile"], "status": case.get("status"), "progress": case.get("progress"),
            "assessment": a.get("id"), "assessed_at": a.get("at"), "outcome": rec.get("outcome"), "label": rec.get("label"), "reasons": rec.get("reasons") or [],
            "requests": rec.get("requests") or [], "caveats": rec.get("caveats") or [], "summary": a.get("summary"),
            "documents": [{"id": d["id"], "name": d["name"], "version": d["current"], "uploaded_as": d.get("category"), "type": d.get("effective_type") or (d.get("classification") or {}).get("type"),
                           "confidence": (d.get("classification") or {}).get("confidence"), "fields": {k: f.get("value") for k, f in (d.get("fields") or {}).items()}}
                          for d in case["documents"] if not d.get("removed")],
            "findings": [{**{k: f.get(k) for k in ("id", "family", "title", "status", "severity", "message", "requires_human")},
                          "basis": [{k: b.get(k) for k in ("id", "name", "of", "citation")} for b in f.get("basis") or []]} for f in a.get("findings") or []],
            "triage": a.get("triage"), "screening": {k: (a.get("screening") or {}).get(k) for k in ("lists", "hits")} if a.get("screening") else None,
            "personal_data": [{k: c.get(k) for k in ("id", "name", "count")} for c in (a.get("privacy") or {}).get("categories") or []],
            "assigned": case.get("assigned"), "decision": case.get("decision"), "filed": case.get("filed")}


def create_app(service: Service | None = None, *, tenant_of: Callable[[Request], str] | None = None, actor_of: Callable[[Request], str] | None = None,
               auth: str = "key", links: dict[str, str] | None = None) -> Starlette:
    svc = service or Service()
    api_key = os.environ.get("DOCUINSIGHTS_API_KEY") or ""
    tenant = tenant_of or (lambda r: "default")
    actor = actor_of or (lambda r: " ".join(unquote(r.headers.get("x-docuinsights-user", "")).split())[:60] or "caseworker")   # a label for the audit trail, not a sign-in

    def base(request: Request) -> str:
        return (request.scope.get("root_path") or "").rstrip("/")

    def guard(request: Request) -> None:
        if auth != "key":
            return   # the host authenticated the request before it reached this app
        supplied = request.headers.get("x-api-key", "")
        if request.headers.get("authorization", "").lower().startswith("bearer "):
            supplied = request.headers["authorization"][7:].strip()
        if api_key:
            address = request.client.host if request.client else "?"
            wait = svc.key_attempts.wait(address)
            if wait:   # too many wrong keys from this address: refused whatever it sends now, so a short key cannot be found by trying
                raise ServiceError(f"Too many wrong keys from this address. Try again in {int(wait // 60) + 1} minute(s).", 429)
            if not (supplied and hmac.compare_digest(supplied.encode(), api_key.encode())):
                if supplied:
                    svc.key_attempts.wrong(address)
                raise ServiceError("That key was not accepted." if supplied else "Send the API key as 'Authorization: Bearer <key>'.", 401)
        elif (request.client.host if request.client else "") not in LOOPBACK or any(h in request.headers for h in PROXIED):
            # behind a proxy on the same machine every caller looks local: what the proxy adds says otherwise
            raise ServiceError("This installation has no API key, so it answers local callers only. Set DOCUINSIGHTS_API_KEY on the server.", 401)

    def api(fn: Callable[..., Any], *, body: bool = False, status: int = 200) -> Callable[[Request], Any]:
        async def endpoint(request: Request) -> Response:
            try:
                guard(request)
                if auth == "key":   # one gate per caller: its key when it sent one, else its address
                    who = request.headers.get("authorization") or request.headers.get("x-api-key") or (request.client.host if request.client else "?")
                    wait = svc.gate.allow(hashlib.sha256(who.encode()).hexdigest()[:16])
                    if wait:
                        return JSONResponse({"detail": f"Too many requests: this caller may make {int(svc.gate.rpm)} a minute. Try again in {int(wait)} seconds."}, status_code=429,
                                            headers={"retry-after": str(int(wait))})
                data: dict[str, Any] = {}
                if body:
                    if int(request.headers.get("content-length") or 0) > MAX_BODY:
                        raise ServiceError("the request is larger than 60 MB", 413)
                    raw = await request.body()
                    if raw:
                        try:
                            data = await request.json()
                        except ValueError as exc:
                            raise ServiceError("the body is not JSON") from exc
                        if not isinstance(data, dict):
                            raise ServiceError("the body must be a JSON object")
                out = await run_in_threadpool(fn, request, data) if body else await run_in_threadpool(fn, request)
                if isinstance(out, Response):
                    return out
                code = status
                if isinstance(out, tuple):
                    out, code = out
                return JSONResponse(out, status_code=code)
            except ServiceError as exc:
                return JSONResponse({"detail": str(exc)}, status_code=exc.status, headers={"www-authenticate": "Bearer"} if exc.status == 401 else None)
            except (StoreError, ProfileError) as exc:
                return JSONResponse({"detail": str(exc)}, status_code=400)
            except KeyError as exc:
                return JSONResponse({"detail": str(exc.args[0]) if exc.args else "not found"}, status_code=404)
            except (ValueError, TypeError, AttributeError, IndexError) as exc:   # a value of the wrong kind in the request (limit=abc, a list where an object is expected)
                return JSONResponse({"detail": f"the request could not be read: {str(exc)[:200] or type(exc).__name__}"}, status_code=400)
            except Exception as exc:   # anything else is ours: say so plainly, keep the detail in the server's log
                log.exception("unhandled error on %s %s", request.method, request.url.path)
                return JSONResponse({"detail": f"the server could not do that ({type(exc).__name__}). The detail is in the server's log."}, status_code=500)
        return endpoint

    T, A = tenant, actor
    ref = lambda r: r.path_params["ref"]  # noqa: E731

    def config(r: Request) -> dict[str, Any]:
        return {**svc.config(), "product": PRODUCT, "version": __version__, "auth": auth, "links": links or {}, "tenant": T(r), "actor": A(r)}

    def add_documents(r: Request, reference: str, documents: list[Any], replaces: str | None = None) -> int:
        n = 0
        for d in documents or []:
            name, data, category = Service.decode(d if isinstance(d, dict) else {})
            svc.add_document(T(r), reference, name, data, category=category, by=A(r), replaces=replaces or (d.get("replaces") if isinstance(d, dict) else None))
            n += 1
        return n

    def create(r: Request, d: dict[str, Any]) -> Any:
        case = svc.create_case(T(r), str(d.get("reference") or ""), d.get("profile"), d.get("fields") or {}, by=A(r))
        add_documents(r, case["reference"], d.get("documents") or [])
        if d.get("assess") and d.get("documents"):
            svc.assess_in_background(T(r), case["reference"], by=A(r))
        return view(svc.case(T(r), case["reference"])), 201

    def sample(r: Request, d: dict[str, Any]) -> Any:
        case = svc.load_sample(T(r), str(d.get("profile") or ""), str(d.get("sample") or ""), by=A(r))
        svc.assess_in_background(T(r), case["reference"], by=A(r))
        return view(svc.case(T(r), case["reference"])), 201

    def upload(r: Request, d: dict[str, Any]) -> Any:
        add_documents(r, ref(r), d.get("documents") or [], d.get("replaces"))
        if d.get("assess"):
            svc.assess_in_background(T(r), ref(r), by=A(r), trigger="amendment" if svc.case(T(r), ref(r)).get("assessment") else "assessment")
        return view(svc.case(T(r), ref(r)))

    def file(r: Request) -> Response:
        v = r.query_params.get("v")
        if v is not None and not (v.isdigit() and int(v) > 0):
            raise ServiceError("v is the number of a version: 1, 2, ...")
        data, name = svc.file_bytes(T(r), ref(r), r.path_params["doc"], int(v) if v else None)
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        return Response(data, media_type=mime, headers={"content-disposition": f'inline; filename="{name}"', "x-content-type-options": "nosniff",
                                                        "content-security-policy": "sandbox"})

    # the integration surface: submit a package, optionally wait for the assessment, read the result
    def v1_submit(r: Request, d: dict[str, Any]) -> Any:
        reference = safe(str(d.get("reference") or ""), "case reference")
        fetched: list[dict[str, Any]] = []
        if d.get("source") == "repository":   # read the package from the repository before anything is registered: a package that is not there registers nothing
            from .repository import RepositoryError

            repo = svc.repository.for_tenant(T(r)) if hasattr(svc.repository, "for_tenant") else svc.repository
            try:
                fetched = list(repo.fetch_case(reference))
            except RepositoryError as exc:
                raise ServiceError(f"the repository could not give the package of '{reference}': {exc}", 404 if "holds no case" in str(exc) else 502) from exc
        if not svc.store.exists(T(r), reference):
            svc.create_case(T(r), reference, d.get("profile"), d.get("fields") or {}, by=A(r))
        for doc in fetched:
            svc.add_document(T(r), reference, doc["name"], doc["data"], category=doc.get("category", ""), by=A(r))
        add_documents(r, reference, d.get("documents") or [])
        if d.get("wait", True):
            return result(svc.assess(T(r), reference, by=A(r))), 200
        svc.assess_in_background(T(r), reference, by=A(r))
        return result(svc.case(T(r), reference)), 202

    # the knowledge base and the foundation
    def k_graph(r: Request) -> dict[str, Any]:
        q = r.query_params
        levels = [x.strip() for x in (q.get("levels") or "").split(",") if x.strip()]
        types = [x for x in (q.get("types") or "").split(",") if x.strip()]
        return svc.knowledge_ready(T(r)).subgraph(levels=levels or None, types=types or None, focus=q.get("focus") or None, limit=min(600, int(q.get("limit") or 220)))

    def k_map(r: Request) -> dict[str, Any]:
        q = r.query_params
        return svc.knowledge_ready(T(r)).level_map([x for x in (q.get("open") or "").split(",") if x], [x for x in q.getlist("drill") if x], show=q.get("show") or None)

    def k_fact(r: Request, d: dict[str, Any]) -> Any:
        return svc.knowledge_fact(T(r), d, by=A(r)), 201

    def k_import(r: Request, d: dict[str, Any]) -> Any:
        name = str(d.get("name") or "").strip()
        if not name or not d.get("content_base64"):
            raise ServiceError("send the file as {name, content_base64}")
        try:
            data = base64.b64decode(str(d["content_base64"]).split(",")[-1], validate=False)
        except Exception as exc:
            raise ServiceError("content_base64 is not valid base64") from exc
        return svc.knowledge_import(T(r), name, data, str(d.get("label") or ""), level=str(d.get("level") or ""), by=A(r)), 201

    def k_export(r: Request) -> Response:
        return Response(svc.knowledge_ready(T(r)).export_zip(), media_type="application/zip",
                        headers={"content-disposition": 'attachment; filename="docuinsights-knowledge-base.zip"', "x-content-type-options": "nosniff"})

    def k_rebuild(r: Request, d: dict[str, Any]) -> dict[str, Any]:
        from .profiles import load_profiles

        return svc.knowledge(T(r)).rebuild(list(load_profiles().values()))

    def record(r: Request) -> Response:
        masked = (r.query_params.get("mask") or "").lower() in ("1", "true", "yes")
        return HTMLResponse(svc.record(T(r), ref(r), masked=masked, by=A(r)), headers={"cache-control": "no-store", "x-content-type-options": "nosniff",
                                                                                         "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'"})

    def cases_csv(r: Request) -> Response:
        return Response(svc.cases_csv(T(r)), media_type="text/csv; charset=utf-8", headers={"content-disposition": 'attachment; filename="docuinsights-cases.csv"', "x-content-type-options": "nosniff",
                                                                                           "cache-control": "no-store"})

    def page(request: Request) -> HTMLResponse:
        html = (WEB / "index.html").read_text(encoding="utf-8").replace("{{BASE}}", base(request)).replace("{{VERSION}}", __version__).replace("{{AUTH}}", "key" if auth == "key" else "host")
        return HTMLResponse(html, headers={"cache-control": "no-cache"})

    def static(request: Request) -> Response:
        name = request.path_params["name"]
        target = (WEB / name).resolve()
        if WEB.resolve() not in target.parents or not target.is_file() or target.suffix not in (".js", ".css", ".svg"):
            return Response("not found", status_code=404)
        return Response(target.read_bytes(), media_type={".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml"}[target.suffix],
                        headers={"cache-control": "no-cache"})

    routes = [
        Route("/", page), Route("/static/{name}", static),
        Route("/health", lambda r: JSONResponse({"status": "ok", "product": PRODUCT, "version": __version__, "model": "offline" if svc.config()["model"].get("offline") else "connected",
                                                 "api_key": "set" if api_key or auth != "key" else "not set: local callers only"})),
        Route("/api/config", api(config)),
        Route("/api/cases", api(lambda r: svc.cases(T(r)))),
        Route("/api/cases", api(create, body=True), methods=["POST"]),
        Route("/api/samples", api(sample, body=True), methods=["POST"]),
        Route("/api/cases/{ref}", api(lambda r: view(svc.case(T(r), ref(r))))),
        Route("/api/cases/{ref}/documents", api(upload, body=True), methods=["POST"]),
        Route("/api/cases/{ref}/documents/{doc}/file", api(file)),
        Route("/api/cases/{ref}/assess", api(lambda r, d: (view(svc.assess_in_background(T(r), ref(r), by=A(r), trigger=str(d.get("trigger") or "assessment"))), 202), body=True), methods=["POST"]),
        Route("/api/cases/{ref}/amendment", api(lambda r, d: view(svc.next_amendment(T(r), ref(r), by=A(r))), body=True), methods=["POST"]),
        Route("/api/cases/{ref}/review", api(lambda r, d: view(svc.review(T(r), ref(r), d, by=A(r))), body=True), methods=["POST"]),
        Route("/api/cases/{ref}/decision", api(lambda r, d: view(svc.decide(T(r), ref(r), str(d.get("decision") or ""), str(d.get("note") or ""), by=A(r))), body=True), methods=["POST"]),
        Route("/api/cases/{ref}/counterfactual", api(lambda r, d: svc.counterfactual(T(r), ref(r), scenario=d.get("scenario"), change=d.get("change"), value=d.get("value")), body=True), methods=["POST"]),
        Route("/api/cases/{ref}/correspondence", api(lambda r, d: svc.correspondence(T(r), ref(r), str(d.get("kind") or "evidence_request")), body=True), methods=["POST"]),
        Route("/api/cases/{ref}/ask", api(lambda r, d: svc.ask(T(r), ref(r), str(d.get("question") or "")), body=True), methods=["POST"]),
        Route("/api/cases/{ref}/file", api(lambda r, d: svc.file_case(T(r), ref(r), by=A(r)), body=True), methods=["POST"]),
        Route("/api/cases/{ref}/assign", api(lambda r, d: view(svc.assign(T(r), ref(r), str(d.get("to") or ""), by=A(r))), body=True), methods=["POST"]),
        Route("/api/cases/{ref}/record", api(record)),
        Route("/api/insights", api(lambda r: svc.insights(T(r)))),
        Route("/api/export/cases.csv", api(cases_csv)),
        Route("/api/feedback/{profile}", api(lambda r: svc.feedback(T(r), r.path_params["profile"]))),
        Route("/api/feedback/{profile}/{id}", api(lambda r, d: svc.decide_proposal(T(r), r.path_params["profile"], r.path_params["id"], str(d.get("decision") or ""), by=A(r),
                                                                                    accept_changes=bool(d.get("accept_changes"))), body=True), methods=["POST"]),
        Route("/api/knowledge", api(lambda r: svc.knowledge_overview(T(r)))),
        Route("/api/knowledge/graph", api(k_graph)),
        Route("/api/knowledge/map", api(k_map)),
        Route("/api/knowledge/entity", api(lambda r: svc.knowledge_ready(T(r)).entity(r.query_params.get("id") or ""))),
        Route("/api/knowledge/trail", api(lambda r: svc.knowledge_ready(T(r)).way_to(r.query_params.get("id") or ""))),
        Route("/api/knowledge/search", api(lambda r: svc.knowledge_ready(T(r)).search(r.query_params.get("q") or ""))),
        Route("/api/knowledge/bases/{id}", api(lambda r, d: svc.knowledge_switch(T(r), r.path_params["id"], bool(d.get("on")), by=A(r)), body=True), methods=["POST"]),
        Route("/api/knowledge/facts", api(k_fact, body=True), methods=["POST"]),
        Route("/api/knowledge/facts/{id}/remove", api(lambda r, d: svc.knowledge_fact_remove(T(r), r.path_params["id"], by=A(r)), body=True), methods=["POST"]),
        Route("/api/knowledge/imports/{id}/place", api(lambda r, d: svc.knowledge_place(T(r), r.path_params["id"], str(d.get("level") or ""), by=A(r)), body=True), methods=["POST"]),
        Route("/api/knowledge/imports/{id}/restore", api(lambda r, d: svc.knowledge_restore(T(r), r.path_params["id"], by=A(r)), body=True), methods=["POST"]),
        Route("/api/cases/{ref}/knowledge", api(lambda r: svc.case_knowledge(T(r), ref(r)))),
        Route("/api/knowledge/export", api(k_export)),
        Route("/api/knowledge/imports", api(k_import, body=True), methods=["POST"]),
        Route("/api/knowledge/imports/{id}/remove", api(lambda r, d: svc.knowledge_remove(T(r), r.path_params["id"], by=A(r)), body=True), methods=["POST"]),
        Route("/api/knowledge/rebuild", api(k_rebuild, body=True), methods=["POST"]),
        Route("/api/platform", api(lambda r: svc.platform(T(r)))),
        Route("/api/ledger", api(lambda r: {"verify": svc.ledger(T(r)).verify(), "entries": svc.ledger(T(r)).entries(case=r.query_params.get("case") or None, limit=min(500, int(r.query_params.get("limit") or 100)))})),
        Route("/api/v1/knowledge", api(lambda r: svc.knowledge_overview(T(r)))),
        Route("/api/v1/knowledge/export", api(k_export)),
        Route("/api/v1/cases", api(v1_submit, body=True), methods=["POST"]),
        Route("/api/v1/cases/{ref}", api(lambda r: result(svc.case(T(r), ref(r))))),
        Route("/api/v1/cases/{ref}/record", api(record)),
        Route("/api/v1/insights", api(lambda r: svc.insights(T(r)))),
        Route("/api/v1/cases/{ref}/ask", api(lambda r, d: svc.ask(T(r), ref(r), str(d.get("question") or "")), body=True), methods=["POST"]),
        Route("/api/v1/health", api(lambda r: {"product": PRODUCT, "version": __version__, "model": svc.config()["model"], "repository": svc.repository.status()})),
    ]
    async def plainly(request: Request, exc: Exception) -> Response:   # an address or a method this API does not have: said as the rest of the API says things
        code = getattr(exc, "status_code", 500)
        if request.url.path.startswith("/api/") or request.url.path == "/api":
            return JSONResponse({"detail": {404: "there is nothing at this address", 405: f"this address does not take {request.method}"}.get(code, str(getattr(exc, "detail", "")) or "the request was refused")}, status_code=code)
        return Response(str(getattr(exc, "detail", "")) or "not found", status_code=code, media_type="text/plain")

    app = Starlette(routes=routes, exception_handlers={HTTPException: plainly})
    app.state.service = svc
    return app


class StripPrefix:
    """Standalone behind a reverse proxy that forwards the prefix unchanged (DOCUINSIGHTS_ROOT_PATH=/docuinsights)."""

    def __init__(self, app: Any, prefix: str):
        self.app, self.prefix = app, prefix.rstrip("/")

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http" and self.prefix:
            path = scope.get("path", "")
            if path == self.prefix or path.startswith(self.prefix + "/"):
                path = path[len(self.prefix):] or "/"
            scope = {**scope, "path": path, "raw_path": path.encode(), "root_path": self.prefix}
        await self.app(scope, receive, send)
