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

import json
import re
import time
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

import structlog
from pydantic import BaseModel, ValidationError

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
    import os
    from pathlib import Path

    _level = getattr(_stdlib_logging, level.upper(), _stdlib_logging.INFO)

    processors = [
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    sage_home = os.environ.get("SAGE_HOME")
    if sage_home:
        log_file = Path(sage_home) / "logs" / "sage.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)

        processors.append(structlog.processors.JSONRenderer())
        structlog.configure(
            processors=processors,
            wrapper_class=structlog.make_filtering_bound_logger(_level),
            context_class=dict,
            logger_factory=structlog.WriteLoggerFactory(file=log_file.open("w", encoding="utf-8")),
            cache_logger_on_first_use=True,
        )
    else:
        processors.append(structlog.dev.ConsoleRenderer() if level == "debug" else structlog.processors.JSONRenderer())
        structlog.configure(
            processors=processors,
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
        getattr(node_fn, "__name__", None) or getattr(getattr(node_fn, "func", None), "__name__", None) or repr(node_fn)
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


# Numeric utilities
def clamp(value: float, lo: float, hi: float) -> float:
    """Return *value* clamped to the closed interval [lo, hi]."""
    return max(lo, min(hi, value))


# Structured output helpers
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)
_FENCED_BLOCK_RE = re.compile(
    r"```(?P<lang>[A-Za-z0-9_+-]*)[ \t]*\n?(?P<body>[\s\S]*?)```",
    re.IGNORECASE,
)
_THINK_BLOCK_RE = re.compile(r"<t?think>[\s\S]*?</t?think>", re.IGNORECASE)
_THINK_TAG_RE = re.compile(r"</?t?think>", re.IGNORECASE)


def is_think_grammar_error(exc: Exception) -> bool:
    """Return True when llama.cpp grammar mode fails on `<think>` tokens."""
    msg = str(exc).lower()
    return "failed to initialize samplers" in msg and "empty grammar stack" in msg and "<think>" in msg


def strip_think_markers(text: Any) -> str:
    """Remove `<think>...</think>` blocks and stray tag fragments."""
    if not isinstance(text, str):
        text = _content_to_text(text)
    if not text:
        return ""
    cleaned = _THINK_BLOCK_RE.sub("", text)
    cleaned = _THINK_TAG_RE.sub("", cleaned)
    return cleaned.strip()


def extract_fenced_block(
    text: str,
    *,
    preferred_languages: set[str] | None = None,
) -> str | None:
    """Return fenced block body, preferring specific languages when provided."""
    if not text:
        return None

    preferred = {lang.lower() for lang in (preferred_languages or set())}
    first_body: str | None = None

    for match in _FENCED_BLOCK_RE.finditer(text):
        lang = (match.group("lang") or "").lower()
        body = (match.group("body") or "").strip("\n")
        if not body:
            continue
        if first_body is None:
            first_body = body
        if preferred and lang in preferred:
            return body
        if not preferred:
            return body

    return first_body


def close_unbalanced_fenced_blocks(text: str) -> str:
    """Append a closing fence when markdown fences are left unbalanced."""
    if not text:
        return ""

    fence_count = sum(1 for line in text.splitlines() if re.match(r"^\s*```", line))
    if fence_count % 2 == 1:
        return text.rstrip() + "\n```"
    return text


def _content_to_text(content: Any) -> str:
    """Normalize provider-specific content payloads to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
                continue
            if isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if text:
                    parts.append(str(text))
                continue
            text = getattr(item, "text", "")
            if text:
                parts.append(str(text))
        return "\n".join(parts)
    return str(content)


def _balanced_json_candidate(text: str) -> str | None:
    """Return the first balanced JSON object/array found in text."""
    start = -1
    depth = 0
    in_string = False
    escape = False

    for i, ch in enumerate(text):
        if start == -1:
            if ch in "[{":
                start = i
                depth = 1
            continue

        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
            continue
        if ch in "[{":
            depth += 1
            continue
        if ch in "]}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]

    return None


def _structured_candidates(raw: Any) -> list[str]:
    """Return ordered JSON candidate strings extracted from model output."""
    maybe_content = raw.get("content", raw) if isinstance(raw, dict) else getattr(raw, "content", raw)

    raw_text = _content_to_text(maybe_content).strip()
    text = strip_think_markers(raw_text) or raw_text
    if not text:
        return []

    candidates: list[str] = []

    for m in _JSON_FENCE_RE.finditer(text):
        block = m.group(1).strip()
        if block:
            candidates.append(block)
            nested = _balanced_json_candidate(block)
            if nested and nested != block:
                candidates.append(nested)

    if text.startswith("{") or text.startswith("["):
        candidates.append(text)

    nested = _balanced_json_candidate(text)
    if nested:
        candidates.append(nested)

    candidates.append(text)

    # Preserve order while deduplicating.
    seen: set[str] = set()
    ordered: list[str] = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            ordered.append(c)
    return ordered


def parse_structured_output[StructuredModelT: BaseModel](raw: Any, schema: type[StructuredModelT]) -> StructuredModelT:
    """Parse model output into a Pydantic schema using tolerant JSON extraction."""
    if isinstance(raw, schema):
        return raw
    if isinstance(raw, dict):
        try:
            return schema.model_validate(raw)
        except ValidationError:
            pass

    last_exc: Exception | None = None
    for candidate in _structured_candidates(raw):
        try:
            return schema.model_validate_json(candidate)
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            last_exc = exc

    preview = strip_think_markers(_content_to_text(getattr(raw, "content", raw)))[:220]
    try:
        import os
        debug_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "planner_validation_debug.log")
        with open(debug_path, "w", encoding="utf-8") as f:
            f.write(f"RAW OUTPUT:\n{_content_to_text(getattr(raw, 'content', raw))}\n\nVALIDATION ERROR:\n{repr(last_exc)}\n")
    except Exception:
        pass
    raise ValueError(f"Unable to parse structured output for {schema.__name__}. Preview: {preview!r}") from last_exc


async def ainvoke_structured_with_fallback[StructuredModelT: BaseModel](
    *,
    prompt: Any,
    llm: Any,
    schema: type[StructuredModelT],
    payload: dict[str, Any],
    timeout_s: float,
    logger: Any,
    event_prefix: str,
    prefer_raw_json: bool = False,
) -> StructuredModelT:
    """Invoke structured output; fall back to raw JSON parsing on grammar failures."""
    import asyncio
    if not prefer_raw_json:
        try:
            bound_kwargs: dict = getattr(llm, "kwargs", {})
            extra_body: dict = bound_kwargs.get("extra_body", {})
            chat_kwargs: dict = extra_body.get("chat_template_kwargs", {})
            if chat_kwargs.get("enable_thinking") is True:
                prefer_raw_json = True
        except Exception:
            pass

    if prefer_raw_json:
        raw_result = await asyncio.wait_for(
            (prompt | llm).ainvoke(payload),
            timeout=timeout_s,
        )
        return parse_structured_output(raw_result, schema)

    try:
        result = await asyncio.wait_for(
            (prompt | llm.with_structured_output(schema)).ainvoke(payload),
            timeout=timeout_s,
        )
        if isinstance(result, schema):
            return result
        return schema.model_validate(result)
    except Exception as exc:
        if isinstance(exc, asyncio.CancelledError):
            raise

        if logger:
            logger.warning(
                f"{event_prefix}_structured_fallback",
                exc_type=type(exc).__name__,
                exc_msg=str(exc)[:220],
            )

        raw_result = await asyncio.wait_for(
            (prompt | llm).ainvoke(payload),
            timeout=timeout_s,
        )
        return parse_structured_output(raw_result, schema)

