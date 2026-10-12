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
    """Opening the door must not mean ignoring a key that was actually offered."""
    for wrong in ("hunter2", "dev-key", "0"):
        with pytest.raises(Exception) as excinfo:
            await verify_api_key(wrong)
        assert "401" in str(getattr(excinfo.value, "status_code", "")) or getattr(
            excinfo.value, "status_code", None
        ) == 401


@pytest.mark.asyncio
async def test_configured_key_accepts_only_itself(real_key):
    assert await verify_api_key("unit-test-key") == "unit-test-key"
    for supplied in (None, "", "   ", "unit-test-ke", "unit-test-keyy"):
        with pytest.raises(Exception) as excinfo:
            await verify_api_key(supplied)
        assert getattr(excinfo.value, "status_code", None) == 401, (
            f"supplied={supplied!r} must be rejected when a key is configured"
        )


@pytest.mark.asyncio
async def test_the_inline_check_matches_the_dependency(real_key):
    """`check_api_key` is the path-param-first variant; the two must agree."""
    await check_api_key("unit-test-key")
    with pytest.raises(Exception) as excinfo:
        await check_api_key("")
    assert getattr(excinfo.value, "status_code", None) == 401
