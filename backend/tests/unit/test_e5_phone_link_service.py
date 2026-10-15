"""E5 / P-01 — `app.services.phone_link_service`: the read-only Phone Link port.

RED-FIRST.  These pins exist before the module does, and they pin the three things the
amendment's P-01 acceptance names:

* a read that returns REAL rows out of a store built to the MEASURED schema
  (`tests/fixtures/phone_store.py` — DDL copied column-for-column from the live store);
* FILETIME -> local conversion, contact-name resolution, the SMS/MMS union, the call-type
  legend — the bridge behaviours that must survive the port;
* **refuse-by-name**: an absent store and a store whose schema drifted BOTH answer
  `E_PHONE_UNAVAILABLE` (never a plausible empty shape, never a fabrication).

Every read goes through `open_ro` (`mode=ro`); one pin proves the connection cannot write, which
is the property the whole feature rests on — the store belongs to the Phone Link app.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

from app.core.errors import E_PHONE_UNAVAILABLE, PolsiaError
from app.services import phone_link_service as phone_service
from tests.fixtures.phone_store import (
    CODE_VALUE,
    DANA,
    MITCH,
    STRANGER,
    THREAD_DANA,
    THREAD_MITCH,
    THREAD_STRANGER,
    UNREAD_TOTAL,
    build_phone_store,
    build_store_with_changed_schema,
    filetime,
)

#: Deterministic anchor: every seeded row is placed relative to it, and it is handed to the
#: service as the "today"/window anchor so no assertion depends on the day the suite runs.
NOW = datetime(2026, 6, 1, 14, 0, 0)


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A real store on disk + `PHONE_LINK_DB_DIR` pointed at it (the compose mechanism)."""
    from app.config import settings

    root = build_phone_store(tmp_path / "Database", now=NOW)
    monkeypatch.setattr(settings, "phone_link_db_dir", str(root))
    return root


def test_absent_store_refuses_by_name(tmp_path, monkeypatch):
    """No mount, no store: `E_PHONE_UNAVAILABLE` — the amendment's P-01 acceptance leg."""
    from app.config import settings

    monkeypatch.setattr(settings, "phone_link_db_dir", str(tmp_path / "no-such-mount"))

    assert phone_service.store_status() == "unavailable"
    with pytest.raises(PolsiaError) as excinfo:
        phone_service.summary(today=NOW.date())
    assert excinfo.value.code == E_PHONE_UNAVAILABLE
    assert excinfo.value.how_to_fix, "a refusal without a fix hint is a dead end for the operator"


def test_present_store_reports_ok(store):
    assert phone_service.store_status() == "ok"


def test_filetime_converts_to_local_iso_with_offset():
    """The store's only timestamp encoding: 100 ns ticks since 1601 — never served raw."""
    instant = datetime(2026, 6, 1, 9, 30, 15)
    when = phone_service.filetime_to_local(filetime(instant))

    assert when is not None
    parsed = datetime.fromisoformat(when)
    assert parsed.tzinfo is not None, "an offset-less string would be read as UTC by the browser"
    assert parsed.replace(tzinfo=None) == instant


def test_zero_and_absent_filetime_are_null_not_epoch():
    """`null` is not `1970` (the frozen `last_when: str|null`): unmeasured stays null."""
    assert phone_service.filetime_to_local(0) is None
    assert phone_service.filetime_to_local(None) is None


def test_non_numeric_filetime_refuses_by_name():
    with pytest.raises(ValueError):
        phone_service.filetime_to_local("not-a-number")


def test_contact_names_resolve_and_unknown_numbers_fall_back_to_the_address(store):
    assert phone_service.contact_name(MITCH) == "Mitch Alvarez"
    assert phone_service.contact_name("(512) 555-0101") == "Mitch Alvarez"  # format-insensitive
    assert phone_service.contact_name(STRANGER) == STRANGER, (
        "a number with no contact row must surface as the number, never as a guess"
    )


def test_number_masking_keeps_only_the_last_four_digits(store):
    masked = phone_service.mask_number(MITCH)

    assert masked.endswith("0101")
    assert MITCH not in masked
    assert not any(ch.isdigit() for ch in masked[:6]), "only the last four digits may survive"


def test_summary_shape_counts_and_today_window(store):
    data = phone_service.summary(today=NOW.date())

    assert set(data) == {"store_status", "unread_total", "threads", "missed_calls_today"}
    assert data["store_status"] == "ok"
    assert data["unread_total"] == UNREAD_TOTAL

    # Newest conversation first; each entry carries the frozen five keys.
    assert [t["thread_id"] for t in data["threads"]] == [THREAD_MITCH, THREAD_DANA, THREAD_STRANGER]
    for thread in data["threads"]:
        assert set(thread) == {"thread_id", "with", "unread", "last_when", "last_text"}
        assert thread["last_when"] is not None and datetime.fromisoformat(thread["last_when"])
    assert data["threads"][0]["with"] == "Mitch Alvarez"
    assert data["threads"][0]["unread"] == 2
    assert data["threads"][0]["last_text"] == "Thanks! Here is my code: 778899"

    # `missed_calls_today` is a LOCAL-day window over real rows: the 26 h-old missed call is out.
    assert [c["who"] for c in data["missed_calls_today"]] == ["Mitch Alvarez"]
    entry = data["missed_calls_today"][0]
    assert set(entry) == {"who", "num_masked", "when"}
    assert entry["num_masked"] == phone_service.mask_number(MITCH)
    assert datetime.fromisoformat(entry["when"]).date() == NOW.date()


def test_messages_union_sms_and_mms_with_direction_and_peer_names(store):
    messages = phone_service.messages(limit=100)

    assert len(messages) == 10, "9 SMS rows + 1 MMS text part — a message-only read loses the MMS"
    for message in messages:
        assert set(message) == {"when", "dir", "who", "text", "thread_id"}
        assert message["dir"] in {"in", "out"}
    assert messages[0]["when"] >= messages[-1]["when"], "newest first"
    # An OUTGOING message has no `from_address` in the store; `who` is the thread's peer.
    outgoing = [m for m in messages if m["dir"] == "out" and m["thread_id"] == THREAD_MITCH]
    assert outgoing and outgoing[0]["who"] == "Mitch Alvarez"
    # The MMS text part surfaces with its own text and the unknown number as the name.
    mms = [m for m in messages if m["text"] == "Here is the photo you asked for"]
    assert len(mms) == 1 and mms[0]["thread_id"] == THREAD_STRANGER
    assert mms[0]["who"] == STRANGER and mms[0]["dir"] == "in"
    # An INCOMING message from the other known contact resolves by name too.
    dana = [m for m in messages if m["thread_id"] == THREAD_DANA and m["dir"] == "in"]
    assert dana and dana[0]["who"] == "Dana Reyes"


def test_messages_thread_filter_and_limit(store):
    thread = phone_service.messages(limit=100, thread_id=THREAD_MITCH)

    assert {m["thread_id"] for m in thread} == {THREAD_MITCH}
    assert len(thread) == 5
    assert len(phone_service.messages(limit=2)) == 2


def test_messages_search_filters_on_the_message_text(store):
    hits = phone_service.messages(limit=100, q="verification code")

    assert len(hits) == 2, "both verification-code messages, newest first"
    assert hits[0]["text"].startswith("Your verification code is 112233")


def test_calls_legend_and_missed_only_filter(store):
    calls = phone_service.calls(limit=100)

    assert [c["type"] for c in calls] == ["missed", "outgoing", "incoming", "missed"], (
        "newest first; 1=incoming 2=outgoing 3=missed 5=missed (the measured legend)"
    )
    for call in calls:
        assert set(call) == {"when", "type", "who", "num_masked", "dur_s"}
    assert calls[1]["dur_s"] == 115 and calls[1]["who"] == "Dana Reyes"

    missed = phone_service.calls(limit=100, missed_only=True)
    assert len(missed) == 2
    assert {c["type"] for c in missed} == {"missed"}
    assert DANA not in missed[0]["num_masked"]


def test_codes_reads_verification_codes_and_respects_the_window(store):
    codes = phone_service.codes(limit=10)

    assert [c["code"] for c in codes] == ["112233", CODE_VALUE], "newest first"
    for entry in codes:
        assert set(entry) == {"when", "from", "code", "snippet"}
        assert entry["from"] == "Mitch Alvarez"
        assert entry["code"] in entry["snippet"]

    # The keyword-anchored regex the bridge measured: an order number and a support number in
    # plain prose are NOT codes (a bare `\d{4,8}` scan would return both).
    assert not {"12345", "5551234", "5551234567"} & {c["code"] for c in codes}

    # Freshness window (boundary INCLUSIVE, as measured in the bridge): both seeded codes are 90
    # and 120 minutes old, so a 1 h window is empty and a 1.5 h window keeps only the newer one.
    assert phone_service.codes(limit=10, within_hours=1, now=NOW) == []
    assert [c["code"] for c in phone_service.codes(limit=10, within_hours=1.5, now=NOW)] == [
        "112233"
    ]


def test_codes_never_read_our_own_outgoing_messages(store):
    """A code is something we RECEIVE: the seeded OUTGOING message carries a code-shaped string."""
    codes = phone_service.codes(limit=10)

    assert {c["code"] for c in codes} == {"112233", CODE_VALUE}
    assert "778899" not in {c["code"] for c in codes}


def test_contacts_export_rows(store):
    contacts = phone_service.contacts()

    assert {"name": "Mitch Alvarez", "phone": "(512) 555-0101"} in contacts
    assert {c["name"] for c in contacts} == {"Mitch Alvarez", "Dana Reyes"}


def test_changed_schema_refuses_by_name(tmp_path, monkeypatch):
    """A store that EXISTS but no longer carries a column we read is a named refusal.

    The file is a valid database, so a directory-stat check would answer 200 from a table it
    cannot read (or, worse, serve empty rows as "no messages").
    """
    from app.config import settings

    root = build_store_with_changed_schema(tmp_path / "Database", now=NOW)
    monkeypatch.setattr(settings, "phone_link_db_dir", str(root))

    assert phone_service.store_status() == "ok", "the directory itself is fine"
    with pytest.raises(PolsiaError) as excinfo:
        phone_service.summary(today=NOW.date())
    assert excinfo.value.code == E_PHONE_UNAVAILABLE
    assert "body" in str(excinfo.value.context), (
        "the refusal must name the drift (which column is missing), not just 'unavailable'"
    )


def test_the_read_connection_cannot_write(store):
    """`mode=ro` proven, not asserted: the store is the Phone Link app's, and we never write."""
    con = phone_service.open_ro(store / "phone.db")
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            con.execute(
                'INSERT INTO "message"(message_id, thread_id, status, body, timestamp) '
                "VALUES (9999, 11, 0, 'x', 0)"
            )
    finally:
        con.close()


def test_a_missing_database_file_inside_a_present_directory_refuses(tmp_path, monkeypatch):
    """Half a mount (the directory exists, `phone.db` does not) is still a named refusal."""
    from app.config import settings

    root = build_phone_store(tmp_path / "Database", now=NOW)
    (root / "phone.db").unlink()
    monkeypatch.setattr(settings, "phone_link_db_dir", str(root))

    with pytest.raises(PolsiaError) as excinfo:
        phone_service.summary(today=NOW.date())
    assert excinfo.value.code == E_PHONE_UNAVAILABLE


def test_activity_since_reads_by_id_cursor_with_a_bounded_lookback(store):
    """The tick's read cursor: ids, not wall clock, plus a lookback window for late rows."""
    from app.services.phone_link_service import PhoneWatermark, activity_since, max_ids

    everything = activity_since(PhoneWatermark(sms=0, mms=0, call=0))

    assert {(row["kind"], row["id"]) for row in everything} == {
        ("sms", 1), ("sms", 2), ("sms", 3), ("sms", 4), ("sms", 5),
        ("sms", 6), ("sms", 7), ("sms", 8), ("sms", 9), ("mms", 101),
        ("call", 901), ("call", 902), ("call", 903), ("call", 904),
    }
    assert [row["when"] for row in everything] == sorted(
        (row["when"] for row in everything), reverse=True
    ), "newest first — the tick reverses it, so the direction is part of the contract"

    # The strict cursor: nothing left once the watermark is at the newest ids.
    assert activity_since(max_ids(), lookback=0) == []

    # A watermark ABOVE the store's ids still re-reads the newest lookback window, so a row that
    # landed out of order is not lost forever; the (kind,id) ledger is what stops a re-publish.
    stale = activity_since(PhoneWatermark(sms=99, mms=999, call=9999))
    assert {row["id"] for row in stale if row["kind"] == "sms"} == {1, 2, 3, 4, 5, 6, 7, 8, 9}


def test_activity_rows_carry_the_uniform_seven_key_shape(store):
    """The tick's input shape — real store fields only, no synthesised values."""
    from app.services.phone_link_service import PhoneWatermark, activity_since

    rows = activity_since(PhoneWatermark(sms=0, mms=0, call=0))
    for row in rows:
        assert set(row) == {"kind", "id", "when", "who", "text", "dir", "call_type"}
        assert row["kind"] in {"sms", "mms", "call"}
        assert isinstance(row["id"], int)
        assert datetime.fromisoformat(row["when"]).tzinfo is not None

    sms = next(row for row in rows if row["kind"] == "sms" and row["id"] == 5)
    assert (sms["dir"], sms["who"], sms["call_type"]) == ("in", "Mitch Alvarez", None)
    call = next(row for row in rows if row["kind"] == "call" and row["id"] == 901)
    assert call["call_type"] == 3, "the tick needs the raw legend digit to decide 'missed'"
    assert call["dir"] is None and call["text"] is None


def test_store_dir_resolution_prefers_the_env_override(tmp_path, monkeypatch):
    """The compose mount contract: `PHONE_LINK_DB_DIR` wins; nothing else is guessed."""
    from app.config import settings

    root = build_phone_store(tmp_path / "Database", now=NOW)
    monkeypatch.setattr(settings, "phone_link_db_dir", str(root))

    assert phone_service.resolve_store_dir() == root


def test_default_store_glob_is_the_measured_windows_path():
    assert "Microsoft.YourPhone_8wekyb3d8bbwe" in phone_service.DEFAULT_STORE_GLOB
    assert "System" in phone_service.DEFAULT_STORE_GLOB
    assert phone_service.CONTAINER_STORE_DIR == "/phone-link"
