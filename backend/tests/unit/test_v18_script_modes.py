"""V18 — repo-wide guard: every shell entrypoint under `scripts/` is executable IN GIT.

WHY THIS FILE EXISTS (three occurrences of one class): `scripts/backup.sh`, `scripts/restore.sh` and
`scripts/smoke.sh` were each committed from Windows as mode 100644.  On Windows
`os.access(path, os.X_OK)` is effectively always true, so every per-file check passed locally while
CI's Linux checkout refused to exec the script (`Permission denied` / an exec-bit assertion red).
Git's index mode is platform-independent — this test asserts THAT, so the class cannot ship again.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def _git_ls_scripts() -> dict[str, str]:
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-s", "scripts/"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    modes: dict[str, str] = {}
    for line in out.splitlines():
        meta, _tab, path = line.partition("\t")
        if path:
            modes[path] = meta.split()[0]
    return modes


def test_every_scripts_shell_entrypoint_is_executable_in_git() -> None:
    modes = _git_ls_scripts()
    shell = {path: mode for path, mode in modes.items() if path.endswith(".sh")}
    assert shell, "expected shell entrypoints under scripts/"
    wrong = {path: mode for path, mode in shell.items() if mode != "100755"}
    assert not wrong, (
        "shell entrypoints must be mode 100755 in git (Windows os.access cannot see the git mode); "
        f"fix with: git update-index --chmod=+x <path> — offenders: {wrong}"
    )
