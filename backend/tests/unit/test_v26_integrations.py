"""V26 / L2 — Integrations & secrets (RED-first).

Written BEFORE `app/services/integration_status_service.py`, before
`app/api/v1/integrations.py`, before the router include and before any Settings section exists.
On the untouched base tree this module fails at collection (`cannot import name
'integration_status_service'`), and the route probe recorded separately in
`wave-v26/l2-integrations/raw/RED-route-404.txt` shows the API answering
`GET /api/v1/integrations -> 404` — the honest RED state for a capability that is absent.

WHAT THIS PINS (the brief's frozen interface):

  * the provider registry is exactly `github, render, sendgrid, stripe, meta_ads, google_ads`
    in that order, with `github`/`render`/`sendgrid` writable and the three ad/payment
    providers READ-ONLY;
  * status is masked only: `configured`, `source` (`stored`/`env`/`mixed`/`none`) and the
    store's own `mask_secret` hint — the saved value never appears in a response body, a
    GET after save, an error body or a log line;
  * save writes through `app/services/secret_store.set_secret` (the ONE existing store), and
    clear removes the stored entry so a provider with no env value reads `configured:false`;
  * `POST /integrations/{provider}/test` is a READ-ONLY bounded probe:
      - `github` -> `GET https://api.github.com/user`, `render` -> `GET
        https://api.render.com/v1/services?limit=1`;
      - 401/403 -> `E_AUTH`, 5xx/timeout/transport -> `E_UPSTREAM`, a missing variable ->
        `E_CONFIG` NAMING it, and NO request leaves the process in that case;
      - `sendgrid`/`stripe`/`meta_ads`/`google_ads` have no read-only probe in this build and
        refuse BY NAME (`E_VALIDATION`) — a test never fabricates a success;
      - unknown provider -> `E_NOT_FOUND` (404);
  * every save/clear/test writes an `activity_log` row carrying provider + action + outcome
    and NO secret anywhere in `detail` or `summary`;
  * the routes sit behind the existing `verify_api_key` dependency.

THE ONLY FAKE is the outbound HTTP transport: `httpx.MockTransport` installed on
`integration_status_service._TRANSPORT` — a LABELLED test double, exactly the seam
`github_service._TRANSPORT` already uses. Validation, key resolution, masking, the store,
the audit row and the refusal mapping all run for real.
"""
from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy import select

from app.config import settings
from app.core.errors import (
    E_AUTH,
    E_CONFIG,
    E_NOT_FOUND,
    E_UPSTREAM,
    E_VALIDATION,
)
from app.models.report import ActivityLog
from app.services import integration_status_service, secret_store

#: Clearly-fake ASCII credentials — a real GitHub token is a long provider-issued string; this one cannot collide.
FAKE_GITHUB = "fake-github-token-v26-do-not-use"
FAKE_RENDER = "rnd_v26fake_do_not_use_9876543210"
FAKE_SENDGRID = "SG.v26fake_do_not_use_abcdefghij"
FAKE_STRIPE = "sk_test_v26fake_do_not_use_11223344"

PROVIDERS = ["github", "render", "sendgrid", "stripe", "meta_ads", "google_ads"]
READ_ONLY = {"stripe", "meta_ads", "google_ads"}
WRITABLE = {"github", "render", "sendgrid"}

#: Every credential name the registry may read, with the Settings field `get_secret` falls back to
#: (secret_store.get_secret: env first, then `settings.<name.lower()>`).  Cleared so a developer's
#: real `.env` can never decide what these tests observe.
CREDENTIAL_FIELDS = {
    "GITHUB_TOKEN": "github_token",
    "RENDER_API_KEY": "render_api_key",
    "RENDER_SERVICE_ID": "render_service_id",
    "SENDGRID_API_KEY": "sendgrid_api_key",
    "STRIPE_SECRET_KEY": "stripe_secret_key",
    "STRIPE_WEBHOOK_SECRET": "stripe_webhook_secret",
    "META_ADS_ACCESS_TOKEN": "meta_ads_access_token",
    "META_ADS_ACCOUNT_ID": "meta_ads_account_id",
    "GOOGLE_ADS_DEVELOPER_TOKEN": "google_ads_developer_token",
    "GOOGLE_ADS_CLIENT_ID": "google_ads_client_id",
    "GOOGLE_ADS_REFRESH_TOKEN": "google_ads_refresh_token",
    "GOOGLE_ADS_CUSTOMER_ID": "google_ads_customer_id",
}


class Wire:
    """LABELLED TEST DOUBLE — a fake provider wire, not a live call.

    Routes `(METHOD, "host/path")` to an `httpx.Response` (or an exception to raise) and
    records every request so a test can prove the probe was a single READ-ONLY GET.
    """

    def __init__(self, routes: dict[tuple[str, str], httpx.Response | Exception]) -> None:
        self.routes = routes
        self.calls: list[dict] = []

    @staticmethod
    def key(request: httpx.Request) -> tuple[str, str]:
        return (request.method, f"{request.url.host}{request.url.path}")

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append({
            "method": request.method,
            "url": str(request.url),
            "authorization": request.headers.get("authorization", ""),
        })
        route = self.routes.get(self.key(request))
        if route is None:
            return httpx.Response(
                404,
                json={"message": f"test wire has no route for {request.method} {request.url}"},
            )
        if isinstance(route, Exception):
            raise route
        return route

    def install(self, monkeypatch: pytest.MonkeyPatch) -> Wire:
        monkeypatch.setattr(
            integration_status_service, "_TRANSPORT", httpx.MockTransport(self)
        )
        return self


@pytest.fixture(autouse=True)
def clean_credentials(monkeypatch, tmp_path):
    """A test-only secret store and NO ambient credential anywhere; returns the store path."""
    store_path = tmp_path / "secrets.json"
    monkeypatch.setenv(secret_store.SECRETS_FILE_ENV_VAR, str(store_path))
    for name, attribute in CREDENTIAL_FIELDS.items():
        monkeypatch.delenv(name, raising=False)
        monkeypatch.setattr(settings, attribute, "", raising=True)
    monkeypatch.setattr(integration_status_service, "_TRANSPORT", None, raising=True)
    return store_path


async def _save(api_client, auth_headers, provider: str, values: dict[str, str]):
    return await api_client.put(
        f"/api/v1/integrations/{provider}", json={"values": values}, headers=auth_headers
    )


async def _audit_rows(db, action: str) -> list[ActivityLog]:
    result = await db.execute(select(ActivityLog).where(ActivityLog.action == action))
    return list(result.scalars().all())


# ── 1. the registry and the masked status ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_status_lists_the_six_frozen_providers_masked_and_unconfigured(
    api_client, auth_headers
) -> None:
    response = await api_client.get("/api/v1/integrations", headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert [entry["provider"] for entry in body["providers"]] == PROVIDERS

    for entry in body["providers"]:
        assert set(entry) == {
            "provider", "label", "configured", "source", "read_only", "testable", "secrets",
        }
        assert entry["configured"] is False
        assert entry["source"] == "none"
        assert entry["read_only"] is (entry["provider"] in READ_ONLY)
        assert entry["testable"] is (entry["provider"] in {"github", "render"})
        assert entry["secrets"], "a provider with no secrets is not a provider"
        for secret in entry["secrets"]:
            assert set(secret) == {"name", "configured", "source", "masked"}
            assert secret["configured"] is False
            assert secret["source"] == "none"
            assert secret["masked"] == "…"

    github = body["providers"][0]
    assert [secret["name"] for secret in github["secrets"]] == ["GITHUB_TOKEN"]
    render = body["providers"][1]
    assert [secret["name"] for secret in render["secrets"]] == [
        "RENDER_API_KEY", "RENDER_SERVICE_ID",
    ]


# ── 2. save: configured true, masked, never echoed, stored, audited ─────────────────────────

@pytest.mark.asyncio
async def test_save_stores_masks_and_never_echoes_the_token(
    api_client, auth_headers, async_db_session, clean_credentials, caplog
) -> None:
    caplog.set_level("DEBUG")
    response = await _save(api_client, auth_headers, "github", {"GITHUB_TOKEN": FAKE_GITHUB})
    assert response.status_code == 200, response.text
    status = response.json()
    assert status["provider"] == "github"
    assert status["configured"] is True
    assert status["source"] == "stored"
    secret = status["secrets"][0]
    assert secret["configured"] is True and secret["source"] == "stored"
    assert secret["masked"].startswith(FAKE_GITHUB[:3]) and secret["masked"].endswith(FAKE_GITHUB[-4:])
    assert secret["masked"] != FAKE_GITHUB, "a mask that equals the secret is not a mask"
    assert FAKE_GITHUB not in response.text

    # it reached the ONE existing store, so the V25 GitHub/deploy adapters read the same value
    document = json.loads(clean_credentials.read_text(encoding="utf-8"))
    assert document["GITHUB_TOKEN"] == FAKE_GITHUB

    again = await api_client.get("/api/v1/integrations/github", headers=auth_headers)
    assert again.status_code == 200, again.text
    assert again.json()["configured"] is True
    assert FAKE_GITHUB not in again.text

    rows = await _audit_rows(async_db_session, "integration_save")
    assert len(rows) == 1, f"expected exactly one save row, got {len(rows)}"
    row = rows[0]
    assert row.detail["provider"] == "github"
    assert row.detail["action"] == "save"
    assert row.detail["outcome"] == "ok"
    assert FAKE_GITHUB not in json.dumps(row.detail)
    assert FAKE_GITHUB not in row.summary
    assert FAKE_GITHUB not in caplog.text, "no log line may carry the saved value"


@pytest.mark.asyncio
async def test_save_refuses_an_unknown_secret_name_by_name(
    api_client, auth_headers, clean_credentials
) -> None:
    response = await _save(api_client, auth_headers, "github", {"GITHUB_TOKEN": FAKE_GITHUB,
                                                               "NOT_A_GITHUB_FIELD": "x"})
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["error"]["code"] == E_VALIDATION
    assert "NOT_A_GITHUB_FIELD" in body["error"]["message"]
    assert not clean_credentials.exists(), "a refused save wrote the store"


# ── 3. test-connection: github ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_github_test_connection_is_one_read_only_get_and_reports_success(
    api_client, auth_headers, monkeypatch
) -> None:
    await _save(api_client, auth_headers, "github", {"GITHUB_TOKEN": FAKE_GITHUB})
    wire = Wire({
        ("GET", "api.github.com/user"): httpx.Response(200, json={"login": "octocat", "id": 1}),
    }).install(monkeypatch)

    response = await api_client.post("/api/v1/integrations/github/test", headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"ok", "provider", "detail"}
    assert body["ok"] is True and body["provider"] == "github"
    assert "octocat" in body["detail"]
    assert FAKE_GITHUB not in response.text

    assert [call["method"] for call in wire.calls] == ["GET"], "the probe must be READ-ONLY"
    assert wire.calls[0]["url"] == "https://api.github.com/user"
    assert wire.calls[0]["authorization"] == f"Bearer {FAKE_GITHUB}"


@pytest.mark.asyncio
async def test_github_401_and_403_are_the_named_E_AUTH(
    api_client, auth_headers, monkeypatch, async_db_session
) -> None:
    await _save(api_client, auth_headers, "github", {"GITHUB_TOKEN": FAKE_GITHUB})
    for status_code in (401, 403):
        wire = Wire({
            ("GET", "api.github.com/user"): httpx.Response(
                status_code, json={"message": "Bad credentials"}
            ),
        }).install(monkeypatch)

        response = await api_client.post(
            "/api/v1/integrations/github/test", headers=auth_headers
        )
        assert response.status_code == 401, response.text
        body = response.json()
        assert body["ok"] is False
        assert body["error"]["code"] == E_AUTH
        assert "github" in body["error"]["message"].lower()
        assert FAKE_GITHUB not in response.text
        assert len(wire.calls) == 1

    rows = await _audit_rows(async_db_session, "integration_test")
    assert len(rows) == 2, f"both refused probes must be audited, got {len(rows)}"
    for row in rows:
        assert row.detail["provider"] == "github"
        assert row.detail["action"] == "test"
        assert row.detail["outcome"] == "refused"
        assert row.detail["code"] == E_AUTH
        assert FAKE_GITHUB not in json.dumps(row.detail)


@pytest.mark.asyncio
async def test_github_5xx_and_transport_failure_are_E_UPSTREAM(
    api_client, auth_headers, monkeypatch
) -> None:
    await _save(api_client, auth_headers, "github", {"GITHUB_TOKEN": FAKE_GITHUB})
    for route in (
        httpx.Response(503, json={"message": "unavailable"}),
        httpx.ConnectError("dns failure"),
        httpx.ReadTimeout("slow"),
    ):
        Wire({("GET", "api.github.com/user"): route}).install(monkeypatch)
        response = await api_client.post(
            "/api/v1/integrations/github/test", headers=auth_headers
        )
        assert response.status_code == 502, response.text
        assert response.json()["error"]["code"] == E_UPSTREAM
        assert FAKE_GITHUB not in response.text


@pytest.mark.asyncio
async def test_missing_token_is_E_CONFIG_naming_it_and_sends_no_request(
    api_client, auth_headers, monkeypatch
) -> None:
    wire = Wire({}).install(monkeypatch)
    response = await api_client.post("/api/v1/integrations/github/test", headers=auth_headers)
    assert response.status_code == 503, response.text
    body = response.json()
    assert body["error"]["code"] == E_CONFIG
    assert "GITHUB_TOKEN" in body["error"]["message"]
    assert wire.calls == [], "a missing credential must be refused before any network work"


# ── 4. test-connection: render ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_render_probe_lists_services_read_only(
    api_client, auth_headers, monkeypatch
) -> None:
    await _save(api_client, auth_headers, "render", {
        "RENDER_API_KEY": FAKE_RENDER, "RENDER_SERVICE_ID": "srv-v26test",
    })
    wire = Wire({
        ("GET", "api.render.com/v1/services"): httpx.Response(
            200, json=[{"id": "srv-v26test", "name": "landing"}]
        ),
    }).install(monkeypatch)

    response = await api_client.post("/api/v1/integrations/render/test", headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True and body["provider"] == "render"
    assert FAKE_RENDER not in response.text
    assert [call["method"] for call in wire.calls] == ["GET"]
    assert wire.calls[0]["url"].startswith("https://api.render.com/v1/services?limit=1")
    assert wire.calls[0]["authorization"] == f"Bearer {FAKE_RENDER}"


@pytest.mark.asyncio
async def test_render_without_a_service_id_is_E_CONFIG_naming_it(
    api_client, auth_headers, monkeypatch
) -> None:
    await _save(api_client, auth_headers, "render", {"RENDER_API_KEY": FAKE_RENDER})
    wire = Wire({}).install(monkeypatch)
    response = await api_client.post("/api/v1/integrations/render/test", headers=auth_headers)
    assert response.status_code == 503, response.text
    body = response.json()
    assert body["error"]["code"] == E_CONFIG
    assert "RENDER_SERVICE_ID" in body["error"]["message"]
    assert wire.calls == []


# ── 5. untestable providers refuse by name — never a fabricated success ────────────────────

@pytest.mark.asyncio
async def test_sendgrid_refuses_the_probe_by_name_without_any_request(
    api_client, auth_headers, monkeypatch
) -> None:
    await _save(api_client, auth_headers, "sendgrid", {"SENDGRID_API_KEY": FAKE_SENDGRID})
    wire = Wire({}).install(monkeypatch)
    response = await api_client.post("/api/v1/integrations/sendgrid/test", headers=auth_headers)
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["error"]["code"] == E_VALIDATION
    assert "sendgrid" in body["error"]["message"].lower()
    assert "probe" in body["error"]["message"].lower()
    assert body["ok"] is False
    assert FAKE_SENDGRID not in response.text
    assert wire.calls == []


@pytest.mark.asyncio
async def test_read_only_providers_report_status_but_refuse_writes(
    api_client, auth_headers, monkeypatch, clean_credentials
) -> None:
    monkeypatch.setenv("STRIPE_SECRET_KEY", FAKE_STRIPE)
    monkeypatch.setattr(settings, "stripe_secret_key", FAKE_STRIPE)

    status = await api_client.get("/api/v1/integrations/stripe", headers=auth_headers)
    assert status.status_code == 200, status.text
    body = status.json()
    assert body["read_only"] is True
    assert body["configured"] is True and body["source"] == "env"
    assert body["secrets"][0]["source"] == "env"
    assert body["secrets"][0]["masked"] != FAKE_STRIPE
    assert FAKE_STRIPE not in status.text

    refused = await _save(api_client, auth_headers, "stripe", {"STRIPE_SECRET_KEY": FAKE_STRIPE})
    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["code"] == E_VALIDATION
    assert "read-only" in refused.json()["error"]["message"].lower()
    assert not clean_credentials.exists(), "a read-only provider must never be written"

    cleared = await api_client.delete("/api/v1/integrations/stripe", headers=auth_headers)
    assert cleared.status_code == 422, cleared.text
    assert cleared.json()["error"]["code"] == E_VALIDATION


# ── 6. clear ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_clear_removes_the_stored_value_and_reports_missing(
    api_client, auth_headers, async_db_session, clean_credentials
) -> None:
    await _save(api_client, auth_headers, "github", {"GITHUB_TOKEN": FAKE_GITHUB})
    response = await api_client.delete("/api/v1/integrations/github", headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["configured"] is False
    assert body["source"] == "none"
    assert body["secrets"][0]["masked"] == "…"
    assert FAKE_GITHUB not in response.text

    document = json.loads(clean_credentials.read_text(encoding="utf-8")) \
        if clean_credentials.exists() else {}
    assert "GITHUB_TOKEN" not in document

    rows = await _audit_rows(async_db_session, "integration_clear")
    assert len(rows) == 1
    assert rows[0].detail == {
        "provider": "github", "action": "clear", "outcome": "ok", "secrets": ["GITHUB_TOKEN"],
    }


# ── 7. unknown provider + auth ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unknown_provider_is_a_404_named_E_NOT_FOUND(api_client, auth_headers) -> None:
    for method, path, kwargs in (
        ("get", "/api/v1/integrations/nope", {}),
        ("put", "/api/v1/integrations/nope", {"json": {"values": {"X": "y"}}}),
        ("post", "/api/v1/integrations/nope/test", {}),
        ("delete", "/api/v1/integrations/nope", {}),
    ):
        response = await getattr(api_client, method)(path, headers=auth_headers, **kwargs)
        assert response.status_code == 404, f"{method.upper()} {path}: {response.text}"
        assert response.json()["error"]["code"] == E_NOT_FOUND


@pytest.mark.asyncio
async def test_routes_require_the_owner_key(api_client) -> None:
    response = await api_client.get("/api/v1/integrations")
    assert response.status_code == 401, response.text
    assert response.json()["error"]["code"] == E_AUTH
