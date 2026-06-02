"""
Hybrid retriever for Sage curriculum corpus.

Pipeline:
1. Dense retrieval  : ChromaDB cosine-similarity search (FastEmbed)
2. Sparse retrieval : BM25 index (rank-bm25) over stored chunk texts
3. RRF fusion       : Reciprocal Rank Fusion merges both ranked lists
4. Course filter    : optional `where` clause restricts to one course

Usage:
    from sage.rag.retrieval import hybrid_retrieve
    chunks = await hybrid_retrieve("explain binary search trees", course_code="CMPC201")
"""

from __future__ import annotations

import asyncio
import pickle
from functools import lru_cache
from pathlib import Path
from typing import Any

import structlog
from fastembed import TextEmbedding

from sage.config import get_settings
from sage.rag.vectorstore import build_course_filter, get_curriculum_collection

log = structlog.get_logger(__name__)


@lru_cache(maxsize=1)
def _get_embed_model() -> TextEmbedding:
    """Load the FastEmbed model once and cache it for the process lifetime."""
    cfg = get_settings()
    model_path = Path(cfg.embedding.embed_model)
    model_name = "BAAI/bge-small-en-v1.5"
    cache_dir = "artifacts/models/embedding-models"
    parts = model_path.parts
    for i, part in enumerate(parts):
        if part.startswith("models--"):
            cache_dir = str(Path(*parts[:i]))
            break

    log.info("embed_model_loading", model_name=model_name, cache_dir=cache_dir)
    model = TextEmbedding(
        model_name=model_name,
        cache_dir=cache_dir,
        local_files_only=True,
    )
    log.info("embed_model_ready")
    return model


def _embed_query(text: str) -> list[float]:
    """Embed a single query string, returning a float list."""
    model = _get_embed_model()
    vectors = list(model.embed([text]))
    return [float(v) for v in vectors[0]]


@lru_cache(maxsize=1)
def _load_bm25_index() -> tuple[Any, list[str], list[str]]:
    """Load the pickled BM25 index and its parallel doc-id / course lists.

    Returns:
        (bm25_model, ids, course_codes) where `ids[i]` is the Chroma
        chunk id and `course_codes[i]` is the course_code string for
        rank *i*.

    Raises:
        FileNotFoundError: If the BM25 pickle does not exist on disk.
    """
    cfg = get_settings()
    bm25_path = Path(cfg.rag.bm25_curriculum_file)
    if not bm25_path.exists():
        raise FileNotFoundError(f"BM25 index not found at {bm25_path}. Run the ingestion pipeline first.")
    log.info("bm25_index_loading", path=str(bm25_path))
    with bm25_path.open("rb") as fh:
        payload: dict[str, Any] = pickle.load(fh)

    bm25 = payload["bm25"]
    ids: list[str] = payload["ids"]
    metadatas: list[dict[str, Any]] = payload.get("metadatas", [{}] * len(ids))
    course_codes: list[str] = [str(m.get("course_code", "")).upper() for m in metadatas]
    log.info("bm25_index_ready", doc_count=len(ids))
    return bm25, ids, course_codes


def _rrf_fuse(
    dense_ids: list[str],
    sparse_ids: list[str],
    *,
    k: int,
) -> list[str]:
    """Reciprocal Rank Fusion of two ranked doc-id lists.

    Args:
        dense_ids:  Ordered list of doc ids from vector search (best first).
        sparse_ids: Ordered list of doc ids from BM25 search (best first).
        k:          RRF damping constant.

    Returns:
        Merged, re-ranked list of doc ids (best first, deduplicated).
    """
    scores: dict[str, float] = {}

    for rank, doc_id in enumerate(dense_ids, start=1):
        scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)

    for rank, doc_id in enumerate(sparse_ids, start=1):
        scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)

    return sorted(scores, key=lambda d: scores[d], reverse=True)


def _dense_retrieve(
    query: str,
    *,
    n_results: int,
    where: dict[str, Any] | None,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Query ChromaDB and return (ordered_ids, id→metadata map).

    Returns:
        A 2-tuple of:
        - ids: doc ids in descending similarity order
        - metas: list of metadata dicts parallel to ids
    """
    collection = get_curriculum_collection()
    query_embedding = _embed_query(query)

    kwargs: dict[str, Any] = {
        "query_embeddings": [query_embedding],
        "n_results": min(n_results, collection.count() or 1),
        "include": ["metadatas", "documents"],
    }
    if where is not None:
        kwargs["where"] = where

    try:
        results = collection.query(**kwargs)
    except Exception as exc:
        log.error(
            "dense_retrieve_failed",
            exc_type=type(exc).__name__,
            error=str(exc)[:300],
        )
        return [], []

    ids: list[str] = (results.get("ids") or [[]])[0]
    raw_metas: list[dict] = (results.get("metadatas") or [[]])[0]
    raw_docs: list[str] = (results.get("documents") or [[]])[0]

    metas: list[dict[str, Any]] = []
    for meta, doc_text in zip(raw_metas, raw_docs, strict=False):
        entry = dict(meta)
        entry["_text"] = doc_text or ""
        metas.append(entry)

    return ids, metas


def _sparse_retrieve(
    query: str,
    *,
    n_results: int,
    course_code: str | None,
) -> list[str]:
    """BM25 retrieval. Returns ordered chunk ids (best first)."""
    try:
        bm25, ids, stored_courses = _load_bm25_index()
    except FileNotFoundError as exc:
        log.warning("bm25_unavailable", reason=str(exc))
        return []

    tokenized_query = query.lower().split()
    scores: list[float] = bm25.get_scores(tokenized_query).tolist()
    ranked = sorted(
        zip(scores, ids, stored_courses, strict=False),
        key=lambda t: t[0],
        reverse=True,
    )

    if course_code and course_code.strip().lower() != "all":
        upper_code = course_code.strip().upper()
        ranked = [(s, did, cc) for s, did, cc in ranked if cc == upper_code]

    return [did for _, did, _ in ranked[:n_results]]


def _hydrate_chunks(
    fused_ids: list[str],
    dense_meta_map: dict[str, dict[str, Any]],
    *,
    top_k: int,
) -> list[dict[str, Any]]:
    """Build the final chunk list from fused ids."""
    missing_ids = [cid for cid in fused_ids[:top_k] if cid not in dense_meta_map]

    if missing_ids:
        try:
            collection = get_curriculum_collection()
            result = collection.get(
                ids=missing_ids,
                include=["metadatas", "documents"],
            )
            for cid, meta, doc in zip(
                result.get("ids", []),
                result.get("metadatas", []),
                result.get("documents", []),
                strict=False,
            ):
                entry = dict(meta or {})
                entry["_text"] = doc or ""
                dense_meta_map[cid] = entry
        except Exception as exc:
            log.warning(
                "chunk_hydration_partial_failure",
                missing=len(missing_ids),
                exc_type=type(exc).__name__,
                error=str(exc)[:200],
            )

    chunks: list[dict[str, Any]] = []
    for cid in fused_ids[:top_k]:
        meta = dense_meta_map.get(cid)
        if meta is None:
            continue
        chunk: dict[str, Any] = {
            "id": cid,
            "text": meta.get("_text", ""),
            "source_file": meta.get("doc_title", meta.get("source_path", "unknown")),
            "source_page": meta.get("page_number", ""),
            "course_code": meta.get("course_code", ""),
            "course_title": meta.get("course_title", ""),
            "program_code": meta.get("program_code", ""),
            "semester": meta.get("semester", ""),
            "chunk_index": meta.get("chunk_index", ""),
        }
        chunks.append(chunk)

    return chunks


async def hybrid_retrieve(
    query: str,
    *,
    course_code: str | None = None,
) -> list[dict[str, Any]]:
    """Hybrid dense+sparse retrieval with RRF fusion.

    Args:
        query: The student's query string.
        course_code: If set (e.g. "CMPC201"), retrieval is restricted to chunks belonging to that course.
                        Pass None or "all" for corpus-wide search.
    """
    cfg = get_settings().rag
    top_k: int = cfg.top_k
    fetch_n: int = top_k * cfg.retrieval_multiplier
    rrf_k: int = cfg.rrf_k_constant

    where = build_course_filter(course_code)

    log.info(
        "hybrid_retrieve_start",
        query_preview=query[:80],
        course_code=course_code or "all",
        top_k=top_k,
        fetch_n=fetch_n,
        rrf_k=rrf_k,
    )

    loop = asyncio.get_event_loop()

    dense_task = loop.run_in_executor(
        None,
        lambda: _dense_retrieve(query, n_results=fetch_n, where=where),
    )
    sparse_task = loop.run_in_executor(
        None,
        lambda: _sparse_retrieve(query, n_results=fetch_n, course_code=course_code),
    )

    (dense_ids, dense_metas), sparse_ids = await asyncio.gather(dense_task, sparse_task)

    dense_meta_map: dict[str, dict[str, Any]] = {cid: meta for cid, meta in zip(dense_ids, dense_metas, strict=False)}
    fused_ids = _rrf_fuse(dense_ids, sparse_ids, k=rrf_k)
    chunks = _hydrate_chunks(fused_ids, dense_meta_map, top_k=top_k)

    log.info(
        "hybrid_retrieve_complete",
        course_code=course_code or "all",
        dense_count=len(dense_ids),
        sparse_count=len(sparse_ids),
        fused_count=len(fused_ids),
        returned=len(chunks),
    )

    return chunks
