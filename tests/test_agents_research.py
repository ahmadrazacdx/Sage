import pytest
import asyncio
from unittest.mock import MagicMock, patch, AsyncMock
from langchain_core.messages import AIMessage

from sage.agents.state import AgentState
from sage.agents.research import research_node, ResearchPlan, Subtopic, SubtopicQueries, ResearchReview

def create_mock_llm(responses=None):
    from langchain_openai import ChatOpenAI
    llm = MagicMock(spec=ChatOpenAI)
    llm.bind.return_value = llm
    llm.bind_tools.return_value = llm
    
    if responses:
        if isinstance(responses, list):
            llm.ainvoke = AsyncMock(side_effect=responses)
        else:
            llm.ainvoke = AsyncMock(return_value=responses)
    else:
        llm.ainvoke = AsyncMock(return_value=AIMessage(content="mocked"))
    return llm

@pytest.mark.asyncio
async def test_research_node_offline():
    state: AgentState = {"query": "test", "online_mode": False}
    llm = create_mock_llm()
    res = await research_node(state, llm)
    assert "internet connection" in res["response"]
    assert res["research_report"] is None

@pytest.mark.asyncio
async def test_research_node_success():
    state: AgentState = {"query": "AI research", "online_mode": True}
    llm = create_mock_llm()
    
    plan = ResearchPlan(
        title="AI",
        subtopics=[Subtopic(name="Basics", queries=SubtopicQueries(academic="AI", web="AI basics", encyclopedia="Artificial Intelligence"))]
    )
    review_pass = ResearchReview(verdict="pass")
    
    with patch("sage.agents.research.ainvoke_structured_with_fallback", side_effect=[plan, review_pass]), \
         patch("sage.tools.search.search_arxiv") as m_arxiv, \
         patch("sage.tools.search.search_web") as m_web, \
         patch("sage.tools.search.search_wikipedia") as m_wiki, \
         patch("sage.agents.research._run_digest_phase", new_callable=AsyncMock) as m_digest, \
         patch("sage.agents.research.ChatOpenAI.ainvoke", new_callable=AsyncMock) as m_writer:
        
        m_arxiv.ainvoke = AsyncMock(return_value={"results": [{"title": "Arxiv Paper", "content": "Text"}]})
        m_web.ainvoke = AsyncMock(return_value={"results": [{"title": "Web Page", "content": "Text"}]})
        m_wiki.ainvoke = AsyncMock(return_value={"results": [{"title": "Wiki Page", "content": "Text"}]})
        
        m_digest.return_value = "Digest content"
        m_writer.return_value = AIMessage(content="# Report\n\n## References\n[1] Arxiv Paper. arxiv.")
        
        with patch("sage.agents.research.export_markdown"), patch("sage.agents.research.export_pdf") as m_pdf:
            m_pdf.ainvoke = AsyncMock(return_value={"path": "/tmp/out.pdf"})
            
            res = await research_node(state, llm)
            
            assert "research_plan" in res
            assert "research_report" in res
            assert len(res["artifact_paths"]) == 1
            assert res["artifact_paths"][0]["kind"] == "pdf"

@pytest.mark.asyncio
async def test_research_node_fallback_plan():
    state: AgentState = {"query": "Quantum Computing", "online_mode": True}
    llm = create_mock_llm()
    
    review_pass = ResearchReview(verdict="pass")
    
    with patch("sage.agents.research.ainvoke_structured_with_fallback", side_effect=[Exception("Fail"), Exception("Fail"), review_pass]), \
         patch("sage.tools.search.search_arxiv") as m_arxiv, \
         patch("sage.tools.search.search_web") as m_web, \
         patch("sage.tools.search.search_wikipedia") as m_wiki, \
         patch("sage.agents.research._run_digest_phase", new_callable=AsyncMock) as m_digest, \
         patch("sage.agents.research.ChatOpenAI.ainvoke", new_callable=AsyncMock) as m_writer:
        
        m_arxiv.ainvoke = AsyncMock(return_value={"results": []})
        m_web.ainvoke = AsyncMock(side_effect=TimeoutError())
        m_wiki.ainvoke = AsyncMock(side_effect=Exception("Fail"))
        
        m_digest.return_value = "Digest content fallback"
        llm.ainvoke = AsyncMock(return_value=AIMessage(content="# Fallback Report"))
        
        with patch("sage.agents.research.export_markdown"), patch("sage.agents.research.export_pdf") as m_pdf:
            m_pdf.ainvoke = AsyncMock(return_value={"error": "pdf error"})
            res = await research_node(state, llm)
            
            assert res["research_report"] == "# Fallback Report"
            assert "Quantum Computing" in res["research_plan"]["title"]

@pytest.mark.asyncio
async def test_research_node_review_loop():
    state: AgentState = {"query": "AI research", "online_mode": True}
    llm = create_mock_llm()
    
    plan = ResearchPlan(title="AI", subtopics=[Subtopic(name="B", queries=SubtopicQueries(web="A"))])
    review_revise = ResearchReview(verdict="revise", issues=[{"type": "Citation", "detail": "Add more"}])
    review_pass = ResearchReview(verdict="pass")
    
    with patch("sage.agents.research.ainvoke_structured_with_fallback", side_effect=[plan, review_revise, review_pass]), \
         patch("sage.tools.search.search_web") as m_web, \
         patch("sage.agents.research._run_digest_phase", new_callable=AsyncMock) as m_digest, \
         patch("sage.agents.research.export_markdown"), patch("sage.agents.research.export_pdf"):
        
        m_web.ainvoke = AsyncMock(return_value={"results": []})
        m_digest.return_value = "Digest content"
        
        llm.ainvoke = AsyncMock(side_effect=[
            AIMessage(content="Report Draft 1"),
            AIMessage(content="Report Draft 2")
        ])
        
        res = await research_node(state, llm)
        assert res["research_report"] == "Report Draft 2"

@pytest.mark.asyncio
async def test_research_node_writer_fails():
    state: AgentState = {"query": "AI", "online_mode": True}
    llm = create_mock_llm()
    
    plan = ResearchPlan(title="AI", subtopics=[])
    
    with patch("sage.agents.research.ainvoke_structured_with_fallback", return_value=plan), \
         patch("sage.agents.research._run_digest_phase", new_callable=AsyncMock) as m_digest:
        
        m_digest.return_value = "Digest"
        llm.ainvoke = AsyncMock(side_effect=Exception("Writer failed"))
        
        res = await research_node(state, llm)
        assert "unable to synthesize" in res["response"]

@pytest.mark.asyncio
async def test_digest_subtopic():
    from sage.agents.research import _digest_subtopic, ContextBudget
    llm = create_mock_llm(AIMessage(content="Condensed facts"))
    budget = ContextBudget.from_settings()
    sub = Subtopic(name="Test")
    res = await _digest_subtopic(sub, "Source content", budget, llm, 10.0)
    assert res == "Condensed facts"

def test_research_helpers():
    from sage.agents.research import _trim, _normalize_references_section, _all_refs_look_fake, _force_replace_references

    assert _trim("hello world", 5) == "hello…"
    assert _trim("hi", 10) == "hi"
    
    report = "# Title\n## References\n[1] Ref 1\n[2] Ref 2"
    norm = _normalize_references_section(report)
    assert "[1] Ref 1" in norm
    
    refs = "[1] Title. Venue. Year.\n[2] Title. Venue. Year."
    assert _all_refs_look_fake(refs) is True
    assert _all_refs_look_fake("[1] Real Paper. arxiv.") is False
    
    res = _force_replace_references("Report content\n## References\nOld", "New references")
    assert "New references" in res
    assert "Old" not in res
