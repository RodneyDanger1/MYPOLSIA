"""FastAPI application wiring (uvicorn app.main:app)."""
from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1 import ads, agents, config, dashboard, finance, health, memory, outreach, social, tasks, ws
from app.config import settings
from app.core.database import dispose_engine
from app.core.errors import PolsiaError, to_http_detail, http_status_for

logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
logger = logging.getLogger(__name__)


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
        logger.info("shutting down %s (db engine disposed)", settings.app_name)


def create_app() -> FastAPI:
    application = FastAPI(
        title="Polsia API",
        description="Autonomous multi-agent AI business platform",
        version="1.0.0",
        lifespan=lifespan,
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
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
                headers={"X-Request-ID": request_id},
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
        )

    api_v1 = APIRouter(prefix="/api/v1")
    api_v1.include_router(health.router)     # no auth
    api_v1.include_router(tasks.router)
    api_v1.include_router(agents.router)
    api_v1.include_router(config.router)
    api_v1.include_router(dashboard.router)
    api_v1.include_router(memory.router)
    api_v1.include_router(finance.router)    # /stripe/webhook is unauthenticated
    api_v1.include_router(social.router)
    api_v1.include_router(outreach.router)
    api_v1.include_router(ads.router)
    application.include_router(api_v1)
    application.include_router(ws.router)

    @application.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        return {"name": settings.app_name, "status": "ok", "health": "/api/v1/health", "docs": "/docs"}

    return application


app = create_app()
