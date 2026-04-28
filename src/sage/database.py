"""
SQLite lifecycle manager for Sage.
All connections use WAL mode for concurrent read/write support.
Usage:

    async with get_async_db() as db:
        await db.execute("INSERT INTO conversations ...")

    # One-time at startup:
    await init_db()
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite
import structlog

from sage.config import get_settings

log = structlog.get_logger(__name__)

# --- Schema ---

_SCHEMA_VERSION: int = 3

_SCHEMA_SQL: str = """
-- Version tracking for future migrations.
CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER NOT NULL,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Conversation metadata.  Message history is stored by
-- LangGraph's AsyncSqliteSaver (checkpointer); this table
-- tracks the lightweight index shown in the UI sidebar.
CREATE TABLE IF NOT EXISTS conversations (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL DEFAULT 'New Chat',
    message_count   INTEGER NOT NULL DEFAULT 0,
    last_message    TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    is_archived     INTEGER NOT NULL DEFAULT 0
);

-- Long-term semantic memory.  Facts about the user extracted
-- after each conversation turn.  embedding column kept for
-- schema compatibility but is no longer populated (FTS5 replaces it).
CREATE TABLE IF NOT EXISTS memories (
    id              TEXT PRIMARY KEY,
    content         TEXT NOT NULL,
    category        TEXT NOT NULL DEFAULT 'general',
    embedding       BLOB,
    confidence      REAL NOT NULL DEFAULT 1.0,
    access_count    INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Corpus file index.  Tracks which files have been ingested
-- into the vector store so the pipeline can detect changes.
CREATE TABLE IF NOT EXISTS corpus_files (
    file_path   TEXT PRIMARY KEY,
    file_hash   TEXT NOT NULL,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    collection  TEXT NOT NULL DEFAULT 'curriculum',
    ingested_at TEXT NOT NULL DEFAULT (datetime('now')),
    status      TEXT NOT NULL DEFAULT 'active'
);

-- Flat key-value store for user-facing settings that
-- persist across sessions (theme, preferred mode, etc.).
CREATE TABLE IF NOT EXISTS user_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ---- Indices ----
CREATE INDEX IF NOT EXISTS idx_conversations_updated
    ON conversations (updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_memories_category
    ON memories (category);

CREATE INDEX IF NOT EXISTS idx_memories_updated
    ON memories (updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_corpus_files_collection
    ON corpus_files (collection);
"""

_FTS5_SETUP: list[str] = [
    """CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
        memory_id UNINDEXED,
        content,
        tokenize = 'porter ascii'
    )""",
    # INSERT trigger: new memory to add to FTS index.
    """CREATE TRIGGER IF NOT EXISTS memories_fts_ai
    AFTER INSERT ON memories
    BEGIN
        INSERT INTO memories_fts(memory_id, content)
        VALUES (new.id, new.content);
    END""",
    # DELETE trigger: removed memory to remove from FTS index.
    """CREATE TRIGGER IF NOT EXISTS memories_fts_ad
    AFTER DELETE ON memories
    BEGIN
        DELETE FROM memories_fts WHERE memory_id = old.id;
    END""",
    # UPDATE trigger: updated content to refresh FTS index.
    """CREATE TRIGGER IF NOT EXISTS memories_fts_au
    AFTER UPDATE OF content ON memories
    BEGIN
        UPDATE memories_fts
        SET content = new.content
        WHERE memory_id = old.id;
    END""",
]
def _resolve_db_path() -> Path:
    """Return the absolute database path, creating parent dirs if absent."""
    db_path = get_settings().database.path
    if not db_path.is_absolute():
        from sage.config import _PROJECT_ROOT
        db_path = _PROJECT_ROOT / db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return db_path


def resolve_db_path() -> Path:
    return _resolve_db_path()


def get_sync_connection() -> sqlite3.Connection:
    """Return a synchronous SQLite connection with WAL mode enabled."""
    db_path = _resolve_db_path()
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    return conn


@asynccontextmanager
async def get_async_db() -> AsyncIterator[aiosqlite.Connection]:
    """Async context manager that yields a WAL-mode aiosqlite connection."""
    db_path = _resolve_db_path()
    db = await aiosqlite.connect(str(db_path))
    try:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute("PRAGMA busy_timeout=5000")
        await db.execute("PRAGMA synchronous=NORMAL")
        db.row_factory = aiosqlite.Row
        yield db
    finally:
        await db.close()


_MIGRATIONS: list[tuple[str, str, str]] = [
    ("conversations", "message_count", "INTEGER NOT NULL DEFAULT 0"),
    ("conversations", "last_message",  "TEXT NOT NULL DEFAULT ''"),
]


async def _run_column_migrations(db: aiosqlite.Connection) -> None:
    """Add any missing columns to existing tables (idempotent)."""
    for table, column, definition in _MIGRATIONS:
        try:
            await db.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
            )
            log.info("migration_applied", table=table, column=column)
        except Exception:
            pass


async def _run_fts5_migration(db: aiosqlite.Connection) -> None:
    """Create FTS5 table + triggers; backfill any rows missing from the index."""
    # Create virtual table and triggers.
    for stmt in _FTS5_SETUP:
        await db.execute(stmt)
    await db.execute(
        """
        INSERT INTO memories_fts(memory_id, content)
        SELECT m.id, m.content
        FROM memories m
        WHERE m.id NOT IN (SELECT memory_id FROM memories_fts)
        """
    )
    log.debug("fts5_migration_complete")

async def init_db() -> None:
    """Create all application tables and apply pending migrations."""
    async with get_async_db() as db:
        await db.executescript(_SCHEMA_SQL)
        await _run_column_migrations(db)
        await _run_fts5_migration(db)

        cursor = await db.execute("SELECT COUNT(*) FROM schema_version")
        row = await cursor.fetchone()
        if row is not None and row[0] == 0:
            await db.execute(
                "INSERT INTO schema_version (version) VALUES (?)",
                (_SCHEMA_VERSION,),
            )
        else:
            await db.execute(
                "UPDATE schema_version "
                "SET version = ?, applied_at = datetime('now') "
                "WHERE version < ?",
                (_SCHEMA_VERSION, _SCHEMA_VERSION),
            )
        await db.commit()

    log.info(
        "database_initialized",
        path=str(_resolve_db_path()),
        schema_version=_SCHEMA_VERSION,
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def upsert_conversation(
    thread_id: str,
    *,
    title: str | None = None,
    last_message: str = "",
    message_count_delta: int = 0,
) -> None:
    """Create or update a conversation row."""
    async with get_async_db() as db:
        cursor = await db.execute(
            "SELECT id, message_count FROM conversations WHERE id = ?",
            (thread_id,),
        )
        row = await cursor.fetchone()
        now = _now_iso()

        if row is None:
            await db.execute(
                "INSERT INTO conversations "
                "(id, title, last_message, message_count, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    thread_id,
                    title or "New Chat",
                    last_message[:200],
                    max(message_count_delta, 0),
                    now,
                    now,
                ),
            )
        else:
            existing_count = row[1] if row[1] else 0
            updates: list[str] = ["updated_at = ?"]
            params: list[Any] = [now]
            if title is not None:
                updates.append("title = ?")
                params.append(title)
            if last_message:
                updates.append("last_message = ?")
                params.append(last_message[:200])
            if message_count_delta > 0:
                updates.append("message_count = ?")
                params.append(existing_count + message_count_delta)
            params.append(thread_id)
            await db.execute(
                f"UPDATE conversations SET {', '.join(updates)} WHERE id = ?",
                params,
            )
        await db.commit()


async def list_conversations(limit: int = 50) -> list[dict[str, Any]]:
    """Return conversations ordered by recency."""
    async with get_async_db() as db:
        cursor = await db.execute(
            "SELECT id, title, last_message, message_count, updated_at "
            "FROM conversations WHERE is_archived = 0 "
            "ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "thread_id": r[0],
                "title": r[1],
                "last_message_preview": r[2] or "",
                "message_count": r[3] or 0,
                "updated_at": r[4] or "",
            }
            for r in rows
        ]


async def delete_conversation(thread_id: str) -> None:
    """Delete a conversation from the metadata table."""
    async with get_async_db() as db:
        await db.execute("DELETE FROM conversations WHERE id = ?", (thread_id,))
        await db.commit()


_FTS_STOP_WORDS: frozenset[str] = frozenset({
    "the", "a", "an", "is", "it", "in", "on", "at", "to", "for",
    "of", "and", "or", "but", "with", "my", "your", "what", "how",
    "why", "who", "me", "i", "am", "are", "was", "be", "have",
    "has", "do", "does", "did", "will", "would", "could", "should",
    "about", "from", "by", "as", "this", "that", "these", "those",
    "can", "its", "into", "than", "then", "them", "they", "their",
    "he", "she", "we", "you", "his", "her", "our", "also", "just",
})


def build_fts_query(text: str) -> str | None:
    """Convert plain-text query into an FTS5 MATCH string.
    
    Examples:
        - "what is my name?"  to  "name"
        - "explain quicksort algorithm"  to  "explain OR quicksort OR algorithm"
    """
    words = re.sub(r"[^\w\s]", " ", text.lower()).split()
    tokens = [w for w in words if w not in _FTS_STOP_WORDS and len(w) >= 3]
    if not tokens:
        tokens = [w for w in words if len(w) >= 3]
    if not tokens:
        return None
    return " OR ".join(tokens[:8])


async def search_memories_fts(
    query: str,
    k: int = 5,
    min_score: float = 0.0,
) -> list[dict[str, Any]]:
    """BM25 full-text search over stored memories.

    Args:
        query:     Plain-text search query (e.g. the user's message).
        k:         Maximum number of results to return.
        min_score: Minimum normalised score (0–1).  Results below this
                   threshold are excluded.  Default 0 keeps everything.

    Returns:
        List of memory dicts with keys: id, content, category,
        confidence, access_count, updated_at, score.
    """
    fts_query = build_fts_query(query)

    async with get_async_db() as db:
        if fts_query:
            try:
                cursor = await db.execute(
                    """
                    SELECT
                        m.id,
                        m.content,
                        m.category,
                        m.confidence,
                        m.access_count,
                        m.updated_at,
                        bm25(memories_fts) AS raw_score
                    FROM memories_fts
                    JOIN memories m ON memories_fts.memory_id = m.id
                    WHERE memories_fts MATCH ?
                    ORDER BY bm25(memories_fts)
                    LIMIT ?
                    """,
                    (fts_query, k * 3),
                )
                rows = await cursor.fetchall()
            except Exception as exc:
                log.warning("fts5_search_failed", error=str(exc)[:200])
                rows = []
        else:
            rows = []

        if not rows:
            cursor = await db.execute(
                """
                SELECT id, content, category, confidence, access_count,
                       updated_at, 0.0 AS raw_score
                FROM memories
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (k,),
            )
            rows = await cursor.fetchall()

    results: list[dict[str, Any]] = []
    for row in rows:
        # Normalise BM25 score.
        raw = float(row[6]) if row[6] else 0.0
        score = min(abs(raw) / 10.0, 1.0) if raw < 0 else 0.5
        if score < min_score:
            continue
        results.append({
            "id": str(row[0]),
            "content": str(row[1]),
            "category": str(row[2]),
            "confidence": float(row[3]),
            "access_count": int(row[4]),
            "updated_at": str(row[5]),
            "score": round(score, 4),
        })

    return results[:k]


async def insert_memory(
    content: str,
    category: str,
    confidence: float = 1.0,
) -> str:
    """Insert a new memory and return its ID."""
    mem_id = f"mem_{uuid4().hex[:12]}"
    async with get_async_db() as db:
        await db.execute(
            "INSERT INTO memories (id, content, category, confidence) "
            "VALUES (?, ?, ?, ?)",
            (mem_id, content, category, confidence),
        )
        await db.commit()
    return mem_id


async def update_memory_timestamp(memory_id: str) -> None:
    """Bump the updated_at and access_count for an existing memory."""
    async with get_async_db() as db:
        await db.execute(
            "UPDATE memories "
            "SET updated_at = datetime('now'), access_count = access_count + 1 "
            "WHERE id = ?",
            (memory_id,),
        )
        await db.commit()


async def get_all_memories() -> list[dict[str, Any]]:
    """Return all memories ordered by recency (no embeddings)."""
    async with get_async_db() as db:
        cursor = await db.execute(
            "SELECT id, content, category, confidence, access_count, updated_at "
            "FROM memories ORDER BY updated_at DESC"
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": r[0],
                "content": r[1],
                "category": r[2],
                "confidence": r[3],
                "access_count": r[4],
                "updated_at": r[5],
            }
            for r in rows
        ]


async def get_recent_memory_contents(limit: int = 100) -> list[dict[str, Any]]:
    """Return id + content for the most recent memories.

    Used by deduplicate_and_store() for Jaccard similarity checks.
    Fetching only the two text columns keeps the query lightweight.
    """
    async with get_async_db() as db:
        cursor = await db.execute(
            "SELECT id, content FROM memories ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [{"id": r[0], "content": r[1]} for r in rows]


async def get_memory_count() -> int:
    """Return total number of stored memories."""
    async with get_async_db() as db:
        cursor = await db.execute("SELECT COUNT(*) FROM memories")
        row = await cursor.fetchone()
        return row[0] if row else 0


async def delete_oldest_memories(keep: int) -> int:
    """Delete memories beyond the keep limit, oldest first.

    The memories_fts_ad trigger propagates deletions to the FTS index
    automatically; no manual FTS cleanup is needed here.

    Returns:
        Number of rows deleted.
    """
    async with get_async_db() as db:
        cursor = await db.execute(
            "DELETE FROM memories WHERE id NOT IN "
            "(SELECT id FROM memories ORDER BY updated_at DESC LIMIT ?)",
            (keep,),
        )
        deleted = cursor.rowcount
        await db.commit()
    return deleted