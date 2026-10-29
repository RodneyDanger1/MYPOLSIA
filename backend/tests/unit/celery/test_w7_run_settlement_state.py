"""WAVE 7 / logic-debugger — the RUN-SETTLEMENT state machine under a half-open attempt.

THE SCENARIO (the auth half-open analogue, in the ledger): a worker is suspended/hung past the
stale-run bound, so `maintenance.close_stale_agent_runs` closes its `agent_runs` row `cancelled`
with an `E_RUN_STALE` interruption record (`usage_json.interrupted`, `cost_scope="unmeasured"`).
The process then comes back and settles the SAME run as `completed` with a real duration and cost.
Two writers now describe one row, and the late write used to:
  * overwrite `status` with no record that the reaper had already ended it (the ledger's "the
    worker died mid-run" fact silently disappears), and
  * have `_apply_run_usage` REPLACE `usage_json`, deleting `interrupted` (and any in-flight usage
    snapshot) — so the row reads `completed` with a measured cost and none of the facts that say
    it was once declared dead.

THE CONTRACT PINNED HERE (wave 5's W5-LD-03 rule, one ledger over): the real outcome may land,
but the reaper's record is never erased silently — it is ANNOTATED as superseded (with the closed
status and the settle status), and a run that settles on its own path carries no such marker.

Reachability: `AGENT_RUN_STALE_AFTER_SECONDS` is 2 h against a 300 s provider cap, so this needs a
suspended process / stopped clock (a laptop lid, a paused container) — not a slow agent. It is a
latent correctness hole in the ledger, graded S3 (a terminal row whose own record contradicts it),
not a live defect in the shipped happy path.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from celery_app.tasks.agent_tasks import _apply_run_usage
from celery_app.tasks.maintenance import (
    AGENT_RUN_STALE_AFTER_SECONDS,
    _close_stale_runs_in_session,
)


def _create_running_run(queue_db, *, started_ago_s: int, usage: dict | None = None, task=None) -> int:
    from app.models.task import AgentRun

    task_id = task if task is not None else queue_db.create_task(title="run settlement state")

    async def _make() -> int:
        async with queue_db.factory() as db:
            run = AgentRun(
                task_id=task_id,
                agent_type="finance",
                status="running",
                started_at=datetime.now(UTC).replace(tzinfo=None)
                - timedelta(seconds=started_ago_s),
                usage_json=usage,
            )
            db.add(run)
            await db.commit()
            return int(run.id)

    return queue_db.run(_make())


def _run_row(queue_db, run_id: int) -> dict:
    rows = queue_db.query("SELECT * FROM agent_runs WHERE id = ?", (run_id,))
    return rows[0]


def _row_snapshot(queue_db, run_id: int):
    """The ORM row (for a writer call) read on the process loop."""
    from sqlalchemy import select

    from app.models.task import AgentRun

    async def _read():
        async with queue_db.factory() as db:
            row = (await db.execute(select(AgentRun).where(AgentRun.id == run_id))).scalar_one()
            await db.commit()  # the writer's own commit is the test's job here
            return row

    return queue_db.run(_read())


# ── 1. the stale close, then the late real settlement ────────────────────────
def test_a_late_completion_after_the_stale_close_is_recorded_not_silently_rewritten(queue_db):
    run_id = _create_running_run(
        queue_db,
        started_ago_s=AGENT_RUN_STALE_AFTER_SECONDS + 60,
        usage={"in_flight": {"provider_calls": 3, "usage": {"input_tokens": 120}}},
    )

    closed = queue_db.run(_close_stale_runs_in_session())
    assert closed["closed"] == [run_id]
    row = _run_row(queue_db, run_id)
    assert row["status"] == "cancelled"
    import json

    usage_at_close = json.loads(row["usage_json"]) if isinstance(row["usage_json"], str) else row["usage_json"]
    assert usage_at_close["interrupted"]["cost_scope"] == "unmeasured"

    # The worker comes back and settles the run for real, through the shipped settle path.
    from app.services.task_service import finish_agent_run

    async def _late_settle():
        async with queue_db.factory() as db:
            settled = await finish_agent_run(
                db,
                run_id,
                "completed",
                output={"summary": "the run really finished"},
                duration_secs=12.5,
                cost_usd=Decimal("0.0012"),
            )
            _apply_run_usage(settled, {"cost_usd": 0.0012, "usage": {"input_tokens": 120, "output_tokens": 30}})
            await db.commit()

    queue_db.run(_late_settle())

    row = _run_row(queue_db, run_id)
    assert row["status"] == "completed", "the run's real outcome is what the ledger now records"
    assert row["duration_secs"] == pytest.approx(12.5)
    usage = json.loads(row["usage_json"]) if isinstance(row["usage_json"], str) else row["usage_json"]
    # THE INVARIANT: the reaper's record survives, and says it was superseded by the settle.
    assert "interrupted" in usage, "the stale-close record must not be erased by the late settle"
    assert usage["interrupted"]["reason_code"] == "E_RUN_STALE"
    assert usage["interrupted"]["superseded_by_status"] == "completed"
    assert usage["interrupted"]["superseded_at"]
    assert usage["in_flight"]["provider_calls"] == 3, "the in-flight snapshot survives the merge"
    assert usage["settlement_superseded"]["closed_status"] == "cancelled"
    assert usage["settlement_superseded"]["settled_status"] == "completed"


def test_an_ordinary_settlement_carries_no_supersession_marker(queue_db):
    run_id = _create_running_run(queue_db, started_ago_s=5)

    from app.services.task_service import finish_agent_run

    async def _settle():
        async with queue_db.factory() as db:
            settled = await finish_agent_run(
                db, run_id, "completed", output={"summary": "ok"}, duration_secs=1.0,
                cost_usd=Decimal("0.0004"),
            )
            _apply_run_usage(settled, {"cost_usd": 0.0004, "usage": {"input_tokens": 10, "output_tokens": 5}})
            await db.commit()

    queue_db.run(_settle())
    row = _run_row(queue_db, run_id)
    assert row["status"] == "completed"
    import json

    usage = json.loads(row["usage_json"]) if isinstance(row["usage_json"], str) else row["usage_json"]
    assert "settlement_superseded" not in usage
    assert "interrupted" not in usage


# ── 2. `_apply_run_usage` is a MERGE, not a replacement ─────────────────────
def test_apply_run_usage_preserves_unknown_usage_keys(queue_db):
    """The settle path's accounting must never delete a key it did not write."""
    run_id = _create_running_run(
        queue_db,
        started_ago_s=5,
        usage={"interrupted": {"reason_code": "E_RUN_STALE"}, "in_flight": {"provider_calls": 2}},
    )

    from sqlalchemy import select

    from app.models.task import AgentRun

    async def _apply() -> dict:
        async with queue_db.factory() as db:
            row = (await db.execute(select(AgentRun).where(AgentRun.id == run_id))).scalar_one()
            _apply_run_usage(row, {"cost_usd": 0.001, "usage": {"input_tokens": 7, "output_tokens": 3}})
            snapshot = dict(row.usage_json or {})
            await db.commit()
            return snapshot

    usage = queue_db.run(_apply())
    assert usage["interrupted"]["reason_code"] == "E_RUN_STALE"
    assert usage["in_flight"]["provider_calls"] == 2
    assert usage["usage"]["input_tokens"] == 7
    assert usage["cost_usd"] == 0.001
