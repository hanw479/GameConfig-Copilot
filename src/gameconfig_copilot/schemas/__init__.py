"""Frozen cross-layer data contracts and JSON Schema export.

Canonical schema names are ``task`` / ``report`` / ``constraint``; a few
descriptive aliases are accepted for ergonomics.

Owner: Foundation Worker F (see ``docs/06-multi-agent-collaboration.md`` §5.2).
Changes to these models require the contract-change process (ADR).
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from .constraint import (
    Constraint,
    ConstraintType,
    LevelBand,
    export_constraint_schema,
)
from .report import (
    ERROR_CODES,
    WARNING_CODES,
    CheckedCounts,
    ErrorCode,
    Severity,
    Suggestion,
    ValidationErrorItem,
    ValidationReport,
    export_validation_report_schema,
)
from .task import (
    Branch,
    Giver,
    LevelRange,
    Objective,
    ObjectiveType,
    Prerequisite,
    PrerequisiteType,
    Provenance,
    Reward,
    RewardType,
    TaskDraft,
    TaskType,
    export_task_draft_schema,
)

__all__ = [
    "ERROR_CODES",
    "WARNING_CODES",
    "Branch",
    "CheckedCounts",
    "Constraint",
    "ConstraintType",
    "ErrorCode",
    "Giver",
    "LevelBand",
    "LevelRange",
    "Objective",
    "ObjectiveType",
    "Prerequisite",
    "PrerequisiteType",
    "Provenance",
    "Reward",
    "RewardType",
    "Severity",
    "Suggestion",
    "TaskDraft",
    "TaskType",
    "ValidationErrorItem",
    "ValidationReport",
    "available_schemas",
    "dump_json_schema",
    "export_all_schemas",
    "export_constraint_schema",
    "export_json_schema",
    "export_task_draft_schema",
    "export_validation_report_schema",
]

CANONICAL_SCHEMAS: dict[str, type[BaseModel]] = {
    "task": TaskDraft,
    "report": ValidationReport,
    "constraint": Constraint,
}

SCHEMA_EXPORTERS = {
    "task": export_task_draft_schema,
    "report": export_validation_report_schema,
    "constraint": export_constraint_schema,
}

SCHEMA_ALIASES = {
    "task_draft": "task",
    "taskdraft": "task",
    "validation_report": "report",
    "validationreport": "report",
    "constraints": "constraint",
}


def available_schemas() -> list[str]:
    """Return canonical schema names in stable order."""

    return list(CANONICAL_SCHEMAS)


def resolve_schema_name(name: str) -> str:
    """Normalize a user-supplied schema name or raise ``KeyError``."""

    key = name.strip().lower()
    key = SCHEMA_ALIASES.get(key, key)
    if key not in CANONICAL_SCHEMAS:
        options = ", ".join(available_schemas())
        raise KeyError(f"未知 schema {name!r}；可用：{options}")
    return key


def export_json_schema(name: str = "task") -> dict[str, Any]:
    """Export one JSON Schema document by canonical name."""

    key = resolve_schema_name(name)
    return SCHEMA_EXPORTERS[key]()


def export_all_schemas() -> dict[str, dict[str, Any]]:
    """Export every contract schema keyed by canonical name."""

    return {key: exporter() for key, exporter in SCHEMA_EXPORTERS.items()}


def dump_json_schema(name: str = "task", *, indent: int = 2) -> str:
    """Serialize one JSON Schema document (UTF-8 friendly)."""

    return json.dumps(export_json_schema(name), ensure_ascii=False, indent=indent)
