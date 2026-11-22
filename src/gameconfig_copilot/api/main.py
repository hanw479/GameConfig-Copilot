"""FastAPI application factory for the read-only loop + two-phase write closure.

``create_app`` accepts injected ``llm`` / ``retriever`` / ``graph`` / ``tools``
so tests and offline demos never touch a provider, a real index, or ``data/``
(the integration fixtures inject a ``ToolService`` rooted at a ``tmp_path``
copy). The module-level ``app`` for ``uvicorn gameconfig_copilot.api.main:app``
builds only session storage at import time; the graph, API key and index are
created on first message.

MCP mode is a **startup** parameter (``create_app(mcp_mode=...)`` or
``GCC_MCP_MODE``), never a runtime API — a prompt cannot escalate it. It defaults
to ``readonly``: ``commit_draft`` then does not exist and ``/confirm`` returns
403, so the demo is fail-closed unless write mode is explicitly enabled. Passing
an injected ``tools`` service implies write mode for tests.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from ..llm import LLMClient
from ..mcp_server.tools import ToolService
from ..observability.tracer import Tracer, get_tracer
from ..rag.retriever import Retriever
from .routes import router
from .runtime import MODES, AppRuntime
from .sessions import DEFAULT_SESSION_TTL_S, SessionStore

__all__ = ["create_app"]


def _session_ttl() -> float:
    raw = os.environ.get("GCC_SESSION_TTL_S")
    if not raw:
        return DEFAULT_SESSION_TTL_S
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"GCC_SESSION_TTL_S 必须是数字，收到 {raw!r}") from exc
    if value <= 0:
        raise ValueError("GCC_SESSION_TTL_S 必须 > 0")
    return value


def _mcp_mode(explicit: str | None, *, tools: ToolService | None) -> str:
    default = "write" if tools is not None else "readonly"
    raw = explicit or os.environ.get("GCC_MCP_MODE") or default
    if raw not in MODES:
        raise ValueError(f"GCC_MCP_MODE 必须是 {MODES} 之一，收到 {raw!r}")
    return raw


def create_app(
    *,
    llm: LLMClient | None = None,
    retriever: Retriever | None = None,
    graph: Any | None = None,
    tools: ToolService | None = None,
    mcp_mode: str | None = None,
    tracer: Tracer | None = None,
    session_ttl_s: float | None = None,
    commit_ttl_s: float | None = None,
    tasks_dir: str | Path | None = None,
    index_dir: str | Path | None = None,
    clock: Any | None = None,
) -> FastAPI:
    """Build the application with optional dependency injection."""

    sessions = SessionStore(
        ttl_s=session_ttl_s if session_ttl_s is not None else _session_ttl(),
        commit_ttl_s=commit_ttl_s if commit_ttl_s is not None else 600.0,
        clock=clock,
    )
    runtime = AppRuntime(
        sessions=sessions,
        tracer=tracer or get_tracer(),
        llm=llm,
        retriever=retriever,
        graph=graph,
        tools=tools,
        mcp_mode=_mcp_mode(mcp_mode, tools=tools),
        tasks_dir=Path(tasks_dir or os.environ.get("GCC_TASKS_DIR") or "data/tasks"),
        index_dir=Path(index_dir).expanduser() if index_dir else None,
    )
    app = FastAPI(
        title="GameConfig Copilot API",
        version="0.1.0",
        description="Retrieval-grounded drafting, deterministic validation, gated writes.",
    )
    app.state.runtime = runtime
    app.include_router(router)

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {
            "status": "ok",
            "service": "gameconfig-copilot",
            "wave": "S2+S3+S4+S5",
            "sessions": len(runtime.sessions),
            "graph_ready": runtime.describe()["graph_ready"],
            # Local MCP registry status (no protocol when fastmcp is absent).
            "mcp": {"connected": runtime.write_enabled, "mode": runtime.mcp_mode},
        }

    return app


app = create_app()
