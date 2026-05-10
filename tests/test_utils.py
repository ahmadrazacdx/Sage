import pytest
import json
import logging
from pydantic import BaseModel, ValidationError
from src.sage.utils import (
    configure_logging,
    with_error_boundary,
    estimate_tokens,
    clamp,
    is_think_grammar_error,
    strip_think_markers,
    extract_fenced_block,
    close_unbalanced_fenced_blocks,
    _content_to_text,
    _balanced_json_candidate,
    _structured_candidates,
    parse_structured_output,
    ainvoke_structured_with_fallback,
)

class DummySchema(BaseModel):
    name: str
    age: int

def test_estimate_tokens():
    assert estimate_tokens("") == 1
    assert estimate_tokens("1234") == 1
    assert estimate_tokens("12345678") == 2

def test_clamp():
    assert clamp(5, 1, 10) == 5
    assert clamp(-5, 1, 10) == 1
    assert clamp(15, 1, 10) == 10

@pytest.mark.asyncio
async def test_with_error_boundary():
    async def failing_node(state):
        raise ValueError("Something broke")
    
    wrapped = with_error_boundary(failing_node)
    result = await wrapped({})
    assert "An error occurred in the **failing_node** step." in result["response"]
    assert "ValueError: Something broke" in result["response"]

    async def successful_node(state):
        return {"response": "success"}

    wrapped_success = with_error_boundary(successful_node)
    result_success = await wrapped_success({})
    assert result_success["response"] == "success"

def test_is_think_grammar_error():
    assert is_think_grammar_error(Exception("Failed to initialize samplers due to empty grammar stack for <think> token"))
    assert not is_think_grammar_error(Exception("Some other error"))

def test_strip_think_markers():
    assert strip_think_markers("<think>some internal thought</think> final response") == "final response"
    assert strip_think_markers("<tthink>think</tthink>hello") == "hello"
    assert strip_think_markers("") == ""
    assert strip_think_markers("no tags here") == "no tags here"

def test_extract_fenced_block():
    text = "Here is code:\n```python\ndef foo(): pass\n```\nAnd more:\n```json\n{}\n```"
    assert extract_fenced_block(text, preferred_languages={"python"}) == "def foo(): pass"
    assert extract_fenced_block(text, preferred_languages={"json"}) == "{}"
    assert extract_fenced_block(text) == "def foo(): pass"
    assert extract_fenced_block("") is None
    assert extract_fenced_block("no blocks") is None

def test_close_unbalanced_fenced_blocks():
    text = "Here is some code:\n```python\nprint('hello')"
    assert close_unbalanced_fenced_blocks(text) == text + "\n```"
    balanced = "```python\npass\n```"
    assert close_unbalanced_fenced_blocks(balanced) == balanced
    assert close_unbalanced_fenced_blocks("") == ""

def test_content_to_text():
    assert _content_to_text("hello") == "hello"
    assert _content_to_text([{"text": "hello"}, "world", {"content": "!"}]) == "hello\nworld\n!"
    class DummyObj:
        text = "obj"
    assert _content_to_text([DummyObj()]) == "obj"
    assert _content_to_text({"not_content": "1"}) == "{'not_content': '1'}"

def test_balanced_json_candidate():
    text = "some prefix {'a': 1} suffix"
    assert _balanced_json_candidate(text) == "{'a': 1}"
    text2 = '[1, 2, {"b": 3}]'
    assert _balanced_json_candidate(text2) == '[1, 2, {"b": 3}]'
    text3 = '{"a": "hello \\"world\\"}'
    assert _balanced_json_candidate(text3) is None

def test_structured_candidates():
    raw = {"content": "Here is json:\n```json\n{\"name\": \"Alice\", \"age\": 30}\n```"}
    candidates = _structured_candidates(raw)
    assert '{"name": "Alice", "age": 30}' in candidates

    raw_list = [{"text": "{\"name\": \"Bob\", \"age\": 25}"}]
    candidates_list = _structured_candidates(raw_list)
    assert '{"name": "Bob", "age": 25}' in candidates_list

def test_parse_structured_output():
    schema = DummySchema
    res1 = parse_structured_output({"name": "Alice", "age": 30}, schema)
    assert res1.name == "Alice"
    
    res2 = parse_structured_output(DummySchema(name="Bob", age=25), schema)
    assert res2.name == "Bob"

    res3 = parse_structured_output("```json\n{\"name\": \"Charlie\", \"age\": 20}\n```", schema)
    assert res3.name == "Charlie"

    with pytest.raises(ValueError, match="Unable to parse structured output"):
        parse_structured_output("invalid json", schema)

@pytest.mark.asyncio
async def test_ainvoke_structured_with_fallback():
    class MockLLM:
        def __init__(self, fail_structured=False, return_raw=False, think_error=False):
            self.fail_structured = fail_structured
            self.return_raw = return_raw
            self.think_error = think_error

        def with_structured_output(self, schema):
            if self.fail_structured:
                if self.think_error:
                    raise ValueError("failed to initialize samplers... empty grammar stack... <think>")
                raise Exception("Structured error")
            class StructuredRunner:
                async def ainvoke(self, payload):
                    return schema(name="Structured", age=1)
            return StructuredRunner()

        async def ainvoke(self, payload):
            if self.return_raw:
                return '{"name": "Raw", "age": 2}'
            return "raw result"

        def __or__(self, other):
            return other

    class MockPrompt:
        def __or__(self, other):
            return other

    class MockLogger:
        def warning(self, *args, **kwargs):
            pass

    prompt = MockPrompt()
    logger = MockLogger()

    llm1 = MockLLM()
    res = await ainvoke_structured_with_fallback(
        prompt=prompt, llm=llm1, schema=DummySchema, payload={}, timeout_s=1.0, logger=logger, event_prefix="test"
    )
    assert res.name == "Structured"

    llm2 = MockLLM(return_raw=True)
    res2 = await ainvoke_structured_with_fallback(
        prompt=prompt, llm=llm2, schema=DummySchema, payload={}, timeout_s=1.0, logger=logger, event_prefix="test", prefer_raw_json=True
    )
    assert res2.name == "Raw"

    llm3 = MockLLM(fail_structured=True, think_error=True, return_raw=True)
    res3 = await ainvoke_structured_with_fallback(
        prompt=prompt, llm=llm3, schema=DummySchema, payload={}, timeout_s=1.0, logger=logger, event_prefix="test"
    )
    assert res3.name == "Raw"

    llm4 = MockLLM(fail_structured=True, think_error=False)
    with pytest.raises(Exception, match="Structured error"):
        await ainvoke_structured_with_fallback(
            prompt=prompt, llm=llm4, schema=DummySchema, payload={}, timeout_s=1.0, logger=logger, event_prefix="test"
        )
