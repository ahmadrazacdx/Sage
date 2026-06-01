from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI

from sage.agents.code_fix import code_fix_node
from sage.agents.diagram import diagram_node
from sage.agents.general import general_node
from sage.agents.planner import RoadmapAnalysis, RoadmapSchedule, planner_node
from sage.agents.quiz import quiz_node, quiz_evaluate_node
from sage.agents.reasoning import reasoning_node
from sage.agents.state import AgentState


def create_mock_llm(responses=None, tool_calls=None):
    llm = MagicMock(spec=ChatOpenAI)
    llm.bind.return_value = llm
    llm.bind_tools.return_value = llm
    llm.ainvoke = AsyncMock()

    if responses:
        if isinstance(responses, list):
            llm.ainvoke.side_effect = responses
        else:
            llm.ainvoke.return_value = responses
    else:
        llm.ainvoke = AsyncMock(return_value=AIMessage(content="mocked"))
    return llm


@pytest.mark.asyncio
async def test_general_node_success():
    state: AgentState = {"query": "Hello", "intent": "general"}
    llm = create_mock_llm(AIMessage(content="General response"))
    res = await general_node(state, llm)
    assert res["response"] == "General response"
    assert len(res["messages"]) == 1


@pytest.mark.asyncio
async def test_general_node_timeout():
    state: AgentState = {"query": "Hello", "intent": "general"}
    llm = create_mock_llm()
    llm.ainvoke = AsyncMock(side_effect=TimeoutError)
    res = await general_node(state, llm)
    assert "timed out" in res["response"]


@pytest.mark.asyncio
async def test_general_node_exception():
    state: AgentState = {"query": "Hello", "intent": "general"}
    llm = create_mock_llm()
    llm.ainvoke = AsyncMock(side_effect=ValueError("Test err"))
    res = await general_node(state, llm)
    assert "ran into an issue" in res["response"]


@pytest.mark.asyncio
async def test_reasoning_node_thinking():
    state: AgentState = {"query": "How does gravity work?", "intent": "thinking"}
    llm = create_mock_llm(AIMessage(content="<think>Thinking...</think>\nGravity is..."))
    res = await reasoning_node(state, llm)
    assert "<think>" in res["response"]
    assert "Gravity is..." in res["response"]


@pytest.mark.asyncio
async def test_reasoning_node_math_fast_path():
    state: AgentState = {"query": "what is 2 + 2?", "intent": "thinking"}
    llm = create_mock_llm()
    with patch("sage.tools.calculator.calculator") as mock_calc:
        mock_calc.invoke.return_value = {"success": True, "result": 4}
        res = await reasoning_node(state, llm)
    assert "4" in res["response"]


@pytest.mark.asyncio
async def test_reasoning_node_math_tool_usage():
    state: AgentState = {"query": "If I have 2 apples and get 2 more, how many do I have?", "intent": "thinking"}

    mock_tool_calls = [{"name": "calculator", "args": {"expression": "2+2"}, "id": "t1"}]
    llm = create_mock_llm(
        [
            AIMessage(content="<think>t</think>", tool_calls=mock_tool_calls),
            AIMessage(content="<think>t2</think>Result is 4"),
        ]
    )

    with patch("sage.tools.calculator.calculator") as mock_calc:
        mock_calc.invoke.return_value = {"success": True, "result": 4}
        res = await reasoning_node(state, llm)
        assert "Result is 4" in res["response"]


@pytest.mark.asyncio
async def test_reasoning_node_explain_with_ku():
    state: AgentState = {
        "query": "Explain concept X",
        "intent": "explain",
        "knowledge_units": [{"id": "KU1", "claim": "X is cool", "source_file": "doc.pdf"}],
    }
    llm = create_mock_llm(AIMessage(content="Based on [KU1], X is cool."))
    res = await reasoning_node(state, llm)
    assert "X is cool" in res["response"]


@pytest.mark.asyncio
async def test_reasoning_node_explain_timeout():
    state: AgentState = {"query": "explain", "intent": "explain"}
    llm = create_mock_llm()
    llm.ainvoke.side_effect = TimeoutError
    res = await reasoning_node(state, llm)
    assert "timed out" in res["response"]


@pytest.mark.asyncio
async def test_planner_node_success():
    state: AgentState = {"query": "Plan for calculus", "intent": "planner"}
    llm = create_mock_llm()

    analysis_mock = RoadmapAnalysis(subject="Calculus", timeline_days=5)
    schedule_mock = RoadmapSchedule()

    with patch("sage.agents.planner.ainvoke_structured_with_fallback", side_effect=[analysis_mock, schedule_mock]):
        res = await planner_node(state, llm)
    assert "Calculus" in res["response"]


@pytest.mark.asyncio
async def test_planner_node_fallback():
    state: AgentState = {"query": "Plan for calculus", "intent": "planner"}
    llm = create_mock_llm()

    analysis_mock = RoadmapAnalysis(subject="Calculus", timeline_days=5)
    with patch("sage.agents.planner.ainvoke_structured_with_fallback", side_effect=[analysis_mock, Exception("Fail")]):
        res = await planner_node(state, llm)
    assert "Calculus" in res["response"]
    assert "Review learning materials" in res["response"]


@pytest.mark.asyncio
async def test_planner_node_analysis_fail():
    state: AgentState = {"query": "Plan for calculus", "intent": "planner"}
    llm = create_mock_llm()
    with patch("sage.agents.planner.ainvoke_structured_with_fallback", side_effect=Exception("Fail")):
        res = await planner_node(state, llm)
    assert "unable to analyse" in res["response"]


@pytest.mark.asyncio
async def test_diagram_node_success():
    state: AgentState = {"query": "draw a diagram", "intent": "diagram"}
    llm = create_mock_llm(
        [
            AIMessage(content='```json\n{"nodes": [{"id": "A"}]}\n```'),
            AIMessage(content="```mermaid\ngraph TD\nA-->B\n```"),
        ]
    )
    res = await diagram_node(state, llm)
    assert "```mermaid" in res["response"]
    assert "A-->B" in res["response"]


@pytest.mark.asyncio
async def test_diagram_node_invalid_mermaid_fix():
    state: AgentState = {"query": "draw a diagram", "intent": "diagram"}
    llm = create_mock_llm(
        [
            AIMessage(content='```json\n{"nodes": [{"id": "A"}]}\n```'),
            AIMessage(content="invalid mermaid code"),
            AIMessage(content="```mermaid\ngraph TD\nA-->B\n```"),
        ]
    )
    res = await diagram_node(state, llm)
    assert "```mermaid" in res["response"]
    assert "A-->B" in res["response"]


@pytest.mark.asyncio
async def test_diagram_node_timeout():
    state: AgentState = {"query": "draw", "intent": "diagram"}
    llm = create_mock_llm()
    llm.ainvoke = AsyncMock(side_effect=TimeoutError)
    res = await diagram_node(state, llm)
    assert "unable to analyse" in res["response"]


@pytest.mark.asyncio
async def test_quiz_node_generate():
    state: AgentState = {"query": "quiz on math", "intent": "quiz"}
    llm = create_mock_llm(
        AIMessage(content='```json\n{"questions": [{"question": "1+1?", "answer": "2", "type": "short_answer"}]}\n```')
    )
    res = await quiz_node(state, llm)
    assert "Here's a short answer quiz" in res["response"]
    assert "1+1?" in res["response"]


@pytest.mark.asyncio
async def test_quiz_node_evaluate():
    state: AgentState = {
        "query": "1. 2\n2. 4",
        "intent": "quiz",
        "last_quiz_questions": (
            '[{"id": 1, "type": "short_answer", "question": "1+1?", "answer": "2", '
            '"explanation": "", "bloom_level": "Understand", "options": null}]'
        ),
    }
    llm = create_mock_llm(
        AIMessage(
            content=(
                '```json\n{"score": "1/1", "percentage": 100.0, "results": [{"id": 1, '
                '"correct": true, "student_answer": "2", "correct_answer": "2", '
                '"explanation": "", "misconception": null, "review_topic": null}], '
                '"summary": {"strengths": [], "gaps": [], "recommended_review": []}}\n```'
            )
        )
    )

    res = await quiz_node(state, llm)
    assert "1/1" in res["response"]
    assert "✅" in res["response"]


def test_quiz_parsing_logic():
    from sage.agents.quiz import (
        QuizOutput,
        QuizQuestion,
        _extract_json,
        _looks_like_answers,
        _validate_quiz,
    )

    assert _extract_json('```json\n{"key": "val"}\n```') == '{"key": "val"}'
    assert _extract_json('Some text {"a": 1} end') == '{"a": 1}'

    q = QuizQuestion(question="What is 1+1?", answer="A", type="mcq", options=["A", "B"])
    quiz = QuizOutput(questions=[q])
    valid = _validate_quiz(quiz)
    assert len(valid.questions) == 1

    assert _looks_like_answers("1. A\n2. B") is True
    assert _looks_like_answers("What is your name?") is False


@pytest.mark.asyncio
async def test_quiz_evaluate_no_payload():
    state: AgentState = {"query": "1. answer\n2. answer", "intent": "quiz"}
    llm = create_mock_llm()
    res = await quiz_node(state, llm)
    assert "couldn't find a prior quiz" in res["response"]


@pytest.mark.asyncio
async def test_code_fix_node_python_success():
    state: AgentState = {"query": "print(1/0)", "intent": "code_fix"}
    llm = create_mock_llm([AIMessage(content="```python\nprint('fixed')\n```"), AIMessage(content="Explanation here")])

    from sage.agents.code_fix import Diagnosis

    diag = Diagnosis(error_type="runtime", root_cause="div by zero", fix_strategy="add check")

    with (
        patch("sage.agents.code_fix.ainvoke_structured_with_fallback", return_value=diag),
        patch("sage.agents.code_fix.execute_python") as mock_sandbox,
    ):
        mock_sandbox.ainvoke = AsyncMock(return_value={"success": True, "stdout": "fixed\n"})
        res = await code_fix_node(state, llm)

    assert "Fixed Code" in res["response"]
    assert "Explanation here" in res["response"]


@pytest.mark.asyncio
async def test_code_fix_node_non_python():
    state: AgentState = {"query": "public static void main", "intent": "code_fix"}
    llm = create_mock_llm(AIMessage(content="Java code issue"))
    res = await code_fix_node(state, llm)
    assert "Java code issue" in res["response"]


@pytest.mark.asyncio
async def test_code_fix_node_framework_skip_sandbox():
    state: AgentState = {"query": "import flask\napp = flask.Flask()", "intent": "code_fix"}
    llm = create_mock_llm([AIMessage(content="```python\napp.run()\n```"), AIMessage(content="Explanation here")])

    from sage.agents.code_fix import Diagnosis

    diag = Diagnosis(error_type="runtime", root_cause="app error", framework="flask")

    with patch("sage.agents.code_fix.ainvoke_structured_with_fallback", return_value=diag):
        res = await code_fix_node(state, llm)

    assert "Framework code (flask)" in res["response"]


@pytest.mark.asyncio
async def test_code_fix_node_sandbox_fail_loop():
    state: AgentState = {"query": "print(x)", "intent": "code_fix"}
    llm = create_mock_llm(
        [
            AIMessage(content="```python\nprint(y)\n```"),
            AIMessage(content="```python\nprint(z)\n```"),
            AIMessage(content="```python\nprint(w)\n```"),
            AIMessage(content="Explanation here"),
        ]
    )

    from sage.agents.code_fix import Diagnosis

    diag = Diagnosis(error_type="runtime", root_cause="name error")

    with (
        patch("sage.agents.code_fix.ainvoke_structured_with_fallback", return_value=diag),
        patch("sage.agents.code_fix.execute_python") as mock_sandbox,
    ):
        mock_sandbox.ainvoke = AsyncMock(return_value={"success": False, "error": "NameError"})
        res = await code_fix_node(state, llm)

    assert "Could not fully resolve the issue" in res["response"]


def test_planner_coercions_and_validations():
    from sage.agents.planner import RoadmapAnalysis, RoadmapSchedule
    assert RoadmapAnalysis._coerce_aliases([]) == []
    assert RoadmapSchedule._coerce_aliases([]) == []
    assert RoadmapSchedule._coerce_checkpoints([]) == []


def test_planner_normalize_schedule_padding_and_checkpoints():
    from sage.agents.planner import _normalize_schedule, ScheduleDay
    analysis = RoadmapAnalysis(subject="Physics", timeline_days=3)
    d1 = ScheduleDay(day=1, session_type="study", topics=["Mechanics"], hours=2, checkpoint={"milestone": "CP1"})
    d2 = ScheduleDay(day=1, session_type="study", topics=["Mechanics"], hours=2)
    
    schedule = RoadmapSchedule(
        schedule=[d1, d2],
        checkpoints=[{"after_day": 1, "milestone": "CP1"}, {"after_day": 1, "milestone": "CP2"}],
        self_assessment_questions=["Q1"]
    )
    res = _normalize_schedule(analysis, schedule)
    
    assert len(res.schedule) == 3
    assert len(res.checkpoints) == 1
    assert len(res.self_assessment_questions) == 3


@pytest.mark.asyncio
async def test_planner_node_analysis_failure_all_retries():
    state = {"query": "Plan subject", "intent": "planner"}
    mock_llm = create_mock_llm()
    with patch("sage.agents.planner.ainvoke_structured_with_fallback", side_effect=Exception("Structured Analysis Failed")):
        res = await planner_node(state, mock_llm)
    assert "unable to analyse" in res["response"]


@pytest.mark.asyncio
async def test_reasoning_node_thinking_fast_path_exception():
    state = {"query": "what is 2 + 2?", "intent": "thinking"}
    mock_llm = create_mock_llm(AIMessage(content="<think>T</think>4"))
    with patch("sage.tools.calculator.calculator") as mock_calc:
        mock_calc.invoke.side_effect = Exception("Fast path error")
        res = await reasoning_node(state, mock_llm)
        assert "4" in res["response"]


@pytest.mark.asyncio
async def test_reasoning_node_thinking_exceptions():
    state = {"query": "solve complex problem", "intent": "thinking"}
    mock_llm = create_mock_llm()
    mock_llm.ainvoke.side_effect = TimeoutError("LLM timeout")
    res = await reasoning_node(state, mock_llm)
    assert "timed out" in res["response"]
    mock_llm.ainvoke.side_effect = ValueError("Some random value error")
    res2 = await reasoning_node(state, mock_llm)
    assert "ran into an issue" in res2["response"]


@pytest.mark.asyncio
async def test_code_fix_node_query_truncation():
    state = {"query": "x = 1\n" * 2000, "intent": "code_fix"}
    mock_llm = create_mock_llm()
    with patch("sage.agents.code_fix.ainvoke_structured_with_fallback", side_effect=Exception("End of test")):
        res = await code_fix_node(state, mock_llm)
        assert "unable to diagnose" in res["response"]


@pytest.mark.asyncio
async def test_quiz_node_generation_exception_path():
    state = {"query": "generate a quiz", "intent": "quiz", "knowledge_units": []}
    mock_llm = create_mock_llm()
    mock_llm.ainvoke.side_effect = Exception("LLM down")
    
    res = await quiz_node(state, mock_llm)
    assert "Unable to generate a quiz" in res["response"]


@pytest.mark.asyncio
async def test_quiz_evaluate_node_exception_path():
    state = {
        "query": "1. A\n2. B", 
        "intent": "quiz", 
        "last_quiz_questions": '[{"id": 1, "type": "short_answer", "question": "Q1", "answer": "A"}]'
    }
    mock_llm = create_mock_llm()
    mock_llm.ainvoke.side_effect = Exception("Grading failed")
    
    res = await quiz_evaluate_node(state, mock_llm)
    assert "Unable to evaluate your answers" in res["response"]

