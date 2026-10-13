"""T10 / seat S5 — the four silent fallbacks outside `base_agent`: S-03…S-06.

Each test pins the RECORDING, not merely "did not raise": a caplog assertion on a stable slug
(`where_filter_dropped`, `priority_coerced`), the planner's own warn list, or — S-04 since T4 —
the named refusal itself.  The RED legs (`seats/S5-silent/raw/redleg-S03..S06.sh`, re-runnable
on the settled tree) revert each source file to its pre-fix bytes; every test here must then FAIL.

What each site means:

* S-03 `memory_service.search_memory` — a collection whose client rejects `where` answers the
  retry WITHOUT the category filter: the drop is logged (`code=where_filter_dropped`, naming
  the query and the lost category) and every hit of that retry is tagged
  `fallback: "sql_unfiltered"`; the normal path must carry no tag.
* S-04 `timeutil.company_tz` — REPAIRED by T4 (seat S2-t4t14): an unknown `COMPANY_TIMEZONE`
  is a named `E_CONFIG` refusal (old expectation: a `code=invalid_company_timezone` warning +
  `ZoneInfo("UTC")` fallback — superseded); a valid zone still records nothing.
* S-05 `daily_cycle._morning_cycle` — a PRESENT priority the `int(... or 3)` coercion rewrites
  is logged (`code=priority_coerced`, naming the entry and the raw value); an ABSENT priority
  is the output schema's documented default (`TaskSpec.priority` — default=3,
  app/schemas/agent_outputs.py) and stays silent.
* S-06 `planner.build_plan` — a spec whose estimate pairs are absent from BOTH key spellings is
  named in the plan's warn list; a measured `0` and the existing non-numeric warn are unchanged.

The S-05 harness is the file-backed-SQLite pattern proven in `tests/unit/test_daily_cycle_window.py`
(self-contained — fixtures do not cross directories): one FILE behind the app-wide session factory,
the orchestrator patched at `crew_factory.run_agent_for_task`, `daily_cycle.group` replaced by a
recorder, the clock pinned via `daily_cycle.now_utc`, and the resulting task rows read back with
the stdlib sqlite3 driver (the reader is not the writer).
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

import app.models  # noqa: F401  (register every table on Base.metadata before create_all)
from app.core import timeutil
from app.core.errors import E_CONFIG, PolsiaError
from app.orchestration.planner import build_plan
from app.services import memory_service
from celery_app.tasks import daily_cycle
from celery_app.tasks._common import run_async

# ── S-03 / memory_service: the where-filter drop ──────────────────────────────


def _raw_hits() -> dict:
    """The shape a Chroma `query()` returns (documents/metadatas/ids/distances, one batch)."""
    return {
        "documents": [["referral program drives sign-ups", "pricing anchors at $99"]],
        "metadatas": [[
            {"category": "strategy", "title": "Growth lever", "source": "test"},
            {"category": "strategy", "title": "Pricing", "source": "test"},
        ]],
        "ids": [["mock-id-1", "mock-id-2"]],
        "distances": [[0.1, 0.2]],
    }


class _CollectionRejectingWhere:
    """A collection whose client rejects the `where` kwarg with TypeError — S-03's case."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def query(self, **kwargs):
        self.calls.append(kwargs)
        if "where" in kwargs:
            raise TypeError("query() got an unexpected keyword argument 'where'")
        return _raw_hits()


class _WorkingCollection:
    """A collection that accepts `where` — the honest path."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def query(self, **kwargs):
        self.calls.append(kwargs)
        return _raw_hits()


@pytest.mark.asyncio
async def test_s03_where_filter_drop_is_recorded_and_hits_tagged(
    async_db_session, monkeypatch, caplog
):
    """The unfiltered retry cannot pass as a filtered read: tag every hit + log the drop.

    Pins: the first `query` carries `where={"category": "strategy"}` and raises TypeError; the
    retry drops ONLY `where` (same `query_texts`); every returned hit carries
    `fallback: "sql_unfiltered"`; and the log names the slug, the query and the lost category.
    """
    fake = _CollectionRejectingWhere()
    monkeypatch.setattr(memory_service, "get_collection", lambda: fake)

    with caplog.at_level(logging.WARNING, logger="app.services.memory_service"):
        results = await memory_service.search_memory(
            async_db_session, "referral growth", n_results=5, category="strategy"
        )

    assert len(fake.calls) == 2, "exactly one retry"
    assert fake.calls[0]["where"] == {"category": "strategy"}
    assert "where" not in fake.calls[1], "the retry drops `where` (that is the defect's path)"
    assert fake.calls[1]["query_texts"] == ["referral growth"]

    assert results, "the fake collection returned hits"
    assert all(hit.get("fallback") == "sql_unfiltered" for hit in results), results
    messages = [
        r.getMessage() for r in caplog.records if r.name == "app.services.memory_service"
    ]
    assert any(
        "code=where_filter_dropped" in m and "'referral growth'" in m and "'strategy'" in m
        for m in messages
    ), messages


@pytest.mark.asyncio
async def test_s03_normal_path_hits_carry_no_fallback_tag(
    async_db_session, monkeypatch, caplog
):
    """The honest path is untouched: `where` is sent once, no tag, no drop record."""
    fake = _WorkingCollection()
    monkeypatch.setattr(memory_service, "get_collection", lambda: fake)

    with caplog.at_level(logging.WARNING, logger="app.services.memory_service"):
        results = await memory_service.search_memory(
            async_db_session, "referral growth", n_results=5, category="strategy"
        )

    assert len(fake.calls) == 1 and fake.calls[0]["where"] == {"category": "strategy"}
    assert results and all("fallback" not in hit for hit in results), results
    assert "where_filter_dropped" not in caplog.text


# ── S-04 / timeutil: the invalid COMPANY_TIMEZONE refusal (repaired by T4) ────


def test_s04_invalid_company_timezone_is_a_named_refusal(monkeypatch):
    """REPAIRED by T4 (seat S2-t4t14): an unknown zone is now a named refusal, no fallback.

    Old expectation (W3-refusals / S-04): with `settings.company_timezone = "Not/AZone"`,
    `company_tz()` returned `ZoneInfo("UTC")` and logged `code=invalid_company_timezone`
    naming the bad value.  New expectation (T4): the same input raises
    `PolsiaError(E_CONFIG)`, whose message names the bad value — the raise IS the proof that
    no fallback value ever reaches a caller, so T2's morning window fails loudly instead of
    computing the wrong day.  Repair sanctioned by the S2-t4t14 brief (COMMON rule 4).
    """
    from app.config import settings

    monkeypatch.setattr(settings, "company_timezone", "Not/AZone")

    with pytest.raises(PolsiaError) as excinfo:
        timeutil.company_tz()

    assert excinfo.value.code == E_CONFIG
    assert "Not/AZone" in excinfo.value.message, "the refusal names the value that is wrong"
    assert "UTC" not in excinfo.value.message, "no quiet default hides in the refusal text"


def test_s04_valid_zone_records_nothing(monkeypatch, caplog):
    """A valid `COMPANY_TIMEZONE` returns its ZoneInfo and records nothing."""
    from app.config import settings

    monkeypatch.setattr(settings, "company_timezone", "America/New_York")

    with caplog.at_level(logging.WARNING, logger="app.core.timeutil"):
        tz = timeutil.company_tz()

    assert tz == ZoneInfo("America/New_York")
    assert "invalid_company_timezone" not in caplog.text


# ── S-05 / daily_cycle: the silent priority coercion ─────────────────────────


class _GroupRecorder:
    """Stands in for `daily_cycle.group`: records the fan-out, contacts no broker."""

    def __init__(self) -> None:
        self.batches: list[list] = []
        self.apply_async_count = 0

    def __call__(self, sigs):
        self.batches.append(list(sigs))
        return self

    def apply_async(self) -> None:
        self.apply_async_count += 1


@pytest.fixture(autouse=True)
def quiet_redis(monkeypatch):
    """No network: the activity publish goes to a mock (same shape as the queue conftest)."""
    mock = AsyncMock()
    mock.publish = AsyncMock(return_value=1)
    monkeypatch.setattr("app.core.events.get_redis", lambda: mock)
    return mock


@pytest.fixture
def cycle_db(monkeypatch, tmp_path):
    """Point the app-wide session factory at a fresh file-backed SQLite database.

    Same harness as tests/unit/test_daily_cycle_window.py (self-contained — fixtures do not
    cross directories).  The cycle task runs on the shared process loop, so the engine is
    built and disposed on that loop too.
    """
    from app.core import database

    path = tmp_path / "silent_fallbacks.sqlite"
    monkeypatch.setattr(
        database.settings, "database_url", f"sqlite+aiosqlite:///{path.as_posix()}"
    )

    async def _build() -> None:
        await database.dispose_engine()
        engine = database.get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(database.Base.metadata.create_all)

    run_async(_build())
    yield path
    run_async(database.dispose_engine())


def _task_priorities(path: Path) -> dict[str, int]:
    """Read the tasks table with the stdlib driver — an assertion about the FILE, not the ORM."""
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    try:
        return {
            row["title"]: row["priority"]
            for row in con.execute("SELECT title, priority FROM tasks").fetchall()
        }
    finally:
        con.close()


def test_s05_malformed_and_zero_priority_are_coerced_with_a_record(cycle_db, monkeypatch, caplog):
    """A PRESENT priority the coercion rewrites is recorded; an absent one is not.

    Evidence for the absent case (read from the tree, not assumed): the orchestrator's typed
    output schema defaults priority (`TaskSpec.priority` — default=3, agent_outputs.py), so
    "no priority key" is the documented default, not malformed — it must stay silent.

    Pins: `"abc"` -> row priority 3 + `code=priority_coerced` naming the entry and `'abc'`;
    `0` (present, falsy, swallowed by `or`) -> row priority 3 + recorded; the measured value
    `2` -> row priority 2, silent; absent -> row priority 3, silent.
    """
    import app.agents.crew_factory as crew_factory
    from app.config import settings

    monkeypatch.setattr(settings, "company_timezone", "UTC")
    monkeypatch.setattr(settings, "morning_cycle_hour", 7)

    entries = [
        {"title": "Malformed priority probe", "agent_type": "ads_management", "priority": "abc"},
        {"title": "Zero priority probe", "agent_type": "email_outreach", "priority": 0},
        {"title": "Numeric priority probe", "agent_type": "code_generation", "priority": 2},
        {"title": "Defaulted priority probe", "agent_type": "business_planning"},
    ]
    monkeypatch.setattr(
        crew_factory,
        "run_agent_for_task",
        MagicMock(return_value={"summary": "priority probe plan", "tasks": entries}),
    )
    monkeypatch.setattr(daily_cycle, "now_utc", lambda: datetime(2026, 10, 12, 7, 30, tzinfo=UTC))
    recorder = _GroupRecorder()
    monkeypatch.setattr(daily_cycle, "group", recorder)

    with caplog.at_level(logging.WARNING, logger="celery_app.tasks.daily_cycle"):
        result = daily_cycle.run_morning_cycle()

    assert result["status"] == "completed"
    assert result["created"] == 7, "3 fixed tasks + 4 planned entries"
    assert recorder.apply_async_count == 1

    prios = _task_priorities(cycle_db)
    assert prios["Malformed priority probe"] == 3, "coerced — the coercion itself is unchanged"
    assert prios["Zero priority probe"] == 3, "a present falsy value is rewritten by the `or` guard"
    assert prios["Numeric priority probe"] == 2, "a real value is kept"
    assert prios["Defaulted priority probe"] == 3, "the schema's documented default"

    messages = [
        r.getMessage()
        for r in caplog.records
        if r.name == "celery_app.tasks.daily_cycle" and "code=priority_coerced" in r.getMessage()
    ]
    assert len(messages) == 2, messages
    assert any("'Malformed priority probe'" in m and "'abc'" in m for m in messages), messages
    assert any("'Zero priority probe'" in m and " 0 -> 3" in m for m in messages), messages
    assert not any("Numeric priority probe" in m for m in messages)
    assert not any("Defaulted priority probe" in m for m in messages), (
        "an absent priority is the output schema's default — it must stay silent"
    )


# ── S-06 / planner: the silent missing-estimate default ──────────────────────


def test_s06_missing_estimates_are_named_in_the_plan_warnings():
    """A spec without estimates is recorded in the plan's warn list, defaults unchanged.

    Pins: the exact warn text names the node, and the node still carries the planner's
    defaults ($0.00 / 60s — the budget gate's semantics are not moved).
    """
    plan = build_plan(
        "2026-10-12", [{"title": "No estimate probe", "agent_type": "ads_management"}]
    )

    assert any(
        w == "node 'No estimate probe': no cost/duration estimate provided; "
        "defaulted to $0.00 / 60s"
        for w in plan.warnings
    ), plan.warnings
    node = next(iter(plan.nodes.values()))
    assert node.est_cost_usd == 0.0 and node.est_duration_s == 60.0, "the defaults are unchanged"


def test_s06_present_estimates_and_measured_zero_stay_silent():
    """A spec carrying its estimates — including a measured `0` — is not 'missing'."""
    priced = build_plan(
        "2026-10-12",
        [{
            "title": "Priced probe",
            "agent_type": "ads_management",
            "estimate_usd": 1.25,
            "estimate_seconds": 90,
        }],
    )
    assert not any("no cost/duration estimate provided" in w for w in priced.warnings), (
        priced.warnings
    )
    priced_node = next(iter(priced.nodes.values()))
    assert priced_node.est_cost_usd == 1.25 and priced_node.est_duration_s == 90.0

    zero = build_plan(
        "2026-10-12",
        [{
            "title": "Zero cost probe",
            "agent_type": "ads_management",
            "estimate_usd": 0,
            "estimate_seconds": 60,
        }],
    )
    assert not any("no cost/duration estimate provided" in w for w in zero.warnings), (
        zero.warnings
    )
    zero_node = next(iter(zero.nodes.values()))
    assert zero_node.est_cost_usd == 0.0, "a measured zero must never read as 'missing'"


def test_s06_non_numeric_estimate_keeps_the_existing_warning_only():
    """The non-numeric path is unchanged: its existing warn fires, the new one does not."""
    plan = build_plan(
        "2026-10-12",
        [{
            "title": "Bad estimate probe",
            "agent_type": "ads_management",
            "estimate_usd": "abc",
            "estimate_seconds": 30,
        }],
    )
    assert any(
        w == "node 'Bad estimate probe': non-numeric estimate coerced to defaults"
        for w in plan.warnings
    ), plan.warnings
    assert not any("no cost/duration estimate provided" in w for w in plan.warnings), plan.warnings
