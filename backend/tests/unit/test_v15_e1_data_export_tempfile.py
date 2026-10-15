"""V15 / E1 — 2R-04 (U2-10): the export ZIP is FILE-BACKED, not a full in-memory payload.

RED-first (wave-v15/BRIEFS.md §COMMON RULES): on the pre-fix tree `GET /api/v1/data/export`
returns a `StreamingResponse` over an already-materialized `bytes` payload
(`app/api/v1/data.py:272-277/:287-294` at b17d7b0) — no file exists, nothing is cleaned up.

The pin proves, against the REAL route function and the REAL ASGI app:
  * the route answers with a `FileResponse` whose `.path` is a REAL zip file on disk
    (built in the system scratch dir under a named prefix — the fix's whole point);
  * the served archive is member-identical to the pre-fix contract: MANIFEST.json with the
    same row counts + the same 8 members (counts from the SAME seeded rows);
  * `X-Polsia-Row-Count` + `Content-Disposition` are unchanged;
  * the temp file is REMOVED after the response completes (the background cleanup), so a
    server that keeps exporting cannot leak scratch files;
  * the HTTP leg still serves the same bytes (ZipFile-readable) — the regression face.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from app.api.v1 import data as data_module

CSV_MEMBERS = (
    "tasks.csv", "agent_runs.csv", "activity_log.csv", "revenue_snapshots.csv",
    "invoices.csv", "task_schedules.csv",
)
JSON_MEMBERS = ("memory_entries.json", "chat_messages.json")
ALL_MEMBERS = CSV_MEMBERS + JSON_MEMBERS

#: Enough rows that the in-memory build is a real payload (not a trivial two-byte zip).
BIG_N = 250
BIG_TITLE = "V15 2R-04 export size probe — " + ("x" * 180)


async def _seed_big(db) -> dict[str, int]:
    from app.models.chat import ChatMessage
    from app.models.task import Task

    for i in range(BIG_N):
        db.add(Task(title=f"{BIG_TITLE} {i}", agent_type="finance", source="manual"))
    for i in range(4):
        db.add(ChatMessage(role="owner", content=f"seed chat {i}"))
    await db.flush()
    counts = {name: 0 for name in (n[:-4] for n in CSV_MEMBERS + JSON_MEMBERS)}
    counts["tasks"] = BIG_N
    counts["chat_messages"] = 4
    return counts


@pytest.mark.asyncio
async def test_export_route_returns_a_file_backed_response_and_cleans_up(async_db_session):
    """THE 2R-04 discriminator: a real temp file on disk, removed after the send completes."""
    counts = await _seed_big(async_db_session)

    resp = await data_module.export_data_route(db=async_db_session)

    # Pre-fix this is a StreamingResponse (no `.path`): the pin fails HERE, by name.
    assert isinstance(resp, data_module.FileResponse), (
        f"2R-04 not fixed: export answered {type(resp).__name__}, not FileResponse — the ZIP "
        "is still built as one in-memory payload"
    )

    path = Path(resp.path)
    assert path.exists(), "the FileResponse path must be a REAL file while the response is live"
    assert path.name.startswith("polsia-data-export-") and path.suffix == ".zip", path.name
    assert resp.media_type == "application/zip"
    assert resp.headers["X-Polsia-Row-Count"] == str(sum(counts.values()))
    assert "attachment; filename=" in resp.headers["Content-Disposition"]

    # The file on disk IS the contracted archive: MANIFEST + 8 members, counts from these rows.
    with zipfile.ZipFile(path) as zf:
        assert set(zf.namelist()) == {"MANIFEST.json", *ALL_MEMBERS}
        import json

        manifest = json.loads(zf.read("MANIFEST.json"))
        by_name = {member["name"]: member for member in manifest["members"]}
        assert by_name["tasks.csv"]["row_count"] == counts["tasks"]
        assert by_name["chat_messages.json"]["row_count"] == counts["chat_messages"]
        assert manifest["total_rows"] == sum(counts.values())

    # Cleanup: the response carries a background task and running it removes the temp file.
    assert resp.background is not None, "no cleanup background task — the temp file would leak"
    await resp.background()
    assert not path.exists(), f"temp file leaked after completion: {path}"


@pytest.mark.asyncio
async def test_export_http_leg_still_serves_the_same_archive(api_client, auth_headers, async_db_session):
    """The regression face: unchanged headers, unchanged member set, ZipFile-readable bytes."""
    import io
    import json

    counts = await _seed_big(async_db_session)
    resp = await api_client.get("/api/v1/data/export", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/zip"
    assert resp.headers["X-Polsia-Row-Count"] == str(sum(counts.values()))
    assert "attachment; filename=" in resp.headers["content-disposition"]

    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    assert set(zf.namelist()) == {"MANIFEST.json", *ALL_MEMBERS}
    manifest = json.loads(zf.read("MANIFEST.json"))
    assert manifest["total_rows"] == sum(counts.values())


@pytest.mark.asyncio
async def test_temp_file_is_removed_even_when_the_send_raises(async_db_session):
    """HOSTILE: a client disconnect mid-body must not leak the temp archive.

    starlette 0.41.3 runs `FileResponse.background` only on the NORMAL completion path
    (measured in the installed source), so the route answers with `ExportFileResponse`, which
    runs the cleanup in a `finally`.  Driven at the ASGI boundary: `send` raises on the first
    body message (a disconnected client) and the file must still be gone afterwards.
    """
    await _seed_big(async_db_session)
    resp = await data_module.export_data_route(db=async_db_session)
    path = Path(resp.path)
    assert path.exists()

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/data/export",
        "headers": [],
        "query_string": b"",
        "http_version": "1.1",
    }

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        if message["type"] == "http.response.body":
            raise RuntimeError("client disconnected")

    with pytest.raises(RuntimeError, match="client disconnected"):
        await resp(scope, receive, send)

    assert not path.exists(), "the temp file leaked on a failed send (client disconnect)"
