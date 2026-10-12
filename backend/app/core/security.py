"""API-key authentication (X-API-Key)."""
from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, status

from app.config import settings


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
    return secrets.compare_digest(provided or "", expected)


async def verify_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key")
) -> str:
    """Dependency for protected routes: missing/incorrect key -> 401."""
    if not _matches(x_api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "X-API-Key"},
        )
    return x_api_key or ""


async def check_api_key(x_api_key: str | None) -> None:
    """Inline check used by routes that must validate the path param FIRST."""
    if not _matches(x_api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "X-API-Key"},
        )
