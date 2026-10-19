"""Wire model for `POST /api/v1/site/package` (V20/L1 — closes V19/L2-06 for that route).

WHY: the route declared `-> dict[str, Any]`; the three keys it serves are the whole contract the
V9 site-package tests and any operator script read.  `files` is the ABSOLUTE paths of the written
package members, in render order — no hosting promise, no URL (see `site_package_service`).
"""
from __future__ import annotations

from pydantic import BaseModel

__all__ = ["SitePackageResponse"]


class SitePackageResponse(BaseModel):
    """`{package_dir, files, slug}` — the written package's paths."""

    package_dir: str
    files: list[str]
    slug: str
