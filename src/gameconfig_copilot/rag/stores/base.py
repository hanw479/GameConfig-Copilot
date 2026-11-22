"""Shared plumbing for sparse (BM25) adapters.

``SparseStore`` extends the neutral :class:`~gameconfig_copilot.rag.store.VectorStore`
contract with persistence hooks. The retriever only needs ``search``/``health``;
only :mod:`gameconfig_copilot.rag.index` uses ``save``/``load``/``chunks``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..store import Chunk, Hit

__all__ = [
    "SparseStore",
    "read_json",
    "write_json_atomic",
]

SPARSE_FORMAT_VERSION = 1


@runtime_checkable
class SparseStore(Protocol):
    """A :class:`VectorStore` that can persist its inverted index to disk."""

    backend_name: str

    def upsert(self, chunks: list[Chunk]) -> None: ...

    def search(self, query: str, filters: Mapping[str, Any], top_k: int) -> list[Hit]: ...

    def health(self) -> bool: ...

    @property
    def chunks(self) -> list[Chunk]:
        """The indexed chunks, in deterministic insertion order."""

    def save(self, directory: Path) -> None: ...


def write_json_atomic(path: Path, payload: Any) -> None:
    """Write canonical JSON via a sibling temp file + ``os.replace``.

    Canonical formatting (sorted keys, fixed separators, no timestamp) is what
    makes repeated index builds byte-for-byte identical, i.e. idempotent.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    tmp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with tmp_path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)
