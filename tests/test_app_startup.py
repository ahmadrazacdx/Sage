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
