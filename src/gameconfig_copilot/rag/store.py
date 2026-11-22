"""Neutral retrieval-layer contracts (ADR-007).

This module is the only cross-worker retrieval contract: business code
(``agents/``, ``evaluation/``) may depend on :class:`Chunk`, :class:`Hit`,
:class:`VectorStore` and the neutral ``filters`` semantics defined here, but
must never depend on a concrete vector-database syntax.

Input
    ``Chunk`` objects produced by :mod:`gameconfig_copilot.rag.chunker`.
Output
    ``list[Hit]`` ordered by descending relevance.
Failure semantics
    ``VectorStore`` implementations raise their own adapter errors; callers get
    an empty list for an empty query / an empty index. A degraded adapter may
    report ``health() is False`` and return no hits instead of crashing the
    pipeline.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "FILTER_KEYS",
    "Chunk",
    "Hit",
    "VectorStore",
    "matches_filters",
    "normalize_filters",
]

#: Neutral filter keys frozen by docs/06 §5.4 and docs/02 §5.2.
FILTER_KEYS = frozenset({"source_type", "task_type", "level_band", "npc_id"})


@dataclass(frozen=True, slots=True)
class Chunk:
    """A retrieval unit with a neutral payload (no vector-store specifics).

    ``metadata`` is the payload and carries at least ``source_type`` and
    ``source_id``; ``parent_id`` links a child block back to its parent block
    (docs/03 S1 "父块回链").
    """

    chunk_id: str
    text: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.chunk_id:
            raise ValueError("chunk_id 不能为空")
        if not isinstance(self.metadata, Mapping):
            raise TypeError("metadata 必须是映射（payload）")

    @property
    def source_id(self) -> str:
        """Stable source reference, e.g. ``wiki:world_overview`` / ``task:T_SIDE_0213``."""

        return str(self.metadata.get("source_id") or self.chunk_id)

    @property
    def source_type(self) -> str:
        return str(self.metadata.get("source_type") or "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "text": self.text,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Chunk:
        return cls(
            chunk_id=str(payload["chunk_id"]),
            text=str(payload.get("text", "")),
            metadata=dict(payload.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class Hit:
    """One search result (contract from docs/06 §5.4).

    ``metadata`` mirrors the chunk payload so callers can render a citation
    (``source_type`` / ``source_id`` / ``position`` / ``parent_id``). The
    ``position`` convenience property is the location inside the source
    (heading path, JSON field name, YAML scope).
    """

    chunk_id: str
    text: str
    score: float
    source_id: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def position(self) -> str:
        return str(self.metadata.get("position") or "")

    @property
    def source_type(self) -> str:
        return str(self.metadata.get("source_type") or "")

    @property
    def parent_id(self) -> str | None:
        value = self.metadata.get("parent_id")
        return str(value) if value else None

    @property
    def citation(self) -> str:
        """``source_id#position`` — matches ``provenance.evidence`` refs."""

        return f"{self.source_id}#{self.position}" if self.position else self.source_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "text": self.text,
            "score": self.score,
            "source_id": self.source_id,
            "metadata": dict(self.metadata),
        }


@runtime_checkable
class VectorStore(Protocol):
    """Thin storage contract; ``filters`` are neutral dictionaries.

    The adapter translates ``filters`` into its native syntax, so business code
    never contains ``where={"$and": ...}`` or ``collection.get(ids=...)``.
    """

    def upsert(self, chunks: list[Chunk]) -> None:
        """Insert or replace chunks by ``chunk_id`` (idempotent)."""

    def search(self, query: str, filters: Mapping[str, Any], top_k: int) -> list[Hit]:
        """Return at most ``top_k`` hits passing ``filters``, best first."""

    def health(self) -> bool:
        """Whether the adapter can currently serve searches."""


def normalize_filters(filters: Mapping[str, Any] | None) -> dict[str, Any]:
    """Drop empty values and canonicalise ``level_band`` to a tuple of ints."""

    if not filters:
        return {}
    normalized: dict[str, Any] = {}
    for key, value in filters.items():
        if value is None or value == "" or value == [] or value == ():
            continue
        if key == "level_band":
            band = tuple(int(item) for item in value)  # type: ignore[arg-type]
            if len(band) != 2:
                raise ValueError("level_band 必须是 (min, max) 两元组")
            if band[0] > band[1]:
                raise ValueError(f"level_band 下限 {band[0]} 大于上限 {band[1]}")
            normalized[key] = band
            continue
        if isinstance(value, str):
            text = value.strip()
            if text:
                normalized[key] = text
            continue
        if isinstance(value, (list, tuple, set, frozenset)):
            items = [item for item in value if item not in (None, "")]
            if items:
                normalized[key] = items
            continue
        normalized[key] = value
    return normalized


def _as_allowed(value: Any) -> Sequence[Any] | None:
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    return None


def matches_filters(metadata: Mapping[str, Any], filters: Mapping[str, Any] | None) -> bool:
    """Apply neutral structural filters to one payload.

    ``level_band`` matches by **range overlap**: a chunk carrying
    ``level_min/level_max`` matches when the two ranges intersect. Payloads that
    carry no level information never match a level filter, which keeps
    "30 级任务的奖励结构" out of a Lv6–9 query (docs/03 S1).
    """

    if not filters:
        return True
    for key, expected in filters.items():
        if expected is None:
            continue
        if key == "level_band":
            band = tuple(int(item) for item in expected)  # type: ignore[arg-type]
            if len(band) != 2:
                raise ValueError("level_band 必须是 (min, max) 两元组")
            level_min = metadata.get("level_min")
            level_max = metadata.get("level_max")
            if level_min is None or level_max is None:
                return False
            if int(level_max) < band[0] or int(level_min) > band[1]:
                return False
            continue
        allowed = _as_allowed(expected)
        actual = metadata.get(key)
        if allowed is not None:
            if actual not in allowed:
                return False
        elif actual != expected:
            return False
    return True
