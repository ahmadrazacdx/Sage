import pytest
from pathlib import Path
import tempfile
import structlog
from pydantic import ValidationError
from sage.rag.corpus import (
    DocumentMetadata,
    _extract_metadata,
    _load_sage_version,
    walk_raw_dir,
    _MAX_FILE_SIZE_BYTES
)

def test_document_metadata_validation():
    doc = DocumentMetadata(
        program_code="CS",
        semester=1,
        course_code="CS101",
        course_title="Intro",
        doc_title="Lec1",
        source_format="pdf",
        source_path="CS/1/CS101_Intro/Lec1.pdf",
        last_modified="2023-01-01T00:00:00Z"
    )
    assert doc.program_code == "CS"
    with pytest.raises(ValidationError):
        DocumentMetadata(
            program_code="123",
            semester=1,
            course_code="CS101",
            course_title="Intro",
            doc_title="Lec1",
            source_format="pdf",
            source_path="CS/1/CS101_Intro/Lec1.pdf",
            last_modified="2023-01-01T00:00:00Z"
        )
    with pytest.raises(ValidationError):
        DocumentMetadata(
            program_code="CS",
            semester=9,
            course_code="CS101",
            course_title="Intro",
            doc_title="Lec1",
            source_format="pdf",
            source_path="CS/1/CS101_Intro/Lec1.pdf",
            last_modified="2023-01-01T00:00:00Z"
        )
    with pytest.raises(ValidationError):
        DocumentMetadata(
            program_code="CS",
            semester=1,
            course_code="CS101",
            course_title="Intro",
            doc_title="Lec1",
            source_format="exe",
            source_path="CS/1/CS101_Intro/Lec1.exe",
            last_modified="2023-01-01T00:00:00Z"
        )

def test_load_sage_version(tmp_path):
    version = _load_sage_version()
    assert isinstance(version, str)

def test_extract_metadata(tmp_path):
    raw_root = tmp_path / "raw"
    course_dir = raw_root / "CS" / "1" / "CS101_Intro"
    course_dir.mkdir(parents=True)
    
    file_path = course_dir / "Lec1.pdf"
    file_path.write_text("dummy")
    
    meta = _extract_metadata(file_path, raw_root)
    assert meta.program_code == "CS"
    assert meta.semester == 1
    assert meta.course_code == "CS101"
    assert meta.course_title == "Intro"
    assert meta.doc_title == "Lec1"
    assert meta.source_format == "pdf"

    shallow = raw_root / "CS" / "Lec1.pdf"
    shallow.parent.mkdir(exist_ok=True)
    shallow.write_text("dummy")
    with pytest.raises(ValueError, match="Path depth"):
        _extract_metadata(shallow, raw_root)

    bad_prog = raw_root / "c" / "1" / "CS101_Intro" / "Lec.pdf"
    bad_prog.parent.mkdir(parents=True, exist_ok=True)
    bad_prog.write_text("dummy")
    with pytest.raises(ValueError, match="PROGRAM_CODE"):
        _extract_metadata(bad_prog, raw_root)

    bad_sem = raw_root / "CS" / "9" / "CS101_Intro" / "Lec.pdf"
    bad_sem.parent.mkdir(parents=True, exist_ok=True)
    bad_sem.write_text("dummy")
    with pytest.raises(ValueError, match="SEMESTER"):
        _extract_metadata(bad_sem, raw_root)
    bad_course = raw_root / "CS" / "1" / "CS101Intro" / "Lec.pdf"
    bad_course.parent.mkdir(parents=True, exist_ok=True)
    bad_course.write_text("dummy")
    with pytest.raises(ValueError, match="COURSE directory"):
        _extract_metadata(bad_course, raw_root)

def test_walk_raw_dir(tmp_path):
    raw_root = tmp_path / "raw"
    assert list(walk_raw_dir(raw_root)) == []

    raw_root.mkdir()
    
    course_dir = raw_root / "CS" / "1" / "CS101_Intro"
    course_dir.mkdir(parents=True)
    
    valid_pdf = course_dir / "Lec1.pdf"
    valid_pdf.write_text("dummy")

    valid_md = course_dir / "Notes.md"
    valid_md.write_text("dummy")

    hidden = course_dir / ".hidden.pdf"
    hidden.write_text("dummy")

    legacy = course_dir / "old.doc"
    legacy.write_text("dummy")

    unsupported = course_dir / "audio.mp3"
    unsupported.write_text("dummy")
    docs = list(walk_raw_dir(raw_root))
    assert len(docs) == 2
    formats = {doc.source_format for doc in docs}
    assert formats == {"pdf", "md"}
    with patch("sage.rag.corpus._MAX_FILE_SIZE_BYTES", 2):
        valid_pdf.write_text("too large string")
        docs_small = list(walk_raw_dir(raw_root))
        assert len(docs_small) == 0

import sys
from unittest.mock import patch

def test_walk_raw_dir_exceptions(tmp_path):
    raw_root = tmp_path / "raw"
    course_dir = raw_root / "CS" / "1" / "CS101_Intro"
    course_dir.mkdir(parents=True)
    
    valid_pdf = course_dir / "Lec1.pdf"
    valid_pdf.write_text("dummy")
    original_stat = Path.stat
    def mock_stat(self, *args, **kwargs):
        if "Lec1.pdf" in self.name:
            frame = sys._getframe(1)
            while frame and frame.f_code.co_name not in ("walk_raw_dir", "is_file"):
                frame = frame.f_back
            if frame and frame.f_code.co_name == "walk_raw_dir":
                raise OSError("fake error")
        return original_stat(self, *args, **kwargs)

    with patch("pathlib.Path.stat", side_effect=mock_stat, autospec=True):
        assert list(walk_raw_dir(raw_root)) == []

    with patch("sage.rag.corpus._extract_metadata", side_effect=ValueError("fake parse error")):
        assert list(walk_raw_dir(raw_root)) == []

