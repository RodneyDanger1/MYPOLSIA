"""Async SQLAlchemy engine/session wiring.

IMPORTANT (measured): `create_async_engine("postgresql+asyncpg://...")` imports the
DBAPI at *construction* time and therefore raises ModuleNotFoundError when asyncpg
is not installed (unit-test venv). The engine is created LAZILY so that importing
`app.core.database` never requires a database driver.
"""
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


class Base(DeclarativeBase):
    """Declarative base shared by every ORM model."""


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """The process-wide engine, tuned per dialect.

    Measured cost of getting this wrong (lane 06): creating a fresh engine per Celery
    task costs ~4.3 ms warm and **179 ms cold** before any query runs, and the shipped
    `celery_app/tasks/*.py` created one per task invocation. One pooled engine per
    process, with `pool_pre_ping` and a statement timeout, removes that entirely.
    """
    global _engine
    if _engine is None:
        url = settings.database_url
        kwargs: dict[str, Any] = {"echo": settings.db_echo, "future": True}
        if url.startswith("sqlite"):
            # unit-test / embedded path: the pool must not hold a thread affinity, and a second
            # writer must WAIT for the single writer lock instead of erroring. The sqlite3 driver's
            # default busy timeout is 5s; a measured **8-way concurrent claim on a 200k-row file DB
            # blew past it and raised `sqlite3.OperationalError: database is locked` inside the claim
            # statement** — the exact case the claim path's docstring assumes away when it says SQLite
            # "serialises the statement behind its single writer lock". 30s makes that assumption true
            # under load instead of only under a single writer.
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30.0}
        else:
            kwargs.update(
                pool_pre_ping=settings.db_pool_pre_ping,
                pool_size=settings.db_pool_size,
                max_overflow=settings.db_max_overflow,
                pool_recycle=settings.db_pool_recycle_seconds,
                pool_use_lifo=True,
                # a runaway query must not pin a worker forever
                connect_args={
                    "server_settings": {
                        "statement_timeout": str(settings.db_statement_timeout_ms),
                        "application_name": settings.app_name,
                    }
                },
            )
        _engine = create_async_engine(url, **kwargs)
    return _engine


def is_sqlite(engine: AsyncEngine | None = None) -> bool:
    """True when the active engine is SQLite (drives the day-window binding).

    ZERO CALLERS (R25 dead-helper sweep, 2026-10-13; raw grep evidence:
    polsia-work/build/R25-deadhelpers/raw/grep_is_sqlite.txt) — nothing in app/,
    celery_app/, scripts/ or tests/ calls it.  The "day-window binding" note describes
    the intended role: the live binding path is `timeutil.dialect_name` /
    `as_bound_instant`, which take the session/engine themselves.  Removal vs adoption
    is an owner decision (REQUESTS R25-1).
    """
    return (engine or get_engine()).dialect.name == "sqlite"


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(), class_=AsyncSession, expire_on_commit=False
        )
    return _session_factory


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


def utcnow() -> datetime:
    """Single source of truth for "now" (timezone-aware UTC)."""
    return datetime.now(UTC)


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request, AUTO-COMMIT on success.

    CLAUDE.md: "get_db() auto-commits on successful yield".  On any exception the
    transaction is rolled back and the exception propagates (so FastAPI returns the
    real status code, not a 200 with a half-written row).
    """
    session_factory = get_session_factory()
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
