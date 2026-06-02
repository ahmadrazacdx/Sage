"""
Conversation session management endpoints.

Persistence is fully SQLite-backed:
  - Conversation metadata: `conversations` table via database.py helpers
  - Message history: LangGraph AsyncSqliteSaver checkpointer
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from sage.database import delete_conversation, list_conversations

log = structlog.get_logger(__name__)
router = APIRouter(tags=["sessions"])


class Session(BaseModel):
    thread_id: str
    title: str
    last_message_preview: str
    updated_at: str


class Message(BaseModel):
    role: str
    content: str
    artifact: dict[str, str] | None = None


@router.get("/sessions", response_model=list[Session])
async def list_sessions(request: Request) -> list[Session]:
    """Return conversations from the database, newest first."""
    conversations = await list_conversations(limit=50)
    return [Session(**c) for c in conversations]


@router.get("/sessions/{thread_id}/messages", response_model=list[Message])
async def get_session_messages(
    thread_id: str,
    request: Request,
) -> list[Message]:
    """Load the full message history for a past conversation.

    Reads from the LangGraph checkpointer if available, else falls
    back to the in-memory thread_messages dict (for active sessions
    that haven't been checkpointed yet).
    """
    # Try checkpointer first (persisted state).
    checkpointer = getattr(request.app.state, "checkpointer", None)
    if checkpointer is not None:
        try:
            config = {"configurable": {"thread_id": thread_id}}
            state = await checkpointer.aget_tuple(config)
            if state is not None and state.checkpoint is not None:
                values = state.checkpoint.get("channel_values", {})
                messages = values.get("messages", [])
                result: list[Message] = []
                for m in messages:
                    role = "user" if getattr(m, "type", "") == "human" else "assistant"
                    content = getattr(m, "content", "") or ""
                    if isinstance(content, list):
                        content = " ".join(str(c.get("text", c) if isinstance(c, dict) else c) for c in content)
                    result.append(Message(role=role, content=str(content)))
                if result:
                    return result
        except Exception:
            pass

    # Fallback to in-memory (for backward compat during transition).
    thread_messages: dict[str, list[dict[str, Any]]] = getattr(request.app.state, "thread_messages", {})
    thread = thread_messages.get(thread_id)
    if thread is not None:
        return [Message(**m) for m in thread]

    raise HTTPException(status_code=404, detail="Session not found.")


@router.delete("/sessions/{thread_id}", status_code=204)
async def delete_session(thread_id: str, request: Request) -> None:
    """Delete a conversation from the metadata table, checkpointer, and memory cache."""
    checkpointer = getattr(request.app.state, "checkpointer", None)
    if checkpointer is not None:
        try:
            conn = checkpointer.conn
            cur = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            all_tables = [row[0] for row in await cur.fetchall()]

            checkpoint_tables: list[str] = []
            for table in all_tables:
                col_cur = await conn.execute(f"PRAGMA table_info({table})")
                cols = [row[1] for row in await col_cur.fetchall()]
                if "thread_id" in cols:
                    checkpoint_tables.append(table)

            for table in checkpoint_tables:
                await conn.execute(
                    f"DELETE FROM {table} WHERE thread_id = ?",  # noqa: S608
                    (thread_id,),
                )
            await conn.execute("DELETE FROM conversations WHERE id = ?", (thread_id,))
            await conn.commit()
        except Exception as exc:
            log.warning(
                "session_delete_failed",
                thread_id=thread_id,
                error=str(exc)[:200],
            )
            try:
                await delete_conversation(thread_id)
            except Exception as exc2:
                log.warning(
                    "conversation_delete_fallback_failed",
                    thread_id=thread_id,
                    error=str(exc2)[:200],
                )
    else:
        await delete_conversation(thread_id)

    # ALways clean in-memory cache.
    getattr(request.app.state, "thread_messages", {}).pop(thread_id, None)
