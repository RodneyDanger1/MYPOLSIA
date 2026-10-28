"""Run-workspace ARTIFACT store (wave 2 / F-08 + F-15): the files an agent wrote become readable.

WHY this file exists (the operator dead end, measured twice):

  * VISUALS-1 F-08 — the trial asked the code agent for "the logo SVG code and the candle label
    text"; run 301 answered *"I wrote `notes/302-logo-and-label-spec.md` in the local code
    workspace"*.  Nothing in the product could open it: `/ops` renders `RUNS DIR
    /app/data/workspace/runs` and `KEPT 10` as TEXT ONLY, and the OpenAPI census served 88 paths
    with no workspace/artifact read among them (`GET /api/v1/artifacts` → `{"detail":"Not Found"}`).
  * VISUALS-2 F-15 — task #302's own claim is contradicted by its ledger:
    `agent_runs.tools_used = ["code_list","github_repo"]` (no `code_write`), and
    `/app/data/workspace/runs/301/` is EMPTY.  The named deliverable never existed anywhere.

So the product owed two things: a READ surface over the files that DO exist, and an honest view
that makes "this run wrote nothing" visible instead of invisible.  Both live here.

WHAT THIS MODULE READS

`<settings.code_workspace_dir>/runs/<run_id>/**` — the exact tree `tool_registry`'s `code_write`
already writes into for a run (this module repeats the constant value `"runs"` rather than
importing a private one; the code tools' run id shape is `^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$` and
this module accepts the same set).  The backend and the worker share the `data` volume, so the API
process sees what the agent wrote in the worker process — verified against the live stack.

WHAT IT DELIBERATELY DOES NOT DO

  * no write/delete/upload/move — `code_write` is the only writer, and
    `POST /api/v1/ops/run-workspace-cleanup` is the only deleter; a second writer would be a
    second authority over the same directory;
  * no serving outside `<workspace>/runs/<run_id>` — absolute paths, drive letters, UNC shares,
    `..`, control characters and symlink/junction escapes are refused BY NAME;
  * no symlink is ever followed or served: a link is counted and named, never read;
  * no binary preview — `content` is UTF-8 text only (a NUL byte or an undecodable body refuses
    by name and points at the download route, which serves bytes);
  * no join to the `agent_runs`/`tasks` tables: the run id is the key, and the UI joins with
    `GET /api/v1/runs?task_id=` (one truth per module).  The DIRECTORY is the artifact truth; the
    ledger is the run truth;
  * no rendering/conversion: a markdown artifact is served as the markdown the agent wrote.

HONESTY RULES (the repo-wide ones, applied to a filesystem read)

  * `code_workspace_dir` exists but is NOT a directory → named `E_CONFIG`; a misconfiguration
    must never look like "no artifacts yet";
  * `<workspace>/runs` absent → a MEASURED empty index (`total: 0`, with a note saying no run has
    written a workspace yet) — the code tools create that directory on first write;
  * `runs/<id>` absent → `E_NOT_FOUND` naming the run (never an empty list that reads like "this
    run wrote nothing");
  * `runs/<id>` present but empty → `total: 0` files — the measured fact, which is exactly the
    fact that exposes a claimed-but-missing deliverable;
  * an over-cap file refuses naming its real byte count and the cap, never a truncated body;
  * every read error is a named `E_STORAGE` refusal with the reason, never a silent empty read.

CAPS (constants, pinned by tests)

  * inline preview: 512 KiB; download: 8 MiB; one run listing: 500 files returned; one run scan:
    5000 files examined; the index: 200 runs.  A listing past a cap says `truncated: true` and
    names the cap — it never quietly drops entries, and it never presents a partial sum as a total.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from app.core.errors import E_CONFIG, E_NOT_FOUND, E_STORAGE, E_VALIDATION, PolsiaError

#: `settings.code_workspace_dir`'s default (the same value `tool_registry` uses).
CODE_WORKSPACE_DEFAULT = "data/workspace"
#: The per-run subdirectory `code_write` writes into (the same value as `tool_registry`).
RUN_WORKSPACE_SUBDIR = "runs"
#: The run id shape the code tools accept — the only directory names this module will enter.
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
#: Windows drive prefix (`C:` / `C:relative`) — an absolute path wearing a drive letter.
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")

#: Inline text preview cap.  Over it the caller is told the real size and pointed at `download`.
MAX_INLINE_BYTES = 512 * 1024
#: Download cap.  A hard bound: this surface serves text deliverables, not binary media.
MAX_DOWNLOAD_BYTES = 8 * 1024 * 1024
#: Files RETURNED by one run listing (the runs are retention-capped at ~20, so this is a belt).
MAX_LIST_ENTRIES = 500
#: Files EXAMINED in one run directory (the bound the index and the listing share).
MAX_SCAN_FILES = 5000
#: Run directories returned by the index.
MAX_RUNS = 200
#: How many skipped/refused directory names the index reports in `skipped` detail.
MAX_SKIP_DETAILS = 20
#: The workspace is text-only by construction (`code_write` refuses NUL bytes and non-UTF-8).
TEXT_ENCODING = "utf-8"


# ── result shapes (plain frozen dataclasses; the router declares the wire models) ──────────────


@dataclass(frozen=True)
class ArtifactFile:
    """One file inside a run workspace — path relative to the run dir, posix-style."""

    path: str
    size: int
    modified_at: datetime


@dataclass(frozen=True)
class SkippedEntry:
    """One directory under `runs/` this module refused to enter, and why (named)."""

    name: str
    reason: str


@dataclass(frozen=True)
class RunSummary:
    """One run directory in the index.  `files: 0` is a MEASURED empty, not "unknown"."""

    run_id: str
    files: int
    total_bytes: int
    latest_modified_at: datetime | None
    truncated: bool = False


@dataclass(frozen=True)
class ArtifactIndex:
    """`GET /artifacts` — every run workspace on this volume, newest activity first."""

    runs: list[RunSummary] = field(default_factory=list)
    truncated: bool = False
    skipped: list[SkippedEntry] = field(default_factory=list)


@dataclass(frozen=True)
class RunListing:
    """`GET /artifacts/{run_id}` — the files one run wrote."""

    run_id: str
    files: list[ArtifactFile] = field(default_factory=list)
    total_bytes: int = 0
    truncated: bool = False
    skipped_links: int = 0


@dataclass(frozen=True)
class ResolvedArtifact:
    """A containment-proven artifact path: `absolute` is the RESOLVED path callers open."""

    run_id: str
    path: str
    absolute: Path
    directory: Path


@dataclass(frozen=True)
class ArtifactContent:
    """`GET /artifacts/{run_id}/content` — one text artifact, decoded."""

    run_id: str
    path: str
    text: str
    size: int
    sha256: str
    modified_at: datetime


@dataclass(frozen=True)
class _Walked:
    """One run workspace's walk result.  `file_count` is the MEASURED number of regular files."""

    files: list[ArtifactFile] = field(default_factory=list)
    file_count: int = 0
    total_bytes: int = 0
    latest_modified_at: datetime | None = None
    skipped_links: int = 0
    truncated: bool = False


@dataclass(frozen=True)
class ArtifactBytes:
    """`GET /artifacts/{run_id}/download` — one artifact's exact bytes."""

    run_id: str
    path: str
    data: bytes
    sha256: str
    modified_at: datetime

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def filename(self) -> str:
        """The last path component — a safe `Content-Disposition` filename."""
        return self.path.rsplit("/", 1)[-1]


# ── workspace resolution ──────────────────────────────────────────────────────────────────────


def workspace_root(root: str | Path | None = None) -> Path:
    """The configured code workspace, resolved; a misconfiguration is the named `E_CONFIG`.

    Read from `settings.code_workspace_dir` at CALL time (the `tool_registry` rule: a relative
    path resolves against the backend working directory, so `data/workspace` is
    `backend/data/workspace` in a checkout and `/app/data/workspace` in the image).  The
    directory is NOT created here: this module only reads, and creating it would make an
    unconfigured root look like a working store.
    """
    if root is not None:
        candidate = Path(root).expanduser()
    else:
        from app.config import settings

        raw = getattr(settings, "code_workspace_dir", CODE_WORKSPACE_DEFAULT)
        text = raw.strip() if isinstance(raw, str) else ""
        candidate = Path(text or CODE_WORKSPACE_DEFAULT).expanduser()
    try:
        resolved = candidate.resolve()
    except (OSError, ValueError) as exc:
        raise PolsiaError(
            E_CONFIG,
            f"artifacts: the code workspace {str(candidate)!r} cannot be resolved "
            f"({type(exc).__name__}: {exc})",
            how_to_fix="set CODE_WORKSPACE_DIR to a directory this process can read and retry",
            context={"workspace": str(candidate)},
        ) from exc
    if resolved.exists() and not resolved.is_dir():
        raise PolsiaError(
            E_CONFIG,
            f"artifacts: the code workspace is {str(resolved)!r}, which exists but is not a "
            "directory — no artifact can be served from it",
            how_to_fix="point CODE_WORKSPACE_DIR at a directory (or delete the file in the way)",
            context={"workspace": str(resolved)},
        )
    return resolved


def runs_root(root: str | Path | None = None) -> Path:
    """`<workspace>/runs` — the directory `code_write` creates per-run subdirectories under.

    A `runs` entry that is a LINK is refused (`E_CONFIG`): every artifact path is proven to sit
    under this directory, and a link here would make that proof point somewhere else.  Absent is
    an honest "no run has written a workspace yet" — this function does not create it.
    """
    base = workspace_root(root)
    candidate = base / RUN_WORKSPACE_SUBDIR
    try:
        resolved = candidate.resolve()
    except (OSError, ValueError) as exc:
        raise PolsiaError(
            E_CONFIG,
            f"artifacts: the runs directory {str(candidate)!r} cannot be resolved "
            f"({type(exc).__name__}: {exc})",
            how_to_fix="check the code workspace directory and its permissions",
            context={"runs_dir": str(candidate)},
        ) from exc
    if resolved != base and base not in resolved.parents:
        raise PolsiaError(
            E_CONFIG,
            f"artifacts: the runs directory resolves outside the code workspace "
            f"({str(resolved)!r}) — a link cannot be used to serve files from elsewhere",
            how_to_fix="remove the link or junction at runs/ and restart the service",
            context={"runs_dir": str(resolved), "workspace": str(base)},
        )
    if candidate.is_symlink():
        raise PolsiaError(
            E_CONFIG,
            f"artifacts: the runs directory {str(candidate)!r} is a symbolic link — artifacts "
            "are only ever served from the configured workspace itself",
            how_to_fix="replace the link with a real directory (or keep the workspace local)",
            context={"runs_dir": str(candidate)},
        )
    if resolved.exists() and not resolved.is_dir():
        raise PolsiaError(
            E_CONFIG,
            f"artifacts: {str(resolved)!r} exists but is not a directory",
            how_to_fix="remove the file at that path, or repoint CODE_WORKSPACE_DIR",
            context={"runs_dir": str(resolved)},
        )
    return resolved


# ── path safety (the code tools' rules, re-proven here rather than imported privately) ───────


def validate_run_id(run_id: object) -> str:
    """A run id this surface will enter, or the named `E_VALIDATION`.

    The accepted set is the shape `code_write`'s run workspace can have: 1-64 characters from
    `[A-Za-z0-9_-]`, starting alphanumeric.  Separators, dots, `..`, spaces and Unicode
    look-alikes cannot pass, so a hostile id can never name a directory outside `runs/`.
    """
    if not isinstance(run_id, str) or not run_id.strip():
        raise PolsiaError(
            E_VALIDATION,
            f"artifacts: run_id must be a non-blank string (got {type(run_id).__name__})",
            how_to_fix="pass the numeric run id from GET /api/v1/runs",
            context={"field": "run_id"},
        )
    name = run_id.strip()
    if not RUN_ID_PATTERN.fullmatch(name):
        raise PolsiaError(
            E_VALIDATION,
            f"artifacts: run_id {run_id!r} is not a safe run id",
            how_to_fix="pass 1-64 letters, digits, '_' or '-' (no separators, dots or '..')",
            context={"field": "run_id", "run_id": name[:80]},
        )
    return name


def relative_parts(path: object) -> list[str]:
    """A safe workspace-RELATIVE path as parts, or the named `E_VALIDATION`.

    Refused BY NAME: a non-string/blank path, control characters (including NUL),
    POSIX-absolute (`/...`), Windows drive-letter (`C:...`, `C:relative`) and UNC
    (`\\\\server...`) forms, and any `..` component.  `.`/empty components collapse; a
    backslash is accepted as a separator and normalised, exactly as `code_write` accepts it.
    """
    if not isinstance(path, str):
        raise PolsiaError(
            E_VALIDATION,
            f"artifacts: 'path' must be a string (got {type(path).__name__})",
            how_to_fix="pass the artifact's path as returned by GET /api/v1/artifacts/{run_id}",
            context={"field": "path"},
        )
    text = path.strip()
    if not text:
        raise PolsiaError(
            E_VALIDATION,
            "artifacts: a non-empty relative 'path' is required",
            how_to_fix="pass the artifact's path as returned by GET /api/v1/artifacts/{run_id}",
            context={"field": "path"},
        )
    if any(ord(ch) < 32 for ch in text):
        raise PolsiaError(
            E_VALIDATION,
            "artifacts: 'path' contains control characters (including NUL)",
            how_to_fix="pass a plain relative path",
            context={"field": "path"},
        )
    normalized = text.replace("\\", "/")
    if normalized.startswith("/"):
        raise PolsiaError(
            E_VALIDATION,
            f"artifacts: absolute paths are refused ({path!r}) — only paths inside the run "
            "workspace are served",
            how_to_fix="drop the leading slash and any drive/share prefix",
            context={"field": "path", "path": text[:200]},
        )
    if _DRIVE_PREFIX.match(normalized):
        raise PolsiaError(
            E_VALIDATION,
            f"artifacts: absolute (drive-letter) paths are refused ({path!r}) — only paths "
            "inside the run workspace are served",
            how_to_fix="drop the drive prefix and pass a run-relative path",
            context={"field": "path", "path": text[:200]},
        )
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise PolsiaError(
            E_VALIDATION,
            f"artifacts: '..' path traversal is refused ({path!r}) — a path can never leave the "
            "run workspace",
            how_to_fix="pass a path inside the run workspace (no '..' components)",
            context={"field": "path", "path": text[:200]},
        )
    if not parts:
        raise PolsiaError(
            E_VALIDATION,
            f"artifacts: {path!r} names no file",
            how_to_fix="pass the artifact's path as returned by GET /api/v1/artifacts/{run_id}",
            context={"field": "path"},
        )
    return parts


def run_dir(run_id: object, root: str | Path | None = None) -> tuple[str, Path]:
    """`(run_id, run directory)` for a run that HAS a workspace, or a named refusal.

    The directory must sit under `runs/` after `resolve()` — a junction planted at `runs/<id>`
    is refused (`E_VALIDATION`) rather than followed.  Absent is `E_NOT_FOUND` (a missing
    workspace is not "this run wrote nothing"), a non-directory is `E_VALIDATION`.
    """
    name = validate_run_id(run_id)
    runs = runs_root(root)
    candidate = runs / name
    try:
        exists = candidate.exists()
    except OSError as exc:  # a path the OS cannot even stat
        raise PolsiaError(
            E_STORAGE,
            f"artifacts: run workspace {name!r} cannot be read ({type(exc).__name__}: {exc})",
            how_to_fix="check that the code workspace directory is readable by this process",
            context={"run_id": name},
        ) from exc
    if not exists:
        raise PolsiaError(
            E_NOT_FOUND,
            f"artifacts: run {name} has no workspace — nothing was written for it",
            how_to_fix=(
                "check the run id with GET /api/v1/runs; a run that wrote no file has no "
                "workspace directory (the code tools create it on first use)"
            ),
            context={"run_id": name},
        )
    try:
        resolved = candidate.resolve()
    except (OSError, ValueError) as exc:
        raise PolsiaError(
            E_STORAGE,
            f"artifacts: run workspace {name!r} cannot be resolved "
            f"({type(exc).__name__}: {exc})",
            how_to_fix="check the code workspace directory and its permissions",
            context={"run_id": name},
        ) from exc
    if resolved != runs and runs not in resolved.parents:
        raise PolsiaError(
            E_VALIDATION,
            f"artifacts: run workspace {name!r} resolves outside the runs directory — a link "
            "or junction cannot be used to serve files from elsewhere",
            how_to_fix="remove the link under runs/ and retry; run workspaces stay inside runs/",
            context={"run_id": name, "resolved": str(resolved)},
        )
    if not resolved.is_dir():
        raise PolsiaError(
            E_VALIDATION,
            f"artifacts: {name!r} exists in the runs directory but is not a directory",
            how_to_fix="remove the conflicting file, or pass a real run id",
            context={"run_id": name},
        )
    return name, resolved


def resolve_artifact(
    run_id: object, path: object, root: str | Path | None = None
) -> ResolvedArtifact:
    """A containment-proven artifact, or a named refusal.

    THE CONTAINMENT PROOF: the candidate is `run_dir / parts...`, and after `Path.resolve()`
    (which follows every link and junction) it must equal the run directory or sit under it.
    The result carries the RESOLVED path — callers open that, never the un-resolved candidate —
    so a link swapped in after this check cannot redirect the read.  A directory is refused
    (`E_VALIDATION`: this surface serves files), an absent file is `E_NOT_FOUND` naming it.
    """
    name, directory = run_dir(run_id, root)
    parts = relative_parts(path)
    candidate = directory.joinpath(*parts)
    try:
        resolved = candidate.resolve()
    except (OSError, ValueError) as exc:
        raise PolsiaError(
            E_STORAGE,
            f"artifacts: path {path!r} cannot be resolved ({type(exc).__name__}: {exc})",
            how_to_fix="pass a plain relative path inside the run workspace",
            context={"run_id": name, "path": str(path)[:200]},
        ) from exc
    if resolved != directory and directory not in resolved.parents:
        raise PolsiaError(
            E_VALIDATION,
            f"artifacts: path {path!r} escapes the run workspace — absolute paths, '..' and "
            "symlink escapes are refused",
            how_to_fix="use a path inside the run workspace; nothing outside it is reachable",
            context={"run_id": name, "path": str(path)[:200], "resolved": str(resolved)},
        )
    relative = "." if resolved == directory else resolved.relative_to(directory).as_posix()
    if not resolved.exists():
        raise PolsiaError(
            E_NOT_FOUND,
            f"artifacts: no file {relative!r} exists in run {name}'s workspace",
            how_to_fix="list the run's artifacts first (GET /api/v1/artifacts/{run_id})",
            context={"run_id": name, "path": relative},
        )
    if resolved.is_symlink():
        raise PolsiaError(
            E_VALIDATION,
            f"artifacts: {relative!r} is a symbolic link — artifacts are served as regular "
            "files only, and a link is never followed",
            how_to_fix="the artifact surface does not serve links; download the real file",
            context={"run_id": name, "path": relative},
        )
    if not resolved.is_file():
        raise PolsiaError(
            E_VALIDATION,
            f"artifacts: {relative!r} is not a regular file (it is a directory or a device)",
            how_to_fix="pass the path of a file; the listing shows which entries are files",
            context={"run_id": name, "path": relative},
        )
    return ResolvedArtifact(run_id=name, path=relative, absolute=resolved, directory=directory)


# ── reads ────────────────────────────────────────────────────────────────────────────────────


def _stat(path: Path, *, run_id: str, relative: str) -> os.stat_result:
    """`os.stat` with an `E_STORAGE` refusal instead of an unhandled `OSError`."""
    try:
        return path.stat()
    except OSError as exc:
        raise PolsiaError(
            E_STORAGE,
            f"artifacts: {relative!r} in run {run_id} cannot be read "
            f"({type(exc).__name__}: {exc})",
            how_to_fix="check that the code workspace directory is readable by this process",
            context={"run_id": run_id, "path": relative},
        ) from exc


def _utc(stamp: float) -> datetime:
    """A filesystem mtime as aware UTC (the wire spelling every response model uses)."""
    return datetime.fromtimestamp(stamp, tz=UTC)


def _walk(
    run_id: str, directory: Path, *, collect: bool, limit: int = MAX_LIST_ENTRIES
) -> _Walked:
    """Walk one run workspace: `(files, total_bytes, latest_mtime, skipped_links, truncated)`.

    `os.walk(followlinks=False)` does not descend symbolic links, but a Windows JUNCTION is a
    reparse-point directory that `os.walk` WILL descend — so every directory is additionally
    proven to resolve inside the run directory before it is kept.  Files that are links are
    counted (`skipped_links`) and never returned.

    `truncated` is true when the walk hit `MAX_SCAN_FILES` examined files OR (in `collect` mode)
    the returned list hit `limit`: in either case `total_bytes`/`latest_mtime` cover only the
    entries actually examined, and every caller says so rather than presenting a partial sum as
    a total.
    """
    files: list[ArtifactFile] = []
    total_bytes = 0
    latest: datetime | None = None
    skipped_links = 0
    examined = 0
    truncated = False
    try:
        for dirpath, dirnames, filenames in os.walk(directory, followlinks=False):
            base = Path(dirpath)
            kept: list[str] = []
            for child in sorted(dirnames):
                child_path = base / child
                try:
                    child_resolved = child_path.resolve()
                except (OSError, ValueError):
                    skipped_links += 1
                    continue
                if child_resolved != directory and directory not in child_resolved.parents:
                    skipped_links += 1
                    continue
                if child_path.is_symlink():
                    skipped_links += 1
                    continue
                kept.append(child)
            dirnames[:] = kept
            for filename in sorted(filenames):
                if examined >= MAX_SCAN_FILES:
                    truncated = True
                    break
                path = base / filename
                try:
                    if path.is_symlink():
                        skipped_links += 1
                        continue
                    stat = path.stat()
                except OSError:
                    skipped_links += 1
                    continue
                examined += 1
                total_bytes += stat.st_size
                modified = _utc(stat.st_mtime)
                if latest is None or modified > latest:
                    latest = modified
                if collect:
                    if len(files) >= limit:
                        truncated = True
                        continue
                    relative = path.relative_to(directory).as_posix()
                    files.append(
                        ArtifactFile(path=relative, size=stat.st_size, modified_at=modified)
                    )
            if truncated and (not collect or len(files) >= limit):
                break
    except OSError as exc:
        raise PolsiaError(
            E_STORAGE,
            f"artifacts: run {run_id}'s workspace cannot be read ({type(exc).__name__}: {exc})",
            how_to_fix="check that the code workspace directory is readable by this process",
            context={"run_id": run_id},
        ) from exc
    return _Walked(
        files=files,
        file_count=examined,
        total_bytes=total_bytes,
        latest_modified_at=latest,
        skipped_links=skipped_links,
        truncated=truncated,
    )


def list_runs(root: str | Path | None = None, *, limit: int = MAX_RUNS) -> ArtifactIndex:
    """Every run workspace under `runs/`, newest activity first — bounded and honest.

    * the runs directory absent → an empty index (a measured "no run has written a workspace");
    * a directory whose name is not a safe run id → reported in `skipped` with the reason,
      never entered;
    * a directory that resolves outside `runs/` (a link/junction) → reported in `skipped`,
      never entered;
    * a run with no files is INCLUDED with `files: 0` — the measured fact that this run wrote
      nothing, which is how a claimed-but-missing deliverable becomes visible instead of
      disappearing from a listing that only shows non-empty runs;
    * more than `limit` run directories → `truncated: true` (never a silent cut).
    """
    runs = runs_root(root)
    try:
        if not runs.exists():
            return ArtifactIndex(runs=[], truncated=False, skipped=[])
        names = sorted(
            (entry.name for entry in os.scandir(runs) if entry.is_dir(follow_symlinks=False)),
            reverse=True,
        )
    except OSError as exc:
        raise PolsiaError(
            E_STORAGE,
            f"artifacts: the runs directory cannot be listed ({type(exc).__name__}: {exc})",
            how_to_fix="check that the code workspace directory is readable by this process",
            context={"runs_dir": str(runs)},
        ) from exc

    summaries: list[RunSummary] = []
    skipped: list[SkippedEntry] = []
    truncated = False
    for name in names:
        if len(summaries) >= limit:
            truncated = True
            break
        if not RUN_ID_PATTERN.fullmatch(name):
            if len(skipped) < MAX_SKIP_DETAILS:
                skipped.append(SkippedEntry(name=name, reason="not a safe run id"))
            continue
        candidate = runs / name
        if candidate.is_symlink():
            if len(skipped) < MAX_SKIP_DETAILS:
                skipped.append(SkippedEntry(name=name, reason="symbolic link"))
            continue
        try:
            resolved = candidate.resolve()
        except (OSError, ValueError):
            if len(skipped) < MAX_SKIP_DETAILS:
                skipped.append(SkippedEntry(name=name, reason="path cannot be resolved"))
            continue
        if resolved != runs and runs not in resolved.parents:
            if len(skipped) < MAX_SKIP_DETAILS:
                skipped.append(SkippedEntry(name=name, reason="resolves outside the runs dir"))
            continue
        walked = _walk(name, resolved, collect=False)
        summaries.append(
            RunSummary(
                run_id=name,
                files=walked.file_count,
                total_bytes=walked.total_bytes,
                latest_modified_at=walked.latest_modified_at,
                truncated=walked.truncated,
            )
        )
    summaries.sort(
        key=lambda item: (
            item.latest_modified_at is not None,
            item.latest_modified_at or datetime.min.replace(tzinfo=UTC),
            item.run_id,
        ),
        reverse=True,
    )
    return ArtifactIndex(runs=summaries, truncated=truncated, skipped=skipped)


def list_run_artifacts(
    run_id: object, root: str | Path | None = None, *, limit: int = MAX_LIST_ENTRIES
) -> RunListing:
    """The files one run wrote: relative posix paths, exact byte sizes, mtimes.

    A run directory that exists but holds no file is `total: 0` — a measured zero.  A nested
    directory that resolves outside the run root (a link/junction) is skipped and COUNTED in
    `skipped_links`; a symlinked file is never listed as a readable artifact either (it is
    counted, not served).  Past `limit` files → `truncated: true`.
    """
    name, directory = run_dir(run_id, root)
    walked = _walk(name, directory, collect=True, limit=limit)
    files = sorted(walked.files, key=lambda item: item.path)
    return RunListing(
        run_id=name,
        files=files,
        # The byte sum of the files THIS listing returns — `truncated` says whether more exist,
        # so a partial page can never be read as the run's whole size.
        total_bytes=sum(file.size for file in files),
        truncated=walked.truncated,
        skipped_links=walked.skipped_links,
    )


def read_artifact_text(
    run_id: object,
    path: object,
    root: str | Path | None = None,
    *,
    max_bytes: int = MAX_INLINE_BYTES,
) -> ArtifactContent:
    """One artifact decoded as UTF-8 text — or a named refusal.

    Over `max_bytes` refuses naming the real size and the cap (never a truncated body, never a
    silent partial file) and points at the download route.  A NUL byte or an undecodable body is
    `E_VALIDATION` ("not text") — this workspace is text-only by construction, so such a file is
    an anomaly the caller must see, not a preview that renders replacement characters.
    """
    artifact = resolve_artifact(run_id, path, root)
    stat = _stat(artifact.absolute, run_id=artifact.run_id, relative=artifact.path)
    if stat.st_size > max_bytes:
        raise PolsiaError(
            E_VALIDATION,
            f"artifacts: {artifact.path!r} is {stat.st_size} bytes, over the {max_bytes}-byte "
            "cap for an inline preview",
            how_to_fix="use GET /api/v1/artifacts/{run_id}/download to fetch the complete file",
            context={
                "run_id": artifact.run_id,
                "path": artifact.path,
                "bytes": stat.st_size,
                "cap_bytes": max_bytes,
            },
        )
    try:
        data = artifact.absolute.read_bytes()
    except OSError as exc:
        raise PolsiaError(
            E_STORAGE,
            f"artifacts: {artifact.path!r} in run {artifact.run_id} cannot be read "
            f"({type(exc).__name__}: {exc})",
            how_to_fix="check that the code workspace directory is readable by this process",
            context={"run_id": artifact.run_id, "path": artifact.path},
        ) from exc
    if b"\x00" in data:
        raise PolsiaError(
            E_VALIDATION,
            f"artifacts: {artifact.path!r} contains a NUL byte — it is binary, not text",
            how_to_fix="download it instead (GET /api/v1/artifacts/{run_id}/download)",
            context={"run_id": artifact.run_id, "path": artifact.path},
        )
    try:
        text = data.decode(TEXT_ENCODING)
    except UnicodeDecodeError as exc:
        raise PolsiaError(
            E_VALIDATION,
            f"artifacts: {artifact.path!r} is not valid {TEXT_ENCODING} text "
            f"({exc.reason} at byte {exc.start})",
            how_to_fix="download it instead (GET /api/v1/artifacts/{run_id}/download)",
            context={
                "run_id": artifact.run_id,
                "path": artifact.path,
                "byte_offset": exc.start,
            },
        ) from exc
    return ArtifactContent(
        run_id=artifact.run_id,
        path=artifact.path,
        text=text,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        modified_at=_utc(stat.st_mtime),
    )


def read_artifact_bytes(
    run_id: object,
    path: object,
    root: str | Path | None = None,
    *,
    max_bytes: int = MAX_DOWNLOAD_BYTES,
) -> ArtifactBytes:
    """One artifact's exact bytes for a download — or a named refusal over `max_bytes`."""
    artifact = resolve_artifact(run_id, path, root)
    stat = _stat(artifact.absolute, run_id=artifact.run_id, relative=artifact.path)
    if stat.st_size > max_bytes:
        raise PolsiaError(
            E_VALIDATION,
            f"artifacts: {artifact.path!r} is {stat.st_size} bytes, over the {max_bytes}-byte "
            "cap for one download",
            how_to_fix=(
                "this surface serves text deliverables up to 8 MiB; read the file from the run "
                "workspace on the host instead"
            ),
            context={
                "run_id": artifact.run_id,
                "path": artifact.path,
                "bytes": stat.st_size,
                "cap_bytes": max_bytes,
            },
        )
    try:
        data = artifact.absolute.read_bytes()
    except OSError as exc:
        raise PolsiaError(
            E_STORAGE,
            f"artifacts: {artifact.path!r} in run {artifact.run_id} cannot be read "
            f"({type(exc).__name__}: {exc})",
            how_to_fix="check that the code workspace directory is readable by this process",
            context={"run_id": artifact.run_id, "path": artifact.path},
        ) from exc
    return ArtifactBytes(
        run_id=artifact.run_id,
        path=artifact.path,
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
        modified_at=_utc(stat.st_mtime),
    )


__all__ = [
    "ArtifactBytes",
    "ArtifactContent",
    "ArtifactFile",
    "ArtifactIndex",
    "CODE_WORKSPACE_DEFAULT",
    "MAX_DOWNLOAD_BYTES",
    "MAX_INLINE_BYTES",
    "MAX_LIST_ENTRIES",
    "MAX_RUNS",
    "MAX_SCAN_FILES",
    "RUN_ID_PATTERN",
    "RUN_WORKSPACE_SUBDIR",
    "ResolvedArtifact",
    "RunListing",
    "RunSummary",
    "SkippedEntry",
    "TEXT_ENCODING",
    "list_run_artifacts",
    "list_runs",
    "read_artifact_bytes",
    "read_artifact_text",
    "relative_parts",
    "resolve_artifact",
    "run_dir",
    "runs_root",
    "validate_run_id",
    "workspace_root",
]
