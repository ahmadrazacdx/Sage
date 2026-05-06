"""
Sage Desktop Shell: pywebview wrapper with system tray.

Provides a native Windows desktop window that embeds the Sage
frontend, auto-starts the backend (FastAPI + llama-server), and
manages the full application lifecycle.

Features:
  - Native window via pywebview + Edge WebView2
  - System tray icon via pystray (Open/Quit actions)
  - Automatic backend lifecycle (FastAPI + dual llama-server)
  - Graceful shutdown of all subprocesses on window close
  - WebView2 compatibility across Windows 10/11
  - Single-instance enforcement (second click surfaces existing window)
"""

from __future__ import annotations

import contextlib
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

import structlog
import uvicorn

_PROJECT_ROOT: Path | None = None
_MUTEX_NAME = "Local\\SageDesktopApp_SingleInstance_v1"
_ERROR_ALREADY_EXISTS = 183


def _acquire_single_instance_lock() -> Any:
    """Try to acquire a named Windows mutex."""
    if sys.platform != "win32":
        return object()
    try:
        import ctypes

        handle = ctypes.windll.kernel32.CreateMutexW(None, True, _MUTEX_NAME)
        err = ctypes.windll.kernel32.GetLastError()
        if err == _ERROR_ALREADY_EXISTS:
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
            return None
        return handle
    except Exception:
        return object()


def _surface_existing_window() -> None:
    """Find the running Sage window and bring it to the foreground."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        hwnd = ctypes.windll.user32.FindWindowW(None, "Sage")
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 9)
            ctypes.windll.user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


def _force_window_foreground(title: str = "Sage") -> None:
    """Force the named window to the foreground on Windows."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        hwnd = ctypes.windll.user32.FindWindowW(None, title)
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 9)
            ctypes.windll.user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


def _apply_taskbar_icon(icon_path: Path, window_title: str = "Sage") -> None:
    """Stamp the Sage .ico onto both the titlebar and taskbar button."""
    if sys.platform != "win32" or not icon_path.is_file():
        return
    try:
        import ctypes

        user32 = ctypes.windll.user32
        LR_LOADFROMFILE = 0x00000010
        IMAGE_ICON = 1
        WM_SETICON = 0x0080
        ICON_SMALL = 0
        ICON_BIG = 1

        time.sleep(0.6)

        hicon_big = user32.LoadImageW(None, str(icon_path), IMAGE_ICON, 256, 256, LR_LOADFROMFILE)
        hicon_small = user32.LoadImageW(None, str(icon_path), IMAGE_ICON, 16, 16, LR_LOADFROMFILE)

        hwnd = None
        for _ in range(30):
            hwnd = user32.FindWindowW(None, window_title)
            if hwnd:
                break
            time.sleep(0.1)
        if hwnd:
            if hicon_big:
                user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, hicon_big)
            if hicon_small:
                user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hicon_small)
    except Exception:
        pass


def _wait_for_backend(port: int, timeout: float = 30.0) -> bool:
    """Poll until the FastAPI health endpoint responds.

    Returns:
        True if backend is ready, False on timeout.
    """
    deadline = time.monotonic() + timeout
    url = f"http://localhost:{port}/api/healthz"

    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2):
                return True
        except Exception:
            time.sleep(0.4)
    return False


def _run_uvicorn(app: Any, host: str, port: int) -> uvicorn.Server:
    """Start uvicorn in a daemon thread and return the server instance."""
    _null_log_config: dict[str, Any] = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {"format": "%(message)s"},
        },
        "handlers": {
            "null": {"class": "logging.NullHandler"},
        },
        "loggers": {
            "uvicorn": {"handlers": ["null"], "level": "CRITICAL", "propagate": False},
            "uvicorn.error": {"handlers": ["null"], "level": "CRITICAL", "propagate": False},
            "uvicorn.access": {"handlers": ["null"], "level": "CRITICAL", "propagate": False},
        },
    }
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="critical",
        access_log=False,
        log_config=_null_log_config,
    )
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True, name="sage-uvicorn")
    thread.start()
    return server


def _setup_tray(window: Any, icon_path: Path | None = None) -> threading.Thread | None:
    """Start a pystray system tray icon in a background thread."""
    log = structlog.get_logger(__name__)
    try:
        import pystray
        from PIL import Image
    except ImportError:
        log.warning("pystray_unavailable", hint="Install pystray + Pillow for system tray support.")
        return None

    img: Any = None
    if icon_path and icon_path.is_file():
        try:
            img = Image.open(str(icon_path)).convert("RGBA").resize((64, 64), Image.LANCZOS)
        except Exception:
            img = None

    if img is None:
        # Generate a simple icon.
        icon_size = 64
        img = Image.new("RGB", (icon_size, icon_size), color=(30, 30, 30))
        try:
            from PIL import ImageDraw, ImageFont

            draw = ImageDraw.Draw(img)
            try:
                font = ImageFont.truetype("arial.ttf", 40)
            except Exception:
                font = ImageFont.load_default()
            bbox = draw.textbbox((0, 0), "S", font=font)
            text_width = bbox[2] - bbox[0]
            text_height = bbox[3] - bbox[1]
            x = (icon_size - text_width) // 2
            y = (icon_size - text_height) // 2 - 4
            draw.text((x, y), "S", fill=(255, 193, 7), font=font)
        except Exception:
            pass

    def on_show(icon: Any, item: Any) -> None:
        if window is not None:
            with contextlib.suppress(Exception):
                window.restore()
        _force_window_foreground()

    def on_quit(icon: Any, item: Any) -> None:
        icon.stop()
        if window is not None:
            with contextlib.suppress(Exception):
                window.destroy()

    icon = pystray.Icon(
        "sage",
        img,
        "Sage",
        menu=pystray.Menu(
            pystray.MenuItem("Open Sage", on_show, default=True),
            pystray.MenuItem("Quit", on_quit),
        ),
    )

    tray_thread = threading.Thread(target=icon.run, daemon=True, name="sage-tray")
    tray_thread.start()
    return tray_thread


def _navigate_when_ready(
    window: Any,
    backend_url: str,
    timeout: float = 30.0,
    icon_path: Path | None = None,
) -> None:
    """Poll /api/healthz; navigate the already-open window once backend is up."""
    log = structlog.get_logger(__name__)
    health_url = f"{backend_url}/api/healthz"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(health_url, timeout=2):
                break
        except Exception:
            time.sleep(0.25)
    else:
        log.error("navigate_timeout", timeout_s=timeout)
        return
    try:
        window.load_url(backend_url)
        log.info("window_navigated", url=backend_url)

        with contextlib.suppress(Exception):
            window.show()
        with contextlib.suppress(Exception):
            window.restore()
        _force_window_foreground()

        if icon_path:
            _apply_taskbar_icon(icon_path)

    except Exception as exc:
        log.warning("window_navigate_failed", error=str(exc)[:200])


def launch() -> None:
    """Launch the Sage desktop application.

    Startup sequence:
        1. Single-instance check — surface existing window and exit if running.
        2. create_app()          — build FastAPI app.
        3. _run_uvicorn()        — start uvicorn in a daemon thread.
        4. webview window        — opens on about:blank.
        5. _navigate_when_ready  — daemon thread polls /api/healthz,
                                   navigates window, forces it to foreground.
        6. _fast_then_heavy      — DB + checkpointer + LLM servers in background.
        7. Frontend overlay      — polls /api/status; fades when model_ready=True.
    """
    from sage.config import _PROJECT_ROOT as project_root
    from sage.config import get_settings
    from sage.utils import configure_logging

    global _PROJECT_ROOT
    _PROJECT_ROOT = project_root

    cfg = get_settings()
    configure_logging(cfg.app.log_level)

    log = structlog.get_logger(__name__)
    _mutex_handle = _acquire_single_instance_lock()
    if _mutex_handle is None:
        log.info("desktop_already_running", action="surfacing_existing_window")
        _surface_existing_window()
        return

    log.info("desktop_launch_starting")

    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Sage")
        except Exception:
            pass

    try:
        # 2. Create FastAPI app
        from sage.app import create_app

        app = create_app()

        # 3. Start uvicorn in a daemon thread.
        server = _run_uvicorn(app, cfg.ui.host, cfg.ui.port)
        backend_url = f"http://localhost:{cfg.ui.port}"

        # 4. Open pywebview window.
        try:
            import webview

            try:
                webview.settings["ALLOW_DOWNLOADS"] = True
                webview.settings["OPEN_EXTERNAL_LINKS"] = True
            except Exception:
                log.warning("webview_settings_unavailable", hint="Upgrade pywebview for download/external link support")
        except ImportError:
            log.error(
                "pywebview_not_installed",
                hint="Install pywebview: pip install pywebview",
            )
            import webbrowser

            webbrowser.open(backend_url)
            log.info("browser_fallback", url=backend_url)
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                return

        _webview_data = _PROJECT_ROOT / "artifacts" / "data" / "webview"
        _webview_data.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("WEBVIEW2_USER_DATA_FOLDER", str(_webview_data))

        icon_path = _PROJECT_ROOT / "sage.ico"
        if not icon_path.is_file():
            icon_path = _PROJECT_ROOT / "installer" / "sage.ico"
        window_kwargs = {
            "width": 1280,
            "height": 820,
            "min_size": (900, 600),
            "resizable": True,
            "text_select": True,
            "easy_drag": False,
        }
        if icon_path.is_file():
            window_kwargs["icon"] = str(icon_path)

        try:
            window = webview.create_window(
                "Sage",
                "about:blank",
                **window_kwargs,
            )
        except TypeError as exc:
            if "icon" in window_kwargs and "icon" in str(exc).lower():
                window_kwargs.pop("icon", None)
                log.warning("webview_icon_unsupported", hint="Retrying without icon support")
                window = webview.create_window(
                    "Sage",
                    "about:blank",
                    **window_kwargs,
                )
            else:
                raise

        # 5. Start system tray icon (opt-in via env var).
        if os.environ.get("SAGE_TRAY_ICON", "").strip().lower() in {"1", "true", "yes"}:
            _setup_tray(window, icon_path if icon_path.is_file() else None)

        # 6. Navigate to backend URL in background once healthz responds.
        threading.Thread(
            target=_navigate_when_ready,
            args=(window, backend_url),
            kwargs={"icon_path": icon_path if icon_path.is_file() else None},
            daemon=True,
            name="sage-navigate",
        ).start()
        log.info("desktop_window_opening")

        webview.start(
            debug=(cfg.app.log_level == "debug"),
            private_mode=False,
            storage_path=str(_webview_data),
        )

        # 7. Shutdown.
        log.info("desktop_window_closed")
        server.should_exit = True
    except Exception as exc:
        log.error("desktop_startup_failed", exc_info=True, exc_type=type(exc).__name__, exc_msg=str(exc))
        raise
