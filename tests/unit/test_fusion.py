"""RRF unit tests (S1-06 acceptance: same scores / empty lists / unordered input)."""

from __future__ import annotations

import pytest

from gameconfig_copilot.rag.fusion import (
    DEFAULT_RRF_K,
    fuse_rankings,
    rrf_contributions,
    rrf_score,
)


def test_default_k_is_60_and_formula_matches_docs() -> None:
    assert DEFAULT_RRF_K == 60
    assert rrf_score(1) == pytest.approx(1 / 61)
    assert rrf_score(3, k=10) == pytest.approx(1 / 13)


def test_single_ranking_keeps_order_and_scores() -> None:
    fused = fuse_rankings([["a", "b", "c"]])

    assert [chunk_id for chunk_id, _ in fused] == ["a", "b", "c"]
    assert fused[0][1] == pytest.approx(1 / 61)
    assert fused[2][1] == pytest.approx(1 / 63)
    assert fused[0][1] > fused[1][1] > fused[2][1]


def test_empty_inputs_yield_empty_output() -> None:
    assert fuse_rankings([]) == []
    assert fuse_rankings([[], []]) == []
    assert rrf_contributions([]) == {}


def test_two_rankings_are_summed() -> None:
    fused = dict(fuse_rankings([["a", "b"], ["b", "c"]]))

    assert fused["b"] == pytest.approx(1 / 62 + 1 / 61)
    assert fused["a"] == pytest.approx(1 / 61)
    assert fused["c"] == pytest.approx(1 / 62)


def test_list_order_does_not_change_scores_or_order() -> None:
    forward = fuse_rankings([["a", "b", "c"], ["c", "b", "a"]])
    backward = fuse_rankings([["c", "b", "a"], ["a", "b", "c"]])

    # "a" and "c" tie on score; the tie is broken by chunk_id, not by input order.
    assert [chunk_id for chunk_id, _ in forward] == ["a", "c", "b"]
    assert forward == backward


def test_ties_are_broken_by_chunk_id() -> None:
    # "a" and "b" have identical scores (rank 1 + rank 2 in opposite lists).
    fused = fuse_rankings([["b", "a"], ["a", "b"]])

    assert [chunk_id for chunk_id, _ in fused] == ["a", "b"]
    assert fused[0][1] == pytest.approx(fused[1][1])


def test_k_is_configurable_and_affects_spread() -> None:
    default = dict(fuse_rankings([["a", "b"]]))
    small_k = dict(fuse_rankings([["a", "b"]], k=1))

    assert default["a"] == pytest.approx(1 / 61)
    assert small_k["a"] == pytest.approx(1 / 2)
    assert small_k["a"] - small_k["b"] > default["a"] - default["b"]


def test_invalid_k_and_rank_are_rejected() -> None:
    with pytest.raises(ValueError):
        fuse_rankings([["a"]], k=-1)
    with pytest.raises(ValueError):
        rrf_score(0)
    with pytest.raises(TypeError):
        rrf_score(1, k=60.0)  # type: ignore[arg-type]


def test_duplicate_id_inside_one_ranking_counts_once() -> None:
    duplicated = fuse_rankings([["a", "a", "b"]])

    assert duplicated[0] == ("a", pytest.approx(1 / 61))
    assert [chunk_id for chunk_id, _ in duplicated] == ["a", "b"]


def test_empty_ids_are_ignored() -> None:
    assert fuse_rankings([["", "a"]]) == [("a", pytest.approx(1 / 62))]
