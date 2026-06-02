from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import SystemMessage

from sage.agents.research import _build_fallback_references, _replace_placeholder_references


def test_replace_placeholder_references():
    report = "Some content\n## References\n[1] Title. Venue. Year.\n[2] Title. Venue. Year."
    sources = [
        {"source": "arxiv", "data": {"results": [{"title": "Real Paper 1"}]}},
        {"source": "web", "data": {"results": [{"title": "Real Paper 2"}]}},
    ]
    res = _replace_placeholder_references(report, sources)
    assert "Real Paper 1" in res
    assert "Real Paper 2" in res
    assert "arxiv" in res


def test_build_fallback_references_limits():
    sources = [
        {"source": "s1", "data": {"results": [{"title": "T1"}, {"title": "T1"}]}},
        {"source": "s2", "data": {"results": [{"title": "T2"}]}},
    ]
    refs = _build_fallback_references(sources)
    assert len(refs) == 2
    assert "T1" in refs[0]
    assert "T2" in refs[1]


@pytest.mark.asyncio
async def test_background_compress_success():
    from sage.routers.chat import _background_compress

    graph = MagicMock()
    mock_snapshot = MagicMock()
    mock_snapshot.values = {"messages": [1, 2, 3]}
    graph.aget_state = AsyncMock(return_value=mock_snapshot)
    graph.aupdate_state = AsyncMock()

    with patch("sage.routers.chat.compress_history", AsyncMock(return_value=[SystemMessage(content="Summary")])):
        await _background_compress(graph, "t1", MagicMock(), 4096)
        graph.aupdate_state.assert_called_once()
        args = graph.aupdate_state.call_args[0]
        assert args[1] == {"history_summary": "Summary"}


def test_split_for_typewriter_edge():
    from sage.routers.chat import _split_for_typewriter

    assert _split_for_typewriter("a" * 10) == ["a" * 10]
    text = "word " * 50
    chunks = _split_for_typewriter(text)
    assert len(chunks) > 1
    assert "".join(chunks) == text


@pytest.mark.asyncio
async def test_run_digest_phase_logic():
    from sage.agents.research import ContextBudget, ResearchPlan, _run_digest_phase

    plan = ResearchPlan.model_validate({"title": "T", "subtopics": [{"name": "S1"}]})
    sources = [{"_subtopic": "S1", "source": "web", "data": {"results": [{"content": "C1"}]}}]
    budget = ContextBudget.from_settings()
    llm = MagicMock()

    with patch("sage.agents.research._digest_subtopic", AsyncMock(return_value="Digest1")):
        res = await _run_digest_phase(plan, sources, budget, llm, 5.0)
        assert "Digest1" in res
        assert "S1" in res


def test_planner_helpers():
    from sage.agents.research import ResearchPlan

    p = ResearchPlan.model_validate({"title": "T", "subtopics": [{"name": "S1"}]})
    assert p.title == "T"
    p2 = ResearchPlan.model_validate({"title": "T", "topics": ["S1"]})
    assert p2.subtopics[0].name == "S1"
