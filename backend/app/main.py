"""FastAPI application wiring (uvicorn app.main:app)."""
from __future__ import annotations

import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.v1 import (
    ads,
    agents,
    approvals,
    auto_mode,
    channels,
    chat,
    config,
    dashboard,
    economics,
    finance,
    health,
    invoices,
    memory,
    notifications,
    ops,
    outreach,
    planner,
    pricing,
    schedules,
    site,
    social,
    support,
    tasks,
    ws,
)
from app.api.v1.runs import (
    router as runs_router,  # wave U5 (P4): the agent_runs ledger read + export
)
from app.config import settings
from app.core.database import dispose_engine
from app.core.errors import E_AUTH, ERROR_HINTS, PolsiaError, http_status_for, to_http_detail
from app.core.redis_client import close_redis


class _JsonFormatter(logging.Formatter):
    """One JSON object per log record — stdlib only (python-json-logger is not a dependency).

    WHY: `settings.log_json` (LOG_JSON) was declared and dead — `.env.example` carried the knob
    with a note that nothing read it.  Fields: ts (the record's own time, UTC), level, logger,
    message (already %-formatted), plus `exception` when a traceback is attached.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def _configure_logging() -> None:
    """Root logging: plain text by default, one JSON object per record under LOG_JSON.

    The `log_json=False` branch is the exact `basicConfig(level=…)` call this module always ran,
    so text-mode logging is byte-identical.  uvicorn configures its OWN loggers
    (uvicorn.error/uvicorn.access) with its own handlers, so this shapes the application's root
    logger either way; the JSON branch is for deployments that ship app logs to a collector.
    """
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    if not settings.log_json:
        logging.basicConfig(level=level)
        return
    handler = logging.StreamHandler()
    handler.setFormatter(_JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler])


_configure_logging()
logger = logging.getLogger(__name__)


def _refusal_headers(exc: PolsiaError) -> dict[str, str]:
    """Extra headers a named refusal must carry — the SAME set wherever it was raised.

    `X-Polsia-Error-Code` is the machine-readable form of the code the body already names.  The
    in-route `refusal_guard` path has always sent it (`deps.as_http_exception`), and a client must
    not see a different response for the same refusal just because it was raised a layer earlier:
    before this header was added here, a pre-route refusal (a dependency, e.g. a failing `get_db`)
    lost it while the identical in-route refusal kept it (R27 §M2, measured in
    build/R27-social-outreach/raw/probe_edb_paths.txt; closed by lane R29).

    Auth is the one refusal with a protocol-level challenge: an E_AUTH response keeps the
    `WWW-Authenticate: X-API-Key` header the previous bare-HTTPException path sent, so a client that
    looks for it is not silently worse off for the refusal having become nameable.
    """
    headers = {"X-Polsia-Error-Code": exc.code}
    if exc.code == E_AUTH:
        headers["WWW-Authenticate"] = "X-API-Key"
    return headers


def coded_detail_envelope(detail: Any) -> dict[str, Any] | None:
    """Wrap a CODED `HTTPException.detail` into the standard refusal envelope (D5).

    At HEAD there were two wire shapes for one refusal: the envelope
    `{"ok": false, "error": {code, message, how_to_fix, context}}` that `to_http_detail` produces
    (emitted by the `PolsiaError` handler below and by the request middleware), and
    `{"detail": {...}}` — what FastAPI's default handler serializes for the coded dicts raised by
    `refusal_guard()` (deps.py:95-125) and by the direct `HTTPException` raises in
    tasks.py:52-77.  The frontend's `isRefusal` parses the envelope only, so those raise-paths
    surfaced as `E_HTTP_<status>` with a raw JSON message even though the refusal was fully named.

    Returns the envelope for a detail dict carrying a non-empty `code`, and `None` for EVERYTHING
    else — a plain string, a validation array, an incidental `HTTPException` — which the caller
    hands back to FastAPI's own handler, unchanged.  Only the coded case is ours to wrap; the
    `how_to_fix` fallback mirrors `to_http_detail` (an empty hint falls back to the code's
    `ERROR_HINTS`, never to "").
    """
    if not isinstance(detail, dict):
        return None
    code = detail.get("code")
    if not isinstance(code, str) or not code:
        return None
    context = detail.get("context")
    return {
        "ok": False,
        "error": {
            "code": code,
            "message": detail.get("message") or "",
            "how_to_fix": detail.get("how_to_fix") or ERROR_HINTS.get(code, ""),
            "context": context if isinstance(context, dict) else {},
        },
    }


@asynccontextmanager
async def lifespan(application: FastAPI):
    logger.info(
        "starting %s (env=%s, sandbox=%s, mock_cli=%s)",
        settings.app_name,
        settings.environment,
        settings.sandbox_mode,
        settings.claude_cli_mock,
    )
    if settings.api_key in ("", "dev-key", "change-me"):
        # Loud on purpose: the dashboard sends this key, so a default value means the API is open.
        logger.warning(
            "API_KEY is the default/blank value — set API_KEY in .env before exposing this service"
        )
    if settings.sandbox_mode and not settings.claude_cli_mock:
        logger.info("sandbox mode is ON: real posts/emails/ads/payments are refused, drafts are recorded")
    try:
        yield
    finally:
        # A clean shutdown releases the connection pool instead of leaving sockets to the OS.
        await dispose_engine()
        # ...and the Redis client, for the same reason: `close_redis()` closes the shared async
        # client's connection pool when one was ever created, and is a no-op when none was (the
        # client is created lazily).  Without it a shutdown leaves that socket to the OS — the same
        # class of leak the engine disposal above exists to prevent (RD4 item 5; audit D4 batch).
        await close_redis()
        logger.info("shutting down %s (db engine disposed, redis closed)", settings.app_name)


def create_app() -> FastAPI:
    application = FastAPI(
        title="Polsia API",
        description="Autonomous multi-agent AI business platform",
        version="1.0.0",
        lifespan=lifespan,
        debug=settings.debug,  # R22: the setting was declared and dead; default False = unchanged
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        # L1-08: the custom headers this app emits must be READABLE by a cross-origin browser
        # client.  Without `expose_headers` only CORS-safelisted response headers are readable
        # from JS, so the shipped error page's advice ("the backend log for this request
        # (X-Request-ID) has the server side") and the machine-readable refusal code were
        # invisible on exactly the split-origin client the default `cors_origins`
        # (http://localhost:3000) describes — same-origin nginx deployments never needed them.
        # V7-T07: X-Polsia-Row-Count (runs export, app/api/v1/runs.py) joins the list so a
        # cross-origin client reads the declared total under the same contract as the rest.
        expose_headers=[
            "X-Polsia-Error-Code",
            "X-Request-ID",
            "X-Response-Time-ms",
            "X-Polsia-Row-Count",
        ],
    )

    @application.middleware("http")
    async def correlation_and_timing(request: Request, call_next):
        """Attach a request id (honouring an inbound one) and log one line per request.

        Without this an operator cannot tie an activity-log row to the HTTP call that caused it.
        """
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except PolsiaError as exc:  # raised before a route could convert it
            logger.warning("request %s failed: %s", request_id, exc)
            return JSONResponse(
                status_code=http_status_for(exc.code), content=to_http_detail(exc),
                headers={"X-Request-ID": request_id, **_refusal_headers(exc)},
            )
        elapsed_ms = (time.perf_counter() - started) * 1000
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Response-Time-ms"] = f"{elapsed_ms:.1f}"
        if elapsed_ms > 2000:
            logger.warning(
                "slow request %s %s %s -> %s in %.0fms",
                request_id, request.method, request.url.path, response.status_code, elapsed_ms,
            )
        return response

    @application.exception_handler(PolsiaError)
    async def polsia_error_handler(request: Request, exc: PolsiaError) -> JSONResponse:
        """Every named failure reaches the client as a named refusal, never as a bare 500."""
        logger.warning("%s on %s %s: %s", exc.code, request.method, request.url.path, exc.message)
        return JSONResponse(
            status_code=http_status_for(exc.code),
            content=to_http_detail(exc),
            headers=_refusal_headers(exc),
        )

    @application.exception_handler(StarletteHTTPException)
    async def coded_http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> Response:
        """D5: a CODED `HTTPException` reaches the client in the same envelope as a named refusal.

        Registered for Starlette's `HTTPException` (FastAPI's subclasses it, and so does every
        route raise) so the raise-based refusals in tasks.py/config.py are covered too.  The
        UNCODED case goes straight back to FastAPI's own handler — the default response for a 404
        route miss, a plain `detail` string or a validation array is byte-identical to before, and
        the `PolsiaError` path above keeps its own handler (no double-wrapping: a `PolsiaError`
        never arrives here).
        """
        envelope = coded_detail_envelope(exc.detail)
        if envelope is None:
            return await http_exception_handler(request, exc)
        logger.warning(
            "%s on %s %s: %s", envelope["error"]["code"], request.method, request.url.path,
            envelope["error"]["message"],
        )
        # L1-04: the header `_refusal_headers` attaches for a `PolsiaError` must travel on this
        # path too.  The coded raises in tasks.py/config.py carry no `headers=` of their own, so
        # before this line the same refusal answered WITH the machine-readable code header when
        # it passed through `refusal_guard` and WITHOUT it when it was raised one layer earlier
        # — the exact split `_refusal_headers` exists to make impossible.  `setdefault` keeps an
        # explicit header (the in-route `refusal_guard` shape sets it via `as_http_exception`)
        # untouched, so every coded refusal carries exactly one value: the body's code.
        headers = dict(exc.headers or {})
        headers.setdefault("X-Polsia-Error-Code", envelope["error"]["code"])
        return JSONResponse(status_code=exc.status_code, content=envelope, headers=headers)

    api_v1 = APIRouter(prefix="/api/v1")
    api_v1.include_router(health.router)     # no auth
    api_v1.include_router(tasks.router)
    api_v1.include_router(agents.router)
    api_v1.include_router(chat.router)       # wave V8 (C1): the owner <-> Polsia chat (message/confirm/history)
    api_v1.include_router(config.router)
    api_v1.include_router(dashboard.router)
    api_v1.include_router(memory.router)
    api_v1.include_router(notifications.router)  # wave U2: the bell's task-derived feed (derived, no table)
    api_v1.include_router(finance.router)    # /stripe/webhook is unauthenticated
    api_v1.include_router(social.router)
    api_v1.include_router(outreach.router)
    api_v1.include_router(ads.router)
    api_v1.include_router(support.router)   # R17: the customer-support inbox ingest (owner's API)
    api_v1.include_router(ops.router)       # R22: activity publish breaker stats + reset (owner)
    api_v1.include_router(approvals.router)  # T02/D1: the approval-token mint (owner)
    api_v1.include_router(channels.router)   # wave U3 (H-01): per-channel send counters + caps (owner read)
    api_v1.include_router(auto_mode.router)  # wave U5 (P2/B22): the bounded Auto Mode window (start/status/stop, owner)
    api_v1.include_router(runs_router)       # wave U5 (P4/B8): the agent_runs usage ledger read + export (owner)
    api_v1.include_router(schedules.router)  # wave V8 (C2): the owner's scheduled task prompts (CRUD; pause = PATCH enabled=false)
    # wave V9 (B2, V9-D3): the money surfaces this seat owns — the pricing-catalog read, the
    # finance planning view, the agent-quality read (ops) and the static-site package export.
    api_v1.include_router(pricing.router)    # V9: the 50-row pricing catalog read (owner)
    api_v1.include_router(site.router)       # V9 (R5 #4): site PACKAGE export — no hosting, no promises (owner)
    # wave V9 settle (V9-D3): the remaining writers' routers, wired here in ONE parent commit with
    # their parity-pin moves — parallel-write timing makes a per-seat main.py owner nondeterministic.
    api_v1.include_router(planner.router)    # V9 (B1): the idea planner — propose/confirm/status (owner)
    api_v1.include_router(invoices.router)   # V9 (B4): invoices v1 — create/list/get/send/void (owner)
    api_v1.include_router(economics.router)  # V9 (B7): keyless macro context — inflation/CPI/FX (owner)
    application.include_router(api_v1)
    application.include_router(ws.router)

    @application.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        return {"name": settings.app_name, "status": "ok", "health": "/api/v1/health", "docs": "/docs"}

    return application


app = create_app()
