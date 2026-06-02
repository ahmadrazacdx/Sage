"""
Subprocess-based Python code executor for Sage.

Provides one LangChain tool:
  1. `execute_python` — run Python code in an isolated child
     process, capture stdout/stderr, and return any generated figures
     as embedded SVG strings ready for display or download.

Code is written to a temp file and executed as a child process
using `sys.executable`, the same Python interpreter bundled with
Sage. All figure files are written inside a unique subdirectory of
`tools.sandbox.figures_dir` and deleted after the SVG data is
read back into the response dict.

Usage:

    from sage.tools.sandbox import execute_python
    result = await execute_python.ainvoke({"code": "print(2 + 2)"})
"""

from __future__ import annotations

import ast
import asyncio
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import structlog
from langchain_core.tools import tool
from pydantic import BaseModel

from sage.config import get_settings

log = structlog.get_logger(__name__)

# --- Constants ---
_SECRET_ENV_KEYS: frozenset[str] = frozenset(
    {
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "LANGCHAIN_API_KEY",
        "HUGGINGFACE_TOKEN",
        "SAGE_SECRET",
    }
)

_MPL_MODULE_ROOTS: frozenset[str] = frozenset(
    {
        "matplotlib",
        "pylab",
        "seaborn",
        "pandas",
    }
)

# Injected before every code block.
_PREAMBLE_TEMPLATE: str = """\
try:
    import matplotlib as _mpl
    _mpl.use('Agg')
    import matplotlib.pyplot as _plt
    import os as _os

    _SAGE_FIG_DIR = {fig_dir}
    _sage_fig_idx = [0]

    def _sage_show(*_args, **_kwargs):
        _sage_fig_idx[0] += 1
        _fname = _os.path.join(
            _SAGE_FIG_DIR,
            "fig_" + str(_sage_fig_idx[0]).zfill(3) + ".svg",
        )
        _plt.savefig(_fname, format="svg", bbox_inches="tight")
        _plt.close()

    _plt.show = _sage_show
    _SAGE_MPL_OK = True
except ImportError:
    _SAGE_MPL_OK = False

"""

# Appended after every code block that uses matplotlib.
# Saves any figures created but never explicitly showed.
_EPILOGUE: str = """

# -- Sage figure epilogue: capture remaining open figures --
if _SAGE_MPL_OK:
    import os as _os2
    for _sage_fn in sorted(_plt.get_fignums()):
        _sage_fig_idx[0] += 1
        _fname = _os2.path.join(
            _SAGE_FIG_DIR,
            "fig_" + str(_sage_fig_idx[0]).zfill(3) + ".svg",
        )
        _plt.figure(_sage_fn)
        _plt.savefig(_fname, format="svg", bbox_inches="tight")
        _plt.close(_sage_fn)
"""


def _response(
    success: bool,
    operation: str,
    *,
    stdout: str = "",
    stderr: str = "",
    figures: list[dict[str, Any]] | None = None,
    error: str | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "success": success,
        "operation": operation,
        "stdout": stdout,
        "stderr": stderr,
        "figures": figures or [],
        "error": error,
        "meta": meta or {},
    }


# --- Input schema ---
class ExecutePythonInput(BaseModel):
    """Input schema for `execute_python`."""

    code: str


def _resolve_figures_dir() -> Path:
    """Return the absolute base figures directory from config.

    Reads `get_settings().tools.sandbox.figures_dir` and resolves it
    relative to the project root (`Path.cwd()`) when running in dev,
    or relative to `sys.executable`'s parent in a frozen build.
    Creates the directory if it does not exist.

    Returns:
        Absolute `Path` to the figures base directory.
    """
    cfg_path: Path = get_settings().tools.sandbox.figures_dir

    if cfg_path.is_absolute():
        resolved = cfg_path
    elif getattr(sys, "frozen", False):
        resolved = Path(sys.executable).parent / cfg_path
    else:
        resolved = Path.cwd() / cfg_path

    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _uses_matplotlib(user_code: str) -> bool:
    """Return True if user_code imports any matplotlib-adjacent module.

    Uses AST parsing for exact detection. Falls back to `True` on `SyntaxError`
    so that the preamble is always injected for unparseable code.  The subprocess
    will surface the real syntax error to the user; skipping the preamble on broken
    code would only create a confusing secondary `NameError`.

    Args:
        user_code: Raw student Python source.

    Returns:
        `True` if the preamble and epilogue should be injected.
    """
    try:
        tree = ast.parse(user_code)
    except SyntaxError:
        return True

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name.split(".")[0] in _MPL_MODULE_ROOTS for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in _MPL_MODULE_ROOTS:
                return True

    return False


def _build_execution_code(user_code: str, figure_dir: str) -> str:
    """Wrap student code with the matplotlib preamble and figure epilogue.

    The preamble and epilogue are injected only when `_uses_matplotlib`
    detects a relevant import, avoiding the ~200–500 ms cold-import cost
    on every non-plotting execution (e.g. `print("hello")`).

    Uses `repr(figure_dir)` to safely embed the path as a Python
    string literal — handles Windows backslashes without manual escaping.

    Args:
        user_code:  Raw student Python source.
        figure_dir: Absolute path string to the per-execution temp
                    directory where SVG figures will be written.

    Returns:
        Complete Python source ready to write to a temp file and execute.
    """
    if not _uses_matplotlib(user_code):
        return user_code

    preamble = _PREAMBLE_TEMPLATE.format(fig_dir=repr(figure_dir))
    return preamble + user_code + _EPILOGUE


def _collect_figures(figure_dir: str) -> list[dict[str, Any]]:
    """Read SVG files written by the child process.

    Reads all `fig_*.svg` files from `figure_dir`, validates each one,
    and returns them as structured dicts.  Figures are returned even when
    the child process exited with an error, because partial output is
    useful for the code-fix agent.

    Args:
        figure_dir: Directory passed to `_build_execution_code`.

    Returns:
        List of figure dicts sorted by index, each with keys:
        `index` (int, 1-based), `format` ("svg"), `data` (SVG
        markup string), `filename` (str).  Empty list if no figures
        were produced or all were invalid.
    """
    figures: list[dict[str, Any]] = []

    for svg_file in sorted(Path(figure_dir).glob("fig_*.svg")):
        try:
            svg_data = svg_file.read_text(encoding="utf-8")
            if len(svg_data.strip()) < 50 or "<svg" not in svg_data:
                continue
            figures.append(
                {
                    "index": int(svg_file.stem.split("_")[1]),
                    "format": "svg",
                    "data": svg_data,
                    "filename": svg_file.name,
                }
            )
        except (OSError, ValueError):
            pass

    return figures


# --- Core subprocess runner ---
async def _run_in_subprocess(
    code: str,
    timeout: float,
) -> tuple[str, str, str, list[dict[str, Any]]]:
    """Write code to a temp file, execute it, and collect output.

    Creates a unique subdirectory inside the configured figures base
    directory for this execution.  Both the code file and the figure
    subdirectory are deleted in the `finally` block on every exit
    path, including timeout and unexpected exceptions.

    Args:
        code:    Student Python source (preamble/epilogue injected here).
        timeout: Hard wall-clock limit in seconds.

    Returns:
        `(status, stdout, stderr, figures)` where *status* is one of
        `"success"`, `"error"`, or `"timeout"`.
    """
    tmp_path: str | None = None
    # Create a unique per-execution subdirectory inside the configured base
    figures_base = _resolve_figures_dir()
    figure_dir: str = tempfile.mkdtemp(prefix="exec_", dir=str(figures_base))

    try:
        full_code = _build_execution_code(code, figure_dir)

        # Named temp file so tracebacks show a real path
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".py",
            prefix="sage_exec_",
            delete=False,
            encoding="utf-8",
        ) as f:
            f.write(full_code)
            tmp_path = f.name

        env = {k: v for k, v in os.environ.items() if k not in _SECRET_ENV_KEYS}
        env["MPLBACKEND"] = "Agg"

        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            tmp_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            import contextlib

            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.communicate()
            return "timeout", "", "", []

        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")
        status: str = "success" if proc.returncode == 0 else "error"

        # Collect figures even on error
        figures = _collect_figures(figure_dir)
        return status, stdout, stderr, figures

    finally:
        if tmp_path and Path(tmp_path).exists():
            import contextlib

            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
        shutil.rmtree(figure_dir, ignore_errors=True)


@tool(args_schema=ExecutePythonInput)
async def execute_python(code: str) -> dict[str, Any]:
    """Execute Python code in an isolated child process.

    Runs using the same Python interpreter and packages bundled with
    Sage.  All standard packages are available.

    Matplotlib figures are captured automatically:
      - `plt.show()` saves the current figure as SVG and closes it.
      - Figures created but never shown are saved by the epilogue.
      - All figures are returned as embedded SVG strings in `figures`
        ready for display in chat or file download.

    This tool serves both direct student code requests and the iterative
    code-fix agent loop.

    Args:
        code: Python source code to execute. Example:

                import numpy as np
                import matplotlib.pyplot as plt

                x = np.linspace(0, 2 * np.pi, 200)
                plt.plot(x, np.sin(x))
                plt.title("Sine wave")
                plt.show()   # captured as SVG automatically

    Returns:
        Dict with keys:
        `success`   : bool.
        `operation` : `"execute_python"`.
        `stdout`    : Captured standard output (str).
        `stderr`    : Captured standard error / tracebacks (str).
        `figures`   : List of figure dicts.  Each dict contains:
        `index` (int, 1-based), `format` ("svg"), `data` (SVG
        markup string), ``filename`` (str).  Empty list when no figures
        were produced.
        `error`     : ``None`` on success; error description on
        failure or timeout.
        `meta`      : Dict with `exec_time_ms` (int),
        `code_length` (int), `figure_count` (int),
        `backend` ("subprocess").
    """
    operation = "execute_python"
    cfg = get_settings().tools.sandbox
    max_len = cfg.max_code_length
    timeout = cfg.timeout

    start = time.perf_counter()
    meta: dict[str, Any] = {
        "code_length": len(code) if code else 0,
        "exec_time_ms": 0,
        "figure_count": 0,
        "backend": "subprocess",
    }

    if not code or not code.strip():
        meta["exec_time_ms"] = int((time.perf_counter() - start) * 1000)
        return _response(False, operation, error="No code provided", meta=meta)

    if len(code) > max_len:
        log.warning("sandbox_code_too_long", length=len(code), max_length=max_len)
        meta["exec_time_ms"] = int((time.perf_counter() - start) * 1000)
        return _response(
            False,
            operation,
            error=f"Code exceeds maximum length ({len(code):,}/{max_len:,} chars)",
            meta=meta,
        )

    try:
        status, stdout, stderr, figures = await _run_in_subprocess(code, timeout)
        meta["exec_time_ms"] = int((time.perf_counter() - start) * 1000)
        meta["figure_count"] = len(figures)

        if status == "timeout":
            log.warning("sandbox_timeout", timeout=timeout)
            return _response(
                False,
                operation,
                stdout=stdout,
                stderr=stderr,
                figures=figures,
                error=f"Execution timed out after {timeout}s",
                meta=meta,
            )

        log.debug(
            "sandbox_execution_complete",
            status=status,
            stdout_len=len(stdout),
            stderr_len=len(stderr),
            figure_count=len(figures),
        )

        if status == "error":
            return _response(
                False,
                operation,
                stdout=stdout,
                stderr=stderr,
                figures=figures,
                error=stderr.strip() or "Execution failed with non-zero exit code",
                meta=meta,
            )

        return _response(
            True,
            operation,
            stdout=stdout,
            stderr=stderr,
            figures=figures,
            meta=meta,
        )

    except Exception as exc:  # noqa: BLE001
        meta["exec_time_ms"] = int((time.perf_counter() - start) * 1000)
        log.error("sandbox_execution_failed", exc=str(exc)[:300])
        return _response(
            False,
            operation,
            error=f"{type(exc).__name__}: {str(exc)[:300]}",
            meta=meta,
        )
