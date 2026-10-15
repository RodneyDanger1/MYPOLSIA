"""Test Celery agent task dispatch (without real broker).

The two tests at the top are the original pins (crew_factory dispatch + unknown agent).

Wave W3-refusals extends this file with the two WORKER HOOKS, driven end to end through
`_execute_agent_task` on the file-backed database from `conftest.py`:

  * `_with_channel_sends` (T5) — the only producer of `context["channel_sends"]`. Pinned here:
    the snapshot is requested for exactly the agent's own capability channels; an agent with no
    external channels gets nothing; a producer that cannot answer leaves the key ABSENT (warned);
  * `_persist_run_artifacts` + the persist-block wiring (T6/T5) — the run's artifacts are written
    on the run's own transaction BEFORE the run row is finished, the report lands in
    `agent_runs.output`, `snapshot_written` is folded back from the PERSISTED upsert, and a failed
    persistence is recorded in the output + an activity row instead of being swallowed.

The co-seat modules these hooks import (`app.services.channel_service`,
`finance_service.persist_finance_run`, …) may not exist on disk while this seat is running, so the
tests install the frozen shape themselves (`_install_channel_service`, `monkeypatch.setattr(…,
raising=False)`). Paths that cannot be exercised before those land are marked UNRUN in the receipt.
"""
import json
import logging
import sys
import types
from unittest.mock import patch

import pytest


@pytest.mark.asyncio
async def test_crew_factory_returns_result():
    """Test that crew_factory.run_agent_for_task dispatches correctly."""
    from app.agents.crew_factory import run_agent_for_task

    with patch("app.agents.social_media.agent.SocialMediaAgent.run") as mock_run:
        mock_run.return_value = {"summary": "Mocked result", "tweets": []}
        result = run_agent_for_task(
            "social_media",
            {"title": "Post tweets", "description": None},
            {"company": {"name": "Test Co"}},
        )
    assert result["summary"] == "Mocked result"


def test_crew_factory_raises_on_unknown_agent():
    from app.agents.crew_factory import run_agent_for_task
    with pytest.raises(ValueError, match="Unknown agent"):
        run_agent_for_task("nonexistent_agent", {}, {})


# ─────────────────────────────────────────────────────────────────────────────
# helpers — the per-call agent seam and the frozen co-seat shapes
# ─────────────────────────────────────────────────────────────────────────────
def _install_agent_spy(monkeypatch, result: dict) -> list[dict]:
    """Replace `crew_factory.run_agent_for_task` with a capture that returns `result`.

    The hook under test runs BEFORE the agent, so the captured `context` is the assertion target.
    """
    import app.agents.crew_factory as crew_factory

    calls: list[dict] = []

    def spy(agent_type, task_dict, context):
        calls.append({"agent_type": agent_type, "task_dict": task_dict, "context": context})
        return result

    monkeypatch.setattr(crew_factory, "run_agent_for_task", spy)
    return calls


def _install_channel_service(monkeypatch, snapshot_fn) -> None:
    """Install the S1 frozen shape: `channel_service.channel_sends_snapshot(db, channels, *, now=None)`."""
    module = types.ModuleType("app.services.channel_service")
    module.channel_sends_snapshot = snapshot_fn
    monkeypatch.setitem(sys.modules, "app.services.channel_service", module)


def _latest_run_output(queue_db) -> dict:
    """The run row as stored in the FILE (stdlib driver, no ORM identity map)."""
    rows = queue_db.query("SELECT output FROM agent_runs ORDER BY id DESC LIMIT 1")
    if not rows or not rows[0]["output"]:
        return {}
    return json.loads(rows[0]["output"])


# ─────────────────────────────────────────────────────────────────────────────
# T5 hook — channel_sends injection at the context build
# ─────────────────────────────────────────────────────────────────────────────
def test_worker_injects_channel_sends_for_the_agents_own_channels(
    monkeypatch, queue_db, execute_body
):
    """RED leg 6: without the injection, `channel_sends` is absent and this fails."""
    seen: dict = {}

    async def snapshot(db, channels, *, now=None):
        seen["channels"] = tuple(channels)
        seen["now"] = now
        return {channel: {"today": 2, "hour": 1} for channel in channels}

    _install_channel_service(monkeypatch, snapshot)
    calls = _install_agent_spy(monkeypatch, {"summary": "posted"})

    outcome = execute_body(queue_db.create_task(agent_type="social_media"))

    assert outcome["status"] == "completed"
    # U5/P1 pin move (old -> new): the capability gained the social platforms behind their
    # adapters (all six shipped; tiktok/youtube joined in V8/C4) — recorded in the
    # wave-u5/coder-p1-social receipt.
    assert seen["channels"] == ("twitter", "linkedin", "facebook", "instagram", "tiktok", "youtube")
    assert seen["now"] is None                            # the caller does not pin the clock
    assert calls[0]["context"]["channel_sends"] == {
        "twitter": {"today": 2, "hour": 1},
        "linkedin": {"today": 2, "hour": 1},
        "facebook": {"today": 2, "hour": 1},
        "instagram": {"today": 2, "hour": 1},
        "tiktok": {"today": 2, "hour": 1},
        "youtube": {"today": 2, "hour": 1},
    }


def test_worker_injects_nothing_for_an_agent_without_channels(
    monkeypatch, queue_db, execute_body
):
    """The orchestrator may not touch a channel: no snapshot, no key."""

    async def snapshot(db, channels, *, now=None):  # pragma: no cover - must never run
        raise AssertionError("no capability channels -> the snapshot must not be requested")

    _install_channel_service(monkeypatch, snapshot)
    calls = _install_agent_spy(monkeypatch, {"summary": "planned"})

    outcome = execute_body(queue_db.create_task(agent_type="orchestrator"))

    assert outcome["status"] == "completed"
    assert "channel_sends" not in calls[0]["context"]


def test_worker_records_a_producer_that_cannot_answer(
    monkeypatch, queue_db, execute_body, caplog
):
    """A missing/unreadable producer is RECORDED and leaves the key absent — never a fake 0."""

    async def snapshot(db, channels, *, now=None):
        raise RuntimeError("no producer in this deployment")

    _install_channel_service(monkeypatch, snapshot)
    calls = _install_agent_spy(monkeypatch, {"summary": "posted"})

    with caplog.at_level(logging.WARNING, logger="celery_app.tasks.agent_tasks"):
        outcome = execute_body(queue_db.create_task(agent_type="social_media"))

    assert outcome["status"] == "completed"                # the run is not killed by a counter
    assert "channel_sends" not in calls[0]["context"]      # unmeasured stays unmeasured
    assert [r for r in caplog.records if "code=channel_sends_unavailable" in r.getMessage()]


# ─────────────────────────────────────────────────────────────────────────────
# T6/T5 hook — _persist_run_artifacts and the persist-block wiring
# ─────────────────────────────────────────────────────────────────────────────
def test_worker_persists_finance_artifacts_and_folds_back_snapshot_written(
    monkeypatch, queue_db, execute_body
):
    """RED leg 7: a dispatcher that returns `{}` unconditionally fails here."""
    calls: list = []

    async def persist(db, result):
        calls.append((db, result))
        return {"ok": True, "snapshot_written": False, "expenses_written": 0}

    monkeypatch.setattr("app.services.finance_service.persist_finance_run", persist, raising=False)
    _install_agent_spy(
        monkeypatch,
        {"summary": "finance ran", "snapshot_written": True, "snapshot": {"revenue_cents": 0}},
    )

    outcome = execute_body(queue_db.create_task(agent_type="finance"))

    assert outcome["status"] == "completed"
    assert len(calls) == 1
    assert calls[0][0] is not None                        # a real session, on the run's transaction
    assert calls[0][1]["summary"] == "finance ran"        # the run's own result was handed over
    output = _latest_run_output(queue_db)
    # V15/4-05b re-pin (r-repair unit): the memory write leg rides the SAME report — a
    # completed run with a summary stores its own memory (`report["memory"]`) beside the
    # artifact legs; the original assertion is preserved by popping that key first.
    persistence = output["persistence"]
    memory = persistence.pop("memory")
    assert persistence == {
        "ok": True,
        "snapshot_written": False,
        "expenses_written": 0,
    }
    assert memory["ok"] is True, memory
    assert isinstance(memory["memory_id"], int)
    # The agent's intent (`snapshot_written: True`) is replaced by the persisted fact (False).
    assert output["snapshot_written"] is False


def test_worker_records_a_failed_persistence_in_output_and_activity(
    monkeypatch, queue_db, execute_body
):
    """A not-ok report: output keeps the error, an activity row is written, the run still completes."""

    async def persist(db, result):
        return {"ok": False, "error": {"code": "E_DB", "message": "snapshot write refused"}}

    monkeypatch.setattr("app.services.finance_service.persist_finance_run", persist, raising=False)
    _install_agent_spy(monkeypatch, {"summary": "finance ran"})

    outcome = execute_body(queue_db.create_task(agent_type="finance"))

    assert outcome["status"] == "completed"               # the RUN succeeded; persistence did not
    output = _latest_run_output(queue_db)
    assert output["persistence"]["ok"] is False
    assert output["persistence"]["error"] == {"code": "E_DB", "message": "snapshot write refused"}
    assert "snapshot_written" not in output               # never set from a failed upsert
    row = queue_db.query(
        "SELECT summary, level, detail FROM activity_log WHERE action = 'run_artifacts_not_persisted'"
    )
    assert len(row) == 1
    assert row[0]["level"] == "warning"
    assert "E_DB" in row[0]["summary"]
    assert json.loads(row[0]["detail"]) == {"code": "E_DB", "message": "snapshot write refused"}


def test_worker_records_no_persistence_report_for_an_agent_without_one(
    monkeypatch, queue_db, execute_body
):
    """No dispatcher -> no report -> no `persistence` key (absent, not `{}`).

    R33: the vehicle changed from `ads_management` to `orchestrator` — ads now HAS a dispatcher
    (its runs report a real zero/shape report, pinned by
    `tests/unit/test_r33_ads_persist_e2e.py`), so it can no longer stand in for "an agent
    without one".  The intent — and every assertion — is unchanged.
    """
    # V15 re-pin (r-repair unit, 4-05b): the memory write leg made a summary-bearing run ALWAYS
    # produce a report (`memory` is a written artifact), so the "nothing to write" vehicle is
    # now a run with NEITHER an artifact branch NOR a summary — for it, `persistence` must
    # still be absent (not `{}`), and the named no-summary skip writes nothing at all.
    _install_agent_spy(monkeypatch, {})

    outcome = execute_body(queue_db.create_task(agent_type="orchestrator"))

    assert outcome["status"] == "completed"
    output = _latest_run_output(queue_db)
    assert "persistence" not in output
    assert queue_db.count("memory_entries") == 0
    assert queue_db.count("activity_log", "action = ?", ("run_artifacts_not_persisted",)) == 0


# ─────────────────────────────────────────────────────────────────────────────
# _persist_run_artifacts — the dispatcher's own contract (no database needed)
# ─────────────────────────────────────────────────────────────────────────────
async def test_persist_run_artifacts_reports_a_named_refusal(monkeypatch):
    from app.core.errors import E_DB, PolsiaError
    from celery_app.tasks.agent_tasks import _persist_run_artifacts

    async def persist(db, result):
        raise PolsiaError(E_DB, "insert refused", how_to_fix="check the database")

    monkeypatch.setattr("app.services.finance_service.persist_finance_run", persist, raising=False)

    report = await _persist_run_artifacts(None, "finance", {})

    assert report == {"ok": False, "error": {"code": E_DB, "message": "insert refused"}}


async def test_persist_run_artifacts_reports_an_unexpected_failure(monkeypatch):
    from celery_app.tasks.agent_tasks import E_RUN_ARTIFACTS_PERSIST_FAILED, _persist_run_artifacts

    async def persist(db, result):
        raise ValueError("boom")

    monkeypatch.setattr("app.services.finance_service.persist_finance_run", persist, raising=False)

    report = await _persist_run_artifacts(None, "finance", {})

    assert report["ok"] is False
    assert report["error"]["code"] == E_RUN_ARTIFACTS_PERSIST_FAILED
    assert "boom" in report["error"]["message"]


async def test_persist_run_artifacts_returns_empty_for_an_agent_without_one():
    from celery_app.tasks.agent_tasks import _persist_run_artifacts

    assert await _persist_run_artifacts(None, "orchestrator", {}) == {}
