"""
Roadmap (study planner) agent node for Sage.

Two-phase LLM pipeline with no retrieval dependency:
  1. Input analysis: extract subject, timeline, scope, topics,
     prerequisites, and difficulty from the student query via
     `ROADMAP_ANALYSIS_PROMPT` + structured output.
  2. Schedule generation: produce a day-by-day study plan via
     `ROADMAP_SCHEDULE_PROMPT` with spaced repetition, prerequisite
     ordering, and progress checkpoints.

Output is formatted as a markdown table + timeline summary.
"""

from __future__ import annotations

import asyncio
import re
import warnings
from typing import Any

import structlog
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field, field_validator, model_validator

from sage.agents.state import AgentState
from sage.config import get_settings
from sage.prompts import (
    ROADMAP_ANALYSIS_PROMPT,
    ROADMAP_SCHEDULE_PROMPT,
)
from sage.utils import ainvoke_structured_with_fallback

PLANNER_SYSTEM_PROMPT: str = (
    "You are a precise academic planning assistant. Your job is to extract structured details and generate study plans. "
    "Respond ONLY with raw, valid JSON matching the requested schema. Never output any greeting, introductory text, "
    "conversational filler, explanation, or markdown fences. Output clean, parseable JSON."
)

log = structlog.get_logger(__name__)

warnings.filterwarnings("ignore", category=UserWarning, module="pydantic")

_MAX_RETRIES: int = 2
_SCHEDULE_TIMEOUT_MULTIPLIER: float = 2.0
_SCHEDULE_TEMPERATURE: float = 0.1


def _escape_md(text: str) -> str:
    """Escape pipe characters and angle brackets to prevent markdown injection."""
    return text.replace("|", "\\|").replace("<", "\\<").replace(">", "\\>")


def _clean(text: str) -> str:
    """Flatten and escape a string for safe use inside a markdown table cell."""
    flat = re.sub(r"\s+", " ", text.replace("\n", " ")).strip()
    flat = flat.replace("| Day | Type | Topics | Hours | Activities |", "")
    flat = flat.replace("| --- | --- | --- | --- | --- |", "")
    return _escape_md(flat.strip())


def _normalize_schedule(analysis: RoadmapAnalysis, schedule: RoadmapSchedule) -> RoadmapSchedule:
    timeline_days = max(1, analysis.timeline_days)
    seen: set[int] = set()
    cleaned: list[ScheduleDay] = []

    for day in sorted(schedule.schedule, key=lambda d: d.day):
        if day.day in seen:
            continue
        seen.add(day.day)
        topics = [_clean(t) for t in day.topics if _clean(t)] or ["Review and consolidation"]
        activities = [_clean(a) for a in day.activities if _clean(a)] or ["Review key concepts"]
        checkpoint_raw: str | None
        if isinstance(day.checkpoint, dict):
            checkpoint_raw = str(
                day.checkpoint.get("milestone") or day.checkpoint.get("checkpoint") or day.checkpoint.get("label") or ""
            )
        elif day.checkpoint is None:
            checkpoint_raw = None
        else:
            checkpoint_raw = str(day.checkpoint)
        cleaned.append(
            ScheduleDay(
                day=day.day,
                session_type=_clean(day.session_type) or "study",
                topics=topics,
                hours=max(0.5, float(day.hours)),
                activities=activities,
                knowledge_unit_refs=[r.strip() for r in day.knowledge_unit_refs if r.strip()],
                checkpoint=_clean(checkpoint_raw) if checkpoint_raw else None,
            )
        )

    if not cleaned:
        return _build_fallback_schedule(analysis)

    cleaned = cleaned[:timeline_days]
    while len(cleaned) < timeline_days:
        last = cleaned[-1]
        next_day = len(cleaned) + 1
        cleaned.append(
            ScheduleDay(
                day=next_day,
                session_type="revision" if next_day >= timeline_days - 1 else "study",
                topics=last.topics,
                hours=analysis.daily_hours_available,
                activities=["Consolidation and targeted practice"],
                knowledge_unit_refs=last.knowledge_unit_refs,
                checkpoint=None,
            )
        )

    seen_cp: set[int] = set()
    norm_cp: list[Checkpoint] = []
    for cp in schedule.checkpoints:
        d = min(max(1, cp.after_day), timeline_days)
        if d in seen_cp:
            continue
        seen_cp.add(d)
        norm_cp.append(Checkpoint(after_day=d, milestone=_clean(cp.milestone)))
    if not norm_cp:
        norm_cp.append(Checkpoint(after_day=timeline_days, milestone="Complete the planned scope confidently"))

    qs = [_clean(q) for q in schedule.self_assessment_questions if _clean(q)][:3]
    while len(qs) < 3:
        qs.append(f"What did you improve most in {analysis.subject} during this plan?")

    return RoadmapSchedule(schedule=cleaned, checkpoints=norm_cp, self_assessment_questions=qs)


class TopicInfo(BaseModel):
    name: str
    difficulty: int = Field(default=2, ge=1, le=3)
    estimated_hours: float = Field(default=2.0, ge=0.5)
    prerequisites: list[str] = Field(default_factory=list)


class RoadmapAnalysis(BaseModel):
    subject: str = "Study Plan"
    timeline_days: int = Field(default=14, ge=1)
    scope: str = "full course"
    daily_hours_available: float = Field(default=3.0, ge=0.5)
    known_topics: list[str] = Field(default_factory=list)
    weak_topics: list[str] = Field(default_factory=list)
    topics: list[TopicInfo] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _coerce_aliases(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        out = dict(data)
        out.setdefault("subject", out.get("topic") or out.get("course") or "Study Plan")
        out.setdefault("scope", out.get("goal") or out.get("exam_type") or "full course")
        out.setdefault(
            "timeline_days",
            out.get("days") or out.get("timeline") or out.get("duration_days") or 14,
        )
        out.setdefault(
            "daily_hours_available",
            out.get("daily_hours") or out.get("hours_per_day") or 3.0,
        )
        if "topics" not in out:
            out["topics"] = out.get("study_topics") or out.get("modules") or []
        return out


class ScheduleDay(BaseModel):
    day: int = 1
    session_type: str = "study"
    topics: list[str] = Field(default_factory=list)
    hours: float = 1.0
    activities: list[str] = Field(default_factory=list)
    knowledge_unit_refs: list[str] = Field(default_factory=list)
    checkpoint: str | dict[str, Any] | None = None


class Checkpoint(BaseModel):
    after_day: int = 1
    milestone: str = "Progress checkpoint"


class RoadmapSchedule(BaseModel):
    schedule: list[ScheduleDay] = Field(default_factory=list)
    checkpoints: list[Checkpoint] = Field(default_factory=list)
    self_assessment_questions: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _coerce_aliases(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        out = dict(data)
        if "schedule" not in out:
            for alias in ("plan", "daily_plan", "daily_schedule", "days"):
                maybe = out.get(alias)
                if isinstance(maybe, list):
                    out["schedule"] = maybe
                    break
        if "checkpoints" not in out:
            maybe_cp = out.get("checkpoint") or out.get("milestones")
            if isinstance(maybe_cp, list):
                out["checkpoints"] = maybe_cp
        if "self_assessment_questions" not in out:
            for alias in ("self_assessment", "assessment_questions", "questions"):
                maybe_q = out.get(alias)
                if isinstance(maybe_q, list):
                    out["self_assessment_questions"] = maybe_q
                    break
        return out

    @field_validator("checkpoints", mode="before")
    @classmethod
    def _coerce_checkpoints(cls, v: Any) -> Any:
        if not isinstance(v, list):
            return v
        out: list[dict[str, Any]] = []
        for idx, item in enumerate(v, start=1):
            if isinstance(item, str):
                out.append({"after_day": idx, "milestone": item})
            elif isinstance(item, dict):
                cp = dict(item)
                cp.setdefault("after_day", idx)
                cp.setdefault("milestone", "Progress checkpoint")
                out.append(cp)
            elif hasattr(item, "model_dump"):
                out.append(item.model_dump())
            elif hasattr(item, "dict"):
                out.append(item.dict())
        return out


_SCOPE_PHRASE: dict[str, str] = {
    "midterm": "your midterm",
    "final": "your final exam",
    "full course": "the full course",
}


def _build_intro(analysis: RoadmapAnalysis, schedule: RoadmapSchedule) -> str:
    scope_phrase = _SCOPE_PHRASE.get(analysis.scope.lower(), f'"{analysis.scope}"')
    total_hours = sum(d.hours for d in schedule.schedule)
    study_days = sum(1 for d in schedule.schedule if d.session_type == "study")
    revision_days = sum(1 for d in schedule.schedule if d.session_type == "revision")
    n_topics = len(analysis.topics)

    topic_clause = (
        f" The plan covers **{n_topics} topic{'s' if n_topics != 1 else ''}**, "
        "sequenced by prerequisites so you always build on solid ground."
        if n_topics
        else ""
    )
    weak_clause = (
        f" I've allocated extra time to your weaker areas "
        f"({', '.join(f'**{t}**' for t in analysis.weak_topics)}) "
        "so they get the attention they deserve."
        if analysis.weak_topics
        else ""
    )
    known_clause = (
        f" Topics you already know "
        f"({', '.join(f'**{t}**' for t in analysis.known_topics)}) "
        "are kept as quick refreshers — no wasted effort."
        if analysis.known_topics
        else ""
    )

    return (
        f"Here's your personalised **{analysis.timeline_days}-day study roadmap** "
        f"for **{analysis.subject}**, built to get you ready for {scope_phrase}. "
        f"You'll study roughly **{analysis.daily_hours_available}h per day** — "
        f"**{total_hours:.0f}h total** across "
        f"**{study_days} study day{'s' if study_days != 1 else ''}** and "
        f"**{revision_days} revision day{'s' if revision_days != 1 else ''}**."
        f"{topic_clause}{weak_clause}{known_clause} "
        "Every session opens with a short spaced-repetition recap, and the final "
        "two days are reserved for full revision and practice tests. Let's get into it! 🚀"
    )


_SESSION_ICON: dict[str, str] = {
    "study": "📖",
    "review": "🔁",
    "revision": "🧠",
    "assessment": "📝",
}
_DIFFICULTY_LABEL: dict[int, str] = {1: "Easy", 2: "Medium", 3: "Hard"}


def _format_schedule_markdown(
    analysis: RoadmapAnalysis,
    schedule: RoadmapSchedule,
) -> str:
    cp_map: dict[int, str] = {cp.after_day: cp.milestone for cp in schedule.checkpoints}
    total_hours = sum(d.hours for d in schedule.schedule)
    study_days = sum(1 for d in schedule.schedule if d.session_type == "study")
    revision_days = sum(1 for d in schedule.schedule if d.session_type == "revision")

    lines: list[str] = []
    lines += [_build_intro(analysis, schedule), ""]

    lines += [
        f"# 🗺️ Study Roadmap — {analysis.subject}",
        "",
        "| 📅 Timeline | 🎯 Scope | ⏱ Daily Budget | 📊 Total Hours |",
        "| :---: | :---: | :---: | :---: |",
        (
            f"| **{analysis.timeline_days} days** "
            f"| {analysis.scope} "
            f"| **{analysis.daily_hours_available}h / day** "
            f"| **{total_hours:.1f}h** |"
        ),
        "",
    ]

    if analysis.known_topics:
        lines.append("✅ **Already solid:** " + "  ".join(f"`{t}`" for t in analysis.known_topics))
    if analysis.weak_topics:
        lines.append("⚠️  **Priority focus:** " + "  ".join(f"`{t}`" for t in analysis.weak_topics))
    if analysis.known_topics or analysis.weak_topics:
        lines.append("")

    if analysis.topics:
        lines += [
            "<details>",
            "<summary><strong>📚 Topic Overview &amp; Difficulty</strong></summary>",
            "",
            "| Topic | Difficulty | Est. Hours | Prerequisites |",
            "| --- | :---: | :---: | --- |",
        ]
        for t in analysis.topics:
            prereqs = ", ".join(t.prerequisites) if t.prerequisites else "—"
            lines.append(
                f"| {t.name} | {'⭐' * t.difficulty} {_DIFFICULTY_LABEL.get(t.difficulty, '')} "
                f"| {t.estimated_hours}h | {prereqs} |"
            )
        lines += ["", "</details>", ""]

    if analysis.timeline_days > 28:
        weekly: dict[int, list[ScheduleDay]] = {}
        for day in schedule.schedule:
            weekly.setdefault(((day.day - 1) // 7) + 1, []).append(day)

        lines += [
            "## 📅 Weekly Schedule",
            "",
            "| Week | Days | Focus Topics | Total Hours | Key Activities |",
            "| :---: | :---: | --- | :---: | --- |",
        ]
        for week, entries in sorted(weekly.items()):
            day_range = f"{entries[0].day}–{entries[-1].day}"
            topics_flat = list(dict.fromkeys(_clean(t) for e in entries for t in e.topics))
            acts_flat = list(dict.fromkeys(_clean(a) for e in entries for a in e.activities))
            total = sum(e.hours for e in entries)
            lines.append(
                f"| {week} | {day_range} | {', '.join(topics_flat[:3]) or 'Revision'} "
                f"| **{total:.1f}h** | {'; '.join(acts_flat[:3]) or 'Weekly consolidation'} |"
            )
    else:
        lines += [
            "## 📅 Daily Schedule",
            "",
            "| Day | Mode | Topics | Hours | Activities |",
            "| :---: | --- | --- | :---: | --- |",
        ]
        for day in schedule.schedule:
            icon = _SESSION_ICON.get(day.session_type, "📌")
            mode = f"{icon} **{day.session_type.capitalize()}**"
            topics = "<br>".join(f"• {t}" for t in day.topics)
            activities = "<br>".join(f"▸ {a}" for a in day.activities) if day.activities else "—"
            lines.append(f"| {day.day} | {mode} | {topics} | **{day.hours}h** | {activities} |")
            if day.day in cp_map:
                lines.append(f"| | 🏁 | ***Checkpoint*** | | *{cp_map[day.day]}* |")

    if schedule.checkpoints:
        lines += ["---", "## 🏁 Progress Checkpoints"]
        for cp in schedule.checkpoints:
            lines.append(f"- **After Day {cp.after_day}:** {_escape_md(cp.milestone)}")

    if schedule.self_assessment_questions:
        lines += [
            "---",
            "## 📝 Self-Assessment",
            "> Answer these *before* your final revision day to spot remaining gaps.",
        ]
        for i, q in enumerate(schedule.self_assessment_questions, 1):
            lines.append(f"- **Q{i}.** {_escape_md(q)}")

    lines += [
        "---",
        f"📊 **{study_days}** study days · **{revision_days}** revision days · **{total_hours:.1f}h** total commitment",
    ]

    return "\n".join(lines)


def _format_knowledge_units(kus: list[dict]) -> str:
    if not kus:
        return "None available."
    return "\n".join(f"[{ku.get('id', 'KU?')}] {ku.get('claim', ku.get('content', ''))}" for ku in kus)


def _build_fallback_schedule(analysis: RoadmapAnalysis) -> RoadmapSchedule:
    """Deterministic minimal schedule when LLM generation fails."""
    if not analysis.topics:
        log.error("analysis_has_no_topics", subject=analysis.subject)
        return RoadmapSchedule(
            schedule=[
                ScheduleDay(
                    day=1,
                    session_type="study",
                    topics=[analysis.subject],
                    hours=analysis.daily_hours_available,
                    activities=["Review learning materials"],
                    knowledge_unit_refs=[],
                    checkpoint=f"Understand {analysis.subject}",
                )
            ],
            checkpoints=[Checkpoint(after_day=1, milestone="Initial learning")],
            self_assessment_questions=[f"What are the main concepts in {analysis.subject}?"],
        )

    days_available = analysis.timeline_days
    topics = sorted(analysis.topics, key=lambda t: len(t.prerequisites))
    daily_hours = analysis.daily_hours_available
    topics_per_day = max(1, len(topics) // max(1, days_available - 1))

    schedule_days: list[ScheduleDay] = []
    for day in range(1, days_available):
        day_topics = topics[(day - 1) * topics_per_day : day * topics_per_day]
        schedule_days.append(
            ScheduleDay(
                day=day,
                session_type="study",
                topics=[t.name for t in day_topics] if day_topics else ["Review & Practice"],
                hours=daily_hours,
                activities=(
                    [
                        "Rapid recap (20 min) of previous material",
                        *[f"Concept study: {t.name}" for t in day_topics],
                        *[f"Practice drill: {t.name}" for t in day_topics],
                    ]
                    if day_topics
                    else ["Rapid recap (20 min)", "General review", "Targeted practice"]
                ),
                knowledge_unit_refs=[],
                checkpoint=(
                    f"Understand {', '.join(' '.join(t.name.split()[:2]) for t in day_topics)}" if day_topics else None
                ),
            )
        )

    schedule_days.append(
        ScheduleDay(
            day=days_available,
            session_type="revision",
            topics=["Full scope review"],
            hours=daily_hours,
            activities=[
                "Comprehensive revision of all covered topics",
                "Timed practice questions and error-log review",
                "Final weak-area reinforcement",
            ],
            knowledge_unit_refs=[],
            checkpoint="Ready for assessment",
        )
    )

    return RoadmapSchedule(
        schedule=schedule_days,
        checkpoints=[Checkpoint(after_day=days_available, milestone="Study plan complete")],
        self_assessment_questions=[
            f"What are the key concepts in {analysis.subject}?",
            f"How do the topics in {analysis.subject} relate to each other?",
            f"Where would you apply {analysis.subject} in practice?",
        ],
    )


async def planner_node(state: AgentState, llm: ChatOpenAI, *, util_llm: ChatOpenAI | None = None) -> dict[str, Any]:
    """Generate a personalised study roadmap.

    Phase 1 - Analyse the student request (structured output).
    Phase 2 - Generate day-by-day schedule (structured output).
    Fallback - Deterministic schedule from analysis if LLM fails.

    Args:
        util_llm: Optional smaller LLM for the analysis step (CPU-only offload).
    """
    cfg = get_settings().agent
    query: str = state.get("query", "")
    kus: list[dict] = state.get("knowledge_units", [])
    student_memory: str = state.get("student_memory", "No prior context available.")
    ku_text = _format_knowledge_units(kus)
    _no_think = {
        "extra_body": {
            "chat_template_kwargs": {"enable_thinking": False},
            "thinking_budget": 0,
            "reasoning_budget": 0,
        }
    }
    llm = llm.bind(**_no_think)

    # Analysis
    analysis_prompt = ChatPromptTemplate.from_messages(
        [
            ("system", PLANNER_SYSTEM_PROMPT),
            ("human", ROADMAP_ANALYSIS_PROMPT),
        ]
    )

    analysis: RoadmapAnalysis | None = None
    last_exc: Exception | None = None
    _analysis_llm = util_llm.bind(**_no_think) if util_llm else llm
    for attempt in range(1, _MAX_RETRIES + 1):
        current_llm = llm if (attempt > 1) else _analysis_llm
        try:
            analysis = await ainvoke_structured_with_fallback(
                prompt=analysis_prompt,
                llm=current_llm,
                schema=RoadmapAnalysis,
                payload={"query": query, "student_memory": student_memory},
                timeout_s=cfg.llm_timeout,
                logger=log,
                event_prefix="roadmap_analysis",
            )  # type: ignore
            log.info(
                "roadmap_analysis_complete",
                subject=analysis.subject,
                timeline=analysis.timeline_days,
                topics=len(analysis.topics),
                attempt=attempt,
            )
            break
        except asyncio.CancelledError:
            log.error("planner_node_analysis_cancelled")
            raise
        except Exception as exc:
            last_exc = exc
            log.warning(
                "roadmap_analysis_retry",
                attempt=attempt,
                exc_type=type(exc).__name__,
                exc_msg=str(exc),
            )

    if analysis is None:
        log.error("roadmap_analysis_failed_all_retries", exc=str(last_exc))
        from langchain_core.messages import AIMessage

        res_text = (
            "I was unable to analyse your study plan request. "
            "Please try again with more details about the subject, timeline, and scope."
        )
        return {
            "messages": [AIMessage(content=res_text)],
            "response": res_text,
        }

    # Schedule
    analysis_json = analysis.model_dump_json()
    schedule_prompt = ChatPromptTemplate.from_messages(
        [
            ("system", PLANNER_SYSTEM_PROMPT),
            ("human", ROADMAP_SCHEDULE_PROMPT),
        ]
    )

    schedule: RoadmapSchedule | None = None
    schedule_timeout = cfg.llm_timeout * _SCHEDULE_TIMEOUT_MULTIPLIER
    last_exc = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            schedule_llm = llm.bind(
                temperature=_SCHEDULE_TEMPERATURE,
                **_no_think,
            )
            schedule = await ainvoke_structured_with_fallback(
                prompt=schedule_prompt,
                llm=schedule_llm,
                schema=RoadmapSchedule,
                payload={"analysis": analysis_json, "knowledge_units": ku_text},
                timeout_s=schedule_timeout,
                logger=log,
                event_prefix="roadmap_schedule",
            )  # type: ignore
            log.info(
                "roadmap_schedule_complete",
                days=len(schedule.schedule),
                checkpoints=len(schedule.checkpoints),
                attempt=attempt,
            )
            break
        except asyncio.CancelledError:
            log.error("planner_node_schedule_cancelled")
            raise
        except TimeoutError as exc:
            last_exc = exc
            log.warning("roadmap_schedule_timeout", attempt=attempt, timeout_s=schedule_timeout)
        except Exception as exc:
            last_exc = exc
            log.warning(
                "roadmap_schedule_retry",
                attempt=attempt,
                exc_type=type(exc).__name__,
                exc_msg=str(exc),
            )

    if schedule is None:
        log.error("roadmap_schedule_failed_all_retries", exc=str(last_exc))
        schedule = _build_fallback_schedule(analysis)
        log.info("roadmap_schedule_fallback_generated", days=len(schedule.schedule))

    schedule = _normalize_schedule(analysis, schedule)
    response_text = _format_schedule_markdown(analysis, schedule)

    from langchain_core.messages import AIMessage

    return {
        "messages": [AIMessage(content=response_text)],
        "response": response_text,
        "tool_calls": [
            {
                "tool": "roadmap_generation",
                "subject": analysis.subject,
                "days": len(schedule.schedule),
            }
        ],
    }
