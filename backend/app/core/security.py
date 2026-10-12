"""API-key authentication (X-API-Key)."""
from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, status

from app.config import settings


def _matches(provided: str | None) -> bool:
    expected = settings.api_key or ""
    if not expected:
        # No key configured: only allow when the caller sends nothing.
        return provided is None
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
