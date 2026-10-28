"""W4 / DT-07 — the approval mint is AUDITED and its token never leaves in the data export.

THE DEFECT (data-hunter DT-07, P2; export leg = A9-02).  `POST /api/v1/approvals` minted a live
bearer credential and wrote NO activity row — the only trace was the token itself in
`tasks.metadata` (never cleared).  Worse, `GET /api/v1/data/export` streamed every ORM column, so
`tasks.csv` shipped that live token verbatim while MANIFEST.json said secrets are not exported.

THE FIX under test:
  * one `approval_minted` activity row per successful mint and one `approval_attached` per attach,
    detail = fingerprint/expiry/ttl/task — NEVER the token;
  * every credential-bearing key (`EXPORT_REDACTED_KEYS`, recursively) is replaced by a visible
    marker in every export member, and MANIFEST.json DECLARES the redaction and the tables the
    export does not carry;
  * the token still WORKS internally: it is stored on the task, read by the worker's reader, and
    verifies against the firewall.

RED legs: pre-fix, the mint writes 0 activity rows and the seeded token sentinel appears verbatim
in `tasks.csv`.
"""
from __future__ import annotations

import csv
import io
import json
import zipfile

import pytest
from sqlalchemy import select

from app.agents.policy_firewall import verify_approval
from app.config import settings
from app.models.report import ActivityLog
from app.models.task import Task

SECRET = "w4-dt07-approval-secret"
TOKEN_SENTINEL = "w4-dt07-token-sentinel-must-not-be-exported"
FINGERPRINT = "w4-dt07-action-fingerprint"


@pytest.fixture(autouse=True)
def _configured_channel(monkeypatch):
    monkeypatch.setattr(settings, "approval_secret", SECRET)


@pytest.fixture(autouse=True)
def _record_dispatch(monkeypatch):
    """Record — never perform — the broker hand-off after an attach (no Redis in unit tests)."""
    import celery_app.tasks.agent_tasks as agent_tasks

    delivered: list[int] = []
    monkeypatch.setattr(
        agent_tasks.run_agent_task, "delay", lambda task_id: delivered.append(task_id),
        raising=False,
    )
    return delivered


async def _audit_rows(db, action: str) -> list[ActivityLog]:
    return list(
        (
            await db.execute(
                select(ActivityLog).where(ActivityLog.action == action).order_by(ActivityLog.id)
            )
        ).scalars()
    )


async def _seed_task(db, *, status: str = "awaiting_approval", metadata: dict | None = None) -> Task:
    task = Task(
        title="w4 dt07 task",
        agent_type="finance",
        source="manual",
        status=status,
        metadata_json=metadata if metadata is not None else {"approval_required": [{"fingerprint": FINGERPRINT}]},
    )
    db.add(task)
    await db.flush()
    return task


@pytest.mark.asyncio
async def test_a_plain_mint_writes_exactly_one_audit_row_without_the_token(
    api_client, auth_headers, async_db_session
):
    """One mint = one audit row; the detail names the fingerprint and NEVER the credential."""
    resp = await api_client.post(
        "/api/v1/approvals", json={"action_fingerprint": FINGERPRINT}, headers=auth_headers
    )
    assert resp.status_code == 201, resp.text
    token = resp.json()["token"]

    (minted,) = await _audit_rows(async_db_session, "approval_minted")
    assert minted.agent_type == "orchestrator"
    detail = minted.detail
    assert isinstance(detail, dict)
    assert detail["action_fingerprint"] == FINGERPRINT
    assert detail["task_id"] is None
    assert isinstance(detail["expires_at"], int) and detail["expires_at"] > 0
    assert token not in json.dumps(detail), "the audit row recorded the TOKEN itself (a credential leak)"
    assert token not in (minted.summary or "")
    assert await _audit_rows(async_db_session, "approval_attached") == []


@pytest.mark.asyncio
async def test_an_attach_writes_the_second_audit_row_and_the_token_still_works(
    api_client, auth_headers, async_db_session
):
    """The attach is audited; the stored token is the SAME credential and still verifies."""
    task = await _seed_task(async_db_session)

    resp = await api_client.post(
        "/api/v1/approvals",
        json={"action_fingerprint": FINGERPRINT, "task_id": int(task.id)},
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    token = resp.json()["token"]
    task_id = int(task.id)

    (minted,) = await _audit_rows(async_db_session, "approval_minted")
    assert minted.detail["task_id"] == int(task.id)
    (attached,) = await _audit_rows(async_db_session, "approval_attached")
    assert attached.detail == {
        "task_id": int(task.id),
        "action_fingerprint": FINGERPRINT,
    }, "the attach row must be a precise audit fact, and must not carry the token"
    assert token not in json.dumps(attached.detail)

    async_db_session.expire_all()
    row = (
        await async_db_session.execute(select(Task).where(Task.id == task_id))
    ).scalar_one()
    assert row.status == "pending", "the token delivery must re-queue the parked row"
    assert row.metadata_json["approval_token"] == token, "the token must still be delivered internally"
    assert verify_approval(
        SECRET.encode(), token, action_fingerprint=FINGERPRINT
    ), "the minted token must still verify against the firewall (nothing was weakened)"


@pytest.mark.asyncio
async def test_the_export_replaces_the_token_with_a_declared_marker(
    api_client, auth_headers, async_db_session
):
    """DT-07's named test: a seeded token sentinel appears in NO export member byte."""
    await _seed_task(
        async_db_session,
        status="pending",
        metadata={
            "approval_token": TOKEN_SENTINEL,
            "approved_payloads": {"fp": {"body": {"approval_token": TOKEN_SENTINEL}}},
            "note": "this must survive",
        },
    )

    resp = await api_client.get("/api/v1/data/export", headers=auth_headers)
    assert resp.status_code == 200, resp.text

    archive = zipfile.ZipFile(io.BytesIO(resp.content))
    for name in archive.namelist():
        assert TOKEN_SENTINEL.encode() not in archive.read(name), (
            f"the live approval token was exported verbatim in {name}"
        )
    manifest = json.loads(archive.read("MANIFEST.json"))
    assert manifest["redacted_fields"], "the manifest must DECLARE the credential redaction"
    assert any("approval_token" in line for line in manifest["redacted_fields"])
    assert "company_config" not in manifest["tables_not_included"]

    rows = list(csv.reader(io.StringIO(archive.read("tasks.csv").decode("utf-8"))))
    header = rows[0]
    metadata_column = header.index("metadata")
    stored = json.loads(rows[1][metadata_column])
    assert stored["approval_token"] == "[redacted: credential]"
    assert stored["approved_payloads"]["fp"]["body"]["approval_token"] == "[redacted: credential]", (
        "the recursive scrub must also cover a nested approved payload"
    )
    assert stored["note"] == "this must survive", "redaction must not drop the rest of the metadata"


@pytest.mark.asyncio
async def test_the_manifest_declares_the_tables_the_export_does_not_carry(
    api_client, auth_headers, async_db_session
):
    """DT-06/DT-07: the archive is 8 members, the wipe covers 24 tables — say so."""
    from app.api.v1 import data as data_module

    resp = await api_client.get("/api/v1/data/export", headers=auth_headers)
    manifest = json.loads(
        zipfile.ZipFile(io.BytesIO(resp.content)).read("MANIFEST.json")
    )

    declared = set(manifest["tables_not_included"])
    assert declared == set(data_module.MEMBER_MODELS) - set(data_module.EXPORT_MODELS)
    assert "prospects" in declared and "support_inbox_items" in declared
    assert manifest["members"] and set(manifest["members"][0]) >= {"name", "table", "format", "row_count", "columns"}


@pytest.mark.asyncio
async def test_a_refused_attach_leaves_no_minted_audit_row(api_client, auth_headers, async_db_session):
    """A refused attach means the caller never received a usable token: the mint audit rolls back.

    The route writes the audit row inside the request transaction and `get_db` rolls that
    transaction back on an exception (the shipped dependency contract); the api_client override
    shares the session, so the test performs that teardown itself.
    """
    task = await _seed_task(async_db_session, status="pending")  # not parked -> E_VALIDATION
    task_id = int(task.id)
    await async_db_session.commit()

    resp = await api_client.post(
        "/api/v1/approvals",
        json={"action_fingerprint": FINGERPRINT, "task_id": task_id},
        headers=auth_headers,
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "E_VALIDATION"

    await async_db_session.rollback()  # get_db's teardown for a refused request
    assert await _audit_rows(async_db_session, "approval_minted") == []
    assert await _audit_rows(async_db_session, "approval_attached") == []
    assert (
        await async_db_session.execute(select(Task).where(Task.id == task_id))
    ).scalar_one().status == "pending", "the refused attach must leave the task byte-identical"
