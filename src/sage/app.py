"""
FastAPI application factory for Sage.

Usage:

    from sage.app import create_app
    app = create_app()
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from sage.agents import build_graph
from sage.config import get_settings
from sage.database import init_db, resolve_db_path
from sage.llm import create_llm, create_utility_llm, start_llm_server, start_utility_server
from sage.network import NetworkMonitor
from sage.tools.export import resolve_export_output_dir

log = structlog.get_logger(__name__)


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_FRONTEND_DIST = _PROJECT_ROOT / "frontend" / "artifacts" / "sage" / "dist"


async def _heavy_startup(app: FastAPI, checkpointer: Any, cfg: Any) -> None:
    """LLM server startup & graph compilation."""
    try:
        # Start primary LLM server in a thread-pool (blocking I/O).
        proc, llm_port, gpu_info = await asyncio.to_thread(start_llm_server)
        app.state.llm_port = llm_port
        app.state.gpu_info  = gpu_info

        # Start utility server in a thread-pool.
        utility_port: int | None = None
        try:
            _, utility_port = await asyncio.to_thread(start_utility_server)
        except FileNotFoundError as exc:
            log.warning(
                "utility_server_skipped",
                reason=str(exc)[:200],
                hint="Memory features will use the primary model as fallback.",
            )
        app.state.utility_port = utility_port

        # LLM clients.
        llm = create_llm(llm_port)
        app.state.llm = llm

        utility_llm = None
        if utility_port is not None:
            utility_llm = create_utility_llm(utility_port)
        app.state.utility_llm = utility_llm

        # Compile graph with the already-open checkpointer.
        app.state.graph = build_graph(llm, checkpointer=checkpointer)

        # Signal frontend: model is ready.
        app.state.model_ready = True
        log.info(
            "app_startup_complete",
            port=cfg.ui.port,
            checkpointer="AsyncSqliteSaver",
            utility_model="available" if utility_llm else "unavailable",
            embedding_model="available" if app.state.embed_model else "unavailable",
        )

    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.error(
            "heavy_startup_failed",
            exc_type=type(exc).__name__,
            error=str(exc)[:500],
        )

@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup / shutdown sequence."""
    cfg = get_settings()

    # --- Instant defaults (< 1 ms) ---
    app.state.model_ready  = False
    app.state.llm          = None
    app.state.utility_llm  = None
    app.state.graph        = None
    app.state.llm_port     = None
    app.state.gpu_info     = {}
    app.state.utility_port = None
    app.state.pending_streams  = {}
    app.state.active_streams   = {}
    app.state.thread_messages  = {}
    app.state.embed_model      = None
    app.state.checkpointer     = None
    app.state.uploaded_docs    = []
    app.state._checkpointer_cm = None
    app.state._heavy_task      = None

    # Start network monitor — fires first probe concurrently, non-blocking.
    network = NetworkMonitor(cfg.network)
    asyncio.create_task(network.start(), name="sage-network-start")
    app.state.network = network

    async def _fast_then_heavy() -> None:
        """Run DB init + checkpointer open + embed probe, then heavy LLM startup."""
    
        try:
            await init_db()
            log.info("database_ready")
        except Exception as exc:
            log.error("init_db_failed", error=str(exc)[:300])
            return
        
        try:
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
            db_path = resolve_db_path()
            cm = AsyncSqliteSaver.from_conn_string(str(db_path))
            checkpointer = await cm.__aenter__()
            app.state.checkpointer     = checkpointer
            app.state._checkpointer_cm = cm

            heavy_task = asyncio.create_task(
                _heavy_startup(app, checkpointer, cfg),
                name="sage-heavy-startup",
            )
            app.state._heavy_task = heavy_task
        except Exception as exc:
            log.error("checkpointer_open_failed", error=str(exc)[:300])
    
    asyncio.create_task(_fast_then_heavy(), name="sage-fast-startup")
    yield

    # --- Shutdown ---
    heavy_task = app.state._heavy_task
    if heavy_task is not None and not heavy_task.done():
        heavy_task.cancel()
        try:
            await heavy_task
        except (asyncio.CancelledError, Exception):
            pass

    cm = app.state._checkpointer_cm
    if cm is not None:
        try:
            await cm.__aexit__(None, None, None)
        except Exception:
            pass

    app.state.model_ready = False
    await network.stop()
    log.info("app_shutdown_complete")

    # Factory

def create_app() -> FastAPI:
    """Build and return the fully-configured FastAPI application."""
    cfg = get_settings()

    app = FastAPI(
        title="Sage API",
        version="0.1.0",
        lifespan=_lifespan,
        docs_url=None,
        redoc_url=None,
    )

    # Pre-lifespan defaults
    app.state.llm_port    = None
    app.state.gpu_info    = {}
    app.state.utility_port = None
    app.state.model_ready = False

    # CORS: harmless in production (same-origin), required for Vite dev.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173",
            f"http://localhost:{cfg.ui.port}",
        ],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # API routers.
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

    # Artifact endpoints.
    _exports_dir = resolve_export_output_dir()

    @app.get("/api/artifacts")
    async def list_artifacts() -> list[dict[str, Any]]:
        """List generated exports (SVG/PDF/MD/TXT), newest first."""
        kind_map = {
            ".pdf": "pdf",
            ".svg": "svg",
            ".md": "md",
            ".txt": "txt",
        }
        if not _exports_dir.exists():
            return []

        artifacts: list[dict[str, Any]] = []
        for file_path in sorted(
            (p for p in _exports_dir.iterdir() if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        ):
            suffix = file_path.suffix.lower()
            kind = kind_map.get(suffix)
            if kind is None:
                continue

            stat = file_path.stat()
            created_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            local_created = created_at.astimezone()
            artifacts.append({
                "kind": kind,
                "filename": file_path.name,
                "size_bytes": stat.st_size,
                "created_at": created_at.isoformat(),
                "date_label": local_created.strftime("%A, %B %d, %Y"),
                "url": f"/api/artifacts/{file_path.name}",
            })

        return artifacts

    @app.get("/api/artifacts/{filename}")
    async def download_artifact(filename: str) -> FileResponse:
        """Serve a generated artifact (PDF, SVG, MD) for download."""
        safe_name = Path(filename).name
        if not safe_name or safe_name != filename:
            raise HTTPException(status_code=400, detail="Invalid filename")
        file_path = _exports_dir / safe_name
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="Artifact not found")
        suffix = file_path.suffix.lower()
        media_map = {".pdf": "application/pdf", ".svg": "image/svg+xml",
                     ".md": "text/markdown", ".txt": "text/plain"}
        media_type = media_map.get(suffix, "application/octet-stream")
        return FileResponse(
            path=str(file_path),
            media_type=media_type,
            filename=safe_name,
        )

    # SPA static-file mount.
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
    