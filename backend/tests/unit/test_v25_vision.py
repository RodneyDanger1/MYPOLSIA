"""V25 / L1 — vision input: task image attachments, storage, HTTP surface, and the model request.

RED-first at base 7faa6dd: `app.services.task_attachment_service`, the `task_attachments` table,
the four routes and the runtime's `images=` seam do not exist, so this file fails at import
(`ModuleNotFoundError`) before any assertion runs.  `raw/red-backend.txt` records that leg.

What is pinned here, in four blocks:

  A. SCHEMA — `task_attachments` exists in the ORM with the documented columns, a task_id index
     and `ON DELETE CASCADE`; migration `0013` is the single head, chains after `0012`, creates
     the table + index + cascade, and round-trips (upgrade -> downgrade -> upgrade) on SQLite.
  B. STORAGE — magic-byte sniffing (PNG/JPEG/WebP accepted; a GIF or a text file named `.png`
     refused BY NAME, never by extension), the 5 MiB per-file cap, the 4-per-task cap, the 8 MiB
     per-task total cap, the sha256, and a stored name that can never escape the task directory.
  C. API — POST/GET list/GET bytes/DELETE with owner-key auth, the correct content type, named
     E_NOT_FOUND for an unknown task/attachment, E_UPSTREAM for a row whose blob is gone, 401
     without a key, and no path traversal through the stored name.
  D. RUNTIME — no images => the user message is the SAME plain string it has always been; images
     => OpenAI-compatible content parts (text + `image_url` data URI); the payload cap refuses
     E_VALIDATION before any HTTP call; the task title rides the invoke seam into
     `registry.execute_tool(task_title=...)`; a replay does not send the images twice.
"""
from __future__ import annotations

import base64
import hashlib
import json
import pathlib
import sqlite3

import httpx
import pytest
from alembic.config import Config as AlembicConfig

from alembic import command

BACKEND = pathlib.Path(__file__).resolve().parents[2]
HEADERS = {"X-API-Key": "test-key"}

# A real 1x1 PNG (67 bytes) — magic-byte-sniffable and small enough to inline.
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
PNG_HEADER = b"\x89PNG\r\n\x1a\n"
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + b"\x00" * 16
WEBP = b"RIFF" + (32).to_bytes(4, "little") + b"WEBP" + b"VP8 " + b"\x00" * 20
GIF = b"GIF89a" + b"\x00" * 32


def png_of_size(size: int) -> bytes:
    """A magic-byte-valid PNG of exactly `size` bytes (header + zero padding)."""
    assert size >= len(PNG_HEADER)
    return PNG_HEADER + b"\x00" * (size - len(PNG_HEADER))


# ─────────────────────────────────────────────────────────────────────────────────────────────
# fixtures / helpers
# ─────────────────────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def attachments_root(tmp_path, monkeypatch):
    """Every test's blobs live in its own tmp dir — the shipped data dir is never touched."""
    from app.config import settings

    root = tmp_path / "attachments"
    monkeypatch.setattr(settings, "attachments_dir", str(root), raising=False)
    return root


async def _seed_task(db, *, title: str = "Read the screenshot", agent_type: str = "finance"):
    from app.models.task import Task

    task = Task(title=title, agent_type=agent_type, status="pending", priority=3)
    db.add(task)
    await db.commit()
    return task


async def _upload(client, task_id: int, data: bytes = PNG_1PX, filename: str = "shot.png",
                  content_type: str = "image/png", headers=HEADERS):
    return await client.post(
        f"/api/v1/tasks/{task_id}/attachments",
        headers=headers,
        files={"file": (filename, data, content_type)},
    )


# ─────────────────────────────────────────────────────────────────────────────────────────────
# A. SCHEMA — table, columns, index, cascade, migration chain
# ─────────────────────────────────────────────────────────────────────────────────────────────


def test_task_attachments_table_contract():
    from app.models.task import TaskAttachment

    table = TaskAttachment.__table__
    assert table.name == "task_attachments"
    assert [c.name for c in table.columns] == [
        "id",
        "task_id",
        "filename",
        "stored_name",
        "mime",
        "size_bytes",
        "sha256",
        "created_at",
    ]
    fk = list(table.c.task_id.foreign_keys)[0]
    assert fk.column.table.name == "tasks"
    assert fk.ondelete == "CASCADE", "an attachment must not outlive its task"
    assert any(ix.name == "ix_task_attachments_task_id" for ix in table.indexes)


def test_migration_0013_is_the_single_head_and_chains_after_0012():
    graph: dict[str, str | None] = {}
    for path in sorted((BACKEND / "alembic" / "versions").glob("*.py")):
        body = path.read_text(encoding="utf-8")
        revision = down = None
        for line in body.splitlines():
            if line.startswith("revision:"):
                revision = line.split("=", 1)[1].strip().strip('"')
            elif line.startswith("down_revision:"):
                down = line.split("=", 1)[1].strip().strip('"')
        assert revision, f"unparsable revision header: {path.name}"
        graph[revision] = None if down in (None, "None") else down
    heads = sorted(r for r in graph if r not in set(graph.values()))
    assert len(heads) == 1, f"expected a single head, found {heads}"
    assert graph["0013"] == "0012", "V25/L1's migration must chain directly after 0012"
    # 0013 is an ancestor of the single head (L4's 0014 legally follows it in this wave).
    cursor, seen = heads[0], []
    while cursor is not None:
        seen.append(cursor)
        cursor = graph.get(cursor)
    assert "0013" in seen, f"0013 is not on the chain to the head {heads[0]}: {seen}"


def test_migration_0013_creates_the_table_cascade_and_index_then_round_trips(monkeypatch, tmp_path):
    from app.config import settings

    db_path = tmp_path / "v25_l1_0013.sqlite"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    monkeypatch.setattr(settings, "database_url", url)
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)

    command.upgrade(cfg, "0012")
    con = sqlite3.connect(db_path.as_posix())
    try:
        assert con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='task_attachments'"
        ).fetchone() is None, "RED: 0012 must not already carry the table"
    finally:
        con.close()

    command.upgrade(cfg, "0013")
    con = sqlite3.connect(db_path.as_posix())
    try:
        assert con.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0013"
        ddl = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='task_attachments'"
        ).fetchone()[0]
        assert "ON DELETE CASCADE" in ddl.upper(), ddl
        cols = {row[1]: row for row in con.execute("PRAGMA table_info(task_attachments)")}
        assert {"id", "task_id", "filename", "stored_name", "mime", "size_bytes", "sha256",
                "created_at"} <= set(cols)
        assert cols["task_id"][3] == 1, "task_id must be NOT NULL"
        assert cols["size_bytes"][3] == 1 and cols["sha256"][3] == 1
        indexes = dict(con.execute(
            "SELECT name, tbl_name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='task_attachments'"
        ))
        assert indexes.get("ix_task_attachments_task_id") == "task_attachments"

        # the cascade is REAL at the engine level, not only in the DDL string
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("INSERT INTO tasks (id, title, agent_type, status) VALUES (1, 't', 'finance', 'pending')")
        con.execute(
            "INSERT INTO task_attachments (task_id, filename, stored_name, mime, size_bytes, sha256) "
            "VALUES (1, 'a.png', 'a.png', 'image/png', 3, 'x')"
        )
        con.commit()
        con.execute("DELETE FROM tasks WHERE id=1")
        con.commit()
        assert con.execute("SELECT COUNT(*) FROM task_attachments").fetchone()[0] == 0
    finally:
        con.close()

    command.downgrade(cfg, "0012")
    con = sqlite3.connect(db_path.as_posix())
    try:
        assert con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='task_attachments'"
        ).fetchone() is None, "downgrade must remove exactly the table upgrade added"
        assert con.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0012"
    finally:
        con.close()

    command.upgrade(cfg, "head")
    con = sqlite3.connect(db_path.as_posix())
    try:
        assert con.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0014"
        assert con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='task_attachments'"
        ).fetchone() is not None
    finally:
        con.close()


# ─────────────────────────────────────────────────────────────────────────────────────────────
# B. STORAGE — magic bytes, caps, exact path, no traversal
# ─────────────────────────────────────────────────────────────────────────────────────────────


def test_the_settings_default_is_the_documented_attachments_dir():
    """The env name is `ATTACHMENTS_DIR`; the default is part of the documented contract."""
    from app.config import Settings

    assert Settings.model_fields["attachments_dir"].default == "data/attachments"


def test_magic_byte_sniffing_accepts_png_jpeg_webp_and_refuses_everything_else():
    from app.services import task_attachment_service as svc

    assert svc.sniff_image_mime(PNG_1PX) == "image/png"
    assert svc.sniff_image_mime(JPEG) == "image/jpeg"
    assert svc.sniff_image_mime(WEBP) == "image/webp"
    assert svc.sniff_image_mime(GIF) is None
    assert svc.sniff_image_mime(b"<html>not an image</html>") is None
    assert svc.sniff_image_mime(b"") is None
    # a truncated RGBA "WEBP" is not enough: RIFF alone is not an image
    assert svc.sniff_image_mime(b"RIFF\x00\x00\x00\x00WAVE") is None


@pytest.mark.asyncio
async def test_uploaded_extension_is_derived_from_the_sniffed_bytes_not_the_client_name(
    async_db_session, attachments_root
):
    from app.services import task_attachment_service as svc

    task = await _seed_task(async_db_session)
    # a JPEG mislabelled by the client as name.png / content-type image/png
    row = await svc.save_attachment(
        async_db_session, task_id=task.id, filename="actually-a-jpeg.png", data=JPEG,
        declared_mime="image/png",
    )
    assert row.mime == "image/jpeg", "the SNIFFED type wins over the client's claim"
    assert row.stored_name.endswith(".jpg")
    assert row.size_bytes == len(JPEG)
    assert row.sha256 == hashlib.sha256(JPEG).hexdigest()
    path = svc.attachment_path(row)
    assert path.parent == (attachments_root / f"task-{task.id}").resolve()
    assert path.read_bytes() == JPEG


@pytest.mark.asyncio
async def test_a_non_image_is_refused_by_name_and_nothing_is_written(async_db_session, attachments_root):
    from app.core.errors import E_VALIDATION, PolsiaError
    from app.services import task_attachment_service as svc

    task = await _seed_task(async_db_session)
    with pytest.raises(PolsiaError) as excinfo:
        await svc.save_attachment(
            async_db_session, task_id=task.id, filename="innocent.png", data=b"GIF89a...."
        )
    assert excinfo.value.code == E_VALIDATION
    assert "PNG" in excinfo.value.message and "WebP" in excinfo.value.message
    assert not attachments_root.exists() or not any(attachments_root.rglob("*"))
    assert await svc.list_attachments(async_db_session, task.id) == []


@pytest.mark.asyncio
async def test_the_per_file_cap_is_five_mebibytes(async_db_session, attachments_root):
    from app.core.errors import E_VALIDATION, PolsiaError
    from app.services import task_attachment_service as svc

    assert svc.MAX_UPLOAD_BYTES == 5 * 1024 * 1024
    task = await _seed_task(async_db_session)
    with pytest.raises(PolsiaError) as excinfo:
        await svc.save_attachment(
            async_db_session, task_id=task.id, filename="huge.png",
            data=png_of_size(svc.MAX_UPLOAD_BYTES + 1),
        )
    assert excinfo.value.code == E_VALIDATION
    assert "5" in excinfo.value.message and "MB" in excinfo.value.message
    assert await svc.list_attachments(async_db_session, task.id) == []


@pytest.mark.asyncio
async def test_the_four_per_task_cap_is_refused_by_name(async_db_session, attachments_root):
    from app.core.errors import E_VALIDATION, PolsiaError
    from app.services import task_attachment_service as svc

    task = await _seed_task(async_db_session)
    for index in range(svc.MAX_PER_TASK):
        await svc.save_attachment(
            async_db_session, task_id=task.id, filename=f"{index}.png", data=PNG_1PX
        )
    with pytest.raises(PolsiaError) as excinfo:
        await svc.save_attachment(
            async_db_session, task_id=task.id, filename="fifth.png", data=PNG_1PX
        )
    assert excinfo.value.code == E_VALIDATION
    assert excinfo.value.context["limit"] == svc.MAX_PER_TASK
    assert len(await svc.list_attachments(async_db_session, task.id)) == svc.MAX_PER_TASK


@pytest.mark.asyncio
async def test_the_per_task_total_byte_cap_is_refused_by_name(
    async_db_session, attachments_root, monkeypatch
):
    from app.core.errors import E_VALIDATION, PolsiaError
    from app.services import task_attachment_service as svc

    monkeypatch.setattr(svc, "MAX_TASK_TOTAL_BYTES", 4096)
    task = await _seed_task(async_db_session)
    await svc.save_attachment(
        async_db_session, task_id=task.id, filename="a.png", data=png_of_size(3000)
    )
    with pytest.raises(PolsiaError) as excinfo:
        await svc.save_attachment(
            async_db_session, task_id=task.id, filename="b.png", data=png_of_size(2000)
        )
    assert excinfo.value.code == E_VALIDATION
    assert excinfo.value.context["limit_bytes"] == 4096
    assert len(await svc.list_attachments(async_db_session, task.id)) == 1


@pytest.mark.asyncio
async def test_a_stored_name_with_separators_can_never_escape_the_task_directory(
    async_db_session, attachments_root
):
    from app.core.errors import E_NOT_FOUND, PolsiaError
    from app.models.task import TaskAttachment
    from app.services import task_attachment_service as svc

    task = await _seed_task(async_db_session)
    row = TaskAttachment(
        task_id=task.id, filename="x.png", stored_name="../../escape.png",
        mime="image/png", size_bytes=10, sha256="0" * 64,
    )
    async_db_session.add(row)
    await async_db_session.commit()
    with pytest.raises(PolsiaError) as excinfo:
        svc.attachment_path(row)
    assert excinfo.value.code == E_NOT_FOUND


# ─────────────────────────────────────────────────────────────────────────────────────────────
# C. API — the four routes
# ─────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_upload_persists_the_row_and_answers_201(api_client, async_db_session, attachments_root):
    from app.services import task_attachment_service as svc

    task = await _seed_task(async_db_session)
    response = await _upload(api_client, task.id, PNG_1PX, "screen shot.png")

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["task_id"] == task.id
    assert body["filename"] == "screen shot.png"
    assert body["mime"] == "image/png"
    assert body["size_bytes"] == len(PNG_1PX)
    assert body["sha256"] == hashlib.sha256(PNG_1PX).hexdigest()
    assert "stored_name" not in body, "the server's storage name is not part of the public row"
    rows = await svc.list_attachments(async_db_session, task.id)
    assert [r.id for r in rows] == [body["id"]]
    assert svc.attachment_path(rows[0]).read_bytes() == PNG_1PX


@pytest.mark.asyncio
async def test_upload_to_an_unknown_task_is_the_named_404(api_client, attachments_root):
    response = await _upload(api_client, 999999)

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "E_NOT_FOUND"
    assert response.json()["error"]["context"]["task_id"] == 999999


@pytest.mark.asyncio
async def test_upload_of_a_non_image_answers_the_named_422(api_client, async_db_session, attachments_root):
    task = await _seed_task(async_db_session)
    response = await _upload(api_client, task.id, GIF, "dancing.png", "image/png")

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["error"]["code"] == "E_VALIDATION"
    assert "PNG" in body["error"]["message"] and "WebP" in body["error"]["message"]
    assert response.headers.get("X-Polsia-Error-Code") == "E_VALIDATION"


@pytest.mark.asyncio
async def test_list_returns_the_metadata_in_upload_order_without_blob_payloads(
    api_client, async_db_session, attachments_root
):
    task = await _seed_task(async_db_session)
    first = await _upload(api_client, task.id, PNG_1PX, "one.png")
    second = await _upload(api_client, task.id, JPEG, "two.jpg", "image/jpeg")
    assert first.status_code == 201 and second.status_code == 201

    response = await api_client.get(f"/api/v1/tasks/{task.id}/attachments", headers=HEADERS)

    assert response.status_code == 200, response.text
    rows = response.json()
    assert [row["filename"] for row in rows] == ["one.png", "two.jpg"]
    assert [row["mime"] for row in rows] == ["image/png", "image/jpeg"]
    assert "data_uri" not in json.dumps(rows), "the list must not inline the image bytes"


@pytest.mark.asyncio
async def test_list_of_an_unknown_task_is_404_and_an_empty_task_is_an_honest_empty_list(
    api_client, async_db_session, attachments_root
):
    missing = await api_client.get("/api/v1/tasks/999999/attachments", headers=HEADERS)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "E_NOT_FOUND"

    task = await _seed_task(async_db_session)
    empty = await api_client.get(f"/api/v1/tasks/{task.id}/attachments", headers=HEADERS)
    assert empty.status_code == 200
    assert empty.json() == [], "no attachments is a measured empty list, never a fabricated row"


@pytest.mark.asyncio
async def test_get_one_serves_the_exact_bytes_and_content_type(api_client, async_db_session, attachments_root):
    task = await _seed_task(async_db_session)
    created = (await _upload(api_client, task.id, JPEG, "photo.jpg", "image/jpeg")).json()

    response = await api_client.get(f"/api/v1/attachments/{created['id']}", headers=HEADERS)

    assert response.status_code == 200, response.text
    assert response.content == JPEG
    assert response.headers["content-type"] == "image/jpeg"
    assert "inline" in response.headers.get("content-disposition", "")


@pytest.mark.asyncio
async def test_get_one_of_an_unknown_attachment_is_the_named_404(api_client, attachments_root):
    response = await api_client.get("/api/v1/attachments/424242", headers=HEADERS)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "E_NOT_FOUND"


@pytest.mark.asyncio
async def test_a_row_whose_blob_vanished_is_a_named_upstream_refusal(
    api_client, async_db_session, attachments_root
):
    from app.services import task_attachment_service as svc

    task = await _seed_task(async_db_session)
    created = (await _upload(api_client, task.id, PNG_1PX)).json()
    rows = await svc.list_attachments(async_db_session, task.id)
    svc.attachment_path(rows[0]).unlink()

    response = await api_client.get(f"/api/v1/attachments/{created['id']}", headers=HEADERS)

    assert response.status_code == 502, response.text
    assert response.json()["error"]["code"] == "E_UPSTREAM"
    assert response.json()["error"]["how_to_fix"], "an upstream refusal must say how to fix it"


@pytest.mark.asyncio
async def test_delete_removes_the_row_and_the_file_and_then_404s(api_client, async_db_session, attachments_root):
    from app.services import task_attachment_service as svc

    task = await _seed_task(async_db_session)
    created = (await _upload(api_client, task.id, PNG_1PX)).json()
    rows = await svc.list_attachments(async_db_session, task.id)
    path = svc.attachment_path(rows[0])

    response = await api_client.delete(f"/api/v1/attachments/{created['id']}", headers=HEADERS)

    assert response.status_code == 204, response.text
    assert not path.exists(), "the blob must be removed with the row"
    assert await svc.list_attachments(async_db_session, task.id) == []
    again = await api_client.delete(f"/api/v1/attachments/{created['id']}", headers=HEADERS)
    assert again.status_code == 404
    assert again.json()["error"]["code"] == "E_NOT_FOUND"


@pytest.mark.asyncio
async def test_every_route_requires_the_owner_key(api_client, async_db_session, attachments_root):
    task = await _seed_task(async_db_session)
    created = (await _upload(api_client, task.id)).json()

    unauthenticated = [
        await api_client.post(
            f"/api/v1/tasks/{task.id}/attachments",
            files={"file": ("a.png", PNG_1PX, "image/png")},
        ),
        await api_client.get(f"/api/v1/tasks/{task.id}/attachments"),
        await api_client.get(f"/api/v1/attachments/{created['id']}"),
        await api_client.delete(f"/api/v1/attachments/{created['id']}"),
    ]
    for response in unauthenticated:
        assert response.status_code == 401, response.text
        assert response.json()["error"]["code"] == "E_AUTH"
        assert response.headers.get("X-Polsia-Error-Code") == "E_AUTH"


# ─────────────────────────────────────────────────────────────────────────────────────────────
# D. RUNTIME — content parts, caps, task_title seam
# ─────────────────────────────────────────────────────────────────────────────────────────────


class _Agent:
    """The runtime only needs an agent TYPE for the tool allowlist check."""

    agent_type = "test"


def _inv(prompt: str = "what is in this image?", resume: str | None = None):
    from app.agents import claude_cli

    return claude_cli.build_invocation(
        prompt=prompt, resume_session=resume, timeout_s=30, schema_name="finance"
    )


def _completion(content: str = "ok") -> dict:
    return {
        "id": "chat-1",
        "object": "chat.completion",
        "model": "deepseek-flash",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2,
                  "prompt_cache_hit_tokens": 0},
    }


class _Wire:
    """A capture seam: every request body the runtime sends, answered with one completion."""

    def __init__(self, content: str = "ok"):
        self.bodies: list[dict] = []
        self.content = content

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        return httpx.Response(200, json=_completion(self.content))

    @property
    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


@pytest.fixture(autouse=True)
def _clean_sessions():
    from app.agents import deepseek_runtime

    deepseek_runtime._reset_session_store()
    yield
    deepseek_runtime._reset_session_store()


def test_without_images_the_user_message_is_the_same_plain_string(monkeypatch):
    """The byte-identical requirement: a task with no images changes NOTHING on the wire."""
    from app.agents import deepseek_runtime

    monkeypatch.setattr(deepseek_runtime, "_resolve_api_key", lambda: "sk-test")
    wire = _Wire()

    result = deepseek_runtime.invoke(_inv("do the thing"), client=wire.client, api_key="sk-test")

    assert json.loads(result.stdout)["is_error"] is False
    messages = wire.bodies[0]["messages"]
    assert messages[-1] == {"role": "user", "content": "do the thing"}
    assert isinstance(messages[-1]["content"], str)


def test_with_images_the_user_message_becomes_text_plus_image_url_parts(monkeypatch):
    from app.agents import deepseek_runtime

    monkeypatch.setattr(deepseek_runtime, "_resolve_api_key", lambda: "sk-test")
    wire = _Wire()
    data_uri = "data:image/png;base64," + base64.b64encode(PNG_1PX).decode("ascii")
    images = [{"mime": "image/png", "data_uri": data_uri, "size_bytes": len(PNG_1PX)}]

    deepseek_runtime.invoke(
        _inv("what is in this image?"), client=wire.client, api_key="sk-test", images=images
    )

    content = wire.bodies[0]["messages"][-1]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "what is in this image?"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"] == data_uri
    # the data URI is the real bytes, base64-encoded once
    assert base64.b64decode(content[1]["image_url"]["url"].split(",", 1)[1]) == PNG_1PX


def test_images_over_the_total_payload_cap_refuse_by_name_before_any_http(monkeypatch):
    from app.agents import deepseek_runtime
    from app.core.errors import E_VALIDATION, PolsiaError

    monkeypatch.setattr(deepseek_runtime, "_resolve_api_key", lambda: "sk-test")
    monkeypatch.setattr(deepseek_runtime, "VISION_MAX_TOTAL_BYTES", 64)
    wire = _Wire()
    data_uri = "data:image/png;base64," + base64.b64encode(PNG_1PX).decode("ascii")

    with pytest.raises(PolsiaError) as excinfo:
        deepseek_runtime.invoke(
            _inv(), client=wire.client, api_key="sk-test",
            images=[{"mime": "image/png", "data_uri": data_uri}],
        )
    assert excinfo.value.code == E_VALIDATION
    assert excinfo.value.context["limit_bytes"] == 64
    assert wire.bodies == [], "a payload the model could never accept must not be sent"


def test_an_image_part_with_an_unsupported_mime_or_a_malformed_data_uri_is_refused(monkeypatch):
    from app.agents import deepseek_runtime
    from app.core.errors import E_VALIDATION, PolsiaError

    monkeypatch.setattr(deepseek_runtime, "_resolve_api_key", lambda: "sk-test")
    wire = _Wire()

    for bad in (
        {"mime": "image/gif", "data_uri": "data:image/gif;base64,R0lGOD"},
        {"mime": "image/png", "data_uri": "https://example.com/a.png"},
        {"mime": "image/png", "data_uri": "data:image/png;base64"},
    ):
        with pytest.raises(PolsiaError) as excinfo:
            deepseek_runtime.invoke(_inv(), client=wire.client, api_key="sk-test", images=[bad])
        assert excinfo.value.code == E_VALIDATION
    assert wire.bodies == []


def test_the_task_title_reaches_execute_tool_through_the_runtime_seam(monkeypatch):
    from app.agents import deepseek_runtime, tool_registry

    seen: list[dict] = []

    def fake_execute(name, arguments, *, agent_type, task_title=None):
        seen.append({"name": name, "agent_type": agent_type, "task_title": task_title})
        return json.dumps({"ok": True})

    monkeypatch.setattr(tool_registry, "execute_tool", fake_execute)
    call = {"id": "call_1", "type": "function",
            "function": {"name": "web_search", "arguments": '{"query":"pricing"}'}}

    messages, reached = deepseek_runtime._execute_tool_calls(
        [call], agent_type="business_planning", url="http://test",
        task_title="Launch the pricing page",
    )

    assert reached == ["web_search"]
    assert seen[0]["task_title"] == "Launch the pricing page"
    assert messages[0]["role"] == "tool" and messages[0]["tool_call_id"] == "call_1"


def test_the_task_title_kwarg_is_omitted_when_the_run_has_no_title(monkeypatch):
    """A tool-less run (and every pre-V25 caller) must invoke `execute_tool` with the OLD signature."""
    from app.agents import deepseek_runtime, tool_registry

    seen: list[dict] = []

    def fake_execute(name, arguments, *, agent_type):  # NOTE: no task_title parameter
        seen.append({"name": name, "agent_type": agent_type})
        return json.dumps({"ok": True})

    monkeypatch.setattr(tool_registry, "execute_tool", fake_execute)
    call = {"id": "call_1", "type": "function",
            "function": {"name": "web_search", "arguments": "{}"}}

    messages, _ = deepseek_runtime._execute_tool_calls(
        [call], agent_type="business_planning", url="http://test", task_title=None
    )
    assert seen == [{"name": "web_search", "agent_type": "business_planning"}]
    assert json.loads(messages[0]["content"]) == {"ok": True}


def test_a_replay_does_not_send_the_images_a_second_time(monkeypatch):
    from app.agents import deepseek_runtime

    monkeypatch.setattr(deepseek_runtime, "_resolve_api_key", lambda: "sk-test")
    wire = _Wire()
    data_uri = "data:image/png;base64," + base64.b64encode(PNG_1PX).decode("ascii")
    images = [{"mime": "image/png", "data_uri": data_uri}]

    first = deepseek_runtime.invoke(
        _inv("look"), client=wire.client, api_key="sk-test", images=images
    )
    session_id = json.loads(first.stdout)["session_id"]

    deepseek_runtime.invoke(
        _inv("repair this", resume=session_id), client=wire.client, api_key="sk-test",
        images=images,
    )

    second_messages = wire.bodies[1]["messages"]
    assert isinstance(second_messages[-1]["content"], str), (
        "on a replay the image already rides the stored exchange; re-sending it would double the "
        "payload and duplicate the conversation"
    )
    list_parts = [m for m in second_messages if isinstance(m.get("content"), list)]
    assert len(list_parts) == 1, "exactly the replayed first turn carries the image"


def test_the_session_store_is_bounded_by_bytes_as_well_as_by_count(monkeypatch):
    from app.agents import deepseek_runtime

    monkeypatch.setattr(deepseek_runtime, "SESSION_STORE_MAX_BYTES", 5000)
    big = "x" * 4000
    deepseek_runtime._store_exchange("s1", [{"role": "user", "content": big}], "a")
    deepseek_runtime._store_exchange("s2", [{"role": "user", "content": big}], "b")

    assert "s2" in deepseek_runtime._SESSIONS
    assert "s1" not in deepseek_runtime._SESSIONS, (
        "an image-bearing exchange can be megabytes; the process-local store needs a byte budget"
    )


# ─────────────────────────────────────────────────────────────────────────────────────────────
# E. THE AGENT SEAM — task title + attachment payloads reach the runtime
# ─────────────────────────────────────────────────────────────────────────────────────────────


def test_attachment_refs_and_image_parts_carry_the_stored_bytes(attachments_root):
    from app.services import task_attachment_service as svc

    task_dir = attachments_root / "task-7"
    task_dir.mkdir(parents=True)
    (task_dir / "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.png").write_bytes(PNG_1PX)
    refs = [{"id": 1, "task_id": 7, "filename": "shot.png", "mime": "image/png",
             "stored_name": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.png", "size_bytes": len(PNG_1PX)}]

    parts = svc.image_parts(refs)

    assert parts[0]["mime"] == "image/png"
    assert parts[0]["size_bytes"] == len(PNG_1PX)
    assert base64.b64decode(parts[0]["data_uri"].split(",", 1)[1]) == PNG_1PX


def test_image_parts_refuse_a_file_whose_bytes_are_not_the_recorded_type(attachments_root):
    from app.core.errors import E_UPSTREAM, PolsiaError
    from app.services import task_attachment_service as svc

    task_dir = attachments_root / "task-7"
    task_dir.mkdir(parents=True)
    (task_dir / "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.png").write_bytes(b"GIF89a-not-really-a-png")
    refs = [{"id": 1, "task_id": 7, "filename": "shot.png", "mime": "image/png",
             "stored_name": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.png"}]

    with pytest.raises(PolsiaError) as excinfo:
        svc.image_parts(refs)
    assert excinfo.value.code == E_UPSTREAM


def test_image_parts_enforce_the_per_task_total_cap_before_building_a_payload(
    attachments_root, monkeypatch
):
    from app.core.errors import E_VALIDATION, PolsiaError
    from app.services import task_attachment_service as svc

    monkeypatch.setattr(svc, "MAX_TASK_TOTAL_BYTES", 64)
    task_dir = attachments_root / "task-7"
    task_dir.mkdir(parents=True)
    (task_dir / "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.png").write_bytes(PNG_1PX)
    refs = [{"id": 1, "task_id": 7, "filename": "shot.png", "mime": "image/png",
             "stored_name": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.png"}]

    with pytest.raises(PolsiaError) as excinfo:
        svc.image_parts(refs)
    assert excinfo.value.code == E_VALIDATION
    assert excinfo.value.context["limit_bytes"] == 64


def test_set_run_context_publishes_the_task_title_and_attachment_refs(attachments_root):
    from app.agents.base_agent import BasePolsiaAgent

    class _Probe(BasePolsiaAgent):
        agent_type = "finance"

        def run(self, task, context):  # pragma: no cover
            return {"summary": "x"}

    task_dir = attachments_root / "task-7"
    task_dir.mkdir(parents=True)
    (task_dir / ("b" * 32 + ".png")).write_bytes(PNG_1PX)

    probe = _Probe()
    probe.set_run_context({
        "title": "  Launch the pricing page  ",
        "attachments": [{"id": 1, "task_id": 7, "mime": "image/png",
                         "stored_name": "b" * 32 + ".png", "filename": "a.png"}],
    })

    assert probe.task_title == "Launch the pricing page", "the title is stripped once, at the seam"
    assert probe.task_attachment_refs[0]["id"] == 1
    assert len(probe.task_images) == 1 and probe.task_images[0]["mime"] == "image/png"
    probe.set_run_context({})
    assert probe.task_title == "" and probe.task_attachment_refs == []
    assert probe.task_images == []


def test_a_run_with_images_and_no_deepseek_runtime_refuses_by_name(monkeypatch, attachments_root):
    """The CLI path cannot carry images: answering without them would silently drop the input."""
    from app.agents import base_agent, deepseek_runtime
    from app.core.errors import E_CONFIG, PolsiaError

    class _Probe(base_agent.BasePolsiaAgent):
        agent_type = "finance"

        def run(self, task, context):  # pragma: no cover
            return {"summary": "x"}

    task_dir = attachments_root / "task-9"
    task_dir.mkdir(parents=True)
    (task_dir / ("b" * 32 + ".png")).write_bytes(PNG_1PX)
    monkeypatch.setattr(deepseek_runtime, "available", lambda: False)

    probe = _Probe()
    probe.set_run_context({
        "title": "Read the screenshot",
        "attachments": [{"id": 1, "task_id": 9, "mime": "image/png",
                         "stored_name": "b" * 32 + ".png", "filename": "a.png"}],
    })
    with pytest.raises(PolsiaError) as excinfo:
        probe._real_call("hello")
    assert excinfo.value.code == E_CONFIG
    assert "WITHOUT seeing them" in excinfo.value.message


def test_real_call_hands_the_title_and_the_images_to_the_runtime(monkeypatch, attachments_root):
    """The seam the whole lane hangs on: `_real_call` -> `runtime.invoke(task_title=, images=)`."""
    from app.agents import base_agent, claude_cli, deepseek_runtime

    class _Probe(base_agent.BasePolsiaAgent):
        agent_type = "finance"

        def run(self, task, context):  # pragma: no cover
            return {"summary": "x"}

    task_dir = attachments_root / "task-9"
    task_dir.mkdir(parents=True)
    (task_dir / "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.png").write_bytes(PNG_1PX)

    captured: dict = {}

    def fake_invoke(inv, **kwargs):
        captured.update(kwargs)
        return claude_cli.InvokeResult(
            stdout=json.dumps({"type": "result", "subtype": "success", "is_error": False,
                               "result": "{}", "usage": {}}),
            stderr="", exit_code=0, wall_seconds=0.0, transport="http",
        )

    monkeypatch.setattr(deepseek_runtime, "available", lambda: True)
    monkeypatch.setattr(deepseek_runtime, "invoke", fake_invoke)
    import app.agents.tool_registry as registry

    monkeypatch.setattr(registry, "tools_for", lambda agent_type: ())

    probe = _Probe()
    probe.set_run_context({
        "title": "Read the screenshot",
        "attachments": [{"id": 1, "task_id": 9, "mime": "image/png",
                         "stored_name": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.png", "filename": "a.png"}],
    })
    probe._real_call("hello")

    assert captured["task_title"] == "Read the screenshot"
    assert len(captured["images"]) == 1
    assert captured["images"][0]["mime"] == "image/png"
    assert base64.b64decode(captured["images"][0]["data_uri"].split(",", 1)[1]) == PNG_1PX
