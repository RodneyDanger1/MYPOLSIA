"""T02b (FU-1) — the approval-token HTTP DELIVERY path: `POST /api/v1/approvals` + `task_id`.

Wave-W1 REQUESTS §1 recorded the gap this file closes: the firewall's token read path was live
(`RunApproval` -> `side_effects._resolve_approval` -> `decide()` ALLOW), the worker already
published `tasks.metadata["approval_token"]`, but **nothing over HTTP could put the minted token
onto a task** — `TaskCreate` carries no `metadata` and the mint route had no attach target, so
the loop's second half could only be exercised through a direct service call.  The owner's chosen
design (option (b)): the mint request gains an OPTIONAL `task_id`; when present the token is
written into that task's `metadata` and the row is re-queued (`awaiting_approval` -> `pending`,
the exact fields the dispatcher's claim keys on) and handed to a worker.

What every leg drives: the REAL route over ASGI (dependency `get_db` pointed at the queue FILE —
the worker's own database), the REAL `_execute_agent_task` worker body, the REAL firewall consumer
(the mock CLI seam is the only double).  The full-loop leg is the acceptance shape:

    run #1 parks the task `awaiting_approval` with the fingerprint recorded
      -> POST /approvals {action_fingerprint, task_id} over HTTP
      -> the row is back to `pending` with `approval_token` in its metadata (+ delivered to the broker)
      -> run #2 executes and the gated action PROCEEDS (`decision: allow`, `approved: True`).

Guardrail legs: a missing task is a named 404; a task not `awaiting_approval` is refused by name
(E_VALIDATION, current status in context) and its metadata is never silently overwritten; an
unconfigured channel still refuses FIRST (E_CONFIG); a dead broker after the attach is a named
E_REDIS refusal with the row's terminal state recorded.

RED legs (driver: wave-W5/run_red_legs.py, byte-safe, sha256-verified restore):
  (a) the metadata-write line removed -> the full loop's second run parks AGAIN (never completes);
  (b) the status guard removed -> the wrong-status leg returns 201 and the metadata is overwritten.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import app.agents.crew_factory as crew_factory
from app.agents.policy_firewall import (
    ActionClass,
    current_run_approval,
    fingerprint,
    reset_run_approval,
)
from app.agents.side_effects import request_side_effect, side_effect_receipt
from app.config import settings
from app.core.errors import E_CONFIG, E_NOT_FOUND, E_REDIS, E_VALIDATION
from tests.unit.celery.conftest import RunSpy

SECRET = "unit-test-approval-secret"
REFUND_PAYLOAD = {"action": "refund", "payment_intent": "pi_3T02b"}


# ── fixtures ──────────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _quiet_redis(monkeypatch):
    """No network: the worker legs publish activity through `events.get_redis` (the celery
    conftest's autouse guard is scoped to `tests/unit/celery/`)."""
    mock = AsyncMock()
    mock.publish = AsyncMock(return_value=1)
    monkeypatch.setattr("app.core.events.get_redis", lambda: mock)
    return mock


@pytest.fixture(autouse=True)
def _clean_run_context():
    """No run context leaks between cases (the worker resets it in a `finally`; tests do too)."""
    reset_run_approval()
    yield
    reset_run_approval()


@pytest.fixture(autouse=True)
def _real_sends(monkeypatch):
    """The gated legs need REAL-SENDS mode: in sandbox every external action is drafted before the
    approval machinery is reached (the shipped default, exercised by its own tests)."""
    monkeypatch.setattr(settings, "sandbox_mode", False)


def _configure_secret(monkeypatch, value: str) -> None:
    """Point the live settings object at `value` (the reader is a defensive getattr)."""
    monkeypatch.setattr(settings, "approval_secret", value)


@pytest_asyncio.fixture
async def queue_client(queue_db, monkeypatch):
    """An ASGI client whose requests talk to the queue FILE — the worker's own database.

    `get_db`'s own contract is mirrored (auto-commit on success, rollback on any exception) so a
    route write lands exactly where the worker will read it and a refusal leaves the file
    byte-identical.  Using ONE database for both sides is what makes the full-loop leg a real
    end-to-end HTTP test rather than two halves facing different stores.
    """
    from app.core.database import get_db
    from app.main import app

    monkeypatch.setattr(settings, "api_key", "test-key")

    async def _get_db():
        async with queue_db.factory() as db:
            try:
                yield db
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    app.dependency_overrides[get_db] = _get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


# ── helpers ───────────────────────────────────────────────────────────────────────────────────


def _refund_fingerprint() -> str:
    return fingerprint(ActionClass.EXTERNAL_IRREVERSIBLE.value, "stripe", REFUND_PAYLOAD)


def _create_task(queue_db, *, title: str, metadata: dict | None = None, status: str = "pending") -> int:
    from app.services.task_service import create_task

    async def _make() -> int:
        async with queue_db.factory() as db:
            task = await create_task(
                db, title=title, agent_type="finance", metadata=metadata, status=status
            )
            await db.commit()
            return int(task.id)

    return queue_db.run(_make())


def _create_task_raw_metadata(queue_db, metadata) -> int:
    """Seed a row whose `metadata` blob is NOT an object (a raw/ops writer) — the edge the merge
    guard must refuse instead of replacing."""
    from app.models.task import Task, TaskStatus

    async def _make() -> int:
        async with queue_db.factory() as db:
            task = Task(
                title="raw metadata row",
                agent_type="finance",
                status=TaskStatus.AWAITING_APPROVAL,
            )
            db.add(task)
            await db.flush()
            task.metadata_json = metadata
            await db.commit()
            return int(task.id)

    return queue_db.run(_make())


def _refund_delegate(seen: dict):
    """A stand-in for the finance agent's refund leg: the REAL firewall call the agent makes."""

    def delegate(agent_type, task_dict, context):  # noqa: ANN001
        published = current_run_approval()
        seen["token"] = published.token if published is not None else None
        outcome = request_side_effect(
            "finance",
            ActionClass.EXTERNAL_IRREVERSIBLE,
            "stripe",
            action="refund",
            payload=dict(REFUND_PAYLOAD),
            value_usd=30.0,
            sends_today=0,
        )
        receipt = side_effect_receipt(outcome, action="refund", ref="pi_3T02b")
        return {"summary": "refund proposed", "side_effects": [receipt]}

    return delegate


def _install_delegate(monkeypatch, queue_db, delegate) -> RunSpy:
    spy = RunSpy(queue_db, delegate=delegate)
    monkeypatch.setattr(crew_factory, "run_agent_for_task", spy)
    return spy


def _install_delay_recorder(monkeypatch) -> list[int]:
    """Record — never perform — the broker hand-off (`run_agent_task.delay`)."""
    import celery_app.tasks.agent_tasks as agent_tasks

    delivered: list[int] = []
    monkeypatch.setattr(
        agent_tasks.run_agent_task,
        "delay",
        lambda task_id: delivered.append(task_id),
        raising=False,
    )
    return delivered


def _latest_run_output(queue_db, task_id: int) -> dict:
    rows = queue_db.query(
        "SELECT output FROM agent_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1", (task_id,)
    )
    return json.loads(rows[0]["output"]) if rows and rows[0]["output"] else {}


def _parked_metadata() -> dict:
    return {"approval_required": [{"fingerprint": _refund_fingerprint(), "action": "refund"}]}


# ── 1. the full loop over HTTP ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_full_loop_over_http_the_token_is_delivered_and_the_gated_action_proceeds(
    queue_db, queue_client, auth_headers, monkeypatch
):
    from celery_app.tasks.agent_tasks import _execute_agent_task

    _configure_secret(monkeypatch, SECRET)
    delivered = _install_delay_recorder(monkeypatch)
    seen: dict = {}
    _install_delegate(monkeypatch, queue_db, _refund_delegate(seen))
    task_id = _create_task(queue_db, title="T02b full loop")

    # 1. the run parks the task with the fingerprint recorded (the T02 construction, unchanged)
    first = queue_db.run(_execute_agent_task(task_id))
    assert first["status"] == "awaiting_approval"
    fp = first["approval_required"][0]["fingerprint"]
    assert queue_db.row(task_id)["status"] == "awaiting_approval"

    # 2. the missing half over HTTP: mint + deliver + re-queue in ONE owner call
    resp = await queue_client.post(
        "/api/v1/approvals",
        json={"action_fingerprint": fp, "task_id": task_id},
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    token = resp.json()["token"]

    # 3. the row is back to `pending` — the dispatcher's own claimability rule — with the token
    row = queue_db.row(task_id)
    assert row["status"] == "pending"
    stored = json.loads(row["metadata"])
    assert stored["approval_token"] == token
    assert stored["approval_required"][0]["fingerprint"] == fp, "the merge keeps the parked record"
    assert row["claimed_by"] is None and row["claimed_at"] is None and row["heartbeat_at"] is None
    assert delivered == [task_id], "the re-queue must hand the row to a worker (after the commit)"

    # 4. execute — the token the row carries turns the firewall's REQUIRE_APPROVAL into ALLOW
    second = queue_db.run(_execute_agent_task(task_id))
    assert second["status"] == "completed"
    assert second["approval_required"] == []
    assert seen["token"] == token, "the run the worker started READ the token it was dispatched with"
    stored_run = _latest_run_output(queue_db, task_id)
    assert stored_run["side_effects"][0]["decision"] == "allow"
    assert stored_run["side_effects"][0]["approved"] is True
    assert queue_db.row(task_id)["status"] == "completed"


@pytest.mark.asyncio
async def test_a_mint_without_a_task_id_is_byte_identical_to_before(
    queue_db, queue_client, auth_headers, monkeypatch
):
    """The pre-T02b request shape must be untouched: same response fields, no write, no dispatch."""
    _configure_secret(monkeypatch, SECRET)
    delivered = _install_delay_recorder(monkeypatch)
    task_id = _create_task(
        queue_db, title="parked but not targeted", status="awaiting_approval",
        metadata=_parked_metadata(),
    )

    fp = _refund_fingerprint()
    resp = await queue_client.post(
        "/api/v1/approvals", json={"action_fingerprint": fp}, headers=auth_headers
    )

    assert resp.status_code == 201
    body = resp.json()
    assert set(body) == {"token", "action_fingerprint", "expires_at", "ttl_s"}
    assert body["action_fingerprint"] == fp
    row = queue_db.row(task_id)
    assert row["status"] == "awaiting_approval", "no task was touched by a plain mint"
    assert "approval_token" not in (json.loads(row["metadata"]) if row["metadata"] else {})
    assert delivered == [], "no dispatch without an attach target"


# ── 2. the guardrails: 404 / wrong status / unconfigured channel — never a partial write ──────


@pytest.mark.asyncio
async def test_a_missing_task_is_a_named_404(queue_client, auth_headers, monkeypatch):
    _configure_secret(monkeypatch, SECRET)

    resp = await queue_client.post(
        "/api/v1/approvals",
        json={"action_fingerprint": _refund_fingerprint(), "task_id": 987654},
        headers=auth_headers,
    )

    assert resp.status_code == 404
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == E_NOT_FOUND
    assert body["error"]["context"]["task_id"] == 987654
    assert "987654" in body["error"]["message"]
    assert resp.headers.get("X-Polsia-Error-Code") == E_NOT_FOUND


@pytest.mark.asyncio
async def test_a_task_not_awaiting_approval_is_refused_by_name_and_left_untouched(
    queue_db, queue_client, auth_headers, monkeypatch
):
    _configure_secret(monkeypatch, SECRET)
    original_metadata = {"inbox_item": {"id": "msg-1", "body": "where is my order"}}
    task_id = _create_task(
        queue_db, title="not parked", metadata=original_metadata, status="pending"
    )

    resp = await queue_client.post(
        "/api/v1/approvals",
        json={"action_fingerprint": _refund_fingerprint(), "task_id": task_id},
        headers=auth_headers,
    )

    assert resp.status_code == 422
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == E_VALIDATION
    assert body["error"]["context"]["status"] == "pending"
    assert body["error"]["context"]["expected"] == "awaiting_approval"
    row = queue_db.row(task_id)
    assert row["status"] == "pending"
    assert json.loads(row["metadata"]) == original_metadata, "never a silent metadata overwrite"


@pytest.mark.asyncio
async def test_a_completed_task_is_refused_by_name_with_its_own_status(
    queue_db, queue_client, auth_headers, monkeypatch
):
    from celery_app.tasks.agent_tasks import _execute_agent_task

    _configure_secret(monkeypatch, SECRET)
    # a run with no side effects completes cleanly — a task that is NOT parked
    _install_delegate(monkeypatch, queue_db, lambda agent_type, t, c: {"summary": "nothing gated"})
    task_id = _create_task(queue_db, title="completed, not parked")
    done = queue_db.run(_execute_agent_task(task_id))
    assert done["status"] == "completed"

    resp = await queue_client.post(
        "/api/v1/approvals",
        json={"action_fingerprint": _refund_fingerprint(), "task_id": task_id},
        headers=auth_headers,
    )

    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == E_VALIDATION
    assert body["error"]["context"]["status"] == "completed"


@pytest.mark.asyncio
async def test_a_non_object_metadata_blob_is_refused_never_replaced(
    queue_db, queue_client, auth_headers, monkeypatch
):
    _configure_secret(monkeypatch, SECRET)
    task_id = _create_task_raw_metadata(queue_db, ["a", "raw", "blob"])

    resp = await queue_client.post(
        "/api/v1/approvals",
        json={"action_fingerprint": _refund_fingerprint(), "task_id": task_id},
        headers=auth_headers,
    )

    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == E_VALIDATION
    assert body["error"]["context"]["metadata_type"] == "list"
    row = queue_db.row(task_id)
    assert json.loads(row["metadata"]) == ["a", "raw", "blob"], "the blob was not replaced"
    assert row["status"] == "awaiting_approval"


@pytest.mark.asyncio
async def test_an_unconfigured_channel_refuses_before_any_task_lookup_or_write(
    queue_db, queue_client, auth_headers, monkeypatch
):
    """E_CONFIG stays the FIRST refusal (pre-T02b order): no token could ever verify, so the task
    is not even looked at — and certainly not written."""
    _configure_secret(monkeypatch, "")
    task_id = _create_task(
        queue_db, title="parked", status="awaiting_approval", metadata=_parked_metadata()
    )

    resp = await queue_client.post(
        "/api/v1/approvals",
        json={"action_fingerprint": _refund_fingerprint(), "task_id": task_id},
        headers=auth_headers,
    )

    assert resp.status_code == 503
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == E_CONFIG
    assert "APPROVAL_SECRET" in body["error"]["message"]
    row = queue_db.row(task_id)
    assert row["status"] == "awaiting_approval"
    assert "approval_token" not in (json.loads(row["metadata"]) if row["metadata"] else {})


@pytest.mark.asyncio
async def test_a_broker_outage_after_the_attach_is_a_named_refusal_and_the_row_says_so(
    queue_db, queue_client, auth_headers, monkeypatch
):
    """The attach committed; the hand-off did not.  Mirror of the manual trigger's contract: the
    row records the measured terminal state and the caller gets a named E_REDIS refusal."""
    import celery_app.tasks.agent_tasks as agent_tasks

    _configure_secret(monkeypatch, SECRET)

    def dead_delay(*_args, **_kwargs):
        raise ConnectionError("Error 61 connecting to localhost:6379. Connection refused.")

    monkeypatch.setattr(agent_tasks.run_agent_task, "delay", dead_delay, raising=False)
    task_id = _create_task(
        queue_db, title="parked, broker down", status="awaiting_approval", metadata=_parked_metadata()
    )

    resp = await queue_client.post(
        "/api/v1/approvals",
        json={"action_fingerprint": _refund_fingerprint(), "task_id": task_id},
        headers=auth_headers,
    )

    assert resp.status_code == 503
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == E_REDIS
    assert body["error"]["context"]["task_id"] == task_id
    assert body["error"]["how_to_fix"]
    row = queue_db.row(task_id)
    assert row["status"] == "failed", "nothing reconciles a row no worker was told about"
    assert "E_REDIS" in (row["error_message"] or "")
    stored = json.loads(row["metadata"])
    assert stored["approval_token"], "the attach itself succeeded and survives for audit"


def test_the_http_delivery_and_the_worker_reader_agree_on_the_metadata_key(queue_db):
    """No drift pin: the producer's key (task_service) is the string the worker's reader
    (`agent_tasks.approval_token_from_metadata`) consumes.  A rename on either side fails HERE."""
    from app.services.task_service import APPROVAL_TOKEN_METADATA_KEY, attach_approval_token
    from celery_app.tasks.agent_tasks import approval_token_from_metadata

    assert approval_token_from_metadata({APPROVAL_TOKEN_METADATA_KEY: "tok"}) == "tok"
    # and end to end through the service write
    task_id = _create_task(
        queue_db, title="key pin", status="awaiting_approval", metadata={"approval_required": []}
    )

    async def _attach() -> dict:
        async with queue_db.factory() as db:
            await attach_approval_token(db, task_id, "tok-123")
            await db.commit()
        return json.loads(queue_db.row(task_id)["metadata"])

    stored = queue_db.run(_attach())
    assert approval_token_from_metadata(stored) == "tok-123"
