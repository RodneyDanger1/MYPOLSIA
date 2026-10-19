"""E5 / P-02 — the frozen H-P1 surface: `GET /api/v1/phone/*`.

RED-FIRST.  These pins are written against the frozen interface BEFORE the router exists.

Every route is pinned for the four properties H-P1 declares:
    * key-auth (no key -> 401 `E_AUTH`, the shared dependency, not a per-route check);
    * the envelope — `{"ok": true, "data": {...}}` on success, the named refusal shape on failure;
    * the EXACT `data` keys the frontend lane codes against (a renamed key must fail here);
    * coded refusals: an absent store answers 503 `E_PHONE_UNAVAILABLE` with the code in the body
      AND in `X-Polsia-Error-Code` — never a 200 with an empty list.

The codes route has two extra pins the amendment demands: it is key-only, and EVERY call writes an
`activity_log` audit row — while the codes themselves never reach that row.
"""
from __future__ import annotations

import csv
import io
import json
import zipfile
from datetime import datetime

import pytest
from sqlalchemy import select

from app.core.errors import E_AUTH, E_PHONE_UNAVAILABLE
from tests.fixtures.phone_store import (
    CODE_VALUE,
    STRANGER,
    THREAD_DANA,
    THREAD_MITCH,
    UNREAD_TOTAL,
    build_phone_store,
)

NOW = datetime(2026, 6, 1, 14, 0, 0)


@pytest.fixture
def phone_store(tmp_path, monkeypatch):
    """The env seam the compose mount uses: `PHONE_LINK_DB_DIR` -> a real store on disk."""
    from app.config import settings

    root = build_phone_store(tmp_path / "Database", now=NOW)
    monkeypatch.setattr(settings, "phone_link_db_dir", str(root))
    return root


@pytest.fixture
def no_phone_store(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "phone_link_db_dir", str(tmp_path / "unmounted"))
    return tmp_path / "unmounted"


PHONE_ROUTES = (
    ("/api/v1/phone/summary", "summary"),
    ("/api/v1/phone/messages", "messages"),
    ("/api/v1/phone/calls", "calls"),
    ("/api/v1/phone/codes", "codes"),
    ("/api/v1/phone/export", "export"),
)


@pytest.mark.parametrize("path,name", PHONE_ROUTES)
async def test_every_phone_route_requires_the_api_key(api_client, path, name):
    """Key-auth is the router dependency — the same one every owner route uses."""
    assert path  # the parametrized path is the subject; `name` only labels the failure
    resp = await api_client.get(path)

    assert resp.status_code == 401, f"{path} answered without a key"
    assert resp.json()["error"]["code"] == E_AUTH


@pytest.mark.parametrize(
    "path,name",
    [
        ("/api/v1/phone/messages", "messages"),
        ("/api/v1/phone/calls", "calls"),
        ("/api/v1/phone/codes", "codes"),
        ("/api/v1/phone/export", "export"),
    ],
)
async def test_absent_store_is_a_named_503_on_every_reading_route(
    api_client, auth_headers, no_phone_store, path, name
):
    """No mount -> `E_PHONE_UNAVAILABLE` by name (and never a fabricated empty payload)."""
    assert no_phone_store
    resp = await api_client.get(path, headers=auth_headers)

    assert resp.status_code == 503, f"{path} did not refuse an absent store ({name})"
    body = resp.json()
    assert set(body) == {"ok", "error"}
    assert body["ok"] is False
    assert body["error"]["code"] == E_PHONE_UNAVAILABLE
    assert resp.headers.get("X-Polsia-Error-Code") == E_PHONE_UNAVAILABLE


async def test_summary_absent_store_is_a_named_503(api_client, auth_headers, no_phone_store):
    """`summary` is the dashboard card's route: it must be the honest state, not a zero card."""
    assert no_phone_store
    resp = await api_client.get("/api/v1/phone/summary", headers=auth_headers)

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == E_PHONE_UNAVAILABLE


async def test_summary_contract(api_client, auth_headers, phone_store):
    assert phone_store
    resp = await api_client.get("/api/v1/phone/summary", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"ok", "data"}
    assert body["ok"] is True
    data = body["data"]
    assert set(data) == {"store_status", "unread_total", "threads", "missed_calls_today"}
    assert data["store_status"] == "ok"
    assert data["unread_total"] == UNREAD_TOTAL
    assert [t["thread_id"] for t in data["threads"]][0] == THREAD_MITCH
    for thread in data["threads"]:
        assert set(thread) == {"thread_id", "with", "unread", "last_when", "last_text"}
    for missed in data["missed_calls_today"]:
        assert set(missed) == {"who", "num_masked", "when"}


async def test_messages_contract_and_filters(api_client, auth_headers, phone_store):
    assert phone_store
    resp = await api_client.get("/api/v1/phone/messages?limit=3", headers=auth_headers)

    assert resp.status_code == 200
    messages = resp.json()["data"]["messages"]
    assert len(messages) == 3, "the limit param is honoured"
    for message in messages:
        assert set(message) == {"when", "dir", "who", "text", "thread_id"}

    threaded = await api_client.get(
        f"/api/v1/phone/messages?thread_id={THREAD_DANA}", headers=auth_headers
    )
    assert {m["thread_id"] for m in threaded.json()["data"]["messages"]} == {THREAD_DANA}

    searched = await api_client.get("/api/v1/phone/messages?q=photo", headers=auth_headers)
    hits = searched.json()["data"]["messages"]
    assert len(hits) == 1 and hits[0]["who"] == STRANGER


async def test_messages_rejects_out_of_range_limits(api_client, auth_headers, phone_store):
    """A limit is a bounded read: 0 and 10^5 are caller errors (422), not silent clamps."""
    assert phone_store
    for bad in ("0", "-5", "5000"):
        resp = await api_client.get(f"/api/v1/phone/messages?limit={bad}", headers=auth_headers)
        assert resp.status_code == 422, f"limit={bad} was accepted"


async def test_calls_contract_and_missed_only(api_client, auth_headers, phone_store):
    assert phone_store
    resp = await api_client.get("/api/v1/phone/calls?limit=10", headers=auth_headers)

    assert resp.status_code == 200
    calls = resp.json()["data"]["calls"]
    assert len(calls) == 4
    for call in calls:
        assert set(call) == {"when", "type", "who", "num_masked", "dur_s"}

    missed = await api_client.get("/api/v1/phone/calls?missed_only=true", headers=auth_headers)
    rows = missed.json()["data"]["calls"]
    assert len(rows) == 2 and {row["type"] for row in rows} == {"missed"}


async def test_codes_contract_and_the_audit_row(api_client, auth_headers, phone_store, async_db_session):
    """The codes route exists for auth flows: it answers with the code AND leaves an audit row.

    The row records the ACCESS (count, window) — never the code value.
    """
    assert phone_store
    resp = await api_client.get("/api/v1/phone/codes", headers=auth_headers)

    assert resp.status_code == 200
    codes = resp.json()["data"]["codes"]
    assert [entry["code"] for entry in codes][0] == "112233"
    for entry in codes:
        assert set(entry) == {"when", "from", "code", "snippet"}

    from app.models.report import ActivityLog

    rows = (await async_db_session.execute(select(ActivityLog))).scalars().all()
    audit = [row for row in rows if "phone" in row.action or "phone" in row.summary.lower()]
    assert len(audit) == 1, "every codes read writes exactly one audit row"
    entry = audit[0]
    assert entry.level == "info"
    assert str(len(codes)) in entry.summary or str(len(codes)) in json.dumps(entry.detail)
    assert CODE_VALUE not in entry.summary
    assert "112233" not in entry.summary, "the audit row must not carry the codes themselves"
    assert "112233" not in json.dumps(entry.detail or {})


async def test_codes_window_param_and_audit_on_an_empty_window(
    api_client, auth_headers, phone_store, async_db_session
):
    """A window with no codes still audits the ACCESS: an empty answer is data too."""
    assert phone_store
    resp = await api_client.get("/api/v1/phone/codes?within_hours=1", headers=auth_headers)

    assert resp.status_code == 200
    assert resp.json()["data"]["codes"] == []

    from app.models.report import ActivityLog

    rows = (await async_db_session.execute(select(ActivityLog))).scalars().all()
    assert len([row for row in rows if "phone" in row.action]) == 1


async def test_export_streams_a_zip_with_the_three_members(api_client, auth_headers, phone_store):
    from app.core.timeutil import today_utc

    assert phone_store
    resp = await api_client.get("/api/v1/phone/export", headers=auth_headers)

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    disposition = resp.headers["content-disposition"]
    assert "attachment" in disposition and ".zip" in disposition
    assert disposition == (
        f'attachment; filename="polsia-phone-export-{today_utc().isoformat()}.zip"'
    ), "the filename carries the UTC day (V19: naive local dates made the same export vary by host)"

    with zipfile.ZipFile(io.BytesIO(resp.content)) as archive:
        assert sorted(archive.namelist()) == ["calls.csv", "contacts.json", "messages.json"]
        messages = json.loads(archive.read("messages.json"))
        assert messages["count"] == len(messages["messages"]) == 10
        assert messages["exported"].endswith("+00:00"), "the exported stamp is UTC-aware"
        assert {"when", "dir", "who", "text", "thread_id"} <= set(messages["messages"][0])
        calls = list(csv.reader(io.StringIO(archive.read("calls.csv").decode("utf-8"))))
        assert calls[0] == ["when", "type", "who", "number", "duration_s"]
        assert len(calls) == 5  # header + 4 measured calls
        contacts = json.loads(archive.read("contacts.json"))
        assert {"name": "Mitch Alvarez", "phone": "(512) 555-0101"} in contacts


async def test_export_refuses_an_absent_store(api_client, auth_headers, no_phone_store):
    assert no_phone_store
    resp = await api_client.get("/api/v1/phone/export", headers=auth_headers)

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == E_PHONE_UNAVAILABLE
