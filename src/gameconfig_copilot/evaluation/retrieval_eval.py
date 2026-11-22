"""Retrieval baseline harness (S1-07): qrels -> retriever -> Recall/MRR/nDCG report.

Contract
--------
Input
    ``data/golden/qrels.json`` (owned by E) and R's retriever
    (``gameconfig_copilot.rag.retriever``, docs/06 §5.4), built from the index
    that ``gcc ingest`` writes to ``out/index``. A missing index is built once
    with R's ``build_index`` so the suite runs on a fresh checkout.
Output
    ``reports/retrieval-baseline.json`` — per-query hits plus aggregated
    ``recall@1/5/10``, ``hit@1/5/10``, ``mrr@10``, ``ndcg@10`` and
    ``source_type_precision@10``.
Failure semantics
    * qrels missing/half-baked -> :class:`RetrievalEvalError` naming the field.
    * retriever or index unavailable -> :class:`RunResult` with
      ``status="retriever_unavailable"`` and the reason; no report is written,
      because a baseline without numbers would be a lie.
    * a retriever raising mid-run -> :class:`RetrievalEvalError` naming the qid.

Metric functions are pure and unit-tested against a fake retriever; only
:func:`load_retriever` (index wiring) and the qrels chunk-id alignment depend on
R's layer, which keeps the baseline reproducible without an index in tests.
"""

from __future__ import annotations

import importlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "CHUNK_ID_SCHEME",
    "DEFAULT_KS",
    "DEFAULT_TOP_K",
    "INDEX_DIR",
    "MRR_CUTOFF",
    "QRELS_PATH",
    "REPORT_PATH",
    "ChunkMatcher",
    "QrelQuery",
    "Qrels",
    "QueryResult",
    "RelevantChunk",
    "RetrievalEvalError",
    "Retriever",
    "RunResult",
    "aggregate_metrics",
    "chunk_id_of",
    "evaluate_query",
    "load_qrels",
    "load_retriever",
    "mrr_at_k",
    "ndcg_at_k",
    "recall_at_k",
    "run_retrieval_eval",
    "summarize_qrels",
    "write_report",
]

DEFAULT_TOP_K = 10
DEFAULT_KS: tuple[int, ...] = (1, 5, 10)
MRR_CUTOFF = 10
CHUNK_ID_SCHEME = "source_type:source_id#section"

REPO_ROOT = Path(__file__).resolve().parents[3]
QRELS_PATH = REPO_ROOT / "data" / "golden" / "qrels.json"
REPORT_PATH = REPO_ROOT / "reports" / "retrieval-baseline.json"
#: R's persisted index (``gcc ingest``); gitignored runtime artifact under out/.
INDEX_DIR = REPO_ROOT / "out" / "index"

_RETRIEVER_MODULE = "gameconfig_copilot.rag.retriever"
_RETRIEVER_ENTRIES = (
    "get_retriever",
    "build_retriever",
    "default_retriever",
    "make_retriever",
)

_METRIC_NOTES = (
    "Recall@k = |相关块 ∩ top-k| / |相关块|，按 query 取平均；"
    "hit@k 为「top-k 内至少命中一个相关块」的比例。",
    "MRR@10 = 首个相关块排名倒数（前 10 内未命中记 0），按 query 取平均。",
    "nDCG@10 使用分级增益 2^rel-1，ideal 排序取该 query 的全部相关块。",
    "20 条 query 全部为可回答问题；知识库外拒答归 Agent 级评测（S7-01/S7-03），不进检索指标分母。",
)


class RetrievalEvalError(RuntimeError):
    """Raised when qrels are malformed or the retriever misbehaves."""


# --------------------------------------------------------------------------
# Hits
# --------------------------------------------------------------------------


@runtime_checkable
class Retriever(Protocol):
    """The slice of R's contract this harness consumes.

    docs/02 §5.2 / docs/06 §5.4 define ``search(query, filters, top_k) ->
    list[Hit]``; the harness calls it positionally so R keeps parameter naming
    freedom. A ``Hit`` may be a mapping or an object exposing ``chunk_id``.
    """

    def search(self, query: str, filters: dict[str, Any], top_k: int) -> Sequence[Any]:
        """Return up to ``top_k`` hits, best first."""
        ...


def chunk_id_of(hit: Any) -> str:
    """Return the chunk id of a hit (mapping, object, or plain string)."""

    if isinstance(hit, str):
        value: Any = hit
    elif isinstance(hit, Mapping):
        value = hit.get("chunk_id")
    else:
        value = getattr(hit, "chunk_id", None)
    if not isinstance(value, str) or not value.strip():
        raise RetrievalEvalError(f"检索结果缺少可用的 chunk_id：{hit!r}")
    return value.strip()


def _source_type_of(hit: Any, chunk_id: str) -> str | None:
    """Best-effort source type: declared metadata first, id prefix second."""

    if isinstance(hit, Mapping):
        declared = hit.get("source_type")
        metadata = hit.get("metadata")
    else:
        declared = getattr(hit, "source_type", None)
        metadata = getattr(hit, "metadata", None)
    if isinstance(metadata, Mapping):
        declared = declared or metadata.get("source_type")
    if isinstance(declared, str) and declared.strip():
        return declared.strip()
    prefix, sep, _ = chunk_id.partition(":")
    return prefix if sep else None


# --------------------------------------------------------------------------
# qrels
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RelevantChunk:
    chunk_id: str
    relevance: int = 2


@dataclass(frozen=True)
class QrelQuery:
    qid: str
    query: str
    relevant: tuple[RelevantChunk, ...]
    expected_source_types: tuple[str, ...] = ()
    filters: dict[str, Any] = field(default_factory=dict)
    difficulty: str = "unspecified"
    intent: str = "unspecified"
    rationale: str = ""


@dataclass(frozen=True)
class Qrels:
    source_path: Path
    version: int
    chunk_id_scheme: str
    scheme_status: str
    queries: tuple[QrelQuery, ...]
    aliases: dict[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class QueryResult:
    qid: str
    query: str
    difficulty: str
    intent: str
    retrieved: tuple[str, ...]
    relevant: tuple[str, ...]
    first_relevant_rank: int | None
    recall: dict[int, float]
    hits: dict[int, bool]
    mrr: float
    ndcg: float
    source_type_precision: float
    filters: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "qid": self.qid,
            "query": self.query,
            "difficulty": self.difficulty,
            "intent": self.intent,
            "filters": dict(self.filters),
            "first_relevant_rank": self.first_relevant_rank,
            "retrieved": list(self.retrieved),
            "relevant": list(self.relevant),
            "recall": {f"recall@{k}": _round(value) for k, value in self.recall.items()},
            "hit": {f"hit@{k}": self.hits[k] for k in self.hits},
            "mrr": _round(self.mrr),
            "ndcg": _round(self.ndcg),
            "source_type_precision": _round(self.source_type_precision),
        }


@dataclass(frozen=True)
class RunResult:
    """Outcome of one harness run; ``report`` stays ``None`` when skipped."""

    status: str
    message: str
    report_path: Path
    report: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _round(value: float) -> float:
    return round(float(value), 4)


def _display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(path)


def _loads_json(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise RetrievalEvalError(f"qrels 文件不存在：{path}") from exc
    except OSError as exc:
        raise RetrievalEvalError(f"qrels 文件无法读取：{path}（{exc}）") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RetrievalEvalError(
            f"qrels 不是合法 JSON：{path} 第 {exc.lineno} 行第 {exc.colno} 列（{exc.msg}）"
        ) from exc


def _expect_mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RetrievalEvalError(f"{where} 必须是 JSON 对象")
    return value


def _expect_str(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RetrievalEvalError(f"{where} 必须是非空字符串")
    return value.strip()


def _expect_label(mapping: Mapping[str, Any], key: str, default: str) -> str:
    value = mapping.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else default


def _parse_relevant(entry: Any, where: str) -> RelevantChunk:
    if isinstance(entry, str):
        return RelevantChunk(chunk_id=_expect_str(entry, where))
    mapping = _expect_mapping(entry, where)
    relevance = mapping.get("relevance", 2)
    if isinstance(relevance, bool) or not isinstance(relevance, int) or relevance <= 0:
        raise RetrievalEvalError(f"{where}.relevance 必须是正整数")
    return RelevantChunk(
        chunk_id=_expect_str(mapping.get("chunk_id"), f"{where}.chunk_id"),
        relevance=relevance,
    )


def _parse_query(raw: Any, index: int) -> QrelQuery:
    where = f"queries[{index}]"
    mapping = _expect_mapping(raw, where)
    qid = _expect_str(mapping.get("qid"), f"{where}.qid")
    query = _expect_str(mapping.get("query"), f"{qid}.query")

    relevant_raw = mapping.get("relevant")
    if not isinstance(relevant_raw, list) or not relevant_raw:
        raise RetrievalEvalError(f"{qid}.relevant 必须是非空数组")
    relevant = tuple(
        _parse_relevant(entry, f"{qid}.relevant[{position}]")
        for position, entry in enumerate(relevant_raw)
    )

    expected_raw = mapping.get("expected_source_types", [])
    if not isinstance(expected_raw, list):
        raise RetrievalEvalError(f"{qid}.expected_source_types 必须是数组")
    expected = tuple(
        _expect_str(value, f"{qid}.expected_source_types[{position}]")
        for position, value in enumerate(expected_raw)
    )

    return QrelQuery(
        qid=qid,
        query=query,
        relevant=relevant,
        expected_source_types=expected,
        filters=dict(_expect_mapping(mapping.get("filters", {}), f"{qid}.filters")),
        difficulty=_expect_label(mapping, "difficulty", "unspecified"),
        intent=_expect_label(mapping, "intent", "unspecified"),
        rationale=_expect_label(mapping, "rationale", ""),
    )


def _parse_aliases(raw: Any) -> dict[str, tuple[str, ...]]:
    mapping = _expect_mapping(raw, "aliases")
    aliases: dict[str, tuple[str, ...]] = {}
    for canonical, values in mapping.items():
        if not isinstance(values, list):
            raise RetrievalEvalError(f"aliases[{canonical!r}] 必须是数组")
        aliases[str(canonical)] = tuple(
            _expect_str(value, f"aliases[{canonical!r}][{position}]")
            for position, value in enumerate(values)
        )
    return aliases


def load_qrels(path: str | Path | None = None) -> Qrels:
    """Load and validate the retrieval qrels (defaults to ``data/golden``)."""

    resolved = Path(path) if path is not None else QRELS_PATH
    raw = _loads_json(resolved)
    mapping = _expect_mapping(raw, str(resolved))

    version = mapping.get("version", 1)
    if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
        raise RetrievalEvalError(f"{_display_path(resolved)}.version 必须是正整数")

    scheme_raw = mapping.get("chunk_id_scheme")
    if isinstance(scheme_raw, Mapping):
        scheme = _expect_label(scheme_raw, "format", CHUNK_ID_SCHEME)
        scheme_status = _expect_label(scheme_raw, "status", "unknown")
    else:
        scheme, scheme_status = CHUNK_ID_SCHEME, "unknown"

    queries_raw = mapping.get("queries")
    if not isinstance(queries_raw, list) or not queries_raw:
        raise RetrievalEvalError(f"{_display_path(resolved)} 缺少非空的 queries 数组")
    queries = tuple(_parse_query(entry, index) for index, entry in enumerate(queries_raw))

    seen: dict[str, int] = {}
    for position, qrel in enumerate(queries):
        if qrel.qid in seen:
            raise RetrievalEvalError(
                f"queries 中 qid 重复：{qrel.qid}（第 {seen[qrel.qid]} 与第 {position} 条）"
            )
        seen[qrel.qid] = position

    return Qrels(
        source_path=resolved,
        version=version,
        chunk_id_scheme=scheme,
        scheme_status=scheme_status,
        queries=queries,
        aliases=_parse_aliases(mapping.get("aliases", {})),
    )


class ChunkMatcher:
    """Maps declared aliases onto their canonical chunk id (single hop).

    Used only for the qrels-side alignment seam: when R's chunker lands with a
    different ``chunk_id`` spelling, register the mapping under the qrels top
    level ``aliases`` key instead of loosening the metric comparison.
    """

    def __init__(self, aliases: Mapping[str, Sequence[str]]) -> None:
        self._canonical: dict[str, str] = {}
        for canonical, values in aliases.items():
            self._canonical.setdefault(canonical, canonical)
            for value in values:
                self._canonical.setdefault(value, canonical)

    def canonical(self, chunk_id: str) -> str:
        return self._canonical.get(chunk_id, chunk_id)


# --------------------------------------------------------------------------
# Metrics (pure functions)
# --------------------------------------------------------------------------


def recall_at_k(ranked: Sequence[str], relevant: Mapping[str, int], k: int) -> float:
    """|relevant ∩ top-k| / |relevant|; ``ranked`` must already be deduped."""

    if k <= 0:
        raise ValueError("k 必须为正整数")
    if not relevant:
        return 0.0
    found = sum(1 for chunk_id in ranked[:k] if chunk_id in relevant)
    return found / len(relevant)


def mrr_at_k(ranked: Sequence[str], relevant: Mapping[str, int], k: int = MRR_CUTOFF) -> float:
    """Reciprocal rank of the first relevant chunk within ``k``, else 0."""

    if k <= 0:
        raise ValueError("k 必须为正整数")
    for rank, chunk_id in enumerate(ranked[:k], start=1):
        if chunk_id in relevant:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(ranked: Sequence[str], relevant: Mapping[str, int], k: int = MRR_CUTOFF) -> float:
    """Graded nDCG with gain ``2**relevance - 1`` and ideal ordering at ``k``."""

    if k <= 0:
        raise ValueError("k 必须为正整数")
    if not relevant:
        return 0.0
    dcg = 0.0
    for rank, chunk_id in enumerate(ranked[:k], start=1):
        gain = relevant.get(chunk_id, 0)
        if gain:
            dcg += (2**gain - 1) / math.log2(rank + 1)
    ideal = sorted(relevant.values(), reverse=True)[:k]
    idcg = sum(
        (2**gain - 1) / math.log2(rank + 1) for rank, gain in enumerate(ideal, start=1)
    )
    return dcg / idcg if idcg else 0.0


def evaluate_query(
    qrel: QrelQuery,
    hits: Sequence[Any],
    *,
    matcher: ChunkMatcher | None = None,
    ks: Sequence[int] = DEFAULT_KS,
    mrr_cutoff: int = MRR_CUTOFF,
) -> QueryResult:
    """Score one query against its relevance labels.

    Duplicate hits (RRF can emit the same chunk twice) are collapsed on first
    occurrence so a duplicate never inflates recall.
    """

    chunk_matcher = matcher or ChunkMatcher({})
    ranked: list[str] = []
    source_types: list[str | None] = []
    seen: set[str] = set()
    for hit in hits:
        raw_id = chunk_id_of(hit)
        canonical = chunk_matcher.canonical(raw_id)
        if canonical in seen:
            continue
        seen.add(canonical)
        ranked.append(canonical)
        source_types.append(_source_type_of(hit, raw_id))

    # Score only the reporting window so "×@10" names stay honest even if R's
    # retriever one day returns more hits than asked for.
    limit = max(max(ks), mrr_cutoff)
    ranked = ranked[:limit]
    source_types = source_types[:limit]

    relevant = {
        chunk_matcher.canonical(item.chunk_id): item.relevance for item in qrel.relevant
    }
    expected_source_types = set(qrel.expected_source_types) or {
        source_type
        for source_type in (
            chunk_matcher.canonical(item.chunk_id).partition(":")[0] for item in qrel.relevant
        )
        if source_type
    }

    first_rank: int | None = None
    for rank, chunk_id in enumerate(ranked, start=1):
        if chunk_id in relevant:
            first_rank = rank
            break

    recall = {k: recall_at_k(ranked, relevant, k) for k in ks}
    hits_by_k = {k: any(chunk_id in relevant for chunk_id in ranked[:k]) for k in ks}
    matched = sum(
        1
        for source_type in source_types
        if source_type in expected_source_types
    )
    precision = matched / len(ranked) if ranked else 0.0

    return QueryResult(
        qid=qrel.qid,
        query=qrel.query,
        difficulty=qrel.difficulty,
        intent=qrel.intent,
        retrieved=tuple(ranked),
        relevant=tuple(relevant),
        first_relevant_rank=first_rank,
        recall=recall,
        hits=hits_by_k,
        mrr=mrr_at_k(ranked, relevant, mrr_cutoff),
        ndcg=ndcg_at_k(ranked, relevant, mrr_cutoff),
        source_type_precision=precision,
        filters=dict(qrel.filters),
    )


def aggregate_metrics(
    results: Sequence[QueryResult],
    *,
    ks: Sequence[int] = DEFAULT_KS,
    mrr_cutoff: int = MRR_CUTOFF,
) -> dict[str, float]:
    """Macro-average the per-query metrics into the report's headline numbers."""

    if not results:
        raise RetrievalEvalError("没有可聚合的 query 结果")
    total = len(results)
    metrics: dict[str, float] = {}
    for k in ks:
        metrics[f"recall@{k}"] = _round(sum(result.recall[k] for result in results) / total)
        metrics[f"hit@{k}"] = _round(sum(1 for result in results if result.hits[k]) / total)
    metrics[f"mrr@{mrr_cutoff}"] = _round(sum(result.mrr for result in results) / total)
    metrics[f"ndcg@{mrr_cutoff}"] = _round(sum(result.ndcg for result in results) / total)
    metrics[f"source_type_precision@{mrr_cutoff}"] = _round(
        sum(result.source_type_precision for result in results) / total
    )
    return metrics


def summarize_qrels(qrels: Qrels) -> dict[str, Any]:
    """Counts that make the qrels distribution auditable from the report."""

    by_intent: dict[str, int] = {}
    by_difficulty: dict[str, int] = {}
    by_source_type: dict[str, int] = {}
    for qrel in qrels.queries:
        by_intent[qrel.intent] = by_intent.get(qrel.intent, 0) + 1
        by_difficulty[qrel.difficulty] = by_difficulty.get(qrel.difficulty, 0) + 1
        for source_type in qrel.expected_source_types:
            by_source_type[source_type] = by_source_type.get(source_type, 0) + 1
    return {
        "count": len(qrels.queries),
        "relevant_labels": sum(len(qrel.relevant) for qrel in qrels.queries),
        "multi_label_queries": sum(1 for qrel in qrels.queries if len(qrel.relevant) > 1),
        "by_intent": dict(sorted(by_intent.items())),
        "by_difficulty": dict(sorted(by_difficulty.items())),
        "by_source_type": dict(sorted(by_source_type.items())),
    }


# --------------------------------------------------------------------------
# Retriever seam (R's rag/retriever.py)
# --------------------------------------------------------------------------


def _import_retriever_module() -> Any:
    """Indirection kept small so tests can stub R's module without an index."""

    return importlib.import_module(_RETRIEVER_MODULE)


def _open_rag_index(index_dir: str | Path | None = None) -> tuple[Any, bool]:
    """Return ``(index, rebuilt)``; reuses the persisted index when current.

    ``build_index`` is R's idempotent ingest path: it compares the corpus digest
    and only rewrites when the corpus/chunker changed, so a stale index can never
    silently produce baseline numbers. Separate indirection keeps unit tests free
    of an index and a corpus.
    """

    from ..rag.index import build_index

    target = Path(index_dir) if index_dir is not None else INDEX_DIR
    index = build_index(REPO_ROOT / "data", target)
    return index, not index.reused


def _retriever_from_index(
    retriever_cls: Any, index_dir: str | Path | None
) -> tuple[Retriever | None, str]:
    try:
        index, _rebuilt = _open_rag_index(index_dir)
    except Exception as exc:
        return None, f"检索索引不可用：{exc}（检查 data/ 语料，或运行 gcc ingest）"
    try:
        instance = retriever_cls(index.sparse, index.dense)
    except Exception as exc:
        return None, f"Retriever(sparse, dense) 构造失败：{exc}"
    if not callable(getattr(instance, "search", None)):
        return None, "Retriever 实例没有 search() 方法（契约待对齐）"
    return instance, ""


def load_retriever(
    factory: Callable[[], Retriever] | None = None,
    *,
    index_dir: str | Path | None = None,
) -> tuple[Retriever | None, str]:
    """Return ``(retriever, reason)``; ``reason`` is empty on success.

    Resolution order mirrors R's documented API (``rag/__init__.py``):
    an explicit ``factory`` (tests), a module-level factory entry point, then
    ``Retriever(sparse, dense)`` built from the persisted ``data/index``.
    """

    if factory is not None:
        return factory(), ""

    try:
        module = _import_retriever_module()
    except ImportError as exc:
        return None, f"待 R 就绪：rag/retriever.py 尚不可导入（{exc}）"

    for entry in _RETRIEVER_ENTRIES:
        candidate = getattr(module, entry, None)
        if candidate is None:
            continue
        try:
            instance = candidate() if callable(candidate) else candidate
        except Exception as exc:
            return None, f"rag/retriever.py 的 {entry}() 构造失败：{exc}"
        if callable(getattr(instance, "search", None)):
            return instance, ""
        return None, f"rag/retriever.py 的 {entry}() 未返回带 search() 的对象"

    retriever_cls = getattr(module, "Retriever", None)
    if retriever_cls is not None:
        return _retriever_from_index(retriever_cls, index_dir)
    return None, "rag/retriever.py 未暴露可用入口（契约待对齐）"


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def _retriever_name(instance: Retriever) -> str:
    cls = type(instance)
    module = getattr(cls, "__module__", "") or ""
    return f"{module}.{cls.__qualname__}".lstrip(".")


def _retriever_details(instance: Retriever, top_k: int) -> dict[str, Any]:
    """Everything needed to reproduce/interpret a baseline number."""

    sparse = getattr(instance, "sparse", None)
    dense = getattr(instance, "dense", None)
    return {
        "name": _retriever_name(instance),
        "top_k": top_k,
        "candidate_k": getattr(instance, "candidate_k", None),
        "fusion_k": getattr(instance, "fusion_k", None),
        "max_results": getattr(instance, "max_results", None),
        "sparse_backend": getattr(sparse, "backend_name", None),
        "dense_backend": getattr(dense, "backend_name", None),
        "dense_available": bool(getattr(instance, "dense_available", False)),
    }


def _build_report(
    qrels: Qrels,
    results: Sequence[QueryResult],
    *,
    metrics: Mapping[str, float],
    retriever: Retriever,
    top_k: int,
    ks: Sequence[int],
    generated_at: str,
) -> dict[str, Any]:
    details = _retriever_details(retriever, top_k)
    notes = [
        *_METRIC_NOTES,
        (
            f"chunk 标识方案 {qrels.chunk_id_scheme}（对齐状态：{qrels.scheme_status}）；"
            "标注已用 rag/chunker.py 的真实 chunk_id 校验，指标实现不做模糊匹配。"
        ),
    ]
    if not details["dense_available"]:
        notes.append(
            "dense 分支为 noop（BGE-M3 / Chroma adapter 未接入），本次基线为 BM25 + RRF "
            "降级配置；dense adapter 就绪后需重跑同一 qrels 再对比。"
        )
    return {
        "suite": "retrieval",
        "status": "ok",
        "generated_at": generated_at,
        "retriever": details,
        "metrics": dict(metrics),
        "config": {
            "top_k": top_k,
            "ks": list(ks),
            "mrr_cutoff": MRR_CUTOFF,
            "n_queries": len(results),
        },
        "qrels": {
            "path": _display_path(qrels.source_path),
            "version": qrels.version,
            "chunk_id_scheme": {
                "format": qrels.chunk_id_scheme,
                "status": qrels.scheme_status,
            },
            "distribution": summarize_qrels(qrels),
        },
        "per_query": [result.as_dict() for result in results],
        "notes": notes,
    }


def write_report(
    report: Mapping[str, Any], path: str | Path | None = None
) -> Path:
    """Write the report as UTF-8 JSON (parents created); returns the path."""

    target = Path(path) if path is not None else REPORT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return target


def run_retrieval_eval(
    *,
    qrels_path: str | Path | None = None,
    report_path: str | Path | None = None,
    retriever: Callable[[], Retriever] | None = None,
    index_dir: str | Path | None = None,
    top_k: int = DEFAULT_TOP_K,
    ks: Sequence[int] = DEFAULT_KS,
    generated_at: str | None = None,
) -> RunResult:
    """Run the retrieval baseline and write ``reports/retrieval-baseline.json``.

    When R's retriever is unavailable the run is skipped with an explanatory
    ``status`` and no report file, so CI never mistakes a placeholder for a
    measured baseline.
    """

    if top_k < max(ks):
        raise RetrievalEvalError(f"top_k={top_k} 小于最大的 k={max(ks)}，无法计算 Recall")
    qrels = load_qrels(qrels_path)
    target = Path(report_path) if report_path is not None else REPORT_PATH

    instance, reason = load_retriever(retriever, index_dir=index_dir)
    if instance is None:
        return RunResult(
            status="retriever_unavailable",
            message=reason,
            report_path=target,
            report=None,
        )

    matcher = ChunkMatcher(qrels.aliases)
    results: list[QueryResult] = []
    for qrel in qrels.queries:
        try:
            hits = list(instance.search(qrel.query, qrel.filters, top_k))
        except RetrievalEvalError:
            raise
        except Exception as exc:
            raise RetrievalEvalError(f"{qrel.qid} 检索失败：{exc}") from exc
        results.append(evaluate_query(qrel, hits, matcher=matcher, ks=ks))

    metrics = aggregate_metrics(results, ks=ks)
    report = _build_report(
        qrels,
        results,
        metrics=metrics,
        retriever=instance,
        top_k=top_k,
        ks=ks,
        generated_at=generated_at or datetime.now().astimezone().isoformat(timespec="seconds"),
    )
    written = write_report(report, target)
    return RunResult(
        status="ok",
        message=(
            f"已评测 {len(results)} 条 query："
            f"Recall@5={metrics['recall@5']:.4f}，MRR@10={metrics['mrr@10']:.4f}；"
            f"报告写入 {_display_path(written)}"
        ),
        report_path=written,
        report=report,
    )
