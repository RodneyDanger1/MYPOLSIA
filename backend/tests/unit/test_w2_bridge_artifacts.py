"""WAVE 2 / F-08 + F-15 — the artifact surface: files an agent wrote are LISTED and READABLE.

The measured defect (quoted from the evidence pack, `visuals/COMPANY-TRIAL.md` §4 and
`WAVE2-PLAN.md` F-15):

  * task #302's run 301 claimed *"I wrote `notes/302-logo-and-label-spec.md` in the local code
    workspace"* — the file does not exist anywhere (`/app/data/workspace/runs/301/` is EMPTY, and
    `agent_runs.tools_used` for run 301 is `["code_list","github_repo"]`, no `code_write`); and
  * the files that DO exist (run 314's `notes/315-brand-one-pager.md` +
    `notes/315-label-data-checklist.md`, 11 373 bytes) were unreachable: the live OpenAPI census
    served 88 `/api/v1` paths and none read a run workspace; `GET /api/v1/artifacts` answered
    `{"detail":"Not Found"}`.

This file pins BOTH halves: the read surface works on a real tree, and the honest view shows a
run that wrote nothing as `files: 0` (a measured zero) instead of hiding it.

Test-double boundary (§7): a temporary DIRECTORY tree is the real filesystem, not a double.  The
one platform primitive that needs a helper is link creation (Windows refuses `os.symlink` without
SeCreateSymbolicLinkPrivilege — measured `WinError 1314`; a junction via `mklink /J` needs no
privilege and IS a reparse point, which is why the containment proof resolves paths rather than
trusting `is_symlink()` — `Path.is_symlink()` is False for a junction on this host, measured).
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from app.agents import tool_registry
from app.config import settings
from app.core.errors import PolsiaError
from app.services import artifact_service as artifacts

# ── helpers ──────────────────────────────────────────────────────────────────────────────────


def _workspace(tmp_path: Path) -> Path:
    """A workspace root with a `runs/` dir — the exact layout `code_write` creates."""
    workspace = tmp_path / "workspace"
    (workspace / "runs").mkdir(parents=True)
    return workspace


def _write(root: Path, relative: str, text: str) -> Path:
    """Write one artifact exactly as `code_write` does (UTF-8, newline-preserving)."""
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    return target


def _link_dir(link: Path, target: Path) -> None:
    """Create a DIRECTORY link/junction; fail loudly if this host cannot provide one.

    POSIX: `os.symlink(..., target_is_directory=True)`.  Windows without the symlink privilege
    (`WinError 1314`, measured): `cmd /c mklink /J` — a junction, which needs no privilege.
    A host that can supply NEITHER fails the test by name: a skipped escape test would be a
    green check that proves nothing.
    """
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode == 0:
            return
        pytest.fail(
            "could not create a directory junction: "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )
    pytest.fail("could not create a directory link; the escape proof cannot run on this host")


def _refusal(call) -> PolsiaError:
    """Run `call`, assert it refuses with a `PolsiaError`, and return it."""
    with pytest.raises(PolsiaError) as caught:
        call()
    return caught.value


def _sample_tree(tmp_path: Path) -> Path:
    """The trial-shaped tree: one run with two nested notes, one run that wrote NOTHING."""
    workspace = _workspace(tmp_path)
    runs = workspace / "runs"
    (runs / "301").mkdir()  # task #302's run: the claimed file was never written
    _write(runs, "314/notes/315-brand-one-pager.md", "# Hearth & Lather\n\nprovisional.\n")
    _write(runs, "314/notes/315-label-data-checklist.md", "- legal name\n- net weight\n")
    _write(runs, "314/notes/deeper/nested.txt", "nested\n")
    return workspace


# ── contract pins (the documented caps + the writer's own layout) ────────────────────────────


def test_the_layout_constants_match_the_code_tools_writer():
    """This reader must agree with the writer's layout, or it reads the wrong tree."""
    assert artifacts.RUN_WORKSPACE_SUBDIR == tool_registry.RUN_WORKSPACE_SUBDIR == "runs"
    assert artifacts.CODE_WORKSPACE_DEFAULT == tool_registry.CODE_WORKSPACE_DEFAULT
    assert artifacts.TEXT_ENCODING == tool_registry.CODE_TEXT_ENCODING


def test_the_caps_are_the_documented_ones():
    """The caps are a public contract (`app/api/v1/artifacts.py` docstring) — pinned here."""
    assert artifacts.MAX_INLINE_BYTES == 512 * 1024
    assert artifacts.MAX_DOWNLOAD_BYTES == 8 * 1024 * 1024
    assert artifacts.MAX_LIST_ENTRIES == 500
    assert artifacts.MAX_SCAN_FILES == 5000
    assert artifacts.MAX_RUNS == 200


# ── the index: runs, including the ones that wrote nothing ───────────────────────────────────


def test_the_index_lists_every_run_workspace_including_an_empty_one(tmp_path):
    """`files: 0` is a MEASURED zero — this is how a missing deliverable becomes visible."""
    index = artifacts.list_runs(_sample_tree(tmp_path))

    assert [run.run_id for run in index.runs] == ["314", "301"]
    by_id = {run.run_id: run for run in index.runs}
    assert by_id["314"].files == 3
    assert by_id["314"].total_bytes == len(
        "# Hearth & Lather\n\nprovisional.\n"
    ) + len("- legal name\n- net weight\n") + len("nested\n")
    assert by_id["314"].latest_modified_at is not None
    assert by_id["314"].truncated is False
    assert by_id["301"].files == 0
    assert by_id["301"].total_bytes == 0
    assert by_id["301"].latest_modified_at is None
    assert index.truncated is False
    assert index.skipped == []


def test_a_workspace_with_no_runs_dir_is_a_measured_empty_index(tmp_path):
    """Nothing written yet is `total: 0` with no refusal — and the dir is NOT created by a read."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    index = artifacts.list_runs(workspace)

    assert index.runs == []
    assert index.truncated is False
    assert not (workspace / "runs").exists(), "a read must never create the writer's directory"


def test_a_workspace_that_is_a_file_is_the_named_config_refusal(tmp_path):
    """A misconfigured root must never look like an empty store."""
    blocker = tmp_path / "workspace"
    blocker.write_text("not a directory", encoding="utf-8")

    refusal = _refusal(lambda: artifacts.list_runs(blocker))

    assert refusal.code == "E_CONFIG"
    assert "not a directory" in refusal.message
    assert refusal.context["workspace"] == str(blocker.resolve())


def test_the_runs_dir_being_a_file_is_the_named_config_refusal(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "runs").write_text("in the way", encoding="utf-8")

    refusal = _refusal(lambda: artifacts.list_runs(workspace))

    assert refusal.code == "E_CONFIG"
    assert refusal.context["runs_dir"]


def test_a_directory_that_is_not_a_run_id_is_skipped_by_name_not_entered(tmp_path):
    """A planted directory cannot smuggle a listing: it is reported, never walked."""
    workspace = _sample_tree(tmp_path)
    (workspace / "runs" / "not a run id").mkdir()
    (workspace / "runs" / "not a run id" / "x.txt").write_text("x", encoding="utf-8")

    index = artifacts.list_runs(workspace)

    assert {run.run_id for run in index.runs} == {"314", "301"}
    assert index.skipped
    assert index.skipped[0].name == "not a run id"
    assert index.skipped[0].reason == "not a safe run id"


def test_the_index_refuses_a_run_directory_that_resolves_outside_runs(tmp_path):
    """A junction at `runs/<id>` is a real escape primitive; the index names it and skips it."""
    workspace = _sample_tree(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("SECRET", encoding="utf-8")
    _link_dir(workspace / "runs" / "999", outside)

    index = artifacts.list_runs(workspace)

    assert {run.run_id for run in index.runs} == {"314", "301"}
    reasons = {entry.name: entry.reason for entry in index.skipped}
    assert reasons.get("999") in ("resolves outside the runs dir", "symbolic link")


def test_the_index_is_bounded_and_says_so(tmp_path):
    workspace = _workspace(tmp_path)
    for run_id in ("1", "2", "3"):
        _write(workspace / "runs", f"{run_id}/a.txt", "x")

    index = artifacts.list_runs(workspace, limit=2)

    assert len(index.runs) == 2
    assert index.truncated is True


# ── one run's listing ────────────────────────────────────────────────────────────────────────


def test_one_runs_files_are_listed_with_posix_paths_and_exact_bytes(tmp_path):
    listing = artifacts.list_run_artifacts("314", _sample_tree(tmp_path))

    assert listing.run_id == "314"
    assert [file.path for file in listing.files] == [
        "notes/315-brand-one-pager.md",
        "notes/315-label-data-checklist.md",
        "notes/deeper/nested.txt",
    ]
    sizes = {file.path: file.size for file in listing.files}
    assert sizes["notes/315-brand-one-pager.md"] == len("# Hearth & Lather\n\nprovisional.\n")
    assert sizes["notes/315-label-data-checklist.md"] == len("- legal name\n- net weight\n")
    assert listing.total_bytes == sum(sizes.values())
    assert listing.truncated is False
    assert listing.skipped_links == 0
    for file in listing.files:
        assert file.modified_at.tzinfo is not None, "wire datetimes are aware UTC"


def test_a_run_that_wrote_nothing_lists_zero_files_rather_than_an_error(tmp_path):
    listing = artifacts.list_run_artifacts("301", _sample_tree(tmp_path))

    assert listing.run_id == "301"
    assert listing.files == []
    assert listing.total_bytes == 0
    assert listing.truncated is False


def test_a_run_with_no_workspace_is_the_named_not_found(tmp_path):
    refusal = _refusal(lambda: artifacts.list_run_artifacts("424242", _sample_tree(tmp_path)))

    assert refusal.code == "E_NOT_FOUND"
    assert refusal.context["run_id"] == "424242"
    assert "no workspace" in refusal.message


@pytest.mark.parametrize(
    "run_id",
    ["..", "../etc", "a/b", "a\\b", ".", "  ", "a b", "run.314", "-leading", "x" * 65, "%2e%2e"],
)
def test_an_unsafe_run_id_is_refused_before_any_filesystem_call(tmp_path, run_id):
    root = _sample_tree(tmp_path)
    refusal = _refusal(lambda: artifacts.list_run_artifacts(run_id, root))
    assert refusal.code == "E_VALIDATION"
    assert refusal.context["field"] == "run_id"


# ── path safety ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "C:\\Windows\\win.ini",
        "C:win.ini",
        "\\\\server\\share\\x.txt",
        "../../etc/passwd",
        "notes/../../outside.txt",
        "..",
        "a\x00b",
        "",
        "   ",
        None,
        5,
    ],
)
def test_an_unsafe_artifact_path_is_refused_by_name(tmp_path, path):
    root = _sample_tree(tmp_path)
    refusal = _refusal(lambda: artifacts.read_artifact_text("314", path, root))

    assert refusal.code == "E_VALIDATION"
    assert refusal.context.get("field") == "path" or "path" in refusal.context


def test_a_directory_is_not_an_artifact(tmp_path):
    root = _sample_tree(tmp_path)
    refusal = _refusal(lambda: artifacts.read_artifact_text("314", "notes", root))

    assert refusal.code == "E_VALIDATION"
    assert "not a regular file" in refusal.message


def test_a_missing_file_is_the_named_not_found(tmp_path):
    root = _sample_tree(tmp_path)
    refusal = _refusal(lambda: artifacts.read_artifact_text("314", "notes/nope.md", root))

    assert refusal.code == "E_NOT_FOUND"
    assert refusal.context["path"] == "notes/nope.md"


def test_a_shared_prefix_outside_file_is_not_reachable(tmp_path):
    """`runs/3142` must never be reachable from run 314 — containment is by path, not prefix."""
    workspace = _workspace(tmp_path)
    _write(workspace / "runs", "3142/other.md", "OTHER RUN\n")
    _write(workspace / "runs", "314/mine.md", "MINE\n")

    listing = artifacts.list_run_artifacts("314", workspace)
    assert [file.path for file in listing.files] == ["mine.md"]
    assert b"OTHER" not in artifacts.read_artifact_bytes(
        "314", "mine.md", workspace
    ).data


def test_a_path_through_a_linked_directory_that_leaves_the_run_is_refused(tmp_path):
    """The escape primitive this host CAN build without privilege, and the reason for `resolve()`."""
    workspace = _workspace(tmp_path)
    _write(workspace / "runs", "314/mine.md", "MINE\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("SECRET\n", encoding="utf-8")
    _link_dir(workspace / "runs" / "314" / "linked", outside)

    refusal = _refusal(lambda: artifacts.read_artifact_text("314", "linked/secret.md", workspace))

    assert refusal.code == "E_VALIDATION"
    assert "escapes the run workspace" in refusal.message
    listing = artifacts.list_run_artifacts("314", workspace)
    assert listing.skipped_links == 1
    assert [file.path for file in listing.files] == ["mine.md"]


def test_a_run_workspace_that_is_a_junction_out_is_refused(tmp_path):
    workspace = _workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("SECRET\n", encoding="utf-8")
    _link_dir(workspace / "runs" / "777", outside)

    refusal = _refusal(lambda: artifacts.list_run_artifacts("777", workspace))

    assert refusal.code == "E_VALIDATION"
    assert "resolves outside the runs directory" in refusal.message


def test_the_runs_dir_being_a_link_out_of_the_workspace_is_the_named_config_refusal(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "elsewhere"
    (outside / "runs").mkdir(parents=True)
    _link_dir(workspace / "runs", outside / "runs")

    refusal = _refusal(lambda: artifacts.list_runs(workspace))

    assert refusal.code == "E_CONFIG"
    assert "outside the code workspace" in refusal.message


def test_a_file_link_is_never_served(tmp_path):
    """A link is never served — proven with whichever primitive this host can create.

    POSIX: a real FILE symlink exercises the `is_symlink` refusal.  Windows without the symlink
    privilege (measured WinError 1314): a file symlink cannot be created, so the same CLAIM is
    exercised through a junction and the file reached through it — which the containment proof
    refuses identically.
    """
    workspace = _workspace(tmp_path)
    _write(workspace / "runs", "314/mine.md", "MINE\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("SECRET\n", encoding="utf-8")

    direct = workspace / "runs" / "314" / "linked-file.md"
    try:
        os.symlink(outside / "secret.md", direct)
    except (OSError, NotImplementedError):
        _link_dir(workspace / "runs" / "314" / "linked-dir", outside)
        target = "linked-dir/secret.md"
    else:
        target = "linked-file.md"

    refusal = _refusal(lambda: artifacts.read_artifact_text("314", target, workspace))
    assert refusal.code == "E_VALIDATION"
    assert "link" in refusal.message or "escapes" in refusal.message


# ── reading text ────────────────────────────────────────────────────────────────────────────


def test_the_text_is_exactly_what_the_agent_wrote(tmp_path):
    workspace = _workspace(tmp_path)
    body = "# Notes\r\n\r\nÜnïcode — and a trailing newline\r\n"
    _write(workspace / "runs", "314/notes/a.md", body)

    artifact = artifacts.read_artifact_text("314", "notes/a.md", workspace)

    assert artifact.text == body
    assert artifact.size == len(body.encode("utf-8"))
    assert artifact.sha256 == hashlib.sha256(body.encode("utf-8")).hexdigest()
    assert artifact.path == "notes/a.md"
    assert artifact.run_id == "314"


def test_a_backslash_spelled_path_resolves_to_the_same_artifact(tmp_path):
    workspace = _workspace(tmp_path)
    _write(workspace / "runs", "314/notes/a.md", "ok\n")

    artifact = artifacts.read_artifact_text("314", "notes\\a.md", workspace)

    assert artifact.path == "notes/a.md"
    assert artifact.text == "ok\n"


def test_an_oversized_preview_refuses_and_names_the_download(tmp_path):
    workspace = _workspace(tmp_path)
    big = "x" * (artifacts.MAX_INLINE_BYTES + 1)
    _write(workspace / "runs", "314/big.md", big)

    refusal = _refusal(lambda: artifacts.read_artifact_text("314", "big.md", workspace))

    assert refusal.code == "E_VALIDATION"
    assert refusal.context["bytes"] == artifacts.MAX_INLINE_BYTES + 1
    assert refusal.context["cap_bytes"] == artifacts.MAX_INLINE_BYTES
    assert "/download" in refusal.how_to_fix
    # The same file IS downloadable through the other route (no silent unavailability).
    assert artifacts.read_artifact_bytes("314", "big.md", workspace).size == len(big)


def test_a_binary_artifact_refuses_preview_and_downloads(tmp_path):
    workspace = _workspace(tmp_path)
    target = workspace / "runs" / "314" / "image.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"PNG\x00\x01\x02")

    refusal = _refusal(lambda: artifacts.read_artifact_text("314", "image.bin", workspace))
    assert refusal.code == "E_VALIDATION"
    assert "NUL byte" in refusal.message

    download = artifacts.read_artifact_bytes("314", "image.bin", workspace)
    assert download.data == b"PNG\x00\x01\x02"
    assert download.filename == "image.bin"


def test_undecodable_text_refuses_preview_and_names_the_byte(tmp_path):
    workspace = _workspace(tmp_path)
    target = workspace / "runs" / "314" / "latin1.md"
    target.parent.mkdir(parents=True)
    target.write_bytes("caf\xe9\n".encode("latin-1"))

    refusal = _refusal(lambda: artifacts.read_artifact_text("314", "latin1.md", workspace))

    assert refusal.code == "E_VALIDATION"
    assert refusal.context["byte_offset"] >= 0
    assert "not valid utf-8 text" in refusal.message


def test_a_download_over_the_hard_cap_refuses_by_name(tmp_path):
    workspace = _workspace(tmp_path)
    _write(workspace / "runs", "314/huge.bin", "x" * 64)

    refusal = _refusal(
        lambda: artifacts.read_artifact_bytes("314", "huge.bin", workspace, max_bytes=10)
    )

    assert refusal.code == "E_VALIDATION"
    assert refusal.context["bytes"] == 64
    assert refusal.context["cap_bytes"] == 10


def test_the_download_digest_matches_the_bytes_on_disk(tmp_path):
    workspace = _workspace(tmp_path)
    _write(workspace / "runs", "314/notes/a.md", "abc\n")

    artifact = artifacts.read_artifact_bytes("314", "notes/a.md", workspace)

    assert artifact.data == b"abc\n"
    assert artifact.sha256 == hashlib.sha256(b"abc\n").hexdigest()
    assert artifact.size == 4
    assert artifact.modified_at.tzinfo is not None


# ── HTTP surface (owner auth, envelopes, headers) ───────────────────────────────────────────


def _http_workspace(tmp_path: Path, monkeypatch) -> Path:
    workspace = _sample_tree(tmp_path)
    monkeypatch.setattr(settings, "code_workspace_dir", str(workspace))
    return workspace


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "/api/v1/artifacts",
        "/api/v1/artifacts/314",
        "/api/v1/artifacts/314/content?path=notes/315-brand-one-pager.md",
        "/api/v1/artifacts/314/download?path=notes/315-brand-one-pager.md",
    ],
)
async def test_every_artifact_route_requires_the_owner_key(api_client, url):
    resp = await api_client.get(url)

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "E_AUTH"


@pytest.mark.asyncio
async def test_the_index_over_http_serves_runs_and_the_empty_one(
    api_client, auth_headers, tmp_path, monkeypatch
):
    _http_workspace(tmp_path, monkeypatch)

    resp = await api_client.get("/api/v1/artifacts", headers=auth_headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 2
    by_id = {item["run_id"]: item for item in body["items"]}
    assert by_id["314"]["files"] == 3
    assert by_id["314"]["bytes"] > 0
    assert by_id["314"]["latest_modified_at"].endswith("Z")
    assert by_id["301"]["files"] == 0
    assert by_id["301"]["bytes"] == 0
    assert by_id["301"]["latest_modified_at"] is None
    assert body["truncated"] is False
    assert body["skipped"] == []


@pytest.mark.asyncio
async def test_one_runs_listing_over_http(api_client, auth_headers, tmp_path, monkeypatch):
    _http_workspace(tmp_path, monkeypatch)

    resp = await api_client.get("/api/v1/artifacts/314", headers=auth_headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["run_id"] == "314"
    assert body["total"] == 3
    assert body["total_bytes"] == sum(item["bytes"] for item in body["items"])
    assert {item["path"] for item in body["items"]} == {
        "notes/315-brand-one-pager.md",
        "notes/315-label-data-checklist.md",
        "notes/deeper/nested.txt",
    }
    assert body["truncated"] is False
    assert body["skipped_links"] == 0


@pytest.mark.asyncio
async def test_the_content_route_serves_the_text_and_its_identity(
    api_client, auth_headers, tmp_path, monkeypatch
):
    _http_workspace(tmp_path, monkeypatch)

    resp = await api_client.get(
        "/api/v1/artifacts/314/content",
        params={"path": "notes/315-brand-one-pager.md"},
        headers=auth_headers,
    )

    assert resp.status_code == 200, resp.text
    assert resp.text == "# Hearth & Lather\n\nprovisional.\n"
    assert resp.headers["content-type"].startswith("text/plain")
    expected = hashlib.sha256(b"# Hearth & Lather\n\nprovisional.\n").hexdigest()
    assert resp.headers["x-meridian-artifact-sha256"] == expected
    assert resp.headers["x-meridian-artifact-bytes"] == "32"
    assert resp.headers["x-meridian-artifact-modified"].endswith("Z")
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert "inline" in resp.headers["content-disposition"]


@pytest.mark.asyncio
async def test_the_download_route_is_an_attachment(api_client, auth_headers, tmp_path, monkeypatch):
    _http_workspace(tmp_path, monkeypatch)

    resp = await api_client.get(
        "/api/v1/artifacts/314/download",
        params={"path": "notes/315-label-data-checklist.md"},
        headers=auth_headers,
    )

    assert resp.status_code == 200, resp.text
    assert resp.content == b"- legal name\n- net weight\n"
    assert resp.headers["content-type"] == "application/octet-stream"
    assert 'attachment; filename="315-label-data-checklist.md"' in (
        resp.headers["content-disposition"]
    )


@pytest.mark.asyncio
async def test_http_refusals_keep_the_named_envelope(
    api_client, auth_headers, tmp_path, monkeypatch
):
    _http_workspace(tmp_path, monkeypatch)

    missing_run = await api_client.get("/api/v1/artifacts/424242", headers=auth_headers)
    unsafe_path = await api_client.get(
        "/api/v1/artifacts/314/content",
        params={"path": "../../etc/passwd"},
        headers=auth_headers,
    )
    # `..`/`%2F` in a URL PATH SEGMENT never reaches a handler (Starlette normalises the route
    # away and answers its own 404), so the routing-reachable hostile id is a dotted/slashed name
    # that DOES match `/{run_id}` — the service must refuse it by name, not by luck.
    unsafe_run = await api_client.get("/api/v1/artifacts/run.314", headers=auth_headers)

    assert missing_run.status_code == 404
    assert missing_run.json()["error"]["code"] == "E_NOT_FOUND"
    assert missing_run.json()["error"]["how_to_fix"]

    assert unsafe_path.status_code == 422
    assert unsafe_path.json()["error"]["code"] == "E_VALIDATION"

    assert unsafe_run.status_code == 422
    assert unsafe_run.json()["error"]["code"] == "E_VALIDATION"


@pytest.mark.asyncio
async def test_the_content_route_requires_a_path_parameter(
    api_client, auth_headers, tmp_path, monkeypatch
):
    _http_workspace(tmp_path, monkeypatch)

    resp = await api_client.get("/api/v1/artifacts/314/content", headers=auth_headers)

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_an_oversized_preview_is_422_and_names_the_cap(
    api_client, auth_headers, tmp_path, monkeypatch
):
    workspace = _http_workspace(tmp_path, monkeypatch)
    _write(workspace / "runs", "314/big.md", "x" * (artifacts.MAX_INLINE_BYTES + 1))

    resp = await api_client.get(
        "/api/v1/artifacts/314/content", params={"path": "big.md"}, headers=auth_headers
    )

    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "E_VALIDATION"
    assert error["context"]["cap_bytes"] == artifacts.MAX_INLINE_BYTES


@pytest.mark.asyncio
async def test_the_documented_route_set_is_exactly_what_is_mounted():
    """No undocumented write/delete surface can appear on `/artifacts` without failing here."""
    from app.main import app

    mounted = {
        (sorted(route.methods)[0], route.path)
        for route in app.routes
        if getattr(route, "path", "").startswith("/api/v1/artifacts")
    }

    assert mounted == {
        ("GET", "/api/v1/artifacts"),
        ("GET", "/api/v1/artifacts/{run_id}"),
        ("GET", "/api/v1/artifacts/{run_id}/content"),
        ("GET", "/api/v1/artifacts/{run_id}/download"),
    }
