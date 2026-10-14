"""Task schedules — CRUD for `task_schedules` + the cadence tick's claim (wave V8, C2).

What this module owns, and the rules it is the ONE definition of:

* **CRUD** — `create_schedule` / `list_schedules` / `get_schedule` / `update_schedule` /
  `delete_schedule` over `task_schedules`.  Validation lives in the wire schemas AND here: the
  service refuses a malformed cadence/hour/weekday BY NAME (`E_VALIDATION`) so a caller that never
  passes through the route cannot store a value the tick would then have to refuse at read time; an
  unknown id is `E_NOT_FOUND` (the route answers it 404).
* **`compute_next_run`** — the next due instant for a cadence, computed in the COMPANY timezone
  from the wall clock the caller hands in.  NEVER `+24h`: a US spring-forward day is 23 h and a
  fall-back day 25 h, so a fixed day-count or fixed-offset implementation drifts by an hour; the
  rule here is one sentence — *the next candidate wall time (date + hour, weekday-filtered)
  STRICTLY AFTER `now`*, converted back to an instant.  An ambiguous wall time (the repeated hour
  of a fall-back day) resolves to its first occurrence and a skipped wall time (the omitted hour of
  a spring-forward day) to the first instant carrying that offset — Python's zoneinfo defaults,
  deterministic and documented.  A `once` schedule's `next_run_at` clears to None once the tick
  has fired it (no fabricated repeat).
* **The pause rule** — `enabled=false` IS the pause: the tick skips the row; disabling CLEARS
  `next_run_at` and enabling recomputes it from the wall clock, so a resume never fires a child for
  a window that passed while the schedule was paused.
* **`claim_due_schedules`** — the tick's one selection: every ENABLED row whose `next_run_at` is
  due (`<= now`, bound for the active dialect).  Every due row is validated BEFORE anything is
  advanced (a malformed stored value refuses `E_CONFIG` with the row and range named, and the tick
  writes nothing — no partial advance, no half-created children); then each due row is advanced in
  the SAME transaction the tick's child insert runs in (`last_run_at = now`, `next_run_at`
  recomputed — None for a spent `once`), and returned as a `ClaimedSchedule` carrying the DEDUPE
  KEY `schedule:<id>:<iso-date-hour>` built from the DUE instant in the company timezone.  Two
  ticks that both see the same due instant therefore collide on the task's unique dedupe key: ONE
  child per schedule per due instant, even across re-deliveries; the recompute (never +24h) keeps
  the NEXT instant correct across DST.

Frozen names (WAVE-PLAN.md, "Frozen interface — SCHEDULES"): the dedupe key format, the pause rule
and the company-tz next-run doctrine are the interfaces this module implements — the tick
(`celery_app.tasks.cadence`) and the routes (`app/api/v1/schedules.py`) add no rules of their own.
"""
from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Any, NamedTuple
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import E_CONFIG, E_NOT_FOUND, E_VALIDATION, ERROR_HINTS, PolsiaError
from app.core.timeutil import as_bound_instant, company_tz, now_utc
from app.models.schedule import ScheduleCadence, TaskSchedule

#: The frozen cadence value set, in definition order (derived from the model's enum — the CHECK
#: list and this tuple cannot drift, the `task_service.VALID_TASK_STATUSES` discipline).
CADENCES: tuple[str, ...] = tuple(member.value for member in ScheduleCadence)

HOUR_MIN, HOUR_MAX = 0, 23
WEEKDAY_MIN, WEEKDAY_MAX = 0, 6
#: `weekdays` fires Monday..Friday (Python's `date.weekday()`: Monday=0).
_WEEKDAYS = frozenset({0, 1, 2, 3, 4})
#: The fields a caller may change through `update_schedule` (the PATCH surface, frozen).
UPDATABLE_FIELDS = frozenset({"name", "prompt", "cadence", "hour", "weekday", "enabled"})


class ClaimedSchedule(NamedTuple):
    """One due row as the tick consumes it: identity, prompt, the due instant, its dedupe key."""

    schedule_id: int
    name: str
    prompt: str
    due_at: datetime  # aware UTC — the instant that was due
    dedupe_key: str   # `schedule:<id>:<ISO date-hour of due_at in the company timezone>`


# ── named refusals (write side E_VALIDATION / stored side E_CONFIG) ───────────────────────────


def _refuse_validation(field: str, value: object, expected: str) -> PolsiaError:
    return PolsiaError(
        E_VALIDATION,
        f"task_schedules.{field} {value!r} is invalid: {expected}",
        how_to_fix=ERROR_HINTS[E_VALIDATION],
        context={"field": field, "value": value, "expected": expected},
    )


def _refuse_stored(field: str, value: object, expected: str) -> PolsiaError:
    return PolsiaError(
        E_CONFIG,
        f"task_schedules.{field} {value!r} is not a valid schedule setting: {expected}",
        how_to_fix=ERROR_HINTS[E_CONFIG],
        context={"field": field, "value": value, "expected": expected},
    )


def _check_cadence(cadence: object) -> None:
    if cadence not in CADENCES:
        raise _refuse_validation("cadence", cadence, f"one of {', '.join(CADENCES)}")


def _check_hour(hour: object) -> None:
    if isinstance(hour, bool) or not isinstance(hour, int) or not HOUR_MIN <= hour <= HOUR_MAX:
        raise _refuse_validation("hour", hour, f"an integer {HOUR_MIN}-{HOUR_MAX}")


def _check_weekday(weekday: object) -> None:
    if weekday is None:
        return
    if (
        isinstance(weekday, bool)
        or not isinstance(weekday, int)
        or not WEEKDAY_MIN <= weekday <= WEEKDAY_MAX
    ):
        raise _refuse_validation(
            "weekday", weekday, f"an integer {WEEKDAY_MIN}-{WEEKDAY_MAX} (Monday=0) or null"
        )


def _validate_fields(cadence: object, hour: object, weekday: object) -> None:
    """The write-side check: refusal BY NAME before any row is built or touched."""
    _check_cadence(cadence)
    _check_hour(hour)
    _check_weekday(weekday)
    if cadence == "weekly" and weekday is None:
        raise _refuse_validation(
            "weekday", weekday, "a weekly schedule needs a weekday 0-6 (Monday=0)"
        )


def _assert_stored_values(row: TaskSchedule) -> None:
    """The read-side belt: a malformed STORED value is refused E_CONFIG at claim time, never
    silently skipped (the row was written by something that bypassed the wire; only the operator
    can fix it)."""
    if row.cadence not in CADENCES:
        raise _refuse_stored("cadence", row.cadence, f"one of {', '.join(CADENCES)}")
    if (
        isinstance(row.hour, bool)
        or not isinstance(row.hour, int)
        or not HOUR_MIN <= row.hour <= HOUR_MAX
    ):
        raise _refuse_stored("hour", row.hour, f"an integer {HOUR_MIN}-{HOUR_MAX}")
    if row.cadence == "weekly":
        weekday = row.weekday
        if weekday is None:
            raise _refuse_stored(
                "weekday", weekday, "a weekly row needs a weekday 0-6 (Monday=0)"
            )
        if (
            isinstance(weekday, bool)
            or not isinstance(weekday, int)
            or not WEEKDAY_MIN <= weekday <= WEEKDAY_MAX
        ):
            raise _refuse_stored(
                "weekday", weekday, f"an integer {WEEKDAY_MIN}-{WEEKDAY_MAX} (Monday=0)"
            )


# ── the next-run math ─────────────────────────────────────────────────────────────────────────


def compute_next_run(
    cadence: str, hour: int, weekday: int | None, *, now: datetime, tz: ZoneInfo
) -> datetime:
    """The next due instant STRICTLY AFTER `now` as company-local wall time, returned aware UTC.

    NEVER `now + 24h`: the candidate is a wall time (`date + hour` in `tz`) and the comparison /
    conversion let zoneinfo carry the real offset for THAT date — the two US DST boundaries in
    tests/unit/test_v8_schedules.py are the fixed-instant proof.  Weekday filtering: `weekdays`
    keeps Monday-Friday, `weekly` requires `weekday` (0=Monday).  `once` and `daily` differ only in
    what the TICK does after firing (`once` clears `next_run_at`).
    """
    _validate_fields(cadence, hour, weekday)
    moment = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    local_now = moment.astimezone(tz)
    day = local_now.date()
    for offset in range(8):  # a weekly cadence always finds its day within 7; 8 is the honest bound
        candidate_day = day + timedelta(days=offset)
        if cadence == "weekdays" and candidate_day.weekday() not in _WEEKDAYS:
            continue
        if cadence == "weekly" and candidate_day.weekday() != weekday:
            continue
        candidate = datetime.combine(candidate_day, time(hour=hour), tzinfo=tz)
        if candidate > local_now:
            return candidate.astimezone(UTC)
    # Unreachable for every cadence in the frozen set (each finds an occurrence within 8 days).
    # Kept as a named refusal rather than an assert so a corrupted tz database can never silently
    # drop a schedule.
    raise _refuse_stored("cadence", cadence, f"no occurrence of {cadence!r} within 8 days")


# ── CRUD ─────────────────────────────────────────────────────────────────────────────────────


async def create_schedule(
    db: AsyncSession,
    *,
    name: str,
    prompt: str,
    cadence: str,
    hour: int,
    weekday: int | None = None,
    enabled: bool = False,
    now: datetime | None = None,
) -> TaskSchedule:
    """Insert one schedule (flushed; `.id` set).  An ENABLED create computes its first `next_run_at`.

    `enabled` defaults FALSE (the column default): a schedule is created PAUSED unless the caller
    says otherwise — a paused row carries NO `next_run_at` (nothing pending, nothing stale).  `now`
    is the injection point for deterministic callers and tests; the wire route never passes it.
    """
    if not isinstance(name, str) or not name.strip():
        raise _refuse_validation("name", name, "a non-empty string")
    if not isinstance(prompt, str) or not prompt.strip():
        raise _refuse_validation("prompt", prompt, "a non-empty string")
    _validate_fields(cadence, hour, weekday)
    row = TaskSchedule(
        name=name,
        prompt=prompt,
        cadence=cadence,
        hour=hour,
        weekday=weekday,
        enabled=bool(enabled),
    )
    if row.enabled:
        row.next_run_at = compute_next_run(
            cadence, hour, weekday, now=now or now_utc(), tz=company_tz()
        )
    db.add(row)
    await db.flush()
    return row


async def list_schedules(db: AsyncSession) -> list[TaskSchedule]:
    """Every row, oldest first (stable id order) — the list route's order, defined here once."""
    rows = (await db.execute(select(TaskSchedule).order_by(TaskSchedule.id))).scalars().all()
    return list(rows)


async def get_schedule(db: AsyncSession, schedule_id: int) -> TaskSchedule:
    """One row by id; an unknown id is the named `E_NOT_FOUND` (the route answers it 404)."""
    row = await db.get(TaskSchedule, int(schedule_id))
    if row is None:
        raise PolsiaError(
            E_NOT_FOUND,
            f"task schedule {schedule_id} does not exist",
            how_to_fix=ERROR_HINTS[E_NOT_FOUND],
            context={"schedule_id": int(schedule_id)},
        )
    return row


async def update_schedule(
    db: AsyncSession, schedule_id: int, changes: dict[str, Any], *, now: datetime | None = None
) -> TaskSchedule:
    """Apply ONLY the provided fields (the route passes `model_dump(exclude_unset=True)`).

    The pause rule: disabling clears `next_run_at`; enabling recomputes it from the wall clock;
    changing cadence/hour/weekday on an enabled row recomputes it too (a name- or prompt-only edit
    leaves the pending instant alone).  An unknown key is refused `E_VALIDATION` before the row is
    read; `weekday: null` clears the column (only legal for the cadences that do not use it).
    """
    unknown = set(changes) - UPDATABLE_FIELDS
    if unknown:
        raise _refuse_validation(
            "changes", sorted(unknown), f"only {sorted(UPDATABLE_FIELDS)} may be updated"
        )
    row = await get_schedule(db, schedule_id)
    cadence = changes.get("cadence", row.cadence)
    hour = changes.get("hour", row.hour)
    weekday = changes.get("weekday", row.weekday)
    _validate_fields(cadence, hour, weekday)
    if "name" in changes and (not isinstance(changes["name"], str) or not changes["name"].strip()):
        raise _refuse_validation("name", changes["name"], "a non-empty string")
    if "prompt" in changes and (
        not isinstance(changes["prompt"], str) or not changes["prompt"].strip()
    ):
        raise _refuse_validation("prompt", changes["prompt"], "a non-empty string")
    for field, value in changes.items():
        setattr(row, field, value)
    if row.enabled:
        if set(changes) & {"cadence", "hour", "weekday", "enabled"} or row.next_run_at is None:
            row.next_run_at = compute_next_run(
                row.cadence, row.hour, row.weekday, now=now or now_utc(), tz=company_tz()
            )
    else:
        row.next_run_at = None
    await db.flush()
    return row


async def delete_schedule(db: AsyncSession, schedule_id: int) -> TaskSchedule:
    """Remove the row and return it (the route answers the removed representation — the
    `api/v1/memory.py` DELETE precedent, not a bare 204)."""
    row = await get_schedule(db, schedule_id)
    await db.delete(row)
    await db.flush()
    return row


# ── the tick's claim ─────────────────────────────────────────────────────────────────────────


async def claim_due_schedules(
    db: AsyncSession, *, now: datetime | None = None, tz: ZoneInfo | None = None
) -> list[ClaimedSchedule]:
    """Every ENABLED due row, advanced and returned with its child's dedupe key (see the module
    docstring for the full contract: validate-then-advance, one child per due instant, anti-+24h).
    """
    moment = now or now_utc()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    zone = tz or company_tz()
    bound = as_bound_instant(moment, db)
    stmt = (
        select(TaskSchedule)
        .where(
            TaskSchedule.enabled.is_(True),
            TaskSchedule.next_run_at.is_not(None),
            TaskSchedule.next_run_at <= bound,
        )
        .order_by(TaskSchedule.id)
    )
    rows = list((await db.execute(stmt)).scalars().all())

    # Validate EVERY due row FIRST: one malformed stored value refuses E_CONFIG naming the row's
    # field and range, and nothing is advanced or created (no partial tick).
    for row in rows:
        _assert_stored_values(row)

    claims: list[ClaimedSchedule] = []
    for row in rows:
        due = row.next_run_at
        # The SELECT above filters `next_run_at IS NOT NULL`; the assert documents that SQL
        # invariant for the type checker (which cannot read WHERE clauses) — it can never fire.
        assert due is not None
        due = due if due.tzinfo is not None else due.replace(tzinfo=UTC)
        bucket = due.astimezone(zone).strftime("%Y-%m-%dT%H")
        claims.append(
            ClaimedSchedule(
                schedule_id=int(row.id),
                name=str(row.name),
                prompt=str(row.prompt),
                due_at=due.astimezone(UTC),
                dedupe_key=f"schedule:{int(row.id)}:{bucket}",
            )
        )
        row.last_run_at = moment
        row.next_run_at = (
            None
            if row.cadence == "once"
            else compute_next_run(row.cadence, row.hour, row.weekday, now=moment, tz=zone)
        )
    await db.flush()
    return claims


__all__ = [
    "CADENCES",
    "ClaimedSchedule",
    "claim_due_schedules",
    "compute_next_run",
    "create_schedule",
    "delete_schedule",
    "get_schedule",
    "list_schedules",
    "update_schedule",
]
