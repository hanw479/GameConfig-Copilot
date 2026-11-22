"""Unit tests for the S7-07 regression gate and its exit codes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from gameconfig_copilot.evaluation import compare

BASE_METRICS = {
    "fpvr": 0.80,
    "recall@5": 0.80,
    "cost_per_run_cny": 0.010,
}


def _report(metrics: dict[str, Any], suite: str = "combined") -> dict[str, Any]:
    return {
        "suite": suite,
        "generated_at": "2026-11-15T12:00:00+08:00",
        "metrics": metrics,
    }


def _compare(
    current: dict[str, Any], baseline: dict[str, Any]
) -> compare.CompareResult:
    return compare.compare_reports(
        current,
        baseline,
        baseline_path="baseline.json",
        current_path="current.json",
    )


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------


def test_no_regression_passes_with_exit_code_zero() -> None:
    result = _compare(
        _report({"fpvr": 0.79, "recall@5": 0.79, "cost_per_run_cny": 0.011}),
        _report(BASE_METRICS),
    )

    assert not result.failed
    assert result.exit_code == 0
    assert {item.status for item in result.comparisons} == {"pass"}


def test_fpvr_drop_of_exactly_three_points_fails() -> None:
    result = _compare(
        _report({"fpvr": 0.77, "recall@5": 0.80, "cost_per_run_cny": 0.010}),
        _report(BASE_METRICS),
    )

    fpvr = next(item for item in result.comparisons if item.metric == "fpvr")
    assert fpvr.status == "fail"
    assert result.exit_code == 1
    assert result.failures == (fpvr,)


def test_fpvr_drop_just_under_threshold_passes() -> None:
    result = _compare(
        _report({"fpvr": 0.7701, "recall@5": 0.80, "cost_per_run_cny": 0.010}),
        _report(BASE_METRICS),
    )

    assert result.exit_code == 0


def test_recall5_drop_of_exactly_two_points_fails() -> None:
    result = _compare(
        _report({"fpvr": 0.80, "recall@5": 0.78, "cost_per_run_cny": 0.010}),
        _report(BASE_METRICS),
    )

    recall = next(item for item in result.comparisons if item.metric == "recall@5")
    assert recall.status == "fail"
    assert result.exit_code == 1


def test_cost_increase_of_exactly_twenty_percent_fails() -> None:
    result = _compare(
        _report({"fpvr": 0.80, "recall@5": 0.80, "cost_per_run_cny": 0.012}),
        _report(BASE_METRICS),
    )

    cost = next(
        item for item in result.comparisons if item.metric == "cost_per_run_cny"
    )
    assert cost.status == "fail"
    assert cost.relative_delta == pytest.approx(0.2)
    assert result.exit_code == 1


def test_cost_increase_below_twenty_percent_passes() -> None:
    result = _compare(
        _report({"fpvr": 0.80, "recall@5": 0.80, "cost_per_run_cny": 0.0119}),
        _report(BASE_METRICS),
    )

    assert result.exit_code == 0


def test_zero_baseline_cost_with_positive_current_cost_fails() -> None:
    result = _compare(
        _report({"fpvr": 0.80, "recall@5": 0.80, "cost_per_run_cny": 0.001}),
        _report({"fpvr": 0.80, "recall@5": 0.80, "cost_per_run_cny": 0.0}),
    )

    cost = next(
        item for item in result.comparisons if item.metric == "cost_per_run_cny"
    )
    assert cost.status == "fail"
    assert "基线成本为 0" in cost.reason


def test_cost_aliases_are_resolved_per_report() -> None:
    result = _compare(
        _report({"fpvr": 0.80, "recall@5": 0.80, "cost_per_run_cny": 0.02}),
        _report({"fpvr": 0.80, "recall@5": 0.80, "cost_cny_per_run": 0.01}),
    )

    cost = next(
        item for item in result.comparisons if item.metric == "cost_per_run_cny"
    )
    assert cost.status == "fail"
    assert cost.relative_delta == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Missing metrics / nested reports
# ---------------------------------------------------------------------------


def test_missing_metrics_are_skipped_not_treated_as_zero() -> None:
    result = _compare(_report({"recall@5": 0.80}), _report(BASE_METRICS))

    fpvr = next(item for item in result.comparisons if item.metric == "fpvr")
    assert fpvr.status == "skipped"
    assert "不按 0 计" in fpvr.reason
    assert result.exit_code == 0


def test_all_missing_metrics_are_skipped() -> None:
    result = _compare({"suite": "empty"}, {"suite": "empty"})

    assert result.exit_code == 0
    assert len(result.skipped) == 3


def test_extract_metrics_reads_nested_suites() -> None:
    report = {
        "suites": {
            "agent": {"metrics": {"fpvr": 0.8}},
            "retrieval": {"metrics": {"recall@5": 0.7, "mrr@10": 0.6}},
        }
    }

    flat = compare.extract_metrics(report)

    assert flat == {"fpvr": 0.8, "recall@5": 0.7, "mrr@10": 0.6}


def test_combined_report_compares_all_three_metrics() -> None:
    current = {
        "suites": {
            "agent": {"metrics": {"fpvr": 0.7}},
            "retrieval": {"metrics": {"recall@5": 0.7}},
            "observability": {"metrics": {"cost_per_run_cny": 0.02}},
        }
    }
    baseline = {
        "suites": {
            "agent": {"metrics": {"fpvr": 0.8}},
            "retrieval": {"metrics": {"recall@5": 0.8}},
            "observability": {"metrics": {"cost_per_run_cny": 0.01}},
        }
    }

    result = _compare(current, baseline)

    assert {item.metric for item in result.failures} == {
        "fpvr",
        "recall@5",
        "cost_per_run_cny",
    }
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# Validation and CLI
# ---------------------------------------------------------------------------


def test_invalid_threshold_is_rejected() -> None:
    with pytest.raises(compare.CompareError, match="fpvr_threshold"):
        compare.compare_reports(
            _report({}), _report({}), fpvr_threshold=1.5
        )


def test_as_dict_uses_custom_thresholds() -> None:
    result = compare.compare_reports(
        _report({"fpvr": 0.78}),
        _report({"fpvr": 0.80}),
        fpvr_threshold=0.05,
    )

    assert result.comparisons[0].status == "pass"
    assert result.as_dict()["thresholds"]["fpvr"] == 0.05


def test_to_markdown_contains_all_rows() -> None:
    result = _compare(_report({"fpvr": 0.77}), _report(BASE_METRICS))
    markdown = result.to_markdown()

    assert "FPVR" in markdown
    assert "Recall@5" in markdown
    assert "单次成本" in markdown
    assert "FAIL" in markdown
    assert "失败原因" in markdown


def test_main_writes_json_and_returns_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    baseline = tmp_path / "baseline.json"
    current = tmp_path / "current.json"
    baseline.write_text(json.dumps(_report(BASE_METRICS)), encoding="utf-8")
    current.write_text(
        json.dumps(_report({"fpvr": 0.70, "recall@5": 0.80, "cost_per_run_cny": 0.01})),
        encoding="utf-8",
    )
    json_path = tmp_path / "compare.json"

    code = compare.main(
        ["--baseline", str(baseline), "--current", str(current), "--json", str(json_path)]
    )

    assert code == 1
    output = capsys.readouterr().out
    assert "FAIL" in output
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["status"] == "fail"
    assert payload["failures"] == ["fpvr"]


def test_main_returns_two_for_missing_input(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = compare.main(
        ["--baseline", str(tmp_path / "missing.json"), "--current", str(tmp_path / "x")]
    )

    assert code == 2
    assert "对比失败" in capsys.readouterr().out


def test_main_returns_zero_when_clean(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    baseline = tmp_path / "baseline.json"
    current = tmp_path / "current.json"
    baseline.write_text(json.dumps(_report(BASE_METRICS)), encoding="utf-8")
    current.write_text(json.dumps(_report(BASE_METRICS)), encoding="utf-8")

    assert compare.main(["--baseline", str(baseline), "--current", str(current)]) == 0
    assert "PASS" in capsys.readouterr().out
