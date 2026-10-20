"""V26/L3 — run-context plumbing: worker → `invoke()` → `execute_tool()` → `runs/<id>/` (RED-first).

Written BEFORE `deepseek_runtime.invoke(..., run_id=...)` and BEFORE
`tool_registry.cleanup_expired_run_workspaces(...)` exist — the RED run is
`raw/RED-run-context.txt` on base `298bb4a`.

WHAT IT PINS (the brief's acceptance checks, one test each):

  * the runtime `invoke` gains exactly ONE optional keyword — `run_id`, keyword-only,
    default `None` — and every pre-V26 caller keeps the old signature;
  * a run id reaches the executor (`execute_tool`), and a run WITHOUT one calls the
    executor with the pre-V25 signature (no `run_id` keyword at all), so old doubles and
    old call sites keep working;
  * a live `invoke(run_id=...)` that calls `code_write` lands under
    `<settings.code_workspace_dir>/runs/<id>/`, `code_list` sees it there, and the shared
    root stays empty; the same run without an id writes to the shared root byte-identically
    and creates no `runs/` tree;
  * a hostile id (`../escape`) never reaches the filesystem: the tool reports
    `E_VALIDATION` and nothing is created outside the workspace;
  * `cleanup_expired_run_workspaces(...)` removes ONLY expired run directories, keeps the
    newest N regardless of age, never touches the shared root's files, never follows a
    link/junction out of `runs/`, and reports the names it removed;
  * the worker (`_execute_agent_task`) hands the `agent_runs.id` it owns to the agent as
    `task_dict["run_id"]` (unit-level, with a LABELLED double at the crew_factory seam);
  * the base agent forwards the published id to the runtime — and omits the keyword for a
    run that has none.

No network: the provider is `httpx.MockTransport`; the worker's crew seam is a labelled
spy (the sanctioned test double, never product code). The workspace is a REAL `tmp_path`
directory and the escape link is a REAL symlink (POSIX) / NTFS junction (Windows).
"""
from __future__ import annotations

import inspect
import json
import os
import subprocess

import httpx
import pytest

from app.agents import claude_cli, deepseek_runtime, tool_registry
from app.agents.base_agent import BasePolsiaAgent

#: Clearly fake, ASCII-only (httpx encodes header values as ASCII) — house pattern.
SAFE_TEST_KEY = "fake-deepseek-key-v26"

RUN = "run-26-l3"
NOW = 1_760_000_000.0

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
}


@pytest.fixture(autouse=True)
def _clean_session_store():
    deepseek_runtime._reset_session_store()
    yield
    deepseek_runtime._reset_session_store()


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    """The real code workspace for this test (settings read at call time)."""
    from app.config import settings

    root = tmp_path / "workspace"
    monkeypatch.setattr(settings, "code_workspace_dir", str(root))
    return root


# ── provider scripting (MockTransport; the runtime's HTTP seam) ─────────────────────────────

class _Scripted:
    """One scripted provider: each call pops the next payload; extras are a hard failure."""

    def __init__(self, *payloads) -> None:
        self.queue = list(payloads)
        self.requests: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append({"url": str(request.url), "method": request.method,
                              "body": json.loads(request.content)})
        assert self.queue, "unscripted extra provider request (the loop over-called)"
        return httpx.Response(200, json=self.queue.pop(0))

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))

    def messages(self, index: int) -> list[dict]:
        return self.requests[index]["body"]["messages"]

    @property
    def calls(self) -> int:
        return len(self.requests)


def _completion(content, *, finish: str = "stop", tool_calls=None) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {
        "id": "chat-v26",
        "object": "chat.completion",
        "model": "deepseek-flash",
        "choices": [{"index": 0, "finish_reason": finish, "message": message}],
        "usage": {"total_tokens": 18, "prompt_tokens": 11, "completion_tokens": 7},
    }


def _tool_call(call_id: str, name: str, arguments: dict) -> dict:
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}


def _inv(prompt: str = "do the thing") -> claude_cli.Invocation:
    return claude_cli.build_invocation(
        prompt=prompt,
        json_schema=None,
        resume_session=None,
        allowed_tools=("code_write", "code_read", "code_list"),
        disallowed_tools=(),
        tools_available="default",
        timeout_s=30,
        schema_name="code_generation",
    )


def _invoke(server: _Scripted, **kwargs) -> dict:
    result = deepseek_runtime.invoke(
        _inv(), client=server.client(), api_key=SAFE_TEST_KEY, **kwargs
    )
    return json.loads(result.stdout)


def _tool_payload(message: dict) -> dict:
    return json.loads(message["content"])


# ── 1. the runtime seam gains exactly one optional keyword ──────────────────────────────────

def test_invoke_gains_exactly_one_optional_run_id():
    signature = inspect.signature(deepseek_runtime.invoke)
    assert list(signature.parameters) == [
        "inv", "client", "api_key", "model", "tools", "agent_type", "task_title",
        "images", "run_id",
    ]
    run_id = signature.parameters["run_id"]
    assert run_id.kind is inspect.Parameter.KEYWORD_ONLY
    assert run_id.default is None


# ── 2/3. the id reaches the executor; no id keeps the OLD executor signature ─────────────────

def test_the_run_id_reaches_execute_tool_through_the_runtime_seam(monkeypatch):
    seen: list[dict] = []

    def fake_execute(name, arguments, *, agent_type, task_title=None, run_id=None):
        seen.append({"name": name, "agent_type": agent_type, "run_id": run_id})
        return json.dumps({"ok": True, "tool": name, "result": {}})

    monkeypatch.setattr(tool_registry, "execute_tool", fake_execute)
    call = {"id": "call_1", "type": "function",
            "function": {"name": "code_list", "arguments": "{}"}}

    messages, reached = deepseek_runtime._execute_tool_calls(
        [call], agent_type="code_generation", url="http://test", run_id=RUN
    )

    assert reached == ["code_list"]
    assert seen == [{"name": "code_list", "agent_type": "code_generation", "run_id": RUN}]
    assert messages[0]["role"] == "tool" and messages[0]["tool_call_id"] == "call_1"


def test_a_run_without_an_id_calls_execute_tool_with_the_old_signature(monkeypatch):
    """Every pre-V26 executor double (and every pre-V25 call site) must keep working."""
    seen: list[dict] = []

    def fake_execute(name, arguments, *, agent_type):  # NOTE: no task_title / run_id
        seen.append({"name": name, "agent_type": agent_type})
        return json.dumps({"ok": True})

    monkeypatch.setattr(tool_registry, "execute_tool", fake_execute)
    call = {"id": "call_1", "type": "function",
            "function": {"name": "code_list", "arguments": "{}"}}

    messages, _ = deepseek_runtime._execute_tool_calls(
        [call], agent_type="code_generation", url="http://test",
        task_title=None, run_id=None,
    )

    assert seen == [{"name": "code_list", "agent_type": "code_generation"}]
    assert json.loads(messages[0]["content"]) == {"ok": True}


# ── 4. a live run writes/lists under runs/<id>/; no id stays at the shared root ─────────────

def test_a_live_run_writes_and_lists_under_its_own_run_directory(workspace):
    server = _Scripted(
        _completion(None, finish="tool_calls", tool_calls=[
            _tool_call("c1", "code_write",
                       {"path": "notes/plan.md", "content": "v26 run context"}),
        ]),
        _completion('{"summary": "wrote it"}'),
    )

    envelope = _invoke(server, run_id=RUN)

    run_file = workspace / tool_registry.RUN_WORKSPACE_SUBDIR / RUN / "notes" / "plan.md"
    assert run_file.is_file(), f"the write did not land under runs/{RUN}/"
    assert run_file.read_text(encoding="utf-8") == "v26 run context"
    assert not (workspace / "notes").exists(), "the shared root must stay untouched"
    assert envelope["result"] == '{"summary": "wrote it"}'
    assert envelope["tools_used"] == ["code_write"]

    listed = json.loads(tool_registry.execute_tool(
        "code_list", {"path": "notes"}, agent_type="code_generation", run_id=RUN))
    assert listed["ok"] is True, listed
    assert [entry["path"] for entry in listed["result"]["entries"]] == ["notes/plan.md"]

    shared_listing = json.loads(tool_registry.execute_tool(
        "code_list", {}, agent_type="code_generation"))
    assert shared_listing["ok"] is True, shared_listing
    # V26 re-pin (old → new): the shared listing was asserted EMPTY; with a per-run tree the
    # shared root legitimately carries the `runs/` directory itself, so the honest assertion is
    # that the run's own files are not exposed — plus a read that must be E_NOT_FOUND.  This is
    # not a loosening of the isolation claim; it re-pins it to what the root may actually show.
    assert "notes" not in [entry["path"] for entry in shared_listing["result"]["entries"]], (
        "the shared root must not expose the run's directory"
    )
    shared_read = json.loads(tool_registry.execute_tool(
        "code_read", {"path": "notes/plan.md"}, agent_type="code_generation"))
    assert shared_read["ok"] is False and shared_read["error"]["code"] == "E_NOT_FOUND"


def test_a_live_run_without_an_id_keeps_the_shared_root_byte_identical(workspace):
    server = _Scripted(
        _completion(None, finish="tool_calls", tool_calls=[
            _tool_call("c1", "code_write", {"path": "shared.txt", "content": "old path"}),
        ]),
        _completion('{"summary": "wrote it"}'),
    )

    _invoke(server)

    assert (workspace / "shared.txt").read_text(encoding="utf-8") == "old path"
    assert not (workspace / tool_registry.RUN_WORKSPACE_SUBDIR).exists(), (
        "a run without an id must not create the runs/ tree at all"
    )


# ── 5. a hostile id never reaches the filesystem through invoke ─────────────────────────────

def test_a_hostile_run_id_never_reaches_the_filesystem_through_invoke(workspace):
    server = _Scripted(
        _completion(None, finish="tool_calls", tool_calls=[
            _tool_call("c1", "code_write", {"path": "evil.txt", "content": "escape"}),
        ]),
        _completion('{"summary": "refused"}'),
    )

    _invoke(server, run_id="../escape")

    tool_message = server.messages(1)[-1]
    payload = _tool_payload(tool_message)
    assert payload["ok"] is False, payload
    assert payload["error"]["code"] == "E_VALIDATION", payload
    assert not (workspace / "escape").exists()
    assert not (workspace.parent / "escape").exists()
    assert not (workspace / "evil.txt").exists()


def test_a_non_string_run_id_is_a_named_refusal_not_a_shared_root_write(workspace):
    """A bad id must never be silently dropped: dropping it would run the tool at the shared root."""
    call = {"id": "call_1", "type": "function",
            "function": {"name": "code_write",
                         "arguments": json.dumps({"path": "evil.txt", "content": "x"})}}

    messages, _ = deepseek_runtime._execute_tool_calls(
        [call], agent_type="code_generation", url="http://test", run_id=123
    )

    payload = _tool_payload(messages[0])
    assert payload["ok"] is False, payload
    assert payload["error"]["code"] == "E_VALIDATION", payload
    assert not workspace.exists(), "a refused id must not create the workspace at all"


# ── 6. bounded retention cleanup ────────────────────────────────────────────────────────────

def _make_run(root, name: str, age_hours: float, *, now: float = NOW) -> None:
    run_dir = root / tool_registry.RUN_WORKSPACE_SUBDIR / name
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "a.txt").write_text(name, encoding="utf-8")
    stamp = now - age_hours * 3600.0
    os.utime(run_dir, (stamp, stamp))


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


def test_cleanup_removes_only_expired_run_dirs_and_preserves_the_root(workspace):
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "shared.txt").write_text("shared root file", encoding="utf-8")
    runs_dir = workspace / tool_registry.RUN_WORKSPACE_SUBDIR
    runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / "stray.txt").write_text("not a run dir", encoding="utf-8")
    for name, age in (("run-0h", 0.0), ("run-05h", 0.5), ("run-2h", 2.0),
                      ("run-3h", 3.0), ("run-4h", 4.0)):
        _make_run(workspace, name, age)

    outcome = tool_registry.cleanup_expired_run_workspaces(
        workspace_root=workspace, keep_latest=2, max_age_hours=1.0, now=NOW
    )

    assert outcome["count"] == 3, outcome
    assert sorted(outcome["removed"]) == ["run-2h", "run-3h", "run-4h"]
    assert outcome["kept"] == 2
    assert outcome["policy"] == {"keep_latest": 2, "max_age_hours": 1.0}
    for kept in ("run-0h", "run-05h"):
        assert (runs_dir / kept / "a.txt").is_file(), kept
    for gone in ("run-2h", "run-3h", "run-4h"):
        assert not (runs_dir / gone).exists(), gone
    assert (workspace / "shared.txt").read_text(encoding="utf-8") == "shared root file"
    assert (runs_dir / "stray.txt").is_file(), "a non-run file under runs/ is never deleted"
    assert workspace.is_dir(), "the workspace root itself is never removed"


def test_cleanup_never_follows_a_link_out_of_the_runs_tree(workspace, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep me", encoding="utf-8")
    runs_dir = workspace / tool_registry.RUN_WORKSPACE_SUBDIR
    _escape_link(runs_dir, outside)
    stamp = NOW - 5 * 3600.0
    os.utime(runs_dir / "escape", (stamp, stamp))

    outcome = tool_registry.cleanup_expired_run_workspaces(
        workspace_root=workspace, keep_latest=0, max_age_hours=1.0, now=NOW
    )

    assert outcome["removed"] == [], outcome
    assert any(item["name"] == "escape" and item["reason"] == "link"
               for item in outcome["skipped"]), outcome
    assert (outside / "keep.txt").is_file(), "a link must never be followed out of runs/"


# ── 7. the worker owns the run row and hands its id over ────────────────────────────────────

def test_the_worker_hands_the_run_id_to_the_invoker(queue_db, execute_body, monkeypatch):
    import app.agents.crew_factory as crew_factory

    seen: list[dict] = []

    def spy(agent_type, task_dict, context):
        # LABELLED DOUBLE: records the worker→agent seam; runs no agent.
        seen.append({"agent_type": agent_type, "task_dict": task_dict})
        return {"summary": "worker seam captured"}

    monkeypatch.setattr(crew_factory, "run_agent_for_task", spy)
    task_id = queue_db.create_task(title="Ship the run-context fix", agent_type="social_media")

    outcome = execute_body(task_id)

    assert outcome["status"] == "completed", outcome
    rows = queue_db.query(
        "SELECT id, status FROM agent_runs WHERE task_id = ? ORDER BY id", (task_id,))
    assert len(rows) == 1, rows
    assert seen[0]["task_dict"]["run_id"] == str(rows[0]["id"]), (
        "the worker must hand the run row's id to the agent"
    )


# ── 8. the base agent forwards the published id (and omits it when absent) ──────────────────

class _Agent(BasePolsiaAgent):
    """Minimal real BasePolsiaAgent subclass for the runtime dispatch seam."""

    agent_type = "test"

    def run(self, task, context):  # pragma: no cover - never dispatched here
        return {"summary": "test"}


def _ok_result() -> claude_cli.InvokeResult:
    envelope = {"type": "result", "subtype": "success", "is_error": False,
                "result": '{"summary": "ok"}', "usage": {}, "num_turns": 1}
    return claude_cli.InvokeResult(stdout=json.dumps(envelope), stderr="", exit_code=0,
                                   wall_seconds=0.01, transport="http")


def test_the_agent_forwards_the_run_id_to_the_runtime(monkeypatch):
    monkeypatch.setattr(deepseek_runtime, "_resolve_api_key", lambda: "key-present")
    seen: dict = {}

    def fake_deepseek(inv, *, task_title=None, images=None, run_id=None):
        seen["run_id"] = run_id
        return _ok_result()

    monkeypatch.setattr(deepseek_runtime, "invoke", fake_deepseek)
    agent = _Agent()
    agent.set_run_context({"title": "Ship the fix", "run_id": RUN})

    agent._real_call("hello deepseek")

    assert seen["run_id"] == RUN


def test_a_run_without_an_id_calls_the_runtime_with_the_old_signature(monkeypatch):
    """A run that owns no id must not send the keyword — pre-V26 doubles stay valid."""
    monkeypatch.setattr(deepseek_runtime, "_resolve_api_key", lambda: "key-present")
    calls: list[dict] = []

    def old_signature_invoke(inv, *, task_title=None, images=None):  # NOTE: no run_id
        calls.append({"task_title": task_title})
        return _ok_result()

    monkeypatch.setattr(deepseek_runtime, "invoke", old_signature_invoke)
    agent = _Agent()
    agent.set_run_context({"title": "Ship the fix"})

    agent._real_call("hello deepseek")

    assert calls == [{"task_title": "Ship the fix"}]
