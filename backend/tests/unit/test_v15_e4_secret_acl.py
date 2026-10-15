"""Wave V15 / lane E4 — 2R-05 (U2-11): the secret store's permission tightening, Windows-aware.

RED-first at `b17d7b0`: `_best_effort_chmod` calls `os.chmod` only; on Windows that toggles the
read-only bit and leaves the inherited directory ACL — there is no `icacls` call anywhere in the
tree (grep: 0 hits) and no warning when the tightening cannot be done.

Pins:
  (a) the Windows branch runs `icacls <file> /inheritance:r /grant:r <user>:(R,W)` via subprocess;
  (b) when the tightening cannot be done, a warning NAMING the file is logged — no silent pass;
  (c) the POSIX branch is unchanged (`os.chmod 0600`, no subprocess);
  (d) a missing icacls binary is a False + named warning, never a crash.

This host IS Windows, but the branch is driven through the module's `_WINDOWS` constant so both
branches are exercised deterministically on any host.
"""
from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import secret_store


def _key_file(tmp_path: Path) -> Path:
    path = tmp_path / "deepseek_api_key.json"
    path.write_text('{"deepseek_api_key": "sk-fake"}', encoding="utf-8")
    return path


def test_windows_branch_invokes_icacls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = _key_file(tmp_path)
    monkeypatch.setattr(secret_store, "_WINDOWS", True)
    monkeypatch.setenv("USERNAME", "tester")
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(secret_store.subprocess, "run", fake_run)

    assert secret_store._best_effort_chmod(path) is True
    assert len(calls) == 1, "exactly one icacls invocation"
    argv = calls[0]
    assert argv[0] == "icacls"
    assert str(path) in argv
    assert "/inheritance:r" in argv, "inherited ACEs must be stripped"
    assert any(part.startswith("/grant:r") for part in argv)
    assert any("tester" in part and "(R,W)" in part for part in argv)


def test_failed_windows_tighten_logs_a_named_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _key_file(tmp_path)
    monkeypatch.setattr(secret_store, "_WINDOWS", True)
    monkeypatch.setattr(secret_store, "_tighten_windows_acl", lambda candidate: False)

    with caplog.at_level(logging.WARNING, logger="app.services.secret_store"):
        assert secret_store._best_effort_chmod(path) is False

    assert str(path) in caplog.text, "the warning must NAME the file"
    assert "icacls" in caplog.text, "the warning must NAME the mechanism that failed"


def test_missing_icacls_binary_is_a_named_warning_not_a_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _key_file(tmp_path)
    monkeypatch.setattr(secret_store, "_WINDOWS", True)
    monkeypatch.setenv("USERNAME", "tester")

    def boom(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("icacls")

    monkeypatch.setattr(secret_store.subprocess, "run", boom)
    with caplog.at_level(logging.WARNING, logger="app.services.secret_store"):
        assert secret_store._best_effort_chmod(path) is False
    assert str(path) in caplog.text


def test_no_username_available_refuses_instead_of_guessing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = _key_file(tmp_path)
    monkeypatch.setattr(secret_store, "_WINDOWS", True)
    monkeypatch.setenv("USERNAME", "")
    monkeypatch.setenv("USER", "")
    monkeypatch.setattr(secret_store.getpass, "getuser", lambda: "")
    calls: list[object] = []
    monkeypatch.setattr(
        secret_store.subprocess, "run", lambda *a, **k: calls.append(a) or None
    )

    assert secret_store._best_effort_chmod(path) is False
    assert calls == [], "no icacls call may be attempted without a user to grant"


def test_posix_branch_unchanged_chmod_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = _key_file(tmp_path)
    monkeypatch.setattr(secret_store, "_WINDOWS", False)
    chmods: list[tuple[Path, int]] = []
    monkeypatch.setattr(secret_store.os, "chmod", lambda p, mode: chmods.append((p, mode)))
    calls: list[object] = []
    monkeypatch.setattr(
        secret_store.subprocess, "run", lambda *a, **k: calls.append(a) or None
    )

    assert secret_store._best_effort_chmod(path) is True
    assert chmods == [(path, 0o600)]
    assert calls == [], "the POSIX branch must not shell out"
