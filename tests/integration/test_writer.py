"""Destructive and end-to-end tests for the S5 write path (S5-08).

Owner: Write Worker M. Every test works on a ``tmp_path`` copy of
``data/tasks``; the real corpus and ``data/`` tree are only ever read
(``shutil.copy2`` is the single read access). Four destructive categories are
covered on purpose:

1. concurrent commits -> the loser gets ``E_STALE_REVISION``;
2. interrupted write -> target stays complete, temp residue is cleaned;
3. escaping paths (``..`` / absolute / symlink) -> ``E_PATH_DENIED``;
4. readonly mode -> write tools are absent from the tool list.

The rest exercises commit/rollback end to end (backup + audit + restore).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from gameconfig_copilot.mcp_server.server import (
    READONLY_TOOLS,
    WRITE_TOOLS,
    ServerConfig,
    build_registry,
    build_server,
    main,
)
from gameconfig_copilot.mcp_server.tools import ToolService
from gameconfig_copilot.mcp_server.writer import (
    E_FINGERPRINT_MISMATCH,
    E_PATH_DENIED,
    E_STALE_REVISION,
    EMPTY_REVISION,
    Writer,
    WriterError,
    fingerprint_for,
    revision_of_file,
)

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_TASKS = REPO_ROOT / "data" / "tasks"
SRC_ROOT = REPO_ROOT / "src"
TASK_ID = "T_SIDE_0213"
TARGET_REL = f"data/tasks/{TASK_ID}.json"


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A throwaway repo root with a copy of every real task file."""

    root = tmp_path / "repo"
    tasks = root / "data" / "tasks"
    tasks.mkdir(parents=True)
    sources = sorted(REAL_TASKS.glob("*.json"))
    assert sources, "real data/tasks must contain the frozen corpus"
    for source in sources:
        shutil.copy2(source, tasks / source.name)
    return root


@pytest.fixture
def writer(workspace: Path) -> Writer:
    instance = Writer(workspace)
    instance.prepare()
    return instance


@pytest.fixture
def service(writer: Writer) -> ToolService:
    return ToolService(writer)


def _load(root: Path, task_id: str = TASK_ID) -> dict[str, Any]:
    payload = json.loads(
        (root / "data" / "tasks" / f"{task_id}.json").read_text(encoding="utf-8")
    )
    assert isinstance(payload, dict)
    return payload


def _target(root: Path, task_id: str = TASK_ID) -> Path:
    return root / "data" / "tasks" / f"{task_id}.json"


def _audited_events(writer: Writer) -> list[str]:
    return [record["event"] for record in writer.read_audit()]


# --------------------------------------------------------------------------- #
# 1. concurrent commits
# --------------------------------------------------------------------------- #


def test_concurrent_commits_second_gets_stale_revision(workspace: Path) -> None:
    draft = _load(workspace)
    draft["rewards"][0]["amount"] = 150
    preview = Writer(workspace).preview(draft)

    outcomes: list[tuple[str, Any]] = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def attempt(author: str) -> None:
        local = Writer(workspace)
        barrier.wait(timeout=10)
        try:
            result = local.commit(
                draft,
                fingerprint=preview["fingerprint"],
                expected_revision=preview["revision"],
                author=author,
            )
            outcome: tuple[str, Any] = ("committed", result["commit_id"])
        except WriterError as exc:
            outcome = (exc.code, exc.details)
        with lock:
            outcomes.append(outcome)

    threads = [
        threading.Thread(target=attempt, args=(f"racer-{index}",), daemon=True)
        for index in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    codes = [code for code, _ in outcomes]
    assert codes.count("committed") == 1, outcomes
    assert codes.count(E_STALE_REVISION) == 1, outcomes
    stale_details = next(details for code, details in outcomes if code == E_STALE_REVISION)
    assert stale_details["expected"] == preview["revision"]
    assert stale_details["actual"] == revision_of_file(_target(workspace))
    assert _load(workspace)["rewards"][0]["amount"] == 150
    commits = [record for record in Writer(workspace).read_audit() if record["event"] == "commit"]
    assert len(commits) == 1


# --------------------------------------------------------------------------- #
# 2. interrupted process / failed replace
# --------------------------------------------------------------------------- #


def test_failed_replace_keeps_target_and_cleans_temp(
    workspace: Path,
    writer: Writer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft = _load(workspace)
    draft["rewards"][0]["amount"] = 150
    preview = writer.preview(draft)
    target = _target(workspace)
    before = target.read_bytes()
    real_replace = os.replace

    def fail_target_replace(source: Any, destination: Any) -> None:
        if Path(destination).resolve() == target.resolve():
            raise OSError("simulated crash before os.replace")
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_target_replace)
    with pytest.raises(WriterError) as info:
        writer.commit(
            draft,
            fingerprint=preview["fingerprint"],
            expected_revision=preview["revision"],
            author="interrupted",
        )
    assert info.value.code == "E_IO"
    assert target.read_bytes() == before
    assert not list((workspace / "data" / "tasks").glob(".*.tmp-*"))
    assert not list((workspace / "data" / "backups").rglob(".*.tmp-*"))
    assert writer.read_audit() == []

    monkeypatch.undo()
    retry = writer.commit(
        draft,
        fingerprint=preview["fingerprint"],
        expected_revision=preview["revision"],
        author="retry",
    )
    assert retry["status"] == "committed"
    assert _load(workspace)["rewards"][0]["amount"] == 150


def test_killed_writer_leaves_no_half_write(workspace: Path, writer: Writer) -> None:
    target = _target(workspace)
    before = target.read_bytes()
    script = (
        "import os, sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(SRC_ROOT)!r})\n"
        "from gameconfig_copilot.mcp_server.writer import Writer\n"
        "root = Path(sys.argv[1])\n"
        "w = Writer(root)\n"
        f"with w.lock({TASK_ID!r}):\n"
        f"    target = root / 'data' / 'tasks' / ({TASK_ID!r} + '.json')\n"
        "    tmp = target.parent / ('.' + target.name + '.tmp-%d-crash' % os.getpid())\n"
        "    tmp.write_bytes(b'{\"half\": ')\n"
        "    os._exit(1)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script, str(workspace)],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "PYTHONPATH": str(SRC_ROOT)},
        check=False,
    )
    assert completed.returncode == 1, completed.stderr
    assert target.read_bytes() == before
    residue = list((workspace / "data" / "tasks").glob(".*.tmp-*"))
    assert residue, "the child must leave an observable temp file"

    stats = writer.prepare()
    assert stats["cleaned_temp_files"] >= 1
    assert not list((workspace / "data" / "tasks").glob(".*.tmp-*"))

    draft = _load(workspace)
    draft["rewards"][0]["amount"] = 140
    preview = writer.preview(draft)
    recovered = Writer(workspace).commit(
        draft,
        fingerprint=preview["fingerprint"],
        expected_revision=preview["revision"],
        author="after-crash",
    )
    assert recovered["status"] == "committed"
    assert _load(workspace)["rewards"][0]["amount"] == 140


def test_startup_cleans_orphan_temp_files(writer: Writer) -> None:
    orphan = writer.tasks_dir / "T_SIDE_9999.json.tmp-999999-orphan"
    orphan.write_text('{"half": ', encoding="utf-8")
    assert writer.cleanup_temp_files(grace_s=0) == 1
    assert not orphan.exists()


# --------------------------------------------------------------------------- #
# 3. path escapes
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw",
    [
        "../etc/passwd",
        "../../data/tasks/T_SIDE_0214.json",
        "sub/../../T_SIDE_0214.json",
        "T_SIDE_0213.txt",
        "T_SIDE_0213.json.txt",
        "",
    ],
)
def test_guard_path_denied(writer: Writer, raw: str) -> None:
    with pytest.raises(WriterError) as info:
        writer.guard_path(raw)
    assert info.value.code == E_PATH_DENIED


def test_guard_accepts_whitelisted_path_forms(writer: Writer) -> None:
    expected = writer.target_for_task_id(TASK_ID)
    assert writer.guard_path(f"{TASK_ID}.json") == expected
    assert writer.guard_path(f"data/tasks/{TASK_ID}.json") == expected


def test_guard_rejects_absolute_path(writer: Writer, tmp_path: Path) -> None:
    absolute = tmp_path / "evil.json"
    with pytest.raises(WriterError) as info:
        writer.guard_path(str(absolute))
    assert info.value.code == E_PATH_DENIED


@pytest.mark.skipif(os.name != "nt", reason="drive-letter escape is Windows-only")
def test_guard_rejects_windows_drive_path(writer: Writer) -> None:
    with pytest.raises(WriterError) as info:
        writer.guard_path("C:\\Windows\\evil.json")
    assert info.value.code == E_PATH_DENIED


def test_guard_rejects_symlink_escape(writer: Writer, workspace: Path) -> None:
    outside_dir = workspace.parent / "outside"
    outside_dir.mkdir()
    outside_file = outside_dir / "outside.json"
    outside_file.write_text('{"task_id": "T_SIDE_0999"}\n', encoding="utf-8")
    link = writer.tasks_dir / "T_SIDE_0999.json"
    if not _make_link(link, outside_file, fallback_dir=outside_dir):
        pytest.skip("symlinks/junctions are not permitted for this user/host")
    try:
        with pytest.raises(WriterError) as info:
            writer.guard_path("T_SIDE_0999.json")
        assert info.value.code == E_PATH_DENIED
    finally:
        _remove_link(link)


def _make_link(link: Path, target: Path, *, fallback_dir: Path) -> bool:
    """Create a file symlink, else (Windows) a directory junction to escape."""

    try:
        link.symlink_to(target)
        return True
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(fallback_dir)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        return completed.returncode == 0 and link.exists()
    return False


def _remove_link(link: Path) -> None:
    try:
        if link.is_symlink() or link.is_file():
            link.unlink(missing_ok=True)
        elif link.is_dir():
            link.rmdir()
    except OSError:
        pass


def test_commit_rejects_escaping_task_id(writer: Writer) -> None:
    draft = _load(writer.root)
    draft["task_id"] = "../../etc/passwd"
    with pytest.raises(WriterError) as info:
        writer.commit(draft, fingerprint="0" * 16, expected_revision="a" * 12, author="x")
    assert info.value.code == E_PATH_DENIED


# --------------------------------------------------------------------------- #
# 4. readonly mode
# --------------------------------------------------------------------------- #


def test_readonly_mode_registers_no_write_tools(service: ToolService) -> None:
    registry = build_registry(service, "readonly")
    assert registry.names() == list(READONLY_TOOLS)
    for name in WRITE_TOOLS:
        assert name not in registry
        with pytest.raises(KeyError):
            registry.get(name)

    write_registry = build_registry(service, "write")
    assert write_registry.names() == [*READONLY_TOOLS, *WRITE_TOOLS]


def test_build_server_readonly_tool_list(workspace: Path) -> None:
    bundle = build_server(ServerConfig(mode="readonly", root=workspace))
    assert bundle.tool_names == list(READONLY_TOOLS)
    assert bundle.backend in {"fastmcp", "local-registry"}
    write_bundle = build_server(ServerConfig(mode="write", root=workspace))
    assert write_bundle.tool_names == [*READONLY_TOOLS, *WRITE_TOOLS]


def test_cli_list_tools_readonly(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["--root", str(workspace), "--mode", "readonly", "--list-tools"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert [item["name"] for item in payload] == list(READONLY_TOOLS)


def test_fastmcp_tool_list_matches_mode(workspace: Path) -> None:
    pytest.importorskip("fastmcp")
    import asyncio

    bundle = build_server(ServerConfig(mode="readonly", root=workspace))
    assert bundle.backend == "fastmcp"
    assert bundle.mcp is not None
    tools = asyncio.run(bundle.mcp.list_tools())
    names = {tool.name for tool in tools}
    assert names == set(READONLY_TOOLS)


def test_fastmcp_adapter_registers_only_mode_tools(service: ToolService) -> None:
    """The adapter hands exactly the registry functions to the backend."""

    from gameconfig_copilot.mcp_server.server import _create_mcp

    class ModernMCP:
        def __init__(self, *, name: str, tools: list[Any]) -> None:
            self.name = name
            self.tools = list(tools)

    registry = build_registry(service, "readonly")
    mcp = _create_mcp(ModernMCP, registry)
    assert [getattr(tool, "__name__", "") for tool in mcp.tools] == list(READONLY_TOOLS)

    class LegacyMCP:
        """Older fastmcp: no ``tools=`` argument, one-arg ``add_tool``."""

        def __init__(self, *, name: str) -> None:
            self.name = name
            self.registered: list[str] = []

        def add_tool(self, function: Any) -> None:
            self.registered.append(function.__name__)

    legacy = _create_mcp(LegacyMCP, registry)
    assert legacy.registered == list(READONLY_TOOLS)


# --------------------------------------------------------------------------- #
# fingerprint binding
# --------------------------------------------------------------------------- #


def test_fingerprint_mismatch_is_refused(writer: Writer, service: ToolService) -> None:
    draft = _load(writer.root)
    draft["rewards"][0]["amount"] = 150
    preview = writer.preview(draft)
    target = _target(writer.root)
    before = target.read_bytes()

    result = service.commit_draft(draft, "0" * 16, preview["revision"], "alice")
    assert result["ok"] is False
    assert result["error"]["code"] == E_FINGERPRINT_MISMATCH
    assert result["error"]["details"]["expected"] == "0" * 16
    assert result["error"]["details"]["actual"] == preview["fingerprint"]
    assert target.read_bytes() == before
    assert writer.read_audit() == []


def test_fingerprint_is_stable_and_sensitive(writer: Writer) -> None:
    draft = _load(writer.root)
    baseline = writer.preview(draft)
    assert writer.preview(draft)["fingerprint"] == baseline["fingerprint"]

    changed = json.loads(json.dumps(draft))
    changed["rewards"][0]["amount"] += 1
    assert writer.preview(changed)["fingerprint"] != baseline["fingerprint"]

    # Bound to the target path and the base revision, not only to the draft.
    other_path = fingerprint_for(draft, "data/tasks/T_SIDE_0214.json", baseline["revision"])
    other_revision = fingerprint_for(draft, baseline["target_path"], EMPTY_REVISION)
    assert other_path != baseline["fingerprint"]
    assert other_revision != baseline["fingerprint"]


# --------------------------------------------------------------------------- #
# end-to-end commit / rollback
# --------------------------------------------------------------------------- #


def test_commit_end_to_end_backup_and_audit(
    workspace: Path,
    writer: Writer,
    service: ToolService,
) -> None:
    draft = _load(workspace)
    draft["rewards"][0]["amount"] = 150
    preview = service.preview_draft(draft)
    assert preview["ok"] is True
    assert preview["target_path"] == TARGET_REL
    assert preview["stats"] == {"added": 1, "removed": 1}
    assert preview["revision"] == revision_of_file(_target(workspace))

    result = service.commit_draft(
        draft,
        preview["fingerprint"],
        preview["revision"],
        "alice",
        note="tune gold reward",
    )
    assert result["ok"] is True
    assert result["status"] == "committed"
    assert result["report"]["ok"] is True
    assert result["new_revision"] == revision_of_file(_target(workspace))

    backup = workspace / result["backup_path"]
    assert backup.is_file()
    backup_payload = json.loads(backup.read_text(encoding="utf-8"))
    assert backup_payload["rewards"][0]["amount"] == 120
    assert _load(workspace)["rewards"][0]["amount"] == 150

    records = writer.read_audit()
    assert _audited_events(writer) == ["commit"]
    assert records[0]["task_id"] == TASK_ID
    assert records[0]["author"] == "alice"
    assert records[0]["note"] == "tune gold reward"
    assert records[0]["base_revision"] == preview["revision"]
    assert records[0]["new_revision"] == result["new_revision"]
    assert records[0]["diff_stats"] == {"added": 1, "removed": 1}

    view = service.get_task_config(TASK_ID)
    assert view["ok"] is True
    assert view["revision"] == result["new_revision"]
    assert view["content"] == _load(workspace)
    assert view["path"] == TARGET_REL


def test_rejected_draft_writes_nothing(
    workspace: Path,
    writer: Writer,
    service: ToolService,
) -> None:
    draft = _load(workspace)
    draft["level_range"] = {"min": 1, "max": 3}
    draft["rewards"] = [{"type": "gold", "amount": 1000}]
    preview = writer.preview(draft)
    target = _target(workspace)
    before = target.read_bytes()

    result = service.commit_draft(draft, preview["fingerprint"], preview["revision"], "alice")
    assert result["ok"] is True
    assert result["status"] == "rejected"
    codes = [error["code"] for error in result["report"]["errors"]]
    assert "E_REWARD_RANGE" in codes
    assert target.read_bytes() == before
    assert not list(writer.backups_dir.rglob("*.json"))
    assert writer.read_audit() == []


def test_create_new_task_has_no_backup(workspace: Path, writer: Writer) -> None:
    draft = _load(workspace)
    draft["task_id"] = "T_SIDE_0299"
    draft["title"] = "测试新建支线"
    draft["prerequisites"] = []
    preview = writer.preview(draft)
    assert preview["revision"] == EMPTY_REVISION
    assert preview["stats"]["added"] > 0
    result = writer.commit(
        draft,
        fingerprint=preview["fingerprint"],
        expected_revision=EMPTY_REVISION,
        author="alice",
    )
    assert result["status"] == "committed"
    assert result["backup_path"] is None
    assert _target(workspace, "T_SIDE_0299").is_file()
    assert writer.read_audit()[-1]["backup_path"] is None


def test_rollback_restores_backup_and_keeps_history(workspace: Path, writer: Writer) -> None:
    original = _load(workspace)
    assert original["rewards"][0]["amount"] == 120

    first = dict(original)
    first["rewards"] = [{"type": "gold", "amount": 150}, {"type": "exp", "amount": 800}]
    preview = writer.preview(first)
    first_commit = writer.commit(
        first,
        fingerprint=preview["fingerprint"],
        expected_revision=preview["revision"],
        author="alice",
    )
    assert first_commit["backup_path"] is not None

    second = json.loads(json.dumps(first))
    second["rewards"][0]["amount"] = 180
    preview = writer.preview(second)
    second_commit = writer.commit(
        second,
        fingerprint=preview["fingerprint"],
        expected_revision=preview["revision"],
        author="alice",
    )
    assert _load(workspace)["rewards"][0]["amount"] == 180

    rollback = writer.rollback(first_commit["commit_id"])
    assert rollback["status"] == "committed"
    assert rollback["restored_from"] == first_commit["commit_id"]
    assert rollback["commit_id"] != first_commit["commit_id"]
    assert rollback["new_revision"] == revision_of_file(_target(workspace))
    assert _load(workspace)["rewards"][0]["amount"] == 120
    assert _audited_events(writer) == ["commit", "commit", "rollback"]
    rollback_record = writer.read_audit()[-1]
    assert rollback_record["restored_from"] == first_commit["commit_id"]
    rollback_backup = workspace / rollback_record["backup_path"]
    assert rollback_backup.is_file()
    assert rollback_backup != workspace / second_commit["backup_path"]
    assert json.loads(rollback_backup.read_text(encoding="utf-8"))["rewards"][0]["amount"] == 180

    # History is append-only: rolling back the second commit restores its content.
    restored = writer.rollback(second_commit["commit_id"])
    assert restored["status"] == "committed"
    assert _load(workspace)["rewards"][0]["amount"] == 150
    assert len(writer.read_audit()) == 4


def test_rollback_unknown_commit_is_refused(writer: Writer) -> None:
    with pytest.raises(WriterError) as info:
        writer.rollback("deadbeefcafe")
    assert info.value.code == "E_BACKUP_NOT_FOUND"


def test_startup_reconcile_records_revision_drift(workspace: Path, writer: Writer) -> None:
    draft = _load(workspace)
    draft["rewards"][0]["amount"] = 150
    preview = writer.preview(draft)
    writer.commit(
        draft,
        fingerprint=preview["fingerprint"],
        expected_revision=preview["revision"],
        author="alice",
    )
    # Simulate "replaced but audit append lost" by editing the file directly.
    tampered = _load(workspace)
    tampered["tags"] = [*tampered["tags"], "tampered"]
    _target(workspace).write_text(
        json.dumps(tampered, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    stats = writer.prepare()
    assert stats["recovered_commits"] == 1
    assert _audited_events(writer)[-1] == "recovered"
    assert writer.prepare()["recovered_commits"] == 0


# --------------------------------------------------------------------------- #
# read tools
# --------------------------------------------------------------------------- #


def test_read_tools_return_structured_errors(service: ToolService) -> None:
    missing = service.get_task_config("T_SIDE_0999")
    assert missing["ok"] is False
    assert missing["error"]["code"] == "E_NOT_FOUND"

    invalid = service.list_recent_tasks(limit=0)
    assert invalid["ok"] is False
    assert invalid["error"]["code"] == "E_INVALID_ARGUMENT"

    unknown_filter = service.list_recent_tasks({"nope": 1})
    assert unknown_filter["ok"] is False
    assert unknown_filter["error"]["code"] == "E_INVALID_ARGUMENT"


def test_list_and_constraints(service: ToolService) -> None:
    listing = service.list_recent_tasks({"task_type": "side"}, 3)
    assert listing["ok"] is True
    assert listing["count"] == 3
    assert all(item["task_type"] == "side" for item in listing["tasks"])
    assert all(item["path"].startswith("data/tasks/") for item in listing["tasks"])

    filtered = service.list_constraints("rewards")
    assert filtered["ok"] is True
    assert filtered["count"] >= 1
    assert all(
        item["id"].startswith("C_REWARD") or item["scope"].startswith("rewards")
        for item in filtered["constraints"]
    )


def test_validate_draft_matches_writer_validation(writer: Writer, service: ToolService) -> None:
    draft = _load(writer.root)
    result = service.validate_draft(draft)
    assert result["ok"] is True
    assert result["report"] == writer.validate(draft).to_dict()
    assert result["report"]["ok"] is True
    assert result["report"]["checked"]["schema"] > 0
