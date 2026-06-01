"""
Sage Preprocessing Pipeline.

Ingests every supported document under `raw/`, applies the 8-stage
cleaning pipeline, injects YAML front-matter, and writes one .md
file per source document to `processed/`.

Trigger:
    uv run python scripts/preprocess.py [OPTIONS]

Options:
    --raw-dir     PATH   Override `raw/` location
    --out-dir     PATH   Override `processed/` location
    --force              Reprocess all files, ignoring mtime cache
    --dry-run            Log what would be processed without writing files
    --course      CODE   Process only files matching this course code
    --workers     N      Override worker count for this run
    --log-level   LEVEL  debug | info | warning | error
"""

from __future__ import annotations

import argparse
import os
import sys
import tomllib
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import structlog
import yaml

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT / "src"))

from sage.config import get_settings
from sage.rag.corpus import DocumentMetadata, walk_raw_dir
from sage.rag.parsers import ParsedDocument, dispatch_parser
from sage.rag.preprocessor import run_preprocessing_pipeline
from sage.utils import configure_logging

log = structlog.get_logger(__name__)

_MAX_WORKERS_WINDOWS: int = 4

def _default_worker_count(configured: int) -> int:
    """Return a safe worker count respecting the OS-specific cap."""
    cpu_count = os.cpu_count() or 2
    if configured > 0:
        raw = configured
    else:
        raw = max(1, cpu_count - 1)

    cap = _MAX_WORKERS_WINDOWS if sys.platform == "win32" else _MAX_WORKERS_UNIX
    return min(raw, cap)

def _load_sage_version() -> str:
    script_path = Path(__file__).resolve()
    pyproject_path: Optional[Path] = None
    for parent in script_path.parents:
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
_FM_DELIMITER: str = "---"
# YAML front-matter serialisation
def _build_front_matter(meta: DocumentMetadata) -> str:
    """
    Serialise DocumentMetadata into a YAML front-matter block.
    Produces a block identical to:
        ---
        program_code: BSCS
        ...
        ---
    """
    data: dict[str, object] = {
        "program_code": meta.program_code,
        "semester": meta.semester,
        "course_code": meta.course_code,
        "course_title": meta.course_title,
        "doc_title": meta.doc_title,
        "source_format": meta.source_format,
        "source_path": meta.source_path,
        "last_modified": meta.last_modified,
        "page_count": meta.page_count,
        "ocr_applied": meta.ocr_applied,
        "ocr_engine": meta.ocr_engine if meta.ocr_applied else "",
        "sage_version": meta.sage_version,
    }
    yaml_str = yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)
    return f"{_FM_DELIMITER}\n{yaml_str}{_FM_DELIMITER}\n"

# Atomic file write
def _atomic_write(output_path: Path, content: str) -> None:
    """
    Write content to output_path atomically via a .tmp rename.

    Prevents partially-written files from being picked up as valid
    output on a crash mid-write.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(".tmp")
    try:
        tmp_path.write_text(content, encoding="utf-8")
        tmp_path.replace(output_path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


# Output path resolution
def _resolve_output_path(meta: DocumentMetadata, raw_root: Path, out_root: Path) -> Path:
    """
    Mirror the raw/ subtree under out_root/, changing the extension to .md.

    Example:
        raw/BSCS/3/CS301_Data_Structures/Lecture_05.pdf
        → processed/BSCS/3/CS301_Data_Structures/Lecture_05.md
    """
    rel = Path(meta.source_path)
    parts = rel.parts
    mirrored = Path(*parts[1:]) if parts[0].lower() == "raw" else rel
    return out_root / mirrored.with_suffix(".md")


def _needs_processing(
    meta: DocumentMetadata,
    output_path: Path,
    force: bool,
) -> bool:
    """Return True if the file should be (re)processed."""
    if force:
        return True
    if not output_path.exists():
        return True
    try:
        src_mtime = meta.abs_path.stat().st_mtime
        out_mtime = output_path.stat().st_mtime
        return src_mtime > out_mtime
    except OSError:
        return True


def _render_ascii_table(headers: list[str], rows: list[list[object]]) -> str:
    """Render a simple ASCII table for terminal summaries."""
    string_rows = [[str(cell) for cell in row] for row in rows]
    widths = [len(h) for h in headers]

    for row in string_rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def _fmt_row(values: list[str]) -> str:
        padded = [val.ljust(widths[i]) for i, val in enumerate(values)]
        return f"| {' | '.join(padded)} |"

    rule = f"+-{'-+-'.join('-' * w for w in widths)}-+"
    lines = [rule, _fmt_row(headers), rule]
    for row in string_rows:
        lines.append(_fmt_row(row))
    lines.append(rule)
    return "\n".join(lines)


def _emit_terminal_summary(
    discovered_total: int,
    queued_total: int,
    duplicate_name_skipped_total: int,
    status_counts: Counter[str],
    ocr_applied_total: int,
    discovered_by_type: Counter[str],
    queued_by_type: Counter[str],
    duplicate_by_type: Counter[str],
    status_by_type: dict[str, Counter[str]],
) -> None:
    """Print a human-readable summary to the terminal."""
    ok = status_counts["ok"]
    skipped_up_to_date = status_counts["skipped"]
    skipped_ocr = status_counts["skipped_ocr"]
    empty = status_counts["empty"]
    errors = status_counts["error"]

    success_rate = (ok / queued_total * 100.0) if queued_total else 0.0

    type_headers = [
        "Type",
        "Found",
        "Queued",
        "DupSkip",
        "OK",
        "Skip",
        "SkipOCR",
        "Empty",
        "Error",
    ]

    all_types = sorted(
        set(discovered_by_type)
        | set(queued_by_type)
        | set(duplicate_by_type)
        | set(status_by_type)
    )

    type_rows: list[list[object]] = []
    for fmt in all_types:
        per_type_status = status_by_type.get(fmt, Counter())
        type_rows.append(
            [
                fmt.upper(),
                discovered_by_type.get(fmt, 0),
                queued_by_type.get(fmt, 0),
                duplicate_by_type.get(fmt, 0),
                per_type_status.get("ok", 0),
                per_type_status.get("skipped", 0),
                per_type_status.get("skipped_ocr", 0),
                per_type_status.get("empty", 0),
                per_type_status.get("error", 0),
            ]
        )

    overview_headers = ["Metric", "Count"]
    overview_rows = [
        ["Discovered files", discovered_total],
        ["Queued unique filenames", queued_total],
        ["Skipped duplicate filenames", duplicate_name_skipped_total],
        ["Successful extractions", ok],
        ["Skipped (up-to-date)", skipped_up_to_date],
        ["Skipped (OCR unavailable/failure)", skipped_ocr],
        ["Empty after preprocessing", empty],
        ["Errors", errors],
        ["OCR applied", ocr_applied_total],
        ["Success rate", f"{success_rate:.1f}%"],
    ]

    summary_lines = [
        "",
        "=" * 88,
        "SAGE PREPROCESS SUMMARY",
        "=" * 88,
        _render_ascii_table(overview_headers, overview_rows),
        _render_ascii_table(type_headers, type_rows) if type_rows else "No file types found.",
        "=" * 88,
        "",
    ]
    print("\n".join(summary_lines), flush=True)


def _process_one_file(
    meta_dict: dict,
    raw_root_str: str,
    out_root_str: str,
    dry_run: bool,
    force: bool,
    log_level: str,
) -> dict:
    """
    Complete preprocessing pipeline for a single document.

    This function runs inside a worker process.  It must be a
    module-level callable (pickle-safe).

    Returns a result dict:
        status: "ok" | "skipped" | "skipped_ocr" | "empty" | "error"
        source_path: str
        output_path: str | None
        source_format: str
        ocr_applied: bool
        error: str | None
    """
    # Re-configure logging inside the worker process
    configure_logging(log_level)

    raw_root = Path(raw_root_str)
    out_root = Path(out_root_str)

    # Reconstruct DocumentMetadata from plain dict (crosses process boundary)
    meta = DocumentMetadata(abs_path=Path(meta_dict["abs_path"]), **{
        k: v for k, v in meta_dict.items() if k != "abs_path"
    })

    output_path = _resolve_output_path(meta, raw_root, out_root)

    if not _needs_processing(meta, output_path, force):
        log.info("file_skipped_up_to_date", source=meta.source_path)
        return {
            "status": "skipped",
            "source_path": meta.source_path,
            "output_path": str(output_path),
            "source_format": meta.source_format,
            "ocr_applied": False,
            "error": None,
        }

    t0 = time.perf_counter()
    cfg = get_settings().preprocessing

    # --- Step 2–4: Parse ---
    parsed: Optional[ParsedDocument] = dispatch_parser(
        meta,
        min_chars_per_page=cfg.min_chars_per_page,
        ocr_engine=cfg.ocr_engine,
        ocr_dpi=cfg.ocr_dpi,
        ocr_language=cfg.ocr_language,
    )

    if parsed is None:
        return {
            "status": "error",
            "source_path": meta.source_path,
            "output_path": None,
            "source_format": meta.source_format,
            "ocr_applied": False,
            "error": "Parser returned None",
        }

    if parsed.ocr_failed:
        log.warning(
            "file_skipped_ocr_failure",
            source=meta.source_path,
            hint=(
                "OCR was required but failed (for example: missing Poppler/Tesseract). "
                "Install OCR dependencies, then rerun."
            ),
        )
        return {
            "status": "skipped_ocr",
            "source_path": meta.source_path,
            "output_path": None,
            "source_format": meta.source_format,
            "ocr_applied": False,
            "error": "OCR required but failed",
        }

    meta.page_count = parsed.page_count
    meta.ocr_applied = parsed.ocr_applied
    meta.ocr_engine = parsed.ocr_engine
    raw_text = parsed.full_text()

    # Step 5: 8-stage mechanical preprocessing
    preprocess_result = run_preprocessing_pipeline(
        raw_text, source_file=meta.source_path
    )

    if preprocess_result is None:
        log.warning("file_empty_after_preprocessing", source=meta.source_path)
        return {
            "status": "empty",
            "source_path": meta.source_path,
            "output_path": None,
            "source_format": meta.source_format,
            "ocr_applied": meta.ocr_applied,
            "error": None,
        }

    cleaned_text, references = preprocess_result

    # Steps 6: Assemble and write
    front_matter = _build_front_matter(meta)

    body_parts = [cleaned_text]
    if references:
        body_parts.append(f"\n## References\n\n{references}")

    full_content = front_matter + "\n" + "\n".join(body_parts)

    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    if dry_run:
        log.info(
            "dry_run_would_write",
            source=meta.source_path,
            output=str(output_path),
            elapsed_ms=elapsed_ms,
            ocr=meta.ocr_applied,
        )
        return {
            "status": "ok",
            "source_path": meta.source_path,
            "output_path": str(output_path),
            "source_format": meta.source_format,
            "ocr_applied": meta.ocr_applied,
            "error": None,
        }

    try:
        _atomic_write(output_path, full_content)
    except Exception as exc:
        log.error(
            "file_write_failed",
            output=str(output_path),
            exc_type=type(exc).__name__,
            error=str(exc)[:200],
        )
        raise SystemExit(1) from exc

    log.info(
        "file_processed",
        source=meta.source_path,
        output=str(output_path),
        pages=meta.page_count,
        ocr=meta.ocr_applied,
        elapsed_ms=elapsed_ms,
    )

    return {
        "status": "ok",
        "source_path": meta.source_path,
        "output_path": str(output_path),
        "source_format": meta.source_format,
        "ocr_applied": meta.ocr_applied,
        "error": None,
    }


# Pipeline orchestrator
def run_pipeline(
    raw_root: Path,
    out_root: Path,
    dry_run: bool,
    force: bool,
    course_filter: Optional[str],
    worker_count: int,
    log_level: str,
) -> int:
    """
    Discover all source documents and process them in parallel.

    Returns:
        Exit code: 0 on success, 1 on critical failure.
    """
    try:
        from tqdm import tqdm  # type: ignore[import-untyped]
    except ImportError:
        log.warning("tqdm_unavailable", hint="Install tqdm for progress bars: uv add tqdm")

        class tqdm:  # type: ignore[no-redef]
            def __init__(self, it, **_):
                self._it = it

            def __iter__(self):
                return iter(self._it)

            def update(self, *_):
                pass

            def set_postfix_str(self, *_):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass
    
    # Collect all valid document metadata
    all_docs = list(walk_raw_dir(raw_root))

    if course_filter:
        all_docs = [d for d in all_docs if d.course_code.upper() == course_filter.upper()]
        log.info("course_filter_applied", course=course_filter, remaining=len(all_docs))

    discovered_by_type: Counter[str] = Counter(d.source_format.lower() for d in all_docs)

    # Skip duplicate filenames globally (across programs/semesters/courses).
    deduped_docs: list[DocumentMetadata] = []
    seen_filename_to_source: dict[str, str] = {}
    duplicate_by_type: Counter[str] = Counter()
    duplicate_name_skipped_total = 0

    for doc in all_docs:
        filename_key = doc.abs_path.name.casefold()
        if filename_key in seen_filename_to_source:
            duplicate_name_skipped_total += 1
            duplicate_by_type[doc.source_format.lower()] += 1
            log.info(
                "file_skipped_duplicate_filename",
                source=doc.source_path,
                duplicate_of=seen_filename_to_source[filename_key],
                filename=doc.abs_path.name,
            )
            continue
        seen_filename_to_source[filename_key] = doc.source_path
        deduped_docs.append(doc)

    queued_by_type: Counter[str] = Counter(d.source_format.lower() for d in deduped_docs)

    if not deduped_docs:
        log.info("no_documents_found", raw_root=str(raw_root))
        _emit_terminal_summary(
            discovered_total=len(all_docs),
            queued_total=0,
            duplicate_name_skipped_total=duplicate_name_skipped_total,
            status_counts=Counter(),
            ocr_applied_total=0,
            discovered_by_type=discovered_by_type,
            queued_by_type=queued_by_type,
            duplicate_by_type=duplicate_by_type,
            status_by_type={},
        )
        return 0

    log.info(
        "pipeline_starting",
        total_documents=len(deduped_docs),
        duplicate_name_skipped=duplicate_name_skipped_total,
        workers=worker_count,
        raw_root=str(raw_root),
        out_root=str(out_root),
        dry_run=dry_run,
    )

    # Serialise metadata to plain dicts for cross-process pickling
    meta_dicts = [
        {**doc.model_dump(exclude={"abs_path"}), "abs_path": str(doc.abs_path)}
        for doc in deduped_docs
    ]
    worker_kwargs = dict(
        raw_root_str=str(raw_root),
        out_root_str=str(out_root),
        dry_run=dry_run,
        force=force,
        log_level=log_level,
    )
    n_ok = n_skipped = n_skipped_ocr = n_empty = n_error = 0
    ocr_applied_count = 0
    status_counts: Counter[str] = Counter()
    status_by_type: dict[str, Counter[str]] = defaultdict(Counter)

    with ProcessPoolExecutor(max_workers=worker_count or None) as executor:
        futures = {
            executor.submit(_process_one_file, md, **worker_kwargs): (
                md["source_path"],
                str(md.get("source_format", "unknown")).lower(),
            )
            for md in meta_dicts
        }

        with tqdm(total=len(futures), desc="Preprocessing", unit="file") as bar:
            for future in as_completed(futures):
                source_path, source_format = futures[future]
                try:
                    result = future.result()
                    status: str = result["status"]
                    result_format = str(result.get("source_format", source_format)).lower()
                    status_counts[status] += 1
                    status_by_type[result_format][status] += 1
                    if result.get("ocr_applied"):
                        ocr_applied_count += 1
                    if status == "ok":
                        n_ok += 1
                    elif status == "skipped":
                        n_skipped += 1
                    elif status == "skipped_ocr":
                        n_skipped_ocr += 1
                    elif status == "empty":
                        n_empty += 1
                    else:
                        n_error += 1
                        log.error(
                            "pipeline_file_error",
                            source=source_path,
                            error=result.get("error", "unknown"),
                        )
                except Exception as exc:
                    n_error += 1
                    status_counts["error"] += 1
                    status_by_type[source_format]["error"] += 1
                    log.error(
                        "pipeline_future_exception",
                        source=source_path,
                        exc_type=type(exc).__name__,
                        error=str(exc)[:300],
                    )
                finally:
                    bar.update(1)
                    bar.set_postfix_str(
                        (
                            f"ok={n_ok} skip={n_skipped} "
                            f"ocrskip={n_skipped_ocr} empty={n_empty} err={n_error}"
                        )
                    )

    log.info(
        "pipeline_complete",
        total=len(deduped_docs),
        discovered=len(all_docs),
        ok=n_ok,
        skipped=n_skipped,
        skipped_ocr=n_skipped_ocr,
        duplicate_name_skipped=duplicate_name_skipped_total,
        empty=n_empty,
        errors=n_error,
    )

    _emit_terminal_summary(
        discovered_total=len(all_docs),
        queued_total=len(deduped_docs),
        duplicate_name_skipped_total=duplicate_name_skipped_total,
        status_counts=status_counts,
        ocr_applied_total=ocr_applied_count,
        discovered_by_type=discovered_by_type,
        queued_by_type=queued_by_type,
        duplicate_by_type=duplicate_by_type,
        status_by_type=status_by_type,
    )

    return 0 if n_error == 0 else 1


# CLI
def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="preprocess",
        description="Sage preprocessing pipeline: raw/ → processed/",
    )
    p.add_argument("--raw-dir", type=Path, default=None, help="Override raw/ directory")
    p.add_argument("--out-dir", type=Path, default=None, help="Override processed/ directory")
    p.add_argument(
        "--force",
        action="store_true",
        help="Reprocess all files, ignoring mtime cache",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Log what would be processed without writing any files",
    )
    p.add_argument(
        "--course",
        type=str,
        default=None,
        metavar="CODE",
        help="Process only documents matching this course code (e.g. CS301)",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=None,
        help="Override worker count",
    )
    p.add_argument(
        "--log-level",
        choices=["debug", "info", "warning", "error"],
        default="info",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    configure_logging(args.log_level)

    cfg = get_settings()
    preprocess_cfg = cfg.preprocessing
    data_root = cfg.app.data_dir
    if not data_root.is_absolute():
        data_root = _PROJECT_ROOT / data_root

    # Resolve directories
    raw_root: Path = args.raw_dir or (data_root / "raw")
    out_root: Path = args.out_dir or (data_root / "processed")

    # Resolve worker count
    worker_count: int = _default_worker_count(
        args.workers if args.workers is not None else preprocess_cfg.workers
    )

    log.info(
        "preprocess_startup",
        raw_root=str(raw_root),
        out_root=str(out_root),
        force=args.force,
        dry_run=args.dry_run,
        course_filter=args.course,
        workers=worker_count,
        platform=sys.platform,
    )

    exit_code = run_pipeline(
        raw_root=raw_root,
        out_root=out_root,
        dry_run=args.dry_run,
        force=args.force,
        course_filter=args.course,
        worker_count=worker_count,
        log_level=args.log_level,
    )

    sys.exit(exit_code)


if __name__ == "__main__":
    main()