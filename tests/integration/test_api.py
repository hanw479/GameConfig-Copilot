"""API integration tests (S2-04/05): SSE order, sessions, timeout, error path."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from gameconfig_copilot.api.main import create_app
from support.fakes import (
    FakeLLM,
    FakeRetriever,
    default_hits,
    valid_task_payload,
)

pytestmark = pytest.mark.integration


def _parse_sse(payload: str) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    for block in payload.strip().split("\n\n"):
        if not block.strip():
            continue
        lines = block.splitlines()
        name = next(
            line.removeprefix("event: ")
            for line in lines
            if line.startswith("event: ")
        )
        data = json.loads(
            next(
                line.removeprefix("data: ")
                for line in lines
                if line.startswith("data: ")
            )
        )
        events.append((name, data))
    return events


def _stream(client: TestClient, session_id: str, message: str) -> list[tuple[str, dict[str, Any]]]:
    with client.stream(
        "POST",
        "/api/v1/messages",
        json={"session_id": session_id, "message": message},
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join(response.iter_text())
    return _parse_sse(body)


@pytest.fixture
def client(tmp_path) -> TestClient:
    llm = FakeLLM(structured_payload=valid_task_payload())
    retriever = FakeRetriever(default_hits())
    app = create_app(
        llm=llm,
        retriever=retriever,
        tasks_dir=tmp_path / "tasks",
        session_ttl_s=60,
        mcp_mode="readonly",
    )
    return TestClient(app)


def _new_session(client: TestClient) -> str:
    response = client.post("/api/v1/sessions", json={"designer_id": "designer_01"})
    assert response.status_code == 201
    return response.json()["session_id"]


def test_healthz_is_available_without_api_key(client: TestClient) -> None:
    response = client.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["mcp"] == {"connected": False, "mode": "readonly"}


def test_create_get_and_resume_session(client: TestClient) -> None:
    session_id = _new_session(client)

    snapshot = client.get(f"/api/v1/sessions/{session_id}")
    assert snapshot.status_code == 200
    assert snapshot.json()["message_count"] == 0
    assert snapshot.json()["expires_in"] > 0

    resumed = client.post(f"/api/v1/sessions/{session_id}/resume")
    assert resumed.status_code == 200
    assert resumed.json()["resumed"] is True
    assert resumed.json()["session_id"] == session_id

    assert client.get("/api/v1/sessions/sess_missing").status_code == 404
    assert client.post("/api/v1/sessions/sess_missing/resume").status_code == 404


def test_session_times_out_and_cannot_be_resumed() -> None:
    now = [datetime.now(timezone.utc)]
    app = create_app(
        llm=FakeLLM(),
        retriever=FakeRetriever(default_hits()),
        session_ttl_s=10,
        clock=lambda: now[0],
    )
    client = TestClient(app)
    session_id = _new_session(client)
    now[0] += timedelta(seconds=11)

    assert client.get(f"/api/v1/sessions/{session_id}").status_code == 410
    assert client.post(f"/api/v1/sessions/{session_id}/resume").status_code == 410


def test_query_sse_event_order_and_citations(client: TestClient) -> None:
    session_id = _new_session(client)
    events = _stream(client, session_id, "铁匠老陈对星港议会是什么立场？")

    names = [name for name, _ in events]
    assert names[0] == "meta"
    assert names[-1] == "done"
    assert "evidence" in names
    assert names.count("done") == 1

    stages = [data["stage"] for name, data in events if name == "status"]
    assert stages[:3] == ["routing", "researching", "answered"]
    answered = next(
        data for name, data in events if name == "status" and data["stage"] == "answered"
    )
    assert answered["answer"]
    assert answered["citations"]
    assert "[" in answered["answer"] and "]" in answered["answer"]

    done = events[-1][1]
    assert done["status"] == "answered"
    assert done["elapsed_ms"] >= 0
    assert done["tokens"] > 0

    snapshot = client.get(f"/api/v1/sessions/{session_id}").json()
    assert snapshot["message_count"] == 2
    assert snapshot["last_stage"] == "answered"


def test_task_sse_order_draft_validation_awaiting_confirmation(client: TestClient) -> None:
    session_id = _new_session(client)
    events = _stream(client, session_id, "给铁匠老陈加一条 6 级支线，去矿洞捡 4 块矿石")

    names = [name for name, _ in events]
    assert names[0] == "meta"
    assert names[-1] == "done"
    for name in ("draft", "validation", "awaiting_confirmation"):
        assert name in names
    assert names.index("draft") < names.index("validation")
    assert names.index("validation") < names.index("awaiting_confirmation")

    draft_event = next(data for name, data in events if name == "draft")
    assert draft_event["version"] == 1
    assert draft_event["draft"]["task_id"].startswith("T_SIDE_")
    assert draft_event["draft"]["provenance"]["evidence"]

    validation = next(data for name, data in events if name == "validation")
    assert validation["ok"] is True
    assert validation["errors"] == []

    awaiting = next(data for name, data in events if name == "awaiting_confirmation")
    assert awaiting["commit_token"].startswith("ct_")
    assert awaiting["target_path"].endswith(".json")
    assert awaiting["expires_in"] == 600
    assert awaiting["diff"]["created"] is True

    assert events[-1][1]["status"] == "awaiting_confirmation"


def test_stream_failure_emits_error_then_done() -> None:
    class ExplodingRetriever(FakeRetriever):
        def search(self, query, filters=None, top_k=5, **kwargs):  # type: ignore[override]
            raise RuntimeError("index unavailable")

    app = create_app(
        llm=FakeLLM(),
        retriever=ExplodingRetriever([]),
        session_ttl_s=60,
    )
    client = TestClient(app)
    session_id = _new_session(client)
    events = _stream(client, session_id, "铁匠老陈对星港议会是什么立场？")

    names = [name for name, _ in events]
    assert "error" in names
    assert names[-1] == "done"
    assert names.index("error") < names.index("done")
    error = next(data for name, data in events if name == "error")
    assert error["code"] == "E_INTERNAL"
    assert events[-1][1]["status"] == "error"


def test_trace_endpoint_returns_route_and_retrieval_spans(client: TestClient) -> None:
    session_id = _new_session(client)
    events = _stream(client, session_id, "铁匠老陈对星港议会是什么立场？")
    trace_id = events[0][1]["trace_id"]

    response = client.get(f"/api/v1/traces/{trace_id}")
    assert response.status_code == 200
    body = response.json()
    span_names = {record["name"] for record in body["events"]}
    assert {"route", "retrieval", "api.messages"} <= span_names
    assert all(record["trace_id"] == trace_id for record in body["events"])

    assert client.get("/api/v1/traces/tr_missing").status_code == 404
