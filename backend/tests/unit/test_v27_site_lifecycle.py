"""Wave V27 / lane L1 — the owner-managed site lifecycle: delete + regenerate.

RED leg (raw output in `wave-v27/l1-site-lifecycle/raw/red-backend.txt`): this file was written
BEFORE `site_service.delete_site` / `site_service.regenerate_site` and the two routes existed, so
the suite failed on the missing names — the honest RED state for a new capability.

What each group proves (the brief's frozen contract):
  * `delete_site` removes `data/sites/<slug>/` (its `meta.json` included); a second delete is the
    named `E_NOT_FOUND`; a slug that is not a plain name (traversal, absolute, empty) is
    `E_VALIDATION`; a directory LINK that resolves outside the sites root is `E_VALIDATION` and its
    target survives; a link INSIDE a site directory is removed, never followed.
  * `regenerate_site` re-renders the three pages + `styles.css` + `sitemap.xml` from the CURRENT
    company profile, keeping slug/title/`created_at`/template/custom_domain, and rewrites `bytes`
    from the files it just wrote.
  * A legacy V24 site (index + styles only, no V26 keys) regenerates to the full V26 shape and can
    then be deleted.
  * Over HTTP: `DELETE /api/v1/sites/{slug}` -> 200 `{slug, deleted}`, second call 404 `E_NOT_FOUND`;
    `POST /api/v1/sites/{slug}/regenerate` -> 200 with the preserved record, 404 for an unknown slug.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from app.core.errors import E_CONFIG, E_NOT_FOUND, E_VALIDATION, PolsiaError
from app.models.company import CompanyConfig
from app.services import site_service
from app.services.company_service import get_company

PAGES = ("index.html", "about.html", "contact.html")
V26_FILES = (*PAGES, "styles.css", "sitemap.xml")


# ── fixtures ─────────────────────────────────────────────────────────────────────────────────
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


def _write_legacy_site(root: Path, slug: str, created_at: str) -> None:
    """A V24 site: `index.html` + `styles.css` + a `meta.json` with none of the V26 keys."""
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


def _link_directory(link: Path, target: Path) -> None:
    """A REAL directory link — an NTFS junction on Windows, a symlink elsewhere. Never a mock.

    A junction needs no admin privilege; a symlink does, so on a host that can create neither the
    test is SKIPPED (stated plainly) rather than weakened into a fictitious path.
    """
    link.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            pytest.skip(f"this host cannot create a directory junction: {result.stderr.strip()}")
        return
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError as exc:  # pragma: no cover - platform capability, not product logic
        pytest.skip(f"this host cannot create a symlink: {exc}")


def _meta(site_dir: Path) -> dict:
    return json.loads((site_dir / "meta.json").read_text(encoding="utf-8"))


# ── 1. delete (service) ──────────────────────────────────────────────────────────────────────
def test_delete_removes_the_site_directory_and_its_meta(tmp_path: Path) -> None:
    _write_legacy_site(tmp_path, "route-site", "2026-01-01T00:00:00+00:00")
    assert (tmp_path / "route-site" / "meta.json").is_file()

    result = site_service.delete_site("route-site", root=tmp_path)

    assert result == {"slug": "route-site", "deleted": True}
    assert not (tmp_path / "route-site").exists()


def test_a_second_delete_is_the_named_not_found(tmp_path: Path) -> None:
    _write_legacy_site(tmp_path, "route-site", "2026-01-01T00:00:00+00:00")
    site_service.delete_site("route-site", root=tmp_path)

    with pytest.raises(PolsiaError) as excinfo:
        site_service.delete_site("route-site", root=tmp_path)
    assert excinfo.value.code == E_NOT_FOUND
    assert excinfo.value.context["slug"] == "route-site"


@pytest.mark.parametrize("bad", ["../outside", "/etc", "..", "", "Site With Spaces", "a/b", "sla/sh"])
def test_delete_refuses_a_slug_that_is_not_a_plain_name(tmp_path: Path, bad: str) -> None:
    sites = tmp_path / "sites"
    sites.mkdir()
    (tmp_path / "outside").mkdir()

    with pytest.raises(PolsiaError) as excinfo:
        site_service.delete_site(bad, root=sites)
    assert excinfo.value.code == E_VALIDATION
    # Nothing outside the sites root was touched.
    assert (tmp_path / "outside").is_dir()


def test_delete_refuses_a_linked_directory_that_resolves_outside_the_sites_root(
    tmp_path: Path,
) -> None:
    sites = tmp_path / "sites"
    sites.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    _link_directory(sites / "evil", outside)

    with pytest.raises(PolsiaError) as excinfo:
        site_service.delete_site("evil", root=sites)
    assert excinfo.value.code == E_VALIDATION
    assert excinfo.value.context["slug"] == "evil"
    # The link's TARGET is intact: the refusal happened before any removal.
    assert (outside / "keep.txt").is_file()


def test_delete_never_follows_a_link_inside_the_site(tmp_path: Path) -> None:
    sites = tmp_path / "sites"
    site = sites / "linked-assets"
    site.mkdir(parents=True)
    (site / "index.html").write_text("<!doctype html>", encoding="utf-8")
    (site / "meta.json").write_text(
        json.dumps({"slug": "linked-assets", "title": "L", "created_at": "", "bytes": 15}),
        encoding="utf-8",
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    _link_directory(site / "escape", outside)

    site_service.delete_site("linked-assets", root=sites)

    assert not site.exists()
    assert (outside / "keep.txt").is_file()  # the link was unlinked, never traversed


# ── 2. regenerate (service) ─────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_regenerate_rewrites_from_the_current_profile_and_keeps_identity(
    async_db_session, tmp_path: Path
) -> None:
    company = _company()
    async_db_session.add(company)
    await async_db_session.commit()
    created = await site_service.generate_site(
        async_db_session,
        title="Acme Site",
        template="modern",
        custom_domain="shop.example.com",
        root=tmp_path,
        base_url="http://first.example/",
        created_at="2026-10-19T12:00:00+00:00",
    )
    slug = created["slug"]
    site_dir = tmp_path / slug
    assert "Autonomous warehouse robots for small factories." in (
        site_dir / "index.html"
    ).read_text(encoding="utf-8")

    # The owner edits the company profile AFTER the site was built.
    company.value_prop = "Inventory drones for cold storage."
    company.description = "We fly the cold aisles."
    await async_db_session.commit()
    async_db_session.expire_all()

    result = await site_service.regenerate_site(
        async_db_session, slug, root=tmp_path, base_url="http://second.example/"
    )

    assert sorted(entry.name for entry in site_dir.iterdir()) == [
        "about.html",
        "contact.html",
        "index.html",
        "meta.json",
        "sitemap.xml",
        "styles.css",
    ]
    index = (site_dir / "index.html").read_text(encoding="utf-8")
    assert "Inventory drones for cold storage." in index
    assert "Autonomous warehouse robots for small factories." not in index
    assert '<body class="t-modern">' in index
    sitemap = (site_dir / "sitemap.xml").read_text(encoding="utf-8")
    assert "http://second.example/sites/acme-site/" in sitemap
    assert "http://first.example" not in sitemap

    meta = _meta(site_dir)
    assert meta["slug"] == "acme-site"
    assert meta["title"] == "Acme Site"
    assert meta["created_at"] == "2026-10-19T12:00:00+00:00"
    assert meta["template"] == "modern"
    assert meta["custom_domain"] == "shop.example.com"
    assert meta["pages"] == ["index.html", "about.html", "contact.html"]
    assert meta["seo"]["description"] == "Inventory drones for cold storage."
    written = sum(
        entry.stat().st_size for entry in site_dir.iterdir() if entry.name != "meta.json"
    )
    assert meta["bytes"] == written
    # The route body describes exactly the record on disk.
    assert result["slug"] == meta["slug"]
    assert result["title"] == meta["title"]
    assert result["created_at"] == meta["created_at"]
    assert result["template"] == meta["template"]
    assert result["custom_domain"] == meta["custom_domain"]
    assert result["pages"] == meta["pages"]
    assert result["bytes"] == written
    assert result["url"] == "/sites/acme-site/"


@pytest.mark.asyncio
async def test_a_legacy_site_regenerates_to_the_full_v26_shape_and_can_be_deleted(
    async_db_session, tmp_path: Path
) -> None:
    async_db_session.add(_company())
    await async_db_session.commit()
    _write_legacy_site(tmp_path, "legacy-site", "2026-01-01T00:00:00+00:00")

    result = await site_service.regenerate_site(
        async_db_session, "legacy-site", root=tmp_path, base_url="http://test/"
    )

    site_dir = tmp_path / "legacy-site"
    assert sorted(entry.name for entry in site_dir.iterdir()) == [
        "about.html",
        "contact.html",
        "index.html",
        "meta.json",
        "sitemap.xml",
        "styles.css",
    ]
    index = (site_dir / "index.html").read_text(encoding="utf-8")
    assert index.lstrip().lower().startswith("<!doctype html>")
    assert "Acme Robotics" in index
    meta = _meta(site_dir)
    assert meta["pages"] == ["index.html", "about.html", "contact.html"]
    assert meta["template"] == "classic"  # a legacy site regenerates as the V24 look
    assert meta["created_at"] == "2026-01-01T00:00:00+00:00"
    assert meta["title"] == "Legacy"
    assert result["pages"] == meta["pages"]

    assert site_service.delete_site("legacy-site", root=tmp_path) == {
        "slug": "legacy-site",
        "deleted": True,
    }
    assert not site_dir.exists()


@pytest.mark.asyncio
async def test_regenerating_an_unknown_slug_is_the_named_not_found(
    async_db_session, tmp_path: Path
) -> None:
    async_db_session.add(_company())
    await async_db_session.commit()

    with pytest.raises(PolsiaError) as excinfo:
        await site_service.regenerate_site(async_db_session, "no-such-site", root=tmp_path)
    assert excinfo.value.code == E_NOT_FOUND
    assert excinfo.value.context["slug"] == "no-such-site"


@pytest.mark.asyncio
async def test_regenerating_without_a_company_profile_is_the_named_config_refusal(
    async_db_session, tmp_path: Path
) -> None:
    _write_legacy_site(tmp_path, "legacy-site", "2026-01-01T00:00:00+00:00")

    with pytest.raises(PolsiaError) as excinfo:
        await site_service.regenerate_site(
            async_db_session, "legacy-site", root=tmp_path, base_url="http://test/"
        )
    assert excinfo.value.code == E_CONFIG
    assert excinfo.value.context["missing"] == "company"


# ── 3. the HTTP surface ─────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_delete_route_removes_the_site_and_a_second_delete_is_404(
    api_client, auth_headers, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(site_service, "SITES_ROOT", tmp_path)
    _write_legacy_site(tmp_path, "route-site", "2026-01-01T00:00:00+00:00")

    first = await api_client.delete("/api/v1/sites/route-site", headers=auth_headers)
    assert first.status_code == 200, first.text
    assert first.json() == {"slug": "route-site", "deleted": True}
    assert not (tmp_path / "route-site").exists()

    second = await api_client.delete("/api/v1/sites/route-site", headers=auth_headers)
    assert second.status_code == 404, second.text
    assert second.json()["error"]["code"] == E_NOT_FOUND


@pytest.mark.asyncio
async def test_regenerate_route_rewrites_the_files_and_preserves_the_record(
    api_client, async_db_session, auth_headers, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(site_service, "SITES_ROOT", tmp_path)
    async_db_session.add(_company())
    await async_db_session.commit()

    created = await api_client.post(
        "/api/v1/sites",
        json={"title": "Route Site", "template": "bold", "custom_domain": "shop.example.com"},
        headers=auth_headers,
    )
    assert created.status_code == 201, created.text
    slug = created.json()["slug"]
    before = (await api_client.get("/api/v1/sites", headers=auth_headers)).json()[0]
    assert before["template"] == "bold"

    company = await get_company(async_db_session)
    assert company is not None
    company.description = "Fresh prose written after the first build."
    await async_db_session.commit()
    async_db_session.expire_all()

    resp = await api_client.post(f"/api/v1/sites/{slug}/regenerate", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["slug"] == slug
    assert body["pages"] == ["index.html", "about.html", "contact.html"]
    assert body["template"] == "bold"
    assert body["custom_domain"] == "shop.example.com"
    assert body["created_at"] == before["created_at"]

    site_dir = tmp_path / slug
    assert "Fresh prose written after the first build." in (site_dir / "about.html").read_text(
        encoding="utf-8"
    )
    assert (site_dir / "sitemap.xml").is_file()
    assert "http://test/sites/" in (site_dir / "sitemap.xml").read_text(encoding="utf-8")

    after = (await api_client.get("/api/v1/sites", headers=auth_headers)).json()
    assert len(after) == 1
    assert after[0]["created_at"] == before["created_at"]
    assert after[0]["bytes"] == body["bytes"]


@pytest.mark.asyncio
async def test_regenerate_route_refuses_an_unknown_slug(
    api_client, async_db_session, auth_headers
) -> None:
    async_db_session.add(_company())
    await async_db_session.commit()

    resp = await api_client.post("/api/v1/sites/no-such-site/regenerate", headers=auth_headers)
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == E_NOT_FOUND


@pytest.mark.asyncio
async def test_the_full_lifecycle_create_regenerate_delete_then_the_list_is_empty(
    api_client, async_db_session, auth_headers, tmp_path: Path, monkeypatch
) -> None:
    """The parent's drill sequence, proved at the ASGI layer: create → regenerate → delete → 404."""
    monkeypatch.setattr(site_service, "SITES_ROOT", tmp_path)
    async_db_session.add(_company())
    await async_db_session.commit()

    created = await api_client.post(
        "/api/v1/sites",
        json={"title": "Drill Site", "template": "modern"},
        headers=auth_headers,
    )
    assert created.status_code == 201, created.text
    slug = created.json()["slug"]

    regenerated = await api_client.post(f"/api/v1/sites/{slug}/regenerate", headers=auth_headers)
    assert regenerated.status_code == 200, regenerated.text
    assert sorted(entry.name for entry in (tmp_path / slug).iterdir()) == [
        "about.html",
        "contact.html",
        "index.html",
        "meta.json",
        "sitemap.xml",
        "styles.css",
    ]

    deleted = await api_client.delete(f"/api/v1/sites/{slug}", headers=auth_headers)
    assert deleted.status_code == 200, deleted.text
    assert not (tmp_path / slug).exists()

    rows = (await api_client.get("/api/v1/sites", headers=auth_headers)).json()
    assert rows == []
    gone = await api_client.get("/api/v1/sites", params={"slug": slug}, headers=auth_headers)
    assert gone.status_code == 404, gone.text
    assert gone.json()["error"]["code"] == E_NOT_FOUND
