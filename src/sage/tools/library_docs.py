"""
Context7 MCP wrapper for programming library documentation lookup.

Provides a LangChain tool that fetches up-to-date library documentation
via the Context7 MCP server.  Context7 indexes official docs for
hundreds of programming libraries (Flask, Django, pandas, NumPy,
React, etc.) and delivers focused, version-specific code examples.

Transport: SSE (HTTP streaming) — connects to the public Context7
endpoint.

Graceful degradation:
  - If `langchain-mcp-adapters` is not installed -> returns error.
  - If the MCP connection fails -> returns warning with fallback advice.
  - If Context7's hosted backend hangs -> returns fallback advice immediately.

This tool is only available when `state.online_mode == True`.
"""

from __future__ import annotations

import os
import asyncio
import re
from typing import TYPE_CHECKING, Any

import structlog
from langchain_core.tools import tool

if TYPE_CHECKING:
    from langchain_mcp_adapters.client import MultiServerMCPClient

log = structlog.get_logger(__name__)

_CONTEXT7_HTTP_URL: str = os.getenv("CONTEXT7_HTTP_URL", "https://mcp.context7.com/mcp")
_CONTEXT7_API_KEY: str = os.getenv("CONTEXT7_API_KEY", "").strip()
_TOOLS_TIMEOUT_S: int  = 10
_RESOLVE_TIMEOUT_S: int = 10
_QUERY_TIMEOUT_S: int  = 20
_MAX_QUERY_LENGTH: int = 300
_MAX_TOKENS: int       = 5000

_TOOL_RESOLVE = "resolve-library-id"
_TOOL_DOCS    = "query-docs"

LIB_CACHE: dict[str, str] = {
    "flask": "/pallets/flask",
    "django": "/django/django",
    "pandas": "/pandas-dev/pandas",
    "numpy": "/numpy/numpy",
    "fastapi": "/tiangolo/fastapi",
    "react": "/facebook/react",
}

_cached_tools: list[Any] | None = None
_tools_lock = asyncio.Lock()


def _sanitize_input(text: str) -> str:
    clean = re.sub(r"[\x00-\x1f\x7f]", "", text).strip()
    return clean[:_MAX_QUERY_LENGTH]


async def _get_tools() -> list[Any]:
    global _cached_tools

    if _cached_tools is not None:
        return _cached_tools

    async with _tools_lock:
        if _cached_tools is not None:
            return _cached_tools

        try:
            from langchain_mcp_adapters.client import MultiServerMCPClient
        except ImportError:
            raise RuntimeError(
                "install langchain-mcp-adapters: uv add langchain-mcp-adapters"
            )

        if not _CONTEXT7_API_KEY:
            raise RuntimeError("CONTEXT7_API_KEY environment variable is missing or empty")

        client = MultiServerMCPClient(
            {
                "context7": {
                    "url": _CONTEXT7_HTTP_URL,
                    "transport": "http",
                    "headers": {
                        "CONTEXT7_API_KEY": _CONTEXT7_API_KEY,
                        "Authorization": f"Bearer {_CONTEXT7_API_KEY}",
                    },
                },
            }
        )

        tools = await asyncio.wait_for(
            client.get_tools(),
            timeout=_TOOLS_TIMEOUT_S,
        )
        _cached_tools = tools
        log.info("context7_tools_cached", count=len(tools))
        return tools


def _find_tool(tools: list[Any], name: str) -> Any:
    match = next((t for t in tools if t.name == name), None)
    if match is None:
        available = [t.name for t in tools]
        raise RuntimeError(
            f"Expected tool '{name}' not found in Context7. "
            f"Available: {available}"
        )
    return match


def _extract_resource_uri(resolve_output: str) -> str | None:
    """Extract context7:// URI from resolve-library-uri output."""
    match = re.search(r"context7://\S+", resolve_output)
    return match.group(0) if match else None


@tool
async def search_library_docs(library: str, query: str) -> str:
    """Fetch up-to-date programming library documentation via Context7 MCP.

    Searches the Context7 documentation index for the specified library
    and returns relevant code examples, API references, and usage
    patterns.  Useful for debugging framework-specific issues and
    finding current API documentation.

    Args:
        library: Name of the programming library (e.g. 'flask').
        query:   Specific documentation topic (e.g. 'request.get_json handling None').

    Returns:
        Relevant documentation excerpts, or an error/fallback message.
    """
    clean_lib   = _sanitize_input(library).lower()
    clean_query = _sanitize_input(query)

    if not clean_lib:
        return "Error: No library name provided"
    if not clean_query:
        return "Error: No query provided"

    try:
        tools = await _get_tools()

        library_id = LIB_CACHE.get(clean_lib)

        if not library_id:
            # Step 1 — resolve library name -> Context7 Library ID
            resolve_tool   = _find_tool(tools, _TOOL_RESOLVE)
            resolve_result = await asyncio.wait_for(
                resolve_tool.ainvoke({"query": clean_query, "libraryName": clean_lib}),
                timeout=_RESOLVE_TIMEOUT_S,
            )

            if not resolve_result:
                return f"No library found matching '{clean_lib}' in Context7"

            resolve_text = str(resolve_result)

            # Strict extraction: only valid path segments
            match = re.search(r"(/[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+)", resolve_text)

            if not match:
                return (
                    f"Could not extract valid library ID for '{clean_lib}'. "
                    f"Context7 returned: {resolve_text[:300]}"
                )

            library_id = match.group(1)
            log.info("context7_resolved", library=clean_lib, id=library_id)
        else:
            log.info("context7_cache_hit", library=clean_lib, id=library_id)

        # Step 2 — fetch docs using the libraryId + correct parameter name
        docs_tool   = _find_tool(tools, _TOOL_DOCS)
        docs_result = await asyncio.wait_for(
            docs_tool.ainvoke(
                {
                    "libraryId": library_id,
                    "query":     clean_query,
                }
            ),
            timeout=_QUERY_TIMEOUT_S,
        )

        if not docs_result:
            return f"No documentation found for '{clean_lib}' on topic '{clean_query}'"

        result_str = str(docs_result)
        log.info(
            "library_docs_complete",
            library=clean_lib,
            query=clean_query[:80],
            result_length=len(result_str),
        )
        return result_str[:8000]

    except (TimeoutError, asyncio.TimeoutError):
        log.warning("library_docs_timeout", library=clean_lib)
        return (
            f"Error: Library docs lookup timed out because the Context7 server is unresponsive. "
            f"Please use standard web search tools to search for '{clean_lib} {clean_query}' instead."
        )
    except ExceptionGroup as eg:
        log.warning("library_docs_exception_group", library=clean_lib, errors=[str(e) for e in eg.exceptions])
        return (
            f"Error: Library docs lookup failed (server timeout/error). "
            f"Please use standard web search tools to search for '{clean_lib} {clean_query}' instead."
        )
    except RuntimeError as exc:
        log.error("library_docs_setup_failed", error=str(exc))
        return f"Error: {exc}"
    except Exception as exc:  # noqa: BLE001
        log.error(
            "library_docs_failed",
            library=clean_lib,
            exc_type=type(exc).__name__,
            error=str(exc)[:200],
        )
        return (
            f"Error: Library docs lookup failed ({type(exc).__name__}). "
            f"Please use standard web search tools to search for '{clean_lib}' documentation instead."
        )