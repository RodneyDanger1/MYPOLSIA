"""WAVE 4 / F-32 — `POST /sites` must TELL an API client what creating the site publishes.

THE DEFECT (bridge STOREFRONT-CONTRACT §3, measured on the wave-3 build): creating a site writes
`contact.html` with the company profile's postal address, legal name and contact email VERBATIM,
and the only warning lived in the create form's prose.  An API client got `{slug,url,bytes}` — no
warning at all — while the exact sentence and additive shape were already specified in the
contract's §3 as `publishing_warnings: [{code, message, fields, how_to_fix}]`, `[]` when unset.

WHAT THIS PINS:
  * all three fields set  -> ONE warning, code `W_PUBLIC_CONTACT_DETAILS`, `fields` in page order,
    a message that names each thing published, and a non-empty `how_to_fix`;
  * one field set         -> `fields` names ONLY that field and the message claims nothing else
    (a blanket sentence about an address nobody stored would be a false claim);
  * no field set          -> `[]`, a real empty list (not an omitted key, not a null);
  * the warning describes the page that was ACTUALLY written: the address printed in the returned
    site's `contact.html` is the same address the warning names.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.models.company import CompanyConfig
from app.services import site_service

ADDRESS = "221B Baker Street, London"
LEGAL = "Cinder and Lye LLC"
EMAIL = "hello@cinder-lye.example"


def _company(**overrides) -> CompanyConfig:
    values = {
        "name": "Cinder & Lye",
        # WAVE 6 (copy-census wiring): this fixture used to read `"Small-batch candles."` — a BANNED
        # claim (`copy_claims.small_batch`).  `publishing_warnings` now appends one warning per
        # blocking census finding, so that value would have added a second entry and the
        # `len(warnings) == 1` pins below would have had to grow.  The fixture therefore carries an
        # ALLOWED line instead ("made to order" + "soy candle" are the supplier's own words), which
        # keeps every assertion in this file exactly as strong as it was: one contact warning when a
        # contact field is set, `[]` when nothing is.  The banned-claim case is pinned in its own
        # suite (`test_w6_publishing_copy_warnings.py`) rather than by weakening these checks.
        "value_prop": "Made-to-order soy candles.",
        "sender_legal_name": None,
        "sender_postal_address": None,
        "notification_email": None,
    }
    values.update(overrides)
    return CompanyConfig(**values)


# ── 1. the service helper: exact fields, honest subsets ──────────────────────────────────────
def test_all_three_fields_produce_one_warning_naming_each_of_them() -> None:
    warnings = site_service.publishing_warnings(
        _company(
            sender_postal_address=ADDRESS,
            sender_legal_name=LEGAL,
            notification_email=EMAIL,
        )
    )
    assert len(warnings) == 1
    warning = warnings[0]
    assert warning["code"] == "W_PUBLIC_CONTACT_DETAILS"
    # Page order: the contact page prints Address, then Legal name, then Email.
    assert warning["fields"] == [
        "sender_postal_address",
        "sender_legal_name",
        "notification_email",
    ]
    assert "postal address" in warning["message"]
    assert "legal name" in warning["message"]
    assert "mailto:" in warning["message"]
    assert warning["how_to_fix"].strip() != ""


def test_only_the_set_field_is_claimed() -> None:
    warnings = site_service.publishing_warnings(_company(notification_email=EMAIL))
    assert [warning["fields"] for warning in warnings] == [["notification_email"]]
    message = warnings[0]["message"]
    assert "mailto:" in message
    # The profile stores no address and no legal name — the warning must not claim it publishes one.
    assert "postal address" not in message
    assert "legal name" not in message


def test_nothing_set_is_an_empty_list_not_a_null() -> None:
    assert site_service.publishing_warnings(_company()) == []
    # Blank strings are "not recorded" too — the renderer trims before it prints.
    assert site_service.publishing_warnings(
        _company(sender_postal_address="  ", sender_legal_name="", notification_email=" ")
    ) == []


# ── 2. the wire: POST /sites 201 carries it (additive, per contract §3) ──────────────────────
@pytest.mark.asyncio
async def test_post_sites_201_serves_the_warning_and_the_page_it_describes(
    api_client, async_db_session, auth_headers, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(site_service, "SITES_ROOT", tmp_path)
    async_db_session.add(
        _company(
            sender_postal_address=ADDRESS,
            sender_legal_name=LEGAL,
            notification_email=EMAIL,
        )
    )
    await async_db_session.commit()

    resp = await api_client.post(
        "/api/v1/sites", json={"title": "Contact disclosure"}, headers=auth_headers
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()

    # The three frozen keys are untouched; the warning is the fourth, additive one.
    assert {"slug", "url", "bytes"} <= set(body)
    warnings = body["publishing_warnings"]
    assert isinstance(warnings, list) and len(warnings) == 1
    warning = warnings[0]
    assert warning["code"] == "W_PUBLIC_CONTACT_DETAILS"
    assert warning["fields"] == [
        "sender_postal_address",
        "sender_legal_name",
        "notification_email",
    ]
    assert warning["how_to_fix"].strip() != ""

    # HONESTY CROSS-CHECK: the warning describes the file that was really written.
    contact = (tmp_path / body["slug"] / "contact.html").read_text(encoding="utf-8")
    assert ADDRESS in contact and LEGAL in contact and EMAIL in contact


@pytest.mark.asyncio
async def test_post_sites_201_serves_an_empty_list_when_the_profile_publishes_nothing(
    api_client, async_db_session, auth_headers, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(site_service, "SITES_ROOT", tmp_path)
    async_db_session.add(_company())
    await async_db_session.commit()

    resp = await api_client.post("/api/v1/sites", json={"title": "Quiet site"}, headers=auth_headers)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["publishing_warnings"] == []
    # ...and the key is PRESENT: absence would make the client guess whether the server answered.
    assert "publishing_warnings" in body


# ── 3. the service result carries the same list (one computation, two readers) ───────────────
@pytest.mark.asyncio
async def test_generate_site_result_carries_the_same_warning(
    async_db_session, tmp_path: Path
) -> None:
    async_db_session.add(_company(sender_legal_name=LEGAL))
    await async_db_session.commit()

    result = await site_service.generate_site(async_db_session, title="Service probe", root=tmp_path)
    assert [warning["fields"] for warning in result["publishing_warnings"]] == [
        ["sender_legal_name"]
    ]
