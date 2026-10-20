"""Wave V24 / lane L3 — the website builder: generation, hosting, listing.

RED leg (raw output in `wave-v24/l3-sites/raw/red-backend.txt`): this file was written BEFORE
`app/services/site_service.py` and `app/api/v1/sites.py` existed, so collection failed on the
imports — the honest RED state for a new capability.

What each group proves (the brief's frozen interface):
  * `render_site` is PURE and DETERMINISTIC — the same company/title/tagline render byte-identical
    HTML+CSS, built from the company profile only.
  * EVERY interpolated value is HTML-escaped: a `<script>` in the company name (or the title, the
    tagline, the target market) renders INERT text, never executable markup.  A `javascript:` website
    URL is text, never an href (the V14/U2-09 defect class).
  * The mailto CTA exists ONLY when `sender_email` exists; with none, the page carries no invented
    address and no `mailto:` at all.
  * Self-contained: one same-origin stylesheet, no external subresources, no scripts.
  * Slugs: `[a-z0-9-]{2,48}`, derived from the title when omitted, a collision is the NAMED
    `E_VALIDATION` naming the slug.
  * `POST /api/v1/sites` -> 201 `{slug,url,bytes}`; `GET /api/v1/sites` -> newest-first list read
    from each site's `meta.json`; an unknown slug is `E_NOT_FOUND`.
  * The generated site is SERVED through the real ASGI app at `/sites/<slug>/` (StaticFiles mount)
    and the mount directory is created at app construction when absent.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app.core.errors import E_NOT_FOUND, E_VALIDATION, PolsiaError
from app.models.company import CompanyConfig
from app.services import site_service


# ── profile fixtures ─────────────────────────────────────────────────────────────────────────
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
    }
    values.update(overrides)
    return CompanyConfig(**values)


def _html(company: CompanyConfig, **kwargs) -> str:
    return site_service.render_site(company, **kwargs)["index.html"]


# ── 1. generation: profile-derived, semantic, self-contained ─────────────────────────────────
def test_render_site_writes_the_two_files_and_uses_the_company_profile() -> None:
    files = site_service.render_site(
        _company(), title="Acme Robotics", tagline="Robots for small factories"
    )
    assert set(files) == {"index.html", "styles.css"}
    html_doc, css = files["index.html"], files["styles.css"]
    assert html_doc.lstrip().lower().startswith("<!doctype html>")
    assert "<title>Acme Robotics</title>" in html_doc
    assert "Robots for small factories" in html_doc
    assert "Autonomous warehouse robots for small factories." in html_doc
    assert "Small and mid-size manufacturers in the EU." in html_doc
    # Semantic landmarks the page is expected to carry.
    assert "<header" in html_doc and "<main" in html_doc and "<footer" in html_doc
    # Self-contained: one same-origin stylesheet, system fonts, no scripts, no external fetches.
    assert '<link rel="stylesheet" href="styles.css">' in html_doc
    assert "<script" not in html_doc.lower()
    assert "http://www.w3.org" not in html_doc  # no external SVG/xlink namespaces either
    assert "@import" not in css and "url(" not in css.lower()
    assert "font-family" in css and "system-ui" in css
    # Responsive: a viewport declaration and at least one max-width media query.
    assert 'name="viewport"' in html_doc
    assert "@media" in css


def test_render_site_is_deterministic() -> None:
    company = _company()
    first = site_service.render_site(company, title="T", tagline="G")
    second = site_service.render_site(company, title="T", tagline="G")
    assert first == second


def test_a_missing_value_prop_falls_back_to_the_next_profile_field_never_to_a_placeholder() -> None:
    company = _company(value_prop="", description="", mission="")
    html_doc = _html(company)
    # No invented marketing sentence: the about paragraph is simply absent when the profile has
    # nothing to say, and the page still renders name + title.
    assert "Acme Robotics" in html_doc
    assert "Autonomous warehouse robots" not in html_doc
    assert "Add a company description" not in html_doc


# ── 2. escaping: hostile profile values render inert ────────────────────────────────────────
def test_a_script_tag_in_the_company_name_renders_inert() -> None:
    hostile = 'Acme <script>alert("x")</script> & Sons'
    html_doc = _html(_company(name=hostile), title="Site")
    assert "<script>" not in html_doc
    assert 'alert("x")' not in html_doc
    assert "&lt;script&gt;" in html_doc
    assert "&amp; Sons" in html_doc


def test_every_interpolated_profile_field_is_escaped() -> None:
    hostile = '<img src=x onerror="alert(1)">'
    html_doc = _html(
        _company(
            name=hostile,
            value_prop=hostile,
            target_market=hostile,
            notification_email='x" onclick="alert(1)',
        ),
        title=hostile,
        tagline=hostile,
    )
    assert "<img" not in html_doc
    assert "<title>&lt;img" in html_doc
    assert "onerror=\"alert(1)\"" not in html_doc
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in html_doc


def test_a_javascript_website_url_is_rendered_as_text_not_a_link() -> None:
    html_doc = _html(_company(website_url="javascript:alert(1)"))
    assert 'href="javascript:' not in html_doc
    assert "javascript:alert(1)" in html_doc


def test_an_http_website_url_is_a_rel_noopener_link() -> None:
    html_doc = _html(_company())
    assert 'href="https://acme.example/robots"' in html_doc
    assert 'rel="noopener' in html_doc


# ── 3. the CTA: mailto ONLY when a sender email exists ──────────────────────────────────────
def test_the_cta_is_a_mailto_only_when_a_sender_email_exists() -> None:
    with_mail = _html(_company(notification_email="owner@acme.example"))
    assert 'href="mailto:owner@acme.example"' in with_mail
    assert "Get in touch" in with_mail

    without = _html(_company(notification_email=""))
    assert "mailto:" not in without
    assert "owner@" not in without
    assert "Get in touch" in without  # a plain line, no invented address


# ── 4. slugs ────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("My New Site!", "my-new-site"),
        ("  Spaced   Out  ", "spaced-out"),
        ("Ünicode — Café", "nicode-caf"),  # non-ascii folded out, never transliterated by guess
        ("A" * 80, "a" * 48),
    ],
)
def test_slug_is_derived_from_the_title(title: str, expected: str) -> None:
    assert site_service.derive_slug(title, fallback="acme") == expected


def test_a_title_with_no_slug_material_falls_back_to_the_company_name() -> None:
    assert site_service.derive_slug("***", fallback="Acme Robotics") == "acme-robotics"
    assert site_service.derive_slug(None, fallback="Acme") == "acme"


@pytest.mark.parametrize(
    "slug",
    ["", "a", "A", "has space", "has/slash", "dot.dot", "x" * 49, "über", "under_score"],
)
def test_an_invalid_slug_is_the_named_validation_refusal(slug: str) -> None:
    with pytest.raises(PolsiaError) as excinfo:
        site_service.validate_slug(slug)
    assert excinfo.value.code == E_VALIDATION
    assert excinfo.value.context["slug"] == slug


def test_a_valid_slug_passes_through() -> None:
    # The frozen pattern is exactly `[a-z0-9-]{2,48}` — a leading/trailing dash is ALLOWED by it,
    # so the validator must not invent a stricter rule and refuse a slug the contract permits.
    for slug in ("ab", "my-site-2", "a-b", "x" * 48, "-edge-"):
        assert site_service.validate_slug(slug) == slug


# ── 5. write + list + 404 (pure disk behaviour, no DB) ─────────────────────────────────────
def _write_site(root: Path, slug: str, created_at: str, title: str = "T") -> None:
    directory = root / slug
    directory.mkdir(parents=True)
    (directory / "index.html").write_text("<!doctype html><title>x</title>", encoding="utf-8")
    (directory / "styles.css").write_text("body{}", encoding="utf-8")
    (directory / "meta.json").write_text(
        json.dumps(
            {"slug": slug, "title": title, "created_at": created_at, "bytes": 34},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_list_sites_reads_meta_json_newest_first(tmp_path: Path) -> None:
    _write_site(tmp_path, "old-site", "2026-01-01T00:00:00+00:00", "Old")
    _write_site(tmp_path, "new-site", "2026-06-01T00:00:00+00:00", "New")
    _write_site(tmp_path, "mid-site", "2026-03-01T00:00:00+00:00", "Mid")
    rows = site_service.list_sites(root=tmp_path)
    assert [row["slug"] for row in rows] == ["new-site", "mid-site", "old-site"]
    assert rows[0] == {
        "slug": "new-site",
        "title": "New",
        "url": "/sites/new-site/",
        "created_at": "2026-06-01T00:00:00+00:00",
        "bytes": 34,
    }


def test_a_directory_without_meta_json_is_not_listed(tmp_path: Path) -> None:
    _write_site(tmp_path, "done", "2026-01-01T00:00:00+00:00")
    (tmp_path / "half-written").mkdir()
    (tmp_path / "half-written" / "index.html").write_text("partial", encoding="utf-8")
    assert [row["slug"] for row in site_service.list_sites(root=tmp_path)] == ["done"]


def test_an_unknown_slug_is_the_named_not_found_refusal(tmp_path: Path) -> None:
    with pytest.raises(PolsiaError) as excinfo:
        site_service.read_site("not-there", root=tmp_path)
    assert excinfo.value.code == E_NOT_FOUND
    assert excinfo.value.context["slug"] == "not-there"


def test_a_corrupt_meta_json_is_skipped_by_the_list_and_named_by_the_single_read(
    tmp_path: Path,
) -> None:
    _write_site(tmp_path, "good-site", "2026-01-01T00:00:00+00:00")
    broken = tmp_path / "broken-site"
    broken.mkdir()
    (broken / "index.html").write_text("<!doctype html>", encoding="utf-8")
    (broken / "meta.json").write_text("{not json", encoding="utf-8")

    # A corrupt entry cannot be listed (the list would be describing a site it cannot read)...
    assert [row["slug"] for row in site_service.list_sites(root=tmp_path)] == ["good-site"]
    # ...and asking for it by name is the same named refusal as asking for a missing one.
    with pytest.raises(PolsiaError) as excinfo:
        site_service.read_site("broken-site", root=tmp_path)
    assert excinfo.value.code == E_NOT_FOUND
    assert "unreadable" in excinfo.value.message


def test_a_non_numeric_bytes_field_is_a_named_refusal_not_a_500(tmp_path: Path) -> None:
    directory = tmp_path / "bad-bytes"
    directory.mkdir()
    (directory / "meta.json").write_text(
        json.dumps({"slug": "bad-bytes", "title": "x", "created_at": "2026-01-01", "bytes": "lots"}),
        encoding="utf-8",
    )
    with pytest.raises(PolsiaError) as excinfo:
        site_service.read_site("bad-bytes", root=tmp_path)
    assert excinfo.value.code == E_NOT_FOUND


# ── 6. generate_site: DB -> disk, bytes, collision ──────────────────────────────────────────
@pytest.mark.asyncio
async def test_generate_site_writes_the_files_and_reports_the_real_byte_count(
    async_db_session, tmp_path: Path
) -> None:
    async_db_session.add(_company())
    await async_db_session.commit()

    result = await site_service.generate_site(
        async_db_session,
        title="Launch page",
        tagline="Robots, delivered",
        root=tmp_path,
        created_at="2026-10-19T12:00:00+00:00",
    )
    assert result["slug"] == "launch-page"
    assert result["url"] == "/sites/launch-page/"
    site_dir = tmp_path / "launch-page"
    # V26/L5 (old -> new): `bytes` used to count index.html + styles.css only.  V26 sites also
    # write about.html, contact.html and sitemap.xml, and the field is the real total of every
    # written file except meta.json (which carries the number itself).
    written = sum(
        entry.stat().st_size for entry in site_dir.iterdir() if entry.name != "meta.json"
    )
    assert result["bytes"] == written
    assert result["bytes"] > 0
    meta = json.loads((site_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["slug"] == "launch-page"
    assert meta["title"] == "Launch page"
    assert meta["bytes"] == result["bytes"]
    assert "created_at" in meta
    # Nothing private: the meta file is inside the PUBLIC site directory.
    assert "email" not in json.dumps(meta).lower()


@pytest.mark.asyncio
async def test_generate_site_uses_the_company_contact_for_the_cta(
    async_db_session, tmp_path: Path
) -> None:
    async_db_session.add(_company(notification_email="hello@acme.example"))
    await async_db_session.commit()

    await site_service.generate_site(async_db_session, title="Contact probe", root=tmp_path)
    html_doc = (tmp_path / "contact-probe" / "index.html").read_text(encoding="utf-8")
    assert 'href="mailto:hello@acme.example"' in html_doc


@pytest.mark.asyncio
async def test_generate_site_twice_with_the_same_inputs_is_byte_identical(
    async_db_session, tmp_path: Path
) -> None:
    async_db_session.add(_company())
    await async_db_session.commit()
    first = await site_service.generate_site(async_db_session, title="Same", root=tmp_path / "a")
    second = await site_service.generate_site(async_db_session, title="Same", root=tmp_path / "b")
    assert first["bytes"] == second["bytes"]
    for name in ("index.html", "styles.css"):
        assert (tmp_path / "a" / "same" / name).read_bytes() == (
            tmp_path / "b" / "same" / name
        ).read_bytes()


@pytest.mark.asyncio
async def test_a_slug_collision_is_named_every_time(async_db_session, tmp_path: Path) -> None:
    async_db_session.add(_company())
    await async_db_session.commit()
    await site_service.generate_site(async_db_session, slug="taken", root=tmp_path)
    with pytest.raises(PolsiaError) as excinfo:
        await site_service.generate_site(async_db_session, slug="taken", root=tmp_path)
    assert excinfo.value.code == E_VALIDATION
    assert excinfo.value.context["slug"] == "taken"
    assert "taken" in excinfo.value.message


@pytest.mark.asyncio
async def test_generation_refuses_by_name_without_a_company(async_db_session, tmp_path: Path) -> None:
    from app.core.errors import E_CONFIG

    with pytest.raises(PolsiaError) as excinfo:
        await site_service.generate_site(async_db_session, title="x", root=tmp_path)
    assert excinfo.value.code == E_CONFIG


# ── 7. the HTTP surface ─────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_post_sites_answers_201_with_slug_url_and_bytes(
    api_client, async_db_session, auth_headers, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(site_service, "SITES_ROOT", tmp_path)
    async_db_session.add(_company())
    await async_db_session.commit()

    resp = await api_client.post(
        "/api/v1/sites",
        json={"title": "From the API", "tagline": "One clear line"},
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert set(body) == {"slug", "url", "bytes"}
    assert body["slug"] == "from-the-api"
    assert body["url"] == "/sites/from-the-api/"
    assert body["bytes"] > 0
    assert (tmp_path / "from-the-api" / "index.html").is_file()


@pytest.mark.asyncio
async def test_get_sites_lists_newest_first_and_filters_by_slug(
    api_client, async_db_session, auth_headers, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(site_service, "SITES_ROOT", tmp_path)
    _write_site(tmp_path, "first-site", "2026-01-01T00:00:00+00:00", "First")
    _write_site(tmp_path, "second-site", "2026-02-01T00:00:00+00:00", "Second")

    resp = await api_client.get("/api/v1/sites", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert [row["slug"] for row in rows] == ["second-site", "first-site"]
    assert set(rows[0]) == {"slug", "title", "url", "created_at", "bytes"}

    filtered = await api_client.get("/api/v1/sites?slug=first-site", headers=auth_headers)
    assert filtered.status_code == 200
    assert [row["slug"] for row in filtered.json()] == ["first-site"]


@pytest.mark.asyncio
async def test_an_unknown_slug_on_the_list_is_404_e_not_found(
    api_client, auth_headers, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(site_service, "SITES_ROOT", tmp_path)
    resp = await api_client.get("/api/v1/sites?slug=never-built", headers=auth_headers)
    assert resp.status_code == 404, resp.text
    error = resp.json()["error"]
    assert error["code"] == E_NOT_FOUND
    assert error["context"]["slug"] == "never-built"
    assert resp.headers["X-Polsia-Error-Code"] == E_NOT_FOUND


@pytest.mark.asyncio
async def test_a_bad_slug_on_post_is_the_named_validation_refusal(
    api_client, async_db_session, auth_headers, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(site_service, "SITES_ROOT", tmp_path)
    async_db_session.add(_company())
    await async_db_session.commit()
    resp = await api_client.post(
        "/api/v1/sites", json={"slug": "Bad Slug!"}, headers=auth_headers
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == E_VALIDATION


@pytest.mark.asyncio
async def test_sites_routes_require_the_owner_key(api_client) -> None:
    resp = await api_client.get("/api/v1/sites")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "E_AUTH"


# ── 8. static serving through the REAL ASGI app ─────────────────────────────────────────────
def test_the_static_mount_serves_the_generated_site(tmp_path: Path, monkeypatch) -> None:
    # Imported HERE: `app.main` imports every router, so this module still collects while another
    # lane is mid-edit on one of them (and the service tests need no app at all).
    from app.main import create_app

    monkeypatch.setattr(site_service, "SITES_ROOT", tmp_path)
    slug = "static-probe"
    directory = tmp_path / slug
    directory.mkdir()
    (directory / "index.html").write_text(
        "<!doctype html><html><head><title>Static probe</title>"
        '<link rel="stylesheet" href="styles.css"></head>'
        "<body><h1>Static probe</h1></body></html>",
        encoding="utf-8",
    )
    (directory / "styles.css").write_text("body { color: #111; }", encoding="utf-8")

    client = TestClient(create_app())
    page = client.get(f"/sites/{slug}/")
    assert page.status_code == 200
    assert "Static probe" in page.text
    assert page.headers["content-type"].startswith("text/html")

    css = client.get(f"/sites/{slug}/styles.css")
    assert css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")
    assert css.text == "body { color: #111; }"


def test_create_app_creates_the_sites_directory_when_absent(tmp_path: Path, monkeypatch) -> None:
    from app.main import create_app

    root = tmp_path / "missing-root"
    assert not root.exists()
    monkeypatch.setattr(site_service, "SITES_ROOT", root)
    create_app()
    assert root.is_dir()


def test_an_unknown_site_path_is_a_plain_404_not_a_fake_page(tmp_path: Path, monkeypatch) -> None:
    from app.main import create_app

    monkeypatch.setattr(site_service, "SITES_ROOT", tmp_path)
    client = TestClient(create_app())
    resp = client.get("/sites/no-such-site/")
    assert resp.status_code == 404
