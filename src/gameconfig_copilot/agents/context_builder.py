"""Token-budgeted context assembly for structured drafting (S3-01).

Budget model frozen by docs/02 §5.3 — total 6000 tokens::

    system + output schema   1200
    world snippets           1500   (2–3, deduplicated by entity)
    similar task templates   2500   (top-2 + 1 same-NPC-different-task)
    constraints              1200   (only rules relevant to this request)
    preference                300   (structured, never free-form prose)

Truncation is two-level: each block fits its own budget item-by-item, then a
global pass trims the lowest-priority block when the sum exceeds 6000. Note
that the per-block budgets in docs/02 §5.3 sum to 6700 while the frozen total is
6000; the global priority pass is what resolves that 700-token gap (preference
first, then task templates, then world, while constraints/system stay longest).
Every truncated/dropped item becomes a warning carrying its citation, which is
what makes "为什么生成漏了某条设定" attributable instead of mysterious.

Token counting is a deterministic heuristic (CJK chars ≈ 1 token, other chars ≈
4/token) rather than a tokenizer dependency; it deliberately over-estimates so
budgets are safe without pulling `tiktoken` into the agent layer.

Preference injection (S6-04)
    The preference block is structured key-value data only — compact JSON, never
    assembled prose. Learned values come from the frozen memory interface
    (:meth:`gameconfig_copilot.memory.preference.Preference.defaults`), and
    every value is resolved through
    :meth:`~gameconfig_copilot.memory.preference.Preference.value_for` so an
    explicit instruction (``explicit=``) beats the learned default. Each injected
    field carries its own ``source`` (``explicit`` / ``preference``), which is
    what lets the report say "来源：偏好" instead of guessing. Entries are kept
    explicit-first when the 300-token block budget is tight, and overflow reuses
    the normal truncation machinery (``CONTEXT_TRUNCATED`` + dropped labels).

Input
    Request, retrieved evidence (wiki/task/constraint), structured preference,
    optional explicit field overrides from the user's instruction.
Output
    :class:`BuiltContext` with per-block budgets, token counts, warnings and
    ready-to-send ``Message`` objects.
Failure semantics
    Missing prompt files raise; malformed preference/explicit types raise
    ``TypeError``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from ..llm.client import Message
from ..memory.preference import Preference as MemoryPreference
from .prompt_loader import load_prompt
from .state import Evidence, Preference

__all__ = [
    "BLOCK_BUDGETS",
    "BLOCK_PRIORITY",
    "BLOCK_TITLES",
    "TOTAL_BUDGET",
    "BuiltContext",
    "ContextBlock",
    "ContextBuilder",
    "ContextWarning",
    "estimate_tokens",
    "truncate_to_tokens",
]

TOTAL_BUDGET = 6000

BLOCK_BUDGETS: dict[str, int] = {
    "system": 1200,
    "world": 1500,
    "tasks": 2500,
    "constraints": 1200,
    "preference": 300,
}

#: Lower number = kept longer. The global pass trims the largest priority first.
BLOCK_PRIORITY: dict[str, int] = {
    "system": 0,
    "constraints": 1,
    "world": 2,
    "tasks": 3,
    "preference": 4,
}

BLOCK_TITLES: dict[str, str] = {
    "system": "系统提示与输出结构",
    "world": "世界观片段（按实体去重）",
    "tasks": "相似任务模板（2 条最相似 + 1 条同 NPC）",
    "constraints": "生效约束（与本次任务类型/等级相关）",
    "preference": "策划偏好（结构化）",
}

_CJK_RE = re.compile(r"[\u3000-\u303f\u3040-\u30ff\u3400-\u9fff\uff00-\uffef]")
_MAX_WORLD = 3
_MAX_TASKS = 3
_MAX_CONSTRAINTS = 8

#: Reward constraints whose [min, max] interval is injected as an explicit line
#: (values come from the constraint corpus, never from a hard-coded table).
_REWARD_INTERVAL_SCOPES: dict[str, str] = {
    "rewards.gold": "金币",
    "rewards.exp": "阅历",
}
_LEVEL_SPAN_RE = re.compile(r"(\d+)\s*(?:到|至|~|～|-|–|—)\s*(\d+)\s*级")
_LEVEL_RE = re.compile(r"(\d+)\s*级")

#: docs/02 §8 preference fields, in injection order (mirrors
#: ``gameconfig_copilot.memory.preference.PREFERENCE_FIELDS``).
_PREFERENCE_FIELDS: tuple[str, ...] = (
    "default_reward_band",
    "task_types",
    "strictness",
    "naming_style",
)

#: Draft-path / legacy preference names mapped to the canonical field above.
_PREFERENCE_ALIASES: dict[str, str] = {
    "default_reward_band": "default_reward_band",
    "reward_band": "default_reward_band",
    "reward_band_side": "default_reward_band",
    "reward": "default_reward_band",
    "rewards": "default_reward_band",
    "task_types": "task_types",
    "task_type": "task_types",
    "strictness": "strictness",
    "naming_style": "naming_style",
    "naming": "naming_style",
}

SOURCE_EXPLICIT = "explicit"
SOURCE_PREFERENCE = "preference"

_SCHEMA_GUIDE = """TaskDraft 关键字段（完整 JSON Schema 由 LLMClient.structured 注入）：
- schema_version=1；task_id 占位 T_<MAIN|SIDE|DAILY>_0000（系统分配真实 ID）
- title ≤80 字；task_type: main|side|daily
- level_range: {min,max}，min≤max，均≥1
- giver: {npc_id: NPC_*, dialogue_ref?: DLG_*}
- prerequisites: [{type: task_completed, ref: 任务ID}]
- objectives: [{seq 从 1 连续, type: talk_to|collect|kill|interact|reach,
  target, count≥1, area}]
- rewards: [{type: gold|exp|item, amount≥0, item_id 仅 type=item}]
- branches: [{branch_id, condition, objectives[], rewards[]}]
- tags: string[]；provenance: {request, evidence[], model, created_at}"""


def estimate_tokens(text: str | None) -> int:
    """Conservative token estimate (CJK 1 char/token, others 4 chars/token)."""

    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    other = len(text) - cjk
    return cjk + ((other + 3) // 4 if other else 0)


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Return the longest prefix (+ ellipsis) within ``max_tokens``."""

    if max_tokens <= 0:
        return ""
    if estimate_tokens(text) <= max_tokens:
        return text
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_tokens(text[:middle] + "…") <= max_tokens:
            low = middle
        else:
            high = middle - 1
    return text[:low] + "…" if low > 0 else ""


def _canonical_preference_field(raw: Any) -> str | None:
    """Map a draft path / legacy preference name to docs/02 §8's four fields."""

    if not isinstance(raw, str):
        return None
    lowered = raw.strip().lower()
    if lowered in _PREFERENCE_ALIASES:
        return _PREFERENCE_ALIASES[lowered]
    if lowered.startswith("reward"):
        return "default_reward_band"
    if lowered.startswith("task_type"):
        return "task_types"
    if lowered.startswith("strictness"):
        return "strictness"
    if lowered.startswith("naming"):
        return "naming_style"
    return None


@dataclass(frozen=True, slots=True)
class ContextWarning:
    """A traceable truncation/failure record."""

    code: str
    message: str
    block: str = ""
    detail: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "block": self.block,
            "detail": list(self.detail),
        }


@dataclass(frozen=True, slots=True)
class ContextBlock:
    """One budgeted context section."""

    name: str
    title: str
    budget: int
    tokens: int
    text: str
    priority: int
    truncated: tuple[str, ...] = ()
    dropped: tuple[str, ...] = ()

    @property
    def within_budget(self) -> bool:
        return self.tokens <= self.budget

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "budget": self.budget,
            "tokens": self.tokens,
            "truncated": list(self.truncated),
            "dropped": list(self.dropped),
        }


@dataclass(frozen=True, slots=True)
class BuiltContext:
    """The assembled, budget-checked context."""

    blocks: tuple[ContextBlock, ...]
    warnings: tuple[ContextWarning, ...]
    total_tokens: int
    prompt_name: str = "task"
    schema_source: str = "llm.structured"

    def block(self, name: str) -> ContextBlock:
        for item in self.blocks:
            if item.name == name:
                return item
        raise KeyError(f"未知上下文区块：{name}")

    @property
    def warning_dicts(self) -> list[dict[str, Any]]:
        return [warning.to_dict() for warning in self.warnings]

    def render(self) -> str:
        """Render everything except the system block as labelled sections."""

        sections: list[str] = []
        for item in self.blocks:
            if item.name == "system" or not item.text:
                continue
            sections.append(f"## {item.title}\n{item.text}")
        return "\n\n".join(sections)

    def to_messages(self, request: str) -> list[Message]:
        """Build the ``[system, user]`` pair consumed by ``LLMClient``."""

        system = self.block("system").text.strip()
        user_parts = []
        body = self.render()
        if body:
            user_parts.append(body)
        user_parts.append(f"## 策划需求\n{request.strip()}")
        return [Message.system(system), Message.user("\n\n".join(user_parts))]

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_tokens": self.total_tokens,
            "prompt_name": self.prompt_name,
            "schema_source": self.schema_source,
            "blocks": [item.to_dict() for item in self.blocks],
            "warnings": self.warning_dicts,
        }


@dataclass(frozen=True, slots=True)
class _Item:
    label: str
    text: str
    score: float


@dataclass(frozen=True, slots=True)
class _PreferenceEntry:
    """One injected preference field, tagged with where its value came from."""

    name: str
    value: Any
    source: str


@dataclass
class ContextBuilder:
    """Deterministic block-budgeted context assembler."""

    prompt_name: str = "task"
    budgets: Mapping[str, int] = field(
        default_factory=lambda: dict(BLOCK_BUDGETS)
    )
    total_budget: int = TOTAL_BUDGET
    _prompt: str | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        missing = set(BLOCK_BUDGETS) - set(self.budgets)
        if missing:
            raise ValueError(f"budgets 缺少区块：{sorted(missing)}")
        if self.total_budget <= 0:
            raise ValueError("total_budget 必须 > 0")
        for name, budget in self.budgets.items():
            if budget < 0:
                raise ValueError(f"区块 {name} 的预算不能为负")

    @property
    def prompt(self) -> str:
        if self._prompt is None:
            self._prompt = load_prompt(self.prompt_name)
        return self._prompt

    # ------------------------------------------------------------------ API
    def build(
        self,
        request: str,
        *,
        evidence: Sequence[Evidence] | None = None,
        constraints: Sequence[Evidence] | None = None,
        preference: Preference | MemoryPreference | Mapping[str, Any] | None = None,
        explicit: Mapping[str, Any] | None = None,
    ) -> BuiltContext:
        """Assemble all blocks, enforce budgets and record truncations.

        ``explicit`` carries the user's explicit instruction for a preference
        field (e.g. ``{"default_reward_band": 500}``). It is resolved through
        :meth:`~gameconfig_copilot.memory.preference.Preference.value_for`, so
        it beats the learned default and is injected with
        ``source="explicit"``.
        """

        evidence_items = list(evidence or ())
        constraint_items = list(constraints or ())
        warnings: list[ContextWarning] = []

        system_text = f"{self.prompt.strip()}\n\n## 输出结构与字段约束\n{_SCHEMA_GUIDE}"
        blocks: list[ContextBlock] = [
            self._fit_text_block(
                "system",
                system_text,
                labels=("system_prompt", "schema_guide"),
                warnings=warnings,
            )
        ]

        world_items = self._select_world(evidence_items)
        blocks.append(self._fit_items_block("world", world_items, warnings))

        task_items = self._select_tasks(evidence_items)
        blocks.append(self._fit_items_block("tasks", task_items, warnings))

        # Interval rows go first so a budget squeeze can never hide the floor.
        constraint_rows = self._reward_interval_items(constraint_items, request)
        constraint_rows.extend(self._select_constraints(constraint_items))
        blocks.append(self._fit_items_block("constraints", constraint_rows, warnings))

        entries, designer_id = self._preference_entries(preference, explicit)
        blocks.append(
            self._fit_preference_block(entries, designer_id, warnings),
        )

        blocks, warnings = self._enforce_global_budget(blocks, warnings)
        total = sum(item.tokens for item in blocks)
        return BuiltContext(
            blocks=tuple(blocks),
            warnings=tuple(warnings),
            total_tokens=total,
            prompt_name=self.prompt_name,
        )

    # -------------------------------------------------------------- selection
    @staticmethod
    def _select_world(evidence: Sequence[Evidence]) -> list[_Item]:
        selected: list[_Item] = []
        seen_sources: set[str] = set()
        for item in sorted(evidence, key=lambda row: row.score, reverse=True):
            if item.source_type != "wiki" or item.source_id in seen_sources:
                continue
            seen_sources.add(item.source_id)
            selected.append(_Item(item.citation, item.text, item.score))
            if len(selected) >= _MAX_WORLD:
                break
        return selected

    @staticmethod
    def _select_tasks(evidence: Sequence[Evidence]) -> list[_Item]:
        tasks = sorted(
            (item for item in evidence if item.source_type == "task"),
            key=lambda row: row.score,
            reverse=True,
        )
        selected: list[_Item] = []
        seen: set[str] = set()
        for item in tasks[:2]:
            selected.append(_Item(item.citation, item.text, item.score))
            seen.add(item.citation)
        if selected:
            first_meta = next(
                (item for item in tasks if item.citation == selected[0].label),
                None,
            )
            npc_id = (
                str(first_meta.metadata.get("npc_id"))
                if first_meta is not None and first_meta.metadata.get("npc_id")
                else ""
            )
            if npc_id:
                for item in tasks:
                    if item.citation in seen:
                        continue
                    if str(item.metadata.get("npc_id", "")) != npc_id:
                        continue
                    selected.append(_Item(item.citation, item.text, item.score))
                    seen.add(item.citation)
                    break
        return selected[:_MAX_TASKS]

    @staticmethod
    def _select_constraints(constraints: Sequence[Evidence]) -> list[_Item]:
        selected: list[_Item] = []
        seen: set[str] = set()
        for item in sorted(constraints, key=lambda row: row.score, reverse=True):
            key = str(item.metadata.get("constraint_id") or item.citation)
            if key in seen:
                continue
            seen.add(key)
            selected.append(_Item(item.citation, item.text, item.score))
            if len(selected) >= _MAX_CONSTRAINTS:
                break
        return selected

    @staticmethod
    def _request_level(request: str) -> int | None:
        """Lowest level named by the request (``6 级`` / ``14 到 20 级``)."""

        span = _LEVEL_SPAN_RE.search(request)
        if span is not None:
            return int(span.group(1))
        single = _LEVEL_RE.search(request)
        return int(single.group(1)) if single is not None else None

    @staticmethod
    def _band_for_level(
        bands: Sequence[Mapping[str, Any]], level: int | None
    ) -> Mapping[str, Any] | None:
        ordered = sorted(bands, key=lambda band: int(band.get("max_level", 0)))
        if not ordered:
            return None
        if level is None:
            return ordered[0]
        for band in ordered:
            if level <= int(band.get("max_level", 0)):
                return band
        return ordered[-1]

    @classmethod
    def _reward_interval_items(
        cls, constraints: Sequence[Evidence], request: str
    ) -> list[_Item]:
        """Explicit ``[min, max]`` rows for reward constraints (values from YAML).

        The generation prompt must see both bounds, not just the cap: the
        validator rejects a reward below ``min_amount`` with ``E_REWARD_RANGE``.
        Bands come from the constraint chunk metadata, so editing the YAML is
        the only way to change these numbers.
        """

        level = cls._request_level(request)
        items: list[_Item] = []
        seen: set[tuple[str, int | None, int | None]] = set()
        for evidence in constraints:
            metadata = evidence.metadata
            if str(metadata.get("constraint_type", "")) != "level_band_range":
                continue
            scope = str(metadata.get("scope", ""))
            label = _REWARD_INTERVAL_SCOPES.get(scope)
            if label is None:
                continue
            bands = [
                band
                for band in (metadata.get("bands") or ())
                if isinstance(band, Mapping)
            ]
            band = cls._band_for_level(bands, level)
            if band is None:
                continue
            low, high = band.get("min_amount"), band.get("max_amount")
            if not isinstance(low, int) or not isinstance(high, int):
                continue
            key = (label, low, high)
            if key in seen:
                continue
            seen.add(key)
            max_level = band.get("max_level", "?")
            if level is None:
                ranges = "；".join(
                    f"Lv≤{row.get('max_level', '?')} [{row.get('min_amount')}, "
                    f"{row.get('max_amount')}]"
                    for row in sorted(
                        bands, key=lambda row: int(row.get("max_level", 0))
                    )
                )
                text = f"{label}奖励必须按等级档落在 {ranges} 内（下限=同档上限的 30%）"
            else:
                text = (
                    f"{label}奖励必须落在 [{low}, {high}] 内"
                    f"（Lv{level} 适用 Lv≤{max_level} 档；下限=同档上限的 30%），"
                    "低于下限或高于上限都会被 E_REWARD_RANGE 拒绝"
                )
            items.append(_Item(f"reward-interval-{scope.rsplit('.', 1)[-1]}", text, 1.0))
        return items

    @staticmethod
    def _preference_entries(
        preference: Preference | MemoryPreference | Mapping[str, Any] | None,
        explicit: Mapping[str, Any] | None,
    ) -> tuple[list[_PreferenceEntry], str]:
        """Resolve learned defaults + explicit overrides into tagged fields.

        The learned side is the frozen memory interface: ``defaults()`` says
        which fields were learned, ``value_for()`` resolves each one so an
        explicit instruction wins (docs/02 §8). Legacy ``Preference`` /
        mapping payloads are still accepted for backward compatibility; their
        keys are canonicalised where they match a preference field.
        """

        if explicit is not None and not isinstance(explicit, Mapping):
            raise TypeError(
                "explicit 必须是映射或 None，"
                f"收到 {type(explicit).__name__}"
            )
        entries: dict[str, _PreferenceEntry] = {}
        designer_id = ""
        if isinstance(preference, MemoryPreference):
            designer_id = preference.designer_id
            defaults = preference.defaults()
            for name in _PREFERENCE_FIELDS:
                if name not in defaults:
                    continue
                entries[name] = _PreferenceEntry(
                    name, preference.value_for(name), SOURCE_PREFERENCE
                )
        elif preference is not None:
            if isinstance(preference, Preference):
                payload: Mapping[str, Any] = preference.to_dict()
            elif isinstance(preference, Mapping):
                payload = {str(key): value for key, value in preference.items()}
            else:
                raise TypeError(
                    "preference 必须是 Preference、记忆 Preference、映射或 None，"
                    f"收到 {type(preference).__name__}"
                )
            for raw_key, value in payload.items():
                if value in (None, "", [], {}, ()):
                    continue
                name = _canonical_preference_field(raw_key) or raw_key
                entries.setdefault(
                    name, _PreferenceEntry(name, value, SOURCE_PREFERENCE)
                )

        for raw_key, raw_value in (explicit or {}).items():
            if raw_value is None:
                continue
            name = _canonical_preference_field(raw_key) or str(raw_key)
            if isinstance(preference, MemoryPreference) and name in _PREFERENCE_FIELDS:
                value = preference.value_for(name, explicit=raw_value)
            else:
                value = raw_value
            entries[name] = _PreferenceEntry(name, value, SOURCE_EXPLICIT)

        return list(entries.values()), designer_id

    @staticmethod
    def _render_preference(
        entries: Sequence[_PreferenceEntry],
        designer_id: str = "",
    ) -> str:
        """Compact structured payload; no prose is ever assembled here."""

        if not entries:
            return ""
        payload: dict[str, Any] = {
            "priority": "explicit > preference",
            "defaults": {
                entry.name: {"value": entry.value, "source": entry.source}
                for entry in entries
            },
        }
        if designer_id:
            payload["designer_id"] = designer_id
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        )

    def _fit_preference_block(
        self,
        entries: Sequence[_PreferenceEntry],
        designer_id: str,
        warnings: list[ContextWarning],
    ) -> ContextBlock:
        """Fit tagged fields into the 300-token block, explicit entries first.

        Dropping whole fields (instead of clipping the JSON mid-string) keeps
        the injected payload parseable, the same way the item blocks drop
        individual snippets.
        """

        budget = int(self.budgets["preference"])
        kept = sorted(entries, key=lambda entry: entry.source != SOURCE_EXPLICIT)
        dropped: list[str] = []
        text = self._render_preference(kept, designer_id)
        while kept and estimate_tokens(text) > budget:
            dropped.append(kept.pop().name)
            text = self._render_preference(kept, designer_id)
        if dropped:
            warnings.append(
                ContextWarning(
                    code="CONTEXT_TRUNCATED",
                    message=f"{BLOCK_TITLES['preference']} 超出预算 {budget} tokens："
                    f"丢弃 {len(dropped)} 项",
                    block="preference",
                    detail=tuple(dropped),
                )
            )
        return ContextBlock(
            name="preference",
            title=BLOCK_TITLES["preference"],
            budget=budget,
            tokens=estimate_tokens(text),
            text=text,
            priority=BLOCK_PRIORITY["preference"],
            dropped=tuple(dropped),
        )

    # --------------------------------------------------------------- fitting
    def _fit_text_block(
        self,
        name: str,
        text: str,
        *,
        labels: tuple[str, ...],
        warnings: list[ContextWarning],
    ) -> ContextBlock:
        budget = int(self.budgets[name])
        tokens = estimate_tokens(text)
        truncated: tuple[str, ...] = ()
        if tokens > budget:
            text = truncate_to_tokens(text, budget)
            tokens = estimate_tokens(text)
            truncated = labels
            warnings.append(
                ContextWarning(
                    code="CONTEXT_TRUNCATED",
                    message=f"{BLOCK_TITLES[name]} 超出预算 {budget} tokens，已截断",
                    block=name,
                    detail=labels,
                )
            )
        return ContextBlock(
            name=name,
            title=BLOCK_TITLES[name],
            budget=budget,
            tokens=tokens,
            text=text,
            priority=BLOCK_PRIORITY[name],
            truncated=truncated,
        )

    def _fit_items_block(
        self,
        name: str,
        items: Sequence[_Item],
        warnings: list[ContextWarning],
    ) -> ContextBlock:
        budget = int(self.budgets[name])
        rendered: list[str] = []
        used = 0
        truncated: list[str] = []
        dropped: list[str] = []
        for index, item in enumerate(items):
            block_text = f"- [{item.label}] {' '.join(item.text.split())}"
            cost = estimate_tokens(block_text) + 1
            if used + cost <= budget:
                rendered.append(block_text)
                used += cost
                continue
            remaining = budget - used - 1
            if remaining >= 60:
                clipped = truncate_to_tokens(block_text, remaining)
                if clipped:
                    rendered.append(clipped)
                    used += estimate_tokens(clipped) + 1
                    truncated.append(item.label)
                    continue
            dropped.extend(row.label for row in items[index:])
            break
        text = "\n".join(rendered)
        tokens = estimate_tokens(text)
        if truncated or dropped:
            parts = []
            if truncated:
                parts.append(f"截断 {len(truncated)} 条")
            if dropped:
                parts.append(f"丢弃 {len(dropped)} 条")
            warnings.append(
                ContextWarning(
                    code="CONTEXT_TRUNCATED",
                    message=f"{BLOCK_TITLES[name]} 超出预算 {budget} tokens："
                    + "，".join(parts),
                    block=name,
                    detail=tuple(truncated + dropped),
                )
            )
        return ContextBlock(
            name=name,
            title=BLOCK_TITLES[name],
            budget=budget,
            tokens=tokens,
            text=text,
            priority=BLOCK_PRIORITY[name],
            truncated=tuple(truncated),
            dropped=tuple(dropped),
        )

    def _enforce_global_budget(
        self,
        blocks: list[ContextBlock],
        warnings: list[ContextWarning],
    ) -> tuple[list[ContextBlock], list[ContextWarning]]:
        rows = list(blocks)
        while sum(item.tokens for item in rows) > self.total_budget:
            total = sum(item.tokens for item in rows)
            excess = total - self.total_budget
            candidates = sorted(
                (item for item in rows if item.tokens > 0),
                key=lambda item: item.priority,
                reverse=True,
            )
            if not candidates:
                break
            target = candidates[0]
            index = rows.index(target)
            keep = max(0, target.tokens - excess)
            clipped = truncate_to_tokens(target.text, keep)
            rows[index] = replace(
                target,
                text=clipped,
                tokens=estimate_tokens(clipped),
                truncated=(*target.truncated, "global_budget"),
            )
            warnings.append(
                ContextWarning(
                    code="CONTEXT_TRUNCATED",
                    message=f"上下文总预算 {self.total_budget} tokens 不足，"
                    f"按优先级截断{BLOCK_TITLES[target.name]}",
                    block=target.name,
                    detail=("global_budget",),
                )
            )
        return rows, warnings
