"""
ChromaDB client singleton for Sage.

Provides a lazily-initialised, module-level ChromaDB `PersistentClient`
and a thin helper to build a `where` filter for course-scoped retrieval.
"""

from __future__ import annotations

import threading
from typing import Any

import chromadb
import structlog

from sage.config import get_settings

log = structlog.get_logger(__name__)

_lock = threading.RLock()
_client: chromadb.PersistentClient | None = None
_collection: chromadb.Collection | None = None


def get_chroma_client() -> chromadb.PersistentClient:
    """Return the module-level ChromaDB PersistentClient, creating it once."""
    global _client
    if _client is not None:
        return _client
    with _lock:
        if _client is not None:
            return _client
        cfg = get_settings().rag
        path = str(cfg.vectordb)
        log.info("chroma_client_init", path=path)
        _client = chromadb.PersistentClient(path=path)
    return _client


def get_curriculum_collection() -> chromadb.Collection:
    """Return the curriculum collection, creating the handle once."""
    global _collection
    if _collection is not None:
        return _collection
    with _lock:
        if _collection is not None:
            return _collection
        client = get_chroma_client()
        name = get_settings().rag.curriculum_collection
        _collection = client.get_or_create_collection(
            name=name,
            metadata={"hnsw:space": "cosine"},
        )
        log.info(
            "chroma_collection_ready",
            name=name,
            count=_collection.count(),
        )
    return _collection


def build_course_filter(course_code: str | None) -> dict[str, Any] | None:
    """Build a ChromaDB `where` clause for course-scoped queries.

    Args:
        course_code: Upper-cased course code (e.g. "CMPC101") or
                     None or "all" to retrieve across the whole corpus.

    Returns:
        A ChromaDB `where` dict, or None for corpus-wide retrieval.
    """
    if not course_code or course_code.strip().lower() == "all":
        return None
    return {"course_code": {"$eq": course_code.strip().upper()}}
