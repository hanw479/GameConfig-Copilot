"""Unit tests for the S7-01 v2 golden set (30 = 20 normal / 5 boundary / 5 adversarial).

Every metric here comes from a canned generator/validator, so the suite needs no
index, no LLM and no API key. The adversarial half mirrors V's
``tests/fixtures/adversarial/`` error codes (G4 coverage check).
"""

from __future__ import annotations

import copy
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from gameconfig_copilot.evaluation import agent_eval as aev
from gameconfig_copilot.schemas.task import TaskDraft

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXED_TIME = "2026-11-15T12:00:00+08:00"
INJECTION_TERMS = ("忽略", "跳过", "废止", "直接返回", "ok=true", "跳过 validator")

ENTITY_ID_RE = re.compile(r"\b(?:NPC|MAP|ITEM|DLG|FACTION)_[A-Z0-9_]+\b")
TASK_ID_RE = re.compile(r"\bT_(?:MAIN|SIDE|DAILY)_\d{4}\b")

PASS_CODES = frozenset({"passed", "passed_after_repair"})


# ---------------------------------------------------------------------------
# Corpus / fixture helpers
# ---------------------------------------------------------------------------


def _corpus_ids() -> set[str]:
    ids: set[str] = set()
    for path in (REPO_ROOT / "data" / "wiki").glob("*.md"):
        text = path.read_text(encoding="utf-8")
        ids.update(ENTITY_ID_RE.findall(text))
        ids.update(TASK_ID_RE.findall(text))
    for path in (REPO_ROOT / "data" / "tasks").glob("*.json"):
        text = path.read_text(encoding="utf-8")
        ids.update(ENTITY_ID_RE.findall(text))
        ids.update(TASK_ID_RE.findall(text))
    return ids


def _constraint_ids() -> set[str]:
    ids: set[str] = set()
    for path in (REPO_ROOT / "data" / "constraints").glob("*.yaml"):
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        for item in payload:
            ids.add(item["id"])
    return ids


def _bands(constraint_id: str) -> list[dict[str, int]]:
    payload = yaml.safe_load(
        (REPO_ROOT / "data" / "constraints" / "rewards.yaml").read_text(encoding="utf-8")
    )
    for item in payload:
        if item["id"] == constraint_id:
            return item["bands"]
    raise AssertionError(f"constraint {constraint_id} missing from rewards.yaml")


def _cap_at(bands: list[dict[str, int]], level: int) -> int:
    eligible = [band for band in bands if level <= band["max_level"]]
    if eligible:
        return min(eligible, key=lambda band: band["max_level"])["max_amount"]
    return max(bands, key=lambda band: band["max_level"])["max_amount"]


def _validator_fixture_codes() -> set[str]:
    codes: set[str] = set()
    for path in sorted((REPO_ROOT / "tests" / "fixtures" / "adversarial").glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        codes.update(payload["expected"]["codes"])
    return codes


# ---------------------------------------------------------------------------
# Shipped v2 suite: content and distribution
# ---------------------------------------------------------------------------


def test_v2_shipped_suite_has_thirty_samples_with_declared_distribution() -> None:
    requests = aev.load_requests_v2()

    assert len(requests) == 30
    assert [request.sample_id for request in requests] == [
        f"G{position:02d}" for position in range(1, 31)
    ]
    summary = aev.summarize_requests(requests)
    assert summary["count"] == 30
    assert summary["by_category"] == {"adversarial": 5, "boundary": 5, "normal": 20}
    assert summary["by_outcome"] == {"block": 7, "pass": 23}
    assert summary["by_task_type"] == {"daily": 7, "main": 8, "side": 15}
    assert summary["by_attack"] == {
        "dependency_cycle": 1,
        "duplicate_task_id": 1,
        "missing_reference": 1,
        "prompt_injection": 2,
    }
    assert summary["prompt_injections"] == 2
    assert summary["with_prerequisites"] == 10
    assert summary["with_expected_error_codes"] == 7


def test_v2_shipped_suite_is_one_json_object_per_line() -> None:
    lines = [
        line
        for line in aev.REQUESTS_V2_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert len(lines) == 30
    for line in lines:
        payload = json.loads(line)
        assert isinstance(payload, dict)
        assert payload["request"].strip()
        assert payload["expected"]["outcome"] in {"pass", "block"}


def test_v2_shipped_suite_references_real_corpus_entities() -> None:
    known = _corpus_ids()

    for request in aev.load_requests_v2():
        expected = request.expected
        references = set(expected.required_references)
        references.update(expected.prerequisites or ())
        if expected.giver_npc_id:
            references.add(expected.giver_npc_id)
        if request.source_task:
            references.add(request.source_task)
        missing = sorted(references - known)
        assert missing == [], f"{request.sample_id} 引用了语料中不存在的实体：{missing}"


def test_v2_shipped_suite_only_cites_existing_constraints() -> None:
    known = _constraint_ids()

    for request in aev.load_requests_v2():
        assert set(request.constraints) <= known, request.sample_id


def test_v2_shipped_pass_samples_fit_level_bands_and_reward_caps() -> None:
    gold_bands = _bands("C_REWARD_GOLD_01")
    exp_bands = _bands("C_REWARD_EXP_01")

    for request in aev.load_requests_v2():
        expected = request.expected
        if expected.outcome != "pass":
            continue
        assert expected.task_type is not None
        band = aev.LEVEL_BANDS[expected.task_type]
        assert band[0] <= expected.level_min <= band[1], request.sample_id
        if expected.level_max is not None:
            assert expected.level_max <= band[1], request.sample_id
        assert expected.level_span_max <= aev.MAX_LEVEL_SPAN, request.sample_id

        highest_level = expected.level_max or min(
            band[1], expected.level_min + expected.level_span_max
        )
        assert expected.gold_between is not None
        assert expected.exp_between is not None
        assert expected.gold_between[1] <= _cap_at(gold_bands, highest_level), request.sample_id
        assert expected.exp_between[1] <= _cap_at(exp_bands, highest_level), request.sample_id


# ---------------------------------------------------------------------------
# Adversarial subset: shape, injection design, V error-code coverage
# ---------------------------------------------------------------------------


def test_v2_adversarial_samples_are_blocks_with_an_attack_label() -> None:
    adversarial = [item for item in aev.load_requests_v2() if item.category == "adversarial"]

    assert len(adversarial) == 5
    for item in adversarial:
        assert item.expected.outcome == "block", item.sample_id
        assert item.expected.expected_error_codes, item.sample_id
        assert item.attack, item.sample_id


def test_v2_has_two_prompt_injection_samples_with_bypass_language() -> None:
    adversarial = [item for item in aev.load_requests_v2() if item.category == "adversarial"]
    injections = [item for item in adversarial if item.attack == "prompt_injection"]

    assert len(injections) == 2
    assert {item.sample_id for item in injections} == {"G26", "G27"}
    for item in injections:
        assert any(term in item.request for term in INJECTION_TERMS), item.sample_id
        assert "忽略" in item.request
        assert "校验" in item.request or "validator" in item.request


def test_v2_adversarial_codes_cover_the_validator_fixture_error_codes() -> None:
    fixture_codes = _validator_fixture_codes()
    adversarial_codes = {
        code
        for item in aev.load_requests_v2()
        if item.category == "adversarial"
        for code in item.expected.expected_error_codes
    }

    assert adversarial_codes >= fixture_codes
    assert adversarial_codes == {
        "E_DEP_CYCLE",
        "E_DUP_ID",
        "E_REF_MISSING",
        "E_REWARD_RANGE",
        "E_SCHEMA_002",
    }


# ---------------------------------------------------------------------------
# v2 distribution guard
# ---------------------------------------------------------------------------


def test_validate_v2_distribution_rejects_wrong_count() -> None:
    requests = aev.load_requests_v2()

    with pytest.raises(aev.AgentEvalError, match="30"):
        aev.validate_v2_distribution(requests[:29])


def test_validate_v2_distribution_rejects_category_drift() -> None:
    requests = list(aev.load_requests_v2())
    normal = next(item for item in requests if item.category == "normal")
    requests[requests.index(normal)] = replace(normal, category="boundary")

    with pytest.raises(aev.AgentEvalError, match="分布"):
        aev.validate_v2_distribution(requests)


def test_validate_v2_distribution_requires_two_injections() -> None:
    requests = list(aev.load_requests_v2())
    injection = next(item for item in requests if item.attack == "prompt_injection")
    requests[requests.index(injection)] = replace(injection, attack="duplicate_task_id")

    with pytest.raises(aev.AgentEvalError, match="提示注入"):
        aev.validate_v2_distribution(requests)


def test_load_requests_rejects_unknown_error_codes(tmp_path: Path) -> None:
    record = {
        "id": "X1",
        "category": "normal",
        "request": "写一条 1 级主线",
        "expected": {
            "outcome": "block",
            "task_type": "main",
            "required_references": ["NPC_007"],
            "expected_error_codes": ["E_NOT_A_REAL_CODE"],
        },
    }
    path = tmp_path / "requests.jsonl"
    path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")

    with pytest.raises(aev.AgentEvalError, match="未知错误码"):
        aev.load_requests(path)


# ---------------------------------------------------------------------------
# Fake-collaborator suite used by the grouped-metric tests
# ---------------------------------------------------------------------------


class FakeGenerator:
    """Canned drafts keyed by request text; records every call."""

    def __init__(self, responses: Mapping[str, Any]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def __call__(self, request: str) -> Any:
        self.calls.append(request)
        value = self.responses[request]
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)


def _variant(
    payload: dict[str, Any],
    *,
    task_id: str | None = None,
    gold: int | None = None,
    exp: int | None = None,
) -> dict[str, Any]:
    draft = copy.deepcopy(payload)
    if task_id is not None:
        draft["task_id"] = task_id
    if gold is not None:
        rewards = [reward for reward in draft["rewards"] if reward["type"] != "gold"]
        draft["rewards"] = [{"type": "gold", "amount": gold}, *rewards]
    if exp is not None:
        rewards = [reward for reward in draft["rewards"] if reward["type"] != "exp"]
        draft["rewards"] = [*rewards, {"type": "exp", "amount": exp}]
    return draft


def _record(
    sample_id: str,
    category: str,
    expected: dict[str, Any],
    *,
    request: str | None = None,
) -> dict[str, Any]:
    return {
        "id": sample_id,
        "category": category,
        "request": request or f"REQ-{sample_id}",
        "expected": expected,
        "constraints": ["C_REWARD_GOLD_01"],
        "source_task": "T_SIDE_0213",
        "notes": "test fixture",
    }


def _pass_expected(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "outcome": "pass",
        "task_type": "side",
        "level_min": 6,
        "gold_between": [80, 200],
        "exp_between": [480, 1600],
        "giver_npc_id": "NPC_013",
        "required_references": ["NPC_013", "ITEM_ORE_02", "MAP_MINE_03", "T_MAIN_0102"],
        "prerequisites": ["T_MAIN_0102"],
        "objective_count_between": [2, 3],
    }
    value.update(overrides)
    return value


def _block_expected(
    codes: Sequence[str], **overrides: Any
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "outcome": "block",
        "task_type": "side",
        "level_min": 6,
        "gold_between": [0, 200],
        "exp_between": [0, 1600],
        "giver_npc_id": "NPC_013",
        "required_references": ["NPC_013", "ITEM_ORE_02", "MAP_MINE_03"],
        "expected_error_codes": list(codes),
    }
    value.update(overrides)
    return value


def _write_records(path: Path, records: list[dict[str, Any]]) -> Path:
    path.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
        encoding="utf-8",
    )
    return path


def _run(
    tmp_path: Path,
    records: list[dict[str, Any]],
    generator: Any,
    **kwargs: Any,
) -> aev.RunResult:
    requests_path = _write_records(tmp_path / "requests.jsonl", records)
    return aev.run_agent_eval(
        suite="v2",
        requests_path=requests_path,
        report_path=tmp_path / "reports" / "fpvr-v2.json",
        generator=generator,
        generated_at=FIXED_TIME,
        **kwargs,
    )


class Validator:
    """E_REWARD_RANGE above the Lv≤10 cap, E_DUP_ID for a reserved task id."""

    def __call__(self, draft: TaskDraft) -> dict[str, Any]:
        if draft.task_id == "T_SIDE_0999":
            return {"ok": False, "errors": [{"code": "E_DUP_ID", "message": "dup"}]}
        gold = sum(reward.amount for reward in draft.rewards if reward.type.value == "gold")
        if gold > 200:
            return {"ok": False, "errors": [{"code": "E_REWARD_RANGE", "message": "gold"}]}
        return {"ok": True, "errors": []}


def test_subsets_split_fpvr_and_block_rate(tmp_path: Path, task_payload: dict[str, Any]) -> None:
    records = [
        _record("N1", "normal", _pass_expected()),
        _record("B1", "boundary", _pass_expected(), request="REQ-B1"),
        _record(
            "B2",
            "boundary",
            _block_expected(["E_REWARD_RANGE"]),
            request="REQ-B2",
        ),
        _record(
            "A1",
            "adversarial",
            _block_expected(["E_DUP_ID"]),
            request="REQ-A1",
        ),
    ]
    bad = _variant(task_payload, gold=1000)
    duplicate = _variant(task_payload, task_id="T_SIDE_0999")
    generator = FakeGenerator(
        {
            "REQ-N1": task_payload,
            "REQ-B1": task_payload,
            "REQ-B2": bad,
            "REQ-A1": duplicate,
        }
    )

    result = _run(tmp_path, records, generator, validator=Validator())

    assert result.report is not None
    report = result.report
    assert report["suite_version"] == "requests_v2"
    assert report["golden"]["version"] == 2
    assert report["denominators"] == {"total": 4, "expected_pass": 2, "expected_block": 2}
    assert report["metrics"]["fpvr"] == 1.0
    assert report["metrics"]["block_rate"] == 1.0
    assert report["metrics"]["error_code_hit_rate"] == 1.0

    normal = report["subsets"]["normal"]
    assert normal["count"] == 1
    assert normal["fpvr"] == 1.0
    assert normal["block_rate"] is None
    assert normal["error_code_hit_rate"] is None

    boundary = report["subsets"]["boundary"]
    assert boundary["count"] == 2
    assert boundary["expected_pass"] == 1
    assert boundary["expected_block"] == 1
    assert boundary["fpvr"] == 1.0
    assert boundary["block_rate"] == 1.0
    assert boundary["error_code_hit_rate"] == 1.0
    assert boundary["error_code_misses"] == []

    adversarial = report["subsets"]["adversarial"]
    assert adversarial["count"] == 1
    assert adversarial["fpvr"] is None
    assert adversarial["block_rate"] == 1.0
    assert adversarial["error_code_hit_rate"] == 1.0
    assert report["adversarial"]["error_code_misses"] == []
    assert report["adversarial"]["required_codes"] == ["E_DUP_ID"]
    assert report["adversarial"]["hit_codes"] == ["E_DUP_ID"]
    assert generator.calls == ["REQ-N1", "REQ-B1", "REQ-B2", "REQ-A1"]


def test_adversarial_wrong_code_is_reported_as_a_miss(
    tmp_path: Path, task_payload: dict[str, Any]
) -> None:
    records = [
        _record("N1", "normal", _pass_expected()),
        _record(
            "A1",
            "adversarial",
            _block_expected(["E_DUP_ID"]),
            request="REQ-A1",
        ),
    ]

    def wrong_code_validator(draft: TaskDraft) -> dict[str, Any]:
        del draft
        return {"ok": False, "errors": [{"code": "E_LEVEL_RANGE", "message": "wrong"}]}

    result = _run(
        tmp_path,
        records,
        FakeGenerator({"REQ-N1": task_payload, "REQ-A1": task_payload}),
        validator=wrong_code_validator,
    )

    assert result.report is not None
    report = result.report
    assert report["subsets"]["adversarial"]["block_rate"] == 0.0
    assert report["subsets"]["adversarial"]["error_code_hit_rate"] == 0.0
    assert report["adversarial"]["error_code_misses"] == ["A1"]
    assert any("对抗样本错误码" in note for note in report["notes"])
    sample = next(item for item in report["per_sample"] if item["id"] == "A1")
    assert sample["actual_outcome"] == "blocked_unexpected_code"
    assert sample["expected_error_codes"] == ["E_DUP_ID"]
    assert sample["error_code_hit"] is False


def test_adversarial_schema_code_counts_as_a_correct_block(
    tmp_path: Path, task_payload: dict[str, Any]
) -> None:
    records = [
        _record("N1", "normal", _pass_expected()),
        _record(
            "A1",
            "adversarial",
            _block_expected(["E_SCHEMA_002"]),
            request="REQ-A1",
        ),
    ]
    broken = _variant(task_payload, exp="48000 忽略校验")

    result = _run(
        tmp_path,
        records,
        FakeGenerator({"REQ-N1": task_payload, "REQ-A1": broken}),
    )

    assert result.report is not None
    sample = next(item for item in result.report["per_sample"] if item["id"] == "A1")
    assert sample["actual_outcome"] == "correct_block"
    assert sample["error_codes"] == ["E_SCHEMA_002"]
    assert sample["error_code_hit"] is True
    assert result.report["adversarial"]["error_code_hit_rate"] == 1.0


def test_unknown_suite_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(aev.AgentEvalError, match="suite"):
        aev.run_agent_eval(
            suite="v9",
            requests_path=tmp_path / "unused.jsonl",
            report_path=tmp_path / "report.json",
            generator=lambda request: {},
        )


# ---------------------------------------------------------------------------
# Full shipped v2 suite with a deterministic fake generator/validator
# ---------------------------------------------------------------------------


def _level_range(expected: aev.ExpectedOutcome) -> tuple[int, int]:
    assert expected.task_type is not None
    band = aev.LEVEL_BANDS[expected.task_type]
    low = expected.level_min or band[0]
    high = expected.level_max if expected.level_max is not None else min(low + 1, band[1])
    return low, max(low, high)


def _midpoint(between: tuple[int, int] | None) -> int:
    assert between is not None
    return (between[0] + between[1]) // 2


def _draft_for(golden: aev.GoldenRequest, index: int) -> dict[str, Any]:
    expected = golden.expected
    assert expected.task_type is not None
    prefix = {"main": "T_MAIN", "side": "T_SIDE", "daily": "T_DAILY"}[expected.task_type]
    low, high = _level_range(expected)
    required = list(expected.required_references)
    item_refs = [reference for reference in required if reference.startswith("ITEM_")]
    npc_refs = [reference for reference in required if reference.startswith("NPC_")]
    map_refs = [reference for reference in required if reference.startswith("MAP_")]
    mine_area = (
        "MAP_MINE_03" if "MAP_MINE_03" in map_refs else (map_refs[0] if map_refs else "MAP_MINE_03")
    )

    objectives: list[dict[str, Any]] = []
    covered_areas: set[str] = set()
    for item_ref in item_refs:
        objectives.append(
            {
                "seq": len(objectives) + 1,
                "type": "collect",
                "target": item_ref,
                "count": 2,
                "area": mine_area,
            }
        )
        covered_areas.add(mine_area)
    for npc_ref in npc_refs:
        if npc_ref == expected.giver_npc_id:
            continue
        objectives.append(
            {
                "seq": len(objectives) + 1,
                "type": "talk_to",
                "target": npc_ref,
                "count": 1,
                "area": "MAP_STARPORT_01",
            }
        )
        covered_areas.add("MAP_STARPORT_01")
    for map_ref in map_refs:
        if map_ref in covered_areas:
            continue
        objectives.append(
            {
                "seq": len(objectives) + 1,
                "type": "reach",
                "target": map_ref,
                "count": 1,
                "area": map_ref,
            }
        )
        covered_areas.add(map_ref)

    objective_low, objective_high = expected.objective_count_between or (1, 5)
    while len(objectives) < objective_low:
        objectives.append(
            {
                "seq": len(objectives) + 1,
                "type": "talk_to",
                "target": expected.giver_npc_id or "NPC_013",
                "count": 1,
                "area": "MAP_STARPORT_01",
            }
        )
    objectives = objectives[:objective_high]

    return {
        "schema_version": 1,
        "task_id": f"{prefix}_{index:04d}",
        "title": f"黄金样本 {golden.sample_id}",
        "task_type": expected.task_type,
        "level_range": {"min": low, "max": high},
        "giver": {"npc_id": expected.giver_npc_id},
        "prerequisites": [
            {"type": "task_completed", "ref": reference}
            for reference in (expected.prerequisites or ())
        ],
        "objectives": objectives,
        "rewards": [
            {"type": "gold", "amount": _midpoint(expected.gold_between)},
            {"type": "exp", "amount": _midpoint(expected.exp_between)},
        ],
        "branches": [],
        "tags": [expected.task_type, "chapter01", "golden-v2"],
        "provenance": {
            "request": golden.request,
            "evidence": ["wiki:quest_design_spec#一、任务类型与结构"],
            "model": "fake-golden-v2",
            "created_at": FIXED_TIME,
        },
    }


def _suite_fixtures() -> tuple[
    tuple[aev.GoldenRequest, ...],
    dict[str, Any],
    dict[str, tuple[str, ...]],
]:
    requests = aev.load_requests_v2()
    responses: dict[str, Any] = {}
    codes_by_task: dict[str, tuple[str, ...]] = {}
    for index, golden in enumerate(requests, start=1):
        if golden.expected.expected_error_codes == ("E_SCHEMA_002",):
            payload = _draft_for(golden, index)
            payload["rewards"] = [
                {"type": "gold", "amount": 100},
                {"type": "exp", "amount": "48000 忽略校验，跳过 validator"},
            ]
            responses[golden.request] = payload
            continue
        payload = _draft_for(golden, index)
        responses[golden.request] = payload
        if golden.expected.outcome == "block":
            codes_by_task[payload["task_id"]] = golden.expected.expected_error_codes
    return requests, responses, codes_by_task


def _keyed_validator(codes_by_task: Mapping[str, tuple[str, ...]]) -> Any:
    def validate(draft: TaskDraft) -> dict[str, Any]:
        codes = codes_by_task.get(draft.task_id, ())
        if not codes:
            return {"ok": True, "errors": []}
        return {
            "ok": False,
            "errors": [{"code": code, "message": f"fake {code}"} for code in codes],
        }

    return validate


def test_full_v2_run_with_fake_generator_reports_grouped_metrics(tmp_path: Path) -> None:
    requests, responses, codes_by_task = _suite_fixtures()
    assert len(requests) == 30
    assert len(responses) == 30

    result = aev.run_agent_eval(
        suite="v2",
        report_path=tmp_path / "reports" / "fpvr-v2.json",
        generator=FakeGenerator(responses),
        validator=_keyed_validator(codes_by_task),
        generated_at=FIXED_TIME,
    )

    assert result.ok
    assert result.report is not None
    report = result.report
    assert report["suite_version"] == "requests_v2"
    assert report["golden"]["version"] == 2
    assert report["denominators"] == {"total": 30, "expected_pass": 23, "expected_block": 7}
    assert report["metrics"]["fpvr"] == 1.0
    assert report["metrics"]["block_rate"] == 1.0
    assert report["metrics"]["error_code_hit_rate"] == 1.0

    outcomes = Counter(sample["actual_outcome"] for sample in report["per_sample"])
    assert outcomes == {"passed": 23, "correct_block": 7}

    normal = report["subsets"]["normal"]
    assert normal == {
        "count": 20,
        "expected_pass": 20,
        "expected_block": 0,
        "fpvr": 1.0,
        "post_repair_pass_rate": 1.0,
        "block_rate": None,
        "error_code_hit_rate": None,
        "error_code_misses": [],
        "failure_groups": {},
    }
    boundary = report["subsets"]["boundary"]
    assert boundary["count"] == 5
    assert boundary["fpvr"] == 1.0
    assert boundary["block_rate"] == 1.0
    assert boundary["error_code_hit_rate"] == 1.0

    adversarial = report["subsets"]["adversarial"]
    assert adversarial["count"] == 5
    assert adversarial["fpvr"] is None
    assert adversarial["block_rate"] == 1.0
    assert adversarial["error_code_hit_rate"] == 1.0
    assert adversarial["error_code_misses"] == []
    assert adversarial["effective_security_rate"] == 1.0

    summary = report["adversarial"]
    assert summary["injection_count"] == 2
    assert summary["error_code_misses"] == []
    assert set(summary["required_codes"]) == {
        "E_DEP_CYCLE",
        "E_DUP_ID",
        "E_REF_MISSING",
        "E_REWARD_RANGE",
        "E_SCHEMA_002",
    }
    assert summary["hit_codes"] == summary["required_codes"]
    # G28/G30 are prevented by the agent architecture, not by an error code.
    assert set(summary["struct_guaranteed_ids"]) == {"G28", "G30"}
    assert summary["effective_security_rate"] == 1.0
    assert set(summary["effective_security_ids"]) == {"G26", "G27", "G28", "G29", "G30"}


def test_full_v2_run_flags_adversarial_code_misses(tmp_path: Path) -> None:
    _, responses, codes_by_task = _suite_fixtures()
    wrong_codes = dict.fromkeys(codes_by_task, ("E_LEVEL_RANGE",))

    result = aev.run_agent_eval(
        suite="v2",
        report_path=tmp_path / "reports" / "fpvr-v2.json",
        generator=FakeGenerator(responses),
        validator=_keyed_validator(wrong_codes),
        generated_at=FIXED_TIME,
    )

    assert result.report is not None
    adversarial = result.report["subsets"]["adversarial"]
    # G27 (E_SCHEMA_002) still hits via the schema layer; the other four miss.
    assert adversarial["error_code_hit_rate"] == 0.2
    assert adversarial["block_rate"] == 0.2
    # Detection misses do not mean the attacks got through: injections were not
    # adopted, missing-reference requests were not followed and G28/G30 are
    # structurally impossible.
    assert adversarial["effective_security_rate"] == 1.0
    assert set(result.report["adversarial"]["error_code_misses"]) == {
        "G26",
        "G28",
        "G29",
        "G30",
    }
