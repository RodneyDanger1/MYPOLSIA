"""V9/B2(e) worker hook pins: the orchestrator's 7-day quality snapshot + the weekly drop row.

The agent half (`weekly_quality_drop` in the orchestrator) COMPUTES the record; agents hold no DB
session, so `_with_agent_quality` supplies the snapshot BEFORE an orchestrator run and
`_log_quality_drop` writes the ONE weekly `agent_quality_drop` activity row on the run's own
transaction. Dedupe is ledger-based: any drop row within 7 days makes the write a no-op.

Evidence: revert drill on the dedupe — planting an always-write made the no-op assertion RED
verbatim (legs: wave-v9/red-hook-drill.txt then green-hook-drill.txt); restored byte-identical,
5/5 green.
"""
from __future__ import annotations

import sqlite3


def test_non_orchestrator_context_is_untouched(queue_db):
    from celery_app.tasks.agent_tasks import _with_agent_quality

    ctx = {"company": "x"}
    out = queue_db.run(_with_agent_quality(None, "social_media", ctx))

    assert out is ctx
    assert "agent_quality" not in out


def test_producer_failure_leaves_the_key_absent(queue_db, monkeypatch):
    import app.api.v1.ops as ops
    from celery_app.tasks.agent_tasks import _with_agent_quality

    async def _boom(db):  # noqa: ANN001, ANN202
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(ops, "_agent_quality_rows", _boom)
    out = queue_db.run(_with_agent_quality(None, "orchestrator", {"a": 1}))

    assert "agent_quality" not in out, "a failed producer must stay distinguishable from good data"


def test_orchestrator_gains_the_measured_snapshot(queue_db, monkeypatch):
    import app.api.v1.ops as ops
    from celery_app.tasks.agent_tasks import _with_agent_quality

    async def _rows(db):  # noqa: ANN001, ANN202
        return [
            {"agent_type": "social_media", "runs_7d": 4, "success_rate": 0.5},
            {"agent_type": "finance", "runs_7d": 2, "success_rate": None},
        ]

    monkeypatch.setattr(ops, "_agent_quality_rows", _rows)
    out = queue_db.run(_with_agent_quality(None, "orchestrator", {"a": 1}))

    assert out["agent_quality"] == {
        "social_media": {"success_rate": 0.5, "runs_7d": 4},
        "finance": {"success_rate": None, "runs_7d": 2},
    }
    assert out["a"] == 1, "copy-on-write: the input context stays intact"


def _drop() -> dict:
    return {"floor": 0.8, "line": "Weekly quality watch: social_media succeeded on 50% of its last-7-day runs", "agents": []}


def _write(queue_db, drop):
    from celery_app.tasks.agent_tasks import _log_quality_drop

    async def _do() -> bool:
        async with queue_db.factory() as db:
            written = await _log_quality_drop(db, "orchestrator", drop)
            await db.commit()
            return written

    return queue_db.run(_do())


def test_drop_row_written_once_per_week_then_again_after_backdating(queue_db):
    written_first = _write(queue_db, _drop())
    assert written_first is True
    assert queue_db.count("activity_log", "action = ?", ("agent_quality_drop",)) == 1

    written_again = _write(queue_db, _drop())
    assert written_again is False, "a drop row within 7 days must make the write a no-op"
    assert queue_db.count("activity_log", "action = ?", ("agent_quality_drop",)) == 1

    # Backdate the row 8 days — the same naive-UTC form sqlite stores — and the next write lands.
    con = sqlite3.connect(str(queue_db.path))
    try:
        con.execute(
            "UPDATE activity_log SET created_at = datetime('now', '-8 days') WHERE action = 'agent_quality_drop'"
        )
        con.commit()
    finally:
        con.close()

    written_after_week = _write(queue_db, _drop())
    assert written_after_week is True, "an 8-day-old row no longer suppresses the weekly record"
    assert queue_db.count("activity_log", "action = ?", ("agent_quality_drop",)) == 2


def test_non_dict_drop_is_a_noop(queue_db):
    from celery_app.tasks.agent_tasks import _log_quality_drop

    async def _do() -> bool:
        async with queue_db.factory() as db:
            return await _log_quality_drop(db, "orchestrator", None)

    assert queue_db.run(_do()) is False
    assert queue_db.count("activity_log", "action = ?", ("agent_quality_drop",)) == 0
