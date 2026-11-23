"""The run mode follows the policy's model: a customer model makes an account mode 2, the layer's model mode 1, and the two cannot be
set to disagree. An account that already disagrees (configured before the two were tied) is flagged. /admin lands on the console."""
from __future__ import annotations

from test_runs import ADMIN, shop_client  # noqa: E402,F401  (the API fixture)

MODE1, MODE2, MODE3 = "coforge_cloud", "coforge_cloud_customer_model", "customer_cloud"
OWN_MODEL = {"version": 1, "models": {"provider": "azure_openai", "endpoint": "https://mm.openai.azure.com/", "api_version": "2025-04-01-preview",
                                      "api_key_env": "MM_KEY", "tiers": {"fast": "gpt-5-mini", "balanced": "gpt-5", "pro": "gpt-5", "complex": "gpt-5"}}}


def _add(client, cid, **body):
    return client.post("/shop/admin/api/customers", json={"id": cid, "name": cid.title(), **body}, headers=ADMIN)


def test_mode_two_follows_from_the_policy_and_cannot_be_set_without_it(shop_client):
    r = _add(shop_client, "mm", run_mode=MODE2)
    assert r.status_code == 409 and "policy" in r.json()["detail"]
    assert _add(shop_client, "mm").status_code == 201
    r = shop_client.patch("/shop/admin/api/customers/mm", json={"run_mode": MODE2}, headers=ADMIN)
    assert r.status_code == 409 and "api_key_env" in r.json()["detail"]
    # a policy naming the customer's model: the account becomes mode 2 by itself
    c = shop_client.put("/shop/admin/api/customers/mm/policy", json=OWN_MODEL, headers=ADMIN).json()
    assert c["run_mode"] == MODE2
    full = shop_client.get("/shop/admin/api/customers/mm", headers=ADMIN).json()
    assert full["run_mode"] == MODE2 and full["warning"] is None and full["policy"]["models"]["provider"] == "azure_openai"
    # mode 1 cannot be asked for while the policy names their model; saving the policy with the layer's model does it
    r = shop_client.patch("/shop/admin/api/customers/mm", json={"run_mode": MODE1}, headers=ADMIN)
    assert r.status_code == 409 and "coforge" in r.json()["detail"]
    c = shop_client.put("/shop/admin/api/customers/mm/policy", json={"version": 1, "models": {"provider": "coforge"}, "limits": {"runs_per_day": 5}}, headers=ADMIN).json()
    assert c["run_mode"] == MODE1
    # mode 3 is a deployment fact: set directly, either way
    c = shop_client.patch("/shop/admin/api/customers/mm", json={"run_mode": MODE3}, headers=ADMIN).json()
    assert c["run_mode"] == MODE3 and c["warning"] is None
    # the other fields still patch, with or without a run mode
    c = shop_client.patch("/shop/admin/api/customers/mm", json={"contact": "ops@mm.example"}, headers=ADMIN).json()
    assert c["contact"] == "ops@mm.example" and c["run_mode"] == MODE3
    # leaving mode 3: mode 1 is allowed while the policy uses the layer's model; mode 2 once the policy names theirs
    assert shop_client.patch("/shop/admin/api/customers/mm", json={"run_mode": MODE1}, headers=ADMIN).json()["run_mode"] == MODE1
    shop_client.patch("/shop/admin/api/customers/mm", json={"run_mode": MODE3}, headers=ADMIN)
    shop_client.put("/shop/admin/api/customers/mm/policy", json=OWN_MODEL, headers=ADMIN)   # a mode-3 account keeps its label ...
    assert shop_client.get("/shop/admin/api/customers/mm", headers=ADMIN).json()["run_mode"] == MODE3
    assert shop_client.patch("/shop/admin/api/customers/mm", json={"run_mode": MODE2}, headers=ADMIN).json()["run_mode"] == MODE2   # ... until asked


def test_an_account_that_already_disagrees_is_flagged_until_fixed(shop_client):
    from caf.config import get_settings
    from caf.shop import get_shop

    assert _add(shop_client, "legacy").status_code == 201
    get_shop(get_settings()).store.update_customer("legacy", run_mode=MODE2)   # as an older console could: the label without the model
    rows = {c["id"]: c for c in shop_client.get("/shop/admin/api/customers", headers=ADMIN).json()}
    assert rows["legacy"]["warning"] and "names no customer model" in rows["legacy"]["warning"]
    # fixing it either way clears the flag: a policy with their model ...
    shop_client.put("/shop/admin/api/customers/legacy/policy", json=OWN_MODEL, headers=ADMIN)
    assert shop_client.get("/shop/admin/api/customers/legacy", headers=ADMIN).json()["warning"] is None
    # ... or back to the layer's model, which also puts the mode right
    c = shop_client.put("/shop/admin/api/customers/legacy/policy", json={"version": 1, "models": {"provider": "coforge"}}, headers=ADMIN).json()
    assert c["run_mode"] == MODE1 and shop_client.get("/shop/admin/api/customers/legacy", headers=ADMIN).json()["warning"] is None


def test_admin_lands_on_the_console(shop_client):
    r = shop_client.get("/admin", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"].endswith("/shop/admin")
    assert shop_client.get("/shop/admin").status_code == 200
