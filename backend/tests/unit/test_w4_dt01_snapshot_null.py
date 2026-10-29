"""W4 / DT-01 — an unmeasured finance snapshot column is NULL, never a fabricated 0.

THE DEFECT (data-hunter DT-01, P1).  `revenue_snapshots` shipped at 0001 with `server_default="0"`
on all seven money/count columns, and `persist_finance_run` passed only the keys the run supplied.
A partial snapshot therefore STORED `arr=0, active=0, churn=0, new=0, month_revenue=0, balance=0`
and `GET /finance/summary`, the dashboard cards and `build_cash_block.cash_on_hand` served those
fabricated zeros as measurements — "omitted" and "measured 0" collapsed into one state.

THE FIX under test:
  * `finance_service.upsert_revenue_snapshot` writes an explicit NULL for every column the caller
    did not supply (INSERT leg) and only moves supplied columns (UPDATE leg, so a retry cannot
    erase a same-day measurement);
  * `models/finance.py` lost the ORM `default=0`s;
  * migration `0017` dropped the seven server defaults (SQLite rebuild + PostgreSQL ALTER);
  * a caller-supplied 0 is still stored and served as a MEASURED zero.

RED leg: against the pre-fix tree the first test below sees the omitted columns as 0 (and the
summary as 0), so it fails on the assertion, not on an import.
"""
from __future__ import annotations

import sqlite3
from datetime import date

import pytest
from sqlalchemy import select

from app.core.errors import E_VALIDATION, MeridianError
from app.models.finance import RevenueSnapshot
from app.services import finance_service

DAY = date(2026, 10, 20)
#: The seven columns DT-01 is about; the migration and the writer must agree on this set.
MONEY_COLUMNS = (
    "mrr_cents",
    "arr_cents",
    "active_subscribers",
    "churned_today",
    "new_today",
    "total_revenue_month_cents",
    "stripe_balance_cents",
)


@pytest.mark.asyncio
async def test_a_partial_snapshot_leaves_every_unmeasured_money_column_null(async_db_session):
    """DT-01's named test: only MRR supplied -> six NULLs, and the summary says unknown."""
    result = await finance_service.persist_finance_run(
        async_db_session,
        {"snapshot": {"snapshot_date": DAY.isoformat(), "mrr_cents": 500_000}},
    )
    assert result["ok"] is True, result
    assert result["snapshot_written"] is True

    async_db_session.expunge_all()  # force a real read-back, not the identity map
    row = (
        await async_db_session.execute(
            select(RevenueSnapshot).where(RevenueSnapshot.snapshot_date == DAY)
        )
    ).scalar_one()
    assert row.mrr_cents == 500_000
    for column in MONEY_COLUMNS:
        if column == "mrr_cents":
            continue
        assert getattr(row, column) is None, (
            f"{column} was not measured by the run but stored {getattr(row, column)!r}; an "
            f"unmeasured fact must be NULL, never a fabricated 0 (DT-01)"
        )

    summary = await finance_service.get_finance_summary(async_db_session, month=DAY)
    assert summary["mrr_cents"] == 500_000
    for key in (
        "arr_cents",
        "active_subscribers",
        "stripe_balance_cents",
        "total_revenue_month_cents",
    ):
        assert summary[key] is None, (
            f"GET /finance/summary served {summary[key]!r} for {key}; the run never measured it"
        )
    assert summary["last_snapshot_date"] == DAY


@pytest.mark.asyncio
async def test_a_full_snapshot_round_trips_and_an_explicit_zero_is_measured(async_db_session):
    """The other direction: supplied values survive, INCLUDING a measured zero."""
    supplied = {
        "mrr_cents": 100_000,
        "arr_cents": 1_200_000,
        "active_subscribers": 0,  # a measured "nobody" is a measurement
        "churned_today": 0,
        "new_today": 3,
        "total_revenue_month_cents": 250_000,
        "stripe_balance_cents": 987_654,
    }
    result = await finance_service.persist_finance_run(
        async_db_session,
        {"snapshot": {"snapshot_date": DAY.isoformat(), **supplied}},
    )
    assert result["ok"] is True, result

    async_db_session.expunge_all()
    row = (
        await async_db_session.execute(
            select(RevenueSnapshot).where(RevenueSnapshot.snapshot_date == DAY)
        )
    ).scalar_one()
    for column, value in supplied.items():
        assert getattr(row, column) == value, (
            f"{column}: a full snapshot must round-trip exactly, got {getattr(row, column)!r}"
        )

    summary = await finance_service.get_finance_summary(async_db_session, month=DAY)
    assert summary["active_subscribers"] == 0, "a measured 0 must NOT be reported as unknown"
    assert summary["arr_cents"] == 1_200_000
    assert summary["total_revenue_month_cents"] == 250_000


@pytest.mark.asyncio
async def test_a_retry_that_omits_a_column_does_not_erase_a_same_day_measurement(async_db_session):
    """The UPDATE leg moves only what THIS call supplied (the row is the DAY's snapshot)."""
    await finance_service.upsert_revenue_snapshot(
        async_db_session, snapshot_date=DAY, mrr_cents=1_000, arr_cents=12_000
    )
    await finance_service.upsert_revenue_snapshot(
        async_db_session, snapshot_date=DAY, mrr_cents=2_000
    )
    row = (
        await async_db_session.execute(
            select(RevenueSnapshot).where(RevenueSnapshot.snapshot_date == DAY)
        )
    ).scalar_one()
    assert row.mrr_cents == 2_000, "the supplied column is updated"
    assert row.arr_cents == 12_000, (
        "the retry omitted arr_cents; the value an earlier same-day run measured must survive"
    )
    assert row.active_subscribers is None


@pytest.mark.asyncio
async def test_an_unknown_snapshot_key_is_the_named_validation_refusal(async_db_session):
    """A key with no column is refused BY NAME instead of dying inside the INSERT compiler."""
    with pytest.raises(MeridianError) as refused:
        await finance_service.upsert_revenue_snapshot(
            async_db_session, snapshot_date=DAY, not_a_column=1
        )
    assert refused.value.code == E_VALIDATION
    assert "not_a_column" in refused.value.message


@pytest.mark.asyncio
async def test_cash_on_hand_is_null_when_the_snapshot_carries_no_balance(async_db_session):
    """DT-01's blast radius: `build_cash_block.cash_on_hand` must not read a fabricated 0."""
    from app.services.report_service import build_cash_block

    await finance_service.persist_finance_run(
        async_db_session,
        {"snapshot": {"snapshot_date": DAY.isoformat(), "mrr_cents": 500_000}},
    )
    block = await build_cash_block(async_db_session)
    assert block["cash_on_hand"] is None, (
        "the newest snapshot has no measured Stripe balance; cash_on_hand must be unknown"
    )
    assert "no measured Stripe balance" in block["reasons"]["cash_on_hand"]


# ── the migration leg (SQLite; PostgreSQL is PG-ONLY/UNVERIFIED on this host) ────────────────
from tests.unit.test_r26_ads_sweep import head_db  # noqa: E402,F401  (fixture registration)


def _money_default_count(db_path: str) -> int:
    con = sqlite3.connect(db_path)
    try:
        ddl = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='revenue_snapshots'"
        ).fetchone()[0]
    finally:
        con.close()
    return sum(1 for line in ddl.splitlines() if "INTEGER DEFAULT '0'" in line)


def test_at_the_migration_head_a_raw_insert_that_omits_money_stores_null(
    head_db,  # noqa: F811 — the module-level import registers the fixture; this asks for it
):
    """The DDL half of DT-01: no server default can fabricate a 0 for a raw INSERT."""
    db_path, _url = head_db
    assert _money_default_count(db_path) == 0, (
        "migration 0017 must leave revenue_snapshots with NO fabricated DEFAULT 0 "
        "(the pre-fix DDL carries seven)"
    )
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "INSERT INTO revenue_snapshots (snapshot_date) VALUES ('2026-10-21')"
        )
        con.commit()
        row = con.execute(
            "SELECT mrr_cents, arr_cents, active_subscribers, churned_today, new_today, "
            "total_revenue_month_cents, stripe_balance_cents FROM revenue_snapshots "
            "WHERE snapshot_date = '2026-10-21'"
        ).fetchone()
    finally:
        con.close()
    assert row == (None,) * len(MONEY_COLUMNS), (
        f"a raw INSERT that omits the money columns stored {row!r}; every omitted column must be NULL"
    )


def test_migration_0017_round_trips_and_preserves_rows_on_sqlite(monkeypatch, tmp_path):
    """upgrade head -> downgrade 0016 -> upgrade head, with a measured row surviving each leg.

    The pre-0017 DDL must be RESTORED by `downgrade()` (seven defaults) and dropped again by
    `upgrade()`; the row's values must never be touched by the rebuild dance.
    """
    from alembic import command
    from alembic import config as alembic_config
    from app.config import settings

    db_path = tmp_path / "w4_0017_roundtrip.sqlite"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    monkeypatch.setattr(settings, "database_url", url)
    cfg = alembic_config.Config()
    cfg.set_main_option("script_location", "alembic")
    cfg.set_main_option("sqlalchemy.url", url)

    command.upgrade(cfg, "0016")
    assert _money_default_count(str(db_path)) == 7, "0016 must be the pre-fix DDL"
    con = sqlite3.connect(str(db_path))
    try:
        con.execute(
            "INSERT INTO revenue_snapshots "
            "(snapshot_date, mrr_cents, arr_cents, stripe_balance_cents) "
            "VALUES ('2026-10-22', 1234, NULL, 5678)"
        )
        con.commit()
    finally:
        con.close()

    command.upgrade(cfg, "head")
    assert _money_default_count(str(db_path)) == 0
    con = sqlite3.connect(str(db_path))
    try:
        # W2 DELIBERATE RE-PIN (2026-10-28): head 0017 -> 0018 (`0018_consumed_approvals.py`,
        # additive).  This test still proves 0017's own effects below, and now also proves the
        # 0018 upgrade applies cleanly on top of a database that already carries rows.
        # W8 DELIBERATE RE-PIN (2026-10-29): head 0019 -> 0020 (`0020_email_sent_requires_
        # provider_evidence.py`, additive to revenue_snapshots — it only touches email_logs).
        assert con.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0020"
        assert con.execute(
            "SELECT mrr_cents, arr_cents, stripe_balance_cents FROM revenue_snapshots "
            "WHERE snapshot_date = '2026-10-22'"
        ).fetchone() == (1234, None, 5678)
        # the explicit index survives the rebuild dance
        assert con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='ix_revenue_snapshots_date'"
        ).fetchone()[0] == 1
        assert con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
            "AND name='invoice_dunning_claim_results'"
        ).fetchone()[0] == 1
        assert con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
            "AND name='consumed_approvals'"
        ).fetchone()[0] == 1, "0018's approval ledger must be created by upgrade head"
    finally:
        con.close()

    command.downgrade(cfg, "0016")
    assert _money_default_count(str(db_path)) == 7, "downgrade must restore the seven defaults"
    con = sqlite3.connect(str(db_path))
    try:
        assert con.execute(
            "SELECT mrr_cents, arr_cents, stripe_balance_cents FROM revenue_snapshots "
            "WHERE snapshot_date = '2026-10-22'"
        ).fetchone() == (1234, None, 5678), "the rebuild must not lose or rewrite a row"
        assert con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
            "AND name='invoice_dunning_claim_results'"
        ).fetchone()[0] == 0, "downgrade must drop the DT-05 table"
        assert con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
            "AND name='consumed_approvals'"
        ).fetchone()[0] == 0, "downgrade must drop the W2 approval ledger"
    finally:
        con.close()

    command.upgrade(cfg, "head")
    assert _money_default_count(str(db_path)) == 0


def test_the_chain_has_a_single_head_at_0018():
    """R5: 0017 owns the W4 slots and 0018 the W2 approval ledger; nothing forks the chain."""
    from alembic.config import Config as AlembicConfig
    from alembic.script import ScriptDirectory

    cfg = AlembicConfig()
    cfg.set_main_option("script_location", "alembic")
    # W2 DELIBERATE RE-PIN (2026-10-28): old assertion `heads == ["0017"]`; head moved to 0018
    # (`0018_consumed_approvals.py`, additive).  The single-head LAW is unchanged.
    # W7/W8 DELIBERATE RE-PIN: 0019 (email acceptance), then 0020 (the same CHECK strengthened).
    assert ScriptDirectory.from_config(cfg).get_heads() == ["0020"]
