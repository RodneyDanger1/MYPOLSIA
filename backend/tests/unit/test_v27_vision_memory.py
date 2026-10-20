"""V27 / L4 — vision memory: migration 0016, the sha256 cache and the history route.

RED-first at base `d6bd944`: `backend/app/models/vision.py` does not exist (this module fails at
import with `ModuleNotFoundError`), `vision_descriptions` is not in the chain (0015 is the head),
`POST /api/v1/vision/describe` persists nothing and has no `cached` field, and there is no
`GET /api/v1/vision/descriptions` route at all.  The failing output is saved under
`build/ui-reverse/wave-v27/l4-vision-memory/red/`.

What is pinned here:

  A. MIGRATION 0016 — single head, chains after 0015, creates `vision_descriptions` with the
     documented columns, the two FKs (`ON DELETE CASCADE` to `task_attachments` AND `tasks`),
     the sha256 index (NOT unique: history needs many rows) and the task_id history index; the
     cascade is proven at the ENGINE level, not only in the DDL string; `downgrade()` removes
     exactly the table + indexes and `upgrade head` re-creates them.
  B. CACHE — the first describe stores a row and answers `cached: false`; the second describe of
     the SAME bytes answers the STORED sentence with `cached: true` and the model double sees
     exactly ONE call in total (the second request is issued with `invoke` replaced by a raiser,
     so a hidden call cannot pass silently).  The lookup is by sha256, so a second attachment
     carrying identical bytes is a hit too; different bytes are a second row and a second call;
     a row measured by a DIFFERENT model is not a hit (a different model is a different
     measurement).  A cache hit writes no new row.
  C. HISTORY — `GET /api/v1/vision/descriptions?task_id=` is newest-first (`created_at, id`),
     scoped to the task, carries the documented row shape (`id`, `attachment_id`,
     `sha256_prefix`, `description`, `model`, `tokens`, `created_at`); unknown task is
     `E_NOT_FOUND`; a missing/non-integer `task_id` is `E_VALIDATION`; the route needs the owner
     key.
  D. HONESTY PRESERVED (the V26 contract, unchanged) — no DeepSeek key is still `E_CONFIG`
     naming `DEEPSEEK_API_KEY` and stores nothing; `tokens: null` stays NULL in the row and in
     the cache hit (never a fabricated 0); the description never overwrites `Task.result_summary`
     or the attachment row.

The model call is a LABELLED HTTP DOUBLE (`_fake_invoke`) implementing the real runtime's
`invoke` signature and envelope shape; it is a test double only and no product code branches on
it.
"""
from __future__ import annotations

import base64
import hashlib
import json
import pathlib
import sqlite3
import struct
import zlib

import pytest
import pytest_asyncio
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

# Register the new table on Base.metadata BEFORE the shared `async_db_session` fixture runs
# `create_all` (also registered by `app/models/__init__.py`, V27/L4 — this import keeps the
# dependency visible in the suite that needs it).
import app.models.vision  # noqa: F401,E402
from alembic import command

BACKEND = pathlib.Path(__file__).resolve().parents[2]
HEADERS = {"X-API-Key": "test-key"}
MODEL = "deepseek-flash"
SHA_PREFIX_LEN = 12


# ─────────────────────────────────────────────────────────────────────────────────────────────
# fixtures: a REAL PNG (stdlib only), the attachment store, the router in a minimal app
# ─────────────────────────────────────────────────────────────────────────────────────────────

def _png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """A real, valid flat-colour PNG — built with stdlib zlib/struct (TEST FIXTURE only)."""
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit truecolour RGB
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


RED_PNG = _png(96, 96, (255, 0, 0))
BLUE_PNG = _png(96, 96, (0, 0, 255))
GREEN_PNG = _png(96, 96, (0, 255, 0))


@pytest.fixture
def attachments_root(tmp_path, monkeypatch):
    """Every test's blobs live in its own tmp dir — the shipped data dir is never touched."""
    from app.config import settings

    root = tmp_path / "attachments"
    monkeypatch.setattr(settings, "attachments_dir", str(root), raising=False)
    return root


@pytest.fixture
def configured_key(monkeypatch):
    """The key seam: a fake key.  Never a real one; never printed."""
    from app.services import secret_store

    monkeypatch.setattr(secret_store, "get_deepseek_api_key", lambda: "fake-deepseek-key-v27")
    return "fake-deepseek-key-v27"


@pytest_asyncio.fixture
async def vision_client(async_db_session, attachments_root, monkeypatch):
    """The vision router mounted in a MINIMAL app (main.py's include list is not this lane's file).

    The two exception handlers mirror the shipped refusal bridge so assertions read the REAL wire
    envelope (`{ok:false, error:{code,message,how_to_fix,context}}` + `X-Polsia-Error-Code`)
    instead of FastAPI's uncoded `{"detail": ...}` — the same bridge `test_v26_vision.py` builds,
    from the same canonical helpers (`errors.to_http_detail` / `errors.http_status_for`).
    """
    from fastapi.responses import JSONResponse
    from starlette.exceptions import HTTPException as StarletteHTTPException

    from app.api.v1 import vision
    from app.config import settings
    from app.core.database import get_db
    from app.core.errors import PolsiaError, http_status_for, to_http_detail

    monkeypatch.setattr(settings, "api_key", "test-key", raising=False)
    app = FastAPI()
    app.include_router(vision.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: async_db_session

    @app.exception_handler(PolsiaError)
    async def _polsia_error(_request, exc: PolsiaError) -> JSONResponse:
        return JSONResponse(
            status_code=http_status_for(exc.code),
            content=to_http_detail(exc),
            headers={"X-Polsia-Error-Code": exc.code},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _coded_http(_request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, dict) and isinstance(detail.get("code"), str) and detail["code"]:
            envelope = {
                "ok": False,
                "error": {
                    "code": detail["code"],
                    "message": detail.get("message") or "",
                    "how_to_fix": detail.get("how_to_fix") or "",
                    "context": detail.get("context") if isinstance(detail.get("context"), dict) else {},
                },
            }
            headers = dict(exc.headers or {})
            headers.setdefault("X-Polsia-Error-Code", detail["code"])
            return JSONResponse(status_code=exc.status_code, content=envelope, headers=headers)
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": detail},
            headers=dict(exc.headers or {}),
        )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


async def _seed_task(db, *, title: str = "Read the screenshot", result_summary: str | None = None):
    from app.models.task import Task

    task = Task(
        title=title,
        agent_type="finance",
        status="pending",
        priority=3,
        result_summary=result_summary,
    )
    db.add(task)
    await db.commit()
    return task


async def _attach(db, task_id: int, data: bytes = RED_PNG, filename: str = "shot.png"):
    from app.services import task_attachment_service as svc

    row = await svc.save_attachment(db, task_id=task_id, filename=filename, data=data)
    await db.commit()
    return row


# ─────────────────────────────────────────────────────────────────────────────────────────────
# the LABELLED model double: real `invoke` signature, real envelope shape
# ─────────────────────────────────────────────────────────────────────────────────────────────

def _fake_invoke(
    *,
    description: str = "The image is a solid red square.",
    usage: dict | None = None,
    served_model: str = MODEL,
):
    """Returns (fake, calls).  `fake` implements `deepseek_runtime.invoke`'s signature."""
    from app.agents import claude_cli

    calls: list[dict] = []

    def fake(
        inv,
        *,
        client=None,
        api_key=None,
        model=None,
        tools=None,
        agent_type=None,
        task_title=None,
        images=None,
    ):
        calls.append(
            {
                "inv": inv,
                "api_key": api_key,
                "model": model,
                "tools": tools,
                "images": images,
            }
        )
        return claude_cli.InvokeResult(
            stdout=json.dumps(
                {
                    "type": "result",
                    "is_error": False,
                    "result": description,
                    "model": served_model,
                    "usage": {} if usage is None else usage,
                }
            ),
            stderr="",
            exit_code=0,
            wall_seconds=0.01,
            transport="http",
        )

    return fake, calls


def _forbidden_invoke(*_args, **_kwargs):  # pragma: no cover - asserted never reached
    raise AssertionError("a cache hit must make ZERO provider calls")


# ─────────────────────────────────────────────────────────────────────────────────────────────
# A. migration 0016
# ─────────────────────────────────────────────────────────────────────────────────────────────

def _revision_graph() -> dict[str, str | None]:
    graph: dict[str, str | None] = {}
    for path in sorted((BACKEND / "alembic" / "versions").glob("[0-9]*.py")):
        body = path.read_text(encoding="utf-8")
        revision = down = None
        for line in body.splitlines():
            if line.startswith("revision:"):
                revision = line.split("=", 1)[1].strip().strip('"')
            elif line.startswith("down_revision:"):
                down = line.split("=", 1)[1].strip().strip('"')
        assert revision, f"unparsable revision header: {path.name}"
        graph[revision] = None if down in (None, "None") else down
    return graph


def test_migration_0016_is_the_single_head_and_chains_after_0015():
    graph = _revision_graph()
    heads = sorted(r for r in graph if r not in set(graph.values()))
    assert heads == ["0016"], f"expected a single head (0016), found {heads}"
    assert graph["0016"] == "0015", "V27/L4's migration must chain directly after 0015"


def test_migration_0016_round_trips_creates_both_cascades_and_both_indexes(monkeypatch, tmp_path):
    from app.config import settings

    db_path = tmp_path / "v27_l4_0016.sqlite"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    monkeypatch.setattr(settings, "database_url", url)
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)

    command.upgrade(cfg, "0015")
    con = sqlite3.connect(db_path.as_posix())
    try:
        assert con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='vision_descriptions'"
        ).fetchone() is None, "RED: 0015 must not already carry the memory table"
    finally:
        con.close()

    command.upgrade(cfg, "0016")
    con = sqlite3.connect(db_path.as_posix())
    try:
        assert con.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0016"
        ddl = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='vision_descriptions'"
        ).fetchone()[0]
        upper = ddl.upper()
        assert "REFERENCES TASK_ATTACHMENTS" in upper and "REFERENCES TASKS" in upper, ddl
        assert upper.count("ON DELETE CASCADE") == 2, ddl

        cols = {row[1]: row for row in con.execute("PRAGMA table_info(vision_descriptions)")}
        assert {
            "id",
            "attachment_id",
            "task_id",
            "sha256",
            "description",
            "model",
            "tokens",
            "created_at",
        } <= set(cols), cols
        for required in ("attachment_id", "task_id", "sha256", "description", "created_at"):
            assert cols[required][3] == 1, f"{required} must be NOT NULL"
        assert cols["model"][3] == 0 and cols["tokens"][3] == 0, "model/tokens are nullable"
        assert cols["created_at"][4] is not None, "created_at must default to the database clock"

        indexes = {
            name: unique
            for _seq, name, unique, _origin, _partial in con.execute(
                "PRAGMA index_list(vision_descriptions)"
            )
        }
        assert indexes.get("ix_vision_descriptions_sha256") == 0, (
            "the sha256 index must exist and must NOT be unique (history keeps many rows)"
        )
        assert indexes.get("ix_vision_descriptions_task_id") == 0

        # The cascades are REAL at the engine level, not only in the DDL string.
        con.execute("PRAGMA foreign_keys=ON")
        sha = hashlib.sha256(RED_PNG).hexdigest()
        stored = "a" * 32 + ".png"
        con.execute("INSERT INTO tasks (id, title, agent_type, status) VALUES (1, 't', 'finance', 'pending')")
        con.execute("INSERT INTO tasks (id, title, agent_type, status) VALUES (2, 't2', 'finance', 'pending')")
        for attachment_id, task_id in ((1, 1), (2, 2)):
            con.execute(
                "INSERT INTO task_attachments (id, task_id, filename, stored_name, mime, "
                "size_bytes, sha256) VALUES (?, ?, 'a.png', ?, 'image/png', 3, ?)",
                (attachment_id, task_id, stored, sha),
            )
        for attachment_id, task_id in ((1, 1), (2, 2)):
            con.execute(
                "INSERT INTO vision_descriptions (attachment_id, task_id, sha256, description, "
                "model, tokens) VALUES (?, ?, ?, 'a red square', 'deepseek-flash', 3)",
                (attachment_id, task_id, sha),
            )
        con.commit()

        con.execute("DELETE FROM task_attachments WHERE id=2")
        con.commit()
        assert con.execute(
            "SELECT COUNT(*) FROM vision_descriptions WHERE attachment_id=2"
        ).fetchone()[0] == 0, "a description must not outlive its attachment"

        con.execute("DELETE FROM tasks WHERE id=1")
        con.commit()
        assert con.execute("SELECT COUNT(*) FROM vision_descriptions").fetchone()[0] == 0, (
            "a description must not outlive its task"
        )
    finally:
        con.close()

    command.downgrade(cfg, "0015")
    con = sqlite3.connect(db_path.as_posix())
    try:
        assert con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='vision_descriptions'"
        ).fetchone() is None, "downgrade must remove exactly the table upgrade added"
        leftovers = con.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'ix_vision_descriptions%'"
        ).fetchall()
        assert leftovers == [], leftovers
        assert con.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0015"
    finally:
        con.close()

    command.upgrade(cfg, "head")
    con = sqlite3.connect(db_path.as_posix())
    try:
        assert con.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0016"
        assert con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='vision_descriptions'"
        ).fetchone() is not None
    finally:
        con.close()


def test_the_model_declares_the_same_table_the_migration_creates():
    from app.models.vision import VisionDescriptionRow

    table = VisionDescriptionRow.__table__
    assert table.name == "vision_descriptions"
    assert [c.name for c in table.columns] == [
        "id",
        "attachment_id",
        "task_id",
        "sha256",
        "description",
        "model",
        "tokens",
        "created_at",
    ]
    cascades = {
        fk.parent.name: (fk.column.table.name, fk.ondelete)
        for fk in table.foreign_keys
    }
    assert cascades == {
        "attachment_id": ("task_attachments", "CASCADE"),
        "task_id": ("tasks", "CASCADE"),
    }, cascades
    index_names = {ix.name: tuple(c.name for c in ix.columns) for ix in table.indexes}
    assert index_names == {
        "ix_vision_descriptions_sha256": ("sha256",),
        "ix_vision_descriptions_task_id": ("task_id",),
    }, index_names
    assert all(not ix.unique for ix in table.indexes), "history needs many rows per sha256"


# ─────────────────────────────────────────────────────────────────────────────────────────────
# B. cache
# ─────────────────────────────────────────────────────────────────────────────────────────────

async def _rows(db):
    from sqlalchemy import select

    from app.models.vision import VisionDescriptionRow

    result = await db.execute(
        select(VisionDescriptionRow).order_by(VisionDescriptionRow.id.asc())
    )
    return list(result.scalars().all())


@pytest.mark.asyncio
async def test_first_describe_stores_a_row_and_answers_cached_false(
    vision_client, async_db_session, configured_key, monkeypatch
):
    from app.services import vision_service

    task = await _seed_task(async_db_session)
    row = await _attach(async_db_session, task.id, RED_PNG, "red.png")
    fake, calls = _fake_invoke(usage={"input_tokens": 120, "output_tokens": 18})
    monkeypatch.setattr(vision_service, "invoke", fake)

    response = await vision_client.post(
        "/api/v1/vision/describe", headers=HEADERS, json={"attachment_id": row.id}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {
        "description": "The image is a solid red square.",
        "model": MODEL,
        "tokens": 138,
        "cached": False,
    }, body
    assert len(calls) == 1

    stored = await _rows(async_db_session)
    assert len(stored) == 1, stored
    record = stored[0]
    assert record.attachment_id == row.id
    assert record.task_id == task.id
    assert record.sha256 == hashlib.sha256(RED_PNG).hexdigest()
    assert record.description == "The image is a solid red square."
    assert record.model == MODEL
    assert record.tokens == 138
    assert record.created_at is not None


@pytest.mark.asyncio
async def test_second_describe_of_the_same_bytes_is_served_from_cache_with_zero_calls(
    vision_client, async_db_session, configured_key, monkeypatch
):
    from app.services import vision_service

    task = await _seed_task(async_db_session)
    row = await _attach(async_db_session, task.id, RED_PNG, "red.png")
    fake, calls = _fake_invoke(usage={"input_tokens": 120, "output_tokens": 18})
    monkeypatch.setattr(vision_service, "invoke", fake)

    first = await vision_client.post(
        "/api/v1/vision/describe", headers=HEADERS, json={"attachment_id": row.id}
    )
    assert first.status_code == 200, first.text
    assert first.json()["cached"] is False

    # The second request runs with the provider replaced by a RAISER: any call is a hard failure.
    monkeypatch.setattr(vision_service, "invoke", _forbidden_invoke)
    second = await vision_client.post(
        "/api/v1/vision/describe", headers=HEADERS, json={"attachment_id": row.id}
    )

    assert second.status_code == 200, second.text
    body = second.json()
    assert body["cached"] is True
    assert body["description"] == first.json()["description"]
    assert body["model"] == MODEL
    assert body["tokens"] == 138
    assert len(calls) == 1, f"the provider must be called exactly once in total, saw {len(calls)}"
    assert len(await _rows(async_db_session)) == 1, "a hit must not write a second row"


@pytest.mark.asyncio
async def test_the_cache_is_keyed_by_the_sha256_so_a_second_attachment_with_equal_bytes_hits(
    vision_client, async_db_session, configured_key, monkeypatch
):
    from app.services import vision_service

    task = await _seed_task(async_db_session)
    first_row = await _attach(async_db_session, task.id, RED_PNG, "one.png")
    second_row = await _attach(async_db_session, task.id, RED_PNG, "copy.png")
    assert first_row.id != second_row.id
    assert first_row.sha256 == second_row.sha256

    fake, calls = _fake_invoke(usage={"input_tokens": 10, "output_tokens": 5})
    monkeypatch.setattr(vision_service, "invoke", fake)

    one = await vision_client.post(
        "/api/v1/vision/describe", headers=HEADERS, json={"attachment_id": first_row.id}
    )
    assert one.status_code == 200, one.text
    assert one.json()["cached"] is False

    two = await vision_client.post(
        "/api/v1/vision/describe", headers=HEADERS, json={"attachment_id": second_row.id}
    )
    assert two.status_code == 200, two.text
    assert two.json()["cached"] is True
    assert two.json()["description"] == one.json()["description"]
    assert len(calls) == 1, "identical bytes must not be measured twice"


@pytest.mark.asyncio
async def test_different_bytes_are_a_second_row_and_a_second_call(
    vision_client, async_db_session, configured_key, monkeypatch
):
    from app.services import vision_service

    task = await _seed_task(async_db_session)
    red = await _attach(async_db_session, task.id, RED_PNG, "red.png")
    blue = await _attach(async_db_session, task.id, BLUE_PNG, "blue.png")

    from app.agents import claude_cli

    calls: list[str] = []

    def colour_aware(inv, *, images=None, **kwargs):
        payload = base64.b64decode(images[0]["data_uri"].split(",", 1)[1])
        digest = hashlib.sha256(payload).hexdigest()
        calls.append(digest)
        return claude_cli.InvokeResult(
            stdout=json.dumps(
                {
                    "type": "result",
                    "is_error": False,
                    "result": (
                        "The image is a solid blue square."
                        if digest == hashlib.sha256(BLUE_PNG).hexdigest()
                        else "The image is a solid red square."
                    ),
                    "model": MODEL,
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                }
            ),
            stderr="",
            exit_code=0,
            wall_seconds=0.01,
            transport="http",
        )

    monkeypatch.setattr(vision_service, "invoke", colour_aware)

    first = await vision_client.post(
        "/api/v1/vision/describe", headers=HEADERS, json={"attachment_id": red.id}
    )
    second = await vision_client.post(
        "/api/v1/vision/describe", headers=HEADERS, json={"attachment_id": blue.id}
    )

    assert first.status_code == 200 and second.status_code == 200, (first.text, second.text)
    assert first.json()["cached"] is False and second.json()["cached"] is False
    assert first.json()["description"] == "The image is a solid red square."
    assert second.json()["description"] == "The image is a solid blue square."
    assert len(calls) == 2

    stored = await _rows(async_db_session)
    assert [row.sha256 for row in stored] == [
        hashlib.sha256(RED_PNG).hexdigest(),
        hashlib.sha256(BLUE_PNG).hexdigest(),
    ]


@pytest.mark.asyncio
async def test_a_description_measured_by_a_different_model_is_not_a_cache_hit(
    vision_client, async_db_session, configured_key, monkeypatch
):
    """A cached sentence from another model is not this model's measurement."""
    from app.models.vision import VisionDescriptionRow
    from app.services import vision_service

    task = await _seed_task(async_db_session)
    row = await _attach(async_db_session, task.id, RED_PNG, "red.png")
    async_db_session.add(
        VisionDescriptionRow(
            attachment_id=row.id,
            task_id=task.id,
            sha256=row.sha256,
            description="An older model's answer.",
            model="some-other-model",
            tokens=7,
        )
    )
    await async_db_session.commit()

    fake, calls = _fake_invoke(description="The current model's answer.")
    monkeypatch.setattr(vision_service, "invoke", fake)

    response = await vision_client.post(
        "/api/v1/vision/describe", headers=HEADERS, json={"attachment_id": row.id}
    )

    assert response.status_code == 200, response.text
    assert response.json()["cached"] is False
    assert response.json()["description"] == "The current model's answer."
    assert len(calls) == 1
    assert len(await _rows(async_db_session)) == 2


# ─────────────────────────────────────────────────────────────────────────────────────────────
# C. history
# ─────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_history_is_newest_first_and_scoped_to_the_task(
    vision_client, async_db_session, configured_key, monkeypatch
):
    from app.services import vision_service

    task = await _seed_task(async_db_session, title="task A")
    other = await _seed_task(async_db_session, title="task B")
    red = await _attach(async_db_session, task.id, RED_PNG, "red.png")
    blue = await _attach(async_db_session, task.id, BLUE_PNG, "blue.png")
    elsewhere = await _attach(async_db_session, other.id, GREEN_PNG, "other.png")

    fake, _calls = _fake_invoke(description="first look")
    monkeypatch.setattr(vision_service, "invoke", fake)
    await vision_client.post(
        "/api/v1/vision/describe", headers=HEADERS, json={"attachment_id": red.id}
    )
    fake_two, _calls_two = _fake_invoke(description="second look")
    monkeypatch.setattr(vision_service, "invoke", fake_two)
    await vision_client.post(
        "/api/v1/vision/describe", headers=HEADERS, json={"attachment_id": blue.id}
    )
    fake_three, _calls_three = _fake_invoke(description="another task")
    monkeypatch.setattr(vision_service, "invoke", fake_three)
    await vision_client.post(
        "/api/v1/vision/describe", headers=HEADERS, json={"attachment_id": elsewhere.id}
    )

    response = await vision_client.get(
        "/api/v1/vision/descriptions", headers=HEADERS, params={"task_id": task.id}
    )

    assert response.status_code == 200, response.text
    history = response.json()
    assert [entry["description"] for entry in history] == ["second look", "first look"], history
    assert [entry["attachment_id"] for entry in history] == [blue.id, red.id]
    assert all(entry["description"] != "another task" for entry in history), (
        "history must be scoped to the requested task"
    )

    other_history = await vision_client.get(
        "/api/v1/vision/descriptions", headers=HEADERS, params={"task_id": other.id}
    )
    assert other_history.status_code == 200, other_history.text
    assert [entry["description"] for entry in other_history.json()] == ["another task"]


@pytest.mark.asyncio
async def test_history_rows_carry_the_documented_shape_and_a_sha256_prefix(
    vision_client, async_db_session, configured_key, monkeypatch
):
    from app.services import vision_service

    task = await _seed_task(async_db_session)
    row = await _attach(async_db_session, task.id, RED_PNG, "red.png")
    fake, _calls = _fake_invoke(
        description="A red square.", usage={"input_tokens": 4, "output_tokens": 2}
    )
    monkeypatch.setattr(vision_service, "invoke", fake)
    await vision_client.post(
        "/api/v1/vision/describe", headers=HEADERS, json={"attachment_id": row.id}
    )

    response = await vision_client.get(
        "/api/v1/vision/descriptions", headers=HEADERS, params={"task_id": task.id}
    )

    assert response.status_code == 200, response.text
    entries = response.json()
    assert len(entries) == 1
    entry = entries[0]
    assert set(entry) == {
        "id",
        "attachment_id",
        "sha256_prefix",
        "description",
        "model",
        "tokens",
        "created_at",
    }, entry
    assert entry["attachment_id"] == row.id
    assert entry["description"] == "A red square."
    assert entry["model"] == MODEL
    assert entry["tokens"] == 6
    assert entry["sha256_prefix"] == row.sha256[:SHA_PREFIX_LEN]
    assert len(entry["sha256_prefix"]) == SHA_PREFIX_LEN
    assert entry["created_at"], "created_at is the stored timestamp, never omitted in silence"


@pytest.mark.asyncio
async def test_history_of_a_task_with_no_descriptions_is_an_honest_empty_list(
    vision_client, async_db_session, configured_key
):
    task = await _seed_task(async_db_session)

    response = await vision_client.get(
        "/api/v1/vision/descriptions", headers=HEADERS, params={"task_id": task.id}
    )

    assert response.status_code == 200, response.text
    assert response.json() == []


@pytest.mark.asyncio
async def test_history_of_an_unknown_task_is_named_not_found(vision_client, configured_key):
    response = await vision_client.get(
        "/api/v1/vision/descriptions", headers=HEADERS, params={"task_id": 424242}
    )

    assert response.status_code == 404, response.text
    error = response.json()["error"]
    assert error["code"] == "E_NOT_FOUND"
    assert error["context"]["task_id"] == 424242
    assert response.headers.get("X-Polsia-Error-Code") == "E_NOT_FOUND"


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [{}, {"task_id": "abc"}, {"task_id": ""}, {"task_id": "-1"},
                                   {"task_id": "1.5"}, {"task_id": True}])
async def test_history_without_a_positive_integer_task_id_is_named_validation(
    vision_client, configured_key, params
):
    response = await vision_client.get(
        "/api/v1/vision/descriptions", headers=HEADERS, params=params
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "E_VALIDATION"
    assert response.headers.get("X-Polsia-Error-Code") == "E_VALIDATION"


@pytest.mark.asyncio
async def test_every_vision_route_still_requires_the_owner_key(vision_client):
    describe = await vision_client.post("/api/v1/vision/describe", json={"attachment_id": 1})
    history = await vision_client.get("/api/v1/vision/descriptions", params={"task_id": 1})

    assert describe.status_code == 401, describe.text
    assert history.status_code == 401, history.text


# ─────────────────────────────────────────────────────────────────────────────────────────────
# D. the V26 honesty contract, unchanged by memory
# ─────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_deepseek_key_is_still_e_config_and_stores_nothing(
    vision_client, async_db_session, monkeypatch
):
    from app.services import secret_store, vision_service

    task = await _seed_task(async_db_session)
    row = await _attach(async_db_session, task.id, RED_PNG)
    monkeypatch.setattr(secret_store, "get_deepseek_api_key", lambda: None)
    monkeypatch.setattr(vision_service, "invoke", _forbidden_invoke)

    response = await vision_client.post(
        "/api/v1/vision/describe", headers=HEADERS, json={"attachment_id": row.id}
    )

    assert response.status_code == 503, response.text
    error = response.json()["error"]
    assert error["code"] == "E_CONFIG"
    assert "DEEPSEEK_API_KEY" in error["message"]
    assert await _rows(async_db_session) == [], "a refused describe must not leave a row"


@pytest.mark.asyncio
async def test_unmeasured_tokens_stay_null_in_the_row_and_in_the_cache_hit(
    vision_client, async_db_session, configured_key, monkeypatch
):
    from app.services import vision_service

    task = await _seed_task(async_db_session)
    row = await _attach(async_db_session, task.id, RED_PNG)
    fake, _calls = _fake_invoke(description="A red square.", usage={})
    monkeypatch.setattr(vision_service, "invoke", fake)

    first = await vision_client.post(
        "/api/v1/vision/describe", headers=HEADERS, json={"attachment_id": row.id}
    )
    assert first.status_code == 200, first.text
    assert first.json()["tokens"] is None

    stored = await _rows(async_db_session)
    assert stored[0].tokens is None, "NULL is unmeasured; 0 would be a fabricated measurement"

    monkeypatch.setattr(vision_service, "invoke", _forbidden_invoke)
    second = await vision_client.post(
        "/api/v1/vision/describe", headers=HEADERS, json={"attachment_id": row.id}
    )
    assert second.status_code == 200, second.text
    assert second.json()["cached"] is True
    assert second.json()["tokens"] is None


@pytest.mark.asyncio
async def test_describe_with_memory_still_never_overwrites_the_task_result(
    vision_client, async_db_session, configured_key, monkeypatch
):
    from app.models.task import Task, TaskAttachment
    from app.services import vision_service

    task = await _seed_task(async_db_session, result_summary="the original task result")
    row = await _attach(async_db_session, task.id, RED_PNG)
    original = (row.sha256, row.mime, row.filename)
    fake, _calls = _fake_invoke(description="A red square.")
    monkeypatch.setattr(vision_service, "invoke", fake)

    response = await vision_client.post(
        "/api/v1/vision/describe", headers=HEADERS, json={"attachment_id": row.id}
    )
    assert response.status_code == 200, response.text

    refreshed = await async_db_session.get(Task, task.id)
    await async_db_session.refresh(refreshed)
    assert refreshed.result_summary == "the original task result"

    attachment = await async_db_session.get(TaskAttachment, row.id)
    await async_db_session.refresh(attachment)
    assert (attachment.sha256, attachment.mime, attachment.filename) == original

    assert len(await _rows(async_db_session)) == 1, "memory is its own table, not the task's"
