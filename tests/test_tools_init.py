import pytest
from sage.tools import get_offline_tools, get_online_tools, get_all_tools, _assert_tools
from langchain_core.tools import BaseTool
from unittest.mock import MagicMock

def test_get_offline_tools():
    tools = get_offline_tools()
    assert len(tools) >= 4
    assert all(isinstance(t, BaseTool) for t in tools)

def test_get_online_tools():
    tools = get_online_tools()
    assert len(tools) >= 0

def test_get_all_tools():
    tools_offline = get_all_tools(online=False)
    tools_all = get_all_tools(online=True)
    assert len(tools_all) >= len(tools_offline)

def test_assert_tools_fail():
    with pytest.raises(TypeError, match="expected BaseTool"):
        _assert_tools([MagicMock()], "test")

def test_get_online_tools_import_fail(monkeypatch):
    import importlib
    orig_import = importlib.import_module
    
    def mock_import(name, package=None):
        if name == "sage.tools.search":
            raise ImportError("Mocked fail")
        return orig_import(name, package)
        
    monkeypatch.setattr("importlib.import_module", mock_import)
    tools = get_online_tools()
    assert len(tools) == 0
