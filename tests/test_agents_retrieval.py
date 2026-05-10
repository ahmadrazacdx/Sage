import pytest
from unittest.mock import MagicMock
from sage.agents.retrieval import retrieval_node, _query_cache_key

@pytest.mark.asyncio
async def test_retrieval_node_stub():
    llm = MagicMock()
    state = {"query": "test query"}
    res = await retrieval_node(state, llm)
    assert "chunks" in res
    assert "knowledge_units" in res
    assert "retrieval_cache_key" in res

@pytest.mark.asyncio
async def test_retrieval_node_cache_hit():
    llm = MagicMock()
    ckey = _query_cache_key("test")
    state = {
        "query": "test",
        "retrieval_cache_key": ckey,
        "retrieval_cache_chunks": [{"id": 1}],
        "retrieval_cache_kus": [{"id": "K1"}]
    }
    res = await retrieval_node(state, llm)
    assert res["chunks"] == [{"id": 1}]
    assert res["knowledge_units"] == [{"id": "K1"}]

def test_query_cache_key():
    k1 = _query_cache_key("hello")
    k2 = _query_cache_key("hello")
    k3 = _query_cache_key("world")
    assert k1 == k2
    assert k1 != k3
    assert len(k1) == 16
