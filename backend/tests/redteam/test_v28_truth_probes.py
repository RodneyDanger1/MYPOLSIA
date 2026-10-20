"""V28 / L6 — adversarial TRUTH probes (redteam).

Five questions, each answered from behaviour rather than from a docstring:

1. AUTH — is any `/api/v1` operation reachable without the owner key other than the three
   deliberately public ones (health, the Stripe webhook, the unsubscribe link)?
2. AUTH — does a missing key answer the NAMED `E_AUTH` (401 + the header), not a
   framework 422 that leaks whether the path/params exist?
3. SECRETS — can a value stored through Settings -> Integrations come back out of any
   response?
4. REFUSALS — does an untestable provider refuse by name, naming the missing variables,
   instead of returning a success-shaped "ok"?
5. FABRICATED SUCCESS — does the Render deploy adapter refuse `E_CONFIG` naming BOTH
   variables before a dry run, instead of previewing a call it could never make?
"""
from __future__ import annotations

import pytest

from app.core.errors import E_AUTH, E_CONFIG, E_NOT_FOUND, E_VALIDATION, PolsiaError

#: The ONLY operations allowed to answer without X-API-Key. Each is public for a stated
#: reason: liveness (health), Stripe cannot send a header (webhook), and a mail recipient
#: has no key (unsubscribe). Adding a name here is an auth decision, not a test detail.
PUBLIC_OPERATIONS = {
    "/api/v1/health",
    "/api/v1/finance/stripe/webhook",
    "/api/v1/outreach/unsubscribe",
}


def _walk_for_guard(dependency, seen=None) -> bool:
    from app.core.security import check_api_key, verify_api_key

    if seen is None:
        seen = set()
    if id(dependency) in seen:
        return False
    seen.add(id(dependency))
    if dependency.call in (verify_api_key, check_api_key):
        return True
    return any(_walk_for_guard(child, seen) for child in (dependency.dependencies or []))


def _unguarded_operations() -> set[str]:
    from fastapi.routing import APIRoute

    from app.main import create_app

    app = create_app()
    unguarded = set()
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if not route.path.startswith("/api/v1"):
            continue
        if any(_walk_for_guard(dep) for dep in route.dependant.dependencies):
            continue
        unguarded.add(route.path)
    return unguarded


# --------------------------------------------------------------------------- #
# 1. Auth coverage: exactly the three public operations, and no other.
# --------------------------------------------------------------------------- #
def test_no_api_operation_is_unauthenticated_except_the_three_public_ones():
    unguarded = _unguarded_operations()
    assert unguarded == PUBLIC_OPERATIONS, (
        "auth coverage moved. Unguarded /api/v1 operations: "
        f"{sorted(unguarded)}; expected exactly {sorted(PUBLIC_OPERATIONS)}. "
        "A new route without Depends(verify_api_key) is an owner-key bypass."
    )


# --------------------------------------------------------------------------- #
# 2. Missing key is the NAMED 401 everywhere, never a validation oracle.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_missing_key_is_a_named_401_across_routers(api_client):
    probes = [
        ("GET", "/api/v1/sites"),
        ("GET", "/api/v1/prospects"),
        ("GET", "/api/v1/integrations"),
        ("GET", "/api/v1/ops/capabilities"),
        ("GET", "/api/v1/phone/summary"),
        ("GET", "/api/v1/dashboard/summary"),
        ("GET", "/api/v1/config"),
        ("POST", "/api/v1/agents/social_media/trigger"),
    ]
    for method, path in probes:
        response = await api_client.request(method, path)
        assert response.status_code == 401, f"{method} {path}: {response.status_code} {response.text[:200]}"
        body = response.json()
        assert body["error"]["code"] == E_AUTH, f"{method} {path}: {body}"
        assert body["error"]["how_to_fix"], f"{method} {path}: refusal carries no fix"
        assert response.headers.get("X-Polsia-Error-Code") == E_AUTH, (
            f"{method} {path}: refusal lost its machine-readable header"
        )


# --------------------------------------------------------------------------- #
# 3. Secrets cannot come back out of the integrations surface.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_stored_secret_never_appears_in_the_integrations_response(
    api_client, auth_headers, tmp_path, monkeypatch
):
    from app.services import secret_store

    # A scratch store, so the assertion never depends on the owner's real secrets file.
    monkeypatch.setenv("POLSIA_SECRETS_FILE", str(tmp_path / "secrets.json"))
    sentinel = "fake-github-sentinel-v28-do-not-use"
    secret_store.set_secret("GITHUB_TOKEN", sentinel)

    response = await api_client.get("/api/v1/integrations", headers=auth_headers)
    assert response.status_code == 200, response.text
    assert sentinel not in response.text, (
        "the integrations response echoed a stored credential value — a live secret leak"
    )
    github = next(p for p in response.json()["providers"] if p["provider"] == "github")
    assert github["configured"] is True
    token = next(s for s in github["secrets"] if s["name"] == "GITHUB_TOKEN")
    assert token["masked"] and sentinel not in token["masked"]
    # No secret object may carry a value-shaped field at all.
    for provider in response.json()["providers"]:
        for secret in provider["secrets"]:
            assert "value" not in secret, f"{provider['provider']}/{secret['name']} carries a value field"


# --------------------------------------------------------------------------- #
# 4. An untestable provider refuses BY NAME — never a fabricated ok:true.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_untestable_provider_refuses_and_names_the_missing_variables(
    api_client, auth_headers, tmp_path, monkeypatch
):
    from app.config import settings

    monkeypatch.setenv("POLSIA_SECRETS_FILE", str(tmp_path / "secrets.json"))
    for name in ("META_ADS_ACCESS_TOKEN", "META_ADS_ACCOUNT_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(settings, "meta_ads_access_token", "", raising=False)
    monkeypatch.setattr(settings, "meta_ads_account_id", "", raising=False)

    response = await api_client.post("/api/v1/integrations/meta_ads/test", headers=auth_headers)
    assert response.status_code == 503, response.text
    error = response.json()["error"]
    assert error["code"] == E_CONFIG
    # The refusal must NAME what is missing, not say "not configured" in general.
    assert "META_ADS_ACCESS_TOKEN" in error["message"]
    assert "META_ADS_ACCOUNT_ID" in error["message"]
    assert response.json()["ok"] is False, "a refusal answered as a success"


# --------------------------------------------------------------------------- #
# 5. Unknown provider is E_NOT_FOUND, not an empty masked status.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_unknown_provider_is_a_named_404(api_client, auth_headers):
    response = await api_client.get("/api/v1/integrations/not-a-provider", headers=auth_headers)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == E_NOT_FOUND


# --------------------------------------------------------------------------- #
# 6. The deploy adapter refuses before it previews a call it cannot make.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_deploy_dry_run_refuses_config_and_names_both_render_variables(monkeypatch):
    from app.config import settings
    from app.services import deploy_service, site_service

    for name in ("RENDER_API_KEY", "RENDER_SERVICE_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(settings, "render_api_key", "", raising=False)
    monkeypatch.setattr(settings, "render_service_id", "", raising=False)
    # The site exists (that is not what is missing); credentials are.
    monkeypatch.setattr(site_service, "read_site_meta", lambda slug, root=None: {"slug": slug})

    with pytest.raises(PolsiaError) as caught:
        await deploy_service.deploy_site("v28-truth-probe", dry_run=True)

    assert caught.value.code == E_CONFIG
    assert "RENDER_API_KEY" in caught.value.message
    assert "RENDER_SERVICE_ID" in caught.value.message
    assert caught.value.how_to_fix, "a configuration refusal must carry the fix"


# --------------------------------------------------------------------------- #
# 7. The validation/not-found split on deploy is the documented one (no lying 200).
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_deploy_of_unknown_slug_is_validation_and_state_read_is_not_found(monkeypatch):
    from app.services import deploy_service

    def _missing(slug, root=None):
        raise PolsiaError(E_NOT_FOUND, f"no site exists for slug {slug!r}")

    monkeypatch.setattr(deploy_service.site_service, "read_site_meta", _missing)

    with pytest.raises(PolsiaError) as request_side:
        await deploy_service.deploy_site("no-such-site", dry_run=True)
    assert request_side.value.code == E_VALIDATION  # bad input named by the REQUEST

    with pytest.raises(PolsiaError) as read_side:
        deploy_service.deploy_state("no-such-site")
    assert read_side.value.code == E_NOT_FOUND  # a resource READ keeps E_NOT_FOUND
