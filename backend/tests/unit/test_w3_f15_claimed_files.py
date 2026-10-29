"""F-15 (wave 3) — a run may only claim the files it really wrote.

Measured defect (VISUALS-2 F-15, live twice):

  * task #302 (COMPLETED) claimed it "wrote `notes/302-logo-and-label-spec.md` in the local code
    workspace"; `find /app/data/workspace/runs/301 -type f` was EMPTY and the run's ledger had no
    `code_write` at all;
  * task #312 (`business_planning`, COMPLETED) claimed a decision memo was "written to
    `notes/312-candle-vs-soap-decision-memo.md`" — an agent with no write tool, no file, no route
    to open one.  A fabricated deliverable and a lost one read identically to the owner.

The rule pinned here: the run's own text is checked against its OWN workspace
(`<code_workspace_dir>/runs/<run_id>/`, the tree `code_write` writes and `artifact_service` serves),
and a claim with no file behind it is LABELLED in the summary the owner reads — never silently
dropped, never assumed true.  A run that claims nothing is untouched; an unverifiable workspace is
reported as unverified, not as missing.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.agents.code_generation.agent import CodeGenerationAgent
from app.services import claimed_files
from app.services.claimed_files import (
    CLAIM_LABEL_OPEN,
    CLAIMED_FILES_KEY,
    audit_claimed_files,
    extract_claimed_paths,
    normalise_claimed_path,
)

RUN_ID = 312


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch) -> Path:
    """A throwaway `code_workspace_dir` so the audit reads THIS test's tree, not the repo's.

    The real resolution path runs end to end: `audit_claimed_files` -> `artifact_service` ->
    `settings.code_workspace_dir` -> `<root>/runs/<id>`.  Only the directory is redirected.
    """
    from app.config import settings

    root = tmp_path / "workspace"
    (root / "runs").mkdir(parents=True)
    monkeypatch.setattr(settings, "code_workspace_dir", str(root))
    return root


# ── the extractor ────────────────────────────────────────────────────────────────────────────


def test_the_extractor_finds_real_claims_and_ignores_urls_dates_and_prose() -> None:
    assert extract_claimed_paths(
        "I wrote `notes/302-logo-and-label-spec.md` in the local code workspace."
    ) == ["notes/302-logo-and-label-spec.md"]
    assert extract_claimed_paths(
        "Decision memo (task 312) written to `notes/312-candle-vs-soap-decision-memo.md`"
    ) == ["notes/312-candle-vs-soap-decision-memo.md"]
    # URLs, dates, "and/or" and prose about writing nothing are NOT claims.
    assert extract_claimed_paths(
        "See http://localhost/api/v1/tasks, 10/28/2026, and/or the docs; no files claimed here."
    ) == []
    assert extract_claimed_paths("prices are quote-only; nothing was written") == []


def test_a_claim_is_normalised_to_a_run_relative_path() -> None:
    assert normalise_claimed_path("data/workspace/runs/316/brand/brand-one-pager.md") == (
        "brand/brand-one-pager.md"
    )
    assert normalise_claimed_path("runs/301/x.md") == "x.md"
    assert normalise_claimed_path("./notes/a.txt") == "notes/a.txt"
    # A traversal claim is kept AS-IS so the audit can call it absent instead of matching it.
    assert normalise_claimed_path("../escape.md") == "../escape.md"


# ── the audit ───────────────────────────────────────────────────────────────────────────────


def test_a_claimed_file_that_was_really_written_is_confirmed(workspace: Path) -> None:
    written = workspace / "runs" / str(RUN_ID) / "brand" / "brand-one-pager.md"
    written.parent.mkdir(parents=True)
    written.write_text("brand one-pager", encoding="utf-8")
    result = {
        "summary": "Wrote brand/brand-one-pager.md for the launch.",
        "notes": [],
    }

    record = audit_claimed_files(run_id=RUN_ID, result=result)

    assert record["claims"] == [
        {"path": "brand/brand-one-pager.md", "status": "written", "bytes": 15}
    ]
    assert record["missing"] == []
    assert CLAIM_LABEL_OPEN not in result["summary"], "a true claim gains no label"


def test_a_claimed_file_that_was_not_written_is_labelled_in_the_summary(workspace: Path) -> None:
    """RED-1: the live shape — no workspace at all, a COMPLETED run naming a deliverable."""
    result = {
        "summary": "Decision memo (task 312) written to notes/312-candle-vs-soap-decision-memo.md.",
        "notes": ["the candle comparison is quote-only"],
    }

    record = audit_claimed_files(run_id=RUN_ID, result=result)

    assert record["missing"] == ["notes/312-candle-vs-soap-decision-memo.md"]
    assert record["claims"][0]["status"] == "missing"
    # The owner reads the summary: the false claim must be labelled THERE.
    assert CLAIM_LABEL_OPEN in result["summary"]
    assert "NOT written by this run" in result["summary"]
    assert "notes/312-candle-vs-soap-decision-memo.md" in result["summary"]
    # ...and the structured record rides the stored output.
    assert result[CLAIMED_FILES_KEY] == record
    assert any("claim check:" in note for note in result["notes"]), result["notes"]


def test_a_run_that_claims_no_file_is_byte_identical(workspace: Path) -> None:
    result = {"summary": "Nothing to do this cycle: no supplier quotes in context.", "notes": []}
    before = dict(result)

    assert audit_claimed_files(run_id=RUN_ID, result=result) == {}
    assert result == before, "a run that claims nothing must not grow keys or labels"


def test_the_label_is_written_once_and_names_at_most_three_paths(workspace: Path) -> None:
    result = {
        "summary": "Wrote notes/a.md, notes/b.md, notes/c.md and notes/d.md.",
        "notes": [],
    }

    audit_claimed_files(run_id=RUN_ID, result=result)
    labelled = result["summary"]
    audit_claimed_files(run_id=RUN_ID, result=result)      # idempotent: a replay cannot stack labels

    assert result["summary"] == labelled
    assert result["summary"].count(CLAIM_LABEL_OPEN) == 1
    assert "and 1 more" in result["summary"], result["summary"]


def test_a_partially_written_claim_only_flags_the_absent_file(workspace: Path) -> None:
    written = workspace / "runs" / str(RUN_ID) / "brand" / "brand-one-pager.md"
    written.parent.mkdir(parents=True)
    written.write_text("brand one-pager", encoding="utf-8")
    result = {
        "summary": "Wrote brand/brand-one-pager.md and brand/label-data-checklist.md.",
        "notes": [],
    }

    record = audit_claimed_files(run_id=RUN_ID, result=result)

    assert [claim["path"] for claim in record["claims"]] == [
        "brand/brand-one-pager.md",
        "brand/label-data-checklist.md",
    ]
    assert record["missing"] == ["brand/label-data-checklist.md"]
    assert "brand/label-data-checklist.md" in result["summary"]
    assert CLAIM_LABEL_OPEN in result["summary"]


def test_an_unverifiable_workspace_is_unverified_not_missing(workspace: Path, monkeypatch) -> None:
    """'Could not check' must never be reported as 'did not write'."""
    run_dir = workspace / "runs" / str(RUN_ID)
    run_dir.mkdir(parents=True)
    # Two files + a page size of one => the listing is truncated, so a claim may be beyond the page.
    (run_dir / "a.md").write_text("a", encoding="utf-8")
    (run_dir / "b.md").write_text("b", encoding="utf-8")
    real = claimed_files.artifact_service.list_run_artifacts
    monkeypatch.setattr(
        claimed_files.artifact_service,
        "list_run_artifacts",
        lambda run_id, root=None: real(run_id, root, limit=1),
    )
    result = {"summary": "Wrote notes/b.md for the launch.", "notes": []}

    record = audit_claimed_files(run_id=RUN_ID, result=result)

    assert record["workspace"] == "truncated"
    assert record["unverified"] == ["notes/b.md"]
    assert record["missing"] == []
    assert CLAIM_LABEL_OPEN not in result["summary"], "an unverified claim must not be called false"


def test_a_run_with_no_id_gets_an_unverified_record_rather_than_a_guess(workspace: Path) -> None:
    result = {"summary": "Wrote notes/a.md.", "notes": []}

    record = audit_claimed_files(run_id="", result=result)

    assert record["workspace"] == "unreadable"
    assert record["unverified"] == ["notes/a.md"]
    assert record["missing"] == []


# ── the seam: every dispatched run passes through timed_run ─────────────────────────────────


def test_the_audit_runs_at_the_timed_run_seam(workspace: Path) -> None:
    """The label must be applied where the summary is finalised (before the worker stores it)."""
    written = workspace / "runs" / "77" / "brand" / "real.md"
    written.parent.mkdir(parents=True)
    written.write_text("real", encoding="utf-8")

    class _Agent(CodeGenerationAgent):
        def run(self, task: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
            return {"summary": "Wrote brand/real.md and brand/fake.md.", "status": "ok"}

    result = _Agent().timed_run(
        {"title": "brand one-pager", "run_id": "77"},
        {"company": {"name": "Hearth & Lather"}},
    )

    assert "brand/real.md" in result["summary"]
    assert CLAIM_LABEL_OPEN in result["summary"]
    assert "brand/fake.md" in result["summary"]
    assert result[CLAIMED_FILES_KEY]["missing"] == ["brand/fake.md"]
