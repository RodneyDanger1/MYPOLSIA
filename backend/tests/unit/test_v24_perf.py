"""V24/L6 — backend hardening pins (RED-first lane).

Every test here is built on a REAL defect reproduced in this lane's receipt
(`build/ui-reverse/wave-v24/l6-backend-hardening/RECEIPT.md`), except the two blocks that are
marked PINS (behaviour that was already correct and must stay so).

The statement counter is a SQLAlchemy `before_cursor_execute` listener on the engine the
session actually uses — no mock, no inferred count: the number asserted is the number of
statements SQLite was asked to run.
"""
from __future__ import annotations

import contextlib
import pathlib
import sqlite3
from datetime import UTC, datetime, timedelta

import alembic.config as alembic_config
import pytest
from sqlalchemy import event, select, text

from alembic import command
from app.models.report import ActivityLog, DailyReport
from app.models.support import SupportInboxItem
from app.models.task import Task
from app.services.activity_service import get_recent_activity
from app.services.auto_mode_service import close_stale_sessions
from app.services.channel_service import channel_sends_snapshot
from app.services.report_service import get_daily_report
from app.services.support_inbox_service import attach_pending, submit_item

BACKEND = pathlib.Path(__file__).resolve().parents[2]


# ── the statement counter ────────────────────────────────────────────────────────────────────


@contextlib.contextmanager
def count_statements(session):
    """Collect every SQL statement the session's engine executes inside the block."""
    bind = session.get_bind()
    sync_engine = getattr(bind, "sync_engine", bind)
    seen: list[str] = []

    def _before(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        seen.append(statement)

    event.listen(sync_engine, "before_cursor_execute", _before)
    try:
        yield seen
    finally:
        event.remove(sync_engine, "before_cursor_execute", _before)


def _selects(statements: list[str]) -> list[str]:
    return [s for s in statements if s.lstrip().upper().startswith("SELECT")]


# ── RED 1: channel snapshot was 2 statements PER channel (14 for 7 channels) ─────────────────

SOCIAL_CHANNELS = ("twitter", "linkedin", "facebook", "instagram", "tiktok", "youtube")


async def _seed_social(db, now: datetime) -> None:
    from app.models.social import SocialPost

    rows = [
        # today + inside the trailing hour
        {"platform": "twitter", "status": "published", "published_at": now - timedelta(minutes=5)},
        {"platform": "twitter", "status": "published", "published_at": now - timedelta(minutes=15)},
        # today but NOT inside the trailing hour
        {"platform": "linkedin", "status": "published", "published_at": now - timedelta(hours=3)},
        # a draft is not a send
        {"platform": "twitter", "status": "draft", "published_at": now - timedelta(minutes=5)},
    ]
    for row in rows:
        db.add(SocialPost(content="bench", **row))
    await db.flush()


async def test_channel_snapshot_uses_two_statements_for_seven_channels(async_db_session):
    """The owner's /channels/caps page must not scale its SQL with the channel count."""
    now = datetime.now(UTC)
    await _seed_social(async_db_session, now)
    channels = [*SOCIAL_CHANNELS, "email"]

    with count_statements(async_db_session) as seen:
        snapshot = await channel_sends_snapshot(async_db_session, channels, now=now)

    assert set(snapshot) == set(channels)
    assert len(_selects(seen)) <= 2, (
        f"expected at most 2 SELECTs for {len(channels)} channels, saw {len(_selects(seen))}:\n"
        + "\n".join(_selects(seen))
    )


async def test_channel_snapshot_counts_are_per_channel_and_windowed(async_db_session):
    """Correctness pin for the rewrite: per-channel, day vs trailing hour, drafts excluded."""
    now = datetime.now(UTC)
    await _seed_social(async_db_session, now)

    snapshot = await channel_sends_snapshot(
        async_db_session, [*SOCIAL_CHANNELS, "email"], now=now
    )

    assert snapshot["twitter"] == {"today": 2, "hour": 2}
    assert snapshot["linkedin"] == {"today": 1, "hour": 0}
    assert snapshot["tiktok"] == {"today": 0, "hour": 0}, "a measurable channel with no sends is 0"
    assert snapshot["email"] == {"today": 0, "hour": 0}


async def test_an_unknown_channel_stays_null_not_zero(async_db_session):
    """UNKNOWN (no local send table) must stay distinguishable from a measured zero."""
    snapshot = await channel_sends_snapshot(async_db_session, ["github"], now=datetime.now(UTC))
    assert snapshot["github"] == {"today": None, "hour": None}


# ── RED 2: support attach did one dedupe SELECT per item + one id SELECT per hit ─────────────


async def test_attach_pending_dedupe_hits_cost_one_lookup_statement(async_db_session):
    """4 already-attached items: the sweep must not issue a SELECT per item."""
    from app.services.support_inbox_service import inbox_task_dedupe_key

    ids = [f"perf-{i}" for i in range(4)]
    for pid in ids:
        await submit_item(
            async_db_session,
            provider_message_id=pid,
            from_address=f"{pid}@example.com",
            subject="s",
            body="b",
        )
        async_db_session.add(
            Task(
                title=f"existing {pid}",
                agent_type="customer_support",
                status="pending",
                source="scheduler",
                dedupe_key=inbox_task_dedupe_key(pid),
            )
        )
    await async_db_session.flush()

    with count_statements(async_db_session) as seen:
        attached = await attach_pending(async_db_session)

    assert len(attached) == 4
    assert len(_selects(seen)) <= 3, (
        f"expected the items read + ONE dedupe prefetch, saw {len(_selects(seen))} SELECTs:\n"
        + "\n".join(_selects(seen))
    )
    items = (
        (await async_db_session.execute(select(SupportInboxItem).order_by(SupportInboxItem.id)))
        .scalars()
        .all()
    )
    assert all(item.status == "dispatched" and item.task_id is not None for item in items)


# ── RED 3: auto-mode close issued one SUM per session ────────────────────────────────────────


async def _seed_expired_sessions(db, count: int) -> list:
    from app.models.auto_mode import AutoModeSession
    from app.models.task import AgentRun

    now = datetime.now(UTC).replace(tzinfo=None)
    sessions = []
    for i in range(count):
        # NON-OVERLAPPING 24h windows, two days apart: each run belongs to exactly one window,
        # so the test can assert per-window attribution (an overlapping window legitimately
        # contains its neighbour's runs too, which would hide an attribution bug).
        started = now - timedelta(days=10 + i * 2)
        session = AutoModeSession(
            started_at=started,
            ends_at=started + timedelta(hours=24),
            tasks_run=0,
        )
        db.add(session)
        await db.flush()
        sessions.append(session)
        # one run INSIDE this session's window, priced differently per session
        db.add(
            AgentRun(
                agent_type="orchestrator",
                run_type="cycle",
                status="completed",
                started_at=started + timedelta(minutes=5),
                cost_usd=i + 1,
                attempt=1,
            )
        )
    await db.flush()
    return sessions


async def test_close_stale_sessions_sums_spend_in_one_statement(async_db_session):
    from app.services.auto_mode_service import spend_over_window

    sessions = await _seed_expired_sessions(async_db_session, 3)
    # the per-row reference answers the batch must reproduce, captured BEFORE the close writes
    expected = [
        await spend_over_window(async_db_session, row.started_at, row.ends_at) for row in sessions
    ]

    with count_statements(async_db_session) as seen:
        closed = await close_stale_sessions(async_db_session)

    assert len(closed) == 3
    assert len(_selects(seen)) <= 2, (
        f"expected 1 session read + 1 grouped spend sum, saw {len(_selects(seen))} SELECTs:\n"
        + "\n".join(_selects(seen))
    )
    # attribution is still per-window, not one pooled total — and equal to the per-row answer
    assert [float(row.spend_usd) for row in sessions] == [float(value) for value in expected]
    assert [float(row.spend_usd) for row in sessions] == [1.0, 2.0, 3.0]


# ── RED 4: malformed at-rest JSON in a REPORT feed was a bare 500 ─────────────────────────────


async def test_get_daily_report_survives_malformed_metrics_json(async_db_session):
    from datetime import date

    day = date(2026, 5, 4)
    async_db_session.add(DailyReport(report_date=day, morning_plan="plan"))
    await async_db_session.flush()
    await async_db_session.execute(
        text("UPDATE daily_reports SET metrics_snapshot = '{bad json' WHERE report_date = :d"),
        {"d": day.isoformat()},
    )
    await async_db_session.flush()
    async_db_session.expire_all()

    report = await get_daily_report(async_db_session, day)

    assert report is not None and report.report_date == day
    assert report.morning_plan == "plan", "honest columns beside the bad one still serve"
    assert report.metrics_snapshot is None, "junk at rest reads as NOT MEASURED, never invented"


# ── RED 5: malformed activity detail was a bare 500 on the dashboard feed ────────────────────

async def test_recent_activity_survives_malformed_detail_json(async_db_session):
    async_db_session.add(
        ActivityLog(agent_type="finance", action="x", summary="real summary", level="info")
    )
    await async_db_session.flush()
    await async_db_session.execute(
        text("UPDATE activity_log SET detail = '{bad json' WHERE summary = 'real summary'")
    )
    await async_db_session.flush()
    async_db_session.expire_all()

    rows = await get_recent_activity(async_db_session, limit=10)

    assert [row.summary for row in rows] == ["real summary"]
    assert rows[0].detail is None


# ── RED 6: migration 0012 — the hot filters had no usable index ──────────────────────────────

PERF_INDEXES = {
    "ix_chat_messages_created_at": "chat_messages",
    "ix_support_inbox_items_created_at": "support_inbox_items",
    "ix_support_inbox_items_status_created_at": "support_inbox_items",
    "ix_invoices_status_id": "invoices",
    "ix_activity_log_agent_action_created": "activity_log",
}


def _indexes(db_path: pathlib.Path) -> dict[str, str]:
    con = sqlite3.connect(db_path.as_posix())
    try:
        rows = con.execute(
            "SELECT name, tbl_name FROM sqlite_master WHERE type = 'index' AND tbl_name IN "
            "('chat_messages','support_inbox_items','invoices','activity_log')"
        ).fetchall()
        return {name: table for name, table in rows}
    finally:
        con.close()


def _plan(db_path: pathlib.Path, sql: str) -> str:
    con = sqlite3.connect(db_path.as_posix())
    try:
        return "\n".join(str(row) for row in con.execute("EXPLAIN QUERY PLAN " + sql))
    finally:
        con.close()


def test_migration_0012_adds_the_hot_indexes_and_round_trips(monkeypatch, tmp_path):
    from app.config import settings

    db_path = tmp_path / "v24_l6_0012.sqlite"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    monkeypatch.setattr(settings, "database_url", url)
    cfg = alembic_config.Config()
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)

    command.upgrade(cfg, "0011")
    before = _indexes(db_path)
    assert not (set(PERF_INDEXES) & set(before)), "RED: 0011 must not already carry them"

    command.upgrade(cfg, "0012")
    after = _indexes(db_path)
    for name, table in PERF_INDEXES.items():
        assert after.get(name) == table, f"{name} missing from {table} at head"
    con = sqlite3.connect(db_path.as_posix())
    try:
        assert con.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0012"
    finally:
        con.close()

    # the plan actually changes: no scan+sort for the chat feed / support list / invoice filter
    chat_plan = _plan(db_path, "SELECT * FROM chat_messages ORDER BY created_at DESC, id DESC LIMIT 50")
    support_plan = _plan(db_path, "SELECT * FROM support_inbox_items ORDER BY created_at DESC, id DESC LIMIT 20")
    invoices_plan = _plan(db_path, "SELECT * FROM invoices WHERE status='paid' ORDER BY id DESC LIMIT 100")
    assert "TEMP B-TREE" not in chat_plan, chat_plan
    assert "TEMP B-TREE" not in support_plan, support_plan
    assert "SCAN invoices" not in invoices_plan, invoices_plan

    # reversible: the exact same five names disappear, nothing else is touched
    command.downgrade(cfg, "0011")
    rolled_back = _indexes(db_path)
    for name in PERF_INDEXES:
        assert name not in rolled_back, f"{name} survived downgrade"

    command.upgrade(cfg, "0012")
    assert set(PERF_INDEXES) <= set(_indexes(db_path))


# ── PINS: already-correct behaviour this lane must not regress ───────────────────────────────


def test_every_outbound_http_client_in_services_declares_a_timeout():
    """Item 5 of the brief, pinned: no outbound call may be unbounded."""
    offenders = []
    for path in sorted((BACKEND / "app" / "services").glob("*.py")):
        text_body = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text_body.splitlines(), start=1):
            if "httpx.AsyncClient(" in line and "timeout=" not in line:
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
            if "_BRIDGE_OPENER.open(" in line and "timeout=" not in line:
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert offenders == [], "outbound HTTP without a timeout:\n" + "\n".join(offenders)


async def test_economics_timeout_is_a_named_source_failure(monkeypatch):
    import httpx as httpx_module

    from app.services import economics_service

    class _TimeoutClient:
        def __init__(self, *args, **kwargs):
            assert kwargs.get("timeout") == economics_service.SOURCE_TIMEOUT_SECONDS

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        async def get(self, url, headers=None):
            raise httpx_module.TimeoutException("fake timeout")

    monkeypatch.setattr(economics_service.httpx, "AsyncClient", _TimeoutClient)
    with pytest.raises(economics_service.SourceFetchError) as excinfo:
        await economics_service._get("https://example.invalid/x", accept="application/json")
    assert "timed out" in str(excinfo.value)


async def test_dashboard_summary_is_still_one_statement(async_db_session):
    """The V19 optimisation this lane must not undo: /dashboard/summary = ONE round trip."""
    from app.api.v1.dashboard import _summary_statement
    from app.services.query_helpers import fetch_one

    with count_statements(async_db_session) as seen:
        await fetch_one(async_db_session, _summary_statement(async_db_session), what="dash")
    assert len(seen) == 1, "\n".join(seen)


def test_the_migration_chain_has_one_head_and_0012_chains_after_0011():
    graph: dict[str, str | None] = {}
    for path in sorted((BACKEND / "alembic" / "versions").glob("*.py")):
        body = path.read_text(encoding="utf-8")
        revision = down = None
        for line in body.splitlines():
            if line.startswith("revision:"):
                revision = line.split("=", 1)[1].strip().strip('"')
            elif line.startswith("down_revision:"):
                down = line.split("=", 1)[1].strip().strip('"')
        assert revision, f"unparsable revision header: {path.name}"
        graph[revision] = None if down in (None, "None") else down
    heads = sorted(r for r in graph if r not in set(graph.values()))
    # V25: the head moved 0012 -> 0014 (L1's 0013_task_attachments, L4's 0014_ads_nullable_revenue);
    # V24's 0012 -> 0011 leg is unchanged and still asserted below.
    assert heads == ["0014"], f"expected a single head (0014), found {heads}"
    assert graph["0012"] == "0011"
    assert graph["0013"] == "0012" and graph["0014"] == "0013"
