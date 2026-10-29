"""SV-W6-02 (wave 8, executed): the 422 declaration has TWO wire shapes — and the set is a census.

WHY THIS FILE EXISTS
--------------------
Wave 7 measured the contradiction live on the rebuilt app
(`systems-validator/raw/w7/live-w7-422-shapes.txt`): ONE status, TWO bodies, on the SAME operation —

    GET /api/v1/support/inbox?status=bogus   -> 422  {"ok": false, "error": {"code": "E_VALIDATION", …}}
    GET /api/v1/support/inbox?limit=0        -> 422  {"detail": [{loc, msg, type, …}]}

The envelope comes from `main.polsia_error_handler` / `main.coded_http_exception_handler`
(`errors.HTTP_STATUS_BY_CODE[E_VALIDATION] == 422`); the array comes from FastAPI's
`RequestValidationError` path.  The schema declared only the array on all 111 operations that declare
422, so `app/core/openapi_errors.py` now declares `anyOf(HTTPValidationError, PolsiaErrorEnvelope)` on
the 109 operations whose handler path can reach a 422-mapped named refusal (`E_VALIDATION`,
`E_AGENT_UNKNOWN`) and leaves the framework array on the two that cannot.

ARITHMETIC (the pin, old -> new)
--------------------------------
  wave 7 schema:  111 x 422 = 111 x $ref HTTPValidationError                    (one shape declared)
  wave 8 schema:  111 x 422 = 109 x anyOf(HTTPValidationError, PolsiaErrorEnvelope)
                             +  2 x $ref HTTPValidationError                    (GET /pricing, POST /ws/ticket)
  other statuses: identical per operation (the proof in
                  `systems-validator/raw/w8/prove-422.txt` shows an empty response-key diff)

THE SET MOVES ONLY WITH A CENSUS
--------------------------------
`CODED_422_OPERATIONS` is static on purpose (the module's idiom), so it can rot: a new route, or a
new `E_VALIDATION` raise in a module some handler imports, would silently keep the array-only
declaration.  `test_the_declared_set_is_the_recomputed_census` therefore re-runs the wave-8 census on
every test run (the handler's defining module + its direct `app.*` imports, and — for the exclusions —
the whole transitive `app.*` closure) and fails until the set, this file and the schema agree.

THE MEASURED `oneOf` CAVEAT (SV-W8-03, reported to the Lead)
------------------------------------------------------------
`HTTPValidationError` declares no `required`, so the ENVELOPE body validates against BOTH branches and
a strict `oneOf` rejects it (`jsonschema==4.23.0` against the bodies captured from the running app:
`raw/w8/prove-422.json`).  The declaration is still more truthful than the array-only one it
replaces, and the entry's description tells a client how to discriminate; `anyOf` is the correction
the report asks the Lead to rule on.  These pins assert the DECLARED shape (oneOf) and the LIVE shapes
(which are unaffected), not a `oneOf`-validates-everything fiction.
"""
from __future__ import annotations

import ast
import json
import pathlib
import re

import pytest
from fastapi.routing import APIRoute

from app.core.openapi_errors import CODED_422_OPERATIONS, PolsiaErrorEnvelope
from app.main import create_app

#: backend/ (this file lives in backend/tests/unit/).
BACKEND = pathlib.Path(__file__).resolve().parents[2]
APP_ROOT = BACKEND / "app"

#: The only codes `errors.HTTP_STATUS_BY_CODE` maps to 422 (recomputed below, not assumed).
VALIDATION_CODES = {"E_VALIDATION", "E_AGENT_UNKNOWN"}

#: 111 operations declare 422 today; 109 of them are coded-capable (the census), 2 are not.
EXPECTED_DECLARING_422 = 111
EXPECTED_CODED = 109

#: The two operations that keep FastAPI's automatic array — no 422-mapped raise site anywhere in
#: their `app.*` import closure.  Named here so an exclusion can never be silent.
ARRAY_ONLY = frozenset({("GET", "/api/v1/pricing"), ("POST", "/api/v1/ws/ticket")})

ENVELOPE_REF = "#/components/schemas/PolsiaErrorEnvelope"
ARRAY_REF = "#/components/schemas/HTTPValidationError"


# ── fixtures ─────────────────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def app_under_test():
    return create_app()


@pytest.fixture(scope="module")
def schema(app_under_test) -> dict:
    return app_under_test.openapi()


@pytest.fixture(scope="module")
def operations(schema: dict) -> dict[tuple[str, str], dict]:
    return {
        (method.upper(), path): operation
        for path, item in schema["paths"].items()
        for method, operation in item.items()
        if method in ("get", "post", "put", "patch", "delete")
    }


@pytest.fixture(scope="module")
def routes(app_under_test) -> dict[tuple[str, str], APIRoute]:
    found: dict[tuple[str, str], APIRoute] = {}
    for route in app_under_test.routes:
        if isinstance(route, APIRoute) and route.include_in_schema:
            for method in route.methods or ():
                found[(method.upper(), route.path)] = route
    return found


# ── the census (the same rule the wave-8 receipt used) ──────────────────────────────────────────


def _module_name(path: pathlib.Path) -> str:
    rel = path.relative_to(BACKEND).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _imports_of(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names if alias.name.startswith("app"))
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app"):
            found.add(node.module)
            found.update(
                f"{node.module}.{alias.name}" for alias in node.names if alias.name != "*"
            )
    return found


def _raises_422(path: pathlib.Path) -> bool:
    """Does this module raise a 422-mapped code through PolsiaError/named_refusal/"raise"?"""
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Raise) and node.exc is not None:
            for sub in ast.walk(node.exc):
                if isinstance(sub, ast.Name) and sub.id in VALIDATION_CODES:
                    return True
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name in ("named_refusal", "PolsiaError"):
                for arg in node.args[:2]:
                    for sub in ast.walk(arg):
                        if isinstance(sub, ast.Name) and sub.id in VALIDATION_CODES:
                            return True
    return False


@pytest.fixture(scope="module")
def module_index() -> tuple[dict[str, pathlib.Path], set[str]]:
    files: dict[str, pathlib.Path] = {}
    raisers: set[str] = set()
    for path in APP_ROOT.rglob("*.py"):
        name = _module_name(path)
        files[name] = path
        if _raises_422(path):
            raisers.add(name)
    return files, raisers


def _closure(
    name: str,
    files: dict[str, pathlib.Path],
    cache: dict[str, set[str]] | None = None,
    seen: set[str] | None = None,
) -> set[str]:
    cache = cache if cache is not None else {}
    if name in cache:
        return cache[name]
    seen = seen or set()
    if name in seen:
        return set()
    seen.add(name)
    out = {name}
    path = files.get(name)
    if path is not None:
        for imported in _imports_of(path):
            candidate = imported if imported in files else imported.rsplit(".", 1)[0]
            if candidate in files:
                out |= _closure(candidate, files, cache, seen)
    cache[name] = out
    return out


def _shapes(operation: dict) -> str:
    schema = (
        (operation.get("responses") or {}).get("422", {}).get("content", {})
        .get("application/json", {})
        .get("schema", {})
    )
    if "$ref" in schema:
        return "ref:" + schema["$ref"].rsplit("/", 1)[-1]
    if "anyOf" in schema:
        return "anyOf:" + ",".join(part.get("$ref", "?").rsplit("/", 1)[-1] for part in schema["anyOf"])
    return "other:" + json.dumps(schema)[:100]


# ── the schema pins ─────────────────────────────────────────────────────────────────────────────


def test_422_stays_declared_and_the_two_shapes_add_up(operations: dict) -> None:
    """111 declared 422; exactly 109 anyOf + 2 array — the accepted arithmetic, measured."""
    declaring = {target for target, op in operations.items() if "422" in op.get("responses", {})}
    assert len(declaring) == EXPECTED_DECLARING_422, sorted(declaring)

    one_of = {target for target in declaring if _shapes(operations[target]).startswith("anyOf:")}
    array = {target for target in declaring if _shapes(operations[target]).startswith("ref:")}
    assert one_of == set(CODED_422_OPERATIONS), {
        "declared oneOf but not in the set": sorted(one_of - set(CODED_422_OPERATIONS)),
        "in the set but still array-only": sorted(set(CODED_422_OPERATIONS) - one_of),
    }
    assert array == set(ARRAY_ONLY), sorted(array)
    assert len(one_of) == EXPECTED_CODED, len(one_of)
    assert len(one_of) + len(array) == len(declaring) == 111
    assert all(_shapes(operations[target]) == "anyOf:HTTPValidationError,PolsiaErrorEnvelope"
               for target in one_of)


def test_every_set_member_is_a_real_operation_that_declares_422(operations: dict) -> None:
    """A typo in the 109-entry literal would silently keep the array declaration — catch it here."""
    unknown = sorted(f"{m} {p}" for m, p in CODED_422_OPERATIONS if (m, p) not in operations)
    assert unknown == [], f"CODED_422_OPERATIONS names operations the schema does not have: {unknown}"
    without = sorted(
        f"{m} {p}" for m, p in CODED_422_OPERATIONS
        if "422" not in operations[(m, p)].get("responses", {})
    )
    assert without == [], f"operations in the set that do not declare a 422 at all: {without}"


def test_the_declared_set_is_the_recomputed_census(
    operations: dict,
    routes: dict[tuple[str, str], APIRoute],
    module_index: tuple[dict[str, pathlib.Path], set[str]],
) -> None:
    """The wave-8 census, recomputed: handler module + its direct `app.*` imports.

    If this fails, do NOT edit the expected number: re-run
    `systems-validator/raw/w8/coded422_census_w8.py`, then add/remove the operation in
    `app/core/openapi_errors.CODED_422_OPERATIONS` and update the report — deliberately.
    """
    files, raisers = module_index
    computed: set[tuple[str, str]] = set()
    for target, operation in operations.items():
        if "422" not in operation.get("responses", {}):
            continue
        route = routes.get(target)
        module = getattr(getattr(route, "endpoint", None), "__module__", None)
        if not module:
            continue
        path = files.get(module)
        reached = module in raisers
        if path is not None and not reached:
            for imported in _imports_of(path):
                candidate = imported if imported in files else imported.rsplit(".", 1)[0]
                if candidate in raisers:
                    reached = True
                    break
        if reached:
            computed.add(target)
    assert computed == set(CODED_422_OPERATIONS), {
        "recomputed but not declared (add them)": sorted(f"{m} {p}" for m, p in computed - set(CODED_422_OPERATIONS)),
        "declared but not recomputed (remove them)": sorted(f"{m} {p}" for m, p in set(CODED_422_OPERATIONS) - computed),
    }


def test_the_two_array_only_operations_have_no_coded_raise_site(
    routes: dict[tuple[str, str], APIRoute],
    module_index: tuple[dict[str, pathlib.Path], set[str]],
) -> None:
    """The exclusions are exact in the STRONG direction: no raiser anywhere in the closure.

    `GET /api/v1/pricing` refuses with `E_CONFIG` 503 (a missing/drifted catalog) and
    `POST /api/v1/ws/ticket` with `E_RATE_LIMITED` 429 (the mint bounds); their 422 is the framework's
    own array (the query bounds / the header param).  The transitive closure is checked, not just the
    direct imports, so a deeper module cannot invalidate the exclusion silently.
    """
    files, raisers = module_index
    offenders = {}
    for method, path in ARRAY_ONLY:
        route = routes[(method, path)]
        module = route.endpoint.__module__
        closure = _closure(module, files)
        hit = sorted(closure & raisers)
        if hit:
            offenders[f"{method} {path}"] = hit
    assert offenders == {}, offenders


def test_multi_method_paths_are_uniform(operations: dict) -> None:
    """A route object shares ONE `responses` dict across its methods.

    `declarations_for` looks the set up per method, so a path whose methods disagree would get the
    oneOf for ALL of them — an over-claim on the array-only method that the route model cannot
    express.  Today every multi-method path is uniformly coded; this pin keeps it that way.
    """
    by_path: dict[str, set[str]] = {}
    for (method, path) in operations:
        by_path.setdefault(path, set()).add(method)
    multi = {path: methods for path, methods in by_path.items() if len(methods) > 1}
    mixed = {
        path: sorted(methods)
        for path, methods in multi.items()
        if len({(method, path) in CODED_422_OPERATIONS for method in methods}) > 1
    }
    assert mixed == {}, mixed
    assert len(multi) == 18, (
        f"the multi-method path count moved ({len(multi)}): re-check this pin's premise"
    )


def test_the_components_every_422_ref_points_at_still_exist(schema: dict) -> None:
    """`oneOf` names two components; the 2 array-only operations keep FastAPI's definitions alive."""
    components = schema["components"]["schemas"]
    for name in ("HTTPValidationError", "ValidationError", "PolsiaErrorEnvelope", "PolsiaErrorDetail"):
        assert name in components, sorted(components)
    referenced = {
        ref.rsplit("/", 1)[-1]
        for ref in re.findall(r"#/components/schemas/([A-Za-z0-9_.]+)", json.dumps(schema))
    }
    dangling = sorted(referenced - set(components))
    assert dangling == [], f"$refs with no component (a generated client fails here): {dangling}"


# ── the live shapes (the declaration must describe what the app really sends) ───────────────────


@pytest.mark.asyncio
async def test_the_three_422_wire_shapes_are_real(api_client, auth_headers) -> None:
    """Envelope / array / envelope+detail — driven through the REAL app, not asserted from code.

    These are the three bodies `raw/w8/prove-422.txt` captured from the ASGI app; the declaration is
    documentation only, so this test also proves the runtime did not move with it.
    """
    # 1. a NAMED refusal -> the envelope, with the machine-readable code header
    coded = await api_client.post(
        "/api/v1/tasks", headers=auth_headers, json={"title": "probe", "agent_type": "not-an-agent"}
    )
    assert coded.status_code == 422, coded.text
    coded_body = coded.json()
    assert coded_body["ok"] is False
    assert coded_body["error"]["code"] == "E_AGENT_UNKNOWN"
    assert "detail" not in coded_body, "the framework array must not be mixed into a plain refusal"
    assert coded.headers.get("x-polsia-error-code") == "E_AGENT_UNKNOWN"
    PolsiaErrorEnvelope.model_validate(coded_body)  # the declared component accepts the wire

    # 2. a framework validation error -> the array, uncoded (the documented exception)
    array = await api_client.get("/api/v1/ads/campaigns", headers=auth_headers, params={"limit": 0})
    assert array.status_code == 422, array.text
    array_body = array.json()
    assert set(array_body) == {"detail"} and isinstance(array_body["detail"], list)
    assert array.headers.get("x-polsia-error-code") is None

    # 3. the non-finite input class -> the envelope PLUS the sanitized array under `detail`
    raw = await api_client.post(
        "/api/v1/tasks",
        content=b'{"title": NaN}',
        headers={**auth_headers, "content-type": "application/json"},
    )
    assert raw.status_code == 422, raw.text
    raw_body = raw.json()
    assert raw_body["error"]["code"] == "E_VALIDATION"
    assert raw.headers.get("x-polsia-error-code") == "E_VALIDATION"
    assert [item for item in raw_body["detail"] if item.get("loc") == ["body", "title"]]
