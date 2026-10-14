"""V9/B3 — the follow-up chain: schema contract, ROLE guard, `follow_up_due` persistence.

Plan §9 B3 (frozen plan 474-490) + Appendix A6 (outreach playbook: 4-7 touches; first touch
≤80 words; ONE binary CTA; each follow-up adds a NEW angle; subject ≤60 chars truthful).

What this file pins (part 1 of 2):
* `EmailOutreachOutput.follow_ups` — OPTIONAL, default `[]`, additive: an output without the
  key validates, an output with it carries `{prospect_email, step, due_in_days, angle}`.
* the ROLE prompt (the V9-D2 placement correction: the prompt is the ROLE constant in
  `email_outreach/agent.py`, there is no prompt.md in this tree) — snapshot guards on the
  exact numbers as strings: the 80-word first-touch bound, the "one binary" CTA rule, the
  60-char subject bound, and `due_in_days` for the follow-up outputs.
* `outreach_service.record_sent_emails` ALSO persists `email_logs.follow_up_due` from the
  output's `follow_ups[].due_in_days` (earliest entry per prospect wins; nothing stated ->
  the column stays NULL, never guessed).

Part 2 (the every-3h sweep: dedupe `followup:<log id>`, replied-suppression, beat entry) is
pinned in this same file below the chain-persistence tests.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.agents.email_outreach.agent import ROLE
from app.models.outreach import EmailLog
from app.schemas.agent_outputs import EmailOutreachOutput
from app.services import outreach_service

NOW = datetime(2026, 10, 14, 9, 0, 0, tzinfo=UTC)


# ── schema: additive, optional, default [] ───────────────────────────────────────────────────


def test_follow_ups_is_optional_defaults_to_empty_and_carries_the_frozen_shape() -> None:
    """Old payloads validate unchanged; new payloads carry the four frozen keys."""
    without = EmailOutreachOutput(summary="no chain stated")
    assert without.follow_ups == []

    entry = {
        "prospect_email": "p@example.com",
        "step": 2,
        "due_in_days": 3,
        "angle": "their new pricing page",
    }
    with_chain = EmailOutreachOutput(summary="chain", follow_ups=[entry])
    (parsed,) = with_chain.follow_ups
    assert parsed.prospect_email == "p@example.com"
    assert parsed.step == 2
    assert parsed.due_in_days == 3
    assert parsed.angle == "their new pricing page"
    # the CLI bridge sees the field (the schema IS the contract sent to the model)
    from app.schemas.agent_outputs import cli_schema_for

    assert "follow_ups" in cli_schema_for("email_outreach")["properties"]


# ── ROLE prompt snapshot guards (V9-D2: ROLE constant, not prompt.md) ────────────────────────


def test_role_prompt_carries_the_v9_outreach_rules() -> None:
    """The exact numbers as STRINGS — the plan's own guard shape ("80", "one binary")."""
    assert "80" in ROLE                 # first touch word bound
    assert "one binary" in ROLE         # ONE binary CTA, verbatim phrase
    assert "60" in ROLE                 # subject character bound
    assert "lowercase" in ROLE          # lowercase-truthful subjects
    assert "due_in_days" in ROLE        # follow_up outputs carry the field
    assert "NEW angle" in ROLE          # each follow-up adds a new angle
    assert "120" not in ROLE            # the old first-touch bound is gone


# ── record_sent_emails: follow_up_due from due_in_days ───────────────────────────────────────


async def test_record_sent_emails_persists_follow_up_due_from_the_outputs_due_in_days(
    async_db_session, monkeypatch,
) -> None:
    """A performed send whose output states a chain gets `follow_up_due` = sent_at + days.

    Two performed sends: the first prospect's chain states due_in_days 3 AND 7 (a later step)
    -> the EARLIEST wins (3); the second prospect's output states no chain -> NULL, never a
    guessed due date.
    """
    monkeypatch.setattr(outreach_service, "utcnow", lambda: NOW)
    db = async_db_session

    out = EmailOutreachOutput(
        summary="sent",
        action="send",
        emails=[
            {"prospect_email": "chain@example.com", "subject": "s1", "body": "b1",
             "status": "sent", "sendgrid_id": "sg-chain-1"},
            {"prospect_email": "cold@example.com", "subject": "s2", "body": "b2",
             "status": "sent", "sendgrid_id": "sg-cold-1"},
        ],
        follow_ups=[
            {"prospect_email": "chain@example.com", "step": 2, "due_in_days": 3,
             "angle": "timing"},
            {"prospect_email": "chain@example.com", "step": 3, "due_in_days": 7,
             "angle": "case study"},
        ],
    ).model_dump()

    report = await outreach_service.record_sent_emails(db, out)
    assert (report["ok"], report["emails_written"]) == (True, 2)

    rows = {
        log.sendgrid_id: log
        for log in (await db.execute(select(EmailLog))).scalars().all()
    }
    # SQLite stores naive UTC text (as_bound_instant); compare against the same convention.
    assert rows["sg-chain-1"].follow_up_due == (NOW + timedelta(days=3)).replace(tzinfo=None)
    assert rows["sg-cold-1"].follow_up_due is None
    # the row that OUR column was set on keeps the send instant on the same clock
    assert rows["sg-chain-1"].status == "sent"


async def test_record_sent_emails_leaves_follow_up_due_null_without_a_chain(
    async_db_session, monkeypatch,
) -> None:
    """An output with no follow_ups key at all (old producers/CS replies) writes NULL."""
    monkeypatch.setattr(outreach_service, "utcnow", lambda: NOW)
    db = async_db_session

    report = await outreach_service.record_sent_emails(db, {"emails": [
        {"status": "sent", "sendgrid_id": "sg-plain", "prospect_email": "plain@example.com",
         "subject": "s", "body": "b"},
    ]})
    assert report["emails_written"] == 1
    log = (await db.execute(select(EmailLog))).scalar_one()
    assert log.follow_up_due is None


# ── the every-3h sweep: ONE follow-up task per prospect, dedupe `followup:<log id>` ──────────
# File-backed DB + the Celery process loop via the shared `queue_db` fixture (the sweeps'
# house pattern: no Redis, no sockets; the broker hand-off is a no-op recorder).

from celery_app.beat_schedule import beat_schedule  # noqa: E402
from celery_app.tasks import followups  # noqa: E402

SWEEP_NOW = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)


def _noop_delay(monkeypatch) -> None:
    """The broker hand-off is not this test's subject: record the enqueue, touch no socket."""
    import celery_app.tasks.agent_tasks as agent_tasks

    monkeypatch.setattr(agent_tasks.run_agent_task, "delay", lambda tid: None, raising=False)


def _seed_due_log(
    queue_db,
    *,
    address: str,
    step: int = 1,
    due_offset_hours: float = -1.0,
    status: str = "sent",
    replied: bool = False,
    prospect_id: int | None = None,
) -> tuple[int, int]:
    """One `email_logs` row on the real schema (optionally on an EXISTING prospect).

    Returns (prospect_id, log_id); a fresh prospect is created when `prospect_id` is None.
    """
    from app.core.database import get_session_factory
    from app.core.timeutil import as_bound_instant
    from app.models.outreach import EmailLog, Prospect

    async def _seed() -> tuple[int, int]:
        async with get_session_factory()() as db:
            if prospect_id is None:
                prospect = Prospect(email=address, status="new")
                db.add(prospect)
                await db.flush()
                pid = int(prospect.id)
            else:
                pid = prospect_id
            log = EmailLog(
                prospect_id=pid,
                sequence_step=step,
                subject=f"s{step}",
                body="b",
                status=status,
                sent_at=as_bound_instant(SWEEP_NOW - timedelta(days=4), db),
                follow_up_due=as_bound_instant(
                    SWEEP_NOW + timedelta(hours=due_offset_hours), db
                ),
                replied_at=as_bound_instant(SWEEP_NOW - timedelta(hours=2), db) if replied else None,
            )
            db.add(log)
            await db.commit()
            return pid, int(log.id)

    return queue_db.run(_seed())


def test_the_sweep_creates_one_follow_up_task_and_the_same_log_never_fires_twice(
    queue_db, monkeypatch,
) -> None:
    """The pinned dedupe: same log id twice -> ONE task, key `followup:<log id>`."""
    monkeypatch.setattr(followups, "now_utc", lambda: SWEEP_NOW)
    _noop_delay(monkeypatch)
    _pid, log_id = _seed_due_log(queue_db, address="chain@example.com")

    first = queue_db.run(followups._run_follow_up_sweep())
    assert first == {"status": "completed", "due": 1, "created": 1, "deduped": 0,
                     "task_ids": first["task_ids"]}
    assert first["task_ids"] and len(first["task_ids"]) == 1

    rows = queue_db.query("SELECT * FROM tasks")
    assert len(rows) == 1
    assert rows[0]["dedupe_key"] == f"followup:{log_id}"
    assert followups.follow_up_dedupe_key(log_id) == f"followup:{log_id}"
    assert rows[0]["agent_type"] == "email_outreach"
    assert "chain@example.com" in rows[0]["title"]

    # Second tick, same due log: the key exists, so the chain does NOT double-fire.
    second = queue_db.run(followups._run_follow_up_sweep())
    assert (second["due"], second["created"], second["deduped"]) == (1, 0, 1)
    assert second["task_ids"] == []
    assert queue_db.count("tasks") == 1


def test_the_sweep_is_one_task_per_prospect_and_the_next_step_is_not_starved(
    queue_db, monkeypatch,
) -> None:
    """Two due steps for one prospect: tick 1 fires step 2; tick 2 fires step 3 — never two
    tasks in one tick, never a step that can't ever fire because an older one idles forever."""
    monkeypatch.setattr(followups, "now_utc", lambda: SWEEP_NOW)
    _noop_delay(monkeypatch)
    pid, older_id = _seed_due_log(
        queue_db, address="chain@example.com", step=1, due_offset_hours=-5.0
    )
    _pid, newer_id = _seed_due_log(
        queue_db, address="chain@example.com", step=2, due_offset_hours=-1.0,
        prospect_id=pid,
    )
    assert older_id != newer_id

    first = queue_db.run(followups._run_follow_up_sweep())
    assert (first["due"], first["created"]) == (2, 1)         # ONE per prospect per tick
    assert queue_db.query("SELECT dedupe_key FROM tasks") == [
        {"dedupe_key": f"followup:{older_id}"}
    ]

    second = queue_db.run(followups._run_follow_up_sweep())
    assert (second["created"], second["deduped"]) == (1, 1)   # the newer step now fires
    keys = {row["dedupe_key"] for row in queue_db.query("SELECT dedupe_key FROM tasks")}
    assert keys == {f"followup:{older_id}", f"followup:{newer_id}"}


def test_a_replied_or_unsent_log_never_reaches_the_sweep(queue_db, monkeypatch) -> None:
    """`replied_at IS NULL` + `status = 'sent'` are the selection's two filters, proven."""
    monkeypatch.setattr(followups, "now_utc", lambda: SWEEP_NOW)
    _noop_delay(monkeypatch)
    _seed_due_log(queue_db, address="replied@example.com", replied=True)
    _seed_due_log(queue_db, address="draft@example.com", status="draft")

    result = queue_db.run(followups._run_follow_up_sweep())

    assert result == {"status": "no_due", "due": 0, "created": 0, "deduped": 0, "task_ids": []}
    assert queue_db.count("tasks") == 0


def test_beat_carries_the_follow_up_sweep_every_three_hours() -> None:
    """The V9-D4 entry, pinned: the task, the 3h cadence, a full-period expiry, and the task
    registering on the same app the moment its module is imported (the decorator side).

    The two registry lines (`celery_config.TASK_MODULES`, `worker.py`'s eager imports) that
    load this module in every worker/beat process are settle wiring — both files sit outside
    this seat's territory; the wave STATE.md carries the exact snippet.
    """
    from celery_app.worker import app as celery_app

    entry = beat_schedule["email-followups"]
    assert entry["task"] == "celery_app.tasks.followups.run_follow_up_sweep"
    assert entry["task"] in celery_app.tasks, "the module task must register on the same app"
    schedule = entry["schedule"]
    assert getattr(schedule, "_orig_minute", None) == 0
    assert getattr(schedule, "_orig_hour", None) == "*/3"
    assert entry["options"]["expires"] == 3 * 3600            # one full period
    assert entry["options"]["queue"] == "agents"
