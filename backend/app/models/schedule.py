"""task_schedules — owner-defined recurring task prompts (wave V8, migration 0008).

Frozen interface (WAVE-PLAN.md, "Frozen interface — SCHEDULES"): the table `task_schedules`, the
class `TaskSchedule` importable from `app.models.schedule`, the columns named exactly below.  The
C2 lane's service / routes / cadence tick are the consumers.

Semantics carried by the columns (plan section 7 C2):

* `cadence` is one of `once` / `daily` / `weekdays` / `weekly` — a named CHECK
  (`ck_task_schedules_cadence`), same spelling discipline as `tasks.status`;
* `hour` is the COMPANY-LOCAL wall-clock hour (0-23) the schedule fires at; `weekday` is the 0-6
  day for `weekly` (Monday=0, Python's `date.weekday()` convention) and stays NULL for the cadences
  that do not use it — NULL is the honest "not applicable", never a 0 sentinel;
* `enabled` is the pause switch: FALSE means the cadence tick SKIPS the row entirely (pause =
  `PATCH enabled=false` on the wire).  NOT NULL server default FALSE so every row states its state;
* `next_run_at` is the DUE instant of the next firing, recomputed from the company-tz WALL CLOCK
  after each firing (never by adding 24h — DST-safe).  NULL = nothing pending (a `once` schedule
  that has fired, or a row never scheduled yet);
* children are created JUST-IN-TIME by the cadence tick — this table NEVER materializes future
  child tasks (plan section 7 C2's M2 trap);
* `last_run_at` is the instant of the most recent firing, written by the tick in the same
  transaction that advances `next_run_at`.
"""
from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, Integer, Text, false, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ScheduleCadence(enum.StrEnum):
    """The `task_schedules.cadence` value set (the four firing rules)."""

    ONCE = "once"
    DAILY = "daily"
    WEEKDAYS = "weekdays"
    WEEKLY = "weekly"


def schedule_cadence_values(enum_cls: type[enum.Enum] = ScheduleCadence) -> list[str]:
    """`Enum.values_callable`: persist the member VALUES, in definition order (task.py's rule)."""
    return [str(member.value) for member in enum_cls]


class TaskSchedule(Base):
    __tablename__ = "task_schedules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    cadence: Mapped[ScheduleCadence] = mapped_column(
        Enum(
            ScheduleCadence,
            native_enum=False,          # VARCHAR + CHECK on every backend
            create_constraint=True,     # the named CHECK is the at-rest enforcement
            name="ck_task_schedules_cadence",
            length=50,
            values_callable=schedule_cadence_values,
        ),
        nullable=False,
    )
    hour: Mapped[int] = mapped_column(Integer, nullable=False)
    weekday: Mapped[int | None] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=false(), default=False
    )
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<TaskSchedule id={self.id} {self.cadence} hour={self.hour}>"
