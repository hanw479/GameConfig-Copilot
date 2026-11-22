"""Retrieval layer (RAG) public API.

Business code should import only the neutral contracts and the retriever::

    from gameconfig_copilot.rag import Chunk, Hit, Retriever

Indexing/loading lives in :mod:`gameconfig_copilot.rag.index`; concrete storage
adapters live in :mod:`gameconfig_copilot.rag.stores` (ADR-007).
"""

from __future__ import annotations

from .chunker import chunk_constraints, chunk_task, chunk_wiki
from .fusion import DEFAULT_RRF_K, fuse_rankings, rrf_score
from .retriever import Retriever, build_filters, normalize_query
from .store import Chunk, Hit, VectorStore, matches_filters, normalize_filters

__all__ = [
    "DEFAULT_RRF_K",
    "Chunk",
    "Hit",
    "Retriever",
    "VectorStore",
    "build_filters",
    "chunk_constraints",
    "chunk_task",
    "chunk_wiki",
    "fuse_rankings",
    "matches_filters",
    "normalize_filters",
    "normalize_query",
    "rrf_score",
]
