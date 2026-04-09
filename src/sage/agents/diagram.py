"""
Diagram agent node for Sage.

Three-step pipeline:
  1. Description: LLM produces structured intermediate (nodes, edges, types).
  2. Mermaid generation: LLM converts description to mmdr-compatible Mermaid.
  3. Validation loop: validate_mermaid checks syntax, DIAGRAM_FIX_PROMPT
     feeds errors back. Falls back to raw code.

SVG rendering is attempted via `render_mermaid_svg` tool for export.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from sage.agents.state import AgentState
from sage.config import get_settings
from sage.prompts import (
    DIAGRAM_DESCRIPTION_PROMPT,
    DIAGRAM_FIX_PROMPT,
    DIAGRAM_MERMAID_PROMPT,
    SYSTEM_PROMPT,
)
from sage.tools.mermaid import render_mermaid_svg, validate_mermaid

log = structlog.get_logger(__name__)


def _format_knowledge_units(kus: list[dict]) -> str:
    if not kus:
        return "None available."
    return "\n".join(
        f"[{ku.get('id', 'KU?')}] {ku.get('claim', ku.get('content', ''))}"
        for ku in kus
    )
 
def _to_str(result: Any) -> str:
    """Unwrap AIMessage or coerce to str."""
    return result.content if isinstance(result, AIMessage) else str(result)
 


def _strip_fences(text: str) -> str:
    """Remove markdown code fences the LLM may wrap around Mermaid output."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text

def _build_response(mermaid_code: str, svg_data: str) -> str:
    """Compose the final markdown response string."""
    parts = [f"```mermaid\n{mermaid_code}\n```"]
    if not svg_data:
        parts.append(
            "\n*SVG export unavailable — "
            "the diagram is displayed above via Mermaid rendering.*"
        )
    return "\n".join(parts)

async def diagram_node(state: AgentState, llm: ChatOpenAI) -> dict[str, Any]:
    """Generate, validate, and render a high-quality Mermaid diagram.
 
    Phase 1: Build a structured description (nodes / edges / phases).
    Phase 2: Convert to mmdr-safe Mermaid with palette.
    Phase 3: Validate; feed errors back for correction up to max_retries.
    Phase 4: Render SVG via mmdr for export.
 
    Returns:
        response   : Mermaid fenced block (Gradio renders natively).
        diagrams   : [{"mermaid_code": str, "svg_data": str}]
        tool_calls : [{"tool": "diagram_generation", "validated": bool}]
    """
    cfg = get_settings().agent
    query: str = state.get("query", "")
    kus: list[dict] = state.get("knowledge_units", [])
    ku_text = _format_knowledge_units(kus)
    timeout: float = cfg.llm_timeout
    max_retries: int = cfg.diagram_max_retries
 
    # Structured description
    desc_prompt = ChatPromptTemplate.from_messages([
        ("system", SYSTEM_PROMPT),
        ("human", DIAGRAM_DESCRIPTION_PROMPT),
    ])
    try:
        desc_result = await asyncio.wait_for(
            (desc_prompt | llm).ainvoke({"query": query, "knowledge_units": ku_text}),
            timeout=timeout,
        )
        description = _to_str(desc_result)
        log.info("diagram_description_complete", desc_len=len(description))
    except asyncio.CancelledError:
        log.error("diagram_description_cancelled")
        raise
    except Exception as exc:
        log.error("diagram_description_failed", exc_type=type(exc).__name__, exc=str(exc)[:300])
        return {
            "response": (
                "I was unable to analyse the diagram requirements. "
                "Please try again with a more specific request."
            )
        }
 
    # Mermaid code generation
    try:
        mermaid_result = await asyncio.wait_for(
            llm.ainvoke([
                SystemMessage(content=DIAGRAM_MERMAID_PROMPT),
                HumanMessage(content=description),
            ]),
            timeout=timeout,
        )
        mermaid_code = _strip_fences(_to_str(mermaid_result))
        log.info("diagram_mermaid_generated", code_len=len(mermaid_code), lines=mermaid_code.count("\n"))
    except asyncio.CancelledError:
        log.error("diagram_mermaid_cancelled")
        raise
    except Exception as exc:
        log.error("diagram_mermaid_gen_failed", exc_type=type(exc).__name__, exc=str(exc)[:300])
        return {"response": "I was unable to generate the diagram code. Please try again."}
 
    # Validation + fix loop
    validated = False
    for attempt in range(1, max_retries + 1):
        try:
            validation = await validate_mermaid.ainvoke({"code": mermaid_code})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("diagram_validate_tool_failed", exc_type=type(exc).__name__, exc=str(exc)[:200])
            break
 
        if validation.get("valid", False):
            validated = True
            log.info("diagram_valid", attempt=attempt)
            break
 
        errors: list[str] = validation.get("errors", ["Unknown validation error"])
        error_text = "\n".join(errors)
        log.warning("diagram_validation_failed", attempt=attempt, errors=error_text[:400])
 
        if attempt >= max_retries:
            log.error("diagram_fix_exhausted", max_retries=max_retries)
            break
        try:
            fix_result = await asyncio.wait_for(
                llm.ainvoke([
                    SystemMessage(content=DIAGRAM_FIX_PROMPT),
                    HumanMessage(content=f"mermaid_code:\n{mermaid_code}\n\nerrors:\n{error_text}"),
                ]),
                timeout=timeout,
            )
            mermaid_code = _strip_fences(_to_str(fix_result))
            log.info("diagram_fix_applied", attempt=attempt, new_len=len(mermaid_code))
        except asyncio.CancelledError:
            log.error("diagram_fix_cancelled")
            raise
        except Exception as exc:
            log.warning("diagram_fix_failed", attempt=attempt, exc=str(exc)[:200])
            break
 
    # Render SVG
    svg_data: str = ""
    if not validated:
        log.warning("diagram_returning_unvalidated", mermaid_lines=mermaid_code.count("\n"))
    try:
        render_result = await asyncio.wait_for(
            render_mermaid_svg.ainvoke({"code": mermaid_code}),
            timeout=timeout,
        )
        if render_result.get("success", False):
            svg_data = render_result.get("svg", "")
            log.info(
                "diagram_svg_rendered",
                svg_len=len(svg_data),
                exec_ms=render_result.get("meta", {}).get("exec_time_ms"),
            )
        else:
            log.warning(
                "diagram_svg_render_unsuccessful",
                error=render_result.get("error", "")[:200],
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("diagram_svg_render_failed", exc=str(exc)[:200])
 
    return {
        "response": _build_response(mermaid_code, svg_data),
        "diagrams": [{"mermaid_code": mermaid_code, "svg_data": svg_data}],
        "tool_calls": [{"tool": "diagram_generation", "validated": validated}],
    }