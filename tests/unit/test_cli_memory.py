"""``gcc memory`` CLI tests (S6-02 acceptance): show/clear, JSON, confirmation.

Every test roots the JSON store at ``tmp_path``; the real
``data/memory/preferences`` is never created or touched.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from gameconfig_copilot.cli import app
from gameconfig_copilot.memory.preference import open_preference_store

runner = CliRunner()
DESIGNER = "u_1024"


def _output(result: Any) -> str:
    combined = getattr(result, "output", "") or ""
    combined += getattr(result, "stderr", "") or ""
    return combined


def _seed(root: Path, *, anomalous: bool = False) -> Path:
    """Write one learned preference via the real extraction path."""

    store = open_preference_store("json", root=root)
    store.extract(
        {
            "designer_id": DESIGNER,
            "commit_id": "commit-001",
            "draft": {
                "task_type": "side",
                "rewards": [{"type": "gold", "amount": 500}],
            },
            "signals": {"strictness": "medium", "naming_style": "snake_case"},
        }
    )
    if anomalous:
        # |5000 - 500| > 2 sigma, so it lands in the pending queue instead of
        # updating the learned band.
        store.extract(
            {
                "designer_id": DESIGNER,
                "commit_id": "commit-002",
                "draft": {
                    "task_type": "side",
                    "rewards": [{"type": "gold", "amount": 5000}],
                },
                "signals": {"strictness": "high", "naming_style": "camelCase"},
            }
        )
    return root / f"{DESIGNER}.json"


def _memory(*args: str) -> Any:
    return runner.invoke(app, ["memory", *args])


def test_show_renders_defaults_sources_and_pending(tmp_path: Path) -> None:
    path = _seed(tmp_path, anomalous=True)

    result = _memory("show", "--designer", DESIGNER, "--root", str(tmp_path))

    assert result.exit_code == 0, _output(result)
    output = _output(result)
    for token in (
        DESIGNER,
        "偏好默认值",
        "default_reward_band",
        "task_types",
        "strictness",
        "naming_style",
        "来源记录",
        "commit-001",
        "待确认队列",
        "pnd_",
    ):
        assert token in output, f"missing {token!r} in output:\n{output}"
    assert path.exists()


def test_show_without_preferences_is_empty_but_valid(tmp_path: Path) -> None:
    result = _memory("show", "--designer", "nobody", "--root", str(tmp_path))

    assert result.exit_code == 0, _output(result)
    output = _output(result)
    assert "尚无偏好" in output
    assert "来源记录（0 条）" in output
    assert "待确认队列（0 条）" in output
    assert not (tmp_path / "nobody.json").exists()


def test_show_json_is_machine_readable(tmp_path: Path) -> None:
    _seed(tmp_path)

    result = _memory("show", "--designer", DESIGNER, "--root", str(tmp_path), "--json")

    assert result.exit_code == 0, _output(result)
    payload = json.loads(result.stdout)
    assert payload["designer_id"] == DESIGNER
    assert payload["backend"] == "json"
    assert payload["expired"] is False
    assert payload["defaults"]["strictness"] == "medium"
    assert payload["defaults"]["naming_style"] == "snake_case"
    assert payload["defaults"]["task_types"] == ["side"]
    band = payload["defaults"]["default_reward_band"]
    assert band["min"] <= 500 <= band["max"]
    assert [source["source_commit"] for source in payload["sources"]] == ["commit-001"] * len(
        payload["sources"]
    )
    assert payload["pending"] == []


def test_show_json_without_preferences_stays_valid(tmp_path: Path) -> None:
    result = _memory(
        "show", "--designer", "nobody", "--root", str(tmp_path), "--json"
    )

    assert result.exit_code == 0, _output(result)
    payload = json.loads(result.stdout)
    assert payload["defaults"] == {}
    assert payload["sources"] == []
    assert payload["pending"] == []


def test_clear_refuses_without_yes(tmp_path: Path) -> None:
    path = _seed(tmp_path)

    result = _memory("clear", "--designer", DESIGNER, "--root", str(tmp_path))

    assert result.exit_code == 2, _output(result)
    output = _output(result)
    assert "--yes" in output
    assert "未删除任何数据" in output
    assert path.exists()


def test_clear_refuses_without_yes_in_json_mode(tmp_path: Path) -> None:
    path = _seed(tmp_path)

    result = _memory(
        "clear", "--designer", DESIGNER, "--root", str(tmp_path), "--json"
    )

    assert result.exit_code == 2, _output(result)
    payload = json.loads(result.stdout)
    assert payload["cleared"] is False
    assert payload["reason"] == "confirmation_required"
    assert path.exists()


def test_clear_with_yes_removes_record_and_is_idempotent(tmp_path: Path) -> None:
    path = _seed(tmp_path)

    result = _memory(
        "clear", "--designer", DESIGNER, "--root", str(tmp_path), "--yes"
    )

    assert result.exit_code == 0, _output(result)
    assert "已清除" in _output(result)
    assert not path.exists()

    again = _memory(
        "clear", "--designer", DESIGNER, "--root", str(tmp_path), "--yes"
    )
    assert again.exit_code == 0, _output(again)
    assert "没有找到" in _output(again)


def test_clear_json_reports_removed_record(tmp_path: Path) -> None:
    path = _seed(tmp_path)

    result = _memory(
        "clear", "--designer", DESIGNER, "--root", str(tmp_path), "--yes", "--json"
    )

    assert result.exit_code == 0, _output(result)
    assert json.loads(result.stdout) == {
        "designer_id": DESIGNER,
        "cleared": True,
        "location": str(path),
    }


def test_designer_id_alias_is_accepted(tmp_path: Path) -> None:
    _seed(tmp_path)

    result = _memory("show", "--designer-id", DESIGNER, "--root", str(tmp_path))

    assert result.exit_code == 0, _output(result)
    assert "default_reward_band" in _output(result)


def test_missing_designer_is_rejected(tmp_path: Path) -> None:
    result = _memory("show", "--root", str(tmp_path))

    assert result.exit_code == 2, _output(result)
    assert "--designer" in _output(result)


def test_unknown_action_is_rejected(tmp_path: Path) -> None:
    result = _memory("export", "--designer", DESIGNER, "--root", str(tmp_path))

    assert result.exit_code == 2, _output(result)
    assert "未知操作" in _output(result)
