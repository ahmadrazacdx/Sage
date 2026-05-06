"""System health, status, and course-listing endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from sage.config import get_settings

router = APIRouter(tags=["system"])


class HealthStatus(BaseModel):
    status: str


class SystemStatus(BaseModel):
    model_ready: bool
    model_name: str
    llm_port: int | None = None
    embedding_model: str
    vectordb_collections: list[str]
    network_online: bool


class CoursesResponse(BaseModel):
    courses: list[str]


@router.get("/healthz", response_model=HealthStatus)
async def health_check() -> HealthStatus:
    """Lightweight liveness probe,always returns 200."""
    return HealthStatus(status="ok")


@router.get("/status", response_model=SystemStatus)
async def get_status(request: Request) -> SystemStatus:
    """System health snapshot. Polled every 5 s by the
    frontend until {"model_ready":True}.
    """
    cfg = get_settings()
    network = getattr(request.app.state, "network", None)

    return SystemStatus(
        model_ready=getattr(request.app.state, "model_ready", False),
        model_name=cfg.llm.active_model_name,
        llm_port=getattr(request.app.state, "llm_port", 0),
        embedding_model=cfg.embedding.embed_model.name,
        vectordb_collections=[
            cfg.rag.curriculum_collection,
            cfg.rag.user_uploads_collection,
        ],
        network_online=network.online if network is not None else False,
    )


@router.get("/courses", response_model=CoursesResponse)
async def get_courses() -> CoursesResponse:
    """Return available course codes.

    MVP: returns an empty list.  The ingestion pipeline (next phase)
    will populate ChromaDB metadata and this endpoint will query it.
    """
    return CoursesResponse(courses=[])
