"""ads revenue is MEASURED or ABSENT — nullable revenue_usd, no default (V25/L4)

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-19

WHY
---
`ad_metrics.revenue_usd` shipped `NUMERIC(14, 4) DEFAULT '0' NOT NULL` (0002:203-207, restated by
0003's rebuild).  That DDL cannot represent the one distinction the ads surface exists to tell:
an UNMEASURED revenue and a MEASURED zero are the same stored byte.  V24/L4 therefore had to
serve every stored `0` as `null` (documented deviation #1 of that lane's receipt), which also
erased genuine `$0.00` days.  This revision gives the column the state it needs:

* `revenue_usd` — NULLABLE, and NO server default.  The writer (`ads_service.record_ads_run`)
  persists NULL when the run reported no revenue; a reported `0` stays `0`.
* Backfill: every EXISTING `revenue_usd = 0` row becomes NULL.  A stored zero written by the old
  DEFAULT was never certified as a measurement (nothing in the tree distinguished the two), so the
  honest migration of those rows is "unknown", not "$0 measured".  This is irreversible in the
  data-sense — see the downgrade note.
* `roas` — the GENERATED expression no longer fabricates a ratio: it yields NULL unless revenue is
  measured (NOT NULL) AND spend is a usable denominator.  Before: `ELSE 0`, so a zero-spend row
  stored a real-looking `0×` from inputs it never had (and a NULL revenue would have stored 0 in
  the `ELSE` branch as well).  The API (`api/v1/ads.py::_metric_response`) already computes ROAS
  from the measured pair; this makes the stored column agree with that law instead of contradicting it.

`spend_usd` is NOT touched: the DDL already declares it nullable (`NUMERIC(14, 4) DEFAULT '0'`,
0002/0003).  The brief's condition ("add a nullable spend_usd only if the DDL is not already
nullable") is not met.  Residual, stated rather than fixed here: the column still carries a
server-side `DEFAULT '0'`, so a writer that OMITS spend at rest gets a stored zero.  The ads
writer avoids that by omitting nothing silently only for revenue (the lane's scope); spend's
default is left as-is by this migration.

DOWNGRADE
---------
Restores `NOT NULL DEFAULT 0` after `COALESCE(revenue_usd, 0)` — the pre-0014 shape, exactly.
The information "this row was never measured" is NOT recoverable after the downgrade; the rows
that were NULL become stored zeros again and will read as measured zeros on a pre-0014 reader.
That asymmetry is inherent to the shape, and is why the receipt calls it out.

SQLite cannot ALTER a column's nullability or default, so — following `0003_money_exact`'s
measured no-rename dance — the table is rebuilt through a `_v25_ad_metrics` temp table with the
FINAL text re-created under the real name (byte-faithful stored CREATE), or, for PostgreSQL, by
plain `ALTER COLUMN` (no rebuild; the generated `roas` column is dropped and re-added in both
engines because a generation expression cannot be altered in place).

Single head: this revision's `down_revision` is "0013" (`0013_task_attachments`, V25/L1).  It is
deliberately NOT "0012": the wave runs L1 first, and the two migrations are one chain.
"""
from __future__ import annotations

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | None = None
depends_on: str | None = None

# The ratio expressions are copied VERBATIM from the 0012 head shape (0002:238-245 / 0003:71),
# except `roas`'s truth rule.  `round(CAST(CAST(x AS REAL) / y AS NUMERIC), 6)` is the form both
# engines accept (PostgreSQL has no round(double precision, int); SQLite needs the REAL division).
_OLD_ROAS_EXPR = (
    "CASE WHEN spend_usd > 0 THEN round(CAST(CAST(revenue_usd AS REAL) / CAST(spend_usd AS REAL) "
    "AS NUMERIC), 6) ELSE 0 END"
)
_NEW_ROAS_EXPR = (
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
#: (0003 sets the same precedent); revenue is copied through an expression by the caller so the
#: backfill is explicit rather than a `SELECT *`.
_COPY_COLUMNS: tuple[str, ...] = (
    "id", "campaign_id", "date", "impressions", "clicks", "conversions", "spend_usd", "revenue_usd",
)

_INDEXES: tuple[str, ...] = (
    "CREATE INDEX ix_ad_metrics_date_campaign ON ad_metrics (date, campaign_id)",
    "CREATE UNIQUE INDEX uq_ad_metrics_campaign_date ON ad_metrics (campaign_id, date)",
)


def _ad_metrics_ddl(table: str, *, revenue: str, roas_expr: str) -> str:
    """One measured CREATE TABLE text for `ad_metrics`.

    `revenue` is the column's own definition tail (`"NUMERIC(14, 4)"` for the nullable shape,
    `"NUMERIC(14, 4) DEFAULT '0' NOT NULL"` for the pre-0014 shape) — the single difference the
    two legs of this migration carry.  Everything else is the measured 0012/0003 text.
    """
    return (
        f"CREATE TABLE {table} (\n"
        "\tid INTEGER NOT NULL, \n"
        "\tcampaign_id INTEGER NOT NULL, \n"
        "\tdate DATE NOT NULL, \n"
        "\timpressions INTEGER DEFAULT '0', \n"
        "\tclicks INTEGER DEFAULT '0', \n"
        "\tconversions INTEGER DEFAULT '0', \n"
        "\tspend_usd NUMERIC(14, 4) DEFAULT '0', \n"
        f"\trevenue_usd {revenue}, ctr NUMERIC(14, 6) GENERATED ALWAYS AS ({_CTR_EXPR}) VIRTUAL, "
        f"cpc NUMERIC(14, 6) GENERATED ALWAYS AS ({_CPC_EXPR}) VIRTUAL, "
        f"roas NUMERIC(14, 6) GENERATED ALWAYS AS ({roas_expr}) VIRTUAL, \n"
        "\tPRIMARY KEY (id), \n"
        "\tFOREIGN KEY(campaign_id) REFERENCES ad_campaigns (id)\n"
        ")"
    )


#: SQLite: the nullable (upgrade) and pre-0014 (downgrade) shapes, temp + final.
_UPGRADE_TEMP_DDL = _ad_metrics_ddl("_v25_ad_metrics", revenue="NUMERIC(14, 4)", roas_expr=_NEW_ROAS_EXPR)
_UPGRADE_FINAL_DDL = _ad_metrics_ddl("ad_metrics", revenue="NUMERIC(14, 4)", roas_expr=_NEW_ROAS_EXPR)
_DOWNGRADE_TEMP_DDL = _ad_metrics_ddl(
    "_v25_ad_metrics", revenue="NUMERIC(14, 4) DEFAULT '0' NOT NULL", roas_expr=_OLD_ROAS_EXPR
)
_DOWNGRADE_FINAL_DDL = _ad_metrics_ddl(
    "ad_metrics", revenue="NUMERIC(14, 4) DEFAULT '0' NOT NULL", roas_expr=_OLD_ROAS_EXPR
)

#: The backfill / restore expressions.  `revenue_usd = 0` matches every NUMERIC spelling of zero
#: (0, 0.0, 0.0000) in both engines; NaN/Infinity cannot reach this NUMERIC(14,4) column (0002's
#: money pre-flight refuses them at the type change, and the column type rejects them since).
_BACKFILL_ZERO_TO_NULL = "CASE WHEN revenue_usd = 0 THEN NULL ELSE revenue_usd END"
_RESTORE_NULL_TO_ZERO = "COALESCE(revenue_usd, 0)"


def _dialect_name() -> str:
    """Active dialect, online or in `--sql` (offline) mode — the 0003 helper, unchanged."""
    try:
        return op.get_bind().dialect.name
    except Exception:  # noqa: BLE001 -- offline mode has no bind
        return op.get_context().dialect.name


def _rebuild(temp_ddl: str, final_ddl: str, revenue_expression: str, verb: str) -> None:
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
        f"INSERT INTO _v25_ad_metrics ({columns}) "
        f"SELECT id, campaign_id, date, impressions, clicks, conversions, spend_usd, "
        f"{revenue_expression} FROM ad_metrics"
    )
    op.execute("DROP TABLE ad_metrics")
    op.execute(final_ddl)
    op.execute(f"INSERT INTO ad_metrics ({columns}) SELECT {columns} FROM _v25_ad_metrics")
    op.execute("DROP TABLE _v25_ad_metrics")
    for index_sql in _INDEXES:
        op.execute(index_sql)
    after = bind.exec_driver_sql("SELECT COUNT(*) FROM ad_metrics").scalar()
    if after != before:
        raise RuntimeError(
            f"0014 rebuild lost rows in ad_metrics: {before} before, {after} after — refusing"
        )
    print(f"[alembic 0014] {verb} ad_metrics: {after} row(s) preserved")


def _replace_roas_generated_column(expression: str) -> None:
    """PostgreSQL cannot alter a generation expression: drop and re-add the STORED column.

    The values are recomputed by the engine from the copied inputs; nothing is written by hand.
    """
    op.execute("ALTER TABLE ad_metrics DROP COLUMN roas")
    op.execute(
        "ALTER TABLE ad_metrics ADD COLUMN roas NUMERIC(14, 6) "
        f"GENERATED ALWAYS AS ({expression}) STORED"
    )


def upgrade() -> None:
    if _dialect_name() == "postgresql":
        # No rebuild: PostgreSQL supports both column-attribute changes and the generated-column
        # swap directly.  Order matters only for clarity — the UPDATE is legal either way.
        op.execute("ALTER TABLE ad_metrics ALTER COLUMN revenue_usd DROP DEFAULT")
        op.execute("ALTER TABLE ad_metrics ALTER COLUMN revenue_usd DROP NOT NULL")
        op.execute("UPDATE ad_metrics SET revenue_usd = NULL WHERE revenue_usd = 0")
        _replace_roas_generated_column(_NEW_ROAS_EXPR)
        print("[alembic 0014] ad_metrics.revenue_usd -> nullable, no default; 0-rows -> NULL")
        return
    _rebuild(_UPGRADE_TEMP_DDL, _UPGRADE_FINAL_DDL, _BACKFILL_ZERO_TO_NULL, "rebuilt")


def downgrade() -> None:
    if _dialect_name() == "postgresql":
        op.execute(f"UPDATE ad_metrics SET revenue_usd = {_RESTORE_NULL_TO_ZERO}")
        op.execute("ALTER TABLE ad_metrics ALTER COLUMN revenue_usd SET DEFAULT 0")
        op.execute("ALTER TABLE ad_metrics ALTER COLUMN revenue_usd SET NOT NULL")
        _replace_roas_generated_column(_OLD_ROAS_EXPR)
        print("[alembic 0014] ad_metrics.revenue_usd -> NOT NULL DEFAULT 0 (NULLs restored as 0)")
        return
    _rebuild(_DOWNGRADE_TEMP_DDL, _DOWNGRADE_FINAL_DDL, _RESTORE_NULL_TO_ZERO, "reverted")
