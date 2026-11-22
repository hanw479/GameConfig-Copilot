"""CLI ``gcc eval --suite agent`` tests: mock generator + missing-key failure.

The mock generator goes through the real ``run_agent_eval`` v2 harness, so these
tests cover the CLI branch, the report shape and the no-network path. The only
real-wiring test is the missing-key case: it must fail loudly, name
``GCC_LLM_API_KEY`` and never call the provider.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from gameconfig_copilot.cli import app
from gameconfig_copilot.cli import eval as cli_eval
from gameconfig_copilot.evaluation import retrieval_eval as rev

runner = CliRunner()


def _output(result: Any) -> str:
    combined = getattr(result, "output", "") or ""
    combined += getattr(result, "stderr", "") or ""
    return combined


def _mock_runtime(
    tmp_path: Path, payload: dict[str, Any]
) -> cli_eval.AgentEvalRuntime:
    def generate(request: str) -> dict[str, Any]:
        del request
        return dict(payload)

    return cli_eval.AgentEvalRuntime(
        generator=generate,
        report_path=tmp_path / "reports" / "fpvr-baseline.json",
    )


def test_cli_eval_agent_runs_mock_generator(
    tmp_path: Path,
    task_payload: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _mock_runtime(tmp_path, task_payload)
    monkeypatch.setattr(cli_eval, "build_agent_eval_runtime", lambda: runtime)

    result = runner.invoke(app, ["eval", "--suite", "agent"])

    assert result.exit_code == 0, _output(result)
    output = _output(result)
    for label in ("FPVR", "修复轮次", "拦截率", "P95", "成本"):
        assert label in output
    assert "fpvr-baseline.json" in output

    report = json.loads(runtime.report_path.read_text(encoding="utf-8"))
    assert report["suite"] == "agent"
    assert report["suite_version"] == "requests_v2"
    assert report["denominators"]["total"] == 30
    assert len(report["per_sample"]) == 30
    assert report["latency"]["samples"] == 30
    assert report["latency"]["p95_ms"] is not None
    # A mock generator exposes no LLM stats, so cost must stay explicitly absent.
    assert "cost" not in report
    assert "成本：n/a" in output


def test_cli_eval_agent_without_key_fails_loudly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Run outside the repository so the developer's real .env is not picked up.
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["eval", "--suite", "agent"])

    assert result.exit_code != 0
    output = _output(result)
    assert "需要 GCC_LLM_API_KEY" in output
    assert not (tmp_path / "reports" / "fpvr-baseline.json").exists()


def test_cli_eval_all_runs_retrieval_then_agent(
    tmp_path: Path,
    task_payload: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retrieval_report = {
        "suite": "retrieval",
        "status": "ok",
        "metrics": {"recall@5": 0.5},
        "qrels": {"distribution": {"count": 20, "relevant_labels": 34}},
    }
    fake_retrieval = rev.RunResult(
        status="ok",
        message="ok",
        report_path=Path("reports/retrieval-baseline.json"),
        report=retrieval_report,
    )
    monkeypatch.setattr(cli_eval, "run_retrieval_eval", lambda: fake_retrieval)
    runtime = _mock_runtime(tmp_path, task_payload)
    monkeypatch.setattr(cli_eval, "build_agent_eval_runtime", lambda: runtime)

    result = runner.invoke(app, ["eval", "--suite", "all"])

    assert result.exit_code == 0, _output(result)
    output = _output(result)
    assert output.index("recall@5") < output.index("FPVR")
    assert runtime.report_path.is_file()
