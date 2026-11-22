"""Supervisor routing tests (S2-01): 20-sample rule accuracy + small-model seam."""

from __future__ import annotations

from gameconfig_copilot.agents.state import Intent
from gameconfig_copilot.agents.supervisor import Supervisor, classify_intent
from gameconfig_copilot.llm import LLMStructuredOutputError
from support.fakes import FakeLLM

SAMPLES: list[tuple[str, Intent]] = [
    ("铁匠老陈对星港议会是什么立场？", Intent.QUERY),
    ("矿洞在哪个地图？", Intent.QUERY),
    ("金币奖励上限是多少？", Intent.QUERY),
    ("介绍一下深井教团", Intent.QUERY),
    ("有没有类似失踪矿车的支线？", Intent.QUERY),
    ("T_SIDE_0213 的前置任务是什么？", Intent.QUERY),
    ("为什么 10 级任务金币不能超过 200？", Intent.QUERY),
    ("阿岚和铁匠老陈是什么关系？", Intent.QUERY),
    ("日常任务每天能做几次？", Intent.QUERY),
    ("知识库里有关于荒原哨站的设定吗？", Intent.QUERY),
    ("给铁匠老陈加一条 6 级支线，去矿洞捡 4 块矿石", Intent.TASK),
    ("生成一个 1 级主线任务，奖励 1000 金币", Intent.TASK),
    ("帮我设计一个日常任务，目标是采集 6 块铁尘矿石", Intent.TASK),
    ("新建支线：去星港城找议长对话", Intent.TASK),
    ("把 T_SIDE_0213 的奖励改成 200 金币", Intent.TASK),
    ("给矿工阿岚做一个 10 级日常采集任务", Intent.TASK),
    ("写一条主线，要求完成前置任务 T_MAIN_0102", Intent.TASK),
    ("你好", Intent.OTHER),
    ("今天天气怎么样？", Intent.OTHER),
    ("谢谢，先这样", Intent.OTHER),
]


def test_rule_intent_accuracy_is_at_least_90_percent() -> None:
    mismatches: list[str] = []
    for text, expected in SAMPLES:
        actual = classify_intent(text)
        if actual.intent is not expected:
            mismatches.append(
                f"{text!r}: expected={expected.value} actual={actual.intent.value} "
                f"reason={actual.reason}"
            )
    accuracy = (len(SAMPLES) - len(mismatches)) / len(SAMPLES)

    assert len(SAMPLES) >= 20
    assert accuracy >= 0.9, "准确率不足 90%：\n" + "\n".join(mismatches)


def test_confident_rule_skips_small_model() -> None:
    llm = FakeLLM(structured_payload={"intent": "task"})
    decision = Supervisor(llm=llm).route("金币奖励上限是多少？")

    assert decision.intent is Intent.QUERY
    assert decision.source == "rule"
    assert llm.structured_calls == []


def test_low_confidence_rule_defers_to_small_model() -> None:
    llm = FakeLLM(structured_payload={"intent": "task"})
    decision = Supervisor(llm=llm).route("T_SIDE_0213")

    assert decision.intent is Intent.TASK
    assert decision.source == "llm"
    assert len(llm.structured_calls) == 1


def test_small_model_failure_falls_back_to_rules() -> None:
    llm = FakeLLM(
        structured_error=LLMStructuredOutputError(
            "router unavailable",
            schema_name="route",
        )
    )
    decision = Supervisor(llm=llm).route("T_SIDE_0213")

    assert decision.intent is Intent.OTHER
    assert decision.source == "fallback"
    assert "回退规则" in decision.reason


def test_out_of_domain_question_routes_to_other() -> None:
    decision = classify_intent("今天天气怎么样？")

    assert decision.intent is Intent.OTHER
    assert decision.confidence < 0.65


def test_empty_request_is_other_with_zero_confidence() -> None:
    decision = classify_intent("   ")

    assert decision.intent is Intent.OTHER
    assert decision.confidence == 0.0
