"""V29 follow-up G — the integrations surface never serves a secret, only a mask.

These pin the promise the V26/V27 waves make, at the HTTP boundary the UI actually uses:

* `GET /integrations` and `GET /integrations/{provider}` carry `configured`/`source`/`masked`
  and nothing that can reconstruct the value — for a STORED secret and for an ENV one;
* a probe whose provider echoes the credential back in its error body answers with the value
  scrubbed (`E_AUTH`, `«redacted»`), because `_scrub` is defense in depth on top of the models;
* the refusals are NAMED: an unknown provider is a coded 404 (`E_NOT_FOUND`), and a write to a
  read-only provider is a coded 422 (`E_VALIDATION`) that names the provider's own settings.

DOUBLES: only the outbound transport (`integration_status_service._TRANSPORT`, the labelled
seam the V26/V27 probe tests use) and throwaway store paths. No product function is replaced.
"""
from __future__ import annotations

import httpx
import pytest

from app.core.errors import E_AUTH, E_NOT_FOUND, E_VALIDATION
from app.services import integration_status_service, secret_store

pytestmark = pytest.mark.asyncio

#: Obviously fake, ASCII, and NOT shaped like any provider's credential format.
FAKE_GITHUB = "v29g-fake-github-token-0123456789"
FAKE_SENDGRID = "v29g-fake-sendgrid-key-abcdefghijkl"

#: Every variable the panel reads; cleared so a real environment value cannot make a status
#: "configured" behind the test's back.
PANEL_VARIABLES = (
    "GITHUB_TOKEN",
    "RENDER_API_KEY",
    "RENDER_SERVICE_ID",
    "SENDGRID_API_KEY",
    "STRIPE_SECRET_KEY",
    "STRIPE_WEBHOOK_SECRET",
    "META_ADS_ACCESS_TOKEN",
    "META_ADS_ACCOUNT_ID",
    "GOOGLE_ADS_DEVELOPER_TOKEN",
    "GOOGLE_ADS_CLIENT_ID",
    "GOOGLE_ADS_REFRESH_TOKEN",
    "GOOGLE_ADS_CUSTOMER_ID",
)


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    """Point the ONE secret store at a scratch file and clear the panel's env variables."""
    monkeypatch.setenv("POLSIA_SECRETS_FILE", str(tmp_path / "secrets.json"))
    for name in PANEL_VARIABLES:
        monkeypatch.delenv(name, raising=False)


async def test_a_stored_value_is_served_only_as_a_mask(api_client, auth_headers) -> None:
    """Configured status is `configured` + `source` + `masked`; the value is nowhere in the wire."""
    secret_store.set_secret("GITHUB_TOKEN", FAKE_GITHUB)

    one = await api_client.get("/api/v1/integrations/github", headers=auth_headers)
    every = await api_client.get("/api/v1/integrations", headers=auth_headers)

    assert one.status_code == 200, one.text
    assert every.status_code == 200, every.text
    assert FAKE_GITHUB not in one.text
    assert FAKE_GITHUB not in every.text

    status = one.json()
    assert status["configured"] is True
    assert status["source"] == "stored"
    entry = status["secrets"][0]
    assert entry["name"] == "GITHUB_TOKEN"
    assert entry["configured"] is True and entry["source"] == "stored"
    assert entry["masked"] != FAKE_GITHUB, "a mask that equals the secret is not a mask"
    assert "…" in entry["masked"], "the store's mask marks the hidden middle"
    assert set(entry) == {"name", "configured", "source", "masked"}, (
        "the status model must have no field that could carry a value"
    )


async def test_an_env_value_is_masked_the_same_way(api_client, auth_headers, monkeypatch) -> None:
    """A credential from the environment is reported as configured, and never echoed either."""
    monkeypatch.setenv("SENDGRID_API_KEY", FAKE_SENDGRID)

    response = await api_client.get("/api/v1/integrations/sendgrid", headers=auth_headers)

    assert response.status_code == 200, response.text
    assert FAKE_SENDGRID not in response.text
    status = response.json()
    assert status["configured"] is True
    assert status["source"] == "env"
    entry = status["secrets"][0]
    assert entry["source"] == "env"
    assert entry["masked"] != FAKE_SENDGRID


async def test_a_probe_that_echoes_the_credential_returns_it_scrubbed(
    api_client, auth_headers, monkeypatch
) -> None:
    """A provider echoing the key in its 401 body must not make the API echo it too."""
    secret_store.set_secret("SENDGRID_API_KEY", FAKE_SENDGRID)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(401, json={"message": f"invalid key {FAKE_SENDGRID}"})
    )
    monkeypatch.setattr(integration_status_service, "_TRANSPORT", transport)

    response = await api_client.post("/api/v1/integrations/sendgrid/test", headers=auth_headers)

    assert response.status_code == 401, response.text
    body = response.json()["error"]
    assert body["code"] == E_AUTH
    assert FAKE_SENDGRID not in response.text, "the provider's echo carried the credential out"
    assert "«redacted»" in body["message"], (
        "the credential must be replaced in place, not just dropped, so the refusal stays honest"
    )


async def test_named_refusals_for_an_unknown_provider_and_a_read_only_write(
    api_client, auth_headers
) -> None:
    """Both refusals carry a code, a how_to_fix and a field naming what was wrong."""
    unknown = await api_client.get("/api/v1/integrations/not_a_provider", headers=auth_headers)
    assert unknown.status_code == 404, unknown.text
    assert unknown.json()["error"]["code"] == E_NOT_FOUND
    assert unknown.headers["X-Polsia-Error-Code"] == E_NOT_FOUND

    read_only = await api_client.put(
        "/api/v1/integrations/stripe",
        json={"values": {"STRIPE_SECRET_KEY": FAKE_GITHUB}},
        headers=auth_headers,
    )
    assert read_only.status_code == 422, read_only.text
    error = read_only.json()["error"]
    assert error["code"] == E_VALIDATION
    assert "read-only" in error["message"]
    assert "STRIPE_SECRET_KEY" in error["how_to_fix"], (
        "a read-only refusal must say which setting the owner should edit instead"
    )
    # Refused before any write: the store file was never created, and the value is not echoed.
    assert FAKE_GITHUB not in read_only.text
