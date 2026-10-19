"""POST/GET /api/v1/sites — build and list the static sites (wave V24, lane L3).

Frozen interface (V24-PLAN):
    POST /api/v1/sites  {title?, tagline?, slug?} -> 201 {slug, url, bytes}
    GET  /api/v1/sites -> [{slug, title, url, created_at, bytes}], newest first
    unknown slug -> E_NOT_FOUND; slug `[a-z0-9-]{2,48}`, collision -> E_VALIDATION naming it

Wave V25 (lane L3) adds the Render deploy surface on top of the same disk-backed site:
    POST /api/v1/sites/{slug}/deploy  {dry_run?} (or ?dry_run=) -> the plan (dry, default) or the
         submitted deploy; `E_CONFIG` with no key/service, `E_UPSTREAM` when Render fails,
         `E_VALIDATION` for a slug that does not exist.
    GET  /api/v1/sites/{slug}/deploy -> {slug, configured, missing, service_id, last_deploy|null};
         `E_NOT_FOUND` for a slug that does not exist.

The disk is the one source of truth (`services/site_service`): a site exists iff its directory holds
a complete `meta.json`, and the LIST is written from those files — there is no second registry to
drift.  The generated files are served by the `StaticFiles` mount at `/sites` (wired in `main.py`)
and, through nginx, at `http://<host>/sites/<slug>/`.

`slug` on the GET is an exact filter, not a second route: it reads the one site and answers the
named `E_NOT_FOUND` when it is absent — the honest answer a client asking for a specific site needs
(an empty list would read like "no sites exist").
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, refusal_guard, verify_api_key
from app.services import deploy_service
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


class DeployRequest(BaseModel):
    """The deploy body.  `dry_run` defaults TRUE — the route previews unless told otherwise."""

    model_config = ConfigDict(extra="forbid")

    dry_run: bool = True


class DeployPlanResponse(BaseModel):
    """The exact HTTP call a deploy makes (or would make); the credential value is masked."""

    method: str
    url: str
    headers: dict[str, str]
    body: dict[str, str]


class DeployRecordResponse(BaseModel):
    """The persisted `last_deploy` record (`site_service.record_deploy`).

    Every field is optional-with-default on purpose: the record lives in a human-editable
    `meta.json`, and a hand-edited partial record must read as "unknown", not crash the route.
    """

    at: str | None = None
    state: str = "unknown"
    status: str | None = None
    deploy_id: str | None = None
    url: str | None = None
    error: dict[str, Any] | None = None


class DeployStateResponse(BaseModel):
    """`GET .../deploy` — whether deploy is configured, and the last real deploy (null if none)."""

    slug: str
    configured: bool
    missing: list[str]
    service_id: str | None
    last_deploy: DeployRecordResponse | None


class DeployResponse(BaseModel):
    """`POST .../deploy` — the dry-run plan, or the deploy Render actually accepted."""

    slug: str
    dry_run: bool
    configured: bool
    state: str
    status: str | None
    deploy_id: str | None
    url: str | None
    at: str | None
    message: str
    plan: DeployPlanResponse


@router.post("/{slug}/deploy", response_model=DeployResponse)
async def deploy_site_route(
    slug: str,
    payload: DeployRequest | None = Body(default=None),
    dry_run: bool | None = Query(
        default=None, description="override the body; omitted = the body's value, else TRUE"
    ),
) -> DeployResponse:
    """Preview (dry run, the default) or submit a Render deploy of one generated site.

    `?dry_run=` wins over the body when both are present; with neither, the deploy is DRY.  A dry
    run makes no network call and writes nothing — it returns the exact planned call.  Live mode
    claims success only for a Render response carrying a deploy id, and persists `last_deploy`
    (status/url/at/id) into the site's `meta.json` through `site_service`.
    """
    requested = dry_run if dry_run is not None else (payload.dry_run if payload else True)
    with refusal_guard():
        result = await deploy_service.deploy_site(slug, dry_run=requested)
    return DeployResponse.model_validate(result)


@router.get("/{slug}/deploy", response_model=DeployStateResponse)
async def get_deploy_state(slug: str) -> DeployStateResponse:
    """The last deploy state of one site (never a Render call), `last_deploy: null` if never run.

    `configured: false` plus `missing` is the honest answer when the owner's Render credential or
    service id is absent — the UI hides the Deploy control rather than offering a dead button.
    An unknown slug is the named `E_NOT_FOUND`, the same resource-read rule as `read_site`.
    """
    with refusal_guard():
        result = deploy_service.deploy_state(slug)
    return DeployStateResponse.model_validate(result)


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
