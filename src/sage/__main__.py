"""
Sage application entry point.

Usage:

    python -m sage           : pywebview desktop window (production)
    python -m sage --dev     : backend-only for Vite dev proxy
    python -m sage --browser : backend + browser tab (no webview)
"""

from __future__ import annotations

import argparse
import signal
import sys
import webbrowser

import uvicorn

from sage.config import get_settings
from sage.utils import configure_logging


def _run_dev_mode() -> None:
    """Backend-only mode for development with Vite dev proxy."""
    cfg = get_settings()
    configure_logging(cfg.app.log_level)

    from sage.app import create_app
    app = create_app()

    import structlog
    log = structlog.get_logger(__name__)
    log.info(
        "dev_mode_starting",
        backend_url=f"http://localhost:{cfg.ui.port}",
        hint="Start frontend with: cd frontend/artifacts/sage && pnpm dev",
    )

    uvicorn.run(
        app,
        host=cfg.ui.host,
        port=cfg.ui.port,
        log_level=cfg.app.log_level,
    )


def _run_browser_mode() -> None:
    """Backend + auto-open browser tab (no pywebview)."""
    cfg = get_settings()
    configure_logging(cfg.app.log_level)

    from sage.app import create_app
    app = create_app()

    url = f"http://localhost:{cfg.ui.port}"
    if cfg.ui.browser_auto_open:
        import threading

        def _open_browser() -> None:
            """Wait a moment for uvicorn to start, then open browser."""
            import time
            time.sleep(2.5)
            webbrowser.open(url)

        threading.Thread(target=_open_browser, daemon=True).start()

    import structlog
    log = structlog.get_logger(__name__)
    log.info("browser_mode_starting", url=url)

    uvicorn.run(
        app,
        host=cfg.ui.host,
        port=cfg.ui.port,
        log_level=cfg.app.log_level,
    )


def _run_desktop_mode() -> None:
    """Full desktop experience via pywebview + system tray."""
    from sage.desktop import launch
    launch()


def main() -> None:
    """Parse CLI arguments and dispatch to the correct mode."""
    parser = argparse.ArgumentParser(
        prog="sage",
        description="Sage — Academic Assistant",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--dev",
        action="store_true",
        help="Backend-only mode (pair with Vite dev server)",
    )
    group.add_argument(
        "--browser",
        action="store_true",
        help="Backend + browser tab (no native window)",
    )

    args = parser.parse_args()

    # Suppress Ctrl+C traceback.
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))

    if args.dev:
        _run_dev_mode()
    elif args.browser:
        _run_browser_mode()
    else:
        _run_desktop_mode()


if __name__ == "__main__":
    main()
