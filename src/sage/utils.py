"""
Shared utilities for Sage.

Provides:
  - configure_logging()  — structlog JSON setup, called once at startup
  - with_error_boundary() — decorator that wraps every LangGraph agent node,
                            preventing unhandled exceptions from crashing the
                            graph
  - estimate_tokens()    — lightweight token estimator (no tokenizer needed)
  - clamp()              — generic numeric clamp helper
"""

from __future__ import annotations

import functools
import time
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

import structlog

log = structlog.get_logger()

# ----Logging----

_LOGGING_CONFIGURED = False


def configure_logging(level: str = "info") -> None:
    """
    Configure structlog for the entire process.

    Must be called once, as early as possible in __main__.py before any
    module-level loggers emit their first message.

    Processors:
      - add_log_level     — include level name in every event
      - add_logger_name   — include calling module name
      - TimeStamper(iso)  — ISO-8601 timestamp
      - StackInfoRenderer — attach stack info when present
      - JSONRenderer      — machine-readable output in production
    """
    global _LOGGING_CONFIGURED
    if _LOGGING_CONFIGURED:
        return

    import logging as _stdlib_logging

    _level = getattr(_stdlib_logging, level.upper(), _stdlib_logging.INFO)

    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.dev.ConsoleRenderer()
            if level == "debug"
            else structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    _LOGGING_CONFIGURED = True


# ----Error boundary----
_StateT = TypeVar("_StateT")


def with_error_boundary(
    node_fn: Callable[..., Coroutine[Any, Any, dict[str, Any]]],
) -> Callable[..., Coroutine[Any, Any, dict[str, Any]]]:
    """
    Decorator that wraps every LangGraph agent node.

    Catches all unhandled exceptions, logs them with structured context,
    and returns a user-friendly error dict instead of crashing the graph.

    The decorator is the last-resort catch. Node internals must still handle
    expected failures (network timeouts, malformed LLM output, etc.) locally.

    Usage:
        graph.add_node("router", with_error_boundary(router_agent))
    """

    _node_name: str = (
        getattr(node_fn, "__name__", None)
        or getattr(getattr(node_fn, "func", None), "__name__", None)
        or repr(node_fn)
    )

    async def _wrapper(state: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        node_name = _node_name
        t0 = time.perf_counter()
        try:
            result = await node_fn(state, *args, **kwargs)
            log.debug(
                "node_complete",
                node=node_name,
                latency_ms=round((time.perf_counter() - t0) * 1000),
            )
            return result
        except Exception as exc:
            latency_ms = round((time.perf_counter() - t0) * 1000)
            log.error(
                "node_failed",
                node=node_name,
                exc_type=type(exc).__name__,
                exc_msg=str(exc)[:200],
                latency_ms=latency_ms,
            )
            return {
                "response": (
                    f"An error occurred in the **{node_name}** step. "
                    f"Please try again or rephrase your query.\n\n"
                    f"*{type(exc).__name__}: {str(exc)[:200]}*"
                )
            }

    return _wrapper


# ----Token estimation----
_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Return a fast, approximate token count for *text*.

    Uses the 1 token ≈ 4 chars heuristic (sufficient for budget guards).
    Does not load a tokenizer — safe to call in any hot path.
    """
    return max(1, len(text) // _CHARS_PER_TOKEN)


# ----Numeric utilities----
def clamp(value: float, lo: float, hi: float) -> float:
    """Return *value* clamped to the closed interval [lo, hi]."""
    return max(lo, min(hi, value))
