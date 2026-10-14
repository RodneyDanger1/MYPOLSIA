"""auto_mode_sessions — one row per Auto Mode window (P2's frozen schema, U5 migration 0007).

Frozen interface (WAVE-PLAN.md, "Frozen interfaces"): the table `auto_mode_sessions`, the class
`AutoModeSession` importable from `app.models.auto_mode`, the columns named exactly below, and the
index on `started_at`.  The P2 lane (auto-mode service + routes) CONSUMES these names; this module
is owned by the P3 schema lane and no other lane edits it.

Semantics carried by the columns (P2's outline, BACKEND-AUDIT §6 P2):

* a session is a WINDOW, not a parent — there is deliberately **no task foreign key**; the row
  records `started_at`/`ends_at`, and when it ends, `stopped_at` + `stop_reason`;
* `stop_reason` is a plain string, one of ``duration_end`` / ``day_cap`` / ``user_stop`` /
  ``error`` (documented value set; asserted by the writers, not by a CHECK — the frozen interface
  declares a nullable string, and SQLite cannot add a CHECK to an existing table anyway);
* `tasks_run` counts terminal writes that fell inside the window — a counter with a server default
  of 0 so an insert that never touches it is still a valid row;
* `spend_usd` is `NUMERIC(14, 4)` like every other money column (A-T17/T18: money is exact at
  rest) and stays **NULL when nothing was measured** — never a fabricated `0`;
* `started_at` carries the index (`ix_auto_mode_sessions_started_at`): "the sessions covering
  instant T" is the read the P2 status/close paths make.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Index, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AutoModeSession(Base):
    __tablename__ = "auto_mode_sessions"
    __table_args__ = (Index("ix_auto_mode_sessions_started_at", "started_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: When the window opened. NOT NULL: a session without a start is not a session.
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    #: The computed end (`start + duration`); NOT NULL — the window is closed before the row exists.
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: When the window actually ended early / was closed lazily. NULL while active.
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Why it ended: duration_end | day_cap | user_stop | error. NULL while active.
    stop_reason: Mapped[str | None] = mapped_column(String(50))
    #: Terminal task writes observed inside the window (default 0 — a fresh window has run none).
    tasks_run: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", default=0
    )
    #: Measured window spend (SUM of agent_runs.cost_usd); NULL when nothing was measured.
    spend_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<AutoModeSession id={self.id} started_at={self.started_at!r}>"
