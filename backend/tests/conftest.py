"""Root conftest — shared fixtures for all unit tests."""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# ─── Claude CLI mock (autouse — always active in unit tests) ──────────────────

@pytest.fixture(autouse=True)
def mock_claude_cli(monkeypatch):
    """Prevent any real claude CLI calls in unit tests."""
    monkeypatch.setenv("CLAUDE_CLI_MOCK", "true")
    monkeypatch.setenv(
        "CLAUDE_CLI_MOCK_RESPONSE",
        json.dumps({"result": "Mock Claude response for testing"}),
    )


# ─── Events circuit breaker reset (autouse) ───────────────────────────────────

@pytest.fixture(autouse=True)
def reset_publish_breaker_state():
    """The events breaker + counters are PROCESS-GLOBAL module state (app/core/events.py).

    Any test that lets a publish fail for real (the worker path with no Redis running:
    agent_tasks -> log_activity -> publish_activity -> connection refused) opens the breaker for
    its cooldown window, and every later publish in the same pytest process is SKIPPED — an
    order-dependent failure measured when the R33 e2e's real-worker tests made the redteam spec
    invariant see 0 publishes instead of 1. Reset around every test so no test can poison another
    (the same leak class fixed for run-taint in RD11)."""
    from app.core import events

    events.reset_publish_breaker()
    yield
    events.reset_publish_breaker()


# ─── In-memory SQLite database ────────────────────────────────────────────────

@pytest_asyncio.fixture
async def async_db_session():
    """SQLite in-memory async session — no Postgres required for unit tests."""
    # `app.models` is imported for its REGISTRATION side effect: `Base.metadata` only knows a table once
    # the module declaring it has been imported. Taking `Base` alone and calling `create_all` on it creates
    # ZERO tables when nothing has imported the models yet, and the first query then dies with
    # `no such table: tasks` — a fixture-ordering artefact that was recorded here as an unexplained puzzle
    # and reproduced three ways before being closed by this import.
    import app.models  # noqa: F401  (registers every table on Base.metadata)
    from app.core.database import Base

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        # SQLite doesn't support ARRAY — create tables via raw SQL for models that use it
        await conn.run_sync(Base.metadata.create_all)

    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as session:
        yield session

    await engine.dispose()


# ─── Mock Redis ───────────────────────────────────────────────────────────────

@pytest.fixture
def mock_redis(mocker):
    mock = AsyncMock()
    mock.publish = AsyncMock(return_value=1)
    mocker.patch("app.core.redis_client.get_redis", return_value=mock)
    mocker.patch("app.core.events.get_redis", return_value=mock)
    return mock


# ─── Mock ChromaDB ────────────────────────────────────────────────────────────

@pytest.fixture
def mock_chroma(mocker):
    collection = MagicMock()
    collection.add = MagicMock()
    collection.query = MagicMock(return_value={
        "documents": [["Mock memory content"]],
        "metadatas": [[{"category": "strategy", "title": "Test", "source": "test"}]],
        "ids": [["mock-id-1"]],
        "distances": [[0.1]],
    })
    mocker.patch("app.core.chroma_client.get_collection", return_value=collection)
    mocker.patch("app.services.memory_service.get_collection", return_value=collection)
    return collection


# ─── FastAPI test client ──────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def api_client(async_db_session, mock_redis):
    from app.config import settings
    from app.core.database import get_db
    from app.main import app

    # Override DB dependency
    app.dependency_overrides[get_db] = lambda: async_db_session
    # Override API key check
    settings.api_key = "test-key"

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()


# ─── Auth header helper ───────────────────────────────────────────────────────

@pytest.fixture
def auth_headers():
    return {"X-API-Key": "test-key"}
