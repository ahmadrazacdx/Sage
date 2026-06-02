import sys
from unittest.mock import MagicMock, patch

mock_fitz = MagicMock()
mock_pdfplumber = MagicMock()
mock_pytesseract = MagicMock()
mock_pdf2image = MagicMock()
mock_easyocr = MagicMock()
mock_pptx = MagicMock()
mock_python_docx = MagicMock()
mock_chardet = MagicMock()

sys.modules['fitz'] = mock_fitz
sys.modules['pdfplumber'] = mock_pdfplumber
sys.modules['pytesseract'] = mock_pytesseract
sys.modules['pdf2image'] = mock_pdf2image
sys.modules['easyocr'] = mock_easyocr
sys.modules['pptx'] = mock_pptx
sys.modules['pptx.util'] = MagicMock()
sys.modules['docx'] = mock_python_docx
sys.modules['chardet'] = mock_chardet

import pytest
from pathlib import Path
from sage.rag.corpus import DocumentMetadata
from sage.rag.parsers import (
    dispatch_parser,
    parse_pdf,
    parse_pptx,
    parse_docx,
    parse_text,
    _validate_pdf_magic,
    _needs_ocr,
    _apply_ocr_ligature_cleanup,
    PageBlock
)

@pytest.fixture
def dummy_meta():
    return DocumentMetadata(
        program_code="CS",
        semester=1,
        course_code="CS101",
        course_title="Intro",
        doc_title="Lec1",
        source_format="pdf",
        source_path="CS/1/CS101_Intro/Lec1.pdf",
        last_modified="2023-01-01T00:00:00Z"
    )

def test_validate_pdf_magic(tmp_path):
    valid_pdf = tmp_path / "valid.pdf"
    valid_pdf.write_bytes(b"%PDF-1.4\n...")
    assert _validate_pdf_magic(valid_pdf) is True

    invalid_pdf = tmp_path / "invalid.pdf"
    invalid_pdf.write_bytes(b"NOTPDF")
    assert _validate_pdf_magic(invalid_pdf) is False

def test_needs_ocr():
    assert _needs_ocr([], 50) is True
    
    blocks = [PageBlock(1, ""), PageBlock(2, "hello"), PageBlock(3, "")]
    assert _needs_ocr(blocks, 5) is True 
    
    blocks2 = [PageBlock(1, "a"), PageBlock(2, "b")]
    assert _needs_ocr(blocks2, 5) is True

    blocks3 = [PageBlock(1, "hello world"), PageBlock(2, "another line with enough text")]
    assert _needs_ocr(blocks3, 10) is False

def test_apply_ocr_ligature_cleanup():
    text = "of-\nﬁce\n\n\nﬄ"
    cleaned = _apply_ocr_ligature_cleanup(text)
    assert cleaned == "office\n\n\nffl"

def test_parse_text(tmp_path):
    txt_file = tmp_path / "test.txt"
    txt_file.write_text("Hello UTF-8", encoding="utf-8")
    doc = parse_text(txt_file)
    assert doc.page_count == 1
    assert doc.blocks[0].text == "Hello UTF-8"
    
    bad_txt = tmp_path / "bad.txt"
    bad_txt.write_bytes(b"\xff\xfeH\x00e\x00l\x00l\x00o\x00")
    
    mock_chardet.detect.return_value = {"encoding": "utf-16"}
    doc2 = parse_text(bad_txt)
    assert "Hello" in doc2.blocks[0].text

def test_parse_docx(tmp_path):
    docx_file = tmp_path / "test.docx"
    docx_file.write_text("dummy")

    mock_doc = MagicMock()
    mock_para = MagicMock()
    mock_para.text = "Hello DOCX"
    mock_para.style.name = "Heading 1"
    mock_doc.paragraphs = [mock_para]
    
    mock_table = MagicMock()
    mock_row = MagicMock()
    mock_cell = MagicMock()
    mock_cell.text = "Cell1"
    mock_row.cells = [mock_cell]
    mock_table.rows = [mock_row]
    mock_doc.tables = [mock_table]

    mock_python_docx.Document.return_value = mock_doc

    doc = parse_docx(docx_file)
    assert "# Hello DOCX" in doc.blocks[0].text
    assert "Cell1" in doc.blocks[0].text

def test_parse_pptx(tmp_path):
    pptx_file = tmp_path / "test.pptx"
    pptx_file.write_text("dummy")

    mock_prs = MagicMock()
    mock_slide = MagicMock()
    mock_shape = MagicMock()
    mock_shape.has_text_frame = True
    mock_para = MagicMock()
    mock_run = MagicMock()
    mock_run.text = "Slide Text"
    mock_para.runs = [mock_run]
    mock_shape.text_frame.paragraphs = [mock_para]
    mock_shape.has_table = False
    
    mock_slide.shapes = [mock_shape]
    mock_slide.has_notes_slide = True
    mock_slide.notes_slide.notes_text_frame.text = "Notes here"
    
    mock_prs.slides = [mock_slide]
    mock_pptx.Presentation.return_value = mock_prs

    doc = parse_pptx(pptx_file)
    assert "Slide Text" in doc.blocks[0].text
    assert "[Notes] Notes here" in doc.blocks[0].text

def test_parse_pdf(tmp_path, dummy_meta):
    pdf_file = tmp_path / "test.pdf"
    pdf_file.write_bytes(b"%PDF-1.4\n")
    dummy_meta.abs_path = pdf_file

    mock_fitz_doc = MagicMock()
    mock_page = MagicMock()
    mock_page.get_text.return_value = "PyMuPDF Text"
    mock_fitz_doc.__getitem__.return_value = mock_page
    mock_fitz_doc.page_count = 1
    mock_fitz.open.return_value = mock_fitz_doc

    doc = parse_pdf(pdf_file, dummy_meta, min_chars_per_page=5, ocr_engine="tesseract", ocr_dpi=300, ocr_language="eng")
    assert doc.blocks[0].text == "PyMuPDF Text"
    assert doc.ocr_applied is False
    
    mock_page.get_text.return_value = ""
    mock_pdf = MagicMock()
    mock_pdfplumber_page = MagicMock()
    mock_pdfplumber_page.extract_text.return_value = ""
    mock_pdf.pages = [mock_pdfplumber_page]
    mock_pdfplumber.open.return_value.__enter__.return_value = mock_pdf
    
    mock_pdf2image.convert_from_path.return_value = ["img1"]
    mock_pytesseract.image_to_string.return_value = "OCR Text"

    doc2 = parse_pdf(pdf_file, dummy_meta, min_chars_per_page=5, ocr_engine="tesseract", ocr_dpi=300, ocr_language="eng")
    assert doc2.blocks[0].text == "OCR Text"
    assert doc2.ocr_applied is True
    assert doc2.ocr_engine == "tesseract"
    
    mock_easyocr_reader = MagicMock()
    mock_easyocr_reader.readtext.return_value = ["EasyOCR", "Text"]
    mock_easyocr.Reader.return_value = mock_easyocr_reader
    
    doc3 = parse_pdf(pdf_file, dummy_meta, min_chars_per_page=5, ocr_engine="easyocr", ocr_dpi=300, ocr_language="eng")
    assert doc3.blocks[0].text == "EasyOCR Text"
    assert doc3.ocr_applied is True
    assert doc3.ocr_engine == "easyocr"

def test_dispatch_parser(tmp_path, dummy_meta):
    pdf_file = tmp_path / "test.pdf"
    pdf_file.write_bytes(b"%PDF-1.4\n")
    dummy_meta.abs_path = pdf_file
    dummy_meta.source_format = "pdf"
    
    with patch("sage.rag.parsers.parse_pdf") as mock_parse_pdf:
        mock_parse_pdf.return_value = "PDF_DOC"
        res = dispatch_parser(dummy_meta)
        assert res == "PDF_DOC"
    
    dummy_meta.source_format = "pptx"
    with patch("sage.rag.parsers.parse_pptx") as mock_parse_pptx:
        mock_parse_pptx.return_value = "PPTX_DOC"
        res = dispatch_parser(dummy_meta)
        assert res == "PPTX_DOC"
        
    dummy_meta.source_format = "docx"
    with patch("sage.rag.parsers.parse_docx") as mock_parse_docx:
        mock_parse_docx.return_value = "DOCX_DOC"
        res = dispatch_parser(dummy_meta)
        assert res == "DOCX_DOC"
        
    dummy_meta.source_format = "md"
    with patch("sage.rag.parsers.parse_text") as mock_parse_text:
        mock_parse_text.return_value = "MD_DOC"
        res = dispatch_parser(dummy_meta)
        assert res == "MD_DOC"

    dummy_meta.source_format = "unknown"
    assert dispatch_parser(dummy_meta) is None

    dummy_meta.source_format = "pdf"
    with patch("sage.rag.parsers.parse_pdf", side_effect=Exception("parse error")):
        assert dispatch_parser(dummy_meta) is None
