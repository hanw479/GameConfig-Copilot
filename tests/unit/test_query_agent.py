"""QueryAgent tests (S2-02/03): read-only tools, citations, refusal branches."""

from __future__ import annotations

from gameconfig_copilot.agents.prompt_loader import load_prompt
from gameconfig_copilot.agents.query_agent import QueryAgent
from gameconfig_copilot.agents.tools import ReadOnlyTools
from support.fakes import (
    FakeLLM,
    FakeRetriever,
    constraint_hit,
    default_hits,
    make_hit,
    task_hit,
    wiki_hit,
)


def _agent(hits=None, *, chat_content: str | None = None) -> tuple[QueryAgent, FakeLLM]:
    llm = FakeLLM(chat_content=chat_content)
    return QueryAgent(FakeRetriever(hits if hits is not None else default_hits()), llm), llm


def test_answer_contains_only_traceable_citations() -> None:
    agent, _ = _agent()
    result = agent.answer("铁匠老陈对星港议会是什么立场？")

    assert result.answer
    assert result.citations
    allowed = {item.citation for item in result.evidence}
    assert set(result.citations) <= allowed
    for citation in result.citations:
        assert f"[{citation}]" in result.answer


def test_constraint_question_uses_list_constraints_tool() -> None:
    agent, _ = _agent()
    result = agent.answer("10 级任务金币奖励上限是多少？")

    assert "list_constraints" in result.tools
    assert any(citation.startswith("constraint:") for citation in result.citations)


def test_list_constraints_pins_reward_floor_constraints() -> None:
    """A BM25 miss must not hide the reward floor from the generation prompt."""

    class ConstraintMissRetriever(FakeRetriever):
        """Indexes the floor but the text search never returns it."""

        def search(self, query, filters=None, top_k=5, **kwargs):
            hits = super().search(query, filters=filters, top_k=top_k, **kwargs)
            return [
                hit
                for hit in hits
                if hit.metadata.get("constraint_type") != "level_band_range"
            ]

    floor = make_hit(
        "constraint:C_REWARD_GOLD_MIN_01",
        "rewards.gold",
        "约束 C_REWARD_GOLD_MIN_01：金币 [60, 200]。",
        source_type="constraint",
        constraint_id="C_REWARD_GOLD_MIN_01",
        constraint_type="level_band_range",
        scope="rewards.gold",
        bands=[{"max_level": 10, "min_amount": 60, "max_amount": 200}],
    )
    other = make_hit(
        "constraint:C_OTHER_01",
        "dialogue.text",
        "约束 C_OTHER_01：对白长度。",
        source_type="constraint",
        constraint_id="C_OTHER_01",
        constraint_type="length",
        scope="dialogue.text",
    )
    tools = ReadOnlyTools(ConstraintMissRetriever([floor, other]))

    result = tools.list_constraints(query="矿洞 支线", top_k=2)

    assert [item.metadata["constraint_id"] for item in result] == [
        "C_REWARD_GOLD_MIN_01",
        "C_OTHER_01",
    ]
    assert result[0].metadata["bands"][0]["min_amount"] == 60


def test_list_constraints_keeps_top_k_as_the_hard_cap() -> None:
    floor = make_hit(
        "constraint:C_REWARD_GOLD_MIN_01",
        "rewards.gold",
        "约束 C_REWARD_GOLD_MIN_01：金币 [60, 200]。",
        source_type="constraint",
        constraint_id="C_REWARD_GOLD_MIN_01",
        constraint_type="level_band_range",
        scope="rewards.gold",
        bands=[{"max_level": 10, "min_amount": 60, "max_amount": 200}],
    )
    tools = ReadOnlyTools(FakeRetriever([floor]))

    assert len(tools.list_constraints(query="矿洞", top_k=1)) == 1


def test_similar_task_question_uses_find_similar_tasks_tool() -> None:
    agent, _ = _agent()
    result = agent.answer("有没有类似 T_SIDE_0213 的支线任务？")

    assert "find_similar_tasks" in result.tools
    assert any(citation.startswith("task:") for citation in result.citations)


def test_no_evidence_refuses_without_calling_llm() -> None:
    agent, llm = _agent([])
    result = agent.answer("游戏里有飞行坐骑吗？")

    assert result.refused is True
    assert "未检索到" in result.answer
    assert result.citations == ()
    assert llm.chat_calls == []


def test_unknown_citation_is_stripped_and_flagged() -> None:
    agent, _ = _agent(chat_content="根据 [wiki:fake_source#nowhere] 的记载，答案是肯定的。")
    result = agent.answer("铁匠老陈对星港议会是什么立场？")

    codes = {warning["code"] for warning in result.warnings}
    assert "UNKNOWN_CITATION" in codes
    assert "MISSING_CITATION" in codes
    assert "wiki:fake_source" not in result.answer
    assert result.citations


def test_valid_citation_is_kept_verbatim() -> None:
    answer = "铁匠老陈隶属铁砧工会 [wiki:world_overview#三、地点与通行]。"
    agent, _ = _agent(chat_content=answer)
    result = agent.answer("铁匠老陈对星港议会是什么立场？")

    assert result.citations == ("wiki:world_overview#三、地点与通行",)
    assert "铁砧工会" in result.answer
    assert result.refused is False


def test_llm_failure_degrades_to_snippets() -> None:
    from gameconfig_copilot.llm import LLMTimeoutError

    llm = FakeLLM(chat_error=LLMTimeoutError("timeout"))
    agent = QueryAgent(FakeRetriever([wiki_hit(), task_hit(), constraint_hit()]), llm)
    result = agent.answer("铁匠老陈的立场是什么？")

    codes = {warning["code"] for warning in result.warnings}
    assert "LLM_TIMEOUT" in codes
    assert result.citations
    assert "超时" in result.answer


def test_tool_registry_exposes_only_read_only_tools() -> None:
    agent, _ = _agent()

    assert agent.tool_names == ("search_wiki", "find_similar_tasks", "list_constraints")
    for forbidden in ("commit_draft", "preview_draft", "write_file", "rollback_commit"):
        assert not hasattr(agent.tools, forbidden)


def test_query_prompt_declares_citation_and_refusal_rules() -> None:
    prompt = load_prompt("query")

    assert "[source_id#position]" in prompt
    assert "未检索到依据" in prompt
    assert "禁止编造" in prompt
    assert "只读" in prompt
