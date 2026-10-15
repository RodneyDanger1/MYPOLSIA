"""Synthetic Phone Link store builder — the MEASURED schema, deterministic rows.

WHY A FIXTURE BUILDER AND NOT A COMMITTED .db FILE
    The store is the Windows Phone Link app's private SQLite database.  Tests need (a) a store
    that exists and (b) a store that is ABSENT, plus the schema-drift case; a binary blob in
    `tests/` cannot express any of those and would be unreadable evidence in review.  The DDL
    below is copied **column-for-column from the real store** (measured 2026-10-15 on this
    machine, `INDEXED_DB_DIR` under
    `%LOCALAPPDATA%/Packages/Microsoft.YourPhone_8wekyb3d8bbwe/LocalCache/Indexed/<GUID>/System/Database`),
    so a service read that works here is a read that works against the real files.

WHAT IS DELIBERATELY REAL IN THE SEED
    * FILETIME ticks (100 ns since 1601) — the store's only timestamp encoding;
    * an SMS/MMS UNION where the MMS text lives in `mms_part.text` (the shape the bridge's
      `MSG_SQL` unions, and the reason a naive `message`-only read silently loses texts);
    * a number that is NOT in `contacts.db` (name resolution falls back to the raw address);
    * a verification-code SMS plus decoys that must NOT be read as codes;
    * a missed call TODAY and one YESTERDAY, so the "missed calls today" window is exercised by
      real data instead of by a mocked clock;
    * `conversation.unread_count` values that sum to a known total.
    Nothing here is random: two builds with the same `now` are byte-identical, so a failure is
    reproducible from the test name alone.

THE `now` SEAM
    Rows are placed relative to `now` (default: wall clock).  Tests pass a fixed `now` and hand
    the same value to the service as its `today`/window anchor, so no assertion depends on the
    day the suite happens to run (the alternative — absolute dates — starts failing at midnight).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

#: 100 ns ticks between 1601-01-01 and 1970-01-01 (the FILETIME epoch offset, seconds).
EPOCH_DELTA_SECONDS = 11644473600


def filetime(timestamp: datetime) -> int:
    """A `datetime` as the store stores it: FILETIME ticks, truncated to the 100 ns unit."""
    return int((timestamp.timestamp() + EPOCH_DELTA_SECONDS) * 10_000_000)


# ── the measured DDL, table by table ────────────────────────────────────────────────────────
# Statement-for-statement from `SELECT sql FROM sqlite_master`, with the terminating `;` that
# sqlite_master omits (it stores one statement per row) restored so `executescript` can run.
PHONE_DB_DDL = """
CREATE TABLE "content_sequence"(
    "content_sequence_id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
    "content_type" TEXT UNIQUE NOT NULL,
    "sequence" INTEGER NOT NULL DEFAULT -1,
    "expected" INTEGER NOT NULL DEFAULT -1);
CREATE TABLE "conversation"(
    "thread_id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
    "recipient_list" TEXT NOT NULL,
    "timestamp" INTEGER NOT NULL DEFAULT 0,
    "msg_count" INTEGER NOT NULL DEFAULT 0,
    "unread_count" INTEGER NOT NULL DEFAULT 0,
    "has_rcs" INTEGER NOT NULL DEFAULT 0,
    "checksum" INTEGER NOT NULL DEFAULT 0,
    "phone_unread_count" INTEGER NOT NULL DEFAULT 0);
CREATE TABLE "conversation_attribute"(
    "thread_id" INTEGER PRIMARY KEY NOT NULL,
    "rcs_thread_id" INTEGER NOT NULL DEFAULT -1,
    "position" INTEGER NOT NULL DEFAULT -1,
    "is_muted" INTEGER NOT NULL DEFAULT -1,
    "is_hidden" INTEGER NOT NULL DEFAULT -1);
CREATE TABLE "message"(
    "message_id" INTEGER PRIMARY KEY NOT NULL,
    "thread_id" INTEGER NOT NULL,
    "status" INTEGER NOT NULL,
    "from_address" TEXT,
    "type" INTEGER,
    "subject" TEXT,
    "body" TEXT NOT NULL,
    "timestamp" INTEGER NOT NULL,
    "pc_status" INTEGER,
    "subscription_id" INTEGER NOT NULL DEFAULT -1,
    "checksum" INTEGER NOT NULL DEFAULT 0,
    "group_id" INTEGER,
    "to_address" TEXT);
CREATE TABLE "mms"(
    "message_id" INTEGER PRIMARY KEY NOT NULL,
    "thread_id" INTEGER NOT NULL,
    "status" INTEGER NOT NULL,
    "type" INTEGER,
    "subscription_id" INTEGER NOT NULL DEFAULT -1,
    "subject" TEXT,
    "charset" INTEGER,
    "timestamp" INTEGER NOT NULL,
    "pc_status" INTEGER,
    "from_address" TEXT,
    "checksum" INTEGER NOT NULL DEFAULT 0,
    "to_addresses" TEXT);
CREATE TABLE "mms_part"(
    "part_id" INTEGER PRIMARY KEY NOT NULL,
    "message_id" INTEGER NOT NULL,
    "sequence_num" INTEGER,
    "content_id" TEXT,
    "content_type" TEXT,
    "text" TEXT,
    "name" TEXT,
    "charset" INTEGER,
    "blob" BLOB,
    "size" INTEGER NOT NULL DEFAULT 0);
CREATE TABLE "participant"(
    "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
    "thread_id" INTEGER NOT NULL,
    "send_address" TEXT NOT NULL,
    "normalized_address" TEXT NOT NULL,
    UNIQUE("thread_id", "normalized_address"));
"""

CALLING_DB_DDL = """
CREATE TABLE "call_history"(
    "call_id" INTEGER NOT NULL PRIMARY KEY,
    "phone_number" TEXT NOT NULL,
    "duration" INTEGER NOT NULL DEFAULT 0,
    "call_type" INTEGER NOT NULL,
    "start_time" INTEGER NOT NULL,
    "is_read" INTEGER NOT NULL DEFAULT 0,
    "phone_account_id" TEXT,
    "last_updated_time" INTEGER NOT NULL DEFAULT 0);
CREATE TABLE "content_sequence"(
    "content_sequence_id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
    "content_type" TEXT UNIQUE NOT NULL,
    "sequence" INTEGER NOT NULL DEFAULT -1,
    "expected" INTEGER NOT NULL DEFAULT -1);
"""

CONTACTS_DB_DDL = """
CREATE TABLE "contact"(
    "contact_id" INTEGER PRIMARY KEY NOT NULL,
    "display_name" TEXT,
    "nickname" TEXT,
    "last_updated_time" INTEGER,
    "thumbnail" BLOB,
    "company" TEXT,
    "job_title" TEXT,
    "notes" TEXT,
    "name_prefix" TEXT,
    "name_suffix" TEXT,
    "given_name" TEXT,
    "middle_name" TEXT,
    "family_name" TEXT,
    "checksum" INTEGER NOT NULL DEFAULT 0);
CREATE TABLE "contact_index"(
    "id" INTEGER PRIMARY KEY AUTOINCREMENT,
    "contact_id" INTEGER NOT NULL,
    "foreign_id" INTEGER NOT NULL,
    "foreign_table_name" TEXT NOT NULL,
    UNIQUE(foreign_id, foreign_table_name));
CREATE TABLE "phonenumber"(
    "phone_number_id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
    "contact_id" INTEGER NOT NULL,
    "phone_number" TEXT NOT NULL,
    "display_phone_number" TEXT,
    "phone_number_type" INTEGER NOT NULL DEFAULT 0,
    "label" TEXT,
    "checksum" INTEGER NOT NULL DEFAULT 0);
CREATE TABLE "emailaddress"(
    "email_address_id" INTEGER PRIMARY KEY NOT NULL,
    "contact_id" INTEGER NOT NULL,
    "type" INTEGER NOT NULL DEFAULT 0,
    "email_address" TEXT NOT NULL,
    "checksum" INTEGER NOT NULL DEFAULT 0);
CREATE TABLE "content_sequence"(
    "content_sequence_id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
    "content_type" TEXT UNIQUE NOT NULL,
    "sequence" INTEGER NOT NULL DEFAULT -1,
    "expected" INTEGER NOT NULL DEFAULT -1);
"""

# ── the seed (documented, so every pin in the tests has one readable source) ────────────────
#: US-format numbers for the two known peers; both are in contacts.db.
MITCH = "+15125550101"
DANA = "+15125550102"
#: NOT in contacts.db — name resolution must fall back to the address itself.
STRANGER = "+15125550999"
THREAD_MITCH = 11
THREAD_DANA = 12
THREAD_STRANGER = 13
#: Sum of `conversation.unread_count`: 2 + 0 + 1.
UNREAD_TOTAL = 3
#: The code a 1.5 h window returns: "verification code is 864213" (120 min back).
CODE_VALUE = "864213"
#: The newer code (90 min back) — inside a 1.5 h window, outside a 1 h one.
OLD_CODE_VALUE = "112233"


def build_phone_store(dest: Path | str, *, now: datetime | None = None) -> Path:
    """Write phone.db / calling.db / contacts.db under `dest` and return `dest`.

    `now` anchors every seeded row (tests pass a fixed instant); the returned path is what a test
    hands to the service as `PHONE_LINK_DB_DIR`.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    moment = now or datetime.now()

    _write_contacts(dest / "contacts.db")
    _write_phone(dest / "phone.db", moment)
    _write_calling(dest / "calling.db", moment)
    return dest


def build_store_with_changed_schema(dest: Path | str, *, now: datetime | None = None) -> Path:
    """A store whose `message` table lost the `body` column — the schema-drift case.

    The file EXISTS and is a valid SQLite database, so a service that only stats the directory
    would answer 200 from a table it cannot read.  The service must instead refuse by name.
    """
    dest = build_phone_store(dest, now=now)
    con = sqlite3.connect(dest / "phone.db")
    try:
        con.execute("ALTER TABLE message RENAME TO message_old")
        con.execute(
            'CREATE TABLE "message"('
            '"message_id" INTEGER PRIMARY KEY NOT NULL, "thread_id" INTEGER NOT NULL, '
            '"status" INTEGER NOT NULL, "body_missing" TEXT)'
        )
        con.commit()
    finally:
        con.close()
    return dest


def _write_contacts(path: Path) -> None:
    con = sqlite3.connect(path)
    try:
        con.executescript(CONTACTS_DB_DDL)
        con.executemany(
            'INSERT INTO "contact"(contact_id, display_name, nickname, checksum) VALUES (?,?,?,0)',
            [(1, "Mitch Alvarez", None), (2, "Dana Reyes", None)],
        )
        con.executemany(
            'INSERT INTO "phonenumber"(contact_id, phone_number, display_phone_number, '
            'phone_number_type, label, checksum) VALUES (?,?,?,1,NULL,0)',
            [(1, MITCH, "(512) 555-0101"), (2, DANA, "(512) 555-0102")],
        )
        con.commit()
    finally:
        con.close()


def _write_phone(path: Path, moment: datetime) -> None:
    con = sqlite3.connect(path)
    try:
        con.executescript(PHONE_DB_DDL)
        # (thread, recipient_list, minutes-since-newest-message, msg_count, unread_count)
        # `timestamp` is each thread's newest message instant (the store keeps them in sync).
        conversations = [
            (THREAD_MITCH, f"tel:{MITCH}", 60, 5, 2),
            (THREAD_DANA, f"tel:{DANA}", 180, 2, 0),
            (THREAD_STRANGER, f"tel:{STRANGER}", 295, 2, 1),
        ]
        con.executemany(
            'INSERT INTO "conversation"(thread_id, recipient_list, timestamp, msg_count, '
            "unread_count, has_rcs, checksum, phone_unread_count) VALUES (?,?,?,?,?,0,0,0)",
            [
                (tid, peers, filetime(moment - timedelta(minutes=offset)), count, unread)
                for tid, peers, offset, count, unread in conversations
            ],
        )
        con.executemany(
            'INSERT INTO "participant"(thread_id, send_address, normalized_address) '
            "VALUES (?,?,?)",
            [
                (THREAD_MITCH, MITCH, MITCH),
                (THREAD_DANA, DANA, DANA),
                (THREAD_STRANGER, STRANGER, STRANGER),
            ],
        )
        # (message_id, thread, type, from_address, body, minutes-ago); type 1 = in, 2 = out
        messages = [
            (1, THREAD_DANA, 1, DANA, "Are we still on for Thursday?", 240),
            (2, THREAD_DANA, 2, "", "Yes — 2pm works.", 180),
            (3, THREAD_STRANGER, 1, STRANGER, "Who is this? Wrong number?", 300),
            (4, THREAD_STRANGER, 2, "", "Sorry, wrong number.", 295),
            (5, THREAD_MITCH, 1, MITCH, "Your verification code is 864213. Do not share it.", 120),
            (6, THREAD_MITCH, 1, MITCH, "Order 12345 shipped; call 5551234567 for support.", 110),
            # 7 is OUTGOING and carries a code-shaped string: the codes reader must skip it.
            (7, THREAD_MITCH, 2, "", "Thanks! Here is my code: 778899", 60),
            # 8 is prose with no code; 9 is a REAL code 90 minutes back (the freshness legs).
            (8, THREAD_MITCH, 1, MITCH, "Missed you at the game — call me back when you can.", 95),
            (9, THREAD_MITCH, 1, MITCH, "Your verification code is 112233. Use within 1 hour.", 90),
        ]
        con.executemany(
            'INSERT INTO "message"(message_id, thread_id, status, from_address, type, subject, '
            "body, timestamp, pc_status, subscription_id, checksum, group_id, to_address) "
            "VALUES (?,?,0,?,?,NULL,?,?,1,-1,0,NULL,NULL)",
            [
                (mid, tid, frm, mtype, body, filetime(moment - timedelta(minutes=ago)))
                for mid, tid, mtype, frm, body, ago in messages
            ],
        )
        # MMS: the text lives in mms_part.text; the store's `mms` row carries the envelope.
        con.execute(
            'INSERT INTO "mms"(message_id, thread_id, status, type, subscription_id, subject, '
            "charset, timestamp, pc_status, from_address, checksum, to_addresses) "
            "VALUES (101,?,0,1,-1,NULL,106,?,1,?,0,NULL)",
            (THREAD_STRANGER, filetime(moment - timedelta(minutes=290)), STRANGER),
        )
        con.executemany(
            'INSERT INTO "mms_part"(part_id, message_id, sequence_num, content_id, content_type, '
            "text, name, charset, blob, size) VALUES (?,?,?,NULL,?,?,NULL,106,NULL,?)",
            [
                (1001, 101, 0, "text/plain", "Here is the photo you asked for", 28),
                (1002, 101, 1, "image/jpeg", None, 2048),
            ],
        )
        con.commit()
    finally:
        con.close()


def _write_calling(path: Path, moment: datetime) -> None:
    con = sqlite3.connect(path)
    try:
        con.executescript(CALLING_DB_DDL)
        # (call_id, number, duration, call_type, minutes-ago) — 1=incoming 2=outgoing 3=missed 5=missed
        calls = [
            (901, MITCH, 0, 3, 60),          # MISSED today (by the caller, never answered)
            (902, DANA, 115, 2, 120),        # outgoing today
            (903, MITCH, 42, 1, 360),        # incoming today (6 h ago)
            (904, STRANGER, 0, 5, 26 * 60),  # missed YESTERDAY (unanswered) — outside "today"
        ]
        con.executemany(
            'INSERT INTO "call_history"(call_id, phone_number, duration, call_type, start_time, '
            "is_read, phone_account_id, last_updated_time) VALUES (?,?,?,?,?,0,NULL,0)",
            [
                (cid, number, duration, ctype, filetime(moment - timedelta(minutes=ago)))
                for cid, number, duration, ctype, ago in calls
            ],
        )
        con.commit()
    finally:
        con.close()
