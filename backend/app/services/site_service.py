"""The website builder (wave V24, lane L3; multi-page in wave V26, lane L5): generate + host a site.

`generate_site` writes `data/sites/<slug>/{index.html,about.html,contact.html,styles.css,sitemap.xml,
meta.json}` from the company profile and returns the slug/url/byte count; `list_sites`/`read_site`
are the disk-backed reads the API serves.  There is **one source of truth — the filesystem**:
`meta.json` beside each site is what the list is built from, so the API process, a restarted process
and the static mount can never disagree about which sites exist (no database table to drift from
the files).

Wave V26 adds Sites 2.0 on top of the same disk contract:
  * THREE pages — `index.html` (the V24 hero/profile page), `about.html` (description + mission) and
    `contact.html` (the profile's contact details, no form and no POST);
  * three SCRIPT-FREE visual presets — `classic`, `modern`, `bold` — selected by `template`; each
    renders the same three pages against its own generated `styles.css` and carries a
    `t-<preset>` body hook;
  * `meta.json` gains `pages` (always all three), `template`, `custom_domain` (a bare hostname, or
    null) and `seo {title, description, og_image}`; every V24 key is preserved;
  * `sitemap.xml` — one absolute `<loc>` per page, built from the `base_url` the API layer supplies
    (`request.base_url`), XML-escaped, with no fabricated `<lastmod>`;
  * `validate_custom_domain` — a bare hostname only, stored as a RECORD: nothing here resolves DNS
    and no wording may claim verification.

A site created before V26 keeps working unchanged: its `meta.json` simply has none of the new keys,
it still lists and serves `index.html` + `styles.css`, and `about.html`/`contact.html` on it are
real 404s (a page is never fabricated for a site that predates it).  V26 generation always writes
all three pages.

Wave V27 adds the owner-managed lifecycle on the same disk contract:
  * `delete_site` removes `<root>/<slug>/` — the site's files and `meta.json` — and is the named
    `E_NOT_FOUND` when the site is already gone.  The target is RESOLVED and must be a direct child
    of the sites root, so a traversal slug, an absolute path or a directory link pointing elsewhere
    is `E_VALIDATION`; the removal walks with `follow_symlinks=False`, so a link inside a site is
    unlinked, never traversed.
  * `regenerate_site` re-renders the three pages, `styles.css` and (when the API supplies
    `base_url`) `sitemap.xml` from the CURRENT company profile, reusing the site's stored
    `template` / `custom_domain` and keeping its slug, title and `created_at`.  A legacy V24 site
    regenerates to the full V26 shape (`template` defaults to `classic`).  `bytes` is rewritten from
    the files actually written; every other `meta.json` key (e.g. `last_deploy`) is preserved.

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
from collections.abc import Sequence
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

#: The frozen V26 page set — every new generation writes all three, in this order.
PAGES = ("index.html", "about.html", "contact.html")
STYLES_FILE = "styles.css"
SITEMAP_FILE = "sitemap.xml"

#: The frozen template preset ids; `classic` is the default and the V24 look.
TEMPLATES = ("classic", "modern", "bold")
DEFAULT_TEMPLATE = "classic"

#: Every file a V26 site writes.  `sitemap.xml` is skipped when no `base_url` was supplied (its
#: `<loc>` values must be absolute; a path-only sitemap would be a lie about the site's address).
_FILES = (*PAGES, STYLES_FILE, SITEMAP_FILE)

#: Domain bounds (DNS limits): the whole name and each label.
DOMAIN_MAX = 253
DOMAIN_LABEL_MAX = 63
_DOMAIN_LABEL = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?$")



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
        # V26 contact page: the sender identity the profile already carries (Wave V8).  Both are
        # business-card facts — the page is public on purpose and prints only what the owner set.
        "sender_legal_name": _clean(company.sender_legal_name),
        "sender_postal_address": _clean(company.sender_postal_address),
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

/* V26: the cross-page header is CSS-only (no script, no external font). */
.site-header { padding-bottom: 1.5rem; }

.site-nav { display: flex; flex-wrap: wrap; gap: 1rem; margin-top: 0.25rem; }

.site-nav a { color: var(--accent); font-weight: 600; }

.site-nav a[aria-current="page"] { color: var(--ink); text-decoration: none; }

.contact-list { margin: 0; }

.contact-list dt { font-weight: 700; }

.contact-list dd { margin: 0 0 0.75rem; }

.note { color: var(--muted); }

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


#: V26 template treatments.  Each one is APPENDED to the shared stylesheet and scoped to the
#: page's own `body.t-<preset>` hook, so the three presets are three real, script-free looks while
#: the structural CSS (and the V24 look of `classic`) stays one reviewed source.
_TEMPLATE_EXTRAS: dict[str, str] = {
    "classic": "",
    "modern": """
body.t-modern {
  --accent: #0f766e;
  --accent-ink: #ffffff;
  --paper: #fbfefd;
  --muted: #4d635e;
  --line: #cfe3dd;
}
body.t-modern .page { max-width: 72rem; }
body.t-modern .site-header {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 1rem;
  padding-bottom: 1rem;
  border-bottom: 1px solid var(--line);
}
body.t-modern .brand { margin: 0; }
body.t-modern .hero {
  padding: 2rem 1.5rem;
  background: #eef7f4;
  border-bottom: 0;
  border-radius: 1rem;
}
body.t-modern .button { border-radius: 999px; padding-inline: 1.5rem; }
""",
    "bold": """
body.t-bold {
  --accent: #d9471b;
  --accent-ink: #ffffff;
  --paper: #fffdf5;
  --muted: #4a4a4f;
  --line: #1a1a1c;
}
body.t-bold .page { max-width: 58rem; }
body.t-bold h1 { text-transform: uppercase; font-weight: 800; letter-spacing: -0.01em; }
body.t-bold .site-header {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 1rem;
  padding-bottom: 1rem;
  border-bottom: 0.35rem solid var(--accent);
}
body.t-bold .brand { margin: 0; }
body.t-bold .hero { border-bottom: 0; border-top: 0.5rem solid var(--accent); padding-top: 1.5rem; }
body.t-bold .button {
  border-radius: 0;
  text-transform: uppercase;
  letter-spacing: 0.06em;
}
""",
}


def _styles(template: str) -> str:
    """The preset's `styles.css`: the shared sheet plus that preset's scoped treatment."""
    extra = _TEMPLATE_EXTRAS[template]
    return _STYLES + (f"\n{extra}" if extra else "")


def validate_template(template: str | None) -> str:
    """The preset VERBATIM when it is one of the frozen ids, else the named `E_VALIDATION`."""
    value = _clean(template) or DEFAULT_TEMPLATE
    if value not in TEMPLATES:
        raise PolsiaError(
            E_VALIDATION,
            f"site template {template!r} is not one of: {', '.join(TEMPLATES)}",
            how_to_fix=f"choose one of the template presets ({', '.join(TEMPLATES)}); omit it for "
            f"{DEFAULT_TEMPLATE!r}",
            context={"field": "template", "template": template, "allowed": list(TEMPLATES)},
        )
    return value


def validate_custom_domain(domain: str | None) -> str | None:
    """A bare hostname, or None for an absent value; anything else is the named `E_VALIDATION`.

    This is a RECORD, not a DNS check: nothing here resolves the name, proves ownership or claims
    the host is live.  The rules are deliberately structural — labels `[a-zA-Z0-9-]` (no leading or
    trailing dash), dots between labels, no scheme, no path/query/fragment, no port, no userinfo, no
    whitespace, no leading/trailing dot, no empty label, at least two labels, DNS length bounds.
    The value is stored VERBATIM (case included): a validator that silently rewrote the record
    would be reporting a hostname the caller never supplied.
    """
    if domain is None:
        return None
    if not isinstance(domain, str):
        raise PolsiaError(
            E_VALIDATION,
            f"custom domain {domain!r} is not a hostname string",
            how_to_fix="send a bare hostname such as example.com, or null for none",
            context={"field": "custom_domain", "custom_domain": domain, "reason": "not a string"},
        )
    if domain == "":
        return None

    reason = ""
    if len(domain) > DOMAIN_MAX:
        reason = f"longer than {DOMAIN_MAX} characters"
    elif any(character.isspace() for character in domain):
        reason = "whitespace"
    elif "://" in domain or "/" in domain:
        reason = "a scheme or path (send the hostname only)"
    elif any(character in domain for character in "?#:;@[]"):
        reason = "a port, query, fragment or userinfo (send the hostname only)"
    elif domain.startswith(".") or domain.endswith("."):
        reason = "a leading or trailing dot"
    elif ".." in domain:
        reason = "an empty label ('..')"
    else:
        labels = domain.split(".")
        if len(labels) < 2:
            reason = "a single label (a domain needs at least two, e.g. example.com)"
        else:
            for label in labels:
                if not label:
                    reason = "an empty label"
                    break
                if len(label) > DOMAIN_LABEL_MAX:
                    reason = f"a label longer than {DOMAIN_LABEL_MAX} characters"
                    break
                if not _DOMAIN_LABEL.fullmatch(label):
                    reason = (
                        f"label {label!r} outside [a-zA-Z0-9-], or starting/ending with a dash"
                    )
                    break

    if reason:
        raise PolsiaError(
            E_VALIDATION,
            f"custom domain {domain!r} is not a bare hostname ({reason})",
            how_to_fix="enter a bare hostname such as example.com — no scheme, path, port, spaces "
            "or trailing dot; it is recorded, not verified",
            context={"field": "custom_domain", "custom_domain": domain, "reason": reason},
        )
    return domain


def _normalise_base_url(base_url: str | None) -> str:
    """`http(s)://host[:port][/prefix]` with no trailing slash; anything else is refused by name."""
    text = _clean(base_url)
    parsed = urlsplit(text)
    if (
        parsed.scheme.lower() not in ("http", "https")
        or not parsed.netloc
        or parsed.query
        or parsed.fragment
    ):
        raise PolsiaError(
            E_VALIDATION,
            f"site base url {base_url!r} is not an absolute http(s) URL",
            how_to_fix="supply the request's own base URL (http://host/), with no query or fragment",
            context={"field": "base_url", "base_url": base_url},
        )
    return text.rstrip("/")


def render_sitemap(base_url: str, slug: str, pages: Sequence[str] = PAGES) -> str:
    """The site's `sitemap.xml`: one ABSOLUTE `<loc>` per page, XML-escaped, no `<lastmod>`.

    `base_url` is the API layer's `request.base_url` — this module never guesses a public host, so
    the sitemap says exactly where the site is reachable through the caller's own origin.  A
    `<lastmod>` is omitted rather than derived from the clock: a fabricated date is worse than no
    date, and `meta.json` (not the page) owns `created_at`.
    """
    base = _normalise_base_url(base_url)
    validated = validate_slug(slug)
    for page in pages:
        if page not in PAGES:
            raise PolsiaError(
                E_VALIDATION,
                f"sitemap page {page!r} is not one of the generated pages",
                how_to_fix=f"list only {', '.join(PAGES)}",
                context={"field": "pages", "page": page},
            )
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for page in pages:
        path = f"/sites/{validated}/" if page == "index.html" else f"/sites/{validated}/{page}"
        lines += ["  <url>", f"    <loc>{_esc(base + path)}</loc>", "  </url>"]
    lines.append("</urlset>")
    return "\n".join(lines) + "\n"


def build_seo(
    company: CompanyConfig,
    *,
    title: str | None = None,
    tagline: str | None = None,
    og_image: str | None = None,
) -> dict[str, Any]:
    """The `seo` record stored in `meta.json`: `<title>`, description, OG image (or null).

    The description follows the SAME fallback chain as the hero tagline, so the tag the page prints
    and the record it stores cannot disagree.  `og_image` is null unless a caller supplies one: the
    company schema has no logo/image column, and pointing `og:image` at an invented URL would be a
    fabricated asset.
    """
    fields = _profile_fields(company)
    page_title = _clean(title) or fields["name"]
    description = (
        _clean(tagline)
        or _first_line(fields["value_prop"])
        or _first_line(fields["description"])
        or _first_line(fields["mission"])
    )
    return {"title": page_title, "description": description, "og_image": og_image or None}


def _nav(active: str) -> list[str]:
    """The shared CSS-only page nav; `aria-current` marks the page the visitor is on."""
    links = (
        ("index.html", "Home", "index"),
        ("about.html", "About", "about"),
        ("contact.html", "Contact", "contact"),
    )
    parts = ['<nav class="site-nav" aria-label="Pages">']
    for href, label, key in links:
        current = ' aria-current="page"' if key == active else ""
        parts.append(f'<a href="{href}"{current}>{label}</a>')
    parts.append("</nav>")
    return parts


def _cta(contact: str) -> str:
    """One CTA: a `mailto:` link ONLY when an address exists, else the plain line."""
    if contact:
        return f'<p class="cta"><a class="button" href="mailto:{_esc(contact)}">Get in touch</a></p>'
    return '<p class="cta cta-note">Get in touch to learn more.</p>'


def _document(
    *,
    template: str,
    page_title: str,
    description: str,
    og_image: str | None,
    name: str,
    active: str,
    main: list[str],
    website_line: str,
) -> str:
    """One complete HTML document: escaped head, CSS-only header/nav, main, footer. No script."""
    parts: list[str] = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{_esc(page_title)}</title>",
    ]
    if description:
        parts.append(f'<meta name="description" content="{_esc(description)}">')
    parts.append('<meta property="og:type" content="website">')
    parts.append(f'<meta property="og:title" content="{_esc(page_title)}">')
    if description:
        parts.append(f'<meta property="og:description" content="{_esc(description)}">')
    if og_image:
        parts.append(f'<meta property="og:image" content="{_esc(og_image)}">')
    parts += [
        '<link rel="stylesheet" href="styles.css">',
        "</head>",
        f'<body class="t-{template}">',
        '<div class="page">',
        '<header class="site-header">',
    ]
    if name:
        parts.append(f'<p class="brand">{_esc(name)}</p>')
    parts.extend(_nav(active))
    parts.append("</header>")
    parts.extend(main)
    parts.append("<footer>")
    if name:
        parts.append(f"<p>{_esc(name)}</p>")
    if website_line:
        parts.append(website_line)
    parts.append("</footer>")
    parts += ["</div>", "</body>", "</html>", ""]
    return "\n".join(parts)


def render_pages(
    company: CompanyConfig,
    *,
    title: str | None = None,
    tagline: str | None = None,
    sender_email: str | None = None,
    template: str = DEFAULT_TEMPLATE,
    slug: str | None = None,
    base_url: str | None = None,
    og_image: str | None = None,
) -> dict[str, str]:
    """`{filename: content}` for a V26 site — pure, deterministic, every value escaped.

    Always the three pages + `styles.css`; `sitemap.xml` too when `base_url` (and a slug, which the
    caller already validated for the directory) is supplied.  `sender_email` overrides the profile's
    contact address (pass `""` to force the no-address variant); when it is `None` the company
    profile's own contact field is used.
    """
    chosen = validate_template(template)
    fields = _profile_fields(company)
    name = fields["name"]

    page_title = _clean(title) or name
    hero_tagline = (
        _clean(tagline)
        or _first_line(fields["value_prop"])
        or _first_line(fields["description"])
        or _first_line(fields["mission"])
    )
    # The index's about section carries the profile prose the hero did NOT already say: the value
    # proposition, description and mission, de-duplicated and with any sentence equal to the
    # tagline dropped — a page that repeats itself reads generated.  When the profile has no prose
    # at all the section is OMITTED, never filled with a placeholder this generator invented.
    about_paragraphs: list[str] = []
    for candidate in (fields["value_prop"], fields["description"], fields["mission"]):
        if candidate and candidate != hero_tagline and candidate not in about_paragraphs:
            about_paragraphs.append(candidate)
    mission = fields["mission"]
    target_market = fields["target_market"]
    contact = _clean(sender_email) if sender_email is not None else fields["contact_email"]
    website_line = _website_line(fields["website_url"])
    seo = build_seo(company, title=page_title, tagline=tagline, og_image=og_image)
    description = str(seo["description"])

    index_main: list[str] = ['<main>', '<section class="hero">', f"<h1>{_esc(page_title)}</h1>"]
    if hero_tagline:
        index_main.append(f'<p class="tagline">{_esc(hero_tagline)}</p>')
    index_main.append(_cta(contact))
    index_main.append("</section>")
    if about_paragraphs:
        index_main += ['<section id="about">', "<h2>About</h2>"]
        index_main += [f"<p>{_esc(paragraph)}</p>" for paragraph in about_paragraphs]
        index_main.append("</section>")
    if target_market:
        index_main += [
            '<section id="who-we-serve">',
            "<h2>Who we serve</h2>",
            f"<p>{_esc(target_market)}</p>",
            "</section>",
        ]
    index_main.append("</main>")

    about_title = f"About {name}" if name else "About"
    about_description = (
        _first_line(fields["description"]) or _first_line(fields["mission"]) or description
    )
    about_main: list[str] = ["<main>", '<section id="about">', "<h2>About</h2>"]
    about_prose = [
        paragraph
        for paragraph in (fields["value_prop"], fields["description"])
        if paragraph and paragraph != hero_tagline
    ]
    if about_prose:
        about_main += [f"<p>{_esc(paragraph)}</p>" for paragraph in about_prose]
    else:
        about_main.append(
            '<p class="note">A company description has not been written yet.</p>'
        )
    about_main.append("</section>")
    if mission and mission not in about_prose:
        about_main += [
            '<section id="mission">',
            "<h2>Mission</h2>",
            f"<p>{_esc(mission)}</p>",
            "</section>",
        ]
    if target_market:
        about_main += [
            '<section id="who-we-serve">',
            "<h2>Who we serve</h2>",
            f"<p>{_esc(target_market)}</p>",
            "</section>",
        ]
    about_main.append("</main>")

    contact_title = f"Contact {name}" if name else "Contact"
    contact_description = f"Contact details for {name}." if name else "Contact details."
    contact_main: list[str] = ["<main>", '<section id="contact">', "<h2>Contact</h2>"]
    details: list[tuple[str, str]] = []
    if contact:
        details.append(("Email", f'<a href="mailto:{_esc(contact)}">{_esc(contact)}</a>'))
    if fields["website_url"]:
        website = fields["website_url"]
        if urlsplit(website).scheme.lower() in ("http", "https"):
            details.append(
                ("Website", f'<a href="{_esc(website)}" rel="noopener">{_esc(website)}</a>')
            )
        else:
            details.append(("Website", _esc(website)))
    if fields["sender_postal_address"]:
        details.append(("Address", _esc(fields["sender_postal_address"])))
    if fields["sender_legal_name"]:
        details.append(("Legal name", _esc(fields["sender_legal_name"])))
    if details:
        contact_main.append('<dl class="contact-list">')
        for label, value in details:
            contact_main.append(f"<dt>{label}</dt>")
            contact_main.append(f"<dd>{value}</dd>")
        contact_main.append("</dl>")
        note = (
            "This page has no form: write to the address above."
            if contact
            else "This page has no form."
        )
        contact_main.append(f'<p class="note">{note}</p>')
    else:
        contact_main.append(
            '<p class="note">No contact details have been added to the company profile yet.</p>'
        )
    contact_main.append("</section>")
    contact_main.append("</main>")

    files = {
        "index.html": _document(
            template=chosen,
            page_title=page_title,
            description=description,
            og_image=seo["og_image"],
            name=name,
            active="index",
            main=index_main,
            website_line=website_line,
        ),
        "about.html": _document(
            template=chosen,
            page_title=about_title,
            description=about_description,
            og_image=seo["og_image"],
            name=name,
            active="about",
            main=about_main,
            website_line=website_line,
        ),
        "contact.html": _document(
            template=chosen,
            page_title=contact_title,
            description=contact_description,
            og_image=seo["og_image"],
            name=name,
            active="contact",
            main=contact_main,
            website_line=website_line,
        ),
        STYLES_FILE: _styles(chosen),
    }
    if base_url is not None:
        if slug is None:
            raise PolsiaError(
                E_VALIDATION,
                "a sitemap needs the site's slug",
                how_to_fix="pass the validated slug alongside base_url",
                context={"field": "slug"},
            )
        files[SITEMAP_FILE] = render_sitemap(base_url, slug, PAGES)
    return files


def render_site(
    company: CompanyConfig,
    *,
    title: str | None = None,
    tagline: str | None = None,
    sender_email: str | None = None,
) -> dict[str, str]:
    """The V24 single-page pair — kept for its existing callers and its `{index, styles}` contract.

    V26 generation calls `render_pages`; this wrapper renders the same classic index page so the two
    cannot drift, and deliberately does NOT return the extra pages (a caller of the old API asked
    for one page and gets one page).
    """
    files = render_pages(company, title=title, tagline=tagline, sender_email=sender_email)
    return {"index.html": files["index.html"], STYLES_FILE: files[STYLES_FILE]}


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
    """One site's `meta.json` as the wire row; an unknown slug is the named `E_NOT_FOUND`.

    V26 sites also report `pages`, `template` and `custom_domain`.  Those keys are ADDED ONLY WHEN
    THE META FILE CARRIES THEM: a V24 `meta.json` has no such keys, and the row for it stays exactly
    the five frozen V24 keys instead of inventing `pages` for a site that has one page.
    """
    validated = validate_slug(slug)
    meta = _load_meta(validated, root)
    try:
        row: dict[str, Any] = {
            "slug": validated,
            "title": str(meta.get("title") or ""),
            "url": f"/sites/{validated}/",
            "created_at": str(meta.get("created_at") or ""),
            "bytes": int(meta.get("bytes") or 0),
        }
        pages = meta.get("pages")
        if isinstance(pages, list) and pages and all(isinstance(page, str) for page in pages):
            row["pages"] = list(pages)
        template = meta.get("template")
        if isinstance(template, str) and template in TEMPLATES:
            row["template"] = template
        if "custom_domain" in meta:
            domain = meta.get("custom_domain")
            row["custom_domain"] = domain if isinstance(domain, str) and domain else None
        return row
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


# ── lifecycle: delete + regenerate (wave V27) ───────────────────────────────────────────────
def _resolve_site_directory(slug: str, *, root: Path | None) -> tuple[str, Path]:
    """The site's own directory, PROVEN to be a direct child of the sites root.

    `validate_slug` already refuses traversal (`..`), absolutes and path separators by the frozen
    `[a-z0-9-]{2,48}` contract.  This adds the filesystem half: a directory whose RESOLVED path is
    not `<root>/<slug>` is refused by name — so a symbolic link (or an NTFS junction) planted under
    the sites root can never aim a delete or a rewrite at anything outside it.  The unresolved path
    is returned, so callers act on the site's own name rather than on a resolved target.
    """
    validated = validate_slug(slug)
    sites_root = _resolve_root(root)
    directory = sites_root / validated
    if directory.is_symlink():
        raise PolsiaError(
            E_VALIDATION,
            f"site directory for slug {validated!r} is a symbolic link; refusing to follow it",
            how_to_fix="remove the link and let Polsia own the site directory, or pick another slug",
            context={"slug": validated, "path": str(directory), "reason": "symlink"},
        )
    try:
        resolved_root = sites_root.resolve()
        resolved = directory.resolve()
    except OSError as exc:
        raise PolsiaError(
            E_VALIDATION,
            f"site path for slug {validated!r} could not be resolved ({type(exc).__name__})",
            how_to_fix="check the sites directory and retry",
            context={"slug": validated, "path": str(directory)},
        ) from exc
    if resolved.parent != resolved_root or resolved.name != validated:
        raise PolsiaError(
            E_VALIDATION,
            f"site path for slug {validated!r} resolves outside the sites directory",
            how_to_fix="use a slug the sites directory owns; a site path is never followed elsewhere",
            context={"slug": validated, "path": str(directory), "resolved": str(resolved)},
        )
    return validated, directory


def _remove_tree(directory: Path) -> None:
    """Remove `directory` WITHOUT ever following a link (symbolic link or NTFS junction).

    `os.scandir` is asked `follow_symlinks=False`; a link is removed as a link (`unlink` for a
    symlink, `rmdir` for a junction) and only a real directory is recursed into.  So a link planted
    inside a site cannot turn a delete into a delete of its target.
    """
    for entry in os.scandir(directory):
        is_junction = getattr(entry, "is_junction", None)
        if entry.is_symlink():
            os.unlink(entry.path)
        elif callable(is_junction) and is_junction():
            os.rmdir(entry.path)  # removes the reparse point, never the target
        elif entry.is_dir(follow_symlinks=False):
            _remove_tree(Path(entry.path))
        else:
            os.unlink(entry.path)
    os.rmdir(directory)


def _write_text_atomic(path: Path, content: str) -> int:
    """Write `content` temp-file-then-`os.replace`; return the REAL byte count written.

    A reader (the static mount, a second worker) sees the old file or the new one, never a torn
    one.  A failed write is `E_STORAGE` naming the file — never a silent no-op that would let the
    route claim a rebuild it did not perform.
    """
    temp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        temp.write_text(content, encoding="utf-8", newline="\n")
        os.replace(temp, path)
    except OSError as exc:
        with contextlib.suppress(OSError):  # best-effort cleanup of the temp file
            temp.unlink(missing_ok=True)
        raise PolsiaError(
            E_STORAGE,
            f"could not write the site file {path.name!r}: {type(exc).__name__}: {exc}",
            how_to_fix=f"make sure the site directory is writable ({path.parent}) and retry",
            context={"path": str(path)},
        ) from exc
    return path.stat().st_size


def delete_site(slug: str, *, root: Path | None = None) -> dict[str, Any]:
    """Remove one site's directory (files + `meta.json`); `{slug, deleted: true}` on success.

    Refusals by name: `E_VALIDATION` for a slug that is not a plain site name or whose directory
    resolves outside the sites root (see `_resolve_site_directory`), `E_NOT_FOUND` when the site is
    already gone (no readable `meta.json`), `E_STORAGE` when the filesystem refuses the removal.
    The DELETE is the site's whole record: `meta.json` lives inside the directory, so a removed
    directory is a removed site in the list, the static mount and every reader at once.
    """
    validated, directory = _resolve_site_directory(slug, root=root)
    if not (directory / "meta.json").is_file():
        raise PolsiaError(
            E_NOT_FOUND,
            f"no site exists for slug {validated!r}",
            how_to_fix="list the sites (GET /api/v1/sites) and use a slug it returns",
            context={"slug": validated},
        )
    try:
        _remove_tree(directory)
    except OSError as exc:
        raise PolsiaError(
            E_STORAGE,
            f"could not delete site {validated!r}: {type(exc).__name__}: {exc}",
            how_to_fix=f"make sure the site directory is writable ({directory}) and retry",
            context={"slug": validated, "path": str(directory)},
        ) from exc
    return {"slug": validated, "deleted": True}


async def regenerate_site(
    db: AsyncSession,
    slug: str,
    *,
    root: Path | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    """Re-render one site from the CURRENT company profile; return its refreshed record.

    The site's stored `template` (else `classic`, which is what a legacy site was built as) and
    `custom_domain` are reused, and its slug, title and `created_at` are kept — a rebuild is not a
    rename or a re-creation.  Every generated file is rewritten atomically; `bytes` is the REAL
    total of the files written, and the stored `meta.json` keys this module does not own (e.g.
    `last_deploy`) are preserved.

    Refusals by name: `E_CONFIG` with no company row/name, `E_NOT_FOUND` for an unknown slug,
    `E_VALIDATION` for a slug/path outside the sites root or a stored template/domain that is no
    longer valid, `E_STORAGE` when a file cannot be written.
    """
    company = await get_company(db)
    if company is None:
        raise PolsiaError(
            E_CONFIG,
            "no company profile exists, so there is nothing to rebuild a site from",
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

    validated, site_dir = _resolve_site_directory(slug, root=root)
    meta = _load_meta(validated, root)
    stored_template = meta.get("template")
    template = validate_template(stored_template if isinstance(stored_template, str) else None)
    stored_domain = meta.get("custom_domain")
    domain = validate_custom_domain(stored_domain if isinstance(stored_domain, str) else None)
    title = _clean(meta.get("title")) or fields["name"]

    rendered = render_pages(
        company,
        title=title,
        tagline=None,
        sender_email=fields["contact_email"],
        template=template,
        slug=validated,
        base_url=base_url,
    )
    sizes = {
        filename: _write_text_atomic(site_dir / filename, content)
        for filename, content in rendered.items()
    }
    total = sum(sizes.values())

    refreshed = dict(meta)
    refreshed.update(
        {
            "slug": validated,
            "title": title,
            "bytes": total,
            "pages": list(PAGES),
            "template": template,
            "custom_domain": domain,
            "seo": build_seo(company, title=title, tagline=None),
        }
    )
    _write_text_atomic(
        site_dir / "meta.json",
        json.dumps(refreshed, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    return {
        "slug": validated,
        "url": f"/sites/{validated}/",
        "bytes": total,
        "title": title,
        "created_at": str(refreshed.get("created_at") or ""),
        "pages": list(PAGES),
        "template": template,
        "custom_domain": domain,
    }


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
    template: str | None = None,
    custom_domain: str | None = None,
    root: Path | None = None,
    base_url: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build `<root>/<slug>/` from the company profile; return `{slug,url,bytes,title,created_at}`.

    V26: the directory holds the three pages, one preset stylesheet and — when `base_url` (the API
    layer's `request.base_url`) is supplied — a `sitemap.xml`; `meta.json` records `pages`,
    `template`, `custom_domain` and `seo` beside every V24 key, and `bytes` is the REAL total of the
    files written.

    Refusals by name: `E_CONFIG` when there is no company row (or no name to put on the page),
    `E_VALIDATION` for an invalid slug / over-long title or tagline / unknown template preset /
    malformed custom domain / a slug that already exists.  `custom_domain` is RECORDED, never
    DNS-verified.  `created_at` is a test seam only; production takes the clock.
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

    chosen_template = validate_template(template)
    domain = validate_custom_domain(custom_domain)

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

    rendered = render_pages(
        company,
        title=page_title,
        tagline=page_tagline or None,
        sender_email=fields["contact_email"],
        template=chosen_template,
        slug=slug_text,
        base_url=base_url,
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
        content = rendered.get(filename)
        if content is None:
            continue  # no sitemap without a base_url: a path-only <loc> would be a lie
        target = site_dir / filename
        target.write_text(content, encoding="utf-8", newline="\n")
        sizes[filename] = target.stat().st_size

    stamp = created_at or datetime.now(UTC).isoformat(timespec="seconds")
    total = sum(sizes.values())
    meta = {
        "slug": slug_text,
        "title": page_title,
        "created_at": stamp,
        "bytes": total,
        # ── V26 (frozen keys) ──────────────────────────────────────────────────────────────
        "pages": list(PAGES),
        "template": chosen_template,
        "custom_domain": domain,  # recorded, NOT DNS-verified
        "seo": build_seo(company, title=page_title, tagline=page_tagline or None),
    }
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
    "DEFAULT_TEMPLATE",
    "PAGES",
    "SITES_ROOT",
    "SITEMAP_FILE",
    "SLUG_PATTERN",
    "STYLES_FILE",
    "TEMPLATES",
    "build_seo",
    "delete_site",
    "derive_slug",
    "generate_site",
    "list_sites",
    "read_deploy_state",
    "read_site",
    "read_site_meta",
    "record_deploy",
    "regenerate_site",
    "render_pages",
    "render_site",
    "render_sitemap",
    "validate_custom_domain",
    "validate_slug",
    "validate_template",
]
