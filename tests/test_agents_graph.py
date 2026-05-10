import pytest
from unittest.mock import MagicMock
from sage.agents.graph import build_graph, _bind_llm, _route_post_reasoning

def test_bind_llm():
    mock_llm = MagicMock()
    def node_fn(state, llm, extra=None):
        return {"res": "ok"}
    bound = _bind_llm(node_fn, mock_llm, extra=1)
    assert bound({"state": 1}) == {"res": "ok"}

def test_route_post_reasoning():
    assert _route_post_reasoning({"intent": "explain"}) == "response_generator"
    assert _route_post_reasoning({"intent": "thinking"}) == "__end__"

def test_build_graph():
    llm = MagicMock()
    graph = build_graph(llm)
    assert graph is not None
    assert len(graph.nodes) >= 10

def test_build_graph_with_checkpointer():
    from langgraph.checkpoint.base import BaseCheckpointSaver
    llm = MagicMock()
    cp = MagicMock(spec=BaseCheckpointSaver)
    graph = build_graph(llm, checkpointer=cp)
    assert graph is not None
