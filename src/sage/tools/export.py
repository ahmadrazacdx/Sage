"""
Document export tools for Sage.

Provides two LangChain tools:
  1. `export_markdown`: write Markdown content to a file.
  2. `export_pdf`: compile Markdown to a High-quality PDF via Typst,
        using the academic_report.typ template.
Usage:

    from sage.tools.export import export_markdown, export_pdf
    path = export_markdown.invoke({"content": "# Report", "filename": "report"})
    path = await export_pdf.ainvoke({"content": "# Report", "filename": "report",
                                       "title": "My Report", "author": "Alice"})
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog
from langchain_core.tools import tool

from sage.config import get_settings

log = structlog.get_logger(__name__)

# --- Constants ---

_MAX_CONTENT_LENGTH: int = 10_000
_MAX_FILENAME_LENGTH: int = 100
_PDF_TIMEOUT: int = 30
_DEFAULT_EXPORT_DIR_RELATIVE = Path("artifacts/data/exports")

_UNSAFE_RE = re.compile(
    r"#(include|read|csv|json|yaml|toml|xml|bytes|plugin|sys)\s*[\(\[]",
    re.IGNORECASE,
)

_TEMPLATE_NAME = "academic_report.typ"


def _response(
    success: bool,
    operation: str,
    path: str | None = None,
    error: str | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
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
    clean = re.sub(r'[<>:"/\\|?*]', "_", clean).strip(". ")
    return (clean or "export")[:_MAX_FILENAME_LENGTH]


def sanitize_export_filename(filename: str) -> str:
    """Public wrapper used by non-tool exporters."""
    return _sanitize_filename(filename)


def _default_documents_export_dir() -> Path:
    """Return the user-facing default export location."""
    return Path.home() / "Documents" / "Sage" / "exports"


def resolve_export_output_dir() -> Path:
    """Return the absolute export output directory, creating it if needed."""
    configured = get_settings().tools.export.output_dir.expanduser()
    if configured.is_absolute():
        output_dir = configured
    elif configured == _DEFAULT_EXPORT_DIR_RELATIVE:
        output_dir = _default_documents_export_dir()
    else:
        from sage.config import _PROJECT_ROOT

        output_dir = _PROJECT_ROOT / configured

    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def reserve_export_path(filename: str, suffix: str) -> Path:
    """Return a writable non-conflicting export path."""
    safe_name = sanitize_export_filename(filename)
    normalized_suffix = suffix if suffix.startswith(".") else f".{suffix}"
    return _nonconflict_path(resolve_export_output_dir() / f"{safe_name}{normalized_suffix}")


def _resolve_output_dir() -> Path:
    """Return the absolute export output directory, creating it if needed."""
    return resolve_export_output_dir()


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


def _resolve_template_path() -> Path | None:
    """Return the absolute path to academic_report.typ, or None if missing."""
    try:
        from sage.config import _PROJECT_ROOT

        candidate = _PROJECT_ROOT / "config" / "templates" / _TEMPLATE_NAME
        if candidate.is_file():
            return candidate
    except Exception:
        pass
    return None


def validate_typst_bin() -> bool:
    """Return True if the configured Typst binary is accessible."""
    import shutil

    bin_path = _resolve_typst_bin()
    p = Path(bin_path)
    if p.suffix:
        return p.is_file()
    return shutil.which(bin_path) is not None


def _nonconflict_path(base: Path) -> Path:
    """Return base if it does not exist, otherwise base_1, base_2, …"""
    if not base.exists():
        return base
    stem, suffix = base.stem, base.suffix
    counter = 1
    while True:
        candidate = base.parent / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def _flush_refs(ref_lines: list[str], out: list[str]) -> None:
    """Emit accumulated [N] reference lines as a Typst hanging-indent block."""
    if not ref_lines:
        return
    out.append("")
    for ref in ref_lines:
        out.append(f"#pad(left: 0pt)[#block(inset: (left: 2em), above: 0.4em)[{ref}]]")
    out.append("")
    ref_lines.clear()


_REF_LINE_RE = re.compile(r"^\[\d+\]\s+.+")


def _markdown_to_typst(md: str) -> str:
    """Convert basic Markdown to Typst markup.

    Security: raises ValueError if hostile Typst file-system directives are found.
    """
    if _UNSAFE_RE.search(md):
        raise ValueError("Unsafe Typst directive detected in content")
    md = re.sub(r"\s+(\[\d+\]\s)", r"\n\1", md)

    lines = md.split("\n")
    out_lines: list[str] = []
    pending_refs: list[str] = []

    for line in lines:
        # --- Reference list lines ---
        if _REF_LINE_RE.match(line.strip()):
            pending_refs.append(line.strip())
            continue
        else:
            _flush_refs(pending_refs, out_lines)

        heading_match = re.match(r"^(#{1,6})\s+(.*)", line)
        if heading_match:
            depth = len(heading_match.group(1))
            body = heading_match.group(2).replace("#", r"\#")
            out_lines.append("=" * depth + " " + body)
            continue

        # Code fences: pass through raw
        if line.startswith("```"):
            out_lines.append(line)
            continue

        # Escape lone '#' (e.g. in reference lists "[1] Author#Year")
        line = re.sub(r"(?<!\\)#", r"\#", line)

        # Bold / italic
        # Links [text](url) to Typst #link
        line = re.sub(
            r"\[([^\]]+)\]\(([^)]+)\)",
            lambda m: f'#link("{m.group(2)}")[{m.group(1)}]',
            line,
        )
        out_lines.append(line)

    # Flush any trailing refs.
    _flush_refs(pending_refs, out_lines)

    return "\n".join(out_lines)


def _generate_typst_source(
    markdown_content: str,
    title: str = "",
    subtitle: str = "",
    author: str = "Sage Research Agent",
    date: str = "",
    institution: str = "",
) -> str:
    """Generate a complete, self-contained Typst document.

    If the academic_report.typ template exists it is used as-is.  Otherwise a rich
    inline fallback is generated so PDF export never silently fails.
    """
    template_path = _resolve_template_path()
    body_typst = _markdown_to_typst(markdown_content)
    date_str = date or datetime.now().strftime("%B %d, %Y")
    inst_str = (
        institution or get_settings().tools.export.institution
        if hasattr(get_settings().tools.export, "institution")
        else "Thal University Bhakkar"
    )

    if template_path:
        tpl = template_path.read_text(encoding="utf-8")
        full_src = tpl.replace("#report-body", body_typst)
        for var, val in [
            ('default: "Research Report"', f'default: "{_esc(title or "Research Report")}"'),
            ('default: ""', f'default: "{_esc(subtitle)}"'),
            ('default: "Sage Research Agent"', f'default: "{_esc(author)}"'),
            ('default: ""', f'default: "{_esc(date_str)}"'),
            ('default: "Thal University Bhakkar"', f'default: "{_esc(inst_str)}"'),
        ]:
            full_src = full_src.replace(var, val, 1)
        return full_src

    # Inline fallback (no template file found)
    return _inline_typst_doc(body_typst, title, subtitle, author, date_str, inst_str)


def _esc(s: str) -> str:
    """Escape double-quotes for embedding in Typst string literals."""
    return s.replace('"', '\\"')


def _inline_typst_doc(
    body: str,
    title: str,
    subtitle: str,
    author: str,
    date_str: str,
    institution: str,
) -> str:
    """Full professional Typst document when the template file is absent."""
    t = _esc(title or "Research Report")
    sub = _esc(subtitle)
    au = _esc(author)
    dt = _esc(date_str)
    ins = _esc(institution)

    return f"""\
// --- Colour palette ---
#let sage-dark   = rgb("#0f2744")
#let sage-mid    = rgb("#1a4a7a")
#let sage-accent = rgb("#2eaadc")
#let sage-light  = rgb("#eaf4fb")
#let sage-text   = rgb("#1c2b3a")
#let sage-muted  = rgb("#6b7f91")
 
// --- Page & typography ---
#set page(
  paper: "a4",
  margin: (top: 2.8cm, bottom: 2.8cm, left: 3cm, right: 2.5cm),
  footer: context {{
    let pg = counter(page).get().first()
    let total = counter(page).final().first()
    if pg > 1 [
      #set text(size: 8.5pt, fill: sage-muted)
      #grid(
        columns: (1fr, auto, 1fr),
        align(left, text("{t}", style: "italic")),
        align(center)[#pg / #total],
        align(right, text("Sage · {ins}")),
      )
      #line(length: 100%, stroke: 0.4pt + sage-muted)
    ]
  }},
)
#set text(font: ("New Computer Modern", "Linux Libertine", "Georgia"),
          size: 11pt, fill: sage-text, lang: "en")
#set par(justify: true, leading: 0.75em)
#set heading(numbering: "1.1.")
 
#show heading.where(level: 1): it => {{
  v(1.4em)
  block[
    #line(length: 100%, stroke: 1.6pt + sage-mid)
    #v(0.25em)
    #text(size: 14pt, weight: "bold", fill: sage-dark, upper(it.body))
    #v(0.2em)
    #line(length: 100%, stroke: 0.4pt + sage-accent)
  ]
  v(0.5em)
}}
#show heading.where(level: 2): it => {{
  v(1em)
  text(size: 12pt, weight: "bold", fill: sage-mid, it.body)
  v(0.4em)
}}
#show heading.where(level: 3): it => {{
  v(0.8em)
  text(size: 11pt, weight: "bold", style: "italic", fill: sage-text, it.body)
  v(0.3em)
}}
#show raw.where(block: true): it => {{
  block(width: 100%, fill: sage-light,
    stroke: (left: 3pt + sage-accent, rest: 0.5pt + sage-muted.lighten(40%)),
    radius: 4pt, inset: (x: 12pt, y: 10pt),
    text(font: ("JetBrains Mono", "Fira Code", "Courier New"), size: 9.5pt, it))
}}
#show raw.where(block: false): it => {{
  box(fill: sage-light, inset: (x: 4pt, y: 2pt), radius: 3pt,
    text(font: ("JetBrains Mono", "Fira Code", "Courier New"), size: 9.5pt, it))
}}
#show link: it => {{ text(fill: sage-accent, it) }}
 
// --- Cover page ---
#page(margin: 0pt, footer: none)[
  #block(width: 100%, height: 42%, fill: sage-dark)[
    #v(1fr)
    #pad(x: 3cm)[
      #block(height: 4pt, width: 6cm, fill: sage-accent)
      #v(0.6em)
      #text(size: 26pt, weight: "bold", fill: white, "{t}")
      {"#v(0.4em)" + chr(10) + f'      #text(size: 14pt, fill: sage-accent, style: "italic", "{sub}")' if subtitle else ""}
    ]
    #v(1.2em)
  ]
  #block(width: 100%, fill: white)[
    #pad(x: 3cm, y: 2cm)[
      #grid(columns: (1fr, auto), gutter: 1.5em,
        [
          #v(0.6em)
          #text(size: 10.5pt, fill: sage-muted)[*Prepared by*]
          #v(0.15em)
          #text(size: 13pt, weight: "bold", fill: sage-dark, "{au}")
          #v(0.9em)
          #text(size: 10.5pt, fill: sage-muted)[*Institution*]
          #v(0.15em)
          #text(size: 12pt, fill: sage-text, "{ins}")
          #v(0.9em)
          #text(size: 10.5pt, fill: sage-muted)[*Date*]
          #v(0.15em)
          #text(size: 12pt, fill: sage-text, "{dt}")
        ],
        [
          #block(width: 3.5cm, height: 3.5cm, fill: sage-light,
                 radius: 50%, stroke: 2pt + sage-accent)[
            #align(center + horizon)[
              #text(size: 9.5pt, weight: "bold", fill: sage-dark)[SAGE]
            ]
          ]
        ],
      )
      #v(2em)
      #line(length: 100%, stroke: 0.5pt + sage-muted.lighten(40%))
      #v(0.5em)
      #text(size: 8.5pt, fill: sage-muted, style: "italic")[
      This report was synthesised from multiple sources for educational purposes.
      ]
    ]
  ]
]
 
#counter(page).update(1)
 
// --- Table of contents ---
#outline(
  title: [
    #text(size: 16pt, weight: "bold", fill: sage-dark)[Contents]
    #v(0.3em)
    #line(length: 100%, stroke: 1.2pt + sage-mid)
    #v(0.6em)
  ],
  depth: 3, indent: auto,
)
#pagebreak()
 
// --- Body ---
{body}
"""


# --- Markdown Export ---
@tool
def export_markdown(content: str, filename: str) -> dict[str, Any]:
    """Export content as a Markdown (.md) file.

    Args:
        content: Markdown-formatted text content to export.
        filename: Desired filename (without extension; unsafe chars stripped).

    Returns:
        Dict with keys: success, operation, path, error, meta.
    """
    operation = "export_markdown"
    if not isinstance(content, str) or not content.strip():
        return _response(False, operation, error="No content provided")

    if len(content) > _MAX_CONTENT_LENGTH:
        return _response(False, operation, error=f"Content too long ({len(content)})")

    output_path = reserve_export_path(filename, ".md")

    try:
        output_path.write_text(content, encoding="utf-8")
        log.info("export_markdown_complete", path=str(output_path), length=len(content))
        return _response(True, operation, path=str(output_path), meta={"length": len(content)})

    except Exception as exc:
        log.error("export_markdown_failed", error=str(exc)[:200])
        return _response(False, operation, error=str(exc)[:200])


# --- PDF Export ---
@tool
async def export_pdf(
    content: str,
    filename: str,
    title: str = "",
    subtitle: str = "",
    author: str = "Sage Research Agent",
    date: str = "",
    institution: str = "",
) -> dict[str, Any]:
    """Export content as a professionally formatted PDF using Typst.

    Produces a cover page, table of contents, numbered sections,
    styled code blocks, and page footers.

    Requires the `typst` binary (path in `tools.export.typst_bin`).

    Args:
        content:     Markdown-formatted text to export.
        filename:    Desired filename (without extension).
        title:       Report title for the cover page.
        subtitle:    Optional subtitle for the cover page.
        author:      Author name for the cover page.
        date:        Date string (defaults to today).
        institution: Institution name for the cover page.

    Returns:
        Dict with keys: success, operation, path, error, meta.
    """
    operation = "export_pdf"

    if not isinstance(content, str) or not content.strip():
        return _response(False, operation, error="No content provided")

    if len(content) > _MAX_CONTENT_LENGTH:
        return _response(False, operation, error=f"Content too long ({len(content)})")

    output_path = reserve_export_path(filename, ".pdf")

    tmp_path: Path | None = None
    try:
        typst_source = _generate_typst_source(
            content,
            title=title,
            subtitle=subtitle,
            author=author,
            date=date,
            institution=institution,
        )
    except ValueError as exc:
        return _response(False, operation, error=str(exc))

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".typ",
            encoding="utf-8",
            delete=False,
        ) as tmp:
            tmp.write(typst_source)
            tmp_path = Path(tmp.name)

        typst_bin = _resolve_typst_bin()
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                typst_bin,
                "compile",
                str(tmp_path),
                str(output_path),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ),
            timeout=_PDF_TIMEOUT,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_PDF_TIMEOUT)

        if proc.returncode != 0:
            err_msg = stderr.decode("utf-8", errors="replace").strip()
            log.error("export_pdf_typst_error", returncode=proc.returncode, stderr=err_msg[:400])
            return _response(False, operation, error=f"Typst compile error: {err_msg[:300]}")

        log.info("export_pdf_complete", path=str(output_path), length=len(content))
        return _response(True, operation, path=str(output_path), meta={"length": len(content)})

    except FileNotFoundError:
        return _response(
            False,
            operation,
            error=(
                "Typst binary not found. Install typst (https://typst.app) "
                "and set tools.export.typst_bin in your config."
            ),
        )
    except TimeoutError:
        return _response(False, operation, error="PDF generation timed out")
    except Exception as exc:
        log.error("export_pdf_unexpected", error=str(exc)[:200])
        return _response(False, operation, error=str(exc)[:200])
    finally:
        if tmp_path and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
