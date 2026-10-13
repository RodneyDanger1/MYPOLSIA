"""R17 — `alembic/versions/0004_support_inbox.py` round-trips on SQLite and moves EXACTLY itself.

Drives alembic's command API in-process, exactly as `alembic upgrade head` does: `env.py`
reads `settings.database_url` (monkeypatched here to a tmp file) and builds its own engine, so
nothing in this test touches the app-wide engine or any other test's database.

Evidence the legs produce: `sqlite_master` snapshots (type, name, sql) before/after the
downgrade — the diff is precisely the new table and its two indexes (+ SQLite's implicit
unique autoindex), every surviving row byte-identical — and the re-upgrade restores the same
DDL byte-for-byte.  The CREATE TABLE text is additionally byte-EQUAL to the ORM's
`create_all` (probe: build/R17-inbox/raw/probe_ddl_parity.txt), so the frozen schema pin and
the migration cannot describe different tables.
"""
from __future__ import annotations

import pathlib
import sqlite3

import pytest

from alembic import command
from alembic import config as alembic_config

BACKEND = pathlib.Path(__file__).resolve().parents[2]
TABLE = "support_inbox_items"
INDEXES = ("ix_support_inbox_items_status", "ix_support_inbox_items_task_id")


def _snapshot(db_path: pathlib.Path) -> dict[tuple[str, str], str | None]:
    con = sqlite3.connect(db_path.as_posix())
    try:
        return {
            (kind, name): sql
            for kind, name, sql in con.execute(
                "SELECT type, name, sql FROM sqlite_master ORDER BY name"
            )
        }
    finally:
        con.close()


def _scalar(db_path: pathlib.Path, sql: str):
    con = sqlite3.connect(db_path.as_posix())
    try:
        return con.execute(sql).fetchone()[0]
    finally:
        con.close()


@pytest.fixture
def alembic_db(monkeypatch, tmp_path):
    """A tmp SQLite database plus the alembic Config env.py will read (settings.database_url)."""
    from app.config import settings

    db_path = tmp_path / "r17_migration.sqlite"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    monkeypatch.setattr(settings, "database_url", url)
    cfg = alembic_config.Config()
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg, db_path


def test_0004_upgrade_downgrade_reupgrade_moves_only_its_own_rows(alembic_db):
    cfg, db_path = alembic_db

    command.upgrade(cfg, "head")
    at_head = _snapshot(db_path)
    assert _scalar(db_path, "SELECT version_num FROM alembic_version") == "0004"
    assert ("table", TABLE) in at_head
    for index in INDEXES:
        assert ("index", index) in at_head
    expected_own_rows = {
        ("table", TABLE),
        ("index", INDEXES[0]),
        ("index", INDEXES[1]),
        # SQLite's implicit autoindex backing the inline UNIQUE (provider_message_id)
        ("index", "sqlite_autoindex_support_inbox_items_1"),
    }
    assert expected_own_rows <= set(at_head)

    command.downgrade(cfg, "-1")
    at_0003 = _snapshot(db_path)

    assert set(at_head) - set(at_0003) == expected_own_rows, (
        "downgrade -1 must drop exactly the 0004 table + its indexes and nothing else"
    )
    assert not set(at_0003) - set(at_head), "downgrade must not add anything"
    assert {key: at_head[key] for key in at_0003} == at_0003, (
        "every surviving table/index must stay byte-identical through the downgrade"
    )

    command.upgrade(cfg, "head")
    assert _snapshot(db_path) == at_head, "re-upgrade must restore the same DDL byte-for-byte"


def test_0004_creates_the_pinned_columns_constraints_and_fk(alembic_db):
    cfg, db_path = alembic_db
    command.upgrade(cfg, "head")

    con = sqlite3.connect(db_path.as_posix())
    try:
        columns = [
            (row[1], row[2], bool(row[3]), row[4])
            for row in con.execute(f"PRAGMA table_info({TABLE})")
        ]
        unique_indexes = [
            (row[1], bool(row[2])) for row in con.execute(f"PRAGMA index_list({TABLE})")
        ]
        foreign_keys = [
            (row[2], row[3], row[4]) for row in con.execute(f"PRAGMA foreign_key_list({TABLE})")
        ]
    finally:
        con.close()

    assert columns == [
        ("id", "INTEGER", True, None),
        ("provider_message_id", "VARCHAR(255)", True, None),
        ("from_address", "VARCHAR(255)", True, None),
        ("subject", "VARCHAR(512)", True, None),
        ("body", "TEXT", True, None),
        ("status", "VARCHAR(50)", True, "'pending'"),
        ("received_at", "DATETIME", False, None),
        ("task_id", "INTEGER", False, None),
        ("created_at", "DATETIME", False, "CURRENT_TIMESTAMP"),
    ]
    assert any(unique and name.startswith("sqlite_autoindex") for name, unique in unique_indexes)
    assert foreign_keys == [("tasks", "task_id", "id")], (
        "task_id must reference tasks.id — the item points at the task that serves it"
    )
