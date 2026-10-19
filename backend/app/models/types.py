"""Dialect-portable column types.

CLAUDE.md: models must use JSON (never JSONB/ARRAY) so the unit suite can
create_all() on SQLite while production is PostgreSQL.  The shipped migration
0001_initial_schema.py is PostgreSQL-only and declares jsonb / text[]; using
`with_variant` keeps the ORM aligned with that migration AND compilable on SQLite.

Measured DDL:
    postgresql: payload JSONB, tags VARCHAR[]
    sqlite    : payload JSON,  tags JSON
and `postgresql.ARRAY(String())` alone fails on SQLite with
    CompileError: ... can't render element of type ARRAY
"""
from __future__ import annotations

import json
import logging

from sqlalchemy import JSON, String
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

logger = logging.getLogger(__name__)


class TolerantJSON(JSON):
    """`JSON` whose READ never raises on one malformed at-rest cell.

    On SQLite a JSON column is TEXT, and a raw/ops writer (not the ORM) can leave bytes that
    are not JSON.  SQLAlchemy's stock result processor then raises `json.JSONDecodeError` WHILE
    FETCHING — so ONE such row turned a whole list read (`GET /api/v1/tasks`) into an uncoded
    500 (V24b; the same class V24/L6 fixed for `daily_reports` and `activity_log`).

    This subclass keeps every bind/DDL behaviour of `JSON` and relaxes only the READ: junk
    reads as `None` ("not measured") plus a WARNING, never a parsed guess.  PostgreSQL keeps
    `JSONB` via `with_variant`; JSONB cannot hold malformed text, so the tolerance is exercised
    by SQLite (the unit/e2e path) and by raw writers there.
    """

    cache_ok = True

    # The frozen schema pin compares `str(column.type)`; this column IS a JSON column with an
    # identical DDL (JSON on SQLite, JSONB on PostgreSQL — see `json_type`), and the tolerance is
    # a READ behaviour only.  Reporting the schema identity keeps that pin truthful instead of
    # relabelling every JSON column in the tree.
    def __repr__(self) -> str:
        return "JSON"

    def __str__(self) -> str:
        return "JSON"

    def result_processor(self, dialect, coltype):  # type: ignore[no-untyped-def]
        """Wrap whatever processor the dialect built (SQLite builds its own, bypassing
        `process_result_value`) so ONE malformed cell reads as `None`, never a 500."""
        parent = super().result_processor(dialect, coltype)
        if parent is None:
            return None

        def process(value):  # type: ignore[no-untyped-def]
            try:
                return parent(value)
            except (TypeError, ValueError):
                logger.warning("tolerant JSON: an unreadable at-rest cell was read as absent")
                return None

        return process

    def process_result_value(self, value, dialect):  # type: ignore[no-untyped-def]
        if value is None or isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            logger.warning("tolerant JSON: an unreadable at-rest cell was read as absent")
            return None


class _TolerantSQLiteJSON(JSON):
    """The SQLite dialect's processor for `TolerantJSON` columns.

    SQLAlchemy's SQLite dialect adapts EVERY `JSON` subclass to its private `_SQliteJson`,
    which discards this module's overrides — so tolerance must be registered as a dialect
    colspec (below) to survive.  This class never raises on one unreadable cell.
    """

    def result_processor(self, dialect, coltype):  # type: ignore[no-untyped-def]
        parent = super().result_processor(dialect, coltype)

        def process(value):  # type: ignore[no-untyped-def]
            try:
                return parent(value)
            except (TypeError, ValueError):
                logger.warning("tolerant JSON: an unreadable at-rest cell was read as absent")
                return None

        return process


def _register_sqlite_tolerance() -> None:
    """Teach every SQLite dialect class to keep `TolerantJSON`'s tolerance.

    The async dialect used by this app (`SQLiteDialect_aiosqlite`) defines its OWN `colspecs`
    mapping — registering only on the base `SQLiteDialect` was silently ignored (measured).
    Additive only: the entry is keyed by OUR class, so no other column type changes.
    """
    try:
        from sqlalchemy.dialects.sqlite import aiosqlite, pysqlite
        from sqlalchemy.dialects.sqlite.base import SQLiteDialect
    except Exception:  # pragma: no cover - the dialect is always importable in this app
        return
    for dialect_cls in (
        SQLiteDialect,
        getattr(pysqlite, "SQLiteDialect_pysqlite", None),
        getattr(aiosqlite, "SQLiteDialect_aiosqlite", None),
    ):
        if dialect_cls is not None:
            dialect_cls.colspecs[TolerantJSON] = _TolerantSQLiteJSON


_register_sqlite_tolerance()


def json_type() -> JSON:
    """dict/list payload column (migration: JSONB)."""
    return TolerantJSON().with_variant(JSONB(), "postgresql")


def string_list_type() -> JSON:
    """list[str] column (migration: ARRAY(String))."""
    return JSON().with_variant(ARRAY(String()), "postgresql")
