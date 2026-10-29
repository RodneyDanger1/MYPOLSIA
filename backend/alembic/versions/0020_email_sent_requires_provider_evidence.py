"""`email_logs`: a `sent` row must carry a NON-BLANK provider id AND the instant it was recorded

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-29

WHY (WAVE 8, data truth under failure — the 0019 enforcement was bypassable).  0019 shipped
`CONSTRAINT ck_email_logs_sent_requires_provider_id CHECK (status <> 'sent' OR sendgrid_id IS NOT
NULL)`.  Two direct-SQL probes on the live database (inside ROLLBACKed transactions, 2026-10-29)
both **inserted**:

    INSERT INTO email_logs (prospect_id, subject, sendgrid_id, status, sent_at)
      VALUES (<id>, 'w8-probe-empty-provider-id', '', 'sent', now());      -- INSERT 0 1
    INSERT INTO email_logs (prospect_id, subject, sendgrid_id, status, sent_at)
      VALUES (<id>, 'w8-probe-no-sent-at', 'sg-w8-probe', 'sent', NULL);   -- INSERT 0 1

* an EMPTY STRING is not NULL, so `sendgrid_id=''` satisfied "has a provider id" while carrying
  no id at all — the exact fabricated send 0019 exists to refuse, one keystroke away;
* nothing required `sent_at`, so a `sent` row could claim a performed send with no recording
  instant — invisible to the email cap, which windows on `sent_at` (`channel_service`), and
  unreadable as history.

This revision strengthens the SAME constraint (same name, so every 0019 pin and probe keeps its
identity) to:

    CHECK (status <> 'sent' OR (
        sendgrid_id IS NOT NULL AND trim(sendgrid_id) <> '' AND sent_at IS NOT NULL))

`trim()` — not PostgreSQL's `btrim()` — because the same rule is rendered by `create_all` on the
SQLite test engine (`app/models/outreach.py`), and the two must say the same thing.  `IS NOT NULL`
is load-bearing on BOTH engines: a NULL never fails a CHECK, so `trim(NULL) <> ''` alone would
pass the row.

WHAT THE DATABASE CAN AND CANNOT PROVE.  It can prove the SHAPE of a send — a non-blank provider
id and a recording instant.  It cannot prove the id came from a provider; that is the writer's
answer (`services.email_lifecycle.provider_accepted` + `outreach_service.record_sent_emails`
require the provider's own 2xx answer or message id).  A database superuser can still write a
plausible-looking lie; the app-level acceptance rule is the authority on provenance, and this
revision closes the shape gap that let an ordinary direct INSERT fabricate a send.

REVERSIBLE.  `downgrade()` restores 0019's predicate exactly (drop -> add the weaker CHECK ->
`sent_at` no longer required).  New rows satisfying the stronger rule always satisfy the weaker
one, so the downgrade cannot be refused by data written under this revision.  The Python/SQLite
leg is the repo's proven no-rename rebuild (0009/0017/0019): SQLite cannot ALTER-ADD a constraint,
so the table is recreated from its own `sqlite_master` text with the constraint inserted before
the closing paren, every row copied, every explicit index replayed, and a row-count guard that
refuses a lost row.  Downgrade removes the constraint from that text by paren matching (a regex
would stop at the CHECK's first nested paren).

OFFLINE (`--sql`).  The PostgreSQL leg renders both ALTERs with no connection; the legacy-row
pre-check is skipped there BY NAME because offline mode cannot read data (the operator runs the
rendered SQL online, where the same pre-check runs before the ALTER).  This was a measured defect
in the first draft: `alembic upgrade 0019:0020 --sql` died with
`AttributeError: 'MockConnection' object has no attribute 'exec_driver_sql'`.  The SQLite leg
refuses by name offline — it must read the live `sqlite_master` text and copy rows, which a
`--sql` render cannot do.  Pinned by
`test_w8_failure_mode_truth.py::test_the_evidence_rule_renders_as_offline_sql` and its downgrade
twin.

PG-only / environment-only, named: the CHECK is enforced on PostgreSQL and validated immediately
(the pre-check below refuses a violating store by name instead of hiding it behind NOT VALID).
On SQLite the migration leg is exercisable (the rebuild asserts the constraint text is present and
the insert probes are pinned in `tests/unit/test_w8_failure_mode_truth.py`), while `create_all`
carries the same rule through the ORM.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "email_logs"
STATUS_CHECK = "ck_email_logs_sent_requires_provider_id"

#: 0019's predicate, restored by `downgrade()` — the constraint body only (no CHECK keyword).
_OLD_BODY = "status <> 'sent' OR sendgrid_id IS NOT NULL"
#: This revision's predicate, installed by `upgrade()`.
_NEW_BODY = (
    "status <> 'sent' OR ("
    "sendgrid_id IS NOT NULL AND trim(sendgrid_id) <> '' AND sent_at IS NOT NULL)"
)

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


def _offline() -> bool:
    """True under `alembic upgrade/downgrade ... --sql`: no connection exists.

    WAVE 8: the first draft of this revision queried the store for its pre-check on EVERY
    dialect='postgresql' run — and in `--sql` mode `op.get_bind()` is a MockConnection with no
    `exec_driver_sql`, so `alembic upgrade 0019:0020 --sql` died with
    `AttributeError: 'MockConnection' object has no attribute 'exec_driver_sql'` (measured).
    Offline mode is for rendering the DDL, not for reading data: the pre-check is skipped BY
    NAME there and the same ALTER statements are emitted, so the operator runs them online and
    the check still runs before the constraint is added.  The SQLite leg cannot be rendered at
    all offline (it rebuilds from the live `sqlite_master` text) and refuses by name.
    """
    return op.get_context().as_sql


def _sqlite_constraint_clause() -> str:
    """The inline constraint as SQLite renders it: `, CONSTRAINT <name> CHECK (<body>)`."""
    return f", CONSTRAINT {STATUS_CHECK} CHECK ({_NEW_BODY})"


def _insert_constraint(ddl: str) -> str:
    """Insert the CHECK as the LAST table-level clause of a `CREATE TABLE email_logs (...)` DDL.

    The rebuild's whole point is to hand SQLite a table definition that carries the constraint;
    that means editing its own `sqlite_master` text (SQLite has no ALTER-ADD for constraints).
    The insertion point is the final closing paren — the table DDL has exactly one trailing `)`.
    Refuses by name when the text is not the shape this migration was written for.
    """
    stripped = ddl.rstrip()
    if not stripped.endswith(")"):
        raise RuntimeError(
            f"[alembic 0020] refusing: the {TABLE} DDL does not end with ')': {stripped[-80:]!r}"
        )
    clause = _sqlite_constraint_clause()
    if STATUS_CHECK in stripped:
        raise RuntimeError(
            f"[alembic 0020] refusing: {STATUS_CHECK} is already present in the {TABLE} DDL"
        )
    # Insert BEFORE the final paren; the preceding character is the last column/FK clause.  A
    # comma is always legal there because a CREATE TABLE body has at least one clause (id).
    head = stripped[:-1].rstrip()
    if not head.endswith(","):
        head = head + ","
    return head + "\n\t" + clause.lstrip(", ") + "\n)"


def _remove_constraint(ddl: str) -> str:
    """Remove the `, CONSTRAINT <name> CHECK (...)` clause from a table DDL by paren matching.

    A regex cannot do this: the CHECK body contains its own parenthesised groups.  Walk from the
    opening paren after `CHECK` with a depth counter to its matching close, then swallow the
    separating comma and whitespace.
    """
    marker = f"CONSTRAINT {STATUS_CHECK} CHECK ("
    start = ddl.find(marker)
    if start < 0:
        raise RuntimeError(
            f"[alembic 0020] refusing: {STATUS_CHECK} is absent from the {TABLE} DDL, so the "
            "downgrade cannot restore 0019's predicate"
        )
    depth = 0
    close = -1
    for index in range(start + len(marker) - 1, len(ddl)):
        char = ddl[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                close = index
                break
    if close < 0:
        raise RuntimeError(
            f"[alembic 0020] refusing: unbalanced {STATUS_CHECK} CHECK body in the {TABLE} DDL"
        )
    end = close + 1
    # swallow the separating comma BEFORE the clause (and any indentation) ...
    prefix_start = start
    while prefix_start > 0 and ddl[prefix_start - 1] in " \t\r\n":
        prefix_start -= 1
    if prefix_start > 0 and ddl[prefix_start - 1] == ",":
        prefix_start -= 1
    elif end < len(ddl) and ddl[end] == ",":
        # ... or the one AFTER it, when the clause was the first/last without a leading comma
        end += 1
    return (ddl[:prefix_start] + ddl[end:]).rstrip()


def _sqlite_rebuild(*, add: bool, verb: str) -> None:
    """0009/0017/0019's no-rename rebuild, carrying/removing the strengthened CHECK."""
    if _offline():
        # Reading the table's own DDL and copying its rows is impossible without a connection.
        # Refuse by NAME instead of the AttributeError a MockConnection produces (WAVE 8).
        raise RuntimeError(
            f"[alembic 0020] refusing: the SQLite leg of this revision rebuilds {TABLE} from its "
            "own sqlite_master text and cannot run in --sql (offline) mode; run the migration "
            "online against the database"
        )
    bind = op.get_bind()
    table_ddl = bind.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
    ).scalar()
    if not isinstance(table_ddl, str) or f"CREATE TABLE {TABLE}" not in table_ddl:
        raise RuntimeError(f"[alembic 0020] refusing: no CREATE TABLE text for {TABLE}")
    index_ddls = [
        row[0]
        for row in bind.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
            (TABLE,),
        ).fetchall()
        if isinstance(row[0], str)
    ]
    new_ddl = _insert_constraint(table_ddl) if add else _remove_constraint(table_ddl)
    if add and STATUS_CHECK not in new_ddl:
        raise RuntimeError(f"[alembic 0020] refusing: the rewritten {TABLE} DDL lost the CHECK")
    if not add and STATUS_CHECK in new_ddl:
        raise RuntimeError(f"[alembic 0020] refusing: the rewritten {TABLE} DDL kept the CHECK")
    columns = ", ".join(_COLUMNS)
    before = bind.exec_driver_sql(f"SELECT COUNT(*) FROM {TABLE}").scalar()
    op.execute(new_ddl.replace(f"CREATE TABLE {TABLE}", f"CREATE TABLE _v20_{TABLE}"))
    op.execute(f"INSERT INTO _v20_{TABLE} ({columns}) SELECT {columns} FROM {TABLE}")
    op.execute(f"DROP TABLE {TABLE}")
    op.execute(new_ddl)
    op.execute(f"INSERT INTO {TABLE} ({columns}) SELECT {columns} FROM _v20_{TABLE}")
    op.execute(f"DROP TABLE _v20_{TABLE}")
    for index_ddl in index_ddls:
        op.execute(index_ddl)
    after = bind.exec_driver_sql(f"SELECT COUNT(*) FROM {TABLE}").scalar()
    if after != before:
        raise RuntimeError(
            f"0020 rebuild lost rows in {TABLE}: {before} before, {after} after -- refusing"
        )
    print(f"[alembic 0020] {verb}: {after} row(s) preserved")


def upgrade() -> None:
    if _dialect_name() == "postgresql":
        # Fail-closed pre-check: a legacy row that already claims `sent` with a blank provider id
        # or no `sent_at` is a data defect, not something this migration may hide behind NOT VALID.
        # Live 2026-10-29: 0 such rows (the table is empty).  `--sql` mode has no connection and
        # therefore cannot read the store: the check is skipped there and the ALTER is still
        # rendered, so running the output online keeps the same fail-closed order.
        if _offline():
            print(
                "[alembic 0020] --sql mode: the legacy-row pre-check cannot run without a "
                "connection; it runs before the ALTER when this SQL is applied online"
            )
        else:
            violating = op.get_bind().exec_driver_sql(
                f"SELECT COUNT(*) FROM {TABLE} WHERE status = 'sent' AND ("
                "sendgrid_id IS NULL OR trim(sendgrid_id) = '' OR sent_at IS NULL)"
            ).scalar()
            if violating:
                raise RuntimeError(
                    f"[alembic 0020] refusing: {violating} {TABLE} row(s) claim status='sent' with a "
                    "blank/absent sendgrid_id or no sent_at.  A performed send must carry the "
                    "provider's non-blank message id AND the instant it was recorded, so those rows "
                    "are a data defect: correct or remove them (raw-w8/w8-failure-mode-audit.sql "
                    "lists them), then re-run this migration."
                )
        op.execute(f"ALTER TABLE {TABLE} DROP CONSTRAINT IF EXISTS {STATUS_CHECK}")
        op.execute(
            f"ALTER TABLE {TABLE} ADD CONSTRAINT {STATUS_CHECK} CHECK ({_NEW_BODY})"
        )
        print(
            f"[alembic 0020] {STATUS_CHECK} strengthened: a sent row now carries a NON-BLANK "
            "sendgrid_id AND a sent_at (0019 accepted '' and NULL sent_at)"
        )
    else:
        _sqlite_rebuild(
            add=True, verb=f"rebuilt {TABLE} (sent requires a non-blank id AND sent_at)"
        )
        print(
            "[alembic 0020] SQLite rebuild carried the CHECK into the table definition "
            "(create_all renders the same rule through app/models/outreach.py)"
        )


def downgrade() -> None:
    if _dialect_name() == "postgresql":
        op.execute(f"ALTER TABLE {TABLE} DROP CONSTRAINT IF EXISTS {STATUS_CHECK}")
        op.execute(
            f"ALTER TABLE {TABLE} ADD CONSTRAINT {STATUS_CHECK} CHECK ({_OLD_BODY})"
        )
        print(
            f"[alembic 0020] downgraded: {STATUS_CHECK} is 0019's predicate again "
            "(sendgrid_id IS NOT NULL; sent_at not required)"
        )
    else:
        _sqlite_rebuild(add=False, verb=f"reverted {TABLE} (0019 predicate regained)")


__all__ = ["revision", "down_revision"]
