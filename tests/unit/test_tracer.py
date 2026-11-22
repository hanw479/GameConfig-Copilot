"""Tracer tests (S0-05): nested span tree, redaction, digest, JSONL append."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import pytest

from gameconfig_copilot.observability.tracer import (
    Tracer,
    current_trace_id,
    get_tracer,
    span,
    trace,
)


def test_nested_spans_form_one_trace_tree(tracer: Tracer) -> None:
    with tracer.span("request", attrs={"session_id": "sess_1"}) as root:
        assert root.trace_id is not None and root.trace_id.startswith("tr_")
        with (
            tracer.span("retrieval.hybrid", attrs={"top_k": 5}),
            tracer.span("llm.chat", attrs={"model": "deepseek-chat"}) as leaf,
        ):
            assert leaf.trace_id == root.trace_id

    assert current_trace_id() is None

    events = {event["name"]: event for event in tracer.read_events()}
    assert set(events) == {"request", "retrieval.hybrid", "llm.chat"}
    assert events["request"]["parent_id"] is None
    assert events["retrieval.hybrid"]["parent_id"] == events["request"]["span_id"]
    assert events["llm.chat"]["parent_id"] == events["retrieval.hybrid"]["span_id"]
    assert len({event["trace_id"] for event in events.values()}) == 1
    for event in events.values():
        assert event["kind"] == "span"
        assert event["status"] == "ok"
        assert event["duration_ms"] >= 0
        assert event["span_id"].startswith("sp_")
        datetime.fromisoformat(event["ts"])


def test_exception_marks_span_error_and_propagates(tracer: Tracer) -> None:
    with pytest.raises(RuntimeError, match="boom"), tracer.span("dangerous.call"):
        raise RuntimeError("boom")

    event = tracer.read_events()[0]
    assert event["status"] == "error"
    assert event["attrs"]["error.type"] == "RuntimeError"
    assert "boom" in event["attrs"]["error.message"]


def test_secrets_emails_and_phones_are_redacted(tracer: Tracer) -> None:
    secret = "sk-" + "a1B2c3D4" * 4
    email = "designer.zhang@example.com"
    phone = "13812345678"
    with tracer.span(
        "redaction.check",
        attrs={
            "prompt": f"key={secret} mail={email} tel={phone}",
            "nested": {"Authorization": secret, "list": [secret]},
            "api_key": secret,
        },
    ):
        pass

    line = tracer.path.read_text(encoding="utf-8")
    assert secret not in line
    assert email not in line
    assert phone not in line
    assert "REDACTED" in line

    attrs = tracer.read_events()[0]["attrs"]
    assert attrs["api_key"] == "***REDACTED***"
    assert attrs["nested"]["Authorization"] == "***REDACTED***"
    assert attrs["nested"]["list"] == ["sk-***REDACTED***"]


def test_large_objects_are_stored_as_digests(tracer: Tracer) -> None:
    huge_string = "x" * 5000
    huge_mapping = {f"key_{index}": "y" * 60 for index in range(60)}
    with tracer.span("big.payload", attrs={"blob": huge_string, "mapping": huge_mapping}):
        pass

    line = tracer.path.read_text(encoding="utf-8")
    assert huge_string not in line
    events = tracer.read_events()
    blob = events[0]["attrs"]["blob"]
    assert blob["_digest"].startswith("sha256:")
    assert blob["_chars"] > 4000
    assert events[0]["attrs"]["mapping"]["_type"] == "mapping"
    assert events[0]["attrs"]["mapping"]["_digest"].startswith("sha256:")


def test_record_event_uses_active_trace(tracer: Tracer) -> None:
    with trace("tr_fixed") as trace_id:
        assert trace_id == "tr_fixed"
        tracer.record_event("stage.changed", {"stage": "drafting"})

    event = tracer.read_events()[0]
    assert event["kind"] == "event"
    assert event["trace_id"] == "tr_fixed"
    assert event["parent_id"] is None
    assert event["attrs"] == {"stage": "drafting"}


def test_record_event_inside_span_inherits_parent(tracer: Tracer) -> None:
    with tracer.span("request") as root:
        tracer.record_event("note", {"ok": True})
        root_id = root.span_id
        trace_id = root.trace_id

    note = tracer.read_events()[0]
    assert note["kind"] == "event"
    assert note["trace_id"] == trace_id
    assert note["parent_id"] == root_id


def test_jsonl_is_append_only(tracer: Tracer) -> None:
    with tracer.span("first"):
        pass
    with tracer.span("second"):
        pass

    lines = [line for line in tracer.path.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 2
    assert [json.loads(line)["name"] for line in lines] == ["first", "second"]


def test_disabled_tracer_writes_nothing(tmp_path: Path) -> None:
    disabled = Tracer(tmp_path / "off.jsonl", enabled=False)
    with disabled.span("ignored"):
        pass
    assert not disabled.path.exists()


def test_default_tracer_uses_environment_path() -> None:
    tracer = get_tracer()
    assert tracer.path == Path(os.environ["GCC_TRACE_PATH"])


def test_module_level_span_uses_default_tracer() -> None:
    with span("module.level") as active:
        assert active.trace_id is not None
    events = get_tracer().read_events()
    assert events[-1]["name"] == "module.level"
