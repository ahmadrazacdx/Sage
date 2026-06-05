"""
Response generator node for Sage.

Performs three post-processing steps on the `explain` path's draft:

  1. `[KU#]` to `[N]` citation renumbering
  2. References section construction
  3. Final markdown assembly
"""

from __future__ import annotations

import os
import re
from typing import Any

import structlog

from langchain_core.messages import AIMessage
from sage.agents.state import AgentState

log = structlog.get_logger(__name__)

_RE_KU_TAG = re.compile(r"\[(KU\d+)\]", re.IGNORECASE)
_CLAIM_MAX_LEN: int = 120


def _normalize_source_name(name: str) -> str:
    base, _ = os.path.splitext(name)
    return re.sub(r"[^a-z0-9]", "", base.lower())


def _clean_source_name(source: str) -> tuple[str, str]:
    """Clean the source filename and return (emoji, clean_name)."""
    clean_name = source.strip()
    base, ext = os.path.splitext(clean_name)
    clean_name = base
    
    emoji = "📚"
    lower_ext = ext.lower()
    lower_source = source.lower()
    if lower_ext in (".pptx", ".ppt", ".docx", ".doc", ".txt", ".md") or "slide" in lower_source:
        emoji = "📑"
    return emoji, clean_name


def _clean_metadata(raw_claim: str, source: str) -> str | None:
    """Extract and clean the bracketed metadata header, filtering out source duplicates."""
    match = re.match(r"^\s*\[(.*?)\]", raw_claim)
    if not match:
        return None
    inner = match.group(1).strip()
    parts = [p.strip() for p in inner.split("|")]
    
    clean_parts = []
    norm_source = _normalize_source_name(source)
    
    for p in parts:
        if _normalize_source_name(p) == norm_source:
            continue
        clean_parts.append(p)
        
    if not clean_parts:
        return None
        
    return " | ".join(clean_parts)


def _build_citation_map(kus: list[dict]) -> dict[str, int]:
    """Build KU-id to numeric-index mapping, deduplicating by source book.

    If multiple KUs reference the same book, they will share the same citation index.
    """
    source_to_idx: dict[str, int] = {}
    ku_to_idx: dict[str, int] = {}
    idx = 0
    for ku in kus:
        ku_id = ku.get("id", "").strip().upper()
        if not ku_id:
            continue

        source = ku.get("source_file", "unknown").strip()
        emoji, clean_source = _clean_source_name(source)
        source_key = clean_source.lower()

        if source_key not in source_to_idx:
            idx += 1
            source_to_idx[source_key] = idx

        ku_to_idx[ku_id] = source_to_idx[source_key]

    return ku_to_idx


def _rewrite_citations(text: str, citation_map: dict[str, int]) -> str:
    """Replace `[KU#]` tags with `[N]` numeric citations."""

    def _replace(match: re.Match) -> str:  # type: ignore[type-arg]
        ku_id = match.group(1).upper()
        num = citation_map.get(ku_id)
        return f"[{num}]" if num is not None else ""

    return _RE_KU_TAG.sub(_replace, text)


def _build_references_section(
    kus: list[dict],
    citation_map: dict[str, int],
) -> str:
    """Build a `## References` markdown section from KUs.

    Args:
        kus: Raw knowledge-unit dicts from agent state.
        citation_map: Mapping produced by `_build_citation_map`.

    Returns:
        A multi-line markdown string, or ``""`` when there are no references.
    """
    if not citation_map:
        return ""

    # Collect (citation_number, ku_dict) pairs.
    ordered: list[tuple[int, dict]] = []
    for ku in kus:
        ku_id = ku.get("id", "").strip().upper()
        num = citation_map.get(ku_id)
        if num is not None:
            ordered.append((num, ku))

    ordered.sort(key=lambda t: t[0])

    # Deduplicate by citation number.
    seen_nums: set[int] = set()
    lines: list[str] = ["## References", ""]
    for num, ku in ordered:
        if num in seen_nums:
            continue
        seen_nums.add(num)

        source = ku.get("source_file", "unknown")
        raw_claim = ku.get("claim", ku.get("content", ""))

        emoji, clean_source = _clean_source_name(source)
        metadata = _clean_metadata(raw_claim, clean_source)

        if metadata:
            ref_str = f"[{num}] {emoji} {clean_source}: [{metadata}]"
        else:
            ref_str = f"[{num}] {emoji} {clean_source}"

        lines.append(ref_str)

    return "\n".join(lines)


async def response_node(state: AgentState) -> dict[str, Any]:
    """Format the draft into the final response.

    Args:
        state: Agent state containing at minimum `response` and
               `knowledge_units` keys.

    Returns:
        Dict with `response` (formatted markdown string) and `citations`
        (structured list for the frontend renderer).
    """
    response: str = state.get("response", "")
    kus: list[dict] = state.get("knowledge_units", [])

    if not response:
        log.warning("response_node_empty_draft")
        return {"response": "No response was generated. Please try again."}

    # Build citation map from available KUs.
    citation_map = _build_citation_map(kus)

    # Rewrite [KU#] to [N].
    formatted = _rewrite_citations(response, citation_map)

    # Append references section.
    refs = _build_references_section(kus, citation_map)
    if refs:
        if not formatted.endswith("\n\n"):
            if formatted.endswith("\n"):
                formatted += "\n"
            else:
                formatted += "\n\n"
        formatted += refs

    citations: list[dict] = []
    seen_ku_ids: set[str] = set()
    for ku in kus:
        ku_id = ku.get("id", "").strip().upper()
        if not ku_id or ku_id in seen_ku_ids:
            continue
        num = citation_map.get(ku_id)
        if num is not None:
            seen_ku_ids.add(ku_id)
            source = ku.get("source_file", "")
            emoji, clean_source = _clean_source_name(source)
            citations.append(
                {
                    "label": f"[{num}]",
                    "ku_id": ku_id,
                    "source": f"{emoji} {clean_source}",
                    "page": ku.get("source_page", ""),
                    "confidence": ku.get("confidence", ""),
                }
            )

    log.info(
        "response_formatted",
        citations_count=len(citations),
        response_len=len(formatted),
    )

    return {
        "messages": [AIMessage(content=formatted)],
        "response": formatted,
        "citations": citations,
    }
