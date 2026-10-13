"""Child-process containment for the CLI transport (D6; audit L3-02 / L3-03).

Three promises, each pinned by a test:

  1. POSIX: the spawned CLI runs in its OWN session (`start_new_session`), and
     `_kill_process_tree` kills the whole process GROUP with SIGKILL — the old code documented
     "kill the group" but only ever called `proc.kill()` on POSIX (pytest.ini L3-02).
  2. Spill files: every `polsia-sysprompt-*.txt` is registered in `_SPILL_TMP_FILES`, and the
     atexit net removes whatever a normal crash left behind (a hard force-kill still cannot run
     handlers — named residual, L3-03).
  3. The normal path still removes the file in `finally` and deregisters it.

RED leg (observed): the `os.killpg(...)` line removed -> the killpg test fails (group kill
never happens); restore -> green.
"""
from __future__ import annotations

import os

from app.agents import claude_cli


class FakeProc:
    """Alive on the first poll (so the kill path runs), then reported dead."""

    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.killed = False
        self._polls = 0

    def poll(self):
        self._polls += 1
        return None if self._polls <= 1 else 0

    def kill(self) -> None:
        self.killed = True

    def communicate(self, timeout=None):  # noqa: ARG002 - interface parity
        return ("", "")


def test_posix_kill_kills_the_whole_process_group(monkeypatch):
    calls: dict[str, object] = {}
    # sys.platform is read at call time; os.getpgid/killpg are looked up on the os module.
    monkeypatch.setattr(claude_cli.sys, "platform", "linux")
    # `signal.SIGKILL` is Unix-only and ABSENT on the Windows host this suite also runs on (probed:
    # hasattr False -> AttributeError at the module's argument evaluation). Inject the POSIX value
    # (9) so the assertion can name exactly what the module passes to killpg; on a POSIX host this
    # re-sets the real constant to itself. (RD6 closeout fix — test-side only.)
    monkeypatch.setattr(
        claude_cli.signal, "SIGKILL", getattr(claude_cli.signal, "SIGKILL", 9), raising=False
    )
    monkeypatch.setattr(claude_cli.os, "getpgid", lambda pid: 999 + pid, raising=False)
    monkeypatch.setattr(claude_cli.os, "killpg", lambda pgid, sig: calls.update(pgid=pgid, sig=sig), raising=False)

    proc = FakeProc()
    claude_cli._kill_process_tree(proc)  # type: ignore[arg-type]

    assert calls.get("pgid") == 999 + 4242, "POSIX must kill the GROUP pid, not the child pid"
    assert calls.get("sig") == claude_cli.signal.SIGKILL
    assert proc.killed is True, "the direct child is still killed as well"


def test_windows_kill_still_uses_taskkill(monkeypatch):
    ran: list[list[str]] = []
    monkeypatch.setattr(claude_cli.sys, "platform", "win32")
    monkeypatch.setattr(
        claude_cli.subprocess, "run", lambda argv, **kw: ran.append(argv), raising=False
    )

    proc = FakeProc(pid=777)
    claude_cli._kill_process_tree(proc)  # type: ignore[arg-type]

    assert ran and ran[0][:2] == ["taskkill", "/F"] and "777" in ran[0], "Windows behavior unchanged"


def test_the_popen_runs_in_its_own_session_on_posix(monkeypatch):
    captured: dict[str, object] = {}

    class FakePopen:
        def __init__(self, argv, **kwargs) -> None:  # noqa: ARG002
            captured.update(kwargs)
            self.pid = 1
            self.returncode = 0

        def communicate(self, input=None, timeout=None):  # noqa: ARG002
            return ("{}", "")

        def poll(self):
            return 0

    monkeypatch.setattr(claude_cli.subprocess, "Popen", FakePopen)
    inv = claude_cli.Invocation(argv=["claude", "-p", "hi"], timeout_s=5)
    claude_cli.invoke(inv)

    assert captured.get("start_new_session") is (os.name == "posix"), (
        "the child must get its own session on POSIX (group-kill precondition) and not on Windows"
    )


def test_spill_file_crash_net_removes_and_cleans_up(monkeypatch, tmp_path):
    captured: dict[str, object] = {}

    class FakePopen:
        def __init__(self, argv, **kwargs) -> None:  # noqa: ARG002
            captured["argv"] = argv
            self.pid = 1
            self.returncode = 0

        def communicate(self, input=None, timeout=None):  # noqa: ARG002
            return ("{}", "")

        def poll(self):
            return 0

    monkeypatch.setattr(claude_cli.subprocess, "Popen", FakePopen)
    inv = claude_cli.Invocation(
        argv=["claude", "-p", claude_cli.SPILL_PLACEHOLDER],
        system_prompt_file_content="system prompt body",
        timeout_s=5,
    )
    claude_cli.invoke(inv)

    # The normal path: the spill file was written, used as argv[1], then removed + deregistered.
    spill = captured["argv"][2]  # type: ignore[index]
    assert str(spill).endswith(".txt") and "polsia-sysprompt-" in str(spill)
    assert not os.path.exists(str(spill)), "finally must remove the spill file"
    assert str(spill) not in claude_cli._SPILL_TMP_FILES, "and deregister it"

    # The crash net: a leaked path the finally never reached is removed by the atexit function.
    leaked = tmp_path / "polsia-sysprompt-leaked.txt"
    leaked.write_text("x")
    claude_cli._SPILL_TMP_FILES.add(str(leaked))
    try:
        claude_cli._cleanup_spill_tmp_files()
        assert not leaked.exists(), "the atexit net must remove what a crash left behind"
    finally:
        claude_cli._SPILL_TMP_FILES.discard(str(leaked))
