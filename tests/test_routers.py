from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from sage.app import create_app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(
        "sage.app.start_llm_server", lambda: (MagicMock(), 8080, {"backend": "cpu", "vram_mb": 0, "gpu_name": None})
    )
    monkeypatch.setattr("sage.app.start_utility_server", lambda: (MagicMock(), 8081))
    monkeypatch.setattr("sage.app.create_llm", lambda port: MagicMock())
    monkeypatch.setattr("sage.app.create_utility_llm", lambda port: MagicMock())
    monkeypatch.setattr("sage.app.build_graph", lambda llm, checkpointer, util_llm: MagicMock())

    app = create_app()
    monkeypatch.setattr("sage.network.NetworkMonitor.start", AsyncMock())
    monkeypatch.setattr("sage.network.NetworkMonitor.stop", AsyncMock())

    with TestClient(app) as client:
        app.state.model_ready = True
        yield client


def test_healthz(client):
    response = client.get("/api/healthz")
    assert response.status_code == 200


def test_status(client):
    response = client.get("/api/status")
    assert response.status_code == 200


def test_list_sessions(client):
    with patch("sage.routers.sessions.list_conversations", AsyncMock(return_value=[])):
        resp = client.get("/api/sessions")
        assert resp.status_code == 200
        assert resp.json() == []


def test_get_session_messages_memory(client):
    client.app.state.thread_messages = {"t1": [{"role": "user", "content": "hi"}]}
    resp = client.get("/api/sessions/t1/messages")
    assert resp.status_code == 200
    assert resp.json()[0]["content"] == "hi"


def test_get_session_messages_checkpointer(client):
    mock_cp = AsyncMock()
    mock_checkpoint = MagicMock()
    mock_checkpoint.checkpoint = {
        "channel_values": {
            "messages": [MagicMock(type="human", content="hello"), MagicMock(type="ai", content="world")]
        }
    }
    mock_cp.aget_tuple.return_value = mock_checkpoint
    client.app.state.checkpointer = mock_cp

    resp = client.get("/api/sessions/t2/messages")
    assert resp.status_code == 200
    data = resp.json()
    assert data[0]["role"] == "user"
    assert data[1]["role"] == "assistant"


def test_get_session_messages_404(client):
    resp = client.get("/api/sessions/unknown/messages")
    assert resp.status_code == 404


def test_delete_session_simple(client):
    with patch("sage.routers.sessions.delete_conversation", AsyncMock()) as mock_del:
        resp = client.delete("/api/sessions/t1")
        assert resp.status_code == 204
        mock_del.assert_called_once()


def test_delete_session_checkpointer(client):
    mock_cp = MagicMock()
    mock_cp.conn = AsyncMock()
    mock_cp.conn.execute.return_value = AsyncMock()
    mock_cp.conn.execute.return_value.fetchall.return_value = [("table1",)]
    client.app.state.checkpointer = mock_cp

    resp = client.delete("/api/sessions/t2")
    assert resp.status_code == 204


def test_list_documents(client):
    client.app.state.uploaded_docs = [{"file": "f1.pdf", "uploaded_at": "now", "chunks": 0}]
    resp = client.get("/api/documents")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_delete_document_success(client):
    client.app.state.uploaded_docs = [{"file": "f1.pdf", "uploaded_at": "now", "chunks": 0}]
    resp = client.delete("/api/documents/f1.pdf")
    assert resp.status_code == 204
    assert len(client.app.state.uploaded_docs) == 0


def test_delete_document_404(client):
    resp = client.delete("/api/documents/missing.pdf")
    assert resp.status_code == 404


def test_upload_documents_limit(client):
    from sage.config import get_settings

    cfg = get_settings()
    cfg.corpus.max_user_documents = 1
    client.app.state.uploaded_docs = [{"file": "f1.pdf"}]

    files = [("files", ("f2.pdf", b"content", "application/pdf"))]
    resp = client.post("/api/upload", files=files)
    assert resp.status_code == 413


def test_upload_documents_bad_ext(client):
    files = [("files", ("f2.exe", b"content", "application/octet-stream"))]
    resp = client.post("/api/upload", files=files)
    assert resp.status_code == 400


def test_upload_documents_success(client):
    client.app.state.uploaded_docs = []
    files = [("files", ("test.pdf", b"%PDF-1.4", "application/pdf"))]
    resp = client.post("/api/upload", files=files)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert len(client.app.state.uploaded_docs) == 1


def test_courses(client):
    with patch("sage.routers.system.get_courses", AsyncMock(return_value={"courses": []})):
        resp = client.get("/api/courses")
        assert resp.status_code == 200
