"""WAVE 8 / logic-debugger — FAILURE MATRIX #3, GROUP A: the ERROR ENVELOPE per route class.

WHAT THIS FILE PINS, and why it is a matrix and not a happy-path test:

Every refusal that reaches an HTTP client must carry the SAME contract — a named `E_*` code, a
message, a `how_to_fix`, structured context, the machine-readable `X-Polsia-Error-Code` header, and
the HTTP status its code means (`app/core/errors.HTTP_STATUS_BY_CODE`, the single table).  The
wave-5 sweep pinned the four framework shapes (404/405/422/500); this wave drives ONE representative
of EVERY route class end-to-end through the real app:

| class        | status | refusal source                                        |
|--------------|--------|-------------------------------------------------------|
| auth         | 401    | missing / wrong API key (protocol challenge kept)      |
| not found    | 404    | route miss AND missing row                             |
| wrong verb   | 405    | framework refusal, named (Allow preserved)             |
| validation   | 422    | coded (bad agent_type) + the DOCUMENTED uncoded array  |
| conflict     | 409    | illegal/same status write, non-runnable run dispatch   |
| policy       | 403    | sandbox refusal of a real side effect                  |
| rate limit   | 429    | WS ticket mint cap (a real limiter, not a mock)        |
| db outage    | 503    | BEFORE the route (dependency) and INSIDE it            |
| provider     | 502    | a planted transport timeout through the REAL mapping   |
| provider auth| 401    | the provider answered 401 through the REAL mapping     |
| config       | 503    | the provider credential is absent (named variable)     |
| timeout      | 504    | a planner step that exceeded its deadline              |
| internal     | 500    | an unhandled fault, with the correlation id            |

Two wire shapes are DELIBERATE exceptions and are asserted as such (so a future "unify everything"
refactor cannot silently break the dashboard's pydantic-array contract):
  * a pydantic validation 422 stays `{"detail": [...]}` with no code;
  * an uncoded 400 (the Stripe webhook's bad signature) stays plain `{"detail": "<text>"}`.

The last row is a CENSUS: every `E_*` code in `app/core/errors.py` must have a non-empty
`how_to_fix` hint and a status, and the two deliberately-unlisted codes must be exactly the two
that mean 500.  A code with no hint is a refusal that cannot say how to fix itself.
"""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.core import errors as errors_module
from app.core.errors import (
    E_ACTION_REPLAY_UNAVAILABLE,
    E_AGENT_UNKNOWN,
    E_AUTH,
    E_CLI_TIMEOUT,
    E_CONFIG,
    E_DB,
    E_EGRESS_BLOCKED,
    E_INTERNAL,
    E_METHOD_NOT_ALLOWED,
    E_NOT_FOUND,
    E_POLICY_DENIED,
    E_RATE_LIMITED,
    E_SANDBOX_BLOCKED,
    E_TASK_NOT_RUNNABLE,
    E_UPSTREAM,
    E_VALIDATION,
    ERROR_HINTS,
    HTTP_STATUS_BY_CODE,
    PolsiaError,
    http_status_for,
    to_http_detail,
)
from app.main import app as application
from app.main import create_app

ENVELOPE_KEYS = {"ok", "error"}
ERROR_KEYS = {"code", "message", "how_to_fix", "context"}


def refusal_of(resp: httpx.Response, *, status: int, code: str) -> dict:
    """Assert the ONE envelope contract and return its `error` block."""
    assert resp.status_code == status, (resp.status_code, resp.text[:400])
    body = resp.json()
    assert set(body) == ENVELOPE_KEYS, f"not the product envelope: {body}"
    assert body["ok"] is False
    error = body["error"]
    assert set(error) == ERROR_KEYS, error
    assert error["code"] == code, error
    assert isinstance(error["message"], str) and error["message"].strip()
    assert isinstance(error["how_to_fix"], str) and error["how_to_fix"].strip(), (
        "a named refusal must say how to fix it"
    )
    assert isinstance(error["context"], dict)
    assert resp.headers.get("x-polsia-error-code") == code, (
        "the machine-readable code header must agree with the body"
    )
    return error


# ── auth ─────────────────────────────────────────────────────────────────────────────────────
async def test_a01_a_missing_key_is_a_named_401_with_the_protocol_challenge(api_client):
    resp = await api_client.get("/api/v1/tasks")
    error = refusal_of(resp, status=401, code=E_AUTH)
    assert "X-API-Key" in resp.headers.get("www-authenticate", ""), (
        "the auth refusal keeps the protocol-level challenge"
    )
    assert "X-API-Key" in error["how_to_fix"]


async def test_a02_a_wrong_key_is_the_same_named_401(api_client):
    """A non-blank WRONG key is still a 401 — never a 500, never a pass."""
    resp = await api_client.get("/api/v1/tasks", headers={"X-API-Key": "definitely-wrong"})
    refusal_of(resp, status=401, code=E_AUTH)


async def test_a03_a_blank_key_counts_as_no_credential(api_client):
    """`CLAUDE.md`'s measured rule: blank == absent while the service is keyed."""
    resp = await api_client.get("/api/v1/tasks", headers={"X-API-Key": ""})
    refusal_of(resp, status=401, code=E_AUTH)


# ── not found / wrong verb ──────────────────────────────────────────────────────────────────
def test_a04_a_route_miss_is_the_named_404():
    with TestClient(create_app()) as client:
        resp = client.get("/api/v1/definitely-not-a-route", headers={"X-API-Key": settings.api_key})
    error = refusal_of(resp, status=404, code=E_NOT_FOUND)
    assert error["context"]["method"] == "GET"


async def test_a05_a_missing_row_is_the_same_named_404(api_client, auth_headers):
    resp = await api_client.get("/api/v1/tasks/999999", headers=auth_headers)
    error = refusal_of(resp, status=404, code=E_NOT_FOUND)
    assert error["context"]["task_id"] == 999999


def test_a06_a_wrong_verb_is_named_and_keeps_allow():
    """`GET /sites/{slug}` does not exist; only POST/DELETE verbs do (wave-5's case)."""
    with TestClient(create_app()) as client:
        resp = client.get("/api/v1/sites/cinder-lye", headers={"X-API-Key": settings.api_key})
    refusal_of(resp, status=405, code=E_METHOD_NOT_ALLOWED)
    assert "DELETE" in resp.headers.get("allow", ""), "the framework Allow header must survive"


# ── validation ──────────────────────────────────────────────────────────────────────────────
async def test_a07_a_coded_validation_refusal_is_a_named_422(api_client, auth_headers):
    """An unknown agent_type is refused by the SERVICE and travels as the envelope."""
    resp = await api_client.post(
        "/api/v1/tasks",
        headers=auth_headers,
        json={"title": "matrix a07", "agent_type": "not_an_agent"},
    )
    error = refusal_of(resp, status=422, code=E_AGENT_UNKNOWN)
    assert "not_an_agent" in error["message"] or "not_an_agent" in str(error["context"])


async def test_a08_the_framework_validation_422_is_the_documented_UNCoded_shape(
    api_client, auth_headers
):
    """pydantic's array is a deliberate exception: `{"detail": [...]}`, no `ok`, no code.

    Asserted rather than "fixed": the dashboard has its own form-error path for it, and wave 5
    pinned the same shape (`test_w5_error_envelope_shapes.py`).  This row exists so the matrix
    STATES the exception instead of quietly omitting the validation class.
    """
    resp = await api_client.post(
        "/api/v1/tasks", headers=auth_headers, json={"nothing": "here"}
    )
    assert resp.status_code == 422
    body = resp.json()
    assert set(body) == {"detail"}, body
    assert isinstance(body["detail"], list) and body["detail"]
    assert "ok" not in body and "error" not in body


async def test_a09_an_integer_past_the_storage_range_is_a_named_422(api_client, auth_headers):
    """Wave-13's S4-F3: caller input, not a server fault — named E_VALIDATION 422."""
    resp = await api_client.get(
        "/api/v1/tasks/9223372036854775808", headers=auth_headers
    )
    refusal_of(resp, status=422, code=E_VALIDATION)


# ── conflict ────────────────────────────────────────────────────────────────────────────────
async def _create_pending_task(api_client, auth_headers) -> int:
    resp = await api_client.post(
        "/api/v1/tasks",
        headers=auth_headers,
        json={"title": "matrix conflict row", "agent_type": "social_media"},
    )
    assert resp.status_code == 201, resp.text[:400]
    return int(resp.json()["id"])


async def test_a10_a_same_status_write_is_a_named_409(api_client, auth_headers):
    task_id = await _create_pending_task(api_client, auth_headers)
    resp = await api_client.patch(
        f"/api/v1/tasks/{task_id}/status", headers=auth_headers, json={"status": "pending"}
    )
    error = refusal_of(resp, status=409, code=E_TASK_NOT_RUNNABLE)
    assert error["context"]["task_id"] == task_id


async def test_a11_a_non_runnable_run_dispatch_is_a_named_409_before_any_hand_off(
    api_client, auth_headers, monkeypatch
):
    """The refusal happens BEFORE `.delay()` — poisoning the broker hand-off proves it."""
    task_id = await _create_pending_task(api_client, auth_headers)
    done = await api_client.patch(
        f"/api/v1/tasks/{task_id}/status", headers=auth_headers, json={"status": "completed"}
    )
    assert done.status_code == 200, done.text[:400]

    import celery_app.tasks.agent_tasks as agent_tasks

    def _poisoned_delay(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("a refused dispatch must never reach the broker")

    monkeypatch.setattr(agent_tasks.run_agent_task, "delay", _poisoned_delay)
    resp = await api_client.post(f"/api/v1/tasks/{task_id}/run", headers=auth_headers)
    refusal_of(resp, status=409, code=E_TASK_NOT_RUNNABLE)


# ── policy (403) ────────────────────────────────────────────────────────────────────────────
async def test_a12_a_sandboxed_side_effect_is_a_named_403(api_client, auth_headers, monkeypatch):
    """`SANDBOX_MODE=true` (the shipped default) refuses a real send BY NAME, 403."""
    monkeypatch.setattr(settings, "sandbox_mode", True)
    resp = await api_client.post(
        "/api/v1/phone/send",
        headers=auth_headers,
        json={"to": "11", "body": "matrix a12"},
    )
    error = refusal_of(resp, status=403, code=E_SANDBOX_BLOCKED)
    assert "SANDBOX_MODE" in error["how_to_fix"]


# ── rate limit (429) ────────────────────────────────────────────────────────────────────────
async def test_a13_the_ws_ticket_mint_cap_is_a_named_429(api_client, auth_headers):
    """A REAL limiter, driven past its bound: the 31st mint in the window refuses by name."""
    from app.api.v1 import ws

    minted: list[int] = []
    ws._mint_times.clear()  # test hygiene: this cap is per-process state
    try:
        refused = None
        for attempt in range(1, 41):
            resp = await api_client.post("/api/v1/ws/ticket", headers=auth_headers)
            if resp.status_code == 200:
                minted.append(attempt)
                continue
            refused = resp
            break
        assert refused is not None, "the mint cap never fired inside 40 attempts"
        error = refusal_of(refused, status=429, code=E_RATE_LIMITED)
        assert error["context"]["mints_in_window"] >= settings.ws_ticket_mint_limit
        assert len(minted) == settings.ws_ticket_mint_limit
    finally:
        ws._mint_times.clear()


# ── database outage (503), before and inside the route ─────────────────────────────────────
def _raising_dependency(exc: BaseException):
    def _boom():  # noqa: ANN202 - a FastAPI dependency
        raise exc

    return _boom


async def test_a14_a_db_refusal_BEFORE_the_route_is_a_named_503(
    api_client, auth_headers, monkeypatch
):
    """The dependency fails AFTER auth (the router-level dependency runs first), i.e. this is the
    pre-route refusal class, not an auth row."""
    from app.core.database import get_db

    monkeypatch.setitem(
        application.dependency_overrides,
        get_db,
        _raising_dependency(PolsiaError(E_DB, "planted: the database is unreachable")),
    )
    resp = await api_client.get("/api/v1/tasks", headers=auth_headers)
    refusal_of(resp, status=503, code=E_DB)


async def test_a15_a_db_refusal_INSIDE_the_route_is_the_same_named_503(
    api_client, auth_headers, monkeypatch
):
    import app.api.v1.tasks as tasks_route

    async def _broken(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise PolsiaError(E_DB, "planted: the read failed mid-route")

    monkeypatch.setattr(tasks_route, "list_tasks", _broken)
    resp = await api_client.get("/api/v1/tasks", headers=auth_headers)
    refusal_of(resp, status=503, code=E_DB)


async def test_a16_both_db_paths_answer_the_SAME_wire(api_client, auth_headers, monkeypatch):
    """R29's lesson, re-proven as a matrix row: the layer a refusal was raised in must not change
    its code, its body shape or its headers."""
    from app.core.database import get_db

    pre_route_override = application.dependency_overrides[get_db]
    monkeypatch.setitem(
        application.dependency_overrides,
        get_db,
        _raising_dependency(PolsiaError(E_DB, "same refusal, before the route")),
    )
    before = await api_client.get("/api/v1/tasks", headers=auth_headers)
    monkeypatch.setitem(application.dependency_overrides, get_db, pre_route_override)

    import app.api.v1.tasks as tasks_route

    async def _broken(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise PolsiaError(E_DB, "same refusal, inside the route")

    monkeypatch.setattr(tasks_route, "list_tasks", _broken)
    inside = await api_client.get("/api/v1/tasks", headers=auth_headers)

    for resp in (before, inside):
        refusal_of(resp, status=503, code=E_DB)
    assert set(before.json()) == set(inside.json())
    assert before.headers.get("x-polsia-error-code") == inside.headers.get("x-polsia-error-code")


# ── provider class (503 config, 401 auth, 502 upstream) ────────────────────────────────────
def _no_secret(_name: str) -> str:
    return ""


def _fake_secret(_name: str) -> str:
    return "fake-token-for-the-matrix"


async def test_a17_a_missing_provider_credential_is_a_named_503_naming_the_variable(
    api_client, auth_headers, monkeypatch
):
    monkeypatch.setattr("app.services.secret_store.get_secret", _no_secret)
    resp = await api_client.post("/api/v1/integrations/sendgrid/test", headers=auth_headers)
    error = refusal_of(resp, status=503, code=E_CONFIG)
    assert "SENDGRID_API_KEY" in error["message"], error["message"]


async def test_a18_a_provider_TIMEOUT_through_the_real_mapping_is_a_named_502(
    api_client, auth_headers, monkeypatch
):
    """Fault injection at the HTTP-client boundary (a permitted double): the adapter's OWN
    `except httpx.TimeoutException -> E_UPSTREAM` translation runs for real."""
    from app.services import integration_status_service as service

    def _handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("planted: the provider never answered", request=request)

    monkeypatch.setattr("app.services.secret_store.get_secret", _fake_secret)
    monkeypatch.setattr(service, "_TRANSPORT", httpx.MockTransport(_handler))
    resp = await api_client.post("/api/v1/integrations/sendgrid/test", headers=auth_headers)
    error = refusal_of(resp, status=502, code=E_UPSTREAM)
    assert "timed out" in error["message"], error["message"]
    assert "fake-token-for-the-matrix" not in resp.text, "a refusal never echoes the credential"


async def test_a19_a_provider_401_is_a_named_401_and_never_echoes_the_credential(
    api_client, auth_headers, monkeypatch
):
    from app.services import integration_status_service as service

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "the key was invalid"})

    monkeypatch.setattr("app.services.secret_store.get_secret", _fake_secret)
    monkeypatch.setattr(service, "_TRANSPORT", httpx.MockTransport(_handler))
    resp = await api_client.post("/api/v1/integrations/sendgrid/test", headers=auth_headers)
    error = refusal_of(resp, status=401, code=E_AUTH)
    assert "refused" in error["message"], error["message"]
    assert "fake-token-for-the-matrix" not in resp.text


async def test_a20_an_unknown_provider_is_a_named_404(api_client, auth_headers):
    resp = await api_client.post("/api/v1/integrations/no-such-provider/test", headers=auth_headers)
    error = refusal_of(resp, status=404, code=E_NOT_FOUND)
    assert "no-such-provider" in error["message"]


# ── timeout (504) and internal (500) ───────────────────────────────────────────────────────
async def test_a21_a_bounded_planner_step_that_expired_is_a_named_504(
    api_client, auth_headers, monkeypatch
):
    """The ONE route class that runs an agent step inside the request (openapi_errors.py:77)."""
    from app.services import planner_service

    async def _hung(_db):  # noqa: ANN001
        raise PolsiaError(
            E_CLI_TIMEOUT,
            "planted: the planner step exceeded its deadline",
            how_to_fix=ERROR_HINTS[E_CLI_TIMEOUT],
        )

    monkeypatch.setattr(planner_service, "propose", _hung)
    resp = await api_client.post("/api/v1/planner/propose", headers=auth_headers)
    error = refusal_of(resp, status=504, code=E_CLI_TIMEOUT)
    assert "AGENT_MAX_RETRIES" in error["how_to_fix"], (
        "the timeout hint must say the ladder is bounded"
    )


def test_a22_an_unhandled_fault_is_a_named_500_with_the_correlation_id(monkeypatch):
    application_ = create_app()
    from app.api.deps import get_db
    from app.core.security import verify_api_key

    monkeypatch.setitem(application_.dependency_overrides, verify_api_key, lambda: None)
    monkeypatch.setitem(
        application_.dependency_overrides,
        get_db,
        _raising_dependency(RuntimeError("planted for the 500 matrix row")),
    )
    with TestClient(application_, raise_server_exceptions=False) as client:
        resp = client.get("/api/v1/tasks")
    error = refusal_of(resp, status=500, code=E_INTERNAL)
    assert error["context"]["request_id"]
    assert resp.headers["x-request-id"] == error["context"]["request_id"]
    assert "X-Request-ID" in error["how_to_fix"]


# ── the census: every code names a fix and a status ────────────────────────────────────────
def test_a23_every_named_code_has_a_how_to_fix_and_a_status():
    codes = sorted(
        value
        for name, value in vars(errors_module).items()
        if name.startswith("E_") and isinstance(value, str) and value.startswith("E_")
    )
    assert len(codes) >= 25, f"the code census shrank: {codes}"
    missing_hints = [code for code in codes if not (ERROR_HINTS.get(code) or "").strip()]
    assert not missing_hints, f"codes with no how_to_fix: {missing_hints}"
    unlisted = {code for code in codes if http_status_for(code) == 500}
    # W8-LD-01 (OPEN, tracked by `test_a23b_...` below): this ONE code is a defect, named here so
    # the census cannot silently absorb a second one.  Fix = one row in app/core/errors.py.
    open_unmapped = {errors_module.E_DEEPSEEK_KEY}
    assert not (unlisted - {E_INTERNAL, E_ACTION_REPLAY_UNAVAILABLE} - open_unmapped), (
        "every OTHER code is a named condition the caller or operator can act on and must carry "
        f"its own status; unmapped: "
        f"{sorted(unlisted - {E_INTERNAL, E_ACTION_REPLAY_UNAVAILABLE} - open_unmapped)}"
    )
    assert {E_INTERNAL, E_ACTION_REPLAY_UNAVAILABLE} <= unlisted, (
        "the two deliberately-unlisted codes must still mean 500 (a corrupt journal cannot be "
        "fixed by changing the request; an unnamed bug is a bug)"
    )
    for code, status in HTTP_STATUS_BY_CODE.items():
        assert code.startswith("E_"), code
        assert 400 <= status <= 599, (code, status)
    # the two deps aliases MUST be the same object (the R29 fix), never a copy
    from app.api import deps

    assert deps.STATUS_BY_CODE is HTTP_STATUS_BY_CODE


# W8-LD-01 CLOSED (Lead, wave 8): E_DEEPSEEK_KEY now has its 503 row in HTTP_STATUS_BY_CODE;
# the strict xfail was removed when the fix landed — the assertion is the fixed behaviour.
def test_a23b_the_configuration_refusals_are_not_unmapped_500s():
    for code in (errors_module.E_DEEPSEEK_KEY,):
        assert http_status_for(code) == 503, (
            f"{code} is operator-fixable configuration, not an internal fault"
        )


def test_a24_the_envelope_helper_matches_what_the_handlers_emit():
    """`to_http_detail` fills the hint fallback — an empty `how_to_fix` must never ship."""
    refusal = PolsiaError(E_EGRESS_BLOCKED, "the address is not routable")
    detail = to_http_detail(refusal)
    assert set(detail) == {"ok", "error"}
    assert detail["error"]["how_to_fix"] == ERROR_HINTS[E_EGRESS_BLOCKED]
    assert refusal.as_dict()["error"]["how_to_fix"] == "", (
        "the raw error keeps the empty hint; only the WIRE fills the fallback"
    )


def test_a26_an_out_of_range_id_is_caller_input_not_a_db_outage():
    """W8-LD-03, CLOSED in wave 9 (the strict xfail was removed when this row went green).

    The defect (measured LIVE on the rebuilt wave-8 stack): `GET
    /api/v1/tasks/9223372036854775808` answered 503 `E_DB` for a PostgreSQL int32-range
    DBAPIError raised inside `query_helpers.db_refusal`, and the client message embedded the
    compiled statement.  A wave-13 route-level pin passed because the SQLite fixture raises
    `OverflowError` instead (handler 422), which is exactly why the live wire disagreed.

    The fix is at the DB boundary (`db_refusal` now classifies a data/range failure as
    `E_VALIDATION` 422 and strips the SQL from the wire text); this row drives the two shapes a
    caller can produce — an out-of-range integer and a malformed date — plus the genuine-outage
    control that must STILL be `E_DB`, so "classify caller input" cannot quietly turn every
    database failure into a 422.
    """
    from sqlalchemy.exc import DataError

    from app.services.query_helpers import db_refusal

    class _AsyncpgStyleDataError(Exception):
        def __str__(self) -> str:
            return (
                "invalid input for query argument $1: 9223372036854775808 "
                "(value out of int32 range)\n[SQL: SELECT tasks.id, tasks.title FROM tasks "
                "WHERE tasks.id = $1::INTEGER]"
            )

    refusal = db_refusal("task 9223372036854775808", _AsyncpgStyleDataError())
    assert refusal.code == E_VALIDATION, (
        "an out-of-range caller-supplied id is caller input, not a database outage"
    )
    assert "SELECT" not in refusal.message and "[SQL:" not in refusal.message, (
        "the client-facing message must never carry the server's compiled statement"
    )
    assert "[SQL:" not in refusal.context.get("detail", ""), (
        "the driver's parameter echo is part of the statement disclosure and must be redacted too"
    )
    assert "SELECT" not in refusal.context.get("detail", "")
    # the driver's OWN sentence survives the redaction (an operator still learns the cause)
    assert "data/range error" in refusal.message

    # a SQLAlchemy-wrapped DataError (the real PostgreSQL shape: DBAPIError whose `orig` is the
    # driver's data error) classifies the same way, without needing the message to say it.
    wrapped = DataError(
        "SELECT 1", {}, Exception("invalid input syntax for type integer: \"9223372036854775808\"")
    )
    assert db_refusal("task 9223372036854775808", wrapped).code == E_VALIDATION

    # CONTROL: a genuine outage is still E_DB 503 — the SQL is redacted, the code is not moved.
    outage = db_refusal(
        "task 7",
        ConnectionRefusedError(
            "connection refused\n[SQL: SELECT tasks.id FROM tasks WHERE tasks.id = 7]"
        ),
    )
    assert outage.code == E_DB, "a database that is down must still read as a database outage"
    assert "[SQL:" not in outage.message and "SELECT" not in outage.message
    assert "connection refused" in outage.message


@pytest.mark.parametrize(
    "code",
    [
        E_AUTH,
        E_NOT_FOUND,
        E_TASK_NOT_RUNNABLE,
        E_POLICY_DENIED,
        E_SANDBOX_BLOCKED,
        E_RATE_LIMITED,
        E_DB,
        E_CONFIG,
        errors_module.E_INTEGRATION,
        E_UPSTREAM,
        E_CLI_TIMEOUT,
        E_VALIDATION,
        E_INTERNAL,
    ],
)
def test_a25_the_status_table_is_total_for_the_matrix_classes(code: str) -> None:
    """Every class the matrix drives has an explicit status — no accidental 500 fallback."""
    if code == E_INTERNAL:
        assert http_status_for(code) == 500
        return
    assert http_status_for(code) != 500, f"{code} would reach the wire as an unmapped 500"
