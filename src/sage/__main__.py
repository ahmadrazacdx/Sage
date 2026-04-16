"""
Sage application entry point.

Usage:

    python -m sage          # Start backend on :8765, open browser
    python -m sage --dev    # Start backend only (for Vite dev workflow)
"""

from __future__ import annotations

import sys
import threading
import webbrowser

import structlog
import uvicorn

from sage.config import get_settings
from sage.llm import start_llm_server
from sage.utils import configure_logging

log = structlog.get_logger(__name__)


def main() -> None:
    cfg = get_settings()
    configure_logging(cfg.app.log_level)

    dev_mode = "--dev" in sys.argv

    # Start llama-server
    log.info(
        "sage_starting",
        host=cfg.ui.host,
        port=cfg.ui.port,
        dev_mode=dev_mode,
    )
    proc, port, gpu_info = start_llm_server()

    # Create FastAPI app
    from sage.app import create_app

    app = create_app(llm_port=port, gpu_info=gpu_info)

    # Configure uvicorn
    uvi_cfg = uvicorn.Config(
        app,
        host=cfg.ui.host,
        port=cfg.ui.port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(uvi_cfg)

    if dev_mode:
        # Dev mode: run uvicorn on the main thread (Ctrl+C to stop)
        log.info(
            "dev_mode_active",
            api_url=f"http://localhost:{cfg.ui.port}",
            hint="Run 'pnpm dev' in frontend/artifacts/sage/ for the UI.",
        )
        try:
            server.run()
        except KeyboardInterrupt:
            pass
    else:
        thread = threading.Thread(
            target=server.run, daemon=True, name="uvicorn"
        )
        thread.start()

        url = f"http://localhost:{cfg.ui.port}"
        log.info("opening_browser", url=url)
        webbrowser.open(url)

        try:
            thread.join()
        except KeyboardInterrupt:
            log.info("sage_interrupted")


if __name__ == "__main__":
    main()
