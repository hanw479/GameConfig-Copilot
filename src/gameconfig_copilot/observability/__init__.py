"""Observability package: JSONL tracer + event model (owner: Foundation F)."""

from __future__ import annotations

from .events import SpanEvent, SpanStatus, new_span_id, new_trace_id, now_iso
from .tracer import (
    DEFAULT_TRACE_PATH,
    Span,
    Tracer,
    current_span,
    current_trace_id,
    get_tracer,
    record_event,
    redact_text,
    reset_tracer,
    sanitize_attrs,
    sanitize_value,
    span,
    trace,
)

__all__ = [
    "DEFAULT_TRACE_PATH",
    "Span",
    "SpanEvent",
    "SpanStatus",
    "Tracer",
    "current_span",
    "current_trace_id",
    "get_tracer",
    "new_span_id",
    "new_trace_id",
    "now_iso",
    "record_event",
    "redact_text",
    "reset_tracer",
    "sanitize_attrs",
    "sanitize_value",
    "span",
    "trace",
]
