"""X-API-Key handling — the blank-key case that locked the whole UI out.

The defect: with no key configured (the documented default; the lifespan warns about it rather than
refusing to start) the service was open ONLY to callers sending no header at all. The shipped frontend
always sets the header (`"X-API-Key": NEXT_PUBLIC_API_KEY ?? ""`), so every page's fetch got 401 and
rendered its error state. curl hid it (no header -> 200) and the unit suite hid it (it sends a real
key). The e2e gate found it: `- main: Failed to load config`.

RED leg: restore `return provided is None` in `app/core/security._matches` and
`test_blank_key_with_no_key_configured_is_not_a_credential` fails with 401.

Behaviour table pinned here (expected key -> supplied key -> verdict):
    ""       -> absent      -> allow      (open service)
    ""       -> "" / "  "   -> allow      (a blank value is not a credential)   <-- the fix
    ""       -> "anything"  -> 401        (a wrong key is still a wrong key)
    "k"      -> "k"         -> allow
    "k"      -> absent/""/wrong -> 401
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.core.errors import E_AUTH, PolsiaError, http_status_for
from app.core.security import check_api_key, verify_api_key


@pytest.fixture
def blank_key(monkeypatch):
    monkeypatch.setattr(settings, "api_key", "")
    yield


@pytest.fixture
def real_key(monkeypatch):
    monkeypatch.setattr(settings, "api_key", "unit-test-key")
    yield


@pytest.mark.asyncio
async def test_open_service_allows_the_absent_key(blank_key):
    assert await verify_api_key(None) == ""


@pytest.mark.asyncio
async def test_blank_key_with_no_key_configured_is_not_a_credential(blank_key):
    """The regression: an empty header must behave exactly like no header.

    Whitespace counts as blank for the VERDICT (it is not a credential), but the function echoes what
    it was given — pinning `== ""` for a whitespace value would be asserting cosmetics, so the second
    line checks the verdict and normalises what comes back.
    """
    assert await verify_api_key("") == ""
    assert (await verify_api_key("   ")).strip() == "", "a whitespace value must be allowed"


@pytest.mark.asyncio
async def test_open_service_still_rejects_a_wrong_key(blank_key):
    """Opening the door must not mean ignoring a key that was actually offered.

    A wrong key now raises the app's NAMED refusal (`E_AUTH`, rendered as 401 by the app's exception
    handler and carrying the WWW-Authenticate challenge for any code that wants it). This asserted the
    bare HTTPException's own `status_code` attribute; the contract that matters is the code plus the
    HTTP mapping, so both are pinned here and the end-to-end status is pinned by the api_client tests.
    """
    for wrong in ("hunter2", "dev-key", "0"):
        with pytest.raises(PolsiaError) as excinfo:
            await verify_api_key(wrong)
        assert excinfo.value.code == E_AUTH
        assert http_status_for(E_AUTH) == 401


@pytest.mark.asyncio
async def test_configured_key_accepts_only_itself(real_key):
    assert await verify_api_key("unit-test-key") == "unit-test-key"
    for supplied in (None, "", "   ", "unit-test-ke", "unit-test-keyy"):
        with pytest.raises(PolsiaError) as excinfo:
            await verify_api_key(supplied)
        assert excinfo.value.code == E_AUTH, (
            f"supplied={supplied!r} must be rejected as a NAMED refusal when a key is configured"
        )
        assert http_status_for(E_AUTH) == 401


@pytest.mark.asyncio
async def test_the_inline_check_matches_the_dependency(real_key):
    """`check_api_key` is the path-param-first variant; the two must agree."""
    await check_api_key("unit-test-key")
    with pytest.raises(PolsiaError) as excinfo:
        await check_api_key("")
    assert excinfo.value.code == E_AUTH
    assert http_status_for(excinfo.value.code) == 401


@pytest.mark.asyncio
async def test_a_rejected_key_answers_with_the_named_refusal_and_its_challenge(api_client):
    """End to end: 401, the refusal body, and the WWW-Authenticate challenge.

    The challenge is the part a refactor drops without noticing. This path used to raise a bare
    `HTTPException` that carried `WWW-Authenticate: X-API-Key`; making the failure nameable (E_AUTH)
    would have silently lost that header if the app's handler did not put it back — main.py's
    `_refusal_headers`. RED leg: delete the `_refusal_headers(exc)` argument from the handler and the
    header assertion fails while the status stays 401.
    """
    resp = await api_client.get("/api/v1/config", headers={"X-API-Key": "definitely-wrong"})
    assert resp.status_code == 401
    assert resp.headers.get("WWW-Authenticate") == "X-API-Key", (
        "a named auth refusal must keep the protocol-level challenge"
    )
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == E_AUTH
    assert body["error"]["how_to_fix"], "a named refusal must say how to fix it"
