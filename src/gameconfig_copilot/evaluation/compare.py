"""Regression gate (S7-07): compare a fresh report with a stored baseline.

Contract
--------
Input
    Two evaluation reports (JSON objects, e.g. ``reports/retrieval-baseline.json``
    and ``reports/baseline.json``) or pre-loaded mappings. Metrics are read from
    ``metrics`` and, when present, from nested ``suites`` / ``agent`` /
    ``retrieval`` objects, so a single combined report can carry every suite.
Output
    :class:`CompareResult` with one row per thresholded metric; ``exit_code`` is
    ``1`` when any threshold is breached so CI/pre-push can fail on regressions.
Failure semantics
    * unreadable/malformed report -> :class:`CompareError` (exit code 2 in ``main``);
    * a metric missing from either side is ``skipped`` with a reason, not a silent
      pass and not a fabricated ``0``;
    * thresholds are inclusive: a drop of exactly 3pt / 2pt and a cost increase of
      exactly 20% fail (``>=``), matching 「−3pt / −2pt / +20% 触发失败」.

Thresholds (docs/03 §S7)
------------------------
``FPVR``  −3pt, ``Recall@5`` −2pt, ``cost per run`` +20%.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "COST_INCREASE_THRESHOLD",
    "DEFAULT_BASELINE_PATH",
    "FPVR_DROP_THRESHOLD",
    "RECALL5_DROP_THRESHOLD",
    "CompareError",
    "CompareResult",
    "Comparison",
    "compare_reports",
    "extract_metrics",
    "load_report",
    "main",
    "write_result",
]

REPO_ROOT = Path(__file__).resolve().parents[3]
#: Baselines are committed under docs/reports/ (S7-08); runtime reports stay in
#: reports/ (gitignored artifacts). Keeping the gate's default inside the tracked
#: docs tree means a fresh checkout can run the gate without a prior eval.
DEFAULT_BASELINE_PATH = REPO_ROOT / "docs" / "reports" / "baseline.json"

FPVR_DROP_THRESHOLD = 0.03
RECALL5_DROP_THRESHOLD = 0.02
COST_INCREASE_THRESHOLD = 0.20
_EPSILON = 1e-9

_PASS = "pass"
_FAIL = "fail"
_SKIPPED = "skipped"

_COST_KEYS: tuple[str, ...] = (
    "cost_per_run_cny",
    "cost_cny_per_run",
    "avg_cost_per_run_cny",
    "mean_cost_per_run_cny",
)
_NESTED_REPORT_KEYS: tuple[str, ...] = (
    "agent",
    "retrieval",
    "ragas",
    "observability",
)


class CompareError(RuntimeError):
    """Raised when a report cannot be read or a threshold is invalid."""


@dataclass(frozen=True)
class Comparison:
    """One metric's baseline/current values and threshold verdict."""

    metric: str
    label: str
    baseline: float | None
    current: float | None
    delta: float | None
    relative_delta: float | None
    threshold: float
    status: str
    reason: str
    unit: str = "ratio"

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "label": self.label,
            "baseline": self.baseline,
            "current": self.current,
            "delta": self.delta,
            "relative_delta": self.relative_delta,
            "threshold": self.threshold,
            "status": self.status,
            "reason": self.reason,
            "unit": self.unit,
        }


@dataclass(frozen=True)
class CompareResult:
    """Whole comparison; ``exit_code`` is the regression gate."""

    baseline_path: str | None
    current_path: str | None
    comparisons: tuple[Comparison, ...]

    @property
    def failed(self) -> bool:
        return any(item.status == _FAIL for item in self.comparisons)

    @property
    def exit_code(self) -> int:
        return 1 if self.failed else 0

    @property
    def failures(self) -> tuple[Comparison, ...]:
        return tuple(item for item in self.comparisons if item.status == _FAIL)

    @property
    def skipped(self) -> tuple[Comparison, ...]:
        return tuple(item for item in self.comparisons if item.status == _SKIPPED)

    def as_dict(self) -> dict[str, Any]:
        thresholds = {item.metric: item.threshold for item in self.comparisons}
        return {
            "suite": "regression",
            "status": _FAIL if self.failed else _PASS,
            "exit_code": self.exit_code,
            "baseline": self.baseline_path,
            "current": self.current_path,
            "thresholds": thresholds,
            "comparisons": [item.as_dict() for item in self.comparisons],
            "failures": [item.metric for item in self.failures],
            "skipped": [item.metric for item in self.skipped],
        }

    def to_markdown(self) -> str:
        """Compact diff table for PR comments / docs/reports entries."""

        lines = [
            "# 回归对比",
            "",
            f"- 基线：`{self.baseline_path or '<in-memory>'}`",
            f"- 本次：`{self.current_path or '<in-memory>'}`",
            f"- 结论：**{'FAIL' if self.failed else 'PASS'}**"
            f"（exit_code={self.exit_code}）",
            "",
            "| 指标 | 基线 | 当前 | 变化 | 阈值 | 结果 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for item in self.comparisons:
            lines.append(
                f"| {item.label} | {_format_value(item, item.baseline)} | "
                f"{_format_value(item, item.current)} | "
                f"{_format_delta(item)} | {_format_threshold(item)} | "
                f"{item.status.upper()} |"
            )
        if self.failures:
            lines.extend(["", "## 失败原因"])
            lines.extend(f"- {item.label}：{item.reason}" for item in self.failures)
        if self.skipped:
            lines.extend(["", "## 跳过项"])
            lines.extend(f"- {item.label}：{item.reason}" for item in self.skipped)
        return "\n".join(lines)


def _format_value(item: Comparison, value: float | None) -> str:
    if value is None:
        return "n/a"
    if item.unit == "cny":
        return f"{value:.6f}"
    return f"{value:.4f}"


def _format_delta(item: Comparison) -> str:
    if item.relative_delta is not None:
        return f"{item.relative_delta:+.2%}"
    if item.delta is None:
        return "n/a"
    return f"{item.delta:+.4f}"


def _format_threshold(item: Comparison) -> str:
    if item.unit == "cny":
        return f"+{item.threshold:.2%}"
    return f"-{item.threshold * 100:.1f}pt"


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _metric_value(flat: Mapping[str, float], key: str) -> float | None:
    value = flat.get(key)
    return float(value) if value is not None else None


def _first_metric_value(
    flat: Mapping[str, float], keys: Sequence[str]
) -> float | None:
    for key in keys:
        value = _metric_value(flat, key)
        if value is not None:
            return value
    return None


def extract_metrics(report: Mapping[str, Any]) -> dict[str, float]:
    """Flatten metric names from a report, its ``suites`` and known nested suites."""

    if not isinstance(report, Mapping):
        raise CompareError("报告必须是 JSON 对象")
    flat: dict[str, float] = {}

    def collect(container: Mapping[str, Any]) -> None:
        metrics = container.get("metrics")
        if isinstance(metrics, Mapping):
            for name, value in metrics.items():
                if _is_number(value):
                    flat[str(name)] = float(value)

    collect(report)
    suites = report.get("suites")
    if isinstance(suites, Mapping):
        for nested in suites.values():
            if isinstance(nested, Mapping):
                collect(nested)
    for key in _NESTED_REPORT_KEYS:
        nested = report.get(key)
        if isinstance(nested, Mapping):
            collect(nested)
    return flat


def _compare_drop(
    metric: str,
    label: str,
    baseline: float | None,
    current: float | None,
    threshold: float,
) -> Comparison:
    delta = current - baseline if baseline is not None and current is not None else None
    relative = (
        delta / abs(baseline)
        if delta is not None and baseline not in (None, 0)
        else None
    )
    if baseline is None or current is None:
        missing = "基线" if baseline is None else "当前报告"
        return Comparison(
            metric=metric,
            label=label,
            baseline=baseline,
            current=current,
            delta=None,
            relative_delta=None,
            threshold=threshold,
            status=_SKIPPED,
            reason=f"{missing}缺少 {metric}，跳过阈值检查（不按 0 计）",
        )
    drop = baseline - current
    if drop >= threshold - _EPSILON:
        return Comparison(
            metric=metric,
            label=label,
            baseline=baseline,
            current=current,
            delta=delta,
            relative_delta=relative,
            threshold=threshold,
            status=_FAIL,
            reason=(
                f"从 {baseline:.4f} 降到 {current:.4f}（-{drop * 100:.2f}pt），"
                f"达到/超过 {threshold * 100:.1f}pt 回退阈值"
            ),
        )
    if drop > 0:
        reason = f"下降 {drop * 100:.2f}pt，未达 {threshold * 100:.1f}pt 阈值"
    else:
        reason = f"提升 {-drop * 100:.2f}pt"
    return Comparison(
        metric=metric,
        label=label,
        baseline=baseline,
        current=current,
        delta=delta,
        relative_delta=relative,
        threshold=threshold,
        status=_PASS,
        reason=reason,
    )


def _compare_cost(
    baseline: float | None,
    current: float | None,
    threshold: float,
) -> Comparison:
    if baseline is None or current is None:
        missing = "基线" if baseline is None else "当前报告"
        return Comparison(
            metric="cost_per_run_cny",
            label="单次成本",
            baseline=baseline,
            current=current,
            delta=None,
            relative_delta=None,
            threshold=threshold,
            status=_SKIPPED,
            reason=f"{missing}缺少成本指标，跳过阈值检查（不按 0 计）",
            unit="cny",
        )
    delta = current - baseline
    if baseline <= 0:
        if current <= 0:
            return Comparison(
                metric="cost_per_run_cny",
                label="单次成本",
                baseline=baseline,
                current=current,
                delta=delta,
                relative_delta=None,
                threshold=threshold,
                status=_PASS,
                reason="基线成本为 0，当前仍为 0",
                unit="cny",
            )
        return Comparison(
            metric="cost_per_run_cny",
            label="单次成本",
            baseline=baseline,
            current=current,
            delta=delta,
            relative_delta=None,
            threshold=threshold,
            status=_FAIL,
            reason=f"基线成本为 0，当前为 {current:.6f} CNY，无法按比例比较",
            unit="cny",
        )
    increase = current / baseline - 1
    if increase >= threshold - _EPSILON:
        return Comparison(
            metric="cost_per_run_cny",
            label="单次成本",
            baseline=baseline,
            current=current,
            delta=delta,
            relative_delta=increase,
            threshold=threshold,
            status=_FAIL,
            reason=(
                f"从 {baseline:.6f} 升到 {current:.6f} CNY（{increase:+.2%}），"
                f"达到/超过 +{threshold * 100:.0f}% 成本阈值"
            ),
            unit="cny",
        )
    return Comparison(
        metric="cost_per_run_cny",
        label="单次成本",
        baseline=baseline,
        current=current,
        delta=delta,
        relative_delta=increase,
        threshold=threshold,
        status=_PASS,
        reason=f"成本变化 {increase:+.2%}，未达 +{threshold * 100:.0f}% 阈值",
        unit="cny",
    )


def _validate_threshold(name: str, value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CompareError(f"{name} 必须是数字")
    if not 0 <= float(value) <= 1:
        raise CompareError(f"{name} 必须在 [0, 1] 内")
    return float(value)


def compare_reports(
    current: Mapping[str, Any],
    baseline: Mapping[str, Any],
    *,
    baseline_path: str | None = None,
    current_path: str | None = None,
    fpvr_threshold: float = FPVR_DROP_THRESHOLD,
    recall5_threshold: float = RECALL5_DROP_THRESHOLD,
    cost_threshold: float = COST_INCREASE_THRESHOLD,
) -> CompareResult:
    """Compare flattened metrics and apply the S7 regression thresholds."""

    if not isinstance(current, Mapping) or not isinstance(baseline, Mapping):
        raise CompareError("current / baseline 必须是 JSON 对象")
    fpvr_threshold = _validate_threshold("fpvr_threshold", fpvr_threshold)
    recall5_threshold = _validate_threshold("recall5_threshold", recall5_threshold)
    cost_threshold = _validate_threshold("cost_threshold", cost_threshold)

    current_flat = extract_metrics(current)
    baseline_flat = extract_metrics(baseline)
    comparisons = [
        _compare_drop(
            "fpvr",
            "FPVR",
            _metric_value(baseline_flat, "fpvr"),
            _metric_value(current_flat, "fpvr"),
            fpvr_threshold,
        ),
        _compare_drop(
            "recall@5",
            "Recall@5",
            _metric_value(baseline_flat, "recall@5"),
            _metric_value(current_flat, "recall@5"),
            recall5_threshold,
        ),
        _compare_cost(
            _first_metric_value(baseline_flat, _COST_KEYS),
            _first_metric_value(current_flat, _COST_KEYS),
            cost_threshold,
        ),
    ]
    return CompareResult(
        baseline_path=baseline_path,
        current_path=current_path,
        comparisons=tuple(comparisons),
    )


def load_report(path: str | Path) -> dict[str, Any]:
    """Read a JSON report; missing/malformed files raise :class:`CompareError`."""

    target = Path(path)
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise CompareError(f"报告文件不存在：{target}") from exc
    except OSError as exc:
        raise CompareError(f"报告文件无法读取：{target}（{exc}）") from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CompareError(
            f"报告不是合法 JSON：{target} 第 {exc.lineno} 行第 {exc.colno} 列"
            f"（{exc.msg}）"
        ) from exc
    if not isinstance(raw, Mapping):
        raise CompareError(f"报告必须是 JSON 对象：{target}")
    return dict(raw)


def write_result(result: CompareResult, path: str | Path) -> Path:
    """Write the comparison as UTF-8 JSON (parents created); returns the path."""

    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(result.as_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise CompareError(f"对比结果无法写入：{target}（{exc}）") from exc
    return target


def main(argv: Sequence[str] | None = None) -> int:
    """CLI: ``python -m gameconfig_copilot.evaluation.compare --current ...``."""

    parser = argparse.ArgumentParser(description="与基线报告对比并返回回归退出码")
    parser.add_argument("--baseline", default=str(DEFAULT_BASELINE_PATH))
    parser.add_argument("--current", required=True)
    parser.add_argument("--json", dest="json_path", default=None)
    parser.add_argument(
        "--fpvr-threshold", type=float, default=FPVR_DROP_THRESHOLD
    )
    parser.add_argument(
        "--recall5-threshold", type=float, default=RECALL5_DROP_THRESHOLD
    )
    parser.add_argument(
        "--cost-threshold", type=float, default=COST_INCREASE_THRESHOLD
    )
    args = parser.parse_args(argv)
    try:
        baseline = load_report(args.baseline)
        current = load_report(args.current)
        result = compare_reports(
            current,
            baseline,
            baseline_path=args.baseline,
            current_path=args.current,
            fpvr_threshold=args.fpvr_threshold,
            recall5_threshold=args.recall5_threshold,
            cost_threshold=args.cost_threshold,
        )
    except CompareError as exc:
        print(f"对比失败：{exc}")
        return 2
    print(result.to_markdown())
    if args.json_path:
        try:
            print(f"JSON 对比结果：{write_result(result, args.json_path)}")
        except CompareError as exc:
            print(f"对比失败：{exc}")
            return 2
    return result.exit_code


if __name__ == "__main__":  # pragma: no cover - CLI glue
    raise SystemExit(main())
