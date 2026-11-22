"""Agent orchestration layer (S2/S3): Supervisor, QueryAgent, TaskAgent, graph.

Ownership: Agent-Orchestration Worker A (docs/06 §5.2). Business code in this
package depends only on the frozen contracts ``LLMClient`` (``llm/client.py``),
``Retriever``/``Hit`` (``rag/``) and ``schemas/``; it never imports a concrete
vector-store adapter or a vendor SDK.

Public entry points::

    from gameconfig_copilot.agents import build_agent_graph, QueryAgent, Supervisor
"""

from __future__ import annotations

from .context_builder import (
    BLOCK_BUDGETS,
    TOTAL_BUDGET,
    BuiltContext,
    ContextBlock,
    ContextBuilder,
    ContextWarning,
    estimate_tokens,
)
from .graph import (
    AgentGraph,
    GraphEdgeError,
    GraphNodeError,
    GraphRecursionError,
    GraphStep,
    build_agent_graph,
    build_graph,
)
from .preview import PreviewError, PreviewResult, build_preview
from .query_agent import QueryAgent, QueryResult
from .state import AgentState, Evidence, Intent, Preference, new_state
from .supervisor import RouteDecision, Supervisor
from .task_agent import (
    DraftResult,
    TaskAgent,
    TaskGenerationError,
    TaskIdAllocator,
)
from .tools import ReadOnlyTools
from .validation import fail_closed_report, schema_validate

__all__ = [
    "BLOCK_BUDGETS",
    "TOTAL_BUDGET",
    "AgentGraph",
    "AgentState",
    "BuiltContext",
    "ContextBlock",
    "ContextBuilder",
    "ContextWarning",
    "DraftResult",
    "Evidence",
    "GraphEdgeError",
    "GraphNodeError",
    "GraphRecursionError",
    "GraphStep",
    "Intent",
    "Preference",
    "PreviewError",
    "PreviewResult",
    "QueryAgent",
    "QueryResult",
    "ReadOnlyTools",
    "RouteDecision",
    "Supervisor",
    "TaskAgent",
    "TaskGenerationError",
    "TaskIdAllocator",
    "build_agent_graph",
    "build_graph",
    "build_preview",
    "estimate_tokens",
    "fail_closed_report",
    "new_state",
    "schema_validate",
]
