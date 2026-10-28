"""`consumed_approvals` — the single-use ledger behind approval tokens (W2 / SENTINEL).

THE DEFECT THIS TABLE CLOSES.  `policy_firewall.issue_approval` mints an HMAC token and
`verify_approval` re-computed the signature on every call, but NOTHING recorded that the token had
been spent: the same valid token verified twice and the live deploy gate ALLOWED the replay
(wave-1 evidence: `sentinel/raw/token-probe.txt`, "REPLAY the same token a second time" -> ok).
Every docstring said "single-use"; no code made it so.

WHY A TABLE AND A UNIQUE INDEX.  A token is presented to whichever process performs the action —
the API (deploy, data wipe) or a Celery worker (an agent's irreversible tool call) — so the
consumption check must be atomic ACROSS PROCESSES.  The only atomicity primitive this stack already
trusts is a database unique index (`truthseeker` used the same reasoning for the live PG triggers):
one `INSERT` per spend, and a second INSERT of the same token is a unique violation, which is
reported as "already used", never as a success.

WHAT IS STORED (and what is deliberately not).
  * `token_id` — the sha256 hex of the token's signature body (the part after the dot, which now
    carries the `jti` nonce).  The RAW token never touches the database: a token is a bearer
    credential (`EXPECTED` in `app/api/v1/data.py`'s redaction list), and a ledger that stored it
    would become a store of live credentials.
  * `jti` — the random nonce itself (not secret: it only proves WHICH mint was spent, and it is
    already inside the token the owner holds).  UNIQUE as well (`uq_consumed_approvals_jti`): one
    nonce identifies one mint, so it can never be spent by two different token bodies.  NULL only
    for a legacy `{exp}.{signature}` token, and NULLs do not collide.
  * `action_fingerprint` — the action the token was minted for, so an audit can answer "what was
    approved" without the token.
  * `consumed_at` / `consumed_by` — when, and which seam spent it ("deploy:landing-page",
    "data-wipe", ...).  `consumed_by` is a LABEL, never a credential.

WHY NO FOREIGN KEY: a spend must outlive the approval's origin and must never be cascaded away by
an unrelated delete.  The row's only job is to say "this token was used"; deleting the history
would re-arm a replay.

PG-ONLY NOTE: this table's guarantee depends on the unique index being enforced by the database
(SQLite enforces it too, and that is what the unit tests exercise).  A deployment that creates the
table WITHOUT the unique constraint would silently disable single-use — the migration must carry
`uq_consumed_approvals_token_id` and a test asserts the constraint exists on the test schema.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ConsumedApproval(Base):
    """One SPENT approval token: the unique `token_id` is what makes a replay impossible."""

    __tablename__ = "consumed_approvals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: sha256 hex of the token's signature body (48 hex chars today -> 64-char digest).  The raw
    #: signature is never stored.
    token_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The random nonce the token carried (`policy_firewall.JTI_HEX_CHARS` hex chars); NULL only
    #: for a legacy `{exp}.{signature}` token minted before the nonce existed.
    jti: Mapped[str | None] = mapped_column(String(32))
    #: The approval fingerprint the spend verified against (`policy_firewall.fingerprint()`).
    action_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    #: When the token was spent; the database clock, never a caller-supplied value.
    consumed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    #: Which seam spent it — a LABEL such as "deploy:<slug>", never a credential or a token.
    consumed_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")

    __table_args__ = (
        # THE guarantee: a second INSERT of the same token is a unique violation, which the
        # consumer reports as "already_used".  Named so a migration/report can cite the constraint.
        UniqueConstraint("token_id", name="uq_consumed_approvals_token_id"),
        # The nonce itself is ALSO unique (W2, truthseeker's ledger half): one `jti` identifies one
        # MINT, so two distinct token bodies carrying the same nonce cannot both be spent.  It is
        # nullable on purpose — a legacy `{exp}.{sig}` token carries no nonce and multiple legacy
        # spends must coexist (SQL treats NULLs as distinct), which is exactly the compatibility
        # `consume_approval` already promises in `policy_firewall._split_token`.
        Index("uq_consumed_approvals_jti", "jti", unique=True),
        Index("ix_consumed_approvals_fingerprint", "action_fingerprint"),
        Index("ix_consumed_approvals_consumed_at", "consumed_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper, never a wire shape
        return (
            f"ConsumedApproval(token_id={self.token_id[:12]}…, "
            f"fp={self.action_fingerprint}, by={self.consumed_by!r})"
        )
