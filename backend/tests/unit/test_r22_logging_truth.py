"""R22 — LOG_JSON and DEBUG stop being advertised-but-dead settings.

WHY the subprocess legs: the JSON/text decision runs ONCE at `app.main` import time, so the honest
observation is a fresh interpreter running the production import path and emitting one record —
not a re-invocation of `_configure_logging` into a root logger pytest already configured.

RED leg (measured; raw/red_leg_c.txt): delete the `settings.log_json` branch from
`app.main._configure_logging` (leave only `basicConfig`) and
`test_log_json_true_emits_a_parseable_json_object` fails — the probe line comes out as
`WARNING:r22.probe:json probe line`, which is not JSON.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]  # tests/unit/<this file> -> backend

PROBE = (
    "import logging\n"
    "import app.main  # noqa: F401  (runs _configure_logging() at import)\n"
    "logging.getLogger('r22.probe').warning('json probe line')\n"
)


def _run_probe(**extra_env: str) -> subprocess.CompletedProcess[str]:
    # The probe owns its logging env: strip LOG_JSON/CELERY_*/DEBUG/LOG_LEVEL so a developer's
    # shell cannot flip the branch this test is here to pin.
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("LOG_JSON", "CELERY_", "DEBUG", "LOG_LEVEL"))
    }
    env["CLAUDE_CLI_MOCK"] = "true"
    env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_log_json_true_emits_a_parseable_json_object():
    proc = _run_probe(LOG_JSON="true")
    assert proc.returncode == 0, proc.stderr

    lines = [line for line in proc.stderr.splitlines() if "json probe line" in line]
    assert lines, f"the probe record never reached stderr:\n{proc.stderr}"
    record = json.loads(lines[-1])  # a collector can parse it — that is the whole point of LOG_JSON
    assert record["level"] == "WARNING"
    assert record["logger"] == "r22.probe"
    assert record["message"] == "json probe line"
    assert "T" in record["ts"], "ts must be an ISO-8601 instant"


def test_log_json_absent_keeps_the_plain_text_line():
    """`log_json=False` (the default) is the `basicConfig` line this module always ran."""
    proc = _run_probe()
    assert proc.returncode == 0, proc.stderr

    lines = proc.stderr.splitlines()
    assert "WARNING:r22.probe:json probe line" in lines
    probe_lines = [line for line in lines if "json probe line" in line]
    assert all(not line.startswith("{") for line in probe_lines), (
        "the default path must not emit JSON"
    )


def test_debug_flag_reaches_the_fastapi_app(monkeypatch):
    """`settings.debug` was declared and had zero consumers; it now reaches the app object."""
    from app.config import settings
    from app.main import app as module_app
    from app.main import create_app

    assert module_app.debug is False, "the default path must equal the pre-R22 behavior"

    monkeypatch.setattr(settings, "debug", True)
    assert create_app().debug is True

    monkeypatch.setattr(settings, "debug", False)
    assert create_app().debug is False
