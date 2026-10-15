"""Wave V12 / G2 — the automation frequency contract (closer C1).

What this file pins (the frozen interface of `app/services/automation_presets.py`):

* The PRESET TABLE mirrors the ORIGINAL's start-modal evidence — Cold Outreach offers
  ONCE/DAILY/WEEKDAYS/WEEKLY (`agent2-code/cap-07.md`), Support offers ONCE/DAILY
  (`cap-08.md`), Meta Ads setup "creates a Meta Ads - Daily task"
  (help.polsia.com/how-to-run-meta-ads, fetched 2026-10-15) — and every frequency value is a
  member of the task-schedules store's value set (`ScheduleCadence`), so the table cannot
  drift from what the store can persist.
* `estimate_runs_per_month` is COUNTED over a fixed 30-day window (2024-01-01, a Monday) and
  never a hand-typed number: daily = 30 days, weekdays = 22 occurrences, weekly = 4-5 by
  weekday, once = 1. The original's modal printed `30/22/4 TASKS/MO`; 30 and 22 must fall out
  of the count, not out of this file's constants.
* `build_schedule_spec` DELEGATES the next-run math to `schedule_service.compute_next_run`
  (the DST-safe, company-tz authority) — the spec's `next_run_at` is byte-identical to that
  function's answer — and its output carries a `schedule_create` dict whose keys are exactly
  what `schedule_service.create_schedule` accepts (name, prompt, cadence, hour, weekday).
* Refusals are BY NAME (`E_VALIDATION` + non-empty how_to_fix): unknown automation, a
  frequency the automation does not offer, and (propagated from the store) a weekly spec with
  no weekday / an out-of-range hour.
"""
from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app.core.errors import E_VALIDATION, PolsiaError
from app.models.schedule import ScheduleCadence, schedule_cadence_values
from app.services.automation_presets import (
    DEFAULTS,
    PRESETS,
    build_schedule_spec,
    estimate_runs_per_month,
    list_presets,
)
from app.services.schedule_service import compute_next_run

NOW = datetime(2026, 10, 15, 15, 0, tzinfo=UTC)  # a Thursday
CHI = ZoneInfo("America/Chicago")

_SCHEDULE_CREATE_KEYS = {"name", "prompt", "cadence", "hour", "weekday"}


# ── the preset table ──────────────────────────────────────────────────────────


def test_preset_table_matches_captured_evidence() -> None:
    assert PRESETS["cold_outreach"] == ("once", "daily", "weekdays", "weekly")
    assert PRESETS["support"] == ("once", "daily")
    assert PRESETS["meta_ads_daily"] == ("daily",)
    # every offered frequency is a value the schedules store can persist (no drift)
    store_values = set(schedule_cadence_values())
    for frequencies in PRESETS.values():
        assert set(frequencies) <= store_values


def test_defaults_exist_for_every_automation() -> None:
    assert set(DEFAULTS) == set(PRESETS)
    for name, prompt in DEFAULTS.values():
        assert name.strip() and prompt.strip()


# ── the counted estimate (never hand-typed) ───────────────────────────────────


def test_daily_and_weekdays_estimates_are_counted() -> None:
    daily = estimate_runs_per_month("daily")
    assert daily["runs_per_month_min"] == daily["runs_per_month_max"] == 30
    weekdays = estimate_runs_per_month("weekdays")
    assert weekdays["runs_per_month_min"] == weekdays["runs_per_month_max"] == 22
    once = estimate_runs_per_month("once")
    assert once["runs_per_month_min"] == once["runs_per_month_max"] == 1
    for estimate in (daily, weekdays, once):
        assert "counted" in estimate["basis"]


def test_weekly_estimate_counts_real_occurrences_per_weekday() -> None:
    # 2024-01-01 (window start) is a Monday: Mondays in 30 days -> Jan 1,8,15,22,29 = 5;
    # Sundays -> Jan 7,14,21,28 = 4.  The numbers come from the calendar, so they must.
    monday = estimate_runs_per_month("weekly", weekday=0)
    assert monday["runs_per_month_min"] == monday["runs_per_month_max"] == 5
    sunday = estimate_runs_per_month("weekly", weekday=6)
    assert sunday["runs_per_month_min"] == sunday["runs_per_month_max"] == 4
    any_day = estimate_runs_per_month("weekly")
    assert (any_day["runs_per_month_min"], any_day["runs_per_month_max"]) == (4, 5)


def test_estimate_refuses_unknown_cadence_and_bad_weekday() -> None:
    with pytest.raises(PolsiaError) as unknown:
        estimate_runs_per_month("fortnightly")
    assert unknown.value.code == E_VALIDATION
    assert unknown.value.how_to_fix
    with pytest.raises(PolsiaError) as bad_day:
        estimate_runs_per_month("weekly", weekday=7)
    assert bad_day.value.code == E_VALIDATION
    with pytest.raises(PolsiaError) as bool_day:
        estimate_runs_per_month("weekly", weekday=True)  # bool is not a weekday
    assert bool_day.value.code == E_VALIDATION


# ── the spec builder ──────────────────────────────────────────────────────────


def test_build_schedule_spec_delegates_next_run() -> None:
    spec = build_schedule_spec("cold_outreach", "weekdays", hour=9, now=NOW, tz=CHI)
    expected = compute_next_run("weekdays", 9, None, now=NOW, tz=CHI)
    assert spec["next_run_at"] == expected.isoformat()
    assert spec["automation"] == "cold_outreach"
    assert spec["frequency"] == "weekdays"
    create = spec["schedule_create"]
    assert set(create) == _SCHEDULE_CREATE_KEYS
    assert create["cadence"] == "weekdays"
    assert create["hour"] == 9
    assert create["weekday"] is None
    assert create["name"] == DEFAULTS["cold_outreach"][0]
    assert create["prompt"] == DEFAULTS["cold_outreach"][1]
    assert spec["estimate"]["runs_per_month_min"] == 22


def test_build_schedule_spec_weekly_carries_weekday_and_exact_estimate() -> None:
    spec = build_schedule_spec("cold_outreach", "weekly", hour=8, weekday=0, now=NOW, tz=CHI)
    expected = compute_next_run("weekly", 8, 0, now=NOW, tz=CHI)
    assert spec["next_run_at"] == expected.isoformat()
    assert spec["schedule_create"]["weekday"] == 0
    assert spec["estimate"]["runs_per_month_min"] == spec["estimate"]["runs_per_month_max"] == 5


def test_build_schedule_spec_name_and_prompt_override() -> None:
    spec = build_schedule_spec(
        "support", "daily", hour=7, now=NOW, tz=CHI,
        name="Morning support", prompt="Custom prompt",
    )
    assert spec["schedule_create"]["name"] == "Morning support"
    assert spec["schedule_create"]["prompt"] == "Custom prompt"


def test_build_schedule_spec_refusals_by_name() -> None:
    with pytest.raises(PolsiaError) as unknown:
        build_schedule_spec("cold_outreach_v2", "daily", hour=9, now=NOW, tz=CHI)
    assert unknown.value.code == E_VALIDATION
    assert unknown.value.how_to_fix
    with pytest.raises(PolsiaError) as not_offered:
        build_schedule_spec("support", "weekly", hour=9, weekday=0, now=NOW, tz=CHI)
    assert not_offered.value.code == E_VALIDATION
    assert "once" in not_offered.value.how_to_fix  # the fix names what IS offered
    # store rules propagate by name: weekly with no weekday, hour out of range
    with pytest.raises(PolsiaError) as no_weekday:
        build_schedule_spec("cold_outreach", "weekly", hour=9, now=NOW, tz=CHI)
    assert no_weekday.value.code == E_VALIDATION
    assert no_weekday.value.how_to_fix
    with pytest.raises(PolsiaError) as bad_hour:
        build_schedule_spec("support", "daily", hour=24, now=NOW, tz=CHI)
    assert bad_hour.value.code == E_VALIDATION


def test_cadence_values_are_the_store_enum_values() -> None:
    # a spec's cadence value must be a literal member of the store's enum (not a copy)
    values = {member.value for member in ScheduleCadence}
    for frequencies in PRESETS.values():
        for frequency in frequencies:
            assert frequency in values


# ── the list shape the routes/UI adapter will consume ─────────────────────────


def test_list_presets_shape() -> None:
    rows = list_presets()
    assert {row["automation"] for row in rows} == set(PRESETS)
    by_name = {row["automation"]: row for row in rows}
    cold = by_name["cold_outreach"]
    assert [f["value"] for f in cold["frequencies"]] == ["once", "daily", "weekdays", "weekly"]
    for frequency in cold["frequencies"]:
        assert frequency["label"].strip()
        estimate = frequency["estimate"]
        assert estimate["runs_per_month_min"] <= estimate["runs_per_month_max"]
    support = by_name["support"]
    assert [f["value"] for f in support["frequencies"]] == ["once", "daily"]
