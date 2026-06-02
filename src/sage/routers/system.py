"""System health, status, and course-listing endpoints."""

from __future__ import annotations

import json
from pathlib import Path

import structlog
from fastapi import APIRouter, Request
from pydantic import BaseModel

from sage.config import get_settings

log = structlog.get_logger(__name__)
router = APIRouter(tags=["system"])


class HealthStatus(BaseModel):
    status: str


class SystemStatus(BaseModel):
    model_ready: bool
    model_name: str
    llm_port: int
    embedding_tier: str
    vectordb_collections: list[str]
    network_online: bool


class CoursesResponse(BaseModel):
    courses: list[str]


def _find_courses_json() -> Path | None:
    """Probe the courses.json locations."""
    cfg = get_settings()
    vectordb_path = Path(cfg.rag.vectordb)
    for candidate in [
        vectordb_path / "courses.json",
        vectordb_path.parent / "courses.json",
    ]:
        if candidate.exists():
            return candidate
    return None


def _load_courses_json() -> list[str]:
    """Load available course codes from courses.json.

    Returns an empty list if the file is missing or malformed so the app
    starts cleanly even before the first ingestion run.
    """
    courses_path = _find_courses_json()
    if courses_path is None:
        log.warning(
            "courses_json_missing",
            hint="Run the ingestion pipeline to generate courses.json.",
        )
        return []
    try:
        with courses_path.open("r", encoding="utf-8") as fh:
            data: dict = json.load(fh)
        courses: list[str] = data.get("courses", [])
        if not isinstance(courses, list):
            raise ValueError("'courses' key must be a list")
        return [str(c) for c in courses]
    except Exception as exc:
        log.error(
            "courses_json_parse_error",
            path=str(courses_path),
            exc_type=type(exc).__name__,
            error=str(exc)[:200],
        )
        return []


@router.get("/healthz", response_model=HealthStatus)
async def health_check() -> HealthStatus:
    """Lightweight liveness probe, always returns 200."""
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
        embedding_tier="lite",
        vectordb_collections=[cfg.rag.curriculum_collection],
        network_online=network.online if network is not None else False,
    )


@router.get("/courses", response_model=CoursesResponse)
async def get_courses() -> CoursesResponse:
    """Return the list of available course codes from the vectordb index.

    Reads `courses.json` which is written by the ingestion pipeline.
    Returns an empty list if the file does not yet exist (first-run, pre-ingestion).
    """
    courses = _load_courses_json()
    log.info("courses_endpoint_served", count=len(courses))
    return CoursesResponse(courses=courses)
