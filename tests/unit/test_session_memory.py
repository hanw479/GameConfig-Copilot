"""SQLite session checkpointer tests (S6-01 / S6-05).

Covers restart recovery, message ordering, draft versions, stage changes,
7-day TTL/expiry, cascade cleanup and concurrent appends. Uses ``tmp_path``
databases only; the real ``data/memory`` is never touched.
"""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from gameconfig_copilot.memory.session import (
    DEFAULT_SESSION_TTL_S,
    MemoryMessage,
    SessionExpiredError,
    SessionNotFoundError,
    SessionState,
    SessionStore,
    SessionValueError,
)


class FakeClock:
    def __init__(self) -> None:
        self.moment = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, **kwargs: float) -> None:
        self.moment += timedelta(**kwargs)


def _state(session_id: str = "sess_1", **kwargs: Any) -> SessionState:
    return SessionState(session_id=session_id, **kwargs)


def test_default_ttl_is_seven_days() -> None:
    assert DEFAULT_SESSION_TTL_S == 7 * 24 * 60 * 60


def test_roundtrip_survives_store_restart(tmp_path: Any) -> None:
    database = tmp_path / "sessions.sqlite3"
    first = SessionStore(database)
    created = datetime(2026, 11, 15, 9, 30, tzinfo=timezone.utc)
    first.put(
        _state(
            designer_id="u_1024",
            stage="drafting",
            draft={"task_id": "T_SIDE_0213", "task_type": "side"},
            messages=[
                MemoryMessage("user", "给铁匠加一条支线", created_at=created),
                MemoryMessage("assistant", "草案已生成", created_at=created),
            ],
        )
    )
    first.append_message("sess_1", "user", "奖励改成 150 金币")
    first.set_stage("sess_1", "awaiting_confirmation")

    restarted = SessionStore(database)
    state = restarted.get("sess_1")

    assert state.designer_id == "u_1024"
    assert state.stage == "awaiting_confirmation"
    assert [(message.role, message.content) for message in state.messages] == [
        ("user", "给铁匠加一条支线"),
        ("assistant", "草案已生成"),
        ("user", "奖励改成 150 金币"),
    ]
    assert [message.seq for message in state.messages] == [1, 2, 3]
    assert state.messages[0].created_at == created
    assert state.draft == {"task_id": "T_SIDE_0213", "task_type": "side"}
    assert state.draft_version == 1
    assert restarted.exists("sess_1") is True
    assert restarted.get_or_none("missing") is None


def test_draft_versions_are_append_only_and_auto_increment(tmp_path: Any) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite3")
    store.put(_state(draft={"title": "v1"}))
    second = store.put(_state(draft={"title": "v2"}))
    third = store.put(_state(draft={"title": "v3"}, draft_version=7))

    assert second.draft_version == 2
    assert third.draft_version == 7
    history = store.draft_history("sess_1")
    assert [entry["version"] for entry in history] == [7, 2, 1]
    assert history[0]["draft"] == {"title": "v3"}
    assert store.get("sess_1").draft == {"title": "v3"}


def test_put_replaces_message_snapshot_without_duplicates(tmp_path: Any) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite3")
    store.put(_state(messages=[MemoryMessage("user", "a"), MemoryMessage("assistant", "b")]))
    store.put(_state(messages=[MemoryMessage("user", "a")]))

    state = store.get("sess_1")
    assert [(message.role, message.content) for message in state.messages] == [
        ("user", "a")
    ]
    assert state.messages[0].seq == 1


def test_ttl_expiry_and_cascade_cleanup(tmp_path: Any) -> None:
    database = tmp_path / "sessions.sqlite3"
    clock = FakeClock()
    store = SessionStore(database, clock=clock)

    store.put(
        _state(
            draft={"task_id": "T_SIDE_0213"},
            messages=[MemoryMessage("user", "hi")],
        )
    )
    clock.advance(seconds=DEFAULT_SESSION_TTL_S - 1)
    assert store.get("sess_1").messages[0].content == "hi"

    clock.advance(seconds=2)
    with pytest.raises(SessionExpiredError):
        store.get("sess_1")
    with pytest.raises(SessionExpiredError):
        store.append_message("sess_1", "user", "late")

    assert store.expire() == 1
    with pytest.raises(SessionNotFoundError):
        store.get("sess_1")
    assert store.expire() == 0

    with sqlite3.connect(database) as connection:
        for table in ("sessions", "messages", "drafts"):
            count = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert count == 0

    # Reusing an expired id starts a fresh session instead of reviving history.
    fresh = store.put(_state(messages=[MemoryMessage("user", "new")]))
    assert fresh.messages[0].seq == 1
    assert fresh.draft is None
    assert store.get("sess_1").created_at == clock.moment


def test_unknown_ids_raise_structured_errors(tmp_path: Any) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite3")
    with pytest.raises(SessionNotFoundError):
        store.get("missing")
    with pytest.raises(SessionNotFoundError):
        store.append_message("missing", "user", "x")
    with pytest.raises(SessionNotFoundError):
        store.set_stage("missing", "drafting")


def test_invalid_arguments_are_rejected(tmp_path: Any) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite3")
    with pytest.raises(SessionValueError):
        store.put(_state(session_id=""))
    with pytest.raises(SessionValueError):
        SessionStore(tmp_path / "bad.sqlite3", ttl_s=0)
    store.put(_state())
    with pytest.raises(SessionValueError):
        store.append_message("sess_1", "", "x")
    with pytest.raises(SessionValueError):
        store.append_message("sess_1", "user", 1)
    with pytest.raises(SessionValueError):
        store.set_stage("sess_1", None)


def test_concurrent_appends_allocate_unique_ordered_seq(tmp_path: Any) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite3")
    store.put(_state())

    contents = [f"message-{index}" for index in range(40)]

    def append(content: str) -> None:
        store.append_message("sess_1", "user", content)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(append, contents))

    state = store.get("sess_1")
    assert len(state.messages) == 40
    assert [message.seq for message in state.messages] == list(range(1, 41))
    assert sorted(message.content for message in state.messages) == sorted(contents)


def test_snapshot_mapping_roundtrip_and_dict_shape(tmp_path: Any) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite3")
    store.put(
        {
            "session_id": "sess_map",
            "designer_id": "u_1",
            "stage": "routing",
            "draft_version": 2,
            "draft": {"task_id": "T_MAIN_0102"},
            "messages": [{"role": "user", "content": "hello", "seq": 1}],
        }
    )
    payload = store.get("sess_map").to_dict()
    assert payload["session_id"] == "sess_map"
    assert payload["draft_version"] == 2
    assert payload["messages"][0]["content"] == "hello"
    assert payload["created_at"] is not None
