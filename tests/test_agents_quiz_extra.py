import pytest
from sage.agents.quiz import (
    _render_evaluation, 
    _render_quiz,
    QuizOutput,
    QuizQuestion, 
    QuizResult, 
    QuizEvaluation, 
    QuizSummary
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
