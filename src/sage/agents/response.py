"""
Response generator node for Sage.

Performs three post-processing steps on the `explain` path's draft:

  1. `[KU#]` to `[N]` citation renumbering
  2. References section construction
  3. Final markdown assembly
"""

from __future__ import annotations

import re
from typing import Any

import structlog

from sage.agents.state import AgentState

log = structlog.get_logger(__name__)

_RE_KU_TAG = re.compile(r"\[(KU\d+)\]", re.IGNORECASE)
_CLAIM_MAX_LEN: int = 120


def _build_citation_map(kus: list[dict]) -> dict[str, int]:
    """Build KU-id to numeric-index mapping, deduplicated by id.

    Returns:
        `{"KU1": 1, "KU2": 2, ...}`
    """
    seen: dict[str, int] = {}
    idx = 0
    for ku in kus:
        ku_id = ku.get("id", "").strip().upper()
        if ku_id and ku_id not in seen:
            idx += 1
            seen[ku_id] = idx
    return seen


def _rewrite_citations(text: str, citation_map: dict[str, int]) -> str:
    """Replace `[KU#]` tags with `[N]` numeric citations."""

    def _replace(match: re.Match) -> str:  # type: ignore[type-arg]
        ku_id = match.group(1).upper()
        num = citation_map.get(ku_id)
        return f"[{num}]" if num is not None else ""

    return _RE_KU_TAG.sub(_replace, text)


def _sanitise_claim(claim: str) -> str:
    """Truncate to *_CLAIM_MAX_LEN* chars and escape embedded double-quotes."""
    claim = claim.replace('"', "'")
    if len(claim) > _CLAIM_MAX_LEN:
        return claim[:_CLAIM_MAX_LEN] + "…"
    return claim


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
        page = ku.get("source_page", "")
        raw_claim = ku.get("claim", ku.get("content", ""))
        confidence = ku.get("confidence", "")

        ref_str = f"[{num}] {source}"
        if page is not None and page != "":
            ref_str += f", p.{page}"
        ref_str += f': "{_sanitise_claim(raw_claim)}"'
        if confidence:
            ref_str += f" (confidence: {confidence})"
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
            citations.append(
                {
                    "label": f"[{num}]",
                    "ku_id": ku_id,
                    "source": ku.get("source_file", ""),
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
        "response": formatted,
        "citations": citations,
    }
