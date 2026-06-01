"""
Corpus directory walker for Sage's preprocessing pipeline.

Discovers all valid source files under `raw/`, validates the enforced
directory hierarchy, and extracts document metadata purely
from path segments.

Expected directory structure:
    raw/
      <PROGRAM_CODE>/
        <SEMESTER>/
          <COURSE_CODE>_<Title>/
            <files>

Usage:
    from sage.rag.corpus import walk_raw_dir, DocumentMetadata
    for doc in walk_raw_dir(raw_dir):
        print(doc.course_code, doc.source_path)
"""

from __future__ import annotations

import re
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import structlog
from pydantic import BaseModel, Field, field_validator
from sage.config import get_settings

log = structlog.get_logger(__name__)


def _load_sage_version() -> str:
    module_path = Path(__file__).resolve()
    pyproject_path: Path | None = None
    for parent in module_path.parents:
        candidate = parent / "pyproject.toml"
        if candidate.exists():
            pyproject_path = candidate
            break

    if pyproject_path is None:
        log.warning("sage_version_fallback", reason="pyproject_not_found")
        return "0.0.0"

    try:
        with pyproject_path.open("rb") as f:
            data = tomllib.load(f)
        return str(data["project"]["version"])
    except (OSError, KeyError, TypeError):
        log.warning("sage_version_fallback", path=str(pyproject_path))
        return "0.0.0"


SAGE_VERSION: str = _load_sage_version()

_settings = get_settings()

_ALLOWED_EXTENSIONS: frozenset[str] = frozenset(_settings.corpus.allowed_extensions)
_LEGACY_EXTENSIONS: frozenset[str] = frozenset({".doc"})
_SKIP_PREFIXES: tuple[str, ...] = (".", "_")
_REQUIRED_DEPTH: int = 4
_PROGRAM_CODE_RE = re.compile(r"^[A-Z]{2,6}$")
_SEMESTER_RE = re.compile(r"^[1-8]$")
_COURSE_DIR_RE = re.compile(r"^([A-Z]{2,4}\d{3,4})[\s_\-]+(.+)$")
_MAX_FILE_SIZE_BYTES: int = _settings.preprocessing.max_file_size_mb * 1024 * 1024

class DocumentMetadata(BaseModel):
    """
    All metadata extractable from a `raw/` file path and filesystem stat."""

    model_config = {"extra": "ignore"}
    program_code: str
    semester: int = Field(ge=1, le=8)
    course_code: str
    course_title: str
    doc_title: str
    source_format: str
    source_path: str
    last_modified: str
    page_count: int = 0
    ocr_applied: bool = False
    ocr_engine: str = ""
    sage_version: str = SAGE_VERSION
    abs_path: Path = Field(default=Path("."), exclude=True)
    model_config = {"arbitrary_types_allowed": True, "extra": "ignore"}

    @field_validator("program_code")
    @classmethod
    def _validate_program(cls, v: str) -> str:
        if not _PROGRAM_CODE_RE.match(v):
            raise ValueError(f"Invalid program_code: {v!r}")
        return v

    @field_validator("source_format")
    @classmethod
    def _validate_format(cls, v: str) -> str:
        allowed = {"pdf", "pptx", "docx", "md", "txt"}
        if v not in allowed:
            raise ValueError(f"source_format must be one of {allowed}, got {v!r}")
        return v

def _extract_metadata(abs_path: Path, raw_root: Path) -> DocumentMetadata:
    """
    Parse metadata from a file's path.

    Raises:
        ValueError: On any parse failure caller logs ERROR and skips file.
    """
    rel = abs_path.relative_to(raw_root)
    parts = rel.parts

    if len(parts) != _REQUIRED_DEPTH:
        raise ValueError(
            f"Path depth {len(parts)} ≠ {_REQUIRED_DEPTH} "
            f"(expected PROGRAM/SEMESTER/COURSE/FILE)"
        )

    program_code, semester_str, course_dir, filename = parts

    # Validate program code
    if not _PROGRAM_CODE_RE.match(program_code):
        raise ValueError(
            f"PROGRAM_CODE {program_code!r} must be 2–6 uppercase alpha characters"
        )

    # Validate semester
    if not _SEMESTER_RE.match(semester_str):
        raise ValueError(
            f"SEMESTER {semester_str!r} must be an integer 1–8 with no leading zeros"
        )
    semester = int(semester_str)

    # Validate course directory name
    course_match = _COURSE_DIR_RE.match(course_dir)
    if not course_match:
        raise ValueError(
            f"COURSE directory {course_dir!r} must match COURSECODE_Title format "
            f"(separator may be underscore, space, or hyphen)"
        )
    course_code = course_match.group(1)
    course_title = course_match.group(2)

    # Document metadata from filename
    doc_title = abs_path.stem
    source_format = abs_path.suffix.lstrip(".").lower()

    # File modification time
    mtime = abs_path.stat().st_mtime
    last_modified = (
        datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    )

    # Normalise path separators
    source_path = str(abs_path.relative_to(raw_root.parent)).replace("\\", "/")

    return DocumentMetadata(
        program_code=program_code,
        semester=semester,
        course_code=course_code,
        course_title=course_title,
        doc_title=doc_title,
        source_format=source_format,
        source_path=source_path,
        last_modified=last_modified,
        abs_path=abs_path,
    )

def walk_raw_dir(raw_root: Path) -> Iterator[DocumentMetadata]:
    """
    Recursively discover all valid source documents under `raw_root`.

    Yields one :class:`DocumentMetadata` per accepted file.  Skips,
    warns, or logs errors inline so the pipeline can process the maximum number 
    of valid files even when some are broken.

    Args:
        raw_root: Absolute path to the `raw/` directory.

    Yields:
        :class:`DocumentMetadata` for every accepted file.
    """
    if not raw_root.is_dir():
        log.error(
            "walk_raw_dir_missing",
            raw_root=str(raw_root),
            hint="Create the raw/ directory and add course materials.",
        )
        return

    total_found = 0
    total_accepted = 0

    for path in sorted(raw_root.rglob("*")):
        if not path.is_file():
            continue

        total_found += 1
        name = path.name

        # Skip hidden and temp files
        if any(name.startswith(p) for p in _SKIP_PREFIXES):
            log.debug("walk_skip_hidden", path=str(path))
            continue

        ext = path.suffix.lower()

        # Warn on legacy .doc (never process)
        if ext in _LEGACY_EXTENSIONS:
            log.warning(
                "walk_legacy_format",
                path=str(path),
                hint="Convert .doc to .docx before re-running the pipeline.",
            )
            continue

        # Skip unsupported extensions silently
        if ext not in _ALLOWED_EXTENSIONS:
            log.debug("walk_skip_unsupported_ext", path=str(path), ext=ext)
            continue

        # Skip oversized files
        try:
            size_bytes = path.stat().st_size
        except OSError as exc:
            log.error(
                "walk_stat_failed",
                path=str(path),
                exc_type=type(exc).__name__,
                error=str(exc)[:200],
            )
            continue

        if size_bytes > _MAX_FILE_SIZE_BYTES:
            log.warning(
                "walk_file_too_large",
                path=str(path),
                size_mb=round(size_bytes / (1024 * 1024), 1),
                limit_mb=200,
                hint="Split the file manually before re-running.",
            )
            continue

        # Validate path depth and extract metadata
        try:
            meta = _extract_metadata(path, raw_root)
        except ValueError as exc:
            log.error(
                "walk_metadata_parse_failed",
                path=str(path),
                error=str(exc)[:200],
            )
            continue

        total_accepted += 1
        log.debug(
            "walk_accepted",
            path=str(path),
            program=meta.program_code,
            semester=meta.semester,
            course=meta.course_code,
        )
        yield meta

    log.info(
        "walk_complete",
        raw_root=str(raw_root),
        total_found=total_found,
        total_accepted=total_accepted,
        total_skipped=total_found - total_accepted,
    )