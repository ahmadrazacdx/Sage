import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock
from sage.agents.router import router_node, route_by_intent, route_post_retrieval
from sage.agents.state import AgentState

@pytest.mark.asyncio
async def test_router_node():
    llm = MagicMock()
    state = AgentState(query="explain physics", mode="explain")
    result = await router_node(state, llm)
    assert result["intent"] == "explain"
    assert result["expanded_query"] == "explain physics"

    state = AgentState(query="help me", mode="general")
    result = await router_node(state, llm)
    assert result["intent"] == "general"

    state = AgentState(query="think about this", mode="thinking")
    result = await router_node(state, llm)
    assert result["intent"] == "thinking"
    assert result["thinking_mode"] is True

def test_route_by_intent():
    state = AgentState(intent="explain")
    assert route_by_intent(state) == "explain"

    state = AgentState(intent="thinking")
    assert route_by_intent(state) == "reasoning"

    state = AgentState(intent="invalid_intent") # type: ignore
    assert route_by_intent(state) == "general"

def test_route_post_retrieval():
    state = AgentState(intent="quiz")
    assert route_post_retrieval(state) == "quiz"

    state = AgentState(intent="explain")
    assert route_post_retrieval(state) == "explain"
