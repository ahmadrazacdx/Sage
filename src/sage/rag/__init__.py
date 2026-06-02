"""
RAG sub-package for Sage.

Usage:
    from sage.rag import hybrid_retrieve
    chunks = await hybrid_retrieve("explain binary search", course_code="CMPC201")
"""

from sage.rag.retrieval import hybrid_retrieve

__all__ = ["hybrid_retrieve"]