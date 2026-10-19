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
    ("/api/v1/approvals", ("POST",)),                            # T02/D1: the owner's approval-token mint
    # wave U5 (P2/B22): the bounded Auto Mode window — OLD surface had no auto-mode route; NEW
    # pins added deliberately with the routes (count 36 -> 39): POST start (ends_at = now +
    # duration, 1 h..5 d), GET status (live counters; lazy duration_end close), DELETE stop early
    # (stop_reason user_stop). Owner-key; one active session.
    ("/api/v1/auto-mode", ("DELETE",)),
    ("/api/v1/auto-mode", ("GET",)),
    ("/api/v1/auto-mode", ("POST",)),
    ("/api/v1/channels/caps", ("GET",)),                         # wave U3 (H-01): per-channel send counters + day caps (channel_sends_snapshot + RateCaps); OLD surface had no caps route — NEW pin added deliberately with the route; NULL = unknown, never 0
    # wave V8 (C1): the owner <-> Polsia chat — POST message, POST confirm, GET history. OLD
    # surface had no chat routes; NEW pins added deliberately (count 41 -> 44). Owner-key.
    ("/api/v1/chat", ("GET",)),
    ("/api/v1/chat", ("POST",)),
    ("/api/v1/chat/confirm", ("POST",)),
    ("/api/v1/config", ("GET",)),
    ("/api/v1/config", ("PUT",)),
    ("/api/v1/config/deepseek-key", ("DELETE",)),               # DeepSeek key lifecycle (masked; owner-managed)
    ("/api/v1/config/deepseek-key", ("GET",)),
    ("/api/v1/config/deepseek-key", ("PUT",)),
    ("/api/v1/config/deepseek-key/verify", ("POST",)),
    ("/api/v1/dashboard/activity", ("GET",)),
    ("/api/v1/dashboard/reports", ("GET",)),                     # wave V8 (C5; WAVE-PLAN V8-D7 anticipated it): the daily-reports LIST surface — OLD surface had only /reports/daily; NEW pin added deliberately with the route. Owner-key.
    ("/api/v1/dashboard/reports/daily", ("GET",)),
    ("/api/v1/dashboard/summary", ("GET",)),
    ("/api/v1/finance/events", ("GET",)),
    ("/api/v1/finance/expenses", ("GET",)),
    # wave V9 (B2, V9-D3): the planning view (burn/capacity/margin + guardrails + tax notes,
    # catalog provenance on the payload). OLD surface had no planning route — NEW pin added
    # deliberately with the route. Owner-key.
    ("/api/v1/finance/planning", ("GET",)),
    ("/api/v1/finance/revenue", ("GET",)),
    ("/api/v1/finance/stripe/webhook", ("POST",)),             # unauthenticated by design
    ("/api/v1/finance/summary", ("GET",)),
    ("/api/v1/health", ("GET",)),
    ("/api/v1/memory", ("GET",)),
    ("/api/v1/memory", ("POST",)),
    ("/api/v1/memory/{memory_id}", ("DELETE",)),                # the memory-delete lane's route (R19); pinned here because R17 owns this file this wave and the lane left it unpinned
    ("/api/v1/notifications", ("GET",)),                          # wave U2 (B1): the bell's task-derived feed — OLD surface had no notifications route (Topbar.tsx refused the feature for its absence); NEW pin added deliberately with the route
    # wave V9 (B2, V9-D3): the per-agent quality/economics read over the runs ledger (null-honest,
    # deterministic). OLD surface had no agent-quality route — NEW pin added deliberately with the
    # route. Owner-key, sibling of the R22 breaker rows below.
    ("/api/v1/ops/agent-quality", ("GET",)),
    ("/api/v1/ops/events/reset", ("POST",)),                     # R22: clear the activity publish breaker after an incident (owner)
    ("/api/v1/ops/events/stats", ("GET",)),                      # R22: the breaker's counters + last error (owner)
    # wave V16 (E6/H-C1): the capability map — ONE derivation read live from AGENT_CAPABILITIES +
    # tool_registry (documented by app/api/v1/ops.py and tests/unit/api/test_v16_ops_capabilities.py).
    # Documented /api/v1 count 65 -> 66; EXPECTED_ROUTES total 67 -> 68.
    ("/api/v1/ops/capabilities", ("GET",)),
    # wave V16 (E5/P-02, frozen H-P1): the owner's phone, read-only. OLD surface had no phone
    # routes; NEW pins added deliberately WITH the routes. Every one is key-auth with the coded
    # refusal `E_PHONE_UNAVAILABLE` (503) when the Phone Link store is not mounted.
    ("/api/v1/phone/calls", ("GET",)),
    ("/api/v1/phone/codes", ("GET",)),                            # key-only + activity_log audit row per call
    ("/api/v1/phone/export", ("GET",)),                           # application/zip: messages.json + calls.csv + contacts.json
    ("/api/v1/phone/messages", ("GET",)),
    # wave V18 (L2): the media surface — list a thread's attachment parts + stream one part's bytes
    # from the real store (documented by app/api/v1/phone.py + tests/unit/api/test_v18_phone_media.py).
    ("/api/v1/phone/media", ("GET",)),
    ("/api/v1/phone/media/{part_id}", ("GET",)),
    # wave V17 (L2): the policy-gated SEND composer's route — sandbox / unpaired / unavailable /
    # validation refuse by name; one activity_log row per attempt; documented by
    # app/api/v1/phone.py + tests/unit/api/test_v17_phone_send.py.
    ("/api/v1/phone/send", ("POST",)),
    ("/api/v1/phone/summary", ("GET",)),
    ("/api/v1/ops/sandbox", ("GET",)),                           # V11 unit 2: read-only SANDBOX_MODE flag behind the UI banner; OLD surface had no sandbox route — NEW pin added deliberately with the route (parent-surgical)
    ("/api/v1/outreach/prospects", ("GET",)),
    # wave V11 (W1, audit B9/B10): the PUBLIC one-click opt-out a compliant footer links to —
    # unauthenticated BY DESIGN (a recipient has no owner key), which is why the outreach router's
    # auth moved to per-route (the finance/webhook precedent). NEW pin added with the route.
    ("/api/v1/outreach/unsubscribe", ("GET",)),
    # wave V9 (B2, V9-D3): the DATED pricing-catalog snapshot read (provenance on the payload).
    # OLD surface had no pricing route — NEW pin added deliberately with the route. Owner-key.
    ("/api/v1/pricing", ("GET",)),
    # wave U5 (P4/B8): the usage-ledger read + export — OLD surface had no runs route; NEW pins
    # added deliberately (count 39 -> 41): GET /runs (filters, {items,total}) and GET /runs/export
    # (csv/json; row count == total). Owner-key.
    ("/api/v1/runs", ("GET",)),
    ("/api/v1/runs/export", ("GET",)),
    # wave V8 (C2): the owner's scheduled task prompts — GET/POST the collection, PATCH/DELETE the
    # item (`/schedules/{schedule_id}`); pause = PATCH enabled=false. OLD surface had no schedules
    # routes; NEW pins added deliberately (count 44 -> 48). Owner-key.
    ("/api/v1/schedules", ("GET",)),
    ("/api/v1/schedules", ("POST",)),
    ("/api/v1/schedules/{schedule_id}", ("DELETE",)),
    ("/api/v1/schedules/{schedule_id}", ("PATCH",)),
    # wave V9 (B2, V9-D3; R5 #4): the static-site PACKAGE export — writes site-<slug>/ under
    # backend/data/exports and returns the paths (NO hosting, no promises). OLD surface had no
    # site route; NEW pin added deliberately with the route (count 48 -> 52 total this wave for
    # B2's four). Owner-key.
    ("/api/v1/site/package", ("POST",)),
    # wave V12 (W-C): the site-package DOWNLOAD — the built package as a ZIP from the exports
    # root (read-only; exports-root-guarded, traversal-refusing, named 404 for an unbuilt slug).
    # OLD surface had no download route; NEW pin added deliberately with the route. Documented
    # /api/v1 count chain 64 -> 65; EXPECTED_ROUTES total (65 + `/` + `/ws/activity`) 66 -> 67.
    # (V12/F2 correction: W-C's first comment chain said "62 -> 63 / total 64 -> 65" — stale
    # against the live tree; re-measured keys: 64 -> 65 api/v1, 66 -> 67 total.)
    ("/api/v1/site/package/download", ("GET",)),
    ("/api/v1/social/posts", ("GET",)),
    ("/api/v1/support/inbox", ("GET",)),                        # R17: the support inbox list (owner)
    ("/api/v1/support/inbox", ("POST",)),                       # R17: the support inbox submit (owner)
    ("/api/v1/tasks", ("GET",)),
    ("/api/v1/tasks", ("POST",)),
    ("/api/v1/tasks/{task_id}", ("GET",)),
    ("/api/v1/tasks/{task_id}/status", ("PATCH",)),               # T12/D20 (build/decisions/DECISIONS.md): the typed status write TaskStatusUpdate documents — OLD surface had no status route; NEW pin added deliberately with the route
    # wave V9 settle (V9-D3, parent): the planner / invoices / economics routers, wired by the
    # parent commit after their writers settled — each documented by its module + wave tests.
    ("/api/v1/economics/context", ("GET",)),
    ("/api/v1/invoices", ("GET",)),
    ("/api/v1/invoices", ("POST",)),
    ("/api/v1/invoices/{invoice_id}", ("GET",)),
    ("/api/v1/invoices/{invoice_id}/send", ("POST",)),
    ("/api/v1/invoices/{invoice_id}/void", ("POST",)),
    ("/api/v1/planner/confirm", ("POST",)),
    ("/api/v1/planner/propose", ("POST",)),
    ("/api/v1/planner/status", ("GET",)),
    # wave V10 (H1/D7): the data portability surface — GET /data/export (ZIP: MANIFEST + the 8
    # members) and DELETE /data (dry-run counts; the single-use-token wipe).  OLD surface had no
    # data routes; NEW pins added deliberately WITH the routes.  Documented /api/v1 count
    # 60 -> 62; EXPECTED_ROUTES total (the 62 above + `/` + `/ws/activity`) 62 -> 64.
    ("/api/v1/data", ("DELETE",)),
    ("/api/v1/data/export", ("GET",)),
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
