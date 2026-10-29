"""POST/GET /api/v1/sites — build and list the static sites (wave V24, lane L3; V26, lane L5).

Frozen interface (V24-PLAN):
    POST /api/v1/sites  {title?, tagline?, slug?} -> 201 {slug, url, bytes}
    GET  /api/v1/sites -> [{slug, title, url, created_at, bytes}], newest first
    unknown slug -> E_NOT_FOUND; slug `[a-z0-9-]{2,48}`, collision -> E_VALIDATION naming it

Wave 4 (F-32 / R-W2-B2) appends ONE additive key to that 201 body — the public-exposure warning
the create form already carried in prose but an API client could not see:
    POST /api/v1/sites -> 201 {slug, url, bytes, publishing_warnings: [{code, message, fields,
                                how_to_fix}]}      # `[]` when the profile publishes nothing

Wave V25 (lane L3) adds the Render deploy surface on top of the same disk-backed site:
    POST /api/v1/sites/{slug}/deploy  {dry_run?, approval_token?} (or ?dry_run=) -> the plan
         (dry, default) or the submitted deploy; `E_CONFIG` with no key/service, `E_UPSTREAM`
         when Render fails, `E_VALIDATION` for a slug that does not exist.
         A LIVE deploy is POLICY-GATED (AC-03/AC-04): `E_SANDBOX_BLOCKED` while SANDBOX_MODE is
         on, `E_POLICY_DENIED` under the operator kill switch / an exhausted render day cap / a
         missing-or-invalid approval token — Render is never called in any of those cases.
    GET  /api/v1/sites/{slug}/deploy -> {slug, configured, missing, service_id, last_deploy|null};
         `E_NOT_FOUND` for a slug that does not exist.

Wave V26 (lane L5) is ADDITIVE on the same routes:
    POST /api/v1/sites  {title?, tagline?, template?, custom_domain?, slug?} -> the same 201 body.
         `template` is one of `classic` (default), `modern`, `bold`; `custom_domain` is a bare
         hostname RECORDED as-is on the site — this API never resolves DNS and never claims the
         domain is verified or live.  Both refusals are the named `E_VALIDATION`.
    GET  /api/v1/sites -> each V26 row also carries `pages`, `template` and `custom_domain`; a V24
         row (whose `meta.json` predates those keys) keeps exactly the five frozen V24 keys.
    The generated `sitemap.xml` is built from THIS request's `request.base_url`, so a site created
    through nginx lists the URLs a visitor actually gets.

Wave V27 (lane L1) adds the owner-managed lifecycle:
    DELETE /api/v1/sites/{slug} -> 200 {slug, deleted: true}; the site's directory and `meta.json`
         are removed.  An unknown slug is `E_NOT_FOUND`; a slug that is not a plain site name or a
         directory that resolves outside `data/sites` (traversal, absolute, link) is `E_VALIDATION`.
    POST   /api/v1/sites/{slug}/regenerate -> 200 {slug, url, bytes, title, created_at, pages,
         template, custom_domain}; the three pages, `styles.css` and `sitemap.xml` are re-rendered
         from the CURRENT company profile with the site's stored template/custom_domain, keeping its
         slug, title and `created_at`.  A legacy single-page site regenerates to the full V26 shape.

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

from fastapi import APIRouter, Body, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.api.deps import get_db, refusal_guard, verify_api_key
from app.core.errors import PolsiaError
from app.services import deploy_service
from app.services.activity_service import log_activity_isolated
from app.services.site_service import delete_site, generate_site, list_sites, regenerate_site

router = APIRouter(prefix="/sites", tags=["sites"], dependencies=[Depends(verify_api_key)])


class SiteCreateRequest(BaseModel):
    """The create body — every field optional; a blank/omitted title defaults to the company name."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    tagline: str | None = None
    slug: str | None = None
    template: str | None = Field(
        default=None,
        description="template preset: classic (default), modern or bold",
    )
    custom_domain: str | None = Field(
        default=None,
        description="bare hostname recorded with the site — recorded, not verified (no DNS check)",
    )


class PublishingWarning(BaseModel):
    """One public-exposure warning for a created site (F-32; bridge STOREFRONT-CONTRACT §3).

    `fields` names the company-profile fields the generated `contact.html` prints VERBATIM, in the
    order the page prints them; a warning exists only for fields the profile actually fills, and
    `[]` is the honest answer when none are set.
    """

    code: str
    message: str
    fields: list[str]
    how_to_fix: str


class SiteCreatedResponse(BaseModel):
    """The frozen 201 body: where the site is and how many bytes were written.

    F-32 / R-W2-B2 appends the ADDITIVE `publishing_warnings` list (the exact shape in
    `bridge/STOREFRONT-CONTRACT.md` §3).  No existing key changes, so a pre-F-32 client parses the
    body as it always did; `[]` is served — never omitted — when nothing is published.
    """

    slug: str
    url: str
    bytes: int
    publishing_warnings: list[PublishingWarning] = Field(default_factory=list)


class SiteSummaryResponse(BaseModel):
    """One row of the disk-backed list (`meta.json`), as the list route serves it.

    `pages`/`template`/`custom_domain` are V26 additions and stay OPTIONAL: a site whose `meta.json`
    predates V26 has none of them, and the route omits absent values (`response_model_exclude_none`)
    so a V24 row is byte-for-byte the five-key row it always was.
    """

    slug: str
    title: str
    url: str
    created_at: str
    bytes: int
    pages: list[str] | None = None
    template: str | None = None
    custom_domain: str | None = Field(
        default=None,
        description="the bare hostname recorded for this site — recorded, not verified",
    )


class SiteDeletedResponse(BaseModel):
    """`DELETE /sites/{slug}` — the site that was removed (its directory and `meta.json` are gone)."""

    slug: str
    deleted: bool


class SiteRegeneratedResponse(BaseModel):
    """`POST /sites/{slug}/regenerate` — the rebuilt site's record, with its identity preserved."""

    slug: str
    url: str
    bytes: int
    title: str
    created_at: str
    pages: list[str]
    template: str
    custom_domain: str | None = None


class DeployRequest(BaseModel):
    """The deploy body.  `dry_run` defaults TRUE — the route previews unless told otherwise.

    `approval_token` (AC-03) is the single-use operator approval for the LIVE action.  A live
    deploy is irreversible, so the firewall requires a token minted for
    `deploy_service.deploy_payload(slug, service_id)`; without one the route answers the named
    `E_POLICY_DENIED` carrying the fingerprint to approve.  A dry run ignores it.
    """

    model_config = ConfigDict(extra="forbid")

    dry_run: bool = True
    approval_token: str = Field(
        default="",
        description=(
            "single-use approval token for the APPROVED fingerprint of this live deploy "
            "(POST /api/v1/approvals); ignored by a dry run"
        ),
    )


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
    """`GET .../deploy` — whether deploy is configured, the last real deploy, and the fingerprint.

    `approval_fingerprint` (weaver R-UIB02-1) is the value the UI mints a single-use approval token
    for (`POST /api/v1/approvals`) so a live deploy can be authorised on the FIRST attempt instead
    of provoking a refusal to learn the fingerprint.  It is `null` when no Render service id is
    configured: the token is bound to `deploy_payload(slug, service_id)`, so with no service id
    there is no action to approve yet, and a fabricated digest would be mintable-but-unusable.
    """

    slug: str
    configured: bool
    missing: list[str]
    service_id: str | None
    approval_fingerprint: str | None = None
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
    #: The fingerprint this deploy is (or would be) gated on — the same value `GET .../deploy`
    #: serves, so a UI that previewed first need not provoke a refusal to mint.
    approval_fingerprint: str | None = None


DEPLOY_REFUSED_ACTION = "deploy_refused"


async def _audit_deploy_refusal(db: AsyncSession, slug: str, exc: PolsiaError) -> None:
    """ONE audit row for a REFUSED live deploy — the "receipt either way" (AC-03).

    Written through the ISOLATED writer (the shipped F-47 pattern the phone send route uses):
    the request session never commits a failed request, so a row added there would be rolled
    back, while a failed audit write must never rewrite the refusal the owner is reading.  The
    record names the code, the policy code and the approval FINGERPRINT — never the approval
    token and never the Render credential.
    """
    await log_activity_isolated(
        db,
        deploy_service.OWNER_DEPLOY_SLOT,
        DEPLOY_REFUSED_ACTION,
        f"live Render deploy of site {slug} refused: {exc.code}",
        level="warning",
        detail={
            "slug": slug,
            "code": exc.code,
            "policy_code": exc.context.get("policy_code"),
            "fingerprint": exc.context.get("fingerprint"),
        },
    )


@router.post("/{slug}/deploy", response_model=DeployResponse)
async def deploy_site_route(
    slug: str,
    payload: DeployRequest | None = Body(default=None),
    dry_run: bool | None = Query(
        default=None, description="override the body; omitted = the body's value, else TRUE"
    ),
    db: AsyncSession = Depends(get_db),
) -> DeployResponse:
    """Preview (dry run, the default) or submit a Render deploy of one generated site.

    `?dry_run=` wins over the body when both are present; with neither, the deploy is DRY.  A dry
    run makes no network call and writes nothing — it returns the exact planned call.  Live mode
    claims success only for a Render response carrying a deploy id, and persists `last_deploy`
    (status/url/at/id) into the site's `meta.json` through `site_service`.

    LIVE MODE IS POLICY-GATED (AC-03/AC-04): the service refuses with the named
    `E_SANDBOX_BLOCKED` while `SANDBOX_MODE=true`, with `E_POLICY_DENIED` when the operator kill
    switch is `freeze`/`throttle`, when the render day cap is exhausted, or when the action has no
    valid approval token — in every one of those cases Render is never called.  The request
    session is passed down for the cap's measured count (one `channel_send:render` ledger row per
    real deploy).  A refused LIVE attempt writes one isolated `deploy_refused` audit row (never the
    token, never the credential) and then raises the same refusal to the caller.
    """
    requested = dry_run if dry_run is not None else (payload.dry_run if payload else True)
    approval_token = (payload.approval_token if payload else "") or ""
    with refusal_guard():
        try:
            result = await deploy_service.deploy_site(
                slug, dry_run=requested, db=db, approval_token=approval_token
            )
        except PolsiaError as exc:
            if not requested:
                await _audit_deploy_refusal(db, slug, exc)
            raise
    return DeployResponse.model_validate(result)


@router.get("/{slug}/deploy", response_model=DeployStateResponse)
async def get_deploy_state(slug: str) -> DeployStateResponse:
    """The last deploy state of one site (never a Render call), `last_deploy: null` if never run.

    `configured: false` plus `missing` is the honest answer when the owner's Render credential or
    service id is absent — the UI hides the Deploy control rather than offering a dead button.
    `approval_fingerprint` is the fingerprint to mint an approval token for (R-UIB02-1); it is
    `null` while the Render service id is unknown, because the token is bound to it.
    An unknown slug is the named `E_NOT_FOUND`, the same resource-read rule as `read_site`.
    """
    with refusal_guard():
        result = deploy_service.deploy_state(slug)
    return DeployStateResponse.model_validate(result)


@router.post("", status_code=201, response_model=SiteCreatedResponse)
async def create_site(
    payload: SiteCreateRequest, request: Request, db: AsyncSession = Depends(get_db)
) -> SiteCreatedResponse:
    """Generate `data/sites/<slug>/` from the company profile and return its address.

    V26 writes three pages (index/about/contact), the chosen template's stylesheet and — from this
    request's `base_url` — a `sitemap.xml`.  `custom_domain` is recorded with the site exactly as
    sent (a bare hostname); it is never resolved or marked verified here.

    Refusals by name (never a fabricated success): `E_CONFIG` with no company profile/name,
    `E_VALIDATION` for a malformed slug, an over-long title/tagline, an unknown template preset, a
    malformed custom domain, or a slug that already exists.
    """
    with refusal_guard():
        result = await generate_site(
            db,
            slug=payload.slug,
            title=payload.title,
            tagline=payload.tagline,
            template=payload.template,
            custom_domain=payload.custom_domain,
            base_url=str(request.base_url),
        )
    return SiteCreatedResponse.model_validate(result)


@router.get("", response_model=list[SiteSummaryResponse], response_model_exclude_none=True)
async def list_site_rows(
    slug: str | None = Query(default=None, description="read exactly this slug (404 when absent)")
) -> list[SiteSummaryResponse]:
    """Every built site, newest first; `?slug=` reads one and refuses `E_NOT_FOUND` if it is gone.

    `response_model_exclude_none` keeps a V24 row at its five frozen keys: the V26 fields are absent
    from that site's `meta.json`, and "absent" must not be rendered as `null` (which would claim the
    site HAS a template and lost it).
    """
    with refusal_guard():
        rows = list_sites(slug=slug)
    return [SiteSummaryResponse.model_validate(row) for row in rows]


@router.delete("/{slug}", response_model=SiteDeletedResponse)
async def delete_site_route(slug: str) -> SiteDeletedResponse:
    """Remove one site's directory — its pages, `styles.css`, sitemap and `meta.json`.

    The disk is the site's only record, so the removed directory is a removed site everywhere: the
    list, the `StaticFiles` mount and any later read.  Removing a tree is BLOCKING filesystem work,
    so it runs in a worker thread (`run_in_threadpool`, the `/ops` cleanup precedent) instead of
    stalling the event loop for every other request (V29/L1 T1.2).  Refusals by name: `E_NOT_FOUND`
    when the slug is already gone (a second delete is honest, not a 200), `E_VALIDATION` for a
    slug/path that is not a direct child of `data/sites` (traversal, absolute or a link),
    `E_STORAGE` when the filesystem refuses the removal.
    """
    with refusal_guard():
        result = await run_in_threadpool(delete_site, slug)
    return SiteDeletedResponse.model_validate(result)


@router.post("/{slug}/regenerate", response_model=SiteRegeneratedResponse)
async def regenerate_site_route(
    slug: str, request: Request, db: AsyncSession = Depends(get_db)
) -> SiteRegeneratedResponse:
    """Rebuild one site from the CURRENT company profile, keeping its identity.

    Writes all three pages, the stored template's `styles.css` and — from this request's
    `base_url` — `sitemap.xml`.  `slug`, `title`, `created_at`, `template` and `custom_domain` are
    preserved: a rebuild is not a rename or a re-creation, and the stored domain remains a RECORD
    (nothing here resolves DNS).  A legacy V24 site becomes the full V26 shape.

    Refusals by name: `E_CONFIG` with no company profile/name, `E_NOT_FOUND` for an unknown slug,
    `E_VALIDATION` for a path outside `data/sites` or a stored template/domain that is no longer
    valid, `E_STORAGE` when a file cannot be written.
    """
    with refusal_guard():
        result = await regenerate_site(db, slug, base_url=str(request.base_url))
    return SiteRegeneratedResponse.model_validate(result)
