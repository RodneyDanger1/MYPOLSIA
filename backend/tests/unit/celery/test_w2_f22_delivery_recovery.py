"""F-22 — the broker's orphaned deliveries, decided by DB truth (wave 2, live incident 2026-10-28).

The incident this file pins (all values captured from the live stack, read-only):

* 21:11:47 UTC — the worker received `run_agent_task` for task **315**, claimed the row
  (`in_progress`, heartbeat = claim), and was SIGKILLed at 21:12:09 mid-run (`ExitCode 137`).
* Redis `unacked` still held that delivery (`7a15b311-…`, routing key `agents`) and
  `unacked_index` held THREE tags: the live one plus `4b6986de-…` (21:02:27) and `f5ed5a87-…`
  (21:07:32) with **no payload at all** — the ghost shape a manual `HDEL` leaves behind.
* Task 315 stayed `in_progress` after the restart; the row for task 313 sat `pending` while its
  message waited in the `agents` queue.  A restart restores nothing: kombu's Redis transport
  restores an unacked delivery only from a live worker's event loop and only after
  `visibility_timeout` (1800 s here), and its shutdown restore never ran (SIGKILL).

The tests below drive the recovery with a broker DOUBLE (the sanctioned message-queue boundary)
and the real file-backed DB, then pin the claim takeover the redelivery needs.  No test here
publishes to a broker, starts a worker, or calls a model.
"""
from __future__ import annotations

import base64
import json
from datetime import timedelta

import pytest

from celery_app.tasks import broker_recovery
from celery_app.tasks.broker_recovery import AGENT_TASK_NAME, Delivery, decode_delivery

#: The REAL base64 body of the live task-315 delivery, captured from
#: `redis-cli HGET unacked 7a15b311-1169-45f2-83a8-c531c7e49e4b` (2026-10-28 21:15 UTC).
#: It decodes to `[[315], {}, {"callbacks": null, ...}]` — the transport's own protocol-2 body.
LIVE_TASK_315_BODY = (
    "W1szMTVdLCB7fSwgeyJjYWxsYmFja3MiOiBudWxsLCAiZXJyYmFja3MiOiBudWxsLCAiY2hhaW4iOiBudWxs"
    "LCAiY2hvcmQiOiBudWxsfV0="
)
#: The live delivery tag + the two ghost tags captured from `unacked_index`.
LIVE_TAG = "7a15b311-1169-45f2-83a8-c531c7e49e4b"
GHOST_TAGS = ("4b6986de-9bb0-4b5a-88c4-b37d1f51d4dd", "f5ed5a87-05d7-4fa4-bb35-0605e0a98e36")


def _pooled_body(task_id: int) -> str:
    return base64.b64encode(
        json.dumps([[task_id], {}, {"callbacks": None, "errbacks": None}]).encode()
    ).decode()


def _live_payload(task_id: int = 315) -> str:
    """The transport's unacked HASH value, with the incident's real body/headers.

    The wrapper is rebuilt (kombu writes `[raw, exchange, routing_key]`; that shape is the
    transport's, not the incident's) but the BODY BYTES and every header value are the captured
    ones (`argsrepr` included) — the decode below is exercised against production bytes.
    """
    body = LIVE_TASK_315_BODY if task_id == 315 else _pooled_body(task_id)
    return json.dumps(
        [
            {
                "body": body,
                "content-encoding": "utf-8",
                "content-type": "application/json",
                "headers": {
                    "lang": "py",
                    "task": AGENT_TASK_NAME,
                    "id": "728fcf3e-35bc-426e-814e-10ba88595493",
                    "argsrepr": f"({task_id},)",
                    "retries": 0,
                },
                "properties": {
                    "correlation_id": "728fcf3e-35bc-426e-814e-10ba88595493",
                    "delivery_tag": LIVE_TAG,
                    "delivery_info": {"exchange": "", "routing_key": "agents"},
                },
            },
            "",
            "agents",
        ]
    )


class FakeBroker:
    """The broker DOUBLE: a snapshot of deliveries plus the two mutations, recorded in order."""

    def __init__(self, deliveries: list[Delivery], *, fail_snapshot: bool = False) -> None:
        self._deliveries = list(deliveries)
        self.fail_snapshot = fail_snapshot
        self.restored: list[str] = []
        self.dropped: list[str] = []
        self.snapshot_calls = 0
        self.closed = False

    def snapshot(self) -> list[Delivery]:
        self.snapshot_calls += 1
        if self.fail_snapshot:
            raise ConnectionError("test-only broker outage")
        # The production adapter hands deliveries back oldest-first; mirror it so the bound test
        # asserts the real ordering rule.
        return sorted(self._deliveries, key=lambda delivery: delivery.score or 0.0)

    def restore(self, tag: str) -> None:
        self.restored.append(tag)

    def drop(self, tag: str) -> None:
        self.dropped.append(tag)

    def close(self) -> None:
        self.closed = True


def _set_status(db, task_id: int, status: str, **kwargs) -> None:
    from app.services.task_service import update_task_status

    async def _run() -> None:
        async with db.factory() as session:
            await update_task_status(session, task_id, status, **kwargs)
            await session.commit()

    db.run(_run())


def _recover(db, broker) -> dict:
    from celery_app.tasks.maintenance import _recover_orphaned_deliveries

    return db.run(_recover_orphaned_deliveries(broker=broker))


def _metadata(db, task_id: int) -> dict:
    raw = db.row(task_id).get("metadata")
    if not raw:
        return {}
    parsed = json.loads(raw)
    return parsed if isinstance(parsed, dict) else {}


def _actions(db) -> list[str]:
    return [row["action"] for row in db.query("SELECT action FROM activity_log ORDER BY id")]


# ── the decoder, against production bytes ────────────────────────────────────────────────────
def test_the_live_task_315_delivery_decodes_to_its_task_id():
    """The captured payload decodes to task 315 — the parser is proven on real broker bytes."""
    delivery = decode_delivery(LIVE_TAG, _live_payload(315), score=1793221907.7976637)

    assert delivery.task_id == 315
    assert delivery.task_name == AGENT_TASK_NAME
    assert delivery.has_payload is True
    assert delivery.managed is True


def test_a_ghost_index_entry_has_no_payload_and_is_never_managed():
    """The two live `unacked_index` tags with no `unacked` field decode as unmanaged."""
    for tag in GHOST_TAGS:
        delivery = decode_delivery(tag, None)

        assert delivery.has_payload is False
        assert delivery.managed is False
        assert delivery.task_id is None


def test_a_malformed_body_is_unmanaged_rather_than_an_exception():
    payload = json.dumps(
        [{"headers": {"task": AGENT_TASK_NAME}, "body": "not-base64!!"}, "", "agents"]
    )

    delivery = decode_delivery("t", payload)

    assert delivery.managed is False
    assert delivery.task_name == AGENT_TASK_NAME


# ── the decision table ──────────────────────────────────────────────────────────────────────
def test_a_pending_rows_delivery_is_restored_and_the_pass_is_idempotent(queue_db):
    task_id = queue_db.create_task(title="pending owed a delivery")
    broker = FakeBroker([decode_delivery(LIVE_TAG, _live_payload(task_id), score=1.0)])

    first = _recover(queue_db, broker)

    assert first["status"] == "completed"
    assert first["restored"] == [task_id]
    assert broker.restored == [LIVE_TAG]
    assert broker.dropped == []

    # A second pass over the SAME snapshot restores nothing again once the tag is gone: the
    # decision is a function of the delivery, and a restored tag leaves `unacked`.
    broker._deliveries = []
    second = _recover(queue_db, broker)

    assert second["restored"] == []
    assert second["scanned"] == 0


def test_a_completed_rows_ghost_delivery_is_dropped_not_requeued(queue_db):
    """No ghost double-run: a delivery for a finished row is dropped, never put back."""
    task_id = queue_db.create_task(title="already completed")
    _set_status(queue_db, task_id, "completed", result_summary="done")
    broker = FakeBroker([decode_delivery(LIVE_TAG, _live_payload(task_id), score=1.0)])

    result = _recover(queue_db, broker)

    assert result["dropped"] == [f"{LIVE_TAG}:task-{task_id}:completed"]
    assert broker.restored == []
    assert queue_db.count("agent_runs") == 0, "the recovery must not start a run"


def test_a_missing_rows_delivery_is_dropped(queue_db):
    broker = FakeBroker([decode_delivery(LIVE_TAG, _live_payload(999_999), score=1.0)])

    result = _recover(queue_db, broker)

    assert broker.dropped == [LIVE_TAG]
    assert "missing-row" in result["dropped"][0]


def test_a_live_claim_is_left_to_the_visibility_timeout(queue_db):
    """The incident's row at restart: claim fresh, delivery still unacked → LEAVE, mutate nothing."""
    task_id = queue_db.create_task(title="live claim")
    queue_db.claim(task_id, "worker-a")
    broker = FakeBroker([decode_delivery(LIVE_TAG, _live_payload(task_id), score=1.0)])

    result = _recover(queue_db, broker)

    assert result["left_live_claim"] == [task_id]
    assert broker.restored == [] and broker.dropped == []
    assert queue_db.row(task_id)["status"] == "in_progress"


def test_an_expired_claim_is_reaped_then_its_delivery_is_restored(queue_db):
    """The row-side and broker-side halves close together: pending + delivery back on the queue."""
    task_id = queue_db.create_task(title="dead worker's claim")
    queue_db.claim(task_id, "worker-a")
    queue_db.backdate_heartbeat(task_id, 4000)
    broker = FakeBroker([decode_delivery(LIVE_TAG, _live_payload(task_id), score=1.0)])

    result = _recover(queue_db, broker)

    row = queue_db.row(task_id)
    assert row["status"] == "pending", "the lease sweep must requeue the expired claim"
    assert row["claimed_by"] is None and row["heartbeat_at"] is None
    assert _metadata(queue_db, task_id)["rearm_reason"] == "stale_claim_requeued"
    assert result["reaped_requeued"] == 1
    assert result["restored"] == [task_id]
    assert broker.restored == [LIVE_TAG]


def test_the_live_incident_row315_and_ghost_index_are_decided_correctly(queue_db):
    """The incident's exact input: row 315-shaped claim fresh + its delivery + two ghost tags.

    At the moment of the restart the claim is still inside its lease, so the delivery must be
    LEFT (the transport owns that window) and the two payload-less index entries must be dropped
    so they can never be "restored" into a phantom message.
    """
    task_id = queue_db.create_task(title="live incident 315", agent_type="code_generation")
    queue_db.claim(task_id, "worker-a")
    broker = FakeBroker([
        decode_delivery(GHOST_TAGS[0], None, score=1793221347.9708776),
        decode_delivery(GHOST_TAGS[1], None, score=1793221652.949776),
        decode_delivery(LIVE_TAG, _live_payload(task_id), score=1793221907.7976637),
    ])

    result = _recover(queue_db, broker)

    assert sorted(broker.dropped) == sorted(GHOST_TAGS), "ghost index entries are removed"
    assert broker.restored == []
    assert result["left_live_claim"] == [task_id]
    assert queue_db.row(task_id)["status"] == "in_progress"


def test_a_future_dated_pending_row_is_dropped_not_delivered_early(queue_db):
    """`claim_task` refuses a future `scheduled_date` — restoring now would burn the delivery."""
    from app.core.timeutil import now_utc

    task_id = queue_db.create_task(
        title="scheduled later", scheduled_date=now_utc() + timedelta(hours=2)
    )
    broker = FakeBroker([decode_delivery(LIVE_TAG, _live_payload(task_id), score=1.0)])

    result = _recover(queue_db, broker)

    assert broker.dropped == [LIVE_TAG]
    assert "not-due" in result["dropped"][0]
    assert queue_db.row(task_id)["status"] == "pending"


def test_a_non_agent_delivery_is_left_untouched(queue_db):
    """A maintenance tick message is NOT this recovery's contract: leave it, name it."""
    payload = json.dumps([
        {"body": _pooled_body(7), "headers": {
            "task": "celery_app.tasks.maintenance.reconcile_stranded_pending_tasks"}},
        "",
        "maintenance",
    ])
    broker = FakeBroker([decode_delivery("tick-tag", payload, score=1.0)])

    result = _recover(queue_db, broker)

    assert result["left_unmanaged"] == [
        "tick-tag:celery_app.tasks.maintenance.reconcile_stranded_pending_tasks"
    ]
    assert broker.restored == [] and broker.dropped == []


def test_a_broker_outage_is_a_named_skip_not_a_crash(queue_db):
    broker = FakeBroker([], fail_snapshot=True)

    result = _recover(queue_db, broker)

    assert result["status"] == "partial"
    assert result["reason"] == "ConnectionError"
    assert result["errored"] == [{"step": "delivery_pass", "error": "ConnectionError"}]


def test_the_batch_bound_is_reported_when_it_truncates(queue_db, monkeypatch):
    from celery_app.tasks import maintenance

    first = queue_db.create_task(title="bound-1")
    second = queue_db.create_task(title="bound-2")
    monkeypatch.setattr(maintenance, "DELIVERY_RECOVERY_BATCH", 1)
    broker = FakeBroker([
        decode_delivery("tag-1", _live_payload(first), score=2.0),
        decode_delivery("tag-2", _live_payload(second), score=1.0),
    ])

    result = _recover(queue_db, broker)

    assert result["scanned"] == 2
    assert result["bound"] == 1
    assert result["bound_reached"] is True
    assert result["restored"] == [second], "oldest delivery first (the snapshot's own order)"
    assert broker.restored == ["tag-2"]


def test_one_activity_row_describes_a_pass_that_changed_something_and_none_otherwise(queue_db):
    task_id = queue_db.create_task(title="activity evidence")
    changed = _recover(queue_db, FakeBroker([
        decode_delivery(LIVE_TAG, _live_payload(task_id), score=1.0),
    ]))
    idle = _recover(queue_db, FakeBroker([]))

    recovery_rows = [
        row for row in queue_db.query("SELECT action, level FROM activity_log ORDER BY id")
        if row["action"] == "broker_delivery_recovery"
    ]

    assert changed["restored"] == [task_id]
    assert idle["restored"] == []
    assert len(recovery_rows) == 1, "one row for one real change; a no-op pass writes nothing"
    assert recovery_rows[0]["level"] == "info"


# ── the production caller: the worker-startup hook ──────────────────────────────────────────
def test_the_worker_startup_hook_is_wired_and_runs_one_pass_per_process(monkeypatch):
    from celery.signals import worker_ready

    from celery_app.tasks import maintenance

    calls: list[str] = []

    async def _fake_recovery() -> dict:
        calls.append("recovery")
        return {"status": "completed", "restored": [], "dropped": []}

    monkeypatch.setattr(maintenance, "_recover_orphaned_deliveries", _fake_recovery)
    monkeypatch.setattr(maintenance, "_STARTUP_RECOVERY_DONE", False, raising=False)

    # celery's Signal keeps `((id(receiver), id(sender)), weakref)` entries (weak refs, so a
    # receiver can be collected); matching by receiver id is the documented way to ask
    # "is this connected?".
    registered_ids: set[int] = set()
    for entry in getattr(worker_ready, "receivers", []):
        key = entry[0] if isinstance(entry, tuple) and entry else entry
        registered_ids.add(key[0] if isinstance(key, tuple) and key else int(key))
    assert id(maintenance._recover_deliveries_on_worker_startup) in registered_ids, (
        "the hook must be connected to worker_ready, not merely defined"
    )

    maintenance._recover_deliveries_on_worker_startup(sender=None)
    maintenance._recover_deliveries_on_worker_startup(sender=None)

    assert calls == ["recovery"], "one startup pass per process, and it really ran"


# ── the redelivery boundary: an expired claim must be takeable, atomically ──────────────────
def _takeover(db, task_id: int, worker: str):
    from app.services.task_service import claim_task

    async def _run():
        async with db.factory() as session:
            row = await claim_task(
                session, worker=worker, task_id=task_id, takeover_stale=True
            )
            await session.commit()
            return row

    return db.run(_run())


def test_an_expired_claim_can_be_taken_over_by_its_redelivery(queue_db):
    task_id = queue_db.create_task(title="redelivery takeover")
    queue_db.claim(task_id, "worker-a")
    queue_db.backdate_heartbeat(task_id, 4000)

    row = _takeover(queue_db, task_id, "worker-b")

    assert row is not None
    stored = queue_db.row(task_id)
    assert stored["status"] == "in_progress"
    assert stored["claimed_by"] == "worker-b"
    assert stored["attempts"] == 2, "the takeover is an execution: the claim counts it"
    assert "task_claim_recovered" in _actions(queue_db), "the recovery is visible, never silent"


def test_a_live_claim_is_still_refused_by_the_redelivery(queue_db):
    from app.core.errors import E_TASK_CLAIMED, MeridianError

    task_id = queue_db.create_task(title="live claim refused")
    queue_db.claim(task_id, "worker-a")

    with pytest.raises(MeridianError) as raised:
        _takeover(queue_db, task_id, "worker-b")

    assert raised.value.code == E_TASK_CLAIMED
    assert queue_db.row(task_id)["claimed_by"] == "worker-a"


def test_takeover_is_opt_in_so_every_other_caller_keeps_the_old_contract(queue_db):
    from app.core.errors import E_TASK_CLAIMED, MeridianError
    from app.services.task_service import claim_task

    task_id = queue_db.create_task(title="opt-in takeover")
    queue_db.claim(task_id, "worker-a")
    queue_db.backdate_heartbeat(task_id, 4000)

    async def _plain_claim():
        async with queue_db.factory() as session:
            return await claim_task(session, worker="worker-b", task_id=task_id)

    with pytest.raises(MeridianError) as raised:
        queue_db.run(_plain_claim())

    assert raised.value.code == E_TASK_CLAIMED
    assert queue_db.row(task_id)["status"] == "in_progress"


def test_an_expired_claim_that_spent_the_ladder_ends_terminal_instead_of_rerunning(queue_db):
    """A crash-looping row must stop by name — the same rule `requeue_stale_claims` applies."""
    from app.core.errors import E_TASK_NOT_RUNNABLE, MeridianError
    from app.services import task_service

    task_id = queue_db.create_task(title="crash loop")
    queue_db.claim(task_id, "worker-a")  # attempts 1
    queue_db.backdate_heartbeat(task_id, 4000)
    # Two more executions through the takeover bring the row to the shipped ladder cap (3).
    assert _takeover(queue_db, task_id, "worker-b") is not None  # attempts 2
    queue_db.backdate_heartbeat(task_id, 4000)
    assert _takeover(queue_db, task_id, "worker-b") is not None  # attempts 3
    queue_db.backdate_heartbeat(task_id, 4000)

    with pytest.raises(MeridianError) as raised:
        _takeover(queue_db, task_id, "worker-c")

    assert raised.value.code == E_TASK_NOT_RUNNABLE
    stored = queue_db.row(task_id)
    assert stored["status"] == "failed"
    assert "lease expired after" in (stored["error_message"] or "")
    assert stored["claimed_by"] is None
    assert "task_claim_recovery_exhausted" in _actions(queue_db)
    assert int(task_service.settings.task_max_attempts) == 3, "the cap this test spends"


def test_broker_recovery_module_exposes_its_decision_vocabulary():
    """The result vocabulary is named, not magic strings scattered across the task body."""
    assert broker_recovery.RESTORED == "restored"
    assert broker_recovery.DROPPED == "dropped"
    assert broker_recovery.LEFT_LIVE_CLAIM == "left_live_claim"
    assert AGENT_TASK_NAME == "celery_app.tasks.agent_tasks.run_agent_task"
