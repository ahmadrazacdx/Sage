"""
Hybrid router for Sage agent graph.

Only "general" mode invokes LLM-based intent classification. 
Unknown/failed classifications default to "general" (never crash).

Exports three functions:
  - `router_node`          : the async node function
  - `route_by_intent`      : conditional edge after router
  - `route_post_retrieval` : conditional edge after retrieval
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from sage.agents.state import AgentState
from sage.config import get_settings
from sage.prompts import ROUTER_PROMPT

log = structlog.get_logger(__name__)

VALID_INTENTS: frozenset[str] = frozenset({
    "explain", "quiz", "diagram", "roadmap",
    "research", "fix", "general", "thinking"
})

MODE_TO_INTENT: dict[str, str | None] = {
    "general":    None,      # Use LLM classification.
    "thinking":   "thinking",
    "explain":    "explain",
    "quiz me":    "quiz",
    "diagram":    "diagram",
    "study plan": "roadmap",
    "research":   "research",
    "code fix":   "fix",
}

_LLM_TIMEOUT_S: int = 120


class RouterOutput(BaseModel):
    """LLM-produced classification."""

    needs_thinking: bool = Field(
        description="True if the query requires complex logical deduction, math proofs, or deep code debugging. False if it's general chat or simple facts."
    )


# --- Router prompt template ---
_ROUTER_CHAIN = ChatPromptTemplate.from_messages([
    ("system", ROUTER_PROMPT),
    ("human", "{query}"),
])

# --- Node function ---
async def router_node(state: AgentState, llm: ChatOpenAI) -> dict[str, Any]:
    """Hybrid router: dropdown takes priority, LLM fallback for 'general'.

    When the user explicitly selects a mode via the dropdown, the
    LLM classification call is skipped.

    Only the `"general"` mode triggers the LLM router to evaluate
    if the query is simple (`general`) or complex (`thinking`).
    """
    mode_raw: str = state.get("mode", "General")
    mode = mode_raw.lower().strip()

    query: str = state.get("query", "")

    # --- Explicit Mode Selected ---
    mapped_intent = MODE_TO_INTENT.get(mode)
    if mapped_intent is not None:
        log.info(
            "router_explicit",
            mode=mode,
            intent=mapped_intent,
        )
        return {
            "intent": mapped_intent,
            "expanded_query": query,
            "thinking_mode": mapped_intent == "thinking",
        }

    # --- "General" Mode: LLM Classifies ---
    cfg = get_settings().agent
    try:
        from langchain_core.output_parsers import JsonOutputParser
        parser = JsonOutputParser(pydantic_object=RouterOutput)
        
        chain = (
            _ROUTER_CHAIN.partial(format_instructions=parser.get_format_instructions()) 
            | llm
            | parser
        )
        
        result_raw = await asyncio.wait_for(
            chain.ainvoke({"query": query}),
            timeout=cfg.llm_timeout,
        )
        
        result = RouterOutput(**result_raw) if isinstance(result_raw, dict) else result_raw

        thinking = result.needs_thinking
        intent = "thinking" if thinking else "general"
        expanded = query

        log.info(
            "router_llm_classified",
            intent=intent,
            thinking_mode=thinking,
        )
    except Exception as exc:
        log.warning(
            "router_llm_fallback",
            exc_type=type(exc).__name__,
            exc_msg=str(exc)[:200],
        )
        intent = "general"
        expanded = query
        thinking = False

    return {
        "intent": intent,
        "expanded_query": expanded,
        "thinking_mode": thinking,
    }

def route_by_intent(state: AgentState) -> str:
    """Conditional edge from router to the appropriate subgraph."""
    intent = state.get("intent", "general")
    if intent not in VALID_INTENTS:
        log.warning("route_unknown_intent", intent=intent, fallback="general")
        return "general"
    
    if intent == "thinking":
        return "reasoning"
        
    return intent


def route_post_retrieval(state: AgentState) -> str:
    """Conditional edge after retrieval dispatches to the right agent.
    Only (explain, quiz, diagram) intents pass through retrieval.
    """
    return state.get("intent", "explain")
