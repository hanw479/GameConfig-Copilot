"""Context builder tests (S3-01, S6-04): block budgets, priority truncation,
structured preference injection and explicit-instruction precedence."""

from __future__ import annotations

import json
from typing import Any

import pytest

from gameconfig_copilot.agents.context_builder import (
    BLOCK_BUDGETS,
    TOTAL_BUDGET,
    ContextBuilder,
    estimate_tokens,
)
from gameconfig_copilot.agents.state import Evidence, Preference
from gameconfig_copilot.memory.preference import JsonPreferenceStore
from gameconfig_copilot.memory.preference import Preference as MemoryPreference
from support.fakes import constraint_hit, make_hit, task_hit, wiki_hit

DESIGNER = "u_1024"


def _evidence(*hits) -> list[Evidence]:
    return [Evidence.from_hit(hit) for hit in hits]


def _learned(
    tmp_path: Any,
    *,
    gold: int = 120,
    task_type: str = "side",
    strictness: str = "high",
    naming: str = "snake_case",
) -> MemoryPreference:
    """Learn one designer preference through the real extraction path (S6-03)."""

    store = JsonPreferenceStore(tmp_path / "prefs")
    store.extract(
        {
            "designer_id": DESIGNER,
            "commit_id": "c1",
            "draft": {"task_type": task_type, "rewards": [{"type": "gold", "amount": gold}]},
            "signals": {"strictness": strictness, "naming_style": naming},
        }
    )
    return store.get(DESIGNER)


def test_estimate_tokens_is_conservative_for_cjk_and_ascii() -> None:
    assert estimate_tokens("中文") == 2
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("") == 0
    assert estimate_tokens("矿洞" * 100) >= 200


def test_all_blocks_fit_their_budget_and_total() -> None:
    builder = ContextBuilder()
    context = builder.build(
        "给铁匠老陈加一条 6 级支线",
        evidence=_evidence(wiki_hit(), task_hit(), constraint_hit()),
        constraints=_evidence(constraint_hit()),
        preference=Preference(reward_band={"gold": [80, 200]}, tags=("side",)),
    )

    assert [block.name for block in context.blocks] == [
        "system",
        "world",
        "tasks",
        "constraints",
        "preference",
    ]
    for block in context.blocks:
        assert block.budget == BLOCK_BUDGETS[block.name]
        assert block.tokens <= block.budget
        assert block.within_budget
    assert context.total_tokens <= TOTAL_BUDGET
    assert "TaskDraft" in context.block("system").text
    assert "LLMClient.structured" in context.block("system").text


def test_oversized_blocks_are_truncated_with_traceable_warnings() -> None:
    huge_wiki = [
        make_hit(
            f"wiki:lore_{index}",
            "section",
            "矿洞" * 900,
            source_type="wiki",
            score=1.0 - index * 0.01,
        )
        for index in range(6)
    ]
    huge_tasks = [
        make_hit(
            f"task:T_SIDE_02{index:02d}",
            "objectives",
            "任务目标：" + "采集矿石。" * 500,
            source_type="task",
            score=0.9 - index * 0.01,
            task_id=f"T_SIDE_02{index:02d}",
            npc_id="NPC_013",
        )
        for index in range(6)
    ]
    builder = ContextBuilder()
    context = builder.build(
        "给铁匠老陈加一条支线",
        evidence=_evidence(*huge_wiki, *huge_tasks),
        constraints=_evidence(constraint_hit()),
    )

    assert context.total_tokens <= TOTAL_BUDGET
    assert context.warnings
    truncated_blocks = {
        warning.block for warning in context.warnings if warning.code == "CONTEXT_TRUNCATED"
    }
    assert "tasks" in truncated_blocks
    tasks_block = context.block("tasks")
    assert tasks_block.truncated or tasks_block.dropped
    assert len(tasks_block.dropped) + len(tasks_block.truncated) >= 1


def test_global_budget_trims_lowest_priority_block_first() -> None:
    builder = ContextBuilder(total_budget=700)
    context = builder.build(
        "给铁匠老陈加一条 6 级支线",
        evidence=_evidence(wiki_hit(), task_hit(), constraint_hit()),
        constraints=_evidence(constraint_hit()),
        preference=Preference(
            reward_band={"gold": [80, 200]},
            naming="铁匠系列",
            tags=("side", "chapter01"),
            notes="偏好" * 100,
        ),
    )

    assert context.total_tokens <= 700
    assert context.block("system").tokens > 0
    assert context.block("preference").tokens == 0
    assert any(
        warning.block == "preference" and "global_budget" in warning.detail
        for warning in context.warnings
    )


def test_preference_block_never_exceeds_300_tokens() -> None:
    builder = ContextBuilder()
    context = builder.build(
        "生成任务",
        evidence=[],
        preference=Preference(notes="很长的偏好" * 500),
    )

    assert context.block("preference").tokens <= 300


def test_messages_have_system_and_user_roles() -> None:
    builder = ContextBuilder()
    context = builder.build("生成任务", evidence=_evidence(wiki_hit()))
    messages = context.to_messages("生成任务")

    assert [message.role for message in messages] == ["system", "user"]
    assert "TaskDraft" in messages[0].content
    assert "生成任务" in messages[1].content
    assert "[wiki:world_overview#三、地点与通行]" in messages[1].content


def test_unknown_preference_type_is_rejected() -> None:
    builder = ContextBuilder()
    with pytest.raises(TypeError):
        builder.build("生成任务", preference="preference as prose")  # type: ignore[arg-type]


def test_total_budget_is_6000_with_documented_block_budgets() -> None:
    assert TOTAL_BUDGET == 6000
    assert BLOCK_BUDGETS == {
        "system": 1200,
        "world": 1500,
        "tasks": 2500,
        "constraints": 1200,
        "preference": 300,
    }


# ---------------------------------------------------------------------------
# S6-04 preference injection: structured key-values, explicit precedence,
# 300-token block budget and 6000-token global budget.
# ---------------------------------------------------------------------------


def test_preference_block_injects_learned_defaults_as_structured_fields(
    tmp_path: Any,
) -> None:
    preference = _learned(tmp_path)
    context = ContextBuilder().build("给铁匠老陈加一条 6 级支线", preference=preference)

    block = context.block("preference")
    payload = json.loads(block.text)
    assert payload["designer_id"] == DESIGNER
    assert payload["priority"] == "explicit > preference"
    assert set(payload["defaults"]) == set(preference.defaults())
    for name, value in preference.defaults().items():
        assert payload["defaults"][name] == {"value": value, "source": "preference"}
    assert block.tokens <= BLOCK_BUDGETS["preference"]
    assert context.total_tokens == sum(item.tokens for item in context.blocks)


def test_preference_block_is_empty_without_preference_or_explicit() -> None:
    context = ContextBuilder().build("生成任务", evidence=_evidence(wiki_hit()))

    block = context.block("preference")
    assert block.text == ""
    assert block.tokens == 0
    assert block.dropped == ()
    assert [warning for warning in context.warnings if warning.block == "preference"] == []


def test_explicit_instruction_overrides_preference_and_is_marked(
    tmp_path: Any,
) -> None:
    preference = _learned(tmp_path)
    context = ContextBuilder().build(
        "这次给 500 金币，严格度低一点",
        preference=preference,
        explicit={"default_reward_band": 500, "strictness": "low"},
    )

    fields = json.loads(context.block("preference").text)["defaults"]
    assert fields["default_reward_band"] == {"value": 500, "source": "explicit"}
    assert fields["strictness"] == {"value": "low", "source": "explicit"}
    assert fields["naming_style"] == {"value": "snake_case", "source": "preference"}
    # The learned preference itself is never mutated by an explicit instruction.
    assert preference.defaults()["default_reward_band"] == {"min": 60, "max": 180}


def test_explicit_alias_names_resolve_to_the_canonical_field(tmp_path: Any) -> None:
    preference = _learned(tmp_path)
    context = ContextBuilder().build(
        "这次给 500 金币",
        preference=preference,
        explicit={"reward_band": 500, "naming": "camelCase"},
    )

    fields = json.loads(context.block("preference").text)["defaults"]
    assert fields["default_reward_band"] == {"value": 500, "source": "explicit"}
    assert fields["naming_style"] == {"value": "camelCase", "source": "explicit"}
    assert "reward_band" not in fields


def test_explicit_instruction_is_injected_without_learned_preference() -> None:
    context = ContextBuilder().build("这次给 500 金币", explicit={"default_reward_band": 500})

    fields = json.loads(context.block("preference").text)["defaults"]
    assert fields == {"default_reward_band": {"value": 500, "source": "explicit"}}


def test_unknown_explicit_type_is_rejected() -> None:
    builder = ContextBuilder()
    with pytest.raises(TypeError):
        builder.build("生成任务", explicit="给 500 金币")  # type: ignore[arg-type]


def test_preference_overflow_drops_learned_fields_with_warning(tmp_path: Any) -> None:
    preference = _learned(tmp_path)
    budgets = dict(BLOCK_BUDGETS)
    budgets["preference"] = 40
    context = ContextBuilder(budgets=budgets).build(
        "生成任务", preference=preference, explicit={"naming_style": "snake_case_custom"}
    )

    block = context.block("preference")
    assert block.tokens <= 40
    assert block.dropped
    payload = json.loads(block.text)
    # Explicit instructions are kept longest; learned fields are dropped first.
    assert payload["defaults"]["naming_style"] == {
        "value": "snake_case_custom",
        "source": "explicit",
    }
    assert "default_reward_band" in block.dropped
    warnings = [warning for warning in context.warnings if warning.block == "preference"]
    assert any(
        warning.code == "CONTEXT_TRUNCATED" and set(warning.detail) == set(block.dropped)
        for warning in warnings
    )


def test_prose_heavy_legacy_preference_is_dropped_at_300_tokens() -> None:
    context = ContextBuilder().build(
        "生成任务",
        preference=Preference(
            reward_band={"gold": [80, 200]},
            naming="铁匠系列",
            notes="很长的偏好" * 200,
        ),
    )

    block = context.block("preference")
    assert block.tokens <= BLOCK_BUDGETS["preference"]
    assert "notes" in block.dropped
    assert json.loads(block.text)["defaults"]["naming_style"]["value"] == "铁匠系列"
    assert any(
        warning.code == "CONTEXT_TRUNCATED" and warning.block == "preference"
        for warning in context.warnings
    )


def test_preference_block_counts_toward_the_6000_token_budget(tmp_path: Any) -> None:
    preference = _learned(tmp_path)
    context = ContextBuilder(total_budget=700).build(
        "给铁匠老陈加一条 6 级支线",
        evidence=_evidence(wiki_hit(), task_hit(), constraint_hit()),
        constraints=_evidence(constraint_hit()),
        preference=preference,
    )

    assert context.total_tokens == sum(item.tokens for item in context.blocks)
    assert context.total_tokens <= 700
    # Preference is the lowest-priority block, so the global pass empties it first.
    assert context.block("preference").tokens == 0
    assert any(
        warning.block == "preference" and "global_budget" in warning.detail
        for warning in context.warnings
    )


# ---------------------------------------------------------------------------
# Reward interval injection: both bounds come from constraint metadata (YAML)
# ---------------------------------------------------------------------------
def _reward_floor_hit(
    constraint_id: str = "C_REWARD_GOLD_MIN_01",
    scope: str = "rewards.gold",
    *,
    bands: list[dict[str, int]] | None = None,
) -> Any:
    bands = bands or [
        {"max_level": 10, "min_amount": 60, "max_amount": 200},
        {"max_level": 20, "min_amount": 150, "max_amount": 500},
        {"max_level": 30, "min_amount": 360, "max_amount": 1200},
    ]
    first = bands[0]
    return make_hit(
        f"constraint:{constraint_id}",
        scope,
        f"约束 {constraint_id}：{scope} 必须落在 "
        f"[{first['min_amount']}, {first['max_amount']}]。",
        source_type="constraint",
        constraint_id=constraint_id,
        constraint_type="level_band_range",
        scope=scope,
        bands=bands,
    )


def test_reward_interval_row_injects_both_bounds_for_the_request_level() -> None:
    context = ContextBuilder().build(
        "给铁匠老陈加一条 6 级支线",
        constraints=_evidence(_reward_floor_hit()),
    )

    constraints_text = context.block("constraints").text
    assert "reward-interval-gold" in constraints_text
    assert "[60, 200]" in constraints_text
    assert "Lv6" in constraints_text
    assert "E_REWARD_RANGE" in constraints_text
    assert context.block("constraints").within_budget


def test_reward_interval_row_picks_the_band_matching_the_request_level() -> None:
    context = ContextBuilder().build(
        "写一条 18 级日常",
        constraints=_evidence(_reward_floor_hit()),
    )

    hint = next(
        line
        for line in context.block("constraints").text.splitlines()
        if "reward-interval-gold" in line
    )
    assert "[150, 500]" in hint
    assert "[60, 200]" not in hint


def test_reward_interval_row_is_skipped_without_band_metadata() -> None:
    context = ContextBuilder().build(
        "给铁匠老陈加一条 6 级支线",
        constraints=_evidence(constraint_hit()),
    )

    assert "reward-interval" not in context.block("constraints").text


def test_reward_interval_rows_stay_within_the_6000_token_budget() -> None:
    context = ContextBuilder().build(
        "写一条 22 到 27 级日常",
        evidence=_evidence(wiki_hit(), task_hit()),
        constraints=_evidence(
            _reward_floor_hit(),
            _reward_floor_hit(
                "C_REWARD_EXP_MIN_01",
                "rewards.exp",
                bands=[
                    {"max_level": 10, "min_amount": 480, "max_amount": 1600},
                    {"max_level": 20, "min_amount": 1200, "max_amount": 4000},
                    {"max_level": 30, "min_amount": 2880, "max_amount": 9600},
                ],
            ),
        ),
    )

    text = context.block("constraints").text
    assert "reward-interval-gold" in text
    assert "reward-interval-exp" in text
    assert context.total_tokens <= TOTAL_BUDGET
