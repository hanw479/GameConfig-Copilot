"""TaskDraft contract tests (S0-03): valid/invalid payloads + schema export."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from pydantic import ValidationError

from gameconfig_copilot.schemas import TaskDraft, export_task_draft_schema


def _mutated(payload: dict[str, Any], **updates: Any) -> dict[str, Any]:
    mutated = copy.deepcopy(payload)
    mutated.update(updates)
    return mutated


def test_valid_payload_roundtrip(task_payload: dict[str, Any]) -> None:
    draft = TaskDraft.model_validate(task_payload)
    assert draft.task_id == "T_SIDE_0213"
    assert draft.objectives[1].count == 4
    assert draft.branches == []

    dumped = draft.model_dump(mode="json")
    assert dumped["provenance"]["created_at"].startswith("2026-11-12T10:00:00")
    assert TaskDraft.model_validate(dumped).rewards[0].amount == 120


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("task_type", "raid"),
        ("task_id", "T_RAID_0001"),
        ("task_id", "T_SIDE_21"),
        ("title", ""),
        ("schema_version", 2),
    ],
)
def test_invalid_scalar_fields_are_rejected(
    task_payload: dict[str, Any], field: str, value: Any
) -> None:
    with pytest.raises(ValidationError):
        TaskDraft.model_validate(_mutated(task_payload, **{field: value}))


def test_non_contiguous_objective_sequence_is_rejected(task_payload: dict[str, Any]) -> None:
    task_payload["objectives"][1]["seq"] = 3
    with pytest.raises(ValidationError, match="连续"):
        TaskDraft.model_validate(task_payload)


def test_level_range_order_is_rejected(task_payload: dict[str, Any]) -> None:
    task_payload["level_range"] = {"min": 9, "max": 6}
    with pytest.raises(ValidationError, match="level_range"):
        TaskDraft.model_validate(task_payload)


def test_extra_fields_are_rejected(task_payload: dict[str, Any]) -> None:
    task_payload["unexpected"] = True
    with pytest.raises(ValidationError, match="Extra inputs"):
        TaskDraft.model_validate(task_payload)


def test_missing_required_field_is_rejected(task_payload: dict[str, Any]) -> None:
    del task_payload["title"]
    with pytest.raises(ValidationError, match="title"):
        TaskDraft.model_validate(task_payload)


def test_item_reward_requires_item_id(task_payload: dict[str, Any]) -> None:
    task_payload["rewards"].append({"type": "item", "amount": 1})
    with pytest.raises(ValidationError, match="item_id"):
        TaskDraft.model_validate(task_payload)


def test_non_item_reward_rejects_item_id(task_payload: dict[str, Any]) -> None:
    task_payload["rewards"][0]["item_id"] = "ITEM_ORE_02"
    with pytest.raises(ValidationError, match="item_id"):
        TaskDraft.model_validate(task_payload)


def test_exported_schema_is_self_contained_json() -> None:
    schema = export_task_draft_schema()

    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"] == "urn:gameconfig-copilot:schema:task-draft:1"
    assert schema["additionalProperties"] is False
    assert {
        "task_id",
        "title",
        "task_type",
        "level_range",
        "giver",
        "prerequisites",
        "objectives",
        "rewards",
        "branches",
        "provenance",
    } <= set(schema["required"])

    definitions = schema["$defs"]
    assert definitions["TaskType"]["enum"] == ["main", "side", "daily"]
    assert definitions["ObjectiveType"]["enum"] == [
        "talk_to",
        "collect",
        "kill",
        "interact",
        "reach",
    ]
    assert schema["properties"]["objectives"]["minItems"] == 1
    assert schema["properties"]["task_id"]["pattern"] == r"^T_(MAIN|SIDE|DAILY)_\d{4}$"

    # Must survive a JSON round-trip exactly as the CLI emits it.
    assert json.loads(json.dumps(schema, ensure_ascii=False))["title"] == "TaskDraft"
