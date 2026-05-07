"""
Quiz agent node for Sage.

Implements two flows:
  - Generation: produce Bloom's-calibrated quiz questions from
     retrieved KUs via structured output.
  - Evaluation: score student answers against the generated
     questions and provide actionable feedback.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
from typing import Any

import structlog
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field, field_validator

from sage.agents.state import AgentState
from sage.config import get_settings
from sage.prompts import (
    QUIZ_GENERATION_PROMPT,
    QUIZ_EVALUATION_PROMPT,
)
from sage.utils import strip_think_markers

log = structlog.get_logger(__name__)

_MAX_RETRIES: int = 1

_PAYLOAD_PREFIX: str = "<!--SAGE_QUIZ_PAYLOAD:"
_PAYLOAD_SUFFIX: str = "-->"

_ANSWER_RE: re.Pattern[str] = re.compile(
    r"^\s*(?:q\s*)?[1-9]\d*\s*[\.\):\-]\s*\S",
    re.IGNORECASE | re.MULTILINE,
)
_MCQ_PHRASE_RE: re.Pattern[str] = re.compile(
    r"\b(which of the following|which one of the following|select the correct|"
    r"which is\b|which are\b|which statement)",
    re.IGNORECASE,
)

_GEN_PROMPT = ChatPromptTemplate.from_messages([
    ("system", QUIZ_GENERATION_PROMPT),
    ("human", "{query}"),
])

_EVAL_PROMPT = ChatPromptTemplate.from_messages([
    ("system", QUIZ_EVALUATION_PROMPT),
    ("human", "{questions_and_answers}"),
])


class QuizQuestion(BaseModel):
    """Single quiz question with answer and metadata."""

    id: int = Field(default=0, description="Assigned sequentially by validator")
    type: str = Field(default="short_answer", description="mcq | short_answer | true_false | code")
    question: str = Field(default="", description="Question text")
    options: list[str] | None = Field(
        default=None, description="MCQ options; null otherwise"
    )
    answer: str = Field(default="", description="Correct answer")
    explanation: str = Field(
        default="", description="Correct-answer rationale with [KU#] references"
    )
    bloom_level: str = Field(default="Understand", description="Bloom's taxonomy level")

class QuizOutput(BaseModel):
    """Structured output: list of quiz questions."""

    questions: list[QuizQuestion] = Field(description="Generated quiz questions")


class QuizResult(BaseModel):
    """Single evaluated answer."""

    id: int = 0
    correct: bool = False
    student_answer: str = ""
    correct_answer: str = ""
    explanation: str = ""
    misconception: str | None = None
    review_topic: str | None = None


class QuizSummary(BaseModel):
    """Summary of quiz evaluation."""

    strengths: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    recommended_review: list[str] = Field(default_factory=list)

    @field_validator("strengths", "gaps", "recommended_review", mode="before")
    @classmethod
    def cast_to_list(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            lines = [line.lstrip("- *").strip() for line in v.split("\n") if line.strip()]
            return lines if lines else [v.strip()]
        if isinstance(v, list):
            return [str(item) for item in v]
        return []


class QuizEvaluation(BaseModel):
    """Full evaluation output."""

    score: str = Field(default="0/0", description="e.g. '3/5'")
    percentage: float = 0.0
    results: list[QuizResult] = Field(default_factory=list)
    summary: QuizSummary = Field(default_factory=QuizSummary)

_chain_cache: dict[tuple, Any] = {}

_NO_THINK = {"chat_template_kwargs": {"enable_thinking": False}}


def _gen_chain(llm: Any) -> Any:
    key = (id(llm), "gen")
    if key not in _chain_cache:
        _chain_cache[key] = _GEN_PROMPT | llm.bind(extra_body=_NO_THINK)
    return _chain_cache[key]


def _eval_chain(llm: Any) -> Any:
    key = (id(llm), "eval")
    if key not in _chain_cache:
        _chain_cache[key] = _EVAL_PROMPT | llm.bind(extra_body=_NO_THINK)
    return _chain_cache[key]

_JSON_FENCE_RE: re.Pattern[str] = re.compile(
    r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE
)


def _content_to_text(content: Any) -> str:
    """Normalise provider-specific message content into plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content") or ""
                if text:
                    parts.append(str(text))
            else:
                text = getattr(item, "text", "")
                if text:
                    parts.append(str(text))
        return "\n".join(parts)
    return str(content)


def _balanced_json_candidate(text: str) -> str | None:
    """Return the first balanced JSON object/array found in text."""
    start = -1
    depth = 0
    in_string = False
    escape = False

    for i, ch in enumerate(text):
        if start == -1:
            if ch in "[{":
                start = i
                depth = 1
            continue

        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]

    return None

def _extract_json(raw: Any) -> str:
    """Extract the most likely JSON payload from model output."""
    if isinstance(raw, AIMessage):
        text = _content_to_text(raw.content)
    elif isinstance(raw, dict):
        text = _content_to_text(raw.get("content", raw))
    else:
        text = _content_to_text(getattr(raw, "content", raw))

    text = (strip_think_markers(text) or text).strip()
    if not text:
        return ""

    fence_matches = [m.group(1).strip() for m in _JSON_FENCE_RE.finditer(text)]
    for candidate in fence_matches:
        if candidate.startswith("{") or candidate.startswith("["):
            return candidate
        nested = _balanced_json_candidate(candidate)
        if nested:
            return nested

    if text.startswith("{") or text.startswith("["):
        return text

    nested = _balanced_json_candidate(text)
    return nested or text

def _parse_quiz(raw: Any) -> QuizOutput:
    data = json.loads(_extract_json(raw))
    return QuizOutput(**data)


def _parse_evaluation(raw: Any) -> QuizEvaluation:
    data = json.loads(_extract_json(raw))
    return QuizEvaluation(**data)

def _format_kus(kus: list[dict]) -> str:
    """Render knowledge units as a numbered list for prompt injection."""
    if not kus:
        return "None Available"
    return "\n".join(
        f"[{ku.get('id', 'KU?')}] {ku.get('claim', ku.get('content', ''))} "
        f"— {ku.get('source_file', 'unknown')}"
        for ku in kus
    )


def _validate_quiz(quiz: QuizOutput | None) -> QuizOutput:
    if quiz is None:
        raise ValueError("Structured output returned None.")
    quiz.questions = [q for q in quiz.questions if q.question.strip()]
    if not quiz.questions:
        raise ValueError("Model returned zero usable questions.")
    for i, q in enumerate(quiz.questions):
        q.id = i + 1
        q.type = q.type.lower().strip() if q.type else "short_answer"
        if q.type not in {"mcq", "short_answer", "true_false", "code"}:
            q.type = "short_answer"
        q.question = re.sub(
            r"^(\*\*?Q\d+\*\*?|Question\s*\d+|Q\d+)[\.\:\-\s]*", "",
            q.question, flags=re.IGNORECASE,
        ).strip()
        if q.type == "mcq" and q.options:
            q.options = [re.sub(r"^([A-Za-z0-9][\.\)\-])\s+", "", opt).strip() for opt in q.options]
            try:
                idx = int(q.answer)
                if not (0 <= idx < len(q.options)):
                    q.answer = q.options[0]
            except (ValueError, TypeError):
                pass

        if q.type == "short_answer" and _MCQ_PHRASE_RE.search(q.question):
            if q.options and len(q.options) >= 2:
                q.type = "mcq"
                log.warning("quiz_question_type_promoted", id=q.id)
            else:
                q.question = ""
    quiz.questions = [q for q in quiz.questions if q.question.strip()]
    if not quiz.questions:
        raise ValueError("All questions were discarded after validation.")
    for i, q in enumerate(quiz.questions):
        q.id = i + 1
    return quiz

def _validate_evaluation(ev: QuizEvaluation, prior: list[QuizQuestion]) -> QuizEvaluation:
    """Post-validate evaluation, assigning proper IDs and native scores."""
    for i, res in enumerate(ev.results):
        if res.id == 0:
            if i < len(prior):
                res.id = prior[i].id
            else:
                res.id = i + 1
            
    total = len(ev.results)
    if total > 0:
        correct = sum(1 for r in ev.results if r.correct)
        ev.score = f"{correct}/{total}"
        ev.percentage = (correct / total) * 100.0
    else:
        ev.score = "0/0"
        ev.percentage = 0.0
    return ev

_INTRO: dict[str, str] = {
    "Multiple Choice": "Here's a multiple choice quiz on {topic}. Select the best answer for each question.",
    "Short Answer": "Here's a short answer quiz on {topic}. Write a concise but complete response for each question.",
    "True / False": "Here's a true/false quiz on {topic}. Decide whether each statement is correct.",
    "Code": "Here's a coding quiz on {topic}. Write or trace code as required by each question.",
    "Mixed": "Here's a mixed quiz on {topic}, covering different question types to test your understanding from multiple angles.",
}

_OUTRO = (
    "Take your time — there's no rush. "
    "When you're ready, reply with your numbered answers "
    "and I'll give you detailed feedback on each one."
)

def _infer_topic(quiz: QuizOutput) -> str:
    first = quiz.questions[0].question if quiz.questions else ""
    words = first.split()
    return " ".join(words[:4]).rstrip(".,?:") + "..." if len(words) > 4 else first


_TYPE_LABEL: dict[str, str] = {
    "mcq": "Multiple Choice",
    "short_answer": "Short Answer",
    "true_false": "True / False",
    "code": "Code",
}

def _render_quiz(quiz: QuizOutput, topic: str = "") -> str:
    type_counts = {q.type for q in quiz.questions}
    n = len(quiz.questions)
    format_label = (
        _TYPE_LABEL.get(next(iter(type_counts)), "Quiz")
        if len(type_counts) == 1
        else "Mixed"
    )

    topic_label = topic.strip() or _infer_topic(quiz)
    intro = _INTRO.get(format_label, _INTRO["Mixed"]).format(topic=topic_label)

    lines: list[str] = [
        intro,
        "",
        f"## {format_label} Quiz ({n} questions)",
        "",
    ]

    for q in quiz.questions:
        type_label = _TYPE_LABEL.get(q.type, q.type)

        lines.append(f"### Question No: {q.id}")
        lines.append(q.question)
        lines.append("")

        if q.type == "mcq" and q.options:
            for i, opt in enumerate(q.options):
                lines.append(f"> **{chr(65 + i)}.** {opt}")
            lines.append("")
        elif q.type == "true_false":
            lines.append("> **A.** True")
            lines.append("> **B.** False")
            lines.append("")

    lines.append(_OUTRO)
    return "\n".join(lines)

def _render_evaluation(ev: QuizEvaluation) -> str:
    """Render evaluation results as student-facing markdown."""
    if ev.percentage >= 90:
        intro = (
            "Outstanding performance! You achieved an excellent score and clearly have a strong grasp of these concepts. "
            "Keep up the great work as we move forward."
        )
    elif ev.percentage >= 70:
        intro = (
            "Great job! You have a solid understanding of the material, though there are a few minor things to polish. "
            "Let's look at the breakdown below to see exactly where you can improve."
        )
    else:
        intro = (
            "Good effort! Quizzes are valuable tools specifically designed to help us identify what we don't know yet. "
            "Let's review the areas that were tricky and strengthen your foundation in these topics."
        )

    lines: list[str] = [intro, f"\n## Quiz Results: {ev.score} ({ev.percentage:.0f}%)\n"]
    for r in ev.results:
        lines.append(f"**Q{r.id}** {'✅' if r.correct else '❌'}")
        lines.append(f"Your answer: {r.student_answer}")
        if not r.correct:
            lines.append(f"Correct answer: {r.correct_answer}")
        if r.explanation and r.explanation.strip():
            lines.append(f"*{r.explanation.strip()}*")
        if r.misconception:
            lines.append(f"💡 Misconception: {r.misconception}")
        if r.review_topic:
            lines.append(f"📖 Review: {r.review_topic}")
        lines.append("")

    s = ev.summary
    if s.strengths:
        lines += ["### Strengths"] + [f"- {x}" for x in s.strengths]
    if s.gaps:
        lines += ["\n### Areas to Improve"] + [f"- {x}" for x in s.gaps]
    if s.recommended_review:
        lines += ["\n### Recommended Review Topics"] + [f"- {x}" for x in s.recommended_review]

    return "\n".join(lines)


def _looks_like_answers(query: str) -> bool:
    """Return True only if the query looks like student answers to an existing quiz."""
    if len(query) > 600:
        return False
    matches = _ANSWER_RE.findall(query)
    if len(matches) < 2:
        return False
    lines = [l.strip() for l in query.splitlines() if l.strip()]
    question_lines = sum(1 for l in lines if l.endswith("?"))
    if lines and question_lines / len(lines) > 0.4:
        return False
    return True


def _serialize(questions: list[QuizQuestion]) -> str:
    return json.dumps([q.model_dump() for q in questions])


def _deserialize(raw: str) -> list[QuizQuestion]:
    return [QuizQuestion(**item) for item in json.loads(raw)]


def _encode_payload(questions: list[QuizQuestion]) -> str:
    """Embed quiz state in an HTML comment for stateless turn recovery."""
    b64 = base64.urlsafe_b64encode(_serialize(questions).encode()).decode("ascii")
    return f"{_PAYLOAD_PREFIX}{b64}{_PAYLOAD_SUFFIX}"


def _decode_payload(text: str) -> str | None:
    """Extract and decode quiz payload from assistant message text."""
    start = text.rfind(_PAYLOAD_PREFIX)
    if start == -1:
        return None
    ps = start + len(_PAYLOAD_PREFIX)
    pe = text.find(_PAYLOAD_SUFFIX, ps)
    if pe == -1:
        return None
    enc = text[ps:pe].strip()
    if not enc:
        return None
    try:
        decoded = base64.urlsafe_b64decode(enc.encode("ascii")).decode("utf-8")
        json.loads(decoded)
        return decoded
    except (ValueError, UnicodeDecodeError):
        return None


def _msg_text(msg: Any) -> str:
    """Best-effort text extraction from LangChain messages or raw dicts."""
    if isinstance(msg, BaseMessage):
        content = msg.content
    elif isinstance(msg, dict):
        content = msg.get("content", "")
    else:
        content = getattr(msg, "content", "")
    return "\n".join(str(x) for x in content) if isinstance(content, list) else str(content)


def _recover_questions(state: AgentState) -> str | None:
    """Return serialised quiz questions from state or prior assistant messages."""
    if raw := state.get("last_quiz_questions"):
        return raw
    for msg in reversed(state.get("messages", [])):
        role = (
            getattr(msg, "type", None)
            or (msg.get("role") if isinstance(msg, dict) else None)
        )
        if role not in {"ai", "assistant"}:
            continue
        if recovered := _decode_payload(_msg_text(msg)):
            return recovered
    return None

<<<<<<< Updated upstream
async def quiz_node(state: AgentState, llm: ChatOpenAI) -> dict[str, Any]:
=======

async def quiz_node(state: AgentState, llm: ChatOpenAI, *, util_llm: ChatOpenAI | None = None) -> dict[str, Any]:
>>>>>>> Stashed changes
    """Generate a quiz or route answer submissions to evaluation.

    Routing:
      - Query looks like numbered answers to quiz_evaluate_node
      - Otherwise → generate a new quiz from KUs

    Args:
        util_llm: Optional smaller LLM for the evaluation step (CPU-only offload).
    """
    query: str = state.get("query", "")

    if _looks_like_answers(query):
        recovered = _recover_questions(state)
        if not recovered:
            return {
                "response": (
                    "I detected quiz answers but couldn't find a prior quiz to grade. "
                    "Please generate a quiz first, then submit numbered answers "
                    "(e.g. `1. B`, `2. Paris`)."
                )
            }
        return await quiz_evaluate_node({**state, "last_quiz_questions": recovered}, llm, util_llm=util_llm)

    cfg = get_settings().agent
    timeout: int = cfg.llm_timeout
    kus: list[dict] = state.get("knowledge_units", [])
    student_memory: str = state.get("student_memory", "No prior student context available.")
    invoke_kwargs = {
        "query": query,
        "knowledge_units": _format_kus(kus),
        "student_memory": student_memory,
    }

    for attempt in range(1, _MAX_RETRIES + 2):
        try:
            raw = await asyncio.wait_for(
                _gen_chain(llm).ainvoke(invoke_kwargs), timeout=timeout
            )
            quiz = _validate_quiz(_parse_quiz(raw))
            serialized = _serialize(quiz.questions)
            log.info("quiz_generated", question_count=len(quiz.questions), attempt=attempt)
            return {
                "response": f"{_render_quiz(quiz, topic=query)}\n\n{_encode_payload(quiz.questions)}",
                "last_quiz_questions": serialized,
                "tool_calls": [{"tool": "quiz_generation", "questions": len(quiz.questions)}],
            }
        except Exception as exc:
            log.warning("quiz_gen_attempt_failed", attempt=attempt, exc=str(exc)[:200])
            if attempt > _MAX_RETRIES:
                break
            await asyncio.sleep(2)

    log.error("quiz_gen_all_retries_failed")
    return {"response": "Unable to generate a quiz right now. Please try again or refine your topic."}


async def quiz_evaluate_node(
    state: AgentState,
    llm: ChatOpenAI,
    *,
    util_llm: ChatOpenAI | None = None,
) -> dict[str, Any]:
    """Evaluate student answers and return per-question feedback + summary.

    Args:
        util_llm: Optional smaller LLM for structured grading (CPU-only offload).
    """
    cfg = get_settings().agent
    timeout: int = cfg.llm_timeout
    query: str = state.get("query", "")
    student_memory: str = state.get("student_memory", "No prior student context available.")

    raw_questions: str | None = state.get("last_quiz_questions")
    if not raw_questions:
        return {
            "response": (
                "I couldn't find a quiz to evaluate. "
                "Please generate a quiz first, then submit your answers."
            )
        }

    prior = _deserialize(raw_questions)
    qa_text = (
        "### Quiz questions\n"
        + "\n".join(
            f"Q{q.id} ({q.type}): {q.question} [Answer: {q.answer}]"
            for q in prior
        )
        + f"\n\n### Student answers\n{query}"
    )

    invoke_kwargs = {"questions_and_answers": qa_text, "student_memory": student_memory}
    _eval_llm = util_llm or llm

    for attempt in range(1, _MAX_RETRIES + 2):
        try:
<<<<<<< Updated upstream
            raw = await asyncio.wait_for(
                _eval_chain(llm).ainvoke(invoke_kwargs), timeout=timeout
            )
=======
            raw = await asyncio.wait_for(_eval_chain(_eval_llm).ainvoke(invoke_kwargs), timeout=timeout)
>>>>>>> Stashed changes
            evaluation = _parse_evaluation(raw)
            evaluation = _validate_evaluation(evaluation, prior)
            log.info("quiz_evaluated", score=evaluation.score, attempt=attempt)
            return {
                "response": _render_evaluation(evaluation),
                "tool_calls": [{"tool": "quiz_evaluation", "score": evaluation.score}],
            }
        except Exception as exc:
            log.warning("quiz_eval_attempt_failed", attempt=attempt, exc=str(exc)[:200])
            if attempt > _MAX_RETRIES:
                break
            await asyncio.sleep(3)

    log.error("quiz_eval_all_retries_failed")
    return {"response": "Unable to evaluate your answers right now. Please try again."}