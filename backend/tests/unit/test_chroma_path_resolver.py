"""D7 — the Chroma store path resolver (audit L3-01; registry D7).

Frozen semantics, each pinned by a test:

  * POSIX hosts: any path passes through untouched, nothing logged;
  * Windows + relative path: passes through untouched, nothing logged;
  * Windows + POSIX-absolute + `CHROMA_PERSIST_DIR` set in the process env: honored verbatim with
    one INFO — the explicit operator choice, and the exact contract `e2e/_sweep-fixtures.py`
    depends on (it reads the same variable directly);
  * Windows + POSIX-absolute + env unset: one WARNING naming the drive-relative hazard, and the
    path resolves to `<backend>/data/chroma` (created on demand).

RED leg (observed): neutralize the guard (`if os.name != "nt" or ...` -> `if False:`) -> the
Windows-hazard test fails; restore -> green.
"""
from __future__ import annotations

import logging
import sys
import types
from pathlib import Path

import pytest

from app.core import chroma_client


def _force(monkeypatch, *, name: str, path: str, env_value: str | None = None) -> None:
    """Put the resolver in exactly the state a platform+config combination produces."""
    monkeypatch.setattr(chroma_client.os, "name", name)
    monkeypatch.setattr(chroma_client.settings, "chroma_persist_dir", path)
    if env_value is None:
        monkeypatch.delenv("CHROMA_PERSIST_DIR", raising=False)
    else:
        monkeypatch.setenv("CHROMA_PERSIST_DIR", env_value)


def test_windows_posix_absolute_without_env_warns_and_resolves_to_backend_data(
    monkeypatch, tmp_path, caplog
):
    fallback = tmp_path / "backend" / "data" / "chroma"
    monkeypatch.setattr(chroma_client, "_fallback_persist_dir", lambda: fallback)
    _force(monkeypatch, name="nt", path="/app/data/chroma")

    with caplog.at_level(logging.INFO, logger="app.core.chroma_client"):
        resolved = chroma_client.resolve_persist_dir()

    assert resolved == str(fallback), "the drive-relative default must not survive on Windows"
    assert fallback.is_dir(), "the resolved store is created on demand, as chroma would"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(warnings) == 1 and not infos, "exactly one WARNING, no INFO, on the hazard path"
    text = warnings[0].getMessage()
    assert "/app/data/chroma" in text and "drive-relative" in text.lower(), (
        "the warning must name the configured path AND the drive-relative hazard"
    )


def test_windows_posix_absolute_with_explicit_env_is_honored_verbatim(monkeypatch, caplog):
    _force(monkeypatch, name="nt", path="/srv/chroma", env_value="/srv/chroma")

    with caplog.at_level(logging.INFO, logger="app.core.chroma_client"):
        resolved = chroma_client.resolve_persist_dir()

    assert resolved == "/srv/chroma", "the operator's explicit choice wins, verbatim"
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]
    assert [r for r in caplog.records if r.levelno == logging.INFO], "one INFO records the choice"


@pytest.mark.parametrize("name", ["nt", "posix"])
def test_relative_path_passes_through_on_both_platforms(monkeypatch, caplog, name):
    _force(monkeypatch, name=name, path="data/chroma")

    with caplog.at_level(logging.INFO, logger="app.core.chroma_client"):
        resolved = chroma_client.resolve_persist_dir()

    assert resolved == "data/chroma"
    assert not caplog.records, "a relative path is already unambiguous — nothing to log"


def test_posix_host_posix_absolute_passes_through(monkeypatch, caplog):
    _force(monkeypatch, name="posix", path="/app/data/chroma")

    with caplog.at_level(logging.INFO, logger="app.core.chroma_client"):
        resolved = chroma_client.resolve_persist_dir()

    assert resolved == "/app/data/chroma", "inside the container the POSIX path IS correct"
    assert not caplog.records, "no warning where there is no hazard"


def test_posix_host_with_explicit_env_still_passes_through(monkeypatch, caplog):
    _force(monkeypatch, name="posix", path="/srv/chroma", env_value="/srv/chroma")

    with caplog.at_level(logging.INFO, logger="app.core.chroma_client"):
        resolved = chroma_client.resolve_persist_dir()

    assert resolved == "/srv/chroma"
    assert not caplog.records, "the INFO branch is Windows-only; POSIX needs no commentary"


def test_fallback_dir_is_anchored_at_backend_not_cwd():
    expected = Path(chroma_client.__file__).resolve().parents[2] / "data" / "chroma"
    assert chroma_client._fallback_persist_dir() == expected
    assert expected.parts[-3:] == ("backend", "data", "chroma")


def test_get_client_hands_chromadb_the_resolved_path(monkeypatch, tmp_path):
    """The consumer wiring: PersistentClient receives resolve_persist_dir(), not the raw setting."""
    seen: list[dict] = []

    fake = types.ModuleType("chromadb")

    class _FakePersistentClient:
        def __init__(self, **kwargs) -> None:
            seen.append(kwargs)

    fake.PersistentClient = _FakePersistentClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "chromadb", fake)
    monkeypatch.setattr(chroma_client, "_client", None)
    monkeypatch.setattr(chroma_client, "_collection", None)
    fallback = tmp_path / "backend" / "data" / "chroma"
    monkeypatch.setattr(chroma_client, "_fallback_persist_dir", lambda: fallback)
    _force(monkeypatch, name="nt", path="/app/data/chroma")

    client = chroma_client.get_client()

    assert isinstance(client, _FakePersistentClient)
    assert seen == [{"path": str(fallback)}]
