"""Graph tests (S3-03): routing, preview path, repair rounds, fail-closed."""

from __future__ import annotations

from gameconfig_copilot.agents.graph import (
    NODE_NAMES,
    AgentGraph,
    build_agent_graph,
)
from gameconfig_copilot.agents.query_agent import QueryAgent
from gameconfig_copilot.agents.state import new_state
from gameconfig_copilot.agents.supervisor import Supervisor
from gameconfig_copilot.agents.task_agent import TaskAgent
from gameconfig_copilot.agents.tools import ReadOnlyTools
from support.fakes import (
    FailingRetriever,
    FakeLLM,
    FakeRetriever,
    FakeValidator,
    bad_report,
    default_hits,
    ok_report,
    valid_task_payload,
)


def _graph(
    *,
    hits=None,
    llm: FakeLLM | None = None,
    validator=None,
    previewer=None,
    committer=None,
    tasks_dir: str = "data/tasks",
) -> AgentGraph:
    fake_llm = llm or FakeLLM(structured_payload=valid_task_payload())
    retriever = FakeRetriever(default_hits() if hits is None else hits)
    return build_agent_graph(
        fake_llm,
        retriever,
        validator=validator,
        previewer=previewer,
        committer=committer,
        tasks_dir=tasks_dir,
    )


class FakeCommitter:
    """Records commit calls and returns a CommitResult-shaped envelope."""

    def __init__(self, result: dict | None = None) -> None:
        self.calls: list[dict] = []
        self.result = result or {
            "ok": True,
            "status": "committed",
            "commit_id": "abc123def456",
            "backup_path": "data/backups/T_SIDE_0001/x.json",
            "new_revision": "rev-after",
            "report": {"ok": True},
        }

    def __call__(self, draft, **kwargs):
        self.calls.append({"draft": draft, **kwargs})
        return dict(self.result)


def test_node_contract_names_match_architecture_document() -> None:
    graph = _graph()

    assert set(graph.node_names) == set(NODE_NAMES)
    assert graph.node_names == NODE_NAMES


def test_query_flow_route_research_compose() -> None:
    graph = _graph()
    state = graph.invoke(new_state("铁匠老陈对星港议会是什么立场？"))

    assert state["visited"] == ["route", "research", "compose"]
    assert state["stage"] == "answered"
    assert state["citations"]
    assert state["answer"]


def test_task_flow_gather_draft_validate_preview(tmp_path) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    graph = _graph(tasks_dir=str(tasks_dir), validator=FakeValidator([ok_report()]))
    state = graph.invoke(new_state("给铁匠老陈加一条 6 级支线，去矿洞捡 4 块矿石"))

    assert state["visited"] == [
        "route",
        "gather_context",
        "draft",
        "validate",
        "preview",
        "human_gate",
    ]
    assert state["stage"] == "awaiting_confirmation"
    assert state["report"]["ok"] is True
    assert state["preview"]["fingerprint"].startswith("sha256:")
    assert state["preview"]["target_path"].endswith(".json")
    assert state["preview"]["diff"]["created"] is True
    assert list(tasks_dir.iterdir()) == []  # preview/gate never write


def test_task_flow_repairs_then_previews(tmp_path) -> None:
    validator = FakeValidator([bad_report(), ok_report()])
    llm = FakeLLM(
        structured_sequence=[
            valid_task_payload(gold=1000),
            valid_task_payload(gold=200),
        ]
    )
    graph = _graph(llm=llm, validator=validator, tasks_dir=str(tmp_path / "tasks"))
    state = graph.invoke(new_state("给铁匠老陈加一条 6 级支线"))

    assert state["visited"] == [
        "route",
        "gather_context",
        "draft",
        "validate",
        "repair",
        "validate",
        "preview",
        "human_gate",
    ]
    assert state["repair_round"] == 1
    assert state["draft_version"] == 2
    assert state["stage"] == "awaiting_confirmation"
    repair_prompt = "\n".join(
        message.content
        for message in llm.structured_calls[-1]["messages"]
    )
    assert "## 原始需求" in repair_prompt
    assert "## 校验错误（仅 severity=error" in repair_prompt
    assert "E_REWARD_RANGE" in repair_prompt
    assert "## 相关约束原文" in repair_prompt


def test_two_failed_repair_rounds_end_rejected(tmp_path) -> None:
    validator = FakeValidator([bad_report()])
    llm = FakeLLM(
        structured_sequence=[
            valid_task_payload(gold=1000),
            valid_task_payload(gold=900),
            valid_task_payload(gold=800),
        ]
    )
    graph = _graph(llm=llm, validator=validator, tasks_dir=str(tmp_path / "tasks"))
    state = graph.invoke(new_state("给铁匠老陈加一条 6 级支线"))

    assert state["repair_round"] == 2
    assert state["draft_version"] == 3
    assert state["stage"] == "rejected"
    assert state["error"]["code"] == "E_REWARD_RANGE"
    assert state["preview"] is None
    assert len(llm.structured_calls) == 3
    assert list(tmp_path.rglob("*.json")) == []  # zero writes, zero previews


def test_repair_without_previous_report_never_calls_the_model(tmp_path) -> None:
    llm = FakeLLM(structured_payload=valid_task_payload())
    graph = _graph(llm=llm, tasks_dir=str(tmp_path / "tasks"))
    state = graph.invoke(
        {
            "request": "给铁匠老陈加一条 6 级支线",
            "draft": valid_task_payload(task_id="T_SIDE_0000"),
            "repair_round": 0,
        },
        start="repair",
    )

    assert state["visited"] == ["repair"]
    assert state["stage"] == "rejected"
    assert state["error"]["code"] == "E_VALIDATION_REQUIRED"
    assert llm.structured_calls == []


def test_human_gate_pauses_without_commit(tmp_path) -> None:
    committer = FakeCommitter()
    graph = _graph(
        validator=FakeValidator([ok_report()]),
        committer=committer,
        tasks_dir=str(tmp_path / "tasks"),
    )
    state = graph.invoke(new_state("给铁匠老陈加一条 6 级支线"))

    assert state["visited"][-1] == "human_gate"
    assert state["stage"] == "awaiting_confirmation"
    assert committer.calls == []
    assert list(tmp_path.rglob("*.json")) == []


def test_confirmed_gate_commits_through_injected_committer(tmp_path) -> None:
    committer = FakeCommitter()
    graph = _graph(
        validator=FakeValidator([ok_report()]),
        committer=committer,
        tasks_dir=str(tmp_path / "tasks"),
    )
    state = new_state("给铁匠老陈加一条 6 级支线")
    state["human_decision"] = {"status": "confirmed", "note": "ok"}
    final = graph.invoke(state)

    assert final["visited"][-2:] == ["human_gate", "commit"]
    assert final["stage"] == "committed"
    assert final["commit"]["commit_id"] == "abc123def456"
    assert final["commit"]["backup"].endswith(".json")
    assert len(committer.calls) == 1
    call = committer.calls[0]
    assert call["fingerprint"].startswith("sha256:")
    assert call["expected_revision"] == "0"
    assert call["author"] == "anonymous"
    assert call["note"] == "ok"
    assert call["draft"]["task_id"].startswith("T_SIDE_")


def test_rejected_gate_keeps_reason_and_writes_nothing(tmp_path) -> None:
    committer = FakeCommitter()
    graph = _graph(
        validator=FakeValidator([ok_report()]),
        committer=committer,
        tasks_dir=str(tmp_path / "tasks"),
    )
    state = new_state("给铁匠老陈加一条 6 级支线")
    state["human_decision"] = {"status": "rejected", "reason": "奖励结构不对"}
    final = graph.invoke(state)

    assert final["visited"] == [
        "route",
        "gather_context",
        "draft",
        "validate",
        "preview",
        "human_gate",
    ]
    assert final["stage"] == "rejected"
    assert final["error"]["code"] == "E_HUMAN_REJECTED"
    assert "奖励结构不对" in final["error"]["message"]
    assert committer.calls == []
    assert list(tmp_path.rglob("*.json")) == []


def test_cancelled_gate_is_not_a_write(tmp_path) -> None:
    committer = FakeCommitter()
    graph = _graph(
        validator=FakeValidator([ok_report()]),
        committer=committer,
        tasks_dir=str(tmp_path / "tasks"),
    )
    state = new_state("给铁匠老陈加一条 6 级支线")
    state["human_decision"] = {"status": "cancelled"}
    final = graph.invoke(state)

    assert final["stage"] == "cancelled"
    assert final["error"]["code"] == "E_CONFIRM_TIMEOUT"
    assert committer.calls == []
    assert list(tmp_path.rglob("*.json")) == []


def test_confirmed_gate_without_write_mode_is_refused(tmp_path) -> None:
    graph = _graph(
        validator=FakeValidator([ok_report()]),
        tasks_dir=str(tmp_path / "tasks"),
    )
    state = new_state("给铁匠老陈加一条 6 级支线")
    state["human_decision"] = {"status": "confirmed"}
    final = graph.invoke(state)

    assert final["stage"] == "rejected"
    assert final["error"]["code"] == "E_WRITE_DISABLED"
    assert list(tmp_path.rglob("*.json")) == []


def test_other_intent_clarifies() -> None:
    graph = _graph()
    state = graph.invoke(new_state("你好"))

    assert state["visited"] == ["route", "clarify"]
    assert state["stage"] == "clarify"
    assert state["intent"] == "other"


def test_constraint_load_failure_aborts_generation_fail_closed(tmp_path) -> None:
    retriever = FailingRetriever(default_hits())
    llm = FakeLLM(structured_payload=valid_task_payload())
    graph = build_agent_graph(llm, retriever, tasks_dir=str(tmp_path / "tasks"))
    state = graph.invoke(new_state("给铁匠老陈加一条 6 级支线"))

    assert state["visited"] == ["route", "gather_context"]
    assert state["stage"] == "rejected"
    assert state["error"]["code"] == "E_CONSTRAINT_LOAD_FAILED"
    assert state["draft"] is None
    assert llm.structured_calls == []


def test_validator_crash_is_treated_as_failure(tmp_path) -> None:
    validator = FakeValidator([RuntimeError("validator exploded")])
    graph = _graph(validator=validator, tasks_dir=str(tmp_path / "tasks"))
    state = graph.invoke(new_state("给铁匠老陈加一条 6 级支线"))

    assert state["report"]["ok"] is False
    assert state["report"]["errors"][0]["code"] == "E_VALIDATOR_CRASH"
    assert state["stage"] == "rejected"


def test_direct_graph_construction_matches_factory(tmp_path) -> None:
    llm = FakeLLM(structured_payload=valid_task_payload())
    retriever = FakeRetriever(default_hits())
    tools = ReadOnlyTools(retriever)
    graph = AgentGraph(
        supervisor=Supervisor(),
        query_agent=QueryAgent(retriever, llm, tools=tools),
        task_agent=TaskAgent(llm, existing_ids_provider=tools.existing_task_ids),
        tools=tools,
        validator=FakeValidator([ok_report()]),
        tasks_dir=str(tmp_path / "tasks"),
    )
    graph.compile()
    state = graph.invoke(new_state("给铁匠老陈加一条 6 级支线"))

    assert state["stage"] == "awaiting_confirmation"
