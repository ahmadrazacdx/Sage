"""
Mermaid diagram rendering and syntax validation for Sage.

Provides two LangChain tools:
  1. `render_mermaid_svg` — render Mermaid source to an SVG string.
  2. `validate_mermaid`  — check whether Mermaid source is renderable.

Both tools delegate to mmdr (mermaid-rs-renderer), a native Rust
binary.

Usage:

    from sage.tools.mermaid import render_mermaid_svg, validate_mermaid

    result = await render_mermaid_svg.ainvoke({"code": "flowchart TD\\nA --> B"})
    check  = await validate_mermaid.ainvoke({"code": "flowchart TD\\nA --> B"})
"""

from __future__ import annotations

import asyncio
import re
import sys
import time
import subprocess
from pathlib import Path
from typing import Any, Dict

import structlog
from langchain_core.tools import tool
from pydantic import BaseModel

from sage.config import get_settings

log = structlog.get_logger(__name__)

# --- Constants ---
_MMDR_ERROR_MARKERS: tuple[str, ...] = (
    "parse error",
    "syntax error",
    "error:",
    "lexical error",
    "no diagram type detected",
    "expecting",
)

_VALIDATE_TIMEOUT: float = 10.0


def _response(
    success: bool,
    operation: str,
    *,
    svg: str | None = None,
    valid: bool | None = None,
    errors: list[str] | None = None,
    error: str | None = None,
    meta: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "success": success,
        "operation": operation,
        "error": error,
        "meta": meta or {},
    }
    if svg is not None:
        base["svg"] = svg
    if valid is not None:
        base["valid"] = valid
        base["errors"] = errors or []
    return base

def _svg_contains_error(svg: str) -> bool:
    """Return True if svg encodes an mmdr parse/syntax error.

    mmdr exits 0 and embeds error text inside a placeholder SVG when it
    cannot fully parse the input.  Scanning SVG content is the only
    reliable way to detect this — exit code alone is insufficient.

    Args:
        svg: Raw SVG string returned by mmdr on stdout.

    Returns:
        True if any known error marker is present; False for a clean render.
    """
    lower = svg.lower()
    return any(marker in lower for marker in _MMDR_ERROR_MARKERS)


def _extract_svg_error(svg: str) -> str:
    """Extract a plain-text error message from an mmdr error-placeholder SVG.

    Strips all XML tags and returns the first line containing a known
    error marker.  Falls back to a generic message if none is found.

    Args:
        svg: SVG string confirmed to contain an error via
            `_svg_contains_error`.

    Returns:
        Human-readable error string suitable for the diagram-fix loop.
    """
    text = re.sub(r"<[^>]+>", " ", svg)
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and any(m in stripped.lower() for m in _MMDR_ERROR_MARKERS):
            return stripped
    return "Diagram parse error — check Mermaid syntax"


# --- Binary resolution ---

def _resolve_mmdr_path() -> Path:
    """Return the expected filesystem path for the mmdr binary.

    Resolution order (stops at first match):
      1. Configured path is absolute → use as-is.
      2. Frozen (PyInstaller) → relative to ``sys.executable`` parent.
      3. Development → relative to ``Path.cwd()`` (project root via
         ``uv run``).

    The same ``default.toml`` value works in both dev and production
    without modification.

    Returns:
        Path to the binary (existence not asserted — use
        ``_assert_mmdr()`` for that).
    """
    cfg_path: Path = get_settings().tools.mermaid.mmdr_bin_path

    if cfg_path.is_absolute():
        return cfg_path

    if getattr(sys, "frozen", False):
        candidate = Path(sys.executable).parent / cfg_path
        if candidate.exists():
            return candidate

    # Dev: CWD is the project root when launched via `uv run`
    return Path.cwd() / cfg_path


def _assert_mmdr() -> Path:
    """Return a validated mmdr path or raise ``FileNotFoundError``.

    Raises:
        FileNotFoundError: Binary absent at the resolved path, with a
            message containing the download URL, expected location, and
            PyInstaller spec snippet.
    """
    path = _resolve_mmdr_path()
    if not path.exists():
        cfg_value = get_settings().tools.mermaid.mmdr_bin_path
        raise FileNotFoundError(
            f"mmdr binary not found.\n"
            f"  Resolved path    : {path}\n"
            f"  Configured value : {cfg_value}\n"
            f"  Download from    : https://github.com/1jehuang/mermaid-rs-renderer/releases\n"
            f"  Windows binary   : mmdr-x86_64-pc-windows-msvc.zip → artifacts/mmdr/mmdr.exe\n"
        )
    return path


# --- Input schemas ---
class MermaidInput(BaseModel):
    """Input schema for ``render_mermaid_svg``."""
    code: str


class ValidateMermaidInput(BaseModel):
    """Input schema for ``validate_mermaid``."""
    code: str


# --- Core subprocess runner ---
async def _run_mmdr(
    code: str,
    timeout: float,
) -> tuple[str, str, str]:
    """Pipe Mermaid source to mmdr and return its output.

    Writes code to mmdr's stdin, captures stdout (SVG) and stderr
    independently, and enforces a hard timeout.  On expiry the process
    is killed and both pipes are drained to prevent OS handle leaks.

    After exit 0 the SVG is inspected via `_svg_contains_error` to
    catch mmdr's lenient behaviour of producing a placeholder SVG for
    some invalid input.

    Args:
        code:    Mermaid diagram source text.
        timeout: Maximum wall-clock seconds before the process is killed.

    Returns:
        `(status, stdout, stderr)` — all strings, never None.
        status is one of `"success"`, `"error"`, `"timeout"`.
    """
    try:
        binary = _assert_mmdr()
    except FileNotFoundError as exc:
        return "error", "", str(exc)

    try:
        proc = await asyncio.create_subprocess_exec(
            str(binary),
            "-e", "svg",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0),
        )

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(input=code.encode("utf-8")),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.communicate()
            return "timeout", "", ""

        stdout = stdout_b.decode("utf-8", errors="replace").strip()
        stderr = stderr_b.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0:
            return "error", stdout, stderr

        if stdout.startswith("<svg") and _svg_contains_error(stdout):
            return "error", "", _extract_svg_error(stdout)

        return "success", stdout, stderr

    except Exception as exc:  # noqa: BLE001
        log.error("mmdr_subprocess_failed", exc=str(exc)[:300])
        return "error", "", f"{type(exc).__name__}: {exc}"


# --- Tools ---
@tool(args_schema=MermaidInput)
async def render_mermaid_svg(code: str) -> Dict[str, Any]:
    """Render a Mermaid diagram definition to SVG markup.

    Invokes the bundled mmdr binary.  No browser, Node.js, or network
    access is required.  Supports all diagram types recognised by
    Mermaid v11: flowchart, sequence, class, state, ER, mindmap, etc.

    Args:
        code: Mermaid diagram source.  Example::

                flowchart TD
                    A[Start] --> B{Decision}
                    B -->|Yes| C[Result]
                    B -->|No|  D[End]

    Returns:
        Dict with keys:

        `success`   : bool.
        `operation` : `"render_mermaid_svg"`.
        `svg`       : SVG markup string on success; empty string on
        failure.
        `error`     : `None` on success; plain-text description on
        failure.
        `meta`      : Dict with `exec_time_ms` (int) and
        `code_length` (int).
    """
    operation = "render_mermaid_svg"
    start = time.perf_counter()
    timeout = get_settings().tools.mermaid.render_timeout

    meta: Dict[str, Any] = {
        "code_length": len(code) if code else 0,
        "exec_time_ms": 0,
    }

    if not code or not code.strip():
        meta["exec_time_ms"] = int((time.perf_counter() - start) * 1000)
        return _response(False, operation, svg="", error="No code provided", meta=meta)

    status, stdout, stderr = await _run_mmdr(code, timeout=timeout)
    meta["exec_time_ms"] = int((time.perf_counter() - start) * 1000)

    if status == "timeout":
        log.warning("mermaid_render_timeout", timeout=timeout, code_length=len(code))
        return _response(
            False, operation, svg="",
            error=f"Render timed out after {timeout}s", meta=meta,
        )

    if status == "error":
        log.warning("mermaid_render_failed", detail=stderr[:300])
        return _response(
            False, operation, svg="",
            error=stderr or "mmdr exited with non-zero status", meta=meta,
        )

    if not stdout.startswith("<svg"):
        log.error("mermaid_unexpected_output", preview=stdout[:120])
        return _response(
            False, operation, svg="",
            error="mmdr exited successfully but output is not valid SVG", meta=meta,
        )

    log.debug("mermaid_render_success", svg_bytes=len(stdout))
    return _response(True, operation, svg=stdout, meta=meta)


@tool(args_schema=ValidateMermaidInput)
async def validate_mermaid(code: str) -> Dict[str, Any]:
    """Check whether Mermaid source is renderable by mmdr.

    Performs a full render pass and discards the SVG.  A clean render
    (exit 0, no error markers in SVG content) means the diagram is valid
    *for Sage's rendering pipeline*.

    Note:
        mmdr's Rust parser is intentionally lenient and accepts some
        technically malformed diagrams as renderable graphs.  The
        contract is therefore:

        - `valid=False` → diagram is definitely broken.
        - `valid=True`  → diagram renders with mmdr, which is the
          only guarantee the diagram-fix agent loop requires.

        Strict Mermaid spec validation requires ``@mermaid-js/parser``
        (Node.js), which is not available in this build.

    Args:
        code: Mermaid diagram source to validate.

    Returns:
        Dict with keys:

        `success`   : bool.
        `operation` : `"validate_mermaid"`.
        `valid`     : `True` if renderable; `False` otherwise.
        `errors`    : List of error strings (empty when
        `valid=True`).
        `error`     : `None` on clean execution; exception message
        on unexpected failure.
        `meta`      : Dict with `exec_time_ms` (int).
    """
    operation = "validate_mermaid"
    start = time.perf_counter()
    meta: Dict[str, Any] = {"exec_time_ms": 0}

    if not code or not code.strip():
        meta["exec_time_ms"] = int((time.perf_counter() - start) * 1000)
        return _response(
            False, operation,
            valid=False, errors=["Empty input"], meta=meta,
        )

    status, _, stderr = await _run_mmdr(code, timeout=_VALIDATE_TIMEOUT)
    meta["exec_time_ms"] = int((time.perf_counter() - start) * 1000)

    if status == "success":
        return _response(True, operation, valid=True, errors=[], meta=meta)

    errors = [ln for ln in stderr.splitlines() if ln.strip()]
    return _response(
        False, operation,
        valid=False,
        errors=errors or ["Diagram parse error — check Mermaid syntax"],
        meta=meta,
    )