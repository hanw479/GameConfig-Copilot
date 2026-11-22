"""Retriever tests (S1-06): synonyms, filters before ranking, RRF, degradation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from gameconfig_copilot.rag.index import (
    IndexLoadError,
    build_index,
    load_index,
)
from gameconfig_copilot.rag.retriever import (
    SYNONYM_TABLE,
    Retriever,
    build_filters,
    normalize_query,
    parse_level_band,
)
from gameconfig_copilot.rag.store import Chunk, Hit
from gameconfig_copilot.rag.stores import NoopDenseStore
from gameconfig_copilot.rag.stores.bm25 import Bm25Store

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA = REPO_ROOT / "data"


def _hit(chunk_id: str, *, score: float = 1.0, **metadata: Any) -> Hit:
    source_id = str(metadata.get("source_id") or f"task:{chunk_id}")
    payload = {
        "source_type": "task",
        "source_id": source_id,
        "position": chunk_id,
        **metadata,
    }
    return Hit(
        chunk_id=chunk_id,
        text=f"text for {chunk_id}",
        score=score,
        source_id=source_id,
        metadata=payload,
    )


def _task_chunk(chunk_id: str, text: str, *, level_min: int, level_max: int) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        text=text,
        metadata={
            "source_type": "task",
            "source_id": f"task:{chunk_id}",
            "position": "task",
            "task_id": chunk_id,
            "task_type": "side",
            "level_min": level_min,
            "level_max": level_max,
            "npc_id": "NPC_013",
        },
    )


class FakeStore:
    """A store with a fixed ranking, for deterministic fusion tests."""

    backend_name = "fake"

    def __init__(self, hits: Sequence[Hit]) -> None:
        self._hits = list(hits)

    def upsert(self, chunks: list[Chunk]) -> None:
        del chunks

    def search(self, query: str, filters: Mapping[str, Any], top_k: int) -> list[Hit]:
        del query, filters
        return list(self._hits)[:top_k]

    def health(self) -> bool:
        return True


@pytest.fixture(scope="module")
def repo_index(tmp_path_factory: pytest.TempPathFactory) -> Any:
    return build_index(DATA, tmp_path_factory.mktemp("rag-index"), sparse_backend="python")


@pytest.fixture()
def repo_retriever(repo_index: Any) -> Retriever:
    return Retriever(repo_index.sparse, repo_index.dense)


def test_synonym_table_has_documented_size_and_expands_entities() -> None:
    assert 10 <= len(SYNONYM_TABLE) <= 20

    mine = normalize_query("矿洞")
    assert mine.expansions == ("MAP_MINE_03",)
    assert "MAP_MINE_03" in mine.normalized

    smith = normalize_query("铁匠老陈 6 级支线")
    assert "NPC_013" in smith.expansions
    assert "side" in smith.expansions
    assert normalize_query("今天天气如何").expansions == ()
    assert normalize_query("   ").normalized == ""


def test_parse_level_band_accepts_cli_and_sequence_forms() -> None:
    assert parse_level_band("6-9") == (6, 9)
    assert parse_level_band("6~9") == (6, 9)
    assert parse_level_band("6") == (6, 6)
    assert parse_level_band([6, 9]) == (6, 9)
    assert parse_level_band(None) is None
    for bad in ("9-6", "x-y", [1], [0, 5]):
        with pytest.raises(ValueError):
            parse_level_band(bad)  # type: ignore[arg-type]


def test_build_filters_keeps_only_provided_keys() -> None:
    filters = build_filters(
        source_type="task",
        task_type="side",
        level_band="6-9",
        npc_id="NPC_013",
    )

    assert filters == {
        "source_type": "task",
        "task_type": "side",
        "level_band": (6, 9),
        "npc_id": "NPC_013",
    }
    assert build_filters() == {}


def test_filters_are_applied_before_scoring() -> None:
    store = Bm25Store()
    high_quality_but_out_of_band = _task_chunk(
        "T_SIDE_9999",
        "矿洞 矿洞 矿洞 矿洞 矿洞 矿洞 矿洞 矿洞",
        level_min=20,
        level_max=25,
    )
    in_band = _task_chunk("T_SIDE_0213", "矿洞", level_min=6, level_max=9)
    store.upsert([high_quality_but_out_of_band, in_band])

    unfiltered = store.search("矿洞", {}, 10)
    filtered = store.search("矿洞", {"level_band": (6, 9)}, 10)

    assert unfiltered[0].chunk_id == "T_SIDE_9999"  # would win on raw BM25
    assert [hit.chunk_id for hit in filtered] == ["T_SIDE_0213"]


def test_level_filter_excludes_sources_without_level_info(repo_retriever: Retriever) -> None:
    hits = repo_retriever.search("矿洞", top_k=15, level_band="6-9")

    assert hits
    for hit in hits:
        assert hit.metadata["source_type"] == "task"
        assert hit.metadata["level_min"] <= 9
        assert hit.metadata["level_max"] >= 6

    # No task overlaps Lv25–30, and wiki/constraint payloads carry no level.
    assert repo_retriever.search("矿洞", top_k=15, level_band="25-30") == []


def test_task_type_filter_excludes_wiki_and_other_types(repo_retriever: Retriever) -> None:
    hits = repo_retriever.search("奖励", top_k=15, task_type="daily")

    assert hits
    assert all(hit.metadata["task_type"] == "daily" for hit in hits)
    assert all(hit.source_type == "task" for hit in hits)


def test_designer_query_hits_the_demo_task(repo_retriever: Retriever) -> None:
    hits = repo_retriever.search(
        "铁匠老陈 6 级支线",
        top_k=5,
        source_type="task",
        level_band="6-9",
    )

    assert hits
    assert hits[0].source_id == "task:T_SIDE_0213"
    assert hits[0].citation.startswith("task:T_SIDE_0213#")
    assert hits[0].score > 0


def test_rrf_merges_both_branches() -> None:
    sparse = FakeStore([_hit("a", source_id="task:a"), _hit("b", source_id="task:b")])
    dense = FakeStore([_hit("b", source_id="task:b"), _hit("c", source_id="task:c")])
    retriever = Retriever(sparse, dense)

    hits = retriever.search("x", top_k=5)

    assert [hit.chunk_id for hit in hits] == ["b", "a", "c"]
    assert hits[0].score == pytest.approx(1 / 62 + 1 / 61)
    assert hits[1].score == pytest.approx(1 / 61)
    assert hits[2].score == pytest.approx(1 / 62)
    assert retriever.dense_available is True


def test_noop_dense_degrades_to_bm25(repo_index: Any, repo_retriever: Retriever) -> None:
    degraded = Retriever(repo_index.sparse, NoopDenseStore())

    assert degraded.dense_available is False
    assert [hit.chunk_id for hit in degraded.search("矿洞", top_k=5)] == [
        hit.chunk_id for hit in repo_retriever.search("矿洞", top_k=5)
    ]


def test_fused_results_are_capped_at_fifteen() -> None:
    sparse_hits = [_hit(f"c{index:02d}") for index in range(20)]
    retriever = Retriever(FakeStore(sparse_hits))

    assert len(retriever.search("x", top_k=50)) == 15
    assert len(retriever.search("x", top_k=3)) == 3


def test_empty_query_and_invalid_top_k() -> None:
    retriever = Retriever(FakeStore([_hit("a")]))

    assert retriever.search("") == []
    with pytest.raises(ValueError):
        retriever.search("x", top_k=0)


def test_index_persistence_is_idempotent(tmp_path: Path) -> None:
    first = build_index(DATA, tmp_path / "a", sparse_backend="python", rebuild=True)
    second = build_index(DATA, tmp_path / "b", sparse_backend="python", rebuild=True)

    for relative in ("manifest.json", "chunks.json", "bm25/bm25.json"):
        assert (tmp_path / "a" / relative).read_bytes() == (
            tmp_path / "b" / relative
        ).read_bytes(), relative

    assert first.chunk_count == second.chunk_count > 0
    assert [chunk.chunk_id for chunk in first.chunks] == [
        chunk.chunk_id for chunk in second.chunks
    ]

    reloaded = load_index(tmp_path / "a")
    assert [hit.chunk_id for hit in Retriever(reloaded.sparse, reloaded.dense).search("矿洞")] == [
        hit.chunk_id for hit in Retriever(first.sparse, first.dense).search("矿洞")
    ]

    manifest_before = (tmp_path / "a" / "manifest.json").read_bytes()
    reused = build_index(DATA, tmp_path / "a", sparse_backend="python")
    assert reused.reused is True
    assert reused.chunk_count == first.chunk_count
    assert (tmp_path / "a" / "manifest.json").read_bytes() == manifest_before


def test_upsert_same_chunks_twice_is_idempotent() -> None:
    store = Bm25Store()
    chunks = [_task_chunk("T_SIDE_0213", "矿洞 矿车", level_min=6, level_max=9)]

    store.upsert(chunks)
    store.upsert(chunks)

    assert len(store.chunks) == 1
    assert [hit.chunk_id for hit in store.search("矿洞", {}, 5)] == ["T_SIDE_0213"]


def test_load_missing_index_has_rebuild_hint(tmp_path: Path) -> None:
    with pytest.raises(IndexLoadError, match="gcc ingest"):
        load_index(tmp_path / "missing")


def test_auto_backend_prefers_bm25s_when_installed() -> None:
    pytest.importorskip("bm25s")
    from gameconfig_copilot.rag.stores import resolve_sparse_backend

    assert resolve_sparse_backend("auto") == "bm25s"


def test_bm25s_backend_roundtrip(tmp_path: Path) -> None:
    pytest.importorskip("bm25s")
    index_dir = tmp_path / "bm25s-index"
    built = build_index(DATA, index_dir, sparse_backend="bm25s", rebuild=True)

    assert built.sparse.backend_name == "bm25s"
    reloaded = load_index(index_dir)
    assert reloaded.sparse.backend_name == "bm25s"
    assert reloaded.chunk_count == built.chunk_count
    assert [hit.chunk_id for hit in Retriever(reloaded.sparse, reloaded.dense).search("矿洞")] == [
        hit.chunk_id for hit in Retriever(built.sparse, built.dense).search("矿洞")
    ]


def test_switching_backend_removes_stale_index_files(tmp_path: Path) -> None:
    pytest.importorskip("bm25s")
    index_dir = tmp_path / "switch-index"
    build_index(DATA, index_dir, sparse_backend="bm25s", rebuild=True)
    assert (index_dir / "bm25" / "engine").is_dir()

    build_index(DATA, index_dir, sparse_backend="python", rebuild=True)
    assert (index_dir / "bm25" / "bm25.json").is_file()
    assert not (index_dir / "bm25" / "engine").exists()

    build_index(DATA, index_dir, sparse_backend="bm25s", rebuild=True)
    assert (index_dir / "bm25" / "engine").is_dir()
    assert not (index_dir / "bm25" / "bm25.json").exists()
