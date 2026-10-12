"""Day-window helpers.

Every query that means "today" must use these, so that the same window is used by
the dashboard, the evening cycle and the maintenance jobs.

Correctness note (measured + PostgreSQL docs): SQLite's CURRENT_TIMESTAMP (which is
what `server_default=func.now()` compiles to) stores the UTC wall clock as naive
text, while PostgreSQL stores `timestamptz` in UTC.  Passing a *naive local* datetime
compares a local wall clock against a UTC column: the PostgreSQL server interprets a
naive timestamp in the session TimeZone
(https://www.postgresql.org/docs/16/datatype-datetime.html - "If no time zone is
stated in the input string, then it is assumed to be in the time zone indicated by
the system's TimeZone parameter") and SQLite compares strings, so the window silently
shifts by the UTC offset.  Always compute bounds as timezone-AWARE UTC.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


def company_tz(tz_name: str | None = None) -> ZoneInfo:
    from app.config import settings

    name = tz_name or settings.company_timezone or "UTC"
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("UTC")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def today_utc() -> date:
    return datetime.now(timezone.utc).date()


def today_in(tz_name: str | None = None) -> date:
    return datetime.now(company_tz(tz_name)).date()


def day_bounds(day: date, tz_name: str | None = None) -> tuple[datetime, datetime]:
    """[start, end) as timezone-aware UTC instants for the company's local day."""
    tz = company_tz(tz_name)
    start = datetime.combine(day, time.min, tzinfo=tz)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def utc_day_bounds(day: date) -> tuple[datetime, datetime]:
    """[start, end) for a UTC calendar day (used when no company row exists)."""
    start = datetime.combine(day, time.min, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


# ── dialect-correct binding ──────────────────────────────────────────────────
# The schema stores `DateTime(timezone=True)` columns. PostgreSQL keeps them as
# `timestamptz`; SQLite keeps the UTC wall clock as *naive* text. Binding a
# timezone-AWARE datetime against a SQLite column therefore renders an offset
# ("…+00:00") that never equals the stored naive string, so a "today" window
# silently returns nothing. Measured in lane 06's matrix: with the app timezone set
# to UTC+9 the naive-local window selected 0 of 303 rows for the same day.
def dialect_name(target: object) -> str:
    """Best-effort dialect name for a Session/Engine/dialect/str."""
    if target is None:
        return "postgresql"
    if isinstance(target, str):
        return target.split("+")[0].split(":")[0]
    name = getattr(target, "name", None) or getattr(getattr(target, "dialect", None), "name", None)
    if name:
        return name
    bind = getattr(target, "bind", None) or getattr(target, "get_bind", None)
    if bind is not None and not isinstance(bind, type(lambda: None)):
        try:
            resolved = bind() if callable(bind) else bind
            return getattr(getattr(resolved, "dialect", None), "name", "postgresql")
        except Exception:  # noqa: BLE001 - never fail a query over introspection
            return "postgresql"
    return "postgresql"


def as_bound_instant(value: datetime, target: object) -> datetime:
    """Adapt an aware UTC instant to what the active dialect can compare against."""
    if dialect_name(target) == "sqlite":
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.astimezone(timezone.utc)


def day_window(
    target: object, day: date | None = None, tz_name: str | None = None
) -> tuple[datetime, datetime]:
    """`[start, end)` for the company's local `day`, bound for the active dialect.

    This is the ONLY day-window helper queries should use: it fixes the company
    timezone (not the process's), and it adapts the bound instants so SQLite and
    PostgreSQL agree on which rows are "today".
    """
    local_day = day or today_in(tz_name)
    start, end = day_bounds(local_day, tz_name)
    return as_bound_instant(start, target), as_bound_instant(end, target)
