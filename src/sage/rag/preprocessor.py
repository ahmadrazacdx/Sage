"""
Mandatory text preprocessing pipeline for Sage.
Stage overview:
  1. Unicode normalisation
  2. Whitespace normalisation
  3. Header / footer removal
  4. Encoding repair (ftfy)
  5. TOC & index removal
  6. Reference / bibliography isolation
  7. Math expression preservation
  8. Content-length gate (returns None on empty)

Usage:
    from sage.rag.preprocessor import run_preprocessing_pipeline
    result = run_preprocessing_pipeline(raw_text, source_file="lecture.pdf")
    if result is None:
        # document is empty —> skip
        ...
    clean_text, references = result
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from typing import Optional

import structlog

log = structlog.get_logger(__name__)
_FTFY_WARNED: bool = False

# Stage 1: zero-width characters to strip
_ZERO_WIDTH_CHARS: tuple[str, ...] = ("\u200b", "\u200c", "\u200d", "\ufeff")

# Stage 1: Fancy quote / dash replacements
_UNICODE_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("\u2018", "'"),
    ("\u2019", "'"),
    ("\u201c", '"'),
    ("\u201d", '"'),
    ("\u2013", "-"),
    ("\u2014", "--"),
    ("\u2026", "..."),
    ("\u00a0", " "),
)

# Stage 3: Page-number patterns
_PAGE_NUMBER_RE = re.compile(
    r"^\s*(?:page\s*)?\d+\s*(?:of\s*\d+)?\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Stage 3: Repeated identical short lines across ≥3 occurrences
_MIN_REPEAT_COUNT: int = 3
_MAX_HEADER_LINE_LEN: int = 120

# Stage 5: TOC detection
_TOC_LINE_RE = re.compile(r"^.{5,}[.\s]{4,}\d+\s*$", re.MULTILINE)
_TOC_MIN_LINE_RATIO: float = 0.40  # ≥40% TOC lines to classify a block as TOC

# Stage 6: Bibliography section headings
_REF_HEADING_RE = re.compile(
    r"^#{0,3}\s*(?:references|bibliography|works\s+cited|further\s+reading)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
# Citation line patterns
_CITATION_LINE_RE = re.compile(
    r"^\s*(?:\[\d+\]|\(\w[^)]{1,60}\d{4}\)|\d+\.\s+\w)",
    re.MULTILINE,
)
_MIN_CITATION_LINES: int = 3

# Stage 7: Math expression heuristics
_INLINE_MATH_RE = re.compile(r"\$[^$\n]{1,200}\$")
_PAREN_MATH_RE = re.compile(r"\\\([^)]{1,200}\\\)")
_MATH_SYMBOLS_RE = re.compile(r"[∑∫∂∇∆∏√≤≥≠≈±×÷∈∉⊂⊃∪∩]{2,}")

# Stage 8: Minimum word count for a non-empty document
_MIN_WORD_COUNT: int = 30

def stage1_unicode_normalisation(text: str) -> str:
    """
    Apply NFC normalisation, remove zero-width characters,
    and replace fancy quotes and dashes with ASCII equivalents.
    """
    text = unicodedata.normalize("NFC", text)
    for ch in _ZERO_WIDTH_CHARS:
        text = text.replace(ch, "")
    for fancy, plain in _UNICODE_REPLACEMENTS:
        text = text.replace(fancy, plain)
    return text


def stage2_whitespace_normalisation(text: str) -> str:
    """
    Collapse runs of >2 newlines to double-newline.
    Collapse horizontal whitespace to single space.
    Strip trailing whitespace from every line.
    """
    # Normalise line endings first
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Strip trailing whitespace from each line
    lines = [line.rstrip() for line in text.split("\n")]
    text = "\n".join(lines)
    # Collapse tabs and multiple spaces to single space within a line
    text = re.sub(r"[ \t]+", " ", text)
    # Collapse runs of more than 2 newlines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def stage3_header_footer_removal(text: str) -> str:
    """
    Remove page-number lines and repeating running headers or watermarks.
    Preserves document-level headings.
    """
    # Remove bare page-number lines
    text = _PAGE_NUMBER_RE.sub("", text)

    # Detect repeating short lines across the full text
    lines = text.split("\n")
    short_lines = [
        ln.strip()
        for ln in lines
        if 1 < len(ln.strip()) <= _MAX_HEADER_LINE_LEN
        and not ln.strip().startswith("#")
    ]
    counts = Counter(short_lines)
    repeated: frozenset[str] = frozenset(
        ln for ln, cnt in counts.items() if cnt >= _MIN_REPEAT_COUNT
    )

    if repeated:
        cleaned_lines = [
            ln for ln in lines if ln.strip() not in repeated
        ]
        text = "\n".join(cleaned_lines)
        log.debug(
            "header_footer_removed",
            removed_patterns=len(repeated),
        )

    return text


def stage4_encoding_repair(text: str) -> str:
    """
    Detect and fix Mojibake using ftfy.  Gracefully skips if ftfy
    is not installed (non-fatal; logs a warning once).
    """
    try:
        import ftfy  # type: ignore[import-untyped]

        return ftfy.fix_text(text)  # type: ignore[no-any-return]
    except ImportError:
            log.warning(
                "ftfy_unavailable",
                hint="Install ftfy for Mojibake repair: uv add ftfy",
            )
    return text


def stage5_toc_removal(text: str) -> str:
    """Remove table-of-contents and index blocks."""
    paragraphs = re.split(r"\n{2,}", text)
    cleaned: list[str] = []

    for para in paragraphs:
        lines = [ln for ln in para.split("\n") if ln.strip()]
        if not lines:
            continue
        toc_lines = _TOC_LINE_RE.findall(para)
        ratio = len(toc_lines) / len(lines)
        if ratio >= _TOC_MIN_LINE_RATIO and len(lines) >= 3:
            log.debug("toc_paragraph_removed", lines=len(lines), toc_ratio=round(ratio, 2))
            continue
        cleaned.append(para)

    return "\n\n".join(cleaned)


def stage6_reference_isolation(text: str) -> tuple[str, str]:
    """Detect and isolate the references or bibliography section."""
    heading_match = _REF_HEADING_RE.search(text)
    if heading_match is None:
        return text, ""

    ref_section = text[heading_match.start():]
    citation_count = len(_CITATION_LINE_RE.findall(ref_section))

    if citation_count < _MIN_CITATION_LINES:
        return text, ""

    main_text = text[: heading_match.start()].rstrip()
    references_text = ref_section.strip()
    log.debug(
        "references_isolated",
        heading=heading_match.group().strip(),
        citation_lines=citation_count,
    )
    return main_text, references_text


def stage7_math_preservation(text: str) -> str:
    """Wrap detected inline math expressions in LaTeX fence markers."""

    def _wrap_symbol_sequence(match: re.Match) -> str:  # type: ignore[type-arg]
        return f"${match.group(0)}$"

    protected = text
    placeholders: dict[str, str] = {}
    counter = 0
    for pat in (_INLINE_MATH_RE, _PAREN_MATH_RE):
        def _placeholder(m: re.Match, c: list[int] = [counter]) -> str:  # noqa: B023
            key = f"\x00MATHPH{c[0]}\x00"
            placeholders[key] = m.group(0)
            c[0] += 1
            return key
        protected = pat.sub(_placeholder, protected)

    # Wrap bare symbol sequences
    protected = _MATH_SYMBOLS_RE.sub(_wrap_symbol_sequence, protected)

    # Restore placeholders
    for key, original in placeholders.items():
        protected = protected.replace(key, original)

    return protected


def stage8_content_length_gate(text: str, source_file: str = "") -> Optional[str]:
    """Return text unchanged if it has ≥30 words, else return None."""
    word_count = len(text.split())
    if word_count < _MIN_WORD_COUNT:
        log.warning(
            "content_gate_empty_document",
            source_file=source_file,
            word_count=word_count,
            threshold=_MIN_WORD_COUNT,
        )
        return None
    return text


# Pipeline Composer
def run_preprocessing_pipeline(
    raw_text: str,
    source_file: str = "",
) -> Optional[tuple[str, str]]:
    """
    Apply all preprocessing stages in strict order.

    Args:
        raw_text:    Raw extracted text (joined from all PageBlocks).
        source_file: Original filename for logging context only.

    Returns:
        `(cleaned_text, references_text)` on success.
        `None` when the document is empty after cleaning.
    """
    if not raw_text or not raw_text.strip():
        log.warning("preprocess_empty_input", source_file=source_file)
        return None

    text = raw_text

    # Stage 1
    text = stage1_unicode_normalisation(text)

    # Stage 2
    text = stage2_whitespace_normalisation(text)

    # Stage 3
    text = stage3_header_footer_removal(text)

    # Stage 4
    text = stage4_encoding_repair(text)

    # Stage 5
    text = stage5_toc_removal(text)

    # Stage 6
    text, references = stage6_reference_isolation(text)

    # Stage 7
    text = stage7_math_preservation(text)

    # Stage 8
    gated = stage8_content_length_gate(text, source_file=source_file)
    if gated is None:
        return None

    log.debug(
        "preprocessing_complete",
        source_file=source_file,
        words=len(gated.split()),
        has_references=bool(references),
    )
    return gated, references