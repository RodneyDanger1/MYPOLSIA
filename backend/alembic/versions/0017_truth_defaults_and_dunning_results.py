"""revenue_snapshots stops fabricating 0s + the append-only dunning claim result (DT-01, DT-05)

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-28

WHY (two money/audit-truth defects, one revision because both are W4's and both are additive):

DT-01 — `revenue_snapshots` shipped at 0001 with `server_default="0"` on all seven
money/count columns (`mrr_cents`, `arr_cents`, `active_subscribers`, `churned_today`,
`new_today`, `total_revenue_month_cents`, `stripe_balance_cents`).  A finance run that reported
only MRR therefore stored `arr=0, active=0, churn=0, new=0, month_revenue=0, balance=0` — an
UNMEASURED fact recorded as a MEASURED zero — and `GET /finance/summary`, the dashboard cards and
`build_cash_block.cash_on_hand` served those fabricated zeros as measurements.  The writer half
of the fix is `finance_service.upsert_revenue_snapshot` (it now writes an explicit NULL for every
column the caller did not supply) and the ORM half is `models/finance.py` (the `default=0`s are
gone).  This revision is the DDL half: drop the seven server defaults so a raw INSERT that omits
them stores NULL.  A caller-supplied 0 is still a measured zero and still stored as 0.

DT-05 — `activity_log` is append-only on PostgreSQL (0002's `trg_activity_log_no_update` refuses
every UPDATE), but the dunning ladder finalized its durable claim by UPDATE-ing that table.  On
the production engine no delivery could finalize and no refusal could be released.  This revision
adds the APPEND-ONLY result table the fixed ladder writes instead:
`invoice_dunning_claim_results` — one row per finalized `invoice_dunning_claim` claim row.

COLUMNS of `invoice_dunning_claim_results`:

    id            INTEGER PK
    claim_row_id  INTEGER NOT NULL, UNIQUE (`uq_invoice_dunning_claim_results_claim`) —
                  the `activity_log.id` of the `invoice_dunning_claim` row this result closes;
                  UNIQUE because a claim is finalized at most once (an append-only ledger with a
                  duplicated finalization would be an ambiguous fact, not a fact)
    invoice_id    INTEGER NOT NULL   — the invoice the step belongs to
    step          VARCHAR(50) NOT NULL — the ladder rung key (`pre_due`, `due`, `due_plus_5`, …)
    state         VARCHAR(20) NOT NULL — `sent` (consumes the step) or `refused` (consumes nothing)
    message_id    VARCHAR(255) NULL  — the provider's message id a delivered step carries
    code          VARCHAR(100) NULL  — the named `E_*` code a refusal carries
    detail        JSON NULL          — `superseded` rungs, `status_flipped_to`, the refusal message
    created_at    DATETIME/TIMESTAMP WITH TIME ZONE, NULL, server default now() (the ORM's
                  `created_at` is nullable; create_all and this migration describe one schema)

NO FOREIGN KEY on purpose: this is the durable ledger that must survive a retention delete of the
audit feed and an invoice-row delete, exactly like the activity ledger it replaces.
`ix_invoice_dunning_claim_results_invoice` serves the per-invoice read the ladder makes every
tick.  The state CHECK is PostgreSQL-only (0002:325-336's convention — SQLite cannot ADD
CONSTRAINT to an existing table, and the writer only ever emits the two constants).

SQLITE LEG of the DT-01 half.  SQLite cannot ALTER a column's default, so the head DDL is read
from `sqlite_master`, the seven ` DEFAULT '0'` tokens are removed, and the table is rebuilt by
0009's proven no-rename dance (create new, copy, drop, recreate, copy back).  The explicit
`ix_revenue_snapshots_date` index is captured and replayed (dropping the table drops it), and the
row count is checked before/after — a mismatch raises rather than losing a row.  `downgrade()`
performs the same dance in reverse (adding the defaults back) so the round-trip is provable on
SQLite.  The PostgreSQL legs are plain `ALTER COLUMN … DROP DEFAULT` / `SET DEFAULT 0` statements;
they are correct by construction but, per this host's rules (no PostgreSQL), they were NOT
executed here — PG-ONLY/UNVERIFIED, named in the receipt.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "revenue_snapshots"
RESULT_TABLE = "invoice_dunning_claim_results"
RESULT_INDEX = "ix_invoice_dunning_claim_results_invoice"
RESULT_STATE_CHECK = "ck_invoice_dunning_claim_results_state"
RESULT_UNIQUE = "uq_invoice_dunning_claim_results_claim"

#: The seven columns that carried a fabricated `DEFAULT 0` (and nothing else changes).
_MONEY_COLUMNS: tuple[str, ...] = (
    "mrr_cents",
    "arr_cents",
    "active_subscribers",
    "churned_today",
    "new_today",
    "total_revenue_month_cents",
    "stripe_balance_cents",
)
#: Every column of the head table, in table order — the copy list of the rebuild.
_COLUMNS: tuple[str, ...] = ("id", "snapshot_date", *_MONEY_COLUMNS, "created_at")


def _dialect_name() -> str:
    """Active dialect name — works online and in `--sql` (offline) mode (0009's helper)."""
    try:
        return op.get_bind().dialect.name
    except Exception:  # noqa: BLE001 -- offline mode has no bind
        return op.get_context().dialect.name


def _rewrite_defaults(ddl: str, *, add: bool) -> str:
    """Add or remove the seven ` DEFAULT '0'` tokens, line by line, and prove the count.

    A line whose column definition does not carry the expected token is refused BY NAME: a
    silent partial rewrite would ship a table with some defaults dropped and some not.
    """
    lines: list[str] = []
    for line in ddl.splitlines():
        for column in _MONEY_COLUMNS:
            bare = f"{column} INTEGER"
            with_default = f"{column} INTEGER DEFAULT '0'"
            if add:
                if f"{bare}," in line and with_default not in line:
                    line = line.replace(f"{bare},", f"{with_default},")
            elif with_default in line:
                line = line.replace(with_default, bare)
        lines.append(line)
    rewritten = "\n".join(lines)
    for column in _MONEY_COLUMNS:
        expected = int(add)
        found = rewritten.count(f"{column} INTEGER DEFAULT '0'")
        if found != expected:
            raise RuntimeError(
                f"[alembic 0017] refusing: {column} carries {found} DEFAULT '0' token(s), "
                f"expected {expected} (add={add}); the head DDL is not the shape this migration "
                f"was written for"
            )
    return rewritten


def _sqlite_rebuild(*, add_defaults: bool, verb: str) -> None:
    """0009's explicit no-rename dance, for `revenue_snapshots` (its index is replayed)."""
    bind = op.get_bind()
    table_ddl = bind.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
    ).scalar()
    if not isinstance(table_ddl, str) or f"CREATE TABLE {TABLE}" not in table_ddl:
        raise RuntimeError(f"[alembic 0017] refusing: no CREATE TABLE text for {TABLE}")
    index_ddls = [
        row[0]
        for row in bind.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
            (TABLE,),
        ).fetchall()
        if isinstance(row[0], str)
    ]
    new_ddl = _rewrite_defaults(table_ddl, add=add_defaults)
    columns = ", ".join(_COLUMNS)
    before = bind.exec_driver_sql(f"SELECT COUNT(*) FROM {TABLE}").scalar()
    op.execute(new_ddl.replace(f"CREATE TABLE {TABLE}", f"CREATE TABLE _v17_{TABLE}"))
    op.execute(f"INSERT INTO _v17_{TABLE} ({columns}) SELECT {columns} FROM {TABLE}")
    op.execute(f"DROP TABLE {TABLE}")
    op.execute(new_ddl)
    op.execute(f"INSERT INTO {TABLE} ({columns}) SELECT {columns} FROM _v17_{TABLE}")
    op.execute(f"DROP TABLE _v17_{TABLE}")
    for index_ddl in index_ddls:
        op.execute(index_ddl)
    after = bind.exec_driver_sql(f"SELECT COUNT(*) FROM {TABLE}").scalar()
    if after != before:
        raise RuntimeError(
            f"0017 rebuild lost rows in {TABLE}: {before} before, {after} after -- refusing"
        )
    print(f"[alembic 0017] {verb}: {after} row(s) preserved")


def _create_result_table() -> None:
    # The literal table name is deliberate: `tests/parity/test_contract_parity.py`'s table census
    # statically reads `op.create_table("<name>"`, so a constant here would hide this revision
    # from the ORM-vs-migration parity guard (the index/drop/ALTER statements below still use
    # RESULT_TABLE).  Runtime DDL is unchanged.
    op.create_table(
        "invoice_dunning_claim_results",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("claim_row_id", sa.Integer(), nullable=False),
        sa.Column("invoice_id", sa.Integer(), nullable=False),
        sa.Column("step", sa.String(length=50), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("message_id", sa.String(length=255), nullable=True),
        sa.Column("code", sa.String(length=100), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            # Match the ORM (`InvoiceDunningClaimResult.created_at: Mapped[datetime | None]`):
            # create_all and this migration must describe ONE schema (F3 integration audit found
            # the pre-fix NOT NULL here would make the production table stricter than the ORM).
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("claim_row_id", name=RESULT_UNIQUE),
    )
    op.create_index(RESULT_INDEX, RESULT_TABLE, ["invoice_id"], unique=False)
    if _dialect_name() == "postgresql":
        # 0002:325-336's convention: PG-only CHECK, NOT VALID (no table scan; enforced for writes).
        op.execute(
            f"ALTER TABLE {RESULT_TABLE} ADD CONSTRAINT {RESULT_STATE_CHECK} "
            "CHECK (state IN ('sent', 'refused')) NOT VALID"
        )
    else:
        print(
            "[alembic 0017] SKIP (sqlite): the state CHECK is PostgreSQL-only "
            "(SQLite cannot ALTER-ADD a constraint; the ladder only writes the two constants)"
        )
    print(
        f"[alembic 0017] created {RESULT_TABLE} (claim_row_id UNIQUE; state sent|refused; "
        f"{RESULT_INDEX})"
    )


def upgrade() -> None:
    if _dialect_name() == "postgresql":
        for column in _MONEY_COLUMNS:
            op.execute(f"ALTER TABLE {TABLE} ALTER COLUMN {column} DROP DEFAULT")
        print(f"[alembic 0017] dropped the 7 fabricated defaults on {TABLE} (PostgreSQL)")
    else:
        _sqlite_rebuild(
            add_defaults=False, verb=f"rebuilt {TABLE} (7 server defaults dropped)"
        )
    _create_result_table()


def downgrade() -> None:
    op.drop_index(RESULT_INDEX, table_name=RESULT_TABLE)
    op.drop_table(RESULT_TABLE)
    if _dialect_name() == "postgresql":
        for column in _MONEY_COLUMNS:
            op.execute(f"ALTER TABLE {TABLE} ALTER COLUMN {column} SET DEFAULT 0")
        print(f"[alembic 0017] restored the 7 defaults on {TABLE} (PostgreSQL)")
    else:
        _sqlite_rebuild(
            add_defaults=True, verb=f"reverted {TABLE} (7 server defaults restored)"
        )
