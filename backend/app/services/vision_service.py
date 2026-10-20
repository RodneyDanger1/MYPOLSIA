"""V26 / L4 — vision on demand; V27 / L4 — vision memory.  Describe ONE task attachment.

The route (`app.api.v1.vision`) is thin; this module owns the work:

1. RESOLVE the attachment — by `attachment_id`, or by `task_id` when that task has exactly
   ONE image attachment.  Ambiguity (more than one image) is `E_VALIDATION` and says how many
   there are; an unknown id and a task with no images are `E_NOT_FOUND`.
2. REQUIRE the key.  A describe is a paid call unless memory already holds the answer, but the
   route's contract is unchanged: with no DeepSeek key configured the answer is `E_CONFIG`
   naming `DEEPSEEK_API_KEY`, before anything is read from disk or sent anywhere.
3. LOOK UP MEMORY (V27) — `(sha256, model)` in `vision_descriptions`.  A hit returns the STORED
   sentence with `cached=True` and makes ZERO provider calls.  The digest is copied from
   `task_attachments.sha256` (measured at upload); this module only re-hashes if a hand-written
   row carries no usable digest.
4. LOAD its bytes through `task_attachment_service.image_parts([row])` — the existing seam
   that re-reads the blob, re-sniffs the magic bytes and builds the runtime's
   `{mime, data_uri, size_bytes}` payload.  Nothing here re-implements sniffing or path
   building, and the blob's presence is checked up front (a row whose file is gone is
   `E_NOT_FOUND`: the attachment the caller named cannot be described).
5. CALL `deepseek_runtime.invoke(...)` — the public seam — with a fixed, honest prompt at the
   deployment's vision model.  No tools are passed: a describe is one plain completion.
6. STORE the measurement (V27) — attachment_id, task_id, sha256, description, model, tokens,
   created_at — and return `{description, model, tokens, cached=False}`.

The cache key is `(sha256, model)` because a description measured by a different model is not
this model's measurement; a row from another model is a miss and is re-measured, never served
as if it were the same thing.  A hit writes no new row, so repeated describes of one image do
not grow the table.

WHAT THIS MODULE NEVER DOES: it never writes the TASK.  `Task.result_summary` and the
attachment row are left untouched (pinned by
`test_describe_never_overwrites_the_task_result_or_the_attachment_row` and
`test_describe_with_memory_still_never_overwrites_the_task_result`).  The description's only
storage is its own `vision_descriptions` row.  `tokens` is measured-or-null: the runtime's
envelope reports `input_tokens`/`output_tokens` per run, and if either is absent the total is
`None` (never a fabricated 0) — stored as NULL and served as null, including from a cache hit.

Refusals reuse the repo's existing codes only:
  * `E_CONFIG`     — no DeepSeek key configured; the message names `DEEPSEEK_API_KEY`;
  * `E_NOT_FOUND`  — unknown attachment/task, a task with no images, a vanished blob;
  * `E_VALIDATION` — bad/ambiguous body, not an image;
  * `E_UPSTREAM`   — the provider answered non-2xx or not at all (status in the context) or
                     returned an off-contract/empty answer.  The key is scrubbed from every
                     message this module builds.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import claude_cli, deepseek_runtime  # noqa: F401  (deepseek_runtime is the seam doc)
from app.agents.deepseek_runtime import invoke
from app.core.errors import (
    E_CLI_JSON,
    E_CLI_TIMEOUT,
    E_CONFIG,
    E_DEEPSEEK_KEY,
    E_NOT_FOUND,
    E_UPSTREAM,
    E_VALIDATION,
    ERROR_HINTS,
    PolsiaError,
)
from app.models.task import Task
from app.models.vision import VisionDescriptionRow
from app.services import secret_store
from app.services import task_attachment_service as attachments

logger = logging.getLogger(__name__)

#: The deployment's vision model (the same model the task runner uses).  It is passed
#: EXPLICITLY so a future change to the agent default cannot silently change what this route
#: calls; the response reports the model the provider actually served.
VISION_MODEL = "deepseek-flash"

#: The environment variable the refusal names when nothing is configured.
DEEPSEEK_ENV_VAR = "DEEPSEEK_API_KEY"

#: Fixed and honest: describe what is visible, never invent what is not.  This is the whole
#: prompt — the image arrives as the same user turn's `image_url` part, built by the runtime.
VISION_PROMPT = (
    "Describe this image factually, for an operator who cannot open it. Say what is visible: "
    "the subject, the colours, any legible text, and the layout. State only what the image "
    "shows; if something is unclear or unreadable, say so instead of guessing."
)

#: The provider body echoed into a refusal is bounded; the runtime already redacts the key,
#: and `_scrub` below re-checks (defence in depth, never the key in a message).
_DETAIL_LIMIT = 300

#: An attachment row carries the digest measured at upload; a 64-hex value is used as-is and
#: is never recomputed (the brief: do not re-hash inside the describe path if the row has it).
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class VisionDescription:
    """The model's answer plus its provenance.  `tokens is None` means UNMEASURED.

    `cached` is part of the measurement's story, not decoration: `True` means this sentence was
    read from `vision_descriptions` and the provider was NOT called for this request.
    """

    description: str
    model: str
    tokens: int | None
    cached: bool = False
    #: The `vision_descriptions` row this sentence lives in (the row just written, or the row
    #: the cache served).  `None` only if a caller builds the dataclass directly.
    description_id: int | None = None
    created_at: datetime | None = None


def _scrub(text: str, secret: str) -> str:
    """Defence in depth: no message this module builds may carry the key verbatim."""
    if secret and secret in text:
        return text.replace(secret, "(key hidden)")
    return text


def _refuse(code: str, message: str, how_to_fix: str = "", **context: Any) -> PolsiaError:
    return PolsiaError(
        code, message, how_to_fix=how_to_fix or ERROR_HINTS.get(code, ""), context=context
    )


# ── resolution ──────────────────────────────────────────────────────────────────────────────

async def _resolve_attachment(
    db: AsyncSession, *, attachment_id: int | None, task_id: int | None
) -> Any:
    """The one attachment to describe — or a named refusal explaining why there is none."""
    if attachment_id is not None and task_id is not None:
        raise _refuse(
            E_VALIDATION,
            "vision describe: pass exactly one of attachment_id or task_id, not both",
            attachment_id=attachment_id,
            task_id=task_id,
        )
    if attachment_id is None and task_id is None:
        raise _refuse(
            E_VALIDATION,
            "vision describe: pass exactly one of attachment_id or task_id — neither was supplied",
        )

    if attachment_id is not None:
        row = await attachments.get_attachment(db, attachment_id)
        if row is None:
            raise _refuse(
                E_NOT_FOUND,
                f"vision describe: attachment {attachment_id} does not exist",
                attachment_id=attachment_id,
            )
        return _require_describable(row)

    # parse_describe_body guarantees exactly one id, so task_id is non-None on this branch.
    assert task_id is not None
    task = await db.get(Task, int(task_id))
    if task is None:
        raise _refuse(
            E_NOT_FOUND,
            f"vision describe: task {task_id} does not exist",
            task_id=task_id,
        )
    rows = [
        row
        for row in await attachments.list_attachments(db, task.id)
        if str(row.mime or "") in attachments.ALLOWED_MIME_TYPES
    ]
    if not rows:
        raise _refuse(
            E_NOT_FOUND,
            f"vision describe: task {task.id} has no image attachments to describe",
            task_id=task.id,
            image_count=0,
        )
    if len(rows) > 1:
        raise _refuse(
            E_VALIDATION,
            f"vision describe: task {task.id} has {len(rows)} image attachments; pass "
            "attachment_id to say which one to describe",
            task_id=task.id,
            image_count=len(rows),
            attachment_ids=[int(row.id) for row in rows],
        )
    return _require_describable(rows[0])


def _require_describable(row: Any) -> Any:
    """A row that can actually be turned into an image part — checked BEFORE any model work."""
    attachment_id = int(row.id)
    mime = str(row.mime or "")
    if mime not in attachments.ALLOWED_MIME_TYPES:
        raise _refuse(
            E_VALIDATION,
            f"vision describe: attachment {attachment_id} is {mime or 'of unknown type'}, not "
            f"{attachments.ALLOWED_MIME_TEXT} — there is no image to describe",
            attachment_id=attachment_id,
            mime=mime,
            accepted=list(attachments.ALLOWED_MIME_TYPES),
        )
    # `attachment_path` is the seam's path builder (it re-validates the stored name).  A row
    # whose blob is gone cannot be described, and the honest code for "the thing you named is
    # not there" is E_NOT_FOUND, not a provider fault: nothing was sent anywhere yet.
    path = attachments.attachment_path(row)
    if not path.is_file():
        raise _refuse(
            E_NOT_FOUND,
            f"vision describe: attachment {attachment_id} is recorded for task {row.task_id} "
            "but its file is missing from the data volume",
            attachment_id=attachment_id,
            task_id=int(row.task_id),
        )
    return row


# ── the model call ──────────────────────────────────────────────────────────────────────────

def _tokens_from(usage: Any) -> int | None:
    """Measured prompt+completion total, or None when EITHER side is unmeasured."""
    if not isinstance(usage, dict):
        return None
    prompt = usage.get("input_tokens")
    completion = usage.get("output_tokens")

    def measured(value: Any) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0

    if not measured(prompt) or not measured(completion):
        return None
    # measured() above guarantees both are real ints (never bool, never None).
    assert isinstance(prompt, int) and isinstance(completion, int)
    return prompt + completion


def _upstream_from_runtime(exc: PolsiaError, key: str) -> PolsiaError:
    """Translate a runtime RAISE into the route's `E_UPSTREAM` / `E_CONFIG` vocabulary."""
    if exc.code == E_DEEPSEEK_KEY:
        return _refuse(
            E_CONFIG,
            f"vision describe: no DeepSeek API key is configured (set {DEEPSEEK_ENV_VAR} or "
            "save one in Settings) — nothing was sent to the provider",
            missing=DEEPSEEK_ENV_VAR,
        )
    if exc.code == E_CLI_TIMEOUT:
        return _refuse(
            E_UPSTREAM,
            _scrub(f"vision describe: DeepSeek did not answer in time ({exc.message})", key),
            provider_status=None,
            subtype="timeout",
        )
    if exc.code == E_CLI_JSON:
        return _refuse(
            E_UPSTREAM,
            _scrub(f"vision describe: the provider's answer was off-contract ({exc.message})", key),
            provider_status=None,
            subtype="off_contract",
        )
    # E_VALIDATION from the runtime keeps its own code and message (it names the real problem,
    # e.g. an unsendable DEEPSEEK_BASE_URL or an image the seam refused); every other code is
    # NOT re-labelled here — an unexpected code must stay visible, not be guessed at.
    return exc


def _call_model(parts: list[dict[str, Any]], key: str) -> VisionDescription:
    """ONE synchronous describe call — runs on a worker thread, never the event loop."""
    invocation = claude_cli.Invocation(
        argv=["deepseek-runtime"],
        stdin_text=VISION_PROMPT,
        transport="stdin",
    )
    try:
        result = invoke(
            invocation,
            api_key=key,
            model=VISION_MODEL,
            images=parts,
        )
    except PolsiaError as exc:
        translated = _upstream_from_runtime(exc, key)
        if translated is exc:
            raise
        raise translated from exc
    except Exception as exc:  # noqa: BLE001 - a transport-specific fault is still upstream
        raise _refuse(
            E_UPSTREAM,
            _scrub(
                f"vision describe: the model call failed ({type(exc).__name__}: {exc})", key
            ),
            provider_status=None,
            subtype="transport",
        ) from exc

    try:
        envelope = json.loads(result.stdout or "")
    except ValueError as exc:
        raise _refuse(
            E_UPSTREAM,
            "vision describe: the runtime returned an envelope that is not JSON",
            provider_status=None,
            subtype="off_contract",
        ) from exc
    if not isinstance(envelope, dict):
        raise _refuse(
            E_UPSTREAM,
            "vision describe: the runtime returned a non-object envelope",
            provider_status=None,
            subtype="off_contract",
        )

    if envelope.get("is_error"):
        status = envelope.get("api_error_status")
        subtype = str(envelope.get("subtype") or "error")
        detail = str(envelope.get("result") or "").strip()[:_DETAIL_LIMIT]
        raise _refuse(
            E_UPSTREAM,
            _scrub(
                f"vision describe: DeepSeek returned an error ({subtype}"
                + (f", provider status {status}" if status is not None else ", no HTTP status")
                + (f"): {detail}" if detail else ")"),
                key,
            ),
            provider_status=status if isinstance(status, int) else None,
            subtype=subtype,
        )

    description = str(envelope.get("result") or "").strip()
    if not description:
        # A 200 with no sentence is not a description.  Refusing by name beats rendering an
        # empty block that looks like the model said nothing about a real image.
        raise _refuse(
            E_UPSTREAM,
            "vision describe: the provider returned an empty description — there is nothing "
            "honest to show for this image",
            provider_status=None,
            subtype="empty_result",
        )

    return VisionDescription(
        description=description,
        model=str(envelope.get("model") or VISION_MODEL),
        tokens=_tokens_from(envelope.get("usage")),
    )


# ── memory: the sha256 cache + the task history (V27 / L4) ──────────────────────────────────

def _recorded_sha256(row: Any) -> str | None:
    """The digest the attachment row already carries — or None when it is not a usable digest.

    `task_attachments.sha256` is measured at upload from the exact bytes that were sniffed and
    stored, so the describe path reads it instead of hashing up to 5 MiB again.  Only a row that
    carries no usable digest (hand-written) falls back to hashing the bytes it is about to send.
    """
    value = str(getattr(row, "sha256", "") or "").strip().lower()
    return value if _SHA256_RE.fullmatch(value) else None


async def _cached_description(db: AsyncSession, *, sha256: str) -> VisionDescriptionRow | None:
    """Newest stored sentence for (bytes digest, the model we would ask) — or None.

    Matching the model too is the point: a sentence measured by another model is a different
    measurement, so it is a miss (and is re-measured), never served as if it were the same.
    """
    stmt = (
        select(VisionDescriptionRow)
        .where(
            VisionDescriptionRow.sha256 == sha256,
            VisionDescriptionRow.model == VISION_MODEL,
        )
        .order_by(VisionDescriptionRow.id.desc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalars().first()


def _from_stored(record: VisionDescriptionRow) -> VisionDescription:
    """A stored row -> the answer shape, marked as served from memory."""
    return VisionDescription(
        description=record.description,
        model=str(record.model or VISION_MODEL),
        tokens=record.tokens,
        cached=True,
        description_id=int(record.id),
        created_at=record.created_at,
    )


async def _store_description(
    db: AsyncSession, *, row: Any, sha256: str, measured: VisionDescription
) -> VisionDescriptionRow:
    """Persist one just-measured sentence; the caller's session commits (route `get_db`)."""
    record = VisionDescriptionRow(
        attachment_id=int(row.id),
        task_id=int(row.task_id),
        sha256=sha256,
        description=measured.description,
        model=measured.model,
        tokens=measured.tokens,
    )
    db.add(record)
    await db.flush()
    return record


async def history(db: AsyncSession, *, task_id: int) -> list[VisionDescriptionRow]:
    """One task's stored descriptions, NEWEST FIRST — or `E_NOT_FOUND` for an unknown task.

    `created_at` is the database clock and may collide at second resolution, so `id` is the
    tiebreaker: two descriptions written in the same instant are still returned in write order.
    """
    task = await db.get(Task, int(task_id))
    if task is None:
        raise _refuse(
            E_NOT_FOUND,
            f"vision history: task {task_id} does not exist",
            task_id=int(task_id),
        )
    stmt = (
        select(VisionDescriptionRow)
        .where(VisionDescriptionRow.task_id == int(task_id))
        .order_by(
            VisionDescriptionRow.created_at.desc(),
            VisionDescriptionRow.id.desc(),
        )
    )
    return list((await db.execute(stmt)).scalars().all())


# ── public entry point ─────────────────────────────────────────────────────────────────────

async def describe(
    db: AsyncSession, *, attachment_id: int | None = None, task_id: int | None = None
) -> VisionDescription:
    """Describe one attachment (see the module docstring).

    Writes at most ONE row — the `vision_descriptions` measurement — and never the task.  A
    cache hit writes nothing and calls the provider zero times.
    """
    row = await _resolve_attachment(db, attachment_id=attachment_id, task_id=task_id)

    key = (secret_store.get_deepseek_api_key() or "").strip()
    if not key:
        raise _refuse(
            E_CONFIG,
            f"vision describe: no DeepSeek API key is configured (set {DEEPSEEK_ENV_VAR} or "
            "save one in Settings) — nothing was sent to the provider",
            missing=DEEPSEEK_ENV_VAR,
        )

    sha256 = _recorded_sha256(row)
    if sha256 is not None:
        cached = await _cached_description(db, sha256=sha256)
        if cached is not None:
            logger.info(
                "vision describe: attachment %s served from memory (row %s, %s) — no provider call",
                getattr(row, "id", "?"),
                cached.id,
                cached.model,
            )
            return _from_stored(cached)

    # The seam re-reads and re-sniffs up to 5 MiB; keep it off the event loop.
    parts = await asyncio.to_thread(attachments.image_parts, [row])

    if sha256 is None:
        # A hand-written row with no usable digest: hash the very bytes being sent, then give
        # memory the same chance it gets on the normal path before paying for a call.
        payload = base64.b64decode(parts[0]["data_uri"].split(",", 1)[1])
        sha256 = hashlib.sha256(payload).hexdigest()
        cached = await _cached_description(db, sha256=sha256)
        if cached is not None:
            logger.info(
                "vision describe: attachment %s served from memory after hashing its bytes",
                getattr(row, "id", "?"),
            )
            return _from_stored(cached)

    measured = await asyncio.to_thread(_call_model, parts, key)
    stored = await _store_description(db, row=row, sha256=sha256, measured=measured)
    logger.info(
        "vision describe: attachment %s described by %s (%s tokens), stored as row %s",
        getattr(row, "id", "?"),
        measured.model,
        measured.tokens if measured.tokens is not None else "unmeasured",
        stored.id,
    )
    # `created_at` is the database clock and is not needed in the describe answer; the row's
    # id is what the caller can use to find it again in the history.
    return VisionDescription(
        description=measured.description,
        model=measured.model,
        tokens=measured.tokens,
        cached=False,
        description_id=int(stored.id) if stored.id is not None else None,
    )


__all__ = [
    "DEEPSEEK_ENV_VAR",
    "VISION_MODEL",
    "VISION_PROMPT",
    "VisionDescription",
    "describe",
    "history",
]
