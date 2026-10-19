"""V25/L2 — per-run code workspace: an optional run id on the executor seam (RED-first).

Written BEFORE `execute_tool(..., run_id=...)` exists and BEFORE `cleanup_run_workspace`
exists — the RED run is `raw/RED-workspace.txt`.

WHAT IT PINS:

  * ABSENT run id (None or blank) keeps today's shared root BYTE-IDENTICAL: same files, same
    result shapes, no `runs/` directory created — every pre-V25 call site is unchanged;
  * a run id scopes `code_write` / `code_read` / `code_list` to
    `<settings.code_workspace_dir>/runs/<run_id>/`, and the reported `path` stays relative to
    that run directory (the model never sees the run prefix);
  * run ids are validated by name (`E_VALIDATION`): separators, dots, `..`, drive letters,
    control characters and over-long ids are refused — no escape can be expressed;
  * a symlink/junction escape inside a run directory is still refused by the shared containment
    proof;
  * `cleanup_run_workspace(run_id)` removes ONLY that run's directory, refuses an invalid id,
    and refuses a run directory that resolves outside `runs/` (a junction must not let a
    cleanup delete somebody else's tree);
  * the executor signature gains exactly one optional keyword: `run_id`.

The workspace is a REAL `tmp_path` directory; the escape link is a REAL symlink on POSIX and a
REAL NTFS junction on Windows (a labelled fixture, never a mock of the code under test).
"""
from __future__ import annotations

import inspect
import json
import os
import subprocess

import pytest

from app.agents import tool_registry
from app.core.errors import E_VALIDATION, PolsiaError

RUN = "run-abc123"


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    from app.config import settings

    root = tmp_path / "workspace"
    monkeypatch.setattr(settings, "code_workspace_dir", str(root))
    return root


def _payload(raw: str) -> dict:
    return json.loads(raw)


def _call(name: str, arguments: dict, *, run_id: str | None = None,
          agent_type: str = "code_generation") -> dict:
    if run_id is None:
        raw = tool_registry.execute_tool(name, arguments, agent_type=agent_type)
    else:
        raw = tool_registry.execute_tool(name, arguments, agent_type=agent_type, run_id=run_id)
    return _payload(raw)


def _write(path: str, content: str, *, run_id: str | None = None) -> dict:
    return _call("code_write", {"path": path, "content": content}, run_id=run_id)


def _read(path: str, *, run_id: str | None = None) -> dict:
    return _call("code_read", {"path": path}, run_id=run_id)


def _list(path: str | None = None, *, run_id: str | None = None) -> dict:
    return _call("code_list", {} if path is None else {"path": path}, run_id=run_id)


def _escape_link(root, target):
    """A REAL escape link: symlink on POSIX, junction on Windows (no privilege needed)."""
    root.mkdir(parents=True, exist_ok=True)
    link = root / "escape"
    if os.name == "nt":
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                                capture_output=True, text=True, check=False)
        if result.returncode != 0:  # pragma: no cover - host without junction support
            pytest.skip(f"cannot create a junction on this host: {result.stderr or result.stdout}")
    else:
        os.symlink(target, link)
    return link


# ── the seam ───────────────────────────────────────────────────────────────────────────────

def test_the_executor_gains_exactly_one_optional_run_id():
    signature = inspect.signature(tool_registry.execute_tool)
    assert list(signature.parameters) == ["name", "arguments", "agent_type", "task_title", "run_id"]
    run_id = signature.parameters["run_id"]
    assert run_id.kind is inspect.Parameter.KEYWORD_ONLY
    assert run_id.default is None


# ── absent run id: today's shared root, byte-identical ─────────────────────────────────────

def test_an_absent_run_id_keeps_the_shared_root(workspace):
    written = _write("notes/plan.md", "hello")

    assert written["ok"] is True
    assert written["result"] == {"path": "notes/plan.md", "bytes": 5}
    assert (workspace / "notes" / "plan.md").is_file(), "the pre-V25 location, unchanged"
    assert not (workspace / tool_registry.RUN_WORKSPACE_SUBDIR).exists(), (
        "a run-less call must not create the runs/ tree"
    )
    assert _read("notes/plan.md")["result"] == {
        "path": "notes/plan.md", "text": "hello", "bytes": 5}
    assert _list("notes")["result"]["entries"] == [
        {"path": "notes/plan.md", "bytes": 5, "kind": "file"}]


def test_a_blank_run_id_is_treated_as_absent(workspace):
    raw = tool_registry.execute_tool("code_write", {"path": "a.txt", "content": "x"},
                                     agent_type="code_generation", run_id="   ")
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert (workspace / "a.txt").is_file()
    assert not (workspace / tool_registry.RUN_WORKSPACE_SUBDIR).exists()


def test_an_explicit_none_run_id_is_byte_identical_to_the_pre_v25_call(workspace):
    without = _write("a.txt", "same")
    explicit = _write("b.txt", "same", run_id="")
    assert set(without["result"]) == set(explicit["result"]) == {"path", "bytes"}
    assert without["result"]["bytes"] == explicit["result"]["bytes"] == 4


# ── a run id scopes every code tool ───────────────────────────────────────────────────────

def test_a_run_id_scopes_write_read_and_list(workspace):
    written = _write("notes/plan.md", "hello", run_id=RUN)

    assert written["ok"] is True
    assert written["result"] == {"path": "notes/plan.md", "bytes": 5}
    assert (workspace / tool_registry.RUN_WORKSPACE_SUBDIR / RUN / "notes" / "plan.md").is_file()
    assert not (workspace / "notes").exists(), "nothing landed in the shared root"

    read = _read("notes/plan.md", run_id=RUN)
    assert read["result"] == {"path": "notes/plan.md", "text": "hello", "bytes": 5}

    listed = _list(run_id=RUN)
    assert listed["result"]["path"] == "."
    assert [entry["path"] for entry in listed["result"]["entries"]] == ["notes"]


def test_runs_cannot_see_each_others_files(workspace):
    _write("secret.txt", "run a only", run_id="run-a")

    other = _read("secret.txt", run_id="run-b")
    assert other["ok"] is False and other["error"]["code"] == "E_NOT_FOUND"

    listing = _list(run_id="run-b")
    assert listing["ok"] is True and listing["result"]["entries"] == []
    assert "empty" in listing["result"]["note"]


def test_the_shared_root_does_not_see_a_runs_files_either(workspace):
    _write("inside.txt", "x", run_id=RUN)

    payload = _read("inside.txt")
    assert payload["ok"] is False and payload["error"]["code"] == "E_NOT_FOUND"


@pytest.mark.parametrize("run_id", [
    "..", ".", "../x", "a/b", "a\\b", "C:\\x", "/abs", "run.id", "run id", "run\nid",
    "x" * 65, "-leading", "_leading", "run/../other",
])
def test_a_hostile_run_id_is_E_VALIDATION_and_creates_nothing(workspace, run_id):
    payload = _write("a.txt", "x", run_id=run_id)

    assert payload["ok"] is False, run_id
    assert payload["error"]["code"] == "E_VALIDATION", run_id
    assert not workspace.exists(), "a refused run id must not create anything"


def test_a_symlink_or_junction_inside_a_run_directory_is_still_refused(workspace, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("internal", encoding="utf-8")
    run_dir = workspace / tool_registry.RUN_WORKSPACE_SUBDIR / RUN
    _escape_link(run_dir, outside)

    write = _write("escape/evil.txt", "exfiltrate", run_id=RUN)
    read = _read("escape/secret.txt", run_id=RUN)
    listed = _list("escape", run_id=RUN)

    assert write["ok"] is False and write["error"]["code"] == "E_VALIDATION"
    assert not (outside / "evil.txt").exists(), "no byte crossed the run boundary"
    assert read["ok"] is False and read["error"]["code"] == "E_VALIDATION"
    assert listed["ok"] is False and listed["error"]["code"] == "E_VALIDATION"


# ── cleanup ───────────────────────────────────────────────────────────────────────────────

def test_cleanup_removes_only_the_named_run(workspace):
    _write("a.txt", "a", run_id="run-a")
    _write("b.txt", "b", run_id="run-b")

    outcome = tool_registry.cleanup_run_workspace("run-a")

    assert outcome["removed"] is True
    assert not (workspace / tool_registry.RUN_WORKSPACE_SUBDIR / "run-a").exists()
    assert (workspace / tool_registry.RUN_WORKSPACE_SUBDIR / "run-b" / "b.txt").is_file()
    assert workspace.is_dir(), "the workspace root itself is never removed"


def test_cleanup_of_an_absent_run_reports_removed_false(workspace):
    assert tool_registry.cleanup_run_workspace("never-existed") == {
        "run_id": "never-existed", "removed": False,
        "path": str(workspace / tool_registry.RUN_WORKSPACE_SUBDIR / "never-existed"),
    }


@pytest.mark.parametrize("run_id", ["..", "../x", "a/b", ".", "x" * 65])
def test_cleanup_refuses_a_hostile_run_id(workspace, run_id):
    with pytest.raises(PolsiaError) as excinfo:
        tool_registry.cleanup_run_workspace(run_id)
    assert excinfo.value.code == E_VALIDATION


def test_cleanup_never_follows_a_junction_out_of_the_runs_tree(workspace, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep me", encoding="utf-8")
    runs_dir = workspace / tool_registry.RUN_WORKSPACE_SUBDIR
    _escape_link(runs_dir, outside)  # runs/escape -> outside

    with pytest.raises(PolsiaError) as excinfo:
        tool_registry.cleanup_run_workspace("escape")

    assert excinfo.value.code == E_VALIDATION
    assert (outside / "keep.txt").is_file(), "a refused cleanup deletes nothing"
