"""
Retrieval agent node for Sage.

Implements hybrid RAG retrieval (dense + BM25 + RRF).

Pipeline:
1. Cache check: reuse previous retrieval
2. hybrid_retrieve(): dense + BM25 + RRF, optionally course-scoped
3. Wrap chunks into KUs: each returned chunk is mapped 1-to-1 into the Knowledge Unit dict schema

Knowledge Unit schema (dict):
    id          : "KU{i+1}"  (1-indexed, e.g. "KU1")
    content     : verbatim chunk text
    claim       : verbatim chunk text
    source_file : doc_title or source_path from chunk metadata
    source_page : page number string (may be empty)
    course_code : course code from chunk metadata
    confidence  : "high" | "medium" | "low" based on RRF rank
"""

from __future__ import annotations

import hashlib
from typing import Any

import structlog
from langchain_openai import ChatOpenAI

from sage.agents.state import AgentState
from sage.rag import hybrid_retrieve

log = structlog.get_logger(__name__)

_CONFIDENCE_HIGH_THRESH = 1
_CONFIDENCE_MED_THRESH = 3


def _query_cache_key(query: str) -> str:
    """Produce a stable 16-char hex cache key from the query."""
    return hashlib.sha256(query.encode()).hexdigest()[:16]


def _wrap_chunks_as_kus(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Wrap retrieved chunk dicts into the Knowledge Unit schema (1-to-1 mapping).

    Args:
        chunks: List of chunk dicts returned by `hybrid_retrieve`.

    Returns:
        Parallel list of KU dicts, ordered by RRF rank.
    """
    kus: list[dict[str, Any]] = []
    for i, chunk in enumerate(chunks):
        rank = i + 1
        if rank <= _CONFIDENCE_HIGH_THRESH:
            confidence = "high"
        elif rank <= _CONFIDENCE_MED_THRESH:
            confidence = "medium"
        else:
            confidence = "low"

        text: str = chunk.get("text", "")
        kus.append(
            {
                "id": f"KU{rank}",
                "content": text,
                "claim": text,
                "source_file": chunk.get("source_file", "unknown"),
                "source_page": chunk.get("source_page", ""),
                "course_code": chunk.get("course_code", ""),
                "confidence": confidence,
            }
        )
    return kus


async def retrieval_node(state: AgentState, llm: ChatOpenAI) -> dict[str, Any]:
    """Hybrid RAG retrieval node"""
    query: str = state.get("query", "")
    course_code: str | None = state.get("course_code")

    if course_code and course_code.strip().lower() == "all":
        course_code = None

    log.info(
        "retrieval_start",
        query_preview=query[:80],
        course_code=course_code or "all",
    )

    cache_key = _query_cache_key(query)

    prev_key = state.get("retrieval_cache_key", "")
    if prev_key and prev_key == cache_key:
        cached_chunks = state.get("retrieval_cache_chunks", [])
        cached_kus = state.get("retrieval_cache_kus", [])
        if cached_chunks:
            log.info(
                "retrieval_cache_hit",
                cache_key=cache_key,
                chunks=len(cached_chunks),
                kus=len(cached_kus),
            )
            return {
                "chunks": cached_chunks,
                "knowledge_units": cached_kus,
            }

    try:
        chunks = await hybrid_retrieve(
            query,
            course_code=course_code,
        )
    except Exception as exc:
        log.error(
            "retrieval_failed",
            exc_type=type(exc).__name__,
            error=str(exc)[:400],
            query_preview=query[:80],
        )
        return {
            "chunks": [],
            "knowledge_units": [],
            "retrieval_cache_key": cache_key,
            "retrieval_cache_chunks": [],
            "retrieval_cache_kus": [],
        }

    knowledge_units = _wrap_chunks_as_kus(chunks)

    log.info(
        "retrieval_complete",
        course_code=course_code or "all",
        chunks=len(chunks),
        kus=len(knowledge_units),
    )

    return {
        "chunks": chunks,
        "knowledge_units": knowledge_units,
        "retrieval_cache_key": cache_key,
        "retrieval_cache_chunks": chunks,
        "retrieval_cache_kus": knowledge_units,
    }