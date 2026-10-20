"""Vision routes — on demand (V26 / L4, F4) and memory (V27 / L4).

TWO routes, both thin:

* `POST /api/v1/vision/describe` — the operator points at an attachment (or at a task whose
  single image it is) and gets back what the vision model sees.  V27 adds `cached`: `true` means
  the sentence was served from `vision_descriptions` with NO provider call in this request.
* `GET /api/v1/vision/descriptions?task_id=<id>` — the task's stored descriptions, newest
  first.  `task_id` is the contract (there is deliberately no per-attachment history route).

The describe body is parsed HERE from the raw request bytes (not by FastAPI's body model) and
the history `task_id` from the raw query string, so a malformed request is the repo's NAMED
`E_VALIDATION` refusal in the standard envelope, with `X-Polsia-Error-Code`, instead of
FastAPI's uncoded `{"detail": [...]}` 422.  The declared schemas still reach OpenAPI through
`app.schemas.vision`.

Auth is the existing `verify_api_key` dependency, applied at the router so it runs before the
body/query is read.  Every refusal comes from `vision_service` through `refusal_guard`.

WIRING: the router is already mounted by `app/main.py` (`api_v1.include_router(vision.router)`,
V26).  V27 adds no include — it adds the second handler to the same router.
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, refusal_guard, verify_api_key
from app.core.errors import E_VALIDATION, ERROR_HINTS, PolsiaError
from app.schemas.vision import (
    SHA256_PREFIX_LEN,
    VisionDescribeResponse,
    VisionDescriptionOut,
    parse_describe_body,
    parse_history_task_id,
)
from app.services.vision_service import describe, history

router = APIRouter(
    prefix="/vision",
    tags=["vision"],
    dependencies=[Depends(verify_api_key)],
)


def _body_refusal(message: str, **context: Any) -> PolsiaError:
    return PolsiaError(
        E_VALIDATION, message, how_to_fix=ERROR_HINTS[E_VALIDATION], context=context
    )


async def _json_body(request: Request) -> Any:
    """The request's JSON value — or a named `E_VALIDATION` (empty body, bad JSON, bad encoding)."""
    raw = await request.body()
    if not raw:
        raise _body_refusal(
            "vision describe: the request body is empty; send "
            '{"attachment_id": <int>} or {"task_id": <int>}'
        )
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise _body_refusal(
            f"vision describe: the request body is not valid JSON ({type(exc).__name__})",
            body_bytes=len(raw),
        ) from exc


@router.post("/describe", response_model=VisionDescribeResponse)
async def describe_attachment(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> VisionDescribeResponse:
    """Describe one image attachment: `{description, model, tokens, cached}` or a named refusal."""
    with refusal_guard():
        parsed = parse_describe_body(await _json_body(request))
        result = await describe(
            db, attachment_id=parsed.attachment_id, task_id=parsed.task_id
        )
    return VisionDescribeResponse(
        description=result.description,
        model=result.model,
        tokens=result.tokens,
        cached=result.cached,
    )


@router.get("/descriptions", response_model=list[VisionDescriptionOut])
async def list_descriptions(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> list[VisionDescriptionOut]:
    """One task's stored descriptions, newest first — `?task_id=<id>` or a named refusal."""
    with refusal_guard():
        task_id = parse_history_task_id(request.query_params.get("task_id"))
        rows = await history(db, task_id=task_id)
    return [
        VisionDescriptionOut(
            id=int(row.id),
            attachment_id=int(row.attachment_id),
            sha256_prefix=str(row.sha256 or "")[:SHA256_PREFIX_LEN],
            description=row.description,
            model=row.model,
            tokens=row.tokens,
            created_at=row.created_at,
        )
        for row in rows
    ]
