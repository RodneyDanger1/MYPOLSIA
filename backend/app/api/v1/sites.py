"""POST/GET /api/v1/sites — build and list the static sites (wave V24, lane L3).

Frozen interface (V24-PLAN):
    POST /api/v1/sites  {title?, tagline?, slug?} -> 201 {slug, url, bytes}
    GET  /api/v1/sites -> [{slug, title, url, created_at, bytes}], newest first
    unknown slug -> E_NOT_FOUND; slug `[a-z0-9-]{2,48}`, collision -> E_VALIDATION naming it

The disk is the one source of truth (`services/site_service`): a site exists iff its directory holds
a complete `meta.json`, and the LIST is written from those files — there is no second registry to
drift.  The generated files are served by the `StaticFiles` mount at `/sites` (wired in `main.py`)
and, through nginx, at `http://<host>/sites/<slug>/`.

`slug` on the GET is an exact filter, not a second route: it reads the one site and answers the
named `E_NOT_FOUND` when it is absent — the honest answer a client asking for a specific site needs
(an empty list would read like "no sites exist").
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, refusal_guard, verify_api_key
from app.services.site_service import generate_site, list_sites

router = APIRouter(prefix="/sites", tags=["sites"], dependencies=[Depends(verify_api_key)])


class SiteCreateRequest(BaseModel):
    """The create body — every field optional; a blank/omitted title defaults to the company name."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    tagline: str | None = None
    slug: str | None = None


class SiteCreatedResponse(BaseModel):
    """Exactly the frozen 201 body: where the site is and how many bytes were written."""

    slug: str
    url: str
    bytes: int


class SiteSummaryResponse(BaseModel):
    """One row of the disk-backed list (`meta.json`), as the list route serves it."""

    slug: str
    title: str
    url: str
    created_at: str
    bytes: int


@router.post("", status_code=201, response_model=SiteCreatedResponse)
async def create_site(
    payload: SiteCreateRequest, db: AsyncSession = Depends(get_db)
) -> SiteCreatedResponse:
    """Generate `data/sites/<slug>/` from the company profile and return its address.

    Refusals by name (never a fabricated success): `E_CONFIG` with no company profile/name,
    `E_VALIDATION` for a malformed slug, an over-long title/tagline, or a slug that already exists.
    """
    with refusal_guard():
        result = await generate_site(
            db, slug=payload.slug, title=payload.title, tagline=payload.tagline
        )
    return SiteCreatedResponse.model_validate(result)


@router.get("", response_model=list[SiteSummaryResponse])
async def list_site_rows(
    slug: str | None = Query(default=None, description="read exactly this slug (404 when absent)")
) -> list[SiteSummaryResponse]:
    """Every built site, newest first; `?slug=` reads one and refuses `E_NOT_FOUND` if it is gone."""
    with refusal_guard():
        rows = list_sites(slug=slug)
    return [SiteSummaryResponse.model_validate(row) for row in rows]
