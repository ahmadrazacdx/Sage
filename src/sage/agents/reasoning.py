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
from langchain_core.messages import AIMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI

from sage.agents.state import AgentState
from sage.config import get_settings
from sage.llm import _with_thinking
from sage.prompts import (
    REASONING_EXPLAIN_PROMPT,
    REASONING_THINKING_SYSTEM,
    SYSTEM_PROMPT,
)

log = structlog.get_logger(__name__)

_RE_THINK_BLOCK = re.compile(
    r"<think>(.*?)</think>", re.IGNORECASE | re.DOTALL
)
_RE_HALLUCINATED_KU = re.compile(r"\s*\[KU\d+\]", re.IGNORECASE)

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

def _intro(intent: str, query: str) -> str:
    """Return a short warm opener, deterministically chosen per query."""
    bank = _THINKING_INTROS if intent == "thinking" else _EXPLAIN_INTROS
    idx = int(hashlib.md5(query.encode()).hexdigest(), 16) % len(bank)
    return bank[idx]

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
        prompt = ChatPromptTemplate.from_messages([
            ("system", SYSTEM_PROMPT + "\n\n" + REASONING_THINKING_SYSTEM),
            ("human", "{query}"),
        ])
 
        try:
            result = await asyncio.wait_for(
                (prompt | llm).ainvoke({"query": query}),
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
        body = _extract_content(result)
        response = f"{_intro('thinking', query)}\n\n{body}"
        log.info(
            "reasoning_thinking_complete",
            response_len=len(response),
            requested_budget=thinking_budget,
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
        ("system", SYSTEM_PROMPT + "\n\n" + REASONING_EXPLAIN_PROMPT),
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