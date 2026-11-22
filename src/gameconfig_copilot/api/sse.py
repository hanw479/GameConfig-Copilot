"""SSE event contract for ``/api/v1/messages`` (docs/02 §10.2, frozen).

The ten event names and their key fields are a cross-layer contract owned by
Worker A (docs/06 §5.2); no event name may be added without an ADR. Because
§10.2 has no dedicated ``answer`` event, the read-only answer is carried by the
final ``status`` event (``stage="answered"`` plus ``answer``/``citations``
fields) — this adds fields to an existing event instead of inventing a name.

Order invariants enforced by :class:`SSEStream`:

* ``meta`` is first and appears exactly once;
* ``done`` is last and appears exactly once;
* nothing may be appended after ``done``;
* only the ten frozen event names are accepted.

Input
    Event payloads appended through the typed helpers.
Output
    Wire frames: ``event: <name>\\ndata: <json>\\n\\n``.
Failure semantics
    Order violations and unknown events raise :class:`SSEOrderError`; the API
    layer never sends a malformed frame.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

__all__ = [
    "SSE_ORDER",
    "STAGE_LABELS",
    "SSEEvent",
    "SSEEventType",
    "SSEOrderError",
    "SSEStream",
]

#: Events documented in docs/02 §10.2, in the order the happy paths use them.
SSE_ORDER: tuple[str, ...] = (
    "meta",
    "status",
    "evidence",
    "draft",
    "validation",
    "awaiting_confirmation",
    "committed",
    "warning",
    "error",
    "done",
)


class SSEEventType(str, Enum):
    META = "meta"
    STATUS = "status"
    EVIDENCE = "evidence"
    DRAFT = "draft"
    VALIDATION = "validation"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    COMMITTED = "committed"
    WARNING = "warning"
    ERROR = "error"
    DONE = "done"


STAGE_LABELS: dict[str, str] = {
    "routing": "正在判断意图",
    "researching": "正在检索知识库",
    "answered": "已回答",
    "gathering_context": "正在加载世界观/相似任务/约束",
    "drafting": "正在生成草案",
    "validating": "正在校验草案",
    "repairing": "正在修复草案",
    "previewed": "已生成预览",
    "awaiting_confirmation": "等待人工确认",
    "confirmed": "已确认，准备写入",
    "committed": "写入成功",
    "cancelled": "已取消",
    "clarify": "需要补充信息",
    "rejected": "已拒绝",
}


class SSEOrderError(ValueError):
    """The event sequence would violate docs/02 §10.2."""


@dataclass(frozen=True, slots=True)
class SSEEvent:
    """One SSE frame."""

    event: SSEEventType
    data: Mapping[str, Any]

    def encode(self) -> str:
        payload = json.dumps(dict(self.data), ensure_ascii=False, default=str)
        return f"event: {self.event.value}\ndata: {payload}\n\n"


class SSEStream:
    """Typed event writer with order validation."""

    def __init__(self) -> None:
        self._events: list[SSEEvent] = []
        self._done = False

    @property
    def events(self) -> tuple[SSEEvent, ...]:
        return tuple(self._events)

    @property
    def event_names(self) -> tuple[str, ...]:
        return tuple(event.event.value for event in self._events)

    def __iter__(self) -> Iterator[str]:
        for event in self._events:
            yield event.encode()

    def append(self, event: SSEEventType, data: Mapping[str, Any]) -> SSEEvent:
        if self._done:
            raise SSEOrderError("done 之后不允许再追加事件")
        if event is SSEEventType.META:
            if self._events:
                raise SSEOrderError("meta 必须是第一个事件，且只能出现一次")
        elif not self._events:
            raise SSEOrderError("第一个事件必须是 meta")
        elif event is SSEEventType.DONE and any(
            item.event is SSEEventType.DONE for item in self._events
        ):
            raise SSEOrderError("done 只能出现一次")
        frame = SSEEvent(event=event, data=dict(data))
        self._events.append(frame)
        if event is SSEEventType.DONE:
            self._done = True
        return frame

    # ------------------------------------------------------------- typed API
    def meta(self, trace_id: str, session_id: str) -> SSEEvent:
        return self.append(
            SSEEventType.META,
            {"trace_id": trace_id, "session_id": session_id},
        )

    def status(self, stage: str, label: str, **extra: Any) -> SSEEvent:
        data: dict[str, Any] = {"stage": stage, "label": label}
        data.update(extra)
        return self.append(SSEEventType.STATUS, data)

    def evidence(self, items: list[Mapping[str, Any]]) -> SSEEvent:
        return self.append(SSEEventType.EVIDENCE, {"items": list(items)})

    def draft(self, draft: Mapping[str, Any], version: int) -> SSEEvent:
        return self.append(SSEEventType.DRAFT, {"draft": dict(draft), "version": version})

    def validation(
        self,
        ok: bool,
        errors: list[Mapping[str, Any]] | None = None,
        warnings: list[Mapping[str, Any]] | None = None,
        **extra: Any,
    ) -> SSEEvent:
        data: dict[str, Any] = {
            "ok": bool(ok),
            "errors": list(errors or []),
            "warnings": list(warnings or []),
        }
        data.update(extra)
        return self.append(SSEEventType.VALIDATION, data)

    def awaiting_confirmation(
        self,
        *,
        commit_token: str,
        diff: Mapping[str, Any],
        target_path: str,
        expires_in: int,
        draft_id: str = "",
    ) -> SSEEvent:
        return self.append(
            SSEEventType.AWAITING_CONFIRMATION,
            {
                "draft_id": draft_id,
                "commit_token": commit_token,
                "diff": dict(diff),
                "target_path": target_path,
                "expires_in": int(expires_in),
            },
        )

    def committed(
        self,
        *,
        commit_id: str,
        backup: str | None,
        revision: str | int,
    ) -> SSEEvent:
        return self.append(
            SSEEventType.COMMITTED,
            {"commit_id": commit_id, "backup": backup, "revision": revision},
        )

    def warning(self, code: str, message: str) -> SSEEvent:
        return self.append(SSEEventType.WARNING, {"code": code, "message": message})

    def error(self, code: str, message: str, stage: str = "") -> SSEEvent:
        return self.append(
            SSEEventType.ERROR,
            {"code": code, "message": message, "stage": stage},
        )

    def done(
        self,
        *,
        status: str,
        elapsed_ms: int,
        tokens: int = 0,
        cost_cny: float = 0.0,
    ) -> SSEEvent:
        return self.append(
            SSEEventType.DONE,
            {
                "status": status,
                "elapsed_ms": max(0, int(elapsed_ms)),
                "tokens": max(0, int(tokens)),
                "cost_cny": round(float(cost_cny), 6),
            },
        )
