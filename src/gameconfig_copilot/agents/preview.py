"""Draft preview: diff + fingerprint, zero writes (S3-03 / ADR-004 seam).

The preview node computes what a future commit *would* change and a content
fingerprint that :mod:`gameconfig_copilot.api` binds a ``commit_token`` to. It
never opens a file for writing and never creates temp files — the
``human_gate``/``commit`` nodes run only after a confirmation.

Two preview sources are supported and normalised to the same graph shape:

* :func:`build_preview` — the in-process S3 preview (offline graph, unit tests);
* the MCP ``preview_draft`` tool — authoritative for writes because its
  fingerprint binds ``draft + target_path + base_revision`` (docs/02 §6.4) and
  is the one ``commit_draft`` re-computes under the lock.

Input
    A schema-valid draft mapping and the tasks directory (or a preview dict from
    an injected previewer).
Output
    Preview dict with ``draft_id``, ``target_path``, ``fingerprint``, ``diff``,
    ``revision`` and ``base_revision``.
Failure semantics
    An illegal target path raises ``E_PATH_DENIED``; unreadable/invalid existing
    JSON raises ``E_IO``; an MCP error envelope raises its own ``E_*`` code; all
    are converted to ``rejected`` by the graph.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "PreviewError",
    "PreviewResult",
    "build_preview",
    "fingerprint_draft",
    "normalize_preview",
]

_TASK_ID_RE_STR = r"^T_(MAIN|SIDE|DAILY)_\d{4}$"
_MAX_DIFF_ITEMS = 200


class PreviewError(RuntimeError):
    """Preview could not be produced; ``code`` is a frozen/E_* style code."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self)}


@dataclass(frozen=True, slots=True)
class PreviewResult:
    """Everything the human gate needs, without touching the target file."""

    draft_id: str
    target_path: str
    fingerprint: str
    diff: dict[str, Any]
    revision: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "target_path": self.target_path,
            "fingerprint": self.fingerprint,
            "diff": self.diff,
            "revision": self.revision,
        }


def fingerprint_draft(draft: Mapping[str, Any]) -> str:
    """Stable SHA-256 over canonical draft JSON."""

    canonical = json.dumps(
        dict(draft),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
        allow_nan=False,
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _diff(old: Any, new: Any, path: str = "") -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    if isinstance(old, Mapping) and isinstance(new, Mapping):
        for key in sorted(set(old) | set(new), key=str):
            child = f"{path}.{key}" if path else str(key)
            if key not in old:
                changes.append({"path": child, "before": None, "after": new[key]})
            elif key not in new:
                changes.append({"path": child, "before": old[key], "after": None})
            else:
                changes.extend(_diff(old[key], new[key], child))
        return changes
    if isinstance(old, list) and isinstance(new, list):
        for index in range(max(len(old), len(new))):
            child = f"{path}[{index}]" if path else f"[{index}]"
            if index >= len(old):
                changes.append({"path": child, "before": None, "after": new[index]})
            elif index >= len(new):
                changes.append({"path": child, "before": old[index], "after": None})
            else:
                changes.extend(_diff(old[index], new[index], child))
        return changes
    if old != new:
        changes.append({"path": path or "$", "before": old, "after": new})
    return changes


def _resolve_target(tasks_dir: Path, task_id: str) -> Path:
    if re.fullmatch(_TASK_ID_RE_STR, task_id) is None:
        raise PreviewError(f"非法 task_id：{task_id!r}", code="E_PATH_DENIED")
    target = (tasks_dir / f"{task_id}.json").resolve()
    root = tasks_dir.resolve()
    if target.parent != root or target.name != f"{task_id}.json":
        raise PreviewError(f"目标路径越界：{target}", code="E_PATH_DENIED")
    return target


def build_preview(
    draft: Mapping[str, Any],
    *,
    tasks_dir: str | Path = "data/tasks",
) -> PreviewResult:
    """Compute target path, fingerprint and diff; never writes anything."""

    task_id = str(draft.get("task_id", "")).strip()
    target = _resolve_target(Path(tasks_dir), task_id)
    fingerprint = fingerprint_draft(draft)

    existing: Mapping[str, Any] | None = None
    if target.is_file():
        try:
            payload = json.loads(target.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PreviewError(f"读取现有任务配置失败：{target}: {exc}", code="E_IO") from exc
        if not isinstance(payload, Mapping):
            raise PreviewError(f"现有任务文件不是 JSON 对象：{target}", code="E_IO")
        existing = payload

    if existing is None:
        diff: dict[str, Any] = {
            "created": True,
            "changed": [],
            "added": [{"path": "$", "after": task_id}],
            "removed": [],
        }
        revision = 0
    else:
        changes = _diff(existing, draft)
        diff = {
            "created": False,
            "changed": changes[:_MAX_DIFF_ITEMS],
            "added": [],
            "removed": [],
            "change_count": len(changes),
        }
        try:
            revision = int(existing.get("revision", 0))
        except (TypeError, ValueError):
            revision = 0

    return PreviewResult(
        draft_id=f"dr_{fingerprint.split(':', 1)[1][:12]}",
        target_path=str(target),
        fingerprint=fingerprint,
        diff=diff,
        revision=revision,
    )


def normalize_preview(
    raw: PreviewResult | Mapping[str, Any],
) -> dict[str, Any]:
    """Normalise a preview result/envelope into the graph/SSE preview dict.

    Accepts :class:`PreviewResult` and the MCP ``preview_draft`` payload
    (``{ok, target_path, revision, unified_diff, stats, fingerprint}``). The
    ``base_revision`` alias carries the content revision the fingerprint was
    computed against, so the confirm path can re-check the draft without
    re-reading the file.
    """

    if isinstance(raw, PreviewResult):
        data = raw.to_dict()
        return {**data, "base_revision": str(raw.revision)}
    if not isinstance(raw, Mapping):
        raise PreviewError(
            f"preview 必须返回 PreviewResult 或映射，收到 {type(raw).__name__}",
            code="E_IO",
        )
    if raw.get("ok") is False:
        error = raw.get("error")
        details = dict(error) if isinstance(error, Mapping) else {}
        raise PreviewError(
            str(details.get("message") or "预览失败"),
            code=str(details.get("code") or "E_IO"),
        )
    fingerprint = str(raw.get("fingerprint") or "")
    if not fingerprint:
        raise PreviewError("preview 结果缺少 fingerprint", code="E_IO")
    revision = raw.get("revision", 0)
    diff = raw.get("diff")
    if isinstance(diff, Mapping):
        normalized_diff = dict(diff)
    else:
        normalized_diff = {
            "unified_diff": str(raw.get("unified_diff") or ""),
            "stats": dict(raw.get("stats") or {}),
        }
    digest = fingerprint.split(":", 1)[-1]
    return {
        "draft_id": str(raw.get("draft_id") or f"dr_{digest[:12]}"),
        "target_path": str(raw.get("target_path") or ""),
        "fingerprint": fingerprint,
        "revision": revision,
        "base_revision": str(raw.get("base_revision") or revision or ""),
        "diff": normalized_diff,
    }
