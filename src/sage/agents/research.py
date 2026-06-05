"""
Research agent subgraph for Sage.

Four-node pipeline:
  1. Planner: Generate structured plan with
     subtopics and per-source search queries.
  2. Searcher: Parallel search_arxiv / search_web / search_wikipedia.
  3. Digester : Each subtopic's raw sources are independently
              compressed into a tight digest via a small LLM call.
  4. Writer: Synthesized all digests into a full academic report.
  5. Reviewer: Quality gate. If verdict is "revise",
     loops back to Writer (max 2 iterations).

Online check: skips online tools if `state["online_mode"] == False`
and warns the user.  Export tools (markdown/PDF) are invoked for
report delivery.
"""

from __future__ import annotations

import asyncio
import dataclasses
import math
import re
from pathlib import Path
from typing import Any

import structlog
from langchain_core.messages import AIMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field, model_validator

from sage.agents.state import AgentState
from sage.config import get_settings
from sage.prompts import (
    RESEARCH_PLAN_PROMPT,
    RESEARCH_REPORT_PROMPT,
    RESEARCH_REVIEW_PROMPT,
)
from sage.tools.export import export_markdown, export_pdf
from sage.utils import ainvoke_structured_with_fallback, strip_think_markers

log = structlog.get_logger(__name__)

_MAX_PLAN_RETRIES: int = 2
_MAX_ITEM_CHARS: int = 600
_TOK_PER_WORD: float = 1.4
_REF_SECTION_HEADING_RE = re.compile(r"(?im)^(?:#{1,6}\s*|\*\*\s*)references(?:\s*\*\*)?\s*:?\s*$")
_REF_ITEM_START_RE = re.compile(r"^\[\d+\]\s+")
_REF_INITIAL_RUN_RE = re.compile(r"(?:\b[A-Z]\.\s*){12,}")
_PLACEHOLDER_REF_RE = re.compile(r"(?im)^\[\d+\]\s+(?:Author(?:\(s\)|\s*\d*)?[.,]?\s*)?Title\.\s*Venue\.\s*Year\.\s*$")
_MAX_REFERENCE_LINE_CHARS = 320
_MAX_INITIALS_IN_RUN = 8
_MAX_FALLBACK_REFS = 24


def _no_think(llm: ChatOpenAI) -> ChatOpenAI:
    return llm.bind(
        extra_body={
            "chat_template_kwargs": {"enable_thinking": False},
            "thinking_budget": 0,
            "reasoning_budget": 0,
        }
    )


_DIGEST_PROMPT = (
    'Subtopic: "{name}"\n\n'
    "Summarise key facts from the sources below in ≤{words} words.\n"
    "Cite every fact as [N]. Precise and technical. No filler.\n\n"
    "Sources:\n{sources}"
)


@dataclasses.dataclass(frozen=True)
class ContextBudget:
    """All token/character limits for one research pipeline run."""

    ctx_size: int  # physical context window (tokens)
    resp_reserve: int  # tokens kept free for report output
    prompt_overhead: int  # writer template boilerplate (tokens)
    digest_words: int  # target digest length per subtopic (words)
    max_subtopics: int  # cap passed to the planner prompt
    digest_in_chars: int  # max raw-source chars fed into ONE digest call

    @property
    def source_char_budget(self) -> int:
        """Max characters of ALL digests combined fed to the report writer."""
        avail_tokens = max(self.ctx_size - self.prompt_overhead - self.resp_reserve, 200)
        return avail_tokens * 4

    @property
    def digest_out_chars(self) -> int:
        """Expected max characters of one digest output (for budget checks)."""
        return int(self.digest_words * _TOK_PER_WORD * 5)  # ~5 chars/word

    @classmethod
    def from_settings(cls) -> ContextBudget:
        """Derive budget from the live LLM context window."""
        llm_cfg = get_settings().llm
        ctx: int = getattr(llm_cfg, "active_context_size", None) or 768
        parallel: int = max(getattr(llm_cfg, "active_parallel_slots", 1), 1)

        resp_reserve: int = max(min(int(ctx * 0.60), 12_000), min(ctx // 2, 300))
        prompt_overhead: int = 180

        # Digest words
        avail_for_digests_tokens = max(ctx - prompt_overhead - resp_reserve, 200)
        max_subs = cls._calc_max_subtopics(ctx)
        words_each = int(avail_for_digests_tokens / (max_subs * _TOK_PER_WORD))
        digest_words: int = max(min(words_each, 200), 60)

        # Digest input budget
        digest_out_reserve = int(digest_words * _TOK_PER_WORD * 1.25)
        digest_in_chars = max((ctx - 30 - digest_out_reserve), 500) * 4

        log.debug(
            "context_budget_computed",
            ctx_size=ctx,
            parallel_slots=parallel,
            resp_reserve=resp_reserve,
            digest_words=digest_words,
            max_subtopics=max_subs,
            source_char_budget=avail_for_digests_tokens * 4,
            digest_in_chars=digest_in_chars,
        )

        return cls(
            ctx_size=ctx,
            resp_reserve=resp_reserve,
            prompt_overhead=prompt_overhead,
            digest_words=digest_words,
            max_subtopics=max_subs,
            digest_in_chars=digest_in_chars,
        )

    @staticmethod
    def _calc_max_subtopics(ctx: int) -> int:
        """Return planner subtopic cap based on context size."""
        if ctx < 4_096:
            return 3
        if ctx < 8_192:
            return 4
        return 5


class SubtopicQueries(BaseModel):
    academic: str = ""
    web: str = ""
    encyclopedia: str = ""


class Subtopic(BaseModel):
    name: str
    description: str = ""
    queries: SubtopicQueries = Field(default_factory=SubtopicQueries)


class ResearchPlan(BaseModel):
    title: str
    subtopics: list[Subtopic]

    @model_validator(mode="before")
    @classmethod
    def _coerce_shape(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        out = dict(data)
        if "subtopics" not in out:
            for alias in ("topics", "sections", "plan"):
                maybe = out.get(alias)
                if isinstance(maybe, list):
                    out["subtopics"] = maybe
                    break

        normalized: list[dict[str, Any]] = []
        for raw in out.get("subtopics", []) or []:
            if isinstance(raw, str):
                normalized.append(
                    {
                        "name": raw,
                        "description": "",
                        "queries": SubtopicQueries().model_dump(),
                    }
                )
                continue

            if not isinstance(raw, dict):
                continue

            item = dict(raw)
            queries = item.get("queries")
            if not isinstance(queries, dict):
                queries = {
                    "academic": str(item.get("academic") or item.get("academic_query") or ""),
                    "web": str(item.get("web") or item.get("web_query") or ""),
                    "encyclopedia": str(item.get("encyclopedia") or item.get("wikipedia") or item.get("wiki") or ""),
                }
            item["queries"] = queries
            item.setdefault("description", "")
            normalized.append(item)

        out["subtopics"] = normalized
        return out


class SubtopicCoverage(BaseModel):
    subtopic: str
    coverage: str = Field(description="complete | partial | missing")


class ReviewIssue(BaseModel):
    type: str
    detail: str
    location: str = ""


class ResearchReview(BaseModel):
    verdict: str = Field(description="pass | revise")
    factual_accuracy: str = "pass"
    citation_completeness: str = "sufficient"
    subtopic_coverage: list[SubtopicCoverage] = Field(default_factory=list)
    structural_conformance: str = "pass"
    issues: list[ReviewIssue] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    overall_comment: str = ""


def _trim(text: str, max_chars: int, ellipsis: bool = True) -> str:
    """Truncate text to approximately `max_chars`."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + ("…" if ellipsis else "")


def _compress_initials_run(match: re.Match[str]) -> str:
    """Shorten pathological repeated initial sequences in broken references."""
    initials = re.findall(r"[A-Z]\.", match.group(0))
    if len(initials) <= _MAX_INITIALS_IN_RUN:
        return match.group(0)
    kept = " ".join(initials[:_MAX_INITIALS_IN_RUN])
    return f"{kept} et al. "


def _normalize_references_section(report: str) -> str:
    """Force one-reference-per-line formatting in the References section."""
    heading_match = _REF_SECTION_HEADING_RE.search(report)
    if heading_match is None:
        return report

    prefix = report[: heading_match.end()]
    references_raw = report[heading_match.end() :].strip()
    if not references_raw:
        return report

    # Ensure each [N] marker starts on its own line inside References.
    references_raw = re.sub(r"(?<!\n)\s*(\[\d+\])\s*", r"\n\1 ", references_raw)

    items: list[str] = []
    current = ""
    for raw_line in references_raw.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if _REF_ITEM_START_RE.match(line):
            if current:
                items.append(current)
            current = line
            continue
        if current:
            current = f"{current} {line}"

    if current:
        items.append(current)
    if not items:
        return report

    seen_refs = set()
    cleaned_items: list[str] = []
    for item in items:
        content_only = re.sub(r"^\[\d+\]\s*", "", item).strip()
        ref_key = "".join(c for c in content_only.lower() if c.isalnum())
        if not ref_key:
            continue
        if ref_key in seen_refs:
            continue
        seen_refs.add(ref_key)

        normalized = " ".join(item.split())
        normalized = _REF_INITIAL_RUN_RE.sub(_compress_initials_run, normalized)
        if len(normalized) > _MAX_REFERENCE_LINE_CHARS:
            normalized = normalized[:_MAX_REFERENCE_LINE_CHARS].rstrip() + "..."
        idx = len(cleaned_items) + 1
        normalized = re.sub(r"^\[\d+\]", f"[{idx}]", normalized)
        cleaned_items.append(normalized)

    return f"{prefix}\n" + "\n".join(cleaned_items)


def _build_fallback_references(all_sources: list[dict[str, Any]]) -> list[str]:
    """Build deterministic references from retrieved source metadata."""
    refs: list[str] = []
    seen_titles: set[str] = set()
    idx = 1

    for src in all_sources:
        if idx > _MAX_FALLBACK_REFS:
            break
        data = src.get("data")
        if not isinstance(data, dict):
            continue
        items = data.get("results", [])
        if not isinstance(items, list):
            continue

        source_name = str(src.get("source", "source")).strip() or "source"
        for item in items:
            if idx > _MAX_FALLBACK_REFS:
                break
            if not isinstance(item, dict):
                continue

            raw_title = str(item.get("title") or "").strip()
            if not raw_title:
                continue
            title = " ".join(raw_title.split())
            title_key = title.lower()
            if title_key in seen_titles:
                continue
            seen_titles.add(title_key)

            refs.append(f"[{idx}] {title}. {source_name}.")
            idx += 1

    return refs


def _all_refs_look_fake(references_raw: str) -> bool:
    """Heuristic: if ≥60% of [N] lines match the generic pattern, treat all as fake."""
    lines = [line.strip() for line in references_raw.splitlines() if _REF_ITEM_START_RE.match(line.strip())]
    if len(lines) < 2:
        return False
    fake = sum(
        1
        for line in lines
        if re.search(r"\bTitle\b.*\bVenue\b.*\bYear\b", line, re.IGNORECASE) or _PLACEHOLDER_REF_RE.match(line)
    )
    return fake >= len(lines) * 0.6


def _replace_placeholder_references(
    report: str,
    all_sources: list[dict[str, Any]],
) -> str:
    """Replace generic placeholder references with source-derived references."""
    heading_match = _REF_SECTION_HEADING_RE.search(report)
    if heading_match is None:
        return report

    prefix = report[: heading_match.end()]
    references_raw = report[heading_match.end() :].strip()
    if not references_raw:
        return report
    if not _PLACEHOLDER_REF_RE.search(references_raw) and not _all_refs_look_fake(references_raw):
        return report

    fallback_refs = _build_fallback_references(all_sources)
    if not fallback_refs:
        return report
    return f"{prefix}\n" + "\n".join(fallback_refs)


def _force_replace_references(report: str, source_refs: str) -> str:
    """Always replace the References section with our deterministic source-derived refs.

    This prevents the LLM from hallucinating or repeating garbled author strings
    in the references block.  Falls back to the original report when no source
    refs are available or no References heading is found.
    """
    if not source_refs or source_refs.strip() == "No references available.":
        return report
    heading_match = _REF_SECTION_HEADING_RE.search(report)
    if heading_match is None:
        return report
    prefix = report[: heading_match.end()]
    return f"{prefix}\n{source_refs}"


def _build_source_references(all_sources: list[dict[str, Any]]) -> str:
    """Build a deterministic References block from search metadata for prompt injection."""
    refs = _build_fallback_references(all_sources)
    if not refs:
        return "No references available."
    return "\n".join(refs)


def _fallback_research_plan(query: str, max_subtopics: int) -> ResearchPlan:
    """Build a deterministic search plan when structured planning fails."""
    q = query.strip() or "the requested topic"
    templates = [
        (
            f"Foundations of {q}",
            f"Core definitions and background for {q}.",
            {
                "academic": f"{q} fundamentals survey",
                "web": f"{q} overview",
                "encyclopedia": q,
            },
        ),
        (
            f"Recent advances in {q}",
            f"Recent methods, results, and trends for {q}.",
            {
                "academic": f"{q} recent advances survey 2024 2025",
                "web": f"latest developments in {q}",
                "encyclopedia": q,
            },
        ),
        (
            f"Applications of {q}",
            f"Practical uses and real-world deployment of {q}.",
            {
                "academic": f"{q} applications survey",
                "web": f"real world applications of {q}",
                "encyclopedia": q,
            },
        ),
        (
            f"Limitations and risks in {q}",
            f"Known weaknesses, risks, and tradeoffs in {q}.",
            {
                "academic": f"{q} limitations challenges survey",
                "web": f"limitations of {q}",
                "encyclopedia": q,
            },
        ),
        (
            f"Open problems in {q}",
            f"Current research gaps and unresolved questions in {q}.",
            {
                "academic": f"{q} open problems future work survey",
                "web": f"open research questions in {q}",
                "encyclopedia": q,
            },
        ),
    ]

    picked = templates[: max(1, max_subtopics)]
    return ResearchPlan(
        title=f"Research survey on {q}"[:120],
        subtopics=[
            Subtopic(name=name, description=desc, queries=SubtopicQueries(**queries)) for name, desc, queries in picked
        ],
    )


async def _search_source(
    tool_fn: Any,
    query: str,
    source_name: str,
    timeout: int,
) -> dict[str, Any]:
    """Run one search tool with timeout and error isolation."""
    try:
        result = await asyncio.wait_for(
            tool_fn.ainvoke({"query": query}),
            timeout=timeout,
        )
        return {"source": source_name, "data": result, "error": None}
    except TimeoutError:
        log.warning("search_timeout", source=source_name, query=query[:60])
        return {"source": source_name, "data": None, "error": f"{source_name} timed out"}
    except Exception as exc:
        log.warning("search_error", source=source_name, exc=str(exc)[:200])
        return {"source": source_name, "data": None, "error": str(exc)[:200]}


def _build_subtopic_source_text(
    sources: list[dict],
    subtopic_name: str,
    global_idx: int,
    max_item_chars: int,
) -> tuple[str, int]:
    """Format search results for one subtopic into a numbered reference block.

    Returns (formatted_text, next_global_index).
    Sources must be tagged with `_subtopic` during search construction.
    """
    lines: list[str] = []
    idx = global_idx

    for src in sources:
        if src.get("_subtopic") != subtopic_name:
            continue
        if src.get("error") or not src.get("data"):
            continue

        data = src["data"]
        items = data.get("results", []) if isinstance(data, dict) else []
        label = src.get("source", "unknown")

        for item in items:
            idx += 1
            raw = item.get("content", item.get("title", ""))
            title = item.get("title", f"Source {idx}")
            lines.append(f"[{idx}] ({label}) {title}\n{_trim(raw, max_item_chars)}")

    text = "\n\n".join(lines) if lines else "No sources retrieved for this subtopic."
    return text, idx


async def _digest_subtopic(
    subtopic: Subtopic,
    source_text: str,
    budget: ContextBudget,
    llm: ChatOpenAI,
    timeout: float,
) -> str:
    """Compress one subtopic's source text into a tight digest.

    Falls back to a truncated version of the raw sources on any LLM
    error so the pipeline never stalls on a single bad digest call.
    """
    trimmed = _trim(source_text, budget.digest_in_chars, ellipsis=False)

    prompt_text = _DIGEST_PROMPT.format(
        name=subtopic.name,
        words=budget.digest_words,
        sources=trimmed,
    )

    estimated_tokens = math.ceil(len(prompt_text) / 4)
    digest_headroom = budget.ctx_size - math.ceil(budget.digest_words * _TOK_PER_WORD * 1.3) - 30
    if estimated_tokens > digest_headroom:
        log.warning(
            "digest_prompt_oversized",
            subtopic=subtopic.name,
            estimated=estimated_tokens,
            headroom=digest_headroom,
            ctx_size=budget.ctx_size,
        )
        return _trim(trimmed, budget.digest_out_chars)

    try:
        result = await asyncio.wait_for(llm.ainvoke(prompt_text), timeout=timeout)
        digest = result.content if isinstance(result, AIMessage) else str(result)
        digest = strip_think_markers(digest)
        log.info("digest_ok", subtopic=subtopic.name[:40], words=len(digest.split()), ctx=budget.ctx_size)
        return digest.strip()
    except Exception as exc:
        log.warning("digest_failed", subtopic=subtopic.name[:40], exc=str(exc)[:200])
        return _trim(trimmed, budget.digest_out_chars)


async def _run_digest_phase(
    plan: ResearchPlan,
    all_sources: list[dict],
    budget: ContextBudget,
    llm: ChatOpenAI,
    timeout: float,
) -> str:
    """MAP: compress each subtopic's sources in parallel.

    Returns a combined digest block ready for the report writer.
    The combined text is guaranteed to fit inside `budget.source_char_budget`.
    """
    # 1. Build per-subtopic source text blocks
    subtopic_sources: list[tuple[Subtopic, str]] = []
    global_idx = 0
    for sub in plan.subtopics:
        text, global_idx = _build_subtopic_source_text(all_sources, sub.name, global_idx, _MAX_ITEM_CHARS)
        subtopic_sources.append((sub, text))

    # 2. Compress all subtopics in parallel
    digests = await asyncio.gather(
        *[_digest_subtopic(sub, src, budget, llm, timeout) for sub, src in subtopic_sources],
        return_exceptions=True,
    )

    # 3. Assemble & safety-trim combined block
    blocks: list[str] = []
    for (sub, _), digest in zip(subtopic_sources, digests, strict=False):
        if isinstance(digest, Exception):
            log.warning("digest_gather_exc", subtopic=sub.name, exc=str(digest)[:200])
            blocks.append(f"### {sub.name}\n[digest unavailable]")
        else:
            blocks.append(f"### {sub.name}\n{digest}")

    combined = "\n\n".join(blocks)

    # Hard-trim to guarantee writer stays in budget
    combined = _trim(combined, budget.source_char_budget, ellipsis=False)

    log.info(
        "digest_phase_done",
        subtopics=len(plan.subtopics),
        chars=len(combined),
        estimated_tokens=math.ceil(len(combined) / 4),
        writer_budget_tokens=budget.ctx_size - budget.prompt_overhead - budget.resp_reserve,
    )
    return combined


async def research_node(
    state: AgentState,
    llm: ChatOpenAI,
    *,
    digest_llm: ChatOpenAI | None = None,
) -> dict[str, Any]:
    """Execute the full Map-Digest-Reduce research pipeline.

    Args:
    state:      Agent state dict with `query` and `online_mode`.
    llm:        Main LLM (planner, writer, reviewer).
    digest_llm: Optional smaller/faster LLM for the MAP digest phase.
                When None, `llm` is used for all phases.
    """
    cfg = get_settings().agent
    query: str = state.get("query", "")
    online: bool = state.get("online_mode", False)
    timeout = float(getattr(cfg, "llm_timeout", 120))
    _digest_llm = digest_llm or llm

    # Offline guard
    if not online:
        log.warning("research_offline_mode")
        res_text = (
            "⚠️ **Research mode requires an internet connection.**\n\n"
            "The Research Agent searches arXiv, the web, and Wikipedia "
            "for multi-source academic reports.  Please connect to the "
            "internet and try again.\n\n"
            "Alternatively, use **Explain** mode for answers from "
            "your course materials (offline)."
        )
        return {
            "messages": [AIMessage(content=res_text)],
            "response": res_text,
            "research_report": None,
        }

    budget = ContextBudget.from_settings()
    if digest_llm is not None:
        util_ctx = get_settings().llm.util_context_window
        digest_out_reserve = int(budget.digest_words * _TOK_PER_WORD * 1.25)
        capped_in_chars = max((util_ctx - 30 - digest_out_reserve), 500) * 4
        budget = dataclasses.replace(budget, digest_in_chars=capped_in_chars)
        log.info(
            "digest_in_chars_capped_for_util_model",
            util_ctx=util_ctx,
            original_digest_words=budget.digest_words,
            capped_in_chars=capped_in_chars,
        )

    log.info(
        "research_budget",
        ctx_size=budget.ctx_size,
        max_subtopics=budget.max_subtopics,
        digest_words=budget.digest_words,
        resp_reserve_tokens=budget.resp_reserve,
        source_char_budget=budget.source_char_budget,
    )

    # Phase 1: Plan
    plan_prompt = ChatPromptTemplate.from_messages(
        [
            ("human", RESEARCH_PLAN_PROMPT),
        ]
    )

    plan: ResearchPlan | None = None
    for attempt in range(1, _MAX_PLAN_RETRIES + 1):
        try:
            plan = await ainvoke_structured_with_fallback(
                prompt=plan_prompt,
                llm=_no_think(llm),
                schema=ResearchPlan,
                payload={
                    "query": query,
                    "max_subtopics": budget.max_subtopics,
                },
                timeout_s=timeout,
                logger=log,
                event_prefix="research_plan",
                prefer_raw_json=True,
            )
            if len(plan.subtopics) > budget.max_subtopics:
                plan = ResearchPlan(
                    title=plan.title,
                    subtopics=plan.subtopics[: budget.max_subtopics],
                )
                log.info("plan_subtopics_capped", cap=budget.max_subtopics, original=len(plan.subtopics))
            log.info("research_plan_complete", title=plan.title, subtopics=len(plan.subtopics), attempt=attempt)
            break
        except Exception as exc:
            log.warning("research_plan_retry", attempt=attempt, exc=str(exc)[:200])

    if plan is None:
        log.warning("research_plan_fallback", query_preview=query[:80])
        plan = _fallback_research_plan(query, budget.max_subtopics)

    # Phase 2: Parallel multi-source search
    try:
        from sage.tools.search import search_arxiv, search_web, search_wikipedia
    except ImportError as exc:
        res_text = f"⚠️ Search tools unavailable: {exc}"
        return {
            "messages": [AIMessage(content=res_text)],
            "response": res_text,
        }

    search_cfg = get_settings().tools.search
    tagged: list[tuple[str, Any]] = []

    for subtopic in plan.subtopics:
        q = subtopic.queries
        if q.academic:
            tagged.append((subtopic.name, _search_source(search_arxiv, q.academic, "arxiv", search_cfg.arxiv_timeout)))
        if q.web:
            tagged.append((subtopic.name, _search_source(search_web, q.web, "web", search_cfg.web_timeout)))
        if q.encyclopedia:
            tagged.append(
                (
                    subtopic.name,
                    _search_source(
                        search_wikipedia,
                        q.encyclopedia,
                        "wikipedia",
                        search_cfg.wiki_timeout,
                    ),
                )
            )

    all_sources: list[dict] = []
    if tagged:
        raw = await asyncio.gather(*[c for _, c in tagged], return_exceptions=True)
        for (sub_name, _), result in zip(tagged, raw, strict=False):
            if isinstance(result, dict):
                result["_subtopic"] = sub_name
                all_sources.append(result)
            else:
                log.warning("search_gather_exception", exc=str(result)[:200])

    log.info("research_sources_ready", total=len(all_sources), with_data=sum(1 for s in all_sources if s.get("data")))

    # Phase 3: Digest MAP
    digest_text = await _run_digest_phase(plan, all_sources, budget, _digest_llm, timeout)

    # Build deterministic references from source metadata
    source_refs = _build_source_references(all_sources)

    # Report Writing
    report_prompt = ChatPromptTemplate.from_messages([("human", RESEARCH_REPORT_PROMPT)])
    report_chain = report_prompt | llm

    writer_timeout = float(getattr(cfg, "research_writer_timeout", 300))
    try:
        report_result = await asyncio.wait_for(
            report_chain.ainvoke(
                {
                    "sources": digest_text,
                    "title": plan.title,
                    "source_references": source_refs,
                }
            ),
            timeout=writer_timeout,
        )
        report = report_result.content if isinstance(report_result, AIMessage) else str(report_result)
        report = strip_think_markers(report)
        report = _force_replace_references(report, source_refs)
        report = _normalize_references_section(report)
    except Exception as exc:
        log.error("research_report_failed", exc=str(exc)[:200])
        res_text = "I was unable to synthesize the research report. Please try again."
        return {
            "messages": [AIMessage(content=res_text)],
            "response": res_text,
        }

    log.info(
        "research_report_written",
        chars=len(report),
        words=len(report.split()),
        estimated_pages=round(len(report.split()) / 350, 1),
    )

    # Phase 5: Review loop
    review_prompt = ChatPromptTemplate.from_messages(
        [
            ("human", RESEARCH_REVIEW_PROMPT),
        ]
    )
    max_iters = int(getattr(cfg, "research_max_iters", 2))

    for review_iter in range(1, max_iters + 1):
        report_tokens = math.ceil(len(report) / 4)
        review_overhead = 180
        if report_tokens + review_overhead > budget.ctx_size - 300:
            keep_chars = (budget.ctx_size - 300 - review_overhead) * 4
            review_report = "…[trimmed for review]\n" + report[-keep_chars:]
            log.warning("review_report_trimmed", original_tokens=report_tokens, keep_chars=keep_chars)
        else:
            review_report = report
        try:
            review_timeout = min(float(timeout), 90.0)  # cap so a slow reviewer can't double overall latency
            review: ResearchReview = await ainvoke_structured_with_fallback(
                prompt=review_prompt,
                llm=_no_think(llm),
                schema=ResearchReview,
                payload={"report": review_report},
                timeout_s=review_timeout,
                logger=log,
                event_prefix="research_review",
                prefer_raw_json=True,
            )
            log.info(
                "research_review_complete",
                verdict=review.verdict,
                iteration=review_iter,
            )

            if review.verdict == "pass":
                break

            issues_text = "\n".join(f"- [{i.type}] {i.detail}" for i in review.issues)
            suggestions_text = "\n".join(f"- {s}" for s in review.suggestions)
            revision_context = (
                f"## Review Feedback (Iteration {review_iter})\n"
                f"### Issues\n{issues_text}\n"
                f"### Suggestions\n{suggestions_text}\n"
                f"### Overall\n{review.overall_comment}\n\n"
                f"## Original Report\n{report}"
            )

            report_result = await asyncio.wait_for(
                report_chain.ainvoke(
                    {
                        "sources": _trim(revision_context, budget.source_char_budget, ellipsis=False),
                        "title": plan.title,
                        "source_references": source_refs,
                    }
                ),
                timeout=writer_timeout,
            )
            report = report_result.content if isinstance(report_result, AIMessage) else str(report_result)
            report = strip_think_markers(report)
            report = _force_replace_references(report, source_refs)
            report = _normalize_references_section(report)
            log.info("research_report_revised", iteration=review_iter)

        except Exception as exc:
            log.warning(
                "research_review_failed",
                iteration=review_iter,
                exc=str(exc)[:200],
            )
            break

    artifact_paths: list[dict[str, str]] = []
    pdf_export_failed = False

    # Export markdown
    safe_title = plan.title.replace(" ", "_")[:50]
    export_suffix = f"research_{safe_title}"
    try:
        export_markdown.invoke(
            {
                "content": report,
                "filename": export_suffix,
            }
        )
    except Exception as exc:
        log.warning("research_export_failed", exc=str(exc)[:200])

    try:
        pdf_result = await export_pdf.ainvoke(
            {
                "content": report,
                "filename": export_suffix,
                "title": plan.title,
                "subtitle": "AI-Generated Research Report",
                "author": "Sage Research Agent",
            }
        )
        pdf_path = str(pdf_result.get("path") or "").strip()
        if pdf_path:
            artifact_paths.append(
                {
                    "kind": "pdf",
                    "filename": Path(pdf_path).name,
                    "path": pdf_path,
                }
            )
        elif pdf_result.get("error"):
            pdf_export_failed = True
            log.warning("export_pdf_soft_fail", error=pdf_result["error"])
        else:
            pdf_export_failed = True
            log.warning("export_pdf_missing_path")
    except Exception as exc:
        pdf_export_failed = True
        log.warning("export_pdf_failed", exc=str(exc)[:200])

    response = report
    if artifact_paths:
        response = "I have completed your research report. Use the download button below to get the PDF."
    elif pdf_export_failed:
        log.info("research_fallback_text_response")

    return {
        "messages": [AIMessage(content=response)],
        "response": response,
        "research_plan": plan.model_dump(),
        "research_sources": all_sources,
        "research_report": report,
        "artifact_paths": artifact_paths,
    }
