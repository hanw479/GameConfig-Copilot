"""MCP server bootstrap (S5-01): mode-gated tool registration.

Owner: Write Worker M (``docs/06-multi-agent-collaboration.md`` §5.2).

Modes
-----
``readonly`` (default) registers only the read/validate/preview tools.
``write`` additionally registers ``commit_draft`` / ``rollback_commit``.
``docs/02`` §6.1 requires write tools to be *absent* from the tool list, not
merely refused at call time, so the registry is built from the mode. The mode
comes from the command line / config file only — never from a tool argument or
prompt — so an injected instruction cannot escalate it at runtime.

Transport backend
-----------------
``fastmcp`` is the canonical backend (``--transport stdio|http``). When the
package is unavailable (offline checkout, restricted CI image) this module still
builds the same local :class:`ToolRegistry` and serves ``--list-tools``, but it
does **not** pretend to speak MCP: ``--serve`` exits with code 3 and an explicit
message. Tests exercise the tools through the registry; no protocol is faked.

Usage
-----
    python -m gameconfig_copilot.mcp_server.server --mode readonly --list-tools
    python -m gameconfig_copilot.mcp_server.server --mode write --transport stdio
"""

from __future__ import annotations

import argparse
import inspect
import json
import sys
import typing
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .tools import ToolService
from .writer import REPO_ROOT, Writer, WriterError

__all__ = [
    "MODES",
    "READONLY_TOOLS",
    "WRITE_TOOLS",
    "ServerBundle",
    "ServerConfig",
    "ToolRegistry",
    "build_config",
    "build_registry",
    "build_server",
    "create_service",
    "main",
]

READONLY_TOOLS: tuple[str, ...] = (
    "get_task_config",
    "list_recent_tasks",
    "list_constraints",
    "validate_draft",
    "preview_draft",
)
WRITE_TOOLS: tuple[str, ...] = ("commit_draft", "rollback_commit")
MODES: tuple[str, ...] = ("readonly", "write")
TRANSPORTS: tuple[str, ...] = ("stdio", "http")
LOCAL_BACKEND = "local-registry"
FASTMCP_BACKEND = "fastmcp"


class ToolRegistry:
    """Mode-built name -> callable map; the single source for the tool list."""

    def __init__(self) -> None:
        self._tools: dict[str, Callable[..., Any]] = {}

    def register(self, name: str, function: Callable[..., Any]) -> None:
        if not name:
            raise ValueError("tool name must not be empty")
        if name in self._tools:
            raise ValueError(f"tool {name!r} already registered")
        self._tools[name] = function

    def get(self, name: str) -> Callable[..., Any]:
        try:
            return self._tools[name]
        except KeyError:
            raise KeyError(f"未注册的工具 {name!r}；当前模式只有 {self.names()}") from None

    def names(self) -> list[str]:
        return list(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def invoke(self, name: str, **params: Any) -> dict[str, Any]:
        """Call a registered tool directly (local registry; not MCP framing)."""

        return self.get(name)(**params)

    def describe(self) -> list[dict[str, Any]]:
        """Name, description and parameter schema for every registered tool."""

        return [
            {
                "name": name,
                "description": _tool_description(function),
                "parameters": _parameters_schema(function),
            }
            for name, function in self._tools.items()
        ]


@dataclass(frozen=True)
class ServerConfig:
    """Startup configuration resolved from CLI flags and the optional YAML file."""

    mode: str = "readonly"
    root: Path = REPO_ROOT
    transport: str = "stdio"
    host: str = "127.0.0.1"
    port: int = 8765
    tasks_dir: Path | None = None

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"mode 必须是 {MODES} 之一，实际为 {self.mode!r}")
        if self.transport not in TRANSPORTS:
            raise ValueError(f"transport 必须是 {TRANSPORTS} 之一，实际为 {self.transport!r}")
        if not 1 <= int(self.port) <= 65535:
            raise ValueError(f"port 越界：{self.port}")


@dataclass
class ServerBundle:
    """A built server: config, registry and (when available) the FastMCP app."""

    config: ServerConfig
    registry: ToolRegistry
    backend: str
    mcp: Any | None = None

    @property
    def tool_names(self) -> list[str]:
        return self.registry.names()


def create_service(config: ServerConfig, *, prepare: bool = True) -> ToolService:
    """Build the tool service; write mode optionally runs startup recovery.

    ``prepare`` is false for introspection commands (``--list-tools``) so merely
    listing the tools never creates ``data/.locks`` / ``data/backups``.
    """

    writer = Writer(config.root, tasks_dir=config.tasks_dir)
    if prepare and config.mode == "write":
        writer.prepare()
    return ToolService(writer)


def build_registry(service: ToolService, mode: str = "readonly") -> ToolRegistry:
    """Register tools for ``mode``; write tools never exist in readonly mode."""

    if mode not in MODES:
        raise ValueError(f"mode 必须是 {MODES} 之一，实际为 {mode!r}")
    registry = ToolRegistry()
    names = READONLY_TOOLS + (WRITE_TOOLS if mode == "write" else ())
    for name in names:
        registry.register(name, getattr(service, name))
    return registry


def build_server(
    config: ServerConfig,
    *,
    service: ToolService | None = None,
    prepare: bool | None = None,
) -> ServerBundle:
    """Build the registry and, when fastmcp is installed, the MCP app."""

    active_service = service or create_service(
        config,
        prepare=prepare if prepare is not None else config.mode == "write",
    )
    registry = build_registry(active_service, config.mode)
    fastmcp_cls = _load_fastmcp()
    if fastmcp_cls is None:
        return ServerBundle(config=config, registry=registry, backend=LOCAL_BACKEND)
    mcp = _create_mcp(fastmcp_cls, registry)
    return ServerBundle(config=config, registry=registry, backend=FASTMCP_BACKEND, mcp=mcp)


def build_config(args: argparse.Namespace) -> ServerConfig:
    """Merge ``--config`` values with explicit CLI overrides (CLI wins)."""

    file_values: dict[str, Any] = {}
    if args.config is not None:
        file_values = _read_config_file(Path(args.config))
    mode = args.mode or file_values.get("mode") or "readonly"
    root_value = args.root or file_values.get("root") or REPO_ROOT
    transport = args.transport or file_values.get("transport") or "stdio"
    host = args.host or file_values.get("host") or "127.0.0.1"
    port = args.port if args.port is not None else int(file_values.get("port", 8765))
    tasks_value = getattr(args, "tasks_dir", None) or file_values.get("tasks_dir")
    root = Path(root_value).expanduser().resolve()
    tasks_dir = Path(tasks_value).expanduser().resolve() if tasks_value else None
    return ServerConfig(
        mode=str(mode),
        root=root,
        transport=str(transport),
        host=str(host),
        port=int(port),
        tasks_dir=tasks_dir,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; returns a process exit code instead of raising."""

    parser = argparse.ArgumentParser(
        prog="gameconfig-copilot-mcp",
        description="GameConfig Copilot MCP server (stdio/http, mode-gated tools).",
    )
    parser.add_argument("--mode", choices=MODES, default=None, help="默认 readonly")
    parser.add_argument("--config", type=Path, default=None, help="config/mcp.yaml")
    parser.add_argument("--root", type=Path, default=None, help="仓库根目录")
    parser.add_argument("--tasks-dir", type=Path, default=None, help="覆盖 data/tasks")
    parser.add_argument("--transport", choices=TRANSPORTS, default=None)
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument(
        "--list-tools",
        action="store_true",
        help="打印当前 mode 注册的工具列表并退出（不启动服务）",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        config = build_config(args)
        bundle = build_server(config, prepare=not args.list_tools)
    except ValueError as exc:
        print(f"MCP 配置错误：{exc}", file=sys.stderr)
        return 2
    except WriterError as exc:
        print(f"MCP 启动失败：{exc}", file=sys.stderr)
        return 2

    if args.list_tools:
        print(json.dumps(bundle.registry.describe(), ensure_ascii=False, indent=2))
        return 0
    if bundle.backend == LOCAL_BACKEND:
        print(
            "fastmcp 不可用（未安装或缺少 server extras）：无法提供 MCP 传输（不伪造协议）。"
            "已构建本地工具注册表，可用 --list-tools 检查；安装 fastmcp 后再启动服务。",
            file=sys.stderr,
        )
        return 3
    if config.transport == "http":
        bundle.mcp.run(transport="http", host=config.host, port=config.port)
    else:
        bundle.mcp.run(transport="stdio")
    return 0


def _load_fastmcp() -> type[Any] | None:
    try:
        from fastmcp import FastMCP
    except ImportError:  # not installed, or installed without the server extras
        return None
    return FastMCP


def _create_mcp(fastmcp_cls: type[Any], registry: ToolRegistry) -> Any:
    """Create a FastMCP app and register exactly the mode-selected tools."""

    functions = [registry.get(name) for name in registry.names()]
    try:
        return fastmcp_cls(name="gameconfig-copilot", tools=functions)
    except TypeError:  # older fastmcp: the constructor has no ``tools`` argument
        mcp = fastmcp_cls(name="gameconfig-copilot")
        for name, function in zip(registry.names(), functions, strict=True):
            _register_tool(mcp, name, function)
        return mcp


def _register_tool(mcp: Any, name: str, function: Callable[..., Any]) -> None:
    """Register one function across fastmcp API generations."""

    description = _tool_description(function)
    tool_method = getattr(mcp, "tool", None)
    if callable(tool_method):
        try:
            tool_method(function, name=name, description=description)
            return
        except TypeError:
            pass
    add_tool = getattr(mcp, "add_tool", None)
    if callable(add_tool):
        try:
            add_tool(function, name=name, description=description)
        except TypeError:
            add_tool(function)
        return
    raise TypeError("fastmcp backend exposes neither tool() nor add_tool()")


def _read_config_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"配置文件不存在：{path}")
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"配置文件无法解析：{exc}") from exc
    if document is None:
        return {}
    if not isinstance(document, dict):
        raise ValueError("配置文件顶层必须是映射")
    section = document.get("mcp", document)
    if not isinstance(section, dict):
        raise ValueError("配置文件的 mcp 段必须是映射")
    return dict(section)


def _tool_description(function: Callable[..., Any]) -> str:
    doc = inspect.getdoc(function) or ""
    return doc.strip() or getattr(function, "__name__", "tool")


def _parameters_schema(function: Callable[..., Any]) -> dict[str, Any]:
    signature = inspect.signature(function)
    try:
        hints = typing.get_type_hints(function)
    except (NameError, TypeError):  # pragma: no cover - exotic annotations
        hints = {}
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, parameter in signature.parameters.items():
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            continue
        schema = _annotation_schema(hints.get(name, parameter.annotation))
        if parameter.default is inspect.Parameter.empty:
            required.append(name)
        elif isinstance(parameter.default, (str, int, float, bool)) or parameter.default is None:
            schema = {**schema, "default": parameter.default}
        properties[name] = schema
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _annotation_schema(annotation: Any) -> dict[str, Any]:
    if annotation is inspect.Parameter.empty or annotation is Any:
        return {}
    if annotation is str:
        return {"type": "string"}
    if annotation is bool:
        return {"type": "boolean"}
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        return {"type": "number"}
    if annotation is dict:
        return {"type": "object"}
    if annotation is list:
        return {"type": "array"}
    origin = typing.get_origin(annotation)
    if origin is dict:
        return {"type": "object"}
    if origin is list:
        return {"type": "array"}
    arguments = typing.get_args(annotation)
    non_null = [item for item in arguments if item is not type(None)]
    if non_null and len(non_null) < len(arguments):
        return {**_annotation_schema(non_null[0]), "nullable": True}
    if non_null:
        return _annotation_schema(non_null[0])
    return {}


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
