"""Wire shapes for `POST /api/v1/vision/describe` (V26 / L4 — vision on demand).

Two models, both deliberately small:

* `VisionDescribeRequest` — exactly ONE of `attachment_id` / `task_id`.  The ROUTE parses the
  raw JSON through `parse_describe_body` rather than letting FastAPI coerce the body, because
  the product contract is that a malformed body is the repo's NAMED refusal (`E_VALIDATION`
  envelope + `X-Polsia-Error-Code`), not FastAPI's uncoded `{"detail": [...]}` 422.  The model
  documents the shape in OpenAPI and is the single validation site (the service trusts what
  this module produces).
* `VisionDescribeResponse` — `{description, model, tokens}`.  `tokens` is `int | None`: the
  measured prompt+completion total when the provider reported both, or `null` when it did not.
  A `0` here would be a fabricated measurement (the NULL-vs-zero rule), so it is never coerced.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from app.core.errors import E_VALIDATION, ERROR_HINTS, PolsiaError

#: The only keys a describe body may carry.  An unknown key is refused by name so a typo
#: (`attachmentId`) cannot silently become "neither id supplied" or, worse, be ignored.
_ALLOWED_KEYS = ("attachment_id", "task_id")


class VisionDescribeRequest(BaseModel):
    """`{"attachment_id": N}` or `{"task_id": N}` — exactly one, both integers."""

    attachment_id: int | None = None
    task_id: int | None = None


class VisionDescribeResponse(BaseModel):
    """What the model said about one attachment, and how much it measured.

    `description` is MODEL OUTPUT for the operator to read; it is never the task's result and
    is never persisted (see `vision_service.describe`).  `model` is the model id the provider
    actually served.  `tokens` is null when unmeasured.
    """

    description: str
    model: str
    tokens: int | None


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
