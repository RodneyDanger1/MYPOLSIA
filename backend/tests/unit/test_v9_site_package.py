"""V9 B2 (R5 #4): POST /api/v1/site/package — the static-site package export.

NO hosting, NO promises: the endpoint writes `index.html` + `HOSTING-README.md` into a
`site-<slug>/` folder from the company's own fields, returns the file paths, refuses E_CONFIG when
the company name or contact is missing, and is deterministic (same fields -> byte-identical HTML).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.models.company import CompanyConfig

REFUSAL_CODE = "E_CONFIG"


def _company(db, **overrides):
    fields = {
        "name": "North Star SEO",
        "description": "Local SEO for small Minnesota businesses.",
        "mission": "Honest local search results.",
        "value_prop": "Simple SEO, fixed scope.",
        "notification_email": "owner@northstar.example",
        "website_url": "https://northstar.example",
    }
    fields.update(overrides)
    company = CompanyConfig(**fields)
    db.add(company)
    return company


@pytest.mark.asyncio
async def test_site_package_refuses_e_config_without_a_company(api_client, auth_headers):
    resp = await api_client.post("/api/v1/site/package", headers=auth_headers)
    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == REFUSAL_CODE


@pytest.mark.asyncio
async def test_site_package_refuses_e_config_without_contact(api_client, auth_headers, async_db_session):
    _company(async_db_session, notification_email=None)
    await async_db_session.commit()
    resp = await api_client.post("/api/v1/site/package", headers=auth_headers)
    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == REFUSAL_CODE


@pytest.mark.asyncio
async def test_site_package_refuses_e_config_without_a_name(api_client, auth_headers, async_db_session):
    _company(async_db_session, name="   ")
    await async_db_session.commit()
    resp = await api_client.post("/api/v1/site/package", headers=auth_headers)
    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == REFUSAL_CODE


@pytest.mark.asyncio
async def test_site_package_builds_files_and_returns_their_paths(
    api_client, auth_headers, async_db_session, tmp_path, monkeypatch
):
    import app.services.site_package_service as service

    monkeypatch.setattr(service, "EXPORTS_ROOT", tmp_path)
    _company(async_db_session)
    await async_db_session.commit()

    resp = await api_client.post("/api/v1/site/package", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    files = {Path(path).name for path in body["files"]}
    assert files == {"index.html", "HOSTING-README.md"}
    package_dir = Path(body["package_dir"])
    assert package_dir.name == "site-north-star-seo"
    for path in body["files"]:
        assert Path(path).is_file()

    html = (package_dir / "index.html").read_text(encoding="utf-8")
    assert "North Star SEO" in html
    assert "owner@northstar.example" in html
    assert 'id="contact"' in html and "Contact" in html
    assert "Not tax advice" not in html  # the site package makes no tax promises either
    readme = (package_dir / "HOSTING-README.md").read_text(encoding="utf-8")
    assert "no hosting" in readme.lower()


@pytest.mark.asyncio
async def test_site_package_html_is_deterministic_and_escaped(async_db_session, tmp_path, monkeypatch):
    import app.services.site_package_service as service

    monkeypatch.setattr(service, "EXPORTS_ROOT", tmp_path)
    _company(async_db_session, name="North Star <script>alert(1)</script>")
    await async_db_session.commit()

    first = await service.build_site_package(async_db_session, root=tmp_path / "one")
    second = await service.build_site_package(async_db_session, root=tmp_path / "two")
    first_html = (Path(first["package_dir"]) / "index.html").read_bytes()
    second_html = (Path(second["package_dir"]) / "index.html").read_bytes()
    assert first_html == second_html, "same company fields must build byte-identical HTML"
    assert b"<script>" not in first_html, "company fields are escaped, never injected raw"
