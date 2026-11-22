"""Unit tests for the S3-05 agent golden set and FPVR harness (fake generator only).

Every number asserted here comes from a canned generator/validator/repairer, so
the suite needs no index, no LLM and no API key.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from gameconfig_copilot.evaluation import agent_eval as aev
from gameconfig_copilot.schemas.report import (
    ErrorCode,
    ValidationErrorItem,
    ValidationReport,
)
from gameconfig_copilot.schemas.task import TaskDraft

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_REQUEST = "给老陈加一条 6 级支线，去矿洞捡 4 块铁尘矿石，前置主线 0102。"
FIXED_TIME = "2026-11-15T12:00:00+08:00"

ENTITY_ID_RE = re.compile(r"\b(?:NPC|MAP|ITEM|DLG|FACTION)_[A-Z0-9_]+\b")
TASK_ID_RE = re.compile(r"\bT_(?:MAIN|SIDE|DAILY)_\d{4}\b")


# ---------------------------------------------------------------------------
# Fixtures / fake collaborators
# ---------------------------------------------------------------------------


def _expected(**overrides: Any) -> dict[str, Any]:
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


def _expected_obj(**overrides: Any) -> aev.ExpectedOutcome:
    value: dict[str, Any] = {
        "outcome": "pass",
        "task_type": "side",
        "level_min": 6,
        "gold_between": (80, 200),
        "exp_between": (480, 1600),
        "giver_npc_id": "NPC_013",
        "required_references": ("NPC_013", "ITEM_ORE_02", "MAP_MINE_03", "T_MAIN_0102"),
        "prerequisites": ("T_MAIN_0102",),
        "objective_count_between": (2, 3),
    }
    value.update(overrides)
    return aev.ExpectedOutcome(**value)


def _record(
    sample_id: str = "G01",
    *,
    request: str = BASE_REQUEST,
    expected: dict[str, Any] | None = None,
    category: str = "normal",
) -> dict[str, Any]:
    return {
        "id": sample_id,
        "category": category,
        "request": request,
        "expected": expected or _expected(),
        "constraints": ["C_REWARD_GOLD_01"],
        "source_task": "T_SIDE_0213",
        "notes": "test fixture",
    }


def _write_records(path: Path, records: list[dict[str, Any]]) -> Path:
    path.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
        encoding="utf-8",
    )
    return path


def _record_with_duplicate_id() -> list[dict[str, Any]]:
    return [_record("G01"), _record("G01")]


def _variant(
    payload: dict[str, Any],
    *,
    task_type: str | None = None,
    level_range: tuple[int, int] | None = None,
    gold: int | None = None,
    exp: int | None = None,
    npc: str | None = None,
    prerequisites: list[str] | None = None,
    objective_count: int | None = None,
    drop: tuple[str, ...] = (),
) -> dict[str, Any]:
    draft = copy.deepcopy(payload)
    for key in drop:
        draft.pop(key, None)
    if task_type is not None:
        draft["task_type"] = task_type
        tags = draft.get("tags")
        if isinstance(tags, list) and tags:
            tags[0] = task_type
    if level_range is not None:
        draft["level_range"] = {"min": level_range[0], "max": level_range[1]}
    if gold is not None:
        rewards = [reward for reward in draft["rewards"] if reward["type"] != "gold"]
        draft["rewards"] = [{"type": "gold", "amount": gold}, *rewards]
    if exp is not None:
        rewards = [reward for reward in draft["rewards"] if reward["type"] != "exp"]
        draft["rewards"] = [*rewards, {"type": "exp", "amount": exp}]
    if npc is not None:
        draft["giver"]["npc_id"] = npc
    if prerequisites is not None:
        draft["prerequisites"] = [
            {"type": "task_completed", "ref": reference} for reference in prerequisites
        ]
    if objective_count is not None:
        objectives = draft["objectives"][:objective_count]
        while len(objectives) < objective_count:
            objectives.append(
                {
                    "seq": len(objectives) + 1,
                    "type": "talk_to",
                    "target": "NPC_013",
                    "count": 1,
                    "area": "MAP_STARPORT_01",
                }
            )
        draft["objectives"] = objectives
    return draft


class FakeGenerator:
    """Canned drafts keyed by request text; records every call."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def __call__(self, request: str) -> Any:
        self.calls.append(request)
        value = self.responses[request]
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)


class CappedGoldValidator:
    """Tiny C_REWARD_GOLD_01 stand-in: reject gold above the level-band cap."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, draft: TaskDraft) -> dict[str, Any]:
        self.calls.append(draft.task_id)
        cap = 200 if draft.level_range.min <= 10 else 500
        gold = sum(reward.amount for reward in draft.rewards if reward.type.value == "gold")
        if gold > cap:
            return {
                "ok": False,
                "errors": [
                    {
                        "code": "E_REWARD_RANGE",
                        "field": "rewards[0].amount",
                        "message": f"gold={gold} 超过 Lv≤10 上限 {cap}",
                        "constraint_ref": "C_REWARD_GOLD_01",
                    }
                ],
            }
        return {"ok": True, "errors": []}


class WrongCodeValidator:
    """Always rejects with E_LEVEL_RANGE so block-code matching can be tested."""

    def __call__(self, draft: TaskDraft) -> dict[str, Any]:
        return {"ok": False, "errors": [{"code": "E_LEVEL_RANGE", "message": "wrong code"}]}


def _run(
    tmp_path: Path,
    records: list[dict[str, Any]],
    generator: Any,
    **kwargs: Any,
) -> aev.RunResult:
    requests_path = _write_records(tmp_path / "requests.jsonl", records)
    report_path = tmp_path / "reports" / "fpvr-baseline.json"
    return aev.run_agent_eval(
        requests_path=requests_path,
        report_path=report_path,
        generator=generator,
        generated_at=FIXED_TIME,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Shipped golden set: data/golden/requests_v1.jsonl
# ---------------------------------------------------------------------------


def test_shipped_requests_have_ten_samples_with_declared_distribution() -> None:
    requests = aev.load_requests()

    assert [request.sample_id for request in requests] == [
        f"G{position:02d}" for position in range(1, 11)
    ]
    summary = aev.summarize_requests(requests)
    assert summary["count"] == 10
    assert summary["by_category"] == {"boundary": 3, "normal": 7}
    assert summary["by_outcome"] == {"block": 1, "pass": 9}
    assert summary["by_task_type"] == {"daily": 3, "main": 3, "side": 4}
    assert summary["with_prerequisites"] == 3
    assert summary["with_expected_error_codes"] == 1


def test_shipped_requests_are_one_json_object_per_line() -> None:
    lines = [
        line
        for line in aev.REQUESTS_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert len(lines) == 10
    for line in lines:
        payload = json.loads(line)
        assert isinstance(payload, dict)
        assert payload["request"].strip()


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


def test_shipped_requests_reference_real_corpus_entities() -> None:
    known = _corpus_ids()

    for request in aev.load_requests():
        expected = request.expected
        references = set(expected.required_references)
        references.update(expected.prerequisites or ())
        if expected.giver_npc_id:
            references.add(expected.giver_npc_id)
        if request.source_task:
            references.add(request.source_task)
        missing = sorted(references - known)
        assert missing == [], f"{request.sample_id} 引用了语料中不存在的实体：{missing}"


def _constraint_ids() -> set[str]:
    ids: set[str] = set()
    for path in (REPO_ROOT / "data" / "constraints").glob("*.yaml"):
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        for item in payload:
            ids.add(item["id"])
    return ids


def test_shipped_requests_only_cite_existing_constraints() -> None:
    known = _constraint_ids()

    for request in aev.load_requests():
        assert set(request.constraints) <= known, request.sample_id


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


def test_shipped_pass_requests_fit_level_bands_and_reward_caps() -> None:
    gold_bands = _bands("C_REWARD_GOLD_01")
    exp_bands = _bands("C_REWARD_EXP_01")

    for request in aev.load_requests():
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


def test_shipped_block_sample_names_the_expected_error_code() -> None:
    blocks = [request for request in aev.load_requests() if request.expected.outcome == "block"]

    assert len(blocks) == 1
    block = blocks[0]
    assert block.expected.expected_error_codes == ("E_REWARD_RANGE",)
    assert block.expected.gold_between == (0, 200)
    assert block.expected.exp_between == (0, 1600)


# ---------------------------------------------------------------------------
# Golden-set loading / validation
# ---------------------------------------------------------------------------


def test_load_requests_missing_file_names_the_path(tmp_path: Path) -> None:
    with pytest.raises(aev.AgentEvalError, match="不存在"):
        aev.load_requests(tmp_path / "nope.jsonl")


def test_load_requests_names_the_bad_json_line(tmp_path: Path) -> None:
    path = tmp_path / "requests.jsonl"
    path.write_text(
        json.dumps(_record("G01"), ensure_ascii=False) + "\n{broken\n",
        encoding="utf-8",
    )

    with pytest.raises(aev.AgentEvalError, match="第 2 行"):
        aev.load_requests(path)


def test_load_requests_rejects_duplicate_ids(tmp_path: Path) -> None:
    path = _write_records(tmp_path / "requests.jsonl", _record_with_duplicate_id())

    with pytest.raises(aev.AgentEvalError, match="id 重复"):
        aev.load_requests(path)


def test_load_requests_rejects_unknown_outcome(tmp_path: Path) -> None:
    path = _write_records(
        tmp_path / "requests.jsonl",
        [_record("G01", expected=_expected(outcome="maybe"))],
    )

    with pytest.raises(aev.AgentEvalError, match="outcome"):
        aev.load_requests(path)


def test_load_requests_requires_task_type_for_pass_samples(tmp_path: Path) -> None:
    expected = _expected()
    expected.pop("task_type")
    path = _write_records(tmp_path / "requests.jsonl", [_record("G01", expected=expected)])

    with pytest.raises(aev.AgentEvalError, match="task_type"):
        aev.load_requests(path)


def test_load_requests_requires_error_codes_for_block_samples(tmp_path: Path) -> None:
    expected = {"outcome": "block", "required_references": ["NPC_013"]}
    path = _write_records(tmp_path / "requests.jsonl", [_record("G01", expected=expected)])

    with pytest.raises(aev.AgentEvalError, match="expected_error_codes"):
        aev.load_requests(path)


def test_load_requests_rejects_inverted_between(tmp_path: Path) -> None:
    path = _write_records(
        tmp_path / "requests.jsonl",
        [_record("G01", expected=_expected(gold_between=[200, 80]))],
    )

    with pytest.raises(aev.AgentEvalError, match="下限"):
        aev.load_requests(path)


def test_load_requests_rejects_level_span_above_six(tmp_path: Path) -> None:
    path = _write_records(
        tmp_path / "requests.jsonl",
        [_record("G01", expected=_expected(level_span_max=7))],
    )

    with pytest.raises(aev.AgentEvalError, match="level_span_max"):
        aev.load_requests(path)


def test_load_requests_rejects_level_outside_type_band(tmp_path: Path) -> None:
    path = _write_records(
        tmp_path / "requests.jsonl",
        [_record("G01", expected=_expected(level_min=21))],
    )

    with pytest.raises(aev.AgentEvalError, match="等级段"):
        aev.load_requests(path)


def test_load_requests_rejects_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "requests.jsonl"
    path.write_text("\n", encoding="utf-8")

    with pytest.raises(aev.AgentEvalError, match="没有有效需求行"):
        aev.load_requests(path)


# ---------------------------------------------------------------------------
# Field assertions
# ---------------------------------------------------------------------------


def test_check_assertions_accepts_the_reference_draft(task_payload: dict[str, Any]) -> None:
    draft = TaskDraft.model_validate(task_payload)

    assert aev.check_assertions(draft, _expected_obj()) == ()


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"task_type": "main"}, "task_type"),
        ({"level_span_max": 2}, "跨度"),
        ({"level_band": (10, 20)}, "超出等级段"),
        ({"gold_between": (80, 80)}, "gold"),
        ({"exp_between": (480, 480)}, "exp"),
        ({"required_references": ("MAP_LOST_09",)}, "缺少引用"),
    ],
)
def test_check_assertions_flags_each_executable_kind(
    task_payload: dict[str, Any], overrides: dict[str, Any], fragment: str
) -> None:
    draft = TaskDraft.model_validate(task_payload)

    failures = aev.check_assertions(draft, _expected_obj(**overrides))

    assert any(fragment in failure for failure in failures), failures


def test_non_executable_assertions_are_advisory_only(
    task_payload: dict[str, Any],
) -> None:
    """Exact level bounds, giver, prerequisite set, counts and NPC/ITEM refs don't fail."""

    draft = TaskDraft.model_validate(task_payload)
    expected = _expected_obj(
        level_min=7,
        level_max=12,
        giver_npc_id="NPC_034",
        prerequisites=("T_MAIN_0101",),
        objective_count_between=(3, 5),
        required_references=("NPC_034",),
    )

    assert aev.check_assertions(draft, expected) == ()
    advisory = aev.check_advisory_assertions(draft, expected)
    assert any("level_range.min" in note for note in advisory)
    assert any("giver.npc_id" in note for note in advisory)
    assert any("prerequisites" in note for note in advisory)
    assert any("objectives" in note for note in advisory)
    assert any("NPC/ITEM" in note for note in advisory)
    assert not any("低于建议下限" in note for note in advisory)


def test_reward_floors_are_hard_assertions(
    task_payload: dict[str, Any],
) -> None:
    """Below-min rewards fail with the MIN constraint ref (not an advisory note)."""

    draft = TaskDraft.model_validate(task_payload)
    expected = _expected_obj(gold_between=(500, 500), exp_between=(4000, 4000))

    failures = aev.check_assertion_failures(draft, expected)

    assert {failure.field for failure in failures} == {"rewards.gold", "rewards.exp"}
    assert {failure.constraint_ref for failure in failures} == {
        "C_REWARD_GOLD_MIN_01",
        "C_REWARD_EXP_MIN_01",
    }
    assert {failure.expected for failure in failures} == {"[500, 500]", "[4000, 4000]"}
    advisory = aev.check_advisory_assertions(draft, expected)
    assert not any("低于建议下限" in note for note in advisory)


def test_assertion_failures_are_structured_for_repair(
    task_payload: dict[str, Any],
) -> None:
    draft = TaskDraft.model_validate(task_payload)

    failures = aev.check_assertion_failures(
        draft, _expected_obj(required_references=("MAP_LOST_09",))
    )

    assert len(failures) == 1
    failure = failures[0]
    assert failure.field == "references"
    assert failure.expected == ["MAP_LOST_09"]
    assert "MAP_LOST_09" in failure.suggestion
    error = failure.as_error()
    assert error["code"] == "E_GOLDEN_ASSERTION"
    assert error["severity"] == "error"
    assert error["field"] == "references"
    assert {"expected", "actual", "suggestion"} <= set(error)


def test_map_reference_must_be_in_the_validator_read_area(
    task_payload: dict[str, Any],
) -> None:
    """A MAP mentioned only in objective.target is not validator-executable."""

    payload = copy.deepcopy(task_payload)
    payload["objectives"][1]["target"] = "MAP_WASTELAND_02"
    draft = TaskDraft.model_validate(payload)

    failures = aev.check_assertion_failures(
        draft, _expected_obj(required_references=("MAP_WASTELAND_02",))
    )

    assert len(failures) == 1
    assert "MAP_WASTELAND_02" in failures[0].message
    assert failures[0].field == "references"


def test_repair_context_carries_structured_assertion_errors(
    task_payload: dict[str, Any],
) -> None:
    golden = aev.load_requests()[0]
    wrong_type = _variant(task_payload, task_type="main")
    contexts: list[dict[str, Any]] = []

    def repair(
        golden_item: aev.GoldenRequest, draft: Any, report: dict[str, Any]
    ) -> dict[str, Any]:
        contexts.append(report)
        return copy.deepcopy(task_payload)

    sample = aev.evaluate_sample(
        golden,
        lambda request: wrong_type,
        repairer=repair,
    )

    assert sample.actual_outcome == "passed_after_repair"
    assert contexts[0]["stage"] == "assertions"
    error = contexts[0]["errors"][0]
    assert error["field"] == "task_type"
    assert error["expected"] == "side"
    assert error["actual"] == "main"
    assert error["severity"] == "error"
    assert contexts[0]["assertion_failures"][0]["field"] == "task_type"
    assert all(error_item["field"] != "draft" for error_item in contexts[0]["errors"])


def test_report_records_assertion_policy_and_advisory_notes(
    tmp_path: Path, task_payload: dict[str, Any]
) -> None:
    advisory_only = _variant(task_payload, npc="NPC_034")

    result = _run(
        tmp_path,
        [_record("G01")],
        FakeGenerator({BASE_REQUEST: advisory_only}),
    )

    assert result.report is not None
    report = result.report
    assert report["metrics"]["fpvr"] == 1.0
    assert report["assertion_policy"]["pending_constraints"]
    assert report["assertion_policy"]["advisory"]
    sample = report["per_sample"][0]
    assert sample["failed_assertions"] == []
    assert any("giver.npc_id" in note for note in sample["advisory_assertions"])


def test_draft_references_covers_giver_objectives_rewards_and_prereqs(
    task_payload: dict[str, Any],
) -> None:
    draft = TaskDraft.model_validate(task_payload)

    references = aev.draft_references(draft)

    assert {"NPC_013", "ITEM_ORE_02", "MAP_MINE_03", "MAP_STARPORT_01", "T_MAIN_0102"} <= references


# ---------------------------------------------------------------------------
# FPVR + repair rounds (fake generator/validator/repairer)
# ---------------------------------------------------------------------------


def test_all_pass_fpvr_is_one_and_report_has_sample_detail(
    tmp_path: Path, task_payload: dict[str, Any]
) -> None:
    records = [_record("G01"), _record("G02", request="SECOND")]
    generator = FakeGenerator({BASE_REQUEST: task_payload, "SECOND": task_payload})

    result = _run(tmp_path, records, generator)

    assert result.ok
    assert result.report_path.is_file()
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert report["suite"] == "agent"
    assert report["suite_version"] == "requests_v1"
    assert report["mode"] == "schema_only"
    assert report["generated_at"] == FIXED_TIME
    assert report["denominators"] == {"total": 2, "expected_pass": 2, "expected_block": 0}
    assert report["metrics"]["fpvr"] == 1.0
    assert report["metrics"]["post_repair_pass_rate"] == 1.0
    assert report["metrics"]["block_rate"] is None
    assert report["metrics"]["first_attempt_schema_rate"] == 1.0
    assert report["repair"]["histogram"] == {"0": 2}
    assert report["repair"]["needed"] == 0
    assert len(report["per_sample"]) == 2
    assert report["per_sample"][0]["actual_outcome"] == "passed"
    assert report["per_sample"][0]["first_pass"] is True
    assert generator.calls == [BASE_REQUEST, "SECOND"]
    assert any("FPVR" in note for note in report["notes"])


def test_compute_fpvr_uses_first_pass_only(
    task_payload: dict[str, Any],
) -> None:
    golden = aev.load_requests()[0]
    passed = aev.evaluate_sample(golden, lambda request: task_payload)
    failed = aev.evaluate_sample(
        golden, lambda request: _variant(task_payload, task_type="main")
    )

    assert aev.compute_fpvr([passed, failed]) == 0.5


def test_generator_may_return_a_draft_result_object(
    tmp_path: Path, task_payload: dict[str, Any]
) -> None:
    result = _run(
        tmp_path,
        [_record("G01")],
        FakeGenerator({BASE_REQUEST: SimpleNamespace(draft=task_payload)}),
    )

    assert result.report is not None
    assert result.report["per_sample"][0]["actual_outcome"] == "passed"
    assert result.report["metrics"]["fpvr"] == 1.0


def test_repair_success_updates_fpvr_and_rounds(
    tmp_path: Path, task_payload: dict[str, Any]
) -> None:
    bad = _variant(task_payload, gold=1000)
    generator = FakeGenerator({BASE_REQUEST: bad})
    validator = CappedGoldValidator()
    contexts: list[dict[str, Any]] = []

    def repair(
        golden: aev.GoldenRequest, draft: Any, report: dict[str, Any]
    ) -> dict[str, Any]:
        contexts.append(report)
        return copy.deepcopy(task_payload)

    result = _run(
        tmp_path, [_record("G01")], generator, validator=validator, repairer=repair
    )

    assert result.report is not None
    metrics = result.report["metrics"]
    assert metrics["fpvr"] == 0.0
    assert metrics["post_repair_pass_rate"] == 1.0
    assert result.report["repair"] == {
        "needed": 1,
        "attempted": 1,
        "repaired": 1,
        "failed": 0,
        "mean_rounds": 1.0,
        "histogram": {"1": 1},
    }
    sample = result.report["per_sample"][0]
    assert sample["actual_outcome"] == "passed_after_repair"
    assert sample["repair_rounds"] == 1
    assert sample["schema_ok"] is True
    assert sample["error_codes"] == []
    assert contexts[0]["stage"] == "validation"
    assert contexts[0]["errors"][0]["code"] == "E_REWARD_RANGE"
    assert generator.calls == [BASE_REQUEST]


def test_repair_exhausted_records_two_rounds(
    tmp_path: Path, task_payload: dict[str, Any]
) -> None:
    bad = _variant(task_payload, gold=1000)
    contexts: list[dict[str, Any]] = []

    def repair(
        golden: aev.GoldenRequest, draft: Any, report: dict[str, Any]
    ) -> dict[str, Any]:
        contexts.append(report)
        return copy.deepcopy(bad)

    result = _run(
        tmp_path,
        [_record("G01")],
        FakeGenerator({BASE_REQUEST: bad}),
        validator=CappedGoldValidator(),
        repairer=repair,
    )

    assert result.report is not None
    metrics = result.report["metrics"]
    assert metrics["fpvr"] == 0.0
    assert metrics["post_repair_pass_rate"] == 0.0
    assert result.report["repair"]["needed"] == 1
    assert result.report["repair"]["attempted"] == 1
    assert result.report["repair"]["repaired"] == 0
    assert result.report["repair"]["failed"] == 1
    assert result.report["repair"]["mean_rounds"] == 2.0
    assert result.report["repair"]["histogram"] == {"2": 1}
    assert result.report["failure_groups"] == {"validation_error": 1}
    sample = result.report["per_sample"][0]
    assert sample["actual_outcome"] == "validation_error"
    assert sample["repair_rounds"] == 2
    assert sample["error_codes"] == ["E_REWARD_RANGE"]
    assert len(contexts) == 2


def test_zero_repair_rounds_skips_the_repairer(
    tmp_path: Path, task_payload: dict[str, Any]
) -> None:
    bad = _variant(task_payload, gold=1000)
    repair_calls: list[Any] = []

    def repair(
        golden: aev.GoldenRequest, draft: Any, report: dict[str, Any]
    ) -> dict[str, Any]:
        repair_calls.append(report)
        return copy.deepcopy(task_payload)

    result = _run(
        tmp_path,
        [_record("G01")],
        FakeGenerator({BASE_REQUEST: bad}),
        validator=CappedGoldValidator(),
        repairer=repair,
        max_repair_rounds=0,
    )

    assert result.report is not None
    sample = result.report["per_sample"][0]
    assert sample["actual_outcome"] == "validation_error"
    assert sample["repair_rounds"] == 0
    assert result.report["repair"]["attempted"] == 0
    assert repair_calls == []


def test_validator_may_return_a_validation_report_contract(
    tmp_path: Path, task_payload: dict[str, Any]
) -> None:
    def report_validator(draft: TaskDraft) -> ValidationReport:
        return ValidationReport(
            ok=False,
            errors=[
                ValidationErrorItem(
                    code=ErrorCode.REWARD_RANGE,
                    field="rewards[0].amount",
                    message="金币奖励越界",
                    constraint_ref="C_REWARD_GOLD_01",
                )
            ],
        )

    result = _run(
        tmp_path,
        [_record("G01")],
        FakeGenerator({BASE_REQUEST: task_payload}),
        validator=report_validator,
    )

    assert result.report is not None
    sample = result.report["per_sample"][0]
    assert sample["actual_outcome"] == "validation_error"
    assert sample["error_codes"] == ["E_REWARD_RANGE"]
    assert "金币奖励越界" in sample["message"]


def test_schema_failure_is_repaired_before_business_validation(
    tmp_path: Path, task_payload: dict[str, Any]
) -> None:
    broken = _variant(task_payload, drop=("title",))
    validator = CappedGoldValidator()
    contexts: list[dict[str, Any]] = []

    def repair(
        golden: aev.GoldenRequest, draft: Any, report: dict[str, Any]
    ) -> dict[str, Any]:
        contexts.append(report)
        return copy.deepcopy(task_payload)

    result = _run(
        tmp_path,
        [_record("G01")],
        FakeGenerator({BASE_REQUEST: broken}),
        validator=validator,
        repairer=repair,
    )

    assert result.report is not None
    sample = result.report["per_sample"][0]
    assert sample["actual_outcome"] == "passed_after_repair"
    assert sample["repair_rounds"] == 1
    assert sample["schema_ok"] is False
    assert sample["validation_ok"] is True
    assert contexts[0]["stage"] == "schema"
    assert contexts[0]["errors"][0]["code"] == "E_SCHEMA_002"
    assert len(validator.calls) == 1
    assert result.report["metrics"]["first_attempt_schema_rate"] == 0.0


def test_assertion_failure_is_not_a_validation_error(
    tmp_path: Path, task_payload: dict[str, Any]
) -> None:
    wrong_type = _variant(task_payload, task_type="main")

    result = _run(tmp_path, [_record("G01")], FakeGenerator({BASE_REQUEST: wrong_type}))

    assert result.report is not None
    sample = result.report["per_sample"][0]
    assert sample["actual_outcome"] == "assertion_failure"
    assert sample["validation_ok"] is True
    assert sample["failed_assertions"]
    assert result.report["metrics"]["fpvr"] == 0.0
    assert result.report["failure_groups"] == {"assertion_failure": 1}


def test_repair_can_fix_a_golden_assertion_failure(
    tmp_path: Path, task_payload: dict[str, Any]
) -> None:
    wrong_type = _variant(task_payload, task_type="main")
    contexts: list[dict[str, Any]] = []

    def repair(
        golden: aev.GoldenRequest, draft: Any, report: dict[str, Any]
    ) -> dict[str, Any]:
        contexts.append(report)
        return copy.deepcopy(task_payload)

    result = _run(
        tmp_path,
        [_record("G01")],
        FakeGenerator({BASE_REQUEST: wrong_type}),
        repairer=repair,
    )

    assert result.report is not None
    sample = result.report["per_sample"][0]
    assert sample["actual_outcome"] == "passed_after_repair"
    assert sample["repair_rounds"] == 1
    assert contexts[0]["stage"] == "assertions"
    assert any("task_type" in failure for failure in contexts[0]["failed_assertions"])


def test_generation_error_is_a_sample_failure_not_a_crash(
    tmp_path: Path,
) -> None:
    result = _run(
        tmp_path,
        [_record("G01")],
        FakeGenerator({BASE_REQUEST: RuntimeError("LLM 超时")}),
    )

    assert result.ok
    assert result.report is not None
    sample = result.report["per_sample"][0]
    assert sample["actual_outcome"] == "generation_error"
    assert "LLM 超时" in sample["message"]
    assert result.report["metrics"]["fpvr"] == 0.0
    assert result.report["metrics"]["generation_error_rate"] == 1.0


# ---------------------------------------------------------------------------
# Block sample handling
# ---------------------------------------------------------------------------


def _block_expected(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "outcome": "block",
        "task_type": "main",
        "level_min": 1,
        "gold_between": [0, 200],
        "exp_between": [0, 1600],
        "giver_npc_id": "NPC_007",
        "required_references": ["NPC_007"],
        "expected_error_codes": ["E_REWARD_RANGE"],
    }
    value.update(overrides)
    return value


def _pass_and_block_records() -> list[dict[str, Any]]:
    return [
        _record("G01"),
        _record("G10", request="BLOCK", expected=_block_expected(), category="boundary"),
    ]


def test_expected_block_with_matching_code_counts_as_intercepted(
    tmp_path: Path, task_payload: dict[str, Any]
) -> None:
    bad = _variant(task_payload, gold=1000)
    generator = FakeGenerator({BASE_REQUEST: task_payload, "BLOCK": bad})

    result = _run(tmp_path, _pass_and_block_records(), generator, validator=CappedGoldValidator())

    assert result.report is not None
    metrics = result.report["metrics"]
    assert metrics["fpvr"] == 1.0
    assert metrics["block_rate"] == 1.0
    assert metrics["outcome_accuracy"] == 1.0
    assert result.report["denominators"] == {
        "total": 2,
        "expected_pass": 1,
        "expected_block": 1,
    }
    block = result.report["per_sample"][1]
    assert block["expected_outcome"] == "block"
    assert block["actual_outcome"] == "correct_block"
    assert block["accepted"] is False
    assert block["error_codes"] == ["E_REWARD_RANGE"]


def test_expected_block_with_wrong_code_is_not_intercepted(
    tmp_path: Path, task_payload: dict[str, Any]
) -> None:
    result = _run(
        tmp_path,
        _pass_and_block_records(),
        FakeGenerator({BASE_REQUEST: task_payload, "BLOCK": task_payload}),
        validator=WrongCodeValidator(),
    )

    assert result.report is not None
    block = result.report["per_sample"][1]
    assert block["actual_outcome"] == "blocked_unexpected_code"
    assert result.report["metrics"]["block_rate"] == 0.0


def test_expected_block_without_validator_is_flagged_unexpected_pass(
    tmp_path: Path, task_payload: dict[str, Any]
) -> None:
    bad = _variant(task_payload, gold=1000)

    result = _run(
        tmp_path,
        _pass_and_block_records(),
        FakeGenerator({BASE_REQUEST: task_payload, "BLOCK": bad}),
    )

    assert result.report is not None
    assert result.report["mode"] == "schema_only"
    block = result.report["per_sample"][1]
    assert block["actual_outcome"] == "unexpected_pass"
    assert block["failed_assertions"]
    assert result.report["metrics"]["block_rate"] == 0.0
    assert any("schema_only" in note for note in result.report["notes"])


def test_expected_block_with_schema_error_is_not_a_correct_block(
    tmp_path: Path, task_payload: dict[str, Any]
) -> None:
    result = _run(
        tmp_path,
        _pass_and_block_records(),
        FakeGenerator(
            {BASE_REQUEST: task_payload, "BLOCK": {"task_id": "T_MAIN_0104"}}
        ),
    )

    assert result.report is not None
    block = result.report["per_sample"][1]
    assert block["actual_outcome"] == "schema_error"
    assert result.report["metrics"]["block_rate"] == 0.0


# ---------------------------------------------------------------------------
# Generator seam (A's agents/task_agent.py)
# ---------------------------------------------------------------------------


def test_load_generator_prefers_explicit_factory(
    monkeypatch: pytest.MonkeyPatch, task_payload: dict[str, Any]
) -> None:
    def factory(request: str) -> dict[str, Any]:
        return task_payload

    monkeypatch.setattr(
        aev, "_import_generator_module", lambda: SimpleNamespace(generate=lambda r: r)
    )

    generator, reason = aev.load_generator(factory)

    assert reason == ""
    assert generator is factory


def test_load_generator_uses_module_entrypoint(
    monkeypatch: pytest.MonkeyPatch, task_payload: dict[str, Any]
) -> None:
    module = SimpleNamespace(generate_draft=lambda request: task_payload)
    monkeypatch.setattr(aev, "_import_generator_module", lambda: module)

    generator, reason = aev.load_generator()

    assert reason == ""
    assert generator is not None
    assert generator("x") is task_payload


def test_load_generator_accepts_task_agent_class(
    monkeypatch: pytest.MonkeyPatch, task_payload: dict[str, Any]
) -> None:
    class FakeTaskAgent:
        def generate(self, request: str) -> dict[str, Any]:
            return task_payload

    monkeypatch.setattr(
        aev, "_import_generator_module", lambda: SimpleNamespace(TaskAgent=FakeTaskAgent)
    )

    generator, reason = aev.load_generator()

    assert reason == ""
    assert generator is not None
    assert generator("x") is task_payload


def test_load_generator_accepts_a_task_agent_instance(
    task_payload: dict[str, Any],
) -> None:
    class FakeTaskAgent:
        def generate(self, request: str) -> SimpleNamespace:
            return SimpleNamespace(draft=task_payload)

    generator, reason = aev.load_generator(FakeTaskAgent())

    assert reason == ""
    assert generator is not None
    assert generator("x").draft is task_payload


def test_load_generator_reports_missing_module(monkeypatch: pytest.MonkeyPatch) -> None:
    def _missing() -> Any:
        raise ModuleNotFoundError("No module named 'gameconfig_copilot.agents.task_agent'")

    monkeypatch.setattr(aev, "_import_generator_module", _missing)

    generator, reason = aev.load_generator()

    assert generator is None
    assert "待 A 就绪" in reason


def test_load_generator_reports_missing_entrypoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aev, "_import_generator_module", lambda: SimpleNamespace())

    generator, reason = aev.load_generator()

    assert generator is None
    assert "未暴露" in reason


def test_load_generator_reports_constructor_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenTaskAgent:
        def __init__(self) -> None:
            raise RuntimeError("needs the retrieval index")

    monkeypatch.setattr(
        aev, "_import_generator_module", lambda: SimpleNamespace(TaskAgent=BrokenTaskAgent)
    )

    generator, reason = aev.load_generator()

    assert generator is None
    assert "无法零参数构造" in reason


def test_run_without_generator_writes_no_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests_path = _write_records(tmp_path / "requests.jsonl", [_record("G01")])
    report_path = tmp_path / "fpvr-baseline.json"
    monkeypatch.setattr(
        aev,
        "load_generator",
        lambda factory=None: (None, "待 A 就绪：agents/task_agent.py 不存在"),
    )

    result = aev.run_agent_eval(requests_path=requests_path, report_path=report_path)

    assert result.status == "generator_unavailable"
    assert not result.ok
    assert result.report is None
    assert not report_path.exists()


# ---------------------------------------------------------------------------
# Harness errors and report writing
# ---------------------------------------------------------------------------


def test_run_rejects_non_callable_generator(
    tmp_path: Path,
) -> None:
    requests_path = _write_records(tmp_path / "requests.jsonl", [_record("G01")])

    with pytest.raises(aev.AgentEvalError, match="generator"):
        aev.run_agent_eval(
            requests_path=requests_path,
            report_path=tmp_path / "report.json",
            generator=42,  # type: ignore[arg-type]
        )


def test_run_rejects_negative_repair_rounds(tmp_path: Path) -> None:
    with pytest.raises(aev.AgentEvalError, match="max_repair_rounds"):
        aev.run_agent_eval(
            requests_path=tmp_path / "unused.jsonl",
            report_path=tmp_path / "report.json",
            generator=lambda request: {},
            max_repair_rounds=-1,
        )


def test_validator_exception_names_the_sample(
    tmp_path: Path, task_payload: dict[str, Any]
) -> None:
    def exploding_validator(draft: TaskDraft) -> dict[str, Any]:
        raise RuntimeError("validator bug")

    with pytest.raises(aev.AgentEvalError, match="G01"):
        _run(
            tmp_path,
            [_record("G01")],
            FakeGenerator({BASE_REQUEST: task_payload}),
            validator=exploding_validator,
        )


def test_repairer_exception_names_the_sample_and_round(
    tmp_path: Path, task_payload: dict[str, Any]
) -> None:
    bad = _variant(task_payload, gold=1000)

    def exploding_repair(
        golden: aev.GoldenRequest, draft: Any, report: dict[str, Any]
    ) -> dict[str, Any]:
        raise RuntimeError("repair node offline")

    with pytest.raises(aev.AgentEvalError, match="修复器第 1 轮"):
        _run(
            tmp_path,
            [_record("G01")],
            FakeGenerator({BASE_REQUEST: bad}),
            validator=CappedGoldValidator(),
            repairer=exploding_repair,
        )


def test_write_report_is_utf8_readable(tmp_path: Path) -> None:
    path = aev.write_report(
        {"suite": "agent", "note": "中文可直接阅读"}, tmp_path / "nested" / "report.json"
    )

    assert "中文可直接阅读" in path.read_text(encoding="utf-8")


def test_report_contains_definitions_and_denominators(
    tmp_path: Path, task_payload: dict[str, Any]
) -> None:
    result = _run(
        tmp_path,
        [_record("G01")],
        FakeGenerator({BASE_REQUEST: task_payload}),
    )

    assert result.report is not None
    report = result.report
    assert report["config"]["requests_path"].endswith("requests.jsonl")
    assert report["golden"]["version"] == 1
    assert report["golden"]["distribution"]["count"] == 1
    assert "denominators" in report
    assert any("FPVR" in note for note in report["notes"])
    assert any("S3-05" in note or "业务规则" in note for note in report["notes"])
