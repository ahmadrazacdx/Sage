"""
Graph state definition for Sage agent orchestration.

`AgentState` is the single shared state schema threaded through every
node in the LangGraph `StateGraph`.  Fields are grouped by concern:

  Core           — message history, query text, resolved intent
  Retrieval      — chunks, knowledge units, retrieval cache
  Research       — plan, aggregated sources, final report
  Output         — response text, citations, diagrams
  Metadata       — tool traces, online mode flag
  UI Control     — dropdown selection, thinking toggle
"""

from __future__ import annotations

from typing import Annotated, Literal

from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


class AgentState(TypedDict, total=False):
    """Typed state for every node in the Sage agent graph."""

    # --- Core ---
    messages: Annotated[list, add_messages]
    query: str
    intent: Literal[
        "explain", "quiz", "diagram", "roadmap",
        "research", "fix", "general", "thinking",
    ]

    # --- Retrieval ---
    course_code: str | None
    semester: str | None
    expanded_query: str
    chunks: list[dict]
    knowledge_units: list[dict]
    retrieval_cache_key: str
    retrieval_cache_chunks: list[dict]
    retrieval_cache_kus: list[dict]

    # --- Research (Research Agent only) ---
    research_plan: dict | None
    research_sources: list[dict]
    research_report: str | None

    student_memory: str

    # --- Output ---
    response: str
    citations: list[dict]
    references: list[dict]
    diagrams: list[dict]
    last_quiz_questions: str | None

    # --- Metadata ---
    tool_calls: list[dict]
    online_mode: bool

    # --- Dropdown Routing ---
    mode: str
    thinking_mode: bool