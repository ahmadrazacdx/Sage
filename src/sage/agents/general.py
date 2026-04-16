"""General-mode node for Sage."""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI

from sage.agents.state import AgentState
from sage.config import get_settings
from sage.prompts import SYSTEM_PROMPT

log = structlog.get_logger(__name__)


async def general_node(state: AgentState, llm: ChatOpenAI) -> dict[str, Any]:
    """Direct LLM answer for general-mode queries."""
    cfg = get_settings().agent
    query: str = state.get("query", "")

    prompt = ChatPromptTemplate.from_messages([
        ("system", SYSTEM_PROMPT),
        ("human", "{query}"),
    ])

    try:
        result = await asyncio.wait_for(
            (prompt | llm).ainvoke({"query": query}),
            timeout=cfg.llm_timeout,
        )
    except asyncio.TimeoutError:
        log.error("general_node_timeout", timeout=cfg.llm_timeout)
        return {"response": "The request timed out. Please try again."}
    except Exception as exc:
        log.error(
            "general_node_failed",
            exc_type=type(exc).__name__,
            exc_msg=str(exc)[:200],
        )
        return {"response": "I ran into an issue. Please try again."}

    content: str = (
        result.content if hasattr(result, "content") else str(result)
    ) or ""
    log.info("general_node_complete", response_len=len(content))
    return {"response": content}
