"""ads spend is MEASURED or ABSENT — spend_usd loses its DEFAULT 0 (V26/L6)

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-19

WHY
---
0014 gave `ad_metrics.revenue_usd` the two states the ads surface exists to tell, and said so
plainly in its own docstring: `spend_usd` was left alone (it was already nullable) but still
carried `DEFAULT '0'`, so a writer that OMITTED spend got a stored zero.  That is the same
conflation revenue just shed: "we never saw a spend figure" and "we measured a $0.00 day" were
one stored byte.  `ads_service.record_ads_run` encoded the bug as a comment — "spend_usd keeps
the pre-existing shape: omitted -> the column's own default".

This revision finishes the pair:

* `spend_usd` — NULLABLE (unchanged) and NO server default.  The writer persists NULL when the
  run reported no spend; a reported `0` stays `0`.
* Backfill: every EXISTING `spend_usd = 0` row becomes NULL.  A stored zero written by the old
  DEFAULT was never certified as a measurement (nothing in the tree distinguished the two), so
  the honest migration of those rows is "unknown", not "$0 spent" — the SAME conservative rule
  0014 applied to revenue.  Irreversible in the data-sense; see DOWNGRADE.
* `revenue_usd` and `roas` are NOT touched: 0014's shape (nullable revenue, no default; the
  measured-only roas expression) is copied through the SQLite rebuild verbatim.  `ctr`/`cpc` are
  unchanged; `cpc` is GENERATED from spend, so it follows to NULL for a day with no spend.

DOWNGRADE
---------
Restores `DEFAULT 0` after `COALESCE(spend_usd, 0)` — the pre-0015 shape for spend, while
revenue stays at 0014's shape (this revision never owned revenue).  The information "this row
was never measured" is NOT recoverable after the downgrade: the rows that were NULL become
stored zeros again and will read as measured zeros on a pre-0015 reader.  That asymmetry is
inherent to the shape, exactly as 0014 documents it for revenue.

SQLite cannot ALTER a column's default, so — following 0014's measured no-rename rebuild — the
table is rebuilt through a `_v26_ad_metrics` temp table with the FINAL text re-created under the
real name (byte-faithful stored CREATE), or, for PostgreSQL, by a plain `ALTER COLUMN` (no
rebuild, no generated-column swap: the roas expression does not change here).

Single head: `down_revision` is "0014" (`0014_ads_nullable_revenue.py`, V25/L4).  Deliberately
NOT "0013": the chain is one line and this revision is its next link.
"""
from __future__ import annotations

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | None = None
depends_on: str | None = None

# The ratio expressions are copied VERBATIM from 0014's final shape (which in turn took them from
# 0002/0003 and its own measured-only roas rule).  `round(CAST(CAST(x AS REAL) / y AS NUMERIC), 6)`
# is the form both engines accept.
_ROAS_EXPR = (
    "CASE WHEN revenue_usd IS NOT NULL AND spend_usd > 0 THEN "
    "round(CAST(CAST(revenue_usd AS REAL) / CAST(spend_usd AS REAL) AS NUMERIC), 6) END"
)
_CTR_EXPR = (
    "CASE WHEN impressions > 0 THEN round(CAST(CAST(clicks AS REAL) / impressions AS NUMERIC), 6) "
    "ELSE 0 END"
)
_CPC_EXPR = (
    "CASE WHEN clicks > 0 THEN round(CAST(CAST(spend_usd AS REAL) / clicks AS NUMERIC), 6) ELSE 0 END"
)

#: Columns copied across every rebuild.  Generated columns are EXCLUDED and recompute on read
#: (0003/0014 precedent); spend is copied through an expression by the caller so the backfill is
#: explicit rather than a `SELECT *`.
_COPY_COLUMNS: tuple[str, ...] = (
    "id", "campaign_id", "date", "impressions", "clicks", "conversions", "spend_usd", "revenue_usd",
)

_INDEXES: tuple[str, ...] = (
    "CREATE INDEX ix_ad_metrics_date_campaign ON ad_metrics (date, campaign_id)",
    "CREATE UNIQUE INDEX uq_ad_metrics_campaign_date ON ad_metrics (campaign_id, date)",
)


def _ad_metrics_ddl(table: str, *, spend: str, revenue: str) -> str:
    """One CREATE TABLE text for `ad_metrics`.

    `spend` is this revision's difference (`"NUMERIC(14, 4)"` for the no-default shape,
    `"NUMERIC(14, 4) DEFAULT '0'"` for the pre-0015 shape); `revenue` is always 0014's
    (`"NUMERIC(14, 4)"`, nullable, no default).  Everything else is the measured 0014 text.
    """
    return (
        f"CREATE TABLE {table} (\n"
        "\tid INTEGER NOT NULL, \n"
        "\tcampaign_id INTEGER NOT NULL, \n"
        "\tdate DATE NOT NULL, \n"
        "\timpressions INTEGER DEFAULT '0', \n"
        "\tclicks INTEGER DEFAULT '0', \n"
        "\tconversions INTEGER DEFAULT '0', \n"
        f"\tspend_usd {spend}, \n"
        f"\trevenue_usd {revenue}, ctr NUMERIC(14, 6) GENERATED ALWAYS AS ({_CTR_EXPR}) VIRTUAL, "
        f"cpc NUMERIC(14, 6) GENERATED ALWAYS AS ({_CPC_EXPR}) VIRTUAL, "
        f"roas NUMERIC(14, 6) GENERATED ALWAYS AS ({_ROAS_EXPR}) VIRTUAL, \n"
        "\tPRIMARY KEY (id), \n"
        "\tFOREIGN KEY(campaign_id) REFERENCES ad_campaigns (id)\n"
        ")"
    )


#: SQLite: the no-default (upgrade) and pre-0015 (downgrade) shapes, temp + final.
_UPGRADE_TEMP_DDL = _ad_metrics_ddl(
    "_v26_ad_metrics", spend="NUMERIC(14, 4)", revenue="NUMERIC(14, 4)"
)
_UPGRADE_FINAL_DDL = _ad_metrics_ddl(
    "ad_metrics", spend="NUMERIC(14, 4)", revenue="NUMERIC(14, 4)"
)
_DOWNGRADE_TEMP_DDL = _ad_metrics_ddl(
    "_v26_ad_metrics", spend="NUMERIC(14, 4) DEFAULT '0'", revenue="NUMERIC(14, 4)"
)
_DOWNGRADE_FINAL_DDL = _ad_metrics_ddl(
    "ad_metrics", spend="NUMERIC(14, 4) DEFAULT '0'", revenue="NUMERIC(14, 4)"
)

#: The backfill / restore expressions.  `spend_usd = 0` matches every NUMERIC spelling of zero
#: (0, 0.0, 0.0000) in both engines; NaN/Infinity cannot reach this NUMERIC(14,4) column.
_BACKFILL_ZERO_TO_NULL = "CASE WHEN spend_usd = 0 THEN NULL ELSE spend_usd END"
_RESTORE_NULL_TO_ZERO = "COALESCE(spend_usd, 0)"


def _dialect_name() -> str:
    """Active dialect, online or in `--sql` (offline) mode — the 0003/0014 helper, unchanged."""
    try:
        return op.get_bind().dialect.name
    except Exception:  # noqa: BLE001 -- offline mode has no bind
        return op.get_context().dialect.name


def _rebuild(temp_ddl: str, final_ddl: str, spend_expression: str, verb: str) -> None:
    """SQLite table rebuild: temp create, copy (backfilled), drop, re-create, copy back, reindex.

    Row count is checked on BOTH sides of the move; a lost row aborts the migration instead of
    leaving a silently truncated table.  No RENAME is used: the final CREATE is re-issued under
    the real table name, so `sqlite_master.sql` is exactly the text a fresh chain would store.
    """
    bind = op.get_bind()
    columns = ", ".join(_COPY_COLUMNS)
    before = bind.exec_driver_sql("SELECT COUNT(*) FROM ad_metrics").scalar()
    op.execute(temp_ddl)
    op.execute(
        f"INSERT INTO _v26_ad_metrics ({columns}) "
        f"SELECT id, campaign_id, date, impressions, clicks, conversions, "
        f"{spend_expression}, revenue_usd FROM ad_metrics"
    )
    op.execute("DROP TABLE ad_metrics")
    op.execute(final_ddl)
    op.execute(f"INSERT INTO ad_metrics ({columns}) SELECT {columns} FROM _v26_ad_metrics")
    op.execute("DROP TABLE _v26_ad_metrics")
    for index_sql in _INDEXES:
        op.execute(index_sql)
    after = bind.exec_driver_sql("SELECT COUNT(*) FROM ad_metrics").scalar()
    if after != before:
        raise RuntimeError(
            f"0015 rebuild lost rows in ad_metrics: {before} before, {after} after — refusing"
        )
    print(f"[alembic 0015] {verb} ad_metrics: {after} row(s) preserved")


def upgrade() -> None:
    if _dialect_name() == "postgresql":
        # No rebuild: PostgreSQL alters a column default in place, and no generated expression
        # changes in this revision.
        op.execute("ALTER TABLE ad_metrics ALTER COLUMN spend_usd DROP DEFAULT")
        op.execute("UPDATE ad_metrics SET spend_usd = NULL WHERE spend_usd = 0")
        print("[alembic 0015] ad_metrics.spend_usd -> no default; 0-rows -> NULL")
        return
    _rebuild(_UPGRADE_TEMP_DDL, _UPGRADE_FINAL_DDL, _BACKFILL_ZERO_TO_NULL, "rebuilt")


def downgrade() -> None:
    if _dialect_name() == "postgresql":
        op.execute(f"UPDATE ad_metrics SET spend_usd = {_RESTORE_NULL_TO_ZERO}")
        op.execute("ALTER TABLE ad_metrics ALTER COLUMN spend_usd SET DEFAULT 0")
        print("[alembic 0015] ad_metrics.spend_usd -> DEFAULT 0 (NULLs restored as 0)")
        return
    _rebuild(_DOWNGRADE_TEMP_DDL, _DOWNGRADE_FINAL_DDL, _RESTORE_NULL_TO_ZERO, "reverted")
