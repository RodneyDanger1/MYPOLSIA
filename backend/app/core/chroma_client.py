"""ChromaDB access.

`chromadb` is NOT installed in the unit-test environment, so it is imported lazily
inside the functions (CLAUDE.md: "Don't import chromadb ... at module level").
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from app.config import settings

# app/core modules log through the stdlib root configuration (events/retry/timeutil convention).
logger = logging.getLogger(__name__)

_client: Any | None = None
_collection: Any | None = None


def _fallback_persist_dir() -> Path:
    """`<repo>/backend/data/chroma`, anchored at THIS file — never at the process cwd.

    The local, writable home for the vector store when the container default is unusable on this
    host (D7; audit L3-01).  `backend/data/` is already the repo's runtime-state directory
    (secret_store writes `backend/data/deepseek_api_key.json` there).
    """
    return Path(__file__).resolve().parents[2] / "data" / "chroma"


def resolve_persist_dir(configured: str | None = None) -> str:
    """Resolve the Chroma store path for THIS platform (D7; audit L3-01) — called by `get_client`.

    `configured` defaults to `settings.chroma_persist_dir` (the consumer path; tests pass it
    directly).  Frozen semantics (registry D7):

      * POSIX hosts, and relative paths on any host: unchanged, no log;
      * Windows + POSIX-absolute + `CHROMA_PERSIST_DIR` present in the process environment: the
        operator's explicit choice — honored verbatim, one INFO.  docker-compose injects env_file
        into the container env (the image keeps `/app/data/chroma`), and e2e/_sweep-fixtures.py
        reads the same variable directly — this branch keeps that explicit-env contract intact;
      * Windows + POSIX-absolute + env unset: one WARNING naming the drive-relative hazard, then
        resolve to `<backend>/data/chroma` (created on demand, as chroma would create its store).

    Windows has no silent third option: an un-set POSIX default resolves DRIVE-relative
    (`/app/data/chroma` -> `C:\\app\\data\\chroma`, measured present on this host, audit L3-01)
    and a store written under a different cwd is invisible to the next run.
    """
    path = settings.chroma_persist_dir if configured is None else configured
    if os.name != "nt" or not str(path).startswith("/"):
        return str(path)
    if "CHROMA_PERSIST_DIR" in os.environ:
        logger.info(
            "chroma_persist_dir %r set explicitly via CHROMA_PERSIST_DIR — honored verbatim (D7)",
            path,
        )
        return str(path)
    fallback = _fallback_persist_dir()
    logger.warning(
        "chroma_persist_dir %r is POSIX-absolute on Windows: without an explicit "
        "CHROMA_PERSIST_DIR it resolves DRIVE-RELATIVE (e.g. 'C:\\app\\data\\chroma') and a "
        "store written under another cwd is invisible. Resolving to %r instead; set "
        "CHROMA_PERSIST_DIR explicitly to keep a specific path (audit L3-01 / registry D7).",
        path,
        str(fallback),
    )
    fallback.mkdir(parents=True, exist_ok=True)
    return str(fallback)


def get_client() -> Any:
    global _client
    if _client is None:
        import chromadb  # lazy

        _client = chromadb.PersistentClient(path=resolve_persist_dir())
    return _client


def get_collection() -> Any:
    global _collection
    if _collection is None:
        client = get_client()
        _collection = client.get_or_create_collection(
            name=settings.chroma_collection,
            metadata={"hnsw:space": "cosine"},
        )
    return _collection


def reset_cache() -> None:
    """Drop the cached chroma client/collection handles (module-level `_client`/`_collection`).

    ZERO CALLERS (R25 dead-helper sweep, 2026-10-13; raw grep evidence:
    polsia-work/build/R25-deadhelpers/raw/grep_reset_cache.txt) — nothing in app/,
    celery_app/, scripts/, tests/ or conftest calls it; not production AND not
    test-support either (tests reset the handles by monkeypatching `_client`/`_collection`
    to None — tests/unit/test_chroma_path_resolver.py:119-120 — or patch
    `get_collection`).  Intended as the maintainer reset for the lazy chroma handles.
    Removal vs adoption is an owner decision (REQUESTS R25-1).
    """
    global _client, _collection
    _client = None
    _collection = None
