"""R17 — the 3h `email-inbox-sweep`: pending inbox items become CS tasks, all the way to the prompt.

The chain this file proves end to end, with the REAL pieces at every step:

  submit_item (service) -> `_email_inbox_sweep` attach (deduped CS task + `metadata.inbox_item`)
  -> `execute_body` = the real `_execute_agent_task` (claim, context, real CustomerSupportAgent,
  CLAUDE_CLI_MOCK seam) -> the assembled prompt the model seam receives.

Everything below the broker boundary is real; the ONE interception is `enqueue_created`
(the external hand-off — an `async def` recording fake), the same boundary
`tests/unit/celery/test_budget_gate.py` no-ops with `_noop_delay`.

RED legs (measured this lane, receipt §RED): (a) dropping the `metadata` pass-through in
`celery_app/tasks/_common.py` unships the item from the task row; (b) dropping the
dispatched-marking UPDATE in `support_inbox_service.attach_pending` re-attaches items and
breaks the double-attach leg.  Restores are sha256-verified.
"""
from __future__ import annotations

import json

from app.services.support_inbox_service import inbox_task_dedupe_key

ITEM = {
    "provider_message_id": "msg-sweep-1",
    "from_address": "customer@example.com",
    "subject": "order #1042",
    "body": "R17-SWEEP-BODY: where is my order #1042? It was due Tuesday.",
}
BARE_TITLE = "Check inbox and reply to customer emails"


def _submit(queue_db, **over) -> int:
    from app.services.support_inbox_service import submit_item

    fields = {**ITEM, **over}

    async def _go() -> int:
        async with queue_db.factory() as db:
            item = await submit_item(db, **fields)
            await db.commit()
            return int(item.id)

    return queue_db.run(_go())


def _run_sweep(queue_db, monkeypatch) -> tuple[dict, list[int]]:
    """Run the REAL sweep body; intercept only the broker hand-off (external boundary).

    `sweep_bucket` is frozen so the bare path's dedupe key cannot straddle a real 3h boundary
    mid-test (the same reason `test_budget_gate.py` passes explicit buckets).
    """
    import celery_app.tasks.agent_tasks as agent_tasks

    enqueued: list[int] = []

    async def _record(task_ids, *, label):
        enqueued.extend(task_ids)

    monkeypatch.setattr(agent_tasks, "enqueue_created", _record)
    monkeypatch.setattr(agent_tasks, "sweep_bucket", lambda period_hours, now=None: "r17-test")
    result = queue_db.run(agent_tasks._email_inbox_sweep())
    return result, enqueued


def _task_rows(queue_db) -> list[dict]:
    return queue_db.query(
        "SELECT id, title, agent_type, status, source, priority, dedupe_key, metadata "
        "FROM tasks ORDER BY id"
    )


def _item_row(queue_db, item_id: int) -> dict:
    return queue_db.query("SELECT * FROM support_inbox_items WHERE id = ?", (item_id,))[0]


# ── the bare path is preserved exactly ───────────────────────────────────────────────────────
def test_with_nothing_pending_the_bare_bucket_task_is_kept(queue_db, monkeypatch):
    result, enqueued = _run_sweep(queue_db, monkeypatch)

    assert result["status"] == "queued"
    assert result["attached"] == []
    rows = _task_rows(queue_db)
    assert len(rows) == 1 and rows[0]["title"] == BARE_TITLE
    assert rows[0]["agent_type"] == "customer_support"
    assert rows[0]["metadata"] is None, "the pre-R17 task carries no payload — unchanged"
    assert rows[0]["dedupe_key"].endswith(":r17-test")
    assert enqueued == [rows[0]["id"]]

    again, _ = _run_sweep(queue_db, monkeypatch)
    assert again["status"] == "duplicate"
    assert len(_task_rows(queue_db)) == 1, "the bucket dedupe key still holds"


# ── the attach path ──────────────────────────────────────────────────────────────────────────
def test_a_pending_item_becomes_a_per_item_task_carrying_the_rd12_metadata(queue_db, monkeypatch):
    item_id = _submit(queue_db)

    result, enqueued = _run_sweep(queue_db, monkeypatch)

    assert result["status"] == "queued"
    (attached,) = result["attached"]
    assert attached["item_id"] == item_id
    assert attached["provider_message_id"] == "msg-sweep-1"
    assert attached["task_created"] is True

    rows = _task_rows(queue_db)
    assert len(rows) == 1, "the per-item task REPLACES the bare bucket task this tick"
    task = rows[0]
    assert task["id"] == attached["task_id"]
    assert task["title"] == "Reply to support email from customer@example.com"
    assert task["agent_type"] == "customer_support" and task["status"] == "pending"
    assert task["dedupe_key"] == inbox_task_dedupe_key("msg-sweep-1") == "support-inbox-msg-sweep-1"
    # the RAW row carries the exact payload the RD12 consumer reads (id/from/subject/body)
    assert json.loads(task["metadata"]) == {
        "inbox_item": {
            "id": "msg-sweep-1",
            "from": "customer@example.com",
            "subject": "order #1042",
            "body": ITEM["body"],
        }
    }
    assert enqueued == [task["id"]], "exactly the created task is handed to the broker"

    stored = _item_row(queue_db, item_id)
    assert stored["status"] == "dispatched"
    assert stored["task_id"] == task["id"]


def test_several_items_each_get_their_own_task_oldest_first(queue_db, monkeypatch):
    from datetime import UTC, datetime

    _submit(
        queue_db,
        provider_message_id="msg-late",
        received_at=datetime(2026, 10, 13, 12, 0, tzinfo=UTC),
    )
    _submit(
        queue_db,
        provider_message_id="msg-early",
        received_at=datetime(2026, 10, 13, 7, 0, tzinfo=UTC),
    )

    result, enqueued = _run_sweep(queue_db, monkeypatch)

    assert [row["provider_message_id"] for row in result["attached"]] == ["msg-early", "msg-late"]
    rows = _task_rows(queue_db)
    assert len(rows) == 2
    payloads = {json.loads(row["metadata"])["inbox_item"]["id"]: row["id"] for row in rows}
    assert set(payloads) == {"msg-early", "msg-late"}, "one task per item, never a batch"
    assert enqueued == [row["id"] for row in rows]


def test_a_second_sweep_attaches_nothing_and_double_attach_is_impossible(queue_db, monkeypatch):
    item_id = _submit(queue_db)
    first, _ = _run_sweep(queue_db, monkeypatch)
    (task_id,) = [row["task_id"] for row in first["attached"]]

    second, enqueued = _run_sweep(queue_db, monkeypatch)

    assert second["attached"] == [], "dispatched items are skipped — nothing re-attached"
    # nothing pending on the second tick: the pre-R17 bucket task appears (its own dedupe key)
    rows = _task_rows(queue_db)
    assert [row["title"] for row in rows].count(BARE_TITLE) == 1
    assert sum(1 for row in rows if row["dedupe_key"] == "support-inbox-msg-sweep-1") == 1
    assert enqueued == [rows[-1]["id"]], "only the newly created bare task is handed off"
    stored = _item_row(queue_db, item_id)
    assert stored["status"] == "dispatched" and stored["task_id"] == task_id, "mark unchanged"


def test_dispatched_items_are_skipped_while_a_new_item_still_attaches(queue_db, monkeypatch):
    first_item = _submit(queue_db, provider_message_id="msg-a")
    sweep_one, _ = _run_sweep(queue_db, monkeypatch)
    (task_a,) = [row["task_id"] for row in sweep_one["attached"]]

    _submit(queue_db, provider_message_id="msg-b")
    sweep_two, _ = _run_sweep(queue_db, monkeypatch)

    assert [row["provider_message_id"] for row in sweep_two["attached"]] == ["msg-b"]
    assert _item_row(queue_db, first_item)["task_id"] == task_a
    assert len(_task_rows(queue_db)) == 2


def test_a_full_budget_attaches_nothing_and_loses_nothing(queue_db, monkeypatch):
    """D3: a denied tick must leave every item PENDING for the next one — never half-dispatch."""
    item_id = _submit(queue_db)
    monkeypatch.setenv("POLSIA_KILL_SWITCH", "freeze")

    result, enqueued = _run_sweep(queue_db, monkeypatch)

    assert result["status"] == "budget_denied"
    assert "kill switch" in result["reason"]
    assert result["attached"] == [] and enqueued == []
    assert _task_rows(queue_db) == []
    assert _item_row(queue_db, item_id)["status"] == "pending"


# ── THE CHAIN: submit -> sweep -> row -> real worker -> the assembled prompt ─────────────────
def test_the_whole_chain_reaches_the_prompt_quarantined(queue_db, monkeypatch, execute_body):
    from app.agents.base_agent import BasePolsiaAgent

    item_id = _submit(queue_db, provider_message_id="msg-chain-1")
    result, enqueued = _run_sweep(queue_db, monkeypatch)
    (task_id,) = [row["task_id"] for row in result["attached"]]
    assert enqueued == [task_id]

    captured: list[dict] = []
    original = BasePolsiaAgent.call_claude_structured

    def recording(self, prompt, **kw):
        captured.append({"prompt": prompt, "system": kw.get("system_prompt_static", "")})
        return original(self, prompt, **kw)

    monkeypatch.setattr(BasePolsiaAgent, "call_claude_structured", recording)

    outcome = execute_body(task_id)

    assert outcome["status"] == "completed", outcome
    assert captured, "the real agent must reach the model seam"
    assert any(
        '<untrusted source="inbox" content_id="msg-chain-1">' in c["prompt"] for c in captured
    ), "the attached item must ride the real quarantine block"
    assert any(ITEM["body"] in c["prompt"] for c in captured)
    assert all(ITEM["body"] not in c["system"] for c in captured), (
        "the cacheable prefix must stay free of per-run customer material"
    )
    assert _item_row(queue_db, item_id)["status"] == "dispatched"
