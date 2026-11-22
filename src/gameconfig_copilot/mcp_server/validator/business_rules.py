"""Business rules: reward bands, ranges, sums, dialogue length, conflicts (S4-03).

Every violation here points back to the YAML entry that produced it
(``constraint_ref``) and, whenever a deterministic replacement exists, carries a
``suggestion`` the repair node can apply without guessing. Boundary values (equal
to a limit) pass — see docs/03 S4 acceptance.

Failure semantics
    Missing/incoherent rule parameters raise, and the engine converts that into
    ``E_VALIDATOR_CRASH``; a value of the wrong type is skipped here because
    :mod:`schema_rules` already reports it as ``E_SCHEMA_002``.
"""

from __future__ import annotations

from typing import ClassVar

from gameconfig_copilot.schemas import (
    Constraint,
    ConstraintType,
    ErrorCode,
    Suggestion,
    ValidationErrorItem,
)

from .engine import (
    FieldRef,
    RuleContext,
    RuleLayer,
    constraint_error,
    field_tokens,
    register,
)

_REWARD_SCOPES = frozenset({"rewards", "activity_rewards"})


def _is_number(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _int_param(constraint: Constraint, name: str) -> int:
    value = constraint.params.get(name)
    if not _is_number(value):
        raise ValueError(f"{constraint.id}: 需要整数参数 {name}，实际为 {value!r}")
    return int(value)


def numeric_error_code(scope: str) -> ErrorCode:
    """Pick the frozen code for a numeric violation from the field's domain.

    Rewards use ``E_REWARD_RANGE`` (docs/01 §7.4), level bands use
    ``E_LEVEL_RANGE``; anything else is a plain invalid value, ``E_SCHEMA_002``.
    """

    head = scope.split(".", 1)[0]
    if head in _REWARD_SCOPES:
        return ErrorCode.REWARD_RANGE
    if "level" in scope:
        return ErrorCode.LEVEL_RANGE
    return ErrorCode.SCHEMA_INVALID_VALUE


def _band_for_level(constraint: Constraint, level: int):
    """The first band whose ``max_level`` covers ``level``; last band above the table."""

    bands = sorted(constraint.bands, key=lambda band: band.max_level)
    return next(
        (band for band in bands if level <= band.max_level),
        bands[-1],
    )


@register
class LevelBandMaxRule:
    """``level_band_max``: reward amount must respect the band for the draft level."""

    rule_type: ClassVar[ConstraintType] = ConstraintType.LEVEL_BAND_MAX
    layer: ClassVar[RuleLayer] = RuleLayer.BUSINESS

    def evaluate(self, ctx: RuleContext, constraint: Constraint) -> list[ValidationErrorItem]:
        level = ctx.int_at("level_range.min")
        if level is None:
            return []
        limit = _band_for_level(constraint, level).max_amount
        code = numeric_error_code(constraint.scope)
        items: list[ValidationErrorItem] = []
        for ref in ctx.scope(constraint.scope):
            if not _is_number(ref.value) or ref.value <= limit:
                continue
            tokens = field_tokens(ref)
            tokens.update({"level": level, "limit": limit})
            items.append(
                constraint_error(
                    constraint,
                    code=code,
                    field=ref.path,
                    values=tokens,
                    suggestion=Suggestion(field=ref.path, value=limit),
                )
            )
        return items


@register
class LevelBandRangeRule:
    """``level_band_range``: reward amount must stay inside the band's [min, max]."""

    rule_type: ClassVar[ConstraintType] = ConstraintType.LEVEL_BAND_RANGE
    layer: ClassVar[RuleLayer] = RuleLayer.BUSINESS

    def evaluate(self, ctx: RuleContext, constraint: Constraint) -> list[ValidationErrorItem]:
        level = ctx.int_at("level_range.min")
        if level is None:
            return []
        band = _band_for_level(constraint, level)
        if band.min_amount is None:  # pragma: no cover - schema already rejects this
            raise ValueError(f"{constraint.id}: level_band_range 的 band 缺少 min_amount")
        low, high = band.min_amount, band.max_amount
        code = numeric_error_code(constraint.scope)
        items: list[ValidationErrorItem] = []
        for ref in ctx.scope(constraint.scope):
            if not _is_number(ref.value) or low <= ref.value <= high:
                continue
            limit = low if ref.value < low else high
            tokens = field_tokens(ref)
            tokens.update({"level": level, "min": low, "max": high, "limit": limit})
            items.append(
                constraint_error(
                    constraint,
                    code=code,
                    field=ref.path,
                    values=tokens,
                    suggestion=Suggestion(field=ref.path, value=limit),
                )
            )
        return items


@register
class RangeRule:
    """``range``: inclusive numeric interval, both bounds configurable."""

    rule_type: ClassVar[ConstraintType] = ConstraintType.RANGE
    layer: ClassVar[RuleLayer] = RuleLayer.BUSINESS

    def evaluate(self, ctx: RuleContext, constraint: Constraint) -> list[ValidationErrorItem]:
        low = _int_param(constraint, "min")
        high = _int_param(constraint, "max")
        if low > high:
            raise ValueError(f"{constraint.id}: params.min ({low}) 不能大于 params.max ({high})")
        code = numeric_error_code(constraint.scope)
        items: list[ValidationErrorItem] = []
        for ref in ctx.scope(constraint.scope):
            if not _is_number(ref.value):
                continue
            if low <= ref.value <= high:
                continue
            limit = low if ref.value < low else high
            tokens = field_tokens(ref)
            tokens.update({"min": low, "max": high})
            items.append(
                constraint_error(
                    constraint,
                    code=code,
                    field=ref.path,
                    values=tokens,
                    suggestion=Suggestion(field=ref.path, value=limit),
                )
            )
        return items


@register
class SumLimitRule:
    """``sum_limit``: total across the scope must not exceed ``params.limit``."""

    rule_type: ClassVar[ConstraintType] = ConstraintType.SUM_LIMIT
    layer: ClassVar[RuleLayer] = RuleLayer.BUSINESS

    def evaluate(self, ctx: RuleContext, constraint: Constraint) -> list[ValidationErrorItem]:
        limit = _int_param(constraint, "limit")
        refs = [ref for ref in ctx.scope(constraint.scope) if _is_number(ref.value)]
        if not refs:
            return []
        total = sum(int(ref.value) for ref in refs)
        if total <= limit:
            return []
        excess = total - limit
        largest = max(refs, key=lambda ref: int(ref.value))
        suggestion = Suggestion(
            field=largest.path,
            value=max(0, int(largest.value) - excess),
        )
        return [
            constraint_error(
                constraint,
                code=numeric_error_code(constraint.scope),
                field=constraint.scope,
                values={"actual": total, "total": total, "limit": limit},
                suggestion=suggestion,
            )
        ]


@register
class LengthRule:
    """``length``: text length inside ``params.min``–``params.max`` characters."""

    rule_type: ClassVar[ConstraintType] = ConstraintType.LENGTH
    layer: ClassVar[RuleLayer] = RuleLayer.BUSINESS

    def evaluate(self, ctx: RuleContext, constraint: Constraint) -> list[ValidationErrorItem]:
        low = _int_param(constraint, "min")
        high = _int_param(constraint, "max")
        if low > high:
            raise ValueError(f"{constraint.id}: params.min ({low}) 不能大于 params.max ({high})")
        items: list[ValidationErrorItem] = []
        for ref in ctx.scope(constraint.scope):
            text = ref.value
            if not isinstance(text, str) or not text:
                continue
            length = len(text)
            if low <= length <= high:
                continue
            suggestion = (
                Suggestion(field=ref.path, value=text[:high]) if length > high else None
            )
            items.append(
                constraint_error(
                    constraint,
                    code=ErrorCode.DIALOGUE_LONG,
                    field=ref.path,
                    values={"actual": length, "min": low, "max": high},
                    suggestion=suggestion,
                )
            )
        return items


@register
class ConflictRule:
    """``conflict``: two reward channels must not be written at the same time."""

    rule_type: ClassVar[ConstraintType] = ConstraintType.CONFLICT
    layer: ClassVar[RuleLayer] = RuleLayer.BUSINESS

    def evaluate(self, ctx: RuleContext, constraint: Constraint) -> list[ValidationErrorItem]:
        other = constraint.params.get("conflicts_with")
        if not isinstance(other, str) or not other:
            raise ValueError(f"{constraint.id}: conflict 规则必须提供 params.conflicts_with")
        if not _has_payload(ctx.scope(constraint.scope)):
            return []
        if not _has_payload(ctx.scope(other)):
            return []
        return [
            constraint_error(
                constraint,
                code=ErrorCode.SCHEMA_INVALID_VALUE,
                field=constraint.scope,
                values={"field": constraint.scope, "conflicts_with": other},
            )
        ]


def _has_payload(refs: list[FieldRef]) -> bool:
    return any(bool(ref.value) for ref in refs)
