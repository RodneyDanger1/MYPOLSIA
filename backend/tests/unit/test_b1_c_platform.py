"""Wave B1 / seat C5 (platform) — the gates a fresh clone runs must be the gates CI runs.

WHAT IS PINNED HERE, AND WHY EACH PIN IS A GATE RATHER THAN A COMMENT

  1. **F-44 (P1)** — two thirds of the test entry points were wired to nothing.  No workflow ran
     `tests/parity` or `tests/redteam` and `make test` did not either, so 206 green tests
     (measured: `tests/parity` + `tests/redteam` = 206 / 0 failed / 0 errors / 1 skipped on
     `501a2af`) executed nowhere a gate could see them.  The fix is not "add two lines": it is
     that the *set of suites CI runs* and the *set `make test` runs* are now compared, both
     ways, from a parse of the two files — so a future suite added to one entry point and not
     the other fails here instead of rotting.
  2. **A2A5-03 (P3)** — `make lint` was a strict subset of the CI lint job
     (`ruff check app/` vs `app/ celery_app/ tests/`), so the documented local gate was green
     while CI was red.  The scope lists are parsed out of both files and compared as SETS, not
     as substrings: an ordering change is not drift, a missing target is.
  3. **D6-R4 (P3)** — README.md:621 and COMPLETENESS.md:83 attributed the 107-operation surface
     to V29, whose own settlement note records **106**.  The 107th operation is wave A3's
     `GET /api/v1/config/phone`.  The COUNT is untouched (107 is what `create_app().openapi()`
     serves — `test_v28_adversarial.py` already pins the number against the live schema); what
     is pinned here is the ATTRIBUTION, because a count with the wrong source is a receipt that
     cannot be audited.

HOW THE FILES ARE READ
    `ci.yml` is parsed with `yaml.safe_load` (PyYAML is a hard dependency of `chromadb`, which
    `backend/requirements.txt` pins — and `tests/unit/test_compose_log_caps.py` already imports
    it), so a malformed workflow fails at parse time rather than passing a substring scan.
    `Makefile` gets a real parse — targets, prerequisites, recipe lines, `\\`-continuations — so
    "`test:` depends on `test-parity`" is a fact about the graph, never about a word appearing
    somewhere in the file.

RED evidence for all three legs: `wave-b1/raw/C5/red-legs.txt`.
"""
from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"
MAKEFILE = REPO_ROOT / "Makefile"
README = REPO_ROOT / "README.md"
COMPLETENESS = REPO_ROOT / "COMPLETENESS.md"

#: The two suites F-44 measured as running nowhere (206 tests on `501a2af`).
UNWIRED_SUITES = ("tests/parity", "tests/redteam")


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file the gates are read from: {path}"
    return path.read_text(encoding="utf-8")


# ── parsers ──────────────────────────────────────────────────────────────────────────────────


def _ci_steps() -> list[dict[str, object]]:
    """Every runnable step of every job in `ci.yml`, as data (not lines of text)."""
    document = yaml.safe_load(_read(CI_YML))
    assert isinstance(document, dict) and isinstance(document.get("jobs"), dict), (
        "ci.yml has no `jobs:` mapping — the workflow shape changed, update this pin"
    )
    steps: list[dict[str, object]] = []
    for job_name, job in document["jobs"].items():
        assert isinstance(job, dict), f"ci.yml job {job_name!r} is not a mapping"
        for step in job.get("steps") or []:
            run = step.get("run")
            if not run:
                continue
            steps.append(
                {
                    "job": job_name,
                    "name": str(step.get("name") or ""),
                    "run": str(run),
                    "continue_on_error": bool(step.get("continue-on-error", False)),
                }
            )
    return steps


def _pytest_suites_in(command: str) -> list[str]:
    """The `tests/...` targets of every `pytest <args>` line in a shell command block."""
    suites: list[str] = []
    for line in command.splitlines():
        if line.lstrip().startswith("#"):
            continue  # a recipe-line or shell comment is not an invocation
        if "pytest" not in line:
            continue
        match = re.search(r"\bpytest\s+(.*)$", line)
        if not match:
            continue
        for token in shlex.split(match.group(1), comments=True):
            if token == "tests" or token.startswith("tests/"):
                suites.append(token.rstrip("/"))
    return suites


_TARGET_LINE = re.compile(r"^([A-Za-z0-9_.$()/-]+)\s*:(?!=)\s*(.*)$")


def _make_targets(text: str) -> dict[str, tuple[list[str], list[str]]]:
    """`target -> (prerequisites, recipe lines)` for a real GNU-make file.

    Handles the three shapes that would make a substring scan lie: a target with no
    prerequisites (`backup:`), a target whose prerequisites wrap across lines (`test-unit
    test-integration \\`), and recipe lines that themselves contain `:` or `#` (a shell
    assignment, a URL, a trailing comment) — recipe lines are consumed while they are
    indented, never re-parsed as targets.
    """
    targets: dict[str, tuple[list[str], list[str]]] = {}
    current: str | None = None
    for raw_line in text.splitlines():
        if raw_line.startswith("\t"):
            if current is not None:
                targets[current][1].append(raw_line.strip())
            continue
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            current = None
            continue
        stripped = raw_line.rstrip()
        if stripped.endswith("\\") and current is not None and ".PHONY" not in stripped:
            # A prerequisite list continued on the next line (still no recipe yet).
            targets[current][0].extend(stripped[:-1].split())
            continue
        match = _TARGET_LINE.match(stripped)
        if match and "=" not in stripped.split(":")[0]:
            name, rest = match.group(1), match.group(2)
            if name == ".PHONY":
                targets.setdefault(name, ([], []))
                current = None
                continue
            prerequisites = [token for token in rest.split("#")[0].split() if token]
            targets[name] = (prerequisites, [])
            current = name
            continue
        current = None  # a variable assignment or a stray line ends any recipe
    return targets


def _make_closure(text: str, entry: str) -> set[str]:
    """`entry` plus every target reachable through its prerequisites."""
    targets = _make_targets(text)
    assert entry in targets, f"the Makefile has no `{entry}:` target — the documented entry point is gone"
    seen: set[str] = set()
    pending = [entry]
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        pending.extend(prereq for prereq in targets[name][0] if prereq in targets)
    return seen


def _make_suites(text: str, entry: str) -> dict[str, list[str]]:
    """`target -> pytest suite paths` for every target reachable from `entry`."""
    targets = _make_targets(text)
    suites: dict[str, list[str]] = {}
    for name in sorted(_make_closure(text, entry)):
        paths: list[str] = []
        for recipe_line in targets[name][1]:
            paths.extend(_pytest_suites_in(recipe_line))
        if paths:
            suites[name] = paths
    return suites


# ── 1. F-44: the parity and red-team suites run in CI ────────────────────────────────────────


def test_ci_runs_the_two_suites_that_used_to_run_nowhere() -> None:
    """F-44: a green `tests/parity` + `tests/redteam` that nothing executes is not a gate."""
    steps = _ci_steps()
    executed = {suite for step in steps for suite in _pytest_suites_in(str(step["run"]))}
    missing = [suite for suite in UNWIRED_SUITES if suite not in executed]
    assert not missing, (
        f"ci.yml runs these suites nowhere: {missing}. `make test` and CI are the two documented "
        "entry points; a suite reached by neither is 206 green tests no gate ever sees (F-44). "
        f"Suites ci.yml does run: {sorted(executed)}"
    )


def test_a_failure_in_either_wired_suite_cannot_be_swallowed() -> None:
    """A step that runs and cannot fail is the same defect with extra ceremony."""
    steps = _ci_steps()
    exemptions = {
        str(step["name"]): step
        for step in steps
        if step["continue_on_error"]
        or re.search(r"\|\|\s*true\b|;\s*true\b", str(step["run"]))
    }
    offenders = {
        name: suite
        for name, step in exemptions.items()
        for suite in _pytest_suites_in(str(step["run"]))
    }
    assert not offenders, (
        "these CI steps run a test suite but cannot fail the job "
        f"(`continue-on-error` or `|| true`): {offenders}. A suite whose exit status is "
        "discarded reds nothing — F-44's other half."
    )


def test_make_test_runs_every_suite_ci_runs() -> None:
    """`make test` is the documented local gate: it must not be a subset of CI's suites."""
    makefile = _read(MAKEFILE)
    local = {suite for suites in _make_suites(makefile, "test").values() for suite in suites}
    remote = {
        suite for step in _ci_steps() for suite in _pytest_suites_in(str(step["run"]))
    }
    missing = sorted(remote - local)
    assert not missing, (
        f"`make test` never runs {missing}, which CI does run. The local gate is a strict "
        "subset again: a developer's `make test` is green on a suite CI will red (F-44 / "
        f"A2A5-03 class). Suites `make test` reaches: {sorted(local)}"
    )
    # ... and the other direction for the two suites this seat wired: `make test` must not
    # claim a suite CI cannot see.
    unwired = [suite for suite in UNWIRED_SUITES if suite not in local or suite not in remote]
    assert not unwired, (
        f"`make test` / ci.yml still disagree about {unwired}: both entry points must run "
        "tests/parity and tests/redteam (F-44)"
    )


def test_the_wired_suite_targets_exist_on_disk_and_carry_tests() -> None:
    """A path that names no collected tests is a green step that gates nothing."""
    for suite in UNWIRED_SUITES:
        directory = REPO_ROOT / "backend" / suite
        assert directory.is_dir(), f"{suite} is named by the gates but does not exist ({directory})"
        tests = sorted(directory.glob("test_*.py"))
        assert tests, f"{suite} is wired into the gates but holds no test_*.py files"


def test_every_wired_suite_target_is_phony() -> None:
    """A file named `test-parity` would silently shadow the target on a dirty clone."""
    makefile = _read(MAKEFILE)
    phony_line = next(
        (line for line in makefile.splitlines() if line.startswith(".PHONY:")), None
    )
    assert phony_line, "the Makefile lost its .PHONY declaration"
    declared = set(phony_line.split(":", 1)[1].split())
    suite_targets = set(_make_suites(makefile, "test"))
    missing = sorted(suite_targets - declared)
    assert not missing, (
        f"{missing} run pytest but are not in .PHONY — a same-named file in the working tree "
        f"makes `make` skip them silently. .PHONY declares: {sorted(declared)}"
    )


# ── 2. A2A5-03: `make lint` runs the scope CI runs ───────────────────────────────────────────


def _lint_scopes(command: str, tool: str) -> list[frozenset[str]]:
    """Every `<tool> [check] <paths>` invocation in a command block, as a set of path targets."""
    scopes: list[frozenset[str]] = []
    for line in command.splitlines():
        if line.lstrip().startswith("#"):
            continue  # a comment that names the command is not the command
        match = re.search(rf"\b{re.escape(tool)}\s+(?:check\s+)?(.*)$", line)
        if not match or tool not in line:
            continue
        if tool == "ruff" and "check" not in line:
            continue
        tokens = [
            token
            for token in shlex.split(match.group(1), comments=True)
            if not token.startswith("-")
        ]
        if tokens:
            scopes.append(frozenset(token.rstrip("/") for token in tokens))
    return scopes


def test_make_lint_runs_exactly_the_ci_ruff_scope() -> None:
    """A2A5-03: `app/` locally vs `app/ celery_app/ tests/` in CI is a red build after a push."""
    ci_scopes = {
        scope
        for step in _ci_steps()
        for scope in _lint_scopes(str(step["run"]), "ruff")
    }
    assert len(ci_scopes) == 1, (
        f"ci.yml pins {len(ci_scopes)} different ruff scopes {sorted(map(sorted, ci_scopes))} — "
        "one lint job, one scope; the local gate cannot match two"
    )
    (ci_scope,) = ci_scopes

    makefile = _read(MAKEFILE)
    targets = _make_targets(makefile)
    local_scopes = [
        scope
        for name in sorted(_make_closure(makefile, "lint"))
        for recipe_line in targets[name][1]
        for scope in _lint_scopes(recipe_line, "ruff")
    ]
    assert local_scopes, "`make lint` runs no ruff check at all"
    assert all(scope == ci_scope for scope in local_scopes), (
        "`make lint` runs ruff on "
        f"{sorted(map(sorted, set(local_scopes)))}; CI runs ruff on {sorted(ci_scope)}. The "
        "documented local gate must be the gate CI enforces (A2A5-03)."
    )


def test_make_lint_runs_exactly_the_ci_mypy_scope() -> None:
    """The same drift, one tool over — `mypy app/` agrees today and must keep agreeing."""
    ci_scopes = {
        scope
        for step in _ci_steps()
        for scope in _lint_scopes(str(step["run"]), "mypy")
    }
    assert ci_scopes, "ci.yml no longer runs mypy — the V21 hard gate is gone"
    assert len(ci_scopes) == 1, f"ci.yml runs {len(ci_scopes)} different mypy scopes: {ci_scopes}"
    (ci_scope,) = ci_scopes

    makefile = _read(MAKEFILE)
    targets = _make_targets(makefile)
    local_scopes = {
        scope
        for name in sorted(_make_closure(makefile, "lint"))
        for recipe_line in targets[name][1]
        for scope in _lint_scopes(recipe_line, "mypy")
    }
    assert local_scopes == {ci_scope}, (
        f"`make lint` runs mypy on {sorted(map(sorted, local_scopes))}; CI runs {sorted(ci_scope)}"
    )


# ── 3. the 109-operation surface is wave A3's, not V29's ─────────────────────────────


_README_COUNT = re.compile(r"The `/api/v1` surface is (\d+)\s+(?:routes|method\+path operations)")
_COMPLETENESS_COUNT = re.compile(
    r"exposes \*\*(\d+) method\+path operations\*\* across \*\*(\d+) modules\*\*"
)


def test_readme_keeps_the_114_count_and_attributes_it_to_wave_a3() -> None:
    """V29 106; A3 phone 107th; B2 /ready+/ws/ticket 108-109; W5 inbound webhook 110th."""
    readme = _read(README)
    match = _README_COUNT.search(readme)
    assert match, "README no longer states the /api/v1 route count — update this pin"
    # WAVE-2 RE-PIN (2026-10-28): four owner-read artifact routes joined the surface —
    # OLD 110; NEW 114 (111th-114th). Recorded here like every prior count move.
    assert int(match.group(1)) == 114, (
        f"README claims {match.group(1)} /api/v1 operations; `create_app().openapi()` serves "
        "114 (measured 2026-10-28: 106 V29 + A3 phone + B2 /ready + B2 /ws/ticket + W5 inbound + wave-2 artifact routes 111-114)."
    )
    sentence = readme[match.start() : readme.index(")", match.start()) + 1]
    assert "A3" in sentence, (
        "README's 114-operation count still credits no wave A3: the measured source of the "
        f"107th route (`GET /api/v1/config/phone`) is wave A3. Sentence: {sentence!r}"
    )
    assert "V29 measurement" not in sentence, (
        "README still calls the count a 'V29 measurement' — V29's own settlement note (COMPLETENESS "
        "header) records 106 operations, so the attribution is a false receipt. "
        f"Sentence: {sentence!r}"
    )


def test_completeness_keeps_the_114_count_and_attributes_it_to_wave_a3() -> None:
    """The same correction in COMPLETENESS §2: 114 operations, 36 modules."""
    text = _read(COMPLETENESS)
    match = _COMPLETENESS_COUNT.search(text)
    assert match, "COMPLETENESS §2 count sentence changed shape — update this pin"
    assert int(match.group(1)) == 114, (
        f"COMPLETENESS claims {match.group(1)} method+path operations; the app serves 114"
        " (measured 2026-10-28 from create_app().openapi())"
    )
    assert int(match.group(2)) == 36, (
        f"COMPLETENESS claims {match.group(2)} route modules; this correction touches none"
    )
    sentence = text[match.start() : text.index(".", match.end()) + 1]
    assert "A3" in sentence, (
        "COMPLETENESS still credits the 114-operation surface without wave A3 — A3 added "
        f"`GET /api/v1/config/phone`, the 107th operation. Sentence: {sentence!r}"
    )
    assert "V29 measurement" not in sentence, (
        "COMPLETENESS still calls the count a 'V29 measurement'; V29 measured 106 (its own settlement "
        f"note says so). Sentence: {sentence!r}"
    )


# ── 4. the job that runs the suites carries the mock contract ───────────────────────────────


def test_every_ci_job_that_runs_pytest_sets_the_mock_contract() -> None:
    """README: "All CI jobs set `CLAUDE_CLI_MOCK=true` — no Claude credentials are needed".

    A suite wired into a job without it spends the run trying to spawn the real CLI; the
    contract that makes the suite hermetic lives on the JOB, so adding a step elsewhere is
    how it silently stops holding.
    """
    document = yaml.safe_load(_read(CI_YML))
    offenders: dict[str, str] = {}
    for job_name, job in document["jobs"].items():
        runs_pytest = any(
            _pytest_suites_in(str(step.get("run") or "")) for step in job.get("steps") or []
        )
        if not runs_pytest:
            continue
        env = {str(key).upper(): str(value) for key, value in (job.get("env") or {}).items()}
        if env.get("CLAUDE_CLI_MOCK") != "true":
            offenders[job_name] = env.get("CLAUDE_CLI_MOCK", "<unset>")
    assert not offenders, (
        f"CI job(s) run pytest without CLAUDE_CLI_MOCK=true: {offenders}. The suites are "
        "hermetic only under that contract (README: 'All CI jobs set CLAUDE_CLI_MOCK=true')."
    )


def test_the_readme_ci_table_names_the_suites_the_workflow_runs() -> None:
    """The README's `ci.yml` row is the documented contract; it must not understate the job."""
    row = next(
        (line for line in _read(README).splitlines() if line.startswith("| `ci.yml`")), None
    )
    assert row, "README's CI/CD table lost its `ci.yml` row"
    assert "parity" in row and "red-team" in row.lower(), (
        "README's `ci.yml` row still describes the workflow as lint + unit tests only, while "
        f"the job now runs the parity and red-team suites too. Row: {row!r}"
    )


@pytest.mark.parametrize("path", [CI_YML, MAKEFILE, README, COMPLETENESS])
def test_the_documents_this_seat_gates_all_parse(path: Path) -> None:
    """A gate that reads a broken file must fail here, not silently match nothing."""
    text = _read(path)
    assert text.strip(), f"{path.name} is empty"
    if path.suffix == ".yml":
        assert isinstance(yaml.safe_load(text), dict), f"{path.name} is not a YAML mapping"
    elif path.name == "Makefile":
        targets = _make_targets(text)
        assert {"test", "lint", "lint-backend"} <= set(targets), (
            f"{path.name} lost a documented entry target: {sorted(targets)}"
        )
    else:
        assert len(text.splitlines()) > 50, f"{path.name} looks truncated"
