"""
Sage Ingestion Pipeline.

Reads preprocessed .md documents from `processed/`, chunks them with
RecursiveCharacterTextSplitter, prepends a deterministic context header
derived from YAML front-matter, embeds with a local FastEmbed
model, upserts into the `curriculum` ChromaDB collection, and atomically
rebuilds the BM25 index for hybrid retrieval.

Trigger:
    uv run python scripts/ingest.py [OPTIONS]

Options:
    --processed-dir PATH   Override processed/ location
    --force                Re-ingest all files, ignoring mtime cache
    --dry-run              Log what would be ingested without writing
    --course      CODE     Ingest only files matching this course code
    --log-level   LEVEL    debug | info | warning | error
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pickle
import re
import sys
import time
import tomllib
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import structlog
import yaml
from fastembed import TextEmbedding
from langchain_text_splitters import RecursiveCharacterTextSplitter
from rank_bm25 import BM25Okapi

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT / "src"))

from sage.config import get_settings
from sage.utils import configure_logging

log = structlog.get_logger(__name__)

_FM_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
_REF_HEADING_RE = re.compile(
    r"^#{0,3}\s*(?:references|bibliography|works\s+cited|further\s+reading)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_\-]")
_CHARS_PER_TOKEN: int = 4
_EMBED_BATCH_SIZE: int = 128
_CHROMA_UPSERT_BATCH: int = 128
_CHROMA_WRITE_CONCURRENCY: int = 12
_EXC_LOG_TRUNC: int = 300
_EXC_MSG_TRUNC: int = 200

# Chunk ID components
_TITLE_MAX_CHARS: int = 64
_COURSE_MAX_CHARS: int = 16
_PATH_HASH_CHARS: int = 8

_MTIME_CACHE_FILENAME: str = "ingest_cache.json"
_PARSE_WORKERS: int = 16

@dataclass
class ProcessedDoc:
    """Parsed representation of one preprocessed .md file."""

    md_path: Path
    meta: dict[str, Any]
    body: str
    references: str
    mtime: float


@dataclass
class IngestResult:
    """Outcome record for one document ingest attempt."""

    status: str
    source_path: str
    course_code: str
    chunk_count: int
    elapsed_ms: int
    error: Optional[str] = None


@dataclass
class IngestPlan:
    """Prepared chunks and metadata for a document ingest."""

    doc: ProcessedDoc
    source_path: str
    course_code: str
    doc_title: str
    chunks: list[str]
    chunk_ids: list[str]
    metadatas: list[dict[str, Any]]
    embed_offset: int

def _load_mtime_cache(cache_path: Path) -> dict[str, float]:
    """Load the mtime cache from JSON.  Returns empty dict if absent or corrupt."""
    if not cache_path.exists():
        return {}
    try:
        return json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning(
            "mtime_cache_load_failed",
            path=str(cache_path),
            exc_type=type(exc).__name__,
            error=str(exc)[:_EXC_MSG_TRUNC],
        )
        return {}


def _save_mtime_cache(cache: dict[str, float], cache_path: Path) -> None:
    """Atomically persist the mtime cache via a temp-file rename."""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(cache, indent=2), encoding="utf-8")
        tmp.replace(cache_path)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        log.warning(
            "mtime_cache_save_failed",
            path=str(cache_path),
            error=str(exc)[:_EXC_MSG_TRUNC],
        )

def _parse_md_file(md_path: Path) -> ProcessedDoc:
    """Parse YAML front-matter, body, and references from a preprocessed .md.

    Raises:
        ValueError: If the file cannot be read.
    """
    try:
        raw = md_path.read_bytes()
        mtime = md_path.stat().st_mtime
    except OSError as exc:
        raise ValueError(f"Cannot read {md_path}: {exc}") from exc

    content = raw.decode("utf-8", errors="replace")
    meta: dict[str, Any] = {}
    body = content

    fm_match = _FM_RE.match(content)
    if fm_match:
        try:
            meta = yaml.safe_load(fm_match.group(1)) or {}
        except yaml.YAMLError as exc:
            log.warning(
                "front_matter_parse_failed",
                path=str(md_path),
                error=str(exc)[:_EXC_MSG_TRUNC],
            )
        body = content[fm_match.end():].strip()

    ref_match = _REF_HEADING_RE.search(body)
    if ref_match:
        references = body[ref_match.start():].strip()
        body = body[: ref_match.start()].strip()
    else:
        references = ""

    return ProcessedDoc(
        md_path=md_path,
        meta=meta,
        body=body,
        references=references,
        mtime=mtime,
    )


def _safe_parse_md_file(md_path: Path) -> Optional[ProcessedDoc]:
    """Parse one .md file with error handling for threaded execution."""
    try:
        return _parse_md_file(md_path)
    except ValueError as exc:
        log.error(
            "md_parse_failed",
            path=str(md_path),
            error=str(exc)[:_EXC_MSG_TRUNC],
        )
        return None


def _walk_processed_dir(
    processed_root: Path,
    course_filter: Optional[str],
) -> list[ProcessedDoc]:
    """Discover and parse all .md files under processed_root."""
    if not processed_root.is_dir():
        log.error(
            "processed_dir_missing",
            path=str(processed_root),
            hint="Run preprocess.py first to populate this directory.",
        )
        return []

    skip_prefixes = (".", "_")
    md_paths = [
        md_path
        for md_path in sorted(processed_root.rglob("*.md"))
        if not any(md_path.name.startswith(p) for p in skip_prefixes)
    ]

    if not md_paths:
        log.info(
            "processed_dir_scanned",
            root=str(processed_root),
            found=0,
            course_filter=course_filter,
        )
        return []

    with ThreadPoolExecutor(max_workers=_PARSE_WORKERS) as pool:
        parsed = list(pool.map(_safe_parse_md_file, md_paths))

    docs = [doc for doc in parsed if doc]

    if course_filter:
        docs = [
            doc
            for doc in docs
            if str(doc.meta.get("course_code", "")).upper() == course_filter.upper()
        ]

    log.info(
        "processed_dir_scanned",
        root=str(processed_root),
        found=len(docs),
        course_filter=course_filter,
    )
    return docs


def _build_context_header(meta: dict[str, Any], md_path: Path) -> str:
    """Build a rich context header purely from YAML front-matter."""
    course_code = str(meta.get("course_code", "")).strip()
    course_title = str(meta.get("course_title", "")).strip()
    doc_title = str(meta.get("doc_title", md_path.stem)).strip()
    semester = meta.get("semester", "")
    program = str(meta.get("program_code", "")).strip()

    parts: list[str] = []
    if course_code and course_title:
        parts.append(f"{course_code} – {course_title}")
    elif course_code:
        parts.append(course_code)

    if doc_title:
        parts.append(doc_title)

    if semester:
        parts.append(f"Semester {semester}")

    if program:
        parts.append(program)

    header_body = " | ".join(parts) if parts else md_path.stem
    return f"[{header_body}]\n\n"


def _chunk_document(
    body: str,
    chunk_size: int,
    chunk_overlap: int,
    min_chunk_tokens: int,
    context_header: str,
) -> list[str]:
    """Split body text, apply the context header, and gate on minimum size.

    The context header is prepended to each chunk so the embedding encodes
    document-level context (course, title, semester) alongside chunk content.

    Returns:
        List of context-enriched, adequately-sized chunk strings.
    """
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        length_function=len,
        separators=["\n\n", "\n", ". ", " ", ""],
    )
    raw_chunks = splitter.split_text(body)

    min_chars = min_chunk_tokens * _CHARS_PER_TOKEN
    enriched: list[str] = []
    for chunk in raw_chunks:
        stripped = chunk.strip()
        if len(stripped) >= min_chars:
            enriched.append(f"{context_header}{stripped}")

    log.debug(
        "document_chunked",
        raw=len(raw_chunks),
        kept=len(enriched),
        min_chars=min_chars,
    )
    return enriched


def _load_embedder(model_name: str, cache_dir: Path) -> TextEmbedding:
    cache_dir.mkdir(parents=True, exist_ok=True)
    
    embedder = TextEmbedding(
        model_name=model_name,
        cache_dir=str(cache_dir),
        threads=os.cpu_count() or 4,
    )
    
    log.info(
        "embedder_loaded",
        model=model_name,
        providers="CPU",
        cache_dir=str(cache_dir),
        threads=os.cpu_count() or 4,
    )
    return embedder

def _embed_chunks(texts: list[str], embedder: TextEmbedding) -> list[list[float]]:
    """Encode chunk texts into normalised 384-dim float vectors via FastEmbed.

    `TextEmbedding.embed()` returns a generator of numpy arrays.  FastEmbed
    normalises bge-small-en-v1.5 embeddings by default, which is required
    for cosine similarity via dot-product in ChromaDB.
    """
    return [v.tolist() for v in embedder.embed(texts, batch_size=_EMBED_BATCH_SIZE)]


def _get_or_create_collection(vectordb_dir: Path, collection_name: str) -> Any:
    """Open or create the named ChromaDB collection.

    Uses cosine distance to match the normalised embeddings produced by
    _embed_chunks().

    Raises:
        ImportError: If chromadb is not installed.
    """
    try:
        import chromadb  # type: ignore[import-untyped]
    except ImportError as exc:
        raise ImportError(
            "chromadb is required: uv add chromadb"
        ) from exc

    vectordb_dir.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(vectordb_dir))
    collection = client.get_or_create_collection(
        name=collection_name,
        metadata={"hnsw:space": "cosine"},
    )
    log.info(
        "chroma_collection_ready",
        collection=collection_name,
        existing_chunks=collection.count(),
    )
    return collection


def _build_chunk_ids(
    source_path: str,
    course_code: str,
    doc_title: str,
    count: int,
) -> list[str]:
    """Generate deterministic, globally unique ChromaDB document IDs.
    
    Format: `{course}__{title}__{path_hash}__chunk_{idx:04d}`

    The 8-char path hash prevents collisions when two courses share a
    document title (e.g. "Introduction.pptx" in CS201 and CS301).
    """
    import hashlib
    safe_course = _SAFE_ID_RE.sub("_", course_code)[:_COURSE_MAX_CHARS]
    safe_title = _SAFE_ID_RE.sub("_", doc_title)[:_TITLE_MAX_CHARS]
    path_hash = hashlib.sha256(source_path.encode()).hexdigest()[:_PATH_HASH_CHARS]
    prefix = f"{safe_course}__{safe_title}__{path_hash}"
    return [f"{prefix}__chunk_{i:04d}" for i in range(count)]


def _build_chunk_metadatas(
    doc: ProcessedDoc,
    count: int,
) -> list[dict[str, Any]]:
    """Build a per-chunk metadata dict from the document's front-matter."""
    meta = doc.meta
    base: dict[str, Any] = {
        "source_path":   str(meta.get("source_path", str(doc.md_path))),
        "program_code":  str(meta.get("program_code", "")),
        "semester":      int(meta.get("semester", 0)),
        "course_code":   str(meta.get("course_code", "")),
        "course_title":  str(meta.get("course_title", "")),
        "doc_title":     str(meta.get("doc_title", doc.md_path.stem)),
        "source_format": str(meta.get("source_format", "")),
        "last_modified": str(meta.get("last_modified", "")),
    }
    return [{**base, "chunk_index": i} for i in range(count)]


def _upsert_to_chroma(
    collection: Any,
    chunk_ids: list[str],
    chunk_texts: list[str],
    embeddings: list[list[float]],
    metadatas: list[dict[str, Any]],
) -> None:
    """Page-upsert chunks into ChromaDB to avoid large single transactions."""
    total = len(chunk_ids)
    for start in range(0, total, _CHROMA_UPSERT_BATCH):
        end = min(start + _CHROMA_UPSERT_BATCH, total)
        collection.upsert(
            ids=chunk_ids[start:end],
            documents=chunk_texts[start:end],
            embeddings=embeddings[start:end],
            metadatas=metadatas[start:end],
        )
    log.debug("chroma_upsert_done", total_chunks=total)


def _build_bm25_from_chunks(
    chunks: list[str],
    chunk_ids: list[str],
    metadatas: list[dict[str, Any]],
    bm25_path: Path,
) -> int:
    """Build BM25 from in-memory chunks and atomically persist it."""
    log.info("bm25_rebuild_starting", total_chunks=len(chunks))
    t0 = time.perf_counter()

    if not chunks:
        log.warning("bm25_rebuild_empty_collection")
        return 0

    tokenized = [doc.lower().split() for doc in chunks]
    bm25_model = BM25Okapi(tokenized)

    bm25_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "bm25":      bm25_model,
        "ids":       chunk_ids,
        "documents": chunks,
        "metadatas": metadatas,
    }
    tmp = bm25_path.with_suffix(".pkl.tmp")
    try:
        with tmp.open("wb") as fh:
            pickle.dump(payload, fh, protocol=pickle.HIGHEST_PROTOCOL)
        tmp.replace(bm25_path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise

    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    log.info(
        "bm25_rebuild_complete",
        total_chunks=len(chunks),
        elapsed_ms=elapsed_ms,
        path=str(bm25_path),
    )
    return len(chunks)

async def _ingest_one_no_embed(
    plan: IngestPlan,
    embeddings: list[list[float]],
    collection: Any,
    mtime_cache: dict[str, float],
    write_sem: asyncio.Semaphore,
) -> IngestResult:
    """Upsert one document using precomputed embeddings."""
    t0 = time.perf_counter()
    start = plan.embed_offset
    end = start + len(plan.chunks)
    chunk_embeddings = embeddings[start:end]

    try:
        async with write_sem:
            await asyncio.to_thread(
                _upsert_to_chroma,
                collection,
                plan.chunk_ids,
                plan.chunks,
                chunk_embeddings,
                plan.metadatas,
            )
    except Exception as exc:
        log.error(
            "chroma_upsert_failed",
            source_path=plan.source_path,
            exc_type=type(exc).__name__,
            error=str(exc)[:_EXC_LOG_TRUNC],
        )
        return IngestResult(
            status="error",
            source_path=plan.source_path,
            course_code=plan.course_code,
            chunk_count=0,
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
            error=f"ChromaDB upsert failed: {str(exc)[:_EXC_MSG_TRUNC]}",
        )

    mtime_cache[plan.source_path] = plan.doc.mtime

    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    log.info(
        "document_ingested",
        source_path=plan.source_path,
        course_code=plan.course_code,
        chunks=len(plan.chunks),
        elapsed_ms=elapsed_ms,
    )
    return IngestResult(
        status="ok",
        source_path=plan.source_path,
        course_code=plan.course_code,
        chunk_count=len(plan.chunks),
        elapsed_ms=elapsed_ms,
    )

async def run_pipeline(
    processed_root: Path,
    dry_run: bool,
    force: bool,
    course_filter: Optional[str],
) -> int:
    """Orchestrate the full ingestion run."""
    cfg = get_settings()
    cfg_rag = cfg.rag
    cfg_emb = cfg.embedding

    # Step 1: discover
    docs = _walk_processed_dir(processed_root, course_filter)
    if not docs:
        log.info("no_processed_documents_found", root=str(processed_root))
        return 0

    log.info(
        "ingest_pipeline_starting",
        total_docs=len(docs),
        dry_run=dry_run,
        force=force,
        course_filter=course_filter,
    )

    # Step 2: mtime cache
    vectordb_dir = cfg_rag.vectordb_dir
    if not vectordb_dir.is_absolute():
        vectordb_dir = _PROJECT_ROOT / vectordb_dir

    cache_path = vectordb_dir / _MTIME_CACHE_FILENAME
    mtime_cache: dict[str, float] = {} if force else _load_mtime_cache(cache_path)

    # Step 3: chunk documents and build ingest plans
    plans: list[IngestPlan] = []
    results: list[IngestResult] = []
    bm25_chunks: list[str] = []
    bm25_ids: list[str] = []
    bm25_metadatas: list[dict[str, Any]] = []
    embed_chunks: list[str] = []

    for doc in docs:
        t0 = time.perf_counter()
        source_path = str(doc.meta.get("source_path", str(doc.md_path)))
        course_code = str(doc.meta.get("course_code", "UNKNOWN"))
        doc_title = str(doc.meta.get("doc_title", doc.md_path.stem))

        cached_mtime = mtime_cache.get(source_path)
        skip_unchanged = (
            not force
            and cached_mtime is not None
            and cached_mtime == doc.mtime
        )
        if skip_unchanged:
            log.debug("ingest_skipped_unchanged", source_path=source_path)

        if skip_unchanged and dry_run:
            results.append(
                IngestResult(
                    status="skipped",
                    source_path=source_path,
                    course_code=course_code,
                    chunk_count=0,
                    elapsed_ms=int((time.perf_counter() - t0) * 1000),
                )
            )
            continue

        if not doc.body.strip():
            log.warning("ingest_empty_body", source_path=source_path)
            results.append(
                IngestResult(
                    status="empty",
                    source_path=source_path,
                    course_code=course_code,
                    chunk_count=0,
                    elapsed_ms=int((time.perf_counter() - t0) * 1000),
                    error="Empty body after preprocessing",
                )
            )
            continue

        context_header = _build_context_header(doc.meta, doc.md_path)
        chunks = _chunk_document(
            doc.body,
            cfg_rag.chunk_size,
            cfg_rag.chunk_overlap,
            cfg_rag.min_chunk_tokens,
            context_header,
        )
        if not chunks:
            log.warning("ingest_no_viable_chunks", source_path=source_path)
            results.append(
                IngestResult(
                    status="empty",
                    source_path=source_path,
                    course_code=course_code,
                    chunk_count=0,
                    elapsed_ms=int((time.perf_counter() - t0) * 1000),
                    error="No chunks after size gate",
                )
            )
            continue

        chunk_ids = _build_chunk_ids(source_path, course_code, doc_title, len(chunks))
        metadatas = _build_chunk_metadatas(doc, len(chunks))

        if not dry_run:
            bm25_chunks.extend(chunks)
            bm25_ids.extend(chunk_ids)
            bm25_metadatas.extend(metadatas)

        if dry_run:
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            log.info(
                "dry_run_would_ingest",
                source_path=source_path,
                chunks=len(chunks),
                context_header=context_header.strip(),
                elapsed_ms=elapsed_ms,
            )
            results.append(
                IngestResult(
                    status="dry_run",
                    source_path=source_path,
                    course_code=course_code,
                    chunk_count=len(chunks),
                    elapsed_ms=elapsed_ms,
                )
            )
            continue

        if skip_unchanged:
            results.append(
                IngestResult(
                    status="skipped",
                    source_path=source_path,
                    course_code=course_code,
                    chunk_count=0,
                    elapsed_ms=int((time.perf_counter() - t0) * 1000),
                )
            )
            continue

        embed_offset = len(embed_chunks)
        embed_chunks.extend(chunks)
        plans.append(
            IngestPlan(
                doc=doc,
                source_path=source_path,
                course_code=course_code,
                doc_title=doc_title,
                chunks=chunks,
                chunk_ids=chunk_ids,
                metadatas=metadatas,
                embed_offset=embed_offset,
            )
        )

    if dry_run:
        log.info("bm25_rebuild_skipped", reason="dry_run")
        _emit_terminal_summary(results, docs)
        return 0 if all(r.status != "error" for r in results) else 1

    if not plans:
        _save_mtime_cache(mtime_cache, cache_path)
        log.info("bm25_rebuild_skipped", reason="no_changes")
        _emit_terminal_summary(results, docs)
        return 0 if all(r.status != "error" for r in results) else 1

    # Step 4: load embedder
    embed_cache_dir = cfg_emb.cache_dir
    if not embed_cache_dir.is_absolute():
        embed_cache_dir = _PROJECT_ROOT / embed_cache_dir

    embedder: TextEmbedding
    try:
        embedder = _load_embedder(cfg_emb.model_name, embed_cache_dir)
    except OSError as exc:
        log.error("embedder_load_failed", error=str(exc))
        return 1

    # Step 5: global embed
    log.info(
        "global_embed_start",
        total_chunks=len(embed_chunks),
        batch_size=_EMBED_BATCH_SIZE,
    )
    try:
        embeddings = await asyncio.to_thread(_embed_chunks, embed_chunks, embedder)
    except Exception as exc:
        log.error(
            "embed_failed",
            exc_type=type(exc).__name__,
            error=str(exc)[:_EXC_LOG_TRUNC],
        )
        error_msg = f"Embed failed: {str(exc)[:_EXC_MSG_TRUNC]}"
        for plan in plans:
            results.append(
                IngestResult(
                    status="error",
                    source_path=plan.source_path,
                    course_code=plan.course_code,
                    chunk_count=0,
                    elapsed_ms=0,
                    error=error_msg,
                )
            )
        _emit_terminal_summary(results, docs)
        return 1

    # Step 6: ChromaDB collection
    try:
        collection = _get_or_create_collection(
            vectordb_dir,
            cfg_rag.curriculum_collection,
        )
    except ImportError as exc:
        log.error("chroma_init_failed", error=str(exc))
        return 1

    # Step 7:  all documents concurrently (limited by semaphore)
    write_sem = asyncio.Semaphore(_CHROMA_WRITE_CONCURRENCY)
    tasks = [
        _ingest_one_no_embed(
            plan=plan,
            embeddings=embeddings,
            collection=collection,
            mtime_cache=mtime_cache,
            write_sem=write_sem,
        )
        for plan in plans
    ]

    raw_results = await asyncio.gather(*tasks, return_exceptions=True)
    for plan, outcome in zip(plans, raw_results):
        if isinstance(outcome, BaseException):
            log.error(
                "ingest_task_exception",
                source_path=plan.source_path,
                exc_type=type(outcome).__name__,
                error=str(outcome)[:_EXC_LOG_TRUNC],
            )
            results.append(
                IngestResult(
                    status="error",
                    source_path=plan.source_path,
                    course_code=plan.course_code,
                    chunk_count=0,
                    elapsed_ms=0,
                    error=str(outcome)[:_EXC_MSG_TRUNC],
                )
            )
        else:
            results.append(outcome)

    # Step 8: persist mtime cache (only entries updated during this run)
    _save_mtime_cache(mtime_cache, cache_path)

    # Step 9: rebuild BM25 index if anything changed
    n_ok = sum(1 for r in results if r.status == "ok")

    bm25_path = cfg_rag.bm25_curriculum_file
    if not bm25_path.is_absolute():
        bm25_path = _PROJECT_ROOT / bm25_path

    if n_ok > 0:
        try:
            total_indexed = _build_bm25_from_chunks(
                bm25_chunks,
                bm25_ids,
                bm25_metadatas,
                bm25_path,
            )
            log.info("bm25_index_ready", total_chunks=total_indexed)
        except Exception as exc:
            log.error(
                "bm25_rebuild_failed",
                exc_type=type(exc).__name__,
                error=str(exc)[:_EXC_LOG_TRUNC],
            )
    else:
        log.info("bm25_rebuild_skipped", reason="no_changes")

    # Step 10: terminal summary
    _emit_terminal_summary(results, docs)

    return 0 if all(r.status != "error" for r in results) else 1


def _render_ascii_table(headers: list[str], rows: list[list[object]]) -> str:
    string_rows = [[str(c) for c in row] for row in rows]
    widths = [len(h) for h in headers]
    for row in string_rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def _fmt(values: list[str]) -> str:
        return "| " + " | ".join(v.ljust(widths[i]) for i, v in enumerate(values)) + " |"

    rule = "+-" + "-+-".join("-" * w for w in widths) + "-+"
    return "\n".join([rule, _fmt(headers), rule, *[_fmt(r) for r in string_rows], rule])


def _emit_terminal_summary(
    results: list[IngestResult],
    docs: list[ProcessedDoc],
) -> None:
    counts: Counter[str] = Counter(r.status for r in results)
    total_chunks = sum(r.chunk_count for r in results)
    total_ms = sum(r.elapsed_ms for r in results)

    by_course: dict[str, Counter[str]] = defaultdict(Counter)
    chunks_by_course: Counter[str] = Counter()
    for r in results:
        by_course[r.course_code][r.status] += 1
        chunks_by_course[r.course_code] += r.chunk_count

    overview = [
        ["Total documents",         len(docs)],
        ["Ingested",            counts["ok"]],
        ["Skipped",      counts["skipped"]],
        ["Dry-run (would ingest)",   counts["dry_run"]],
        ["Empty)",   counts["empty"]],
        ["Errors",                   counts["error"]],
        ["Total chunks produced",    total_chunks],
        ["Wall time",                f"{total_ms / 1000:.1f}s"],
    ]

    course_rows = [
        [
            code,
            by_course[code]["ok"],
            by_course[code]["skipped"],
            by_course[code]["empty"],
            by_course[code]["error"],
            chunks_by_course[code],
        ]
        for code in sorted(by_course)
    ]

    lines = [
        "",
        "=" * 88,
        "SAGE INGEST SUMMARY",
        "=" * 88,
        _render_ascii_table(["Metric", "Value"], overview),
    ]
    if course_rows:
        lines.append(
            _render_ascii_table(
                ["Course", "OK", "Skipped", "Empty", "Errors", "Chunks"],
                course_rows,
            )
        )
    lines += ["=" * 88, ""]
    print("\n".join(lines), flush=True)

def _load_sage_version() -> str:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "pyproject.toml"
        if candidate.exists():
            try:
                with candidate.open("rb") as fh:
                    data = tomllib.load(fh)
                return str(data["project"]["version"])
            except (OSError, KeyError, TypeError):
                pass
    return "0.0.0"


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="ingest",
        description="Sage ingestion pipeline: processed/ → ChromaDB + BM25",
    )
    p.add_argument(
        "--processed-dir",
        type=Path,
        default=None,
        metavar="PATH",
        help="Override the processed/ directory (default: artifacts/data/processed)",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Re-ingest all files, ignoring the mtime cache",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Log what would be ingested without writing to ChromaDB or BM25",
    )
    p.add_argument(
        "--course",
        type=str,
        default=None,
        metavar="CODE",
        help="Ingest only documents matching this course code (e.g. CS301)",
    )
    p.add_argument(
        "--log-level",
        choices=["debug", "info", "warning", "error"],
        default="info",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    configure_logging(args.log_level)

    cfg = get_settings()
    data_root = cfg.app.data_dir
    if not data_root.is_absolute():
        data_root = _PROJECT_ROOT / data_root

    processed_root: Path = args.processed_dir or (data_root / "processed")

    log.info(
        "ingest_startup",
        version=_load_sage_version(),
        processed_root=str(processed_root),
        force=args.force,
        dry_run=args.dry_run,
        course_filter=args.course,
    )

    sys.exit(
        asyncio.run(
            run_pipeline(
                processed_root=processed_root,
                dry_run=args.dry_run,
                force=args.force,
                course_filter=args.course,
            )
        )
    )


if __name__ == "__main__":
    main()