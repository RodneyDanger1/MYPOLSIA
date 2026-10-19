"""Wave V21 / lane L1 — the gates and surfaces this lane hardened, as runnable pins.

WHAT IS PINNED HERE, AND WHY EACH PIN IS A GATE RATHER THAN A COMMENT
  1. `mypy app` is a HARD CI gate.  `ci.yml` carried `mypy app/ || true`, so four real type
     errors (three in `services/phone_send_service.py`, one in `api/v1/runs.py`) could never
     fail a job.  The three this lane owns are fixed at the source; the reviewed form of that
     fix is pinned by the phone tests at the bottom of this file.
  2. `X-Polsia-Total` is CORS-exposed.  `api/v1/totals.py` added the header in V20 for a
     cross-origin browser client, but `expose_headers` never listed it, so JS could not read it
     (only CORS-safelisted headers are readable without it).
  3. `/apidocs` reaches FastAPI's Swagger UI THROUGH nginx.  `location /` sends `/docs` to the
     Next app (its operator page — deliberately untouched) and `/openapi.json` nowhere, so the
     API's own docs were unreachable through the proxy.  The fix is a pair of nginx routes onto
     the backend's EXISTING `/docs` + `/openapi.json`, which keeps this API's route table
     byte-identical (`tests/parity/test_api_surface_parity.py` stays exact) and needs no
     `docs_url` change.  Both routes inherit the 2R-02 sandbox gate: with SANDBOX_MODE=false the
     backend answers 404 and nginx passes that 404 through.
  4. The phone bridge's "unreachable" message renders `urllib.error.URLError.reason`, which
     typeshed declares `str | BaseException` — the one runtime-visible line touched by the mypy
     work.  A string reason must read as the reason, never as `str: <reason>`.

Nothing here runs nginx or docker: the live half is the parent's reload + curl (PARENT
LIVE-CHECK in `wave-v21/l1-gate-hardening/RECEIPT.md`).  Raw RED output:
`wave-v21/l1-gate-hardening/raw/red-tests.txt`.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app.config import settings
from app.core.errors import E_PHONE_UNAVAILABLE, PolsiaError
from app.main import create_app
from app.services.phone_send_service import (
    E_PHONE_SEND_UNPAIRED,
    _bridge_unreachable,
    parse_driver_stdout,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
NGINX_CONF = (REPO_ROOT / "nginx" / "nginx.conf").read_text(encoding="utf-8")
CI_YML = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")


def _block(text: str, header: str) -> str:
    """The body of the first `header { ... }` block, braces balanced."""
    start = text.index(header)
    depth = 0
    for index in range(start, len(text)):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise AssertionError(f"unbalanced braces after {header!r}")


# ── 1. the mypy step is a hard gate ───────────────────────────────────────────────────────────


def test_the_mypy_ci_step_is_a_hard_gate() -> None:
    """`mypy app/` must be the step's command and must not swallow its exit status."""
    lines = CI_YML.splitlines()
    step_at = next(
        index for index, line in enumerate(lines) if line.strip() == "- name: mypy"
    )
    # The step carries an explanatory comment block, so scan a window rather than a fixed offset.
    run_line = next(
        line for line in lines[step_at : step_at + 12] if line.strip().startswith("run:")
    )
    assert "mypy app" in run_line, run_line
    assert "|| true" not in run_line, (
        f"the mypy step is advisory again ({run_line.strip()!r}) — a type error must fail CI"
    )


# ── 2. X-Polsia-Total is readable cross-origin ───────────────────────────────────────────────


def _exposed(response) -> set[str]:
    raw = response.headers.get("access-control-expose-headers") or ""
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


@pytest.mark.asyncio
async def test_polsia_total_is_cors_exposed(api_client) -> None:
    origin = settings.cors_origins[0]
    resp = await api_client.get("/api/v1/health", headers={"Origin": origin})
    assert resp.status_code == 200
    exposed = _exposed(resp)
    assert "x-polsia-total" in exposed, resp.headers.get("access-control-expose-headers")
    # The V7/V18 set must survive the addition — one header list, one contract.
    assert {
        "x-polsia-error-code",
        "x-request-id",
        "x-response-time-ms",
        "x-polsia-row-count",
        "content-disposition",
    } <= exposed, resp.headers.get("access-control-expose-headers")


# ── 3. the API docs are reachable THROUGH nginx, and Next keeps `/docs` ──────────────────────


def test_nginx_routes_apidocs_to_the_backends_own_docs_page() -> None:
    block = _block(NGINX_CONF, "location = /apidocs {")
    assert "proxy_pass http://$backend_up/docs;" in block, block


def test_nginx_routes_the_openapi_schema_beside_it() -> None:
    block = _block(NGINX_CONF, "location = /openapi.json {")
    assert "proxy_pass http://$backend_up;" in block, block
    # No location-level add_header here: the server-scope hardening (CSP/nosniff/XFO) must apply
    # to the schema response unchanged.
    assert "add_header" not in block, block


def test_apidocs_allows_the_swagger_cdn_under_csp() -> None:
    """200 with a blank page is a false success: Swagger UI's assets live on a CDN.

    The server-scope CSP allows `'self'` only, and a location-level add_header REPLACES the
    inherited set — so the docs location must (a) widen script/style/img for the CDN the page
    actually loads and (b) repeat the other two hardening headers it would otherwise drop.
    """
    block = _block(NGINX_CONF, "location = /apidocs {")
    assert "https://cdn.jsdelivr.net" in block, block
    for header in ("Content-Security-Policy", "X-Frame-Options", "X-Content-Type-Options"):
        assert header in block, f"{header} would be dropped by the location-level add_header"
    csp = next(line for line in block.splitlines() if "Content-Security-Policy" in line)
    assert "unsafe-eval" not in csp, "Swagger UI does not need eval; do not grant it"


def test_next_keeps_docs_untouched() -> None:
    """The frontend's `/docs` operator page must not be shadowed by the API route."""
    assert "location = /docs {" not in NGINX_CONF
    assert "location /docs" not in NGINX_CONF


def test_apidocs_is_a_proxy_path_not_an_api_route(monkeypatch: pytest.MonkeyPatch) -> None:
    """The route table is unchanged by design: nginx maps the public path onto `/docs`."""
    monkeypatch.setattr(settings, "sandbox_mode", True)
    paths = {getattr(route, "path", None) for route in create_app().routes}
    assert "/apidocs" not in paths, (
        "adding /apidocs to the app would break the exact API-surface parity pin; the proxy is "
        "the seam"
    )


def test_the_backend_serves_the_docs_and_schema_the_proxy_maps_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of the route: `/docs` + `/openapi.json` answer 200 under the sandbox gate."""
    monkeypatch.setattr(settings, "sandbox_mode", True)
    with TestClient(create_app()) as client:
        schema = client.get("/openapi.json")
        assert schema.status_code == 200
        document = schema.json()
        assert "openapi" in document
        assert document["paths"], "a schema with no paths documents nothing"
        docs = client.get("/docs")
        assert docs.status_code == 200
        assert "swagger-ui" in docs.text.lower()


# ── 4. the phone-send fixes that made the type gate pass ─────────────────────────────────────


def test_unreachable_bridge_renders_a_string_reason_without_a_fake_type() -> None:
    """`URLError.reason` is `str | BaseException`; the str arm must not print `str: …`."""
    err = _bridge_unreachable("http://127.0.0.1:9", "Connection refused")
    assert err.code == E_PHONE_UNAVAILABLE
    assert "Connection refused" in err.message
    assert "str:" not in err.message


def test_unreachable_bridge_keeps_the_type_when_the_reason_is_an_exception() -> None:
    err = _bridge_unreachable("http://127.0.0.1:9", TimeoutError("read timed out"))
    assert "TimeoutError: read timed out" in err.message


def test_driver_refusal_keeps_the_context_object() -> None:
    """The mypy fix binds `error["context"]` once; the refusal must still carry it."""
    payload = {
        "ok": False,
        "error": {
            "code": E_PHONE_SEND_UNPAIRED,
            "message": "pair the phone first",
            "how_to_fix": "run the pairing flow",
            "context": {"ticket": "abc123"},
        },
    }
    with pytest.raises(PolsiaError) as info:
        parse_driver_stdout(3, json.dumps(payload))
    assert info.value.code == E_PHONE_SEND_UNPAIRED
    assert info.value.context == {"ticket": "abc123", "driver_exit": 3}
