"""WAVE V17 / L1 — E7 carried findings F-03 … F-06, one RED-first pin block per finding.

F-03 (MED) — the pause re-arm clears `paused_release` on broker ACCEPTANCE, and `.delay()`
returning only means the broker took the publish.  Accept-then-lose (Redis flush/restart without
persistence) left a NON-scheduler row `pending`, unclaimed, marker-less, and nothing ever fed it
again: the stranded-`pending` reconciler covered `source='scheduler'` rows only.  The fix extends
that net to a row that carries the re-arm's own evidence (`metadata.rearmed_at`) and is still
`pending`/unclaimed after a full window, whatever its source, at most once per window (the
existing `dispatched_at` stamp spaces the next attempt).

F-04 (LOW) — `_clear_paused_markers` was a read-modify-write of the whole `metadata` JSON with no
row lock: a concurrent writer's keys that landed between its SELECT and its COMMIT were silently
overwritten.  The pin drives a second, well-behaved writer (lock-before-read) to commit exactly
inside that window and requires both writers' disjoint keys to survive.

F-05 (LOW) — `claim_task` increments `attempts` on EVERY claim, and the V15 pause cycle makes a row
repeatedly claimable, so three pause/resume cycles burned the whole retry ladder and one later
lease expiry terminal-failed work that really ran zero times.

F-06 (LOW) — `integrations._day_window` is documented as the UTC day but used `date.today()`
(process-local), which shifts the Google Ads / Meta / Stripe provider windows on a non-UTC host.

Everything here runs the real services against the file-backed queue DB on the Celery process loop.
The ONLY stand-ins are the process boundaries: the broker hand-off (`.delay`) and, for F-06, the
clock seam (`today_utc`), which is exactly what the finding says the code must consult.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, date, datetime, timedelta

from celery_app.tasks import maintenance

WINDOW_S = maintenance.PENDING_RECONCILE_AFTER_SECONDS


# ── shared helpers ────────────────────────────────────────────────────────────────
def _seed_company(row_db, *, paused: bool) -> None:
    from app.models.company import CompanyConfig

    async def _go() -> None:
        async with row_db.factory() as db:
            db.add(CompanyConfig(name="E7 Co", dispatch_paused=paused))
            await db.commit()

    row_db.run(_go())


def _set_paused(row_db, paused: bool) -> None:
    from sqlalchemy import select

    from app.models.company import CompanyConfig

    async def _go() -> None:
        async with row_db.factory() as db:
            row = (await db.execute(select(CompanyConfig))).scalars().first()
            row.dispatch_paused = paused
            await db.commit()

    row_db.run(_go())


def _meta(row_db, task_id: int) -> dict:
    """The row's metadata as the FILE stores it (raw text -> dict), not the ORM identity map."""
    raw = row_db.row(task_id).get("metadata")
    if raw in (None, "null", ""):
        return {}
    return json.loads(raw) if isinstance(raw, str) else (raw or {})


def _set_meta(row_db, task_id: int, meta: dict | None) -> None:
    from sqlalchemy import select

    from app.models.task import Task

    async def _go() -> None:
        async with row_db.factory() as db:
            row = (await db.execute(select(Task).where(Task.id == task_id))).scalar_one()
            row.metadata_json = meta
            await db.commit()

    row_db.run(_go())


def _seed_pending(row_db, *, title: str, source: str, metadata: dict | None) -> int:
    from app.services.task_service import create_task

    async def _go() -> int:
        async with row_db.factory() as db:
            row = await create_task(
                db, title=title, agent_type="email_outreach", source=source
            )
            row.metadata_json = metadata
            await db.commit()
            return int(row.id)

    return row_db.run(_go())


class DelayRecorder:
    """`run_agent_task` stand-in at the broker edge: records `.delay`, contacts no broker."""

    def __init__(self) -> None:
        self.delivered: list[int] = []

    def delay(self, task_id: int) -> None:
        self.delivered.append(int(task_id))


def _install_delay(monkeypatch) -> DelayRecorder:
    rec = DelayRecorder()
    monkeypatch.setattr("celery_app.tasks.agent_tasks.run_agent_task", rec)
    return rec


# ══ F-03 ═════════════════════════════════════════════════════════════════════════
def test_an_accept_then_lost_rearm_is_redispatched_exactly_once(queue_db, monkeypatch):
    """The resumed row whose message the broker accepted then lost is fed again — once."""
    from celery_app.tasks.agent_tasks import run_agent_task as real_task

    _seed_company(queue_db, paused=True)
    task_id = _seed_pending(
        queue_db, title="manual run queued over a pause", source="manual", metadata=None
    )
    assert real_task(task_id)["task_status"] == "pending"  # claimed, then released by the gate

    _set_paused(queue_db, False)
    rec = _install_delay(monkeypatch)

    # Resume: the re-arm hands the row off and clears the marker on ACCEPTANCE.
    assert queue_db.run(maintenance._rearm_paused_released_tasks())["rearmed"] == [task_id]
    assert rec.delivered == [task_id]
    meta = _meta(queue_db, task_id)
    assert "paused_release" not in meta and meta.get("rearmed_at")
    assert queue_db.row(task_id)["status"] == "pending"

    # The accepted message is LOST (nothing ever claims the row). Inside the window the
    # reconciler must not race the live hand-off ...
    inside = queue_db.run(maintenance._reconcile_stranded_pending())
    assert rec.delivered == [task_id], "a fresh re-arm is a live hand-off, not a stranded row"
    assert inside["redispatched"] == 0

    # ... and once a full window has passed with no claim, it is re-dispatched EXACTLY once.
    stale = dict(meta)
    stale["rearmed_at"] = (datetime.now(UTC) - timedelta(seconds=WINDOW_S + 60)).isoformat()
    _set_meta(queue_db, task_id, stale)

    after = queue_db.run(maintenance._reconcile_stranded_pending())
    assert rec.delivered == [task_id, task_id], "the lost non-scheduler row must be re-fed"
    assert after["redispatched"] == 1
    assert _meta(queue_db, task_id).get("dispatched_at"), "the redispatch is evidence, not a guess"

    # Exactly once per window: a second tick inside the new window sends nothing.
    queue_db.run(maintenance._reconcile_stranded_pending())
    assert rec.delivered == [task_id, task_id]
    assert queue_db.row(task_id)["status"] == "pending"  # a hand-off, never a fake completion


def test_a_claimed_or_finished_row_is_never_redispatched(queue_db, monkeypatch):
    """Old re-arm evidence is not a licence to re-feed work somebody already took."""
    from sqlalchemy import select

    from app.models.task import Task

    old = (datetime.now(UTC) - timedelta(seconds=WINDOW_S + 60)).isoformat()
    rec = _install_delay(monkeypatch)

    claimed = _seed_pending(
        queue_db, title="claimed after re-arm", source="manual", metadata={"rearmed_at": old}
    )
    queue_db.claim(claimed, "worker-alive")

    finished = _seed_pending(
        queue_db, title="finished after re-arm", source="chat", metadata={"rearmed_at": old}
    )

    async def _finish() -> None:
        async with queue_db.factory() as db:
            row = (await db.execute(select(Task).where(Task.id == finished))).scalar_one()
            row.status = "completed"
            await db.commit()

    queue_db.run(_finish())

    result = queue_db.run(maintenance._reconcile_stranded_pending())

    assert rec.delivered == [], "a claimed or finished row must never be handed off again"
    assert result["redispatched"] == 0
    assert queue_db.row(claimed)["status"] == "in_progress"
    assert queue_db.row(finished)["status"] == "completed"


def test_malformed_or_absent_rearm_evidence_is_skipped_not_crashed(queue_db, monkeypatch):
    """Hostile metadata: a non-ISO stamp and a NULL payload are skips, never an exception."""
    rec = _install_delay(monkeypatch)
    bad = _seed_pending(
        queue_db, title="broken stamp", source="manual", metadata={"rearmed_at": "not-a-date"}
    )
    empty = _seed_pending(queue_db, title="no metadata at all", source="manual", metadata=None)

    result = queue_db.run(maintenance._reconcile_stranded_pending())

    assert rec.delivered == []
    assert result["status"] == "completed"
    assert queue_db.row(bad)["status"] == "pending"
    assert queue_db.row(empty)["status"] == "pending"


# ══ F-04 ═════════════════════════════════════════════════════════════════════════
class _HookedSession:
    """The clear's session with ONE hook: run writer B after its first statement.

    Lets the pin drive the exact interleaving the finding describes (B commits inside the
    clear's read->write window) without adding a seam to the production function.
    """

    def __init__(self, real, hook) -> None:
        self._real = real
        self._hook = hook
        self._fired = False

    async def __aenter__(self):
        await self._real.__aenter__()
        return self

    async def __aexit__(self, *exc):
        return await self._real.__aexit__(*exc)

    async def execute(self, stmt, *args, **kwargs):
        result = await self._real.execute(stmt, *args, **kwargs)
        if not self._fired:
            self._fired = True
            await self._hook(stmt)
        return result

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_clearing_a_paused_marker_cannot_clobber_a_concurrent_writers_keys(queue_db, monkeypatch):
    """Two writers, disjoint keys, both must survive the clear (no lost update)."""
    from app.core import database
    from app.models.task import Task

    # WAL: a reader and a writer can hold the file at the same time, which is what makes the
    # read->write window real instead of serialised by SQLite's rollback-journal lock ladder.
    con = sqlite3.connect(str(queue_db.path))
    try:
        assert con.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
    finally:
        con.close()

    task_id = _seed_pending(
        queue_db,
        title="two writers",
        source="manual",
        metadata={
            "paused_release": {"released_at": datetime.now(UTC).isoformat(), "reason": "paused"},
            "keep": "a",
        },
    )
    real_factory = database.get_session_factory()

    async def _writer_b() -> None:
        """A WELL-BEHAVED concurrent writer: it takes the row lock BEFORE reading the payload."""
        from sqlalchemy import select, update

        async with real_factory() as db:
            await db.execute(
                update(Task).where(Task.id == task_id).values(updated_at=Task.updated_at)
            )
            row = (
                await db.execute(select(Task).where(Task.id == task_id).with_for_update())
            ).scalar_one()
            merged = dict(row.metadata_json or {})
            merged["b_key"] = "b"
            row.metadata_json = merged
            await db.commit()

    pending: list[asyncio.Task] = []

    async def _hook(stmt) -> None:
        task = asyncio.create_task(_writer_b())
        if getattr(stmt, "is_select", False):
            # The clear read the payload without a lock: B's commit lands in its window.
            await task
        else:
            # The clear holds the write lock: B must serialise behind it.
            pending.append(task)

    class _HookedFactory:
        def __call__(self):
            return _HookedSession(real_factory(), _hook)

    monkeypatch.setattr(database, "get_session_factory", lambda: _HookedFactory())

    queue_db.run(maintenance._clear_paused_markers([task_id]))

    if pending:  # the fixed path: B is still waiting for the clear's lock
        from celery_app.tasks._common import get_process_loop

        async def _join() -> None:
            await asyncio.gather(*pending)

        asyncio.run_coroutine_threadsafe(_join(), get_process_loop()).result(timeout=60)

    meta = _meta(queue_db, task_id)
    assert meta.get("keep") == "a", "the clear must not drop a key it does not own"
    assert meta.get("b_key") == "b", "a concurrent writer's key must survive the clear"
    assert meta.get("rearmed_at"), "the clear still stamps its own evidence"
    assert "paused_release" not in meta, "the marker it DOES own is removed"


# ══ F-05 ═════════════════════════════════════════════════════════════════════════
def test_pause_cycles_do_not_burn_attempts_and_a_later_lease_expiry_stays_requeueable(queue_db):
    """Three pause/resume cycles are not three attempts: the retry ladder stays intact."""
    from app.config import settings
    from celery_app.tasks.agent_tasks import run_agent_task as real_task
    from celery_app.tasks.maintenance import requeue_stale_task_claims

    _seed_company(queue_db, paused=True)
    task_id = _seed_pending(
        queue_db, title="paused three times", source="manual", metadata=None
    )

    for _ in range(3):
        assert real_task(task_id)["task_status"] == "pending"
        assert queue_db.row(task_id)["status"] == "pending"

    assert queue_db.row(task_id)["attempts"] == 0, (
        "a pause release is not an execution: it must return the claim's increment"
    )

    _set_paused(queue_db, False)
    queue_db.claim(task_id, "worker-real")
    assert queue_db.row(task_id)["attempts"] == 1, "one real claim is one attempt"

    queue_db.backdate_heartbeat(task_id, seconds=settings.task_claim_lease_seconds + 60)
    requeue_stale_task_claims()

    row = queue_db.row(task_id)
    assert row["status"] == "pending", "one real attempt may not be the terminal one"
    assert not (row["error_message"] or ""), "no 'attempts exhausted' reason on a once-run task"
    assert row["attempts"] == 1


def test_a_plain_release_still_counts_as_an_attempt(queue_db):
    """The refund is scoped to pause releases — a graceful release keeps its attempt burned."""
    from app.services.task_service import release_claim

    task_id = _seed_pending(queue_db, title="plain release", source="test", metadata=None)
    queue_db.claim(task_id, "worker-1")

    async def _release() -> None:
        async with queue_db.factory() as db:
            await release_claim(db, task_id, "worker-1")
            await db.commit()

    queue_db.run(_release())

    row = queue_db.row(task_id)
    assert row["status"] == "pending" and row["claimed_by"] is None
    assert row["attempts"] == 1, "a non-pause release keeps the shipped attempts semantics"


def test_clearing_a_marker_on_a_null_payload_stamps_the_evidence(queue_db):
    """Hostile: a NULL metadata payload must be cleared into a fresh dict, not crash the tick."""
    task_id = _seed_pending(queue_db, title="no payload", source="manual", metadata=None)

    queue_db.run(maintenance._clear_paused_markers([task_id]))

    meta = _meta(queue_db, task_id)
    assert list(meta) == ["rearmed_at"], meta
    assert meta["rearmed_at"], "the acceptance instant is the row's own evidence"


def test_a_pause_release_refunds_a_missing_attempt_counter(queue_db):
    """Hostile: `attempts` NULL / already 0 must floor at 0, never go negative."""
    from sqlalchemy import select

    from app.models.task import Task
    from app.services.task_service import RELEASE_KIND_PAUSED, release_claim

    task_id = _seed_pending(queue_db, title="null attempts", source="manual", metadata=None)

    async def _go() -> None:
        async with queue_db.factory() as db:
            row = (await db.execute(select(Task).where(Task.id == task_id))).scalar_one()
            row.status = "in_progress"
            row.claimed_by = "worker-null"
            row.attempts = None
            await db.commit()

    queue_db.run(_go())

    async def _release() -> None:
        async with queue_db.factory() as db:
            await release_claim(db, task_id, "worker-null", release_kind=RELEASE_KIND_PAUSED)
            await db.commit()

    queue_db.run(_release())
    assert queue_db.row(task_id)["attempts"] == 0

    # And an unknown kind is a caller error, not a silent plain release.
    queue_db.claim(task_id, "worker-x")

    async def _bad() -> None:
        async with queue_db.factory() as db:
            await release_claim(db, task_id, "worker-x", release_kind="typo")

    try:
        queue_db.run(_bad())
    except ValueError as exc:
        assert "release_kind" in str(exc)
    else:  # pragma: no cover - a silent accept is the defect
        raise AssertionError("an unknown release_kind must be refused")


# ══ F-06 ═════════════════════════════════════════════════════════════════════════
def test_the_day_window_is_the_utc_day_not_the_process_local_day(monkeypatch):
    """The provider windows must move with UTC, not with the worker host's clock."""
    from app.agents import integrations

    local_day = date(2026, 1, 1)
    utc_day = date(2026, 1, 2)

    class _LocalDate(date):
        @classmethod
        def today(cls):  # the process-local "today" the finding measured
            return local_day

    monkeypatch.setattr(integrations, "date", _LocalDate, raising=True)
    monkeypatch.setattr(integrations, "today_utc", lambda: utc_day, raising=False)

    assert integrations._day_window(1) == (utc_day, utc_day), (
        "a one-day window is the UTC day, not the host's local day"
    )
    assert integrations._day_window(7) == (date(2025, 12, 27), utc_day)
    assert integrations._day_window(0) == (utc_day, utc_day), "days<=0 floors to one day"


def test_the_day_window_ends_on_the_live_utc_date():
    """The real clock: whatever the host timezone, the window's end is the UTC date."""
    from app.agents.integrations import _day_window

    assert _day_window(1)[1] == datetime.now(UTC).date()
