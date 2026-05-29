from sage.agents.reasoning import _ensure_think_wrapped, _looks_like_answer_paragraph, _looks_like_reasoning_paragraph


def test_ensure_think_wrapped():
    assert _ensure_think_wrapped("<think>T</think>A") == "<think>T</think>A"
    assert "<think>" in _ensure_think_wrapped("")
    text = "First I will look at the data.\n\nThen I will calculate.\n\nTherefore the answer is 42."
    wrapped = _ensure_think_wrapped(text)
    assert "<think>" in wrapped
    assert "</think>" in wrapped
    assert "42" in wrapped


def test_looks_like_reasoning_paragraph():
    assert _looks_like_reasoning_paragraph("First I will think about it") is True
    assert _looks_like_reasoning_paragraph("The answer is 42") is False


def test_looks_like_answer_paragraph():
    assert _looks_like_answer_paragraph("Therefore, we conclude") is True
    assert _looks_like_answer_paragraph("Let's look at the steps") is False
