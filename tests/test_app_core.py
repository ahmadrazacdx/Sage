from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI

from sage.app import create_app


@pytest.fixture
def mock_deps(monkeypatch):
    monkeypatch.setattr(
        "sage.app.start_llm_server", lambda: (MagicMock(), 8080, {"backend": "cpu", "vram_mb": 0, "gpu_name": None})
    )
    monkeypatch.setattr("sage.app.start_utility_server", lambda: (MagicMock(), 8081))
    monkeypatch.setattr("sage.app.create_llm", lambda port: MagicMock())
    monkeypatch.setattr("sage.app.create_utility_llm", lambda port: MagicMock())
    monkeypatch.setattr("sage.app.build_graph", lambda llm, cp, ullm: MagicMock())
    monkeypatch.setattr("sage.app.init_db", AsyncMock())
    monkeypatch.setattr("langgraph.checkpoint.sqlite.aio.AsyncSqliteSaver", MagicMock())
    monkeypatch.setattr("sage.network.NetworkMonitor", MagicMock())


def test_create_app_basic(mock_deps):
    app = create_app()
    assert isinstance(app, FastAPI)
    assert app.state.model_ready is False


@pytest.mark.asyncio
async def test_app_lifespan(mock_deps):
    from sage.app import _lifespan

    app = FastAPI()

    with patch("sage.app.get_settings") as m_settings:
        m_settings.return_value.database.path = ":memory:"

        async with _lifespan(app):
            assert app.state.model_ready is False

    assert app.state.model_ready is False


@pytest.mark.asyncio
async def test_app_lifespan_fail(mock_deps):
    from sage.app import _lifespan

    app = FastAPI()

    with patch("sage.app.init_db", side_effect=Exception("DB fail")):
        async with _lifespan(app):
            pass
