"""
Document export tools for Sage.

Provides two LangChain tools:
  1. `export_markdown` —write Markdown content to a file.
  2. `export_pdf` —compile Markdown to a LaTeX-quality PDF via Typst.

The Typst template is stored at `config/templates/academic_report.typ`.

Usage:

    from sage.tools.export import export_markdown, export_pdf
    path = export_markdown.invoke({"content": "# Report", "filename": "report"})
    path = await export_pdf.ainvoke({"content": "# Report", "filename": "report"})
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, Any

import structlog
from langchain_core.tools import tool

from sage.config import get_settings

log = structlog.get_logger(__name__)

# --- Constants ---

_MAX_CONTENT_LENGTH: int = 100_000
_MAX_FILENAME_LENGTH: int = 100
_PDF_TIMEOUT: int = 30

def _response(success: bool, operation: str, path: str | None = None, error: str | None = None, meta: Dict[str, Any] | None = None) -> Dict[str, Any]:
    return {
        "success": success,
        "operation": operation,
        "path": path,
        "error": error,
        "meta": meta or {},
    }

def _sanitize_filename(filename: str) -> str:
    """Strip unsafe characters and enforce length limit."""
    clean = Path(filename).name
    clean = re.sub(r'[<>:"/\\|?*]', "_", clean)
    clean = clean.strip(". ")
    if not clean:
        clean = "export"
    return clean[:_MAX_FILENAME_LENGTH]


def _resolve_output_dir() -> Path:
    """Return the absolute export output directory, creating it if needed."""
    output_dir = get_settings().tools.export.output_dir
    if not output_dir.is_absolute():
        from sage.config import _PROJECT_ROOT
        output_dir = _PROJECT_ROOT / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def _resolve_typst_bin() -> str:
    """Resolve the path to the typst binary against the project root if it is local."""
    bin_path = get_settings().tools.export.typst_bin
    p = Path(bin_path)

    if p.is_absolute():
        return str(p)

    if "/" in bin_path or "\\" in bin_path:
        from sage.config import _PROJECT_ROOT
        return str(_PROJECT_ROOT / bin_path)

    return bin_path


def validate_typst_bin() -> bool:
    """Return True if the configured Typst binary is accessible.

    Call this during startup / health-check to fail early rather than
    discovering the missing binary during the first PDF export request.
    """
    import shutil
    bin_path = _resolve_typst_bin()
    p = Path(bin_path)
    if p.suffix:
        return p.is_file()
    return shutil.which(bin_path) is not None


# --- Markdown Export ---
@tool
def export_markdown(content: str, filename: str) -> Dict[str, Any]:
    """Export content as a Markdown (.md) file.

    Writes the content to the configured export directory.  Returns
    the absolute path to the created file.

    Args:
        content: Markdown-formatted text content to export.
        filename: Desired filename (without extension).  Unsafe
            characters are stripped automatically.

    Returns:
        Absolute path to the created .md file, or an error message.
    """
    operation = "export_markdown"
    if not isinstance(content, str) or not content.strip():
        return _response(False, operation, error="No content provided")

    if len(content) > _MAX_CONTENT_LENGTH:
        return _response(False, operation, error=f"Content too long ({len(content)})")

    safe_name = _sanitize_filename(filename)
    output_dir = _resolve_output_dir()
    output_path = output_dir / f"{safe_name}.md"

    counter = 1
    while output_path.exists():
        output_path = output_dir / f"{safe_name}_{counter}.md"
        counter += 1

    try:
        output_path.write_text(content, encoding="utf-8")
        log.info(
            "export_markdown_complete",
            path=str(output_path),
            content_length=len(content),
        )
    
        return _response(
            True,
            operation,
            path=str(output_path),
            meta={"length": len(content)},
        )

    except Exception as exc:
        log.error("export_markdown_failed", error=str(exc)[:200])
        return _response(False, operation, error=str(exc)[:200])


# --- PDF Export ---
@tool
async def export_pdf(content: str, filename: str) -> Dict[str, Any]:
    """Export content as a PDF using Typst.

    Compiles Markdown content through a Typst template to produce a
    professionally formatted PDF with Computer Modern fonts, numbered
    sections, and math rendering.

    Requires the `typst` binary to be installed and accessible
    (path configured in `tools.export.typst_bin`).

    Args:
        content: Markdown-formatted text content to export.
        filename: Desired filename (without extension).

    Returns:
        Absolute path to the created .pdf file, or an error message
        if Typst is unavailable or compilation fails.
    """
    operation = "export_pdf"

    if not isinstance(content, str) or not content.strip():
        return _response(False, operation, error="No content provided")

    if len(content) > _MAX_CONTENT_LENGTH:
        return _response(False, operation, error=f"Content too long ({len(content)})")

    safe_name = _sanitize_filename(filename)
    output_dir = _resolve_output_dir()
    output_path = output_dir / f"{safe_name}.pdf"

    counter = 1
    while output_path.exists():
        output_path = output_dir / f"{safe_name}_{counter}.pdf"
        counter += 1

    try:
        typst_content = _generate_typst_source(content)
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".typ",
            encoding="utf-8",
            delete=False,
        ) as tmp:
            tmp.write(typst_content)
            tmp_path = Path(tmp.name)

        # Run Typst compilation.
        resolved_typst_bin = _resolve_typst_bin()
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                resolved_typst_bin,
                "compile",
                str(tmp_path),
                str(output_path),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ),
            timeout=_PDF_TIMEOUT,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=_PDF_TIMEOUT,
        )

        # Cleanup temp file.
        tmp_path.unlink(missing_ok=True)

        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace").strip()
            log.error(
                "export_pdf_typst_failed",
                returncode=proc.returncode,
                stderr=err[:300],
            )
        log.info("export_pdf_complete", path=str(output_path))

        return _response(
            True,
            operation,
            path=str(output_path),
            meta={"length": len(content)},
        )

    except FileNotFoundError:
        return _response(False, operation, error="Typst binary not found")
    
    except TimeoutError:
        return _response(False, operation, error="PDF generation timed out")
    
    except ValueError as exc:
        return _response(False, operation, error=str(exc))

    except Exception as exc:
        log.error("export_pdf_unexpected", error=str(exc)[:200])
        return _response(False, operation, error=str(exc)[:200])


def _markdown_to_typst(md: str) -> str:
    """Lightweight translation of basic Markdown into Typst syntax.

    Scans for Typst file/system call patterns before transforming.
    Raises ValueError so export_pdf returns a sanitized error rather than
    passing hostile content to the Typst compiler.

    NOTE: This is a conservative primitive for the tool layer.
    Callers are expected to pass well-structured Markdown; the tool does not
    enforce document schema (title/sections/citations), that is an
    agent-layer responsibility.
    """
    _UNSAFE = re.compile(
        r"#(include|read|csv|json|yaml|toml|xml|bytes|plugin|sys)\s*[\(\[]",
        re.IGNORECASE,
    )
    if _UNSAFE.search(md):
        raise ValueError("Unsafe Typst directive detected")

    md = md.replace("#", r"\#")

    def _heading(m: re.Match) -> str:  # type: ignore[type-arg]
        depth = m.group(1).count(r"\#")
        return "=" * depth + " "

    md = re.sub(r"^((?:\\#)+)\s+", _heading, md, flags=re.MULTILINE)
    md = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'#link("\2")[\1]', md)

    return md

def _generate_typst_source(markdown_content: str) -> str:
    """Wrap markdown content in a Typst document template.

    Produces a minimal but well-formatted Typst document with:
    - A4 page size with standard academic margins
    - Heading numbering
    - Raw markdown content (Typst natively supports a subset of
      Markdown syntax)
    """
    body = _markdown_to_typst(markdown_content)
    return (
        '#set page(paper: "a4", margin: (x: 2.5cm, y: 2.5cm))\n'
        '#set text(font: "New Computer Modern", size: 11pt)\n'
        '#set heading(numbering: "1.1")\n'
        "#set par(justify: true)\n\n"
        f"{body}\n"
    )
