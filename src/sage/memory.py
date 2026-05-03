"""Long-term semantic memory for Sage"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from langchain_openai import ChatOpenAI

from sage.config import get_settings
from sage.database import (
    delete_oldest_memories,
    get_all_memories,
    get_memory_count,
    insert_memory,
    search_memories_fts,
    update_memory_timestamp,
)
from sage.utils import estimate_tokens, strip_think_markers

log = structlog.get_logger(__name__)

_NOISE_PREFIXES: tuple[str, ...] = (
    "fact statement", "fact:", "note:", "memory:", "output:",
    "example:", "format:", "here", "based on",
)

_REJECT_CONTENT: tuple[str, ...] = (
    "unknown", "n/a", "not specified", "not mentioned", "not provided",
    "ai assistant", "language model", "llm", "sage", "assistant",
    "role:", "role is",
)

# --- Memory extraction prompt ---

_EXTRACT_PROMPT = """You extract long-term facts about a student from conversations.

CATEGORIES (pick the best fit):
- identity   : name, university, program, year, location
- study      : courses enrolled, exam dates, grades, topics struggling with, topics mastered
- preference : how they like explanations (examples vs theory), language, response style

QUALITY GATE — only extract if ALL are true:
1. Still useful two weeks from now (not session-specific questions)
2. Revealed by the student, not inferred by the assistant
3. Specific enough to act on ("struggles with pointers" not "finds CS hard")
4. Not already in Existing Memories below

DO NOT extract: greetings, thanks, generic questions, anything the assistant said, vague observations.

Existing Memories:
{existing_memories}

Conversation:
User: {user_message}
Assistant: {assistant_message}

Output one fact per line exactly like these examples:
[identity] Name is Alex
[study] Enrolled in Data Structures, semester 3
[preference] Prefers code examples before theory
[study] Struggles with dynamic programming concepts

IMPORTANT: The examples above are placeholders. Do not copy them unless the
conversation explicitly contains those facts.

Maximum 4 facts. If nothing qualifies, output exactly: NONE"""

_TITLE_PROMPT = """Generate a short, descriptive title (4-8 words) for this conversation based on the first user message. Output ONLY the title, nothing else.

User message: {message}"""


_COMPRESS_PROMPT = """Summarize this conversation history in 2-3 sentences. Focus on:
- Topics discussed and questions asked
- Key conclusions or answers provided
- Any decisions or preferences expressed

Conversation:
{history}

Summary:"""

async def extract_memories(
    user_message: str,
    assistant_message: str,
    utility_llm: ChatOpenAI,
    existing_memories: str = "",
) -> list[dict[str, str]]:
    """Extract structured facts from one conversation turn.

    Args:
        user_message:      The user's message text.
        assistant_message: The assistant's response text.
        utility_llm:       ChatOpenAI instance for the small model.
        existing_memories: Formatted string of already-known facts.

    Returns:
        List of dicts with 'content' and 'category' keys.
    """
    cfg = get_settings().memory
    if len(user_message.strip()) < 10 or len(assistant_message.strip()) < 20:
        return []

    prompt_text = _EXTRACT_PROMPT.format(
        existing_memories=existing_memories or "(none yet)",
        user_message=user_message[:1500],
        assistant_message=assistant_message[:1500],
    )

    try:
        result = await asyncio.wait_for(
            utility_llm.ainvoke(prompt_text),
            timeout=30.0,
        )
        raw_text = strip_think_markers(
            result.content if hasattr(result, "content") else str(result)
        ).strip()
    except Exception as exc:
        log.warning(
            "memory_extraction_failed",
            exc_type=type(exc).__name__,
            exc_msg=str(exc)[:200],
        )
        return []

    if not raw_text or raw_text.upper().strip() == "NONE":
        return []

    memories: list[dict[str, str]] = []
    valid_categories = {"identity", "study", "preference"}

    for line in raw_text.splitlines():
        line = line.strip().lstrip("-•*·").strip()
        if not line or line.upper() == "NONE":
            continue

        if not (line.startswith("[") and "]" in line):
            continue
        bracket_end = line.index("]")
        category = line[1:bracket_end].strip().lower().replace(" ", "_")
        content = line[bracket_end + 1:].strip().lstrip(":- ").strip()
        content_lower = content.lower()
        for noise in _NOISE_PREFIXES:
            if content_lower.startswith(noise):
                content = content[len(noise):].lstrip(": ").strip()
                content_lower = content.lower()
                break
        if len(content) < 8:
            continue
        if len(content) > 200:
            content = content[:200]
        if content_lower.replace("_", " ") == category.replace("_", " "):
            continue
        if any(p in content_lower for p in _REJECT_CONTENT):
            continue
        if category not in valid_categories:
            # Try to rescue obvious misclassifications before discarding.
            if any(w in content_lower for w in ("name", "university", "program", "year", "from")):
                category = "identity"
            elif any(w in content_lower for w in ("course", "exam", "grade", "struggle", "master", "semester")):
                category = "study"
            elif any(w in content_lower for w in ("prefer", "like", "want", "style", "example", "theory")):
                category = "preference"
            else:
                continue
        memories.append({"content": content, "category": category})

    return memories[:4]


async def deduplicate_and_store(
    new_memories: list[dict[str, str]],
) -> int:
    """Deduplicate new memories via FTS and persist unique facts.

    Returns:
        Count of genuinely new memories stored.
    """
    cfg = get_settings().memory
    stored_count = 0

    for new_mem in new_memories:
        content = new_mem["content"]
        category = new_mem["category"]
        content_lower = content.lower().strip()
        # FTS-based duplicate check: high-scoring match → skip.
        candidates = await search_memories_fts(content, k=3)
        is_duplicate = False
        for mem in candidates:
            if mem["content"].lower().strip() == content_lower:
                await update_memory_timestamp(mem["id"])
                is_duplicate = True
                break
            if mem["score"] >= cfg.dedup_similarity:
                is_duplicate = True
                break
        if is_duplicate:
            continue

        mem_id = await insert_memory(
            content=content,
            category=category,
            confidence=1.0,
        )
        stored_count += 1
        log.info(
            "memory_stored",
            memory_id=mem_id,
            category=category,
            content_preview=content[:80],
        )

    # Enforce max memories limit by pruning oldest.
    total = await get_memory_count()
    if total > cfg.max_memories:
        deleted = await delete_oldest_memories(cfg.max_memories)
        if deleted:
            log.info("memory_pruned", deleted=deleted, remaining=cfg.max_memories)

    return stored_count

async def search_memories(
    query: str,
    k: int | None = None,
) -> list[dict[str, Any]]:
    """Retrieve memories relevant to the query via FTS.

    Returns:
        List of memory dicts sorted by relevance, each containing at
        minimum: id, content, category, score.
    """
    cfg = get_settings().memory
    if k is None:
        k = cfg.search_top_k
    results = await search_memories_fts(query, k=k, min_score=cfg.search_min_score)
    if results:
        return results

    all_memories = await get_all_memories()
    return [
        {
            "id": m["id"],
            "content": m["content"],
            "category": m["category"],
            "score": 0.3,
        }
        for m in all_memories[:k]
    ]

async def inject_memory_context(
    query: str,
    max_facts: int = 10,
) -> str:
    """Build a memory context block for injection into the system prompt.

    Returns a formatted markdown string, or an empty string when no
    relevant memories are found.
    """
    memories = await search_memories(query)
    if not memories:
        return ""

    memories = memories[:max_facts]
    lines: list[str] = []
    for mem in memories:
        cat = mem["category"].replace("_", " ").title()
        lines.append(f"- [{cat}] {mem['content']}")

    return (
        "## Student Memory\n"
        "Things I remember about this student from past conversations:\n"
        + "\n".join(lines)
    )

async def compress_history(
    messages: list[Any],
    utility_llm: ChatOpenAI,
    max_tokens: int | None = None,
) -> list[Any]:
    """Sliding-window + summarisation for context-window management.

    Summarises messages older than the most recent ``keep_count`` turns
    into a single SystemMessage.  The original messages are returned
    unchanged when within the token budget.

    Args:
        messages:    LangChain message objects (HumanMessage/AIMessage).
        utility_llm: Small model for summarisation.
        max_tokens:  Token budget (defaults to config).

    Returns:
        Compressed message list fitting within the token budget.
    """
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    cfg = get_settings().memory
    if max_tokens is None:
        max_tokens = cfg.max_history_tokens

    total_tokens = sum(
        estimate_tokens(
            m.content
            if hasattr(m, "content") and isinstance(m.content, str)
            else str(m)
        )
        for m in messages
    )

    if total_tokens <= max_tokens:
        return messages

    keep_count = min(6, len(messages))
    recent = messages[-keep_count:]
    older = messages[:-keep_count]

    if not older:
        return recent

    history_lines: list[str] = []
    for msg in older:
        role = "User" if isinstance(msg, HumanMessage) else "Assistant"
        content = msg.content if hasattr(msg, "content") else str(msg)
        if isinstance(content, str):
            history_lines.append(f"{role}: {content[:500]}")

    history_text = "\n".join(history_lines)

    try:
        result = await asyncio.wait_for(
            utility_llm.ainvoke(
                _COMPRESS_PROMPT.format(history=history_text[:3000])
            ),
            timeout=20.0,
        )
        summary = strip_think_markers(
            result.content if hasattr(result, "content") else str(result)
        ).strip()
    except Exception as exc:
        log.warning(
            "history_compression_failed",
            exc_type=type(exc).__name__,
            exc_msg=str(exc)[:200],
        )
        return recent

    if not summary:
        return recent

    summary_msg = SystemMessage(
        content=f"Previous conversation summary: {summary}"
    )
    return [summary_msg, *recent]

async def generate_title(
    message: str,
    utility_llm: ChatOpenAI,
) -> str:
    """Generate a conversation title from the first user message."""
    try:
        result = await asyncio.wait_for(
            utility_llm.ainvoke(
                _TITLE_PROMPT.format(message=message[:500])
            ),
            timeout=15.0,
        )
        title = strip_think_markers(
            result.content if hasattr(result, "content") else str(result)
        ).strip().strip('"').strip("'")
        title = title.split("\n")[0].strip()
        words = title.split()
        if len(words) > 8:
            title = " ".join(words[:8]) + "…"
        return title or _fallback_title(message)
    except Exception:
        return _fallback_title(message)


def _fallback_title(text: str) -> str:
    words = text.split()
    title = " ".join(words[:6])
    if len(words) > 6:
        title += "…"
    return title

async def post_turn_memory_hook(
    user_message: str,
    assistant_message: str,
    utility_llm: ChatOpenAI,
) -> None:
    """Background hook called after each conversation turn.

    Args:
        user_message:      The user's message text.
        assistant_message: The assistant's response text.
        utility_llm:       Small model for extraction.
    """
    try:
        all_mems = await get_all_memories()
        existing_text = (
            "\n".join(f"- [{m['category']}] {m['content']}" for m in all_mems[:20])
            if all_mems else ""
        )

        new_memories = await extract_memories(
            user_message=user_message,
            assistant_message=assistant_message,
            utility_llm=utility_llm,
            existing_memories=existing_text,
        )

        if new_memories:
            stored = await deduplicate_and_store(new_memories)
            log.info(
                "memory_hook_complete",
                extracted=len(new_memories),
                stored=stored,
            )
        else:
            log.debug("memory_hook_no_facts_extracted")

    except Exception as exc:
        log.error(
            "memory_hook_error",
            exc_type=type(exc).__name__,
            exc_msg=str(exc)[:200],
        )