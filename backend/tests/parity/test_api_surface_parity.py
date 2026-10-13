"""API-surface parity — the route table `create_app()` publishes, pinned route by route.

WHY THIS GUARD EXISTS
    The route table is the API's public contract: the Next.js frontend, nginx, the dashboard's live
    socket and the shipped tests all depend on it, and until now nothing checked it. The older
    `test_contract_parity.py` pins only the *documented* routes, and only in one direction (a
    documented route must exist); a route that is **added, renamed or removed** passes it untouched
    as long as the tests keep importing the app object instead of the path. A rename is the
    dangerous shape of that: every shipped test stays green while the running system 404s. Adding,
    renaming or deleting a route is a deliberate act, and this test makes it a deliberate act — the
    commit that changes the surface updates `EXPECTED_ROUTES` in the same commit.

THE DOCS-ROUTES DECISION (decided once, here, and not revisited)
    `create_app()` leaves FastAPI's `docs_url` / `redoc_url` / `openapi_url` at their defaults, so
    the framework appends four routes that no module in this repo declares: `/openapi.json`,
    `/docs`, `/docs/oauth2-redirect`, `/redoc` (all GET+HEAD). They are **NOT** in
    `EXPECTED_ROUTES`. Reason: they are a property of the installed FastAPI version rather than of
    this API's contract, and mixing the two makes a framework bump look like product drift — the
    cost of a gate that fires for reasons the reader did not choose is that people start updating
    its list without reading it, which is how the gate dies. They are also the routes a hardened
    deployment is most likely to switch off (`docs_url=None`), and pinning them inside the product
    contract would turn a deployment option into a product promise. They are **measured anyway**:
    `test_framework_docs_surface_is_pinned_separately` below keeps them as their own state, so
    leaving them out of the contract is not the same as not looking.

HOW THE SURFACE IS ENUMERATED
    In-process from `create_app()` — the same constructor `app.main` runs at import time, and the
    same call `uvicorn app.main:app` serves — so the list cannot describe a different app. A
    test-fixture app is deliberately NOT used: a route table that exists only under test
    configuration (an extra prefix, overridden dependencies, docs disabled) is a contract no
    deployment serves, and baking it in would pin the test harness instead of the API.
    A websocket route carries `methods is None` rather than a method set. It is kept, with `None`
    as the sentinel, instead of being skipped: `/ws/activity` is the dashboard's live feed, and
    dropping it here would make this guard blind to exactly the route whose loss is hardest to see
    from the HTTP side. `None` therefore means "not an HTTP-method route", never "unknown".
"""
from __future__ import annotations

from app.main import create_app

# (path, methods) — methods is a sorted tuple, or None for a websocket route (see the docstring).
# Pinned in the order FastAPI publishes it; the comparison sorts both sides, so ordering here is
# documentation, not assertion.
EXPECTED_ROUTES: list[tuple[str, tuple[str, ...] | None]] = [
    ("/", ("GET",)),                                           # include_in_schema=False, still served
    ("/api/v1/ads/campaigns", ("GET",)),
    ("/api/v1/agents/status", ("GET",)),
    ("/api/v1/agents/{agent_type}/trigger", ("POST",)),
    ("/api/v1/config", ("GET",)),
    ("/api/v1/config", ("PUT",)),
    ("/api/v1/config/deepseek-key", ("DELETE",)),               # DeepSeek key lifecycle (masked; owner-managed)
    ("/api/v1/config/deepseek-key", ("GET",)),
    ("/api/v1/config/deepseek-key", ("PUT",)),
    ("/api/v1/config/deepseek-key/verify", ("POST",)),
    ("/api/v1/dashboard/activity", ("GET",)),
    ("/api/v1/dashboard/reports/daily", ("GET",)),
    ("/api/v1/dashboard/summary", ("GET",)),
    ("/api/v1/finance/events", ("GET",)),
    ("/api/v1/finance/expenses", ("GET",)),
    ("/api/v1/finance/revenue", ("GET",)),
    ("/api/v1/finance/stripe/webhook", ("POST",)),             # unauthenticated by design
    ("/api/v1/finance/summary", ("GET",)),
    ("/api/v1/health", ("GET",)),
    ("/api/v1/memory", ("GET",)),
    ("/api/v1/memory", ("POST",)),
    ("/api/v1/memory/{memory_id}", ("DELETE",)),                # the memory-delete lane's route (R19); pinned here because R17 owns this file this wave and the lane left it unpinned
    ("/api/v1/ops/events/reset", ("POST",)),                     # R22: clear the activity publish breaker after an incident (owner)
    ("/api/v1/ops/events/stats", ("GET",)),                      # R22: the breaker's counters + last error (owner)
    ("/api/v1/outreach/prospects", ("GET",)),
    ("/api/v1/social/posts", ("GET",)),
    ("/api/v1/support/inbox", ("GET",)),                        # R17: the support inbox list (owner)
    ("/api/v1/support/inbox", ("POST",)),                       # R17: the support inbox submit (owner)
    ("/api/v1/tasks", ("GET",)),
    ("/api/v1/tasks", ("POST",)),
    ("/api/v1/tasks/{task_id}", ("GET",)),
    ("/ws/activity", None),                                    # websocket: methods is None
]

# FastAPI's own surface, held as a separate state (see the docs-routes decision in the docstring).
FRAMEWORK_DOCS_ROUTES: list[tuple[str, tuple[str, ...] | None]] = [
    ("/docs", ("GET", "HEAD")),
    ("/docs/oauth2-redirect", ("GET", "HEAD")),
    ("/openapi.json", ("GET", "HEAD")),
    ("/redoc", ("GET", "HEAD")),
]

FRAMEWORK_PATHS = frozenset(path for path, _ in FRAMEWORK_DOCS_ROUTES)

RouteKey = tuple[str, tuple[str, ...] | None]


def _live_routes() -> list[RouteKey]:
    """Every route `create_app()` publishes, framework routes included, sorted.

    Built from `app.routes`, which is what the server mounts — not from the router modules, so a
    router that is imported but never `include_router`'d shows up as missing here (the "declared
    but never wired" defect class) instead of being counted twice.
    """
    app = create_app()
    rows: list[RouteKey] = []
    for route in app.routes:
        path = getattr(route, "path", None)
        if not path:  # Mounts and middleware have no routable path — not part of the surface.
            continue
        methods = getattr(route, "methods", None)
        rows.append((path, tuple(sorted(methods)) if methods else None))
    return sorted(rows)


# ── 1. the product contract ───────────────────────────────────────────────────────────────────
def test_the_published_surface_is_exactly_the_pinned_one() -> None:
    live = sorted(row for row in _live_routes() if row[0] not in FRAMEWORK_PATHS)
    expected = sorted(EXPECTED_ROUTES)
    added, removed = sorted(set(live) - set(expected)), sorted(set(expected) - set(live))
    assert live == expected, (
        f"route drift: added={added} removed={removed} — added= you published a surface no client "
        "knows about, removed= you unpublished one a client depends on. If the change is "
        "deliberate, update EXPECTED_ROUTES in this commit."
    )


def test_the_pinned_surface_carries_no_duplicate_rows() -> None:
    """A duplicated (path, methods) pair would make the set-difference above hide one of them."""
    duplicates = sorted({row for row in EXPECTED_ROUTES if EXPECTED_ROUTES.count(row) > 1})
    assert not duplicates, f"EXPECTED_ROUTES lists these twice: {duplicates}"


# ── 2. the framework surface, pinned as its own state ─────────────────────────────────────────
def test_framework_docs_surface_is_pinned_separately() -> None:
    """`create_app()` uses FastAPI's default docs URLs; this records that choice, separately.

    These four routes are not this repo's declarations (see the docstring), so they live outside
    `EXPECTED_ROUTES` — but "outside the contract" must not read as "unmeasured". Turning the
    framework surface off (`FastAPI(docs_url=None)`) or a FastAPI bump that renames one of them is
    a deliberate act too, and it updates this list in the same commit.
    """
    live = sorted(row for row in _live_routes() if row[0] in FRAMEWORK_PATHS)
    expected = sorted(FRAMEWORK_DOCS_ROUTES)
    added, removed = sorted(set(live) - set(expected)), sorted(set(expected) - set(live))
    assert live == expected, f"framework route drift: added={added} removed={removed}"
