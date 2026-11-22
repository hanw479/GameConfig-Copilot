"""Shared pytest fixtures.

Autouse isolation keeps tests independent of the developer machine:
no real ``GCC_LLM_*`` secrets, no writes to the repository ``traces/``.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any

import pytest

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from gameconfig_copilot.config import reset_settings_cache  # noqa: E402
from gameconfig_copilot.observability.tracer import Tracer, reset_tracer  # noqa: E402

_GCC_VARIABLES = (
    "GCC_LLM_PROVIDER",
    "GCC_LLM_MODEL",
    "GCC_LLM_BASE_URL",
    "GCC_LLM_API_KEY",
    "GCC_LLM_TIMEOUT_S",
)


@pytest.fixture(autouse=True)
def _runtime_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _GCC_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GCC_TRACE_PATH", str(tmp_path / "traces.jsonl"))
    reset_settings_cache()
    reset_tracer()
    yield
    reset_settings_cache()
    reset_tracer()


@pytest.fixture
def task_payload() -> dict[str, Any]:
    """A valid TaskDraft payload aligned with docs/01 §7.1."""

    return copy.deepcopy(
        {
            "schema_version": 1,
            "task_id": "T_SIDE_0213",
            "title": "铁匠的委托：失踪的矿车",
            "task_type": "side",
            "level_range": {"min": 6, "max": 9},
            "giver": {"npc_id": "NPC_013", "dialogue_ref": "DLG_013_02"},
            "prerequisites": [{"type": "task_completed", "ref": "T_MAIN_0102"}],
            "objectives": [
                {
                    "seq": 1,
                    "type": "talk_to",
                    "target": "NPC_013",
                    "count": 1,
                    "area": "MAP_STARPORT_01",
                },
                {
                    "seq": 2,
                    "type": "collect",
                    "target": "ITEM_ORE_02",
                    "count": 4,
                    "area": "MAP_MINE_03",
                },
            ],
            "rewards": [{"type": "gold", "amount": 120}, {"type": "exp", "amount": 800}],
            "branches": [],
            "tags": ["side", "chapter01", "npc_blacksmith"],
            "provenance": {
                "request": "给铁匠老陈加一条 6 级支线，去矿洞捡 4 块矿石",
                "evidence": ["wiki:world_overview#faction", "task:T_MAIN_0102#objectives"],
                "model": "deepseek-chat",
                "created_at": "2026-11-12T10:00:00+08:00",
            },
        }
    )


@pytest.fixture
def tracer(tmp_path: Path) -> Tracer:
    return Tracer(tmp_path / "trace.jsonl")
