"""Document upload and management endpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request, UploadFile
from pydantic import BaseModel

from sage.config import get_settings

router = APIRouter(tags=["documents"])


class Document(BaseModel):
    file: str
    uploaded_at: str
    chunks: int


class UploadResponse(BaseModel):
    status: str
    files_processed: int
    chunks_indexed: int


@router.post("/upload", response_model=UploadResponse)
async def upload_documents(
    request: Request,
    files: list[UploadFile],
    course: str = "all",
) -> UploadResponse:
    """Accept document uploads. Validates file types and document-count limits."""
    cfg = get_settings()
    allowed = set(cfg.corpus.allowed_extensions)
    docs: list[dict[str, Any]] = getattr(request.app.state, "uploaded_docs", [])

    if len(docs) + len(files) > cfg.corpus.max_user_documents:
        for f in files:
            await f.close()
        raise HTTPException(
            status_code=413,
            detail=f"Upload limit is {cfg.corpus.max_user_documents} documents.",
        )

    processed = 0
    for f in files:
        filename = f.filename or "unnamed"
        ext = ""
        if "." in filename:
            ext = "." + filename.rsplit(".", 1)[-1].lower()
        if ext not in allowed:
            await f.close()
            for remaining in files[processed + 1 :]:
                await remaining.close()
            raise HTTPException(
                status_code=400,
                detail=f"File type {ext or '(none)'} is not supported.",
            )

        await f.read()
        await f.close()
        docs.append(
            {
                "file": filename,
                "uploaded_at": datetime.now(UTC).isoformat(),
                "chunks": 0,
            }
        )
        processed += 1

    return UploadResponse(
        status="ok",
        files_processed=processed,
        chunks_indexed=0,
    )


@router.get("/documents", response_model=list[Document])
async def list_documents(request: Request) -> list[Document]:
    """List all user-uploaded documents."""
    docs: list[dict[str, Any]] = getattr(request.app.state, "uploaded_docs", [])
    return [Document(**d) for d in docs]


@router.delete("/documents/{filename}", status_code=204)
async def delete_document(filename: str, request: Request) -> None:
    """Remove an uploaded document by filename."""
    docs: list[dict[str, Any]] = getattr(request.app.state, "uploaded_docs", [])
    before = len(docs)
    request.app.state.uploaded_docs = [d for d in docs if d["file"] != filename]
    if len(request.app.state.uploaded_docs) == before:
        raise HTTPException(status_code=404, detail="Document not found.")
