import ast
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.sage.tools.calculator import (
    _MAX_BIT_LENGTH,
    _evaluate,
    _SafeEvaluator,
    calculator,
)
from src.sage.tools.export import (
    _generate_typst_source,
    _markdown_to_typst,
    _nonconflict_path,
    _sanitize_filename,
    export_markdown,
    export_pdf,
    validate_typst_bin,
)
from src.sage.tools.sandbox import (
    _build_execution_code,
    _collect_figures,
    _uses_matplotlib,
    execute_python,
)


def test_calculator_success():
    res = calculator.invoke({"expression": "2 + 2"})
    assert res["success"] is True
    assert res["result"] == 4

    res2 = calculator.invoke({"expression": "sqrt(16) * 2"})
    assert res2["success"] is True
    assert res2["result"] == 8.0

    res3 = calculator.invoke({"expression": "pi"})
    assert res3["success"] is True


def test_calculator_invalid_syntax():
    res = calculator.invoke({"expression": "2 + * 2"})
    assert res["success"] is False
    assert "Invalid expression syntax" in res["error"]


def test_calculator_too_long():
    long_expr = "1" + " + 1" * 300
    res = calculator.invoke({"expression": long_expr})
    assert res["success"] is False
    assert "Expression too long" in res["error"]


def test_calculator_errors():
    res = calculator.invoke({"expression": "1 / 0"})
    assert res["success"] is False
    assert "Math error" in res["error"]


def test_calculator_evaluator_limits():
    with pytest.raises(ValueError, match="Unsupported constant type: bool"):
        _evaluate("True")

    with pytest.raises(ValueError, match="Unsupported constant type: str"):
        _evaluate("'string'")

    with pytest.raises(ValueError, match="Unknown variable: 'x'"):
        _evaluate("x + 1")

    with pytest.raises(ValueError, match="Unsupported operator"):
        _evaluate("1 << 2")

    with pytest.raises(ValueError, match="Only direct function calls are allowed"):
        _evaluate("math.sqrt(4)")

    with pytest.raises(ValueError, match="Function 'open' is not allowed"):
        _evaluate("open('file.txt')")

    with pytest.raises(ValueError, match="Too many arguments"):
        _evaluate("abs(" + ",".join(["1"] * 60) + ")")
    with pytest.raises(ValueError, match="factorial argument too large"):
        _evaluate("factorial(6000)")

    with pytest.raises(ValueError, match="Exponent too large"):
        _evaluate("pow(2.0, 3000)")

    with pytest.raises(ValueError, match="Exponentiation result exceeds"):
        _evaluate("2 ** 60000")

    with pytest.raises(ValueError, match="is too large"):
        _evaluate("sin(1e16)")

    with pytest.raises(ValueError, match="exceeds|limit"):
        _evaluate(f"(10**{_MAX_BIT_LENGTH // 4}) * (10**{_MAX_BIT_LENGTH // 4})")

    evaluator = _SafeEvaluator()
    evaluator._depth = 49
    with pytest.raises(ValueError, match="Expression too deeply nested"):
        evaluator.visit(ast.parse("1 + 1", mode="eval").body)

    evaluator = _SafeEvaluator()
    with pytest.raises(ValueError, match="Integer too large"):
        evaluator._check_magnitude(10 ** (_MAX_BIT_LENGTH + 1))

    with pytest.raises(ValueError, match="Float too large"):
        evaluator._check_magnitude(float("inf"))


def test_calculator_coverage_edges():
    res = calculator.invoke({"expression": "1e-13"})
    assert res["result"] == 0.0
    max_bits = _MAX_BIT_LENGTH
    res = calculator.invoke({"expression": f"(10**{max_bits // 4}) * (10**{max_bits // 4})"})
    assert res["success"] is False
    assert "exceeds" in res["error"] or "limit" in res["error"]
    res = calculator.invoke({"expression": "~5"})
    assert res["success"] is False
    assert "Unsupported unary operator" in res["error"]

    res = calculator.invoke({"expression": "factorial(10)"})
    assert res["success"] is True
    assert res["result"] == 3628800
    res = calculator.invoke({"expression": "sin(0)"})
    assert res["success"] is True
    assert res["result"] == 0.0

    res = calculator.invoke({"expression": "sin(1e16)"})
    assert res["success"] is False
    assert "too large" in res["error"]

    res = calculator.invoke({"expression": "pow(2)"})
    assert res["success"] is False
    assert "pow() takes 2 or 3" in res["error"]


def test_export_markdown_basic():
    with patch("pathlib.Path.write_text") as mock_write:
        res = export_markdown.invoke({"content": "# Hello", "filename": "test.md"})
        assert res["success"] is True
        assert res["operation"] == "export_markdown"
        mock_write.assert_called_once()


def test_export_markdown_no_content():
    res = export_markdown.invoke({"content": "", "filename": "test"})
    assert res["success"] is False


def test_export_markdown_too_long():
    res = export_markdown.invoke({"content": "A" * 15000, "filename": "test"})
    assert res["success"] is False
    assert "Content too long" in res["error"]


def test_sanitize_filename():
    assert _sanitize_filename("foo/bar.txt") == "bar.txt"
    assert _sanitize_filename("foo\\bar|baz*.txt") == "bar_baz_.txt"


def test_nonconflict_path(tmp_path):
    f = tmp_path / "test.txt"
    f.touch()
    res = _nonconflict_path(f)
    assert res.name == "test_1.txt"


def test_markdown_to_typst():
    md = "# Heading 1\n## Heading 2\n[1] Reference\n[link](url)\n```python\nprint(1)\n```"
    typst = _markdown_to_typst(md)
    assert "= Heading 1" in typst
    assert "== Heading 2" in typst
    assert '#link("url")[link]' in typst
    assert "```python" in typst

    with pytest.raises(ValueError, match="Unsafe Typst directive"):
        _markdown_to_typst('#include("malicious.typ")')


def test_generate_typst_source():
    with patch("src.sage.tools.export._resolve_template_path", return_value=None):
        src = _generate_typst_source("# Report", title="My Report")
        assert "My Report" in src
        assert "= Report" in src


def test_validate_typst_bin():
    with patch("src.sage.tools.export._resolve_typst_bin", return_value="typst"):
        with patch("shutil.which", return_value="/usr/bin/typst"):
            assert validate_typst_bin() is True
    with (
        patch("src.sage.tools.export._resolve_typst_bin", return_value="typst"),
        patch("shutil.which", return_value=None),
        patch("pathlib.Path.is_file", return_value=False),
    ):
        assert validate_typst_bin() is False


@pytest.mark.asyncio
async def test_export_pdf_basic():
    mock_proc = MagicMock()
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))
    mock_proc.returncode = 0

    with patch("pathlib.Path.write_text"), patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        res = await export_pdf.ainvoke({"content": "# Test", "filename": "test", "title": "Title"})
        assert res["success"] is True


@pytest.mark.asyncio
async def test_export_pdf_compile_error():
    mock_proc = MagicMock()
    mock_proc.communicate = AsyncMock(return_value=(b"", b"error details"))
    mock_proc.returncode = 1

    with patch("pathlib.Path.write_text"), patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        res = await export_pdf.ainvoke({"content": "# Test", "filename": "test"})
        assert res["success"] is False
        assert "Typst compile error" in res["error"]


def test_uses_matplotlib():
    assert _uses_matplotlib("import matplotlib.pyplot as plt") is True
    assert _uses_matplotlib("from seaborn import heatmap") is True
    assert _uses_matplotlib("import os\nimport sys") is False
    assert _uses_matplotlib("def foo( ) : :") is True


def test_build_execution_code():
    code = "import os"
    built = _build_execution_code(code, "/tmp/fig")
    assert code == built

    mpl_code = "import matplotlib.pyplot as plt\nplt.plot()"
    built_mpl = _build_execution_code(mpl_code, "/tmp/fig")
    assert "import matplotlib as _mpl" in built_mpl
    assert mpl_code in built_mpl
    assert "_EPILOGUE" not in built_mpl
    assert "# -- Sage figure epilogue" in built_mpl


def test_collect_figures(tmp_path):
    f1 = tmp_path / "fig_001.svg"
    f1.write_text("<svg>" + "a" * 50 + "</svg>")
    f2 = tmp_path / "fig_002.svg"
    f2.write_text("invalid")

    figs = _collect_figures(str(tmp_path))
    assert len(figs) == 1
    assert figs[0]["index"] == 1
    assert "svg" in figs[0]["data"]


@pytest.mark.asyncio
async def test_execute_python_basic():
    mock_proc = MagicMock()
    mock_proc.communicate = AsyncMock(return_value=(b"hello\n", b""))
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        res = await execute_python.ainvoke({"code": "print('hello')"})
        assert res["success"] is True
        assert res["stdout"] == "hello\n"
        assert res["stderr"] == ""


@pytest.mark.asyncio
async def test_execute_python_timeout():
    mock_proc = MagicMock()
    mock_proc.communicate = AsyncMock(side_effect=[TimeoutError(), (b"", b"")])
    mock_proc.kill = MagicMock()

    with (
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        patch("src.sage.tools.sandbox.get_settings") as mock_settings,
    ):
        mock_settings.return_value.tools.sandbox.timeout = 0.1
        mock_settings.return_value.tools.sandbox.max_code_length = 1000
        mock_settings.return_value.tools.sandbox.figures_dir = Path("artifacts/sandbox/figures")

        res = await execute_python.ainvoke({"code": "while True: pass"})
        assert res["success"] is False
        assert "timed out" in res["error"]


@pytest.mark.asyncio
async def test_execute_python_error():
    mock_proc = MagicMock()
    mock_proc.communicate = AsyncMock(return_value=(b"", b"Traceback..."))
    mock_proc.returncode = 1

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        res = await execute_python.ainvoke({"code": "1/0"})
        assert res["success"] is False
        assert "Traceback" in res["error"]


@pytest.mark.asyncio
async def test_execute_python_no_code():
    res = await execute_python.ainvoke({"code": ""})
    assert res["success"] is False
    assert "No code provided" in res["error"]


@pytest.mark.asyncio
async def test_execute_python_too_long():
    res = await execute_python.ainvoke({"code": "a" * 150000})
    assert res["success"] is False
    assert "Code exceeds maximum length" in res["error"]
