"""Alembic environment.

WHY the SQLite foreign-key guard lives HERE (L2-07) and not in a migration
--------------------------------------------------------------------------------
`0003_money_exact._rebuild` drops each table and re-creates it from measured DDL.  It drops
the FK PARENT (`ad_campaigns`) while child rows still point at it (the real `ad_metrics`, and
the freshly-created `_t17_ad_metrics`, both carry `REFERENCES ad_campaigns (id)`), which SQLite
turns into an implicit `DELETE` on the parent — violating the child constraint the moment
`PRAGMA foreign_keys=ON` is in force.  The chain only survives today because nothing enables
enforcement; enforcement ON is the standard hardening (it is also the only way SQLite honours
the FK semantics 0002 adds on PostgreSQL), and the 12-step ALTER TABLE procedure this
migration deliberately mimics starts by DISABLING foreign keys for exactly this reason.
Measured (research-B): a chain run over a seeded 0002 database fails with
`IntegrityError: FOREIGN KEY constraint failed` when enforcement is on.

Where the pragma can live is decided by SQLite itself: "It is not possible to enable or
disable foreign key constraints in the middle of a multi-statement transaction" — it is
silently IGNORED.  Alembic runs `upgrade head` as one transaction (by the time 0003 runs,
0002's duplicate-resolution `DELETE` has already opened it), so an
`op.execute("PRAGMA foreign_keys=OFF")` inside 0003 is a measured NO-OP.  At raw-connect time
there is no transaction yet, so the pragma is honoured; the guard is therefore a `connect`
event listener on the migration engine, and after the run a `PRAGMA foreign_key_check`
(12-step step 10) proves no dangling references were left behind (the raw-DBAPI listener
channel is measured working under aiosqlite).

The migration engine is created per run and discarded, so the pragma is per-run connection
state that cannot leak into the application engine (`app/core/database.py`); step 12
(re-enable) is therefore a no-op we state rather than code.  Offline (`--sql`) mode never
connects and is unaffected.  If an operator later enables FKs for the app engine, that
decision does not — and must not — change these migrations.
"""
import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings
from app.core.database import Base

# Import all models so Alembic can detect them
import app.models  # noqa: F401

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def install_sqlite_fk_guard(connectable) -> bool:
    """SQLite 12-step step 1: disable FK enforcement at raw-connect time.  Returns whether.

    The listener fires before the driver can have opened any transaction, so the pragma is
    honoured (see the module docstring for why this location is the only one that works).
    The form — a DBAPI cursor execute inside the `connect` event — is the one measured
    working under the aiosqlite dialect.
    """
    if connectable.dialect.name != "sqlite":
        return False

    @event.listens_for(connectable.sync_engine, "connect")
    def _fk_off_on_connect(dbapi_connection, _record):  # noqa: ANN001, ANN202
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.close()

    return True


async def check_sqlite_foreign_keys(connection) -> None:
    """12-step step 10: refuse to report an upgrade whose result has dangling references."""
    rows = (await connection.exec_driver_sql("PRAGMA foreign_key_check")).fetchall()
    if rows:
        sample = ", ".join(f"{row[0]} (rowid={row[1]} -> {row[2]})" for row in rows[:5])
        raise RuntimeError(
            "alembic: SQLite foreign_key_check found dangling references after this run "
            f"({len(rows)} violation(s): {sample}) — refusing to report success"
        )


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = create_async_engine(settings.database_url)
    guarded = install_sqlite_fk_guard(connectable)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
        if guarded:
            # Runs after the migration transaction committed (step 10/11 order).
            await check_sqlite_foreign_keys(connection)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
