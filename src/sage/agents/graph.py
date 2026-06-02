"""
LangGraph state graph assembly for Sage.

It constructs the full agent graph, wraps every node with
`with_error_boundary`, and returns a compiled `CompiledStateGraph`.
"""

from __future__ import annotations

import functools
from typing import Any

import structlog
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from sage.agents.code_fix import code_fix_node
from sage.agents.diagram import diagram_node
from sage.agents.general import general_node
from sage.agents.planner import planner_node
from sage.agents.quiz import quiz_node
from sage.agents.reasoning import reasoning_node
from sage.agents.research import research_node
from sage.agents.response import response_node
from sage.agents.retrieval import retrieval_node
from sage.agents.router import route_by_intent, route_post_retrieval, router_node
from sage.agents.state import AgentState
from sage.utils import with_error_boundary

log = structlog.get_logger(__name__)


def _bind_llm(node_fn: Any, llm: ChatOpenAI) -> Any:
    """Bind the LLM instance to a node function that accepts (state, llm)."""
    return functools.partial(node_fn, llm=llm)


def _route_post_reasoning(state: AgentState) -> str:
    """Route out of the reasoning node.

    - `explain` intent goes to citation formatter (response_generator)
    - `thinking` intent goes to EXIT directly
    """
    return "response_generator" if state.get("intent") == "explain" else END


def build_graph(llm: ChatOpenAI) -> CompiledStateGraph:
    """Assemble, compile, and return the Sage agent graph.

    Args:
        llm: The `ChatOpenAI` instance pointed at the local
             llama-server.  Created once by `create_llm(port)`
             in `sage.llm` and passed here during startup.

    Returns:
        A compiled `CompiledStateGraph` with all nodes error-bounded
        and edges wired per the topology in the module docstring.
        Ready for `await graph.ainvoke(state)`.
    """
    graph = StateGraph(AgentState)

    # Nodes
    graph.add_node("router", with_error_boundary(_bind_llm(router_node, llm)))
    graph.add_node("retrieval", with_error_boundary(_bind_llm(retrieval_node, llm)))
    graph.add_node("general", with_error_boundary(_bind_llm(general_node, llm)))
    graph.add_node("reasoning", with_error_boundary(_bind_llm(reasoning_node, llm)))
    graph.add_node("response_generator", with_error_boundary(response_node))
    graph.add_node("quiz", with_error_boundary(_bind_llm(quiz_node, llm)))
    graph.add_node("diagram", with_error_boundary(_bind_llm(diagram_node, llm)))
    graph.add_node("planner", with_error_boundary(_bind_llm(planner_node, llm)))
    graph.add_node("research", with_error_boundary(_bind_llm(research_node, llm)))
    graph.add_node("code_fix", with_error_boundary(_bind_llm(code_fix_node, llm)))

    # Entry Edge
    graph.add_edge(START, "router")
    graph.add_conditional_edges(
        "router",
        route_by_intent,
        {
            "explain": "retrieval",
            "quiz": "retrieval",
            "diagram": "retrieval",
            "general": "general",
            "reasoning": "reasoning",
            "roadmap": "planner",
            "research": "research",
            "fix": "code_fix",
        },
    )

    # Post-retrieval dispatch
    graph.add_conditional_edges(
        "retrieval",
        route_post_retrieval,
        {
            "explain": "reasoning",
            "quiz": "quiz",
            "diagram": "diagram",
        },
    )

    # Post-reasoning dispatch
    graph.add_conditional_edges(
        "reasoning",
        _route_post_reasoning,
        {
            "response_generator": "response_generator",
            END: END,
        },
    )

    # Terminal Edges
    graph.add_edge("general", END)
    graph.add_edge("response_generator", END)
    graph.add_edge("quiz", END)
    graph.add_edge("diagram", END)
    graph.add_edge("planner", END)
    graph.add_edge("research", END)
    graph.add_edge("code_fix", END)

    # Compile
    compiled = graph.compile()

    log.info(
        "graph_compiled",
        nodes=len(compiled.nodes),
        hint="No checkpointer configured — state is ephemeral.",
    )

    return compiled
