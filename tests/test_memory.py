import pytest
from sage import memory
from unittest.mock import patch, AsyncMock
from langchain_core.messages import HumanMessage, AIMessage
from tests.conftest import ExactMockChatOpenAI

class MockFailingLLM(ExactMockChatOpenAI):
    async def ainvoke(self, *args, **kwargs):
        raise Exception("Failed")

@pytest.mark.asyncio
async def test_extract_memories():
    llm = ExactMockChatOpenAI()
    llm.mock_response = "[identity] Name is Alex\n[study] Enrolled in Data Structures"
    
    mems = await memory.extract_memories("My name is Alex. I am in Data Structures.", "Hello Alex! I am happy to help you with Data Structures today.", llm)
    assert len(mems) == 2
    assert mems[0]["category"] == "identity"
    assert "Alex" in mems[0]["content"]

    mems_short = await memory.extract_memories("Hi", "Hi there!", llm)
    assert len(mems_short) == 0

    llm.mock_response = "NONE"
    mems_none = await memory.extract_memories("User says something longer.", "Assistant says something longer too.", llm)
    assert len(mems_none) == 0

    llm_failing = MockFailingLLM()
    mems_exc = await memory.extract_memories("User says something longer.", "Assistant says something longer too.", llm_failing)
    assert len(mems_exc) == 0

@pytest.mark.asyncio
async def test_deduplicate_and_store():
    memories = [
        {"category": "identity", "content": "Name is Alex"},
        {"category": "study", "content": "Loves Python"}
    ]
    stored = await memory.deduplicate_and_store(memories)
    assert stored == 2
    
    stored_again = await memory.deduplicate_and_store(memories)
    assert stored_again == 0

@pytest.mark.asyncio
async def test_search_memories():
    await memory.deduplicate_and_store([{"category": "identity", "content": "I like Java."}])
    mems = await memory.search_memories("Java")
    assert len(mems) > 0
    assert "Java" in mems[0]["content"]

@pytest.mark.asyncio
async def test_inject_memory_context():
    await memory.deduplicate_and_store([{"category": "identity", "content": "I am learning Java."}])
    context = await memory.inject_memory_context("Java")
    assert "Student Memory" in context
    assert "Java" in context
    
    from sage.database import delete_oldest_memories
    await delete_oldest_memories(0)
    context_empty = await memory.inject_memory_context("RandomThingNoOneKnows")
    assert context_empty == ""

@pytest.mark.asyncio
async def test_compress_history():
    llm = ExactMockChatOpenAI()
    llm.mock_response = "Summary of old messages."
    
    msgs = [HumanMessage(content="Hello " * 100)] * 10
    compressed = await memory.compress_history(msgs, llm, max_tokens=10)
    
    assert len(compressed) < 10
    assert "Summary of old messages." in compressed[0].content

    llm_failing = MockFailingLLM()
    compressed_exc = await memory.compress_history(msgs, llm_failing, max_tokens=10)
    assert len(compressed_exc) == min(6, len(msgs))

@pytest.mark.asyncio
async def test_generate_title():
    llm = ExactMockChatOpenAI()
    llm.mock_response = "Great Conversation"
    title = await memory.generate_title("Let's talk about Python", llm)
    assert title == "Great Conversation"

    llm_failing = MockFailingLLM()
    title_exc = await memory.generate_title("Let's talk about Python today in detail", llm_failing)
    assert title_exc == "Let's talk about Python today in…"

@pytest.mark.asyncio
async def test_post_turn_memory_hook():
    llm = ExactMockChatOpenAI()
    llm.mock_response = "[study] Learning testing"
    await memory.post_turn_memory_hook(
        "I am learning how to test Python code today.",
        "That is a great skill to have. What framework?",
        llm
    )
    mems = await memory.search_memories("testing")
    assert len(mems) > 0

    llm_failing = MockFailingLLM()
    await memory.post_turn_memory_hook("test", "test", llm_failing)
