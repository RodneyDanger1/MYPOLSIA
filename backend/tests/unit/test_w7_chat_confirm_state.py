"""WAVE 7 / logic-debugger — the CHAT CONFIRM state machine under injected conversation failure.

WHY (wave-7 job 3, and the wave-6 F-40 restore): the F-40 fix made the confirm gate restorable from
the STORED proposal, so the gate is now re-offered after a reload for its ORIGINAL 15-minute window.
That repair introduced a state question it did not answer: what happens when the owner sends a
SECOND proposal while the first gate is still open?  Both rows are `state="pending"`, both tokens
are live, the history restores only the NEWEST one, and the older stored message says *"Reloading
the page brings the card back while that token is still valid"* — which is false for the older row.

THE DEFECT (measured on the wave-6 tree, 8c941d1, RED leg in the wave-7 receipt): the first
proposal's token still CONFIRMS after the second proposal exists. A stale tab (or a script holding
the 201 body) files the OLD plan minutes after the owner moved on; the UI never shows that card and
a reload can never restore it, so the state is unreachable by design and reachable by token.

WHAT THIS PINS (the single-gate rule):
  * a NEW proposal supersedes every older pending proposal (`state="superseded"` + `superseded_by`);
  * the superseded token is refused BY NAME (`E_TASK_NOT_RUNNABLE`, `context.reason="superseded"`)
    and creates nothing;
  * a plain ANSWER never supersedes an open gate;
  * a gate RESTORED from history and then superseded by a new send cannot be confirmed either
    (the reload-then-send leg — the exact seam wave 6 introduced);
  * "already consumed" keeps precedence over "superseded" for a row that was confirmed first;
  * the restore read never mints a token that is ALREADY expired when the clock ticks between the
    window check and the mint (the boundary second).
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

import celery_app.tasks.agent_tasks as agent_tasks
from app.config import settings
from app.models.chat import ChatMessage, ChatRole
from app.models.task import Task
from app.services import chat_service as chat

SECRET = "w7-chat-confirm-secret"
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


async def _propose(api_client, auth_headers, text: str = PROPOSAL) -> dict:
    resp = await api_client.post("/api/v1/chat", json={"message": text}, headers=auth_headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _owner_rows(db) -> list[ChatMessage]:
    rows = await db.execute(
        select(ChatMessage).where(ChatMessage.role == ChatRole.OWNER).order_by(ChatMessage.id.asc())
    )
    return list(rows.scalars().all())


async def _confirm(api_client, auth_headers, token: str):
    return await api_client.post(
        "/api/v1/chat/confirm", json={"confirm_token": token}, headers=auth_headers
    )


# ── 1. a new proposal supersedes the older gate ───────────────────────────────
@pytest.mark.asyncio
async def test_a_new_proposal_supersedes_the_older_pending_row(
    api_client, auth_headers, async_db_session
):
    first = await _propose(api_client, auth_headers)
    second = await _propose(api_client, auth_headers)
    assert first["confirm_token"] and second["confirm_token"]

    rows = await _owner_rows(async_db_session)
    assert len(rows) == 2
    older, newer = rows
    assert older.intent["state"] == "superseded", (
        "the older proposal must stop being a live gate when a newer one is created"
    )
    assert older.intent["superseded_by"] == newer.id
    assert older.intent["kind"] == "create_task"
    assert newer.intent["state"] == "pending", "the newest proposal is the one live gate"
    assert older.task_id is None and newer.task_id is None
    # The supersede is an INTENT change only: the transcript is still readable verbatim.
    assert older.content == PROPOSAL


@pytest.mark.asyncio
async def test_the_superseded_token_is_refused_by_name_and_creates_nothing(
    api_client, auth_headers, async_db_session, delivered
):
    first = await _propose(api_client, auth_headers)
    await _propose(api_client, auth_headers)

    resp = await _confirm(api_client, auth_headers, first["confirm_token"])
    assert resp.status_code == 409, "a superseded gate must never file its plan"
    body = resp.json()
    assert body["error"]["code"] == "E_TASK_NOT_RUNNABLE"
    assert body["error"]["context"]["reason"] == "superseded"
    assert body["error"]["context"]["chat_message_id"] == (await _owner_rows(async_db_session))[0].id
    assert body["error"]["how_to_fix"]

    tasks = (await async_db_session.execute(select(Task))).scalars().all()
    assert tasks == [], "nothing was created from the stale gate"
    assert delivered == [], "nothing was dispatched"


@pytest.mark.asyncio
async def test_three_proposals_leave_exactly_one_live_gate(api_client, auth_headers, async_db_session):
    tokens = [(await _propose(api_client, auth_headers))["confirm_token"] for _ in range(3)]
    rows = await _owner_rows(async_db_session)
    assert [row.intent["state"] for row in rows] == ["superseded", "superseded", "pending"]
    # each proposal was superseded by the NEXT one (the state the row was in when it was retired)
    assert rows[0].intent["superseded_by"] == rows[1].id
    assert rows[1].intent["superseded_by"] == rows[2].id

    restored = (await api_client.get("/api/v1/chat", headers=auth_headers)).json()["pending_confirm"]
    assert restored is not None and restored["chat_message_id"] == rows[2].id

    # Only the newest token confirms; every older one is refused by name.
    for stale in tokens[:-1]:
        refused = await _confirm(api_client, auth_headers, stale)
        assert refused.status_code == 409
        assert refused.json()["error"]["context"]["reason"] == "superseded"
    ok = await _confirm(api_client, auth_headers, tokens[-1])
    assert ok.status_code == 200


# ── 2. the wave-6 seam: RESTORE then a NEW SEND ──────────────────────────────
@pytest.mark.asyncio
async def test_a_restored_gate_is_superseded_by_a_new_send_and_cannot_confirm(
    api_client, auth_headers, async_db_session, delivered
):
    """The exact sequence wave 6 made reachable: reload (restore) -> send again -> stale CONFIRM."""
    await _propose(api_client, auth_headers)
    restored = (await api_client.get("/api/v1/chat?limit=50", headers=auth_headers)).json()[
        "pending_confirm"
    ]
    assert restored is not None and restored["token"]

    second = await _propose(api_client, auth_headers)
    assert second["confirm_token"]

    stale = await _confirm(api_client, auth_headers, restored["token"])
    assert stale.status_code == 409, (
        "a gate restored before a new proposal must not file its plan after it"
    )
    assert stale.json()["error"]["context"]["reason"] == "superseded"
    assert (await async_db_session.execute(select(Task))).scalars().all() == []
    assert delivered == []

    fresh = await _confirm(api_client, auth_headers, second["confirm_token"])
    assert fresh.status_code == 200


@pytest.mark.asyncio
async def test_the_original_and_the_restored_token_are_one_binding(
    api_client, auth_headers, async_db_session, delivered
):
    """A restore re-mints the token for the SAME row: two live tokens, one consumption.

    Wave 6 made the history read MINT a token (the approvals minter is nonce-bearing), so the
    browser can hold the 201 body's token while a reload hands the page a second one.  Both name
    the same row/plan/expiry, and the row's consumption marker is the single-use rule: the first
    click files one task, the other token is the named replay.
    """
    posted = await _propose(api_client, auth_headers)
    restored = (await api_client.get("/api/v1/chat", headers=auth_headers)).json()["pending_confirm"]
    assert restored is not None
    assert restored["token"] != posted["confirm_token"], "the re-mint is nonce-bearing (not a copy)"

    first = await _confirm(api_client, auth_headers, posted["confirm_token"])
    assert first.status_code == 200
    second = await _confirm(api_client, auth_headers, restored["token"])
    assert second.status_code == 409
    assert second.json()["error"]["context"]["reason"] == "already_used"
    assert len((await async_db_session.execute(select(Task))).scalars().all()) == 1
    assert delivered == [first.json()["task_id"]]


@pytest.mark.asyncio
async def test_a_plain_answer_does_not_supersede_the_open_gate(api_client, auth_headers, async_db_session):
    first = await _propose(api_client, auth_headers)
    answer = await api_client.post("/api/v1/chat", json={"message": "status"}, headers=auth_headers)
    assert answer.status_code == 201 and answer.json()["intent"]["kind"] == "answer"

    rows = await _owner_rows(async_db_session)
    proposals = [row for row in rows if row.intent is not None]
    assert len(proposals) == 1, "an answer writes an owner row with NO intent"
    assert proposals[0].intent["state"] == "pending"
    ok = await _confirm(api_client, auth_headers, first["confirm_token"])
    assert ok.status_code == 200


@pytest.mark.asyncio
async def test_already_consumed_keeps_precedence_over_superseded(
    api_client, auth_headers, async_db_session
):
    first = await _propose(api_client, auth_headers)
    ok = await _confirm(api_client, auth_headers, first["confirm_token"])
    assert ok.status_code == 200
    await _propose(api_client, auth_headers)  # a later proposal exists

    replay = await _confirm(api_client, auth_headers, first["confirm_token"])
    assert replay.status_code == 409
    assert replay.json()["error"]["context"]["reason"] == "already_used", (
        "a token spent BEFORE the supersede must report the spend, not the supersede"
    )


# ── 3. the restore boundary second must not mint a dead token ────────────────
@pytest.mark.asyncio
async def test_the_restore_never_mints_a_token_that_is_already_expired(
    api_client, auth_headers, async_db_session, monkeypatch
):
    """The clock ticks between the window check and the mint at exactly `created_at + TTL`.

    Without the guard the response carries a token whose `exp` is one second in the PAST while
    `expires_at` says the gate is open — a card that promises a click and answers `expired`.
    """
    await _propose(api_client, auth_headers)
    row = (await _owner_rows(async_db_session))[0]
    assert row.created_at is not None
    created = row.created_at if row.created_at.tzinfo is not None else row.created_at.replace(tzinfo=UTC)
    boundary = int(created.timestamp()) + chat.CONFIRM_TOKEN_TTL_S
    row.created_at = datetime.fromtimestamp(boundary - chat.CONFIRM_TOKEN_TTL_S, UTC)
    await async_db_session.commit()

    # First clock read (the window check) is exactly at the boundary; every later read is one
    # second past it — the mint sees `ttl_s = -1` without a guard.
    calls = {"n": 0}

    def _ticking_time() -> float:
        calls["n"] += 1
        return float(boundary if calls["n"] == 1 else boundary + 1)

    monkeypatch.setattr(chat.time, "time", _ticking_time)

    restored = await chat.restore_pending_confirm(async_db_session)
    assert restored is None, (
        "a gate whose window closed between the check and the mint must not be offered"
    )
