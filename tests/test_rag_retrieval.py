import sys
from unittest.mock import patch, MagicMock, mock_open

mock_fastembed = MagicMock()
sys.modules['fastembed'] = mock_fastembed

import pytest
import pickle
import asyncio
from sage.rag.retrieval import (
    _get_embed_model,
    _embed_query,
    _load_bm25_index,
    _rrf_fuse,
    _dense_retrieve,
    _sparse_retrieve,
    _hydrate_chunks,
    hybrid_retrieve
)

def test_get_embed_model():
    with patch("sage.rag.retrieval.get_settings") as mock_settings:
        mock_settings.return_value.embedding.embed_model = "dummy/path"
        model = _get_embed_model()
        model2 = _get_embed_model()
        assert model is model2

def test_embed_query():
    with patch("sage.rag.retrieval._get_embed_model") as mock_get_model:
        mock_model = MagicMock()
        mock_model.embed.return_value = [[0.1, 0.2, 0.3]]
        mock_get_model.return_value = mock_model
        
        vec = _embed_query("hello")
        assert vec == [0.1, 0.2, 0.3]
        mock_model.embed.assert_called_once_with(["hello"])

class DummyBM25:
    pass

def test_load_bm25_index(tmp_path):
    with patch("sage.rag.retrieval.get_settings") as mock_settings:
        bm25_path = tmp_path / "bm25.pkl"
        mock_settings.return_value.rag.bm25_curriculum_file = str(bm25_path)
        
        with pytest.raises(FileNotFoundError):
            _load_bm25_index.__wrapped__() 

        mock_bm25 = DummyBM25()
        data = {
            "bm25": mock_bm25,
            "ids": ["id1", "id2"],
            "metadatas": [{"course_code": "CS101"}, {}]
        }
        with open(bm25_path, "wb") as f:
            pickle.dump(data, f)
            
        bm25, ids, courses = _load_bm25_index.__wrapped__()
        assert isinstance(bm25, DummyBM25)
        assert ids == ["id1", "id2"]
        assert courses == ["CS101", ""]

def test_rrf_fuse():
    dense_ids = ["a", "b", "c"]
    sparse_ids = ["c", "a", "d"]
    fused = _rrf_fuse(dense_ids, sparse_ids, k=60)
    assert fused[0] == "a"
    assert fused[1] == "c"
    assert "b" in fused
    assert "d" in fused

def test_dense_retrieve():
    with patch("sage.rag.retrieval.get_curriculum_collection") as mock_get_coll, \
         patch("sage.rag.retrieval._embed_query") as mock_embed:
        
        mock_embed.return_value = [0.1, 0.2]
        mock_coll = MagicMock()
        mock_coll.count.return_value = 10
        mock_coll.query.return_value = {
            "ids": [["id1", "id2"]],
            "metadatas": [[{"course_code": "CS101"}, {"course_code": "CS102"}]],
            "documents": [["doc1", "doc2"]]
        }
        mock_get_coll.return_value = mock_coll
        
        ids, metas = _dense_retrieve("query", n_results=2, where=None)
        assert ids == ["id1", "id2"]
        assert metas[0]["course_code"] == "CS101"
        assert metas[0]["_text"] == "doc1"
        
        mock_coll.query.side_effect = Exception("db error")
        ids2, metas2 = _dense_retrieve("query", n_results=2, where=None)
        assert ids2 == []
        assert metas2 == []

def test_sparse_retrieve():
    with patch("sage.rag.retrieval._load_bm25_index") as mock_load:
        mock_load.side_effect = FileNotFoundError()
        assert _sparse_retrieve("query", n_results=5, course_code=None) == []
        
        mock_bm25 = MagicMock()
        import numpy as np
        mock_bm25.get_scores.return_value = np.array([0.9, 0.1, 0.5])
        mock_load.side_effect = None
        mock_load.return_value = (mock_bm25, ["id1", "id2", "id3"], ["CS101", "CS102", "CS101"])
        
        ids = _sparse_retrieve("query", n_results=2, course_code=None)
        assert ids == ["id1", "id3"]
        
        ids2 = _sparse_retrieve("query", n_results=2, course_code="CS102")
        assert ids2 == ["id2"]

def test_hydrate_chunks():
    fused_ids = ["id1", "id2", "id3"]
    dense_meta_map = {
        "id1": {"course_code": "CS101", "_text": "doc1", "doc_title": "lec1"},
    }
    
    with patch("sage.rag.retrieval.get_curriculum_collection") as mock_get_coll:
        mock_coll = MagicMock()
        mock_coll.get.return_value = {
            "ids": ["id2"],
            "metadatas": [{"course_code": "CS102"}],
            "documents": ["doc2"]
        }
        mock_get_coll.return_value = mock_coll
        
        chunks = _hydrate_chunks(fused_ids, dense_meta_map, top_k=2)
        assert len(chunks) == 2
        assert chunks[0]["id"] == "id1"
        assert chunks[0]["text"] == "doc1"
        assert chunks[1]["id"] == "id2"
        assert chunks[1]["text"] == "doc2"
        mock_coll.get.assert_called_once_with(ids=["id2"], include=["metadatas", "documents"])
        
        mock_coll.get.side_effect = Exception("db error")
        chunks2 = _hydrate_chunks(["id3"], dense_meta_map, top_k=1)
        assert len(chunks2) == 0

@pytest.mark.asyncio
async def test_hybrid_retrieve():
    with patch("sage.rag.retrieval.get_settings") as mock_settings, \
         patch("sage.rag.retrieval._dense_retrieve") as mock_dense, \
         patch("sage.rag.retrieval._sparse_retrieve") as mock_sparse, \
         patch("sage.rag.retrieval._rrf_fuse") as mock_rrf, \
         patch("sage.rag.retrieval._hydrate_chunks") as mock_hyd:
        
        cfg = mock_settings.return_value.rag
        cfg.top_k = 2
        cfg.retrieval_multiplier = 2
        cfg.rrf_k_constant = 60
        
        mock_dense.return_value = (["id1"], [{"_text": "doc1"}])
        mock_sparse.return_value = ["id1", "id2"]
        mock_rrf.return_value = ["id1", "id2"]
        mock_hyd.return_value = [{"id": "id1"}, {"id": "id2"}]
        
        chunks = await hybrid_retrieve("query", course_code="CS101")
        assert len(chunks) == 2
        mock_dense.assert_called_once()
        mock_sparse.assert_called_once()
        mock_rrf.assert_called_once()
        mock_hyd.assert_called_once()
