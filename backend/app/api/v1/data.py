"""`/api/v1/data` — the owner's data portability surface (wave V10, D7).

Two routes, one contract each:

    GET    /api/v1/data/export   -> 200 application/zip (MANIFEST.json + 8 members)
    DELETE /api/v1/data          -> 200 JSON (the guarded wipe) | named refusals

THE EXPORT (V10-D2)
    Members: tasks, agent_runs, activity_log, revenue_snapshots, invoices, task_schedules (CSV —
    tabular data) + memory_entries, chat_messages (JSON).  `MANIFEST.json` names every member,
    its table, its format, its columns and its ROW COUNT AS EXPORTED — the manifest is built
    from the same rows that are written into the archive, so the declared counts and the archive
    cannot disagree.  A NULL cell is an empty CSV cell (CSV has no null type — documented, the
    `runs.py` convention); a dict/list cell is JSON-encoded into its CSV cell (a Python `repr`
    would not round-trip); datetimes are ISO-8601.  The export NEVER carries the DeepSeek key
    store or `.env` contents — the store is a FILE outside every member table, and
    `tests/unit/api/test_v10_data_export.py` asserts a seeded key sentinel appears in NO member
    (the brief's absolute rule).  Reads run inside `refusal_guard()`; the ZIP is fully built and
    counted BEFORE the response object exists (the `runs.py` streaming doctrine: an honest count
    cannot be declared before the first byte otherwise) — but the archive itself is written to a
    TEMP FILE in the system scratch dir (`mkstemp`, U2-10), never materialized as one in-memory
    `bytes` payload, and served by `FileResponse`, which streams it from disk in its own bounded
    chunks.  The temp file is removed by a background task once the response has been sent (and
    on any build failure), so the endpoint cannot leak scratch files.

THE WIPE (V10-D3)
    `DELETE /api/v1/data` deletes the member tables' rows in ONE transaction and KEEPS
    `company_config` rows and the DeepSeek key store (a file — nothing here can touch it).
    Two query modes:

      * `?dry_run=true` — answers WOULD-delete counts; never deletes, needs no token, never
        consumes one;
      * `?confirm_token=<token>` — the guarded wipe.  The token is minted through the SHIPPED
        approvals machinery (`POST /api/v1/approvals` with `action_fingerprint =
        DATA_WIPE_ACTION_FINGERPRINT`), which mints `"{exp}.{sig}"` via
        `policy_firewall.issue_approval` under the SAME `APPROVAL_SECRET`; verification here is
        `policy_firewall.verify_approval` — the REAL primitive, same secret, same parse.  No
        new mint exists in this module: the shipped mint already accepts any fingerprint string,
        and `DATA_WIPE_ACTION_FINGERPRINT` embeds a digest of the frozen member set, so a future
        member-list change invalidates old tokens instead of silently widening what they approve.

SINGLE-USE — HOW, AND WHY IT LOOKS LIKE THIS
    The approvals token is stateless (HMAC + TTL), so single-use needs a durable consumed-token
    record the wipe itself cannot delete.  The record lives BESIDE the key store it must survive:
    `<backend>/data/data_wipe_consumed.json` (`_CONSUMED_TOKENS_PATH`), written temp-file +
    `os.replace` (the `secret_store` pattern; tests monkeypatch the path).  Ordering is
    FAIL-CLOSED and deliberate:

      1. parse + verify the token (malformed/expired/invalid refuse by name, nothing burned);
      2. read the marker; a sig already present -> `already_used` refusal (this is what refuses a
         REUSE even after the member tables were re-seeded — pinned by the test suite);
      3. open the wipe transaction and lock the KEPT `company_config` row (`FOR UPDATE`; a no-op
         on SQLite, which serializes writes anyway) as the cross-process mutex; an all-empty
         member set refuses `E_VALIDATION` WITHOUT burning the token (a pointless mint is not a
         use); concurrent same-token requests serialize on the lock and the loser sees the empty
         state the winner committed — refused, never a second wipe;
      4. APPEND + persist the consumed record, THEN delete rows in the same open transaction and
         commit.  A crash between (4) and the commit burns the token without wiping (re-mint);
         the reverse order would leave a wipe whose token could wipe again — the fail-closed
         direction is the correct one.
    A MISSING marker file is an empty history; a CORRUPT/unreadable one refuses `E_STORAGE` —
    the single-use property cannot be checked, so the wipe cannot proceed (never a silent reset).
    Residual, stated: the marker check and the write are not one atomic file operation, so two
    SIMULTANEOUS same-token requests can both pass step 2 — step 3's lock + empty-state re-check
    closes that window (the loser deletes nothing and reports its own affected-row counts).

FK NOTE (kept tables that reference wiped rows)
    `support_inbox_items.task_id` FKs `tasks.id` and the inbox is NOT a member (the owner's
    customer mail is not this endpoint's to delete).  On PostgreSQL the task deletes would fail
    with an IntegrityError on a referenced row, so the same transaction NULLs those references
    first: the inbox rows keep their content, they lose only a pointer to a task that no longer
    exists.  With no reference left, nothing dangles.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import tempfile
import time
import zipfile
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import delete, func, select, update
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

from app.agents.policy_firewall import configured_approval_secret, verify_approval
from app.api.deps import get_db, refusal_guard, verify_api_key
from app.core.errors import (
    E_CONFIG,
    E_DB,
    E_POLICY_DENIED,
    E_STORAGE,
    E_VALIDATION,
    PolsiaError,
)
from app.core.timeutil import iso_utc
from app.models.chat import ChatMessage
from app.models.company import CompanyConfig
from app.models.finance import RevenueSnapshot
from app.models.invoice import Invoice
from app.models.memory import MemoryEntry
from app.models.report import ActivityLog
from app.models.schedule import TaskSchedule
from app.models.support import SupportInboxItem
from app.models.task import AgentRun, Task

router = APIRouter(prefix="/data", tags=["data"], dependencies=[Depends(verify_api_key)])

#: The export/wipe members (V10-D2), in the brief's order.  table name -> ORM class.
MEMBER_MODELS: dict[str, type[Any]] = {
    "tasks": Task,
    "agent_runs": AgentRun,
    "activity_log": ActivityLog,
    "revenue_snapshots": RevenueSnapshot,
    "invoices": Invoice,
    "task_schedules": TaskSchedule,
    "memory_entries": MemoryEntry,
    "chat_messages": ChatMessage,
}

#: Tabular members export as CSV; the rest as JSON (D2's split).
CSV_MEMBERS: frozenset[str] = frozenset(
    {"tasks", "agent_runs", "activity_log", "revenue_snapshots", "invoices", "task_schedules"}
)

#: Wipe order: FK children first (chat_messages/agent_runs -> tasks), then the rest.
WIPE_ORDER: tuple[str, ...] = (
    "chat_messages",
    "agent_runs",
    "tasks",
    "activity_log",
    "revenue_snapshots",
    "invoices",
    "task_schedules",
    "memory_entries",
)

#: What the wipe provably leaves alone (the response serves this so a client need not assume).
KEPT: tuple[str, ...] = ("company_config", "deepseek_key_store")

#: The action fingerprint the owner mints for.  The digest binds the constant to the frozen
#: member set: adding/removing a member changes the string, so a token minted before the change
#: fails verification instead of approving a wider wipe than it named.
DATA_WIPE_ACTION_FINGERPRINT = "polsia-data-wipe:v1:" + hashlib.sha256(
    "|".join(sorted(MEMBER_MODELS)).encode("utf-8")
).hexdigest()[:12]

#: The single-use consumed-token record (runtime state; sibling of the key store it survives).
_CONSUMED_TOKENS_PATH: Path = (
    Path(__file__).resolve().parents[3] / "data" / "data_wipe_consumed.json"
)

#: How many consumed records are kept (bounded growth; the file is audit, not a log sink).
_CONSUMED_KEEP = 50

logger = logging.getLogger(__name__)

#: The export archive is a temp file in the system scratch dir (U2-10): named so an operator can
#: see whose file it is, `.zip` so tooling knows the format, removed after the response is sent.
EXPORT_TEMP_PREFIX = "polsia-data-export-"
EXPORT_TEMP_SUFFIX = ".zip"


class DataWipeResponse(BaseModel):
    """The DELETE wire shape: the dry run answers `would_delete`, the wipe answers `deleted`."""

    ok: bool = True
    dry_run: bool
    action_fingerprint: str
    would_delete: dict[str, int] | None = None
    deleted: dict[str, int] | None = None
    total_rows: int
    kept: list[str]
    how_to_execute: str | None = None


# ── value serialization ──────────────────────────────────────────────────────────────────────


def _json_safe(value: Any) -> Any:
    """One at-rest value as JSON-safe data (`datetime`/`date` -> ISO-8601, `Decimal` -> str).

    `Decimal` stays a STRING on purpose: money exported through a float loses cents, and this
    surface exists to move the owner's data without silent conversion.
    """
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def _csv_cell(value: Any) -> str:
    """One CSV cell: `None` -> empty (no null in CSV), dict/list -> JSON, else ISO/str."""
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (dict, list)):
        return json.dumps(_json_safe(value), ensure_ascii=False)
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def _member_attrs(model: type[Any]) -> list[Any]:
    """The ORM column properties of a member, in table order (`attr.key` is the SAFE attribute).

    The mapper — not `__table__.columns` — is the source: `Task`'s DB column `metadata` is
    mapped as `metadata_json` precisely because `metadata` is reserved, so reading raw column
    keys would hand back Declarative's MetaData object instead of the value.
    """
    return list(sa_inspect(model).mapper.column_attrs)


# ── the export ───────────────────────────────────────────────────────────────────────────────


async def _build_export(db: AsyncSession) -> tuple[Path, int]:
    """Read every member, write MANIFEST.json + the members to a TEMP-FILE zip.

    Returns `(temp file path, total rows)`.  The rows are read ONCE and the manifest is built
    from those same rows (the declared-count rule above); the U2-10 change is only WHERE the
    archive is assembled: via `tempfile.mkstemp` in the system scratch dir instead of one
    in-memory `BytesIO` whose `getvalue()` doubled the payload.  A failure while writing removes
    the temp file before it propagates — this endpoint never leaks a scratch file.
    """
    manifest_members: list[dict[str, Any]] = []
    member_files: list[tuple[str, str]] = []  # (archive name, text payload)

    for table, model in MEMBER_MODELS.items():
        attrs = _member_attrs(model)
        result = await db.execute(select(model).order_by(model.id))
        rows = result.scalars().all()

        if table in CSV_MEMBERS:
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            columns = [attr.columns[0].name for attr in attrs]
            writer.writerow(columns)
            for row in rows:
                writer.writerow([_csv_cell(getattr(row, attr.key)) for attr in attrs])
            archive_name = f"{table}.csv"
            member_files.append((archive_name, buffer.getvalue()))
        else:
            columns = [attr.columns[0].name for attr in attrs]
            payload = [
                {attr.columns[0].name: _json_safe(getattr(row, attr.key)) for attr in attrs}
                for row in rows
            ]
            archive_name = f"{table}.json"
            member_files.append((archive_name, json.dumps(payload, indent=2, default=str)))

        manifest_members.append(
            {
                "name": archive_name,
                "table": table,
                "format": "csv" if table in CSV_MEMBERS else "json",
                "row_count": len(rows),
                "columns": columns,
            }
        )

    total_rows = sum(member["row_count"] for member in manifest_members)
    manifest = {
        "product": "polsia",
        "export_version": 1,
        "exported_at": iso_utc(datetime.now(UTC)),
        "members": manifest_members,
        "total_rows": total_rows,
        "not_included": [
            "the DeepSeek key store (backend/data/deepseek_api_key.json) — a file, never a member",
            ".env contents — secrets are not exported",
        ],
    }

    try:
        fd, raw_path = tempfile.mkstemp(prefix=EXPORT_TEMP_PREFIX, suffix=EXPORT_TEMP_SUFFIX)
    except OSError as exc:
        raise PolsiaError(
            E_STORAGE,
            f"the export ZIP temp file could not be created in the system scratch dir "
            f"({type(exc).__name__}: {exc})",
            how_to_fix=f"check that {tempfile.gettempdir()} exists and is writable, and that "
                       "the disk has room, then retry the export",
            context={"scratch_dir": tempfile.gettempdir()},
        ) from exc
    path = Path(raw_path)
    os.close(fd)  # Windows: release the mkstemp handle so ZipFile can reopen the path
    try:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("MANIFEST.json", json.dumps(manifest, indent=2))
            for archive_name, member_text in member_files:
                archive.writestr(archive_name, member_text)
    except OSError as exc:
        _remove_export_tempfile(path)
        raise PolsiaError(
            E_STORAGE,
            f"the export ZIP could not be written to {path} ({type(exc).__name__}: {exc})",
            how_to_fix=f"check that {path.parent} is writable and the disk has room, then "
                       "retry the export",
            context={"path": str(path)},
        ) from exc
    except BaseException:
        # Anything else (cancellation, a serialization failure): the temp file still goes away.
        _remove_export_tempfile(path)
        raise
    return path, total_rows


def _remove_export_tempfile(path: Path) -> None:
    """Best-effort removal of the export temp archive (never raises: a cleanup failure must not
    turn an already-served export into an error inside the response or the background task)."""
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:  # pragma: no cover - unlink on the scratch dir is the only caller
        logger.warning("could not remove the export temp file %s (%s)", path, exc)


class ExportFileResponse(FileResponse):
    """`FileResponse` whose cleanup background runs even when the send raises.

    WHY the subclass: starlette 0.41.3 runs `await self.background()` only on the NORMAL
    completion path of `FileResponse.__call__` (measured in the installed source) — a client
    disconnect mid-body propagates the exception and SKIPS the background task, which would
    leak one temp archive per aborted download.  This subclass takes ownership of the
    background callable (`self.background` is nulled before delegating) and runs it in a
    `finally`, exactly once, on every exit — success, refusal, or disconnect.  The cleanup
    callable itself is `_remove_export_tempfile` (idempotent, never raises).
    """

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        cleanup = self.background
        self.background = None  # we run it ourselves, exactly once
        try:
            await super().__call__(scope, receive, send)
        finally:
            if cleanup is not None:
                await cleanup()


@router.get("/export")
async def export_data_route(db: AsyncSession = Depends(get_db)) -> ExportFileResponse:
    """The full-data ZIP (manifest + 8 members) — a temp file built and counted before the

    first byte, streamed from disk by `FileResponse`, removed by the background task once the
    response has been sent (U2-10) — including when the send itself fails (subclass above).
    """
    with refusal_guard():
        path, total_rows = await _build_export(db)
    filename = f"polsia-data-export-{date.today().isoformat()}.zip"
    return ExportFileResponse(
        path,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Polsia-Row-Count": str(total_rows),
        },
        background=BackgroundTask(_remove_export_tempfile, path),
    )


# ── the single-use consumed-token record ─────────────────────────────────────────────────────


def _read_consumed() -> list[dict[str, Any]]:
    """The consumed records: `[]` when the file was never written, `E_STORAGE` when unreadable."""
    path = _CONSUMED_TOKENS_PATH
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        consumed = data["consumed"]
        if not isinstance(consumed, list):
            raise ValueError("consumed is not a list")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise PolsiaError(
            E_STORAGE,
            f"the data-wipe single-use marker file is unreadable ({type(exc).__name__}), so a "
            "confirm token's single-use property cannot be checked",
            how_to_fix=f"repair or remove {path} — removing it also removes the replay history, "
                       "so only do that if you accept an already-used token could be presented "
                       "again within its TTL",
            context={"path": str(path)},
        ) from exc
    return [entry for entry in consumed if isinstance(entry, dict)]


def _persist_consumed(consumed: list[dict[str, Any]]) -> None:
    """Append-persist the (bounded) consumed record: temp file + `os.replace`, fail-closed."""
    path = _CONSUMED_TOKENS_PATH
    trimmed = consumed[-_CONSUMED_KEEP:]
    payload = json.dumps({"version": 1, "consumed": trimmed}, indent=2)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        raise PolsiaError(
            E_STORAGE,
            f"could not persist the data-wipe consumed-token record ({type(exc).__name__}) — "
            "refusing to wipe, because the token's single-use property could not be made durable",
            how_to_fix=f"check that {path.parent} is writable, then retry the same command",
            context={"path": str(path)},
        ) from exc


# ── token verification ───────────────────────────────────────────────────────────────────────


def _verify_wipe_token(secret: bytes, token: str) -> str:
    """Parse+verify a presented wipe token; return its signature half.  Refuses BY NAME.

    The parse is the approvals parser (`"{exp}.{sig}"`, integer expiry first so an expired token
    gets its own message, then the firewall's HMAC verify — `verify_approval`).
    """
    exp_part, _, sig_part = token.partition(".")
    if not exp_part or not sig_part or "." in sig_part:
        raise PolsiaError(
            E_VALIDATION,
            "the confirm_token is not a Polsia approval token (expected '{expiry}.{signature}')",
            how_to_fix="mint one via POST /api/v1/approvals with the action_fingerprint from "
                       "DELETE /api/v1/data?dry_run=true and pass the token back verbatim",
            context={"reason": "malformed"},
        )
    try:
        exp = int(exp_part)
    except ValueError:
        raise PolsiaError(
            E_VALIDATION,
            "the confirm_token is not a Polsia approval token (its expiry is not an integer)",
            how_to_fix="pass the confirm_token value exactly as POST /api/v1/approvals returned it",
            context={"reason": "malformed"},
        ) from None
    if int(time.time()) > exp:
        raise PolsiaError(
            E_POLICY_DENIED,
            "the confirm token expired — nothing was deleted",
            how_to_fix="run DELETE /api/v1/data?dry_run=true and mint a fresh token "
                       "(POST /api/v1/approvals), then retry promptly",
            context={
                "reason": "expired",
                "expired_at": exp,
                "action_fingerprint": DATA_WIPE_ACTION_FINGERPRINT,
            },
        )
    if not verify_approval(secret, token, action_fingerprint=DATA_WIPE_ACTION_FINGERPRINT):
        raise PolsiaError(
            E_POLICY_DENIED,
            "the confirm token failed verification: it was not minted by this server for the "
            "data-wipe action, or APPROVAL_SECRET changed since it was minted — nothing was "
            "deleted",
            how_to_fix="mint again via POST /api/v1/approvals with action_fingerprint "
                       f"{DATA_WIPE_ACTION_FINGERPRINT!r} (and restart with the current "
                       "APPROVAL_SECRET if it was rotated)",
            context={
                "reason": "invalid",
                "action_fingerprint": DATA_WIPE_ACTION_FINGERPRINT,
            },
        )
    return sig_part


# ── counts + the wipe ────────────────────────────────────────────────────────────────────────


async def _count_members(db: AsyncSession) -> dict[str, int]:
    """Live row counts of the member tables, by table name (one query each, no cache)."""
    counts: dict[str, int] = {}
    for table, model in MEMBER_MODELS.items():
        result = await db.execute(select(func.count()).select_from(model.__table__))
        counts[table] = int(result.scalar_one())
    return counts


@router.delete("")
async def delete_data_route(
    dry_run: bool = Query(False),
    confirm_token: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
) -> DataWipeResponse:
    """Dry run (no token) or the guarded wipe (single-use confirm token).  Refusals by name."""
    with refusal_guard():
        if dry_run:
            # A read, like the export: never deletes, never touches a token.
            counts = await _count_members(db)
            return DataWipeResponse(
                dry_run=True,
                action_fingerprint=DATA_WIPE_ACTION_FINGERPRINT,
                would_delete=counts,
                total_rows=sum(counts.values()),
                kept=list(KEPT),
                how_to_execute=(
                    "mint the confirm token with POST /api/v1/approvals "
                    f'{{"action_fingerprint": "{DATA_WIPE_ACTION_FINGERPRINT}"}}, then run '
                    "DELETE /api/v1/data?confirm_token=<token>"
                ),
            )

        secret = configured_approval_secret()
        if not secret:
            raise PolsiaError(
                E_CONFIG,
                "the data-wipe confirm channel is not configured: APPROVAL_SECRET is unset, so "
                "no token could ever verify",
                how_to_fix="set APPROVAL_SECRET in .env (see .env.example) and restart the "
                           "service, then mint a token for the data-wipe fingerprint",
                context={"action_fingerprint": DATA_WIPE_ACTION_FINGERPRINT},
            )
        if not confirm_token:
            raise PolsiaError(
                E_VALIDATION,
                "DELETE /api/v1/data deletes every member table and requires a single-use "
                "confirm token; none was supplied",
                how_to_fix="run DELETE /api/v1/data?dry_run=true to read the action_fingerprint "
                           "and the counts, mint a token with POST /api/v1/approvals, then "
                           "retry with ?confirm_token=<token>",
                context={"action_fingerprint": DATA_WIPE_ACTION_FINGERPRINT},
            )
        sig = _verify_wipe_token(secret, confirm_token)

        consumed = _read_consumed()
        for entry in consumed:
            if entry.get("fingerprint") == DATA_WIPE_ACTION_FINGERPRINT and entry.get("sig") == sig:
                raise PolsiaError(
                    E_POLICY_DENIED,
                    "this confirm token was already used — it authorized the wipe recorded at "
                    f"{entry.get('consumed_at', 'an earlier time')}; a confirm token is single-use",
                    how_to_fix="run DELETE /api/v1/data?dry_run=true and mint a fresh token, "
                               "then retry with the new confirm_token",
                    context={"reason": "already_used", "consumed_at": entry.get("consumed_at")},
                )

        try:
            # The cross-process mutex: lock the KEPT config row (`FOR UPDATE`; SQLite no-op,
            # where writes serialize anyway).  Everything below is one transaction.
            await db.execute(select(CompanyConfig.id).limit(1).with_for_update())

            counts_before = await _count_members(db)
            total_before = sum(counts_before.values())
            if total_before == 0:
                await db.rollback()
                raise PolsiaError(
                    E_VALIDATION,
                    "there is nothing to delete: every member table is already empty — the "
                    "confirm token was NOT consumed",
                    how_to_fix="nothing to do; run DELETE /api/v1/data?dry_run=true if this is "
                               "unexpected",
                    context={"action_fingerprint": DATA_WIPE_ACTION_FINGERPRINT},
                )

            # Burn the token BEFORE the deletes (fail-closed): a crash mid-wipe costs a re-mint,
            # never a wipe whose token could wipe again.  Still inside the lock.
            consumed.append(
                {
                    "sig": sig,
                    "fingerprint": DATA_WIPE_ACTION_FINGERPRINT,
                    "consumed_at": iso_utc(datetime.now(UTC)),
                }
            )
            _persist_consumed(consumed)

            # Kept tables that FK wiped rows: clear the reference, keep the row (module docstring).
            await db.execute(
                update(SupportInboxItem)
                .where(SupportInboxItem.task_id.is_not(None))
                .values(task_id=None)
            )

            deleted: dict[str, int] = {}
            for table in WIPE_ORDER:
                model = MEMBER_MODELS[table]
                result = await db.execute(delete(model))
                rowcount = result.rowcount
                deleted[table] = (
                    int(rowcount)
                    if isinstance(rowcount, int) and rowcount >= 0
                    else counts_before[table]  # dialect cannot report it: the locked pre-count
                )
            await db.commit()
        except SQLAlchemyError as exc:
            await db.rollback()
            raise PolsiaError(
                E_DB,
                f"the data wipe failed mid-transaction ({exc.__class__.__name__}); the "
                "transaction was rolled back — the confirm token is consumed (fail-closed)",
                how_to_fix="check the database, then dry-run and mint a fresh token to retry",
                context={"action_fingerprint": DATA_WIPE_ACTION_FINGERPRINT},
            ) from exc

    return DataWipeResponse(
        dry_run=False,
        action_fingerprint=DATA_WIPE_ACTION_FINGERPRINT,
        deleted=deleted,
        total_rows=sum(deleted.values()),
        kept=list(KEPT),
    )
