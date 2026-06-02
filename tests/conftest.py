from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, BaseMessage
from langchain_openai import ChatOpenAI

_browser_patcher = patch("webbrowser.open", lambda url: None)
_browser_patcher.start()


@pytest.fixture
def temp_db_path(tmp_path: Path) -> Path:
    db_file = tmp_path / "test_sage.db"
    return db_file


@pytest.fixture
def mock_settings(monkeypatch):
    from sage.config import get_settings

    settings = get_settings()
    settings.database.path = Path(":memory:")

    def fake_get_settings():
        return settings

    monkeypatch.setattr("sage.config.get_settings", fake_get_settings)
    return settings


@pytest.fixture(autouse=True)
async def setup_test_db(monkeypatch, temp_db_path: Path):
    """Overrides the database path for all tests and initializes schema."""
    import sage.database

    def fake_resolve_db_path():
        return temp_db_path

    monkeypatch.setattr(sage.database, "_resolve_db_path", fake_resolve_db_path)
    monkeypatch.setattr(sage.database, "resolve_db_path", fake_resolve_db_path)

    # Initialize the test database
    await sage.database.init_db()

    yield temp_db_path


class ExactMockChatOpenAI(ChatOpenAI):
    """An exact mock of ChatOpenAI that returns predictable chunks or full messages."""

    mock_response: str = "This is a mocked LLM response."
    mock_tool_calls: list = []

    def __init__(self, **kwargs):
        super().__init__(api_key="local", model="mock-model", **kwargs)

    def invoke(self, input: Any, config: dict | None = None, **kwargs: Any) -> BaseMessage:
        msg = AIMessage(content=self.mock_response, tool_calls=self.mock_tool_calls)
        return msg

    async def ainvoke(self, input: Any, config: dict | None = None, **kwargs: Any) -> BaseMessage:
        return self.invoke(input, config, **kwargs)

    def stream(self, input: Any, config: dict | None = None, **kwargs: Any) -> Iterator[BaseMessage]:
        yield AIMessage(content=self.mock_response, tool_calls=self.mock_tool_calls)

    async def astream(self, input: Any, config: dict | None = None, **kwargs: Any) -> AsyncIterator[BaseMessage]:
        yield AIMessage(content=self.mock_response, tool_calls=self.mock_tool_calls)

    def bind(self, **kwargs: Any) -> "ExactMockChatOpenAI":
        return self

    def bind_tools(self, tools: list[Any], **kwargs: Any) -> "ExactMockChatOpenAI":
        self.mock_tools_bound = tools
        return self


@pytest.fixture(autouse=True)
def patch_llm_factory(monkeypatch):
    """Replaces the LLM factory with our exact mock so no real llama-server is started."""
    from sage import llm

    # We don't want to start real server
    monkeypatch.setattr(
        llm, "start_llm_server", lambda: (MagicMock(), 8080, {"backend": "cpu", "vram_mb": 0, "gpu_name": None})
    )
    monkeypatch.setattr(llm, "start_utility_server", lambda: (MagicMock(), 8081))

    def mock_create_llm(port: int):
        return ExactMockChatOpenAI(streaming=True)

    def mock_create_utility_llm(port: int):
        return ExactMockChatOpenAI(streaming=False)

    monkeypatch.setattr(llm, "create_llm", mock_create_llm)
    monkeypatch.setattr(llm, "create_utility_llm", mock_create_utility_llm)
