"""The app's error-response declarations, applied to EVERY operation (SV-W4-01, wave 5).

WHY THIS FILE EXISTS
--------------------
Measured on the rebuilt wave-4 app (HEAD `7b81f56`, live `GET /openapi.json`): **114 operations,
0 of which declared a 401**, and **113 declared no error response beyond FastAPI's automatic
422** (the single exception was `GET /attachments/{id}`, `404,502`).  Yet the same build really
answers `401 E_AUTH`, `404 E_NOT_FOUND`, `503 E_CONFIG/E_STORAGE/E_DB` and `500 E_INTERNAL` — a
client generator, a typed SDK or an operator reading `/docs` could not see any of it.  The audit
that found this is `polsia-work/build/swarm-2026-10-28/systems-validator/VALIDATION-MATRIX.md`
§3.1; this module is the fix, and `app.main` calls it once while the app is assembled (before the
schema is first generated).

WHAT IS DECLARED, AND WHY EACH CLASS IS TRUE
--------------------------------------------
The declarations are derived from the app's own wiring, not copied by hand:

  * **500 `E_INTERNAL`** — EVERY operation.  `app.main.ErrorBoundary` turns any uncaught exception
    into this envelope (`main.py`, class `ErrorBoundary`), so no operation can honestly omit it.
  * **503 `E_CONFIG` / `E_STORAGE` / `E_DB` / `E_REDIS` / `E_PHONE_UNAVAILABLE` / `E_CLI_MISSING`**
    — every operation except the dependency-free liveness probe `GET /api/v1/health`
    (`app/api/v1/health.py` says it "never consults a dependency").  A readiness/DB session, the
    secret store, the artifact volume, the phone store, the broker and the provider seam all map
    to 503 in `app/core/errors.HTTP_STATUS_BY_CODE`; the only operation that touches none of them
    is liveness.  This is a declared SUPERSET of the class: one read-only probe (e.g. `/signals`,
    whose feed failures are reported per-source inside a 200) may never take this path in
    practice — declaring the service-unavailable class on a dependency-touching operation is the
    conservative direction (a missing class is the defect this module exists to remove).
  * **401 `E_AUTH`** — exactly the operations whose dependency tree contains `verify_api_key`
    (that is the router/route guard the app really uses; the header is `X-API-Key`).  The five
    public operations are therefore NOT given a 401 by accident:
    `/health`, `/ready`, `/finance/stripe/webhook`, `/support/inbox/inbound` (its own provider
    secret; a wrong secret is a real 401, declared below), `/outreach/unsubscribe` (a public
    opt-out link).
  * **404 `E_NOT_FOUND`** — every resource-addressed operation (its path template carries a
    `{param}`): that is the class that can name an absent run/task/row.  A collection read cannot
    return it and does not declare it.
  * **operation-specific extras** — `OPERATION_EXTRA_STATUSES` below.  Each entry was traced to
    the code that raises it; no status is declared on a route that cannot produce it.

DOCUMENTED EXCEPTIONS (pinned by `tests/unit/test_w5_openapi_error_responses.py`)
--------------------------------------------------------------------------------
  * `GET /api/v1/health` — `200 | 500` only (liveness: no dependency, no credential).
  * `POST /api/v1/finance/stripe/webhook` — unauthenticated by design; a bad signature is a
    400 with the framework's plain `{"detail": <string>}` body (pinned by
    `tests/unit/test_refusal_unify.py`), not the refusal envelope — declared that way.
  * `POST /api/v1/support/inbox/inbound` — the provider secret, never `X-API-Key`: a missing or
    wrong secret is `401 E_AUTH`, declared explicitly (it is not a `verify_api_key` dependency).
  * `DELETE /api/v1/data` — 403 `E_POLICY_DENIED` (the approval-token gate in `data.py`).
  * `POST /api/v1/tasks/{task_id}/run`, `PATCH /api/v1/tasks/{task_id}/status` — 409
    (`E_TASK_NOT_RUNNABLE`, `tasks.py`).
  * The 422 validation body keeps the framework's `{"detail": [...]}` shape (auto-declared by
    FastAPI; `main.py` deliberately preserves it) — it is not the refusal envelope, so it is not
    re-declared here.

NOT DECLARED YET, NAMED (wave-6 census; see the W5 report's residual register)
-----------------------------------------------------------------------------
Per-operation domain refusals raised inside services (402 `E_BALANCE`, 413
`E_PROMPT_TOO_LARGE`, 429 `E_BUDGET_EXCEEDED`/`E_RATE_LIMITED` on the spend/effect routes, 502
`E_INTEGRATION`/`E_UPSTREAM`/`E_CLI_*`, 403 `E_SANDBOX_BLOCKED` on the effect routes, 504
`E_CLI_TIMEOUT`) are not yet on the operations that emit them — the ticket mint's 429 IS declared
below because that refusal already reached the wire from a sibling lane.  The rest are enumerated
with their raising seams in `VALIDATION-MATRIX-W5.md` §1.1 and are NOT silently missing: this
module declares the four universal classes + the traced per-operation ones.
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.utils import create_model_field
from pydantic import BaseModel, Field

from app.api.deps import get_db
from app.core.security import verify_api_key


class PolsiaErrorDetail(BaseModel):
    """The `error` object of the product refusal envelope (`app/core/errors.to_http_detail`)."""

    code: str = Field(description="the named E_* code, e.g. E_AUTH / E_NOT_FOUND / E_INTERNAL")
    message: str = Field(description="one sentence naming what failed")
    how_to_fix: str = Field(default="", description="what the caller/operator can do next")
    context: dict[str, Any] = Field(
        default_factory=dict, description="structured detail (ids, counts, request id)"
    )


class PolsiaErrorEnvelope(BaseModel):
    """The refusal envelope every named failure uses on the wire: `{"ok": false, "error": {...}}`."""

    ok: Literal[False] = Field(description="always false — the request failed")
    error: PolsiaErrorDetail


def _envelope(description: str) -> dict[str, Any]:
    """One `responses=` entry carrying the envelope model (FastAPI turns it into a $ref)."""
    return {"model": PolsiaErrorEnvelope, "description": description}


#: Bodies that are NOT the envelope, declared without a model on purpose (a generic JSON object).
_JSON_OBJECT: dict[str, Any] = {
    "content": {"application/json": {"schema": {"type": "object"}}},
}

#: The shared classes.  405 is deliberately absent: the app's 405 is raised by the framework and
#: named below (`E_METHOD_NOT_ALLOWED`), but it is not an operation-level response — a route is
#: only reached when the verb matches.
SHARED_ERROR_RESPONSES: dict[int, dict[str, Any]] = {
    401: _envelope(
        "E_AUTH — the X-API-Key header is missing or wrong (WWW-Authenticate: X-API-Key, "
        "X-Polsia-Error-Code: E_AUTH)"
    ),
    404: _envelope("E_NOT_FOUND — the run/task/row named by the path does not exist"),
    403: _envelope(
        "E_POLICY_DENIED — the action needs an approval (or a wider policy allowance); the "
        "refusal names the gate that refused it"
    ),
    409: _envelope(
        "E_TASK_NOT_RUNNABLE / E_RUN_CANCELLED / E_TASK_CLAIMED — the row is not in a state "
        "that accepts this operation (the current state is in error.context)"
    ),
    429: _envelope(
        "E_RATE_LIMITED / E_BUDGET_EXCEEDED — a rate or spend cap refused this request; back off "
        "until the window resets, or raise the configured cap"
    ),
    500: _envelope(
        "E_INTERNAL — the request failed unexpectedly; the body's context.request_id matches the "
        "X-Request-ID response header and the server log"
    ),
    503: _envelope(
        "a dependency this operation needs is unavailable — E_CONFIG / E_STORAGE / E_DB / "
        "E_REDIS / E_PHONE_UNAVAILABLE / E_CLI_MISSING (`retryable`: the service, not the "
        "request, is the problem)"
    ),
}

#: Operation-specific refusals, each traced to the code that raises it (see the module docstring).
OPERATION_EXTRA_STATUSES: dict[tuple[str, str], tuple[int, ...]] = {
    # data.py `delete_data_route` -> `_verify_wipe_token` raises E_POLICY_DENIED (403).
    ("DELETE", "/api/v1/data"): (403,),
    # tasks.py `run_task_endpoint` / `update_task_status_endpoint` raise E_TASK_NOT_RUNNABLE (409).
    ("POST", "/api/v1/tasks/{task_id}/run"): (409,),
    ("PATCH", "/api/v1/tasks/{task_id}/status"): (409,),
    # finance.py `stripe_webhook`: bad signature / missing header -> 400 (plain detail or a coded
    # E_VALIDATION envelope); a missing signing secret -> E_CONFIG 503 (already declared).
    ("POST", "/api/v1/finance/stripe/webhook"): (400,),
    # support.py `require_inbound_secret`: missing/wrong provider secret -> E_AUTH 401.
    ("POST", "/api/v1/support/inbox/inbound"): (401,),
    # ws.py `_authorise_mint` (sentinel's F-03b, landed after this declaration was designed): a
    # mint at the per-operator socket cap or over the rolling mint window is E_RATE_LIMITED (429).
    ("POST", "/api/v1/ws/ticket"): (429,),
}

#: Operations that are public on purpose (no `verify_api_key` in the dependency tree) and the
#: operations whose declared error set is narrower than the shared one.  The test pins both sets.
UNAUTHENTICATED_OPERATIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/api/v1/health"),
        ("GET", "/api/v1/ready"),
        ("GET", "/api/v1/outreach/unsubscribe"),
        ("POST", "/api/v1/finance/stripe/webhook"),
        ("POST", "/api/v1/support/inbox/inbound"),
    }
)

#: The only operation with no dependency at all: it declares `500` and nothing else.
NO_UNAVAILABLE_OPERATION: frozenset[tuple[str, str]] = frozenset({("GET", "/api/v1/health")})


def _dependant_calls(route: APIRoute) -> list[Any]:
    """Every callable in the route's dependency tree (route + router + parameter deps)."""
    calls: list[Any] = []
    stack = [route.dependant]
    seen: set[int] = set()
    while stack:
        dependant = stack.pop()
        if id(dependant) in seen:
            continue
        seen.add(id(dependant))
        if dependant.call is not None:
            calls.append(dependant.call)
        stack.extend(dependant.dependencies)
    return calls


def _methods(route: APIRoute) -> set[str]:
    return {method.upper() for method in (route.methods or ())}


def declarations_for(route: APIRoute) -> dict[int, dict[str, Any]]:
    """The error responses ONE operation must declare — the single rule this module encodes."""
    methods = _methods(route)
    target = (sorted(methods)[0] if methods else "GET", route.path)
    calls = _dependant_calls(route)
    authenticated = any(call is verify_api_key for call in calls)
    db_backed = any(call is get_db for call in calls)

    statuses: set[int] = {500}
    if target not in NO_UNAVAILABLE_OPERATION:
        statuses.add(503)
    if authenticated:
        statuses.add(401)
    if "{" in route.path:
        statuses.add(404)
    for method in methods:
        statuses.update(OPERATION_EXTRA_STATUSES.get((method, route.path), ()))

    out: dict[int, dict[str, Any]] = {}
    for status in sorted(statuses):
        if status == 400 and target == ("POST", "/api/v1/finance/stripe/webhook"):
            # The webhook's bad-signature body is the framework's plain {"detail": <string>} (and
            # the coded event-id defect is the envelope): declare a JSON object, not a lie.
            out[status] = {
                "description": (
                    "the delivery could not be verified: a plain {\"detail\": <string>} body for "
                    "a bad/missing Stripe signature, or the refusal envelope for a coded event "
                    "defect (E_VALIDATION)"
                ),
                **_JSON_OBJECT,
            }
            continue
        shared = SHARED_ERROR_RESPONSES[status]
        entry: dict[str, Any] = {}
        for key, value in shared.items():
            entry[key] = dict(value) if isinstance(value, dict) else value
        if status == 503 and db_backed:
            entry["description"] = (
                "a dependency this operation needs is unavailable — E_CONFIG / E_STORAGE / "
                "E_DB / E_REDIS / E_PHONE_UNAVAILABLE / E_CLI_MISSING (the request may be "
                "retried once the service is up)"
            )
        out[status] = entry
    return out


def declare_error_responses(app: FastAPI) -> int:
    """Apply `declarations_for` to every schema operation.  Returns the number of entries added.

    Called from `create_app()` AFTER the routers are included and BEFORE the schema is first
    generated.  An operation that already declared a status keeps its own entry (the precedent
    was `tasks.py`'s `GET /attachments/{id}` -> `404,502`), so a route-specific declaration is
    never overwritten by the shared text.
    """
    added = 0
    for route in app.routes:
        if not isinstance(route, APIRoute) or not route.include_in_schema:
            continue
        try:
            declared = declarations_for(route)
        except Exception:  # noqa: BLE001 — a declaration bug must not stop the app booting
            continue
        for status, entry in declared.items():
            if status in route.responses or str(status) in route.responses:
                continue
            route.responses[str(status)] = entry
            # Routes built by FastAPI compile their `responses={...}` models into
            # `route.response_fields` in `APIRoute.__init__`.  Our entries are added AFTER that
            # construction, so the schema generator would otherwise find no field for the status
            # and emit the description with NO content (measured: components had no
            # PolsiaErrorEnvelope and every 401 body was empty).  Build the same field FastAPI
            # builds, keyed by the SAME response key, so the envelope lands in components.schemas.
            model = entry.get("model")
            if model is not None and str(status) not in route.response_fields:
                route.response_fields[str(status)] = create_model_field(
                    name=f"Response_{status}_{route.unique_id}",
                    type_=model,
                    mode="serialization",
                )
            added += 1
    return added


__all__ = [
    "NO_UNAVAILABLE_OPERATION",
    "OPERATION_EXTRA_STATUSES",
    "PolsiaErrorDetail",
    "PolsiaErrorEnvelope",
    "SHARED_ERROR_RESPONSES",
    "UNAUTHENTICATED_OPERATIONS",
    "declare_error_responses",
    "declarations_for",
]
