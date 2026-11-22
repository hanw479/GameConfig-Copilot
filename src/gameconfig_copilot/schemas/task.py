"""TaskDraft contract — the single source of truth for task configuration.

Aligned with ``docs/01-product-outline.md`` §7.1. The same Pydantic model is
used for runtime validation and for the JSON Schema injected into LLM prompts,
so "documented contract" and "validated contract" can never drift.

Input
    JSON-compatible dict / keyword arguments.
Output
    A validated :class:`TaskDraft`; :func:`export_task_draft_schema` returns the
    JSON Schema document.
Failure semantics
    Invalid enums, malformed IDs, non-contiguous objective ``seq`` or extra
    fields raise :class:`pydantic.ValidationError` — they are never silently
    coerced.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from ._base import ContractModel, export_model_schema

TASK_ID_PATTERN = r"^T_(MAIN|SIDE|DAILY)_\d{4}$"
TASK_ID_RE = re.compile(TASK_ID_PATTERN)


class TaskType(str, Enum):
    MAIN = "main"
    SIDE = "side"
    DAILY = "daily"


class ObjectiveType(str, Enum):
    TALK_TO = "talk_to"
    COLLECT = "collect"
    KILL = "kill"
    INTERACT = "interact"
    REACH = "reach"


class RewardType(str, Enum):
    GOLD = "gold"
    EXP = "exp"
    ITEM = "item"


class PrerequisiteType(str, Enum):
    TASK_COMPLETED = "task_completed"


class LevelRange(ContractModel):
    min: int = Field(ge=1, description="Minimum required player level.")
    max: int = Field(ge=1, description="Maximum supported player level.")

    @model_validator(mode="after")
    def _min_not_greater_than_max(self) -> LevelRange:
        if self.min > self.max:
            raise ValueError(f"level_range.min ({self.min}) 不能大于 max ({self.max})")
        return self


class Giver(ContractModel):
    npc_id: str = Field(min_length=1, pattern=r"^NPC_[A-Za-z0-9_]+$")
    dialogue_ref: str | None = Field(
        default=None,
        pattern=r"^DLG_[A-Za-z0-9_]+$",
        description="Dialogue asset reference; optional for NPCs without lines.",
    )


class Prerequisite(ContractModel):
    type: PrerequisiteType = PrerequisiteType.TASK_COMPLETED
    ref: str = Field(min_length=1, description="Referenced task ID.")


class Objective(ContractModel):
    seq: int = Field(ge=1, description="1-based sequence, contiguous within the draft.")
    type: ObjectiveType
    target: str = Field(min_length=1, description="NPC / item / map entity ID.")
    count: int = Field(default=1, ge=1)
    area: str = Field(min_length=1, description="Map or area ID where the objective happens.")


class Reward(ContractModel):
    type: RewardType
    amount: int = Field(ge=0)
    item_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _item_rewards_need_item_id(self) -> Reward:
        if self.type is RewardType.ITEM and not self.item_id:
            raise ValueError("type=item 的奖励必须提供 item_id")
        if self.type is not RewardType.ITEM and self.item_id is not None:
            raise ValueError("只有 type=item 的奖励才允许设置 item_id")
        return self


class Branch(ContractModel):
    branch_id: str = Field(min_length=1)
    condition: str = Field(min_length=1, description="Human-readable branching condition.")
    objectives: list[Objective] = Field(default_factory=list)
    rewards: list[Reward] = Field(default_factory=list)


class Provenance(ContractModel):
    request: str = Field(min_length=1, description="Original natural-language request.")
    evidence: list[str] = Field(
        default_factory=list,
        description="Evidence refs such as 'wiki:world_overview#faction'.",
    )
    model: str = Field(min_length=1, description="Model identifier that produced the draft.")
    created_at: datetime = Field(description="Timezone-aware creation timestamp.")


class TaskDraft(ContractModel):
    """Structured task draft (contract version 1)."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "schema_version": 1,
                    "task_id": "T_SIDE_0213",
                    "title": "铁匠的委托：失踪的矿车",
                    "task_type": "side",
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
                    "rewards": [{"type": "gold", "amount": 120}, {"type": "exp", "amount": 800}],
                    "branches": [],
                    "tags": ["side", "chapter01", "npc_blacksmith"],
                    "provenance": {
                        "request": "给铁匠老陈加一条 6 级支线，去矿洞捡 4 块矿石",
                        "evidence": [
                            "wiki:world_overview#faction",
                            "task:T_MAIN_0102#objectives",
                        ],
                        "model": "deepseek-chat",
                        "created_at": "2026-11-12T10:00:00+08:00",
                    },
                }
            ]
        }
    )

    schema_version: Literal[1] = 1
    task_id: str = Field(pattern=TASK_ID_PATTERN)
    title: str = Field(min_length=1, max_length=80)
    task_type: TaskType
    level_range: LevelRange
    giver: Giver
    prerequisites: list[Prerequisite]
    objectives: list[Objective] = Field(min_length=1)
    rewards: list[Reward]
    branches: list[Branch]
    tags: list[str] = Field(default_factory=list)
    provenance: Provenance

    @field_validator("task_id")
    @classmethod
    def _task_id_is_well_formed(cls, value: str) -> str:
        if not TASK_ID_RE.fullmatch(value):
            raise ValueError("task_id 必须匹配 ^T_(MAIN|SIDE|DAILY)_\\d{4}$")
        return value

    @model_validator(mode="after")
    def _objective_seq_is_contiguous(self) -> TaskDraft:
        seqs = [objective.seq for objective in self.objectives]
        expected = list(range(1, len(seqs) + 1))
        if seqs != expected:
            raise ValueError(f"objectives[].seq 必须从 1 连续递增，当前为 {seqs}")
        return self


def export_task_draft_schema() -> dict[str, Any]:
    """Export the TaskDraft JSON Schema for prompt injection and CLI dump."""

    return export_model_schema(
        TaskDraft,
        schema_id="urn:gameconfig-copilot:schema:task-draft:1",
    )
