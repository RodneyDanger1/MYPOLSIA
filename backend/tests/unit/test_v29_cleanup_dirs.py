"""V29 / L1 T1.5 — drill SITE directories: prefix allowlist + quarantine rename.

WHY THIS FILE EXISTS
--------------------
`backend/scripts/cleanup_drill_data.py` removed tagged ROWS and only COUNTED the drill site
directories under `backend/data/sites` (they stayed on disk forever).  The naive completion —
"reuse `TAG_RE` on the path" — is wrong twice over, and this module pins both halves:

* `TAG_RE` (`\\[drill\\]|\\bdrill\\b`) misses `v24-live-*` / `v25-drive-*`, which are drill sites, and
  it would MATCH a real site whose name happens to be "drill";
* a delete is not needed at all.  `--apply` renames each allowlisted directory into
  `<backend>/data/trash/v29-<stamp>/` — beside `sites/`, never inside it, so the `StaticFiles`
  mount can never serve a quarantined site — and the operator can put it back.

The contract pinned below:

* only a DIRECT child of the sites root whose name starts with `SITE_DIR_PREFIXES` is eligible;
* a symlink or a Windows junction that carries an allowlisted name is REFUSED, never followed
  (its target must still hold its files after the run);
* the rename happens AFTER the row transaction commits — the ordering is asserted, not assumed;
* the row transaction is untouched by this lane's change: `_delete` still refuses a stale plan;
* a dry run counts and deletes nothing.

DOUBLES: the sites root (a temp dir monkeypatched onto the module's `BACKEND`) and the database
engine (a labelled fake that records `commit`, so the AFTER-THE-COMMIT ordering is measurable).
The script's own `_plan` / `_delete` / `_quarantine_site_dirs` / `main` run for real.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]
DB_URL = "sqlite+aiosqlite:///./v29-cleanup-probe.db"

ALLOWLISTED = ("v24-live-1", "v25-drive-2", "v26-drill-3")


# ── the module under test ─────────────────────────────────────────────────────────────────────


@pytest.fixture()
def cleanup():
    """The script module, imported the way its CLI entry point sees it."""
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    import scripts.cleanup_drill_data as module

    return module


# ── the fake database (records commit, answers every query with nothing) ─────────────────────


class _Result:
    def __init__(self, rows=None, scalar=None) -> None:
        self._rows = list(rows or [])
        self._scalar = scalar

    def fetchall(self):
        return list(self._rows)

    def scalar(self):
        return self._scalar

    def scalar_one(self):
        return self._scalar


class _State:
    """The one fact this fake exists to prove: whether the row transaction COMMITTED."""

    def __init__(self) -> None:
        self.events: list[str] = []

    @property
    def committed(self) -> bool:
        return "commit" in self.events


class _Conn:
    def __init__(self, state: _State) -> None:
        self.state = state
        self.rowcount = 0

    async def execute(self, statement, params=None):  # noqa: ANN001 — SQLAlchemy-shaped
        sql = str(statement)
        if "alembic_version" in sql:
            return _Result(scalar="0016")
        if sql.strip().upper().startswith("SELECT COUNT(*)"):
            return _Result(scalar=0)
        return _Result()


class _Ctx:
    def __init__(self, state: _State, *, begin: bool) -> None:
        self.state = state
        self.begin = begin
        self.conn = _Conn(state)

    async def __aenter__(self):
        self.state.events.append("begin" if self.begin else "connect")
        return self.conn

    async def __aexit__(self, *exc_info):
        if self.begin and exc_info[0] is None:
            self.state.events.append("commit")
        return False


class _Engine:
    def __init__(self, state: _State) -> None:
        self.state = state

    def connect(self):
        return _Ctx(self.state, begin=False)

    def begin(self):
        return _Ctx(self.state, begin=True)

    async def dispose(self):
        return None


@pytest.fixture()
def db_state(monkeypatch):
    """The fake engine, installed on the name `_run` imports at call time."""
    state = _State()
    monkeypatch.setattr(
        "sqlalchemy.ext.asyncio.create_async_engine", lambda url: _Engine(state), raising=True
    )
    return state


# ── the temp sites root ──────────────────────────────────────────────────────────────────────


@pytest.fixture()
def sites_root(tmp_path, monkeypatch, cleanup):
    """`<tmp>/data/sites`, with the module's `BACKEND` pointed at `<tmp>`."""
    monkeypatch.setattr(cleanup, "BACKEND", tmp_path, raising=True)
    root = tmp_path / "data" / "sites"
    root.mkdir(parents=True)
    return root


def _seed(root: Path, names=ALLOWLISTED) -> None:
    for name in names:
        directory = root / name
        directory.mkdir()
        (directory / "index.html").write_text(f"<h1>{name}</h1>", encoding="utf-8")


def _trash_batches(root: Path) -> list[Path]:
    trash = root.parent / "trash"
    if not trash.is_dir():
        return []
    return sorted(path for path in trash.iterdir() if path.name.startswith("v29-"))


def _residual_line(stdout: str, needle: str) -> str:
    for line in stdout.splitlines():
        if needle in line:
            return line
    raise AssertionError(f"no line containing {needle!r} in:\n{stdout}")


def _make_junction(link: Path, target: Path) -> bool:
    """A Windows directory junction needs no elevation; a POSIX symlink needs no extra right."""
    try:
        if os.name == "nt":
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True,
                text=True,
            )
            return result.returncode == 0 and link.exists()
        os.symlink(target, link, target_is_directory=True)
        return True
    except OSError:
        return False


# ── 1. eligibility: the allowlist, not TAG_RE ───────────────────────────────────────────────


def test_only_allowlisted_direct_children_are_eligible(cleanup, sites_root):
    """`v24-live-*`/`v25-drive-*`/`v26-drill-*` yes; a real site, a file and a "drill" site no."""
    _seed(sites_root)
    (sites_root / "real-site").mkdir()
    (sites_root / "notes.txt").write_text("keep me", encoding="utf-8")
    (sites_root / "drill").mkdir()  # a REAL site named "drill" — TAG_RE would match this

    eligible = [path.name for path in cleanup._site_dir_candidates()[0]]

    assert eligible == sorted(ALLOWLISTED), (
        "eligibility is the explicit prefix allowlist: TAG_RE misses v24-live/v25-drive and "
        f"matches a site called 'drill' (got {eligible})"
    )


def test_the_plan_carries_paths_and_the_residual_carries_the_count(cleanup, sites_root):
    """`plan["site_dirs"]` is PATHS (what apply renames); `residual["site_dirs"]` is the count."""
    _seed(sites_root)
    (sites_root / "real-site").mkdir()

    plan = asyncio.run(cleanup._plan(_Conn(_State())))

    assert [Path(path).name for path in plan["site_dirs"]] == sorted(ALLOWLISTED)
    assert plan["residual"]["site_dirs"] == len(ALLOWLISTED)
    assert plan["residual"]["site_dirs_refused"] == 0


# ── 2. dry run: counts, deletes nothing, writes no trash ────────────────────────────────────


def test_dry_run_reports_the_site_dir_count_and_moves_nothing(
    cleanup, sites_root, db_state, capsys
):
    _seed(sites_root)
    (sites_root / "real-site").mkdir()

    exit_code = cleanup.main(["--database-url", DB_URL])
    stdout = capsys.readouterr().out

    assert exit_code == 0, stdout
    assert "DRY RUN" in stdout and "nothing was deleted" in stdout
    assert _residual_line(stdout, "drill site directories").split()[-1] == "3"
    assert sorted(path.name for path in sites_root.iterdir()) == [
        "real-site", "v24-live-1", "v25-drive-2", "v26-drill-3",
    ]
    assert _trash_batches(sites_root) == [], "a dry run must not create the quarantine batch"


# ── 3. apply: rename into data/trash/v29-<stamp>, AFTER the row transaction commits ────────


def test_apply_quarantines_the_allowlisted_dirs_into_a_v29_batch(
    cleanup, sites_root, db_state, capsys
):
    _seed(sites_root)
    (sites_root / "real-site").mkdir()
    (sites_root / "real-site" / "index.html").write_text("real", encoding="utf-8")
    (sites_root / "notes.txt").write_text("keep me", encoding="utf-8")

    exit_code = cleanup.main(["--apply", "--database-url", DB_URL])
    stdout = capsys.readouterr().out

    assert exit_code == 0, stdout
    assert "APPLIED — deleted 0 row(s); quarantined 3 drill site director(y|ies)" in stdout

    batches = _trash_batches(sites_root)
    assert len(batches) == 1, f"exactly one v29 batch was expected, got {batches}"
    trash = sites_root.parent / "trash"
    assert trash.is_dir() and trash.parent == sites_root.parent, "trash sits BESIDE sites/"
    assert sorted(path.name for path in batches[0].iterdir()) == sorted(ALLOWLISTED)
    assert (batches[0] / "v24-live-1" / "index.html").is_file(), (
        "the rename must carry the site's own files into the quarantine batch"
    )
    assert sorted(path.name for path in sites_root.iterdir()) == ["notes.txt", "real-site"]
    assert (sites_root / "real-site" / "index.html").read_text(encoding="utf-8") == "real"


def test_the_quarantine_runs_after_the_row_transaction_commits(
    cleanup, sites_root, db_state, capsys, monkeypatch
):
    """The rows are the irreversible half: they must be committed before a directory moves."""
    _seed(sites_root)
    seen: dict[str, object] = {}
    real = cleanup._quarantine_site_dirs

    def spy(paths, **kwargs):
        seen["committed"] = db_state.committed
        seen["events"] = list(db_state.events)
        return real(paths, **kwargs)

    monkeypatch.setattr(cleanup, "_quarantine_site_dirs", spy, raising=True)

    exit_code = cleanup.main(["--apply", "--database-url", DB_URL])
    stdout = capsys.readouterr().out

    assert exit_code == 0, stdout
    assert seen["committed"] is True, (
        "the site directories were quarantined before the delete transaction committed "
        f"(engine events: {seen['events']})"
    )
    assert seen["events"] == ["connect", "begin", "commit"], (
        "the plan is read on a plain connection, the deletes ride ONE transaction, and the "
        "rename happens after it commits — never a directory move inside the row transaction"
    )


# ── 4. a link that carries an allowlisted name is refused, never followed ───────────────────


def test_a_link_with_an_allowlisted_name_is_refused_and_never_followed(
    cleanup, sites_root, db_state, capsys
):
    outside = sites_root.parent / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("do not touch", encoding="utf-8")
    link = sites_root / "v26-drill-link"

    if not _make_junction(link, outside):
        pytest.skip("this host cannot create a symlink/junction (no privilege)")

    eligible = [path.name for path in cleanup._site_dir_candidates()[0]]
    assert "v26-drill-link" not in eligible, "a link is never an eligible site directory"

    exit_code = cleanup.main(["--apply", "--database-url", DB_URL])
    stdout = capsys.readouterr().out

    assert exit_code == 0, stdout
    assert "quarantined 0 drill site director(y|ies)" in stdout
    assert "REFUSED" in stdout and "v26-drill-link" in stdout, (
        "a refused link must be NAMED in the run's output, not silently ignored"
    )
    assert link.is_dir(), "the link itself was moved; it must be left exactly where it is"
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "do not touch"
    assert _trash_batches(sites_root) == [], "nothing eligible → no empty batch is created"
