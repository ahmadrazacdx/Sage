"""
Chat submission and Server-Sent Events streaming endpoints.

Protocol:
    1. Frontend  POST /api/chat receives {thread_id, message_id}
    2. Frontend  GET  /api/stream/{thread_id}  gets SSE event stream
       Events: chunk | tool_call | thinking | done | error

Rationale:
    - `POST /api/chat` validates input and stores a "pending stream" keyed by `thread_id`.
    - `GET /api/stream/{thread_id}` pops the pending entry, runs the
      LangGraph agent graph via `astream_events(version="v2")`, and
      yields SSE events.
    - Concurrency guard: only one active stream per `thread_id`.
    - The generator always emits `{type: "done" | "error"}`, the frontend uses it to close `EventSource`.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Request
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, Field
from starlette.responses import StreamingResponse

from sage.agents import VALID_INTENTS

log = structlog.get_logger(__name__)

router = APIRouter(tags=["chat"])


_REALTIME_STREAM_NODES: frozenset[str] = frozenset({
    "general", "reasoning", "thinking",
})

_BATCH_EMIT_INTENTS: frozenset[str] = frozenset({
    "explain", "diagram", "quiz", "roadmap", "research", "fix",
})

_STALE_PENDING_TTL: float = 60.0

class ChatRequest(BaseModel):
    thread_id: str | None = None
    message: str = Field(..., min_length=1, max_length=2000)
    mode: str
    course: str = "all"


class ChatResponse(BaseModel):
    thread_id: str
    message_id: str

def _short_id() -> str:
    return uuid.uuid4().hex[:8]


def _sse(data: dict[str, Any]) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _title_from_message(text: str) -> str:
    """Derive a short title from the first user message."""
    words = text.split()
    title = " ".join(words[:6])
    if len(words) > 6:
        title += "…"
    return title


def _build_lc_history(
    stored: list[dict[str, str]],
) -> list[HumanMessage | AIMessage]:
    """Convert plain dicts to LangChain message objects."""
    out: list[HumanMessage | AIMessage] = []
    for m in stored:
        if m["role"] == "user":
            out.append(HumanMessage(content=m["content"]))
        else:
            out.append(AIMessage(content=m["content"]))
    return out


# POST
@router.post("/chat", response_model=ChatResponse)
async def submit_chat(body: ChatRequest, request: Request) -> ChatResponse:
    """Validate a user message and prepare a pending stream."""
    if not getattr(request.app.state, "model_ready", False):
        raise HTTPException(
            status_code=503,
            detail="Model not ready. Please wait.",
        )
    if body.mode not in VALID_INTENTS:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid mode '{body.mode}'. "
            f"Expected one of: {', '.join(sorted(VALID_INTENTS))}",
        )

    thread_id = body.thread_id or _short_id()
    message_id = _short_id()

    pending: dict[str, dict[str, Any]] = request.app.state.pending_streams
    active: dict[str, bool] = request.app.state.active_streams

    if thread_id in pending:
        raise HTTPException(
            status_code=409,
            detail="A pending stream already exists for this thread. "
                   "Connect to /api/stream/{thread_id} to consume it first.",
        )
    if active.get(thread_id):
        raise HTTPException(
            status_code=409,
            detail="A stream is already active for this thread.",
        )
    
    # Build LangGraph input state
    history: list[dict[str, str]] = request.app.state.thread_messages.get(
        thread_id, []
    )
    lc_messages = _build_lc_history(history)
    lc_messages.append(HumanMessage(content=body.message))

    network = getattr(request.app.state, "network", None)

    state_input: dict[str, Any] = {
        "messages": lc_messages,
        "query": body.message,
        "mode": body.mode,
        "course_code": body.course if body.course != "all" else None,
        "online_mode": network.online if network is not None else False,
    }

    pending[thread_id] = {
        "state_input": state_input,
        "user_message": body.message,
        "intent": body.mode,
        "created_at": asyncio.get_event_loop().time(),
    }

    return ChatResponse(thread_id=thread_id, message_id=message_id)


# GET
@router.get("/stream/{thread_id}")
async def stream_response(thread_id: str, request: Request) -> StreamingResponse:
    """SSE endpoint: runs the agent graph and streams token events."""

    active: dict[str, bool] = request.app.state.active_streams
    pending: dict[str, dict[str, Any]] = request.app.state.pending_streams

    if active.get(thread_id):
        raise HTTPException(
            status_code=409,
            detail="Stream already active for this thread.",
        )

        now = asyncio.get_event_loop().time()
    stale = [
        tid for tid, e in pending.items()
        if now - e.get("created_at", now) > _STALE_PENDING_TTL
        and tid != thread_id
    ]
    for tid in stale:
        pending.pop(tid, None)
        log.info("pending_stream_evicted", evicted_thread_id=tid)
 
    entry = pending.pop(thread_id, None)
    if entry is None:
        raise HTTPException(
            status_code=404,
            detail="No pending stream for this thread.",
        )
 
    active[thread_id] = True

    graph = request.app.state.graph
    state_input: dict[str, Any] = entry["state_input"]
    user_message: str = entry["user_message"]
    intent: str = entry["intent"]
    is_batch: bool = intent in _BATCH_EMIT_INTENTS

    async def _generate():  # noqa: C901
        accumulated: list[str] = []

        try:
            if is_batch:
                invoke_task = asyncio.create_task(
                    graph.ainvoke(state_input)
                )
                while not invoke_task.done():
                    await asyncio.sleep(5)
                    if not invoke_task.done():
                        yield ": keepalive\n\n"

                final_state: dict[str, Any] = await invoke_task
                response_text: str = final_state.get("response", "")
                if response_text:
                    accumulated.append(response_text)
                    yield _sse({"type": "chunk", "text": response_text})

            else:
                # Real-time streaming path (general / thinking)
                async for event in graph.astream_events(state_input, version="v2"):
                    kind: str = event["event"]
                    meta: dict[str, Any] = event.get("metadata", {})
                    node: str = meta.get("langgraph_node", "")

                    if kind == "on_tool_start":
                        tool_name = event.get("name", "")
                        if tool_name:
                            yield _sse({"type": "tool_call", "name": tool_name})
                        continue

                    if (
                        kind == "on_chat_model_stream"
                        and node in _REALTIME_STREAM_NODES
                    ):
                        chunk = event.get("data", {}).get("chunk")
                        if chunk is None:
                            continue
                        text: str = getattr(chunk, "content", "") or ""
                        if not text:
                            continue
                        accumulated.append(text)
                        yield _sse({"type": "chunk", "text": text})

            # Persist to in-memory session store
            final_content = "".join(accumulated)
            messages: dict[str, list[dict[str, str]]] = getattr(
                request.app.state, "thread_messages", {}
            )
            thread_msgs = messages.setdefault(thread_id, [])
            thread_msgs.append({"role": "user", "content": user_message})
            if final_content:
                thread_msgs.append({"role": "assistant", "content": final_content})

            thread_meta: dict[str, dict[str, Any]] = getattr(
                request.app.state, "thread_meta", {}
            )
            existing = thread_meta.get(thread_id, {})
            thread_meta[thread_id] = {
                "thread_id": thread_id,
                "title": existing.get("title") or _title_from_message(user_message),
                "last_message_preview": (final_content or "")[:100],
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }

            yield _sse({"type": "done"})

        except asyncio.CancelledError:
            log.info("stream_cancelled", thread_id=thread_id)
            raise

        except Exception as exc:
            log.error(
                "stream_error",
                thread_id=thread_id,
                exc_type=type(exc).__name__,
                error=str(exc)[:500],
            )
            yield _sse({"type": "error", "message": str(exc)[:500]})

        finally:
            active.pop(thread_id, None)

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )

