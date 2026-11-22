"""Ragas answer-quality harness (S7-02): lazy, optional and never fabricated.

Contract
--------
Input
    A sequence of :class:`RagasSample` (question / answer / retrieved contexts /
    optional ground truth). The judge model is pinned by
    :class:`JudgeConfig` (``GCC_RAGAS_JUDGE_MODEL`` + ``GCC_RAGAS_JUDGE_VERSION``),
    so two runs are only comparable when both id and version match.
Output
    ``reports/ragas-baseline.json`` — per-metric means, denominators, per-sample
    scores, the exact judge id and the metric names. Ragas itself is imported
    **inside** :func:`load_ragas`, so importing this module (and the test suite)
    never requires the optional dependency.
Failure semantics
    * ragas not installed -> :class:`RagasUnavailable`; no report is written and
      no score is invented.
    * an injected judge is a test/replay seam only: its report is marked
      ``engine="injected_judge"`` and is not written to the default report path.
    * a judge returning a non-number / value outside ``[0, 1]`` -> :class:`RagasEvalError`
      naming the sample and metric.
    * samples empty, missing fields, or an unknown metric -> :class:`RagasEvalError`.

The fixed judge version is a deliberate anti-drift control: Ragas scores are only
meaningful relative to the model that produced them, so ``model_id`` is recorded
in every report and validated to be a pinned date/semver rather than ``latest``.
"""

from __future__ import annotations

import importlib
import json
import math
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

__all__ = [
    "DEFAULT_JUDGE_MODEL",
    "DEFAULT_JUDGE_TEMPERATURE",
    "DEFAULT_JUDGE_VERSION",
    "DEFAULT_METRICS",
    "JUDGE_MAX_TOKENS_ENV",
    "JUDGE_MODEL_ENV",
    "JUDGE_TEMPERATURE_ENV",
    "JUDGE_VERSION_ENV",
    "RAGAS_MIN_VERSION",
    "REPORT_PATH",
    "Judge",
    "JudgeConfig",
    "RagasEvalError",
    "RagasSample",
    "RagasUnavailable",
    "RunResult",
    "aggregate_scores",
    "build_report",
    "load_ragas",
    "run_ragas_eval",
    "score_samples",
    "write_report",
]

REPO_ROOT = Path(__file__).resolve().parents[3]
REPORT_PATH = REPO_ROOT / "reports" / "ragas-baseline.json"

RAGAS_PACKAGE = "ragas"
RAGAS_MIN_VERSION = "0.2"
DEFAULT_METRICS: tuple[str, ...] = (
    "faithfulness",
    "answer_relevancy",
    "context_precision",
    "context_recall",
)

#: Pinned judge identity (ADR-008 default provider is DeepSeek-compatible).
DEFAULT_JUDGE_MODEL = "deepseek-chat"
DEFAULT_JUDGE_VERSION = "2026-11-01"
DEFAULT_JUDGE_TEMPERATURE = 0.0
DEFAULT_JUDGE_MAX_TOKENS = 1024
#: ADR-008 default provider; only used when the caller did not set GCC_LLM_BASE_URL.
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"

JUDGE_MODEL_ENV = "GCC_RAGAS_JUDGE_MODEL"
JUDGE_VERSION_ENV = "GCC_RAGAS_JUDGE_VERSION"
JUDGE_TEMPERATURE_ENV = "GCC_RAGAS_JUDGE_TEMPERATURE"
JUDGE_MAX_TOKENS_ENV = "GCC_RAGAS_JUDGE_MAX_TOKENS"
JUDGE_EMBEDDING_ENV = "GCC_RAGAS_JUDGE_EMBEDDING"

_PINNED_VERSION_RE = re.compile(r"^(?:v?\d{4}-\d{2}-\d{2}|v?\d+\.\d+(?:\.\d+)?)$")
_FLOATING_VERSIONS = frozenset({"latest", "main", "master", "head", "stable", "edge", "*"})

_ENGINE_RAGAS = "ragas"
_ENGINE_INJECTED = "injected_judge"


class RagasEvalError(RuntimeError):
    """Raised for malformed samples, invalid judge output or a failing evaluation."""


class RagasUnavailable(RagasEvalError):
    """Raised when ragas (or its judge adapter) is not installed; never faked."""


class Judge(Protocol):
    """Minimal judge contract: score one metric for one sample in ``[0, 1]``."""

    def score(self, metric: str, sample: RagasSample) -> float:
        """Return the metric value for ``sample``."""
        ...


@dataclass(frozen=True)
class JudgeConfig:
    """Pinned judge identity and decoding parameters recorded in every report."""

    model: str = DEFAULT_JUDGE_MODEL
    version: str = DEFAULT_JUDGE_VERSION
    temperature: float = DEFAULT_JUDGE_TEMPERATURE
    max_tokens: int = DEFAULT_JUDGE_MAX_TOKENS

    def __post_init__(self) -> None:
        model = self.model.strip() if isinstance(self.model, str) else ""
        if not model:
            raise RagasEvalError(f"{JUDGE_MODEL_ENV}（judge model）必须是非空字符串")
        version = self.version.strip() if isinstance(self.version, str) else ""
        lowered = version.lower()
        if lowered in _FLOATING_VERSIONS:
            raise RagasEvalError(
                f"{JUDGE_VERSION_ENV} 必须是固定版本（日期或 semver），"
                f"不能是 {version!r}；浮动版本会让两次 Ragas 分数不可比"
            )
        if not _PINNED_VERSION_RE.match(version):
            raise RagasEvalError(
                f"{JUDGE_VERSION_ENV} 必须是固定版本（YYYY-MM-DD 或 semver），"
                f"当前为 {version!r}"
            )
        if isinstance(self.temperature, bool) or not isinstance(
            self.temperature, (int, float)
        ):
            raise RagasEvalError(f"{JUDGE_TEMPERATURE_ENV} 必须是数字")
        temperature = float(self.temperature)
        if not 0 <= temperature <= 2:
            raise RagasEvalError(f"{JUDGE_TEMPERATURE_ENV} 必须在 [0, 2] 内")
        if isinstance(self.max_tokens, bool) or not isinstance(self.max_tokens, int):
            raise RagasEvalError(f"{JUDGE_MAX_TOKENS_ENV} 必须是整数")
        if self.max_tokens < 1:
            raise RagasEvalError(f"{JUDGE_MAX_TOKENS_ENV} 必须 ≥ 1")
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "version", version)
        object.__setattr__(self, "temperature", temperature)

    @property
    def model_id(self) -> str:
        """Stable ``model@version`` identity used to compare runs."""

        return f"{self.model}@{self.version}"

    def as_dict(self) -> dict[str, Any]:
        """Report-safe view (no API key/base URL, only reproducible knobs)."""

        return {
            "model": self.model,
            "version": self.version,
            "model_id": self.model_id,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "pinned": True,
        }

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> JudgeConfig:
        """Build the config from ``GCC_RAGAS_JUDGE_*`` (defaults are pinned)."""

        source = os.environ if env is None else env
        model = source.get(JUDGE_MODEL_ENV, DEFAULT_JUDGE_MODEL)
        version = source.get(JUDGE_VERSION_ENV, DEFAULT_JUDGE_VERSION)
        raw_temperature = source.get(JUDGE_TEMPERATURE_ENV)
        raw_max_tokens = source.get(JUDGE_MAX_TOKENS_ENV)
        try:
            temperature = (
                float(raw_temperature)
                if raw_temperature is not None
                else DEFAULT_JUDGE_TEMPERATURE
            )
        except ValueError as exc:
            raise RagasEvalError(
                f"{JUDGE_TEMPERATURE_ENV} 必须是数字，当前为 {raw_temperature!r}"
            ) from exc
        try:
            max_tokens = (
                int(raw_max_tokens)
                if raw_max_tokens is not None
                else DEFAULT_JUDGE_MAX_TOKENS
            )
        except ValueError as exc:
            raise RagasEvalError(
                f"{JUDGE_MAX_TOKENS_ENV} 必须是整数，当前为 {raw_max_tokens!r}"
            ) from exc
        return cls(
            model=model,
            version=version,
            temperature=temperature,
            max_tokens=max_tokens,
        )


@dataclass(frozen=True)
class RagasSample:
    """One answer-quality sample; contexts/ground truth drive applicability."""

    sample_id: str
    question: str
    answer: str
    contexts: tuple[str, ...] = ()
    ground_truth: str | None = None

    def __post_init__(self) -> None:
        for field_name, value in (
            ("sample_id", self.sample_id),
            ("question", self.question),
            ("answer", self.answer),
        ):
            if not isinstance(value, str) or not value.strip():
                raise RagasEvalError(f"{field_name} 必须是非空字符串")
            object.__setattr__(self, field_name, value.strip())
        if not isinstance(self.contexts, (list, tuple)):
            raise RagasEvalError("contexts 必须是字符串数组")
        contexts: list[str] = []
        for position, context in enumerate(self.contexts):
            if not isinstance(context, str) or not context.strip():
                raise RagasEvalError(f"contexts[{position}] 必须是非空字符串")
            contexts.append(context.strip())
        object.__setattr__(self, "contexts", tuple(contexts))
        if self.ground_truth is not None:
            if not isinstance(self.ground_truth, str) or not self.ground_truth.strip():
                raise RagasEvalError("ground_truth 必须是非空字符串或 None")
            object.__setattr__(self, "ground_truth", self.ground_truth.strip())


def load_ragas() -> Any:
    """Import ragas lazily; raise :class:`RagasUnavailable` instead of faking scores."""

    try:
        return importlib.import_module(RAGAS_PACKAGE)
    except ImportError as exc:
        missing = getattr(exc, "name", RAGAS_PACKAGE) or RAGAS_PACKAGE
        if missing == RAGAS_PACKAGE:
            raise RagasUnavailable(
                "Ragas 未安装：pip install "
                f"'{RAGAS_PACKAGE}>={RAGAS_MIN_VERSION}' langchain-openai；"
                "本 harness 不会用假分数代替真实评测，也不会写替代报告。"
            ) from exc
        raise RagasUnavailable(
            f"Ragas 已安装但缺少依赖 {missing!r}：请安装 "
            f"'{RAGAS_PACKAGE}>={RAGAS_MIN_VERSION}' 的完整依赖（含 langchain-openai）"
        ) from exc


def _normalize_metrics(metrics: Sequence[str]) -> tuple[str, ...]:
    if isinstance(metrics, (str, bytes)) or not isinstance(metrics, Sequence):
        raise RagasEvalError("metrics 必须是字符串数组")
    normalized: list[str] = []
    for metric in metrics:
        if not isinstance(metric, str) or metric not in DEFAULT_METRICS:
            raise RagasEvalError(
                f"未知 Ragas 指标 {metric!r}；可选 {'/'.join(DEFAULT_METRICS)}"
            )
        if metric in normalized:
            continue
        normalized.append(metric)
    if not normalized:
        raise RagasEvalError("metrics 不能为空")
    return tuple(normalized)


def _normalize_samples(samples: Sequence[RagasSample]) -> tuple[RagasSample, ...]:
    if isinstance(samples, (str, bytes)) or not isinstance(samples, Sequence):
        raise RagasEvalError("samples 必须是 RagasSample 数组")
    normalized: list[RagasSample] = []
    seen: set[str] = set()
    for position, sample in enumerate(samples):
        if not isinstance(sample, RagasSample):
            raise RagasEvalError(f"samples[{position}] 必须是 RagasSample")
        if sample.sample_id in seen:
            raise RagasEvalError(f"samples 中 sample_id 重复：{sample.sample_id}")
        seen.add(sample.sample_id)
        normalized.append(sample)
    if not normalized:
        raise RagasEvalError("samples 不能为空")
    return tuple(normalized)


def _metric_applicable(metric: str, sample: RagasSample) -> bool:
    """Which metrics can be computed without inventing missing context/labels."""

    has_context = bool(sample.contexts)
    if metric == "faithfulness":
        return has_context
    if metric == "answer_relevancy":
        return True
    if metric in {"context_precision", "context_recall"}:
        return has_context and sample.ground_truth is not None
    raise RagasEvalError(f"未知 Ragas 指标 {metric!r}")


def _validate_score(raw: Any, where: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise RagasEvalError(f"{where} 必须返回数字，当前为 {type(raw).__name__}")
    score = float(raw)
    if not math.isfinite(score) or not 0 <= score <= 1:
        raise RagasEvalError(f"{where} 必须在 [0, 1] 内，当前为 {score}")
    return score


def _score_judge(judge: Any, metric: str, sample: RagasSample) -> float:
    method = getattr(judge, "score", None)
    if callable(method):
        return _validate_score(method(metric, sample), f"{sample.sample_id}.{metric}")
    if callable(judge):
        return _validate_score(judge(metric, sample), f"{sample.sample_id}.{metric}")
    raise RagasEvalError("judge 必须是带 score(metric, sample) 的对象或可调用对象")


def aggregate_scores(
    rows: Sequence[Mapping[str, Any]],
    samples: Sequence[RagasSample],
    metrics: Sequence[str] = DEFAULT_METRICS,
) -> dict[str, Any]:
    """Average per-sample scores with an explicit denominator per metric."""

    normalized_metrics = _normalize_metrics(metrics)
    normalized_samples = _normalize_samples(samples)
    if len(rows) != len(normalized_samples):
        raise RagasEvalError(
            f"评分行数 {len(rows)} 与样本数 {len(normalized_samples)} 不一致"
        )
    per_sample: list[dict[str, Any]] = []
    for position, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise RagasEvalError(f"rows[{position}] 必须是 JSON 对象")
        expected_id = normalized_samples[position].sample_id
        if row.get("id") != expected_id:
            raise RagasEvalError(
                f"rows[{position}].id 应为 {expected_id}，当前为 {row.get('id')!r}"
            )
        entry: dict[str, Any] = {"id": expected_id}
        for metric in normalized_metrics:
            entry[metric] = row.get(metric)
        per_sample.append(entry)

    denominators: dict[str, int] = {}
    means: dict[str, float | None] = {}
    for metric in normalized_metrics:
        values = [
            _validate_score(entry[metric], f"{entry['id']}.{metric}")
            for entry in per_sample
            if entry[metric] is not None
        ]
        denominators[metric] = len(values)
        means[metric] = round(sum(values) / len(values), 4) if values else None
    return {
        "metrics": means,
        "denominators": denominators,
        "per_sample": per_sample,
    }


def score_samples(
    samples: Sequence[RagasSample],
    judge: Any,
    metrics: Sequence[str] = DEFAULT_METRICS,
) -> dict[str, Any]:
    """Score samples with an injected judge (test/replay seam, not a ragas call)."""

    normalized = _normalize_samples(samples)
    normalized_metrics = _normalize_metrics(metrics)
    rows: list[dict[str, Any]] = []
    for sample in normalized:
        row: dict[str, Any] = {"id": sample.sample_id}
        for metric in normalized_metrics:
            row[metric] = (
                _score_judge(judge, metric, sample)
                if _metric_applicable(metric, sample)
                else None
            )
        rows.append(row)
    return aggregate_scores(rows, normalized, normalized_metrics)


def build_report(
    samples: Sequence[RagasSample],
    aggregation: Mapping[str, Any],
    *,
    judge: Mapping[str, Any],
    engine: str,
    metrics: Sequence[str],
    generated_at: str,
    notes: Sequence[str] = (),
) -> dict[str, Any]:
    """Assemble the JSON report shared by the ragas and injected-judge paths."""

    normalized = _normalize_samples(samples)
    normalized_metrics = _normalize_metrics(metrics)
    if engine not in {_ENGINE_RAGAS, _ENGINE_INJECTED}:
        raise RagasEvalError(f"未知 engine {engine!r}")
    if not isinstance(aggregation, Mapping):
        raise RagasEvalError("aggregation 必须是 JSON 对象")
    metric_notes = list(notes)
    metric_notes.append(
        f"judge 固定为 {judge.get('model_id')}（temperature={judge.get('temperature')}，"
        f"max_tokens={judge.get('max_tokens')}）；更换模型或版本后分数不可直接对比。"
    )
    if engine == _ENGINE_INJECTED:
        metric_notes.append(
            "本次 engine=injected_judge（单测/录制回放接缝），不是真实 Ragas 基线；"
            "默认报告路径不会写盘。"
        )
    denominators = {
        "samples": len(normalized),
        **dict(aggregation.get("denominators", {})),
    }
    return {
        "suite": "ragas",
        "status": "ok",
        "generated_at": generated_at,
        "engine": engine,
        "judge": {**dict(judge), "source": engine},
        "metrics": dict(aggregation.get("metrics", {})),
        "denominators": denominators,
        "config": {
            "metrics": list(normalized_metrics),
            "ragas_min_version": RAGAS_MIN_VERSION,
        },
        "per_sample": list(aggregation.get("per_sample", ())),
        "notes": metric_notes,
    }


def write_report(report: Mapping[str, Any], path: str | Path | None = None) -> Path:
    """Write the report as UTF-8 JSON (parents created); returns the path."""

    target = Path(path) if path is not None else REPORT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return target


@dataclass(frozen=True)
class RunResult:
    """Outcome of one Ragas run; ``report_path`` is ``None`` for an unwritten run."""

    status: str
    message: str
    report_path: Path | None
    report: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _result_rows(
    result: Any,
    samples: Sequence[RagasSample],
    metrics: Sequence[str],
) -> list[dict[str, Any]]:
    """Convert a ragas ``EvaluationResult`` into the harness' flat rows."""

    to_pandas = getattr(result, "to_pandas", None)
    if not callable(to_pandas):
        raise RagasEvalError(
            "ragas 结果没有 to_pandas()；请升级到 ragas>=0.2 或使用 --engine 回放接缝"
        )
    try:
        frame = to_pandas()
        records = frame.to_dict("records")
    except Exception as exc:  # third-party shape drift must be reported, not guessed
        raise RagasEvalError(f"ragas 结果无法转换为表格：{exc}") from exc
    if len(records) != len(samples):
        raise RagasEvalError(
            f"ragas 返回 {len(records)} 行，但输入 {len(samples)} 条样本"
        )
    rows: list[dict[str, Any]] = []
    for position, record in enumerate(records):
        sample = samples[position]
        row: dict[str, Any] = {"id": sample.sample_id}
        for metric in metrics:
            raw = record.get(metric)
            row[metric] = (
                _validate_score(raw, f"{sample.sample_id}.{metric}")
                if raw is not None and not (isinstance(raw, float) and math.isnan(raw))
                else None
            )
        rows.append(row)
    return rows


def _metric_objects(metrics: Sequence[str]) -> list[Any]:
    try:
        from ragas import metrics as ragas_metrics
    except ImportError as exc:
        raise RagasUnavailable(
            f"ragas>= {RAGAS_MIN_VERSION} 的指标模块不可用：{exc}"
        ) from exc
    objects: list[Any] = []
    for name in metrics:
        metric = getattr(ragas_metrics, name, None)
        if metric is None:
            raise RagasUnavailable(
                f"已安装的 ragas 不提供指标 {name!r}；需要 ragas>={RAGAS_MIN_VERSION}"
            )
        objects.append(metric)
    return objects


def _build_dataset(module: Any, samples: Sequence[RagasSample]) -> Any:
    dataset_cls = getattr(module, "EvaluationDataset", None)
    sample_cls = getattr(module, "SingleTurnSample", None)
    if dataset_cls is None or sample_cls is None:
        raise RagasUnavailable(
            "已安装的 ragas 缺少 EvaluationDataset / SingleTurnSample；"
            f"需要 ragas>={RAGAS_MIN_VERSION}"
        )
    try:
        dataset = dataset_cls(
            samples=[
                sample_cls(
                    user_input=sample.question,
                    response=sample.answer,
                    retrieved_contexts=list(sample.contexts),
                    reference=sample.ground_truth,
                )
                for sample in samples
            ]
        )
    except Exception as exc:
        raise RagasEvalError(f"构建 ragas 数据集失败：{exc}") from exc
    return dataset


def _judge_credentials() -> tuple[str, str | None]:
    api_key = os.environ.get("GCC_LLM_API_KEY", "").strip()
    if not api_key:
        raise RagasEvalError(
            "Ragas judge 需要 GCC_LLM_API_KEY（与业务 LLM 同一 provider 配置）；"
            "密钥只从环境读取，不写入报告"
        )
    base_url = os.environ.get("GCC_LLM_BASE_URL", "").strip() or None
    return api_key, base_url


def _build_judge_llm(config: JudgeConfig) -> Any:
    api_key, base_url = _judge_credentials()
    try:
        from langchain_openai import ChatOpenAI
        from ragas.llms import LangchainLLM
    except ImportError as exc:
        raise RagasUnavailable(
            "ragas 已安装，但缺少 judge 适配依赖 langchain-openai："
            "pip install langchain-openai"
        ) from exc
    kwargs: dict[str, Any] = {
        "model": config.model,
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
        "api_key": api_key,
    }
    if base_url:
        kwargs["base_url"] = base_url
    elif config.model.startswith("deepseek"):
        kwargs["base_url"] = DEFAULT_DEEPSEEK_BASE_URL
    try:
        return LangchainLLM(ChatOpenAI(**kwargs))
    except Exception as exc:
        raise RagasEvalError(f"judge 模型 {config.model_id} 初始化失败：{exc}") from exc


def _build_judge_embeddings() -> Any:
    api_key, base_url = _judge_credentials()
    model = os.environ.get(JUDGE_EMBEDDING_ENV, "text-embedding-3-small").strip()
    try:
        from langchain_openai import OpenAIEmbeddings
        from ragas.embeddings import LangchainEmbeddingsWrapper
    except ImportError as exc:
        raise RagasUnavailable(
            "answer_relevancy / context_precision 需要 embedding 适配依赖："
            "pip install langchain-openai"
        ) from exc
    kwargs: dict[str, Any] = {"model": model, "api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    try:
        return LangchainEmbeddingsWrapper(OpenAIEmbeddings(**kwargs))
    except Exception as exc:
        raise RagasEvalError(f"judge embedding {model!r} 初始化失败：{exc}") from exc


def evaluate_with_ragas(
    module: Any,
    samples: Sequence[RagasSample],
    config: JudgeConfig,
    *,
    metrics: Sequence[str],
    generated_at: str,
) -> dict[str, Any]:
    """Run the real ragas evaluator through the configured pinned judge."""

    normalized = _normalize_samples(samples)
    normalized_metrics = _normalize_metrics(metrics)
    dataset = _build_dataset(module, normalized)
    metric_objects = _metric_objects(normalized_metrics)
    kwargs: dict[str, Any] = {
        "dataset": dataset,
        "metrics": metric_objects,
        "llm": _build_judge_llm(config),
    }
    if any(
        metric in {"answer_relevancy", "context_precision"}
        for metric in normalized_metrics
    ):
        kwargs["embeddings"] = _build_judge_embeddings()
    try:
        result = module.evaluate(**kwargs)
    except Exception as exc:  # third-party failure: surface it, never fabricate
        raise RagasEvalError(f"ragas 评测失败（judge={config.model_id}）：{exc}") from exc
    rows = _result_rows(result, normalized, normalized_metrics)
    aggregation = aggregate_scores(rows, normalized, normalized_metrics)
    return build_report(
        normalized,
        aggregation,
        judge=config.as_dict(),
        engine=_ENGINE_RAGAS,
        metrics=normalized_metrics,
        generated_at=generated_at,
    )


def run_ragas_eval(
    *,
    samples: Sequence[RagasSample],
    judge: Any = None,
    judge_config: JudgeConfig | None = None,
    metrics: Sequence[str] = DEFAULT_METRICS,
    report_path: str | Path | None = None,
    generated_at: str | None = None,
) -> RunResult:
    """Run Ragas (or an injected judge) and optionally write the report.

    Without ``judge`` this requires a working ragas installation: absence raises
    :class:`RagasUnavailable` and writes nothing. With an injected ``judge`` the
    run is an explicit test/replay path and is only written when the caller gives
    an explicit ``report_path`` (so a fake score can never overwrite the baseline).
    """

    normalized = _normalize_samples(samples)
    normalized_metrics = _normalize_metrics(metrics)
    config = judge_config if judge_config is not None else JudgeConfig.from_env()
    if not isinstance(config, JudgeConfig):
        raise RagasEvalError("judge_config 必须是 JudgeConfig")
    timestamp = generated_at or datetime.now().astimezone().isoformat(timespec="seconds")

    if judge is None:
        module = load_ragas()
        report = evaluate_with_ragas(
            module,
            normalized,
            config,
            metrics=normalized_metrics,
            generated_at=timestamp,
        )
        target: Path | None = (
            Path(report_path) if report_path is not None else REPORT_PATH
        )
    else:
        aggregation = score_samples(normalized, judge, normalized_metrics)
        report = build_report(
            normalized,
            aggregation,
            judge=config.as_dict(),
            engine=_ENGINE_INJECTED,
            metrics=normalized_metrics,
            generated_at=timestamp,
        )
        target = Path(report_path) if report_path is not None else None

    written = write_report(report, target) if target is not None else None
    metric_text = "，".join(
        f"{name}={value if value is not None else 'n/a'}"
        for name, value in report["metrics"].items()
    )
    message = (
        f"Ragas engine={report['engine']}，{len(normalized)} 条样本：{metric_text}；"
        + (f"报告写入 {written}" if written is not None else "未写盘（注入 judge 路径）")
    )
    return RunResult(
        status="ok",
        message=message,
        report_path=written,
        report=report,
    )
