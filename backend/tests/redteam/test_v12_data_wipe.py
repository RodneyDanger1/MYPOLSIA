"""V12 red-team — `DELETE /api/v1/data` (the guarded wipe) under attack.

Surface: `app/api/v1/data.py`.  Seams are the same the unit suite uses (house style): the
consumed-token marker path is monkeypatched per test, `APPROVAL_SECRET` is the shipped
approvals channel, and tokens are minted ONLY through `POST /api/v1/approvals` (never
hand-rolled — a hand-rolled token proves nothing about the shipped mint).

Attack list:
  1. no token at all (wanting to delete without approval)
  2. empty / malformed confirm_token values
  3. tampered signature, expired token — named refusals, nothing deleted
  4. REPLAYED single-use token, including after the tables were re-seeded
  5. dry-run attempting a REAL deletion (and: does a dry run burn the presented token?)
  6. the wipe channel unconfigured (no APPROVAL_SECRET): fail closed
  7. unauthenticated wipe (the router-level key)
"""
from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import func, select

from app.agents.policy_firewall import configured_approval_secret, issue_approval
from app.api.v1 import data as data_module
from app.config import settings

SECRET = "redteam-v12-wipe-secret"
WIPE = "/api/v1/data"


@pytest.fixture(autouse=True)
def _configured_channel(monkeypatch: Any):
    monkeypatch.setattr(settings, "approval_secret", SECRET)


@pytest.fixture(autouse=True)
def _isolated_marker(monkeypatch: Any, tmp_path: Any):
    """The single-use marker is runtime state beside the key store; per-test file, never the real one."""
    monkeypatch.setattr(data_module, "_CONSUMED_TOKENS_PATH", tmp_path / "data_wipe_consumed.json")


async def _seed_members(db) -> dict[str, int]:
    """One row in every member table (the same minimal seed the V10 unit suite uses)."""
    from datetime import date

    from app.models.chat import ChatMessage
    from app.models.finance import RevenueSnapshot
    from app.models.invoice import Invoice
    from app.models.memory import MemoryEntry
    from app.models.report import ActivityLog
    from app.models.schedule import TaskSchedule
    from app.models.task import AgentRun, Task

    task = Task(title="V12 redteam seed", agent_type="finance", source="manual", status="completed")
    db.add(task)
    await db.flush()
    db.add(AgentRun(task_id=task.id, agent_type="finance", status="completed"))
    db.add(ActivityLog(agent_type="finance", action="seed", summary="redteam seed"))
    db.add(RevenueSnapshot(snapshot_date=date(2026, 10, 14)))
    db.add(Invoice(number="INV-V12-0001", issue_date=date(2026, 10, 14),
                   due_date=date(2026, 11, 13), net_days=30, currency="USD", status="draft"))
    db.add(TaskSchedule(name="seed schedule", prompt="seed prompt", cadence="daily", hour=9))
    db.add(MemoryEntry(category="strategy", title="seed memory", content="seeded"))
    db.add(ChatMessage(role="owner", content="seed chat"))
    await db.commit()
    return {
        "tasks": 1, "agent_runs": 1, "activity_log": 1, "revenue_snapshots": 1,
        "invoices": 1, "task_schedules": 1, "memory_entries": 1, "chat_messages": 1,
    }


async def _counts(db) -> dict[str, int]:
    counts = {}
    for name, model in data_module.MEMBER_MODELS.items():
        counts[name] = int(
            (await db.execute(select(func.count()).select_from(model.__table__))).scalar_one()
        )
    return counts


async def _mint(api_client, auth_headers) -> str:
    resp = await api_client.post(
        "/api/v1/approvals",
        json={"action_fingerprint": data_module.DATA_WIPE_ACTION_FINGERPRINT},
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["token"]


# ── 1 + 2. no token / empty / malformed ──────────────────────────────────────────────────────


async def test_no_token_cannot_delete_anything(api_client, auth_headers, async_db_session):
    """ATTACK: call DELETE with no confirm_token — every member must survive."""
    seeded = await _seed_members(async_db_session)
    resp = await api_client.delete(WIPE, headers=auth_headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "E_VALIDATION"
    assert await _counts(async_db_session) == seeded


async def test_empty_and_malformed_tokens_cannot_delete_anything(api_client, auth_headers, async_db_session):
    """ATTACK: `?confirm_token=` empty, a bare word, a three-part token, a non-integer expiry."""
    seeded = await _seed_members(async_db_session)
    for bad in ("", "not-a-polsia-token", "123.abc.def", "notint.deadbeef"):
        resp = await api_client.delete(f"{WIPE}?confirm_token={bad}", headers=auth_headers)
        assert resp.status_code == 422, bad
        assert resp.json()["error"]["code"] == "E_VALIDATION", bad
    assert await _counts(async_db_session) == seeded


async def test_tampered_and_expired_tokens_are_named_refusals(api_client, auth_headers, async_db_session):
    """ATTACK: flip one signature byte; or present a token minted with a negative TTL."""
    seeded = await _seed_members(async_db_session)
    good = await _mint(api_client, auth_headers)
    exp_part, sig_part = good.split(".", 1)
    flipped = "0" if sig_part[0] != "0" else "1"
    tampered = f"{exp_part}.{flipped}{sig_part[1:]}"

    resp = await api_client.delete(f"{WIPE}?confirm_token={tampered}", headers=auth_headers)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "E_POLICY_DENIED"
    assert await _counts(async_db_session) == seeded

    expired = issue_approval(configured_approval_secret(),
                             action_fingerprint=data_module.DATA_WIPE_ACTION_FINGERPRINT, ttl_s=-5)
    resp = await api_client.delete(f"{WIPE}?confirm_token={expired.token}", headers=auth_headers)
    assert resp.status_code == 403
    assert "expired" in str(resp.json()["error"]["context"]).lower()
    assert await _counts(async_db_session) == seeded


# ── 4. replay ────────────────────────────────────────────────────────────────────────────────


async def test_a_replayed_token_cannot_wipe_reseeded_data(api_client, auth_headers, async_db_session):
    """ATTACK: after a successful wipe, re-seed the tables and present the SAME token again.

    Single-use is the marker's property, not the empty state's: the replay must be refused
    (already_used, 403) and the re-seeded rows must survive untouched.
    """
    seeded = await _seed_members(async_db_session)
    token = await _mint(api_client, auth_headers)

    first = await api_client.delete(f"{WIPE}?confirm_token={token}", headers=auth_headers)
    assert first.status_code == 200, first.text
    assert first.json()["deleted"] == seeded

    reseeded = await _seed_members(async_db_session)
    replay = await api_client.delete(f"{WIPE}?confirm_token={token}", headers=auth_headers)
    assert replay.status_code == 403
    body = replay.json()
    assert body["error"]["code"] == "E_POLICY_DENIED"
    assert body["error"]["context"]["reason"] == "already_used"
    assert await _counts(async_db_session) == reseeded, "a replayed token must delete NOTHING"


# ── 5. dry-run vs real deletion ──────────────────────────────────────────────────────────────


async def test_dry_run_never_deletes_even_with_a_valid_token_and_does_not_burn_it(
    api_client, auth_headers, async_db_session,
):
    """ATTACK: use dry-run as a deletion attempt, and use it to burn an approval.

    `?dry_run=true&confirm_token=<real token>` must answer would-delete counts and delete
    NOTHING; the presented token must remain UNCONSUMED, proven by performing the real wipe
    with the same token afterwards.
    """
    seeded = await _seed_members(async_db_session)
    token = await _mint(api_client, auth_headers)

    dry = await api_client.delete(f"{WIPE}?dry_run=true&confirm_token={token}", headers=auth_headers)
    assert dry.status_code == 200, dry.text
    body = dry.json()
    assert body["dry_run"] is True
    assert body["would_delete"] == seeded
    assert body["deleted"] is None
    assert await _counts(async_db_session) == seeded, "dry run deleted rows"

    real = await api_client.delete(f"{WIPE}?confirm_token={token}", headers=auth_headers)
    assert real.status_code == 200, real.text
    assert real.json()["deleted"] == seeded, "the dry run consumed the token (fail: re-mint needed)"
    assert all(count == 0 for count in (await _counts(async_db_session)).values())


# ── 6 + 7. unconfigured channel / unauthenticated ────────────────────────────────────────────


async def test_unconfigured_wipe_channel_fails_closed(api_client, auth_headers, async_db_session, monkeypatch):
    """ATTACK: present any token on an install with APPROVAL_SECRET unset — 503, nothing deleted."""
    seeded = await _seed_members(async_db_session)
    monkeypatch.setattr(settings, "approval_secret", "")
    resp = await api_client.delete(f"{WIPE}?confirm_token=1.deadbeef", headers=auth_headers)
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "E_CONFIG"
    assert await _counts(async_db_session) == seeded


async def test_unauthenticated_wipe_is_401(api_client, async_db_session):
    """ATTACK: delete the owner's data with no API key at all."""
    seeded = await _seed_members(async_db_session)
    resp = await api_client.delete(f"{WIPE}?dry_run=true")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "E_AUTH"
    assert await _counts(async_db_session) == seeded
