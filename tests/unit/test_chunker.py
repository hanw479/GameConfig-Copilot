"""Chunker tests (S1-04): heading hierarchy, 400–600 windows, parent links."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from gameconfig_copilot.rag.chunker import (
    MAX_CHUNK_CHARS,
    MIN_CHUNK_CHARS,
    chunk_constraints,
    chunk_task,
    chunk_wiki,
    split_wiki_sections,
    split_windows,
)
from gameconfig_copilot.rag.corpus import load_chunks

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA = REPO_ROOT / "data"
LONG_SENTENCE = "矿洞在星港城东侧，矿车沿着断轨缓慢行驶，通风机每隔一阵停摆。"
WIKI_TEXT = (
    "# 星港志\n\n"
    "> 用途：测试标题层级与窗口切分。\n\n"
    "## 一、总览\n\n"
    + LONG_SENTENCE * 30
    + "\n\n### 1.1 细节\n\n短节内容，只验证层级。\n\n"
    "## 二、实体\n\n| ID | 名称 |\n| --- | --- |\n| `MAP_MINE_03` | 矿洞 |\n"
)


def test_wiki_heading_hierarchy_is_preserved() -> None:
    sections = split_wiki_sections(WIKI_TEXT)

    assert [section.heading_path for section in sections] == [
        ("星港志",),
        ("星港志", "一、总览"),
        ("星港志", "一、总览", "1.1 细节"),
        ("星港志", "二、实体"),
    ]
    assert sections[2].level == 3


def test_wiki_windows_are_bounded_and_overlap_by_ten_percent() -> None:
    body = LONG_SENTENCE * 30
    windows = split_windows(body)

    assert len(windows) >= 2
    assert all(len(piece) <= MAX_CHUNK_CHARS for _, _, piece in windows)
    assert all(len(piece) >= MIN_CHUNK_CHARS for _, _, piece in windows[:-1])
    for (start, end, piece), (next_start, _, next_piece) in zip(
        windows, windows[1:], strict=False
    ):
        overlap = int((end - start) * 0.10)
        assert 0 < next_start < end
        assert piece[-overlap:] == next_piece[:overlap]
        assert next_start == end - overlap


def test_wiki_chunks_carry_citation_metadata() -> None:
    chunks = chunk_wiki(WIKI_TEXT, source_name="starport")

    assert chunks
    assert {chunk.metadata["source_type"] for chunk in chunks} == {"wiki"}
    assert {chunk.metadata["source_id"] for chunk in chunks} == {"wiki:starport"}
    positions = {chunk.metadata["position"] for chunk in chunks}
    # The document H1 is dropped from the citation because source_id names it.
    assert "一、总览" in positions
    assert "一、总览 / 1.1 细节" in positions
    overview = [c for c in chunks if c.metadata["position"] == "一、总览"]
    assert len(overview) >= 2
    assert overview[0].metadata["window_total"] == len(overview)
    assert overview[0].metadata["heading_path"] == ["星港志", "一、总览"]
    assert all(len(chunk.text) <= MAX_CHUNK_CHARS for chunk in chunks)


def test_chunk_ids_are_deterministic() -> None:
    first = chunk_wiki(WIKI_TEXT, source_name="starport")
    second = chunk_wiki(WIKI_TEXT, source_name="starport")

    assert [chunk.chunk_id for chunk in first] == [chunk.chunk_id for chunk in second]
    assert len({chunk.chunk_id for chunk in first}) == len(first)


def test_task_chunks_have_parent_links_and_payload(tmp_path: Path) -> None:
    task = json.loads((DATA / "tasks" / "T_MAIN_0102.json").read_text(encoding="utf-8"))
    chunks = chunk_task(task, source_path="tasks/T_MAIN_0102.json")

    assert [chunk.metadata["position"] for chunk in chunks] == [
        "task",
        "objectives",
        "rewards",
        "branches",
    ]
    parent, *children = chunks
    assert parent.chunk_id == "task:T_MAIN_0102"
    assert parent.metadata["parent_id"] is None
    assert parent.metadata["task_id"] == "T_MAIN_0102"
    assert parent.metadata["task_type"] == "main"
    assert (parent.metadata["level_min"], parent.metadata["level_max"]) == (4, 8)
    assert parent.metadata["npc_id"] == "NPC_001"
    assert "封条之下" in parent.text
    for child in children:
        assert child.metadata["parent_id"] == parent.chunk_id
        assert child.metadata["task_id"] == task["task_id"]
        assert child.metadata["source_id"] == "task:T_MAIN_0102"
    by_position = {chunk.metadata["position"]: chunk for chunk in children}
    assert "ITEM_ORE_02" in by_position["objectives"].text
    assert "金币 150" in by_position["rewards"].text
    assert "b_report" in by_position["branches"].text
    assert "b_conceal" in by_position["branches"].text


def test_task_without_branches_still_has_a_branch_block(task_payload: dict[str, Any]) -> None:
    payload = copy.deepcopy(task_payload)
    chunks = chunk_task(payload)

    branches = chunks[3]
    assert branches.metadata["position"] == "branches"
    assert branches.text.endswith("无")


def test_task_requires_task_id() -> None:
    with pytest.raises(ValueError):
        chunk_task({"title": "缺少 ID"})


def test_constraints_are_one_chunk_per_rule() -> None:
    payload = yaml.safe_load((DATA / "constraints" / "rewards.yaml").read_text(encoding="utf-8"))
    chunks = chunk_constraints(payload, source_file="constraints/rewards.yaml")

    assert len(chunks) == len(payload) == 6
    ids = [chunk.chunk_id for chunk in chunks]
    assert ids == [f"constraint:{item['id']}" for item in payload]
    first = chunks[0]
    assert first.metadata["source_type"] == "constraint"
    assert first.metadata["source_id"] == "constraint:C_REWARD_GOLD_01"
    assert first.metadata["scope"] == "rewards.gold"
    assert first.metadata["severity"] == "error"
    assert "Lv≤10 上限 200" in first.text
    assert first.metadata["bands"][0] == {"max_level": 10, "max_amount": 200}
    assert "C_REWARD_EXP_01" not in first.text
    assert "控制经济通胀" in first.text

    floor = next(
        chunk for chunk in chunks if chunk.chunk_id == "constraint:C_REWARD_GOLD_MIN_01"
    )
    assert floor.metadata["constraint_type"] == "level_band_range"
    assert "Lv≤10 [60, 200]" in floor.text
    assert floor.metadata["bands"][0]["min_amount"] == 60
    assert floor.metadata["bands"][0]["max_amount"] == 200
    assert "成长曲线" in floor.text


def test_constraint_requires_id() -> None:
    with pytest.raises(ValueError):
        chunk_constraints([{"scope": "rewards.gold"}], source_file="bad.yaml")


def test_invalid_window_parameters() -> None:
    with pytest.raises(ValueError):
        split_windows("abc", min_chars=10, max_chars=5)
    with pytest.raises(ValueError):
        split_windows("abc", overlap_ratio=1.0)


def test_corpus_loader_is_complete_and_idempotent() -> None:
    chunks = load_chunks(DATA)
    again = load_chunks(DATA)

    assert len(chunks) > 50
    assert [chunk.chunk_id for chunk in chunks] == [chunk.chunk_id for chunk in again]
    assert len({chunk.chunk_id for chunk in chunks}) == len(chunks)
    assert {chunk.source_type for chunk in chunks} == {"wiki", "task", "constraint"}
    task_chunks = [chunk for chunk in chunks if chunk.source_type == "task"]
    assert any(chunk.metadata["position"] == "objectives" for chunk in task_chunks)
