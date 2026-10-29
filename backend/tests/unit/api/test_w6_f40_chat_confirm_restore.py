"""WAVE 6 / F-40 — the confirm gate is restored from the STORED proposal (or the copy says why not).

THE DEFECT (`visuals/VISUALS-FINDINGS-W5.md` §4, measured live on `7b81f56`): after one chat
proposal, the rail showed the reply *"Confirm and I'll queue it — nothing is created until you
confirm (single-use token, expires in 15 min)"* and the confirm card.  After ANY reload the card
and its CONFIRM button were GONE (`chat-confirm-card` nodes = 0), while the stored message still
gave that instruction and the activity strip still read *awaiting confirm*.  The confirm token
lived only in the 201 body, and the frozen history read carried no gate — the owner lost the
request and the screen instructed an action it no longer offered.

WHAT THIS PINS:

  * `GET /api/v1/chat` restores the gate for the newest STILL-OPEN stored proposal: a token that
    verifies against the same chat row, the row's original `created_at + 15 min` expiry, and the
    same `create_task` facts (agent, title, paid-run caps) the 201 served;
  * the restored token is the SAME single-use binding: confirming once creates exactly one task,
    a replay is the named 409, and after consumption the history restores NOTHING;
  * an expired proposal window restores nothing (no card may be offered for a dead token), and an
    unconfigured APPROVAL_SECRET restores nothing (a token that could never verify is not minted);
  * the per-row history shape is unchanged — the gate is a separate top-level key;
  * the stored reply copy itself is true after a reload: it says the card comes back while the
    token is valid and that the stored message cannot queue anything once it expires.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

import celery_app.tasks.agent_tasks as agent_tasks
from app.config import settings
from app.models.chat import ChatMessage, ChatRole
from app.models.task import Task
from app.services import chat_service as chat

SECRET = "w6-f40-approval-secret"
PROPOSAL = "create a task to research competitor pricing"


@pytest.fixture(autouse=True)
def _configured_channel(monkeypatch):
    monkeypatch.setattr(settings, "approval_secret", SECRET)
    monkeypatch.setattr(settings, "sandbox_mode", False)


@pytest.fixture(autouse=True)
def delivered(monkeypatch):
    """Record — never perform — the broker hand-off (`run_agent_task.delay`)."""
    sent: list[int] = []
    monkeypatch.setattr(agent_tasks.run_agent_task, "delay", lambda tid: sent.append(tid), raising=False)
    return sent


async def _owner_rows(db) -> list[ChatMessage]:
    rows = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.role == ChatRole.OWNER)
        .order_by(ChatMessage.id.asc())
    )
    return list(rows.scalars().all())


async def _propose(api_client, auth_headers) -> dict:
    resp = await api_client.post("/api/v1/chat", json={"message": PROPOSAL}, headers=auth_headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ── 1. the restore itself ─────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_history_restores_the_open_confirm_gate(api_client, auth_headers, async_db_session):
    posted = await _propose(api_client, auth_headers)
    assert posted["requires_confirm"] is True and posted["confirm_token"]

    history = await api_client.get("/api/v1/chat?limit=50", headers=auth_headers)
    assert history.status_code == 200, history.text
    body = history.json()

    restored = body["pending_confirm"]
    assert restored is not None, "an open proposal must be restorable after a reload"
    assert isinstance(restored["token"], str) and restored["token"] != ""
    assert isinstance(restored["chat_message_id"], int)

    # The token verifies against the SAME stored row (the single-use binding did not move).
    check = chat.check_confirm_token(SECRET.encode(), restored["token"])
    assert check.ok, check.reason
    assert check.chat_message_id == restored["chat_message_id"]
    owner_rows = await _owner_rows(async_db_session)
    assert [row.id for row in owner_rows] == [restored["chat_message_id"]]
    intent = owner_rows[0].intent
    assert intent["kind"] == "create_task" and intent["state"] == "pending"

    # The restored window is the row's ORIGINAL created_at + 15 min (never a longer-lived token).
    created = owner_rows[0].created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    expires_at = datetime.fromisoformat(restored["expires_at"].replace("Z", "+00:00"))
    assert abs((expires_at - (created + timedelta(seconds=chat.CONFIRM_TOKEN_TTL_S))).total_seconds()) <= 1

    # The disclosure facts ride the block, so the restored card states the caps the click obeys.
    restored_intent = restored["intent"]
    assert restored_intent["kind"] == "create_task"
    assert restored_intent["agent_type"] == posted["intent"]["agent_type"]
    assert restored_intent["title"] == posted["intent"]["title"]
    assert restored_intent["paid_run"] is True
    assert restored_intent["run_cost_cap_usd"] == posted["intent"]["run_cost_cap_usd"]
    assert restored_intent["day_cost_cap_usd"] == posted["intent"]["day_cost_cap_usd"]


@pytest.mark.asyncio
async def test_the_restored_token_confirms_exactly_once_and_then_disappears(
    api_client, auth_headers, async_db_session, delivered
):
    await _propose(api_client, auth_headers)
    restored = (await api_client.get("/api/v1/chat", headers=auth_headers)).json()["pending_confirm"]
    assert restored is not None

    conf = await api_client.post(
        "/api/v1/chat/confirm", json={"confirm_token": restored["token"]}, headers=auth_headers
    )
    assert conf.status_code == 200, conf.text
    task_id = conf.json()["task_id"]
    tasks = (await async_db_session.execute(select(Task))).scalars().all()
    assert [task.id for task in tasks] == [task_id]
    assert delivered == [task_id], "one restored confirm dispatches one run"

    # Consumed: a reload no longer offers a gate, and the replay is the named 409 (no 2nd task).
    after = (await api_client.get("/api/v1/chat", headers=auth_headers)).json()
    assert after["pending_confirm"] is None
    replay = await api_client.post(
        "/api/v1/chat/confirm", json={"confirm_token": restored["token"]}, headers=auth_headers
    )
    assert replay.status_code == 409
    assert replay.json()["error"]["code"] == "E_TASK_NOT_RUNNABLE"
    tasks = (await async_db_session.execute(select(Task))).scalars().all()
    assert len(tasks) == 1


@pytest.mark.asyncio
async def test_the_newest_pending_proposal_is_the_one_restored(api_client, auth_headers, async_db_session):
    await _propose(api_client, auth_headers)
    second = await _propose(api_client, auth_headers)

    owner_rows = await _owner_rows(async_db_session)
    assert len(owner_rows) == 2
    newest = owner_rows[-1]
    restored = (await api_client.get("/api/v1/chat", headers=auth_headers)).json()["pending_confirm"]
    assert restored is not None
    assert restored["chat_message_id"] == newest.id
    # ...and the newest reply is the one whose copy the owner is reading.
    assert second["confirm_token"] is not None


# ── 2. when nothing may be restored ───────────────────────────────────────────
@pytest.mark.asyncio
async def test_an_expired_proposal_window_restores_nothing(
    api_client, auth_headers, async_db_session
):
    await _propose(api_client, auth_headers)
    rows = await _owner_rows(async_db_session)
    rows[0].created_at = datetime.now(UTC) - timedelta(seconds=chat.CONFIRM_TOKEN_TTL_S + 60)
    await async_db_session.commit()

    body = (await api_client.get("/api/v1/chat", headers=auth_headers)).json()
    assert body["pending_confirm"] is None, "a closed token window must not offer a card"
    # The stored transcript is untouched: the message is still there (the copy says what to do).
    assert any("send the request again" in item["content"] for item in body["items"])


@pytest.mark.asyncio
async def test_an_unconfigured_secret_restores_nothing(api_client, auth_headers, monkeypatch):
    await _propose(api_client, auth_headers)
    monkeypatch.setattr(settings, "approval_secret", "")

    body = (await api_client.get("/api/v1/chat", headers=auth_headers)).json()
    assert body["pending_confirm"] is None


@pytest.mark.asyncio
async def test_a_plain_answer_exchange_restores_nothing(api_client, auth_headers):
    resp = await api_client.post("/api/v1/chat", json={"message": "status"}, headers=auth_headers)
    assert resp.status_code == 201 and resp.json()["intent"]["kind"] == "answer"

    body = (await api_client.get("/api/v1/chat", headers=auth_headers)).json()
    assert body["pending_confirm"] is None


@pytest.mark.asyncio
async def test_a_pending_row_whose_task_already_exists_is_not_restored(async_db_session):
    """Belt as well as braces: the consumption marker sets `task_id`, and a linked row is done."""
    from app.services.task_service import create_task

    task = await create_task(
        async_db_session, title="Already filed", agent_type="orchestrator", source="chat"
    )
    await async_db_session.flush()
    async_db_session.add(
        ChatMessage(
            role=ChatRole.OWNER,
            content="create a task to file me",
            intent={"kind": "create_task", "state": "pending", "plan": {"agent_type": "orchestrator"}},
            task_id=task.id,
        )
    )
    await async_db_session.commit()

    assert await chat.restore_pending_confirm(async_db_session) is None


@pytest.mark.asyncio
async def test_a_row_with_no_timestamp_cannot_be_restored(async_db_session):
    """No stamp means the window cannot be computed — a made-up expiry would be fabricated."""
    row = ChatMessage(
        role=ChatRole.OWNER,
        content="create a task with no stamp",
        intent={"kind": "create_task", "state": "pending", "plan": {"agent_type": "orchestrator"}},
        task_id=None,
    )
    assert row.created_at is None  # before the DB default is applied
    assert chat.pending_confirm_expiry(row) is None


# ── 3. the wire shape and the stored copy ────────────────────────────────────
@pytest.mark.asyncio
async def test_the_history_row_shape_is_unchanged(api_client, auth_headers):
    await _propose(api_client, auth_headers)
    body = (await api_client.get("/api/v1/chat", headers=auth_headers)).json()

    assert set(body) == {"items", "total", "pending_confirm"}
    for item in body["items"]:
        assert set(item) == {"role", "content", "created_at", "task_id"}


@pytest.mark.asyncio
async def test_the_stored_reply_states_the_reload_truth(api_client, auth_headers):
    posted = await _propose(api_client, auth_headers)
    text = posted["reply"]["text"]
    assert "Confirm and I'll queue it" in text
    assert "Reloading the page brings the card back" in text
    assert "once it expires this stored message cannot queue anything" in text
    # ...and the exact sentence is what PERSISTS (a reload reads the stored row, not the 201 body).
    body = (await api_client.get("/api/v1/chat", headers=auth_headers)).json()
    stored_reply = [item for item in body["items"] if item["role"] == "meridian"][-1]
    assert stored_reply["content"] == text
