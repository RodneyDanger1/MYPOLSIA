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

from sqlalchemy import JSON, String
from sqlalchemy.dialects.postgresql import ARRAY, JSONB


def json_type() -> JSON:
    """dict/list payload column (migration: JSONB)."""
    return JSON().with_variant(JSONB(), "postgresql")


def string_list_type() -> JSON:
    """list[str] column (migration: ARRAY(String))."""
    return JSON().with_variant(ARRAY(String()), "postgresql")
