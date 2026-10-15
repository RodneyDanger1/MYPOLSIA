"""POST /api/v1/site/package — the static-site package export (V9 B2, R5 #4).

Owner-key like every sibling v1 router.  The service writes the folder and returns the file
paths; a missing company name/contact is a named `E_CONFIG` refusal.  NO hosting, NO promises —
see `site_package_service`.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, refusal_guard, verify_api_key
from app.services.site_package_service import build_package_archive, build_site_package

router = APIRouter(prefix="/site", tags=["site"], dependencies=[Depends(verify_api_key)])


@router.post("/package")
async def package_site(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """`{package_dir, files, slug}` — the written package's paths, never a hosting promise."""
    with refusal_guard():
        result = await build_site_package(db)
    return result


@router.get("/package/download")
async def download_site_package(
    slug: str | None = None, db: AsyncSession = Depends(get_db)
) -> Response:
    """The written package as a ZIP (`Content-Disposition` names `site-<slug>.zip`).

    Read-only and exports-root-guarded (V12/W-C): `slug` defaults to the current company's
    package; an unsafe slug is the named `E_VALIDATION`, an unbuilt one the named `E_NOT_FOUND`
    — the route never answers an empty zip that reads like success.
    """
    with refusal_guard():
        archive, filename = await build_package_archive(db, slug=slug)
    return Response(
        content=archive,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
