"""
Retrieval agent node (RAG stub with smart caching).
"""

from __future__ import annotations

import hashlib
from typing import Any

import structlog
from langchain_openai import ChatOpenAI

from sage.agents.state import AgentState

log = structlog.get_logger(__name__)


def _query_cache_key(expanded_query: str) -> str:
    """Produce a stable cache key from the expanded query.

    Uses a truncated SHA-256 hash — sufficient for cache-hit
    comparison while avoiding collisions in practical use.
    """
    return hashlib.sha256(expanded_query.encode()).hexdigest()[:16]


async def retrieval_node(state: AgentState, llm: ChatOpenAI) -> dict[str, Any]:
    """Retrieve relevant curriculum chunks and extract Knowledge Units.

    Currently a stub — returns empty retrieval fields.  When RAG is
    integrated, this function's body will be replaced with:
      1. ``hybrid_retrieve(expanded_query)`` → dense + sparse + RRF
      2. ``extract_knowledge_units(chunks, query, llm)`` → KU list

    **Smart cache**: If the expanded_query hash matches the previous
    retrieval, cached results are reused.  This prevents redundant
    vector lookups when the student asks sequential questions about
    the same topic (e.g. "explain B-trees" → "what about deletion?").
    """
    expanded_query: str = state.get("expanded_query", state.get("query", ""))
    course_code: str | None = state.get("course_code")
    
    if course_code and course_code.lower() != "all":
        log.info("retrieval_course_filter", course_code=course_code, hint="Retrieving strictly within course metadata")
    else:
        log.info("retrieval_global_search", hint="Retrieving across entire corpus")
        
    log.info(
        "retrieval_stub_active",
        query_preview=expanded_query[:80],
        course_code=course_code,
        hint="RAG pipeline not yet integrated.",
    )
    cache_key = _query_cache_key(expanded_query)

    # ── Cache hit: reuse previous retrieval ──
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

    # ── RAG stub: no retrieval yet ──
    # TODO: Replace with actual hybrid retrieval + KU extraction.
    # The function signature and return dict keys are stable contracts.
    log.warning(
        "retrieval_stub_active",
        query_preview=expanded_query[:80],
        hint="RAG pipeline not yet integrated — returning empty results.",
    )

    chunks: list[dict] = []
    knowledge_units: list[dict] = []

    return {
        "chunks": chunks,
        "knowledge_units": knowledge_units,
        "retrieval_cache_key": cache_key,
        "retrieval_cache_chunks": chunks,
        "retrieval_cache_kus": knowledge_units,
    }
