"""V27 / L3 — SendGrid and Stripe get REAL read-only test-connection probes (RED-first).

WHY THIS FILE EXISTS
--------------------
V26 shipped the integrations surface with two probes (`github`, `render`); every other provider
refused a test BY NAME (`E_VALIDATION`) because no read-only endpoint was wired.  V27 closes the
two that have one:

    sendgrid -> GET https://api.sendgrid.com/v3/scopes   (Bearer API key; 200 = key accepted)
    stripe   -> GET https://api.stripe.com/v1/balance    (Bearer secret key; 200 = key accepted)

Both are READ-ONLY: one GET, no redirects, bounded at `PROBE_TIMEOUT_S`, and the answer is never
echoed — SendGrid reports only the NUMBER of scopes, Stripe reports only that the balance
endpoint answered (amounts are deliberately not read out).

ON THE UNTOUCHED BASE TREE this module FAILS, and that is the point: `sendgrid`/`stripe` report
`testable: false` and a test request on either refuses `E_VALIDATION` (422) instead of probing.
The failure is saved under the lane dir (`raw/RED-backend.txt`).

WHAT THIS PINS
--------------
  * `testable` is true for github, render, sendgrid, stripe and STILL false for meta_ads,
    google_ads (they have no documented read-only endpoint in this build — no fabricated probe);
  * each probe is exactly ONE `GET` to the documented URL, with `Authorization: Bearer <key>`;
  * 401/403 -> `E_AUTH` (401), 5xx/timeout/transport -> `E_UPSTREAM` (502);
  * a missing credential -> `E_CONFIG` (503) NAMING it, and NO request leaves the process;
  * Stripe's probe needs only `STRIPE_SECRET_KEY` — the webhook secret is status-only;
  * no redirect is followed; the probe never outlives the 10 s ceiling;
  * the token appears in no response body, log line or audit row.

THE ONLY DOUBLE is the outbound HTTP transport: `httpx.MockTransport` installed on
`integration_status_service._TRANSPORT` — a LABELLED test double (never product code).  The
registry, credential resolution, masking, refusal mapping, audit row and router all run for real.
"""
from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy import select

from app.config import settings
from app.core.errors import E_AUTH, E_CONFIG, E_UPSTREAM, E_VALIDATION
from app.models.report import ActivityLog
from app.services import integration_status_service, secret_store

#: Clearly-fake ASCII credentials: long enough to be masked, shaped like the real thing, and
#: impossible to confuse with a live value.
FAKE_SENDGRID = "SG.v27fake_do_not_use.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
FAKE_STRIPE = "sk_test_v27fake_do_not_use_000000000000"
FAKE_META_TOKEN = "meta-v27fake-do-not-use"
FAKE_META_ACCOUNT = "act_v27fake"
FAKE_GOOGLE_DEVELOPER = "goog-v27fake-dev"
FAKE_GOOGLE_CLIENT = "goog-v27fake-client"
FAKE_GOOGLE_REFRESH = "goog-v27fake-refresh"
FAKE_GOOGLE_CUSTOMER = "1234567890"

SENDGRID_SCOPES = ["mail.send", "alerts.read", "stats.read"]
STRIPE_BALANCE = {
    "object": "balance",
    "livemode": False,
    "available": [{"amount": 123456, "currency": "usd"}],
    "pending": [{"amount": 7890, "currency": "usd"}],
}

#: Every credential name these tests may observe, with the Settings field behind it.
CREDENTIAL_FIELDS = {
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

    Routes `(METHOD, "host/path")` to an `httpx.Response` (or an exception to raise) and records
    every request, so a test can prove the probe was a single READ-ONLY GET.
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
    """A test-only secret store and NO ambient credential anywhere."""
    store_path = tmp_path / "secrets.json"
    monkeypatch.setenv(secret_store.SECRETS_FILE_ENV_VAR, str(store_path))
    for name, attribute in CREDENTIAL_FIELDS.items():
        monkeypatch.delenv(name, raising=False)
        monkeypatch.setattr(settings, attribute, "", raising=True)
    monkeypatch.setattr(integration_status_service, "_TRANSPORT", None, raising=True)
    return store_path


def _configure_env(monkeypatch: pytest.MonkeyPatch, values: dict[str, str]) -> None:
    """Put a credential in the environment AND the Settings field (the `.env` path)."""
    for name, value in values.items():
        monkeypatch.setenv(name, value)
        monkeypatch.setattr(settings, CREDENTIAL_FIELDS[name], value, raising=True)


async def _audit_rows(db, action: str) -> list[ActivityLog]:
    result = await db.execute(select(ActivityLog).where(ActivityLog.action == action))
    return list(result.scalars().all())


# ── 1. the registry flag ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_status_marks_sendgrid_and_stripe_testable_and_leaves_the_ads_providers_alone(
    api_client, auth_headers
) -> None:
    response = await api_client.get("/api/v1/integrations", headers=auth_headers)
    assert response.status_code == 200, response.text
    providers = response.json()["providers"]
    assert [entry["provider"] for entry in providers] == [
        "github", "render", "sendgrid", "stripe", "meta_ads", "google_ads",
    ]
    testable = {entry["provider"]: entry["testable"] for entry in providers}
    assert testable == {
        "github": True,
        "render": True,
        "sendgrid": True,
        "stripe": True,
        "meta_ads": False,
        "google_ads": False,
    }


# ── 2. sendgrid: one read-only GET, scope COUNT only ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_sendgrid_probe_is_one_read_only_get_and_reports_the_scope_count(
    api_client, auth_headers, monkeypatch
) -> None:
    saved = await api_client.put(
        "/api/v1/integrations/sendgrid",
        json={"values": {"SENDGRID_API_KEY": FAKE_SENDGRID}},
        headers=auth_headers,
    )
    assert saved.status_code == 200, saved.text

    wire = Wire({
        ("GET", "api.sendgrid.com/v3/scopes"): httpx.Response(200, json={"scopes": SENDGRID_SCOPES}),
    }).install(monkeypatch)

    response = await api_client.post("/api/v1/integrations/sendgrid/test", headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"ok", "provider", "detail"}
    assert body["ok"] is True and body["provider"] == "sendgrid"
    assert "api.sendgrid.com/v3/scopes" in body["detail"]
    assert "key accepted (3 scopes)" in body["detail"]
    # The scope LIST is not the key, but it is not needed to prove the key either.
    assert "mail.send" not in body["detail"]
    assert FAKE_SENDGRID not in response.text

    assert [call["method"] for call in wire.calls] == ["GET"], "the probe must be READ-ONLY"
    assert wire.calls[0]["url"] == "https://api.sendgrid.com/v3/scopes"
    assert wire.calls[0]["authorization"] == f"Bearer {FAKE_SENDGRID}"


# ── 3. stripe: one read-only GET, no balances printed ───────────────────────────────────────

@pytest.mark.asyncio
async def test_stripe_probe_reports_the_balance_endpoint_without_printing_amounts(
    api_client, auth_headers, monkeypatch
) -> None:
    _configure_env(monkeypatch, {"STRIPE_SECRET_KEY": FAKE_STRIPE})
    wire = Wire({
        ("GET", "api.stripe.com/v1/balance"): httpx.Response(200, json=STRIPE_BALANCE),
    }).install(monkeypatch)

    response = await api_client.post("/api/v1/integrations/stripe/test", headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True and body["provider"] == "stripe"
    assert "balance endpoint answered 200" in body["detail"]
    assert "123456" not in body["detail"] and "7890" not in body["detail"]
    assert FAKE_STRIPE not in response.text

    assert [call["method"] for call in wire.calls] == ["GET"], "the probe must be READ-ONLY"
    assert wire.calls[0]["url"] == "https://api.stripe.com/v1/balance"
    assert wire.calls[0]["authorization"] == f"Bearer {FAKE_STRIPE}"


@pytest.mark.asyncio
async def test_stripe_probe_does_not_require_the_status_only_webhook_secret(
    api_client, auth_headers, monkeypatch
) -> None:
    """`required` is the probe's need, not every variable the panel reports."""
    _configure_env(monkeypatch, {"STRIPE_SECRET_KEY": FAKE_STRIPE})
    wire = Wire({
        ("GET", "api.stripe.com/v1/balance"): httpx.Response(200, json=STRIPE_BALANCE),
    }).install(monkeypatch)

    response = await api_client.post("/api/v1/integrations/stripe/test", headers=auth_headers)
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    assert len(wire.calls) == 1


# ── 4. refusals: auth, upstream, config ─────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["sendgrid", "stripe"])
async def test_401_and_403_are_the_named_E_AUTH(
    api_client, auth_headers, monkeypatch, provider: str
) -> None:
    if provider == "sendgrid":
        await api_client.put(
            "/api/v1/integrations/sendgrid",
            json={"values": {"SENDGRID_API_KEY": FAKE_SENDGRID}},
            headers=auth_headers,
        )
        token = FAKE_SENDGRID
    else:
        _configure_env(monkeypatch, {"STRIPE_SECRET_KEY": FAKE_STRIPE})
        token = FAKE_STRIPE

    for status_code in (401, 403):
        wire = Wire({
            ("GET", "api.sendgrid.com/v3/scopes"): httpx.Response(
                status_code, json={"errors": [{"message": "access forbidden"}]}
            ),
            ("GET", "api.stripe.com/v1/balance"): httpx.Response(
                status_code, json={"error": {"message": "Invalid API Key provided"}}
            ),
        }).install(monkeypatch)

        response = await api_client.post(
            f"/api/v1/integrations/{provider}/test", headers=auth_headers
        )
        assert response.status_code == 401, response.text
        body = response.json()
        assert body["ok"] is False
        assert body["error"]["code"] == E_AUTH
        assert provider in body["error"]["message"].lower()
        assert token not in response.text
        assert len(wire.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["sendgrid", "stripe"])
async def test_5xx_timeout_and_transport_failure_are_E_UPSTREAM(
    api_client, auth_headers, monkeypatch, provider: str
) -> None:
    if provider == "sendgrid":
        await api_client.put(
            "/api/v1/integrations/sendgrid",
            json={"values": {"SENDGRID_API_KEY": FAKE_SENDGRID}},
            headers=auth_headers,
        )
        key = ("GET", "api.sendgrid.com/v3/scopes")
        token = FAKE_SENDGRID
    else:
        _configure_env(monkeypatch, {"STRIPE_SECRET_KEY": FAKE_STRIPE})
        key = ("GET", "api.stripe.com/v1/balance")
        token = FAKE_STRIPE

    for route in (
        httpx.Response(503, json={"message": "unavailable"}),
        httpx.ConnectError("dns failure"),
        httpx.ReadTimeout("slow"),
    ):
        Wire({key: route}).install(monkeypatch)
        response = await api_client.post(
            f"/api/v1/integrations/{provider}/test", headers=auth_headers
        )
        assert response.status_code == 502, response.text
        assert response.json()["error"]["code"] == E_UPSTREAM
        assert token not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "variable"),
    [("sendgrid", "SENDGRID_API_KEY"), ("stripe", "STRIPE_SECRET_KEY")],
)
async def test_missing_key_is_E_CONFIG_naming_it_and_sends_no_request(
    api_client, auth_headers, monkeypatch, provider: str, variable: str
) -> None:
    wire = Wire({}).install(monkeypatch)
    response = await api_client.post(
        f"/api/v1/integrations/{provider}/test", headers=auth_headers
    )
    assert response.status_code == 503, response.text
    body = response.json()
    assert body["error"]["code"] == E_CONFIG
    assert variable in body["error"]["message"]
    assert wire.calls == [], "a missing credential must be refused before any network work"


# ── 5. bounds: no redirects, ten seconds ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_probe_follows_no_redirect(api_client, auth_headers, monkeypatch) -> None:
    await api_client.put(
        "/api/v1/integrations/sendgrid",
        json={"values": {"SENDGRID_API_KEY": FAKE_SENDGRID}},
        headers=auth_headers,
    )
    wire = Wire({
        ("GET", "api.sendgrid.com/v3/scopes"): httpx.Response(
            302, headers={"Location": "https://evil.example/collect"}
        ),
    }).install(monkeypatch)

    response = await api_client.post("/api/v1/integrations/sendgrid/test", headers=auth_headers)
    assert response.status_code == 502, response.text
    assert response.json()["error"]["code"] == E_UPSTREAM
    assert len(wire.calls) == 1, "a redirect must not be followed"
    assert "evil.example" not in wire.calls[0]["url"]


def test_the_probe_ceiling_is_the_briefs_ten_seconds() -> None:
    assert 0 < integration_status_service.PROBE_TIMEOUT_S <= 10.0


# ── 6. no secret in the audit row or the logs, and the ads refusals still hold ──────────────

@pytest.mark.asyncio
async def test_a_successful_probe_is_audited_without_carrying_the_token(
    api_client, auth_headers, monkeypatch, async_db_session, caplog
) -> None:
    caplog.set_level("DEBUG")
    await api_client.put(
        "/api/v1/integrations/sendgrid",
        json={"values": {"SENDGRID_API_KEY": FAKE_SENDGRID}},
        headers=auth_headers,
    )
    Wire({
        ("GET", "api.sendgrid.com/v3/scopes"): httpx.Response(200, json={"scopes": SENDGRID_SCOPES}),
    }).install(monkeypatch)

    response = await api_client.post("/api/v1/integrations/sendgrid/test", headers=auth_headers)
    assert response.status_code == 200, response.text

    rows = await _audit_rows(async_db_session, "integration_test")
    assert len(rows) == 1, f"expected exactly one test row, got {len(rows)}"
    row = rows[0]
    assert row.detail["provider"] == "sendgrid"
    assert row.detail["action"] == "test"
    assert row.detail["outcome"] == "ok"
    assert FAKE_SENDGRID not in json.dumps(row.detail)
    assert FAKE_SENDGRID not in row.summary
    assert FAKE_SENDGRID not in caplog.text, "no log line may carry the credential"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["meta_ads", "google_ads"])
async def test_providers_without_a_read_endpoint_still_refuse_by_name(
    api_client, auth_headers, monkeypatch, provider: str
) -> None:
    if provider == "meta_ads":
        _configure_env(monkeypatch, {
            "META_ADS_ACCESS_TOKEN": FAKE_META_TOKEN,
            "META_ADS_ACCOUNT_ID": FAKE_META_ACCOUNT,
        })
    else:
        _configure_env(monkeypatch, {
            "GOOGLE_ADS_DEVELOPER_TOKEN": FAKE_GOOGLE_DEVELOPER,
            "GOOGLE_ADS_CLIENT_ID": FAKE_GOOGLE_CLIENT,
            "GOOGLE_ADS_REFRESH_TOKEN": FAKE_GOOGLE_REFRESH,
            "GOOGLE_ADS_CUSTOMER_ID": FAKE_GOOGLE_CUSTOMER,
        })
    wire = Wire({}).install(monkeypatch)

    response = await api_client.post(
        f"/api/v1/integrations/{provider}/test", headers=auth_headers
    )
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["error"]["code"] == E_VALIDATION
    assert provider in body["error"]["message"].lower()
    assert "no read-only probe" in body["error"]["message"].lower()
    assert wire.calls == []
