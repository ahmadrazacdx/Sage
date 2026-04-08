"""
Deterministic router for Sage agent graph.
Directly maps UI dropdown selections to intents.

Exports three functions:
  - `router_node`          : the async node function
  - `route_by_intent`      : conditional edge after router
  - `route_post_retrieval` : conditional edge after retrieval
"""

from __future__ import annotations

from typing import Any

import structlog
from langchain_openai import ChatOpenAI

from sage.agents.state import AgentState

log = structlog.get_logger(__name__)

VALID_INTENTS: frozenset[str] = frozenset({
    "explain", "quiz", "diagram", "roadmap",
    "research", "fix", "general", "thinking"
})

MODE_TO_INTENT: dict[str, str] = {
    "general":    "general",
    "thinking":   "thinking",
    "explain":    "explain",
    "quiz me":    "quiz",
    "diagram":    "diagram",
    "study plan": "roadmap",
    "research":   "research",
    "code fix":   "fix",
}


# --- Node function ---
async def router_node(state: AgentState, llm: ChatOpenAI) -> dict[str, Any]:
    """Fully deterministic router: dropdown takes absolute priority."""
    mode_raw: str = state.get("mode", "General")
    mode = mode_raw.lower().strip()

    query: str = state.get("query", "")

    # Map the dropdown selection directly to an intent
    mapped_intent = MODE_TO_INTENT.get(mode, "general")

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
