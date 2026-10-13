"""Suite-end fixture sweep (T12) — the belt to the per-spec `finally` braces.

Removes every `[e2e-fixture]`-tagged row this suite can create, from BOTH stores, and nothing else:

  * ChromaDB documents FIRST (they are keyed by the sqlite rows' `chroma_id`), then the sqlite rows —
    deleting only the sqlite half would leave searchable ghost documents (memory.spec.ts says why);
  * the tagged memory rows are deleted only when EVERY one of their documents is provably present in
    the resolved store and the delete verifiably lands; otherwise the rows are KEPT and this script
    exits non-zero.  `CHROMA_PERSIST_DIR` / `CHROMA_COLLECTION` must point at the store the BACKEND
    writes to (when unset, both this script and the backend default to /app/data/chroma +
    polsia_memory, which on this host is `C:/app/data/chroma`);
  * tag-scoped predicates only — the pre-existing untagged rows (e.g. "Manual ads_management run")
    cannot match, so nothing a human created is ever deleted;
  * idempotent: run it twice; the second run deletes 0.  Two-pass proof (no stack needed):
        <venv>/python.exe e2e/_sweep-fixtures.py && <venv>/python.exe e2e/_sweep-fixtures.py

R-W4-2 restart protocol (canonical policy text: e2e/_restart-guard.sh; enforced by run-suite.sh's
pre-flight and by playwright.config.ts's globalSetup, e2e/_guard.ts): a backend process that is
LIVE while this sweep deletes documents keeps a stale in-memory chroma collection — the measured
failure is memory-search 500s on the next run (W4-ship/RECEIPT.md §3.9: phantom hits with
content=None, 32 tracebacks; endgame V2 run 3 reproduced it).  So, when this run deletes document(s)
and a backend answers /health (probing E2E_BACKEND_URL, default http://localhost:8000, then
E2E_API_BASE, default http://localhost), a marker file is written to
`<CHROMA_PERSIST_DIR>/.e2e-backend-restart-required` (override: E2E_RESTART_MARKER) and the output
says RESTART REQUIRED.  When no backend answers, any stale marker is instead CLEARED (whatever
starts later opens the store fresh).  A sweep that deletes 0 documents never touches the marker.

Run by `playwright.config.ts`'s globalTeardown (via `_helpers.ts`'s default export) after the last
test, and by hand with the backend venv python (chromadb lives there):
    <repo>/.venv/Scripts/python.exe e2e/_sweep-fixtures.py    (override the interpreter: E2E_PYTHON)
    E2E_SQLITE=... must point at the DB the BACKEND serves from; unset, the default is the
    repo-relative, gitignored <repo>/.e2e-lab.sqlite, and a missing file is a named no-op (CI's
    stack is Postgres, where this suite's fixtures are skipped by name).

Exit codes: 0 = swept (or nothing to sweep); 1 = tagged memory rows exist but their documents could
not be confirmed/removed — the rows were kept, and the run must not report success.  (Exit 0 can
still mean "restart required" — that signal is the marker file plus the SWEEP RESTART REQUIRED
lines, by design: the teardown itself did complete.)
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
import urllib.request

PREFIX = "[e2e-fixture]"
# Same env var + default as e2e/_helpers.ts (SQLITE_PATH), so both halves agree on the one DB file:
# explicit E2E_SQLITE wins, else the repo-relative, gitignored lab DB beside the repo root — never a
# machine-specific absolute path (another checkout has neither the file nor its parent directory, and
# `sqlite3.connect` would CREATE an empty DB there and then die on its missing schema).
DB = os.environ.get("E2E_SQLITE") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".e2e-lab.sqlite"
)
def _default_chroma_dir() -> str:
    """Mirror the backend's D7 resolver (app/core/chroma_client.resolve_persist_dir).

    On Windows an unset POSIX default resolves to <repo>/backend/data/chroma — NOT the
    drive-relative C:\\app\\data\\chroma (audit L3-01) — and the sweep must probe the store the
    backend actually serves from (RD7 follow-up / R-W4-2).  POSIX and containers keep the
    mounted /app/data/chroma; CHROMA_PERSIST_DIR is set explicitly there and this branch is
    only the host-side fallback.
    """
    if os.name == "nt":
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(repo_root, "backend", "data", "chroma")
    return "/app/data/chroma"


CHROMA_DIR = os.environ.get("CHROMA_PERSIST_DIR") or _default_chroma_dir()
CHROMA_COLLECTION = os.environ.get("CHROMA_COLLECTION", "polsia_memory")

# R-W4-2 restart marker + liveness probes (see the module docstring; policy: e2e/_restart-guard.sh).
# The marker is resolved once, the same way as run-suite.sh/_guard.ts resolve it: explicit
# CHROMA_PERSIST_DIR wins, else the D7-mirrored default above (all three implementations agree).
MARKER = os.environ.get("E2E_RESTART_MARKER") or os.path.join(
    os.path.abspath(CHROMA_DIR), ".e2e-backend-restart-required"
)
PROBE_URLS = (
    f"{os.environ.get('E2E_BACKEND_URL', 'http://localhost:8000')}/api/v1/health",
    f"{os.environ.get('E2E_API_BASE', 'http://localhost')}/api/v1/health",
)

# table -> (column, predicate).  prospects keys its fixture by email (outreach.spec.ts's own marker).
TAGGED = (
    ("tasks", "title", f"{PREFIX}%"),
    ("memory_entries", "title", f"{PREFIX}%"),
    ("social_posts", "content", f"{PREFIX}%"),
    ("ad_campaigns", "name", f"{PREFIX}%"),
    ("prospects", "email", "e2e-fixture-%"),
)
TABLES = [table for table, _column, _pattern in TAGGED]


def _probe_health(url: str) -> bool:
    """True iff this URL answers 2xx (never raises; 2 s timeout)."""
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            return 200 <= resp.status < 300
    except Exception:  # noqa: BLE001 - a dead probe is simply not live; never a crash
        return False


def backend_is_live() -> str | None:
    """First probe URL that answers 2xx, else None.

    Same liveness contract as run-suite.sh/_restart-guard.sh (`curl -fsS`) and _guard.ts (`fetch`
    + res.ok): any 2xx from the no-auth ``GET /api/v1/health`` (backend/app/api/v1/health.py,
    mounted at /api/v1 by backend/app/main.py:119-121) counts as "a backend is answering".
    """
    for url in PROBE_URLS:
        if _probe_health(url):
            return url
    return None


def note_restart_requirement(deleted_docs: int) -> None:
    """R-W4-2: record (marker) or clear the backend-restart requirement after a chroma delete.

    Called only when this sweep actually deleted ``deleted_docs`` > 0 documents.  A backend that is
    live right now keeps a stale in-memory collection (measured: memory-search 500s — W4 §3.9 /
    V2 run 3), so the marker tells the next suite run to demand a restart; with no live backend the
    direct delete is safe and any stale marker is cleared.  Policy text: e2e/_restart-guard.sh.
    """
    live = backend_is_live()
    if live:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S %z")
        wrote = True
        try:
            with open(MARKER, "w", encoding="utf-8") as fh:
                fh.write(
                    f"R-W4-2 restart required: the suite-end sweep deleted {deleted_docs} chroma\n"
                    f"document(s) while a backend answered at {live}.\n"
                    f"store: {CHROMA_DIR}\nstamp: {stamp}\n"
                )
        except OSError as exc:
            wrote = False
            print(f"SWEEP could not write the restart marker {MARKER}: {exc}")
        print(
            f"SWEEP RESTART REQUIRED: deleted {deleted_docs} chroma document(s) while a backend "
            f"answered at {live} — that process's in-memory chroma collection is now stale "
            f"(measured: memory-search 500s; W4 §3.9 / V2 run 3)."
        )
        if wrote:
            print(f"SWEEP marker written: {MARKER}")
            print("SWEEP → restart the backend before the next suite run: run-suite.sh's pre-flight")
            print("SWEEP   (and the suite's globalSetup) will refuse until then; attest the restart")
            print("SWEEP   with E2E_BACKEND_RESTARTED=1 — see e2e/_restart-guard.sh.")
        else:
            print("SWEEP WARNING: the marker could not be written — restart the backend before the")
            print("SWEEP   next run anyway; the guard cannot enforce what it cannot see.")
        return
    note = ", ".join(PROBE_URLS)
    if os.path.exists(MARKER):
        try:
            os.remove(MARKER)
            print(f"SWEEP no backend answered {note} — stale marker cleared: {MARKER}")
        except OSError as exc:
            print(f"SWEEP WARNING: stale marker {MARKER} could not be cleared: {exc}")
    else:
        print(
            f"SWEEP no backend answered {note} — no restart needed (nothing caches this store)."
        )


def sweep_chroma(memory_rows: list[tuple]) -> tuple[bool, str, int]:
    """Delete the tagged rows' documents; return (ok, message, deleted_docs).

    Refuses (ok=False) unless every tagged row's `chroma_id` is present in the resolved collection:
    a missing id means this is probably the wrong directory — deleting the sqlite rows then would
    strand the documents wherever the backend really wrote them.  `deleted_docs` is what this sweep
    actually removed (0 on every refusal) — the R-W4-2 marker decision keys off it.
    """
    if not os.path.isdir(CHROMA_DIR):
        return False, f"chroma dir {CHROMA_DIR} does not exist — cannot confirm/remove documents there", 0
    try:
        import chromadb  # the venv dependency; the unit-test env deliberately lacks it
    except Exception as exc:  # noqa: BLE001 - reported, never hidden
        return False, (
            f"chromadb not importable in this interpreter ({type(exc).__name__}: {exc}) — run the "
            "sweep with the backend venv python (see the module docstring)"
        ), 0
    try:
        client = chromadb.PersistentClient(path=CHROMA_DIR)
        collection = client.get_collection(CHROMA_COLLECTION)
    except Exception as exc:  # noqa: BLE001
        return False, (
            f"chroma collection {CHROMA_COLLECTION!r} unreachable in {CHROMA_DIR} "
            f"({type(exc).__name__}: {exc})"
        ), 0

    count_before = collection.count()
    ids = [chroma_id for _row_id, chroma_id, _title in memory_rows if chroma_id]
    try:
        present = collection.get(ids=ids).get("ids") if ids else []
    except Exception as exc:  # noqa: BLE001
        return False, f"chroma get(ids=...) failed ({type(exc).__name__}: {exc}) — not deleting anything", 0
    missing = sorted(set(ids) - set(present or []))
    if missing:
        return False, (
            f"{len(missing)} of {len(ids)} tagged chroma ids are NOT in {CHROMA_DIR} "
            f"(e.g. {missing[:3]}) — this is probably not the backend's store; point "
            "CHROMA_PERSIST_DIR at the one that holds them and re-run"
        ), 0

    if ids:
        collection.delete(ids=ids)
    extra: list[str] = []
    try:  # belt and braces: documents keyed by the row id metadata (covers non-standard chroma ids)
        got = collection.get(where={"memory_id": {"$in": [row_id for row_id, _c, _t in memory_rows]}})
        extra = (got or {}).get("ids") or []
        if extra:
            collection.delete(ids=extra)
    except Exception as exc:  # noqa: BLE001 - reported, never hidden
        print(f"SWEEP metadata sweep skipped: {type(exc).__name__}: {exc}")
    count_after = collection.count()
    return True, (
        f"chroma {CHROMA_DIR}: {count_before} -> {count_after} docs (deleted {len(ids)} by chroma_id, "
        f"{len(extra)} by memory_id metadata)"
    ), len(ids) + len(extra)


def main() -> int:
    if not os.path.exists(DB):
        # Nothing this suite could have seeded exists: the fixture writer targets the SQLite lab DB,
        # and the CI stack is Postgres (E2E_SQLITE unset there). A NAMED no-op, not a failure — the
        # throw-on-failure contract below still covers every run that actually wrote something.
        print(
            f"SWEEP db={DB} is not present — nothing to sweep "
            "(fixtures need the SQLite lab: set E2E_SQLITE to the DB the backend serves from)."
        )
        return 0
    con = sqlite3.connect(DB)
    con.execute("PRAGMA busy_timeout = 10000")
    try:
        counts_before = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES}
    except sqlite3.Error as exc:
        # The file exists but is not the backend's schema (a stray empty DB, a wrong pointer): refuse
        # loudly instead of reporting a clean sweep over a file this suite never wrote to.
        print(
            f"SWEEP REFUSED: {DB} exists but its schema is not the backend's "
            f"({type(exc).__name__}: {exc}) — point E2E_SQLITE at the DB the backend serves from; "
            "nothing was deleted."
        )
        con.close()
        return 1
    memory_rows = con.execute(
        "SELECT id, chroma_id, title FROM memory_entries WHERE title LIKE ?", (f"{PREFIX}%",)
    ).fetchall()

    print(f"SWEEP db={DB}")
    print(f"SWEEP tagged memory rows: {len(memory_rows)}")
    for row_id, chroma_id, title in memory_rows:
        print(f"  mem  id={row_id} chroma_id={chroma_id} title={title!r}")

    exit_code = 0
    memory_ok = True
    if memory_rows:
        memory_ok, detail, deleted_docs = sweep_chroma(memory_rows)
        print(f"SWEEP {detail}")
        if not memory_ok:
            exit_code = 1
            print(
                "SWEEP REFUSED to delete the tagged memory_entries rows: their documents could not be "
                "confirmed/removed in ChromaDB (deleting the sqlite half alone would leave ghost search "
                "hits).  Fix the store pointer above and re-run — the rows are still here."
            )
        else:
            cur = con.execute("DELETE FROM memory_entries WHERE title LIKE ?", (f"{PREFIX}%",))
            print(f"SWEEP deleted memory_entries rows: {cur.rowcount}")
            if deleted_docs:
                # R-W4-2: documents were removed from a live store — decide the restart marker.
                note_restart_requirement(deleted_docs)

    for table, column, pattern in TAGGED:
        if table == "memory_entries":
            continue
        cur = con.execute(f"DELETE FROM {table} WHERE {column} LIKE ?", (pattern,))
        print(f"SWEEP deleted {table} rows: {cur.rowcount}")
    con.commit()
    counts_after = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES}
    print(f"SWEEP counts before: {counts_before}")
    print(f"SWEEP counts after:  {counts_after}")
    con.close()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
