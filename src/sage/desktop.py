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
"""

from __future__ import annotations

import atexit
import os
import signal
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

import structlog
import uvicorn

log = structlog.get_logger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


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
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)

    thread = threading.Thread(
        target=server.run, daemon=True, name="sage-uvicorn"
    )
    thread.start()
    return server


def _setup_tray(window: Any) -> threading.Thread | None:
    """Start a pystray system tray icon in a background thread.

    Returns the thread, or None if pystray is not available.
    """
    try:
        import pystray
        from PIL import Image
    except ImportError:
        log.warning("pystray_unavailable", hint="Install pystray + Pillow for system tray support.")
        return None

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
            try:
                window.restore()
            except Exception:
                pass

    def on_quit(icon: Any, item: Any) -> None:
        icon.stop()
        if window is not None:
            try:
                window.destroy()
            except Exception:
                pass

    icon = pystray.Icon(
        "sage",
        img,
        "Sage",
        menu=pystray.Menu(
            pystray.MenuItem("Open Sage", on_show, default=True),
            pystray.MenuItem("Quit", on_quit),
        ),
    )

    tray_thread = threading.Thread(
        target=icon.run, daemon=True, name="sage-tray"
    )
    tray_thread.start()
    return tray_thread


def _navigate_when_ready(window: Any, backend_url: str, timeout: float = 30.0) -> None:
    """Poll /api/healthz; navigate the already-open window once backend is up."""
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
    except Exception as exc:
        log.warning("window_navigate_failed", error=str(exc)[:200])


def launch() -> None:
    """Launch the Sage desktop application.

    Startup sequence:
        1. create_app()        — build FastAPI app
        2. _run_uvicorn()      — start uvicorn in a daemon thread
        3. webview window      — opens on about:blank in
        4. _navigate_when_ready— daemon thread polls /api/healthz
                                 navigates window to real URL once ready
        5. _fast_then_heavy    — DB + checkpointer + LLM servers in background
        6. Frontend overlay    — polls /api/status; fades when model_ready=True
    """
    from sage.config import get_settings
    from sage.utils import configure_logging

    cfg = get_settings()
    configure_logging(cfg.app.log_level)

    log.info("desktop_launch_starting")

    # 1. Create FastAPI app
    from sage.app import create_app
    app = create_app()

    # 2. Start uvicorn in background thread.
    server = _run_uvicorn(app, cfg.ui.host, cfg.ui.port)
    backend_url = f"http://localhost:{cfg.ui.port}"

    # 3. Open pywebview window.
    try:
        import webview
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

    window = webview.create_window(
        "Sage",
        "about:blank",
        width=1280,
        height=820,
        min_size=(900, 600),
        resizable=True,
        text_select=True,
        easy_drag=False,
    )

    # 4. Start system tray icon.
    _setup_tray(window)

    # 5. Navigate to backend URL in background once healthz responds.
    threading.Thread(
        target=_navigate_when_ready,
        args=(window, backend_url),
        daemon=True,
        name="sage-navigate",
    ).start()
    log.info("desktop_window_opening")

    webview.start(
        debug=(cfg.app.log_level == "debug"),
        private_mode=False,
    )

    # 6. Shutdown.
    log.info("desktop_window_closed")
    server.should_exit = True
