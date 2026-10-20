"""V29 follow-up G — prospect integrity: a refused PATCH must not half-apply, children stay.

Written RED-first against `9d427c7`:

**D-G2 `test_a_refused_patch_changes_nothing`.**  `update_prospect` applied each provided field
to the ORM row as it walked the dict, so a body like `{"email": ..., "company": "New Co",
"status": "  "}` mutated `email`/`company` BEFORE the blank-status refusal was raised.  The
module promises two things that together make this a data-integrity bug: "the incoming status is
validated BEFORE the row is touched" and "the session survives a refused write … the caller's
session is usable immediately afterwards".  A caller that keeps using that session (the very
reason the rollback branches exist) then commits a half-applied edit that no request asked for,
and no refusal was reported for those fields.  Measured: after the 422, the stored company was
`New Co`.  The fix validates and normalises EVERY provided value before it touches the row.

The remaining tests pin the V27/V29 contract so it cannot regress silently:

* a blank status is a coded `E_VALIDATION` (`context.field == "status"`) on create and patch,
  `null` clears to "unrecorded", and a real word is trimmed and stored;
* a legacy free-text status still reads, filters by exact equality and survives an unrelated
  PATCH (the column is an open string, never an enum this surface invents);
* a prospect the outreach agent has emailed is left as RESIDUAL — the FK enabled in the test
  fixture refuses the delete, both the row and its `email_logs` survive, and the count in the
  refusal is the real one.

DOUBLES: none.  Real SQLite through the shared fixture, real service calls and routes.
"""
from __future__ import annotations

import pytest
from sqlalchemy import func, select, text

from app.core.errors import E_VALIDATION, PolsiaError
from app.models.outreach import EmailLog, Prospect
from app.services import prospect_service

pytestmark = pytest.mark.asyncio


async def test_a_refused_patch_changes_nothing(async_db_session) -> None:
    """Every field is validated before the row is touched — a 422 leaves the row intact."""
    row = Prospect(email="atomic@example.com", company="Old Co", status="new")
    async_db_session.add(row)
    await async_db_session.commit()

    with pytest.raises(PolsiaError) as excinfo:
        await prospect_service.update_prospect(
            async_db_session,
            row.id,
            {"email": "  New@Example.com ", "company": "New Co", "status": "   "},
        )

    refusal = excinfo.value
    assert refusal.code == E_VALIDATION
    assert refusal.context["field"] == "status"

    # The module promises a usable session after a refusal and exactly one write per request:
    # the caller's next ORDINARY commit must not persist a half-applied edit.
    await async_db_session.commit()
    await async_db_session.refresh(row)
    assert row.email == "atomic@example.com", "the refused patch stored the new address"
    assert row.company == "Old Co", "the refused patch stored the new company"
    assert row.status == "new", "the refused patch touched the status word"


async def test_blank_status_is_coded_on_create_and_patch_and_null_is_unrecorded(
    api_client, auth_headers
) -> None:
    """`""`/whitespace is caller error; `null` means "unrecorded"; a word is trimmed and stored."""
    refused_create = await api_client.post(
        "/api/v1/prospects",
        json={"email": "v29g-blank-create@example.com", "status": "   "},
        headers=auth_headers,
    )
    assert refused_create.status_code == 422, refused_create.text
    error = refused_create.json()["error"]
    assert error["code"] == E_VALIDATION
    assert error["context"]["field"] == "status"

    listed = await api_client.get(
        "/api/v1/prospects",
        params={"search": "v29g-blank-create@example.com"},
        headers=auth_headers,
    )
    assert listed.json()["items"] == [], "a refused create must not leave a row behind"

    created = await api_client.post(
        "/api/v1/prospects",
        json={"email": "v29g-blank-patch@example.com", "status": "new"},
        headers=auth_headers,
    )
    assert created.status_code == 201, created.text
    prospect_id = created.json()["id"]

    refused_patch = await api_client.patch(
        f"/api/v1/prospects/{prospect_id}", json={"status": ""}, headers=auth_headers
    )
    assert refused_patch.status_code == 422, refused_patch.text
    assert refused_patch.json()["error"]["code"] == E_VALIDATION
    assert refused_patch.json()["error"]["context"]["field"] == "status"

    cleared = await api_client.patch(
        f"/api/v1/prospects/{prospect_id}", json={"status": None}, headers=auth_headers
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["status"] is None, "an explicit null is 'unrecorded', not a refusal"

    stored = await api_client.patch(
        f"/api/v1/prospects/{prospect_id}", json={"status": "  contacted  "}, headers=auth_headers
    )
    assert stored.status_code == 200, stored.text
    assert stored.json()["status"] == "contacted"


async def test_a_legacy_status_word_reads_filters_and_survives_an_unrelated_patch(
    api_client, async_db_session, auth_headers
) -> None:
    """The column is an open string: existing free text must keep working, not be enum-swept."""
    async_db_session.add(Prospect(email="v29g-legacy@example.com", status="met at a conference"))
    await async_db_session.commit()

    listed = await api_client.get(
        "/api/v1/prospects", params={"status": "met at a conference"}, headers=auth_headers
    )
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["total"] == 1
    assert body["items"][0]["status"] == "met at a conference"

    unrelated = await api_client.get(
        "/api/v1/prospects", params={"status": "contacted"}, headers=auth_headers
    )
    assert unrelated.json()["total"] == 0, "the status filter is EXACT equality"

    patched = await api_client.patch(
        f"/api/v1/prospects/{body['items'][0]['id']}",
        json={"company": "Acme"},
        headers=auth_headers,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["status"] == "met at a conference", (
        "an unrelated PATCH must not re-interpret or drop a legacy status word"
    )


async def test_a_prospect_with_send_history_is_left_as_residual_never_cascaded(
    async_db_session
) -> None:
    """The FK refuses the delete; the row AND its `email_logs` survive, with the real count."""
    enabled = (await async_db_session.execute(text("PRAGMA foreign_keys"))).scalar()
    assert enabled == 1, (
        "the test fixture must enforce SQLite foreign keys or this test proves nothing about "
        "the production (PostgreSQL) RESTRICT"
    )

    prospect = Prospect(email="v29g-residual@example.com", status="contacted")
    async_db_session.add(prospect)
    await async_db_session.flush()
    prospect_id = prospect.id
    async_db_session.add_all(
        [
            EmailLog(prospect_id=prospect_id, subject="First touch"),
            EmailLog(prospect_id=prospect_id, subject="Follow-up"),
        ]
    )
    await async_db_session.commit()

    with pytest.raises(PolsiaError) as excinfo:
        await prospect_service.delete_prospect(async_db_session, prospect_id)

    refusal = excinfo.value
    assert refusal.code == E_VALIDATION
    assert refusal.context["email_logs"] == 2
    assert refusal.context["constraint"] == "email_logs.prospect_id"

    # Nothing cascaded: the parent is still readable through the service and both children are
    # still there (a CASCADE would have taken them, a bare 500 would have left the session dead).
    survivor = await prospect_service.get_prospect(async_db_session, prospect_id)
    assert survivor.email == "v29g-residual@example.com"
    logs = (
        await async_db_session.execute(
            select(func.count()).select_from(EmailLog).where(EmailLog.prospect_id == prospect_id)
        )
    ).scalar_one()
    assert logs == 2, "the refused delete removed outreach history"
