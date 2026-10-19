"""Task image attachments (V25/L1) — the storage layer behind vision input.

ONE image = one row in `task_attachments` + one blob under
`<settings.attachments_dir>/task-<task_id>/<uuid4>.<ext>`.  Nothing about the operator's file
name decides where a byte is written or read: `filename` is display data, `stored_name` is
server-generated and is the ONLY name ever joined into a path (and even then it is re-checked as
a single path component and re-resolved inside the task directory — two independent guards,
because path traversal is not a bug you get to fix twice).

THE CAPS (documented here, enforced here, and repeated by the DeepSeek runtime as defence in
depth).  A request over any of them is a NAMED `E_VALIDATION` refusal — never a silent truncation
and never a half-written file:

    * type      PNG / JPEG / WebP, decided by MAGIC BYTES (sniffing), never the extension and
                never the client's `Content-Type`;
    * per file  <= 5 MiB (`MAX_UPLOAD_BYTES`);
    * per task  <= 4 attachments (`MAX_PER_TASK`);
    * per task  <= 8 MiB of image bytes in total (`MAX_TASK_TOTAL_BYTES`) — the same bound the
                runtime applies to ONE request, so a task the upload route accepted can always
                be sent to the model.

`image_parts()` is the bridge to `deepseek_runtime.invoke(images=...)`: it re-reads each recorded
blob, re-sniffs it (a file swapped on disk after upload is not the image the row recorded) and
returns `{mime, data_uri, size_bytes}`.  A blob that vanished or no longer sniffs as its recorded
type is `E_UPSTREAM` (the record is intact, the object behind it is not), never an empty list that
would let a run answer blind about an image the operator attached.

Storage is LOCAL to the process's data volume.  That is deliberate and documented: this product
runs one backend container with one data volume; a multi-replica deployment would need shared
storage before it could upload on one replica and read on another.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.errors import E_CONFIG, E_NOT_FOUND, E_UPSTREAM, E_VALIDATION, PolsiaError
from app.models.task import Task, TaskAttachment

logger = logging.getLogger(__name__)

# ── caps (the documented contract; tests pin every one) ──────────────────────────────────────
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_PER_TASK = 4
MAX_TASK_TOTAL_BYTES = 8 * 1024 * 1024
DEFAULT_ATTACHMENTS_DIR = "data/attachments"

#: MIME -> storage extension.  The extension is DERIVED from the sniffed type (never the
#: uploaded name): `photo.jpeg` and `photo.PNG` both land on the canonical form.
_EXTENSIONS: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
}
ALLOWED_MIME_TYPES: tuple[str, ...] = tuple(_EXTENSIONS)
ALLOWED_MIME_TEXT = "PNG, JPEG or WebP"

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"
#: The bytes that matter for WEBP: `RIFF` at 0 and the `WEBP` form type at 8.  A bare RIFF
#: container (WAV/AVI) is NOT an image and must not pass.
_WEBP_RIFF = b"RIFF"
_WEBP_FORM = b"WEBP"

_STORED_NAME_RE = re.compile(r"^[0-9a-f]{32}\.(?:png|jpg|webp)$")
#: Uploads are read in bounded chunks; the route never materialises more than the cap plus one
#: chunk, so an oversized file is refused WITHOUT first being buffered whole in memory.
READ_CHUNK_BYTES = 64 * 1024


def sniff_image_mime(head: bytes) -> str | None:
    """The image type implied by the leading BYTES, or None when they are not PNG/JPEG/WebP.

    Magic-byte sniffing is the only type decision this module makes: an HTML file renamed
    `screenshot.png`, or a GIF with a `.png` extension, is refused by name.  Only the first
    bytes are inspected — callers pass the whole payload (cheap enough at <= 5 MiB) and this
    function reads what it needs.
    """
    if not isinstance(head, (bytes, bytearray)) or len(head) < 4:
        return None
    if head.startswith(_PNG_MAGIC):
        return "image/png"
    if head.startswith(_JPEG_MAGIC):
        return "image/jpeg"
    if len(head) >= 12 and head[:4] == _WEBP_RIFF and head[8:12] == _WEBP_FORM:
        return "image/webp"
    return None


def resolve_root() -> Path:
    """The attachments root as an ABSOLUTE path (relative settings resolve against the CWD).

    A root that cannot be created is not discovered until the first write; `save_attachment`
    names that failure `E_CONFIG`.  This function itself performs no I/O.
    """
    raw = str(getattr(settings, "attachments_dir", DEFAULT_ATTACHMENTS_DIR) or "").strip()
    return Path(raw or DEFAULT_ATTACHMENTS_DIR).expanduser().resolve()


def task_dir(task_id: int, *, root: Path | None = None) -> Path:
    """`<root>/task-<id>` — the only directory this module ever writes into or reads from."""
    return (root or resolve_root()) / f"task-{int(task_id)}"


def _ref_value(ref: Any, field: str) -> Any:
    """Read one field from an ORM row OR the dict shape the worker passes through prompts."""
    if isinstance(ref, dict):
        return ref.get(field)
    return getattr(ref, field, None)


def _safe_stored_name(name: Any) -> str:
    """A stored name must be ONE generated file name; anything else is refused as not found.

    Two layers: the strict generated-name regex, and a basename equality check (which would
    also catch Windows separators on a POSIX host).  A row written by hand with `../../x.png`
    therefore cannot read outside the task directory.
    """
    text = str(name or "")
    if (
        not _STORED_NAME_RE.match(text)
        or Path(text).name != text
        or "\\" in text
        or "/" in text
        or text in {".", ".."}
    ):
        raise PolsiaError(
            E_NOT_FOUND,
            f"stored attachment name {text!r} is not a server-generated file name; nothing was read",
            how_to_fix=(
                "this row was not written by the upload route — inspect it, or delete it and "
                "re-upload the image"
            ),
            context={"stored_name": text[:128]},
        )
    return text


def attachment_path(row: Any) -> Path:
    """The absolute path of one attachment's blob — traversal-proof by construction.

    The task directory and the stored name are joined, then the RESULT is resolved and checked
    to still sit inside `<root>/task-<id>`.  A symlink or a crafted row that escapes raises
    `E_NOT_FOUND`, so a caller can never be served a file outside the attachment root.
    """
    task_id = _ref_value(row, "task_id")
    if task_id is None:
        raise PolsiaError(E_NOT_FOUND, "attachment row carries no task_id", context={})
    stored = _safe_stored_name(_ref_value(row, "stored_name"))
    root = resolve_root()
    directory = task_dir(int(task_id), root=root)
    path = directory / stored
    try:
        path.resolve().relative_to(directory.resolve())
    except (ValueError, OSError) as exc:  # pragma: no cover - _safe_stored_name already refuses
        raise PolsiaError(
            E_NOT_FOUND,
            f"stored attachment path for task {task_id} escapes its directory",
            how_to_fix="inspect the task_attachments row; it was not written by the upload route",
            context={"task_id": int(task_id), "stored_name": stored},
        ) from exc
    return path


def _display_filename(raw: Any) -> str:
    """The operator's file name, bounded and reduced to its last component for DISPLAY only."""
    text = str(raw or "").replace("\x00", "").strip()
    text = text.replace("\\", "/").rsplit("/", 1)[-1].strip()
    return (text or "image")[:255]


def _storage_refusal(exc: OSError, *, what: str, path: Path) -> PolsiaError:
    """A write/mkdir/unlink failure at the data volume: `E_CONFIG`, with the real OSError named."""
    return PolsiaError(
        E_CONFIG,
        f"attachments storage: {what} {str(path)!r} failed ({type(exc).__name__}: {exc})",
        how_to_fix=(
            "check ATTACHMENTS_DIR (default data/attachments under the backend working "
            "directory) exists, is writable by the backend user and has free space"
        ),
        context={"what": what, "path": str(path), "reason": type(exc).__name__},
    )


async def _task_usage(db: AsyncSession, task_id: int) -> tuple[int, int]:
    """(count, total_bytes) of this task's attachments — ONE aggregate query, no rows loaded."""
    stmt = select(
        func.count(TaskAttachment.id),
        func.coalesce(func.sum(TaskAttachment.size_bytes), 0),
    ).where(TaskAttachment.task_id == int(task_id))
    count, total = (await db.execute(stmt)).one()
    return int(count or 0), int(total or 0)


async def read_upload(upload: Any) -> bytes:
    """Read a multipart part, refusing over the per-file cap BEFORE buffering the whole file.

    Starlette spools an `UploadFile` to disk above ~1 MiB, so `await upload.read()` alone would
    let a large body fill the temp volume before our cap ever ran.  This reads at most
    `MAX_UPLOAD_BYTES + READ_CHUNK_BYTES` and names the real total it reached; the exact byte
    count of a larger file is deliberately not measured (that would mean reading it all).
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            raise PolsiaError(
                E_VALIDATION,
                (
                    f"attachment upload: the file is at least {total} bytes, over the 5 MB "
                    f"per-image limit ({MAX_UPLOAD_BYTES} bytes)"
                ),
                how_to_fix="resize or compress the image so it is at most 5 MB",
                context={"size_bytes_at_least": total, "limit_bytes": MAX_UPLOAD_BYTES},
            )
        chunks.append(chunk)
    return b"".join(chunks)


async def save_attachment(
    db: AsyncSession,
    *,
    task_id: int,
    filename: str,
    data: bytes,
    declared_mime: str | None = None,
) -> TaskAttachment:
    """Validate, persist the blob and add the row; returns the row (flushed, not committed).

    The route commits (via `get_db`); every refusal happens BEFORE a byte is written, so a
    rejected upload never leaves a file or a row behind.  `declared_mime` is accepted only so
    the caller can pass what the client claimed — it is recorded in the refusal context and
    never trusted for the stored `mime`.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise PolsiaError(
            E_VALIDATION,
            f"attachment upload: expected bytes, got {type(data).__name__}",
            how_to_fix="send the file as multipart/form-data with a `file` part",
        )
    payload = bytes(data)
    if not payload:
        raise PolsiaError(
            E_VALIDATION,
            "attachment upload: the file is empty",
            how_to_fix="choose an image file that has content",
            context={"filename": _display_filename(filename)},
        )
    if len(payload) > MAX_UPLOAD_BYTES:
        raise PolsiaError(
            E_VALIDATION,
            (
                f"attachment upload: {len(payload)} bytes exceeds the 5 MB per-image limit "
                f"({MAX_UPLOAD_BYTES} bytes)"
            ),
            how_to_fix="resize or compress the image so it is at most 5 MB",
            context={
                "filename": _display_filename(filename),
                "size_bytes": len(payload),
                "limit_bytes": MAX_UPLOAD_BYTES,
            },
        )

    mime = sniff_image_mime(payload)
    if mime is None:
        raise PolsiaError(
            E_VALIDATION,
            (
                f"attachment upload: {_display_filename(filename)!r} is not {ALLOWED_MIME_TEXT} "
                "(the bytes were inspected, not the file name or the declared content type)"
            ),
            how_to_fix=f"upload a {ALLOWED_MIME_TEXT} image; other formats are not accepted",
            context={
                "filename": _display_filename(filename),
                "declared_mime": (declared_mime or "")[:100],
                "accepted": list(ALLOWED_MIME_TYPES),
            },
        )

    # Serialise concurrent uploads to ONE task on PostgreSQL BEFORE the count/total checks:
    # those checks are read-then-write, so without a row lock two simultaneous uploads could both
    # observe 3 attachments and both commit (a 5th).  `FOR UPDATE` makes the 4-per-task bound
    # exact on the production database; SQLite renders no locking clause and serialises writers
    # per connection.  The runtime's per-request byte cap remains a hard bound either way.
    await db.execute(select(Task.id).where(Task.id == int(task_id)).with_for_update())

    count, total_bytes = await _task_usage(db, task_id)
    if count >= MAX_PER_TASK:
        raise PolsiaError(
            E_VALIDATION,
            (
                f"task {task_id} already has {count} attachments; the limit is {MAX_PER_TASK} "
                "per task"
            ),
            how_to_fix="remove an image from the task first (DELETE /api/v1/attachments/<id>)",
            context={"task_id": int(task_id), "count": count, "limit": MAX_PER_TASK},
        )
    if total_bytes + len(payload) > MAX_TASK_TOTAL_BYTES:
        raise PolsiaError(
            E_VALIDATION,
            (
                f"task {task_id} already stores {total_bytes} image bytes; adding {len(payload)} "
                f"would exceed the {MAX_TASK_TOTAL_BYTES}-byte per-task total"
            ),
            how_to_fix="remove an image or use smaller images; the whole task must fit the per-run vision budget",
            context={
                "task_id": int(task_id),
                "stored_bytes": total_bytes,
                "incoming_bytes": len(payload),
                "limit_bytes": MAX_TASK_TOTAL_BYTES,
            },
        )

    extension = _EXTENSIONS[mime]
    stored_name = f"{uuid.uuid4().hex}.{extension}"
    root = resolve_root()
    directory = task_dir(int(task_id), root=root)
    path = directory / stored_name
    temp_path = directory / f".{stored_name}.part"

    try:
        directory.mkdir(parents=True, exist_ok=True)
        # Write-then-rename: a crash mid-upload can leave a `.part` file, never a truncated blob
        # that the row would later serve as a valid image.
        temp_path.write_bytes(payload)
        os.replace(temp_path, path)
    except OSError as exc:
        with contextlib.suppress(OSError):  # best-effort cleanup of a partial file
            temp_path.unlink(missing_ok=True)
        raise _storage_refusal(exc, what="write", path=directory) from exc

    row = TaskAttachment(
        task_id=int(task_id),
        filename=_display_filename(filename),
        stored_name=stored_name,
        mime=mime,
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    db.add(row)
    try:
        await db.flush()
    except Exception:
        # The row did not land: remove the blob so storage and the table cannot drift.
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)
        raise
    logger.info(
        "attachment %s stored for task %s (%s, %d bytes)", row.id, task_id, mime, len(payload)
    )
    return row


async def list_attachments(db: AsyncSession, task_id: int) -> list[TaskAttachment]:
    """This task's attachments, OLDEST FIRST (upload order) — a stable order for thumbnails."""
    stmt = (
        select(TaskAttachment)
        .where(TaskAttachment.task_id == int(task_id))
        .order_by(TaskAttachment.id.asc())
    )
    return list((await db.execute(stmt)).scalars().all())


async def get_attachment(db: AsyncSession, attachment_id: int) -> TaskAttachment | None:
    """One attachment by id, or None (the route turns that into `E_NOT_FOUND`)."""
    return await db.get(TaskAttachment, int(attachment_id))


async def read_attachment_bytes(row: Any) -> bytes:
    """The blob's bytes, with an honest upstream refusal when it is gone or unreadable."""
    return _read_bytes(row)


async def delete_attachment(db: AsyncSession, row: TaskAttachment) -> None:
    """Remove the blob and the row together — or refuse with neither half removed.

    A missing blob is NOT an error on delete: the row is the thing the owner asked to remove and
    the file is already gone (the desired end state).  Any other filesystem failure stops the
    delete before the row is touched, so a row never outlives its blob by our own hand.
    """
    path = attachment_path(row)
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise _storage_refusal(exc, what="remove", path=path) from exc
    await db.delete(row)
    await db.flush()


def attachment_ref(row: Any) -> dict[str, Any]:
    """The minimal, JSON-safe descriptor the worker hands to the agent prompt payload."""
    return {
        "id": int(_ref_value(row, "id")),
        "task_id": int(_ref_value(row, "task_id")),
        "filename": str(_ref_value(row, "filename") or ""),
        "mime": str(_ref_value(row, "mime") or ""),
        "stored_name": str(_ref_value(row, "stored_name") or ""),
        "size_bytes": int(_ref_value(row, "size_bytes") or 0),
    }


def attachment_refs(rows: list[Any]) -> list[dict[str, Any]]:
    return [attachment_ref(row) for row in rows]


def image_parts(refs: list[Any] | None) -> list[dict[str, Any]]:
    """Attachment refs -> the runtime's `images=` payload (`mime`, `data_uri`, `size_bytes`).

    Called on the RUN thread (never the event loop — it reads up to 8 MiB from disk).  Every
    blob is re-sniffed: a file replaced on disk after upload must not be sent as the recorded
    type.  The per-task total cap is re-applied here because the runtime's request budget is
    per REQUEST and a caller could hand this function refs that never went through the upload
    route.  A refusal is named — dropping an unreadable image silently would let the agent
    answer as if the operator had never attached anything.
    """
    parts: list[dict[str, Any]] = []
    total = 0
    for ref in refs or []:
        mime = str(_ref_value(ref, "mime") or "")
        if mime not in _EXTENSIONS:
            raise PolsiaError(
                E_VALIDATION,
                f"attachment {_ref_value(ref, 'id')}: recorded mime {mime!r} is not {ALLOWED_MIME_TEXT}",
                how_to_fix="re-upload the image; the row records an unsupported content type",
                context={"attachment_id": _ref_value(ref, "id"), "mime": mime},
            )
        data = _read_bytes(ref)
        sniffed = sniff_image_mime(data)
        if sniffed != mime:
            raise PolsiaError(
                E_UPSTREAM,
                (
                    f"attachment {_ref_value(ref, 'id')}: stored bytes are "
                    f"{sniffed or 'not an image'}, but the row records {mime} — the file changed "
                    "or was corrupted after upload"
                ),
                how_to_fix="delete the attachment and upload it again",
                context={"attachment_id": _ref_value(ref, "id"), "recorded": mime,
                         "sniffed": sniffed},
            )
        if len(data) > MAX_UPLOAD_BYTES:  # pragma: no cover - the upload route cannot produce it
            raise PolsiaError(
                E_VALIDATION,
                f"attachment {_ref_value(ref, 'id')} is {len(data)} bytes, over the 5 MB limit",
                how_to_fix="re-upload a smaller image",
                context={"attachment_id": _ref_value(ref, "id"), "limit_bytes": MAX_UPLOAD_BYTES},
            )
        total += len(data)
        if total > MAX_TASK_TOTAL_BYTES:
            raise PolsiaError(
                E_VALIDATION,
                (
                    f"task {_ref_value(ref, 'task_id')}: image attachments total {total} bytes, "
                    f"over the {MAX_TASK_TOTAL_BYTES}-byte per-request vision budget"
                ),
                how_to_fix="remove an image or use smaller images, then run the task again",
                context={"task_id": _ref_value(ref, "task_id"), "total_bytes": total,
                         "limit_bytes": MAX_TASK_TOTAL_BYTES},
            )
        parts.append({
            "mime": mime,
            "data_uri": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}",
            "size_bytes": len(data),
        })
    return parts


def _read_bytes(ref: Any) -> bytes:
    """The blob's bytes for the SYNC run thread; the row is already in hand (no session needed)."""
    path = attachment_path(ref)
    try:
        return path.read_bytes()
    except FileNotFoundError as exc:
        raise PolsiaError(
            E_UPSTREAM,
            (
                f"attachment {_ref_value(ref, 'id')} is recorded for task "
                f"{_ref_value(ref, 'task_id')} but its file is missing on the data volume"
            ),
            how_to_fix=(
                "the image row is intact but the blob is not; delete the attachment and upload "
                "the image again (or restore the data volume from backup)"
            ),
            context={"attachment_id": _ref_value(ref, "id"), "path": str(path)},
        ) from exc
    except OSError as exc:
        raise PolsiaError(
            E_UPSTREAM,
            f"attachment {_ref_value(ref, 'id')} could not be read ({type(exc).__name__}: {exc})",
            how_to_fix="check permissions and free space on the attachments data volume",
            context={"attachment_id": _ref_value(ref, "id"), "path": str(path)},
        ) from exc


__all__ = [
    "ALLOWED_MIME_TEXT",
    "ALLOWED_MIME_TYPES",
    "DEFAULT_ATTACHMENTS_DIR",
    "MAX_PER_TASK",
    "MAX_TASK_TOTAL_BYTES",
    "MAX_UPLOAD_BYTES",
    "attachment_path",
    "attachment_ref",
    "attachment_refs",
    "delete_attachment",
    "get_attachment",
    "image_parts",
    "list_attachments",
    "read_attachment_bytes",
    "read_upload",
    "resolve_root",
    "save_attachment",
    "sniff_image_mime",
    "task_dir",
]
