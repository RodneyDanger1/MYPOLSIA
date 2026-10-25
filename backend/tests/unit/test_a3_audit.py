"""Wave A3 / seat S4 — AUDIT + API HONESTY: the pins for F-47, F-01, A9-04, F-48, F-37.

RED-FIRST (OATH): every test below was written and RUN against the base tree (`a66a235`) before
the code it pins existed.  The measured red legs live in
`polsia-work/build/wave-a3/raw/S4/red-before.xml`; the green run in `green.xml`.  The five defects:

* **F-47** an audit `flush()` that fails leaves the REQUEST session needing a rollback, so
  `get_db`'s commit raises AFTER the SMS left — one real send, one bare 500, and the owner's retry
  sends a second SMS.  `test_a_failed_audit_write_cannot_500_the_send` reproduces it with a store
  that physically has no `activity_log` table (no monkeypatched internals: a REAL driver error).
* **F-47 (ordering)** the audit row must exist, COMMITTED, before its event is published, and it
  must live on its own connection so the caller's rollback cannot erase it
  (`test_the_refusal_audit_is_committed_before_it_is_published`).
* **F-01** an uncaught exception must reach the client as the app's coded envelope, WITH the CORS
  header a split-origin dashboard needs — not `500 text/plain` from outside `CORSMiddleware`.
* **A9-04** the side-effect path (integrations save/clear/test, phone send) must actually PERSIST
  its audit row, and publish only after the commit that made it durable.
* **F-48** the data export must stream each member into the archive instead of holding every
  member's serialised text at once, and the manifest must still declare the rows it wrote.
* **F-37 backend** `GET /api/v1/config/phone` answers `200 {"configured", "available"}` when the
  phone store is absent — never a 5xx, never a 404.

No product mock: the only replacements here are external side-effect boundaries (the phone send
driver subprocess, the integrations secret store) and fault injection at the storage layer.
"""
from __future__ import annotations

import json
import sqlite3
import zipfile
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, Literal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.api.v1 import data as data_module
from app.config import settings
from app.core import events
from app.models.report import ActivityLog
from app.services import activity_service, phone_link_service, phone_send_service

API_KEY = "test-key"
SEND_URL = "/api/v1/phone/send"


# ── the test seam ────────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolated_audit_seam():
    """Reset `activity_service.ISOLATED_SESSION_FACTORY` around every test.

    `getattr`/`hasattr` on purpose: on the base tree the name does not exist yet, and a fixture that
    raises AttributeError would hide which ASSERTION each red leg actually fails.
    """
    previous = getattr(activity_service, "ISOLATED_SESSION_FACTORY", None)
    if hasattr(activity_service, "ISOLATED_SESSION_FACTORY"):
        activity_service.ISOLATED_SESSION_FACTORY = None
    yield
    if hasattr(activity_service, "ISOLATED_SESSION_FACTORY"):
        activity_service.ISOLATED_SESSION_FACTORY = previous


async def _engine(*, drop: tuple[str, ...] = ()) -> AsyncEngine:
    """A private in-memory SQLite engine with every model table (minus `drop`)."""
    import app.models  # noqa: F401  — registers every table on Base.metadata
    from app.core.database import Base

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    tables = [table for name, table in Base.metadata.tables.items() if name not in drop]

    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables))
    return engine


def _session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def _client(
    session_factory: Callable[[], AsyncSession],
    *,
    raise_app_exceptions: bool = True,
) -> AsyncIterator[AsyncClient]:
    """An HTTP client whose `get_db` behaves EXACTLY like the shipped dependency.

    Commit on success, rollback on any exception — the whole point of F-47 is what that contract
    does to a session an audit write already poisoned.
    """
    from app.core.database import get_db
    from app.main import app

    async def _request_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    saved_overrides = dict(app.dependency_overrides)
    saved_key = settings.api_key
    app.dependency_overrides[get_db] = _request_session
    settings.api_key = API_KEY
    try:
        transport = ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(saved_overrides)
        settings.api_key = saved_key


async def _activity_rows(session_factory: Callable[[], AsyncSession], action: str) -> list[Any]:
    async with session_factory() as reader:
        rows = (
            (await reader.execute(select(ActivityLog).where(ActivityLog.action == action)))
            .scalars()
            .all()
        )
    return list(rows)


# ── F-47 — a failed audit write must not 500 a send that already happened ──────────────────────


async def test_a_failed_audit_write_cannot_500_the_send(monkeypatch):
    """The activity store is down; the SMS still goes out and the answer still says so.

    Fault injection is a REAL store failure, not a patched internal: the engine carries every table
    EXCEPT `activity_log`, so the audit insert raises the driver's own `no such table` error.  The
    production default (no `ISOLATED_SESSION_FACTORY`) is exactly this case: the audit session is
    derived from the caller's engine.
    """
    engine = await _engine(drop=("activity_log",))
    Session = _session_factory(engine)
    sends: list[tuple[Any, Any]] = []

    async def fake_send(to: Any, body: Any, **kwargs: Any) -> phone_send_service.SendResult:
        sends.append((to, body))
        return phone_send_service.SendResult(
            sent_at="2026-10-24T12:00:00+00:00", to_masked="***4477"
        )

    monkeypatch.setattr(phone_send_service, "send", fake_send)
    try:
        async with _client(Session, raise_app_exceptions=False) as client:
            resp = await client.post(
                SEND_URL,
                headers={"X-API-Key": API_KEY},
                json={"to": "4477", "body": "Running ten minutes late."},
            )
    finally:
        await engine.dispose()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["data"]["sent_at"] == "2026-10-24T12:00:00+00:00"
    assert body["audit"] == "unavailable", "the send happened; the answer must not invite a retry"
    assert len(sends) == 1, "exactly one real send"


async def test_the_refusal_audit_is_committed_before_it_is_published(monkeypatch, mock_redis):
    """The row lands on the audit store's OWN connection, and its event follows the commit.

    Two engines: the request's (A) and the audit store (B).  B is the isolated writer's factory, so
    a row in B is proof that the audit transaction was committed before the route raised — the
    caller's rollback cannot reach it.  The publisher RECORDS how many rows B already holds when it
    fires: the answer must be 1, never 0 (`log_activity` publishes after `flush()`, i.e. before any
    commit; a refused request then rolls that row away while the feed has already announced it).
    """
    request_engine = await _engine()
    audit_engine = await _engine()
    RequestSession = _session_factory(request_engine)
    AuditSession = _session_factory(audit_engine)
    observed: list[tuple[str, int]] = []
    publish = events.publish_activity

    async def recording_publish(payload: dict[str, Any]) -> Any:
        async with AuditSession() as reader:
            count = (
                await reader.execute(select(func.count()).select_from(ActivityLog))
            ).scalar_one()
        observed.append((str(payload.get("action")), int(count)))
        return await publish(payload)

    monkeypatch.setattr(events, "publish_activity", recording_publish)
    monkeypatch.setattr(settings, "sandbox_mode", True)
    activity_service.ISOLATED_SESSION_FACTORY = AuditSession
    try:
        async with _client(RequestSession) as client:
            resp = await client.post(
                SEND_URL,
                headers={"X-API-Key": API_KEY},
                json={"to": "4477", "body": "Running ten minutes late."},
            )
        rows = await _activity_rows(AuditSession, phone_send_service.AUDIT_ACTION)
    finally:
        await request_engine.dispose()
        await audit_engine.dispose()

    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "E_SANDBOX_BLOCKED"
    assert len(rows) == 1, "the refused attempt's audit row is committed on its own connection"
    assert "E_SANDBOX_BLOCKED" in (rows[0].summary or "")
    assert "Running ten minutes late." not in json.dumps(rows[0].detail or {})
    assert observed == [(phone_send_service.AUDIT_ACTION, 1)], (
        "the event must be published AFTER the commit that made the row durable"
    )
    assert mock_redis.publish.await_count == 1


# ── F-01 — an uncaught exception is the coded envelope, inside CORS ─────────────────────────────


async def test_an_uncaught_exception_is_the_coded_envelope_with_cors():
    """A route that raises `ValueError` must answer `E_INTERNAL`, not `500 text/plain`."""
    from starlette.routing import Route

    from app.main import app

    async def boom(request: Any) -> Any:
        raise ValueError("a3-planted failure: this route always raises")

    route = Route("/__a3_boom", boom)
    app.router.routes.append(route)
    engine = await _engine()
    Session = _session_factory(engine)
    try:
        async with _client(Session, raise_app_exceptions=False) as client:
            resp = await client.get(
                "/__a3_boom", headers={"Origin": "http://localhost:3000", "X-API-Key": API_KEY}
            )
    finally:
        app.router.routes.remove(route)
        await engine.dispose()

    assert resp.status_code == 500, resp.text
    assert resp.headers["content-type"].startswith("application/json"), (
        f"a bare 500 is text/plain: {resp.headers.get('content-type')!r}"
    )
    body = resp.json()
    assert body["ok"] is False
    error = body["error"]
    assert error["code"] == "E_INTERNAL"
    assert error["how_to_fix"], "a coded refusal always says how to fix it"
    request_id = error["context"]["request_id"]
    assert request_id, "the envelope names the request the server log can be searched for"
    assert resp.headers["X-Polsia-Error-Code"] == "E_INTERNAL"
    assert resp.headers["X-Request-ID"] == request_id, "one request id, not two"
    assert resp.headers["access-control-allow-origin"] == "http://localhost:3000", (
        "the 500 was produced outside CORSMiddleware, so a split-origin client cannot read it"
    )
    assert "Traceback" not in resp.text and 'File "' not in resp.text, "no stack trace on the wire"


# ── A9-04 — the side-effect path persists its row, and publishes after the commit ──────────────


async def test_a_failed_publish_after_the_commit_does_not_rewrite_the_answer(monkeypatch):
    """The row is committed, then the announce fails: the client still gets its coded 403.

    The publish is the LAST step of the isolated writer and it must not be able to turn a durable
    audit row — or a refusal that already happened — into a 500.  (The outage path is real:
    `events.publish_activity` can raise before its own try block, e.g. when the Redis client cannot
    be constructed or the payload cannot be serialized.)
    """
    request_engine = await _engine()
    audit_engine = await _engine()
    RequestSession = _session_factory(request_engine)
    AuditSession = _session_factory(audit_engine)

    async def exploding_publish(payload: dict[str, Any]) -> None:
        raise RuntimeError("a3-planted publish outage")

    monkeypatch.setattr(events, "publish_activity", exploding_publish)
    monkeypatch.setattr(settings, "sandbox_mode", True)
    activity_service.ISOLATED_SESSION_FACTORY = AuditSession
    try:
        async with _client(RequestSession) as client:
            resp = await client.post(
                SEND_URL,
                headers={"X-API-Key": API_KEY},
                json={"to": "4477", "body": "Running ten minutes late."},
            )
        rows = await _activity_rows(AuditSession, phone_send_service.AUDIT_ACTION)
    finally:
        await request_engine.dispose()
        await audit_engine.dispose()

    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "E_SANDBOX_BLOCKED"
    assert len(rows) == 1, "the committed row survives a failed announce"


async def test_the_integration_side_effect_path_persists_a_real_audit_row(monkeypatch, mock_redis):
    """`PUT /integrations/{provider}` (a refused save) must leave ONE committed row, then publish.

    No stub anywhere: an empty `values` body is refused by the service BEFORE any store write
    (`integration_status_service._checked_values`), and the route's audit is what this pins.
    """
    request_engine = await _engine()
    audit_engine = await _engine()
    RequestSession = _session_factory(request_engine)
    AuditSession = _session_factory(audit_engine)
    observed: list[tuple[str, int]] = []
    publish = events.publish_activity

    async def recording_publish(payload: dict[str, Any]) -> Any:
        async with AuditSession() as reader:
            count = (
                await reader.execute(select(func.count()).select_from(ActivityLog))
            ).scalar_one()
        observed.append((str(payload.get("action")), int(count)))
        return await publish(payload)

    monkeypatch.setattr(events, "publish_activity", recording_publish)
    activity_service.ISOLATED_SESSION_FACTORY = AuditSession
    try:
        async with _client(RequestSession) as client:
            resp = await client.put(
                "/api/v1/integrations/github",
                headers={"X-API-Key": API_KEY},
                json={"values": {}},
            )
        rows = await _activity_rows(AuditSession, "integration_save")
    finally:
        await request_engine.dispose()
        await audit_engine.dispose()

    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "E_VALIDATION"
    assert len(rows) == 1, "the side-effect path wrote no activity row at all"
    assert rows[0].agent_type == "integrations"
    detail = rows[0].detail or {}
    assert detail["provider"] == "github"
    assert detail["outcome"] == "refused"
    assert detail["code"] == "E_VALIDATION"
    assert observed == [("integration_save", 1)], (
        "the audit event must follow the commit, not the flush"
    )
    assert mock_redis.publish.await_count == 1


# ── F-48 — the export streams every member into the archive ────────────────────────────────────


def _instrument_zip(monkeypatch) -> dict[str, list[int]]:
    """Record the size of EVERY payload handed to the archive, per member name."""
    sizes: dict[str, list[int]] = {}
    real_open = zipfile.ZipFile.open
    real_writestr = zipfile.ZipFile.writestr

    class _WriteCounter:
        """A write-through shim: the exporter sees a file object, the test sees the chunk sizes."""

        def __init__(self, handle: Any, name: str) -> None:
            self._handle = handle
            self._name = name

        def write(self, data: bytes) -> int:
            sizes.setdefault(self._name, []).append(len(data))
            return self._handle.write(data)

        def flush(self) -> None:
            self._handle.flush()

        def writable(self) -> bool:
            return True

        def __enter__(self) -> _WriteCounter:
            return self

        def __exit__(self, *exc_info: Any) -> None:
            self._handle.close()

    def counting_open(
        self: zipfile.ZipFile, name: Any, mode: Literal["r", "w"] = "r", *args: Any, **kwargs: Any
    ) -> Any:
        handle = real_open(self, name, mode, *args, **kwargs)
        if mode == "w":
            return _WriteCounter(handle, name if isinstance(name, str) else str(name))
        return handle

    def counting_writestr(self: zipfile.ZipFile, info: Any, data: Any, *args: Any, **kwargs: Any):
        name = info if isinstance(info, str) else str(getattr(info, "filename", info))
        payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        sizes.setdefault(name, []).append(len(payload))
        return real_writestr(self, info, data, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", counting_open)
    monkeypatch.setattr(zipfile.ZipFile, "writestr", counting_writestr)
    return sizes


async def test_the_data_export_streams_members_instead_of_holding_them(
    async_db_session, monkeypatch, tmp_path
):
    """3 000 runs: no single hand-off to the archive may carry half a member, and the ZIP is whole.

    The measurement is BYTES HANDED TO THE ARCHIVE, not RSS: a whole-member build passes one
    payload the size of the member (measured today: one `writestr` of the full CSV text), a streamed
    build passes page-sized chunks and at least two of them.
    """
    from app.models.task import AgentRun

    seeded = 3_000
    async_db_session.add_all(
        [
            AgentRun(
                agent_type="code_generation",
                status="succeeded",
                usage_json={"note": "x" * 200, "n": index},
            )
            for index in range(seeded)
        ]
    )
    await async_db_session.flush()

    sizes = _instrument_zip(monkeypatch)
    path, total_rows = await data_module._build_export(async_db_session)
    try:
        assert path.exists()
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            manifest = json.loads(archive.read("MANIFEST.json"))
            by_name = {member["name"]: member for member in manifest["members"]}
            csv_text = archive.read("agent_runs.csv").decode("utf-8")
    finally:
        data_module._remove_export_tempfile(path)

    assert names == {"MANIFEST.json", *(
        f"{table}.{'csv' if table in data_module.CSV_MEMBERS else 'json'}" for table in data_module.MEMBER_MODELS
    )}
    assert by_name["agent_runs.csv"]["row_count"] == seeded
    assert manifest["total_rows"] == total_rows == seeded
    assert len(csv_text.splitlines()) == seeded + 1, "header + one line per run"

    chunks = sizes.get("agent_runs.csv", [])
    total_bytes = sum(chunks)
    assert total_bytes > 200_000, "the member is big enough for this measurement to mean something"
    assert chunks, "the member was never handed to the archive by the instrumented path"
    assert max(chunks) < total_bytes / 2, (
        f"one call handed the archive {max(chunks)} of {total_bytes} bytes: the member was "
        "materialised in memory instead of streamed"
    )
    assert len(chunks) >= 2, "a streamed member is written in more than one chunk"


# ── F-37 backend — GET /config/phone answers 200 whether or not a store is mounted ─────────────


async def test_config_phone_answers_200_when_the_store_is_absent(monkeypatch, tmp_path):
    """§4.16: absent store = `200 {"configured": false, "available": false}`, never 5xx."""
    empty = tmp_path / "no-store-here"
    empty.mkdir()
    monkeypatch.setattr(settings, "phone_link_db_dir", "")
    monkeypatch.setattr(phone_link_service, "DEFAULT_STORE_GLOB", str(empty))

    engine = await _engine()
    Session = _session_factory(engine)
    try:
        async with _client(Session) as client:
            resp = await client.get("/api/v1/config/phone", headers={"X-API-Key": API_KEY})
    finally:
        await engine.dispose()

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"configured": False, "available": False}


async def test_config_phone_answers_200_when_the_store_is_mounted(monkeypatch, tmp_path):
    """A mounted, readable store is `available: true` — the same 200, an honest true."""
    store = tmp_path / "phone-link"
    store.mkdir()
    for name in phone_link_service.STORE_FILES:
        with sqlite3.connect(store / name) as connection:
            connection.execute("CREATE TABLE probe (id INTEGER)")
    monkeypatch.setattr(settings, "phone_link_db_dir", str(store))

    engine = await _engine()
    Session = _session_factory(engine)
    try:
        async with _client(Session) as client:
            resp = await client.get("/api/v1/config/phone", headers={"X-API-Key": API_KEY})
    finally:
        await engine.dispose()

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"configured": True, "available": True}


async def test_config_phone_is_a_config_question_not_a_phone_read(monkeypatch, tmp_path):
    """A configured-but-unmounted store is still a 200 with `available: false` (never the 503)."""
    monkeypatch.setattr(settings, "phone_link_db_dir", str(tmp_path / "configured-but-absent"))

    engine = await _engine()
    Session = _session_factory(engine)
    try:
        async with _client(Session) as client:
            resp = await client.get("/api/v1/config/phone", headers={"X-API-Key": API_KEY})
    finally:
        await engine.dispose()

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"configured": True, "available": False}
