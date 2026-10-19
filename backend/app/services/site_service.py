"""The website builder (wave V24, lane L3): generate + host a real static site.

`generate_site` writes `data/sites/<slug>/{index.html,styles.css,meta.json}` from the company
profile and returns the slug/url/byte count; `list_sites`/`read_site` are the disk-backed reads the
API serves.  There is **one source of truth — the filesystem**: `meta.json` beside each site is what
the list is built from, so the API process, a restarted process and the static mount can never
disagree about which sites exist (no database table to drift from the files).

The generated page, in the order it is assembled:
  * `title` — the caller's title, else the company name;
  * `tagline` — the caller's tagline, else the profile's value prop / description / mission;
  * about — the profile's description (else mission), omitted rather than invented when empty;
  * who-we-serve — `target_market`, omitted when the profile has none;
  * one CTA — a `mailto:` link ONLY when the company profile carries a contact address
    (`notification_email`, this schema's contact field: there is no `sender_email` column), else a
    plain "get in touch" line with NO address invented; and
  * the company's own `website_url` in the footer, as a link only when its scheme is http(s)
    (a `javascript:` URL renders inert text — the V14/U2-09 defect class).

**Every interpolated value is HTML-escaped** (`html.escape(..., quote=True)`), including the mailto
address that lands in an `href`.  The output is deterministic: no clock, no random id, no dict-order
dependence — the same profile + title + tagline render byte-identical files.  `created_at` lives in
`meta.json` only; it never touches the page.

The page is self-contained: a system font stack, one same-origin `styles.css`, no scripts, no
`@import`, no `url(...)`, no external subresource of any kind.  It is semantic (`header`/`main`/
`section`/`footer`) and responsive (fluid type + a small-screen media query).

Slugs are `[a-z0-9-]{2,48}`.  A slug supplied by the caller is validated verbatim (never silently
repaired); an omitted one is DERIVED from the title, then the company name.  A slug that already
exists is the named `E_VALIDATION` — creation refuses, it never overwrites a published site.
"""
from __future__ import annotations

import contextlib
import html
import json
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import E_CONFIG, E_NOT_FOUND, E_STORAGE, E_VALIDATION, PolsiaError
from app.models.company import CompanyConfig
from app.services.company_service import get_company

#: Where generated sites land.  A module attribute (not a constant inlined at call sites) so a test
#: can point the whole service — and `main.create_app()`'s static mount — at a scratch directory.
SITES_ROOT = Path(__file__).resolve().parents[2] / "data" / "sites"

#: The frozen slug contract: lowercase letters, digits and single dashes, 2-48 chars.
SLUG_PATTERN = re.compile(r"^[a-z0-9-]{2,48}$")
SLUG_MIN, SLUG_MAX = 2, 48

#: Bounds on the caller's copy.  A page title is a `<title>`/`<h1>`, not an essay; an over-long
#: value is refused by name instead of being truncated behind the caller's back.
MAX_TITLE = 120
MAX_TAGLINE = 240

_FILES = ("index.html", "styles.css")


# ── small helpers ────────────────────────────────────────────────────────────────────────────
def _esc(value: Any) -> str:
    """HTML-escape ONE interpolated value; quotes escaped too (values land in attributes)."""
    return html.escape(str(value), quote=True)


def _clean(value: Any) -> str:
    """Trimmed text for a nullable/whitespace profile string, `""` when there is nothing."""
    return str(value or "").strip()


def _first_line(value: Any) -> str:
    text = _clean(value)
    return text.splitlines()[0].strip() if text else ""


def derive_slug(title: str | None, *, fallback: str) -> str:
    """A slug from `title` (else `fallback`): lowercase, non-alphanumerics folded to dashes.

    Truncation happens BEFORE the trailing-dash strip so a 48-char cap can never leave a dash at
    the end.  A title with no slug material at all (`"***"`) falls through to the fallback (the
    company name) instead of producing a meaningless `site`; a candidate shorter than the 2-char
    minimum is suffixed with `-site`.  Deterministic, no transliteration guessing.
    """
    candidate = ""
    for source in (_clean(title), _clean(fallback)):
        folded = re.sub(r"[^a-z0-9]+", "-", source.lower()).strip("-")
        folded = re.sub(r"-{2,}", "-", folded)[:SLUG_MAX].strip("-")
        if folded:
            candidate = folded
            break
    if len(candidate) < SLUG_MIN:
        candidate = f"{candidate}-site".strip("-") if candidate else "site"
    return candidate[:SLUG_MAX].strip("-") or "site"


def validate_slug(slug: str) -> str:
    """The slug VERBATIM when it matches the frozen contract, else the named `E_VALIDATION`."""
    if not isinstance(slug, str) or not SLUG_PATTERN.fullmatch(slug):
        raise PolsiaError(
            E_VALIDATION,
            f"site slug {slug!r} is not a valid slug",
            how_to_fix=(
                "use 2-48 characters of lowercase letters, digits and dashes "
                "([a-z0-9-]); omit the slug to derive one from the title"
            ),
            context={"slug": slug, "pattern": SLUG_PATTERN.pattern},
        )
    return slug


def _resolve_root(root: Path | None) -> Path:
    return Path(root) if root is not None else SITES_ROOT


def _profile_fields(company: CompanyConfig) -> dict[str, str]:
    """The profile text the page may print, already trimmed (escaped at interpolation time)."""
    return {
        "name": _clean(company.name),
        "value_prop": _clean(company.value_prop),
        "description": _clean(company.description),
        "mission": _clean(company.mission),
        "target_market": _clean(company.target_market),
        "website_url": _clean(company.website_url),
        "contact_email": _clean(company.notification_email),
    }


def _website_line(website: str) -> str:
    """The footer's website: a link only for an http(s) URL, escaped text otherwise."""
    if not website:
        return ""
    scheme = urlsplit(website).scheme.lower()
    if scheme in ("http", "https"):
        return (
            f'<p class="footer-site">Website: '
            f'<a href="{_esc(website)}" rel="noopener">{_esc(website)}</a></p>'
        )
    return f'<p class="footer-site">Website: {_esc(website)} (not an http(s) link)</p>'


# ── the page ─────────────────────────────────────────────────────────────────────────────────
_STYLES = """/* Generated by Polsia from the company profile — self-contained, no external requests. */
:root {
  color-scheme: light dark;
  --ink: #16181d;
  --muted: #565d6b;
  --paper: #ffffff;
  --line: #dcdfe5;
  --accent: #1f4fd8;
  --accent-ink: #ffffff;
}

* { box-sizing: border-box; }

html { -webkit-text-size-adjust: 100%; }

body {
  margin: 0;
  background: var(--paper);
  color: var(--ink);
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  font-size: 1rem;
  line-height: 1.6;
}

.page {
  max-width: 62rem;
  margin: 0 auto;
  padding: 2.5rem 1.25rem 3rem;
}

.hero { padding-bottom: 2rem; border-bottom: 1px solid var(--line); }

.brand {
  margin: 0 0 1.5rem;
  font-size: 0.8125rem;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--muted);
}

h1 {
  margin: 0 0 0.75rem;
  font-size: clamp(2rem, 6vw, 3.25rem);
  line-height: 1.1;
  letter-spacing: -0.02em;
}

.tagline {
  max-width: 42rem;
  margin: 0 0 1.5rem;
  font-size: clamp(1.0625rem, 2.4vw, 1.25rem);
  color: var(--muted);
}

.cta { margin: 0; }

.button {
  display: inline-block;
  padding: 0.7rem 1.25rem;
  border-radius: 0.375rem;
  background: var(--accent);
  color: var(--accent-ink);
  font-weight: 600;
  text-decoration: none;
}

.button:hover, .button:focus { text-decoration: underline; }

.cta-note { margin: 0; font-weight: 600; }

main { padding: 2rem 0; }

section { max-width: 46rem; margin: 0 0 2rem; }

section:last-child { margin-bottom: 0; }

h2 {
  margin: 0 0 0.5rem;
  font-size: 0.8125rem;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--muted);
}

section p { margin: 0 0 0.75rem; }

footer {
  padding-top: 1.5rem;
  border-top: 1px solid var(--line);
  font-size: 0.875rem;
  color: var(--muted);
}

footer p { margin: 0 0 0.25rem; }

footer a { color: var(--accent); }

@media (max-width: 30rem) {
  .page { padding: 1.75rem 1rem 2.25rem; }
}

@media (prefers-color-scheme: dark) {
  :root {
    --ink: #f2f3f5;
    --muted: #a8aebc;
    --paper: #101216;
    --panel: #191c22;
    --line: #2c313b;
    --accent: #7fa0ff;
    --accent-ink: #0b1020;
  }
}
"""


def render_site(
    company: CompanyConfig,
    *,
    title: str | None = None,
    tagline: str | None = None,
    sender_email: str | None = None,
) -> dict[str, str]:
    """`{filename: content}` for one site — pure, deterministic, every value escaped.

    `sender_email` overrides the profile's contact address (pass `""` to force the no-address
    variant); when it is `None` the company profile's own contact field is used, so the CTA rule is
    one rule: an address that EXISTS renders a `mailto:` link, nothing else renders the plain
    get-in-touch line with no address at all.
    """
    fields = _profile_fields(company)
    name = fields["name"]

    page_title = _clean(title) or name
    hero_tagline = (
        _clean(tagline)
        or _first_line(fields["value_prop"])
        or _first_line(fields["description"])
        or _first_line(fields["mission"])
    )
    # The about section carries the profile prose that the hero did NOT already say: the value
    # proposition, description and mission, de-duplicated and with any sentence equal to the
    # tagline dropped — a page that repeats itself reads generated.  When the profile has no prose
    # at all the section is OMITTED, never filled with a placeholder this generator invented.
    about_paragraphs: list[str] = []
    for candidate in (fields["value_prop"], fields["description"], fields["mission"]):
        if candidate and candidate != hero_tagline and candidate not in about_paragraphs:
            about_paragraphs.append(candidate)
    target_market = fields["target_market"]
    contact = _clean(sender_email) if sender_email is not None else fields["contact_email"]

    parts: list[str] = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{_esc(page_title)}</title>",
    ]
    if hero_tagline:
        parts.append(f'<meta name="description" content="{_esc(hero_tagline)}">')
    parts += ['<link rel="stylesheet" href="styles.css">', "</head>", "<body>", '<div class="page">']

    parts += ['<header class="hero">']
    if name:
        parts.append(f'<p class="brand">{_esc(name)}</p>')
    parts.append(f"<h1>{_esc(page_title)}</h1>")
    if hero_tagline:
        parts.append(f'<p class="tagline">{_esc(hero_tagline)}</p>')
    if contact:
        parts.append(
            f'<p class="cta"><a class="button" href="mailto:{_esc(contact)}">Get in touch</a></p>'
        )
    else:
        parts.append('<p class="cta cta-note">Get in touch to learn more.</p>')
    parts.append("</header>")

    parts.append("<main>")
    if about_paragraphs:
        parts.append('<section id="about">')
        parts.append("<h2>About</h2>")
        parts.extend(f"<p>{_esc(paragraph)}</p>" for paragraph in about_paragraphs)
        parts.append("</section>")
    if target_market:
        parts.append('<section id="who-we-serve">')
        parts.append("<h2>Who we serve</h2>")
        parts.append(f"<p>{_esc(target_market)}</p>")
        parts.append("</section>")
    parts.append("</main>")

    parts.append("<footer>")
    if name:
        parts.append(f"<p>{_esc(name)}</p>")
    website_line = _website_line(fields["website_url"])
    if website_line:
        parts.append(website_line)
    parts.append("</footer>")
    parts += ["</div>", "</body>", "</html>", ""]
    html_document = "\n".join(parts)
    return {"index.html": html_document, "styles.css": _STYLES}


# ── reads ────────────────────────────────────────────────────────────────────────────────────
def _meta_path(slug: str, root: Path) -> Path:
    return root / slug / "meta.json"


def _load_meta(slug: str, root: Path | None) -> dict[str, Any]:
    """The site's FULL `meta.json` document; unknown/unreadable is the named `E_NOT_FOUND`.

    One reader for every meta consumer (`read_site`, `read_site_meta`, `read_deploy_state`,
    `record_deploy`), so the "a site exists iff meta.json is a readable JSON object" rule cannot
    drift between the list, the deploy routes and the builder.
    """
    validated = validate_slug(slug)
    sites_root = _resolve_root(root)
    path = _meta_path(validated, sites_root)
    if not path.is_file():
        raise PolsiaError(
            E_NOT_FOUND,
            f"no site exists for slug {validated!r}",
            how_to_fix="list the sites (GET /api/v1/sites) and use a slug it returns, or create one",
            context={"slug": validated},
        )
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(meta, dict):
            raise ValueError("meta.json is not a JSON object")
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise PolsiaError(
            E_NOT_FOUND,
            f"site {validated!r} has an unreadable meta.json ({type(exc).__name__})",
            how_to_fix="re-create the site (POST /api/v1/sites with a new slug); the files are intact",
            context={"slug": validated, "path": str(path)},
        ) from exc
    return meta


def read_site(slug: str, *, root: Path | None = None) -> dict[str, Any]:
    """One site's `meta.json` as the wire row; an unknown slug is the named `E_NOT_FOUND`."""
    validated = validate_slug(slug)
    meta = _load_meta(validated, root)
    try:
        return {
            "slug": validated,
            "title": str(meta.get("title") or ""),
            "url": f"/sites/{validated}/",
            "created_at": str(meta.get("created_at") or ""),
            "bytes": int(meta.get("bytes") or 0),
        }
    except (TypeError, ValueError) as exc:
        # A meta.json this module did not write (e.g. `"bytes": "lots"`) is not a site the API can
        # honestly describe: the same named refusal as an unreadable file, never a raw 500.
        raise PolsiaError(
            E_NOT_FOUND,
            f"site {validated!r} has an unreadable meta.json ({type(exc).__name__})",
            how_to_fix="re-create the site (POST /api/v1/sites with a new slug); the files are intact",
            context={"slug": validated, "path": str(_meta_path(validated, _resolve_root(root)))},
        ) from exc


def read_site_meta(slug: str, *, root: Path | None = None) -> dict[str, Any]:
    """The site's full `meta.json` (every key), for consumers the frozen wire row does not carry.

    Read-only: the caller gets a fresh dict and cannot mutate the file through it.
    """
    return _load_meta(slug, root)


def read_deploy_state(slug: str, *, root: Path | None = None) -> dict[str, Any] | None:
    """The site's stored `last_deploy` record, or None when it was never deployed.

    A record that is not a JSON object is reported as ABSENT (None) rather than crashing a read:
    the deploy route must answer "no deploy state" honestly, not 500 on a hand-edited file.
    """
    meta = _load_meta(slug, root)
    record = meta.get("last_deploy")
    return record if isinstance(record, dict) else None


def record_deploy(
    slug: str, record: dict[str, Any], *, root: Path | None = None
) -> dict[str, Any]:
    """Merge `last_deploy` into the site's `meta.json` atomically; returns the record stored.

    The write is temp-file-then-`os.replace` beside the target, so a reader (the static mount, a
    second API worker, the list route) sees either the old document or the new one — never a torn
    file.  Every existing key is preserved: the deploy facts are ADDITIVE, and `meta.json` stays
    the one source of truth for the site.  Note the file lives inside the PUBLIC site directory —
    callers must pass non-secret deploy facts only (the Render key is never written here).

    An unreadable/missing site is `E_NOT_FOUND` (from `_load_meta`); a failed write is `E_STORAGE`
    naming the file, never a silent no-op that would let the API claim a record it did not store.
    """
    meta = _load_meta(slug, root)
    site_dir = _resolve_root(root) / validate_slug(slug)
    meta["last_deploy"] = record
    path = site_dir / "meta.json"
    temp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        temp.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        os.replace(temp, path)
    except OSError as exc:
        with contextlib.suppress(OSError):  # best-effort cleanup of the temp file
            temp.unlink(missing_ok=True)
        raise PolsiaError(
            E_STORAGE,
            f"could not record the deploy state for site {slug!r}: {type(exc).__name__}: {exc}",
            how_to_fix=f"make sure the sites directory is writable ({site_dir}) and retry",
            context={"slug": slug, "path": str(path)},
        ) from exc
    return record


def list_sites(*, slug: str | None = None, root: Path | None = None) -> list[dict[str, Any]]:
    """Every site with a complete `meta.json`, NEWEST first (created_at desc, slug asc).

    Passing `slug` reads exactly that one site (the filtered read) and refuses `E_NOT_FOUND` when
    it is not there.  A directory without a readable `meta.json` is a half-written create, not a
    site: it is skipped by the LIST (it cannot be opened or previewed anyway) and named by the
    single-slug read.
    """
    if slug is not None:
        return [read_site(slug, root=root)]

    sites_root = _resolve_root(root)
    if not sites_root.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for directory in sorted(sites_root.iterdir()):
        if not directory.is_dir() or not (directory / "meta.json").is_file():
            continue
        if not SLUG_PATTERN.fullmatch(directory.name):
            continue
        try:
            rows.append(read_site(directory.name, root=sites_root))
        except PolsiaError:
            continue  # corrupt meta.json: not a site this list can honestly describe
    rows.sort(key=lambda row: row["slug"])
    rows.sort(key=lambda row: row["created_at"], reverse=True)
    return rows


# ── the write ────────────────────────────────────────────────────────────────────────────────
async def generate_site(
    db: AsyncSession,
    *,
    slug: str | None = None,
    title: str | None = None,
    tagline: str | None = None,
    root: Path | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build `<root>/<slug>/` from the company profile; return `{slug,url,bytes,title,created_at}`.

    Refusals by name: `E_CONFIG` when there is no company row (or no name to put on the page),
    `E_VALIDATION` for an invalid slug / over-long title or tagline / a slug that already exists.
    `created_at` is a test seam only; production takes the clock.
    """
    company = await get_company(db)
    if company is None:
        raise PolsiaError(
            E_CONFIG,
            "no company profile exists, so there is nothing to build a site from",
            how_to_fix="create the company profile in settings first (PUT /api/v1/config)",
            context={"missing": "company"},
        )
    fields = _profile_fields(company)
    if not fields["name"]:
        raise PolsiaError(
            E_CONFIG,
            "the company profile has no name, so the page has no author to name",
            how_to_fix="set the company name in settings (PUT /api/v1/config) and retry",
            context={"missing": ["name"]},
        )

    page_title = _clean(title) or fields["name"]
    if len(page_title) > MAX_TITLE:
        raise PolsiaError(
            E_VALIDATION,
            f"site title is {len(page_title)} characters; the limit is {MAX_TITLE}",
            how_to_fix=f"shorten the title to {MAX_TITLE} characters or fewer",
            context={"field": "title", "length": len(page_title), "max": MAX_TITLE},
        )
    page_tagline = _clean(tagline)
    if len(page_tagline) > MAX_TAGLINE:
        raise PolsiaError(
            E_VALIDATION,
            f"site tagline is {len(page_tagline)} characters; the limit is {MAX_TAGLINE}",
            how_to_fix=f"shorten the tagline to {MAX_TAGLINE} characters or fewer",
            context={"field": "tagline", "length": len(page_tagline), "max": MAX_TAGLINE},
        )

    slug_text = (
        validate_slug(slug) if slug is not None else derive_slug(title, fallback=fields["name"])
    )
    sites_root = _resolve_root(root)
    sites_root.mkdir(parents=True, exist_ok=True)
    site_dir = sites_root / slug_text
    if site_dir.exists():
        raise PolsiaError(
            E_VALIDATION,
            f"site slug {slug_text!r} already exists",
            how_to_fix="choose a different slug or title; an existing site is never overwritten",
            context={"slug": slug_text, "reason": "collision"},
        )

    rendered = render_site(
        company,
        title=page_title,
        tagline=page_tagline or None,
        sender_email=fields["contact_email"],
    )
    try:
        site_dir.mkdir(parents=False, exist_ok=False)
    except FileExistsError as exc:  # reached only by a concurrent create of the same slug
        raise PolsiaError(
            E_VALIDATION,
            f"site slug {slug_text!r} already exists",
            how_to_fix="choose a different slug or title; an existing site is never overwritten",
            context={"slug": slug_text, "reason": "collision"},
        ) from exc

    sizes: dict[str, int] = {}
    for filename in _FILES:
        target = site_dir / filename
        target.write_text(rendered[filename], encoding="utf-8", newline="\n")
        sizes[filename] = target.stat().st_size

    stamp = created_at or datetime.now(UTC).isoformat(timespec="seconds")
    total = sum(sizes.values())
    meta = {"slug": slug_text, "title": page_title, "created_at": stamp, "bytes": total}
    (site_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return {
        "slug": slug_text,
        "url": f"/sites/{slug_text}/",
        "bytes": total,
        "title": page_title,
        "created_at": stamp,
    }


__all__ = [
    "SITES_ROOT",
    "SLUG_PATTERN",
    "derive_slug",
    "generate_site",
    "list_sites",
    "read_deploy_state",
    "read_site",
    "read_site_meta",
    "record_deploy",
    "render_site",
    "validate_slug",
]
