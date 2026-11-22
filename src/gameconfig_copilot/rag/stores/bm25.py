"""Pure-Python BM25 adapter (no third-party binary dependency).

Used when ``bm25s`` is not installed, and as the deterministic backend in unit
tests. Chinese text is tokenised into 2-grams plus whole ASCII tokens by
:mod:`gameconfig_copilot.rag.stores._tokenize`.

Contract
--------
Implements the neutral ``VectorStore`` protocol (``upsert`` / ``search`` /
``health``) and adds ``save`` for index persistence. Structural ``filters`` are
applied **before scoring**, so a filtered-out document can never appear in the
top-k even if it would rank first (docs/03 S1 "过滤优先").

Failure semantics
    ``top_k <= 0`` or malformed params raise :class:`ValueError`. An empty
    corpus/query returns ``[]``; a chunk with the same ``chunk_id`` is replaced
    in place, so re-indexing the same corpus is idempotent.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..store import Chunk, Hit, matches_filters
from ._tokenize import tokenize
from .base import SPARSE_FORMAT_VERSION, read_json, write_json_atomic

__all__ = ["Bm25Store"]

_FILE_NAME = "bm25.json"


class Bm25Store:
    """Inverted index with Okapi BM25 scoring and JSON persistence."""

    backend_name = "python-bm25"

    def __init__(self, *, k1: float = 1.5, b: float = 0.75) -> None:
        if k1 <= 0:
            raise ValueError(f"k1 必须 > 0，当前为 {k1}")
        if not 0 <= b <= 1:
            raise ValueError(f"b 必须在 [0, 1] 内，当前为 {b}")
        self.k1 = float(k1)
        self.b = float(b)
        self._chunks: list[Chunk] = []
        self._term_freqs: list[dict[str, int]] = []
        self._lengths: list[int] = []
        self._document_freqs: dict[str, int] = {}
        self._avg_length = 0.0

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
        if not self._chunks or not query or not query.strip():
            return []
        query_counts = Counter(tokenize(query))
        if not query_counts:
            return []
        allowed = [
            index
            for index, chunk in enumerate(self._chunks)
            if matches_filters(chunk.metadata, filters)
        ]
        if not allowed:
            return []

        total_docs = len(self._chunks)
        average_length = self._avg_length or 1.0
        idf = {
            token: math.log(
                1.0 + (total_docs - self._document_freqs.get(token, 0) + 0.5)
                / (self._document_freqs.get(token, 0) + 0.5)
            )
            for token in query_counts
        }
        scored: list[tuple[float, int]] = []
        for index in allowed:
            term_freqs = self._term_freqs[index]
            length = self._lengths[index] or 1
            norm = self.k1 * (1.0 - self.b + self.b * length / average_length)
            score = 0.0
            for token, query_freq in query_counts.items():
                freq = term_freqs.get(token)
                if not freq:
                    continue
                score += idf[token] * freq * (self.k1 + 1.0) / (freq + norm) * query_freq
            if score > 0.0:
                scored.append((score, index))

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
        return True

    # -- persistence -----------------------------------------------------------

    def save(self, directory: Path) -> None:
        directory = Path(directory)
        payload = {
            "backend": self.backend_name,
            "version": SPARSE_FORMAT_VERSION,
            "params": {"k1": self.k1, "b": self.b},
            "docs": [
                {
                    "chunk": chunk.to_dict(),
                    "tf": self._term_freqs[index],
                    "length": self._lengths[index],
                }
                for index, chunk in enumerate(self._chunks)
            ],
            "df": self._document_freqs,
        }
        write_json_atomic(directory / _FILE_NAME, payload)

    @classmethod
    def load(cls, directory: Path) -> Bm25Store:
        payload = read_json(Path(directory) / _FILE_NAME)
        if payload.get("version") != SPARSE_FORMAT_VERSION:
            raise ValueError(
                f"BM25 索引版本不匹配：{payload.get('version')} != {SPARSE_FORMAT_VERSION}"
            )
        if payload.get("backend") != cls.backend_name:
            raise ValueError(f"BM25 索引 backend 不匹配：{payload.get('backend')}")
        params = payload.get("params") or {}
        store = cls(k1=float(params.get("k1", 1.5)), b=float(params.get("b", 0.75)))
        docs = payload.get("docs") or []
        chunks: list[Chunk] = []
        term_freqs: list[dict[str, int]] = []
        lengths: list[int] = []
        for doc in docs:
            chunks.append(Chunk.from_dict(doc["chunk"]))
            term_freqs.append({str(key): int(value) for key, value in doc["tf"].items()})
            lengths.append(int(doc["length"]))
        store._chunks = chunks
        store._term_freqs = term_freqs
        store._lengths = lengths
        store._document_freqs = {str(key): int(value) for key, value in payload["df"].items()}
        store._avg_length = (sum(lengths) / len(lengths)) if lengths else 0.0
        return store

    # -- internals -------------------------------------------------------------

    def _rebuild(self, chunks: list[Chunk]) -> None:
        self._chunks = chunks
        self._term_freqs = []
        self._lengths = []
        self._document_freqs = {}
        for chunk in chunks:
            counts = Counter(tokenize(chunk.text))
            self._term_freqs.append(dict(counts))
            self._lengths.append(sum(counts.values()))
            for token in counts:
                self._document_freqs[token] = self._document_freqs.get(token, 0) + 1
        total = sum(self._lengths)
        self._avg_length = (total / len(chunks)) if chunks else 0.0
