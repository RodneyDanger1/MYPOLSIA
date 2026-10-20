"""`POST /api/v1/vision/describe` — vision on demand (V26 / L4, F4).

ONE route, deliberately: the operator points at an attachment (or at a task whose single image
it is) and gets back what the vision model sees.  There is no history route and no persistence
— the description is model output rendered for the operator, never the task's result.

The body is parsed HERE from the raw request bytes (not by FastAPI's body model) so that a
malformed or ambiguous body is the repo's NAMED `E_VALIDATION` refusal in the standard envelope,
with `X-Polsia-Error-Code`, instead of FastAPI's uncoded `{"detail": [...]}` 422.  The declared
schema still reaches OpenAPI through `VisionDescribeRequest` in `app.schemas.vision`.

Auth is the existing `verify_api_key` dependency, applied at the router so it runs before the
body is read.  Every refusal comes from `vision_service` through `refusal_guard`.

WIRING (the parent adds this at integration — `app/main.py` is lane L2's file):
    from app.api.v1 import (..., vision, ...)
    api_v1.include_router(vision.router)
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, refusal_guard, verify_api_key
from app.core.errors import E_VALIDATION, ERROR_HINTS, PolsiaError
from app.schemas.vision import VisionDescribeResponse, parse_describe_body
from app.services.vision_service import describe

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
    """Describe one image attachment: `{description, model, tokens}` or a named refusal."""
    with refusal_guard():
        parsed = parse_describe_body(await _json_body(request))
        result = await describe(
            db, attachment_id=parsed.attachment_id, task_id=parsed.task_id
        )
    return VisionDescribeResponse(
        description=result.description,
        model=result.model,
        tokens=result.tokens,
    )
