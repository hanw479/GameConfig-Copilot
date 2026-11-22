"""Two-phase confirm/reject API flow (S5-07): token scope, expiry, commit SSE.

Every test runs on a ``tmp_path`` copy of the frozen task corpus; the MCP
``Writer`` is rooted there (tasks/backups/locks/audit all inside ``tmp_path``),
so ``data/`` in the repository is never written.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from gameconfig_copilot.api.main import create_app
from gameconfig_copilot.mcp_server.tools import ToolService
from gameconfig_copilot.mcp_server.writer import Writer
from support.fakes import FakeLLM, FakeRetriever, default_hits, valid_task_payload

pytestmark = pytest.mark.integration

REAL_TASKS = Path(__file__).resolve().parents[2] / "data" / "tasks"
TASK_MESSAGE = "给铁匠老陈加一条 6 级支线，去矿洞捡 4 块矿石"


def _parse_sse(payload: str) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    for block in payload.strip().split("\n\n"):
        if not block.strip():
            continue
        lines = block.splitlines()
        name = next(
            line.removeprefix("event: ") for line in lines if line.startswith("event: ")
        )
        data = json.loads(
            next(
                line.removeprefix("data: ") for line in lines if line.startswith("data: ")
            )
        )
        events.append((name, data))
    return events


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    tasks = root / "data" / "tasks"
    tasks.mkdir(parents=True)
    sources = sorted(REAL_TASKS.glob("*.json"))
    assert sources, "real data/tasks must contain the frozen corpus"
    for source in sources:
        shutil.copy2(source, tasks / source.name)
    return root


def _client(
    root: Path,
    *,
    llm: FakeLLM | None = None,
    commit_ttl_s: float = 600.0,
    clock: Any | None = None,
    mcp_mode: str = "write",
    tools: ToolService | None = None,
) -> tuple[TestClient, Writer]:
    writer = Writer(root)
    service = tools or ToolService(writer)
    app = create_app(
        llm=llm or FakeLLM(structured_payload=valid_task_payload()),
        retriever=FakeRetriever(default_hits()),
        tools=service,
        mcp_mode=mcp_mode,
        tasks_dir=root / "data" / "tasks",
        session_ttl_s=3600,
        commit_ttl_s=commit_ttl_s,
        clock=clock,
    )
    return TestClient(app), writer


def _new_session(client: TestClient) -> str:
    response = client.post("/api/v1/sessions", json={"designer_id": "designer_01"})
    assert response.status_code == 201
    return response.json()["session_id"]


def _stream(client: TestClient, session_id: str, message: str) -> list[tuple[str, dict]]:
    with client.stream(
        "POST",
        "/api/v1/messages",
        json={"session_id": session_id, "message": message},
    ) as response:
        assert response.status_code == 200
        body = "".join(response.iter_text())
    return _parse_sse(body)


def _post_sse(client: TestClient, path: str, body: dict[str, Any]) -> list[tuple[str, dict]]:
    with client.stream("POST", path, json=body) as response:
        assert response.status_code == 200, response.read()
        payload = "".join(response.iter_text())
    return _parse_sse(payload)


def _awaiting(events: list[tuple[str, dict]]) -> dict[str, Any]:
    return next(data for name, data in events if name == "awaiting_confirmation")


def _open_draft(
    client: TestClient,
    session_id: str,
    message: str = TASK_MESSAGE,
) -> tuple[list[tuple[str, dict]], dict[str, Any]]:
    events = _stream(client, session_id, message)
    awaiting = _awaiting(events)
    return events, awaiting


def test_confirm_commits_after_preview_and_emits_committed(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    client, _writer = _client(root)
    session_id = _new_session(client)
    external_before = (REAL_TASKS / "T_SIDE_0214.json").read_bytes()

    events, awaiting = _open_draft(client, session_id)
    names = [name for name, _ in events]
    assert names[0] == "meta" and names[-1] == "done"
    assert names.index("validation") < names.index("awaiting_confirmation")
    assert events[-1][1]["status"] == "awaiting_confirmation"
    assert awaiting["commit_token"].startswith("ct_")
    assert awaiting["draft_id"].startswith("dr_")
    assert awaiting["target_path"] == "data/tasks/T_SIDE_0214.json"
    assert awaiting["expires_in"] == 600

    target = root / awaiting["target_path"]
    before = target.read_bytes()
    assert before == external_before  # preview performed zero writes

    confirm = _post_sse(
        client,
        f"/api/v1/drafts/{awaiting['draft_id']}/confirm",
        {"commit_token": awaiting["commit_token"], "note": "demo ok"},
    )
    confirm_names = [name for name, _ in confirm]
    assert confirm_names[0] == "meta"
    assert confirm_names[-1] == "done"
    assert "committed" in confirm_names
    assert confirm_names.index("committed") > confirm_names.index("status")
    committed = next(data for name, data in confirm if name == "committed")
    assert len(committed["commit_id"]) == 12
    assert committed["backup"].startswith("data/backups/T_SIDE_0214/")
    assert committed["revision"] and committed["revision"] != ""
    assert confirm[-1][1]["status"] == "committed"

    combined = [name for name, _ in events] + confirm_names
    assert combined.index("awaiting_confirmation") < combined.index("committed")

    after = json.loads(target.read_text(encoding="utf-8"))
    assert after["task_id"] == "T_SIDE_0214"
    assert after["rewards"][0]["amount"] == 120
    assert after["provenance"]["model"] == "fake-model"
    assert (root / committed["backup"]).is_file()
    assert (root / "traces" / "commits.jsonl").is_file()
    assert (REAL_TASKS / "T_SIDE_0214.json").read_bytes() == external_before

    snapshot = client.get(f"/api/v1/sessions/{session_id}").json()
    assert snapshot["last_stage"] == "committed"

    # One-shot token: a second confirm is a 409, not a second write.
    again = client.post(
        f"/api/v1/drafts/{awaiting['draft_id']}/confirm",
        json={"commit_token": awaiting["commit_token"]},
    )
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "E_COMMIT_TOKEN_CONSUMED"


def test_confirm_rejects_expired_token(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    now = [datetime.now(timezone.utc)]
    client, _writer = _client(root, commit_ttl_s=600, clock=lambda: now[0])
    session_id = _new_session(client)
    _events, awaiting = _open_draft(client, session_id)
    target = root / awaiting["target_path"]
    before = target.read_bytes()

    now[0] += timedelta(seconds=601)

    response = client.post(
        f"/api/v1/drafts/{awaiting['draft_id']}/confirm",
        json={"commit_token": awaiting["commit_token"], "note": "too late"},
    )
    assert response.status_code == 410
    assert response.json()["detail"]["code"] == "E_COMMIT_TOKEN_EXPIRED"
    assert target.read_bytes() == before
    assert not (root / "traces" / "commits.jsonl").exists()


def test_confirm_rejects_token_from_another_draft(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    llm = FakeLLM(
        structured_sequence=[
            valid_task_payload(gold=120),
            valid_task_payload(gold=150),
        ]
    )
    client, _writer = _client(root, llm=llm)
    session_id = _new_session(client)

    _, first = _open_draft(client, session_id, TASK_MESSAGE)
    _, second = _open_draft(client, session_id, "再给铁匠老陈加一条 6 级支线")
    assert first["draft_id"] != second["draft_id"]

    response = client.post(
        f"/api/v1/drafts/{second['draft_id']}/confirm",
        json={"commit_token": first["commit_token"]},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "E_COMMIT_TOKEN_SCOPE"
    assert not (root / "traces" / "commits.jsonl").exists()


def test_confirm_rejects_mutated_draft_fingerprint(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    client, _writer = _client(root)
    session_id = _new_session(client)
    _events, awaiting = _open_draft(client, session_id)
    target = root / awaiting["target_path"]
    before = target.read_bytes()

    runtime = client.app.state.runtime
    found = runtime.sessions.find_pending(awaiting["commit_token"])
    assert found is not None
    _session, pending = found
    pending.draft["rewards"][0]["amount"] = 9999

    response = client.post(
        f"/api/v1/drafts/{awaiting['draft_id']}/confirm",
        json={"commit_token": awaiting["commit_token"]},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "E_FINGERPRINT_MISMATCH"
    assert target.read_bytes() == before
    assert not (root / "traces" / "commits.jsonl").exists()


def test_unknown_token_and_wrong_draft_are_rejected(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    client, _writer = _client(root)
    session_id = _new_session(client)
    _events, awaiting = _open_draft(client, session_id)

    unknown = client.post(
        f"/api/v1/drafts/{awaiting['draft_id']}/confirm",
        json={"commit_token": "ct_does_not_exist"},
    )
    assert unknown.status_code == 404
    assert unknown.json()["detail"]["code"] == "E_COMMIT_TOKEN_UNKNOWN"

    wrong_draft = client.post(
        "/api/v1/drafts/dr_not_this_one/confirm",
        json={"commit_token": awaiting["commit_token"]},
    )
    assert wrong_draft.status_code == 409
    assert wrong_draft.json()["detail"]["code"] == "E_COMMIT_TOKEN_SCOPE"


def test_reject_records_reason_and_blocks_later_confirm(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    client, _writer = _client(root)
    session_id = _new_session(client)
    _events, awaiting = _open_draft(client, session_id)
    target = root / awaiting["target_path"]
    before = target.read_bytes()

    rejected = client.post(
        f"/api/v1/drafts/{awaiting['draft_id']}/reject",
        json={"commit_token": awaiting["commit_token"], "reason": "奖励结构不合适"},
    )
    assert rejected.status_code == 200
    body = rejected.json()
    assert body["status"] == "rejected"
    assert body["reason"] == "奖励结构不合适"

    snapshot = client.get(f"/api/v1/sessions/{session_id}").json()
    assert snapshot["last_stage"] == "rejected"
    assert target.read_bytes() == before

    confirm = client.post(
        f"/api/v1/drafts/{awaiting['draft_id']}/confirm",
        json={"commit_token": awaiting["commit_token"]},
    )
    assert confirm.status_code == 409
    assert confirm.json()["detail"]["code"] == "E_COMMIT_TOKEN_CONSUMED"
    assert target.read_bytes() == before


def test_confirm_surfaces_stale_revision_without_overwrite(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    client, _writer = _client(root)
    session_id = _new_session(client)
    _events, awaiting = _open_draft(client, session_id)
    target = root / awaiting["target_path"]

    # Someone else edits the file between preview and confirm.
    tampered = json.loads(target.read_text(encoding="utf-8"))
    tampered["title"] = "被别人改过的任务"
    target.write_text(json.dumps(tampered, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    other_bytes = target.read_bytes()

    confirm = _post_sse(
        client,
        f"/api/v1/drafts/{awaiting['draft_id']}/confirm",
        {"commit_token": awaiting["commit_token"]},
    )
    names = [name for name, _ in confirm]
    assert "error" in names
    error = next(data for name, data in confirm if name == "error")
    assert error["code"] == "E_STALE_REVISION"
    assert confirm[-1][1]["status"] == "rejected"
    assert target.read_bytes() == other_bytes


def test_readonly_mode_refuses_confirm(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    app = create_app(
        llm=FakeLLM(structured_payload=valid_task_payload()),
        retriever=FakeRetriever(default_hits()),
        tasks_dir=root / "data" / "tasks",
        mcp_mode="readonly",
        session_ttl_s=3600,
    )
    client = TestClient(app)
    assert client.get("/healthz").json()["mcp"] == {
        "connected": False,
        "mode": "readonly",
    }
    session_id = _new_session(client)
    _events, awaiting = _open_draft(client, session_id)

    response = client.post(
        f"/api/v1/drafts/{awaiting['draft_id']}/confirm",
        json={"commit_token": awaiting["commit_token"]},
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "E_WRITE_DISABLED"
    assert (root / "data" / "tasks" / "T_SIDE_0214.json").read_bytes() == (
        REAL_TASKS / "T_SIDE_0214.json"
    ).read_bytes()
