"""
Reasoning agent node for Sage.
Handles the explain & thinking paths.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from typing import Any

import structlog
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI

from sage.agents.state import AgentState
from sage.config import get_settings
from sage.llm import _with_thinking
from sage.prompts import (
    REASONING_EXPLAIN_PROMPT,
    REASONING_THINKING_SYSTEM,
    SYSTEM_PROMPT,
    SYSTEM_PROMPT_WITH_CITATIONS,
    THINKING_TOOLS_SYSTEM,
)

log = structlog.get_logger(__name__)

_RE_THINK_BLOCK = re.compile(
    r"<think>(.*?)</think>", re.IGNORECASE | re.DOTALL
)
_RE_HALLUCINATED_KU = re.compile(r"\s*\[KU\d+\]", re.IGNORECASE)
_RE_REASONING_LEAD = re.compile(
    r"^(the user|let me|i need|i should|i can|i will|i(?:'| )?ll|first|then|to solve|"
    r"the calculator|search results|this is|i must)\b",
    re.IGNORECASE,
)
_RE_ANSWER_LEAD = re.compile(
    r"^(the result|result|answer|final answer|therefore|thus|in short|in summary|"
    r"intuitively|intuition|technical details|key takeaway)\b",
    re.IGNORECASE,
)

_THINKING_INTROS: list[str] = [
    "Great question. let me think through this carefully.",
    "Good one. I'll reason through this step by step.",
    "Let me work through this systematically before giving you an answer.",
]

_EXPLAIN_INTROS: list[str] = [
    "Here's a detailed breakdown drawing from your course material.",
    "Let me walk you through this, grounding the explanation in your resource material.",
    "I'll explain this thoroughly using the relevant course content.",
]

_MAX_VISIBLE_SAFE_THINKING_BUDGET: int = 512
_FAST_MATH_THINKING_BUDGET: int = 128
_MATH_KEYWORDS: tuple[str, ...] = (
    "what is",
    "calculate",
    "compute",
    "sqrt",
    "log",
    "sin",
    "cos",
    "tan",
)
_MATH_PREFIXES: tuple[str, ...] = (
    "what is",
    "calculate",
    "compute",
    "evaluate",
    "solve",
    "find",
    "what's",
)

def _intro(intent: str, query: str) -> str:
    """Return a short warm opener, deterministically chosen per query."""
    bank = _THINKING_INTROS if intent == "thinking" else _EXPLAIN_INTROS
    idx = int(hashlib.md5(query.encode()).hexdigest(), 16) % len(bank)
    return bank[idx]


def _is_math_focused_query(query: str) -> bool:
    """Heuristic: detect compact calculation queries for lower thinking budget."""
    text = query.lower().strip()
    if not text:
        return False
    if any(k in text for k in _MATH_KEYWORDS):
        return any(ch.isdigit() for ch in text)

    operator_chars = set("+-*/^()=")
    operator_count = sum(1 for ch in text if ch in operator_chars)
    digit_count = sum(1 for ch in text if ch.isdigit())
    return digit_count >= 2 and operator_count >= 1


def _extract_math_expression(query: str) -> str | None:
    """Extract a calculator-safe expression from a short NL math query."""
    text = query.strip()
    if not text:
        return None

    lowered = text.lower()
    for prefix in _MATH_PREFIXES:
        token = f"{prefix} "
        if lowered.startswith(token):
            text = text[len(token):].strip()
            lowered = text.lower()
            break

    for stem in ("the result of ", "result of ", "value of "):
        if lowered.startswith(stem):
            text = text[len(stem):].strip()
            lowered = text.lower()
            break

    text = text.rstrip("?.! ")
    text = text.strip("`")
    if "=" in text:
        text = text.split("=", maxsplit=1)[0].strip()
    if not text or not any(ch.isdigit() for ch in text):
        return None

    allowed_chars = set("0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_+-*/%^()., \t")
    if any(ch not in allowed_chars for ch in text):
        return None
    return text


def _format_calculator_result(value: Any) -> str:
    """Format calculator output with stable integer rendering when exact."""
    if isinstance(value, float):
        rounded = round(value)
        if abs(value - rounded) < 1e-12:
            return str(int(rounded))
    return str(value)

def _format_knowledge_units(kus: list[dict]) -> str:
    """Render KU list as numbered lines for prompt injection.

    Returns "None available." on empty input so the model gets an explicit
    signal rather than a blank field.
    """
    if not kus:
        return "None available."
    lines: list[str] = []
    for ku in kus:
        ku_id = ku.get("id", "KU?")
        claim = ku.get("claim", ku.get("content", ""))
        source = ku.get("source_file", "unknown")
        page = ku.get("source_page", "")
        suffix = f" p.{page}" if page else ""
        lines.append(f"- {claim} [{ku_id}] ({source}{suffix})")
    return "\n".join(lines)

def _strip_think_blocks(text: str) -> str:
    """Strip all <think>...</think> blocks and stray tags from model text."""
    cleaned = _RE_THINK_BLOCK.sub("", text)
    cleaned = cleaned.replace("<think>", "").replace("</think>", "")
    return cleaned.strip()


def _strip_hallucinated_kus(text: str) -> str:
    """Remove all [KU#] citation tags from text.

    Called when no Knowledge Units were provided to prevent the model
    from fabricating citation tags that reference nothing.
    """
    return _RE_HALLUCINATED_KU.sub("", text)


def _extract_think_blocks(text: str) -> str:
    """Return concatenated contents of all <think>...</think> blocks."""
    matches = _RE_THINK_BLOCK.findall(text)
    if not matches:
        return ""
    return "\n".join(m.strip() for m in matches if m.strip())


def _extract_content(result: Any, include_native_thinking: bool = False) -> str:
    """Extract answer text and optionally prepend native reasoning trace."""
    if not isinstance(result, AIMessage):
        return str(result)

    raw = result.content or ""
    think = _extract_think_blocks(raw)
    answer = _strip_think_blocks(raw)

    if include_native_thinking and think and answer:
        return f"<think>\n{think}\n</think>\n\n{answer}"
    if include_native_thinking and think:
        log.warning(
            "reasoning_content_only_no_answer",
            think_len=len(think),
            answer_len=len(answer),
        )
        return f"<think>\n{think}\n</think>"
    return answer


def _looks_like_reasoning_paragraph(paragraph: str) -> bool:
    """Best-effort detector for chain-of-thought style lead-in text."""
    p = paragraph.strip()
    if not p:
        return False
    if _RE_REASONING_LEAD.match(p):
        return True

    lowered = p.lower()
    if "tool" in lowered and ("result" in lowered or "calculator" in lowered or "search" in lowered):
        return True
    return False


def _looks_like_answer_paragraph(paragraph: str) -> bool:
    """Heuristic detector for where the student-facing answer begins."""
    p = paragraph.strip()
    if not p:
        return False
    if _RE_ANSWER_LEAD.match(p):
        return True

    lowered = p.lower()
    if lowered.startswith("## "):
        return True
    if lowered.startswith("the result is") or lowered.startswith("the answer is"):
        return True
    return False


def _ensure_think_wrapped(response: str) -> str:
    """Guarantee thinking-mode output contains explicit think/answer channels."""
    text = (response or "").strip()
    if not text:
        return "<think>\nReasoning completed.\n</think>"

    if "<think>" in text and "</think>" in text:
        return text

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    reasoning_parts: list[str] = []
    idx = 0
    while idx < len(paragraphs) and _looks_like_reasoning_paragraph(paragraphs[idx]):
        reasoning_parts.append(paragraphs[idx])
        idx += 1

    if reasoning_parts and idx < len(paragraphs):
        trace = "\n\n".join(reasoning_parts)
        answer = "\n\n".join(paragraphs[idx:]).strip()
        return f"<think>\n{trace}\n</think>\n\n{answer}"

    for split_idx, paragraph in enumerate(paragraphs):
        if split_idx > 0 and _looks_like_answer_paragraph(paragraph):
            trace = "\n\n".join(paragraphs[:split_idx])
            answer = "\n\n".join(paragraphs[split_idx:]).strip()
            return f"<think>\n{trace}\n</think>\n\n{answer}"

    if len(paragraphs) >= 2:
        trace = paragraphs[0]
        answer = "\n\n".join(paragraphs[1:]).strip()
        return f"<think>\n{trace}\n</think>\n\n{answer}"

    return f"<think>\nCompleted reasoning and tool steps.\n</think>\n\n{text}"

async def reasoning_node(state: AgentState, llm: ChatOpenAI) -> dict[str, Any]:
    """Reasoning agent, handles `thinking` and `explain` intents.

    Args:
        state: Current agent state dict (AgentState TypedDict).
        llm:   Shared ChatOpenAI instance pointing at the local llama.cpp server.
 
    Returns:
        Dict with key `response` (str). When thinking is active the
        response begins with a `<think>…</think>` block followed by the
        visible answer.
    """
    cfg = get_settings().agent
    llm_cfg = get_settings().llm

    intent: str = state.get("intent", "general")
    query: str = state.get("query", "")
    kus: list[dict] = state.get("knowledge_units", [])
    student_memory: str = state.get(
        "student_memory", "No prior student context available."
    )

    thinking_budget: int = llm_cfg.reasoning_budget

    # Thinking path
    if intent == "thinking":
        # Bind calculator + web-search tools
        calculator_tool = None
        try:
            from sage.tools.calculator import calculator
            from sage.tools.search import search_web
            calculator_tool = calculator
            is_math_query = _is_math_focused_query(query)
            thinking_tools = [calculator] if is_math_query else [calculator, search_web]
        except ImportError:
            is_math_query = _is_math_focused_query(query)
            thinking_tools = []

        if is_math_query and calculator_tool is not None:
            expression = _extract_math_expression(query)
            if expression:
                try:
                    calc_payload = calculator_tool.invoke({"expression": expression})
                except Exception as exc:
                    log.warning(
                        "reasoning_math_fast_path_invoke_failed",
                        exc_type=type(exc).__name__,
                        exc_msg=str(exc)[:200],
                    )
                else:
                    if isinstance(calc_payload, dict) and calc_payload.get("success") is True:
                        result_text = _format_calculator_result(calc_payload.get("result"))
                        response = f"The result is {result_text}."
                        log.info(
                            "reasoning_math_fast_path_complete",
                            expression=expression[:80],
                            response_len=len(response),
                        )
                        return {"response": response}
                    if isinstance(calc_payload, dict):
                        log.warning(
                            "reasoning_math_fast_path_tool_error",
                            expression=expression[:80],
                            error=str(calc_payload.get("error", ""))[:200],
                        )

        system_thinking = SYSTEM_PROMPT + REASONING_THINKING_SYSTEM + THINKING_TOOLS_SYSTEM
        effective_budget = min(thinking_budget, _MAX_VISIBLE_SAFE_THINKING_BUDGET)
        if is_math_query:
            effective_budget = min(effective_budget, _FAST_MATH_THINKING_BUDGET)
        llm_with_thinking = _with_thinking(llm, effective_budget)
        llm_with_tools = (
            llm_with_thinking.bind_tools(thinking_tools)
            if thinking_tools
            else llm_with_thinking
        )

        messages_so_far: list[Any] = [
            SystemMessage(content=system_thinking),
            HumanMessage(content=query),
        ]
        response = ""
        for _iter in range(3):
            try:
                result = await asyncio.wait_for(
                    llm_with_tools.ainvoke(messages_so_far),
                    timeout=cfg.llm_timeout,
                )
            except asyncio.TimeoutError:
                log.error("reasoning_thinking_timeout", timeout=cfg.llm_timeout)
                return {"response": "The request timed out. Please try again."}
            except Exception as exc:
                log.error(
                    "reasoning_thinking_failed",
                    exc_type=type(exc).__name__,
                    exc_msg=str(exc)[:200],
                )
                return {"response": "I ran into an issue processing your request. Please try again."}

            tool_calls = getattr(result, "tool_calls", None) or []
            if not tool_calls:
                response = _extract_content(result, include_native_thinking=True)
                break

            tool_msgs: list[ToolMessage] = []
            for tc in tool_calls:
                tool_name = tc.get("name", "")
                tool_args = tc.get("args", {})
                tc_id = tc.get("id", "")
                try:
                    if tool_name == "calculator" and thinking_tools:
                        tool_result = calculator.invoke(tool_args)
                    elif tool_name == "search_web" and thinking_tools:
                        tool_result = await search_web.ainvoke(tool_args)
                    else:
                        tool_result = f"Tool {tool_name!r} not available."
                except Exception as te:
                    tool_result = f"Tool error: {te}"
                tool_msgs.append(ToolMessage(content=str(tool_result), tool_call_id=tc_id))

            messages_so_far.extend([result, *tool_msgs])
        else:
            response = _extract_content(result, include_native_thinking=True)  # type: ignore[possibly-undefined]

        if not response.strip():
            response = "I could not produce a reliable final answer. Please try rephrasing the request."

        response = _ensure_think_wrapped(response)

        log.info(
            "reasoning_thinking_complete",
            response_len=len(response),
            requested_budget=thinking_budget,
            effective_budget=effective_budget,
        )
        return {"response": response}

    # Explain path
    ku_text = _format_knowledge_units(kus)

    # Build KU-specific tag list for citation
    ku_tags = ", ".join(f"[{ku['id']}]" for ku in kus) if kus else ""

    if kus:
        human_msg = (
            "{query}\n\n"
            f"Remember: cite Knowledge Units as {ku_tags} inline. "
            "End with **Key Takeaway:**"
        )
    else:
        human_msg = (
            "{query}\n\n"
            "No course material was provided. Start your answer with: "
            '"No course material found — answering from general knowledge." '
            "End with **Key Takeaway:**"
        )

    prompt = ChatPromptTemplate.from_messages([
        ("system", SYSTEM_PROMPT_WITH_CITATIONS + "\n\n" + REASONING_EXPLAIN_PROMPT),
        ("human", human_msg),
    ])
 
    try:
        result = await asyncio.wait_for(
            (prompt | llm).ainvoke({
                "query": query,
                "knowledge_units": ku_text,
                "student_memory": student_memory,
            }),
            timeout=cfg.llm_timeout,
        )
    except asyncio.TimeoutError:
        log.error("reasoning_explain_timeout", timeout=cfg.llm_timeout)
        return {"response": "The request timed out. Please try again."}
    except Exception as exc:
        log.error(
            "reasoning_explain_failed",
            exc_type=type(exc).__name__,
            exc_msg=str(exc)[:200],
        )
        return {"response": "I ran into an issue while explaining your question. Please try again."}
    body = _extract_content(result)
    if not kus:
        body = _strip_hallucinated_kus(body)

    response = f"{_intro('explain', query)}\n\n{body}"
    log.info("reasoning_explain_complete", ku_count=len(kus), response_len=len(response))
    return {"response": response}