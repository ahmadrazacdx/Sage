from unittest.mock import MagicMock, patch

import pytest

from sage import desktop


def test_acquire_single_instance_lock_non_win32():
    with patch("sys.platform", "linux"):
        res = desktop._acquire_single_instance_lock()
        assert res is not None


def test_acquire_single_instance_lock_win32_success():
    with patch("sys.platform", "win32"):
        mock_ctypes = MagicMock()
        mock_ctypes.windll.kernel32.CreateMutexW.return_value = 12345
        mock_ctypes.windll.kernel32.GetLastError.return_value = 0
        with patch.dict("sys.modules", {"ctypes": mock_ctypes}):
            res = desktop._acquire_single_instance_lock()
            assert res == 12345


def test_acquire_single_instance_lock_win32_exists():
    with patch("sys.platform", "win32"):
        mock_ctypes = MagicMock()
        mock_ctypes.windll.kernel32.CreateMutexW.return_value = 12345
        mock_ctypes.windll.kernel32.GetLastError.return_value = desktop._ERROR_ALREADY_EXISTS
        with patch.dict("sys.modules", {"ctypes": mock_ctypes}):
            res = desktop._acquire_single_instance_lock()
            assert res is None
            mock_ctypes.windll.kernel32.CloseHandle.assert_called_once_with(12345)


def test_surface_existing_window():
    with patch("sys.platform", "win32"):
        mock_ctypes = MagicMock()
        mock_ctypes.windll.user32.FindWindowW.return_value = 999
        with patch.dict("sys.modules", {"ctypes": mock_ctypes}):
            desktop._surface_existing_window()
            mock_ctypes.windll.user32.ShowWindow.assert_called_once_with(999, 9)


def test_surface_existing_window_exception():
    with (
        patch("sys.platform", "win32"),
        patch.dict("sys.modules", {"ctypes": MagicMock(side_effect=Exception("test"))}),
    ):
        desktop._surface_existing_window()


def test_force_window_foreground():
    with patch("sys.platform", "win32"):
        mock_ctypes = MagicMock()
        mock_ctypes.windll.user32.FindWindowW.return_value = 999
        with patch.dict("sys.modules", {"ctypes": mock_ctypes}):
            desktop._force_window_foreground("Test")
            mock_ctypes.windll.user32.ShowWindow.assert_called_once_with(999, 9)


def test_force_window_foreground_exception():
    with (
        patch("sys.platform", "win32"),
        patch.dict("sys.modules", {"ctypes": MagicMock(side_effect=Exception("test"))}),
    ):
        desktop._force_window_foreground("Test")


def test_apply_taskbar_icon(tmp_path):
    icon_path = tmp_path / "test.ico"
    icon_path.touch()
    with patch("sys.platform", "win32"):
        mock_ctypes = MagicMock()
        mock_ctypes.windll.user32.LoadImageW.return_value = 111
        mock_ctypes.windll.user32.FindWindowW.return_value = 999
        with patch.dict("sys.modules", {"ctypes": mock_ctypes}), patch("time.sleep"):
            desktop._apply_taskbar_icon(icon_path, "Test")
            assert mock_ctypes.windll.user32.SendMessageW.call_count == 2


def test_wait_for_backend_success():
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value.__enter__.return_value = MagicMock()
        assert desktop._wait_for_backend(8000, timeout=0.1) is True


def test_wait_for_backend_timeout():
    with patch("urllib.request.urlopen", side_effect=Exception("Connection refused")), patch("time.sleep"):
        assert desktop._wait_for_backend(8000, timeout=0.1) is False


def test_run_uvicorn():
    mock_app = MagicMock()
    with patch("uvicorn.Server") as mock_server_cls, patch("threading.Thread") as mock_thread:
        server_instance = mock_server_cls.return_value
        res = desktop._run_uvicorn(mock_app, "127.0.0.1", 8000)
        assert res == server_instance
        mock_thread.assert_called_once()
        mock_thread.return_value.start.assert_called_once()


def test_setup_tray(tmp_path):
    mock_window = MagicMock()
    icon_path = tmp_path / "test.ico"
    icon_path.touch()

    mock_pystray = MagicMock()
    mock_PIL = MagicMock()

    with patch.dict("sys.modules", {"pystray": mock_pystray, "PIL": mock_PIL}):
        thread = desktop._setup_tray(mock_window, icon_path)
        assert thread is not None
        mock_pystray.Icon.assert_called_once()


def test_setup_tray_fallback(tmp_path):
    mock_window = MagicMock()
    mock_pystray = MagicMock()
    mock_PIL = MagicMock()

    with patch.dict("sys.modules", {"pystray": mock_pystray, "PIL": mock_PIL}):
        mock_PIL.Image.new.return_value = MagicMock()
        thread = desktop._setup_tray(mock_window, None)
        assert thread is not None

        args, kwargs = mock_pystray.Menu.call_args

        menu_items = args
        for item in menu_items:
            if item.text == "Open Sage" or item.text == "Quit":
                item.action(MagicMock(), MagicMock())


def test_navigate_when_ready():
    mock_window = MagicMock()
    with patch("urllib.request.urlopen"), patch("time.sleep"):
        desktop._navigate_when_ready(mock_window, "http://localhost:8000", timeout=0.1)
        mock_window.load_url.assert_called_once_with("http://localhost:8000")


def test_navigate_when_ready_exception():
    mock_window = MagicMock()
    with patch("urllib.request.urlopen"), patch("time.sleep"):
        mock_window.load_url.side_effect = Exception("test")
        desktop._navigate_when_ready(mock_window, "http://localhost:8000", timeout=0.1)


def test_launch():
    with (
        patch("sage.desktop._acquire_single_instance_lock", return_value=123),
        patch("sage.app.create_app") as mock_create_app,
        patch("sage.desktop._run_uvicorn") as mock_run_uvicorn,
        patch("sage.desktop._setup_tray"),
        patch("threading.Thread"),
        patch("sage.desktop.sys.platform", "win32"),
        patch("os.environ.setdefault"),
    ):
        mock_webview = MagicMock()
        mock_ctypes = MagicMock()
        with patch.dict("sys.modules", {"webview": mock_webview, "ctypes": mock_ctypes}):
            desktop.launch()
            mock_create_app.assert_called_once()
            mock_run_uvicorn.assert_called_once()
            mock_webview.create_window.assert_called_once()
            mock_webview.start.assert_called_once()


def test_launch_webview_exception():
    with (
        patch("sage.desktop._acquire_single_instance_lock", return_value=123),
        patch("sage.app.create_app"),
        patch("sage.desktop._run_uvicorn"),
        patch("sage.desktop._setup_tray"),
        patch("threading.Thread"),
        patch("sage.desktop.sys.platform", "linux"),
        patch("os.environ.setdefault"),
    ):
        mock_webview = MagicMock()
        mock_webview.create_window.side_effect = [TypeError("icon"), MagicMock()]
        with patch.dict("sys.modules", {"webview": mock_webview}):
            desktop.launch()
            assert mock_webview.create_window.call_count == 2


def test_launch_pywebview_import_error():
    with (
        patch("sage.desktop._acquire_single_instance_lock", return_value=123),
        patch("sage.app.create_app"),
        patch("sage.desktop._run_uvicorn"),
        patch("sage.desktop.sys.platform", "linux"),
        patch.dict("sys.modules", {"webview": None}),
        patch("webbrowser.open") as mock_wb_open,
        patch("time.sleep", side_effect=KeyboardInterrupt),
    ):
        desktop.launch()
        mock_wb_open.assert_called_once()


def test_launch_general_exception():
    with (
        patch("sage.desktop._acquire_single_instance_lock", return_value=123),
        patch("sage.app.create_app", side_effect=ValueError("test_error")),
        pytest.raises(ValueError, match="test_error"),
    ):
        desktop.launch()
