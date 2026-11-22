"""Chunkers: Wiki headings + sliding windows, semantic task JSON, one-per-rule YAML.

Design decisions (docs/03 S1, ADR-002):

* **Wiki** — split by Markdown heading hierarchy first, then window long sections
  into 400–600 character pieces with ~10% overlap. The heading path is kept in
  ``position`` so a citation reads ``wiki:world_overview#三、地点与通行``.
* **Task JSON** — one parent chunk for the whole task plus child chunks for
  ``objectives`` / ``rewards`` / ``branches``. Children carry ``task_id`` and
  ``parent_id`` so generation can locate a field and then pull the full structure
  from the parent (docs/03 S1 "子块定位 + 父块补全").
* **Constraint YAML** — one chunk per constraint entry: rules must never be
  merged, otherwise a citation cannot point back to exactly one ``C_*`` id.

Input
    Markdown text, a parsed task mapping, or a list of parsed YAML mappings.
Output
    ``list[Chunk]`` with neutral metadata; ids are deterministic, so rebuilding
    the index is idempotent.
Failure semantics
    Missing required identifiers raise :class:`ValueError`; malformed task
    collections raise :class:`TypeError`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .store import Chunk

__all__ = [
    "MAX_CHUNK_CHARS",
    "MIN_CHUNK_CHARS",
    "OVERLAP_RATIO",
    "WikiSection",
    "chunk_constraints",
    "chunk_task",
    "chunk_wiki",
    "split_windows",
    "split_wiki_sections",
]

#: Wiki window bounds and overlap (docs/03 S1: 400–600 字, 10% overlap).
MIN_CHUNK_CHARS = 400
MAX_CHUNK_CHARS = 600
OVERLAP_RATIO = 0.10

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_SENTENCE_END = "。！？；!?;\n"

_TASK_TYPE_LABELS = {"main": "主线", "side": "支线", "daily": "日常"}
_OBJECTIVE_LABELS = {
    "talk_to": "对话",
    "collect": "采集",
    "kill": "击杀",
    "interact": "交互",
    "reach": "到达",
}
_REWARD_LABELS = {"gold": "金币", "exp": "阅历", "item": "道具"}


@dataclass(frozen=True, slots=True)
class WikiSection:
    """One heading section: the ancestor title path plus its body text."""

    heading_path: tuple[str, ...]
    heading_levels: tuple[int, ...]
    level: int
    body: str

    @property
    def title(self) -> str:
        return self.heading_path[-1] if self.heading_path else ""

    @property
    def display_path(self) -> tuple[str, ...]:
        """Heading path without the document-level H1 (source_id already names it)."""

        if len(self.heading_path) > 1 and self.heading_levels and self.heading_levels[0] == 1:
            return self.heading_path[1:]
        return self.heading_path


def split_wiki_sections(text: str) -> list[WikiSection]:
    """Split Markdown into sections, preserving the heading hierarchy.

    A section's body ends at the next heading of any level; the ancestor path is
    recovered with a level stack. Fenced code blocks are skipped so ``#`` inside
    code is not mistaken for a heading.
    """

    sections: list[WikiSection] = []
    stack: list[tuple[int, str]] = []
    buffer: list[str] = []
    in_fence = False

    def flush() -> None:
        body = "\n".join(buffer).strip()
        if body:
            path = tuple(title for _, title in stack)
            levels = tuple(level for level, _ in stack)
            sections.append(WikiSection(path, levels, stack[-1][0] if stack else 0, body))
        buffer.clear()

    for line in text.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            buffer.append(line)
            continue
        match = None if in_fence else _HEADING_RE.match(line)
        if match is None:
            buffer.append(line)
            continue
        flush()
        level = len(match.group(1))
        title = match.group(2).strip()
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
    flush()
    return sections


def _cut_point(text: str, start: int, end: int, min_chars: int) -> int:
    """Prefer a sentence boundary in the last part of the window."""

    floor = start + min_chars
    if floor >= end:
        return end
    for index in range(end - 1, floor - 1, -1):
        if text[index] in _SENTENCE_END:
            return index + 1
    return end


def split_windows(
    text: str,
    *,
    min_chars: int = MIN_CHUNK_CHARS,
    max_chars: int = MAX_CHUNK_CHARS,
    overlap_ratio: float = OVERLAP_RATIO,
) -> list[tuple[int, int, str]]:
    """Window text into ``(start, end, piece)`` triples with overlap.

    Windows are at most ``max_chars`` long and use a sentence boundary when one
    exists after ``min_chars``. The final window may be shorter than
    ``min_chars``; sections shorter than ``min_chars`` produce one window.
    """

    if min_chars <= 0 or max_chars <= 0:
        raise ValueError("min_chars / max_chars 必须为正数")
    if min_chars > max_chars:
        raise ValueError(f"min_chars ({min_chars}) 不能大于 max_chars ({max_chars})")
    if not 0 <= overlap_ratio < 1:
        raise ValueError(f"overlap_ratio 必须在 [0, 1) 内，当前为 {overlap_ratio}")
    stripped = text.strip()
    if not stripped:
        return []

    windows: list[tuple[int, int, str]] = []
    length = len(stripped)
    start = 0
    while start < length:
        end = min(start + max_chars, length)
        if end < length:
            end = _cut_point(stripped, start, end, min_chars)
        piece = stripped[start:end].strip()
        if piece:
            windows.append((start, end, piece))
        if end >= length:
            break
        overlap = max(1, int((end - start) * overlap_ratio))
        next_start = end - overlap
        start = next_start if next_start > start else start + 1
    return windows


def chunk_wiki(
    text: str,
    *,
    source_name: str,
    source_path: str | None = None,
) -> list[Chunk]:
    """Chunk one Wiki document; ``source_name`` is the file stem."""

    if not source_name:
        raise ValueError("source_name 不能为空")
    source_id = f"wiki:{source_name}"
    chunks: list[Chunk] = []
    for section_index, section in enumerate(split_wiki_sections(text), start=1):
        path_titles = section.heading_path
        position = " / ".join(section.display_path)
        topic = section.title or source_name
        windows = split_windows(section.body)
        for window_index, (start, end, piece) in enumerate(windows, start=1):
            chunk_id = f"wiki:{source_name}:s{section_index:02d}:w{window_index:02d}"
            metadata: dict[str, Any] = {
                "source_type": "wiki",
                "source_id": source_id,
                "position": position,
                "parent_id": None,
                "topic": topic,
                "heading_path": list(path_titles),
                "heading_levels": list(section.heading_levels),
                "section_level": section.level,
                "window_index": window_index,
                "window_total": len(windows),
                "window_start": start,
                "window_end": end,
                "path": source_path or f"wiki/{source_name}.md",
            }
            chunks.append(Chunk(chunk_id=chunk_id, text=piece, metadata=metadata))
    return chunks


def _render_objective(objective: Mapping[str, Any]) -> str:
    objective_type = str(objective.get("type", "unknown"))
    label = _OBJECTIVE_LABELS.get(objective_type, objective_type)
    target = objective.get("target", "?")
    count = objective.get("count", 1)
    area = objective.get("area", "?")
    return f"{label} {target} ×{count}（区域 {area}）"


def _render_reward(reward: Mapping[str, Any]) -> str:
    reward_type = str(reward.get("type", "unknown"))
    label = _REWARD_LABELS.get(reward_type, reward_type)
    amount = reward.get("amount", 0)
    if reward_type == "item":
        return f"{label} {reward.get('item_id', '?')} ×{amount}"
    return f"{label} {amount}"


def _render_objectives(objectives: Iterable[Mapping[str, Any]]) -> str:
    lines = []
    for objective in objectives:
        seq = objective.get("seq", "?")
        lines.append(f"{seq}. {_render_objective(objective)}")
    return "\n".join(lines) if lines else "无"


def _render_branch(branch: Mapping[str, Any]) -> str:
    rewards = "；".join(_render_reward(item) for item in branch.get("rewards") or []) or "无"
    lines = [
        f"分支 {branch.get('branch_id', '?')}：条件「{branch.get('condition', '?')}」",
        f"  目标：{_render_objectives(branch.get('objectives') or [])}",
        f"  奖励：{rewards}",
    ]
    return "\n".join(lines)


def _render_rewards(rewards: Iterable[Mapping[str, Any]]) -> str:
    rendered = "；".join(_render_reward(reward) for reward in rewards)
    return rendered or "无"


def _task_metadata(task: Mapping[str, Any], *, source_path: str | None) -> dict[str, Any]:
    task_id = str(task.get("task_id") or "").strip()
    if not task_id:
        raise ValueError("任务缺少 task_id，无法生成稳定 chunk")
    level_range = task.get("level_range") or {}
    giver = task.get("giver") or {}
    metadata: dict[str, Any] = {
        "source_type": "task",
        "source_id": f"task:{task_id}",
        "parent_id": None,
        "task_id": task_id,
        "task_type": task.get("task_type"),
        "level_min": level_range.get("min"),
        "level_max": level_range.get("max"),
        "npc_id": giver.get("npc_id"),
        "tags": list(task.get("tags") or []),
        "path": source_path or f"tasks/{task_id}.json",
    }
    return metadata


def chunk_task(
    task: Mapping[str, Any],
    *,
    source_path: str | None = None,
) -> list[Chunk]:
    """Chunk one task JSON into a parent block plus semantic child blocks."""

    if not isinstance(task, Mapping):
        raise TypeError("chunk_task 只接受映射（已解析的 JSON 对象）")
    base = _task_metadata(task, source_path=source_path)
    task_id = str(base["task_id"])
    title = str(task.get("title") or "").strip() or task_id
    task_type = str(task.get("task_type") or "")
    type_label = _TASK_TYPE_LABELS.get(task_type, task_type or "未标注")
    level_min = base.get("level_min")
    level_max = base.get("level_max")
    level_text = (
        f"Lv{level_min}–{level_max}"
        if level_min is not None and level_max is not None
        else "未标注"
    )
    giver = task.get("giver") or {}
    dialogue = giver.get("dialogue_ref") or "无"
    prerequisites = task.get("prerequisites") or []
    prerequisite_text = (
        "、".join(str(item.get("ref")) for item in prerequisites) if prerequisites else "无"
    )
    branches = list(task.get("branches") or [])

    parent_lines = [
        f"任务 {task_id}「{title}」",
        f"类型：{type_label}（{task_type or 'unknown'}）；等级段：{level_text}；"
        f"发布者：{base.get('npc_id') or '未标注'}；对白：{dialogue}",
        f"前置：{prerequisite_text}",
        f"目标：\n{_render_objectives(task.get('objectives') or [])}",
        f"奖励：{_render_rewards(task.get('rewards') or [])}",
        "分支：\n" + ("\n".join(_render_branch(item) for item in branches) if branches else "无"),
        f"标签：{'、'.join(base['tags']) if base['tags'] else '无'}",
    ]
    parent = Chunk(
        chunk_id=f"task:{task_id}",
        text="\n".join(parent_lines),
        metadata={**base, "position": "task"},
    )

    child_specs: list[tuple[str, str]] = [
        ("objectives", "目标（objectives）"),
        ("rewards", "奖励（rewards）"),
        ("branches", "分支（branches）"),
    ]
    child_texts: dict[str, str] = {
        "objectives": _render_objectives(task.get("objectives") or []),
        "rewards": _render_rewards(task.get("rewards") or []),
        "branches": "\n".join(_render_branch(item) for item in branches) if branches else "无",
    }
    children = [
        Chunk(
            chunk_id=f"task:{task_id}#{field}",
            text=f"任务 {task_id}「{title}」的{label}：\n{child_texts[field]}",
            metadata={
                **base,
                "position": field,
                "parent_id": parent.chunk_id,
            },
        )
        for field, label in child_specs
    ]
    return [parent, *children]


def _render_bands(bands: Iterable[Mapping[str, Any]]) -> str:
    parts: list[str] = []
    for band in bands:
        max_level = band.get("max_level", "?")
        max_amount = band.get("max_amount", "?")
        min_amount = band.get("min_amount")
        if min_amount is None:
            parts.append(f"Lv≤{max_level} 上限 {max_amount}")
        else:
            parts.append(f"Lv≤{max_level} [{min_amount}, {max_amount}]")
    return "；".join(parts) or "无"


def chunk_constraints(
    items: Sequence[Mapping[str, Any]],
    *,
    source_file: str,
) -> list[Chunk]:
    """One chunk per YAML constraint entry (never merge rules)."""

    chunks: list[Chunk] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, Mapping):
            raise TypeError(f"约束条目 #{index} 不是映射：{type(item)!r}")
        constraint_id = str(item.get("id") or "").strip()
        if not constraint_id:
            raise ValueError(f"约束条目 #{index} 缺少 id（{source_file}）")
        scope = str(item.get("scope") or "")
        severity = str(item.get("severity") or "error")
        constraint_type = str(item.get("type") or "")
        lines = [
            f"约束 {constraint_id}（scope={scope or '未标注'}；"
            f"type={constraint_type or '未标注'}；severity={severity}）",
            f"规则：{item.get('message') or '未提供'}",
            f"理由：{item.get('rationale') or '未提供'}",
        ]
        if item.get("bands"):
            lines.append(f"分档：{_render_bands(item['bands'])}")
        if item.get("params"):
            params = "，".join(f"{key}={value}" for key, value in item["params"].items())
            lines.append(f"参数：{params}")
        if item.get("target"):
            lines.append(f"目标：{item['target']}")
        metadata: dict[str, Any] = {
            "source_type": "constraint",
            "source_id": f"constraint:{constraint_id}",
            "parent_id": None,
            "position": scope or constraint_id,
            "constraint_id": constraint_id,
            "constraint_type": constraint_type,
            "scope": scope,
            "severity": severity,
            "path": source_file,
        }
        if item.get("bands"):
            # Structured copy so context assembly can inject the interval without
            # re-parsing the rendered rule text.
            metadata["bands"] = [
                {str(key): value for key, value in band.items()}
                for band in item["bands"]
                if isinstance(band, Mapping)
            ]
        chunks.append(
            Chunk(
                chunk_id=f"constraint:{constraint_id}",
                text="\n".join(lines),
                metadata=metadata,
            )
        )
    return chunks
