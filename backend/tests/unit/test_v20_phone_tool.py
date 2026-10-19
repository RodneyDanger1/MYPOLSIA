"""V20/L2 — the `phone_read` agent tool: firewalled phone context, codes structurally absent.

RED-FIRST.  This file is written BEFORE `tool_registry` learns the tool and BEFORE
`policy_firewall` carries the allowlist; on the untouched tree every leg below fails (unknown
tool by name, no spec, no allowlist entry) — the RED run is `raw/RED-phone-tool.txt`.

WHAT IT PINS (the frozen V20 interface, verbatim from the wave plan):

  * name `phone_read`, args `{"thread_id"?: int, "limit"?: int (1..10, default 5)}`;
  * success payload `{ok:true, tool:"phone_read", result:{store_status, threads:[{thread_id,
    who, last_when, unread}], messages:[{when, dir, who, text}]}}` — threads ALWAYS (newest
    ≤10); `messages` only when `thread_id` is given (≤10, with an honest truncation note);
  * **no codes field exists in the shape at all** — plus the house precedent from
    `celery_app/tasks/phone_activity.py::_detail_for`: a message body that LOOKS like a
    verification code is WITHHELD, so no code VALUE can ride a body out of the tool either;
  * absent/unreadable/corrupt store -> a named `E_PHONE_UNAVAILABLE` refusal (the service's own
    message), never an empty fabricated inbox;
  * every call rides the firewalled `execute_tool` path: an agent outside the allowlist gets the
    standard `E_POLICY_DENIED` refusal naming tool + agent, and H-C1
    (`GET /api/v1/ops/capabilities`, driven at ASGI level) lists `phone_read` exactly for the
    agents the allowlist grants it to.

The store is a REAL on-disk SQLite fixture built by `tests/fixtures/phone_store.py` (the
measured Phone Link schema).  Tests that need more rows than the shared seed appends REAL rows
to that same store (`_add_threads` / `_add_thread_messages`) — a labelled test fixture, never a
mock of the service under test.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

import pytest

from app.agents import policy_firewall as pf
from app.agents import tool_registry
from app.core.errors import E_PHONE_UNAVAILABLE, E_POLICY_DENIED, E_VALIDATION
from app.services import phone_link_service
from tests.fixtures.phone_store import (
    CODE_VALUE,
    OLD_CODE_VALUE,
    STRANGER,
    THREAD_DANA,
    THREAD_MITCH,
    THREAD_STRANGER,
    UNREAD_TOTAL,
    build_phone_store,
    filetime,
)

#: Deterministic anchor: the shared fixture's rows are placed relative to it (same seam the E5
#: service pins use), so no assertion depends on the day the suite runs.
NOW = datetime(2026, 6, 1, 14, 0, 0)

#: The V20 allowlist decision (evidence + rationale in the lane RECEIPT): the two agents whose
#: existing capability already reads the OWNER's own data (the orchestrator plans from the
#: owner's real situation; customer_support reads the owner's inbound correspondence).
PHONE_READ_HOLDERS = {"orchestrator", "customer_support"}
#: Every other firewall agent keeps its existing toolbox (least privilege).
PHONE_READ_DENIED = set(pf.AGENT_CAPABILITIES) - PHONE_READ_HOLDERS


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A real Phone Link store on disk + `PHONE_LINK_DB_DIR` pointed at it (the compose path)."""
    from app.config import settings

    root = build_phone_store(tmp_path / "Database", now=NOW)
    monkeypatch.setattr(settings, "phone_link_db_dir", str(root))
    return root


def _payload(raw: str) -> dict:
    return json.loads(raw)


def _read(arguments: dict | None = None, *, agent_type: str = "orchestrator") -> dict:
    return _payload(tool_registry.execute_tool("phone_read", arguments or {}, agent_type=agent_type))


def _add_threads(root, *, count: int) -> None:
    """Append `count` REAL conversations newer than every seeded one (test fixture, labelled)."""
    con = sqlite3.connect(root / "phone.db")
    try:
        for i in range(count):
            con.execute(
                'INSERT INTO "conversation"(thread_id, recipient_list, timestamp, msg_count, '
                "unread_count, has_rcs, checksum, phone_unread_count) VALUES (?,?,?,?,0,0,0,0)",
                (
                    900 + i,
                    f"tel:+151****015{i:02d}",
                    filetime(NOW - timedelta(minutes=50) + timedelta(seconds=i)),
                    1,
                ),
            )
        con.commit()
    finally:
        con.close()


def _add_thread_messages(root, *, count: int) -> int:
    """Append a REAL thread with `count` messages (newest first on read); return its thread id."""
    thread_id = 990
    con = sqlite3.connect(root / "phone.db")
    try:
        con.execute(
            'INSERT INTO "conversation"(thread_id, recipient_list, timestamp, msg_count, '
            "unread_count, has_rcs, checksum, phone_unread_count) VALUES (?,?,?,?,0,0,0,0)",
            (thread_id, "tel:+151****0199", filetime(NOW - timedelta(minutes=1)), count),
        )
        for i in range(1, count + 1):
            con.execute(
                'INSERT INTO "message"(message_id, thread_id, status, from_address, type, subject, '
                "body, timestamp, pc_status, subscription_id, checksum, group_id, to_address) "
                "VALUES (?,?,0,?,1,NULL,?,?,1,-1,0,NULL,NULL)",
                (
                    9000 + i,
                    thread_id,
                    "+151****0199",
                    f"extension note {i:02d}",
                    filetime(NOW - timedelta(minutes=i)),
                ),
            )
        con.commit()
    finally:
        con.close()
    return thread_id


def _all_keys(node) -> set[str]:
    """Every dict key at every depth of a JSON payload."""
    keys: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            keys.add(str(key))
            keys |= _all_keys(value)
    elif isinstance(node, list):
        for item in node:
            keys |= _all_keys(item)
    return keys


# ── the frozen interface ─────────────────────────────────────────────────────────────────────

def test_the_spec_is_the_frozen_function_spec():
    spec = next(s for s in tool_registry.TOOL_SPECS
                if s["function"]["name"] == "phone_read")
    assert spec["type"] == "function"
    function = spec["function"]
    parameters = function["parameters"]
    assert parameters["type"] == "object"
    assert set(parameters["properties"]) == {"thread_id", "limit"}
    assert parameters["required"] == []
    assert parameters["additionalProperties"] is False
    description = function["description"].lower()
    assert "code" in description, "the description must say codes are not available to tools"
    assert "not available" in description or "never" in description
    assert "phone_read" in tool_registry.TOOL_GUIDANCE


def test_the_frozen_allowlist_grants_phone_read_to_exactly_the_owner_data_readers():
    holders = {
        agent_type
        for agent_type, capability in pf.AGENT_CAPABILITIES.items()
        if "phone_read" in capability.agent_tools
    }
    assert holders == PHONE_READ_HOLDERS
    assert tool_registry.tools_for("orchestrator") == (
        "macro_snapshot", "pricing_catalog", "phone_read")
    assert tool_registry.tools_for("customer_support") == ("phone_read",)


def test_phone_read_returns_the_frozen_thread_shape_from_a_fixture_store(store):
    payload = _read()

    assert payload["ok"] is True and payload["tool"] == "phone_read"
    result = payload["result"]
    # `truncated`/`note` are the frozen interface's demanded truncation note (empty here); the
    # `messages` key is deliberately ABSENT without `thread_id`.
    assert set(result) == {"store_status", "threads", "truncated", "note"}
    assert result["store_status"] == "ok"
    assert result["truncated"] is False and result["note"] == ""
    assert [t["thread_id"] for t in result["threads"]] == [
        THREAD_MITCH, THREAD_DANA, THREAD_STRANGER]
    assert [t["who"] for t in result["threads"]] == [
        "Mitch Alvarez", "Dana Reyes", STRANGER]
    assert [t["unread"] for t in result["threads"]] == [2, 0, 1]
    assert sum(t["unread"] for t in result["threads"]) == UNREAD_TOTAL
    for thread in result["threads"]:
        assert set(thread) == {"thread_id", "who", "last_when", "unread"}
        assert isinstance(thread["thread_id"], int)
        assert thread["last_when"] and datetime.fromisoformat(thread["last_when"]).tzinfo is not None
    # A JSON null is "not supplied" (models emit it): still the threads-only shape.
    assert "messages" not in _read({"thread_id": None})["result"]


def test_phone_read_returns_a_threads_messages_in_the_frozen_four_key_shape(store):
    result = _read({"thread_id": THREAD_MITCH})["result"]

    messages = result["messages"]
    assert len(messages) == 5, "the whole thread when it fits the default limit"
    for message in messages:
        assert set(message) == {"when", "dir", "who", "text"}
        assert message["dir"] in {"in", "out"}
        assert message["when"] and datetime.fromisoformat(message["when"]).tzinfo is not None
    assert [m["dir"] for m in messages] == ["out", "in", "in", "in", "in"], "newest first"
    assert messages[0]["who"] == "Mitch Alvarez", "an outgoing row resolves to the thread peer"
    assert "Missed you at the game" in messages[2]["text"]
    assert "Order 12345 shipped" in messages[3]["text"]
    # Three seeded bodies are code-shaped (ids 7, 9, 5): the house withholding rule applies.
    withheld = [m for m in messages if m["text"] == tool_registry.PHONE_CODE_WITHHELD]
    assert len(withheld) == 3


# ── limit caps + honest truncation ──────────────────────────────────────────────────────────

def test_the_default_limit_is_five_and_a_smaller_limit_truncates_honestly(store):
    assert tool_registry.PHONE_READ_LIMIT_DEFAULT == 5
    thread_id = _add_thread_messages(store, count=14)

    result = _read({"thread_id": thread_id})["result"]
    assert len(result["messages"]) == 5, "no limit -> the frozen default of 5"
    assert result["truncated"] is True
    assert "message cap 10" in result["note"]
    assert "thread" in result["note"] and str(thread_id) in result["note"]

    small = _read({"thread_id": thread_id, "limit": 2})["result"]
    assert len(small["messages"]) == 2
    assert small["truncated"] is True

    null_limit = _read({"thread_id": thread_id, "limit": None})["result"]
    assert len(null_limit["messages"]) == 5, "a JSON null means the default, not an error"


def test_a_huge_limit_is_capped_at_ten_with_an_honest_note(store):
    thread_id = _add_thread_messages(store, count=14)

    result = _read({"thread_id": thread_id, "limit": 999})["result"]

    assert len(result["messages"]) == 10, "the message cap is 10, never the caller's number"
    assert [m["text"] for m in result["messages"]] == [
        f"extension note {i:02d}" for i in range(1, 11)]
    assert result["truncated"] is True
    assert "message cap 10" in result["note"]


def test_the_thread_list_is_the_newest_ten_and_says_when_it_cut(store):
    _add_threads(store, count=12)

    result = _read()["result"]

    assert len(result["threads"]) == 10, "threads are always the newest <= 10"
    assert result["truncated"] is True
    assert "thread cap 10" in result["note"]
    # The 12 appended conversations are newer than every seeded one, so they fill the window.
    assert all(900 <= t["thread_id"] <= 911 for t in result["threads"])


def test_an_unknown_thread_is_an_honest_empty_not_a_refusal(store):
    """The store WAS read and holds nothing for that id: `[]` is honest (vs the 503 absence)."""
    result = _read({"thread_id": 424242})["result"]

    assert result["messages"] == []
    assert result["truncated"] is False and result["note"] == ""


# ── refusals: absent / corrupt store, hostile arguments ─────────────────────────────────────

def test_an_absent_store_is_a_named_refusal_never_an_empty_inbox(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "phone_link_db_dir", str(tmp_path / "no-such-mount"))

    payload = _read()

    assert payload["ok"] is False
    error = payload["error"]
    assert error["code"] == E_PHONE_UNAVAILABLE
    assert "store" in error["message"].lower(), "the service's honest message rides the refusal"
    assert error["how_to_fix"], "a refusal without a fix hint is a dead end for the operator"
    assert error["context"]["tool"] == "phone_read"
    assert error["context"]["agent"] == "orchestrator"


def test_a_corrupt_store_is_a_named_refusal_not_a_crash(tmp_path, monkeypatch):
    from app.config import settings

    root = build_phone_store(tmp_path / "Database", now=NOW)
    (root / "phone.db").write_bytes(b"this is not a sqlite database")
    monkeypatch.setattr(settings, "phone_link_db_dir", str(root))

    payload = _read({"thread_id": THREAD_MITCH})

    assert payload["ok"] is False
    assert payload["error"]["code"] == E_PHONE_UNAVAILABLE


def test_a_corrupt_timestamp_is_the_same_named_refusal(store):
    """`filetime_to_local` refuses an unrepresentable value BY DESIGN; the tool names the store."""
    con = sqlite3.connect(store / "phone.db")
    try:
        con.execute('UPDATE "message" SET timestamp = ? WHERE message_id = 5', ("garbage",))
        con.commit()
    finally:
        con.close()

    payload = _read({"thread_id": THREAD_MITCH})

    assert payload["ok"] is False
    assert payload["error"]["code"] == E_PHONE_UNAVAILABLE
    assert "ValueError" in payload["error"]["message"]


@pytest.mark.parametrize("arguments", [
    {"thread_id": -1},
    {"thread_id": 0},
    {"thread_id": True},
    {"thread_id": "eleven"},
    {"thread_id": 10**20},
    {"limit": 0},
    {"limit": -3},
    {"limit": "many"},
])
def test_hostile_arguments_are_refused_by_name(store, arguments):
    payload = _read(arguments)

    assert payload["ok"] is False
    assert payload["error"]["code"] == E_VALIDATION
    key = next(iter(arguments))
    assert key in payload["error"]["message"]


def test_an_unknown_tool_still_names_the_known_set():
    payload = _payload(tool_registry.execute_tool("phone_dial", {}, agent_type="orchestrator"))

    assert payload["ok"] is False
    assert payload["error"]["code"] == E_VALIDATION
    message = payload["error"]["message"]
    assert "phone_read" in message and "web_fetch" in message, "the known set includes the new tool"


# ── codes are absent BY CONSTRUCTION (hostile probe) ────────────────────────────────────────

def test_no_codes_key_exists_at_any_depth_and_no_code_value_escapes(store, monkeypatch):
    """The hostile probe: serialise the FULL thread payload and hunt for codes.

    Two independent proofs:
      1. no key named `code`/`codes` exists anywhere in the result (there is no codes surface);
      2. the fixture's real code values — and an outgoing code-shaped body — cannot appear in
         the serialized JSON, because a code-shaped body is withheld (the `phone_activity`
         precedent, not a silent edit: the marker names the withholding).
    The `codes` reader is also trip-wired: if the tool ever reached it, the assertion fires.
    """
    def _tripwire(**_kwargs):  # pragma: no cover - only fires on a violated contract
        raise AssertionError("phone_read must never reach phone_link_service.codes")

    monkeypatch.setattr(phone_link_service, "codes", _tripwire)

    payload = _read({"thread_id": THREAD_MITCH})

    assert payload["ok"] is True
    keys = _all_keys(payload["result"])
    assert "code" not in keys and "codes" not in keys
    serialized = json.dumps(payload)
    for value in (CODE_VALUE, OLD_CODE_VALUE, "778899"):
        assert value not in serialized, f"a verification code ({value}) escaped the tool"


def test_the_codes_reader_is_not_part_of_the_tool_surface():
    import inspect

    source = inspect.getsource(tool_registry._run_phone_read)
    assert ".codes(" not in source and "phone.codes" not in source


# ── the firewall: denied agents + H-C1 ─────────────────────────────────────────────────────

@pytest.mark.parametrize("agent_type", sorted(PHONE_READ_DENIED))
def test_an_agent_outside_the_allowlist_gets_the_standard_refusal(store, agent_type):
    payload = _read({}, agent_type=agent_type)

    assert payload["ok"] is False
    error = payload["error"]
    assert error["code"] == E_POLICY_DENIED
    assert "phone_read" in error["message"] and agent_type in error["message"]
    assert error["context"]["policy_code"] == "POLSIA-E114"


def test_an_unknown_agent_is_refused_by_name(store):
    payload = _read({}, agent_type="no_such_agent")

    assert payload["ok"] is False
    assert payload["error"]["code"] == E_POLICY_DENIED
    assert "no_such_agent" in payload["error"]["message"]


@pytest.mark.asyncio
async def test_h_c1_lists_phone_read_exactly_where_the_allowlist_says(api_client, auth_headers):
    resp = await api_client.get("/api/v1/ops/capabilities", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()

    tools_by_agent = {
        row["agent_type"]: [tool["name"] for tool in row["tools"]]
        for row in data["agents"]
    }
    holders = {agent for agent, names in tools_by_agent.items() if "phone_read" in names}
    assert holders == PHONE_READ_HOLDERS
    assert tools_by_agent["orchestrator"] == [
        "macro_snapshot", "pricing_catalog", "phone_read"]
    assert tools_by_agent["customer_support"] == ["phone_read"]

    registry_tools = {tool["name"]: tool["description"] for tool in data["registry_tools"]}
    assert "phone_read" in registry_tools
    assert registry_tools["phone_read"] == next(
        spec["function"]["description"] for spec in tool_registry.TOOL_SPECS
        if spec["function"]["name"] == "phone_read"
    )
