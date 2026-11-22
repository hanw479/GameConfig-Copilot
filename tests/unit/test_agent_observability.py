"""Unit tests for the S7-03 observability roll-up (report + trace aggregation).

Every number comes from a synthetic report / JSONL records, so no LLM, index or
real trace file is needed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from gameconfig_copilot.evaluation import agent_eval as aev

FIXED_TIME = "2026-11-15T12:00:00+08:00"


def _llm_record(
    trace_id: str,
    *,
    span_id: str,
    duration_ms: int,
    prompt: int = 100,
    completion: int = 20,
    cost: float | None = 0.01,
    name: str = "llm.chat",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    attrs: dict[str, Any] = {
        "provider": "deepseek",
        "model": "deepseek-chat",
        "prompt_tokens": prompt,
        "completion_tokens": completion,
    }
    if cost is not None:
        attrs["cost_cny"] = cost
    if extra:
        attrs.update(extra)
    return {
        "ts": "2026-11-15T12:00:00+08:00",
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_id": None,
        "name": name,
        "kind": "span",
        "duration_ms": duration_ms,
        "status": "ok",
        "attrs": attrs,
    }


def _trace_records() -> list[dict[str, Any]]:
    return [
        _llm_record("tr_a", span_id="sp_1", duration_ms=100, cost=0.01),
        _llm_record("tr_a", span_id="sp_2", duration_ms=200, cost=0.02, prompt=200),
        _llm_record("tr_b", span_id="sp_3", duration_ms=300, cost=0.03, completion=40),
        {
            "ts": "2026-11-15T12:00:01+08:00",
            "trace_id": "tr_a",
            "span_id": "sp_4",
            "parent_id": "sp_1",
            "name": "retrieval",
            "kind": "span",
            "duration_ms": 50,
            "status": "ok",
            "attrs": {},
        },
    ]


def _report() -> dict[str, Any]:
    per_sample: list[dict[str, Any]] = []
    for position in range(1, 21):
        adversarial = position > 15
        per_sample.append(
            {
                "id": f"G{position:02d}",
                "category": "adversarial" if adversarial else "normal",
                "actual_outcome": "correct_block" if adversarial else "passed",
                "duration_ms": position * 10,
            }
        )
    return {
        "suite": "agent",
        "generated_at": FIXED_TIME,
        "metrics": {
            "fpvr": 0.75,
            "post_repair_pass_rate": 0.8,
            "mean_repair_rounds": 0.5,
            "block_rate": 0.9,
            "error_code_hit_rate": 1.0,
        },
        "denominators": {"total": 20, "expected_pass": 15, "expected_block": 5},
        "failure_groups": {"validation_error": 3, "assertion_failure": 1},
        "repair": {"mean_rounds": 0.5, "histogram": {"0": 10, "1": 5}},
        "adversarial": {"block_rate": 1.0, "count": 5},
        "per_sample": per_sample,
    }


# ---------------------------------------------------------------------------
# Percentile
# ---------------------------------------------------------------------------


def test_percentile_uses_nearest_rank() -> None:
    values = [float(value) for value in range(10, 210, 10)]

    assert aev.percentile(values) == 190.0
    assert aev.percentile([10]) == 10.0
    assert aev.percentile([], 0.95) is None


def test_percentile_rejects_invalid_quantile() -> None:
    with pytest.raises(aev.AgentEvalError, match="quantile"):
        aev.percentile([1.0], 0)
    with pytest.raises(aev.AgentEvalError, match="quantile"):
        aev.percentile([1.0], 1.5)


# ---------------------------------------------------------------------------
# Trace aggregation
# ---------------------------------------------------------------------------


def test_extract_llm_usage_normalizes_tokens_and_skips_other_spans() -> None:
    records = _trace_records()
    records.append({"trace_id": "tr_a", "name": "route", "duration_ms": 0, "attrs": {}})

    usage = aev.extract_llm_usage(records)

    assert len(usage) == 3
    assert usage[0].provider == "deepseek"
    assert usage[0].model == "deepseek-chat"
    assert usage[0].prompt_tokens == 100
    assert usage[0].completion_tokens == 20
    assert usage[0].total_tokens == 120
    assert usage[0].cost_cny == 0.01
    assert usage[2].total_tokens == 140


def test_extract_llm_usage_accepts_tokens_total_object() -> None:
    record = _llm_record("tr_a", span_id="sp_1", duration_ms=1)
    record["attrs"] = {
        "provider": "deepseek",
        "model": "deepseek-chat",
        "tokens": {"total": 999},
        "cost_cny": 0.0,
    }

    usage = aev.extract_llm_usage([record])

    assert usage[0].total_tokens == 999
    assert usage[0].prompt_tokens is None


def test_extract_llm_usage_keeps_missing_cost_as_none() -> None:
    record = _llm_record("tr_a", span_id="sp_1", duration_ms=1, cost=None)

    usage = aev.extract_llm_usage([record])

    assert usage[0].cost_cny is None


def test_summarize_traces_computes_cost_per_run_and_p95() -> None:
    summary = aev.summarize_traces(_trace_records())

    assert summary["records"] == 4
    assert summary["traces"] == 2
    assert summary["llm_calls"] == 3
    assert summary["llm_traces"] == 2
    assert summary["priced_calls"] == 3
    assert summary["unpriced_calls"] == 0
    assert summary["total_tokens"] == 120 + 220 + 140
    assert summary["total_cost_cny"] == 0.06
    assert summary["cost_per_run_cny"] == 0.03
    assert summary["cost_basis"] == "per_trace_with_llm"
    # tr_a spans 12:00:00.000–12:00:01.050 (the retrieval span starts 1s later),
    # tr_b is a single 300ms span -> end-to-end P95 = 1050ms.
    assert summary["p95_latency_ms"] == 1050.0
    assert summary["p95_span_ms"] == 300.0
    assert summary["p95_llm_latency_ms"] == 300.0
    assert summary["providers"] == {"deepseek": 3}
    assert summary["models"] == {"deepseek-chat": 3}


def test_summarize_traces_returns_none_cost_without_priced_calls() -> None:
    records = [
        _llm_record("tr_a", span_id="sp_1", duration_ms=1, cost=None),
    ]

    summary = aev.summarize_traces(records)

    assert summary["total_cost_cny"] is None
    assert summary["cost_per_run_cny"] is None
    assert summary["unpriced_calls"] == 1


# ---------------------------------------------------------------------------
# Report aggregation
# ---------------------------------------------------------------------------


def test_summarize_agent_report_keeps_denominators_and_failure_groups() -> None:
    summary = aev.summarize_agent_report(_report())

    assert summary["metrics"]["fpvr"] == 0.75
    assert summary["metrics"]["mean_repair_rounds"] == 0.5
    assert summary["metrics"]["dangerous_block_rate"] == 1.0
    assert summary["metrics"]["block_rate"] == 0.9
    assert summary["metrics"]["p95_latency_ms"] == 190.0
    assert summary["denominators"] == {
        "total": 20,
        "expected_pass": 15,
        "expected_block": 5,
        "dangerous_total": 5,
        "dangerous_blocked": 5,
        "latency_samples": 20,
        "priced_runs": 0,
    }
    assert summary["failure_groups"] == {
        "assertion_failure": 1,
        "validation_error": 3,
    }


def test_summarize_agent_report_falls_back_to_overall_block_rate() -> None:
    report = _report()
    report.pop("adversarial")

    summary = aev.summarize_agent_report(report)

    assert summary["metrics"]["dangerous_block_rate"] == 0.9
    assert any("adversarial" in note for note in summary["notes"])


def test_summarize_agent_report_requires_repair_metric() -> None:
    report = _report()
    report["metrics"].pop("mean_repair_rounds")
    report.pop("repair")

    with pytest.raises(aev.AgentEvalError, match="修复轮次"):
        aev.summarize_agent_report(report)


def test_summarize_agent_report_falls_back_to_trace_p95() -> None:
    report = _report()
    for sample in report["per_sample"]:
        sample.pop("duration_ms")

    summary = aev.summarize_agent_report(report, _trace_records())

    assert summary["metrics"]["p95_latency_ms"] == 1050.0
    assert summary["metrics"]["cost_per_run_cny"] == 0.03
    assert any("回退" in note for note in summary["notes"])


def test_summarize_agent_report_requires_fpvr() -> None:
    report = _report()
    report["metrics"].pop("fpvr")

    with pytest.raises(aev.AgentEvalError, match="fpvr"):
        aev.summarize_agent_report(report)


# ---------------------------------------------------------------------------
# Combined summary
# ---------------------------------------------------------------------------


def test_build_observability_summary_merges_report_and_traces() -> None:
    summary = aev.build_observability_summary(
        _report(),
        trace_records=_trace_records(),
        generated_at=FIXED_TIME,
        report_ref="reports/fpvr-baseline.json",
        trace_ref="traces/traces.jsonl",
    )

    assert summary["suite"] == "observability"
    assert summary["status"] == "ok"
    assert summary["generated_at"] == FIXED_TIME
    assert summary["metrics"]["fpvr"] == 0.75
    assert summary["metrics"]["p95_latency_ms"] == 190.0
    assert summary["metrics"]["cost_per_run_cny"] == 0.03
    assert summary["denominators"]["dangerous_blocked"] == 5
    assert summary["failure_groups"] == {
        "assertion_failure": 1,
        "validation_error": 3,
    }
    assert summary["sources"] == {
        "report": "reports/fpvr-baseline.json",
        "traces": "traces/traces.jsonl",
        "trace_records": 4,
    }
    assert any("nearest-rank" in note for note in summary["notes"])


def test_build_observability_summary_notes_missing_cost() -> None:
    summary = aev.build_observability_summary(
        _report(),
        trace_records=[_llm_record("tr_a", span_id="sp_1", duration_ms=1, cost=None)],
        generated_at=FIXED_TIME,
    )

    assert summary["metrics"]["cost_per_run_cny"] is None
    assert any("cost" in note for note in summary["notes"])


# ---------------------------------------------------------------------------
# File orchestration
# ---------------------------------------------------------------------------


def test_run_observability_summary_writes_json(tmp_path: Path) -> None:
    report_path = tmp_path / "fpvr-baseline.json"
    report_path.write_text(
        json.dumps(_report(), ensure_ascii=False), encoding="utf-8"
    )
    trace_path = tmp_path / "traces.jsonl"
    trace_path.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in _trace_records())
        + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "observability-summary.json"

    result = aev.run_observability_summary(
        report_path=report_path,
        trace_path=trace_path,
        output_path=output,
        generated_at=FIXED_TIME,
    )

    assert result.ok
    assert result.report is not None
    assert result.report["metrics"]["cost_per_run_cny"] == 0.03
    assert output.exists()
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written["suite"] == "observability"
    assert written["metrics"]["p95_latency_ms"] == 190.0
    assert "FPVR=75.00%" in result.message


def test_run_observability_summary_missing_report_raises(tmp_path: Path) -> None:
    with pytest.raises(aev.AgentEvalError, match="不存在"):
        aev.run_observability_summary(
            report_path=tmp_path / "missing.json",
            trace_path=tmp_path / "missing.jsonl",
            output_path=tmp_path / "out.json",
        )


def test_load_trace_records_rejects_corrupt_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "traces.jsonl"
    path.write_text('{"trace_id": "tr_a"}\nnot-json\n', encoding="utf-8")

    with pytest.raises(aev.AgentEvalError, match="第 2 行"):
        aev.load_trace_records(path)
