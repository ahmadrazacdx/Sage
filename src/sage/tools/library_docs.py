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
from typing import TYPE_CHECKING, Any, Literal

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

def _response(
    *,
    success: bool,
    data: str | None,
    error: str | None,
    source: Literal["context7", "cache", "fallback", "error"],
) -> dict[str, Any]:
    return {
        "success": success,
        "data": data,
        "error": error,
        "source": source,
    }


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
            raise RuntimeError("CONTEXT7_API_KEY missing")

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
        raise RuntimeError(f"Missing required tool: {name}")
    return match


@tool
async def search_library_docs(library: str, query: str) -> dict[str, Any]:
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
        return _response(
            success=False,
            data=None,
            error="Missing library name",
            source="error",
        )
    if not clean_query:
        return _response(
            success=False,
            data=None,
            error="Missing query",
            source="error",
        )

    try:
        tools = await _get_tools()

        library_id = LIB_CACHE.get(clean_lib)

        source: Literal["context7", "cache", "fallback"] = "context7"

        if not library_id:
            resolve_tool   = _find_tool(tools, _TOOL_RESOLVE)
            resolve_result = await asyncio.wait_for(
                resolve_tool.ainvoke({"query": clean_query, "libraryName": clean_lib}),
                timeout=_RESOLVE_TIMEOUT_S,
            )

            if not resolve_result:
                return _response(
                    success=False,
                    data=None,
                    error=f"No library found: {clean_lib}",
                    source="fallback",
                )

            resolve_text = str(resolve_result)

            match = re.search(r"(/[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+)", resolve_text)

            if not match:
                return _response(
                    success=False,
                    data=None,
                    error="Failed to parse library ID",
                    source="error",
                )

            library_id = match.group(1)
            log.info("context7_resolved", library=clean_lib, id=library_id)
        else:
            source = "cache"
            log.info("context7_cache_hit", library=clean_lib, id=library_id)

        # Fetch docs
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
            return _response(
                success=False,
                data=None,
                error="No documentation found",
                source="fallback",
            )

        result_str = str(docs_result)
        log.info(
            "context7_success",
            library=clean_lib,
            query=clean_query[:80],
            result_length=len(result_str),
        )
        return _response(
            success=True,
            data=result_str[:8000],
            error=None,
            source=source,
        )

    except (TimeoutError, asyncio.TimeoutError):
        log.warning("library_docs_timeout", library=clean_lib)
        return _response(
            success=False,
            data=None,
            error="Context7 timeout",
            source="fallback",
        )

    except RuntimeError as exc:
        log.error("library_docs_setup_failed", error=str(exc))
        return _response(
            success=False,
            data=None,
            error=str(exc),
            source="error",
        )
    except Exception as exc:  # noqa: BLE001
        log.error(
            "library_docs_failed",
            library=clean_lib,
            exc_type=type(exc).__name__,
            error=str(exc)[:200],
        )
        return _response(
            success=False,
            data=None,
            error=f"{type(exc).__name__}: {str(exc)[:200]}",
            source="error",
        )