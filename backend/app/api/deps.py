"""Shared FastAPI dependencies + the refusal bridge.

`get_db` is re-exported from `app.core.database` on purpose: the shipped conftest
overrides *that object* (`app.dependency_overrides[get_db] = ...`), so routes must
depend on the same import.

Refusals: services raise `PolsiaError` with an `E_*` code instead of returning a
plausible empty shape.  `refusal_guard()` turns that into an HTTP response that keeps
the code, the message and the fix hint in the body — the caller never gets a bare 500
with no explanation, and a non-`PolsiaError` still propagates as a real bug.
"""
from __future__ import annotations

import contextlib

from fastapi import HTTPException, status

from app.core.database import get_db  # noqa: F401  (re-exported)
from app.core.errors import (  # noqa: F401  (re-exported)
    E_AGENT_BAD_OUTPUT,
    E_AGENT_UNKNOWN,
    E_AUTH,
    E_BUDGET_EXCEEDED,
    E_CLI_EXIT,
    E_CLI_JSON,
    E_CLI_MISSING,
    E_CLI_TIMEOUT,
    E_CONFIG,
    E_DB,
    E_INTEGRATION,
    E_NOT_FOUND,
    E_POLICY_DENIED,
    E_PROMPT_TOO_LARGE,
    E_RATE_LIMITED,
    E_REDIS,
    E_SANDBOX_BLOCKED,
    E_STORAGE,
    E_TASK_CLAIMED,
    E_TASK_NOT_RUNNABLE,
    E_VALIDATION,
    PolsiaError,
)
from app.core.security import check_api_key, verify_api_key  # noqa: F401  (re-exported)

#: `E_*` code -> HTTP status.  Anything unmapped is a server-side fault (500) but
#: still carries the code in the body.
STATUS_BY_CODE: dict[str, int] = {
    E_AUTH: status.HTTP_401_UNAUTHORIZED,
    E_VALIDATION: status.HTTP_422_UNPROCESSABLE_ENTITY,
    E_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    E_AGENT_UNKNOWN: status.HTTP_422_UNPROCESSABLE_ENTITY,
    E_AGENT_BAD_OUTPUT: status.HTTP_502_BAD_GATEWAY,
    E_TASK_CLAIMED: status.HTTP_409_CONFLICT,
    E_TASK_NOT_RUNNABLE: status.HTTP_409_CONFLICT,
    E_POLICY_DENIED: status.HTTP_403_FORBIDDEN,
    E_SANDBOX_BLOCKED: status.HTTP_403_FORBIDDEN,
    E_BUDGET_EXCEEDED: status.HTTP_429_TOO_MANY_REQUESTS,
    E_RATE_LIMITED: status.HTTP_429_TOO_MANY_REQUESTS,
    E_PROMPT_TOO_LARGE: status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
    # E_CONFIG surfaces at request time when a malformed COMPANY_TIMEZONE blocks a day
    # window: the service's own misconfiguration, not the caller's fault and not an
    # unmapped internal error — same class as E_REDIS.  Kept in agreement with
    # errors.HTTP_STATUS_BY_CODE, which the middleware/exception-handler paths use
    # (parent-surgical completion of T4; the two tables must not disagree).
    E_CONFIG: status.HTTP_503_SERVICE_UNAVAILABLE,
    E_DB: status.HTTP_503_SERVICE_UNAVAILABLE,
    E_REDIS: status.HTTP_503_SERVICE_UNAVAILABLE,
    E_STORAGE: status.HTTP_503_SERVICE_UNAVAILABLE,
    E_CLI_MISSING: status.HTTP_503_SERVICE_UNAVAILABLE,
    E_CLI_TIMEOUT: status.HTTP_504_GATEWAY_TIMEOUT,
    E_CLI_EXIT: status.HTTP_502_BAD_GATEWAY,
    E_CLI_JSON: status.HTTP_502_BAD_GATEWAY,
    E_INTEGRATION: status.HTTP_502_BAD_GATEWAY,
}


def refusal_status(code: str) -> int:
    return STATUS_BY_CODE.get(code, status.HTTP_500_INTERNAL_SERVER_ERROR)


def as_http_exception(exc: PolsiaError) -> HTTPException:
    """The HTTP form of a refusal: same code, same fix hint, no lost detail."""
    return HTTPException(
        status_code=refusal_status(exc.code),
        detail={
            "code": exc.code,
            "message": exc.message,
            "how_to_fix": exc.how_to_fix,
            "context": exc.context,
        },
        headers={"X-Polsia-Error-Code": exc.code},
    )


def refusal_guard():
    """Context manager: raise `PolsiaError` in a route body, get the named refusal out.

    Use it INSIDE the route function body::

        @router.get("/things", response_model=list[ThingOut])
        async def list_things(db: AsyncSession = Depends(get_db)) -> ...:
            with refusal_guard():
                things = await list_things_service(db)
            return [ThingOut.model_validate(t) for t in things]

    A *decorator* wrapper does not work here and was tried: `functools.wraps` does not
    copy `__globals__`, so with `from __future__ import annotations` FastAPI resolves
    the route's string annotations against the *wrapper's* module and cannot find
    `AsyncSession`/body models — every endpoint that takes a body answered 422
    (measured: 9 shipped tests failed).  A context manager leaves the signature
    untouched.

    Only `PolsiaError` is translated; everything else keeps propagating (an
    unexpected exception is a bug, not a refusal, and hiding it behind a tidy 503
    would undo the point of this module).
    """

    @contextlib.contextmanager
    def _guard():
        try:
            yield
        except PolsiaError as exc:
            raise as_http_exception(exc) from exc

    return _guard()


__all__ = [
    "STATUS_BY_CODE",
    "as_http_exception",
    "check_api_key",
    "get_db",
    "refusal_guard",
    "refusal_status",
    "verify_api_key",
]
