"""
Chat submission and Server-Sent Events streaming endpoints.

Protocol:
    1. Frontend  POST /api/chat receives {thread_id, message_id}
    2. Frontend  GET  /api/stream/{thread_id}  gets SSE event stream
       Events: chunk | node_start | tool_call | thinking | artifact | heartbeat | done | error

Rationale:
    - `POST /api/chat` validates input and stores a "pending stream" keyed by `thread_id`.
    - `GET /api/stream/{thread_id}` pops the pending entry, runs the
      LangGraph agent graph via `astream_events(version="v2")`, and
      yields SSE events for EVERY path (streaming and batch alike).
    - node_start events are emitted when each graph node begins, this drives the frontend progress timeline for all agentic modes.
    - For batch intents the LLM token stream is suppressed; only the
      final response chunk is emitted after the graph completes.
    - Concurrency guard: only one active stream per `thread_id`.
    - Always emits `{type: "done" | "error"}` at the end.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Request
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, Field
from starlette.responses import StreamingResponse

from sage.agents import VALID_INTENTS
from sage.utils import close_unbalanced_fenced_blocks, strip_think_markers

log = structlog.get_logger(__name__)

router = APIRouter(tags=["chat"])

_BATCH_INTENTS: frozenset[str] = frozenset(
    {
        "explain",
        "diagram",
        "quiz",
        "roadmap",
        "research",
        "fix",
    }
)
_NON_STREAMING_INTENTS: frozenset[str] = _BATCH_INTENTS
_TYPEWRITER_INTENTS: frozenset[str] = frozenset({"explain"})

_STALE_PENDING_TTL: float = 60.0
_SSE_HEARTBEAT_INTERVAL_S: float = 10.0
_TYPEWRITER_TARGET_CHARS_PER_CHUNK: int = 28
_TYPEWRITER_MIN_TOTAL_DELAY_S: float = 0.25
_TYPEWRITER_MAX_TOTAL_DELAY_S: float = 1.20

_NODE_LABELS: dict[str, str] = {
    "router": "🧭 Routing request…",
    "retrieval": "📚 Retrieving course materials…",
    "reasoning": "🧠 Reasoning through content…",
    "response_generator": "✍️ Formatting response…",
    "quiz": "🧩 Generating quiz…",
    "diagram": "📊 Building diagram…",
    "planner": "📅 Building study plan…",
    "research": "🔬 Researching topic…",
    "code_fix": "🔧 Analysing code…",
    "general": "💬 Generating answer…",
}

# Tool labels
_TOOL_LABELS: dict[str, str] = {
    "validate_mermaid": "🔍 Validating diagram syntax…",
    "render_mermaid_svg": "🖼️ Rendering diagram…",
    "search_arxiv": "📄 Searching arXiv…",
    "search_web": "🌐 Searching the web…",
    "search_wikipedia": "📖 Searching Wikipedia…",
    "calculator": "🔢 Running calculation…",
    "execute_python": "⚙️ Executing code…",
    "export_pdf": "📋 Generating PDF report…",
    "export_markdown": "📝 Saving markdown…",
    "corpus_search": "📚 Searching course materials…",
}

_SKIP_NODES: frozenset[str] = frozenset({"router"})
_RE_THINK_BLOCK = re.compile(r"<think>(.*?)</think>", re.IGNORECASE | re.DOTALL)
_THINK_OPEN_TAG = "<think>"
_THINK_CLOSE_TAG = "</think>"


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


def _canonical_tool_name(tool_name: str) -> str:
    """Normalize third-party tool names to stable UI-facing names."""
    raw = (tool_name or "").strip()
    key = raw.lower()

    aliases: dict[str, str] = {
        "duckduckgo_results_json": "search_web",
        "duckduckgo_results": "search_web",
        "duckduckgo_search": "search_web",
    }
    if key in aliases:
        return aliases[key]
    if "duckduckgo" in key:
        return "search_web"
    return raw


def _tool_label(canonical_name: str, raw_name: str) -> str:
    """Return a readable progress label for known and unknown tools."""
    if canonical_name in _TOOL_LABELS:
        return _TOOL_LABELS[canonical_name]

    lowered = (raw_name or canonical_name).lower()
    if "wiki" in lowered:
        return _TOOL_LABELS.get("search_wikipedia", "📖 Searching Wikipedia…")
    if "arxiv" in lowered:
        return _TOOL_LABELS.get("search_arxiv", "📄 Searching arXiv…")
    if "search" in lowered:
        return _TOOL_LABELS.get("search_web", "🌐 Searching the web…")
    return "⚙️ Running tool…"


def _resolve_node_name(event: dict[str, Any], meta: dict[str, Any]) -> str:
    """Resolve graph node name from LangGraph v2 event payloads."""
    node = str(meta.get("langgraph_node", "") or "").strip()
    if node:
        return node

    event_name = str(event.get("name", "") or "").strip()
    if event_name in _NODE_LABELS:
        return event_name
    return ""


def _split_for_typewriter(text: str) -> list[str]:
    """Split final response into readable chunks for gradual UI reveal."""
    if not text:
        return []

    chunks: list[str] = []
    parts = re.split(r"(\s+)", text)
    buffer: list[str] = []
    buffer_len = 0

    for part in parts:
        if not part:
            continue
        buffer.append(part)
        buffer_len += len(part)

        if buffer_len < _TYPEWRITER_TARGET_CHARS_PER_CHUNK:
            continue

        should_flush = part.isspace() or part.endswith((".", "!", "?", ",", ";", ":"))
        if should_flush:
            chunks.append("".join(buffer))
            buffer = []
            buffer_len = 0

    if buffer:
        chunks.append("".join(buffer))
    return chunks


def _typewriter_delay_seconds(text_len: int, chunk_count: int) -> float:
    """Compute a tiny per-chunk delay while keeping total added latency bounded."""
    if text_len <= 0 or chunk_count <= 1:
        return 0.0

    total_delay = max(
        _TYPEWRITER_MIN_TOTAL_DELAY_S,
        min(_TYPEWRITER_MAX_TOTAL_DELAY_S, text_len / 1500.0),
    )
    return total_delay / chunk_count


def _split_thinking_response(text: str) -> tuple[str, str]:
    """Split combined '<think>...</think> + answer' payload into two channels."""
    if not text:
        return "", ""

    traces = [block.strip() for block in _RE_THINK_BLOCK.findall(text) if block and block.strip()]

    visible = _RE_THINK_BLOCK.sub("", text)
    visible = visible.replace("<think>", "").replace("</think>", "").strip()

    # Handle malformed output where only a closing tag is present.
    if not traces and "</think>" in text:
        before, _sep, after = text.partition("</think>")
        fallback_trace = before.replace("<think>", "").strip()
        if fallback_trace:
            traces = [fallback_trace]
        visible = after.replace("<think>", "").replace("</think>", "").strip()

    return "\n\n".join(traces), visible


def _strip_partial_tag_suffix(text: str, tag: str) -> tuple[str, str]:
    """Keep trailing partial tag bytes in carry so split tags are not leaked."""
    max_overlap = min(len(text), len(tag) - 1)
    for overlap in range(max_overlap, 0, -1):
        if text.endswith(tag[:overlap]):
            return text[:-overlap], text[-overlap:]
    return text, ""


def _consume_thinking_chunk(
    text: str,
    *,
    in_think: bool,
    seen_think_marker: bool,
) -> tuple[list[str], list[str], bool, str, bool]:
    """Split streamed model text into thinking and visible-answer fragments."""
    thinking_parts: list[str] = []
    answer_parts: list[str] = []
    remaining = text
    carry = ""

    while remaining:
        if in_think:
            close_idx = remaining.find(_THINK_CLOSE_TAG)
            if close_idx == -1:
                safe, carry = _strip_partial_tag_suffix(remaining, _THINK_CLOSE_TAG)
                if safe:
                    thinking_parts.append(safe)
                return thinking_parts, answer_parts, in_think, carry, seen_think_marker

            if close_idx > 0:
                thinking_parts.append(remaining[:close_idx])
            remaining = remaining[close_idx + len(_THINK_CLOSE_TAG) :]
            in_think = False
            seen_think_marker = True
            continue

        open_idx = remaining.find(_THINK_OPEN_TAG)
        close_idx = remaining.find(_THINK_CLOSE_TAG)

        # Handle malformed output where thinking text ends with a stray closing marker.
        if open_idx == -1 and close_idx != -1 and not seen_think_marker:
            if close_idx > 0:
                thinking_parts.append(remaining[:close_idx])
            remaining = remaining[close_idx + len(_THINK_CLOSE_TAG) :]
            seen_think_marker = True
            continue

        if open_idx == -1:
            safe, carry = _strip_partial_tag_suffix(remaining, _THINK_OPEN_TAG)
            if safe:
                answer_parts.append(safe)
            return thinking_parts, answer_parts, in_think, carry, seen_think_marker

        if open_idx > 0:
            answer_parts.append(remaining[:open_idx])
        remaining = remaining[open_idx + len(_THINK_OPEN_TAG) :]
        in_think = True
        seen_think_marker = True

    return thinking_parts, answer_parts, in_think, carry, seen_think_marker


def _flush_thinking_carry(carry: str, *, in_think: bool) -> tuple[str, str]:
    """Finalize buffered trailing data at end of stream."""
    if not carry:
        return "", ""

    cleaned = carry.replace(_THINK_OPEN_TAG, "").replace(_THINK_CLOSE_TAG, "")
    if not cleaned:
        return "", ""
    if in_think:
        return cleaned, ""

    # Drop an incomplete opening/closing tag fragment if it is only marker bytes.
    lower_cleaned = cleaned.lower().strip()
    if _THINK_OPEN_TAG.startswith(lower_cleaned) or _THINK_CLOSE_TAG.startswith(lower_cleaned):
        return "", ""
    return "", cleaned


def _title_from_message(text: str) -> str:
    """Derive a short title from the first user message."""
    words = text.split()
    title = " ".join(words[:6])
    if len(words) > 6:
        title += "…"
    return title


def _sanitize_markdown_response(text: str, *, strip_think: bool = True) -> str:
    """Normalize assistant markdown to avoid renderer breakage in the UI."""
    cleaned = text or ""
    if strip_think:
        cleaned = strip_think_markers(cleaned)
    cleaned = close_unbalanced_fenced_blocks(cleaned)
    return cleaned


def _artifact_payload(raw: dict[str, Any]) -> dict[str, str] | None:
    """Normalize one artifact record for SSE and session history."""
    if not isinstance(raw, dict):
        return None
    path = str(raw.get("path", "") or "").strip()
    filename = str(raw.get("filename", "") or "").strip()
    if not filename and path:
        filename = Path(path).name
    if not filename:
        return None
    kind = str(raw.get("kind", "file") or "file")
    return {
        "kind": kind,
        "filename": filename,
        "path": path,
        "url": f"/api/artifacts/{filename}",
    }


def _build_lc_history(
    stored: list[dict[str, Any]],
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
    history: list[dict[str, Any]] = request.app.state.thread_messages.get(thread_id, [])
    lc_messages = _build_lc_history(history)
    lc_messages.append(HumanMessage(content=body.message))

    network = getattr(request.app.state, "network", None)

    state_input: dict[str, Any] = {
        "messages": lc_messages,
        "query": body.message,
        "mode": body.mode,
        "course_code": body.course if body.course != "all" else None,
        "online_mode": network.online if network is not None else False,
        "thread_id": thread_id,
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
    """SSE endpoint: runs the agent graph and streams events."""

    active: dict[str, bool] = request.app.state.active_streams
    pending: dict[str, dict[str, Any]] = request.app.state.pending_streams

    if active.get(thread_id):
        raise HTTPException(
            status_code=409,
            detail="Stream already active for this thread.",
        )

    now = asyncio.get_event_loop().time()
    stale = [
        tid
        for tid, e in pending.items()
        if now - e.get("created_at", now) > _STALE_PENDING_TTL and tid != thread_id
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
    stream_tokens: bool = intent not in _NON_STREAMING_INTENTS

    async def _generate():  # noqa: C901
        accumulated_chunks: list[str] = []
        emitted_nodes: set[str] = set()
        final_state_from_events: dict[str, Any] | None = None
        latest_artifact: dict[str, str] | None = None
        thinking_in_block = False
        seen_think_marker = False
        thinking_carry = ""

        try:
            event_iter = graph.astream_events(state_input, version="v2").__aiter__()
            while True:
                try:
                    event = await asyncio.wait_for(
                        event_iter.__anext__(),
                        timeout=_SSE_HEARTBEAT_INTERVAL_S,
                    )
                except TimeoutError:
                    yield _sse({"type": "heartbeat"})
                    continue
                except StopAsyncIteration:
                    break

                kind: str = event["event"]
                meta: dict[str, Any] = event.get("metadata", {})
                node = _resolve_node_name(event, meta)

                if kind == "on_chain_start" and node and node not in _SKIP_NODES:
                    if node not in emitted_nodes:
                        emitted_nodes.add(node)
                        label = _NODE_LABELS.get(node, f"⚙️ {node}…")
                        yield _sse({"type": "node_start", "node": node, "label": label})

                elif kind == "on_tool_start":
                    raw_tool_name: str = event.get("name", "")
                    if raw_tool_name:
                        canonical = _canonical_tool_name(raw_tool_name)
                        label = _tool_label(canonical, raw_tool_name)
                        yield _sse(
                            {
                                "type": "tool_call",
                                "name": canonical,
                                "raw_name": raw_tool_name,
                                "label": label,
                            }
                        )
                elif kind == "on_chat_model_stream" and stream_tokens:
                    chunk = event.get("data", {}).get("chunk")
                    if chunk is None:
                        continue
                    text: str = getattr(chunk, "content", "") or ""
                    if not text:
                        continue
                    if intent == "thinking":
                        merged = f"{thinking_carry}{text}"
                        _, answer_parts, thinking_in_block, thinking_carry, seen_think_marker = (
                            _consume_thinking_chunk(
                                merged,
                                in_think=thinking_in_block,
                                seen_think_marker=seen_think_marker,
                            )
                        )

                        answer_text = "".join(answer_parts)
                        if answer_text:
                            accumulated_chunks.append(answer_text)
                            yield _sse({"type": "chunk", "text": answer_text})
                    else:
                        accumulated_chunks.append(text)
                        yield _sse({"type": "chunk", "text": text})
                elif kind == "on_chain_end":
                    output = event.get("data", {}).get("output")
                    if isinstance(output, dict) and (
                        "response" in output or "artifact_paths" in output
                    ):
                        final_state_from_events = output

            final_state: dict[str, Any] | None = final_state_from_events

            if intent == "thinking" and stream_tokens:
                _, tail_answer = _flush_thinking_carry(
                    thinking_carry,
                    in_think=thinking_in_block,
                )
                if tail_answer:
                    accumulated_chunks.append(tail_answer)
                    yield _sse({"type": "chunk", "text": tail_answer})

            if stream_tokens and not accumulated_chunks and final_state is not None:
                response_text = str(final_state.get("response", "") or "")
                if response_text:
                    if intent == "thinking":
                        _, visible_answer = _split_thinking_response(response_text)
                        if visible_answer:
                            accumulated_chunks.append(visible_answer)
                            yield _sse({"type": "chunk", "text": visible_answer})
                    else:
                        response_text = _sanitize_markdown_response(response_text)
                        accumulated_chunks.append(response_text)
                        yield _sse({"type": "chunk", "text": response_text})

                for art in final_state.get("artifact_paths", []):
                    payload = _artifact_payload(art)
                    if payload is None:
                        continue
                    latest_artifact = payload
                    yield _sse({"type": "artifact", **payload})

            if not stream_tokens:
                if final_state is None:
                    log.warning(
                        "stream_missing_final_state",
                        thread_id=thread_id,
                        intent=intent,
                    )
                    final_state = await graph.ainvoke(state_input)

                response_text = str(final_state.get("response", "") or "")
                if response_text and intent != "thinking":
                    response_text = _sanitize_markdown_response(response_text)
                artifact_paths = final_state.get("artifact_paths", [])

                if intent == "research" and artifact_paths:
                    short_msg = response_text or (
                        "I have completed your research report. "
                        "Use the download button below to get the PDF."
                    )
                    accumulated_chunks = [short_msg]
                    yield _sse({"type": "chunk", "text": short_msg})
                elif intent == "thinking":
                    _, visible_answer = _split_thinking_response(response_text)
                    if visible_answer:
                        accumulated_chunks = [visible_answer]
                        yield _sse({"type": "chunk", "text": visible_answer})
                elif response_text and intent in _TYPEWRITER_INTENTS:
                    chunks = _split_for_typewriter(response_text)
                    delay_s = _typewriter_delay_seconds(len(response_text), len(chunks))
                    accumulated_chunks = []
                    for chunk in chunks:
                        accumulated_chunks.append(chunk)
                        yield _sse({"type": "chunk", "text": chunk})
                        if delay_s > 0:
                            await asyncio.sleep(delay_s)
                elif response_text:
                    accumulated_chunks = [response_text]
                    yield _sse({"type": "chunk", "text": response_text})

                for art in artifact_paths:
                    payload = _artifact_payload(art)
                    if payload is None:
                        continue
                    latest_artifact = payload
                    yield _sse({"type": "artifact", **payload})

            final_content = "".join(accumulated_chunks)
            messages: dict[str, list[dict[str, Any]]] = getattr(
                request.app.state, "thread_messages", {}
            )
            thread_msgs = messages.setdefault(thread_id, [])
            thread_msgs.append({"role": "user", "content": user_message})
            if final_content or latest_artifact:
                assistant_message: dict[str, Any] = {
                    "role": "assistant",
                    "content": final_content,
                }
                if latest_artifact:
                    assistant_message["artifact"] = latest_artifact
                thread_msgs.append(assistant_message)

            thread_meta: dict[str, dict[str, Any]] = getattr(request.app.state, "thread_meta", {})
            existing = thread_meta.get(thread_id, {})
            thread_meta[thread_id] = {
                "thread_id": thread_id,
                "title": existing.get("title") or _title_from_message(user_message),
                "last_message_preview": (final_content or "")[:100],
                "updated_at": datetime.now(UTC).isoformat(),
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
