"""Wave V15 / lane E4 — 5-04: `scripts/backup.sh` executed FOR REAL, one cycle (`--once`).

No docker on this host, and none is needed: a compose volume mount IS a directory inside the
container, and `pg_dump` is stubbed at the process boundary only (a PATH shim — the same seam the
backend suites use for HTTP).  Proves the artifacts (naming + content), the per-volume tars, the
retention prune, the named refusal on an empty/missing volume mount, that a failed pg_dump leaves
NO partial dump behind, and that the loop stops cleanly on SIGTERM.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "backup.sh"

MARKER = "-- fake pg_dump marker (test double at the process boundary)"
PG_DUMP_OK = (
    "#!/bin/sh\n"
    "out=''\n"
    "while [ $# -gt 0 ]; do\n"
    '  case "$1" in\n'
    '    -f) out="$2"; shift 2 ;;\n'
    "    *) shift ;;\n"
    "  esac\n"
    "done\n"
    '[ -n "$out" ] || { echo "fake pg_dump: no -f given" >&2; exit 2; }\n'
    f"echo '{MARKER}' > \"$out\"\n"
    "exit 0\n"
)
PG_DUMP_FAIL = "#!/bin/sh\necho 'pg_dump: error: connection to server failed' >&2\nexit 1\n"


def _make_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8", newline="\n")
    os.chmod(path, 0o755)


@pytest.fixture
def sh_path() -> str:
    found = shutil.which("sh")
    assert found, "this suite drives scripts/backup.sh through a real POSIX sh"
    return found


def _run(
    tmp_path: Path,
    sh_path: str,
    volume_names: list[str],
    *,
    empty_volumes: tuple[str, ...] = (),
    missing_volumes: tuple[str, ...] = (),
    pg_dump_body: str = PG_DUMP_OK,
    retention_days: str = "2",
    stale_files: dict[str, float] | None = None,
    args: tuple[str, ...] = ("--once",),
    timeout: int = 120,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    _make_executable(shim_dir / "pg_dump", pg_dump_body)

    backups = tmp_path / "backups"
    backups.mkdir()
    for name, age_days in (stale_files or {}).items():
        stale = backups / name
        stale.write_text("stale\n", encoding="utf-8")
        past = time.time() - age_days * 86400
        os.utime(stale, (past, past))

    volumes_root = tmp_path / "volumes"
    for name in volume_names:
        if name in missing_volumes:
            continue
        directory = volumes_root / name
        directory.mkdir(parents=True)
        if name not in empty_volumes:
            (directory / "payload.txt").write_text(f"{name} payload\n", encoding="utf-8")

    env = {
        **os.environ,
        "PATH": shim_dir.as_posix() + os.pathsep + os.environ["PATH"],
        "BACKUP_DIR": backups.as_posix(),
        "VOLUMES_ROOT": volumes_root.as_posix(),
        "BACKUP_VOLUMES": " ".join(volume_names),
        "BACKUP_RETENTION_DAYS": retention_days,
        "BACKUP_INTERVAL_SECONDS": "86400",
        "POSTGRES_USER": "polsia",
        "POSTGRES_DB": "polsia",
        "PGHOST": "fake-postgres",
    }
    proc = subprocess.run(
        [sh_path, SCRIPT.as_posix(), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    return proc, backups


def test_once_writes_the_dump_and_every_volume_archive(sh_path: str, tmp_path: Path) -> None:
    proc, backups = _run(tmp_path, sh_path, ["alpha", "beta", "gamma", "delta"])

    assert proc.returncode == 0, proc.stdout + proc.stderr
    dumps = sorted(backups.glob("polsia-*.sql"))
    assert len(dumps) == 1
    assert MARKER in dumps[0].read_text(encoding="utf-8")
    for name in ("alpha", "beta", "gamma", "delta"):
        archive = backups / f"{name}.tgz"
        assert archive.is_file(), archive
        with tarfile.open(archive) as tar:
            assert any(member.name.endswith("payload.txt") for member in tar.getmembers()), name
    assert not list(backups.glob("*.part")), "no partial artifacts may survive a green cycle"


def test_once_prunes_backups_older_than_the_retention_window(sh_path: str, tmp_path: Path) -> None:
    proc, backups = _run(
        tmp_path,
        sh_path,
        ["alpha"],
        retention_days="2",
        stale_files={"polsia-1999-01-01.sql": 5.0, "old_archive.tgz": 5.0},
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not (backups / "polsia-1999-01-01.sql").exists()
    assert not (backups / "old_archive.tgz").exists()
    assert "pruned" in proc.stdout
    fresh = sorted(backups.glob("polsia-*.sql"))
    assert len(fresh) == 1, "the fresh dump must survive the prune"
    assert (backups / "alpha.tgz").is_file()


def test_once_refuses_empty_or_missing_volume_mounts_by_name(sh_path: str, tmp_path: Path) -> None:
    proc, backups = _run(
        tmp_path,
        sh_path,
        ["alpha", "empty", "ghost"],
        empty_volumes=("empty",),
        missing_volumes=("ghost",),
    )

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert (backups / "alpha.tgz").is_file()
    assert not (backups / "empty.tgz").exists()
    assert not (backups / "ghost.tgz").exists()
    assert "empty" in proc.stdout and "EMPTY" in proc.stdout
    assert "ghost" in proc.stdout
    assert len(list(backups.glob("polsia-*.sql"))) == 1, (
        "the dump leg is independent — a refused volume must not veto the database dump"
    )


def test_failed_pg_dump_writes_no_dump_and_no_partial(sh_path: str, tmp_path: Path) -> None:
    proc, backups = _run(tmp_path, sh_path, ["alpha"], pg_dump_body=PG_DUMP_FAIL)

    assert proc.returncode == 1
    assert not list(backups.glob("polsia-*.sql"))
    assert not list(backups.glob("*.part")), "the temp .part must be removed on failure"
    assert "ERROR" in proc.stdout
    assert (backups / "alpha.tgz").is_file(), "the honest volume leg still runs"


def test_unknown_argument_refuses(sh_path: str, tmp_path: Path) -> None:
    proc, _ = _run(tmp_path, sh_path, ["alpha"], args=("--bogus",))
    assert proc.returncode == 2
    assert "unknown argument" in proc.stdout


def test_loop_stops_cleanly_on_sigterm(sh_path: str, tmp_path: Path) -> None:
    """No --once: the loop runs one cycle then sleeps; SIGTERM must end it with a clean exit."""
    proc = None
    pid_file = tmp_path / "loop.pid"
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    _make_executable(shim_dir / "pg_dump", PG_DUMP_OK)
    backups = tmp_path / "backups"
    backups.mkdir()
    volumes_root = tmp_path / "volumes"
    (volumes_root / "alpha").mkdir(parents=True)
    (volumes_root / "alpha" / "payload.txt").write_text("x\n", encoding="utf-8")
    env = {
        **os.environ,
        "PATH": shim_dir.as_posix() + os.pathsep + os.environ["PATH"],
        "BACKUP_DIR": backups.as_posix(),
        "VOLUMES_ROOT": volumes_root.as_posix(),
        "BACKUP_VOLUMES": "alpha",
        "BACKUP_INTERVAL_SECONDS": "600",
        "POSTGRES_USER": "polsia",
        "POSTGRES_DB": "polsia",
        "PGHOST": "fake-postgres",
    }
    try:
        for attempt in range(3):
            launcher = f'echo $$ > "{pid_file.as_posix()}"; exec "{SCRIPT.as_posix()}"'
            proc = subprocess.Popen(
                [sh_path, "-c", launcher],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
            )
            deadline = time.time() + 30
            while time.time() < deadline:
                if list(backups.glob("polsia-*.sql")):
                    break
                time.sleep(0.2)
            if list(backups.glob("polsia-*.sql")) and proc.poll() is None:
                break
            # Known MSYS2 fork/exec flake ("child_copy: cygheap read copy failed ... Win32 error
            # 299"): the runtime can refuse to spawn a child under ASLR/AV pressure and the loop
            # dies at startup.  Re-spawn — bounded, and only for a child that never produced the
            # first artifact.
            if proc.poll() is None:
                proc.kill()
            output = proc.communicate(timeout=10)[0] or ""
            assert attempt < 2, f"the loop never stayed up while its first cycle ran\n{output}"
            time.sleep(1.0)
        assert list(backups.glob("polsia-*.sql")), "the first cycle must run at startup"
        # SIGTERM as a REAL POSIX signal: Python's send_signal(SIGTERM) on Windows is
        # TerminateProcess (no trap runs), so the signal is delivered by MSYS `kill` — the same
        # runtime as the loop — exactly like `docker stop` sends SIGTERM to the entrypoint.
        loop_pid = pid_file.read_text(encoding="utf-8").strip()
        assert loop_pid.isdigit(), "the launcher must have recorded the loop's pid"
        kill = subprocess.run(
            [sh_path, "-c", f"kill -TERM {loop_pid}"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert kill.returncode == 0, f"MSYS kill failed: {kill.stdout}{kill.stderr}"
        output = proc.communicate(timeout=15)[0]
        assert proc.returncode == 0, f"clean exit expected, got {proc.returncode}\n{output}"
        assert "exiting cleanly" in output
    finally:
        if proc is not None and proc.poll() is None:  # never leak a child process
            proc.kill()
            proc.communicate(timeout=10)
