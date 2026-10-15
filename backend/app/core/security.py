"""API-key authentication (X-API-Key)."""
from __future__ import annotations

import secrets

from fastapi import Header

from app.config import settings
from app.core.errors import E_AUTH, PolsiaError


def _matches(provided: str | None) -> bool:
    expected = settings.api_key or ""
    if not expected:
        # No key configured: the service is OPEN (the lifespan warns loudly about this rather than
        # refusing to start). A caller that sends an EMPTY value is sending no credential, so it must
        # be treated as absent. This used to be `provided is None`, which rejected a blank header —
        # and the shipped frontend ALWAYS sets the header (`"X-API-Key": NEXT_PUBLIC_API_KEY ?? ""`),
        # so against an open backend every page's data load returned 401 and the UI rendered its
        # error/empty states. Found by the e2e gate, which had never been run: the browser snapshot
        # read `- main: Failed to load config` while curl (no header at all) got 200.
        return not (provided or "").strip()
    # U2-01 (wave-v14): compare BYTES, never `str`. `secrets.compare_digest` raises
    # `TypeError: comparing strings with non-ASCII characters is not supported` when either
    # `str` operand contains ANY non-ASCII char — so a header value with one byte >= 0x80
    # (uvicorn decodes header bytes as latin-1) turned every protected route into a 500
    # instead of the named E_AUTH, and a non-ASCII *configured* key 500'd EVERY request.
    # On bytes compare_digest never raises; a non-ASCII value is simply a wrong key.
    try:
        return secrets.compare_digest(
            (provided or "").encode("utf-8"), expected.encode("utf-8"))
    except UnicodeError:  # lone surrogates cannot round-trip UTF-8: not a possible key value
        return False


async def verify_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key")
) -> str:
    """Dependency for protected routes: missing/incorrect key -> 401."""
    if not _matches(x_api_key):
        # A NAMED refusal, like every other failure in this service: the code is what a client branches
        # on, `http_status_for(E_AUTH)` is 401, and the app adds the WWW-Authenticate challenge back
        # (main.py). This path used to raise a bare HTTPException, which made a wrong key the one
        # failure that could not name itself — an audit found E_AUTH had no raise site anywhere.
        raise PolsiaError(
            E_AUTH,
            "the X-API-Key header is missing or does not match the configured key",
        )
    return x_api_key or ""


async def check_api_key(x_api_key: str | None) -> None:
    """Inline check — the parity-tested twin of `verify_api_key`.

    No shipped route calls it any more: U2-05 (wave-v14) moved the trigger route to a
    router-level dependency so auth runs BEFORE path validation. It stays exported for the
    parity suite and as the second supported form of the key check.
    """
    if not _matches(x_api_key):
        raise PolsiaError(
            E_AUTH,
            "the X-API-Key header is missing or does not match the configured key",
        )


def api_key_matches(provided: str | None) -> bool:
    """Public form of `_matches` for non-HTTP transports (the /ws/activity socket).

    Same semantics as every REST route: no key configured => the service is open (a blank
    value counts as "no credential"); otherwise a constant-time compare. Exposed so the
    WebSocket route checks the key through ONE definition instead of a second implementation
    that could drift from `verify_api_key` (audit L1-01/L3-08).
    """
    return _matches(provided)
