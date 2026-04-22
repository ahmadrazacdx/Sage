"""
Online search tools for Sage.

Provides three LangChain tools for external information retrieval:
  1. `search_arxiv` — academic paper search via `ArxivRetriever`
  2. `search_web` — general web search via `DuckDuckGoSearchRun`
  3. `search_wikipedia` — encyclopedia lookup via `WikipediaRetriever`

All tools enforce timeouts from `SearchSettings` via
asyncio.wait_for and degrade gracefully on failure.

These tools are only available when `state.online_mode == True`.
"""

from __future__ import annotations

import asyncio
import re
from typing import Dict, Any, List, Optional
import warnings

import structlog
from langchain_core.tools import tool

from sage.config import get_settings
try:
    from bs4 import GuessedAtParserWarning
except ImportError:
    GuessedAtParserWarning = None  # type: ignore[assignment]


log = structlog.get_logger(__name__)
if GuessedAtParserWarning is not None:
    warnings.filterwarnings("ignore", category=GuessedAtParserWarning)

# --- Constants ---
_MAX_QUERY_LENGTH: int = 500
_SEMAPHORE = asyncio.Semaphore(5)


def _sanitize_query(query: str) -> str:
    """Strip control characters and enforce length limit."""
    if not isinstance(query, str):
        return ""
    clean = re.sub(r"[\x00-\x1f\x7f]", "", query).strip()
    return clean[:_MAX_QUERY_LENGTH]


def _error(msg: str) -> Dict[str, Any]:
    return {
        "query": "",
        "source": "",
        "results": [],
        "error": msg,
    }


def _success(query: str, source: str, results: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "query": query,
        "source": source,
        "results": results,
        "error": None,
    }


# --- arXiv ---
@tool
async def search_arxiv(query: str) -> Dict[str, Any]:
    """Search arXiv for academic papers relevant to the query.

    Returns paper titles, abstracts, and arXiv IDs.  Useful for
    research on cutting-edge topics in computer science, mathematics,
    and physics.

    Args:
        query: Research topic or keywords to search for.

    Returns:
        Formatted search results, or an error/empty-result message.
    """
    cfg = get_settings().tools.search

    if not isinstance(query, str):
        return _error("Query must be a string")

    clean_query = _sanitize_query(query)
    if not clean_query:
        return _error("Empty search query")

    try:
        from langchain_community.retrievers import ArxivRetriever

        retriever = ArxivRetriever(
            top_k_results=cfg.max_results,
            load_max_docs=cfg.max_results,
        )

        async with _SEMAPHORE:
            docs = await asyncio.wait_for(
                asyncio.to_thread(retriever.invoke, clean_query),
                timeout=cfg.arxiv_timeout,
            )

        if not docs:
            return _error(f"No arXiv papers found for: '{clean_query}'")

        log.info("search_arxiv_complete", query=clean_query[:80], results=len(docs))

        results = [
            {
                "title": doc.metadata.get("Title") or "Untitled",
                "id": doc.metadata.get("Entry ID") or "",
                "content": doc.page_content[:500],
            }
            for doc in docs
        ]

        return _success(clean_query, "arxiv", results)

    except asyncio.TimeoutError:
        return _error(f"arXiv search timed out after {cfg.arxiv_timeout}s")
    except ImportError:
        return _error("Install 'arxiv' package")
    except Exception as exc:
        return _error(str(exc)[:200])


# --- Web (DuckDuckGo) ---
@tool
async def search_web(query: str) -> Dict[str, Any]:
    """Search the web using DuckDuckGo for general information.

    Returns relevant web page snippets.  Useful for finding
    documentation, tutorials, blog posts, and current information.

    Args:
        query: Search query string.

    Returns:
        Formatted search results, or an error message.
    """
    cfg = get_settings().tools.search

    if not isinstance(query, str):
        return _error("Query must be a string")

    clean_query = _sanitize_query(query)
    if not clean_query:
        return _error("Empty search query")

    try:
        from langchain_community.tools import DuckDuckGoSearchResults
        from langchain_community.utilities import DuckDuckGoSearchAPIWrapper

        wrapper = DuckDuckGoSearchAPIWrapper(
            max_results=cfg.max_results,
        )
        search = DuckDuckGoSearchResults(api_wrapper=wrapper)

        async with _SEMAPHORE:
            result: str = await asyncio.wait_for(
                asyncio.to_thread(search.invoke, clean_query),
                timeout=cfg.web_timeout,
            )

        if not result:
            return _error(f"No web results found for: '{clean_query}'")

        log.info("search_web_complete", query=clean_query[:80])

        # Normalize raw string into structured format
        results = [{"content": result}]

        return _success(clean_query, "web", results)

    except asyncio.TimeoutError:
        return _error(f"Web search timed out after {cfg.web_timeout}s")
    except ImportError:
        return _error("Install 'duckduckgo-search' package")
    except Exception as exc:
        return _error(str(exc)[:200])


# --- Wikipedia ---
@tool
async def search_wikipedia(query: str) -> Dict[str, Any]:
    """Search Wikipedia for background information on a topic.

    Returns article summaries useful for research context and
    introductory material.

    Args:
        query: Topic or concept to look up.

    Returns:
        Wikipedia article summaries, or an error message.
    """
    cfg = get_settings().tools.search

    if not isinstance(query, str):
        return _error("Query must be a string")

    clean_query = _sanitize_query(query)
    if not clean_query:
        return _error("Empty search query")

    try:
        from langchain_community.retrievers import WikipediaRetriever

        retriever = WikipediaRetriever(
            top_k_results=min(cfg.max_results, 3),
            load_max_docs=min(cfg.max_results, 3),
        )

        async with _SEMAPHORE:
            docs = await asyncio.wait_for(
                asyncio.to_thread(retriever.invoke, clean_query),
                timeout=cfg.wiki_timeout,
            )

        if not docs:
            return _error(f"No Wikipedia articles found for: '{clean_query}'")

        log.info(
            "search_wikipedia_complete",
            query=clean_query[:80],
            results=len(docs),
        )

        results = [
            {
                "title": doc.metadata.get("title") or doc.metadata.get("Title") or "Untitled",
                "content": doc.page_content[:800],
            }
            for doc in docs
        ]

        return _success(clean_query, "wikipedia", results)

    except asyncio.TimeoutError:
        return _error(f"Wikipedia search timed out after {cfg.wiki_timeout}s")
    except ImportError:
        return _error("Install 'wikipedia' package")
    except Exception as exc:
        return _error(str(exc)[:200])