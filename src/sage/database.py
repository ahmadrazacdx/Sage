"""
SQLite lifecycle manager for Sage.

Provides:
  - get_async_db()    : async context manager for aiosqlite connections
  - init_db()         : creates application tables on first run
  - get_sync_connection() : sync connection factory for non-async contexts

All connections use WAL mode for concurrent read/write support.
Schema covers only application metadata (conversations, corpus index,
user settings).

Usage:

    async with get_async_db() as db:
        await db.execute("INSERT INTO conversations ...")

    # One-time at startup:
    await init_db()
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite
import structlog

from sage.config import get_settings

log = structlog.get_logger(__name__)

# --- Schema ---

_SCHEMA_VERSION: int = 1

_SCHEMA_SQL: str = """
-- Version tracking for future migrations.
CREATE TABLE IF NOT EXISTS schema_version (
    version   INTEGER NOT NULL,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Conversation metadata.  Message history is stored by
-- LangGraph's SqliteSaver (separate phase); this table
-- tracks only the lightweight index shown in the UI sidebar.
CREATE TABLE IF NOT EXISTS conversations (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT 'New Chat',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    is_archived INTEGER NOT NULL DEFAULT 0
);

-- Corpus file index.  Tracks which files have been ingested
-- into ChromaDB so the ingestion pipeline can detect changes
-- and avoid re-processing unchanged files.
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

-- Indices for sidebar queries (conversations sorted by recency).
CREATE INDEX IF NOT EXISTS idx_conversations_updated
    ON conversations (updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_corpus_files_collection
    ON corpus_files (collection);
"""


def _resolve_db_path() -> Path:
    """Return the absolute database path, creating parent dirs if absent."""
    db_path = get_settings().database.path
    if not db_path.is_absolute():
        from sage.config import _PROJECT_ROOT

        db_path = _PROJECT_ROOT / db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return db_path


def get_sync_connection() -> sqlite3.Connection:
    """Return a synchronous SQLite connection with WAL mode enabled."""
    db_path = _resolve_db_path()
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


@asynccontextmanager
async def get_async_db() -> AsyncIterator[aiosqlite.Connection]:
    """Async context manager that yields a WAL-mode aiosqlite connection.

    Usage:

        async with get_async_db() as db:
            cursor = await db.execute("SELECT * FROM conversations")
            rows = await cursor.fetchall()
    """
    db_path = _resolve_db_path()
    db = await aiosqlite.connect(str(db_path))
    try:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute("PRAGMA busy_timeout=5000")
        db.row_factory = aiosqlite.Row
        yield db
    finally:
        await db.close()


# --- Initialisation ---
async def init_db() -> None:
    """Create all application tables if they do not already exist."""
    async with get_async_db() as db:
        await db.executescript(_SCHEMA_SQL)
        cursor = await db.execute("SELECT COUNT(*) FROM schema_version")
        row = await cursor.fetchone()
        if row is not None and row[0] == 0:
            await db.execute(
                "INSERT INTO schema_version (version) VALUES (?)",
                (_SCHEMA_VERSION,),
            )
        await db.commit()

    log.info(
        "database_initialized",
        path=str(_resolve_db_path()),
        schema_version=_SCHEMA_VERSION,
    )
