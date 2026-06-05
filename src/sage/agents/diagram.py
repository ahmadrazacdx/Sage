"""
Diagram agent node for Sage.

Two-step pipeline:
  1. Description: LLM produces structured JSON (nodes, edges, types, phases).
  2. Mermaid generation: LLM converts description to bare Mermaid structure.
  3. Style injection: Python post-processor adds classDef, class, and linkStyle.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import defaultdict
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
from sage.utils import extract_fenced_block, strip_think_markers

log = structlog.get_logger(__name__)

_MERMAID_START_RE = re.compile(
    r"(?m)^\s*(?:mermaid\s+)?(?P<type>flowchart|graph|sequenceDiagram|classDiagram|stateDiagram|erDiagram|mindmap|journey|timeline|gitGraph)\b",
    re.IGNORECASE
)

_INIT_BLOCK_RE = re.compile(r"%%\{init:.*?\}%%\s*\n?", re.DOTALL)
_EDGE_LINE_RE = re.compile(r"""(?x)
    (?P<dashed> -\.+-?>)
  | (?P<solid>  --+>)
  | (?P<amp>    \s&\s)
""")
_SUBGRAPH_DEF_RE = re.compile(r"^\s*subgraph\s+([^\s\[\"']+|\"[^\"]+\"|'[^']+')")

_CLASS_DEFS: list[str] = [
    "    classDef primary   fill:#3b82f6,stroke:#2563eb,stroke-width:1.5px,color:#ffffff,rx:8,ry:8",
    "    classDef secondary fill:#8b5cf6,stroke:#7c3aed,stroke-width:1.5px,color:#ffffff,rx:8,ry:8",
    "    classDef accent    fill:#10b981,stroke:#059669,stroke-width:1.5px,color:#ffffff,rx:8,ry:8",
    "    classDef warning   fill:#f59e0b,stroke:#d97706,stroke-width:1.5px,color:#ffffff,rx:8,ry:8",
    "    classDef neutral   fill:#64748b,stroke:#475569,stroke-width:1px,color:#ffffff,rx:8,ry:8",
    "    classDef highlight fill:#ec4899,stroke:#db2777,stroke-width:1.5px,color:#ffffff,rx:8,ry:8",
]

_TYPE_TO_CLASS: dict[str, str] = {
    "process": "primary",
    "decision": "warning",
    "terminal": "accent",
    "data": "highlight",
    "actor": "secondary",
    "entity": "neutral",
}

_PRIMARY_LINK_STYLE = "stroke:#3b82f6,stroke-width:2.5px"
_DASHED_LINK_STYLE = "stroke:#a78bfa,stroke-dasharray:5 5,stroke-width:1.5px"

_SUBGRAPH_STYLES = [
    "fill:#1e40af15,stroke:#1e40af,stroke-width:3px",
    "fill:#5b21b615,stroke:#5b21b6,stroke-width:3px",
    "fill:#065f4615,stroke:#065f46,stroke-width:3px",
    "fill:#92400e15,stroke:#92400e,stroke-width:3px",
    "fill:#9d174d15,stroke:#9d174d,stroke-width:3px",
    "fill:#1e293b15,stroke:#1e293b,stroke-width:3px",
]

_PHASE1_BUDGET_FRACTION: float = 0.60


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
        cleaned = cleaned[match.start("type") :]

    cleaned = _INIT_BLOCK_RE.sub("", cleaned)
    cleaned = _deduplicate_mermaid(cleaned)
    cleaned = re.sub(r'([A-Za-z0-9_]+)\["?\1\["?(.*?)"?\]"?\]', r'\1["\2"]', cleaned)

    return cleaned.strip()


def _deduplicate_mermaid(code: str) -> str:
    """Remove repeated diagram body that the LLM sometimes emits."""
    seen: set[str] = set()
    lines = code.split("\n")
    cut_at: int | None = None
    for i, line in enumerate(lines):
        m = re.match(r"^\s*subgraph\s+(\S+)", line)
        if m:
            sg_id = m.group(1).strip('"').strip("'")
            if sg_id in seen:
                cut_at = i
                break
            seen.add(sg_id)
    if cut_at is not None:
        return "\n".join(lines[:cut_at]).rstrip()
    return code


def _is_valid_mermaid(code: str) -> tuple[bool, str]:
    """Sanity check: valid declaration, balanced subgraphs, and has edges."""
    if not _MERMAID_START_RE.match(code.strip()):
        return False, "Does not start with a valid diagram type declaration."

    subgraph_count = len(re.findall(r"^\s*subgraph\b", code, re.MULTILINE))
    end_count = len(re.findall(r"^\s*end\b", code, re.MULTILINE))
    if subgraph_count != end_count:
        return False, f"Unbalanced subgraphs: found {subgraph_count} 'subgraph' blocks but {end_count} 'end' tags."

    edge_re = re.compile(r"(--.*?-->|--.*?---|==.*?==>|-\..*?\.->|-->|---|==>|===|-\.->|-\.-)")
    if not edge_re.search(code):
        return False, "Diagram contains no edges/connections."

    return True, ""


def _parse_description_json(raw: str) -> dict | None:
    """Best-effort parse of the structured description JSON from the LLM."""
    cleaned = strip_think_markers(raw).strip()
    fenced = extract_fenced_block(cleaned, preferred_languages={"json"})
    if fenced:
        cleaned = fenced
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(cleaned[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return None


def _inject_mermaid_styling(bare_code: str, description: dict | None) -> str:
    """Inject premium classDef, class assignments, and linkStyle into bare Mermaid."""
    lines = bare_code.strip().split("\n")
    if not lines:
        return bare_code
    structural_lines: list[str] = []
    for line in lines:
        stripped = line.strip().lower()
        if (
            stripped.startswith("classdef ")
            or stripped.startswith("class ")
            or stripped.startswith("linkstyle ")
            or stripped.startswith("style ")
        ):
            continue
        cleaned_line = re.sub(r":::[A-Za-z0-9_-]+", "", line)
        structural_lines.append(cleaned_line)

    result_lines: list[str] = [structural_lines[0]]
    result_lines.extend(_CLASS_DEFS)
    result_lines.extend(structural_lines[1:])
    subgraph_ids: list[str] = []
    for line in structural_lines:
        m = _SUBGRAPH_DEF_RE.match(line)
        if m:
            sg_id = m.group(1).strip()
            subgraph_ids.append(sg_id)

    for i, sg_id in enumerate(subgraph_ids):
        style = _SUBGRAPH_STYLES[i % len(_SUBGRAPH_STYLES)]
        result_lines.append(f"    style {sg_id} {style}")

    unique_nodes: list[str] = []
    if description and "nodes" in description:
        for node in description["nodes"]:
            node_id = node.get("id", "")
            if node_id in _MERMAID_RESERVED_IDS:
                node_id = f"{node_id}_n"
            if node_id and node_id not in unique_nodes:
                unique_nodes.append(node_id)

    node_id_re = re.compile(r"^\s+(\w+)\s*[\[\({]")
    for line in structural_lines[1:]:
        m = node_id_re.match(line)
        if m:
            node_id = m.group(1)
            if (
                node_id.lower() not in ("subgraph", "end", "direction", "classdef", "class", "linkstyle", "style")
                and node_id not in unique_nodes
            ):
                unique_nodes.append(node_id)

    class_groups: dict[str, list[str]] = defaultdict(list)
    _AVAILABLE_CLASSES = ["primary", "secondary", "accent", "warning", "neutral", "highlight"]

    for i, node_id in enumerate(unique_nodes):
        class_name = _AVAILABLE_CLASSES[i % len(_AVAILABLE_CLASSES)]
        class_groups[class_name].append(node_id)

    for class_name, node_ids in class_groups.items():
        if node_ids:
            result_lines.append(f"    class {','.join(node_ids)} {class_name}")

    solid_indices: list[int] = []
    dashed_indices: list[int] = []
    edge_idx = 0

    _EDGE_ARROW_RE = re.compile(r"(-\.+-?>|--+>)")

    for line in structural_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("subgraph") or stripped.startswith("end"):
            continue

        arrows = _EDGE_ARROW_RE.findall(stripped)
        if not arrows:
            continue

        for arrow in arrows:
            if "-." in arrow:
                dashed_indices.append(edge_idx)
            else:
                solid_indices.append(edge_idx)
            edge_idx += 1

    if solid_indices:
        result_lines.append(f"    linkStyle {','.join(str(i) for i in solid_indices)} {_PRIMARY_LINK_STYLE}")
    if dashed_indices:
        result_lines.append(f"    linkStyle {','.join(str(i) for i in dashed_indices)} {_DASHED_LINK_STYLE}")

    return "\n".join(result_lines)


_MERMAID_RESERVED_IDS: frozenset[str] = frozenset(
    {
        "end",
        "graph",
        "subgraph",
        "classDef",
        "class",
        "style",
        "linkStyle",
        "direction",
        "flowchart",
        "click",
    }
)


def _sanitize_mermaid_ids(code: str) -> str:
    """Rename node IDs that collide with Mermaid reserved keywords."""
    reserved_used: set[str] = set()
    for line in code.split("\n"):
        for kw in _MERMAID_RESERVED_IDS:
            if re.search(rf"\b{kw}\s*[\[\({{]", line):
                reserved_used.add(kw)

    if not reserved_used:
        return code

    rename = {kw: f"{kw}_n" for kw in reserved_used}

    result: list[str] = []
    for line in code.split("\n"):
        if line.strip() == "end":
            result.append(line)
            continue
        for old, new in rename.items():
            line = re.sub(rf"\b{old}\b(?=\s*[\[\({{])", new, line)
            line = re.sub(rf"((?:-->|-\.->|==>)\s*)\b{old}\b", rf"\g<1>{new}", line)
            line = re.sub(rf"^(\s*)\b{old}\b(?=\s)", rf"\g<1>{new}", line)
        result.append(line)
    return "\n".join(result)


def _trim_description_for_mermaid(desc: dict) -> dict:
    """Cap node/edge count so Mermaid generation receives a bounded input."""
    _MAX_NODES_TO_MERMAID = 17
    nodes = desc.get("nodes", [])
    if len(nodes) <= _MAX_NODES_TO_MERMAID:
        return desc
    kept = nodes[:_MAX_NODES_TO_MERMAID]
    kept_ids = {n.get("id") for n in kept}
    trimmed_edges = [e for e in desc.get("edges", []) if e.get("from") in kept_ids and e.get("to") in kept_ids]
    log.debug(
        "description_trimmed_for_mermaid",
        original_nodes=len(nodes),
        kept_nodes=len(kept),
        original_edges=len(desc.get("edges", [])),
        kept_edges=len(trimmed_edges),
    )
    return {**desc, "nodes": kept, "edges": trimmed_edges}


def _build_response(mermaid_code: str) -> str:
    """Compose the final markdown response with a mermaid fenced block."""
    return f"Here is your diagram:\n\n```mermaid\n{mermaid_code}\n```"


async def diagram_node(state: AgentState, llm: ChatOpenAI, *, util_llm: ChatOpenAI | None = None) -> dict[str, Any]:
    """Generate a high-quality Mermaid diagram with deterministic styling.
    Args:
        util_llm: Optional smaller LLM for fix steps.

    Returns:
        response   : Markdown with mermaid fenced block.
        diagrams   : [{"mermaid_code": str}]
        tool_calls : [{"tool": "diagram_generation", "validated": bool}]
    """
    cfg = get_settings().agent
    query: str = state.get("query", "")
    timeout: float = max(cfg.llm_timeout, 480.0)
    max_retries: int = cfg.diagram_max_retries
    wall_t0: float = time.monotonic()
    phase1_limit: float = timeout * _PHASE1_BUDGET_FRACTION
    _fix_llm = util_llm or llm

    if hasattr(llm, "copy"):
        llm = llm.copy(update={"streaming": False})
    if hasattr(_fix_llm, "copy"):
        _fix_llm = _fix_llm.copy(update={"streaming": False})

    _no_think = {
        "extra_body": {
            "chat_template_kwargs": {"enable_thinking": False},
            "thinking_budget": 0,
            "reasoning_budget": 0,
        }
    }
    llm = llm.bind(**_no_think)
    _fix_llm = _fix_llm.bind(**_no_think)

    # Structured description
    desc_prompt = ChatPromptTemplate.from_messages(
        [
            ("system", DIAGRAM_DESCRIPTION_PROMPT),
            ("human", "Query: {query}"),
        ]
    )
    description_json: dict | None = None
    try:
        desc_result = await asyncio.wait_for(
            (desc_prompt | llm).ainvoke({"query": query}),
            timeout=phase1_limit,
        )
        raw_description = _to_str(desc_result)

        if len(raw_description) > 5000:
            raw_description = raw_description[:5000] + "\n... (truncated)"

        description_json = _parse_description_json(raw_description)
        if description_json:
            description_json = _trim_description_for_mermaid(description_json)
            description = json.dumps(description_json, ensure_ascii=False)
        else:
            description = raw_description
        phase2_limit: float = max(timeout - (time.monotonic() - wall_t0) - 5.0, 60.0)
        log.info(
            "diagram_description_complete",
            desc_len=len(description),
            parsed=description_json is not None,
            phase2_budget_s=round(phase2_limit),
        )
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
            timeout=phase2_limit,
        )
        mermaid_code = _sanitize_mermaid_ids(_strip_fences(_to_str(mermaid_result)))
        log.info("diagram_mermaid_generated", code_len=len(mermaid_code), lines=mermaid_code.count("\n"))
    except asyncio.CancelledError:
        log.error("diagram_mermaid_cancelled")
        raise
    except Exception as exc:
        log.error("diagram_mermaid_gen_failed", exc_type=type(exc).__name__, exc=str(exc)[:300])
        return {"response": "I was unable to generate the diagram code. Please try again."}
    mermaid_code = _inject_mermaid_styling(mermaid_code, description_json)
    log.info("diagram_styling_injected", styled_len=len(mermaid_code))
    validated, error_msg = _is_valid_mermaid(mermaid_code)
    if not validated:
        log.warning("diagram_initial_sanity_failed", preview=mermaid_code[:120], error=error_msg)

    for attempt in range(1, max_retries + 1):
        log.info("diagram_validation_check", attempt=attempt, valid=validated)
        if validated:
            log.info("diagram_valid", attempt=attempt)
            break

        if attempt >= max_retries:
            log.error("diagram_fix_exhausted", max_retries=max_retries)
            break

        try:
            fix_result = await asyncio.wait_for(
                _fix_llm.ainvoke(
                    [
                        SystemMessage(content=DIAGRAM_FIX_PROMPT),
                        HumanMessage(content=f"mermaid_code:\n```mermaid\n{mermaid_code}\n```\n\nerrors:\n{error_msg}"),
                    ]
                ),
                timeout=60.0,
            )
            fixed_code = _sanitize_mermaid_ids(_strip_fences(_to_str(fix_result)))
            mermaid_code = _inject_mermaid_styling(fixed_code, description_json)
            validated, error_msg = _is_valid_mermaid(mermaid_code)
            log.info("diagram_fix_applied", attempt=attempt, new_len=len(mermaid_code), valid=validated)
        except asyncio.CancelledError:
            log.error("diagram_fix_cancelled")
            raise
        except Exception as exc:
            log.warning("diagram_fix_failed", attempt=attempt, exc=str(exc)[:200])
            break

    if not validated:
        log.warning("diagram_returning_unvalidated", mermaid_lines=mermaid_code.count("\n"))

    response_text = _build_response(mermaid_code)

    return {
        "messages": [AIMessage(content=response_text)],
        "response": response_text,
        "diagrams": [{"mermaid_code": mermaid_code}],
        "tool_calls": [{"tool": "diagram_generation", "validated": validated}],
    }
