"""Degraded dense-store adapter (no embedding dependency in this wave).

Why a no-op? Dense retrieval needs an embedding model (BGE-M3) and a vector
database (Chroma). Neither is a dependency of S1, and downloading weights in an
offline/sandboxed checkout would make the demo fragile. ``NoopDenseStore`` keeps
the pipeline shape intact: it accepts ``upsert`` so the index build still has a
dense slot, returns ``[]`` from ``search``, and reports ``health() is False`` so
the retriever transparently falls back to BM25 + RRF.

Upgrade path (docs/03 S1-05): add ``rag/stores/chroma.py`` with the same
``VectorStore`` methods (``upsert`` / ``search`` / ``health``) and embed BGE-M3
inside that adapter. ``rag/index.py`` then selects it; ``rag/retriever.py`` and
``agents/`` stay untouched (ADR-007).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..store import Chunk, Hit

__all__ = ["NoopDenseStore"]


class NoopDenseStore:
    """A dense store that deliberately stores nothing and returns no hits."""

    backend_name = "noop"

    def __init__(self) -> None:
        self._chunks: list[Chunk] = []

    @property
    def chunks(self) -> list[Chunk]:
        return list(self._chunks)

    def upsert(self, chunks: list[Chunk]) -> None:
        self._chunks = list(chunks)

    def search(self, query: str, filters: Mapping[str, Any], top_k: int) -> list[Hit]:
        del query, filters, top_k
        return []

    def health(self) -> bool:
        return False

    def save(self, directory: Path) -> None:
        """No vector data to persist; the index manifest records the backend name."""

        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "noop.txt").write_text(
            "dense retrieval disabled: NoopDenseStore (BM25 + RRF only)\n",
            encoding="utf-8",
        )
