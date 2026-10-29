"""SV-W4-01 (wave 5): the OpenAPI contract must declare the errors the app really returns.

Measured at the wave-4 HEAD (`7b81f56`, live): **114 operations, 0 declaring 401, 113 declaring no
error response beyond FastAPI's automatic 422** (the single exception was
`GET /attachments/{id}`).  Yet the same build answers `401 E_AUTH`, `404 E_NOT_FOUND`,
`503 E_CONFIG/E_STORAGE/E_DB` and `500 E_INTERNAL`.  `app/core/openapi_errors.py` declares the
shared classes at app-assembly time; this file pins the RESULT on the generated schema.

The assertions are deliberately about the schema a client reads, not about the helper's return
value: a declaration that works in a unit call but does not reach `/openapi.json` would pass the
wrong test.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.openapi_errors import (
    OPERATION_EXTRA_STATUSES,
    UNAUTHENTICATED_OPERATIONS,
    MeridianErrorEnvelope,
)
from app.main import create_app


def _codes(operation: dict) -> set[str]:
    return set(operation["responses"])


@pytest.fixture(scope="module")
def schema() -> dict:
    return create_app().openapi()


@pytest.fixture(scope="module")
def operations(schema: dict) -> dict[tuple[str, str], dict]:
    ops: dict[tuple[str, str], dict] = {}
    for path, item in schema["paths"].items():
        for method, operation in item.items():
            if method in ("get", "post", "put", "patch", "delete"):
                ops[(method.upper(), path)] = operation
    return ops


def test_no_operation_declares_only_a_success_and_422(operations: dict) -> None:
    """The original defect, as a pin: 113/114 operations used to declare nothing but 200/422."""
    bare = sorted(
        f"{method} {path}"
        for (method, path), op in operations.items()
        if not (_codes(op) - {"200", "201", "202", "204", "422"})
    )
    assert bare == [], f"operations with no declared error response: {bare}"


def test_every_operation_declares_the_internal_error(operations: dict) -> None:
    missing = sorted(f"{m} {p}" for (m, p), op in operations.items() if "500" not in _codes(op))
    assert missing == [], f"500 E_INTERNAL is possible on every operation; missing on: {missing}"


def test_every_dependency_touching_operation_declares_unavailable(operations: dict) -> None:
    """Liveness is the ONE operation with no dependency; everything else can answer 503."""
    missing = sorted(
        f"{m} {p}"
        for (m, p), op in operations.items()
        if p != "/api/v1/health" and "503" not in _codes(op)
    )
    assert missing == [], f"503 is a real class for a dependency-touching op; missing on: {missing}"
    assert _codes(operations[("GET", "/api/v1/health")]) == {"200", "500"}, (
        "liveness must declare exactly 200|500 — it touches no dependency"
    )


def test_the_unauthenticated_operations_are_exactly_the_public_five(operations: dict) -> None:
    """A 401 on a public operation would be a lie; a missing 401 on a guarded one is the defect.

    `POST /support/inbox/inbound` is public to `X-API-Key` but refuses a missing/wrong PROVIDER
    secret with `401 E_AUTH`, so it declares 401 explicitly (an operation-specific entry).
    """
    without_401 = {
        (m, p) for (m, p), op in operations.items() if "401" not in _codes(op)
    }
    assert without_401 == UNAUTHENTICATED_OPERATIONS - {("POST", "/api/v1/support/inbox/inbound")}
    assert "401" in _codes(operations[("POST", "/api/v1/support/inbox/inbound")])


def test_every_addressed_operation_declares_not_found(operations: dict) -> None:
    """A `{param}` path is resource-addressed, so an absent run/task/row is a real 404.

    REPAIRED PIN (wave 6 / SV-W6-03): the original assertion was `pathless_404 == []` — "a
    collection read cannot 404".  That heuristic was too strong for five operations whose request
    names a row/plan WITHOUT a path parameter, and wave 6's raise-site trace declared their real
    404 (app/core/openapi_errors.OPERATION_EXTRA_STATUSES):

      old arithmetic: 31 path-addressed 404s, 0 pathless by construction;
      new arithmetic: 31 path-addressed + 5 body/query-addressed = 36 declared 404s.

    The five are pinned by name here and in tests/unit/test_w6_domain_statuses.py, so a sixth
    pathless 404 still fails this test.
    """
    missing = sorted(
        f"{m} {p}"
        for (m, p), op in operations.items()
        if "{" in p and "404" not in _codes(op)
    )
    assert missing == [], f"addressed operations without a declared 404: {missing}"
    pathless_404 = sorted(
        f"{m} {p}"
        for (m, p), op in operations.items()
        if "{" not in p and "404" in _codes(op)
    )
    assert pathless_404 == sorted(
        f"{method} {path}"
        for method, path in {
            ("POST", "/api/v1/approvals"),
            ("POST", "/api/v1/planner/propose"),
            ("POST", "/api/v1/planner/confirm"),
            ("POST", "/api/v1/vision/describe"),
            ("GET", "/api/v1/site/package/download"),
        }
    ), pathless_404


def test_the_declared_envelope_schema_is_real(schema: dict, operations: dict) -> None:
    """The 401/404/500/503 bodies must be the ENVELOPE, not an empty description."""
    schemas = schema["components"]["schemas"]
    assert "MeridianErrorEnvelope" in schemas, sorted(schemas)
    assert "MeridianErrorDetail" in schemas, sorted(schemas)
    assert set(schemas["MeridianErrorEnvelope"]["required"]) == {"ok", "error"}
    assert set(schemas["MeridianErrorDetail"]["required"]) == {"code", "message"}
    for status in ("401", "404", "500", "503"):
        body = operations[("GET", "/api/v1/runs/{run_id}")]["responses"][status]["content"]
        assert body["application/json"]["schema"] == {
            "$ref": "#/components/schemas/MeridianErrorEnvelope"
        }, f"{status} does not reference the envelope"


def test_the_model_describes_the_runtime_envelope() -> None:
    """The published model must validate the body the app really sends (probed, not assumed)."""
    with TestClient(create_app()) as client:
        resp = client.get("/api/v1/runs")
    assert resp.status_code == 401
    assert resp.headers["x-meridian-error-code"] == "E_AUTH"
    parsed = MeridianErrorEnvelope.model_validate(resp.json())
    assert parsed.ok is False
    assert parsed.error.code == "E_AUTH"
    assert parsed.error.how_to_fix


def test_the_documented_operation_specific_exceptions(operations: dict) -> None:
    """Each extra status is pinned to the route whose code really raises it."""
    exact = {
        ("POST", "/api/v1/finance/stripe/webhook"): {"200", "400", "500", "503"},
        ("GET", "/api/v1/health"): {"200", "500"},
        ("GET", "/api/v1/ready"): {"200", "500", "503"},
    }
    for target, expected in exact.items():
        assert _codes(operations[target]) == expected, (target, _codes(operations[target]))

    for target, extras in OPERATION_EXTRA_STATUSES.items():
        codes = _codes(operations[target])
        missing = {str(status) for status in extras} - codes
        assert not missing, f"{target} must declare {sorted(missing)} (code: {codes})"

    assert "403" in _codes(operations[("DELETE", "/api/v1/data")])
    assert "409" in _codes(operations[("POST", "/api/v1/tasks/{task_id}/run")])
    assert "409" in _codes(operations[("PATCH", "/api/v1/tasks/{task_id}/status")])
    assert "429" in _codes(operations[("POST", "/api/v1/ws/ticket")]), (
        "sentinel's F-03b mint rate limit is a real 429 on the ticket route"
    )


def test_the_validation_error_declares_both_documented_shapes(operations: dict) -> None:
    """REPAIRED PIN (wave 8 / SV-W6-02): 422 has TWO bodies on the SAME operation, so the schema
    declares both.

    Wave 7 measured the contradiction live (`systems-validator/raw/w7/live-w7-422-shapes.txt`):
    `POST /api/v1/tasks` answers `{"detail": [...]}` for a malformed body AND the refusal envelope
    (`E_AGENT_UNKNOWN`) for a bad `agent_type` — one status, two shapes.  The assertion below used to
    pin the array as the operation's ONLY shape: right about the wire it looked at, wrong about the
    contract, because the route's own coded `HTTPException` and the `E_VALIDATION` raises in
    `app.services.task_service` (a direct import) reach `main.coded_http_exception_handler` /
    `main.meridian_error_handler`, both of which answer 422 with the envelope.

      old arithmetic: 111 x 422 = 111 x $ref HTTPValidationError                  (array only)
      new arithmetic: 111 x 422 = 109 x anyOf(HTTPValidationError, MeridianErrorEnvelope)  [wave-8 ruling
    SV-W8-01: anyOf — HTTPValidationError has no `required`, so an envelope body validates
    under BOTH branches and oneOf would reject the real coded 422]
                                +   2 x $ref HTTPValidationError                  (pricing, ws/ticket)

    `POST /api/v1/tasks` is one of the 109, so its body is the oneOf.  The array is still DECLARED
    (as the first branch, and alone on the two operations that cannot answer the envelope) — this
    is not a narrowing; the runtime array body is still pinned by
    `tests/unit/test_v14_money_fixes.py::test_ordinary_validation_errors_keep_the_uncoded_default_shape`
    and `tests/unit/api/test_w8_error_envelopes.py::test_a08_…`.
    """
    op = operations[("POST", "/api/v1/tasks")]
    schema = op["responses"]["422"]["content"]["application/json"]["schema"]
    assert [part.get("$ref") for part in schema["anyOf"]] == [
        "#/components/schemas/HTTPValidationError",
        "#/components/schemas/MeridianErrorEnvelope",
    ], schema
    # the two operations with no reachable coded refusal keep the framework array ALONE
    for target in (("GET", "/api/v1/pricing"), ("POST", "/api/v1/ws/ticket")):
        array = operations[target]["responses"]["422"]["content"]["application/json"]["schema"]
        assert array["$ref"].endswith("HTTPValidationError"), (target, array)
