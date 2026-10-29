"""W2 — the single-use approval ledger (`consumed_approvals`, migration 0018).

Ownership split: sentinel implemented the ENFORCEMENT (`policy_firewall._split_token` /
`consume_approval` / `consume_approval_or_refuse` and the gates that call them); truthseeker owns
the MODEL + MIGRATION + the schema census pins.  These tests pin the DATA-LAYER guarantee the
enforcement relies on, independently of that enforcement code:

  * the shape of `consumed_approvals` and both of its unique keys (`token_id` — the token's ledger
    identity, sha256 of its signature body — and `jti` — the mint nonce; nullable so legacy
    nonce-less tokens can still be spent);
  * the DATABASE refuses a second spend of one token / one nonce (an `IntegrityError`, not a
    Python read-then-write check that two processes could race);
  * the migration round-trips `0017 -> 0018 -> 0017 -> 0018` on SQLite while preserving rows of
    pre-existing tables, and its downgrade drops only the ledger it created.

WHY A UNIQUE KEY IS THE RIGHT MECHANISM (and the same reasoning finance used for the Stripe
webhook race): a token can be presented to the API process or to a Celery worker, so the spend
check must be atomic ACROSS PROCESSES.  The database's unique index is the only atomicity
primitive this stack already trusts.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest
from sqlalchemy import UniqueConstraint, func, select
from sqlalchemy.exc import IntegrityError

from app.core.database import Base
from app.models.approval import ConsumedApproval

LEDGER = "consumed_approvals"


def _table():
    return Base.metadata.tables[LEDGER]


async def _count(db) -> int:
    return int((await db.execute(select(func.count()).select_from(ConsumedApproval))).scalar_one())


def _row(token_id: str, jti: str | None, by: str = "w2-test") -> ConsumedApproval:
    return ConsumedApproval(
        token_id=token_id,
        jti=jti,
        action_fingerprint="external_irreversible|render|deploy",
        consumed_by=by,
    )


# ── 1. the shape the enforcement writes into ─────────────────────────────────────────────────


def test_the_ledger_shape_and_both_unique_keys() -> None:
    """`token_id` and `jti` are BOTH unique; `jti` is nullable (legacy tokens have no nonce)."""
    table = _table()
    assert [column.name for column in table.columns] == [
        "id",
        "token_id",
        "jti",
        "action_fingerprint",
        "consumed_at",
        "consumed_by",
    ]
    assert table.c.token_id.nullable is False
    assert table.c.jti.nullable is True, "a legacy token carries no nonce: NULL must be representable"
    assert table.c.action_fingerprint.nullable is False
    assert table.c.consumed_at.nullable is False
    assert table.c.consumed_by.nullable is False

    single_column_uniques = {
        f"{next(iter(constraint.columns)).name}"
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint) and len(constraint.columns) == 1
    }
    assert "token_id" in single_column_uniques

    unique_indexes = {
        index.name: ([column.name for column in index.columns], bool(index.unique))
        for index in table.indexes
    }
    assert unique_indexes == {
        "uq_consumed_approvals_jti": (["jti"], True),
        "ix_consumed_approvals_fingerprint": (["action_fingerprint"], False),
        "ix_consumed_approvals_consumed_at": (["consumed_at"], False),
    }


# ── 2. the DATABASE guarantee (the whole point of the table) ─────────────────────────────────


@pytest.mark.asyncio
async def test_a_second_spend_of_one_token_is_refused_by_the_database(async_db_session) -> None:
    async_db_session.add(_row("a" * 64, "0123456789abcdef"))
    await async_db_session.flush()
    async_db_session.add(_row("a" * 64, "fedcba9876543210"))
    with pytest.raises(IntegrityError):
        await async_db_session.flush()
    await async_db_session.rollback()


@pytest.mark.asyncio
async def test_one_nonce_cannot_be_spent_by_two_token_bodies(async_db_session) -> None:
    """`jti` = one mint: a different token body carrying an already-spent nonce is refused."""
    async_db_session.add(_row("b" * 64, "same-jti-0000000"))
    await async_db_session.flush()
    async_db_session.add(_row("c" * 64, "same-jti-0000000"))
    with pytest.raises(IntegrityError):
        await async_db_session.flush()
    await async_db_session.rollback()


@pytest.mark.asyncio
async def test_two_legacy_spends_without_a_nonce_coexist(async_db_session) -> None:
    """NULL nonces are distinct on both engines: legacy token support must not collide."""
    async_db_session.add(_row("d" * 64, None))
    async_db_session.add(_row("e" * 64, None))
    await async_db_session.flush()
    assert await _count(async_db_session) == 2


@pytest.mark.asyncio
async def test_the_audit_columns_are_filled_by_defaults(async_db_session) -> None:
    """`consumed_at` is the database clock; `consumed_by` defaults to "" (never NULL)."""
    row = ConsumedApproval(token_id="f" * 64, jti=None, action_fingerprint="fp")
    async_db_session.add(row)
    await async_db_session.flush()
    await async_db_session.refresh(row)
    assert isinstance(row.consumed_at, datetime), "consumed_at must be database-generated"
    assert row.consumed_by == ""


# ── 3. migration 0018: additive, reversible, single-head, and does not touch other rows ──────


def _index_names(con: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in con.execute(f"PRAGMA index_list({table})").fetchall()]


def test_migration_0018_round_trips_and_preserves_unrelated_rows(monkeypatch, tmp_path) -> None:
    """0017 -> 0018 -> 0017 -> 0018: the ledger appears/disappears, a task row survives every leg."""
    from alembic import command
    from alembic import config as alembic_config
    from app.config import settings

    db_path = tmp_path / "w2_0018_roundtrip.sqlite"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    monkeypatch.setattr(settings, "database_url", url)
    cfg = alembic_config.Config()
    cfg.set_main_option("script_location", "alembic")
    cfg.set_main_option("sqlalchemy.url", url)

    command.upgrade(cfg, "0017")
    con = sqlite3.connect(db_path.as_posix())
    try:
        assert con.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0017"
        assert con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='consumed_approvals'"
        ).fetchone()[0] == 0, "the ledger must not exist below its revision"
        con.execute(
            "INSERT INTO tasks (title, agent_type, status) VALUES ('w2 kept', 'finance', 'pending')"
        )
        con.commit()
    finally:
        con.close()

    command.upgrade(cfg, "head")
    con = sqlite3.connect(db_path.as_posix())
    try:
        assert con.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0019"
        assert con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='consumed_approvals'"
        ).fetchone()[0] == 1
        assert "uq_consumed_approvals_jti" in _index_names(con, LEDGER)
        # The behavioral guarantee on the MIGRATED schema (not just create_all): the second spend
        # of one token_id is refused, and the database supplies consumed_at.
        con.execute(
            "INSERT INTO consumed_approvals (token_id, jti, action_fingerprint, consumed_by) "
            "VALUES ('1' || substr(hex(randomblob(31)), 1, 63), '0123456789abcdef', 'fp', 'roundtrip')"
        )
        con.execute(
            "INSERT INTO consumed_approvals (token_id, jti, action_fingerprint, consumed_by) "
            "VALUES ('2' || substr(hex(randomblob(31)), 1, 63), NULL, 'fp', 'legacy')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                "INSERT INTO consumed_approvals (token_id, jti, action_fingerprint, consumed_by) "
                "VALUES ((SELECT token_id FROM consumed_approvals LIMIT 1), NULL, 'fp', 'dup')"
            )
        assert con.execute(
            "SELECT COUNT(*) FROM consumed_approvals WHERE consumed_at IS NULL"
        ).fetchone()[0] == 0, "consumed_at must be database-generated on the migrated schema"
        assert con.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        con.rollback()
    finally:
        con.close()

    command.downgrade(cfg, "0017")
    con = sqlite3.connect(db_path.as_posix())
    try:
        assert con.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0017"
        assert con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='consumed_approvals'"
        ).fetchone()[0] == 0, "downgrade must drop only the table this revision created"
        assert con.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1, (
            "the downgrade must not touch a pre-existing table's rows"
        )
    finally:
        con.close()

    command.upgrade(cfg, "head")
    con = sqlite3.connect(db_path.as_posix())
    try:
        assert con.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0019"
        assert con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='consumed_approvals'"
        ).fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM consumed_approvals").fetchone()[0] == 0, (
            "a downgrade/upgrade cycle cannot resurrect ledger rows — the drop is destructive by "
            "design and that is stated in the migration"
        )
        assert con.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    finally:
        con.close()
