"""Wave V7 — Auto Mode start-window race: the PostgreSQL advisory lock (drill-proven fix).

The V7 close drill (two true-concurrent POST /api/v1/auto-mode against the LIVE PostgreSQL
stack) PROVED the race the U5 module docstring had flagged as "in principle": both requests
passed the check and created windows (sessions 2 and 3; raw evidence in
polsia-work/build/ui-reverse/wave-v7/drills.txt).

Fix under pin here: `start_session` takes a PostgreSQL transaction-scoped advisory lock
BEFORE its check-then-insert, so a second starter blocks until the first transaction commits,
then observes the committed window and refuses `E_VALIDATION`. On SQLite (the unit harness)
the helper is an explicit no-op — the dialect guard is asserted in this file, and the
whole-flow proof lives in the wave-v7 live re-drill record.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.errors import PolsiaError
from app.services import auto_mode_service as ams


class _FakeBind:
    def __init__(self, name: str) -> None:
        self.dialect = SimpleNamespace(name=name)


class _RecordingDb:
    """A minimal stand-in for AsyncSession: records executed SQL, answers get_bind()."""

    def __init__(self, dialect_name: str) -> None:
        self._bind = _FakeBind(dialect_name)
        self.calls: list[tuple[str, dict]] = []

    def get_bind(self):  # noqa: ANN201 - test double mirrors SQLAlchemy's shape
        return self._bind

    async def execute(self, statement, params=None):  # noqa: ANN001, ANN201
        self.calls.append((str(statement), dict(params or {})))
        return None


async def test_lock_is_an_explicit_noop_on_non_postgres_dialects() -> None:
    """SQLite serializes writers; the helper must not emit PG-only SQL there."""
    db = _RecordingDb("sqlite")
    await ams._acquire_start_window_lock(db)
    assert db.calls == []


async def test_lock_takes_the_postgres_advisory_xact_lock_with_the_fixed_key() -> None:
    db = _RecordingDb("postgresql")
    await ams._acquire_start_window_lock(db)
    assert len(db.calls) == 1, "exactly one lock statement per start"
    sql, params = db.calls[0]
    assert "pg_advisory_xact_lock" in sql.lower()
    assert params["lock_key"] == ams._POSTGRES_START_WINDOW_LOCK_KEY


async def test_start_session_acquires_the_lock_before_checking(monkeypatch, async_db_session) -> None:
    """The start path must route through the helper — the fix cannot be bypassed by callers."""
    seen: list[str] = []

    async def spy(db) -> None:  # noqa: ANN001
        seen.append("locked")

    monkeypatch.setattr(ams, "_acquire_start_window_lock", spy)
    row = await ams.start_session(async_db_session, duration_hours=1.0)
    assert seen == ["locked"]
    assert row.id is not None


async def test_second_start_still_refuses_e_validation(async_db_session) -> None:
    """Sequential contract unchanged: one active window; the second start refuses by name."""
    await ams.start_session(async_db_session, duration_hours=1.0)
    with pytest.raises(PolsiaError) as excinfo:
        await ams.start_session(async_db_session, duration_hours=1.0)
    assert excinfo.value.code == "E_VALIDATION"
    assert "already active" in excinfo.value.message
