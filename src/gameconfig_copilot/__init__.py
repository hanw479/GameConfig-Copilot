"""GameConfig Copilot.

A retrieval-grounded, validation-gated copilot for game task configuration.

Layering (see ``docs/02-architecture.md``)::

    api -> agents -> {rag, memory, mcp_client}
    agents -> {schemas, llm}      # llm exposes only the LLMClient protocol
    mcp_server -> {schemas, validator, writer}
    evaluation -> {rag, agents, mcp_server, llm}
    observability <- every layer (through the tracer interface)

The package import is deliberately cheap and side-effect free: no settings are
read, no network client is built and no trace file is created at import time.
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["__version__"]
