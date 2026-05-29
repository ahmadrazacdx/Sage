from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from sage.tools.search import _sanitize_query, search_arxiv, search_web, search_wikipedia


def test_sanitize_query():
    assert _sanitize_query("test query") == "test query"
    assert _sanitize_query("\x00test\x1f") == "test"
    assert _sanitize_query("a" * 1000) == "a" * 500
    assert _sanitize_query(123) == ""


@pytest.mark.asyncio
async def test_search_arxiv_empty_or_invalid():
    with pytest.raises(ValidationError):
        await search_arxiv.ainvoke({"query": 123})

    res2 = await search_arxiv.ainvoke({"query": ""})
    assert res2["error"] == "Empty search query"


@pytest.mark.asyncio
@patch("langchain_community.retrievers.ArxivRetriever", create=True)
async def test_search_arxiv_success(mock_retriever):
    mock_instance = mock_retriever.return_value
    mock_doc = MagicMock()
    mock_doc.metadata = {"Title": "Test Paper", "Entry ID": "1234"}
    mock_doc.page_content = "This is a test abstract."
    mock_instance.invoke.return_value = [mock_doc]

    res = await search_arxiv.ainvoke({"query": "test query"})
    assert res["error"] is None
    assert len(res["results"]) == 1
    assert res["results"][0]["title"] == "Test Paper"


@pytest.mark.asyncio
@patch("langchain_community.retrievers.ArxivRetriever", create=True)
async def test_search_arxiv_empty_result(mock_retriever):
    mock_instance = mock_retriever.return_value
    mock_instance.invoke.return_value = []

    res = await search_arxiv.ainvoke({"query": "test query"})
    assert "No arXiv papers found" in res["error"]


@pytest.mark.asyncio
async def test_search_web_empty_or_invalid():
    with pytest.raises(ValidationError):
        await search_web.ainvoke({"query": 123})

    res2 = await search_web.ainvoke({"query": ""})
    assert res2["error"] == "Empty search query"


@pytest.mark.asyncio
@patch("langchain_community.tools.DuckDuckGoSearchResults", create=True)
@patch("langchain_community.utilities.DuckDuckGoSearchAPIWrapper", create=True)
async def test_search_web_success(mock_wrapper, mock_search):
    mock_instance = mock_search.return_value
    mock_instance.invoke.return_value = "This is a web snippet."

    res = await search_web.ainvoke({"query": "test query"})
    assert res["error"] is None
    assert len(res["results"]) == 1
    assert res["results"][0]["content"] == "This is a web snippet."


@pytest.mark.asyncio
@patch("langchain_community.tools.DuckDuckGoSearchResults", create=True)
@patch("langchain_community.utilities.DuckDuckGoSearchAPIWrapper", create=True)
async def test_search_web_empty_result(mock_wrapper, mock_search):
    mock_instance = mock_search.return_value
    mock_instance.invoke.return_value = ""

    res = await search_web.ainvoke({"query": "test query"})
    assert "No web results found" in res["error"]


@pytest.mark.asyncio
async def test_search_wikipedia_empty_or_invalid():
    with pytest.raises(ValidationError):
        await search_wikipedia.ainvoke({"query": 123})

    res2 = await search_wikipedia.ainvoke({"query": ""})
    assert res2["error"] == "Empty search query"


@pytest.mark.asyncio
@patch("langchain_community.retrievers.WikipediaRetriever", create=True)
async def test_search_wikipedia_success(mock_retriever):
    mock_instance = mock_retriever.return_value
    mock_doc = MagicMock()
    mock_doc.metadata = {"title": "Test Wiki"}
    mock_doc.page_content = "This is a wiki summary."
    mock_instance.invoke.return_value = [mock_doc]

    res = await search_wikipedia.ainvoke({"query": "test query"})
    assert res["error"] is None
    assert len(res["results"]) == 1
    assert res["results"][0]["title"] == "Test Wiki"


@pytest.mark.asyncio
@patch("langchain_community.retrievers.WikipediaRetriever", create=True)
async def test_search_wikipedia_empty_result(mock_retriever):
    mock_instance = mock_retriever.return_value
    mock_instance.invoke.return_value = []

    res = await search_wikipedia.ainvoke({"query": "test query"})
    assert "No Wikipedia articles found" in res["error"]
