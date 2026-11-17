"""Shop routes on the API service: the storefront, the customer's account and the operator's admin API.

    Public      GET  /shop, /shop/agents/{id}, /shop/account, /shop/admin   the pages
                GET  /shop/catalog, /shop/catalog/{agent_id}                 listings with indicative prices
                POST /shop/requests                                          an access request from the storefront
    Customer    GET  /shop/me, /shop/me/usage                                (bearer: the customer's key)
    Admin       /shop/admin/api/...                                          (bearer: CAF_API_TOKEN, or local)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from ..branding import PRODUCT
from .service import ShopService
from .store import RUN_MODES, ShopError

WEB = Path(__file__).parent / "web"


class CustomerIn(BaseModel):
    id: str
    name: str
    run_mode: str = "coforge_cloud"
    contact: str = ""
    notes: str = ""


class CustomerPatch(BaseModel):
    name: str | None = None
    run_mode: str | None = None
    status: str | None = None
    contact: str | None = None
    notes: str | None = None


class KeyIn(BaseModel):
    label: str = ""


class SubscriptionItem(BaseModel):
    kind: str = Field(description="agent | playbook | all")
    item_id: str = "*"
    ends_at: str | None = Field(default=None, description="YYYY-MM-DD, empty = open-ended")


class RequestIn(BaseModel):
    name: str
    email: str
    company: str = ""
    items: list[str] = Field(default_factory=list)
    run_mode: str = ""
    message: str = ""


def _err(exc: ShopError) -> HTTPException:
    return HTTPException(exc.status, str(exc))


def role_of(request: Request) -> str:
    return getattr(request.state, "role", "local")


def customer_of(request: Request) -> dict[str, Any] | None:
    return getattr(request.state, "customer", None)


def require_admin(request: Request) -> None:
    if role_of(request) not in ("admin", "local"):
        raise HTTPException(403, "This needs the operator token (CAF_API_TOKEN), not a customer key.")


def require_customer(request: Request) -> dict[str, Any]:
    c = customer_of(request)
    if c is None:
        raise HTTPException(401, "Send your customer key as 'Authorization: Bearer <key>'.")
    return c


def register(app: FastAPI, settings: Any, shop: ShopService) -> None:
    def page(request: Request, name: str) -> HTMLResponse:
        base = (request.scope.get("root_path") or "").rstrip("/")  # behind a reverse-proxy prefix the links keep working
        html = (WEB / name).read_text(encoding="utf-8")
        html = html.replace("{{PRODUCT}}", PRODUCT.name).replace("{{COMPANY}}", PRODUCT.company).replace("{{BASE}}", base)
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

    # ---------------------------------------------------------------- pages and static files
    @app.get("/shop", response_class=HTMLResponse, include_in_schema=False)
    def storefront(request: Request) -> HTMLResponse:
        return page(request, "shop.html")

    @app.get("/shop/agents/{agent_id}", response_class=HTMLResponse, include_in_schema=False)
    def listing_page(request: Request, agent_id: str) -> HTMLResponse:
        return page(request, "agent.html")

    @app.get("/shop/account", response_class=HTMLResponse, include_in_schema=False)
    def account_page(request: Request) -> HTMLResponse:
        return page(request, "account.html")

    @app.get("/shop/admin", response_class=HTMLResponse, include_in_schema=False)
    def admin_page(request: Request) -> HTMLResponse:
        return page(request, "admin.html")

    @app.get("/shop/static/{name}", include_in_schema=False)
    def static_file(name: str) -> FileResponse:
        target = (WEB / name).resolve()
        if not target.is_relative_to(WEB.resolve()) or not target.is_file() or target.suffix not in (".css", ".js", ".svg", ".png"):
            raise HTTPException(404, "not found")
        return FileResponse(target, headers={"Cache-Control": "no-cache"})

    # ---------------------------------------------------------------- public catalog
    @app.get("/shop/catalog")
    def shop_catalog() -> dict[str, Any]:
        """Every agent and bundle with its indicative price (public)."""
        return {**shop.listings(), "product": PRODUCT.as_dict()}

    @app.get("/shop/catalog/{agent_id}")
    def shop_listing(agent_id: str) -> dict[str, Any]:
        try:
            return shop.listing(agent_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/shop/requests", status_code=201)
    def access_request(req: RequestIn) -> dict[str, Any]:
        """An access request from the storefront; the operator sees it in the admin console."""
        try:
            rec = shop.store.add_request(name=req.name, email=req.email, company=req.company, items=req.items,
                                         run_mode=req.run_mode, message=req.message)
        except ShopError as exc:
            raise _err(exc) from exc
        return {"id": rec["id"], "status": rec["status"], "message": "Thanks. We will be in touch with your access details."}

    # ---------------------------------------------------------------- the customer's account
    @app.get("/shop/me")
    def me(request: Request) -> dict[str, Any]:
        """Your subscriptions, entitled agents, policy, usage this month and how to connect."""
        c = require_customer(request)
        return shop.account_view(c, str(request.base_url).rstrip("/"))

    @app.get("/shop/me/usage")
    def my_usage(request: Request, month: str | None = Query(None, pattern=r"^\d{4}-\d{2}$")) -> dict[str, Any]:
        c = require_customer(request)
        return shop.store.usage_summary(c["id"], month)

    # ---------------------------------------------------------------- admin
    @app.get("/shop/admin/api/overview")
    def admin_overview(request: Request) -> dict[str, Any]:
        require_admin(request)
        customers = shop.store.list_customers()
        return {"product": PRODUCT.as_dict(), "customers": len(customers),
                "active": sum(1 for c in customers if c["status"] == "active"),
                "requests_new": len(shop.store.list_requests("new")), "usage": shop.store.usage_summary(),
                "run_modes": RUN_MODES, "provider": shop.settings.llm.resolved_provider()}

    @app.get("/shop/admin/api/customers")
    def admin_customers(request: Request) -> list[dict[str, Any]]:
        require_admin(request)
        out = []
        for c in shop.store.list_customers():
            out.append({**c, "policy": shop.policy_of(c).redacted(), "subscriptions": shop.store.list_subscriptions(c["id"]),
                        "entitled_agents": sorted(shop.entitled_agents(c)), "keys": shop.store.list_keys(c["id"])})
        return out

    @app.post("/shop/admin/api/customers", status_code=201)
    def admin_add_customer(request: Request, body: CustomerIn) -> dict[str, Any]:
        require_admin(request)
        try:
            return shop.store.add_customer(body.id, body.name, run_mode=body.run_mode, contact=body.contact, notes=body.notes)
        except ShopError as exc:
            raise _err(exc) from exc

    @app.get("/shop/admin/api/customers/{customer_id}")
    def admin_customer(request: Request, customer_id: str) -> dict[str, Any]:
        require_admin(request)
        try:
            c = shop.store.get_customer(customer_id)
        except ShopError as exc:
            raise _err(exc) from exc
        return {**c, "policy": shop.policy_of(c).redacted(), "subscriptions": shop.store.list_subscriptions(customer_id),
                "entitled_agents": sorted(shop.entitled_agents(c)), "entitled_playbooks": shop.entitled_playbooks(c),
                "keys": shop.store.list_keys(customer_id), "usage": shop.store.usage_summary(customer_id),
                "recent": shop.store.recent_usage(customer_id, 30), "connection": shop.connection_info(c, str(request.base_url).rstrip("/"))}

    @app.patch("/shop/admin/api/customers/{customer_id}")
    def admin_patch_customer(request: Request, customer_id: str, body: CustomerPatch) -> dict[str, Any]:
        require_admin(request)
        try:
            return shop.store.update_customer(customer_id, **{k: v for k, v in body.model_dump().items() if v is not None})
        except ShopError as exc:
            raise _err(exc) from exc

    @app.post("/shop/admin/api/customers/{customer_id}/keys", status_code=201)
    def admin_issue_key(request: Request, customer_id: str, body: KeyIn = Body(default_factory=KeyIn)) -> dict[str, Any]:
        """Issue a key. The full key is in this answer only; afterwards only its prefix is known."""
        require_admin(request)
        try:
            full, rec = shop.store.issue_key(customer_id, body.label)
        except ShopError as exc:
            raise _err(exc) from exc
        return {**rec, "key": full}

    @app.delete("/shop/admin/api/customers/{customer_id}/keys/{key_id}")
    def admin_revoke_key(request: Request, customer_id: str, key_id: str) -> dict[str, Any]:
        require_admin(request)
        try:
            shop.store.revoke_key(customer_id, key_id)
        except ShopError as exc:
            raise _err(exc) from exc
        return {"revoked": key_id}

    @app.put("/shop/admin/api/customers/{customer_id}/subscriptions")
    def admin_subscriptions(request: Request, customer_id: str, items: list[SubscriptionItem]) -> dict[str, Any]:
        require_admin(request)
        try:
            subs = shop.store.set_subscriptions(customer_id, [i.model_dump() for i in items])
        except ShopError as exc:
            raise _err(exc) from exc
        c = shop.store.get_customer(customer_id)
        return {"subscriptions": subs, "entitled_agents": sorted(shop.entitled_agents(c)), "entitled_playbooks": shop.entitled_playbooks(c)}

    @app.put("/shop/admin/api/customers/{customer_id}/policy")
    def admin_policy(request: Request, customer_id: str, policy: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Replace the customer's policy (see config/policies/example-customer.yaml). An inline api_key is encrypted."""
        require_admin(request)
        try:
            c = shop.set_policy(customer_id, policy)
        except ShopError as exc:
            raise _err(exc) from exc
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(400, f"policy rejected: {exc}") from exc
        return {"customer": c["id"], "run_mode": c["run_mode"], "policy": shop.policy_of(c).redacted()}

    @app.post("/shop/admin/api/customers/{customer_id}/check")
    def admin_check(request: Request, customer_id: str, only: str | None = Query(None, description="comma-separated agent ids"),
                    tier: str | None = Query(None, description="force one tier, e.g. fast")) -> dict[str, Any]:
        """Run the subscribed agents' self-tests on this customer's model and keep the result on the customer."""
        require_admin(request)
        try:
            c = shop.store.get_customer(customer_id)
            return shop.check_agents(c, [a.strip() for a in only.split(",") if a.strip()] if only else None, tier=tier)
        except ShopError as exc:
            raise _err(exc) from exc

    @app.get("/shop/admin/api/usage")
    def admin_usage(request: Request, customer: str | None = None, month: str | None = Query(None, pattern=r"^\d{4}-\d{2}$")) -> dict[str, Any]:
        require_admin(request)
        return shop.store.usage_summary(customer, month)

    @app.get("/shop/admin/api/requests")
    def admin_requests(request: Request, status: str | None = None) -> list[dict[str, Any]]:
        require_admin(request)
        return shop.store.list_requests(status)

    @app.patch("/shop/admin/api/requests/{request_id}")
    def admin_request_status(request: Request, request_id: int, status: str = Body(..., embed=True)) -> dict[str, Any]:
        require_admin(request)
        try:
            shop.store.set_request_status(request_id, status)
        except ShopError as exc:
            raise _err(exc) from exc
        return {"id": request_id, "status": status}

    @app.exception_handler(ShopError)
    async def shop_error(_request: Request, exc: ShopError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)
