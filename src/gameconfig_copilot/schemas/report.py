"""ValidationReport contract and the frozen ``E_*`` / ``W_*`` error codes.

Error codes and their "auto-fixable" semantics are frozen in
``docs/01-product-outline.md`` §7.4. Downstream layers (validator, repair node,
evaluation) must use :class:`ErrorCode` instead of string literals so a typo
fails at import time.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import ConfigDict, Field, model_validator

from ._base import ContractModel, export_model_schema


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


class ErrorCode(str, Enum):
    """All codes from docs/01 §7.4 (13 errors + 2 warnings)."""

    SCHEMA_MISSING_FIELD = "E_SCHEMA_001"
    SCHEMA_INVALID_VALUE = "E_SCHEMA_002"
    REF_MISSING = "E_REF_MISSING"
    DUP_ID = "E_DUP_ID"
    REWARD_RANGE = "E_REWARD_RANGE"
    LEVEL_RANGE = "E_LEVEL_RANGE"
    DEP_CYCLE = "E_DEP_CYCLE"
    PATH_DENIED = "E_PATH_DENIED"
    STALE_REVISION = "E_STALE_REVISION"
    FINGERPRINT_MISMATCH = "E_FINGERPRINT_MISMATCH"
    VALIDATION_REQUIRED = "E_VALIDATION_REQUIRED"
    VALIDATOR_CRASH = "E_VALIDATOR_CRASH"
    IO = "E_IO"
    DIALOGUE_LONG = "W_DIALOGUE_LONG"
    DIALOGUE_ASSIGN_CONFLICT = "W_DIALOGUE_ASSIGN_CONFLICT"


WARNING_CODES: frozenset[ErrorCode] = frozenset(
    {ErrorCode.DIALOGUE_LONG, ErrorCode.DIALOGUE_ASSIGN_CONFLICT}
)
ERROR_CODES: frozenset[ErrorCode] = frozenset(ErrorCode) - WARNING_CODES


class Suggestion(ContractModel):
    field: str = Field(min_length=1, description="Field path to patch, e.g. rewards[0].amount.")
    value: Any = Field(description="Suggested replacement value.")


class ValidationErrorItem(ContractModel):
    code: ErrorCode
    severity: Severity = Severity.ERROR
    field: str = Field(
        default="",
        description="Dotted/indexed field path, empty for global errors.",
    )
    message: str = Field(min_length=1)
    constraint_ref: str | None = Field(
        default=None,
        description="Constraint ID that produced the error, e.g. C_REWARD_GOLD_01.",
    )
    suggestion: Suggestion | None = None


class CheckedCounts(ContractModel):
    """How many checks ran, by layer (wire field names: schema/business/conflict)."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    schema_count: int = Field(default=0, ge=0, alias="schema")
    business: int = Field(default=0, ge=0)
    conflict: int = Field(default=0, ge=0)


class ValidationReport(ContractModel):
    """Deterministic validation outcome; ``ok`` must never contradict ``errors``."""

    ok: bool
    checked: CheckedCounts = Field(default_factory=CheckedCounts)
    errors: list[ValidationErrorItem] = Field(default_factory=list)
    warnings: list[ValidationErrorItem] = Field(default_factory=list)
    duration_ms: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _ok_matches_errors(self) -> ValidationReport:
        if self.ok and self.errors:
            raise ValueError("ok=True 的报告中不允许出现 errors")
        return self

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready dict using contract field names (``checked.schema``)."""

        return self.model_dump(mode="json", by_alias=True)


def export_validation_report_schema() -> dict[str, Any]:
    """Export the ValidationReport JSON Schema."""

    return export_model_schema(
        ValidationReport,
        schema_id="urn:gameconfig-copilot:schema:validation-report:1",
    )
