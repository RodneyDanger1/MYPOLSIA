"""SV-W5-01 (wave 6): the per-operation DOMAIN statuses, derived from raise sites and pinned.

WHAT THIS FILE PINS, AND WHY A SCHEMA PIN IS THE RIGHT TEST
-----------------------------------------------------------
Wave 5 declared the four universal classes (401/404/500/503) on every operation and NAMED the
operation-specific domain refusals as a residual.  Wave 6 traced every domain code to the code that
raises it and declared the reachable ones.  The pins below are on the GENERATED schema, because a
table that is right in a unit call but does not reach `/openapi.json` would pass the wrong test
(the same reasoning as `test_w5_openapi_error_responses.py`).

The expected sets are the census, not a copy of the table: every operation listed here has a raise
site reachable from its handler, cited in `app/core/openapi_errors.OPERATION_EXTRA_STATUSES` (and in
`meridian-work/build/swarm-2026-10-28/systems-validator/raw/w6/domain-reach.txt`).  A new status on an
operation, or a new operation carrying one, must be added HERE too - deliberately, with its seam.

A status that is NOT declared matters just as much as one that is: `test_no_operation_claims_...`
pins the operations that were considered and refused (the wave-5 census listed `POST /tasks`,
`POST /chat/confirm` and `POST /auto-mode` for a 429; the trace disproved it - they enqueue, and
`accounting.authorize` has exactly two production call sites, one of them in an unwired module).
"""
from __future__ import annotations

import pytest

from app.core.openapi_errors import (
    DOMAIN_STATUS_EXCEPTIONS,
    OPERATION_EXTRA_STATUSES,
    SHARED_ERROR_RESPONSES,
)
from app.main import create_app

#: The domain statuses this wave reasons about (the shared 401/404/500/503 are tested by wave 5).
DOMAIN = {"400", "403", "404", "409", "413", "429", "502", "504"}

#: Every operation that carries a domain status beyond the universal classes, with the raise site
#: that made it true.  A `{param}` path already implies the universal 404 (wave-5 rule); the sets
#: below include it where the path carries a parameter, so the pin states the WHOLE domain set.
#: THE PIN MOVES ONLY WITH A RAISE SITE.
EXPECTED_DOMAIN_STATUSES: dict[tuple[str, str], set[str]] = {
    # external effects (the policy firewall's decision object)
    ("POST", "/api/v1/sites/{slug}/deploy"): {"403", "404", "429", "502"},
    ("POST", "/api/v1/invoices/{invoice_id}/send"): {"403", "404", "429", "502"},
    ("POST", "/api/v1/phone/send"): {"403", "409"},
    ("DELETE", "/api/v1/data"): {"403"},
    # the one operation that runs an agent step inside the request
    ("POST", "/api/v1/planner/propose"): {"403", "404", "409", "413", "429", "502", "504"},
    # upstream faults the API relays
    ("GET", "/api/v1/trust/link"): {"502"},
    ("GET", "/api/v1/economics/convert"): {"502"},
    ("POST", "/api/v1/integrations/{provider}/test"): {"404", "502"},
    ("POST", "/api/v1/vision/describe"): {"404", "502"},
    ("GET", "/api/v1/attachments/{attachment_id}"): {"404", "502"},
    # pathless operations addressed by body/query (SV-W6-03)
    ("GET", "/api/v1/site/package/download"): {"404"},
    ("POST", "/api/v1/approvals"): {"404"},
    ("POST", "/api/v1/planner/confirm"): {"404"},
    # state conflicts
    ("POST", "/api/v1/tasks/{task_id}/run"): {"404", "409"},
    ("PATCH", "/api/v1/tasks/{task_id}/status"): {"404", "409"},
    ("POST", "/api/v1/chat/confirm"): {"409"},
    ("POST", "/api/v1/ws/ticket"): {"429"},
    # documented format exception (not the envelope's code set)
    ("POST", "/api/v1/finance/stripe/webhook"): {"400"},
    # `POST /support/inbox/inbound` declares a 401 (its own provider secret); the credential class
    # is pinned by the wave-5 file and 401 is not a DOMAIN status here, so it is not repeated.
}

#: Pathless operations that can still answer 404 - addressed by a BODY or QUERY value, not a path.
PATHLESS_404_OPERATIONS = {
    ("GET", "/api/v1/site/package/download"),
    ("POST", "/api/v1/approvals"),
    ("POST", "/api/v1/planner/propose"),
    ("POST", "/api/v1/planner/confirm"),
    ("POST", "/api/v1/vision/describe"),
}


def _codes(op: dict) -> set[str]:
    return set(op.get("responses", {}))


@pytest.fixture(scope="module")
def schema() -> dict:
    return create_app().openapi()


@pytest.fixture(scope="module")
def operations(schema: dict) -> dict[tuple[str, str], dict]:
    return {
        (method.upper(), path): operation
        for path, item in schema["paths"].items()
        for method, operation in item.items()
        if method in ("get", "post", "put", "patch", "delete")
    }


def _domain_codes(operations: dict, target: tuple[str, str]) -> set[str]:
    return _codes(operations[target]) & DOMAIN


def test_the_declared_domain_set_per_operation_is_the_traced_one(operations: dict) -> None:
    """Each operation declares exactly the domain statuses a reachable raise site produces."""
    mismatches = {}
    for target, expected in EXPECTED_DOMAIN_STATUSES.items():
        actual = _domain_codes(operations, target)
        if actual != expected:
            mismatches[f"{target[0]} {target[1]}"] = {
                "expected": sorted(expected),
                "declared": sorted(actual),
            }
    assert mismatches == {}, mismatches


def test_the_operation_extra_table_and_the_schema_agree(operations: dict) -> None:
    """The table is the declaration; the schema is where it lands.  Both must say the same."""
    for target, extras in OPERATION_EXTRA_STATUSES.items():
        missing = {str(status) for status in extras} - _codes(operations[target])
        assert not missing, f"{target} declared {sorted(missing)} in the table but not in the schema"


def test_every_declarable_status_has_a_shared_entry() -> None:
    """`declarations_for` looks each status up in SHARED_ERROR_RESPONSES; a gap drops them ALL.

    Measured while landing this wave: adding 413/504 to the extras table without a shared entry
    raised KeyError inside `declare_error_responses`, whose boot-safety `except` then skipped the
    operation entirely (500/503/401 disappeared from 7 operations).  This pin makes that a red test
    instead of a silent schema regression.
    """
    statuses = {status for extras in OPERATION_EXTRA_STATUSES.values() for status in extras}
    missing = sorted(
        status for status in statuses if status not in SHARED_ERROR_RESPONSES and status != 400
    )
    assert missing == [], f"statuses declared with no shared envelope entry: {missing}"


def test_every_domain_status_body_is_the_envelope(operations: dict) -> None:
    """A declared 403/404/409/413/429/502/504 must reference the refusal envelope, not be empty."""
    ref = "#/components/schemas/MeridianErrorEnvelope"
    broken = {}
    for target, expected in EXPECTED_DOMAIN_STATUSES.items():
        for status in sorted(expected & {"403", "404", "409", "413", "429", "502", "504"}):
            body = operations[target]["responses"][status].get("content") or {}
            schema = (body.get("application/json") or {}).get("schema") or {}
            if schema.get("$ref") != ref:
                broken[f"{target[0]} {target[1]} {status}"] = schema
    assert broken == {}, broken


def test_the_universal_classes_are_unchanged(operations: dict) -> None:
    """This wave adds domain classes; it must not lose a universal one (the wave-5 finding)."""
    assert len(operations) == 114, len(operations)
    assert sum("500" in _codes(op) for op in operations.values()) == 114
    assert sum("503" in _codes(op) for op in operations.values()) == 113
    assert sum("401" in _codes(op) for op in operations.values()) == 110
    bare = sorted(
        f"{m} {p}"
        for (m, p), op in operations.items()
        if not (_codes(op) - {"200", "201", "202", "204", "422"})
    )
    assert bare == [], bare


def test_the_429_operations_are_exactly_the_spend_and_mint_ones(operations: dict) -> None:
    """Budget is checked in the request only by the planner; send caps on the two effect routes."""
    declared = sorted(f"{m} {p}" for (m, p), op in operations.items() if "429" in _codes(op))
    assert declared == [
        "POST /api/v1/invoices/{invoice_id}/send",
        "POST /api/v1/planner/propose",
        "POST /api/v1/sites/{slug}/deploy",
        "POST /api/v1/ws/ticket",
    ], declared


def test_413_and_504_are_declared_only_where_an_invocation_is_built_in_the_request(
    operations: dict,
) -> None:
    """E_PROMPT_TOO_LARGE / E_CLI_TIMEOUT reach the wire only through planner propose's inline step."""
    for status in ("413", "504"):
        declared = sorted(f"{m} {p}" for (m, p), op in operations.items() if status in _codes(op))
        assert declared == ["POST /api/v1/planner/propose"], (status, declared)


def test_the_403_operations_are_the_external_effect_routes(operations: dict) -> None:
    """A 403 is a policy/sandbox refusal of an ACTION; a read route must never claim one."""
    declared = sorted(f"{m} {p}" for (m, p), op in operations.items() if "403" in _codes(op))
    assert declared == [
        "DELETE /api/v1/data",
        "POST /api/v1/invoices/{invoice_id}/send",
        "POST /api/v1/phone/send",
        "POST /api/v1/planner/propose",
        "POST /api/v1/sites/{slug}/deploy",
    ], declared


def test_the_502_operations_are_the_traced_upstream_faults(operations: dict) -> None:
    declared = sorted(f"{m} {p}" for (m, p), op in operations.items() if "502" in _codes(op))
    assert declared == [
        "GET /api/v1/attachments/{attachment_id}",
        "GET /api/v1/economics/convert",
        "GET /api/v1/trust/link",
        "POST /api/v1/integrations/{provider}/test",
        "POST /api/v1/invoices/{invoice_id}/send",
        "POST /api/v1/planner/propose",
        "POST /api/v1/sites/{slug}/deploy",
        "POST /api/v1/vision/describe",
    ], declared


def test_the_409_operations_are_the_state_conflicts(operations: dict) -> None:
    declared = sorted(f"{m} {p}" for (m, p), op in operations.items() if "409" in _codes(op))
    assert declared == [
        "PATCH /api/v1/tasks/{task_id}/status",
        "POST /api/v1/chat/confirm",
        "POST /api/v1/phone/send",
        "POST /api/v1/planner/propose",
        "POST /api/v1/tasks/{task_id}/run",
    ], declared


def test_the_pathless_404_operations_are_addressed_by_body_or_query(operations: dict) -> None:
    """SV-W6-03: five operations answer a real 404 without a `{param}` path.

    The wave-5 heuristic ('a pathless collection read cannot 404') was too strong for them: the
    row/plan they name arrives in the body or the query string.
    """
    pathless = sorted(
        f"{m} {p}" for (m, p), op in operations.items() if "{" not in p and "404" in _codes(op)
    )
    assert pathless == sorted(f"{m} {p}" for m, p in PATHLESS_404_OPERATIONS), pathless


def test_no_operation_claims_a_domain_status_the_raise_sites_cannot_reach(operations: dict) -> None:
    """The operations the wave-5 census named but the raise-site trace disproved."""
    unreachable = {
        ("POST", "/api/v1/tasks"): {"403", "413", "429", "502", "504"},
        ("POST", "/api/v1/chat/confirm"): {"403", "413", "429", "502", "504"},
        ("POST", "/api/v1/auto-mode"): {"403", "413", "429", "502", "504"},
        ("GET", "/api/v1/trust/domain"): {"502", "504"},  # DNS/TLS failures are FIELDS, not refusals
        ("GET", "/api/v1/signals"): {"502", "504"},       # per-feed failures are fields in a 200
        ("POST", "/api/v1/sites/{slug}/regenerate"): {"403", "413", "429", "502", "504"},
        ("POST", "/api/v1/agents/{agent_type}/trigger"): {"403", "413", "429", "502", "504"},
        ("GET", "/api/v1/site/package/download"): {"413", "429", "502", "504"},
        ("POST", "/api/v1/phone/send"): {"413", "429", "502", "504"},
    }
    overclaims = {}
    for target, forbidden in unreachable.items():
        found = _codes(operations[target]) & forbidden
        if found:
            overclaims[f"{target[0]} {target[1]}"] = sorted(found)
    assert overclaims == {}, overclaims


def test_every_documented_exception_has_a_reason() -> None:
    """The exception set is the contract for what is NOT declared elsewhere; each entry names why."""
    assert DOMAIN_STATUS_EXCEPTIONS, "the documented exception set is empty"
    for status, reason in DOMAIN_STATUS_EXCEPTIONS.items():
        assert isinstance(status, int), status
        assert len(reason) > 40, (status, reason)
    # 402 has NO operation at all; the other keys name a restricted declaration.
    assert 402 in DOMAIN_STATUS_EXCEPTIONS
    for status in (403, 413, 429, 504):
        assert status in DOMAIN_STATUS_EXCEPTIONS, status


def test_the_webhook_and_liveness_exceptions_still_hold(operations: dict) -> None:
    """The wave-5 exact sets survive this wave (a domain extension must not quietly edit them)."""
    assert _codes(operations[("GET", "/api/v1/health")]) == {"200", "500"}
    assert _codes(operations[("GET", "/api/v1/ready")]) == {"200", "500", "503"}
    assert _codes(operations[("POST", "/api/v1/finance/stripe/webhook")]) == {
        "200",
        "400",
        "500",
        "503",
    }


@pytest.mark.parametrize(
    ("method", "url", "params", "body", "expected_status"),
    [
        ("POST", "/api/v1/planner/confirm", None, {"candidate_id": "idea-not-a-real-id"}, 404),
        ("GET", "/api/v1/site/package/download", {"slug": "never-built-sv-w6"}, None, 404),
    ],
)
@pytest.mark.asyncio
async def test_a_pathless_404_is_real_on_the_wire(
    api_client, auth_headers, method, url, params, body, expected_status
) -> None:
    """The declaration is not theory: the real app answers the coded 404 envelope for both.

    `POST /planner/confirm` with an unknown candidate (no proposal on record) -> E_NOT_FOUND;
    `GET /site/package/download?slug=never-built-sv-w6` -> E_NOT_FOUND.  The existing route tests
    pin the same statuses; this one pins the WIRE SHAPE the new declaration promises.
    """
    response = await api_client.request(
        method, url, headers=auth_headers, params=params, json=body
    )
    assert response.status_code == expected_status, response.text
    payload = response.json()
    assert payload.get("error", {}).get("code") == "E_NOT_FOUND", payload
    assert response.headers.get("x-meridian-error-code") == "E_NOT_FOUND"
