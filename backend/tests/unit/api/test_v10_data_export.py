"""V10 / seat h1 (D7) — the data portability surface: GET /api/v1/data/export + DELETE /api/v1/data.

RED-FIRST leg (OATH): this file is written BEFORE `app/api/v1/data.py` exists and before
`app/main.py` wires its router; collection fails with ModuleNotFoundError until they land.  Raw
pytest legs live in `polsia-work/build/ui-reverse/wave-v10/h1-docs-ops/RECEIPT.md`.

The legs this pins (the brief's list, plus the two honesty legs the brief states as absolutes):
  * export ZIP member set + row counts match the seeded rows (MANIFEST included);
  * the export NEVER carries the DeepSeek key store or .env values (asserted, not assumed);
  * the dry run answers WOULD-delete counts and deletes NOTHING (no token needed for it);
  * DELETE without / malformed / tampered / expired token -> refused BY NAME, nothing deleted;
  * a token minted through the SHIPPED approvals machinery (POST /api/v1/approvals, same
    APPROVAL_SECRET) wipes the member tables in one pass; `company_config` survives and the
    DeepSeek key store is untouched — while a kept `support_inbox_items` row that POINTED at a
    wiped task keeps its content and loses only the dangling reference;
  * a consumed token stays refused even after the members are RE-SEEDED (single-use is the
    marker's property, not the empty state's).
"""
from __future__ import annotations

import csv
import io
import json
import zipfile
from datetime import date

import pytest
from sqlalchemy import func, select

from app.agents.policy_firewall import configured_approval_secret, issue_approval
from app.api.v1 import data as data_module
from app.config import settings
from app.services import secret_store

SECRET = "v10-unit-approval-secret"

#: The key-store sentinel: it must NEVER appear in any export member (the brief's absolute rule).
KEY_SENTINEL = "sk-v10-sentinel-must-never-be-exported"

#: Member file names, in the frozen D2 split (CSV where tabular, JSON otherwise).
CSV_MEMBERS = (
    "tasks.csv",
    "agent_runs.csv",
    "activity_log.csv",
    "revenue_snapshots.csv",
    "invoices.csv",
    "task_schedules.csv",
)
JSON_MEMBERS = ("memory_entries.json", "chat_messages.json")
ALL_MEMBERS = CSV_MEMBERS + JSON_MEMBERS


@pytest.fixture(autouse=True)
def _configured_channel(monkeypatch):
    """A configured approvals channel — the same seam test_v8_chat.py uses."""
    monkeypatch.setattr(settings, "approval_secret", SECRET)


@pytest.fixture(autouse=True)
def _isolated_token_marker(monkeypatch, tmp_path):
    """The consumed-token marker is runtime state; every test gets its own file."""
    monkeypatch.setattr(
        data_module, "_CONSUMED_TOKENS_PATH", tmp_path / "data_wipe_consumed.json"
    )


@pytest.fixture(autouse=True)
def _isolated_key_store(monkeypatch, tmp_path):
    """The DeepSeek key store resolves to a per-test file (never the owner's real one)."""
    monkeypatch.setenv("DEEPSEEK_KEY_FILE", str(tmp_path / "deepseek_api_key.json"))


async def _seed_members(db) -> dict[str, int]:
    """One row in every member table (+ one KEPT support row pointing at the seeded task).

    Returns the expected per-member counts (the export/wipe assertions compare against these).
    """
    from app.models.chat import ChatMessage
    from app.models.company import CompanyConfig
    from app.models.finance import RevenueSnapshot
    from app.models.invoice import Invoice
    from app.models.memory import MemoryEntry
    from app.models.report import ActivityLog
    from app.models.schedule import TaskSchedule
    from app.models.support import SupportInboxItem
    from app.models.task import AgentRun, Task

    task = Task(title="V10 seed task", agent_type="finance", source="manual", status="completed")
    db.add(task)
    await db.flush()
    db.add(AgentRun(task_id=task.id, agent_type="finance", status="completed"))
    db.add(ActivityLog(agent_type="finance", action="seed", summary="seeded activity"))
    db.add(RevenueSnapshot(snapshot_date=date(2026, 10, 14)))
    db.add(
        Invoice(
            number="INV-V10-0001",
            issue_date=date(2026, 10, 14),
            due_date=date(2026, 11, 13),
            net_days=30,
            currency="USD",
            status="draft",
        )
    )
    db.add(TaskSchedule(name="seed schedule", prompt="seed prompt", cadence="daily", hour=9))
    db.add(MemoryEntry(category="strategy", title="seed memory", content="seeded memory"))
    db.add(ChatMessage(role="owner", content="seed chat"))
    db.add(CompanyConfig(name="V10 Seed Co"))
    db.add(
        SupportInboxItem(
            provider_message_id="v10-seed-1",
            from_address="customer@example.com",
            subject="kept subject",
            body="kept body",
            task_id=task.id,
        )
    )
    await db.flush()
    return {
        "tasks": 1, "agent_runs": 1, "activity_log": 1, "revenue_snapshots": 1,
        "invoices": 1, "task_schedules": 1, "memory_entries": 1, "chat_messages": 1,
    }


async def _member_counts(db) -> dict[str, int]:
    """Live row counts of the 8 member tables, by table name.

    No rollback: the route and the test share one session, and a rollback here would discard
    freshly-flushed seed rows the test is about to assert on.
    """
    counts: dict[str, int] = {}
    for table_name, model in data_module.MEMBER_MODELS.items():
        result = await db.execute(select(func.count()).select_from(model.__table__))
        counts[table_name] = int(result.scalar_one())
    return counts


async def _mint_wipe_token(api_client, auth_headers) -> str:
    """Mint through the SHIPPED approvals route — never a hand-rolled token."""
    resp = await api_client.post(
        "/api/v1/approvals",
        json={"action_fingerprint": data_module.DATA_WIPE_ACTION_FINGERPRINT},
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["token"]


# ── auth ─────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_export_requires_the_owner_key(api_client):
    resp = await api_client.get("/api/v1/data/export")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "E_AUTH"


@pytest.mark.asyncio
async def test_delete_requires_the_owner_key(api_client):
    resp = await api_client.delete("/api/v1/data")
    assert resp.status_code == 401


# ── the export ───────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_export_zip_members_and_counts_match_the_seeded_rows(
    api_client, auth_headers, async_db_session
):
    seeded = await _seed_members(async_db_session)
    resp = await api_client.get("/api/v1/data/export", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/zip"
    assert resp.headers["X-Polsia-Row-Count"] == str(sum(seeded.values()))

    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    assert set(zf.namelist()) == {"MANIFEST.json", *ALL_MEMBERS}

    manifest = json.loads(zf.read("MANIFEST.json"))
    by_name = {member["name"]: member for member in manifest["members"]}
    assert set(by_name) == set(ALL_MEMBERS)
    for table, count in seeded.items():
        for name in (f"{table}.csv", f"{table}.json"):
            if name in by_name:
                assert by_name[name]["row_count"] == count
                assert by_name[name]["table"] == table

    for name in CSV_MEMBERS:
        rows = list(csv.reader(io.StringIO(zf.read(name).decode("utf-8"))))
        assert len(rows) == seeded[name[:-4]] + 1, name  # header + one row per member row
        assert rows[0] == by_name[name]["columns"], name
        assert rows[0][0] == "id"

    for name in JSON_MEMBERS:
        items = json.loads(zf.read(name))
        assert isinstance(items, list) and len(items) == seeded[name[:-5]], name

    chat = json.loads(zf.read("chat_messages.json"))
    assert chat[0]["content"] == "seed chat"


@pytest.mark.asyncio
async def test_export_never_carries_the_key_store(api_client, auth_headers, async_db_session):
    await _seed_members(async_db_session)
    secret_store.set_deepseek_api_key(KEY_SENTINEL)

    resp = await api_client.get("/api/v1/data/export", headers=auth_headers)
    assert resp.status_code == 200

    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    assert "deepseek_api_key.json" not in zf.namelist()
    for name in zf.namelist():
        assert KEY_SENTINEL.encode() not in zf.read(name), name
    # The store itself is untouched by a read-only export.
    assert secret_store.get_deepseek_api_key() == KEY_SENTINEL


# ── the dry run ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dry_run_reports_would_delete_counts_and_deletes_nothing(
    api_client, auth_headers, async_db_session
):
    seeded = await _seed_members(async_db_session)
    before = await _member_counts(async_db_session)

    resp = await api_client.delete("/api/v1/data?dry_run=true", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dry_run"] is True
    assert body["would_delete"] == seeded
    assert body["total_rows"] == sum(seeded.values())
    assert body["action_fingerprint"] == data_module.DATA_WIPE_ACTION_FINGERPRINT
    assert body["deleted"] is None

    after = await _member_counts(async_db_session)
    assert after == before == seeded


@pytest.mark.asyncio
async def test_dry_run_with_a_token_still_never_deletes(
    api_client, auth_headers, async_db_session
):
    seeded = await _seed_members(async_db_session)
    token = await _mint_wipe_token(api_client, auth_headers)

    resp = await api_client.delete(
        f"/api/v1/data?dry_run=true&confirm_token={token}", headers=auth_headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["would_delete"] == seeded
    assert await _member_counts(async_db_session) == seeded


# ── the refusals ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_without_a_token_is_refused_by_name(
    api_client, auth_headers, async_db_session
):
    seeded = await _seed_members(async_db_session)

    resp = await api_client.delete("/api/v1/data", headers=auth_headers)
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "E_VALIDATION"
    assert "confirm token" in error["message"]
    assert "dry_run" in error["how_to_fix"]
    assert await _member_counts(async_db_session) == seeded


@pytest.mark.asyncio
async def test_malformed_token_is_refused_by_name(api_client, auth_headers, async_db_session):
    seeded = await _seed_members(async_db_session)

    resp = await api_client.delete(
        "/api/v1/data?confirm_token=not-a-polsia-token", headers=auth_headers
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "E_VALIDATION"
    assert await _member_counts(async_db_session) == seeded


@pytest.mark.asyncio
async def test_tampered_token_is_refused_by_name(api_client, auth_headers, async_db_session):
    seeded = await _seed_members(async_db_session)
    token = await _mint_wipe_token(api_client, auth_headers)
    exp_part, sig_part = token.split(".", 1)
    flipped = "0" if sig_part[0] != "0" else "1"
    tampered = f"{exp_part}.{flipped}{sig_part[1:]}"

    resp = await api_client.delete(
        f"/api/v1/data?confirm_token={tampered}", headers=auth_headers
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "E_POLICY_DENIED"
    assert await _member_counts(async_db_session) == seeded


@pytest.mark.asyncio
async def test_expired_token_is_refused_by_name(api_client, auth_headers, async_db_session):
    seeded = await _seed_members(async_db_session)
    expired = issue_approval(
        configured_approval_secret(),
        action_fingerprint=data_module.DATA_WIPE_ACTION_FINGERPRINT,
        ttl_s=-5,
    )

    resp = await api_client.delete(
        f"/api/v1/data?confirm_token={expired.token}", headers=auth_headers
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "E_POLICY_DENIED"
    assert "expired" in json.dumps(resp.json()["error"]["context"])
    assert await _member_counts(async_db_session) == seeded


# ── the wipe ─────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_minted_token_wipes_members_and_keeps_config_and_key_store(
    api_client, auth_headers, async_db_session
):
    from app.models.company import CompanyConfig

    seeded = await _seed_members(async_db_session)
    secret_store.set_deepseek_api_key(KEY_SENTINEL)
    token = await _mint_wipe_token(api_client, auth_headers)

    resp = await api_client.delete(
        f"/api/v1/data?confirm_token={token}", headers=auth_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dry_run"] is False
    assert body["deleted"] == seeded
    assert body["total_rows"] == sum(seeded.values())
    assert body["kept"] == ["company_config", "deepseek_key_store"]

    assert await _member_counts(async_db_session) == {table: 0 for table in seeded}

    company = await async_db_session.execute(select(func.count()).select_from(CompanyConfig))
    assert int(company.scalar_one()) == 1

    # The key store is a FILE, kept by design.
    assert secret_store.get_deepseek_api_key() == KEY_SENTINEL

    # The kept support row lost only its now-dangling task reference.
    from app.models.support import SupportInboxItem

    support = (await async_db_session.execute(select(SupportInboxItem))).scalar_one()
    assert support.subject == "kept subject" and support.task_id is None


@pytest.mark.asyncio
async def test_reused_token_is_refused_even_after_members_are_reseeded(
    api_client, auth_headers, async_db_session
):
    await _seed_members(async_db_session)
    token = await _mint_wipe_token(api_client, auth_headers)

    first = await api_client.delete(
        f"/api/v1/data?confirm_token={token}", headers=auth_headers
    )
    assert first.status_code == 200, first.text

    # Re-seed AFTER the wipe: the state is non-empty again, so an empty-state guard alone
    # would let the same token wipe twice — the consumed-token marker is what must refuse.
    from app.models.chat import ChatMessage
    from app.models.task import Task

    async_db_session.add(Task(title="post-wipe task", agent_type="finance"))
    async_db_session.add(ChatMessage(role="owner", content="post-wipe chat"))
    await async_db_session.flush()

    second = await api_client.delete(
        f"/api/v1/data?confirm_token={token}", headers=auth_headers
    )
    assert second.status_code == 403, second.text
    error = second.json()["error"]
    assert error["code"] == "E_POLICY_DENIED"
    assert "already" in " ".join(str(value) for value in error.values()).lower()

    # Nothing was deleted by the refused reuse: the re-seeded rows survive.
    counts = await _member_counts(async_db_session)
    assert counts["tasks"] == 1 and counts["chat_messages"] == 1
