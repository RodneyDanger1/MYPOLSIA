"""FIXER-5 / L1-07 — the DeepSeek key store's default path is repo-anchored, not cwd-relative.

Premise (RED before the fix; raw evidence: fix-5/raw/probe_premises_l1.txt + the repo-root
leg): launched from the repo root, `_resolve_key_file()` answered `data\\deepseek_api_key.json`
resolving to `<repo>/data/…` — a path `.gitignore` does not cover — while `.gitignore:63`,
the compose `backend_data:/app/data` volume and every documented launch assume
`backend/data/…`.  The fix anchors the documented default at
`Path(__file__).resolve().parents[2]` (backend/), the D7 treatment chroma_client applies.

The explicit-override semantics are preserved verbatim: `DEEPSEEK_KEY_FILE` (or a non-default
Settings field) still wins and still resolves against the cwd it names.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app.services import secret_store

BACKEND = Path(secret_store.__file__).resolve().parents[2]
ANCHORED = BACKEND / "data" / "deepseek_api_key.json"


@pytest.fixture
def default_store(monkeypatch: pytest.MonkeyPatch):
    """The unconfigured default: no env override, the Settings field at its default value."""
    monkeypatch.delenv(secret_store.KEY_FILE_ENV_VAR, raising=False)
    monkeypatch.setattr(
        secret_store.settings, "deepseek_key_file", secret_store.DEFAULT_KEY_FILE, raising=False
    )
    return secret_store._resolve_key_file


def test_default_is_the_anchored_backend_data_path(default_store):
    assert default_store() == ANCHORED


def test_a_launch_from_the_repo_root_still_lands_in_backend_data(default_store, monkeypatch):
    """The exact failing scenario the finding names (uvicorn started from the repo root)."""
    monkeypatch.chdir(BACKEND.parent)  # <repo root>
    assert default_store() == ANCHORED

    monkeypatch.chdir(BACKEND)  # the documented `cd backend` launch: same answer
    assert default_store() == ANCHORED


def test_an_explicit_env_override_is_honoured_verbatim(default_store, monkeypatch, tmp_path):
    custom = tmp_path / "custom_key.json"
    monkeypatch.setenv(secret_store.KEY_FILE_ENV_VAR, str(custom))
    assert default_store() == custom

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(secret_store.KEY_FILE_ENV_VAR, "relative_key.json")
    # Override semantics preserved byte-for-byte: a relative override stays cwd-relative
    # (the resolved file would live at tmp_path/relative_key.json).
    assert default_store() == Path("relative_key.json")


def test_a_settings_field_override_is_honoured_verbatim(default_store, monkeypatch, tmp_path):
    custom = tmp_path / "from_settings.json"
    monkeypatch.setattr(secret_store.settings, "deepseek_key_file", str(custom), raising=False)
    assert default_store() == custom


def test_legacy_cwd_relative_store_is_read_when_the_anchored_one_does_not_exist(
    default_store, monkeypatch, tmp_path, caplog
):
    """Compatibility: a store a pre-fix wrong-cwd launch wrote at the old default is migrated.

    One WARNING names both paths; the file's value is served through the normal read path.
    """
    legacy = tmp_path / "data" / "deepseek_api_key.json"
    legacy.parent.mkdir()
    legacy.write_text('{"deepseek_api_key": "legacy-key-value"}', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    # Hermetic seam (2026-10-13, found live in the docker pass): the anchored path is CONSTRUCTED
    # absent under tmp_path, never merely assumed absent on the host.  A real stored key in
    # backend/data/ (the owner entered one through the app) used to flip this precondition and
    # red the suite on a machine that actually uses the product — the same seam the sibling
    # `test_anchored_store_wins_over_a_legacy_file` already applies.
    anchored_absent = tmp_path / "anchored-absent" / "deepseek_api_key.json"
    monkeypatch.setattr(secret_store, "_anchored_default_key_file", lambda: anchored_absent)
    assert not anchored_absent.is_file(), "precondition: the anchored store must not exist"

    with caplog.at_level(logging.WARNING, logger=secret_store.__name__):
        resolved = default_store()
        assert resolved.resolve() == legacy.resolve()
        # the legacy value is really served, and the key itself is never logged
        monkeypatch.delenv(secret_store.DEEPSEEK_ENV_VAR, raising=False)
        assert secret_store.get_deepseek_api_key() == "legacy-key-value"

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("legacy" in r.getMessage() for r in warnings)
    assert "legacy-key-value" not in caplog.text


def test_a_documented_launch_never_warns_about_a_legacy_file(
    default_store, monkeypatch, caplog
):
    """From `backend/`, the legacy path IS the anchored path — no warning, no extra read."""
    monkeypatch.chdir(BACKEND)
    with caplog.at_level(logging.WARNING, logger=secret_store.__name__):
        assert default_store() == ANCHORED
    assert not [r for r in caplog.records if "legacy" in r.getMessage()]


def test_anchored_store_wins_over_a_legacy_file(default_store, monkeypatch, tmp_path):
    """Once the anchored file exists, a cwd-relative stray can never shadow it.

    The anchor LOCATION is the test seam (the repo's real `backend/data/` is never written
    by a test); the precedence logic under test is untouched.
    """
    cwd = tmp_path / "cwd"
    legacy = cwd / "data" / "deepseek_api_key.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{"deepseek_api_key": "legacy-key-value"}', encoding="utf-8")
    anchored = tmp_path / "anchored" / "deepseek_api_key.json"
    anchored.parent.mkdir(parents=True)
    anchored.write_text('{"deepseek_api_key": "anchored-key-value"}', encoding="utf-8")

    monkeypatch.setattr(secret_store, "_anchored_default_key_file", lambda: anchored)
    monkeypatch.chdir(cwd)
    assert default_store() == anchored
