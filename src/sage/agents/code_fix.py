"""
Code fix agent subgraph for Sage.

Three-node pipeline:
  1. Diagnosis: Structured analysis of the bug (language, error type, root cause, fix strategy).
  2. Fix loop: LLM generates a fix, sandbox verifies, retry on error.
  3. Explanation: Educational walkthrough with diff, best practice, and citations.
"""

from __future__ import annotations

import ast
import asyncio
import re
import time
from typing import Any

import structlog
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from sage.agents.state import AgentState
from sage.config import get_settings
from sage.prompts import (
    CODE_FIX_DIAGNOSIS_PROMPT,
    CODE_FIX_EXPLANATION_PROMPT,
    CODE_FIX_SYSTEM_PROMPT,
)
from sage.tools.sandbox import execute_python
from sage.utils import (
    ainvoke_structured_with_fallback,
    close_unbalanced_fenced_blocks,
    extract_fenced_block,
    strip_think_markers,
)

log = structlog.get_logger(__name__)

_MAX_RETRIES: int = 3

# Frameworks that cannot run in the subprocess sandbox.
_UNSANDBOXABLE_MODULES: frozenset[str] = frozenset(
    {
        "flask",
        "django",
        "fastapi",
        "uvicorn",
        "gunicorn",
        "tornado",
        "aiohttp",
        "starlette",
        "quart",
        "celery",
        "redis",
        "sqlalchemy",
    }
)

_NON_PYTHON_PATTERNS: list[tuple[str, str]] = [
    (r"#include\s*<", "C/C++"),
    (r"public\s+static\s+void\s+main", "Java"),
    (r"\bfunction\s+\w+\s*\(", "JavaScript"),
    (r"\bconst\s+\w+\s*=\s*\(.*\)\s*=>", "JavaScript/TypeScript"),
    (r"System\.out\.println", "Java"),
    (r"console\.log\(", "JavaScript"),
    (r"\bfn\s+\w+\s*\(", "Rust"),
    (r"\bpackage\s+main", "Go"),
]


class Diagnosis(BaseModel):
    language: str = "python"
    framework: str | None = None
    error_type: str = Field(description="syntax | runtime | logic | type | import | timeout")
    error_message: str = ""
    root_cause: str = ""
    affected_lines: list[int] = Field(default_factory=list)
    fix_strategy: str = ""
    alternative_strategies: list[str] = Field(default_factory=list)
    confidence: str = "medium"


def _strip_code_fences(text: str) -> str:
    """Extract executable code from noisy LLM output."""
    cleaned = strip_think_markers(text)
    fenced = extract_fenced_block(cleaned, preferred_languages={"python", "py"})
    candidate = fenced if fenced is not None else cleaned
    candidate = re.sub(r"^```\w*\n", "", candidate)
    candidate = re.sub(r"\n```\s*$", "", candidate)
    return candidate.strip()


def _fenced_block(text: str, language: str = "") -> str:
    """Wrap text in a markdown fence that cannot be broken by embedded backticks."""
    body = text.rstrip("\n")
    max_backtick_run = max((len(m.group(0)) for m in re.finditer(r"`+", body)), default=0)
    fence = "`" * max(3, max_backtick_run + 1)
    opener = f"{fence}{language}".rstrip() + "\n"
    return f"{opener}{body}\n{fence}"


def _detect_non_python(code: str) -> str | None:
    """Return the detected language name if code appears non-Python."""
    for pattern, lang in _NON_PYTHON_PATTERNS:
        if re.search(pattern, code):
            return lang
    return None


def _detect_framework_imports(code: str) -> str | None:
    """Use AST to detect unsandboxable framework imports."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in _UNSANDBOXABLE_MODULES:
                    return root
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in _UNSANDBOXABLE_MODULES:
                return root
    return None


def _format_knowledge_units(kus: list[dict]) -> str:
    if not kus:
        return "None available."
    return "\n".join(f"[{ku.get('id', 'KU?')}] {ku.get('claim', ku.get('content', ''))}" for ku in kus)


def _extract_text(result: Any) -> str:
    """Safely extract string content from an LLM result."""
    return result.content if isinstance(result, AIMessage) else str(result)


async def code_fix_node(state: AgentState, llm: ChatOpenAI) -> dict[str, Any]:
    """Diagnose, fix, verify, and explain a code bug.

    The pipeline handles three scenarios:
      1. Pure Python code: diagnose → fix → sandbox verify → explain
      2. Framework code: diagnose → fix (no sandbox) → explain
      3. Non-Python code: general LLM analysis (no sandbox)
    """
    cfg = get_settings().agent
    query: str = state.get("query", "").strip()
    kus: list[dict] = state.get("knowledge_units", [])
    ku_text = _format_knowledge_units(kus)
    t_start = time.perf_counter()

    if not query:
        return {
            "response": ("I was unable to diagnose the code issue. Please provide the error message and relevant code.")
        }

    _active_ctx = get_settings().llm.active_context_size
    _max_out = get_settings().llm.max_tokens
    _MAX_QUERY_CHARS: int = (
        max(1_000, min(8_000, (_active_ctx - min(_max_out, _active_ctx // 2) - 600) * 4)) if _active_ctx > 0 else 2_000
    )
    if len(query) > _MAX_QUERY_CHARS:
        log.warning("code_fix_input_truncated", original_len=len(query), limit=_MAX_QUERY_CHARS, ctx_size=_active_ctx)
        query = query[:_MAX_QUERY_CHARS] + "\n# ... (truncated — submit a shorter excerpt)"

    non_python = _detect_non_python(query)
    if non_python:
        log.info("code_fix_non_python_detected", language=non_python)
        try:
            result = await asyncio.wait_for(
                llm.ainvoke(
                    [
                        SystemMessage(content=CODE_FIX_SYSTEM_PROMPT),
                        HumanMessage(
                            content=(
                                f"This appears to be {non_python} code. "
                                f"Analyse it and identify any bugs or issues.\n\n"
                                f"{query}"
                            )
                        ),
                    ]
                ),
                timeout=cfg.llm_timeout,
            )
            text = _extract_text(result)
        except TimeoutError:
            text = f"Analysis timed out for {non_python} code."
        except Exception as exc:
            log.error("code_fix_non_python_failed", exc=str(exc)[:200])
            text = f"This appears to be {non_python} code. I cannot execute it in the sandbox."
        return {"response": text}

    # Diagnosis
    diag_prompt = ChatPromptTemplate.from_messages(
        [
            ("system", CODE_FIX_SYSTEM_PROMPT + "\n\n" + CODE_FIX_DIAGNOSIS_PROMPT),
            ("human", "Diagnose the code issue above."),
        ]
    )

    diagnosis: Diagnosis | None = None

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            diagnosis = await ainvoke_structured_with_fallback(
                prompt=diag_prompt,
                llm=llm,
                schema=Diagnosis,
                payload={"code": query, "error": "See code and user message above"},
                timeout_s=cfg.llm_timeout,
                logger=log,
                event_prefix="code_fix_diagnosis",
            )
            log.info(
                "code_fix_diagnosis",
                error_type=diagnosis.error_type,
                root_cause=diagnosis.root_cause[:100],
                confidence=diagnosis.confidence,
                attempt=attempt,
            )
            break
        except TimeoutError:
            log.warning("code_fix_diagnosis_timeout", attempt=attempt)
        except Exception as exc:
            log.warning("code_fix_diagnosis_retry", attempt=attempt, exc=str(exc)[:200])

    if diagnosis is None:
        return {
            "response": ("I was unable to diagnose the code issue. Please provide the error message and relevant code.")
        }

    # Framework detection
    framework: str | None = _detect_framework_imports(query) or diagnosis.framework
    skip_sandbox: bool = bool(framework and framework.lower() in _UNSANDBOXABLE_MODULES)
    if skip_sandbox:
        log.info("code_fix_framework_skip_sandbox", framework=framework)

    # Fix loop
    fix_prompt = ChatPromptTemplate.from_messages(
        [
            ("system", CODE_FIX_SYSTEM_PROMPT),
            (
                "human",
                "Fix this code based on the diagnosis and any sandbox feedback below.\n\n"
                "## Diagnosis (JSON)\n{diagnosis}\n\n"
                "## Affected Lines\n{affected_lines}\n\n"
                "## Previous Sandbox Error\n{sandbox_error}\n\n"
                "## Code to Fix\n```python\n{code}\n```\n\n"
                "Rules:\n"
                "- If a sandbox error is provided, fix the issue shown in that error FIRST.\n"
                "- Change ONLY the lines required to correct the error.\n"
                "- Return ONLY the complete corrected Python code. No explanation.",
            ),
        ]
    )

    current_code: str = query
    fixed_code: str = ""
    execution_result: str = ""
    fix_succeeded: bool = False
    last_sandbox_error: str = "No prior error — apply the diagnosis fix strategy."
    affected_str: str = (
        ", ".join(str(ln) for ln in diagnosis.affected_lines)
        if diagnosis.affected_lines
        else "(see root_cause in diagnosis)"
    )

    for attempt in range(1, _MAX_RETRIES + 1):
        # Generate fix.
        try:
            fix_result = await asyncio.wait_for(
                (fix_prompt | llm).ainvoke(
                    {
                        "diagnosis": diagnosis.model_dump_json(),
                        "affected_lines": affected_str,
                        "sandbox_error": last_sandbox_error,
                        "code": current_code,
                    }
                ),
                timeout=cfg.llm_timeout,
            )
            fixed_code = _strip_code_fences(_extract_text(fix_result))
            fixed_code = strip_think_markers(fixed_code)
        except TimeoutError:
            log.warning("code_fix_gen_timeout", attempt=attempt)
            continue
        except Exception as exc:
            log.warning("code_fix_gen_failed", attempt=attempt, exc=str(exc)[:200])
            continue

        if skip_sandbox:
            execution_result = (
                f"Framework code ({framework}) — sandbox execution skipped. Fix is based on static analysis."
            )
            fix_succeeded = True
            break

        # Verify in sandbox.
        try:
            sandbox_result = await asyncio.wait_for(
                execute_python.ainvoke({"code": fixed_code}),
                timeout=cfg.llm_timeout,
            )
            if sandbox_result.get("success", False):
                execution_result = sandbox_result.get("stdout", "(no output)")
                fix_succeeded = True
                log.info("code_fix_verified", attempt=attempt)
                break
            else:
                error = sandbox_result.get("error", "Unknown error")
                execution_result = f"Attempt {attempt}/{_MAX_RETRIES}: {error}"
                log.warning("code_fix_sandbox_error", attempt=attempt, error=error[:200])
                last_sandbox_error = f"The previous fix failed with this sandbox error:\n{error}"
                current_code = fixed_code
        except TimeoutError:
            log.warning("code_fix_sandbox_timeout", attempt=attempt)
            execution_result = f"Attempt {attempt}/{_MAX_RETRIES}: sandbox timed out"
        except Exception as exc:
            log.warning("code_fix_sandbox_exception", attempt=attempt, exc=str(exc)[:200])
            execution_result = f"Sandbox error: {str(exc)[:200]}"

    # Explanation
    explain_prompt = ChatPromptTemplate.from_messages(
        [
            ("system", CODE_FIX_SYSTEM_PROMPT + "\n\n" + CODE_FIX_EXPLANATION_PROMPT),
            ("human", "Explain the fix above."),
        ]
    )

    explanation: str = ""
    try:
        explain_result = await asyncio.wait_for(
            (explain_prompt | llm).ainvoke(
                {
                    "diagnosis": diagnosis.model_dump_json() if diagnosis else "(no diagnosis)",
                    "original_code": query,
                    "fixed_code": fixed_code or "(no fix generated)",
                    "execution_result": execution_result or "(not executed)",
                    "knowledge_units": ku_text,
                }
            ),
            timeout=cfg.llm_timeout,
        )
        explanation = _extract_text(explain_result)
        explanation = strip_think_markers(explanation)
        explanation = close_unbalanced_fenced_blocks(explanation)
        explanation = re.sub(
            r"\n{1,3}###\s+Key Concept[^\n]*\n+(?:None|N/A)[.\s]*$",
            "",
            explanation,
            flags=re.IGNORECASE,
        ).rstrip()
    except TimeoutError:
        log.error("code_fix_explain_timeout")
        explanation = (
            f"### What Was Wrong\n{diagnosis.root_cause}\n\n"
            f"### Why It Happened\n{diagnosis.error_type} error — see diagnosis above.\n\n"
            f"### Best Practice\nReview the fix strategy: {diagnosis.fix_strategy}"
        )
    except Exception as exc:
        err_str = str(exc)
        log.error("code_fix_explain_failed", exc=err_str[:200])
        explanation = (
            f"### What Was Wrong\n{diagnosis.root_cause}\n\n"
            f"### Why It Happened\n`{diagnosis.error_type}` error. "
            f"{diagnosis.fix_strategy}\n\n"
            f"### Best Practice\nKeep code submissions short so the full "
            "explanation can be generated."
        )

    # Assemble response
    parts: list[str] = []

    if not fix_succeeded:
        parts.append(f"⚠️ **Could not fully resolve the issue after {_MAX_RETRIES} attempts.**")

    diag_lines = [
        "### Diagnosis",
        f"- **Error type**: {diagnosis.error_type}",
        f"- **Root cause**: {diagnosis.root_cause}",
    ]
    if diagnosis.fix_strategy:
        diag_lines.append(f"- **Fix strategy**: {diagnosis.fix_strategy}")
    parts.append("\n".join(diag_lines))

    if fixed_code:
        if fix_succeeded:
            parts.append(f"### Fixed Code\n{_fenced_block(fixed_code, 'python')}")
        else:
            parts.append(
                f"### Last Attempted Fix (did not pass verification)\n"
                f"{_fenced_block(fixed_code, 'python')}\n"
                f"*This fix failed sandbox verification. Review the error above.*"
            )

    if execution_result:
        parts.append(f"### Execution Result\n{_fenced_block(execution_result)}")

    if explanation:
        parts.append(explanation)

    elapsed_ms = int((time.perf_counter() - t_start) * 1000)
    log.info(
        "code_fix_complete",
        fix_succeeded=fix_succeeded,
        error_type=diagnosis.error_type,
        elapsed_ms=elapsed_ms,
    )

    return {
        "response": "\n\n".join(parts),
        "tool_calls": [
            {
                "tool": "code_fix",
                "fix_succeeded": fix_succeeded,
                "error_type": diagnosis.error_type,
            }
        ],
    }
