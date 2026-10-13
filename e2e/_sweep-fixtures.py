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

Run by `playwright.config.ts`'s globalTeardown (via `_helpers.ts`'s default export) after the last
test, and by hand with the backend venv python (chromadb lives there):
    C:/Users/Riley/Desktop/Polsia/.venv/Scripts/python.exe e2e/_sweep-fixtures.py

Exit codes: 0 = swept (or nothing to sweep); 1 = tagged memory rows exist but their documents could
not be confirmed/removed — the rows were kept, and the run must not report success.
"""
from __future__ import annotations

import os
import sqlite3
import sys

PREFIX = "[e2e-fixture]"
# Same env var + default as e2e/_helpers.ts (SQLITE_PATH), so both halves agree on the one DB file.
DB = os.environ.get("E2E_SQLITE", "C:/Users/Riley/Desktop/polsia-work/build/e2e.sqlite")
CHROMA_DIR = os.environ.get("CHROMA_PERSIST_DIR", "/app/data/chroma")
CHROMA_COLLECTION = os.environ.get("CHROMA_COLLECTION", "polsia_memory")

# table -> (column, predicate).  prospects keys its fixture by email (outreach.spec.ts's own marker).
TAGGED = (
    ("tasks", "title", f"{PREFIX}%"),
    ("memory_entries", "title", f"{PREFIX}%"),
    ("social_posts", "content", f"{PREFIX}%"),
    ("ad_campaigns", "name", f"{PREFIX}%"),
    ("prospects", "email", "e2e-fixture-%"),
)
TABLES = [table for table, _column, _pattern in TAGGED]


def sweep_chroma(memory_rows: list[tuple]) -> tuple[bool, str]:
    """Delete the tagged rows' documents; return (ok, message).

    Refuses (ok=False) unless every tagged row's `chroma_id` is present in the resolved collection:
    a missing id means this is probably the wrong directory — deleting the sqlite rows then would
    strand the documents wherever the backend really wrote them.
    """
    if not os.path.isdir(CHROMA_DIR):
        return False, f"chroma dir {CHROMA_DIR} does not exist — cannot confirm/remove documents there"
    try:
        import chromadb  # the venv dependency; the unit-test env deliberately lacks it
    except Exception as exc:  # noqa: BLE001 - reported, never hidden
        return False, (
            f"chromadb not importable in this interpreter ({type(exc).__name__}: {exc}) — run the "
            "sweep with the backend venv python (see the module docstring)"
        )
    try:
        client = chromadb.PersistentClient(path=CHROMA_DIR)
        collection = client.get_collection(CHROMA_COLLECTION)
    except Exception as exc:  # noqa: BLE001
        return False, (
            f"chroma collection {CHROMA_COLLECTION!r} unreachable in {CHROMA_DIR} "
            f"({type(exc).__name__}: {exc})"
        )

    count_before = collection.count()
    ids = [chroma_id for _row_id, chroma_id, _title in memory_rows if chroma_id]
    try:
        present = collection.get(ids=ids).get("ids") if ids else []
    except Exception as exc:  # noqa: BLE001
        return False, f"chroma get(ids=...) failed ({type(exc).__name__}: {exc}) — not deleting anything"
    missing = sorted(set(ids) - set(present or []))
    if missing:
        return False, (
            f"{len(missing)} of {len(ids)} tagged chroma ids are NOT in {CHROMA_DIR} "
            f"(e.g. {missing[:3]}) — this is probably not the backend's store; point "
            "CHROMA_PERSIST_DIR at the one that holds them and re-run"
        )

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
    )


def main() -> int:
    con = sqlite3.connect(DB)
    con.execute("PRAGMA busy_timeout = 10000")
    counts_before = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES}
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
        memory_ok, detail = sweep_chroma(memory_rows)
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
