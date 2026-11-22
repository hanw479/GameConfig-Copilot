"""CLI skeleton tests (S0-06): schema dump, friendly placeholders, unknown cmd."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from typer.testing import CliRunner

from gameconfig_copilot.cli import app

REPO_ROOT = Path(__file__).resolve().parents[2]
runner = CliRunner()


def _output(result: object) -> str:
    combined = getattr(result, "output", "") or ""
    combined += getattr(result, "stderr", "") or ""
    return combined


def test_schema_dump_emits_valid_json_schema() -> None:
    result = runner.invoke(app, ["schema", "dump"])

    assert result.exit_code == 0, _output(result)
    payload = json.loads(result.output)
    assert payload["$id"] == "urn:gameconfig-copilot:schema:task-draft:1"
    assert payload["additionalProperties"] is False
    assert "task_id" in payload["properties"]


def test_schema_dump_accepts_alias_and_all() -> None:
    report = runner.invoke(app, ["schema", "dump", "--name", "validation_report"])
    assert report.exit_code == 0, _output(report)
    assert json.loads(report.output)["$id"].endswith("validation-report:1")

    every = runner.invoke(app, ["schema", "dump", "--name", "all"])
    assert every.exit_code == 0, _output(every)
    assert set(json.loads(every.output)) == {"task", "report", "constraint"}


def test_schema_dump_writes_output_file(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "task.schema.json"
    result = runner.invoke(app, ["schema", "dump", "-n", "task", "-o", str(target)])

    assert result.exit_code == 0, _output(result)
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["title"] == "TaskDraft"


def test_schema_dump_rejects_unknown_name() -> None:
    result = runner.invoke(app, ["schema", "dump", "--name", "nope"])

    assert result.exit_code == 2
    assert "未知 schema" in _output(result)


def test_schema_list() -> None:
    result = runner.invoke(app, ["schema", "list"])

    assert result.exit_code == 0
    for name in ("task", "report", "constraint"):
        assert name in result.output


def test_help_lists_all_commands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    output = _output(result)
    for command in ("schema", "ingest", "search", "ask", "eval", "memory"):
        assert command in output


def test_unimplemented_commands_are_friendly() -> None:
    # ``eval`` landed in S1-07, ``ingest``/``search`` in S1-05/06/08 and
    # ``memory`` show/clear in S6-02; only the later-wave ``ask`` stays pending.
    expectations = {
        "ask": "S2",
    }
    for command, stage in expectations.items():
        result = runner.invoke(app, [command])
        assert result.exit_code == 2, f"{command}: {_output(result)}"
        output = _output(result)
        assert "尚未实现" in output
        assert stage in output


def _ingest_into(tmp_path: Path) -> Path:
    index_dir = tmp_path / "index"
    result = runner.invoke(
        app,
        [
            "ingest",
            "--source",
            str(REPO_ROOT / "data"),
            "--index",
            str(index_dir),
            "--bm25-backend",
            "python",
        ],
    )
    assert result.exit_code == 0, _output(result)
    return index_dir


def test_ingest_then_search_roundtrip(tmp_path: Path) -> None:
    index_dir = _ingest_into(tmp_path)

    result = runner.invoke(
        app,
        [
            "search",
            "铁匠老陈 6 级支线",
            "--top-k",
            "3",
            "--type",
            "task",
            "--level",
            "6-9",
            "--index",
            str(index_dir),
        ],
    )

    assert result.exit_code == 0, _output(result)
    output = _output(result)
    assert "来源：task:" in output
    assert "分数：" in output
    assert "T_SIDE_0213" in output
    assert "来源：wiki:" not in output
    assert "来源：constraint:" not in output


def test_ingest_second_run_reuses_unchanged_index(tmp_path: Path) -> None:
    index_dir = _ingest_into(tmp_path)

    result = runner.invoke(
        app,
        [
            "ingest",
            "--source",
            str(REPO_ROOT / "data"),
            "--index",
            str(index_dir),
            "--bm25-backend",
            "python",
        ],
    )

    assert result.exit_code == 0, _output(result)
    assert "索引已是最新" in _output(result)


def test_search_without_index_points_to_ingest(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["search", "矿洞", "--index", str(tmp_path / "missing")],
    )

    assert result.exit_code == 2
    assert "gcc ingest" in _output(result)


def test_search_rejects_unknown_source_type(tmp_path: Path) -> None:
    index_dir = _ingest_into(tmp_path)

    result = runner.invoke(
        app,
        ["search", "矿洞", "--type", "npc", "--index", str(index_dir)],
    )

    assert result.exit_code == 2
    assert "未知来源类型" in _output(result)


def test_unknown_command_is_friendly() -> None:
    result = runner.invoke(app, ["frobnicate"])

    assert result.exit_code != 0
    output = _output(result).lower()
    assert "frobnicate" in output
    assert "no such command" in output or "did you mean" in output


def test_module_entrypoint_works_without_api_key(tmp_path: Path) -> None:
    """Mirrors the acceptance command: ``python -m gameconfig_copilot.cli schema dump``."""

    env = os.environ.copy()
    env.pop("GCC_LLM_API_KEY", None)
    env.pop("GCC_LLM_PROVIDER", None)
    env["GCC_TRACE_PATH"] = str(tmp_path / "cli-traces.jsonl")
    # The CLI forces UTF-8 on its own streams; pin the harness the same way so
    # the assertion does not depend on the machine's ANSI code page.
    env["PYTHONIOENCODING"] = "utf-8"
    existing_path = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(REPO_ROOT / "src"), existing_path) if part
    )

    proc = subprocess.run(
        [sys.executable, "-B", "-m", "gameconfig_copilot.cli", "schema", "dump"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=90,
    )

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["$id"].endswith("task-draft:1")
    assert payload["title"] == "TaskDraft"
