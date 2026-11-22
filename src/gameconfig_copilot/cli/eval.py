"""``gcc eval`` — evaluation entry point (S1-07 retrieval, S7 agent suite).

``--suite retrieval`` reads ``data/golden/qrels.json``, drives
``rag/retriever.py`` and writes ``reports/retrieval-baseline.json``.
``--suite agent`` builds the **real** ``LLMClient`` / ``TaskAgent`` / S4 validator
from :func:`gameconfig_copilot.config.get_settings`, runs the frozen 30-sample v2
golden suite and writes ``reports/fpvr-baseline.json``. ``--suite all`` runs
retrieval first and then the agent suite.

A missing ``GCC_LLM_API_KEY`` is reported as such and exits non-zero: the harness
never falls back to a fake generator, and no report is written for a suite that
did not actually run.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer
from rich.markup import escape
from rich.panel import Panel

from ..config import SettingsError
from ..evaluation.agent_eval import AgentEvalError, percentile, run_agent_eval
from ..evaluation.agent_eval import RunResult as AgentRunResult
from ..evaluation.agent_eval import write_report as write_agent_report
from ..evaluation.retrieval_eval import RetrievalEvalError, RunResult, run_retrieval_eval
from ._common import console, err_console

__all__ = [
    "AgentEvalRuntime",
    "build_agent_eval_runtime",
    "evaluate",
]

_SUITES = ("retrieval", "agent", "all")
# Distinct from the placeholder's 2: "dependency not ready yet" is not "not implemented".
_EXIT_RETRIEVER_UNAVAILABLE = 3
_EXIT_EVAL_ERROR = 1
# Configuration preconditions (missing key / unknown provider) are usage errors.
_EXIT_CONFIG = 2


# ---------------------------------------------------------------------------
# Retrieval suite (S1-07)
# ---------------------------------------------------------------------------


def _report_retrieval_unavailable(result: RunResult) -> None:
    err_console.print(
        Panel(
            f"{result.message}\n\n"
            "[dim]检索侧就绪后重跑：gcc eval --suite retrieval[/dim]\n"
            "[dim]本次不写 reports/retrieval-baseline.json：没有真实数字的基线不应落盘。[/dim]",
            title="gcc eval · 检索基线未跑",
            border_style="yellow",
            padding=(0, 1),
        )
    )
    raise typer.Exit(code=_EXIT_RETRIEVER_UNAVAILABLE)


def _report_retrieval_metrics(result: RunResult) -> None:
    report = result.report or {}
    metrics = report.get("metrics", {})
    distribution = report.get("qrels", {}).get("distribution", {})
    lines = [
        f"query 数：{distribution.get('count', '?')}"
        f" · 相关标注：{distribution.get('relevant_labels', '?')}"
    ]
    for name in (
        "recall@1",
        "recall@5",
        "recall@10",
        "mrr@10",
        "ndcg@10",
        "source_type_precision@10",
    ):
        if name in metrics:
            lines.append(f"{name:<26} {metrics[name]:.4f}")
    lines.append(f"\n报告：{result.report_path}")
    console.print(
        Panel(
            "\n".join(lines),
            title="gcc eval · 检索基线（S1-07）",
            border_style="green",
            padding=(0, 1),
        )
    )


# ---------------------------------------------------------------------------
# Agent suite (S7): production wiring + report formatting
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentEvalRuntime:
    """Everything one agent-suite run consumes.

    Production builds this from settings in :func:`build_agent_eval_runtime`;
    tests inject a mock generator and a ``tmp_path`` report path so no real API
    call and no repository write happen.
    """

    generator: Callable[[str], Any]
    validator: Callable[[Any], Any] | None = None
    repairer: Callable[..., Any] | None = None
    stats: Callable[[], Mapping[str, Any]] | None = None
    report_path: Path | None = None
    notes: tuple[str, ...] = ()
    close: Callable[[], None] | None = None


def _gather_context(tools: Any, request: str) -> tuple[list[Any], list[Any]]:
    """Mirror the graph's ``gather_context`` node (wiki + tasks + constraints)."""

    world = tools.search_wiki(request, top_k=4)
    similar = tools.find_similar_tasks(request, top_k=3)
    constraints = tools.list_constraints(query=request, top_k=8)
    evidence: list[Any] = []
    seen: set[str] = set()
    for item in [*world, *similar]:
        if item.citation in seen:
            continue
        seen.add(item.citation)
        evidence.append(item)
    return evidence, constraints


def _repair_report(failure_context: Mapping[str, Any]) -> dict[str, Any]:
    """Turn the harness failure context into the report ``TaskAgent.repair`` wants.

    Validation errors already carry ``severity="error"``; assertion-only failures
    become one synthetic error per message so the repair prompt is never blind.
    """

    errors = [
        dict(item) for item in failure_context.get("errors") or () if isinstance(item, Mapping)
    ]
    if not errors:
        errors = [
            {
                "code": "E_GOLDEN_ASSERTION",
                "severity": "error",
                "source": "golden_assertion",
                "field": "assertions",
                "expected": "无失败断言",
                "actual": list(failure_context.get("failed_assertions") or []),
                "suggestion": "按 message 重新生成完整草案；本轮没有可定位的字段级错误",
                "message": f"黄金字段断言未满足：{assertion}",
            }
            for assertion in failure_context.get("failed_assertions") or ()
        ]
    return {"ok": False, "errors": errors, "warnings": []}


def _stats_delta(
    llm: Any,
    baseline: Mapping[str, Any],
) -> Callable[[], dict[str, Any]]:
    """Return a callable with **this run's** LLM calls/tokens/cost, not lifetime."""

    def snapshot() -> dict[str, Any]:
        current = dict(llm.snapshot_stats())
        delta: dict[str, Any] = {}
        for key, value in current.items():
            before = baseline.get(key)
            if isinstance(value, (int, float)) and isinstance(before, (int, float)):
                delta[key] = value - before
            else:
                delta[key] = value
        return delta

    return snapshot


def build_agent_eval_runtime(*, report_path: Path | None = None) -> AgentEvalRuntime:
    """Wire the real generator/validator for ``gcc eval --suite agent``.

    Raises
    ------
    SettingsError
        Missing ``GCC_LLM_API_KEY`` (the CLI turns this into a distinct exit).
    AgentEvalError
        The retrieval index cannot be opened, so generation would silently lose
        its constraints/evidence context.
    """

    from ..agents.context_builder import ContextBuilder
    from ..agents.task_agent import TaskAgent
    from ..agents.tools import ReadOnlyTools
    from ..config import get_settings
    from ..evaluation.retrieval_eval import load_retriever
    from ..llm import create_client
    from ..mcp_server.writer import Writer

    settings = get_settings()
    llm = create_client(settings)
    retriever, reason = load_retriever()
    if retriever is None:
        raise AgentEvalError(
            "检索索引不可用，生成将失去约束/证据上下文："
            f"{reason or '未知原因'}；请先运行 gcc ingest 或 gcc eval --suite retrieval"
        )

    tools = ReadOnlyTools(retriever)
    writer = Writer()
    agent = TaskAgent(
        llm,
        context_builder=ContextBuilder(),
        existing_ids_provider=tools.existing_task_ids,
    )
    baseline = dict(llm.snapshot_stats())

    def generate(request: str) -> Any:
        evidence, constraints = _gather_context(tools, request)
        return agent.generate(request, evidence=evidence, constraints=constraints)

    def validate_draft(draft: Any) -> Any:
        """The MCP ``validate_draft`` check: YAML rules + repo snapshot + schema."""

        return writer.validate(draft)

    def repair(golden: Any, current_raw: Any, failure_context: Mapping[str, Any]) -> Any:
        draft = getattr(current_raw, "draft", current_raw)
        evidence, constraints = _gather_context(tools, golden.request)
        return agent.repair(
            golden.request,
            draft,
            _repair_report(failure_context),
            evidence=evidence,
            constraints=constraints,
        )

    notes = (
        f"生成：{settings.llm_provider}/{settings.llm_model} · TaskAgent + 检索上下文",
        "校验：S4 规则引擎（data/constraints）+ data/tasks 快照",
        "修复：TaskAgent.repair，最多 2 轮",
    )
    return AgentEvalRuntime(
        generator=generate,
        validator=validate_draft,
        repairer=repair,
        stats=_stats_delta(llm, baseline),
        report_path=report_path,
        notes=notes,
        close=llm.close,
    )


def _fmt_rate(value: Any) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{float(value):.2%}"
    return "n/a"


def _fmt_number(value: Any, *, digits: int = 1, suffix: str = "") -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{float(value):,.{digits}f}{suffix}"
    return "n/a"


def _augment_report(result: AgentRunResult, usage: Mapping[str, Any] | None) -> None:
    """Append latency/cost to the report, then rewrite it once."""

    report = result.report
    if not isinstance(report, dict):
        return
    per_sample = report.get("per_sample") or []
    durations = [
        int(sample.get("duration_ms") or 0) for sample in per_sample if isinstance(sample, Mapping)
    ]
    report["latency"] = {
        "samples": len(durations),
        "p50_ms": percentile(durations, 0.5),
        "p95_ms": percentile(durations, 0.95),
        "mean_ms": round(sum(durations) / len(durations), 1) if durations else None,
        "max_ms": max(durations) if durations else None,
    }
    if usage is not None:
        cost = float(usage.get("cost_cny") or 0.0)
        runs = len(per_sample) or 1
        report["cost"] = {
            "llm_calls": usage.get("requests", 0),
            "retries": usage.get("retries", 0),
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
            "total_cost_cny": round(cost, 6),
            "cost_per_run_cny": round(cost / runs, 6),
        }
    write_agent_report(report, result.report_path)


def _report_agent_metrics(result: AgentRunResult, notes: Sequence[str]) -> None:
    report = result.report or {}
    metrics = report.get("metrics", {})
    denominators = report.get("denominators", {})
    repair = report.get("repair", {})
    adversarial = report.get("adversarial", {})
    latency = report.get("latency", {})
    cost = report.get("cost")
    lines = [
        f"样本：{denominators.get('total', '?')}"
        f"（期望通过 {denominators.get('expected_pass', '?')}"
        f" / 期望拦截 {denominators.get('expected_block', '?')}）",
        f"FPVR：{_fmt_rate(metrics.get('fpvr'))}"
        f" · 修复后通过率：{_fmt_rate(metrics.get('post_repair_pass_rate'))}",
        "修复轮次："
        f"需修复 {repair.get('needed', '?')}"
        f" · 已尝试 {repair.get('attempted', '?')}"
        f" · 已修复 {repair.get('repaired', '?')}"
        f" · 失败 {repair.get('failed', '?')}"
        f" · 平均 {_fmt_number(repair.get('mean_rounds'), digits=2)}",
        f"拦截率：{_fmt_rate(metrics.get('block_rate'))}"
        f" · 对抗子集：{_fmt_rate(adversarial.get('block_rate'))}",
        "P95 延迟："
        f"{_fmt_number(latency.get('p95_ms'), digits=0, suffix=' ms')}"
        f" · 平均 {_fmt_number(latency.get('mean_ms'), digits=0, suffix=' ms')}",
    ]
    if isinstance(cost, Mapping):
        lines.append(
            "成本："
            f"¥{_fmt_number(cost.get('total_cost_cny'), digits=4)}"
            f" · 单条 ¥{_fmt_number(cost.get('cost_per_run_cny'), digits=4)}"
            f" · {_fmt_number(cost.get('total_tokens'), digits=0)} tokens"
            f" · {_fmt_number(cost.get('llm_calls'), digits=0)} 次调用"
        )
    else:
        lines.append("成本：n/a（生成器未暴露统计）")
    lines.append(f"\n报告：{result.report_path}")
    for note in notes:
        lines.append(f"[dim]{escape(note)}[/dim]")
    console.print(
        Panel(
            "\n".join(lines),
            title="gcc eval · Agent 评测（S7）",
            border_style="green",
            padding=(0, 1),
        )
    )


def _run_agent_suite() -> None:
    """Run the v2 suite; exits non-zero instead of fabricating a baseline."""

    runtime: AgentEvalRuntime | None = None
    try:
        try:
            runtime = build_agent_eval_runtime()
        except SettingsError as exc:
            err_console.print(
                Panel(
                    "Agent 评测需要真实 LLM 生成，但当前环境没有可用的凭据。\n\n"
                    "需要 GCC_LLM_API_KEY：设置进程环境变量，或写入本地 .env"
                    "（.env 已被忽略）。\n\n"
                    f"[dim]{escape(str(exc))}[/dim]\n"
                    "[dim]未调用任何生成接口，也未写 reports/fpvr-baseline.json。[/dim]",
                    title="gcc eval · Agent 评测缺少凭据",
                    border_style="yellow",
                    padding=(0, 1),
                )
            )
            raise typer.Exit(code=_EXIT_CONFIG) from exc

        try:
            result = run_agent_eval(
                suite="v2",
                generator=runtime.generator,
                validator=runtime.validator,
                repairer=runtime.repairer,
                report_path=runtime.report_path,
            )
        except AgentEvalError as exc:
            err_console.print(f"[red]Agent 评测失败：[/red]{escape(str(exc))}")
            raise typer.Exit(code=_EXIT_EVAL_ERROR) from exc

        if not result.ok:
            err_console.print(
                Panel(
                    f"{escape(result.message)}\n\n"
                    "[dim]本次不写 reports/fpvr-baseline.json："
                    "没有真实生成的基线不应落盘。[/dim]",
                    title="gcc eval · Agent 基线未跑",
                    border_style="yellow",
                    padding=(0, 1),
                )
            )
            raise typer.Exit(code=_EXIT_EVAL_ERROR)

        usage: Mapping[str, Any] | None = None
        if runtime.stats is not None:
            try:
                usage = runtime.stats()
            except Exception as exc:  # noqa: BLE001 - reporting must not fail the run
                err_console.print(f"[yellow]成本统计不可用：[/yellow]{escape(type(exc).__name__)}")
        _augment_report(result, usage)
        _report_agent_metrics(result, runtime.notes)
    finally:
        if runtime is not None and runtime.close is not None:
            # Closing a client must never mask the evaluation result.
            with contextlib.suppress(Exception):
                runtime.close()


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


def evaluate(
    suite: str = typer.Option(
        "all",
        "--suite",
        case_sensitive=False,
        help="评测套件：retrieval | agent | all。",
    ),
) -> None:
    """运行评测套件（S7 交付）。"""

    normalized = suite.strip().lower()
    if normalized not in _SUITES:
        raise typer.BadParameter(f"未知评测套件：{suite}（可选 {' | '.join(_SUITES)}）")

    if normalized in {"retrieval", "all"}:
        try:
            result = run_retrieval_eval()
        except RetrievalEvalError as exc:
            err_console.print(f"[red]检索评测失败：[/red]{exc}")
            raise typer.Exit(code=_EXIT_EVAL_ERROR) from exc
        if not result.ok:
            _report_retrieval_unavailable(result)
        _report_retrieval_metrics(result)
        if normalized == "retrieval":
            return

    _run_agent_suite()
