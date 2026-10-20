"""V28/L5 — FK cascades on the migrated schema (SQLite, enforcement ON) + the app-engine gap.

The two cascades the V25/V27 surfaces depend on are declared in migration 0013/0016 and in the
ORM.  This file proves them at the ENGINE level on a database built by the real chain (not
`create_all`), including the sibling leg nobody tests: deleting ONE attachment must take only that
attachment's descriptions, never the task's other images' history.

It also pins the other half of the law — the parents that deliberately do NOT cascade:

* `agent_runs.task_id` and `support_inbox_items.task_id` have no `ON DELETE`, so a task with
  ledger/CS rows cannot be deleted by accident (the ledger is evidence, not a child blob).
* `email_logs.prospect_id` is NOT NULL with no `ON DELETE`, so a prospect with send history is
  likewise protected.  (`users` does not exist in this schema; `prospects` is the owner/CRM parent
  that actually has children, which is why it is covered here.)

D-3 (reported, xfail-strict): `app/core/database.py::get_engine()` never enables
`PRAGMA foreign_keys` on SQLite, so on the README's documented non-Docker path these cascades do
not fire at all.  The migration-level truth below is what PostgreSQL gets and what SQLite gets in
any process that turns the pragma on.
"""
from __future__ import annotations

import pathlib
import sqlite3

import pytest

from alembic import command
from alembic import config as alembic_config

BACKEND = pathlib.Path(__file__).resolve().parents[2]
SHA = "a" * 64


@pytest.fixture
def migrated_db(monkeypatch, tmp_path):
    """A fresh SQLite file migrated from ZERO to head, returned with FK enforcement available."""
    from app.config import settings

    db_path = tmp_path / "v28_cascades.sqlite"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    monkeypatch.setattr(settings, "database_url", url)
    cfg = alembic_config.Config()
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    return db_path


@pytest.fixture
def con(migrated_db):
    """A raw connection with FK enforcement ON — the PostgreSQL-equivalent behaviour."""
    connection = sqlite3.connect(migrated_db.as_posix())
    connection.execute("PRAGMA foreign_keys=ON")
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    try:
        yield connection
    finally:
        connection.close()


def _task(con: sqlite3.Connection, task_id: int, title: str = "a task") -> None:
    con.execute(
        "INSERT INTO tasks (id, title, agent_type, status) VALUES (?, ?, 'finance', 'pending')",
        (task_id, title),
    )


def _attachment(con: sqlite3.Connection, attachment_id: int, task_id: int) -> None:
    con.execute(
        "INSERT INTO task_attachments (id, task_id, filename, stored_name, mime, size_bytes, sha256) "
        "VALUES (?, ?, 'a.png', ?, 'image/png', 3, ?)",
        (attachment_id, task_id, f"{attachment_id:032d}.png", SHA),
    )


def _description(con: sqlite3.Connection, description_id: int, attachment_id: int, task_id: int) -> None:
    con.execute(
        "INSERT INTO vision_descriptions (id, attachment_id, task_id, sha256, description, model, "
        "tokens) VALUES (?, ?, ?, ?, 'a red square', 'deepseek-flash', 3)",
        (description_id, attachment_id, task_id, SHA),
    )


def _ids(con: sqlite3.Connection, table: str) -> list[int]:
    return [row[0] for row in con.execute(f"SELECT id FROM {table} ORDER BY id")]


def test_deleting_a_task_cascades_its_attachments_and_their_descriptions(con) -> None:
    """task → attachments → vision descriptions: one DELETE removes the whole subtree."""
    _task(con, 1)
    _task(con, 2)
    _attachment(con, 1, task_id=1)
    _attachment(con, 2, task_id=1)
    _attachment(con, 3, task_id=2)
    _description(con, 1, attachment_id=1, task_id=1)
    _description(con, 2, attachment_id=2, task_id=1)
    _description(con, 3, attachment_id=3, task_id=2)
    con.commit()

    con.execute("DELETE FROM tasks WHERE id = 1")
    con.commit()

    assert _ids(con, "task_attachments") == [3], "task 1's attachments must be gone"
    assert _ids(con, "vision_descriptions") == [3], (
        "task 1's descriptions must be gone; task 2's must survive"
    )
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []


def test_deleting_one_attachment_cascades_only_its_own_descriptions(con) -> None:
    """The sibling leg: a task's OTHER images keep their history when one image is removed."""
    _task(con, 1)
    _attachment(con, 1, task_id=1)
    _attachment(con, 2, task_id=1)
    _description(con, 1, attachment_id=1, task_id=1)
    _description(con, 2, attachment_id=2, task_id=1)
    con.commit()

    con.execute("DELETE FROM task_attachments WHERE id = 1")
    con.commit()

    assert _ids(con, "task_attachments") == [2]
    assert _ids(con, "vision_descriptions") == [2], (
        "deleting attachment 1 must not delete attachment 2's description"
    )


def test_a_task_with_an_agent_run_cannot_be_deleted(con) -> None:
    """`agent_runs.task_id` has no ON DELETE: the ledger is evidence, never a cascaded child."""
    _task(con, 1)
    con.execute(
        "INSERT INTO agent_runs (id, task_id, agent_type, status) VALUES (1, 1, 'finance', 'completed')"
    )
    con.commit()

    with pytest.raises(sqlite3.IntegrityError):
        con.execute("DELETE FROM tasks WHERE id = 1")
    con.rollback()
    assert _ids(con, "tasks") == [1]
    assert _ids(con, "agent_runs") == [1]

    # The documented cleanup order (runs first) is the only way through.
    con.execute("DELETE FROM agent_runs WHERE task_id = 1")
    con.execute("DELETE FROM tasks WHERE id = 1")
    con.commit()
    assert _ids(con, "tasks") == []


def test_a_task_with_a_support_item_cannot_be_deleted(con) -> None:
    """`support_inbox_items.task_id` has no ON DELETE: a customer message is not a task artefact."""
    _task(con, 1)
    con.execute(
        "INSERT INTO support_inbox_items (id, provider_message_id, from_address, subject, body, task_id) "
        "VALUES (1, 'pm-1', 'a@b.c', 'help', 'body', 1)"
    )
    con.commit()

    with pytest.raises(sqlite3.IntegrityError):
        con.execute("DELETE FROM tasks WHERE id = 1")
    con.rollback()
    assert _ids(con, "tasks") == [1]
    assert _ids(con, "support_inbox_items") == [1]


def test_a_prospect_with_send_history_cannot_be_deleted(con) -> None:
    """The owner-facing parent that DOES have children: `email_logs.prospect_id` (NOT NULL, no ON DELETE)."""
    con.execute("INSERT INTO prospects (id, email) VALUES (1, 'p@example.com')")
    con.execute("INSERT INTO email_campaigns (id, name, status) VALUES (1, 'real', 'sent')")
    con.execute(
        "INSERT INTO email_logs (id, prospect_id, campaign_id, status) VALUES (1, 1, 1, 'sent')"
    )
    con.commit()

    with pytest.raises(sqlite3.IntegrityError):
        con.execute("DELETE FROM prospects WHERE id = 1")
    con.rollback()
    assert _ids(con, "prospects") == [1]
    assert _ids(con, "email_logs") == [1]
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []


def test_the_orm_and_the_shipped_ddl_agree_on_every_task_child_cascade(migrated_db) -> None:
    """A drift pin with no database session: ORM `ondelete` == what the chain actually shipped."""
    import app.models  # noqa: F401
    from app.models.chat import ChatMessage
    from app.models.outreach import EmailLog
    from app.models.support import SupportInboxItem
    from app.models.task import AgentRun, TaskAttachment
    from app.models.vision import VisionDescriptionRow

    expected: dict[str, dict[str, str]] = {}
    for model in (TaskAttachment, VisionDescriptionRow, AgentRun, SupportInboxItem, ChatMessage, EmailLog):
        table = model.__table__
        expected[table.name] = {
            fk.parent.name: (fk.ondelete or "NO ACTION") for fk in table.foreign_keys
        }

    con = sqlite3.connect(migrated_db.as_posix())
    try:
        for table_name, fk_map in expected.items():
            shipped = {
                row[3]: row[6] or "NO ACTION"
                for row in con.execute(f"PRAGMA foreign_key_list({table_name})")
            }
            assert shipped == fk_map, (
                f"{table_name}: ORM declares {fk_map}, the chain shipped {shipped}"
            )
        # The specific cascades the brief names, stated in words as well as in the map.
        assert expected["task_attachments"]["task_id"] == "CASCADE"
        assert expected["vision_descriptions"]["task_id"] == "CASCADE"
        assert expected["vision_descriptions"]["attachment_id"] == "CASCADE"
        assert expected["agent_runs"]["task_id"] == "NO ACTION"
        assert expected["email_logs"]["prospect_id"] == "NO ACTION"
    finally:
        con.close()


async def test_deleting_an_attachment_through_the_service_removes_its_descriptions(
    async_db_session,
) -> None:
    """The user-facing consequence of D-3: `DELETE .../attachments/{id}` on the documented
    non-Docker (SQLite) stack removes the row but leaves the vision-memory rows behind."""
    from sqlalchemy import func, select

    from app.models.task import Task, TaskAttachment
    from app.models.vision import VisionDescriptionRow
    from app.services.task_attachment_service import delete_attachment

    task = Task(title="t", agent_type="finance", status="pending")
    async_db_session.add(task)
    await async_db_session.flush()
    attachment = TaskAttachment(
        task_id=task.id,
        filename="a.png",
        stored_name="a" * 32 + ".png",
        mime="image/png",
        size_bytes=3,
        sha256=SHA,
    )
    async_db_session.add(attachment)
    await async_db_session.flush()
    async_db_session.add(
        VisionDescriptionRow(
            attachment_id=attachment.id,
            task_id=task.id,
            sha256=SHA,
            description="a red square",
        )
    )
    await async_db_session.commit()

    await delete_attachment(async_db_session, attachment)
    await async_db_session.commit()

    remaining = (
        await async_db_session.execute(select(func.count()).select_from(VisionDescriptionRow))
    ).scalar_one()
    assert remaining == 0, "deleting an attachment must not strand its vision descriptions"


async def test_the_sqlite_app_engine_enforces_foreign_keys(monkeypatch, tmp_path) -> None:
    from app.config import settings
    from app.core.database import dispose_engine, get_engine

    await dispose_engine()
    monkeypatch.setattr(
        settings, "database_url", f"sqlite+aiosqlite:///{(tmp_path / 'app_engine.sqlite').as_posix()}"
    )
    engine = get_engine()
    try:
        async with engine.connect() as connection:
            value = (await connection.exec_driver_sql("PRAGMA foreign_keys")).scalar()
        assert value == 1, (
            "SQLite app connections must enforce foreign keys or every ON DELETE CASCADE in the "
            "migrations is decorative"
        )
    finally:
        await dispose_engine()
