"""General-mode node for Sage."""

from __future__ import annotations

import asyncio
import re
from typing import Any

import structlog
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from sage.agents.state import AgentState
from sage.config import get_settings
from sage.prompts import SYSTEM_PROMPT

log = structlog.get_logger(__name__)

_RE_HALLUCINATED_KU = re.compile(r"\s*\[KU\d+\]", re.IGNORECASE)
_RE_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)


def _history_window(ctx_size: int) -> int:
    """Return max history turn-pairs to send based on context window size."""
    if ctx_size <= 4_096:
        return 4
    if ctx_size <= 8_192:
        return 8
    if ctx_size <= 16_384:
        return 12
    return 16


def _clean_general_output(text: str) -> str:
    """Remove think blocks and hallucinated [KU#] citation tags."""
    text = _RE_THINK_BLOCK.sub("", text)
    text = text.replace("<think>", "").replace("</think>", "")
    text = _RE_HALLUCINATED_KU.sub("", text)
    return text.strip()


async def general_node(state: AgentState, llm: ChatOpenAI) -> dict[str, Any]:
    """Direct LLM answer for general-mode queries."""
    cfg_agent = get_settings().agent
    cfg_llm = get_settings().llm

    ctx_size = cfg_llm.active_context_size or 4_096
    window = _history_window(ctx_size)

    student_memory: str = state.get("student_memory", "")
    history_summary: str = state.get("history_summary", "") or ""

    messages: list = state.get("messages", [])
    if not messages:
        messages = [HumanMessage(content=state.get("query", ""))]
    capped = messages[-(window * 2) :]
    system_parts: list[str] = [SYSTEM_PROMPT]
    if student_memory:
        system_parts.append(student_memory)
    if history_summary:
        system_parts.append(f"Previous conversation summary:\n{history_summary}")
    prompt_messages: list = [SystemMessage(content="\n\n".join(system_parts))]
    prompt_messages.extend(capped)

    try:
        result = await asyncio.wait_for(
            llm.ainvoke(prompt_messages),
            timeout=cfg_agent.llm_timeout,
        )
    except TimeoutError:
        log.error("general_node_timeout", timeout=cfg_agent.llm_timeout)
        return {"response": "The request timed out. Please try again."}
    except Exception as exc:
        log.error(
            "general_node_failed",
            exc_type=type(exc).__name__,
            exc_msg=str(exc)[:200],
        )
        return {"response": "I ran into an issue. Please try again."}

    raw: str = (result.content if hasattr(result, "content") else str(result)) or ""
    content = _clean_general_output(raw)
    log.info(
        "general_node_complete",
        response_len=len(content),
        ctx_size=ctx_size,
        window=window,
        history_capped=len(messages) > window * 2,
    )
    return {"messages": [AIMessage(content=content)], "response": content}
