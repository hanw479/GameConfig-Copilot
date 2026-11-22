"""Agent state contract (docs/02 §4.1).

``AgentState`` mirrors the frozen ``AgentState`` TypedDict from the architecture
document field-for-field. A few **additive** keys are documented inline
(``answer``, ``citations``, ``warnings``, ``tokens``, ``context``, ``visited``,
``human_decision``, ``commit``) because the state machine needs to carry them
between nodes; they never replace a documented field.

Input
    A request string plus session identifiers.
Output
    A mutable ``dict`` shaped like :class:`AgentState`.
Failure semantics
    ``new_state`` rejects an empty request so a blank message cannot silently
    become an ``other`` route.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, TypedDict

from ..rag.store import Hit

__all__ = [
    "AgentState",
    "Evidence",
    "Intent",
    "Preference",
    "new_state",
]


class Intent(str, Enum):
    """The three Supervisor outcomes frozen by docs/02 §4.2."""

    QUERY = "query"
    TASK = "task"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class Evidence:
    """One retrieved snippet plus its citation (docs/02 §5.2).

    ``citation`` is ``source_id#position`` and is exactly the string that
    ``provenance.evidence`` and the QueryAgent answer must quote, so every
    citation in a model answer can be traced back to a stored chunk.
    """

    chunk_id: str
    source_id: str
    text: str
    score: float = 0.0
    position: str = ""
    source_type: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.chunk_id:
            raise ValueError("Evidence.chunk_id 不能为空")
        if not self.source_id:
            raise ValueError("Evidence.source_id 不能为空")

    @property
    def citation(self) -> str:
        return f"{self.source_id}#{self.position}" if self.position else self.source_id

    @classmethod
    def from_hit(cls, hit: Hit) -> Evidence:
        """Convert the retrieval-layer ``Hit`` into a citation-ready snippet."""

        metadata = dict(hit.metadata)
        return cls(
            chunk_id=hit.chunk_id,
            source_id=hit.source_id,
            text=hit.text,
            score=float(hit.score),
            position=hit.position,
            source_type=str(metadata.get("source_type") or hit.source_type),
            metadata=metadata,
        )

    def to_dict(self) -> dict[str, Any]:
        """SSE/JSON view used by the ``evidence`` event (docs/02 §10.2)."""

        return {
            "source": self.citation,
            "source_id": self.source_id,
            "source_type": self.source_type,
            "chunk_id": self.chunk_id,
            "score": round(self.score, 6),
            "snippet": self.text,
        }


@dataclass(frozen=True, slots=True)
class Preference:
    """Structured designer preference (docs/02 §5.3 budget 300).

    The memory worker (S6) is the eventual producer; S2/S3 only define the shape
    so context assembly can inject it without parsing free-form prose.
    """

    reward_band: Mapping[str, Any] | None = None
    naming: str | None = None
    tags: tuple[str, ...] = ()
    notes: str | None = None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> Preference:
        if not payload:
            return cls()
        tags = payload.get("tags") or ()
        if isinstance(tags, str):
            tags = (tags,)
        band = payload.get("reward_band")
        return cls(
            reward_band=dict(band) if isinstance(band, Mapping) else None,
            naming=str(payload["naming"]) if payload.get("naming") else None,
            tags=tuple(str(item) for item in tags),
            notes=str(payload["notes"]) if payload.get("notes") else None,
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if self.reward_band:
            payload["reward_band"] = dict(self.reward_band)
        if self.naming:
            payload["naming"] = self.naming
        if self.tags:
            payload["tags"] = list(self.tags)
        if self.notes:
            payload["notes"] = self.notes
        return payload


class AgentState(TypedDict, total=False):
    """Graph state; field names frozen by docs/02 §4.1.

    Documented fields::

        trace_id, session_id, designer_id, messages, intent, request, evidence,
        constraints, preference, draft, draft_version, report, repair_round,
        preview, stage, error

    Additive (documented extensions, never replacements)::

        answer        final QueryAgent answer text
        citations     citation strings quoted by the answer
        warnings      non-fatal issues surfaced as SSE ``warning`` events
        tokens        accumulated prompt-token estimate for the ``done`` event
        cost_cny      accumulated cost estimate for the ``done`` event
        context       the BuiltContext reused by ``draft`` (avoids rebuilding)
        visited       node names in execution order (tests / tracing)
        route_decision  Supervisor decision snapshot for observability
        human_decision  confirmation injected by ``api/routes.py`` before a
                        resumed ``human_gate`` run (status/note/draft_id)
        commit        CommitResult summary written by the ``commit`` node
    """

    trace_id: str
    session_id: str
    designer_id: str
    messages: list[Any]
    intent: str
    request: str
    evidence: list[Evidence]
    constraints: list[Evidence]
    preference: Preference
    draft: dict[str, Any] | None
    draft_version: int
    report: dict[str, Any] | None
    repair_round: int
    preview: dict[str, Any] | None
    stage: str
    error: dict[str, Any] | None
    answer: str
    citations: list[str]
    warnings: list[dict[str, Any]]
    tokens: int
    cost_cny: float
    context: Any
    visited: list[str]
    route_decision: dict[str, Any]
    human_decision: dict[str, Any]
    commit: dict[str, Any] | None


def new_state(
    request: str,
    *,
    trace_id: str = "",
    session_id: str = "",
    designer_id: str = "",
    messages: list[Any] | None = None,
) -> AgentState:
    """Create the initial state for one request."""

    if not request or not request.strip():
        raise ValueError("request 不能为空")
    return AgentState(
        trace_id=trace_id,
        session_id=session_id,
        designer_id=designer_id,
        messages=list(messages or []),
        request=request.strip(),
        evidence=[],
        constraints=[],
        draft=None,
        draft_version=0,
        report=None,
        repair_round=0,
        preview=None,
        stage="",
        error=None,
        answer="",
        citations=[],
        warnings=[],
        tokens=0,
        cost_cny=0.0,
        visited=[],
        human_decision={},
        commit=None,
    )
