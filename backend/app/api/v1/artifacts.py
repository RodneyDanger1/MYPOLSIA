"""`/api/v1/artifacts` — the READ surface over run workspaces (wave 2 / F-08 + F-15).

THE OPERATOR DEAD END THIS CLOSES (measured, quoted in `artifact_service`): the candle trial's
code agent wrote `notes/302-logo-and-label-spec.md` into the run workspace, and the owner had no
way to open it — `/ops` shows the runs DIRECTORY PATH as text, no route served a workspace file,
and the OpenAPI census (88 paths, live) contained none.  Worse, the claimed file in that case was
never written at all (`runs/301/` is empty; the ledger records no `code_write`), and the product
could not show that either.

FROZEN CONTRACT (owner-key like every sibling v1 router):

    GET /api/v1/artifacts
        200 {"items": [{"run_id","files","bytes","latest_modified_at","truncated"}, ...],
             "total": N, "truncated": bool, "skipped": [{"name","reason"}, ...]}
        Every run DIRECTORY under `<code_workspace_dir>/runs/`, newest activity first, INCLUDING
        runs with `files: 0` — that measured zero is how a claimed-but-missing deliverable becomes
        visible.  A directory that is not a safe run id, a symbolic link, or one that resolves
        outside `runs/` is reported in `skipped` with the reason and never entered.
        The runs directory absent (`nothing written yet`) → `{"items": [], "total": 0}`.

    GET /api/v1/artifacts/{run_id}
        200 {"run_id", "items": [{"path","bytes","modified_at"}, ...], "total", "total_bytes",
             "truncated", "skipped_links"}
        Files only (directories are not artifacts); nested paths are posix-relative to the run
        dir.  An existing-but-empty run dir is `total: 0` (a MEASURED empty, not an error).
        An absent run workspace is the named 404 `E_NOT_FOUND` naming the run.

    GET /api/v1/artifacts/{run_id}/content?path=<relative>
        200 text/plain; charset=utf-8 — the artifact's text, exactly as written (no rendering).
        Headers: `X-Polsia-Artifact-Sha256`, `X-Polsia-Artifact-Bytes`,
        `X-Polsia-Artifact-Modified`, `X-Content-Type-Options: nosniff`.

    GET /api/v1/artifacts/{run_id}/download?path=<relative>
        200 application/octet-stream — the artifact's exact bytes as an attachment
        (`Content-Disposition: attachment; filename="..."`), same three artifact headers.

    REFUSALS (all named, `{"ok": false, "error": {code, message, how_to_fix, context}}`):
      * `E_VALIDATION` (422) — an unsafe run id (`..`, separators, dots, spaces, >64 chars); an
        absolute POSIX/drive-letter/UNC path; a `..` component; a control character; a path that
        resolves outside the run dir (a link/junction escape); a symbolic link; a directory; a
        file over the inline cap (512 KiB) or the download cap (8 MiB); a binary file requested
        through `content` (NUL byte or undecodable UTF-8);
      * `E_NOT_FOUND` (404) — a run with no workspace directory; a file that does not exist;
      * `E_STORAGE` (503) — the volume answered an OS error (named, with the reason);
      * `E_CONFIG` (503) — `code_workspace_dir` is not a directory, or `runs/` is a link.
      * no `X-API-Key` → the standard 401 `E_AUTH`.

WHAT THIS ROUTER DELIBERATELY DOES NOT DO: no write/upload/delete (the writer is `code_write`,
the deleter is `POST /api/v1/ops/run-workspace-cleanup`), no `agent_runs`/`tasks` join (the UI
joins with `GET /api/v1/runs?task_id=`), no rendering or conversion, no binary preview, and no
path outside the run workspace is ever resolved, let alone served.

The route bodies stay thin: every rule — caps, path safety, refusal codes — lives in
`app/services/artifact_service.py` so the service can be exercised without HTTP.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel

from app.api.deps import refusal_guard, verify_api_key
from app.core.timeutil import iso_utc
from app.schemas.common import UtcDatetime
from app.services import artifact_service

router = APIRouter(prefix="/artifacts", tags=["artifacts"], dependencies=[Depends(verify_api_key)])

#: A `Content-Disposition` filename may only carry these characters: anything else (quotes,
#: separators, CR/LF, non-ASCII) is replaced, so a hostile file name cannot inject a header.
_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_FILENAME_CHARS = 100


class ArtifactFileModel(BaseModel):
    """One artifact file: the path relative to its run dir, its exact byte size, its mtime."""

    path: str
    bytes: int
    modified_at: UtcDatetime


class SkippedDirectoryModel(BaseModel):
    """One directory under `runs/` the index refused to enter, and why."""

    name: str
    reason: str


class RunSummaryModel(BaseModel):
    """One run workspace.  `files: 0` is a measured empty — the run wrote nothing."""

    run_id: str
    files: int
    bytes: int
    latest_modified_at: UtcDatetime | None
    truncated: bool


class ArtifactIndexModel(BaseModel):
    """`GET /artifacts` — `total` is the number of run workspaces served, not the page size."""

    items: list[RunSummaryModel]
    total: int
    truncated: bool
    skipped: list[SkippedDirectoryModel]


class RunArtifactsModel(BaseModel):
    """`GET /artifacts/{run_id}` — the files one run wrote (directories are not artifacts)."""

    run_id: str
    items: list[ArtifactFileModel]
    total: int
    total_bytes: int
    truncated: bool
    skipped_links: int


def _safe_filename(name: str) -> str:
    """A header-safe download filename derived from the artifact path (never a path)."""
    cleaned = _SAFE_FILENAME.sub("_", name).strip("._-")
    if not cleaned:
        return "artifact.txt"
    return cleaned[:_MAX_FILENAME_CHARS]


def _artifact_headers(*, sha256: str, size: int, modified_at) -> dict[str, str]:
    """The provenance headers both byte routes carry (the artifact's identity, not a claim)."""
    return {
        "X-Polsia-Artifact-Sha256": sha256,
        "X-Polsia-Artifact-Bytes": str(size),
        "X-Polsia-Artifact-Modified": iso_utc(modified_at) or "",
        "X-Content-Type-Options": "nosniff",
    }


@router.get("", response_model=ArtifactIndexModel)
async def list_artifacts_route() -> ArtifactIndexModel:
    """Every run workspace on this volume — including the ones that wrote NOTHING."""
    with refusal_guard():
        index = artifact_service.list_runs()
    return ArtifactIndexModel(
        items=[
            RunSummaryModel(
                run_id=run.run_id,
                files=run.files,
                bytes=run.total_bytes,
                latest_modified_at=run.latest_modified_at,
                truncated=run.truncated,
            )
            for run in index.runs
        ],
        total=len(index.runs),
        truncated=index.truncated,
        skipped=[
            SkippedDirectoryModel(name=entry.name, reason=entry.reason)
            for entry in index.skipped
        ],
    )


@router.get("/{run_id}", response_model=RunArtifactsModel)
async def list_run_artifacts_route(run_id: str) -> RunArtifactsModel:
    """The files one run wrote; an empty run workspace is a measured `total: 0`."""
    with refusal_guard():
        listing = artifact_service.list_run_artifacts(run_id)
    return RunArtifactsModel(
        run_id=listing.run_id,
        items=[
            ArtifactFileModel(
                path=file.path, bytes=file.size, modified_at=file.modified_at
            )
            for file in listing.files
        ],
        total=len(listing.files),
        total_bytes=listing.total_bytes,
        truncated=listing.truncated,
        skipped_links=listing.skipped_links,
    )


@router.get("/{run_id}/content")
async def read_artifact_content_route(
    run_id: str, path: str = Query(..., description="the artifact's run-relative path")
) -> Response:
    """One artifact as UTF-8 text — the deliverable the operator could not open before."""
    with refusal_guard():
        artifact = artifact_service.read_artifact_text(run_id, path)
    headers = _artifact_headers(
        sha256=artifact.sha256, size=artifact.size, modified_at=artifact.modified_at
    )
    headers["Content-Disposition"] = f'inline; filename="{_safe_filename(artifact.path)}"'
    return Response(
        content=artifact.text.encode(artifact_service.TEXT_ENCODING),
        media_type=f"text/plain; charset={artifact_service.TEXT_ENCODING}",
        headers=headers,
    )


@router.get("/{run_id}/download")
async def download_artifact_route(
    run_id: str, path: str = Query(..., description="the artifact's run-relative path")
) -> Response:
    """One artifact's exact bytes as an attachment (same identity headers as `content`)."""
    with refusal_guard():
        artifact = artifact_service.read_artifact_bytes(run_id, path)
    headers = _artifact_headers(
        sha256=artifact.sha256, size=artifact.size, modified_at=artifact.modified_at
    )
    headers["Content-Disposition"] = f'attachment; filename="{_safe_filename(artifact.filename)}"'
    return Response(
        content=artifact.data,
        media_type="application/octet-stream",
        headers=headers,
    )
