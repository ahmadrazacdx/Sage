"""System health, status, and course-listing endpoints."""

from __future__ import annotations

import json
import pickle
from pathlib import Path

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
    institution_name: str
    institution_department: str
    institution_email: str
    institution_website: str


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
        ],
        network_online=network.online if network is not None else False,
        institution_name=cfg.institution.name,
        institution_department=cfg.institution.department,
        institution_email=cfg.institution.contact_email,
        institution_website=cfg.institution.social.get("website", ""),
    )


@router.get("/courses", response_model=CoursesResponse)
async def get_courses() -> CoursesResponse:
    """Return available course codes.

    Queries courses.json, the BM25 index, or ChromaDB metadata.
    """
    import structlog
    log = structlog.get_logger(__name__)
    cfg = get_settings()
    project_root = Path(__file__).resolve().parents[3]

    vectordb_path = Path(cfg.rag.vectordb)
    if not vectordb_path.is_absolute():
        vectordb_path = project_root / vectordb_path

    # 1. Try reading courses.json
    paths = [
        vectordb_path / "courses.json",
        vectordb_path.parent / "courses.json",
    ]
    for path in paths:
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                courses = data.get("courses", [])
                if courses:
                    log.info("courses_loaded_from_json", path=str(path), count=len(courses))
                    return CoursesResponse(courses=sorted(list(set(courses))))
            except Exception as e:
                log.error("courses_json_load_failed", path=str(path), error=str(e))

    # 2. Try loading from BM25 pickle
    bm25_path = Path(cfg.rag.bm25_curriculum_file)
    if not bm25_path.is_absolute():
        bm25_path = project_root / bm25_path

    if bm25_path.exists():
        try:
            with bm25_path.open("rb") as fh:
                payload = pickle.load(fh)
            metadatas = payload.get("metadatas", [])
            courses = {
                str(m.get("course_code", "")).upper()
                for m in metadatas
                if isinstance(m, dict) and m.get("course_code")
            }
            if courses:
                log.info("courses_loaded_from_bm25", path=str(bm25_path), count=len(courses))
                return CoursesResponse(courses=sorted(list(courses)))
        except Exception as e:
            log.error("courses_bm25_load_failed", path=str(bm25_path), error=str(e))

    # 3. Try querying ChromaDB collection directly
    try:
        from sage.rag.vectorstore import get_curriculum_collection
        collection = get_curriculum_collection()
        results = collection.get(include=["metadatas"])
        metadatas = results.get("metadatas")
        if metadatas:
            courses = {
                str(m.get("course_code", "")).upper()
                for m in metadatas
                if isinstance(m, dict) and m.get("course_code")
            }
            if courses:
                log.info("courses_loaded_from_chroma", count=len(courses))
                return CoursesResponse(courses=sorted(list(courses)))
    except Exception as e:
        log.error("courses_chroma_load_failed", error=str(e))

    log.warn("no_courses_found")
    return CoursesResponse(courses=[])
