"""End-to-end read-only loop (S2-05): 3 questions with real corpus + mock LLM."""

from __future__ import annotations

from pathlib import Path

import pytest

from gameconfig_copilot.agents.query_agent import QueryAgent
from gameconfig_copilot.rag.index import build_index, load_index
from gameconfig_copilot.rag.retriever import Retriever
from support.fakes import FakeLLM, FakeRetriever

pytestmark = pytest.mark.e2e

REPO_ROOT = Path(__file__).resolve().parents[2]

CASES = [
    ("铁匠老陈对星港议会是什么立场？", "wiki:"),
    ("有没有类似 T_SIDE_0213 的支线任务？", "task:"),
    ("10 级任务金币奖励上限是多少？", "constraint:"),
]


@pytest.fixture(scope="module")
def retriever(tmp_path_factory: pytest.TempPathFactory) -> Retriever:
    index_dir = tmp_path_factory.mktemp("index")
    build_index(
        REPO_ROOT / "data",
        index_dir,
        rebuild=True,
        sparse_backend="python",
    )
    rag_index = load_index(index_dir)
    return Retriever(rag_index.sparse, rag_index.dense)


@pytest.mark.parametrize(("question", "expected_prefix"), CASES)
def test_three_questions_have_traceable_citations(
    retriever: Retriever,
    question: str,
    expected_prefix: str,
) -> None:
    agent = QueryAgent(retriever, FakeLLM())
    result = agent.answer(question)

    assert result.refused is False
    assert result.citations
    assert any(citation.startswith(expected_prefix) for citation in result.citations)
    allowed = {item.citation for item in result.evidence}
    assert set(result.citations) <= allowed
    for citation in result.citations:
        assert f"[{citation}]" in result.answer


def test_out_of_knowledge_base_question_refuses() -> None:
    agent = QueryAgent(FakeRetriever([]), FakeLLM())
    result = agent.answer("游戏里有飞行坐骑吗？")

    assert result.refused is True
    assert "未检索到" in result.answer
    assert result.citations == ()


def test_real_corpus_has_wiki_task_and_constraint_chunks(retriever: Retriever) -> None:
    from gameconfig_copilot.agents.tools import ReadOnlyTools

    tools = ReadOnlyTools(retriever)
    assert tools.search_wiki("铁匠老陈 星港议会")
    assert tools.find_similar_tasks("矿洞 采集 支线")
    assert tools.list_constraints(query="金币 奖励 上限")
    assert len(tools.existing_task_ids()) >= 10
