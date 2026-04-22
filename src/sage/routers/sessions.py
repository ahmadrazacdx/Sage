"""
Conversation session management endpoints.

All data is held **in-memory** for the MVP.  The persistence layer
(AsyncSqliteSaver + LangGraph checkpointing) is scheduled for the
next development phase.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

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
    """Return at most 50 conversations, newest first."""
    meta: dict[str, dict[str, Any]] = getattr(
        request.app.state, "thread_meta", {}
    )
    items = sorted(meta.values(), key=lambda s: s["updated_at"], reverse=True)
    return [Session(**s) for s in items[:50]]


@router.get("/sessions/{thread_id}/messages", response_model=list[Message])
async def get_session_messages(
    thread_id: str,
    request: Request,
) -> list[Message]:
    """Load the full message history for a past conversation."""
    messages: dict[str, list[dict[str, Any]]] = getattr(
        request.app.state, "thread_messages", {}
    )
    thread = messages.get(thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    return [Message(**m) for m in thread]


@router.delete("/sessions/{thread_id}", status_code=204)
async def delete_session(thread_id: str, request: Request) -> None:
    """Delete a conversation and its messages from memory."""
    getattr(request.app.state, "thread_messages", {}).pop(thread_id, None)
    getattr(request.app.state, "thread_meta", {}).pop(thread_id, None)
