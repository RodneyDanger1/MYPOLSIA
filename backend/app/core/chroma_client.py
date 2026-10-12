"""ChromaDB access.

`chromadb` is NOT installed in the unit-test environment, so it is imported lazily
inside the functions (CLAUDE.md: "Don't import chromadb ... at module level").
"""
from __future__ import annotations

from typing import Any

from app.config import settings

_client: Any | None = None
_collection: Any | None = None


def get_client() -> Any:
    global _client
    if _client is None:
        import chromadb  # lazy

        _client = chromadb.PersistentClient(path=settings.chroma_persist_dir)
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
    global _client, _collection
    _client = None
    _collection = None
