"""Reciprocal Rank Fusion (RRF) — a pure, dependency-free function.

Implements the formula from docs/02 §5.2::

    score(d) = Σ_i 1 / (k + rank_i(d)),  k = 60

Why not a vector database's built-in hybrid API? See ADR-007: fusion must be
unit-testable, reproducible, and independent of the storage backend so switching
to Qdrant in S7 changes only an adapter.

Input
    Ranked chunk-id lists (dense ranking first, sparse ranking second, ...).
Output
    ``[(chunk_id, rrf_score), ...]`` sorted by descending score; equal scores are
    ordered by ``chunk_id`` so the result is stable under any permutation of the
    input rankings (important for reproducible evaluation).
Failure semantics
    Empty input yields an empty list. Duplicate ids inside one ranking are
    counted once (a malformed list must not inflate a document's score).
"""

from __future__ import annotations

import math
from collections.abc import Sequence

__all__ = ["DEFAULT_RRF_K", "fuse_rankings", "rrf_contributions", "rrf_score"]

#: Default RRF constant from docs/02 §5.2 / docs/03 S1.
DEFAULT_RRF_K = 60


def _validate_k(k: int) -> int:
    if isinstance(k, bool) or not isinstance(k, int):
        raise TypeError("k 必须是整数")
    if k < 0:
        raise ValueError(f"k 必须 >= 0，当前为 {k}")
    return k


def rrf_score(rank: int, *, k: int = DEFAULT_RRF_K) -> float:
    """Score one document at 1-based ``rank`` for a single ranking."""

    if rank < 1:
        raise ValueError(f"rank 从 1 开始，当前为 {rank}")
    return 1.0 / (_validate_k(k) + rank)


def rrf_contributions(
    rankings: Sequence[Sequence[str]],
    *,
    k: int = DEFAULT_RRF_K,
) -> dict[str, float]:
    """Aggregate per-document scores without sorting (useful for inspection)."""

    _validate_k(k)
    contributions: dict[str, list[float]] = {}
    seen: dict[str, set[int]] = {}
    for list_index, ranking in enumerate(rankings):
        for rank, chunk_id in enumerate(ranking, start=1):
            if not chunk_id:
                continue
            seen.setdefault(chunk_id, set())
            if list_index in seen[chunk_id]:
                continue  # duplicate inside the same ranking: count once
            seen[chunk_id].add(list_index)
            contributions.setdefault(chunk_id, []).append(1.0 / (k + rank))
    # ``math.fsum`` keeps sums order-independent, so swapping the ranking lists
    # does not change scores even in floating point.
    return {chunk_id: math.fsum(values) for chunk_id, values in contributions.items()}


def fuse_rankings(
    rankings: Sequence[Sequence[str]],
    *,
    k: int = DEFAULT_RRF_K,
) -> list[tuple[str, float]]:
    """Fuse ranked id lists into ``[(chunk_id, score)]``, best first."""

    scores = rrf_contributions(rankings, k=k)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))
