"""Wave V28 / lane L2 — backend error-path sweep: the refusals that used to be bare 500s.

Every test here was written BEFORE the fix (RED), against `f0ce8a2`:

1. **`DELETE /api/v1/prospects/{id}` with outreach history.**  `prospects` is the parent of
   `email_logs.prospect_id`, and the PostgreSQL constraint is `ON DELETE RESTRICT` (alembic
   `0002_queue_and_perf.py:306`).  `delete_prospect` caught nothing, so an owner deleting a
   prospect the outreach agent had ever emailed got an uncoded 500 (`IntegrityError`) and — worse
   on the route path — a session left in a failed state.  The module's own docstring promises
   "nothing here raises a bare exception out of a route"; the service now answers the named
   `E_VALIDATION` with the row count, and the route answers 422 with the code in the envelope.

   SQLite does not enforce foreign keys unless asked, so these tests ASK
   (`PRAGMA foreign_keys=ON`) — the same enforcement PostgreSQL has by default.  Without the
   pragma the delete would silently succeed and the test would prove nothing.

2. **The public opt-out stored the recipient's typed case.**  `unsubscribe_prospect` created the
   prospect as typed (`Ada@Example.COM`) while every other writer stores `strip().lower()`, and
   the unique index on `prospects.email` is case-SENSITIVE — so one person could exist twice, one
   row unsubscribed and one not.  The opt-out now normalises exactly like the CRM.

3. **`POST /api/v1/sites` when the disk refuses a write.**  `generate_site` created the site
   directory and then wrote each file with a bare `Path.write_text`, so `ENOSPC` / `EACCES` /
   a full volume escaped as a raw `OSError` (500, no code), and the half-written directory stayed
   behind — `generate_site` refuses an existing directory as a slug collision, and `delete_site`
   refuses a directory with no `meta.json`, so that slug was PERMANENTLY wedged.  The service now
   writes atomically (the module's own `_write_text_atomic`), answers `E_STORAGE` naming the file,
   and removes the directory it created before raising, so the retry the owner is told to make
   can actually succeed.

Every defect is pinned at the service AND, where it has one, the HTTP boundary: a service-level
refusal that a route still turns into a 500 is not a fixed product.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from app.core.errors import E_STORAGE, E_VALIDATION, PolsiaError
from app.models.company import CompanyConfig
from app.models.outreach import EmailLog, Prospect
from app.services import prospect_service, site_service


def _company() -> CompanyConfig:
    return CompanyConfig(
        name="Acme Robotics",
        value_prop="Autonomous warehouse robots for small factories.",
        description="We build robots that do the boring lifting.",
    )


async def _enable_sqlite_foreign_keys(db) -> None:
    """Make SQLite enforce the FK the PostgreSQL schema enforces by default.

    `PRAGMA foreign_keys` is a no-op inside a transaction, so this is issued on the session's
    connection before the statement that needs it.  The assertion is the proof the next test
    failure is about the product, not about a silently-ignored pragma.
    """
    await db.execute(text("PRAGMA foreign_keys=ON"))
    enabled = (await db.execute(text("PRAGMA foreign_keys"))).scalar()
    assert enabled == 1, "SQLite refused to enable foreign keys; the test would prove nothing"


# ── 1. prospect delete vs. outreach history ──────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_deleting_a_prospect_with_send_history_is_a_named_refusal(
    async_db_session,
) -> None:
    prospect = Prospect(email="ada@example.com", first_name="Ada")
    async_db_session.add(prospect)
    await async_db_session.flush()
    prospect_id = prospect.id
    async_db_session.add(EmailLog(prospect_id=prospect_id, subject="Hello"))
    await async_db_session.commit()
    await _enable_sqlite_foreign_keys(async_db_session)

    with pytest.raises(PolsiaError) as excinfo:
        await prospect_service.delete_prospect(async_db_session, prospect_id)

    refusal = excinfo.value
    assert refusal.code == E_VALIDATION
    assert refusal.context["prospect_id"] == prospect_id
    assert refusal.context["email_logs"] == 1
    assert refusal.context["constraint"] == "email_logs.prospect_id"
    assert "unsubscribed" in refusal.how_to_fix

    # The refusal is an ANSWER, not a poisoned session: the row survived and is readable now.
    still_there = await prospect_service.get_prospect(async_db_session, prospect_id)
    assert still_there.email == "ada@example.com"


@pytest.mark.asyncio
async def test_delete_route_answers_the_named_refusal_when_send_history_exists(
    api_client, async_db_session, auth_headers
) -> None:
    prospect = Prospect(email="grace@example.com")
    async_db_session.add(prospect)
    await async_db_session.flush()
    prospect_id = prospect.id
    async_db_session.add(EmailLog(prospect_id=prospect_id, subject="Hello"))
    await async_db_session.commit()
    await _enable_sqlite_foreign_keys(async_db_session)

    response = await api_client.delete(
        f"/api/v1/prospects/{prospect_id}", headers=auth_headers
    )

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["error"]["code"] == E_VALIDATION
    assert body["error"]["context"]["prospect_id"] == prospect_id
    assert response.headers["X-Polsia-Error-Code"] == E_VALIDATION


@pytest.mark.asyncio
async def test_deleting_a_prospect_without_history_still_removes_it(async_db_session) -> None:
    """The pin that keeps the refusal from swallowing the normal delete."""
    prospect = Prospect(email="clean@example.com")
    async_db_session.add(prospect)
    await async_db_session.commit()

    removed = await prospect_service.delete_prospect(async_db_session, prospect.id)
    await async_db_session.commit()

    assert removed.id == prospect.id
    with pytest.raises(PolsiaError) as excinfo:
        await prospect_service.get_prospect(async_db_session, prospect.id)
    assert excinfo.value.code == "E_NOT_FOUND"


# ── 2. the opt-out producer must store the same address the CRM stores ──────────────────────
@pytest.mark.asyncio
async def test_an_opt_out_normalises_the_address_before_it_creates_a_prospect(
    async_db_session,
) -> None:
    """The public opt-out and the owner's CRM must agree on what one address IS.

    `record_sent_emails` stores `strip().lower()`, `prospect_service.normalize_email` stores
    `strip().lower()`, and `email_compliance` matches case-insensitively — but
    `unsubscribe_prospect` created the row with the recipient's TYPED case.  The unique index on
    `prospects.email` is case-SENSITIVE, so `Ada@Example.COM` and `ada@example.com` became two
    rows for one person: the owner's list shows a duplicate, one row subscribed and one not.
    """
    from app.services.outreach_service import unsubscribe_prospect

    result = await unsubscribe_prospect(async_db_session, "  Ada@Example.COM ")

    assert result["address"] == "ada@example.com"
    # One person, one prospect: the owner's create of the same address is now a named duplicate.
    with pytest.raises(PolsiaError) as excinfo:
        await prospect_service.create_prospect(async_db_session, email="ada@example.com")
    assert excinfo.value.code == E_VALIDATION
    assert excinfo.value.context["field"] == "email"


# ── 3. site create vs. a disk that refuses a write ───────────────────────────────────────────
def _fail_second_write(monkeypatch, original) -> None:
    """Make the SECOND file write of a site create fail like a full disk."""
    calls = {"count": 0}

    def flaky_write_text(self, *args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 2:
            raise OSError(28, "No space left on device")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", flaky_write_text)


@pytest.mark.asyncio
async def test_a_failed_site_write_is_e_storage_and_leaves_the_slug_reusable(
    async_db_session, tmp_path: Path, monkeypatch
) -> None:
    async_db_session.add(_company())
    await async_db_session.flush()
    original = Path.write_text
    _fail_second_write(monkeypatch, original)

    with pytest.raises(PolsiaError) as excinfo:
        await site_service.generate_site(
            async_db_session, root=tmp_path, slug="acme-site", title="Acme"
        )

    assert excinfo.value.code == E_STORAGE
    assert str(tmp_path / "acme-site") in excinfo.value.context["path"]
    # A create that failed left NOTHING behind: the slug is not a collision on retry.
    assert not (tmp_path / "acme-site").exists()

    monkeypatch.setattr(Path, "write_text", original)
    result = await site_service.generate_site(
        async_db_session, root=tmp_path, slug="acme-site", title="Acme"
    )
    assert result["slug"] == "acme-site"
    assert (tmp_path / "acme-site" / "meta.json").is_file()


@pytest.mark.asyncio
async def test_create_site_route_answers_e_storage_and_can_be_retried(
    api_client, async_db_session, auth_headers, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(site_service, "SITES_ROOT", tmp_path)
    async_db_session.add(_company())
    await async_db_session.commit()
    original = Path.write_text
    _fail_second_write(monkeypatch, original)

    response = await api_client.post(
        "/api/v1/sites", json={"title": "Disk Full"}, headers=auth_headers
    )

    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == E_STORAGE
    assert response.headers["X-Polsia-Error-Code"] == E_STORAGE
    assert not (tmp_path / "disk-full").exists()

    monkeypatch.setattr(Path, "write_text", original)
    retry = await api_client.post(
        "/api/v1/sites", json={"title": "Disk Full"}, headers=auth_headers
    )
    assert retry.status_code == 201, retry.text
    assert retry.json()["slug"] == "disk-full"
