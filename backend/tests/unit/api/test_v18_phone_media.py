"""WAVE V18 / lane L2 — the frozen phone MEDIA surface + the V-12 non-object-body audit fix.

RED-FIRST: this module is written before `app/services/phone_media_service.py` and before the two
`/media` routes exist.  At HEAD every pin here fails — `_media_service()` raises
`ModuleNotFoundError` (naming the missing module) and the two media URLs answer 404 from the
router — and the fix is the implementation, never the test.

FROZEN INTERFACE (wave-v18/V18-PLAN.md)
    GET /api/v1/phone/media?thread_id=<int>
        -> 200 {"ok": true, "data": {"parts": [{part_id, content_type, size_bytes, when,
           thread_id}], "truncated": bool}}     cap 50 newest; `truncated` NAMES the cap.
    GET /api/v1/phone/media/{part_id}
        -> 200 BINARY; Content-Type from the part row (fallback application/octet-stream);
           Content-Disposition: attachment; filename="phone-part-<id>.<ext>";
           404 E_NOT_FOUND; 422 E_VALIDATION for a part over the documented size cap;
           503 E_PHONE_UNAVAILABLE when the store is absent (and when a row LIES about its size —
           a corrupt store is an unavailable store, not a truncated download).
    Both key-auth (the router dependency); the envelope exists on the JSON route only.
    V-12: POST /api/v1/phone/send accepts `payload: Any`; a NON-OBJECT body still writes exactly
    ONE `activity_log` row (code E_VALIDATION, empty/masked target) and answers the coded 422 —
    identical to every other validation refusal in the product.

THE FIXTURE STORE IS THE MEASURED SCHEMA (RECON, 2026-10-18, read-only dump in the lane RECEIPT)
    `mms_part(part_id PK, message_id, sequence_num, content_id, content_type, text, name,
    charset, blob, size)`; `mms(message_id PK, thread_id, timestamp, ...)`; the thread join is
    `mms_part.message_id -> mms.message_id -> mms.thread_id` and resolved 1844/1844 blob-bearing
    parts on the real store (0 orphans), so a thread->parts join IS supported.  Timestamps are
    FILETIME ticks on `mms.timestamp` (parts carry none).  Blobs are stored RAW (JPEG `ffd8ffdb`,
    PNG `89504e47`, audio `00000018` magic on the real store — no compression layer) and
    `size == length(blob)` on every one of the 1844 rows.
    The REAL MEDIA PREDICATE is therefore `blob IS NOT NULL AND size > 0`: the store keeps MMS
    text/SMIL parts as ZERO-LENGTH blobs (1670 of 1844 blob rows on this machine), which are
    message bodies, not attachments.

TEST FIXTURES, LABELLED
    Every store below is a SYNTHETIC fixture written by `tests/fixtures/phone_store.build_phone_store`
    (the shipped measured-DDL builder) plus REAL byte payloads added through plain sqlite.  No test
    in this file touches the owner's live Phone Link database and no test asserts against a mock of
    the code under test: the service, the SQL, the ASGI stack and the audit write are all real.
    The only replaced boundary is the documented `MEDIA_MAX_BYTES` seam in the one cap pin (the
    alternative — writing a real 25 MiB blob — proves the same branch for 25 MiB of disk churn).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import settings
from app.core.errors import E_NOT_FOUND, E_PHONE_UNAVAILABLE, E_VALIDATION
from tests.fixtures.phone_store import (
    THREAD_DANA,
    THREAD_MITCH,
    THREAD_STRANGER,
    build_phone_store,
    filetime,
)

MEDIA_URL = "/api/v1/phone/media"
SEND_URL = "/api/v1/phone/send"
NOW = datetime(2026, 10, 18, 12, 0, 0)

#: Real container headers + payloads, so "the bytes are the bytes" is a byte-equal claim.
JPEG_BYTES = b"\xff\xd8\xff\xdb" + bytes(range(256)) * 2 + b"\xff\xd9"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 8
AUDIO_BYTES = b"\x00\x00\x00\x18ftypmp42" + bytes(range(200))
GIF_BYTES = b"GIF89a" + bytes(range(64))
UNTYPED_BYTES = b"\x00\x01\x02\x03binary-without-a-content-type\xfe\xff"


def _media_service():
    """Import the subject LAZILY so the RED leg fails HERE, naming the missing module."""
    from app.services import phone_media_service as service

    return service


def _part(
    part_id: int,
    message_id: int,
    thread_id: int,
    content_type: str | None,
    blob: bytes | None,
    minute: int,
    *,
    size: int | None = None,
) -> dict:
    """One `mms_part` spec; `minute` is minutes-before-NOW on the owning `mms` envelope."""
    return {
        "part_id": part_id,
        "message_id": message_id,
        "thread_id": thread_id,
        "content_type": content_type,
        "blob": blob,
        "size": (len(blob) if blob is not None else 0) if size is None else size,
        "minute": minute,
    }


#: The main fixture: media, MMS body parts (zero-length blobs), a blob-less row and an untyped row.
MAIN_PARTS = [
    _part(2001, 201, THREAD_MITCH, "image/jpeg", JPEG_BYTES, 40),
    _part(2002, 201, THREAD_MITCH, "text/plain", b"", 40),          # MMS body, not media
    _part(2003, 202, THREAD_MITCH, "image/png", PNG_BYTES, 30),
    _part(2004, 202, THREAD_MITCH, "application/smil", b"", 30),    # SMIL body, not media
    _part(2005, 203, THREAD_MITCH, "audio/mp4", AUDIO_BYTES, 20),
    _part(2006, 204, THREAD_MITCH, "image/jpeg", None, 10, size=1234),  # no bytes at all
    _part(2007, 205, THREAD_MITCH, None, UNTYPED_BYTES, 5),         # no content_type -> fallback
    _part(2010, 206, THREAD_DANA, "image/gif", GIF_BYTES, 15),
]


def _build(dest: Path, parts: list[dict], *, now: datetime = NOW) -> Path:
    """A COMPLETE measured store (the shipped builder) + the caller's REAL `mms_part` rows.

    `build_phone_store` is used unchanged: its own `mms_part` rows carry NULL blobs (they are
    message-body fixtures), so they are invisible to the media predicate — which is itself pinned
    below (thread 13 must list zero media parts, not the fake `size=2048` row).
    """
    root = build_phone_store(dest, now=now)
    con = sqlite3.connect(root / "phone.db")
    try:
        envelopes: dict[tuple[int, int], int] = {}
        for spec in parts:
            envelopes.setdefault((spec["message_id"], spec["thread_id"]), spec["minute"])
        con.executemany(
            'INSERT INTO "mms"(message_id, thread_id, status, type, subscription_id, subject, '
            "charset, timestamp, pc_status, from_address, checksum, to_addresses) "
            "VALUES (?,?,0,1,-1,NULL,106,?,1,NULL,0,NULL)",
            [
                (mid, tid, filetime(now - timedelta(minutes=minute)))
                for (mid, tid), minute in envelopes.items()
            ],
        )
        con.executemany(
            'INSERT INTO "mms_part"(part_id, message_id, sequence_num, content_id, content_type, '
            "text, name, charset, blob, size) VALUES (?,?,0,NULL,?,NULL,?,106,?,?)",
            [
                (
                    spec["part_id"],
                    spec["message_id"],
                    spec["content_type"],
                    f"part-{spec['part_id']}.bin",
                    spec["blob"],
                    spec["size"],
                )
                for spec in parts
            ],
        )
        con.commit()
    finally:
        con.close()
    return root


@pytest.fixture
def media_store(tmp_path, monkeypatch):
    """The main fixture store, wired as the process's Phone Link store."""
    root = _build(tmp_path / "Database", MAIN_PARTS)
    monkeypatch.setattr(settings, "phone_link_db_dir", str(root))
    return root


def _list(client, headers, thread_id: int, **params):
    return client.get(MEDIA_URL, headers=headers, params={"thread_id": thread_id, **params})


def _stream(client, headers, part_id: int):
    return client.get(f"{MEDIA_URL}/{part_id}", headers=headers)


# ── the list route ──────────────────────────────────────────────────────────────────────────────


async def test_list_returns_only_real_media_parts_newest_first(api_client, auth_headers, media_store):
    """Shape, predicate and ordering in one pin: the frozen five keys, media only, newest first.

    The zero-length MMS body parts (2002 text/plain, 2004 SMIL) and the blob-less row (2006) must
    NOT appear: they are text parts the store keeps with an empty/NULL blob, and listing them would
    offer the UI a "download" that can only 404.
    """
    resp = await _list(api_client, auth_headers, THREAD_MITCH)

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    data = body["data"]
    assert set(data) == {"parts", "truncated"}
    assert data["truncated"] is False
    assert [p["part_id"] for p in data["parts"]] == [2007, 2005, 2003, 2001]
    for part in data["parts"]:
        assert set(part) == {"part_id", "content_type", "size_bytes", "when", "thread_id"}
        assert part["thread_id"] == THREAD_MITCH
        assert isinstance(part["size_bytes"], int) and part["size_bytes"] > 0
        assert datetime.fromisoformat(part["when"]).tzinfo is not None, (
            "an offset-less `when` is read as UTC by a browser and renders the wrong hour"
        )
    by_id = {p["part_id"]: p for p in data["parts"]}
    assert by_id[2001]["content_type"] == "image/jpeg"
    assert by_id[2001]["size_bytes"] == len(JPEG_BYTES)
    assert by_id[2007]["content_type"] == "application/octet-stream", (
        "a NULL content_type is named by the documented fallback, never an empty header value"
    )


async def test_list_is_scoped_to_its_thread(api_client, auth_headers, media_store):
    """The join is the measured one (`mms_part` -> `mms` -> `thread_id`): one thread, its parts."""
    dana = (await _list(api_client, auth_headers, THREAD_DANA)).json()["data"]
    assert [p["part_id"] for p in dana["parts"]] == [2010]

    stranger = (await _list(api_client, auth_headers, THREAD_STRANGER)).json()["data"]
    assert stranger["parts"] == [], (
        "the builder's thread-13 parts carry NULL blobs (size 2048 is a message-body fixture); "
        "they are not media and must not be listed"
    )
    assert stranger["truncated"] is False


async def test_list_caps_at_fifty_and_says_truncated(api_client, auth_headers, tmp_path, monkeypatch):
    """60 media parts -> the 50 NEWEST, and `truncated: true` names the cap rather than hiding it."""
    parts = [
        _part(3000 + i, 400 + i, THREAD_MITCH, "image/jpeg", JPEG_BYTES, minute=60 - i)
        for i in range(60)
    ]
    root = _build(tmp_path / "Database", parts)
    monkeypatch.setattr(settings, "phone_link_db_dir", str(root))

    data = (await _list(api_client, auth_headers, THREAD_MITCH)).json()["data"]

    assert len(data["parts"]) == 50
    assert data["truncated"] is True
    # minute 60-i: i=59 is the NEWEST (1 minute ago) -> the newest 50 are i=59..10.
    assert [p["part_id"] for p in data["parts"]] == [3059 - n for n in range(50)]


async def test_list_accepts_a_lower_limit_but_never_a_higher_one(api_client, auth_headers, media_store):
    """The cap is a property of the surface: `limit` may narrow the window, never widen it."""
    narrowed_resp = await _list(api_client, auth_headers, THREAD_MITCH, limit=2)
    assert narrowed_resp.status_code == 200
    narrowed = narrowed_resp.json()["data"]
    assert [p["part_id"] for p in narrowed["parts"]] == [2007, 2005]
    assert narrowed["truncated"] is True

    too_wide = await _list(api_client, auth_headers, THREAD_MITCH, limit=51)
    assert too_wide.status_code == 422
    # A Query bound is a FRAMEWORK request-validation refusal, and the product pins that shape as
    # "uncoded by design" (`main.request_validation_error_handler`: `{"detail": [...]}` for every
    # serializable echo — tests/unit/api/test_r26_ads_edges.py).  The cap is still ENFORCED; it is
    # simply not a hand-rolled surface refusal, so it carries no envelope of its own.
    assert "detail" in too_wide.json()
    assert "data" not in too_wide.json()


async def test_list_requires_the_api_key(api_client):
    resp = await api_client.get(MEDIA_URL, params={"thread_id": THREAD_MITCH})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "E_AUTH"


async def test_list_503_when_the_store_is_absent(api_client, auth_headers, tmp_path, monkeypatch):
    """"No mount" is a named 503 — never `200 []`, which is indistinguishable from "no media"."""
    monkeypatch.setattr(settings, "phone_link_db_dir", str(tmp_path / "not-mounted"))

    resp = await _list(api_client, auth_headers, THREAD_MITCH)

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == E_PHONE_UNAVAILABLE
    assert resp.headers["X-Polsia-Error-Code"] == E_PHONE_UNAVAILABLE


async def test_list_503_when_the_media_columns_have_drifted(api_client, auth_headers, tmp_path, monkeypatch):
    """A store that dropped `blob`/`size` is UNAVAILABLE, not a 500 traceback.

    `phone_link_service.REQUIRED_COLUMNS` covers the columns IT reads (message_id, content_type,
    text) — not the two media columns — so the media read must name its own drift.  This is the
    only pin that reaches `phone_media_service._rows`'s `sqlite3.Error` branch; without it a
    Phone Link release that renamed `blob` would surface as an unhandled OperationalError.
    """
    root = _build(tmp_path / "Database", MAIN_PARTS)
    con = sqlite3.connect(root / "phone.db")
    try:
        con.execute('ALTER TABLE "mms_part" RENAME TO "mms_part_old"')
        con.execute(
            'CREATE TABLE "mms_part"("part_id" INTEGER PRIMARY KEY NOT NULL, '
            '"message_id" INTEGER NOT NULL, "content_type" TEXT, "text" TEXT)'
        )
        con.commit()
    finally:
        con.close()
    monkeypatch.setattr(settings, "phone_link_db_dir", str(root))

    resp = await _list(api_client, auth_headers, THREAD_MITCH)

    assert resp.status_code == 503
    body = resp.json()
    assert body["error"]["code"] == E_PHONE_UNAVAILABLE
    assert body["error"]["context"]["table"] == "mms_part"
    assert resp.headers["X-Polsia-Error-Code"] == E_PHONE_UNAVAILABLE


# ── the binary route ───────────────────────────────────────────────────────────────────────────


async def test_stream_returns_the_exact_stored_bytes(api_client, auth_headers, media_store):
    """Byte-equal to the seeded blob; no re-encoding, no truncation, no JSON wrapper."""
    resp = await _stream(api_client, auth_headers, 2001)

    assert resp.status_code == 200
    assert resp.content == JPEG_BYTES
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.headers["content-length"] == str(len(JPEG_BYTES))
    assert "json" not in resp.headers["content-type"]


async def test_stream_names_the_attachment_from_the_row_type(api_client, auth_headers, media_store):
    """`Content-Disposition` comes from the CONTENT TYPE, never from the row's untrusted `name`."""
    jpeg = await _stream(api_client, auth_headers, 2001)
    assert jpeg.headers["content-disposition"] == 'attachment; filename="phone-part-2001.jpg"'

    png = await _stream(api_client, auth_headers, 2003)
    assert png.content == PNG_BYTES
    assert png.headers["content-disposition"] == 'attachment; filename="phone-part-2003.png"'

    audio = await _stream(api_client, auth_headers, 2005)
    assert audio.content == AUDIO_BYTES
    assert audio.headers["content-disposition"] == 'attachment; filename="phone-part-2005.m4a"'


async def test_stream_falls_back_for_an_untyped_part(api_client, auth_headers, media_store):
    """A NULL `content_type` serves as octet-stream + `.bin` — the documented fallback, both places."""
    resp = await _stream(api_client, auth_headers, 2007)

    assert resp.status_code == 200
    assert resp.content == UNTYPED_BYTES
    assert resp.headers["content-type"] == "application/octet-stream"
    assert resp.headers["content-disposition"] == 'attachment; filename="phone-part-2007.bin"'


async def test_stream_404_names_a_missing_part(api_client, auth_headers, media_store):
    resp = await _stream(api_client, auth_headers, 999_999)

    assert resp.status_code == 404
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == E_NOT_FOUND
    assert resp.headers["X-Polsia-Error-Code"] == E_NOT_FOUND


async def test_stream_404_for_a_part_with_no_bytes(api_client, auth_headers, media_store):
    """A text/message part (blob NULL) and a zero-length part are NOT downloadable attachments."""
    for part_id in (2006, 2002):
        resp = await _stream(api_client, auth_headers, part_id)
        assert resp.status_code == 404, f"part {part_id} has no stored bytes"
        assert resp.json()["error"]["code"] == E_NOT_FOUND


async def test_stream_503_when_the_store_is_absent(api_client, auth_headers, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "phone_link_db_dir", str(tmp_path / "not-mounted"))

    resp = await _stream(api_client, auth_headers, 2001)

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == E_PHONE_UNAVAILABLE


async def test_stream_refuses_an_oversized_part_by_name(api_client, auth_headers, media_store, monkeypatch):
    """The stream cap is a named 422, not a silent truncation and not a multi-hundred-MB response."""
    service = _media_service()
    monkeypatch.setattr(service, "MEDIA_MAX_BYTES", 1024)

    resp = await _stream(api_client, auth_headers, 2003)  # PNG_BYTES is 2048 bytes

    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == E_VALIDATION
    assert resp.headers["X-Polsia-Error-Code"] == E_VALIDATION
    assert body["error"]["context"]["part_id"] == 2003
    assert body["error"]["context"]["max_bytes"] == 1024


def test_the_documented_stream_cap_is_25_mib():
    """Measured max part on the real store is 2,308,674 bytes (~2.2 MiB); the cap is ~11x headroom."""
    assert _media_service().MEDIA_MAX_BYTES == 25 * 1024 * 1024


async def test_stream_refuses_a_row_that_lies_about_its_size(api_client, auth_headers, tmp_path, monkeypatch):
    """Corrupt store: `size` says 9999, the blob is 5 bytes.  Never serve a short body as complete."""
    root = _build(
        tmp_path / "Database",
        [_part(2100, 301, THREAD_MITCH, "image/jpeg", b"short", 5, size=9999)],
    )
    monkeypatch.setattr(settings, "phone_link_db_dir", str(root))

    resp = await _stream(api_client, auth_headers, 2100)

    assert resp.status_code == 503
    body = resp.json()
    assert body["error"]["code"] == E_PHONE_UNAVAILABLE
    context = body["error"]["context"]
    assert context["part_id"] == 2100
    assert context["declared_size"] == 9999
    assert context["actual_bytes"] == 5


async def test_stream_requires_the_api_key(api_client):
    resp = await api_client.get(f"{MEDIA_URL}/2001")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "E_AUTH"


# ── V-12: a non-object body still leaves its one audit row ────────────────────────────────────


@pytest.mark.parametrize("raw", [b"[]", b'"a string"', b"42", b"null", b'["to","body"]'])
async def test_non_object_send_body_is_a_coded_422_and_one_audit_row(raw, monkeypatch):
    """V-12: `payload: dict` made FastAPI refuse BEFORE the route ran, so the attempt vanished.

    Mirrors `test_v17_phone_send.py`'s two-session pattern exactly: the dependency opens its own
    session and does what `core.database.get_db` does on a refusal (ROLLBACK), then a FRESH session
    must still see the audit row — i.e. the route committed it before raising, not merely added it.
    """
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    import app.models  # noqa: F401  — registers every table on Base.metadata
    from app.core.database import Base, get_db
    from app.main import app
    from app.models.report import ActivityLog

    engine = create_async_engine(
        "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async def _request_session():
        """The shipped `get_db` contract: commit on success, ROLLBACK on any exception."""
        async with Session() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = _request_session
    monkeypatch.setattr(settings, "api_key", "test-key")
    # If the shape guard were skipped this body would answer 403 E_SANDBOX_BLOCKED, not 422.
    monkeypatch.setattr(settings, "sandbox_mode", True)
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                SEND_URL,
                headers={"X-API-Key": "test-key", "Content-Type": "application/json"},
                content=raw,
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["error"]["code"] == E_VALIDATION
    assert resp.headers["X-Polsia-Error-Code"] == E_VALIDATION

    async with Session() as reader:
        rows = (
            (await reader.execute(select(ActivityLog).where(ActivityLog.action == "phone_send")))
            .scalars()
            .all()
        )
    await engine.dispose()

    assert len(rows) == 1, "exactly ONE audit row per attempt, and it must survive the rollback"
    assert E_VALIDATION in (rows[0].summary or ""), "the row names the refusal code"
    assert (rows[0].detail or {}).get("target") == "", "a non-object body has no target to mask"


async def test_object_bodied_send_still_behaves_exactly_as_before(api_client, auth_headers, async_db_session):
    """The V-12 change must not move the shipped send path: a dict body still reaches the gate."""
    resp = await api_client.post(
        SEND_URL, headers=auth_headers, json={"to": "11", "body": "Running ten minutes late."}
    )

    assert resp.status_code == 403, "sandbox is the shipped default and refuses by name"
    assert resp.json()["error"]["code"] == "E_SANDBOX_BLOCKED"

    from app.models.report import ActivityLog

    rows = (await async_db_session.execute(select(ActivityLog))).scalars().all()
    assert [r.action for r in rows if r.action == "phone_send"] == ["phone_send"]
