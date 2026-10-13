"""PHASE-3 fixer-2 / M1 (fsync before replace) + M5 (evening-summary sentinel guards).

M1: `set_deepseek_api_key`'s docstring says "durably", but the write path was temp-write ->
`os.replace` with no `os.fsync` — atomic (never torn) but not crash-durable.  The pin is the
ORDER: fsync of the file handle must happen before the replace.

M5: `save_evening_summary` assigned `evening_summary`/`insights`/`metrics_snapshot`
unconditionally, so a re-run with default arguments erased what an earlier run wrote.  The
sentinel makes "not supplied" distinct from an explicit `None`; the counters' replace-vs-merge
question is recorded as REQUESTS (receipt), not silently changed here.
"""
from __future__ import annotations

import json
import os
from datetime import date

import pytest

from app.services import secret_store
from app.services.report_service import save_evening_summary


def test_secret_store_fsyncs_the_temp_file_before_the_replace(tmp_path, monkeypatch):
    events: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace

    def recording_fsync(fd: int) -> None:
        events.append("fsync")
        real_fsync(fd)

    def recording_replace(src, dst) -> None:  # noqa: ANN001
        events.append("replace")
        real_replace(src, dst)

    key_path = tmp_path / "deepseek_api_key.json"
    monkeypatch.setenv(secret_store.KEY_FILE_ENV_VAR, str(key_path))
    monkeypatch.setattr(secret_store.os, "fsync", recording_fsync)
    monkeypatch.setattr(secret_store.os, "replace", recording_replace)

    secret_store.set_deepseek_api_key("  sk-phase3-fsync  ")

    assert events == ["fsync", "replace"], (
        f"the durability order is fsync(file) THEN replace; measured {events}"
    )
    stored = json.loads(key_path.read_text(encoding="utf-8"))
    assert stored["deepseek_api_key"] == "sk-phase3-fsync"  # stripped, as before


@pytest.mark.asyncio
async def test_evening_summary_rerun_with_defaults_preserves_written_fields(async_db_session):
    day = date(2026, 10, 13)
    await save_evening_summary(
        async_db_session,
        day,
        summary="first run",
        insights=["i1"],
        metrics_snapshot={"mrr": 1},
        tasks_completed=2,
        tasks_failed=1,
    )
    await async_db_session.commit()

    again = await save_evening_summary(async_db_session, day)  # defaults = "not supplied"

    assert again.evening_summary == "first run", "a re-run with defaults erased the summary"
    assert again.insights == ["i1"]
    assert again.metrics_snapshot == {"mrr": 1}


@pytest.mark.asyncio
async def test_evening_summary_explicit_none_still_clears(async_db_session):
    day = date(2026, 10, 14)
    await save_evening_summary(
        async_db_session, day, summary="s", insights=["i"], metrics_snapshot={"m": 1}
    )
    await async_db_session.commit()

    cleared = await save_evening_summary(
        async_db_session, day, summary=None, insights=None, metrics_snapshot=None
    )
    assert cleared.evening_summary is None
    assert cleared.insights == []
    assert cleared.metrics_snapshot == {}
