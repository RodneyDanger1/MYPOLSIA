"""consumed_approvals — the single-use approval-token ledger (W2, SENTINEL + TRUTHSEEKER)

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-28

WHY.  `policy_firewall.issue_approval` mints an HMAC token and `verify_approval` is stateless, so
the SAME token verified any number of times inside its TTL.  sentinel measured the live
consequence: `"REPLAY the same token a second time" -> ok` — the deploy gate performed an
irreversible action twice from ONE human approval (`sentinel/raw/token-probe.txt`).  Four shipped
docstrings claimed "single-use"; nothing consumed the token.  sentinel's enforcement half
(`policy_firewall.consume_approval` / `consume_approval_or_refuse`, the deploy and sites gates)
writes this ledger; THIS revision is the durable schema half, deliberately additive.

COLUMNS of `consumed_approvals` (identical to `app/models/approval.py`; create_all and the
migration describe ONE schema):

    id                 INTEGER PK (SQLite rowid / PostgreSQL SERIAL)
    token_id           VARCHAR(64) NOT NULL, UNIQUE (`uq_consumed_approvals_token_id`) —
                       sha256 hex of the token's signature body.  The RAW token is never stored
                       (a token is a bearer credential), so a database export cannot replay an
                       approval.  This unique key is THE single-use guarantee: the second spend of
                       one token is a unique violation, reported as `already_used`.
    jti                VARCHAR(32) NULL, UNIQUE (`uq_consumed_approvals_jti`) — the random nonce a
                       nonce-bearing token carries; one nonce identifies one mint and cannot be
                       spent by two different token bodies.  NULL only for a legacy
                       `{exp}.{signature}` token, and SQL treats NULLs as distinct, so several
                       legacy spends coexist (the compatibility `policy_firewall._split_token`
                       already implements).
    action_fingerprint VARCHAR(64) NOT NULL — the action the token was minted for
                       (`ix_consumed_approvals_fingerprint` serves the audit/retention read).
    consumed_at        TIMESTAMP WITH TIME ZONE NOT NULL, server default now() — the database
                       clock (`ix_consumed_approvals_consumed_at` serves retention reads).
    consumed_by        VARCHAR(120) NOT NULL — a short seam label such as `deploy:landing-page`,
                       never a credential (`policy_firewall` truncates to 120).

NO FOREIGN KEY on purpose: this is security state that must outlive the approval's origin and must
never be cascaded away — a deleted consumption row would re-arm a replay.  The data wipe KEEPS this
ledger (`app/api/v1/data.py::KEPT`): deleting spent jtis during a wipe would resurrect every token
presented before it, turning the owner's data deletion into a replay window.

REVERSIBILITY.  `downgrade()` drops the three indexes and the table.  The drop is destructive to
LEDGER DATA BY DESIGN (the table does not exist below this revision); it does not touch any
pre-existing table or row, which is what the roundtrip test proves (`0017 -> 0018 -> 0017 -> 0018`
with a `tasks` row preserved throughout).

Both engines are handled by portable DDL (no dialect branch): `op.create_table` +
`op.create_index(unique=True)` render correctly on SQLite and PostgreSQL.  PostgreSQL was verified
by `pg_dump`-independent introspection in the wave-2 receipt (§ migration roundtrip); the SQLite leg
is exercised by the unit tests.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "consumed_approvals"
TOKEN_UNIQUE = "uq_consumed_approvals_token_id"
JTI_UNIQUE = "uq_consumed_approvals_jti"
FINGERPRINT_INDEX = "ix_consumed_approvals_fingerprint"
CONSUMED_AT_INDEX = "ix_consumed_approvals_consumed_at"


def upgrade() -> None:
    # The literal table name is deliberate: `tests/parity/test_contract_parity.py` statically reads
    # `op.create_table("<name>"` to compare the ORM table set with the migration chain (0017 made
    # the same choice); a constant here would hide this revision from the parity guard.
    op.create_table(
        "consumed_approvals",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("token_id", sa.String(length=64), nullable=False),
        sa.Column("jti", sa.String(length=32), nullable=True),
        sa.Column("action_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "consumed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("consumed_by", sa.String(length=120), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_id", name=TOKEN_UNIQUE),
    )
    # The nonce's own uniqueness: one jti = one mint.  NULLs are distinct on both engines, so a
    # legacy token without a nonce (NULL) never collides with another legacy spend.
    op.create_index(JTI_UNIQUE, TABLE, ["jti"], unique=True)
    op.create_index(FINGERPRINT_INDEX, TABLE, ["action_fingerprint"], unique=False)
    op.create_index(CONSUMED_AT_INDEX, TABLE, ["consumed_at"], unique=False)
    print(
        f"[alembic 0018] created {TABLE} (token_id UNIQUE, jti UNIQUE, "
        f"{FINGERPRINT_INDEX}, {CONSUMED_AT_INDEX})"
    )


def downgrade() -> None:
    op.drop_index(CONSUMED_AT_INDEX, table_name=TABLE)
    op.drop_index(FINGERPRINT_INDEX, table_name=TABLE)
    op.drop_index(JTI_UNIQUE, table_name=TABLE)
    op.drop_table(TABLE)
    print(f"[alembic 0018] dropped {TABLE} (ledger rows are removed with it, by design)")
