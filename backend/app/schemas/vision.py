"""Wire shapes for the vision routes (V26 describe + V27 memory).

Three models, all deliberately small:

* `VisionDescribeRequest` — exactly ONE of `attachment_id` / `task_id`.  The ROUTE parses the
  raw JSON through `parse_describe_body` rather than letting FastAPI coerce the body, because
  the product contract is that a malformed body is the repo's NAMED refusal (`E_VALIDATION`
  envelope + `X-Polsia-Error-Code`), not FastAPI's uncoded `{"detail": [...]}` 422.  The model
  documents the shape in OpenAPI and is the single validation site (the service trusts what
  this module produces).
* `VisionDescribeResponse` — `{description, model, tokens, cached}`.  `tokens` is `int | None`:
  the measured prompt+completion total when the provider reported both, or `null` when it did
  not.  A `0` here would be a fabricated measurement (the NULL-vs-zero rule), so it is never
  coerced.  `cached` is V27/L4: `true` means the sentence came from `vision_descriptions` and
  NO provider call was made; `false` means it was just measured and stored.  The field is
  additive — the V26 block is the same block plus one honest provenance bit.
* `VisionDescriptionOut` — one history row for `GET /api/v1/vision/descriptions?task_id=`.
  `sha256_prefix` is the leading 12 hex characters of the image digest: enough to tell two
  images apart on screen without printing 64 characters of noise beside every sentence.

`parse_history_task_id` lives here (not in the route) for the same reason `parse_describe_body`
does: one validation site, named refusals, no FastAPI-coerced 422.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from app.core.errors import E_VALIDATION, ERROR_HINTS, PolsiaError

#: The only keys a describe body may carry.  An unknown key is refused by name so a typo
#: (`attachmentId`) cannot silently become "neither id supplied" or, worse, be ignored.
_ALLOWED_KEYS = ("attachment_id", "task_id")

#: `sha256_prefix` length in the history rows (12 hex chars = 48 bits of the digest).
SHA256_PREFIX_LEN = 12

#: Query ids are decimal ASCII digits only: `str.isdigit()` also accepts non-ASCII digits, and
#: a leading `+`/`-` is not an id.
_DIGITS = re.compile(r"[0-9]+")


class VisionDescribeRequest(BaseModel):
    """`{"attachment_id": N}` or `{"task_id": N}` — exactly one, both integers."""

    attachment_id: int | None = None
    task_id: int | None = None


class VisionDescribeResponse(BaseModel):
    """What the model said about one attachment, how much it measured, and where it came from.

    `description` is MODEL OUTPUT for the operator to read; it is never the task's result and
    its only other home is the `vision_descriptions` row it was stored in.  `model` is the model
    id the provider actually served.  `tokens` is null when unmeasured.  `cached` is true only
    when this sentence was served from memory with no provider call in this request.
    """

    description: str
    model: str
    tokens: int | None
    cached: bool = False


class VisionDescriptionOut(BaseModel):
    """One row of a task's description history (`GET /vision/descriptions?task_id=`)."""

    id: int
    attachment_id: int
    #: Leading `SHA256_PREFIX_LEN` characters of the image digest — a display handle, not the
    #: full cache key (the client that needs the full digest can ask the attachment route).
    sha256_prefix: str
    description: str
    model: str | None
    tokens: int | None
    created_at: datetime | None


def _refuse(message: str, how_to_fix: str, **context: Any) -> PolsiaError:
    return PolsiaError(E_VALIDATION, message, how_to_fix=how_to_fix, context=context)


def parse_describe_body(payload: Any) -> VisionDescribeRequest:
    """JSON value -> validated request, or a NAMED `E_VALIDATION` refusal (no silent defaults).

    Rules, all asserted by `tests/unit/test_v26_vision.py`:
      * the body must be a JSON object;
      * only `attachment_id` / `task_id` may appear;
      * each supplied id must be a real integer (bool is NOT an id — `True` would otherwise
        coerce to 1 and describe the wrong attachment; a numeric string is not coerced either);
      * exactly one must be supplied — both or neither is ambiguous and refused.
    """
    if not isinstance(payload, dict):
        raise _refuse(
            f"vision describe: the request body must be a JSON object with exactly one of "
            f"attachment_id or task_id, got {type(payload).__name__}",
            how_to_fix=ERROR_HINTS[E_VALIDATION],
            body_type=type(payload).__name__,
        )

    unknown = sorted(str(key) for key in payload if key not in _ALLOWED_KEYS)
    if unknown:
        raise _refuse(
            f"vision describe: unknown field(s) {', '.join(unknown)}; the body accepts only "
            "attachment_id and task_id",
            how_to_fix="send {\"attachment_id\": <int>} or {\"task_id\": <int>}",
            unknown_fields=unknown,
        )

    for key in _ALLOWED_KEYS:
        value = payload.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            raise _refuse(
                f"vision describe: {key} must be an integer id, got {type(value).__name__}",
                how_to_fix="send the numeric id, e.g. {\"attachment_id\": 12}",
                field=key,
                value_type=type(value).__name__,
            )

    attachment_id = payload.get("attachment_id")
    task_id = payload.get("task_id")
    if attachment_id is not None and task_id is not None:
        raise _refuse(
            "vision describe: pass exactly one of attachment_id or task_id, not both "
            f"(attachment_id={attachment_id}, task_id={task_id})",
            how_to_fix="choose the attachment to describe, or the task whose single image it is",
            attachment_id=attachment_id,
            task_id=task_id,
        )
    if attachment_id is None and task_id is None:
        raise _refuse(
            "vision describe: pass exactly one of attachment_id or task_id — neither was supplied",
            how_to_fix="send {\"attachment_id\": <int>} or {\"task_id\": <int>}",
        )
    return VisionDescribeRequest(attachment_id=attachment_id, task_id=task_id)


def parse_history_task_id(raw: str | None) -> int:
    """`?task_id=` query value -> a positive integer, or a NAMED `E_VALIDATION` refusal.

    A missing value is a caller error (the route must know WHOSE history), not an empty
    history: refusing by name beats answering `[]` for a request that named no task.
    """
    if raw is None or raw == "":
        raise _refuse(
            "vision history: pass ?task_id=<id> — the history is scoped to one task",
            how_to_fix="send GET /api/v1/vision/descriptions?task_id=<task id>",
        )
    text = str(raw).strip()
    if not _DIGITS.fullmatch(text):
        raise _refuse(
            f"vision history: task_id must be a positive integer, got {raw!r}",
            how_to_fix="send ?task_id=<task id>, e.g. ?task_id=7",
            task_id=str(raw)[:50],
        )
    task_id = int(text)
    if task_id <= 0:
        raise _refuse(
            f"vision history: task_id must be a positive integer, got {task_id}",
            how_to_fix="send ?task_id=<task id>, e.g. ?task_id=7",
            task_id=task_id,
        )
    return task_id


__all__ = [
    "SHA256_PREFIX_LEN",
    "VisionDescriptionOut",
    "VisionDescribeRequest",
    "VisionDescribeResponse",
    "parse_describe_body",
    "parse_history_task_id",
]
