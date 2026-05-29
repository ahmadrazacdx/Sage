import pytest

from sage.agents.response import (
    _build_citation_map,
    _build_references_section,
    _rewrite_citations,
    _sanitise_claim,
    response_node,
)


@pytest.mark.asyncio
async def test_response_node_basic():
    state = {"response": "Hello [KU1]", "knowledge_units": [{"id": "KU1", "claim": "Fact 1", "source_file": "doc.pdf"}]}
    res = await response_node(state)
    assert "Hello [1]" in res["response"]
    assert "## References" in res["response"]
    assert "[1] doc.pdf" in res["response"]
    assert res["citations"][0]["label"] == "[1]"


@pytest.mark.asyncio
async def test_response_node_empty():
    state = {"response": ""}
    res = await response_node(state)
    assert "No response was generated" in res["response"]


def test_response_helpers():
    kus = [{"id": "KU1"}, {"id": "ku1"}, {"id": "KU2"}]
    cmap = _build_citation_map(kus)
    assert cmap == {"KU1": 1, "KU2": 2}

    assert _rewrite_citations("Test [KU1] and [KU3]", cmap) == "Test [1] and "

    assert _sanitise_claim('hello "world"') == "hello 'world'"
    assert len(_sanitise_claim("a" * 200)) == 121

    assert _build_references_section([], {}) == ""
    refs = _build_references_section(
        [{"id": "KU1", "source_file": "S1", "claim": "C1", "source_page": "10", "confidence": "high"}], {"KU1": 1}
    )
    assert "## References" in refs
    assert "[1] S1, p.10" in refs
    assert "confidence: high" in refs
