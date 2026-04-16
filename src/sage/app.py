"""
FastAPI application factory for Sage.

Usage:

    from sage.app import create_app
    app = create_app(llm_port=port, gpu_info=gpu_info)
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from sage.agents import build_graph
from sage.config import get_settings
from sage.database import init_db
from sage.llm import create_llm
from sage.network import NetworkMonitor

log = structlog.get_logger(__name__)


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_FRONTEND_DIST = _PROJECT_ROOT / "frontend" / "artifacts" / "sage" / "dist"


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup / shutdown sequence.

    Startup (concurrent where safe):
        1. init_db()       : SQLite schema creation
        2. NetworkMonitor  : first probe + background poller
        3. build_graph(llm): compile LangGraph agent graph

    All heavy singletons are stored on `app.state`, routers read
    them via `request.app.state`.  Nothing is re-initialised per
    request.
    """
    cfg = get_settings()

    # LLM client
    llm = create_llm(app.state.llm_port)
    app.state.llm = llm

    # Concurrent I/O-bound init
    network = NetworkMonitor(cfg.network)
    await asyncio.gather(
        init_db(),
        network.start(),
    )

    app.state.graph = build_graph(llm)
    app.state.network = network

    # In-memory stores
    thread_messages: dict[str, list[dict[str, str]]] = {}
    thread_meta: dict[str, dict[str, Any]] = {}
    pending_streams: dict[str, dict[str, Any]] = {}
    active_streams: dict[str, bool] = {}
    uploaded_docs: list[dict[str, Any]] = []

    app.state.thread_messages = thread_messages
    app.state.thread_meta = thread_meta
    app.state.pending_streams = pending_streams
    app.state.active_streams = active_streams
    app.state.uploaded_docs = uploaded_docs

    app.state.model_ready = True
    log.info("app_startup_complete", port=cfg.ui.port)

    yield

    # Shutdown
    app.state.model_ready = False
    await network.stop()
    log.info("app_shutdown_complete")


# Factory

def create_app(*, llm_port: int, gpu_info: dict[str, Any]) -> FastAPI:
    """Build and return the fully-configured FastAPI application.

    Args:
        llm_port: TCP port of the running llama-server.
        gpu_info: Hardware detection dict from `detect_gpu()`.
    """
    cfg = get_settings()

    app = FastAPI(
        title="Sage API",
        version="0.1.0",
        lifespan=_lifespan,
        docs_url=None,
        redoc_url=None,
    )

    # Pre-lifespan state
    app.state.llm_port = llm_port
    app.state.gpu_info = gpu_info
    app.state.model_ready = False

    # CORS — harmless in production (same-origin), required for Vite dev
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173",
            f"http://localhost:{cfg.ui.port}",
        ],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # API routers
    from sage.routers import (
        chat_router,
        documents_router,
        sessions_router,
        system_router,
    )

    app.include_router(system_router, prefix="/api")
    app.include_router(sessions_router, prefix="/api")
    app.include_router(documents_router, prefix="/api")
    app.include_router(chat_router, prefix="/api")

    # SPA static-file mount
    if _FRONTEND_DIST.is_dir():
        app.mount(
            "/",
            StaticFiles(directory=str(_FRONTEND_DIST), html=True),
            name="spa",
        )
    else:
        log.warning(
            "frontend_dist_not_found",
            path=str(_FRONTEND_DIST),
            hint="Run 'pnpm build' in frontend/artifacts/sage/ to create it.",
        )

    return app
