"""F-15 (wave 3) — a run may only claim the files it really wrote.

Measured defect (VISUALS-2 F-15, live twice):

  * task #302's result said it "wrote `notes/302-logo-and-label-spec.md` in the local code
    workspace".  The path did not exist anywhere on the box, and the run's own ledger had no
    `code_write` call — the task was COMPLETED;
  * task #312 (`business_planning`) said *"Decision memo (task 312) written to
    `notes/312-candle-vs-soap-decision-memo.md`"* — an agent with NO write tool at all, no file,
    COMPLETED.

An owner cannot tell a fabricated deliverable from a lost one, which is the whole debate: the file
either exists in the run's workspace or the claim is false.

WHAT THIS MODULE DOES (and what it deliberately does not)

  * it READS the run's own human-facing text (`summary` + `notes`) for path-like tokens with a
    file extension, and checks each one against `<code_workspace_dir>/runs/<run_id>/` — the exact
    tree `code_write` writes into and `artifact_service` serves (this module uses that service's
    listing/containment rules rather than re-implementing path safety);
  * a claimed path that is NOT in the workspace is LABELLED in the very text the owner reads
    (`summary` gains one bracketed sentence, `notes` gains one line) and recorded as structured
    data under `claimed_files` on the stored output.  A claim is never silently deleted: the run
    said it, and the record must show both the claim and the check;
  * it never invents a path, never searches the whole filesystem, and never treats "the workspace
    is missing" as "the file may be somewhere else": no workspace means nothing was written by
    `code_write`, which is exactly what the label says;
  * a run that claims NO path is untouched — no new keys, byte-identical output (the same rule
    the wave-2 cost guard follows);
  * an UNVERIFIABLE claim (a truncated listing, an unreadable workspace) is reported as
    `unverified`, not as missing: "could not check" must never read as "did not write".

The seam is `BasePolsiaAgent.timed_run` (every dispatched run, planner runs included), which is
where the run's `summary` is finalised before the worker copies it into `tasks.result_summary`.
"""
from __future__ import annotations

import re
from typing import Any

from app.core.errors import PolsiaError
from app.services import artifact_service

#: The result key carrying the audit.  Written ONLY when the run claimed at least one path, so a
#: run with no claims keeps its historical output key set.
CLAIMED_FILES_KEY = "claimed_files"

#: The label a summary gains when it claimed a file that is not there.  Bracketed and unmissable:
#: it rides the same text the false claim was in.
CLAIM_LABEL_OPEN = "[CLAIM CHECK:"

#: Extensions a text deliverable can have.  A path token WITHOUT one of these is not treated as a
#: file claim (so "api/v1/tasks", "10/28/2026" and "and/or" never become claims).
CLAIM_EXTS = (
    "md", "markdown", "txt", "csv", "tsv", "json", "yaml", "yml", "html", "htm", "svg", "png",
    "jpg", "jpeg", "pdf", "py", "sh", "js", "ts", "tsx", "jsx", "sql", "xml", "docx", "xlsx",
)

#: One path-like token: at least one `/`, a known extension, and a bounded character set.  Quotes,
#: backticks and trailing sentence punctuation are stripped afterwards (models wrap paths in all of
#: them, and a claim mid-sentence ends with a full stop).  The trailing lookahead excludes letters,
#: digits and separators — but NOT `.`, so `wrote notes/a.md.` is still a claim of `notes/a.md`.
_PATH_TOKEN = re.compile(
    r"(?<![\w/.-])((?:\.{0,2}/)?(?:[\w.-]+/)+[\w.-]+\.(?:" + "|".join(CLAIM_EXTS) + r"))(?![\w/-])",
    re.IGNORECASE,
)

#: Prefixes a claim may carry that are NOT part of the run-relative path.
_WORKSPACE_PREFIXES = ("data/workspace/", "./data/workspace/")


def _clean(token: str) -> str:
    """A raw token with wrapping quotes/backticks and sentence punctuation removed.

    A trailing `.` is stripped (a claim mid-sentence ends with a full stop) but leading dots are
    KEPT: `../x.md` must stay a traversal claim the audit can report, not silently become `x.md`.
    """
    text = token.strip().strip("`'\"()[]<>,;:«»“”‘’")
    return text.rstrip(".") if text.endswith(".") and not text.endswith("..") else text


def normalise_claimed_path(token: str) -> str:
    """A claimed token as a RUN-RELATIVE posix path (or "" when it names nothing usable).

    `runs/<id>/x.md`, `data/workspace/runs/<id>/x.md` and `./x.md` all normalise to the same
    run-relative path, because the claim is about a file IN THE RUN WORKSPACE either way.  A
    leading `../` or an absolute path is kept as-is so the audit can say "outside the workspace"
    instead of pretending it matched.
    """
    text = _clean(token).replace("\\", "/")
    if not text or "://" in text or any(ch.isspace() for ch in text):
        return ""
    while text.startswith("./"):
        text = text[2:]
    for prefix in _WORKSPACE_PREFIXES:
        if text.lower().startswith(prefix):
            text = text[len(prefix):]
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if not parts:
        return ""
    lowered = [part.lower() for part in parts]
    if "runs" in lowered:
        index = lowered.index("runs")
        # `runs/<id>/<path…>` (and a bare `runs/<id>`) is run-relative from <path…> on.
        remainder = parts[index + 2:]
        if remainder:
            return "/".join(remainder)
        return ""
    return "/".join(parts)


def extract_claimed_paths(text: str) -> list[str]:
    """Every distinct run-relative path a piece of run text CLAIMS, in first-seen order."""
    if not isinstance(text, str) or not text:
        return []
    seen: list[str] = []
    for match in _PATH_TOKEN.finditer(text):
        path = normalise_claimed_path(match.group(1))
        if path and path not in seen:
            seen.append(path)
    return seen


def _claim_texts(result: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    summary = result.get("summary")
    if isinstance(summary, str):
        texts.append(summary)
    notes = result.get("notes")
    if isinstance(notes, list):
        texts.extend(str(note) for note in notes if isinstance(note, str))
    return texts


def _label_summary(summary: str, missing: list[str]) -> str:
    """`summary` + ONE bracketed sentence naming the claims the workspace does not back."""
    shown = ", ".join(missing[:3])
    more = f" and {len(missing) - 3} more" if len(missing) > 3 else ""
    label = (
        f"{CLAIM_LABEL_OPEN} this run named {shown}{more}, but no such file exists in this run's "
        "workspace — the path was NOT written by this run]"
    )
    if CLAIM_LABEL_OPEN in summary:
        return summary
    return f"{summary.rstrip()} {label}".strip()


def audit_claimed_files(
    *, run_id: Any, result: dict[str, Any], root: str | None = None
) -> dict[str, Any]:
    """Check every file path the run's text claims against its OWN workspace; label what is absent.

    Returns the audit record (also written to `result[CLAIMED_FILES_KEY]` when there is anything
    to say) and MUTATES `result` in place: the summary gains its label and `notes` gains one line,
    because the summary is what the owner reads (`tasks.result_summary`).

    `{}` when the run claims no path, when the run has no id (an in-process probe, a planner step
    with no `agent_runs` row), or when the result carries no text.
    """
    if not isinstance(result, dict):
        return {}
    claimed: list[str] = []
    for text in _claim_texts(result):
        for path in extract_claimed_paths(text):
            if path not in claimed:
                claimed.append(path)
    if not claimed:
        return {}
    run_key = "" if run_id is None else str(run_id).strip()
    if not run_key:
        # No run identity => no workspace to check, and a guess would be worse than a label.
        return _record(result, {}, claimed, run_id="", truncated=False, unreadable=True)

    try:
        listing = artifact_service.list_run_artifacts(run_key, root)
    except PolsiaError as exc:
        # E_NOT_FOUND: the run has no workspace at all (nothing was written).  E_CONFIG/E_VALIDATION/
        # E_STORAGE: the workspace cannot be read or is not a run workspace — UNVERIFIABLE, which is
        # not the same claim as "not written".
        written: dict[str, int] = {}
        if exc.code in ("E_NOT_FOUND", "E_VALIDATION"):
            return _record(result, written, claimed, run_id=run_key, truncated=False,
                           unreadable=False)
        return _record(result, written, claimed, run_id=run_key, truncated=False, unreadable=True)

    written = {entry.path: entry.size for entry in listing.files}
    return _record(
        result, written, claimed, run_id=run_key, truncated=listing.truncated, unreadable=False
    )


def _record(
    result: dict[str, Any],
    written: dict[str, int],
    claimed: list[str],
    *,
    run_id: str,
    truncated: bool,
    unreadable: bool,
) -> dict[str, Any]:
    """Build, persist and LABEL the audit.  One truth per claim: found / missing / unverified."""
    claims: list[dict[str, Any]] = []
    missing: list[str] = []
    unverified: list[str] = []
    for path in claimed:
        if path in written:
            claims.append({"path": path, "status": "written", "bytes": written[path]})
        elif truncated or unreadable:
            claims.append({"path": path, "status": "unverified"})
            unverified.append(path)
        else:
            claims.append({"path": path, "status": "missing"})
            missing.append(path)
    record: dict[str, Any] = {
        "run_id": run_id,
        "workspace": "unreadable" if unreadable else ("truncated" if truncated else "read"),
        "claims": claims,
        "missing": missing,
        "unverified": unverified,
    }
    result[CLAIMED_FILES_KEY] = record
    if missing:
        summary = str(result.get("summary") or "")
        result["summary"] = _label_summary(summary, missing)
        notes = result.get("notes")
        notes = [str(note) for note in notes] if isinstance(notes, list) else []
        line = (
            f"claim check: this run's text named {len(missing)} file(s) that are NOT in its run "
            f"workspace ({', '.join(missing[:5])}) — a claimed deliverable is only real when the "
            "run workspace holds it"
        )
        if line not in notes and len(notes) < 20:
            notes.append(line)
            result["notes"] = notes
    return record


__all__ = [
    "CLAIMED_FILES_KEY",
    "CLAIM_EXTS",
    "CLAIM_LABEL_OPEN",
    "audit_claimed_files",
    "extract_claimed_paths",
    "normalise_claimed_path",
]
