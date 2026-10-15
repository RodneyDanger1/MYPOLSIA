"""Static-site PACKAGE export (V9 B2, R5 #4): `POST /api/v1/site/package`.

Builds `site-<slug>/` — `index.html` (about/business copy, services, contact) + `HOSTING-README.md`
— from the company's OWN fields into `backend/data/exports/`, and returns the file paths.  NO
hosting, NO deployment, NO promises: the package is a folder of static files and the README says
exactly that (deployment stays retired per D24; the endpoint name says "package").

Deterministic: the same company fields render byte-identical HTML (no timestamps, no ordering
nondeterminism), every interpolated value is HTML-escaped, and the only refusal is `E_CONFIG` when
the company row — or its name/contact — is missing (a site built without them would be a page
nobody could be answered from).
"""
from __future__ import annotations

import html
import io
import re
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import E_CONFIG, E_NOT_FOUND, E_VALIDATION, PolsiaError
from app.models.company import CompanyConfig
from app.services.company_service import get_company

#: Where packages land (module attribute so a test can point it at a scratch dir).
EXPORTS_ROOT = Path(__file__).resolve().parents[2] / "data" / "exports"

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name}</title>
<meta name="description" content="{tagline}">
</head>
<body>
<header>
<h1>{name}</h1>
<p>{tagline}</p>
</header>
<main>
<section id="about">
<h2>About</h2>
<p>{about}</p>
</section>
<section id="services">
<h2>Services</h2>
{services}
</section>
<section id="contact">
<h2>Contact</h2>
<address>
{contact}
</address>
</section>
</main>
<footer>
<p>{name} — built from the company profile. Static files only; hosting is not provided.</p>
</footer>
</body>
</html>
"""

_SERVICES_EMPTY = (
    "<p>Services are not configured yet — add them to the company profile before publishing "
    "this site.</p>"
)
_CONTACT_ADDRESS_PLACEHOLDER = "<p>[Add your business mailing address before publishing]</p>"


def _slug(name: str) -> str:
    """A filesystem-safe slug: lowercase alphanumerics joined by single dashes."""
    text = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return text or "company"


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _tagline(company: CompanyConfig) -> str:
    for candidate in (company.value_prop, company.description, company.mission):
        if candidate and str(candidate).strip():
            return str(candidate).strip().split("\n")[0]
    return f"What {company.name} does — add a value proposition to the company profile."


def _about(company: CompanyConfig) -> str:
    parts = [value.strip() for value in (company.description, company.mission) if value and str(value).strip()]
    if not parts:
        return "Add a company description in the settings to fill this section."
    return " ".join(parts)


def _services(company: CompanyConfig) -> str:
    """The SERVICES section: the confirmed idea (name + one-liner) when one is configured."""
    idea = company.confirmed_idea if isinstance(company.confirmed_idea, dict) else {}
    name = str(idea.get("name") or "").strip()
    if not name:
        return _SERVICES_EMPTY
    one_liner = str(idea.get("one_liner") or "").strip()
    item = f"{name} — {one_liner}" if one_liner else name
    return f"<ul>\n<li>{_esc(item)}</li>\n</ul>"


def _contact(company: CompanyConfig) -> str:
    """The CONTACT section: the real email, plus explicitly-labelled placeholder slots."""
    lines = [f"<p>Email: {_esc(company.notification_email)}</p>"]
    address = str(company.sender_postal_address or "").strip()
    lines.append(f"<p>{_esc(address)}</p>" if address else _CONTACT_ADDRESS_PLACEHOLDER)
    website = str(company.website_url or "").strip()
    if website:
        lines.append(_website_line(website))
    return "\n".join(lines)


def _website_line(website: str) -> str:
    """The website as a LINK only when its scheme is http(s); anything else stays plain text.

    `_esc` escapes HTML metacharacters but not URL SCHEMES — a `javascript:`/`data:` value
    rendered into `href` would be click-to-run code in the published artifact (U2-09,
    wave-v14: probe2_out.json rendered `<a href="javascript:alert(5)">`). The configured
    value is never silently dropped: a non-http(s) URL renders escaped, without a link.
    """
    scheme = urlsplit(website).scheme.lower()
    if scheme in ("http", "https"):
        return f'<p>Web: <a href="{_esc(website)}">{_esc(website)}</a></p>'
    return f"<p>Web: {_esc(website)} (shown as text — not an http(s) URL)</p>"


def render_site_package(company: CompanyConfig) -> dict[str, str]:
    """`{filename: content}` for the package — pure and deterministic (no clock, no I/O)."""
    html_document = _HTML_TEMPLATE.format(
        name=_esc(company.name),
        tagline=_esc(_tagline(company)),
        about=_esc(_about(company)),
        services=_services(company),
        contact=_contact(company),
    )
    readme = (
        f"# {company.name} — static site package\n\n"
        "This folder is a plain static site: `index.html` plus this README. There is **no hosting, "
        "no deployment and no uptime promise** attached to it — nothing here is published, and "
        "Polsia makes no promises about what happens if you publish it.\n\n"
        "## Contents\n\n"
        "- `index.html` — the site page (about, services, contact), built from the company profile.\n"
        "- `HOSTING-README.md` — this file.\n\n"
        "## Publishing it yourself\n\n"
        "Any static host can serve this folder (upload `index.html` where it serves the site "
        "root). Review the contact section first: placeholder lines are marked in the page.\n"
    )
    return {"index.html": html_document, "HOSTING-README.md": readme}


async def build_site_package(db: AsyncSession, root: Path | None = None) -> dict[str, Any]:
    """Write the package under `root` (default `EXPORTS_ROOT`) and return its paths.

    Refuses `E_CONFIG` BY NAME when there is no company row, or its name / contact
    (`notification_email`) is missing — the refusal names which one.
    """
    company = await get_company(db)
    if company is None:
        raise PolsiaError(
            E_CONFIG,
            "no company profile exists, so there is nothing to build a site package from",
            how_to_fix="create the company profile in settings first (PUT /api/v1/config)",
            context={"missing": "company"},
        )
    missing = []
    if not str(company.name or "").strip():
        missing.append("name")
    if not str(company.notification_email or "").strip():
        missing.append("contact (notification_email)")
    if missing:
        raise PolsiaError(
            E_CONFIG,
            f"site package needs the company {' and '.join(missing)}",
            how_to_fix="set the missing company field(s) in settings (PUT /api/v1/config) and retry",
            context={"missing": missing},
        )

    exports_root = Path(root) if root is not None else EXPORTS_ROOT
    package_dir = exports_root / f"site-{_slug(str(company.name))}"
    package_dir.mkdir(parents=True, exist_ok=True)
    files: list[str] = []
    for filename, content in render_site_package(company).items():
        target = package_dir / filename
        target.write_text(content, encoding="utf-8")
        files.append(str(target))
    return {"package_dir": str(package_dir), "files": files, "slug": _slug(str(company.name))}


# ── V12/W-C: the download READ path (exports-root-guarded; never serves outside the root) ───

#: Only the exact shapes `_slug()` can produce may be resolved on disk.
_SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def resolve_package_dir(slug: str, root: Path | None = None) -> Path:
    """The exports-root-guarded directory for `slug` — never anything outside the exports root.

    Refuses `E_VALIDATION` BY NAME for any slug `_slug()` could not have produced (slashes,
    dots, spaces cannot pass the pattern), and re-checks containment after `resolve()` as a
    second belt — the download route must never serve a path that left the root.
    """
    exports_root = (Path(root) if root is not None else EXPORTS_ROOT).resolve()
    if not _SLUG_PATTERN.fullmatch(slug or ""):
        raise PolsiaError(
            E_VALIDATION,
            f"site package slug {slug!r} is not a filesystem-safe slug",
            how_to_fix="build the package first (POST /api/v1/site/package) and pass its slug",
            context={"slug": slug},
        )
    target = (exports_root / f"site-{slug}").resolve()
    if not target.is_relative_to(exports_root):  # unreachable past the pattern; belt only
        raise PolsiaError(
            E_VALIDATION,
            f"site package slug {slug!r} resolves outside the exports root",
            how_to_fix="pass the slug POST /api/v1/site/package returned",
            context={"slug": slug},
        )
    return target


def zip_package(package_dir: Path) -> bytes:
    """The package directory as an in-memory ZIP (sorted entries; symlinks that leave the
    directory are skipped — a redirect cannot smuggle an outside file into the archive)."""
    root = package_dir.resolve()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob("*")):
            if not path.is_file() or not path.resolve().is_relative_to(root):
                continue
            archive.write(path, arcname=path.relative_to(root).as_posix())
    return buffer.getvalue()


async def build_package_archive(
    db: AsyncSession, *, slug: str | None = None
) -> tuple[bytes, str]:
    """`(zip_bytes, filename)` for the package named by `slug` (default: the current company's).

    Read-only — builds nothing.  A missing package directory is the named `E_NOT_FOUND` (the
    route answers 404 with how_to_fix), never an empty zip that reads like success.
    """
    if slug is None:
        company = await get_company(db)
        if company is None:
            raise PolsiaError(
                E_CONFIG,
                "no company profile exists, so there is no package to download",
                how_to_fix="create the company profile in settings first (PUT /api/v1/config)",
                context={"missing": "company"},
            )
        slug = _slug(str(company.name))
    package_dir = resolve_package_dir(slug)
    if not package_dir.is_dir():
        raise PolsiaError(
            E_NOT_FOUND,
            f"no site package exists for slug {slug!r}",
            how_to_fix="build it first with POST /api/v1/site/package, then retry the download",
            context={"slug": slug},
        )
    return zip_package(package_dir), f"site-{slug}.zip"


__all__ = [
    "EXPORTS_ROOT",
    "build_package_archive",
    "build_site_package",
    "render_site_package",
    "resolve_package_dir",
    "zip_package",
]
