"""V26 / L4 — vision on demand: describe ONE task attachment with the DeepSeek runtime.

The route (`app.api.v1.vision`) is thin; this module owns the work:

1. RESOLVE the attachment — by `attachment_id`, or by `task_id` when that task has exactly
   ONE image attachment.  Ambiguity (more than one image) is `E_VALIDATION` and says how many
   there are; an unknown id and a task with no images are `E_NOT_FOUND`.
2. LOAD its bytes through `task_attachment_service.image_parts([row])` — the existing seam
   that re-reads the blob, re-sniffs the magic bytes and builds the runtime's
   `{mime, data_uri, size_bytes}` payload.  Nothing here re-implements sniffing or path
   building, and the blob's presence is checked up front (a row whose file is gone is
   `E_NOT_FOUND`: the attachment the caller named cannot be described).
3. CALL `deepseek_runtime.invoke(...)` — the public seam — with a fixed, honest prompt at the
   deployment's vision model.  No tools are passed: a describe is one plain completion.
4. RETURN `{description, model, tokens}`.

WHAT THIS MODULE NEVER DOES: it never writes.  The description is model output for the
operator to read, not the task's result — `Task.result_summary` and the attachment row are
left untouched (pinned by `test_describe_never_overwrites_the_task_result_or_the_attachment_row`).
`tokens` is measured-or-null: the runtime's envelope reports `input_tokens`/`output_tokens`
per run, and if either is absent the total is `None` (never a fabricated 0).

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
import json
import logging
from dataclasses import dataclass
from typing import Any

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


@dataclass(frozen=True)
class VisionDescription:
    """The model's answer plus its provenance.  `tokens is None` means UNMEASURED."""

    description: str
    model: str
    tokens: int | None


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


# ── public entry point ─────────────────────────────────────────────────────────────────────

async def describe(
    db: AsyncSession, *, attachment_id: int | None = None, task_id: int | None = None
) -> VisionDescription:
    """Describe one attachment (see the module docstring).  READ-ONLY apart from the model call."""
    row = await _resolve_attachment(db, attachment_id=attachment_id, task_id=task_id)

    # The seam re-reads and re-sniffs up to 5 MiB; keep it off the event loop.
    parts = await asyncio.to_thread(attachments.image_parts, [row])

    key = (secret_store.get_deepseek_api_key() or "").strip()
    if not key:
        raise _refuse(
            E_CONFIG,
            f"vision describe: no DeepSeek API key is configured (set {DEEPSEEK_ENV_VAR} or "
            "save one in Settings) — nothing was sent to the provider",
            missing=DEEPSEEK_ENV_VAR,
        )

    description = await asyncio.to_thread(_call_model, parts, key)
    logger.info(
        "vision describe: attachment %s described by %s (%s tokens)",
        getattr(row, "id", "?"),
        description.model,
        description.tokens if description.tokens is not None else "unmeasured",
    )
    return description


__all__ = [
    "DEEPSEEK_ENV_VAR",
    "VISION_MODEL",
    "VISION_PROMPT",
    "VisionDescription",
    "describe",
]
