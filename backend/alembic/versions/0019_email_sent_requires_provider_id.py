"""`email_logs.status` stops fabricating "sent" — default `draft` + the provider-acceptance CHECK

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-29

WHY (WAVE 7, email lifecycle data truth).  `email_logs.status` shipped at 0001 with
`server_default="sent"` on BOTH the column and the ORM, so a row that stated no status was
recorded as a PERFORMED SEND — no provider id, no `sent_at`, and yet `channel_service`'s email
cap counts exactly `status == "sent"` rows.  Any writer that forgot a status (a data import, a
raw INSERT, a future producer) could therefore fabricate a send, and nothing in the database
objected.  Two DDL facts close it:

* the server default becomes `'draft'` — the state that claims the least;
* PostgreSQL gains `CONSTRAINT ck_email_logs_sent_requires_provider_id CHECK (status <> 'sent' OR
  sendgrid_id IS NOT NULL)`: a `sent` row must carry the provider's own message id, which is
  acceptance evidence no local writer can invent.

The writer half (`outreach_service.record_sent_emails`) already requires a non-empty provider id;
the agent half is `services.email_lifecycle.provider_accepted` (a 2xx provider answer, or the
provider's message id).  This revision is the DDL half.

THE CHECK IS POSTGRESQL-ONLY, the repo's established convention (0002's triggers, 0017's
`invoice_dunning_claim_results` state CHECK): SQLite cannot ALTER-ADD a constraint to an existing
table, and the SQLite unit tests write `status='sent'` fixtures directly.  The PG leg is
pre-checked (a violating row refuses the migration by name rather than being silently hidden by
NOT VALID) and then validated, so the live database proves the invariant.  On SQLite the only DDL
change is the column default, applied by the proven no-rename rebuild dance 0009/0017 use (drop,
recreate from the rewritten `sqlite_master` text, copy rows back, replay every explicit index —
including 0005's partial UNIQUE `uq_email_logs_sendgrid_id` — with a row-count guard).

PG-only / environment-only, named: the CHECK is enforced on PostgreSQL (probed live inside a
rolled-back transaction, see the lane's `raw-w7/w7-sent-check-probe.txt`); on SQLite the writer's
gate and the model default are what the tests can exercise.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "email_logs"
STATUS_CHECK = "ck_email_logs_sent_requires_provider_id"

#: The old and new column definition exactly as SQLAlchemy renders a string server_default.
_OLD_STATUS_DDL = "status VARCHAR(50) DEFAULT 'sent'"
_NEW_STATUS_DDL = "status VARCHAR(50) DEFAULT 'draft'"

#: Every column of the table, in table order — the copy list of the SQLite rebuild.
_COLUMNS: tuple[str, ...] = (
    "id",
    "prospect_id",
    "campaign_id",
    "sequence_step",
    "subject",
    "body",
    "sendgrid_id",
    "status",
    "sent_at",
    "opened_at",
    "replied_at",
    "follow_up_due",
    "created_at",
)


def _dialect_name() -> str:
    """Active dialect name — works online and in `--sql` (offline) mode (0009's helper)."""
    try:
        return op.get_bind().dialect.name
    except Exception:  # noqa: BLE001 -- offline mode has no bind
        return op.get_context().dialect.name


def _sqlite_rebuild(*, to_sent: bool, verb: str) -> None:
    """0009/0017's no-rename rebuild: rewrite the status default, keep every row and index."""
    bind = op.get_bind()
    table_ddl = bind.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
    ).scalar()
    if not isinstance(table_ddl, str) or f"CREATE TABLE {TABLE}" not in table_ddl:
        raise RuntimeError(f"[alembic 0019] refusing: no CREATE TABLE text for {TABLE}")
    index_ddls = [
        row[0]
        for row in bind.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
            (TABLE,),
        ).fetchall()
        if isinstance(row[0], str)
    ]
    source, target = (_NEW_STATUS_DDL, _OLD_STATUS_DDL) if to_sent else (
        _OLD_STATUS_DDL, _NEW_STATUS_DDL
    )
    if table_ddl.count(source) != 1:
        raise RuntimeError(
            f"[alembic 0019] refusing: expected exactly ONE {source!r} in the {TABLE} DDL, "
            f"found {table_ddl.count(source)} — the head DDL is not the shape this migration "
            "was written for"
        )
    new_ddl = table_ddl.replace(source, target)
    if new_ddl.count(target) != 1:
        raise RuntimeError(f"[alembic 0019] refusing: the rewritten {TABLE} DDL lost its default")
    columns = ", ".join(_COLUMNS)
    before = bind.exec_driver_sql(f"SELECT COUNT(*) FROM {TABLE}").scalar()
    op.execute(new_ddl.replace(f"CREATE TABLE {TABLE}", f"CREATE TABLE _v19_{TABLE}"))
    op.execute(f"INSERT INTO _v19_{TABLE} ({columns}) SELECT {columns} FROM {TABLE}")
    op.execute(f"DROP TABLE {TABLE}")
    op.execute(new_ddl)
    op.execute(f"INSERT INTO {TABLE} ({columns}) SELECT {columns} FROM _v19_{TABLE}")
    op.execute(f"DROP TABLE _v19_{TABLE}")
    for index_ddl in index_ddls:
        op.execute(index_ddl)
    after = bind.exec_driver_sql(f"SELECT COUNT(*) FROM {TABLE}").scalar()
    if after != before:
        raise RuntimeError(
            f"0019 rebuild lost rows in {TABLE}: {before} before, {after} after -- refusing"
        )
    print(f"[alembic 0019] {verb}: {after} row(s) preserved")


def upgrade() -> None:
    if _dialect_name() == "postgresql":
        # Fail-closed pre-check: a legacy row that already claims `sent` without a provider id is
        # a data defect, not something this migration may hide behind NOT VALID.  Live 2026-10-29:
        # 0 such rows (the table is empty), so the constraint can be validated immediately.
        violating = op.get_bind().exec_driver_sql(
            f"SELECT COUNT(*) FROM {TABLE} WHERE status = 'sent' AND sendgrid_id IS NULL"
        ).scalar()
        if violating:
            raise RuntimeError(
                f"[alembic 0019] refusing: {violating} {TABLE} row(s) claim status='sent' with no "
                "sendgrid_id.  A performed send must carry the provider's message id, so those "
                "rows are a data defect: correct or remove them (the lane's w7-email-integrity.sql "
                "lists them), then re-run this migration."
            )
        op.execute(f"ALTER TABLE {TABLE} ALTER COLUMN status SET DEFAULT 'draft'")
        op.execute(
            f"ALTER TABLE {TABLE} ADD CONSTRAINT {STATUS_CHECK} "
            "CHECK (status <> 'sent' OR sendgrid_id IS NOT NULL)"
        )
        print(
            f"[alembic 0019] {TABLE}.status default 'sent' -> 'draft'; added {STATUS_CHECK} "
            "(a sent row must carry the provider's message id)"
        )
    else:
        _sqlite_rebuild(
            to_sent=False, verb=f"rebuilt {TABLE} (status default 'sent' -> 'draft')"
        )
        print(
            "[alembic 0019] SKIP (sqlite): the sent-requires-provider-id CHECK is PostgreSQL-only "
            "(SQLite cannot ALTER-ADD a constraint; the writer gate owns that rule on this engine)"
        )


def downgrade() -> None:
    if _dialect_name() == "postgresql":
        op.execute(f"ALTER TABLE {TABLE} DROP CONSTRAINT {STATUS_CHECK}")
        op.execute(f"ALTER TABLE {TABLE} ALTER COLUMN status SET DEFAULT 'sent'")
        print(f"[alembic 0019] dropped {STATUS_CHECK}; {TABLE}.status default back to 'sent'")
    else:
        _sqlite_rebuild(
            to_sent=True, verb=f"reverted {TABLE} (status default 'draft' -> 'sent')"
        )


__all__ = ["revision", "down_revision"]
