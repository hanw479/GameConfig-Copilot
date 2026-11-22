"""Hybrid retriever: query normalisation -> filters -> BM25 + dense -> RRF.

Pipeline (docs/02 §5.2)::

    raw query -> 术语归一 -> neutral filters -> BM25 top-30 + dense top-30
              -> RRF (k=60) top-15 -> list[Hit]

Structural filters are applied **inside** each store before scoring, so an
out-of-band chunk can never leak into the results (docs/03 S1 验收: 过滤生效).
Dense retrieval is optional: when the adapter reports ``health() is False``
(:class:`~gameconfig_copilot.rag.stores.noop.NoopDenseStore` in S1) the pipeline
degrades to BM25 + RRF without failing.

Input
    Query string, neutral ``filters`` and ``top_k``.
Output
    ``list[Hit]`` ordered by descending RRF score; ``Hit.score`` is the fused
    score, and ``Hit.citation`` is ``source_id#position``.
Failure semantics
    Empty query / empty index return ``[]``; invalid ``top_k`` or malformed
    ``level_band`` raise :class:`ValueError`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from .fusion import DEFAULT_RRF_K, fuse_rankings
from .store import Hit, VectorStore, normalize_filters

__all__ = [
    "DEFAULT_CANDIDATE_K",
    "DEFAULT_MAX_RESULTS",
    "SYNONYM_TABLE",
    "NormalizedQuery",
    "Retriever",
    "build_filters",
    "normalize_query",
    "parse_level_band",
]

#: Candidate depth for each branch before fusion (docs/02 §5.2: top-30).
DEFAULT_CANDIDATE_K = 30
#: How many fused hits to keep (docs/02 §5.2: top-15).
DEFAULT_MAX_RESULTS = 15

#: 18 groups of domain synonyms; ``canonical -> surface forms``. Canonical ids
#: are exactly what the corpus and validator use, so normalisation makes BM25
#: match ``MAP_MINE_03`` when the designer typed "矿洞".
SYNONYM_TABLE: dict[str, tuple[str, ...]] = {
    "MAP_MINE_03": ("废弃矿洞", "矿洞", "矿车", "采掘", "矿井", "矿脉", "采矿", "矿点"),
    "NPC_013": ("铁匠老陈", "老陈", "铁匠"),
    "NPC_001": ("议长", "沈砚"),
    "NPC_021": ("矿工阿岚", "阿岚"),
    "MAP_STARPORT_01": ("星港城", "星港"),
    "MAP_WASTELAND_02": ("荒原哨站", "哨站", "荒原"),
    "FACTION_COUNCIL_01": ("星港议会", "议会"),
    "FACTION_ANVIL_02": ("铁砧工会", "工会"),
    "FACTION_DEEPWELL_03": ("深井教团", "教团"),
    "ITEM_ORE_02": ("铁尘矿石", "矿石"),
    "ITEM_ORE_05": ("深井萤石", "萤石"),
    "ITEM_RELIC_DEEPWELL_01": ("深井铃",),
    "C_REWARD_GOLD_01": ("金币上限", "奖励上限"),
    "gold": ("金币", "铭币"),
    "exp": ("阅历", "经验"),
    "side": ("支线",),
    "main": ("主线",),
    "daily": ("日常",),
}


@dataclass(frozen=True, slots=True)
class NormalizedQuery:
    raw: str
    normalized: str
    expansions: tuple[str, ...] = ()


def normalize_query(query: str) -> NormalizedQuery:
    """Append canonical entity ids for recognised synonyms (no replacement)."""

    raw = query.strip()
    if not raw:
        return NormalizedQuery("", "")
    lowered = raw.lower()
    expansions: list[str] = []
    for canonical, variants in SYNONYM_TABLE.items():
        if canonical.lower() in lowered:
            continue
        if any(variant.lower() in lowered for variant in variants):
            expansions.append(canonical)
    normalized = raw if not expansions else f"{raw} {' '.join(expansions)}"
    return NormalizedQuery(raw=raw, normalized=normalized, expansions=tuple(expansions))


def parse_level_band(value: str | Sequence[int] | None) -> tuple[int, int] | None:
    """Parse ``"6-9"`` (or a 2-item sequence) into ``(6, 9)``."""

    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip().replace("–", "-").replace("~", "-")
        if not text:
            return None
        parts = [part.strip() for part in text.split("-")]
        if len(parts) == 1 and parts[0].isdigit():
            parts = [parts[0], parts[0]]
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            raise ValueError(f"等级段格式应为 min-max（如 6-9），收到 {value!r}")
        band = (int(parts[0]), int(parts[1]))
    else:
        items = list(value)
        if len(items) != 2:
            raise ValueError(f"等级段必须是两个整数，收到 {value!r}")
        band = (int(items[0]), int(items[1]))
    if band[0] < 1 or band[1] < 1:
        raise ValueError(f"等级段必须为正数，收到 {band}")
    if band[0] > band[1]:
        raise ValueError(f"等级段下限 {band[0]} 大于上限 {band[1]}")
    return band


def build_filters(
    *,
    source_type: str | Sequence[str] | None = None,
    task_type: str | Sequence[str] | None = None,
    level_band: str | Sequence[int] | None = None,
    npc_id: str | Sequence[str] | None = None,
) -> dict[str, Any]:
    """Build the neutral filter dict understood by every adapter."""

    filters: dict[str, Any] = {}
    if source_type:
        filters["source_type"] = source_type
    if task_type:
        filters["task_type"] = task_type
    if npc_id:
        filters["npc_id"] = npc_id
    band = parse_level_band(level_band)
    if band is not None:
        filters["level_band"] = band
    return normalize_filters(filters)


class Retriever:
    """Two-way (BM25 + dense) retrieval fused by RRF."""

    def __init__(
        self,
        sparse: VectorStore,
        dense: VectorStore | None = None,
        *,
        candidate_k: int = DEFAULT_CANDIDATE_K,
        fusion_k: int = DEFAULT_RRF_K,
        max_results: int = DEFAULT_MAX_RESULTS,
    ) -> None:
        if candidate_k <= 0:
            raise ValueError(f"candidate_k 必须 > 0，当前为 {candidate_k}")
        if max_results <= 0:
            raise ValueError(f"max_results 必须 > 0，当前为 {max_results}")
        if fusion_k < 0:
            raise ValueError(f"fusion_k 必须 >= 0，当前为 {fusion_k}")
        self.sparse = sparse
        self.dense = dense
        self.candidate_k = candidate_k
        self.fusion_k = fusion_k
        self.max_results = max_results

    @property
    def dense_available(self) -> bool:
        return self.dense is not None and self.dense.health()

    def search(
        self,
        query: str,
        filters: Mapping[str, Any] | None = None,
        top_k: int = 5,
        *,
        source_type: str | Sequence[str] | None = None,
        task_type: str | Sequence[str] | None = None,
        level_band: str | Sequence[int] | None = None,
        npc_id: str | Sequence[str] | None = None,
    ) -> list[Hit]:
        """Search both branches and fuse their rankings."""

        if top_k <= 0:
            raise ValueError(f"top_k 必须 > 0，当前为 {top_k}")
        normalized = normalize_query(query)
        if not normalized.normalized:
            return []
        active_filters = normalize_filters(filters)
        active_filters.update(
            build_filters(
                source_type=source_type,
                task_type=task_type,
                level_band=level_band,
                npc_id=npc_id,
            )
        )

        sparse_hits = self.sparse.search(
            normalized.normalized, active_filters, self.candidate_k
        )
        dense_hits: list[Hit] = []
        if self.dense is not None and self.dense.health():
            dense_hits = self.dense.search(
                normalized.normalized, active_filters, self.candidate_k
            )
        if not sparse_hits and not dense_hits:
            return []

        fused = fuse_rankings(
            [[hit.chunk_id for hit in sparse_hits], [hit.chunk_id for hit in dense_hits]],
            k=self.fusion_k,
        )[: self.max_results]
        by_id: dict[str, Hit] = {}
        for hit in [*sparse_hits, *dense_hits]:
            by_id.setdefault(hit.chunk_id, hit)
        results = [
            replace(by_id[chunk_id], score=score)
            for chunk_id, score in fused
            if chunk_id in by_id
        ]
        return results[: min(top_k, self.max_results)]
