"""Hybrid index orchestration: corpus -> chunks -> (BM25 + dense) on disk.

The index is deliberately thin: it owns chunk ordering, the manifest and
persistence, while scoring lives in :mod:`gameconfig_copilot.rag.stores.*` and
fusion lives in :mod:`gameconfig_copilot.rag.fusion`. Rebuilding the same corpus
produces the same chunk ids, the same manifest (no timestamps) and the same
canonical JSON, so ``gcc ingest`` is idempotent.

Layout::

    out/index/
      manifest.json
      chunks.json
      bm25/bm25.json          # python-bm25 backend
      bm25/engine/*.npy       # bm25s backend
      dense/noop.txt          # NoopDenseStore (dense disabled)

Failure semantics
    A missing/invalid index raises :class:`IndexLoadError` with a rebuild hint;
    an empty corpus raises :class:`IndexBuildError`. The manifest is written
    last, so a crashed build never looks like a valid index.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .corpus import load_chunks
from .store import Chunk, VectorStore
from .stores import (
    NoopDenseStore,
    SparseStore,
    build_sparse_store,
    load_sparse_store,
    resolve_sparse_backend,
)
from .stores.base import read_json, write_json_atomic

__all__ = [
    "DEFAULT_INDEX_DIR",
    "INDEX_VERSION",
    "IndexBuildError",
    "IndexLoadError",
    "IndexStats",
    "RagIndex",
    "build_index",
    "corpus_digest",
    "load_index",
]

INDEX_VERSION = 1
# ``data/`` is corpus-only (owned by the data worker, read-only for RAG); the
# run-time index lives in the already-gitignored ``out/`` directory.
DEFAULT_INDEX_DIR = Path("out/index")
MANIFEST_FILE = "manifest.json"
CHUNKS_FILE = "chunks.json"
SPARSE_DIR = "bm25"
DENSE_DIR = "dense"


class IndexBuildError(RuntimeError):
    """Raised when the corpus cannot be turned into an index."""


class IndexLoadError(RuntimeError):
    """Raised when an index directory is missing, stale or corrupted."""


@dataclass(frozen=True, slots=True)
class IndexStats:
    index_dir: Path
    chunk_count: int
    by_source_type: Mapping[str, int]
    sparse_backend: str
    dense_backend: str
    corpus_digest: str
    reused: bool = False


@dataclass(slots=True)
class RagIndex:
    """An in-memory index handle that can be searched or persisted."""

    sparse: SparseStore
    dense: VectorStore
    chunks: list[Chunk]
    manifest: Mapping[str, Any]
    index_dir: Path
    reused: bool = False
    _stats_by_type: dict[str, int] = field(default_factory=dict, repr=False)

    @property
    def bm25(self) -> SparseStore:
        """Alias used by ``rag/retriever.py`` / CLI code."""

        return self.sparse

    @property
    def chunk_count(self) -> int:
        return len(self.chunks)

    def stats(self, *, reused: bool | None = None) -> IndexStats:
        return IndexStats(
            index_dir=self.index_dir,
            chunk_count=len(self.chunks),
            by_source_type=dict(self._stats_by_type),
            sparse_backend=self.sparse.backend_name,
            dense_backend=str(self.manifest.get("dense_backend", "noop")),
            corpus_digest=str(self.manifest.get("corpus_digest", "")),
            reused=self.reused if reused is None else reused,
        )

    def save(self) -> None:
        self.index_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = self.index_dir / MANIFEST_FILE
        if manifest_path.exists():
            manifest_path.unlink()
        # Recreate the backend directory so switching backends leaves no stale
        # artifacts behind (a reader must never see mixed bm25/bm25s files).
        sparse_dir = self.index_dir / SPARSE_DIR
        if sparse_dir.exists():
            shutil.rmtree(sparse_dir)
        self.sparse.save(sparse_dir)
        dense_save = getattr(self.dense, "save", None)
        if callable(dense_save):
            dense_save(self.index_dir / DENSE_DIR)
        write_json_atomic(
            self.index_dir / CHUNKS_FILE,
            {
                "version": INDEX_VERSION,
                "chunks": [chunk.to_dict() for chunk in self.chunks],
            },
        )
        write_json_atomic(manifest_path, dict(self.manifest))


def corpus_digest(chunks: Sequence[Chunk]) -> str:
    """Stable SHA-256 over canonical chunk JSON (used to skip rebuilds)."""

    text = json.dumps(
        [chunk.to_dict() for chunk in chunks],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _count_by_source_type(chunks: Sequence[Chunk]) -> dict[str, int]:
    return dict(sorted(Counter(chunk.source_type for chunk in chunks).items()))


def _build_manifest(
    chunks: Sequence[Chunk],
    *,
    sparse_backend: str,
    dense_backend: str,
    digest: str,
) -> dict[str, Any]:
    return {
        "index_version": INDEX_VERSION,
        "corpus_digest": digest,
        "chunk_count": len(chunks),
        "by_source_type": _count_by_source_type(chunks),
        "sparse_backend": sparse_backend,
        "dense_backend": dense_backend,
    }


def build_index(
    source_dir: Path | str = "data",
    index_dir: Path | str = DEFAULT_INDEX_DIR,
    *,
    rebuild: bool = False,
    sparse_backend: str = "auto",
    dense_store: VectorStore | None = None,
) -> RagIndex:
    """Build (or reuse) the hybrid index for ``source_dir``.

    ``rebuild=False`` reuses an existing index when its corpus digest matches,
    which keeps repeated ``gcc ingest`` calls idempotent and fast.
    """

    target = Path(index_dir)
    try:
        chunks = load_chunks(source_dir)
    except RuntimeError as exc:
        raise IndexBuildError(str(exc)) from exc
    if not chunks:
        raise IndexBuildError(f"没有从 {source_dir} 载入任何 chunk")

    digest = corpus_digest(chunks)
    requested_backend = sparse_backend.strip().lower()
    if not rebuild and (target / MANIFEST_FILE).is_file():
        try:
            existing = load_index(target)
        except IndexLoadError:
            existing = None
        backend_matches = requested_backend == "auto" or (
            existing is not None
            and existing.sparse.backend_name == resolve_sparse_backend(sparse_backend)
        )
        if (
            existing is not None
            and existing.manifest.get("corpus_digest") == digest
            and backend_matches
        ):
            existing.reused = True
            return existing

    sparse = build_sparse_store(sparse_backend)
    sparse.upsert(list(chunks))
    dense = dense_store if dense_store is not None else NoopDenseStore()
    dense.upsert(list(chunks))
    manifest = _build_manifest(
        chunks,
        sparse_backend=sparse.backend_name,
        dense_backend=getattr(dense, "backend_name", "noop"),
        digest=digest,
    )
    index = RagIndex(
        sparse=sparse,
        dense=dense,
        chunks=list(chunks),
        manifest=manifest,
        index_dir=target,
        _stats_by_type=_count_by_source_type(chunks),
    )
    index.save()
    return index


def load_index(index_dir: Path | str = DEFAULT_INDEX_DIR) -> RagIndex:
    """Load a persisted index; raises :class:`IndexLoadError` with a rebuild hint."""

    directory = Path(index_dir)
    manifest_path = directory / MANIFEST_FILE
    if not manifest_path.is_file():
        raise IndexLoadError(
            f"索引不存在或未完成构建：{manifest_path}；先运行 `gcc ingest`。"
        )
    try:
        manifest = read_json(manifest_path)
    except (OSError, json.JSONDecodeError) as exc:
        raise IndexLoadError(f"索引 manifest 损坏：{manifest_path}: {exc}") from exc
    if manifest.get("index_version") != INDEX_VERSION:
        raise IndexLoadError(
            f"索引版本不匹配：{manifest.get('index_version')} != {INDEX_VERSION}；"
            "请运行 `gcc ingest --rebuild`。"
        )
    chunks_path = directory / CHUNKS_FILE
    try:
        chunk_payload = read_json(chunks_path)
        chunks = [Chunk.from_dict(item) for item in chunk_payload["chunks"]]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise IndexLoadError(f"索引 chunk 文件损坏：{chunks_path}: {exc}") from exc

    sparse_backend = str(manifest.get("sparse_backend") or "python-bm25")
    try:
        sparse = load_sparse_store(directory / SPARSE_DIR, sparse_backend)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        raise IndexLoadError(
            f"无法加载 BM25 索引（{sparse_backend}）：{exc}；"
            "请运行 `gcc ingest --rebuild --bm25-backend python`。"
        ) from exc
    if {chunk.chunk_id for chunk in sparse.chunks} != {chunk.chunk_id for chunk in chunks}:
        raise IndexLoadError(
            "索引的 BM25 文档与 chunks.json 不一致；请运行 `gcc ingest --rebuild`。"
        )

    dense_backend = str(manifest.get("dense_backend") or "noop")
    if dense_backend != "noop":
        raise IndexLoadError(
            f"未知 dense backend：{dense_backend}；请运行 `gcc ingest --rebuild`。"
        )
    dense = NoopDenseStore()
    dense.upsert(list(chunks))
    return RagIndex(
        sparse=sparse,
        dense=dense,
        chunks=list(chunks),
        manifest=manifest,
        index_dir=directory,
        _stats_by_type=dict(manifest.get("by_source_type") or {}),
    )
