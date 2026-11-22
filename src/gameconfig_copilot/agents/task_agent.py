"""TaskAgent: ``LLMClient.structured`` -> schema-valid TaskDraft (S3-02).

The model is asked for a *placeholder* ``task_id`` (``T_<TYPE>_0000``); the
agent then overwrites it with the rule-allocated ID ("type + max seq + 1") and
rewrites ``provenance`` (request, evidence citations, model, timestamp). That
removes ID collisions at the source instead of validating them afterwards
(docs/03 S3).

Structured-output failures (``LLMStructuredOutputError``) are retried **once**
with the validation detail appended; a second failure raises
:class:`TaskGenerationError`, which the graph turns into ``rejected``. Repair
rounds reuse the same retry policy but keep the original ``task_id``; their
prompt is built by :func:`_repair_prompt` from the four required sections and
only carries ``severity="error"`` findings (docs/03 S4-05).

Input
    Request + ``BuiltContext`` (or evidence/constraints/preference).
Output
    :class:`DraftResult` (JSON draft, validated ``TaskDraft``, attempts, tokens).
Failure semantics
    Bad model output after one retry -> ``E_SCHEMA_002``; missing/invalid
    ``task_type`` -> ``E_SCHEMA_002``; final Pydantic validation failure ->
    ``E_SCHEMA_002``; repair without a previous report (or with warning-only
    findings) -> ``E_VALIDATION_REQUIRED`` instead of a blind retry. No files are
    written.
"""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from ..llm.client import (
    LLMClient,
    LLMStructuredOutputError,
    Message,
)
from ..observability.events import now_iso
from ..schemas import TaskDraft, TaskType
from .context_builder import BuiltContext, ContextBuilder
from .state import Evidence, Preference

__all__ = [
    "DraftResult",
    "TaskAgent",
    "TaskGenerationError",
    "TaskIdAllocator",
]

_TASK_ID_RE = re.compile(r"^T_(MAIN|SIDE|DAILY)_(\d{4})$")
_PREFIX_BY_TYPE: dict[TaskType, str] = {
    TaskType.MAIN: "MAIN",
    TaskType.SIDE: "SIDE",
    TaskType.DAILY: "DAILY",
}
_MAX_STATE_CHARS = 8000
_MAX_REPAIR_CONSTRAINTS = 8


class TaskGenerationError(RuntimeError):
    """Structured generation failed after the one allowed retry."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "E_SCHEMA_002",
        detail: str = "",
        attempts: int = 0,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail
        self.attempts = attempts

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "detail": self.detail[:1000],
            "attempts": self.attempts,
        }


@dataclass(frozen=True, slots=True)
class DraftResult:
    """One generated/repared draft plus provenance for tracing."""

    draft: dict[str, Any]
    task: TaskDraft
    model: str
    tokens: int
    attempts: int
    warnings: tuple[dict[str, Any], ...] = ()
    context: BuiltContext | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "draft": self.draft,
            "model": self.model,
            "tokens": self.tokens,
            "attempts": self.attempts,
            "warnings": list(self.warnings),
        }


class TaskIdAllocator:
    """Deterministic ``T_<TYPE>_<max+1>`` allocator (docs/03 S3)."""

    def __init__(self, existing_ids: Sequence[str] | None = None) -> None:
        self._max = {"MAIN": 0, "SIDE": 0, "DAILY": 0}
        for task_id in existing_ids or ():
            self.register(task_id)

    def register(self, task_id: str) -> None:
        match = _TASK_ID_RE.fullmatch(str(task_id).strip())
        if match is None:
            return
        prefix, sequence = match.group(1), int(match.group(2))
        self._max[prefix] = max(self._max[prefix], sequence)

    def next_id(self, task_type: TaskType) -> str:
        prefix = _PREFIX_BY_TYPE[task_type]
        sequence = self._max[prefix] + 1
        if sequence > 9999:
            raise TaskGenerationError(
                f"{prefix} 任务序号已用尽（>9999）",
                code="E_DUP_ID",
            )
        self._max[prefix] = sequence
        return f"T_{prefix}_{sequence:04d}"


def _coerce_task_type(payload: Mapping[str, Any]) -> TaskType:
    raw = payload.get("task_type")
    if isinstance(raw, TaskType):
        return raw
    try:
        return TaskType(str(raw))
    except ValueError as exc:
        raise TaskGenerationError(
            f"模型输出的 task_type 非法：{raw!r}",
            detail=str(exc),
        ) from exc


def _error_items(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return only the blocking ``severity="error"`` items of a report."""

    items: list[dict[str, Any]] = []
    for raw in report.get("errors") or ():
        if not isinstance(raw, Mapping):
            continue
        severity = getattr(raw.get("severity"), "value", raw.get("severity", "error"))
        if str(severity) != "error":
            continue
        items.append(dict(raw))
    return items


def _repair_prompt(
    request: str,
    current_draft: Mapping[str, Any],
    errors: Sequence[Mapping[str, Any]],
    *,
    warnings: Sequence[Mapping[str, Any]],
    constraints: Sequence[Evidence],
) -> str:
    """Build the four-section repair prompt required by docs/03 S4-05."""

    parts = [
        "## 原始需求（修复不得改变策划意图）",
        request.strip(),
        "## 当前草案（需要修复）",
        f"```json\n{_clip_json(current_draft)}\n```",
        "## 校验错误（仅 severity=error，必须逐条修复）",
        f"```json\n{_clip_json(list(errors))}\n```",
    ]
    if warnings:
        parts.append(
            f"（另有 {len(warnings)} 条 warning 属提示级，本节点不处理，不要为此改动结构。）"
        )
    directives = _error_directives(errors)
    if directives:
        parts.append("## 字段级修复指令（按 field 定位，逐条改完）")
        parts.extend(directives)
    parts.append("## 相关约束原文（修复依据，不得违反）")
    constraint_lines = _constraint_lines(errors, constraints)
    parts.extend(constraint_lines)
    parts.append(
        "请输出修复后的完整 TaskDraft JSON；只改 error 指向的字段，"
        "task_id 保持当前草案的值。"
    )
    return "\n\n".join(parts)


def _error_directives(errors: Sequence[Mapping[str, Any]]) -> list[str]:
    """Render one concrete instruction per error that names a field.

    Golden-assertion failures arrive as ``{field, expected, actual,
    suggestion}``; validator errors carry ``field`` and a structured
    ``suggestion``. Both are rendered so the model patches the exact path
    instead of rewriting the whole draft.
    """

    lines: list[str] = []
    for error in errors:
        field = str(error.get("field") or "").strip()
        if not field:
            continue
        pieces = [f"- `{field}`"]
        if error.get("actual") is not None:
            pieces.append(
                "当前 " + json.dumps(error["actual"], ensure_ascii=False, default=str)
            )
        if error.get("expected") is not None:
            pieces.append(
                "要求 " + json.dumps(error["expected"], ensure_ascii=False, default=str)
            )
        suggestion = error.get("suggestion")
        if isinstance(suggestion, Mapping):
            target = suggestion.get("field") or field
            value = suggestion.get("value")
            pieces.append(
                f"建议 `{target}` = {json.dumps(value, ensure_ascii=False, default=str)}"
            )
        elif suggestion:
            pieces.append(f"建议 {suggestion}")
        pieces.append(f"（{error.get('message') or error.get('code') or '需修复'}）")
        lines.append("；".join(pieces))
    return lines


def _constraint_lines(
    errors: Sequence[Mapping[str, Any]],
    constraints: Sequence[Evidence],
) -> list[str]:
    """Pick the constraints referenced by the errors, falling back to all."""

    refs = {
        str(error.get("constraint_ref")).strip()
        for error in errors
        if error.get("constraint_ref")
    }
    matched = [
        item
        for item in constraints
        if refs and str(item.metadata.get("constraint_id") or "") in refs
    ]
    selected = matched or list(constraints)[: _MAX_REPAIR_CONSTRAINTS]
    lines: list[str] = []
    seen: set[str] = set()
    for item in selected:
        if item.citation in seen:
            continue
        seen.add(item.citation)
        lines.append(f"- [{item.citation}] {' '.join(item.text.split())}")
    return lines or ["- 无可用约束原文；以错误列表的字段与取值要求为准。"]


def _clip_json(payload: Any) -> str:
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    return text if len(text) <= _MAX_STATE_CHARS else text[:_MAX_STATE_CHARS] + "…"


class TaskAgent:
    """Structured draft generation with a single retry on schema errors."""

    def __init__(
        self,
        llm: LLMClient,
        *,
        context_builder: ContextBuilder | None = None,
        existing_ids_provider: Callable[[], Sequence[str]] | None = None,
        model_name: str | None = None,
        now: Callable[[], str] | None = None,
    ) -> None:
        self.llm = llm
        self.context_builder = context_builder or ContextBuilder()
        self._existing_ids_provider = existing_ids_provider
        self._model_name = model_name or str(getattr(llm, "model", "") or "unknown")
        self._now = now or now_iso
        self._allocated: set[str] = set()

    @property
    def model_name(self) -> str:
        return self._model_name

    # ------------------------------------------------------------------ API
    def generate(
        self,
        request: str,
        *,
        evidence: Sequence[Evidence] | None = None,
        constraints: Sequence[Evidence] | None = None,
        preference: Preference | Mapping[str, Any] | None = None,
        context: BuiltContext | None = None,
        existing_ids: Sequence[str] | None = None,
    ) -> DraftResult:
        """Generate a fresh draft; retries structured output once."""

        built = context or self.context_builder.build(
            request,
            evidence=evidence,
            constraints=constraints,
            preference=preference,
        )
        messages = built.to_messages(request)
        payload, attempts = self._structured_with_retry(messages)
        task_type = _coerce_task_type(payload)
        allocator = TaskIdAllocator(self._known_ids(existing_ids))
        task_id = allocator.next_id(task_type)
        task, draft = self._finalize(
            payload,
            request=request,
            evidence=list(evidence or ()),
            task_id=task_id,
        )
        self._allocated.add(task_id)
        return DraftResult(
            draft=draft,
            task=task,
            model=self._model_name,
            tokens=built.total_tokens,
            attempts=attempts,
            warnings=tuple(built.warning_dicts),
            context=built,
        )

    def draft(
        self,
        request: str,
        *,
        evidence: Sequence[Evidence] | None = None,
        constraints: Sequence[Evidence] | None = None,
        preference: Preference | Mapping[str, Any] | None = None,
        context: BuiltContext | None = None,
        existing_ids: Sequence[str] | None = None,
    ) -> DraftResult:
        """Alias for :meth:`generate` (graph/CLI ergonomics)."""

        return self.generate(
            request,
            evidence=evidence,
            constraints=constraints,
            preference=preference,
            context=context,
            existing_ids=existing_ids,
        )

    def repair(
        self,
        request: str,
        current_draft: Mapping[str, Any],
        report: Mapping[str, Any],
        *,
        evidence: Sequence[Evidence] | None = None,
        constraints: Sequence[Evidence] | None = None,
        preference: Preference | Mapping[str, Any] | None = None,
        context: BuiltContext | None = None,
    ) -> DraftResult:
        """Repair a draft using the previous ``ValidationReport`` (never blind).

        The prompt always carries the four required sections (docs/03 S4-05):
        original request, current draft, **error-only** findings and the
        relevant constraint text. A missing report or a report without
        ``severity="error"`` items raises ``E_VALIDATION_REQUIRED`` instead of
        asking the model to guess, so a repair round is never a blind retry.
        """

        constraint_items = list(constraints or ())
        if not constraint_items:
            constraint_items = [
                item for item in evidence or () if item.source_type == "constraint"
            ]
        built = context or self.context_builder.build(
            request,
            evidence=evidence,
            constraints=constraint_items,
            preference=preference,
        )
        if not report:
            raise TaskGenerationError(
                "修复节点必须携带上一轮 ValidationReport",
                code="E_VALIDATION_REQUIRED",
            )
        errors = _error_items(report)
        if not errors:
            raise TaskGenerationError(
                "上一轮 ValidationReport 中没有 severity=error 的问题，禁止无信息重试",
                code="E_VALIDATION_REQUIRED",
                detail=f"report.ok={report.get('ok')!r}, "
                f"warnings={len(report.get('warnings') or [])}",
            )
        messages = built.to_messages(request)
        messages.append(
            Message.user(
                _repair_prompt(
                    request,
                    current_draft,
                    errors,
                    warnings=list(report.get("warnings") or []),
                    constraints=constraint_items,
                )
            )
        )
        payload, attempts = self._structured_with_retry(messages)
        original_id = str(current_draft.get("task_id", "")).strip()
        if not _TASK_ID_RE.fullmatch(original_id):
            task_type = _coerce_task_type(payload)
            original_id = TaskIdAllocator(self._known_ids()).next_id(task_type)
        task, draft = self._finalize(
            payload,
            request=request,
            evidence=list(evidence or ()),
            task_id=original_id,
        )
        return DraftResult(
            draft=draft,
            task=task,
            model=self._model_name,
            tokens=built.total_tokens,
            attempts=attempts,
            warnings=tuple(built.warning_dicts),
            context=built,
        )

    # -------------------------------------------------------------- internal
    def _known_ids(self, explicit: Sequence[str] | None = None) -> list[str]:
        ids: set[str] = set(self._allocated)
        ids.update(str(item) for item in (explicit or ()))
        if self._existing_ids_provider is not None:
            with contextlib.suppress(Exception):
                ids.update(str(item) for item in self._existing_ids_provider())
        return sorted(ids)

    def _structured_with_retry(
        self,
        messages: list[Message],
    ) -> tuple[dict[str, Any], int]:
        last_error: LLMStructuredOutputError | None = None
        latest = list(messages)
        for attempt in (1, 2):
            try:
                payload = self.llm.structured(latest, TaskDraft, temperature=0.0)
            except LLMStructuredOutputError as exc:
                last_error = exc
                if attempt == 2:
                    break
                detail = (exc.validation_detail or str(exc))[:1200]
                latest = [
                    *messages,
                    Message.user(
                        "上一次输出未通过 TaskDraft Schema 校验：\n"
                        f"{detail}\n请修正后只输出完整的 JSON 对象。"
                    ),
                ]
                continue
            if not isinstance(payload, Mapping):
                raise TaskGenerationError(
                    "LLMClient.structured 未返回 JSON 对象",
                    detail=type(payload).__name__,
                    attempts=attempt,
                )
            return dict(payload), attempt
        detail = (last_error.validation_detail if last_error else "")[:1200]
        raise TaskGenerationError(
            "模型输出在重试一次后仍未通过 TaskDraft Schema 校验",
            detail=detail,
            attempts=2,
        )

    def _finalize(
        self,
        payload: Mapping[str, Any],
        *,
        request: str,
        evidence: Sequence[Evidence],
        task_id: str,
    ) -> tuple[TaskDraft, dict[str, Any]]:
        data = dict(payload)
        data.pop("reasoning", None)
        data["task_id"] = task_id
        citations: list[str] = []
        for item in evidence:
            if item.citation not in citations:
                citations.append(item.citation)
        data["provenance"] = {
            "request": request.strip(),
            "evidence": citations[:5],
            "model": self._model_name,
            "created_at": self._now(),
        }
        try:
            task = TaskDraft.model_validate(data)
        except ValidationError as exc:
            raise TaskGenerationError(
                "生成的草案未通过 TaskDraft 最终校验",
                detail=str(exc),
            ) from exc
        return task, task.model_dump(mode="json")
