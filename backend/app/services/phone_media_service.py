"""Phone MEDIA — the frozen `/api/v1/phone/media` read surface over `mms_part.blob`.

MEASURED SCHEMA (built to this, not to a guess — read-only dump, 2026-10-18)
    mms_part(part_id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL, sequence_num INTEGER,
             content_id TEXT, content_type TEXT, text TEXT, name TEXT, charset INTEGER,
             blob BLOB, size INTEGER NOT NULL DEFAULT 0)
    mms(message_id INTEGER PRIMARY KEY, thread_id INTEGER NOT NULL, timestamp INTEGER NOT NULL, …)
    index: mms_part_message_id_idx on (message_id)

    THREAD JOIN — supported, measured.  `mms_part.message_id -> mms.message_id -> mms.thread_id`
    resolved 1844/1844 blob-bearing parts on this machine's store, 0 orphans, so `thread_id` is a
    real scope and the list route does not need to degrade to "serve by part id only".

    CONTENT TYPE — a real column (`mms_part.content_type`), never NULL/empty on a blob row.
    Measured values: text/plain, application/smil, image/jpeg, audio/mp4, image/png, image/gif.
    It is used for the `Content-Type` header but SANITIZED first (`_safe_content_type`): a store
    value is data, and a value carrying CR/LF would otherwise be a response-splitting primitive.
    The response filename is built from `part_id` + a fixed extension map, never from the row's
    `name` (an untrusted string that could carry a path or quotes).

    BLOBS ARE RAW.  First bytes measured on this store: JPEG `ffd8ffdb`, PNG `89504e47`, audio/mp4
    `00000018` — the container bytes as the phone sent them, no gzip/compression wrapper, so
    `len(blob)` is the payload's true byte count and `size == length(blob)` held on all 1844 rows
    (0 mismatches).  The stream re-checks that equality per row anyway: a store that disagrees
    with itself must not be served as a complete download.

    REAL MEDIA PREDICATE — `blob IS NOT NULL AND size > 0`.  The store keeps MMS text and SMIL
    parts as ZERO-LENGTH blobs (1670 of 1844 blob rows here; 174 real media parts, 44.31 MiB,
    max part 2,308,674 bytes).  Those zero-length rows are message bodies — `phone_link_service
    .messages` already serves them — and listing them as attachments would offer the UI a
    download that can only 404.  A blob-less row whose `size` column is non-zero also exists in
    the fixture builder's own seed; `blob IS NOT NULL` excludes it.

WHY THIS MODULE IS SEPARATE FROM `phone_link_service`
    That module owns the message/call/code reads and its `REQUIRED_COLUMNS` contract (settled,
    read-only this wave).  Media has its own failure classes — a missing `blob` column, a row that
    lies about its size, an attachment over the stream cap — and its own refusal vocabulary.  It
    reuses that module's PUBLIC store plumbing only (`resolve_store_dir`, `open_ro`,
    `filetime_to_local`), so there is exactly ONE definition of where the store lives and how a
    FILETIME becomes local ISO-8601.

REFUSALS, BY NAME
    E_PHONE_UNAVAILABLE 503  store absent / phone.db unreadable / `blob`+`size` columns missing /
                             a row whose `size` disagrees with its blob (a corrupt store is an
                             unavailable store — never a silently short download).
    E_NOT_FOUND         404  no such part, or a part with no stored bytes (NULL/empty blob).
    E_VALIDATION        422  a part larger than `MEDIA_MAX_BYTES` (the documented stream cap).
    Every route raises through `PolsiaError`; the shipped plumbing (`deps.refusal_guard` →
    `main`'s coded handlers) supplies the envelope and `X-Polsia-Error-Code`.  Nothing here
    hand-rolls a response.

WHAT IS NEVER LOGGED
    No blob byte, no filename, no `name` column: `logger` records the part id and byte counts only.
    (The download's bytes are the owner's private media; a log line is not a place for them.)
"""
from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from app.core.errors import E_NOT_FOUND, E_PHONE_UNAVAILABLE, E_VALIDATION, PolsiaError
from app.services import phone_link_service as link

logger = logging.getLogger(__name__)

#: The list window.  Frozen at 50 by the V18 plan: a thread's newest 50 attachments, and
#: `truncated: true` when the store holds more (the cap is NAMED, never silent).
MEDIA_LIST_LIMIT = 50

#: The stream cap.  Measured against this machine's store (2026-10-18): 174 media parts,
#: 44.31 MiB total, LARGEST single part 2,308,674 bytes (~2.2 MiB), only 7 parts over 1 MiB.
#: 25 MiB is ~11x the measured maximum: generous enough that no real attachment is refused,
#: tight enough that a hostile/corrupt row cannot make the API allocate without bound.
MEDIA_MAX_BYTES = 25 * 1024 * 1024

CONTENT_TYPE_FALLBACK = "application/octet-stream"

#: Extension per content type — the ONLY source of the download filename's suffix.  A type that
#: is absent here (or unparseable) is served as `.bin`.
EXTENSION_BY_CONTENT_TYPE: dict[str, str] = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/heic": "heic",
    "image/bmp": "bmp",
    "audio/mp4": "m4a",
    "audio/mpeg": "mp3",
    "audio/amr": "amr",
    "audio/ogg": "ogg",
    "audio/wav": "wav",
    "video/mp4": "mp4",
    "video/3gpp": "3gp",
    "video/quicktime": "mov",
    "text/plain": "txt",
    "text/x-vcard": "vcf",
    "application/pdf": "pdf",
    "application/smil": "smil",
}

#: `type/subtype` in RFC 6838 token form.  Deliberately strict: anything else (a type with a CR,
#: a parameterized value, an empty string) falls back to octet-stream rather than reaching a header.
_MIME_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,63}/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,63}$"
)

#: The list read: the measured join, the media predicate, newest first.  `LIMIT ?` is always
#: `cap + 1` so truncation is DETECTED rather than assumed from an exact-size page.
_MEDIA_LIST_SQL = """
SELECT mp.part_id AS part_id,
       mp.content_type AS content_type,
       mp.size AS size,
       mo.thread_id AS thread_id,
       mo.timestamp AS ts
FROM mms_part mp
JOIN mms mo ON mo.message_id = mp.message_id
WHERE mp.blob IS NOT NULL AND mp.size > 0 AND mo.thread_id = ?
ORDER BY mo.timestamp DESC, mp.part_id DESC
LIMIT ?
"""

#: One part, bytes included.  The `blob` is read only here — never in a list query, so a list of
#: 50 attachments never pulls 50 blobs through the driver.
_ONE_PART_SQL = """
SELECT mp.part_id AS part_id,
       mp.message_id AS message_id,
       mp.content_type AS content_type,
       mp.blob AS blob,
       mp.size AS size,
       mo.thread_id AS thread_id,
       mo.timestamp AS ts
FROM mms_part mp
LEFT JOIN mms mo ON mo.message_id = mp.message_id
WHERE mp.part_id = ?
"""


# ── refusals ───────────────────────────────────────────────────────────────────────────────────


def _unavailable(
    message: str, *, how_to_fix: str = "", context: dict[str, Any] | None = None
) -> PolsiaError:
    """The store-fault refusal (`E_PHONE_UNAVAILABLE` 503) with the measured detail attached.

    `context` is a NAMED parameter, not `**kwargs`: passing `context={...}` into a `**context`
    catcher nests it one level deep (`{"context": {...}}`) and every caller that reads
    `error.context.part_id` then misses — a real defect this lane's own lying-size pin caught.
    """
    return PolsiaError(
        E_PHONE_UNAVAILABLE,
        message,
        how_to_fix=how_to_fix
        or (
            "mount the Phone Link store read-only (PHONE_LINK_DB_DIR; compose binds it to "
            f"{link.CONTAINER_STORE_DIR}:ro) and start the Phone Link app so it keeps syncing"
        ),
        context=dict(context or {}),
    )


def _not_found(
    part_id: int, *, reason: str, context: dict[str, Any] | None = None
) -> PolsiaError:
    """A part that does not exist AS A DOWNLOAD — named 404, never an empty 200."""
    return PolsiaError(
        E_NOT_FOUND,
        f"phone media part {int(part_id)} has no downloadable bytes ({reason})",
        how_to_fix=(
            "list the thread first (GET /api/v1/phone/media?thread_id=<int>) and use a part_id "
            "from that response; MMS text/SMIL body parts carry no attachment bytes"
        ),
        context={"part_id": int(part_id), "reason": reason, **dict(context or {})},
    )


# ── store plumbing (public API of phone_link_service, one definition of the store) ────────────


def _open_phone_db() -> sqlite3.Connection:
    """`phone.db` read-only, or a named 503 (absent mount / missing file / locked / unreadable).

    `resolve_store_dir` is the single store-location authority (config override + the measured
    Windows glob); `open_ro` is the single read-only opener.  Both are public on the settled
    module, so this lane adds no second definition of either.
    """
    root = link.resolve_store_dir()  # raises E_PHONE_UNAVAILABLE when there is no mount at all
    path = root / "phone.db"
    if not path.exists():
        raise _unavailable(
            f"phone.db is missing from the Phone Link store directory {root}",
            context={"store_dir": str(root), "missing": "phone.db"},
        )
    try:
        return link.open_ro(path)
    except sqlite3.Error as exc:
        raise _unavailable(
            f"phone.db could not be opened read-only ({type(exc).__name__}: {exc})",
            context={"store_dir": str(root), "file": "phone.db"},
        ) from exc


def _rows(sql: str, params: tuple[Any, ...]) -> list[sqlite3.Row]:
    """Run one media read; ANY sqlite fault is a store fault, named, never a 500 traceback.

    This is where a store that dropped `mms_part.blob`/`size` lands: `phone_link_service`'s drift
    check owns the columns IT reads (message_id, content_type, text), not the two media columns, so
    the media read has to name its own drift instead of leaking `OperationalError` as a 500.
    """
    con = _open_phone_db()
    try:
        return con.execute(sql, params).fetchall()
    except sqlite3.Error as exc:
        raise _unavailable(
            f"the phone media read failed ({type(exc).__name__}: {exc}) — the store's mms_part "
            f"table no longer carries the shape this build reads",
            how_to_fix=(
                "the Phone Link app changed its database layout; re-measure `mms_part` "
                "(part_id, message_id, content_type, blob, size) and update "
                "app/services/phone_media_service.py — no media read can be trusted until then"
            ),
            context={"table": "mms_part", "error": type(exc).__name__},
        ) from exc
    finally:
        con.close()


# ── value conversion ─────────────────────────────────────────────────────────────────────────


def _when(ticks: Any) -> str | None:
    """`mms.timestamp` FILETIME -> local ISO-8601 with offset; `0`/NULL -> None (not 1970).

    A part with no `mms` envelope (an orphan the store should not have) has no timestamp; the
    frozen shape allows `when: str | null`, so that is reported as null rather than invented.
    """
    if ticks is None:
        return None
    try:
        return link.filetime_to_local(ticks)
    except ValueError:
        # A corrupt timestamp is not a reason to hide a real attachment; `when` is metadata.
        logger.warning("phone media: part timestamp is not a FILETIME (%r)", ticks)
        return None


def _safe_content_type(raw: Any) -> str:
    """The row's content type when it is a bare RFC 6838 type, else `application/octet-stream`."""
    value = str(raw or "").split(";", 1)[0].strip()
    return value if _MIME_RE.match(value) else CONTENT_TYPE_FALLBACK


def _filename(part_id: int, content_type: str) -> str:
    """`phone-part-<id>.<ext>` — digits and a map-sourced suffix, so no row value reaches a header."""
    extension = EXTENSION_BY_CONTENT_TYPE.get(content_type.split(";", 1)[0].strip(), "bin")
    return f"phone-part-{int(part_id)}.{extension}"


# ── the list ─────────────────────────────────────────────────────────────────────────────────


def media_parts(*, thread_id: int, limit: int = MEDIA_LIST_LIMIT) -> dict[str, Any]:
    """`{"parts": [{part_id, content_type, size_bytes, when, thread_id}], "truncated": bool}`.

    Newest first by the OWNING MMS's `timestamp` (parts carry no timestamp of their own).  The read
    asks for `cap + 1` rows: `truncated` is then a measured fact ("the store had more") rather than
    a guess that a full page means more.  An unknown/empty thread is an honest empty list — the
    store answered, it simply holds nothing — never a 503.
    """
    capped = max(1, min(int(limit), MEDIA_LIST_LIMIT))
    rows = _rows(_MEDIA_LIST_SQL, (int(thread_id), capped + 1))
    truncated = len(rows) > capped
    return {
        "parts": [
            {
                "part_id": int(row["part_id"]),
                "content_type": _safe_content_type(row["content_type"]),
                "size_bytes": int(row["size"]),
                "when": _when(row["ts"]),
                "thread_id": int(row["thread_id"]),
            }
            for row in rows[:capped]
        ],
        "truncated": truncated,
    }


# ── one part, bytes included ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MediaPart:
    """One downloadable attachment: the verified bytes plus the headers they may be served with."""

    part_id: int
    content_type: str
    size_bytes: int
    data: bytes
    thread_id: int | None
    when: str | None

    def disposition(self) -> str:
        """`attachment; filename="phone-part-<id>.<ext>"` — built from id + fixed extension map."""
        return f'attachment; filename="{_filename(self.part_id, self.content_type)}"'


def read_part(part_id: int, *, max_bytes: int | None = None) -> MediaPart:
    """The verified bytes for one part, or a named refusal.

    Check order (each step is cheaper and more caller-facing than the next):
      1. the row must exist with a NON-EMPTY blob   -> 404 E_NOT_FOUND
      2. its DECLARED size must be within the cap   -> 422 E_VALIDATION
      3. its declared size must equal `len(blob)`   -> 503 E_PHONE_UNAVAILABLE (corrupt store)
    Only then are the bytes handed back.  Serving a short body as if it were complete is the one
    failure a download route must never have: the client would store a truncated photo as the
    owner's real photo.
    """
    cap = MEDIA_MAX_BYTES if max_bytes is None else int(max_bytes)
    rows = _rows(_ONE_PART_SQL, (int(part_id),))
    if not rows:
        raise _not_found(part_id, reason="no mms_part row")
    row = rows[0]

    blob = row["blob"]
    if blob is None or not isinstance(blob, (bytes, bytearray, memoryview)):
        raise _not_found(part_id, reason="the part carries no stored blob")
    declared = int(row["size"] or 0)
    actual = len(blob)
    if declared <= 0 or actual == 0:
        raise _not_found(part_id, reason="the stored blob is empty (an MMS text/SMIL body part)")

    if declared > cap:
        raise PolsiaError(
            E_VALIDATION,
            f"phone media part {int(part_id)} is {declared} bytes, over the {cap}-byte stream cap",
            how_to_fix=(
                "the attachment is larger than this surface serves; export it with the Hermes "
                "`phone-link-bridge` media command, or raise MEDIA_MAX_BYTES deliberately"
            ),
            context={"part_id": int(part_id), "size_bytes": declared, "max_bytes": cap},
        )

    if declared != actual:
        raise _unavailable(
            f"phone media part {int(part_id)} declares {declared} bytes but its blob holds "
            f"{actual} — the store row is corrupt, so no complete download can be served",
            how_to_fix=(
                "this is a Phone Link store integrity fault, not a request error: let the Phone "
                "Link app re-sync the message, then retry; nothing is served truncated by design"
            ),
            context={
                "part_id": int(part_id),
                "declared_size": declared,
                "actual_bytes": actual,
                "reason": "size_mismatch",
            },
        )

    content_type = _safe_content_type(row["content_type"])
    thread = row["thread_id"]
    logger.debug(
        "phone media part %s served (%s bytes, %s)", int(part_id), actual, content_type
    )
    return MediaPart(
        part_id=int(part_id),
        content_type=content_type,
        size_bytes=actual,
        data=bytes(blob),
        thread_id=int(thread) if thread is not None else None,
        when=_when(row["ts"]),
    )


__all__ = [
    "CONTENT_TYPE_FALLBACK",
    "EXTENSION_BY_CONTENT_TYPE",
    "MEDIA_LIST_LIMIT",
    "MEDIA_MAX_BYTES",
    "MediaPart",
    "media_parts",
    "read_part",
]
