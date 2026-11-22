"""Storage adapters for the retrieval layer.

The rest of the codebase depends only on the neutral
:class:`~gameconfig_copilot.rag.store.VectorStore` protocol. Selecting a backend
happens here and in :mod:`gameconfig_copilot.rag.index`; switching to Chroma or
Qdrant means adding a module to this package, not touching ``agents/`` or
``rag/retriever.py`` (ADR-007).

``build_sparse_store("auto")`` prefers ``bm25s`` when the wheel is importable and
falls back to the bundled pure-Python BM25 otherwise.
"""

from __future__ import annotations

from pathlib import Path

from .base import SparseStore
from .bm25 import Bm25Store
from .bm25s import Bm25sStore
from .noop import NoopDenseStore

__all__ = [
    "Bm25Store",
    "Bm25sStore",
    "NoopDenseStore",
    "SparseStore",
    "build_sparse_store",
    "load_sparse_store",
    "resolve_sparse_backend",
]

_BACKEND_ALIASES = {
    "auto": "auto",
    "python": "python-bm25",
    "pure": "python-bm25",
    "python-bm25": "python-bm25",
    "bm25s": "bm25s",
}


def resolve_sparse_backend(backend: str = "auto") -> str:
    """Resolve an alias to the concrete backend name (``python-bm25`` / ``bm25s``)."""

    key = (backend or "auto").strip().lower()
    if key not in _BACKEND_ALIASES:
        allowed = ", ".join(sorted(_BACKEND_ALIASES))
        raise ValueError(f"未知 BM25 backend: {backend!r}（可选：{allowed}）")
    resolved = _BACKEND_ALIASES[key]
    if resolved == "auto":
        return "bm25s" if Bm25sStore.available() else "python-bm25"
    return resolved


def build_sparse_store(
    backend: str = "auto",
    *,
    k1: float = 1.5,
    b: float = 0.75,
) -> SparseStore:
    """Build an empty sparse index on the requested backend."""

    resolved = resolve_sparse_backend(backend)
    if resolved == "bm25s":
        return Bm25sStore(k1=k1, b=b)
    return Bm25Store(k1=k1, b=b)


def load_sparse_store(directory: Path, backend: str) -> SparseStore:
    """Load a persisted sparse index; ``backend`` comes from the index manifest."""

    resolved = resolve_sparse_backend(backend)
    if resolved == "bm25s":
        return Bm25sStore.load(Path(directory))
    return Bm25Store.load(Path(directory))
