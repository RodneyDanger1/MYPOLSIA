"""B2 / seat C6 — CI and documentation truth, measured from the tree.

Why this module exists (A2A5-02 / F-06 / F-08 / F-09):  the repo's only docs-truth gate
(`backend/tests/unit/test_v28_adversarial.py`) opens exactly two files while its docstring claims
"the docs", and it never reads the docs' own proof idiom — a `file.py:NNN` coordinate.  So the
corpus could rot for dozens of commits while a green suite told the operator the docs were pinned.

What this module does instead:

* **Walks every markdown file in the repo** (not a hand-picked pair) and resolves every
  `path/to/file.py:NNN` coordinate to a real file and a line that exists and carries code — not a
  blank line, a bare `#`, a `}` or a separator.  That is a LIVENESS pin, not a semantic one: it
  proves the coordinate still points at code, never that the sentence around it is true.  The
  latter needs a reader; saying otherwise would be the same class of lie this file exists to stop.
* **The security map (`docs/SUPERVISION.md`) may not carry line numbers at all.**  It is cited by
  humans during incidents and its line numbers rotted wholesale (12 of 16); its anchors are
  `path.py::symbol`, resolved here with `ast`, so moving a function cannot silently break the map.
* **A doc that names a caller is checked against that caller's source** (F-08): SUPERVISION says
  `tool_registry.execute_tool` calls `decide()`, so the test reads `execute_tool` and looks for the
  call, and F-09's chat-rail sentence is checked against a deterministic-router behaviour.
* **The e2e spec's environment demands are pinned** (F-06): every dashboard test must pin the
  phone-config answer (the page mounts the phone card unconditionally), and the signals test may
  demand links only when the live payload actually carried items — the API is allowed to answer
  with an honest empty, which the page renders as "No headlines right now".

Every number is measured here.  Nothing is copied from another doc.
"""
from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND = REPO_ROOT / "backend"

#: Directories that hold vendored/generated content, never product docs.
_SKIP_DIRS = {
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    ".next",
    ".pytest_cache",
    "dist",
    "build",
    "playwright-report",
    "test-results",
}

#: `path/to/module.py:123` or a range `path/to/module.py:123-145`.
_COORDINATE = re.compile(r"`?([A-Za-z0-9_][A-Za-z0-9_./-]*\.py):(\d+)(?:-(\d+))?`?")

#: `path/to/module.py::symbol` — the rot-proof anchor form SUPERVISION.md uses.
_SYMBOL = re.compile(r"([A-Za-z0-9_][A-Za-z0-9_./-]*\.py)::([A-Za-z_][A-Za-z0-9_]*)")

_TRIVIAL_LINES = {"}", ")", "]", "#", '"""', "'''", "---", "===", "...", "pass"}


def _walk(suffix: str) -> list[Path]:
    """Every TRACKED file with `suffix`, with vendored trees pruned (never descended).

    `git ls-files` is the definition of "the docs this repo ships": the worktree also holds
    gitignored campaign history (e.g. `wave-v10/`), which is a receipt of past audits, not a
    present-tense document.  A source tarball has no such directories, so the disk walk is the
    fallback when git is not available.
    """
    import subprocess

    try:
        listed = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-files", "-z", f"*{suffix}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        listed = ""

    if listed:
        paths = [REPO_ROOT / rel for rel in listed.split("\0") if rel]
    else:
        import os

        paths = []
        for root, dirnames, filenames in os.walk(REPO_ROOT):
            dirnames[:] = [name for name in dirnames if name not in _SKIP_DIRS]
            root_path = Path(root)
            paths.extend(root_path / name for name in filenames if name.endswith(suffix))
    return sorted(path for path in paths if path.is_file())


def _docs() -> list[Path]:
    return _walk(".md")


def _py_index() -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = {}
    for path in _walk(".py"):
        index.setdefault(path.name, []).append(path)
    return index


def _resolve(rel: str, index: dict[str, list[Path]]) -> tuple[Path | None, str]:
    """Resolve a doc's module path: repo root, then `backend/`, then a unique basename."""
    for base in (REPO_ROOT, BACKEND):
        candidate = base / rel
        if candidate.is_file():
            return candidate, "path"
    hits = index.get(Path(rel).name, [])
    if len(hits) == 1:
        return hits[0], "basename"
    if len(hits) > 1:
        return None, f"ambiguous ({len(hits)} files named {Path(rel).name})"
    return None, "missing"


def _trivial(line: str) -> bool:
    stripped = line.strip()
    if not stripped or stripped in _TRIVIAL_LINES:
        return True
    return set(stripped) <= {"-", "=", "#", "*", " "}


def _module_names(path: Path) -> set[str]:
    """Every name a doc may anchor to: defs, classes and module-level assignments."""
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
    for node in tree.body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
    return names


def _doc_line_number(text: str, offset: int) -> int:
    return text[:offset].count("\n") + 1


# --------------------------------------------------------------------------- #
# A2A5-02 — the corpus the gate claims, actually walked.
# --------------------------------------------------------------------------- #
def test_the_docs_gate_walks_the_whole_corpus():
    """The gate's scope is the repo's tracked markdown, not the two files the old gate opened."""
    docs = _docs()
    names = {path.relative_to(REPO_ROOT).as_posix() for path in docs}

    # The present-tense documents the register named (A2A5-02 / F-45). A scope that stops
    # including one of these is the defect this test exists to prevent, not a tidy-up.
    expected = {
        "README.md",
        "COMPLETENESS.md",
        "COMPARISON-MERIDIAN.md",
        "CLAUDE.md",
        "USAGE.md",
        "NEXTSTEPS.md",
        "RELEASE-CHECKLIST.md",
        "docs/SUPERVISION.md",
        "docs/ACCEPTANCE_TESTING.md",
    }
    missing = sorted(expected - names)
    assert not missing, f"the docs-truth corpus no longer covers: {', '.join(missing)}"
    assert len(docs) >= 10, f"only {len(docs)} markdown files found — the walk is broken"

    # And the corpus really contains coordinates to check (a corpus of prose-only docs would
    # make every assertion below vacuously green).
    with_coordinates = [path for path in docs if _COORDINATE.search(path.read_text(encoding="utf-8"))]
    assert len(with_coordinates) >= 3, (
        "fewer than three markdown files carry a `file.py:NNN` coordinate — either the docs lost "
        f"their proof idiom or this walk is blind (found: {[p.name for p in with_coordinates]})"
    )


def test_every_doc_coordinate_resolves_to_a_line_of_code():
    """Every `file.py:NNN` in every doc must land on an existing, non-empty line of code."""
    index = _py_index()
    offenders: list[str] = []
    total = 0

    for doc in _docs():
        text = doc.read_text(encoding="utf-8", errors="replace")
        for match in _COORDINATE.finditer(text):
            rel, first = match.group(1), int(match.group(2))
            last = int(match.group(3)) if match.group(3) else first
            total += 1
            where = f"{doc.relative_to(REPO_ROOT).as_posix()}:{_doc_line_number(text, match.start())}"
            target, how = _resolve(rel, index)
            if target is None:
                offenders.append(f"{where} -> {rel}:{first} ({how})")
                continue
            lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
            if not (0 < first <= len(lines)) or not (0 < last <= len(lines)):
                offenders.append(
                    f"{where} -> {rel}:{first}-{last} (out of range: {len(lines)} lines)"
                )
                continue
            if _trivial(lines[first - 1]):
                offenders.append(
                    f"{where} -> {rel}:{first} lands on {lines[first - 1]!r} [{how}]"
                )

    assert total >= 20, f"only {total} coordinates found across {len(_docs())} docs — walk is broken"
    assert not offenders, (
        "doc coordinates no longer point at code; re-anchor them (or use a `::symbol` anchor "
        "where the line will move):\n" + "\n".join(offenders)
    )


# --------------------------------------------------------------------------- #
# A6-02 (the same rot class) — SUPERVISION.md anchors by symbol, not by line.
# --------------------------------------------------------------------------- #
def test_supervision_map_anchors_by_symbol_and_never_by_line_number():
    supervision = REPO_ROOT / "docs" / "SUPERVISION.md"
    text = supervision.read_text(encoding="utf-8")

    stale = [
        match.group(0)
        for match in _COORDINATE.finditer(text)
        if match.group(1).endswith(".py")
    ]
    assert not stale, (
        "SUPERVISION.md is the security map and its line numbers rotted wholesale (12 of 16 "
        "wrong before wave B2). Anchor by symbol (`path.py::def_name`) instead of a line: "
        f"{stale}"
    )

    index = _py_index()
    anchors = list(_SYMBOL.finditer(text))
    assert len(anchors) >= 10, (
        f"only {len(anchors)} symbol anchors in SUPERVISION.md — one per tier leg is the minimum "
        "for a map a human follows during an incident"
    )
    unresolved: list[str] = []
    for match in anchors:
        rel, symbol = match.group(1), match.group(2)
        target, how = _resolve(rel, index)
        if target is None:
            unresolved.append(f"{rel}::{symbol} ({how})")
            continue
        if symbol not in _module_names(target):
            unresolved.append(f"{rel}::{symbol} (no such def/class/assignment)")
    assert not unresolved, "SUPERVISION.md names symbols the tree does not have:\n" + "\n".join(
        unresolved
    )


# --------------------------------------------------------------------------- #
# F-08 — the tool leg's caller is named, and the named caller really calls it.
# --------------------------------------------------------------------------- #
def test_supervision_names_the_tool_leg_caller_and_the_code_agrees():
    text = (REPO_ROOT / "docs" / "SUPERVISION.md").read_text(encoding="utf-8")

    assert "no production caller" not in text, (
        "SUPERVISION.md again says decide()'s tool leg has no production caller; "
        "`tool_registry.execute_tool` calls decide() for every registry tool call"
    )
    assert "app/agents/tool_registry.py::execute_tool" in text, (
        "SUPERVISION.md must name the tool leg's production caller with its anchor"
    )

    from app.agents import tool_registry

    # The doc names a two-hop chain: execute_tool -> _execute -> decide(). Both hops are checked,
    # so the sentence cannot outlive either one.
    assert "_execute(" in inspect.getsource(tool_registry.execute_tool), (
        "the doc says `execute_tool` reaches decide() through `_execute`, but the function no "
        "longer calls it"
    )
    assert "decide(" in inspect.getsource(tool_registry._execute), (
        "the doc says `_execute` calls decide(), and it does not — one of the two is now false"
    )


# --------------------------------------------------------------------------- #
# F-09 — the README describes the chat rail as what it is: a regex router.
# --------------------------------------------------------------------------- #
def test_readme_describes_the_chat_rail_as_the_router_it_is():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    assert "the chat rail talks to the orchestrator" not in readme, (
        "README.md again claims the chat rail talks to the orchestrator; "
        "`chat_service.classify_message` is a regex/stem router with no model call"
    )
    assert "backend/app/services/chat_service.py::classify_message" in readme, (
        "README.md must anchor the chat-rail description to the router that implements it"
    )

    from app.services import chat_service

    # The behaviour the sentence claims: one message -> one rule, and money/external asks are
    # refused instead of routed to an agent. A model-backed rail could not promise either.
    first = chat_service.classify_message("what is my MRR")
    second = chat_service.classify_message("what is my MRR")
    assert first == second, "classify_message is not deterministic — the README's claim is false"
    refused = chat_service.classify_message("pay the invoice for the customer")
    assert refused.kind == "refused", (
        f"a money ask classified as {refused.kind!r}: the rail would queue what the README says "
        "it refuses by name"
    )
    assert refused.rule == "money_external"


# --------------------------------------------------------------------------- #
# F-45 — a count the docs state in the present tense is measured here.
# --------------------------------------------------------------------------- #
def test_acceptance_tables_count_matches_the_models():
    text = (REPO_ROOT / "docs" / "ACCEPTANCE_TESTING.md").read_text(encoding="utf-8")
    match = re.search(r"all (\d+) tables present", text)
    assert match, "ACCEPTANCE_TESTING.md no longer states the table count — update this pin"
    claimed = int(match.group(1))
    measured = sum(
        len(re.findall(r"__tablename__\s*=", path.read_text(encoding="utf-8")))
        for path in (BACKEND / "app" / "models").glob("*.py")
    )
    assert claimed == measured, (
        f"docs/ACCEPTANCE_TESTING.md claims all {claimed} tables are created by `make init-db`; "
        f"backend/app/models declares {measured} `__tablename__` tables"
    )


# --------------------------------------------------------------------------- #
# F-06 — the e2e spec may only demand the environment the page promises.
# --------------------------------------------------------------------------- #
def _spec_text() -> str:
    return (REPO_ROOT / "e2e" / "v29-followup-e.spec.ts").read_text(encoding="utf-8")


def test_every_dashboard_e2e_test_pins_the_phone_config_answer():
    """/dashboard mounts the phone card unconditionally, so every visit must pin that read.

    The card asks `GET /api/v1/config/phone` first (200 in every deployment) and only then the
    503-only `/phone/summary`. On a runner with no phone store the summary refuses, and the
    browser's resource-load line lands in `watchConsole` — a console error inside a test about
    signals. Pinning the config answer (or stubbing it outright) is what makes the file
    environment-independent; a new dashboard test that skips it re-opens the CI red.
    """
    text = _spec_text()
    chunks = re.split(r"\n  test\(", text)
    dashboard_chunks = [chunk for chunk in chunks if 'page.goto("/dashboard")' in chunk]
    assert len(dashboard_chunks) >= 3, (
        f"found {len(dashboard_chunks)} dashboard tests — this pin expects the signals pair plus "
        "the phone-refusal test; update it with the spec"
    )
    unpinned = [
        chunk.splitlines()[0][:90]
        for chunk in dashboard_chunks
        # The config answer may be pinned by the shared helper or stubbed inline with the route
        # regex (`config\/phone`), so both spellings count as "this test owns the answer".
        if "pinNoPhoneStore(page)" not in chunk
        and not ("config" in chunk and "phone" in chunk)
    ]
    assert not unpinned, (
        "these dashboard tests mount the phone card without pinning its config answer, so the "
        "runner's own phone store decides whether the console stays clean:\n"
        + "\n".join(unpinned)
    )


def test_the_signals_spec_demands_links_only_when_the_payload_has_items():
    """The live feed is allowed to answer with an honest empty — the spec must allow it too."""
    text = _spec_text()
    signals = text.split('test("Signals widget: live payload shape')[1]
    signals = signals.split("\n  test(")[0]

    assert "if (body.items.length > 0) {" in signals, (
        "the signals test asserts links without asking the payload whether it carried any: a "
        "cold or refused HN/TechCrunch feed then fails a page that behaved correctly"
    )
    assert "expect(linkCount).toBeGreaterThan(0);" not in signals, (
        "the old unconditional link demand is back — it fails on the page's own honest empty"
    )
    guard = signals.index("if (body.items.length > 0) {")
    demand = signals.index(").toBeGreaterThan(0);")
    assert guard < demand, "the link demand is not inside the items branch"
    assert "No headlines right now" in signals[guard:], (
        "the zero-item branch must assert the card's honest empty state, not just skip the links"
    )
