"""V8 / seat C1 — the frozen chat contract, end to end, over HTTP.

Pins the brief's endpoint legs: every intent branch incl. the E_POLICY_DENIED refusal name;
confirm creates EXACTLY one task (reused token -> 409, no second task); expired token -> 409;
GET history shape; 401 without the owner key; the secret-echo leg (`sk-` never returned in
intent/reply/history); a money refusal creates NO task row; SANDBOX_MODE respected.
"""
from __future__ import annotations

import time as time_module

import pytest
from sqlalchemy import select

import celery_app.tasks.agent_tasks as agent_tasks
from app.config import settings
from app.models.report import ActivityLog
from app.models.task import Task
from app.services import chat_service as chat
from app.services.task_service import create_task

SECRET = "v8-unit-approval-secret"


@pytest.fixture(autouse=True)
def _configured_channel(monkeypatch):
    """A configured approvals channel + sandbox off, so reply texts are the stable forms."""
    monkeypatch.setattr(settings, "approval_secret", SECRET)
    monkeypatch.setattr(settings, "sandbox_mode", False)


@pytest.fixture(autouse=True)
def delivered(monkeypatch):
    """Record — never perform — the broker hand-off (`run_agent_task.delay`)."""
    sent: list[int] = []
    monkeypatch.setattr(agent_tasks.run_agent_task, "delay", lambda tid: sent.append(tid), raising=False)
    return sent


# ── auth ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_requires_the_owner_key(api_client):
    resp = await api_client.post("/api/v1/chat", json={"message": "status"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "E_AUTH"


@pytest.mark.asyncio
async def test_get_history_requires_the_owner_key(api_client):
    resp = await api_client.get("/api/v1/chat")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_confirm_requires_the_owner_key(api_client):
    resp = await api_client.post("/api/v1/chat/confirm", json={"confirm_token": "1.2.3"})
    assert resp.status_code == 401


# ── the answer branch ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_answer_branch_returns_the_frozen_201_body(api_client, auth_headers):
    resp = await api_client.post(
        "/api/v1/chat", json={"message": "how are things going"}, headers=auth_headers
    )
    assert resp.status_code == 201
    body = resp.json()
    assert set(body) == {"reply", "intent", "task_id", "requires_confirm", "confirm_token", "refusal"}
    assert body["reply"]["text"]
    assert body["intent"]["kind"] == "answer"
    assert body["intent"]["source"] == "dashboard summary"
    assert body["task_id"] is None
    assert body["requires_confirm"] is False
    assert body["confirm_token"] is None
    assert body["refusal"] is None


@pytest.mark.asyncio
async def test_answer_reads_the_real_database(api_client, auth_headers, async_db_session):
    await create_task(
        async_db_session, title="Seeded completed task", agent_type="finance",
        source="manual", status="completed",
    )
    await async_db_session.flush()
    resp = await api_client.post("/api/v1/chat", json={"message": "status"}, headers=auth_headers)
    assert resp.status_code == 201
    assert "1 completed" in resp.json()["reply"]["text"]


# ── the refusal branch ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_money_refusal_names_the_code_and_files_nothing(api_client, auth_headers, async_db_session):
    resp = await api_client.post(
        "/api/v1/chat", json={"message": "pay the contractor $500"}, headers=auth_headers
    )
    assert resp.status_code == 201, "the refusal is a 201 body field, not an HTTP error"
    body = resp.json()
    assert body["intent"]["kind"] == "refused"
    assert body["refusal"]["code"] == "E_POLICY_DENIED"
    assert body["refusal"]["message"] and body["refusal"]["how_to_fix"]
    assert "approvals" in body["refusal"]["how_to_fix"], "must name the approvals path"
    assert body["task_id"] is None and body["confirm_token"] is None
    assert body["requires_confirm"] is False

    tasks = (await async_db_session.execute(select(Task))).scalars().all()
    assert tasks == [], "a refused money request must never queue a task"
    activity = (await async_db_session.execute(select(ActivityLog))).scalars().all()
    assert [row.action for row in activity] == ["chat_message"]
    assert activity[0].agent_type == "polsia"


@pytest.mark.asyncio
async def test_sandbox_mode_is_respected(api_client, auth_headers, monkeypatch):
    monkeypatch.setattr(settings, "sandbox_mode", True)
    resp = await api_client.post(
        "/api/v1/chat", json={"message": "buy ads on google"}, headers=auth_headers
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["refusal"]["code"] == "E_POLICY_DENIED"
    assert "SANDBOX_MODE" in body["refusal"]["how_to_fix"]


@pytest.mark.asyncio
async def test_create_task_without_secret_refuses_by_name(api_client, auth_headers, monkeypatch):
    monkeypatch.setattr(settings, "approval_secret", "")
    resp = await api_client.post(
        "/api/v1/chat", json={"message": "create a task to research competitors"}, headers=auth_headers
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["intent"]["kind"] == "refused"
    assert body["refusal"]["code"] == "E_CONFIG"
    assert body["confirm_token"] is None


# ── the confirm branch: exactly once ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_confirm_creates_exactly_one_task_and_reuse_is_409(
    api_client, auth_headers, async_db_session, delivered
):
    post = await api_client.post(
        "/api/v1/chat",
        json={"message": "create a task to research competitor pricing"},
        headers=auth_headers,
    )
    assert post.status_code == 201
    body = post.json()
    assert body["intent"]["kind"] == "create_task"
    assert body["intent"]["agent_type"] == "competitor_research"
    assert body["requires_confirm"] is True
    assert body["task_id"] is None, "nothing exists until the owner confirms"
    token = body["confirm_token"]
    assert token

    tasks = (await async_db_session.execute(select(Task))).scalars().all()
    assert tasks == [], "the proposal alone must not create a task"

    conf = await api_client.post(
        "/api/v1/chat/confirm", json={"confirm_token": token}, headers=auth_headers
    )
    assert conf.status_code == 200
    task_id = conf.json()["task_id"]

    tasks = (await async_db_session.execute(select(Task))).scalars().all()
    assert len(tasks) == 1
    assert tasks[0].id == task_id
    assert tasks[0].agent_type == "competitor_research"
    assert tasks[0].source == "chat"
    assert delivered == [task_id], "the task was handed to a worker exactly once"

    replay = await api_client.post(
        "/api/v1/chat/confirm", json={"confirm_token": token}, headers=auth_headers
    )
    assert replay.status_code == 409
    err = replay.json()["error"]
    assert err["code"] == "E_TASK_NOT_RUNNABLE"
    assert err["message"] and err["how_to_fix"]
    tasks = (await async_db_session.execute(select(Task))).scalars().all()
    assert len(tasks) == 1, "a reused token must never create a second task"
    assert delivered == [task_id]


@pytest.mark.asyncio
async def test_broker_outage_is_a_named_503_and_the_token_stays_consumed(
    api_client, auth_headers, async_db_session, monkeypatch
):
    """The E_REDIS honesty branch (agents.py:102-128 shape): the created row survives the
    refusal with its measured terminal state, and the consumed token can never fire twice."""
    def dead_delay(*_args, **_kwargs):
        raise ConnectionError("redis down")

    monkeypatch.setattr(agent_tasks.run_agent_task, "delay", dead_delay, raising=False)

    post = await api_client.post(
        "/api/v1/chat", json={"message": "create a task to research competitors"}, headers=auth_headers
    )
    token = post.json()["confirm_token"]
    resp = await api_client.post(
        "/api/v1/chat/confirm", json={"confirm_token": token}, headers=auth_headers
    )
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "E_REDIS"

    tasks = (await async_db_session.execute(select(Task))).scalars().all()
    assert len(tasks) == 1, "the created row survives the refusal (load-bearing commit)"
    assert str(tasks[0].status) == "failed", "the row records its measured terminal state"

    replay = await api_client.post(
        "/api/v1/chat/confirm", json={"confirm_token": token}, headers=auth_headers
    )
    assert replay.status_code == 409
    assert len((await async_db_session.execute(select(Task))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_expired_token_is_a_named_409(api_client, auth_headers, monkeypatch):
    post = await api_client.post(
        "/api/v1/chat", json={"message": "create a task to research competitors"}, headers=auth_headers
    )
    token = post.json()["confirm_token"]

    future = time_module.time() + chat.CONFIRM_TOKEN_TTL_S + 5
    monkeypatch.setattr(chat.time, "time", lambda: future)
    resp = await api_client.post(
        "/api/v1/chat/confirm", json={"confirm_token": token}, headers=auth_headers
    )
    assert resp.status_code == 409
    err = resp.json()["error"]
    assert err["code"] == "E_TASK_NOT_RUNNABLE"
    assert "expired" in err["message"]


@pytest.mark.asyncio
async def test_malformed_token_is_a_named_422(api_client, auth_headers):
    resp = await api_client.post(
        "/api/v1/chat/confirm", json={"confirm_token": "not-a-token"}, headers=auth_headers
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "E_VALIDATION"


# ── history ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_history_shape_is_the_frozen_one(api_client, auth_headers):
    await api_client.post("/api/v1/chat", json={"message": "status"}, headers=auth_headers)
    await api_client.post(
        "/api/v1/chat", json={"message": "create a task to research competitor pricing"}, headers=auth_headers
    )
    resp = await api_client.get("/api/v1/chat?limit=50", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["total"], int)
    assert body["total"] == 4, "two exchanges persist owner + polsia rows each"
    items = body["items"]
    assert len(items) == 4
    for item in items:
        assert set(item) == {"role", "content", "created_at", "task_id"}
    assert [item["role"] for item in items] == ["owner", "polsia", "owner", "polsia"]
    assert items[0]["content"] == "status"
    assert items[0]["task_id"] is None


# ── secrets never echo ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_key_material_is_never_echoed(api_client, auth_headers, async_db_session):
    message = "create a task to rotate my key sk-live-deadbeef1234 right now"
    resp = await api_client.post("/api/v1/chat", json={"message": message}, headers=auth_headers)
    assert resp.status_code == 201
    body = resp.json()
    assert "sk-" not in resp.text, "no sk- byte-sequence may appear anywhere in the response"
    assert "sk-live-deadbeef1234" not in resp.text
    assert "[redacted]" in body["intent"]["title"]

    history = await api_client.get("/api/v1/chat", headers=auth_headers)
    assert "sk-" not in history.text, "stored chat content must never carry the key either"

    conf = await api_client.post(
        "/api/v1/chat/confirm", json={"confirm_token": body["confirm_token"]}, headers=auth_headers
    )
    assert conf.status_code == 200
    tasks = (await async_db_session.execute(select(Task))).scalars().all()
    assert "sk-" not in tasks[0].title
    assert "sk-" not in (tasks[0].description or "")


@pytest.mark.asyncio
async def test_key_material_never_echoes_on_the_answer_branch(api_client, auth_headers):
    resp = await api_client.post(
        "/api/v1/chat",
        json={"message": "my key is sk-live-deadbeef1234 — what is our status"},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    assert "sk-" not in resp.text
