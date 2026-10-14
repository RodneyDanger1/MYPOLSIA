"""V9/B3 — replied-stamping: ONE reply stops the follow-up chain (R4 OUTREACH-3).

`support_inbox_service.submit_item` is the ingest.  When the sender of an ingested message
matches a stored prospect email, every not-yet-stamped `email_logs` row of that prospect gets
`replied_at` — the exact column the every-3h follow-up sweep (`celery_app/tasks/followups.py`)
filters on (`replied_at IS NULL`).  Pinned here:

* the stamp lands on EVERY unstamped log of the matched prospect (the whole chain stops);
* a previously stamped row keeps its original instant (a second reply is not a new stop);
* another prospect's rows are untouched, and an unknown sender stamps nothing (report shape
  `{"matched": False, "prospect_id": None, "stamped": 0}` — "no match" is an answer, not an error);
* a duplicate re-delivery of a known provider id does NOT restamp (the stored row is returned
  unchanged — a duplicate delivery is not an edit, the service's own doctrine);
* end to end on the file-backed queue DB: the chain fires once, the reply is ingested, and the
  next sweep finds NOTHING — "reply stops the chain", proven against the real sweep.

Doubles live only here: the fixed clock and the broker hand-off no-op (the sweeps' house
pattern).  Every row is written by the REAL service on the real schema.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.timeutil import as_bound_instant
from app.models.outreach import EmailLog, Prospect
from app.services import support_inbox_service
from app.services.support_inbox_service import stamp_prospect_replied, submit_item

NOW = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
EARLIER = datetime(2026, 10, 1, 8, 0, 0, tzinfo=UTC)
SWEEP_NOW = datetime(2026, 10, 14, 12, 30, 0, tzinfo=UTC)


def _log(prospect_id: int, db, *, replied_at: datetime | None = None, step: int = 1) -> EmailLog:
    """One sent log row on the real schema, chain live unless `replied_at` says otherwise."""
    return EmailLog(
        prospect_id=prospect_id,
        sequence_step=step,
        subject=f"s{step}",
        body="b",
        status="sent",
        sent_at=as_bound_instant(NOW - timedelta(days=5), db),
        follow_up_due=as_bound_instant(NOW - timedelta(hours=1), db),
        replied_at=as_bound_instant(replied_at, db) if replied_at is not None else None,
    )


# ── submit_item: the ingest stamps the matched prospect's chain ──────────────────────────────


async def test_an_ingested_reply_stamps_every_unstamped_log_of_the_matched_prospect(
    async_db_session, monkeypatch,
) -> None:
    monkeypatch.setattr(support_inbox_service, "now_utc", lambda: NOW)
    db = async_db_session
    chain = Prospect(email="chain@example.com", status="new")
    other = Prospect(email="other@example.com", status="new")
    db.add_all([chain, other])
    await db.flush()
    unstamped = _log(int(chain.id), db, step=1)
    stamped = _log(int(chain.id), db, step=2, replied_at=EARLIER)
    bystander = _log(int(other.id), db, step=1)
    db.add_all([unstamped, stamped, bystander])
    await db.commit()

    item = await submit_item(
        db,
        provider_message_id="reply-1",
        from_address="CHAIN@Example.COM",     # case-insensitive match on the stored address
        subject="Re: hello",
        body="sounds interesting — tell me more",
    )
    await db.commit()

    assert item.status == "pending"           # ingest itself is unchanged
    for row in (unstamped, stamped, bystander):
        await db.refresh(row)
    assert unstamped.replied_at == NOW.replace(tzinfo=None)   # stamped at the ingest instant
    assert stamped.replied_at == EARLIER.replace(tzinfo=None)  # original stop instant kept
    assert bystander.replied_at is None                        # another prospect: untouched


async def test_an_unknown_sender_stamps_nothing_and_reports_no_match(async_db_session) -> None:
    db = async_db_session
    prospect = Prospect(email="chain@example.com", status="new")
    db.add(prospect)
    await db.flush()
    log = _log(int(prospect.id), db)
    db.add(log)
    await db.commit()

    report = await stamp_prospect_replied(db, from_address="stranger@example.com")

    assert report == {"matched": False, "prospect_id": None, "stamped": 0}
    await db.refresh(log)
    assert log.replied_at is None

    # and through the ingest path: the item is stored, the chain stays live
    item = await submit_item(
        db, provider_message_id="stranger-1", from_address="stranger@example.com",
        subject="unrelated", body="hello",
    )
    await db.commit()
    await db.refresh(log)
    assert item.status == "pending" and log.replied_at is None


async def test_a_duplicate_delivery_does_not_restamp(async_db_session, monkeypatch) -> None:
    """Re-submitting a known provider id returns the STORED row and never touches the stamp."""
    monkeypatch.setattr(support_inbox_service, "now_utc", lambda: NOW)
    db = async_db_session
    prospect = Prospect(email="chain@example.com", status="new")
    db.add(prospect)
    await db.flush()
    log = _log(int(prospect.id), db)
    db.add(log)
    await db.commit()

    first = await submit_item(
        db, provider_message_id="dup-1", from_address="chain@example.com",
        subject="Re: hello", body="first delivery",
    )
    await db.commit()
    await db.refresh(log)
    assert log.replied_at == NOW.replace(tzinfo=None)

    # A human/backfill rewinds the stamp; the duplicate delivery must not overwrite it.
    log.replied_at = as_bound_instant(EARLIER, db)
    await db.commit()

    second = await submit_item(
        db, provider_message_id="dup-1", from_address="chain@example.com",
        subject="Re: hello", body="duplicate delivery",
    )
    await db.commit()
    await db.refresh(log)
    assert int(second.id) == int(first.id)
    assert log.replied_at == EARLIER.replace(tzinfo=None)


# ── end to end on the file-backed queue DB: the reply stops the chain, the sweep finds nothing ─


def test_reply_stops_the_chain_and_the_next_sweep_finds_nothing(queue_db, monkeypatch) -> None:
    """Chain live -> sweep fires it; reply ingested -> the SAME sweep finds nothing to do."""
    from app.core.database import get_session_factory
    from celery_app.tasks import followups

    monkeypatch.setattr(followups, "now_utc", lambda: SWEEP_NOW)
    import celery_app.tasks.agent_tasks as agent_tasks

    monkeypatch.setattr(agent_tasks.run_agent_task, "delay", lambda tid: None, raising=False)

    async def _seed() -> int:
        async with get_session_factory()() as db:
            prospect = Prospect(email="chain@example.com", status="new")
            db.add(prospect)
            await db.flush()
            log = EmailLog(
                prospect_id=prospect.id,
                sequence_step=1,
                subject="s1",
                body="b",
                status="sent",
                sent_at=as_bound_instant(SWEEP_NOW - timedelta(days=4), db),
                follow_up_due=as_bound_instant(SWEEP_NOW - timedelta(hours=1), db),
            )
            db.add(log)
            await db.commit()
            return int(log.id)

    async def _ingest_reply() -> None:
        async with get_session_factory()() as db:
            await submit_item(
                db, provider_message_id="reply-e2e-1", from_address="chain@example.com",
                subject="Re: hello", body="stop the sequence, I'm interested",
            )
            await db.commit()

    log_id = queue_db.run(_seed())

    live = queue_db.run(followups._run_follow_up_sweep())
    assert (live["due"], live["created"]) == (1, 1)      # the chain was real and due
    assert queue_db.query("SELECT dedupe_key FROM tasks") == [
        {"dedupe_key": f"followup:{log_id}"}
    ]

    queue_db.run(_ingest_reply())
    stamped = queue_db.query("SELECT replied_at FROM email_logs WHERE id = ?", (log_id,))
    assert stamped and stamped[0]["replied_at"] is not None

    after = queue_db.run(followups._run_follow_up_sweep())
    assert after == {"status": "no_due", "due": 0, "created": 0, "deduped": 0, "task_ids": []}
    assert queue_db.count("tasks") == 1                   # nothing new was created
