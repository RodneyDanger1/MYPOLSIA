"""WAVE 7 — the follow-up chain honours the suppression list (opt-out precedence).

The every-3h sweep used to select a due step purely on `follow_up_due`, `replied_at IS NULL` and
`status='sent'`.  An address that had OPTED OUT therefore still got a follow-up task: the send
path would refuse it (`E_POLICY_DENIED`) but the chain had already minted work — one task per step,
every campaign, for a person who asked to be left alone.  Opt-out must outrank "a step is due",
and the skip must be COUNTED, never silent.

RED leg: `raw-w7/RED-G-followup-suppressed.xml` (remove the filter -> one task appears).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.timeutil import as_bound_instant

SWEEP_NOW = datetime(2026, 10, 29, 12, 30, 0, tzinfo=UTC)


def _seed_due_log(queue_db: Any, *, address: str, status: str) -> tuple[int, int]:
    """One due `email_logs` row owned by a prospect in `status` (the follow-up's input)."""
    from app.core.database import get_session_factory
    from app.models.outreach import EmailLog, Prospect

    async def _seed() -> tuple[int, int]:
        async with get_session_factory()() as db:
            prospect = Prospect(email=address, status=status)
            db.add(prospect)
            await db.flush()
            log = EmailLog(
                prospect_id=int(prospect.id),
                sequence_step=1,
                subject="s1",
                body="b",
                status="sent",
                sendgrid_id=f"sg-{address}",
                sent_at=as_bound_instant(SWEEP_NOW - timedelta(days=4), db),
                follow_up_due=as_bound_instant(SWEEP_NOW - timedelta(hours=1), db),
            )
            db.add(log)
            await db.commit()
            return int(prospect.id), int(log.id)

    return queue_db.run(_seed())


def _noop_delay(monkeypatch: Any) -> None:
    """The broker hand-off is a no-op (the sweeps' house pattern)."""
    import celery_app.tasks.followups as followups

    async def _noop(ids: list[int], *, label: str) -> None:  # pragma: no cover - not called
        return None

    monkeypatch.setattr(followups, "enqueue_created", _noop)

    async def _noop_mark(ids: list[int]) -> None:  # pragma: no cover - not called
        return None

    import celery_app.tasks.maintenance as maintenance

    monkeypatch.setattr(maintenance, "mark_tasks_dispatched", _noop_mark)


def test_an_opted_out_prospect_gets_no_follow_up_task_and_the_skip_is_counted(
    queue_db, monkeypatch,
) -> None:
    """RED LEG: the pre-fix sweep creates `followup:<id>` for the suppressed prospect."""
    import celery_app.tasks.followups as followups

    monkeypatch.setattr(followups, "now_utc", lambda: SWEEP_NOW)
    _noop_delay(monkeypatch)
    _seed_due_log(queue_db, address="opted-out@example.com", status="unsubscribed")

    result = queue_db.run(followups._run_follow_up_sweep())

    assert result == {
        "status": "no_due",
        "due": 0,
        "created": 0,
        "deduped": 0,
        "task_ids": [],
        "suppressed": 1,
    }
    assert queue_db.count("tasks") == 0


def test_a_live_prospect_still_gets_its_follow_up(queue_db, monkeypatch) -> None:
    """The pin that keeps the suppression filter from swallowing the normal chain."""
    import celery_app.tasks.followups as followups

    monkeypatch.setattr(followups, "now_utc", lambda: SWEEP_NOW)
    _noop_delay(monkeypatch)
    _pid, log_id = _seed_due_log(queue_db, address="live@example.com", status="contacted")

    result = queue_db.run(followups._run_follow_up_sweep())

    assert result["created"] == 1 and result["due"] == 1
    assert "suppressed" not in result                     # nothing withheld: no key invented
    rows = queue_db.query("SELECT dedupe_key FROM tasks")
    assert rows == [{"dedupe_key": f"followup:{log_id}"}]


def test_a_mixed_batch_fires_the_live_step_and_never_the_suppressed_one(
    queue_db, monkeypatch,
) -> None:
    """Both in one tick: the live prospect's step fires; the suppressed one is counted away."""
    import celery_app.tasks.followups as followups

    monkeypatch.setattr(followups, "now_utc", lambda: SWEEP_NOW)
    _noop_delay(monkeypatch)
    _seed_due_log(queue_db, address="live@example.com", status="new")
    _seed_due_log(queue_db, address="opted-out@example.com", status="unsubscribed")

    result = queue_db.run(followups._run_follow_up_sweep())

    assert (result["due"], result["created"]) == (1, 1)
    titles = [row["title"] for row in queue_db.query("SELECT title FROM tasks")]
    assert len(titles) == 1 and "live@example.com" in titles[0]
    assert "opted-out@example.com" not in titles[0]
