"""ValidationReport / ErrorCode contract tests (S0-04)."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from gameconfig_copilot.schemas import (
    ERROR_CODES,
    WARNING_CODES,
    ErrorCode,
    ValidationErrorItem,
    ValidationReport,
    export_validation_report_schema,
)

DOC_ERROR_CODES = {
    "E_SCHEMA_001",
    "E_SCHEMA_002",
    "E_REF_MISSING",
    "E_DUP_ID",
    "E_REWARD_RANGE",
    "E_LEVEL_RANGE",
    "E_DEP_CYCLE",
    "E_PATH_DENIED",
    "E_STALE_REVISION",
    "E_FINGERPRINT_MISMATCH",
    "E_VALIDATION_REQUIRED",
    "E_VALIDATOR_CRASH",
    "E_IO",
    "W_DIALOGUE_LONG",
    "W_DIALOGUE_ASSIGN_CONFLICT",
}


def test_error_codes_match_product_outline() -> None:
    warnings = {"W_DIALOGUE_LONG", "W_DIALOGUE_ASSIGN_CONFLICT"}
    assert {code.value for code in ErrorCode} == DOC_ERROR_CODES
    assert {code.value for code in ERROR_CODES} == DOC_ERROR_CODES - warnings
    assert {code.value for code in WARNING_CODES} == warnings
    assert not ERROR_CODES & WARNING_CODES


def test_report_roundtrip_uses_wire_field_names() -> None:
    report = ValidationReport(
        ok=False,
        checked={"schema": 12, "business": 34, "conflict": 5},
        errors=[
            ValidationErrorItem(
                code=ErrorCode.REWARD_RANGE,
                field="rewards[0].amount",
                message="Lv6–9 支线任务金币奖励上限为 200，当前 1000",
                constraint_ref="C_REWARD_GOLD_01",
                suggestion={"field": "rewards[0].amount", "value": 180},
            )
        ],
        duration_ms=11,
    )

    payload = report.to_dict()
    assert payload["checked"] == {"schema": 12, "business": 34, "conflict": 5}
    assert payload["errors"][0]["code"] == "E_REWARD_RANGE"
    assert payload["errors"][0]["suggestion"] == {"field": "rewards[0].amount", "value": 180}
    assert json.loads(json.dumps(payload, ensure_ascii=False))["ok"] is False


def test_warning_code_is_allowed_in_warnings() -> None:
    report = ValidationReport(
        ok=True,
        warnings=[
            ValidationErrorItem(
                code=ErrorCode.DIALOGUE_LONG,
                severity="warning",
                message="对白超过建议长度",
            )
        ],
    )
    assert report.ok is True
    assert report.warnings[0].severity.value == "warning"


def test_ok_true_cannot_carry_errors() -> None:
    with pytest.raises(ValidationError, match="ok=True"):
        ValidationReport(
            ok=True,
            errors=[ValidationErrorItem(code=ErrorCode.IO, message="disk full")],
        )


@pytest.mark.parametrize("code", ["E_NOT_A_CODE", "W_UNKNOWN", ""])
def test_invalid_error_code_is_rejected(code: str) -> None:
    with pytest.raises(ValidationError):
        ValidationErrorItem(code=code, message="boom")


def test_exported_report_schema_contains_all_codes_and_aliases() -> None:
    schema = export_validation_report_schema()
    assert schema["$id"] == "urn:gameconfig-copilot:schema:validation-report:1"
    assert set(schema["$defs"]["ErrorCode"]["enum"]) == DOC_ERROR_CODES

    checked = schema["$defs"]["CheckedCounts"]["properties"]
    assert set(checked) == {"schema", "business", "conflict"}
    assert schema["additionalProperties"] is False
    json.dumps(schema, ensure_ascii=False)
