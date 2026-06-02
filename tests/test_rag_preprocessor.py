from sage.rag.preprocessor import (
    run_preprocessing_pipeline,
    stage1_unicode_normalisation,
    stage2_whitespace_normalisation,
    stage3_header_footer_removal,
    stage4_encoding_repair,
    stage5_toc_removal,
    stage6_reference_isolation,
    stage7_math_preservation,
    stage8_content_length_gate,
)


def test_stage1_unicode_normalisation():
    text = "Hello\u200bWorld \u2018Quotes\u2019 \u2014 \u00a0"
    assert stage1_unicode_normalisation(text) == "HelloWorld 'Quotes' --  "


def test_stage2_whitespace_normalisation():
    text = "Line 1 \t \n\n\n\r\nLine 2  \n \n \n"
    assert stage2_whitespace_normalisation(text) == "Line 1\n\nLine 2"


def test_stage3_header_footer_removal():
    text = "Page 1 of 5\nReal content\nPage 2 of 5\nMore content"
    res = stage3_header_footer_removal(text)
    assert "Real content" in res
    assert "More content" in res
    assert "Page 1" not in res

    text_repeated = "CONFIDENTIAL\nContent A\nCONFIDENTIAL\nContent B\nCONFIDENTIAL\nContent C\n"
    res_rep = stage3_header_footer_removal(text_repeated)
    assert "CONFIDENTIAL" not in res_rep
    assert "Content A\nContent B\nContent C" in res_rep


def test_stage4_encoding_repair():
    text = "This is fine."
    assert stage4_encoding_repair(text) == "This is fine."
    text_mojibake = "This is a test â€“ with mojibake."
    res = stage4_encoding_repair(text_mojibake)
    assert isinstance(res, str)


def test_stage5_toc_removal():
    toc = "1. Introduction .... 1\n2. Background .... 5\n3. Method .... 10\n"
    text = f"Title\n\n{toc}\n\nActual Chapter 1"
    res = stage5_toc_removal(text)
    assert "Actual Chapter 1" in res
    assert "Introduction .... 1" not in res

    no_toc = "This is a normal paragraph with numbers like 1, 2, and 3.\nIt has another line.\nAnd a third."
    assert stage5_toc_removal(no_toc) == no_toc


def test_stage6_reference_isolation():
    text = "Some text.\n\n## References\n[1] Author A. 2020.\n[2] Author B. 2021.\n[3] Author C. 2022."
    main, refs = stage6_reference_isolation(text)
    assert "Some text." in main
    assert "[1] Author A" in refs
    assert "References" not in main

    text2 = "Some text.\n\n## References\n[1] Author A. 2020."
    main2, refs2 = stage6_reference_isolation(text2)
    assert "Some text." in main2
    assert "References" in main2
    assert refs2 == ""


def test_stage7_math_preservation():
    text = "Equation $E=mc^2$ and \\(a^2 + b^2 = c^2\\)."
    res = stage7_math_preservation(text)
    assert "$E=mc^2$" in res
    assert "\\(a^2 + b^2 = c^2\\)" in res

    text2 = "Sum is ∑∫∂"
    res2 = stage7_math_preservation(text2)
    assert "$∑∫∂$" in res2


def test_stage8_content_length_gate():
    short_text = "Not enough words here."
    assert stage8_content_length_gate(short_text) is None

    long_text = " ".join(["word"] * 30)
    assert stage8_content_length_gate(long_text) == long_text


def test_run_preprocessing_pipeline():
    assert run_preprocessing_pipeline("") is None

    text = " ".join(["word"] * 30) + "\n\n## References\n[1] A 2020\n[2] B 2021\n[3] C 2022"
    res = run_preprocessing_pipeline(text)
    assert res is not None
    main, refs = res
    assert "word" in main
    assert "[1]" in refs
