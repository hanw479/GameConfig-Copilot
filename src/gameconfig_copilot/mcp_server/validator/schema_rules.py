"""Schema-layer checks: required fields, types, enums, patterns, seq (S4-02).

Two kinds live here, and both feed ``checked.schema``:

* structural rules (always on, no YAML entry) — the :class:`TaskDraft` contract
  from docs/01 §7.1: required fields, field types, closed enums, ``task_id`` /
  ``npc_id`` patterns, contiguous ``objectives[].seq``;
* YAML-driven ``enum`` / ``regex`` rules, so ``C_AREA_ENUM_01`` and
  ``C_DIALOGUE_FORMAT_01`` are enforced from the frozen corpus instead of being
  hard-coded twice.

Failure semantics
    Values that cannot be read are reported as ``E_SCHEMA_001`` (missing) or
    ``E_SCHEMA_002`` (invalid value); a rule whose YAML parameters are unusable
    raises, and the engine turns that into ``E_VALIDATOR_CRASH``.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

from gameconfig_copilot.schemas import (
    Constraint,
    ConstraintType,
    ErrorCode,
    ObjectiveType,
    PrerequisiteType,
    RewardType,
    Suggestion,
    TaskType,
    ValidationErrorItem,
)
from gameconfig_copilot.schemas.task import TASK_ID_PATTERN

from .engine import (
    FieldRef,
    RuleContext,
    RuleLayer,
    constraint_error,
    field_tokens,
    preview,
    register,
    register_structural,
    structural_error,
)

TASK_ID_RE = re.compile(TASK_ID_PATTERN)
NPC_ID_RE = re.compile(r"^NPC_[A-Za-z0-9_]+$")
TITLE_MAX_LENGTH = 80

_REQUIRED_FIELDS: tuple[str, ...] = (
    "schema_version",
    "task_id",
    "title",
    "task_type",
    "level_range.min",
    "level_range.max",
    "giver.npc_id",
    "prerequisites",
    "objectives",
    "rewards",
    "branches",
    "provenance.request",
    "provenance.model",
    "provenance.created_at",
)

_ENUM_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("task_type", tuple(item.value for item in TaskType)),
    ("objectives[].type", tuple(item.value for item in ObjectiveType)),
    ("branches[].objectives[].type", tuple(item.value for item in ObjectiveType)),
    ("rewards[].type", tuple(item.value for item in RewardType)),
    ("prerequisites[].type", tuple(item.value for item in PrerequisiteType)),
)

_MISSING = object()

_OBJECTIVE_FIELDS: tuple[str, ...] = ("seq", "type", "target", "area")
_REWARD_FIELDS: tuple[str, ...] = ("type", "amount")
_PREREQUISITE_FIELDS: tuple[str, ...] = ("type", "ref")
_BRANCH_FIELDS: tuple[str, ...] = ("branch_id", "condition")


def _blank(value: Any) -> bool:
    return isinstance(value, str) and not value.strip()


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@register_structural
class RequiredFieldsRule:
    """docs/01 §7.1 required fields, including the nested list items."""

    name: ClassVar[str] = "required_fields"

    def check(self, ctx: RuleContext) -> tuple[list[ValidationErrorItem], int]:
        items: list[ValidationErrorItem] = []
        checked = 0
        for path in _REQUIRED_FIELDS:
            checked += 1
            value = ctx.get(path, _MISSING)
            if value is _MISSING or _blank(value):
                items.append(
                    structural_error(
                        ErrorCode.SCHEMA_MISSING_FIELD,
                        field=path,
                        message=f"缺少必填字段 {path}",
                    )
                )
        checked += 1
        objectives = ctx.items_at("objectives")
        if not objectives:
            items.append(
                structural_error(
                    ErrorCode.SCHEMA_MISSING_FIELD,
                    field="objectives",
                    message="objectives 至少需要 1 条目标",
                )
            )
        for index, objective in objectives:
            for key in _OBJECTIVE_FIELDS:
                checked += 1
                if _blank(objective.get(key)) or key not in objective:
                    items.append(
                        structural_error(
                            ErrorCode.SCHEMA_MISSING_FIELD,
                            field=f"objectives[{index}].{key}",
                            message=f"缺少必填字段 objectives[{index}].{key}",
                        )
                    )
        for section, fields in (
            ("rewards", _REWARD_FIELDS),
            ("prerequisites", _PREREQUISITE_FIELDS),
            ("branches", _BRANCH_FIELDS),
        ):
            for index, entry in ctx.items_at(section):
                for key in fields:
                    checked += 1
                    if key not in entry or _blank(entry.get(key)):
                        path = f"{section}[{index}].{key}"
                        items.append(
                            structural_error(
                                ErrorCode.SCHEMA_MISSING_FIELD,
                                field=path,
                                message=f"缺少必填字段 {path}",
                            )
                        )
        return items, checked


@register_structural
class FieldTypesRule:
    """Type and bound checks that mirror the Pydantic contract (docs/01 §7.1)."""

    name: ClassVar[str] = "field_types"

    def check(self, ctx: RuleContext) -> tuple[list[ValidationErrorItem], int]:
        items: list[ValidationErrorItem] = []
        checked = 0

        def expect(condition: bool, field: str, message: str) -> None:
            nonlocal checked
            checked += 1
            if not condition:
                items.append(
                    structural_error(ErrorCode.SCHEMA_INVALID_VALUE, field=field, message=message)
                )

        if ctx.has("schema_version"):
            expect(
                ctx.get("schema_version") == 1,
                "schema_version",
                "schema_version 必须为 1",
            )
        if ctx.has("title"):
            title = ctx.get("title")
            expect(
                isinstance(title, str) and len(title) <= TITLE_MAX_LENGTH,
                "title",
                f"title 必须是长度不超过 {TITLE_MAX_LENGTH} 的字符串",
            )
        for field in ("level_range.min", "level_range.max"):
            if ctx.has(field):
                value = ctx.get(field)
                expect(
                    _is_int(value) and value >= 1,
                    field,
                    f"{field} 必须是 >= 1 的整数",
                )
        for index, objective in ctx.items_at("objectives"):
            seq = objective.get("seq")
            expect(
                _is_int(seq) and seq >= 1,
                f"objectives[{index}].seq",
                f"objectives[{index}].seq 必须是 >= 1 的整数",
            )
            if "count" in objective:
                count = objective.get("count")
                expect(
                    _is_int(count) and count >= 1,
                    f"objectives[{index}].count",
                    f"objectives[{index}].count 必须是 >= 1 的整数",
                )
        for index, reward in ctx.items_at("rewards"):
            if "amount" in reward:
                amount = reward.get("amount")
                expect(
                    _is_int(amount) and amount >= 0,
                    f"rewards[{index}].amount",
                    f"rewards[{index}].amount 必须是 >= 0 的整数",
                )
        return items, checked


@register_structural
class EnumValuesRule:
    """Closed enums from the TaskDraft contract (E_SCHEMA_002)."""

    name: ClassVar[str] = "enum_values"

    def check(self, ctx: RuleContext) -> tuple[list[ValidationErrorItem], int]:
        items: list[ValidationErrorItem] = []
        checked = 0
        for scope, allowed in _ENUM_FIELDS:
            for ref in ctx.scope(scope):
                checked += 1
                if ref.value is None or ref.value in allowed:
                    continue
                items.append(
                    structural_error(
                        ErrorCode.SCHEMA_INVALID_VALUE,
                        field=ref.path,
                        message=(
                            f"{ref.path} 只能是 {', '.join(allowed)} 之一，"
                            f"当前 {preview(ref.value)}"
                        ),
                    )
                )
        return items, checked


@register_structural
class IdentifierFormatRule:
    """ID shapes: ``task_id`` (docs/01 §7.1) and the ``npc_id`` contract.

    ``giver.dialogue_ref`` is intentionally left to ``C_DIALOGUE_FORMAT_01`` so a
    bad dialogue number yields one error owned by the corpus, not two.
    """

    name: ClassVar[str] = "identifier_format"

    def check(self, ctx: RuleContext) -> tuple[list[ValidationErrorItem], int]:
        items: list[ValidationErrorItem] = []
        checked = 0
        task_id = ctx.get("task_id")
        if isinstance(task_id, str) and task_id:
            checked += 1
            if TASK_ID_RE.fullmatch(task_id) is None:
                items.append(
                    structural_error(
                        ErrorCode.SCHEMA_INVALID_VALUE,
                        field="task_id",
                        message="task_id 必须匹配 ^T_(MAIN|SIDE|DAILY)_\\d{4}$",
                    )
                )
        npc_id = ctx.get("giver.npc_id")
        if isinstance(npc_id, str) and npc_id:
            checked += 1
            if NPC_ID_RE.fullmatch(npc_id) is None:
                items.append(
                    structural_error(
                        ErrorCode.SCHEMA_INVALID_VALUE,
                        field="giver.npc_id",
                        message="giver.npc_id 必须匹配 ^NPC_[A-Za-z0-9_]+$",
                    )
                )
        return items, checked


@register_structural
class ObjectiveSequenceRule:
    """``objectives[].seq`` must be 1..N in order (docs/01 §7.1)."""

    name: ClassVar[str] = "objective_sequence"

    def check(self, ctx: RuleContext) -> tuple[list[ValidationErrorItem], int]:
        entries = ctx.items_at("objectives")
        if not entries:
            return [], 0
        seqs = [entry.get("seq") for _, entry in entries]
        if not all(_is_int(seq) for seq in seqs):
            # Type errors belong to FieldTypesRule; a non-int seq cannot be ordered.
            return [], 1
        expected = list(range(1, len(seqs) + 1))
        if seqs == expected:
            return [], 1
        return (
            [
                structural_error(
                    ErrorCode.SCHEMA_INVALID_VALUE,
                    field="objectives[].seq",
                    message=f"objectives[].seq 必须从 1 连续递增，当前为 {seqs}",
                )
            ],
            1,
        )


@register
class EnumConstraintRule:
    """YAML ``enum``: value must be one of ``params.values``."""

    rule_type: ClassVar[ConstraintType] = ConstraintType.ENUM
    layer: ClassVar[RuleLayer] = RuleLayer.SCHEMA

    def evaluate(self, ctx: RuleContext, constraint: Constraint) -> list[ValidationErrorItem]:
        allowed = list(constraint.params.get("values") or ())
        if not allowed:
            raise ValueError(f"{constraint.id}: enum 规则必须提供非空 params.values")
        items: list[ValidationErrorItem] = []
        for ref in ctx.scope(constraint.scope):
            if ref.value is None or ref.value in allowed:
                continue
            tokens = field_tokens(ref)
            tokens["allowed"] = ", ".join(str(value) for value in allowed)
            items.append(
                constraint_error(
                    constraint,
                    code=ErrorCode.SCHEMA_INVALID_VALUE,
                    field=ref.path,
                    values=tokens,
                )
            )
        return items


@register
class RegexConstraintRule:
    """YAML ``regex``: the field must fully match ``params.pattern``."""

    rule_type: ClassVar[ConstraintType] = ConstraintType.REGEX
    layer: ClassVar[RuleLayer] = RuleLayer.SCHEMA

    def evaluate(self, ctx: RuleContext, constraint: Constraint) -> list[ValidationErrorItem]:
        pattern_text = constraint.params.get("pattern")
        if not isinstance(pattern_text, str) or not pattern_text:
            raise ValueError(f"{constraint.id}: regex 规则必须提供非空 params.pattern")
        pattern = re.compile(pattern_text)
        items: list[ValidationErrorItem] = []
        for ref in ctx.scope(constraint.scope):
            if not isinstance(ref.value, str) or not ref.value:
                continue
            if pattern.fullmatch(ref.value) is not None:
                continue
            tokens = field_tokens(ref)
            tokens["pattern"] = pattern_text
            items.append(
                constraint_error(
                    constraint,
                    code=ErrorCode.SCHEMA_INVALID_VALUE,
                    field=ref.path,
                    values=tokens,
                    suggestion=_dialogue_ref_suggestion(ctx, ref),
                )
            )
        return items


def _dialogue_ref_suggestion(ctx: RuleContext, ref: FieldRef) -> Suggestion | None:
    """Derive ``DLG_<npc digits>_01`` when the NPC already encodes the number."""

    if ref.path != "giver.dialogue_ref":
        return None
    npc_id = ctx.get("giver.npc_id")
    if not isinstance(npc_id, str):
        return None
    match = re.search(r"\d{3}", npc_id)
    if match is None:
        return None
    candidate = f"DLG_{match.group(0)}_01"
    if candidate == ref.value:
        return None
    return Suggestion(field=ref.path, value=candidate)
