"""Schema-layer validation used by the graph's ``validate`` node (S3/S4 seam).

S4's deterministic rule engine (`mcp_server/validator/`) is owned by another
worker; until it is wired in, the graph validates against the frozen
``TaskDraft`` contract and reports ``E_SCHEMA_*`` items. The validator is an
injectable callable so Wave 3 can swap in the full engine without touching the
graph. Any validator exception is treated as *not passed* (fail-closed,
``E_VALIDATOR_CRASH`` — docs/02 §12).

Input
    A draft mapping.
Output
    :class:`~gameconfig_copilot.schemas.ValidationReport`.
Failure semantics
    Never raises: malformed drafts become ``ok=False`` reports.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from ..schemas import (
    CheckedCounts,
    ErrorCode,
    Severity,
    TaskDraft,
    ValidationErrorItem,
    ValidationReport,
)

__all__ = ["fail_closed_report", "schema_validate"]


def _error_code(error: Mapping[str, Any]) -> ErrorCode:
    if str(error.get("type")) in {"missing", "value_error.missing"}:
        return ErrorCode.SCHEMA_MISSING_FIELD
    return ErrorCode.SCHEMA_INVALID_VALUE


def _field_path(error: Mapping[str, Any]) -> str:
    location = error.get("loc") or ()
    path = ".".join(str(part) for part in location)
    return path or "draft"


def schema_validate(draft: Mapping[str, Any]) -> ValidationReport:
    """Validate ``draft`` against :class:`TaskDraft` and build a report."""

    try:
        TaskDraft.model_validate(dict(draft))
    except ValidationError as exc:
        items = [
            ValidationErrorItem(
                code=_error_code(error),
                severity=Severity.ERROR,
                field=_field_path(error),
                message=str(error.get("msg", "Schema 校验失败")),
            )
            for error in exc.errors()
        ]
        return ValidationReport(
            ok=False,
            checked=CheckedCounts(schema=1),
            errors=items,
        )
    except Exception as exc:  # pragma: no cover - defensive
        return fail_closed_report(exc)
    return ValidationReport(ok=True, checked=CheckedCounts(schema=1))


def fail_closed_report(exc: BaseException) -> ValidationReport:
    """Turn a validator crash into a blocking report (fail-closed)."""

    return ValidationReport(
        ok=False,
        checked=CheckedCounts(schema=0),
        errors=[
            ValidationErrorItem(
                code=ErrorCode.VALIDATOR_CRASH,
                severity=Severity.ERROR,
                field="draft",
                message=f"校验器异常，按不通过处理：{type(exc).__name__}: {exc}",
            )
        ],
    )
