"""Constraint contract for the YAML rule DSL.

Mirrors ``docs/01-product-outline.md`` §7.3. One YAML mapping -> one
:class:`Constraint`; the set of rule types is closed (MVP) so the validator can
dispatch on :class:`ConstraintType` instead of free-form strings.

Rule-type parameters
--------------------
``level_band_max``   ``bands`` (required, ``{max_level, max_amount}``)
``level_band_range`` ``bands`` (required, ``{max_level, min_amount, max_amount}``)
``range``            ``params: {min, max}``
``enum``            ``params: {values: [...]}``
``unique``          ``scope`` + ``target`` (collection name)
``reference_exists`` ``target`` (collection name)
``dependency_order`` ``params`` (ordering hints)
``sum_limit``       ``params: {limit}``
``conflict``        ``params`` (mutually-exclusive scope)
``regex``           ``params: {pattern}``
``length``          ``params: {min, max}``

Failure semantics
    Unknown rule types, malformed IDs, missing bands for a ``level_band_*`` rule,
    a band range without ``min_amount`` (or with ``min_amount > max_amount``) and
    extra fields all raise :class:`pydantic.ValidationError`.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import Field, model_validator

from ._base import ContractModel, export_model_schema
from .report import Severity


class ConstraintType(str, Enum):
    LEVEL_BAND_MAX = "level_band_max"
    LEVEL_BAND_RANGE = "level_band_range"
    RANGE = "range"
    ENUM = "enum"
    UNIQUE = "unique"
    REFERENCE_EXISTS = "reference_exists"
    DEPENDENCY_ORDER = "dependency_order"
    SUM_LIMIT = "sum_limit"
    CONFLICT = "conflict"
    REGEX = "regex"
    LENGTH = "length"


class LevelBand(ContractModel):
    max_level: int = Field(ge=1)
    min_amount: int | None = Field(
        default=None,
        ge=0,
        description="Floor for type=level_band_range; must be absent for level_band_max.",
    )
    max_amount: int = Field(ge=0)


class Constraint(ContractModel):
    id: str = Field(pattern=r"^C_[A-Z0-9_]+$")
    scope: str = Field(min_length=1, description="Field path the rule applies to.")
    type: ConstraintType
    severity: Severity = Severity.ERROR
    message: str = Field(min_length=1, description="Template with {placeholders}.")
    rationale: str | None = Field(
        default=None,
        description="Why the rule exists; surfaced in validation explanations.",
    )
    target: str | None = Field(
        default=None,
        description="Collection name for unique / reference_exists rules.",
    )
    bands: list[LevelBand] = Field(default_factory=list)
    params: dict[str, Any] = Field(
        default_factory=dict,
        description="Type-specific parameters, see the module docstring.",
    )

    @model_validator(mode="after")
    def _level_band_rules_need_coherent_bands(self) -> Constraint:
        if self.type in {ConstraintType.LEVEL_BAND_MAX, ConstraintType.LEVEL_BAND_RANGE}:
            if not self.bands:
                raise ValueError(f"type={self.type.value} 必须提供非空 bands")
            levels = [band.max_level for band in self.bands]
            if len(levels) != len(set(levels)):
                raise ValueError("bands[].max_level 不能重复")
            if self.type is ConstraintType.LEVEL_BAND_RANGE:
                for band in self.bands:
                    if band.min_amount is None:
                        raise ValueError("type=level_band_range 的每个 band 必须提供 min_amount")
                    if band.min_amount > band.max_amount:
                        raise ValueError(
                            f"bands[].min_amount ({band.min_amount}) 不能大于 "
                            f"max_amount ({band.max_amount})"
                        )
            elif any(band.min_amount is not None for band in self.bands):
                raise ValueError("type=level_band_max 的 band 不允许设置 min_amount")
        elif self.bands:
            raise ValueError("只有 level_band_max / level_band_range 才允许设置 bands")
        return self


def export_constraint_schema() -> dict[str, Any]:
    """Export the Constraint JSON Schema."""

    return export_model_schema(
        Constraint,
        schema_id="urn:gameconfig-copilot:schema:constraint:1",
    )
