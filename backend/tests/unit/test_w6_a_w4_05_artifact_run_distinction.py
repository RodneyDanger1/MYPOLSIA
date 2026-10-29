"""WAVE 6 / A-W4-05 — `/artifacts/{run_id}` must not answer "no workspace" for a run that never existed.

THE MEASURED DEFECT (live, HEAD `01bbbf7`, quoted in `forge/raw/w6/live-artifacts-a-w4-05.txt`)::

    GET /api/v1/artifacts/999999  -> 404
    {"ok":false,"error":{"code":"E_NOT_FOUND",
     "message":"artifacts: run 999999 has no workspace — nothing was written for it",
     "how_to_fix":"check the run id with GET /api/v1/runs; a run that wrote no file has no
                   workspace directory (the code tools create it on first use)",
     "context":{"run_id":"999999"}}}

    GET /api/v1/runs/999999       -> 404
    {"ok":false,"error":{"code":"E_NOT_FOUND","message":"run 999999 does not exist", ...}}

Two different facts shared ONE answer, and the shared answer asserted the one that was FALSE:
run 999999 does not exist, yet the artifact surface told the operator it exists and wrote nothing
("nothing was written for it" is a claim about a run's history the surface never measured).  A
run that really wrote nothing is a third, legitimate state — `runs/301/` exists and lists
`files: 0` — so the surface has to tell all three apart:

    * the run has a workspace            -> 200 (the listing, `total: 0` included)
    * the run exists, no workspace here  -> 404 `E_NOT_FOUND`, "has no workspace",   kind=no_workspace
    * no run with that id and no workspace -> 404 `E_NOT_FOUND`, "no such run",       kind=no_such_run

WHY THE LEDGER IS CONSULTED ONLY WHEN THE WORKSPACE IS ABSENT: the happy path never opens the
database, so an artifact read keeps working through a DB outage exactly as it did before; the
ledger read exists solely to stop the 404 from MISDESCRIBING the cause.  A ledger FAILURE is
therefore never flattened into "no such run" — it stays the named `E_DB` it really is.

Both refusals keep `E_NOT_FOUND` (404 is the correct status for "this resource is not here"; the
sibling `/api/v1/runs/{id}` 404s a missing run with the same code), and they are distinguished by
the message and by a machine-readable `context.kind`.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import settings
from app.core.errors import E_DB, PolsiaError
from app.models.task import AgentRun
from app.services import artifact_service
from app.services import run_service as runs_service

# ── helpers ──────────────────────────────────────────────────────────────────────────────────


def _workspace(tmp_path: Path, *, runs: tuple[str, ...] = ()) -> Path:
    """A workspace root with `runs/`, plus one directory per name in `runs`."""
    workspace = tmp_path / "workspace"
    (workspace / "runs").mkdir(parents=True)
    for name in runs:
        (workspace / "runs" / name).mkdir()
    return workspace


async def _seed_run(db, run_id: int) -> AgentRun:
    """A ledger row with no workspace — the "it exists and wrote nothing" state."""
    run = AgentRun(id=run_id, agent_type="code_generation", run_type="task", status="completed")
    db.add(run)
    await db.flush()
    return run


def _error(resp) -> dict:
    body = resp.json()
    assert body.get("ok") is False, f"expected the named refusal envelope, got {body!r}"
    return body["error"]


# ── the public predicate the route builds on (service level, no HTTP) ────────────────────────


def test_workspace_exists_is_a_pure_filesystem_answer(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path, runs=("301",))
    monkeypatch.setattr(settings, "code_workspace_dir", str(workspace))

    assert artifact_service.workspace_exists("301") is True
    assert artifact_service.workspace_exists("424242") is False


def test_workspace_exists_still_refuses_an_unsafe_id_by_name(tmp_path, monkeypatch):
    """The id rule is checked FIRST: a hostile id is never treated as "just absent"."""
    workspace = _workspace(tmp_path)
    monkeypatch.setattr(settings, "code_workspace_dir", str(workspace))

    for run_id in ("..", "a/b", "a.b", "C:", "", "x" * 65):
        with pytest.raises(PolsiaError) as caught:
            artifact_service.workspace_exists(run_id)
        assert caught.value.code == "E_VALIDATION", run_id


def test_workspace_exists_propagates_a_misconfigured_workspace(tmp_path, monkeypatch):
    """A misconfiguration is `E_CONFIG` — never a bare `False` that reads like "no run"."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(settings, "code_workspace_dir", str(blocker))

    with pytest.raises(PolsiaError) as caught:
        artifact_service.workspace_exists("301")
    assert caught.value.code == "E_CONFIG"


def test_the_no_workspace_refusal_names_its_kind(tmp_path, monkeypatch):
    """The existing service refusal keeps its message and gains the discriminator."""
    monkeypatch.setattr(settings, "code_workspace_dir", str(_workspace(tmp_path)))

    with pytest.raises(PolsiaError) as caught:
        artifact_service.run_dir("424242")
    refusal = caught.value
    assert refusal.code == "E_NOT_FOUND"
    assert "no workspace" in refusal.message
    assert refusal.context.get("kind") == "no_workspace"


# ── over HTTP: the three states, told apart ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_run_id_the_ledger_never_had_is_no_such_run(
    api_client, auth_headers, tmp_path, monkeypatch
):
    """A-W4-05: THE finding.  Nothing anywhere names this run, so say exactly that."""
    monkeypatch.setattr(settings, "code_workspace_dir", str(_workspace(tmp_path)))

    resp = await api_client.get("/api/v1/artifacts/424242", headers=auth_headers)

    assert resp.status_code == 404
    error = _error(resp)
    assert error["code"] == "E_NOT_FOUND"
    assert "no such run" in error["message"].lower(), error["message"]
    assert "nothing was written for it" not in error["message"].lower(), (
        "a run that never existed must not be described as a run that wrote nothing: "
        f"{error['message']!r}"
    )
    assert error["context"]["kind"] == "no_such_run"
    assert error["context"]["run_id"] == "424242"
    assert "/runs" in error["how_to_fix"], error["how_to_fix"]


@pytest.mark.asyncio
async def test_a_real_run_that_wrote_nothing_still_says_no_workspace(
    api_client, auth_headers, async_db_session, tmp_path, monkeypatch
):
    """The OTHER half: this run does exist, and the answer must stay "no workspace"."""
    monkeypatch.setattr(settings, "code_workspace_dir", str(_workspace(tmp_path)))
    await _seed_run(async_db_session, 424242)

    resp = await api_client.get("/api/v1/artifacts/424242", headers=auth_headers)

    assert resp.status_code == 404
    error = _error(resp)
    assert error["code"] == "E_NOT_FOUND"
    assert "no workspace" in error["message"].lower(), error["message"]
    assert "no such run" not in error["message"].lower(), error["message"]
    assert error["context"]["kind"] == "no_workspace"


@pytest.mark.asyncio
async def test_a_real_run_with_a_workspace_never_opens_the_ledger(
    api_client, auth_headers, tmp_path, monkeypatch
):
    """The happy path must not depend on the database (it did not before A-W4-05)."""
    workspace = _workspace(tmp_path, runs=("314",))
    (workspace / "runs" / "314" / "notes").mkdir()
    (workspace / "runs" / "314" / "notes" / "a.md").write_text("# x\n", encoding="utf-8")
    monkeypatch.setattr(settings, "code_workspace_dir", str(workspace))

    def _explode(*_args, **_kwargs):  # pragma: no cover - reached only on a regression
        raise AssertionError("the ledger was read although the workspace exists")

    monkeypatch.setattr(runs_service, "get_run", _explode)

    resp = await api_client.get("/api/v1/artifacts/314", headers=auth_headers)

    assert resp.status_code == 200, resp.text
    assert resp.json()["total"] == 1


@pytest.mark.asyncio
async def test_an_unsafe_run_id_is_refused_before_the_ledger(
    api_client, auth_headers, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "code_workspace_dir", str(_workspace(tmp_path)))

    def _explode(*_args, **_kwargs):  # pragma: no cover - reached only on a regression
        raise AssertionError("the ledger was read for an unsafe run id")

    monkeypatch.setattr(runs_service, "get_run", _explode)

    resp = await api_client.get("/api/v1/artifacts/run.314", headers=auth_headers)

    assert resp.status_code == 422
    assert _error(resp)["code"] == "E_VALIDATION"


@pytest.mark.asyncio
async def test_a_ledger_failure_is_never_reported_as_no_such_run(
    api_client, auth_headers, tmp_path, monkeypatch
):
    """Fail closed on the RIGHT code: an unavailable ledger is E_DB, not a 404."""
    monkeypatch.setattr(settings, "code_workspace_dir", str(_workspace(tmp_path)))

    async def _unavailable(*_args, **_kwargs):
        raise PolsiaError(E_DB, "run ledger is unavailable", how_to_fix="check DATABASE_URL")

    monkeypatch.setattr(runs_service, "get_run", _unavailable)

    resp = await api_client.get("/api/v1/artifacts/424242", headers=auth_headers)

    assert resp.status_code == 503, resp.text
    error = _error(resp)
    assert error["code"] == E_DB
    assert "no such run" not in error["message"].lower()
    assert error["how_to_fix"]


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["/content?path=notes/a.md", "/download?path=notes/a.md"])
async def test_the_sibling_byte_routes_answer_the_same_way(
    api_client, auth_headers, tmp_path, monkeypatch, suffix
):
    """One cause, one answer: the byte routes must not re-introduce the conflation."""
    monkeypatch.setattr(settings, "code_workspace_dir", str(_workspace(tmp_path)))

    resp = await api_client.get(f"/api/v1/artifacts/424242{suffix}", headers=auth_headers)

    assert resp.status_code == 404
    error = _error(resp)
    assert "no such run" in error["message"].lower(), error["message"]
    assert error["context"]["kind"] == "no_such_run"


@pytest.mark.asyncio
async def test_a_missing_file_in_a_real_workspace_keeps_its_own_refusal(
    api_client, auth_headers, tmp_path, monkeypatch
):
    """Control: the file-level 404 is untouched by the run-level distinction."""
    workspace = _workspace(tmp_path, runs=("314",))
    monkeypatch.setattr(settings, "code_workspace_dir", str(workspace))

    resp = await api_client.get(
        "/api/v1/artifacts/314/content", params={"path": "notes/a.md"}, headers=auth_headers
    )

    assert resp.status_code == 404
    error = _error(resp)
    assert "no file" in error["message"].lower(), error["message"]
    assert error["context"].get("kind") != "no_such_run"
