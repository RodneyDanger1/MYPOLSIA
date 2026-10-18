"""Wave V17 / lane L5 — `scripts/restore.sh`: the restore drill that did not exist.

RED-first pins, written before the script.  Every pin here is either STATIC (the script must
contain the safety structure: trap teardown, no published ports, no compose project) or
DRY-RUN/REFUSAL (the paths that must never touch docker).  The real drill — an actual
postgres:16-alpine container, an actual load of `backups/polsia-*.sql`, actual verification
queries — is EXECUTED by the lane and its log lives in the wave receipt; a unit test must not
pretend to prove it, and no docker is required to run this file.

The docker seam is a PATH shim (same process-boundary seam `test_v15_e4_backup_script.py` uses
for pg_dump): a shim that records every invocation proves dry-run invokes NOTHING, and a shim
that reports a container name proves the name guard refuses before any container is created.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "restore.sh"

CONTAINER = "polsia-restore-drill-pg"
CANONICAL_VOLUMES = ("backend_data", "chroma_data", "celery_beat_data", "redis_data")

# A docker shim that records every call and answers the two read-only probes the script makes
# before it creates anything.  No state, no containers, no daemon.
DOCKER_SHIM = """#!/bin/sh
echo "$*" >> "$DOCKER_CALL_LOG"
case "$1" in
  ps) exit 0 ;;
  inspect) exit 1 ;;
  volume) exit 1 ;;
esac
exit 0
"""

# A docker shim that claims the drill container name already exists.
DOCKER_NAME_TAKEN = f"""#!/bin/sh
echo "$*" >> "$DOCKER_CALL_LOG"
case "$1" in
  ps) echo "{CONTAINER}" ;;
esac
exit 0
"""


def _make_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8", newline="\n")
    os.chmod(path, 0o755)


def _sh() -> str:
    found = shutil.which("sh")
    assert found, "this suite drives scripts/restore.sh through a real POSIX sh"
    return found


def _make_artifacts(src: Path, *, sql: bool = True, volumes=CANONICAL_VOLUMES) -> Path:
    """A backup dir shaped exactly like backup.sh's output (no docker involved)."""
    src.mkdir(parents=True, exist_ok=True)
    if sql:
        (src / "polsia-2026-10-14.sql").write_text(
            "--\n-- PostgreSQL database dump\n--\n\n"
            "SET statement_timeout = 0;\n"
            "CREATE TABLE public.tasks (id integer);\n"
            "COPY public.tasks (id) FROM stdin;\n1\n2\n3\n\\.\n",
            encoding="utf-8",
        )
    for name in volumes:
        archive = src / f"{name}.tgz"
        payload = src / f"{name}-payload"
        payload.mkdir(exist_ok=True)
        (payload / "payload.txt").write_text(f"{name} payload\n", encoding="utf-8")
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(payload / "payload.txt", arcname="./payload.txt")
        shutil.rmtree(payload)
    return src


def _run(tmp_path: Path, *args: str, docker_body: str | None = DOCKER_SHIM,
         docker_present: bool = True) -> tuple[subprocess.CompletedProcess[str], Path]:
    shim = tmp_path / "shim"
    shim.mkdir(exist_ok=True)
    call_log = tmp_path / "docker-calls.log"
    call_log.write_text("", encoding="utf-8")
    if docker_present:
        _make_executable(shim / "docker", docker_body or DOCKER_SHIM)
        path = shim.as_posix() + os.pathsep + os.environ["PATH"]
    else:
        # Simulate "no docker anywhere" PORTABLY.  The Windows trick (PATH = sh's own dir) relied
        # on docker living outside it; on the Linux CI runner docker IS /usr/bin/docker, so the
        # script found it, tried to run it and exited 1 — measured red on 2026-10-18.  On POSIX,
        # build a tool dir holding symlinks to ONLY the tools restore.sh needs.
        if os.name == "nt":
            path = Path(shutil.which("sh")).parent.as_posix()  # docker is not beside sh on Windows
        else:
            tools = (
                "sh", "cat", "ls", "wc", "head", "grep", "tar", "gzip", "date", "mktemp",
                "realpath", "stat", "sed", "awk", "sort", "tail", "cut", "tr", "basename", "dirname",
            )
            tooldir = tmp_path / "shim-nodocker"
            tooldir.mkdir(exist_ok=True)
            for tool in tools:
                found = shutil.which(tool)
                if found and not (tooldir / tool).exists():
                    os.symlink(found, tooldir / tool)
            path = tooldir.as_posix()
    env = {
        **os.environ,
        "PATH": path,
        "DOCKER_CALL_LOG": call_log.as_posix(),
        "RESTORE_PG_CONTAINER": CONTAINER,
    }
    proc = subprocess.run(
        [_sh(), SCRIPT.as_posix(), *args],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    return proc, call_log


def test_script_parses_under_posix_sh() -> None:
    assert SCRIPT.is_file(), f"missing {SCRIPT}"
    proc = subprocess.run([_sh(), "-n", SCRIPT.as_posix()], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr


def test_script_is_executable_and_has_a_shebang() -> None:
    first = SCRIPT.read_text(encoding="utf-8").splitlines()[0]
    assert first.startswith("#!") and "sh" in first
    assert os.access(SCRIPT, os.X_OK), "restore.sh must be executable (git mode 100755)"


def test_dry_run_prints_the_plan_and_never_calls_docker(tmp_path: Path) -> None:
    src = _make_artifacts(tmp_path / "backups")
    proc, calls = _run(tmp_path, "--src", src.as_posix(), "--dry-run")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = proc.stdout
    assert "DRY RUN" in out.upper()
    low = out.lower()
    assert CONTAINER in out, "the plan must name the exact throwaway container"
    assert "postgres:16-alpine" in out
    assert "no published ports" in low or "no host ports" in low
    assert "never the live" in low or "compose project" in low
    assert "trap" in low or "always" in low
    assert calls.read_text(encoding="utf-8") == "", "dry run must not invoke docker at all"


def test_missing_src_refuses_by_name(tmp_path: Path) -> None:
    proc, calls = _run(tmp_path, "--dry-run")
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "--src is required" in proc.stdout
    assert calls.read_text(encoding="utf-8") == ""


def test_src_that_does_not_exist_refuses_by_name(tmp_path: Path) -> None:
    proc, _ = _run(tmp_path, "--src", (tmp_path / "nope").as_posix(), "--dry-run")
    assert proc.returncode == 2
    assert "does not exist" in proc.stdout


def test_src_without_a_database_dump_refuses_by_name(tmp_path: Path) -> None:
    src = _make_artifacts(tmp_path / "backups", sql=False)
    proc, _ = _run(tmp_path, "--src", src.as_posix(), "--dry-run")
    assert proc.returncode == 2
    assert "polsia-*.sql" in proc.stdout


def test_src_without_volume_archives_refuses_by_name(tmp_path: Path) -> None:
    src = _make_artifacts(tmp_path / "backups", volumes=())
    proc, _ = _run(tmp_path, "--src", src.as_posix(), "--dry-run")
    assert proc.returncode == 2
    assert ".tgz" in proc.stdout


def test_src_missing_a_canonical_volume_names_it(tmp_path: Path) -> None:
    src = _make_artifacts(tmp_path / "backups", volumes=("backend_data", "chroma_data"))
    proc, _ = _run(tmp_path, "--src", src.as_posix(), "--dry-run")
    assert proc.returncode == 2
    assert "celery_beat_data" in proc.stdout and "redis_data" in proc.stdout


def test_corrupt_volume_archive_refuses_by_name(tmp_path: Path) -> None:
    src = _make_artifacts(tmp_path / "backups")
    (src / "redis_data.tgz").write_bytes(b"this is not a gzip stream")
    proc, _ = _run(tmp_path, "--src", src.as_posix(), "--dry-run")
    assert proc.returncode == 2
    assert "not a readable gzip tar" in proc.stdout
    assert "redis_data.tgz" in proc.stdout


def test_empty_database_dump_refuses_by_name(tmp_path: Path) -> None:
    src = _make_artifacts(tmp_path / "backups")
    (src / "polsia-2026-10-14.sql").write_text("", encoding="utf-8")
    proc, _ = _run(tmp_path, "--src", src.as_posix(), "--dry-run")
    assert proc.returncode == 2
    assert "EMPTY" in proc.stdout


def test_refuses_when_the_drill_container_name_already_exists(tmp_path: Path) -> None:
    src = _make_artifacts(tmp_path / "backups")
    proc, calls = _run(tmp_path, "--src", src.as_posix(), docker_body=DOCKER_NAME_TAKEN)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "already exists" in proc.stdout
    log = calls.read_text(encoding="utf-8")
    assert "run" not in log.split(), f"the name guard must fire before any `docker run`:\n{log}"


def test_refuses_by_name_when_docker_is_absent(tmp_path: Path) -> None:
    src = _make_artifacts(tmp_path / "backups")
    proc, _ = _run(tmp_path, "--src", src.as_posix(), docker_present=False)
    assert proc.returncode == 2
    assert "docker" in proc.stdout.lower()


def test_static_pins_the_safety_structure() -> None:
    body = SCRIPT.read_text(encoding="utf-8")
    assert "trap " in body, "teardown must be trap-based so a failure still removes the container"
    assert "docker rm -f" in body, "teardown must force-remove the throwaway container"
    assert "--publish" not in body and "-p 5" not in body, "the drill must never publish a port"
    assert "docker compose" not in body, "the drill must never touch the compose project"
    assert "pg_isready" in body
    assert "pg_restore" in body and "psql" in body, "both dump formats must be routed"
    assert "createdb" in body
