"""W4 / DT-06 — the owner data wipe actually wipes: Chroma documents, every table, every store.

THE DEFECT (data-hunter DT-06, P1).  `DELETE /api/v1/data` deleted 8 of 24 tables, left the
memory rows' ChromaDB documents searchable (a bare `DELETE FROM memory_entries`), never touched the
attachment/site/run/export files, and its `kept` list named two of the eighteen stores that
survived.  The owner control's promise ("provably keeps the company config and the DeepSeek key
store") was false in every direction.

THE FIX under test:
  * memory rows are deleted through the SHIPPED fail-closed `memory_service.delete_memory` (Chroma
    document first, `E_STORAGE` refusal if it cannot be removed, then the row);
  * `MEMBER_MODELS` is EVERY shipped owner-data table (all 24; `company_config` is the only kept
    table), and `WIPE_ORDER` is a valid FK order;
  * the owner's file stores (attachments/sites/exports/run workspaces/stale export archives) are
    emptied — the directories survive, their owner content does not;
  * `kept` is the COMPLETE list of surviving data stores;
  * a storage failure anywhere is a NAMED refusal with the database transaction rolled back — never
    a 200 that hid a partial wipe.

RED legs: pre-fix, the seeded prospect/support/stripe/expense rows survive (`deleted` omits them),
the Chroma document still answers `search_memory`, and the attachment/site files are still there.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.api.v1 import data as data_module
from app.config import settings
from app.core.database import Base
from app.core.errors import E_STORAGE
from app.services import memory_service, site_package_service, site_service, task_attachment_service

SECRET = "w4-dt06-approval-secret"
KEY_SENTINEL = "sk-w4-dt06-sentinel-never-wiped"


class FakeCollection:
    """A tiny in-memory vector store with the exact call shapes `memory_service` uses."""

    def __init__(self) -> None:
        self.documents: dict[str, tuple[str, dict]] = {}
        self.fail_delete = False

    def add(self, *, documents, metadatas, ids) -> None:
        for document, metadata, document_id in zip(documents, metadatas, ids, strict=True):
            self.documents[str(document_id)] = (document, dict(metadata))

    def delete(self, *, ids) -> None:
        if self.fail_delete:
            raise RuntimeError("simulated ChromaDB outage (w4-dt06)")
        for document_id in ids:
            self.documents.pop(str(document_id), None)

    def query(self, *, query_texts, n_results, where=None):  # noqa: ANN001, ARG002 - chroma shape
        hits = list(self.documents.items())[:n_results]
        return {
            "documents": [[document for _id, (document, _meta) in hits]],
            "metadatas": [[meta for _id, (_document, meta) in hits]],
            "ids": [[document_id for document_id, _hit in hits]],
            "distances": [[0.1 for _ in hits]],
        }


@pytest.fixture(autouse=True)
def _configured_channel(monkeypatch):
    monkeypatch.setattr(settings, "approval_secret", SECRET)


@pytest.fixture(autouse=True)
def _isolated_stores(monkeypatch, tmp_path):
    """Point EVERY store the wipe touches at a scratch dir — never the repo's own data/ tree."""
    monkeypatch.setattr(data_module, "_CONSUMED_TOKENS_PATH", tmp_path / "consumed.json")
    monkeypatch.setattr(task_attachment_service, "resolve_root", lambda: tmp_path / "attachments")
    monkeypatch.setattr(site_service, "SITES_ROOT", tmp_path / "sites")
    monkeypatch.setattr(site_package_service, "EXPORTS_ROOT", tmp_path / "exports")
    monkeypatch.setattr(data_module.settings, "code_workspace_dir", str(tmp_path / "workspace"))
    monkeypatch.setattr(data_module, "_export_temp_dir", lambda: tmp_path / "scratch")
    monkeypatch.setenv("DEEPSEEK_KEY_FILE", str(tmp_path / "deepseek_api_key.json"))
    for directory in ("attachments/task-1", "sites/demo-site", "exports/site-acme", "workspace/runs/run-1", "scratch"):
        (tmp_path / directory).mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture
def fake_chroma(monkeypatch) -> FakeCollection:
    collection = FakeCollection()
    monkeypatch.setattr(memory_service, "get_collection", lambda: collection)
    return collection


async def _seed(db, tmp_path: Path) -> None:
    """One row in every previously-untouched table family + the file stores' contents."""
    from app.models.ads import AdCampaign, AdMetric
    from app.models.approval import ConsumedApproval
    from app.models.auto_mode import AutoModeSession
    from app.models.chat import ChatMessage
    from app.models.company import CompanyConfig
    from app.models.competitor import Competitor
    from app.models.finance import ExpenseRecord, RevenueSnapshot, StripeEvent
    from app.models.invoice import Invoice
    from app.models.outreach import EmailCampaign, EmailLog, Prospect
    from app.models.report import ActivityLog, DailyReport
    from app.models.schedule import TaskSchedule
    from app.models.social import SocialEngagement, SocialPost
    from app.models.support import SupportInboxItem
    from app.models.task import AgentRun, Task, TaskAttachment
    from app.models.vision import VisionDescriptionRow
    from app.services import secret_store

    task = Task(title="w4 dt06 task", agent_type="finance", source="manual", status="completed")
    db.add(task)
    await db.flush()
    db.add(AgentRun(task_id=task.id, agent_type="finance", status="completed"))
    db.add(ActivityLog(agent_type="finance", action="w4_seed", summary="seeded activity"))
    db.add(RevenueSnapshot(snapshot_date=date(2026, 10, 20), mrr_cents=100))
    db.add(
        Invoice(
            number="INV-W4-0001",
            issue_date=date(2026, 10, 20),
            due_date=date(2026, 11, 19),
            net_days=30,
            currency="USD",
            status="draft",
        )
    )
    db.add(TaskSchedule(name="w4 schedule", prompt="p", cadence="daily", hour=9))
    db.add(ChatMessage(role="owner", content="seed chat"))
    db.add(CompanyConfig(name="W4 Seed Co"))

    # ── the tables the pre-fix wipe left behind (the finding's own list) ──────────────────────
    prospect = Prospect(email="prospect@example.com", first_name="Prospect")
    campaign = EmailCampaign(name="outreach")
    db.add_all([prospect, campaign])
    await db.flush()
    db.add(EmailLog(prospect_id=prospect.id, campaign_id=campaign.id))
    db.add(
        StripeEvent(
            stripe_event_id="evt_w4_seed",
            event_type="charge.succeeded",
            amount_cents=100,
            currency="usd",
        )
    )
    db.add(
        ExpenseRecord(
            category="ads", vendor="Google", amount_cents=500, currency="usd", date=date(2026, 10, 20)
        )
    )
    db.add(
        SupportInboxItem(
            provider_message_id="w4-msg-1",
            from_address="customer@example.com",
            subject="customer mail that must be wiped",
            body="body",
            task_id=task.id,
        )
    )
    ad = AdCampaign(platform="meta", name="w4 campaign", total_spent_usd=Decimal("1.00"))
    db.add(ad)
    await db.flush()
    db.add(AdMetric(campaign_id=ad.id, date=date(2026, 10, 20), impressions=10))
    db.add(Competitor(name="Rival"))
    db.add(
        AutoModeSession(
            started_at=datetime.now(UTC) - timedelta(hours=1),
            ends_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    db.add(DailyReport(report_date=date(2026, 10, 20)))
    db.add(SocialPost(content="post"))
    db.add(SocialEngagement(author_handle="@someone", content="a mention"))

    # W2 (migration 0018): one SPENT approval token.  The wipe must KEEP this row — deleting spent
    # nonces would re-arm the token and turn the owner's data deletion into a replay window.
    db.add(
        ConsumedApproval(
            token_id="a" * 64,
            jti="0123456789abcdef",
            action_fingerprint="external_irreversible|render|deploy",
            consumed_by="w4-dt06-seed",
        )
    )

    # ── an attachment row + vision row + the blob on disk ───────────────────────────────────
    blob = tmp_path / "attachments" / "task-1" / "stored.png"
    blob.write_bytes(b"attachment-bytes")
    attachment = TaskAttachment(
        task_id=task.id,
        filename="shot.png",
        stored_name="stored.png",
        mime="image/png",
        size_bytes=blob.stat().st_size,
        sha256="a" * 64,
    )
    db.add(attachment)
    await db.flush()
    db.add(
        VisionDescriptionRow(
            attachment_id=attachment.id,
            task_id=task.id,
            sha256="a" * 64,
            description="what the screenshot showed",
        )
    )

    # ── the file stores' real content ───────────────────────────────────────────────────────
    (tmp_path / "sites" / "demo-site" / "index.html").write_text("<html>demo</html>", encoding="utf-8")
    (tmp_path / "exports" / "site-acme" / "index.html").write_text("<html>pkg</html>", encoding="utf-8")
    (tmp_path / "workspace" / "runs" / "run-1" / "notes.txt").write_text("run file", encoding="utf-8")
    (tmp_path / "scratch" / f"{data_module.EXPORT_TEMP_PREFIX}stale{data_module.EXPORT_TEMP_SUFFIX}").write_text(
        "a leaked export", encoding="utf-8"
    )
    # a look-alike that must NOT be removed (it is not this product's archive)
    (tmp_path / "scratch" / "someone-else.zip").write_text("not ours", encoding="utf-8")

    # ── a memory row indexed in the fake vector store ───────────────────────────────────────
    await memory_service.store_memory(
        db,
        category="strategy",
        title="w4 secret strategy",
        content="the wipe must make this unsearchable",
    )
    secret_store.set_deepseek_api_key(KEY_SENTINEL)  # sync file write, like `secret_store`'s API
    await db.commit()


async def _member_counts(db) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table, model in data_module.MEMBER_MODELS.items():
        result = await db.execute(select(func.count()).select_from(model.__table__))
        counts[table] = int(result.scalar_one())
    return counts


async def _mint(api_client, auth_headers) -> str:
    resp = await api_client.post(
        "/api/v1/approvals",
        json={"action_fingerprint": data_module.DATA_WIPE_ACTION_FINGERPRINT},
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["token"]


def _files(root: Path) -> list[str]:
    if not root.exists():
        return []
    return sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())


def test_every_orm_table_is_a_member_or_the_declared_kept_config():
    """DT-06's scope pin: the wipe set is the SCHEMA, minus the DECLARED kept tables.

    W2 DELIBERATE RE-PIN (2026-10-28; migration 0018): old assertion
    `orm_tables - members == {"company_config"}`.  `consumed_approvals` (the approval-token
    single-use ledger) is NOT a wipe member: it is security state that must outlive the wipe or a
    used approval token could be replayed after it.  It is declared in `KEPT`, so the wipe still
    reports every survivor; the equality below cannot hide a third, undeclared survivor.
    """
    orm_tables = set(Base.metadata.tables)
    members = set(data_module.MEMBER_MODELS)
    assert orm_tables - members == {"company_config", "consumed_approvals"}, (
        "every shipped table must be wiped or DECLARED kept; the survivors are the kept company "
        f"config and the kept approval ledger. missing from the declaration: "
        f"{sorted(orm_tables - members - {'company_config', 'consumed_approvals'})}"
    )
    assert members - orm_tables == set(), "the wipe names a table the schema does not have"
    assert data_module.KEPT == (
        "company_config",
        "consumed_approvals",
        "deepseek_key_store",
        "data_wipe_consumed_record",
    ), "kept must name every surviving data store (company config + approval ledger + the two files)"
    assert "invoice_dunning_claim_results" in members, "the DT-05 ledger is owner data too"


def test_the_wipe_order_is_a_valid_fk_order():
    """Every FK child is deleted before its parent, or PostgreSQL refuses the wipe."""
    position = {table: index for index, table in enumerate(data_module.WIPE_ORDER)}
    assert set(position) == set(data_module.MEMBER_MODELS)
    offenders: list[str] = []
    for table, model in data_module.MEMBER_MODELS.items():
        for foreign_key in model.__table__.foreign_keys:
            parent_table = foreign_key.column.table.name
            if parent_table not in position:
                continue  # kept parent (company_config) — nothing is deleted from it
            if position[table] > position[parent_table]:
                offenders.append(f"{table} (deleted after its parent {parent_table})")
    assert not offenders, f"WIPE_ORDER deletes children after parents: {offenders}"


@pytest.mark.asyncio
async def test_the_wipe_deletes_every_member_and_declares_the_exact_kept_set(
    api_client, auth_headers, async_db_session, fake_chroma, tmp_path
):
    from app.models.company import CompanyConfig
    from app.services import secret_store

    await _seed(async_db_session, tmp_path)
    token = await _mint(api_client, auth_headers)

    resp = await api_client.delete(f"/api/v1/data?confirm_token={token}", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["dry_run"] is False
    assert set(body["deleted"]) == set(data_module.MEMBER_MODELS), (
        "the wipe must account for every member table, not only the old 8"
    )
    assert body["kept"] == list(data_module.KEPT), (
        "kept must be the COMPLETE list of surviving data stores (no over-claim, no under-report)"
    )
    assert body["total_rows"] == sum(body["deleted"].values())

    assert await _member_counts(async_db_session) == {
        table: 0 for table in data_module.MEMBER_MODELS
    }, "a member table still holds rows after a 200 wipe"

    assert int(
        (await async_db_session.execute(select(func.count()).select_from(CompanyConfig))).scalar_one()
    ) == 1, "company_config must survive"
    from app.models.approval import ConsumedApproval

    assert int(
        (
            await async_db_session.execute(
                select(func.count()).select_from(ConsumedApproval)
            )
        ).scalar_one()
    ) == 1, (
        "the approval-token ledger must survive the wipe: deleting spent jtis would re-arm every "
        "token presented before it (a replay window)"
    )
    assert secret_store.get_deepseek_api_key() == KEY_SENTINEL, "the key store must survive"
    assert data_module._CONSUMED_TOKENS_PATH.exists(), (
        "the single-use marker must survive the wipe (or a used token could wipe again)"
    )


@pytest.mark.asyncio
async def test_a_wiped_memory_is_not_searchable(
    api_client, auth_headers, async_db_session, fake_chroma, tmp_path
):
    """DT-06's named test: the ChromaDB document goes with the row, not just the SQL row."""
    await _seed(async_db_session, tmp_path)
    assert fake_chroma.documents, "the seed must have indexed one document"
    before = await memory_service.search_memory(async_db_session, "secret strategy")
    assert before and "unsearchable" in before[0]["content"]

    token = await _mint(api_client, auth_headers)
    resp = await api_client.delete(f"/api/v1/data?confirm_token={token}", headers=auth_headers)
    assert resp.status_code == 200, resp.text

    assert fake_chroma.documents == {}, (
        "the ChromaDB document survived the wipe: a bare table DELETE leaves the memory searchable"
    )
    after = await memory_service.search_memory(async_db_session, "secret strategy")
    assert after == [], f"semantic search still answers a wiped memory: {after}"
    assert await memory_service.resolve_agent_memories(
        async_db_session, agent_type="finance", query="secret strategy"
    ) == [], "a wiped memory still reached the agent-prompt resolver"


@pytest.mark.asyncio
async def test_the_file_stores_are_emptied_and_their_directories_survive(
    api_client, auth_headers, async_db_session, fake_chroma, tmp_path
):
    await _seed(async_db_session, tmp_path)
    token = await _mint(api_client, auth_headers)

    resp = await api_client.delete(f"/api/v1/data?confirm_token={token}", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["deleted_files"] == {
        "attachments": 1,
        "sites": 1,
        "exports": 1,
        "run_workspaces": 1,
        "export_temp_archives": 1,
    }, body.get("deleted_files")

    for label, root in data_module._file_store_roots().items():
        assert root.is_dir(), f"the {label} store directory itself must survive (mount point)"
        assert _files(root) == [], f"the {label} store still carries owner files"

    scratch = tmp_path / "scratch"
    assert not list(scratch.glob(f"{data_module.EXPORT_TEMP_PREFIX}*")), (
        "a stale export archive is a full copy of the owner's tables; the wipe must remove it"
    )
    assert (scratch / "someone-else.zip").exists(), (
        "the wipe removes ONLY this product's export temp archives"
    )


@pytest.mark.asyncio
async def test_a_chroma_failure_is_a_named_refusal_and_deletes_no_rows(
    api_client, auth_headers, async_db_session, fake_chroma, tmp_path
):
    """Fail-closed: the shipped delete refuses `E_STORAGE`, and the wipe returns it BY NAME."""
    await _seed(async_db_session, tmp_path)
    before = await _member_counts(async_db_session)
    fake_chroma.fail_delete = True
    token = await _mint(api_client, auth_headers)

    resp = await api_client.delete(f"/api/v1/data?confirm_token={token}", headers=auth_headers)

    assert resp.status_code == 503, resp.text
    error = resp.json()["error"]
    assert error["code"] == E_STORAGE
    assert "ChromaDB" in error["message"] or "chroma" in error["message"].lower()
    await async_db_session.rollback()  # the request session's teardown (get_db rolls back)
    after = await _member_counts(async_db_session)
    assert after == before, "a storage refusal must not leave rows deleted (the transaction rolls back)"
    assert (tmp_path / "attachments" / "task-1" / "stored.png").exists(), (
        "the refusal happened before the file stores were touched"
    )


@pytest.mark.asyncio
async def test_a_file_store_failure_is_a_named_refusal_and_deletes_no_rows(
    api_client, auth_headers, async_db_session, fake_chroma, tmp_path, monkeypatch
):
    await _seed(async_db_session, tmp_path)
    before = await _member_counts(async_db_session)

    def _boom(path):  # noqa: ANN001 - shutil.rmtree shape
        raise OSError("simulated locked directory (w4-dt06)")

    monkeypatch.setattr(data_module.shutil, "rmtree", _boom)
    token = await _mint(api_client, auth_headers)
    resp = await api_client.delete(f"/api/v1/data?confirm_token={token}", headers=auth_headers)

    assert resp.status_code == 503, resp.text
    error = resp.json()["error"]
    assert error["code"] == E_STORAGE
    assert "could not remove" in error["message"]
    assert error["context"]["removed_before_failure"] >= 0
    await async_db_session.rollback()
    assert await _member_counts(async_db_session) == before, (
        "a file-store refusal must not leave the database half-wiped"
    )


@pytest.mark.asyncio
async def test_the_dry_run_counts_the_files_and_deletes_nothing(
    api_client, auth_headers, async_db_session, fake_chroma, tmp_path
):
    await _seed(async_db_session, tmp_path)
    before = await _member_counts(async_db_session)

    resp = await api_client.delete("/api/v1/data?dry_run=true", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["would_delete"] == before
    assert body["deleted"] is None
    assert body["would_delete_files"] == {
        "attachments": 1,
        "sites": 1,
        "exports": 1,
        "run_workspaces": 1,
        "export_temp_archives": 1,
    }
    assert body["kept"] == list(data_module.KEPT)
    assert (tmp_path / "attachments" / "task-1" / "stored.png").exists(), "a dry run deletes NOTHING"
    assert fake_chroma.documents, "a dry run deletes NO documents"
    assert await _member_counts(async_db_session) == before
