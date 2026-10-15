"""E5 / P-03 — the phone activity tick: phone events into the EXISTING activity pub/sub.

RED-FIRST.  The amendment's P-03 acceptance names exactly three properties, and each is a leg here:

    1. **dedupe** — run the tick twice -> ZERO duplicate events (the F1-class lesson);
    2. **event shape** — `"SMS received: <who>"` / `"Missed call: <who>"` — nothing else is an
       event (an outgoing text and a completed call are not news);
    3. **watermark persisted honestly** — a durable file that survives the process, and a row the
       cursor has already run past is still published exactly once (the lookback window), because
       the durable (kind,id) ledger — not the cursor — is what makes that safe.

The tick is a MAINTENANCE task: it runs on the beat, it must be idempotent, and an absent store
must be a NAMED no-op (`E_PHONE_UNAVAILABLE` in the result + zero rows), never a crash-loop or a
fabricated event.  The durable activity row is the feed's source of truth (`log_activity`) and the
Redis publish is the live nudge on `polsia:activity` — both are asserted, because "publishes into
the existing activity pub/sub" is the whole point of the item.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

import pytest

from app.core.errors import E_PHONE_UNAVAILABLE
from tests.fixtures.phone_store import CODE_VALUE, MITCH, STRANGER, build_phone_store

NOW = datetime(2026, 6, 1, 14, 0, 0)
#: The fixture seeds 6 incoming SMS + 1 incoming MMS + 2 missed calls (types 3 and 5) = 9 events.
EXPECTED_EVENTS = 9
#: Incoming messages FROM Mitch specifically (ids 5, 6, 8, 9) — the rest of the events are the
#: other peers and the two missed calls.
MITCH_EVENTS = 4


@pytest.fixture
def phone_store(tmp_path, monkeypatch):
    from app.config import settings

    root = build_phone_store(tmp_path / "Database", now=NOW)
    monkeypatch.setattr(settings, "phone_link_db_dir", str(root))
    return root


@pytest.fixture
def state_path(tmp_path, monkeypatch):
    """The durable watermark/ledger file, redirected per test (never the real data/ path)."""
    from celery_app.tasks import phone_activity

    path = tmp_path / "phone_activity_state.json"
    monkeypatch.setattr(phone_activity, "STATE_PATH", path)
    return path


def _tick() -> dict:
    from celery_app.tasks.phone_activity import publish_phone_activity

    return publish_phone_activity()


def _append_message(store, message_id: int, *, body: str, sender: str = MITCH, thread_id: int = 12) -> None:
    """Add one INCOMING message to the fixture store (a new sync row, exactly like the app's)."""
    con = sqlite3.connect(store / "phone.db")
    try:
        con.execute(
            'INSERT INTO "message"(message_id, thread_id, status, from_address, type, subject, '
            "body, timestamp, pc_status, subscription_id, checksum, group_id, to_address) "
            "VALUES (?,?,0,?,1,NULL,?,?,1,-1,0,NULL,NULL)",
            (message_id, thread_id, sender, body, 134365733490470000),
        )
        con.commit()
    finally:
        con.close()


def test_tick_publishes_every_new_event_once(queue_db, phone_store, state_path, quiet_redis):
    """First tick: one durable activity row + one Redis publish per real event."""
    assert phone_store and state_path
    result = _tick()

    assert result["status"] == "completed"
    assert result["published"] == EXPECTED_EVENTS
    assert result["skipped"] == 0

    rows = queue_db.query(
        "SELECT action, summary, detail, agent_type, level FROM activity_log ORDER BY id"
    )
    assert len(rows) == EXPECTED_EVENTS
    summaries = [row["summary"] for row in rows]
    assert summaries.count("SMS received: Mitch Alvarez") == MITCH_EVENTS
    assert "SMS received: Dana Reyes" in summaries
    assert f"SMS received: {STRANGER}" in summaries, "an unknown number is named by its number"
    assert "Missed call: Mitch Alvarez" in summaries
    assert f"Missed call: {STRANGER}" in summaries
    assert {row["action"] for row in rows} == {"phone_sms_received", "phone_missed_call"}
    assert {row["agent_type"] for row in rows} == {"system"}
    assert {row["level"] for row in rows} == {"info"}

    # The pub/sub edge: one publish per event, on the SHIPPED channel.
    assert quiet_redis.publish.await_count == EXPECTED_EVENTS
    assert quiet_redis.publish.await_args.args[0] == "polsia:activity"


def test_tick_twice_publishes_nothing_the_second_time(queue_db, phone_store, state_path, quiet_redis):
    """THE dedupe probe (amendment P-03 acceptance): tick twice -> zero duplicate events."""
    assert phone_store and state_path
    first = _tick()
    rows_after_first = queue_db.count("activity_log")
    publishes_after_first = quiet_redis.publish.await_count

    second = _tick()

    assert first["published"] == EXPECTED_EVENTS
    assert second["published"] == 0
    assert second["skipped"] >= EXPECTED_EVENTS, "the re-read rows are counted as skips, not hidden"
    assert queue_db.count("activity_log") == rows_after_first, "a second tick wrote a duplicate row"
    assert quiet_redis.publish.await_count == publishes_after_first, "a second tick re-published"


def test_watermark_and_ledger_are_persisted(queue_db, phone_store, state_path, quiet_redis):
    """The durable state: the id cursor per source table + the published (kind,id) ledger."""
    assert phone_store
    _tick()

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["watermark"] == {"sms": 9, "mms": 101, "call": 904}
    assert len(state["published"]) == EXPECTED_EVENTS
    assert "sms:5" in state["published"] and "call:901" in state["published"]
    assert set(state) >= {"version", "watermark", "published", "updated_at"}


def test_a_row_the_cursor_has_run_past_is_published_exactly_once(
    queue_db, phone_store, state_path, quiet_redis
):
    """Out-of-order sync: the cursor is AHEAD of a row that arrives later — and it is still news.

    This is why the service reads a bounded lookback past the cursor and why the (kind,id) ledger,
    not the cursor, is the dedupe guard: the row is published once, and never again.
    """
    assert phone_store
    _tick()
    publishes_after_first = quiet_redis.publish.await_count
    state = json.loads(state_path.read_text(encoding="utf-8"))

    # The cursor has run ahead (the store's ids jumped), then a row lands BELOW it.
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "watermark": {"sms": 99, "mms": 999, "call": 9999},
                "published": state["published"],
                "updated_at": state["updated_at"],
            }
        ),
        encoding="utf-8",
    )
    _append_message(phone_store, 10, body="Late sync row")

    result = _tick()

    assert result["published"] == 1, "the lookback window must surface the late row"
    summaries = [
        row["summary"] for row in queue_db.query("SELECT summary FROM activity_log ORDER BY id")
    ]
    assert summaries.count("SMS received: Mitch Alvarez") == MITCH_EVENTS + 1
    assert quiet_redis.publish.await_count == publishes_after_first + 1
    assert _tick()["published"] == 0, "and it is not published again on the next tick"
    assert quiet_redis.publish.await_count == publishes_after_first + 1


def test_a_verification_code_body_is_never_copied_into_the_feed(
    queue_db, phone_store, state_path, quiet_redis
):
    """The feed is rendered in the UI: the activity row names the sender, never the code."""
    assert phone_store
    _tick()

    rows = queue_db.query("SELECT summary, detail FROM activity_log")
    assert CODE_VALUE not in json.dumps(rows), "a 2FA code leaked into the activity feed"
    assert "112233" not in json.dumps(rows), "the older code leaked too"
    code_row = next(row for row in rows if "redacted" in (row["detail"] or ""))
    assert code_row["summary"] == "SMS received: Mitch Alvarez"
    assert "text" not in json.loads(code_row["detail"])


def test_absent_store_is_a_named_no_op(queue_db, state_path, tmp_path, monkeypatch, quiet_redis):
    """No mount: the tick reports the named code and writes NOTHING (no crash-loop, no fiction)."""
    from app.config import settings

    monkeypatch.setattr(settings, "phone_link_db_dir", str(tmp_path / "unmounted"))

    result = _tick()

    assert result["status"] == "store_unavailable"
    assert result["code"] == E_PHONE_UNAVAILABLE
    assert result["published"] == 0
    assert queue_db.count("activity_log") == 0
    assert not state_path.exists(), "no watermark is written for a store that was never read"
    assert quiet_redis.publish.await_count == 0


def test_a_corrupt_state_file_refuses_instead_of_replaying_the_store(
    queue_db, phone_store, state_path, quiet_redis
):
    """A damaged ledger would re-publish the whole store — the tick names it and publishes nothing."""
    from app.core.errors import E_STORAGE

    assert phone_store
    state_path.write_text("{not json", encoding="utf-8")

    result = _tick()

    assert result["status"] == "state_corrupt"
    assert result["code"] == E_STORAGE
    assert result["published"] == 0
    assert queue_db.count("activity_log") == 0
    assert quiet_redis.publish.await_count == 0


def test_save_state_trims_by_numeric_recency_not_string_order(state_path):
    """RED-FIRST (V16 live-drill fix, second iteration).

    Two failure modes measured live on the real store, one test each:
      * string order put 'mms:999' above 'mms:5233' — and worse, the GLOBAL sort by kind string
        starved the whole 'call' family ('call' < 'mms' < 'sms'), so the newest missed calls
        were trimmed out of a 500-key ledger and re-published on EVERY tick (tick2 == tick3 ==
        tick5 == {published: 3} against the real store);
      * a per-kind budget is therefore required: every id space keeps its OWN newest N keys.
    """
    from celery_app.tasks import phone_activity

    per_kind = phone_activity.LEDGER_MAX // 3
    keys = (
        {f"call:{i}" for i in range(1, 301)}
        | {f"mms:{i}" for i in range(1000, 1500)}
        | {f"sms:{i}" for i in range(1, 51)}
    )

    phone_activity._save_state(phone_activity.phone.PhoneWatermark(mms=1499), set(keys))
    _watermark, kept = phone_activity._load_state()

    assert len(kept) <= phone_activity.LEDGER_MAX
    assert "call:300" in kept, "the newest call key must survive — the live bug was call keys = 0"
    assert f"call:{300 - per_kind + 1}" in kept, "the newest per-kind budget of calls is kept"
    assert "call:1" not in kept, "only the OLDEST call keys are trimmed"
    assert "mms:1499" in kept, "numeric order within the kind: the newest mms key survives"
    assert "mms:1000" not in kept, "string order would keep 'mms:1000' and drop 'mms:1499'"
    assert "sms:50" in kept, "a kind smaller than its budget loses nothing"


def test_the_ledger_budget_covers_every_re_readable_row():
    """The invariant behind LEDGER_MAX: the lookback can re-read ACTIVITY_LOOKBACK ids per
    id space (sms/mms/call); with a PER-KIND budget, each space's newest budget must cover its
    whole re-readable window or the tick would re-publish a re-read row on every run."""
    from app.services import phone_link_service
    from celery_app.tasks import phone_activity

    assert phone_activity.LEDGER_MAX // 3 >= phone_link_service.ACTIVITY_LOOKBACK, (
        "the per-kind ledger budget must cover the whole re-readable window of one id space"
    )
