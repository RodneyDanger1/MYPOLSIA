"""V28/L5 — run-workspace retention edges (`tool_registry.cleanup_expired_run_workspaces`).

The V27 suite pins the task/route/activity wiring.  This file pins the POLICY BOUNDARIES the
docstring claims, each on a real `<root>/runs/` tree with explicit mtimes:

* exactly `keep_latest` old directories → ALL protected (the count bound is inclusive);
* one more than `keep_latest` → exactly the oldest goes;
* exactly `max_age_hours` old → KEPT ("older than", not "as old as"), one second older → removed;
* a plain FILE under `runs/`, a foreign-named directory, a linked run directory and a run
  directory CONTAINING a link are all skipped by name and never traversed;
* a `runs/` that is itself a link refuses (E_VALIDATION) before enumerating anything;
* siblings of `runs/` are outside the operation entirely.

Portability: directory symlinks are used where the OS allows them; on Windows without the
symlink privilege the helper falls back to a directory JUNCTION, which is the same skip branch
(`entry.is_symlink() or os.path.isjunction(entry.path)`) and needs no elevation.
"""
from __future__ import annotations

import os
import pathlib
import subprocess

import pytest

from app.agents.tool_registry import cleanup_expired_run_workspaces
from app.core.errors import E_VALIDATION, PolsiaError

NOW = 1_000_000_000  # fixed epoch seconds: every age below is exact integer arithmetic


def _mkdir(runs: pathlib.Path, name: str, *, age_seconds: int | None = None) -> pathlib.Path:
    target = runs / name
    target.mkdir()
    if age_seconds is not None:
        # utime AFTER any write inside: creating an entry bumps the directory mtime (the V27
        # Linux fixture bug this repo already fixed once).
        os.utime(target, (NOW - age_seconds, NOW - age_seconds))
    return target


def _make_dir_link(link: pathlib.Path, target: pathlib.Path) -> None:
    """A directory symlink, or a Windows junction when the symlink privilege is unavailable."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except OSError:
        if os.name != "nt":
            raise
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "ws"
    (root / "runs").mkdir(parents=True)
    return root


def test_exactly_keep_latest_old_directories_are_all_protected(workspace) -> None:
    runs = workspace / "runs"
    _mkdir(runs, "run-oldest", age_seconds=120 * 3600)
    _mkdir(runs, "run-middle", age_seconds=110 * 3600)
    _mkdir(runs, "run-newest", age_seconds=100 * 3600)

    protected = cleanup_expired_run_workspaces(
        workspace_root=workspace, keep_latest=3, max_age_hours=72, now=NOW
    )
    assert protected["removed"] == []
    assert protected["kept"] == 3, "the newest `keep_latest` are protected even when all are old"
    assert protected["policy"] == {"keep_latest": 3, "max_age_hours": 72.0}

    # One MORE directory than the bound: exactly the oldest is now eligible.
    shrunk = cleanup_expired_run_workspaces(
        workspace_root=workspace, keep_latest=2, max_age_hours=72, now=NOW
    )
    assert shrunk["removed"] == ["run-oldest"]
    assert shrunk["kept"] == 2
    assert sorted(p.name for p in runs.iterdir()) == ["run-middle", "run-newest"]


def test_exactly_72h_is_kept_and_one_second_older_is_removed(workspace) -> None:
    runs = workspace / "runs"
    _mkdir(runs, "run-at-72h", age_seconds=72 * 3600)
    _mkdir(runs, "run-at-72h-plus-1s", age_seconds=72 * 3600 + 1)

    result = cleanup_expired_run_workspaces(
        workspace_root=workspace, keep_latest=0, max_age_hours=72, now=NOW
    )
    assert result["removed"] == ["run-at-72h-plus-1s"], (
        "expiry is STRICTLY older than max_age_hours: exactly 72 h is not yet expired"
    )
    assert result["kept"] == 1
    assert (runs / "run-at-72h").is_dir()
    assert not (runs / "run-at-72h-plus-1s").exists()


def test_a_file_under_runs_is_skipped_never_deleted(workspace) -> None:
    runs = workspace / "runs"
    stray = runs / "stray.txt"
    stray.write_text("not a run", encoding="utf-8")
    named_like_a_run = runs / "run-as-file"
    named_like_a_run.write_text("still not a run", encoding="utf-8")
    _mkdir(runs, "run-old", age_seconds=100 * 3600)

    result = cleanup_expired_run_workspaces(
        workspace_root=workspace, keep_latest=0, max_age_hours=72, now=NOW
    )
    assert result["removed"] == ["run-old"]
    assert stray.exists() and named_like_a_run.exists(), "a FILE under runs/ is never a candidate"
    reasons = {item["name"]: item["reason"] for item in result["skipped"]}
    assert reasons == {"stray.txt": "not_a_directory", "run-as-file": "not_a_directory"}


def test_a_foreign_named_directory_is_skipped_by_name(workspace) -> None:
    runs = workspace / "runs"
    foreign = runs / "not a run"
    foreign.mkdir()
    spaced = runs / "run with spaces"
    spaced.mkdir()
    os.utime(foreign, (NOW - 100 * 3600, NOW - 100 * 3600))
    os.utime(spaced, (NOW - 100 * 3600, NOW - 100 * 3600))

    result = cleanup_expired_run_workspaces(
        workspace_root=workspace, keep_latest=0, max_age_hours=72, now=NOW
    )
    assert result["removed"] == []
    assert foreign.is_dir() and spaced.is_dir(), "a foreign name is never touched"
    reasons = {item["name"]: item["reason"] for item in result["skipped"]}
    assert reasons == {"not a run": "invalid_name", "run with spaces": "invalid_name"}


def test_a_linked_run_directory_is_skipped_and_its_target_survives(workspace, tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    keep = outside / "keep.txt"
    keep.write_text("must survive", encoding="utf-8")
    _make_dir_link(workspace / "runs" / "run-link", outside)

    result = cleanup_expired_run_workspaces(
        workspace_root=workspace, keep_latest=0, max_age_hours=72, now=NOW
    )
    assert result["removed"] == [], "a link is never followed, so nothing behind it is deleted"
    assert keep.exists(), "the link target's contents must survive"
    assert (workspace / "runs" / "run-link").exists()
    assert {item["name"]: item["reason"] for item in result["skipped"]} == {"run-link": "link"}


def test_a_run_directory_containing_a_link_is_skipped(workspace, tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    keep = outside / "keep.txt"
    keep.write_text("must survive", encoding="utf-8")

    run_dir = _mkdir(workspace / "runs", "run-with-link")
    _make_dir_link(run_dir / "inner", outside)
    # Creating the link inside bumps the directory mtime; age it AFTER the write (the V27 lesson).
    os.utime(run_dir, (NOW - 100 * 3600, NOW - 100 * 3600))

    result = cleanup_expired_run_workspaces(
        workspace_root=workspace, keep_latest=0, max_age_hours=72, now=NOW
    )
    assert result["removed"] == [], "an expired tree containing a link is SKIPPED, not traversed"
    assert run_dir.is_dir() and keep.exists()
    assert {item["name"]: item["reason"] for item in result["skipped"]} == {
        "run-with-link": "contains_link"
    }


def test_a_linked_runs_directory_refuses_and_deletes_nothing(workspace, tmp_path) -> None:
    outside = tmp_path / "outside"
    (outside / "run-old").mkdir(parents=True)
    (outside / "run-old" / "keep.txt").write_text("must survive", encoding="utf-8")
    # Replace the real runs/ with a link to the sibling tree.
    (workspace / "runs").rmdir()
    _make_dir_link(workspace / "runs", outside)

    with pytest.raises(PolsiaError) as excinfo:
        cleanup_expired_run_workspaces(workspace_root=workspace, keep_latest=0, max_age_hours=72, now=NOW)
    assert excinfo.value.code == E_VALIDATION
    assert "link or junction" in excinfo.value.message
    assert (outside / "run-old" / "keep.txt").exists(), "a refused run must not delete anything"


def test_siblings_of_runs_are_outside_the_operation(workspace) -> None:
    sibling_file = workspace / "keepme.txt"
    sibling_file.write_text("outside runs", encoding="utf-8")
    sibling_dir = workspace / "other"
    sibling_dir.mkdir()
    (sibling_dir / "run-old").mkdir()
    os.utime(sibling_dir, (NOW - 100 * 3600, NOW - 100 * 3600))

    result = cleanup_expired_run_workspaces(
        workspace_root=workspace, keep_latest=0, max_age_hours=72, now=NOW
    )
    assert result["removed"] == []
    assert sibling_file.exists() and sibling_dir.is_dir()
