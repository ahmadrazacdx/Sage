from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI

from sage.app import _heavy_startup


@pytest.mark.asyncio
async def test_heavy_startup_success():
    app = FastAPI()
    app.state.llm_port = None
    app.state.model_ready = False

    mock_cp = MagicMock()
    mock_cfg = MagicMock()

    with (
        patch("sage.app.start_llm_server", return_value=(MagicMock(), 8000, {"backend": "cpu"})),
        patch("sage.app.start_utility_server", return_value=(MagicMock(), 8001)),
        patch("sage.app.create_llm", return_value=MagicMock()),
        patch("sage.app.create_utility_llm", return_value=MagicMock()),
        patch("sage.app.build_graph", return_value=MagicMock()),
    ):
        await _heavy_startup(app, mock_cp, mock_cfg)
        assert app.state.llm_port == 8000
        assert app.state.model_ready is True


@pytest.mark.asyncio
async def test_heavy_startup_fail():
    app = FastAPI()
    app.state.model_ready = False

    mock_cp = MagicMock()
    mock_cfg = MagicMock()

    with patch("sage.app.start_llm_server", side_effect=Exception("Critical fail")):
        await _heavy_startup(app, mock_cp, mock_cfg)
        assert app.state.model_ready is False


@pytest.mark.asyncio
async def test_heavy_startup_success_gpu():
    app = FastAPI()
    app.state.llm_port = None
    app.state.model_ready = False

    mock_cp = MagicMock()
    mock_cfg = MagicMock()

    with (
        patch("sage.app.start_llm_server", return_value=(MagicMock(), 8000, {"backend": "cuda"})),
        patch("sage.app.start_utility_server", return_value=(MagicMock(), 8001)) as mock_start_util,
        patch("sage.app.create_llm", return_value=MagicMock()),
        patch("sage.app.build_graph", return_value=MagicMock()) as mock_build_graph,
    ):
        await _heavy_startup(app, mock_cp, mock_cfg)
        assert app.state.llm_port == 8000
        assert app.state.model_ready is True
        mock_start_util.assert_not_called()
        mock_build_graph.assert_called_once()
        _, kwargs = mock_build_graph.call_args
        assert kwargs.get("util_llm") is None
