"""Deterministic rule engine for draft validation (S4-01).

Owner: Validation Worker V (``docs/06-multi-agent-collaboration.md`` §5.2).
ADR-005: validation never calls an LLM, so every verdict is reproducible from
``(draft, constraints, repo_snapshot)``.

Input
    ``validate(draft, constraints, repo_snapshot)`` — a draft mapping (a validated
    :class:`TaskDraft` is accepted too), the already-loaded :class:`Constraint`
    corpus and a :class:`RepoSnapshot`. ``validate`` performs no IO; YAML reads
    live in :func:`load_constraints` and snapshot building in
    :meth:`RepoSnapshot.from_task_payloads`.
Output
    :class:`ValidationReport`. ``duration_ms`` stays ``0`` on purpose: the
    transport layer stamps wall-clock time, so identical inputs always produce
    identical reports.
Failure semantics
    Any exception raised while running a rule becomes a single
    ``E_VALIDATOR_CRASH`` report — an exception is a failure verdict, never a
    pass (docs/02 §12). Unknown rule types and unreadable constraint files fail
    closed instead of silently skipping.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar, Protocol, runtime_checkable

import yaml

from gameconfig_copilot.schemas import (
    WARNING_CODES,
    CheckedCounts,
    Constraint,
    ConstraintType,
    ErrorCode,
    Severity,
    Suggestion,
    ValidationErrorItem,
    ValidationReport,
)

__all__ = [
    "DEFAULT_CONSTRAINTS_DIR",
    "RULE_REGISTRY",
    "STRUCTURAL_RULES",
    "ConstraintLoadError",
    "FieldRef",
    "RepoSnapshot",
    "Rule",
    "RuleContext",
    "RuleLayer",
    "StructuralRule",
    "TaskRecord",
    "UnregisteredRuleError",
    "constraint_error",
    "field_tokens",
    "load_builtin_rules",
    "load_constraints",
    "parse_constraints",
    "preview",
    "register",
    "register_structural",
    "render_message",
    "resolve_scope",
    "validate",
]

# ``src/gameconfig_copilot/mcp_server/validator/engine.py`` -> repository root.
DEFAULT_CONSTRAINTS_DIR = Path(__file__).resolve().parents[4] / "data" / "constraints"

REWARD_TYPES = frozenset({"gold", "exp", "item"})
_REWARD_CONTAINERS = frozenset({"rewards", "activity_rewards"})
_REFERENCE_TARGETS = frozenset({"tasks", "npcs", "items", "dialogs", "maps"})
_PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*)\}")
_MISSING = object()


class RuleLayer(str, Enum):
    """Which report counter a rule feeds (``checked.schema/business/conflict``)."""

    SCHEMA = "schema"
    BUSINESS = "business"
    CONFLICT = "conflict"


class ConstraintLoadError(RuntimeError):
    """Raised when the YAML constraint corpus is missing or malformed."""


class UnregisteredRuleError(RuntimeError):
    """Raised when a constraint asks for a rule type nobody registered."""


@runtime_checkable
class Rule(Protocol):
    """One YAML rule type; implementations are stateless and side-effect free."""

    rule_type: ClassVar[ConstraintType]
    layer: ClassVar[RuleLayer]

    def evaluate(self, ctx: RuleContext, constraint: Constraint) -> list[ValidationErrorItem]:
        """Return every violation of ``constraint`` for the draft in ``ctx``."""


@runtime_checkable
class StructuralRule(Protocol):
    """Always-on schema check that is not driven by a YAML constraint."""

    name: ClassVar[str]

    def check(self, ctx: RuleContext) -> tuple[list[ValidationErrorItem], int]:
        """Return ``(items, checks_run)``; ``checks_run`` feeds ``checked.schema``."""


@dataclass(frozen=True, slots=True)
class FieldRef:
    """A resolved field: dotted/indexed path plus its current value."""

    path: str
    value: Any


@dataclass(frozen=True, slots=True)
class TaskRecord:
    """The committed-task slice the validator needs (pure data, no IO)."""

    task_id: str
    level_min: int | None = None
    prerequisites: tuple[str, ...] = ()
    path: str | None = None

    @classmethod
    def from_mapping(cls, raw: str | Mapping[str, Any]) -> TaskRecord:
        if isinstance(raw, str):
            return cls(task_id=raw)
        if not isinstance(raw, Mapping):
            raise ValueError(f"tasks[] 条目必须是映射或字符串，实际为 {type(raw).__name__}")
        task_id = raw.get("task_id") or raw.get("id")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("tasks[] 条目缺少 task_id")
        level = raw.get("level_min", raw.get("level"))
        return cls(
            task_id=task_id,
            level_min=level if isinstance(level, int) and not isinstance(level, bool) else None,
            prerequisites=_prerequisite_refs(raw.get("prerequisites")),
            path=raw.get("path") if isinstance(raw.get("path"), str) else None,
        )


@dataclass(frozen=True, slots=True)
class RepoSnapshot:
    """Read-only repository projection; every collection may be ``None``.

    ``None`` means "the caller knows nothing about this collection", so rules
    that need it are skipped. An empty ``frozenset`` means "known to be empty",
    so every reference is missing. Building the snapshot is the caller's job
    (MCP tools / tests); it keeps :func:`validate` a pure function.
    """

    tasks: Mapping[str, TaskRecord] | None = None
    npcs: frozenset[str] | None = None
    items: frozenset[str] | None = None
    dialogs: frozenset[str] | None = None
    maps: frozenset[str] | None = None
    dialogue_assignments: Mapping[str, str] | None = None
    dialogue_texts: Mapping[str, str] | None = None

    @classmethod
    def empty(cls) -> RepoSnapshot:
        return cls()

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any] | None,
        *,
        base: RepoSnapshot | None = None,
    ) -> RepoSnapshot:
        """Build a snapshot from JSON-friendly data, optionally layered on ``base``."""

        if raw is None:
            return base if base is not None else cls()
        tasks = dict(base.tasks) if base is not None and base.tasks is not None else {}
        for entry in raw.get("tasks") or ():
            record = TaskRecord.from_mapping(entry)
            tasks[record.task_id] = record
        if "tasks" in raw:
            task_index: Mapping[str, TaskRecord] | None = tasks
        else:
            task_index = base.tasks if base is not None else None
        assignments = dict(base.dialogue_assignments or {}) if base is not None else {}
        assignments.update(_str_mapping(raw.get("dialogue_assignments")))
        if "dialogue_assignments" in raw:
            assignment_index: Mapping[str, str] | None = assignments
        else:
            assignment_index = base.dialogue_assignments if base is not None else None
        texts = dict(base.dialogue_texts or {}) if base is not None else {}
        texts.update(_str_mapping(raw.get("dialogue_texts")))
        if "dialogue_texts" in raw:
            text_index: Mapping[str, str] | None = texts
        else:
            text_index = base.dialogue_texts if base is not None else None
        return cls(
            tasks=task_index,
            npcs=_id_set(raw, "npcs", base.npcs if base is not None else None),
            items=_id_set(raw, "items", base.items if base is not None else None),
            dialogs=_id_set(raw, "dialogs", base.dialogs if base is not None else None),
            maps=_id_set(raw, "maps", base.maps if base is not None else None),
            dialogue_assignments=assignment_index,
            dialogue_texts=text_index,
        )

    @classmethod
    def from_task_payloads(
        cls,
        payloads: Iterable[Mapping[str, Any]],
        *,
        dialogue_texts: Mapping[str, str] | None = None,
    ) -> RepoSnapshot:
        """Project raw task JSON payloads into a snapshot (pure; no IO).

        Entity IDs are harvested from the payloads themselves: givers and
        ``talk_to`` / ``kill`` targets are NPCs, ``collect`` targets and reward
        ``item_id`` are items, ``reach`` targets and objective ``area`` values are
        maps, and ``dialogue_ref`` values are dialogues.
        """

        tasks: dict[str, TaskRecord] = {}
        npcs: set[str] = set()
        items: set[str] = set()
        maps: set[str] = set()
        dialogs: set[str] = set()
        assignments: dict[str, str] = {}
        for payload in payloads:
            if not isinstance(payload, Mapping):
                continue
            task_id = payload.get("task_id")
            if not isinstance(task_id, str) or not task_id:
                continue
            tasks[task_id] = TaskRecord(
                task_id=task_id,
                level_min=payload.get("level_range", {}).get("min")
                if isinstance(payload.get("level_range"), Mapping)
                else None,
                prerequisites=_prerequisite_refs(payload.get("prerequisites")),
                path=payload.get("path") if isinstance(payload.get("path"), str) else None,
            )
            giver = payload.get("giver")
            if isinstance(giver, Mapping):
                npc_id = giver.get("npc_id")
                if isinstance(npc_id, str) and npc_id:
                    npcs.add(npc_id)
                dialogue_ref = giver.get("dialogue_ref")
                if isinstance(dialogue_ref, str) and dialogue_ref:
                    dialogs.add(dialogue_ref)
                    assignments.setdefault(dialogue_ref, task_id)
            for objective in _mappings(payload.get("objectives")):
                area = objective.get("area")
                if isinstance(area, str) and area:
                    maps.add(area)
                target = objective.get("target")
                if isinstance(target, str) and target:
                    kind = objective.get("type")
                    if kind in {"talk_to", "kill"}:
                        npcs.add(target)
                    elif kind == "collect":
                        items.add(target)
                    elif kind == "reach":
                        maps.add(target)
                    elif kind == "interact":
                        npcs.add(target)
                        items.add(target)
            for reward in _mappings(payload.get("rewards")):
                item_id = reward.get("item_id")
                if isinstance(item_id, str) and item_id:
                    items.add(item_id)
        return cls(
            tasks=tasks,
            npcs=frozenset(npcs),
            items=frozenset(items),
            dialogs=frozenset(dialogs),
            maps=frozenset(maps),
            dialogue_assignments=assignments,
            dialogue_texts=dict(dialogue_texts) if dialogue_texts else None,
        )

    def collection(self, name: str) -> frozenset[str] | None:
        """Known IDs for ``name``; ``None`` means unknown, ``frozenset()`` means empty."""

        if name == "tasks":
            return None if self.tasks is None else frozenset(self.tasks)
        if name == "npcs":
            return self.npcs
        if name == "items":
            return self.items
        if name == "dialogs":
            return self.dialogs
        if name == "maps":
            return self.maps
        raise ValueError(f"未知引用集合 {name!r}；只支持 {sorted(_REFERENCE_TARGETS)}")


@dataclass(frozen=True, slots=True)
class RuleContext:
    """Everything a rule may look at; no IO, no LLM, no global state."""

    draft: Mapping[str, Any]
    snapshot: RepoSnapshot

    @property
    def task_id(self) -> str | None:
        value = self.draft.get("task_id")
        return value if isinstance(value, str) and value else None

    def has(self, path: str) -> bool:
        return self.get(path, _MISSING) is not _MISSING

    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self.draft
        for part in path.split("."):
            if not isinstance(node, Mapping) or part not in node:
                return default
            node = node[part]
        return node

    def int_at(self, path: str) -> int | None:
        value = self.get(path)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    def items_at(self, path: str) -> list[tuple[int, Mapping[str, Any]]]:
        value = self.get(path)
        if not isinstance(value, list):
            return []
        return [(index, item) for index, item in enumerate(value) if isinstance(item, Mapping)]

    def scope(self, scope: str) -> list[FieldRef]:
        return resolve_scope(self.draft, scope, self.snapshot)

    def prereq_refs(self) -> list[tuple[int, str]]:
        return [
            (index, ref)
            for index, entry in self.items_at("prerequisites")
            if isinstance(ref := entry.get("ref"), str) and ref
        ]


def _prerequisite_refs(raw: Any) -> tuple[str, ...]:
    refs: list[str] = []
    for entry in raw if isinstance(raw, list) else ():
        ref = entry.get("ref") if isinstance(entry, Mapping) else entry
        if isinstance(ref, str) and ref:
            refs.append(ref)
    return tuple(refs)


def _mappings(raw: Any) -> list[Mapping[str, Any]]:
    return [item for item in raw if isinstance(item, Mapping)] if isinstance(raw, list) else []


def _str_mapping(raw: Any) -> dict[str, str]:
    if not isinstance(raw, Mapping):
        return {}
    return {str(key): str(value) for key, value in raw.items()}


def _id_set(
    raw: Mapping[str, Any],
    key: str,
    fallback: frozenset[str] | None,
) -> frozenset[str] | None:
    if key not in raw:
        return fallback
    value = raw.get(key) or ()
    return frozenset(str(item) for item in value)


def resolve_scope(
    draft: Mapping[str, Any],
    scope: str,
    snapshot: RepoSnapshot | None = None,
) -> list[FieldRef]:
    """Resolve a YAML ``scope`` such as ``objectives[].area`` into field refs.

    Supported shapes: dotted keys (``level_range.min``), list wildcards
    (``objectives[].area``), reward-type filters (``rewards.gold``,
    ``activity_rewards.item``), whole sections (``rewards``) and the virtual
    ``dialogue.text`` scope (draft ``dialogue.text`` or the snapshot's text for
    ``giver.dialogue_ref``). Missing values yield an empty list; the required
    field rule is what reports them.
    """

    if scope == "dialogue.text":
        return _dialogue_texts(draft, snapshot)
    parts = [part for part in scope.split(".") if part]
    if not parts:
        return []
    return _walk(draft, parts, "")


def _dialogue_texts(draft: Mapping[str, Any], snapshot: RepoSnapshot | None) -> list[FieldRef]:
    dialogue = draft.get("dialogue")
    if isinstance(dialogue, Mapping):
        text = dialogue.get("text")
        if isinstance(text, str):
            return [FieldRef("dialogue.text", text)]
    giver = draft.get("giver")
    ref = giver.get("dialogue_ref") if isinstance(giver, Mapping) else None
    if not isinstance(ref, str) or snapshot is None or not snapshot.dialogue_texts:
        return []
    text = snapshot.dialogue_texts.get(ref)
    if not isinstance(text, str):
        return []
    return [FieldRef(f"dialogue[{ref}].text", text)]


def _walk(node: Any, parts: Sequence[str], prefix: str) -> list[FieldRef]:
    head = parts[0]
    rest = parts[1:]
    is_list = head.endswith("[]")
    key = head[:-2] if is_list else head
    if not isinstance(node, Mapping):
        return []
    if len(rest) == 1 and rest[0] in REWARD_TYPES and key in _REWARD_CONTAINERS:
        return _reward_amounts(node.get(key), rest[0], _join(prefix, key))
    value = node.get(key)
    if value is None:
        return []
    path = _join(prefix, key)
    if is_list:
        if not isinstance(value, list):
            return []
        refs: list[FieldRef] = []
        for index, item in enumerate(value):
            refs.extend(_walk(item, rest, f"{path}[{index}]"))
        return refs
    if not rest:
        return [FieldRef(path, value)]
    return _walk(value, rest, path)


def _reward_amounts(container: Any, reward_type: str, base: str) -> list[FieldRef]:
    refs: list[FieldRef] = []
    for index, item in enumerate(container if isinstance(container, list) else ()):
        if not isinstance(item, Mapping) or item.get("type") != reward_type:
            continue
        if "amount" in item:
            refs.append(FieldRef(f"{base}[{index}].amount", item.get("amount")))
    return refs


def _join(prefix: str, part: str) -> str:
    return f"{prefix}.{part}" if prefix else part


def preview(value: Any, *, limit: int = 48) -> str:
    """Single-line, length-capped rendering used inside error messages."""

    text = " ".join(str(value).split())
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def render_message(template: str, values: Mapping[str, Any] | None = None) -> str:
    """Fill ``{placeholder}`` slots without ever raising on odd templates.

    ``str.format`` would explode on an unbalanced brace or an unknown key, and a
    constraint typo must not turn a real violation into a crash. Unknown slots are
    left untouched so the raw template stays diagnosable.
    """

    if not isinstance(template, str):  # pragma: no cover - Constraint guarantees str
        return str(template)
    tokens = values or {}

    def _replace(match: re.Match[str]) -> str:
        value = tokens.get(match.group(1), _MISSING)
        return match.group(0) if value is _MISSING or value is None else str(value)

    return _PLACEHOLDER.sub(_replace, template)


def field_tokens(ref: FieldRef) -> dict[str, Any]:
    """Template values for one field, including its own leaf name (``{area}``)."""

    leaf = re.sub(r"\[\d+\]", "", ref.path.rsplit(".", 1)[-1])
    return {"actual": ref.value, "value": ref.value, "field": ref.path, leaf: ref.value}


def constraint_error(
    constraint: Constraint,
    *,
    code: ErrorCode,
    field: str,
    values: Mapping[str, Any] | None = None,
    suggestion: Suggestion | None = None,
) -> ValidationErrorItem:
    """Build a violation owned by ``constraint`` and rendered from its template."""

    tokens: dict[str, Any] = {"field": field}
    tokens.update(values or {})
    return ValidationErrorItem(
        code=code,
        field=field,
        message=render_message(constraint.message, tokens),
        constraint_ref=constraint.id,
        suggestion=suggestion,
    )


def structural_error(
    code: ErrorCode,
    *,
    field: str,
    message: str,
    suggestion: Suggestion | None = None,
) -> ValidationErrorItem:
    """Build a violation that comes from the always-on schema contract."""

    return ValidationErrorItem(code=code, field=field, message=message, suggestion=suggestion)


RULE_REGISTRY: dict[ConstraintType, Rule] = {}
STRUCTURAL_RULES: list[StructuralRule] = []


def register(rule_cls: type[Rule]) -> type[Rule]:
    """Class decorator: instantiate and register a YAML rule type."""

    RULE_REGISTRY[rule_cls.rule_type] = rule_cls()
    return rule_cls


def register_structural(rule_cls: type[StructuralRule]) -> type[StructuralRule]:
    """Class decorator: instantiate and register an always-on schema check."""

    STRUCTURAL_RULES.append(rule_cls())
    return rule_cls


_RULES_LOADED = False


def load_builtin_rules() -> None:
    """Import the rule modules once so their decorators have run.

    :func:`validate` calls this itself: importing only ``engine`` still yields a
    complete registry, and a half-imported package fails closed instead of
    validating against an empty registry.
    """

    global _RULES_LOADED
    if _RULES_LOADED:
        return
    from . import business_rules, conflicts, schema_rules  # noqa: F401  (registration side effects)

    _RULES_LOADED = True


def parse_constraints(document: Any, *, source: str = "<memory>") -> list[Constraint]:
    """Validate an already-parsed YAML document, fail-closed on any defect."""

    if document is None:
        return []
    if not isinstance(document, list):
        raise ConstraintLoadError(f"{source}: 约束文件顶层必须是 YAML 列表")
    constraints: list[Constraint] = []
    seen: dict[str, str] = {}
    for index, raw in enumerate(document):
        if not isinstance(raw, Mapping):
            raise ConstraintLoadError(
                f"{source}[{index}]: 约束必须是映射，实际为 {type(raw).__name__}"
            )
        try:
            constraint = Constraint.model_validate(raw)
        except ValueError as exc:
            raise ConstraintLoadError(f"{source}[{index}]: 约束非法：{exc}") from exc
        if constraint.id in seen:
            raise ConstraintLoadError(
                f"{source}: 约束 ID {constraint.id} 重复（另一处 {seen[constraint.id]}）"
            )
        seen[constraint.id] = f"{source}[{index}]"
        constraints.append(constraint)
    return constraints


def load_constraints(
    paths: Sequence[str | Path] | None = None,
    *,
    directory: str | Path | None = None,
) -> list[Constraint]:
    """Load the YAML constraint DSL from files (the only IO in this module).

    Missing files, YAML syntax errors, schema violations and duplicate IDs all
    raise :class:`ConstraintLoadError`; docs/02 §12 requires generating to stop
    rather than proceed without constraints.
    """

    if paths is not None:
        candidates: list[Path] = []
        for raw in paths:
            path = Path(raw)
            if path.is_dir():
                candidates.extend(sorted([*path.glob("*.yaml"), *path.glob("*.yml")]))
            else:
                candidates.append(path)
    else:
        base = Path(directory) if directory is not None else DEFAULT_CONSTRAINTS_DIR
        candidates = sorted([*base.glob("*.yaml"), *base.glob("*.yml")]) if base.is_dir() else []
        if not candidates:
            raise ConstraintLoadError(
                f"约束目录不存在或为空：{base}（fail-closed，禁止无约束校验）"
            )

    constraints: list[Constraint] = []
    origins: dict[str, str] = {}
    for path in candidates:
        if not path.is_file():
            raise ConstraintLoadError(f"约束文件不存在：{path}")
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise ConstraintLoadError(f"{path.name}: 无法加载 YAML：{exc}") from exc
        for constraint in parse_constraints(document, source=path.name):
            if constraint.id in origins:
                raise ConstraintLoadError(
                    f"约束 ID {constraint.id} 在 {origins[constraint.id]} 与 {path.name} 重复"
                )
            origins[constraint.id] = path.name
            constraints.append(constraint)
    return constraints


def validate(
    draft: Mapping[str, Any] | Any,
    constraints: Sequence[Constraint | Mapping[str, Any]],
    repo_snapshot: RepoSnapshot | Mapping[str, Any] | None,
) -> ValidationReport:
    """Validate ``draft`` against ``constraints`` and ``repo_snapshot``.

    Pure function: no IO, no LLM, no clock. ``repo_snapshot=None`` means "no
    repository knowledge", which skips every rule that needs committed state
    (uniqueness, reference existence, dependency order); callers that gate a
    write must pass a real snapshot.
    """

    try:
        load_builtin_rules()
        payload = _as_mapping(draft)
        snapshot = _as_snapshot(repo_snapshot)
        ctx = RuleContext(draft=payload, snapshot=snapshot)
        items, counts = _run_rules(ctx, constraints)
        return _build_report(items, counts)
    except Exception as exc:  # noqa: BLE001 - fail-closed by contract (ADR-005)
        return _crash_report(exc)


def _as_mapping(draft: Any) -> Mapping[str, Any]:
    if isinstance(draft, Mapping):
        return draft
    dump = getattr(draft, "model_dump", None)
    if callable(dump):
        dumped = dump(mode="python")
        if isinstance(dumped, Mapping):
            return dumped
    raise TypeError(f"draft 必须是映射或 TaskDraft，实际为 {type(draft).__name__}")


def _as_snapshot(repo_snapshot: RepoSnapshot | Mapping[str, Any] | None) -> RepoSnapshot:
    if repo_snapshot is None:
        return RepoSnapshot.empty()
    if isinstance(repo_snapshot, RepoSnapshot):
        return repo_snapshot
    if isinstance(repo_snapshot, Mapping):
        return RepoSnapshot.from_mapping(repo_snapshot)
    raise TypeError(
        f"repo_snapshot 必须是 RepoSnapshot 或映射，实际为 {type(repo_snapshot).__name__}"
    )


def _run_rules(
    ctx: RuleContext,
    constraints: Sequence[Constraint | Mapping[str, Any]],
) -> tuple[list[ValidationErrorItem], CheckedCounts]:
    items: list[ValidationErrorItem] = []
    schema_checks = 0
    for rule in STRUCTURAL_RULES:
        produced, checked = rule.check(ctx)
        items.extend(produced)
        schema_checks += checked
    business_checks = 0
    conflict_checks = 0
    for raw in constraints:
        constraint = raw if isinstance(raw, Constraint) else Constraint.model_validate(raw)
        rule = RULE_REGISTRY.get(constraint.type)
        if rule is None:
            raise UnregisteredRuleError(
                f"规则类型 {constraint.type.value!r} 未注册（约束 {constraint.id}）"
            )
        for item in rule.evaluate(ctx, constraint):
            items.append(_normalize_severity(item, constraint))
        if rule.layer is RuleLayer.SCHEMA:
            schema_checks += 1
        elif rule.layer is RuleLayer.BUSINESS:
            business_checks += 1
        else:
            conflict_checks += 1
    counts = CheckedCounts(
        schema_count=schema_checks,
        business=business_checks,
        conflict=conflict_checks,
    )
    return items, counts


def _normalize_severity(
    item: ValidationErrorItem,
    constraint: Constraint,
) -> ValidationErrorItem:
    """Warning codes and warning constraints never block a write."""

    if constraint.severity is Severity.WARNING or item.code in WARNING_CODES:
        return item.model_copy(update={"severity": Severity.WARNING})
    return item


def _build_report(
    items: Iterable[ValidationErrorItem],
    counts: CheckedCounts,
) -> ValidationReport:
    unique: dict[tuple[str, str, str, str], ValidationErrorItem] = {}
    for item in items:
        key = (item.code.value, item.field, item.constraint_ref or "", item.message)
        unique.setdefault(key, item)
    ordered = sorted(
        unique.values(),
        key=lambda item: (item.field, item.constraint_ref or "", item.code.value, item.message),
    )
    errors = [item for item in ordered if item.severity is Severity.ERROR]
    warnings = [item for item in ordered if item.severity is not Severity.ERROR]
    return ValidationReport(
        ok=not errors,
        checked=counts,
        errors=errors,
        warnings=warnings,
        duration_ms=0,
    )


def _crash_report(exc: BaseException) -> ValidationReport:
    detail = f"{type(exc).__name__}: {exc}".strip()
    return ValidationReport(
        ok=False,
        errors=[
            ValidationErrorItem(
                code=ErrorCode.VALIDATOR_CRASH,
                field="",
                message=f"校验器内部异常，本次校验按不通过处理（{preview(detail, limit=200)}）",
            )
        ],
        duration_ms=0,
    )
