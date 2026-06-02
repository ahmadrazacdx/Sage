"""
Tool registry for Sage.

Centralizes tool discovery and construction.  Agent nodes and the
LangGraph `ToolNode` use these factories to obtain tool lists.

Tools are categorized as:
  - Offline: Available without network (calculator, sandbox,
    export)
  - Online: Require network (arXiv, web, Wikipedia)

Usage:

    from sage.tools import get_offline_tools, get_online_tools, get_all_tools

    offline = get_offline_tools()
    online  = get_online_tools()
    all_t   = get_all_tools(online=True)
"""

from __future__ import annotations

import structlog
from langchain_core.tools import BaseTool

from sage.tools.calculator import calculator
from sage.tools.export import export_markdown, export_pdf
from sage.tools.sandbox import execute_python

log = structlog.get_logger(__name__)

__all__ = [
    "get_offline_tools",
    "get_online_tools",
    "get_all_tools",
    "OFFLINE_TOOL_NAMES",
    "ONLINE_TOOL_NAMES",
]

# --- Offline Tools ---
_OFFLINE_TOOLS: list[BaseTool] = [
    calculator,
    execute_python,
    export_markdown,
    export_pdf,
]

# --- Online Tools ---
_ONLINE_TOOL_MODULES: dict[str, tuple[str, str]] = {
    # logical_name: (module_path, attribute_name)
    "search_arxiv": ("sage.tools.search", "search_arxiv"),
    "search_web": ("sage.tools.search", "search_web"),
    "search_wikipedia": ("sage.tools.search", "search_wikipedia"),
}

# --- Metadata ---
OFFLINE_TOOL_NAMES: list[str] = [t.name for t in _OFFLINE_TOOLS]
ONLINE_TOOL_NAMES: list[str] = list(_ONLINE_TOOL_MODULES.keys())
OFFLINE_TOOL_COUNT: int = len(_OFFLINE_TOOLS)
ONLINE_TOOL_COUNT: int = len(_ONLINE_TOOL_MODULES)
TOTAL_TOOL_COUNT: int = OFFLINE_TOOL_COUNT + ONLINE_TOOL_COUNT


def _assert_tools(tools: list[BaseTool], source: str) -> list[BaseTool]:
    """Raise TypeError if any item is not a BaseTool instance.

    Guards against misconfigured tool constructors returning None or
    a plain function rather than a decorated tool object.
    """
    for i, t in enumerate(tools):
        if not isinstance(t, BaseTool):
            raise TypeError(
                f"[{source}] Item at index {i} is {type(t)!r}, expected BaseTool. Check the tool constructor."
            )
    return tools


def get_offline_tools() -> list[BaseTool]:
    """Return all tools that function without network access.

    Returns:
        List of offline tools: calculator, execute_python, export_markdown,export_pdf.
    """
    return _assert_tools(list(_OFFLINE_TOOLS), "get_offline_tools")


def get_online_tools() -> list[BaseTool]:
    """Return all tools that require network access.

    Each tool module is imported independently. If a single import
    fails (missing optional dependency, MCP not configured, etc.),
    that tool is skipped and a warning is logged — the remaining
    online tools are still returned.

    Returns:
        List of successfully loaded online tools. May be empty if
        all optional dependencies are absent.
    """
    tools: list[BaseTool] = []

    for logical_name, (module_path, attr) in _ONLINE_TOOL_MODULES.items():
        try:
            import importlib

            module = importlib.import_module(module_path)
            tool = getattr(module, attr)

            if not isinstance(tool, BaseTool):
                raise TypeError(f"Expected BaseTool, got {type(tool)!r} for {attr!r}")

            tools.append(tool)
            log.debug("online_tool_loaded", tool=logical_name)

        except ImportError as exc:
            log.warning(
                "online_tool_unavailable",
                tool=logical_name,
                reason="import_error",
                detail=str(exc),
            )
        except AttributeError as exc:
            log.error(
                "online_tool_misconfigured",
                tool=logical_name,
                reason="attribute_missing",
                detail=str(exc),
            )
        except TypeError as exc:
            log.error(
                "online_tool_misconfigured",
                tool=logical_name,
                reason="wrong_type",
                detail=str(exc),
            )

    return tools


def get_all_tools(*, online: bool = False) -> list[BaseTool]:
    """Return the full tool set based on network availability.

    Args:
        online: If True, include online tools alongside offline ones.
            Defaults to False (offline only).

    Returns:
        Combined list of offline tools (always) and any successfully
        loaded online tools (if ``online=True``).
    """
    tools = get_offline_tools()

    if online:
        online_tools = get_online_tools()
        if not online_tools:
            log.warning("online_tools_empty", hint="All online tool imports failed.")
        tools.extend(online_tools)

    log.debug(
        "tool_registry_built",
        total=len(tools),
        offline=len(get_offline_tools()),
        online=len(tools) - OFFLINE_TOOL_COUNT if online else 0,
        names=[t.name for t in tools],
    )

    return tools
