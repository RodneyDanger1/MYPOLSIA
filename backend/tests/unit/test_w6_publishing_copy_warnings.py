"""WAVE 6 — the wave-5 copy census is now WIRED into the publish write path (`publishing_warnings`).

WHY (the closure of `truthseeker/REPORT-W5.md` §3.6 / §11.2 and `bridge/STOREFRONT-CONTRACT.md`
§6.1): the census in `app/services/copy_claims.py` DETECTED banned claims and private values in the
profile data the generated site renders, but nothing called it on a write path — so the corrected
copy was only as durable as the next edit.  `site_service.publishing_warnings` (the F-32 warning
channel that already names published contact details) now appends one warning per BLOCKING census
finding.

WHAT THIS PINS:

  * a banned claim in a profile field (or inside `pricing_model` JSON) produces
    `W_BANNED_COPY_CLAIM` naming the field, the term, the reason and a fix;
  * a private value in copy produces `W_PUBLISHED_PRIVATE_VALUE` and the message NEVER reproduces
    the value (AGENTS.md §27) — the finding is masked;
  * the exposure `W_PUBLIC_CONTACT_DETAILS` already names is not repeated as a private warning;
  * `unverified` H-register items (e.g. `cold-process`) never warn here — `blocking()` is
    banned+private only;
  * one warning per finding, in wire form `{code, message, fields, how_to_fix}`;
  * the channel is ADVISORY, not a gate: the site is still generated and the 201 still returns the
    three original keys plus `publishing_warnings`.

The banned-claim case is pinned HERE rather than by loosening the F-32 suite, whose fixture now
carries an allowed line (`test_w4_f32_site_publishing_warnings.py` documents the fixture change).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.models.company import CompanyConfig
from app.services import site_service
from app.services.copy_claims import census_profile, summarise

ADDRESS = "221B Baker Street, London"
LEGAL = "Cinder and Lye LLC"
EMAIL = "hello@cinder-lye.example"


def _company(**overrides) -> CompanyConfig:
    values = {
        "name": "Cinder & Lye",
        "value_prop": "Made-to-order soy candles.",
        "sender_legal_name": None,
        "sender_postal_address": None,
        "notification_email": None,
    }
    values.update(overrides)
    return CompanyConfig(**values)


def _codes(warnings: list[dict]) -> list[str]:
    return [warning["code"] for warning in warnings]


# ── 1. a banned claim is warned, by field and by name ────────────────────────────────────────
def test_a_banned_claim_in_the_profile_is_a_named_warning() -> None:
    warnings = site_service.publishing_warnings(_company(value_prop="Small-batch candles."))

    assert _codes(warnings) == ["W_BANNED_COPY_CLAIM"]
    warning = warnings[0]
    assert warning["fields"] == ["value_prop"]
    assert "small_batch" in warning["message"]
    assert "Small-batch candles." in warning["message"]  # the owner's own line, quoted back
    assert warning["how_to_fix"].strip() != ""
    assert "BRAND-KIT" in warning["how_to_fix"]


def test_every_banned_finding_gets_its_own_warning() -> None:
    warnings = site_service.publishing_warnings(
        _company(value_prop="Small-batch, non-toxic, hand-poured candles.")
    )

    copy = [warning for warning in warnings if warning["code"] == "W_BANNED_COPY_CLAIM"]
    assert len(copy) == 3, _codes(warnings)
    terms = {term for warning in copy for term in ("small_batch", "non_toxic", "hand_poured") if term in warning["message"]}
    assert terms == {"small_batch", "non_toxic", "hand_poured"}
    assert all(warning["fields"] == ["value_prop"] for warning in copy)


def test_a_banned_claim_inside_pricing_model_json_is_named_by_its_path() -> None:
    warnings = site_service.publishing_warnings(
        _company(
            pricing_model={
                "skus": [
                    {"sku": "CL-WSL-9", "name": "White Sage", "retail": 26,
                     "description": "Coconut-soy in a reusable tin."}
                ]
            }
        )
    )

    copy = [warning for warning in warnings if warning["code"] == "W_BANNED_COPY_CLAIM"]
    assert copy, "a claim inside the JSON catalogue is still published copy"
    fields = {field for warning in copy for field in warning["fields"]}
    assert fields == {"pricing_model.skus[0].description"}
    assert any("coconut_soy" in warning["message"] for warning in copy)
    assert any("tin_container" in warning["message"] for warning in copy)


# ── 2. a private value is warned and MASKED ─────────────────────────────────────────────────
def test_a_private_value_in_copy_is_a_masked_warning() -> None:
    warnings = site_service.publishing_warnings(
        _company(description="We ship from 42 Example Lane every Tuesday.")
    )

    private = [warning for warning in warnings if warning["code"] == "W_PUBLISHED_PRIVATE_VALUE"]
    assert len(private) == 1, _codes(warnings)
    warning = private[0]
    assert warning["fields"] == ["description"]
    assert "owner_home_street" in warning["message"]
    # The value itself is never reproduced — the finding is masked (AGENTS §27 / contract §9.4).
    serialized = json.dumps(warnings)
    assert "42 Example Lane" not in serialized
    assert "42 Example" not in serialized


def test_the_contact_identity_is_not_repeated_as_a_private_copy_warning() -> None:
    """`W_PUBLIC_CONTACT_DETAILS` already says the legal name/address go public verbatim."""
    warnings = site_service.publishing_warnings(
        _company(
            sender_postal_address=ADDRESS,
            sender_legal_name=LEGAL,
            notification_email=EMAIL,
        )
    )

    assert _codes(warnings) == ["W_PUBLIC_CONTACT_DETAILS"]
    assert warnings[0]["fields"] == [
        "sender_postal_address",
        "sender_legal_name",
        "notification_email",
    ]


def test_a_private_value_outside_the_contact_fields_still_warns() -> None:
    warnings = site_service.publishing_warnings(
        _company(
            sender_postal_address=ADDRESS,
            sender_legal_name=LEGAL,
            mission="Ask us: 42 Example Lane, Springfield, IL 62704.",
        )
    )

    codes = _codes(warnings)
    assert codes[0] == "W_PUBLIC_CONTACT_DETAILS"  # the contact warning comes first
    assert "W_PUBLISHED_PRIVATE_VALUE" in codes[1:]
    private_fields = {
        field
        for warning in warnings
        if warning["code"] == "W_PUBLISHED_PRIVATE_VALUE"
        for field in warning["fields"]
    }
    assert private_fields == {"mission"}


# ── 3. unverified is not blocking ───────────────────────────────────────────────────────────
def test_an_unverified_claim_is_reported_by_the_census_but_never_warns() -> None:
    company = _company(mission="Cold-process soap follows in a later wave.")
    summary = summarise(census_profile(company))
    assert summary["unverified"] >= 1, "the census DID find the H4 watch — the warning class is the point"

    assert site_service.publishing_warnings(company) == []


# ── 4. one clean profile stays [] and one banned profile still creates the site ─────────────
def test_a_clean_profile_still_returns_the_honest_empty_list() -> None:
    assert site_service.publishing_warnings(_company()) == []


@pytest.mark.asyncio
async def test_the_channel_warns_and_does_not_gate_the_create(
    api_client, async_db_session, auth_headers, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(site_service, "SITES_ROOT", tmp_path)
    async_db_session.add(_company(value_prop="Small-batch candles."))
    await async_db_session.commit()

    resp = await api_client.post(
        "/api/v1/sites", json={"title": "Warned site"}, headers=auth_headers
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()

    # The three frozen keys are untouched; the warning channel is the fourth key.
    assert {"slug", "url", "bytes"} <= set(body)
    assert "W_PUBLISHED_PRIVATE_VALUE" not in _codes(body["publishing_warnings"])
    copy = [w for w in body["publishing_warnings"] if w["code"] == "W_BANNED_COPY_CLAIM"]
    assert len(copy) == 1
    assert set(copy[0]) == {"code", "message", "fields", "how_to_fix"}
    assert copy[0]["fields"] == ["value_prop"]

    # ADVISORY, not a gate: the site was written exactly as before.
    site_dir = tmp_path / body["slug"]
    for name in ("index.html", "about.html", "contact.html", "styles.css", "meta.json"):
        assert (site_dir / name).is_file(), name
    assert body["bytes"] > 0
