"""Conflict detection: uniqueness, reference existence, dependency order (S4-04).

All three rules need committed repository state and therefore read it from
:class:`RepoSnapshot` instead of the filesystem. A cycle is reported as
``E_DEP_CYCLE`` (not auto-fixable per docs/01 §7.4); a duplicated ``task_id``
carries the next free ID as its suggestion.

Failure semantics
    An unknown ``target`` / ``order_by`` raises, and the engine turns that into
    ``E_VALIDATOR_CRASH``: a mistyped corpus entry must not weaken the gate.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import ClassVar

from gameconfig_copilot.schemas import (
    Constraint,
    ConstraintType,
    ErrorCode,
    Suggestion,
    ValidationErrorItem,
)

from .engine import (
    RuleContext,
    RuleLayer,
    TaskRecord,
    constraint_error,
    field_tokens,
    register,
)

_TASK_ID_PARTS = re.compile(r"^(T_(?:MAIN|SIDE|DAILY)_)(\d{4})$")
_MAX_GRAPH_NODES = 4096


@register
class UniqueRule:
    """``unique``: ``task_id`` must be new, a dialogue may belong to one task."""

    rule_type: ClassVar[ConstraintType] = ConstraintType.UNIQUE
    layer: ClassVar[RuleLayer] = RuleLayer.CONFLICT

    def evaluate(self, ctx: RuleContext, constraint: Constraint) -> list[ValidationErrorItem]:
        target = constraint.target or ""
        if target == "tasks":
            return self._task_id(ctx, constraint)
        if target == "dialogue_assignments":
            return self._dialogue(ctx, constraint)
        raise ValueError(f"{constraint.id}: 未知的 unique target {target!r}")

    def _task_id(self, ctx: RuleContext, constraint: Constraint) -> list[ValidationErrorItem]:
        task_id = ctx.task_id
        records = ctx.snapshot.tasks
        if task_id is None or records is None:
            return []
        existing = records.get(task_id)
        if existing is None:
            return []
        return [
            constraint_error(
                constraint,
                code=ErrorCode.DUP_ID,
                field="task_id",
                values={
                    "task_id": task_id,
                    "existing_path": existing.path or "路径未提供",
                    "field": "task_id",
                },
                suggestion=_next_task_id(task_id, records),
            )
        ]

    def _dialogue(self, ctx: RuleContext, constraint: Constraint) -> list[ValidationErrorItem]:
        dialogue_ref = ctx.get("giver.dialogue_ref")
        assignments = ctx.snapshot.dialogue_assignments
        if not isinstance(dialogue_ref, str) or not dialogue_ref or assignments is None:
            return []
        owner = assignments.get(dialogue_ref)
        if owner is None or owner == ctx.task_id:
            return []
        return [
            constraint_error(
                constraint,
                code=ErrorCode.DIALOGUE_ASSIGN_CONFLICT,
                field="giver.dialogue_ref",
                values={
                    "dialogue_ref": dialogue_ref,
                    "existing_task": owner,
                    "field": "giver.dialogue_ref",
                },
            )
        ]


@register
class ReferenceExistsRule:
    """``reference_exists``: every referenced entity must be in the snapshot.

    Supported ``target`` values are ``tasks`` / ``npcs`` / ``items`` /
    ``dialogs`` / ``maps``; the scope decides which field is read.
    """

    rule_type: ClassVar[ConstraintType] = ConstraintType.REFERENCE_EXISTS
    layer: ClassVar[RuleLayer] = RuleLayer.CONFLICT

    def evaluate(self, ctx: RuleContext, constraint: Constraint) -> list[ValidationErrorItem]:
        target = constraint.target or ""
        known = ctx.snapshot.collection(target)
        if known is None:
            return []
        items: list[ValidationErrorItem] = []
        for ref in ctx.scope(constraint.scope):
            value = ref.value
            if not isinstance(value, str) or not value or value in known:
                continue
            tokens = field_tokens(ref)
            tokens.update({"ref": value, "area": value, "target": target})
            items.append(
                constraint_error(
                    constraint,
                    code=ErrorCode.REF_MISSING,
                    field=ref.path,
                    values=tokens,
                )
            )
        return items


@register
class DependencyOrderRule:
    """``dependency_order``: no cycles / bounded depth, and no level inversion.

    The corpus uses one rule type for two checks, discriminated by params:
    ``order_by`` selects the level-order check (``C_DEP_LEVEL_ORDER_01``), and its
    absence selects the cycle/depth check (``C_DEP_NO_CYCLE_01``).
    """

    rule_type: ClassVar[ConstraintType] = ConstraintType.DEPENDENCY_ORDER
    layer: ClassVar[RuleLayer] = RuleLayer.CONFLICT

    def evaluate(self, ctx: RuleContext, constraint: Constraint) -> list[ValidationErrorItem]:
        if "order_by" in constraint.params:
            return self._level_order(ctx, constraint)
        return self._cycles(ctx, constraint)

    def _level_order(
        self,
        ctx: RuleContext,
        constraint: Constraint,
    ) -> list[ValidationErrorItem]:
        order_by = constraint.params.get("order_by")
        if order_by != "level_range.min":
            raise ValueError(f"{constraint.id}: 暂不支持 order_by={order_by!r}")
        direction = constraint.params.get("direction", "nondecreasing")
        if direction != "nondecreasing":
            raise ValueError(f"{constraint.id}: 暂不支持 direction={direction!r}")
        level = ctx.int_at("level_range.min")
        records = ctx.snapshot.tasks
        if level is None or records is None:
            return []
        items: list[ValidationErrorItem] = []
        for index, ref in ctx.prereq_refs():
            record = records.get(ref)
            if record is None or record.level_min is None:
                continue
            if record.level_min <= level:
                continue
            items.append(
                constraint_error(
                    constraint,
                    code=ErrorCode.LEVEL_RANGE,
                    field=f"prerequisites[{index}].ref",
                    values={
                        "ref": ref,
                        "ref_level": record.level_min,
                        "level": level,
                        "field": f"prerequisites[{index}].ref",
                    },
                    suggestion=Suggestion(field="level_range.min", value=record.level_min),
                )
            )
        return items

    def _cycles(self, ctx: RuleContext, constraint: Constraint) -> list[ValidationErrorItem]:
        if constraint.params.get("allow_cycles") is True:
            return []
        max_depth = constraint.params.get("max_depth")
        if not isinstance(max_depth, int) or isinstance(max_depth, bool) or max_depth < 1:
            raise ValueError(f"{constraint.id}: dependency_order 需要正整数 params.max_depth")
        task_id = ctx.task_id
        if task_id is None:
            return []
        edges = _dependency_edges(ctx)
        if len(edges) > _MAX_GRAPH_NODES:
            raise ValueError(f"{constraint.id}: 依赖图节点过多（{len(edges)}），拒绝计算")
        cycle = _find_cycle(task_id, edges)
        if cycle is not None:
            return [
                constraint_error(
                    constraint,
                    code=ErrorCode.DEP_CYCLE,
                    field="prerequisites",
                    values={
                        "max_depth": max_depth,
                        "cycle": " -> ".join(cycle),
                        "field": "prerequisites",
                    },
                )
            ]
        depth = _dependency_depth(task_id, edges)
        if depth <= max_depth:
            return []
        return [
            constraint_error(
                constraint,
                code=ErrorCode.DEP_CYCLE,
                field="prerequisites",
                values={
                    "max_depth": max_depth,
                    "depth": depth,
                    "field": "prerequisites",
                },
            )
        ]


def _dependency_edges(ctx: RuleContext) -> dict[str, tuple[str, ...]]:
    """Dependency graph: draft prerequisites win over a stale repo record."""

    edges: dict[str, tuple[str, ...]] = {}
    for record in (ctx.snapshot.tasks or {}).values():
        edges[record.task_id] = record.prerequisites
    task_id = ctx.task_id
    if task_id is not None:
        refs = tuple(dict.fromkeys(ref for _, ref in ctx.prereq_refs()))
        if refs or task_id not in edges:
            edges[task_id] = refs
    return edges


def _find_cycle(start: str, edges: dict[str, tuple[str, ...]]) -> list[str] | None:
    """Iterative DFS; returns the cycle closed at ``start`` (``None`` if acyclic)."""

    stack: list[tuple[str, int]] = [(start, 0)]
    path: list[str] = []
    on_path: set[str] = set()
    expanded: set[str] = set()
    path.append(start)
    on_path.add(start)
    expanded.add(start)
    while stack:
        node, child_index = stack[-1]
        children = edges.get(node, ())
        if child_index >= len(children):
            stack.pop()
            on_path.discard(node)
            path.pop()
            continue
        stack[-1] = (node, child_index + 1)
        child = children[child_index]
        if child in on_path:
            return [*path[path.index(child) :], child]
        if child in expanded or child not in edges:
            continue
        expanded.add(child)
        path.append(child)
        on_path.add(child)
        stack.append((child, 0))
    return None


def _dependency_depth(start: str, edges: dict[str, tuple[str, ...]]) -> int:
    """Longest prerequisite chain from ``start`` (acyclic by the time we call it)."""

    depth: dict[str, int] = {start: 0}
    frontier = [start]
    while frontier:
        node = frontier.pop()
        for child in edges.get(node, ()):
            candidate = depth[node] + 1
            if depth.get(child, -1) < candidate:
                depth[child] = candidate
                frontier.append(child)
    return max(depth.values())


def _next_task_id(task_id: str, records: Mapping[str, TaskRecord]) -> Suggestion | None:
    """Suggest the next free ID in the same namespace (docs/01 §7.4)."""

    match = _TASK_ID_PARTS.fullmatch(task_id)
    if match is None:
        return None
    prefix = match.group(1)
    taken: set[int] = set()
    for existing in records:
        other = _TASK_ID_PARTS.fullmatch(existing)
        if other is not None and other.group(1) == prefix:
            taken.add(int(other.group(2)))
    candidate = max(taken) + 1 if taken else 1
    while candidate in taken:
        candidate += 1
    if candidate > 9999:
        return None
    return Suggestion(field="task_id", value=f"{prefix}{candidate:04d}")


__all__ = ["DependencyOrderRule", "ReferenceExistsRule", "UniqueRule"]
