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
from typing import Any

import structlog
from langchain_core.messages import AIMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from sage.agents.state import AgentState
from sage.config import get_settings
from sage.prompts import (
    RESEARCH_PLAN_PROMPT,
    RESEARCH_REPORT_PROMPT,
    RESEARCH_REVIEW_PROMPT,
)
from sage.tools.export import export_markdown, export_pdf

log = structlog.get_logger(__name__)

_MAX_PLAN_RETRIES: int  = 2
_MAX_ITEM_CHARS: int    = 600
_TOK_PER_WORD: float    = 1.4


def _no_think(llm: ChatOpenAI) -> ChatOpenAI:
    return llm.bind(
        extra_body={"chat_template_kwargs": {"enable_thinking": False}}
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
 
    ctx_size: int           # physical context window (tokens)
    resp_reserve: int       # tokens kept free for report output
    prompt_overhead: int    # writer template boilerplate (tokens)
    digest_words: int       # target digest length per subtopic (words)
    max_subtopics: int      # cap passed to the planner prompt
    digest_in_chars: int    # max raw-source chars fed into ONE digest call
 
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
    def from_settings(cls) -> "ContextBudget":
        """Derive budget from the live LLM context window.

        llama.cpp splits ``ctx_size`` evenly across ``--parallel`` slots.
        All budget limits must be derived from the per-slot budget
        (``ctx_size // parallel_slots``), not the total KV-cache size.

        Safe to call before the server starts (falls back to 3072 ctx / 1 slot).
        """
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
    description: str
    queries: SubtopicQueries


class ResearchPlan(BaseModel):
    title: str
    subtopics: list[Subtopic]


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
    except asyncio.TimeoutError:
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
 
        data  = src["data"]
        items = data.get("results", []) if isinstance(data, dict) else []
        label = src.get("source", "unknown")
 
        for item in items:
            idx += 1
            raw   = item.get("content", item.get("title", ""))
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
        log.warning("digest_prompt_oversized",
                    subtopic=subtopic.name, estimated=estimated_tokens,
                    headroom=digest_headroom, ctx_size=budget.ctx_size)
        return _trim(trimmed, budget.digest_out_chars)
 
    try:
        result = await asyncio.wait_for(llm.ainvoke(prompt_text), timeout=timeout)
        digest = result.content if isinstance(result, AIMessage) else str(result)
        log.info("digest_ok", subtopic=subtopic.name[:40],
                 words=len(digest.split()), ctx=budget.ctx_size)
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
        text, global_idx = _build_subtopic_source_text(
            all_sources, sub.name, global_idx, _MAX_ITEM_CHARS
        )
        subtopic_sources.append((sub, text))
 
    # 2. Compress all subtopics in parallel
    digests = await asyncio.gather(
        *[_digest_subtopic(sub, src, budget, llm, timeout)
          for sub, src in subtopic_sources],
        return_exceptions=True,
    )
 
    # 3. Assemble & safety-trim combined block
    blocks: list[str] = []
    for (sub, _), digest in zip(subtopic_sources, digests):
        if isinstance(digest, Exception):
            log.warning("digest_gather_exc", subtopic=sub.name, exc=str(digest)[:200])
            blocks.append(f"### {sub.name}\n[digest unavailable]")
        else:
            blocks.append(f"### {sub.name}\n{digest}")
 
    combined = "\n\n".join(blocks)
 
    # Hard-trim to guarantee writer stays in budget
    combined = _trim(combined, budget.source_char_budget, ellipsis=False)
 
    log.info("digest_phase_done",
             subtopics=len(plan.subtopics),
             chars=len(combined),
             estimated_tokens=math.ceil(len(combined) / 4),
             writer_budget_tokens=budget.ctx_size - budget.prompt_overhead - budget.resp_reserve)
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
        return {
            "response": (
                "⚠️ **Research mode requires an internet connection.**\n\n"
                "The Research Agent searches arXiv, the web, and Wikipedia "
                "for multi-source academic reports.  Please connect to the "
                "internet and try again.\n\n"
                "Alternatively, use **Explain** mode for answers from "
                "your course materials (offline)."
            ),
            "research_report": None,
        }

    budget = ContextBudget.from_settings()
    log.info("research_budget",
             ctx_size=budget.ctx_size,
             max_subtopics=budget.max_subtopics,
             digest_words=budget.digest_words,
             resp_reserve_tokens=budget.resp_reserve,
             source_char_budget=budget.source_char_budget)

    # Phase 1: Plan
    plan_prompt = ChatPromptTemplate.from_messages([
        ("human", RESEARCH_PLAN_PROMPT),
    ])
    plan_chain = plan_prompt | _no_think(llm).with_structured_output(ResearchPlan)

    plan: ResearchPlan | None = None
    for attempt in range(1, _MAX_PLAN_RETRIES + 1):
        try:
            plan = await asyncio.wait_for(
                plan_chain.ainvoke({
                    "query": query,
                    "max_subtopics": budget.max_subtopics,
                }),
                timeout=timeout,
            )
            if len(plan.subtopics) > budget.max_subtopics:
                plan = ResearchPlan(
                    title=plan.title,
                    subtopics=plan.subtopics[:budget.max_subtopics],
                )
                log.info("plan_subtopics_capped",
                         cap=budget.max_subtopics, original=len(plan.subtopics))
            log.info("research_plan_complete",
                     title=plan.title,
                     subtopics=len(plan.subtopics),
                     attempt=attempt)
            break
        except Exception as exc:
            log.warning("research_plan_retry", attempt=attempt, exc=str(exc)[:200])

    if plan is None:
        return {
            "response": "I was unable to create a research plan. Please try again.",
            "research_plan": None,
        }

    # Phase 2: Parallel multi-source search
    try:
        from sage.tools.search import search_arxiv, search_web, search_wikipedia
    except ImportError as exc:
        return {"response": f"⚠️ Search tools unavailable: {exc}"}

    search_cfg = get_settings().tools.search
    tagged: list[tuple[str, Any]] = []

    for subtopic in plan.subtopics:
        q = subtopic.queries
        if q.academic:
            tagged.append((subtopic.name,
                _search_source(search_arxiv, q.academic, "arxiv", search_cfg.arxiv_timeout)))
        if q.web:
            tagged.append((subtopic.name,
                _search_source(search_web, q.web, "web", search_cfg.web_timeout)))
        if q.encyclopedia:
            tagged.append((subtopic.name,
                _search_source(search_wikipedia, q.encyclopedia, "wikipedia", search_cfg.wiki_timeout)))

    all_sources: list[dict] = []
    if tagged:
        raw = await asyncio.gather(*[c for _, c in tagged], return_exceptions=True)
        for (sub_name, _), result in zip(tagged, raw):
            if isinstance(result, dict):
                result["_subtopic"] = sub_name
                all_sources.append(result)
            else:
                log.warning("search_gather_exception", exc=str(result)[:200])

    log.info("research_sources_ready",
             total=len(all_sources),
             with_data=sum(1 for s in all_sources if s.get("data")))
 

    # Phase 3: Digest MAP
    digest_text = await _run_digest_phase(
        plan, all_sources, budget, _digest_llm, timeout
    )

    # Report Writing
    report_prompt = ChatPromptTemplate.from_messages([("human", RESEARCH_REPORT_PROMPT)])
    report_chain = report_prompt | llm

    writer_timeout = float(getattr(cfg, "research_writer_timeout", 300))
    try:
        report_result = await asyncio.wait_for(
            report_chain.ainvoke({
                "sources": digest_text,
                "title": plan.title,
            }),
            timeout=writer_timeout,
        )
        report = (
            report_result.content
            if isinstance(report_result, AIMessage)
            else str(report_result)
        )
    except Exception as exc:
        log.error("research_report_failed", exc=str(exc)[:200])
        return {"response": "I was unable to synthesize the research report. Please try again."}

    log.info("research_report_written",
             chars=len(report),
             words=len(report.split()),
             estimated_pages=round(len(report.split()) / 350, 1))

    # Phase 5: Review loop
    review_prompt = ChatPromptTemplate.from_messages([
        ("human", RESEARCH_REVIEW_PROMPT),
    ])
    review_chain  = review_prompt | _no_think(llm).with_structured_output(ResearchReview)
    max_iters     = int(getattr(cfg, "research_max_iters", 2))

    for review_iter in range(1, max_iters + 1):
        report_tokens = math.ceil(len(report) / 4)
        review_overhead = 180
        if report_tokens + review_overhead > budget.ctx_size - 300:
            keep_chars = (budget.ctx_size - 300 - review_overhead) * 4
            review_report = "…[trimmed for review]\n" + report[-keep_chars:]
            log.warning("review_report_trimmed",
                        original_tokens=report_tokens, keep_chars=keep_chars)
        else:
            review_report = report
        try:
            review: ResearchReview = await asyncio.wait_for(
                review_chain.ainvoke({"report": review_report}),
                timeout=timeout,
            )
            log.info(
                "research_review_complete",
                verdict=review.verdict,
                iteration=review_iter,
            )

            if review.verdict == "pass":
                break

            issues_text = "\n".join(
                f"- [{i.type}] {i.detail}" for i in review.issues
            )
            suggestions_text = "\n".join(
                f"- {s}" for s in review.suggestions
            )
            revision_context = (
                f"## Review Feedback (Iteration {review_iter})\n"
                f"### Issues\n{issues_text}\n"
                f"### Suggestions\n{suggestions_text}\n"
                f"### Overall\n{review.overall_comment}\n\n"
                f"## Original Report\n{report}"
            )

            report_result = await asyncio.wait_for(
                report_chain.ainvoke({
                    "sources": _trim(revision_context, budget.source_char_budget, ellipsis=False),
                    "title": plan.title,
                }),
                timeout=writer_timeout,
            )
            report = (
                report_result.content
                if isinstance(report_result, AIMessage)
                else str(report_result)
            )
            log.info("research_report_revised", iteration=review_iter)

        except Exception as exc:
            log.warning(
                "research_review_failed",
                iteration=review_iter,
                exc=str(exc)[:200],
            )
            break

    # Export markdown
    safe_title = plan.title.replace(" ", "_")[:50]
    export_suffix = f"research_{safe_title}"
    try:
        export_result = export_markdown.invoke({
            "content": report,
            "filename": export_suffix,
        })
    except Exception as exc:
        log.warning("research_export_failed", exc=str(exc)[:200])

    try:
        pdf_result = await export_pdf.ainvoke({
            "content": report,
            "filename": export_suffix,
            "title": plan.title,
            "subtitle": "AI-Generated Research Report",
            "author": "Sage Research Agent",
        })
        if pdf_result.get("path"):
            report += f"\n*PDF saved to: `{pdf_result['path']}`*"
        elif pdf_result.get("error"):
            log.warning("export_pdf_soft_fail", error=pdf_result["error"])
    except Exception as exc:
        log.warning("export_pdf_failed", exc=str(exc)[:200])

    return {
        "response": report,
        "research_plan": plan.model_dump(),
        "research_sources": all_sources,
        "research_report": report,
    }
