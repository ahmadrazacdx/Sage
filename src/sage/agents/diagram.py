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
import re
from datetime import datetime
from typing import Any

import structlog
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI

from sage.agents.state import AgentState
from sage.config import get_settings
from sage.prompts import (
    DIAGRAM_DESCRIPTION_PROMPT,
    DIAGRAM_FIX_PROMPT,
    DIAGRAM_MERMAID_PROMPT,
)
from sage.tools.export import reserve_export_path
from sage.tools.mermaid import render_mermaid_svg, validate_mermaid
from sage.utils import extract_fenced_block, strip_think_markers

log = structlog.get_logger(__name__)

_MERMAID_START_RE = re.compile(
    r"(?m)^\s*(?:flowchart|graph|sequenceDiagram|classDiagram|stateDiagram|erDiagram|mindmap|journey|timeline|gitGraph)\b"
)


def _format_knowledge_units(kus: list[dict]) -> str:
    if not kus:
        return "None available."
    return "\n".join(f"[{ku.get('id', 'KU?')}] {ku.get('claim', ku.get('content', ''))}" for ku in kus)


def _to_str(result: Any) -> str:
    """Unwrap AIMessage or coerce to str."""
    return result.content if isinstance(result, AIMessage) else str(result)


def _strip_fences(text: str) -> str:
    """Extract clean Mermaid code from potentially noisy model output."""
    cleaned = strip_think_markers(text).strip()

    fenced = extract_fenced_block(cleaned, preferred_languages={"mermaid"})
    if fenced is None:
        fenced = extract_fenced_block(cleaned)
    if fenced:
        cleaned = fenced

    match = _MERMAID_START_RE.search(cleaned)
    if match is not None:
        cleaned = cleaned[match.start() :]

    return cleaned.strip()


def _slugify_query(query: str) -> str:
    """Derive a filesystem-safe short slug from the user query."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", (query or "").strip().lower()).strip("_")
    return (slug or "diagram")[:48]


def _export_svg_artifact(svg_data: str, query: str) -> dict[str, str] | None:
    """Persist diagram SVG to exports and return artifact metadata."""
    if not svg_data.strip():
        return None

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = f"diagram_{_slugify_query(query)}_{timestamp}"

    try:
        output_path = reserve_export_path(stem, ".svg")
        output_path.write_text(svg_data, encoding="utf-8")
        return {
            "kind": "svg",
            "filename": output_path.name,
            "path": str(output_path),
        }
    except Exception as exc:  # noqa: BLE001
        log.warning("diagram_svg_export_failed", exc=str(exc)[:200])
        return None


def _build_response(mermaid_code: str, svg_data: str, export_filename: str | None = None) -> str:
    """Compose the final markdown response string."""
    parts: list[str] = []
    if svg_data:
        caption = "Here's your diagram's mermaid source code." if export_filename else "Diagram rendered successfully."
        parts.append(caption)
    else:
        parts.append("*SVG render/export unavailable. Mermaid source is shown below.*")

    parts.append(f"\n\n```text\n{mermaid_code}\n```")
    return "\n".join(parts)


async def diagram_node(state: AgentState, llm: ChatOpenAI, *, util_llm: ChatOpenAI | None = None) -> dict[str, Any]:
    """Generate, validate, and render a high-quality Mermaid diagram.

    Phase 1: Build a structured description (nodes / edges / phases).
    Phase 2: Convert to mmdr-safe Mermaid with palette.
    Phase 3: Validate; feed errors back for correction up to max_retries.
    Phase 4: Render SVG via mmdr for export.
    Args:
        util_llm: Optional smaller LLM for description and fix steps (CPU-only offload).

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
    _desc_llm = util_llm or llm

    _no_think = {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}
    llm = llm.bind(**_no_think)
    _desc_llm = _desc_llm.bind(**_no_think)

    # Structured description
    desc_prompt = ChatPromptTemplate.from_messages(
        [
            ("human", DIAGRAM_DESCRIPTION_PROMPT),
        ]
    )
    try:
        desc_result = await asyncio.wait_for(
            (desc_prompt | _desc_llm).ainvoke({"query": query, "knowledge_units": ku_text}),
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
                "I was unable to analyse the diagram requirements. Please try again with a more specific request."
            )
        }

    # Mermaid code generation
    try:
        mermaid_result = await asyncio.wait_for(
            llm.ainvoke(
                [
                    SystemMessage(content=DIAGRAM_MERMAID_PROMPT),
                    HumanMessage(content=description),
                ]
            ),
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
                _desc_llm.ainvoke(
                    [
                        SystemMessage(content=DIAGRAM_FIX_PROMPT),
                        HumanMessage(content=f"mermaid_code:\n{mermaid_code}\n\nerrors:\n{error_text}"),
                    ]
                ),
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

    artifact_paths: list[dict[str, str]] = []
    if svg_data:
        artifact = _export_svg_artifact(svg_data, query)
        if artifact:
            artifact_paths.append(artifact)

    response_text = _build_response(
        mermaid_code,
        svg_data,
        artifact_paths[0]["filename"] if artifact_paths else None,
    )

    return {
        "messages": [AIMessage(content=response_text)],
        "response": response_text,
        "diagrams": [{"mermaid_code": mermaid_code, "svg_data": svg_data}],
        "artifact_paths": artifact_paths,
        "tool_calls": [{"tool": "diagram_generation", "validated": validated}],
    }
