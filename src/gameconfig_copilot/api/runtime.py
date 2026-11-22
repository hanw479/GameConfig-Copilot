"""API runtime container: lazy graph/LLM/retriever/MCP-tool wiring + sessions.

The HTTP layer never imports a provider SDK or vector-store adapter; it asks the
runtime for the already-built :class:`AgentGraph` and, in write mode, for the
MCP :class:`ToolService`. Construction is lazy so ``/healthz`` and session
creation work on a fresh checkout with no API key and no index; the first
message reports a clean error instead of crashing startup.

The MCP layer is composed **here** (and not in ``agents/``): the graph receives
the ``preview_draft`` / ``commit_draft`` callables as ports, which keeps the
orchestration layer transport-agnostic and preserves the ADR-003 process
boundary for when ``fastmcp`` is available (the local registry is the offline
transport used by the demo and tests).

Input
    Injected graph/llm/retriever/tools (tests) or defaults from settings/index.
Output
    A cached ``AgentGraph`` plus a ``SessionStore`` and an optional ``ToolService``.
Failure semantics
    Missing index/API key raises on first use; the route handler converts it to
    an ``error`` SSE event. Read-only mode never registers ``commit_draft``, and
    the graph's commit node refuses with ``E_WRITE_DISABLED``.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..agents.graph import AgentGraph, build_agent_graph
from ..llm import LLMClient, create_client
from ..mcp_server.tools import ToolService
from ..mcp_server.writer import REPO_ROOT, Writer
from ..observability.tracer import Tracer, get_tracer
from ..rag.index import DEFAULT_INDEX_DIR, load_index
from ..rag.retriever import Retriever
from .sessions import SessionStore

__all__ = ["AppRuntime", "default_retriever"]

MODES: tuple[str, ...] = ("readonly", "write")


def default_retriever(index_dir: str | Path | None = None) -> Retriever:
    """Load ``out/index`` and wrap it in the hybrid retriever."""

    configured = index_dir or os.environ.get("GCC_INDEX_DIR") or DEFAULT_INDEX_DIR
    rag_index = load_index(Path(configured).expanduser())
    return Retriever(rag_index.sparse, rag_index.dense)


@dataclass
class AppRuntime:
    """Process-wide dependencies shared by all requests."""

    sessions: SessionStore
    tracer: Tracer = field(default_factory=get_tracer)
    llm: LLMClient | None = None
    retriever: Retriever | None = None
    graph: AgentGraph | None = None
    tools: ToolService | None = None
    mcp_mode: str = "readonly"
    tasks_dir: Path = Path("data/tasks")
    index_dir: Path | None = None
    _graph_cache: AgentGraph | None = field(default=None, init=False, repr=False)
    _tools_cache: ToolService | None = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(
        default_factory=threading.RLock, init=False, repr=False
    )

    def __post_init__(self) -> None:
        if self.mcp_mode not in MODES:
            raise ValueError(f"mcp_mode 必须是 {MODES} 之一，实际为 {self.mcp_mode!r}")
        if self.tools is not None:
            self._tools_cache = self.tools

    @property
    def write_enabled(self) -> bool:
        """Whether the commit path is available (write mode + a tool service)."""

        return self.mcp_mode == "write"

    def get_tool_service(self) -> ToolService | None:
        """Return the MCP tool service in write mode, else ``None``.

        The default service writes under the repository root (``data/tasks``,
        ``data/backups``, ``traces/commits.jsonl``); tests inject a service
        rooted at a ``tmp_path`` copy so ``data/`` is never touched.
        """

        if not self.write_enabled:
            return None
        if self._tools_cache is not None:
            return self._tools_cache
        with self._lock:
            if self._tools_cache is None:
                if self.tasks_dir.is_absolute():
                    tasks = self.tasks_dir
                    # Keep backups/locks/audit next to an injected corpus root
                    # (``<root>/data/tasks``) instead of the repository's data/.
                    root = (
                        tasks.parent.parent
                        if tasks.parent.name == "data"
                        else tasks.parent
                    )
                else:
                    tasks = REPO_ROOT / self.tasks_dir
                    root = REPO_ROOT
                writer = Writer(root, tasks_dir=tasks)
                # Mirror the MCP server's write-mode startup: create dirs,
                # clean stale temp files and reconcile the audit tail.
                writer.prepare()
                self._tools_cache = ToolService(writer)
            return self._tools_cache

    def get_graph(self) -> AgentGraph:
        """Return the compiled graph, building it on first use."""

        if self._graph_cache is not None:
            return self._graph_cache
        with self._lock:
            if self._graph_cache is not None:
                return self._graph_cache
            tools = self.get_tool_service()
            if self.graph is not None:
                self._graph_cache = self.graph
                return self._graph_cache
            llm = self.llm or create_client()
            retriever = self.retriever or default_retriever(self.index_dir)
            self._graph_cache = build_agent_graph(
                llm,
                retriever,
                tracer=self.tracer,
                previewer=tools.preview_draft if tools is not None else None,
                committer=tools.commit_draft if tools is not None else None,
                tasks_dir=str(self.tasks_dir),
            )
            return self._graph_cache

    def describe(self) -> dict[str, Any]:
        return {
            "graph_ready": self._graph_cache is not None,
            "sessions": len(self.sessions),
            "tasks_dir": str(self.tasks_dir),
            "mcp_mode": self.mcp_mode,
            "tools_ready": self._tools_cache is not None,
        }
