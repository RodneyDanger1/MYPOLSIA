"""invoices.tax loses the fabricated server default 0 (S1-04)

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-15

`invoices.tax` shipped at 0008 with `server_default="0"` AND the ORM `default=0`, so an
INSERT that omitted `tax` (or sent `None`) recorded a MEASURED zero for an unmeasured tax —
the null≠0 breach the model docstring (`app/models/invoice.py`) and `create_invoice`'s
docstring (`app/services/invoice_service.py`) both refuse (wave-v13/s1-backend FINDINGS
S1-04: every invoice without a tax showed and rendered `$0.00` as if measured, and the
shipped pin never asserted the field).  The ORM half is removed in the model; this revision
removes the DDL half.

SQLite: the explicit no-rename rebuild (0003's measured pattern — `batch_alter_table` fails
on this stack: alembic 1.14 / SQLAlchemy 2.0.36 / aiosqlite).  The DDL texts are byte-for-byte
the 0008-head `sqlite_master` text for `invoices` (lane dump kept at
`wave-v14/v1-money/raw/head_invoices_ddl.txt`); only the `tax` token changes:
`tax NUMERIC(12, 2) DEFAULT '0'` -> `tax NUMERIC(12, 2)`.  Row counts are checked
before/after; a mismatch raises.

PostgreSQL: a straight `ALTER TABLE invoices ALTER COLUMN tax DROP DEFAULT` (`downgrade()`
sets it back).  The PG leg is correct by construction but was NOT executed in this lane (no
Postgres per the machine rules) — the SQLite leg is the one with a live test
(`tests/unit/test_v14_money_fixes.py::test_migration_head_ddl_does_not_fabricate_tax`).
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: 0008-head `sqlite_master` text for `invoices` (measured; tab-indented) — byte-faithful.
_OLD_DDL: str = "CREATE TABLE invoices (\n\tid INTEGER NOT NULL, \n\tnumber TEXT NOT NULL, \n\tbill_to JSON, \n\tissue_date DATE NOT NULL, \n\tdue_date DATE NOT NULL, \n\tnet_days INTEGER DEFAULT '30' NOT NULL, \n\tcurrency TEXT DEFAULT 'USD' NOT NULL, \n\tsubtotal NUMERIC(12, 2), \n\ttax NUMERIC(12, 2) DEFAULT '0', \n\ttotal NUMERIC(12, 2), \n\tstatus VARCHAR(50) NOT NULL, \n\tpayment_link TEXT, \n\tpaid_at DATETIME, \n\tlate_fee_note TEXT, \n\tline_items JSON, \n\tPRIMARY KEY (id), \n\tUNIQUE (number), \n\tCONSTRAINT ck_invoices_status CHECK (status IN ('draft', 'sent', 'paid', 'void', 'overdue'))\n)"
#: the same text with the fabricated default dropped — the only token that changes.
_NEW_DDL: str = "CREATE TABLE invoices (\n\tid INTEGER NOT NULL, \n\tnumber TEXT NOT NULL, \n\tbill_to JSON, \n\tissue_date DATE NOT NULL, \n\tdue_date DATE NOT NULL, \n\tnet_days INTEGER DEFAULT '30' NOT NULL, \n\tcurrency TEXT DEFAULT 'USD' NOT NULL, \n\tsubtotal NUMERIC(12, 2), \n\ttax NUMERIC(12, 2), \n\ttotal NUMERIC(12, 2), \n\tstatus VARCHAR(50) NOT NULL, \n\tpayment_link TEXT, \n\tpaid_at DATETIME, \n\tlate_fee_note TEXT, \n\tline_items JSON, \n\tPRIMARY KEY (id), \n\tUNIQUE (number), \n\tCONSTRAINT ck_invoices_status CHECK (status IN ('draft', 'sent', 'paid', 'void', 'overdue'))\n)"

_COLUMNS: tuple[str, ...] = (
    "id", "number", "bill_to", "issue_date", "due_date", "net_days", "currency",
    "subtotal", "tax", "total", "status", "payment_link", "paid_at", "late_fee_note",
    "line_items",
)


def _dialect_name() -> str:
    """Active dialect name -- works online and in `--sql` (offline) mode."""
    try:
        return op.get_bind().dialect.name
    except Exception:  # noqa: BLE001 -- offline mode has no bind
        return op.get_context().dialect.name


def _rebuild(ddl: str, verb: str) -> None:
    """0003's explicit no-rename dance, for the single `invoices` table."""
    bind = op.get_bind()
    columns = ", ".join(_COLUMNS)
    before = bind.exec_driver_sql("SELECT COUNT(*) FROM invoices").scalar()
    op.execute(ddl.replace("CREATE TABLE invoices", "CREATE TABLE _v14_invoices"))
    op.execute(f"INSERT INTO _v14_invoices ({columns}) SELECT {columns} FROM invoices")
    op.execute("DROP TABLE invoices")
    op.execute(ddl)
    op.execute(f"INSERT INTO invoices ({columns}) SELECT {columns} FROM _v14_invoices")
    op.execute("DROP TABLE _v14_invoices")
    after = bind.exec_driver_sql("SELECT COUNT(*) FROM invoices").scalar()
    if after != before:
        raise RuntimeError(
            f"0009 rebuild lost rows in invoices: {before} before, {after} after -- refusing"
        )
    print(f"[alembic 0009] {verb} invoices.tax: {after} row(s) preserved")


def upgrade() -> None:
    if _dialect_name() == "postgresql":
        op.execute("ALTER TABLE invoices ALTER COLUMN tax DROP DEFAULT")
        print("[alembic 0009] dropped invoices.tax default (PostgreSQL)")
        return
    _rebuild(_NEW_DDL, "rebuilt (server default dropped)")


def downgrade() -> None:
    if _dialect_name() == "postgresql":
        op.execute("ALTER TABLE invoices ALTER COLUMN tax SET DEFAULT 0")
        print("[alembic 0009] restored invoices.tax default (PostgreSQL)")
        return
    _rebuild(_OLD_DDL, "reverted (server default restored)")
