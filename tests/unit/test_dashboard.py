"""Unit tests for the S7-06 dashboard data layer (Streamlit itself is optional)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_PATH = REPO_ROOT / "scripts" / "dashboard.py"


def _load_dashboard() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gcc_dashboard", DASHBOARD_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def dashboard() -> ModuleType:
    return _load_dashboard()


def _record(
    trace_id: str,
    name: str,
    *,
    ts: str,
    duration_ms: int = 10,
    status: str = "ok",
    attrs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "ts": ts,
        "trace_id": trace_id,
        "span_id": f"sp_{name}",
        "parent_id": None,
        "name": name,
        "kind": "span",
        "duration_ms": duration_ms,
        "status": status,
        "attrs": attrs or {},
    }


def test_module_imports_without_streamlit(dashboard: ModuleType) -> None:
    assert dashboard is not None
    assert hasattr(dashboard, "load_trace_records")
    assert hasattr(dashboard, "main")


def test_missing_trace_file_is_an_empty_dataset(
    dashboard: ModuleType, tmp_path: Path
) -> None:
    dataset = dashboard.load_trace_records(tmp_path / "missing.jsonl")

    assert dataset.exists is False
    assert dataset.records == ()
    assert dataset.skipped_lines == ()


def test_load_trace_records_skips_malformed_lines(
    dashboard: ModuleType, tmp_path: Path
) -> None:
    path = tmp_path / "traces.jsonl"
    lines = [
        json.dumps(_record("tr_1", "route", ts="2026-11-15T10:00:00+08:00")),
        "not-json",
        json.dumps(["not", "an", "object"]),
        json.dumps(_record("tr_1", "llm.chat", ts="2026-11-15T10:00:01+08:00")),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    dataset = dashboard.load_trace_records(path)

    assert dataset.exists is True
    assert len(dataset.records) == 2
    assert dataset.skipped_lines == (2, 3)


def test_load_trace_records_honors_env_path(
    dashboard: ModuleType, tmp_path: Path
) -> None:
    path = tmp_path / "env-traces.jsonl"
    path.write_text(
        json.dumps(_record("tr_env", "route", ts="2026-11-15T10:00:00+08:00")),
        encoding="utf-8",
    )

    dataset = dashboard.load_trace_records(env={"GCC_TRACE_PATH": str(path)})

    assert dataset.path == path
    assert len(dataset.records) == 1


def test_summarize_traces_groups_and_measures_duration(dashboard: ModuleType) -> None:
    records = [
        _record(
            "tr_1",
            "route",
            ts="2026-11-15T10:00:00+08:00",
            duration_ms=10,
            attrs={"session_id": "sess_1"},
        ),
        _record(
            "tr_1",
            "retrieval",
            ts="2026-11-15T10:00:00.100+08:00",
            duration_ms=100,
            status="error",
        ),
        _record("tr_2", "route", ts="2026-11-15T10:01:00+08:00", duration_ms=5),
    ]

    summaries = dashboard.summarize_traces(records)
    by_id = {summary.trace_id: summary for summary in summaries}

    assert set(by_id) == {"tr_1", "tr_2"}
    assert by_id["tr_1"].span_count == 2
    assert by_id["tr_1"].error_count == 1
    assert by_id["tr_1"].duration_ms == 200
    assert by_id["tr_1"].names == ("route", "retrieval")
    assert by_id["tr_1"].sessions == ("sess_1",)
    assert by_id["tr_2"].span_count == 1
    assert summaries[0].trace_id == "tr_1"


def test_summarize_traces_rejects_missing_trace_id(dashboard: ModuleType) -> None:
    with pytest.raises(dashboard.DashboardError, match="trace_id"):
        dashboard.summarize_traces([{"name": "route", "attrs": {}}])


def test_trace_detail_returns_ordered_spans(dashboard: ModuleType) -> None:
    records = [
        _record(
            "tr_1",
            "retrieval",
            ts="2026-11-15T10:00:01+08:00",
            attrs={"top_k": 5},
        ),
        _record("tr_1", "route", ts="2026-11-15T10:00:00+08:00"),
        _record("tr_2", "route", ts="2026-11-15T10:02:00+08:00"),
    ]

    detail = dashboard.trace_detail(records, "tr_1")

    assert detail is not None
    assert detail["summary"]["span_count"] == 2
    assert [span["name"] for span in detail["spans"]] == ["route", "retrieval"]
    assert detail["spans"][1]["attrs"] == {"top_k": 5}


def test_trace_detail_unknown_trace_returns_none(dashboard: ModuleType) -> None:
    assert dashboard.trace_detail([], "tr_missing") is None


def test_load_metrics_scans_parseable_reports(
    dashboard: ModuleType, tmp_path: Path
) -> None:
    (tmp_path / "retrieval-baseline.json").write_text(
        json.dumps(
            {
                "suite": "retrieval",
                "generated_at": "2026-11-15T12:00:00+08:00",
                "status": "ok",
                "metrics": {"recall@5": 0.775, "mrr@10": 0.7625},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "fpvr-baseline.json").write_text(
        json.dumps({"suite": "agent", "metrics": {"fpvr": 0.8}}), encoding="utf-8"
    )
    (tmp_path / "broken.json").write_text("{not-json", encoding="utf-8")
    (tmp_path / "no-metrics.json").write_text(
        json.dumps({"suite": "empty"}), encoding="utf-8"
    )

    metrics = dashboard.load_metrics(tmp_path)

    assert set(metrics) == {"agent", "retrieval"}
    assert metrics["retrieval"]["metrics"]["recall@5"] == 0.775
    assert metrics["agent"]["metrics"]["fpvr"] == 0.8


def test_load_metrics_missing_directory_is_empty(
    dashboard: ModuleType, tmp_path: Path
) -> None:
    assert dashboard.load_metrics(tmp_path / "missing") == {}


def test_load_baseline_doc_reads_markdown(dashboard: ModuleType, tmp_path: Path) -> None:
    path = tmp_path / "baseline.md"
    path.write_text("# 基线\nRecall@5 0.775\n", encoding="utf-8")

    assert "Recall@5 0.775" in dashboard.load_baseline_doc(path)
    assert dashboard.load_baseline_doc(tmp_path / "missing.md") == ""


def test_load_baseline_metrics_reads_nested_suites(
    dashboard: ModuleType, tmp_path: Path
) -> None:
    path = tmp_path / "baseline.json"
    path.write_text(
        json.dumps(
            {
                "suite": "combined",
                "status": "partial",
                "suites": {
                    "retrieval": {
                        "generated_at": "2026-11-15T12:00:00+08:00",
                        "metrics": {"recall@5": 0.775, "mrr@10": 0.7625},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    metrics = dashboard.load_baseline_metrics(path)

    assert set(metrics) == {"baseline:retrieval"}
    assert metrics["baseline:retrieval"]["status"] == "partial"
    assert metrics["baseline:retrieval"]["metrics"]["recall@5"] == 0.775


def test_committed_baseline_metrics_load(dashboard: ModuleType) -> None:
    metrics = dashboard.load_baseline_metrics()

    assert metrics["baseline:retrieval"]["metrics"]["recall@5"] == 0.775
    assert metrics["baseline:retrieval"]["status"] == "partial"


def test_main_raises_dashboard_unavailable_without_streamlit(
    dashboard: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dashboard, "st", None)

    with pytest.raises(dashboard.DashboardUnavailable, match="streamlit"):
        dashboard.main()
