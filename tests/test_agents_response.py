import pytest

from sage.agents.response import (
    _build_citation_map,
    _build_references_section,
    _clean_metadata,
    _clean_source_name,
    _normalize_source_name,
    _rewrite_citations,
    response_node,
)


def _sanitise_claim(claim: str) -> str:
    cleaned = claim.replace('"', "'")
    if len(cleaned) > 120:
        return cleaned[:120] + "…"
    return cleaned


@pytest.mark.asyncio
async def test_response_node_basic():
    state = {"response": "Hello [KU1]", "knowledge_units": [{"id": "KU1", "claim": "Fact 1", "source_file": "doc.pdf"}]}
    res = await response_node(state)
    assert "Hello [1]" in res["response"]
    assert "## References" in res["response"]
    assert "[1] 📚 doc" in res["response"]
    assert res["citations"][0]["label"] == "[1]"


@pytest.mark.asyncio
async def test_response_node_empty():
    state = {"response": ""}
    res = await response_node(state)
    assert "No response was generated" in res["response"]


def test_response_helpers():
    kus = [
        {"id": "KU1", "source_file": "file1.pdf"},
        {"id": "ku1", "source_file": "file1.pdf"},
        {"id": "KU2", "source_file": "file2.pdf"},
    ]
    cmap = _build_citation_map(kus)
    assert cmap == {"KU1": 1, "KU2": 2}

    assert _rewrite_citations("Test [KU1] and [KU3]", cmap) == "Test [1] and "

    assert _sanitise_claim('hello "world"') == "hello 'world'"
    assert len(_sanitise_claim("a" * 200)) == 121

    assert _build_references_section([], {}) == ""
    refs = _build_references_section(
        [{"id": "KU1", "source_file": "S1.pdf", "claim": "[Page 10 | confidence: high] C1"}], {"KU1": 1}
    )
    assert "## References" in refs
    assert "[1] 📚 S1: [Page 10 | confidence: high]" in refs


def test_response_extra():
    assert _normalize_source_name("Doc-File.pdf") == "docfile"

    emoji, clean = _clean_source_name("presentation.pptx")
    assert emoji == "📑"
    assert clean == "presentation"

    assert _clean_metadata("[Author | S1] claim", "S1.pdf") == "Author"
    assert _clean_metadata("[S1] claim", "S1.pdf") is None

    assert _build_citation_map([{"id": ""}, {"id": "KU1", "source_file": "s1.pdf"}]) == {"KU1": 1}

    refs = _build_references_section(
        [
            {"id": "KU1", "source_file": "S1.pdf", "claim": "C1"},
            {"id": "KU2", "source_file": "S1.pdf", "claim": "C2"},
        ],
        {"KU1": 1, "KU2": 1},
    )
    assert refs.count("[1]") == 1


@pytest.mark.asyncio
async def test_response_node_no_refs():
    state = {"response": "Hello world", "knowledge_units": []}
    res = await response_node(state)
    assert "## References" not in res["response"]
    state2 = {
        "response": "Hello [1]",
        "knowledge_units": [{"id": "", "source_file": "doc.pdf"}, {"id": "KU1", "source_file": "doc.pdf"}],
    }
    res2 = await response_node(state2)
    assert len(res2["citations"]) == 1
