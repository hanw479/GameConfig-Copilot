"""Unit tests for the S7-02 Ragas harness.

The real ragas package is optional and absent in CI; the tests cover the lazy
import guard, the pinned judge config and the injected-judge scoring seam with a
deterministic fake judge.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from gameconfig_copilot.evaluation import ragas_eval

FIXED_TIME = "2026-11-15T12:00:00+08:00"
BASE_SCORES = {
    "faithfulness": 0.8,
    "answer_relevancy": 0.6,
    "context_precision": 0.4,
    "context_recall": 0.2,
}


class FakeJudge:
    """Deterministic judge: metric-specific constants, records every call."""

    def __init__(self, scores: dict[str, float] | None = None) -> None:
        self.scores = dict(scores or BASE_SCORES)
        self.calls: list[tuple[str, str]] = []

    def score(self, metric: str, sample: Any) -> float:
        self.calls.append((metric, sample.sample_id))
        return self.scores[metric]


def _samples() -> list[ragas_eval.RagasSample]:
    return [
        ragas_eval.RagasSample(
            "Q1",
            "矿洞在哪里？",
            "废弃矿洞 MAP_MINE_03 位于荒原哨站以东。",
            ("wiki:world_overview#三、地点",),
            "MAP_MINE_03 在荒原哨站以东。",
        ),
        ragas_eval.RagasSample(
            "Q2",
            "铁匠叫什么？",
            "铁匠老陈。",
            ("wiki:npc_profiles#NPC_013",),
            None,
        ),
        ragas_eval.RagasSample(
            "Q3",
            "没有检索到依据。",
            "知识库未覆盖该问题。",
        ),
    ]


# ---------------------------------------------------------------------------
# Lazy import guard
# ---------------------------------------------------------------------------


def test_load_ragas_raises_unavailable_when_module_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _missing(name: str, package: str | None = None) -> Any:
        del package
        raise ModuleNotFoundError(f"No module named {name!r}", name=name)

    monkeypatch.setattr(ragas_eval.importlib, "import_module", _missing)

    with pytest.raises(ragas_eval.RagasUnavailable, match="pip install"):
        ragas_eval.load_ragas()


def test_run_without_judge_and_without_ragas_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _unavailable() -> Any:
        raise ragas_eval.RagasUnavailable("Ragas 未安装（测试）")

    monkeypatch.setattr(ragas_eval, "load_ragas", _unavailable)
    report_path = tmp_path / "ragas-baseline.json"

    with pytest.raises(ragas_eval.RagasUnavailable, match="未安装"):
        ragas_eval.run_ragas_eval(
            samples=_samples(),
            report_path=report_path,
            generated_at=FIXED_TIME,
        )

    assert not report_path.exists()


# ---------------------------------------------------------------------------
# Judge config (fixed version)
# ---------------------------------------------------------------------------


def test_judge_config_defaults_are_pinned() -> None:
    config = ragas_eval.JudgeConfig()

    assert config.model == ragas_eval.DEFAULT_JUDGE_MODEL
    assert config.version == ragas_eval.DEFAULT_JUDGE_VERSION
    assert config.model_id == "deepseek-chat@2026-11-01"
    assert config.as_dict()["pinned"] is True


@pytest.mark.parametrize("version", ["latest", "main", "HEAD", "stable", "*"])
def test_judge_config_rejects_floating_versions(version: str) -> None:
    with pytest.raises(ragas_eval.RagasEvalError, match="固定版本"):
        ragas_eval.JudgeConfig(version=version)


def test_judge_config_rejects_malformed_version() -> None:
    with pytest.raises(ragas_eval.RagasEvalError, match="固定版本"):
        ragas_eval.JudgeConfig(version="2026-11")


def test_judge_config_reads_env_overrides() -> None:
    config = ragas_eval.JudgeConfig.from_env(
        {
            ragas_eval.JUDGE_MODEL_ENV: "internal-qwen",
            ragas_eval.JUDGE_VERSION_ENV: "1.2.3",
            ragas_eval.JUDGE_TEMPERATURE_ENV: "0.2",
            ragas_eval.JUDGE_MAX_TOKENS_ENV: "2048",
        }
    )

    assert config.model_id == "internal-qwen@1.2.3"
    assert config.temperature == 0.2
    assert config.max_tokens == 2048


def test_judge_config_rejects_bad_env_numbers() -> None:
    with pytest.raises(ragas_eval.RagasEvalError, match="数字"):
        ragas_eval.JudgeConfig.from_env(
            {ragas_eval.JUDGE_TEMPERATURE_ENV: "warm"}
        )
    with pytest.raises(ragas_eval.RagasEvalError, match="整数"):
        ragas_eval.JudgeConfig.from_env(
            {ragas_eval.JUDGE_MAX_TOKENS_ENV: "many"}
        )


def test_judge_config_rejects_out_of_range_temperature() -> None:
    with pytest.raises(ragas_eval.RagasEvalError, match=r"\[0, 2\]"):
        ragas_eval.JudgeConfig(temperature=3.0)


# ---------------------------------------------------------------------------
# Scoring with a fake judge
# ---------------------------------------------------------------------------


def test_score_samples_uses_metric_applicability_for_denominators() -> None:
    scores = ragas_eval.score_samples(_samples(), FakeJudge())

    assert scores["metrics"] == {
        "faithfulness": 0.8,
        "answer_relevancy": 0.6,
        "context_precision": 0.4,
        "context_recall": 0.2,
    }
    # Q1 and Q2 have contexts; Q3 does not -> faithfulness denominator 2.
    assert scores["denominators"]["faithfulness"] == 2
    # answer_relevancy always applies.
    assert scores["denominators"]["answer_relevancy"] == 3
    # context_* need both contexts and ground truth -> only Q1.
    assert scores["denominators"]["context_precision"] == 1
    assert scores["denominators"]["context_recall"] == 1
    per_sample = {row["id"]: row for row in scores["per_sample"]}
    assert per_sample["Q3"]["faithfulness"] is None
    assert per_sample["Q2"]["context_recall"] is None
    assert per_sample["Q1"]["context_recall"] == 0.2


def test_score_samples_rejects_out_of_range_scores() -> None:
    judge = FakeJudge({"faithfulness": 1.4})

    with pytest.raises(ragas_eval.RagasEvalError, match=r"\[0, 1\]"):
        ragas_eval.score_samples(_samples(), judge)


def test_score_samples_rejects_non_numeric_scores() -> None:
    class BadJudge:
        def score(self, metric: str, sample: Any) -> Any:
            del metric, sample
            return "high"

    with pytest.raises(ragas_eval.RagasEvalError, match="数字"):
        ragas_eval.score_samples(_samples(), BadJudge())


def test_score_samples_rejects_duplicate_sample_ids() -> None:
    samples = [_samples()[0], _samples()[0]]

    with pytest.raises(ragas_eval.RagasEvalError, match="重复"):
        ragas_eval.score_samples(samples, FakeJudge())


def test_run_with_fake_judge_writes_marked_report(tmp_path: Path) -> None:
    report_path = tmp_path / "ragas-baseline.json"
    result = ragas_eval.run_ragas_eval(
        samples=_samples(),
        judge=FakeJudge(),
        report_path=report_path,
        generated_at=FIXED_TIME,
    )

    assert result.ok
    assert result.report is not None
    assert result.report["engine"] == "injected_judge"
    assert result.report["judge"]["source"] == "injected_judge"
    assert result.report["judge"]["model_id"] == "deepseek-chat@2026-11-01"
    assert result.report["denominators"] == {
        "samples": 3,
        "faithfulness": 2,
        "answer_relevancy": 3,
        "context_precision": 1,
        "context_recall": 1,
    }
    assert result.report["metrics"]["faithfulness"] == 0.8
    assert any("injected_judge" in note for note in result.report["notes"])
    written = json.loads(report_path.read_text(encoding="utf-8"))
    assert written["engine"] == "injected_judge"


def test_injected_judge_does_not_touch_default_report_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    default_path = tmp_path / "default-ragas.json"
    monkeypatch.setattr(ragas_eval, "REPORT_PATH", default_path)

    result = ragas_eval.run_ragas_eval(
        samples=_samples(),
        judge=FakeJudge(),
        generated_at=FIXED_TIME,
    )

    assert result.report_path is None
    assert not default_path.exists()
    assert "未写盘" in result.message


def test_run_rejects_empty_samples() -> None:
    with pytest.raises(ragas_eval.RagasEvalError, match="不能为空"):
        ragas_eval.run_ragas_eval(
            samples=[],
            judge=FakeJudge(),
            generated_at=FIXED_TIME,
        )


def test_report_records_configured_judge_identity(tmp_path: Path) -> None:
    config = ragas_eval.JudgeConfig(model="internal-qwen", version="1.0.0")

    result = ragas_eval.run_ragas_eval(
        samples=_samples(),
        judge=FakeJudge(),
        judge_config=config,
        report_path=tmp_path / "report.json",
        generated_at=FIXED_TIME,
    )

    assert result.report is not None
    assert result.report["judge"]["model_id"] == "internal-qwen@1.0.0"
