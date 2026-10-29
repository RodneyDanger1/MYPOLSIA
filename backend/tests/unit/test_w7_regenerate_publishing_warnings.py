"""WAVE 7 — `POST /sites/{slug}/regenerate` serves the copy census too (`publishing_warnings`).

WHY (R-W6-A3, carried from `weaver/REPORT-W6.md` §3 / §12): the F-32 channel serves
`publishing_warnings` on CREATE only. `regenerate_site` re-renders and re-publishes every page from
the CURRENT company profile — so a rebuilt site can introduce exactly what create warns about
(a banned claim, a private value), and the rebuild finished in silence: the 200 carried no census.
The decision recorded in wave 6 asked whether regenerate should carry the warnings; this wave adds
it as an ADDITIVE field (bridge contract request R-W7-A2 re-issues STOREFRONT-CONTRACT §6.1 for it)
rather than leaving the rebuild unmeasured.

WHAT THIS PINS
  * the service result carries `publishing_warnings`, and a clean profile's answer is `[]` (a
    measured empty answer, not an omitted key);
  * a banned claim in the CURRENT profile fires `W_BANNED_COPY_CLAIM` on the rebuild path with the
    same shape create uses (`code` / `message` / `fields` / `how_to_fix`);
  * a private value is masked on this path exactly as on create (`W_PUBLISHED_PRIVATE_VALUE`, the
    value never reproduced);
  * the field is ADDITIVE: the frozen identity keys (`slug` / `url` / `bytes` / `title` /
    `created_at`) are unchanged, and the rebuild is NOT withheld — the census stays advisory, never
    a gate (the site's pages are written and served);
  * over HTTP the response body carries the key (so the frontend consumer in
    `frontend/src/app/sites/page.tsx` has something real to render).

The profile fixtures mirror `test_w6_publishing_copy_warnings.py` (identity unset = nothing public)
so a warning here means the REBUILD path produced it, not that the fixture was dirty.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.models.company import CompanyConfig
from app.services import site_service


def _company(**overrides) -> CompanyConfig:
    """A profile whose copy is clean unless a test says otherwise (the wave-6 fixture rule)."""
    values = {
        "name": "Cinder & Lye",
        "value_prop": "Made-to-order soy candles.",
        "sender_legal_name": None,
        "sender_postal_address": None,
        "notification_email": None,
    }
    values.update(overrides)
    return CompanyConfig(**values)


async def _created(db, tmp_path: Path):
    return await site_service.generate_site(
        db, title="Cinder & Lye", template="classic", root=tmp_path,
        base_url="http://first.example/",
    )


def _codes(warnings: list[dict]) -> list[str]:
    return [warning["code"] for warning in warnings]


# ── 1. the service result carries the field ─────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_regenerate_serves_an_empty_warning_list_for_a_clean_profile(
    async_db_session, tmp_path: Path
) -> None:
    async_db_session.add(_company())
    await async_db_session.commit()
    created = await _created(async_db_session, tmp_path)

    result = await site_service.regenerate_site(
        async_db_session, created["slug"], root=tmp_path, base_url="http://test/"
    )

    # The key is PRESENT and empty: a measured "nothing blocking", never an omitted field.
    assert "publishing_warnings" in result
    assert result["publishing_warnings"] == []


@pytest.mark.asyncio
async def test_regenerate_warns_on_a_banned_claim_the_current_profile_carries(
    async_db_session, tmp_path: Path
) -> None:
    company = _company()
    async_db_session.add(company)
    await async_db_session.commit()
    created = await _created(async_db_session, tmp_path)

    # The owner edits the profile AFTER the create: the rebuild is what publishes this line.
    company.value_prop = "Small-batch candles."
    async_db_session.add(company)
    await async_db_session.commit()

    result = await site_service.regenerate_site(
        async_db_session, created["slug"], root=tmp_path, base_url="http://test/"
    )

    assert _codes(result["publishing_warnings"]) == ["W_BANNED_COPY_CLAIM"]
    warning = result["publishing_warnings"][0]
    assert set(warning) == {"code", "message", "fields", "how_to_fix"}
    assert warning["fields"] == ["value_prop"]
    assert "small_batch" in warning["message"]
    assert warning["how_to_fix"].strip() != ""
    # Advisory, not a gate: the page was rewritten with the line it warns about.
    served = (tmp_path / created["slug"] / "index.html").read_text(encoding="utf-8")
    assert "Small-batch candles." in served


@pytest.mark.asyncio
async def test_regenerate_masks_a_private_value_exactly_like_create(
    async_db_session, tmp_path: Path
) -> None:
    company = _company()
    async_db_session.add(company)
    await async_db_session.commit()
    created = await _created(async_db_session, tmp_path)

    # A private value the owner pasted into copy. It must be warned AND never reproduced.
    company.description = "We ship from 42 Example Lane every Tuesday."
    async_db_session.add(company)
    await async_db_session.commit()

    result = await site_service.regenerate_site(
        async_db_session, created["slug"], root=tmp_path, base_url="http://test/"
    )

    warnings = result["publishing_warnings"]
    assert _codes(warnings) == ["W_PUBLISHED_PRIVATE_VALUE"], _codes(warnings)
    assert warnings[0]["fields"] == ["description"]
    serialized = json.dumps(warnings)
    assert "42 Example Lane" not in serialized, serialized
    assert "42 Example" not in serialized, serialized


# ── 2. additivity: the frozen identity keys are untouched ───────────────────────────────────────
@pytest.mark.asyncio
async def test_the_warning_field_is_additive_and_identity_is_preserved(
    async_db_session, tmp_path: Path
) -> None:
    async_db_session.add(_company())
    await async_db_session.commit()
    created = await _created(async_db_session, tmp_path)
    before = json.loads((tmp_path / created["slug"] / "meta.json").read_text(encoding="utf-8"))

    result = await site_service.regenerate_site(
        async_db_session, created["slug"], root=tmp_path, base_url="http://test/"
    )

    # Every pre-wave-7 key still answers the same contract.
    assert set(result) >= {
        "slug", "url", "bytes", "title", "created_at", "pages", "template", "custom_domain",
        "publishing_warnings",
    }
    # The keys the create response also serves are IDENTICAL to what the create said.
    for key in ("slug", "url", "title", "created_at"):
        assert result[key] == created[key], key
    assert result["template"] == "classic"  # the stored template, preserved by the rebuild
    assert result["pages"] == list(site_service.PAGES)
    assert isinstance(result["bytes"], int) and result["bytes"] > 0
    # Identity survived on disk too (the rebuild is not a re-creation).
    after = json.loads((tmp_path / created["slug"] / "meta.json").read_text(encoding="utf-8"))
    assert after["created_at"] == before["created_at"]
    assert after["slug"] == before["slug"]


# ── 3. over HTTP: the body carries the key ─────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_the_regenerate_route_serializes_the_warning_field(
    api_client, auth_headers, async_db_session, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(site_service, "SITES_ROOT", tmp_path)
    async_db_session.add(_company(value_prop="Small-batch candles."))
    await async_db_session.commit()
    created = await site_service.generate_site(
        async_db_session, title="Cinder & Lye", root=tmp_path, base_url="http://test/"
    )

    response = await api_client.post(
        f"/api/v1/sites/{created['slug']}/regenerate", headers=auth_headers
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["slug"] == created["slug"]
    # The key is on the wire, with the create path's shape.
    assert "publishing_warnings" in body
    assert _codes(body["publishing_warnings"]) == ["W_BANNED_COPY_CLAIM"]
    warning = body["publishing_warnings"][0]
    assert set(warning) == {"code", "message", "fields", "how_to_fix"}
