"""run-artifact natural keys + the CHECK constraints' promised VALIDATE (quiet revision)

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-13

Two at-rest integrity jobs in one revision; both are "quiet" (no schema rewrite beyond the
documented duplicate pre-clean, and VALIDATE takes only a SHARE UPDATE EXCLUSIVE lock):

**A. The run-artifact dedupes get the key they always assumed (L2-08).**
`social_service.record_published_posts` ("Deduped by `(platform, tweet_id)` so a retried run
cannot double-count") and `outreach_service.record_sent_emails` (the same shape on
`sendgrid_id`) were SELECT-then-INSERT with NO database key behind the claim: two overlapping
writers (Celery runs with `--concurrency=4`; a beat-driven run and a manual trigger overlap)
both miss the SELECT and both insert.  `channel_service` counts those rows for the per-channel
caps, so one real send becomes two and a legitimate send is refused with `E_RATE_LIMITED` —
a failure of the very producer-parity story these writers exist for.  Measured RED before
this revision (two sessions, one `tweet_id`): 2 rows, `channel_sends_today('twitter') == 2`.

The fix is the DB-owned invariant — a PARTIAL unique index per natural key (SQLite partial
indexes and PostgreSQL `CREATE INDEX ... WHERE` both honour UNIQUE, and in both dialects NULLs
are distinct, so legacy rows without a provider id stay legal):

* `uq_social_posts_platform_tweet_id` UNIQUE(platform, tweet_id) WHERE tweet_id IS NOT NULL
  (`tweet_id` alone is not identity — it is the provider's id, scoped by platform);
* `uq_email_logs_sendgrid_id` UNIQUE(sendgrid_id) WHERE sendgrid_id IS NOT NULL.

PRE-CLEAN FIRST (the 0002 idiom — keep `MIN(id)` per key; `DELETE` + `MIN` is portable SQL):
earlier revisions permitted exact duplicates and the index build must not fail on the rows it
is there to protect.  This is a DESTRUCTIVE step and it is documented as such: for one natural
key the OLDEST row survives and every later duplicate is dropped (it was the same logical send
recorded twice).  The rows removed are counted and printed per table for the operator's log.

**B. `VALIDATE CONSTRAINT` for 0002's NOT VALID CHECKs (M4).**
`0002_queue_and_perf.py:294` promised "VALIDATE happens in a later, quiet migration" and none
was ever written (`grep VALIDATE alembic/versions/` found only that comment).  `NOT VALID`
still enforces new writes, but PostgreSQL does not assume the constraint holds for existing
rows (the planner cannot use it) until VALIDATE.  This loop validates every still-unvalidated
CHECK constraint in the current schema; PostgreSQL documents that "Nothing happens if the
constraint is already marked valid", so it is idempotent.  PostgreSQL-only: SQLite never
created those constraints and prints the 0002-idiom skip.

A legacy row that VIOLATES a constraint makes the corresponding `VALIDATE` fail loudly, naming
the constraint.  That is the honest outcome — a violating row is data an operator must look at
(`ck_expense_records_currency` is the known candidate: `record_expense` did not lowercase the
currency), and silently rewriting production rows inside a schema migration is not this
revision's call.  Remediate the data, then re-run `alembic upgrade head`.

The duplicate pre-clean is NOT reversible by `downgrade()` (the doubles the key forbids cannot
be re-invented); dropping the two indexes restores the pre-0005 schema shape.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: (index name, table, key columns, predicate) — the two run-artifact natural keys.  The
#: predicate (and the kwargs spelling) match `app/models/{social,outreach}.py`'s `__table_args__`
#: so `create_all` and the chain describe one schema.
_NATURAL_KEYS: tuple[tuple[str, str, tuple[str, ...], str], ...] = (
    (
        "uq_social_posts_platform_tweet_id",
        "social_posts",
        ("platform", "tweet_id"),
        "tweet_id IS NOT NULL",
    ),
    (
        "uq_email_logs_sendgrid_id",
        "email_logs",
        ("sendgrid_id",),
        "sendgrid_id IS NOT NULL",
    ),
)


def _dialect_name() -> str:
    """Active dialect name — works online and in `--sql` (offline) mode (0002/0003 idiom)."""
    try:
        return op.get_bind().dialect.name
    except Exception:  # noqa: BLE001 - offline mode exposes only the context dialect
        return op.get_context().dialect.name


def _pre_clean(bind, table: str, key_columns: tuple[str, ...]) -> int:
    """Drop exact duplicates on the natural key, keeping the oldest row (`MIN(id)`).

    Portable SQL (no `USING`, no window functions): the `NOT IN (SELECT MIN(id) ...)` shape is
    0002's own duplicate resolution.  Rows with a NULL key component are exempt — the index is
    partial for exactly those.  Returns the number of rows removed (before/after count, so the
    reported number is measured rather than the driver's rowcount).
    """
    cols = ", ".join(key_columns)
    predicate = " AND ".join(f"{column} IS NOT NULL" for column in key_columns)
    before = bind.exec_driver_sql(f"SELECT COUNT(*) FROM {table}").scalar()
    bind.exec_driver_sql(
        f"DELETE FROM {table} WHERE {predicate} AND id NOT IN "
        f"(SELECT MIN(id) FROM {table} WHERE {predicate} GROUP BY {cols})"
    )
    after = bind.exec_driver_sql(f"SELECT COUNT(*) FROM {table}").scalar()
    return int(before or 0) - int(after or 0)


def upgrade() -> None:
    bind = op.get_bind()

    # ── A. the run-artifact natural keys ────────────────────────────────────
    for name, table, columns, predicate in _NATURAL_KEYS:
        removed = _pre_clean(bind, table, columns)
        print(
            f"[alembic 0005] pre-clean {table}: {removed} duplicate row(s) removed "
            f"(kept MIN(id) per {'+'.join(columns)}; the same logical send recorded twice)"
        )
        op.create_index(
            name,
            table,
            list(columns),
            unique=True,
            postgresql_where=sa.text(predicate),
            sqlite_where=sa.text(predicate),
        )
        print(
            f"[alembic 0005] created {name} "
            f"(UNIQUE {', '.join(columns)} WHERE {predicate})"
        )

    # ── B. VALIDATE the 0002 CHECK constraints (PostgreSQL-only, idempotent) ─
    if _dialect_name() != "postgresql":
        print(
            f"[alembic 0005] SKIP ({_dialect_name()}): CHECK-constraint VALIDATE — SQLite "
            "cannot carry the constraints 0002 added (PostgreSQL-only), so there is nothing "
            "to validate here."
        )
        return
    stale = bind.exec_driver_sql(
        "SELECT rel.relname, con.conname "
        "FROM pg_constraint con "
        "JOIN pg_class rel ON rel.oid = con.conrelid "
        "JOIN pg_namespace ns ON ns.oid = rel.relnamespace "
        "WHERE con.contype = 'c' AND NOT con.convalidated AND ns.nspname = current_schema() "
        "ORDER BY rel.relname, con.conname"
    ).fetchall()
    if not stale:
        print("[alembic 0005] every CHECK constraint in the current schema is already validated")
    for table, name in stale:
        op.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {name}")
        print(f"[alembic 0005] validated {name} on {table}")


def downgrade() -> None:
    """Drop exactly what `upgrade()` created; the duplicate pre-clean is not reversible."""
    for name, table, _columns, _predicate in reversed(_NATURAL_KEYS):
        op.drop_index(name, table_name=table)
    if _dialect_name() != "postgresql":
        print(
            f"[alembic 0005] SKIP ({_dialect_name()}): CHECK VALIDATE downgrade "
            "(PostgreSQL-only; nothing was validated here)"
        )
