"""Optional ``bm25s`` adapter (used when the wheel is installed).

``bm25s`` is not a hard dependency of the project: if it is missing the pure
Python :class:`~gameconfig_copilot.rag.stores.bm25.Bm25Store` is used instead
(docs/03 S1). The adapter hides the library behind the same neutral contract;
business code never imports ``bm25s`` and never sees its ``.npy`` files.

Failure semantics
    Constructing/loading without the library raises :class:`RuntimeError` with a
    rebuild hint. A stale engine directory is removed before saving, so a
    smaller corpus cannot leave behind deleted documents.
"""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..store import Chunk, Hit, matches_filters
from ._tokenize import tokenize
from .base import SPARSE_FORMAT_VERSION, read_json, write_json_atomic

try:  # pragma: no cover - trivial import guard
    import bm25s
except ImportError:  # pragma: no cover - exercised on minimal installs
    bm25s = None  # type: ignore[assignment]

__all__ = ["Bm25sStore"]

_CHUNKS_FILE = "chunks.json"
_ENGINE_DIR = "engine"


class Bm25sStore:
    """``bm25s``-backed inverted index with JSON chunk metadata."""

    backend_name = "bm25s"

    def __init__(self, *, k1: float = 1.5, b: float = 0.75) -> None:
        if bm25s is None:
            raise RuntimeError(
                "bm25s 未安装；请改用 python-bm25（默认自动回退），或 `pip install bm25s`。"
            )
        if k1 <= 0:
            raise ValueError(f"k1 必须 > 0，当前为 {k1}")
        if not 0 <= b <= 1:
            raise ValueError(f"b 必须在 [0, 1] 内，当前为 {b}")
        self.k1 = float(k1)
        self.b = float(b)
        self._chunks: list[Chunk] = []
        self._engine: Any = None

    @staticmethod
    def available() -> bool:
        return bm25s is not None

    # -- neutral store contract ------------------------------------------------

    @property
    def chunks(self) -> list[Chunk]:
        return list(self._chunks)

    def upsert(self, chunks: list[Chunk]) -> None:
        if not chunks:
            return
        merged = list(self._chunks)
        position = {chunk.chunk_id: index for index, chunk in enumerate(merged)}
        for chunk in chunks:
            if not isinstance(chunk, Chunk):
                raise TypeError(f"upsert 只接受 Chunk，收到 {type(chunk)!r}")
            if chunk.chunk_id in position:
                merged[position[chunk.chunk_id]] = chunk
            else:
                position[chunk.chunk_id] = len(merged)
                merged.append(chunk)
        self._rebuild(merged)

    def search(self, query: str, filters: Mapping[str, Any], top_k: int) -> list[Hit]:
        if top_k <= 0:
            raise ValueError(f"top_k 必须 > 0，当前为 {top_k}")
        if not self._chunks or self._engine is None or not query or not query.strip():
            return []
        tokens = tokenize(query)
        if not tokens:
            return []
        allowed = [
            index
            for index, chunk in enumerate(self._chunks)
            if matches_filters(chunk.metadata, filters)
        ]
        if not allowed:
            return []
        scores = self._engine.get_scores(tokens)
        scored = [
            (float(scores[index]), index) for index in allowed if float(scores[index]) > 0.0
        ]
        scored.sort(key=lambda item: (-item[0], self._chunks[item[1]].chunk_id))
        return [
            Hit(
                chunk_id=self._chunks[index].chunk_id,
                text=self._chunks[index].text,
                score=score,
                source_id=self._chunks[index].source_id,
                metadata=self._chunks[index].metadata,
            )
            for score, index in scored[:top_k]
        ]

    def health(self) -> bool:
        return bm25s is not None

    # -- persistence -----------------------------------------------------------

    def save(self, directory: Path) -> None:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        engine_dir = directory / _ENGINE_DIR
        if engine_dir.exists():
            shutil.rmtree(engine_dir)
        if self._chunks and self._engine is not None:
            self._engine.save(str(engine_dir), show_progress=False)
        write_json_atomic(
            directory / _CHUNKS_FILE,
            {
                "backend": self.backend_name,
                "version": SPARSE_FORMAT_VERSION,
                "params": {"k1": self.k1, "b": self.b},
                "chunks": [chunk.to_dict() for chunk in self._chunks],
            },
        )

    @classmethod
    def load(cls, directory: Path) -> Bm25sStore:
        directory = Path(directory)
        payload = read_json(directory / _CHUNKS_FILE)
        if payload.get("version") != SPARSE_FORMAT_VERSION:
            raise ValueError(
                f"BM25S 索引版本不匹配：{payload.get('version')} != {SPARSE_FORMAT_VERSION}"
            )
        if payload.get("backend") != cls.backend_name:
            raise ValueError(f"BM25S 索引 backend 不匹配：{payload.get('backend')}")
        params = payload.get("params") or {}
        store = cls(k1=float(params.get("k1", 1.5)), b=float(params.get("b", 0.75)))
        store._chunks = [Chunk.from_dict(item) for item in payload.get("chunks") or []]
        engine_dir = directory / _ENGINE_DIR
        if store._chunks:
            if bm25s is None:
                raise RuntimeError(
                    "索引由 bm25s backend 构建，但当前环境缺少 bm25s；"
                    "请安装 bm25s 或 `gcc ingest --rebuild --bm25-backend python`。"
                )
            store._engine = bm25s.BM25.load(
                str(engine_dir),
                load_corpus=False,
                show_progress=False,
                verbosity=0,
            )
        return store

    # -- internals -------------------------------------------------------------

    def _rebuild(self, chunks: list[Chunk]) -> None:
        self._chunks = chunks
        if not chunks:
            self._engine = None
            return
        engine = bm25s.BM25(k1=self.k1, b=self.b)
        engine.index([tokenize(chunk.text) for chunk in chunks], show_progress=False)
        self._engine = engine
