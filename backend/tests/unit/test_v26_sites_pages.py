"""Wave V26 / lane L5 — Sites 2.0: three pages, three presets, custom domain, sitemap.

RED leg (raw output in `wave-v26/l5-sites/raw/red-backend.txt`): this file was written BEFORE
`render_pages` / `render_sitemap` / `validate_custom_domain` / `TEMPLATES` existed, so the suite
failed on those names — the honest RED state for a new capability.

What each group proves (the brief's frozen contract):
  * `render_pages` renders `index.html`, `about.html`, `contact.html`, one `styles.css` and — when
    the API supplies a `base_url` — `sitemap.xml`; the output is deterministic.
  * The three presets (`classic`, `modern`, `bold`) render the SAME three pages with a DIFFERENT
    stylesheet and a `t-<preset>` body hook; an unknown preset is the named `E_VALIDATION`.
  * Every interpolated value is escaped: `<script>alert("x")</script>` in the company name (and the
    title, tagline, description, mission, target market, contact address) is inert markup in all
    three pages and absent from a well-formed sitemap. No page carries a form or a POST.
  * `contact.html` carries the profile's contact details (mailto + business address + website) and
    no form; `about.html` carries description/mission.
  * `sitemap.xml` lists one ABSOLUTE `<loc>` per page, XML-escaped, no fabricated `<lastmod>`; a
    non-http(s) base is refused by name.
  * `validate_custom_domain` accepts bare hostnames only and refuses scheme/path/port/space/`..`/
    empty-label/trailing-dot values by name.
  * `meta.json` gains `pages`/`template`/`custom_domain`/`seo` and keeps every V24 key; `bytes` is
    the real total of every file written.
  * `POST /api/v1/sites` accepts `template` + `custom_domain`; the list row carries
    `pages`/`template`/`custom_domain` for a V26 site and stays exactly five keys for a V24 site;
    a legacy site still serves `index.html` + `styles.css` and its `about.html` is a REAL 404.
  * Everything is served through the real ASGI app's `StaticFiles` mount at `/sites`.
"""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.testclient import TestClient

from app.core.errors import E_NOT_FOUND, E_VALIDATION, PolsiaError
from app.models.company import CompanyConfig
from app.services import site_service

HOSTILE = '<script>alert("x")</script>'
PAGES = ("index.html", "about.html", "contact.html")


# ── profile fixture ─────────────────────────────────────────────────────────────────────────
def _company(**overrides) -> CompanyConfig:
    values = {
        "name": "Acme Robotics",
        "value_prop": "Autonomous warehouse robots for small factories.",
        "target_market": "Small and mid-size manufacturers in the EU.",
        "description": "We build robots that do the boring lifting.",
        "mission": "Make automation affordable.",
        "website_url": "https://acme.example/robots",
        "notification_email": "",
        "sender_legal_name": "Acme Robotics AB",
        "sender_postal_address": "1 Main Street, Berlin",
    }
    values.update(overrides)
    return CompanyConfig(**values)


def _pages(company: CompanyConfig | None = None, **kwargs) -> dict[str, str]:
    kwargs.setdefault("slug", "acme-robots")
    kwargs.setdefault("base_url", "http://127.0.0.1:8000/")
    return site_service.render_pages(company or _company(), **kwargs)


def _locs(sitemap: str) -> list[str]:
    root = ET.fromstring(sitemap)
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    return [element.text or "" for element in root.findall("sm:url/sm:loc", ns)]


# ── 1. the three pages + sitemap ───────────────────────────────────────────────────────────
def test_render_pages_returns_three_pages_one_stylesheet_and_a_sitemap() -> None:
    files = _pages()
    assert set(files) == {*PAGES, "styles.css", "sitemap.xml"}
    for name in PAGES:
        document = files[name]
        assert document.lstrip().lower().startswith("<!doctype html>"), name
        assert "<script" not in document.lower(), name
        assert 'href="styles.css"' in document, name
        # Every page links the other two: the site is navigable without JS.
        for target in PAGES:
            assert f'href="{target}"' in document, (name, target)
        assert "<main" in document and "<footer" in document, name
        assert 'name="viewport"' in document, name
    css = files["styles.css"]
    assert "@import" not in css and "url(" not in css.lower()
    assert "system-ui" in css


def test_render_pages_is_deterministic() -> None:
    assert _pages() == _pages()


def test_each_template_preset_renders_the_same_pages_with_its_own_treatment() -> None:
    assert tuple(site_service.TEMPLATES) == ("classic", "modern", "bold")
    stylesheets = set()
    for template in site_service.TEMPLATES:
        files = _pages(template=template)
        assert set(files) == {*PAGES, "styles.css", "sitemap.xml"}
        for name in PAGES:
            assert f'<body class="t-{template}">' in files[name], (template, name)
        stylesheets.add(files["styles.css"])
    # Three presets, three visually distinct stylesheets (not one string with a renamed class).
    assert len(stylesheets) == 3


def test_an_unknown_template_is_the_named_validation_refusal() -> None:
    with pytest.raises(PolsiaError) as excinfo:
        site_service.validate_template("neon")
    assert excinfo.value.code == E_VALIDATION
    assert excinfo.value.context["field"] == "template"
    assert excinfo.value.context["template"] == "neon"
    with pytest.raises(PolsiaError):
        _pages(template="neon")


def test_the_default_template_is_classic() -> None:
    assert site_service.DEFAULT_TEMPLATE == "classic"
    assert _pages()["index.html"] == _pages(template="classic")["index.html"]


# ── 2. escaping: hostile profile values are inert everywhere ─────────────────────────────────
def test_a_script_tag_in_the_company_name_is_inert_in_all_three_pages() -> None:
    files = _pages(
        _company(
            name=HOSTILE,
            value_prop=HOSTILE,
            description=HOSTILE,
            mission=HOSTILE,
            target_market=HOSTILE,
            notification_email='x" onclick="alert(1)',
        ),
        title=HOSTILE,
        tagline=HOSTILE,
    )
    for name in PAGES:
        document = files[name]
        assert "<script" not in document.lower(), name
        assert 'alert("x")' not in document, name
        assert "&lt;script&gt;" in document, name
        assert "onclick=\"alert(1)\"" not in document, name
        assert "&quot;" in document, name  # the attribute-breaking quote was escaped
    # The sitemap interpolates no profile text: it is script-free and well-formed XML.
    sitemap = files["sitemap.xml"]
    assert "<script" not in sitemap.lower()
    assert ET.fromstring(sitemap) is not None


def test_the_seo_title_and_description_are_escaped_too() -> None:
    files = _pages(_company(), title=HOSTILE, tagline=HOSTILE)
    index = files["index.html"]
    assert f"<title>{HOSTILE}</title>" not in index
    assert "&lt;script&gt;" in index
    assert 'property="og:title" content="&lt;script&gt;' in index
    assert 'property="og:description" content="&lt;script&gt;' in index


def test_no_page_carries_a_form_or_a_post() -> None:
    files = _pages()
    for name in PAGES:
        lowered = files[name].lower()
        assert "<form" not in lowered, name
        assert 'method="post"' not in lowered, name
        assert "<script" not in lowered, name


# ── 3. about + contact content from the profile ─────────────────────────────────────────────
def test_about_carries_the_company_description_and_mission() -> None:
    about = _pages()["about.html"]
    assert "We build robots that do the boring lifting." in about
    assert "Make automation affordable." in about
    assert "Small and mid-size manufacturers in the EU." in about


def test_contact_carries_the_profile_contact_details_and_no_form() -> None:
    contact = _pages(
        _company(notification_email="hello@acme.example")
    )["contact.html"]
    assert 'href="mailto:hello@acme.example"' in contact
    assert "hello@acme.example" in contact
    assert "1 Main Street, Berlin" in contact
    assert 'href="https://acme.example/robots"' in contact
    assert "<form" not in contact.lower()


def test_contact_invents_no_address_when_the_profile_has_none() -> None:
    contact = _pages(
        _company(
            notification_email="",
            sender_postal_address="",
            sender_legal_name="",
            website_url="",
        )
    )["contact.html"]
    assert "mailto:" not in contact
    assert "@" not in contact  # no invented address, no bare "@"
    assert "<form" not in contact.lower()
    assert "contact details" in contact.lower()


# ── 4. the sitemap ──────────────────────────────────────────────────────────────────────────
def test_the_sitemap_lists_one_absolute_url_per_page_without_a_fabricated_lastmod() -> None:
    sitemap = site_service.render_sitemap(
        "http://127.0.0.1:8000/", "acme-robots", site_service.PAGES
    )
    assert _locs(sitemap) == [
        "http://127.0.0.1:8000/sites/acme-robots/",
        "http://127.0.0.1:8000/sites/acme-robots/about.html",
        "http://127.0.0.1:8000/sites/acme-robots/contact.html",
    ]
    assert "<lastmod>" not in sitemap
    assert sitemap.startswith("<?xml")


def test_the_sitemap_is_xml_escaped() -> None:
    sitemap = site_service.render_sitemap("http://probe.test/a&b/", "acme", site_service.PAGES)
    assert "&amp;" in sitemap
    assert _locs(sitemap)[0] == "http://probe.test/a&b/sites/acme/"


def test_a_non_http_base_url_is_refused_by_name() -> None:
    with pytest.raises(PolsiaError) as excinfo:
        site_service.render_sitemap("ftp://probe/", "acme", site_service.PAGES)
    assert excinfo.value.code == E_VALIDATION
    assert excinfo.value.context["field"] == "base_url"


# ── 5. custom domain: a record, hostname only ───────────────────────────────────────────────
@pytest.mark.parametrize(
    "domain", ["example.com", "sub.example.co.uk", "a-b.example.com", "EXAMPLE.com", "xn--bcher-kva.example"]
)
def test_a_bare_hostname_is_accepted_verbatim(domain: str) -> None:
    assert site_service.validate_custom_domain(domain) == domain


@pytest.mark.parametrize(
    "domain",
    [
        "http://x/y",
        "https://example.com",
        "example.com/path",
        "example.com?x=1",
        "example.com#frag",
        "example.com:8080",
        " example.com",
        "example.com ",
        "exa mple.com",
        "example..com",
        ".example.com",
        "example.com.",
        "-bad.example.com",
        "bad-.example.com",
        "user@example.com",
        "x",
    ],
)
def test_a_malformed_domain_is_the_named_validation_refusal(domain: str) -> None:
    with pytest.raises(PolsiaError) as excinfo:
        site_service.validate_custom_domain(domain)
    assert excinfo.value.code == E_VALIDATION
    assert excinfo.value.context["field"] == "custom_domain"


def test_an_absent_domain_is_none_not_an_empty_string() -> None:
    assert site_service.validate_custom_domain(None) is None


@pytest.mark.parametrize(
    "domain",
    ["a" * 64 + ".com", ("a" * 49 + ".") * 6 + "com"],
)
def test_the_domain_length_bounds_are_enforced(domain: str) -> None:
    with pytest.raises(PolsiaError) as excinfo:
        site_service.validate_custom_domain(domain)
    assert excinfo.value.code == E_VALIDATION


# ── 6. generate_site: the V26 meta, bytes, backward-compatible render_site ──────────────────
@pytest.mark.asyncio
async def test_generate_site_writes_three_pages_a_sitemap_and_the_v26_meta(
    async_db_session, tmp_path: Path
) -> None:
    async_db_session.add(_company(notification_email="hello@acme.example"))
    await async_db_session.commit()

    result = await site_service.generate_site(
        async_db_session,
        title="Acme Site",
        template="bold",
        custom_domain="shop.example.com",
        root=tmp_path,
        base_url="http://127.0.0.1:8000/",
        created_at="2026-10-19T12:00:00+00:00",
    )
    site_dir = tmp_path / "acme-site"
    assert sorted(entry.name for entry in site_dir.iterdir()) == [
        "about.html",
        "contact.html",
        "index.html",
        "meta.json",
        "sitemap.xml",
        "styles.css",
    ]
    meta = json.loads((site_dir / "meta.json").read_text(encoding="utf-8"))
    # Frozen V26 keys, exact shapes.
    assert meta["pages"] == ["index.html", "about.html", "contact.html"]
    assert meta["template"] == "bold"
    assert meta["custom_domain"] == "shop.example.com"
    assert set(meta["seo"]) == {"title", "description", "og_image"}
    assert meta["seo"]["title"] == "Acme Site"
    assert meta["seo"]["description"] == "Autonomous warehouse robots for small factories."
    assert meta["seo"]["og_image"] is None
    # Every V24 key is still there, with the same meaning.
    assert meta["slug"] == "acme-site"
    assert meta["title"] == "Acme Site"
    assert meta["created_at"] == "2026-10-19T12:00:00+00:00"
    written = sum(
        entry.stat().st_size for entry in site_dir.iterdir() if entry.name != "meta.json"
    )
    assert meta["bytes"] == written
    assert result["bytes"] == written
    # meta.json lives inside the PUBLIC site directory: no contact address in it.
    assert "hello@acme.example" not in json.dumps(meta)
    # The sitemap is built from the API-supplied base_url.
    assert _locs((site_dir / "sitemap.xml").read_text(encoding="utf-8"))[0] == (
        "http://127.0.0.1:8000/sites/acme-site/"
    )


@pytest.mark.asyncio
async def test_generate_site_defaults_to_classic_and_a_null_domain(
    async_db_session, tmp_path: Path
) -> None:
    async_db_session.add(_company())
    await async_db_session.commit()
    await site_service.generate_site(
        async_db_session, title="Plain Site", root=tmp_path, base_url="http://test/"
    )
    meta = json.loads((tmp_path / "plain-site" / "meta.json").read_text(encoding="utf-8"))
    assert meta["template"] == "classic"
    assert meta["custom_domain"] is None
    assert meta["pages"] == ["index.html", "about.html", "contact.html"]


@pytest.mark.asyncio
async def test_generate_site_refuses_a_malformed_domain_by_name(
    async_db_session, tmp_path: Path
) -> None:
    async_db_session.add(_company())
    await async_db_session.commit()
    with pytest.raises(PolsiaError) as excinfo:
        await site_service.generate_site(
            async_db_session, title="Bad Domain", custom_domain="http://x/y", root=tmp_path
        )
    assert excinfo.value.code == E_VALIDATION
    assert excinfo.value.context["field"] == "custom_domain"
    assert not (tmp_path / "bad-domain").exists()


def test_render_site_keeps_its_two_file_v24_contract() -> None:
    # V24 call sites still get exactly the single-page pair; V26 generation calls render_pages.
    files = site_service.render_site(_company(), title="Acme Robotics")
    assert set(files) == {"index.html", "styles.css"}


# ── 7. the HTTP surface ─────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_post_sites_accepts_template_and_custom_domain_and_records_them(
    api_client, async_db_session, auth_headers, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(site_service, "SITES_ROOT", tmp_path)
    async_db_session.add(_company())
    await async_db_session.commit()

    resp = await api_client.post(
        "/api/v1/sites",
        json={"title": "Bold probe", "template": "bold", "custom_domain": "shop.example.com"},
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    # The frozen 201 body does not change: {slug, url, bytes}.
    assert set(resp.json()) == {"slug", "url", "bytes"}
    site_dir = tmp_path / "bold-probe"
    meta = json.loads((site_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["template"] == "bold"
    assert meta["custom_domain"] == "shop.example.com"
    assert meta["pages"] == ["index.html", "about.html", "contact.html"]
    # request.base_url reached the sitemap.
    sitemap = (site_dir / "sitemap.xml").read_text(encoding="utf-8")
    assert "http://test/sites/bold-probe/about.html" in sitemap

    rows = (await api_client.get("/api/v1/sites", headers=auth_headers)).json()
    assert len(rows) == 1
    assert rows[0]["pages"] == ["index.html", "about.html", "contact.html"]
    assert rows[0]["template"] == "bold"
    assert rows[0]["custom_domain"] == "shop.example.com"


@pytest.mark.asyncio
async def test_a_legacy_list_row_stays_exactly_the_five_v24_keys(
    api_client, auth_headers, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(site_service, "SITES_ROOT", tmp_path)
    _write_legacy_site(tmp_path, "legacy-site", "2026-01-01T00:00:00+00:00")
    rows = (await api_client.get("/api/v1/sites", headers=auth_headers)).json()
    assert rows[0] == {
        "slug": "legacy-site",
        "title": "Legacy",
        "url": "/sites/legacy-site/",
        "created_at": "2026-01-01T00:00:00+00:00",
        "bytes": 34,
    }
    assert set(rows[0]) == {"slug", "title", "url", "created_at", "bytes"}


@pytest.mark.asyncio
async def test_post_sites_refuses_a_malformed_domain_and_an_unknown_template(
    api_client, async_db_session, auth_headers, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(site_service, "SITES_ROOT", tmp_path)
    async_db_session.add(_company())
    await async_db_session.commit()

    bad_domain = await api_client.post(
        "/api/v1/sites",
        json={"title": "Nope", "custom_domain": "http://x/y"},
        headers=auth_headers,
    )
    assert bad_domain.status_code == 422, bad_domain.text
    assert bad_domain.json()["error"]["code"] == E_VALIDATION
    assert bad_domain.json()["error"]["context"]["field"] == "custom_domain"
    assert not (tmp_path / "nope").exists()

    bad_template = await api_client.post(
        "/api/v1/sites",
        json={"title": "Nope", "template": "neon"},
        headers=auth_headers,
    )
    assert bad_template.status_code == 422, bad_template.text
    assert bad_template.json()["error"]["code"] == E_VALIDATION
    assert bad_template.json()["error"]["context"]["field"] == "template"


# ── 8. served through the REAL ASGI app ─────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_the_static_mount_serves_three_pages_and_the_sitemap(
    async_db_session, tmp_path: Path, monkeypatch
) -> None:
    from app.main import create_app

    monkeypatch.setattr(site_service, "SITES_ROOT", tmp_path)
    async_db_session.add(_company())
    await async_db_session.commit()
    await site_service.generate_site(
        async_db_session, title="Served Probe", root=tmp_path, base_url="http://test"
    )

    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for path in (
            "/sites/served-probe/",
            "/sites/served-probe/index.html",
            "/sites/served-probe/about.html",
            "/sites/served-probe/contact.html",
        ):
            resp = await client.get(path)
            assert resp.status_code == 200, (path, resp.status_code)
            assert resp.headers["content-type"].startswith("text/html"), path
        css = await client.get("/sites/served-probe/styles.css")
        assert css.status_code == 200
        assert css.headers["content-type"].startswith("text/css")
        sitemap = await client.get("/sites/served-probe/sitemap.xml")
        assert sitemap.status_code == 200
        assert "xml" in sitemap.headers["content-type"]
        assert "http://test/sites/served-probe/contact.html" in sitemap.text


@pytest.mark.asyncio
async def test_a_legacy_site_still_serves_index_and_its_about_page_is_a_real_404(
    tmp_path: Path, monkeypatch
) -> None:
    from app.main import create_app

    monkeypatch.setattr(site_service, "SITES_ROOT", tmp_path)
    _write_legacy_site(tmp_path, "legacy-site", "2026-01-01T00:00:00+00:00")

    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/sites/legacy-site/")).status_code == 200
        assert (await client.get("/sites/legacy-site/styles.css")).status_code == 200
        # The V26 pages are NOT fabricated for a site that predates them.
        assert (await client.get("/sites/legacy-site/about.html")).status_code == 404
        assert (await client.get("/sites/legacy-site/sitemap.xml")).status_code == 404


def test_the_static_mount_serves_a_site_written_with_the_test_client(tmp_path: Path, monkeypatch) -> None:
    # Sync probe: the SAME mount serves the V26 file set written verbatim by render_pages.
    from app.main import create_app

    monkeypatch.setattr(site_service, "SITES_ROOT", tmp_path)
    files = _pages()
    directory = tmp_path / "acme-robots"
    directory.mkdir()
    for name, content in files.items():
        (directory / name).write_text(content, encoding="utf-8")

    client = TestClient(create_app())
    for name in (*PAGES, "sitemap.xml"):
        resp = client.get(f"/sites/acme-robots/{name}")
        assert resp.status_code == 200, name
    assert client.get("/sites/acme-robots/").text.lstrip().lower().startswith("<!doctype html>")


def _write_legacy_site(root: Path, slug: str, created_at: str) -> None:
    """A V24 `meta.json` (no V26 keys) + its two files — the backward-compatibility fixture."""
    directory = root / slug
    directory.mkdir(parents=True)
    (directory / "index.html").write_text("<!doctype html><title>Legacy</title>", encoding="utf-8")
    (directory / "styles.css").write_text("body{}", encoding="utf-8")
    (directory / "meta.json").write_text(
        json.dumps(
            {"slug": slug, "title": "Legacy", "created_at": created_at, "bytes": 34},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_an_unknown_site_page_is_a_404_not_a_fabricated_page(tmp_path: Path, monkeypatch) -> None:
    from app.main import create_app

    monkeypatch.setattr(site_service, "SITES_ROOT", tmp_path)
    client = TestClient(create_app())
    resp = client.get("/sites/no-such-site/about.html")
    assert resp.status_code == 404
    # E_NOT_FOUND stays the service-level word for "no such site".
    with pytest.raises(PolsiaError) as excinfo:
        site_service.read_site_meta("no-such-site", root=tmp_path)
    assert excinfo.value.code == E_NOT_FOUND
