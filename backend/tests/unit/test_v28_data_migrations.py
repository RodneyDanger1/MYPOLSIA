"""V28/L5 — the migration chain from ZERO: fresh SQLite file → `0017` with all 25 tables.

Why this file exists
--------------------
Every shipped chain test starts at an intermediate revision (`0002` in
`test_fs2_l2_07_sqlite_fk_guard.py`, `0004` in `test_support_inbox_migration.py`, …), so nothing in
the tree proves the FIRST link: that `alembic upgrade head` over an EMPTY database — the CI e2e
case, the fresh-container case, the operator's first `make up` — produces the schema the ORM and
the API expect.  A chain that only works when a previous database exists is not a chain.

What is pinned
--------------
1. from an empty file the chain reaches the single head `0017` and creates exactly the 25 ORM
   tables (no extra table, no missing table), with `PRAGMA foreign_key_check` empty;
2. the revision graph is one line of 17 revisions ending at `0017` — no branch, no orphan;
3. a second `upgrade head` on the already-migrated database is a NO-OP (schema bytes unchanged);
4. the ads money columns the chain ships carry NO server default, which is what makes "absent"
   representable at rest (the 0014/0015 law).

W4/F3 DELIBERATE RE-PIN (2026-10-28; DT-01 + DT-05, migration 0017): old head `0016` → `0017`,
old revision count 16 → 17, old table count 24 → 25 (`invoice_dunning_claim_results`, the
append-only dunning claim ledger).  The four laws above are unchanged; the 0017 revision is
additive except for DT-01's deliberate drop of the seven fabricated server defaults.
"""
from __future__ import annotations

import pathlib
import sqlite3

import pytest
from alembic.script import ScriptDirectory

from alembic import command
from alembic import config as alembic_config

BACKEND = pathlib.Path(__file__).resolve().parents[2]
# W4/F3 DELIBERATE RE-PIN (2026-10-28): head 0016 -> 0017 (migration
# `0017_truth_defaults_and_dunning_results.py`, DT-01 + DT-05), 16 -> 17 revisions.
HEAD = "0017"
REVISION_COUNT = 17

#: The schema's 25 product tables (24 at `f0ce8a2` + 0017's append-only dunning result table,
#: measured from `Base.metadata` by F3 on 2026-10-28).  Spelled out (not
#: derived) so a migration that silently drops a table cannot also silently drop the assertion.
EXPECTED_TABLES = {
    "activity_log",
    "ad_campaigns",
    "ad_metrics",
    "agent_runs",
    "auto_mode_sessions",
    "chat_messages",
    "company_config",
    "competitors",
    "daily_reports",
    "email_campaigns",
    "email_logs",
    "expense_records",
    # W4/DT-05 (0017): the append-only finalization rows for `invoice_dunning_claim`.
    "invoice_dunning_claim_results",
    "invoices",
    "memory_entries",
    "prospects",
    "revenue_snapshots",
    "social_engagements",
    "social_posts",
    "stripe_events",
    "support_inbox_items",
    "task_attachments",
    "task_schedules",
    "tasks",
    "vision_descriptions",
}


@pytest.fixture
def fresh_db(monkeypatch, tmp_path):
    """An EMPTY SQLite file plus the alembic Config `env.py` reads (`settings.database_url`)."""
    from app.config import settings

    db_path = tmp_path / "v28_from_zero.sqlite"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    monkeypatch.setattr(settings, "database_url", url)
    cfg = alembic_config.Config()
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg, db_path


def _connect(db_path: pathlib.Path) -> sqlite3.Connection:
    con = sqlite3.connect(db_path.as_posix())
    con.execute("PRAGMA foreign_keys=ON")
    return con


def _product_tables(con: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version'"
        )
    }


def _schema_dump(con: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """Every object's (type, name, SQL) — the whole schema, sorted, for an exact before/after."""
    return sorted(
        con.execute(
            "SELECT type, name, COALESCE(sql, '') FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
    )


def test_the_chain_from_zero_reaches_0017_with_exactly_the_25_model_tables(fresh_db) -> None:
    """THE brief's first case: empty file → head → the schema the app believes it has."""
    import app.models  # noqa: F401 — registers every table on Base.metadata
    from app.core.database import Base

    cfg, db_path = fresh_db
    command.upgrade(cfg, "head")

    con = _connect(db_path)
    try:
        assert con.execute("SELECT version_num FROM alembic_version").fetchone()[0] == HEAD
        tables = _product_tables(con)
        # W4/F3 DELIBERATE RE-PIN (2026-10-28): old 24 -> new 25 product tables; the addition is
        # 0017's `invoice_dunning_claim_results` (DT-05).  The equality to both the spelled-out
        # set and the ORM is unchanged.
        assert len(tables) == 25, f"expected 25 product tables, found {len(tables)}: {sorted(tables)}"
        assert tables == EXPECTED_TABLES, {
            "missing": sorted(EXPECTED_TABLES - tables),
            "unexpected": sorted(tables - EXPECTED_TABLES),
        }
        # The hard-coded list and the ORM must agree: a drift pin that cannot rot silently.
        assert tables == set(Base.metadata.tables), {
            "missing_from_migration": sorted(set(Base.metadata.tables) - tables),
            "missing_from_orm": sorted(tables - set(Base.metadata.tables)),
        }
        assert con.execute("PRAGMA foreign_key_check").fetchall() == [], (
            "a from-zero chain must not leave a dangling reference"
        )
    finally:
        con.close()


def test_the_revision_graph_is_one_line_of_seventeen_revisions_ending_at_0017(fresh_db) -> None:
    """No branch, no orphan: every revision is reachable from the single head by `down_revision`."""
    cfg, _db_path = fresh_db
    script = ScriptDirectory.from_config(cfg)

    # W4/F3 DELIBERATE RE-PIN (2026-10-28): head ["0016"] -> ["0017"]; 16 -> 17 chain links
    # (`0017_truth_defaults_and_dunning_results.py`, DT-01 + DT-05).  Linearity itself unchanged.
    assert script.get_heads() == [HEAD], script.get_heads()
    chain: list[str] = []
    revision = script.get_revision(HEAD)
    while revision is not None:
        chain.append(revision.revision)
        assert revision.down_revision is None or isinstance(revision.down_revision, str), (
            f"{revision.revision} has a merge/branch down_revision {revision.down_revision!r}"
        )
        revision = script.get_revision(revision.down_revision) if revision.down_revision else None
    assert len(chain) == REVISION_COUNT, chain
    assert chain[0] == HEAD and chain[-1] == "0001", chain
    assert len(set(chain)) == len(chain), f"a revision repeats in the chain: {chain}"


def test_a_second_upgrade_head_is_a_noop_on_the_migrated_database(fresh_db) -> None:
    """The e2e/dev path runs `upgrade head` on every boot; a repeat must not rebuild or lose rows."""
    cfg, db_path = fresh_db
    command.upgrade(cfg, "head")

    con = _connect(db_path)
    try:
        before = _schema_dump(con)
        con.execute(
            "INSERT INTO tasks (id, title, agent_type, status) VALUES (1, 'kept', 'finance', 'pending')"
        )
        con.commit()
    finally:
        con.close()

    command.upgrade(cfg, "head")

    con = _connect(db_path)
    try:
        assert _schema_dump(con) == before, "a second upgrade head changed the schema"
        # W4/F3 DELIBERATE RE-PIN (2026-10-28): head 0016 -> 0017 (see HEAD above).
        assert con.execute("SELECT version_num FROM alembic_version").fetchone()[0] == HEAD
        assert con.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1, (
            "a second upgrade head must not touch rows"
        )
    finally:
        con.close()


def test_the_shipped_ads_money_columns_have_no_server_default(fresh_db) -> None:
    """0014/0015's law at the DDL level: a column default would re-conflate absent with zero."""
    cfg, db_path = fresh_db
    command.upgrade(cfg, "head")

    con = _connect(db_path)
    try:
        columns = {
            row[1]: {"notnull": bool(row[3]), "default": row[4]}
            # `table_xinfo`, not `table_info`: the latter hides the GENERATED ctr/cpc/roas columns.
            for row in con.execute("PRAGMA table_xinfo(ad_metrics)")
        }
        for money in ("spend_usd", "revenue_usd"):
            assert money in columns, f"{money} is missing from the shipped ad_metrics"
            assert columns[money]["notnull"] is False, f"{money} must stay nullable"
            assert columns[money]["default"] is None, (
                f"{money} carries a server default {columns[money]['default']!r}: an omitted value "
                "would be stored as a measured zero"
            )
        assert {"ctr", "cpc", "roas"} <= set(columns), sorted(columns)
    finally:
        con.close()
