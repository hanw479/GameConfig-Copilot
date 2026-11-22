"""Deterministic fakes shared by agent/API/e2e tests (no network, no API key)."""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping, Sequence
from typing import Any

from gameconfig_copilot.llm.client import (
    LLMError,
    LLMResponse,
    LLMStructuredOutputError,
    Usage,
)
from gameconfig_copilot.rag.store import Chunk, Hit, matches_filters
from gameconfig_copilot.schemas import (
    CheckedCounts,
    ErrorCode,
    Severity,
    ValidationErrorItem,
    ValidationReport,
)


def make_hit(
    source_id: str,
    position: str,
    text: str,
    *,
    source_type: str,
    score: float = 1.0,
    **metadata: Any,
) -> Hit:
    payload: dict[str, Any] = {
        "source_type": source_type,
        "source_id": source_id,
        "position": position,
    }
    payload.update(metadata)
    return Hit(
        chunk_id=f"{source_id}#{position}",
        text=text,
        score=score,
        source_id=source_id,
        metadata=payload,
    )


class _SparseStub:
    def __init__(self, hits: Sequence[Hit]) -> None:
        self.chunks = [
            Chunk(chunk_id=hit.chunk_id, text=hit.text, metadata=dict(hit.metadata))
            for hit in hits
        ]


class FakeRetriever:
    """Filters in metadata order; records queries for tool-selection assertions."""

    def __init__(self, hits: Sequence[Hit]) -> None:
        self._hits = list(hits)
        self.sparse = _SparseStub(self._hits)
        self.queries: list[tuple[str, dict[str, Any]]] = []

    def search(
        self,
        query: str,
        filters: Mapping[str, Any] | None = None,
        top_k: int = 5,
        **_: Any,
    ) -> list[Hit]:
        active = dict(filters or {})
        self.queries.append((query, active))
        matched = [
            hit for hit in self._hits if matches_filters(hit.metadata, active)
        ]
        return matched[:top_k]

    def health(self) -> bool:
        return True


class FailingRetriever(FakeRetriever):
    """Raises when constraints are requested, to exercise fail-closed gather."""

    def search(
        self,
        query: str,
        filters: Mapping[str, Any] | None = None,
        top_k: int = 5,
        **_: Any,
    ) -> list[Hit]:
        if dict(filters or {}).get("source_type") == "constraint":
            raise RuntimeError("constraint store unavailable")
        return super().search(query, filters=filters, top_k=top_k)


def _citation_from_prompt(messages: Sequence[Any]) -> str:
    text = "\n".join(
        str(getattr(message, "content", message)) for message in messages
    )
    references = [
        match
        for match in re.findall(r"\[([^\[\]\n]{1,200})\]", text)
        if ":" in match
        and "#" in match
        and "<" not in match
        and ">" not in match
        and "source_id" not in match
    ]
    if not references:
        return "未检索到依据：知识库未覆盖该问题。"
    return f"根据检索片段，[{references[0]}] 给出了相关依据。"


class FakeLLM:
    """Implements the ``LLMClient`` Protocol with configurable failures."""

    def __init__(
        self,
        *,
        chat_content: str | None = None,
        structured_payload: Mapping[str, Any] | None = None,
        structured_sequence: Sequence[Any] | None = None,
        structured_errors_before_success: int = 0,
        chat_error: LLMError | None = None,
        structured_error: LLMError | None = None,
        model: str = "fake-model",
    ) -> None:
        self.model = model
        self.chat_calls: list[list[str]] = []
        self.structured_calls: list[Any] = []
        self._chat_content = chat_content
        self._payload = structured_payload
        self._sequence = list(structured_sequence or ())
        self._remaining_errors = structured_errors_before_success
        self._chat_error = chat_error
        self._structured_error = structured_error

    def chat(
        self,
        messages: Sequence[Any],
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        del temperature, max_tokens
        self.chat_calls.append([str(getattr(item, "content", item)) for item in messages])
        if self._chat_error is not None:
            raise self._chat_error
        content = self._chat_content or _citation_from_prompt(messages)
        return LLMResponse(
            content=content,
            model=self.model,
            usage=Usage(prompt_tokens=12, completion_tokens=8),
        )

    def structured(
        self,
        messages: Sequence[Any],
        schema: Any,
        *,
        temperature: float = 0.0,
    ) -> dict[str, Any]:
        del temperature
        self.structured_calls.append({"schema": schema, "messages": list(messages)})
        if self._remaining_errors > 0:
            self._remaining_errors -= 1
            raise LLMStructuredOutputError(
                "模型输出未通过 TaskDraft 校验",
                schema_name="TaskDraft",
                raw_content='{"task_id": "broken"}',
                validation_detail="task_id: String should match pattern",
            )
        if self._structured_error is not None:
            raise self._structured_error
        if self._sequence:
            value = self._sequence.pop(0)
            if isinstance(value, BaseException):
                raise value
            return copy.deepcopy(dict(value))
        if self._payload is None:
            raise LLMStructuredOutputError(
                "FakeLLM 未配置 structured_payload",
                schema_name="TaskDraft",
            )
        return copy.deepcopy(dict(self._payload))

    def health(self) -> bool:
        return True


def valid_task_payload(
    task_id: str = "T_SIDE_0000",
    *,
    task_type: str = "side",
    gold: int = 120,
    exp: int = 800,
) -> dict[str, Any]:
    """A valid TaskDraft payload; ``task_id`` is the model's placeholder."""

    return {
        "schema_version": 1,
        "task_id": task_id,
        "title": "铁匠的委托：失踪的矿车",
        "task_type": task_type,
        "level_range": {"min": 6, "max": 9},
        "giver": {"npc_id": "NPC_013", "dialogue_ref": "DLG_013_02"},
        "prerequisites": [{"type": "task_completed", "ref": "T_MAIN_0102"}],
        "objectives": [
            {
                "seq": 1,
                "type": "talk_to",
                "target": "NPC_013",
                "count": 1,
                "area": "MAP_STARPORT_01",
            },
            {
                "seq": 2,
                "type": "collect",
                "target": "ITEM_ORE_02",
                "count": 4,
                "area": "MAP_MINE_03",
            },
        ],
        "rewards": [
            {"type": "gold", "amount": gold},
            {"type": "exp", "amount": exp},
        ],
        "branches": [],
        "tags": ["side", "chapter01", "npc_blacksmith"],
        "provenance": {
            "request": "给铁匠老陈加一条 6 级支线，去矿洞捡 4 块矿石",
            "evidence": ["wiki:world_overview#faction"],
            "model": "placeholder",
            "created_at": "2026-11-12T10:00:00+08:00",
        },
    }


def ok_report() -> ValidationReport:
    return ValidationReport(ok=True, checked=CheckedCounts(schema=1))


def bad_report(
    *,
    code: ErrorCode = ErrorCode.REWARD_RANGE,
    field: str = "rewards[0].amount",
    message: str = "Lv6 的任务金币奖励不得超过 200",
    constraint_ref: str = "C_REWARD_GOLD_01",
) -> ValidationReport:
    return ValidationReport(
        ok=False,
        checked=CheckedCounts(schema=1, business=1),
        errors=[
            ValidationErrorItem(
                code=code,
                severity=Severity.ERROR,
                field=field,
                message=message,
                constraint_ref=constraint_ref,
            )
        ],
    )


class FakeValidator:
    """Returns queued reports; the last one repeats once the queue is empty."""

    def __init__(self, results: Sequence[ValidationReport | BaseException]) -> None:
        if not results:
            raise ValueError("FakeValidator 至少需要一个结果")
        self.results = list(results)
        self.calls = 0
        self.seen: list[dict[str, Any]] = []

    def __call__(self, draft: Mapping[str, Any]) -> ValidationReport:
        self.seen.append(dict(draft))
        index = min(self.calls, len(self.results) - 1)
        self.calls += 1
        value = self.results[index]
        if isinstance(value, BaseException):
            raise value
        return value


def wiki_hit(position: str = "三、地点与通行", text: str | None = None) -> Hit:
    return make_hit(
        "wiki:world_overview",
        position,
        text
        or "废弃矿洞位于荒原哨站以东，是铁砧工会的采掘区；议会对其归属有争议。",
        source_type="wiki",
        score=0.042,
    )


def task_hit(
    task_id: str = "T_SIDE_0213",
    *,
    npc_id: str = "NPC_013",
    score: float = 0.031,
) -> Hit:
    return make_hit(
        f"task:{task_id}",
        "rewards",
        f"任务 {task_id} 的奖励（rewards）：金币 120、阅历 800。",
        source_type="task",
        score=score,
        task_id=task_id,
        npc_id=npc_id,
        level_min=6,
        level_max=9,
    )


def constraint_hit(
    constraint_id: str = "C_REWARD_GOLD_01",
    *,
    scope: str = "rewards.gold",
    score: float = 0.028,
) -> Hit:
    return make_hit(
        f"constraint:{constraint_id}",
        scope,
        f"约束 {constraint_id}：Lv10 以下任务金币奖励不得超过 200。",
        source_type="constraint",
        score=score,
        constraint_id=constraint_id,
        scope=scope,
        severity="error",
    )


def default_hits() -> list[Hit]:
    return [wiki_hit(), task_hit(), constraint_hit()]
