"""V12/W-C — GET /api/v1/site/package/download: the written package as a ZIP from the exports
root. Reads-only, owner-key like its sibling; exports-root-guarded, never a traversal.
(Mirrors the /ops/sandbox parent-surgical change: route + focused test + both parity pins.)

RED-first: this file failed at collection (route absent -> 404) on the pre-fix tree.
"""
from __future__ import annotations

import io
import zipfile

import pytest

import app.models  # noqa: F401
from app.models.company import CompanyConfig


def _company(db):
    company = CompanyConfig(
        name="North Star SEO",
        description="d",
        mission="m",
        value_prop="v",
        notification_email="owner@northstar.example",
        website_url="https://northstar.example",
    )
    db.add(company)
    return company


@pytest.mark.asyncio
async def test_download_requires_the_owner_key(api_client):
    resp = await api_client.get("/api/v1/site/package/download")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_download_serves_the_built_package_as_a_zip(
    api_client, auth_headers, async_db_session, tmp_path, monkeypatch
):
    import app.services.site_package_service as service

    monkeypatch.setattr(service, "EXPORTS_ROOT", tmp_path)
    _company(async_db_session)
    await async_db_session.commit()

    built = await api_client.post("/api/v1/site/package", headers=auth_headers)
    assert built.status_code == 200, built.text
    slug = built.json()["slug"]

    resp = await api_client.get("/api/v1/site/package/download", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/zip")
    assert f"site-{slug}.zip" in resp.headers.get("content-disposition", "")

    archive = zipfile.ZipFile(io.BytesIO(resp.content))
    assert sorted(archive.namelist()) == ["HOSTING-README.md", "index.html"]
    on_disk = tmp_path / f"site-{slug}" / "index.html"
    assert archive.read("index.html") == on_disk.read_bytes()

    named = await api_client.get(
        "/api/v1/site/package/download", params={"slug": slug}, headers=auth_headers
    )
    assert named.status_code == 200


@pytest.mark.asyncio
async def test_download_names_a_missing_package(api_client, auth_headers, tmp_path, monkeypatch):
    import app.services.site_package_service as service

    monkeypatch.setattr(service, "EXPORTS_ROOT", tmp_path)
    resp = await api_client.get(
        "/api/v1/site/package/download", params={"slug": "never-built"}, headers=auth_headers
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "E_NOT_FOUND"


@pytest.mark.asyncio
async def test_download_refuses_traversal_slugs(api_client, auth_headers, tmp_path, monkeypatch):
    import app.services.site_package_service as service

    monkeypatch.setattr(service, "EXPORTS_ROOT", tmp_path)
    for bad in ("../etc", "..%2F..%2Fetc", "a/b", "site-north-star-seo/../..", "North Star", "..", "."):
        resp = await api_client.get(
            "/api/v1/site/package/download", params={"slug": bad}, headers=auth_headers
        )
        assert resp.status_code >= 400, f"slug {bad!r} was served"
        assert resp.json()["error"]["code"] == "E_VALIDATION", (bad, resp.text)
