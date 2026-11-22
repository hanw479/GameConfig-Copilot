"""Guard tests for the committed S7-08 baseline artifacts.

The Markdown/JSON baseline is a deliverable, so drift must be visible in CI:
the declared retrieval numbers must match the committed snapshot, and the
partial-baseline labels must stay explicit (BM25-only / mock generation pending).
"""

from __future__ import annotations

import json
from pathlib import Path

from gameconfig_copilot.evaluation import compare

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE_MD = REPO_ROOT / "docs" / "reports" / "baseline.md"
BASELINE_JSON = REPO_ROOT / "docs" / "reports" / "baseline.json"


def _baseline() -> dict:
    return json.loads(BASELINE_JSON.read_text(encoding="utf-8"))


def test_baseline_markdown_records_measured_retrieval_numbers() -> None:
    text = BASELINE_MD.read_text(encoding="utf-8")

    for number in ("0.775", "0.875", "0.7625", "547"):
        assert number in text
    assert "BM25-only" in text
    assert "mock" in text
    assert "待真实 LLM" in text


def test_baseline_json_matches_committed_retrieval_metrics() -> None:
    baseline = _baseline()
    retrieval = baseline["suites"]["retrieval"]["metrics"]

    assert baseline["suite"] == "combined"
    assert baseline["status"] == "partial"
    assert retrieval["recall@5"] == 0.775
    assert retrieval["recall@10"] == 0.875
    assert retrieval["mrr@10"] == 0.7625
    assert retrieval["ndcg@10"] == 0.7382
    assert baseline["suites"]["retrieval"]["retriever"]["dense_available"] is False
    assert baseline["pending"]["agent"]


def test_baseline_json_records_the_measured_agent_fpvr_and_reward_floors() -> None:
    baseline = _baseline()
    agent = baseline["suites"]["agent"]

    assert agent["metrics"]["fpvr"] == 0.913
    assert agent["denominators"] == {
        "total": 30,
        "expected_pass": 23,
        "expected_block": 7,
    }
    assert agent["metrics"]["post_repair_pass_rate"] == 1.0
    assert agent["reward_lower_bounds"]["status"] == "enforced"
    assert set(agent["reward_lower_bounds"]["constraints"]) == {
        "C_REWARD_GOLD_MIN_01",
        "C_REWARD_EXP_MIN_01",
    }
    assert agent["reward_lower_bounds"]["gold"] == {
        "max_level_10": 60,
        "max_level_20": 150,
        "max_level_30": 360,
    }


def test_default_compare_baseline_is_the_committed_snapshot() -> None:
    assert compare.DEFAULT_BASELINE_PATH == BASELINE_JSON
    assert compare.DEFAULT_BASELINE_PATH.exists()


def test_compare_against_itself_is_clean() -> None:
    baseline = _baseline()

    result = compare.compare_reports(
        baseline,
        baseline,
        baseline_path=str(BASELINE_JSON),
        current_path=str(BASELINE_JSON),
    )

    assert result.exit_code == 0
    assert result.failed is False
    # FPVR/cost are now recorded in the committed baseline, so nothing is skipped.
    assert result.skipped == ()
