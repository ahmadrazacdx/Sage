from sage.agents.quiz import (
    QuizEvaluation,
    QuizOutput,
    QuizQuestion,
    QuizResult,
    QuizSummary,
    _render_evaluation,
    _render_quiz,
)


def test_render_quiz():
    q = QuizQuestion(id=1, question="Q1", answer="A", type="mcq", options=["OptA", "OptB"])
    quiz = QuizOutput(questions=[q])
    res = _render_quiz(quiz)
    assert "Question No: 1" in res
    assert "OptA" in res


def test_render_evaluation():
    res1 = QuizResult(id=1, correct=True, student_answer="A", correct_answer="A", explanation="Good")
    res2 = QuizResult(id=2, correct=False, student_answer="B", correct_answer="C", explanation="Bad")

    summary = QuizSummary(strengths=["S1"], gaps=["G1"], recommended_review=["R1"])
    eval_data = QuizEvaluation(score="1/2", percentage=50.0, results=[res1, res2], summary=summary)

    formatted = _render_evaluation(eval_data)
    assert "50%" in formatted
    assert "✅" in formatted
    assert "❌" in formatted
    assert "Strengths" in formatted
    assert "Improve" in formatted


def test_render_evaluation_high_score():
    eval_data = QuizEvaluation(score="10/10", percentage=100.0, results=[], summary=QuizSummary())
    formatted = _render_evaluation(eval_data)
    assert "Outstanding" in formatted


def test_looks_like_answers_heuristics():
    from sage.agents.quiz import _looks_like_answers
    assert _looks_like_answers("1. B\n2. C\n3. A") is True
    assert _looks_like_answers("1. A\n" * 150) is False
    assert _looks_like_answers("1. A") is False
    assert _looks_like_answers("1. Is this correct?\n2. What is this?") is False


def test_stateless_payload_codec():
    from sage.agents.quiz import _encode_payload, _decode_payload, _serialize, _deserialize

    q = QuizQuestion(id=1, question="Q1", answer="A", type="mcq", options=["A", "B"])
    payload = _encode_payload([q])
    
    assert "<!--SAGE_QUIZ_PAYLOAD:" in payload
    
    recovered_json = _decode_payload(payload)
    assert recovered_json is not None
    
    recovered_objs = _deserialize(recovered_json)
    assert len(recovered_objs) == 1
    assert recovered_objs[0].question == "Q1"

    assert _decode_payload("plain text") is None
    assert _decode_payload("<!--SAGE_QUIZ_PAYLOAD:corrupt_b64-->") is None
    assert _decode_payload("<!--SAGE_QUIZ_PAYLOAD:   -->") is None


def test_validate_quiz_handling():
    import pytest
    from sage.agents.quiz import _validate_quiz
    with pytest.raises(ValueError, match="Structured output returned None"):
        _validate_quiz(None)
    with pytest.raises(ValueError, match="Model returned zero usable questions"):
        _validate_quiz(QuizOutput(questions=[]))
    q1 = QuizQuestion(question="**Q1**: What is 2+2?", type="mcq", options=["3", "4"], answer="5")
    q2 = QuizQuestion(question="Question 2 - which of the following is correct?", type="short_answer", options=["A", "B"], answer="A")
    
    quiz = QuizOutput(questions=[q1, q2])
    validated = _validate_quiz(quiz)
    assert len(validated.questions) == 2
    assert validated.questions[0].question == "What is 2+2?"
    assert validated.questions[0].id == 1
    assert validated.questions[0].answer == "3"
    assert validated.questions[1].question == "which of the following is correct?"
    assert validated.questions[1].type == "mcq"


def test_validate_evaluation_matching():
    from sage.agents.quiz import _validate_evaluation
    ev = QuizEvaluation(results=[])
    res = _validate_evaluation(ev, [])
    assert res.score == "0/0"
    assert res.percentage == 0.0
    r1 = QuizResult(id=0, correct=True, student_answer="A")
    prior = [QuizQuestion(id=5, question="Q1")]
    ev_new = QuizEvaluation(results=[r1])
    
    res_new = _validate_evaluation(ev_new, prior)
    assert res_new.results[0].id == 5
    assert res_new.score == "1/1"
    assert res_new.percentage == 100.0

