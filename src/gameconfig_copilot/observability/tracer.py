"""Lightweight JSONL tracer: contextvar span tree + redaction + digest.

Design (ADR-006): no OpenTelemetry dependency; field semantics follow OTel so an
exporter can be swapped in later.

Input
    ``with tracer.span("llm.chat", attrs={"model": ...}) as span: ...``.
Output
    One JSON object per line in ``GCC_TRACE_PATH`` (default
    ``traces/traces.jsonl``), containing ``trace_id`` / ``span_id`` /
    ``parent_id`` / ``duration_ms`` / ``status`` / sanitized ``attrs``.
Failure semantics
    Tracing must never break the business call: write errors are logged unless
    ``strict=True``. Exceptions inside a span are recorded as
    ``status="error"`` and re-raised.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID

from .events import SpanEvent, SpanStatus, new_span_id, new_trace_id, now_iso

__all__ = [
    "DEFAULT_TRACE_PATH",
    "Span",
    "Tracer",
    "current_span",
    "current_trace_id",
    "get_tracer",
    "record_event",
    "redact_text",
    "reset_tracer",
    "sanitize_attrs",
    "sanitize_value",
    "span",
    "trace",
]

LOGGER = logging.getLogger(__name__)

DEFAULT_TRACE_PATH = "traces/traces.jsonl"
DEFAULT_MAX_ATTR_CHARS = 2048
_MAX_DEPTH = 8
_PREVIEW_CHARS = 160

_SENSITIVE_KEY_NAMES = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "access_token",
        "refresh_token",
        "password",
        "secret",
        "client_secret",
    }
)

_REDACTION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"sk-[A-Za-z0-9]{16,}"), "sk-***REDACTED***"),
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"), r"\1***REDACTED***"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "***@***.***"),
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "1**********"),
)

_current_span: ContextVar[Span | None] = ContextVar("gcc_current_span", default=None)
_current_trace_id: ContextVar[str | None] = ContextVar("gcc_current_trace_id", default=None)
_default_tracer_lock = threading.Lock()
_default_tracer: Tracer | None = None


def redact_text(value: str) -> str:
    """Mask API keys, bearer tokens, e-mail addresses and CN mobile numbers."""

    result = value
    for pattern, replacement in _REDACTION_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def _digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _serialized_size(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))
    except (TypeError, ValueError):  # pragma: no cover - defensive fallback
        return len(str(value))


def sanitize_value(value: Any, *, max_chars: int = DEFAULT_MAX_ATTR_CHARS, depth: int = 0) -> Any:
    """Return a JSON-safe, redacted copy of an attribute value.

    Strings/containers larger than ``max_chars`` are replaced by a digest record
    (``_digest`` + size metadata) so traces never carry whole documents.
    """

    if depth > _MAX_DEPTH:
        return "<max-depth>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Enum):
        return sanitize_value(value.value, max_chars=max_chars, depth=depth + 1)
    if isinstance(value, str):
        cleaned = redact_text(value)
        if len(cleaned) > max_chars:
            return {
                "_digest": _digest(cleaned),
                "_chars": len(cleaned),
                "_preview": cleaned[:_PREVIEW_CHARS],
            }
        return cleaned
    if isinstance(value, bytes):
        return {"_digest": "sha256:" + hashlib.sha256(value).hexdigest(), "_bytes": len(value)}
    if isinstance(value, Path):
        return redact_text(str(value))
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, (UUID, Decimal)):
        return str(value)
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text.lower() in _SENSITIVE_KEY_NAMES:
                sanitized[key_text] = "***REDACTED***"
            else:
                sanitized[key_text] = sanitize_value(
                    item, max_chars=max_chars, depth=depth + 1
                )
        size = _serialized_size(sanitized)
        if size > max_chars:
            return {
                "_digest": _digest(json.dumps(sanitized, ensure_ascii=False, sort_keys=True)),
                "_chars": size,
                "_type": "mapping",
                "_entries": len(sanitized),
                "_preview": json.dumps(sanitized, ensure_ascii=False, sort_keys=True)[
                    :_PREVIEW_CHARS
                ],
            }
        return sanitized
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [
            sanitize_value(item, max_chars=max_chars, depth=depth + 1)
            for item in value
        ]
        size = _serialized_size(items)
        if size > max_chars:
            return {
                "_digest": _digest(json.dumps(items, ensure_ascii=False, sort_keys=True)),
                "_chars": size,
                "_type": "sequence",
                "_entries": len(items),
                "_preview": json.dumps(items, ensure_ascii=False, sort_keys=True)[
                    :_PREVIEW_CHARS
                ],
            }
        return items
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return sanitize_value(model_dump(mode="json"), max_chars=max_chars, depth=depth + 1)
    return sanitize_value(str(value), max_chars=max_chars, depth=depth + 1)


def sanitize_attrs(
    attrs: Mapping[str, Any] | None,
    *,
    max_chars: int = DEFAULT_MAX_ATTR_CHARS,
) -> dict[str, Any]:
    """Sanitize a whole attribute dict, including sensitive key names."""

    if not attrs:
        return {}
    sanitized: dict[str, Any] = {}
    for key, value in attrs.items():
        key_text = str(key)
        if key_text.lower() in _SENSITIVE_KEY_NAMES:
            sanitized[key_text] = "***REDACTED***"
        else:
            sanitized[key_text] = sanitize_value(value, max_chars=max_chars)
    return sanitized


class Span:
    """A single timed span; inherits ``trace_id`` and parent from contextvars."""

    def __init__(
        self,
        tracer: Tracer,
        name: str,
        *,
        attrs: Mapping[str, Any] | None = None,
        trace_id: str | None = None,
        parent_id: str | None = None,
    ) -> None:
        if not name:
            raise ValueError("span name must not be empty")
        self._tracer = tracer
        self.name = name
        self.span_id = new_span_id()
        self.trace_id = trace_id
        self.parent_id = parent_id
        self.status: SpanStatus = "ok"
        self.attrs: dict[str, Any] = dict(attrs or {})
        self._start = time.perf_counter()
        self._span_token: Token[Span | None] | None = None
        self._trace_token: Token[str | None] | None = None
        self._activated = False
        self._ended = False

    def __enter__(self) -> Span:
        self._activate()
        return self

    def __exit__(self, exc_type: Any, exc: BaseException | None, tb: Any) -> bool:
        if exc is not None:
            self.record_exception(exc)
        self.end()
        return False

    def _activate(self) -> None:
        if self._ended:
            raise RuntimeError("cannot start an ended span")
        if self._activated:
            raise RuntimeError("span already started")
        self._activated = True
        parent = _current_span.get()
        if self.trace_id is None:
            self.trace_id = parent.trace_id if parent is not None else new_trace_id()
        if self.parent_id is None and parent is not None:
            self.parent_id = parent.span_id
        self._span_token = _current_span.set(self)
        self._trace_token = _current_trace_id.set(self.trace_id)

    def set_attr(self, key: str, value: Any) -> Span:
        self.attrs[key] = value
        return self

    def update_attrs(self, attrs: Mapping[str, Any]) -> Span:
        self.attrs.update(attrs)
        return self

    def set_status(self, status: SpanStatus) -> Span:
        self.status = status
        return self

    def record_exception(self, exc: BaseException) -> Span:
        self.status = "error"
        self.attrs.setdefault("error.type", type(exc).__name__)
        self.attrs.setdefault("error.message", str(exc))
        return self

    def end(self) -> None:
        """Close the span, restore contextvars and append one JSONL record."""

        if self._ended:
            return
        self._ended = True
        duration_ms = max(0, int(round((time.perf_counter() - self._start) * 1000)))
        if self._span_token is not None:
            _current_span.reset(self._span_token)
            self._span_token = None
        if self._trace_token is not None:
            _current_trace_id.reset(self._trace_token)
            self._trace_token = None
        self._tracer._write(
            SpanEvent(
                ts=now_iso(),
                trace_id=self.trace_id or new_trace_id(),
                span_id=self.span_id,
                parent_id=self.parent_id,
                name=self.name,
                kind="span",
                duration_ms=duration_ms,
                status=self.status,
                attrs=sanitize_attrs(self.attrs, max_chars=self._tracer.max_attr_chars),
            )
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Span(name={self.name!r}, span_id={self.span_id!r}, ended={self._ended})"


class Tracer:
    """Append-only JSONL writer for span events."""

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        max_attr_chars: int = DEFAULT_MAX_ATTR_CHARS,
        strict: bool = False,
        enabled: bool = True,
    ) -> None:
        configured = path or os.environ.get("GCC_TRACE_PATH") or DEFAULT_TRACE_PATH
        self.path = Path(configured).expanduser()
        self.max_attr_chars = max(256, int(max_attr_chars))
        self.strict = strict
        self.enabled = enabled
        self._lock = threading.Lock()

    def span(
        self,
        name: str,
        *,
        attrs: Mapping[str, Any] | None = None,
        trace_id: str | None = None,
        parent_id: str | None = None,
    ) -> Span:
        """Create a span to be used with ``with`` (ids resolve on enter)."""

        return Span(self, name, attrs=attrs, trace_id=trace_id, parent_id=parent_id)

    def start_span(
        self,
        name: str,
        *,
        attrs: Mapping[str, Any] | None = None,
        trace_id: str | None = None,
        parent_id: str | None = None,
    ) -> Span:
        """Start a span immediately (for try/finally style call sites)."""

        active = self.span(name, attrs=attrs, trace_id=trace_id, parent_id=parent_id)
        active._activate()
        return active

    def record_event(self, name: str, attrs: Mapping[str, Any] | None = None) -> str:
        """Append a point-in-time event under the current trace; return trace_id."""

        parent = _current_span.get()
        trace_id = (
            parent.trace_id if parent is not None else _current_trace_id.get()
        ) or new_trace_id()
        event = SpanEvent(
            ts=now_iso(),
            trace_id=trace_id,
            span_id=new_span_id(),
            parent_id=parent.span_id if parent is not None else None,
            name=name,
            kind="event",
            status="ok",
            attrs=sanitize_attrs(attrs, max_chars=self.max_attr_chars),
        )
        self._write(event)
        return trace_id

    def read_events(self) -> list[dict[str, Any]]:
        """Read back all JSONL records (used by tests and the trace panel)."""

        if not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    records.append(json.loads(stripped))
                except json.JSONDecodeError:  # pragma: no cover - corrupt tail
                    LOGGER.warning("Skipping malformed trace line in %s", self.path)
        return records

    def _write(self, event: SpanEvent) -> None:
        if not self.enabled:
            return
        line = event.to_json()
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(line)
                    handle.write("\n")
                    handle.flush()
        except OSError as exc:
            if self.strict:
                raise
            LOGGER.warning("Failed to write trace to %s: %s", self.path, exc)


def get_tracer() -> Tracer:
    """Return the process-wide tracer (path from ``GCC_TRACE_PATH``)."""

    global _default_tracer
    if _default_tracer is None:
        with _default_tracer_lock:
            if _default_tracer is None:
                _default_tracer = Tracer()
    return _default_tracer


def reset_tracer() -> None:
    """Drop the cached default tracer (tests / config reload)."""

    global _default_tracer
    with _default_tracer_lock:
        _default_tracer = None


def current_span() -> Span | None:
    return _current_span.get()


def current_trace_id() -> str | None:
    return _current_trace_id.get()


@contextmanager
def span(
    name: str,
    *,
    attrs: Mapping[str, Any] | None = None,
    tracer: Tracer | None = None,
) -> Iterator[Span]:
    """Context-manager convenience around the default tracer."""

    active_tracer = tracer or get_tracer()
    with active_tracer.span(name, attrs=attrs) as active:
        yield active


@contextmanager
def trace(trace_id: str | None = None) -> Iterator[str]:
    """Establish a trace id without a timed span (e.g. API request entry)."""

    value = trace_id or new_trace_id()
    token = _current_trace_id.set(value)
    try:
        yield value
    finally:
        _current_trace_id.reset(token)


def record_event(name: str, attrs: Mapping[str, Any] | None = None) -> str:
    """Record a point-in-time event with the default tracer."""

    return get_tracer().record_event(name, attrs)
