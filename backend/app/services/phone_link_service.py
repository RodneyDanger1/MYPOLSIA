"""Phone Link read path — a read-only port of the Hermes `phone-link-bridge` reader.

WHY THIS MODULE EXISTS
    The Hermes-side bridge (`scripts/phone_link.py` + the `phone-link` MCP server) already reads
    this store, but Polsia's agents run in the backend/worker containers and CANNOT call a Hermes
    MCP.  Without a native read path the orchestrator could never see the phone.  This module is
    that path: the same measured read logic, inside the product, under the product's refusal
    contract.

WHAT IT READS (the store, measured 2026-10-15)
    `PHONE_LINK_DB_DIR` -> `phone.db` (message / mms / mms_part / conversation / participant),
    `calling.db` (call_history), `contacts.db` (contact / phonenumber).  The compose file
    bind-mounts it READ-ONLY at `/phone-link`; the default is the measured Windows path
    (`DEFAULT_STORE_GLOB`, newest first — the GUID directory changes per install).

THE FIVE RULES THIS MODULE ENCODES
    1. **Read-only, always.**  Every open is `mode=ro` (`open_ro`), so a write is refused by the
       SQLite layer itself, not by discipline.  The store belongs to the Phone Link app.
    2. **Refuse by name, never fabricate.**  An absent mount, a half-mounted directory (a missing
       `*.db`), a drifted schema (a column this build reads no longer exists) and a locked file all
       raise `PolsiaError(E_PHONE_UNAVAILABLE)` with the drift/detail in `context`.  There is NO
       empty-inbox degradation: an empty list here means "the store really has no rows".
    3. **Timestamps are converted, never passed through.**  The store stores FILETIME (100 ns ticks
       since 1601).  `filetime_to_local` returns the machine-LOCAL wall clock as ISO-8601 WITH its
       UTC offset (a browser parsing an offset-less string would read it as UTC and show the wrong
       hour); `0`/absent becomes **None** — null is not 1970 (the frozen `last_when: str|null`).
    4. **The SMS/MMS union is mandatory.**  MMS bodies live in `mms_part.text`, not in `mms`; a
       `message`-only read silently loses every MMS.  XML-ish parts (`<...>`) are excluded — the
       measured pitfall the bridge encodes.
    5. **Codes are read defensively.**  `codes` scans INCOMING messages only (a code is something
       we receive), anchored on a keyword (`code|otp|pin|verification|...`) within 24 non-word
       characters of the code — so an order number or a support number in prose is not returned.
       Privacy: the caller (the codes route) audit-logs the ACCESS, never the code values.

DIFFERENCES FROM THE HERMES BRIDGE, stated once
    * `messages`/`calls` return typed dicts on the frozen H-P1 keys, not printable tables;
    * `who` on an OUTGOING message is the thread's PEER (resolved from `conversation.
      recipient_list` / `participant`).  The store leaves `from_address` empty on our own sends,
      and "me" is not the useful name for "who is this thread with"; the bridge prints "me" there
      because it is a human at a terminal;
    * an unreadable `contacts.db` is a named refusal instead of the bridge's silent fallback to raw
      numbers — a surface that quietly loses every contact name looks like a working inbox full of
      strangers;
    * nothing here writes, ever: no export root, no media, no archive bookkeeping (the export ZIP
      is built by the route in the product's own scratch-dir pattern).
"""
from __future__ import annotations

import contextlib
import glob
import logging
import os
import re
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from app.config import settings
from app.core.errors import E_PHONE_UNAVAILABLE, PolsiaError

logger = logging.getLogger(__name__)

#: The measured Phone Link store location (`<install GUID>` varies per machine).
DEFAULT_STORE_GLOB = (
    "%LOCALAPPDATA%/Packages/Microsoft.YourPhone_8wekyb3d8bbwe/LocalCache/Indexed/*/System/Database"
)
#: The container-side mount point (docker-compose.yml, `:ro`).
CONTAINER_STORE_DIR = "/phone-link"
#: 100 ns ticks between 1601-01-01 and 1970-01-01.
EPOCH_DELTA_SECONDS = 11_644_473_600
#: How long a read waits for the Phone Link app to release a write lock before refusing.
READ_TIMEOUT_SECONDS = 5.0
#: The three databases a COMPLETE store carries.  Half a mount is a configuration error.
STORE_FILES = ("phone.db", "calling.db", "contacts.db")
#: Rows are ordered `ts DESC`; the codes scan and the missed-call window are bounded reads.
CODE_SCAN_LIMIT = 250
MISSED_TODAY_SCAN_LIMIT = 200
#: H-P1 `threads[]` size on `summary`: the dashboard card's "recent threads" window.
SUMMARY_THREAD_LIMIT = 10
#: A search/limit ceiling, so a caller cannot ask the container to serialize the whole store.
MESSAGE_LIMIT_MAX = 500
#: The tick's lookback: ids re-read on every pass so a row that syncs OUT OF ORDER is not lost;
#: the durable (kind,id) ledger — not the cursor — is what makes the publish exactly-once.
ACTIVITY_LOOKBACK = 50
#: Peer list / body caps: a bounded wire payload, never a truncated silent edit of the text.
PEERS_MAX_CHARS = 120
TEXT_MAX_CHARS = 4000
#: Measured call-type legend: 1=incoming 2=outgoing 3=missed 5=missed (unanswered).
CALL_TYPES: dict[int, str] = {1: "incoming", 2: "outgoing", 3: "missed", 5: "missed"}
MISSED_CALL_TYPES = frozenset({3, 5})

#: The verification-code pattern.  MEASURED, not inherited: the Hermes bridge's regex
#: (`(?:code|otp|...)[^0-9A-Za-z]{0,24}([0-9]{4,8}|[A-Z0-9]{4,8})\b`, `re.I`) was run against this
#: machine's store (probe: `wave-v16/e5-phone-backend/raw/p01-codes-regex-probe.txt`) and it
#: answered the WORD "code" for 14 of 31 keyword-bearing rows — because a case-insensitive
#: `[A-Z0-9]{4,8}` matches "code" itself — and MISSED the real codes in the most common real
#: format ("Your Stripe verification code is: 215960": its separator class `[^0-9A-Za-z]{0,24}`
#: cannot cross the word "is").  This port fixes both classes:
#:   * the token must contain a DIGIT (so "code"/"CAULDRON"/"START"/"ROADTRIP" are not codes);
#:   * the anchor window is text (`{0,64}`/`{0,48}`, non-greedy) so "code is: NNNNNN" and
#:     "Enter NNNNNN as your Shopify authentication code" both resolve;
#:   * the token may not sit inside a URL/domain (the `(?<![./@?\w-])` guard — "theceshop.kvo8.io"
#:     is a hostname, not a code);
#:   * 4-8 characters with a word boundary, so a 10/11-digit phone number is never read as a code.
#: Residual, stated: a promo code that contains a digit (DULUTH's "DX4T") still matches, because
#: it IS a code in that message; every row carries its sender, so the caller can tell them apart.
_CODE_KEYWORD = r"(?:code|otp|pin|passcode|verification|verify|one[- ]time|security code|auth)"
_CODE_TOKEN = r"(?<![./@?\w-])(?=[0-9A-Z]{4,8}\b)(?=[0-9A-Z]*[0-9])[0-9A-Z]{4,8}"
CODE_RE = re.compile(
    rf"(?:{_CODE_KEYWORD}).{{0,64}}?({_CODE_TOKEN})|({_CODE_TOKEN}).{{0,48}}?(?:{_CODE_KEYWORD})",
    re.I | re.S,
)

#: The SMS+MMS union.  `mms_part.text` is where an MMS body actually lives; `NOT LIKE '<%'`
#: drops XML-ish parts (the measured pitfall: a raw SMIL/XML blob is not a message body).
MESSAGE_UNION_SQL = """
SELECT 'sms' AS kind, m.message_id AS id, m.thread_id AS tid, m.type AS dir,
       m.from_address AS frm, m.body AS body, m.timestamp AS ts
FROM message m
UNION ALL
SELECT 'mms' AS kind, mp.message_id AS id, mo.thread_id AS tid, mo.type AS dir,
       mo.from_address AS frm, mp.text AS body, mo.timestamp AS ts
FROM mms_part mp JOIN mms mo ON mo.message_id = mp.message_id
WHERE mp.text IS NOT NULL AND mp.text NOT LIKE '<%'
"""

#: Every column this module reads, per database.  Checked on OPEN (`_schema_drift`): a Phone Link
#: release that renames or drops one of these must produce a named refusal, not a wrong answer.
REQUIRED_COLUMNS: dict[str, dict[str, tuple[str, ...]]] = {
    "phone.db": {
        "message": ("message_id", "thread_id", "type", "from_address", "body", "timestamp"),
        "mms": ("message_id", "thread_id", "type", "from_address", "timestamp"),
        "mms_part": ("message_id", "content_type", "text"),
        "conversation": ("thread_id", "recipient_list", "timestamp", "unread_count"),
        "participant": ("thread_id", "normalized_address"),
    },
    "calling.db": {
        "call_history": ("call_id", "phone_number", "duration", "call_type", "start_time"),
    },
    "contacts.db": {
        "contact": ("contact_id", "display_name"),
        "phonenumber": ("contact_id", "phone_number", "display_phone_number"),
    },
}


# ── refusals ─────────────────────────────────────────────────────────────────────────────────


def _unavailable(
    message: str, *, how_to_fix: str = "", **context: Any
) -> PolsiaError:
    """The one refusal this module raises.  `context` carries the machine-readable detail."""
    return PolsiaError(
        E_PHONE_UNAVAILABLE,
        message,
        how_to_fix=how_to_fix
        or (
            "set PHONE_LINK_DB_DIR to the Phone Link store directory (compose mounts it at "
            f"{CONTAINER_STORE_DIR}:ro) and start the Phone Link app so it keeps syncing"
        ),
        context=context,
    )


# ── store location ───────────────────────────────────────────────────────────────────────────


def resolve_store_dir() -> Path:
    """The store directory: `PHONE_LINK_DB_DIR` when set, else the newest measured install path.

    Raises `E_PHONE_UNAVAILABLE` when NEITHER resolves — that is the "no mount at all" case and
    it is refused here rather than silently answering from an empty directory.
    """
    configured = (settings.phone_link_db_dir or "").strip()
    if configured:
        return Path(os.path.expandvars(os.path.expanduser(configured)))

    pattern = os.path.expandvars(DEFAULT_STORE_GLOB)
    candidates = [Path(p) for p in glob.glob(pattern) if (Path(p) / "phone.db").exists()]
    if not candidates:
        raise _unavailable(
            "no Phone Link store was found and PHONE_LINK_DB_DIR is not set",
            context={"configured": None, "searched": pattern, "container_path": CONTAINER_STORE_DIR},
        )
    # The GUID directory changes per install; the freshest `phone.db` is the live one.
    candidates.sort(key=lambda p: (p / "phone.db").stat().st_mtime, reverse=True)
    return candidates[0]


def store_status() -> str:
    """`"ok"` when every store database is present and openable, `"unavailable"` otherwise.

    Deliberately shallow (presence + readability, not columns): it answers "is there a store
    here?" for `summary`'s `store_status` field and for callers deciding whether to show the
    mount hint.  A schema problem surfaces as a named refusal on the read itself.
    """
    try:
        root = resolve_store_dir()
    except PolsiaError:
        return "unavailable"
    if not root.is_dir():
        return "unavailable"
    for name in STORE_FILES:
        path = root / name
        if not path.exists():
            return "unavailable"
        try:
            with contextlib.closing(open_ro(path)) as con:
                con.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' LIMIT 1"
                ).fetchone()
        except sqlite3.Error:
            return "unavailable"
    return "ok"


def open_ro(path: str | Path) -> sqlite3.Connection:
    """A READ-ONLY SQLite connection (`mode=ro`) with `Row` access.  Never writable."""
    uri = f"{Path(path).resolve().as_uri()}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=READ_TIMEOUT_SECONDS)
    con.row_factory = sqlite3.Row
    return con


def _schema_drift(con: sqlite3.Connection, db_name: str) -> list[str]:
    """`["phone.db:message.body", ...]` — the columns this build reads that the file no longer has."""
    expected = REQUIRED_COLUMNS.get(db_name, {})
    drift: list[str] = []
    for table, columns in expected.items():
        found = {
            row[1]
            for row in con.execute(f'PRAGMA table_info("{table}")').fetchall()
        }
        if not found:
            drift.append(f"{db_name}:{table} (table missing)")
            continue
        drift.extend(f"{db_name}:{table}.{column}" for column in columns if column not in found)
    return drift


def _connect(db_name: str) -> sqlite3.Connection:
    """Open one store database read-only, or refuse by name (absent / half-mount / drift / lock)."""
    try:
        root = resolve_store_dir()
    except PolsiaError:
        raise
    path = root / db_name
    if not path.exists():
        raise _unavailable(
            f"{db_name} is missing from the Phone Link store directory {root}",
            context={"store_dir": str(root), "missing": db_name, "expected_files": list(STORE_FILES)},
        )
    try:
        con = open_ro(path)
    except sqlite3.Error as exc:
        raise _unavailable(
            f"{db_name} could not be opened read-only ({type(exc).__name__}: {exc})",
            context={"store_dir": str(root), "file": db_name},
        ) from exc
    try:
        drift = _schema_drift(con, db_name)
    except sqlite3.Error as exc:
        con.close()
        raise _unavailable(
            f"{db_name} could not be inspected ({type(exc).__name__}: {exc})",
            context={"store_dir": str(root), "file": db_name},
        ) from exc
    if drift:
        con.close()
        raise _unavailable(
            f"the {db_name} schema no longer carries every column this build reads "
            f"({', '.join(drift)})",
            how_to_fix=(
                "the Phone Link app changed its database layout; re-measure the schema and update "
                "REQUIRED_COLUMNS in app/services/phone_link_service.py (no read can be trusted "
                "until then)"
            ),
            context={"store_dir": str(root), "file": db_name, "drift": drift},
        )
    return con


@contextlib.contextmanager
def _read(db_name: str) -> Iterator[sqlite3.Connection]:
    """`_connect` with a guaranteed close (sqlite3's own context manager does not close)."""
    con = _connect(db_name)
    try:
        yield con
    finally:
        con.close()


# ── value conversion ─────────────────────────────────────────────────────────────────────────


def filetime_to_local(ticks: int | float | str | None) -> str | None:
    """FILETIME ticks -> machine-local ISO-8601 WITH offset.  `0`/`None` -> `None` (not 1970).

    Raising `ValueError` for a non-numeric or unrepresentable value is deliberate: the value came
    out of the store and a corrupt timestamp is a real fault, not an "unmeasured" one.
    """
    if ticks is None:
        return None
    if isinstance(ticks, bool):
        raise ValueError(f"non-numeric FILETIME ticks {ticks!r}")
    if isinstance(ticks, str):
        stripped = ticks.strip()
        if not stripped:
            return None
        if not re.fullmatch(r"-?\d+", stripped):
            raise ValueError(f"non-numeric FILETIME ticks {ticks!r}")
        ticks = int(stripped)
    elif not isinstance(ticks, (int, float)):
        raise ValueError(f"non-numeric FILETIME ticks {ticks!r}")
    if not ticks:
        return None
    seconds = ticks / 10_000_000 - EPOCH_DELTA_SECONDS
    try:
        local = datetime.fromtimestamp(seconds).astimezone()
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError(f"FILETIME ticks {ticks!r} are outside the representable range") from exc
    return local.isoformat(timespec="seconds")


def _norm10(number: str | None) -> str:
    """The bridge's normalization: digits only, last 10 — format-insensitive contact matching."""
    digits = re.sub(r"\D", "", number or "")
    return digits[-10:] if len(digits) >= 10 else digits


def mask_number(number: str | None) -> str:
    """`+15125550101` -> `***-***-0101`.  The raw number never leaves the API unmasked.

    Handles the store's own partially masked values (`+160****4095` — Phone Link masks some
    numbers before writing them) without leaking the digits it does carry.
    """
    digits = re.sub(r"\D", "", number or "")
    if len(digits) >= 4:
        return f"***-***-{digits[-4:]}"
    return "***" if digits else ""


def _clean_text(body: str | None) -> str:
    return re.sub(r"\s+", " ", body or "").strip()[:TEXT_MAX_CHARS]


# ── contacts ─────────────────────────────────────────────────────────────────────────────────


def contact_map() -> dict[str, str]:
    """`{normalized 10-digit number: display_name}` from `contacts.db` (read on every call).

    Both `phone_number` and `display_phone_number` are indexed, so a lookup works whatever format
    the caller/thread carries.
    """
    mapping: dict[str, str] = {}
    with _read("contacts.db") as con:
        rows = con.execute(
            'SELECT c.display_name AS name, p.phone_number AS number, '
            "p.display_phone_number AS display FROM phonenumber p "
            'JOIN contact c ON c.contact_id = p.contact_id '
            "ORDER BY p.phone_number_id"
        ).fetchall()
    for row in rows:
        name = (row["name"] or "").strip()
        if not name:
            continue
        for raw in (row["number"], row["display"]):
            key = _norm10(raw)
            if key and key not in mapping:
                mapping[key] = name
    return mapping


def contact_name(address: str | None, mapping: dict[str, str] | None = None) -> str:
    """Display name for an address; the address itself when there is no contact row.

    An empty address is `"me"` (our own sends carry no `from_address`) — the caller decides
    whether that is meaningful; `messages` resolves outgoing rows to the thread's peer first.
    """
    if not address:
        return "me"
    table = mapping if mapping is not None else contact_map()
    return table.get(_norm10(address), address)


def _peer_names(recipient_list: str | None, mapping: dict[str, str]) -> str:
    """`"tel:+15125550101, +1512..."` -> `"Mitch Alvarez"` (the thread's display name)."""
    names: list[str] = []
    for part in re.split(r"[,;\s]+", (recipient_list or "").replace("tel:", "")):
        part = part.strip()
        if not part:
            continue
        name = mapping.get(_norm10(part), part)
        if name not in names:
            names.append(name)
    return ", ".join(names)[:PEERS_MAX_CHARS]


def _thread_peers(phone_con: sqlite3.Connection) -> dict[int, str]:
    """`{thread_id: display peers}` from `conversation.recipient_list`, then `participant`."""
    mapping = contact_map()
    peers: dict[int, str] = {}
    for row in phone_con.execute(
        'SELECT thread_id AS tid, recipient_list AS peers FROM "conversation"'
    ).fetchall():
        peers[int(row["tid"])] = _peer_names(row["peers"], mapping)
    for row in phone_con.execute(
        'SELECT thread_id AS tid, normalized_address AS addr FROM "participant" ORDER BY id'
    ).fetchall():
        tid = int(row["tid"])
        if not peers.get(tid):
            peers[tid] = _peer_names(row["addr"], mapping)
    return peers


def contacts() -> list[dict[str, Any]]:
    """Every contact as `{name, phone}` (the export member and the name-resolution source)."""
    with _read("contacts.db") as con:
        rows = con.execute(
            'SELECT c.display_name AS name, p.display_phone_number AS phone FROM contact c '
            "LEFT JOIN phonenumber p ON p.contact_id = c.contact_id "
            "ORDER BY c.display_name"
        ).fetchall()
    return [{"name": row["name"], "phone": row["phone"]} for row in rows]


# ── messages ─────────────────────────────────────────────────────────────────────────────────


def _message_rows(
    con: sqlite3.Connection,
    *,
    limit: int | None,
    thread_id: int | None = None,
    q: str | None = None,
) -> list[sqlite3.Row]:
    sql = f"SELECT * FROM ({MESSAGE_UNION_SQL}) WHERE 1 = 1"
    params: list[Any] = []
    if thread_id is not None:
        sql += " AND tid = ?"
        params.append(int(thread_id))
    if q:
        sql += " AND body LIKE ?"
        params.append(f"%{q}%")
    sql += " ORDER BY ts DESC, id DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    return con.execute(sql, params).fetchall()


def _message_dict(row: sqlite3.Row, peers: dict[int, str], mapping: dict[str, str]) -> dict[str, Any]:
    tid = int(row["tid"])
    incoming = row["dir"] == 1
    who = (
        contact_name(row["frm"], mapping)
        if incoming and row["frm"]
        else peers.get(tid) or contact_name(row["frm"], mapping)
    )
    return {
        "when": filetime_to_local(row["ts"]),
        "dir": "in" if incoming else "out",
        "who": who,
        "text": _clean_text(row["body"]),
        "thread_id": tid,
    }


def messages(
    *, limit: int = 50, thread_id: int | None = None, q: str | None = None
) -> list[dict[str, Any]]:
    """Newest-first SMS+MMS rows: `{when, dir, who, text, thread_id}` (the frozen H-P1 shape)."""
    capped = max(1, min(int(limit), MESSAGE_LIMIT_MAX))
    mapping = contact_map()
    with _read("phone.db") as con:
        peers = _thread_peers(con)
        rows = _message_rows(con, limit=capped, thread_id=thread_id, q=q)
    return [_message_dict(row, peers, mapping) for row in rows]


def all_messages() -> list[dict[str, Any]]:
    """EVERY message — the export path only (the owner's own archive; no window, no cap)."""
    mapping = contact_map()
    with _read("phone.db") as con:
        peers = _thread_peers(con)
        rows = _message_rows(con, limit=None)
    return [_message_dict(row, peers, mapping) for row in rows]


# ── calls ────────────────────────────────────────────────────────────────────────────────────


def _call_dict(row: sqlite3.Row, mapping: dict[str, str]) -> dict[str, Any]:
    number = row["num"]
    return {
        "when": filetime_to_local(row["ts"]),
        "type": CALL_TYPES.get(row["ct"], f"type{row['ct']}"),
        "who": contact_name(number, mapping),
        "num_masked": mask_number(number),
        "dur_s": row["dur"],
    }


def calls(*, limit: int = 50, missed_only: bool = False) -> list[dict[str, Any]]:
    """Newest-first call history: `{when, type, who, num_masked, dur_s}`."""
    capped = max(1, min(int(limit), MESSAGE_LIMIT_MAX))
    mapping = contact_map()
    sql = (
        'SELECT phone_number AS num, duration AS dur, call_type AS ct, start_time AS ts '
        'FROM "call_history"'
    )
    if missed_only:
        sql += f" WHERE call_type IN ({', '.join(str(t) for t in sorted(MISSED_CALL_TYPES))})"
    sql += " ORDER BY start_time DESC, call_id DESC LIMIT ?"
    with _read("calling.db") as con:
        rows = con.execute(sql, (capped,)).fetchall()
    return [_call_dict(row, mapping) for row in rows]


def export_calls() -> list[dict[str, Any]]:
    """EVERY call with its RAW number, oldest first — the export path only.

    The route surface masks numbers (`calls`); the archive is the owner's own copy of their own
    data (the V10 `/data/export` rule), so it carries the store's value unchanged.  Oldest-first
    and unlimited, mirroring the Hermes-side bridge export.
    """
    mapping = contact_map()
    with _read("calling.db") as con:
        rows = con.execute(
            'SELECT phone_number AS num, duration AS dur, call_type AS ct, start_time AS ts '
            'FROM "call_history" ORDER BY start_time ASC, call_id ASC'
        ).fetchall()
    return [
        {
            "when": filetime_to_local(row["ts"]),
            "type": CALL_TYPES.get(row["ct"], f"type{row['ct']}"),
            "who": contact_name(row["num"], mapping),
            "number": row["num"],
            "dur_s": row["dur"],
        }
        for row in rows
    ]


def missed_calls_today(*, today: date | None = None) -> list[dict[str, Any]]:
    """`{who, num_masked, when}` for missed calls on the LOCAL day.

    `start_time DESC` + a bounded scan: the newest missed calls are today's, so a limit that
    covers two days of history can never hide one (a store with >200 missed calls since today
    would mean >200 missed calls AFTER today, which the ordering makes impossible).
    """
    day = today or date.today()
    mapping = contact_map()
    sql = (
        'SELECT phone_number AS num, duration AS dur, call_type AS ct, start_time AS ts '
        'FROM "call_history" '
        f"WHERE call_type IN ({', '.join(str(t) for t in sorted(MISSED_CALL_TYPES))}) "
        "ORDER BY start_time DESC, call_id DESC LIMIT ?"
    )
    with _read("calling.db") as con:
        rows = con.execute(sql, (MISSED_TODAY_SCAN_LIMIT,)).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        when = filetime_to_local(row["ts"])
        if when is None or datetime.fromisoformat(when).date() != day:
            continue
        out.append(
            {"who": contact_name(row["num"], mapping), "num_masked": mask_number(row["num"]), "when": when}
        )
    return out


# ── summary ──────────────────────────────────────────────────────────────────────────────────


def unread_total() -> int:
    """Sum of `conversation.unread_count` — a real measured count (0 when the store has none)."""
    with _read("phone.db") as con:
        value = con.execute(
            'SELECT COALESCE(SUM(unread_count), 0) AS total FROM "conversation"'
        ).fetchone()
    return int(value["total"] or 0)


def summary(
    *, today: date | None = None, thread_limit: int = SUMMARY_THREAD_LIMIT
) -> dict[str, Any]:
    """The H-P1 `data` object for `GET /api/v1/phone/summary`."""
    mapping = contact_map()
    with _read("phone.db") as con:
        threads = con.execute(
            'SELECT thread_id AS tid, recipient_list AS peers, timestamp AS ts, '
            "unread_count AS unread FROM \"conversation\" "
            "ORDER BY timestamp DESC, thread_id DESC LIMIT ?",
            (int(thread_limit),),
        ).fetchall()
        last_rows = con.execute(
            f"SELECT tid AS tid, body AS body FROM ("
            f"  SELECT tid, body, ROW_NUMBER() OVER (PARTITION BY tid ORDER BY ts DESC, id DESC) AS rn"
            f"  FROM ({MESSAGE_UNION_SQL})"
            f") WHERE rn = 1"
        ).fetchall()
    last_by_thread = {int(row["tid"]): _clean_text(row["body"]) for row in last_rows}

    return {
        "store_status": "ok",
        "unread_total": unread_total(),
        "threads": [
            {
                "thread_id": int(row["tid"]),
                "with": _peer_names(row["peers"], mapping),
                "unread": int(row["unread"] or 0),
                "last_when": filetime_to_local(row["ts"]),
                "last_text": last_by_thread.get(int(row["tid"]), ""),
            }
            for row in threads
        ],
        "missed_calls_today": missed_calls_today(today=today),
    }


# ── verification codes ───────────────────────────────────────────────────────────────────────


def codes(*, limit: int = 10, within_hours: float | None = None, now: datetime | None = None):
    """Verification codes from INCOMING messages: `{when, from, code, snippet}`.

    The freshness window is boundary-INCLUSIVE (`ts >= now - within_hours`), matching the
    measured bridge.  The scan is bounded (`CODE_SCAN_LIMIT` newest incoming messages).
    """
    capped = max(1, min(int(limit), 50))
    mapping = contact_map()
    cutoff_ticks: float | None = None
    if within_hours:
        anchor = now or datetime.now()
        cutoff_ticks = (anchor.timestamp() - float(within_hours) * 3600 + EPOCH_DELTA_SECONDS) * 1e7
    with _read("phone.db") as con:
        rows = _message_rows(con, limit=CODE_SCAN_LIMIT)
    out: list[dict[str, Any]] = []
    for row in rows:
        if row["dir"] != 1:  # incoming only: never read our own outgoing text back as a code
            continue
        if cutoff_ticks is not None and (row["ts"] or 0) < cutoff_ticks:
            continue
        match = CODE_RE.search(row["body"] or "")
        if not match:
            continue
        # Two groups: the keyword-first and token-first forms of the pattern (see CODE_RE).
        code = match.group(1) or match.group(2)
        out.append(
            {
                "when": filetime_to_local(row["ts"]),
                "from": contact_name(row["frm"], mapping),
                "code": code,
                "snippet": _clean_text(row["body"])[:140],
            }
        )
        if len(out) >= capped:
            break
    return out


# ── the activity tick's read cursor ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PhoneWatermark:
    """The durable read cursor: the newest (kind, id) this module has READ, per source table.

    Per-kind because `message.message_id` and `mms.message_id` are separate id spaces — one
    counter for both would let a low MMS id look "already read".
    """

    sms: int = 0
    mms: int = 0
    call: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"sms": self.sms, "mms": self.mms, "call": self.call}

    @classmethod
    def from_dict(cls, data: Any) -> PhoneWatermark:
        """Lenient by design: a corrupt/absent watermark reads as 0 (re-read everything) rather
        than as a fabricated high-water mark that would silently skip activity forever."""
        if not isinstance(data, dict):
            return cls()
        return cls(
            sms=int(data.get("sms") or 0),
            mms=int(data.get("mms") or 0),
            call=int(data.get("call") or 0),
        )


def max_ids() -> PhoneWatermark:
    """The newest id in each source table — the cursor value AFTER a pass over the store."""
    with _read("phone.db") as con:
        sms = con.execute('SELECT COALESCE(MAX(message_id), 0) AS v FROM "message"').fetchone()["v"]
        mms = con.execute('SELECT COALESCE(MAX(message_id), 0) AS v FROM "mms"').fetchone()["v"]
    with _read("calling.db") as con:
        call = con.execute('SELECT COALESCE(MAX(call_id), 0) AS v FROM "call_history"').fetchone()["v"]
    return PhoneWatermark(sms=int(sms or 0), mms=int(mms or 0), call=int(call or 0))


def _threshold(watermark: int, newest: int, lookback: int) -> int:
    """Read `id > threshold`: everything past the cursor, PLUS the newest `lookback` ids.

    `min(cursor, newest - lookback)` is the whole trick: the lookback re-reads a bounded tail of
    the id space even when the cursor has already run past it, so a row that synced late is seen —
    and the (kind,id) ledger upstream is what keeps a re-read from re-publishing.
    """
    return min(int(watermark), max(0, int(newest) - max(0, int(lookback))))


def activity_since(
    watermark: PhoneWatermark, *, lookback: int = ACTIVITY_LOOKBACK
) -> list[dict[str, Any]]:
    """Rows newer than `watermark` (plus a bounded lookback), NEWEST FIRST.

    Uniform seven-key rows — `{kind, id, when, who, text, dir, call_type}` — so the tick's
    formatter needs no per-kind schema.  Only REAL store rows come out of here.
    """
    mapping = contact_map()
    rows: list[tuple[int, dict[str, Any]]] = []
    with _read("phone.db") as con:
        peers = _thread_peers(con)
        sms_newest = con.execute(
            'SELECT COALESCE(MAX(message_id), 0) AS v FROM "message"'
        ).fetchone()["v"]
        mms_newest = con.execute('SELECT COALESCE(MAX(message_id), 0) AS v FROM "mms"').fetchone()["v"]
        message_rows = con.execute(
            f"SELECT * FROM ({MESSAGE_UNION_SQL}) "
            "WHERE (kind = 'sms' AND id > ?) OR (kind = 'mms' AND id > ?) "
            "ORDER BY ts DESC, id DESC",
            (
                _threshold(watermark.sms, int(sms_newest or 0), lookback),
                _threshold(watermark.mms, int(mms_newest or 0), lookback),
            ),
        ).fetchall()

    for row in message_rows:
        payload = _message_dict(row, peers, mapping)
        rows.append(
            (
                row["ts"],
                {
                    "kind": row["kind"],
                    "id": int(row["id"]),
                    "when": payload["when"],
                    "who": payload["who"],
                    "text": payload["text"],
                    "dir": payload["dir"],
                    "call_type": None,
                },
            )
        )

    with _read("calling.db") as con:
        newest_call = con.execute(
            'SELECT COALESCE(MAX(call_id), 0) AS v FROM "call_history"'
        ).fetchone()["v"]
        call_rows = con.execute(
            'SELECT call_id AS id, phone_number AS num, duration AS dur, call_type AS ct, '
            'start_time AS ts FROM "call_history" WHERE call_id > ? '
            "ORDER BY start_time DESC, call_id DESC",
            (_threshold(watermark.call, int(newest_call or 0), lookback),),
        ).fetchall()
    for row in call_rows:
        rows.append(
            (
                row["ts"],
                {
                    "kind": "call",
                    "id": int(row["id"]),
                    "when": filetime_to_local(row["ts"]),
                    "who": contact_name(row["num"], mapping),
                    "text": None,
                    "dir": None,
                    "call_type": int(row["ct"]),
                },
            )
        )

    rows.sort(key=lambda item: item[0], reverse=True)
    return [payload for _ts, payload in rows]


__all__ = [
    "ACTIVITY_LOOKBACK",
    "CALL_TYPES",
    "CONTAINER_STORE_DIR",
    "DEFAULT_STORE_GLOB",
    "MESSAGE_LIMIT_MAX",
    "PhoneWatermark",
    "activity_since",
    "all_messages",
    "calls",
    "codes",
    "contact_map",
    "contact_name",
    "contacts",
    "filetime_to_local",
    "mask_number",
    "max_ids",
    "messages",
    "missed_calls_today",
    "open_ro",
    "resolve_store_dir",
    "store_status",
    "summary",
    "unread_total",
]
