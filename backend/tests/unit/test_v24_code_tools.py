"""V24/L2 — `code_write` / `code_read` / `code_list`: a REAL, path-traversal-proof text workspace.

RED-FIRST.  This file is written BEFORE `tool_registry` learns the tools and BEFORE
`policy_firewall` grants them; on the untouched tree every leg fails by NAME ("unknown tool
'code_write': this registry ships exactly ...") — the RED run is `raw/RED-code.txt`.

WHAT IT PINS (the frozen V24 interface, verbatim from the wave plan):

  * the workspace root is `settings.code_workspace_dir` (default `data/workspace`, env
    override `CODE_WORKSPACE_DIR`) — a REAL directory on disk, created lazily;
  * `code_write {path, content}` -> `{path, bytes}`: UTF-8 text only, <= 256 KiB, relative
    paths only, parents created, overwrite INSIDE the workspace allowed and recorded;
  * `code_read {path}` -> `{path, text, bytes}`: UTF-8 text only, <= 256 KiB, missing file is
    `E_NOT_FOUND`, a directory / binary / non-UTF-8 / oversized file is `E_VALIDATION`;
  * `code_list {path?}` -> `{path, entries:[{path, bytes, kind}], returned, truncated, note}`:
    RELATIVE paths plus sizes;
  * traversal-proof BY NAME: absolute paths (POSIX and Windows), `..` (both separators), and
    symlink/reparse-point escapes are refused with `E_VALIDATION` — and nothing is written
    outside the root;
  * the allowlist: `code_generation` holds all three; the orchestrator does NOT (its own
    docstring: "It cannot fetch, post or spend ... a planner that can act is a planner that can
    be injected into acting") — every other agent is refused by the firewall, by name.

The workspace is a REAL `tmp_path` directory; the escape link is a REAL symlink on POSIX and a
REAL NTFS junction on Windows (no privilege needed) — a labelled fixture, never a mock of the
code under test.
"""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from app.agents import policy_firewall as pf
from app.agents import tool_registry
from app.config import Settings

CAP = 256 * 1024


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    """Point the setting at a real, empty directory for this test."""
    from app.config import settings

    root = tmp_path / "workspace"
    monkeypatch.setattr(settings, "code_workspace_dir", str(root))
    return root


def _payload(raw: str) -> dict:
    return json.loads(raw)


def _call(name: str, arguments: dict, *, agent_type: str = "code_generation") -> dict:
    return _payload(tool_registry.execute_tool(name, arguments, agent_type=agent_type))


def _write(path: str, content: str) -> dict:
    return _call("code_write", {"path": path, "content": content})


def _read(path: str) -> dict:
    return _call("code_read", {"path": path})


def _list(path: str | None = None) -> dict:
    return _call("code_list", {} if path is None else {"path": path})


def _escape_link(root, target):
    """A REAL escape link inside the workspace: symlink on POSIX, junction on Windows.

    Junctions need no privilege on Windows (symlinks do); both are reparse points that
    `Path.resolve()` follows, which is exactly the escape the tool must refuse.
    """
    root.mkdir(parents=True, exist_ok=True)
    link = root / "escape"
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:  # pragma: no cover - host without junction support
            pytest.skip(f"cannot create a junction on this host: {result.stderr or result.stdout}")
    else:
        os.symlink(target, link)
    return link


# ── the frozen interface + the setting ──────────────────────────────────────────────────────

def test_the_three_specs_are_frozen_function_specs():
    specs = {s["function"]["name"]: s for s in tool_registry.TOOL_SPECS}
    for name in ("code_write", "code_read", "code_list"):
        assert name in specs, name
        assert specs[name]["type"] == "function"
        function = specs[name]["function"]
        parameters = function["parameters"]
        assert parameters["type"] == "object"
        assert parameters["additionalProperties"] is False
        assert name in tool_registry.TOOL_GUIDANCE
    assert specs["code_write"]["function"]["parameters"]["required"] == ["path", "content"]
    assert specs["code_read"]["function"]["parameters"]["required"] == ["path"]
    assert specs["code_list"]["function"]["parameters"]["required"] == []


def test_the_setting_defaults_to_data_workspace():
    assert Settings.model_fields["code_workspace_dir"].default == "data/workspace"
    assert tool_registry.CODE_WORKSPACE_DEFAULT == "data/workspace"
    assert tool_registry.CODE_WRITE_MAX_BYTES == CAP


def test_the_allowlist_grants_the_code_tools_to_code_generation_only():
    assert tool_registry.tools_for("code_generation") == (
        "code_write", "code_read", "code_list")
    # The orchestrator DECISION (evidence quoted in the lane RECEIPT): it plans from the context
    # it is given and must not act; a workspace reader would widen a planner into an actor.
    assert tool_registry.tools_for("orchestrator") == (
        "macro_snapshot", "pricing_catalog", "phone_read")
    holders = {
        agent_type for agent_type, cap in pf.AGENT_CAPABILITIES.items()
        if set(cap.agent_tools) & {"code_write", "code_read", "code_list"}
    }
    assert holders == {"code_generation"}


# ── write → read → list round-trip ─────────────────────────────────────────────────────────

def test_write_read_list_round_trip(workspace):
    written = _write("notes/plan.md", "line one\nline two — ünïcode ✅\n")

    assert written["ok"] is True and written["tool"] == "code_write"
    assert written["result"] == {"path": "notes/plan.md",
                                 "bytes": len("line one\nline two — ünïcode ✅\n".encode())}
    assert (workspace / "notes" / "plan.md").is_file(), "a REAL file on disk, parents created"
    raw = (workspace / "notes" / "plan.md").read_bytes()
    assert raw == "line one\nline two — ünïcode ✅\n".encode(), "UTF-8 bytes, no newline rewrite"

    read = _read("notes/plan.md")
    assert read["ok"] is True and read["tool"] == "code_read"
    assert read["result"] == {"path": "notes/plan.md",
                              "text": "line one\nline two — ünïcode ✅\n",
                              "bytes": len(raw)}

    listed = _list()
    assert listed["ok"] is True and listed["tool"] == "code_list"
    result = listed["result"]
    assert result["path"] == "."
    assert result["truncated"] is False
    by_path = {entry["path"]: entry for entry in result["entries"]}
    assert by_path["notes"]["kind"] == "dir" and by_path["notes"]["bytes"] is None

    inside = _list("notes")
    assert inside["result"]["path"] == "notes"
    assert inside["result"]["entries"] == [
        {"path": "notes/plan.md", "bytes": len(raw), "kind": "file"}]


def test_an_existing_file_is_overwritten_inside_the_workspace_and_bytes_are_recorded(workspace):
    _write("a.txt", "first version is longer")
    second = _write("a.txt", "second")

    assert second["result"] == {"path": "a.txt", "bytes": 6}
    assert _read("a.txt")["result"]["text"] == "second"
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "second"


def test_an_empty_file_is_honest_zero_bytes(workspace):
    written = _write("empty.txt", "")

    assert written["result"] == {"path": "empty.txt", "bytes": 0}
    assert _read("empty.txt")["result"]["text"] == ""


def test_the_size_cap_is_the_frozen_256_kib(workspace):
    at_cap = _write("big.txt", "x" * CAP)
    assert at_cap["ok"] is True and at_cap["result"]["bytes"] == CAP

    over = _write("bigger.txt", "x" * (CAP + 1))
    assert over["ok"] is False and over["error"]["code"] == "E_VALIDATION"
    assert str(CAP) in over["error"]["message"]
    assert not (workspace / "bigger.txt").exists(), "a refused write writes nothing"


def test_read_refuses_a_file_over_the_cap_by_name(workspace):
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "huge.txt").write_bytes(b"y" * (CAP + 1))

    payload = _read("huge.txt")

    assert payload["ok"] is False and payload["error"]["code"] == "E_VALIDATION"
    assert str(CAP) in payload["error"]["message"]


# ── traversal / absolute / symlink refusals (by name) ──────────────────────────────────────

@pytest.mark.parametrize("path", [
    "../escape.txt",
    "a/../../escape.txt",
    "..\\escape.txt",
    "a/..",
    "/etc/passwd",
    "C:\\Windows\\win.ini",
    "C:/Windows/win.ini",
    "\\\\server\\share\\x.txt",
    "x\x00y.txt",
])
def test_traversal_and_absolute_paths_are_refused_by_name(workspace, tmp_path, path):
    payload = _write(path, "nope")

    assert payload["ok"] is False, path
    assert payload["error"]["code"] == "E_VALIDATION", payload
    assert payload["error"]["context"]["tool"] == "code_write"
    assert not (tmp_path / "escape.txt").exists(), "nothing escapes the workspace"


@pytest.mark.parametrize("path", ["../escape.txt", "/etc/passwd", "a/../../x"])
def test_read_and_list_refuse_traversal_too(workspace, path):
    read = _read(path)
    listed = _list(path)

    assert read["ok"] is False and read["error"]["code"] == "E_VALIDATION"
    assert listed["ok"] is False and listed["error"]["code"] == "E_VALIDATION"


def test_a_symlink_or_junction_escape_is_refused_for_write_read_and_list(workspace, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("internal secret", encoding="utf-8")
    _escape_link(workspace, outside)

    write = _write("escape/evil.txt", "exfiltrate")
    read = _read("escape/secret.txt")
    listed = _list("escape")

    assert write["ok"] is False and write["error"]["code"] == "E_VALIDATION"
    assert "escape" in write["error"]["message"].lower() or "workspace" in write["error"]["message"].lower()
    assert not (outside / "evil.txt").exists(), "no byte crossed the workspace boundary"
    assert read["ok"] is False and read["error"]["code"] == "E_VALIDATION"
    assert listed["ok"] is False and listed["error"]["code"] == "E_VALIDATION"


def test_a_blank_path_is_refused_for_write_and_read(workspace):
    for name, arguments in (("code_write", {"path": "  ", "content": "x"}),
                            ("code_read", {"path": ""})):
        payload = _call(name, arguments)
        assert payload["ok"] is False and payload["error"]["code"] == "E_VALIDATION", name


# ── read refusals: absent / directory / binary / non-UTF-8 ─────────────────────────────────

def test_reading_a_missing_file_is_E_NOT_FOUND(workspace):
    payload = _read("does/not/exist.txt")

    assert payload["ok"] is False
    assert payload["error"]["code"] == "E_NOT_FOUND"
    assert "does/not/exist.txt" in payload["error"]["message"]


def test_reading_a_directory_is_refused(workspace):
    (workspace / "adir").mkdir(parents=True)

    payload = _read("adir")

    assert payload["ok"] is False and payload["error"]["code"] == "E_VALIDATION"


def test_binary_content_is_refused_on_write_and_on_read(workspace):
    nul = _write("nul.txt", "abc\x00def")
    assert nul["ok"] is False and nul["error"]["code"] == "E_VALIDATION"

    (workspace / "bin.dat").write_bytes(b"\x00\x01\x02binary")
    payload = _read("bin.dat")
    assert payload["ok"] is False and payload["error"]["code"] == "E_VALIDATION"
    assert "binary" in payload["error"]["message"].lower()


def test_non_utf8_bytes_are_refused(workspace):
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "latin1.txt").write_bytes(b"caf\xe9")

    payload = _read("latin1.txt")

    assert payload["ok"] is False and payload["error"]["code"] == "E_VALIDATION"
    assert "utf-8" in payload["error"]["message"].lower()


def test_a_non_string_content_is_refused(workspace):
    for content in (None, 42, ["x"], {"a": 1}):
        payload = _call("code_write", {"path": "x.txt", "content": content})
        assert payload["ok"] is False and payload["error"]["code"] == "E_VALIDATION", content
    assert not (workspace / "x.txt").exists()


def test_a_lone_surrogate_content_is_refused(workspace):
    """JSON can carry `"\\ud800"`; it is not UTF-8 text, so the write is refused by name."""
    payload = _write("surrogate.txt", "broken \ud800")

    assert payload["ok"] is False and payload["error"]["code"] == "E_VALIDATION"
    assert not (workspace / "surrogate.txt").exists()


# ── list semantics ─────────────────────────────────────────────────────────────────────────

def test_listing_an_empty_workspace_is_an_honest_empty_list(workspace):
    payload = _list()

    assert payload["ok"] is True
    assert payload["result"]["entries"] == []
    assert payload["result"]["note"], "an empty listing says so"


def test_listing_is_capped_with_an_honest_truncation_note(workspace, monkeypatch):
    monkeypatch.setattr(tool_registry, "CODE_LIST_MAX_ENTRIES", 2)
    for index in range(3):
        _write(f"file{index}.txt", "x")

    payload = _list()

    result = payload["result"]
    assert result["returned"] == 2
    assert result["truncated"] is True
    assert "2" in result["note"]


def test_listing_only_uses_relative_paths(workspace):
    _write("deep/nested/file.txt", "x")

    root_entries = _list()["result"]["entries"]
    nested_entries = _list("deep/nested")["result"]["entries"]

    assert [entry["path"] for entry in root_entries] == ["deep"]
    assert [entry["path"] for entry in nested_entries] == ["deep/nested/file.txt"]
    assert nested_entries[0]["bytes"] == 1


# ── the firewall: who may call these at all ────────────────────────────────────────────────

@pytest.mark.parametrize("agent_type", sorted(set(pf.AGENT_CAPABILITIES) - {"code_generation"}))
def test_every_agent_outside_the_allowlist_is_refused_by_name(workspace, agent_type):
    payload = _call("code_write", {"path": "x.txt", "content": "x"}, agent_type=agent_type)

    assert payload["ok"] is False
    assert payload["error"]["code"] == "E_POLICY_DENIED"
    assert "code_write" in payload["error"]["message"]
    assert agent_type in payload["error"]["message"]
    assert not (workspace / "x.txt").exists()


def test_the_orchestrator_cannot_read_or_list_the_workspace(workspace):
    """The evidence-based decision: the orchestrator plans from the context it is given ("It
    cannot fetch, post or spend ... a planner that can act is a planner that can be injected
    into acting").  It keeps its macro/pricing/phone reads and gains NO code surface."""
    for name in ("code_read", "code_list", "code_write"):
        payload = _call(name, {"path": ".", "content": "x"} if name == "code_write" else {},
                        agent_type="orchestrator")
        assert payload["ok"] is False and payload["error"]["code"] == "E_POLICY_DENIED", name


def test_an_unknown_agent_is_refused(workspace):
    payload = _call("code_list", {}, agent_type="no_such_agent")

    assert payload["ok"] is False and payload["error"]["code"] == "E_POLICY_DENIED"


# ── the root itself ───────────────────────────────────────────────────────────────────────

def test_a_workspace_root_that_is_a_file_is_a_named_config_refusal(tmp_path, monkeypatch):
    from app.config import settings

    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(settings, "code_workspace_dir", str(blocker))

    payload = _list()

    assert payload["ok"] is False
    assert payload["error"]["code"] == "E_CONFIG"
    assert payload["error"]["how_to_fix"]


def test_the_workspace_root_is_created_lazily(workspace):
    assert not workspace.exists()

    payload = _list()

    assert payload["ok"] is True
    assert workspace.is_dir(), "the tool creates the settings.code_workspace_dir root"
