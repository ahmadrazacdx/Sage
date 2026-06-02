import pytest
from unittest.mock import patch, MagicMock
from sage.rag.vectorstore import get_chroma_client, get_curriculum_collection, build_course_filter
import sage.rag.vectorstore as vs

@pytest.fixture(autouse=True)
def reset_singletons():
    vs._client = None
    vs._collection = None
    yield
    vs._client = None
    vs._collection = None

def test_build_course_filter():
    assert build_course_filter(None) is None
    assert build_course_filter("all") is None
    assert build_course_filter("ALL") is None
    assert build_course_filter(" CMPC101 ") == {"course_code": {"$eq": "CMPC101"}}

@patch("sage.rag.vectorstore.chromadb.PersistentClient")
@patch("sage.rag.vectorstore.get_settings")
def test_get_chroma_client(mock_settings, mock_persistent_client):
    mock_settings.return_value.rag.vectordb = "dummy/path"
    
    mock_client_instance = MagicMock()
    mock_persistent_client.return_value = mock_client_instance

    client1 = get_chroma_client()
    client2 = get_chroma_client()

    assert client1 is client2
    assert client1 is mock_client_instance
    mock_persistent_client.assert_called_once_with(path="dummy/path")

@patch("sage.rag.vectorstore.get_chroma_client")
@patch("sage.rag.vectorstore.get_settings")
def test_get_curriculum_collection(mock_settings, mock_get_client):
    mock_settings.return_value.rag.curriculum_collection = "my_collection"
    
    mock_client = MagicMock()
    mock_collection = MagicMock()
    mock_collection.count.return_value = 10
    mock_client.get_or_create_collection.return_value = mock_collection
    mock_get_client.return_value = mock_client

    coll1 = get_curriculum_collection()
    coll2 = get_curriculum_collection()

    assert coll1 is coll2
    assert coll1 is mock_collection
    mock_client.get_or_create_collection.assert_called_once_with(
        name="my_collection",
        metadata={"hnsw:space": "cosine"}
    )
