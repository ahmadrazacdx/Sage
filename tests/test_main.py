from unittest.mock import patch

from sage.__main__ import main


def test_main_no_args():
    with patch("sys.argv", ["sage"]), patch("sage.__main__._run_desktop_mode") as mock_desktop:
        main()
        mock_desktop.assert_called_once()


def test_main_dev():
    with patch("sys.argv", ["sage", "--dev"]), patch("uvicorn.run"):
        main()


def test_main_browser():
    with patch("sys.argv", ["sage", "--browser"]), patch("uvicorn.run"), patch("webbrowser.open"):
        main()
