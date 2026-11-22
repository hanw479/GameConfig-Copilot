"""Observability event model (one JSONL line per span/event).

Contract aligned with ``docs/02-architecture.md`` §9.1. Field names are frozen:
``ts`` / ``trace_id`` / ``span_id`` / ``parent_id`` / ``name`` / ``kind`` /
``duration_ms`` / ``status`` / ``attrs``. ``attrs`` is an open dict so later
phases (S7) can add attributes without changing the event structure.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "SpanEvent",
    "SpanStatus",
    "new_span_id",
    "new_trace_id",
    "now_iso",
]

SpanStatus = Literal["ok", "error", "unset"]


def _new_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex}"


def new_trace_id() -> str:
    """Generate a trace id (``tr_`` + 32 hex chars)."""

    return _new_id("tr_")


def new_span_id() -> str:
    """Generate a span id (``sp_`` + 32 hex chars)."""

    return _new_id("sp_")


def now_iso() -> str:
    """Local timezone ISO-8601 timestamp with millisecond precision."""

    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


class SpanEvent(BaseModel):
    """One JSONL record: a completed span or a point-in-time event."""

    model_config = ConfigDict(extra="forbid")

    ts: str
    trace_id: str
    span_id: str
    parent_id: str | None = None
    name: str
    kind: Literal["span", "event"] = "span"
    duration_ms: int = Field(default=0, ge=0)
    status: SpanStatus = "ok"
    attrs: dict[str, Any] = Field(default_factory=dict)

    def to_json(self) -> str:
        """Serialize for a single JSONL line (no trailing newline)."""

        return json.dumps(self.model_dump(mode="json"), ensure_ascii=False)
