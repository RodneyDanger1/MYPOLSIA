"""R29 — ONE status table, ONE refusal response: the same `PolsiaError` answers the same way
wherever it was raised.

The defect this file closes (R27 §M2; raw evidence in
`build/R27-social-outreach/raw/probe_edb_paths.txt` + `status_table_diff.txt`):
`deps.STATUS_BY_CODE` and `errors.HTTP_STATUS_BY_CODE` were two literal tables that disagreed for
nine codes (`E_DB`, `E_STORAGE`, the four `E_CLI_*`, `E_INTEGRATION`, `E_AGENT_BAD_OUTPUT`,
`E_PROMPT_TOO_LARGE`).  The in-route `refusal_guard` path read one table and answered 502/503/504/413
with an `X-Polsia-Error-Code` header; the app-level handler — everything raised before the route,
e.g. a failing `get_db` — read the other and answered a 500 whose code header was missing.  Same
refusal, two different wires.

Pinned here, through the REAL shipped app (`api_client` → ASGITransport → the request middleware +
both exception handlers), for the nine codes plus eleven controls and one deliberately-unmapped
code (`E_DEEPSEEK_KEY`):

  * pre-route vs in-route → identical status, identical full header set (modulo the per-request
    timing measurement), identical envelope — and the status equals the documented canonical value,
    so a move back to the old 500 fails by name;
  * the tables are ONE OBJECT (`deps.STATUS_BY_CODE is errors.HTTP_STATUS_BY_CODE`), so a future
    code cannot be added to one side only; the exact 21-row census is pinned as a literal, so
    changing it is a deliberate act that updates the pin;
  * two REAL shipped pairs beyond the synthetic matrix: `E_DB` on `/api/v1/social/posts` (a broken
    read inside the route vs a `get_db` that refuses before it) and `E_AUTH` from both real raise
    sites (`verify_api_key` as a router dependency and `check_api_key` inline in `agents.py`).

E_AUTH is measured on its two shipped shapes instead of the synthetic route pair on purpose: no
shipped route raises E_AUTH inside `refusal_guard`, and the shipped in-route shape (an inline
`check_api_key` raise in the body) already travels the app-level handler — so the pair tested is the
pair that exists.  Both legs must carry `WWW-Authenticate: X-API-Key` as well as
`X-Polsia-Error-Code`.

RED legs (measured this lane; each restores to a sha256-verified file afterwards):
  * restoring the divergent literal `deps.STATUS_BY_CODE` → the parity test fails with the named
    mismatch (`E_DB: in-route 503 vs pre-route 500`, header missing) and the identity test fails;
  * dropping `X-Polsia-Error-Code` from `main._refusal_headers` → the parity test fails on the
    missing header while every status stays correct (the handler path this lane corrected).
"""
from __future__ import annotations

import pytest
from fastapi import Depends
from sqlalchemy.exc import SQLAlchemyError

from app.api import deps
from app.api.deps import get_db, refusal_guard
from app.core import errors
from app.core.errors import (
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
    E_DEEPSEEK_KEY,
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
    ERROR_HINTS,
    PolsiaError,
)
from app.main import app

#: The nine codes the two tables used to disagree on (R27 §M2 / raw/status_table_diff.txt).
DIVERGING = [
    E_AGENT_BAD_OUTPUT,
    E_CLI_EXIT,
    E_CLI_JSON,
    E_CLI_MISSING,
    E_CLI_TIMEOUT,
    E_DB,
    E_INTEGRATION,
    E_PROMPT_TOO_LARGE,
    E_STORAGE,
]

#: The agreeing family, kept as controls so a "fix" that broke the already-correct rows fails here.
CONTROLS = [
    E_VALIDATION,
    E_NOT_FOUND,
    E_AGENT_UNKNOWN,
    E_TASK_CLAIMED,
    E_TASK_NOT_RUNNABLE,
    E_POLICY_DENIED,
    E_SANDBOX_BLOCKED,
    E_BUDGET_EXCEEDED,
    E_RATE_LIMITED,
    E_CONFIG,
    E_REDIS,
]

#: `E_DEEPSEEK_KEY` is deliberately unmapped in BOTH tables (it refuses inside the agent runtime,
#: not at an HTTP boundary today) — it must still be parity-consistent: 500 + code header, both paths.
MATRIX_CODES = DIVERGING + CONTROLS + [E_DEEPSEEK_KEY]

#: The exact canonical mapping, pinned as a literal: a code added to (or removed from) either table
#: without updating this census fails `test_the_status_tables_agree_exactly_and_match_the_census`.
EXPECTED_STATUS_BY_CODE: dict[str, int] = {
    E_AUTH: 401,
    E_VALIDATION: 422,
    E_NOT_FOUND: 404,
    E_AGENT_UNKNOWN: 422,
    E_TASK_CLAIMED: 409,
    E_TASK_NOT_RUNNABLE: 409,
    E_BUDGET_EXCEEDED: 429,
    E_RATE_LIMITED: 429,
    E_SANDBOX_BLOCKED: 403,
    E_POLICY_DENIED: 403,
    E_CONFIG: 503,
    E_REDIS: 503,
    E_DB: 503,
    E_STORAGE: 503,
    E_CLI_MISSING: 503,
    E_CLI_TIMEOUT: 504,
    E_CLI_EXIT: 502,
    E_CLI_JSON: 502,
    E_AGENT_BAD_OUTPUT: 502,
    E_INTEGRATION: 502,
    E_PROMPT_TOO_LARGE: 413,
}


class _Raiser:
    """A callable that raises `exc` — the dead-driver simulation for the real E_DB route leg."""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    async def __call__(self, *args, **kwargs):
        raise self.exc


def _refusal(code: str) -> PolsiaError:
    """The SAME refusal object shape for both legs, so the responses are byte-comparable."""
    return PolsiaError(code, f"r29 parity probe for {code}", context={"probe": code})


def _in_route_endpoint(code: str):
    """The documented in-route shape: `PolsiaError` in the body, converted by `refusal_guard`."""

    async def endpoint() -> dict:
        with refusal_guard():
            raise _refusal(code)
        return {}  # unreachable — the guard re-raises as the coded HTTPException

    return endpoint


def _pre_route_endpoint():
    """The endpoint behind the pre-route leg must never run: its dependency refuses first."""

    async def endpoint() -> dict:
        raise AssertionError("the dependency must refuse before the endpoint body runs")

    return endpoint


def _raising_dep(code: str):
    """A route-level dependency that raises the same refusal BEFORE the route body exists."""

    async def dep() -> None:
        raise _refusal(code)

    return dep


@pytest.fixture
def parity_routes():
    """Register the two throwaway route families on the REAL app; remove them afterwards.

    Test-only routes (like any test double, they live in tests/); both go through the shipped
    middleware and exception handlers, so what they measure is the production response plumbing.
    """
    before = list(app.router.routes)
    paths: dict[str, tuple[str, str]] = {}
    for code in MATRIX_CODES:
        in_path = f"/api/v1/__r29__/in/{code}"
        pre_path = f"/api/v1/__r29__/pre/{code}"
        app.add_api_route(in_path, _in_route_endpoint(code), methods=["GET"])
        app.add_api_route(
            pre_path,
            _pre_route_endpoint(),
            methods=["GET"],
            dependencies=[Depends(_raising_dep(code))],
        )
        paths[code] = (in_path, pre_path)
    try:
        yield paths
    finally:
        app.router.routes[:] = before


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# the tables
# ═══════════════════════════════════════════════════════════════════════════════════════════════


def test_the_status_tables_are_one_object_not_two():
    """`deps.STATUS_BY_CODE` must ALIAS the canonical table — a second literal cannot come back.

    Identity, not equality: with a shared object there is no code path, import order or mutation
    under which the two names can answer differently, and a future row added to
    `errors.HTTP_STATUS_BY_CODE` is automatically the row `deps.refusal_status` uses.
    """
    assert deps.STATUS_BY_CODE is errors.HTTP_STATUS_BY_CODE, (
        "deps.STATUS_BY_CODE must alias errors.HTTP_STATUS_BY_CODE, not shadow it"
    )


def test_the_status_tables_agree_exactly_and_match_the_census():
    """Equality both ways + the exact 21-row census: a code added to one side only fails here."""
    assert deps.STATUS_BY_CODE == errors.HTTP_STATUS_BY_CODE
    assert set(deps.STATUS_BY_CODE) == set(errors.HTTP_STATUS_BY_CODE)
    assert dict(errors.HTTP_STATUS_BY_CODE) == EXPECTED_STATUS_BY_CODE, (
        "the canonical mapping changed — update the census deliberately and check the parity "
        "matrix below still names every code"
    )
    for code, http_status in EXPECTED_STATUS_BY_CODE.items():
        assert deps.refusal_status(code) == http_status == errors.http_status_for(code)


def test_unmapped_codes_still_fall_back_to_500_on_both_helpers():
    """An unmapped code is a 500 on BOTH paths — one rule, not two (E_DEEPSEEK_KEY today)."""
    for code in (E_DEEPSEEK_KEY, "E_NOT_A_CODE"):
        assert code not in errors.HTTP_STATUS_BY_CODE, f"{code} unexpectedly mapped"
        assert errors.http_status_for(code) == 500 == deps.refusal_status(code)


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# the wire: pre-route == in-route, for every code
# ═══════════════════════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("code", MATRIX_CODES)
async def test_the_same_refusal_is_the_same_response_pre_route_and_in_route(
    api_client, auth_headers, parity_routes, code,
):
    """Raise `code` inside the route body and from a dependency; the two wires must be identical."""
    in_path, pre_path = parity_routes[code]
    headers = {**auth_headers, "x-request-id": f"r29-{code}"}

    in_resp = await api_client.get(in_path, headers=headers)
    pre_resp = await api_client.get(pre_path, headers=headers)

    expected_status = EXPECTED_STATUS_BY_CODE.get(code, 500)
    expected_envelope = {
        "ok": False,
        "error": {
            "code": code,
            "message": f"r29 parity probe for {code}",
            "how_to_fix": ERROR_HINTS[code],
            "context": {"probe": code},
        },
    }

    for leg, resp in (("in-route", in_resp), ("pre-route", pre_resp)):
        assert resp.status_code == expected_status, (
            f"{code} ({leg}): the documented status is {expected_status}, got {resp.status_code}"
        )
        assert resp.headers.get("X-Polsia-Error-Code") == code, (
            f"{code} ({leg}): the machine-readable code header is missing"
        )
        assert resp.headers.get("x-request-id") == f"r29-{code}", f"{code} ({leg}): request id lost"
        assert resp.headers.get("x-response-time-ms"), f"{code} ({leg}): timing header lost"
        assert resp.json() == expected_envelope, f"{code} ({leg}): envelope drift: {resp.json()}"

    # header-for-header equality: the timing header is a per-request measurement, compared as
    # presence above; everything else — content type, code header, request id — must be equal.
    in_headers = {k: v for k, v in in_resp.headers.items() if k != "x-response-time-ms"}
    pre_headers = {k: v for k, v in pre_resp.headers.items() if k != "x-response-time-ms"}
    assert pre_headers == in_headers, (
        f"{code}: pre-route headers differ from in-route headers:\n"
        f"  pre={sorted(pre_headers)}\n  in ={sorted(in_headers)}"
    )


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# the wire: the two REAL shipped pairs
# ═══════════════════════════════════════════════════════════════════════════════════════════════


async def test_real_e_db_refusal_matches_wherever_the_failure_happened(
    api_client, auth_headers, async_db_session, monkeypatch,
):
    """The R27 probe scenario through the shipped route: a broken read vs a refusing `get_db`."""
    monkeypatch.setattr(
        async_db_session, "execute", _Raiser(SQLAlchemyError("simulated driver failure"))
    )
    in_resp = await api_client.get("/api/v1/social/posts", headers=auth_headers)

    previous = app.dependency_overrides.get(get_db)
    try:

        async def _refusing_get_db():
            raise PolsiaError(E_DB, "could not open a session", how_to_fix=ERROR_HINTS[E_DB])

        app.dependency_overrides[get_db] = _refusing_get_db
        pre_resp = await api_client.get("/api/v1/social/posts", headers=auth_headers)
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = previous

    assert in_resp.status_code == pre_resp.status_code == 503, (
        f"E_DB must be 503 wherever it is raised: in={in_resp.status_code} "
        f"pre={pre_resp.status_code}"
    )
    for leg, resp in (("in-route", in_resp), ("pre-route", pre_resp)):
        assert resp.headers["X-Polsia-Error-Code"] == E_DB, leg
        body = resp.json()
        assert body["ok"] is False and body["error"]["code"] == E_DB, leg
        assert body["error"]["how_to_fix"] == ERROR_HINTS[E_DB], leg
    # the same envelope shape and code+fix; the message names the layer that failed (by design)
    assert set(pre_resp.json()) == set(in_resp.json()) == {"ok", "error"}
    assert set(pre_resp.json()["error"]) == set(in_resp.json()["error"])
    assert "could not read social posts" in in_resp.json()["error"]["message"]
    assert "could not open a session" in pre_resp.json()["error"]["message"]


async def test_real_e_auth_is_one_response_from_both_shipped_raise_sites(api_client):
    """`verify_api_key` (dependency) and `check_api_key` (in the body) must answer identically."""
    wrong = {"X-API-Key": "definitely-wrong", "x-request-id": "r29-e-auth"}
    pre_resp = await api_client.get("/api/v1/config", headers=wrong)          # router dependency
    in_resp = await api_client.post("/api/v1/agents/social_media/trigger", headers=wrong)  # body

    for leg, resp in (("verify_api_key", pre_resp), ("check_api_key", in_resp)):
        assert resp.status_code == 401, leg
        assert resp.headers.get("X-Polsia-Error-Code") == E_AUTH, leg
        assert resp.headers.get("WWW-Authenticate") == "X-API-Key", leg
        body = resp.json()
        assert body["ok"] is False and body["error"]["code"] == E_AUTH, leg
        assert body["error"]["how_to_fix"], f"{leg}: a refusal must say how to fix it"

    assert pre_resp.json() == in_resp.json(), "the two E_AUTH raise sites drifted apart"
    in_headers = {k: v for k, v in in_resp.headers.items() if k != "x-response-time-ms"}
    pre_headers = {k: v for k, v in pre_resp.headers.items() if k != "x-response-time-ms"}
    assert pre_headers == in_headers
