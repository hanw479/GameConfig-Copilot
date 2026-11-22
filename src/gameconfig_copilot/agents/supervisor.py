"""Supervisor intent routing: rules first, optional small model second (S2-01).

Routing is intentionally cheap: a weighted keyword/pattern classifier decides
``query`` / ``task`` / ``other`` and only ambiguous inputs (low confidence) are
sent to an optional routing model (docs/02 §3.2 budget 0.3s). When the model is
absent, fails, or returns an invalid intent, the rule decision is kept verbatim —
routing must never become a single point of failure.

Input
    The raw request string.
Output
    :class:`RouteDecision` with ``intent``, ``confidence``, ``source`` and a
    human-readable ``reason``.
Failure semantics
    An empty request is classified ``other`` with confidence 0 (route failure ->
    ``clarify`` per docs/02 §4.2). LLM failures are swallowed and reported via
    ``source="fallback"``; they never raise.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Literal

from ..llm.client import LLMClient, LLMError, Message
from .prompt_loader import load_prompt
from .state import Intent

__all__ = [
    "DOMAIN_TERMS",
    "RouteDecision",
    "Supervisor",
    "classify_intent",
]

Source = Literal["rule", "llm", "fallback"]

#: Game-config domain vocabulary. A generic question word ("怎么样？") is not
#: enough to route to QueryAgent: without a domain term it is chit-chat.
DOMAIN_TERMS: frozenset[str] = frozenset(
    {
        "任务",
        "支线",
        "主线",
        "日常",
        "奖励",
        "金币",
        "铭币",
        "阅历",
        "经验",
        "道具",
        "等级",
        "级",
        "前置",
        "依赖",
        "约束",
        "规则",
        "上限",
        "下限",
        "范围",
        "npc",
        "对话",
        "对白",
        "剧情",
        "世界观",
        "设定",
        "背景",
        "阵营",
        "势力",
        "立场",
        "关系",
        "地图",
        "区域",
        "场景",
        "矿洞",
        "矿车",
        "矿石",
        "萤石",
        "星港",
        "荒原",
        "哨站",
        "铁匠",
        "议长",
        "教团",
        "工会",
        "议会",
        "阿岚",
        "老陈",
        "知识库",
        "配置",
        "草案",
        "模板",
        "标签",
        "接口",
        "奖励带",
        "任务id",
        "游戏",
    }
)

_TASK_VERB_PATTERNS: tuple[tuple[re.Pattern[str], float], ...] = (
    (re.compile(r"加一条|加个|加一个|补一条|补个|新增一条|新增个"), 3.0),
    (re.compile(r"新建|创建|生成|设计|写一条|做一条|做一个|帮我配|来一条|安排一个"), 3.0),
    (re.compile(r"搞一个|整一个|来一个|弄一个"), 3.0),
    (re.compile(r"改成|改为|调整为|修改为|替换为"), 2.0),
    (re.compile(r"调整|修改|改一下|改下|改到|改至|添加"), 2.0),
    (re.compile(r"删除|删掉|去掉|移除"), 2.0),
    (re.compile(r"(?:给|帮|为).{0,20}(?:加|添加|生成|设计|做|建|写)"), 3.0),
)

_TASK_NOUN_SCORE = 0.3

_QUERY_PATTERNS: tuple[tuple[re.Pattern[str], float], ...] = (
    (re.compile(r"为什么"), 3.0),
    (re.compile(r"是什么|什么是"), 2.5),
    (re.compile(r"什么"), 2.0),
    (re.compile(r"怎么|怎样"), 2.0),
    (re.compile(r"如何"), 2.0),
    (re.compile(r"哪(?:个|里|张|位|些)?"), 1.5),
    (re.compile(r"谁"), 2.0),
    (re.compile(r"多少|几次|几级|几条|几个"), 2.0),
    (re.compile(r"是否|有没有|能否|可不可以|可以吗"), 2.0),
    (re.compile(r"吗[？?]?$|呢[？?]?$"), 1.5),
    (re.compile(r"介绍|解释|说明一下|讲述"), 2.0),
    (re.compile(r"查一下|查查|告诉我|列出|有哪些"), 1.5),
    (re.compile(r"看看|看一下|看下|查一查"), 1.5),
    (re.compile(r"t_(?:main|side|daily)_\d{4}"), 1.5),
    (re.compile(r"铁匠老陈|老陈|阿岚|议长|npc_"), 1.5),
    (
        re.compile(
            r"关系|立场|背景|设定|规则|上限|下限|约束|相似|类似|参考|知识库|前置|依赖"
        ),
        1.0,
    ),
    (re.compile(r"[？?]"), 2.0),
)

_ROUTE_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {"intent": {"type": "string", "enum": ["query", "task", "other"]}},
    "required": ["intent"],
    "additionalProperties": False,
}

_VALID_INTENTS = {intent.value for intent in Intent}


def _has_domain_term(text: str) -> bool:
    lowered = text.lower()
    if re.search(r"t_(?:main|side|daily)_\d{4}", lowered):
        return True
    return any(term in lowered for term in DOMAIN_TERMS)


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """One routing outcome; ``source`` records who actually decided."""

    intent: Intent
    confidence: float
    source: Source = "rule"
    reason: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "intent": self.intent.value,
            "confidence": round(self.confidence, 3),
            "source": self.source,
            "reason": self.reason,
        }


def _rule_decision(request: str) -> RouteDecision:
    text = request.strip()
    if not text:
        return RouteDecision(Intent.OTHER, 0.0, "rule", "空请求，无法判定意图")

    has_domain = _has_domain_term(text)

    task_verb_score = 0.0
    task_hits: list[str] = []
    for pattern, weight in _TASK_VERB_PATTERNS:
        match = pattern.search(text)
        if match is not None:
            task_verb_score += weight
            task_hits.append(match.group(0))

    query_score = 0.0
    query_hits: list[str] = []
    for pattern, weight in _QUERY_PATTERNS:
        match = pattern.search(text)
        if match is not None:
            query_score += weight
            query_hits.append(match.group(0))

    task_noun_score = _TASK_NOUN_SCORE * sum(
        1 for noun in ("任务", "支线", "主线", "日常", "奖励", "目标", "级")
        if noun in text
    )
    task_score = task_verb_score + (task_noun_score if has_domain else 0.0)

    if task_verb_score > 0 and task_verb_score >= max(2.0, query_score * 0.6):
        chosen = Intent.TASK
        reason = f"任务动词：{'/'.join(dict.fromkeys(task_hits))}"
    elif query_score >= 2.0 and has_domain:
        chosen = Intent.QUERY
        reason = f"提问模式：{'/'.join(dict.fromkeys(query_hits))}"
    elif task_verb_score > 0:
        chosen = Intent.TASK
        reason = f"任务动词：{'/'.join(dict.fromkeys(task_hits))}"
    elif task_noun_score >= 1.5 and has_domain:
        chosen = Intent.TASK
        reason = "任务域名词密集"
    else:
        chosen = Intent.OTHER
        reason = "无任务动词且未命中领域提问词" if not has_domain else "信息不足"

    if chosen is Intent.TASK:
        score = max(task_score, 1.0)
    elif chosen is Intent.QUERY:
        score = max(query_score, 1.0)
    else:
        score = 0.0
    confidence = 0.3 if chosen is Intent.OTHER else min(0.99, 0.5 + score / 12)
    return RouteDecision(chosen, confidence, "rule", reason)


def classify_intent(request: str) -> RouteDecision:
    """Convenience wrapper around the rule-only classifier."""

    return _rule_decision(request)


class Supervisor:
    """Rules + optional small model intent router."""

    def __init__(
        self,
        llm: LLMClient | None = None,
        *,
        llm_confidence_threshold: float = 0.65,
    ) -> None:
        if not 0.0 <= llm_confidence_threshold <= 1.0:
            raise ValueError("llm_confidence_threshold 必须在 [0, 1] 内")
        self._llm = llm
        self.llm_confidence_threshold = llm_confidence_threshold
        self._prompt: str | None = None

    @property
    def prompt(self) -> str:
        if self._prompt is None:
            self._prompt = load_prompt("supervisor")
        return self._prompt

    def route(self, request: str) -> RouteDecision:
        """Classify one request; never raises on model failure."""

        decision = _rule_decision(request)
        if self._llm is None or decision.confidence >= self.llm_confidence_threshold:
            return decision

        messages = [Message.system(self.prompt), Message.user(request.strip())]
        try:
            payload = self._llm.structured(messages, _ROUTE_SCHEMA, temperature=0.0)
        except (LLMError, ValueError, TypeError, KeyError):
            return replace(
                decision,
                source="fallback",
                reason=f"{decision.reason}；小模型路由失败，回退规则",
            )

        raw_intent = (
            str(payload.get("intent", "")).strip().lower()
            if isinstance(payload, dict)
            else ""
        )
        if raw_intent not in _VALID_INTENTS:
            return replace(
                decision,
                source="fallback",
                reason=f"{decision.reason}；小模型返回非法意图 {raw_intent!r}，回退规则",
            )
        return RouteDecision(
            Intent(raw_intent),
            0.9,
            "llm",
            "低置信度规则结果交由小模型判定",
        )

    def classify(self, request: str) -> RouteDecision:
        """Alias for :meth:`route` (node/CLI ergonomics)."""

        return self.route(request)
