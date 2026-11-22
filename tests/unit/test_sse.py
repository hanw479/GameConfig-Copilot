"""SSE contract tests (S2-04): frozen event names, order guards, wire format."""

from __future__ import annotations

import json

import pytest

from gameconfig_copilot.api.sse import (
    SSEEventType,
    SSEOrderError,
    SSEStream,
)


def _parse(frames: list[str]) -> list[tuple[str, dict]]:
    parsed: list[tuple[str, dict]] = []
    for frame in frames:
        lines = frame.strip().splitlines()
        name = next(line.removeprefix("event: ") for line in lines if line.startswith("event: "))
        data = json.loads(
            next(line.removeprefix("data: ") for line in lines if line.startswith("data: "))
        )
        parsed.append((name, data))
    return parsed


def test_happy_path_sequence_encodes_ten_frozen_event_names_in_order() -> None:
    stream = SSEStream()
    stream.meta("tr_1", "sess_1")
    stream.status("routing", "正在判断意图")
    stream.evidence([{"source": "wiki:a#b", "score": 0.1, "snippet": "x"}])
    stream.draft({"task_id": "T_SIDE_0214"}, 1)
    stream.validation(True, [], [])
    stream.awaiting_confirmation(
        commit_token="ct_x",
        diff={"created": True},
        target_path="data/tasks/T_SIDE_0214.json",
        expires_in=600,
    )
    stream.warning("CONTEXT_TRUNCATED", "截断")
    stream.done(status="awaiting_confirmation", elapsed_ms=12, tokens=100, cost_cny=0.001)

    names = stream.event_names
    assert names[0] == "meta"
    assert names[-1] == "done"
    assert set(names) <= {event.value for event in SSEEventType}
    parsed = _parse(list(stream))
    assert [name for name, _ in parsed] == list(names)
    assert parsed[0][1] == {"trace_id": "tr_1", "session_id": "sess_1"}
    assert parsed[-1][1]["status"] == "awaiting_confirmation"


def test_meta_must_be_first() -> None:
    stream = SSEStream()

    with pytest.raises(SSEOrderError):
        stream.status("routing", "x")


def test_meta_cannot_repeat() -> None:
    stream = SSEStream()
    stream.meta("tr_1", "sess_1")

    with pytest.raises(SSEOrderError):
        stream.meta("tr_2", "sess_2")


def test_no_events_after_done() -> None:
    stream = SSEStream()
    stream.meta("tr_1", "sess_1")
    stream.done(status="answered", elapsed_ms=1)

    with pytest.raises(SSEOrderError):
        stream.status("answered", "late")


def test_done_only_once() -> None:
    stream = SSEStream()
    stream.meta("tr_1", "sess_1")
    stream.done(status="answered", elapsed_ms=1)

    with pytest.raises(SSEOrderError):
        stream.done(status="answered", elapsed_ms=2)


def test_error_terminates_with_error_then_done() -> None:
    stream = SSEStream()
    stream.meta("tr_1", "sess_1")
    stream.error("E_INTERNAL", "boom", "research")
    stream.done(status="error", elapsed_ms=3)

    assert stream.event_names == ("meta", "error", "done")


def test_validation_payload_shape() -> None:
    stream = SSEStream()
    stream.meta("tr_1", "sess_1")
    frame = stream.validation(
        False,
        [{"code": "E_REWARD_RANGE", "field": "rewards[0].amount"}],
        [],
        checked={"schema": 1},
    )
    payload = _parse([frame.encode()])[0][1]

    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "E_REWARD_RANGE"
    assert payload["checked"] == {"schema": 1}
    assert frame.encode().endswith("\n\n")
