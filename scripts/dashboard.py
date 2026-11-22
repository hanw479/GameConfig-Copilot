"""Streamlit dashboard (S7-06): trace list / trace detail / metrics.

Run::

    pip install 'streamlit>=1.30,<2'
    streamlit run scripts/dashboard.py

Contract
--------
The module is import-safe without Streamlit: the UI import is guarded, and every
data-loading function below is a pure Python function tested in CI. When Streamlit
is missing, :func:`main` raises :class:`DashboardUnavailable` instead of failing at
import time — so ``pytest`` never needs the optional dependency.
Output
    Three pages over ``traces/traces.jsonl`` (or ``GCC_TRACE_PATH``) and the JSON
    reports in ``reports/``: trace list, trace detail (spans + attrs), metrics
    (retrieval / agent / ragas summaries) plus the baseline Markdown.
Failure semantics
    * a missing trace file is an empty dataset (fresh checkout), not an error;
    * malformed JSONL lines are skipped and listed in ``TraceDataset.skipped_lines``;
    * malformed report files are ignored by :func:`load_metrics` (the metrics page
      shows only reports that parsed).
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:  # optional dependency: CI only exercises the pure data functions
    import streamlit as st
except ImportError:  # pragma: no cover - exercised by test monkeypatch/CI
    st = None  # type: ignore[assignment]

__all__ = [
    "BASELINE_DOC",
    "BASELINE_JSON",
    "DEFAULT_REPORTS_DIR",
    "DEFAULT_TRACE_PATH",
    "PAGES",
    "DashboardError",
    "DashboardUnavailable",
    "TraceDataset",
    "TraceSummary",
    "load_baseline_doc",
    "load_baseline_metrics",
    "load_metrics",
    "load_trace_records",
    "main",
    "summarize_traces",
    "trace_detail",
]

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TRACE_PATH = REPO_ROOT / "traces" / "traces.jsonl"
DEFAULT_REPORTS_DIR = REPO_ROOT / "reports"
BASELINE_DOC = REPO_ROOT / "docs" / "reports" / "baseline.md"
BASELINE_JSON = REPO_ROOT / "docs" / "reports" / "baseline.json"
PAGES: tuple[str, ...] = ("轨迹列表", "轨迹详情", "指标")


class DashboardError(RuntimeError):
    """Raised for malformed trace/report input that cannot be rendered."""


class DashboardUnavailable(DashboardError):
    """Raised when Streamlit is not installed (data functions still work)."""


@dataclass(frozen=True)
class TraceDataset:
    """Parsed trace file plus the line numbers that were skipped."""

    path: Path
    records: tuple[dict[str, Any], ...]
    skipped_lines: tuple[int, ...] = ()
    exists: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "exists": self.exists,
            "records": len(self.records),
            "skipped_lines": list(self.skipped_lines),
        }


@dataclass(frozen=True)
class TraceSummary:
    """One row on the trace list page."""

    trace_id: str
    span_count: int
    error_count: int
    start_ts: str | None
    end_ts: str | None
    duration_ms: int
    names: tuple[str, ...]
    sessions: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "span_count": self.span_count,
            "error_count": self.error_count,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "duration_ms": self.duration_ms,
            "names": " → ".join(self.names),
            "sessions": ", ".join(self.sessions),
        }


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # Naive timestamps (hand-written fixtures) are treated as UTC so min/max
    # never mixes offset-aware and offset-naive datetimes.
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _duration_ms(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def load_trace_records(
    path: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> TraceDataset:
    """Load JSONL traces, honoring ``GCC_TRACE_PATH`` unless an explicit path is given."""

    if path is not None:
        target = Path(path)
    else:
        source = os.environ if env is None else env
        target = Path(source.get("GCC_TRACE_PATH", str(DEFAULT_TRACE_PATH)))
    if not target.exists():
        return TraceDataset(path=target, records=(), exists=False)
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise DashboardError(f"轨迹文件无法读取：{target}（{exc}）") from exc

    records: list[dict[str, Any]] = []
    skipped: list[int] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            raw = json.loads(stripped)
        except json.JSONDecodeError:
            skipped.append(line_number)
            continue
        if not isinstance(raw, Mapping):
            skipped.append(line_number)
            continue
        records.append(dict(raw))
    return TraceDataset(
        path=target,
        records=tuple(records),
        skipped_lines=tuple(skipped),
        exists=True,
    )


def summarize_traces(
    records: Sequence[Mapping[str, Any]],
) -> tuple[TraceSummary, ...]:
    """Group records by ``trace_id`` and compute span/error/duration summary rows."""

    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for position, record in enumerate(records, start=1):
        if not isinstance(record, Mapping):
            raise DashboardError(f"第 {position} 条轨迹记录不是 JSON 对象")
        trace_id = record.get("trace_id")
        if not isinstance(trace_id, str) or not trace_id.strip():
            raise DashboardError(f"第 {position} 条轨迹记录缺少 trace_id")
        grouped.setdefault(trace_id, []).append(record)

    summaries: list[TraceSummary] = []
    for trace_id, spans in grouped.items():
        starts: list[datetime] = []
        ends: list[datetime] = []
        durations: list[int] = []
        names: dict[str, None] = {}
        sessions: dict[str, None] = {}
        errors = 0
        for span in spans:
            name = span.get("name")
            if isinstance(name, str) and name:
                names.setdefault(name, None)
            if span.get("status") == "error":
                errors += 1
            duration = _duration_ms(span.get("duration_ms"))
            durations.append(duration)
            start = _parse_ts(span.get("ts"))
            if start is not None:
                starts.append(start)
                ends.append(start + timedelta(milliseconds=duration))
            attrs = span.get("attrs")
            if isinstance(attrs, Mapping):
                session_id = attrs.get("session_id")
                if isinstance(session_id, str) and session_id:
                    sessions.setdefault(session_id, None)
        if starts and ends:
            duration_ms = int((max(ends) - min(starts)).total_seconds() * 1000)
        else:
            duration_ms = max(durations, default=0)
        summaries.append(
            TraceSummary(
                trace_id=trace_id,
                span_count=len(spans),
                error_count=errors,
                start_ts=min(starts).isoformat() if starts else None,
                end_ts=max(ends).isoformat() if ends else None,
                duration_ms=duration_ms,
                names=tuple(names),
                sessions=tuple(sessions),
            )
        )
    return tuple(
        sorted(
            summaries,
            key=lambda item: (item.start_ts is None, item.start_ts or "", item.trace_id),
        )
    )


def trace_detail(
    records: Sequence[Mapping[str, Any]],
    trace_id: str,
) -> dict[str, Any] | None:
    """Return the ordered span list for one trace (``None`` when unknown)."""

    if not isinstance(trace_id, str) or not trace_id.strip():
        raise DashboardError("trace_id 必须是非空字符串")
    spans = [
        dict(record)
        for record in records
        if isinstance(record, Mapping) and record.get("trace_id") == trace_id
    ]
    if not spans:
        return None
    spans.sort(
        key=lambda span: (
            str(span.get("ts") or ""),
            str(span.get("span_id") or ""),
        )
    )
    summary = next(
        item for item in summarize_traces(records) if item.trace_id == trace_id
    )
    return {
        "summary": summary.as_dict(),
        "spans": [
            {
                "span_id": span.get("span_id"),
                "parent_id": span.get("parent_id"),
                "name": span.get("name"),
                "kind": span.get("kind", "span"),
                "ts": span.get("ts"),
                "duration_ms": _duration_ms(span.get("duration_ms")),
                "status": span.get("status", "ok"),
                "attrs": dict(span.get("attrs") or {})
                if isinstance(span.get("attrs"), Mapping)
                else {},
            }
            for span in spans
        ],
    }


def load_metrics(
    reports_dir: str | Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Scan ``reports/*.json`` for objects with a ``metrics`` map, keyed by suite."""

    directory = Path(reports_dir) if reports_dir is not None else DEFAULT_REPORTS_DIR
    if not directory.exists():
        return {}
    collected: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(raw, Mapping):
            continue
        metrics = raw.get("metrics")
        if not isinstance(metrics, Mapping) or not metrics:
            continue
        suite = raw.get("suite")
        key = suite.strip() if isinstance(suite, str) and suite.strip() else path.stem
        if key in collected:
            key = f"{key}:{path.stem}"
        collected[key] = {
            "suite": key,
            "path": str(path),
            "generated_at": raw.get("generated_at"),
            "metrics": dict(metrics),
            "status": raw.get("status"),
        }
    return collected


def load_baseline_doc(path: str | Path | None = None) -> str:
    """Return the baseline Markdown (empty string when not written yet)."""

    target = Path(path) if path is not None else BASELINE_DOC
    try:
        return target.read_text(encoding="utf-8")
    except OSError:
        return ""


def load_baseline_metrics(
    path: str | Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Read the committed baseline's nested suite metrics (``baseline:<suite>``).

    ``reports/`` is a gitignored runtime directory, so a fresh checkout has no
    metrics page data. The committed ``docs/reports/baseline.json`` fills that
    gap without pretending it is a fresh measurement (status stays ``partial``).
    """

    target = Path(path) if path is not None else BASELINE_JSON
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, Mapping):
        return {}
    collected: dict[str, dict[str, Any]] = {}
    suites = raw.get("suites")
    if isinstance(suites, Mapping):
        for suite, payload in suites.items():
            if not isinstance(payload, Mapping):
                continue
            metrics = payload.get("metrics")
            if not isinstance(metrics, Mapping) or not metrics:
                continue
            key = f"baseline:{suite}"
            collected[key] = {
                "suite": key,
                "path": str(target),
                "generated_at": payload.get("generated_at"),
                "metrics": dict(metrics),
                "status": raw.get("status"),
            }
        return collected
    metrics = raw.get("metrics")
    if isinstance(metrics, Mapping) and metrics:
        collected["baseline"] = {
            "suite": "baseline",
            "path": str(target),
            "generated_at": raw.get("generated_at"),
            "metrics": dict(metrics),
            "status": raw.get("status"),
        }
    return collected


def _require_streamlit() -> Any:
    if st is None:
        raise DashboardUnavailable(
            "Streamlit 未安装：pip install 'streamlit>=1.30,<2'；"
            "数据加载函数（load_trace_records/summarize_traces/trace_detail/load_metrics）"
            "仍可在无 UI 环境下单测。"
        )
    return st


def _render_trace_list(ui: Any) -> None:
    dataset = load_trace_records()
    summaries = summarize_traces(dataset.records)
    ui.title("轨迹列表")
    ui.caption(
        f"{dataset.path} · {len(dataset.records)} 条记录 · {len(summaries)} 个 trace"
        + (f" · 跳过 {len(dataset.skipped_lines)} 行" if dataset.skipped_lines else "")
    )
    if not summaries:
        ui.info("traces/ 下还没有轨迹；先跑一次 gcc ask / API 请求生成 JSONL。")
        return
    ui.dataframe([item.as_dict() for item in summaries], use_container_width=True)


def _render_trace_detail(ui: Any) -> None:
    dataset = load_trace_records()
    summaries = summarize_traces(dataset.records)
    ui.title("轨迹详情")
    if not summaries:
        ui.info("没有可打开的 trace。")
        return
    options = {f"{item.trace_id} · {item.span_count} spans": item.trace_id for item in summaries}
    selected = ui.selectbox("选择 trace", list(options))
    detail = trace_detail(dataset.records, options[selected])
    if detail is None:
        ui.warning("该 trace 已不存在。")
        return
    summary = detail["summary"]
    columns = ui.columns(4)
    columns[0].metric("span 数", summary["span_count"])
    columns[1].metric("错误 span", summary["error_count"])
    columns[2].metric("时长（ms）", summary["duration_ms"])
    columns[3].metric("session", summary["sessions"] or "—")
    ui.subheader("span 顺序")
    ui.dataframe(
        [
            {
                "ts": span["ts"],
                "name": span["name"],
                "status": span["status"],
                "duration_ms": span["duration_ms"],
                "parent_id": span["parent_id"],
            }
            for span in detail["spans"]
        ],
        use_container_width=True,
    )
    for span in detail["spans"]:
        with ui.expander(f"{span['name']} · {span['status']} · {span['span_id']}"):
            ui.json(span["attrs"])


def _render_metrics(ui: Any) -> None:
    ui.title("指标")
    reports = {**load_baseline_metrics(), **load_metrics()}
    if not reports:
        ui.warning("reports/ 与 docs/reports/baseline.json 都没有可解析的指标报告；先跑 gcc eval。")
    for suite, payload in reports.items():
        ui.subheader(suite)
        ui.caption(f"{payload['path']} · {payload.get('generated_at') or '未记录时间'}")
        ui.dataframe(
            [
                {"metric": name, "value": value}
                for name, value in payload["metrics"].items()
            ],
            use_container_width=True,
        )
    baseline = load_baseline_doc()
    if baseline:
        ui.subheader("基线报告（docs/reports/baseline.md）")
        ui.markdown(baseline)
    else:
        ui.info("docs/reports/baseline.md 尚未生成。")


def main() -> int:
    """Streamlit entry point; raises :class:`DashboardUnavailable` without Streamlit."""

    ui = _require_streamlit()
    ui.set_page_config(page_title="GameConfig Copilot · 评测与观测", layout="wide")
    ui.sidebar.title("GameConfig Copilot")
    ui.sidebar.caption("S7 评测与观测面板")
    page = ui.sidebar.radio("页面", PAGES)
    if page == PAGES[0]:
        _render_trace_list(ui)
    elif page == PAGES[1]:
        _render_trace_detail(ui)
    else:
        _render_metrics(ui)
    return 0


if __name__ == "__main__":  # pragma: no cover - Streamlit CLI glue
    try:
        main()
    except DashboardUnavailable as exc:
        print(f"面板未启动：{exc}")
        raise SystemExit(1) from exc
