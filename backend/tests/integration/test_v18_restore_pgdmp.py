"""V18 / L3 proof 4 — `scripts/restore.sh` executes its PGDMP (custom-format) path for real.

The V17 UNRUN list carried "`PGDMP` custom-format restore path … implemented, not executed". This
test executes it against real containers:

1. a real `postgres:16-alpine` container gets a small schema (2 tables, 5 rows) and a REAL
   `pg_dump -Fc` is taken from it — the custom format, byte for byte;
2. a backup dir is assembled exactly as `scripts/backup.sh` leaves one: one `polsia-*.sql` dump
   plus the four volume archives as readable gzip tars;
3. `scripts/restore.sh --src <dir>` runs the drill it ships (its OWN throwaway container, default
   name `polsia-restore-drill-pg`), which must exit 0, report `format=custom`, dispatch
   `pg_restore` (never `psql`), report the restored table count, materialise all four volumes and
   print `RESULT: PASS`;
4. teardown is asserted from the outside: no `polsia-restore-drill-pg` container may remain.

Docker IS required here (the drill container is the point). If docker, bash or the drill name is
unavailable the test SKIPS with a named reason — never a silent pass. Whatever happens, the drill
container and the temp artefacts are removed in a `finally`.
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import make_url

pytestmark = [pytest.mark.integration]

REPO_ROOT = Path(__file__).resolve().parents[3]
DRILL_NAME = "polsia-restore-drill-pg"
VOLUMES = ("backend_data", "chroma_data", "celery_beat_data", "redis_data")
EXPECTED_TABLES = 2


def _docker(*args: str, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, **kwargs)


def _require_docker() -> str:
    if shutil.which("docker") is None:
        pytest.skip("SKIP-BY-NAME: docker is not on PATH — the restore drill cannot run")
    probe = _docker("version", "--format", "{{.Server.Version}}")
    if probe.returncode != 0:
        pytest.skip(f"SKIP-BY-NAME: docker daemon unreachable: {probe.stderr.strip()[:200]}")
    if _docker("ps", "-a", "--format", "{{.Names}}").stdout.split().count(DRILL_NAME):
        pytest.skip(
            f"SKIP-BY-NAME: a container named {DRILL_NAME} already exists — restore.sh refuses "
            "to reuse or replace it (live stack), so the drill cannot run here"
        )
    return probe.stdout.strip()


def _bash() -> str:
    path = shutil.which("bash")
    if path is None:
        pytest.skip("SKIP-BY-NAME: bash is not on PATH — scripts/restore.sh cannot run")
    return path


def _force_remove_drill() -> None:
    _docker("rm", "-f", DRILL_NAME)


def _write_volume_archive(path: Path, label: str) -> None:
    """A real gzip tar with a directory entry and files inside it (backup.sh's shape)."""
    with tarfile.open(path, "w:gz") as tar:
        info = tarfile.TarInfo(name=f"{label}/")
        info.type = tarfile.DIRTYPE
        info.mode = 0o755
        tar.addfile(info)
        payload = f"{label} v18 restore drill\n".encode()
        for member in ("keep.txt", "nested/also.txt"):
            data = payload + member.encode()
            member_info = tarfile.TarInfo(name=f"{label}/{member}")
            member_info.size = len(data)
            tar.addfile(member_info, io.BytesIO(data))


def test_restore_sh_runs_its_pgdmp_path_and_tears_the_drill_container_down():
    docker_version = _require_docker()
    bash = _bash()

    from testcontainers.postgres import PostgresContainer

    work = Path(tempfile.mkdtemp(prefix="v18-restore-"))
    src = work / "backups"
    src.mkdir()
    try:
        with PostgresContainer("postgres:16-alpine") as pg:
            # testcontainers' PostgresContainer API varies by version (`get_container_id` is absent
            # on recent releases; the CI runner's version raised AttributeError).  The wrapped
            # docker-py container's `.id` is stable across versions.
            container_id = pg.get_wrapped_container().id
            url = make_url(pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://"))
            user, database = url.username, url.database

            schema = _docker(
                "exec",
                container_id,
                "psql",
                "-U",
                user,
                "-d",
                database,
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                "create table v18_restore_probe(id int primary key, note text);"
                "insert into v18_restore_probe values (1,'alpha'),(2,'beta'),(3,'gamma');"
                "create table v18_restore_second(id int primary key);"
                "insert into v18_restore_second values (1),(2);",
            )
            assert schema.returncode == 0, schema.stderr

            dump = src / "polsia-20260302-120000.sql"
            with dump.open("wb") as handle:
                dumped = subprocess.run(
                    ["docker", "exec", container_id, "pg_dump", "-U", user, "-d", database, "-Fc"],
                    stdout=handle,
                    stderr=subprocess.PIPE,
                )
            assert dumped.returncode == 0, dumped.stderr.decode(errors="replace")
            assert dump.stat().st_size > 0
            assert dump.read_bytes()[:5] == b"PGDMP", "the artifact must be a REAL custom-format dump"

        for volume in VOLUMES:
            _write_volume_archive(src / f"{volume}.tgz", volume)

        tmpdir = work / "tmp"
        tmpdir.mkdir()
        env = {
            **os.environ,
            "TMPDIR": str(tmpdir).replace("\\", "/"),
        }
        completed = subprocess.run(
            [bash, "scripts/restore.sh", "--src", str(src).replace("\\", "/")],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=300,
            env=env,
        )
        output = completed.stdout + completed.stderr
        print(output)  # the drill's own log is the evidence (banked with `-s`)

        assert completed.returncode == 0, f"restore.sh exited {completed.returncode}:\n{output}"
        assert "format=custom" in output, output
        assert "note: custom-format dump — row-count manifest is not derivable" in output, output
        assert (
            f"command: docker exec -i {DRILL_NAME} pg_restore -U" in output
        ), "the PGDMP path must dispatch pg_restore, never psql"
        assert f"INFO tables restored = {EXPECTED_TABLES}" in output, output
        for volume in VOLUMES:
            assert f"PASS volume/{volume}:" in output, output
        assert "RESULT: PASS" in output, output

        had_exit = _docker("ps", "-a", "--format", "{{.Names}}")
        assert had_exit.returncode == 0
        assert DRILL_NAME not in had_exit.stdout.split(), (
            f"teardown must remove {DRILL_NAME}; 'docker ps -a' still lists it"
        )
        assert f"teardown: docker rm -f {DRILL_NAME}" in output, output
        print(f"docker server {docker_version}")
    finally:
        _force_remove_drill()
        shutil.rmtree(work, ignore_errors=True)
