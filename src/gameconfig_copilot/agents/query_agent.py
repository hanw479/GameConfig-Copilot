"""QueryAgent: retrieval-grounded, citation-enforced read-only answers (S2-02/03).

The agent calls the three read-only tools, renders the snippets as the **only**
allowed evidence, and post-validates every ``[source]`` the model emits:

* citations must match a retrieved ``source_id#position`` (or its ``source_id``);
* unknown citations are stripped and reported as a warning;
* an answer without any valid citation is replaced by a deterministic snippet
  list — the agent never returns an uncited factual claim;
* no evidence at all short-circuits to "未检索到依据" **without calling the LLM**.

Input
    A question; the retriever and LLM are injected (Protocols only).
Output
    :class:`QueryResult` (answer, citations, evidence, warnings, tools used).
Failure semantics
    LLM timeout/error degrades to the retrieved snippet list with a warning;
    an empty retrieval is an explicit refusal branch, never a hallucination.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..llm.client import LLMClient, LLMError, LLMTimeoutError, Message
from .prompt_loader import load_prompt
from .state import Evidence
from .tools import ReadOnlyTools

__all__ = ["QueryAgent", "QueryResult"]

CITATION_RE = re.compile(r"\[([^\[\]\n]{1,200})\]")
_REFERENCE_HINTS = ("相似", "类似", "参考", "历史", "已有", "同npc", "同类", "模板")
_CONSTRAINT_HINTS = (
    "约束",
    "规则",
    "上限",
    "下限",
    "限制",
    "允许",
    "禁止",
    "必须",
    "范围",
    "等级段",
    "奖励区间",
    "门槛",
)
_MAX_EVIDENCE = 10
_SNIPPET_CHARS = 220


def _snippet(text: str, limit: int = _SNIPPET_CHARS) -> str:
    compact = " ".join(text.split())
    return compact if len(compact) <= limit else compact[:limit] + "…"


@dataclass(frozen=True, slots=True)
class QueryResult:
    """One grounded answer plus the evidence needed to audit it."""

    answer: str
    citations: tuple[str, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    warnings: tuple[dict[str, Any], ...] = ()
    tools: tuple[str, ...] = ()
    tokens: int = 0
    model: str = ""
    refused: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "citations": list(self.citations),
            "evidence": [item.to_dict() for item in self.evidence],
            "warnings": [dict(item) for item in self.warnings],
            "tools": list(self.tools),
            "refused": self.refused,
        }


@dataclass
class _EnforcedAnswer:
    answer: str
    citations: list[str] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    refused: bool = False


class QueryAgent:
    """Read-only question answering over the hybrid retriever."""

    def __init__(
        self,
        retriever: Any,
        llm: LLMClient,
        *,
        tools: ReadOnlyTools | None = None,
        max_evidence: int = _MAX_EVIDENCE,
    ) -> None:
        if max_evidence <= 0:
            raise ValueError("max_evidence 必须 > 0")
        self.retriever = retriever
        self.llm = llm
        self.tools = tools or ReadOnlyTools(retriever)
        self.max_evidence = max_evidence
        self._prompt: str | None = None

    @property
    def prompt(self) -> str:
        if self._prompt is None:
            self._prompt = load_prompt("query")
        return self._prompt

    @property
    def tool_names(self) -> tuple[str, ...]:
        return self.tools.tool_names

    # ------------------------------------------------------------------ API
    def answer(self, question: str, *, top_k: int = 5) -> QueryResult:
        """Retrieve, answer and enforce citations in one call."""

        evidence, tools_used = self.collect_evidence(question, top_k=top_k)
        return self.compose(question, evidence, tools_used=tools_used)

    def ask(self, question: str, *, top_k: int = 5) -> QueryResult:
        """Alias for :meth:`answer` (CLI ergonomics)."""

        return self.answer(question, top_k=top_k)

    def collect_evidence(
        self,
        question: str,
        *,
        top_k: int = 5,
    ) -> tuple[list[Evidence], tuple[str, ...]]:
        """Call the read-only tools selected by the question's shape."""

        text = question.strip()
        lowered = text.lower()

        wiki_evidence = self.tools.search_wiki(text, top_k=top_k)
        tools_used = ["search_wiki"]

        task_evidence: list[Evidence] = []
        constraint_evidence: list[Evidence] = []
        wants_similar = any(hint in lowered for hint in _REFERENCE_HINTS)
        wants_constraints = any(hint in lowered for hint in _CONSTRAINT_HINTS)
        if wants_similar:
            task_evidence = self.tools.find_similar_tasks(text, top_k=3)
            tools_used.append("find_similar_tasks")
        if wants_constraints:
            constraint_evidence = self.tools.list_constraints(query=text, top_k=6)
            tools_used.append("list_constraints")

        # Put the tool selected by the question shape first so the generated
        # answer cites the most relevant source type (and truncation keeps it).
        if wants_constraints:
            ordered = [*constraint_evidence, *wiki_evidence, *task_evidence]
        elif wants_similar:
            ordered = [*task_evidence, *wiki_evidence, *constraint_evidence]
        else:
            ordered = [*wiki_evidence, *task_evidence, *constraint_evidence]

        deduped: list[Evidence] = []
        seen: set[str] = set()
        for item in ordered:
            key = item.citation
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return deduped[: self.max_evidence], tuple(tools_used)

    def compose(
        self,
        question: str,
        evidence: list[Evidence] | tuple[Evidence, ...],
        *,
        tools_used: tuple[str, ...] = (),
    ) -> QueryResult:
        """Answer from already-retrieved evidence (used by the graph)."""

        if not evidence:
            refusal = (
                "未检索到依据：知识库未覆盖该问题。"
                "请补充关键词，或确认该设定是否已进入知识库。"
            )
            return QueryResult(
                answer=refusal,
                evidence=(),
                warnings=(
                    {
                        "code": "NO_EVIDENCE",
                        "message": "检索结果为空，按只读闭环规则拒答，不调用生成模型。",
                    },
                ),
                tools=tools_used,
                model=getattr(self.llm, "model", ""),
                refused=True,
            )

        messages = [
            Message.system(self.prompt),
            Message.user(self._render_evidence(question, evidence)),
        ]
        warnings: list[dict[str, Any]] = []
        try:
            response = self.llm.chat(messages, temperature=0.1, max_tokens=600)
            raw_answer = response.content
            model = response.model
            usage = getattr(response, "usage", None)
            tokens = int(getattr(usage, "total_tokens", 0) or 0)
        except LLMTimeoutError as exc:
            raw_answer = self._fallback_answer(evidence, "生成模型超时")
            warnings.append({"code": "LLM_TIMEOUT", "message": str(exc)})
            model = getattr(self.llm, "model", "")
            tokens = 0
        except LLMError as exc:
            raw_answer = self._fallback_answer(evidence, "生成模型不可用")
            warnings.append({"code": "LLM_ERROR", "message": str(exc)})
            model = getattr(self.llm, "model", "")
            tokens = 0

        enforced = self._enforce_citations(raw_answer, evidence)
        warnings.extend(enforced.warnings)
        return QueryResult(
            answer=enforced.answer,
            citations=tuple(enforced.citations),
            evidence=tuple(evidence),
            warnings=tuple(warnings),
            tools=tools_used,
            tokens=tokens,
            model=model,
            refused=enforced.refused,
        )

    # -------------------------------------------------------------- internal
    def _render_evidence(
        self,
        question: str,
        evidence: list[Evidence] | tuple[Evidence, ...],
    ) -> str:
        lines = [
            "## 问题",
            question.strip(),
            "",
            "## 检索片段（唯一可用依据，只能引用方括号内的来源）",
        ]
        for item in evidence:
            lines.append(f"- [{item.citation}] 相关度 {item.score:.4f}")
            lines.append(f"  {_snippet(item.text)}")
        lines.extend(
            [
                "",
                "## 回答要求",
                "先给结论，再给依据；每条事实性陈述后附 `[source_id#position]`。",
                "若片段不足，直接回答「未检索到依据：知识库未覆盖该问题。」",
            ]
        )
        return "\n".join(lines)

    def _enforce_citations(
        self,
        answer: str,
        evidence: list[Evidence] | tuple[Evidence, ...],
    ) -> _EnforcedAnswer:
        allowed: dict[str, str] = {}
        for item in evidence:
            allowed[item.citation] = item.citation
            allowed.setdefault(item.source_id, item.citation)

        valid: list[str] = []
        unknown: list[str] = []

        def replace(match: re.Match[str]) -> str:
            reference = match.group(1).strip()
            canonical = allowed.get(reference)
            if canonical is None:
                if reference not in unknown:
                    unknown.append(reference)
                return ""
            if canonical not in valid:
                valid.append(canonical)
            return f"[{canonical}]"

        cleaned = " ".join(CITATION_RE.sub(replace, answer).split())
        warnings: list[dict[str, Any]] = []
        if unknown:
            warnings.append(
                {
                    "code": "UNKNOWN_CITATION",
                    "message": "模型引用了检索片段中不存在的来源，已移除："
                    + "、".join(unknown[:5]),
                }
            )

        if not valid or not cleaned:
            warnings.append(
                {
                    "code": "MISSING_CITATION",
                    "message": "模型回答缺少可解析的引用，已降级为检索片段列表。",
                }
            )
            return _EnforcedAnswer(
                answer=self._fallback_answer(evidence, "回答缺少可验证引用"),
                citations=[item.citation for item in evidence[:5]],
                warnings=warnings,
                refused=True,
            )

        if unknown:
            warnings.append(
                {
                    "code": "ANSWER_REWRITTEN",
                    "message": "已移除无法溯源的引用，其余引用均可在检索结果中定位。",
                }
            )
        return _EnforcedAnswer(answer=cleaned, citations=valid, warnings=warnings)

    @staticmethod
    def _fallback_answer(
        evidence: list[Evidence] | tuple[Evidence, ...],
        reason: str,
    ) -> str:
        lines = [f"{reason}，以下仅为检索到的片段（未生成推断性结论）："]
        for item in evidence[:5]:
            lines.append(f"- [{item.citation}] {_snippet(item.text, 160)}")
        return "\n".join(lines)
