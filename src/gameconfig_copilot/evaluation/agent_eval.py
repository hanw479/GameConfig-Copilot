"""Agent-level evaluation harness (S3-05 baseline, extended for S7-01).

Metric
------
**FPVR** (First-Pass Validation Rate) = 首次生成即通过「Schema + 校验器 + 黄金字段
断言」的样本数 / 期望通过的样本数。修复节点（S4-05）的轮次单独统计：
``repair_rounds`` 为首次失败后实际发生的修复轮数（0 表示首次即通过）。
期望被拦截的边界样本不进 FPVR 分母，另算 ``block_rate``。

S7-01 adds the v2 golden set (30 samples: normal 20 / boundary 5 / adversarial 5,
two of them prompt injection). Adversarial samples are judged by **error-code
hits**: every code in ``expected.expected_error_codes`` must appear in the
validation report, otherwise the sample is ``blocked_unexpected_code`` (or
``schema_error`` when the draft never reaches the validator). Metrics are
reported both overall and per category (``subsets``).

Contract
--------
Input
    ``data/golden/requests_v1.jsonl`` or ``data/golden/requests_v2.jsonl``（归 E）
    加上注入的生成器。生成器是任意
    ``Callable[[str], Any]``，或带 ``generate(request)`` 的 A ``TaskAgent`` 实例；
    返回 :class:`TaskDraft` / 可 ``model_validate`` 的 mapping / ``DraftResult``
    （取 ``.draft``）/ 带 ``model_dump()`` 的对象。A 的 ``agents/task_agent.py``
    就绪后用 :func:`load_generator` 自动发现，或直接传入实例/适配函数。
    可选 ``validator``（``Callable[[TaskDraft], ValidationReport | Mapping]``）把
    S4 业务规则接入，得到业务级 FPVR；不传则按 S3-05 口径只做 Schema 校验。
Output
    ``reports/fpvr-baseline.json`` —— 指标、分母、修复轮次直方图、失败分组与
    逐样本明细（期望/实际结果、错误码、未满足的字段断言、耗时）。
Failure semantics
    * 黄金集缺失 / 某行非法 JSON / 字段缺失 / ID 重复 -> :class:`AgentEvalError`
      指明行号或字段。
    * 未注入生成器且 A 的模块尚不可用 -> ``RunResult.status ==
      "generator_unavailable"``，**不写报告**：没有真实生成的基线不落盘。
    * 单条生成失败 -> 记为 ``generation_error`` 样本，不中断整轮评测。
    * 校验器/修复器自身抛错 -> :class:`AgentEvalError` 指明样本 ID；这是 harness
      的 bug，不是「样本失败」，不能悄悄计入 FPVR。

Notes
-----
默认模式是 ``schema_only``（S3-05）：边界拦截样本需要 S4 业务校验器才能判定，
报告会在 ``notes`` 里显式标注。录制回放时把缓存好的生成结果包成 callable 注入
即可，整个评测不依赖真实 LLM / API key。
"""

from __future__ import annotations

import importlib
import json
import math
import re
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from ..schemas.report import ErrorCode
from ..schemas.task import TaskDraft, TaskType

__all__ = [
    "ASSERTION_POLICY",
    "DEFAULT_MAX_REPAIR_ROUNDS",
    "DEFAULT_SUMMARY_PATH",
    "DEFAULT_TRACE_PATH",
    "GOLDEN_SUITES",
    "LEVEL_BANDS",
    "P95_QUANTILE",
    "PENDING_CONSTRAINTS",
    "REPORT_PATH",
    "REQUESTS_PATH",
    "REQUESTS_V2_PATH",
    "SUITE_VERSION",
    "SUITE_VERSION_V2",
    "V2_EXPECTED_COUNTS",
    "V2_REQUIRED_INJECTIONS",
    "V2_TOTAL",
    "AgentEvalError",
    "AssertionFailure",
    "ExpectedOutcome",
    "GoldenRequest",
    "LlmUsage",
    "RunResult",
    "SampleResult",
    "build_observability_summary",
    "check_advisory_assertions",
    "check_assertion_failures",
    "check_assertions",
    "compute_fpvr",
    "compute_metrics",
    "compute_subset_metrics",
    "draft_references",
    "evaluate_sample",
    "extract_llm_usage",
    "load_agent_report",
    "load_generator",
    "load_requests",
    "load_requests_v2",
    "load_trace_records",
    "percentile",
    "run_agent_eval",
    "run_observability_summary",
    "summarize_agent_report",
    "summarize_requests",
    "summarize_traces",
    "validate_v2_distribution",
    "write_report",
]

DEFAULT_MAX_REPAIR_ROUNDS = 2
SUITE_VERSION = "requests_v1"
SUITE_VERSION_V2 = "requests_v2"

REPO_ROOT = Path(__file__).resolve().parents[3]
REQUESTS_PATH = REPO_ROOT / "data" / "golden" / "requests_v1.jsonl"
REQUESTS_V2_PATH = REPO_ROOT / "data" / "golden" / "requests_v2.jsonl"
REPORT_PATH = REPO_ROOT / "reports" / "fpvr-baseline.json"
#: S7-03 observability roll-up (FPVR + repair + danger block + P95 + cost).
DEFAULT_SUMMARY_PATH = REPO_ROOT / "reports" / "observability-summary.json"
#: Default JSONL trace file (same path the tracer writes unless GCC_TRACE_PATH).
DEFAULT_TRACE_PATH = REPO_ROOT / "traces" / "traces.jsonl"
P95_QUANTILE = 0.95

#: Golden suites selectable through :func:`run_agent_eval` (``suite=``).
GOLDEN_SUITES: dict[str, Path] = {
    "v1": REQUESTS_PATH,
    "v2": REQUESTS_V2_PATH,
}

#: S7-01 frozen distribution; ``load_requests_v2`` refuses to drift from it.
V2_TOTAL = 30
V2_EXPECTED_COUNTS: dict[str, int] = {"normal": 20, "boundary": 5, "adversarial": 5}
V2_REQUIRED_INJECTIONS = 2

CATEGORIES = frozenset(V2_EXPECTED_COUNTS)
ATTACK_PROMPT_INJECTION = "prompt_injection"

#: Entity / task IDs named by a request (used only to judge adversarial intent).
_ENTITY_ID_RE = re.compile(r"\b(?:NPC|MAP|ITEM|DLG|FACTION)_[A-Z0-9_]+\b")
_TASK_ID_RE = re.compile(r"\bT_(?:MAIN|SIDE|DAILY)_\d{4}\b")

#: Only MAP_/T_ references have a validator rule behind them: objective areas
#: (C_AREA_EXISTS_01 / C_AREA_ENUM_01) and prerequisites (C_PREREQ_EXISTS_01).
#: NPC/ITEM mentions are still reported, but only as advisory notes.
_ENFORCEABLE_REFERENCE_PREFIXES = ("MAP_", "T_")

#: Why the golden field assertions are split into hard / advisory checks.
#: ``enforced`` means the harness can decide acceptance from a deterministic
#: rule; ``rewritten`` means the original exact-match assertion was replaced by
#: an executable equivalent; ``advisory`` notes are reported but never fail a
#: sample (the validator has no rule that could enforce them).
ASSERTION_POLICY: dict[str, list[dict[str, Any]]] = {
    "enforced": [
        {
            "assertion": "task_type",
            "rule": "TaskDraft.task_type 枚举（E_SCHEMA_002）",
            "reason": "类型是封闭枚举，可直接判定。",
        },
        {
            "assertion": "level_band / level_span",
            "rule": "主线 1-15 / 支线 5-20 / 日常 10-30，跨度 ≤ 6（任务设计规范）",
            "reason": "等级段与跨度是任务设计规范里的确定性硬约束。",
        },
        {
            "assertion": "reference_exists（仅 MAP_ / T_）",
            "rule": "C_AREA_EXISTS_01 / C_AREA_ENUM_01 / C_PREREQ_EXISTS_01",
            "reason": "只有目标区域和前置任务有 validator 引用规则，可按错误码复验。",
        },
        {
            "assertion": "gold_max / exp_max",
            "rule": "C_REWARD_GOLD_01 / C_REWARD_EXP_01（按 level_range.min 取档位上限）",
            "reason": "奖励上限是约束语料给出的奖励硬规则。",
        },
        {
            "assertion": "gold_min / exp_min",
            "rule": (
                "C_REWARD_GOLD_MIN_01 / C_REWARD_EXP_MIN_01"
                "（level_band_range，按 level_range.min 取档位下限）"
            ),
            "reason": (
                "下限约束已加入 data/constraints（下限=同档上限 30%），"
                "validator 可执行 E_REWARD_RANGE，因此恢复为硬断言。"
            ),
        },
    ],
    "rewritten": [
        {
            "assertion": "level_min / level_max 精确值",
            "rule": "改为 level_band + level_span",
            "reason": (
                "level_range.min/max 的精确取值没有对应约束；"
                "任务设计只限定所属等级段与跨度。"
            ),
        },
        {
            "assertion": "prerequisites 精确集合",
            "rule": "改为必需 T_ 引用存在 + validator 的前置存在/等级顺序检查",
            "reason": (
                "前置集合是策划意图，不是校验规则；"
                "能执行的是「引用的任务存在且等级不倒挂」。"
            ),
        },
        {
            "assertion": "required_references（NPC_ / ITEM_）",
            "rule": "改为 advisory",
            "reason": (
                "约束语料没有针对 giver/objective target 的 reference_exists，"
                "NPC/ITEM 引用无法被 validator 判定。"
            ),
        },
    ],
    "advisory": [
        {
            "assertion": "giver_npc_id 精确 NPC",
            "reason": "没有「某任务必须由某 NPC 发布」的约束；发布者是叙事选择，只做提示。",
        },
        {
            "assertion": "objective_count_between",
            "reason": "没有目标条数约束；目标是 1 条还是 4 条属于任务结构设计，validator 不判定。",
        },
        {
            "assertion": "prerequisites 精确集合 / 等级 min-max 精确值",
            "reason": "精确值用于诊断提示词是否理解需求，不作为可执行通过条件。",
        },
        {
            "assertion": "NPC-ITEM 引用缺失",
            "reason": "缺少对应约束；计入 advisory 与「待加约束」清单，不改变 FPVR。",
        },
    ],
}

#: Product expectations that surfaced as advisory only; adding these to
#: ``data/constraints`` is a product decision, not something the harness may
#: silently turn into a pass/fail rule. The reward floors used to live here and
#: are now enforced by ``C_REWARD_GOLD_MIN_01`` / ``C_REWARD_EXP_MIN_01``.
PENDING_CONSTRAINTS: tuple[dict[str, Any], ...] = (
    {
        "candidate_id": "C_OBJECTIVE_COUNT_01",
        "scope": "objectives",
        "rule": "按任务原型给目标条数区间（日常 1、交付 2-3、三段式 3）",
        "evidence": ["G05/G13/G14/G16/G22 目标条数与策划预期不符"],
        "decision": "目标条数属于关卡结构规范；若产品要求硬约束，需要新增 count 规则。",
    },
    {
        "candidate_id": "C_GIVER_ASSIGN_01",
        "scope": "giver.npc_id",
        "rule": "发布者归属表 / NPC 角色约束",
        "evidence": ["G12 giver=NPC_021 期望 NPC_034", "G24 giver=NPC_013 期望 NPC_007"],
        "decision": "需要 NPC 职责表才能判定；当前只能提示，不能自动判错。",
    },
)


#: Level bands frozen in ``data/wiki/quest_design_spec.md`` §一/§二.
LEVEL_BANDS: dict[str, tuple[int, int]] = {
    TaskType.MAIN.value: (1, 15),
    TaskType.SIDE.value: (5, 20),
    TaskType.DAILY.value: (10, 30),
}
MAX_LEVEL_SPAN = 6

_GENERATOR_MODULE = "gameconfig_copilot.agents.task_agent"
_GENERATOR_ENTRIES = ("generate_draft", "generate_task", "generate")
_GENERATOR_CLASSES = ("TaskAgent",)

_PASS_OUTCOMES = frozenset({"passed", "passed_after_repair"})
_KNOWN_ERROR_CODES = frozenset(code.value for code in ErrorCode)

_NOTES = (
    "FPVR = 首轮（修复轮次 0）同时通过 Schema、注入的校验器与全部黄金字段断言的"
    "期望通过样本数 / 期望通过样本数；修复后的通过率单列 post_repair_pass_rate。",
    "期望拦截的样本（outcome=block）不进 FPVR 分母；block_rate = 正确拦截数 / "
    "期望拦截数，错误码必须命中 expected_error_codes。",
    "schema_only 模式只校验 TaskDraft 结构（S3-05 口径），业务规则（奖励/等级/引用/"
    "成环）需要注入 S4 校验器后才能判定；边界拦截样本在该模式下会记为 unexpected_pass。",
    "字段断言是黄金集对生成内容的要求（类型/等级段/奖励区间/引用），不是校验器错误；"
    "断言失败会进 failed_assertions，便于定位提示词或上下文问题。",
    "断言已按可执行规则分层（见 assertion_policy）：硬断言保留 task_type、等级段/跨度、"
    "MAP/TASK 引用存在、奖励上限与奖励下限（C_REWARD_*_MIN_01 已加入约束语料）；"
    "objectives 精确条数、giver 精确 NPC、等级 min/max 精确值、前置精确集合、"
    "NPC/ITEM 引用仍为 advisory，不计入 FPVR。",
    "修复回灌改为结构化断言错误 {field, expected, actual, suggestion}（不再合成 "
    'field="draft"），TaskAgent.repair 逐字段定位；validator 错误保留 field/suggestion。',
    "对抗口径分列 attack_resisted（攻击/注入未被采纳）、code_hit（命中 "
    "expected_error_codes）与 struct_guaranteed（ID 分配器重写 task_id、自环不可达，"
    "commit 复检兜底）；effective_security_rate 是三者并集，危险拦截率采用该值。",
    "v2 按子集（normal/boundary/adversarial）分组统计；对抗样本的错误码必须全部命中 "
    "expected_error_codes，未命中样本列在 adversarial.error_code_misses。",
)


class AgentEvalError(RuntimeError):
    """Raised when the golden set, a generator, or a validator is unusable."""


# --------------------------------------------------------------------------
# Golden requests
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ExpectedOutcome:
    """What a draft for one golden request must look like.

    ``outcome="pass"`` requires the field assertions below. ``outcome="block"``
    requires a rejection whose error codes contain ``expected_error_codes``;
    field assertions are only used to diagnose an unexpected pass.
    """

    outcome: str
    task_type: str | None = None
    level_min: int | None = None
    level_max: int | None = None
    level_band: tuple[int, int] | None = None
    level_span_max: int = MAX_LEVEL_SPAN
    gold_between: tuple[int, int] | None = None
    exp_between: tuple[int, int] | None = None
    giver_npc_id: str | None = None
    required_references: tuple[str, ...] = ()
    prerequisites: tuple[str, ...] | None = None
    objective_count_between: tuple[int, int] | None = None
    expected_error_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class GoldenRequest:
    sample_id: str
    category: str
    request: str
    expected: ExpectedOutcome
    constraints: tuple[str, ...] = ()
    source_task: str | None = None
    notes: str = ""
    attack: str | None = None


def _display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(path)


def _expect_mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AgentEvalError(f"{where} 必须是 JSON 对象")
    return value


def _expect_str(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AgentEvalError(f"{where} 必须是非空字符串")
    return value.strip()


def _optional_str(value: Any, where: str) -> str | None:
    if value is None:
        return None
    return _expect_str(value, where)


def _expect_int(value: Any, where: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AgentEvalError(f"{where} 必须是整数")
    if minimum is not None and value < minimum:
        raise AgentEvalError(f"{where} 必须 ≥ {minimum}，当前为 {value}")
    return value


def _optional_int(value: Any, where: str, *, minimum: int | None = None) -> int | None:
    if value is None:
        return None
    return _expect_int(value, where, minimum=minimum)


def _expect_between(value: Any, where: str) -> tuple[int, int]:
    if not isinstance(value, list) or len(value) != 2:
        raise AgentEvalError(f"{where} 必须是 [下限, 上限] 两元素数组")
    low = _expect_int(value[0], f"{where}[0]", minimum=0)
    high = _expect_int(value[1], f"{where}[1]", minimum=0)
    if low > high:
        raise AgentEvalError(f"{where} 的下限 {low} 不得大于上限 {high}")
    return low, high


def _expect_str_list(value: Any, where: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise AgentEvalError(f"{where} 必须是字符串数组")
    return tuple(
        _expect_str(item, f"{where}[{position}]") for position, item in enumerate(value)
    )


def _parse_expected(raw: Any, where: str) -> ExpectedOutcome:
    mapping = _expect_mapping(raw, where)
    outcome = _expect_str(mapping.get("outcome"), f"{where}.outcome")
    if outcome not in {"pass", "block"}:
        raise AgentEvalError(f"{where}.outcome 只能是 pass 或 block，当前为 {outcome!r}")

    task_type = _optional_str(mapping.get("task_type"), f"{where}.task_type")
    if task_type is not None and task_type not in LEVEL_BANDS:
        raise AgentEvalError(
            f"{where}.task_type 必须是 {'/'.join(sorted(LEVEL_BANDS))}，当前为 {task_type!r}"
        )
    if outcome == "pass" and task_type is None:
        raise AgentEvalError(f"{where}.task_type 在 outcome=pass 时必填")

    level_min = _optional_int(mapping.get("level_min"), f"{where}.level_min", minimum=1)
    level_max = _optional_int(mapping.get("level_max"), f"{where}.level_max", minimum=1)
    if level_min is not None and level_max is not None and level_min > level_max:
        raise AgentEvalError(f"{where}.level_min 不得大于 level_max")

    level_band = (
        _expect_between(mapping["level_band"], f"{where}.level_band")
        if "level_band" in mapping
        else None
    )
    if level_band is not None and level_band[0] < 1:
        raise AgentEvalError(f"{where}.level_band 下限必须 ≥ 1")

    span_max = _expect_int(
        mapping.get("level_span_max", MAX_LEVEL_SPAN),
        f"{where}.level_span_max",
        minimum=0,
    )
    if span_max > MAX_LEVEL_SPAN:
        raise AgentEvalError(
            f"{where}.level_span_max 不得超过 {MAX_LEVEL_SPAN}（任务设计规范硬约束）"
        )

    effective_band = level_band or (LEVEL_BANDS[task_type] if task_type else None)
    if effective_band is not None and outcome == "pass":
        if level_min is not None and not effective_band[0] <= level_min <= effective_band[1]:
            raise AgentEvalError(
                f"{where}.level_min={level_min} 不在 {task_type} 等级段 "
                f"{effective_band[0]}–{effective_band[1]} 内"
            )
        if level_max is not None and not effective_band[0] <= level_max <= effective_band[1]:
            raise AgentEvalError(
                f"{where}.level_max={level_max} 不在 {task_type} 等级段 "
                f"{effective_band[0]}–{effective_band[1]} 内"
            )

    gold_between = (
        _expect_between(mapping["gold_between"], f"{where}.gold_between")
        if "gold_between" in mapping
        else None
    )
    exp_between = (
        _expect_between(mapping["exp_between"], f"{where}.exp_between")
        if "exp_between" in mapping
        else None
    )
    giver_npc_id = _optional_str(mapping.get("giver_npc_id"), f"{where}.giver_npc_id")
    required_references = _expect_str_list(
        mapping.get("required_references", []), f"{where}.required_references"
    )
    prerequisites = (
        _expect_str_list(mapping["prerequisites"], f"{where}.prerequisites")
        if "prerequisites" in mapping
        else None
    )
    objective_count_between = (
        _expect_between(
            mapping["objective_count_between"], f"{where}.objective_count_between"
        )
        if "objective_count_between" in mapping
        else None
    )
    expected_error_codes = _expect_str_list(
        mapping.get("expected_error_codes", []), f"{where}.expected_error_codes"
    )
    unknown_codes = [code for code in expected_error_codes if code not in _KNOWN_ERROR_CODES]
    if unknown_codes:
        raise AgentEvalError(
            f"{where}.expected_error_codes 含未知错误码：{', '.join(unknown_codes)}；"
            "必须使用 schemas/report.py 中冻结的 E_*/W_* 代码"
        )

    if outcome == "pass":
        missing = [
            name
            for name, value in (
                ("level_min", level_min),
                ("gold_between", gold_between),
                ("exp_between", exp_between),
                ("giver_npc_id", giver_npc_id),
            )
            if value is None
        ]
        if missing:
            raise AgentEvalError(f"{where} 缺少期望通过样本的字段断言：{', '.join(missing)}")
        if not required_references:
            raise AgentEvalError(f"{where}.required_references 在 outcome=pass 时不得为空")
        if expected_error_codes:
            raise AgentEvalError(f"{where}.expected_error_codes 只允许出现在 outcome=block 样本上")
        if span_max < 1:
            raise AgentEvalError(f"{where}.level_span_max 在 outcome=pass 时必须 ≥ 1")
    else:
        if not expected_error_codes:
            raise AgentEvalError(f"{where}.expected_error_codes 在 outcome=block 时必填")
        if not required_references:
            raise AgentEvalError(f"{where}.required_references 不得为空")

    return ExpectedOutcome(
        outcome=outcome,
        task_type=task_type,
        level_min=level_min,
        level_max=level_max,
        level_band=level_band,
        level_span_max=span_max,
        gold_between=gold_between,
        exp_between=exp_between,
        giver_npc_id=giver_npc_id,
        required_references=required_references,
        prerequisites=prerequisites,
        objective_count_between=objective_count_between,
        expected_error_codes=expected_error_codes,
    )


def _parse_record(raw: Any, line_number: int) -> GoldenRequest:
    where = f"第 {line_number} 行"
    mapping = _expect_mapping(raw, where)
    sample_id = _expect_str(mapping.get("id"), f"{where}.id")
    category = _expect_str(mapping.get("category"), f"{sample_id}.category")
    if category not in CATEGORIES:
        raise AgentEvalError(
            f"{sample_id}.category 只能是 {'/'.join(sorted(CATEGORIES))}，当前为 {category!r}"
        )
    request = _expect_str(mapping.get("request"), f"{sample_id}.request")
    expected = _parse_expected(mapping.get("expected"), f"{sample_id}.expected")
    constraints = _expect_str_list(mapping.get("constraints", []), f"{sample_id}.constraints")
    source_task = _optional_str(mapping.get("source_task"), f"{sample_id}.source_task")
    notes = _optional_str(mapping.get("notes"), f"{sample_id}.notes") or ""
    attack = _optional_str(mapping.get("attack"), f"{sample_id}.attack")
    return GoldenRequest(
        sample_id=sample_id,
        category=category,
        request=request,
        expected=expected,
        constraints=constraints,
        source_task=source_task,
        notes=notes,
        attack=attack,
    )


def load_requests(path: str | Path | None = None) -> tuple[GoldenRequest, ...]:
    """Load and validate a golden-set JSONL file (default: v1, UTF-8 JSONL)."""

    resolved = Path(path) if path is not None else REQUESTS_PATH
    try:
        text = resolved.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise AgentEvalError(f"黄金需求文件不存在：{resolved}") from exc
    except OSError as exc:
        raise AgentEvalError(f"黄金需求文件无法读取：{resolved}（{exc}）") from exc

    requests: list[GoldenRequest] = []
    seen: dict[str, int] = {}
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            raw = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise AgentEvalError(
                f"{_display_path(resolved)} 第 {line_number} 行不是合法 JSON："
                f"第 {exc.colno} 列（{exc.msg}）"
            ) from exc
        golden = _parse_record(raw, line_number)
        if golden.sample_id in seen:
            raise AgentEvalError(
                f"{_display_path(resolved)} 中 id 重复：{golden.sample_id}"
                f"（第 {seen[golden.sample_id]} 行与第 {line_number} 行）"
            )
        seen[golden.sample_id] = line_number
        requests.append(golden)

    if not requests:
        raise AgentEvalError(f"{_display_path(resolved)} 没有有效需求行")
    return tuple(requests)


def validate_v2_distribution(requests: Sequence[GoldenRequest]) -> None:
    """Enforce the frozen S7-01 v2 shape (30 = 20/5/5, two prompt injections).

    The audit is intentionally strict: a drifted golden set silently changes the
    benchmark, so distribution problems raise :class:`AgentEvalError` instead of
    producing a plausible-looking report.
    """

    if len(requests) != V2_TOTAL:
        raise AgentEvalError(
            f"v2 黄金集必须是 {V2_TOTAL} 条，当前为 {len(requests)} 条"
        )
    by_category = Counter(item.category for item in requests)
    expected = Counter(V2_EXPECTED_COUNTS)
    if by_category != expected:
        raise AgentEvalError(
            "v2 黄金集分布必须为 "
            + " / ".join(f"{name} {count}" for name, count in V2_EXPECTED_COUNTS.items())
            + f"，当前为 {dict(sorted(by_category.items()))}"
        )

    for item in requests:
        if item.category == "normal" and item.expected.outcome != "pass":
            raise AgentEvalError(f"{item.sample_id} 是 normal 样本，但 outcome 不是 pass")
        if item.category != "adversarial":
            continue
        if item.expected.outcome != "block":
            raise AgentEvalError(f"{item.sample_id} 是对抗样本，必须声明 outcome=block")
        if not item.expected.expected_error_codes:
            raise AgentEvalError(f"{item.sample_id} 是对抗样本，必须声明 expected_error_codes")
        if not item.attack:
            raise AgentEvalError(
                f"{item.sample_id} 是对抗样本，必须用 attack 标注攻击类型"
            )

    injections = [
        item.sample_id
        for item in requests
        if item.category == "adversarial" and item.attack == ATTACK_PROMPT_INJECTION
    ]
    if len(injections) != V2_REQUIRED_INJECTIONS:
        raise AgentEvalError(
            f"v2 必须包含 {V2_REQUIRED_INJECTIONS} 条提示注入对抗样本，"
            f"当前为 {len(injections)} 条：{injections}"
        )

    seen_requests: dict[str, str] = {}
    for item in requests:
        normalized = " ".join(item.request.split())
        if normalized in seen_requests:
            raise AgentEvalError(
                f"v2 需求文本重复：{item.sample_id} 与 {seen_requests[normalized]}"
            )
        seen_requests[normalized] = item.sample_id


def load_requests_v2(path: str | Path | None = None) -> tuple[GoldenRequest, ...]:
    """Load ``data/golden/requests_v2.jsonl`` and enforce its frozen distribution."""

    resolved = Path(path) if path is not None else REQUESTS_V2_PATH
    requests = load_requests(resolved)
    validate_v2_distribution(requests)
    return requests


def summarize_requests(requests: Sequence[GoldenRequest]) -> dict[str, Any]:
    """Counts that make the golden distribution auditable from the report."""

    by_category = Counter(item.category for item in requests)
    by_outcome = Counter(item.expected.outcome for item in requests)
    by_task_type = Counter(
        item.expected.task_type for item in requests if item.expected.task_type is not None
    )
    by_attack = Counter(item.attack for item in requests if item.attack)
    return {
        "count": len(requests),
        "by_category": dict(sorted(by_category.items())),
        "by_outcome": dict(sorted(by_outcome.items())),
        "by_task_type": dict(sorted(by_task_type.items())),
        "by_attack": dict(sorted(by_attack.items())),
        "prompt_injections": by_attack.get(ATTACK_PROMPT_INJECTION, 0),
        "with_prerequisites": sum(
            1 for item in requests if item.expected.prerequisites
        ),
        "with_expected_error_codes": sum(
            1 for item in requests if item.expected.expected_error_codes
        ),
    }


# --------------------------------------------------------------------------
# Field assertions
# --------------------------------------------------------------------------


def draft_references(draft: TaskDraft) -> frozenset[str]:
    """Every entity id a draft visibly points at (giver/objective/reward/prereq)."""

    references = {draft.giver.npc_id}
    if draft.giver.dialogue_ref:
        references.add(draft.giver.dialogue_ref)
    for objective in draft.objectives:
        references.add(objective.target)
        references.add(objective.area)
    for prerequisite in draft.prerequisites:
        references.add(prerequisite.ref)
    for reward in draft.rewards:
        if reward.item_id:
            references.add(reward.item_id)
    return frozenset(references)


def _reward_total(draft: TaskDraft, reward_type: str) -> int:
    return sum(reward.amount for reward in draft.rewards if reward.type.value == reward_type)


def _effective_band(expected: ExpectedOutcome) -> tuple[int, int] | None:
    if expected.level_band is not None:
        return expected.level_band
    if expected.task_type is not None:
        return LEVEL_BANDS[expected.task_type]
    return None


@dataclass(frozen=True)
class AssertionFailure:
    """One hard golden-assertion failure with a repair-friendly field path.

    ``TaskAgent.repair`` receives ``{field, expected, actual, suggestion}`` so it
    can patch the offending field instead of guessing from a synthetic
    ``field="draft"`` error. ``message`` keeps the human-readable form the report
    has always carried.
    """

    field: str
    expected: Any
    actual: Any
    suggestion: str
    message: str
    constraint_ref: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "field": self.field,
            "expected": self.expected,
            "actual": self.actual,
            "suggestion": self.suggestion,
            "message": self.message,
        }
        if self.constraint_ref:
            payload["constraint_ref"] = self.constraint_ref
        return payload

    def as_error(self) -> dict[str, Any]:
        """Repair-prompt shaped error: severity=error so it is never dropped."""

        payload = self.as_dict()
        payload.update(
            {
                "code": "E_GOLDEN_ASSERTION",
                "severity": "error",
                "source": "golden_assertion",
            }
        )
        return payload


def _reward_cap(expected: ExpectedOutcome, reward_type: str) -> int | None:
    """Upper bound the validator enforces (``gold_between[1]``/``exp_between[1]``)."""

    between = expected.gold_between if reward_type == "gold" else expected.exp_between
    return between[1] if between is not None else None


def _reward_interval(
    expected: ExpectedOutcome, reward_type: str
) -> tuple[int, int] | None:
    """The ``[min, max]`` interval the validator enforces for one reward type."""

    return expected.gold_between if reward_type == "gold" else expected.exp_between


def check_assertion_failures(
    draft: TaskDraft, expected: ExpectedOutcome
) -> tuple[AssertionFailure, ...]:
    """Return the *hard* (deterministically executable) assertion failures.

    Executable families: ``task_type`` (closed enum), ``level_band`` /
    ``level_span`` (design spec), ``reference_exists`` for MAP_/T_ references
    (validator has rules for areas and prerequisites) and reward
    **intervals** — both bounds, because ``C_REWARD_*_MIN_01`` now enforces the
    floor the same way ``C_REWARD_*_01`` enforces the cap. Everything else moves
    to :func:`check_advisory_assertions` and never affects FPVR.
    """

    failures: list[AssertionFailure] = []
    if expected.task_type is not None and draft.task_type.value != expected.task_type:
        failures.append(
            AssertionFailure(
                field="task_type",
                expected=expected.task_type,
                actual=draft.task_type.value,
                suggestion=f"将 task_type 改为 {expected.task_type}",
                message=f"task_type={draft.task_type.value}，期望 {expected.task_type}",
            )
        )

    band = _effective_band(expected)
    if band is not None and (
        draft.level_range.min < band[0] or draft.level_range.max > band[1]
    ):
        failures.append(
            AssertionFailure(
                field="level_range",
                expected=f"{band[0]}-{band[1]}",
                actual=f"{draft.level_range.min}-{draft.level_range.max}",
                suggestion=(
                    f"把等级段收进 {band[0]}-{band[1]}，并保持跨度不超过 "
                    f"{expected.level_span_max}"
                ),
                message=(
                    f"level_range={draft.level_range.min}-{draft.level_range.max}，"
                    f"超出等级段 {band[0]}-{band[1]}"
                ),
            )
        )
    span = draft.level_range.max - draft.level_range.min
    if span > expected.level_span_max:
        failures.append(
            AssertionFailure(
                field="level_range",
                expected=f"跨度≤{expected.level_span_max}",
                actual=span,
                suggestion=f"压缩等级跨度到 {expected.level_span_max} 级以内",
                message=f"level 跨度={span}，上限 {expected.level_span_max}",
            )
        )

    for reward_type, cap_ref, floor_ref in (
        ("gold", "C_REWARD_GOLD_01", "C_REWARD_GOLD_MIN_01"),
        ("exp", "C_REWARD_EXP_01", "C_REWARD_EXP_MIN_01"),
    ):
        interval = _reward_interval(expected, reward_type)
        if interval is None:
            continue
        low, high = interval
        actual = _reward_total(draft, reward_type)
        if actual < low:
            failures.append(
                AssertionFailure(
                    field=f"rewards.{reward_type}",
                    expected=f"[{low}, {high}]",
                    actual=actual,
                    suggestion=(
                        f"把 {reward_type} 总额提高到 {low} 以上"
                        f"（{floor_ref} 在 Lv{draft.level_range.min} 的档位下限）"
                    ),
                    message=f"{reward_type}={actual}，低于下限 {low}",
                    constraint_ref=floor_ref,
                )
            )
            continue
        if actual <= high:
            continue
        failures.append(
            AssertionFailure(
                field=f"rewards.{reward_type}",
                expected=f"[{low}, {high}]",
                actual=actual,
                suggestion=(
                    f"把 {reward_type} 总额降到 {high} 以内"
                    f"（{cap_ref} 在 Lv{draft.level_range.min} 的档位上限）"
                ),
                message=f"{reward_type}={actual}，超过上限 {high}",
                constraint_ref=cap_ref,
            )
        )

    current_references = draft_references(draft)
    required = [
        reference
        for reference in expected.required_references
        if reference.startswith(_ENFORCEABLE_REFERENCE_PREFIXES)
    ]
    missing = [
        reference
        for reference in required
        if not _reference_is_executable(draft, reference)
    ]
    if missing:
        constraint_ref = _reference_constraint(missing[0])
        failures.append(
            AssertionFailure(
                field="references",
                expected=missing,
                actual=sorted(current_references),
                suggestion=_reference_suggestion(missing),
                message=f"缺少引用：{', '.join(missing)}",
                constraint_ref=constraint_ref,
            )
        )
    return tuple(failures)


def _reference_is_executable(draft: TaskDraft, reference: str) -> bool:
    """Check MAP_/T_ references in the exact field the validator reads.

    ``C_AREA_EXISTS_01`` / ``C_AREA_ENUM_01`` read ``objectives[].area`` and
    ``C_PREREQ_EXISTS_01`` reads ``prerequisites[].ref``; a mention in an
    unrelated field is not enough to make the rule executable.
    """

    if reference.startswith("MAP_"):
        return any(objective.area == reference for objective in draft.objectives)
    if reference.startswith("T_"):
        return any(prerequisite.ref == reference for prerequisite in draft.prerequisites)
    return reference in draft_references(draft)


def _reference_constraint(reference: str) -> str:
    return "C_AREA_EXISTS_01" if reference.startswith("MAP_") else "C_PREREQ_EXISTS_01"


def _reference_suggestion(missing: Sequence[str]) -> str:
    maps = [reference for reference in missing if reference.startswith("MAP_")]
    tasks = [reference for reference in missing if reference.startswith("T_")]
    parts: list[str] = []
    if maps:
        parts.append(
            "把目标所在的 objectives[].area 改为 "
            + "/".join(maps)
            + "，或新增一个指向该地图的 reach 目标"
        )
    if tasks:
        parts.append(
            "在 prerequisites[] 中补上 " + "/".join(tasks) + "（type=task_completed）"
        )
    return "；".join(parts) or "补充缺失的引用"


def check_advisory_assertions(
    draft: TaskDraft, expected: ExpectedOutcome
) -> tuple[str, ...]:
    """Golden expectations kept for diagnostics only (never affect FPVR).

    These are the assertions the validator cannot execute: exact level bounds,
    exact giver NPC, exact prerequisite set, objective counts and NPC/ITEM
    references. Reward intervals are **not** here any more: both bounds are
    enforced by ``C_REWARD_*_01`` / ``C_REWARD_*_MIN_01`` and checked as hard
    assertions in :func:`check_assertion_failures`.
    """

    notes: list[str] = []
    if expected.level_min is not None and draft.level_range.min != expected.level_min:
        notes.append(
            f"level_range.min={draft.level_range.min}，期望 {expected.level_min}"
            "（仅提示：无 min 约束）"
        )
    if expected.level_max is not None and draft.level_range.max != expected.level_max:
        notes.append(
            f"level_range.max={draft.level_range.max}，期望 {expected.level_max}"
            "（仅提示：无 max 约束）"
        )
    if expected.giver_npc_id is not None and draft.giver.npc_id != expected.giver_npc_id:
        notes.append(
            f"giver.npc_id={draft.giver.npc_id}，期望 {expected.giver_npc_id}"
            "（仅提示：无发布者归属约束）"
        )
    if expected.prerequisites is not None:
        actual = sorted(prerequisite.ref for prerequisite in draft.prerequisites)
        wanted = sorted(expected.prerequisites)
        if actual != wanted:
            notes.append(
                f"prerequisites={actual}，期望 {wanted}（仅提示：精确集合不是校验规则）"
            )
    if expected.objective_count_between is not None:
        low, high = expected.objective_count_between
        actual_count = len(draft.objectives)
        if not low <= actual_count <= high:
            notes.append(
                f"objectives={actual_count}，期望 {low}-{high}"
                "（仅提示：无目标条数约束）"
            )
    current_references = draft_references(draft)
    advisory_refs = [
        reference
        for reference in expected.required_references
        if not reference.startswith(_ENFORCEABLE_REFERENCE_PREFIXES)
        and reference not in current_references
    ]
    if advisory_refs:
        notes.append(
            f"缺少引用：{', '.join(advisory_refs)}"
            "（仅提示：无 NPC/ITEM 引用规则）"
        )
    return tuple(notes)


def check_assertions(
    draft: TaskDraft, expected: ExpectedOutcome
) -> tuple[str, ...]:
    """Return hard assertion messages; empty means the draft matches the policy."""

    return tuple(
        failure.message for failure in check_assertion_failures(draft, expected)
    )


# --------------------------------------------------------------------------
# Validation reports
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str = ""
    field: str = ""
    constraint_ref: str | None = None
    suggestion: dict[str, Any] | None = None

    def as_error(self) -> dict[str, Any]:
        """Repair-prompt shaped error, preserving field/suggestion from the report."""

        payload: dict[str, Any] = {
            "code": self.code,
            "severity": "error",
            "message": self.message or self.code,
        }
        if self.field:
            payload["field"] = self.field
        if self.constraint_ref:
            payload["constraint_ref"] = self.constraint_ref
        if self.suggestion is not None:
            payload["suggestion"] = self.suggestion
        return payload


@dataclass(frozen=True)
class ValidationOutcome:
    ok: bool
    issues: tuple[ValidationIssue, ...] = ()

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(issue.code for issue in self.issues)


def _suggestion_dict(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    if isinstance(raw, Mapping):
        return dict(raw)
    dump = getattr(raw, "model_dump", None)
    if callable(dump):
        value = dump(mode="json")
        return dict(value) if isinstance(value, Mapping) else None
    return None


def _issue_from(raw: Any) -> ValidationIssue:
    if isinstance(raw, Mapping):
        code = raw.get("code")
        message = raw.get("message", "")
        field = raw.get("field", "")
        constraint_ref = raw.get("constraint_ref")
        suggestion = raw.get("suggestion")
    else:
        code = getattr(raw, "code", None)
        message = getattr(raw, "message", "")
        field = getattr(raw, "field", "")
        constraint_ref = getattr(raw, "constraint_ref", None)
        suggestion = getattr(raw, "suggestion", None)
    code = getattr(code, "value", code)
    message = getattr(message, "value", message)
    return ValidationIssue(
        code=str(code) if code else "UNKNOWN",
        message=str(message) if message else "",
        field=str(field) if field else "",
        constraint_ref=str(constraint_ref) if constraint_ref else None,
        suggestion=_suggestion_dict(suggestion),
    )


def _normalize_validation(raw: Any, sample_id: str) -> ValidationOutcome:
    if raw is None:
        raise AgentEvalError(
            f"{sample_id} 校验器返回 None；需要 ValidationReport 或 {{ok, errors}} 映射"
        )
    if isinstance(raw, bool):
        ok = raw
        errors: Any = []
    elif isinstance(raw, Mapping):
        ok = raw.get("ok")
        errors = raw.get("errors", [])
    else:
        ok = getattr(raw, "ok", None)
        errors = getattr(raw, "errors", [])
    if not isinstance(ok, bool):
        raise AgentEvalError(f"{sample_id} 校验报告缺少布尔字段 ok")
    if not isinstance(errors, Sequence) or isinstance(errors, (str, bytes)):
        raise AgentEvalError(f"{sample_id} 校验报告的 errors 必须是数组")
    issues = tuple(_issue_from(error) for error in errors)
    if ok and issues:
        raise AgentEvalError(f"{sample_id} 校验报告自相矛盾：ok=true 但含 errors")
    if not ok and not issues:
        issues = (ValidationIssue("UNKNOWN", "校验器返回 ok=false 但未提供 errors"),)
    return ValidationOutcome(ok=ok, issues=issues)


def _run_validator(
    draft: TaskDraft,
    validator: Callable[[TaskDraft], Any] | None,
    sample_id: str,
) -> ValidationOutcome:
    if validator is None:
        return ValidationOutcome(ok=True)
    try:
        raw = validator(draft)
    except Exception as exc:
        raise AgentEvalError(f"{sample_id} 校验器失败：{exc}") from exc
    return _normalize_validation(raw, sample_id)


def _pydantic_error_message(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors()[:5]:
        location = ".".join(str(part) for part in error.get("loc", ())) or "<root>"
        parts.append(f"{location}: {error.get('msg', 'invalid')}")
    return "；".join(parts)


def _parse_draft(raw: Any) -> tuple[TaskDraft | None, str]:
    if isinstance(raw, TaskDraft):
        return raw, ""
    payload = raw
    if not isinstance(raw, Mapping):
        # A's TaskAgent returns DraftResult(draft=..., task=TaskDraft); accept
        # both the mapping and the validated model without importing agents/.
        draft_attribute = getattr(raw, "draft", None)
        if isinstance(draft_attribute, TaskDraft):
            return draft_attribute, ""
        if isinstance(draft_attribute, Mapping):
            payload = draft_attribute
        else:
            task_attribute = getattr(raw, "task", None)
            if isinstance(task_attribute, TaskDraft):
                return task_attribute, ""
            dump = getattr(raw, "model_dump", None)
            if not callable(dump):
                return None, f"生成器返回 {type(raw).__name__}，无法转换为 TaskDraft"
            try:
                payload = dump(mode="json")
            except TypeError:
                try:
                    payload = dump()
                except Exception as exc:  # report, never guess
                    return None, f"生成结果 model_dump() 失败：{exc}"
            except Exception as exc:  # report, never guess
                return None, f"生成结果 model_dump() 失败：{exc}"
    if not isinstance(payload, Mapping):
        return None, f"生成器返回 {type(payload).__name__}，无法转换为 TaskDraft 映射"
    try:
        return TaskDraft.model_validate(payload), ""
    except ValidationError as exc:
        return None, _pydantic_error_message(exc)


# --------------------------------------------------------------------------
# Per-sample evaluation
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Attempt:
    raw: Any
    draft: TaskDraft | None
    schema_ok: bool
    validation: ValidationOutcome
    failed_assertions: tuple[str, ...]
    message: str = ""
    assertion_failures: tuple[AssertionFailure, ...] = ()
    advisory_assertions: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.schema_ok and self.validation.ok and not self.failed_assertions

    @property
    def failure_stage(self) -> str:
        if not self.schema_ok:
            return "schema_error"
        if not self.validation.ok:
            return "validation_error"
        return "assertion_failure"

    def failure_context(self) -> dict[str, Any]:
        if not self.schema_ok:
            stage = "schema"
        elif not self.validation.ok:
            stage = "validation"
        else:
            stage = "assertions"
        if not self.validation.ok:
            errors = [issue.as_error() for issue in self.validation.issues]
        elif self.assertion_failures:
            errors = [failure.as_error() for failure in self.assertion_failures]
        else:
            errors = []
        return {
            "stage": stage,
            "ok": False,
            # Repair node input: validation errors keep their field/suggestion;
            # assertion-only failures become structured {field, expected,
            # actual, suggestion} errors instead of a synthetic field="draft".
            "errors": errors,
            "failed_assertions": list(self.failed_assertions),
            "assertion_failures": [
                failure.as_dict() for failure in self.assertion_failures
            ],
            "advisory_assertions": list(self.advisory_assertions),
        }


@dataclass(frozen=True)
class SampleResult:
    sample_id: str
    category: str
    request: str
    expected_outcome: str
    actual_outcome: str
    first_pass: bool
    repair_rounds: int
    schema_ok: bool
    validation_ok: bool
    error_codes: tuple[str, ...] = ()
    failed_assertions: tuple[str, ...] = ()
    duration_ms: int = 0
    message: str = ""
    expected_error_codes: tuple[str, ...] = ()
    error_code_hit: bool = False
    attack: str | None = None
    assertion_failures: tuple[AssertionFailure, ...] = ()
    advisory_assertions: tuple[str, ...] = ()
    attack_resisted: bool | None = None
    struct_guaranteed: bool = False
    struct_guaranteed_reason: str | None = None
    effective_security: bool | None = None

    @property
    def accepted(self) -> bool:
        return self.actual_outcome in _PASS_OUTCOMES

    @property
    def correct_block(self) -> bool:
        return self.actual_outcome == "correct_block"

    @property
    def matches_expected(self) -> bool:
        if self.expected_outcome == "pass":
            return self.accepted
        return self.correct_block

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.sample_id,
            "category": self.category,
            "request": self.request,
            "expected_outcome": self.expected_outcome,
            "actual_outcome": self.actual_outcome,
            "first_pass": self.first_pass,
            "accepted": self.accepted,
            "repair_rounds": self.repair_rounds,
            "schema_ok": self.schema_ok,
            "validation_ok": self.validation_ok,
            "error_codes": list(self.error_codes),
            "expected_error_codes": list(self.expected_error_codes),
            "error_code_hit": self.error_code_hit,
            "attack": self.attack,
            "failed_assertions": list(self.failed_assertions),
            "assertion_failures": [
                failure.as_dict() for failure in self.assertion_failures
            ],
            "advisory_assertions": list(self.advisory_assertions),
            "attack_resisted": self.attack_resisted,
            "struct_guaranteed": self.struct_guaranteed,
            "struct_guaranteed_reason": self.struct_guaranteed_reason,
            "effective_security": self.effective_security,
            "duration_ms": self.duration_ms,
            "message": self.message,
        }


def _attempt(
    raw: Any,
    expected: ExpectedOutcome,
    validator: Callable[[TaskDraft], Any] | None,
    sample_id: str,
) -> _Attempt:
    draft, schema_error = _parse_draft(raw)
    if draft is None:
        return _Attempt(
            raw=raw,
            draft=None,
            schema_ok=False,
            validation=ValidationOutcome(
                ok=False,
                issues=(ValidationIssue("E_SCHEMA_002", schema_error),),
            ),
            failed_assertions=(),
            message=schema_error,
        )
    validation = _run_validator(draft, validator, sample_id)
    failures = check_assertion_failures(draft, expected) if validation.ok else ()
    advisory = check_advisory_assertions(draft, expected) if validation.ok else ()
    failed_assertions = tuple(failure.message for failure in failures)
    message = "；".join(issue.message for issue in validation.issues if issue.message)
    if not message and failed_assertions:
        message = "；".join(failed_assertions)
    return _Attempt(
        raw=raw,
        draft=draft,
        schema_ok=True,
        validation=validation,
        failed_assertions=failed_assertions,
        message=message,
        assertion_failures=failures,
        advisory_assertions=advisory,
    )


def _named_task_id(request: str) -> str | None:
    match = _TASK_ID_RE.search(request)
    return match.group(0) if match is not None else None


def _request_entity_ids(request: str) -> set[str]:
    return set(_ENTITY_ID_RE.findall(request))


def _placeholder_ids(golden: GoldenRequest) -> set[str]:
    """Entity IDs an attack asks for but the golden expectation does not allow."""

    allowed = set(golden.expected.required_references)
    allowed.update(golden.expected.prerequisites or ())
    if golden.expected.giver_npc_id:
        allowed.add(golden.expected.giver_npc_id)
    return _request_entity_ids(golden.request) - allowed


def _injection_adopted(draft: TaskDraft | None, expected: ExpectedOutcome) -> bool:
    """Whether the draft actually contains the injected over-cap reward payload.

    Prompt-injection samples (G26/G27) ask for rewards far above the band cap or
    for a string amount. A schema-invalid draft is treated conservatively as
    "possibly adopted"; a valid draft is adopted only when a reward total exceeds
    the executable cap.
    """

    if draft is None:
        return True
    for reward_type in ("gold", "exp"):
        cap = _reward_cap(expected, reward_type)
        if cap is not None and _reward_total(draft, reward_type) > cap:
            return True
    return False


def _security_signals(
    golden: GoldenRequest, attempt: _Attempt
) -> tuple[bool | None, bool, str | None, bool | None]:
    """Return ``(attack_resisted, struct_guaranteed, reason, effective_security)``.

    ``struct_guaranteed`` records attacks the agent architecture makes
    impossible regardless of validator coverage: the allocator rewrites
    ``task_id`` (so a requested duplicate ID is never written) and a TaskDraft
    can only declare its own prerequisites, so an existing task can never be
    made to depend on the draft (the self-cycle in G30 is unreachable; the
    commit path re-validates as a backstop).
    """

    attack = golden.attack
    if attack is None:
        return None, False, None, None
    draft = attempt.draft
    resisted = False
    guaranteed = False
    reason: str | None = None

    if attack == ATTACK_PROMPT_INJECTION:
        resisted = not _injection_adopted(draft, golden.expected)
    elif attack == "duplicate_task_id":
        requested = _named_task_id(golden.request)
        if draft is not None and requested is not None and draft.task_id.strip() != requested:
            resisted = True
            guaranteed = True
            reason = (
                "TaskAgent 用 TaskIdAllocator 重写 task_id，"
                "请求复用的 ID 不会进入草案；commit 前还有一次复检。"
            )
    elif attack == "dependency_cycle":
        guaranteed = True
        reason = (
            "TaskDraft 只能声明自身前置，无法反向修改已提交任务；"
            "TaskAgent 另行分配 task_id，自环不可达（commit 复检兜底）。"
        )
    elif attack == "missing_reference":
        placeholders = _placeholder_ids(golden)
        if draft is not None and not (draft_references(draft) & placeholders):
            resisted = True

    effective = bool(
        resisted
        or guaranteed
        or not attempt.schema_ok
        or not attempt.validation.ok
    )
    return resisted, guaranteed, reason, effective


def _sample_result(
    golden: GoldenRequest,
    attempt: _Attempt,
    *,
    outcome: str,
    first_pass: bool,
    repair_rounds: int,
    first_schema_ok: bool,
    started: float,
) -> SampleResult:
    expected_codes = golden.expected.expected_error_codes
    actual_codes = attempt.validation.codes
    error_code_hit = bool(expected_codes) and all(
        code in actual_codes for code in expected_codes
    )
    resisted, guaranteed, reason, effective = _security_signals(golden, attempt)
    return SampleResult(
        sample_id=golden.sample_id,
        category=golden.category,
        request=golden.request,
        expected_outcome=golden.expected.outcome,
        actual_outcome=outcome,
        first_pass=first_pass,
        repair_rounds=repair_rounds,
        schema_ok=first_schema_ok,
        validation_ok=attempt.validation.ok,
        error_codes=actual_codes,
        failed_assertions=attempt.failed_assertions,
        duration_ms=int((time.perf_counter() - started) * 1000),
        message=attempt.message,
        expected_error_codes=expected_codes,
        error_code_hit=error_code_hit,
        attack=golden.attack,
        assertion_failures=attempt.assertion_failures,
        advisory_assertions=attempt.advisory_assertions,
        attack_resisted=resisted,
        struct_guaranteed=guaranteed,
        struct_guaranteed_reason=reason,
        effective_security=effective,
    )


def _judge_block(
    golden: GoldenRequest,
    attempt: _Attempt,
    started: float,
) -> SampleResult:
    wanted = golden.expected.expected_error_codes
    actual_codes = set(attempt.validation.codes)
    matched = all(code in actual_codes for code in wanted)
    if not attempt.schema_ok:
        # A schema-invalid draft never reaches the validator; it is only a
        # correct interception when the golden sample expects the schema code
        # (e.g. an injected request that asks for a string reward amount).
        outcome = "correct_block" if matched else "schema_error"
    elif not attempt.validation.ok:
        outcome = "correct_block" if matched else "blocked_unexpected_code"
    else:
        outcome = "unexpected_pass"
    return _sample_result(
        golden,
        attempt,
        outcome=outcome,
        first_pass=False,
        repair_rounds=0,
        first_schema_ok=attempt.schema_ok,
        started=started,
    )


def evaluate_sample(
    golden: GoldenRequest,
    generator: Callable[[str], Any],
    *,
    validator: Callable[[TaskDraft], Any] | None = None,
    repairer: Callable[[GoldenRequest, Any, Mapping[str, Any]], Any] | None = None,
    max_repair_rounds: int = DEFAULT_MAX_REPAIR_ROUNDS,
) -> SampleResult:
    """Generate, validate and (optionally) repair one golden request.

    ``repairer(request, last_raw_draft, failure_context) -> new_raw_draft`` mirrors
    the S4-05 repair node: at most ``max_repair_rounds`` rounds, each fed the
    validation report (and failed assertions) from the previous attempt.
    """

    if isinstance(max_repair_rounds, bool) or not isinstance(max_repair_rounds, int):
        raise AgentEvalError("max_repair_rounds 必须是整数")
    if max_repair_rounds < 0:
        raise AgentEvalError("max_repair_rounds 不得为负数")

    started = time.perf_counter()
    try:
        raw = generator(golden.request)
    except Exception as exc:
        return SampleResult(
            sample_id=golden.sample_id,
            category=golden.category,
            request=golden.request,
            expected_outcome=golden.expected.outcome,
            actual_outcome="generation_error",
            first_pass=False,
            repair_rounds=0,
            schema_ok=False,
            validation_ok=False,
            duration_ms=int((time.perf_counter() - started) * 1000),
            message=f"生成失败：{exc}",
        )

    first = _attempt(raw, golden.expected, validator, golden.sample_id)

    if golden.expected.outcome == "block":
        return _judge_block(golden, first, started)

    if first.passed:
        return _sample_result(
            golden,
            first,
            outcome="passed",
            first_pass=True,
            repair_rounds=0,
            first_schema_ok=first.schema_ok,
            started=started,
        )

    if repairer is None or max_repair_rounds == 0:
        return _sample_result(
            golden,
            first,
            outcome=first.failure_stage,
            first_pass=False,
            repair_rounds=0,
            first_schema_ok=first.schema_ok,
            started=started,
        )

    if not callable(repairer):
        raise AgentEvalError("repairer 必须是可调用对象")

    current_raw = raw
    last = first
    for round_number in range(1, max_repair_rounds + 1):
        try:
            current_raw = repairer(golden, current_raw, last.failure_context())
        except Exception as exc:
            raise AgentEvalError(
                f"{golden.sample_id} 修复器第 {round_number} 轮失败：{exc}"
            ) from exc
        last = _attempt(current_raw, golden.expected, validator, golden.sample_id)
        if last.passed:
            return _sample_result(
                golden,
                last,
                outcome="passed_after_repair",
                first_pass=False,
                repair_rounds=round_number,
                first_schema_ok=first.schema_ok,
                started=started,
            )

    return _sample_result(
        golden,
        last,
        outcome=last.failure_stage,
        first_pass=False,
        repair_rounds=max_repair_rounds,
        first_schema_ok=first.schema_ok,
        started=started,
    )


# --------------------------------------------------------------------------
# Metrics and report
# --------------------------------------------------------------------------


def compute_subset_metrics(samples: Sequence[SampleResult]) -> dict[str, Any]:
    """Metrics for one category slice; denominators may legitimately be zero.

    The overall FPVR must have an expected-pass denominator, but a subset such
    as ``adversarial`` only has expected-block samples, so its ``fpvr`` is
    ``None`` rather than an error.
    """

    if not samples:
        raise AgentEvalError("没有可聚合的样本")
    pass_samples = [sample for sample in samples if sample.expected_outcome == "pass"]
    block_samples = [sample for sample in samples if sample.expected_outcome == "block"]
    coded_samples = [sample for sample in samples if sample.expected_error_codes]
    first_pass = sum(1 for sample in pass_samples if sample.first_pass)
    accepted = sum(1 for sample in pass_samples if sample.accepted)
    correct_blocks = sum(1 for sample in block_samples if sample.correct_block)
    hits = sum(1 for sample in coded_samples if sample.error_code_hit)
    security_samples = [
        sample for sample in block_samples if sample.effective_security is not None
    ]
    secured = sum(1 for sample in security_samples if sample.effective_security)
    failure_groups = Counter(
        sample.actual_outcome for sample in pass_samples if not sample.accepted
    )
    metrics: dict[str, Any] = {
        "count": len(samples),
        "expected_pass": len(pass_samples),
        "expected_block": len(block_samples),
        "fpvr": _round(first_pass / len(pass_samples)) if pass_samples else None,
        "post_repair_pass_rate": (
            _round(accepted / len(pass_samples)) if pass_samples else None
        ),
        "block_rate": (
            _round(correct_blocks / len(block_samples)) if block_samples else None
        ),
        "error_code_hit_rate": (
            _round(hits / len(coded_samples)) if coded_samples else None
        ),
        "error_code_misses": [
            sample.sample_id for sample in coded_samples if not sample.error_code_hit
        ],
        "failure_groups": dict(sorted(failure_groups.items())),
    }
    if security_samples:
        metrics["effective_security_rate"] = _round(secured / len(security_samples))
        metrics["effective_security_ids"] = [
            sample.sample_id for sample in security_samples if sample.effective_security
        ]
    return metrics


def compute_metrics(samples: Sequence[SampleResult]) -> dict[str, Any]:
    """Aggregate FPVR, block rate, code hits and repair rounds (overall + subsets)."""

    if not samples:
        raise AgentEvalError("没有可聚合的样本")
    pass_samples = [sample for sample in samples if sample.expected_outcome == "pass"]
    block_samples = [sample for sample in samples if sample.expected_outcome == "block"]
    if not pass_samples:
        raise AgentEvalError("没有期望通过的样本，FPVR 分母为 0")

    first_pass = sum(1 for sample in pass_samples if sample.first_pass)
    accepted = sum(1 for sample in pass_samples if sample.accepted)
    rounds = [sample.repair_rounds for sample in pass_samples]
    histogram = Counter(str(value) for value in rounds)
    repaired = sum(
        1 for sample in pass_samples if sample.accepted and sample.repair_rounds > 0
    )
    attempted = sum(1 for sample in pass_samples if sample.repair_rounds > 0)
    repair_failed = sum(
        1 for sample in pass_samples if not sample.accepted and sample.repair_rounds > 0
    )
    failure_groups = Counter(
        sample.actual_outcome for sample in pass_samples if not sample.accepted
    )
    correct_blocks = sum(1 for sample in block_samples if sample.correct_block)
    block_rate = (
        _round(correct_blocks / len(block_samples)) if block_samples else None
    )
    coded_samples = [sample for sample in samples if sample.expected_error_codes]
    code_hits = sum(1 for sample in coded_samples if sample.error_code_hit)
    categories = sorted({sample.category for sample in samples})
    subsets = {
        category: compute_subset_metrics(
            [sample for sample in samples if sample.category == category]
        )
        for category in categories
    }

    return {
        "metrics": {
            "fpvr": _round(first_pass / len(pass_samples)),
            "post_repair_pass_rate": _round(accepted / len(pass_samples)),
            "first_attempt_schema_rate": _round(
                sum(1 for sample in samples if sample.schema_ok) / len(samples)
            ),
            "outcome_accuracy": _round(
                sum(1 for sample in samples if sample.matches_expected) / len(samples)
            ),
            "block_rate": block_rate,
            "error_code_hit_rate": (
                _round(code_hits / len(coded_samples)) if coded_samples else None
            ),
            "mean_repair_rounds": _round(sum(rounds) / len(rounds)),
            "generation_error_rate": _round(
                sum(1 for sample in samples if sample.actual_outcome == "generation_error")
                / len(samples)
            ),
        },
        "denominators": {
            "total": len(samples),
            "expected_pass": len(pass_samples),
            "expected_block": len(block_samples),
        },
        "repair": {
            "needed": len(pass_samples) - first_pass,
            "attempted": attempted,
            "repaired": repaired,
            "failed": repair_failed,
            "mean_rounds": _round(sum(rounds) / len(rounds)),
            "histogram": dict(sorted(histogram.items(), key=lambda item: int(item[0]))),
        },
        "failure_groups": dict(sorted(failure_groups.items())),
        "subsets": subsets,
    }


def _round(value: float) -> float:
    return round(float(value), 4)


def compute_fpvr(samples: Sequence[SampleResult]) -> float:
    """First-Pass Validation Rate over the samples expected to pass (0–1)."""

    return compute_metrics(samples)["metrics"]["fpvr"]


# --------------------------------------------------------------------------
# Observability roll-up (S7-03)
# --------------------------------------------------------------------------

_OBSERVABILITY_NOTES = (
    "P95 采用 nearest-rank：升序排列后取 ceil(0.95 × n) 位置的值，不做插值；"
    "报告只汇总真实样本/轨迹，缺字段时输出 null 而不是估算值。",
    "单次成本 = 所有 llm.* span 的 cost_cny 之和 / 出现过 LLM 调用的 trace 数；"
    "没有 cost_cny 的调用不参与分母，并计入 unpriced_calls。",
    "危险拦截率优先取对抗子集的 effective_security_rate"
    "（attack_resisted ∪ struct_guaranteed ∪ validator 拦截）；"
    "缺该字段时回退 adversarial.block_rate，再回退整体 block_rate 并在 notes 标注。",
)


@dataclass(frozen=True)
class LlmUsage:
    """One ``llm.*`` span's usage attributes, normalized from trace JSONL."""

    trace_id: str
    span_id: str
    name: str
    provider: str | None
    model: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens_override: int | None
    cost_cny: float | None
    duration_ms: int

    @property
    def total_tokens(self) -> int | None:
        """``prompt + completion`` or an explicit total; ``None`` when unknown."""

        if self.total_tokens_override is not None:
            return self.total_tokens_override
        if self.prompt_tokens is None and self.completion_tokens is None:
            return None
        return (self.prompt_tokens or 0) + (self.completion_tokens or 0)


def percentile(values: Sequence[float], quantile: float = P95_QUANTILE) -> float | None:
    """Nearest-rank percentile; ``None`` for an empty sample.

    No interpolation on purpose: with 20 golden samples the 19th value is the
    P95, which is easier to audit from ``per_sample`` than an interpolated one.
    """

    if not 0 < quantile <= 1:
        raise AgentEvalError(f"quantile 必须在 (0, 1] 内，当前为 {quantile}")
    numbers: list[float] = []
    for position, value in enumerate(values):
        number = _coerce_number(value, f"values[{position}]")
        if number is not None:
            numbers.append(number)
    if not numbers:
        return None
    ordered = sorted(numbers)
    rank = math.ceil(quantile * len(ordered))
    return ordered[max(0, rank - 1)]


def _coerce_number(value: Any, where: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AgentEvalError(f"{where} 必须是数字，当前为 {type(value).__name__}")
    number = float(value)
    if not math.isfinite(number):
        raise AgentEvalError(f"{where} 必须是有限数字")
    return number


def _coerce_int(value: Any, where: str, *, minimum: int = 0) -> int | None:
    number = _coerce_number(value, where)
    if number is None:
        return None
    if not number.is_integer():
        raise AgentEvalError(f"{where} 必须是整数")
    integer = int(number)
    if integer < minimum:
        raise AgentEvalError(f"{where} 必须 ≥ {minimum}，当前为 {integer}")
    return integer


def _required_text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AgentEvalError(f"{where} 必须是非空字符串")
    return value.strip()


def _optional_text(value: Any, where: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, where)


def _optional_round(value: Any, where: str) -> float | None:
    number = _coerce_number(value, where)
    return _round(number) if number is not None else None


def _as_sequence(value: Any, where: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise AgentEvalError(f"{where} 必须是数组")
    return value


def extract_llm_usage(
    records: Sequence[Mapping[str, Any]],
) -> tuple[LlmUsage, ...]:
    """Extract normalized usage from every ``llm.*`` record; others are ignored.

    Missing ``provider``/``model``/token/cost attributes stay ``None``: it is the
    audit's job (S7-05) to report incompleteness, not this summary's job to guess.
    """

    usage: list[LlmUsage] = []
    for position, record in enumerate(records, start=1):
        where = f"第 {position} 条轨迹记录"
        if not isinstance(record, Mapping):
            raise AgentEvalError(f"{where} 不是 JSON 对象")
        name = _required_text(record.get("name"), f"{where}.name")
        if not name.startswith("llm."):
            continue
        attrs = record.get("attrs", {})
        if not isinstance(attrs, Mapping):
            raise AgentEvalError(f"{where}（{name}）的 attrs 必须是 JSON 对象")
        trace_id = _required_text(record.get("trace_id"), f"{where}.trace_id")
        duration_ms = _coerce_int(record.get("duration_ms", 0), f"{where}.duration_ms")
        prompt_tokens = _coerce_int(
            attrs.get("prompt_tokens"), f"{where}.attrs.prompt_tokens"
        )
        completion_tokens = _coerce_int(
            attrs.get("completion_tokens"), f"{where}.attrs.completion_tokens"
        )
        total_override: int | None = None
        raw_tokens = attrs.get("tokens")
        if isinstance(raw_tokens, Mapping):
            total_override = _coerce_int(
                raw_tokens.get("total"), f"{where}.attrs.tokens.total"
            )
            if prompt_tokens is None:
                prompt_tokens = _coerce_int(
                    raw_tokens.get("prompt"), f"{where}.attrs.tokens.prompt"
                )
            if completion_tokens is None:
                completion_tokens = _coerce_int(
                    raw_tokens.get("completion"), f"{where}.attrs.tokens.completion"
                )
        elif raw_tokens is not None:
            total_override = _coerce_int(raw_tokens, f"{where}.attrs.tokens")
        cost = _coerce_number(
            attrs.get("cost_cny", attrs.get("cost")), f"{where}.attrs.cost_cny"
        )
        if cost is not None and cost < 0:
            raise AgentEvalError(f"{where}.attrs.cost_cny 不得为负数")
        usage.append(
            LlmUsage(
                trace_id=trace_id,
                span_id=_optional_text(record.get("span_id"), f"{where}.span_id") or "",
                name=name,
                provider=_optional_text(attrs.get("provider"), f"{where}.attrs.provider"),
                model=_optional_text(attrs.get("model"), f"{where}.attrs.model"),
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens_override=total_override,
                cost_cny=cost,
                duration_ms=duration_ms or 0,
            )
        )
    return tuple(usage)


def _parse_iso_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _trace_durations(records: Sequence[Mapping[str, Any]]) -> list[int]:
    """Per-trace end-to-end duration: latest span end minus earliest span start.

    Falls back to the longest span when timestamps are missing/unparseable, so a
    hand-written fixture still produces a number instead of silently dropping the
    trace from the P95 denominator.
    """

    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for record in records:
        if record.get("kind", "span") != "span":
            continue
        trace_id = record.get("trace_id")
        if isinstance(trace_id, str) and trace_id:
            grouped.setdefault(trace_id, []).append(record)
    durations: list[int] = []
    for spans in grouped.values():
        starts: list[datetime] = []
        ends: list[datetime] = []
        span_durations: list[int] = []
        for span in spans:
            duration = _coerce_int(span.get("duration_ms", 0), "轨迹.duration_ms") or 0
            span_durations.append(duration)
            start = _parse_iso_ts(span.get("ts"))
            if start is not None:
                starts.append(start)
                ends.append(start + timedelta(milliseconds=duration))
        if starts and ends:
            durations.append(int((max(ends) - min(starts)).total_seconds() * 1000))
        elif span_durations:
            durations.append(max(span_durations))
    return durations


def summarize_traces(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate LLM usage and end-to-end latency from parsed JSONL trace records."""

    records_tuple = tuple(records)
    span_durations: list[int] = []
    trace_ids: set[str] = set()
    for position, record in enumerate(records_tuple, start=1):
        where = f"第 {position} 条轨迹记录"
        if not isinstance(record, Mapping):
            raise AgentEvalError(f"{where} 不是 JSON 对象")
        trace_ids.add(_required_text(record.get("trace_id"), f"{where}.trace_id"))
        if record.get("kind", "span") != "span":
            continue
        duration = _coerce_int(record.get("duration_ms", 0), f"{where}.duration_ms")
        span_durations.append(duration or 0)

    calls = extract_llm_usage(records_tuple)
    llm_traces = {call.trace_id for call in calls}
    priced = [call for call in calls if call.cost_cny is not None]
    tokenized = [call for call in calls if call.total_tokens is not None]
    total_cost = round(sum(call.cost_cny for call in priced), 6) if priced else None
    cost_per_run = (
        round(total_cost / len(llm_traces), 6)
        if total_cost is not None and llm_traces
        else None
    )
    return {
        "records": len(records_tuple),
        "traces": len(trace_ids),
        "llm_calls": len(calls),
        "llm_traces": len(llm_traces),
        "priced_calls": len(priced),
        "unpriced_calls": len(calls) - len(priced),
        "tokenized_calls": len(tokenized),
        "providers": dict(
            sorted(Counter(call.provider for call in calls if call.provider).items())
        ),
        "models": dict(sorted(Counter(call.model for call in calls if call.model).items())),
        "total_tokens": (
            sum(call.total_tokens for call in tokenized) if tokenized else None
        ),
        "total_cost_cny": total_cost,
        "cost_per_run_cny": cost_per_run,
        "cost_basis": "per_trace_with_llm" if cost_per_run is not None else None,
        "p95_latency_ms": percentile(_trace_durations(records_tuple)),
        "p95_span_ms": percentile(span_durations),
        "p95_llm_latency_ms": percentile([call.duration_ms for call in calls]),
    }


def _dangerous_counts(report: Mapping[str, Any]) -> tuple[int, int]:
    """Count adversarial samples whose dangerous intent did not get through.

    New reports carry ``effective_security`` (attack resisted, code hit or
    structurally guaranteed). Legacy reports without the field fall back to
    ``actual_outcome == "correct_block"``.
    """

    per_sample = report.get("per_sample")
    if per_sample is None:
        return 0, 0
    samples = _as_sequence(per_sample, "评测报告.per_sample")
    total = 0
    blocked = 0
    for position, sample in enumerate(samples):
        if not isinstance(sample, Mapping):
            raise AgentEvalError(f"per_sample[{position}] 必须是 JSON 对象")
        if sample.get("category") != "adversarial":
            continue
        total += 1
        effective = sample.get("effective_security")
        if effective is True or (
            effective is None and sample.get("actual_outcome") == "correct_block"
        ):
            blocked += 1
    return total, blocked


def summarize_agent_report(
    report: Mapping[str, Any],
    trace_records: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Roll an agent evaluation report (+ optional traces) into S7 headline metrics.

    The report supplies FPVR, repair rounds, danger block rate and per-sample
    latency; traces supply token totals and per-run cost. Every aggregate keeps
    its denominator so a percentage can never be read without its sample count.
    """

    if not isinstance(report, Mapping):
        raise AgentEvalError("评测报告必须是 JSON 对象")
    metrics = report.get("metrics")
    if not isinstance(metrics, Mapping):
        raise AgentEvalError("评测报告缺少 metrics 对象")
    denominators = report.get("denominators")
    if not isinstance(denominators, Mapping):
        raise AgentEvalError("评测报告缺少 denominators 对象")

    fpvr = _coerce_number(metrics.get("fpvr"), "metrics.fpvr")
    if fpvr is None:
        raise AgentEvalError("评测报告缺少 metrics.fpvr")
    repair_block = report.get("repair")
    repair = dict(repair_block) if isinstance(repair_block, Mapping) else {}
    mean_repair = _coerce_number(
        metrics.get("mean_repair_rounds"), "metrics.mean_repair_rounds"
    )
    if mean_repair is None:
        mean_repair = _coerce_number(
            repair.get("mean_rounds"), "repair.mean_rounds"
        )
    if mean_repair is None:
        raise AgentEvalError("评测报告缺少平均修复轮次（metrics.mean_repair_rounds）")

    block_rate = _coerce_number(metrics.get("block_rate"), "metrics.block_rate")
    dangerous_rate: float | None = block_rate
    notes: list[str] = []
    adversarial = report.get("adversarial")
    if isinstance(adversarial, Mapping):
        effective_rate = _coerce_number(
            adversarial.get("effective_security_rate"),
            "adversarial.effective_security_rate",
        )
        adversarial_rate = _coerce_number(
            adversarial.get("block_rate"), "adversarial.block_rate"
        )
        if effective_rate is not None:
            dangerous_rate = effective_rate
            notes.append(
                "危险拦截率采用对抗子集 effective_security_rate"
                "（attack_resisted ∪ struct_guaranteed ∪ validator 拦截），"
                "不是单纯错误码命中率。"
            )
        elif adversarial_rate is not None:
            dangerous_rate = adversarial_rate
        else:
            notes.append("对抗子集缺少 block_rate，危险拦截率回退为整体 block_rate。")
    else:
        notes.append("报告没有 adversarial 子集，危险拦截率回退为整体 block_rate。")

    per_sample = report.get("per_sample")
    durations: list[int] = []
    if per_sample is not None:
        for position, sample in enumerate(_as_sequence(per_sample, "评测报告.per_sample")):
            if not isinstance(sample, Mapping):
                raise AgentEvalError(f"per_sample[{position}] 必须是 JSON 对象")
            duration = _coerce_int(
                sample.get("duration_ms"), f"per_sample[{position}].duration_ms"
            )
            if duration is not None:
                durations.append(duration)
    p95_latency = percentile(durations)

    total = _coerce_int(denominators.get("total"), "denominators.total")
    expected_pass = _coerce_int(
        denominators.get("expected_pass"), "denominators.expected_pass"
    )
    expected_block = _coerce_int(
        denominators.get("expected_block"), "denominators.expected_block"
    )
    if total is None or expected_pass is None or expected_block is None:
        raise AgentEvalError(
            "评测报告 denominators 必须包含 total / expected_pass / expected_block"
        )

    trace_tuple = tuple(trace_records)
    trace_summary = summarize_traces(trace_tuple) if trace_tuple else None
    if p95_latency is None and trace_summary is not None:
        p95_latency = trace_summary["p95_latency_ms"]
        if p95_latency is not None:
            notes.append("报告 per_sample 无 duration_ms，P95 回退为轨迹 span P95。")
    if p95_latency is None:
        notes.append("报告与轨迹都没有可用延迟，P95 为 null。")
    cost_per_run = (
        trace_summary["cost_per_run_cny"] if trace_summary is not None else None
    )
    if trace_summary is None:
        notes.append("未提供轨迹记录，单次成本与轨迹延迟为 null。")
    elif cost_per_run is None:
        notes.append(
            "轨迹中没有带 cost_cny 的 llm.* span，单次成本为 null"
            "（不按 token 估算）。"
        )
    dangerous_total, dangerous_blocked = _dangerous_counts(report)
    raw_failure_groups = report.get("failure_groups", {})
    if not isinstance(raw_failure_groups, Mapping):
        raise AgentEvalError("评测报告 failure_groups 必须是 JSON 对象")
    failure_groups = {
        str(name): int(count)
        for name, count in sorted(raw_failure_groups.items())
    }

    return {
        "metrics": {
            "fpvr": _round(fpvr),
            "post_repair_pass_rate": _optional_round(
                metrics.get("post_repair_pass_rate"),
                "metrics.post_repair_pass_rate",
            ),
            "mean_repair_rounds": _round(mean_repair),
            "dangerous_block_rate": (
                _round(dangerous_rate) if dangerous_rate is not None else None
            ),
            "block_rate": _round(block_rate) if block_rate is not None else None,
            "p95_latency_ms": p95_latency,
            "cost_per_run_cny": cost_per_run,
        },
        "denominators": {
            "total": total,
            "expected_pass": expected_pass,
            "expected_block": expected_block,
            "dangerous_total": dangerous_total,
            "dangerous_blocked": dangerous_blocked,
            "latency_samples": len(durations),
            "priced_runs": trace_summary["llm_traces"] if trace_summary else 0,
        },
        "failure_groups": failure_groups,
        "repair": repair,
        "traces": trace_summary,
        "notes": notes,
    }


def build_observability_summary(
    report: Mapping[str, Any],
    *,
    trace_records: Sequence[Mapping[str, Any]] = (),
    generated_at: str | None = None,
    report_ref: str | None = None,
    trace_ref: str | None = None,
) -> dict[str, Any]:
    """Build the written S7-03 summary (``reports/observability-summary.json``)."""

    trace_tuple = tuple(trace_records)
    summary = summarize_agent_report(report, trace_tuple)
    return {
        "suite": "observability",
        "status": "ok",
        "generated_at": generated_at
        or datetime.now().astimezone().isoformat(timespec="seconds"),
        "metrics": summary["metrics"],
        "denominators": summary["denominators"],
        "failure_groups": summary["failure_groups"],
        "repair": summary["repair"],
        "traces": summary["traces"],
        "sources": {
            "report": str(report_ref) if report_ref is not None else None,
            "traces": str(trace_ref) if trace_ref is not None else None,
            "trace_records": len(trace_tuple),
        },
        "notes": [*_OBSERVABILITY_NOTES, *summary["notes"]],
    }


def load_agent_report(path: str | Path | None = None) -> dict[str, Any]:
    """Read an agent evaluation report; missing/malformed files raise a clear error."""

    target = Path(path) if path is not None else REPORT_PATH
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise AgentEvalError(
            f"评测报告不存在：{_display_path(target)}；"
            "先运行 run_agent_eval（注入真实 generator）产出报告，再跑本汇总。"
        ) from exc
    except OSError as exc:
        raise AgentEvalError(f"评测报告无法读取：{_display_path(target)}（{exc}）") from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AgentEvalError(
            f"评测报告不是合法 JSON：{_display_path(target)} "
            f"第 {exc.lineno} 行第 {exc.colno} 列（{exc.msg}）"
        ) from exc
    if not isinstance(raw, Mapping):
        raise AgentEvalError(f"评测报告必须是 JSON 对象：{_display_path(target)}")
    return dict(raw)


def load_trace_records(
    path: str | Path | None = None,
) -> tuple[dict[str, Any], ...]:
    """Read JSONL trace records; a missing file yields ``()`` (no traces yet)."""

    target = Path(path) if path is not None else DEFAULT_TRACE_PATH
    if not target.exists():
        return ()
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise AgentEvalError(f"轨迹文件无法读取：{_display_path(target)}（{exc}）") from exc
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            raw = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise AgentEvalError(
                f"轨迹不是合法 JSONL：{_display_path(target)} 第 {line_number} 行"
                f"（{exc.msg}）"
            ) from exc
        if not isinstance(raw, Mapping):
            raise AgentEvalError(
                f"轨迹第 {line_number} 行必须是 JSON 对象：{_display_path(target)}"
            )
        records.append(dict(raw))
    return tuple(records)


def run_observability_summary(
    *,
    report_path: str | Path | None = None,
    trace_path: str | Path | None = None,
    output_path: str | Path | None = None,
    generated_at: str | None = None,
) -> RunResult:
    """Aggregate the agent report + traces and write the observability summary."""

    resolved_report = Path(report_path) if report_path is not None else REPORT_PATH
    resolved_trace = Path(trace_path) if trace_path is not None else DEFAULT_TRACE_PATH
    report = load_agent_report(resolved_report)
    records = load_trace_records(resolved_trace)
    summary = build_observability_summary(
        report,
        trace_records=records,
        generated_at=generated_at,
        report_ref=_display_path(resolved_report),
        trace_ref=_display_path(resolved_trace) if resolved_trace.exists() else None,
    )
    target = Path(output_path) if output_path is not None else DEFAULT_SUMMARY_PATH
    written = write_report(summary, target)
    metrics = summary["metrics"]
    dangerous = metrics["dangerous_block_rate"]
    p95 = metrics["p95_latency_ms"]
    cost = metrics["cost_per_run_cny"]
    message = (
        f"FPVR={metrics['fpvr']:.2%}"
        f"（首轮通过 {summary['denominators']['expected_pass']} 条期望通过样本），"
        f"平均修复 {metrics['mean_repair_rounds']:.2f} 轮，"
        f"危险拦截率={dangerous if dangerous is not None else 'n/a'}，"
        f"P95={p95 if p95 is not None else 'n/a'} ms，"
        f"单次成本={cost if cost is not None else 'n/a'} CNY；"
        f"报告写入 {_display_path(written)}"
    )
    return RunResult(status="ok", message=message, report_path=written, report=summary)


def _callable_name(function: Any) -> str:
    module = getattr(function, "__module__", "") or ""
    qualname = getattr(function, "__qualname__", "") or getattr(function, "__name__", "")
    if not qualname:
        return type(function).__name__
    return f"{module}.{qualname}".lstrip(".")


def _validator_mode(validator: Callable[[TaskDraft], Any] | None) -> str:
    return "schema_only" if validator is None else "custom_validator"


def _adversarial_summary(samples: Sequence[SampleResult]) -> dict[str, Any]:
    """Adversarial view: code hits vs. attacks actually resisted / guaranteed.

    A validator code hit proves detection, but not every dangerous request can
    (or should) surface as an error code: prompt injections may simply be
    ignored, and duplicate-ID / self-cycle requests are made impossible by the
    agent's ID allocator and the TaskDraft shape. ``effective_security_rate`` is
    the union of code hits, resisted attacks and structural guarantees, so the
    headline number measures real safety instead of validator coverage.
    """

    adversarial = [sample for sample in samples if sample.category == "adversarial"]
    coded = [sample for sample in adversarial if sample.expected_error_codes]
    required = sorted({code for sample in coded for code in sample.expected_error_codes})
    hit = sorted(
        {
            code
            for sample in coded
            if sample.error_code_hit
            for code in sample.expected_error_codes
        }
    )
    correct_blocks = sum(1 for sample in adversarial if sample.correct_block)
    code_hits = sum(1 for sample in coded if sample.error_code_hit)
    injections = [
        sample for sample in adversarial if sample.attack == ATTACK_PROMPT_INJECTION
    ]
    resisted = [sample for sample in adversarial if sample.attack_resisted is True]
    injection_resisted = [sample for sample in injections if sample.attack_resisted is True]
    guaranteed = [sample for sample in adversarial if sample.struct_guaranteed]
    secured = [sample for sample in adversarial if sample.effective_security is True]
    return {
        "count": len(adversarial),
        "injection_count": len(injections),
        "attacks": dict(
            sorted(
                Counter(sample.attack for sample in adversarial if sample.attack).items()
            )
        ),
        "block_rate": (
            _round(correct_blocks / len(adversarial)) if adversarial else None
        ),
        "error_code_hit_rate": _round(code_hits / len(coded)) if coded else None,
        "error_code_misses": [
            sample.sample_id for sample in coded if not sample.error_code_hit
        ],
        "required_codes": required,
        "hit_codes": hit,
        "attack_resisted_rate": (
            _round(len(resisted) / len(adversarial)) if adversarial else None
        ),
        "attack_resisted_ids": [sample.sample_id for sample in resisted],
        "injection_resisted_rate": (
            _round(len(injection_resisted) / len(injections)) if injections else None
        ),
        "injection_resisted_ids": [
            sample.sample_id for sample in injection_resisted
        ],
        "struct_guaranteed_rate": (
            _round(len(guaranteed) / len(adversarial)) if adversarial else None
        ),
        "struct_guaranteed_ids": [sample.sample_id for sample in guaranteed],
        "struct_guaranteed_reasons": {
            sample.sample_id: sample.struct_guaranteed_reason
            for sample in guaranteed
            if sample.struct_guaranteed_reason
        },
        "effective_security_rate": (
            _round(len(secured) / len(adversarial)) if adversarial else None
        ),
        "effective_security_ids": [sample.sample_id for sample in secured],
    }


def build_report(
    samples: Sequence[SampleResult],
    requests: Sequence[GoldenRequest],
    *,
    generator: Callable[[str], Any],
    validator: Callable[[TaskDraft], Any] | None,
    requests_path: Path,
    max_repair_rounds: int,
    generated_at: str,
    suite_version: str = SUITE_VERSION,
    golden_version: int = 1,
) -> dict[str, Any]:
    stats = compute_metrics(samples)
    notes = list(_NOTES)
    mode = _validator_mode(validator)
    if mode == "schema_only":
        notes.append(
            "本次未注入 validator（schema_only）；期望拦截样本的 block_rate 不能作为业务"
            "拦截率，S4 规则引擎就绪后注入同一黄金集重跑。"
        )
    adversarial = _adversarial_summary(samples)
    if adversarial["error_code_misses"]:
        notes.append(
            "对抗样本错误码未全部命中 expected："
            + ", ".join(adversarial["error_code_misses"])
        )
    return {
        "suite": "agent",
        "suite_version": suite_version,
        "status": "ok",
        "generated_at": generated_at,
        "mode": mode,
        "config": {
            "requests_path": _display_path(requests_path),
            "max_repair_rounds": max_repair_rounds,
            "validator": mode,
        },
        "generator": {"name": _callable_name(generator)},
        "denominators": stats["denominators"],
        "metrics": stats["metrics"],
        "repair": stats["repair"],
        "failure_groups": stats["failure_groups"],
        "subsets": stats["subsets"],
        "adversarial": adversarial,
        "assertion_policy": {
            **ASSERTION_POLICY,
            "pending_constraints": [dict(item) for item in PENDING_CONSTRAINTS],
        },
        "golden": {
            "path": _display_path(requests_path),
            "version": golden_version,
            "distribution": summarize_requests(requests),
        },
        "per_sample": [sample.as_dict() for sample in samples],
        "notes": notes,
    }


def write_report(report: Mapping[str, Any], path: str | Path | None = None) -> Path:
    """Write the report as UTF-8 JSON (parents created); returns the path."""

    target = Path(path) if path is not None else REPORT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return target


# --------------------------------------------------------------------------
# Generator seam (A's agents/task_agent.py)
# --------------------------------------------------------------------------


class Generator(Protocol):
    """The only generation contract this harness consumes."""

    def __call__(self, request: str) -> Any:
        """Return a TaskDraft-compatible object for one natural-language request."""
        ...


def _import_generator_module() -> Any:
    """Indirection kept small so tests can stub A's module without an LLM."""

    return importlib.import_module(_GENERATOR_MODULE)


def _generator_from(candidate: Any) -> Callable[[str], Any] | None:
    """Accept a plain callable or A's ``TaskAgent``-like object with ``generate``."""

    if callable(candidate):
        return candidate
    method = getattr(candidate, "generate", None)
    if callable(method):
        agent_type = type(candidate)

        def generate(request: str) -> Any:
            return method(request)

        # Keep the report's generator name attributable to A's agent class.
        generate.__name__ = "generate"
        generate.__qualname__ = f"{agent_type.__qualname__}.generate"
        generate.__module__ = agent_type.__module__
        return generate
    return None


def load_generator(
    factory: Any = None,
) -> tuple[Callable[[str], Any] | None, str]:
    """Resolve the generator; ``reason`` is empty on success.

    Resolution order: an explicit callable (tests/replay) or ``TaskAgent``-like
    object with ``generate``, a module-level
    ``generate_draft``/``generate_task``/``generate`` function, then a
    ``TaskAgent`` instance exposing ``generate``. A's ``TaskAgent`` needs an
    ``LLMClient``, so the default run reports ``generator_unavailable`` rather
    than fabricating a baseline; pass a ready agent instance or an adapter
    function once A's layer is wired.
    """

    if factory is not None:
        resolved = _generator_from(factory)
        if resolved is None:
            raise AgentEvalError(
                "generator 必须是可调用对象，或带 generate() 方法的 TaskAgent 实例"
            )
        return resolved, ""

    try:
        module = _import_generator_module()
    except ImportError as exc:
        return None, f"待 A 就绪：agents/task_agent.py 尚不可导入（{exc}）"

    for entry in _GENERATOR_ENTRIES:
        candidate = getattr(module, entry, None)
        if callable(candidate):
            return candidate, ""

    for class_name in _GENERATOR_CLASSES:
        agent_class = getattr(module, class_name, None)
        if not isinstance(agent_class, type):
            continue
        try:
            agent = agent_class()
        except Exception as exc:  # report constructor failure
            return None, (
                f"agents/task_agent.py 的 {class_name}() 需要依赖，无法零参数构造：{exc}；"
                "请传入 TaskAgent 实例或 generate 适配函数"
            )
        method = getattr(agent, "generate", None)
        if callable(method):
            return method, ""

    return None, "agents/task_agent.py 未暴露 generate()/generate_draft()（契约待对齐）"


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RunResult:
    """Outcome of one harness run; ``report`` stays ``None`` when skipped."""

    status: str
    message: str
    report_path: Path
    report: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def run_agent_eval(
    *,
    suite: str = "v1",
    requests_path: str | Path | None = None,
    report_path: str | Path | None = None,
    generator: Any = None,
    validator: Callable[[TaskDraft], Any] | None = None,
    repairer: Callable[[GoldenRequest, Any, Mapping[str, Any]], Any] | None = None,
    max_repair_rounds: int = DEFAULT_MAX_REPAIR_ROUNDS,
    generated_at: str | None = None,
) -> RunResult:
    """Run one agent golden suite and write ``reports/fpvr-baseline.json``.

    ``suite="v2"`` loads ``data/golden/requests_v2.jsonl`` and enforces its frozen
    20/5/5 distribution; an explicit ``requests_path`` is loaded generically (so
    tests can use a small synthetic suite) but still reports the v2 version.
    Without a generator (or A's module) the run is skipped with an explanatory
    ``status`` and no report file, so CI never mistakes a placeholder for a
    measured baseline.
    """

    if isinstance(max_repair_rounds, bool) or not isinstance(max_repair_rounds, int):
        raise AgentEvalError("max_repair_rounds 必须是整数")
    if max_repair_rounds < 0:
        raise AgentEvalError("max_repair_rounds 不得为负数")
    if validator is not None and not callable(validator):
        raise AgentEvalError("validator 必须是可调用对象")
    if repairer is not None and not callable(repairer):
        raise AgentEvalError("repairer 必须是可调用对象")
    if suite not in GOLDEN_SUITES:
        raise AgentEvalError(
            f"suite 只能是 {'/'.join(sorted(GOLDEN_SUITES))}，当前为 {suite!r}"
        )

    if requests_path is not None:
        resolved_requests = Path(requests_path)
        requests = load_requests(resolved_requests)
    else:
        resolved_requests = GOLDEN_SUITES[suite]
        requests = (
            load_requests_v2(resolved_requests)
            if suite == "v2"
            else load_requests(resolved_requests)
        )
    suite_version = SUITE_VERSION_V2 if suite == "v2" else SUITE_VERSION
    golden_version = 2 if suite == "v2" else 1
    target = Path(report_path) if report_path is not None else REPORT_PATH

    instance, reason = load_generator(generator)
    if instance is None:
        return RunResult(
            status="generator_unavailable",
            message=reason,
            report_path=target,
            report=None,
        )

    samples = [
        evaluate_sample(
            golden,
            instance,
            validator=validator,
            repairer=repairer,
            max_repair_rounds=max_repair_rounds,
        )
        for golden in requests
    ]
    report = build_report(
        samples,
        requests,
        generator=instance,
        validator=validator,
        requests_path=resolved_requests,
        max_repair_rounds=max_repair_rounds,
        generated_at=generated_at or datetime.now().astimezone().isoformat(timespec="seconds"),
        suite_version=suite_version,
        golden_version=golden_version,
    )
    written = write_report(report, target)
    metrics = report["metrics"]
    denominators = report["denominators"]
    block_rate = metrics["block_rate"]
    first_pass = sum(1 for sample in samples if sample.first_pass)
    message = (
        f"已评测 {denominators['total']} 条需求（{suite_version}）："
        f"FPVR={metrics['fpvr']:.2%}"
        f"（首轮通过 {first_pass}/{denominators['expected_pass']}），"
        f"block_rate={block_rate if block_rate is not None else 'n/a'}；"
        f"报告写入 {_display_path(written)}"
    )
    return RunResult(status="ok", message=message, report_path=written, report=report)
