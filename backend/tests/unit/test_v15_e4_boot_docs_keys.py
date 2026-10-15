"""Wave V15 / lane E4 — boot-behaviour pins for `app/main.py` (2R-02, 2R-06, 5-03).

RED-first at HEAD `b17d7b0`: `create_app()` always registers /docs, /redoc and /openapi.json
(no `docs_url`/`redoc_url`/`openapi_url` override exists), and `lifespan` warns about the
default/blank key only — a non-ASCII configured key goes unmentioned.

5-03 (#7 LOCKED) is verify-only: the dev-key boot warning must SURVIVE every E4 edit to
`main.py`.  The grep proof lives in the lane receipt (`wave-v15/e4-security-ops/raw/`); this pin
is its runnable form — it fails if the warning text disappears or stops firing.

The lifespan runs through `starlette.testclient.TestClient` as a context manager — that is what
actually executes startup/shutdown (the same mechanism pinned by
tests/unit/test_lifecycle_shutdown.py) — so the warnings asserted here are the REAL boot
warnings, not a re-implementation.
"""
from __future__ import annotations

import logging

import pytest
from starlette.testclient import TestClient

from app.config import settings
from app.main import app, create_app


def _boot(monkeypatch: pytest.MonkeyPatch, api_key: str, caplog: pytest.LogCaptureFixture) -> str:
    """Start the REAL app with `api_key` configured and return the captured boot log."""
    monkeypatch.setattr(settings, "api_key", api_key)
    with caplog.at_level(logging.WARNING, logger="app.main"), TestClient(app) as client:
        assert client.get("/").status_code == 200, "the lifespan must have started the app"
    return caplog.text


# ── 2R-02 / U2-07 + decision #8: docs auto-hide when sandbox is OFF ─────────────────────────────


def test_docs_routes_are_hidden_when_sandbox_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "sandbox_mode", False)
    application = create_app()  # reads settings.sandbox_mode at construction — that is the seam
    with TestClient(application) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/redoc").status_code == 404
        assert client.get("/openapi.json").status_code == 404


def test_root_does_not_advertise_docs_when_sandbox_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "sandbox_mode", False)
    with TestClient(create_app()) as client:
        assert client.get("/").json()["docs"] is None


def test_docs_routes_are_served_when_sandbox_is_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sandbox-ON branch (local drill) keeps the previous behaviour byte-for-byte."""
    monkeypatch.setattr(settings, "sandbox_mode", True)
    with TestClient(create_app()) as client:
        assert client.get("/docs").status_code == 200
        assert client.get("/openapi.json").status_code == 200
        assert client.get("/").json()["docs"] == "/docs"


# ── 2R-06 / V2 handoff: a non-ASCII configured key is a total outage — say so at boot ───────────


def test_non_ascii_api_key_warns_at_boot(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    text = _boot(monkeypatch, "clé-Ünicode-key", caplog)
    assert "non-ASCII" in text
    assert "clé-Ünicode-key" not in text, "the key itself must never be logged"


def test_ascii_api_key_does_not_warn_non_ascii(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    text = _boot(monkeypatch, "ascii-only-key-1234", caplog)
    assert "non-ASCII" not in text


def test_blank_api_key_warns_default_not_non_ascii(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    text = _boot(monkeypatch, "", caplog)
    assert "default/blank" in text
    assert "non-ASCII" not in text


# ── 5-03 / #7 LOCKED (verify-only): the dev-key warning is preserved ────────────────────────────


def test_dev_key_boot_warning_still_fires(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    text = _boot(monkeypatch, "dev-key", caplog)
    assert "API_KEY is the default/blank value" in text
