import pytest
import sqlite3
import aiosqlite
from pathlib import Path
from sage import database
from unittest.mock import patch, MagicMock

@pytest.mark.asyncio
async def test_database_init():
    async with database.get_async_db() as db:
        cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in await cursor.fetchall()]
        assert "conversations" in tables
        assert "memories" in tables
        assert "schema_version" in tables
        assert "memories_fts" in tables

def test_resolve_db_path():
    path = database.resolve_db_path()
    assert isinstance(path, Path)

def test_get_sync_connection():
    conn = database.get_sync_connection()
    assert isinstance(conn, sqlite3.Connection)
    conn.close()

@pytest.mark.asyncio
async def test_conversations_crud():
    await database.upsert_conversation("test_thread", title="My Title", last_message="Hello", message_count_delta=1)
    convs = await database.list_conversations()
    assert len(convs) >= 1
    
    found = False
    for c in convs:
        if c["thread_id"] == "test_thread":
            assert c["title"] == "My Title"
            assert c["last_message_preview"] == "Hello"
            assert c["message_count"] == 1
            found = True
            
    assert found
    
    await database.upsert_conversation("test_thread", title="Updated", last_message="World", message_count_delta=2)
    convs = await database.list_conversations()
    for c in convs:
        if c["thread_id"] == "test_thread":
            assert c["title"] == "Updated"
            assert c["last_message_preview"] == "World"
            assert c["message_count"] == 3
            break

    await database.delete_conversation("test_thread")
    convs = await database.list_conversations()
    for c in convs:
        assert c["thread_id"] != "test_thread"

def test_build_fts_query():
    assert database.build_fts_query("hello") == "hello"
    assert database.build_fts_query("what is my name?") == "name"
    assert database.build_fts_query("a" * 10) == "aaaaaaaaaa"
    assert database.build_fts_query("a" * 2) is None

@pytest.mark.asyncio
async def test_memories_crud():
    mem_id = await database.insert_memory("Test memory content", "test_cat", 0.9)
    assert mem_id.startswith("mem_")
    
    mems = await database.get_all_memories()
    assert any(m["id"] == mem_id for m in mems)
    
    recents = await database.get_recent_memory_contents()
    assert any(m["id"] == mem_id for m in recents)
    
    count = await database.get_memory_count()
    assert count > 0
    
    await database.update_memory_timestamp(mem_id)
    
    fts_results = await database.search_memories_fts("Test memory content", min_score=0)
    assert any(m["id"] == mem_id for m in fts_results)

    empty_results = await database.search_memories_fts("a b c", min_score=0)
    
    deleted = await database.delete_oldest_memories(0)
    assert deleted > 0
    
    mems = await database.get_all_memories()
    assert len(mems) == 0
