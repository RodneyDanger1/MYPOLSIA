"""The generated-site static route — the ONE place a site directory's bytes reach a visitor.

WHY THIS EXISTS (F-39, `visuals/VISUALS-FINDINGS-W5.md` §4, measured live on `7b81f56`)

The `/sites/<slug>/` mount was a bare `StaticFiles(directory=..., html=True)`, so it served **every
file** under `data/sites/<slug>/` with HTTP 200 and no auth:

  * all 17 legacy site directories served `.fixture.json` — the lanes' reversible marker for
    "owner data from a pre-wave drill" — and its body named internal process files
    (`taskmaster/REPORT-W4.md`) on a public page;
  * by the same rule, any future fixture, debug dump or config file a lane wrote into a site
    directory would be published the moment it appeared.

A site directory is a PUBLICATION (`bridge/STOREFRONT-CONTRACT.md` §9), and the generator writes a
known file set (`site_service.PAGES` + `styles.css` + `sitemap.xml` + `meta.json`).  So the route
serves that set — the slug directory itself (whose `index.html` the `html=True` behaviour resolves)
and those files inside it — and refuses everything else as a **named 404**.  The refusal is a
serving decision: the marker files stay on disk (their disposition is the owner's, wave-6 plan
"the markers stay on disk"), they are simply never published.

Honesty rules this module keeps:

  * a refusal is the product envelope (`E_NOT_FOUND` + `how_to_fix` + `X-Meridian-Error-Code`), the
    same wire shape every other 404 in this app speaks — never a bare "Not Found" and never a 200;
  * the refusal names the RULE (`reason`: `dotfile` / `not_a_site_file`) and the path, but never
    confirms whether the refused file exists — a 404 must not become an existence oracle;
  * the generator's real files are untouched: `index.html`, `about.html`, `contact.html`,
    `styles.css`, `sitemap.xml` and `meta.json` (the storefront contract §9 serves `meta.json`);
  * a dotfile/dot-directory is refused at any depth, and `..`-shaped input is refused by the same
    rule — Starlette normalises the URL path before this function runs, so the pure decision is
    also safe on its own;
  * `HEAD` on a refused path answers the same 404 with no body.
"""
from __future__ import annotations

from typing import Any

from starlette.responses import JSONResponse, Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from app.core.errors import E_NOT_FOUND

#: Why a path was refused.  Stable strings so a caller/test branches on the rule, not the prose.
DOTFILE = "dotfile"
NOT_A_SITE_FILE = "not_a_site_file"

#: The files a generated site is made of — `site_service.PAGES` + `STYLES_FILE` + `SITEMAP_FILE` +
#: `meta.json`.  Kept as a literal here (not imported from `site_service`) because this is a
#: SERVING allowlist: growing the generator's output must be a deliberate, tested change to the
#: published surface, never a side effect of adding a file to the renderer.  A test pins the two
#: lists together (`tests/unit/test_w6_f39_site_static_refusal.py`), so drift fails loudly.
SERVED_SITE_FILES: tuple[str, ...] = (
    "index.html",
    "about.html",
    "contact.html",
    "styles.css",
    "sitemap.xml",
    "meta.json",
)

#: `os.sep` and `/` both appear (Starlette's `get_path` normalises to the OS separator on Windows).
_SEPARATORS = ("\\", "/")


def _components(path: str) -> list[str]:
    """The path's components, with empty and `.` segments dropped (never `.`/`..` semantics)."""
    normalised = path or ""
    for separator in _SEPARATORS:
        normalised = normalised.replace(separator, "/")
    return [part for part in normalised.split("/") if part not in ("", ".")]


def _served_path(components: list[str]) -> str:
    """The public path a refusal names — always the mount's own `/sites/…` spelling."""
    return "/sites/" + "/".join(components)


def site_path_refusal(path: str) -> str | None:
    """The refusal reason for one mount-relative path, or `None` when it may be served.

    Allowed shapes, and nothing else:

    * ``""``/``"."`` — the mount root (StaticFiles resolves a root `index.html`, or 404s);
    * ``"<slug>"`` — a site directory (its `index.html` is resolved by `html=True`);
    * ``"<slug>/<one of SERVED_SITE_FILES>"`` — one generated file.

    A component that starts with ``.`` is refused FIRST, so a dotfile/dot-directory is always
    reported as `DOTFILE` (that is the class of leak F-39 found); any other unlisted name is
    `NOT_A_SITE_FILE`.  Nested directories are refused by the same shape rule: the generator writes
    no subdirectories, so nothing legitimate is lost.
    """
    components = _components(path)
    if not components:
        return None  # the mount root — StaticFiles decides (there is no root index.html)
    for component in components:
        if component.startswith("."):
            return DOTFILE
    if len(components) == 1:
        return None  # a site directory
    if len(components) == 2 and components[1] in SERVED_SITE_FILES:
        return None
    return NOT_A_SITE_FILE


def _message(reason: str, served: str) -> tuple[str, str]:
    """`(message, how_to_fix)` for one refusal — built here so the reasons cannot drift."""
    served_list = ", ".join(SERVED_SITE_FILES)
    if reason == DOTFILE:
        return (
            f"{served} is not a page of this site: a name that starts with a dot is an internal "
            "file (a marker, a config, a dot-directory), and an internal file is never published "
            "with a generated site.",
            "open the site root (/sites/<slug>/) or one of its generated pages instead; internal "
            "files stay on disk for the owner and are deliberately not served",
        )
    return (
        f"{served} is not a file of this site: a generated site is made of its own file set "
        f"({served_list}), and this name is not one of them.",
        "open the site root (/sites/<slug>/) or one of its generated pages (index.html, "
        "about.html, contact.html); this route serves a generated site, not an arbitrary "
        "directory",
    )


def site_refusal_body(reason: str, path: str) -> dict[str, Any]:
    """The product envelope for one refusal (the shape `core.errors.to_http_detail` produces)."""
    served = _served_path(_components(path))
    message, how_to_fix = _message(reason, served)
    return {
        "ok": False,
        "error": {
            "code": E_NOT_FOUND,
            "message": message,
            "how_to_fix": how_to_fix,
            "context": {
                "path": served,
                "reason": reason,
                "served_files": list(SERVED_SITE_FILES),
            },
        },
    }


class SiteStaticFiles(StaticFiles):
    """`StaticFiles` for `data/sites` that refuses non-page files BY NAME (F-39).

    Everything else — the directory redirect, `html=True` index resolution, content types, range
    and cache headers — stays Starlette's own implementation: this override only inserts the
    serving decision in front of it, so the fix cannot change how a real page is served.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        method = scope.get("method", "GET")
        if method not in ("GET", "HEAD"):
            # Keep the framework's own 405 answer for a non-read method (only read methods are
            # mounted for static bytes); the refusal below is about the PATH, not the verb.
            return await super().get_response(path, scope)
        reason = site_path_refusal(path)
        if reason is None:
            return await super().get_response(path, scope)
        headers = {"X-Meridian-Error-Code": E_NOT_FOUND, "Cache-Control": "no-store"}
        if method == "HEAD":
            return Response(status_code=404, headers=headers, media_type="application/json")
        return JSONResponse(site_refusal_body(reason, path), status_code=404, headers=headers)


__all__ = [
    "DOTFILE",
    "NOT_A_SITE_FILE",
    "SERVED_SITE_FILES",
    "SiteStaticFiles",
    "site_path_refusal",
    "site_refusal_body",
]
