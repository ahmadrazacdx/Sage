from sage.agents.diagram import (
    _inject_mermaid_styling,
    _is_valid_mermaid,
    _parse_description_json,
    _sanitize_mermaid_ids,
    _strip_fences,
    _trim_description_for_mermaid,
)


def test_strip_fences():
    text = "<think>thought</think>```mermaid\ngraph TD; A-->B\n``` noisy end"
    assert _strip_fences(text) == "graph TD; A-->B"

    text2 = "graph TD; A-->B"
    assert _strip_fences(text2) == "graph TD; A-->B"


def test_is_valid_mermaid():
    v, msg = _is_valid_mermaid("flowchart TD\nA-->B")
    assert v is True
    v, msg = _is_valid_mermaid("random text")
    assert v is False
    v, msg = _is_valid_mermaid("flowchart TD\nsubgraph S\nA-->B")
    assert "Unbalanced" in msg
    v, msg = _is_valid_mermaid("flowchart TD\nA[Node]")
    assert "no edges" in msg


def test_parse_description_json():
    raw = '```json\n{"nodes": [{"id": "n1"}]}\n```'
    parsed = _parse_description_json(raw)
    assert parsed["nodes"][0]["id"] == "n1"

    assert _parse_description_json("invalid") is None


def test_inject_mermaid_styling():
    code = "flowchart TD\n    A-->B\n    subgraph S1\n    C-.->D\n    end"
    styled = _inject_mermaid_styling(code, {"nodes": [{"id": "A"}]})
    assert "classDef primary" in styled
    assert "style S1" in styled
    assert "linkStyle" in styled
    assert "class A primary" in styled


def test_sanitize_mermaid_ids():
    code = "flowchart TD\n    end[Finish]\n    A-->end"
    sanitized = _sanitize_mermaid_ids(code)
    assert "end_n" in sanitized
    assert "end[" not in sanitized


def test_trim_description_for_mermaid():
    nodes = [{"id": str(i)} for i in range(30)]
    edges = [{"from": "1", "to": "2"}, {"from": "1", "to": "25"}]
    desc = {"nodes": nodes, "edges": edges}
    trimmed = _trim_description_for_mermaid(desc)
    assert len(trimmed["nodes"]) == 17
    assert len(trimmed["edges"]) == 1
