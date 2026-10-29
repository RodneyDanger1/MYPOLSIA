"""Wave B2 / seat C4 — orchestration, reporting and task truth (F-04, F-26, F-43, F-51, carries).

Every test here pins a MEASURED defect on the pre-fix tree:

  F-04  `route_agent_type` was a substring scan: "find new leads" routed to `ads_management`
        (the "ads" inside "leads") and any message containing "plan" ("explain the plan" — a
        question, not a task) routed to `business_planning`.
  F-26  `tasks.result_summary` had NO reader: an agent's output was never consumed downstream.
        A downstream task now records its upstream agents' results (`metadata.upstream_results`,
        typed `UpstreamResult`, bounded) at the moment the dependency release makes it runnable.
  F-43  the evening report counted only tasks CREATED that day, `yesterday_summary` was a
        `LIMIT 1` of any day, parked approvals had no reaper and a `rejected` write left the
        approval flag "pending".
  F-51  the stale-claim sweep was unbounded and wrote ONE activity row carrying every id.
  R1    `github`/`stripe` had no send counter (every configured cap refused `E114 cap_unknown`).
  R2    `depends_on_cycle` was flattened to a bool, discarding the members the producer held.
  R4    `excluded_rows` had no typed field (the count rode a string).
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.core.database import utcnow
from app.core.errors import E_DB, MeridianError
from app.models.finance import StripeEvent
from app.models.report import ActivityLog
from app.models.task import Task
from app.schemas.planning import BurnLineItemResponse
from app.schemas.task import TaskResponse
from app.services import channel_service, chat_service, report_service, task_service


@pytest.fixture(autouse=True)
def quiet_redis(monkeypatch):
    """No network: the publishing writer is stubbed, the committed rows are real."""
    from unittest.mock import AsyncMock

    mock = AsyncMock()
    mock.publish = AsyncMock(return_value=1)
    monkeypatch.setattr("app.core.events.get_redis", lambda: mock)
    return mock


async def _activity_actions(db) -> list[ActivityLog]:
    from sqlalchemy import select

    rows = (await db.execute(select(ActivityLog).order_by(ActivityLog.id))).scalars().all()
    return list(rows)


# ── F-04: intent + word boundaries, and an explicit "I did not understand" ────────────────────


def test_leads_route_to_outreach_not_ads():
    """'find new leads' must reach outreach — 'ads' is a SUBSTRING of 'leads', not the word."""
    decision = chat_service.route_agent_decision("create a task to find new leads")
    assert decision.understood is True
    assert decision.agent_type == "email_outreach"
    assert decision.matched in {"lead", "leads"}
    assert chat_service.route_agent_type("create a task to find new leads") == "email_outreach"


def test_explain_the_plan_is_not_delegated():
    """A question containing 'plan' is not a planning TASK: the router says it did not understand."""
    decision = chat_service.route_agent_decision("explain the plan")
    assert decision.understood is False
    assert decision.reason == "not_a_task_request"
    assert decision.agent_type is None
    # the frozen accessor still answers the default — the DECISION carries the honesty
    assert chat_service.route_agent_type("explain the plan") == "orchestrator"


def test_a_substring_inside_a_word_no_longer_routes():
    """'uploads' contains 'ads' and 'planets' contains 'plan' — neither is a keyword match."""
    assert chat_service.route_agent_decision("create a task to audit the uploads").agent_type is None
    assert chat_service.route_agent_type("create a task to audit the uploads") == "orchestrator"
    assert (
        chat_service.route_agent_decision("create a task about the roadmap").agent_type
        == "business_planning"
    )


def test_a_money_message_is_never_routed_to_an_agent():
    decision = chat_service.route_agent_decision("create a task to issue a refund")
    assert decision.understood is False
    assert decision.reason == "refused"


# ── F-26: a downstream task consumes an upstream agent's real result ─────────────────────────


async def _completed_task(db, *, title: str, agent_type: str, summary: str) -> Task:
    task = await task_service.create_task(db, title=title, agent_type=agent_type)
    await task_service.transition_task_status(
        db, task.id, "completed", result_summary=summary
    )
    return task


async def test_agent_b_records_what_agent_a_produced(async_db_session):
    """The plan A -> B: B's row carries A's RESULT, typed, at the release that makes B runnable."""
    db = async_db_session
    a = await _completed_task(
        db, title="Research the niche", agent_type="competitor_research",
        summary="Five competitors; two price under $20.",
    )
    b = await task_service.create_task(
        db,
        title="Write the landing page",
        agent_type="code_generation",
        metadata={"depends_on_ids": [a.id], "plan_day": "2026-10-25"},
    )

    released = await task_service.release_ready_dependents(db, completed_id=a.id)
    assert released == [b.id]

    reloaded = await task_service.get_task(db, b.id)
    recorded = reloaded.metadata_json["upstream_results"]
    assert recorded == [
        {
            "task_id": a.id,
            "title": "Research the niche",
            "status": "completed",
            "result_summary": "Five competitors; two price under $20.",
            "truncated": False,
            "completed_at": recorded[0]["completed_at"],
        }
    ]
    assert recorded[0]["completed_at"] is not None
    assert reloaded.metadata_json["upstream_results_recorded_at"]

    # the wire serves the SAME record, typed (this is the reader result_summary never had)
    wire = TaskResponse.model_validate(reloaded)
    assert [item.task_id for item in wire.upstream_results] == [a.id]
    assert wire.upstream_results[0].result_summary.startswith("Five competitors")

    # and the live read agrees with the recorded copy
    live = await task_service.upstream_results_for(db, b.id)
    assert [item.task_id for item in live] == [a.id]


async def test_a_long_upstream_result_is_bounded_and_says_it_was_cut(async_db_session):
    db = async_db_session
    long_summary = "x" * (task_service.UPSTREAM_RESULT_SUMMARY_CHARS + 500)
    a = await _completed_task(
        db, title="Huge report", agent_type="competitor_research", summary=long_summary
    )
    b = await task_service.create_task(
        db, title="Consume it", agent_type="email_outreach",
        metadata={"depends_on_ids": [a.id]},
    )

    await task_service.release_ready_dependents(db, completed_id=a.id)
    record = (await task_service.get_task(db, b.id)).metadata_json["upstream_results"][0]
    assert record["truncated"] is True
    assert len(record["result_summary"]) == task_service.UPSTREAM_RESULT_SUMMARY_CHARS
    assert record["result_summary"].endswith("…")


async def test_the_fan_in_is_bounded_to_the_first_five_edges(async_db_session):
    db = async_db_session
    ups = [
        await _completed_task(
            db, title=f"Upstream {index}", agent_type="competitor_research",
            summary=f"result {index}",
        )
        for index in range(task_service.UPSTREAM_MAX_RESULTS + 2)
    ]
    b = await task_service.create_task(
        db, title="Wide fan-in", agent_type="orchestrator",
        metadata={"depends_on_ids": [up.id for up in ups]},
    )

    await task_service.release_ready_dependents(db, completed_id=ups[0].id)
    recorded = (await task_service.get_task(db, b.id)).metadata_json["upstream_results"]
    assert len(recorded) == task_service.UPSTREAM_MAX_RESULTS
    assert [item["task_id"] for item in recorded] == [
        up.id for up in ups[: task_service.UPSTREAM_MAX_RESULTS]
    ]


# ── F-51: a bounded sweep, one activity row per recovered task ────────────────────────────────


async def _stale_claim(db, title: str) -> Task:
    task = await task_service.create_task(db, title=title, agent_type="orchestrator")
    await task_service.claim_task(db, worker="worker-dead")
    row = await task_service.get_task(db, task.id)
    row.heartbeat_at = utcnow() - timedelta(hours=5)
    row.claimed_at = utcnow() - timedelta(hours=5)
    await db.flush()
    return row


async def test_one_activity_row_per_requeued_task(async_db_session):
    db = async_db_session
    claims = [await _stale_claim(db, f"stale {index}") for index in range(3)]
    before = len(await _activity_actions(db))

    recovered = await task_service.requeue_stale_claims(db)
    assert recovered == 3

    rows = [
        row for row in (await _activity_actions(db))[before:]
        if row.action == "stale_claims_requeued"
    ]
    assert len(rows) == 3, "one row per recovered task, never one row carrying every id"
    detail_ids = sorted(row.detail["requeued_ids"][0] for row in rows)
    assert detail_ids == sorted(claim.id for claim in claims)
    for row in rows:
        assert row.detail["requeued_ids"] == [row.detail["task_id"]]
        assert row.detail["sweep_size"] == 3
        assert "…" not in row.summary
        assert f"#{row.detail['task_id']}" in row.summary


async def test_the_sweep_is_bounded(async_db_session, monkeypatch):
    db = async_db_session
    monkeypatch.setattr(task_service, "STALE_CLAIM_BATCH", 2)
    claims = [await _stale_claim(db, f"bounded {index}") for index in range(3)]

    assert await task_service.requeue_stale_claims(db) == 2
    # read the COMMITTED values, not the identity map (the sweep writes with
    # `synchronize_session=False`, so loaded objects keep their pre-sweep status)
    from sqlalchemy import select

    rows = (
        await db.execute(
            select(Task.id, Task.status).where(Task.id.in_([claim.id for claim in claims]))
        )
    ).all()
    statuses = {int(task_id): str(status) for task_id, status in rows}
    assert sum(1 for status in statuses.values() if status == "pending") == 2
    assert sum(1 for status in statuses.values() if status == "in_progress") == 1
    # the next pass continues from where the bound stopped
    assert await task_service.requeue_stale_claims(db) == 1


# ── F-43: the day digest, the reaper, and the approval flag ──────────────────────────────────


async def test_the_digest_sees_an_outcome_that_was_not_CREATED_today(async_db_session):
    """A task created yesterday and finished today is IN today's report (it used to vanish)."""
    db = async_db_session
    task = await task_service.create_task(
        db, title="Carried over", agent_type="social_media"
    )
    task.created_at = utcnow() - timedelta(days=2)
    await db.flush()
    await task_service.transition_task_status(
        db, task.id, "completed", result_summary="Posted three threads."
    )

    digest = await report_service.build_day_digest(db, utcnow().date())
    assert digest.status == "measured"
    assert [row.task_id for row in digest.completed] == [task.id]
    assert digest.completed[0].text == "Posted three threads."
    # the old measurement (created today) does NOT see it — that is the thinness being fixed
    assert digest.counts_created.get("completed", 0) == 0
    assert "1 completed" in digest.headline()
    assert "Posted three threads." in digest.prompt_block()


async def test_the_digest_says_so_when_it_cannot_read(async_db_session, monkeypatch):
    db = async_db_session

    async def _boom(*_args, **_kwargs):
        raise MeridianError(E_DB, "the database is unreachable")

    monkeypatch.setattr(report_service, "fetch_rows", _boom)
    digest = await report_service.build_day_digest(db, utcnow().date())
    assert digest.status == "unavailable"
    assert digest.reason == E_DB
    assert "could not be read" in digest.headline()
    assert digest.completed == [] and digest.failed == []


async def test_an_empty_day_is_a_measured_empty_not_a_zero_report(async_db_session):
    digest = await report_service.build_day_digest(async_db_session, utcnow().date())
    assert digest.status == "empty"
    assert "no task reached an outcome" in digest.headline()


async def test_yesterday_summary_is_that_days_summary(async_db_session):
    """The `LIMIT 1` defect: a summary from ANOTHER day is not yesterday's."""
    db = async_db_session
    today = utcnow().date()
    await report_service.save_evening_summary(
        db, report_date=today - timedelta(days=4), summary="four days ago"
    )
    await report_service.save_evening_summary(
        db, report_date=today - timedelta(days=1), summary="yesterday's real summary"
    )
    assert await report_service.get_evening_summary_for(db, today - timedelta(days=1)) == (
        "yesterday's real summary"
    )
    # a day that has no report answers None — never the newest row in the table
    assert await report_service.get_evening_summary_for(db, today) is None


async def test_a_parked_approval_is_reaped_and_the_flag_stops_saying_pending(async_db_session):
    db = async_db_session
    task = await task_service.create_task(
        db,
        title="Send the invoice email",
        agent_type="email_outreach",
        status="awaiting_approval",
        metadata={"approval_required": [{"fingerprint": "fp-1", "action": "email.send"}]},
    )
    task.updated_at = utcnow() - timedelta(days=10)
    await db.flush()
    before = len(await _activity_actions(db))

    reaped = await task_service.reap_stale_approvals(db, ttl_s=3600)
    assert reaped == [task.id]

    row = await task_service.get_task(db, task.id)
    assert str(row.status) == "failed"
    assert "expired" in (row.error_message or "")
    assert row.metadata_json["approval_required"] == []
    assert row.metadata_json["approval_cleared"]["status"] == "failed"
    dropped = row.metadata_json["approval_cleared"]["fingerprints"]
    assert len(dropped) == 1 and "fp-1" in dropped[0]
    assert TaskResponse.model_validate(row).approval_required is False

    events = [
        event for event in (await _activity_actions(db))[before:]
        if event.action == "stale_approvals_reaped"
    ]
    assert len(events) == 1
    assert events[0].detail["task_id"] == task.id


async def test_a_fresh_park_is_not_reaped(async_db_session):
    db = async_db_session
    task = await task_service.create_task(
        db, title="Waiting on you", agent_type="social_media",
        status="awaiting_approval", metadata={"approval_required": ["fp-1"]},
    )
    assert await task_service.reap_stale_approvals(db, ttl_s=3600) == []
    assert str((await task_service.get_task(db, task.id)).status) == "awaiting_approval"


async def test_a_rejected_write_clears_the_approval_flag(async_db_session):
    """R3: a denied row must not keep serving `approval_required: true`."""
    db = async_db_session
    task = await task_service.create_task(
        db, title="Parked then denied", agent_type="ads_management",
        metadata={"approval_required": [{"fingerprint": "fp-9"}]},
    )
    await task_service.transition_task_status(db, task.id, "rejected", actor="owner")

    row = await task_service.get_task(db, task.id)
    assert str(row.status) == "rejected"
    assert row.metadata_json["approval_required"] == []
    assert row.metadata_json["approval_cleared"]["status"] == "rejected"
    assert TaskResponse.model_validate(row).approval_required is False


# ── R1: github / stripe counters (the `E114 cap_unknown` replay refusal) ─────────────────────


async def test_github_and_stripe_report_measured_sends(async_db_session):
    db = async_db_session
    snapshot = await channel_service.channel_sends_snapshot(db, ["github", "stripe"])
    assert snapshot["github"] == {"today": 0, "hour": 0}
    assert snapshot["stripe"] == {"today": 0, "hour": 0}
    assert await channel_service.channel_sends_today(db, "github") == 0
    assert await channel_service.channel_sends_this_hour(db, "stripe") == 0

    await channel_service.record_channel_send(db, "github", detail={"pr": 12})
    db.add(
        StripeEvent(
            stripe_event_id="evt_1",
            event_type="charge.succeeded",
            amount_cents=500,
            currency="usd",
            processed_at=utcnow(),
        )
    )
    await db.flush()

    after = await channel_service.channel_sends_snapshot(db, ["github", "stripe"])
    assert after["github"]["today"] == 1
    assert after["github"]["hour"] == 1
    assert after["stripe"]["today"] == 1


async def test_a_channel_with_no_source_is_still_unknown(async_db_session):
    """The honest `None` is preserved for channels that really have no local record."""
    snapshot = await channel_service.channel_sends_snapshot(
        async_db_session, ["render", "google_ads"]
    )
    assert snapshot["render"] == {"today": None, "hour": None}
    assert snapshot["google_ads"] == {"today": None, "hour": None}


# ── R2 / R4: the wire carries what the producer holds ────────────────────────────────────────


def test_depends_on_cycle_members_are_served_with_ids_and_titles():
    response = TaskResponse.model_validate(
        {
            "id": 7,
            "title": "Released root",
            "agent_type": "orchestrator",
            "metadata_json": {
                "plan_day": "2026-10-25",
                "depends_on_cycle": ["Task B", "Task C"],
                "depends_on_cycle_members": [
                    {"id": 8, "title": "Task B"},
                    {"id": 9, "title": "Task C"},
                ],
                "depends_on_ids": [],
            },
        }
    )
    assert response.plan["depends_on_cycle"] is True          # frozen bool kept
    assert [member.model_dump() for member in response.depends_on_cycle_members] == [
        {"id": 8, "title": "Task B"},
        {"id": 9, "title": "Task C"},
    ]
    assert [member.title for member in response.plan["depends_on_cycle_members"]] == [
        "Task B",
        "Task C",
    ]


def test_a_malformed_cycle_member_is_dropped_not_fatal():
    response = TaskResponse.model_validate(
        {
            "id": 7,
            "title": "Bad bag",
            "agent_type": "orchestrator",
            "metadata_json": {"depends_on_cycle_members": [{"id": "x"}, {"id": 3, "title": "ok"}]},
        }
    )
    assert [member.id for member in response.depends_on_cycle_members] == [3]


def test_excluded_rows_is_a_typed_int_and_the_reason_string_stays():
    """R4: the count gets a real field; the prose a client renders is untouched."""
    item = BurnLineItemResponse(
        label="software",
        monthly_usd=42.0,
        source="expense_records",
        reason="3 rows excluded (non-USD)",
        excluded_rows=3,
    )
    assert item.excluded_rows == 3
    assert item.reason == "3 rows excluded (non-USD)"
    # unmeasured stays null — never a fabricated 0
    assert BurnLineItemResponse(label="x", monthly_usd=None, source=None).excluded_rows is None


async def test_a_cancelled_write_records_how_the_row_ended(async_db_session):
    """F-43 'no cancelled writer': the row's own record says who ended it, and the flag follows."""
    db = async_db_session
    task = await task_service.create_task(
        db, title="Parked then cancelled", agent_type="social_media",
        metadata={"approval_required": [{"fingerprint": "fp-c"}]},
    )
    before = len(await _activity_actions(db))
    await task_service.transition_task_status(db, task.id, "cancelled", actor="owner")

    row = await task_service.get_task(db, task.id)
    assert str(row.status) == "cancelled"
    assert row.metadata_json["approval_required"] == []
    assert row.metadata_json["approval_cleared"]["actor"] == "owner"
    assert row.metadata_json["approval_cleared"]["status"] == "cancelled"

    events = (await _activity_actions(db))[before:]
    assert [event.action for event in events] == [task_service.TASK_STATUS_CHANGED_ACTION]
    assert events[0].detail == {
        "task_id": task.id, "from": "pending", "to": "cancelled", "actor": "owner",
    }
