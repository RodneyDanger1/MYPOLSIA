"""V22 / L1 — `agent_runs.tools_used` is PERSISTED, exposed and honest (the V21 finding closed).

WHAT V21 PROVED (and this suite re-proves from the other side): the DeepSeek runtime writes
`envelope["tools_used"]` for a tool-enabled run (`deepseek_runtime.py:1225`) but the persistence
boundary dropped it — no column, no `RunUsage` field, no `RUN_META_KEYS` entry.  This lane closes
the chain and pins it end to end:

  envelope -> `accounting.usage_from_envelope` -> `RunUsage.as_dict` -> `_apply_run_usage`
  -> `agent_runs.tools_used` -> `GET /api/v1/runs/{id}` -> `tools_used: string[] | null`

THE THREE STATES (the whole point of the lane — they must never collapse):

  * `null`  — NEVER RECORDED: the envelope had no `tools_used` key (a tool-less run, mock mode, or
              a legacy row written before this migration).  Never fabricated into `[]`.
  * `[]`    — RECORDED AND NONE: the runtime had tools available and the model called none.  A
              measured zero, written as a real empty JSON array.
  * `["…"]` — RECORDED NAMES, bounded to 50 entries; when the source list overflows, the LAST
              entry is an honest truncation marker (never a fake tool name that counts as a tool).

What is pinned here:
  1. `usage_from_envelope` reads the list, distinguishes absent/null (None) from `[]`, and refuses
     junk shapes (a string, a list of non-strings) instead of coercing.
  2. The 50-entry cap + truncation marker, and no marker on a list that fits.
  3. The proven write seam `_apply_run_usage` writes the COLUMN (and the `usage_json` audit copy),
     leaves the column NULL when the envelope was absent, and keeps `[]` as `[]`.
  4. `RUN_META_KEYS` carries the key (the documented result->column list cannot drift again).
  5. The ORM column exists and round-trips a list, `[]`, and NULL through SQLite.
  6. `GET /api/v1/runs/{id}` serves all three states exactly; a malformed at-rest value reads as
     null and the row STILL serves (the U5 per-row junk rule).
  7. The list route's frozen row keys are UNCHANGED — tools_used is detail-only.
  8. Migration 0010 is additive + reversible on SQLite: upgrade adds the nullable column, an
     existing row keeps its data and reads NULL, downgrade removes exactly that column and keeps
     the row; re-upgrade restores it byte-for-byte.
"""
from __future__ import annotations

import pathlib
import sqlite3
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.agents.accounting import (
    TOOLS_USED_MAX,
    bounded_tools_used,
    usage_from_envelope,
)
from app.models.task import AgentRun

RUNS_PATH = "/api/v1/runs"
DETAIL_PATH = "/api/v1/runs/{run_id}"

#: The frozen U5 list row key set — the detail route must NOT widen it (tools_used is detail-only).
ROW_KEYS = [
    "id", "task_id", "task_title", "agent_type", "run_type", "status", "attempt",
    "started_at", "ended_at", "duration_secs", "tokens_input", "tokens_output",
    "tokens_used", "cost_usd", "cost_source", "pricing_version",
]

#: The detail adds exactly the V21 counters plus the V22 tool list.
DETAIL_EXTRA_KEYS = {"num_turns", "is_error", "tools_used"}

BACKEND = pathlib.Path(__file__).resolve().parents[2]


async def _run(db, *, tools_used=None) -> AgentRun:
    run = AgentRun(agent_type="content", run_type="task", status="completed", tools_used=tools_used)
    db.add(run)
    await db.flush()
    return run


# ── the envelope seam: absent vs [] vs names ─────────────────────────────────────────────────


def test_envelope_absent_is_none_and_empty_list_is_a_measured_zero():
    """The two states the lane exists for: absent -> None (never recorded); [] -> [] (recorded)."""
    assert usage_from_envelope({"session_id": "s"}).tools_used is None
    assert usage_from_envelope({"session_id": "s", "tools_used": None}).tools_used is None
    assert usage_from_envelope({"session_id": "s", "tools_used": []}).tools_used == []
    assert usage_from_envelope({"session_id": "s", "tools_used": ["web_search", "calculator"]}).tools_used == [
        "web_search",
        "calculator",
    ]


def test_junk_tool_shapes_are_unmeasured_never_coerced():
    """A string is not a list; junk-only entries are not names.  Both stay None (never fabricated)."""
    assert usage_from_envelope({"tools_used": "web_search"}).tools_used is None
    assert usage_from_envelope({"tools_used": [1, None, True]}).tools_used is None
    assert usage_from_envelope({"tools_used": {}}).tools_used is None
    # A real name next to junk keeps the real name; the junk entry is dropped, not stringified.
    assert usage_from_envelope({"tools_used": ["web_search", 7]}).tools_used == ["web_search"]


def test_tool_list_is_capped_with_an_honest_truncation_marker():
    """51+ names -> exactly TOOLS_USED_MAX entries; the last one SAYS it is a truncation marker."""
    names = [f"tool_{i}" for i in range(TOOLS_USED_MAX + 10)]
    bounded = bounded_tools_used(names)
    assert bounded is not None
    assert len(bounded) == TOOLS_USED_MAX, "the stored list must never exceed the cap"
    assert bounded[:-1] == names[: TOOLS_USED_MAX - 1], "kept names stay in order"
    marker = bounded[-1]
    assert "11" in marker and "capped" in marker.lower(), (
        f"the marker must state how many names were dropped, got {marker!r}"
    )
    # A list that FITS carries no marker at all.
    exact = bounded_tools_used([f"t{i}" for i in range(TOOLS_USED_MAX)])
    assert exact is not None and len(exact) == TOOLS_USED_MAX and exact[-1] == f"t{TOOLS_USED_MAX - 1}"


def test_as_dict_carries_tools_only_when_the_envelope_did():
    """`as_dict` must not invent a `tools_used: None` key for a tool-less envelope."""
    assert "tools_used" not in usage_from_envelope({"session_id": "s"}).as_dict()
    assert usage_from_envelope({"tools_used": []}).as_dict()["tools_used"] == []
    assert usage_from_envelope({"tools_used": ["web_search"]}).as_dict()["tools_used"] == ["web_search"]


# ── the proven write seam: _apply_run_usage ───────────────────────────────────────────────────


def test_run_meta_keys_carries_tools_used():
    """The documented result->column key list must name the new key, or the seam drifts again."""
    from app.agents.base_agent import RUN_META_KEYS

    assert "tools_used" in RUN_META_KEYS


def test_apply_run_usage_writes_the_column_from_the_envelope_chain():
    """envelope -> RunUsage.as_dict -> _apply_run_usage -> the column.  A real [], not None."""
    from celery_app.tasks.agent_tasks import _apply_run_usage

    row = SimpleNamespace(tools_used=None, usage_json=None)
    result = usage_from_envelope(
        {"session_id": "s", "tools_used": ["web_search", "calculator"]}
    ).as_dict()
    _apply_run_usage(row, result)
    assert row.tools_used == ["web_search", "calculator"]
    # The audit copy rides usage_json too (RUN_META_KEYS' "record the rest" loop).
    assert row.usage_json is not None and row.usage_json["tools_used"] == ["web_search", "calculator"]

    empty_row = SimpleNamespace(tools_used=None, usage_json=None)
    _apply_run_usage(empty_row, usage_from_envelope({"tools_used": []}).as_dict())
    assert empty_row.tools_used == [], "a recorded empty list must survive as [], never NULL"

    absent_row = SimpleNamespace(tools_used=None, usage_json=None)
    _apply_run_usage(absent_row, usage_from_envelope({"session_id": "s"}).as_dict())
    assert absent_row.tools_used is None, "envelope absent -> NULL, never a fabricated []"

    junk_row = SimpleNamespace(tools_used=None, usage_json=None)
    _apply_run_usage(junk_row, {"tools_used": "web_search"})
    assert junk_row.tools_used is None


# ── the ORM column ───────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_orm_column_round_trips_names_empty_and_null(async_db_session):
    db = async_db_session
    named = await _run(db, tools_used=["web_search", "calculator"])
    empty = await _run(db, tools_used=[])
    legacy = await _run(db)
    await db.flush()

    fetched = await db.get(AgentRun, named.id)
    assert fetched is not None and fetched.tools_used == ["web_search", "calculator"]
    fetched_empty = await db.get(AgentRun, empty.id)
    assert fetched_empty is not None and fetched_empty.tools_used == []
    fetched_legacy = await db.get(AgentRun, legacy.id)
    assert fetched_legacy is not None and fetched_legacy.tools_used is None


# ── the wire ────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_detail_serves_the_three_states_and_keeps_the_list_contract(
    api_client, auth_headers, async_db_session
):
    """GET /runs/{id}: names, [], and null each served EXACTLY; the list route stays frozen."""
    db = async_db_session
    named = await _run(db, tools_used=["web_search", "calculator"])
    empty = await _run(db, tools_used=[])
    legacy = await _run(db)

    named_body = (await api_client.get(DETAIL_PATH.format(run_id=named.id), headers=auth_headers)).json()
    empty_body = (await api_client.get(DETAIL_PATH.format(run_id=empty.id), headers=auth_headers)).json()
    legacy_body = (await api_client.get(DETAIL_PATH.format(run_id=legacy.id), headers=auth_headers)).json()

    assert named_body["tools_used"] == ["web_search", "calculator"]
    assert empty_body["tools_used"] == []
    assert legacy_body["tools_used"] is None
    assert set(named_body) == set(ROW_KEYS) | DETAIL_EXTRA_KEYS

    # The ledger list is NOT bloated with the detail-only tool list.
    listing = (await api_client.get(RUNS_PATH, headers=auth_headers)).json()
    assert listing["total"] == 3
    assert all(list(item) == ROW_KEYS for item in listing["items"])


@pytest.mark.asyncio
async def test_malformed_at_rest_tools_used_reads_null_and_still_serves(
    api_client, auth_headers, async_db_session
):
    """One bad at-rest value must not refuse the detail (the U5 per-row junk rule)."""
    db = async_db_session
    run = await _run(db, tools_used=["web_search"])
    await db.execute(
        text("UPDATE agent_runs SET tools_used = '{not json' WHERE id = :i"), {"i": run.id}
    )
    await db.flush()

    resp = await api_client.get(DETAIL_PATH.format(run_id=run.id), headers=auth_headers)
    assert resp.status_code == 200, resp.text[:300]
    assert resp.json()["tools_used"] is None


@pytest.mark.asyncio
async def test_non_string_entries_at_rest_serve_only_the_real_names(
    api_client, auth_headers, async_db_session
):
    db = async_db_session
    run = await _run(db, tools_used=["web_search"])
    await db.execute(
        text("UPDATE agent_runs SET tools_used = '[\"web_search\", 7, null]' WHERE id = :i"),
        {"i": run.id},
    )
    await db.flush()

    body = (await api_client.get(DETAIL_PATH.format(run_id=run.id), headers=auth_headers)).json()
    assert body["tools_used"] == ["web_search"]


# ── the migration (SQLite, in-process alembic — the R17 pattern) ─────────────────────────────


def _snapshot(db_path: pathlib.Path) -> dict[tuple[str, str], str | None]:
    con = sqlite3.connect(db_path.as_posix())
    try:
        return {
            (kind, name): sql
            for kind, name, sql in con.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name")
        }
    finally:
        con.close()


def _scalar(db_path: pathlib.Path, sql: str):
    con = sqlite3.connect(db_path.as_posix())
    try:
        return con.execute(sql).fetchone()[0]
    finally:
        con.close()


def _columns(db_path: pathlib.Path, table: str) -> list[tuple[str, str, bool, str | None]]:
    con = sqlite3.connect(db_path.as_posix())
    try:
        return [
            (row[1], row[2], bool(row[3]), row[4])
            for row in con.execute(f"PRAGMA table_info({table})")
        ]
    finally:
        con.close()


@pytest.fixture
def alembic_db(monkeypatch, tmp_path):
    """A tmp SQLite database plus the alembic Config env.py reads (`settings.database_url`)."""
    from alembic import config as alembic_config
    from app.config import settings

    db_path = tmp_path / "v22_tools_used.sqlite"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    monkeypatch.setattr(settings, "database_url", url)
    cfg = alembic_config.Config()
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg, db_path


def test_migration_0010_is_additive_and_reversible_on_sqlite(alembic_db):
    """upgrade -> nullable column + row intact; downgrade -> column gone, row intact; re-upgrade."""
    from alembic import command

    cfg, db_path = alembic_db
    command.upgrade(cfg, "0009")

    con = sqlite3.connect(db_path.as_posix())
    try:
        assert "tools_used" not in [col[1] for col in con.execute("PRAGMA table_info(agent_runs)")]
        con.execute(
            "INSERT INTO agent_runs (agent_type, run_type, status) VALUES ('content', 'task', 'completed')"
        )
        con.commit()
    finally:
        con.close()
    at_0009 = _snapshot(db_path)

    command.upgrade(cfg, "0010")
    columns = _columns(db_path, "agent_runs")
    tools = [col for col in columns if col[0] == "tools_used"]
    assert len(tools) == 1, "0010 must add exactly one column"
    _name, type_name, not_null, default = tools[0]
    assert not_null is False, "the column is NULLABLE: NULL = never recorded"
    assert default is None, "no default: a legacy row must stay NULL, not become []"
    assert type_name.upper().startswith("JSON"), f"portable JSON column, got {type_name!r}"
    assert _scalar(db_path, "SELECT COUNT(*) FROM agent_runs") == 1
    assert _scalar(db_path, "SELECT tools_used FROM agent_runs") is None

    # A recorded list survives a downgrade+re-upgrade cycle (the column is dropped, the ROW is not).
    con = sqlite3.connect(db_path.as_posix())
    try:
        con.execute(
            "UPDATE agent_runs SET tools_used = '[\"web_search\", \"calculator\"]' WHERE id = (SELECT MIN(id) FROM agent_runs)"
        )
        con.commit()
    finally:
        con.close()

    command.downgrade(cfg, "0009")
    assert "tools_used" not in [col[0] for col in _columns(db_path, "agent_runs")]
    assert _scalar(db_path, "SELECT COUNT(*) FROM agent_runs") == 1, "downgrade must not lose rows"
    at_0009_again = _snapshot(db_path)
    assert set(at_0009_again) <= set(at_0009) | {
        ("table", "alembic_version")
    }, "downgrade must remove exactly the column, not rebuild other objects"
    assert at_0009_again[("table", "agent_runs")] == at_0009[("table", "agent_runs")], (
        "the agent_runs DDL after downgrade must be byte-identical to the 0009 head"
    )

    command.upgrade(cfg, "0010")
    assert "tools_used" in [col[0] for col in _columns(db_path, "agent_runs")]
    assert _scalar(db_path, "SELECT COUNT(*) FROM agent_runs") == 1
    assert _scalar(db_path, "SELECT tools_used FROM agent_runs") is None, (
        "re-upgrading a legacy row must read NULL (never a fabricated [])"
    )
