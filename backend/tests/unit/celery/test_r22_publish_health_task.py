"""R22 — the 15-minute publish-health tick: dirty stats WARN with the full dict, clean stats are silent.

WHY this file exists: the activity breaker counts failures and skips (`events.publish_stats()`),
but that function had zero production readers — "a skip is never silent" was true only inside the
test suite.  `check_activity_publish_health` is the reader: it runs on the beat entry
`activity-publish-healthcheck` and writes the stats into the worker's log.

RED legs (measured; raw/red_leg_b.txt): delete the `activity-publish-healthcheck` entry from
`celery_app/beat_schedule.py` and `test_the_health_tick_is_scheduled_in_the_real_beat_conf`
fails with a KeyError — the task exists but beat would never run it (the D-15 defect class).
"""
from __future__ import annotations

import logging
import time

import pytest

from app.core import events
from celery_app.tasks.maintenance import check_activity_publish_health
from celery_app.worker import app

TASK_NAME = "celery_app.tasks.maintenance.check_activity_publish_health"
LOGGER_NAME = "celery_app.tasks.maintenance"


@pytest.fixture(autouse=True)
def _isolate_breaker():
    """Breaker state is module-global; the tick must not observe another test's incident."""
    events.reset_publish_breaker()
    yield
    events.reset_publish_breaker()


def test_dirty_stats_are_warned_with_the_full_dict(caplog, monkeypatch):
    monkeypatch.setattr(events, "_publish_failures", 2)
    monkeypatch.setattr(events, "_publish_skipped", 7)
    monkeypatch.setattr(events, "_last_publish_error", "ConnectionError: refused")

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        result = check_activity_publish_health()

    assert result["clean"] is False
    assert result["stats"]["failures"] == 2
    assert result["stats"]["skipped_while_open"] == 7

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, "one tick must produce exactly one warning line"
    text = warnings[0].getMessage()
    # The FULL dict, not a bare count: every key is on the line an operator reads.
    for key in ("failures", "skipped_while_open", "breaker_open", "cooldown_seconds", "last_error"):
        assert key in text, f"the warning must carry the stats dict; {key!r} missing from {text!r}"
    assert "ConnectionError" in text


def test_an_open_breaker_alone_is_dirty(caplog, monkeypatch):
    """No counters, breaker still open — a skip would happen; that is non-clean by itself."""
    monkeypatch.setattr(events, "_publish_blocked_until", time.monotonic() + 60.0)

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        result = check_activity_publish_health()

    assert result["stats"]["breaker_open"] is True
    assert result["clean"] is False
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_clean_stats_stay_silent_at_warning_and_leave_a_debug_trace(caplog):
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        result = check_activity_publish_health()

    assert result == {"status": "completed", "clean": True, "stats": events.publish_stats()}
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING], (
        "a clean fleet must not pay one warning line per tick"
    )
    assert any(
        r.levelno == logging.DEBUG and "clean" in r.getMessage() for r in caplog.records
    ), "clean ticks leave a debug trace, so the tick is never invisible"


def test_the_health_tick_is_scheduled_in_the_real_beat_conf() -> None:
    """Declared-but-unscheduled is the defect class: a task in the module is not scheduled
    until the app conf beat actually reads carries it."""
    entry = app.conf.beat_schedule["activity-publish-healthcheck"]
    assert entry["task"] == TASK_NAME
    assert TASK_NAME in app.tasks, "the module task must be registered on the same app"
    assert entry["schedule"] is not None
    assert entry["options"]["expires"] == 900, "one full period — a late tick collapses (backlog rule)"
    assert entry["options"]["queue"] == "maintenance"
