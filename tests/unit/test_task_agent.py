"""TaskAgent tests (S3-02): structured retry, rule-allocated IDs, provenance."""

from __future__ import annotations

import pytest

from gameconfig_copilot.agents.state import Evidence
from gameconfig_copilot.agents.task_agent import (
    TaskAgent,
    TaskGenerationError,
    TaskIdAllocator,
)
from gameconfig_copilot.schemas import (
    CheckedCounts,
    ErrorCode,
    Severity,
    TaskType,
    ValidationErrorItem,
    ValidationReport,
)
from support.fakes import (
    FakeLLM,
    bad_report,
    default_hits,
    valid_task_payload,
)


def _evidence() -> list[Evidence]:
    return [Evidence.from_hit(hit) for hit in default_hits()]


def test_structured_output_is_retried_once_then_succeeds() -> None:
    llm = FakeLLM(
        structured_payload=valid_task_payload(),
        structured_errors_before_success=1,
    )
    agent = TaskAgent(llm)
    result = agent.generate(
        "给铁匠老陈加一条 6 级支线",
        evidence=_evidence(),
        existing_ids=["T_SIDE_0213", "T_SIDE_0216"],
    )

    assert result.attempts == 2
    assert len(llm.structured_calls) == 2
    assert result.draft["task_id"] == "T_SIDE_0217"
    retry_messages = llm.structured_calls[1]["messages"]
    assert any("Schema 校验" in message.content for message in retry_messages)


def test_structured_output_failure_after_retry_raises() -> None:
    llm = FakeLLM(
        structured_payload=valid_task_payload(),
        structured_errors_before_success=2,
    )
    agent = TaskAgent(llm)

    with pytest.raises(TaskGenerationError) as excinfo:
        agent.generate("给铁匠老陈加一条支线", evidence=_evidence())

    error = excinfo.value
    assert error.code == "E_SCHEMA_002"
    assert error.attempts == 2
    assert len(llm.structured_calls) == 2


def test_placeholder_task_id_is_replaced_by_allocator() -> None:
    llm = FakeLLM(structured_payload=valid_task_payload(task_id="T_SIDE_0000"))
    agent = TaskAgent(llm)
    result = agent.generate(
        "给铁匠老陈加一条 6 级支线",
        evidence=_evidence(),
        existing_ids=["T_SIDE_0216"],
    )

    assert result.draft["task_id"] == "T_SIDE_0217"
    assert result.task.task_id == "T_SIDE_0217"


def test_provenance_is_rewritten_with_evidence_citations() -> None:
    llm = FakeLLM(structured_payload=valid_task_payload())
    agent = TaskAgent(llm)
    result = agent.generate("给铁匠老陈加一条 6 级支线", evidence=_evidence())

    provenance = result.draft["provenance"]
    assert provenance["request"] == "给铁匠老陈加一条 6 级支线"
    assert provenance["model"] == "fake-model"
    assert provenance["created_at"]
    assert "wiki:world_overview#三、地点与通行" in provenance["evidence"]
    assert provenance["evidence"] == [
        item.citation for item in _evidence()
    ][: len(provenance["evidence"])]


def test_repair_keeps_original_task_id_and_uses_report() -> None:
    llm = FakeLLM(structured_payload=valid_task_payload())
    agent = TaskAgent(llm)
    generated = agent.generate(
        "给铁匠老陈加一条 6 级支线",
        evidence=_evidence(),
        existing_ids=["T_SIDE_0216"],
    )
    repaired = agent.repair(
        "给铁匠老陈加一条 6 级支线",
        generated.draft,
        bad_report().to_dict(),
        evidence=_evidence(),
    )

    assert repaired.draft["task_id"] == "T_SIDE_0217"
    repair_messages = llm.structured_calls[-1]["messages"]
    prompt = "\n".join(message.content for message in repair_messages)
    # docs/03 S4-05: original request + current draft + errors + constraint text.
    assert "## 原始需求" in prompt
    assert "给铁匠老陈加一条 6 级支线" in prompt
    assert "## 当前草案" in prompt
    assert "## 校验错误（仅 severity=error" in prompt
    assert "E_REWARD_RANGE" in prompt
    assert "rewards[0].amount" in prompt
    assert "## 相关约束原文" in prompt
    assert "C_REWARD_GOLD_01" in prompt
    assert "Lv10 以下任务金币奖励不得超过 200" in prompt


def test_repair_prompt_omits_warnings_and_only_lists_errors() -> None:
    report = ValidationReport(
        ok=False,
        checked=CheckedCounts(schema=1, business=1),
        errors=[
            ValidationErrorItem(
                code=ErrorCode.REWARD_RANGE,
                field="rewards[0].amount",
                message="金币奖励越界",
                constraint_ref="C_REWARD_GOLD_01",
            )
        ],
        warnings=[
            ValidationErrorItem(
                code=ErrorCode.DIALOGUE_LONG,
                severity=Severity.WARNING,
                field="giver.dialogue_ref",
                message="对白过长（warning 不应回灌修复）",
            )
        ],
    )
    llm = FakeLLM(structured_payload=valid_task_payload())
    agent = TaskAgent(llm)
    agent.repair(
        "给铁匠老陈加一条 6 级支线",
        valid_task_payload(task_id="T_SIDE_0217"),
        report.to_dict(),
        evidence=_evidence(),
    )

    prompt = "\n".join(
        message.content for message in llm.structured_calls[-1]["messages"]
    )
    assert "对白过长" not in prompt
    assert "warning 属提示级" in prompt


def test_repair_without_previous_report_is_rejected_without_llm_call() -> None:
    llm = FakeLLM(structured_payload=valid_task_payload())
    agent = TaskAgent(llm)

    with pytest.raises(TaskGenerationError) as excinfo:
        agent.repair(
            "给铁匠老陈加一条 6 级支线",
            valid_task_payload(task_id="T_SIDE_0217"),
            {},
            evidence=_evidence(),
        )

    assert excinfo.value.code == "E_VALIDATION_REQUIRED"
    assert llm.structured_calls == []


def test_repair_with_warning_only_report_is_not_a_blind_retry() -> None:
    report = ValidationReport(
        ok=True,
        checked=CheckedCounts(schema=1),
        warnings=[
            ValidationErrorItem(
                code=ErrorCode.DIALOGUE_LONG,
                severity=Severity.WARNING,
                field="giver.dialogue_ref",
                message="对白过长",
            )
        ],
    )
    llm = FakeLLM(structured_payload=valid_task_payload())
    agent = TaskAgent(llm)

    with pytest.raises(TaskGenerationError) as excinfo:
        agent.repair(
            "给铁匠老陈加一条 6 级支线",
            valid_task_payload(task_id="T_SIDE_0217"),
            report.to_dict(),
            evidence=_evidence(),
        )

    assert excinfo.value.code == "E_VALIDATION_REQUIRED"
    assert llm.structured_calls == []


def test_task_id_allocator_uses_max_sequence_per_type() -> None:
    allocator = TaskIdAllocator(["T_SIDE_0213", "T_MAIN_0102", "T_DAILY_0303"])

    assert allocator.next_id(TaskType.SIDE) == "T_SIDE_0214"
    assert allocator.next_id(TaskType.MAIN) == "T_MAIN_0103"
    assert allocator.next_id(TaskType.DAILY) == "T_DAILY_0304"


def test_invalid_task_type_is_rejected() -> None:
    payload = valid_task_payload()
    payload["task_type"] = "raid"
    agent = TaskAgent(FakeLLM(structured_payload=payload))

    with pytest.raises(TaskGenerationError) as excinfo:
        agent.generate("给铁匠老陈加一条支线", evidence=_evidence())

    assert excinfo.value.code == "E_SCHEMA_002"


def test_task_prompt_forbids_model_supplied_ids() -> None:
    from gameconfig_copilot.agents.prompt_loader import load_prompt

    prompt = load_prompt("task")
    assert "T_<MAIN|SIDE|DAILY>_0000" in prompt
    assert "禁止自行编造任务 ID" in prompt
    assert "provenance" in prompt
