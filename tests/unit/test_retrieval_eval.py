"""Unit tests for the S1-07 retrieval baseline harness (fake retriever only).

The metrics are pure functions, so every number asserted here is derived from a
canned ranking — no index, no embedding model, no network.
"""

from __future__ import annotations

import json
import re
import types
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import gameconfig_copilot.cli.eval as cli_eval
from gameconfig_copilot.cli import app
from gameconfig_copilot.evaluation import retrieval_eval as rev

runner = CliRunner()

REPO_ROOT = Path(__file__).resolve().parents[2]

CHUNK_ID_RE = re.compile(
    r"^(wiki:[a-z_]+:s\d{2}:w\d{2}"
    r"|task:T_(MAIN|SIDE|DAILY)_\d{4}(#(objectives|rewards|branches))?"
    r"|constraint:C_[A-Z0-9_]+)$"
)


class FakeRetriever:
    """Canned rankings keyed by query text; records every call."""

    def __init__(self, rankings: dict[str, list[str]] | None = None) -> None:
        self.rankings = rankings or {}
        self.calls: list[tuple[str, dict[str, Any], int]] = []

    def search(self, query: str, filters: dict[str, Any], top_k: int) -> list[dict[str, Any]]:
        self.calls.append((query, filters, top_k))
        return [
            {"chunk_id": chunk_id, "score": 1.0 / (position + 1)}
            for position, chunk_id in enumerate(self.rankings.get(query, [])[:top_k])
        ]


def _relevant(*chunk_ids: str) -> tuple[rev.RelevantChunk, ...]:
    return tuple(rev.RelevantChunk(chunk_id=chunk_id, relevance=2) for chunk_id in chunk_ids)


def _qrel(
    qid: str = "Q1",
    query: str = "矿洞分几层",
    relevant: tuple[rev.RelevantChunk, ...] | None = None,
    expected: tuple[str, ...] = ("wiki",),
) -> rev.QrelQuery:
    return rev.QrelQuery(
        qid=qid,
        query=query,
        relevant=relevant or _relevant("wiki:world_overview#三、地点与通行"),
        expected_source_types=expected,
    )


def _write_qrels(
    path: Path,
    queries: list[dict[str, Any]],
    aliases: dict[str, list[str]] | None = None,
) -> Path:
    payload = {
        "version": 1,
        "chunk_id_scheme": {"format": rev.CHUNK_ID_SCHEME, "status": "pending_alignment"},
        "aliases": aliases or {},
        "queries": queries,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _simple_query(qid: str, query: str, *chunk_ids: str) -> dict[str, Any]:
    return {
        "qid": qid,
        "query": query,
        "expected_source_types": ["wiki"],
        "relevant": [{"chunk_id": chunk_id, "relevance": 2} for chunk_id in chunk_ids],
    }


def _raise_import_error() -> Any:
    raise ModuleNotFoundError("No module named 'gameconfig_copilot.rag.retriever'")


def _output(result: object) -> str:
    combined = getattr(result, "output", "") or ""
    combined += getattr(result, "stderr", "") or ""
    return combined


# ---------------------------------------------------------------------------
# qrels: the shipped artifact
# ---------------------------------------------------------------------------


def test_shipped_qrels_loads_twenty_queries() -> None:
    qrels = rev.load_qrels()

    assert len(qrels.queries) == 20
    assert len({qrel.qid for qrel in qrels.queries}) == 20
    assert all(qrel.relevant for qrel in qrels.queries)
    # Aligned to R's chunker as of 2026-11-15; the corpus test below re-verifies.
    assert qrels.scheme_status == "aligned_with_rag_chunker"


def test_shipped_qrels_uses_declared_chunk_id_scheme() -> None:
    qrels = rev.load_qrels()

    for qrel in qrels.queries:
        for item in qrel.relevant:
            assert CHUNK_ID_RE.match(item.chunk_id), item.chunk_id
            assert item.chunk_id.split(":", 1)[0] in qrel.expected_source_types


def test_qrels_chunk_ids_exist_in_real_corpus() -> None:
    """Alignment guard: every labelled id must be something R's chunker emits."""

    corpus = pytest.importorskip("gameconfig_copilot.rag.corpus")
    chunk_ids = {chunk.chunk_id for chunk in corpus.load_chunks(REPO_ROOT / "data")}
    qrels = rev.load_qrels()

    missing = [
        (qrel.qid, item.chunk_id)
        for qrel in qrels.queries
        for item in qrel.relevant
        if item.chunk_id not in chunk_ids
    ]

    assert missing == [], f"qrels 与 rag/chunker.py 输出不一致：{missing}"


def test_shipped_qrels_covers_all_three_corpora_and_difficulties() -> None:
    summary = rev.summarize_qrels(rev.load_qrels())

    assert summary["count"] == 20
    assert set(summary["by_source_type"]) == {"wiki", "task", "constraint"}
    assert all(count > 0 for count in summary["by_source_type"].values())
    assert sum(summary["by_difficulty"].values()) == 20
    assert summary["multi_label_queries"] > 0
    assert summary["relevant_labels"] == sum(
        len(qrel.relevant) for qrel in rev.load_qrels().queries
    )


def test_shipped_qrels_filters_are_neutral_dicts() -> None:
    qrels = rev.load_qrels()

    for qrel in qrels.queries:
        assert isinstance(qrel.filters, dict)
        assert set(qrel.filters) <= {"source_type", "task_type", "level_band"}


# ---------------------------------------------------------------------------
# qrels: failure modes
# ---------------------------------------------------------------------------


def test_load_qrels_missing_file_names_the_path(tmp_path: Path) -> None:
    missing = tmp_path / "nope.json"

    with pytest.raises(rev.RetrievalEvalError, match="不存在"):
        rev.load_qrels(missing)


def test_load_qrels_rejects_malformed_json(tmp_path: Path) -> None:
    broken = tmp_path / "qrels.json"
    broken.write_text('{"queries": [}', encoding="utf-8")

    with pytest.raises(rev.RetrievalEvalError, match="不是合法 JSON"):
        rev.load_qrels(broken)


def test_load_qrels_rejects_duplicate_qids(tmp_path: Path) -> None:
    path = _write_qrels(
        tmp_path / "qrels.json",
        [_simple_query("Q1", "a", "wiki:a#x"), _simple_query("Q1", "b", "wiki:b#y")],
    )

    with pytest.raises(rev.RetrievalEvalError, match="qid 重复"):
        rev.load_qrels(path)


def test_load_qrels_rejects_empty_relevant(tmp_path: Path) -> None:
    path = _write_qrels(
        tmp_path / "qrels.json",
        [{"qid": "Q1", "query": "a", "relevant": []}],
    )

    with pytest.raises(rev.RetrievalEvalError, match="relevant 必须是非空数组"):
        rev.load_qrels(path)


def test_load_qrels_rejects_non_positive_relevance(tmp_path: Path) -> None:
    path = _write_qrels(
        tmp_path / "qrels.json",
        [{"qid": "Q1", "query": "a", "relevant": [{"chunk_id": "wiki:a#x", "relevance": 0}]}],
    )

    with pytest.raises(rev.RetrievalEvalError, match="relevance 必须是正整数"):
        rev.load_qrels(path)


def test_load_qrels_accepts_plain_string_relevant_entries(tmp_path: Path) -> None:
    path = _write_qrels(
        tmp_path / "qrels.json",
        [{"qid": "Q1", "query": "a", "relevant": ["wiki:a#x"]}],
    )

    qrels = rev.load_qrels(path)

    assert qrels.queries[0].relevant == (rev.RelevantChunk(chunk_id="wiki:a#x", relevance=2),)


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------


def test_perfect_ranking_scores_one() -> None:
    qrel = _qrel()
    hits = [{"chunk_id": "wiki:world_overview#三、地点与通行"}]

    result = rev.evaluate_query(qrel, hits)

    assert result.recall == {1: 1.0, 5: 1.0, 10: 1.0}
    assert result.mrr == 1.0
    assert result.ndcg == 1.0
    assert result.hits == {1: True, 5: True, 10: True}
    assert result.source_type_precision == 1.0
    assert result.first_relevant_rank == 1


def test_miss_scores_zero() -> None:
    qrel = _qrel()

    result = rev.evaluate_query(qrel, [{"chunk_id": "wiki:world_overview#一、世界格局"}])

    assert result.recall == {1: 0.0, 5: 0.0, 10: 0.0}
    assert result.mrr == 0.0
    assert result.ndcg == 0.0
    assert result.hits == {1: False, 5: False, 10: False}
    assert result.first_relevant_rank is None


def test_partial_recall_across_multiple_labels() -> None:
    qrel = _qrel(relevant=_relevant("wiki:a#x", "wiki:b#y"))
    hits = [{"chunk_id": "wiki:other"}, {"chunk_id": "wiki:other2"}, {"chunk_id": "wiki:a#x"}]

    result = rev.evaluate_query(qrel, hits)

    assert result.recall == {1: 0.0, 5: 0.5, 10: 0.5}
    assert result.hits == {1: False, 5: True, 10: True}
    assert result.mrr == pytest.approx(1 / 3)
    assert result.first_relevant_rank == 3


def test_duplicate_hits_do_not_inflate_recall() -> None:
    qrel = _qrel(relevant=_relevant("wiki:a#x", "wiki:b#y"))
    hits = [
        {"chunk_id": "wiki:a#x"},
        {"chunk_id": "wiki:a#x"},
        {"chunk_id": "wiki:c#z"},
    ]

    result = rev.evaluate_query(qrel, hits)

    assert result.retrieved == ("wiki:a#x", "wiki:c#z")
    assert result.recall[5] == 0.5


def test_mrr_cutoff_ignores_hits_beyond_the_window() -> None:
    ranked = [f"wiki:miss{i}" for i in range(10)] + ["wiki:a#x"]

    assert rev.mrr_at_k(ranked, {"wiki:a#x": 2}, k=10) == 0.0
    assert rev.mrr_at_k(ranked, {"wiki:a#x": 2}, k=11) == pytest.approx(1 / 11)


def test_ndcg_matches_manual_graded_gain() -> None:
    relevant = {"wiki:a#x": 2, "wiki:b#y": 1}
    ranked = ["wiki:b#y", "wiki:a#x"]

    dcg = 1.0 / 1.0 + 3.0 / 1.584962500721156
    idcg = 3.0 / 1.0 + 1.0 / 1.584962500721156

    assert rev.ndcg_at_k(ranked, relevant, k=10) == pytest.approx(dcg / idcg)


def test_recall_rejects_non_positive_k() -> None:
    with pytest.raises(ValueError):
        rev.recall_at_k(["wiki:a#x"], {"wiki:a#x": 1}, k=0)


def test_chunk_id_of_accepts_mapping_object_and_string() -> None:
    class Hit:
        chunk_id = "task:T_SIDE_0213#objectives"

    assert rev.chunk_id_of({"chunk_id": "wiki:a#x"}) == "wiki:a#x"
    assert rev.chunk_id_of(Hit()) == "task:T_SIDE_0213#objectives"
    assert rev.chunk_id_of("  constraint:C_REWARD_GOLD_01  ") == "constraint:C_REWARD_GOLD_01"


def test_hit_without_chunk_id_is_rejected() -> None:
    with pytest.raises(rev.RetrievalEvalError, match="缺少可用的 chunk_id"):
        rev.evaluate_query(_qrel(), [{"score": 0.9}])


def test_alias_mapping_counts_retrieved_alias_as_relevant() -> None:
    matcher = rev.ChunkMatcher({"wiki:world_overview#三、地点与通行": ["c_102"]})
    qrel = _qrel()

    result = rev.evaluate_query(qrel, [{"chunk_id": "c_102"}], matcher=matcher)

    assert result.recall[5] == 1.0
    assert result.retrieved == ("wiki:world_overview#三、地点与通行",)


def test_source_type_precision_is_scoped_by_expected_types() -> None:
    qrel = _qrel(expected=("task",))
    hits = [{"chunk_id": "wiki:a#x"}, {"chunk_id": "task:T_MAIN_0101#objectives"}]

    result = rev.evaluate_query(qrel, hits)

    assert result.source_type_precision == 0.5


def test_source_type_uses_hit_metadata_when_present() -> None:
    qrel = _qrel(expected=("task",))
    hits = [{"chunk_id": "opaque-1", "metadata": {"source_type": "task"}}]

    result = rev.evaluate_query(qrel, hits)

    assert result.source_type_precision == 1.0


def test_aggregate_metrics_macro_average() -> None:
    hit = rev.evaluate_query(_qrel("Q1"), [{"chunk_id": "wiki:world_overview#三、地点与通行"}])
    miss = rev.evaluate_query(_qrel("Q2"), [])

    metrics = rev.aggregate_metrics([hit, miss])

    assert metrics["recall@1"] == 0.5
    assert metrics["recall@5"] == 0.5
    assert metrics["mrr@10"] == 0.5
    assert metrics["ndcg@10"] == 0.5
    assert metrics["hit@1"] == 0.5
    assert metrics["source_type_precision@10"] == 0.5


def test_aggregate_rejects_empty_results() -> None:
    with pytest.raises(rev.RetrievalEvalError, match="没有可聚合"):
        rev.aggregate_metrics([])


# ---------------------------------------------------------------------------
# retriever seam
# ---------------------------------------------------------------------------


def test_load_retriever_uses_module_entrypoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeRetriever({})
    module = types.SimpleNamespace(get_retriever=lambda: fake)
    monkeypatch.setattr(rev, "_import_retriever_module", lambda: module)

    retriever, reason = rev.load_retriever()

    assert reason == ""
    assert retriever is fake


def test_load_retriever_reports_missing_entrypoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rev, "_import_retriever_module", lambda: types.SimpleNamespace())

    retriever, reason = rev.load_retriever()

    assert retriever is None
    assert "未暴露" in reason


def test_load_retriever_builds_from_persisted_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R exposes ``Retriever(sparse, dense)`` rather than a no-arg factory."""

    class FakeRetrieverClass:
        def __init__(self, sparse: Any, dense: Any) -> None:
            self.sparse = sparse
            self.dense = dense

        def search(self, query: str, filters: dict[str, Any], top_k: int) -> list[Any]:
            return []

    sparse = types.SimpleNamespace(backend_name="python-bm25")
    dense = types.SimpleNamespace(backend_name="noop")
    index = types.SimpleNamespace(sparse=sparse, dense=dense)
    module = types.SimpleNamespace(Retriever=FakeRetrieverClass)
    monkeypatch.setattr(rev, "_import_retriever_module", lambda: module)
    monkeypatch.setattr(rev, "_open_rag_index", lambda index_dir: (index, False))

    retriever, reason = rev.load_retriever()

    assert reason == ""
    assert isinstance(retriever, FakeRetrieverClass)
    assert retriever.sparse is sparse
    assert retriever.dense is dense


def test_load_retriever_auto_builds_a_missing_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeRetrieverClass:
        def __init__(self, sparse: Any, dense: Any) -> None:
            self.sparse = sparse
            self.dense = dense

        def search(self, query: str, filters: dict[str, Any], top_k: int) -> list[Any]:
            return []

    index = types.SimpleNamespace(sparse=object(), dense=None)
    monkeypatch.setattr(rev, "_import_retriever_module", lambda: types.SimpleNamespace(
        Retriever=FakeRetrieverClass
    ))
    monkeypatch.setattr(rev, "_open_rag_index", lambda index_dir: (index, True))

    retriever, reason = rev.load_retriever()

    assert reason == ""
    assert isinstance(retriever, FakeRetrieverClass)


def test_load_retriever_reports_missing_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _no_index(_index_dir: Any) -> Any:
        raise RuntimeError("索引不存在或未完成构建：out/index/manifest.json")

    monkeypatch.setattr(rev, "_open_rag_index", _no_index)
    module = types.SimpleNamespace(Retriever=lambda sparse, dense: None)
    monkeypatch.setattr(rev, "_import_retriever_module", lambda: module)

    retriever, reason = rev.load_retriever()

    assert retriever is None
    assert "索引不可用" in reason
    assert "gcc ingest" in reason


def test_load_retriever_reports_import_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rev, "_import_retriever_module", _raise_import_error)

    retriever, reason = rev.load_retriever()

    assert retriever is None
    assert "待 R 就绪" in reason


# ---------------------------------------------------------------------------
# end-to-end (fake retriever, real files in tmp_path)
# ---------------------------------------------------------------------------


def test_per_query_record_keeps_neutral_filters() -> None:
    qrel = rev.QrelQuery(
        qid="Q1",
        query="a",
        relevant=_relevant("wiki:a#x"),
        expected_source_types=("wiki",),
        filters={"source_type": ["wiki"], "level_band": [6, 9]},
    )

    result = rev.evaluate_query(qrel, [{"chunk_id": "wiki:a#x"}])

    assert result.as_dict()["filters"] == {"source_type": ["wiki"], "level_band": [6, 9]}


def test_run_writes_report_and_metrics(tmp_path: Path) -> None:
    filtered = _simple_query("Q2", "深井教团", "wiki:world_overview#深井教团")
    filtered["filters"] = {"source_type": ["wiki"]}
    qrels = _write_qrels(
        tmp_path / "qrels.json",
        [
            _simple_query("Q1", "矿洞分几层", "wiki:world_overview#三、地点与通行"),
            filtered,
        ],
    )
    fake = FakeRetriever(
        {
            "矿洞分几层": ["wiki:world_overview#三、地点与通行"],
            "深井教团": ["wiki:world_overview#一、世界格局", "wiki:world_overview#深井教团"],
        }
    )
    report_path = tmp_path / "out" / "retrieval-baseline.json"

    result = rev.run_retrieval_eval(
        qrels_path=qrels,
        report_path=report_path,
        retriever=lambda: fake,
        generated_at="2026-11-15T12:00:00+08:00",
    )

    assert result.status == "ok"
    assert report_path.is_file()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["suite"] == "retrieval"
    assert report["status"] == "ok"
    assert report["generated_at"] == "2026-11-15T12:00:00+08:00"
    assert report["metrics"]["recall@5"] == 1.0
    assert report["metrics"]["mrr@10"] == 0.75
    assert report["qrels"]["distribution"]["count"] == 2
    assert report["config"]["top_k"] == 10
    assert report["retriever"]["top_k"] == 10
    assert report["retriever"]["dense_available"] is False
    assert any("BM25" in note for note in report["notes"])
    assert len(report["per_query"]) == 2
    assert report["per_query"][0]["qid"] == "Q1"
    assert report["per_query"][1]["filters"] == {"source_type": ["wiki"]}
    assert fake.calls[0] == ("矿洞分几层", {}, 10)
    assert fake.calls[1] == ("深井教团", {"source_type": ["wiki"]}, 10)


def test_run_without_retriever_writes_no_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    qrels = _write_qrels(tmp_path / "qrels.json", [_simple_query("Q1", "a", "wiki:a#x")])
    report_path = tmp_path / "retrieval-baseline.json"
    monkeypatch.setattr(rev, "_import_retriever_module", _raise_import_error)

    result = rev.run_retrieval_eval(qrels_path=qrels, report_path=report_path)

    assert result.status == "retriever_unavailable"
    assert "待 R 就绪" in result.message
    assert result.report is None
    assert not report_path.exists()


def test_run_rejects_top_k_smaller_than_max_k(tmp_path: Path) -> None:
    qrels = _write_qrels(tmp_path / "qrels.json", [_simple_query("Q1", "a", "wiki:a#x")])

    with pytest.raises(rev.RetrievalEvalError, match="top_k"):
        rev.run_retrieval_eval(qrels_path=qrels, retriever=lambda: FakeRetriever(), top_k=5)


def test_run_names_the_failing_qid(tmp_path: Path) -> None:
    qrels = _write_qrels(tmp_path / "qrels.json", [_simple_query("Q7", "a", "wiki:a#x")])

    class ExplodingRetriever:
        def search(self, query: str, filters: dict[str, Any], top_k: int) -> list[Any]:
            raise RuntimeError("vector store offline")

    with pytest.raises(rev.RetrievalEvalError, match="Q7"):
        rev.run_retrieval_eval(qrels_path=qrels, retriever=lambda: ExplodingRetriever())


def test_report_is_utf8_readable(tmp_path: Path) -> None:
    report = {"suite": "retrieval", "status": "ok", "note": "中文可直接阅读"}
    path = rev.write_report(report, tmp_path / "nested" / "report.json")

    assert "中文可直接阅读" in path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI shell
# ---------------------------------------------------------------------------


def test_cli_eval_retrieval_success_exit_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    report = {
        "suite": "retrieval",
        "status": "ok",
        "metrics": {"recall@1": 0.5, "recall@5": 0.75, "mrr@10": 0.625},
        "qrels": {"distribution": {"count": 20, "relevant_labels": 34}},
    }
    fake_result = rev.RunResult(
        status="ok",
        message="ok",
        report_path=Path("reports/retrieval-baseline.json"),
        report=report,
    )
    monkeypatch.setattr(cli_eval, "run_retrieval_eval", lambda: fake_result)

    result = runner.invoke(app, ["eval", "--suite", "retrieval"])

    assert result.exit_code == 0, _output(result)
    output = _output(result)
    assert "recall@5" in output
    assert "0.7500" in output


def test_cli_eval_retrieval_without_retriever_exits_three(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_result = rev.RunResult(
        status="retriever_unavailable",
        message="待 R 就绪：rag/retriever.py 尚不可导入",
        report_path=Path("reports/retrieval-baseline.json"),
    )
    monkeypatch.setattr(cli_eval, "run_retrieval_eval", lambda: fake_result)

    result = runner.invoke(app, ["eval", "--suite", "retrieval"])

    assert result.exit_code == 3
    output = _output(result)
    assert "待 R 就绪" in output
    assert "不写" in output


def test_cli_eval_surfaces_qrels_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> rev.RunResult:
        raise rev.RetrievalEvalError("qrels 文件不存在：x")

    monkeypatch.setattr(cli_eval, "run_retrieval_eval", _boom)

    result = runner.invoke(app, ["eval", "--suite", "retrieval"])

    assert result.exit_code == 1
    assert "qrels 文件不存在" in _output(result)


def test_cli_eval_rejects_unknown_suite() -> None:
    result = runner.invoke(app, ["eval", "--suite", "nope"])

    assert result.exit_code == 2
    assert "未知评测套件" in _output(result)


# ``--suite agent`` / ``--suite all`` now drive real generation and are covered
# in ``test_cli_eval_agent.py`` with a mock generator (no network, no API key).
