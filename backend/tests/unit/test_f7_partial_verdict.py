"""F-07 (visuals, P1): the ledger must not present a PARTIAL run as an unqualified success.

Live evidence: 8 of the trial's 10 tasks carried `status: completed` while the run's own text said
"status partial" or that no deliverable was produced; "the badge, the ledger row and the dashboard
card all say COMPLETED".

What this lane can honestly do without inventing a task status the vocabulary does not have
(`pending|in_progress|completed|failed|cancelled|awaiting_approval|rejected`):

  * keep `refused` a NAMED failure (F2-FEED-1 — the task is `failed`, never completed);
  * for `partial`, put the run's own verdict at the TOP of the stored output (`payload_status`)
    and prefix the summary — which the worker copies verbatim into `tasks.result_summary` — with
    `[partial] `, so the feed, the task page and the ops table cannot read as a clean success.

RED leg (forge receipt): with `_record_run_verdict` removed, the partial test fails by name on the
`[partial] ` prefix and the missing `payload_status`.
"""
from __future__ import annotations

import json

from app.agents import crew_factory
from app.agents.base_agent import BasePolsiaAgent


def _install(monkeypatch, *, status: str, summary: str) -> None:
    class StubAgent(BasePolsiaAgent):
        agent_type = "business_planning"
        output_schema = None

        def run(self, task, context):  # noqa: ANN001 - the frozen BasePolsiaAgent seam
            return {"summary": summary, "status": status, "notes": ["a page was unreachable"]}

    monkeypatch.setattr(crew_factory, "get_agent_class", lambda agent_type: StubAgent)
    # These tests pin the REAL-run ledger shape; the autouse CLAUDE_CLI_MOCK seam is exempt from
    # the visible label (its summary is a pinned fixture, see `_record_run_verdict`).
    monkeypatch.setattr("app.agents.base_agent.is_mock_mode", lambda: False)


def _row(queue_db, task_id):
    return queue_db.row(task_id)


def test_a_partial_run_is_labelled_partial_in_the_task_summary(
    monkeypatch, queue_db, execute_body, mock_redis
):
    _install(
        monkeypatch,
        status="partial",
        summary="Research run: supplier prices are quote-only; the positioning analysis is complete.",
    )
    task_id = queue_db.create_task(title="Research the US candle market", agent_type="business_planning")

    outcome = execute_body(task_id)

    row = _row(queue_db, task_id)
    assert row["status"] == "completed", row
    assert outcome.get("status") == "completed", outcome
    assert (row["result_summary"] or "").startswith("[partial] "), row["result_summary"]
    stored = json.loads(
        queue_db.query("SELECT output FROM agent_runs ORDER BY id DESC LIMIT 1")[0]["output"] or "{}"
    )
    assert stored["payload_status"] == "partial", stored.get("payload_status")
    assert stored["summary"].startswith("[partial] "), stored.get("summary")


def test_the_label_is_applied_once_even_when_the_model_already_said_it(
    monkeypatch, queue_db, execute_body, mock_redis
):
    _install(monkeypatch, status="partial", summary="[partial] already labelled by the run")
    task_id = queue_db.create_task(title="Research the market", agent_type="business_planning")

    execute_body(task_id)

    row = _row(queue_db, task_id)
    assert row["result_summary"] == "[partial] already labelled by the run", row["result_summary"]


def test_an_ok_run_is_not_labelled(monkeypatch, queue_db, execute_body, mock_redis):
    _install(monkeypatch, status="ok", summary="Four competitors profiled with live sources.")
    task_id = queue_db.create_task(title="Research the market", agent_type="business_planning")

    execute_body(task_id)

    row = _row(queue_db, task_id)
    assert row["status"] == "completed"
    assert (row["result_summary"] or "").startswith("[partial]") is False, row["result_summary"]
    stored = json.loads(
        queue_db.query("SELECT output FROM agent_runs ORDER BY id DESC LIMIT 1")[0]["output"] or "{}"
    )
    assert "payload_status" not in stored
