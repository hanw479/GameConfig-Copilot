"""Read-only retrieval tools for QueryAgent / context assembly (S2-02).

Exactly three tools are exposed, all backed by ``rag/retriever.py`` (ADR-007):

* ``search_wiki`` — world/NPC/location knowledge;
* ``find_similar_tasks`` — historical task templates (optionally by level/NPC);
* ``list_constraints`` — active constraint rules for this request.

There is deliberately **no** write/commit tool on this class, so a QueryAgent
cannot mutate the repository even if a prompt is injected (docs/03 S2 "从工具
注册层面杜绝越权").

Input
    A natural-language query plus neutral filters.
Output
    ``list[Evidence]`` ordered by fused score, empty when nothing matches.
Failure semantics
    Retriever errors propagate to the caller: ``gather_context`` treats them as
    fail-closed, while QueryAgent surfaces an explicit "未检索到" answer.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ..observability.tracer import Tracer, get_tracer
from ..rag.retriever import Retriever, build_filters
from ..rag.store import Hit
from .state import Evidence

__all__ = ["ReadOnlyTools"]


def _digest(value: Any) -> str:
    """Short, content-stable digest for tool args/results (never the payload)."""

    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):  # pragma: no cover - defensive fallback
        text = repr(value)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


class ReadOnlyTools:
    """The QueryAgent tool registry: retrieval only, no side effects."""

    TOOL_NAMES: tuple[str, ...] = (
        "search_wiki",
        "find_similar_tasks",
        "list_constraints",
    )

    def __init__(
        self,
        retriever: Retriever,
        *,
        max_results: int = 10,
        tracer: Tracer | None = None,
    ) -> None:
        if max_results <= 0:
            raise ValueError("max_results 必须 > 0")
        self.retriever = retriever
        self.max_results = max_results
        self._tracer = tracer

    @property
    def tracer(self) -> Tracer:
        return self._tracer or get_tracer()

    @property
    def tool_names(self) -> tuple[str, ...]:
        return self.TOOL_NAMES

    # ---------------------------------------------------------------- tools
    def search_wiki(self, query: str, *, top_k: int = 5) -> list[Evidence]:
        """Search world-knowledge chunks."""

        return self._run_tool(
            "search_wiki",
            {"query": query, "top_k": top_k},
            lambda: self._search(
                query,
                filters={"source_type": "wiki"},
                top_k=top_k,
            ),
        )

    def find_similar_tasks(
        self,
        query: str,
        *,
        top_k: int = 3,
        level_band: str | Sequence[int] | None = None,
        npc_id: str | None = None,
        task_type: str | None = None,
    ) -> list[Evidence]:
        """Find historical task templates, optionally filtered by level/NPC."""

        def call() -> list[Evidence]:
            filters = build_filters(
                source_type="task",
                task_type=task_type,
                level_band=level_band,
                npc_id=npc_id,
            )
            return self._search(query or "任务 模板", filters=filters, top_k=top_k)

        return self._run_tool(
            "find_similar_tasks",
            {
                "query": query,
                "top_k": top_k,
                "level_band": level_band,
                "npc_id": npc_id,
                "task_type": task_type,
            },
            call,
        )

    def list_constraints(
        self,
        *,
        query: str | None = None,
        scope: str | None = None,
        top_k: int = 8,
    ) -> list[Evidence]:
        """List active constraints relevant to ``query``/``scope``.

        ``scope`` is matched as a substring against the retrieved ``scope``
        metadata, so ``rewards.gold`` narrows the result without a new index.

        Reward floor constraints (``level_band_range`` on ``rewards.gold`` /
        ``rewards.exp``) are pinned to the front of every result: they are
        relevant to every draft, and a BM25 miss must not silently hide a hard
        floor from the generation prompt. ``top_k`` stays the hard cap.
        """

        def call() -> list[Evidence]:
            text = (query or scope or "约束 规则 上限 限制").strip()
            evidence = self._search(
                text,
                filters={"source_type": "constraint"},
                top_k=max(top_k, 1) * 2,
            )
            if scope:
                narrowed = [
                    item
                    for item in evidence
                    if scope in str(item.metadata.get("scope", "")) or scope in item.text
                ]
                if narrowed:
                    evidence = narrowed
            pinned = self._reward_floor_constraints()
            if pinned:
                pinned_ids = {item.chunk_id for item in pinned}
                evidence = pinned + [
                    item for item in evidence if item.chunk_id not in pinned_ids
                ]
            return evidence[:top_k]

        return self._run_tool(
            "list_constraints",
            {"query": query, "scope": scope, "top_k": top_k},
            call,
        )

    def _reward_floor_constraints(self) -> list[Evidence]:
        """Pinned reward-interval constraints from the sparse index (if present)."""

        sparse = getattr(self.retriever, "sparse", None)
        chunks = getattr(sparse, "chunks", None)
        if not chunks:
            return []
        pinned: list[Evidence] = []
        for chunk in chunks:
            metadata = getattr(chunk, "metadata", None) or {}
            if str(metadata.get("source_type", "")) != "constraint":
                continue
            if str(metadata.get("constraint_type", "")) != "level_band_range":
                continue
            if str(metadata.get("scope", "")) not in {"rewards.gold", "rewards.exp"}:
                continue
            pinned.append(
                Evidence(
                    chunk_id=chunk.chunk_id,
                    source_id=chunk.source_id,
                    text=chunk.text,
                    score=0.0,
                    position=str(metadata.get("position") or ""),
                    source_type="constraint",
                    metadata=dict(metadata),
                )
            )
        return sorted(pinned, key=lambda item: item.chunk_id)

    # ---------------------------------------------------------------- helper
    def existing_task_ids(self) -> list[str]:
        """Existing task ids from the searchable index (for ID allocation).

        Uses only the neutral ``SparseStore.chunks`` view via ``Retriever.sparse``;
        it never opens a vector-store client directly (ADR-007). A retriever
        without an index (mock) simply yields ``[]``.
        """

        sparse = getattr(self.retriever, "sparse", None)
        chunks = getattr(sparse, "chunks", None)
        if not chunks:
            return []
        ids: list[str] = []
        for chunk in chunks:
            metadata = getattr(chunk, "metadata", None) or {}
            if str(metadata.get("source_type", "")) != "task":
                continue
            task_id = metadata.get("task_id")
            if task_id:
                ids.append(str(task_id))
        return sorted(set(ids))

    def _run_tool(
        self,
        name: str,
        args: Mapping[str, Any],
        call: Callable[[], list[Evidence]],
    ) -> list[Evidence]:
        """Run one read-only tool inside a ``tool.<name>`` span (S7-05).

        Attrs carry only digests/counts, never the query text or snippets.
        Exceptions are recorded on the span and re-raised unchanged, so
        ``gather_context`` still fails closed with the original error.
        """

        with self.tracer.span(
            f"tool.{name}",
            attrs={"tool": name, "args_digest": _digest(args)},
        ) as span:
            try:
                result = call()
            except Exception as exc:
                span.update_attrs({"ok": False, "error_code": type(exc).__name__})
                raise
            span.update_attrs(
                {
                    "ok": True,
                    "error_code": None,
                    "result_digest": _digest([item.citation for item in result]),
                    "result_count": len(result),
                }
            )
            return result

    def _search(
        self,
        query: str,
        *,
        filters: dict[str, object],
        top_k: int,
    ) -> list[Evidence]:
        if top_k <= 0:
            return []
        hits: list[Hit] = self.retriever.search(
            query,
            filters=filters,
            top_k=min(top_k, self.max_results),
        )
        evidence: list[Evidence] = []
        seen: set[str] = set()
        for hit in hits:
            item = Evidence.from_hit(hit)
            if not item.text.strip() or item.citation in seen:
                continue
            seen.add(item.citation)
            evidence.append(item)
        return evidence
