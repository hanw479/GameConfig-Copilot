"""Constraint DSL contract tests (S0-04, docs/01 §7.3)."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from gameconfig_copilot.schemas import Constraint, ConstraintType, export_constraint_schema

RULE_TYPES = {
    "level_band_max",
    "level_band_range",
    "range",
    "enum",
    "unique",
    "reference_exists",
    "dependency_order",
    "sum_limit",
    "conflict",
    "regex",
    "length",
}


def test_yaml_example_parses() -> None:
    constraint = Constraint.model_validate(
        {
            "id": "C_REWARD_GOLD_01",
            "scope": "rewards.gold",
            "type": "level_band_max",
            "bands": [
                {"max_level": 10, "max_amount": 200},
                {"max_level": 20, "max_amount": 500},
                {"max_level": 30, "max_amount": 1200},
            ],
            "severity": "error",
            "message": "Lv{level} 的任务金币奖励不得超过 {limit}",
            "rationale": "控制经济通胀，见《星港纪元》经济白皮书 §3.2",
        }
    )
    assert constraint.type is ConstraintType.LEVEL_BAND_MAX
    assert constraint.bands[0].max_amount == 200


@pytest.mark.parametrize("rule_type", sorted(RULE_TYPES))
def test_every_mvp_rule_type_is_accepted(rule_type: str) -> None:
    payload: dict[str, object] = {
        "id": "C_TEST_01",
        "scope": "rewards",
        "type": rule_type,
        "message": "rule {value}",
        "params": {"min": 0, "max": 10},
        "target": "tasks",
    }
    if rule_type == "level_band_max":
        payload["bands"] = [{"max_level": 10, "max_amount": 200}]
    elif rule_type == "level_band_range":
        payload["bands"] = [{"max_level": 10, "min_amount": 60, "max_amount": 200}]
    constraint = Constraint.model_validate(payload)
    assert constraint.type.value == rule_type


def test_level_band_range_parses_min_and_max() -> None:
    constraint = Constraint.model_validate(
        {
            "id": "C_REWARD_GOLD_MIN_01",
            "scope": "rewards.gold",
            "type": "level_band_range",
            "message": "Lv{level} 必须在 {min}–{max} 之间",
            "bands": [{"max_level": 10, "min_amount": 60, "max_amount": 200}],
        }
    )
    assert constraint.type is ConstraintType.LEVEL_BAND_RANGE
    assert constraint.bands[0].min_amount == 60
    assert constraint.bands[0].max_amount == 200


def test_level_band_range_requires_min_amount() -> None:
    with pytest.raises(ValidationError, match="min_amount"):
        Constraint.model_validate(
            {
                "id": "C_REWARD_GOLD_MIN_01",
                "scope": "rewards.gold",
                "type": "level_band_range",
                "message": "range",
                "bands": [{"max_level": 10, "max_amount": 200}],
            }
        )


def test_level_band_range_rejects_inverted_bounds() -> None:
    with pytest.raises(ValidationError, match="min_amount"):
        Constraint.model_validate(
            {
                "id": "C_REWARD_GOLD_MIN_01",
                "scope": "rewards.gold",
                "type": "level_band_range",
                "message": "range",
                "bands": [{"max_level": 10, "min_amount": 300, "max_amount": 200}],
            }
        )


def test_level_band_max_rejects_min_amount() -> None:
    with pytest.raises(ValidationError, match="min_amount"):
        Constraint.model_validate(
            {
                "id": "C_REWARD_GOLD_01",
                "scope": "rewards.gold",
                "type": "level_band_max",
                "message": "limit",
                "bands": [{"max_level": 10, "min_amount": 60, "max_amount": 200}],
            }
        )


def test_level_band_range_requires_bands() -> None:
    with pytest.raises(ValidationError, match="bands"):
        Constraint.model_validate(
            {
                "id": "C_REWARD_GOLD_MIN_01",
                "scope": "rewards.gold",
                "type": "level_band_range",
                "message": "range",
            }
        )


def test_level_band_max_requires_bands() -> None:
    with pytest.raises(ValidationError, match="bands"):
        Constraint.model_validate(
            {
                "id": "C_REWARD_GOLD_01",
                "scope": "rewards.gold",
                "type": "level_band_max",
                "message": "limit",
            }
        )


def test_bands_are_only_allowed_for_level_band_max() -> None:
    with pytest.raises(ValidationError, match="bands"):
        Constraint.model_validate(
            {
                "id": "C_TEST_01",
                "scope": "rewards",
                "type": "range",
                "message": "limit",
                "bands": [{"max_level": 10, "max_amount": 200}],
            }
        )


def test_duplicate_band_levels_are_rejected() -> None:
    with pytest.raises(ValidationError, match="max_level"):
        Constraint.model_validate(
            {
                "id": "C_TEST_01",
                "scope": "rewards",
                "type": "level_band_max",
                "message": "limit",
                "bands": [
                    {"max_level": 10, "max_amount": 200},
                    {"max_level": 10, "max_amount": 300},
                ],
            }
        )


@pytest.mark.parametrize("constraint_id", ["C_lowercase", "X_REWARD", "C-REWARD"])
def test_constraint_id_pattern_is_enforced(constraint_id: str) -> None:
    with pytest.raises(ValidationError):
        Constraint.model_validate(
            {
                "id": constraint_id,
                "scope": "rewards",
                "type": "range",
                "message": "limit",
            }
        )


def test_unknown_rule_type_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Constraint.model_validate(
            {
                "id": "C_TEST_01",
                "scope": "rewards",
                "type": "made_up_rule",
                "message": "limit",
            }
        )


def test_exported_constraint_schema_has_closed_rule_enum() -> None:
    schema = export_constraint_schema()
    assert schema["$id"] == "urn:gameconfig-copilot:schema:constraint:1"
    assert set(schema["$defs"]["ConstraintType"]["enum"]) == RULE_TYPES
    assert schema["additionalProperties"] is False
    json.dumps(schema, ensure_ascii=False)
