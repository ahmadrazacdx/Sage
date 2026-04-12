"""
Agentic flow registry for Sage.

Primary Usage (build and run the compiled graph):

    from sage.agents import build_graph, AgentState

    graph = build_graph(llm)
    result = await graph.ainvoke({
        "messages": [...],
        "query":    "Explain B-trees",
        "mode":     "explain",
    })

Secondary Usage (individual node functions):

    from sage.agents import (
        router_node, retrieval_node, reasoning_node,
        response_node, quiz_node, diagram_node,
        planner_node, research_node, code_fix_node,
    )
"""

from __future__ import annotations

import structlog

# Graph Factory
from sage.agents.graph import build_graph

# Shared State Schema
from sage.agents.state import AgentState

# Node Functions
from sage.agents.router import (
    router_node,
    route_by_intent,
    route_post_retrieval,
    VALID_INTENTS,
    MODE_TO_INTENT,
)
from sage.agents.retrieval import retrieval_node
from sage.agents.reasoning import reasoning_node
from sage.agents.response import response_node
from sage.agents.quiz import quiz_node, quiz_evaluate_node
from sage.agents.diagram import diagram_node
from sage.agents.planner import planner_node
from sage.agents.research import research_node
from sage.agents.code_fix import code_fix_node, Diagnosis

log = structlog.get_logger(__name__)

# Node metadata

# All registered graph node names, in topological order.
NODE_NAMES: list[str] = [
    "router",
    "retrieval",
    "reasoning",
    "response_generator",
    "quiz",
    "diagram",
    "planner",
    "research",
    "code_fix",
]

# Nodes that receive the LLM instance via functools.partial at graph build time.
_LLM_BOUND_NODES: frozenset[str] = frozenset({
    "router",
    "retrieval",
    "reasoning",
    "quiz",
    "diagram",
    "planner",
    "research",
    "code_fix",
})

# Nodes that exit directly to END without passing through response_generator.
_DIRECT_EXIT_NODES: frozenset[str] = frozenset({
    "quiz",
    "diagram",
    "planner",
    "research",
    "code_fix",
})

# Aggregate Counts
TOTAL_NODE_COUNT: int   = len(NODE_NAMES)
LLM_NODE_COUNT: int     = len(_LLM_BOUND_NODES)
DIRECT_EXIT_COUNT: int  = len(_DIRECT_EXIT_NODES)

# Public Re-exports
__all__: list[str] = [
    # Graph factory
    "build_graph",
    # State schema
    "AgentState",
    # Node functions (core pipeline)
    "router_node",
    "retrieval_node",
    "reasoning_node",
    "response_node",
    # Node functions (specialised agents)
    "quiz_node",
    "quiz_evaluate_node",   # tool
    "diagram_node",
    "planner_node",
    "research_node",
    "code_fix_node",
    # Routing helpers
    "route_by_intent",
    "route_post_retrieval",
    # Routing metadata
    "VALID_INTENTS",
    "MODE_TO_INTENT",
    # Node registry
    "NODE_NAMES",
    "TOTAL_NODE_COUNT",
    "LLM_NODE_COUNT",
    "DIRECT_EXIT_COUNT",
    # Pydantic output models
    "Diagnosis",
]