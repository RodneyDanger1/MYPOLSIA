"""V28/L3 — the wiring audit as a test: no route, tool, beat entry or setting may be orphaned.

Four surfaces, four guards (plus two cross-checks).  Each guard FAILS on an orphan instead of
recording it in prose, so the audit cannot rot:

1. **Routes** — every operation in `app.openapi()` is referenced by shipped UI code, OR listed in
   `API_ONLY` with the reason a browser never calls it.  The list is exact: an entry that no longer
   exists (or a route that gained a consumer) fails as STALE, so the documentation cannot drift.
   The activity WebSocket is not in OpenAPI (FastAPI omits WS routes) and is pinned separately.
2. **UI → route** — every multi-segment API path a page or hook builds resolves to a real route,
   so a renamed endpoint cannot linger as a dead call in the UI.  Single-segment literals are page
   links (`/tasks` is both a page and the API path) and are not judged.
3. **Tools** — `TOOL_SPECS`, `_RUNNERS` and `TOOL_GUIDANCE` name the same seventeen tools, and the
   policy firewall's union of `agent_tools` is exactly that set: no advertised-but-unrunnable tool,
   no allowlisted-but-nonexistent tool, and every tool has an owner that production can start.
4. **Beat** — every `beat_schedule` entry names a task the worker registers, carries a schedule,
   and targets a queue the app DECLARES (`task_queues`), so a tick cannot land in a queue nobody
   consumes.  Also pins that the schedule the app actually loads IS `beat_schedule` (the D-14
   "declared 5 / effective 0" defect), and that every `task_routes` target is declared too.
5. **Settings** — every `Settings` field is read somewhere in product code (`app/`, `celery_app/`,
   `scripts/`) through `settings.<name>`, `getattr(settings, "<name>")` or `_setting("<name>")`.
   A read through a COMPUTED attribute name must be declared in `DYNAMIC_READERS` with its reason,
   which is what makes an otherwise invisible wiring explicit.

Paths are resolved from this file (`backend/tests/unit/…`), never from the process CWD.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
FRONTEND = BACKEND.parent / "frontend"
UI_SRC = FRONTEND / "src"
API_PREFIX = "/api/v1"

#: Routes with NO UI consumer, each with the reason a browser never calls it.  Keyed
#: (METHOD, openapi path).  Keep this exact: `test_every_route_has_a_ui_consumer_or_a_documented_reason`
#: fails on a stale entry as well as on an undocumented route.
API_ONLY: dict[tuple[str, str], str] = {
    ("POST", "/api/v1/finance/stripe/webhook"): (
        "Stripe -> us. The provider posts signed events here; no UI initiates or reads it. "
        "Signature-verified in app/api/v1/finance.py; the resulting state is read by the UI "
        "through /finance/summary and /dashboard/activity."
    ),
    ("GET", "/api/v1/invoices/{invoice_id}"): (
        "API/e2e client read. The invoices page renders whole rows (items included) from the "
        "list endpoint, so it never needs a detail fetch; e2e/invoices.spec.ts:82 asserts the "
        "route for API clients."
    ),
    ("GET", "/api/v1/outreach/unsubscribe"): (
        "Recipient-facing HTML page behind the one-click unsubscribe link in outgoing email "
        "(signed token, no login). It is consumed by a mail client's browser, not by the SPA."
    ),
    ("GET", "/api/v1/trust/link"): (
        "Agent tool `link_status` only. A model checks a cited URL before sending it; no page "
        "renders a link-health card, so no browser call exists by design."
    ),
}

#: Settings read through a COMPUTED attribute name (invisible to a literal scan).  Empty today:
#: deploy_service reads its two fields by literal name.  Any future entry must carry a reason.
DYNAMIC_READERS: dict[str, str] = {}

#: Agent types that production starts WITHOUT `crew_factory.AGENT_MAP` — a service instantiates
#: them directly, so their capabilities/tools are still reachable.
SERVICE_INVOKED_AGENTS = {"company_planner": "app/services/planner_service.py:373"}


# ── route + UI extraction ────────────────────────────────────────────────────────────────────
def _norm(path: str) -> str:
    """Normalise a path for comparison: drop query/fragment, collapse every param to `{}`."""
    path = path.split("?", 1)[0].split("#", 1)[0]
    path = re.sub(r"\$\{[^}]*\}", "{}", path)
    path = re.sub(r"\{[^}]*\}", "{}", path)
    path = re.sub(r"/+", "/", path).rstrip("/")
    return path or "/"


@lru_cache(maxsize=1)
def _routes() -> dict[str, frozenset[str]]:
    from app.main import app

    return {
        path: frozenset(method.upper() for method in operations)
        for path, operations in app.openapi()["paths"].items()
    }


def _is_comment_line(text: str, offset: int) -> bool:
    """True when `offset` sits on a comment line — a route named in prose is not a consumer."""
    start = text.rfind("\n", 0, offset) + 1
    return text[start:offset].lstrip().startswith(("*", "//", "/*"))


@lru_cache(maxsize=1)
def _ui_paths() -> dict[str, frozenset[str]]:
    """`{normalized API path: {files}}` for every path literal in shipped UI code.

    Two shapes exist in the frontend: absolute (`${API_URL}/api/v1/...`, raw `fetch`) and
    relative (`api.get("/tasks")`, `apiFetch` prefixes `/api/v1`).  Both are read from the
    source text, comments excluded; `__tests__` is excluded because a test double is not a UI
    consumer.
    """
    assert UI_SRC.is_dir(), (
        f"cannot audit UI wiring: {UI_SRC} is missing. The route leg needs the frontend source "
        "tree (CI checks out the whole repo, so this only fires in a backend-only tree)."
    )
    segments = sorted(
        {
            path.split("/")[3]
            for path in _routes()
            if path.count("/") >= 3 and path.startswith(API_PREFIX + "/")
        },
        key=len,
        reverse=True,
    )
    assert segments, "no API route segments were discovered from app.openapi()"
    alternation = "|".join(re.escape(segment) for segment in segments)
    absolute = re.compile(r"/api/v1/[A-Za-z0-9_\-/{}.$]*")
    # Concatenated rather than %-formatted: the pattern itself is full of braces and escapes.
    relative = re.compile(
        r"""["'`](/(?:"""
        + alternation
        + r""")(?:/[A-Za-z0-9_\-/{}.$]*)?|/(?:"""
        + alternation
        + r""")(?=["'`]))"""
    )
    websocket = re.compile(r"/ws/[A-Za-z0-9_\-]+")

    found: dict[str, set[str]] = {}
    for source in sorted(UI_SRC.rglob("*.ts")) + sorted(UI_SRC.rglob("*.tsx")):
        if "__tests__" in source.parts or source.name.endswith((".test.ts", ".test.tsx")):
            continue
        text = source.read_text(encoding="utf-8", errors="replace")
        relative_name = source.relative_to(FRONTEND).as_posix()
        for pattern, is_absolute in ((absolute, True), (relative, False), (websocket, True)):
            for match in pattern.finditer(text):
                if _is_comment_line(text, match.start()):
                    continue
                value = match.group(0 if is_absolute else 1)
                if value.startswith(API_PREFIX):
                    value = value[len(API_PREFIX):] or "/"
                found.setdefault(_norm(value), set()).add(relative_name)
    return {path: frozenset(files) for path, files in found.items()}


# ── 1. every route is consumed or documented ─────────────────────────────────────────────────
def test_every_route_has_a_ui_consumer_or_a_documented_reason() -> None:
    routes = _routes()
    ui = _ui_paths()

    undocumented: list[str] = []
    for path, methods in sorted(routes.items()):
        key = _norm(path[len(API_PREFIX):] if path.startswith(API_PREFIX) else path)
        for method in sorted(methods):
            if key in ui:
                continue
            if (method, path) in API_ONLY:
                continue
            undocumented.append(f"{method} {path}")

    stale = [
        f"{method} {path}"
        for (method, path) in sorted(API_ONLY)
        if path not in routes or method not in routes[path]
    ]

    assert not stale, f"API_ONLY entries no longer match a route: {stale}"
    assert not undocumented, (
        "routes with no UI consumer and no documented API-only reason: "
        + "; ".join(undocumented)
    )


def test_the_activity_websocket_has_a_ui_consumer() -> None:
    """FastAPI's OpenAPI omits WebSocket routes, so the activity feed is pinned by hand."""
    from app.api.v1 import ws

    route = next((r for r in ws.router.routes if getattr(r, "path", "") == "/ws/activity"), None)
    assert route is not None, "the activity WebSocket route is no longer mounted"
    consumers = _ui_paths().get("/ws/activity", frozenset())
    assert consumers, "no shipped UI code connects to /ws/activity"


# ── 2. every UI API path resolves to a real route ────────────────────────────────────────────
def test_every_ui_api_path_resolves_to_a_route() -> None:
    routes = {_norm(path[len(API_PREFIX):]) for path in _routes()}
    orphans: list[str] = []
    for path, files in sorted(_ui_paths().items()):
        if path.startswith("/ws/") or path.count("/") < 2:
            continue  # WS handled above; single-segment literals are page links
        if path in routes:
            continue
        orphans.append(f"{path} (in {', '.join(sorted(files)[:2])})")
    assert not orphans, "UI code calls API paths that no route serves: " + "; ".join(orphans)


_UI_CALL = re.compile(
    r"""\bapi\s*\.\s*(?P<method>get|post|put|patch|delete)\s*"""
    r"""(?:<[^<>()]*(?:<[^<>()]*>[^<>()]*)*>)?\s*\(\s*(?P<q>["'`])(?P<path>[^"'`]+)(?P=q)"""
)


def test_ui_methods_match_the_route() -> None:
    """Where the method is statically visible, the UI must use one the route serves.

    A path through a helper (`useFinanceRead("/finance/summary")`) carries no method at the
    literal, so those calls are covered by the route-existence guard only — the doc says so.
    """
    routes = _routes()
    by_key: dict[str, set[str]] = {}
    for path, methods in routes.items():
        by_key.setdefault(_norm(path[len(API_PREFIX):]), set()).update(methods)

    mismatches: list[str] = []
    checked = 0
    for source in sorted(UI_SRC.rglob("*.ts")) + sorted(UI_SRC.rglob("*.tsx")):
        if "__tests__" in source.parts or source.name.endswith((".test.ts", ".test.tsx")):
            continue
        text = source.read_text(encoding="utf-8", errors="replace")
        for match in _UI_CALL.finditer(text):
            if _is_comment_line(text, match.start()):
                continue
            key = _norm(match.group("path"))
            methods = by_key.get(key)
            if not methods:
                continue  # caught by test_every_ui_api_path_resolves_to_a_route
            checked += 1
            method = match.group("method").upper()
            if method not in methods:
                mismatches.append(
                    f"{method} {key} in {source.relative_to(FRONTEND).as_posix()}:{text.count(chr(10), 0, match.start()) + 1} "
                    f"(route serves {sorted(methods)})"
                )
    assert checked > 0, "no method-visible UI calls were found — the extraction is broken"
    assert not mismatches, "UI calls the wrong HTTP method: " + "; ".join(mismatches)


# ── 3. tools ─────────────────────────────────────────────────────────────────────────────────
def test_every_registry_tool_is_allowlisted_and_executable() -> None:
    from app.agents import tool_registry
    from app.agents.crew_factory import AGENT_MAP
    from app.agents.policy_firewall import AGENT_CAPABILITIES

    spec_names = {spec["function"]["name"] for spec in tool_registry.TOOL_SPECS}
    runner_names = set(tool_registry._RUNNERS)
    guidance_names = set(tool_registry.TOOL_GUIDANCE)

    assert spec_names == runner_names, (
        f"tools advertised without a runner: {sorted(spec_names - runner_names)}; "
        f"runners without a spec: {sorted(runner_names - spec_names)}"
    )
    assert spec_names == guidance_names, (
        f"tools without guidance: {sorted(spec_names - guidance_names)}; "
        f"guidance without a tool: {sorted(guidance_names - spec_names)}"
    )

    allowed = {
        tool
        for capability in AGENT_CAPABILITIES.values()
        for tool in capability.agent_tools
    }
    assert spec_names == allowed, (
        f"tools no agent may call: {sorted(spec_names - allowed)}; "
        f"allowlisted names that are not tools: {sorted(allowed - spec_names)}"
    )

    dispatchable = set(AGENT_MAP) | set(SERVICE_INVOKED_AGENTS)
    missing_capability = sorted(set(AGENT_MAP) - set(AGENT_CAPABILITIES))
    assert not missing_capability, (
        f"dispatchable agents with no capability (their tool calls would be denied as unknown): "
        f"{missing_capability}"
    )
    ownerless = sorted(
        name
        for name in spec_names
        if not any(
            name in AGENT_CAPABILITIES[agent].agent_tools
            for agent in dispatchable
            if agent in AGENT_CAPABILITIES
        )
    )
    assert not ownerless, f"tools every production path would refuse: {ownerless}"


# ── 4. beat ──────────────────────────────────────────────────────────────────────────────────
def test_every_beat_entry_is_registered_and_its_queue_is_consumed() -> None:
    import celery_app.worker as worker
    from celery_app import celery_config
    from celery_app.beat_schedule import beat_schedule

    registered = {name for name in worker.app.tasks if not name.startswith("celery.")}
    declared_queues = {queue.name for queue in celery_config.task_queues}

    problems: list[str] = []
    for entry_name, entry in sorted(beat_schedule.items()):
        task = entry.get("task")
        if task not in registered:
            problems.append(f"{entry_name}: task {task!r} is not registered by the worker")
        if "schedule" not in entry:
            problems.append(f"{entry_name}: no schedule")
        queue = (entry.get("options") or {}).get("queue")
        if queue not in declared_queues:
            problems.append(f"{entry_name}: queue {queue!r} is not declared in task_queues")

    assert not problems, "beat wiring gaps: " + "; ".join(problems)
    assert worker.app.conf.beat_schedule == beat_schedule, (
        "the Celery app does not load celery_app.beat_schedule — declared but not effective"
    )
    undeclared_routes = sorted(
        f"{pattern} -> {route['queue']}"
        for pattern, route in celery_config.task_routes.items()
        if route["queue"] not in declared_queues
    )
    assert not undeclared_routes, f"task_routes target undeclared queues: {undeclared_routes}"


# ── 5. settings ──────────────────────────────────────────────────────────────────────────────
@lru_cache(maxsize=1)
def _product_source() -> tuple[Path, ...]:
    files: list[Path] = []
    for root in (BACKEND / "app", BACKEND / "celery_app", BACKEND / "scripts"):
        if root.is_dir():
            files.extend(sorted(root.rglob("*.py")))
    return tuple(files)


def _has_reader(field: str, text: str) -> bool:
    name = re.escape(field)
    return bool(
        re.search(rf"settings\.{name}\b", text)
        or re.search(rf"getattr\(\s*settings\s*,\s*[\"']{name}[\"']", text)
        or re.search(rf"_setting\(\s*[\"']{name}[\"']", text)
    )


def test_every_settings_field_has_a_reader() -> None:
    from app.config import Settings

    fields = list(Settings.model_fields)
    texts = [path.read_text(encoding="utf-8", errors="replace") for path in _product_source()]

    stale = sorted(name for name in DYNAMIC_READERS if name not in fields)
    assert not stale, f"DYNAMIC_READERS names fields that do not exist: {stale}"

    unread = sorted(
        field
        for field in fields
        if field not in DYNAMIC_READERS and not any(_has_reader(field, text) for text in texts)
    )
    assert not unread, (
        "Settings fields no product code reads (add the read, or declare it in DYNAMIC_READERS "
        f"with the reason): {unread}"
    )
