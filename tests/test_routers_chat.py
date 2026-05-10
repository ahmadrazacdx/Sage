import pytest
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient
from fastapi import FastAPI
import json

from sage.routers.chat import (
    router,
    _short_id,
    _canonical_tool_name,
    _tool_label,
    _resolve_node_name,
    _split_for_typewriter,
    _typewriter_delay_seconds,
    _split_thinking_response,
    _strip_partial_tag_suffix,
    _consume_thinking_chunk,
    _flush_thinking_carry,
    _title_from_message,
    _sanitize_markdown_response,
    _max_memory_facts,
    _compress_max_tokens,
)

@pytest.fixture
def test_app():
    app = FastAPI()
    app.include_router(router, prefix="/api")
    
    app.state.model_ready = True
    app.state.pending_streams = {}
    app.state.active_streams = {}
    app.state.thread_messages = {}
    
    mock_network = MagicMock()
    mock_network.online = False
    app.state.network = mock_network
    
    app.state.utility_llm = MagicMock()
    app.state.llm = MagicMock()
    app.state.graph = MagicMock()
    
    return app

@pytest.fixture
def client(test_app):
    return TestClient(test_app)

def test_helpers():
    assert len(_short_id()) == 8
    
    assert _canonical_tool_name("duckduckgo_search") == "search_web"
    assert _canonical_tool_name("random_tool") == "random_tool"
    
    assert _tool_label("search_web", "duck") == "🌐 Searching the web…"
    assert _tool_label("unknown", "unknown") == "⚙️ Running tool…"
    
    assert _resolve_node_name({"name": "router"}, {}) == "router"
    assert _resolve_node_name({}, {"langgraph_node": "test"}) == "test"
    
    assert _split_for_typewriter("") == []
    assert len(_split_for_typewriter("hello " * 20)) > 1
    assert _typewriter_delay_seconds(100, 2) > 0
    assert _typewriter_delay_seconds(0, 0) == 0.0

def test_thinking_helpers():
    t, v = _split_thinking_response("<think>hmm</think>answer")
    assert "hmm" in t
    assert v == "answer"
    
    t2, v2 = _split_thinking_response("malformed</think>ans")
    assert "malformed" in t2
    assert v2 == "ans"

    assert _strip_partial_tag_suffix("ab<thi", "<think>") == ("ab", "<thi")

    tp, ap, it, c, s = _consume_thinking_chunk("hello<think>world</think>end", in_think=False, seen_think_marker=False)
    assert tp == ["world"]
    assert ap == ["hello", "end"]
    assert it is False
    assert s is True

    assert _flush_thinking_carry("", in_think=False) == ("", "")
    assert _flush_thinking_carry("text", in_think=True) == ("text", "")

def test_submit_chat_invalid(client):
    client.app.state.model_ready = False
    res = client.post("/api/chat", json={"message": "hi", "mode": "general"})
    assert res.status_code == 503
    client.app.state.model_ready = True
    
    res = client.post("/api/chat", json={"message": "hi", "mode": "invalid_mode"})
    assert res.status_code == 422

@patch("sage.routers.chat.inject_memory_context", AsyncMock(return_value="memory"))
def test_submit_chat_success(client):
    res = client.post("/api/chat", json={"message": "hello", "mode": "general", "course": "CS101"})
    assert res.status_code == 200
    data = res.json()
    assert "thread_id" in data
    assert "message_id" in data
    
    thread_id = data["thread_id"]
    assert thread_id in client.app.state.pending_streams
    
    res2 = client.post("/api/chat", json={"message": "hello", "mode": "general", "thread_id": thread_id})
    assert res2.status_code == 409
    
    client.app.state.active_streams[thread_id] = True
    client.app.state.pending_streams.pop(thread_id)
    res3 = client.post("/api/chat", json={"message": "hello", "mode": "general", "thread_id": thread_id})
    assert res3.status_code == 409


class MockEventIter:
    def __init__(self, events):
        self.events = events
        self.idx = 0
    def __aiter__(self): return self
    async def __anext__(self):
        if self.idx < len(self.events):
            evt = self.events[self.idx]
            self.idx += 1
            return evt
        raise StopAsyncIteration


@patch("sage.routers.chat.upsert_conversation", AsyncMock())
@patch("sage.routers.chat.post_turn_memory_hook", AsyncMock())
@patch("sage.routers.chat.generate_title", AsyncMock(return_value="Title"))
def test_stream_response_general(client):
    thread_id = "test_thread"
    client.app.state.pending_streams[thread_id] = {
        "state_input": {},
        "user_message": "hello",
        "intent": "general",
        "created_at": 0,
        "ctx_size": 4096
    }
    
    events = [
        {"event": "on_chain_start", "metadata": {"langgraph_node": "general"}},
        {"event": "on_tool_start", "name": "search_web"},
        {"event": "on_chat_model_stream", "data": {"chunk": MagicMock(content="chunk")}},
        {"event": "on_chain_end", "data": {"output": {"response": "done_response"}}}
    ]
    
    client.app.state.graph.astream_events.return_value = MockEventIter(events)
    
    res = client.get(f"/api/stream/{thread_id}")
    assert res.status_code == 200
    
    text = res.text
    assert 'data: {"type": "node_start"' in text
    assert 'data: {"type": "tool_call"' in text
    assert 'data: {"type": "chunk", "text": "chunk"}' in text
    assert 'data: {"type": "done"}' in text

@patch("sage.routers.chat.upsert_conversation", AsyncMock())
@patch("sage.routers.chat.post_turn_memory_hook", AsyncMock())
def test_stream_response_batch(client):
    thread_id = "test_batch"
    client.app.state.pending_streams[thread_id] = {
        "state_input": {},
        "user_message": "explain this",
        "intent": "explain",
    }
    
    events = [
        {"event": "on_chain_end", "data": {"output": {"response": "batch response", "artifact_paths": [{"path": "/test.pdf", "filename": "test.pdf"}]}}}
    ]
    client.app.state.graph.astream_events.return_value = MockEventIter(events)
    
    res = client.get(f"/api/stream/{thread_id}")
    assert res.status_code == 200
    text = res.text
    
    assert 'data: {"type": "chunk", "text": "batch response' in text
    assert 'data: {"type": "artifact", "kind": "file", "filename": "test.pdf", "path": "/test.pdf"' in text

def test_stream_missing(client):
    res = client.get("/api/stream/unknown")
    assert res.status_code == 404

def test_stream_active(client):
    client.app.state.active_streams["active_thread"] = True
    res = client.get("/api/stream/active_thread")
    assert res.status_code == 409

@patch("sage.routers.chat.upsert_conversation", AsyncMock())
@patch("sage.routers.chat.post_turn_memory_hook", AsyncMock())
def test_stream_event_types(client):
    thread_id = "test_events"
    client.app.state.pending_streams[thread_id] = {
        "state_input": {}, "user_message": "hi", "intent": "general"
    }
    
    events = [
        {"event": "on_chat_model_start", "name": "llm"},
        {"event": "on_tool_end", "name": "search_web", "data": {"output": "result"}},
        {"event": "on_chain_stream", "data": {"chunk": {"response": "stream"}}},
        {"event": "on_chain_end", "data": {"output": {"response": "done"}}}
    ]
    client.app.state.graph.astream_events.return_value = MockEventIter(events)
    
    res = client.get(f"/api/stream/{thread_id}")
    assert res.status_code == 200
    assert "done" in res.text

def test_markdown_sanitization():
    from sage.routers.chat import _sanitize_markdown_response
    assert _sanitize_markdown_response("hello") == "hello"
    assert _sanitize_markdown_response("<think>hmm</think>ans") == "ans"

def test_title_from_message():
    from sage.routers.chat import _title_from_message
    assert _title_from_message("hello world") == "hello world"
    assert len(_title_from_message("word " * 10)) <= 50
